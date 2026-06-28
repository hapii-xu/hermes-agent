"""跨 agent 的文件状态协调。

防止并发子 agent（同一进程、同一文件系统）触碰同一个文件时出现错乱的编辑。它是对
``run_agent._should_parallelize_tool_batch`` 中单 agent 路径重叠检查的补充 —— 本模块
负责捕获「子 agent B 写入了一个子 agent A 已经读过的文件」这种情况，否则 A 的下一次
写入会用陈旧内容覆盖 B 的改动。

设计
------
一个进程范围的单例 ``FileStateRegistry`` 按解析后的路径跟踪：

  * 每个 agent 的读取戳：{task_id: {path: (mtime, read_ts, partial)}}
  * 全局最后一次写入者：{path: (task_id, write_ts)}
  * 用于「读→改→写」临界区的每路径 ``threading.Lock``

文件工具使用三个公开钩子：

  * ``record_read(task_id, path, *, partial)`` —— 由 read_file 调用
  * ``note_write(task_id, path)`` —— 在 write_file / patch 之后调用
  * ``check_stale(task_id, path)`` —— 在 write_file / patch 之前调用

此外还有 ``lock_path(path)`` —— 一个上下文管理器，返回每路径的锁，用于包裹整个
「读→改→写」块。以及 ``writes_since(task_id, since_ts, paths)``，供 delegate_tool 中
的子 agent 完成提醒使用。

当设置了 ``HERMES_DISABLE_FILE_STATE_GUARD=1`` 时，所有方法都是无操作。

本模块刻意与 ``file_tools.py`` 中的 ``_read_tracker`` 分开 —— 那个跟踪器是按任务
（per-task）的，负责处理「连续读取循环」检测，那是另一个关注点。
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


# ── 公开的戳类型 ────────────────────────────────────────────────
# (mtime, read_ts, partial)。当 read_file 返回的是分窗口视图
# （offset > 1 或 limit < total_lines）时 partial=True —— 在部分读取之后发生的写入
# 仍应告警，以便模型重新完整读取。
ReadStamp = Tuple[float, float, bool]

# 每个 agent 保留的已解析路径条目数量。有上限，以防长会话累积无上限的状态。
# 溢出时按插入顺序丢弃最旧的条目。
_MAX_PATHS_PER_AGENT = 4096

# 全局最后写入者映射的容量上限。同样的策略。
_MAX_GLOBAL_WRITERS = 4096


class FileStateRegistry:
    """跨 agent 文件编辑的进程级协调器。"""

    def __init__(self) -> None:
        self._reads: Dict[str, Dict[str, ReadStamp]] = defaultdict(dict)
        self._last_writer: Dict[str, Tuple[str, float]] = {}
        self._path_locks: Dict[str, threading.Lock] = {}
        self._meta_lock = threading.Lock()  # 保护 _path_locks
        self._state_lock = threading.Lock()  # 保护 _reads + _last_writer

    # ── 路径锁管理 ────────────────────────────────────────────────
    def _lock_for(self, resolved: str) -> threading.Lock:
        with self._meta_lock:
            lock = self._path_locks.get(resolved)
            if lock is None:
                lock = threading.Lock()
                self._path_locks[resolved] = lock
            return lock

    @contextmanager
    def lock_path(self, resolved: str):
        """为「读→改→写」区段获取每路径锁。

        同一进程、同一文件系统 —— 同一路径上的线程会串行化。不同路径则并行推进。
        """
        lock = self._lock_for(resolved)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    # ── 读/写记账 ───────────────────────────────────────────────
    def record_read(
        self,
        task_id: str,
        resolved: str,
        *,
        partial: bool = False,
        mtime: Optional[float] = None,
    ) -> None:
        if _disabled():
            return
        if mtime is None:
            try:
                mtime = os.path.getmtime(resolved)
            except OSError:
                return
        now = time.time()
        with self._state_lock:
            agent_reads = self._reads[task_id]
            agent_reads[resolved] = (float(mtime), now, bool(partial))
            _cap_dict(agent_reads, _MAX_PATHS_PER_AGENT)

    def note_write(
        self,
        task_id: str,
        resolved: str,
        *,
        mtime: Optional[float] = None,
    ) -> None:
        """记录一次成功的写入。

        同时更新全局最后写入者映射以及该 agent 自己的读取戳
        （一次写入隐含一次读取 —— 此刻 agent 已知当前内容）。
        """
        if _disabled():
            return
        if mtime is None:
            try:
                mtime = os.path.getmtime(resolved)
            except OSError:
                return
        now = time.time()
        with self._state_lock:
            self._last_writer[resolved] = (task_id, now)
            _cap_dict(self._last_writer, _MAX_GLOBAL_WRITERS)
            # 写入者自己的视图现在是最新的。
            self._reads[task_id][resolved] = (float(mtime), now, False)
            _cap_dict(self._reads[task_id], _MAX_PATHS_PER_AGENT)

    def check_stale(self, task_id: str, resolved: str) -> Optional[str]:
        """当这次写入会变成陈旧写入时，返回一条面向模型的告警。

        三类陈旧情况，按严重程度排序：

          1. 兄弟子 agent 在本 agent 上次读取之后写入了该文件。
          2. 外部/未知变更（mtime 与我们上次读取时不同）。
          3. agent 从未读取过该文件（只写不读）。

        当写入安全时返回 ``None``。不会抛异常 —— 由调用方决定是阻断还是告警。
        """
        if _disabled():
            return None
        with self._state_lock:
            stamp = self._reads.get(task_id, {}).get(resolved)
            last_writer = self._last_writer.get(resolved)

        # 情况 3：从未读取过，并且我们也没有写入记录 —— 全新文件，或本 agent 首次
        # 触碰。交给已有的 _check_sensitive_path 和文件存在性逻辑去处理；这里没什么
        # 可告警的。
        if stamp is None and last_writer is None:
            return None

        try:
            current_mtime = os.path.getmtime(resolved)
        except OSError:
            # 文件不存在 —— 写入会创建它；不算陈旧。
            return None

        # 情况 1：兄弟子 agent 在我们上次读取之后做了修改。
        if last_writer is not None:
            writer_tid, writer_ts = last_writer
            if writer_tid != task_id:
                if stamp is None:
                    return (
                        f"{resolved} was modified by sibling subagent "
                        f"{writer_tid!r} but this agent never read it. "
                        "Read the file before writing to avoid overwriting "
                        "the sibling's changes."
                    )
                read_ts = stamp[1]
                if writer_ts > read_ts:
                    return (
                        f"{resolved} was modified by sibling subagent "
                        f"{writer_tid!r} at {_fmt_ts(writer_ts)} — after "
                        f"this agent's last read at {_fmt_ts(read_ts)}. "
                        "Re-read the file before writing."
                    )

        # 情况 2：外部/未知修改（mtime 漂移）。
        if stamp is not None:
            read_mtime, _read_ts, partial = stamp
            if current_mtime != read_mtime:
                return (
                    f"{resolved} was modified since you last read it "
                    "on disk (external edit or unrecorded writer). "
                    "Re-read the file before writing."
                )
            if partial:
                return (
                    f"{resolved} was last read with offset/limit pagination "
                    "(partial view). Re-read the whole file before "
                    "overwriting it."
                )

        # 情况 3b：agent 确实从未读取过该文件。
        if stamp is None:
            return (
                f"{resolved} was not read by this agent. "
                "Read the file first so you can write an informed edit."
            )

        return None

    # ── delegate_tool 的提醒辅助 ───────────────────────────────────
    def writes_since(
        self,
        exclude_task_id: str,
        since_ts: float,
        paths: Iterable[str],
    ) -> Dict[str, List[str]]:
        """返回 ``since_ts`` 之后、由 ``exclude_task_id`` 以外的 agent 所做的写入对应的
        ``{writer_task_id: [paths]}``。

        供 delegate_task 在委派结果后追加一条「子 agent 修改了父 agent 之前读过的文件」
        的提醒。
        """
        if _disabled():
            return {}
        paths_set = set(paths)
        out: Dict[str, List[str]] = defaultdict(list)
        with self._state_lock:
            for p, (writer_tid, ts) in self._last_writer.items():
                if writer_tid == exclude_task_id:
                    continue
                if ts < since_ts:
                    continue
                if p in paths_set:
                    out[writer_tid].append(p)
        return dict(out)

    def known_reads(self, task_id: str) -> List[str]:
        """返回该 agent 已读取过的已解析路径列表。"""
        if _disabled():
            return []
        with self._state_lock:
            return list(self._reads.get(task_id, {}).keys())

    # ── 测试钩子 ───────────────────────────────────────────────
    def clear(self) -> None:
        """重置全部状态。仅供测试使用。"""
        with self._state_lock:
            self._reads.clear()
            self._last_writer.clear()
        with self._meta_lock:
            self._path_locks.clear()


# ── 模块级单例 + 辅助函数 ─────────────────────────────────────────
_registry = FileStateRegistry()


def get_registry() -> FileStateRegistry:
    return _registry


def _disabled() -> bool:
    # 每次调用都重新读取，以便测试能通过 monkeypatch.setenv 来切换。
    return os.environ.get("HERMES_DISABLE_FILE_STATE_GUARD", "").strip() == "1"


def _fmt_ts(ts: float) -> str:
    # 用于错误信息的简短相对挂钟时间；避免在热路径上引入 datetime 格式化的开销。
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _cap_dict(d: dict, limit: int) -> None:
    """通过按插入顺序丢弃最旧条目，把 dict 裁剪到 ``limit`` 个条目。"""
    over = len(d) - limit
    if over <= 0:
        return
    # dict 保留插入顺序（PY>=3.7）—— 弹出最旧的键。
    it = iter(d)
    for _ in range(over):
        try:
            d.pop(next(it))
        except (StopIteration, KeyError):
            break


# ── 便捷封装（调用点使用这些短名字）────────────
def record_read(task_id: str, resolved_or_path: str | Path, *, partial: bool = False) -> None:
    _registry.record_read(task_id, str(resolved_or_path), partial=partial)


def note_write(task_id: str, resolved_or_path: str | Path) -> None:
    _registry.note_write(task_id, str(resolved_or_path))


def check_stale(task_id: str, resolved_or_path: str | Path) -> Optional[str]:
    return _registry.check_stale(task_id, str(resolved_or_path))


def lock_path(resolved_or_path: str | Path):
    return _registry.lock_path(str(resolved_or_path))


def writes_since(
    exclude_task_id: str,
    since_ts: float,
    paths: Iterable[str | Path],
) -> Dict[str, List[str]]:
    return _registry.writes_since(exclude_task_id, since_ts, [str(p) for p in paths])


def known_reads(task_id: str) -> List[str]:
    return _registry.known_reads(task_id)


__all__ = [
    "FileStateRegistry",
    "get_registry",
    "record_read",
    "note_write",
    "check_stale",
    "lock_path",
    "writes_since",
    "known_reads",
]
