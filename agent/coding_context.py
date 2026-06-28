"""编码上下文感知 —— 基础 Hermes，适用于所有交互界面。

当用户在代码工作区（CLI、TUI、桌面应用或通过 ACP 的编辑器）中运行 Hermes 时，
Hermes 会切换到**编码姿态**。本模块是决定我们是否处于该姿态及其含义的唯一入口，
使代码库的其余部分无需自行推断"我们是否在编码"。

架构 —— 一个接缝，多个消费者
----------------------------------------
姿态被建模为一个不可变的 :class:`RuntimeMode`，从一个小型的 :class:`ContextProfile`
注册表中选取（目前有：``coding`` 和 ``general``）。配置文件是*数据*——它声明要折叠到的
工具集、要注入的操作简报，以及其他领域的提示（模型路由、内存、子智能体）。
每个领域读取同一个已解析对象，而不是自行探测 git/config：

  * **系统提示** —— ``RuntimeMode.system_blocks()`` → 操作简报 +
    实时 git/工作区快照（``agent/system_prompt.py``）。
  * **工具集** —— ``RuntimeMode.toolset_selection()`` → ``coding`` 工具集
    加上用户启用的 MCP 服务器（``cli.py`` / ``tui_gateway``）。仅在
    可选的 ``focus`` 模式下：默认姿态仅影响提示词，从不触碰用户配置的工具集
    （消息/智能家居/音乐等工具集默认关闭，而显式启用了图片生成或 Spotify 的
    用户不应因处于 git 仓库中而失去这些功能）。
  * **委托** —— 子智能体继承父级的工具集并通过相同的提示构建器运行，
    因此编码姿态可免费传播给子级。
  * **模型/内存/压缩** —— 在配置文件中声明
    （``model_hint``、``memory_policy``）作为扩展接缝；消费者读取
    ``mode.profile`` 而非自行决策。

缓存安全性
------------
模式**一次性**解析，且不可变。工作区快照在提示构建时构建一次，并烘焙进
*稳定*系统提示层——不会按每轮重新探测（否则会破坏提示缓存）。分支和脏状态
在会话中会漂移，因此简报告知模型在依赖快照前先用 ``git`` 重新检查。
因此 ``/coding`` 翻转只在下次会话时生效（延迟），与 ``/skills install`` 相对
``--now`` 的合约相同。

激活方式（配置 ``agent.coding_context``）：

  * ``auto``（默认）—— 在处于代码工作区（git 仓库或已识别项目根）的交互式编码
    界面上启用姿态（简报 + 快照）。仅影响提示；工具集和技能索引不受影响。
  * ``focus`` —— 类似 ``auto``，但额外将工具集折叠为 ``coding`` 集 + 已启用的
    MCP 服务器，并在提示的技能索引中将非编码技能类别降级为仅显示名称（不隐藏
    任何技能）。需显式选择以获得精简模式。
  * ``on`` —— 在任何地方强制启用姿态（包括非工作区）。仅影响提示。
  * ``off`` —— 完全禁用。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hermes.coding_context")

CODING_TOOLSET = "coding"

# 在 ``auto`` 模式下，适合编码姿态的界面。消息平台（telegram、discord、slack 等）
# 故意排除——聊天群机器人不是结对编程。
INTERACTIVE_CODING_PLATFORMS = {"cli", "tui", "acp", "desktop", ""}

# 即使还不是 git 仓库，也能将目录标记为代码工作区的项目根信号。
# 廉价的文件名检查——无需解析。
_PROJECT_MARKERS = (
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "package.json", "tsconfig.json", "deno.json",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
    "Gemfile", "composer.json", "mix.exs", "pubspec.yaml",
    "CMakeLists.txt", "Makefile", "Dockerfile",
    "AGENTS.md", "CLAUDE.md", ".cursorrules",
)

# 在快照中与清单分开展示的智能体指令文件。
_CONTEXT_FILES = ("AGENTS.md", "CLAUDE.md", ".cursorrules")

# 锁文件 → 包管理器，按优先级顺序检查。
_PY_LOCKFILES = (("uv.lock", "uv"), ("poetry.lock", "poetry"), ("Pipfile.lock", "pipenv"))
_JS_LOCKFILES = (
    ("pnpm-lock.yaml", "pnpm"), ("bun.lockb", "bun"), ("bun.lock", "bun"),
    ("yarn.lock", "yarn"), ("package-lock.json", "npm"),
)

# 值得作为验证命令展示的 package.json 脚本 / Makefile 目标。
_VERIFY_TARGETS = ("test", "tests", "lint", "typecheck", "check", "build", "fmt", "format")
_MAX_VERIFY_COMMANDS = 8
_MAX_FACT_FILE_BYTES = 256 * 1024

_GIT_TIMEOUT = 2.5


# 按模型的编辑格式引导。将编辑工具格式与模型的训练方式匹配，可减少错误和浪费的推理
# （OpenAI/Codex 最擅长补丁样式的差异；Anthropic 模型——以及大多数开源编码模型，
# 其 RL 脚手架使用 str_replace 风格的编辑器——最擅长字符串替换）。我们的 `patch` 工具
# 同时支持两种模式：mode="patch"（V4A 多文件）和 mode="replace"（查找并替换）。
# 我们将每个系列引导至其原生格式。未知系列不做引导（简报的中性措辞保持原样）。
# 子串匹配模型 id；与 TOOL_USE_ENFORCEMENT_MODELS 对齐。
#
# GPT/Codex 对所有编辑都使用 V4A，包括单文件：在 codex-rs 中，
# apply_patch（V4A — apply_patch.lark）是唯一的文件编辑器，不存在
# str_replace 风格的工具，且出厂模型提示说"即使单文件编辑"也要使用
# apply_patch——因此 replace 模式的引导会将这些模型引导至其第一方工具
# 从未训练过的格式。
_EDIT_FORMAT_GUIDANCE: dict[str, tuple[tuple[str, ...], str]] = {
    "patch": (
        ("gpt", "codex"),
        "- Edit format: author new files with `write_file`; for edits to "
        "existing code use `patch` with `mode='patch'` (V4A diff) — including "
        "single-file edits. It's the edit format you handle most reliably.",
    ),
    "replace": (
        ("claude", "sonnet", "opus", "haiku",
         "gemini", "gemma", "deepseek", "qwen", "kimi", "glm", "grok",
         "hermes", "llama", "mistral", "devstral", "minimax"),
        "- Edit format: author new files with `write_file`; for edits to "
        "existing code prefer `patch` in `mode='replace'` — match a unique "
        "snippet and swap it. Reach for `mode='patch'` (V4A) only when an edit "
        "genuinely spans several files at once.",
    ),
}


def _model_family(model: Optional[str]) -> Optional[str]:
    """将模型 id 分类为编辑格式系列键，或返回 ``None``。

    用于将编码姿态引导至模型训练所用的 `patch` 模式。
    设计上对系列无感知：无法识别的模型返回 ``None``，操作简报的中性编辑措辞生效。
    """
    if not model:
        return None
    lowered = model.lower()
    for family, (needles, _line) in _EDIT_FORMAT_GUIDANCE.items():
        if any(n in lowered for n in needles):
            return family
    return None


def _edit_format_line(model: Optional[str]) -> str:
    """此模型系列的编辑格式引导行（无则返回 ``""``）。"""
    family = _model_family(model)
    if family is None:
        return ""
    return _EDIT_FORMAT_GUIDANCE[family][1]


# 编码姿态的操作简报。此处引用的工具名（read_file、search_files、patch、write_file、
# terminal、todo）在 coding 工具集和 _HERMES_CORE_TOOLS 中，因此在所有触发
# 此简报的界面上都存在。
CODING_AGENT_GUIDANCE = (
    "You are a coding agent pairing with the user inside their codebase. "
    "Operate like a careful senior engineer.\n"
    "\n"
    "Gather context first:\n"
    "- Read the relevant files with `read_file` and locate code with "
    "`search_files` before changing anything. Trace a symbol to its definition "
    "and usages rather than guessing its shape.\n"
    "- Batch independent lookups: when several reads/searches don't depend on "
    "each other, issue them together in one turn instead of one at a time.\n"
    "- Never invent files, symbols, APIs, or imports. If you haven't seen it in "
    "the repo, go look. Don't assume a library is available — check the project "
    "manifest (pyproject.toml / package.json / Cargo.toml / go.mod) and how "
    "neighbouring files import it.\n"
    "\n"
    "Make changes through the tools, not the chat:\n"
    "- Edit with `patch`/`write_file`. Do NOT print code blocks to the user as "
    "a substitute for editing — apply the change, then summarise it. Only show "
    "code when the user explicitly asks to see it.\n"
    "- Match the project's existing style and conventions; AGENTS.md / "
    "CLAUDE.md / .cursorrules already in context win over your defaults. Touch "
    "only what the task needs — no drive-by refactors, renames, or reformatting "
    "— and add any imports/dependencies your code requires.\n"
    "- If an edit fails to apply, re-read the file to get the current exact "
    "contents before retrying — don't repeat a stale patch. If the same region "
    "fails twice, rewrite the enclosing function or file with `write_file` "
    "instead of attempting a third patch.\n"
    "\n"
    "Verify, and know when to stop:\n"
    "- Use `terminal` for git, builds, tests, and inspection. Run the relevant "
    "tests/linter/build and confirm they pass before claiming the work is done.\n"
    "- Terminal state persists across calls: current directory and exported "
    "environment variables carry forward. Activate a virtualenv or export setup "
    "vars once, then reuse that state instead of re-sourcing it before every "
    "test command.\n"
    "- Fix root causes, not symptoms: when you find a bug, check sibling call "
    "paths for the same flaw and fix the class, not just the reported site.\n"
    "- When fixing linter/type errors on a file, stop after about three "
    "attempts on the same file and ask the user rather than looping.\n"
    "- Track multi-step work with `todo`. Reference code as `path:line` instead "
    "of pasting whole files.\n"
    "\n"
    "Respect the user's repo: don't commit, push, or rewrite history unless "
    "asked, and never read, print, or commit secrets — leave `.env` and "
    "credential files alone unless the user explicitly asks. The Workspace "
    "block below is a snapshot from session start — re-run `git status`/"
    "`git branch` before relying on it. Be concise: lead with the change or "
    "answer, not a preamble."
)


# ── 上下文配置文件（声明式姿态定义）──────────────────────────────────────────


@dataclass(frozen=True)
class ContextProfile:
    """命名的操作姿态。纯数据——消费者读取这些字段。

    ``toolset``      —— 当没有固定的显式选择时，折叠到此工具集（+ 启用的 MCP）；
                       ``None`` 保留平台默认值。
    ``guidance``     —— 注入稳定系统提示的操作简报；
                       ``""`` 不注入任何内容。
    ``model_hint``   —— 智能模型路由的路由偏好键
                       （扩展接缝；路由器尚未使用）。
    ``memory_policy``—— 内存命名空间/权重提示（扩展接缝）。
    ``compact_skill_categories`` —— 在可选的 ``focus`` 模式下，在系统提示的
                       技能索引中被降级为仅显示名称的技能类别。从不隐藏：
                       每个技能名称保持可见（使基于内存锚点的召回继续工作）
                       ——仅丢弃描述以减少索引噪声。拒绝列表语义，
                       因此未知/自定义类别保留完整条目。
    """

    name: str
    toolset: Optional[str] = None
    guidance: str = ""
    model_hint: Optional[str] = None
    memory_policy: str = "default"
    compact_skill_categories: tuple[str, ...] = ()


# 明显不属于编码工作流的技能类别。仅在可选的 ``focus`` 模式下在提示技能索引中
# 降级为仅显示名称（拒绝列表——此处未列出的任何内容，包括自定义用户类别，
# 保留完整条目）。与编码相关的类别（devops、github、mcp、
# 数据科学、图表、研究、安全等）故意不包含在内。
_NON_CODING_SKILL_CATEGORIES = (
    "apple", "communication", "cooking", "creative", "email", "finance",
    "gaming", "gifs", "health", "media", "music", "note-taking",
    "productivity", "shopping", "smart-home", "social-media", "travel",
    "yuanbao",
)


GENERAL_PROFILE = ContextProfile(name="general")
CODING_PROFILE = ContextProfile(
    name="coding",
    toolset=CODING_TOOLSET,
    guidance=CODING_AGENT_GUIDANCE,
    model_hint="coding",
    memory_policy="project",
    compact_skill_categories=_NON_CODING_SKILL_CATEGORIES,
)

_PROFILES: dict[str, ContextProfile] = {
    GENERAL_PROFILE.name: GENERAL_PROFILE,
    CODING_PROFILE.name: CODING_PROFILE,
}


def get_profile(name: str) -> ContextProfile:
    """返回注册的配置文件，回退到 ``general``。"""
    return _PROFILES.get(name, GENERAL_PROFILE)


# ── 辅助函数 ─────────────────────────────────────────────────────────────────


def _coding_mode(config: Optional[dict[str, Any]]) -> str:
    """返回规范化的 ``agent.coding_context`` 模式（auto/focus/on/off）。"""
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}
    raw = ((config or {}).get("agent", {}) or {}).get("coding_context", "auto")
    mode = str(raw).strip().lower()
    if mode in {"focus", "strict", "lean"}:
        return "focus"
    if mode in {"on", "true", "yes", "1", "always"}:
        return "on"
    if mode in {"off", "false", "no", "0", "never"}:
        return "off"
    return "auto"


def _resolve_cwd(cwd: Optional[str | Path]) -> Path:
    if cwd:
        return Path(cwd).expanduser()
    try:
        from agent.runtime_cwd import resolve_agent_cwd

        return resolve_agent_cwd()
    except Exception:
        return Path(os.getcwd())


def _git_root(cwd: Path) -> Optional[Path]:
    current = cwd.resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _home() -> Optional[Path]:
    try:
        return Path.home().resolve()
    except (OSError, RuntimeError):
        return None


def _marker_root(cwd: Path) -> Optional[Path]:
    """最近的看起来像项目根的祖先目录，或 ``None``。

    向上最多走几层，因此即使用户在子目录中，工作区根中的清单也会被计入。
    ``$HOME`` 本身被跳过——位于主目录中的 Makefile 或 AGENTS.md 是全局
    用户配置，而非项目根信号。
    """
    current = cwd.resolve()
    home = _home()
    for depth, parent in enumerate([current, *current.parents]):
        if depth > 6:
            break
        if parent == home:
            continue
        for marker in _PROJECT_MARKERS:
            if (parent / marker).exists():
                return parent
    return None


def _detect_profile_name(mode: str, platform: str, cwd_str: str) -> str:
    """解析适用的配置文件。

    ``auto``/``focus``：当界面是交互式且 cwd 是代码工作区（git 仓库或已识别的
    项目根）时使用编码配置。``on``：始终编码。``off``：始终通用。

    根植于 ``$HOME`` 的 git 仓库（dotfiles 模式）不是工作区信号——若无此守卫，
    在 dotfiles 管理的主目录下的每个会话都会静默切换到编码姿态。

    检测故意不进行记忆：只是少量 ``stat`` 调用，且无论如何调用者都会在每个会话中
    解析一次模式。在此缓存会有长生命周期进程（网关/TUI）为不同工作目录的会话
    提供服务时产生过时姿态的风险。
    """
    if mode == "off":
        return GENERAL_PROFILE.name
    if mode == "on":
        return CODING_PROFILE.name
    if platform and platform.strip().lower() not in INTERACTIVE_CODING_PLATFORMS:
        return GENERAL_PROFILE.name
    cwd = Path(cwd_str)
    git_root = _git_root(cwd)
    if git_root is not None and git_root == _home():
        git_root = None  # $HOME 处的 dotfiles 仓库——不是代码工作区
    if git_root is not None or _marker_root(cwd) is not None:
        return CODING_PROFILE.name
    return GENERAL_PROFILE.name


# ── RuntimeMode（接缝）──────────────────────────────────────────────────


@dataclass(frozen=True)
class RuntimeMode:
    """会话的已解析操作姿态。构造时不可变。

    通过 :func:`resolve_runtime_mode` 构建一次，由每个关心编码/通用区别的领域使用。
    在会话中不要修改或重新解析——那会破坏提示缓存。
    """

    profile: ContextProfile
    surface: str
    cwd: Path
    # 此姿态在其下解析的规范化 ``agent.coding_context`` 模式（auto/focus/on/off）。
    # 工具集折叠仅在 ``focus`` 下触发。
    config_mode: str = "auto"
    # 此会话运行的模型 id（例如 "anthropic/claude-opus-4.8"）。仅用于将编辑格式
    # 引导引导至模型系列——参见 ``_edit_format_line``。固定于会话，因此缓存安全。
    model: Optional[str] = None

    @property
    def kind(self) -> str:
        return self.profile.name

    @property
    def is_coding(self) -> bool:
        return self.profile.name == CODING_PROFILE.name

    def toolset_selection(self, config: Optional[dict[str, Any]] = None) -> Optional[list[str]]:
        """此姿态的工具集列表，或 ``None`` 表示保留平台默认值。

        仅在可选的 ``focus`` 模式下非 ``None``。默认姿态仅影响提示：
        大多数可剥离工具集默认关闭，而显式启用了某个工具集（图像生成用于前端/
        游戏资产，消息传递用于构建通知等）的用户在编码时仍会保留它。

        调用者仅在用户未固定显式选择时（``--toolsets``、``HERMES_TUI_TOOLSETS`` 等）
        才应用此选项；它们永远不会覆盖固定选择。返回配置文件的工具集加上已启用的
        MCP 服务器。
        """
        if self.config_mode != "focus":
            return None
        if self.profile.toolset is None:
            return None
        return [self.profile.toolset, *_enabled_mcp_servers(config)]

    def system_blocks(self) -> list[str]:
        """此姿态的稳定系统提示块（简报 + 工作区）。

        操作简报附带了针对模型系列的编辑格式微调（一个缓存字符串，而非单独的块），
        以便将模型引导至它最擅长处理的 `patch` 模式——参见 ``_edit_format_line``。
        """
        if not self.is_coding:
            return []
        blocks: list[str] = []
        if self.profile.guidance:
            brief = self.profile.guidance
            edit_line = _edit_format_line(self.model)
            if edit_line:
                brief = f"{brief}\n{edit_line}"
            blocks.append(brief)
        workspace = build_coding_workspace_block(self.cwd)
        if workspace:
            blocks.append(workspace)
        return blocks

    def compact_skill_categories(self) -> frozenset[str]:
        """活跃姿态在索引中降级为仅显示名称的技能类别。

        在编码姿态外及可选 ``focus`` 模式外为空——默认姿态从不触碰技能索引。
        未要求精简提示的用户为每个类别保留完整条目——实践中，即使仅显示名称，
        ``auto`` 下的索引变更也被证明太过令人惊讶（降级的描述是模型在决定
        加载什么时不再权衡的信息）。

        即使在 ``focus`` 下也是降级——从不隐藏。早期版本从索引中完全修剪这些
        类别，导致实际工作流中的静默能力损失：智能体创建的技能是模型积累的项目
        记忆（服务器运维手册、学到的陷阱等），而模型不会可靠地使用 ``skills_list``
        来重新发现索引停止显示的内容。仅显示名称使每个技能在召回时仍可加载，
        同时减少描述噪声。
        """
        if not self.is_coding or self.config_mode != "focus":
            return frozenset()
        return frozenset(self.profile.compact_skill_categories)


def resolve_runtime_mode(
    *,
    platform: Optional[str] = None,
    cwd: Optional[str | Path] = None,
    config: Optional[dict[str, Any]] = None,
    model: Optional[str] = None,
) -> RuntimeMode:
    """一次性解析操作姿态。廉价——少量 ``stat`` 调用。

    这是每个领域都应调用的单一入口点。返回的对象不可变，可在会话中安全缓存。
    检测本身故意*不*进行记忆（参见 ``_detect_profile_name``），以防长生命周期
    进程固定过时姿态；调用者在每个会话中解析一次并持有结果。``model`` 仅用于
    引导编辑格式，不影响检测。
    """
    resolved_cwd = _resolve_cwd(cwd)
    mode = _coding_mode(config)
    name = _detect_profile_name(
        mode, (platform or "").strip().lower(), str(resolved_cwd)
    )
    return RuntimeMode(
        profile=get_profile(name),
        surface=platform or "",
        cwd=resolved_cwd,
        config_mode=mode,
        model=model,
    )


# ── 向后兼容接口（RuntimeMode 的薄包装）────────────────────────────────────


def is_coding_context(
    *,
    platform: Optional[str] = None,
    cwd: Optional[str | Path] = None,
    config: Optional[dict[str, Any]] = None,
) -> bool:
    """Hermes 当前是否应该以编码姿态运行。"""
    return resolve_runtime_mode(platform=platform, cwd=cwd, config=config).is_coding


def coding_selection(
    *,
    platform: Optional[str] = None,
    cwd: Optional[str | Path] = None,
    config: Optional[dict[str, Any]] = None,
) -> Optional[list[str]]:
    """编码姿态的工具集选择。

    仅当用户选择了 ``focus`` 模式且姿态处于活动状态时非 ``None``——
    默认编码姿态从不覆盖已配置的工具集。
    """
    return resolve_runtime_mode(
        platform=platform, cwd=cwd, config=config
    ).toolset_selection(config)


def coding_system_blocks(
    *,
    platform: Optional[str] = None,
    cwd: Optional[str | Path] = None,
    config: Optional[dict[str, Any]] = None,
    model: Optional[str] = None,
) -> list[str]:
    """当前姿态的稳定系统提示块（通用姿态时为空）。

    ``model`` 将简报的编辑格式微调引导至模型系列。
    """
    return resolve_runtime_mode(
        platform=platform, cwd=cwd, config=config, model=model
    ).system_blocks()


def coding_compact_skill_categories(
    *,
    platform: Optional[str] = None,
    cwd: Optional[str | Path] = None,
    config: Optional[dict[str, Any]] = None,
) -> frozenset[str]:
    """活跃姿态在索引中降级为仅显示名称的技能类别。

    在编码姿态外及可选 ``focus`` 模式外为空——默认姿态从不触碰技能索引。
    在 ``focus`` 下，降级——从不隐藏：每个技能名称保留在索引中，可通过
    ``skill_view`` / ``skills_list`` 加载；仅丢弃描述。
    """
    return resolve_runtime_mode(
        platform=platform, cwd=cwd, config=config
    ).compact_skill_categories()


def _enabled_mcp_servers(config: Optional[dict[str, Any]]) -> list[str]:
    """用户已启用的 MCP 服务器名称——在编码姿态中保留。

    MCP 服务器（figma、browser、tophat 等）是显式配置的，是编码工作流的一部分，
    不是需要剥离的噪声。
    """
    try:
        from hermes_cli.config import read_raw_config
        from hermes_cli.tools_config import _parse_enabled_flag

        servers = read_raw_config().get("mcp_servers") or {}
        return [
            str(name)
            for name, cfg in servers.items()
            if isinstance(cfg, dict)
            and _parse_enabled_flag(cfg.get("enabled", True), default=True)
        ]
    except Exception:
        return []


# ── git/工作区探测 ─────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _parse_status(porcelain: str) -> tuple[dict[str, str], dict[str, int]]:
    """将 ``git status --porcelain=2 --branch`` 解析为分支信息 + 计数。"""
    branch: dict[str, str] = {}
    counts = {"staged": 0, "modified": 0, "untracked": 0, "conflicts": 0}
    for line in porcelain.splitlines():
        if line.startswith("# branch.head"):
            branch["head"] = line.split(maxsplit=2)[-1]
        elif line.startswith("# branch.upstream"):
            branch["upstream"] = line.split(maxsplit=2)[-1]
        elif line.startswith("# branch.ab"):
            parts = line.split()
            branch["ahead"], branch["behind"] = parts[2].lstrip("+"), parts[3].lstrip("-")
        elif line.startswith(("1 ", "2 ")):
            xy = line.split(maxsplit=2)[1]
            if xy[0] != ".":
                counts["staged"] += 1
            if xy[1] != ".":
                counts["modified"] += 1
        elif line.startswith("u "):
            counts["conflicts"] += 1
        elif line.startswith("? "):
            counts["untracked"] += 1
    return branch, counts


def _read_small(path: Path) -> str:
    """读取一个小文本文件，或返回 ``""``——从不抛出异常，从不读取大文件。"""
    try:
        if not path.is_file() or path.stat().st_size > _MAX_FACT_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


@dataclass(frozen=True)
class ProjectFacts:
    """结构化的项目事实——模型的验证循环，一次性检测。

    与工作区快照相同的数据，以结构化形式暴露，使非提示消费者
    （例如桌面验证 UI）可以读取，而不是重新检测并偏离提示。
    """

    manifests: list[str]
    package_managers: list[str]
    verify_commands: list[str]
    context_files: list[str]


def detect_project_facts(root: Path) -> ProjectFacts:
    """检测清单、包管理器、验证命令和上下文文件。

    廉价：stat 调用加上读取几个小文件。是提示快照（:func:`_project_facts`）
    和网关 ``project.facts`` 的唯一数据来源——因此 UI 从不重新嗅探验证命令。
    """
    manifests = [m for m in _PROJECT_MARKERS if m not in _CONTEXT_FILES and (root / m).is_file()]
    package_managers = list(
        dict.fromkeys(pm for lock, pm in (*_PY_LOCKFILES, *_JS_LOCKFILES) if (root / lock).is_file())
    )

    verify: list[str] = []
    if (root / "scripts" / "run_tests.sh").is_file():
        verify.append("scripts/run_tests.sh")
    if (root / "package.json").is_file():
        try:
            scripts = json.loads(_read_small(root / "package.json") or "{}").get("scripts") or {}
        except (json.JSONDecodeError, AttributeError):
            scripts = {}
        js_pm = next((pm for lock, pm in _JS_LOCKFILES if (root / lock).is_file()), "npm")
        verify.extend(f"{js_pm} run {name}" for name in _VERIFY_TARGETS if name in scripts)
    if (root / "pytest.ini").is_file() or "[tool.pytest" in _read_small(root / "pyproject.toml"):
        verify.append("pytest")
    makefile = _read_small(root / "Makefile")
    if makefile:
        verify.extend(
            f"make {name}" for name in _VERIFY_TARGETS
            if re.search(rf"^{re.escape(name)}\s*:", makefile, re.MULTILINE)
        )

    return ProjectFacts(
        manifests=manifests,
        package_managers=package_managers,
        verify_commands=list(dict.fromkeys(verify))[:_MAX_VERIFY_COMMANDS],
        context_files=[c for c in _CONTEXT_FILES if (root / c).is_file()],
    )


def _project_facts(root: Path) -> list[str]:
    """将 :func:`detect_project_facts` 渲染为工作区快照行。

    预先向模型提供其*验证循环*——使用哪个清单、哪个包管理器，以及准确的
    测试/lint/构建命令——而不是每个会话都让它重新发现。在提示构建时一次性构建；
    字符串输出必须保持字节稳定以保留提示缓存。
    """
    f = detect_project_facts(root)
    facts: list[str] = []

    if f.manifests:
        line = f"- Project: {', '.join(f.manifests[:6])}"
        if f.package_managers:
            line += f" ({'/'.join(f.package_managers)})"
        facts.append(line)
    if f.verify_commands:
        facts.append(f"- Verify: {'; '.join(f.verify_commands)}")
    if f.context_files:
        facts.append(f"- Context files: {', '.join(f.context_files)}")

    return facts


def project_facts_for(cwd: Optional[str | Path] = None) -> Optional[dict[str, Any]]:
    """``cwd`` 的结构化项目事实——在工作区外返回 ``None``。

    与系统提示快照使用相同的检测（git 根，否则标记根），暴露给非提示消费者
    （桌面验证 UI），使它们永远不需要重新推导"我们是否在编码"或重复验证命令嗅探。
    """
    resolved = _resolve_cwd(cwd)
    root = _git_root(resolved) or _marker_root(resolved)
    if root is None:
        return None

    f = detect_project_facts(root)
    return {
        "root": str(root),
        "manifests": f.manifests,
        "packageManagers": f.package_managers,
        "verifyCommands": f.verify_commands,
        "contextFiles": f.context_files,
    }


def build_coding_workspace_block(cwd: Optional[str | Path] = None) -> str:
    """系统提示的工作区快照（在工作区外为空）。

    当 cwd 在仓库中时包含 Git 状态（分支/状态/提交），加上检测到的项目事实
    （清单、包管理器、验证命令、上下文文件）——以便仅有标记（非 git）的项目
    也能获得快照。
    """
    resolved = _resolve_cwd(cwd)
    git_root = _git_root(resolved)
    root = git_root or _marker_root(resolved)
    if root is None:
        return ""

    lines = ["Workspace (snapshot at session start — re-check with `git` before acting on it):"]
    lines.append(f"- Root: {root}")

    if git_root is not None:
        branch, counts = _parse_status(_git(root, "status", "--porcelain=2", "--branch"))
        head = branch.get("head", "")
        if head and head != "(detached)":
            line = f"- Branch: {head}"
            if branch.get("upstream"):
                line += f" → {branch['upstream']}"
                ahead, behind = branch.get("ahead", "0"), branch.get("behind", "0")
                if ahead != "0" or behind != "0":
                    line += f" (ahead {ahead}, behind {behind})"
            lines.append(line)
        elif head == "(detached)":
            lines.append("- Branch: (detached HEAD)")

        # 链接的工作树：每个工作树的 git 目录与共享的公共目录不同。
        # 我们展示它是一个工作树的事实（以便模型知道分支/stash 是共享状态），
        # 但故意不暴露主树路径——给模型第二个绝对路径会导致它有时在错误的
        # 目录中运行命令。
        git_dir, common_dir = _git(root, "rev-parse", "--git-dir"), _git(root, "rev-parse", "--git-common-dir")
        if git_dir and common_dir and Path(git_dir).resolve() != Path(common_dir).resolve():
            lines.append("- Worktree: linked (git state shared with primary tree)")

        dirty = [f"{n} {label}" for label, n in (
            ("staged", counts["staged"]), ("modified", counts["modified"]),
            ("untracked", counts["untracked"]), ("conflicts", counts["conflicts"]),
        ) if n]
        lines.append(f"- Status: {', '.join(dirty) if dirty else 'clean'}")

        recent = _git(root, "log", "-3", "--pretty=%h %s")
        if recent:
            lines.append("- Recent commits:")
            lines.extend(f"    {c}" for c in recent.splitlines())

    lines.extend(_project_facts(root))
    return "\n".join(lines)
