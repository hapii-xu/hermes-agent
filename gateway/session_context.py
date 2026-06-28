"""
Hermes gateway 的会话级上下文变量。

使用 Python 的 ``contextvars.ContextVar`` 替代此前基于 ``os.environ`` 的
会话状态（``HERMES_SESSION_PLATFORM``、``HERMES_SESSION_CHAT_ID`` 等）。

**为何重要**

gateway 通过 ``asyncio`` 并发处理消息。当两条消息同时到达时，旧代码会这样做：

    os.environ["HERMES_SESSION_THREAD_ID"] = str(context.source.thread_id)

由于 ``os.environ`` 是 *进程级全局* 的，在消息 A 的 agent 运行结束之前，
它的值会被消息 B 静默覆盖。因此后台任务通知和工具调用会被路由到错误的
thread。

``contextvars.ContextVar`` 的值是 *任务级局部* 的：每个 ``asyncio``
任务（以及它通过 ``run_in_executor`` 派生的任何线程）都拥有自己的一份副本，
因此并发消息之间互不干扰。

**向后兼容**

公共辅助函数 ``get_session_env(name, default="")`` 与旧的
``os.getenv("HERMES_SESSION_*", ...)`` 调用一一对应。现有工具代码只需
替换 import 和调用处即可：

    # 之前
    import os
    platform = os.getenv("HERMES_SESSION_PLATFORM", "")

    # 之后
    from gateway.session_context import get_session_env
    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
"""

from contextvars import ContextVar
from typing import Any

# 哨兵值，用于区分"在此上下文中从未设置"与"显式设置为空字符串"。
# 当 contextvar 持有 _UNSET 时，回退到 os.environ（兼容 CLI/cron）。
# 当持有 "" 时（clear_session_vars 重置后），直接返回 "" — 不再回退。
_UNSET: Any = object()

# ---------------------------------------------------------------------------
# 按任务的会话变量
# ---------------------------------------------------------------------------

_SESSION_PLATFORM: ContextVar = ContextVar("HERMES_SESSION_PLATFORM", default=_UNSET)
_SESSION_SOURCE: ContextVar = ContextVar("HERMES_SESSION_SOURCE", default=_UNSET)
_SESSION_CHAT_ID: ContextVar = ContextVar("HERMES_SESSION_CHAT_ID", default=_UNSET)
_SESSION_CHAT_NAME: ContextVar = ContextVar("HERMES_SESSION_CHAT_NAME", default=_UNSET)
_SESSION_THREAD_ID: ContextVar = ContextVar("HERMES_SESSION_THREAD_ID", default=_UNSET)
_SESSION_USER_ID: ContextVar = ContextVar("HERMES_SESSION_USER_ID", default=_UNSET)
_SESSION_USER_NAME: ContextVar = ContextVar("HERMES_SESSION_USER_NAME", default=_UNSET)
_SESSION_KEY: ContextVar = ContextVar("HERMES_SESSION_KEY", default=_UNSET)
_SESSION_ID: ContextVar = ContextVar("HERMES_SESSION_ID", default=_UNSET)
# 触发当前 turn 的消息 ID。用作回复锚点，使后台进程通知留在发起的
# Telegram 私聊 topic 内（这些通道仅凭 thread id + 回复锚点路由）。
_SESSION_MESSAGE_ID: ContextVar = ContextVar("HERMES_SESSION_MESSAGE_ID", default=_UNSET)

# 表示当前会话的投递通道是否能在当前 turn 结束之后将一次 ASYNC 完成结果
# 路由回 agent（即唤醒一个新的 turn）。
#
# True  — CLI（进程内 completion_queue 排空）以及真正的 gateway
#         平台（Telegram/Discord/Slack/...），它们持有持久的出站通道并
#         运行 watcher/drain 循环。
# False — 无状态的请求/响应适配器（API server：每条路由，无论是 spec 还是
#         专有的，都会在 turn 结束时拆除通道，因此稍后完成的后台结果无处可去）。
#
# 承诺异步投递的工具（terminal notify_on_complete / watch_patterns、
# delegate_task background=True）通过 ``async_delivery_supported()`` 读取
# 此值，并拒绝给出通道无法兑现的承诺 — 把静默 no-op 变成显式契约。
#
# 默认 _UNSET => 视为支持，因此 CLI（从不设置 platform）以及任何不感知
# contextvar 的路径都能继续工作。无状态适配器通过在 adapter 类上设置
# ``supports_async_delivery = False`` 来显式 opt OUT；gateway 在 session-bind
# 时把该值传播进此 contextvar。
_SESSION_ASYNC_DELIVERY: ContextVar = ContextVar("HERMES_SESSION_ASYNC_DELIVERY", default=_UNSET)

