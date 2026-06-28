"""面向所有工具的按线程中断信号机制。

提供线程作用域的中断跟踪，使得中断一个 agent 会话不会杀掉运行在
其他会话中的工具。这在 gateway 中尤为关键——多个 agent 会在同一
进程里并发运行。

agent 会在 run_conversation() 开始时保存自己的执行线程 ID，并把它
传给 set_interrupt()/clear_interrupt()。工具调用 is_interrupted() 时
会检查当前线程——无需任何参数。

在工具中的用法：
    from tools.interrupt import is_interrupted
    if is_interrupted():
        return {"output": "[interrupted]", "returncode": 130}
"""

import logging
import os
import threading

logger = logging.getLogger(__name__)

# 可选开启的调试跟踪——与 tools/environments/base.py 中的
# HERMES_DEBUG_INTERRUPT 配合使用。开启后会按调用记录 set/check 的日志，
# 这样在排查"中断已发送但工具始终没看到"的报告时，就能看清调用方线程、
# 目标线程以及当前状态。
_DEBUG_INTERRUPT = bool(os.getenv("HERMES_DEBUG_INTERRUPT"))

if _DEBUG_INTERRUPT:
    # AIAgent 的 quiet_mode 路径在 CLI 启动时会把 `tools` logger 强制设为 ERROR。
    # 这里把本模块自己的 logger 强制设回 INFO，让跟踪日志能在 agent.log 中可见。
    logger.setLevel(logging.INFO)

# 已被中断的线程 ident 集合。
_interrupted_threads: set[int] = set()
_lock = threading.Lock()


def set_interrupt(active: bool, thread_id: int | None = None) -> None:
    """为指定线程设置或清除中断。

    参数：
        active: 为 True 表示发送中断信号，为 False 表示清除中断。
        thread_id: 目标线程的 ident。当为 None 时，目标为当前线程
                   （为 CLI/测试保留向后兼容）。
    """
    tid = thread_id if thread_id is not None else threading.current_thread().ident
    with _lock:
        if active:
            _interrupted_threads.add(tid)
        else:
            _interrupted_threads.discard(tid)
        _snapshot = set(_interrupted_threads) if _DEBUG_INTERRUPT else None
    if _DEBUG_INTERRUPT:
        logger.info(
            "[interrupt-debug] set_interrupt(active=%s, target_tid=%s) "
            "called_from_tid=%s current_set=%s",
            active, tid, threading.current_thread().ident, _snapshot,
        )


def is_interrupted() -> bool:
    """检查当前线程是否被请求了中断。

    可在任意线程中安全调用——每个线程只能看到自己的中断状态。
    """
    tid = threading.current_thread().ident
    with _lock:
        return tid in _interrupted_threads


# ---------------------------------------------------------------------------
# 向后兼容的 _interrupt_event 代理
# ---------------------------------------------------------------------------
# 一些遗留的调用点（code_execution_tool、process_registry 以及测试）
# 会直接 import _interrupt_event 并调用 .is_set() / .set() / .clear()。
# 这个垫片把这些调用映射到上面的按线程函数，使得底层机制升级为线程作用域
# 后，既有代码仍然可以正常工作。

class _ThreadAwareEventProxy:
    """直接替换式代理，把 threading.Event 的方法映射到按线程的状态上。"""

    def is_set(self) -> bool:
        return is_interrupted()

    def set(self) -> None:  # noqa: A003
        set_interrupt(True)

    def clear(self) -> None:
        set_interrupt(False)

    def wait(self, timeout: float | None = None) -> bool:
        """并未真正实现——立即返回当前状态。"""
        return self.is_set()


_interrupt_event = _ThreadAwareEventProxy()
