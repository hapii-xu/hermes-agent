"""Dashboard 的认证门控中间件。

当 ``app.state.auth_required is True`` 时启用。门控的职责：

  1. 允许少量路由无需认证即可通过（登录页、``/auth/*`` OAuth 往返、
     ``/api/auth/providers``、静态资源）。
  2. 其他所有路由要求有效的 session cookie，并将已验证的
     :class:`Session` 附加到 ``request.state.session``。
  3. 在 HTML 路由上，缺失/无效 cookie 重定向到 ``/login``。
     在 ``/api/*`` 路由上，返回 401 JSON。

当 ``auth_required`` 为 False（回环模式）时，中间件为无操作；
传统的 ``_SESSION_TOKEN`` ``auth_middleware`` 处理这些绑定。
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from hermes_cli.dashboard_auth import list_providers
from hermes_cli.dashboard_auth.audit import AuditEvent, audit_log
from hermes_cli.dashboard_auth.base import ProviderError, RefreshExpiredError
from hermes_cli.dashboard_auth.cookies import read_session_cookies
from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS

_log = logging.getLogger(__name__)

# 绕过认证门控的前缀。通过 ``path == prefix`` 或
# ``path.startswith(prefix)`` 匹配——因此 ``/assets/``（带尾部斜杠）
# 匹配 ``/assets/foo.css`` 但不匹配 ``/assetsleak``。认证引导
# （登录页、OAuth 往返、提供者列表）和静态资源挂载放在此处。
_GATE_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/auth/login",
    "/auth/callback",
    "/auth/password-login",
    "/auth/logout",
    "/login",
    "/api/auth/providers",
    "/assets/",
    "/favicon.ico",
    "/ds-assets/",
    "/fonts/",
    "/fonts-terminal/",
)


def _path_is_public(path: str) -> bool:
    """如果 ``path`` 绕过 OAuth 认证门控则返回 True。

    两个公开性来源：

    * :data:`PUBLIC_API_PATHS` — 传统 ``_SESSION_TOKEN`` 中间件也
      遵守的共享 ``/api/*`` 白名单。精确匹配（无前缀扩展），
      因此添加 ``/api/status`` 不会意外暴露
      ``/api/status/secret-extension``。
    * :data:`_GATE_PUBLIC_PREFIXES` — 认证引导路由和静态挂载。
      前缀匹配，因此 ``/assets/foo.css`` 通过 ``/assets/`` 亮起。
    """
    if path in PUBLIC_API_PATHS:
        return True
    return any(
        path == prefix or path.startswith(prefix)
        for prefix in _GATE_PUBLIC_PREFIXES
    )


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def _unauth_response(request: Request, *, reason: str) -> Response:
    """API 路由 → 401 JSON 带 ``login_url``；HTML 路由 → 302 → /login。

    JSON 信封携带 ``login_url`` 字段和 ``next=`` 查询字符串，
    以便 SPA 的全局 401 处理器在重新认证后将用户送回原处。
    契约故意保持简单，以便任何 fetch 包装器都可以实现重定向
    而无需解析详细信息：

        if response.status === 401 && body.error in ("unauthenticated",
                                                       "session_expired"):
            window.location.assign(body.login_url);

    HTML 重定向也携带 ``next=`` 查询字符串，以便没有 cookie 时
    直接导航到 ``/sessions``（等）的用户在登录后回到 ``/sessions``。

    在带有 ``X-Forwarded-Prefix: /hermes`` 的反向代理下，
    ``login_url`` 会被加上前缀（``/hermes/login?next=...``），
    以便浏览器的 window.location.assign / Location: 跟随到达
    代理的登录页，而非裸 ``/login``（代理不会将其路由到 dashboard）。
    """
    from hermes_cli.dashboard_auth.prefix import prefix_from_request

    path = request.url.path
    next_param = _safe_next_target(request)
    prefix = prefix_from_request(request)
    login_url = (
        f"{prefix}/login?next={next_param}" if next_param
        else f"{prefix}/login"
    )

    if path.startswith("/api/"):
        # API 路由永远不会重定向：浏览器 fetch() API 会不透明地
        # 跟随 302 进入跨域 OAuth 流程。返回 401 和结构化信封，
        # 以便 SPA 可以整页导航到 login_url。
        error_code = (
            "session_expired"
            if reason == "invalid_or_expired_session"
            else "unauthenticated"
        )
        return JSONResponse(
            {
                "error": error_code,
                "detail": "Unauthorized",
                "reason": reason,
                "login_url": login_url,
            },
            status_code=401,
        )
    return RedirectResponse(url=login_url, status_code=302)


def _safe_next_target(request: Request) -> str:
    """构建 URL 编码的 ``next`` 查询值，或空字符串。

    仅接受同源相对路径；绝对 URL 或 ``//evil.com`` 开放重定向
    尝试会被静默丢弃。返回空字符串意味着调用者生成裸 ``/login``
    URL——没问题，用户在重新认证后着陆到 dashboard 根目录。
    """
    path = request.url.path
    # 拒绝任何不以 "/" 开头或以 "//" 开头的路径
    # （协议相对 URL——会开放重定向到攻击者主机）。
    if not path or not path.startswith("/") or path.startswith("//"):
        return ""
    # 不重定向回认证路由本身——那会形成循环。
    if any(
        path == p or path.startswith(p)
        for p in ("/login", "/auth/", "/api/auth/")
    ):
        return ""
    # 拒绝所有 ``/api/*`` 路径。401 信封代码路径在任何未认证的
    # SPA fetch 中触发（例如从 ModelsPage 发出的
    # ``GET /api/analytics/models``），SPA 的全局 401 处理器
    # 整页导航到 ``login_url``。OAuth 往返后用户会着陆到
    # API URL 并看到原始 JSON 而非 dashboard。SPA 路由可以保留
    # （它们不以 ``/api/`` 开头）；SPA 自身的
    # ``sessionStorage["hermes.lastLocation"]`` 回退（在
    # ``web/src/lib/api.ts`` 中）覆盖了深链接场景。
    if path == "/api" or path.startswith("/api/"):
        return ""
    # 保留查询字符串（如果存在）（例如 /sessions?page=2）。
    query = request.url.query
    target = f"{path}?{query}" if query else path
    # 对整个内容进行 urlencode 作为单个值。
    from urllib.parse import quote
    return quote(target, safe="")


async def gated_auth_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """仅在 ``app.state.auth_required is True`` 时启用。

    回环模式下的无操作直通，以便传统 auth_middleware 可以通过
    ``_SESSION_TOKEN`` 处理这些绑定。
    """
    if not getattr(request.app.state, "auth_required", False):
        return await call_next(request)

    path = request.url.path
    if _path_is_public(path):
        return await call_next(request)

    at, _rt = read_session_cookies(request)
    if not at and not _rt:
        # 两个 token 都不存在——完全没有 session。无需验证或刷新；强制登录。
        return _unauth_response(request, reason="no_cookie")

    # 依次尝试每个已注册提供者的 verify_session。提供者对于
    # 不认识的 token 必须返回 None（不抛出异常）。这允许
    # 多个提供者叠加——第一个识别 token 的提供者胜出。
    #
    # 当 access-token cookie 缺失但 refresh-token cookie 存在时，
    # 跳过验证直接走下面的刷新路径。这是常见的过期场景，
    # 而非边缘情况：access-token cookie 设置为
    # ``Max-Age = access_token_expires_in``（约 15 分钟），
    # 因此浏览器在 token 过期时立即清除它，而 refresh-token
    # cookie 存活 30 天。从那时起浏览器只发送
    # ``hermes_session_rt``。如果在此处因 ``not at`` 而退出，
    # 我们会在每次过期时将用户弹到 /login，尽管持有完全有效的
    # refresh token——这破坏了整个透明刷新功能。
    session = None
    if at:
        # 依次尝试每个已注册提供者的 verify_session。不识别 token 的
        # 提供者返回 None，我们继续；第一个返回 Session 的提供者胜出。
        #
        # 提供者也可能抛出 ProviderError（其 IDP/JWKS 不可达，
        # 因此既不能确认也不能否认 token）。多个提供者叠加时，
        # 这绝不能中止链——token 可能属于*另一个*可达的提供者。
        # （具体来说：self-hosted-OIDC session 先命中 `nous` 提供者，
        # 后者尝试访问 Nous Portal 的 JWKS；如果不可达则抛出，
        # 但 `self-hosted` 提供者仍然可以验证 token。）因此我们
        # 记住不可达错误并继续。只有当没有提供者验证 token
        # 且至少一个不可达时，我们才返回 503——区分
        # "临时 IDP 宕机"（不强制重新登录）和
        # "token 确实无效"（走刷新/重新登录流程）。
        unreachable_provider: str | None = None
        for provider in list_providers():
            try:
                session = provider.verify_session(access_token=at)
            except ProviderError as e:
                _log.warning(
                    "dashboard-auth: provider %r unreachable during verify: %s",
                    provider.name, e,
                )
                audit_log(
                    AuditEvent.SESSION_VERIFY_FAILURE,
                    provider=provider.name,
                    reason="provider_unreachable",
                    ip=_client_ip(request),
                )
                if unreachable_provider is None:
                    unreachable_provider = provider.name
                continue
            if session is not None:
                break
        if session is None and unreachable_provider is not None:
            # 没有提供者可以验证 token，且至少一个不可达——
            # 视为临时宕机而非强制通过（可能同样不可达的）
            # 刷新进行重新登录。
            return JSONResponse(
                {"detail": f"Auth provider {unreachable_provider!r} unreachable"},
                status_code=503,
            )

    if session is None:
        # Access token 已过期/无效。在强制重新登录之前，尝试使用
        # refresh token 轮换它（如果 session cookie 携带了的话）。
        # 成功时我们将轮换后的 cookie 重新设置在响应上并透明地
        # 服务请求；RefreshExpiredError（RT 失效/被撤销/检测到重用）
        # 时我们走清除并重新登录流程。
        refreshed = _attempt_refresh(request, refresh_token=_rt)
        if refreshed is not None:
            new_session, refreshing_provider = refreshed
            request.state.session = new_session
            response = await call_next(request)
            # 持久化轮换后的 token。Portal 在每次刷新时轮换 refresh token
            # 并运行重用检测，因此写回新的 RT 是强制性的：过期的 RT cookie
            # 会在下次刷新时重放已轮换的 token，并在（Portal 宽限期之外）
            # 撤销整个 session。将 cookie 的 Secure/Path 绑定到请求形态。
            from hermes_cli.dashboard_auth.cookies import (
                detect_https,
                set_session_cookies,
            )
            from hermes_cli.dashboard_auth.prefix import prefix_from_request

            set_session_cookies(
                response,
                access_token=new_session.access_token,
                refresh_token=new_session.refresh_token,
                access_token_expires_in=_expires_in_seconds(new_session),
                use_https=detect_https(request),
                prefix=prefix_from_request(request),
            )
            audit_log(
                AuditEvent.REFRESH_SUCCESS,
                provider=refreshing_provider,
                user_id=new_session.user_id,
                ip=_client_ip(request),
            )
            return response

        audit_log(
            AuditEvent.SESSION_VERIFY_FAILURE,
            reason="no_provider_recognises",
            ip=_client_ip(request),
        )
        response = _unauth_response(request, reason="invalid_or_expired_session")
        # 清除失效的 cookie，使浏览器不再继续发送它们。
        # 刷新已经失败（或没有 RT），因此唯一正确的下一步是
        # 通过 /login 进行完整重新认证。局部导入避免了
        # cookies → middleware 在模块加载时的循环。传递活动前缀
        # 使删除的 Path 匹配设置的 Path（否则浏览器会忽略它）。
        from hermes_cli.dashboard_auth.cookies import clear_session_cookies
        from hermes_cli.dashboard_auth.prefix import prefix_from_request
        clear_session_cookies(response, prefix=prefix_from_request(request))
        return response

    request.state.session = session
    return await call_next(request)


def _expires_in_seconds(session) -> int:
    """距离 access token ``exp`` 的秒数，下限为 60。

    镜像认证路由的 ``max(60, exp - now)``，使 access-token cookie 的
    Max-Age 跟踪 token 生命周期，即使在略有偏差的时钟上也是如此。
    ``time`` 局部导入以保持模块的导入面最小。
    """
    import time

    return max(60, int(session.expires_at) - int(time.time()))


def _attempt_refresh(request: Request, *, refresh_token):
    """尝试通过 refresh token 轮换过期的 session。

    成功时返回 ``(new_session, provider_name)``，如果没有 RT 或每个提供者的
    ``refresh_session`` 都以 ``RefreshExpiredError`` 失败
    （RT 失效/被撤销/检测到重用→强制重新登录）则返回 ``None``。

    ``ProviderError``（Portal 不可达）不会在此被吞入重新登录——
    重新抛出会使请求 500；相反我们记录日志并返回 None，
    让调用者强制干净重新登录，这比在狭窄刷新窗口内的临时网络
    波动时出现硬错误的用户体验更安全。
    """
    if not refresh_token:
        return None
    for provider in list_providers():
        try:
            new_session = provider.refresh_session(refresh_token=refresh_token)
        except RefreshExpiredError:
            # 此提供者拥有 RT 但它已失效——停止尝试其他提供者
            # （一个 RT 只属于一个提供者）并强制重新登录。
            audit_log(
                AuditEvent.REFRESH_FAILURE,
                provider=provider.name,
                reason="refresh_expired",
                ip=_client_ip(request),
            )
            return None
        except ProviderError as e:
            _log.warning(
                "dashboard-auth: provider %r unreachable during refresh: %s",
                provider.name, e,
            )
            audit_log(
                AuditEvent.REFRESH_FAILURE,
                provider=provider.name,
                reason="provider_unreachable",
                ip=_client_ip(request),
            )
            return None
        if new_session is not None:
            return new_session, provider.name
    return None

