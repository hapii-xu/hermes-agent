"""disk-cleanup 插件 — 自动清理 Hermes 临时会话文件。

连接三种行为：

1. ``post_tool_call`` 钩子 — 检查 ``write_file`` 和 ``terminal``
   工具结果中，在 ``HERMES_HOME`` 下匹配测试/临时模式的新创建路径
   并静默追踪它们。无需 agent 配合。

2. ``on_session_end`` 钩子 — 当本次对话中有测试文件被自动追踪时，
   运行 :func:`disk_cleanup.quick` 并在 ``$HERMES_HOME/disk-cleanup/cleanup.log``
   中记录一行日志。

3. ``/disk-cleanup`` 斜杠命令 — 手动执行 ``status``、``dry-run``、
   ``quick``、``deep``、``track``、``forget``。

替代 PR #12212 的 skill+脚本设计：agent 不再需要记住运行命令。
"""

from __future__ import annotations

import logging
import re
import shlex
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Set

from . import disk_cleanup as dg

logger = logging.getLogger(__name__)


# 本次对话中"新追踪测试文件"的每任务集合。以 task_id
#（或 session_id 作为备选）为键，使 on_session_end 可以决定是否运行
# 清理。由锁保护 — post_tool_call 可在并行工具调用中并发触发。
_recent_test_tracks: Dict[str, Set[str]] = {}
_lock = threading.Lock()


# 我们可以解析的工具调用结果形状
_WRITE_FILE_PATH_KEY = "path"
_TERMINAL_PATH_REGEX = re.compile(r"(?:^|\s)(/[^\s'\"`]+|\~/[^\s'\"`]+)")


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _tracker_key(task_id: str, session_id: str) -> str:
    return task_id or session_id or "default"


def _record_track(task_id: str, session_id: str, path: Path, category: str) -> None:
    """记录本次对话中我们将 *path* 作为 *category* 追踪。"""
    if category != "test":
        return
    key = _tracker_key(task_id, session_id)
    with _lock:
        _recent_test_tracks.setdefault(key, set()).add(str(path))


def _drain(task_id: str, session_id: str) -> Set[str]:
    """弹出本次对话中追踪的测试路径集合。"""
    key = _tracker_key(task_id, session_id)
    with _lock:
        return _recent_test_tracks.pop(key, set())


def _attempt_track(path_str: str, task_id: str, session_id: str) -> None:
    """尽力而为的自动追踪。永不抛出异常。"""
    try:
        p = Path(path_str).expanduser()
    except Exception:
        return
    if not p.exists():
        return
    category = dg.guess_category(p)
    if category is None:
        return
    newly = dg.track(str(p), category, silent=True)
    if newly:
        _record_track(task_id, session_id, p, category)


def _extract_paths_from_write_file(args: Dict[str, Any]) -> Set[str]:
    path = args.get(_WRITE_FILE_PATH_KEY)
    return {path} if isinstance(path, str) and path else set()


def _extract_paths_from_patch(args: Dict[str, Any]) -> Set[str]:
    # patch 工具也通过 `mode="patch"` 路径创建新文件，但
    # 大多数用法是编辑现有文件 — 我们只关心新的临时创建，
    # 因此保守地处理 patch，只提取单文件 `path` 参数。
    # 追踪后清理是幂等的，因此重新追踪已追踪的文件是空操作（track() 中去重）。
    path = args.get("path")
    return {path} if isinstance(path, str) and path else set()


def _extract_paths_from_terminal(args: Dict[str, Any], result: str) -> Set[str]:
    """尽力而为：从终端命令及其输出中提取候选文件系统路径，
    然后让 ``guess_category`` / ``is_safe_path`` 过滤。
    """
    paths: Set[str] = set()
    cmd = args.get("command") or ""
    if isinstance(cmd, str) and cmd:
        # 对命令进行分词 — 捕获 `touch /tmp/hermes-x/test_foo.py`
        try:
            for tok in shlex.split(cmd, posix=True):
                if tok.startswith(("/", "~")):
                    paths.add(tok)
        except ValueError:
            pass
    # 只在结果文本大小合理时扫描（避免 50KB 的转储）。
    if isinstance(result, str) and len(result) < 4096:
        for match in _TERMINAL_PATH_REGEX.findall(result):
            paths.add(match)
    return paths


# ---------------------------------------------------------------------------
# 钩子
# ---------------------------------------------------------------------------

def _on_post_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **_: Any,
) -> None:
    """自动追踪最近工具调用创建的临时文件。"""
    if not isinstance(args, dict):
        return

    candidates: Set[str] = set()
    if tool_name == "write_file":
        candidates = _extract_paths_from_write_file(args)
    elif tool_name == "patch":
        candidates = _extract_paths_from_patch(args)
    elif tool_name == "terminal":
        candidates = _extract_paths_from_terminal(args, result if isinstance(result, str) else "")
    else:
        return

    for path_str in candidates:
        _attempt_track(path_str, task_id, session_id)


