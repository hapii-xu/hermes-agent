"""gateway ↔ node RPC 的线路协议。

所有内容都是具有相同信封形状的 JSON 对象：

    请求：  {"type": <str>, "id": <str>, "token": <str>, "payload": <dict>}
    响应：  {"type": "<req-type>_res", "id": <req-id>, "payload": <dict>}
    错误：  {"type": "error", "id": <req-id>, "error": <str>}

请求必须携带共享 bearer token（通过
``hermes meet node approve`` 在网关上设置，并由服务端从磁盘读取）。
token 不匹配将在分发前被拒绝。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Tuple


VALID_REQUEST_TYPES = frozenset({
    "start_bot",
    "stop",
    "status",
    "transcript",
    "say",
    "ping",
})


def make_request(
    type: str,
    token: str,
    payload: Dict[str, Any],
    req_id: str | None = None,
) -> Dict[str, Any]:
    """构造一个请求信封。

    当未提供 ``req_id`` 时自动生成（uuid4 hex），以便调用方
    可以关联异步响应。
    """
    if not isinstance(type, str) or not type:
        raise ValueError("type must be a non-empty string")
    if type not in VALID_REQUEST_TYPES:
        raise ValueError(f"unknown request type: {type!r}")
    if not isinstance(token, str):
        raise ValueError("token must be a string")
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    return {
        "type": type,
        "id": req_id or uuid.uuid4().hex,
        "token": token,
        "payload": payload,
    }


def make_response(req_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """构建一个成功响应。调用方提供 *请求* 类型；
    我们在其后附加 ``_res``，以便客户端可以断言他们收到了正确的
    回复。

    为简单起见，此处我们不需要类型 — 客户端通常只
    依赖 ``id``。但我们仍然发出通用的 ``*_res`` 信封。
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")
    return {"type": "response", "id": req_id, "payload": payload}


def make_error(req_id: str, error: str) -> Dict[str, Any]:
    return {"type": "error", "id": req_id, "error": str(error)}


def encode(msg: Dict[str, Any]) -> str:
    """将消息信封序列化为 JSON 字符串。"""
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False)


def decode(raw: str) -> Dict[str, Any]:
    """解析 JSON 信封，对任何格式错误的内容抛出 ValueError。

    最小类型验证：必须是对象，必须包含 ``type`` 和
    ``id``。更重的验证（token 匹配、payload 形状）在
    服务端的 :func:`validate_request` 中进行。
    """
    try:
        obj = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("envelope must be a JSON object")
    if "type" not in obj or not isinstance(obj["type"], str):
        raise ValueError("envelope missing string 'type'")
    if "id" not in obj or not isinstance(obj["id"], str):
        raise ValueError("envelope missing string 'id'")
    return obj


def validate_request(msg: Dict[str, Any], expected_token: str) -> Tuple[bool, str]:
    """根据服务端的共享 token 检查已解码的请求。

    当信封可接受时返回 ``(True, "")``，
    否则返回 ``(False, <reason>)``。Reason 字符串可以安全地
    在错误信封中返回给客户端。
    """
    if not isinstance(msg, dict):
        return False, "envelope must be a dict"
    t = msg.get("type")
    if not isinstance(t, str) or not t:
        return False, "missing or non-string 'type'"
    if t not in VALID_REQUEST_TYPES:
        return False, f"unknown request type: {t!r}"
    if not isinstance(msg.get("id"), str) or not msg.get("id"):
        return False, "missing or non-string 'id'"
    token = msg.get("token")
    if not isinstance(token, str) or not token:
        return False, "missing token"
    if token != expected_token:
        return False, "token mismatch"
    payload = msg.get("payload")
    if not isinstance(payload, dict):
        return False, "payload must be a dict"
    return True, ""
