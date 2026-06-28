"""
Hermes Gateway - 多平台消息集成。

本模块提供一个统一的 gateway，用于将 Hermes agent
连接到各种消息平台（Telegram、Discord、WhatsApp、微信等），支持：
- 会话管理（具有重置策略的持久对话）
- 动态上下文注入（agent 知道消息来源）
- 投递路由（将 cron 任务输出路由到相应频道）
- 平台特定工具集（不同平台具有不同能力）
"""

from .config import GatewayConfig, PlatformConfig, HomeChannel, load_gateway_config
from .session import (
    SessionContext,
    SessionStore,
    SessionResetPolicy,
    build_session_context_prompt,
)
from .delivery import DeliveryRouter, DeliveryTarget

__all__ = [
    # 配置
    "GatewayConfig",
    "PlatformConfig", 
    "HomeChannel",
    "load_gateway_config",
    # 会话
    "SessionContext",
    "SessionStore",
    "SessionResetPolicy",
    "build_session_context_prompt",
    # 投递
    "DeliveryRouter",
    "DeliveryTarget",
]
