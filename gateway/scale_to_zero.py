"""gateway 的 scale-to-zero 空闲检测 + 休眠静默（dormant-quiesce）（Phase 0）。

这是 gateway 侧的行为（BEHAVIOUR）层，消费 relay 的 scale-to-zero 原语
（PRIMITIVES）（gateway-gateway Phase 5：buffered-flip、持久化的 per-instance
buffer、wakeUrl 探测、重连 supervisor）。它拥有 *决定* 是否进入空闲的权力，
并驱动 relay transport 的 ``go_dormant()``（D12）——它本身并不挂起机器。
在 Fly 上，此刻已无流量的机器由 ``autostop:"suspend"`` 挂起，并由
autostart-on-wakeUrl 唤醒（decisions.md Q3=C′）。

设计约束（decisions.md）：
  - per-instance 的启用完全由 NAS 的 "Labs" 开关门控，并以
    ``HERMES_SCALE_TO_ZERO`` 环境戳的形式传递到 gateway（D11/Q8=A）。
    它不是用户配置键；``scale_to_zero.idle_timeout_minutes`` 才是
    config.yaml 中的配置（D2）。
  - 只有在消息通道是 relay-only 或不存在（D1/F6），并且注册了 wakeUrl
    （§3.4(1)），并且 flag 被置位时，才挂载（arm）。
  - 空闲 = 没有在途的 agent turn 且 N 分钟内没有入站消息且没有正在进行的
    后台工作（D2/D3/F7）。
  - 静默使用 ``go_dormant()``（关闭 socket + 保留 supervisor），
    绝不使用 stop/restart 的 drain 或 ``disconnect()``（F12/F14）。进程
    保持存活；Fly 负责冻结 + 恢复它。
  - 这里故意不调用 ``mark_resume_pending``（D13 —— 挂起会保留 RAM；只有
    当我们改用 autostop:"stop" 或观察到被 kill 时才需要恢复逻辑）。

纯辅助函数（``parse_idle_timeout_seconds``、``scale_to_zero_enabled``、
``messaging_is_relay_only_or_absent``、``is_idle``、``should_arm``）接受
朴素输入，因此无需运行中的 gateway 即可进行单元测试。
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional

# 当 scaleToZero Labs 开关打开时，由 NAS 打上的环境 flag（D11/Q8=A），
# 与 `relay` feature 打上 GATEWAY_RELAY_URL 的方式一致。仅识别真值。
SCALE_TO_ZERO_ENV = "HERMES_SCALE_TO_ZERO"

# config.yaml 默认值（D2）。行为性设置 -> 放在 config 中，而不是 env。
DEFAULT_IDLE_TIMEOUT_MINUTES = 5

_TRUTHY = {"1", "true", "yes", "on"}


def scale_to_zero_enabled(environ: Optional[dict] = None) -> bool:
    """per-instance 的 Labs 开关是否打开（即 HERMES_SCALE_TO_ZERO 戳）。

    D11/Q8=A：该环境 flag 是到达 gateway 的唯一 per-instance 启用信号。
    缺失/为空/为假 -> 禁用（故障安全的默认关闭）。
    """
    env = environ if environ is not None else os.environ
    return str(env.get(SCALE_TO_ZERO_ENV, "")).strip().lower() in _TRUTHY


def parse_idle_timeout_seconds(
    cfg_value: Any, default_minutes: int = DEFAULT_IDLE_TIMEOUT_MINUTES
) -> float:
    """把 ``scale_to_zero.idle_timeout_minutes``（config.yaml，D2）强制转换为秒。

    对任何非数字 / 非正值都回退到默认值（绝不抛异常，也绝不返回 <= 0 ——
    一个零或负的超时会让 gateway 立即进入休眠，这绝不是预期的行为）。
    """
    try:
        minutes = float(cfg_value)
    except (TypeError, ValueError):
        minutes = float(default_minutes)
    if minutes <= 0:
        minutes = float(default_minutes)
    return minutes * 60.0


def messaging_is_relay_only_or_absent(platforms: Iterable[Any]) -> bool:
    """当唯一连接的消息平台是 RELAY，或没有任何平台（纯 Chronos / 无平台
    agent）时返回 True —— 这是 F6/D1 的结构性前提。

    一个直接连接的平台（Discord/Telegram/Slack/...）持有活动的 socket，
    无法 scale to zero，因此它的存在会使该 feature 卸除。我们通过平台的
    ``.value``/name 来比较，以避免在这里导入 enum（保持本模块轻导入、
    可单元测试）。
    """
    names = {_platform_name(p) for p in platforms}
    names.discard("relay")
    return len(names) == 0


def _platform_name(platform: Any) -> str:
    value = getattr(platform, "value", platform)
    return str(value).strip().lower()


def should_arm(
    *,
    enabled: bool,
    relay_only_or_absent: bool,
    wake_url: Optional[str],
) -> bool:
    """是否要启动空闲监视器（D1/D11/§3.4(1)）。

    以下条件必须全部满足：Labs 开关打开、消息通道是 relay-only/不存在、
    并且注册了 wakeUrl（一个已挂起但没有可达唤醒目标的实例是个黑洞 ——
    §3.4(1)）。任一条件不满足 -> 监视器永不启动（没有空闲计时器、没有
    休眠），因此一个未开启的实例行为与今天完全一致。
    """
    return bool(enabled) and bool(relay_only_or_absent) and bool(wake_url)


def is_idle(
    *,
    running_agent_count: int,
    seconds_since_last_inbound: float,
    idle_timeout_seconds: float,
    has_live_background_work: bool,
) -> bool:
    """空闲判定谓词（D2/D3/F7）。纯函数 —— 组合三个合取条件。

    当且仅当以下全部成立时为空闲：没有在途的 agent turn、超时窗口内没有
    入站消息、并且没有正在进行的后台工作（后台化的 delegate_task / kanban /
    bg terminal）。任何正在进行的工作都会让 gateway 保持唤醒 —— 在执行中途
    挂起会导致工作丢失。
    """
    if running_agent_count > 0:
        return False
    if has_live_background_work:
        return False
    return seconds_since_last_inbound >= idle_timeout_seconds
