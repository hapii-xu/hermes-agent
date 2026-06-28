"""Gateway 侧的 relay 认证原语。EXPERIMENTAL。

connector⇄gateway 通道需要鉴权，因为 gateway 可能由客户管理并暴露在公网（见
connector 仓库的 ``docs/connector-gateway-auth-design.md``）。本模块是两套 HMAC 方案的
**gateway 一半**，其线上字节必须与 connector 的 TypeScript 完全一致：

1. **WS upgrade 鉴权**（gateway → connector）：gateway 在 ``/relay`` WebSocket
   upgrade 时附带 ``Authorization: Bearer <token>``，其中
   ``token = make_upgrade_token(gateway_id, secret)``。镜像 connector 的
   ``relayAuthToken.ts`` 中的 ``makeToken``（``src/core/relayAuthToken.ts``）：
   ``base64url(f"{payload}:{exp}:{sig}")``，其中
   ``sig = HMAC_SHA256(f"{payload}:{exp}", secret).hexdigest()``，且
   ``payload == gateway_id``。

2. **inbound delivery 签名**（connector → gateway）：connector 用 per-tenant 的
   *delivery key* 为每个 inbound POST 签名，通过 ``x-relay-timestamp`` +
   ``x-relay-signature`` 头携带；gateway 在接受事件前先校验。镜像 connector 的
   ``deliverySigning.ts``：对精确的请求 body 字节计算
   ``sig = HMAC_SHA256(f"{ts}.{body_json}", key).hexdigest()``，并做 replay-window
   偏移检查。

两套方案都使用**多 secret 验证列表**（主 secret 在前，轮转窗口期内接一个副 secret），
与 ``api/src/handlers/stats_oauth.ts`` 完全一致——这样 secret 轮转不会使已签发的
token 失效。

EXPERIMENTAL：在 ≥2 个 Class-1 平台验证该 relay 契约之前，可能不经 deprecation
周期而变更。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Optional, Sequence

# connector 用于 inbound delivery 签名的 header 名
#（connector 的 ``src/core/deliverySigning.ts`` —— DELIVERY_TS_HEADER / SIG_HEADER）。
DELIVERY_TS_HEADER = "x-relay-timestamp"
DELIVERY_SIG_HEADER = "x-relay-signature"

# inbound delivery 签名的默认 replay 窗口（connector 默认值）。
_DEFAULT_MAX_SKEW_SECONDS = 300
# upgrade token 的默认 TTL（connector ``makeUpgradeToken`` 的默认值）。
_DEFAULT_UPGRADE_TTL_SECONDS = 300


def _hmac_hex(payload: str, secret: str) -> str:
    """对 ``payload`` 在 ``secret``（UTF-8）下计算 HMAC-SHA256 的十六进制摘要。"""
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def sign(payload: str, secret: str) -> str:
    """HMAC-SHA256 的十六进制摘要 —— 即 connector 的 ``sign``（relayAuthToken.ts）。"""
    return _hmac_hex(payload, secret)


def verify_signature(payload: str, sig_hex: str, secrets: Sequence[str]) -> bool:
    """以常数时间检查 ``sig_hex`` 是否是 ``payload`` 在 ``secrets`` 中任一 secret
    （轮转窗口）下的合法 HMAC。长度不匹配的候选项会被跳过且不泄露时序信息。镜像
    ``verifySignature``。
    """
    try:
        sig_buf = bytes.fromhex(sig_hex)
    except (ValueError, TypeError):
        return False
    if len(sig_buf) == 0:
        return False
    for secret in secrets:
        if not secret:
            continue
        expected = bytes.fromhex(_hmac_hex(payload, secret))
        if len(expected) != len(sig_buf):
            continue
        if hmac.compare_digest(sig_buf, expected):
            return True
    return False


def make_token(payload: str, secret: str, ttl_seconds: int = 0) -> str:
    """构建一个已签名、可选过期的 token —— 即 connector 的 ``makeToken``。

    ``base64url(f"{payload}:{exp}:{sig}")``，其中 ``exp`` 是 unix 秒级过期时间
    （0 = 永不过期），``sig = HMAC_SHA256(f"{payload}:{exp}", secret)``。
    base64url 不带 padding，以匹配 Node 的 ``Buffer.toString("base64url")``。
    """
    exp = int(time.time()) + ttl_seconds if ttl_seconds > 0 else 0
    signed = f"{payload}:{exp}"
    sig = _hmac_hex(signed, secret)
    raw = f"{signed}:{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_upgrade_token(
    gateway_id: str, secret: str, ttl_seconds: int = _DEFAULT_UPGRADE_TTL_SECONDS
) -> str:
    """gateway 发送的 WS-upgrade bearer token：``payload = gateway_id``。

    connector 先读取 ``gateway_id``（payload 头部）以索引其 secret 验证列表，再用该
    gateway 存储的 secret(s) 校验签名。镜像 connector 的 ``makeUpgradeToken``。
    """
    return make_token(gateway_id, secret, ttl_seconds)


def verify_token(token: str, secrets: Sequence[str]) -> Optional[str]:
    """校验由 ``make_token`` 构建的 token；返回 payload 或 None。

    从右侧拆分，以便 payload 自身可以包含冒号（镜像 connector 的
    ``verifyToken``）。会拒绝已过期的 token 以及签名不匹配验证列表中任何 secret 的
    token。
    """
    try:
        # base64url 解码，并补齐 padding。
        padded = token + "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, TypeError):
        return None
    parts = decoded.split(":")
    if len(parts) < 3:
        return None
    sig = parts[-1]
    try:
        exp = int(parts[-2])
    except ValueError:
        return None
    payload = ":".join(parts[:-2])
    if exp != 0 and int(time.time()) > exp:
        return None
    signed = f"{payload}:{exp}"
    return payload if verify_signature(signed, sig, secrets) else None


def _delivery_payload(ts: int, body_json: str) -> str:
    """inbound delivery 的签名材料：``f"{ts}.{body_json}"``。"""
    return f"{ts}.{body_json}"


def verify_delivery_signature(
    body_json: str,
    timestamp: Optional[str],
    signature: Optional[str],
    verify_keys: Sequence[str],
    max_skew_seconds: int = _DEFAULT_MAX_SKEW_SECONDS,
    *,
    now: Optional[int] = None,
) -> bool:
    """校验一个 connector→gateway 的 inbound delivery 签名。

    ``body_json`` 必须是按 UTF-8 解码的精确请求 body 字节——connector 对字面序列化
    的 body 签名，因此 gateway 对字面接收到的 body 做校验（不做重新序列化）。检查
    时间戳与 now 的偏差在 ``max_skew_seconds`` 之内，且 HMAC 匹配轮转验证列表中的任一
    key。镜像 connector 的 ``verifyDeliverySignature``。
    """
    if not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False
    current = now if now is not None else int(time.time())
    if abs(current - ts) > max_skew_seconds:
        return False
    return verify_signature(_delivery_payload(ts, body_json), signature, verify_keys)
