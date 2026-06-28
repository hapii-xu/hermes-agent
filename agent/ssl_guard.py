"""Hermes Agent 的 SSL CA 证书预防性检查。

本模块在 OpenAI/httpx 将证书包路径错误转化为晦涩的
``FileNotFoundError: [Errno 2] No such file or directory`` 异常之前，
提前捕获损坏的 CA 证书包路径。
"""

from __future__ import annotations

import logging
import os
import ssl
from pathlib import Path

from agent.errors import SSLConfigurationError

logger = logging.getLogger(__name__)

_CA_BUNDLE_ENV_VARS = (
    "HERMES_CA_BUNDLE",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)

_SKIP_VALUES = {"1", "true", "yes", "on"}


def _skip_ssl_guard_enabled() -> bool:
    return os.getenv("HERMES_SKIP_SSL_GUARD", "").strip().lower() in _SKIP_VALUES


def _repair_hint() -> str:
    return (
        "Repair: python -m pip install --force-reinstall certifi openai httpx\n"
        "If you configured a custom corporate CA bundle, fix or unset the "
        "broken CA bundle environment variable."
    )


def _ssl_err(message: str) -> SSLConfigurationError:
    """创建一个一致的、用户可操作的 SSL 配置错误。"""
    return SSLConfigurationError(f"{message}\n{_repair_hint()}")


def _validate_bundle_path(label: str, value: str, *, require_substantial: bool = False) -> None:
    path = Path(value).expanduser()
    if not path.exists():
        raise _ssl_err(f"{label} points to a missing CA bundle: {value}")
    if not path.is_file():
        raise _ssl_err(f"{label} does not point to a CA bundle file: {value}")
    if require_substantial and path.stat().st_size < 1024:
        raise _ssl_err(f"{label} at {value} appears corrupted (too small)")
    try:
        ctx = ssl.create_default_context(cafile=str(path))
    except Exception as exc:
        raise _ssl_err(f"{label} CA bundle at {value} cannot be loaded: {exc}") from exc
    if not ctx.get_ca_certs():
        raise _ssl_err(f"{label} CA bundle at {value} did not load any certificates")


def verify_ca_bundle() -> None:
    """验证已配置和内置的 CA 证书是否存在且可加载。

    Raises:
        SSLConfigurationError: 若显式 CA 证书包环境变量指向无效路径，
            或 certifi 内置的 ``cacert.pem`` 缺失/损坏时抛出。
    """
    if _skip_ssl_guard_enabled():
        logger.debug("SSL CA bundle guard skipped via HERMES_SKIP_SSL_GUARD")
        return

    for env_var in _CA_BUNDLE_ENV_VARS:
        value = os.getenv(env_var)
        if value:
            _validate_bundle_path(env_var, value)

    try:
        import certifi
    except Exception as exc:
        raise _ssl_err(f"certifi is not importable: {exc}") from exc

    ca_bundle = str(certifi.where())
    _validate_bundle_path("certifi", ca_bundle, require_substantial=True)


def verify_ca_bundle_with_fallback() -> None:
    """旧调用点的向后兼容封装。

    旧 PR 名称提到了平台回退，但允许在 certifi 证书包损坏的情况下启动，
    仍会导致 httpx/OpenAI 和 requests 调用点在后续失败。
    保留封装函数名，但执行相同的检查。
    """
    verify_ca_bundle()
