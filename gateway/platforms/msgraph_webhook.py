"""Microsoft Graph 变更通知 Webhook 入站适配器。"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
from collections import deque
from hashlib import sha1
from typing import Any, Awaitable, Callable, Dict, Optional

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    is_network_accessible,
)

logger = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8646
DEFAULT_WEBHOOK_PATH = "/msgraph/webhook"
DEFAULT_MAX_SEEN_RECEIPTS = 5000
NotificationScheduler = Callable[[Dict[str, Any], MessageEvent], Awaitable[None] | None]


def check_msgraph_webhook_requirements() -> bool:
    """检查 Webhook 所需依赖是否可用。"""
    return AIOHTTP_AVAILABLE


class MSGraphWebhookAdapter(BasePlatformAdapter):
    """接收 Microsoft Graph 变更通知并在内部处理。"""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.MSGRAPH_WEBHOOK)
        extra = config.extra or {}
        self._host: str = str(extra.get("host", DEFAULT_HOST))
        self._port: int = int(extra.get("port", DEFAULT_PORT))
        self._webhook_path: str = self._normalize_path(
            extra.get("webhook_path", DEFAULT_WEBHOOK_PATH)
        )
        self._health_path: str = self._normalize_path(extra.get("health_path", "/health"))
        self._accepted_resources: list[str] = [
            str(value).strip()
            for value in (extra.get("accepted_resources") or [])
            if str(value).strip()
        ]
        self._client_state: Optional[str] = self._string_or_none(extra.get("client_state"))
        self._max_seen_receipts = max(
            1, int(extra.get("max_seen_receipts", DEFAULT_MAX_SEEN_RECEIPTS))
        )
        self._allowed_source_networks: list[ipaddress._BaseNetwork] = (
            self._parse_allowed_source_cidrs(extra.get("allowed_source_cidrs"))
        )
        self._runner = None
        self._notification_scheduler: Optional[NotificationScheduler] = None
        self._seen_receipts: set[str] = set()
        self._seen_receipt_order: deque[str] = deque()
        self._accepted_count = 0
        self._duplicate_count = 0

    @staticmethod
    def _string_or_none(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _normalize_path(path: Any) -> str:
        raw = str(path or "").strip() or "/"
        return raw if raw.startswith("/") else f"/{raw}"

    @staticmethod
    def _build_receipt_key(notification: Dict[str, Any]) -> Optional[str]:
        explicit_id = str(notification.get("id") or "").strip()
        if explicit_id:
            return f"id:{explicit_id}"
        return None

    @staticmethod
    def _normalize_resource_value(resource: str) -> str:
        return str(resource or "").strip().strip("/")

    @staticmethod
    def _parse_allowed_source_cidrs(
        raw: Any,
    ) -> list[ipaddress._BaseNetwork]:
        """解析可选的 CIDR 范围列表，允许其中的 IP 向 Webhook POST 数据。

        空值或缺失值表示"允许所有"（与该字段存在之前的行为相同）。
        填写后，来源 IP 不在任何列出 CIDR 范围内的请求将在解析请求体
        之前以 403 拒绝。在生产部署中使用此字段将端点限制为
        Microsoft Graph 公布的 Webhook 源 IP 范围。
        """
        if raw is None:
            return []
        if isinstance(raw, str):
            candidates = [chunk.strip() for chunk in raw.split(",")]
        elif isinstance(raw, (list, tuple, set)):
            candidates = [str(chunk).strip() for chunk in raw]
        else:
            return []

        networks: list[ipaddress._BaseNetwork] = []
        for chunk in candidates:
            if not chunk:
                continue
            try:
                networks.append(ipaddress.ip_network(chunk, strict=False))
            except ValueError:
                logger.warning(
                    "[msgraph_webhook] Ignoring invalid allowed_source_cidrs entry: %r",
                    chunk,
                )
        return networks

    def set_notification_scheduler(self, scheduler: Optional[NotificationScheduler]) -> None:
        self._notification_scheduler = scheduler

    def _source_allowlist_required_but_missing(self) -> bool:
        return is_network_accessible(self._host) and not self._allowed_source_networks

    async def connect(self) -> bool:
        if self._client_state is None:
            logger.error(
                "[msgraph_webhook] Refusing to start without extra.client_state configured"
            )
            return False
        if self._source_allowlist_required_but_missing():
            logger.error(
                "[msgraph_webhook] Refusing to start: binding to %s requires "
                "extra.allowed_source_cidrs. Configure the Microsoft Graph "
                "source CIDRs or bind to loopback (127.0.0.1/::1) behind a "
                "tunnel or reverse proxy.",
                self._host,
            )
            return False

        app = web.Application()
        app.router.add_get(self._health_path, self._handle_health)
        app.router.add_get(self._webhook_path, self._handle_validation)
        app.router.add_post(self._webhook_path, self._handle_notification)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        self._mark_connected()
        logger.info(
            "[msgraph_webhook] Listening on %s:%d%s",
            self._host,
            self._port,
            self._webhook_path,
        )
        return True

    async def disconnect(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._mark_disconnected()

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        logger.info("[msgraph_webhook] Response for %s: %s", chat_id, content[:200])
        return SendResult(success=True)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "webhook"}

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        if not self._source_ip_allowed(request):
            return web.Response(status=403)
        return web.json_response(
            {
                "status": "ok",
                "platform": self.platform.value,
                "webhook_path": self._webhook_path,
                "accepted": self._accepted_count,
                "duplicates": self._duplicate_count,
            }
        )

    async def _handle_validation(self, request: "web.Request") -> "web.Response":
        """处理 Microsoft Graph 订阅验证握手。

        Graph 通过向端点发送携带 ``validationToken`` 查询参数的 GET 请求
        来验证订阅端点；服务必须在 10 秒内以 ``text/plain`` 形式逐字
        回显该 token。其他情况（裸 GET、缺少 token 的 GET）一律拒绝，
        防止端点被枚举或被误用于数据外泄。
        """
        if not self._source_ip_allowed(request):
            return web.Response(status=403)
        validation_token = request.query.get("validationToken", "")
        if not validation_token:
            return web.Response(status=400)
        return web.Response(text=validation_token, content_type="text/plain")

    async def _handle_notification(self, request: "web.Request") -> "web.Response":
        if not self._source_ip_allowed(request):
            return web.Response(status=403)

        # Graph 不会在 POST 上携带 validationToken，但为兼容在带内
        # 重播握手的防御性客户端，此处也予以处理。
        validation_token = request.query.get("validationToken", "")
        if validation_token:
            return web.Response(text=validation_token, content_type="text/plain")

        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400)

        notifications = body.get("value")
        if not isinstance(notifications, list):
            return web.Response(status=400)

        accepted = 0
        duplicates = 0
        auth_rejected = 0
        other_rejected = 0

        for raw_notification in notifications:
            if not isinstance(raw_notification, dict):
                other_rejected += 1
                continue
            notification = dict(raw_notification)
            if not self._resource_accepted(str(notification.get("resource") or "")):
                other_rejected += 1
                continue
            if not self._verify_client_state(notification):
                # 将错误的 clientState 视为认证失败：若整批消息都是伪造的，
                # 返回 403 可阻止发送方继续重试。合法的 Graph 重试携带
                # 有效的 clientState，会命中 accepted/duplicate 路径。
                auth_rejected += 1
                continue

            receipt_key = self._build_receipt_key(notification)
            if receipt_key is not None:
                if self._has_seen_receipt(receipt_key):
                    duplicates += 1
                    continue
                self._remember_receipt(receipt_key)

            accepted += 1
            self._accepted_count += 1
            event = self._build_message_event(notification, receipt_key)
            self._schedule_notification(notification, event)

        self._duplicate_count += duplicates
        # 如果有成功接收或去重的消息，返回 202 空体，使 Graph 确认成功
        # 且不泄露内部计数器。如果所有条目均认证失败，返回 403 让
        # 发送伪造通知的攻击者收到明确拒绝。其他失败（格式错误、
        # 资源不匹配）属于发送方配置问题，返回 400。
        if accepted or duplicates:
            return web.Response(status=202)
        if auth_rejected and not other_rejected:
            return web.Response(status=403)
        return web.Response(status=400)

    def _source_ip_allowed(self, request: "web.Request") -> bool:
        """如果请求源 IP 在配置的允许列表中则返回 True。

        仅绑定到回环地址的部署可省略 ``allowed_source_cidrs``，
        适用于本地反向代理和开发隧道。绑定到网络可访问地址时，
        在配置明确的 CIDR 允许列表之前默认拒绝。
        """
        if self._source_allowlist_required_but_missing():
            return False
        if not self._allowed_source_networks:
            return True
        peer = request.remote or ""
        if not peer:
            return False
        try:
            peer_addr = ipaddress.ip_address(peer)
        except ValueError:
            return False
        return any(peer_addr in network for network in self._allowed_source_networks)

    def _resource_accepted(self, resource: str) -> bool:
        if not self._accepted_resources:
            return True
        normalized_resource = self._normalize_resource_value(resource)
        for pattern in self._accepted_resources:
            normalized_pattern = self._normalize_resource_value(pattern)
            if not normalized_pattern:
                continue
            if normalized_pattern.endswith("*"):
                prefix = normalized_pattern[:-1].rstrip("/")
                if normalized_resource == prefix or normalized_resource.startswith(f"{prefix}/"):
                    return True
                continue
            if (
                normalized_resource == normalized_pattern
                or normalized_resource.startswith(f"{normalized_pattern}/")
            ):
                return True
        return False

    def _verify_client_state(self, notification: Dict[str, Any]) -> bool:
        """验证 Graph 提供的 clientState 是否与配置的密钥匹配。

        使用 ``hmac.compare_digest`` 而非 ``==``，防止不匹配时
        通过字符串比较时序泄露已匹配的前缀字符数。配置的
        client_state 是共享密钥（安装指南中记录为
        "通过 ``openssl rand -hex 32`` 生成"），因此使用时序安全
        比较是正确的原语。
        """
        expected = self._client_state
        if expected is None:
            return False
        provided = self._string_or_none(notification.get("clientState"))
        if provided is None:
            return False
        return hmac.compare_digest(provided, expected)

    def _has_seen_receipt(self, receipt_key: str) -> bool:
        return receipt_key in self._seen_receipts

    def _remember_receipt(self, receipt_key: str) -> None:
        self._seen_receipts.add(receipt_key)
        self._seen_receipt_order.append(receipt_key)
        while len(self._seen_receipt_order) > self._max_seen_receipts:
            oldest = self._seen_receipt_order.popleft()
            self._seen_receipts.discard(oldest)

    def _build_message_event(
        self,
        notification: Dict[str, Any],
        receipt_key: Optional[str],
    ) -> MessageEvent:
        message_id = receipt_key or f"sha1:{sha1(json.dumps(notification, sort_keys=True).encode('utf-8')).hexdigest()}"
        source = self.build_source(
            chat_id=f"msgraph:{notification.get('subscriptionId', 'unknown')}",
            chat_name="msgraph/webhook",
            chat_type="webhook",
            user_id="msgraph",
            user_name="Microsoft Graph",
        )
        return MessageEvent(
            text=self._render_prompt(notification),
            message_type=MessageType.TEXT,
            source=source,
            raw_message=notification,
            message_id=message_id,
            internal=True,
        )

    def _render_prompt(self, notification: Dict[str, Any]) -> str:
        template = self.config.extra.get("prompt", "")
        if template:
            payload = {
                "notification": notification,
                "resource": notification.get("resource", ""),
                "change_type": notification.get("changeType", ""),
                "subscription_id": notification.get("subscriptionId", ""),
            }
            return self._render_template(template, payload)
        rendered = json.dumps(notification, indent=2, sort_keys=True)[:4000]
        return f"Microsoft Graph change notification:\n\n```json\n{rendered}\n```"

    def _render_template(self, template: str, payload: Dict[str, Any]) -> str:
        import re

        def _resolve(match: "re.Match[str]") -> str:
            key = match.group(1)
            value: Any = payload
            for part in key.split("."):
                if isinstance(value, dict):
                    value = value.get(part, f"{{{key}}}")
                else:
                    return f"{{{key}}}"
            if isinstance(value, (dict, list)):
                return json.dumps(value, sort_keys=True)[:2000]
            return str(value)

        return re.sub(r"\{([a-zA-Z0-9_.]+)\}", _resolve, template)

    def _schedule_notification(
        self,
        notification: Dict[str, Any],
        event: MessageEvent,
    ) -> None:
        scheduler = self._notification_scheduler
        if scheduler is not None:
            result = scheduler(notification, event)
            if asyncio.iscoroutine(result):
                task = asyncio.create_task(result)
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            return

        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
