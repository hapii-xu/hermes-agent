"""面向 agent 的工具：响应被 CDP 监督器捕获的原生 JS 对话框。

本工具只做响应 —— agent 先从 ``browser_snapshot`` 输出中读取
``pending_dialogs``，再调用 ``browser_dialog(action=...)`` 来
接受或关闭。

与 ``browser_cdp`` 一样以 ``_browser_cdp_check`` 作为门控，
所以只有当 CDP 端点可达时它才出现（带有 ``connectUrl`` 的
Browserbase、通过 ``/browser connect`` 连接的本地 Chromium 系浏览器，
或在配置中设置了 ``browser.cdp_url``）。

完整设计见
``website/docs/developer-guide/browser-supervisor.md``。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from tools.browser_supervisor import SUPERVISOR_REGISTRY
from tools.registry import registry

logger = logging.getLogger(__name__)


BROWSER_DIALOG_SCHEMA: Dict[str, Any] = {
    "name": "browser_dialog",
    "description": (
        "Respond to a native JavaScript dialog (alert / confirm / prompt / "
        "beforeunload) that is currently blocking the page.\n\n"
        "**Workflow:** call ``browser_snapshot`` first — if a dialog is open, "
        "it appears in the ``pending_dialogs`` field with ``id``, ``type``, "
        "and ``message``. Then call this tool with ``action='accept'`` or "
        "``action='dismiss'``.\n\n"
        "**Prompt dialogs:** pass ``prompt_text`` to supply the response "
        "string. Ignored for alert/confirm/beforeunload.\n\n"
        "**Multiple dialogs:** if more than one dialog is queued (rare — "
        "happens when a second dialog fires while the first is still open), "
        "pass ``dialog_id`` from the snapshot to disambiguate.\n\n"
        "**Availability:** only present when a CDP-capable backend is "
        "attached — Browserbase sessions, local Chromium-family browser via "
        "``/browser connect``, or ``browser.cdp_url`` in config.yaml. "
        "Not available on Camofox (REST-only) or the default Playwright "
        "local browser (CDP port is hidden)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["accept", "dismiss"],
                "description": (
                    "'accept' clicks OK / returns the prompt text. "
                    "'dismiss' clicks Cancel / returns null from prompt(). "
                    "For ``beforeunload`` dialogs: 'accept' allows the "
                    "navigation, 'dismiss' keeps the page."
                ),
            },
            "prompt_text": {
                "type": "string",
                "description": (
                    "Response string for a ``prompt()`` dialog. Ignored for "
                    "other dialog types. Defaults to empty string."
                ),
            },
            "dialog_id": {
                "type": "string",
                "description": (
                    "Specific dialog to respond to, from "
                    "``browser_snapshot.pending_dialogs[].id``. Required "
                    "only when multiple dialogs are queued."
                ),
            },
        },
        "required": ["action"],
    },
}


def browser_dialog(
    action: str,
    prompt_text: Optional[str] = None,
    dialog_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> str:
    """响应当前活动任务 CDP 监督器上的一个待处理对话框。"""
    effective_task_id = task_id or "default"
    supervisor = SUPERVISOR_REGISTRY.get(effective_task_id)
    if supervisor is None:
        return json.dumps(
            {
                "success": False,
                "error": (
                    "No CDP supervisor is attached to this task. Either the "
                    "browser backend doesn't expose CDP (Camofox, default "
                    "Playwright) or no browser session has been started yet. "
                    "Call browser_navigate or /browser connect first."
                ),
            }
        )

    result = supervisor.respond_to_dialog(
        action=action,
        prompt_text=prompt_text,
        dialog_id=dialog_id,
    )
    if result.get("ok"):
        return json.dumps(
            {
                "success": True,
                "action": action,
                "dialog": result.get("dialog", {}),
            }
        )
    return json.dumps({"success": False, "error": result.get("error", "unknown error")})


def _browser_dialog_check() -> bool:
    """门控：与 ``browser_cdp`` 相同 —— 仅在 CDP 可达时才提供。

    保持一致，这样两个工具会一起出现和消失。监督器本身由
    ``browser_navigate`` / ``/browser connect`` / 创建 Browserbase
    会话时延迟启动，因此一个可达的 CDP URL 就足以确定要展示该工具。
    """
    try:
        from tools.browser_cdp_tool import _browser_cdp_check  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover —— 防御性
        logger.debug("browser_dialog check: browser_cdp_tool import failed: %s", exc)
        return False
    return _browser_cdp_check()


registry.register(
    name="browser_dialog",
    toolset="browser-cdp",
    schema=BROWSER_DIALOG_SCHEMA,
    handler=lambda args, **kw: browser_dialog(
        action=args.get("action", ""),
        prompt_text=args.get("prompt_text"),
        dialog_id=args.get("dialog_id"),
        task_id=kw.get("task_id"),
    ),
    check_fn=_browser_dialog_check,
    emoji="💬",
)
