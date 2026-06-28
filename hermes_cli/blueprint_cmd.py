"""CLI、TUI 和 gateway 的 ``/blueprint`` 命令共享逻辑。

仪表盘"自动化蓝图"表单的对话式对应物。界面有屏幕时，用户填写表单
（仪表盘 / GUI 应用），API 直接调用 ``fill_blueprint`` -> ``create_job``。
界面仅为聊天行时，用户按名称选择蓝图，agent 逐一询问所需信息
（消息助手模型：选择蓝图 → 它问你几个问题 → 完成）。

子命令形式：
  /blueprint                      列出目录
  /blueprint <name>               按名称匹配蓝图，然后 SEED THE AGENT
                                    以对话方式逐一询问用户各字段值
  /blueprint <name> slot=val …    直接填充并创建 cron 任务
                                    （仪表盘 / 文档 / 高级用户的确定性快捷方式 — 无需 agent 介入）

``<name>`` 格式宽松：精确 key、唯一前缀或模糊匹配均可解析；
查询有歧义时列出候选项；未知时建议最接近的结果。解析成功后，
处理器返回 ``agent_seed`` — 由蓝图的类型化字段和调度/提示模板
构建的自然语言指令 — 调用界面将其作为普通用户消息输入给 agent
（gateway：重写 ``event.text`` 并透传，即 ``/steer`` 模式；CLI：
主循环执行的一次性待定 seed）。agent 随后逐一询问各字段值，
并调用已有的 ``cronjob`` 工具。无需新工具，无需第二个任务引擎。

解析基于 shlex，因此带引号的自由文本值（``criteria="from my boss"``）
可正确保留。
"""

from __future__ import annotations

import difflib
import logging
import shlex
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class BlueprintCommandResult:
    """``/blueprint`` 调用的结果。

    ``text`` 始终显示给用户。当 ``agent_seed`` 有值时，
    调用界面还应将该 seed 作为用户的下一轮消息传给 agent
    （蓝图已匹配，agent 将以对话方式收集各字段值）。
    当 ``agent_seed`` 为 None 时，命令已完全处理
    （目录列表、直接创建或错误），不向 agent 发送任何内容。
    """

    text: str
    agent_seed: Optional[str] = None


