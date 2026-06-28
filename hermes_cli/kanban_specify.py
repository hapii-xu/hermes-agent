"""Kanban triage 规格化器 — 将一句话想法充实为完整的 spec。

由 ``hermes kanban specify [task_id | --all]`` 使用。接收一个位于 Triage 列
的任务（一个粗略想法，通常只有标题），调用辅助 LLM 生成：

  * 精简后的标题（可选 — 仅在模型提出明显不同的标题时才替换）
  * 具体的 body：目标、建议方法、验收标准

然后通过 ``kanban_db.specify_triage_task`` 将任务从 ``triage -> todo``
翻转。调度器会在下一个 tick 将其提升为 ``ready``（如果没有未完成的父任务
则立即提升）。

设计说明
------------

* 本模块有意与 ``hermes_cli/goals.py`` 结构一致 — 相同的 aux 客户端模式，
  相同的"空配置 => 跳过而非崩溃"容错机制。保持接口精简，失败模式可预测。

* prompt 是一对简短的 system + user。我们要求返回 ``{title, body}`` 格式的
  JSON；如果解析失败，回退为将整个响应视为 body，标题保持不变。无重试循环
  — 一次调用，控制成本。

* 不显式请求 structured output / JSON mode，以便 specifier 可以在不支持
  该功能的 provider 上工作。解析是宽容的（容忍 JSON 外部的 markdown 代码块）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_db as kb

from utils import env_int

HERMES_KANBAN_SPECIFY_MAX_TOKENS = max(
    1500,
    env_int("HERMES_KANBAN_SPECIFY_MAX_TOKENS", 6000),
)

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are the Kanban triage specifier for the Hermes Agent board.
A user dropped a rough idea into the Triage column. Your job is to turn it
into a concrete, actionable task spec that an autonomous worker can pick up
and execute without further clarification.

Output a single JSON object with exactly two keys:

  {
    "title": "<tightened task title, <= 80 chars, imperative voice>",
    "body":  "<multi-line spec, see structure below>"
  }

The body MUST include these sections, each prefixed with a bold markdown
heading, in this order:

  **Goal** — one sentence, user-facing outcome.
  **Approach** — 2-5 bullets on how a worker should tackle it.
  **Acceptance criteria** — checklist of concrete, verifiable conditions.
  **Out of scope** — short list of things NOT to touch (omit if nothing
      obvious; never invent scope creep).

Rules:
  - Keep the tightened title close in meaning to the original idea — do
    NOT invent a different project.
  - If the original idea is already detailed, preserve its substance and
    just reformat into the sections above.
  - Never add invented requirements the user didn't hint at.
  - No preamble, no closing remarks, no code fences around the JSON.
  - Output only the JSON object and nothing else.
"""


_USER_TEMPLATE = """Task id: {task_id}
Current title: {title}
Current body:
{body}
"""


@dataclass
class SpecifyOutcome:
    """指定单个 triage 任务的结果。"""

    task_id: str
    ok: bool
    reason: str = ""
    new_title: Optional[str] = None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _extract_json_blob(raw: str) -> Optional[dict]:
    """宽容的 JSON 提取 — 容忍代码块和前后空白。解析失败时返回 None。"""
    if not raw:
        return None
    stripped = _FENCE_RE.sub("", raw.strip())
    # 贪心策略：找到第一个 `{` 和最后一个 `}` 并尝试解析该切片。
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    candidate = stripped[first : last + 1]
    try:
        val = json.loads(candidate)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(val, dict):
        return None
    return val


def _profile_author() -> str:
    """``hermes_cli.kanban._profile_author`` 的镜像。本地保留以避免
    kanban.py 导入本模块时产生循环导入。"""
    return (
        os.environ.get("HERMES_PROFILE")
        or os.environ.get("USER")
        or "specifier"
    )


