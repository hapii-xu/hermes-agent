#!/usr/bin/env python3
"""
异步（后台）委派注册表。

支撑 ``delegate_task(background=true)``：父 agent 把一个子 agent 派发到模块级
的守护执行器上运行，并立即返回一个句柄，这样用户和模型可以继续工作，而子任务
在后台运行。

当子任务完成时，会把一个完成事件推入共享的
``process_registry.completion_queue``，其 ``type="async_delegation"``。CLI
（``cli.py`` 的 process_loop）和 gateway（``_run_process_watcher`` /
``completion_queue`` 排空逻辑）在 agent 空闲时已经在轮询该队列，并会根据
每个事件伪造一个新的用户/内部回合。我们刻意复用这条通道，而不是直接
介入正在运行的 agent 循环：

  - 完成事件在 agent 空闲时作为一个新回合浮现，绝不会被插在某个工具结果
    和某条 assistant 消息之间。这保证了严格的消息角色交替仍然合法，并且
    prompt 缓存保持完整（硬性不变量：绝不篡改过往上下文）。
  - 我们免费继承了队列的去重、崩溃恢复检查点，以及现有的 CLI + gateway
    排空接线——不必在这个仓库里最大的两个文件中新增排空循环。

完成负载携带一个丰富、自包含的任务来源信息块（原始目标、父级提供的上下文、
工具集、模型、派发时间、状态，以及完整的结果摘要）。当结果重新进入对话时，
父级可能正沉浸在无关的上下文中，已经不记得当初为什么会有这个子 agent；
这个信息块让它可以选择使用该结果，或者在情况已变化时重新派发。

本模块只负责异步生命周期。真正的子任务构建 + 运行被委派回
``delegate_tool._run_single_child``（通过注入的 runner 完成），因此所有
凭据租赁、心跳、超时和结果塑形逻辑都集中在一处。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.thread import _worker
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class _DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """工作线程不会阻塞进程退出的 ThreadPoolExecutor 变体。

    标准库 ``ThreadPoolExecutor`` 的工作线程是非守护的。后台委派显式属于
    尽力而为的分离式工作，因此一个耗时较长的子任务应当能被
    ``/stop``/关闭操作打断，但绝不能在用户退出后还让一个 CLI 进程一直
    挂起。
    """

    def _adjust_thread_count(self) -> None:
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = "%s_%d" % (self._thread_name_prefix or self, num_threads)
            t = threading.Thread(
                name=thread_name,
                target=_worker,
                args=(
                    weakref.ref(self, weakref_cb),
                    self._work_queue,
                    self._initializer,
                    self._initargs,
                ),
                daemon=True,
            )
            t.start()
            self._threads.add(t)


# ---------------------------------------------------------------------------
# 模块级状态
# ---------------------------------------------------------------------------
# 一个持久的守护执行器（不是 `with ThreadPoolExecutor()` 块，那样会在
# 退出时 join 并完全违背异步的初衷）。工作线程是守护线程，因此硬性的
# 进程退出不会卡在某个进行中的子任务上。
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_executor_max_workers: int = 0

_records_lock = threading.Lock()
# delegation_id -> 记录字典。在本次运行期内以及完成后的短尾期都会保留，
# 以便 `list_async_delegations()` 能展示最近的结果。
_records: Dict[str, Dict[str, Any]] = {}

_DEFAULT_MAX_ASYNC_CHILDREN = 3
# 修剪之前最多保留多少条已完成记录，供状态查询使用。
_MAX_RETAINED_COMPLETED = 50


def _get_executor(max_workers: int) -> ThreadPoolExecutor:
    """惰性地创建（或扩容）共享的守护执行器。

    我们从不缩容——ThreadPoolExecutor 无法改变大小——但如果配置的上限在
    多次调用之间增长了，我们会重建一个更大的池。已经在旧池上运行的进行中
    future 会继续运行，直到旧池被垃圾回收。
    """
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is None or max_workers > _executor_max_workers:
            # 守护线程：thread_name_prefix 有助于在堆栈转储中调试。
            _executor = _DaemonThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="async-delegate",
            )
            _executor_max_workers = max_workers
        return _executor


def active_count() -> int:
    """当前正在运行的异步委派数量。"""
    with _records_lock:
        return sum(1 for r in _records.values() if r.get("status") == "running")


def _new_delegation_id() -> str:
    return f"deleg_{uuid.uuid4().hex[:8]}"


def _prune_completed_locked() -> None:
    """丢弃超出保留上限的最旧的已完成记录。

    调用方必须持有 ``_records_lock``。
    """
    completed = [
        (rid, r)
        for rid, r in _records.items()
        if r.get("status") != "running"
    ]
    if len(completed) <= _MAX_RETAINED_COMPLETED:
        return
    # 按完成时间排序（回退到派发时间），最旧的在前。
    completed.sort(key=lambda kv: kv[1].get("completed_at") or kv[1].get("dispatched_at") or 0)
    for rid, _ in completed[: len(completed) - _MAX_RETAINED_COMPLETED]:
        _records.pop(rid, None)


def dispatch_async_delegation(
    *,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    runner: Callable[[], Dict[str, Any]],
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
) -> Dict[str, Any]:
    """在守护执行器上启动 ``runner`` 并立即返回一个句柄。

    参数
    ----------
    goal, context, toolsets, role, model
        派发时的任务规格，原样捕获进丰富的完成信息块。
    session_key
        gateway 的 session_key（取自 ``tools.approval.get_current_session_key``），
        必须在派发之前于父线程中捕获，因为守护工作线程不会携带该 contextvar。
        用于把完成事件路由回发起会话。
    runner
        无参可调用对象，负责构建并运行子任务，返回与
        ``_run_single_child`` 产出的相同结果字典。在工作线程上运行。
    interrupt_fn
        可选的可调用对象，用于通知子任务停止（在关闭/显式取消时使用）。
    max_async_children
        并发上限。达到上限时派发会被拒绝（调用方应回退到同步或告知
        用户），而不是排队，因此一个失控的模型无法堆积无上限的后台工作。

    返回
    -------
    dict
        成功时为 ``{"status": "dispatched", "delegation_id": ...}``，
        达到上限时为 ``{"status": "rejected", "error": ...}``。
    """
    delegation_id = _new_delegation_id()
    dispatched_at = time.time()
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": goal,
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
    }
    # 容量检查与记录插入在同一个锁持有期内完成——分开检查 active_count()
    # 会让两次并发派发（例如来自不同 gateway 会话）同时通过检查并超过上限。
    with _records_lock:
        running = sum(
            1 for r in _records.values() if r.get("status") == "running"
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or run this task synchronously "
                    f"(background=false). Raise delegation.max_async_children in "
                    f"config.yaml to allow more concurrent background subagents."
                ),
            }
        _records[delegation_id] = record

    executor = _get_executor(max_async_children)

    def _worker() -> None:
        result: Dict[str, Any] = {}
        status = "error"
        try:
            result = runner() or {}
            status = result.get("status") or "completed"
        except Exception as exc:  # noqa: BLE001 — 绝不能让工作线程崩溃
            logger.exception("Async delegation %s crashed", delegation_id)
            result = {
                "status": "error",
                "summary": None,
                "error": f"{type(exc).__name__}: {exc}",
                "api_calls": 0,
                "duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize(delegation_id, result, status)

    try:
        executor.submit(_worker)
    except Exception as exc:  # pragma: no cover — 池提交失败很罕见
        with _records_lock:
            _records.pop(delegation_id, None)
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation: {exc}",
        }

    logger.info(
        "Dispatched async delegation %s (session_key=%s): %s",
        delegation_id, session_key or "<cli>", (goal or "")[:80],
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _finalize(delegation_id: str, result: Dict[str, Any], status: str) -> None:
    """将一条记录标记为完成，并把完成事件推入队列。"""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return
        record["status"] = status
        record["completed_at"] = time.time()
        record["interrupt_fn"] = None  # 丢弃闭包；子任务已结束
        # 在持锁期间快照事件所需的字段。
        event_record = dict(record)
        _prune_completed_locked()

    _push_completion_event(event_record, result, status)


def _push_completion_event(
    record: Dict[str, Any], result: Dict[str, Any], status: str
) -> None:
    """把一个 type='async_delegation' 的事件推入共享的完成队列。

    尽力而为：此处失败绝不能让工作线程崩溃，但这意味着结果会静默丢失，
    因此我们以高音量记录日志。
    """
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s finished but process_registry import failed; "
            "result lost: %s",
            record.get("delegation_id"), exc,
        )
        return

    summary = result.get("summary")
    error = result.get("error")
    dispatched_at = record.get("dispatched_at") or time.time()
    completed_at = record.get("completed_at") or time.time()

    evt = {
        "type": "async_delegation",
        "delegation_id": record.get("delegation_id"),
        # session_key 用于把完成事件路由回发起的 gateway 会话；
        # 空字符串 => CLI（单会话）路径。
        "session_key": record.get("session_key", ""),
        "goal": record.get("goal", ""),
        "context": record.get("context"),
        "toolsets": record.get("toolsets"),
        "role": record.get("role"),
        "model": result.get("model") or record.get("model"),
        "status": status,
        "summary": summary,
        "error": error,
        "api_calls": result.get("api_calls", 0),
        "duration_seconds": result.get(
            "duration_seconds", round(completed_at - dispatched_at, 2)
        ),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
        "exit_reason": result.get("exit_reason"),
    }
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s: failed to enqueue completion event; "
            "result lost: %s",
            record.get("delegation_id"), exc,
        )


def dispatch_async_delegation_batch(
    *,
    goals: List[str],
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    runner: Callable[[], Dict[str, Any]],
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
) -> Dict[str, Any]:
    """把一整个扇出批次作为单个后台单元派发。

    与 ``dispatch_async_delegation``（支撑单个子 agent）不同，此处的
    ``runner`` 运行整个批次——它并行地构建并 join 每个子任务，并返回同步
    路径本会返回的组合结果 ``{"results": [...], "total_duration_seconds": N}``
    字典。整个批次只占用一个异步槽位（批内并行度由
    ``max_concurrent_children`` 单独约束），因此单次 ``delegate_task`` 扇出
    不会自行耗尽异步池。

    当批次完成时，会把单个完成事件推入共享的
    ``process_registry.completion_queue``，携带完整的逐任务 ``results``
    列表，因此汇总摘要在每个子任务完成后会作为一条消息重新进入对话——
    对话绝不会在它们运行期间被阻塞。

    成功时返回 ``{"status": "dispatched", "delegation_id": ...}``，当异步
    池达到上限时返回 ``{"status": "rejected", "error": ...}``。
    """
    delegation_id = _new_delegation_id()
    dispatched_at = time.time()
    n = len(goals)
    # 用于状态列表/完成事件头的合并目标标签。
    combined_goal = (
        goals[0] if n == 1 else f"{n} parallel subagents: " + "; ".join(g[:40] for g in goals)
    )
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": combined_goal,
        "goals": list(goals),
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
        "is_batch": True,
    }
    with _records_lock:
        running = sum(
            1 for r in _records.values() if r.get("status") == "running"
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or raise delegation.max_async_children in "
                    f"config.yaml to allow more concurrent background units."
                ),
            }
        _records[delegation_id] = record

    executor = _get_executor(max_async_children)

    def _worker() -> None:
        combined: Dict[str, Any] = {}
        status = "error"
        try:
            combined = runner() or {}
            # 批次状态：除非每个子任务都出错/被中断，否则为 completed。
            child_results = combined.get("results") or []
            if child_results and all(
                (r.get("status") not in ("completed", "success"))
                for r in child_results
            ):
                status = "error"
            else:
                status = "completed"
        except Exception as exc:  # noqa: BLE001 — 绝不能让工作线程崩溃
            logger.exception("Async delegation batch %s crashed", delegation_id)
            combined = {
                "results": [],
                "error": f"{type(exc).__name__}: {exc}",
                "total_duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize_batch(delegation_id, combined, status)

    try:
        executor.submit(_worker)
    except Exception as exc:  # pragma: no cover
        with _records_lock:
            _records.pop(delegation_id, None)
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation batch: {exc}",
        }

    logger.info(
        "Dispatched async delegation batch %s (%d task(s), session_key=%s)",
        delegation_id, n, session_key or "<cli>",
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _finalize_batch(
    delegation_id: str, combined: Dict[str, Any], status: str
) -> None:
    """将一条批次记录标记为完成，并推送单个合并的完成事件。"""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return
        record["status"] = status
        record["completed_at"] = time.time()
        record["interrupt_fn"] = None
        event_record = dict(record)
        _prune_completed_locked()

    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation batch %s finished but process_registry import "
            "failed; result lost: %s",
            delegation_id, exc,
        )
        return

    dispatched_at = event_record.get("dispatched_at") or time.time()
    completed_at = event_record.get("completed_at") or time.time()
    evt = {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_key": event_record.get("session_key", ""),
        "goal": event_record.get("goal", ""),
        "goals": event_record.get("goals"),
        "context": event_record.get("context"),
        "toolsets": event_record.get("toolsets"),
        "role": event_record.get("role"),
        "model": event_record.get("model"),
        "status": status,
        "is_batch": True,
        # 完整的逐任务结果列表——格式化器据此渲染一个合并的多任务信息块。
        "results": combined.get("results") or [],
        "error": combined.get("error"),
        "total_duration_seconds": combined.get("total_duration_seconds"),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
    }
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation batch %s: failed to enqueue completion event; "
            "result lost: %s",
            delegation_id, exc,
        )


def list_async_delegations() -> List[Dict[str, Any]]:
    """异步委派的快照（运行中 + 最近完成的）。

    可从任意线程安全调用。会排除不可序列化的 interrupt_fn。
    """
    with _records_lock:
        return [
            {k: v for k, v in r.items() if k != "interrupt_fn"}
            for r in _records.values()
        ]


def interrupt_all(reason: str = "shutdown") -> int:
    """通知每个正在运行的异步委派停止。返回通知的数量。

    在 ``/stop`` 和 gateway 关闭时使用，以免某个悬空的后台子 agent 在无人
    监听的情况下继续消耗 token。子任务仍会通过正常的 finalize 路径发出
    一个完成事件（status='interrupted'）。
    """
    count = 0
    with _records_lock:
        targets = [
            r for r in _records.values() if r.get("status") == "running"
        ]
    for r in targets:
        fn = r.get("interrupt_fn")
        if callable(fn):
            try:
                fn()
                count += 1
            except Exception as exc:
                logger.debug(
                    "interrupt_all: %s interrupt failed: %s",
                    r.get("delegation_id"), exc,
                )
    if count:
        logger.info("Interrupted %d async delegation(s) (%s)", count, reason)
    return count


def _reset_for_tests() -> None:
    """仅用于测试：清空所有状态并销毁执行器。"""
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
        _executor = None
        _executor_max_workers = 0
    with _records_lock:
        _records.clear()
