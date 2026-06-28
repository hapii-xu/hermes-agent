"""gateway 的周期性进程内存使用日志记录。

移植自 cline/cline#10343（src/standalone/memory-monitor.ts）。

gateway 是一个长期存活的进程，会随着缓存 agent 实例、会话记录、工具 schema、
memory provider、MCP 连接等不断累积内存。这些子系统里任何一个发生缓慢泄漏，
在单行日志里都看不出来——只有通过观察 RSS 在数小时内的攀升才能发现。

本模块每隔 N 分钟（默认 5 分钟）输出一行结构化的 ``[MEMORY] ...`` 日志，
这样在排查疑似泄漏时，维护者就可以 grep ``agent.log`` / ``gateway.log``，
得到一个由 RSS + Python GC 统计构成的时间序列。计时器运行在后台线程中，
并随 gateway 一同干净地关闭。

设计要点（与 Cline 移植版保持一致）：
  * 对 grep 友好的、以 ``[MEMORY]`` 开头的单行格式。
  * 关闭时记录最终快照，从而“退出前的最后一个 RSS”总会出现在日志里。
  * 启动时立即记录基线快照。
  * 守护线程——绝不阻塞进程退出。
  * 优先使用 ``resource``（标准库，Linux/macOS），在 ``resource`` 不可用时
    （Windows）回退到 ``psutil``。两者都是可选的；当两者都不可用时，我们
    只发出一次 WARNING 并禁用监控，而不是让 gateway 崩溃。

配置：``config.yaml`` 中的 ``logging.memory_monitor``——默认值块见
``hermes_cli/config.py``。
"""

from __future__ import annotations

import gc
import logging
import os
import sys
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_BYTES_TO_MB = 1024 * 1024

_monitor_thread: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None
_start_time: Optional[float] = None
_interval_seconds: float = 300.0  # 5 分钟
_lock = threading.Lock()


def _get_rss_mb() -> Optional[int]:
    """返回当前进程的常驻集大小（RSS，单位 MB），不可用时返回 None。

    优先尝试 ``resource.getrusage``（Linux/macOS，无额外依赖），失败后回退
    到 ``psutil``，它是 hermes-agent 的一个可选依赖。
    """
    # Linux / macOS —— resource 是标准库。在 Linux 上 ru_maxrss 的单位是 KB，
    # 在 macOS 上是字节（没错，确实如此）。我们把它当作廉价的“当前”RSS 来用——
    # ru_maxrss 报告的是该进程的高水位值，而这正是泄漏检测真正想要的。
    try:
        import resource

        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return int(maxrss / _BYTES_TO_MB)
        # Linux / 其他 unix：单位是 KB
        return int(maxrss / 1024)
    except Exception:
        pass

    # 回退方案：psutil（Windows，或没有 resource 的特殊 unix）。
    try:
        import psutil  # type: ignore

        rss = psutil.Process(os.getpid()).memory_info().rss
        return int(rss / _BYTES_TO_MB)
    except Exception:
        return None


def log_memory_usage(prefix: str = "") -> None:
    """以对 grep 友好的 ``[MEMORY] ...`` 单行格式记录当前内存使用情况。

    可以在重要生命周期时刻（关闭之后、上下文压缩之后等）从任意线程按需调用，
    都是安全的。

    参数（Parameters）
    ----------
    prefix
        可选的额外标签，插入到 ``[MEMORY]`` 之后——例如
        ``"baseline"``、``"shutdown"``。
    """
    rss = _get_rss_mb()
    uptime = int(time.monotonic() - _start_time) if _start_time else 0
    # gc.get_stats() 返回各代的回收计数；其总和可作为
    # “我们制造了多少垃圾”的一个廉价近似指标。
    try:
        gc_counts = gc.get_count()  # (gen0, gen1, gen2)
    except Exception:
        gc_counts = (0, 0, 0)
    # 线程数在排查线程泄漏时是一个 handy 的关联指标。
    try:
        thread_count = threading.active_count()
    except Exception:
        thread_count = 0

    tag = f"{prefix} " if prefix else ""
    if rss is None:
        logger.info(
            "[MEMORY] %srss=unavailable gc=%s threads=%d uptime=%ds",
            tag,
            gc_counts,
            thread_count,
            uptime,
        )
    else:
        logger.info(
            "[MEMORY] %srss=%dMB gc=%s threads=%d uptime=%ds",
            tag,
            rss,
            gc_counts,
            thread_count,
            uptime,
        )


def _monitor_loop(stop_event: threading.Event, interval: float) -> None:
    """后台线程主体——每隔 ``interval`` 秒记录一次，直到被停止。"""
    while not stop_event.wait(interval):
        try:
            log_memory_usage()
        except Exception as e:
            # 绝不能让监控器把 gateway 搞崩溃；只记录日志然后继续。
            logger.debug("Memory monitor iteration failed: %s", e)


def start_memory_monitoring(interval_seconds: float = 300.0) -> bool:
    """在一个守护线程中启动周期性内存使用日志记录。

    立即记录一次以捕获基线，之后每隔 ``interval_seconds`` 记录一次。
    可以安全地多次调用——当第一个监控器仍在运行时，后续调用都是空操作。

    参数（Parameters）
    ----------
    interval_seconds
        记录频率。默认 300 秒（5 分钟），与上游 cline/cline 实现保持一致。

    返回（Returns）
    -------
    bool
        若启动了一个全新的监控线程则返回 True；若已有监控线程在运行，
        或内存内省不可用，则返回 False。
    """
    global _monitor_thread, _stop_event, _start_time, _interval_seconds

    with _lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return False

        # 先做一次健全性检查，确认我们确实能读取 RSS。如果 resource 和
        # psutil 都不可用，就没必要起一个只能永远记录 "rss=unavailable" 的
        # 线程——发出一次警告然后退出即可。
        if _get_rss_mb() is None:
            logger.warning(
                "[MEMORY] Memory monitoring unavailable: neither resource.getrusage "
                "nor psutil could read process RSS — skipping periodic logging.",
            )
            return False

        _start_time = time.monotonic()
        _interval_seconds = float(interval_seconds)
        _stop_event = threading.Event()

        # 循环开始前的基线快照。
        log_memory_usage(prefix="baseline")

        _monitor_thread = threading.Thread(
            target=_monitor_loop,
            args=(_stop_event, _interval_seconds),
            name="gateway-memory-monitor",
            daemon=True,
        )
        _monitor_thread.start()

        logger.info(
            "[MEMORY] Periodic memory monitoring started (interval: %ds)",
            int(_interval_seconds),
        )
        return True


def stop_memory_monitoring(timeout: float = 2.0) -> None:
    """停止监控线程并记录一次最终快照。

    即使从未调用过 ``start_memory_monitoring()``，调用本函数也是安全的。
    """
    global _monitor_thread, _stop_event

    with _lock:
        if _stop_event is None or _monitor_thread is None:
            return

        # 在拆卸前记录最终快照，从而“最后一个 RSS”总会出现在日志里。
        try:
            log_memory_usage(prefix="shutdown")
        except Exception:
            pass

        _stop_event.set()
        thread = _monitor_thread
        _monitor_thread = None
        _stop_event = None

    # 在锁之外 join，这样即使某次日志调用卡住，也不会让关闭过程死锁。
    try:
        thread.join(timeout=timeout)
    except Exception:
        pass

    logger.info("[MEMORY] Periodic memory monitoring stopped")


def is_running() -> bool:
    """后台监控线程是否仍在运行。"""
    with _lock:
        return _monitor_thread is not None and _monitor_thread.is_alive()
