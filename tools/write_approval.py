#!/usr/bin/env python3
"""写入审批闸门 + 针对记忆与技能写入的待审存储。

背景
----
agent 会写入两个跨会话持久化的存储：

  * **memory** —— MEMORY.md / USER.md，体量小（约 200 字符）的声明式条目
  * **skills** —— SKILL.md 及配套文件，可能非常大（10-100 KB）

这两个存储都有两种写入来源：

  * **foreground** —— 普通的 agent 回合（用户在场/正在对话）
  * **background_review** —— 在某个回合之后运行、自主决定保存什么的
    自我改进评审分叉（也就是用户抱怨的「错误假设」的来源）

本模块允许用户通过一个布尔值 ``write_approval`` 按子系统对这些写入加闸门：

  * ``false``（默认）—— 自由写入（闸门存在之前的行为）
  * ``true``            —— 要求审批：不提交写入；要么内联提示
    （仅限 memory、交互式 CLI），要么把它**暂存**到待审存储，并交给用户
    通过带外渠道审批或拒绝

memory 与 skills 之间的大小差异是真实且无法避免的：一条 memory 条目
可以在聊天气泡里就地评审；100 KB 的 SKILL.md 则不行。因此闸门会把两者
都暂存到磁盘，但评审方式因子系统而异（见 ``hermes_cli`` 的斜杠命令处理）：
memory 展示完整内容，skills 展示元数据 + 一行要点 + 一个 ``diff`` 逃生
通道（CLI/仪表盘/文件）。

对于来自后台的写入（守护线程不能阻塞在交互式提示上）以及 gateway 会话
（没有内联提示通道——评审通过 ``/memory pending`` 进行），暂存是强制性的。
前台 CLI 的 memory 写入通过危险命令审批回调内联提示；技能写入则总是
暂存（体量太大，难以在循环中即时审阅）。

待审记录存放在 ``<HERMES_HOME>/pending/{memory,skills}/<id>.json`` 下，
因此它们能挺过进程重启，并可从 CLI、gateway 或 Web 仪表盘进行评审。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# 子系统标识符
MEMORY = "memory"
SKILLS = "skills"
_SUBSYSTEMS = (MEMORY, SKILLS)

# 配置键（按子系统）。一个布尔值：审批闸门默认关闭（写入自由流动，即
# 闸门存在之前的行为），开启则意味着对每次写入进行暂存/提示以征求用户
# 审批。这里刻意没有第三种「屏蔽所有写入」的状态——要完全禁用某个子
# 系统，请使用它自己的启用开关（例如 ``memory.memory_enabled: false``）。
CONFIG_KEY = "write_approval"


# ---------------------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------------------

def write_approval_enabled(subsystem: str) -> bool:
    """返回 ``subsystem`` 的审批闸门是否开启。

    从 config.yaml 读取 ``<subsystem>.write_approval``。对于任何未设置/
    无效的值默认为 ``False``（闸门关闭——写入自由流动），以便现有安装
    在用户主动开启之前保持原有行为。
    """
    if subsystem not in _SUBSYSTEMS:
        return False
    try:
        from hermes_cli.config import load_config, cfg_get
        cfg = load_config()
        raw = cfg_get(cfg, subsystem, CONFIG_KEY, default=False)
    except Exception:
        return False
    return _normalize_enabled(raw)


def _normalize_enabled(value: Any) -> bool:
    """把配置值强制转换为布尔值。默认（未知值）为 False（闸门关闭）。

    接受真正的布尔值以及常见的真/假字符串。YAML 1.1 已经会把裸
    ``on``/``off``/``yes``/``no`` 解析为布尔值，因此字符串分支主要服务于
    手工编辑的配置。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"on", "true", "yes", "1", "approve", "enabled"}
    return False


# ---------------------------------------------------------------------------
# 待审存储（基于文件）
# ---------------------------------------------------------------------------

def _pending_dir(subsystem: str) -> Path:
    return get_hermes_home() / "pending" / subsystem


