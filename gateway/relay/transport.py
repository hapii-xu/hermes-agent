"""Relay transport 协议 —— gateway 与 connector 之间的线上契约。EXPERIMENTAL。

``RelayAdapter``（gateway 侧）把所有网络 I/O 委托给一个 ``RelayTransport``。gateway
是向 connector 拨号出去的，因此生产环境的 transport 是一个 WebSocket 客户端；在测试中
它是一个内存中的 stub（``tests/gateway/relay/stub_connector.py``）。

本模块只定义协议接口——没有具体的 transport。该契约涉及四个方面：

  1. 生命周期：``connect`` / ``disconnect``。
  2. 握手：``handshake`` 返回 connector 为本 adapter 代理的平台所声明的能力
     ``CapabilityDescriptor``。
  3. Inbound：``set_inbound_handler`` 注册一个回调，transport 会在 connector 投递每个
     规范化的 ``MessageEvent`` 时调用它。
  4. Outbound：``send_outbound`` 把 send/edit/typing 动作回传给 connector；
     ``get_chat_info`` 代理一次 chat-info 查询；``send_interrupt`` 把回合内的 /stop
     沿拥有该 session_key 的 socket 路由出去。

EXPERIMENTAL：在 >=2 个 Class-1 平台验证通过之前，可能不经 deprecation 周期而变更。见
docs/relay-connector-contract.md。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional, Protocol, runtime_checkable

from gateway.platforms.base import MessageEvent
from gateway.relay.descriptor import CapabilityDescriptor

# transport 在每个规范化的 inbound 事件上调用的回调。
InboundHandler = Callable[[MessageEvent], Awaitable[None]]

# transport 在每个转发的 passthrough 请求上调用的回调（§5.1）。第一个参数是一个
# PassthroughForward（gateway/relay/ws_transport.py）——此处以 Any 标注，以保持本
# protocol 模块不依赖具体 transport 的导入（ws_transport 是反向依赖本模块的）。第二个
# 参数是可选的 bufferId（Phase 5 §5.3 buffered flip），handler 在持久接管后对其进行
# ack。
PassthroughHandler = Callable[[Any, Optional[str]], Awaitable[None]]


@runtime_checkable
class RelayTransport(Protocol):
    """完整的 gateway<->connector transport 契约。"""

    async def connect(self) -> bool:
        """打开到 connector 的连接；成功返回 True。"""
        ...

    async def disconnect(self) -> None:
        """关闭连接。"""
        ...

    async def handshake(self) -> CapabilityDescriptor:
        """返回 connector 声明的能力 descriptor。"""
        ...

    def set_inbound_handler(self, handler: InboundHandler) -> None:
        """注册在每条 inbound MessageEvent 上调用的回调。"""
        ...

    def set_passthrough_handler(self, handler: "PassthroughHandler") -> None:
        """注册在每个转发的 passthrough 请求上调用的回调。

        Phase 5 §5.1：passthrough plane（Discord interactions、Twilio 等）在 connector
        处应答提供商的边缘 ACK，然后经由同一条 outbound socket 把真实请求转发给
        gateway（托管 gateway 没有公开的 inbound 端口）。transport 会在每个
        ``passthrough_forward`` 帧上调用 ``handler(forward, buffer_id)``。对 transport
        而言是可选的（内存中的 stub 可能不实现它）。
        """
        ...

    async def send_outbound(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """把一个 outbound 动作（send/edit/typing）传给 connector。

        返回一个结果 dict；对于 ``op == "send"``，它携带 ``success`` 以及可选的
        ``message_id`` / ``error``。
        """
        ...

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """向 connector 代理一次 chat-info 查询。"""
        ...

    async def send_interrupt(self, session_key: str, reason: Optional[str] = None) -> None:
        """把回合内的 /stop 路由给 connector，作用于 ``session_key``。

        connector 把它沿运行该 session 的 gateway 实例所拥有的 socket 转发下去（即
        /stop 路由不变式）。在 gateway 侧这是 OUTBOUND 方向；真正的任务取消发生在
        connector 回显一个 interrupt inbound 时（在 Task 1.4 中处理）。
        """
        ...

    async def go_idle(self, timeout_s: float = 10.0) -> bool:
        """请求 connector 把本实例切到 buffered-only（Phase 5 §5.3）。

        发送 ``going_idle`` 并等待 connector 的 ``going_idle_ack``——这是 connector 权威
        的确认，表示实时投递已停止、inbound 现在被持久缓冲以便重连时重放（Q-5.3c）。收到
        ack 返回 True，超时 / 未连接返回 False（调用方无论结果都会继续关闭；在没有 §5.3
        接线的情况下就只是没有缓冲）。对 transport 而言是可选的（内存中的 stub 可能不
        实现它）。作为 gateway 现有 drain 迁移的一部分发送——而非一条新的 idle 路径。
        """
        ...

    async def send_follow_up(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """对一个绑定到 session 的共享身份能力进行操作（A2 outbound）。

        某些平台会给 connector 一个作用于 SHARED bot 身份的凭证（例如 Discord
        interaction 的 follow-up token，有效期约 15 分钟）。在 A2 下该凭证永不到达
        gateway——connector 在边缘剥离了它，并按 session 为键绑定到其能力 vault 中。要
        使用它，gateway 对自己已在的 session 发起一个 SEMANTIC 动作；它从不命名或持有
        token。

        action dict 携带：
          ``op``          == ``"follow_up"``
          ``session_key`` 要使用其绑定能力的 session
          ``kind``        能力种类（例如 ``"discord.interaction_token"``）
          ``content``     要通过该能力发送的消息内容
          ``metadata?``   可选的附加内容

        connector 解析出真实能力（其侧的 ``resolveOutboundCapability``），强制租户匹配
        （租户 B 永远无法使用租户 A 的能力），然后流出。返回
        ``{success, message_id?, error?}``；当能力缺失/过期或租户不匹配时 ``success``
        为 False——此时 gateway 没有任何可以重试的依据（设计如此：一个泄露的 gateway
        不持有任何能力材料）。
        """
        ...
