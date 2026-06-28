"""google_meet 插件的远程"节点主机"原语。

允许 Meet bot（Playwright + Chrome）运行在与
hermes-agent 网关不同的机器上。网关通过一个小型 JSON-over-WebSocket
RPC 协议与远程节点通信；节点包装现有的
``plugins.google_meet.process_manager`` API。

拓扑
--------
    gateway (Linux)  ── ws://mac.local:18789 ──▶  node server (Mac)
                                                  └─ process_manager
                                                     └─ meet_bot (Playwright)

原因：Google 登录 + Chrome 配置文件位于用户的笔记本电脑上。
在本地运行 bot 可以复用该配置文件，而无需将凭证传输到
服务器。

公共接口
--------------
    NodeClient     — 网关侧 RPC 客户端（每次调用使用短生命周期同步 WS）
    NodeServer     — 托管 bot 的长运行服务器
    NodeRegistry   — 已批准节点的本地 JSON 注册表（name → url+token）
    protocol       — 消息信封辅助函数（make_request、encode、decode 等）
"""

from __future__ import annotations

from plugins.google_meet.node import protocol
from plugins.google_meet.node.client import NodeClient
from plugins.google_meet.node.protocol import (
    VALID_REQUEST_TYPES,
    decode,
    encode,
    make_error,
    make_request,
    make_response,
    validate_request,
)
from plugins.google_meet.node.registry import NodeRegistry
from plugins.google_meet.node.server import NodeServer

__all__ = [
    "NodeClient",
    "NodeServer",
    "NodeRegistry",
    "protocol",
    "make_request",
    "make_response",
    "make_error",
    "encode",
    "decode",
    "validate_request",
    "VALID_REQUEST_TYPES",
]