def stage_write(subsystem: str, payload: Dict[str, Any],
                *, summary: str, origin: str) -> Dict[str, Any]:
    """持久化一笔待审写入，并返回描述它的简短记录。

    参数：
        subsystem: ``memory`` 或 ``skills``。
        payload: 审批通过后重放该写入所需的精确 kwargs
            （例如 memory 的 ``{"action": "add", "target": "user",
            "content": "..."}``，或技能的完整 ``skill_manage`` kwargs）。
        summary: 一行人类可读的描述，展示在待审列表中。
            对技能而言这是 LLM/启发式生成的要点；对 memory 而言可以就是
            条目文本本身。
        origin: ``foreground`` 或 ``background_review``——记录以备审计。

    返回一个包含 ``id`` 和元数据的字典。尽力而为：磁盘失败时会记录日志，
    但仍返回一条记录（该写入就此丢失，这对审批闸门而言是安全的失败方式
    ——不会有任何东西被静默提交）。
    """
    pid = uuid.uuid4().hex[:8]
    record = {
        "id": pid,
        "subsystem": subsystem,
        "action": payload.get("action", ""),
        "summary": (summary or "").strip(),
        "origin": origin or "foreground",
        "created_at": time.time(),
        "payload": payload,
    }
    try:
        d = _pending_dir(subsystem)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{pid}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:  # pragma: no cover - 磁盘失败路径
        logger.error("Failed to stage pending %s write: %s", subsystem, e, exc_info=True)
    return record


def list_pending(subsystem: str) -> List[Dict[str, Any]]:
    """返回 ``subsystem`` 的所有待审记录，最旧的在前。"""
    d = _pending_dir(subsystem)
    if not d.exists():
        return []
    records: List[Dict[str, Any]] = []
    for p in d.glob("*.json"):
        try:
            records.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            logger.warning("Skipping unreadable pending record: %s", p)
    records.sort(key=lambda r: r.get("created_at", 0))
    return records


def get_pending(subsystem: str, pending_id: str) -> Optional[Dict[str, Any]]:
    """按 id 返回单条待审记录，没有则返回 None。"""
    path = _pending_dir(subsystem) / f"{pending_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def discard_pending(subsystem: str, pending_id: str) -> bool:
    """删除一条待审记录。如果它原本存在则返回 True。"""
    path = _pending_dir(subsystem) / f"{pending_id}.json"
    try:
        if path.exists():
            path.unlink()
            return True
    except Exception as e:  # pragma: no cover
        logger.error("Failed to discard pending %s/%s: %s", subsystem, pending_id, e)
    return False


def pending_count(subsystem: str) -> int:
    """待审记录的廉价计数（用于通知徽标）。"""
    d = _pending_dir(subsystem)
    if not d.exists():
        return 0
    try:
        return sum(1 for _ in d.glob("*.json"))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# 写入来源
# ---------------------------------------------------------------------------

def current_origin() -> str:
    """返回当前写入来源：``foreground`` 或 ``background_review``。

    复用技能溯源用的 ContextVar，后台评审分叉已经设置了它（见
    ``agent.background_review`` / ``AIAgent._spawn_background_review``）。
    前台 agent 回合会让它保持默认值 ``foreground``。
    """
    try:
        from tools.skill_provenance import get_current_write_origin
        return get_current_write_origin()
    except Exception:
        return "foreground"


def is_background() -> bool:
    return current_origin() == "background_review"


# ---------------------------------------------------------------------------
# 闸门决策
# ---------------------------------------------------------------------------

class GateDecision:
    """对单次写入尝试评估闸门后的结果。

    三个布尔标志中恰好有一个为 True：
      * ``allow``  —— 继续执行真正的写入（闸门关闭，或已获内联审批）。
      * ``blocked`` —— 拒绝写入（用户拒绝了一次内联审批提示）。
        ``message`` 解释原因；把它呈现给 agent。
      * ``stage``  —— 不写入；调用方应通过 ``stage_write`` 暂存负载
        （闸门开启，且没有可用的内联提示——gateway、后台评审、脚本，
        或任何技能写入）。``message`` 是面向用户的「已暂存待审批」提示。
    """

    __slots__ = ("allow", "blocked", "stage", "message")

    def __init__(self, *, allow=False, blocked=False, stage=False, message=""):
        self.allow = allow
        self.blocked = blocked
        self.stage = stage
        self.message = message


