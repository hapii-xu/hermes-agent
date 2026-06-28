"""RelayAdapter —— 一个由 connector 代理、通用的 gateway adapter。EXPERIMENTAL。

它是一个 ``BasePlatformAdapter`` 子类，在握手时从 connector 接收一个
``CapabilityDescriptor``，告知它正在代理哪个平台、要向 ``GatewayStreamConsumer``
声明哪些能力。它实现了四个抽象方法（``connect`` / ``disconnect`` / ``send``
/ ``get_chat_info``）以及能力接口（``MAX_MESSAGE_LENGTH``、``message_len_fn``、
``supports_draft_streaming``），具体做法是把网络 I/O 委托给注入的 transport，并从
descriptor 上读取能力。

这里没有任何 per-platform 的 gateway 代码：只有 connector 这一方知道“这个 chat_id
对应一个 Discord channel，通过 Discord websocket 发送它”。gateway 只看到一个普通
的 ``MessageEvent`` 进来，并调用 ``adapter.send`` 发出去。

EXPERIMENTAL：在 >=2 个 Class-1 平台验证通过之前，transport 协议和 descriptor
schema 可能不经 deprecation 周期而变更。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.relay.descriptor import CapabilityDescriptor
from gateway.relay.transport import RelayTransport
from gateway.session import SessionSource

logger = logging.getLogger(__name__)


def _utf16_len(text: str) -> int:
    """统计 UTF-16 code unit 数量（Telegram 的长度单位）。"""
    return len(text.encode("utf-16-le")) // 2


# 根据 descriptor 的 ``len_unit`` 做表驱动的长度单位选择。
_LEN_FNS: Dict[str, Callable[[str], int]] = {
    "chars": len,
    "utf16": _utf16_len,
}


class RelayAdapter(BasePlatformAdapter):
    """通用的 relay adapter，声明一个由 connector 协商出的能力画像。"""

    def __init__(
        self,
        config: PlatformConfig,
        descriptor: CapabilityDescriptor,
        transport: Optional[RelayTransport] = None,
    ) -> None:
        # relay adapter 代理多个平台，但对 runner 表现为一个单一逻辑平台；
        # Platform.RELAY 即用于标识它。
        super().__init__(config, Platform.RELAY)
        self.descriptor = descriptor
        self._transport = transport
        # 被 stream_consumer 读取的能力接口（getattr(..., 4096)）。
        self.MAX_MESSAGE_LENGTH = descriptor.max_message_length
        # chat_id -> guild_id（Discord）/ workspace scope，从 inbound 事件中学习得到。
        # connector 的 egress guard 从 OUTBOUND action 的 metadata.guild_id 解析所属
        # 租户；而 gateway 的通用投递路径（run.py 的 _thread_metadata_for_source）只
        # 携带 thread_id，因此我们在这里依据 inbound 看到的内容重新附上 scope。以
        # chat_id（channel）为键，因为 send() 收到的就是它。见 routedEgressGuard.ts。
        self._scope_by_chat: Dict[str, str] = {}
        # chat_id -> DM channel（无 guild_id）的作者 user_id。DM 回复没有 guild
        # 判别符，因此 connector 从收件人的 author binding 解析其租户；我们把该
        # user_id 作为 metadata.user_id 重新附在 outbound action 上，以便它能完成
        # 解析。见 _capture_scope。
        self._dm_user_by_chat: Dict[str, str] = {}
        self.supports_code_blocks = descriptor.markdown_dialect not in ("", "plain")
        # Phase 7 Unit 7d-B：监视 transport 是否出现终态 auth 撤销（在成功握手后的一次
        # 4401 关闭 = 运维者把本实例从 relay 中 opt-out）。发生撤销时我们上报一个
        # 干净的、不可重试的“relay disabled”致命错误，使 dashboard 不再针对一个已失效
        # 的凭证显示红色的“retrying”转圈。
        self._revocation_monitor: Optional[asyncio.Task[None]] = None

    # ── 能力接口（来自 descriptor）─────────────────────────────────────
    @property
    def authorization_is_upstream(self) -> bool:
        """relay 的授权由 connector 执行，而非在本地执行。

        connector 认证本 gateway 的 WS（per-instance secret），并在投递前执行仅 owner
        的 author-binding 解析，因此任何 inbound relay 事件都已被授权为“本实例绑定的
        用户”（``user_instance_binding``，以 connector 观察到的 author id 为键）。
        因此实例绝不能因为缺少本地 ``RELAY_ALLOWED_USERS`` 环境变量 allowlist 而
        默认拒绝 relay 用户。见 ``BasePlatformAdapter.authorization_is_upstream``。
        """
        return True

    @property
    def message_len_fn(self) -> Callable[[str], int]:
        return _LEN_FNS.get(self.descriptor.len_unit, len)

    def supports_draft_streaming(
        self,
        chat_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        return self.descriptor.supports_draft_streaming

    # ── 抽象方法（委托给 transport）────────────────────────────────────
    async def connect(self) -> bool:
        if self._transport is None:
            raise RuntimeError("RelayAdapter has no transport configured")
        self._transport.set_inbound_handler(self._on_inbound)
        # inbound 中断（connector -> 所属 gateway）作为 interrupt_inbound 帧，经由
        # 同一条 outbound WS 到达；将它们桥接到 adapter 的中断路径。仅限 WS：没有
        # inbound HTTP receiver。
        set_interrupt = getattr(self._transport, "set_interrupt_inbound_handler", None)
        if callable(set_interrupt):
            set_interrupt(self.on_interrupt)
        # Passthrough-plane 的转发（Discord interactions、Twilio 等）也经由同一条
        # outbound WS（Phase 5 §5.1）——connector 在边缘 ACK 后把真实请求转发到此处，
        # 因此托管 gateway 不需要公开的 inbound 端口。将它们桥接到 adapter 的
        # passthrough handler。
        set_passthrough = getattr(self._transport, "set_passthrough_handler", None)
        if callable(set_passthrough):
            set_passthrough(self._on_passthrough)
        ok = await self._transport.connect()
        if not ok:
            return False
        # 从 connector 协商出真实的能力 descriptor 并采用它——构造时传入的占位符会被
        # 替换为 connector 为本 gateway 实际代理的平台所声明的内容。
        try:
            descriptor = await self._transport.handshake()
        except Exception as exc:  # noqa: BLE001 - 握手失败 = 连接失败
            logger.warning("relay handshake failed: %s", exc)
            return False
        self._apply_descriptor(descriptor)
        # inbound（消息 + 中断）经由 connector 的 relay bus 通过 outbound WS 投递——
        # 没有 inbound HTTP endpoint（托管 gateway 没有公开 IP）。transport 的 reader
        # 已经把 `inbound` / `interrupt_inbound` 帧派发到上面接好的 handler。
        # Phase 7 Unit 7d-B：开始监视终态 auth 撤销（opt-out）。仅当 transport 暴露了
        # `auth_revoked`（生产环境的 WebSocket transport）时才有意义；测试/stub transport
        # 没有该方法。
        if hasattr(self._transport, "auth_revoked"):
            self._start_revocation_monitor()
        return True

    def _start_revocation_monitor(self) -> None:
        """（一次性）启动一个任务，把 transport 的 auth 撤销转换为一个干净的、不可
        重试的“relay disabled”致命错误。幂等。"""
        if self._revocation_monitor is not None and not self._revocation_monitor.done():
            return
        try:
            self._revocation_monitor = asyncio.create_task(
                self._watch_for_revocation(), name="relay-revocation-monitor"
            )
        except RuntimeError:
            # 没有正在运行的事件循环（例如某个单元测试通过 stub 同步调用 connect()）
            # —— 无需监视。
            self._revocation_monitor = None

    async def _watch_for_revocation(self, poll_interval_s: float = 1.0) -> None:
        """轮询 transport 是否出现终态 4401 撤销（opt-out）。发生撤销时，上报一个
        不可重试的 `relay_disabled` 致命错误，使 dashboard 渲染干净的“Relay disabled”
        状态而非红色的“retrying”转圈，并通知 gateway 的致命错误 handler 以便干净地
        移除该 adapter（它不会被排入重连队列，因为凭证在实例重建之前一直处于失效
        状态）。"""
        transport = self._transport
        try:
            while True:
                if transport is None or getattr(transport, "auth_revoked", False):
                    break
                await asyncio.sleep(poll_interval_s)
        except asyncio.CancelledError:
            raise
        if transport is None or not getattr(transport, "auth_revoked", False):
            return
        logger.warning(
            "relay credential revoked (opt-out) — marking the relay adapter disabled"
        )
        # 不可重试：被撤销的 secret 在重建之前永远不会恢复，因此
        # _handle_adapter_fatal_error 绝不能把它排入重连队列。
        self._set_fatal_error(
            "relay_disabled",
            "Relay disabled (opted out — recreate the instance to re-enable)",
            retryable=False,
        )
        try:
            await self._notify_fatal_error()
        except Exception:  # noqa: BLE001 - 通知是 best-effort
            logger.debug("relay revocation fatal-error notify failed", exc_info=True)

    def _apply_descriptor(self, descriptor: CapabilityDescriptor) -> None:
        """把（重新）协商出的 descriptor 采用到当前活跃的能力接口中。"""
        self.descriptor = descriptor
        self.MAX_MESSAGE_LENGTH = descriptor.max_message_length
        self.supports_code_blocks = descriptor.markdown_dialect not in ("", "plain")

    async def _on_inbound(self, event) -> None:
        """把一个由 connector 投递的 MessageEvent 桥接到正常的 adapter 路径。"""
        self._capture_scope(event)
        await self.handle_message(event)

    def _capture_scope(self, event) -> None:
        """从 inbound 事件中记住某个 chat_id 的 egress 判别符，以便我们的 outbound
        （agent 的回复）能为 connector 的 egress 租户解析重新断言它。绝不抛异常——
        scope 跟踪绝不能破坏 inbound。

        两种情形，对应 connector 的两条租户解析路径：
          - GUILD 消息：记住 chat_id -> guild_id。connector 从 metadata.guild_id
            （路由表）解析租户。
          - DM（无 guild_id）：记住 chat_id -> 真实的作者 user_id。DM 不携带 guild
            判别符，因此 connector 改为从收件人的 author binding（resolveByUser）解析
            租户；它需要 OUTBOUND action 上的 user_id 才能完成解析。缺少它的话，DM
            回复就没有可解析的判别符，connector 的 egress guard 会以“target not routed
            to an onboarded tenant”为由拒绝。见 gateway-gateway 的
            routedEgressGuard.ts / discordTenantOf。
        """
        try:
            src = getattr(event, "source", None)
            if not src:
                return
            chat = getattr(src, "chat_id", None)
            if not chat:
                return
            guild = getattr(src, "guild_id", None)
            if guild:
                self._scope_by_chat[str(chat)] = str(guild)
                return
            # DM：没有 guild_id。记住真实的作者 id，用于 outbound 的 author-binding
            # 解析（即我们在该 DM 中回复的用户）。
            user_id = getattr(src, "user_id", None)
            if user_id:
                self._dm_user_by_chat[str(chat)] = str(user_id)
        except Exception:  # noqa: BLE001 - scope 跟踪绝不能破坏 inbound
            pass

    def _with_scope(self, chat_id: str, metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """确保 outbound metadata 携带 connector 的 egress guard 解析所属租户所需的
        判别符。两种情形：

          - GUILD 回复：重新附上 metadata.guild_id（路由表解析）。
          - DM 回复：没有 guild_id，因此重新附上 metadata.user_id——我们在 inbound 时
            看到的真实作者 id——connector 通过收件人的 author binding（resolveByUser）
            将其解析为租户。缺少其中任一个，egress 都会以“target not routed to an
            onboarded tenant”被拒绝。见 gateway-gateway 的 routedEgressGuard.ts /
            discordTenantOf。

        当相关值已存在或对该 chat 未知时为无操作。
        """
        meta: Dict[str, Any] = dict(metadata or {})
        if not meta.get("guild_id"):
            scope = self._scope_by_chat.get(str(chat_id))
            if scope:
                meta["guild_id"] = scope
        # DM 的 author-binding 判别符。仅在无 guild 时才有意义（guild 回复按 guild_id
        # 解析）；其他情况下携带它也无害，但我们只在该 chat 是已知 DM 且字段缺失时才
        # 设置它。
        if not meta.get("guild_id") and not meta.get("user_id"):
            dm_user = self._dm_user_by_chat.get(str(chat_id))
            if dm_user:
                meta["user_id"] = dm_user
        return meta

    async def on_interrupt(self, session_key: str, chat_id: str) -> None:
        """把 connector 投递的 /stop 桥接到 adapter 的中断路径。

        connector 把一次回合内的中断沿由运行 ``session_key`` 的 gateway 实例所拥有的
        socket 转发下来；本方法把它路由到现有的 per-session 中断机制（设置
        ``_active_sessions[session_key]`` 的 Event 并清除 typing），取消正确的回合而
        不影响兄弟 session。
        """
        await self.interrupt_session_activity(session_key, chat_id)

    async def _on_passthrough(self, forward, buffer_id: Optional[str] = None) -> None:
        """处理一个由 connector 转发的 passthrough 请求（Phase 5 §5.1）。

        passthrough plane（Discord interactions、Twilio webhooks 等）在 connector 的
        EDGE 回复提供商对延迟敏感的 ACK，然后经由 outbound WS 把真实的、已消毒
        （SANITIZED）的请求转发给本 gateway。connector 是信任边界：它在边缘验证了
        提供商签名，并把任何共享身份凭证（例如 Discord interaction 的 follow-up
        token）剥离到它自己的 vault 中——因此该 body 不携带任何 token，agent 随后通过
        无 token 的 ``follow_up`` 路径（``send_follow_up``）对其操作，从不持有该凭证。

        对于 Discord interaction，我们解码（JSON）body 并把它转换成规范化的
        ``MessageEvent``，使其流经与聊天消息（``handle_message``）相同的 agent 路径；
        agent 的回复经由正常的 outbound/follow_up 路径流出。非 JSON 或非 interaction
        的转发目前只会被记录并丢弃（通过 relay 的 Twilio/SMS 是后续单元的工作）。

        绝不抛异常：一个格式错误的转发绝不能杀死 read loop。

        NOTE（开放语义子设计，已标记待评审）：下方的 interaction -> MessageEvent 映射
        是 v1 默认行为。slash-command / button interaction（相对于普通消息）的确切
        agent UX——命令名呈现、选项渲染、deferred 还是立即响应——是规格中跟踪的开放
        部分；而 TRANSPORT + 接收机制（整条路径）已经定型。
        """
        try:
            platform = getattr(forward, "platform", "") or ""
            if platform == "discord":
                event = self._discord_interaction_to_event(forward)
                if event is not None:
                    self._capture_scope(event)
                    await self.handle_message(event)
                    return
            logger.info(
                "relay passthrough_forward dropped (no handler): platform=%s method=%s path=%s",
                platform,
                getattr(forward, "method", "?"),
                getattr(forward, "path", "?"),
            )
        except Exception:  # noqa: BLE001 - a bad forward must never break the reader
            logger.warning("relay passthrough_forward handling failed", exc_info=True)

    def _discord_interaction_to_event(self, forward):
        """把转发的 Discord interaction body 转换为 MessageEvent，无法转换时返回 None。

        以与 connector 处理 interaction 相同的方式（connector 侧的
        ``interactionSessionSource``）构建 session source，使 agent 的 session key 与
        connector 绑定 follow-up 能力的那个 key 一致。当 body 不是一个可用的
        interaction（例如 PING，connector 已在边缘应答并永不转发）时返回 None。
        """
        import json

        from gateway.platforms.base import MessageType

        try:
            payload = json.loads(bytes(getattr(forward, "body", b"")).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(payload, dict):
            return None
        # type 1 = PING（在边缘应答，永不转发）；2 = APPLICATION_COMMAND；
        # 3 = MESSAGE_COMPONENT；5 = MODAL_SUBMIT。给出一个 best-effort 的文本。
        itype = payload.get("type")
        data = payload.get("data") or {}
        if itype == 2:
            text = str(data.get("name") or "")
        elif itype == 3:
            text = str(data.get("custom_id") or "")
        else:
            text = ""
        member = payload.get("member") or {}
        user = (member.get("user") if isinstance(member, dict) else None) or payload.get("user") or {}
        channel_id = str(payload.get("channel_id") or "")
        guild_id = payload.get("guild_id")
        source = SessionSource(
            platform=Platform.RELAY,
            chat_id=channel_id,
            chat_type="channel" if guild_id else "dm",
            user_id=str(user.get("id")) if isinstance(user, dict) and user.get("id") else None,
            user_name=str(user.get("username")) if isinstance(user, dict) and user.get("username") else None,
            guild_id=str(guild_id) if guild_id else None,
            message_id=str(payload.get("id")) if payload.get("id") else None,
        )
        return MessageEvent(text=text, message_type=MessageType.TEXT, source=source)

    async def disconnect(self) -> None:
        # Phase 7 Unit 7d-B：先停止撤销监视器，使其不会在主动拆除期间/之后触发一个
        # 虚假的致命错误。
        if self._revocation_monitor is not None:
            self._revocation_monitor.cancel()
            try:
                await self._revocation_monitor
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - best-effort 的拆除
                pass
            self._revocation_monitor = None
        if self._transport is not None:
            # Phase 5 §5.3：作为 gateway 现有 drain/shutdown 迁移的一部分发送 going_idle
            #（当 gateway 进入 `draining` 时 runner 会调用 adapter.disconnect()）。在
            # 拆除 socket 之前请求 connector 把本实例切到 buffered-only，意味着我们休眠
            # 期间到达的 inbound 会被持久缓冲，并在重连时重放，而不是被推送给一个正在
            # 关闭的 socket。connector 是权威方（它对切换 ack）；我们持续服务直到收到
            # ack（Q-5.3c）。best-effort + 带守卫：一个没有 go_idle 的 transport（stub）
            # 或一次失败/超时的 ack 都不能阻塞 shutdown——我们像以前一样继续 disconnect，
            # 没有回退。
            go_idle = getattr(self._transport, "go_idle", None)
            if callable(go_idle):
                try:
                    result: Any = go_idle()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:  # noqa: BLE001 - going-idle 是一项优化，绝不阻塞 drain
                    logger.debug("relay going_idle failed during drain", exc_info=True)
            await self._transport.disconnect()

    async def go_dormant(self) -> bool:
        """为 scale-to-zero 挂起而让 relay 进入静默（D12 / Phase 0）。

        与 ``disconnect()``（用于 shutdown/restart 的终态拆除）不同，本方法保持
        adapter 的重连路径处于就绪状态，以便机器唤醒时 gateway 重新拨号并排空其缓冲
        的积压消息。可用时委托给 transport 的 ``go_dormant()``；没有该方法的 transport
       （stub）是无操作并返回 False，因此调用方能安全降级。

        NOTE：刻意不停止撤销监视器——进入 dormant 并非拆除；监视器保持存活，以便在
        静默期间发生的真实 opt-out/撤销在唤醒时仍能被上报。
        """
        if self._transport is None:
            return False
        go_dormant = getattr(self._transport, "go_dormant", None)
        if not callable(go_dormant):
            return False
        try:
            result: Any = go_dormant()
            if asyncio.iscoroutine(result):
                return bool(await result)
            return bool(result)
        except Exception:  # noqa: BLE001 - dormancy 是 best-effort，绝不阻塞 idle 路径
            logger.debug("relay go_dormant failed", exc_info=True)
            return False

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if self._transport is None:
            return SendResult(success=False, error="no transport")
        result = await self._transport.send_outbound(
            {
                "op": "send",
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": self._with_scope(chat_id, metadata),
            }
        )
        return SendResult(
            success=bool(result.get("success")),
            message_id=result.get("message_id"),
            error=result.get("error"),
        )

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        # 代理给 connector（它拥有平台连接 / 缓存）。
        if self._transport is None:
            return {"name": chat_id, "type": "dm"}
        return await self._transport.get_chat_info(chat_id)

    async def send_follow_up(
        self,
        session_key: str,
        kind: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """通过一个绑定到 session 的共享身份能力发送（A2 outbound）。

        gateway 永不持有凭证：它只声明自己已在的 session 以及能力 ``kind``，由
        connector 从其 vault 解析出真实值并流出（同时强制租户匹配）。例如用于以共享
        bot 身份发送一条 Discord interaction follow-up，而 token 永不到达 gateway。见
        RelayTransport.send_follow_up。
        """
        if self._transport is None:
            return SendResult(success=False, error="no transport")
        result = await self._transport.send_follow_up(
            {
                "op": "follow_up",
                "session_key": session_key,
                "kind": kind,
                "content": content,
                "metadata": metadata or {},
            }
        )
        return SendResult(
            success=bool(result.get("success")),
            message_id=result.get("message_id"),
            error=result.get("error"),
        )
