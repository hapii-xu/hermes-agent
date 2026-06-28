"""每个 agent 的迭代预算 — 线程安全的消费/退还计数器。

从 ``run_agent.py`` 中提取。每个 ``AIAgent`` 实例（父 agent 或
子 agent）持有一个 :class:`IterationBudget`；父 agent 的上限来自
``max_iterations``（默认 90），每个子 agent 的上限来自
``delegation.max_iterations``（默认 50）。

``run_agent`` 重新导出 ``IterationBudget``，以便已有的
``from run_agent import IterationBudget`` 导入保持不变。
"""

from __future__ import annotations

import threading


class IterationBudget:
    """agent 的线程安全迭代计数器。

    每个 agent（父 agent 或子 agent）都有自己的 ``IterationBudget``。
    父 agent 的预算上限为 ``max_iterations``（默认 90）。
    每个子 agent 获得独立的预算，上限为
    ``delegation.max_iterations``（默认 50）— 这意味着父 agent
    加子 agent 的总迭代次数可能超过父 agent 的上限。
    用户通过 config.yaml 中的 ``delegation.max_iterations`` 控制
    每个子 agent 的限制。

    ``execute_code``（编程式工具调用）的迭代通过 :meth:`refund` 退还，
    因此不会消耗预算。
    """

    def __init__(self, max_total: int):
        self.max_total = max_total
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """尝试消费一次迭代。允许则返回 True。"""
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
        """退还一次迭代（例如用于 execute_code 轮次）。"""
        with self._lock:
            if self._used > 0:
                self._used -= 1

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)


__all__ = ["IterationBudget"]
