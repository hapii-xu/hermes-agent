#!/usr/bin/env python3
"""
文件操作模块

提供文件操作能力（读取、写入、打补丁、搜索），可跨所有终端后端工作
（本地、docker、ssh、singularity、modal、daytona）。

核心思路是：所有文件操作都可以表达为 shell 命令，因此我们包装终端后端的
execute() 接口，提供统一的文件 API。

用法：
    from tools.file_operations import ShellFileOperations
    from tools.terminal_tool import _active_environments

    # 为某个终端环境获取文件操作对象
    file_ops = ShellFileOperations(terminal_env)

    # 读取文件
    result = file_ops.read_file("/path/to/file.py")

    # 写入文件
    result = file_ops.write_file("/path/to/new.py", "print('hello')")

    # 搜索内容
    result = file_ops.search("TODO", path=".", file_glob="*.py")
"""

import os
import re
import difflib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, ClassVar
from pathlib import Path
from tools.binary_extensions import BINARY_EXTENSIONS

from agent.file_safety import (
    build_write_denied_paths,
    build_write_denied_prefixes,
    is_write_denied as _shared_is_write_denied,
)


# ---------------------------------------------------------------------------
# 写入路径黑名单 —— 拦截对敏感系统/凭据文件的写入
# ---------------------------------------------------------------------------

_HOME = str(Path.home())

WRITE_DENIED_PATHS = build_write_denied_paths(_HOME)

WRITE_DENIED_PREFIXES = build_write_denied_prefixes(_HOME)


_OSC_SEQUENCE_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_FENCE_MARKER_RE = re.compile(r"'?\x07?__HERMES_FENCE_[A-Za-z0-9]+__\x07?'?")


def _strip_terminal_fence_leaks(text: str) -> str:
    """从文件读取输出中剥离泄露的终端 fence 包装标记。"""
    if not text:
        return text

    cleaned_lines: List[str] = []
    for line in text.splitlines(keepends=True):
        had_terminal_wrapper = "__HERMES_FENCE_" in line or "\x1b]" in line
        cleaned = _OSC_SEQUENCE_RE.sub("", line)
        cleaned = _FENCE_MARKER_RE.sub("", cleaned)
        cleaned = cleaned.replace("\x07", "")
        if had_terminal_wrapper and cleaned.strip("'\r\n\t ") == "":
            continue
        cleaned_lines.append(cleaned)
    return "".join(cleaned_lines)


def _detect_line_ending(sample: str) -> Optional[str]:
    """返回 ``sample`` 中占主导的换行符，若无法判定则返回 None。

    检查前几个换行符，若出现 ``\\r\\n``（Windows / DOS）则选用它，
    否则选用 ``\\n``（Unix）。对于无法判定的空内容/单行内容返回
    ``None``。用于在 write_file 和 patch 操作之间保留文件原始换行符
    ——否则 agent 工具参数里的裸 LF 会把 Windows 换行文件静默归一化，
    而当只有被替换区域发生变化时，patch 会产生混合换行符。
    """
    if not sample:
        return None
    # 检查第一段内容即可判定，且扫描代价很小。
    head = sample[:4096]
    if "\r\n" in head:
        return "\r\n"
    if "\n" in head:
        return "\n"
    return None


def _normalize_line_endings(text: str, target: str) -> str:
    """将 ``text`` 中所有换行符转换为 ``target``（``\\n`` 或 ``\\r\\n``）。

    幂等：``_normalize_line_endings(_normalize_line_endings(x, "\\r\\n"), "\\r\\n") == _normalize_line_endings(x, "\\r\\n")``。
    同时剥离孤立的 ``\\r`` 字符，因此混合换行的内容可一次性归一化。
    """
    # 先归一到 LF（处理 CRLF 和孤立 CR），再在目标为 CRLF 时展开。
    # 顺序很关键：分别替换会把 CRLF 双重转换成 LFLF。
    lf_normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if target == "\n":
        return lf_normalized
    if target == "\r\n":
        return lf_normalized.replace("\n", "\r\n")
    return text


# UTF-8 字节顺序标记（BOM）。某些 Windows 编辑器（记事本、较旧的 Visual Studio、
# 部分 PowerShell 重定向）会在 UTF-8 文本文件前加上这个不可见的 3 字节标记
#（EF BB BF == U+FEFF）。它渲染为空白，但在解码字符串开头是一个真实字符，
# 因此若不处理：
#   - read_file 会把多余的 U+FEFF 作为第一个字符暴露出来（模型在 `import ...`
#     之前看到一个幽灵字符），并且
#   - patch 针对真实首行的匹配会落空，write_file 在重写时会静默丢弃或重复该标记。
# 我们在读取时剥离它，使模型看到干净内容；在写入时若原文件有该标记则恢复它——
# 与上面的换行符保留逻辑完全对应（在磁盘上检测，在编辑过程中保留）。
_UTF8_BOM = "\ufeff"


def _strip_bom(text: str) -> tuple[str, bool]:
    """返回 (去除开头 BOM 后的文本, 是否含有 BOM)。

    仅剥离开头的单个 BOM；出现在内容中间的 BOM 不作处理
    （在那里它是合法数据，而非文件标记）。
    """
    if text and text.startswith(_UTF8_BOM):
        return text[len(_UTF8_BOM):], True
    return text, False


def _has_bom(text: Optional[str]) -> bool:
    """若 ``text`` 以 UTF-8 BOM 开头则返回 True。"""
    return bool(text) and text.startswith(_UTF8_BOM)


def _is_write_denied(path: str) -> bool:
    """若路径在写入黑名单中则返回 True。"""
    return _shared_is_write_denied(path)


# =============================================================================
# 结果数据类
# =============================================================================

@dataclass
class ReadResult:
    """读取文件的结果。"""
    content: str = ""
    total_lines: int = 0
    file_size: int = 0
    truncated: bool = False
    hint: Optional[str] = None
    is_binary: bool = False
    is_image: bool = False
    base64_content: Optional[str] = None
    mime_type: Optional[str] = None
    dimensions: Optional[str] = None  # 图片用："宽x高"
    error: Optional[str] = None
    similar_files: List[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None and v != []}


@dataclass
class WriteResult:
    """写入文件的结果。"""
    bytes_written: int = 0
    dirs_created: bool = False
    lint: Optional[Dict[str, Any]] = None
    # 来自 LSP 层的语义诊断（适用时）。单独放在一个字段里（不并入
    # ``lint``），使模型和任何下游解析器能把语法错误与语义错误作为
    # 两路独立信号读取。在以下情况为 ``None``：LSP 被禁用、文件不在
    # git 工作区、或本次编辑未引入任何诊断。
    lsp_diagnostics: Optional[str] = None
    error: Optional[str] = None
    warning: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class PatchResult:
    """给文件打补丁的结果。"""
    success: bool = False
    diff: str = ""
    files_modified: List[str] = field(default_factory=list)
    files_created: List[str] = field(default_factory=list)
    files_deleted: List[str] = field(default_factory=list)
    lint: Optional[Dict[str, Any]] = None
    # 参见 :class:`WriteResult.lsp_diagnostics`。
    lsp_diagnostics: Optional[str] = None
    error: Optional[str] = None
    
    def to_dict(self) -> dict:
        result = {"success": self.success}
        if self.diff:
            result["diff"] = self.diff
        if self.files_modified:
            result["files_modified"] = self.files_modified
        if self.files_created:
            result["files_created"] = self.files_created
        if self.files_deleted:
            result["files_deleted"] = self.files_deleted
        if self.lint:
            result["lint"] = self.lint
        if self.lsp_diagnostics:
            result["lsp_diagnostics"] = self.lsp_diagnostics
        if self.error:
            result["error"] = self.error
        return result


@dataclass
class SearchMatch:
    """单条搜索匹配。"""
    path: str
    line_number: int
    content: str
    mtime: float = 0.0  # 修改时间，用于排序


@dataclass
class SearchResult:
    """搜索的结果。"""
    matches: List[SearchMatch] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    total_count: int = 0
    truncated: bool = False
    limit_reason: Optional[str] = None
    warning: Optional[str] = None
    error: Optional[str] = None
    
    # 当内容模式匹配数超过此阈值时，把匹配项压缩为按路径分组的文本块。
    # 低于此阈值时，详尽数组本身已足够紧凑，分组表头反而得不偿失。
    _DENSIFY_MIN_MATCHES: ClassVar[int] = 5

    def _densify_matches(self) -> Optional[str]:
        """把内容模式匹配渲染为紧凑的、按路径分组的文本块。

        尽尽形式会为每条匹配重复 ``{"path","line","content"}`` 键和完整路径
        字符串。本方法将连续匹配按路径分组（路径仅打印一次，随后是
        ``  <行号>: <内容>`` 行），该过程无损——每个路径、行号和内容字节
        都保留——且模型无需任何解码步骤即可阅读。

        当压缩得不偿失（匹配太少）时返回 ``None``，
        使调用方回退到详尽数组。
        """
        if len(self.matches) < self._DENSIFY_MIN_MATCHES:
            return None
        # ripgrep 按路径顺序输出匹配（同一文件的所有命中是连续的），
        # 因此在路径变化时分组即可把每个文件折叠为单个表头，且无需重排结果。
        lines: list[str] = []
        current_path: Optional[str] = None
        for m in self.matches:
            if m.path != current_path:
                lines.append(m.path)
                current_path = m.path
            # 仅 rstrip 尾部空白；代码中的前导缩进是有意义的，
            # 在 "<行号>: " 前缀之后原样保留。
            lines.append(f"  {m.line_number}: {m.content.rstrip()}")
        return "\n".join(lines)

    def to_dict(self, densify: bool = False) -> dict:
        result: dict[str, object] = {"total_count": self.total_count}
        if self.matches:
            dense = self._densify_matches() if densify else None
            if dense is not None:
                # 自描述：format 键告诉模型如何解读该文本块，使其无需猜测结构。
                result["matches_format"] = (
                    "path-grouped: each file path on its own line, followed by "
                    "indented '<line>: <content>' rows for matches in that file"
                )
                result["matches_text"] = dense
            else:
                result["matches"] = [
                    {"path": m.path, "line": m.line_number, "content": m.content}
                    for m in self.matches
                ]
        if self.files:
            result["files"] = self.files
        if self.counts:
            result["counts"] = self.counts
        if self.truncated:
            result["truncated"] = True
        if self.limit_reason:
            result["limit_reason"] = self.limit_reason
        if self.warning:
            result["warning"] = self.warning
        if self.error:
            result["error"] = self.error
        return result


@dataclass
class LintResult:
    """对文件做 lint 检查的结果。"""
    success: bool = True
    skipped: bool = False
    output: str = ""
    message: str = ""
    
    def to_dict(self) -> dict:
        if self.skipped:
            return {"status": "skipped", "message": self.message}
        result = {"status": "ok" if self.success else "error", "output": self.output}
        if self.message:
            result["message"] = self.message
        return result


@dataclass
class ExecuteResult:
    """执行 shell 命令的结果。"""
    stdout: str = ""
    exit_code: int = 0


_SEARCH_TIMEOUT_MARKER_RE = re.compile(r"\n?\[Command timed out after \d+s\]\s*$")


def _search_stdout_and_limit(result: ExecuteResult) -> tuple[str, Optional[str]]:
    """返回清理后用于解析的 stdout，以及搜索超时的限制原因。"""
    if result.exit_code == 124:
        return _SEARCH_TIMEOUT_MARKER_RE.sub("", result.stdout), "search_timeout"
    return result.stdout, None


