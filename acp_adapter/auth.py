"""ACP 认证辅助模块 — 检测并广播 Hermes 认证方式。"""

from __future__ import annotations

from typing import Any, Optional


TERMINAL_SETUP_AUTH_METHOD_ID = "hermes-setup"


def detect_provider() -> Optional[str]:
    """解析当前活跃的 Hermes 运行时 provider，如果不可用则返回 None。

    将 ``Callable`` 类型的 ``api_key``（Azure Foundry Entra ID bearer
    token 提供者 — 参见 :mod:`agent.azure_identity_adapter`）视为有效凭据。
    如果不做此处理，使用 Entra 配置的 Foundry 部署的 ACP 会话会静默地
    回退到 ``"openrouter"``，导致 ACP 认证握手拒绝合法的 provider。
    """
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        runtime = resolve_runtime_provider()
        api_key = runtime.get("api_key")
        provider = runtime.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            return None
        is_string_key = isinstance(api_key, str) and api_key.strip()
        is_callable_provider = callable(api_key) and not isinstance(api_key, str)
        if is_string_key or is_callable_provider:
            return provider.strip().lower()
    except Exception:
        return None
    return None


def has_provider() -> bool:
    """如果 Hermes 能够解析到任何运行时 provider 凭据，则返回 True。"""
    return detect_provider() is not None


def build_auth_methods() -> list[Any]:
    """返回适用于 Hermes 的注册中心兼容 ACP 认证方法列表。

    官方 ACP 注册中心会验证代理在初始握手期间至少广播一种可用的认证方式。
    全新安装的 Zed 可能尚未配置 Hermes provider 凭据，因此 Hermes 始终广播
    一个终端设置方法。当凭据已存在时，还会将解析到的 provider 作为默认的
    代理管理运行时凭据方式进行广播。
    """
    from acp.schema import AuthMethodAgent, TerminalAuthMethod

    methods: list[Any] = []
    provider = detect_provider()
    if provider:
        methods.append(
            AuthMethodAgent(
                id=provider,
                name=f"{provider} runtime credentials",
                description=(
                    "Authenticate Hermes using the currently configured "
                    f"{provider} runtime credentials."
                ),
            )
        )

    methods.append(
        TerminalAuthMethod(
            id=TERMINAL_SETUP_AUTH_METHOD_ID,
            name="Configure Hermes provider",
            description=(
                "Open Hermes' interactive model/provider setup in a terminal. "
                "Use this when Hermes has not been configured on this machine yet."
            ),
            type="terminal",
            args=["--setup"],
        )
    )
    return methods