def evaluate_gate(subsystem: str, *, inline_summary: str = "",
                  inline_detail: str = "") -> GateDecision:
    """决定如何处理 ``subsystem`` 的一笔待审写入。

    参数：
        subsystem: ``memory`` 或 ``skills``。
        inline_summary: 用作内联审批提示标题的简短描述
            （仅 memory 前台路径）。
        inline_detail: 内联提示中展示的完整内容（memory 条目很小；
            技能绝不走内联路径）。

    决策矩阵：
        闸门关闭（默认）                       → allow（写入自由流动）
        闸门开启，memory + 交互式 CLI          → 内联审批/拒绝提示
        闸门开启，memory + gateway/脚本/后台   → stage
        闸门开启，skills（任何来源）           → stage（体量太大，无法内联评审）

    说明：不存在由配置驱动的 "blocked" 结果——闸门只会延迟写入以等待审批，
    绝不会静默拒绝。``blocked`` 仍然会在用户*主动拒绝*内联提示时产生。
    """
    if not write_approval_enabled(subsystem):
        return GateDecision(allow=True)

    background = is_background()

    # 技能总是暂存——SKILL.md 体量太大，无法内联评审；而且后台技能写入
    # 发生在守护线程中，没有用户在场。
    if subsystem == SKILLS or background:
        where = "/skills pending" if subsystem == SKILLS else "/memory pending"
        return GateDecision(
            stage=True,
            message=(
                f"Staged for approval ({subsystem}.write_approval is on). "
                f"Not yet saved — review with {where}."
            ),
        )

    # memory + 前台：如果存在一个交互式审批通道（在本线程上注册的 CLI
    # 审批回调），则内联提示——条目足够小，可以完整展示。否则
    # （gateway、脚本、批处理、无监听器）改为暂存，而不是强制盲目拒绝。
    if _interactive_approval_available():
        granted = _prompt_inline_memory_approval(inline_summary, inline_detail)
        if granted is True:
            return GateDecision(allow=True)
        if granted is False:
            return GateDecision(
                blocked=True,
                message="Memory write denied by user. The change was not saved.",
            )
        # granted 为 None → 提示失败；回退到暂存。

    return GateDecision(
        stage=True,
        message=(
            "Staged for approval (memory.write_approval is on). "
            "Not yet saved — review with /memory pending."
        ),
    )


def _interactive_approval_available() -> bool:
    """当前台 memory 写入可以内联审批时返回 True。

    内联提示需要一个由交互式 CLI 注册的、按线程生效的审批回调
    （``tools.terminal_tool.set_approval_callback``）。所有其他界面都会
    改为暂存：

    * **Gateway/API 会话** —— 危险命令的 ``/approve`` 往返位于待审审批
      队列中（``submit_pending`` + ``_await_gateway_decision``），
      ``prompt_dangerous_approval`` 永远不会触及它；试图从 gateway 会话
      发起提示会命中 ``input()`` 回退并被静默拒绝。暂存能给用户一个真正
      的评审入口（``/memory pending``）。
    * 脚本、cron 和后台线程——没有用户在场。
    """
    try:
        from tools.terminal_tool import _get_approval_callback
        return _get_approval_callback() is not None
    except Exception:
        return False


def _prompt_inline_memory_approval(summary: str, detail: str) -> Optional[bool]:
    """向用户内联提示是否审批一笔 memory 写入。

    返回 True（批准）、False（拒绝）或 None（没有可用的交互式提示/
    提示失败 → 调用方应改为暂存）。

    复用为危险命令注册的、按线程生效的 CLI 审批回调
    （``tools.terminal_tool.set_approval_callback``）。该回调被直接调用——
    而非经由 ``prompt_dangerous_approval``——因为那个包装会回退到
    ``input()``（在 prompt_toolkit 下容易死锁，见 #15216），并把回调错误
    转换为静默拒绝；而此处一个失败的提示必须改为暂存该写入。
    """
    try:
        from tools.terminal_tool import _get_approval_callback
    except Exception:
        return None

    callback = _get_approval_callback()
    if callback is None:
        # 本线程上没有交互式通道——改为暂存，而不是冒险走 input() 回退
        # （在 prompt_toolkit 下会死锁，在测试中会因 EOF 被拒绝）。
        return None

    header = summary.strip() or "Save to memory?"
    body = detail.strip()
    description = f"Save to memory: {header}"
    command = body if body else header
    # 直接调用回调，而不是经由 prompt_dangerous_approval：那个包装会把
    # 回调异常吞成 "deny"，从而静默拒绝写入。直接调用让崩溃的提示回退
    # 到暂存（闸门只会延迟写入，绝不会丢弃它）。
    try:
        choice = callback(command, description, allow_permanent=False)
    except Exception as e:
        logger.error("Inline memory approval prompt failed: %s", e)
        return None

    if choice in {"once", "session"}:
        return True
    if choice == "deny":
        return False
    # 任何其他结果（例如超时已经返回 "deny" 的情况已处理）→
    # 把未知结果视为无决策，从而改为暂存，而不是静默丢弃。
    return None


