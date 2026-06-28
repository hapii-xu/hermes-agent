"""重试工具 —— 用于去相关重试的抖动退避。

用抖动延迟替代固定指数退避，防止多个会话同时命中同一
速率受限提供商时产生惊群式重试峰值。
"""

import random
import threading
import time
from typing import Any

# 进程内抖动种子唯一性的单调计数器。
# 使用锁保护，避免并发重试路径（如多个 gateway 会话同时重试）中的竞态条件。
_jitter_counter = 0
_jitter_lock = threading.Lock()

# Z.AI Coding Plan 的 GLM-5.2 endpoint 对于原本有效的 Hermes 请求
# 经常返回 HTTP 429 code 1305（"服务可能暂时过载..."）。
# 短重试倾向于持续冲击同一过载窗口；在若干次正常重试后逐步扩大等待窗口。
# 保持上限对交互友好：简单的 TUI 消息应在几分钟内明显失败，而非静默等待 20+ 分钟。
_ZAI_CODING_OVERLOAD_LONG_BACKOFF = (30.0, 60.0, 90.0, 120.0)


def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
    jitter_ratio: float = 0.5,
) -> float:
    """计算带抖动的指数退避延迟。

    Args:
        attempt: 从 1 开始的重试次数。
        base_delay: 第 1 次重试的基础延迟（秒）。
        max_delay: 最大延迟上限（秒）。
        jitter_ratio: 将计算出的延迟作为随机抖动范围的比例。
            0.5 表示抖动在 [0, 0.5 * delay] 范围内均匀分布。

    Returns:
        延迟秒数：min(base * 2^(attempt-1), max_delay) + 抖动值。

    抖动使并发重试去相关，避免多个会话命中同一提供商时同时重试。
    """
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        tick = _jitter_counter

    exponent = max(0, attempt - 1)
    if exponent >= 63 or base_delay <= 0:
        delay = max_delay
    else:
        delay = min(base_delay * (2 ** exponent), max_delay)

    # 使用时间 + 计数器作为种子，即使时钟精度较低也能实现去相关。
    seed = (time.time_ns() ^ (tick * 0x9E3779B9)) & 0xFFFFFFFF
    rng = random.Random(seed)
    jitter = rng.uniform(0, jitter_ratio * delay)

    return delay + jitter


def _error_text(error: Any) -> str:
    """尽力展平提供商错误文本，用于重试分类。"""
    parts = [
        error,
        getattr(error, "message", None),
        getattr(error, "body", None),
        getattr(error, "response", None),
    ]
    return " ".join(str(part) for part in parts if part is not None).lower()


def is_zai_coding_overload_error(*, base_url: str | None, model: str | None, error: Any) -> bool:
    """对于 Z.AI Coding Plan 的瞬时过载 429 返回 True。

    Coding Plan endpoint 将过载报告为 HTTP 429，body code 为 1305，
    消息为 "The service may be temporarily overloaded..."。
    仅对该特定形状进行特殊处理，以便普通配额/账单 429
    仍通过现有分类器快速失败。
    """
    base = (base_url or "").lower()
    model_name = (model or "").lower()
    status = getattr(error, "status_code", None)
    text = _error_text(error)
    return (
        status == 429
        and "api.z.ai/api/coding/paas/v4" in base
        and "glm-5.2" in model_name
        and ("1305" in text or "temporarily overloaded" in text)
    )


def adaptive_rate_limit_backoff(
    attempt: int,
    *,
    base_url: str | None,
    model: str | None,
    error: Any,
    default_wait: float,
    short_attempts: int = 3,
) -> tuple[float, str | None]:
    """感知提供商的速率限制退避。

    对于大多数提供商，直接返回未变的 ``default_wait``。
    对于 Z.AI Coding Plan GLM-5.2 过载，前 ``short_attempts`` 次
    重试保持正常的短指数退避，之后切换到逐步延长的等待
    （30s → 60s → 90s → 120s，有上限）加轻量抖动。

    ``attempt`` 从 1 开始，与重试循环的日志记录次数一致。
    返回 ``(wait_seconds, reason_label)``，当提供商特定策略触发时，
    ``reason_label`` 适合用于状态/日志装饰。
    """
    if not is_zai_coding_overload_error(base_url=base_url, model=model, error=error):
        return default_wait, None
    if attempt <= short_attempts:
        return default_wait, "zai_coding_overload_short"

    idx = min(attempt - short_attempts - 1, len(_ZAI_CODING_OVERLOAD_LONG_BACKOFF) - 1)
    base_delay = _ZAI_CODING_OVERLOAD_LONG_BACKOFF[idx]
    # 较小的抖动比例使长等待时间可读，同时仍避免并发 Hermes 会话间的同步重试风暴。
    return jittered_backoff(1, base_delay=base_delay, max_delay=base_delay, jitter_ratio=0.2), "zai_coding_overload_long"
