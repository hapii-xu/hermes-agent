"""ntfy 平台适配器（Hermes 插件）。

通过 HTTP 流（``/json`` 端点，``poll=false``）订阅 ntfy.sh 或任何自托管
ntfy 服务器上的 topic，并通过 HTTP POST 发布回复。无需外部 SDK——仅依赖
httpx，而 httpx 已经是 Hermes 的依赖项。

此适配器作为 Hermes 平台插件部署在 ``plugins/platforms/ntfy/`` 目录下。
Hermes 插件加载器在启动时扫描该目录，调用 :func:`register`，然后该平台
便可通过注册表供 ``gateway/run.py`` 和 ``tools/send_message_tool`` 使用
——无需修改核心文件。

config.yaml 中的配置::

    platforms:
      ntfy:
        enabled: true
        extra:
          server: "https://ntfy.sh"       # 或自托管 URL
          topic: "hermes-in"              # 订阅的 topic（接收消息）
          publish_topic: "hermes-out"     # 可选——默认与 topic 相同
          token: "..."                    # 可选的 Bearer / Basic 认证 token
          markdown: true                  # 可选——启用 markdown（默认: false）

环境变量（均在适配器构造时读取，环境变量优先级高于 config.yaml 的
``extra`` 配置）:

    NTFY_TOPIC                 要订阅的 topic（必需）
    NTFY_SERVER_URL            服务器 URL（默认: https://ntfy.sh）
    NTFY_TOKEN                 Bearer token 或用于 Basic 认证的 'user:pass'
    NTFY_PUBLISH_TOPIC         回复 topic（默认使用 NTFY_TOPIC）
    NTFY_MARKDOWN              "true"/"1"/"yes" 启用 X-Markdown header
    NTFY_ALLOWED_USERS         白名单（gateway 将其视为 user ID；
                               在 ntfy 中这些是 topic 名称）
    NTFY_ALLOW_ALL_USERS       允许任何 topic——仅限开发环境使用
    NTFY_HOME_CHANNEL          用于 cron / 通知投递的默认 topic
    NTFY_HOME_CHANNEL_NAME     主频道的人类可读标签

身份模型：ntfy 没有原生的认证用户身份。``title`` 字段由发布者控制，
不用于授权。每个 topic 被视为一个单独的可信频道——``user_id`` 固定为
topic 名称。对于任何真正的信任边界，请使用受 read token 保护的私有
topic。
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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
)

logger = logging.getLogger(__name__)


class _FatalStreamError(Exception):
    """当流错误不可恢复时抛出（例如 401、404）。"""


DEFAULT_SERVER = "https://ntfy.sh"
MAX_MESSAGE_LENGTH = 4096  # ntfy 消息体大小限制
DEDUP_WINDOW_SECONDS = 300
DEDUP_MAX_SIZE = 1000
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
STREAM_TIMEOUT_SECONDS = 90  # ntfy keepalive 默认值为 55s；留出余量
_ECHO_TAG = "hermes-agent"  # 附加到出站消息的标签，用于防止回声循环


def _build_auth_header(token: str) -> Dict[str, str]:
    """根据 ntfy token 构建 ``Authorization`` header。

    由 :class:`NtfyAdapter._auth_headers` 和 :func:`_standalone_send` 共用，
    确保两条路径遵循相同的认证格式和空白字符剥离规则。

    Token 会剥离首尾空白——粘贴的 token 经常带有尾部换行符，
    否则会导致 header 格式错误（``Authorization: Bearer foo\\n``）。
    ``user:pass`` 格式的 token 会使用 Basic 认证；其他任何内容都
    视为 Bearer token。未配置 token 时返回 ``{}``。
    """
    if not token:
        return {}
    token = token.strip()
    if not token:
        return {}
    if ":" in token:
        import base64
        encoded = base64.b64encode(token.encode()).decode()
        return {"Authorization": f"Basic {encoded}"}
    return {"Authorization": f"Bearer {token}"}


def _truncate_body(message: str, *, context: str) -> bytes:
    """应用 ntfy 的 4096 字符限制，截断时记录警告日志。

    ``context`` 会包含在日志消息中，以便区分适配器和独立发送两种截断场景。
    """
    if len(message) > MAX_MESSAGE_LENGTH:
        logger.warning(
            "%s: truncating message from %d to %d chars (ntfy limit)",
            context, len(message), MAX_MESSAGE_LENGTH,
        )
    return message[:MAX_MESSAGE_LENGTH].encode("utf-8")


def check_requirements() -> bool:
    """检查 ntfy 适配器是否可安装且已进行最低限度配置。

    直接读取 ``NTFY_TOPIC`` 以避免每次预检时执行完整的
    ``load_gateway_config()``（后者还会写入 ``os.environ``）。
    """
    if not HTTPX_AVAILABLE:
        return False
    topic = os.getenv("NTFY_TOPIC", "").strip()
    return bool(topic)


def validate_config(config) -> bool:
    """验证已配置的 ntfy 平台是否设置了 topic。"""
    extra = getattr(config, "extra", {}) or {}
    topic = extra.get("topic") or os.getenv("NTFY_TOPIC", "")
    return bool(topic)


def is_connected(config) -> bool:
    """检查 ntfy 是否已配置（通过环境变量或 config.yaml）。"""
    extra = getattr(config, "extra", {}) or {}
    topic = os.getenv("NTFY_TOPIC") or extra.get("topic", "")
    return bool(topic)


class NtfyAdapter(BasePlatformAdapter):
    """ntfy 适配器。

    通过 HTTP 流（``/json`` 端点）订阅 topic，并通过 HTTP POST 发布回复。
    无需外部 SDK——仅依赖 httpx。
    """

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        platform = Platform("ntfy")
        super().__init__(config=config, platform=platform)

        extra = config.extra or {}
        self._server: str = (
            extra.get("server")
            or os.getenv("NTFY_SERVER_URL", DEFAULT_SERVER)
        ).rstrip("/")
        self._topic: str = extra.get("topic") or os.getenv("NTFY_TOPIC", "")
        self._publish_topic: str = (
            extra.get("publish_topic")
            or os.getenv("NTFY_PUBLISH_TOPIC", "")
            or self._topic
        )
        self._token: str = extra.get("token") or os.getenv("NTFY_TOKEN", "")

        self._stream_task: Optional[asyncio.Task] = None
        self._http_client: Optional["httpx.AsyncClient"] = None

        # 消息去重：msg_id -> 时间戳
        self._seen_messages: Dict[str, float] = {}

    # -- 连接生命周期 -------------------------------------------------------

    async def connect(self) -> bool:
        """启动流式订阅任务，连接到 ntfy。"""
        if not HTTPX_AVAILABLE:
            logger.warning("[%s] httpx not installed. Run: pip install httpx", self.name)
            return False
        if not self._topic:
            logger.warning("[%s] NTFY_TOPIC not configured", self.name)
            return False

        try:
            self._http_client = httpx.AsyncClient(timeout=None)
            self._stream_task = asyncio.create_task(self._run_stream())
            self._mark_connected()
            logger.info("[%s] Connected — subscribing to %s/%s", self.name, self._server, self._topic)
            return True
        except Exception as e:
            logger.error("[%s] Failed to connect: %s", self.name, e)
            return False

    async def _run_stream(self) -> None:
        """订阅 ntfy topic，支持自动重连。"""
        backoff_idx = 0
        stream_start: float = 0.0
        url = f"{self._server}/{self._topic}/json"
        headers = self._auth_headers()

        while self._running:
            try:
                logger.debug("[%s] Opening stream to %s", self.name, url)
                stream_start = time.monotonic()
                await self._consume_stream(url, headers)
            except asyncio.CancelledError:
                return
            except _FatalStreamError:
                self._running = False
                return
            except Exception as e:
                if not self._running:
                    return
                logger.warning("[%s] Stream error: %s", self.name, e)

            if not self._running:
                return

            # 如果流保持连接超过 60s，重置退避计数器
            if time.monotonic() - stream_start >= 60.0:
                backoff_idx = 0
            delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
            logger.info("[%s] Reconnecting in %ds...", self.name, delay)
            await asyncio.sleep(delay)
            backoff_idx += 1

    async def _consume_stream(self, url: str, headers: Dict[str, str]) -> None:
        """打开 HTTP 流式连接并分发事件。"""
        # poll=false 保持持久的流式连接，通过 keepalive 事件维持
        params = {"poll": "false"}
        async with self._http_client.stream(
            "GET",
            url,
            headers=headers,
            params=params,
            timeout=httpx.Timeout(connect=15.0, read=STREAM_TIMEOUT_SECONDS, write=15.0, pool=15.0),
        ) as response:
            if response.status_code == 401:
                logger.error(
                    "[%s] Authentication failed (401) — stopping reconnect loop. Check NTFY_TOKEN.",
                    self.name,
                )
                self._set_fatal_error(
                    "ntfy_unauthorized",
                    "ntfy server rejected auth (401). Check NTFY_TOKEN.",
                    retryable=False,
                )
                raise _FatalStreamError("401 Unauthorized")
            if response.status_code == 404:
                logger.error(
                    "[%s] Topic not found (404): %s — stopping reconnect loop.",
                    self.name, self._topic,
                )
                self._set_fatal_error(
                    "ntfy_topic_not_found",
                    f"ntfy topic '{self._topic}' returned 404. Check NTFY_TOPIC.",
                    retryable=False,
                )
                raise _FatalStreamError("404 Not Found")
            response.raise_for_status()

            async for line in response.aiter_lines():
                if not self._running:
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("event") == "message":
                    await self._on_message(event)

    async def disconnect(self) -> None:
        """断开与 ntfy 的连接。"""
        self._running = False
        self._mark_disconnected()

        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
            self._stream_task = None

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        self._seen_messages.clear()
        logger.info("[%s] Disconnected", self.name)

    # -- 入站消息处理 -------------------------------------------------------

    async def _on_message(self, event: Dict[str, Any]) -> None:
        """处理传入的 ntfy 消息事件。"""
        msg_id = event.get("id") or uuid.uuid4().hex
        if self._is_duplicate(msg_id):
            logger.debug("[%s] Duplicate message %s, skipping", self.name, msg_id)
            return

        # 回声循环防护：跳过本适配器标记的消息
        tags = event.get("tags") or []
        if _ECHO_TAG in tags:
            logger.debug("[%s] Skipping own message (echo tag)", self.name)
            return

        text = (event.get("message") or "").strip()
        if not text:
            logger.debug("[%s] Empty message body, skipping", self.name)
            return

        topic = event.get("topic") or self._topic
        # ntfy 没有原生的认证用户身份。title 字段由发布者控制，
        # 不应用于授权——任何知道 topic 的发布者都可以将 title 设为
        # 允许的用户名。将 ntfy 视为单一可信频道；user_id 固定为
        # topic 名称。仅当 topic 本身受 read token 保护时，
        # NTFY_ALLOWED_USERS 才构成真正的信任边界。
        user_id = topic
        user_name = topic

        source = self.build_source(
            chat_id=topic,
            chat_name=topic,
            chat_type="dm",
            user_id=user_id,
            user_name=user_name,
        )

        unix_ts = event.get("time")
        try:
            timestamp = (
                datetime.fromtimestamp(int(unix_ts), tz=timezone.utc)
                if unix_ts else datetime.now(tz=timezone.utc)
            )
        except (ValueError, OSError, TypeError):
            timestamp = datetime.now(tz=timezone.utc)

        message_event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=msg_id,
            raw_message=event,
            timestamp=timestamp,
        )

        logger.debug("[%s] Message on topic %s: %s", self.name, topic, text[:80])
        await self.handle_message(message_event)

    # -- 消息去重 -----------------------------------------------------------

    def _is_duplicate(self, msg_id: str) -> bool:
        """如果此消息 ID 在去重窗口内已被见过，返回 True。"""
        now = time.time()
        if len(self._seen_messages) > DEDUP_MAX_SIZE:
            cutoff = now - DEDUP_WINDOW_SECONDS
            self._seen_messages = {k: v for k, v in self._seen_messages.items() if v > cutoff}

        if msg_id in self._seen_messages:
            return True
        self._seen_messages[msg_id] = now
        return False

    # -- 出站消息 -----------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """向已配置的 publish topic 发布消息。"""
        metadata = metadata or {}
        publish_topic = metadata.get("publish_topic") or self._publish_topic or chat_id

        if not self._http_client:
            return SendResult(success=False, error="HTTP client not initialized")

        url = f"{self._server}/{publish_topic}"
        markdown_enabled = (self.config.extra or {}).get("markdown", False)
        headers = {
            **self._auth_headers(),
            "Content-Type": "text/plain; charset=utf-8",
            "X-Tags": _ECHO_TAG,
        }
        if markdown_enabled:
            headers["X-Markdown"] = "true"

        if len(content) > self.MAX_MESSAGE_LENGTH:
            logger.warning(
                "[%s] Message truncated from %d to %d chars (ntfy limit)",
                self.name, len(content), self.MAX_MESSAGE_LENGTH,
            )
        body = content[:self.MAX_MESSAGE_LENGTH]

        try:
            resp = await self._http_client.post(
                url, content=body.encode("utf-8"), headers=headers, timeout=15.0,
            )
            if resp.status_code < 300:
                try:
                    data = resp.json()
                    returned_id = data.get("id") or uuid.uuid4().hex[:12]
                except Exception:
                    returned_id = uuid.uuid4().hex[:12]
                return SendResult(success=True, message_id=returned_id)
            body_text = resp.text
            logger.warning("[%s] Send failed HTTP %d: %s", self.name, resp.status_code, body_text[:200])
            return SendResult(success=False, error=f"HTTP {resp.status_code}: {body_text[:200]}")
        except httpx.TimeoutException:
            return SendResult(success=False, error="Timeout publishing to ntfy")
        except Exception as e:
            logger.error("[%s] Send error: %s", self.name, e)
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """ntfy 不支持输入指示器。"""
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """返回 ntfy topic 的基本信息。"""
        return {"name": chat_id, "type": "dm"}

    # -- 辅助方法 -----------------------------------------------------------

    def _auth_headers(self) -> Dict[str, str]:
        """如果配置了 token，构建 Authorization header。"""
        return _build_auth_header(self._token)


# ---------------------------------------------------------------------------
# 插件注册
# ---------------------------------------------------------------------------


def _env_enablement() -> dict | None:
    """在 gateway 配置加载期间从环境变量初始化 ``PlatformConfig.extra``。

    由平台注册表的环境变量启用钩子在适配器构造之前调用，使
    ``gateway status`` 和 ``get_connected_platforms()`` 能够反映仅通过
    环境变量配置的状态，而无需实例化 HTTP client。
    当 ntfy 未进行最低限度配置时返回 ``None``；调用方将跳过自动启用。

    返回字典中的特殊 ``home_channel`` 键由核心钩子处理——它会成为
    ``PlatformConfig`` 上的正式 ``HomeChannel`` 数据类，而不是合并到 ``extra`` 中。
    """
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        return None
    seed: dict = {
        "topic": topic,
        "server": os.getenv("NTFY_SERVER_URL", DEFAULT_SERVER).rstrip("/"),
    }
    publish_topic = os.getenv("NTFY_PUBLISH_TOPIC", "").strip()
    if publish_topic:
        seed["publish_topic"] = publish_topic
    token = os.getenv("NTFY_TOKEN", "").strip()
    if token:
        seed["token"] = token
    markdown = os.getenv("NTFY_MARKDOWN", "").strip().lower()
    if markdown:
        seed["markdown"] = markdown in ("1", "true", "yes")
    home = os.getenv("NTFY_HOME_CHANNEL", "").strip() or topic
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("NTFY_HOME_CHANNEL_NAME", home),
        }
    return seed


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """进程外发布，用于 cron / send_message_tool 的回退路径。

    由 ``tools/send_message_tool._send_via_adapter`` 和 cron 调度器在
    gateway runner 不在当前进程中时使用（例如 ``hermes cron`` 独立运行）。
    没有此钩子时，``deliver=ntfy`` 的 cron 任务会因
    ``No live adapter for platform`` 而失败。

    ``thread_id`` 和 ``media_files`` 仅为签名一致性而接受——ntfy 没有
    消息线程或附件的原生支持。如果设置了 ``NTFY_MARKDOWN`` 或
    ``pconfig.extra["markdown"]`` 为 True，则启用 markdown。
    """
    if not HTTPX_AVAILABLE:
        return {"error": "ntfy standalone send: httpx not installed"}

    extra = getattr(pconfig, "extra", {}) or {}
    server = (
        extra.get("server")
        or os.getenv("NTFY_SERVER_URL", DEFAULT_SERVER)
    ).rstrip("/")
    publish_topic = (
        chat_id
        or extra.get("publish_topic")
        or os.getenv("NTFY_PUBLISH_TOPIC", "").strip()
        or extra.get("topic")
        or os.getenv("NTFY_TOPIC", "").strip()
    )
    if not publish_topic:
        return {"error": "ntfy standalone send: NTFY_TOPIC not configured"}

    token = extra.get("token") or os.getenv("NTFY_TOKEN", "")
    markdown_env = os.getenv("NTFY_MARKDOWN", "").strip().lower()
    markdown_enabled = bool(extra.get("markdown")) or markdown_env in ("1", "true", "yes")

    headers = {"Content-Type": "text/plain; charset=utf-8", "X-Tags": _ECHO_TAG, **_build_auth_header(token)}
    if markdown_enabled:
        headers["X-Markdown"] = "true"

    body = _truncate_body(message, context="ntfy standalone")

    url = f"{server}/{publish_topic}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, content=body, headers=headers)
        if resp.status_code >= 300:
            return {"error": f"ntfy HTTP {resp.status_code}: {resp.text[:200]}"}
        try:
            data = resp.json()
            msg_id = data.get("id") or uuid.uuid4().hex[:12]
        except Exception:
            msg_id = uuid.uuid4().hex[:12]
        return {"success": True, "platform": "ntfy", "chat_id": publish_topic, "message_id": msg_id}
    except Exception as e:
        return {"error": f"ntfy standalone send failed: {e}"}


def register(ctx) -> None:
    """插件入口点——由 Hermes 插件系统在启动时调用。"""
    ctx.register_platform(
        name="ntfy",
        label="ntfy",
        adapter_factory=lambda cfg: NtfyAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["NTFY_TOPIC"],
        install_hint="pip install httpx   # already a Hermes dependency",
        # 环境变量驱动的自动配置：初始化 PlatformConfig.extra，使仅通过环境变量
        # 配置的设置能在 `hermes gateway status` 中显示，而无需实例化 HTTP client。
        env_enablement_fn=_env_enablement,
        # Cron home-channel 投递支持——设置后 `deliver=ntfy` 的 cron 任务
        # 会路由到 NTFY_HOME_CHANNEL。
        cron_deliver_env_var="NTFY_HOME_CHANNEL",
        # 进程外 cron 投递。没有此钩子时，当 cron 与 gateway 分开运行，
        # deliver=ntfy 的 cron 任务会因 "No live adapter" 而失败。
        standalone_sender_fn=_standalone_send,
        # 用于 _is_user_authorized() 集成的认证环境变量。
        allowed_users_env="NTFY_ALLOWED_USERS",
        allow_all_env="NTFY_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="🔔",
        # ntfy 发布者没有持久身份——topic 名称是唯一的标识符，
        # 没有手机号/邮箱等需要脱敏的信息。
        pii_safe=True,
        allow_update_command=True,
        platform_hint=(
            "You are communicating via ntfy push notifications. "
            "Use plain text by default — ntfy supports optional markdown "
            "(set markdown: true in config or NTFY_MARKDOWN=true). "
            "Keep responses concise; ntfy is a push notification service "
            "with a 4096-character per-message limit."
        ),
    )
