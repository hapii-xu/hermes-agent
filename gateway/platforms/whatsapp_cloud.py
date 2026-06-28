"""
WhatsApp Cloud API 适配器 —— 官方 Meta WhatsApp Business Platform。

本适配器是 ``whatsapp.py``（Baileys bridge）的 *补充*，而非替代。
两者相互独立：

- ``whatsapp.py``      —— 非官方 Baileys bridge，面向个人账号，无需
                         公网 URL，存在封号风险。
- ``whatsapp_cloud.py``（本文件）—— 官方 Meta Cloud API，需要 Business
                         账号，需要公网 webhook URL，基于 token 的鉴权。

两者通过 ``WhatsAppBehaviorMixin`` 共享门控 / mention / 格式化行为。

阶段范围（本文件随阶段演进）：
- Phase 2 —— 通过 Graph API 出站文本 + 带 verify-token 握手的 webhook 服务器。
- Phase 3 —— X-Hub-Signature-256 HMAC 校验（原始 body，常数时间）+ wamid
            重放保护 + 通过 handle_message 分发。Phase 3 适配器已可端到端
            用于文本 DM。
- Phase 4 —— 媒体上传 + 发送（图片/视频/音频/文档），通过 Graph media
            端点下载入站媒体，通过 ffmpeg 进行语音 opus 转换，并在 ffmpeg
            不在 PATH 中时优雅回退为 MP3。可读类型的文档文本注入。
- Phase 5 —— 24 小时会话窗口 + 模板兜底。

启用本适配器所需的环境变量：
- WHATSAPP_CLOUD_PHONE_NUMBER_ID  （Graph URL 路径组件）
- WHATSAPP_CLOUD_ACCESS_TOKEN     （System User 永久 token）

可选 / Phase-3+：
- WHATSAPP_CLOUD_APP_ID
- WHATSAPP_CLOUD_APP_SECRET       （X-Hub-Signature-256 的 HMAC 密钥）
- WHATSAPP_CLOUD_WABA_ID          （分析 / 未来用途）
- WHATSAPP_CLOUD_VERIFY_TOKEN     （hub.verify_token 共享密钥）
- WHATSAPP_CLOUD_WEBHOOK_HOST     （默认 0.0.0.0）
- WHATSAPP_CLOUD_WEBHOOK_PORT     （默认 8090）
- WHATSAPP_CLOUD_WEBHOOK_PATH     （默认 /whatsapp/webhook）
- WHATSAPP_CLOUD_API_VERSION      （默认 v20.0）
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import mimetypes
import os
import re
import shutil
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

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
    SUPPORTED_DOCUMENT_TYPES,
)
from gateway.platforms.whatsapp_common import WhatsAppBehaviorMixin
from hermes_constants import get_hermes_dir

logger = logging.getLogger(__name__)


DEFAULT_API_VERSION = "v20.0"
DEFAULT_WEBHOOK_HOST = "0.0.0.0"
DEFAULT_WEBHOOK_PORT = 8090
DEFAULT_WEBHOOK_PATH = "/whatsapp/webhook"
GRAPH_API_BASE = "https://graph.facebook.com"
# Meta 最长会重试失败的 webhook 长达 7 天。我们不需要为整个重试窗口
# 记住每一个 wamid —— 真正的风险是几分钟内的重复投递，而不是几天。
# 5000 条记录配合 FIFO 淘汰对正常流量绰绰有余，且限制了内存占用。
WAMID_DEDUP_CACHE_SIZE = 5000
# interactive-button 状态字典与每聊天 last-wamid 缓存的上限。
# 对任何现实数量的在途提示 / 聊天都已足够宽裕。
INTERACTIVE_STATE_CACHE_SIZE = 1000

# Meta 为 Cloud API /media 端点文档记录的按类型大小上限。
# 这些是硬性限制；对超过上限的上传我们以干净错误拒绝，而不是
# 往返 Graph 才被拒绝。
# https://developers.facebook.com/docs/whatsapp/cloud-api/reference/media
_MEDIA_SIZE_LIMITS = {
    "image": 5 * 1024 * 1024,        # 5 MB（JPEG、PNG）
    "video": 16 * 1024 * 1024,       # 16 MB
    "audio": 16 * 1024 * 1024,       # 16 MB（MP3、AAC、AMR、OGG opus）
    "document": 100 * 1024 * 1024,   # 100 MB
    "sticker": 100 * 1024,           # 100 KB 动态，500 KB 静态
}

# 当无法从路径扩展名猜测时的默认 MIME 类型。
_DEFAULT_MIME = {
    "image": "image/jpeg",
    "video": "video/mp4",
    "audio": "audio/mpeg",
    "document": "application/octet-stream",
    "sticker": "image/webp",
}

# 导入时的 ffmpeg 位置。``shutil.which`` 在 Windows 上遵守 PATHEXT，
# 因此用户的 ``ffmpeg.exe`` 会被识别。为 None 时表示 MP3 语音回退为
# WhatsApp 中的 "audio file attachment" 渲染。
_FFMPEG_PATH = shutil.which("ffmpeg")

# Python 的 mimetypes 模块对某些类型返回 RFC 正确但在现实不常见的
# 扩展名（audio/ogg → .oga（RFC 5334）；audio/mp4 → .mp4 而非语音消息
# 事实标准的 .m4a）。我们下游的 STT 流水线白名单了现实中常见的扩展名，
# 因此覆盖 Meta 发送的少数不匹配默认值的类型。
_WHATSAPP_MIME_EXTENSION_OVERRIDES: Dict[str, str] = {
    # WhatsApp 语音消息 —— Ogg 容器中的 opus codec。
    "audio/ogg": ".ogg",
    "audio/x-opus+ogg": ".ogg",
    "audio/opus": ".ogg",
    # iOS 语音备忘 —— MP4 容器中的 AAC；STT 工具期望 .m4a。
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    # 图片 —— mimetypes 偶尔返回 .jpe（legacy IANA）而非 .jpg，
    # 这会让按扩展名切换的工具出错。
    "image/jpeg": ".jpg",
}


def _ext_for_mime(mime: str) -> Optional[str]:
    """将 MIME 类型解析为我们想要的磁盘文件扩展名。

    先查询覆盖映射，使 ``audio/ogg`` 这类类型产生下游工具真正能接受的
    扩展名（``.ogg``，而不是技术上正确但实际损坏的 ``.oga``）。
    对未固定的类型回退到 Python 的 ``mimetypes.guess_extension``。
    """
    if not mime:
        return None
    primary = mime.split(";")[0].strip().lower()
    override = _WHATSAPP_MIME_EXTENSION_OVERRIDES.get(primary)
    if override:
        return override
    return mimetypes.guess_extension(primary) or None


# 入站媒体缓存位于用户的 hermes 目录下，因此能跨重启和 gateway 重载存活
# —— 与 Baileys bridge 使用的约定相同。
_INBOUND_MEDIA_CACHE = Path(get_hermes_dir("platforms/whatsapp_cloud/media", "whatsapp_cloud/media"))


def check_whatsapp_cloud_requirements() -> bool:
    """返回传输依赖是否可用。

    webhook 服务器（入站）需要 aiohttp。Graph API 调用（出站）需要 httpx。
    两者都随 hermes-agent 的默认依赖集分发，因此在正常安装中应始终为 True。
    """
    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE


class WhatsAppCloudAdapter(WhatsAppBehaviorMixin, BasePlatformAdapter):
    """WhatsApp Business Cloud API 适配器。

    出站：HTTPS POST 到 ``graph.facebook.com/<api_version>/<phone_id>/messages``。
    入站：接受 Meta webhook 负载的 aiohttp 服务器。

    mixin 必须位于 bases 列表的最前面，使其 ``format_message`` 覆盖
    ``BasePlatformAdapter.format_message``（base 提供了一个不会将 Markdown
    转换为 WhatsApp 语法的通用实现）。Baileys 适配器同样如此。
    """

    splits_long_messages = True  # send() 通过 truncate_message() 分块

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WHATSAPP_CLOUD)
        extra = config.extra or {}

        # 必需
        self._phone_number_id: str = str(extra.get("phone_number_id", "")).strip()
        self._access_token: str = str(extra.get("access_token", "")).strip()

        # 可选 / 在后续阶段使用
        self._app_id: str = str(extra.get("app_id", "")).strip()
        self._app_secret: str = str(extra.get("app_secret", "")).strip()
        self._waba_id: str = str(extra.get("waba_id", "")).strip()
        self._verify_token: str = str(extra.get("verify_token", "")).strip()

        # webhook 服务器配置
        self._webhook_host: str = str(extra.get("webhook_host", DEFAULT_WEBHOOK_HOST))
        self._webhook_port: int = int(extra.get("webhook_port", DEFAULT_WEBHOOK_PORT))
        self._webhook_path: str = self._normalize_path(
            extra.get("webhook_path", DEFAULT_WEBHOOK_PATH)
        )
        self._health_path: str = self._normalize_path(
            extra.get("health_path", "/health")
        )

        # Graph API
        self._api_version: str = str(extra.get("api_version", DEFAULT_API_VERSION))

        # Behavior-mixin 契约：这些名称由 mixin 的门控方法读取。
        # WHATSAPP_CLOUD_* 环境变量优先，使两个适配器能以各自独立策略
        # 并行运行；共享的 WHATSAPP_* 名称作为单适配器设置的兜底。
        import os

        self._reply_prefix: Optional[str] = extra.get("reply_prefix")
        self._dm_policy: str = str(
            extra.get("dm_policy")
            or os.getenv("WHATSAPP_CLOUD_DM_POLICY")
            or os.getenv("WHATSAPP_DM_POLICY", "open")
        ).strip().lower()
        self._allow_from: set[str] = self._normalize_allow_ids(
            self._coerce_allow_list(
                extra.get("allow_from")
                or extra.get("allowFrom")
                or os.getenv("WHATSAPP_CLOUD_ALLOW_FROM")
            )
        )
        self._group_policy: str = str(
            extra.get("group_policy")
            or os.getenv("WHATSAPP_CLOUD_GROUP_POLICY")
            or os.getenv("WHATSAPP_GROUP_POLICY", "open")
        ).strip().lower()
        self._group_allow_from: set[str] = self._normalize_allow_ids(
            self._coerce_allow_list(
                extra.get("group_allow_from")
                or extra.get("groupAllowFrom")
                or os.getenv("WHATSAPP_CLOUD_GROUP_ALLOW_FROM")
            )
        )
        self._mention_patterns = self._compile_mention_patterns()

        # webhook 去重状态 —— wamid → True。OrderedDict 提供 O(1) FIFO
        # 淘汰。仅存于内存；Phase 5 可能提升到 SessionDB，如果我们决定
        # 需要跨 gateway 重启的重放保护。
        self._seen_wamids: "OrderedDict[str, bool]" = OrderedDict()
        self._duplicate_count: int = 0
        self._accepted_count: int = 0
        self._rejected_signature_count: int = 0

        # 用于警告的一次性标志，否则会刷屏日志。
        self._warned_no_ffmpeg: bool = False

        # 每聊天的最新入站 wamid 缓存。Meta 的 typing 指示符 + 已读回执
        # API 需要附加到一个具体的 message_id（通常是 "会话中最新的消息"）。
        # 我们在每条接受的入站消息上刷新它，使 ``send_typing`` 始终有
        # 一个有效目标，而无需在 gateway 的 base 契约里塞一个额外的 kwarg。
        # 仅存于内存；gateway 重启时下一条入站消息会重新填充。
        self._last_inbound_wamid_by_chat: "OrderedDict[str, str]" = OrderedDict()

        # Interactive-button 状态。每个映射短 id（嵌入出站 button 负载）→
        # gateway 解析器所需的 session/correlation key。分发表见
        # ``_handle_interactive_reply``。条目在用户点击按钮时弹出；
        # 否则被忽略的提示会永久累积，因此每个字典通过 _bounded_put 进行
        # FIFO 上限（最旧的待处理提示先淘汰 —— 被淘汰的按钮点击会降级为
        # 纯文本兜底路径，与 gateway 重启后一样）。
        #   _clarify_state:        clarify_id → session_key（通过
        #                          tools.clarify_gateway.resolve_gateway_clarify 解析）
        #   _exec_approval_state:  approval_id → session_key（通过
        #                          tools.approval.resolve_gateway_approval 解析）
        #   _slash_confirm_state:  confirm_id → session_key（通过
        #                          tools.slash_confirm.resolve 解析）
        self._clarify_state: "OrderedDict[str, str]" = OrderedDict()
        self._exec_approval_state: "OrderedDict[str, str]" = OrderedDict()
        self._slash_confirm_state: "OrderedDict[str, str]" = OrderedDict()

        # 运行时
        self._runner = None
        self._http_client: Optional["httpx.AsyncClient"] = None

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _normalize_path(path: Any) -> str:
        raw = str(path or "").strip() or "/"
        return raw if raw.startswith("/") else f"/{raw}"

    def _graph_url(self, path: str) -> str:
        """为本适配器的 phone-number 作用域构建 Graph API URL。"""
        if path.startswith("/"):
            path = path[1:]
        return f"{GRAPH_API_BASE}/{self._api_version}/{self._phone_number_id}/{path}"

    @staticmethod
    def _bounded_put(cache: "OrderedDict[str, str]", key: str, value: str) -> None:
        """插入到 FIFO 上限的 OrderedDict，淘汰最旧的条目。"""
        cache[key] = value
        while len(cache) > INTERACTIVE_STATE_CACHE_SIZE:
            cache.popitem(last=False)

    def _effective_reply_prefix(self) -> str:
        """Cloud API 没有 self-chat 概念 —— 永不附加回复前缀。

        覆盖 mixin 默认值（后者依据 WHATSAPP_MODE=self-chat，
        这是一个仅 Baileys 才有的设置）。
        """
        if self._reply_prefix is not None:
            return self._reply_prefix.replace("\\n", "\n")
        return ""

    @staticmethod
    def _normalize_allow_ids(ids: set[str]) -> set[str]:
        """将白名单条目规范化为纯 wa_id 形式。

        Cloud API 以纯 wa_id（数字、无 JID 后缀）标识用户，而 Baileys
        使用 ``<digits>@s.whatsapp.net`` JID。在两个适配器之间共享白名单
        （或粘贴带 ``+`` 或分隔符的 JID/电话号码）的用户也应能匹配，
        因此剥离任何 ``@...`` 后缀和非数字字符。
        """
        normalized: set[str] = set()
        for entry in ids:
            bare = entry.split("@", 1)[0]
            digits = re.sub(r"\D", "", bare)
            normalized.add(digits or entry)
        return normalized

    def _is_dm_allowed(self, sender_id: str) -> bool:
        """针对规范化后的纯 wa_id 进行白名单检查。"""
        if self._dm_policy == "allowlist":
            bare = re.sub(r"\D", "", str(sender_id).split("@", 1)[0])
            return (bare or sender_id) in self._allow_from
        return super()._is_dm_allowed(sender_id)

    # ------------------------------------------------------------------ lifecycle
    async def connect(self) -> bool:
        if not check_whatsapp_cloud_requirements():
            self._set_fatal_error(
                "whatsapp_cloud_deps_missing",
                "aiohttp and httpx are required for whatsapp_cloud — "
                "reinstall hermes-agent.",
                retryable=False,
            )
            return False
        if not self._phone_number_id or not self._access_token:
            self._set_fatal_error(
                "whatsapp_cloud_unconfigured",
                "WHATSAPP_CLOUD_PHONE_NUMBER_ID and WHATSAPP_CLOUD_ACCESS_TOKEN "
                "are required.",
                retryable=False,
            )
            return False

        # 出站 HTTP 客户端。更紧的 keepalive 与其他平台适配器一致，
        # 使空闲的 CLOSE_WAIT 尽快排空（#18451）。
        from gateway.platforms._http_client_limits import platform_httpx_limits

        self._http_client = httpx.AsyncClient(
            timeout=30.0, limits=platform_httpx_limits()
        )

        # 入站 webhook 服务器。
        app = web.Application()
        app.router.add_get(self._health_path, self._handle_health)
        app.router.add_get(self._webhook_path, self._handle_verify)
        app.router.add_post(self._webhook_path, self._handle_webhook)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._webhook_host, self._webhook_port)
        await site.start()

        self._mark_connected()
        logger.info(
            "[whatsapp_cloud] Listening on %s:%d%s (Graph %s, phone_id=%s)",
            self._webhook_host,
            self._webhook_port,
            self._webhook_path,
            self._api_version,
            self._phone_number_id,
        )
        if not self._verify_token:
            logger.warning(
                "[whatsapp_cloud] WHATSAPP_CLOUD_VERIFY_TOKEN is not set — "
                "the GET subscription handshake will fail until it is."
            )
        if not self._app_secret:
            logger.warning(
                "[whatsapp_cloud] WHATSAPP_CLOUD_APP_SECRET is not set — "
                "incoming webhook POSTs will be refused with 503. Set "
                "the app secret to enable inbound message delivery."
            )
        return True

    async def disconnect(self) -> None:
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                logger.exception("[whatsapp_cloud] webhook server cleanup failed")
            self._runner = None
        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception:
                logger.exception("[whatsapp_cloud] http client close failed")
            self._http_client = None
        self._mark_disconnected()

    # ------------------------------------------------------------------ outbound
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """通过 Graph API 发送一条文本消息。

        ``chat_id`` 是接收者的 WhatsApp ID（``wa_id``）—— 通常是带国家
        代码的电话号码，不带加号。
        """
        if self._http_client is None:
            return SendResult(success=False, error="Not connected")
        if not content or not content.strip():
            return SendResult(success=True, message_id=None)

        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, self._outgoing_chunk_limit())

        url = self._graph_url("messages")
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

        last_message_id: Optional[str] = None
        for idx, chunk in enumerate(chunks):
            payload: Dict[str, Any] = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": chat_id,
                "type": "text",
                "text": {"body": chunk, "preview_url": True},
            }
            if reply_to and idx == 0:
                # 仅在第一个分块上引用用户消息。
                payload["context"] = {"message_id": reply_to}
            try:
                resp = await self._http_client.post(url, headers=headers, json=payload)
            except Exception as exc:
                logger.exception("[whatsapp_cloud] send failed")
                return SendResult(success=False, error=str(exc))

            if resp.status_code != 200:
                # Meta 在 body 中返回结构化错误 —— 将其上抛给调用方，
                # 使日志行具备可操作的上下文。
                try:
                    body = resp.json()
                except Exception:
                    body = {"raw": resp.text[:500]}
                error_msg = self._format_graph_error(body, resp.status_code)
                logger.warning(
                    "[whatsapp_cloud] send rejected (status=%d): %s",
                    resp.status_code,
                    error_msg,
                )
                return SendResult(success=False, error=error_msg)

            try:
                data = resp.json()
                ids = data.get("messages") or []
                if ids:
                    last_message_id = ids[0].get("id")
            except Exception:
                pass

        return SendResult(success=True, message_id=last_message_id)

    # ------------------------------------------------------------------ typing 指示符 + 已读回执
    #
    # Meta 将这两者耦合进单次 API 调用：一个带 ``status: "read"`` 的
    # POST /messages 会将消息标记为已读（蓝色双勾），可选的
    # ``typing_indicator`` 字段还会在用户的聊天 UI 中显示 "typing..."
    # 小提示。该指示符在我们回复或 25 秒后自动消失（取先到者）——
    # 因此完全契合 "我看到你的消息了，正在回复" 的 UX。
    #
    # API 需要附加到一个具体的 message_id。我们在
    # _last_inbound_wamid_by_chat 中按聊天缓存最新入站 wamid
    # （在 _build_message_event_from_cloud 中刷新），使本方法能查到它，
    # 而无需 gateway base 契约把 event.message_id 一路塞进
    # send_typing 的签名。

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """将最新入站消息标记为已读，并在用户的聊天 UI 中显示 typing 指示符。

        尽力而为：任何错误（尚无入站 wamid、网络失败、过期 token、
        消息超过 30 天）都会被静默吞掉，使 agent 的主回复路径不会
        被 UX 修饰阻塞。
        """
        if self._http_client is None:
            return
        wamid = self._last_inbound_wamid_by_chat.get(chat_id)
        if not wamid:
            # 此聊天尚无入站消息（或缓存因重启被清空）—— 跳过。
            # 下一条入站消息会重新填充。
            return

        url = self._graph_url("messages")
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": wamid,
            "typing_indicator": {"type": "text"},
        }
        try:
            resp = await self._http_client.post(url, headers=headers, json=payload)
        except Exception:
            # 网络 / 连接错误 —— 静默失败。Typing UX 绝不能阻塞消息分发。
            return
        # 尽力而为：上抛 4xx 供运维可见，但不抛异常。
        # 错误码 131009 = "Parameter value is not valid"（通常是 wamid
        # 超过 30 天）—— 在长时间沉寂的会话中常见，以 info 而非 warning
        # 记录。
        if resp.status_code != 200:
            try:
                body = resp.json()
                code = ((body or {}).get("error") or {}).get("code")
            except Exception:
                code = None
            if code == 131009:
                logger.info(
                    "[whatsapp_cloud] typing/read indicator rejected: "
                    "wamid %s likely older than 30 days", wamid,
                )
            else:
                logger.debug(
                    "[whatsapp_cloud] typing/read indicator returned %d (%s)",
                    resp.status_code, code,
                )

    # ------------------------------------------------------------------ interactive messages
    #
    # WhatsApp Cloud 支持两种我们在此使用的 interactive 原语：
    #   * ``interactive.type=button`` —— 最多 3 个 quick-reply 按钮。
    #     每个按钮有一个 ``id``（≤256 字符，点击时原样返回）和一个
    #     ``title``（≤20 字符，显示的标签）。用于 ≤3 选项的 clarify、
    #     exec_approval 和 slash_confirm。
    #   * ``interactive.type=list``   —— 单个 "Tap to choose" 按钮，
    #     打开一个最多 10 行的浮层。用于 >3 选项的 clarify 和模型选择器。
    #
    # 与工具模板不同，这些是自由形态的，不需要 Meta 侧审批。它们仅在
    # 24 小时会话窗口*内*有效 —— 这没问题，因为下面五个发送方都是
    # 在直接响应用户消息时触发（会话中途的 clarify、工具调用中途的
    # approval 等），因此被调用时我们始终处于窗口内。

    async def _post_interactive(
        self,
        chat_id: str,
        interactive_body: Dict[str, Any],
        reply_to: Optional[str] = None,
    ) -> SendResult:
        """``interactive`` 消息负载的低层 POST。

        ``interactive_body`` 是内部的 ``interactive: {...}`` 字典 ——
        由调用方提供 ``type``、``body`` 和 ``action``。此封装处理鉴权、
        错误映射和 message_id 提取，使每个 send_* 方法专注于自身的
        按钮形状。
        """
        if self._http_client is None:
            return SendResult(success=False, error="Not connected")

        url = self._graph_url("messages")
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": chat_id,
            "type": "interactive",
            "interactive": interactive_body,
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}

        try:
            resp = await self._http_client.post(url, headers=headers, json=payload)
        except Exception as exc:
            logger.exception("[whatsapp_cloud] interactive send failed")
            return SendResult(success=False, error=str(exc))

        if resp.status_code != 200:
            try:
                body = resp.json()
            except Exception:
                body = {"raw": resp.text[:500]}
            error_msg = self._format_graph_error(body, resp.status_code)
            logger.warning(
                "[whatsapp_cloud] interactive rejected (status=%d): %s",
                resp.status_code, error_msg,
            )
            return SendResult(success=False, error=error_msg)

        last_message_id: Optional[str] = None
        try:
            data = resp.json()
            ids = data.get("messages") or []
            if ids:
                last_message_id = ids[0].get("id")
        except Exception:
            pass
        return SendResult(success=True, message_id=last_message_id)

    @staticmethod
    def _truncate_button_label(text: str, limit: int = 20) -> str:
        """WhatsApp 将 quick-reply 按钮标题限制为 20 字符，list-row
        标题限制为 24 字符。用省略号截断，使我们能尽可能多地展示选项。"""
        text = str(text or "").strip()
        if len(text) <= limit:
            return text
        # 为省略号预留 1 字符。WhatsApp 将省略号计入长度上限。
        return text[: max(1, limit - 1)] + "…"

    @staticmethod
    def _truncate_body(text: str, limit: int = 1024) -> str:
        """``interactive.body.text`` 上限为 1024 字符。"""
        text = str(text or "")
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[list],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """将 clarify 提示渲染为原生 WhatsApp interactive 按钮。

        - 1–3 个选项 → ``interactive.type=button``（inline 胶囊按钮）。
        - 4+ 个选项 → ``interactive.type=list``（点按打开的浮层，最多
          10 行）。Telegram 的 "Other (type answer)" 兜底入口作为最后
          一行追加，选择它会将条目切换为文本捕获模式，由 gateway 的
          文本拦截处理。
        - 0 个选项（开放式）→ 纯文本问题；会话中的下一条消息由
          gateway 捕获并解析 clarify。

        按钮 ``id`` 字段携带 ``cl:<clarify_id>:<idx>``（或 ``:other``）；
        入站 webhook 解析按前缀分发。
        """
        if self._http_client is None:
            return SendResult(success=False, error="Not connected")

        question = (question or "").strip()
        reply_to = (metadata or {}).get("reply_to_message_id") if metadata else None

        # 开放式 → 直接发送问题，gateway 捕获下一条消息。
        if not choices:
            return await self.send(chat_id, f"❓ {question}", reply_to=reply_to)

        # 对齐 Telegram：在 body 中渲染完整选项文本，使长选项不会被
        # 截断到 20 字符的按钮标签上限。将选项截断为 MAX_CHOICES（4）
        # —— 工具层已强制此限制，但做防御性处理。
        choices_list = [str(c).strip() for c in choices[:10] if str(c).strip()]
        option_lines = "\n".join(
            f"{i + 1}. {c}" for i, c in enumerate(choices_list)
        )
        body_text = self._truncate_body(f"❓ {question}\n\n{option_lines}")

        if len(choices_list) <= 3:
            buttons = [
                {
                    "type": "reply",
                    "reply": {
                        "id": f"cl:{clarify_id}:{idx}",
                        "title": self._truncate_button_label(str(idx + 1)),
                    },
                }
                for idx in range(len(choices_list))
            ]
            interactive: Dict[str, Any] = {
                "type": "button",
                "body": {"text": body_text},
                "action": {"buttons": buttons},
            }
        else:
            # List 模式：每行必须包含 id + title（≤24 字符）。
            # Description（≤72 字符）渲染在标题下方 —— 我们把截断后的
            # 选项文本放在那里以提升可扫读性。
            rows = []
            for idx, choice_text in enumerate(choices_list):
                rows.append({
                    "id": f"cl:{clarify_id}:{idx}",
                    "title": self._truncate_button_label(f"{idx + 1}", limit=24),
                    "description": self._truncate_button_label(choice_text, limit=72),
                })
            rows.append({
                "id": f"cl:{clarify_id}:other",
                "title": "✏️ Other",
                "description": "Type your own answer",
            })
            interactive = {
                "type": "list",
                "body": {"text": body_text},
                "action": {
                    "button": "Choose",
                    "sections": [{"title": "Options", "rows": rows}],
                },
            }

        result = await self._post_interactive(chat_id, interactive, reply_to=reply_to)
        if result.success:
            self._bounded_put(self._clarify_state, clarify_id, session_key)
        return result

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """将危险命令的审批提示渲染为原生按钮。

        两个 quick-reply 按钮（Approve / Deny）。点击通过
        ``tools.approval.resolve_gateway_approval`` 解析等待中的 agent
        —— 与文本 ``/approve`` 流程相同的机制。agent 线程会一直阻塞，
        直到用户点击或输入响应。
        """
        if self._http_client is None:
            return SendResult(success=False, error="Not connected")

        # WhatsApp body 上限 1024 字符；为命令周围的框架文字预留空间。
        cmd = command or ""
        cmd_preview = cmd if len(cmd) <= 800 else cmd[:800] + "..."
        body_text = self._truncate_body(
            f"⚠️ *Command Approval Required*\n\n"
            f"```\n{cmd_preview}\n```\n\n"
            f"Reason: {description}"
        )

        approval_id = uuid.uuid4().hex[:12]
        reply_to = (metadata or {}).get("reply_to_message_id") if metadata else None

        interactive = {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {"id": f"appr:{approval_id}:approve", "title": "✅ Approve"},
                    },
                    {
                        "type": "reply",
                        "reply": {"id": f"appr:{approval_id}:deny", "title": "❌ Deny"},
                    },
                ],
            },
        }

        result = await self._post_interactive(chat_id, interactive, reply_to=reply_to)
        if result.success:
            self._bounded_put(self._exec_approval_state, approval_id, session_key)
        return result

    async def send_slash_confirm(
        self,
        chat_id: str,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """将斜杠命令确认提示渲染为 3 按钮形式。

        对齐 Telegram 的 send_slash_confirm：Approve Once / Always /
        Cancel。confirm_id 由调用方（斜杠命令处理程序）提供 —— 我们
        只是存储 session_key 映射，供入站解析器查找。
        """
        if self._http_client is None:
            return SendResult(success=False, error="Not connected")

        body_text = self._truncate_body(f"*{title}*\n\n{message}")
        reply_to = (metadata or {}).get("reply_to_message_id") if metadata else None

        interactive = {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {"id": f"sc:once:{confirm_id}", "title": "✅ Approve Once"},
                    },
                    {
                        "type": "reply",
                        "reply": {"id": f"sc:always:{confirm_id}", "title": "🔒 Always"},
                    },
                    {
                        "type": "reply",
                        "reply": {"id": f"sc:cancel:{confirm_id}", "title": "❌ Cancel"},
                    },
                ],
            },
        }

        result = await self._post_interactive(chat_id, interactive, reply_to=reply_to)
        if result.success:
            self._bounded_put(self._slash_confirm_state, confirm_id, session_key)
        return result

    @staticmethod
    def _format_graph_error(body: Dict[str, Any], status_code: int) -> str:
        err = (body or {}).get("error") or {}
        # Graph API 错误形状：
        # {"error": {"message": "...", "type": "...", "code": ..., "fbtrace_id": "..."}}
        message = err.get("message") or body.get("raw") or "unknown error"
        code = err.get("code")
        if code is not None:
            return f"graph error {code} (HTTP {status_code}): {message}"
        return f"HTTP {status_code}: {message}"

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        # Cloud API 没有像 Slack/Discord 那样的直接 "chat info" 端点
        # —— 我们仅回显 wa_id。Profile name（已知时）通过 webhook
        # ``contacts[].profile.name`` 流入，并缓存在 MessageEvent 上，
        # 而非此处。
        return {"name": chat_id, "type": "dm"}

    # ------------------------------------------------------------------ outbound media
    async def _upload_media(
        self,
        file_path: str,
        media_kind: str,
        mime_type: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        """将本地文件上传到 Graph /media 端点。

        成功时返回 ``(media_id, None)``，失败时返回 ``(None, error_string)``。
        两步发送：此处获取 id，然后 ``_send_media`` 引用它。用于我们有
        本地文件但没有公网 URL 的场景。

        ``media_kind`` 是 "image"、"video"、"audio"、"document"、
        "sticker" 之一 —— 选择大小上限 + 默认 mime 兜底。
        """
        if self._http_client is None:
            return None, "Not connected"
        if not os.path.exists(file_path):
            return None, f"File not found: {file_path}"

        size = os.path.getsize(file_path)
        cap = _MEDIA_SIZE_LIMITS.get(media_kind, _MEDIA_SIZE_LIMITS["document"])
        if size > cap:
            return None, (
                f"File {os.path.basename(file_path)} is {size} bytes; "
                f"Cloud API {media_kind} cap is {cap} bytes"
            )

        if not mime_type:
            mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            mime_type = _DEFAULT_MIME.get(media_kind, "application/octet-stream")

        url = self._graph_url("media")
        headers = {"Authorization": f"Bearer {self._access_token}"}
        try:
            with open(file_path, "rb") as fh:
                files = {
                    "file": (os.path.basename(file_path), fh, mime_type),
                    "messaging_product": (None, "whatsapp"),
                    "type": (None, mime_type),
                }
                resp = await self._http_client.post(url, headers=headers, files=files)
        except Exception as exc:
            logger.exception("[whatsapp_cloud] media upload failed")
            return None, str(exc)

        if resp.status_code != 200:
            try:
                body = resp.json()
            except Exception:
                body = {"raw": resp.text[:500]}
            return None, self._format_graph_error(body, resp.status_code)

        try:
            data = resp.json()
            media_id = data.get("id")
        except Exception:
            media_id = None
        if not media_id:
            return None, "Upload response missing 'id'"
        return media_id, None

    async def _send_media(
        self,
        chat_id: str,
        media_kind: str,
        *,
        media_id: Optional[str] = None,
        media_link: Optional[str] = None,
        caption: Optional[str] = None,
        filename: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        """POST 一条媒体消息，引用已上传的 media_id 或公网 ``link``。

        ``media_id`` 和 ``media_link`` 必须恰好设置一个。Caption 和
        filename 在 Meta 接受的位置透传（caption 在 image/video/document；
        filename 仅在 document）。
        """
        if self._http_client is None:
            return SendResult(success=False, error="Not connected")
        if bool(media_id) == bool(media_link):
            return SendResult(
                success=False,
                error="Exactly one of media_id or media_link must be set",
            )

        url = self._graph_url("messages")
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

        media_block: Dict[str, Any] = {}
        if media_id:
            media_block["id"] = media_id
        else:
            media_block["link"] = media_link
        if caption and media_kind in {"image", "video", "document"}:
            media_block["caption"] = caption
        if filename and media_kind == "document":
            media_block["filename"] = filename

        payload: Dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": chat_id,
            "type": media_kind,
            media_kind: media_block,
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}

        try:
            resp = await self._http_client.post(url, headers=headers, json=payload)
        except Exception as exc:
            logger.exception("[whatsapp_cloud] media send failed")
            return SendResult(success=False, error=str(exc))

        if resp.status_code != 200:
            try:
                body = resp.json()
            except Exception:
                body = {"raw": resp.text[:500]}
            error_msg = self._format_graph_error(body, resp.status_code)
            logger.warning(
                "[whatsapp_cloud] media send rejected (status=%d, kind=%s): %s",
                resp.status_code, media_kind, error_msg,
            )
            return SendResult(success=False, error=error_msg)

        try:
            data = resp.json()
            ids = data.get("messages") or []
            wamid = ids[0].get("id") if ids else None
        except Exception:
            wamid = None
        return SendResult(success=True, message_id=wamid)

    async def _send_media_from_path_or_link(
        self,
        chat_id: str,
        source: str,
        media_kind: str,
        *,
        caption: Optional[str] = None,
        filename: Optional[str] = None,
        reply_to: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> SendResult:
        """智能分发：HTTPS URL → ``link`` 发送；本地路径 → 上传 + ``id`` 发送。

        在可能时优先走 ``link`` 路径（少一次 Graph 往返）。Meta 自行从
        URL 拉取。作为 ``send_image`` / ``send_video`` 等的公共后端 ——
        保持公开方法体精简。
        """
        if source.startswith(("http://", "https://")):
            return await self._send_media(
                chat_id,
                media_kind,
                media_link=source,
                caption=caption,
                filename=filename,
                reply_to=reply_to,
            )
        media_id, err = await self._upload_media(source, media_kind, mime_type)
        if err:
            return SendResult(success=False, error=err)
        return await self._send_media(
            chat_id,
            media_kind,
            media_id=media_id,
            caption=caption,
            filename=filename,
            reply_to=reply_to,
        )

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """通过公网 URL 发送图片。优先使用 Meta 的 ``link`` 模式。

        ``**kwargs`` 吸收 base class 传入的平台无关参数（例如
        ``metadata``），Cloud API 用不到它们。与 send_image_file /
        send_video / send_voice / send_document 一致。
        """
        return await self._send_media_from_path_or_link(
            chat_id, image_url, "image", caption=caption, reply_to=reply_to
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """通过两步 upload + id 发送本地图片文件。"""
        return await self._send_media_from_path_or_link(
            chat_id, image_path, "image", caption=caption, reply_to=reply_to
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """发送视频。本地路径 → 上传；HTTPS URL → link 模式。"""
        return await self._send_media_from_path_or_link(
            chat_id, video_path, "video", caption=caption, reply_to=reply_to
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """将音频文件作为 WhatsApp 语音消息发送。

        WhatsApp 将 ``audio/ogg; codecs=opus`` 渲染为绿色语音气泡；
        其他音频类型（MP3、AAC 等）显示为通用音频附件。Hermes TTS 生成
        MP3，因此我们先尝试用 ffmpeg 转换为 opus，ffmpeg 不可用时
        回退为原样发送 MP3。
        """
        source = audio_path
        mime_type: Optional[str] = None

        is_local_mp3 = (
            not audio_path.startswith(("http://", "https://"))
            and audio_path.lower().endswith(".mp3")
            and os.path.exists(audio_path)
        )
        if is_local_mp3:
            opus_path = await self._convert_to_opus(audio_path)
            if opus_path:
                try:
                    result = await self._send_media_from_path_or_link(
                        chat_id, opus_path, "audio",
                        caption=caption, reply_to=reply_to,
                        mime_type="audio/ogg; codecs=opus",
                    )
                finally:
                    # .ogg 是源 MP3 旁边的瞬时转换产物 —— 上传后清理，
                    # 使语音发送不会每条消息泄漏一个文件。
                    try:
                        os.unlink(opus_path)
                    except OSError:
                        pass
                return result
            # 将作为 MP3 附件投递，而非语音气泡。
            # 警告一次性日志记录在 _convert_to_opus 内部。
            mime_type = "audio/mpeg"

        return await self._send_media_from_path_or_link(
            chat_id, source, "audio",
            caption=caption, reply_to=reply_to, mime_type=mime_type,
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
        """发送文档附件，可选附带 filename + caption。"""
        return await self._send_media_from_path_or_link(
            chat_id, file_path, "document",
            caption=caption,
            filename=file_name or os.path.basename(file_path),
            reply_to=reply_to,
        )

    # ------------------------------------------------------------------ opus conversion
    async def _convert_to_opus(self, mp3_path: str) -> Optional[str]:
        """将 MP3 转换为 ``audio/ogg; codecs=opus``，用于语音气泡。

        返回转换后文件的路径；如果 ffmpeg 缺失 / 转换失败则返回 None
        （调用方回退为将原始 MP3 作为音频文件发送）。

        ``-application voip`` 将 opus 编码器调优为语音。
        ``-b:a 32k -vbr on`` 匹配 WhatsApp 原生语音消息的比特率
        （文件小、可懂度好）。
        """
        if not _FFMPEG_PATH:
            self._warn_once_no_ffmpeg()
            return None

        out_path = mp3_path.rsplit(".", 1)[0] + ".ogg"
        try:
            proc = await asyncio.create_subprocess_exec(
                _FFMPEG_PATH, "-y", "-i", mp3_path,
                "-c:a", "libopus", "-b:a", "32k", "-vbr", "on",
                "-application", "voip", out_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0 or not Path(out_path).exists():
                logger.error(
                    "[whatsapp_cloud] ffmpeg opus conversion failed "
                    "(returncode=%s): %s",
                    proc.returncode,
                    (stderr or b"").decode("utf-8", errors="replace")[:500],
                )
                return None
            return out_path
        except Exception:
            logger.exception("[whatsapp_cloud] ffmpeg subprocess raised")
            return None

    def _warn_once_no_ffmpeg(self) -> None:
        if self._warned_no_ffmpeg:
            return
        self._warned_no_ffmpeg = True
        logger.warning(
            "[whatsapp_cloud] ffmpeg not found on PATH — voice messages will "
            "be delivered as MP3 audio attachments instead of native voice "
            "notes (green waveform bubble). Install ffmpeg to enable: "
            "Windows `winget install Gyan.FFmpeg`, macOS `brew install ffmpeg`, "
            "Linux package manager."
        )

    # ------------------------------------------------------------------ inbound media
    async def _download_media_to_cache(
        self,
        media_id: str,
        *,
        ext_hint: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        """两步 Graph 媒体下载：``GET /<id>`` → 临时 URL → 字节流。

        成功时返回 ``(local_path, mime_type)``。``mime_type`` 回退为
        Graph 在元数据响应中报告的值。任何失败都返回 ``(None, None)``
        （并记录日志）。

        第 1 步获得的临时 URL 是签名的，约 5 分钟后过期；我们立即下载，
        且从不持久化该 URL。
        """
        if self._http_client is None:
            return None, None
        # 纵深防御：media_id 来自（已校验签名的）webhook 负载，但它下面
        # 会被插入到 Graph URL 和缓存文件名中 —— 拒绝任何非纯 Meta 风格
        # media id 的内容，使恶意负载无法遍历路径。
        media_id = str(media_id).strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]+", media_id):
            logger.warning(
                "[whatsapp_cloud] refusing malformed media id %r", media_id[:64]
            )
            return None, None
        headers = {"Authorization": f"Bearer {self._access_token}"}

        # 第 1 步 —— 元数据（给我们一个临时签名 URL + mime）
        try:
            meta_resp = await self._http_client.get(
                f"{GRAPH_API_BASE}/{self._api_version}/{media_id}",
                headers=headers,
            )
        except Exception:
            logger.exception(
                "[whatsapp_cloud] media metadata fetch raised (id=%s)", media_id
            )
            return None, None
        if meta_resp.status_code != 200:
            logger.warning(
                "[whatsapp_cloud] media metadata fetch failed (id=%s, status=%d)",
                media_id, meta_resp.status_code,
            )
            return None, None

        try:
            meta = meta_resp.json()
        except Exception:
            return None, None
        temp_url = meta.get("url")
        mime = meta.get("mime_type") or ""
        if not temp_url:
            return None, None

        # 第 2 步 —— 字节流（即便 URL 已签名也需要鉴权；Meta 明确说明
        # —— 仅凭 URL 不够）。
        try:
            blob_resp = await self._http_client.get(temp_url, headers=headers)
        except Exception:
            logger.exception(
                "[whatsapp_cloud] media bytes fetch raised (id=%s)", media_id
            )
            return None, None
        if blob_resp.status_code != 200:
            logger.warning(
                "[whatsapp_cloud] media bytes fetch failed (id=%s, status=%d)",
                media_id, blob_resp.status_code,
            )
            return None, None

        # 确定扩展名。优先使用覆盖映射，使 audio/ogg 产生 .ogg
        # （而非 mimetypes 默认返回的、技术上正确但实际损坏的 .oga）。
        # 回退到 ext_hint，对未知类型使用 ``.bin``。
        ext = ext_hint
        if not ext and mime:
            ext = _ext_for_mime(mime)
        if not ext:
            ext = ".bin"

        _INBOUND_MEDIA_CACHE.mkdir(parents=True, exist_ok=True)
        out_path = _INBOUND_MEDIA_CACHE / f"{media_id}{ext}"
        try:
            out_path.write_bytes(blob_resp.content)
        except OSError:
            logger.exception(
                "[whatsapp_cloud] failed to write cached media (id=%s)", media_id
            )
            return None, None

        return str(out_path), mime or None


    # ------------------------------------------------------------------ inbound
    async def _handle_health(self, request: "web.Request") -> "web.Response":
        return web.json_response(
            {
                "status": "ok",
                "platform": self.platform.value,
                "phone_number_id": self._phone_number_id,
                "webhook_path": self._webhook_path,
                "verify_token_configured": bool(self._verify_token),
                "app_secret_configured": bool(self._app_secret),
                "ffmpeg_present": _FFMPEG_PATH is not None,
                "accepted": self._accepted_count,
                "duplicates": self._duplicate_count,
                "rejected_signature": self._rejected_signature_count,
            }
        )

    async def _handle_verify(self, request: "web.Request") -> "web.Response":
        """Meta 订阅验证握手。

        Meta 发起 GET ``<webhook>?hub.mode=subscribe&hub.verify_token=...
        &hub.challenge=...``。当且仅当 ``hub.mode == "subscribe"`` 且
        ``hub.verify_token`` 匹配共享密钥时，我们必须以纯文本回显 challenge。
        使用常数时间比较。
        """
        if not self._verify_token:
            # 配置错误的服务器 —— 拒绝，而不是静默接受任意 verify_token，
            # 否则会让攻击者得以订阅。
            return web.Response(status=503, text="verify_token not configured")

        mode = request.query.get("hub.mode", "")
        token = request.query.get("hub.verify_token", "")
        challenge = request.query.get("hub.challenge", "")

        if mode != "subscribe":
            return web.Response(status=400, text="bad mode")

        # 常数时间比较，避免通过计时泄露 token 长度 / 内容。
        # ``hmac.compare_digest`` 适用于 str。
        import hmac as _hmac

        if not _hmac.compare_digest(token, self._verify_token):
            return web.Response(status=403, text="verify_token mismatch")
        if not challenge:
            return web.Response(status=400, text="missing challenge")
        return web.Response(text=challenge, content_type="text/plain")

    async def _handle_webhook(self, request: "web.Request") -> "web.Response":
        """入站 webhook POST 处理器。

        生命周期：
          1. 读取原始字节（签名基于原始 body —— 绝不能先做 JSON 解析，
             否则字节会改变）。
          2. 用 ``app_secret`` 校验 ``X-Hub-Signature-256`` HMAC。
          3. 解析 JSON。
          4. 遍历 ``entry[].changes[].value.{messages, statuses, contacts}``。
          5. 每条消息：按 wamid 去重，构建 MessageEvent，通过
             ``handle_message`` 分发（它会运行 mixin 的门控）。
          6. 一旦确认请求有效就始终响应 200 —— Meta 在非 200 时会重试
             长达 7 天，而我们不希望因分发期间的瞬时 bug 让下游 agent
             工作成倍增加。
        """
        try:
            raw = await request.read()
        except Exception:
            return web.Response(status=400)

        # Meta 文档记录的最大负载为 3MB。比 aiohttp 更早拒绝，这样我们
        # 甚至不会对巨型垃圾内容计算 HMAC。
        if len(raw) > 3 * 1024 * 1024:
            return web.Response(status=413)

        # 如果未配置 app_secret 则拒绝一切请求。没有它我们无法验证发送者，
        # 处理器会成为一个数据注入点。与 GET verify 握手在 verify_token
        # 为空时拒绝的防御姿态相同。
        if not self._app_secret:
            logger.error(
                "[whatsapp_cloud] webhook POST refused: app_secret unset. "
                "Set WHATSAPP_CLOUD_APP_SECRET to enable inbound delivery."
            )
            return web.Response(status=503, text="app_secret not configured")

        signature_header = request.headers.get("X-Hub-Signature-256", "")
        if not self._verify_signature(raw, signature_header):
            self._rejected_signature_count += 1
            logger.warning(
                "[whatsapp_cloud] rejected webhook: invalid X-Hub-Signature-256 "
                "(header=%r, body_len=%d)",
                signature_header,
                len(raw),
            )
            return web.Response(status=401)

        # 仅在签名通过后才解析 —— 攻击者构造的坏 JSON 已被过滤掉，
        # 这里只是防止 Meta 发来格式错误的内容。
        import json as _json

        try:
            payload = _json.loads(raw)
        except Exception:
            logger.warning("[whatsapp_cloud] webhook body is not valid JSON")
            return web.Response(status=400)

        if not isinstance(payload, dict):
            return web.Response(status=400)

        await self._dispatch_payload(payload)
        return web.Response(status=200)

    # ------------------------------------------------------------------ signature
    def _verify_signature(self, raw_body: bytes, header: str) -> bool:
        """校验 X-Hub-Signature-256 HMAC。

        Meta 发送 ``sha256=<hex>``；我们用 ``app_secret`` 作为密钥、
        ``raw_body``（UTF-8 字节，而非重新序列化的 JSON）作为消息计算
        相同的 HMAC。常数时间比较。
        """
        if not self._app_secret or not header:
            return False
        if not header.startswith("sha256="):
            return False
        expected_hex = header[len("sha256="):].strip()
        if not expected_hex:
            return False
        computed = hmac.new(
            self._app_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(computed.lower(), expected_hex.lower())

    # ------------------------------------------------------------------ dispatch
    def _dedup_wamid(self, wamid: str) -> bool:
        """当此 wamid 是首次见到时返回 True。

        如果 wamid 已在内存缓存中，则返回 False（并增加重复计数器）。
        缓存在 ``WAMID_DEDUP_CACHE_SIZE`` 处按 FIFO 淘汰。
        """
        if not wamid:
            # 没有 wamid 意味着我们无法去重 —— 放行。Meta 应始终填充
            # ``id``，但做防御性处理。
            return True
        if wamid in self._seen_wamids:
            self._duplicate_count += 1
            return False
        self._seen_wamids[wamid] = True
        # 淘汰最旧条目以保持在上限以下。
        while len(self._seen_wamids) > WAMID_DEDUP_CACHE_SIZE:
            self._seen_wamids.popitem(last=False)
        return True

    async def _dispatch_payload(self, payload: Dict[str, Any]) -> None:
        """遍历已校验的 Meta webhook 负载并分发每条消息。

        负载形状（已截断）：
          {object, entry: [{id, changes: [{value: {messages, contacts,
          statuses, metadata}, field: "messages"}]}]}

        我们将 ``messages`` 事件呈现为 MessageEvents；``statuses`` 事件
        （sent/delivered/read/failed）只记录日志不分发 —— agent 当前不
        消费投递回执，转发它们会产生嘈杂的合成事件。
        """
        if payload.get("object") != "whatsapp_business_account":
            logger.debug(
                "[whatsapp_cloud] ignoring non-WABA payload (object=%r)",
                payload.get("object"),
            )
            return
        for entry in payload.get("entry") or []:
            if not isinstance(entry, dict):
                continue
            for change in entry.get("changes") or []:
                if not isinstance(change, dict):
                    continue
                if change.get("field") != "messages":
                    # 其他字段（account_alerts、template_status_update 等）
                    # 取决于订阅，且不是消息入口。静默跳过。
                    continue
                value = change.get("value") or {}
                contacts = value.get("contacts") or []
                metadata = value.get("metadata") or {}
                # 为即将呈现的消息构建 wa_id → profile-name 索引。
                contacts_by_waid: Dict[str, str] = {}
                for contact in contacts:
                    if not isinstance(contact, dict):
                        continue
                    wa_id = str(contact.get("wa_id") or "").strip()
                    profile = contact.get("profile") or {}
                    name = str(profile.get("name") or "").strip()
                    if wa_id:
                        contacts_by_waid[wa_id] = name

                for raw_message in value.get("messages") or []:
                    if not isinstance(raw_message, dict):
                        continue
                    wamid = str(raw_message.get("id") or "").strip()
                    if not self._dedup_wamid(wamid):
                        logger.debug(
                            "[whatsapp_cloud] duplicate wamid %s, skipping",
                            wamid,
                        )
                        continue
                    try:
                        event = await self._build_message_event_from_cloud(
                            raw_message, contacts_by_waid, metadata
                        )
                    except Exception:
                        # 构建错误也绝不能上抛：上方 wamid 已标记为去重，
                        # 因此这里返回 500 会让 Meta 重试整批，而该批中的
                        # 每条消息（包括此条）都会被作为重复静默丢弃。
                        # 记录日志并继续处理下一条消息。
                        logger.exception(
                            "[whatsapp_cloud] failed to build event for wamid %s",
                            wamid,
                        )
                        continue
                    if event is None:
                        continue
                    self._accepted_count += 1
                    try:
                        await self.handle_message(event)
                    except Exception:
                        # 分发错误绝不能上抛 —— Meta 会重试整批，
                        # 使 bug 成倍放大。
                        logger.exception(
                            "[whatsapp_cloud] handle_message raised for wamid %s",
                            wamid,
                        )

                # 以 debug 级别记录状态更新 —— 有助于诊断 "Meta 是否接受了
                # 我的出站"，又不会淹没 INFO 日志。
                for status in value.get("statuses") or []:
                    if isinstance(status, dict):
                        logger.debug(
                            "[whatsapp_cloud] status %s for %s",
                            status.get("status"),
                            status.get("id"),
                        )

    async def _dispatch_interactive_reply(
        self,
        raw_message: Dict[str, Any],
        contacts_by_waid: Dict[str, str],
    ) -> bool:
        """将入站 interactive 回复路由到匹配的解析器。

        如果该点击被认领则返回 True（调用方应丢弃该 webhook 条目，
        不再分发新的会话 turn）。当 id 没有可识别的前缀、没有活跃的
        状态条目、或解析器自身报告没有等待者时返回 False —— 在这些
        情况下，调用方回退到标准 text-event 分发，将按钮标题作为普通
        用户消息处理。这种优雅兜底覆盖了过期点击和跨进程重启的场景。

        分发表：
          ``cl:<clarify_id>:<idx|other>``  → resolve_gateway_clarify
          ``appr:<approval_id>:approve|deny`` → resolve_gateway_approval
          ``sc:<once|always|cancel>:<confirm_id>`` → slash_confirm.resolve
        """
        inter = raw_message.get("interactive") or {}
        # button_reply（interactive.type=button）和 list_reply
        # （interactive.type=list）将 id+title 放在不同的子对象中。
        inner = inter.get("button_reply") or inter.get("list_reply") or {}
        button_id = str(inner.get("id") or "").strip()
        if not button_id:
            return False

        # Clarify：cl:<clarify_id>:<idx|other>
        if button_id.startswith("cl:"):
            parts = button_id.split(":", 2)
            if len(parts) != 3:
                return False
            _, clarify_id, choice = parts
            session_key = self._clarify_state.pop(clarify_id, None)
            if not session_key:
                logger.info(
                    "[whatsapp_cloud] clarify tap with no matching state "
                    "(clarify_id=%s) — likely stale; falling back to text",
                    clarify_id,
                )
                return False
            try:
                from tools.clarify_gateway import resolve_gateway_clarify
            except ImportError:
                logger.warning(
                    "[whatsapp_cloud] clarify resolver unavailable; "
                    "falling back to text dispatch"
                )
                return False
            if choice == "other":
                # 用户想输入自由格式的答案。将条目切换为文本捕获模式，
                # 使 gateway 的文本拦截（在 _handle_message 中）能拾取他们
                # 的下一条消息并解析 clarify。若不切换，
                # ``get_pending_for_session`` 不会返回该条目 —— 下一条文本
                # 会落入常规 agent 路径，与仍在 clarify 中阻塞的 agent
                # 线程冲突，产生 "Interrupting current task" 循环。
                try:
                    from tools.clarify_gateway import mark_awaiting_text
                    flipped = mark_awaiting_text(clarify_id)
                except Exception:
                    logger.exception(
                        "[whatsapp_cloud] mark_awaiting_text failed for %s",
                        clarify_id,
                    )
                    flipped = False
                if not flipped:
                    # 条目在用户点击和我们的处理之间消失了（超时、/new、
                    # gateway 重启）。丢弃过期状态，并落入文本分发，使
                    # 用户的点击不被完全忽略。
                    logger.info(
                        "[whatsapp_cloud] clarify 'Other' tap but entry "
                        "missing (clarify_id=%s); falling back to text",
                        clarify_id,
                    )
                    return False
                # 因为我们之前弹出了状态，这里放回 —— 保持 clarify_id →
                # session_key 映射活跃，以防未来还有点击落在同一提示上。
                self._clarify_state[clarify_id] = session_key
                try:
                    await self.send(
                        str(raw_message.get("from") or ""),
                        "✏️ Type your answer:",
                    )
                except Exception:
                    logger.exception("[whatsapp_cloud] clarify other-prompt failed")
                return True  # 认领，这样我们也不会把该点击作为文本分发
            try:
                idx = int(choice)
            except ValueError:
                logger.warning(
                    "[whatsapp_cloud] clarify tap had non-int choice: %r",
                    choice,
                )
                # 放回状态，使后续文本仍可解析。
                self._clarify_state[clarify_id] = session_key
                return False
            # 使用标题文本作为解析后的响应，使 agent 看到人类可读的答案，
            # 而非索引。标题是数字标签（"1"、"2"、...），因此我们从原始
            # 提示查找完整选项 —— 但我们没有持久化它。回退为传入索引；
            # agent 在上下文中持有提示，可以自行解释。
            response_text = str(inner.get("title") or str(idx + 1))
            resolved = resolve_gateway_clarify(clarify_id, response_text)
            if not resolved:
                # 解析器找不到等待者（例如 agent 已超时）。落入文本分发。
                logger.info(
                    "[whatsapp_cloud] clarify resolver reported no waiter "
                    "(clarify_id=%s) — falling back to text", clarify_id,
                )
                return False
            return True

        # Exec approval：appr:<approval_id>:approve|deny
        if button_id.startswith("appr:"):
            parts = button_id.split(":", 2)
            if len(parts) != 3:
                return False
            _, approval_id, choice = parts
            session_key = self._exec_approval_state.pop(approval_id, None)
            if not session_key:
                logger.info(
                    "[whatsapp_cloud] approval tap with no matching state "
                    "(approval_id=%s) — likely stale; falling back to text",
                    approval_id,
                )
                return False
            if choice not in ("approve", "deny"):
                self._exec_approval_state[approval_id] = session_key
                return False
            try:
                from tools.approval import resolve_gateway_approval
            except ImportError:
                logger.warning(
                    "[whatsapp_cloud] approval resolver unavailable"
                )
                return False
            count = resolve_gateway_approval(session_key, choice)
            if not count:
                logger.info(
                    "[whatsapp_cloud] approval resolver reported no waiter "
                    "(session_key=%s) — likely already resolved",
                    session_key,
                )
            # 发送确认消息 —— 对齐 Telegram 的 UX。
            try:
                confirm_text = (
                    "✅ Approved." if choice == "approve" else "❌ Denied."
                )
                await self.send(str(raw_message.get("from") or ""), confirm_text)
            except Exception:
                logger.exception("[whatsapp_cloud] approval confirm failed")
            return True

        # Slash confirm：sc:<once|always|cancel>:<confirm_id>
        if button_id.startswith("sc:"):
            parts = button_id.split(":", 2)
            if len(parts) != 3:
                return False
            _, choice, confirm_id = parts
            session_key = self._slash_confirm_state.pop(confirm_id, None)
            if not session_key:
                logger.info(
                    "[whatsapp_cloud] slash_confirm tap with no matching state "
                    "(confirm_id=%s) — likely stale", confirm_id,
                )
                return False
            if choice not in ("once", "always", "cancel"):
                self._slash_confirm_state[confirm_id] = session_key
                return False
            try:
                from tools import slash_confirm as _slash_confirm_mod
            except ImportError:
                logger.warning(
                    "[whatsapp_cloud] slash_confirm resolver unavailable"
                )
                return False
            try:
                result_text = await _slash_confirm_mod.resolve(
                    session_key, confirm_id, choice
                )
            except Exception:
                logger.exception("[whatsapp_cloud] slash_confirm.resolve failed")
                return True  # 仍认领该点击；把它作为文本呈现也无济于事
            if result_text:
                try:
                    await self.send(str(raw_message.get("from") or ""), result_text)
                except Exception:
                    logger.exception("[whatsapp_cloud] slash_confirm reply failed")
            return True

        # 未知前缀 —— 让文本分发将标题作为普通消息处理。可能是来自
        # 我们不认识的某个插件定义适配器的点击；将其作为文本是安全默认。
        return False

    async def _build_message_event_from_cloud(
        self,
        raw_message: Dict[str, Any],
        contacts_by_waid: Dict[str, str],
        metadata: Dict[str, Any],
    ) -> Optional[MessageEvent]:
        """将 Cloud-API 消息对象转换为 Hermes MessageEvent。

        Phase 4 在文本之外扩展，按 ``media_id`` 通过两步 Graph 端点
        下载入站媒体（图片、视频、音频/语音、文档、贴纸）。缓存文件被
        填入 ``media_urls`` / ``media_types``，使 agent 的 vision 和 STT
        层能看到它们。可读文本的文档（.txt、.md、.json、源代码等）会被
        读取并前置到消息正文，上限 100KB —— 与 Baileys 适配器使用的
        启发式相同。

        如果消息被 mixin 的门控过滤掉（广播过滤、白名单、mention 要求），
        则返回 None。
        """
        msg_type_str = str(raw_message.get("type") or "text").lower()

        # Interactive 回复（按钮点击、列表选择）携带我们发送提示时设置
        # 的 ``id``。在落入文本分发之前，先将它们路由到对应的 gateway
        # 解析器 —— 解析器会解除等待中 agent 线程的阻塞，因此我们不希望
        # 同一点击又触发一次新的会话 turn。
        if msg_type_str == "interactive":
            handled = await self._dispatch_interactive_reply(
                raw_message, contacts_by_waid
            )
            if handled:
                return None

        body = ""
        if msg_type_str == "text":
            text = raw_message.get("text") or {}
            body = str(text.get("body") or "")
        elif msg_type_str in {"button", "interactive"}:
            # Quick-reply 按钮。将按钮负载视为文本，使 agent 能推理用户的
            # 选择。
            if msg_type_str == "button":
                body = str((raw_message.get("button") or {}).get("text") or "")
            else:
                inter = raw_message.get("interactive") or {}
                # button_reply / list_reply 都暴露 ``title``
                inner = inter.get("button_reply") or inter.get("list_reply") or {}
                body = str(inner.get("title") or "")
        elif msg_type_str in {"image", "video", "audio", "voice", "document", "sticker"}:
            # caption 位于 image / video / document 上。其他媒体类型在
            # Meta 规范中不携带 caption，但做防御性处理。
            inner = raw_message.get(msg_type_str) or {}
            body = str(inner.get("caption") or "")

        message_type = {
            "text": MessageType.TEXT,
            "image": MessageType.PHOTO,
            "video": MessageType.VIDEO,
            "audio": MessageType.VOICE,
            "voice": MessageType.VOICE,
            "document": MessageType.DOCUMENT,
            "sticker": MessageType.PHOTO,
            "button": MessageType.TEXT,
            "interactive": MessageType.TEXT,
            "location": MessageType.TEXT,
            "contacts": MessageType.TEXT,
        }.get(msg_type_str, MessageType.TEXT)

        sender_id = str(raw_message.get("from") or "").strip()
        sender_name = contacts_by_waid.get(sender_id, "")

        # Cloud API 对 DM 没有独立的 "chat" 实体 —— chat_id 等于发送者
        # 的 wa_id。群组支持推迟到 v2。
        #
        # 防御性守卫：如果 Meta 投递了群组形态的负载（群组支持由 Meta
        # 按能力层级门控；某些 WABA 已启用），则拒绝，而不是静默将其
        # 视为 DM。群组消息在消息对象上携带标识群组 JID 的 ``chat``
        # 字段 —— 它的缺失表示是 DM。
        chat_field = raw_message.get("chat")
        if chat_field:
            logger.warning(
                "[whatsapp_cloud] received group-shaped message (chat=%s, "
                "wamid=%s) — group support is not yet implemented; dropping. "
                "Use the Baileys whatsapp adapter for group chats.",
                chat_field, raw_message.get("id"),
            )
            return None

        chat_id = sender_id

        # 构建 mixin 的 _should_process_message 所期望的数据字典。
        # Cloud API 使用的字段名与 Baileys 不同，因此我们做适配。
        gating_data = {
            "chatId": chat_id,
            "senderId": sender_id,
            "isGroup": False,  # Phase 3 = 仅 DM
            "body": body,
        }
        if not self._should_process_message(gating_data):
            return None

        # 如果是非文本消息类型则下载媒体。入站媒体以
        # ``{type: "image", image: {id, mime_type, sha256, ...}}`` 形式到达。
        media_urls: list[str] = []
        media_types: list[str] = []
        if msg_type_str in {"image", "video", "audio", "voice", "document", "sticker"}:
            inner = raw_message.get(msg_type_str) or {}
            media_id = str(inner.get("id") or "").strip()
            inbound_mime = str(inner.get("mime_type") or "").strip()
            if media_id:
                ext_hint = None
                if inbound_mime:
                    ext_hint = _ext_for_mime(inbound_mime)
                local_path, dl_mime = await self._download_media_to_cache(
                    media_id, ext_hint=ext_hint
                )
                if local_path:
                    media_urls.append(local_path)
                    media_types.append(dl_mime or inbound_mime or "application/octet-stream")
                    logger.info(
                        "[whatsapp_cloud] cached inbound %s media: %s",
                        msg_type_str, local_path,
                    )
                else:
                    logger.warning(
                        "[whatsapp_cloud] failed to download inbound %s (id=%s) — "
                        "agent will see message metadata but not the binary",
                        msg_type_str, media_id,
                    )
                # Document：原始文件名，用于 agent 的 UX。
                if msg_type_str == "document":
                    fname = str(inner.get("filename") or "").strip()
                    if fname and not body:
                        body = f"[Document: {fname}]"

        # 对于可读文本的文档，将文件内容直接注入消息正文，使 agent
        # 无需单独调用 read_file 即可推理它。与 Baileys 适配器使用的
        # 启发式相同。100KB 上限与 Telegram/Discord/Slack 一致。
        MAX_TEXT_INJECT_BYTES = 100 * 1024
        if msg_type_str == "document" and media_urls:
            for doc_path in media_urls:
                ext = Path(doc_path).suffix.lower()
                if ext in {
                    ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml",
                    ".log", ".py", ".js", ".ts", ".html", ".css",
                }:
                    try:
                        file_size = Path(doc_path).stat().st_size
                        if file_size > MAX_TEXT_INJECT_BYTES:
                            logger.info(
                                "[whatsapp_cloud] skipping text injection for %s "
                                "(%d bytes > %d)",
                                doc_path, file_size, MAX_TEXT_INJECT_BYTES,
                            )
                            continue
                        content = Path(doc_path).read_text(
                            encoding="utf-8", errors="replace"
                        )
                        display_name = Path(doc_path).name
                        injection = f"[Content of {display_name}]:\n{content}"
                        body = f"{injection}\n\n{body}" if body else injection
                    except OSError:
                        logger.exception(
                            "[whatsapp_cloud] failed to read document text: %s",
                            doc_path,
                        )

        # 当用户回复了我们某条消息时，context.id 会被设置。
        context = raw_message.get("context") or {}
        reply_to_id = str(context.get("id") or "").strip() or None

        source = self.build_source(
            chat_id=chat_id,
            chat_name=sender_name or chat_id,
            chat_type="dm",
            user_id=sender_id,
            user_name=sender_name or None,
        )

        # Cloud API 时间戳是 unix 秒（字符串）。MessageEvent 不强制类型，
        # 但下游代码会用它进行格式化。
        wamid = str(raw_message.get("id") or "") or None
        if wamid and chat_id:
            # 刷新每聊天的最新 wamid 缓存，使后续 send_typing 调用能将
            # 指示符 + 已读回执附加到此消息。在此处（在
            # _should_process_message 门控之后）完成，使被过滤的消息不会
            # 对不想要的入站流量泄漏 typing。
            self._bounded_put(self._last_inbound_wamid_by_chat, chat_id, wamid)

        return MessageEvent(
            text=body,
            message_type=message_type,
            source=source,
            raw_message=raw_message,
            message_id=wamid,
            reply_to_message_id=reply_to_id,
            media_urls=media_urls,
            media_types=media_types,
        )