def _split_tool_diagnostics(output: str) -> tuple[str, str]:
    """把 rg/grep 的诊断行与真正的匹配输出分离。

    ``_exec`` 以 ``stderr=subprocess.STDOUT`` 运行命令，因此 ``rg``/``grep``
    的错误和警告文本会与匹配行交错在同一股流里。诊断信息不能被当作匹配解析，
    而在硬失败时它们就是需要暴露的错误消息。

    返回 ``(diagnostics, payload)``，其中 ``payload`` 仅包含看起来像真正
    搜索输出的行——匹配行（``file:line:content``）、仅文件路径、计数行、
    或上下文行/分隔符。其余内容（带工具前缀的错误、rg 多行 ``regex parse
    error`` 块及其缩脱的脱字符行、空行）都被归入 ``diagnostics``。

    按*形状*而非按错误前缀分类，正是让 exit-2 守卫能区分纯失败
    （无可用 payload → 暴露错误）与部分失败（部分文件匹配、某个文件不可读
    → 保留匹配）的关键。这也意味着错误文本永远不会被误解析为匹配——
    这是先于 exit-code 修复就存在的潜在 bug。
    """
    diagnostics: list[str] = []
    payload: list[str] = []
    for line in output.split('\n'):
        if not line.strip():
            continue
        # 工具诊断总是带 "<tool>: " 前缀（例如 "rg: <file>: Permission
        # denied"、"grep: Invalid regular expression"、"rg: regex parse
        # error:"）。先检查这一点：真实匹配路径里可能合法地包含
        # "-<数字>"（例如临时目录 ".../pytest-686/..."），形状正则否则会
        # 把它当作匹配行。
        stripped = line.lstrip()
        if stripped.startswith("rg: ") or stripped.startswith("grep: "):
            diagnostics.append(line)
            continue
        # 否则按输出形状分类。rg 的 regex-parse-error 块还会输出一条
        # 缩进的脱字符行和一条无工具前缀的 "error: ..." 尾行；二者都不符合
        # 搜索输出形状，因此落入 diagnostics。
        #   匹配 / 计数 : "<path>:<...>"   （含冒号；rg -c 用 path:count）
        #   仅文件       : "<path>"         （无空白，无前导冒号）
        #   上下文行     : "<path>-<line>-" 或 "--" 分组分隔符
        if line == "--" or _SEARCH_OUTPUT_RE.match(line):
            payload.append(line)
        else:
            diagnostics.append(line)
    return '\n'.join(diagnostics), '\n'.join(payload)


# 真正的 rg/grep 输出行以一个路径 token 开头，其后紧跟 ``:``（匹配/计数）、
# ``-``（上下文）或无（仅文件）。工具诊断（"rg: ..."、"grep: ..."、
# "error: ..."、缩进的脱字符）永远不会匹配，因为路径 token 禁止空白，
# 且前导工具前缀如 "rg" 后跟 ": "（空格）会被否定字符类拒绝。
_SEARCH_OUTPUT_RE = re.compile(r'^([A-Za-z]:)?[^\s:][^\n]*?[:\-]\d|^[^\s:][^\s]*$')


def _parse_search_context_line(line: str) -> tuple[str, int, str] | None:
    """解析 ``path-line-content`` 格式的 grep/rg 上下文输出。

    上下文行存在歧义，因为文件名里可能合法地包含 ``-<数字>-`` 片段。
    优先选取最右侧的数字分隔符，使类似 ``dir/file-12-name.py-8-context``
    的路径解析为 ``dir/file-12-name.py`` 第 ``8`` 行，而不是在 ``file``
    处截断。
    """
    if not line or line == "--":
        return None

    match = None
    for candidate in re.finditer(r'-(\d+)-', line):
        match = candidate

    if match is None:
        return None

    path = line[:match.start()]
    if not path:
        return None

    return path, int(match.group(1)), line[match.end():]


# =============================================================================
# 抽象接口
# =============================================================================

class FileOperations(ABC):
    """跨终端后端文件操作的抽象接口。"""

    @abstractmethod
    def read_file(self, path: str, offset: int = 1, limit: int = 500) -> ReadResult:
        """读取文件，支持分页。"""
        ...

    @abstractmethod
    def read_file_raw(self, path: str) -> ReadResult:
        """以纯字符串形式读取完整文件内容。

        无分页、无行号前缀、无逐行截断。返回 ReadResult，
        其中 .content 为完整文件文本，失败时设置 .error。
        无论文件大小，始终读到 EOF。
        """
        ...

    @abstractmethod
    def write_file(self, path: str, content: str) -> WriteResult:
        """向文件写入内容，按需创建目录。"""
        ...

    @abstractmethod
    def patch_replace(self, path: str, old_string: str, new_string: str,
                      replace_all: bool = False) -> PatchResult:
        """使用模糊匹配替换文件中的文本。"""
        ...

    @abstractmethod
    def patch_v4a(self, patch_content: str) -> PatchResult:
        """应用 V4A 格式的补丁。"""
        ...

    @abstractmethod
    def delete_file(self, path: str) -> WriteResult:
        """删除文件。失败时返回设置了 .error 的 WriteResult。"""
        ...

    def delete_path(self, path: str, recursive: bool = False) -> WriteResult:
        """跨平台删除，可处理文件，并在 recursive=True 时处理目录树。
        默认实现把非递归情形委托给 ``delete_file``；具备原生递归支持
        的后端应重写此方法。
        """
        if recursive:
            return WriteResult(error="Recursive delete not implemented for this backend")
        return self.delete_file(path)

    @abstractmethod
    def move_file(self, src: str, dst: str) -> WriteResult:
        """把文件从 src 移动/重命名到 dst。失败时返回设置了 .error 的 WriteResult。"""
        ...

    @abstractmethod
    def search(self, pattern: str, path: str = ".", target: str = "content",
               file_glob: Optional[str] = None, limit: int = 50, offset: int = 0,
               output_mode: str = "content", context: int = 0) -> SearchResult:
        """搜索内容或文件。"""
        ...


# =============================================================================
# 基于 shell 的实现
# =============================================================================

# 图片扩展名（二进制的一个子集，可作为 base64 返回）
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.ico'}

# 按文件扩展名索引的 shell linter。通过 _exec() 以文件系统路径调用。
# 覆盖那些编译/类型检查需要外部工具链的语言
#（py_compile、node、tsc、go vet、rustfmt）。
LINTERS = {
    '.py': 'python -m py_compile {file} 2>&1',
    '.js': 'node --check {file} 2>&1',
    '.ts': 'npx tsc --noEmit {file} 2>&1',
    '.go': 'go vet {file} 2>&1',
    '.rs': 'rustfmt --check {file} 2>&1',
}

# 这些扩展名的逐文件 shell linter 在结构上弱于真正的 LSP 服务器，且在真实
# 项目中会产生幻影错误：
#
# - ``.ts``：``tsc --noEmit FILE.ts`` 忽略 ``tsconfig.json``，并默认为
#   no-lib / ES5，因此每个 ES2015+ 标准库引用（``Promise``、``Map``、
#   ``Set``、``ReadonlySet``、``Iterable``、``Math.imul``、
#   ``Number.isFinite`` 等）都会报告为缺失。这会在每次编辑时用 20K+
#   token 的误报淹没 agent 的 lint 字段。没有受支持的 tsc 标志能修复单
#   文件调用；标准替代方案是通过 LSP 调用 ``tsserver``，它会遵循
#   tsconfig 并给出真实诊断。
#
#   ``.tsx`` 故意不在 ``LINTERS`` 中（因此也不在此处）：它没有 shell
#   linter 条目，所以会原样落入 ``ext not in LINTERS`` 的跳过分支。本 PR
#   之前的行为：``.tsx`` 隐式地 ``skipped``。保持这一行为意味着禁用 LSP
#   时 ``.tsx`` 编辑不获得逐文件语法检查（与本 PR 之前一致），而不是像
#   ``.ts`` 那样跑损坏的 ``tsc`` 调用。启用 LSP 时，``.tsx`` 由 LSP 层
#   经 ``_maybe_lsp_diagnostics`` 覆盖，与 ``.ts`` 完全一致。
#
# - ``.go``：``go vet FILE.go`` 在模块 / GOPATH 之外会以 "cannot find
#   package" 失败——已由 ``_LINTER_UNUSABLE_PATTERNS`` 部分处理，但仅
#   当该包错误是唯一输出时；混合的真实+幻影输出仍会漏过。
#   ``gopls`` 是标准替代方案。
#
# - ``.rs``：``rustfmt --check FILE.rs`` 检查的是风格而非类型，且会拒绝
#   非 Cargo 项目文件。``rust-analyzer`` 是标准替代方案。
#
# 当 LSP 服务已配置且对该扩展名文件 ``enabled_for(path)`` 为真时，
# ``_check_lint`` 会跳过这些扩展名的 shell linter——由
# ``lsp_diagnostics`` 通道承载真实信号。``LINTERS`` 中的其余项
#（Python ``py_compile``、``node --check``）快速、文件局部且正确，
# 因此无条件运行。
_SHELL_LINTER_LSP_REDUNDANT = frozenset({'.ts', '.go', '.rs'})


# 这些模式表示 linter 的基础命令存在于 PATH 中，但实际上无法运行——例如
# ``npx tsc`` 在 tsc 未安装到 node_modules 时，或 rustfmt 抱怨没有 Cargo
# 项目时。当 linter 输出中出现这些子串之一时，``_check_lint`` 返回
# ``skipped`` 而非 ``error``，以便：
#
# 1. 写入不会因为 agent 无法修复的工具链问题而被标记。
# 2. LSP 语义层仍会运行（它依据 success/skipped 放行）。
#
# 这些模式对 linter stdout 做大小写不敏感匹配。
_LINTER_UNUSABLE_PATTERNS = {
    'npx': (
        # 当包未在本地安装且无法自动安装（无网络、注册表关闭等），
        # 或它尝试运行的二进制不对时，npx 会打印此横幅。
        'this is not the tsc command you are looking for',
        # npx 在 --no-install 解析失败时
        'could not determine executable to run',
        'not found in npm registry',
    ),
    'rustfmt': (
        # rustfmt 在 Cargo 项目之外运行
        'no input filename given',
        'error: not a workspace',
    ),
    'go': (
        # ``go vet`` 作用于模块 / GOPATH 之外的文件
        'cannot find package',
        'go: cannot find main module',
    ),
}


def _looks_like_linter_unusable(base_cmd: str, output: str) -> bool:
    """当 ``base_cmd`` 的 ``output`` 表明 linter 自身无法运行
    （工具链缺口），而非被检查文件的真实 lint 错误时，返回 True。

    ``base_cmd`` 是 linter 命令行的第一个单词（``npx``、
    ``rustfmt``、``go``……）。``output`` 是运行它时捕获的 stdout/stderr。
    """
    patterns = _LINTER_UNUSABLE_PATTERNS.get(base_cmd)
    if not patterns:
        return False
    lower = output.lower()
    return any(p in lower for p in patterns)


def _lint_json_inproc(content: str) -> tuple[bool, str]:
    """进程内 JSON 语法检查。返回 (ok, error_message)。"""
    import json as _json
    try:
        _json.loads(content)
        return True, ""
    except _json.JSONDecodeError as e:
        return False, f"JSONDecodeError: {e.msg} (line {e.lineno}, column {e.colno})"
    except Exception as e:  # noqa: BLE001 — 任何解析失败都视为 lint 失败
        return False, f"{type(e).__name__}: {e}"


def _lint_yaml_inproc(content: str) -> tuple[bool, str]:
    """进程内 YAML 语法检查。返回 (ok, error_message)。

    若未安装 PyYAML 则优雅跳过——YAML 解析是可选的。
    """
    try:
        import yaml as _yaml
    except ImportError:
        # PyYAML 不可用——静默跳过，调用方视为无 linter。
        return True, "__SKIP__"
    try:
        _yaml.safe_load(content)
        return True, ""
    except _yaml.YAMLError as e:
        return False, f"YAMLError: {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _lint_toml_inproc(content: str) -> tuple[bool, str]:
    """进程内 TOML 语法检查（标准库 tomllib，Python 3.11+）。"""
    try:
        import tomllib as _toml
    except ImportError:
        # 3.11 之前通过 tomli 回退（若已安装）。
        try:
            import tomli as _toml  # type: ignore[no-redef]
        except ImportError:
            return True, "__SKIP__"
    try:
        _toml.loads(content)
        return True, ""
    except Exception as e:  # tomllib 抛出 TOMLDecodeError，它是 ValueError 的子类
        return False, f"{type(e).__name__}: {e}"