# Cron 自动投递变量 — 在 run_job() 中按 job 设置，以免并发 job 互相覆盖对方的
# 投递目标。
_CRON_AUTO_DELIVER_PLATFORM: ContextVar = ContextVar("HERMES_CRON_AUTO_DELIVER_PLATFORM", default=_UNSET)
_CRON_AUTO_DELIVER_CHAT_ID: ContextVar = ContextVar("HERMES_CRON_AUTO_DELIVER_CHAT_ID", default=_UNSET)
_CRON_AUTO_DELIVER_THREAD_ID: ContextVar = ContextVar("HERMES_CRON_AUTO_DELIVER_THREAD_ID", default=_UNSET)

_VAR_MAP = {
    "HERMES_SESSION_PLATFORM": _SESSION_PLATFORM,
    "HERMES_SESSION_SOURCE": _SESSION_SOURCE,
    "HERMES_SESSION_CHAT_ID": _SESSION_CHAT_ID,
    "HERMES_SESSION_CHAT_NAME": _SESSION_CHAT_NAME,
    "HERMES_SESSION_THREAD_ID": _SESSION_THREAD_ID,
    "HERMES_SESSION_USER_ID": _SESSION_USER_ID,
    "HERMES_SESSION_USER_NAME": _SESSION_USER_NAME,
    "HERMES_SESSION_KEY": _SESSION_KEY,
    "HERMES_SESSION_ID": _SESSION_ID,
    "HERMES_SESSION_MESSAGE_ID": _SESSION_MESSAGE_ID,
    "HERMES_CRON_AUTO_DELIVER_PLATFORM": _CRON_AUTO_DELIVER_PLATFORM,
    "HERMES_CRON_AUTO_DELIVER_CHAT_ID": _CRON_AUTO_DELIVER_CHAT_ID,
    "HERMES_CRON_AUTO_DELIVER_THREAD_ID": _CRON_AUTO_DELIVER_THREAD_ID,
}


def set_current_session_id(session_id: str) -> None:
    """在 ContextVar 和 ``os.environ`` 之间同步 ``HERMES_SESSION_ID``。

    像 CLI 这样长生命周期的单进程入口可以通过 ``/new``、``/resume``、
    ``/branch`` 或压缩拆分来轮换 session，而无需重建整个 agent。工具仍通过
    ``get_session_env("HERMES_SESSION_ID")`` 读取，并以 ``os.environ`` 作为
    回退，因此当活动 session 变化时，两条存储路径必须同步移动。
    """
    import os

    os.environ["HERMES_SESSION_ID"] = session_id
    _SESSION_ID.set(session_id)


