"""共享的 FAL.ai SDK 基础设施。

存放每个基于 FAL 的工具都需要的无状态原子组件：

* :func:`import_fal_client` —— 延迟导入 + ``lazy_deps`` 集成，
  这样 ``fal_client`` 不会在冷启动时被拉入（急切导入时
  每次 CLI 调用会增加约 64 ms）。
* :class:`_ManagedFalSyncClient` —— 通过标准的 ``fal_client.SyncClient``
  原语驱动 Nous 托管的 fal-queue 网关的包装器。
* :func:`_normalize_fal_queue_url_format`、:func:`_extract_http_status`
  —— 托管客户端包装器和 ``_submit_fal_request`` 共用的小工具函数。

有状态的部件（缓存全局变量、``_managed_fal_client*`` 选择器、
``_submit_fal_request``）刻意保留在
:mod:`tools.image_generation_tool` 上。该模块是现有测试套件
（``tests/tools/test_image_generation.py``、
``tests/tools/test_managed_media_gateways.py``）以及
``plugins/image_gen/fal/`` 插件 ``_it`` 间接层的 patch 目标 —— 把缓存
移到这里会悄悄使 ``monkeypatch.setattr(image_tool,
"_managed_fal_client", None)`` 失效，因为查找会改走
``fal_common`` 的命名空间。详见 issue #26241 中逐规则的说明。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Union
from urllib.parse import urlencode


def import_fal_client() -> Any:
    """导入 ``fal_client``（可用时通过 ``lazy_deps``）并返回
    模块引用。

    调用方负责把结果缓存到自己的模块全局变量上 —— 保持按模块的全局变量
    让测试可以 monkey-patch 目标模块的 ``fal_client`` 属性，
    并使该 patched 值对该模块的调用点持续生效。

    当该包确实不可用时抛出 :class:`ImportError`。
    """
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("image.fal", prompt=False)
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 —— lazy_deps 会透出安装提示
        raise ImportError(str(exc))
    import fal_client  # type: ignore  # noqa: WPS433 —— 刻意延迟
    return fal_client


def _normalize_fal_queue_url_format(queue_run_origin: str) -> str:
    normalized_origin = str(queue_run_origin or "").strip().rstrip("/")
    if not normalized_origin:
        raise ValueError("Managed FAL queue origin is required")
    return f"{normalized_origin}/"


def _extract_http_status(exc: BaseException) -> Optional[int]:
    """从 httpx/fal 异常中返回 HTTP 状态码，否则返回 None。

    对各种异常形态做防御式处理 —— httpx.HTTPStatusError 暴露
    ``.response.status_code``，而 fal_client 包装器可能直接暴露
    ``.status_code``。
    """
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    return None


class _ManagedFalSyncClient:
    """针对托管队列主机的、基于 ``fal_client.SyncClient`` 的
    轻量按实例包装器。

    该包装器自带 ``fal_client`` 模块引用，而不是去取模块全局变量，
    因此调用方仍能控制使用哪个模块作用域内的 ``fal_client``
    （这对那些替换 legacy 模块 ``fal_client`` 属性的测试 patch 很重要）。
    """

    def __init__(self, fal_client: Any, *, key: str, queue_run_origin: str):
        sync_client_class = getattr(fal_client, "SyncClient", None)
        if sync_client_class is None:
            raise RuntimeError("fal_client.SyncClient is required for managed FAL gateway mode")

        client_module = getattr(fal_client, "client", None)
        if client_module is None:
            raise RuntimeError("fal_client.client is required for managed FAL gateway mode")

        self._queue_url_format = _normalize_fal_queue_url_format(queue_run_origin)
        self._sync_client = sync_client_class(key=key)
        self._http_client = getattr(self._sync_client, "_client", None)
        self._maybe_retry_request = getattr(client_module, "_maybe_retry_request", None)
        self._raise_for_status = getattr(client_module, "_raise_for_status", None)
        self._request_handle_class = getattr(client_module, "SyncRequestHandle", None)
        self._add_hint_header = getattr(client_module, "add_hint_header", None)
        self._add_priority_header = getattr(client_module, "add_priority_header", None)
        self._add_timeout_header = getattr(client_module, "add_timeout_header", None)

        if self._http_client is None:
            raise RuntimeError("fal_client.SyncClient._client is required for managed FAL gateway mode")
        if self._maybe_retry_request is None or self._raise_for_status is None:
            raise RuntimeError("fal_client.client request helpers are required for managed FAL gateway mode")
        if self._request_handle_class is None:
            raise RuntimeError("fal_client.client.SyncRequestHandle is required for managed FAL gateway mode")

    def submit(
        self,
        application: str,
        arguments: Dict[str, Any],
        *,
        path: str = "",
        hint: Optional[str] = None,
        webhook_url: Optional[str] = None,
        priority: Any = None,
        headers: Optional[Dict[str, str]] = None,
        start_timeout: Optional[Union[int, float]] = None,
    ):
        url = self._queue_url_format + application
        if path:
            url += "/" + path.lstrip("/")
        if webhook_url is not None:
            url += "?" + urlencode({"fal_webhook": webhook_url})

        request_headers = dict(headers or {})
        if hint is not None and self._add_hint_header is not None:
            self._add_hint_header(hint, request_headers)
        if priority is not None:
            if self._add_priority_header is None:
                raise RuntimeError("fal_client.client.add_priority_header is required for priority requests")
            self._add_priority_header(priority, request_headers)
        if start_timeout is not None:
            if self._add_timeout_header is None:
                raise RuntimeError("fal_client.client.add_timeout_header is required for timeout requests")
            self._add_timeout_header(start_timeout, request_headers)

        response = self._maybe_retry_request(
            self._http_client,
            "POST",
            url,
            json=arguments,
            timeout=getattr(self._sync_client, "default_timeout", 120.0),
            headers=request_headers,
        )
        self._raise_for_status(response)

        data = response.json()
        return self._request_handle_class(
            request_id=data["request_id"],
            response_url=data["response_url"],
            status_url=data["status_url"],
            cancel_url=data["cancel_url"],
            client=self._http_client,
        )