def _lint_python_inproc(content: str) -> tuple[bool, str]:
    """通过 ast.parse 进行进程内 Python 语法检查。

    捕获 SyntaxError、IndentationError 以及 ast 模块拒绝的其他一切
    ——覆盖范围与 py_compile 相同，但没有子进程开销，也不依赖 PATH
    中存在 ``python``。
    """
    import ast as _ast
    try:
        _ast.parse(content)
        return True, ""
    except SyntaxError as e:
        loc = f" (line {e.lineno}, column {e.offset})" if e.lineno else ""
        return False, f"{type(e).__name__}: {e.msg}{loc}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


# 按文件扩展名索引的进程内 linter。存在时优先于 shell linter——无子进程
# 开销，每次调用仅微秒级。每个可调用对象接收文件内容（str），返回
#(ok: bool, error: str)。错误字符串为 ``"__SKIP__"`` 表示该 linter 不可用
#（缺少依赖），应视为"无 linter"。
LINTERS_INPROC = {
    '.py': _lint_python_inproc,
    '.json': _lint_json_inproc,
    '.yaml': _lint_yaml_inproc,
    '.yml': _lint_yaml_inproc,
    '.toml': _lint_toml_inproc,
}

# 读取操作的最大限制
MAX_LINES = 2000
MAX_LINE_LENGTH = 2000
MAX_FILE_SIZE = 50 * 1024  # 50KB
DEFAULT_READ_OFFSET = 1
DEFAULT_READ_LIMIT = 500
DEFAULT_SEARCH_OFFSET = 0
DEFAULT_SEARCH_LIMIT = 50


