"""agent → NAS ``agent-cron`` 端点的轻量 HTTP 客户端（Chronos）。

Chronos provider 只与 NAS 通信 — 不命名任何调度器供应商，也
不持有调度器凭据。NAS 拥有外部调度器（内部实现细节）及其账户；
agent 只是请求 NAS "在时间 T 设置单次触发器" / "取消" / "列出"，
使用 agent 现有的 Nous Portal access token 进行身份验证
（与调用 portal 使用的相同 token — 无需新密钥）。

通信契约：``docs/chronos-managed-cron-contract.md``。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("cron.chronos")

# portal 基础 URL 下的端点路径。
_PROVISION_PATH = "/api/agent-cron/provision"
_CANCEL_PATH = "/api/agent-cron/cancel"
_LIST_PATH = "/api/agent-cron/list"


class NasCronClientError(RuntimeError):
    """NAS agent-cron 调用失败时抛出（非 2xx 或传输错误）。"""


class NasCronClient:
    """agent→NAS provision/cancel/list 端点的最小客户端。

    使用 agent 的支持刷新的 Nous access token 进行认证。无调度器
    供应商，无调度器凭据 — NAS 在这三个调用后面隐藏了所有这些细节。
    """

    def __init__(self, portal_url: str, *, timeout_seconds: float = 15.0) -> None:
        self.portal_url = portal_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    # -- 认证 -------------------------------------------------------------

    def _access_token(self) -> str:
        """agent 现有的 Nous Portal access token（支持刷新）。"""
        from hermes_cli.auth import resolve_nous_access_token
        return resolve_nous_access_token()

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
        }

    # -- HTTP -------------------------------------------------------------

    def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        import requests  # 延迟导入：agent 已依赖 requests

        url = f"{self.portal_url}{path}"
        try:
            resp = requests.post(
                url, json=body, headers=self._headers(), timeout=self.timeout_seconds
            )
        except Exception as e:
            raise NasCronClientError(f"POST {path} failed: {e}") from e
        if resp.status_code // 100 != 2:
            raise NasCronClientError(
                f"POST {path} returned {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return resp.json() if resp.content else {}
        except Exception:
            return {}

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        import requests

        url = f"{self.portal_url}{path}"
        try:
            resp = requests.get(
                url, params=params, headers=self._headers(), timeout=self.timeout_seconds
            )
        except Exception as e:
            raise NasCronClientError(f"GET {path} failed: {e}") from e
        if resp.status_code // 100 != 2:
            raise NasCronClientError(
                f"GET {path} returned {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return resp.json() if resp.content else {}
        except Exception:
            return {}

    # -- 端点 --------------------------------------------------------

    def provision(self, *, job_id: str, fire_at: str, agent_callback_url: str,
                  dedup_key: str) -> Dict[str, Any]:
        """请求 NAS 在 ``fire_at``（ISO 8601）为 ``job_id`` 设置单次触发器。

        ``dedup_key``（``{job_id}:{fire_at}``）使对同一触发时间的重复设置
        在 NAS 侧幂等。返回 NAS 响应（例如 ``{schedule_id}``）。
        """
        return self._post(_PROVISION_PATH, {
            "job_id": job_id,
            "fire_at": fire_at,
            "agent_callback_url": agent_callback_url,
            "dedup_key": dedup_key,
        })

    def cancel(self, *, job_id: str) -> Dict[str, Any]:
        """请求 NAS 取消为 ``job_id`` 设置的任何单次触发器。"""
        return self._post(_CANCEL_PATH, {"job_id": job_id})

    def list_armed(self) -> List[Dict[str, Any]]:
        """列出 NAS 当前为此 agent 设置的单次触发器。

        返回 ``{job_id, fire_at, schedule_id}`` 列表。尽力而为：用于
        reconcile 在冷进程上查找孤立触发器；出错时
        调用者回退到对所有期望任务的幂等重新设置。
        """
        data = self._get(_LIST_PATH, {})
        items = data.get("armed") if isinstance(data, dict) else None
        return items if isinstance(items, list) else []
