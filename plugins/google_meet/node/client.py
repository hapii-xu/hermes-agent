"""远程 meet 节点的网关侧 RPC 客户端。

每次调用都会打开一个短生命周期的同步 WebSocket 到节点，发送
恰好一个请求，读取恰好一个响应，然后关闭。这使
客户端在非异步工具处理器中易于使用，并避免了
跨 agent turn 维护持久连接状态。

``websockets`` 包是一个可选依赖 — 我们延迟导入它，以便
插件加载不需要它。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from plugins.google_meet.node import protocol as _proto


class NodeClient:
    """与服务端请求接口匹配的轻量同步 WS 客户端。"""

    def __init__(self, url: str, token: str, timeout: float = 10.0) -> None:
        if not isinstance(url, str) or not url:
            raise ValueError("url must be a non-empty string")
        if not isinstance(token, str) or not token:
            raise ValueError("token must be a non-empty string")
        self.url = url
        self.token = token
        self.timeout = float(timeout)

    # ----- 核心 RPC -----------------------------------------------------

    def _rpc(self, type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """发送一个请求，返回响应 payload 字典。

        当服务端发送 ``error`` 信封
        或响应 id 不匹配时抛出 RuntimeError。
        """
        try:
            from websockets.sync.client import connect  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "NodeClient requires the 'websockets' package. "
                "Install it with: pip install websockets"
            ) from exc

        req = _proto.make_request(type, self.token, payload)
        raw_out = _proto.encode(req)

        with connect(self.url, open_timeout=self.timeout,
                     close_timeout=self.timeout) as ws:
            ws.send(raw_out)
            raw_in = ws.recv(timeout=self.timeout)

        if isinstance(raw_in, (bytes, bytearray)):
            raw_in = raw_in.decode("utf-8")
        resp = _proto.decode(raw_in)

        if resp.get("type") == "error":
            raise RuntimeError(f"node error: {resp.get('error', '<unknown>')}")
        if resp.get("id") != req["id"]:
            raise RuntimeError(
                f"response id mismatch: sent {req['id']}, got {resp.get('id')!r}"
            )
        payload_out = resp.get("payload")
        if not isinstance(payload_out, dict):
            # Ping 返回 {"type": "pong", "payload": {...}} — 仍是字典。
            raise RuntimeError("response missing payload dict")
        return payload_out

    # ----- 便捷方法 ---------------------------------------------------------

    def start_bot(
        self,
        url: str,
        guest_name: str = "Hermes Agent",
        duration: Optional[str] = None,
        headed: bool = False,
        mode: str = "transcribe",
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "url": url,
            "guest_name": guest_name,
            "headed": bool(headed),
            "mode": mode,
        }
        if duration is not None:
            payload["duration"] = duration
        return self._rpc("start_bot", payload)

    def stop(self) -> Dict[str, Any]:
        return self._rpc("stop", {})

    def status(self) -> Dict[str, Any]:
        return self._rpc("status", {})

    def transcript(self, last: Optional[int] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if last is not None:
            payload["last"] = int(last)
        return self._rpc("transcript", payload)

    def say(self, text: str) -> Dict[str, Any]:
        return self._rpc("say", {"text": str(text)})

    def ping(self) -> Dict[str, Any]:
        return self._rpc("ping", {})
