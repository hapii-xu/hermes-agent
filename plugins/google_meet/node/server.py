"""远程节点服务器。

在将托管 Meet bot 的机器上运行（通常是用户已登录 Chrome 的
Mac 笔记本电脑）。暴露一个 WebSocket 端点，接受签名的 RPC 请求
并将它们分发到现有的
``plugins.google_meet.process_manager`` 模块。

通过 ``hermes meet node run`` 启动。

Token 处理
--------------
首次启动时，我们生成 32 个十六进制字符的熵并将其持久化到
``$HERMES_HOME/workspace/meetings/node_token.json``。后续启动
复用同一 token，以便之前已批准的网关无需重新
配对。操作员通过带外方式将此 token 复制到网关
通过 ``hermes meet node approve <name> <url> <token>``。

依赖项
------------
``websockets`` 是一个可选依赖。我们在
:meth:`serve` 中延迟导入它，因此除非您
实际托管节点，否则安装插件不需要它。
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home
from plugins.google_meet.node import protocol as _proto


def _default_token_path() -> Path:
    return Path(get_hermes_home()) / "workspace" / "meetings" / "node_token.json"


class NodeServer:
    """本地执行 meet bot RPC 的 WebSocket 服务器。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 18789,
        token_path: Optional[Path] = None,
        display_name: str = "hermes-meet-node",
    ) -> None:
        self.host = host
        self.port = port
        self.display_name = display_name
        self.token_path = Path(token_path) if token_path is not None else _default_token_path()
        self._token: Optional[str] = None

    # ----- token 管理 --------------------------------------------

    def ensure_token(self) -> str:
        """返回已持久化的共享密钥，首次使用时生成一个。"""
        if self._token:
            return self._token
        if self.token_path.is_file():
            try:
                data = json.loads(self.token_path.read_text(encoding="utf-8"))
                tok = data.get("token")
                if isinstance(tok, str) and tok:
                    self._token = tok
                    return tok
            except (OSError, json.JSONDecodeError):
                pass
        tok = secrets.token_hex(16)  # 32 hex chars
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"token": tok, "generated_at": time.time()}, indent=2),
            encoding="utf-8",
        )
        # 限制为仅所有者可读写 — token 授予对 meet bot 的完整 RPC
        # 访问权限（启动、转录、在会议中发言）。
        try:
            tmp.chmod(0o600)
        except (OSError, NotImplementedError):
            # 在非 POSIX 文件系统上尽力而为；在 POSIX 上设置模式。
            pass
        tmp.replace(self.token_path)
        self._token = tok
        return tok

    def get_token(self) -> str:
        """:meth:`ensure_token` 的别名；后续调用不会变更。"""
        return self.ensure_token()

    # ----- dispatch -----------------------------------------------------

    async def _handle_request(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """Validate + dispatch a single decoded request envelope.

        Always returns a response envelope (success or error); never
        raises. Errors from inside the process_manager are wrapped into
        the response payload's ``ok``/``error`` keys (which pm already
        does) rather than being re-encoded as error envelopes — the
        envelope-level error channel is reserved for auth / protocol
        failures.
        """
        expected = self.ensure_token()
        ok, reason = _proto.validate_request(msg, expected)
        if not ok:
            return _proto.make_error(str(msg.get("id") or ""), reason)

        req_id = msg["id"]
        t = msg["type"]
        payload = msg["payload"]

        # 延迟导入，以便测试 mock 可以自由 monkeypatch。
        from plugins.google_meet import process_manager as pm

        try:
            if t == "ping":
                return {"type": "pong", "id": req_id,
                        "payload": {"display_name": self.display_name,
                                    "ts": time.time()}}
            if t == "start_bot":
                # 白名单我们传递给 pm.start 的 kwargs。
                kwargs = {
                    k: payload[k]
                    for k in ("url", "guest_name", "duration", "headed",
                              "auth_state", "session_id", "out_dir")
                    if k in payload
                }
                if "url" not in kwargs:
                    return _proto.make_error(req_id, "missing 'url' in payload")
                result = pm.start(**kwargs)
                return _proto.make_response(req_id, result)
            if t == "stop":
                reason_arg = payload.get("reason", "requested")
                result = pm.stop(reason=reason_arg)
                return _proto.make_response(req_id, result)
            if t == "status":
                return _proto.make_response(req_id, pm.status())
            if t == "transcript":
                last = payload.get("last")
                result = pm.transcript(last=last)
                return _proto.make_response(req_id, result)
            if t == "say":
                # v2 接线：当存在时，将内容入队到活跃
                # 会议的 out_dir 中的 say_queue.jsonl。bot 侧
                # 消费者为 v3+（对于 v1，这是一个返回 ok 的存根）。
                text = payload.get("text", "")
                active = pm._read_active()  # type: ignore[attr-defined]
                enqueued = False
                if active and active.get("out_dir"):
                    queue = Path(active["out_dir"]) / "say_queue.jsonl"
                    try:
                        queue.parent.mkdir(parents=True, exist_ok=True)
                        with queue.open("a", encoding="utf-8") as fh:
                            fh.write(json.dumps({"text": text, "ts": time.time()}) + "\n")
                        enqueued = True
                    except OSError:
                        enqueued = False
                return _proto.make_response(
                    req_id,
                    {"ok": True, "enqueued": enqueued, "text": text},
                )
        except Exception as exc:  # noqa: BLE001 — surface any pm crash to client
            return _proto.make_error(req_id, f"{type(exc).__name__}: {exc}")

        return _proto.make_error(req_id, f"unhandled type: {t!r}")

    # ----- server loop --------------------------------------------------

    async def serve(self) -> None:
        """运行 WebSocket 服务器直到取消。

        永久阻塞。调用方通常将其包装在 ``asyncio.run`` 中。
        """
        try:
            import websockets  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "NodeServer.serve requires the 'websockets' package. "
                "Install it with: pip install websockets"
            ) from exc

        self.ensure_token()

        async def _handler(ws):
            async for raw in ws:
                try:
                    msg = _proto.decode(raw if isinstance(raw, str) else raw.decode("utf-8"))
                except ValueError as exc:
                    await ws.send(_proto.encode(_proto.make_error("", f"decode: {exc}")))
                    continue
                reply = await self._handle_request(msg)
                await ws.send(_proto.encode(reply))

        async with websockets.serve(_handler, self.host, self.port):
            # 运行直到取消。
            import asyncio
            await asyncio.Future()
