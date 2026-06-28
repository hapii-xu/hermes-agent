"""基于异步流的轻量 LSP JSON-RPC 2.0 分帧器。

LSP 线路格式：

    Content-Length: <bytes>\\r\\n
    \\r\\n
    <utf-8 JSON body>

消息体是 JSON-RPC 2.0 信封：request、response 或 notification。

本模块替代了 TypeScript 实现中 ``vscode-jsonrpc/node`` 的职责。
我们刻意保持精简——仅提供分帧和信封辅助——让 :class:`agent.lsp.client.LSPClient`
可以专注于协议语义。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger("agent.lsp.protocol")

# 我们关心的 LSP 错误码。完整列表见
# https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#errorCodes
ERROR_CONTENT_MODIFIED = -32801
ERROR_REQUEST_CANCELLED = -32800
ERROR_METHOD_NOT_FOUND = -32601


class LSPProtocolError(Exception):
    """当线路协议被违反时抛出。

    与 :class:`LSPRequestError` 不同，后者表示服务器返回了 JSON-RPC
    错误响应——这是符合协议的行为。本异常表示分帧或信封本身已损坏。
    """


class LSPRequestError(Exception):
    """当 LSP 请求返回错误响应时抛出。

    携带 JSON-RPC 的 ``code``、``message`` 和可选的 ``data``。
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"LSP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


def encode_message(obj: dict) -> bytes:
    """将 JSON-RPC 信封编码为 Content-Length 分帧的字节串。

    消息体编码为紧凑的 UTF-8 JSON（分隔符之间无空格）——与
    ``vscode-jsonrpc`` 的输出一致，确保 Content-Length 计数精确。
    """
    body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


async def read_message(reader: asyncio.StreamReader) -> Optional[dict]:
    """从流中读取一条 Content-Length 分帧的 JSON-RPC 消息。

    在正常 EOF 时返回 ``None``（服务器在消息之间干净地关闭了 stdout
    ——典型的关闭流程）。在分帧格式错误时抛出 :class:`LSPProtocolError`。

    成功时读取位置会前进到 JSON 消息体之后。
    """
    headers: dict = {}
    header_bytes = 0
    while True:
        try:
            line = await reader.readuntil(b"\r\n")
        except asyncio.IncompleteReadError as e:
            # 读取头部时遇到 EOF。如果尚未开始头部块，视为正常 EOF；
            # 否则视为分帧损坏。
            if not e.partial and not headers:
                return None
            raise LSPProtocolError(
                f"unexpected EOF while reading LSP headers (partial={e.partial!r})"
            ) from e
        # 防御性上限：防止服务器持续发送头部但从不发出 CRLF-CRLF。
        # 将总头部字节数限制在 8 KiB——正常服务器远低于 200 字节。
        header_bytes += len(line)
        if header_bytes > 8192:
            raise LSPProtocolError(
                f"LSP header block exceeded 8 KiB without terminator"
            )
        line = line[:-2]  # strip CRLF
        if not line:
            break  # blank line ends header block
        try:
            key, _, value = line.decode("ascii").partition(":")
        except UnicodeDecodeError as e:
            raise LSPProtocolError(f"non-ASCII LSP header: {line!r}") from e
        if not key:
            raise LSPProtocolError(f"malformed LSP header line: {line!r}")
        headers[key.strip().lower()] = value.strip()

    cl = headers.get("content-length")
    if cl is None:
        raise LSPProtocolError(f"LSP message missing Content-Length: {headers!r}")
    try:
        n = int(cl)
    except ValueError as e:
        raise LSPProtocolError(f"non-integer Content-Length: {cl!r}") from e
    if n < 0 or n > 64 * 1024 * 1024:  # 64 MiB 安全检查上限
        raise LSPProtocolError(f"unreasonable Content-Length: {n}")

    try:
        body = await reader.readexactly(n)
    except asyncio.IncompleteReadError as e:
        raise LSPProtocolError(
            f"truncated LSP body: expected {n} bytes, got {len(e.partial)}"
        ) from e

    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise LSPProtocolError(f"invalid JSON in LSP body: {e}") from e
    except UnicodeDecodeError as e:
        raise LSPProtocolError(f"non-UTF-8 LSP body: {e}") from e


def make_request(req_id: int, method: str, params: Any) -> dict:
    """构建 JSON-RPC 2.0 请求信封。"""
    msg: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def make_notification(method: str, params: Any) -> dict:
    """构建 JSON-RPC 2.0 通知信封（无 ``id``）。"""
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def make_response(req_id: Any, result: Any) -> dict:
    """构建 JSON-RPC 2.0 成功响应信封。"""
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def make_error_response(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    """构建 JSON-RPC 2.0 错误响应信封。"""
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def classify_message(msg: dict) -> Tuple[str, Any]:
    """返回 ``(kind, key)``，其中 kind 为 ``request``、
    ``response``、``notification``、``invalid`` 之一。

    对于 request/response，key 为请求 id；对于 notification，
    key 为方法名；对于 invalid 消息，key 为 ``None``。
    """
    if not isinstance(msg, dict):
        return "invalid", None
    if msg.get("jsonrpc") != "2.0":
        return "invalid", None
    has_id = "id" in msg
    has_method = "method" in msg
    if has_id and has_method:
        return "request", msg["id"]
    if has_id and ("result" in msg or "error" in msg):
        return "response", msg["id"]
    if has_method and not has_id:
        return "notification", msg["method"]
    return "invalid", None


__all__ = [
    "ERROR_CONTENT_MODIFIED",
    "ERROR_REQUEST_CANCELLED",
    "ERROR_METHOD_NOT_FOUND",
    "LSPProtocolError",
    "LSPRequestError",
    "encode_message",
    "read_message",
    "make_request",
    "make_notification",
    "make_response",
    "make_error_response",
    "classify_message",
]
