"""
Signal 附件发送速率限制调度器。

进程级令牌桶模拟器，镜像 signal-cli/Signal-Server 针对每个账号强制执行的
附件速率限制。生产者（``SignalAdapter.send_multiple_images`` 和
``send_message`` 工具的 Signal 路径）在发送附件前调用 ``acquire(n)``；
收到 429 响应时调用 ``feedback(retry_after, n)``，使模型根据
服务器的权威提示重新校准。

调度器通过 ``asyncio.Lock`` 序列化并发调用，
在共享同一 signal-cli 守护进程的各 agent 会话之间提供 FIFO 公平性。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SIGNAL_MAX_ATTACHMENTS_PER_MSG = 32  # 单条消息附件上限（来源：Signal-{Android,Desktop} 源码）
SIGNAL_RATE_LIMIT_BUCKET_CAPACITY = 50  # 服务端附件速率限制令牌桶容量
SIGNAL_RATE_LIMIT_DEFAULT_RETRY_AFTER = 4  # signal-cli < v0.14.3 的默认令牌补充间隔（秒）
SIGNAL_RATE_LIMIT_MAX_ATTEMPTS = 2  # 初始尝试 + 1 次重试
SIGNAL_BATCH_PACING_NOTICE_THRESHOLD = 10.0  # 预估等待时间超过 10s 时向用户提示延迟
SIGNAL_RPC_ERROR_RATELIMIT = -5  # signal-cli (v0.14.3+) 速率限制异常的 JSON-RPC 错误码


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class SignalRateLimitError(Exception):
    """
    当调用方通过 ``raise_on_rate_limit=True`` 选择抛出时，
    由 ``SignalAdapter._rpc`` 在收到速率限制响应时抛出。

    在 signal-cli ≥ v0.14.3 上携带服务端提供的每令牌 Retry-After（秒）。
    旧版本不暴露此字段时 ``retry_after`` 为 None。
    """

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SignalSchedulerError(Exception):
    pass

# ---------------------------------------------------------------------------
# 检测辅助函数——用于从 signal-cli 的各种错误格式中识别 429 响应
# （typed code、[429] 子字符串，以及通过 AttachmentInvalidException
#  透出的 libsignal-net RetryLaterException）。
# ---------------------------------------------------------------------------

# "Retry after 4 seconds" / "retry after 4 second" —— libsignal-net 的
# RetryLaterException 字符串形式，在附件上传时遭遇 429 后浮现
# （signal-cli 将其包装为 AttachmentInvalidException 而非
#  RateLimitException，因此 typed 路径不会触发）。
_RETRY_AFTER_RE = re.compile(r"Retry after (\d+(?:\.\d+)?)\s*second", re.IGNORECASE)


def _extract_retry_after_seconds(err: Any) -> Optional[float]:
    """从 signal-cli 速率限制错误中提取每令牌 Retry-After 窗口（秒）。

    依次尝试两个来源：
    1. ``error.data.response.results[*].retryAfterSeconds``——signal-cli ≥ v0.14.3
       针对普通 RateLimitException 暴露的结构化字段。
    2. 从消息中解析 ``"Retry after N seconds"``——覆盖 libsignal-net 的
       RetryLaterException 在附件上传速率限制时被包装为
       AttachmentInvalidException 的情况，此时结构化字段为 null。

    两者均无值时返回 None。
    """
    msg = ""
    if isinstance(err, dict):
        data = err.get("data") or {}
        response = data.get("response") or {}
        results = response.get("results") or []
        candidates = [
            r.get("retryAfterSeconds") for r in results
            if isinstance(r, dict) and r.get("retryAfterSeconds")
        ]
        if candidates:
            return float(max(candidates))
        msg = str(err.get("message", ""))
    else:
        msg = str(err)
    match = _RETRY_AFTER_RE.search(msg)
    return float(match.group(1)) if match else None


def _is_signal_rate_limit_error(err: Any) -> bool:
    """如果 signal-cli RPC 错误为速率限制失败则返回 True。

    匹配三个层次：
    - typed ``RATELIMIT_ERROR`` 错误码（signal-cli ≥ v0.14.3，普通 RateLimitException）
    - 遗留的 ``[429] / RateLimitException`` 子字符串
    - libsignal-net 的 ``RetryLaterException`` / ``Retry after N seconds``
      在附件上传速率限制时透过 ``AttachmentInvalidException`` 暴露——
      signal-cli 从不将其重新标记为 RateLimitException，因此只能靠子字符串匹配。
    """
    if isinstance(err, dict) and err.get("code") == SIGNAL_RPC_ERROR_RATELIMIT:
        return True

    message = (
        str(err.get("message", ""))
        if isinstance(err, dict)
        else str(err)
    )
    msg_lower = message.lower()
    return (
        "[429]" in message
        or "ratelimit" in msg_lower
        or "retrylaterexception" in msg_lower
        or "retry after" in msg_lower
    )


# ---------------------------------------------------------------------------
# 杂项辅助函数
# ---------------------------------------------------------------------------

def _format_wait(seconds: float) -> str:
    """面向用户的等待时间友好标签，用于批量发送提示。"""
    s = max(0.0, seconds)
    if s < 90:
        return f"{int(round(s))}s"
    return f"{max(1, int(round(s / 60)))} min"


def _signal_send_timeout(num_attachments: int) -> float:
    """Signal ``send`` RPC 的 HTTP 超时时间。

    signal-cli 在调用过程中串行上传附件，因此服务端耗时随批量大小线性增加。
    默认 30s 足以应对纯文本发送，但对大型附件批次会在上传中途截断——
    此时即便 signal-cli 在数秒后成功完成发送，我们也会记录到虚假失败。
    按每附件 5s 线性扩展，下限为 60s。
    """
    if num_attachments <= 0:
        return 30.0
    return max(60.0, 5.0 * num_attachments)


# ---------------------------------------------------------------------------
# 调度器
# ---------------------------------------------------------------------------

class SignalAttachmentScheduler:
    """针对 Signal 附件发送的进程级令牌桶模拟器。

    令牌桶最多持有 ``capacity`` 个令牌（默认 50，对应 Signal 服务端
    速率限制桶大小）。每个附件消耗一个令牌。令牌以 ``refill_rate``
    令牌/秒的速率补充，该速率根据服务端在 429 触发时返回的每令牌
    Retry-After 提示进行校准。在观察到首次 429 之前，使用文档记录的
    默认值（1 令牌 / 4 秒）。

    并发的 ``acquire(n)`` 调用通过 ``asyncio.Lock`` 串行化——
    在访问同一守护进程的各 agent 会话之间实现自然 FIFO。
    """

    def __init__(
        self,
        capacity: float = float(SIGNAL_RATE_LIMIT_BUCKET_CAPACITY),
        default_retry_after: float = float(SIGNAL_RATE_LIMIT_DEFAULT_RETRY_AFTER),
    ) -> None:
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.refill_rate = 1.0 / float(default_retry_after)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        if elapsed > 0 and self.tokens < self.capacity:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def estimate_wait(self, n: int) -> float:
        """粗略估算 ``n`` 个令牌可用所需的秒数。
        用于在提交可能静默阻塞的 ``acquire`` 之前决定是否向用户
        发出批量发送提示。无锁操作；与并发 acquire 之间的微小竞争
        对信息性提示无害。
        """
        now = time.monotonic()
        elapsed = now - self.last_refill
        projected = self.tokens
        if elapsed > 0 and projected < self.capacity:
            projected = min(self.capacity, projected + elapsed * self.refill_rate)
        deficit = n - projected
        if deficit <= 0:
            return 0.0
        return deficit / self.refill_rate

    async def acquire(self, n: int) -> float:
        """阻塞直到至少有 ``n`` 个令牌可用，返回已等待的秒数。

        **不**扣除令牌——令牌桶是服务端容量的只读模型。
        RPC 完成后调用 ``report_rpc_duration()`` 以将模型与服务端时间线同步。

        对于大量并发协程请求大批次上传的情况不够完美
        （``report_rpc_duration`` 需要较长时间才能被调用），
        但这只是一个模拟。Signal 服务端才是真实依据，
        会在触发速率限制时抛出异常并触发重排。

        在 ``asyncio.sleep`` 期间释放锁，允许其他调用方交错执行。
        重试循环在每次睡眠后重新检查，以防截止时间过于悲观。
        """
        if n <= 0:
            return 0.0
        if n > self.capacity:
            raise SignalSchedulerError(
                f"Signal scheduler was called requesting {n} tokens "
                f"(max is {self.capacity})",
            )

        total_slept = 0.0
        first_pass = True
        while True:
            async with self._lock:
                self._refill()
                if self.tokens >= n:
                    if not first_pass or total_slept > 0:
                        logger.debug(
                            "Signal scheduler: tokens sufficient for %d "
                            "(remaining=%.1f, total_slept=%.1fs)",
                            n, self.tokens, total_slept,
                        )
                    return total_slept
                deficit = n - self.tokens
            wait = deficit / self.refill_rate
            if first_pass:
                logger.info(
                    "Signal scheduler: pausing %.1fs for %d tokens "
                    "(available=%.1f, deficit=%.1f, refill=%.4f/s ≈ %.1fs/token)",
                    wait, n, self.tokens, deficit,
                    self.refill_rate, 1.0 / self.refill_rate,
                )
                first_pass = False
            await asyncio.sleep(wait)
            total_slept += wait

    async def report_rpc_duration(self, rpc_duration: float, n_attachments: int) -> None:
        """记录刚完成的附件发送 RPC。

        扣除 ``n_attachments`` 个令牌，且不在上传窗口期内补充令牌。
        Signal 服务端在 RPC 开始时检查令牌桶，在请求处理期间*不*补充——
        响应返回后才恢复补充。若在上传期间也计入补充，会导致累积漂移
        并最终触发 429。

        推进 ``last_refill``，使下次 ``acquire`` / ``_refill``
        从此时刻开始计时。
        """
        if n_attachments <= 0:
            return

        async with self._lock:
            now = time.monotonic()
            token_before = self.tokens
            self.tokens = max(0.0, token_before - float(n_attachments))
            self.last_refill = now
        logger.log(
            logging.INFO if rpc_duration > 10 and n_attachments > 5 else logging.DEBUG,
            "Signal scheduler: RPC for %d att took %.1fs — "
            "tokens %.1f → %.1f (deducted=%d, no upload refill credited, refill=%.4fs⁻¹)",
            n_attachments, rpc_duration,
            token_before, self.tokens,
            n_attachments, self.refill_rate,
        )

    def feedback(self, retry_after: Optional[float], n_attempted: int) -> None:
        """在收到 429 后应用服务端反馈。

        ``retry_after`` 是服务端报告的每*令牌*补充窗口（秒）；
        当 signal-cli 版本低于 v0.14.3 且未暴露该字段时为 None。

        存在有效值时，以此校准 ``refill_rate``：服务端为权威来源。
        """
        if retry_after and retry_after > 0:
            new_rate = 1.0 / float(retry_after)
            if new_rate != self.refill_rate:
                logger.info(
                    "Signal scheduler: calibrating refill_rate to %.4f tokens/sec "
                    "(server retry_after=%.1fs per token)",
                    new_rate, retry_after,
                )
                self.refill_rate = new_rate
        self.tokens = 0.0
        self.last_refill = time.monotonic()

    def state(self) -> dict:
        """返回当前调度器状态用于诊断日志（只读）。

        不推进 ``last_refill``——可安全地从日志路径调用，
        不会扰动令牌桶。
        """
        now = time.monotonic()
        elapsed = now - self.last_refill
        projected = self.tokens
        if elapsed > 0 and projected < self.capacity:
            projected = min(self.capacity, projected + elapsed * self.refill_rate)
        return {
            "tokens": round(projected, 1),
            "capacity": int(self.capacity),
            "refill_rate": round(self.refill_rate, 4),
            "refill_seconds_per_token": round(1.0 / self.refill_rate, 1) if self.refill_rate > 0 else float("inf"),
        }


# ---------------------------------------------------------------------------
# 进程级单例
# ---------------------------------------------------------------------------

_scheduler: Optional[SignalAttachmentScheduler] = None


def get_scheduler() -> SignalAttachmentScheduler:
    """返回进程级调度器，首次访问时创建。"""
    global _scheduler
    if _scheduler is None:
        _scheduler = SignalAttachmentScheduler()
        logger.info(
            "Signal scheduler: created (capacity=%d tokens, refill=%.4f/s ≈ %.1fs/token)",
            int(_scheduler.capacity),
            _scheduler.refill_rate,
            1.0 / _scheduler.refill_rate,
        )
    return _scheduler


def _reset_scheduler() -> None:
    """丢弃缓存的调度器，使下次 ``get_scheduler`` 调用创建新实例。
    仅供测试使用——禁止在生产路径中调用。"""
    global _scheduler
    _scheduler = None
