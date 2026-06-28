"""Firecrawl 云浏览器 provider — 插件形式。

继承自 :class:`agent.browser_provider.BrowserProvider`（PR #25214 引入的
面向插件的 ABC）。旧的内置模块
``tools.browser_providers.firecrawl`` 已在同一 PR 中移除；此文件
现在是规范实现。

这是云浏览器路径 — 与 ``plugins/web/firecrawl/`` 的 Firecrawl 网页插件不同，
后者处理 ``/v2/search`` / ``/v2/scrape`` / ``/v2/crawl`` 上的搜索/提取/爬取。
两个插件共用 ``FIRECRAWL_API_KEY`` 环境变量，但访问不同端点（此处
访问 ``/v2/browser``）。

此 provider 响应的配置键::

    browser:
      cloud_provider: "firecrawl"   # 仅支持显式选择 — 不在
                                    # 旧式自动检测流程中

认证环境变量::

    FIRECRAWL_API_KEY=...           # https://firecrawl.dev
    FIRECRAWL_API_URL=...           # 可选覆盖（默认 https://api.firecrawl.dev）
    FIRECRAWL_BROWSER_TTL=...       # 可选，默认 300 秒
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict

import requests

from agent.browser_provider import BrowserProvider

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.firecrawl.dev"


class FirecrawlBrowserProvider(BrowserProvider):
    """Firecrawl (https://firecrawl.dev) 云浏览器后端。

    仅支持云浏览器路径 — 搜索/提取/爬取功能位于独立的
    ``plugins/web/firecrawl/`` 插件中。
    """

    @property
    def name(self) -> str:
        return "firecrawl"

    @property
    def display_name(self) -> str:
        return "Firecrawl"

    def is_available(self) -> bool:
        return bool(os.environ.get("FIRECRAWL_API_KEY"))

    # ------------------------------------------------------------------
    # 会话生命周期
    # ------------------------------------------------------------------

    def _api_url(self) -> str:
        return os.environ.get("FIRECRAWL_API_URL", _BASE_URL)

    def _headers(self) -> Dict[str, str]:
        api_key = os.environ.get("FIRECRAWL_API_KEY")
        if not api_key:
            raise ValueError(
                "FIRECRAWL_API_KEY environment variable is required. "
                "Get your key at https://firecrawl.dev"
            )
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

    def create_session(self, task_id: str) -> Dict[str, object]:
        try:
            ttl = int(os.environ.get("FIRECRAWL_BROWSER_TTL", "300"))
        except (ValueError, TypeError):
            ttl = 300

        body: Dict[str, object] = {"ttl": ttl}

        try:
            response = requests.post(
                f"{self._api_url()}/v2/browser",
                headers=self._headers(),
                json=body,
                timeout=30,
            )
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Firecrawl API connection failed: {exc}"
            ) from exc

        if not response.ok:
            raise RuntimeError(
                f"Failed to create Firecrawl browser session: "
                f"{response.status_code} {response.text}"
            )

        data = response.json()
        session_name = f"hermes_{task_id}_{uuid.uuid4().hex[:8]}"

        logger.info("Created Firecrawl browser session %s", session_name)

        return {
            "session_name": session_name,
            "bb_session_id": data["id"],
            "cdp_url": data["cdpUrl"],
            "features": {"firecrawl": True},
        }

    def close_session(self, session_id: str) -> bool:
        try:
            response = requests.delete(
                f"{self._api_url()}/v2/browser/{session_id}",
                headers=self._headers(),
                timeout=10,
            )
            if response.status_code in {200, 201, 204}:
                logger.debug("Successfully closed Firecrawl session %s", session_id)
                return True
            else:
                logger.warning(
                    "Failed to close Firecrawl session %s: HTTP %s - %s",
                    session_id,
                    response.status_code,
                    response.text[:200],
                )
                return False
        except Exception as e:
            logger.error("Exception closing Firecrawl session %s: %s", session_id, e)
            return False

    def emergency_cleanup(self, session_id: str) -> None:
        if not self.is_available():
            logger.warning(
                "Cannot emergency-cleanup Firecrawl session %s — missing credentials",
                session_id,
            )
            return
        try:
            requests.delete(
                f"{self._api_url()}/v2/browser/{session_id}",
                headers=self._headers(),
                timeout=5,
            )
        except Exception as e:
            logger.debug(
                "Emergency cleanup failed for Firecrawl session %s: %s", session_id, e
            )

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Firecrawl",
            "badge": "paid",
            "tag": "Cloud browser with remote execution",
            "env_vars": [
                {
                    "key": "FIRECRAWL_API_KEY",
                    "prompt": "Firecrawl API key",
                    "url": "https://firecrawl.dev",
                },
            ],
            "post_setup": "agent_browser",
        }
