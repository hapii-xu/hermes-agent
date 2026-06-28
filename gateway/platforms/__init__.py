"""
平台适配器模块，用于消息集成。

每个适配器负责：
- 接收来自平台的消息
- 向平台发送消息/响应
- 平台特定的身份验证
- 消息格式化与媒体处理
"""

from .base import BasePlatformAdapter, MessageEvent, SendResult

# QQAdapter 和 YuanbaoAdapter 以前在此处直接导入，但代码库中没有任何地方
# 使用 ``from gateway.platforms import QQAdapter`` 这样的调用（所有实际调用
# 点都使用完整路径 ``from gateway.platforms.qqbot import QQAdapter``）。
# 早期导入会引入 qqbot 的分块上传 + 键盘 + 引导机制以及
# yuanbao 的 WebSocket 栈——即使从未触及网关适配器，
# 也会在每次 CLI 调用时增加约 48ms 启动时间和约 8MB RSS 内存。
#
# 使用 PEP 562 模块级 ``__getattr__`` 保持公共重导出正常工作，
# 同时将实际导入推迟到首次属性访问。对于仍然从包根
# 导入适配器的外部代码，此方案完全向后兼容。
__all__ = [
    "BasePlatformAdapter",
    "MessageEvent",
    "SendResult",
    "QQAdapter",
    "YuanbaoAdapter",
]


def __getattr__(name):
    if name == "QQAdapter":
        from .qqbot import QQAdapter  # noqa: F401
        return QQAdapter
    if name == "YuanbaoAdapter":
        from .yuanbao import YuanbaoAdapter  # noqa: F401
        return YuanbaoAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(__all__)
