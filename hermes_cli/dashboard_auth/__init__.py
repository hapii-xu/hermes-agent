"""Dashboard 认证提供者框架。

仅当 dashboard 绑定到非回环主机且未使用 ``--insecure`` 时，
dashboard auth 门控才会生效。在该模式下，每个请求必须携带来自
已注册的 ``DashboardAuthProvider`` 插件之一的已验证 session。

Nous 提供者位于 ``plugins/dashboard-auth-nous/``，是默认提供者。
第三方可通过插件钩子 ``ctx.register_dashboard_auth_provider`` 注册自己的提供者。
"""
from hermes_cli.dashboard_auth.base import (
    DashboardAuthProvider,
    Session,
    LoginStart,
    InvalidCodeError,
    InvalidCredentialsError,
    ProviderError,
    RefreshExpiredError,
    assert_protocol_compliance,
)
from hermes_cli.dashboard_auth.registry import (
    register_provider,
    get_provider,
    list_providers,
    clear_providers,
)

__all__ = [
    "DashboardAuthProvider",
    "Session",
    "LoginStart",
    "InvalidCodeError",
    "InvalidCredentialsError",
    "ProviderError",
    "RefreshExpiredError",
    "assert_protocol_compliance",
    "register_provider",
    "get_provider",
    "list_providers",
    "clear_providers",
]
