"""
使用官方 QQ Bot API (v2) 的 QQ Bot 平台适配器。

通过 QQ Bot WebSocket Gateway 接收入站事件，并使用
REST API（``api.sgroup.qq.com``）发送消息和上传媒体文件。

config.yaml 中的配置：
    platforms:
      qq:
        enabled: true
        extra:
          app_id: "your-app-id"            # 或使用 QQ_APP_ID 环境变量
          client_secret: "your-secret"     # 或使用 QQ_CLIENT_SECRET 环境变量
          markdown_support: true           # 启用 QQ markdown（msg_type 2）
          dm_policy: "open"                # open | allowlist | disabled
          allow_from: ["openid_1"]
          group_policy: "open"             # open | allowlist | disabled
          group_allow_from: ["group_openid_1"]
          stt:                             # 语音转文字配置（可选）
            provider: "zai"                # zai（GLM-ASR）、openai（Whisper）等
            baseUrl: "https://open.bigmodel.cn/api/coding/paas/v4"
            apiKey: "your-stt-api-key"     # 或设置 QQ_STT_API_KEY 环境变量
            model: "glm-asr"               # glm-asr、whisper-1 等

    语音转写优先级：
      1. QQ 内置的 ``asr_refer_text``（腾讯 ASR — 免费，总是优先尝试）
      2. 通过 ``stt`` 配置或 ``QQ_STT_*`` 环境变量配置的 STT 提供者

参考文档：https://bot.q.qq.com/wiki/develop/api-v2/
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    aiohttp = None  # type: ignore[assignment]

try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    _ssrf_redirect_guard,
    cache_document_from_bytes,
    cache_image_from_bytes,
)
from gateway.platforms.helpers import strip_markdown

logger = logging.getLogger(__name__)


class QQCloseError(Exception):
    """当 QQ WebSocket 以特定关闭码断开时抛出。

    携带关闭码和原因，以便在重连循环中正确处理。
    """

    def __init__(self, code, reason=""):
        self.code = int(code) if code else None
        self.reason = str(reason) if reason else ""
        super().__init__(f"WebSocket closed (code={self.code}, reason={self.reason})")


# ---------------------------------------------------------------------------
# 常量 — 从共享 constants 模块导入。
# ---------------------------------------------------------------------------

from gateway.platforms.qqbot.constants import (
    API_BASE,
    TOKEN_URL,
    GATEWAY_URL_PATH,
    DEFAULT_API_TIMEOUT,
    FILE_UPLOAD_TIMEOUT,
    CONNECT_TIMEOUT_SECONDS,
    RECONNECT_BACKOFF,
    MAX_RECONNECT_ATTEMPTS,
    RATE_LIMIT_DELAY,
    QUICK_DISCONNECT_THRESHOLD,
    MAX_QUICK_DISCONNECT_COUNT,
    MAX_MESSAGE_LENGTH,
    DEDUP_WINDOW_SECONDS,
    DEDUP_MAX_SIZE,
    MSG_TYPE_TEXT,
    MSG_TYPE_MARKDOWN,
    MSG_TYPE_MEDIA,
    MSG_TYPE_INPUT_NOTIFY,
    MEDIA_TYPE_IMAGE,
    MEDIA_TYPE_VIDEO,
    MEDIA_TYPE_VOICE,
    MEDIA_TYPE_FILE,
)
from gateway.platforms.qqbot.utils import (
    coerce_list as _coerce_list_impl,
    build_user_agent,
)
from gateway.platforms.qqbot.chunked_upload import (
    ChunkedUploader,
    UploadDailyLimitExceededError,
    UploadFileTooLargeError,
)
from gateway.platforms.qqbot.keyboards import (
    ApprovalRequest,
    InlineKeyboard,
    InteractionEvent,
    build_approval_keyboard,
    build_update_prompt_keyboard,
    parse_approval_button_data,
    parse_interaction_event,
    parse_update_prompt_button_data,
)


def check_qq_requirements() -> bool:
    """检查 QQ 运行时依赖是否可用。"""
    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE


def _coerce_list(value: Any) -> List[str]:
    """将配置值强制转换为去空白后的字符串列表。"""
    return _coerce_list_impl(value)


# ---------------------------------------------------------------------------
# QQAdapter
# ---------------------------------------------------------------------------


class QQAdapter(BasePlatformAdapter):
    """基于官方 QQ Bot WebSocket Gateway + REST API 的 QQ Bot 适配器。"""

    # QQ Bot API 不支持编辑已发送的消息。
    SUPPORTS_MESSAGE_EDITING = False
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    _TYPING_INPUT_SECONDS = 60  # 上报给 QQ 的 input_notify 时长
    _TYPING_DEBOUNCE_SECONDS = 50  # 在过期前刷新

    @property
    def _log_tag(self) -> str:
        """日志前缀，包含 app_id 以便区分多实例。"""
        app_id = getattr(self, "_app_id", None)
        if app_id:
            return f"QQBot:{app_id}"
        return "QQBot"

    def _fail_pending(self, reason: str) -> None:
        """使所有待处理的响应 future 失败。"""
        for fut in self._pending_responses.values():
            if not fut.done():
                fut.set_exception(RuntimeError(reason))
        self._pending_responses.clear()

    def _mark_transport_disconnected(self) -> None:
        """将 QQ WS 标记为断开，但不停止重连循环。

        BasePlatformAdapter 使用 _running 同时表示进程生命周期和连接状态。
        QQBot 需要在短暂传输断开期间保持监听任务存活，以便在短暂的
        gateway 或网络故障后能继续尝试重连。
        """
        if self.has_fatal_error:
            return
        self._write_runtime_status_safe(
            "disconnected",
            platform_state="disconnected",
            error_code=None,
            error_message=None,
        )

    @property
    def is_connected(self) -> bool:
        """仅当 QQ WebSocket 传输可用时返回 True。"""
        return bool(self._running and self._ws and not self._ws.closed)

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.QQBOT)

        extra = config.extra or {}
        self._app_id = str(extra.get("app_id") or os.getenv("QQ_APP_ID", "")).strip()
        self._client_secret = str(
            extra.get("client_secret") or os.getenv("QQ_CLIENT_SECRET", "")
        ).strip()
        self._markdown_support = bool(extra.get("markdown_support", True))

        # 认证/访问控制策略
        self._dm_policy = str(extra.get("dm_policy", "open")).strip().lower()
        self._allow_from = _coerce_list(
            extra.get("allow_from") or extra.get("allowFrom")
        )
        self._group_policy = str(extra.get("group_policy", "open")).strip().lower()
        self._group_allow_from = _coerce_list(
            extra.get("group_allow_from") or extra.get("groupAllowFrom")
        )

        # 连接状态
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._heartbeat_interval: float = 30.0  # 秒，由 Hello 更新
        self._session_id: Optional[str] = None
        self._last_seq: Optional[int] = None
        self._chat_type_map: Dict[str, str] = {}  # chat_id → "c2c"|"group"|"guild"|"dm"

        # 请求/响应关联
        self._pending_responses: Dict[str, asyncio.Future] = {}
        self._seen_messages: Dict[str, float] = {}

        # 每个会话最后入站消息 ID — 供 send_typing 使用
        self._last_msg_id: Dict[str, str] = {}
        # 输入指示去抖：chat_id → 上次 send_typing 时间戳
        self._typing_sent_at: Dict[str, float] = {}

        # token 缓存
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

        # 上传缓存：content_hash -> {file_info, file_uuid, expires_at}
        self._upload_cache: Dict[str, Dict[str, Any]] = {}

        # 内联键盘交互路由。该回调（如已设置）会在适配器对每个
        # INTERACTION_CREATE 事件 ACK 之后被调用。调用方（审批/更新提示的
        # gateway 装配代码）通过 set_interaction_callback() 注册。
        self._interaction_callback: Optional[
            Callable[[InteractionEvent], Awaitable[None]]
        ] = None

        # 默认交互分发器：将审批按钮点击路由到
        # tools.approval.resolve_gateway_approval()，将更新提示按钮点击路由到
        # ~/.hermes/.update_response。在此设置以便跨适配器的 gateway
        # 契约（send_exec_approval / send_update_prompt）开箱即用；
        # 调用方可通过 set_interaction_callback(None) 覆盖，或注册自定义 handler。
        self._interaction_callback = self._default_interaction_dispatch

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "QQBot"

    @property
    def enforces_own_access_policy(self) -> bool:
        """QQBot 在入口处通过 dm_policy/group_policy 控制私聊/群聊访问。"""
        return True

    # ------------------------------------------------------------------
    # 连接生命周期
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """认证、获取 gateway URL 并打开 WebSocket。"""
        if not AIOHTTP_AVAILABLE:
            message = "QQ startup failed: aiohttp not installed"
            self._set_fatal_error("qq_missing_dependency", message, retryable=True)
            logger.warning("[%s] %s. Run: pip install aiohttp", self._log_tag, message)
            return False
        if not HTTPX_AVAILABLE:
            message = "QQ startup failed: httpx not installed"
            self._set_fatal_error("qq_missing_dependency", message, retryable=True)
            logger.warning("[%s] %s. Run: pip install httpx", self._log_tag, message)
            return False
        if not self._app_id or not self._client_secret:
            message = "QQ startup failed: QQ_APP_ID and QQ_CLIENT_SECRET are required"
            self._set_fatal_error("qq_missing_credentials", message, retryable=True)
            logger.warning("[%s] %s", self._log_tag, message)
            return False

        # 防止使用相同凭据重复连接
        if not self._acquire_platform_lock("qqbot-appid", self._app_id, "QQBot app ID"):
            return False

        try:
            # 收紧 keepalive 连接池，使空闲的 CLOSE_WAIT 套接字在
            # Cloudflare Warp 等代理后更快排空（#18451）。
            from gateway.platforms._http_client_limits import platform_httpx_limits
            self._http_client = httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                event_hooks={"response": [_ssrf_redirect_guard]},
                limits=platform_httpx_limits(),
            )

            # 1. 获取 access token
            await self._ensure_token()

            # 2. 获取 WebSocket gateway URL
            gateway_url = await self._get_gateway_url()
            logger.info("[%s] Gateway URL: %s", self._log_tag, gateway_url)

            # 3. 打开 WebSocket
            await self._open_ws(gateway_url)

            # 4. 启动监听器
            self._listen_task = asyncio.create_task(self._listen_loop())
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            self._mark_connected()
            logger.info("[%s] Connected", self._log_tag)
            return True
        except Exception as exc:
            message = f"QQ startup failed: {exc}"
            self._set_fatal_error("qq_connect_error", message, retryable=True)
            logger.error("[%s] %s", self._log_tag, message, exc_info=True)
            await self._cleanup()
            self._release_platform_lock()
            return False

    async def disconnect(self) -> None:
        """关闭所有连接并停止监听器。"""
        self._running = False
        self._mark_disconnected()

        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        await self._cleanup()
        self._release_platform_lock()
        logger.info("[%s] Disconnected", self._log_tag)

    async def _cleanup(self) -> None:
        """关闭 WebSocket、HTTP session 和 client。"""
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None

        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        # 使待处理项失败
        for fut in self._pending_responses.values():
            if not fut.done():
                fut.set_exception(RuntimeError("Disconnected"))
        self._pending_responses.clear()

    # ------------------------------------------------------------------
    # Token 管理
    # ------------------------------------------------------------------

    async def _ensure_token(self) -> str:
        """返回有效的 access token，必要时刷新（带 singleflight 去重）。"""
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        async with self._token_lock:
            # 加锁后二次检查
            if self._access_token and time.time() < self._token_expires_at - 60:
                return self._access_token

            try:
                resp = await self._http_client.post(
                    TOKEN_URL,
                    json={"appId": self._app_id, "clientSecret": self._client_secret},
                    timeout=DEFAULT_API_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                raise RuntimeError(f"Failed to get QQ Bot access token: {exc}") from exc

            token = data.get("access_token")
            if not token:
                raise RuntimeError(
                    f"QQ Bot token response missing access_token: {data}"
                )

            expires_in = int(data.get("expires_in", 7200))
            self._access_token = token
            self._token_expires_at = time.time() + expires_in
            logger.info(
                "[%s] Access token refreshed, expires in %ds", self._log_tag, expires_in
            )
            return self._access_token

    async def _get_gateway_url(self) -> str:
        """从 REST API 获取 WebSocket gateway URL。"""
        token = await self._ensure_token()
        try:
            resp = await self._http_client.get(
                f"{API_BASE}{GATEWAY_URL_PATH}",
                headers={
                    "Authorization": f"QQBot {token}",
                    "User-Agent": build_user_agent(),
                },
                timeout=DEFAULT_API_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"Failed to get QQ Bot gateway URL: {exc}") from exc

        url = data.get("url")
        if not url:
            raise RuntimeError(f"QQ Bot gateway response missing url: {data}")
        return url

    # ------------------------------------------------------------------
    # WebSocket 生命周期
    # ------------------------------------------------------------------

    async def _open_ws(self, gateway_url: str) -> None:
        """打开到 QQ Bot gateway 的 WebSocket 连接。"""
        # 仅清理 WebSocket 资源 — 保持 _http_client 存活以供 REST API 调用。
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

        # 遵循 WSL 代理环境变量以连接 QQ WebSocket。Hermes 升级会覆盖此
        # 本地补丁，因此 QQ 在升级后可能回退为直连超时。
        self._session = aiohttp.ClientSession(trust_env=True)
        ws_proxy = (
            os.getenv("WSS_PROXY")
            or os.getenv("wss_proxy")
            or os.getenv("HTTPS_PROXY")
            or os.getenv("https_proxy")
            or os.getenv("ALL_PROXY")
            or os.getenv("all_proxy")
        )
        self._ws = await self._session.ws_connect(
            gateway_url,
            headers={
                "User-Agent": build_user_agent(),
            },
            timeout=CONNECT_TIMEOUT_SECONDS,
            proxy=ws_proxy,
        )
        logger.info("[%s] WebSocket connected to %s", self._log_tag, gateway_url)

    async def _listen_loop(self) -> None:
        """读取 WebSocket 事件并在出错时重连。

        关闭码处理遵循 OpenClaw qqbot 参考实现：
          4004 → token 无效，刷新并重连
          4006/4007/4009 → session 无效，清除 session 并重新 identify
          4008 → 被限流，退避 60 秒
          4914 → bot 离线/沙箱，停止重连
          4915 → bot 被封禁，停止重连
        """
        backoff_idx = 0
        connect_time = 0.0
        quick_disconnect_count = 0

        while self._running:
            try:
                connect_time = time.monotonic()
                await self._read_events()
                backoff_idx = 0
                quick_disconnect_count = 0
            except asyncio.CancelledError:
                return
            except QQCloseError as exc:
                if not self._running:
                    return

                code = exc.code
                logger.warning(
                    "[%s] WebSocket closed: code=%s reason=%s",
                    self._log_tag,
                    code,
                    exc.reason,
                )

                # 快速断连检测（权限问题、配置错误）
                duration = time.monotonic() - connect_time
                if duration < QUICK_DISCONNECT_THRESHOLD and connect_time > 0:
                    quick_disconnect_count += 1
                    logger.info(
                        "[%s] Quick disconnect (%.1fs), count: %d",
                        self._log_tag,
                        duration,
                        quick_disconnect_count,
                    )
                    if quick_disconnect_count >= MAX_QUICK_DISCONNECT_COUNT:
                        logger.error(
                            "[%s] Too many quick disconnects. "
                            "Check: 1) AppID/Secret correct 2) Bot permissions on QQ Open Platform",
                            self._log_tag,
                        )
                        self._set_fatal_error(
                            "qq_quick_disconnect",
                            "Too many quick disconnects — check bot permissions",
                            retryable=True,
                        )
                        return
                else:
                    quick_disconnect_count = 0

                self._mark_transport_disconnected()
                self._fail_pending("Connection closed")

                # 对致命错误码停止重连（不可恢复的错误）
                if code in {
                        4001,  # 无效 opcode
                        4002,  # 无效 payload
                        4010,  # 无效 shard
                        4011,  # 需要 sharding
                        4012,  # 无效 API 版本
                        4013,  # 无效 intent
                        4014,  # intent 未授权
                        4914,  # 仅离线/沙箱
                        4915,  # 已封禁
                }:
                    fatal_descriptions = {
                        4001: "invalid opcode",
                        4002: "invalid payload",
                        4010: "invalid shard",
                        4011: "sharding required",
                        4012: "invalid API version",
                        4013: "invalid intent",
                        4014: "intent not authorized",
                        4914: "offline/sandbox-only",
                        4915: "banned",
                    }
                    desc = fatal_descriptions.get(code, f"fatal error (code={code})")
                    logger.error(
                        "[%s] Bot is %s. Check QQ Open Platform.", self._log_tag, desc
                    )
                    self._set_fatal_error(
                        f"qq_{desc}", f"Bot is {desc}", retryable=False
                    )
                    return

                # 被限流
                if code == 4008:
                    logger.info(
                        "[%s] Rate limited (4008), waiting %ds",
                        self._log_tag,
                        RATE_LIMIT_DELAY,
                    )
                    if backoff_idx >= MAX_RECONNECT_ATTEMPTS:
                        self._mark_disconnected()
                        return
                    await asyncio.sleep(RATE_LIMIT_DELAY)
                    if await self._reconnect(backoff_idx):
                        backoff_idx = 0
                        quick_disconnect_count = 0
                    else:
                        backoff_idx += 1
                    continue

                # token 无效 → 清除缓存的 token 以便 _ensure_token() 刷新
                if code == 4004:
                    logger.info(
                        "[%s] Invalid token (4004), will refresh and reconnect",
                        self._log_tag,
                    )
                    self._access_token = None
                    self._token_expires_at = 0.0

                # Session 无效 → 清除 session，下次 Hello 时重新 identify。
                # 注意：此处不包含 4009（连接超时）—— 按 QQ 协议它可恢复，
                # 应保留 session 状态。
                if code in {
                        4006,
                        4007,
                        4900,
                        4901,
                        4902,
                        4903,
                        4904,
                        4905,
                        4906,
                        4907,
                        4908,
                        4909,
                        4910,
                        4911,
                        4912,
                        4913,
                }:
                    logger.info(
                        "[%s] Session error (%d), clearing session for re-identify",
                        self._log_tag,
                        code,
                    )
                    self._session_id = None
                    self._last_seq = None

                if await self._reconnect(backoff_idx):
                    backoff_idx = 0
                    quick_disconnect_count = 0
                else:
                    backoff_idx += 1
                    if backoff_idx >= MAX_RECONNECT_ATTEMPTS:
                        logger.error("[%s] Max reconnect attempts reached (QQCloseError)", self._log_tag)
                        self._mark_disconnected()
                        return

            except Exception as exc:
                if not self._running:
                    return
                logger.warning("[%s] WebSocket error: %s", self._log_tag, exc)
                self._mark_transport_disconnected()
                self._fail_pending("Connection interrupted")

                if backoff_idx >= MAX_RECONNECT_ATTEMPTS:
                    logger.error("[%s] Max reconnect attempts reached", self._log_tag)
                    self._mark_disconnected()
                    return

                if await self._reconnect(backoff_idx):
                    backoff_idx = 0
                    quick_disconnect_count = 0
                else:
                    backoff_idx += 1

    async def _reconnect(self, backoff_idx: int) -> bool:
        """尝试重连 WebSocket。成功返回 True。"""
        delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
        logger.info(
            "[%s] Reconnecting in %ds (attempt %d)...",
            self._log_tag,
            delay,
            backoff_idx + 1,
        )
        await asyncio.sleep(delay)

        self._heartbeat_interval = 30.0  # 重置，直到收到 Hello
        try:
            await self._ensure_token()
            gateway_url = await self._get_gateway_url()
            await self._open_ws(gateway_url)
            self._mark_connected()
            logger.info("[%s] Reconnected", self._log_tag)
            return True
        except Exception as exc:
            logger.warning("[%s] Reconnect failed: %s", self._log_tag, exc)
            return False

    async def _read_events(self) -> None:
        """读取 WebSocket 帧，直到连接关闭。"""
        if not self._ws:
            raise RuntimeError("WebSocket not connected")
        if self._ws.closed:
            # 一个已关闭但非 None 的 ws 会让 while 条件在进入时即为假，
            # 于是此处会正常返回 —— _listen_loop 会将其视为一次干净读取，
            # 并立即以退避值重置为 0 重试，从而导致 100% CPU 空转。
            # 在此抛出异常，以走重连/退避路径。
            raise RuntimeError("WebSocket closed")

        while self._running and self._ws and not self._ws.closed:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = self._parse_json(msg.data)
                if payload:
                    self._dispatch_payload(payload)
            elif msg.type in {aiohttp.WSMsgType.PING,}:
                # aiohttp 自动回复 PONG
                pass
            elif msg.type == aiohttp.WSMsgType.CLOSE:
                raise QQCloseError(msg.data, msg.extra)
            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                raise RuntimeError("WebSocket closed")

    async def _heartbeat_loop(self) -> None:
        """周期性发送心跳（QQ Gateway 期望 op 1 心跳携带最新 seq）。

        心跳间隔由 Hello（op 10）事件的 heartbeat_interval 决定。
        QQ 默认约为 41 秒；我们按间隔的 80% 发送以留出安全余量。
        """
        try:
            while self._running:
                await asyncio.sleep(self._heartbeat_interval)
                if not self._ws or self._ws.closed:
                    continue
                try:
                    # d 应为已接收的最新序列号，或 null
                    await self._ws.send_json({"op": 1, "d": self._last_seq})
                except Exception as exc:
                    logger.debug("[%s] Heartbeat failed: %s", self._log_tag, exc)
        except asyncio.CancelledError:
            pass

    async def _send_identify(self) -> None:
        """发送 op 2 Identify 以认证 WebSocket 连接。

        收到 op 10 Hello 后，客户端必须发送携带 bot token 和 intents 的
        op 2 Identify。成功时服务器会回复一个 READY 派发事件。

        参考文档：https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/reference.html
        """
        token = await self._ensure_token()
        identify_payload = {
            "op": 2,
            "d": {
                "token": f"QQBot {token}",
                "intents": (1 << 25)
                           | (1 << 30)
                           | (1 << 12)
                           | (1 << 26),  # C2C_GROUP_AT_MESSAGES + PUBLIC_GUILD_MESSAGES + DIRECT_MESSAGE + INTERACTION
                "shard": [0, 1],
                "properties": {
                    "$os": "macOS",
                    "$browser": "hermes-agent",
                    "$device": "hermes-agent",
                },
            },
        }
        try:
            if self._ws and not self._ws.closed:
                await self._ws.send_json(identify_payload)
                logger.info("[%s] Identify sent", self._log_tag)
            else:
                logger.warning(
                    "[%s] Cannot send Identify: WebSocket not connected", self._log_tag
                )
        except Exception as exc:
            logger.error("[%s] Failed to send Identify: %s", self._log_tag, exc)

    async def _send_resume(self) -> None:
        """发送 op 6 Resume 以在重连后重新认证。

        参考文档：https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/reference.html
        """
        token = await self._ensure_token()
        resume_payload = {
            "op": 6,
            "d": {
                "token": f"QQBot {token}",
                "session_id": self._session_id,
                "seq": self._last_seq,
            },
        }
        try:
            if self._ws and not self._ws.closed:
                await self._ws.send_json(resume_payload)
                logger.info(
                    "[%s] Resume sent (session_id=%s, seq=%s)",
                    self._log_tag,
                    self._session_id,
                    self._last_seq,
                )
            else:
                logger.warning(
                    "[%s] Cannot send Resume: WebSocket not connected", self._log_tag
                )
        except Exception as exc:
            logger.error("[%s] Failed to send Resume: %s", self._log_tag, exc)
            # 若 resume 失败，清除 session，下次 Hello 时回退到 identify
            self._session_id = None
            self._last_seq = None

    @staticmethod
    def _create_task(coro):
        """调度一个协程，若没有正在运行的事件循环则静默跳过。

        这样可避免测试在 ``asyncio.run()`` 之外同步调用
        ``_dispatch_payload`` 时出现 ``RuntimeError: no running event loop``。
        """
        try:
            loop = asyncio.get_running_loop()
            return loop.create_task(coro)
        except RuntimeError:
            return None

    def _dispatch_payload(self, payload: Dict[str, Any]) -> None:
        """路由入站 WebSocket 载荷（同步派发，派生异步处理任务）。"""
        op = payload.get("op")
        t = payload.get("t")
        s = payload.get("s")
        d = payload.get("d")
        if isinstance(s, int) and (self._last_seq is None or s > self._last_seq):
            self._last_seq = s

        # op 10 = Hello（心跳间隔）—— 必须回复 Identify/Resume
        if op == 10:
            d_data = d if isinstance(d, dict) else {}
            interval_ms = d_data.get("heartbeat_interval", 30000)
            # 按服务器间隔的 80% 发送心跳以留出安全余量
            self._heartbeat_interval = interval_ms / 1000.0 * 0.8
            logger.debug(
                "[%s] Hello received, heartbeat_interval=%dms (sending every %.1fs)",
                self._log_tag,
                interval_ms,
                self._heartbeat_interval,
            )
            # 认证：若有 session 则发送 Resume，否则发送 Identify。
            # 使用 _create_task，在没有事件循环运行时（测试场景）也是安全的。
            if self._session_id and self._last_seq is not None:
                self._create_task(self._send_resume())
            else:
                self._create_task(self._send_identify())
            return

        # op 0 = Dispatch（派发）
        if op == 0 and t:
            if t == "READY":
                self._handle_ready(d)
            elif t == "RESUMED":
                logger.info("[%s] Session resumed", self._log_tag)
            elif t in {
                    "C2C_MESSAGE_CREATE",
                    "GROUP_AT_MESSAGE_CREATE",
                    "DIRECT_MESSAGE_CREATE",
                    "GUILD_MESSAGE_CREATE",
                    "GUILD_AT_MESSAGE_CREATE",
            }:
                asyncio.create_task(self._on_message(t, d))
            elif t == "INTERACTION_CREATE":
                self._create_task(self._on_interaction(d))
            else:
                logger.debug("[%s] Unhandled dispatch: %s", self._log_tag, t)
            return

        # op 11 = Heartbeat ACK（心跳确认）
        if op == 11:
            return

        # op 7 = Server Reconnect（服务器要求重连，例如负载均衡、维护）。
        # 关闭 WS 使 _read_events 抛出异常，外层循环随之触发带 Resume 的重连。
        if op == 7:
            logger.info("[%s] Server requested reconnect (op 7)", self._log_tag)
            if self._ws and not self._ws.closed:
                self._create_task(self._ws.close())
            return

        # op 9 = Invalid Session（无效 session）—— d=True 表示 session 可恢复，
        # d=False 表示必须从头重新 identify。
        if op == 9:
            resumable = bool(d) if d is not None else False
            if not resumable:
                logger.info(
                    "[%s] Invalid session (op 9, not resumable), clearing session",
                    self._log_tag,
                )
                self._session_id = None
                self._last_seq = None
            else:
                logger.info("[%s] Invalid session (op 9, resumable)", self._log_tag)
            if self._ws and not self._ws.closed:
                self._create_task(self._ws.close())
            return

        logger.debug("[%s] Unknown op: %s", self._log_tag, op)

    def _handle_ready(self, d: Any) -> None:
        """处理 READY 事件 —— 保存 session_id 以供 resume 使用。"""
        if isinstance(d, dict):
            self._session_id = d.get("session_id")
            logger.info("[%s] Ready, session_id=%s", self._log_tag, self._session_id)

    # ------------------------------------------------------------------
    # JSON 辅助函数
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json(raw: Any) -> Optional[Dict[str, Any]]:
        try:
            payload = json.loads(raw)
        except Exception:
            logger.warning("[QQBot] Failed to parse JSON: %r", raw)
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _next_msg_seq(msg_id: str) -> int:
        """生成一个 0..65535 范围内的消息序列号。"""
        time_part = int(time.time()) % 100000000
        rand = int(uuid.uuid4().hex[:4], 16)
        return (time_part ^ rand) % 65536

    # ------------------------------------------------------------------
    # 入站消息处理
    # ------------------------------------------------------------------

    async def handle_message(self, event: MessageEvent) -> None:
        """按会话缓存最后一条消息 ID，然后委托给基类处理。"""
        if event.message_id and event.source.chat_id:
            self._last_msg_id[event.source.chat_id] = event.message_id
        await super().handle_message(event)

    async def _on_message(self, event_type: str, d: Any) -> None:
        """处理入站 QQ Bot 消息事件。"""
        if not isinstance(d, dict):
            return

        # 提取公共字段
        msg_id = str(d.get("id", ""))
        if not msg_id or self._is_duplicate(msg_id):
            logger.debug(
                "[%s] Duplicate or missing message id: %s", self._log_tag, msg_id
            )
            return

        timestamp = str(d.get("timestamp", ""))
        content = str(d.get("content", "")).strip()
        author = d.get("author") if isinstance(d.get("author"), dict) else {}

        # 按事件类型路由
        if event_type == "C2C_MESSAGE_CREATE":
            await self._handle_c2c_message(d, msg_id, content, author, timestamp)
        elif event_type in {"GROUP_AT_MESSAGE_CREATE",}:
            await self._handle_group_message(d, msg_id, content, author, timestamp)
        elif event_type in {"GUILD_MESSAGE_CREATE", "GUILD_AT_MESSAGE_CREATE"}:
            await self._handle_guild_message(d, msg_id, content, author, timestamp)
        elif event_type == "DIRECT_MESSAGE_CREATE":
            await self._handle_dm_message(d, msg_id, content, author, timestamp)

    # ------------------------------------------------------------------
    # 内联键盘交互（INTERACTION_CREATE）
    # ------------------------------------------------------------------

    def set_interaction_callback(
        self,
        callback: Optional[Callable[[InteractionEvent], Awaitable[None]]],
    ) -> None:
        """注册（或清除）交互回调。

        在适配器对每个 ``INTERACTION_CREATE`` 事件完成 ACK *之后* 调用一次。
        回调负责根据 ``button_data`` 载荷，将按钮点击路由到正确的子系统
        （审批解析器、更新提示解析器等）。
        """
        self._interaction_callback = callback

    async def _on_interaction(self, d: Any) -> None:
        """处理 ``INTERACTION_CREATE`` 事件。

        职责：

        1. 将原始载荷解析为 :class:`InteractionEvent`。
        2. ACK 该交互（``PUT /interactions/{id}``），使客户端停止在按钮上
           显示加载指示器。
        3. 若已注册交互回调，则派发给该回调。
        """
        if not isinstance(d, dict):
            return
        try:
            event = parse_interaction_event(d)
        except Exception as exc:
            logger.warning(
                "[%s] Failed to parse INTERACTION_CREATE: %s", self._log_tag, exc
            )
            return

        if not event.id:
            logger.warning(
                "[%s] INTERACTION_CREATE missing id, skipping ACK", self._log_tag
            )
            return

        # 及时 ACK 交互 —— 按 QQ 文档，若未快速响应，客户端会在按钮上
        # 显示错误图标。
        try:
            await self._acknowledge_interaction(event.id)
        except Exception as exc:
            logger.warning(
                "[%s] Failed to ACK interaction %s: %s",
                self._log_tag, event.id, exc,
            )

        logger.info(
            "[%s] Interaction: scene=%s button_data=%r operator=%s",
            self._log_tag, event.scene, event.button_data, event.operator_openid,
        )

        callback = self._interaction_callback
        if callback is None:
            logger.debug(
                "[%s] No interaction callback registered; dropping button "
                "click %r",
                self._log_tag, event.button_data,
            )
            return
        try:
            await callback(event)
        except Exception as exc:
            logger.error(
                "[%s] Interaction callback raised: %s",
                self._log_tag, exc, exc_info=True,
            )

    async def _acknowledge_interaction(
            self,
            interaction_id: str,
            code: int = 0,
    ) -> None:
        """通过 ``PUT /interactions/{id}`` ACK 一次按钮交互。

        :param interaction_id: ``INTERACTION_CREATE`` 事件中的 ``id`` 字段。
        :param code: 响应码（``0`` = 成功）。
        """
        if not self._http_client:
            raise RuntimeError("HTTP client not initialized — not connected?")
        token = await self._ensure_token()
        headers = {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
            "User-Agent": build_user_agent(),
        }
        resp = await self._http_client.put(
            f"{API_BASE}/interactions/{interaction_id}",
            headers=headers,
            json={"code": code},
            timeout=DEFAULT_API_TIMEOUT,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Interaction ACK failed [{resp.status_code}]: "
                f"{resp.text[:200]}"
            )

    # QQ 键盘按钮决策 → ``tools.approval.resolve_gateway_approval`` 接受的
    # ``choice`` 词表的映射。QQ 的三按钮布局（受移动端空间限制）将
    # "session" 与 "always" 合并为单个 "always" 按钮；只想要 session 级别
    # 审批的用户可改用 ``/approve session`` 文本命令。
    _APPROVAL_BUTTON_TO_CHOICE = {
        "allow-once": "once",
        "allow-always": "always",
        "deny": "deny",
    }

    @staticmethod
    def _parse_gateway_session_key(session_key: str) -> Optional[Dict[str, str]]:
        """解析 ``agent:main:<platform>:<chat_type>:<chat_id>[:<user_id>]``。"""
        parts = str(session_key or "").split(":")
        if len(parts) < 5 or parts[0] != "agent" or parts[1] != "main":
            return None
        parsed = {
            "platform": parts[2],
            "chat_type": parts[3],
            "chat_id": parts[4],
        }
        if len(parts) > 5:
            parsed["user_id"] = parts[5]
        return parsed

    def _is_authorized_interaction_for_session(
            self,
            event: InteractionEvent,
            session_key: str,
    ) -> bool:
        """基于 session 与操作者对审批/更新交互进行授权校验。"""
        parsed = self._parse_gateway_session_key(session_key)
        operator = str(event.operator_openid or "").strip()
        if not parsed or parsed.get("platform") != "qqbot" or not operator:
            return False

        chat_type = parsed.get("chat_type", "")
        chat_id = parsed.get("chat_id", "")
        if chat_type == "c2c":
            return bool(chat_id) and operator == chat_id

        if chat_type in {"group", "guild"}:
            event_chat = str(event.group_openid or event.guild_id or "").strip()
            if not event_chat or event_chat != chat_id:
                return False
            session_user = str(parsed.get("user_id", "")).strip()
            return bool(session_user) and operator == session_user

        return False

    async def _default_interaction_dispatch(
            self,
            event: InteractionEvent,
    ) -> None:
        """将 ``INTERACTION_CREATE`` 按钮点击路由到正确的子系统。

        - ``approve:<session_key>:<decision>`` →
          :func:`tools.approval.resolve_gateway_approval`
          （解除等待危险命令审批的 agent 线程阻塞）。
        - ``update_prompt:<answer>`` →
          将答案写入 ``~/.hermes/.update_response``，供分离运行的
          ``hermes update --gateway`` 进程消费。
        - 其他内容以 DEBUG 级别记录并忽略。

        在 ``__init__`` 中安装为适配器的默认交互回调。调用方可通过
        :meth:`set_interaction_callback` 替换以将点击路由到别处
        （或传入 ``None`` 以彻底丢弃）。
        """
        button_data = event.button_data
        if not button_data:
            return

        approval = parse_approval_button_data(button_data)
        if approval is not None:
            session_key, decision = approval
            choice = self._APPROVAL_BUTTON_TO_CHOICE.get(decision)
            if choice is None:
                logger.warning(
                    "[%s] Unknown approval decision %r (session=%s)",
                    self._log_tag, decision, session_key,
                )
                return
            if not self._is_authorized_interaction_for_session(event, session_key):
                logger.warning(
                    "[%s] Rejected unauthorized approval click for session %s "
                    "(operator=%s)",
                    self._log_tag, session_key, event.operator_openid,
                )
                return
            try:
                # 延迟导入，以保证适配器在未运行审批子系统的测试中
                # 仍可被导入。
                from tools.approval import resolve_gateway_approval
                count = resolve_gateway_approval(session_key, choice)
                logger.info(
                    "[%s] Button resolved %d approval(s) for session %s "
                    "(choice=%s, operator=%s)",
                    self._log_tag, count, session_key, choice,
                    event.operator_openid,
                )
            except Exception as exc:
                logger.error(
                    "[%s] resolve_gateway_approval failed for session %s: %s",
                    self._log_tag, session_key, exc,
                )
            return

        update_answer = parse_update_prompt_button_data(button_data)
        if update_answer is not None:
            update_session_key = f"agent:main:qqbot:{event.scene}:{event.group_openid or event.guild_id or event.user_openid}"
            if not self._is_authorized_interaction_for_session(event, update_session_key):
                logger.warning(
                    "[%s] Rejected unauthorized update prompt click (operator=%s)",
                    self._log_tag, event.operator_openid,
                )
                return
            self._write_update_response(update_answer, event.operator_openid)
            return

        logger.debug(
            "[%s] Unrecognised button_data %r from interaction %s",
            self._log_tag, button_data, event.id,
        )

    @staticmethod
    def _write_update_response(answer: str, operator: str = "") -> None:
        """以原子方式将更新提示的答案写入 ``.update_response``。

        与 Discord / Telegram / 飞书适配器保持一致：分离运行的
        ``hermes update --gateway`` 监视器轮询此文件，以获取对其交互式
        提示（暂存恢复、配置迁移）的 ``y``/``n`` 响应。通过
        ``tmp + rename`` 写入，使不完整的写入无法误导读取方。
        """
        try:
            from hermes_constants import get_hermes_home
            home = get_hermes_home()
            response_path = home / ".update_response"
            tmp = response_path.with_suffix(".tmp")
            tmp.write_text(answer)
            tmp.replace(response_path)
            logger.info(
                "QQ update prompt answered %r by %s",
                answer, operator or "(unknown)",
            )
        except Exception as exc:
            logger.error("Failed to write update response: %s", exc)

    async def _handle_c2c_message(
            self,
            d: Dict[str, Any],
            msg_id: str,
            content: str,
            author: Dict[str, Any],
            timestamp: str,
    ) -> None:
        """处理 C2C（私聊）消息事件。"""
        user_openid = str(author.get("user_openid", ""))
        if not user_openid:
            return
        if not self._is_dm_allowed(user_openid):
            return

        text = content
        attachments_raw = d.get("attachments")
        logger.info(
            "[%s] C2C message: id=%s content=%r attachments=%s",
            self._log_tag,
            msg_id,
            content[:50] if content else "",
            (
                f"{len(attachments_raw) if isinstance(attachments_raw, list) else 0} items"
                if attachments_raw
                else "None"
            ),
        )
        if attachments_raw and isinstance(attachments_raw, list):
            for _i, _att in enumerate(attachments_raw):
                if isinstance(_att, dict):
                    logger.info(
                        "[%s] attachment[%d]: content_type=%s url=%s filename=%s",
                        self._log_tag,
                        _i,
                        _att.get("content_type", ""),
                        str(_att.get("url", ""))[:80],
                        _att.get("filename", ""),
                    )

        # 统一处理所有附件（图片、语音、文件）
        att_result = await self._process_attachments(attachments_raw)
        image_urls = att_result["image_urls"]
        image_media_types = att_result["image_media_types"]
        voice_transcripts = att_result["voice_transcripts"]
        attachment_info = att_result["attachment_info"]

        # 将语音转写追加到文本正文
        if voice_transcripts:
            voice_block = "\n".join(voice_transcripts)
            text = (
                (text + "\n\n" + voice_block).strip() if text.strip() else voice_block
            )
        # 追加非媒体附件信息
        if attachment_info:
            text = (
                (text + "\n\n" + attachment_info).strip()
                if text.strip()
                else attachment_info
            )

        logger.info(
            "[%s] After processing: images=%d, voice=%d",
            self._log_tag,
            len(image_urls),
            len(voice_transcripts),
        )

        # 合并引用消息的上下文（message_type=103 → msg_elements[0]）。
        quoted = await self._process_quoted_context(d)
        text = self._merge_quote_into(text, quoted["quote_block"])
        if quoted["image_urls"]:
            image_urls = image_urls + quoted["image_urls"]
            image_media_types = image_media_types + quoted["image_media_types"]

        if not text.strip() and not image_urls:
            return

        self._chat_type_map[user_openid] = "c2c"
        event = MessageEvent(
            source=self.build_source(
                chat_id=user_openid,
                user_id=user_openid,
                chat_type="dm",
            ),
            text=text,
            message_type=self._detect_message_type(image_urls, image_media_types),
            raw_message=d,
            message_id=msg_id,
            media_urls=image_urls,
            media_types=image_media_types,
            timestamp=self._parse_qq_timestamp(timestamp),
        )
        await self.handle_message(event)

    async def _handle_group_message(
            self,
            d: Dict[str, Any],
            msg_id: str,
            content: str,
            author: Dict[str, Any],
            timestamp: str,
    ) -> None:
        """处理群 @ 消息事件。"""
        group_openid = str(d.get("group_openid", ""))
        if not group_openid:
            return
        if not self._is_group_allowed(
                group_openid, str(author.get("member_openid", ""))
        ):
            return

        # 去除内容中的 @bot 提及前缀
        text = self._strip_at_mention(content)
        att_result = await self._process_attachments(d.get("attachments"))
        image_urls = att_result["image_urls"]
        image_media_types = att_result["image_media_types"]
        voice_transcripts = att_result["voice_transcripts"]
        attachment_info = att_result["attachment_info"]

        # 追加语音转写
        if voice_transcripts:
            voice_block = "\n".join(voice_transcripts)
            text = (
                (text + "\n\n" + voice_block).strip() if text.strip() else voice_block
            )
        if attachment_info:
            text = (
                (text + "\n\n" + attachment_info).strip()
                if text.strip()
                else attachment_info
            )

        # 合并引用消息的上下文（message_type=103 → msg_elements[0]）。
        quoted = await self._process_quoted_context(d)
        text = self._merge_quote_into(text, quoted["quote_block"])
        if quoted["image_urls"]:
            image_urls = image_urls + quoted["image_urls"]
            image_media_types = image_media_types + quoted["image_media_types"]

        if not text.strip() and not image_urls:
            return

        self._chat_type_map[group_openid] = "group"
        event = MessageEvent(
            source=self.build_source(
                chat_id=group_openid,
                user_id=str(author.get("member_openid", "")),
                chat_type="group",
            ),
            text=text,
            message_type=self._detect_message_type(image_urls, image_media_types),
            raw_message=d,
            message_id=msg_id,
            media_urls=image_urls,
            media_types=image_media_types,
            timestamp=self._parse_qq_timestamp(timestamp),
        )
        await self.handle_message(event)

    async def _handle_guild_message(
            self,
            d: Dict[str, Any],
            msg_id: str,
            content: str,
            author: Dict[str, Any],
            timestamp: str,
    ) -> None:
        """处理频道/子频道消息事件。"""
        channel_id = str(d.get("channel_id", ""))
        if not channel_id:
            return

        # 应用 group_policy ACL —— 频道属于类群组场景。
        # 若不做此检查，bot 所在任意频道的任意成员都能绕过配置的 allowlist。
        guild_id = str(d.get("guild_id", ""))
        author_id = str(author.get("id", ""))
        if not self._is_group_allowed(guild_id or channel_id, author_id):
            logger.debug(
                "[%s] Guild message blocked by ACL: channel=%s user=%s",
                self._log_tag, channel_id, author_id,
            )
            return

        member = d.get("member") if isinstance(d.get("member"), dict) else {}
        nick = str(member.get("nick", "")) or str(author.get("username", ""))

        text = content
        att_result = await self._process_attachments(d.get("attachments"))
        image_urls = att_result["image_urls"]
        image_media_types = att_result["image_media_types"]
        voice_transcripts = att_result["voice_transcripts"]
        attachment_info = att_result["attachment_info"]

        if voice_transcripts:
            voice_block = "\n".join(voice_transcripts)
            text = (
                (text + "\n\n" + voice_block).strip() if text.strip() else voice_block
            )
        if attachment_info:
            text = (
                (text + "\n\n" + attachment_info).strip()
                if text.strip()
                else attachment_info
            )

        # 合并引用消息的上下文（message_type=103 → msg_elements[0]）。
        quoted = await self._process_quoted_context(d)
        text = self._merge_quote_into(text, quoted["quote_block"])
        if quoted["image_urls"]:
            image_urls = image_urls + quoted["image_urls"]
            image_media_types = image_media_types + quoted["image_media_types"]

        if not text.strip() and not image_urls:
            return

        self._chat_type_map[channel_id] = "guild"
        event = MessageEvent(
            source=self.build_source(
                chat_id=channel_id,
                user_id=str(author.get("id", "")),
                user_name=nick or None,
                chat_type="group",
            ),
            text=text,
            message_type=self._detect_message_type(image_urls, image_media_types),
            raw_message=d,
            message_id=msg_id,
            media_urls=image_urls,
            media_types=image_media_types,
            timestamp=self._parse_qq_timestamp(timestamp),
        )
        await self.handle_message(event)

    async def _handle_dm_message(
            self,
            d: Dict[str, Any],
            msg_id: str,
            content: str,
            author: Dict[str, Any],
            timestamp: str,
    ) -> None:
        """处理频道私聊（DM）消息事件。"""
        guild_id = str(d.get("guild_id", ""))
        if not guild_id:
            return

        # 应用 dm_policy ACL —— 此前频道私聊未做鉴权。
        # 若不做此检查，bot 所在任意频道的任意成员都能通过私信绕过
        # 配置的 allowlist。
        author_id = str(author.get("id", ""))
        if not self._is_dm_allowed(author_id):
            logger.debug(
                "[%s] Guild DM blocked by ACL: guild=%s user=%s",
                self._log_tag, guild_id, author_id,
            )
            return

        text = content
        att_result = await self._process_attachments(d.get("attachments"))
        image_urls = att_result["image_urls"]
        image_media_types = att_result["image_media_types"]
        voice_transcripts = att_result["voice_transcripts"]
        attachment_info = att_result["attachment_info"]

        if voice_transcripts:
            voice_block = "\n".join(voice_transcripts)
            text = (
                (text + "\n\n" + voice_block).strip() if text.strip() else voice_block
            )
        if attachment_info:
            text = (
                (text + "\n\n" + attachment_info).strip()
                if text.strip()
                else attachment_info
            )

        # 合并引用消息的上下文（message_type=103 → msg_elements[0]）。
        quoted = await self._process_quoted_context(d)
        text = self._merge_quote_into(text, quoted["quote_block"])
        if quoted["image_urls"]:
            image_urls = image_urls + quoted["image_urls"]
            image_media_types = image_media_types + quoted["image_media_types"]

        if not text.strip() and not image_urls:
            return

        self._chat_type_map[guild_id] = "dm"
        event = MessageEvent(
            source=self.build_source(
                chat_id=guild_id,
                user_id=str(author.get("id", "")),
                chat_type="dm",
            ),
            text=text,
            message_type=self._detect_message_type(image_urls, image_media_types),
            raw_message=d,
            message_id=msg_id,
            media_urls=image_urls,
            media_types=image_media_types,
            timestamp=self._parse_qq_timestamp(timestamp),
        )
        await self.handle_message(event)

    # ------------------------------------------------------------------
    # 引用消息处理
    # ------------------------------------------------------------------

    async def _process_quoted_context(
            self,
            d: Dict[str, Any],
    ) -> Dict[str, Any]:
        """处理用户回复时所引用的消息。

        当用户在引用另一条消息的情况下回复时，平台会设置
        ``message_type = 103``，并将被引用消息的内容和附件放入
        ``msg_elements[0]``。旧适配器完全忽略 ``msg_elements``，因此：

        - 引用文本仅在用户自己也输入了内容时才会呈现 —— 纯引用回复
          什么也看不到。
        - 引用附件（图片、语音、文件）从不被下载或描述。
        - 引用的语音消息尤其不会产生转写，因此 LLM 无从得知用户
          所指内容。

        本方法解析 ``msg_elements``，并将被引用附件送入与消息主体相同的
        :meth:`_process_attachments` 流水线，使被引用的语音消息获得 STT
        转写、被引用的图片得到一致的缓存处理。

        :param d: 原始入站消息字典（来自 WS 派发载荷）。
        :returns: 包含以下键的字典：

            - ``quote_block``：要前置到用户文本正文的字符串
              （无引用内容时为空）。
            - ``image_urls``：被引用图片的已缓存本地路径列表。
            - ``image_media_types``：与之平行的图片 MIME 类型列表。
        """
        empty = {
            "quote_block": "",
            "image_urls": [],
            "image_media_types": [],
        }
        # 短路：仅 message_type 103 表示引用。
        try:
            if int(d.get("message_type", 0) or 0) != 103:
                return empty
        except (TypeError, ValueError):
            return empty

        elements = d.get("msg_elements")
        if not isinstance(elements, list) or not elements:
            return empty

        # msg_elements[0] 承载被引用的消息。额外的 element（若有）
        # 在实践中极为罕见；为完整性起见，我们拼接它们的文本并合并附件。
        quoted_text_parts: List[str] = []
        all_attachments: List[Dict[str, Any]] = []
        for elem in elements:
            if not isinstance(elem, dict):
                continue
            etext = str(elem.get("content", "")).strip()
            if etext:
                quoted_text_parts.append(etext)
            eatts = elem.get("attachments")
            if isinstance(eatts, list):
                for a in eatts:
                    if isinstance(a, dict):
                        all_attachments.append(a)

        att_result = await self._process_attachments(all_attachments)
        quoted_voice = att_result.get("voice_transcripts") or []
        quoted_info = att_result.get("attachment_info") or ""
        quoted_images = att_result.get("image_urls") or []
        quoted_image_types = att_result.get("image_media_types") or []

        lines: List[str] = []
        if quoted_text_parts:
            lines.append(" ".join(quoted_text_parts))
        for t in quoted_voice:
            lines.append(t)
        if quoted_info:
            lines.append(quoted_info)

        if not lines and not quoted_images:
            return empty

        if lines:
            quote_block = "[Quoted message]:\n" + "\n".join(lines)
        else:
            # 仅图片引用：至少给 LLM 一个标记，让它知道有上下文被引用。
            quote_block = "[Quoted message]: (image)"

        return {
            "quote_block": quote_block,
            "image_urls": quoted_images,
            "image_media_types": quoted_image_types,
        }

    @staticmethod
    def _merge_quote_into(text: str, quote_block: str) -> str:
        """将 ``quote_block`` 前置到 *text*，中间以空行分隔。"""
        if not quote_block:
            return text
        if text.strip():
            return f"{quote_block}\n\n{text}".strip()
        return quote_block

    # ------------------------------------------------------------------
    # 附件处理
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_message_type(media_urls: list, media_types: list):
        """根据附件的 content type 判定 MessageType。"""
        if not media_urls:
            return MessageType.TEXT
        if not media_types:
            return MessageType.PHOTO
        first_type = media_types[0].lower() if media_types else ""
        if "audio" in first_type or "voice" in first_type or "silk" in first_type:
            return MessageType.VOICE
        if "video" in first_type:
            return MessageType.VIDEO
        if "image" in first_type or "photo" in first_type:
            return MessageType.PHOTO
        logger.debug(
            "Unknown media content_type '%s', defaulting to TEXT",
            first_type,
        )
        return MessageType.TEXT

    async def _process_attachments(
            self,
            attachments: Any,
    ) -> Dict[str, Any]:
        """处理入站附件（所有消息类型）。

        对应 OpenClaw 的 ``processAttachments`` —— 统一处理图片、语音和其他文件。

        返回的字典包含：
        - image_urls: list[str]  —— 已缓存的本地图片路径
        - image_media_types: list[str] —— 已缓存图片的 MIME 类型
        - voice_transcripts: list[str] —— 语音消息的 STT 转写
        - attachment_info: str —— 非图片、非语音附件的文本描述
        """
        if not isinstance(attachments, list):
            return {
                "image_urls": [],
                "image_media_types": [],
                "voice_transcripts": [],
                "attachment_info": "",
            }

        image_urls: List[str] = []
        image_media_types: List[str] = []
        voice_transcripts: List[str] = []
        other_attachments: List[str] = []

        for att in attachments:
            if not isinstance(att, dict):
                continue

            ct = str(att.get("content_type", "")).strip().lower()
            url_raw = str(att.get("url", "")).strip()
            filename = str(att.get("filename", ""))
            if url_raw.startswith("//"):
                url = f"https:{url_raw}"
            elif url_raw:
                url = url_raw
            else:
                url = ""
                continue

            logger.debug(
                "[%s] Processing attachment: content_type=%s, url=%s, filename=%s",
                self._log_tag,
                ct,
                url[:80],
                filename,
            )

            if self._is_voice_content_type(ct, filename):
                # 语音：优先使用 QQ 的 asr_refer_text，其次 voice_wav_url，最后 STT。
                asr_refer = (
                    str(att.get("asr_refer_text", "")).strip()
                    if isinstance(att.get("asr_refer_text"), str)
                    else ""
                )
                voice_wav_url = (
                    str(att.get("voice_wav_url", "")).strip()
                    if isinstance(att.get("voice_wav_url"), str)
                    else ""
                )

                transcript = await self._stt_voice_attachment(
                    url,
                    ct,
                    filename,
                    asr_refer_text=asr_refer or None,
                    voice_wav_url=voice_wav_url or None,
                )
                if transcript:
                    voice_transcripts.append(f"[Voice] {transcript}")
                    logger.debug("[%s] Voice transcript: %s", self._log_tag, transcript)
                else:
                    logger.warning("[%s] Voice STT failed for %s", self._log_tag, url[:60])
                    voice_transcripts.append("[Voice] [语音识别失败]")
            elif ct.startswith("image/"):
                # 图片：下载并缓存到本地。
                try:
                    cached_path = await self._download_and_cache(url, ct, filename)
                    if cached_path and os.path.isfile(cached_path):
                        image_urls.append(cached_path)
                        image_media_types.append(ct or "image/jpeg")
                    elif cached_path:
                        logger.warning(
                            "[%s] Cached image path does not exist: %s",
                            self._log_tag,
                            cached_path,
                        )
                except Exception as exc:
                    logger.debug("[%s] Failed to cache image: %s", self._log_tag, exc)
            else:
                # 其他附件（视频、文件等）：下载并记录路径。
                try:
                    cached_path = await self._download_and_cache(url, ct, filename)
                    if cached_path:
                        name = filename or ct
                        if ct.startswith("video/"):
                            other_attachments.append(f"[video: {name} ({cached_path})]")
                        else:
                            other_attachments.append(f"[file: {name} ({cached_path})]")
                except Exception as exc:
                    logger.debug("[%s] Failed to cache attachment: %s", self._log_tag, exc)

        attachment_info = "\n".join(other_attachments) if other_attachments else ""
        return {
            "image_urls": image_urls,
            "image_media_types": image_media_types,
            "voice_transcripts": voice_transcripts,
            "attachment_info": attachment_info,
        }

    async def _download_and_cache(
            self, url: str, content_type: str, original_name: str = "",
    ) -> Optional[str]:
        """下载 URL 并缓存到本地。

        :param original_name: 附件元数据中的首选文件名。
            为空时回退到 URL 路径的 basename。
        """
        from tools.url_safety import is_safe_url

        if not is_safe_url(url):
            raise ValueError(f"Blocked unsafe URL: {url[:80]}")

        if not self._http_client:
            return None

        try:
            resp = await self._http_client.get(
                url,
                timeout=30.0,
                headers=self._qq_media_headers(),
            )
            resp.raise_for_status()
            data = resp.content
        except Exception as exc:
            logger.debug(
                "[%s] Download failed for %s: %s", self._log_tag, url[:80], exc
            )
            return None

        if content_type.startswith("image/"):
            ext = mimetypes.guess_extension(content_type) or ".jpg"
            return cache_image_from_bytes(data, ext)
        elif content_type == "voice" or content_type.startswith("audio/"):
            # QQ 语音消息通常为 .amr 或 .silk 格式。
            # 使用 ffmpeg 转换为 .wav，以便 STT 引擎处理。
            return await self._convert_audio_to_wav(data, url)
        else:
            filename = (
                original_name
                or Path(urlparse(url).path).name
                or "qq_attachment"
            )
            return cache_document_from_bytes(data, filename)

    @staticmethod
    def _is_voice_content_type(content_type: str, filename: str) -> bool:
        """检查附件是否为语音/音频消息。"""
        ct = content_type.strip().lower()
        fn = filename.strip().lower()
        if ct == "voice" or ct.startswith("audio/"):
            return True
        _VOICE_EXTENSIONS = (
            ".silk",
            ".amr",
            ".mp3",
            ".wav",
            ".ogg",
            ".m4a",
            ".aac",
            ".speex",
            ".flac",
        )
        if any(fn.endswith(ext) for ext in _VOICE_EXTENSIONS):
            return True
        return False

    def _qq_media_headers(self) -> Dict[str, str]:
        """返回用于 QQ 多媒体 CDN 下载的 Authorization 请求头。

        QQ 的多媒体 URL（multimedia.nt.qq.com.cn）要求在 Authorization
        请求头中携带 bot 的 access token，否则下载会返回非 200 状态。
        """
        if self._access_token:
            return {"Authorization": f"QQBot {self._access_token}"}
        return {}

    async def _stt_voice_attachment(
            self,
            url: str,
            content_type: str,
            filename: str,
            *,
            asr_refer_text: Optional[str] = None,
            voice_wav_url: Optional[str] = None,
    ) -> Optional[str]:
        """下载语音附件，转换为 wav，并转写。

        优先级：
        1. QQ 内置的 ``asr_refer_text``（腾讯自研 ASR —— 免费，无需 API 调用）。
        2. 在 ``voice_wav_url`` 上的自托管 STT（QQ 预转换的 WAV，避免 SILK 解码）。
        3. 在原始附件 URL 上的自托管 STT（需要 SILK→WAV 转换）。

        返回转写文本，失败返回 None。
        """
        # 1. 若可用，使用 QQ 内置的 ASR 文本
        if asr_refer_text:
            logger.debug(
                "[%s] STT: using QQ asr_refer_text: %r", self._log_tag, asr_refer_text[:100]
            )
            return asr_refer_text

        # 决定下载哪个 URL（优先 voice_wav_url —— 已是 WAV）
        download_url = url
        is_pre_wav = False
        if voice_wav_url:
            if voice_wav_url.startswith("//"):
                voice_wav_url = f"https:{voice_wav_url}"
            download_url = voice_wav_url
            is_pre_wav = True
            logger.debug("[%s] STT: using voice_wav_url (pre-converted WAV)", self._log_tag)

        from tools.url_safety import is_safe_url
        if not is_safe_url(download_url):
            logger.warning("[QQ] STT blocked unsafe URL: %s", download_url[:80])
            return None

        try:
            # 2. 下载音频（QQ CDN 需要 Authorization 请求头）
            if not self._http_client:
                logger.warning("[%s] STT: no HTTP client", self._log_tag)
                return None

            download_headers = self._qq_media_headers()
            logger.debug(
                "[%s] STT: downloading voice from %s (pre_wav=%s, headers=%s)",
                self._log_tag,
                download_url[:80],
                is_pre_wav,
                bool(download_headers),
            )
            resp = await self._http_client.get(
                download_url,
                timeout=30.0,
                headers=download_headers,
                follow_redirects=True,
            )
            resp.raise_for_status()
            audio_data = resp.content
            logger.debug(
                "[%s] STT: downloaded %d bytes, content_type=%s",
                self._log_tag,
                len(audio_data),
                resp.headers.get("content-type", "unknown"),
            )

            if len(audio_data) < 10:
                logger.warning(
                    "[%s] STT: downloaded data too small (%d bytes), skipping",
                    self._log_tag,
                    len(audio_data),
                )
                return None

            # 3. 转换为 wav（若已有预转换的 WAV 则跳过）
            if is_pre_wav:
                import tempfile

                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    tmp.write(audio_data)
                    wav_path = tmp.name
                logger.debug(
                    "[%s] STT: using pre-converted WAV directly (%d bytes)",
                    self._log_tag,
                    len(audio_data),
                )
            else:
                logger.debug(
                    "[%s] STT: converting to wav, filename=%r", self._log_tag, filename
                )
                wav_path = await self._convert_audio_to_wav_file(audio_data, filename)
                if not wav_path or not Path(wav_path).exists():
                    logger.warning(
                        "[%s] STT: ffmpeg conversion produced no output", self._log_tag
                    )
                    return None

            # 4. 调用 STT API
            logger.debug("[%s] STT: calling ASR on %s", self._log_tag, wav_path)
            transcript = await self._call_stt(wav_path)

            # 5. 清理临时文件
            try:
                os.unlink(wav_path)
            except OSError:
                pass

            if transcript:
                logger.debug("[%s] STT success: %r", self._log_tag, transcript[:100])
            else:
                logger.warning("[%s] STT: ASR returned empty transcript", self._log_tag)
            return transcript
        except (httpx.HTTPStatusError, httpx.TransportError, IOError) as exc:
            logger.warning(
                "[%s] STT failed for voice attachment: %s: %s",
                self._log_tag,
                type(exc).__name__,
                exc,
            )
            return None

    async def _convert_audio_to_wav_file(
            self, audio_data: bytes, filename: str
    ) -> Optional[str]:
        """使用 pilk（SILK）或 ffmpeg 将音频字节转换为临时 .wav 文件。

        QQ 语音消息通常为 SILK 格式，ffmpeg 无法解码。
        策略：始终先尝试 pilk，若 pilk 失败再回退到 ffmpeg。

        返回 wav 文件路径，失败返回 None。
        """
        import tempfile

        ext = (
            Path(filename).suffix.lower()
            if Path(filename).suffix
            else self._guess_ext_from_data(audio_data)
        )
        logger.info(
            "[%s] STT: audio_data size=%d, ext=%r, first_20_bytes=%r",
            self._log_tag,
            len(audio_data),
            ext,
            audio_data[:20],
        )

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp_src:
            tmp_src.write(audio_data)
            src_path = tmp_src.name

        wav_path = src_path.rsplit(".", 1)[0] + ".wav"

        # 先尝试 pilk（可处理 SILK 及许多其他格式）
        result = await self._convert_silk_to_wav(src_path, wav_path)

        # 若 pilk 失败，尝试 ffmpeg
        if not result:
            result = await self._convert_ffmpeg_to_wav(src_path, wav_path)

        # 若 ffmpeg 也失败，尝试将原始 PCM 写成 WAV（最后的手段）
        if not result:
            result = await self._convert_raw_to_wav(audio_data, wav_path)

        # 清理源文件
        try:
            os.unlink(src_path)
        except OSError:
            pass

        return result

    @staticmethod
    def _guess_ext_from_data(data: bytes) -> str:
        """根据 magic bytes 猜测文件扩展名。"""
        if data[:9] == b"#!SILK_V3" or data[:6] == b"#!SILK":
            return ".silk"
        if data[:2] == b"\x02!":
            return ".silk"
        if data[:4] == b"RIFF":
            return ".wav"
        if data[:4] == b"fLaC":
            return ".flac"
        if data[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
            return ".mp3"
        if data[:4] == b"\x30\x26\xb2\x75" or data[:4] == b"\x4f\x67\x67\x53":
            return ".ogg"
        if data[:4] == b"\x00\x00\x00\x20" or data[:4] == b"\x00\x00\x00\x1c":
            return ".amr"
        # 未知格式默认为 .amr（QQ 最常见的语音格式）
        return ".amr"

    @staticmethod
    def _looks_like_silk(data: bytes) -> bool:
        """检查字节是否像 SILK 音频文件。"""
        return data[:6] == b"#!SILK" or data[:2] == b"\x02!" or data[:9] == b"#!SILK_V3"

    async def _convert_silk_to_wav(self, src_path: str, wav_path: str) -> Optional[str]:
        """使用 pilk 库将音频文件转换为 WAV。

        先按原样尝试转换，若扩展名不同则改用 .silk 再试。
        pilk 能处理带有各种头（或无头）的 SILK 文件。
        """
        try:
            import pilk
        except ImportError:
            logger.warning(
                "[%s] pilk not installed — cannot decode SILK audio. Run: pip install pilk",
                self._log_tag,
            )
            return None

        # 按原样尝试转换
        try:
            pilk.silk_to_wav(src_path, wav_path, rate=16000)
            if Path(wav_path).exists() and Path(wav_path).stat().st_size > 44:
                logger.debug(
                    "[%s] pilk converted %s to wav (%d bytes)",
                    self._log_tag,
                    Path(src_path).name,
                    Path(wav_path).stat().st_size,
                )
                return wav_path
        except Exception as exc:
            logger.debug("[%s] pilk direct conversion failed: %s", self._log_tag, exc)

        # 尝试重命名为 .silk 后转换（pilk 会检查扩展名）
        silk_path = src_path.rsplit(".", 1)[0] + ".silk"
        try:
            import shutil

            shutil.copy2(src_path, silk_path)
            pilk.silk_to_wav(silk_path, wav_path, rate=16000)
            if Path(wav_path).exists() and Path(wav_path).stat().st_size > 44:
                logger.debug(
                    "[%s] pilk converted %s (as .silk) to wav (%d bytes)",
                    self._log_tag,
                    Path(src_path).name,
                    Path(wav_path).stat().st_size,
                )
                return wav_path
        except Exception as exc:
            logger.debug("[%s] pilk .silk conversion failed: %s", self._log_tag, exc)
        finally:
            try:
                os.unlink(silk_path)
            except OSError:
                pass

        return None

    async def _convert_raw_to_wav(self, audio_data: bytes, wav_path: str) -> Optional[str]:
        """最后手段：尝试将音频数据写成 raw PCM 16-bit 单声道 16kHz WAV。

        若数据并非 raw PCM，结果将是无意义噪声，但至少 ASR 引擎不会崩溃
        —— 只是返回空转写而已。
        """
        try:
            import wave

            with wave.open(wav_path, "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(audio_data)
            return wav_path
        except Exception as exc:
            logger.debug("[%s] raw PCM fallback failed: %s", self._log_tag, exc)
            return None

    async def _convert_ffmpeg_to_wav(self, src_path: str, wav_path: str) -> Optional[str]:
        """使用 ffmpeg 将音频文件转换为 WAV。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                src_path,
                "-ar",
                "16000",
                "-ac",
                "1",
                wav_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.wait(), timeout=30)
            if proc.returncode != 0:
                stderr = await proc.stderr.read() if proc.stderr else b""
                logger.warning(
                    "[%s] ffmpeg failed for %s: %s",
                    self._log_tag,
                    Path(src_path).name,
                    stderr[:200].decode(errors="replace"),
                )
                return None
        except (asyncio.TimeoutError, FileNotFoundError) as exc:
            logger.warning("[%s] ffmpeg conversion error: %s", self._log_tag, exc)
            return None

        if not Path(wav_path).exists() or Path(wav_path).stat().st_size <= 44:
            logger.warning(
                "[%s] ffmpeg produced no/small output for %s",
                self._log_tag,
                Path(src_path).name,
            )
            return None
        logger.debug(
            "[%s] ffmpeg converted %s to wav (%d bytes)",
            self._log_tag,
            Path(src_path).name,
            Path(wav_path).stat().st_size,
        )
        return wav_path

    def _resolve_stt_config(self) -> Optional[Dict[str, str]]:
        """从配置/环境变量解析 STT 后端配置。

        优先级：
        1. 插件级专用：config.yaml 中的 ``channels.qqbot.stt`` → ``self.config.extra["stt"]``
        2. QQ 专用环境变量：``QQ_STT_API_KEY`` / ``QQ_STT_BASE_URL`` / ``QQ_STT_MODEL``
        3. 若均未配置则返回 None（将跳过 STT，QQ 内置 ASR 仍可用）。
        """
        extra = self.config.extra or {}

        # 1. 插件级专用 STT 配置（对应 OpenClaw 的 channels.qqbot.stt）
        stt_cfg = extra.get("stt")
        if isinstance(stt_cfg, dict) and stt_cfg.get("enabled") is not False:
            base_url = stt_cfg.get("baseUrl") or stt_cfg.get("base_url", "")
            api_key = stt_cfg.get("apiKey") or stt_cfg.get("api_key", "")
            model = stt_cfg.get("model", "")
            if base_url and api_key:
                return {
                    "base_url": base_url.rstrip("/"),
                    "api_key": api_key,
                    "model": model or "whisper-1",
                }
            # 仅 provider 配置：只有 model 名称，使用默认 provider
            if api_key:
                provider = stt_cfg.get("provider", "zai")
                # 将 provider 映射到 base URL
                _PROVIDER_BASE_URLS = {
                    "zai": "https://open.bigmodel.cn/api/coding/paas/v4",
                    "openai": "https://api.openai.com/v1",
                    "glm": "https://open.bigmodel.cn/api/coding/paas/v4",
                }
                base_url = _PROVIDER_BASE_URLS.get(provider, "")
                if base_url:
                    return {
                        "base_url": base_url,
                        "api_key": api_key,
                        "model": model
                                 or ("glm-asr" if provider in {"zai", "glm"} else "whisper-1"),
                    }

        # 2. QQ 专用环境变量（由 `hermes setup gateway` / `hermes gateway` 设置）
        qq_stt_key = os.getenv("QQ_STT_API_KEY", "")
        if qq_stt_key:
            base_url = os.getenv(
                "QQ_STT_BASE_URL",
                "https://open.bigmodel.cn/api/coding/paas/v4",
            )
            model = os.getenv("QQ_STT_MODEL", "glm-asr")
            return {
                "base_url": base_url.rstrip("/"),
                "api_key": qq_stt_key,
                "model": model,
            }

        return None

    async def _call_stt(self, wav_path: str) -> Optional[str]:
        """调用 OpenAI 兼容的 STT API 以转写 wav 文件。

        使用 ``channels.qqbot.stt`` 配置中的 provider；若未配置则回退到
        QQ 内置的 ``asr_refer_text``。若 STT 未配置或调用失败则返回 None。
        """
        stt_cfg = self._resolve_stt_config()
        if not stt_cfg:
            logger.warning(
                "[%s] STT not configured (no stt config or QQ_STT_API_KEY)",
                self._log_tag,
            )
            return None

        base_url = stt_cfg["base_url"]
        api_key = stt_cfg["api_key"]
        model = stt_cfg["model"]

        try:
            with open(wav_path, "rb") as f:
                resp = await self._http_client.post(
                    f"{base_url}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": (Path(wav_path).name, f, "audio/wav")},
                    data={"model": model},
                    timeout=30.0,
                )
            resp.raise_for_status()
            result = resp.json()
            # Zhipu/GLM 格式：{"choices": [{"message": {"content": "transcript text"}}]}
            choices = result.get("choices", [])
            if choices:
                content = choices[0].get("message", {}).get("content", "")
                if content.strip():
                    return content.strip()
            # OpenAI/Whisper 格式：{"text": "transcript text"}
            text = result.get("text", "")
            if text.strip():
                return text.strip()
            return None
        except (httpx.HTTPStatusError, IOError) as exc:
            logger.warning(
                "[%s] STT API call failed (model=%s, base=%s): %s",
                self._log_tag,
                model,
                base_url[:50],
                exc,
            )
            return None

    async def _convert_audio_to_wav(
            self, audio_data: bytes, source_url: str
    ) -> Optional[str]:
        """使用 pilk（SILK）或 ffmpeg 将音频字节转换为 .wav，并缓存结果。"""
        import tempfile

        # 根据 magic bytes 或 URL 判定源格式
        ext = (
            Path(urlparse(source_url).path).suffix.lower()
            if urlparse(source_url).path
            else ""
        )
        if not ext or ext not in {
                ".silk",
                ".amr",
                ".mp3",
                ".wav",
                ".ogg",
                ".m4a",
                ".aac",
                ".flac",
        }:
            ext = self._guess_ext_from_data(audio_data)

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp_src:
            tmp_src.write(audio_data)
            src_path = tmp_src.name

        wav_path = src_path.rsplit(".", 1)[0] + ".wav"
        try:
            is_silk = ext == ".silk" or self._looks_like_silk(audio_data)
            if is_silk:
                result = await self._convert_silk_to_wav(src_path, wav_path)
            else:
                result = await self._convert_ffmpeg_to_wav(src_path, wav_path)

            if not result:
                logger.warning(
                    "[%s] audio conversion failed for %s (format=%s)",
                    self._log_tag,
                    source_url[:60],
                    ext,
                )
                return cache_document_from_bytes(audio_data, f"qq_voice{ext}")
        except Exception:
            return cache_document_from_bytes(audio_data, f"qq_voice{ext}")
        finally:
            try:
                os.unlink(src_path)
            except OSError:
                pass

        # 校验输出并缓存
        try:
            wav_data = Path(wav_path).read_bytes()
            os.unlink(wav_path)
            return cache_document_from_bytes(wav_data, "qq_voice.wav")
        except Exception as exc:
            logger.debug("[%s] Failed to read converted wav: %s", self._log_tag, exc)
            return None

    # ------------------------------------------------------------------
    # 出站消息 —— REST API
    # ------------------------------------------------------------------

    async def _api_request(
            self,
            method: str,
            path: str,
            body: Optional[Dict[str, Any]] = None,
            timeout: float = DEFAULT_API_TIMEOUT,
    ) -> Dict[str, Any]:
        """向 QQ Bot API 发起已认证的 REST API 请求。"""
        if not self._http_client:
            raise RuntimeError("HTTP client not initialized — not connected?")

        token = await self._ensure_token()
        headers = {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
            "User-Agent": build_user_agent(),
        }

        try:
            resp = await self._http_client.request(
                method,
                f"{API_BASE}{path}",
                headers=headers,
                json=body,
                timeout=timeout,
            )
            data = resp.json()
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"QQ Bot API error [{resp.status_code}] {path}: "
                    f"{data.get('message', data)}"
                )
            return data
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"QQ Bot API timeout [{path}]: {exc}") from exc

    async def _upload_media(
            self,
            target_type: str,
            target_id: str,
            file_type: int,
            url: Optional[str] = None,
            file_data: Optional[str] = None,
            srv_send_msg: bool = False,
            file_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """上传媒体并返回 file_info。"""
        path = (
            f"/v2/users/{target_id}/files"
            if target_type == "c2c"
            else f"/v2/groups/{target_id}/files"
        )

        body: Dict[str, Any] = {
            "file_type": file_type,
            "srv_send_msg": srv_send_msg,
        }
        if url:
            body["url"] = url
        elif file_data:
            body["file_data"] = file_data
        if file_type == MEDIA_TYPE_FILE and file_name:
            body["file_name"] = file_name

        # 重试瞬态上传失败
        for attempt in range(3):
            try:
                return await self._api_request(
                    "POST", path, body, timeout=FILE_UPLOAD_TIMEOUT
                )
            except RuntimeError as exc:
                err_msg = str(exc)
                if any(
                        kw in err_msg
                        for kw in ("400", "401", "Invalid", "timeout", "Timeout")
                ):
                    raise
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                else:
                    raise

    # 放弃发送前等待重连的最长时间（秒）。
    _RECONNECT_WAIT_SECONDS = 15.0
    # 等待期间轮询 is_connected 的频率（秒）。
    _RECONNECT_POLL_INTERVAL = 0.5

    async def _wait_for_reconnection(self) -> bool:
        """等待 WebSocket 监听器重连。

        监听循环（_listen_loop）在断开时会自动重连，但存在一个竞态窗口：
        send() 可能在断开后、重连完成前被调用。本方法最多轮询
        _RECONNECT_WAIT_SECONDS 秒的 is_connected。

        重连成功返回 True，仍处于断开状态返回 False。
        """
        logger.info("[%s] Not connected — waiting for reconnection (up to %.0fs)",
                    self._log_tag, self._RECONNECT_WAIT_SECONDS)
        waited = 0.0
        while waited < self._RECONNECT_WAIT_SECONDS:
            await asyncio.sleep(self._RECONNECT_POLL_INTERVAL)
            waited += self._RECONNECT_POLL_INTERVAL
            if self.is_connected:
                logger.info("[%s] Reconnected after %.1fs", self._log_tag, waited)
                return True
        logger.warning("[%s] Still not connected after %.0fs", self._log_tag, self._RECONNECT_WAIT_SECONDS)
        return False

    async def send(
            self,
            chat_id: str,
            content: str,
            reply_to: Optional[str] = None,
            metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """向 QQ 用户或群组发送文本或 markdown 消息。

        应用 format_message()，通过 truncate_message() 拆分长消息，
        并以指数退避重试瞬态失败。
        """
        del metadata

        if not self.is_connected:
            if not await self._wait_for_reconnection():
                return SendResult(success=False, error="Not connected", retryable=True)

        if not content or not content.strip():
            return SendResult(success=True)

        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)

        last_result = SendResult(success=False, error="No chunks")
        for chunk in chunks:
            last_result = await self._send_chunk(chat_id, chunk, reply_to)
            if not last_result.success:
                return last_result
            # 仅对第一个分片 reply_to
            reply_to = None
        return last_result

    async def _send_chunk(
            self,
            chat_id: str,
            content: str,
            reply_to: Optional[str] = None,
    ) -> SendResult:
        """发送单个分片，带重试和指数退避。"""
        last_exc: Optional[Exception] = None
        chat_type = self._guess_chat_type(chat_id)

        for attempt in range(3):
            try:
                if chat_type == "c2c":
                    return await self._send_c2c_text(chat_id, content, reply_to)
                elif chat_type == "group":
                    return await self._send_group_text(chat_id, content, reply_to)
                elif chat_type == "guild":
                    return await self._send_guild_text(chat_id, content, reply_to)
                else:
                    return SendResult(
                        success=False, error=f"Unknown chat type for {chat_id}"
                    )
            except Exception as exc:
                last_exc = exc
                err = str(exc).lower()
                # 永久性错误 —— 不重试
                if any(
                        k in err
                        for k in ("invalid", "forbidden", "not found", "bad request")
                ):
                    break
                # 瞬态错误 —— 退避后重试
                if attempt < 2:
                    delay = 1.0 * (2 ** attempt)
                    logger.warning(
                        "[%s] send retry %d/3 after %.1fs: %s",
                        self._log_tag,
                        attempt + 1,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)

        error_msg = str(last_exc) if last_exc else "Unknown error"
        logger.error("[%s] Send failed: %s", self._log_tag, error_msg)
        retryable = not any(
            k in error_msg.lower() for k in ("invalid", "forbidden", "not found")
        )
        return SendResult(success=False, error=error_msg, retryable=retryable)

    async def _send_c2c_text(
            self,
            openid: str,
            content: str,
            reply_to: Optional[str] = None,
            keyboard: Optional[InlineKeyboard] = None,
    ) -> SendResult:
        """通过 REST API 向 C2C 用户发送文本。

        :param keyboard: 附带到消息上的可选内联键盘。
        """
        self._next_msg_seq(reply_to or openid)
        body = self._build_text_body(content, reply_to)
        if reply_to:
            body["msg_id"] = reply_to
        if keyboard is not None:
            body["keyboard"] = keyboard.to_dict()

        data = await self._api_request("POST", f"/v2/users/{openid}/messages", body)
        msg_id = str(data.get("id", uuid.uuid4().hex[:12]))
        return SendResult(success=True, message_id=msg_id, raw_response=data)

    async def _send_group_text(
            self,
            group_openid: str,
            content: str,
            reply_to: Optional[str] = None,
            keyboard: Optional[InlineKeyboard] = None,
    ) -> SendResult:
        """通过 REST API 向群组发送文本。

        :param keyboard: 附带到消息上的可选内联键盘。
        """
        self._next_msg_seq(reply_to or group_openid)
        body = self._build_text_body(content, reply_to)
        if reply_to:
            body["msg_id"] = reply_to
        if keyboard is not None:
            body["keyboard"] = keyboard.to_dict()

        data = await self._api_request(
            "POST", f"/v2/groups/{group_openid}/messages", body
        )
        msg_id = str(data.get("id", uuid.uuid4().hex[:12]))
        return SendResult(success=True, message_id=msg_id, raw_response=data)

    async def _send_guild_text(
            self, channel_id: str, content: str, reply_to: Optional[str] = None
    ) -> SendResult:
        """通过 REST API 向频道发送文本。"""
        body: Dict[str, Any] = {"content": content[: self.MAX_MESSAGE_LENGTH]}
        if reply_to:
            body["msg_id"] = reply_to

        data = await self._api_request("POST", f"/channels/{channel_id}/messages", body)
        msg_id = str(data.get("id", uuid.uuid4().hex[:12]))
        return SendResult(success=True, message_id=msg_id, raw_response=data)

    # ------------------------------------------------------------------
    # 内联键盘出站辅助（审批 / 更新提示流程）
    # ------------------------------------------------------------------

    async def send_with_keyboard(
            self,
            chat_id: str,
            content: str,
            keyboard: InlineKeyboard,
            reply_to: Optional[str] = None,
    ) -> SendResult:
        """发送附带内联键盘的单条文本消息。

        与 :meth:`send` 不同，本方法**不会**将长内容拆分为多个分片 ——
        键盘消息只有一个交互面，拆分会使按钮脱离第一个分片而孤立。
        调用方应保持审批/更新提示正文简短。

        频道（channel）聊天不支持内联键盘；对此类场景返回不可重试的失败。
        """
        if not self.is_connected:
            if not await self._wait_for_reconnection():
                return SendResult(
                    success=False, error="Not connected", retryable=True
                )

        chat_type = self._guess_chat_type(chat_id)
        formatted = self.format_message(content)
        truncated = formatted[: self.MAX_MESSAGE_LENGTH]
        try:
            if chat_type == "c2c":
                return await self._send_c2c_text(
                    chat_id, truncated, reply_to, keyboard=keyboard,
                )
            if chat_type == "group":
                return await self._send_group_text(
                    chat_id, truncated, reply_to, keyboard=keyboard,
                )
            return SendResult(
                success=False,
                error=(
                    f"Inline keyboards not supported for chat_type "
                    f"{chat_type!r}"
                ),
                retryable=False,
            )
        except Exception as exc:
            logger.error(
                "[%s] send_with_keyboard failed: %s", self._log_tag, exc
            )
            return SendResult(success=False, error=str(exc))

    async def send_approval_request(
            self,
            chat_id: str,
            req: ApprovalRequest,
            reply_to: Optional[str] = None,
    ) -> SendResult:
        """发送三按钮审批请求（``allow-once / allow-always / deny``）。

        渲染文本来自 :func:`build_approval_text`；调用方可通过传入自定义
        的 :class:`ApprovalRequest` 来覆盖。

        用户点击按钮 → 触发 ``INTERACTION_CREATE`` → 适配器已注册的
        :meth:`set_interaction_callback` 处理器通过
        :func:`parse_approval_button_data` 解码 ``button_data``。
        """
        from gateway.platforms.qqbot.keyboards import build_approval_text
        return await self.send_with_keyboard(
            chat_id,
            build_approval_text(req),
            build_approval_keyboard(req.session_key),
            reply_to=reply_to,
        )

    # ------------------------------------------------------------------
    # 跨适配器 gateway 契约 —— send_exec_approval + send_update_prompt
    # ------------------------------------------------------------------
    #
    # 这些方法对应 gateway/run.py 在适配器类上探测的签名（如
    # type(adapter).send_exec_approval、type(adapter).send_update_prompt），
    # 用于基于按钮的审批/更新确认 UX。Discord、Telegram、Slack、Matrix
    # 和飞书均已实现同一契约。

    async def send_exec_approval(
            self,
            chat_id: str,
            command: str,
            session_key: str,
            description: str = "dangerous command",
            metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """为危险命令发送基于按钮的执行审批提示。

        当 agent 被阻塞等待审批时，由 ``gateway/run.py`` 的
        ``_approval_notify_sync`` 调用。按钮点击通过
        :func:`tools.approval.resolve_gateway_approval` 解析 —— 由适配器的
        交互回调（:meth:`_default_interaction_dispatch`）派发。
        """
        del metadata  # QQ 没有 thread_id / DM 定向覆盖。

        # 当有 reply-to 消息时，用它作为被动消息的上下文。
        # QQ 要求向从未见过的用户发送出站消息时携带 msg_id；
        # 最后一条入站 msg_id 是自然之选。
        msg_id = self._last_msg_id.get(chat_id)

        req = ApprovalRequest(
            session_key=session_key,
            title=f"Execute this command?",
            description=description,
            command_preview=command,
            timeout_sec=self._APPROVAL_TIMEOUT_SECONDS,
        )
        return await self.send_approval_request(
            chat_id, req, reply_to=msg_id,
        )

    _APPROVAL_TIMEOUT_SECONDS = 300  # 与 gateway 的默认 gateway_timeout 一致

    async def send_update_prompt(
            self,
            chat_id: str,
            prompt: str,
            default: str = "",
            session_key: str = "",
            metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """发送带内联按钮的是/否更新确认提示。

        对应 ``gateway/run.py`` 的 ``hermes update --gateway`` 监视器所使用的
        跨适配器契约。按钮点击以 ``INTERACTION_CREATE`` 形式出现，其中
        ``button_data = 'update_prompt:y'`` 或 ``'update_prompt:n'``；
        适配器的交互回调将答案写入 ``~/.hermes/.update_response``，
        以便分离运行的更新进程读取。
        """
        del session_key, metadata  # 仅为契约对齐而保留。

        default_hint = f" (default: {default})" if default else ""
        content = f"⚕ **Update Needs Your Input**\n\n{prompt}{default_hint}"
        msg_id = self._last_msg_id.get(chat_id)
        return await self.send_with_keyboard(
            chat_id,
            content,
            build_update_prompt_keyboard(),
            reply_to=msg_id,
        )

    def _build_text_body(
            self, content: str, reply_to: Optional[str] = None
    ) -> Dict[str, Any]:
        """构建用于 C2C/群组文本发送的消息体。"""
        msg_seq = self._next_msg_seq(reply_to or "default")

        if self._markdown_support:
            body: Dict[str, Any] = {
                "markdown": {"content": content[: self.MAX_MESSAGE_LENGTH]},
                "msg_type": MSG_TYPE_MARKDOWN,
                "msg_seq": msg_seq,
            }
        else:
            body = {
                "content": content[: self.MAX_MESSAGE_LENGTH],
                "msg_type": MSG_TYPE_TEXT,
                "msg_seq": msg_seq,
            }

        if reply_to:
            # 非 markdown 模式下，添加 message_reference
            if not self._markdown_support:
                body["message_reference"] = {"message_id": reply_to}

        return body

    # ------------------------------------------------------------------
    # 原生媒体发送
    # ------------------------------------------------------------------

    async def send_image(
            self,
            chat_id: str,
            image_url: str,
            caption: Optional[str] = None,
            reply_to: Optional[str] = None,
            metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """通过 QQ Bot API 上传原生发送图片。"""
        del metadata

        result = await self._send_media(
            chat_id, image_url, MEDIA_TYPE_IMAGE, "image", caption, reply_to
        )
        if result.success or not self._is_url(image_url):
            return result

        # 回退到文本 URL
        logger.warning(
            "[%s] Image send failed, falling back to text: %s",
            self._log_tag,
            result.error,
        )
        fallback = f"{caption}\n{image_url}" if caption else image_url
        return await self.send(chat_id=chat_id, content=fallback, reply_to=reply_to)

    async def send_image_file(
            self,
            chat_id: str,
            image_path: str,
            caption: Optional[str] = None,
            reply_to: Optional[str] = None,
            **kwargs,
    ) -> SendResult:
        """原生发送本地图片文件。"""
        del kwargs
        return await self._send_media(
            chat_id, image_path, MEDIA_TYPE_IMAGE, "image", caption, reply_to
        )

    async def send_voice(
            self,
            chat_id: str,
            audio_path: str,
            caption: Optional[str] = None,
            reply_to: Optional[str] = None,
            **kwargs,
    ) -> SendResult:
        """原生发送语音消息。"""
        del kwargs
        return await self._send_media(
            chat_id, audio_path, MEDIA_TYPE_VOICE, "voice", caption, reply_to
        )

    async def send_video(
            self,
            chat_id: str,
            video_path: str,
            caption: Optional[str] = None,
            reply_to: Optional[str] = None,
            **kwargs,
    ) -> SendResult:
        """原生发送视频。"""
        del kwargs
        return await self._send_media(
            chat_id, video_path, MEDIA_TYPE_VIDEO, "video", caption, reply_to
        )

    async def send_document(
            self,
            chat_id: str,
            file_path: str,
            caption: Optional[str] = None,
            file_name: Optional[str] = None,
            reply_to: Optional[str] = None,
            **kwargs,
    ) -> SendResult:
        """原生发送文件/文档。"""
        del kwargs
        return await self._send_media(
            chat_id,
            file_path,
            MEDIA_TYPE_FILE,
            "file",
            caption,
            reply_to,
            file_name=file_name,
        )

    async def _send_media(
            self,
            chat_id: str,
            media_source: str,
            file_type: int,
            kind: str,
            caption: Optional[str] = None,
            reply_to: Optional[str] = None,
            file_name: Optional[str] = None,
    ) -> SendResult:
        """上传媒体并作为原生消息发送。

        上传策略：

        - **HTTP(S) URL** → 单次 ``POST /v2/{users|groups}/{id}/files``，
          携带 ``url=...``。QQ 平台直接抓取该 URL；当源已托管时这是最快路径。
        - **本地文件** → 三步分块上传（prepare / PUT parts / complete）。
          可处理高达平台约 100 MB 单文件限制的文件，且不受旧适配器约 10 MB
          内联 base64 上限的约束。
        """
        if not self.is_connected:
            if not await self._wait_for_reconnection():
                return SendResult(success=False, error="Not connected", retryable=True)

        chat_type = self._guess_chat_type(chat_id)
        if chat_type == "guild":
            # 频道不支持同样方式的原生媒体上传。
            return SendResult(
                success=False,
                error="Guild media send not supported via this path",
            )

        try:
            if self._is_url(media_source):
                # URL 上传 —— 让平台直接抓取。
                resolved_name = (
                    file_name
                    or Path(urlparse(media_source).path).name
                    or "media"
                )
                upload = await self._upload_media(
                    chat_type,
                    chat_id,
                    file_type,
                    url=media_source,
                    srv_send_msg=False,
                    file_name=resolved_name if file_type == MEDIA_TYPE_FILE else None,
                )
            else:
                # 本地文件 —— 分块上传（prepare / PUT parts / complete）。
                resolved_name, upload = await self._upload_local_file(
                    chat_type,
                    chat_id,
                    media_source,
                    file_type,
                    file_name,
                )

            file_info = upload.get("file_info") or (
                upload.get("data", {}) or {}
            ).get("file_info")
            if not file_info:
                return SendResult(
                    success=False,
                    error=f"Upload returned no file_info: {upload}",
                )

            # 发送媒体消息
            msg_seq = self._next_msg_seq(chat_id)
            body: Dict[str, Any] = {
                "msg_type": MSG_TYPE_MEDIA,
                "media": {"file_info": file_info},
                "msg_seq": msg_seq,
            }
            if caption:
                body["content"] = caption[: self.MAX_MESSAGE_LENGTH]
            if reply_to:
                body["msg_id"] = reply_to

            send_data = await self._api_request(
                "POST",
                (
                    f"/v2/users/{chat_id}/messages"
                    if chat_type == "c2c"
                    else f"/v2/groups/{chat_id}/messages"
                ),
                body,
            )
            return SendResult(
                success=True,
                message_id=str(send_data.get("id", uuid.uuid4().hex[:12])),
                raw_response=send_data,
            )
        except UploadDailyLimitExceededError as exc:
            # 不可重试：每日配额已用尽。给调用方可操作的文本，
            # 以便模型组织友好的回复。
            logger.warning(
                "[%s] Daily upload limit exceeded for %s (%s)",
                self._log_tag, exc.file_name, exc.file_size_human,
            )
            return SendResult(
                success=False,
                error=(
                    f"QQ daily upload limit exceeded for {exc.file_name!r} "
                    f"({exc.file_size_human}). Retry tomorrow."
                ),
                retryable=False,
            )
        except UploadFileTooLargeError as exc:
            logger.warning(
                "[%s] File too large: %s (%s, platform limit %s)",
                self._log_tag, exc.file_name, exc.file_size_human, exc.limit_human,
            )
            return SendResult(
                success=False,
                error=(
                    f"{exc.file_name!r} ({exc.file_size_human}) exceeds the "
                    f"QQ per-file upload limit ({exc.limit_human})."
                ),
                retryable=False,
            )
        except Exception as exc:
            logger.error("[%s] Media send failed: %s", self._log_tag, exc)
            return SendResult(success=False, error=str(exc))

    async def _upload_local_file(
            self,
            chat_type: str,
            chat_id: str,
            media_source: str,
            file_type: int,
            file_name: Optional[str],
    ) -> Tuple[str, Dict[str, Any]]:
        """分块上传本地文件并返回 ``(resolved_name, complete_response)``。

        返回的 ``complete_response`` 包含将填入后续 RichMedia 消息体的
        ``file_info`` token。

        :raises UploadDailyLimitExceededError: biz_code 为 40093002 时。
        :raises UploadFileTooLargeError: 文件超出平台限制时。
        :raises FileNotFoundError: 路径不存在时。
        :raises ValueError: 路径看起来像占位符（``<path>``）时。
        :raises RuntimeError: HTTP client 未初始化时。
        """
        if not self._http_client:
            raise RuntimeError("HTTP client not initialized — not connected?")

        local_path = Path(media_source).expanduser()
        if not local_path.is_absolute():
            local_path = (Path.cwd() / local_path).resolve()

        if not local_path.exists() or not local_path.is_file():
            if media_source.startswith("<") or len(media_source) < 3:
                raise ValueError(
                    f"Invalid media source (looks like a placeholder): {media_source!r}"
                )
            raise FileNotFoundError(f"Media file not found: {local_path}")

        resolved_name = file_name or local_path.name
        uploader = ChunkedUploader(
            api_request=self._api_request,
            http_put=self._http_client.put,
            log_tag=self._log_tag,
        )
        complete = await uploader.upload(
            chat_type=chat_type,
            target_id=chat_id,
            file_path=str(local_path),
            file_type=file_type,
            file_name=resolved_name,
        )
        return resolved_name, complete

    async def _load_media(
            self, source: str, file_name: Optional[str] = None
    ) -> Tuple[str, str, str]:
        """从 URL 或本地路径加载媒体。返回 (base64_or_url, content_type, filename)。"""
        source = str(source).strip()
        if not source:
            raise ValueError("Media source is required")

        parsed = urlparse(source)
        if parsed.scheme in {"http", "https"}:
            # 对于 URL，直接透传给上传 API
            content_type = mimetypes.guess_type(source)[0] or "application/octet-stream"
            resolved_name = file_name or Path(parsed.path).name or "media"
            return source, content_type, resolved_name

        # 本地文件 —— 编码为原始 base64 以填入 QQ Bot API 的 file_data 字段。
        # QQ API 期望纯 base64，而非 data URI。
        local_path = Path(source).expanduser()
        if not local_path.is_absolute():
            local_path = (Path.cwd() / local_path).resolve()

        if not local_path.exists() or not local_path.is_file():
            # 防御 LLM 有时输出的占位符路径（如 "<path>"），
            # 而非真实文件路径。
            if source.startswith("<") or len(source) < 3:
                raise ValueError(
                    f"Invalid media source (looks like a placeholder): {source!r}"
                )
            raise FileNotFoundError(f"Media file not found: {local_path}")

        raw = local_path.read_bytes()
        resolved_name = file_name or local_path.name
        content_type = (
                mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
        )
        b64 = base64.b64encode(raw).decode("ascii")
        return b64, content_type, resolved_name

    # ------------------------------------------------------------------
    # 输入指示器
    # ------------------------------------------------------------------

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """向 C2C 用户发送输入通知（仅 C2C 支持）。

        去抖为约每 50 秒一次请求（API 会设置 60 秒指示器）。
        QQ API 要求提供原始消息 ID —— 从 ``_on_message`` 填充的
        ``_last_msg_id`` 中获取。
        """
        if not self.is_connected:
            return

        chat_type = self._guess_chat_type(chat_id)
        if chat_type != "c2c":
            return

        msg_id = self._last_msg_id.get(chat_id)
        if not msg_id:
            return

        # 去抖 —— 若最近已发送则跳过
        now = time.time()
        last_sent = self._typing_sent_at.get(chat_id, 0.0)
        if now - last_sent < self._TYPING_DEBOUNCE_SECONDS:
            return

        try:
            msg_seq = self._next_msg_seq(chat_id)
            body = {
                "msg_type": MSG_TYPE_INPUT_NOTIFY,
                "msg_id": msg_id,
                "input_notify": {
                    "input_type": 1,
                    "input_second": self._TYPING_INPUT_SECONDS,
                },
                "msg_seq": msg_seq,
            }
            await self._api_request("POST", f"/v2/users/{chat_id}/messages", body)
            self._typing_sent_at[chat_id] = now
        except Exception as exc:
            logger.debug("[%s] send_typing failed: %s", self._log_tag, exc)

    # ------------------------------------------------------------------
    # 格式化
    # ------------------------------------------------------------------

    def format_message(self, content: str) -> str:
        """为 QQ 格式化消息。

        当启用 markdown_support 时，内容原样发送（由 QQ 渲染）。
        当禁用时，通过共享 helper 去除 markdown（与 BlueBubbles/SMS 相同）。
        """
        if self._markdown_support:
            return content
        return strip_markdown(content)

    # ------------------------------------------------------------------
    # 聊天信息
    # ------------------------------------------------------------------

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """基于聊天类型启发式返回聊天信息。"""
        chat_type = self._guess_chat_type(chat_id)
        return {
            "name": chat_id,
            "type": "group" if chat_type in {"group", "guild"} else "dm",
        }

    # ------------------------------------------------------------------
    # 辅助函数
    # ------------------------------------------------------------------

    @staticmethod
    def _is_url(source: str) -> bool:
        return urlparse(str(source)).scheme in {"http", "https"}

    def _guess_chat_type(self, chat_id: str) -> str:
        """根据已存储的入站元数据判定聊天类型，回退到 'c2c'。"""
        if chat_id in self._chat_type_map:
            return self._chat_type_map[chat_id]
        return "c2c"

    @staticmethod
    def _strip_at_mention(content: str) -> str:
        """去除群消息内容中的 @bot 提及前缀。"""
        # QQ 群 @ 消息可能以 bot 的 QQ 号/ID 作为前缀
        import re

        stripped = re.sub(r"^@\S+\s*", "", content.strip())
        return stripped

    def _is_dm_allowed(self, user_id: str) -> bool:
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return self._entry_matches(self._allow_from, user_id)
        return True

    def _is_group_allowed(self, group_id: str, user_id: str) -> bool:
        if self._group_policy == "disabled":
            return False
        if self._group_policy == "allowlist":
            return self._entry_matches(self._group_allow_from, group_id)
        return True

    @staticmethod
    def _entry_matches(entries: List[str], target: str) -> bool:
        normalized_target = str(target).strip().lower()
        for entry in entries:
            normalized = str(entry).strip().lower()
            if normalized == "*" or normalized == normalized_target:
                return True
        return False

    def _parse_qq_timestamp(self, raw: str) -> datetime:
        """解析 QQ API 时间戳（ISO 8601 字符串或整数毫秒）。

        QQ API 从整数毫秒改为了 ISO 8601 字符串。
        本方法优雅地兼容两种格式。
        """
        if not raw:
            return datetime.now(tz=timezone.utc)
        try:
            return datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            pass
        try:
            return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
        except (ValueError, TypeError):
            pass
        return datetime.now(tz=timezone.utc)

    def _is_duplicate(self, msg_id: str) -> bool:
        now = time.time()
        if len(self._seen_messages) > DEDUP_MAX_SIZE:
            cutoff = now - DEDUP_WINDOW_SECONDS
            self._seen_messages = {
                key: ts for key, ts in self._seen_messages.items() if ts > cutoff
            }
        if msg_id in self._seen_messages:
            return True
        self._seen_messages[msg_id] = now
        return False