def _resolve_origin(explicit: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if explicit is not None:
        return explicit
    try:
        from gateway.session_context import get_session_env

        platform = get_session_env("HERMES_SESSION_PLATFORM")
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID")
        if platform and chat_id:
            return {
                "platform": platform,
                "chat_id": chat_id,
                "chat_name": get_session_env("HERMES_SESSION_CHAT_NAME") or None,
                "thread_id": get_session_env("HERMES_SESSION_THREAD_ID") or None,
            }
    except Exception:
        pass
    return None


def _parse_kv(tokens) -> Tuple[Dict[str, str], list]:
    """从裸 token 中分离 ``slot=value`` token。返回 (values, leftovers)。"""
    values: Dict[str, str] = {}
    leftovers = []
    for tok in tokens:
        if "=" in tok:
            k, _, v = tok.partition("=")
            k = k.strip()
            if k:
                values[k] = v.strip()
                continue
        leftovers.append(tok)
    return values, leftovers


def match_blueprint(query: str) -> Tuple[Optional[Any], List[Any]]:
    """将自由输入的蓝图名称解析为对应蓝图。

    返回 ``(blueprint, candidates)``：
      * 精确 key 或唯一前缀/模糊匹配 -> ``(blueprint, [])``
      * 有歧义（2+ 个候选）        -> ``(None, [candidates…])``
      * 无合理匹配                 -> ``(None, [])``

    匹配策略宽松，因为聊天行用户需手动输入名称（不像仪表盘/Discord 可以选择）：
    先精确匹配 key，再对 key 或标题做大小写不敏感的前缀匹配，
    最后使用 difflib 进行模糊匹配。
    """
    from cron.blueprint_catalog import CATALOG, get_blueprint

    q = (query or "").strip().lower()
    if not q:
        return None, []

    exact = get_blueprint(q)
    if exact is not None:
        return exact, []

    # 对 key 或标题词首进行前缀匹配。
    prefix = [
        r for r in CATALOG
        if r.key.lower().startswith(q)
        or any(w.lower().startswith(q) for w in r.title.split())
    ]
    if len(prefix) == 1:
        return prefix[0], []
    if len(prefix) > 1:
        return None, prefix

    # 在 key/标题/描述中任意位置进行子串匹配。
    substr = [
        r for r in CATALOG
        if q in r.key.lower() or q in r.title.lower() or q in r.description.lower()
    ]
    if len(substr) == 1:
        return substr[0], []
    if len(substr) > 1:
        return None, substr

    # 对 key 进行模糊匹配（容忍拼写错误）。
    keys = [r.key for r in CATALOG]
    close = difflib.get_close_matches(q, keys, n=3, cutoff=0.6)
    if len(close) == 1:
        return get_blueprint(close[0]), []
    if len(close) > 1:
        return None, [get_blueprint(k) for k in close]

    return None, []


def _humanize_schedule(blueprint) -> str:
    from cron.blueprint_catalog import _humanize_schedule as _h

    try:
        return _h(blueprint)
    except Exception:
        return "on a schedule"


def build_blueprint_seed(blueprint) -> str:
    """构建 agent 将要执行的自然语言填充请求。

    agent 将此作为普通用户轮次读取，逐一询问用户每个未填充的字段值，
    然后调用 ``cronjob`` 工具，使用由蓝图 ``schedule_template``
    和渲染后的提示构建的 cron 表达式。默认值会明确标出，
    以便 agent 向用户提供建议。
    """
    from cron.blueprint_catalog import WEEKDAY_PRESETS

    lines: List[str] = []
    lines.append(
        f"Set up the '{blueprint.title}' automation for me (automation blueprint "
        f"'{blueprint.key}'). {blueprint.description}"
    )
    lines.append("")
    lines.append(
        "Ask me for each of these, one at a time, offering the default in "
        "brackets if I don't have a preference:"
    )
    for s in blueprint.slots:
        bits = [f"- {s.label} ({s.name})"]
        if s.options:
            bits.append(f" — one of: {', '.join(map(str, s.options))}")
        if s.default not in (None, ""):
            bits.append(f" [default: {s.default}]")
        if s.optional:
            bits.append(" (optional)")
        if s.help:
            bits.append(f" — {s.help}")
        lines.append("".join(bits))

    lines.append("")
    lines.append(
        "Once you have my answers, create the job by calling the cronjob tool "
        "with action='create'. Build the schedule as a cron expression from "
        f"this template: `{blueprint.schedule_template}` "
        "(fill {minute}/{hour} from the chosen time, {dow} from the weekday "
        f"choice using {dict(WEEKDAY_PRESETS)}, {{interval_min}} from any "
        "interval). Use this exact prompt for the job (substituting my "
        f"answers into any {{slot}} placeholders): \"{blueprint.prompt_template}\". "
        "Confirm the schedule and what it will do before you create it."
    )
    return "\n".join(lines)


def _fmt_catalog() -> str:
    from cron.blueprint_catalog import CATALOG

    lines = ["Automation Blueprints — `/blueprint <name>` and I'll ask you what I need:\n"]
    for r in CATALOG:
        lines.append(f"  • {r.key} — {r.title}")
        lines.append(f"    {r.description}")
    lines.append(
        "\nTip: `/blueprint <name>` walks you through it. Power users can "
        "pass values inline, e.g. `/blueprint morning-brief time=08:00`."
    )
    return "\n".join(lines)


def _fmt_candidates(query: str, candidates: List[Any]) -> str:
    lines = [f"'{query}' matches several blueprints — which one?\n"]
    for r in candidates:
        lines.append(f"  • {r.key} — {r.title}")
    lines.append("\nRun `/blueprint <name>` with one of the names above.")
    return "\n".join(lines)


def _fmt_no_match(query: str) -> str:
    from cron.blueprint_catalog import CATALOG

    keys = [r.key for r in CATALOG]
    close = difflib.get_close_matches((query or "").lower(), keys, n=3, cutoff=0.4)
    msg = f"No automation blueprint matches '{query}'."
    if close:
        msg += " Did you mean: " + ", ".join(close) + "?"
    msg += " Run /blueprint to see the catalog."
    return msg


def _manage_hint(surface: str) -> str:
    """创建后的管理提示。/cron 是仅限 CLI 的斜杠命令；
    在 gateway 平台上，用户通过询问 agent（cronjob 工具）
    或从仪表盘来管理任务。"""
    if surface == "cli":
        return "Manage it with /cron."
    return "Ask me to list, pause, or remove it any time."


def handle_blueprint_command(
    args: str,
    *,
    origin: Optional[Dict[str, Any]] = None,
    surface: str = "cli",
) -> BlueprintCommandResult:
    """分发 ``/blueprint`` 调用。

    返回 :class:`BlueprintCommandResult`。当 ``agent_seed`` 有值时，
    调用方必须将其作为用户的下一轮消息传给 agent；否则命令已完全处理，
    仅显示 ``text``。

    ``args`` 是 ``/blueprint`` 之后的所有内容。``origin`` 让直接创建的任务
    可以回送到其设置所在的聊天。``surface``（``"cli"`` | ``"gateway"``）
    决定后续提示的措辞 — ``/cron`` 仅存在于 CLI。
    """
    try:
        from cron.blueprint_catalog import fill_blueprint, BlueprintFillError
    except Exception as e:  # pragma: no cover - import guard
        logger.debug("blueprint catalog import failed: %s", e)
        return BlueprintCommandResult("Automation Blueprints are unavailable in this build.")

    try:
        tokens = shlex.split(args or "")
    except ValueError:
        tokens = (args or "").split()

    # 无参数 -> 列出目录。
    if not tokens:
        return BlueprintCommandResult(_fmt_catalog())

    query = tokens[0]
    values, _leftover = _parse_kv(tokens[1:])

    blueprint, candidates = match_blueprint(query)
    if blueprint is None:
        if candidates:
            return BlueprintCommandResult(_fmt_candidates(query, candidates))
        return BlueprintCommandResult(_fmt_no_match(query))

    # ``<name>`` 无内联字段值 -> 向 agent 注入 seed，由其逐一询问。
    if not values:
        seed = build_blueprint_seed(blueprint)
        text = (
            f"Setting up '{blueprint.title}' ({_humanize_schedule(blueprint)}). "
            "I'll ask you a couple of things…"
        )
        return BlueprintCommandResult(text, agent_seed=seed)

    # ``<name> slot=val …`` -> 直接填充并创建（确定性快捷方式）。
    try:
        spec = fill_blueprint(blueprint, values, origin=_resolve_origin(origin))
    except BlueprintFillError as e:
        return BlueprintCommandResult(
            f"Can't set up '{blueprint.title}': {e}\n"
            f"Or just run /blueprint {blueprint.key} and I'll ask you for the values."
        )

    try:
        from cron.jobs import create_job

        job = create_job(**spec)
    except Exception as e:
        logger.debug("blueprint create_job failed: %s", e)
        return BlueprintCommandResult(f"Failed to create the job: {e}")

    sched = job.get("schedule_display") or spec.get("schedule", "")
    return BlueprintCommandResult(
        f"Scheduled '{blueprint.title}'"
        + (f" ({sched})" if sched else "")
        + f", delivering to {spec.get('deliver', 'origin')}. {_manage_hint(surface)}"
    )
