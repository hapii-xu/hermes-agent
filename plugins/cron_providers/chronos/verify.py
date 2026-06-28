"""Chronos 的入站 cron-fire token 验证（阶段 4E.1）。

当 NAS 将外部调度器触发中继到 agent 时，它通过 POST 请求
``/api/cron/fire`` 携带一个短期有效的 NAS 生成的 JWT。此模块在
任何任务运行前验证该 JWT — 这是远程触发任务执行的安全边界。

我们验证 NAS 生成的 JWT（agent 已信任的路径），而不是让外部调度器直接
调用 agent：调度器使用 NAS 的密钥签名，agent 不持有（也不应持有）这些密钥。
参见计划的 DQ-4。

验证器是可插拔的（``get_fire_verifier``），因此逃生舱模式
（每个任务的直接 cron-key）可以在不更改处理器的情况下替换进来。

加密委托给 PyJWT（已声明的依赖项）— 我们不手动实现 JWT 验证。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("cron.chronos.verify")

# 将 token 限定到 fire 端点的用途声明。通用 agent
# JWT（不含此声明）不得重放到 /api/cron/fire。
_FIRE_PURPOSE = "cron_fire"


def verify_nas_fire_token(
    *,
    token: str,
    expected_audience: str,
    jwks_or_key: Optional[str] = None,
    issuer: Optional[str] = None,
    leeway_seconds: int = 30,
) -> Optional[Dict[str, Any]]:
    """验证 NAS 生成的 cron-fire JWT。返回解码后的声明，或 None。

    检查（全部必须通过）：
      - 与 NAS JWKS 的签名验证（``jwks_or_key`` 是 JWKS URL）— RS256
        系列；对称密钥被拒绝（NAS 使用非对称签名）。
      - ``aud`` == ``expected_audience``（此 agent：``agent:{instance_id}``）。
      - ``exp`` / ``nbf`` 在 ``leeway_seconds`` 范围内。
      - 配置了 issuer 时 ``iss`` == ``issuer``。
      - ``purpose`` == ``"cron_fire"`` — 防止通用 agent JWT 被
        重放到 fire 端点。

    任何失败时返回 None（永不抛出异常），使处理器可以回复 401
    而不泄露哪项检查失败。
    """
    if not token or not expected_audience:
        return None
    if not jwks_or_key:
        # 未配置验证密钥 → 无法验证 → 拒绝。对于安全边界，
        # 我们永不回退到无签名解码。
        logger.warning("cron fire: no JWKS/key configured; refusing token")
        return None

    try:
        import jwt
        from jwt import PyJWKClient

        # 通过 token 的 kid 从 JWKS 端点解析签名密钥。
        signing_key = None
        if jwks_or_key.startswith("http://") or jwks_or_key.startswith("https://"):
            jwk_client = PyJWKClient(jwks_or_key)
            signing_key = jwk_client.get_signing_key_from_jwt(token).key
        else:
            # 内联传入的 PEM 公钥（测试 / 固定密钥部署）。
            signing_key = jwks_or_key

        options = {"require": ["exp", "aud"]}
        decode_kwargs: Dict[str, Any] = dict(
            algorithms=["RS256", "RS384", "RS512", "ES256", "ES384"],
            audience=expected_audience,
            leeway=leeway_seconds,
            options=options,
        )
        if issuer:
            decode_kwargs["issuer"] = issuer

        claims = jwt.decode(token, signing_key, **decode_kwargs)
    except Exception as e:
        logger.warning("cron fire: token verification failed: %s", e)
        return None

    if claims.get("purpose") != _FIRE_PURPOSE:
        logger.warning("cron fire: token missing/!=%s purpose claim", _FIRE_PURPOSE)
        return None

    return claims


def get_fire_verifier() -> Callable[..., Optional[Dict[str, Any]]]:
    """返回当前活跃的入站触发验证器。

    默认 = NAS-JWT 验证器。DQ-4 逃生舱（每个任务的直接 cron-key）
    将在此处返回 cron-key 验证器，由配置选择
    — 因此当认证模式切换时 webhook 处理器无需更改。
    """
    return verify_nas_fire_token
