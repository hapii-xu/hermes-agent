"""绕过 dashboard 认证的 ``/api/*`` 路径的共享白名单。

两个中间件强制执行 dashboard 认证，之前各自维护此列表的独立副本：

* ``hermes_cli.web_server.auth_middleware`` — 回环 / ``--insecure``
  模式，基于临时 ``_SESSION_TOKEN`` 门控。
* ``hermes_cli.dashboard_auth.middleware.gated_auth_middleware`` —
  非回环模式，基于 OAuth session cookie 门控。

当列表漂移时，``/api/status`` 最终在legacy门控下公开，但在 OAuth
门控下返回 401。这破坏了 portal 的通配符活性探测
（``nous-account-service`` ``fly-provider.ts``
``getInstanceRuntimeStatus``），它无 cookie 获取 ``/api/status``
作为"agent dashboard 存活"的唯一信号：每个健康的通配符子域 agent
在 portal UI 中显示为 STARTING/down，即使 dashboard 正常服务。

在此集中白名单，以便两个中间件导入相同的 frozenset，防止下次漂移。
保持此列表最小——只有真正非敏感的只读端点应该在这里。作为健全性
检查，每个条目都应该可以安全地暴露给：

  * 外部 uptime 探测（Pingdom、Better Stack、NAS），
  * 用户登录前的 dashboard SPA，
  * 任何碰巧 ``curl`` 主机名的人。

如果新端点未通过所有三个测试，它应该被门控，SPA 应在登录后引导它。
"""
from __future__ import annotations

PUBLIC_API_PATHS: frozenset[str] = frozenset({
    # Liveness probe target. Returns version, gateway state, active
    # session count, and the dashboard auth-gate shape. No bodies, no
    # session content, no secrets. Documented as the portal's wildcard
    # liveness probe in
    # ``docs/agent-dashboard-public-url-contract.md`` (NAS side).
    "/api/status",
    # Read-only config-defaults / schema feeds for the SPA's Config page.
    "/api/config/defaults",
    "/api/config/schema",
    # Read-only model metadata (context windows, etc.) — same shape as
    # provider catalogs already exposed on the public internet.
    "/api/model/info",
    # Read-only theme + plugin manifests for the dashboard skin engine.
    "/api/dashboard/themes",
    "/api/dashboard/plugins",
    # Chronos managed-cron fire webhook (NAS -> agent). NOT cookie-gated: it
    # carries its own short-lived NAS-minted JWT (purpose=cron_fire), which the
    # handler verifies as the real auth. Must bypass the dashboard auth gate so
    # the NAS relay's bearer-only callback reaches the verifier instead of a
    # 401 no_cookie. The JWT — not this allowlist — is the security boundary.
    "/api/cron/fire",
})
