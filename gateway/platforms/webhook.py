"""通用 webhook 平台适配器。

运行一个 aiohttp HTTP 服务器，接收来自外部服务（GitHub、GitLab、JIRA、
Stripe 等）的 webhook POST 请求，校验 HMAC 签名，将 payload 转换为 agent
prompt，并把响应路由回原始来源或另一个已配置的平台。

配置位于 config.yaml 的 platforms.webhook.extra.routes 下。每条路由定义：
  - events：要接受的事件类型（基于请求头的过滤）
  - secret：用于签名校验的 HMAC 密钥（必填）
  - prompt：用 webhook payload 填充的模板字符串
  - skills：可选，要为 agent 加载的技能列表
  - deliver：响应的发送目标（github_comment、telegram 等）
  - deliver_extra：附加投递配置（repo、pr_number、chat_id）
  - deliver_only：为 true 时跳过 agent —— 渲染后的 prompt 就是要投递的消息。
    适用于外部推送通知（Supabase、监控告警、agent 间 ping），在这些场景下
    零 LLM 成本和亚秒级投递比 agent 推理更重要。

安全：
  - 每条路由必须配置 HMAC 密钥（在启动时校验）
  - 每条路由的速率限制（固定窗口，可配置）
  - 幂等缓存防止 webhook 重试时重复运行 agent
  - 在读取 payload 之前检查请求体大小限制
  - 将 secret 设置为 "INSECURE_NO_AUTH" 可跳过校验（仅用于测试）
"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import subprocess
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

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
)

logger = logging.getLogger(__name__)

# 当 /p/<profile>/ 前缀命名的 profile 本 gateway 不提供服务时，
# _resolve_request_profile 返回的哨兵值（→ 404）。与 None 不同
# （None 表示没有前缀 / 多路复用关闭 → 作为默认 profile 处理）。
_PROFILE_REJECTED = object()

_BUILTIN_DELIVER_PLATFORMS = {
    "telegram", "discord", "slack", "signal", "sms", "whatsapp",
    "matrix", "mattermost", "homeassistant", "email", "dingtalk",
    "feishu", "wecom", "wecom_callback", "weixin", "bluebubbles",
    "qqbot", "yuanbao",
}

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8644
_INSECURE_NO_AUTH = "INSECURE_NO_AUTH"
_DYNAMIC_ROUTES_FILENAME = "webhook_subscriptions.json"
_RATE_WINDOW_SECONDS = 60.0

# 仅服务于本机发起连接的主机名/IP 字面量。其他地址出于安全护栏目的
# 一律视为公网绑定。
_LOOPBACK_HOSTS = frozenset({
    "127.0.0.1",
    "localhost",
    "::1",
    "ip6-localhost",
    "ip6-loopback",
})


def _is_loopback_host(host: str) -> bool:
    """当 `host` 仅绑定到本机时返回 True。

    覆盖 IPv4 回环、标准 `localhost` 别名、带括号和不带括号形式的 IPv6 回环，
    以及常见的 Debian 风格别名。任何假值（空字符串、None）都被保守地视为
    非回环，因为未设置 host 通常意味着平台默认的公网绑定。
    """
    if not host:
        return False
    return host.strip().lower() in _LOOPBACK_HOSTS


def check_webhook_requirements() -> bool:
    """检查 webhook 适配器的依赖是否可用。"""
    return AIOHTTP_AVAILABLE


class WebhookAdapter(BasePlatformAdapter):
    """通用 webhook 接收器，通过 HTTP POST 触发 agent 运行。"""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WEBHOOK)
        self._host: str = config.extra.get("host", DEFAULT_HOST)
        self._port: int = int(config.extra.get("port", DEFAULT_PORT))
        self._global_secret: str = config.extra.get("secret", "")
        self._static_routes: Dict[str, dict] = config.extra.get("routes", {})
        self._dynamic_routes: Dict[str, dict] = {}
        self._dynamic_routes_mtime: float = 0.0
        self._routes: Dict[str, dict] = dict(self._static_routes)
        self._runner = None

        # 以会话 chat_id 为键的投递信息。
        #
        # 该 chat_id 的每次 send() 调用（状态消息和最终响应）都会读取它。
        # 通过每次 POST 的 TTL 清理保证字典有界 —— 参见 _prune_delivery_info()。
        # 切勿在 send() 中 pop，否则中间状态消息（例如 fallback 通知、
        # 上下文压力告警）会在最终响应到达前消耗掉该条目，导致响应静默地
        # 回退到 "log" 投递类型。
        self._delivery_info: Dict[str, dict] = {}
        self._delivery_info_created: Dict[str, float] = {}
        self._delivery_info_order: Deque[tuple[float, str]] = deque()

        # 跨平台投递所用的 gateway runner 引用（由外部设置）
        self.gateway_runner = None

        # 幂等：最近处理过的 delivery ID 的 TTL 缓存。
        # 防止 webhook 提供方重试时重复运行 agent。
        self._seen_deliveries: Dict[str, float] = {}
        self._idempotency_ttl: int = 3600  # 1 小时
        self._seen_deliveries_next_prune_at: float = 0.0

        # 速率限制：固定窗口内每条路由的时间戳。
        self._rate_counts: Dict[str, Deque[float]] = {}
        self._rate_limit: int = int(config.extra.get("rate_limit", 30))  # 每分钟

        # 请求体大小限制（先鉴权后读 body 的模式）
        self._max_body_bytes: int = int(
            config.extra.get("max_body_bytes", 1_048_576)
        )  # 1MB

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        # 在校验之前加载 agent 创建的订阅
        self._reload_dynamic_routes()

        # 启动时校验路由 —— 每条路由都必须配置 secret
        for name, route in self._routes.items():
            secret = route.get("secret", self._global_secret)
            if not secret:
                raise ValueError(
                    f"[webhook] Route '{name}' has no HMAC secret. "
                    f"Set 'secret' on the route or globally. "
                    f"For testing without auth, set secret to '{_INSECURE_NO_AUTH}'."
                )

            # 安全护栏：当 INSECURE_NO_AUTH 与非回环绑定同时出现时拒绝启动。
            # 这个逃生口仅用于本地测试；在公网接口上提供无鉴权的路由属于
            # 部署级别的危险操作，我们宁愿提前崩溃也不愿冒险上线。
            if secret == _INSECURE_NO_AUTH and not _is_loopback_host(self._host):
                raise ValueError(
                    f"[webhook] Route '{name}' uses INSECURE_NO_AUTH secret "
                    f"but is bound to non-loopback host '{self._host}'. "
                    f"INSECURE_NO_AUTH is for local testing only. "
                    f"Refusing to start to prevent accidental exposure."
                )
            # deliver_only 路由会绕过 agent —— POST body 会通过配置的投递
            # 目标变成直接的推送通知。预先校验以便错误配置在启动时就暴露，
            # 而不是等到第一次 webhook POST 时才发现。
            if route.get("deliver_only"):
                deliver = route.get("deliver", "log")
                if not deliver or deliver == "log":
                    raise ValueError(
                        f"[webhook] Route '{name}' has deliver_only=true but "
                        f"deliver is '{deliver}'. Direct delivery requires a "
                        f"real target (telegram, discord, slack, github_comment, etc.)."
                    )

        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/webhooks/{route_name}", self._handle_webhook)
        # 多 profile 多路复用：/p/<profile>/webhooks/<route> 前缀会将入站
        # 事件路由到对应 profile。使用同一个 handler；profile 从路径中捕获
        # 并盖戳到 SessionSource 上，以便 agent 这一轮解析该 profile 的
        # config/skills/credentials。仅当 gateway.multiplex_profiles 开启时生效
        # （由 handler 校验）。
        app.router.add_post(
            "/p/{profile}/webhooks/{route_name}", self._handle_webhook
        )

        # 端口冲突检测 —— 如果端口已被占用则快速失败
        import socket as _socket
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _s:
                _s.settimeout(1)
                _s.connect(('127.0.0.1', self._port))
            logger.error('[webhook] Port %d already in use. Set a different port in config.yaml: platforms.webhook.port', self._port)
            return False
        except (ConnectionRefusedError, OSError):
            pass  # 端口空闲

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        self._mark_connected()

        route_names = ", ".join(self._routes.keys()) or "(none configured)"
        logger.info(
            "[webhook] Listening on %s:%d — routes: %s",
            self._host,
            self._port,
            route_names,
        )
        return True

    async def disconnect(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._mark_disconnected()
        logger.info("[webhook] Disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """将 agent 的响应投递到配置的目标。

        chat_id 为 ``webhook:{route}:{delivery_id}``。在 webhook 接收期间
        存储的投递信息使用 ``.get()`` 读取（而非 pop），这样在最终响应之前
        发出的中间状态消息（fallback 模型通知、上下文压力告警等）就不会
        消耗掉该条目并静默地把最终响应降级为 ``log`` 投递类型。TTL 清理在
        POST 时进行。
        """
        delivery = self._delivery_info.get(chat_id, {})
        deliver_type = delivery.get("deliver", "log")

        if deliver_type == "log":
            logger.info("[webhook] Response for %s: %s", chat_id, content[:200])
            return SendResult(success=True)

        if deliver_type == "github_comment":
            return await self._deliver_github_comment(content, delivery)

        # 跨平台投递 —— 任何带有 gateway 适配器的平台。
        # 同时检查内置名称和插件注册的平台。
        _is_known_platform = deliver_type in _BUILTIN_DELIVER_PLATFORMS
        if not _is_known_platform:
            try:
                from gateway.platform_registry import platform_registry
                _is_known_platform = platform_registry.is_registered(deliver_type)
            except Exception:
                pass
        if self.gateway_runner and _is_known_platform:
            return await self._deliver_cross_platform(
                deliver_type, content, delivery
            )

        logger.warning("[webhook] Unknown deliver type: %s", deliver_type)
        return SendResult(
            success=False, error=f"Unknown deliver type: {deliver_type}"
        )

    def _prune_delivery_info(self, now: float) -> None:
        """丢弃超过幂等 TTL 的 delivery_info 条目。

        与 ``_seen_deliveries`` 的清理模式一致。每次 POST 时调用，
        这样即便大量 webhook 触发且从未收到最终响应，字典大小也以
        ``rate_limit * TTL`` 为上界。
        """
        if len(self._delivery_info_order) < len(self._delivery_info_created):
            self._delivery_info_order = deque(
                (created_at, key)
                for key, created_at in sorted(
                    self._delivery_info_created.items(), key=lambda item: item[1]
                )
            )
        cutoff = now - self._idempotency_ttl
        while self._delivery_info_order and self._delivery_info_order[0][0] < cutoff:
            created_at, key = self._delivery_info_order.popleft()
            if self._delivery_info_created.get(key) != created_at:
                continue
            self._delivery_info.pop(key, None)
            self._delivery_info_created.pop(key, None)

    def _prune_seen_deliveries(self, now: float) -> None:
        """偶尔清理过期的 delivery ID，避免每次 POST 都扫描。"""
        if now < self._seen_deliveries_next_prune_at:
            return
        cutoff = now - self._idempotency_ttl
        stale = [k for k, t in self._seen_deliveries.items() if t < cutoff]
        for k in stale:
            self._seen_deliveries.pop(k, None)
        self._seen_deliveries_next_prune_at = now + min(60.0, max(1.0, self._idempotency_ttl / 10))

    def _record_rate_limit_hit(self, route_name: str, now: float) -> bool:
        """记录本次命中后，若路由仍在限额之内则返回 True。"""
        window = self._rate_counts.get(route_name)
        if not isinstance(window, deque):
            new_window: Deque[float] = deque(window or ())
            self._rate_counts[route_name] = new_window
            window = new_window
        cutoff = now - _RATE_WINDOW_SECONDS
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= self._rate_limit:
            return False
        window.append(now)
        return True

    def _record_delivery_id(self, delivery_id: str, now: float) -> bool:
        """当该 delivery 应被处理时返回 True。"""
        seen_at = self._seen_deliveries.get(delivery_id)
        if seen_at is not None and now - seen_at < self._idempotency_ttl:
            return False
        if seen_at is not None:
            self._seen_deliveries.pop(delivery_id, None)
        self._seen_deliveries[delivery_id] = now
        if len(self._seen_deliveries) > max(self._rate_limit * 2, 128):
            self._prune_seen_deliveries(now)
        return True

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "webhook"}

    # ------------------------------------------------------------------
    # HTTP handler
    # ------------------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """GET /health —— 简单的健康检查。"""
        return web.json_response({"status": "ok", "platform": "webhook"})

    def _reload_dynamic_routes(self) -> None:
        """如果文件发生变更，则从磁盘重新加载 agent 创建的订阅。"""
        from hermes_constants import get_hermes_home
        hermes_home = get_hermes_home()
        subs_path = hermes_home / _DYNAMIC_ROUTES_FILENAME
        if not subs_path.exists():
            if self._dynamic_routes:
                self._dynamic_routes = {}
                self._routes = dict(self._static_routes)
                logger.debug("[webhook] Dynamic subscriptions file removed, cleared dynamic routes")
            return
        try:
            mtime = subs_path.stat().st_mtime
            if mtime <= self._dynamic_routes_mtime:
                return  # 无变化
            data = json.loads(subs_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            # 合并：静态路由优先于动态路由。
            # 拒绝任何有效 secret 为空的动态路由 —— 空 secret 会导致
            # _handle_webhook 完全跳过 HMAC 校验，从而让未鉴权的调用者进入。
            new_dynamic: Dict[str, dict] = {}
            for k, v in data.items():
                if k in self._static_routes:
                    continue
                effective_secret = v.get("secret", self._global_secret)
                if not effective_secret:
                    logger.warning(
                        "[webhook] Dynamic route '%s' skipped: 'secret' is "
                        "missing or empty. Set a valid HMAC secret, or use "
                        "'%s' to explicitly disable auth (testing only).",
                        k,
                        _INSECURE_NO_AUTH,
                    )
                    continue
                if (
                    effective_secret == _INSECURE_NO_AUTH
                    and not _is_loopback_host(self._host)
                ):
                    logger.warning(
                        "[webhook] Dynamic route '%s' skipped: INSECURE_NO_AUTH "
                        "is only allowed on loopback hosts. Current host: '%s'.",
                        k,
                        self._host,
                    )
                    continue
                new_dynamic[k] = v
            self._dynamic_routes = new_dynamic
            self._routes = {**self._dynamic_routes, **self._static_routes}
            self._dynamic_routes_mtime = mtime
            logger.info(
                "[webhook] Reloaded %d dynamic route(s): %s",
                len(self._dynamic_routes),
                ", ".join(self._dynamic_routes.keys()) or "(none)",
            )
        except Exception as e:
            logger.error("[webhook] Failed to reload dynamic routes: %s", e)

    def _resolve_request_profile(self, request: "web.Request"):
        """解析并校验 webhook 请求上的 ``/p/<profile>/`` URL 前缀。

        返回值：
          - 当不存在 profile 前缀，或多路复用关闭时返回 ``None``
            （前缀被忽略，请求按默认 profile 处理）。
          - 当存在前缀、多路复用开启，且该 profile 属于本 gateway 提供
            的 profile 时，返回 profile 名称（str）。
          - 当存在前缀但 profile 未知/未配置时返回 ``_PROFILE_REJECTED``
            （handler 返回 404）。
        """
        profile = (request.match_info.get("profile") or "").strip()
        if not profile:
            return None
        runner = self.gateway_runner
        cfg = getattr(runner, "config", None)
        if not getattr(cfg, "multiplex_profiles", False):
            # 提供了前缀但多路复用关闭 —— 忽略它，按单 profile
            # gateway 处理（不要对原本有效的路由返回 404）。
            return None
        try:
            from hermes_cli.profiles import profiles_to_serve
            served = {name for name, _ in profiles_to_serve(multiplex=True)}
        except Exception:
            return _PROFILE_REJECTED
        if profile not in served:
            return _PROFILE_REJECTED
        return profile

    async def _handle_webhook(self, request: "web.Request") -> "web.Response":
        """POST /webhooks/{route_name} —— 接收并处理一个 webhook 事件。"""
        # 每次请求都热重载动态订阅（以 mtime 为门控，开销很小）
        self._reload_dynamic_routes()

        route_name = request.match_info.get("route_name", "")
        route_config = self._routes.get(route_name)

        # 多 profile：如果存在 /p/<profile>/ 前缀则解析并校验。
        profile = self._resolve_request_profile(request)
        if profile is _PROFILE_REJECTED:
            return web.json_response(
                {"error": "Unknown or unconfigured profile"}, status=404
            )

        if not route_config:
            return web.json_response(
                {"error": f"Unknown route: {route_name}"}, status=404
            )

        # 被禁用的路由仍保留在订阅文件中（以便 dashboard 可以重新启用），
        # 但会拒绝入站事件。默认启用：只有显式的 ``enabled: false`` 才会
        # 关闭路由，与 mcp_servers 的 ``enabled`` 语义保持一致。
        if route_config.get("enabled", True) is False:
            return web.json_response(
                {"error": f"Route disabled: {route_name}"}, status=403
            )

        # ── 先鉴权后读 body ──────────────────────────────────────
        # 在读取完整 payload 之前先检查 Content-Length。
        content_length = request.content_length or 0
        if content_length > self._max_body_bytes:
            return web.json_response(
                {"error": "Payload too large"}, status=413
            )

        # 读取 body（必须在任何校验之前完成）
        try:
            raw_body = await request.read()
        except Exception as e:
            logger.error("[webhook] Failed to read body: %s", e)
            return web.json_response({"error": "Bad request"}, status=400)

        # 优先校验 HMAC 签名（仅对显式的本地测试模式 INSECURE_NO_AUTH
        # 跳过）。缺失/空的 secret 必须在此处失败关闭（fail closed），
        # 而不仅是在 connect() 期间，这样直接复用 handler 就无法把一条
        # 公网 webhook 路由变成未鉴权的 agent 分发入口。
        secret = route_config.get("secret", self._global_secret)
        if not secret:
            logger.error(
                "[webhook] Route %s has no HMAC secret; refusing request",
                route_name,
            )
            return web.json_response(
                {"error": "Webhook route is missing an HMAC secret"},
                status=403,
            )
        if secret != _INSECURE_NO_AUTH:
            if not self._validate_signature(request, raw_body, secret):
                logger.warning(
                    "[webhook] Invalid signature for route %s", route_name
                )
                return web.json_response(
                    {"error": "Invalid signature"}, status=401
                )

        # ── 速率限制（鉴权之后） ────────────────────────────────
        now = time.time()
        if not self._record_rate_limit_hit(route_name, now):
            return web.json_response(
                {"error": "Rate limit exceeded"}, status=429
            )

        # 解析 payload
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            # 退而求其次尝试 form-encoded
            try:
                import urllib.parse

                payload = dict(
                    urllib.parse.parse_qsl(raw_body.decode("utf-8"))
                )
            except Exception:
                return web.json_response(
                    {"error": "Cannot parse body"}, status=400
                )

        # 检查事件类型过滤器
        event_type = (
            request.headers.get("X-GitHub-Event", "")
            or request.headers.get("X-GitLab-Event", "")
            or payload.get("event_type", "")
            or payload.get("type", "")
            or "unknown"
        )
        allowed_events = route_config.get("events", [])
        if allowed_events and event_type not in allowed_events:
            logger.debug(
                "[webhook] Ignoring event %s for route %s (allowed: %s)",
                event_type,
                route_name,
                allowed_events,
            )
            return web.json_response(
                {"status": "ignored", "event": event_type}
            )

        # 用模板渲染 prompt
        prompt_template = route_config.get("prompt", "")
        prompt = self._render_prompt(
            prompt_template, payload, event_type, route_name
        )

        # 如果配置了技能，注入技能内容。
        # 我们直接调用 build_skill_invocation_message()，而不是使用
        # /skill-name 斜杠命令 —— gateway 的命令解析器会拦截这些命令
        # 并破坏正常流程。
        skills = route_config.get("skills", [])
        if skills:
            try:
                from agent.skill_commands import (
                    build_skill_invocation_message,
                    get_skill_commands,
                )

                skill_cmds = get_skill_commands()
                for skill_name in skills:
                    cmd_key = f"/{skill_name}"
                    if cmd_key in skill_cmds:
                        skill_content = build_skill_invocation_message(
                            cmd_key, user_instruction=prompt
                        )
                        if skill_content:
                            prompt = skill_content
                            break  # 加载第一个匹配的技能
                    else:
                        logger.warning(
                            "[webhook] Skill '%s' not found", skill_name
                        )
            except Exception as e:
                logger.warning("[webhook] Skill loading failed: %s", e)

        # 构造唯一的 delivery ID
        delivery_id = request.headers.get(
            "X-GitHub-Delivery",
            request.headers.get(
                "svix-id",
                request.headers.get("X-Request-ID", str(int(time.time() * 1000))),
            ),
        )

        # ── 幂等性 ────────────────────────────────────────────────
        # 跳过重复的投递（webhook 重试）。
        now = time.time()
        if not self._record_delivery_id(delivery_id, now):
            logger.info(
                "[webhook] Skipping duplicate delivery %s", delivery_id
            )
            return web.json_response(
                {"status": "duplicate", "delivery_id": delivery_id},
                status=200,
            )

        # ── 直接投递模式（deliver_only） ────────────────────────
        # 完全跳过 agent —— 渲染后的 prompt 就是我们要投递的消息。
        # 适用场景：外部服务（Supabase、监控、cron 任务、其他 agent）
        # 需要以零 LLM 成本向用户聊天推送一条纯通知。复用与 agent 模式
        # 相同的 HMAC 鉴权、速率限制、幂等性和模板渲染。
        if route_config.get("deliver_only"):
            delivery = {
                "deliver": route_config.get("deliver", "log"),
                "deliver_extra": self._render_delivery_extra(
                    route_config.get("deliver_extra", {}), payload
                ),
                "payload": payload,
            }
            logger.info(
                "[webhook] direct-deliver event=%s route=%s target=%s msg_len=%d delivery=%s",
                event_type,
                route_name,
                delivery["deliver"],
                len(prompt),
                delivery_id,
            )
            try:
                result = await self._direct_deliver(prompt, delivery)
            except Exception:
                logger.exception(
                    "[webhook] direct-deliver failed route=%s delivery=%s",
                    route_name,
                    delivery_id,
                )
                return web.json_response(
                    {"status": "error", "error": "Delivery failed", "delivery_id": delivery_id},
                    status=502,
                )

            if result.success:
                return web.json_response(
                    {
                        "status": "delivered",
                        "route": route_name,
                        "target": delivery["deliver"],
                        "delivery_id": delivery_id,
                    },
                    status=200,
                )
            # 已尝试投递但目标拒绝了它 —— 返回 502 并附带一个通用错误
            # （不要泄露 adapter 层的细节）。
            logger.warning(
                "[webhook] direct-deliver target rejected route=%s target=%s error=%s",
                route_name,
                delivery["deliver"],
                result.error,
            )
            return web.json_response(
                {"status": "error", "error": "Delivery failed", "delivery_id": delivery_id},
                status=502,
            )

        # 在 session key 中使用 delivery_id，这样同一路由上的并发 webhook
        # 会获得独立的 agent 运行（不会被排队/打断）。
        session_chat_id = f"webhook:{route_name}:{delivery_id}"

        # 为 send() 存储投递信息。该 chat_id 的每次 send() 调用（中间状态
        # 消息和最终响应）都会读取它，因此我们不要在 send 时 pop。基于
        # TTL 的清理保证字典有界。
        deliver_config = {
            "deliver": route_config.get("deliver", "log"),
            "deliver_extra": self._render_delivery_extra(
                route_config.get("deliver_extra", {}), payload
            ),
            "payload": payload,
        }
        self._delivery_info[session_chat_id] = deliver_config
        self._delivery_info_created[session_chat_id] = now
        self._delivery_info_order.append((now, session_chat_id))
        self._prune_delivery_info(now)

        # 构造 source 和 event
        source = self.build_source(
            chat_id=session_chat_id,
            chat_name=f"webhook/{route_name}",
            chat_type="webhook",
            user_id=f"webhook:{route_name}",
            user_name=route_name,
        )
        if profile and isinstance(profile, str):
            source.profile = profile
        event = MessageEvent(
            text=prompt,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=payload,
            message_id=delivery_id,
        )

        logger.info(
            "[webhook] %s event=%s route=%s prompt_len=%d delivery=%s",
            request.method,
            event_type,
            route_name,
            len(prompt),
            delivery_id,
        )

        # 非阻塞 —— 立即返回 202 Accepted
        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        return web.json_response(
            {
                "status": "accepted",
                "route": route_name,
                "event": event_type,
                "delivery_id": delivery_id,
            },
            status=202,
        )

    # ------------------------------------------------------------------
    # 签名校验
    # ------------------------------------------------------------------

    def _validate_signature(
        self, request: "web.Request", body: bytes, secret: str
    ) -> bool:
        """校验 webhook 签名（GitHub、GitLab、Svix、通用 HMAC-SHA256）。"""
        def _header(name: str) -> str:
            return (
                request.headers.get(name, "")
                or request.headers.get(name.lower(), "")
                or request.headers.get(name.upper(), "")
            )

        # Svix / AgentMail：
        #   svix-id: msg_...
        #   svix-timestamp: unix 秒
        #   svix-signature: v1,<base64-hmac> [v1,<base64-hmac> ...]
        # 签名内容为："{id}.{timestamp}.{raw_body}"。Svix 密钥通常以
        # "whsec_" 开头，剩余部分是 base64 编码。
        svix_id = _header("svix-id")
        svix_timestamp = _header("svix-timestamp")
        svix_signature = _header("svix-signature")
        if svix_id or svix_timestamp or svix_signature:
            return self._validate_svix_signature(
                body=body,
                secret=secret,
                msg_id=svix_id,
                timestamp=svix_timestamp,
                signature_header=svix_signature,
            )

        # GitHub：X-Hub-Signature-256 = sha256=<hex>
        gh_sig = request.headers.get("X-Hub-Signature-256", "")
        if gh_sig:
            expected = "sha256=" + hmac.new(
                secret.encode(), body, hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(gh_sig, expected)

        # GitLab：X-Gitlab-Token = <明文 secret>
        gl_token = request.headers.get("X-Gitlab-Token", "")
        if gl_token:
            return hmac.compare_digest(gl_token, secret)

        # 通用：X-Webhook-Signature = <十六进制 HMAC-SHA256>
        generic_sig = request.headers.get("X-Webhook-Signature", "")
        if generic_sig:
            expected = hmac.new(
                secret.encode(), body, hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(generic_sig, expected)

        # 未识别到签名 header 但配置了 secret → 拒绝
        logger.debug(
            "[webhook] Secret configured but no signature header found"
        )
        return False

    def _validate_svix_signature(
        self,
        body: bytes,
        secret: str,
        msg_id: str,
        timestamp: str,
        signature_header: str,
        tolerance_seconds: int = 300,
    ) -> bool:
        """校验 AgentMail webhook 所用的、Svix 兼容的签名。"""
        if not (msg_id and timestamp and signature_header and secret):
            return False

        try:
            ts = int(timestamp)
        except (TypeError, ValueError):
            return False
        if abs(int(time.time()) - ts) > tolerance_seconds:
            logger.warning("[webhook] Svix signature timestamp outside replay window")
            return False

        if secret.startswith("whsec_"):
            encoded_secret = secret.removeprefix("whsec_")
            try:
                key = base64.b64decode(encoded_secret, validate=True)
            except (binascii.Error, ValueError):
                logger.debug("[webhook] Invalid whsec_ Svix signing secret")
                return False
        else:
            # 对那些文档记录使用 Svix 风格 header、却直接下发原始共享密钥
            # 而非 whsec_ base64 密钥的服务方，保持宽容。
            logger.debug("[webhook] Validating Svix-style signature with raw secret")
            key = secret.encode()

        signed_content = msg_id.encode() + b"." + timestamp.encode() + b"." + body
        expected = base64.b64encode(
            hmac.new(key, signed_content, hashlib.sha256).digest()
        ).decode()

        # 在密钥轮换期间，Svix 可能发送以空格分隔的多个签名。每条
        # 记录的格式为 "vN,<base64>"。
        for part in signature_header.split():
            try:
                version, signature = part.split(",", 1)
            except ValueError:
                continue
            if version == "v1" and hmac.compare_digest(signature, expected):
                return True
        return False

    # ------------------------------------------------------------------
    # Prompt 渲染
    # ------------------------------------------------------------------

    def _render_prompt(
        self,
        template: str,
        payload: dict,
        event_type: str,
        route_name: str,
    ) -> str:
        """用 webhook payload 渲染 prompt 模板。

        支持以点号表示法访问嵌套 dict：
        ``{pull_request.title}`` → ``payload["pull_request"]["title"]``

        特殊 token ``{__raw__}`` 会把整个 payload 以缩进 JSON 形式输出
        （截断到 4000 字符）。适用于监控告警，或任何 agent 需要看到
        完整 payload 的 webhook。
        """
        if not template:
            truncated = json.dumps(payload, indent=2)[:4000]
            return (
                f"Webhook event '{event_type}' on route "
                f"'{route_name}':\n\n```json\n{truncated}\n```"
            )

        def _resolve(match: re.Match) -> str:
            key = match.group(1)
            # 特殊 token：把整个 payload 以 JSON 形式输出
            if key == "__raw__":
                return json.dumps(payload, indent=2)[:4000]
            value: Any = payload
            for part in key.split("."):
                if isinstance(value, dict):
                    value = value.get(part, f"{{{key}}}")
                else:
                    return f"{{{key}}}"
            if isinstance(value, (dict, list)):
                return json.dumps(value, indent=2)[:2000]
            return str(value)

        return re.sub(r"\{([a-zA-Z0-9_.]+)\}", _resolve, template)

    def _render_delivery_extra(
        self, extra: dict, payload: dict
    ) -> dict:
        """用 payload 数据渲染 delivery_extra 模板值。"""
        rendered: Dict[str, Any] = {}
        for key, value in extra.items():
            if isinstance(value, str):
                rendered[key] = self._render_prompt(value, payload, "", "")
            else:
                rendered[key] = value
        return rendered

    # ------------------------------------------------------------------
    # 响应投递
    # ------------------------------------------------------------------

    async def _direct_deliver(
        self, content: str, delivery: dict
    ) -> SendResult:
        """不调用 agent，直接投递 *content*。

        由 ``deliver_only`` 路由使用：渲染后的模板成为字面消息体，
        我们分发给 agent 模式 ``send()`` 流程所用的同一批投递辅助函数。
        所有在 agent 模式下可用的目标类型在这里同样可用 —— Telegram、
        Discord、Slack、GitHub PR 评论等。
        """
        deliver_type = delivery.get("deliver", "log")

        if deliver_type == "log":
            # 不应到达这里 —— 启动校验会拒绝 deliver_only 搭配
            # deliver=log —— 但仍做防御性保护。
            logger.info("[webhook] direct-deliver log-only: %s", content[:200])
            return SendResult(success=True)

        if deliver_type == "github_comment":
            return await self._deliver_github_comment(content, delivery)

        # 进入跨平台分发器，它会校验目标名称并通过 gateway runner 路由。
        return await self._deliver_cross_platform(
            deliver_type, content, delivery
        )

    async def _deliver_github_comment(
        self, content: str, delivery: dict
    ) -> SendResult:
        """通过 ``gh`` CLI 把 agent 响应发布为 GitHub PR/issue 评论。"""
        extra = delivery.get("deliver_extra", {})
        repo = extra.get("repo", "")
        pr_number = extra.get("pr_number", "")

        if not repo or not pr_number:
            logger.error(
                "[webhook] github_comment delivery missing repo or pr_number"
            )
            return SendResult(
                success=False, error="Missing repo or pr_number"
            )

        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "comment",
                    str(pr_number),
                    "--repo",
                    repo,
                    "--body",
                    content,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                logger.info(
                    "[webhook] Posted comment on %s#%s", repo, pr_number
                )
                return SendResult(success=True)
            else:
                logger.error(
                    "[webhook] gh pr comment failed: %s", result.stderr
                )
                return SendResult(success=False, error=result.stderr)
        except FileNotFoundError:
            logger.error(
                "[webhook] 'gh' CLI not found — install GitHub CLI for "
                "github_comment delivery"
            )
            return SendResult(
                success=False, error="gh CLI not installed"
            )
        except Exception as e:
            logger.error("[webhook] github_comment delivery error: %s", e)
            return SendResult(success=False, error=str(e))

    async def _deliver_cross_platform(
        self, platform_name: str, content: str, delivery: dict
    ) -> SendResult:
        """把响应路由到另一个平台（telegram、discord 等）。"""
        if not self.gateway_runner:
            return SendResult(
                success=False,
                error="No gateway runner for cross-platform delivery",
            )

        try:
            target_platform = Platform(platform_name)
        except ValueError:
            return SendResult(
                success=False, error=f"Unknown platform: {platform_name}"
            )

        adapter = self.gateway_runner.adapters.get(target_platform)
        if not adapter:
            return SendResult(
                success=False,
                error=f"Platform {platform_name} not connected",
            )

        # 如果 deliver_extra 中没有指定 chat_id，则使用 home channel
        extra = delivery.get("deliver_extra", {})
        chat_id = extra.get("chat_id", "")
        if not chat_id:
            home = self.gateway_runner.config.get_home_channel(target_platform)
            if home:
                chat_id = home.chat_id
            else:
                return SendResult(
                    success=False,
                    error=f"No chat_id or home channel for {platform_name}",
                )

        # 从 deliver_extra 传入 thread_id，以便 Telegram 论坛主题正常工作
        metadata = None
        thread_id = extra.get("message_thread_id") or extra.get("thread_id")
        if thread_id:
            metadata = {"thread_id": thread_id}

        return await adapter.send(chat_id, content, metadata=metadata)
