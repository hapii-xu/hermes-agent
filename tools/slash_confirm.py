"""通用斜杠命令确认原语（gateway 端）。

那些副作用非破坏性但代价较高、值得提示给用户的斜杠命令
（目前只有 ``/reload-mcp``，它会使 provider 的提示词缓存失效）
都经由本模块处理。

两条投递路径：

  1. 按钮 UI —— 重写了 ``send_slash_confirm`` 的适配器会渲染
     三个内联按钮（批准一次 / 始终批准 / 取消）。
     按钮回调会调用 ``resolve(session_key, confirm_id, choice)``。

  2. 文本兜底 —— 没有按钮 UI 的适配器会收到一个纯文本提示。
     用户用 ``/approve``、``/always`` 或 ``/cancel`` 回复；
     gateway 的 ``_handle_message`` 会拦截这些回复并直接调用
     ``resolve()``。

状态存储在模块级别（与 ``tools.approval`` 一样），这样平台
适配器在解析回调时就无需持有 ``GatewayRunner`` 实例的反向引用。
CLI 路径（``cli.py``）使用一个本地同步变体 —— 见那里的
``_prompt_slash_confirm``。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# 按 gateway 的 session_key 索引的待处理确认。每条记录：
#   {
#       "confirm_id": str,
#       "command":    str,                       # 例如 "reload-mcp"
#       "handler":    Callable[[str], Awaitable[Optional[str]]],
#       "created_at": float,                     # time.time()
#   }
_pending: Dict[str, Dict[str, Any]] = {}
_lock = threading.RLock()

# 默认超时 —— 当同一会话的下一条消息到达时，比此更久的待处理确认会被丢弃。
# 按钮在适配器丢弃 callback_data 之前一直有效（Telegram：约 48 小时；
# Discord：临时消息；Slack：3 秒 ack + 长时效的 actions）。
DEFAULT_TIMEOUT_SECONDS = 300


def register(
    session_key: str,
    confirm_id: str,
    command: str,
    handler: Callable[[str], Awaitable[Optional[str]]],
) -> None:
    """注册一个待处理的斜杠命令确认。

    会覆盖同一 ``session_key`` 上之前任何待处理的确认 ——
    用户发起新的可确认命令会取代那条已过期的确认。
    """
    with _lock:
        _pending[session_key] = {
            "confirm_id": confirm_id,
            "command": command,
            "handler": handler,
            "created_at": time.time(),
        }


def get_pending(session_key: str) -> Optional[Dict[str, Any]]:
    """返回某个会话的待处理确认字典，若没有则返回 None。"""
    with _lock:
        entry = _pending.get(session_key)
        return dict(entry) if entry else None


def clear(session_key: str) -> None:
    """丢弃 ``session_key`` 对应的待处理确认，但不执行它。"""
    with _lock:
        _pending.pop(session_key, None)


def clear_if_stale(session_key: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> bool:
    """如果待处理确认已超过 ``timeout`` 秒，则丢弃它。

    如果丢弃了一条记录则返回 True。
    """
    with _lock:
        entry = _pending.get(session_key)
        if not entry:
            return False
        if time.time() - float(entry.get("created_at", 0) or 0) > timeout:
            _pending.pop(session_key, None)
            return True
        return False


async def resolve(
    session_key: str,
    confirm_id: str,
    choice: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Optional[str]:
    """解析一个待处理确认。

    ``choice`` 必须是 ``"once"``、``"always"`` 或 ``"cancel"`` 之一。
    返回 handler 的输出字符串（作为后续消息发送），
    若确认已过期、已解析或 confirm_id 不匹配则返回 ``None``。

    可安全地从 asyncio 回调（按钮点击）或从 gateway 的消息拦截路径调用。
    """
    with _lock:
        entry = _pending.get(session_key)
        if not entry:
            return None
        if entry.get("confirm_id") != confirm_id:
            # 过期的 confirm_id —— 已被同一会话上更新的提示取代。
            return None
        # 在运行 handler 之前先弹出，以防重复回调
        #（例如按钮双击）把它运行两次。
        _pending.pop(session_key, None)
        if time.time() - float(entry.get("created_at", 0) or 0) > timeout:
            return None
        handler = entry.get("handler")
        command = entry.get("command", "?")

    if not handler:
        return None
    try:
        result = await handler(choice)
    except Exception as exc:
        logger.error(
            "Slash-confirm handler for /%s raised: %s",
            command, exc, exc_info=True,
        )
        return f"❌ Error handling confirmation: {exc}"
    return result if isinstance(result, str) else None


def resolve_sync_compat(
    loop: asyncio.AbstractEventLoop,
    session_key: str,
    confirm_id: str,
    choice: str,
) -> Optional[str]:
    """同步辅助：在事件循环上调度 resolve() 并等待结果。

    供运行在与事件循环不同线程上的平台回调路径使用
    （例如某些配置下 Discord 的按钮点击 handler）。
    在异步上下文中请优先使用异步的 ``resolve()``。
    """
    try:
        from agent.async_utils import safe_schedule_threadsafe
        fut = safe_schedule_threadsafe(
            resolve(session_key, confirm_id, choice), loop,
            logger=logger,
            log_message="resolve_sync_compat scheduling failed",
        )
        if fut is None:
            return None
        return fut.result(timeout=30)
    except Exception as exc:
        logger.error("resolve_sync_compat failed: %s", exc)
        return None