def _on_session_end(
    session_id: str = "",
    completed: bool = True,
    interrupted: bool = False,
    **_: Any,
) -> None:
    """如果本次对话中有测试文件被追踪，则运行快速清理。"""
    # 清空任务级和会话级两个桶。实际上每次对话只有一个被填充；
    # 另一个为空。
    drained_session = _drain("", session_id)
    # 也清空可能存在的任务范围桶。这是廉价的扫描：如果 agent 生成了
    # 子 agent（每个有自己的 task_id），它们会记录到独立的桶中；
    # 我们想在会话结束时清理所有这些。
    with _lock:
        task_buckets = list(_recent_test_tracks.keys())
    for key in task_buckets:
        if key and key != session_id:
            _recent_test_tracks.pop(key, None)

    if not drained_session and not task_buckets:
        return

    try:
        summary = dg.quick()
    except Exception as exc:
        logger.debug("disk-cleanup quick cleanup failed: %s", exc)
        return

    if summary["deleted"] or summary["empty_dirs"]:
        dg._log(
            f"AUTO_QUICK (session_end): deleted={summary['deleted']} "
            f"dirs={summary['empty_dirs']} freed={dg.fmt_size(summary['freed'])}"
        )


# ---------------------------------------------------------------------------
# Slash command
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
/disk-cleanup — ephemeral-file cleanup

Subcommands:
  status                     Per-category breakdown + top-10 largest
  dry-run                    Preview what quick/deep would delete
  quick                      Run safe cleanup now (no prompts)
  deep                       Run quick, then list items that need prompts
  track <path> <category>    Manually add a path to tracking
  forget <path>              Stop tracking a path (does not delete)

Categories: temp | test | research | download | chrome-profile | cron-output | other

All operations are scoped to HERMES_HOME and /tmp/hermes-*.
Test files are auto-tracked on write_file / terminal and auto-cleaned at session end.
"""


def _fmt_summary(summary: Dict[str, Any]) -> str:
    base = (
        f"[disk-cleanup] Cleaned {summary['deleted']} files + "
        f"{summary['empty_dirs']} empty dirs, freed {dg.fmt_size(summary['freed'])}."
    )
    if summary.get("errors"):
        base += f"\n  {len(summary['errors'])} error(s); see cleanup.log."
    return base


def _handle_slash(raw_args: str) -> Optional[str]:
    argv = raw_args.strip().split()
    if not argv or argv[0] in {"help", "-h", "--help"}:
        return _HELP_TEXT

    sub = argv[0]

    if sub == "status":
        return dg.format_status(dg.status())

    if sub == "dry-run":
        auto, prompt = dg.dry_run()
        auto_size = sum(i["size"] for i in auto)
        prompt_size = sum(i["size"] for i in prompt)
        lines = [
            "Dry-run preview (nothing deleted):",
            f"  Auto-delete : {len(auto)} files ({dg.fmt_size(auto_size)})",
        ]
        for item in auto:
            lines.append(f"    [{item['category']}] {item['path']}")
        lines.append(
            f"  Needs prompt: {len(prompt)} files ({dg.fmt_size(prompt_size)})"
        )
        for item in prompt:
            lines.append(f"    [{item['category']}] {item['path']}")
        lines.append(
            f"\n  Total potential: {dg.fmt_size(auto_size + prompt_size)}"
        )
        return "\n".join(lines)

    if sub == "quick":
        return _fmt_summary(dg.quick())

    if sub == "deep":
        # 会话内的 deep 无法以交互方式提示用户 — 显示 quick 清理的内容
        # 以及需要确认的项目。
        quick_summary = dg.quick()
        _auto, prompt_items = dg.dry_run()
        lines = [_fmt_summary(quick_summary)]
        if prompt_items:
            size = sum(i["size"] for i in prompt_items)
            lines.append(
                f"\n{len(prompt_items)} item(s) need confirmation "
                f"({dg.fmt_size(size)}):"
            )
            for item in prompt_items:
                lines.append(f"  [{item['category']}] {item['path']}")
            lines.append(
                "\nRun `/disk-cleanup forget <path>` to skip, or delete "
                "manually via terminal."
            )
        return "\n".join(lines)

    if sub == "track":
        if len(argv) < 3:
            return "Usage: /disk-cleanup track <path> <category>"
        path_arg = argv[1]
        category = argv[2]
        if category not in dg.ALLOWED_CATEGORIES:
            return (
                f"Unknown category '{category}'. "
                f"Allowed: {sorted(dg.ALLOWED_CATEGORIES)}"
            )
        if dg.track(path_arg, category, silent=True):
            return f"Tracked {path_arg} as '{category}'."
        return (
            f"Not tracked (already present, missing, or outside HERMES_HOME): "
            f"{path_arg}"
        )

    if sub == "forget":
        if len(argv) < 2:
            return "Usage: /disk-cleanup forget <path>"
        n = dg.forget(argv[1])
        return (
            f"Removed {n} tracking entr{'y' if n == 1 else 'ies'} for {argv[1]}."
            if n else f"Not found in tracking: {argv[1]}"
        )

    return f"Unknown subcommand: {sub}\n\n{_HELP_TEXT}"


# ---------------------------------------------------------------------------
# 插件注册
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_command(
        "disk-cleanup",
        handler=_handle_slash,
        description="Track and clean up ephemeral Hermes session files.",
    )