def specify_task(
    task_id: str,
    *,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
) -> SpecifyOutcome:
    """指定单个 triage 任务并将其提升为 ``todo``。

    返回一个描述执行结果的对象。对可预见的失败模式（任务不在 triage、
    未配置 aux 客户端、API 错误、响应格式错误）绝不抛出异常 — 这些
    通过 ``ok=False`` 反映，以便 ``--all`` 扫描可以跳过单个失败继续执行。
    """
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    if task is None:
        return SpecifyOutcome(task_id, False, "unknown task id")
    if task.status != "triage":
        return SpecifyOutcome(
            task_id, False, f"task is not in triage (status={task.status!r})"
        )

    try:
        from agent.auxiliary_client import get_auxiliary_extra_body, get_text_auxiliary_client
    except Exception as exc:  # pragma: no cover — import smoke test
        logger.debug("specify: auxiliary client import failed: %s", exc)
        return SpecifyOutcome(task_id, False, "auxiliary client unavailable")

    try:
        client, model = get_text_auxiliary_client("triage_specifier")
    except Exception as exc:
        logger.debug("specify: get_text_auxiliary_client failed: %s", exc)
        return SpecifyOutcome(task_id, False, "auxiliary client unavailable")

    if client is None or not model:
        return SpecifyOutcome(
            task_id, False, "no auxiliary client configured"
        )

    user_msg = _USER_TEMPLATE.format(
        task_id=task.id,
        title=_truncate(task.title or "", 400),
        body=_truncate(task.body or "(no body)", 4000),
    )

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=HERMES_KANBAN_SPECIFY_MAX_TOKENS,
            timeout=timeout or 120,
            extra_body=get_auxiliary_extra_body() or None,
        )
    except Exception as exc:
        logger.info(
            "specify: API call failed for %s (%s) — skipping",
            task_id, exc,
        )
        return SpecifyOutcome(
            task_id, False, f"LLM error: {type(exc).__name__}"
        )

    try:
        raw = (resp.choices[0].message.content or "").strip()
    except Exception:
        raw = ""

    parsed = _extract_json_blob(raw)

    new_title: Optional[str]
    new_body: Optional[str]
    if parsed is None:
        # 回退策略：将整个回复视为 body，标题保持原样。
        # 最坏情况是用户事后手动编辑 — 仍好过在 triage 中因 LLM 回复
        # 格式错误而导致任务被搁置。
        stripped_raw = raw.strip()
        if not stripped_raw:
            return SpecifyOutcome(
                task_id, False, "LLM returned an empty response"
            )
        new_title = None
        new_body = stripped_raw
    else:
        title_val = parsed.get("title")
        body_val = parsed.get("body")
        new_title = (
            title_val.strip()
            if isinstance(title_val, str) and title_val.strip()
            else None
        )
        new_body = (
            body_val if isinstance(body_val, str) and body_val.strip() else None
        )
        if new_body is None and new_title is None:
            return SpecifyOutcome(
                task_id, False, "LLM response missing title and body"
            )

    with kb.connect_closing() as conn:
        ok = kb.specify_triage_task(
            conn,
            task_id,
            title=new_title,
            body=new_body,
            author=author or _profile_author(),
        )
    if not ok:
        # 竞态：在我们上面的读取和写入之间，其他人已经提升/归档了该任务。
        # 报告错误，不崩溃。
        return SpecifyOutcome(
            task_id, False, "task moved out of triage before promotion"
        )
    return SpecifyOutcome(task_id, True, "specified", new_title=new_title)


def list_triage_ids(*, tenant: Optional[str] = None) -> list[str]:
    """返回当前处于 triage 列的任务 id。

    ``tenant`` 缩小扫描范围；``None`` 返回所有 triage 任务。
    """
    with kb.connect_closing() as conn:
        tasks = kb.list_tasks(
            conn,
            status="triage",
            tenant=tenant,
            include_archived=False,
        )
    return [t.id for t in tasks]
