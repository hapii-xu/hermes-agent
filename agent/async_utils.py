"""async/sync 桥接辅助工具。

代码库中约有 30 处通过 :func:`asyncio.run_coroutine_threadsafe`
从工作线程将协程调度到事件循环上。该函数可能抛出
:class:`RuntimeError`（例如在关机竞态中事件循环已被关闭），
此时协程对象既不会被 await，也不会被关闭 ——
这会触发 ``"coroutine '<name>' was never awaited"`` RuntimeWarning
并使协程帧泄漏直至 GC 回收。

:func:`safe_schedule_threadsafe` 对该调用进行封装：
在调度失败时关闭协程，并返回 ``None``（而非半成品的 future），
以便调用方可以干净地处理失败情况：

    fut = safe_schedule_threadsafe(coro, loop)
    if fut is None:
        return  # 或执行回退逻辑
    fut.result(timeout=5)

此辅助函数故意不处理 ``future.result()`` 的失败 ——
那是另一个关注点。一旦事件循环接受了协程，
其生命周期就属于事件循环，而非调度线程。
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Future
from typing import Any, Coroutine, Optional


_DEFAULT_LOGGER = logging.getLogger(__name__)


def safe_schedule_threadsafe(
    coro: Coroutine[Any, Any, Any],
    loop: Optional[asyncio.AbstractEventLoop],
    *,
    logger: Optional[logging.Logger] = None,
    log_message: str = "Failed to schedule coroutine on loop",
    log_level: int = logging.DEBUG,
) -> Optional[Future]:
    """从同步上下文将 ``coro`` 调度到 ``loop`` 上，防止协程泄漏。

    成功时返回 :class:`concurrent.futures.Future`；
    若事件循环为空或 :func:`asyncio.run_coroutine_threadsafe` 抛出异常
    （例如关机竞态中事件循环已关闭），则返回 ``None``。
    在所有失败路径中，协程都会被 :meth:`close`，
    以避免触发 ``"coroutine was never awaited"`` 警告或泄漏协程帧。

    调用方对返回的 future 拥有完全控制权：
    可以调用 ``.result(timeout=...)``、附加 ``add_done_callback``，
    或忽略它（fire-and-forget）等。
    """
    log = logger if logger is not None else _DEFAULT_LOGGER

    if loop is None:
        if asyncio.iscoroutine(coro):
            coro.close()
        log.log(log_level, "%s: loop is None", log_message)
        return None

    try:
        return asyncio.run_coroutine_threadsafe(coro, loop)
    except Exception as exc:
        if asyncio.iscoroutine(coro):
            coro.close()
        log.log(log_level, "%s: %s", log_message, exc)
        return None
