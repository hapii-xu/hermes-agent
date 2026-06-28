"""GatewayRunner 的 Kanban 看板监视器方法。

逐字从 ``gateway/run.py`` 中提取（巨文件拆解第 3 阶段）。这些是后台循环
方法，用于订阅 kanban 看板、投递通知/产物，并驱动多 agent 调度器。它们只
使用 ``self`` 状态，因此放在 ``GatewayRunner`` 继承的一个 mixin 上 ——
``self._kanban_*`` 调用点通过 MRO 解析的结果完全相同，使这次移动对行为
没有影响，同时把约 1,000 行代码从 run.py 中移出。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Optional

# 匹配 run.py 使用的 logger（logging.getLogger(__name__)，其中 __name__ ==
# "gateway.run"），使提取出的日志记录保留原始 logger 名称。
logger = logging.getLogger("gateway.run")


def _resolve_auto_decompose_settings(
    load_config: Callable[[], Any],
) -> "tuple[bool, int]":
    """解析实时的（enabled, per_tick）auto-decompose 设置。

    在每个调度器 tick 上从 config 重新读取（#49638），这样把
    ``kanban.auto_decompose: false`` 改成关闭失控的扇出就能在下一个 tick 生效，
    而不需要重启 gateway。auto-decompose 是一个安全开关 —— 用户看到它创建并
    启动了他们不想要的任务时，会用这个标志来停止它；而过时的启动时捕获值
    静默忽略该修改正是 #49638 报告的 bug。

    **故障安全**：如果读取 config 抛异常，返回 ``(False, 3)`` —— 一次瞬态
    读取错误绝不能重新启用用户关闭的功能，也不能回退到容易爆发的默认开启
    行为。``per_tick`` 被钳制到 ``>= 1``。
    """
    try:
        cfg = load_config()
    except Exception:
        return False, 3
    kcfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    enabled = bool(kcfg.get("auto_decompose", True))
    try:
        per_tick = int(kcfg.get("auto_decompose_per_tick", 3) or 3)
    except (TypeError, ValueError):
        per_tick = 3
    if per_tick < 1:
        per_tick = 1
    return enabled, per_tick


def _acquire_singleton_lock(lock_path) -> "tuple[Optional[object], str]":
    """为唯一的调度器获取一个独占的、非阻塞的劝告锁。

    全机范围内只允许一个 gateway 进程运行内嵌的 kanban 调度器：并发的调度器
    会让回收频率翻倍（各自运行自己的 ``release_stale_claims`` → promote →
    dispatch 循环），让事件日志中的 claim-attempt 事件翻倍，而且 —— 在
    ``wal_autocheckpoint=0`` 时 —— 并发的手动 WAL checkpoint 会损坏索引页。
    ``dispatch_in_gateway`` 配置标志是主控制；这把锁是能在配置漂移和同 profile
    重启竞态下存活的兜底。

    委托给 :func:`gateway.status._try_acquire_file_lock`（POSIX 上用
    ``fcntl``，Windows 上用 ``msvcrt``），使该守卫跨平台。

    成功时返回 ``(handle, "held")`` —— 调用方在进程生命周期内持有文件句柄，
    并**必须**在完成后通过 :func:`_release_singleton_lock` 释放它。
    当另一个进程持有锁时返回 ``(None, "contended")``（调用方绝不能调度）。
    当无法执行锁定时（无 flock 的非 POSIX 文件系统，或 status.py 辅助函数
    无法导入）返回 ``(None, "unavailable")`` —— 调用方回退到仅靠配置控制。
    """
    try:
        from gateway.status import _try_acquire_file_lock  # 延迟导入；同一包
    except ImportError:
        return None, "unavailable"
    try:
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(lock_path), "a+", encoding="utf-8")
    except OSError:
        return None, "unavailable"
    if not _try_acquire_file_lock(handle):
        handle.close()
        return None, "contended"
    return handle, "held"


def _release_singleton_lock(handle) -> None:
    """释放通过 :func:`_acquire_singleton_lock` 获取的调度器单例锁。"""
    if handle is None:
        return
    try:
        from gateway.status import _release_file_lock
        _release_file_lock(handle)
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass


class GatewayKanbanWatchersMixin:
    """GatewayRunner 的 kanban 监视器 / 通知器 / 调度器循环。"""

    async def _kanban_notifier_watcher(self, interval: float = 5.0) -> None:
        """轮询 ``kanban_notify_subs`` 并向用户投递终态事件。

        对每个订阅行，取出比存储的游标更新的、kind 属于终态集合
        （``completed``、``blocked``、``gave_up``、``crashed``、
        ``timed_out``）的 ``task_events``。每个新事件向
        ``(platform, chat_id, thread_id)`` 发送一条消息，然后推进游标。
        当任务到达终态（``completed`` / ``archived``）时，移除该订阅。

        在 gateway 事件循环中运行；所有 SQLite 工作通过
        ``asyncio.to_thread`` 推到线程中，使循环永不阻塞在 WAL 锁上。一个
        tick 中的失败不会阻止后续 tick。

        **多看板：** 每个 tick 遍历磁盘上发现的每个看板。订阅存在于每个看板
        自己的 DB 内，不能跨看板，因此投递语义不变 —— 这纯粹是单 DB 轮询的
        扇出。
        """
        # 门控：只有拥有调度权的 gateway 才打开 kanban DB 进行通知器轮询。
        # 非调度 gateway 没有要投递的订阅 —— 所有 kanban 状态都在调度所有者
        # 的按看板 DB 中。这防止了 N 个 gateway 的 -shm 竞争。
        # TODO: 等按看板的 dispatcher_owner 跟踪落地后再按看板门控。
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban notifier: config loader unavailable; disabled")
            return
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban notifier: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY env")
            return
        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban notifier: cannot load config (%s); disabled", exc)
            return
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info(
                "kanban notifier: disabled via config kanban.dispatch_in_gateway=false"
            )
            return
        from gateway.config import Platform as _Platform
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban notifier: kanban_db not importable; notifier disabled")
            return

        TERMINAL_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out")
        # 仅当任务到达真正终态（done / archived）时才移除订阅。我们过去也会
        # 在任何终态事件 kind（gave_up / crashed / timed_out / blocked）时
        # 取消订阅，但这会在调度器重新生成任务时悄悄把用户踢出循环：一个
        # 崩溃、被回收、再次运行、又再次崩溃的 worker 只会在第一次崩溃时
        # 通知，因为订阅在第一次事件后就删除了。这与 PR #22941 为 `blocked`
        # 修复的 unblock 后再 block 的循环形态相同。让订阅一直保留到任务
        # 真正完成，能让游标（由 claim_unseen_events_for_sub 原子推进）处理
        # 去重，且任何重试循环事件都能到达用户。
        # 按订阅的发送失败计数器。Adapter.send 抛异常意味着聊天已死
        # （被删除、bot 被踢等）—— 连续 N 次发送失败后丢弃该订阅，以免
        # 每 5 秒永远对着一个死掉的聊天空转。
        MAX_SEND_FAILURES = 3
        sub_fail_counts: dict[tuple, int] = getattr(
            self, "_kanban_sub_fail_counts", {}
        )
        self._kanban_sub_fail_counts = sub_fail_counts
        notifier_profile = getattr(self, "_kanban_notifier_profile", None)
        if not notifier_profile:
            notifier_profile = self._active_profile_name()
            self._kanban_notifier_profile = notifier_profile

        # 初始延迟，让 gateway 完成适配器的接线。
        await asyncio.sleep(5)

        while self._running:
            try:
                def _collect():
                    deliveries: list[dict] = []
                    active_platforms = {
                        getattr(platform, "value", str(platform)).lower()
                        for platform in self.adapters.keys()
                    }
                    if not active_platforms:
                        logger.debug("kanban notifier: no connected adapters; skipping tick")
                        return deliveries

                    # 枚举磁盘上的每个看板，但每个解析后的 DB 路径只轮询一次。
                    # 当 HERMES_KANBAN_DB 固定看板路径时，多个 slug 可能指向
                    # 同一个 DB；没有这个守卫，一个 gateway 在推进游标之前可能
                    # 多次收集同一个订阅/事件。
                    try:
                        boards = _kb.list_boards(include_archived=False)
                    except Exception:
                        boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
                    seen_db_paths: set[str] = set()
                    for board_meta in boards:
                        slug = board_meta.get("slug") or _kb.DEFAULT_BOARD
                        db_path = board_meta.get("db_path")
                        try:
                            resolved_db_path = str(Path(db_path).expanduser().resolve()) if db_path else str(_kb.kanban_db_path(slug).resolve())
                        except Exception:
                            resolved_db_path = f"slug:{slug}"
                        if resolved_db_path in seen_db_paths:
                            logger.debug(
                                "kanban notifier: skipping duplicate board slug %s for DB %s",
                                slug, resolved_db_path,
                            )
                            continue
                        seen_db_paths.add(resolved_db_path)
                        try:
                            conn = _kb.connect(board=slug)
                        except Exception as exc:
                            logger.debug("kanban notifier: cannot open board %s: %s", slug, exc)
                            continue
                        try:
                            # `connect()` 在每个进程首次打开时运行 schema +
                            # 幂等迁移，所以这里显式调用 `init_db()` 是冗余的。
                            # 更糟的是：`init_db()` 会故意打破进程内缓存，并在
                            # *第二个* 连接上重新运行迁移，与第一个连接产生
                            # 竞态，过去每次 gateway 针对遗留 DB 启动时都会
                            # 记录一条无害但吵闹的 `duplicate column name`
                            # 回溯（以及间歇性的 "database is locked"
                            # —— issue #21378）。`_add_column_if_missing` 现在
                            # 容忍该竞态，但我们仍跳过冗余调用以避免浪费工作。
                            subs = _kb.list_notify_subs(conn)
                            if not subs:
                                logger.debug("kanban notifier: board %s has no subscriptions", slug)
                            for sub in subs:
                                owner_profile = sub.get("notifier_profile") or None
                                if owner_profile and owner_profile != notifier_profile:
                                    logger.debug(
                                        "kanban notifier: subscription for %s owned by profile %s; current profile %s skipping",
                                        sub.get("task_id"), owner_profile, notifier_profile,
                                    )
                                    continue
                                platform = (sub.get("platform") or "").lower()
                                if platform not in active_platforms:
                                    logger.debug(
                                        "kanban notifier: subscription for %s on %s skipped; adapter not connected",
                                        sub.get("task_id"), platform or "<missing>",
                                    )
                                    continue
                                old_cursor, cursor, events = _kb.claim_unseen_events_for_sub(
                                    conn,
                                    task_id=sub["task_id"],
                                    platform=sub["platform"],
                                    chat_id=sub["chat_id"],
                                    thread_id=sub.get("thread_id") or "",
                                    kinds=TERMINAL_KINDS,
                                )
                                if not events:
                                    continue
                                task = _kb.get_task(conn, sub["task_id"])
                                logger.debug(
                                    "kanban notifier: claimed %d event(s) for %s on board %s cursor %s→%s",
                                    len(events), sub["task_id"], slug, old_cursor, cursor,
                                )
                                deliveries.append({
                                    "sub": sub,
                                    "old_cursor": old_cursor,
                                    "cursor": cursor,
                                    "events": events,
                                    "task": task,
                                    "board": slug,
                                })
                        finally:
                            conn.close()
                    return deliveries

                deliveries = await asyncio.to_thread(_collect)
                for d in deliveries:
                    sub = d["sub"]
                    task = d["task"]
                    board_slug = d.get("board")
                    platform_str = (sub["platform"] or "").lower()
                    try:
                        plat = _Platform(platform_str)
                    except ValueError:
                        # 未知的 platform 字符串；跳过并推进游标，以免永远重放。
                        await asyncio.to_thread(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        continue
                    adapter = self.adapters.get(plat)
                    if adapter is None:
                        logger.debug(
                            "kanban notifier: adapter %s disconnected before delivery for %s; rewinding claim",
                            platform_str, sub["task_id"],
                        )
                        await asyncio.to_thread(
                            self._kanban_rewind,
                            sub,
                            d["cursor"],
                            d.get("old_cursor", 0),
                            board_slug,
                        )
                        continue
                    title = (task.title if task else sub["task_id"])[:120]
                    for ev in d["events"]:
                        kind = ev.kind
                        # 身份前缀：把终态提醒归因于完成工作的 worker。
                        # 让舰队（一个聊天订阅多个任务）一目了然。
                        who = (task.assignee if task and task.assignee else None)
                        tag = f"@{who} " if who else ""
                        if kind == "completed":
                            # 优先用 run 的 summary（worker 有意为之的、面向
                            # 人类的交接，承载在事件载荷中），然后回退到
                            # task.result，以兼容 run 上线之前写入的旧行。
                            handoff = ""
                            payload_summary = None
                            if ev.payload and ev.payload.get("summary"):
                                payload_summary = str(ev.payload["summary"])
                            if payload_summary:
                                lines = payload_summary.strip().splitlines()
                                h = lines[0][:200] if lines else payload_summary[:200]
                                handoff = f"\n{h}"
                            elif task and task.result:
                                lines = task.result.strip().splitlines()
                                r = lines[0][:160] if lines else task.result[:160]
                                handoff = f"\n{r}"
                            msg = (
                                f"✔ {tag}Kanban {sub['task_id']} done"
                                f" — {title}{handoff}"
                            )
                        elif kind == "blocked":
                            reason = ""
                            if ev.payload and ev.payload.get("reason"):
                                reason = f": {str(ev.payload['reason'])[:160]}"
                            msg = f"⏸ {tag}Kanban {sub['task_id']} blocked{reason}"
                        elif kind == "gave_up":
                            err = ""
                            if ev.payload and ev.payload.get("error"):
                                err = f"\n{str(ev.payload['error'])[:200]}"
                            msg = (
                                f"✖ {tag}Kanban {sub['task_id']} gave up "
                                f"after repeated spawn failures{err}"
                            )
                        elif kind == "crashed":
                            msg = (
                                f"✖ {tag}Kanban {sub['task_id']} worker crashed "
                                f"(pid gone); dispatcher will retry"
                            )
                        elif kind == "timed_out":
                            limit = 0
                            if ev.payload and ev.payload.get("limit_seconds"):
                                limit = int(ev.payload["limit_seconds"])
                            msg = (
                                f"⏱ {tag}Kanban {sub['task_id']} timed out "
                                f"(max_runtime={limit}s); will retry"
                            )
                        else:
                            continue
                        metadata: dict[str, Any] = {}
                        if sub.get("thread_id"):
                            metadata["thread_id"] = sub["thread_id"]
                        sub_key = (
                            sub["task_id"], sub["platform"],
                            sub["chat_id"], sub.get("thread_id") or "",
                        )
                        try:
                            await adapter.send(
                                sub["chat_id"], msg, metadata=metadata,
                            )
                            logger.debug(
                                "kanban notifier: delivered %s event for %s to %s/%s on board %s",
                                kind, sub["task_id"], platform_str, sub["chat_id"], board_slug,
                            )
                            # 投递文本通知后，把 worker 在
                            # ``kanban_complete(summary=..., artifacts=[...])``
                            # 中引用（或旧版 ``result`` 字段）的任何产物路径
                            # 作为原生上传呈现。``extract_local_files`` 在
                            # summary 中查找裸的绝对路径；
                            # ``send_document`` / ``send_image_file`` 上传它们。
                            # 仅在 ``completed`` 事件时触发，以免在重试时刷附件。
                            if kind == "completed":
                                try:
                                    await self._deliver_kanban_artifacts(
                                        adapter=adapter,
                                        chat_id=sub["chat_id"],
                                        metadata=metadata,
                                        event_payload=getattr(ev, "payload", None),
                                        task=task,
                                    )
                                except Exception as art_exc:
                                    logger.debug(
                                        "kanban notifier: artifact delivery for %s failed: %s",
                                        sub["task_id"], art_exc,
                                    )
                            # 成功时重置失败计数器。
                            sub_fail_counts.pop(sub_key, None)
                        except Exception as exc:
                            fails = sub_fail_counts.get(sub_key, 0) + 1
                            sub_fail_counts[sub_key] = fails
                            logger.warning(
                                "kanban notifier: send failed for %s on %s "
                                "(attempt %d/%d): %s",
                                sub["task_id"], platform_str, fails,
                                MAX_SEND_FAILURES, exc,
                            )
                            if fails >= MAX_SEND_FAILURES:
                                logger.warning(
                                    "kanban notifier: dropping subscription "
                                    "%s on %s after %d consecutive send failures",
                                    sub["task_id"], platform_str, fails,
                                )
                                await asyncio.to_thread(self._kanban_unsub, sub, board_slug)
                                sub_fail_counts.pop(sub_key, None)
                            else:
                                await asyncio.to_thread(
                                    self._kanban_rewind,
                                    sub,
                                    d["cursor"],
                                    d.get("old_cursor", 0),
                                    board_slug,
                                )
                            # 瞬态失败时回退发送前的 claim，以便后续 tick 可以
                            # 重试。失败次数过多后，丢弃订阅是终态动作。
                            break
                    else:
                        # 所有事件都已投递；推进游标。游标是去重机制 —— 它
                        # 防止在后续 tick 上重复投递同一事件。
                        await asyncio.to_thread(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        # 仅当任务到达真正终态（done / archived）时才取消订阅。
                        # 对于 blocked / gave_up / crashed / timed_out，保留订阅，
                        # 以便调度器重新生成任务并循环进入相同状态时用户能再次
                        # 收到通知。关于这所防止的失败模式，参见上面
                        # TERMINAL_KINDS 处更长的注释。
                        task_terminal = task and task.status in {"done", "archived"}
                        if task_terminal:
                            await asyncio.to_thread(
                                self._kanban_unsub, sub, board_slug,
                            )
            except Exception as exc:
                logger.warning("kanban notifier tick failed: %s", exc)
            # 带取消检查地睡眠。
            for _ in range(int(max(1, interval))):
                if not self._running:
                    return
                await asyncio.sleep(1)

    def _kanban_advance(
        self, sub: dict, cursor: int, board: Optional[str] = None,
    ) -> None:
        """同步辅助函数：推进订阅的游标。在 to_thread 中运行。

        ``board`` 把 DB 连接范围限定为拥有此订阅的看板。一个看板里的取消
        订阅游标不能触及另一个看板。
        """
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.advance_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                new_cursor=cursor,
            )
        finally:
            conn.close()

    def _kanban_unsub(self, sub: dict, board: Optional[str] = None) -> None:
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.remove_notify_sub(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
            )
        finally:
            conn.close()

    def _kanban_rewind(
        self,
        sub: dict,
        claimed_cursor: int,
        old_cursor: int,
        board: Optional[str] = None,
    ) -> None:
        """同步辅助函数：在发送失败后撤销已 claim 的通知游标。"""
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.rewind_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                claimed_cursor=claimed_cursor,
                old_cursor=old_cursor,
            )
        finally:
            conn.close()

    async def _deliver_kanban_artifacts(
        self,
        *,
        adapter,
        chat_id: str,
        metadata: dict,
        event_payload: Optional[dict],
        task,
    ) -> None:
        """上传已完成 kanban 任务所引用的产物文件。

        传入 ``kanban_complete(artifacts=[...])`` 的 worker 通过完成事件发送
        绝对文件路径，以便下游的人把交付物作为原生上传获取，而不是聊天里打印
        的路径。

        按优先级扫描的来源：
          1. ``event_payload['artifacts']``（显式列表 —— 首选）
          2. ``event_payload['summary']``（截断后的首行）
          3. ``task.result``（旧版回退）

        文件会去重，缺失的文件会被静默跳过（该路径可能只是被提及用于参考），
        投递错误会被记录但不会中断通知器循环。
        """
        from pathlib import Path as _Path

        candidates: list[str] = []
        seen: set[str] = set()

        def _add(path: str) -> None:
            if not path:
                return
            expanded = os.path.expanduser(path)
            if expanded in seen:
                return
            if not os.path.isfile(expanded):
                return
            seen.add(expanded)
            candidates.append(expanded)

        # 1. 载荷中显式的 artifacts 列表。
        if isinstance(event_payload, dict):
            raw = event_payload.get("artifacts")
            if isinstance(raw, (list, tuple)):
                for item in raw:
                    if isinstance(item, str):
                        _add(item)

            # 2. 嵌入载荷 summary 中的路径。
            summary = event_payload.get("summary")
            if isinstance(summary, str) and summary:
                paths, _ = adapter.extract_local_files(summary)
                for p in paths:
                    _add(p)

        # 3. 旧版：嵌入 task.result 中的路径。
        if task is not None and getattr(task, "result", None):
            result_text = str(task.result)
            paths, _ = adapter.extract_local_files(result_text)
            for p in paths:
                _add(p)

        if not candidates:
            return

        from gateway.platforms.base import BasePlatformAdapter
        candidates = BasePlatformAdapter.filter_local_delivery_paths(candidates)
        if not candidates:
            return

        _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}

        from urllib.parse import quote as _quote

        # 把图片分区，让它们在支持批量图片上传的平台（Signal/Slack RPC）上
        # 走单次 send_multiple_images 调用。
        image_paths = [p for p in candidates if _Path(p).suffix.lower() in _IMAGE_EXTS]
        other_paths = [p for p in candidates if _Path(p).suffix.lower() not in _IMAGE_EXTS]

        if image_paths:
            try:
                batch = [(f"file://{_quote(p)}", "") for p in image_paths]
                await adapter.send_multiple_images(
                    chat_id=chat_id, images=batch, metadata=metadata,
                )
            except Exception as exc:
                logger.warning(
                    "kanban notifier: image batch upload failed: %s", exc,
                )

        for path in other_paths:
            ext = _Path(path).suffix.lower()
            try:
                if ext in _VIDEO_EXTS:
                    await adapter.send_video(
                        chat_id=chat_id, video_path=path, metadata=metadata,
                    )
                else:
                    await adapter.send_document(
                        chat_id=chat_id, file_path=path, metadata=metadata,
                    )
            except Exception as exc:
                logger.warning(
                    "kanban notifier: artifact upload (%s) failed: %s",
                    path, exc,
                )

    async def _kanban_dispatcher_watcher(self) -> None:
        """内嵌的 kanban 调度器 —— 每 `dispatch_interval_seconds` 一个 tick。

        由 config.yaml 中的 `kanban.dispatch_in_gateway` 门控（默认 True）。
        为 True 时，gateway 承载该 profile 的唯一调度器：不需要单独的
        `hermes kanban daemon` 进程。为 False 时，循环立即退出，预期会有外部
        daemon。

        每个 tick 在 ``asyncio.to_thread`` 内调用
        :func:`kanban_db.dispatch_once`，使 SQLite WAL 锁永不阻塞事件循环。
        一个 tick 中的失败不会阻止后续 tick —— 与 `_kanban_notifier_watcher`
        相同的模式。

        关闭：循环在 tick 之间检查 ``self._running``；gateway 的 stop() 把它
        翻转为 False 并取消待处理任务，而进行中的 ``to_thread`` 在当前的
        ``dispatch_once`` 调用完成后自行返回（在空闲看板上通常 <1ms）。
        """
        # 启动时读取一次 config。如果用户之后翻转标志，他们重启 gateway；
        # 与此处其他后台监视器相同的模式。遵循 HERMES_KANBAN_DISPATCH_IN_GATEWAY
        # 环境变量作为逃生舱（假值无需编辑 YAML 即可禁用）。
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban dispatcher: config loader unavailable; disabled")
            return
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban dispatcher: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY env")
            return

        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban dispatcher: cannot load config (%s); disabled", exc)
            return
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info(
                "kanban dispatcher: disabled via config kanban.dispatch_in_gateway=false"
            )
            return

        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban dispatcher: kanban_db not importable; dispatcher disabled")
            return

        # 单调度器兜底。dispatch_in_gateway 默认为 true，所以一个新 profile 的
        # gateway（或同 profile 的重启竞态）可能悄悄启动第二个调度器；并发调度器
        # 会让回收频率翻倍、让 claim-attempt 事件翻倍，而且 —— 在
        # wal_autocheckpoint=0 时 —— 并发的手动 WAL checkpoint 会损坏索引页。
        # 该锁位于机器全局的 kanban 根目录（设计上跨 profile 共享），因此它会
        # 串行化所有 gateway。
        self._kanban_dispatcher_lock_handle = None
        _lock_path = _kb.kanban_home() / "kanban" / ".dispatcher.lock"
        _lock_handle, _lock_state = _acquire_singleton_lock(_lock_path)
        if _lock_state == "contended":
            logger.info(
                "kanban dispatcher: another gateway already holds the dispatcher "
                "lock (%s); this gateway will NOT dispatch.", _lock_path,
            )
            return
        if _lock_state == "held":
            self._kanban_dispatcher_lock_handle = _lock_handle  # 在进程生命周期内持有
            logger.info("kanban dispatcher: holding singleton dispatcher lock (%s)", _lock_path)
        else:
            logger.warning(
                "kanban dispatcher: advisory lock unavailable at %s; proceeding "
                "on config control alone.", _lock_path,
            )

        try:
            interval = float(kanban_cfg.get("dispatch_interval_seconds", 60) or 60)
        except (ValueError, TypeError):
            logger.warning(
                "kanban dispatcher: invalid dispatch_interval_seconds=%r, using default 60",
                kanban_cfg.get("dispatch_interval_seconds"),
            )
            interval = 60.0
        interval = max(interval, 1.0)  # 合理下限 —— 比这更紧是个自找麻烦的设置

        # 读取 max_spawn 配置以限制并发 kanban 任务
        max_spawn = kanban_cfg.get("max_spawn", None)
        if max_spawn is not None:
            logger.info(f"kanban dispatcher: max_spawn={max_spawn}")

        # 限制同时运行的任务数量，以免慢速 worker（本地 LLM、资源受限的主机）
        # 堆积并超时。设置后，当看板已有这么多 'running' 状态的任务时，调度器
        # 跳过生成。
        raw_max_in_progress = kanban_cfg.get("max_in_progress", None)
        max_in_progress = None
        if raw_max_in_progress is not None:
            try:
                max_in_progress = int(raw_max_in_progress)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.max_in_progress=%r; ignoring",
                    raw_max_in_progress,
                )
                max_in_progress = None
            else:
                if max_in_progress < 1:
                    logger.warning(
                        "kanban dispatcher: kanban.max_in_progress=%r is below 1; ignoring",
                        raw_max_in_progress,
                    )
                    max_in_progress = None
                else:
                    logger.info(f"kanban dispatcher: max_in_progress={max_in_progress}")

        raw_failure_limit = kanban_cfg.get("failure_limit", _kb.DEFAULT_FAILURE_LIMIT)
        try:
            failure_limit = int(raw_failure_limit)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.failure_limit=%r; using default %d",
                raw_failure_limit,
                _kb.DEFAULT_FAILURE_LIMIT,
            )
            failure_limit = _kb.DEFAULT_FAILURE_LIMIT
        if failure_limit < 1:
            logger.warning(
                "kanban dispatcher: kanban.failure_limit=%r is below 1; using default %d",
                raw_failure_limit,
                _kb.DEFAULT_FAILURE_LIMIT,
            )
            failure_limit = _kb.DEFAULT_FAILURE_LIMIT

        # 读取 stale_timeout_seconds —— 0 禁用过时检测。
        raw_stale = kanban_cfg.get("dispatch_stale_timeout_seconds", 0)
        try:
            stale_timeout_seconds = int(raw_stale or 0)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.dispatch_stale_timeout_seconds=%r; "
                "disabling stale detection",
                raw_stale,
            )
            stale_timeout_seconds = 0

        # 读取 kanban.default_assignee —— 为没有显式 assignee 创建的任务
        # （例如通过 dashboard 创建）提供的回退 profile。设置后，调度器把它应用
        # 到未分配的 ready 任务上，而不是无限期跳过（#27145）。空字符串
        # （schema 默认值）意味着"不回退，继续跳过" —— 与现有安装向后兼容。
        default_assignee = (kanban_cfg.get("default_assignee") or "").strip() or None
        if default_assignee:
            logger.info(
                "kanban dispatcher: default_assignee=%r (unassigned ready tasks "
                "will route to this profile)",
                default_assignee,
            )

        # 读取 kanban.max_in_progress_per_profile —— 按 profile 的并发上限
        # （#21582）。设置后，即使全局 max_in_progress 允许，单个 profile 一次
        # 运行的 worker 也不超过 N 个。防止单个 profile 的本地模型 / API 配额 /
        # 浏览器池被扇出压垮。
        raw_per_profile = kanban_cfg.get("max_in_progress_per_profile", None)
        max_in_progress_per_profile = None
        if raw_per_profile is not None:
            try:
                max_in_progress_per_profile = int(raw_per_profile)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.max_in_progress_per_profile=%r; ignoring",
                    raw_per_profile,
                )
                max_in_progress_per_profile = None
            else:
                if max_in_progress_per_profile < 1:
                    logger.warning(
                        "kanban dispatcher: kanban.max_in_progress_per_profile=%r is below 1; ignoring",
                        raw_per_profile,
                    )
                    max_in_progress_per_profile = None
                else:
                    logger.info(
                        "kanban dispatcher: max_in_progress_per_profile=%d",
                        max_in_progress_per_profile,
                    )

        # 初始延迟，让 gateway 在调度器生成 worker（这些 worker 可能会命中
        # gateway 的通知订阅等）之前完成适配器接线。与通知器监视器的延迟一致。
        await asyncio.sleep(5)

        # 健康遥测，镜像自 `_cmd_daemon`：当 ready 队列非空但连续 N 个 tick 的
        # 生成数为 0 时警告 —— 通常意味着 PATH 损坏、venv 缺失或凭证丢失。
        HEALTH_WINDOW = 6
        bad_ticks = 0
        last_warn_at = 0
        # 避免对看起来损坏的看板 DB 热循环，但不要永久抑制相同指纹的重试：
        # 瞬态 WAL/打开竞态可能在一个 tick 中表现为 "database disk image
        # is malformed"。
        CORRUPT_BOARD_RETRY_AFTER_SECONDS = 300
        disabled_corrupt_boards: dict[
            str, tuple[tuple[str, int | None, int | None], float]
        ] = {}

        def _board_db_fingerprint(slug: str) -> tuple[str, int | None, int | None]:
            path = _kb.kanban_db_path(slug)
            try:
                resolved = str(path.expanduser().resolve())
            except Exception:
                resolved = str(path)
            try:
                stat = path.stat()
            except OSError:
                return (resolved, None, None)
            return (resolved, stat.st_mtime_ns, stat.st_size)

        def _is_corrupt_board_db_error(exc: Exception) -> bool:
            corrupt_guard_error = getattr(_kb, "KanbanDbCorruptError", None)
            if corrupt_guard_error is not None and isinstance(exc, corrupt_guard_error):
                return True
            if not isinstance(exc, sqlite3.DatabaseError):
                return False
            msg = str(exc).lower()
            return (
                "file is not a database" in msg
                or "database disk image is malformed" in msg
            )

        def _tick_once_for_board(slug: str) -> "Optional[object]":
            """为特定看板运行一次 dispatch_once。

            通过 `asyncio.to_thread` 在工作线程中运行。`board=slug` 透传给
            `dispatch_once`，使 `resolve_workspace` 和 `_default_spawn` 看到
            正确的路径。按看板的 DB 被显式打开，以便并发看板绝不共享连接句柄
            或意外跨看板 claim。
            """
            conn = None
            fingerprint = _board_db_fingerprint(slug)
            disabled_entry = disabled_corrupt_boards.get(slug)
            if disabled_entry is not None:
                disabled_fingerprint, disabled_at = disabled_entry
                age = time.monotonic() - disabled_at
                if (
                    disabled_fingerprint == fingerprint
                    and age < CORRUPT_BOARD_RETRY_AFTER_SECONDS
                ):
                    return None
                if disabled_fingerprint == fingerprint:
                    logger.info(
                        "kanban dispatcher: board %s database fingerprint unchanged "
                        "after %.0fs quarantine; retrying dispatch",
                        slug,
                        age,
                    )
                else:
                    logger.info(
                        "kanban dispatcher: board %s database changed; retrying dispatch",
                        slug,
                    )
                disabled_corrupt_boards.pop(slug, None)
            try:
                conn = _kb.connect(board=slug)
                # `connect()` 在每个进程首次打开时运行 schema + 幂等迁移；
                # 此前这里显式调用的 `init_db()` 会打破进程内缓存，并在第二个
                # 连接上重新运行迁移，与第一个产生竞态。参见
                # `_kanban_notifier_watcher` 中匹配的注释和 issue #21378。
                return _kb.dispatch_once(
                    conn,
                    board=slug,
                    max_spawn=max_spawn,
                    max_in_progress=max_in_progress,
                    failure_limit=failure_limit,
                    stale_timeout_seconds=stale_timeout_seconds,
                    default_assignee=default_assignee,
                    max_in_progress_per_profile=max_in_progress_per_profile,
                )
            except sqlite3.DatabaseError as exc:
                if _is_corrupt_board_db_error(exc):
                    disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                    logger.error(
                        "kanban dispatcher: board %s database %s is not a valid "
                        "SQLite database; pausing dispatch for this board until "
                        "the file changes, the gateway restarts, or the "
                        "quarantine timer expires. Move or restore the file, "
                        "then run `hermes kanban init` if you need a fresh board.",
                        slug,
                        fingerprint[0],
                    )
                    return None
                logger.exception("kanban dispatcher: tick failed on board %s", slug)
                return None
            except Exception as exc:
                if _is_corrupt_board_db_error(exc):
                    disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                    logger.error(
                        "kanban dispatcher: board %s database %s is not a valid "
                        "SQLite database; pausing dispatch for this board until "
                        "the file changes, the gateway restarts, or the "
                        "quarantine timer expires. Move or restore the file, "
                        "then run `hermes kanban init` if you need a fresh board.",
                        slug,
                        fingerprint[0],
                    )
                    return None
                logger.exception("kanban dispatcher: tick failed on board %s", slug)
                return None
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        def _tick_once() -> "list[tuple[str, Optional[object]]]":
            """对每个看板运行一次 dispatch_once。返回 (slug, result) 对。

            每个 tick 枚举看板能让调度器在用户运行中创建新看板时保持诚实：
            不需要重启，下一个 tick 会自动拾取它。
            """
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            out: list[tuple[str, "Optional[object]"]] = []
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                out.append((slug, _tick_once_for_board(slug)))
            return out

        def _ready_nonempty() -> bool:
            """廉价探针：任意看板上是否至少有一个 ready+assigned+unclaimed 的
            任务，其 assignee 映射到一个真实的 Hermes profile（即调度器会真正
            为之生成 worker 的任务）？

            分配给控制面车道（例如 ``orion-cc``、``orion-research``）的任务由
            终端通过 ``claim_task`` 直接拉取，永远不会被生成，所以满是这类
            任务的队列是"正确地空闲"，而不是"卡住"。在这里过滤掉它们，使卡住
            警告只在真正的失败时触发（PATH 损坏、venv 缺失、真实 Hermes
            profile 的凭证丢失）。
            """
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                conn = None
                try:
                    conn = _kb.connect(board=slug)
                    if _kb.has_spawnable_ready(conn):
                        return True
                    if _kb.has_spawnable_review(conn):
                        return True
                except Exception:
                    continue
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
            return False

        # 自动分解：在调度器扇出 worker 之前，把新的 triage 任务转成 ready 的
        # 工作图。由 ``kanban.auto_decompose`` 门控（默认 True）。由
        # ``kanban.auto_decompose_per_tick``（默认 3）封顶，以免大量 triage
        # 任务在一个 tick 内突发消耗 aux LLM；剩余的延迟到后续 tick。
        #
        # 该标志每个 tick 都从 config 重新读取（#49638），而不是在启动时捕获
        # 一次。auto-decompose 是一个安全开关：用户看到它扇出并运行他们不想要
        # 的任务时，会用 ``kanban.auto_decompose: false`` 来停止它 —— 而这必须
        # 在下一个 tick 生效，而不是要求重启 gateway。（报告：auto-decompose
        # 在用户仍在输入任务描述时就创建并启动了破坏性任务，而该标志"无法被
        # 禁用"，因为 gateway 已经捕获了其启动时的值。）
        def _read_auto_decompose_settings() -> tuple[bool, int]:
            """每个 tick 从当前 config 重新解析 (enabled, per_tick)。"""
            return _resolve_auto_decompose_settings(_load_config)

        def _auto_decompose_tick(auto_decompose_per_tick: int) -> int:
            """对所有看板最多 N 个 triage 任务运行自动分解器。返回本次 tick
            成功分解或指定的 triage 任务数量。
            """
            try:
                from hermes_cli import kanban_decompose as _decomp
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "kanban auto-decompose: import failed (%s); skipping", exc,
                )
                return 0
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            attempted = 0
            successes = 0
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                if attempted >= auto_decompose_per_tick:
                    break
                # 在调用期间固定此看板 —— 与 dashboard 的 specify 端点相同的
                # 模式。分解器模块连接时不带 board kwarg，依赖环境变量。
                prev_env = os.environ.get("HERMES_KANBAN_BOARD")
                try:
                    os.environ["HERMES_KANBAN_BOARD"] = slug
                    try:
                        triage_ids = _decomp.list_triage_ids()
                    except Exception as exc:
                        logger.debug(
                            "kanban auto-decompose: list_triage_ids failed on board %s (%s)",
                            slug, exc,
                        )
                        triage_ids = []
                    for tid in triage_ids:
                        if attempted >= auto_decompose_per_tick:
                            break
                        attempted += 1
                        try:
                            outcome = _decomp.decompose_task(
                                tid, author="auto-decomposer",
                            )
                        except Exception:
                            logger.exception(
                                "kanban auto-decompose: decompose_task crashed on %s",
                                tid,
                            )
                            continue
                        if outcome.ok:
                            successes += 1
                            if outcome.fanout and outcome.child_ids:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → %d children",
                                    slug, tid, len(outcome.child_ids),
                                )
                            else:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → single task (no fanout)",
                                    slug, tid,
                                )
                        else:
                            # 常见的 no-op 原因（未配置 aux client）不应每个
                            # tick 都刷日志。以 debug 级别记录。
                            logger.debug(
                                "kanban auto-decompose [%s]: %s skipped: %s",
                                slug, tid, outcome.reason,
                            )
                finally:
                    if prev_env is None:
                        os.environ.pop("HERMES_KANBAN_BOARD", None)
                    else:
                        os.environ["HERMES_KANBAN_BOARD"] = prev_env
            return successes

        logger.info(
            "kanban dispatcher: embedded in gateway (interval=%.1fs)", interval
        )
        while self._running:
            try:
                # 在按看板工作之前回收僵尸子进程，以便某个看板 DB 的失败不会
                # 阻塞无关 worker 的清理。
                pids = await asyncio.to_thread(_kb.reap_worker_zombies)
                if pids:
                    logger.info(
                        "kanban dispatcher: reaped %d zombie worker(s), pids=%s",
                        len(pids),
                        pids,
                    )
            except Exception:
                logger.exception("kanban dispatcher: zombie reaper failed")

            try:
                # 每个 tick 实时重新读取 auto-decompose 开关，使用户翻转
                # kanban.auto_decompose=false 来停止失控扇出在下一个 tick 生效，
                # 而不是在 gateway 重启时（#49638）。
                _ad_enabled, _ad_per_tick = _read_auto_decompose_settings()
                if _ad_enabled:
                    await asyncio.to_thread(_auto_decompose_tick, _ad_per_tick)
                results = await asyncio.to_thread(_tick_once)
                any_spawned = False
                for slug, res in (results or []):
                    if res is not None and getattr(res, "spawned", None):
                        any_spawned = True
                        # 默认安静 —— 仅在确实发生事情时记录日志，使空闲的
                        # gateway 保持安静。
                        logger.info(
                            "kanban dispatcher [%s]: spawned=%d reclaimed=%d "
                            "crashed=%d timed_out=%d promoted=%d auto_blocked=%d",
                            slug,
                            len(res.spawned),
                            res.reclaimed,
                            len(res.crashed) if hasattr(res.crashed, "__len__") else 0,
                            len(res.timed_out) if hasattr(res.timed_out, "__len__") else 0,
                            res.promoted,
                            len(res.auto_blocked) if hasattr(res.auto_blocked, "__len__") else 0,
                        )
                # 健康遥测（跨看板聚合）
                ready_pending = await asyncio.to_thread(_ready_nonempty)
                if ready_pending and not any_spawned:
                    bad_ticks += 1
                else:
                    bad_ticks = 0
                if bad_ticks >= HEALTH_WINDOW:
                    now = int(time.time())
                    if now - last_warn_at >= 300:
                        logger.warning(
                            "kanban dispatcher stuck: ready queue non-empty for "
                            "%d consecutive ticks but 0 workers spawned. Check "
                            "profile health (venv, PATH, credentials) and "
                            "`hermes kanban list --status ready`.",
                            bad_ticks,
                        )
                        last_warn_at = now
            except asyncio.CancelledError:
                logger.debug("kanban dispatcher: cancelled")
                _release_singleton_lock(self._kanban_dispatcher_lock_handle)
                self._kanban_dispatcher_lock_handle = None
                raise
            except Exception:
                logger.exception("kanban dispatcher: unexpected watcher error")

            # 以 1s 切片睡眠，使关闭迅速 —— 否则 stop() 会等待最长
            # `interval` 秒以完成当前睡眠。
            slept = 0.0
            while slept < interval and self._running:
                await asyncio.sleep(min(1.0, interval - slept))
                slept += 1.0

        _release_singleton_lock(self._kanban_dispatcher_lock_handle)
        self._kanban_dispatcher_lock_handle = None