def _coerce_int(value: Any, default: int) -> int:
    """对工具分页输入做尽力而为的整数转换。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_read_pagination(offset: Any = DEFAULT_READ_OFFSET,
                              limit: Any = DEFAULT_READ_LIMIT) -> tuple[int, int]:
    """返回安全的 read_file 分页边界。

    工具 schema 声明了最小/最大值，但并非每个调用方或 provider 都在分派前
    校验 schema。在此处做夹紧，使非法值不会渗入 sed 范围（如 ``0,-1p``）。

    ``limit`` 的上界来自 config.yaml 中的 ``tool_output.max_lines``
    （默认为模块级 ``MAX_LINES`` 常量）。
    """
    from tools.tool_output_limits import get_max_lines
    max_lines = get_max_lines()
    normalized_offset = max(1, _coerce_int(offset, DEFAULT_READ_OFFSET))
    normalized_limit = _coerce_int(limit, DEFAULT_READ_LIMIT)
    normalized_limit = max(1, min(normalized_limit, max_lines))
    return normalized_offset, normalized_limit


def normalize_search_pagination(offset: Any = DEFAULT_SEARCH_OFFSET,
                                limit: Any = DEFAULT_SEARCH_LIMIT) -> tuple[int, int]:
    """为 shell head/tail 管线返回安全的搜索分页边界。"""
    normalized_offset = max(0, _coerce_int(offset, DEFAULT_SEARCH_OFFSET))
    normalized_limit = max(1, _coerce_int(limit, DEFAULT_SEARCH_LIMIT))
    return normalized_offset, normalized_limit


_REGEX_NEWLINE_ESCAPE_RE = re.compile(r"(?<!\\)(?:\\\\)*\\n")


def _pattern_has_regex_newline(pattern: str) -> bool:
    """当内容搜索正则尝试匹配换行符时返回 True。

    ``search_files`` 以面向行的模式运行 rg/grep，而非 rg 的
    ``-U``/``--multiline`` 模式，因此换行正则无法跨行匹配。同时检测
    已解码进工具参数的字面换行符，以及正则 ``\n`` 转义（``n`` 之前有
    奇数个反斜杠）。偶数个反斜杠（如 ``\\n``）表示搜索字面的反斜杠+n，
    不应告警。
    """
    return "\n" in pattern or bool(_REGEX_NEWLINE_ESCAPE_RE.search(pattern))


def _is_line_oriented_newline_error(error: Optional[str]) -> bool:
    """当需要多行模式时 rg 抛出的硬错误，返回 True。"""
    if not error:
        return False
    return "literal \"\\n\" is not allowed" in error and "--multiline" in error


def _maybe_warn_line_oriented_newline_pattern(result: SearchResult, pattern: str) -> SearchResult:
    """仅当搜索未找到可用结果时附加换行正则告警。"""
    if result.total_count != 0 or not _pattern_has_regex_newline(pattern):
        return result
    if result.error and not _is_line_oriented_newline_error(result.error):
        return result
    result.error = None
    result.warning = (
        "0 results found. Note: search_files content search is line-oriented "
        "and does not run ripgrep with -U/--multiline, so `\\n` in the regex "
        "does not match line breaks. Use context=N to inspect neighboring "
        "lines, or escape as `\\\\n` when searching for a literal backslash+n."
    )
    return result


class ShellFileOperations(FileOperations):
    """
    基于 shell 命令实现的文件操作。

    可与任何具备 execute(command, cwd) 方法的终端后端协同工作。
    包括本地、docker、singularity、ssh、modal 和 daytona 环境。
    """

    def __init__(self, terminal_env, cwd: str = None):
        """
        用一个终端环境初始化文件操作。

        参数：
            terminal_env: 任何具备 execute(command, cwd) 方法的对象。
                         返回 {"output": str, "returncode": int}
            cwd: 可选的显式回退 cwd，用于终端环境没有 cwd 属性时
                （少见——大多数后端实时跟踪 cwd）。

        说明：
            每次 _exec() 调用都优先使用 LIVE ``terminal_env.cwd`` 而非
            ``self.cwd``，以便通过终端工具运行的 ``cd`` 命令能被立即感知。
            ``self.cwd`` 仅在环境完全没有 cwd 时用作回退——它并非权威
            cwd，尽管可在初始化时设置。

            历史 bug（已修复）：此类的早期版本对每次 _exec() 调用都使用
            初始化时的 cwd，导致用户在终端运行 ``cd`` 后，传给
            patch/read/write 的相对路径指向了错误的目录。补丁会声称成功并
            返回看似合理的 diff，却落在原目录里，造成明显的静默失败。
        """
        self.env = terminal_env
        # 从多种可能来源确定 cwd。
        # 重要：不要回退到 os.getcwd()——那是主机的本地路径，在
        # 容器/云后端（modal、docker）内部并不存在。
        # 若没有任何来源提供 cwd，使用 "/" 作为安全的通用默认值。
        self.cwd = cwd or getattr(terminal_env, 'cwd', None) or \
                   getattr(getattr(terminal_env, 'config', None), 'cwd', None) or "/"

        # 命令可用性检查的缓存
        self._command_cache: Dict[str, bool] = {}
    
    def _exec(self, command: str, cwd: str = None, timeout: int = None,
              stdin_data: str = None) -> ExecuteResult:
        """通过终端后端执行命令。

        参数：
            stdin_data: 若提供，则通过管道送入进程 stdin，而非嵌入命令字符串。
                       可绕过 ARG_MAX。

        cwd 解析顺序（关键——参见类 docstring）：
          1. 显式 ``cwd`` 参数（若提供）
          2. 实时 ``self.env.cwd``（跟踪通过终端运行的 ``cd`` 命令）
          3. 初始化时的 ``self.cwd``（环境无 cwd 属性时的回退）

        此顺序确保文件操作中的相对路径跟随终端的当前目录，而非此
        file_ops 最初创建时所在的目录。参见 test_file_ops_cwd_tracking.py。
        """
        kwargs = {}
        if timeout:
            kwargs['timeout'] = timeout
        if stdin_data is not None:
            kwargs['stdin_data'] = stdin_data

        # 从实时环境解析 cwd，使 `cd` 命令能被感知。
        # 仅当环境不跟踪 cwd 时才回退到初始化时的 self.cwd。
        effective_cwd = cwd or getattr(self.env, 'cwd', None) or self.cwd
        result = self.env.execute(command, cwd=effective_cwd, **kwargs)
        return ExecuteResult(
            stdout=result.get("output", ""),
            exit_code=result.get("returncode", 0)
        )
    
    def _has_command(self, cmd: str) -> bool:
        """检查环境中是否存在某命令（带缓存）。"""
        if cmd not in self._command_cache:
            result = self._exec(f"command -v {cmd} >/dev/null 2>&1 && echo 'yes'")
            self._command_cache[cmd] = result.stdout.strip() == 'yes'
        return self._command_cache[cmd]
    
    def _is_likely_binary(self, path: str, content_sample: str = None) -> bool:
        """
        检查文件是否可能是二进制。

        使用扩展名检查（快速）+ 内容分析（回退）。
        """
        ext = os.path.splitext(path)[1].lower()
        if ext in BINARY_EXTENSIONS:
            return True

        # 内容分析：不可打印字符占比 >30% 即视为二进制
        if content_sample:
            non_printable = sum(1 for c in content_sample[:1000]
                               if ord(c) < 32 and c not in '\n\r\t')
            return non_printable / min(len(content_sample), 1000) > 0.30
        
        return False
    
    def _is_image(self, path: str) -> bool:
        """检查文件是否为可作 base64 返回的图片。"""
        ext = os.path.splitext(path)[1].lower()
        return ext in IMAGE_EXTENSIONS
    
    def _add_line_numbers(self, content: str, start_line: int = 1) -> str:
        """以 ``LINE_NUM|CONTENT`` 格式为内容添加行号。

        行号槽采用紧凑的 ``<n>|`` 前缀（如 ``34|foo``），而非定宽零/空格
        填充形式（``    34|foo``）。填充纯粹是 token 开销：在密集源码上，
        填充槽比裸内容多耗约 48% 的 token，比紧凑形式多约 16%，因为前导
        空格 + 零填充在每一行都会被切成额外的 token。一项 A/B 测试
       （Sonnet 4.6，2 轮）表明，紧凑槽在行引用 / patch / 值查找 / 结构
        任务上与填充槽持平（双方均 4/4），而完全去掉行号会让行引用退化
        （模型手工计数出现差一错误，3/4）——因此我们保留行号，只是不保留
        填充。
        """
        from tools.tool_output_limits import get_max_line_length
        max_line_length = get_max_line_length()
        lines = content.split('\n')
        numbered = []
        for i, line in enumerate(lines, start=start_line):
            # 截断过长的行
            if len(line) > max_line_length:
                line = line[:max_line_length] + "... [truncated]"
            numbered.append(f"{i}|{line}")
        return '\n'.join(numbered)
    
    def _expand_path(self, path: str) -> str:
        """
        把 ~ 和 ~user 等 shell 风格路径展开为绝对路径。

        必须在 shell 转义之前完成，因为 ~ 在单引号内不会展开。
        """
        if not path:
            return path

        # 处理 ~ 和 ~user
        if path.startswith('~'):
            # 通过终端环境获取家目录
            result = self._exec("echo $HOME")
            if result.exit_code == 0 and result.stdout.strip():
                home = result.stdout.strip()
                if path == '~':
                    return home
                elif path.startswith('~/'):
                    return home + path[1:]  # 用家目录替换 ~
                # ~username 格式——在交给 shell 展开前先提取并校验用户名
                #（防止通过 "~; rm -rf /" 之类的路径进行 shell 注入）。
                rest = path[1:]  # 去掉开头的 ~
                slash_idx = rest.find('/')
                username = rest[:slash_idx] if slash_idx >= 0 else rest
                if username and re.fullmatch(r'[a-zA-Z0-9._-]+', username):
                    # 仅展开 ~username（不展开整个路径），以避免通过
                    # "~user/$(malicious)" 之类的路径后缀进行 shell 注入。
                    expand_result = self._exec(f"echo ~{username}")
                    if expand_result.exit_code == 0 and expand_result.stdout.strip():
                        user_home = expand_result.stdout.strip()
                        suffix = path[1 + len(username):]  # 例如 "/rest/of/path"
                        return user_home + suffix
        
        return path
    
    def _escape_shell_arg(self, arg: str) -> str:
        """转义字符串以安全用于 shell 命令。"""
        # 使用单引号，并转义字符串中的任何单引号
        return "'" + arg.replace("'", "'\"'\"'") + "'"

    def _atomic_write(self, path: str, content: str) -> "ExecuteResult":
        """通过临时文件 + rename 将 ``content`` 原子地写入 ``path``。

        通过 stdin 把 ``content`` 流式送入 ``path`` 同目录下的临时文件
        （使最终的 ``mv`` 是同一文件系统上的真实 rename，而非非原子的跨设备
        拷贝），保留既有文件（若存在）的 mode，然后 rename 覆盖目标。
        任何失败时临时文件都会被删除，从而绝不在用户数据旁泄漏残缺的
        ``.hermes-tmp`` 文件，且原文件保持不变。内容走 stdin，因此没有
        ARG_MAX 限制。

        返回 :class:`ExecuteResult`；``exit_code == 0`` 表示文件已被原子地
        替换到位。非零退出码表示没有发生 rename，原文件（若有）完好无损。
        """
        q_path = self._escape_shell_arg(path)
        parent = os.path.dirname(path) or "."
        q_parent = self._escape_shell_arg(parent)
        # 模板基本名：以隐藏开头，使它不出现在随手的 `ls` 中；带一个标记，
        # 使孤立的临时文件（仅在 cat 与 mv 之间硬崩溃时才可能出现）可被识别。
        tmpl = self._escape_shell_arg(".hermes-tmp.XXXXXX")

        # 单条 shell 脚本，全部加引号。说明：
        #  - `mktemp` 将临时文件落在目标自身目录（-p），使 `mv` 同 FS 原子；
        #    后端缺少 mktemp 时回退到带 PID 的名字（少见；busybox/macOS/Linux
        #    都自带）。
        #  - `chmod --reference` 仅 GNU 支持，因此用 `stat`（GNU `-c%a` 或
        #    BSD `-f%Lp`）读取八进制 mode 并显式 `chmod`；静默的尽力而为
        #    ——权限拷贝失败不得中断写入，文件仍按默认 umask 权限落地。
        #  - `trap ... EXIT` 保证临时文件在每条错误路径（cat 失败、mv 失败、
        #    信号）上都被删除，但在成功 mv 之后不删除（那时临时文件已不存在）。
        #  - 我们 `cat >` 到临时文件，再 `mv -f` 覆盖目标。
        script = (
            "set -e; "
            f"d={q_parent}; t={q_path}; "
            'tmp="$(mktemp -p "$d" ' + tmpl + ' 2>/dev/null '
            '|| mktemp "$d/.hermes-tmp.$$.XXXXXX" 2>/dev/null '
            '|| { tmp="$d/.hermes-tmp.$$"; : > "$tmp" && echo "$tmp"; })"; '
            '[ -n "$tmp" ] || { echo "atomic write: could not create temp file" >&2; exit 1; }; '
            "trap 'rm -f \"$tmp\"' EXIT; "
            # 保留既有目标的 mode（尽力而为，绝不致命）
            'if [ -e "$t" ]; then '
            'm="$(stat -c%a "$t" 2>/dev/null || stat -f%Lp "$t" 2>/dev/null || true)"; '
            '[ -n "$m" ] && chmod "$m" "$tmp" 2>/dev/null || true; '
            "fi; "
            'cat > "$tmp"; '
            'mv -f "$tmp" "$t"; '
            "trap - EXIT"
        )
        return self._exec(script, stdin_data=content)

    def _detect_file_line_ending(self, path: str, pre_content: Optional[str] = None) -> Optional[str]:
        """检测磁盘上文件的占主导换行符。

        若 ``pre_content`` 已可用（我们刚为 lint/LSP 读过该文件），
        则直接检查它——零额外 exec 调用。否则执行一次小小的
        ``head -c 4096`` 采样前 4KB。

        CRLF（Windows）返回 ``"\\r\\n"``，LF（Unix）返回 ``"\\n"``，
        无法判定时（新文件、空文件、首段无换行的单行文件）返回 ``None``。
        """
        if pre_content:
            return _detect_line_ending(pre_content)
        # 文件可能不存在（新写入）——此时 `head` 以退出码 0 返回空 stdout，
        # 下面会得到 None。开销很小的探测。
        head_cmd = f"head -c 4096 {self._escape_shell_arg(path)} 2>/dev/null"
        head_result = self._exec(head_cmd)
        if head_result.exit_code != 0 or not head_result.stdout:
            return None
        return _detect_line_ending(head_result.stdout)

    def _file_has_bom(self, path: str, pre_content: Optional[str] = None) -> bool:
        """磁盘上的文件是否以 UTF-8 BOM 开头。

        若已读过该文件则使用 ``pre_content``（零额外 exec 调用）；否则
        执行一次小小的 ``head -c 3`` 仅采样该标记。缺失/空文件返回 False
        （新写入不会有 BOM，除非调用方显式包含一个）。
        """
        if pre_content is not None:
            return _has_bom(pre_content)
        head_cmd = f"head -c 3 {self._escape_shell_arg(path)} 2>/dev/null"
        head_result = self._exec(head_cmd)
        if head_result.exit_code != 0 or not head_result.stdout:
            return False
        return _has_bom(head_result.stdout)


    def _unified_diff(self, old_content: str, new_content: str, filename: str) -> str:
        """生成新旧内容之间的 unified diff。"""
        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}"
        )
        return ''.join(diff)
    
    # =========================================================================
    # 读取（READ）实现
    # =========================================================================

    def read_file(self, path: str, offset: int = 1, limit: int = 500) -> ReadResult:
        """
        读取文件，支持分页、二进制检测和行号。

        参数：
            path: 文件路径（绝对路径或相对 cwd 的路径）
            offset: 起始行号（从 1 开始，默认 1）
            limit: 最多返回的行数（默认 500，最大 2000）

        返回：
            ReadResult，含内容、元数据或错误信息
        """
        # 展开 ~ 等 shell 路径
        path = self._expand_path(path)

        offset, limit = normalize_read_pagination(offset, limit)

        # 检查文件是否存在并获取大小（wc -c 是 POSIX 命令，Linux + macOS 通用）
        stat_cmd = f"wc -c < {self._escape_shell_arg(path)} 2>/dev/null"
        stat_result = self._exec(stat_cmd)
        
        if stat_result.exit_code != 0:
            # 文件未找到——尝试推荐相似文件
            return self._suggest_similar_files(path)
        
        stat_output = _strip_terminal_fence_leaks(stat_result.stdout)
        try:
            file_size = int(stat_output.strip())
        except ValueError:
            file_size = 0
        
        # 检查文件是否过大
        if file_size > MAX_FILE_SIZE:
            # 仍尝试读取，但给出告警
            pass

        # 图片永远不会内联——重定向到 vision 工具
        if self._is_image(path):
            return ReadResult(
                is_image=True,
                is_binary=True,
                file_size=file_size,
                hint=(
                    "Image file detected. Automatically redirected to vision_analyze tool. "
                    "Use vision_analyze with this file path to inspect the image contents."
                ),
            )
        
        # 读取一段样本以检查是否为二进制内容
        sample_cmd = f"head -c 1000 {self._escape_shell_arg(path)} 2>/dev/null"
        sample_result = self._exec(sample_cmd)
        sample_output = _strip_terminal_fence_leaks(sample_result.stdout)
        
        if self._is_likely_binary(path, sample_output):
            return ReadResult(
                is_binary=True,
                file_size=file_size,
                error="Binary file - cannot display as text. Use appropriate tools to handle this file type."
            )
        
        # 用 sed 分页读取
        end_line = offset + limit - 1
        read_cmd = f"sed -n '{offset},{end_line}p' {self._escape_shell_arg(path)}"
        read_result = self._exec(read_cmd)
        
        if read_result.exit_code != 0:
            return ReadResult(error=f"Failed to read file: {read_result.stdout}")
        read_output = _strip_terminal_fence_leaks(read_result.stdout)
        # 剥离开头的 UTF-8 BOM，使模型永远不会在第一个真实字符之前看到
        # 幽灵 U+FEFF。仅在第一段有意义（标记位于字节 0）；后续页不可能带它。
        if offset == 1:
            read_output, _ = _strip_bom(read_output)
        
        # 获取总行数
        wc_cmd = f"wc -l < {self._escape_shell_arg(path)}"
        wc_result = self._exec(wc_cmd)
        wc_output = _strip_terminal_fence_leaks(wc_result.stdout)
        try:
            total_lines = int(wc_output.strip())
        except ValueError:
            total_lines = 0
        
        # 检查是否被截断
        truncated = total_lines > end_line
        hint = None
        if truncated:
            hint = f"Use offset={end_line + 1} to continue reading (showing {offset}-{end_line} of {total_lines} lines)"
        
        return ReadResult(
            content=self._add_line_numbers(read_output, offset),
            total_lines=total_lines,
            file_size=file_size,
            truncated=truncated,
            hint=hint
        )
    
    def _suggest_similar_files(self, path: str) -> ReadResult:
        """当请求的文件未找到时，推荐相似文件。"""
        dir_path = os.path.dirname(path) or "."
        filename = os.path.basename(path)
        basename_no_ext = os.path.splitext(filename)[0]
        ext = os.path.splitext(filename)[1].lower()
        lower_name = filename.lower()

        # 列出目标目录下的文件
        ls_cmd = f"ls -1 {self._escape_shell_arg(dir_path)} 2>/dev/null | head -50"
        ls_result = self._exec(ls_cmd)

        scored: list = []  # (score, filepath) —— 越高越匹配
        if ls_result.exit_code == 0 and ls_result.stdout.strip():
            for f in ls_result.stdout.strip().split('\n'):
                if not f:
                    continue
                lf = f.lower()
                score = 0

                # 完全匹配（不应发生，但作为守卫）
                if lf == lower_name:
                    score = 100
                # 同基本名、不同扩展名（如 config.yml 与 config.yaml）
                elif os.path.splitext(f)[0].lower() == basename_no_ext.lower():
                    score = 90
                # 目标是候选的前缀，或反之
                elif lf.startswith(lower_name) or lower_name.startswith(lf):
                    score = 70
                # 子串匹配（候选包含查询）
                elif lower_name in lf:
                    score = 60
                # 反向子串（查询包含候选名）
                elif lf in lower_name and len(lf) > 2:
                    score = 40
                # 同扩展名且有一定重叠
                elif ext and os.path.splitext(f)[1].lower() == ext:
                    common = set(lower_name) & set(lf)
                    if len(common) >= max(len(lower_name), len(lf)) * 0.4:
                        score = 30

                if score > 0:
                    scored.append((score, os.path.join(dir_path, f)))

        scored.sort(key=lambda x: -x[0])
        similar = [fp for _, fp in scored[:5]]

        return ReadResult(
            error=f"File not found: {path}",
            similar_files=similar
        )
    
    def read_file_raw(self, path: str) -> ReadResult:
        """以纯字符串形式读取完整文件内容。

        无分页、无行号前缀、无逐行截断。
        使用 cat，因此无论文件大小都会返回完整内容。
        """
        path = self._expand_path(path)
        stat_cmd = f"wc -c < {self._escape_shell_arg(path)} 2>/dev/null"
        stat_result = self._exec(stat_cmd)
        if stat_result.exit_code != 0:
            return self._suggest_similar_files(path)
        stat_output = _strip_terminal_fence_leaks(stat_result.stdout)
        try:
            file_size = int(stat_output.strip())
        except ValueError:
            file_size = 0
        if self._is_image(path):
            return ReadResult(is_image=True, is_binary=True, file_size=file_size)
        sample_result = self._exec(f"head -c 1000 {self._escape_shell_arg(path)} 2>/dev/null")
        sample_output = _strip_terminal_fence_leaks(sample_result.stdout)
        if self._is_likely_binary(path, sample_output):
            return ReadResult(
                is_binary=True, file_size=file_size,
                error="Binary file — cannot display as text."
            )
        cat_result = self._exec(f"cat {self._escape_shell_arg(path)}")
        if cat_result.exit_code != 0:
            return ReadResult(error=f"Failed to read file: {cat_result.stdout}")
        # 剥离开头的 UTF-8 BOM，使 patch 的模糊匹配器在干净内容上工作
        #（第 1 行前的幽灵 U+FEFF 会使精确首行匹配落空）。write_file 在写回时
        # 恢复该 BOM——它会重新探测磁盘文件（文件仍带该标记），因此往返保留
        # 了该标记。
        raw_content, _ = _strip_bom(_strip_terminal_fence_leaks(cat_result.stdout))
        return ReadResult(
            content=raw_content,
            file_size=file_size,
        )

    def delete_file(self, path: str) -> WriteResult:
        """删除单个文件。

        跨平台：通过 ``python -c`` 在终端环境的 Python 上运行，因此在
        未自带 ``rm`` 的 Windows shell（``cmd.exe``/PowerShell）上也能工作。
        此处拒绝目录——删除目录树请用 ``delete_path(recursive=True)``。
        """
        return self._python_delete(path, recursive=False)

    def delete_path(self, path: str, recursive: bool = False) -> WriteResult:
        """跨平台删除，可处理文件，并在 recursive=True 时处理目录树。
        始终优先于直接发出 ``rm -rf`` / ``Remove-Item -Recurse``，使同一次
        工具调用在每个后端（本地 / docker / ssh / Windows）上都能工作。
        """
        return self._python_delete(path, recursive=recursive)

    def _python_delete(self, path: str, recursive: bool) -> WriteResult:
        path = self._expand_path(path)
        if _is_write_denied(path):
            return WriteResult(error=f"Delete denied: {path} is a protected path")

        # 这里不能 shell 调用 ``rm``——它在 Windows 的 ``cmd.exe`` 或
        # PowerShell 上不存在，因此当后端的终端是 Windows shell 时就走这条
        # 代码路径。通过 ``repr()`` 把路径烘焙进代码片段，使引用在每个 shell
        # 上都正确。
        snippet = (
            "import shutil, pathlib, sys\n"
            f"p = pathlib.Path({path!r})\n"
            f"recursive = {bool(recursive)!r}\n"
            "try:\n"
            "    if p.is_dir() and not p.is_symlink():\n"
            "        if recursive:\n"
            "            shutil.rmtree(p)\n"
            "        else:\n"
            "            print('is a directory: ' + str(p), file=sys.stderr); sys.exit(2)\n"
            "    else:\n"
            # 注意：避免使用 ``unlink(missing_ok=True)``——该 kwarg 出现在
            # Python 3.8，而远程解释器（docker/ssh）在较旧的发行版上可能仍是
            # 3.7。下面的 FileNotFoundError 处理器覆盖同一情形，且可回溯到 3.4。
            "        p.unlink()\n"
            "except FileNotFoundError:\n"
            "    pass\n"
            "except Exception as exc:\n"
            "    print(str(exc), file=sys.stderr); sys.exit(1)\n"
        )

        result = self._exec(f"python3 -c {self._escape_shell_arg(snippet)}")

        # 回退到 ``python``（Windows / 较旧系统上没有 ``python3`` 符号链接，
        # 但 PATH 上有 ``python`` 二进制）。
        if result.exit_code != 0 and "python3" in (result.stdout or ""):
            result = self._exec(f"python -c {self._escape_shell_arg(snippet)}")

        if result.exit_code != 0:
            return WriteResult(error=f"Failed to delete {path}: {(result.stdout or '').strip() or 'unknown error'}")

        return WriteResult()

    def move_file(self, src: str, dst: str) -> WriteResult:
        """通过 mv 移动文件。"""
        src = self._expand_path(src)
        dst = self._expand_path(dst)
        for p in (src, dst):
            if _is_write_denied(p):
                return WriteResult(error=f"Move denied: {p} is a protected path")
        result = self._exec(
            f"mv {self._escape_shell_arg(src)} {self._escape_shell_arg(dst)}"
        )
        if result.exit_code != 0:
            return WriteResult(error=f"Failed to move {src} -> {dst}: {result.stdout}")
        return WriteResult()

    # =========================================================================
    # 写入（WRITE）实现
    # =========================================================================

    def write_file(self, path: str, content: str) -> WriteResult:
        """
        向文件写入内容，按需创建父目录。

        通过 stdin 管道传输内容，以避开大文件的 OS ARG_MAX 限制。内容永远不会
        出现在 shell 命令字符串中——只有文件路径会出现。

        写入完成后，通过 ``_check_lint_delta()`` 运行"首写后/惰性前"的 lint
        检查。若新内容干净，该 lint 调用为 O(一次解析)。若新内容有错误，则
        也会对写前内容做 lint，并仅暴露本次写入新引入的错误——既有问题被过滤
        掉，使 agent 不会被分散注意力去追查它们。

        参数：
            path: 要写入的文件路径
            content: 要写入的内容

        返回：
            WriteResult，含已写字节数、lint 摘要或错误。
        """
        # 展开 ~ 等 shell 路径
        path = self._expand_path(path)

        # 拦截对敏感路径的写入
        if _is_write_denied(path):
            return WriteResult(error=f"Write denied: '{path}' is a protected system/credential file.")

        # 捕获写前内容。有两个消费者需要它：
        #
        #   1. lint-delta 层（用于 ast.parse、json.loads 等进程内 linter）
        #      需要先前内容来计算本次写入新引入的 lint 错误集合。
        #   2. LSP 层需要写前/写后内容来构建行偏移映射——当增删行时，编辑点
        #      以下的既有诊断会位移，偏移映射把基线诊断重映射到写后坐标，使
        #      严格的（范围感知）delta 键能匹配。
        #
        # 因此我们为之捕获 pre_content 的扩展名集合，是进程内 lint 覆盖与
        # LSP 覆盖的并集。对于两者都不覆盖的扩展名（二进制、不透明格式），
        # 跳过这次读取以保持热路径快速。
        ext = os.path.splitext(path)[1].lower()
        pre_content: Optional[str] = None
        want_pre = ext in LINTERS_INPROC or self._lsp_handles_extension(ext)
        if want_pre:
            # 尽力而为的读取；失败（文件缺失、权限）使 pre_content 保持
            # None，从而让两个下游消费者都优雅降级（lint 报告所有错误；
            # LSP 跳过偏移映射）。
            read_cmd = f"cat {self._escape_shell_arg(path)} 2>/dev/null"
            read_result = self._exec(read_cmd)
            if read_result.exit_code == 0 and read_result.stdout:
                pre_content = read_result.stdout

        # ── 换行符保留（Roo Code 模式）──────────────────────────────
        # 若文件以 CRLF 换行存在，而 agent 的内容是裸 LF，则在写入前转换为
        # CRLF。否则写入会静默归一化 Windows 换行文件（而当只有被替换区域
        # 变化时，patch 会产生混合换行）。用一小段 head 样本检测，避免仅为
        # 换行符目的读取整个文件。
        original_ending = self._detect_file_line_ending(path, pre_content)
        if original_ending == "\r\n":
            content = _normalize_line_endings(content, "\r\n")

        # ── BOM 保留 ─────────────────────────────────────────────────
        # 若磁盘上的文件以 UTF-8 BOM 开头，则保留它。read_file 会剥离 BOM，
        # 使 agent 永远看不到它，这意味着它交回给 write_file / patch 的内容
        # 也没有 BOM——若不在此处恢复，往返会静默剥离该标记并改变文件的字节
        # 签名（某些 Windows 工具链依赖它）。仅当原文件有 BOM 且新内容尚未
        # 携带 BOM 时才前置（防止调用方传入原始字节导致双重 BOM）。
        if self._file_has_bom(path, pre_content) and not _has_bom(content):
            content = _UTF8_BOM + content

        # 为此文件快照 LSP 诊断（尽力而为），使写后 LSP 层只返回本次编辑
        # 引入的诊断。镜像 claude-code 的 ``beforeFileEdited`` 模式，但接到
        # 本地 LSP 而非外部 IDE。
        self._snapshot_lsp_baseline(path)

        # 创建父目录
        parent = os.path.dirname(path)
        dirs_created = False

        if parent:
            mkdir_cmd = f"mkdir -p {self._escape_shell_arg(parent)}"
            mkdir_result = self._exec(mkdir_cmd)
            if mkdir_result.exit_code == 0:
                dirs_created = True

        # 原子写入：流式送入同目录下的临时文件，再 ``mv`` 覆盖目标。该 rename
        # 在 POSIX（以及我们运行的每个后端 FS）上是原子的，因此崩溃 / 断电 /
        # 写入中途管道截断时，原文件保持完好，而非写了一半的损坏文件。同目录
        # 至关重要——跨文件系统的 ``mv`` 会退化为拷贝+删除，这并非原子；把临时
        # 文件放在目标旁边才能保证真正的 rename。内容仍走 stdin，因此没有
        # ARG_MAX 限制。
        #
        # 当后端支持时，用 ``mktemp``（碰撞安全）创建临时文件，否则回退到带
        # PID 的名字。然后 chmod 临时文件以匹配既有文件的 mode（若有），使原子
        # 替换不会静默扩大或收窄权限；并在任何失败时清理临时文件，从而绝不在
        # 用户文件旁泄漏 ``.hermes-tmp`` 残留。
        write_result = self._atomic_write(path, content)

        if write_result.exit_code != 0:
            return WriteResult(error=f"Failed to write file: {write_result.stdout}")

        # 获取已写字节数（wc -c 是 POSIX 命令，Linux + macOS 通用）
        stat_cmd = f"wc -c < {self._escape_shell_arg(path)} 2>/dev/null"
        stat_result = self._exec(stat_cmd)

        try:
            bytes_written = int(stat_result.stdout.strip())
        except ValueError:
            bytes_written = len(content.encode('utf-8'))

        # 写后 lint，带 delta 精炼。
        lint_result = self._check_lint_delta(path, pre_content=pre_content, post_content=content)

        # 来自 LSP 层的语义诊断——独立通道。仅在语法层报告干净时触发
        #（对一个连解析都过不了的文件请求 LSP 没有意义）。传入写前/写后
        # 内容，使 LSP 层能构建行偏移映射，把基线诊断重映射到写后坐标。
        # 尽力而为：任何失败路径都返回 ``""``。
        lsp_diagnostics: Optional[str] = None
        if lint_result.success or lint_result.skipped:
            block = self._maybe_lsp_diagnostics(
                path, pre_content=pre_content, post_content=content
            )
            if block:
                lsp_diagnostics = block

        return WriteResult(
            bytes_written=bytes_written,
            dirs_created=dirs_created,
            lint=lint_result.to_dict() if lint_result else None,
            lsp_diagnostics=lsp_diagnostics,
        )
    
    # =========================================================================
    # PATCH 实现（替换模式）
    # =========================================================================

    def patch_replace(self, path: str, old_string: str, new_string: str,
                      replace_all: bool = False) -> PatchResult:
        """
        使用模糊匹配替换文件中的文本。

        参数：
            path: 要修改的文件路径
            old_string: 要查找的文本（除非 replace_all=True，否则必须唯一）
            new_string: 替换文本
            replace_all: 为 True 时替换所有匹配项

        返回：
            PatchResult，含 diff 和 lint 结果
        """
        # 展开 ~ 等 shell 路径
        path = self._expand_path(path)

        # 拦截对敏感路径的写入
        if _is_write_denied(path):
            return PatchResult(error=f"Write denied: '{path}' is a protected system/credential file.")

        # 读取当前内容
        read_cmd = f"cat {self._escape_shell_arg(path)} 2>/dev/null"
        read_result = self._exec(read_cmd)
        
        if read_result.exit_code != 0:
            return PatchResult(error=f"Failed to read file: {path}")
        
        content = read_result.stdout
        # 匹配前剥离开头的 UTF-8 BOM，使模糊匹配器和 diff 在干净内容上工作
        #（第 1 行前的幽灵 U+FEFF 会使精确首行匹配落空）。write_file 在写回时
        # 通过重新探测磁盘文件来恢复 BOM，因此往返保留该标记。
        content, _ = _strip_bom(content)

        # 导入并使用模糊匹配
        from tools.fuzzy_match import fuzzy_find_and_replace
        
        new_content, match_count, _strategy, error = fuzzy_find_and_replace(
            content, old_string, new_string, replace_all
        )
        
        if error or match_count == 0:
            err_msg = error or f"Could not find match for old_string in {path}"
            try:
                from tools.fuzzy_match import format_no_match_hint
                err_msg += format_no_match_hint(err_msg, match_count, old_string, content)
            except Exception:
                pass
            return PatchResult(error=err_msg)

        # ── 换行符保留 ───────────────────────────────────────────────
        # 模型在工具参数（JSON 编码）中几乎总是用裸 LF 发送 old_string/
        # new_string，但磁盘上的文件可能是 CRLF。fuzzy_find_and_replace
        # 之后，``new_content`` 是混合换行字符串：被替换区域是 LF，周围
        # 文本保留文件的 CRLF。把它整体归一化到文件检测出的换行符，使磁盘
        # 文件一致，且下方的 unified diff 反映真实变化。
        file_ending = _detect_line_ending(content)
        if file_ending:
            new_content = _normalize_line_endings(new_content, file_ending)

        # 写回
        write_result = self.write_file(path, new_content)
        if write_result.error:
            return PatchResult(error=f"Failed to write changes: {write_result.error}")

        # 写后校验——重新读取文件，确认我们打算写入的字节确实落盘。捕获静默
        # 的持久化失败（后端 FS 异常、与另一任务的竞争、管道截断等），否则会
        # 在文件磁盘未变时返回"带 diff 的成功"。
        verify_cmd = f"cat {self._escape_shell_arg(path)} 2>/dev/null"
        verify_result = self._exec(verify_cmd)
        if verify_result.exit_code != 0:
            return PatchResult(error=f"Post-write verification failed: could not re-read {path}")
        # 比较前归一化换行符。在 Windows 上，Python 默认的文本模式
        # ``open()`` 在写入时把 ``\n`` 转换为 ``\r\n``，因此磁盘文件合法地
        # 持有 CRLF，而我们的 ``new_content`` 字符串是裸 LF。若不做此归一化，
        # Windows 上的每次 patch 都会返回假的"写了 39、读到 42"假阴性，即使
        # 编辑已正确落地。POSIX 后端不做转换，因此那里是空操作。我们还剥离
        # 重读内容的开头 BOM：write_file 在磁盘上恢复了该标记，但
        # ``new_content`` 是我们用于匹配的无 BOM 字符串，因此比较必须去掉它
        # 才能同口径对比。
        _verify_bomless, _ = _strip_bom(verify_result.stdout)
        _verify_stdout_normalized = _verify_bomless.replace("\r\n", "\n").replace("\r", "\n")
        _new_content_normalized = new_content.replace("\r\n", "\n").replace("\r", "\n")
        if _verify_stdout_normalized != _new_content_normalized:
            return PatchResult(error=(
                f"Post-write verification failed for {path}: on-disk content "
                f"differs from intended write "
                f"(wrote {len(_new_content_normalized)} chars, read back "
                f"{len(_verify_stdout_normalized)} chars after normalizing line endings). "
                "The patch did not persist. Re-read the file and try again."
            ))

        # 生成 diff
        diff = self._unified_diff(content, new_content, path)

        # 带 delta 精炼的自动 lint：仅暴露本次 patch 引入的错误，过滤掉既有
        # lint 失败，使 agent 不会因原本就存在的问题分心。
        lint_result = self._check_lint_delta(path, pre_content=content, post_content=new_content)

        return PatchResult(
            success=True,
            diff=diff,
            files_modified=[path],
            lint=lint_result.to_dict() if lint_result else None,
            # 传播内部 ``write_file`` 调用已捕获的 LSP 诊断。其基线是
            # patch 前内容（在 write_file 开头经 ``_snapshot_lsp_baseline``
            # 采集），因此 delta 对整个 patch 是正确的。该字段与语法检查的
            # ``lint`` 分开，使 agent 能读取两路信号。
            lsp_diagnostics=write_result.lsp_diagnostics,
        )
    
    def patch_v4a(self, patch_content: str) -> PatchResult:
        """
        应用 V4A 格式的补丁。

        V4A 格式：
            *** Begin Patch
            *** Update File: path/to/file.py
            @@ 上下文提示 @@
             上下文行
            -删除的行
            +新增的行
            *** End Patch

        参数：
            patch_content: V4A 格式的补丁字符串

        返回：
            PatchResult，含所做的变更
        """
        # 导入 patch 解析器
        from tools.patch_parser import parse_v4a_patch, apply_v4a_operations
        
        operations, parse_error = parse_v4a_patch(patch_content)
        if parse_error:
            return PatchResult(error=f"Failed to parse patch: {parse_error}")
        
        # 应用操作
        result = apply_v4a_operations(operations, self)
        return result

    def _check_lint(self, path: str, content: Optional[str] = None) -> LintResult:
        """
        编辑后对文件运行语法检查。

        优先为结构化格式（JSON、YAML、TOML）使用进程内 linter——它们通过
        Python 标准库在微秒级内解析，且不需要子进程。对编译/类型检查类语言
        （py_compile、node --check、tsc、go vet、rustfmt）回退到 shell
        linter 表。

        参数：
            path: 文件路径（用于选择 linter + 用于 shell 调用）。
            content: 可选的文件内容。若提供且存在与扩展名匹配的进程内 linter，
                     则直接对该内容做 lint，不从磁盘重读文件。对 shell linter
                     无效。

        返回：
            LintResult，含状态和任何错误。
        """
        ext = os.path.splitext(path)[1].lower()

        # 可用时优先使用进程内 linter。
        inproc = LINTERS_INPROC.get(ext)
        if inproc is not None:
            # 需要内容——要么传入，要么从磁盘读取。
            if content is None:
                read_cmd = f"cat {self._escape_shell_arg(path)} 2>/dev/null"
                read_result = self._exec(read_cmd)
                if read_result.exit_code != 0:
                    return LintResult(skipped=True, message=f"Failed to read {path} for lint")
                content = read_result.stdout
            ok, err = inproc(content)
            if err == "__SKIP__":
                return LintResult(skipped=True, message=f"No linter available for {ext} (missing dependency)")
            return LintResult(success=ok, output="" if ok else err)

        # 回退到 shell linter。
        if ext not in LINTERS:
            return LintResult(skipped=True, message=f"No linter for {ext} files")

        # 若有真实 LSP 服务器处于活动状态且声明处理此文件，则对那些逐文件
        # shell 调用在结构上更弱/会刷出幻影错误的扩展名跳过 shell linter。
        # 各扩展名的理由参见上方的 ``_SHELL_LINTER_LSP_REDUNDANT``。
        # LSP 层经 ``_maybe_lsp_diagnostics`` 单独运行，并在 WriteResult /
        # PatchResult 的 ``lsp_diagnostics`` 中承载真实诊断。
        if ext in _SHELL_LINTER_LSP_REDUNDANT and self._lsp_will_handle(path):
            return LintResult(
                skipped=True,
                message=f"LSP server handles {ext} — shell linter skipped",
            )

        linter_cmd = LINTERS[ext]
        # 提取基础命令（第一个单词）
        base_cmd = linter_cmd.split()[0]

        if not self._has_command(base_cmd):
            return LintResult(skipped=True, message=f"{base_cmd} not available")

        # 运行 linter
        cmd = linter_cmd.replace("{file}", self._escape_shell_arg(path))
        result = self._exec(cmd, timeout=30)

        if result.exit_code != 0 and _looks_like_linter_unusable(base_cmd, result.stdout):
            # linter 命令存在于 PATH 上但实际无法运行（例如 tsc 不在
            # node_modules 时跑 ``npx tsc``；没有 Cargo 项目时跑
            # ``rustfmt --check``）。这是工具链缺口，而非真正的 lint 失败
            #——以 ``skipped`` 暴露它，使写入不被标记，且 LSP 层仍会运行。
            from tools.ansi_strip import strip_ansi
            cleaned = strip_ansi(result.stdout).strip()
            # 折叠为单行——npx 横幅是多行 ASCII。
            first_line = next(
                (ln.strip() for ln in cleaned.splitlines() if ln.strip()),
                cleaned[:120],
            )
            return LintResult(
                skipped=True,
                message=f"{base_cmd} not usable: {first_line[:200]}",
            )

        return LintResult(
            success=result.exit_code == 0,
            output=result.stdout.strip() if result.stdout.strip() else ""
        )

    def _check_lint_delta(self, path: str, pre_content: Optional[str],
                          post_content: Optional[str] = None) -> LintResult:
        """
        运行写后语法 lint，并与写前基线比较。

        两层策略：

        1. **语法检查**（进程内或基于 shell，微秒级）。捕获促成本层的 bug 类：
           损坏的写入、被压坏的引号、截断的输出。热路径。

        2. 当语法层报告错误时，对写前内容做 **delta 精炼**。过滤掉编辑前
           就已存在的错误，使 agent 不会被继承的状态分散注意力。

        来自 LSP 层的语义诊断经 :meth:`_maybe_lsp_diagnostics` 单独获取，
        并在 :class:`WriteResult` / :class:`PatchResult` 的
        ``lsp_diagnostics`` 字段中暴露。两路通道分开，使 agent（及任何下游
        解析器）能把语法错误与语义错误作为独立信号读取。

        参数：
            path: 文件路径（用于 linter 选择）。
            pre_content: 写入【之前】的文件内容。新文件或写前状态不可用时
                         传 None——跳过 delta 精炼并返回所有写后错误。
            post_content: 写入【之后】的文件内容。可选；若为 None，shell
                          linter 从磁盘读取（与 _check_lint 相同）。

        返回：
            LintResult。``output`` 含完整的写后 lint 错误（无写前状态），
            或仅含新错误行（已应用 delta 精炼）。
        """
        post = self._check_lint(path, content=post_content)

        # 热路径：写后在语法上干净。
        if post.success or post.skipped:
            return post

        # 写后有含语法错误。若有写前内容，则运行 delta 精炼以过滤既有错误。
        if pre_content is None:
            return post

        pre = self._check_lint(path, content=pre_content)
        if pre.success or pre.skipped or not pre.output:
            # 写前干净（或无法 lint）——写后错误都是新的。返回完整写后输出。
            return post

        # 写前和写后都有错误。在非空、去空白行上计算集合差。注意：单错误解析器
        #（ast.parse、json.loads）在首个错误处停止，不报告后续错误——若既有
        # 错误在到达编辑区域之前就阻断了解析，则无法证明本次编辑是干净的。因此
        # 若每个写后错误在编辑前就已出现，我们把文件报告为仍损坏，但注明本次
        # 编辑在其之上未引入任何新内容——agent 由此知道这是继承状态、而非新增
        # 破坏，同时不会静默丢弃该错误。
        pre_lines = {ln.strip() for ln in pre.output.splitlines() if ln.strip()}
        post_lines = [ln for ln in post.output.splitlines() if ln.strip() and ln.strip() not in pre_lines]

        if not post_lines:
            # 写后的每个错误在写前都存在——本次编辑没有明显让情况更糟，但文件
            # 仍然损坏，agent 应当知晓。
            return LintResult(
                success=False,
                output=post.output,
                message="Pre-existing lint errors — this edit didn't introduce new ones but the file is still broken.",
            )

        return LintResult(
            success=False,
            output=(
                "New lint errors introduced by this edit "
                "(pre-existing errors filtered out):\n" + "\n".join(post_lines)
            )
        )

    def _lsp_local_only(self) -> bool:
        """当且仅当此 FileOperations 接到本地后端时返回 True。

        LSP 服务器运行在主机进程上——它需要访问自己要 lint 的文件。远程/沙箱
        后端（Docker、Modal、SSH、Daytona）把文件保存在沙箱内，主机侧的 LSP
        服务器够不到，因此我们对这些后端完全跳过 LSP 路径。
        """
        env = getattr(self, "env", None)
        if env is None:
            # 防御性：某些测试通过 ``__new__`` 构造 ShellFileOperations
            # 而不经 ``__init__``，因此 ``self.env`` 可能缺失。无 env
            # = 无 LSP 路径。
            return False
        try:
            from tools.environments.local import LocalEnvironment
        except Exception:  # noqa: BLE001
            return False
        return isinstance(env, LocalEnvironment)

    def _lsp_handles_extension(self, ext: str) -> bool:
        """当且仅当某个已注册的 LSP 服务器声明处理此扩展名时返回 True。

        用于决定是否为行偏移映射捕获写前内容。捕获很廉价（主机上一次
        ``cat``），但若没有 LSP 会查看该文件则毫无意义。

        在远程后端上调用是安全的——注册表纯粹是进程内元数据；我们仍以
        :meth:`_lsp_local_only` 把真正的 LSP 路径放行。
        """
        if not ext:
            return False
        try:
            from agent.lsp.servers import SERVERS
        except Exception:  # noqa: BLE001
            return False
        ext_lower = ext.lower()
        for srv in SERVERS:
            if ext_lower in srv.extensions:
                return True
        return False

    def _lsp_will_handle(self, path: str) -> bool:
        """当且仅当 LSP 服务处于活动状态且会 lint 此文件时返回 True。

        比 :meth:`_lsp_handles_extension` 更强——后者只检查静态服务器注册表。
        本方法额外要求 LSP 服务已被配置/启用，且文件通过
        :meth:`agent.lsp.manager.LSPService.enabled_for`（它依据工作区检测、
        已禁用服务器集合、坏对短路来放行）。

        供 :meth:`_check_lint` 用来决定是否对
        ``_SHELL_LINTER_LSP_REDUNDANT`` 中的扩展名跳过逐文件 shell linter。

        尽力而为：任何失败路径都返回 False，使 shell linter 照常运行
        ——绝不基于一个实际上无法回答该问题的 LSP 探测来抑制 lint。
        """
        if not self._lsp_local_only():
            return False
        try:
            from agent.lsp import get_service
        except Exception:  # noqa: BLE001
            return False
        try:
            svc = get_service()
        except Exception:  # noqa: BLE001
            return False
        if svc is None:
            return False
        try:
            return bool(svc.enabled_for(path))
        except Exception:  # noqa: BLE001
            return False

    def _snapshot_lsp_baseline(self, path: str) -> None:
        """捕获编辑前的 LSP 诊断，使写后 delta 正确。

        尽力而为。每条失败路径都静默——LSP 是增强层，绝不能破坏一次写入。

        在非本地后端（Docker、Modal、SSH 等）上完全跳过——服务器看不到
        沙箱内的文件。
        """
        if not self._lsp_local_only():
            return
        try:
            from agent.lsp import get_service
            svc = get_service()
        except Exception:  # noqa: BLE001
            return
        if svc is None:
            return
        try:
            svc.snapshot_baseline(path)
        except Exception:  # noqa: BLE001
            pass

    def _maybe_lsp_diagnostics(
        self,
        path: str,
        *,
        pre_content: Optional[str] = None,
        post_content: Optional[str] = None,
    ) -> str:
        """为 ``path`` 尽力而为地获取 LSP 语义诊断。

        返回格式化的 ``<diagnostics>`` 块；当 LSP 不可用/被禁用/未产生错误时
        返回空字符串。

        当同时提供 ``pre_content`` 和 ``post_content`` 时，会构建行偏移映射
        并传给 LSPService，使基线诊断在做集合差之前被重映射到写后坐标。
        若不如此，删除或插入行的编辑会把编辑点以下的每个既有诊断都暴露为
        "本次编辑引入"。

        把一切包在 try/except 中，使行为异常的 LSP 服务器不能破坏写入。
        这里有意吞掉所有错误——调用层已返回干净的语法结果，因此此处返回
        ``""`` 仅表示"没有额外信息可加"。

        在非本地后端（Docker、Modal、SSH 等）上完全跳过——理由与
        ``_snapshot_lsp_baseline`` 相同。
        """
        if not self._lsp_local_only():
            return ""
        try:
            from agent.lsp import get_service
        except Exception:  # noqa: BLE001
            return ""
        try:
            svc = get_service()
        except Exception:  # noqa: BLE001
            return ""
        if svc is None or not svc.enabled_for(path):
            return ""

        # 当同时有写前和写后内容时构建行偏移映射——它把基线诊断重映射到
        # 写后坐标，使严格的（范围感知）delta 键能正确匹配。
        line_shift = None
        if pre_content is not None and post_content is not None and pre_content != post_content:
            try:
                from agent.lsp.range_shift import build_line_shift
                line_shift = build_line_shift(pre_content, post_content)
            except Exception:  # noqa: BLE001
                line_shift = None

        try:
            diagnostics = svc.get_diagnostics_sync(path, delta=True, line_shift=line_shift)
        except Exception:  # noqa: BLE001
            return ""
        if not diagnostics:
            return ""
        try:
            from agent.lsp.reporter import report_for_file, truncate
            block = report_for_file(path, diagnostics)
            if not block:
                return ""
            return truncate("LSP diagnostics introduced by this edit:\n" + block)
        except Exception:  # noqa: BLE001
            return ""
    
    # =========================================================================
    # 搜索（SEARCH）实现
    # =========================================================================

    def search(self, pattern: str, path: str = ".", target: str = "content",
               file_glob: Optional[str] = None, limit: int = 50, offset: int = 0,
               output_mode: str = "content", context: int = 0) -> SearchResult:
        """
        搜索内容或文件。

        参数：
            pattern: 正则（用于内容）或 glob 模式（用于文件）
            path: 要搜索的目录/文件（默认：cwd）
            target: "content"（grep）或 "files"（glob）
            file_glob: 内容搜索的文件模式过滤器（如 "*.py"）
            limit: 最多结果数（默认 50）
            offset: 跳过前 N 个结果
            output_mode: "content"、"files_only" 或 "count"
            context: 匹配项周围的上下文行数

        返回：
            SearchResult，含匹配项或文件列表
        """
        offset, limit = normalize_search_pagination(offset, limit)

        # 展开 ~ 等 shell 路径
        path = self._expand_path(path)

        # 搜索前校验路径是否存在
        check = self._exec(f"test -e {self._escape_shell_arg(path)} && echo exists || echo not_found")
        if "not_found" in check.stdout:
            # 尝试推荐附近的路径
            parent = os.path.dirname(path) or "."
            basename_query = os.path.basename(path)
            hint_parts = [f"Path not found: {path}"]
            # 检查父目录是否存在并列出相似条目
            parent_check = self._exec(
                f"test -d {self._escape_shell_arg(parent)} && echo yes || echo no"
            )
            if "yes" in parent_check.stdout and basename_query:
                ls_result = self._exec(
                    f"ls -1 {self._escape_shell_arg(parent)} 2>/dev/null | head -20"
                )
                if ls_result.exit_code == 0 and ls_result.stdout.strip():
                    lower_q = basename_query.lower()
                    candidates = []
                    for entry in ls_result.stdout.strip().split('\n'):
                        if not entry:
                            continue
                        le = entry.lower()
                        if lower_q in le or le in lower_q or le.startswith(lower_q[:3]):
                            candidates.append(os.path.join(parent, entry))
                    if candidates:
                        hint_parts.append(
                            "Similar paths: " + ", ".join(candidates[:5])
                        )
            return SearchResult(
                error=". ".join(hint_parts),
                total_count=0
            )
        
        if target == "files":
            return self._search_files(pattern, path, limit, offset)
        else:
            return self._search_content(pattern, path, file_glob, limit, offset, 
                                        output_mode, context)
    
    def _search_files(self, pattern: str, path: str, limit: int, offset: int) -> SearchResult:
        """按名称模式（类 glob）搜索文件。"""
        # 若未已存在 **/ 前缀，则自动前置以进行递归搜索
        if not pattern.startswith('**/') and '/' not in pattern:
            search_pattern = pattern
        else:
            search_pattern = pattern.split('/')[-1]

        search_root = Path(path)
        has_hidden_path_ancestor = any(
            part not in {".", ".."} and part.startswith(".")
            for part in search_root.parts
        )

        # 优先用 ripgrep：遵循 .gitignore、默认排除隐藏目录，且具备并行目录
        # 遍历（在宽目录树上比 find 快约 200 倍）。镜像已使用 rg 的
        # _search_content。
        if self._has_command('rg'):
            return self._search_files_rg(search_pattern, path, limit, offset)

        # 回退：find（更慢，不感知 .gitignore）
        if not self._has_command('find'):
            return SearchResult(
                error="File search requires 'rg' (ripgrep) or 'find'. "
                      "Install ripgrep for best results: "
                      "https://github.com/BurntSushi/ripgrep#installation"
            )

        # 排除隐藏目录（与 ripgrep 的默认行为一致）。
        hidden_exclude = "-not -path '*/.*'" if not has_hidden_path_ancestor else ""
        hidden_filter_expr = f" {hidden_exclude}" if hidden_exclude else ""

        # 对标准根使用 shell 分页。对隐藏根，收集完整输出，以便我们能重新应用
        # 隐藏后代过滤，同时允许显式的隐藏根搜索。
        pagination_expr = ""
        if not has_hidden_path_ancestor:
            pagination_expr = f" | tail -n +{offset + 1} | head -n {limit}"

        cmd = f"find {self._escape_shell_arg(path)}{hidden_filter_expr} -type f -name {self._escape_shell_arg(search_pattern)} " \
              f"-printf '%T@ %p\\n' 2>/dev/null | sort -rn{pagination_expr}"

        result = self._exec(cmd, timeout=60)
        stdout, limit_reason = _search_stdout_and_limit(result)

        if not stdout.strip() and not limit_reason:
            # 不带 -printf 重试（兼容 BSD find——macOS）
            cmd_simple = f"find {self._escape_shell_arg(path)}{hidden_filter_expr} -type f -name {self._escape_shell_arg(search_pattern)} " \
                        f"2>/dev/null | sort -rn{pagination_expr}"
            result = self._exec(cmd_simple, timeout=60)
            stdout, limit_reason = _search_stdout_and_limit(result)

        files = []
        for line in stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split(' ', 1)
            if len(parts) == 2 and parts[0].replace('.', '').isdigit():
                files.append(parts[1])
            else:
                files.append(line)

        # 对显式的隐藏根，find 基于路径的过滤会排除该隐藏路径下的每个文件。
        # 在命令执行后应用后代过滤，使仅显式根祖先被绕过。
        if has_hidden_path_ancestor:
            normalized_root = search_root.resolve()
            filtered_files = []
            for file_path in files:
                try:
                    rel_parts = Path(file_path).resolve().relative_to(normalized_root).parts
                except ValueError:
                    rel_parts = Path(file_path).parts
                if any(part not in {".", ".."} and part.startswith(".") for part in rel_parts):
                    continue
                filtered_files.append(file_path)
            files = filtered_files[offset:offset + limit]
        # 标准根的分页已在 shell 中应用

        return SearchResult(
            files=files,
            total_count=len(files),
            truncated=bool(limit_reason),
            limit_reason=limit_reason,
        )

    def _search_files_rg(self, pattern: str, path: str, limit: int, offset: int) -> SearchResult:
        """使用 ripgrep 的 --files 模式按名称搜索文件。

        rg --files 遵循 .gitignore 并默认排除隐藏目录，且使用并行目录遍历，
        在宽目录树上比 find 快约 200 倍。当 rg >= 13.0 支持 --sortr 时，
        结果按修改时间排序（最近编辑的在前）。
        """
        # rg --files -g 使用 glob 模式；把裸名字包起来，使其在任意深度匹配
        #（等价于 find -name）。
        if '/' not in pattern and not pattern.startswith('*'):
            glob_pattern = f"*{pattern}"
        else:
            glob_pattern = pattern

        fetch_limit = limit + offset
        # 先尝试按 mtime 排序（rg 13+）；不支持时回退到未排序。
        cmd_sorted = (
            f"rg --files --sortr=modified -g {self._escape_shell_arg(glob_pattern)} "
            f"{self._escape_shell_arg(path)} 2>/dev/null "
            f"| head -n {fetch_limit}"
        )
        result = self._exec(cmd_sorted, timeout=60)
        stdout, limit_reason = _search_stdout_and_limit(result)
        all_files = [f for f in stdout.strip().split('\n') if f]

        if not all_files and not limit_reason:
            # --sortr 在较旧的 rg 上可能失败；不带它重试。
            cmd_plain = (
                f"rg --files -g {self._escape_shell_arg(glob_pattern)} "
                f"{self._escape_shell_arg(path)} 2>/dev/null "
                f"| head -n {fetch_limit}"
            )
            result = self._exec(cmd_plain, timeout=60)
            stdout, limit_reason = _search_stdout_and_limit(result)
            all_files = [f for f in stdout.strip().split('\n') if f]

        page = all_files[offset:offset + limit]

        return SearchResult(
            files=page,
            total_count=len(all_files),
            truncated=len(all_files) >= fetch_limit or bool(limit_reason),
            limit_reason=limit_reason,
        )
    
    def _search_content(self, pattern: str, path: str, file_glob: Optional[str],
                        limit: int, offset: int, output_mode: str, context: int) -> SearchResult:
        """在文件内部搜索内容（类 grep）。"""
        # 先试 ripgrep（快），回退到 grep（慢但能用）
        if self._has_command('rg'):
            result = self._search_with_rg(pattern, path, file_glob, limit, offset,
                                          output_mode, context)
        elif self._has_command('grep'):
            result = self._search_with_grep(pattern, path, file_glob, limit, offset,
                                            output_mode, context)
        else:
            # rg 和 grep 都不可用（未装 Git Bash 的 Windows 等）
            return SearchResult(
                error="Content search requires ripgrep (rg) or grep. "
                      "Install ripgrep: https://github.com/BurntSushi/ripgrep#installation"
            )

        return _maybe_warn_line_oriented_newline_pattern(result, pattern)
    
    def _search_with_rg(self, pattern: str, path: str, file_glob: Optional[str],
                        limit: int, offset: int, output_mode: str, context: int) -> SearchResult:
        """使用 ripgrep 搜索。"""
        cmd_parts = ["rg", "--line-number", "--no-heading", "--with-filename"]

        # 按需添加上下文
        if context > 0:
            cmd_parts.extend(["-C", str(context)])

        # 添加文件 glob 过滤器（必须加引号以防 shell 展开）
        if file_glob:
            cmd_parts.extend(["--glob", self._escape_shell_arg(file_glob)])

        # 输出模式处理
        if output_mode == "files_only":
            cmd_parts.append("-l")  # 仅文件
        elif output_mode == "count":
            cmd_parts.append("-c")  # 每文件计数

        # 添加模式和路径
        cmd_parts.append(self._escape_shell_arg(pattern))
        cmd_parts.append(self._escape_shell_arg(path))

        # 多取一些行，以便在切片前报告真实总数。对于上下文模式，rg 在组间
        # 发射分隔行（"--"），因此我们慷慨地抓取并在 Python 中过滤。
        fetch_limit = limit + offset + 200 if context > 0 else limit + offset
        cmd_parts.extend(["|", "head", "-n", str(fetch_limit)])
        
        # `set -o pipefail` 使 rg 的退出状态能穿过 `| head` 传播。否则管线
        # 会报告 head 的状态（0），掩盖 rg 的错误码（2），使下方的守卫不可达。
        # rg 对截断的 head 处理干净（SIGPIPE 时退出 0），因此 pipefail 不会
        # 在成功但被截断的搜索上引入假错误。
        cmd = "set -o pipefail; " + " ".join(cmd_parts)
        result = self._exec(cmd, timeout=60)
        stdout, limit_reason = _search_stdout_and_limit(result)

        # _exec 把 stderr 合并进 stdout（stderr=subprocess.STDOUT），因此 rg 的
        # 诊断行（"rg: <file>: <error>"、"rg: regex parse error:"）会与匹配输出
        # 交错。把它们分离出来：诊断不能被当作匹配解析，而在硬错误时它们就是
        # 消息本身。
        diagnostics, payload = _split_tool_diagnostics(stdout)

        # rg 退出码：0=找到匹配，1=无匹配，2=错误。即使在部分错误时
        #（例如一个不可读的文件在一棵 otherwise 匹配的树中）rg 也返回 2，
        # 因此仅当 exit==2 且无可用匹配 payload 残留时才暴露错误。否则保留
        # 真实匹配。
        if result.exit_code == 2 and not payload.strip():
            error_msg = diagnostics.strip() or result.stdout.strip() or "Search error"
            return SearchResult(error=f"Search failed: {error_msg}", total_count=0)

        # 解析无诊断的 payload，使错误文本永远不会成为匹配。
        stdout = payload
        # 根据输出模式解析结果
        if output_mode == "files_only":
            all_files = [f for f in stdout.strip().split('\n') if f]
            total = len(all_files)
            page = all_files[offset:offset + limit]
            return SearchResult(
                files=page,
                total_count=total,
                truncated=bool(limit_reason),
                limit_reason=limit_reason,
            )
        
        elif output_mode == "count":
            counts = {}
            for line in stdout.strip().split('\n'):
                if ':' in line:
                    parts = line.rsplit(':', 1)
                    if len(parts) == 2:
                        try:
                            counts[parts[0]] = int(parts[1])
                        except ValueError:
                            pass
            return SearchResult(
                counts=counts,
                total_count=sum(counts.values()),
                truncated=bool(limit_reason),
                limit_reason=limit_reason,
            )
        
        else:
            # 解析内容匹配和上下文行。
            # rg 匹配行：  "file:lineno:content" （冒号分隔）
            # rg 上下文行："file-lineno-content" （短横线分隔）
            # rg 组分隔符："--"
            # 注意：Windows 上路径含盘符（如 C:\path），因此简单的 split(":")
            # 会出错。用正则同时处理两个平台。
            _match_re = re.compile(r'^([A-Za-z]:)?(.*?):(\d+):(.*)$')
            matches = []
            for line in stdout.strip().split('\n'):
                if not line or line == "--":
                    continue
                
                # 先试匹配行（冒号分隔：file:line:content）
                m = _match_re.match(line)
                if m:
                    matches.append(SearchMatch(
                        path=(m.group(1) or '') + m.group(2),
                        line_number=int(m.group(3)),
                        content=m.group(4)[:500]
                    ))
                    continue
                
                # 试上下文行（短横线分隔：file-line-content）
                # 仅在请求了上下文时尝试，以避免误报
                if context > 0:
                    parsed = _parse_search_context_line(line)
                    if parsed:
                        matches.append(SearchMatch(
                            path=parsed[0],
                            line_number=parsed[1],
                            content=parsed[2][:500]
                        ))

            total = len(matches)
            page = matches[offset:offset + limit]
            return SearchResult(
                matches=page,
                total_count=total,
                truncated=total > offset + limit or bool(limit_reason),
                limit_reason=limit_reason,
            )

    def _search_with_grep(self, pattern: str, path: str, file_glob: Optional[str],
                          limit: int, offset: int, output_mode: str, context: int) -> SearchResult:
        """回退：使用 grep 搜索。"""
        cmd_parts = ["grep", "-rnH"]  # -H 即使单文件搜索也强制输出文件名
        
        # 排除隐藏目录（与 ripgrep 的默认行为一致）。
        # 这防止搜索进入 .hub/index-cache/、.git/ 等目录内部。
        cmd_parts.append("--exclude-dir='.*'")

        # 按需添加上下文
        if context > 0:
            cmd_parts.extend(["-C", str(context)])

        # 添加文件模式过滤器（必须加引号以防 shell 展开）
        if file_glob:
            cmd_parts.extend(["--include", self._escape_shell_arg(file_glob)])

        # 输出模式处理
        if output_mode == "files_only":
            cmd_parts.append("-l")
        elif output_mode == "count":
            cmd_parts.append("-c")

        # 添加模式和路径
        cmd_parts.append(self._escape_shell_arg(pattern))
        cmd_parts.append(self._escape_shell_arg(path))

        # 慷慨抓取，以便在切片前计算总数
        fetch_limit = limit + offset + (200 if context > 0 else 0)
        cmd_parts.extend(["|", "head", "-n", str(fetch_limit)])
        
        # `set -o pipefail` 使 grep 的退出状态能穿过 `| head` 传播
        #（否则管线报告 head 的 0，掩盖 grep 的错误 2）。截断的 head 使 grep
        # 在 otherwise 成功的搜索上以 141（SIGPIPE）退出；下方严格的
        # `== 2` 守卫会忽略它，因此 pipefail 不会把截断结果变成假错误。
        cmd = "set -o pipefail; " + " ".join(cmd_parts)
        result = self._exec(cmd, timeout=60)
        stdout, limit_reason = _search_stdout_and_limit(result)

        # _exec 把 stderr 合并进 stdout，因此 grep 的诊断行
        #（"grep: <file>: <error>"）会与匹配交错。把它们分离出来，使其永远不会
        # 被当作匹配解析，并使硬错误拥有干净的消息。
        diagnostics, payload = _split_tool_diagnostics(stdout)

        # grep 退出码：0=找到匹配，1=无匹配，2=错误。即使在部分错误时
        #（例如一个不可读的文件），只要其他文件匹配了 grep 也返回 2，因此
        # 仅当 exit==2 且无可用匹配 payload 残留时才暴露错误。
        if result.exit_code == 2 and not payload.strip():
            error_msg = diagnostics.strip() or result.stdout.strip() or "Search error"
            return SearchResult(error=f"Search failed: {error_msg}", total_count=0)

        stdout = payload
        if output_mode == "files_only":
            all_files = [f for f in stdout.strip().split('\n') if f]
            total = len(all_files)
            page = all_files[offset:offset + limit]
            return SearchResult(
                files=page,
                total_count=total,
                truncated=bool(limit_reason),
                limit_reason=limit_reason,
            )
        
        elif output_mode == "count":
            counts = {}
            for line in stdout.strip().split('\n'):
                if ':' in line:
                    parts = line.rsplit(':', 1)
                    if len(parts) == 2:
                        try:
                            counts[parts[0]] = int(parts[1])
                        except ValueError:
                            pass
            return SearchResult(
                counts=counts,
                total_count=sum(counts.values()),
                truncated=bool(limit_reason),
                limit_reason=limit_reason,
            )
        
        else:
            # grep 匹配行：  "file:lineno:content"（冒号）
            # grep 上下文行："file-lineno-content"（短横线）
            # grep 组分隔符："--"
            # 注意：Windows 上路径含盘符（如 C:\path），因此简单的 split(":")
            # 会出错。用正则同时处理两个平台。
            _match_re = re.compile(r'^([A-Za-z]:)?(.*?):(\d+):(.*)$')
            matches = []
            for line in stdout.strip().split('\n'):
                if not line or line == "--":
                    continue
                
                m = _match_re.match(line)
                if m:
                    matches.append(SearchMatch(
                        path=(m.group(1) or '') + m.group(2),
                        line_number=int(m.group(3)),
                        content=m.group(4)[:500]
                    ))
                    continue
                
                if context > 0:
                    parsed = _parse_search_context_line(line)
                    if parsed:
                        matches.append(SearchMatch(
                            path=parsed[0],
                            line_number=parsed[1],
                            content=parsed[2][:500]
                        ))

            
            total = len(matches)
            page = matches[offset:offset + limit]
            return SearchResult(
                matches=page,
                total_count=total,
                truncated=total > offset + limit or bool(limit_reason),
                limit_reason=limit_reason,
            )
