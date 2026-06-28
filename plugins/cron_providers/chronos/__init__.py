"""Chronos — NAS 托管的 cron provider（弹性缩容至零）。

Chronos（希腊时间之神，与 Hermes 并列）是第一个非默认
``CronScheduler``。它允许托管网关在空闲时缩容至零，同时仍能
触发 cron 任务：它不使用 60 秒的进程内定时器，而是请求 NAS 在每个任务
真正的下次触发时间精确设置一个外部单次触发器。NAS 通过经过身份验证的
webhook（``/api/cron/fire``）在触发时回调 agent；agent 通过
共享的 ``run_one_job`` 体执行任务并重新设置下一个单次触发器。

NAS 使用的外部调度器是 NAS 的内部实现细节 —
Chronos 不命名任何供应商，不持有调度器凭据，只使用 agent
现有的 Nous token 与 NAS 的 ``agent-cron`` 端点通信。

设计约束（参见计划的 DQ-1）：
  - start() 设置所有已启用的任务并立即返回；它永不阻塞，也不启动
    周期性唤醒。触发之间机器真正处于零状态。
  - reconcile 仅在热进程上运行（start / on_jobs_changed / 搭载于
    触发事件），永不作为休眠机器的周期性唤醒。

除非 ``cron.provider: chronos``，否则处于非活跃状态。``resolve_cron_scheduler``
在 Chronos 不可用时回退到内置，因此 cron 永不丢失其触发器。

通信契约：``docs/chronos-managed-cron-contract.md``。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from cron.scheduler_provider import CronScheduler

logger = logging.getLogger("cron.chronos")


def _cfg(*keys: str, default: Any = "") -> Any:
    """读取 cron.chronos.* 配置值（不访问网络）。"""
    try:
        from hermes_cli.config import cfg_get, load_config
        return cfg_get(load_config(), *keys, default=default)
    except Exception:
        return default


class ChronosCronScheduler(CronScheduler):
    """NAS 托管的外部 cron provider。"""

    def __init__(self) -> None:
        # job_id → fire_at 的内存映射，记录我们已请求 NAS 设置的触发器。
        # 尽力维护的缓存；reconcile 从 jobs.json 重建期望状态，因此冷启动
        # 进程只需重新设置（通过 dedup_key 保证幂等性）。
        self._armed: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._client = None  # 延迟构造（is_available 中不访问网络）

    # -- 身份 / 可用性 -----------------------------------------

    @property
    def name(self) -> str:
        return "chronos"

    def is_available(self) -> bool:
        """仅检查配置 — 不访问网络。

        Chronos 需要一个 portal 基础 URL、agent 自身公开可达的
        回调 URL（用于 NAS→agent 触发），以及可用的 Nous token（agent
        已登录 portal）。如果任何一项缺失，resolve_cron_scheduler
        将回退到内置定时器。
        """
        if not (_cfg("cron", "chronos", "portal_url") and _cfg("cron", "chronos", "callback_url")):
            return False
        return self._have_nous_token()

    def _have_nous_token(self) -> bool:
        """如果 agent 已登录 Nous Portal 则返回 True（不访问网络）。

        检查存储的认证状态中是否有 Nous access token — 不会刷新
        或访问网络（is_available 必须保持离线）。实际的
        支持刷新的 token 在 provision 时延迟解析。
        """
        try:
            from hermes_cli.auth import get_provider_auth_state
            state = get_provider_auth_state("nous") or {}
            return bool(state.get("access_token"))
        except Exception:
            return False

    # -- 客户端 -----------------------------------------------------------

    def _get_client(self):
        if self._client is None:
            from ._nas_client import NasCronClient
            self._client = NasCronClient(_cfg("cron", "chronos", "portal_url"))
        return self._client

    def _callback_url(self) -> str:
        return str(_cfg("cron", "chronos", "callback_url") or "")

    # -- 生命周期 --------------------------------------------------------

    def start(self, stop_event, *, adapters=None, loop=None, interval=60):
        """通过 NAS 设置所有已启用的任务，然后立即返回。

        不阻塞，也不启动 60 秒唤醒（DQ-1）— 这正是弹性缩容至零的核心。
        机器仅在 NAS→agent 触发时唤醒。
        """
        try:
            self.reconcile()
        except Exception as e:
            logger.warning("Chronos start() reconcile failed: %s", e)
        # 刻意返回 — 无循环，无周期性唤醒。

    def stop(self) -> None:
        return None

    def on_jobs_changed(self) -> None:
        """任务被创建/更新/移除/暂停/恢复 — 协调 NAS 注册表
        使受影响的单次触发器被（重新）设置或取消。"""
        try:
            self.reconcile()
        except Exception as e:
            logger.debug("Chronos on_jobs_changed reconcile failed: %s", e)

    # -- 设置触发器 -----------------------------------------------------------

    def _arm_one_shot(self, job: Dict[str, Any]) -> None:
        """请求 NAS 在任务的 next_run_at 时精确设置一个单次触发器。

        agent 计算时间；NAS 及其调度器是被动执行方。
        通过 dedup_key 对 (job_id, fire_at) 保证幂等性，因此重复设置
        同一触发时间在 NAS 侧为空操作。
        """
        job_id = job["id"]
        fire_at = job.get("next_run_at")
        if not fire_at:
            return
        dedup_key = f"{job_id}:{fire_at}"
        self._get_client().provision(
            job_id=job_id,
            fire_at=fire_at,
            agent_callback_url=self._callback_url(),
            dedup_key=dedup_key,
        )
        with self._lock:
            self._armed[job_id] = fire_at

    def _cancel(self, job_id: str) -> None:
        try:
            self._get_client().cancel(job_id=job_id)
        finally:
            with self._lock:
                self._armed.pop(job_id, None)

    def _list_armed(self) -> Dict[str, str]:
        """已观察到的已设置单次触发器：job_id → fire_at。

        优先使用内存映射（热进程）；若映射为冷/空，则询问 NAS
        （尽力而为）。如果 NAS 列表失败，返回现有数据 — reconcile 随后
        会幂等地重新设置期望的任务。
        """
        with self._lock:
            if self._armed:
                return dict(self._armed)
        try:
            observed = {
                item["job_id"]: item.get("fire_at", "")
                for item in self._get_client().list_armed()
                if item.get("job_id")
            }
            with self._lock:
                self._armed.update(observed)
            return observed
        except Exception as e:
            logger.debug("Chronos _list_armed failed (will re-arm idempotently): %s", e)
            return {}

    # -- 协调 --------------------------------------------------------

    def reconcile(self) -> None:
        """将 NAS 已设置的单次触发器与 jobs.json（期望状态）收敛：
        设置缺失的 / 重新设置时间变更的，取消孤立的。"""
        from cron.jobs import load_jobs

        desired: Dict[str, str] = {
            j["id"]: j["next_run_at"]
            for j in load_jobs()
            if j.get("enabled") and j.get("next_run_at") and j.get("state") != "paused"
        }
        observed = self._list_armed()

        # 设置缺失或时间变更的触发器。
        for job_id, fire_at in desired.items():
            if observed.get(job_id) != fire_at:
                # Re-fetch the full job dict to arm (need the whole record).
                from cron.jobs import get_job
                job = get_job(job_id)
                if job:
                    try:
                        self._arm_one_shot(job)
                    except Exception as e:
                        logger.warning("Chronos failed to arm job %s: %s", job_id, e)

        # 取消孤立的触发器（已设置但不再需要）。
        for job_id in list(observed.keys()):
            if job_id not in desired:
                try:
                    self._cancel(job_id)
                except Exception as e:
                    logger.warning("Chronos failed to cancel orphan %s: %s", job_id, e)

    # -- 触发 -------------------------------------------------------------

    def fire_due(self, job_id: str, *, adapters: Any = None, loop: Any = None) -> bool:
        """运行到期任务（通过 ABC 默认值 claim + run_one_job），然后
        通过 NAS 重新设置下一个单次触发器。

        重新设置在运行后进行，使 next_run_at 反映已完成的触发。
        如果任务已消失（单次任务完成 / repeat-N 耗尽），get_job
        返回 None → 无需重新设置（调度自然停止）。
        """
        ran = super().fire_due(job_id, adapters=adapters, loop=loop)
        if ran:
            from cron.jobs import get_job
            job = get_job(job_id)
            if job and job.get("enabled") and job.get("next_run_at"):
                try:
                    self._arm_one_shot(job)
                except Exception as e:
                    logger.warning("Chronos failed to re-arm job %s after fire: %s", job_id, e)
        return ran


def register(ctx) -> None:
    """插件入口点 — 将 Chronos provider 注册到加载器中。

    与 memory 插件形式一致；plugins/cron_providers 发现机制调用此函数并
    通过 register_cron_scheduler 收集 provider。
    """
    ctx.register_cron_scheduler(ChronosCronScheduler())
