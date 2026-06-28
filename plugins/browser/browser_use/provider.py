"""Browser Use 云浏览器 provider — 插件形式。

继承 :class:`agent.browser_provider.BrowserProvider`（PR #25214 中引入的
面向插件的抽象基类）。旧的内嵌模块
``tools.browser_providers.browser_use`` 已在同一 PR 中移除；此文件
现在是规范实现。

Browser Use 是唯一支持双重认证的浏览器后端：直接使用的
``BROWSER_USE_API_KEY`` 供自付费用户使用，或托管的 Nous tool
gateway（Hermes 用它将 Browser Use 会话计费到 Nous
订阅）。分派顺序——先直接 API key，再托管 gateway
——保留了 ``tools.browser_providers.browser_use.BrowserUseProvider._get_config_or_none``
中迁移前的行为。

此 provider 响应的配置键::

    browser:
      cloud_provider: "browser-use"   # 显式选择
    tool_gateway:
      browser: "gateway"              # 可选：优先使用托管 gateway
                                      #   即使设置了 BROWSER_USE_API_KEY

认证环境变量（二选一）::

    BROWSER_USE_API_KEY=...           # https://browser-use.com
    # 或托管的 Nous gateway 条目（通过 'hermes setup' 配置）
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from typing import Any, Dict, Optional

import requests

from agent.browser_provider import BrowserProvider

logger = logging.getLogger(__name__)

# 托管模式下会话创建的幂等性跟踪。托管的 Nous
# gateway 在重试 POST 时返回 409 "already in progress"；我们转发
# 原始幂等键以便 gateway 可以去重。成功或
# 最终失败时清除。
_pending_create_keys: Dict[str, str] = {}
_pending_create_keys_lock = threading.Lock()

_BASE_URL = "https://api.browser-use.com/api/v3"
_DEFAULT_MANAGED_TIMEOUT_MINUTES = 5
_DEFAULT_MANAGED_PROXY_COUNTRY_CODE = "us"


def _get_or_create_pending_create_key(task_id: str) -> str:
    with _pending_create_keys_lock:
        existing = _pending_create_keys.get(task_id)
        if existing:
            return existing

        created = f"browser-use-session-create:{uuid.uuid4().hex}"
        _pending_create_keys[task_id] = created
        return created


def _clear_pending_create_key(task_id: str) -> None:
    with _pending_create_keys_lock:
        _pending_create_keys.pop(task_id, None)


def _should_preserve_pending_create_key(response: requests.Response) -> bool:
    """在创建失败后决定是否保留幂等键。

    当失败看起来可重试（5xx）或 gateway 报告
    原始请求仍在进行中（409 "already in progress"）时保留
    该键——在这两种情况下，使用相同的键重试可以让
    gateway 去重。

    在任何其他 4xx（认证失败、错误请求等）时丢弃
    该键——这些不会通过重试成功。
    """
    if response.status_code >= 500:
        return True

    if response.status_code != 409:
        return False

    try:
        payload = response.json()
    except Exception:
        return False

    if not isinstance(payload, dict):
        return False

    error = payload.get("error")
    if not isinstance(error, dict):
        return False

    message = str(error.get("message") or "").lower()
    return "already in progress" in message


class BrowserUseBrowserProvider(BrowserProvider):
    """Browser Use (https://browser-use.com) 云浏览器后端。

    双重认证：当设置了 BROWSER_USE_API_KEY 时优先使用，否则
    回退到托管的 Nous tool gateway（当 ``tool_gateway.browser`` 配置
    路由到它时）。设置 ``tool_gateway.browser: gateway`` 会翻转
    顺序，使得即使存在 BROWSER_USE_API_KEY，托管计费也优先。
    """

    @property
    def name(self) -> str:
        return "browser-use"

    @property
    def display_name(self) -> str:
        return "Browser Use"

    def is_available(self) -> bool:
        return self._get_config_or_none(refresh_token=False) is not None

    # ------------------------------------------------------------------
    # 配置解析（直接 API key 或托管 Nous gateway）
    # ------------------------------------------------------------------

    def _get_config_or_none(self, *, refresh_token: bool = True) -> Optional[Dict[str, Any]]:
        # 在此处导入以避免在模块导入时产生硬依赖——
        # managed_tool_gateway 会拉入 Nous 认证栈，这可能
        # 很重，而且直接 API key 用户不需要它。
        from tools.managed_tool_gateway import (
            peek_nous_access_token,
            resolve_managed_tool_gateway,
        )
        from tools.tool_backend_helpers import prefers_gateway

        # 除非用户通过 ``tool_gateway.browser: gateway`` 显式选择了
        # 托管的 Nous gateway，否则直接 API key 优先。
        api_key = os.environ.get("BROWSER_USE_API_KEY")
        if api_key and not prefers_gateway("browser"):
            return {
                "api_key": api_key,
                "base_url": _BASE_URL,
                "managed_mode": False,
            }

        # 让可用性扫描远离同步 OAuth 刷新路径。
        managed = resolve_managed_tool_gateway(
            "browser-use",
            token_reader=None if refresh_token else peek_nous_access_token,
        )
        if managed is None:
            return None

        return {
            "api_key": managed.nous_user_token,
            "base_url": managed.gateway_origin.rstrip("/"),
            "managed_mode": True,
        }

    def _get_config(self) -> Dict[str, Any]:
        from tools.tool_backend_helpers import managed_nous_tools_enabled

        config = self._get_config_or_none()
        if config is None:
            message = (
                "Browser Use requires a direct BROWSER_USE_API_KEY credential."
            )
            if managed_nous_tools_enabled():
                message = (
                    "Browser Use requires either a direct BROWSER_USE_API_KEY "
                    "credential or a managed Browser Use gateway configuration."
                )
            raise ValueError(message)
        return config

    # ------------------------------------------------------------------
    # 会话生命周期
    # ------------------------------------------------------------------

    def _headers(self, config: Dict[str, Any]) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Browser-Use-API-Key": config["api_key"],
        }

    def create_session(self, task_id: str) -> Dict[str, object]:
        config = self._get_config()
        managed_mode = bool(config.get("managed_mode"))

        headers = self._headers(config)
        if managed_mode:
            headers["X-Idempotency-Key"] = _get_or_create_pending_create_key(task_id)

        # 保持 gateway 支持的会话短暂，以便计费授权不会
        # 在 Hermes 只需要任务范围的临时浏览器时
        # 默认使用较长的 Browser-Use 超时。
        payload = (
            {
                "timeout": _DEFAULT_MANAGED_TIMEOUT_MINUTES,
                "proxyCountryCode": _DEFAULT_MANAGED_PROXY_COUNTRY_CODE,
            }
            if managed_mode
            else {}
        )

        try:
            response = requests.post(
                f"{config['base_url']}/browsers",
                headers=headers,
                json=payload,
                timeout=30,
            )
        except requests.RequestException as exc:
            # 托管模式：原样抛出，以便调用方可以使用保留的
            # 幂等键重试。直接模式：将网络错误包装为
            # 干净的 RuntimeError 以便终端用户理解。
            if managed_mode:
                raise
            raise RuntimeError(
                f"Browser Use API connection failed: {exc}"
            ) from exc

        if not response.ok:
            if managed_mode and not _should_preserve_pending_create_key(response):
                _clear_pending_create_key(task_id)
            raise RuntimeError(
                f"Failed to create Browser Use session: "
                f"{response.status_code} {response.text}"
            )

        session_data = response.json()
        if managed_mode:
            _clear_pending_create_key(task_id)
        session_name = f"hermes_{task_id}_{uuid.uuid4().hex[:8]}"
        external_call_id = (
            response.headers.get("x-external-call-id") if managed_mode else None
        )

        logger.info("Created Browser Use session %s", session_name)

        cdp_url = session_data.get("cdpUrl") or session_data.get("connectUrl") or ""

        return {
            "session_name": session_name,
            "bb_session_id": session_data["id"],
            "cdp_url": cdp_url,
            "features": {"browser_use": True},
            "external_call_id": external_call_id,
        }

    def close_session(self, session_id: str) -> bool:
        try:
            config = self._get_config()
        except ValueError:
            logger.warning(
                "Cannot close Browser Use session %s — missing credentials", session_id
            )
            return False

        try:
            response = requests.patch(
                f"{config['base_url']}/browsers/{session_id}",
                headers=self._headers(config),
                json={"action": "stop"},
                timeout=10,
            )
            if response.status_code in {200, 201, 204}:
                logger.debug("Successfully closed Browser Use session %s", session_id)
                return True
            else:
                logger.warning(
                    "Failed to close Browser Use session %s: HTTP %s - %s",
                    session_id,
                    response.status_code,
                    response.text[:200],
                )
                return False
        except Exception as e:
            logger.error("Exception closing Browser Use session %s: %s", session_id, e)
            return False

    def emergency_cleanup(self, session_id: str) -> None:
        config = self._get_config_or_none()
        if config is None:
            logger.warning(
                "Cannot emergency-cleanup Browser Use session %s — missing credentials",
                session_id,
            )
            return
        try:
            requests.patch(
                f"{config['base_url']}/browsers/{session_id}",
                headers=self._headers(config),
                json={"action": "stop"},
                timeout=5,
            )
        except Exception as e:
            logger.debug(
                "Emergency cleanup failed for Browser Use session %s: %s", session_id, e
            )

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Browser Use",
            "badge": "paid",
            "tag": "Cloud browser with remote execution",
            "env_vars": [
                {
                    "key": "BROWSER_USE_API_KEY",
                    "prompt": "Browser Use API key",
                    "url": "https://browser-use.com",
                },
            ],
            "post_setup": "agent_browser",
        }