# ---------------------------------------------------------------------------
# 技能专用辅助函数（评审入口用的要点 + diff）
# ---------------------------------------------------------------------------

def skill_gist(action: str, name: str, *, content: str = "",
               file_path: str = "", old_string: str = "",
               new_string: str = "") -> str:
    """为一笔待审技能写入构造一行人类可读的要点。

    基于启发式，不调用模型——这个要点提供的信息足以在聊天气泡中决定
    批准/拒绝，而完整 diff 则留在 /skills diff（CLI/仪表盘/文件）背后。
    对于 create/edit，它会提取 frontmatter 的 ``description:``；对于
    patch/write_file，它描述变更的规模。
    """
    if action in {"create", "edit"} and content:
        desc = _frontmatter_description(content)
        size = f"{len(content) // 1024 + 1} KB" if len(content) >= 1024 else f"{len(content)} chars"
        verb = "create" if action == "create" else "rewrite"
        if desc:
            return f"{verb} '{name}' — {desc} ({size})"
        return f"{verb} '{name}' ({size})"
    if action == "patch":
        target = file_path or "SKILL.md"
        removed = old_string.count("\n") + 1 if old_string else 0
        added = new_string.count("\n") + 1 if new_string else 0
        return f"patch '{name}' {target} (+{added}/-{removed} lines)"
    if action == "write_file":
        return f"write {file_path} in '{name}'"
    if action == "remove_file":
        return f"remove {file_path} from '{name}'"
    if action == "delete":
        return f"delete skill '{name}'"
    return f"{action} '{name}'"


def _frontmatter_description(content: str) -> str:
    """从 SKILL.md 的 YAML frontmatter 中提取 ``description:`` 的值。"""
    import re
    m = re.search(r"^description:\s*(.+)$", content, re.MULTILINE)
    if not m:
        return ""
    desc = m.group(1).strip().strip("'\"")
    return desc[:140]


def skill_pending_diff(record: Dict[str, Any]) -> str:
    """为一笔已暂存的技能写入构造完整的统一 diff（或完整内容）。

    供 /skills diff <id> 在一个能渲染它的界面上使用（CLI 分页器、Web
    仪表盘，或直接打开待审 JSON 文件）。对 create 而言是新文件内容；对
    edit/patch 而言是针对当前磁盘上技能的统一 diff。
    """
    import difflib
    payload = record.get("payload", {})
    action = payload.get("action", "")
    name = payload.get("name", "")

    if action == "create":
        return (payload.get("content") or "")

    # 为可 diff 的动作解析当前磁盘上的内容。
    try:
        from tools.skill_manager_tool import _find_skill
    except Exception:
        _find_skill = None  # type: ignore

    current = ""
    target_label = "SKILL.md"
    if _find_skill is not None:
        found = _find_skill(name)
        if found:
            base = found["path"]
            if action == "edit":
                p = base / "SKILL.md"
            elif action in {"patch", "write_file"}:
                rel = payload.get("file_path") or "SKILL.md"
                p = base / rel
                target_label = rel
            else:
                p = base / "SKILL.md"
            try:
                if p.exists():
                    current = p.read_text(encoding="utf-8")
            except Exception:
                current = ""

    if action == "edit":
        new = payload.get("content") or ""
    elif action == "patch":
        old_s = payload.get("old_string") or ""
        new_s = payload.get("new_string") or ""
        new = current.replace(old_s, new_s) if current else f"(patch {old_s!r} → {new_s!r})"
    elif action == "write_file":
        new = payload.get("file_content") or ""
    elif action == "remove_file":
        return f"remove file: {payload.get('file_path')} from skill '{name}'"
    elif action == "delete":
        return f"delete skill '{name}'"
    else:
        return f"({action} on '{name}')"

    diff = difflib.unified_diff(
        current.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{target_label}",
        tofile=f"b/{target_label}",
    )
    text = "".join(diff)
    return text or "(no textual change)"
