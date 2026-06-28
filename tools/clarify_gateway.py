"""网关侧 clarify 原语（基于事件的阻塞队列）。

``clarify`` 工具需要向用户提一个问题并阻塞 agent 线程，直到用户
作答。在 CLI 模式下这很简单——``input()`` 是同步的。在网关模式下，
agent 运行在工作线程上，而事件循环负责处理用户的回复，因此我们需要
一个线程安全的原语，能够：

  * 存储一个挂起的 clarify 请求（附带生成的 ``clarify_id``），
  * 在一个 ``Event`` 上阻塞 agent 线程，
  * 当网关的按钮回调或文本拦截触发
    ``resolve_gateway_clarify(clarify_id, response)`` 时解除等待，
  * 支持超时，使永不回复的用户不会永远挂起 agent 线程
    （否则也会一直占着网关的 running-agent 守卫）。

状态为模块级（与 ``tools.approval`` 形状相同），这样平台适配器可以
调用 ``resolve_gateway_clarify``，而无需持有 ``GatewayRunner`` 实例
的反向引用。

适配器有两条投递路径：

  1. **按钮 UI** —— 适配器重写 ``send_clarify`` 以渲染内联按钮
     （例如 Telegram 的 ``InlineKeyboardMarkup``）。按钮回调以所选
     字符串作为结果。最后一个 "Other (type answer)" 按钮进入文本
     捕获模式，用于自由格式的回复。

  2. **文本回退** —— 没有富 UI 的适配器渲染一个编号列表。用户以
     数字（"2"）或自由文本回复；网关的 ``_handle_message`` 拦截该
     回复并直接解析。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# =========================================================================
# 模块级状态
# =========================================================================

@dataclass
class _ClarifyEntry:
    """网关会话内一个挂起的 clarify 请求。"""
    clarify_id: str
    session_key: str
    question: str
    choices: Optional[List[str]]
    event: threading.Event = field(default_factory=threading.Event)
    response: Optional[str] = None
    awaiting_text: bool = False  # 当用户选择 "Other" 或 clarify 为开放式时置位

    def signature(self) -> Dict[str, object]:
        return {
            "clarify_id": self.clarify_id,
            "session_key": self.session_key,
            "question": self.question,
            "choices": list(self.choices) if self.choices else None,
        }


_lock = threading.RLock()
# clarify_id → _ClarifyEntry（按钮回调的主查找表）
_entries: Dict[str, _ClarifyEntry] = {}
# session_key → list[clarify_id]（FIFO；用于文本回退拦截和会话清理）
_session_index: Dict[str, List[str]] = {}


# =========================================================================
# 公共 API —— agent 线程侧
# =========================================================================

def register(
    clarify_id: str,
    session_key: str,
    question: str,
    choices: Optional[List[str]],
) -> _ClarifyEntry:
    """注册一个挂起的 clarify 请求并返回该条目。

    调用方（gateway clarify_callback）随后会把提示发送给用户，
    并在 ``wait_for_response(clarify_id, timeout)`` 上阻塞。
    """
    entry = _ClarifyEntry(
        clarify_id=clarify_id,
        session_key=session_key,
        question=question,
        choices=list(choices) if choices else None,
        # 开放式（无选项）→ 下一条消息即为回复，无需按钮。
        awaiting_text=not bool(choices),
    )
    with _lock:
        _entries[clarify_id] = entry
        _session_index.setdefault(session_key, []).append(clarify_id)
    return entry


def wait_for_response(clarify_id: str, timeout: float) -> Optional[str]:
    """在条目的事件上阻塞，直到被解析或超时触发。

    以 1 秒为切片轮询，使 agent 的非活动心跳能持续触发——否则
    ``Event.wait(timeout=600)`` 会把线程阻塞 10 分钟，期间零活动触达，
    网关的非活动看门狗会在用户还在打字时就杀掉 agent。

    返回已解析的回复字符串，超时则返回 ``None``。
    """
    with _lock:
        entry = _entries.get(clarify_id)
    if entry is None:
        return None

    try:
        from tools.environments.base import touch_activity_if_due
    except Exception:  # pragma: no cover - 可选依赖
        touch_activity_if_due = None

    deadline = time.monotonic() + max(timeout, 0.0)
    activity_state = {"last_touch": time.monotonic(), "start": time.monotonic()}
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if entry.event.wait(timeout=min(1.0, remaining)):
            break
        if touch_activity_if_due is not None:
            touch_activity_if_due(activity_state, "waiting for user clarify response")

    with _lock:
        # 无论解析结果如何，都从索引中移除。
        _entries.pop(clarify_id, None)
        ids = _session_index.get(entry.session_key)
        if ids and clarify_id in ids:
            ids.remove(clarify_id)
            if not ids:
                _session_index.pop(entry.session_key, None)

    return entry.response


# =========================================================================
# 公共 API —— 网关 / 适配器侧
# =========================================================================

def resolve_gateway_clarify(clarify_id: str, response: str) -> bool:
    """解除在 ``clarify_id`` 上等待的 agent 线程阻塞。

    若找到并解析了条目则返回 True，否则返回 False
    （已解析、已过期或从未存在）。
    """
    with _lock:
        entry = _entries.get(clarify_id)
        if entry is None:
            return False
    entry.response = str(response) if response is not None else ""
    entry.event.set()
    return True


def get_pending_for_session(session_key: str) -> Optional[_ClarifyEntry]:
    """返回某个会话中最旧的挂起 clarify 条目，没有则返回 None。

    供 ``_handle_message`` 中的文本回退拦截使用——当一个 clarify
    正在等待自由格式的文本回复时，该会话的下一条用户消息会被
    捕获为答案。
    """
    with _lock:
        ids = _session_index.get(session_key) or []
        for cid in ids:
            entry = _entries.get(cid)
            if entry is None:
                continue
            if entry.awaiting_text:
                return entry
        return None


def mark_awaiting_text(clarify_id: str) -> bool:
    """把一个条目切换到文本捕获模式（用户选择了 'Other' 按钮）。

    若条目存在且已切换则返回 True，否则返回 False。
    """
    with _lock:
        entry = _entries.get(clarify_id)
        if entry is None:
            return False
        entry.awaiting_text = True
        return True


def has_pending(session_key: str) -> bool:
    """当此会话至少有一个挂起的 clarify 条目时返回 True。"""
    with _lock:
        ids = _session_index.get(session_key) or []
        return any(_entries.get(cid) is not None for cid in ids)


def clear_session(session_key: str) -> int:
    """解析并丢弃某个会话的所有挂起 clarify。

    供会话边界清理（例如 ``/new``、网关关闭、缓存 agent 驱逐）使用，
    使被阻塞的 agent 线程不会一直挂到其会话结束之后。返回被取消的
    条目数量。
    """
    with _lock:
        ids = list(_session_index.pop(session_key, []) or [])
        entries = [_entries.pop(cid, None) for cid in ids]
    cancelled = 0
    for entry in entries:
        if entry is None:
            continue
        # 空字符串哨兵——agent 代码可以通过检查 wait_for_response 的返回值
        # 配合自身的超时截止时间，把它与真实回复区分开。大多数调用方
        # 只是把任何假值结果当作 "用户未回复"。
        entry.response = ""
        entry.event.set()
        cancelled += 1
    return cancelled


# =========================================================================
# 配置
# =========================================================================

def get_clarify_timeout() -> int:
    """从配置读取 clarify 回复超时（秒）。

    默认 600（10 分钟）——既足够长让用户输入一段经过思考的回复，
    又足够短，使一个被遗弃的提示最终能解除 agent 线程的阻塞，
    而不是永远占着 running-agent 守卫。

    读取 config.yaml 中的 ``agent.clarify_timeout``。
    """
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        agent_cfg = cfg.get("agent", {}) or {}
        return int(agent_cfg.get("clarify_timeout", 600))
    except Exception:
        return 600


# =========================================================================
# 每会话通知钩子（网关 → 适配器桥接）
# =========================================================================
# 对应 tools.approval 的 _gateway_notify_cbs：网关注册一个每会话回调，
# 用于把 clarify 提示发送给用户。该回调桥接 sync→async（运行在 agent
# 线程上；在事件循环上调度适配器的 ``send_clarify`` 调用）。

_notify_cbs: Dict[str, Callable[[_ClarifyEntry], None]] = {}


def register_notify(session_key: str, cb: Callable[[_ClarifyEntry], None]) -> None:
    """注册一个 ``clarify_callback`` 使用的每会话通知回调。"""
    with _lock:
        _notify_cbs[session_key] = cb


def unregister_notify(session_key: str) -> None:
    """丢弃每会话通知回调，并取消所有挂起的 clarify 条目。"""
    with _lock:
        _notify_cbs.pop(session_key, None)
    # 取消所有挂起条目，使被阻塞的线程在运行结束时（中断、完成、
    # 网关关闭）得以收尾。
    clear_session(session_key)


def get_notify(session_key: str) -> Optional[Callable[[_ClarifyEntry], None]]:
    with _lock:
        return _notify_cbs.get(session_key)
