"""生产级 WebSocket RelayTransport —— gateway 到 connector 的活跃链路。

gateway 通过 WebSocket 向 connector 的 relay endpoint 拨号，并使用 connector 仓库
（``gateway-gateway`` 的 ``src/relay/protocol.ts``）中定义、并在
``docs/relay-connector-contract.md`` 中镜像的换行分隔 JSON 帧协议通信：

  gateway -> connector : hello, outbound, interrupt
  connector -> gateway : descriptor, inbound, outbound_result, interrupt_inbound

帧类型：
  hello            {type, platform, botId}
  descriptor       {type, descriptor}                       （握手回复）
  inbound          {type, event, bufferId?}                 （一个规范化的 MessageEvent）
  outbound         {type, requestId, action}                （send/edit/typing/follow_up）
  outbound_result  {type, requestId, result}
  interrupt        {type, session_key, reason?}             （gateway 流出 /stop）
  interrupt_inbound{type, session_key, chat_id}             （connector -> 所属 gateway）

这是 ``RelayTransport`` Protocol 背后的具体 transport；``RelayAdapter`` 把所有网络
I/O 委托给它。Outbound 调用以 ``requestId`` 为键阻塞在 per-request future 上，直到
匹配的 ``outbound_result`` 到达。一个后台 reader 任务把 inbound 帧推送给已注册的
handler，并 resolve 处于等待中的 outbound future。

EXPERIMENTAL：在至少两个 Class-1 平台验证通过之前，帧 schema 可能不经 deprecation
周期而变更。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.relay.descriptor import CapabilityDescriptor
from gateway.relay.transport import InboundHandler

logger = logging.getLogger(__name__)

try:  # 惰性/可选依赖 —— 与 gateway/platforms/feishu.py 一致
    import websockets
except ImportError:  # pragma: no cover - 仅在该 extra 缺失时触发
    websockets = None  # type: ignore[assignment]

WEBSOCKETS_AVAILABLE = websockets is not None

# 等待握手 descriptor 以及每个 outbound 结果的时长。
_HANDSHAKE_TIMEOUT_S = 30.0
_OUTBOUND_TIMEOUT_S = 30.0

# Phase 7 Unit 7d-B：connector 在拒绝/撤销某个 gateway 的 WS upgrade 鉴权时发送的应用
# 层关闭码（镜像 connector 的 `4401` "unauthorized" 关闭——一个私有使用码，而非标准 WS
# 码）。在成功握手之后收到的 4401 表示 per-gateway secret 已被撤销（opt-out /
# deprovision），transport 会把它当作终态处理。
_RELAY_UNAUTHORIZED_CLOSE_CODE = 4401


def _ws_dial_url(url: str) -> str:
    """把 connector URL 规范化为 ``ws(s)://…/relay`` 拨号目标。

    relay URL 一次性配置（``GATEWAY_RELAY_URL`` / ``gateway.relay_url``），作为
    connector 的 BASE URL（例如 ``https://connector.example``），同时被 provision POST
    （需要 ``http(s)://…/relay/provision``——见 ``_provision_url``）和 WS 拨号（需要
    ``ws(s)://…/relay``，即 connector 挂载其 ``WebSocketServer`` 的路径）共享。两处
    规范化都是关键的：

      - scheme：``https -> wss``、``http -> ws``（``websockets.connect`` 在 http(s) URL
        上会抛出“scheme isn't ws or wss”）。
      - path：确保以 ``/relay`` 结尾（connector 对升级到其他任何 path 都返回 HTTP 400，
        因为 WS server 挂载在 ``/relay``）。

    幂等：一个已经是 ``ws(s)://…/relay`` 的 URL 会被原样返回，因此一个连同 scheme 和/或
    ``/relay`` 一起配置的 URL 仍然有效。
    """
    raw = (url or "").strip()
    if raw.startswith("https://"):
        raw = "wss://" + raw[len("https://"):]
    elif raw.startswith("http://"):
        raw = "ws://" + raw[len("http://"):]
    raw = raw.rstrip("/")
    if not raw.endswith("/relay"):
        raw = f"{raw}/relay"
    return raw


def _event_from_wire(raw: Dict[str, Any]) -> MessageEvent:
    """从 connector 规范化的 inbound 载荷重建一个 MessageEvent。

    connector 以 snake_case 的线上形态（§3）发送 SessionSource；把它映射回 gateway 的
    dataclass。未知的消息类型回退为 TEXT。
    """
    src = raw.get("source", {}) or {}
    from gateway.config import Platform

    platform = src.get("platform", "relay")
    try:
        platform_enum = Platform(platform)
    except ValueError:
        platform_enum = Platform.RELAY

    source = SessionSource(
        platform=platform_enum,
        chat_id=src.get("chat_id", ""),
        chat_type=src.get("chat_type", "dm"),
        chat_name=src.get("chat_name"),
        user_id=src.get("user_id"),
        user_name=src.get("user_name"),
        thread_id=src.get("thread_id"),
        chat_topic=src.get("chat_topic"),
        user_id_alt=src.get("user_id_alt"),
        chat_id_alt=src.get("chat_id_alt"),
        guild_id=src.get("guild_id"),
        parent_chat_id=src.get("parent_chat_id"),
        message_id=src.get("message_id"),
        # 真实的 upstream 信任信号：本事件经由 per-instance 已鉴权的 relay WS 到达，
        # 因此 connector 已经把它解析为本实例 owner 绑定的作者。``platform`` 是底层
        # 平台（例如 discord），不是 ``relay``——authz 以本标志作为 upstream 信任决策的
        # 依据，而不是以 ``platform`` 为依据（后者会失效，因为 relay adapter 是在
        # ``Platform.RELAY`` 下注册的）。在此盖戳，绝不从线上读取。
        delivered_via_upstream_relay=True,
    )
    try:
        msg_type = MessageType(raw.get("message_type", "text"))
    except ValueError:
        msg_type = MessageType.TEXT

    return MessageEvent(
        text=raw.get("text", ""),
        message_type=msg_type,
        source=source,
        message_id=raw.get("message_id"),
        reply_to_message_id=raw.get("reply_to_message_id"),
        media_urls=raw.get("media_urls") or [],
    )


@dataclass
class PassthroughForward:
    """一个由 connector 转发的 passthrough-plane 请求（Phase 5 §5.1）。

    connector 在其边缘应答了提供商对延迟敏感的 ACK，然后经由 WS 把真实的（已消毒的）
    请求转发给本 gateway。``body`` 是 connector 转发的精确解码字节（线上以 base64 编码
    以保证字节一致）。``headers`` 保留到达顺序。
    """

    platform: str
    bot_id: str
    method: str
    path: str
    headers: list[tuple[str, str]]
    body: bytes


def _passthrough_from_wire(raw: Dict[str, Any]) -> PassthroughForward:
    """从 connector 的线上帧重建一个 PassthroughForward。

    镜像 connector 的 ``PassthroughForward``（relay/protocol.ts）：body 从 base64 解码
    回 connector 转发的精确字节，使 gateway 重新处理字节一致的内容（connector 是信任
    边界；它已在边缘完成校验）。
    """
    import base64

    body_b64 = raw.get("bodyB64", "") or ""
    try:
        body = base64.b64decode(body_b64)
    except Exception:  # noqa: BLE001 - 格式错误的 body 绝不能让 reader 崩溃
        body = b""
    headers_raw = raw.get("headers", []) or []
    headers: list[tuple[str, str]] = []
    for pair in headers_raw:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            headers.append((str(pair[0]), str(pair[1])))
    return PassthroughForward(
        platform=str(raw.get("platform", "")),
        bot_id=str(raw.get("botId", "")),
        method=str(raw.get("method", "")),
        path=str(raw.get("path", "")),
        headers=headers,
        body=body,
    )


class WebSocketRelayTransport:
    """基于 gateway 向 connector 拨号的 WebSocket 连接实现的 RelayTransport。"""

    def __init__(
        self,
        url: str,
        platform: str,
        bot_id: str,
        *,
        connect_timeout_s: float = _HANDSHAKE_TIMEOUT_S,
        outbound_timeout_s: float = _OUTBOUND_TIMEOUT_S,
        gateway_id: Optional[str] = None,
        upgrade_secret: Optional[str] = None,
        reconnect: bool = False,
        reconnect_backoff_s: float = 1.0,
        reconnect_max_backoff_s: float = 30.0,
    ) -> None:
        if not WEBSOCKETS_AVAILABLE:
            raise RuntimeError(
                "WebSocketRelayTransport requires the 'websockets' package "
                "(install the messaging extra)."
            )
        self._url = _ws_dial_url(url)
        self._platform = platform
        self._bot_id = bot_id
        self._connect_timeout_s = connect_timeout_s
        self._outbound_timeout_s = outbound_timeout_s
        # 连接鉴权（Phase 2）：当配置了 per-gateway secret 时，gateway 在 WS upgrade
        # 上附带一个 HMAC bearer，以便 connector 对其鉴权（否则以 4401 拒绝）。
        # gateway_id 标识已登记的实例——connector 读取它以索引其 secret 验证列表，再
        # 校验签名。缺省 -> 未认证 upgrade（开发/测试，或一个不强制鉴权的 connector）。
        self._gateway_id = gateway_id
        self._upgrade_secret = upgrade_secret

        # Phase 5 §5.3：一个全新的 reconnect 监督者。基础 transport 的 _read_loop 在
        # socket 关闭时只是结束（“重连是调用方的策略”）；开启 reconnect=True 后，
        # transport 会在一次意外关闭（而非主动 disconnect()）后重新拨号 + 重新握手，
        # 以便一个进入 idle/suspended 的 gateway 重新建立其 socket——这使得 connector
        # 在新握手时排空该实例 buffered-only 的 delivery-leg 积压（onResume）。默认关闭，
        # 以便现有测试 + stub 不受影响；register_relay_adapter 在生产环境中开启它。
        self._reconnect = reconnect
        self._reconnect_backoff_s = reconnect_backoff_s
        self._reconnect_max_backoff_s = reconnect_max_backoff_s
        self._supervisor: Optional[asyncio.Task[None]] = None
        # scale-to-zero §Phase 0（D12/F14）：一次 DORMANT 关闭既不同于 disconnect()
        #（终态：取消监督者），也不同于意外关闭（立即重新拨号）。go_dormant() 会把它
        # 置为 True，然后在不设置 _closing 的情况下关闭 socket——这样 _read_loop 的
        # fall-through 仍会启动 reconnect 监督者（唤醒路径保持就绪），但监督者按较长的
        # dormant 节奏等待，而非快速的 reconnect backoff，从而不会与平台的挂起窗口
        # 冲突。恢复时（进程解冻）处于等待中的 wait 完成，重新拨号成功，connector 在
        # 新握手时排空本实例的缓冲积压。在成功的重新拨号时清除（_dial_and_start）。
        self._dormant = False
        # dormant 期间的重新拨号轮询节奏。一台已挂起机器的事件循环是冻结的，因此该
        # 定时器只有在机器醒来后才会推进；它只需要足够短，以便一台刚唤醒的机器能及时
        # 重新拨号（connector 的唤醒 poke 才是最初触发平台 autostart 的因素——§3.4(5)）。
        self._dormant_redial_s = 1.0

        self._ws: Any = None
        self._reader: Optional[asyncio.Task[None]] = None
        self._inbound: Optional[InboundHandler] = None
        self._descriptor: Optional[CapabilityDescriptor] = None
        self._descriptor_ready: asyncio.Future[CapabilityDescriptor] | None = None
        # requestId -> 等待匹配 outbound_result 的 future。
        self._pending: Dict[str, asyncio.Future[Dict[str, Any]]] = {}
        # Phase 5 §5.3：等待 connector 的 going_idle_ack 的 future。
        self._going_idle_ack: asyncio.Future[None] | None = None
        self._closing = False
        # Phase 7 Unit 7d-B：在我们已经至少成功握手一次之后收到的 4401（unauthorized）
        # 关闭，意味着 connector 撤销了本 gateway 的 per-gateway secret——即运维者把本
        # 实例从 relay 中 opt-out（Unit 7b deprovision）。这是终态：secret 已失效，因此
        # 重新拨号只会对着一个死凭证无休止地空转（即 dashboard 显示的“retrying 4401”）。
        # 我们停止重连，并把它上报为干净的、不可重试的“disabled”状态。在任何成功握手
        # 之前的 4401 仍可重试——那是冷启动 / 尚未 provision 的竞态，而非撤销。
        self._handshake_succeeded = False
        self._auth_revoked = False

    # ── 生命周期 ────────────────────────────────────────────────────────
    async def connect(self) -> bool:
        await self._dial_and_start()
        return True

    async def _dial_and_start(self) -> None:
        """打开 socket、启动 reader、发送 hello。被 connect() 以及重连监督者在
        重新拨号时使用。"""
        loop = asyncio.get_running_loop()
        self._descriptor_ready = loop.create_future()
        # 一次新的握手即将到来；清除任何陈旧的 descriptor，以便 handshake() 等待新的
        # descriptor（在重新拨号时很关键）。
        self._descriptor = None
        # scale-to-zero（D12）：一次成功的（重新）拨号会结束任何 dormant 状态——我们
        # 再次活跃，因此随后的意外关闭应按正常的快速 backoff 重连，而非 dormant 节奏。
        self._dormant = False
        headers = self._upgrade_headers()
        if headers:
            self._ws = await websockets.connect(self._url, additional_headers=headers)  # type: ignore[union-attr]
        else:
            self._ws = await websockets.connect(self._url)  # type: ignore[union-attr]
        self._reader = asyncio.create_task(self._read_loop(), name="relay-ws-reader")
        # 发送 hello；descriptor 经由 reader 到达并 resolve handshake()。
        await self._send({"type": "hello", "platform": self._platform, "botId": self._bot_id})

    def _upgrade_headers(self) -> Dict[str, str]:
        """WS upgrade 的鉴权 header，未配置 secret 时返回 {}。

        附带 ``Authorization: Bearer ***，其中 token 是用 per-gateway secret
        （``gateway/relay/auth.py`` 的 ``make_upgrade_token``）签名的 bearer，以
        ``gateway_id`` 为键，以便 connector 索引其验证列表。当它缺失/无效/被撤销时
        connector 拒绝 upgrade（close 4401）；未鉴权的 connector 则忽略它。
        """
        if not (self._upgrade_secret and self._gateway_id):
            return {}
        from gateway.relay.auth import make_upgrade_token

        token = make_upgrade_token(self._gateway_id, self._upgrade_secret)
        return {"Authorization": f"Bearer {token}"}

    async def disconnect(self) -> None:
        self._closing = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - best-effort 的拆除
                pass
            self._supervisor = None
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - best-effort 的拆除
                pass
            self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        # 让任何在途的 outbound 等待者失败，以免调用方挂起。
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("relay transport closed"))
        self._pending.clear()
        if self._going_idle_ack is not None and not self._going_idle_ack.done():
            self._going_idle_ack.set_exception(RuntimeError("relay transport closed"))

    async def handshake(self) -> CapabilityDescriptor:
        if self._descriptor is not None:
            return self._descriptor
        if self._descriptor_ready is None:
            raise RuntimeError("handshake() called before connect()")
        return await asyncio.wait_for(self._descriptor_ready, timeout=self._connect_timeout_s)

    @property
    def auth_revoked(self) -> bool:
        """一旦 connector 在先前成功握手之后以 4401 关闭了 socket 即为 True——即
        per-gateway secret 已被撤销（运维者把本实例从 relay 中 opt-out）。终态：
        transport 停止重连，adapter 上报一个干净的“disabled”状态。"""
        return self._auth_revoked

    def set_inbound_handler(self, handler: InboundHandler) -> None:
        self._inbound = handler

    # ── outbound ─────────────────────────────────────────────────────────
    async def send_outbound(self, action: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request_response(action)

    async def send_follow_up(self, action: Dict[str, Any]) -> Dict[str, Any]:
        # follow_up 复用同一条 outbound 帧；connector 按 action.op 派发。保留为一个独立
        # 方法，以满足 transport Protocol，并使 A2 调用点显式。
        return await self._request_response(action)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        result = await self._request_response(
            {"op": "get_chat_info", "chat_id": chat_id}, frame_type="outbound"
        )
        # connector 在 outbound_result 信封内回复 chat-info。
        info = result.get("chat_info") or result
        return {"name": info.get("name", chat_id), "type": info.get("type", "dm")}

    async def send_interrupt(self, session_key: str, reason: Optional[str] = None) -> None:
        await self._send({"type": "interrupt", "session_key": session_key, "reason": reason})

    # ── going-idle / buffered-flip（Phase 5 §5.3）──────────────────────
    async def go_idle(self, timeout_s: float = 10.0) -> bool:
        """请求 connector 把本实例的目的地切到 buffered-only。

        发送 ``going_idle`` 并等待 connector 的 ``going_idle_ack``——这是 connector
        权威的确认，表示实时投递已停止、随后的 inbound 被持久缓冲（Q-5.3c）。收到 ack
        返回 True，超时 / 未连接返回 False（调用方无论如何都会继续关闭——最坏情况下一个
        实时事件与正在关闭的 socket 竞态，与 §5.3 之前完全一致，无回退）。

        gateway 在收到 ack 之前持续服务（read loop 继续处理 inbound），因此落在切换
        窗口内的事件会被实时投递，而非丢失。
        """
        if self._ws is None:
            return False
        loop = asyncio.get_running_loop()
        self._going_idle_ack = loop.create_future()
        try:
            await self._send({"type": "going_idle"})
            await asyncio.wait_for(self._going_idle_ack, timeout=timeout_s)
            return True
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001 - ack 是 best-effort
            return False
        finally:
            self._going_idle_ack = None

    async def go_dormant(self, timeout_s: float = 10.0) -> bool:
        """为 scale-to-zero 挂起而让本 transport 进入静默（D12 / Phase 0）。

        与 ``disconnect()`` 和意外关闭（F14）都不同：
          - ``disconnect()`` 设置 ``_closing=True`` 并取消 reconnect 监督者——终态，
            “永久关闭”。在此之后挂起的机器在唤醒时永远不会重新拨号，因此其缓冲积压会
            滞留。
          - 意外关闭会立即重新拨号（快速 backoff）——socket 永不停留在关闭状态，因此
            平台代理永远看不到连接消失，也永远不会挂起机器。

        ``go_dormant()`` 是挂起行为所需的第三种模式：
          1. ``go_idle()`` → connector 把本实例切到 buffered-only 并 ack（因此我们休眠
             期间到达的 inbound 被持久缓冲，并在下次握手时重放）。
          2. 关闭 socket，以便平台代理看到负载降到零（Fly ``autostop:"suspend"`` 的
             前置条件）——但不设置 ``_closing``。reader 正常的 end-of-socket
             fall-through 仍会就绪 reconnect 监督者，因此唤醒路径保持活跃；``_dormant``
             标志只是让该监督者按 dormant 节奏轮询，而非与挂起窗口对抗。

        恢复时（进程解冻）监督者处于等待中的 wait 完成，重新拨号成功，connector 在新
        握手时排空缓冲积压。返回 ``go_idle`` 的 ack 结果（收到 ack 为 True）；dormancy
        关闭无论如何都会发生（错失 ack 最坏也只是让一个实时事件与正在关闭的 socket 竞
        态，与 §5.3 已容忍的情形完全一致）。

        无操作安全：一个从未连接过的 transport（``_ws is None``）只会返回 False 而不
        关闭。
        """
        if self._ws is None:
            return False
        acked = await self.go_idle(timeout_s=timeout_s)
        # 在关闭之前标记为 dormant，以便监督者（由 reader 的 fall-through 就绪）采用
        # dormant 节奏，且一个竞态的实时事件不能把我们翻转回快速重连。
        self._dormant = True
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001 - best-effort；reader 仍会结束 + 就绪 reconnect
            logger.debug("relay go_dormant: ws.close() raised", exc_info=True)
        return acked

    async def _send_inbound_ack(self, buffer_id: str) -> None:
        """确认已持久接收一次缓冲的 inbound 投递（§5.3）。

        在 adapter 已持久接管一个 connector 在重连时重放的缓冲 inbound 事件之后发送；
        connector 只有在此之后才 ack 该缓冲条目，从而在 delivery leg 上做到排空且不
        重复。
        """
        try:
            await self._send({"type": "inbound_ack", "bufferId": buffer_id})
        except Exception:  # noqa: BLE001 - 失败的 ack 只会让该条目下次重新投递
            logger.debug("relay: inbound_ack send failed for %s", buffer_id)

    async def _request_response(
        self, action: Dict[str, Any], frame_type: str = "outbound"
    ) -> Dict[str, Any]:
        if self._ws is None:
            return {"success": False, "error": "relay transport not connected"}
        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Dict[str, Any]] = loop.create_future()
        self._pending[request_id] = fut
        try:
            await self._send({"type": frame_type, "requestId": request_id, "action": action})
            return await asyncio.wait_for(fut, timeout=self._outbound_timeout_s)
        except asyncio.TimeoutError:
            return {"success": False, "error": "relay outbound timed out"}
        finally:
            self._pending.pop(request_id, None)

    # ── wire I/O ─────────────────────────────────────────────────────────
    async def _send(self, frame: Dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("relay transport not connected")
        await self._ws.send(json.dumps(frame) + "\n")

    async def _read_loop(self) -> None:
        assert self._ws is not None
        buf = ""
        try:
            async for chunk in self._ws:
                buf += chunk if isinstance(chunk, str) else chunk.decode("utf-8")
                # 换行分隔的帧；保留任何尾部的不完整行。
                *lines, buf = buf.split("\n")
                for line in lines:
                    if line.strip():
                        await self._handle_frame(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 记录日志 + 让任务结束；重连在下方处理
            # Phase 7 Unit 7d-B：检测一次 4401（unauthorized）关闭。在先前成功握手之后，
            # 这是一次撤销（opt-out / deprovision）——per-gateway secret 已失效，因此重连
            # 是徒劳的。锁存一个终态的“auth revoked”状态，并不要重新拨号。在任何成功握手
            # 之前，4401 仍可重试（冷启动竞态）。
            if self._close_code_of(exc) == _RELAY_UNAUTHORIZED_CLOSE_CODE and self._handshake_succeeded:
                self._auth_revoked = True
                if not self._closing:
                    logger.warning(
                        "relay ws closed 4401 (unauthorized) after a successful handshake — "
                        "treating as a revoked relay credential (opt-out); not reconnecting"
                    )
            elif not self._closing:
                logger.warning("relay ws read loop ended: %s", exc)
        # Phase 5 §5.3：socket 已关闭。如果启用了 reconnect 且本次并非主动 disconnect()，
        # 则启动 reconnect 监督者，让 gateway 重新拨号 + 重新握手（这会触发 connector 在
        # 新握手时的 buffered-flip 排空）。自调度：reader 在此结束，监督者重新拨号并启动
        # 一个新的 reader。
        # Phase 7 Unit 7d-B：被撤销的凭证（终态 4401）是我们刻意不重连的唯一情形——
        # secret 在实例重建之前一直失效，因此空转只会重现该失败。
        if (
            self._reconnect
            and not self._closing
            and not self._auth_revoked
            and (self._supervisor is None or self._supervisor.done())
        ):
            self._supervisor = asyncio.create_task(
                self._reconnect_loop(), name="relay-ws-reconnect"
            )

    @staticmethod
    def _close_code_of(exc: BaseException) -> Optional[int]:
        """best-effort 地从抛出的异常中提取 WebSocket 关闭码。websockets 的
        ConnectionClosed* 通过 `.rcvd`/`.sent` 暴露对端的 Close 帧（优先使用；在
        websockets 13+ 中 `.code` 已弃用）。未知时返回 None。"""
        for attr in ("rcvd", "sent"):
            frame = getattr(exc, attr, None)
            fcode = getattr(frame, "code", None)
            if isinstance(fcode, int):
                return fcode
        code = getattr(exc, "code", None)
        return code if isinstance(code, int) else None

    async def _reconnect_loop(self) -> None:
        """以带封顶的指数 backoff 重新拨号 connector，直到重连成功或 disconnect() 被
        调用。§5.3 全新引入：重新建立的 socket 会让 connector 在新握手时重放本实例
        buffered-only 的积压（即 delivery-leg 的 onResume）。绝不向外抛异常（重新拨号
        失败只会重试）；在拨号成功（其 reader 接管）或进入 closing 时结束。

        scale-to-zero（D12）：当关闭是一次主动的 go_dormant() 而非意外掉线时，从
        dormant 轮询节奏开始。在一台已挂起的机器上事件循环是冻结的，因此这个 sleep 只
        有在机器醒来后才会推进——它只需要足够短，以便一台刚唤醒的机器能及时重新拨号。
        一次成功的 _dial_and_start() 会清除 _dormant，因此任何之后的意外掉线都按正常的
        快速 backoff 重连。"""
        backoff = self._dormant_redial_s if self._dormant else self._reconnect_backoff_s
        while not self._closing:
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            if self._closing:
                return
            try:
                await self._dial_and_start()
                logger.info("relay ws reconnected")
                return  # 全新的 reader 已在运行；监督者的工作完成
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 拨号失败时持续重试
                logger.warning("relay ws reconnect failed: %s", exc)
                backoff = min(backoff * 2, self._reconnect_max_backoff_s)

    async def _handle_frame(self, line: str) -> None:
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("relay: skipping malformed frame")
            return
        ftype = frame.get("type")
        if ftype == "descriptor":
            descriptor = CapabilityDescriptor.from_json(json.dumps(frame.get("descriptor", {})))
            self._descriptor = descriptor
            # Phase 7 Unit 7d-B：收到 descriptor 意味着 WS upgrade 鉴权通过且 connector
            # 接纳了我们——记录我们已至少握手一次，以便之后的 4401 关闭被解读为撤销
            #（opt-out），而非冷启动竞态。
            self._handshake_succeeded = True
            if self._descriptor_ready is not None and not self._descriptor_ready.done():
                self._descriptor_ready.set_result(descriptor)
        elif ftype == "inbound":
            if self._inbound is not None:
                event = _event_from_wire(frame.get("event", {}))
                await self._inbound(event)
                # Phase 5 §5.3：一次缓冲投递（在重连时重放）携带一个 bufferId；在 handler
                # 持久接管它之后 ack，以便 connector 推进其 delivery-leg 缓冲游标（不重复）。
                # 实时投递没有 bufferId——无需 ack。
                buffer_id = frame.get("bufferId")
                if buffer_id:
                    await self._send_inbound_ack(str(buffer_id))
        elif ftype == "going_idle_ack":
            # Phase 5 §5.3：connector 已确认我们的目的地现在是 buffered-only；resolve
            # go_idle() 正在阻塞等待的那个等待者。
            if self._going_idle_ack is not None and not self._going_idle_ack.done():
                self._going_idle_ack.set_result(None)
        elif ftype == "outbound_result":
            fut = self._pending.get(frame.get("requestId", ""))
            if fut is not None and not fut.done():
                fut.set_result(frame.get("result", {}))
        elif ftype == "interrupt_inbound":
            # 由 runner 接线桥接到 adapter 的中断路径。
            handler = getattr(self, "_interrupt_inbound_handler", None)
            if handler is not None:
                await handler(frame.get("session_key", ""), frame.get("chat_id", ""))
        elif ftype == "passthrough_forward":
            # Phase 5 §5.1：一个转发的 passthrough-plane 请求（Discord interaction、
            # Twilio 等），connector 已在边缘 ACK。它与 inbound 消息复用同一条 outbound
            # WS，因此托管 gateway 不需要公开的 inbound 端口。派发给 adapter 的 handler；
            # bufferId（存在时，§5.3 buffered flip）被传入用于 ack。
            handler = getattr(self, "_passthrough_handler", None)
            if handler is not None:
                fwd = _passthrough_from_wire(frame.get("forward", {}))
                await handler(fwd, frame.get("bufferId"))
        else:
            # hello/outbound/interrupt 都是 gateway->connector；若被回显则忽略。
            pass

    def set_interrupt_inbound_handler(self, handler: Any) -> None:
        """注册用于 connector->gateway interrupt_inbound 帧的回调。"""
        self._interrupt_inbound_handler = handler

    def set_passthrough_handler(self, handler: Any) -> None:
        """注册用于 connector->gateway passthrough_forward 帧的回调。

        镜像 set_interrupt_inbound_handler：runner/adapter 通过接线使一个转发的
        passthrough 请求（Phase 5 §5.1）经由 gateway 已持有的同一条 outbound WS 到达
        adapter。``handler(forward, buffer_id)``。
        """
        self._passthrough_handler = handler
