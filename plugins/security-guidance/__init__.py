"""security-guidance 插件 — 基于模式匹配的快速安全警告，在文件写入时触发。

提供一个行为钩子：

* ``transform_tool_result`` 钩子 — 扫描 ``write_file`` / ``patch`` /
  ``skill_manage``（write/patch 模式）*正在写入的内容*，检测已知的危险代码模式
  （eval(、pickle.load、yaml.load、os.system、subprocess(shell=True)、
  dangerouslySetInnerHTML、verify=False、ECB、易受 XXE 攻击的 XML 解析器、
  GitHub Actions ``${{ github.event.* }}`` 注入、未设置 ``weights_only=True`` 的
  torch.load 等）。当任何模式匹配时，插件会在 JSON tool-result 字符串末尾
  追加一个 ``⚠️ Security warning`` 块。文件仍会被写入；模型在下一次 turn
  的 tool message 中看到警告并可以自行修正。

为什么不直接阻止？模式匹配存在不可忽视的误报率（tokenizer 中的 ``eval(``、
已经用 ``yaml.SafeLoader`` 包装的 ``yaml.load``、测试数据中的 ECB 等）。
直接阻止会将每个误报都变成审批提示或中断工作流。警告是第 1 层合适的
严重程度 — 模型读取警告后要么修正代码，要么简要说明该用法为何是安全的。

如需严格阻止模式（完全拒绝写入），设置
``SECURITY_GUIDANCE_BLOCK=1``。这会以严格性换取便利性，
适用于默认不安全模式属于策略违规的共享开发环境。

模式数据存放在 ``patterns.py`` 中，逐字 fork 自 Anthropic 的
``claude-plugins-official``，遵循 Apache-2.0 协议。详见此目录下的
``LICENSE`` 和 ``NOTICE``。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from . import patterns as _patterns

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

# 需要扫描其参数中"写入磁盘的代码内容"的工具名称。
# 映射：工具名 -> (路径参数名, 内容参数名)。对于有多个可能
# 内容字段的工具（patch 的 old/new_string 与原始 patch 文本），
# 扫描所有已填充的字符串字段。
_TARGET_TOOLS: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "write_file": ("path", ("content",)),
    "patch": ("path", ("new_string", "patch")),
    # skill_manage 的 write_file / patch 子操作。file_path 保存
    # skill 目录内的相对路径；我们以相同方式扫描。
    "skill_manage": ("file_path", ("file_content", "new_string")),
}

# 扫描内容的上限。超过此值则跳过 — 对 10 MB 的文件块做模式匹配
# 信噪比很低，且会拖慢 agent 循环。
_MAX_SCAN_BYTES = 256 * 1024


def _block_mode_enabled() -> bool:
    return os.environ.get("SECURITY_GUIDANCE_BLOCK", "").lower() in {"1", "true", "yes", "on"}


def _plugin_disabled() -> bool:
    return os.environ.get("SECURITY_GUIDANCE_DISABLE", "").lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------


# 预编译正则表达式（仅一次）。子串模式保留为普通字符串 —
# ``str.__contains__`` 比纯字面字符的正则更快。
_COMPILED: List[Dict[str, Any]] = []
for _rule in _patterns.SECURITY_PATTERNS:
    _entry: Dict[str, Any] = {
        "ruleName": _rule["ruleName"],
        "reminder": _rule["reminder"],
        "path_filter": _rule.get("path_filter"),
        "path_check": _rule.get("path_check"),
        "substrings": tuple(_rule.get("substrings", ())),
        "regex": None,
    }
    _re_src = _rule.get("regex")
    if _re_src:
        try:
            _entry["regex"] = re.compile(_re_src)
        except re.error as _err:
            logger.warning(
                "security-guidance: skipping rule %s — invalid regex %r: %s",
                _rule["ruleName"], _re_src, _err,
            )
            continue
    _COMPILED.append(_entry)


def _scan_content(path: str, content: str) -> List[Tuple[str, str]]:
    """返回 [(ruleName, reminder), ...]，包含所有匹配的模式。

    ``path`` 由每条规则的路径过滤器（path_filter / path_check）使用。
    每次调用中每条规则最多触发一次 — 同一规则的多次匹配会
    合并为单条警告记录。
    """
    if not content or len(content.encode("utf-8", errors="ignore")) > _MAX_SCAN_BYTES:
        return []
    hits: List[Tuple[str, str]] = []
    for entry in _COMPILED:
        # path_check：规则仅根据路径匹配触发（不涉及内容正则）。
        # 用于"你正在编辑敏感文件，此处提供提醒"类的全局警告 —
        # github_actions_workflow 是典型示例。
        path_check = entry.get("path_check")
        if path_check is not None:
            try:
                if path_check(path or ""):
                    hits.append((entry["ruleName"], entry["reminder"]))
            except Exception:
                pass
            # 路径检查规则不做内容模式匹配；继续下一条。
            continue
        # path_filter：当路径过滤器返回 False 时跳过该规则
        # （例如仅适用于 Python 的规则跳过 .js 文件；eval_injection 跳过 .md）
        path_filter = entry.get("path_filter")
        if path_filter is not None:
            try:
                if not path_filter(path or ""):
                    continue
            except Exception:
                continue
        matched = False
        for sub in entry["substrings"]:
            if sub in content:
                matched = True
                break
        if not matched and entry["regex"] is not None:
            if entry["regex"].search(content):
                matched = True
        if matched:
            hits.append((entry["ruleName"], entry["reminder"]))
    return hits


def _extract_path_and_content(tool_name: str, args: Any) -> List[Tuple[str, str]]:
    """返回 [(path, content), ...]，对应一次工具调用。无可扫描内容时返回空列表。"""
    spec = _TARGET_TOOLS.get(tool_name)
    if spec is None or not isinstance(args, dict):
        return []
    path_key, content_keys = spec
    path = args.get(path_key) or ""
    if not isinstance(path, str):
        path = ""
    out: List[Tuple[str, str]] = []
    for ck in content_keys:
        val = args.get(ck)
        if isinstance(val, str) and val:
            out.append((path, val))
    return out


def _format_warning_block(findings: List[Tuple[str, str]]) -> str:
    """将匹配结果渲染为 Markdown 块，追加到工具结果末尾。"""
    names = ", ".join(name for name, _ in findings)
    lines = [
        "",
        "---",
        f"⚠️ Security guidance — {len(findings)} pattern{'s' if len(findings) != 1 else ''} matched ({names})",
        "",
    ]
    for _, reminder in findings:
        lines.append(reminder)
        lines.append("")
    lines.append(
        "Pattern matches can be false positives. If the construct is safe in this "
        "context, briefly document why in a code comment and continue. Otherwise, "
        "fix the code before moving on."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 钩子
# ---------------------------------------------------------------------------


def _scan_args(tool_name: str, args: Any) -> List[Tuple[str, str]]:
    """pre_tool_call（阻止模式）和 transform_tool_result（警告模式）
    共用的扫描路径。"""
    if _plugin_disabled():
        return []
    findings: List[Tuple[str, str]] = []
    for path, content in _extract_path_and_content(tool_name, args):
        findings.extend(_scan_content(path, content))
    return findings


def _on_pre_tool_call(
    tool_name: str = "",
    args: Any = None,
    **_: Any,
) -> Optional[Dict[str, str]]:
    """阻止模式：若有任何模式匹配，拒绝写入。

    默认为非阻止模式 — 此处返回 None，由
    ``transform_tool_result`` 在结果中追加警告。
    """
    if not _block_mode_enabled():
        return None
    findings = _scan_args(tool_name, args)
    if not findings:
        return None
    return {
        "action": "block",
        "message": (
            "security-guidance refused this write: "
            + _format_warning_block(findings)
            + "\n\nTo override, unset SECURITY_GUIDANCE_BLOCK and retry."
        ),
    }


def _on_transform_tool_result(
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    **_: Any,
) -> Optional[str]:
    """警告模式钩子：在工具结果末尾追加安全警告块。

    返回字符串会替换模型在下一轮 turn 中看到的结果。
    返回 None 则保持结果不变。
    """
    # 阻止模式通过 pre_tool_call 处理匹配结果；此钩子在那种情况下
    # 无事可做（工具未运行，因此没有可包装的结果）。
    if _block_mode_enabled():
        return None
    findings = _scan_args(tool_name, args)
    if not findings:
        return None
    if not isinstance(result, str):
        return None
    # 不装饰错误结果 — 模型已经有更严重的问题需要处理。
    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict) and "error" in parsed and len(parsed) <= 2:
            return None
    except (ValueError, TypeError):
        pass
    return result + "\n\n" + _format_warning_block(findings)


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
