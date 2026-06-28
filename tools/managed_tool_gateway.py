"""Nous 托管厂商透传的通用托管工具网关辅助函数。"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

from hermes_constants import get_hermes_home
from tools.tool_backend_helpers import managed_nous_tools_enabled

_DEFAULT_TOOL_GATEWAY_DOMAIN = "nousresearch.com"
_DEFAULT_TOOL_GATEWAY_SCHEME = "https"
_NOUS_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120


@dataclass(frozen=True)
class ManagedToolGatewayConfig:
    vendor: str
    gateway_origin: str
    nous_user_token: str
    managed_mode: bool


def auth_json_path():
    """返回 Hermes 的 auth 存储路径，遵循 HERMES_HOME 覆盖。"""
    return get_hermes_home() / "auth.json"


def _read_nous_provider_state() -> Optional[dict]:
    try:
        path = auth_json_path()
        if not path.is_file():
            return None
        data = json.loads(path.read_text())
        providers = data.get("providers", {})
        if not isinstance(providers, dict):
            return None
        nous_provider = providers.get("nous", {})
        if isinstance(nous_provider, dict):
            return nous_provider
    except Exception:
        pass
    return None


def _parse_timestamp(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _access_token_is_expiring(expires_at: object, skew_seconds: int) -> bool:
    expires = _parse_timestamp(expires_at)
    if expires is None:
        return True
    remaining = (expires - datetime.now(timezone.utc)).total_seconds()
    return remaining <= max(0, int(skew_seconds))


def peek_nous_access_token() -> Optional[str]:
    """在不触发刷新的前提下，低开销地探测 Nous 网关 token。

    可用性扫描（``hermes tools``、banner/状态绘制、provider 的
    ``is_available()`` 检查）必须避开同步 OAuth 刷新路径。因此本辅助函数
    只检查显式的环境变量覆盖与已缓存的 auth-store token，既不检查过期，
    也不发起任何网络调用。真正可靠的刷新处理留在调用
    :func:`read_nous_access_token` 的请求/会话路径中。
    """
    explicit = os.getenv("TOOL_GATEWAY_USER_TOKEN")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    nous_provider = _read_nous_provider_state() or {}
    access_token = nous_provider.get("access_token")
    if isinstance(access_token, str) and access_token.strip():
        return access_token.strip()
    return None


def read_nous_access_token() -> Optional[str]:
    """从 auth 存储或环境变量覆盖中读取 Nous Subscriber OAuth access token。"""
    explicit = os.getenv("TOOL_GATEWAY_USER_TOKEN")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    nous_provider = _read_nous_provider_state() or {}
    cached_token = peek_nous_access_token()

    if cached_token and not _access_token_is_expiring(
        nous_provider.get("expires_at"),
        _NOUS_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
    ):
        return cached_token

    try:
        from hermes_cli.auth import resolve_nous_access_token

        refreshed_token = resolve_nous_access_token(
            refresh_skew_seconds=_NOUS_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
        )
        if isinstance(refreshed_token, str) and refreshed_token.strip():
            return refreshed_token.strip()
    except Exception as exc:
        logger.debug("Nous access token refresh failed: %s", exc)

    return cached_token


def get_tool_gateway_scheme() -> str:
    """返回已配置的共享网关 URL scheme。"""
    scheme = os.getenv("TOOL_GATEWAY_SCHEME", "").strip().lower()
    if not scheme:
        return _DEFAULT_TOOL_GATEWAY_SCHEME

    if scheme in {"http", "https"}:
        return scheme

    raise ValueError("TOOL_GATEWAY_SCHEME must be 'http' or 'https'")


def build_vendor_gateway_url(vendor: str) -> str:
    """返回某个具体厂商的网关 origin。"""
    vendor_key = f"{vendor.upper().replace('-', '_')}_GATEWAY_URL"
    explicit_vendor_url = os.getenv(vendor_key, "").strip().rstrip("/")
    if explicit_vendor_url:
        return explicit_vendor_url

    shared_scheme = get_tool_gateway_scheme()
    shared_domain = os.getenv("TOOL_GATEWAY_DOMAIN", "").strip().strip("/")
    if shared_domain:
        return f"{shared_scheme}://{vendor}-gateway.{shared_domain}"

    return f"{shared_scheme}://{vendor}-gateway.{_DEFAULT_TOOL_GATEWAY_DOMAIN}"


def resolve_managed_tool_gateway(
    vendor: str,
    gateway_builder: Optional[Callable[[str], str]] = None,
    token_reader: Optional[Callable[[], Optional[str]]] = None,
) -> Optional[ManagedToolGatewayConfig]:
    """为某个厂商解析共享的托管工具网关配置。"""
    if not managed_nous_tools_enabled():
        return None

    resolved_gateway_builder = gateway_builder or build_vendor_gateway_url
    resolved_token_reader = token_reader or read_nous_access_token

    gateway_origin = resolved_gateway_builder(vendor)
    nous_user_token = resolved_token_reader()
    if not gateway_origin or not nous_user_token:
        return None

    return ManagedToolGatewayConfig(
        vendor=vendor,
        gateway_origin=gateway_origin,
        nous_user_token=nous_user_token,
        managed_mode=True,
    )


def is_managed_tool_gateway_ready(
    vendor: str,
    gateway_builder: Optional[Callable[[str], str]] = None,
    token_reader: Optional[Callable[[], Optional[str]]] = None,
) -> bool:
    """当存在网关 URL 且很可能可用的 Nous token 时返回 True。

    默认使用 :func:`peek_nous_access_token`，使只读的可用性扫描避免同步
    OAuth 刷新。即将发起真实网关请求的调用方应使用
    :func:`resolve_managed_tool_gateway`（它仍默认使用感知刷新的
    :func:`read_nous_access_token`）。
    """
    return resolve_managed_tool_gateway(
        vendor,
        gateway_builder=gateway_builder,
        token_reader=token_reader or peek_nous_access_token,
    ) is not None