def set_session_vars(
    platform: str = "",
    source: str = "",
    chat_id: str = "",
    chat_name: str = "",
    thread_id: str = "",
    user_id: str = "",
    user_name: str = "",
    session_key: str = "",
    session_id: str = "",
    message_id: str = "",
    cwd: str = "",
    async_delivery: bool = True,
) -> list:
    """设置全部会话上下文变量并返回 reset token。

    在 handler 退出时，请在 ``finally`` 块中调用 ``clear_session_vars(tokens)``。
    注意 ``clear_session_vars`` 会把每个变量重置为 ``""``（以抑制 ``os.environ``
    回退），而不是恢复之前的值 —— 这些辅助函数不可嵌套/非栈安全，返回的 token
    仅为 API 兼容而保留。

    ``cwd`` 为此上下文固定逻辑工作目录。

    ``async_delivery`` 声明此会话的通道是否能在 turn 结束后将一次后台完成结果
    路由回 agent（参见 ``_SESSION_ASYNC_DELIVERY`` / ``async_delivery_supported``）。
    无状态请求/响应适配器（API server）传入 ``False``。
    """
    tokens = [
        _SESSION_PLATFORM.set(platform),
        _SESSION_SOURCE.set(source),
        _SESSION_CHAT_ID.set(chat_id),
        _SESSION_CHAT_NAME.set(chat_name),
        _SESSION_THREAD_ID.set(thread_id),
        _SESSION_USER_ID.set(user_id),
        _SESSION_USER_NAME.set(user_name),
        _SESSION_KEY.set(session_key),
        _SESSION_ID.set(session_id),
        _SESSION_MESSAGE_ID.set(message_id),
        _SESSION_ASYNC_DELIVERY.set(bool(async_delivery)),
    ]
    try:
        from agent.runtime_cwd import set_session_cwd

        set_session_cwd(cwd)
    except Exception:
        pass
    return tokens


def clear_session_vars(tokens: list) -> None:
    """将会话上下文变量标记为已显式清除。

    将所有变量设置为 ``""``，使 ``get_session_env`` 返回空字符串而不是回退到
    （可能已过期的）``os.environ`` 值。*tokens* 参数仅为兼容那些保存了
    ``set_session_vars`` 返回值的调用方而保留，但实际清除使用 ``var.set("")``
    而非 ``var.reset(token)``，以确保"显式清除"状态与"从未设置"（持有 ``_UNSET``
    哨兵）的状态可区分。
    """
    for var in (
        _SESSION_PLATFORM,
        _SESSION_SOURCE,
        _SESSION_CHAT_ID,
        _SESSION_CHAT_NAME,
        _SESSION_THREAD_ID,
        _SESSION_USER_ID,
        _SESSION_USER_NAME,
        _SESSION_KEY,
        _SESSION_ID,
        _SESSION_MESSAGE_ID,
    ):
        var.set("")
    # 将异步投递能力重置为"从未设置"哨兵，而不是某个 falsy 值：已清除的上下文应
    # 回退到默认支持的行为（CLI / 不感知的路径），而不是被误认为已 opt-out 的
    # 无状态适配器。
    _SESSION_ASYNC_DELIVERY.set(_UNSET)
    try:
        from agent.runtime_cwd import clear_session_cwd

        clear_session_cwd()
    except Exception:
        pass


def get_session_env(name: str, default: str = "") -> str:
    """按旧的 ``HERMES_SESSION_*`` 名称读取会话上下文变量。

    是 ``os.getenv("HERMES_SESSION_*", default)`` 的直接替代品。

    解析顺序：
    1. context variable（由 gateway 设置以保证并发安全访问）。
       如果变量被显式设置（即使为 ``""``）—— 通过 ``set_session_vars`` 或
       ``clear_session_vars`` —— 则返回该值 — **不回退到 os.environ**。
    2. ``os.environ``（仅当 context variable 在此上下文中从未被设置时 —— 即
       CLI、cron 调度器以及完全不使用 ``set_session_vars`` 的测试进程）。
    3. *default*
    """
    import os

    var = _VAR_MAP.get(name)
    if var is not None:
        value = var.get()
        if value is not _UNSET:
            return value
    # 回退到 os.environ，以兼容 CLI、cron 和测试
    return os.getenv(name, default)


def async_delivery_supported() -> bool:
    """当前会话是否能在稍后投递一次后台完成结果。

    仅当活动会话由无状态适配器（API server）显式绑定时才返回 ``False`` —— 该适配器
    无法在 turn 结束后将通知路由回 agent。CLI、cron 和真正的 gateway 平台 —— 以及
    任何从未绑定该 contextvar 的路径 —— 都返回 ``True``。

    承诺异步投递的工具（``terminal`` notify_on_complete / watch_patterns、
    ``delegate_task`` background=True）在注册 watcher / 派发分离子任务之前会查询此值，
    以便在通道无法兑现承诺时拒绝它，而不是静默 no-op。
    """
    value = _SESSION_ASYNC_DELIVERY.get()
    if value is _UNSET:
        return True
    return bool(value)
