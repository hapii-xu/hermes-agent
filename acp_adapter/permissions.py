"""Hermes 危险命令审批的 ACP 权限桥接。"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import TimeoutError as FutureTimeout
from itertools import count
from typing import Callable

from acp.schema import (
    AllowedOutcome,
    PermissionOption,
)

logger = logging.getLogger(__name__)

# 将 ACP 权限选项 ID 映射到 Hermes 审批结果字符串。
# 即使选项列表不同，选项 ID 在 ``allow_permanent=True`` 和
# ``allow_permanent=False`` 两条路径中都保持稳定。
_OPTION_ID_TO_HERMES = {
    "allow_once": "once",
    "allow_session": "session",
    "allow_always": "always",
    "deny": "deny",
    "deny_always": "deny",
}

_PERMISSION_REQUEST_IDS = count(1)


def _permission_option_supports_kind(kind: str) -> bool:
    """返回已安装的 ACP SDK 是否接受指定的权限选项类型。"""
    try:
        PermissionOption(option_id="__probe__", kind=kind, name="probe")
    except Exception:
        return False
    return True


def _build_permission_options(*, allow_permanent: bool) -> list[PermissionOption]:
    """返回与 Hermes 审批语义匹配的 ACP 选项。"""
    options = [
        PermissionOption(option_id="allow_once", kind="allow_once", name="Allow once"),
        PermissionOption(
            option_id="allow_session",
            # ACP 没有会话级类型，因此使用最接近的持久化提示，
            # 同时在选项 ID 中保留 Hermes 语义。
            kind="allow_always",
            name="Allow for session",
        ),
    ]
    if allow_permanent:
        options.append(
            PermissionOption(
                option_id="allow_always",
                kind="allow_always",
                name="Allow always",
            ),
        )
    options.append(PermissionOption(option_id="deny", kind="reject_once", name="Deny"))
    if _permission_option_supports_kind("reject_always"):
        options.append(
            PermissionOption(
                option_id="deny_always",
                kind="reject_always",
                name="Deny always",
            ),
        )
    return options


def _build_permission_tool_call(command: str, description: str):
    """返回附加到权限请求的 ACP 工具调用更新。

    ``request_permission`` 期望 ``ToolCallUpdate`` 载荷 — 由
    ``_acp.update_tool_call`` 生成 — 而不是 ``ToolCallStart``。
    每个请求获得唯一的 ``perm-check-N`` ID，这样并发请求不会冲突。
    """
    import acp as _acp

    tool_call_id = f"perm-check-{next(_PERMISSION_REQUEST_IDS)}"
    title = f"{description}: {command}" if description else command
    content_text = f"{description}\n$ {command}" if description else f"$ {command}"
    return _acp.update_tool_call(
        tool_call_id,
        title=title,
        kind="execute",
        status="pending",
        content=[_acp.tool_content(_acp.text_block(content_text))],
        raw_input={"command": command, "description": description},
    )


def _map_outcome_to_hermes(outcome: object, *, allowed_option_ids: set[str]) -> str:
    """将 ACP 权限结果映射为 Hermes 审批字符串。"""
    if not isinstance(outcome, AllowedOutcome):
        return "deny"

    option_id = outcome.option_id
    if option_id not in allowed_option_ids:
        logger.warning("Permission request returned unknown option_id: %s", option_id)
        return "deny"
    return _OPTION_ID_TO_HERMES.get(option_id, "deny")


def make_approval_callback(
    request_permission_fn: Callable,
    loop: asyncio.AbstractEventLoop,
    session_id: str,
    timeout: float = 60.0,
) -> Callable[..., str]:
    """
    返回桥接到 ACP 的 Hermes 兼容审批回调。

    该回调接受 ``command`` 和 ``description`` 以及可选的关键字参数，
    如 ``tools.approval.prompt_dangerous_approval()`` 使用的 ``allow_permanent``。

    Args:
        request_permission_fn: ACP 连接的 ``request_permission`` 协程。
        loop: ACP 连接所在的事件循环。
        session_id: 当前 ACP 会话 ID。
        timeout: 自动拒绝前等待响应的秒数。
    """

    def _callback(
        command: str,
        description: str,
        *,
        allow_permanent: bool = True,
        **_: object,
    ) -> str:
        from agent.async_utils import safe_schedule_threadsafe

        options = _build_permission_options(allow_permanent=allow_permanent)

        tool_call = _build_permission_tool_call(command, description)
        coro = request_permission_fn(
            session_id=session_id,
            tool_call=tool_call,
            options=options,
        )
        future = safe_schedule_threadsafe(
            coro, loop,
            logger=logger,
            log_message="Permission request: failed to schedule on loop",
        )
        if future is None:
            return "deny"

        try:
            response = future.result(timeout=timeout)
        except (FutureTimeout, Exception) as exc:
            future.cancel()
            logger.warning("Permission request timed out or failed: %s", exc)
            return "deny"

        if response is None:
            return "deny"

        allowed_option_ids = {option.option_id for option in options}
        return _map_outcome_to_hermes(
            response.outcome,
            allowed_option_ids=allowed_option_ids,
        )

    return _callback
