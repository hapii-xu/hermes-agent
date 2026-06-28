"""Browserbase 云浏览器 provider — 插件形式。

继承自 :class:`agent.browser_provider.BrowserProvider`（PR #25214 引入的
面向插件的 ABC）。旧的内置模块
``tools.browser_providers.browserbase`` 已在同一 PR 中移除；此文件
现在是规范实现。

Browserbase 需要直接提供 ``BROWSERBASE_API_KEY`` 和 ``BROWSERBASE_PROJECT_ID``
凭据。托管 Nous 网关支持已移除 — Nous
订阅现在通过 Browser Use 路由（参见
``plugins/browser/browser_use/``）。

此 provider 响应的配置键::

    browser:
      cloud_provider: "browserbase"

认证环境变量::

    BROWSERBASE_API_KEY=...       # https://browserbase.com
    BROWSERBASE_PROJECT_ID=...

可选功能开关::

    BROWSERBASE_BASE_URL=...      # 默认 https://api.browserbase.com
    BROWSERBASE_PROXIES=true      # 默认 true
    BROWSERBASE_ADVANCED_STEALTH=false
    BROWSERBASE_KEEP_ALIVE=true   # 默认 true
    BROWSERBASE_SESSION_TIMEOUT=... （秒，整数，最大 21600 = 6小时）
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict, Optional

import requests

from agent.browser_provider import BrowserProvider

logger = logging.getLogger(__name__)


class BrowserbaseBrowserProvider(BrowserProvider):
    """Browserbase (https://browserbase.com) 云浏览器后端。

    仅支持直接凭据 — 托管 Nous 网关支持已移至 Browser Use provider。
    """

    @property
    def name(self) -> str:
        return "browserbase"

    @property
    def display_name(self) -> str:
        return "Browserbase"

    def is_available(self) -> bool:
        return self._get_config_or_none() is not None

    # ------------------------------------------------------------------
    # 配置解析
    # ------------------------------------------------------------------

    def _get_config_or_none(self) -> Optional[Dict[str, Any]]:
        api_key = os.environ.get("BROWSERBASE_API_KEY")
        project_id = os.environ.get("BROWSERBASE_PROJECT_ID")
        if api_key and project_id:
            return {
                "api_key": api_key,
                "project_id": project_id,
                "base_url": os.environ.get(
                    "BROWSERBASE_BASE_URL", "https://api.browserbase.com"
                ).rstrip("/"),
            }
        return None

    def _get_config(self) -> Dict[str, Any]:
        config = self._get_config_or_none()
        if config is None:
            raise ValueError(
                "Browserbase requires BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID "
                "environment variables."
            )
        return config

    # ------------------------------------------------------------------
    # 会话生命周期
    # ------------------------------------------------------------------

    def create_session(self, task_id: str) -> Dict[str, object]:
        config = self._get_config()

        # 可选的环境变量开关
        enable_proxies = os.environ.get("BROWSERBASE_PROXIES", "true").lower() != "false"
        enable_advanced_stealth = (
            os.environ.get("BROWSERBASE_ADVANCED_STEALTH", "false").lower() == "true"
        )
        enable_keep_alive = (
            os.environ.get("BROWSERBASE_KEEP_ALIVE", "true").lower() != "false"
        )
        custom_timeout_ms = os.environ.get("BROWSERBASE_SESSION_TIMEOUT")

        features_enabled = {
            "basic_stealth": True,
            "proxies": False,
            "advanced_stealth": False,
            "keep_alive": False,
            "custom_timeout": False,
        }

        session_config: Dict[str, object] = {"projectId": config["project_id"]}

        if enable_keep_alive:
            session_config["keepAlive"] = True

        if custom_timeout_ms:
            try:
                timeout_val = int(custom_timeout_ms)
                if timeout_val > 0:
                    session_config["timeout"] = timeout_val
            except ValueError:
                logger.warning(
                    "Invalid BROWSERBASE_SESSION_TIMEOUT value: %s", custom_timeout_ms
                )

        if enable_proxies:
            session_config["proxies"] = True

        if enable_advanced_stealth:
            session_config["browserSettings"] = {"advancedStealth": True}

        # --- 通过 API 创建会话 ---
        headers = {
            "Content-Type": "application/json",
            "X-BB-API-Key": config["api_key"],
        }

        try:
            response = requests.post(
                f"{config['base_url']}/v1/sessions",
                headers=headers,
                json=session_config,
                timeout=30,
            )

            proxies_fallback = False
            keepalive_fallback = False

            # 处理 402 — 付费功能不可用
            if response.status_code == 402:
                if enable_keep_alive:
                    keepalive_fallback = True
                    logger.warning(
                        "keepAlive may require paid plan (402), retrying without it. "
                        "Sessions may timeout during long operations."
                    )
                    session_config.pop("keepAlive", None)
                    response = requests.post(
                        f"{config['base_url']}/v1/sessions",
                        headers=headers,
                        json=session_config,
                        timeout=30,
                    )

                if response.status_code == 402 and enable_proxies:
                    proxies_fallback = True
                    logger.warning(
                        "Proxies unavailable (402), retrying without proxies. "
                        "Bot detection may be less effective."
                    )
                    session_config.pop("proxies", None)
                    response = requests.post(
                        f"{config['base_url']}/v1/sessions",
                        headers=headers,
                        json=session_config,
                        timeout=30,
                    )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Browserbase API connection failed: {exc}"
            ) from exc

        if not response.ok:
            raise RuntimeError(
                f"Failed to create Browserbase session: "
                f"{response.status_code} {response.text}"
            )

        session_data = response.json()
        session_name = f"hermes_{task_id}_{uuid.uuid4().hex[:8]}"

        if enable_proxies and not proxies_fallback:
            features_enabled["proxies"] = True
        if enable_advanced_stealth:
            features_enabled["advanced_stealth"] = True
        if enable_keep_alive and not keepalive_fallback:
            features_enabled["keep_alive"] = True
        if custom_timeout_ms and "timeout" in session_config:
            features_enabled["custom_timeout"] = True

        feature_str = ", ".join(k for k, v in features_enabled.items() if v)
        logger.info(
            "Created Browserbase session %s with features: %s", session_name, feature_str
        )

        return {
            "session_name": session_name,
            "bb_session_id": session_data["id"],
            "cdp_url": session_data["connectUrl"],
            "features": features_enabled,
        }

    def close_session(self, session_id: str) -> bool:
        try:
            config = self._get_config()
        except ValueError:
            logger.warning(
                "Cannot close Browserbase session %s — missing credentials", session_id
            )
            return False

        try:
            response = requests.post(
                f"{config['base_url']}/v1/sessions/{session_id}",
                headers={
                    "X-BB-API-Key": config["api_key"],
                    "Content-Type": "application/json",
                },
                json={
                    "projectId": config["project_id"],
                    "status": "REQUEST_RELEASE",
                },
                timeout=10,
            )
            if response.status_code in {200, 201, 204}:
                logger.debug("Successfully closed Browserbase session %s", session_id)
                return True
            else:
                logger.warning(
                    "Failed to close session %s: HTTP %s - %s",
                    session_id,
                    response.status_code,
                    response.text[:200],
                )
                return False
        except Exception as e:
            logger.error("Exception closing Browserbase session %s: %s", session_id, e)
            return False

    def emergency_cleanup(self, session_id: str) -> None:
        config = self._get_config_or_none()
        if config is None:
            logger.warning(
                "Cannot emergency-cleanup Browserbase session %s — missing credentials",
                session_id,
            )
            return
        try:
            requests.post(
                f"{config['base_url']}/v1/sessions/{session_id}",
                headers={
                    "X-BB-API-Key": config["api_key"],
                    "Content-Type": "application/json",
                },
                json={
                    "projectId": config["project_id"],
                    "status": "REQUEST_RELEASE",
                },
                timeout=5,
            )
        except Exception as e:
            logger.debug(
                "Emergency cleanup failed for Browserbase session %s: %s", session_id, e
            )

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Browserbase",
            "badge": "paid",
            "tag": "Cloud browser with stealth and proxies",
            "env_vars": [
                {
                    "key": "BROWSERBASE_API_KEY",
                    "prompt": "Browserbase API key",
                    "url": "https://browserbase.com",
                },
                {
                    "key": "BROWSERBASE_PROJECT_ID",
                    "prompt": "Browserbase project ID",
                },
            ],
            "post_setup": "agent_browser",
        }
