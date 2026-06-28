"""长期存活的平台适配器共享 HTTP 客户端工厂。

网关消息平台（QQ Bot、飞书、企业微信、钉钉、Signal、
BlueBubbles、企微回调）在适配器生命周期内保持一个持久的
``httpx.AsyncClient``。这样可以跨多次 API 调用摊销 TLS/连接建立
开销，但也意味着进程的文件描述符压力对连接池回收空闲
keep-alive 连接的激进程度很敏感。

httpx 默认的 ``keepalive_expiry`` 为 5 秒。在 macOS 加 Cloudflare
Warp（以及其他透明代理）环境下，对端发起的 FIN 在本地套接字实际
释放之前可能在 ``CLOSE_WAIT`` 状态停留超过该时间——叠加 7 个长期存活
的适配器加上 LLM 客户端和 MCP 客户端，很容易触碰默认 256 个 fd 的限制。
参见 #18451。

``platform_httpx_limits()`` 返回一个更紧凑的 ``httpx.Limits``，
供适配器工厂替代 httpx 默认值使用。所选参数值：

* ``max_keepalive_connections=10``——对任何单个适配器都绰绰有余；
  平台 API 很少并行超过此数量。
* ``keepalive_expiry=2.0``——激进关闭空闲套接字，防止代理的
  滞留 CLOSE_WAIT 窗口耗尽进程文件描述符。

可通过 ``HERMES_GATEWAY_HTTPX_KEEPALIVE_EXPIRY`` /
``HERMES_GATEWAY_HTTPX_MAX_KEEPALIVE`` 环境变量在负载下调优。
"""

from __future__ import annotations

import os

try:
    import httpx
except ImportError:  # pragma: no cover —— 可选依赖
    httpx = None  # type: ignore[assignment]


_DEFAULT_KEEPALIVE_EXPIRY_S = 2.0
_DEFAULT_MAX_KEEPALIVE = 10


def platform_httpx_limits() -> "httpx.Limits | None":
    """返回针对持久平台适配器客户端调优的 ``httpx.Limits``。

    当 httpx 不可导入时返回 ``None``，使调用方无需硬依赖此辅助函数
    即可回退到 httpx 内置默认值。
    """
    if httpx is None:
        return None

    def _env_float(name: str, default: float) -> float:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return default
        return val if val > 0 else default

    def _env_int(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return default
        return val if val > 0 else default

    keepalive_expiry = _env_float(
        "HERMES_GATEWAY_HTTPX_KEEPALIVE_EXPIRY", _DEFAULT_KEEPALIVE_EXPIRY_S
    )
    max_keepalive = _env_int(
        "HERMES_GATEWAY_HTTPX_MAX_KEEPALIVE", _DEFAULT_MAX_KEEPALIVE
    )

    return httpx.Limits(
        max_keepalive_connections=max_keepalive,
        # 将 max_connections 保留为 httpx 默认值（100）——余量充足。
        keepalive_expiry=keepalive_expiry,
    )
