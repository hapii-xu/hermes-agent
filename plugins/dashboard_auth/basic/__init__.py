"""BasicAuthProvider — 用户名/密码仪表板认证（无 OAuth IDP）。

一个自托管的"为仪表板设置密码"provider。它插入到与 Nous OAuth
provider 相同的 ``DashboardAuthProvider`` 框架中，但使用用户名 + 密码
而非 OAuth 重定向进行认证：设置 ``supports_password = True`` 并实现
``complete_password_login``。登录页面为其渲染凭据表单；
登录后的一切（会话 cookie、验证、刷新、ws-ticket、注销）与 OAuth
路径相同，因为密码会话只是一个带有 provider 生成的不透明 token 的
:class:`Session`。

此 provider **没有外部 IDP，也没有数据库**。凭据预先配置；
会话是由此 provider 生成和验证的无状态 HMAC 签名 token。
这使其保持零基础设施 — 适合单机自托管仪表板。

Configuration surfaces (env wins over config.yaml when set non-empty),
mirroring the Nous provider's precedence convention:

  ``config.yaml`` — canonical surface::

      dashboard:
        basic_auth:
          username: admin               # required
          # Provide EITHER a precomputed scrypt hash (preferred — no
          # plaintext at rest) ...
          password_hash: "scrypt$..."   # see hash_password()
          # ... OR a plaintext password (hashed in-memory at load).
          password: "s3cret"
          secret: "<32+ random bytes, base64 or hex>"  # optional; token-signing key
          session_ttl_seconds: 43200    # optional; access-token lifetime (default 12h)

  Environment overrides::

      HERMES_DASHBOARD_BASIC_AUTH_USERNAME
      HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH   # preferred
      HERMES_DASHBOARD_BASIC_AUTH_PASSWORD        # plaintext fallback
      HERMES_DASHBOARD_BASIC_AUTH_SECRET
      HERMES_DASHBOARD_BASIC_AUTH_TTL_SECONDS

如果未配置 ``secret``，则在启动时生成一个随机的每进程密钥。
这对单进程仪表板没问题，但意味着所有会话在重启时失效，
且会话不能跨多个工作进程存活 — 为稳定的多工作进程/重启存活会话
设置显式 ``secret``。

密码哈希使用标准库 :func:`hashlib.scrypt`（内存密集型，无第三方依赖）。
``complete_password_login`` 运行恒定时间比较，即使对未知用户名也始终
执行哈希，因此端点不是用户名枚举的定时预言机。

跳过原因：
  与 Nous provider 类似，此模块暴露了一个模块级 ``LAST_SKIP_REASON``，
  当插件加载但拒绝注册时（未配置用户名/密码），网关的失败关闭分支
  可以显示该原因。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Optional

from hermes_cli.dashboard_auth import (
    DashboardAuthProvider,
    InvalidCredentialsError,
    LoginStart,
    RefreshExpiredError,
    Session,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 默认值
# ---------------------------------------------------------------------------

# access token 生命周期。当 access token 过期时，中间件通过
# refresh token（30 天）透明刷新，因此这控制刷新往返发生的频率，
# 而非用户保持登录的时长。
_DEFAULT_TTL_SECONDS = 12 * 60 * 60  # 12小时
_REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60  # 30天

# scrypt 参数（RFC 7914 / 标准库 hashlib.scrypt）。n 必须是 2 的幂；
# 这些是广泛推荐的交互式登录参数（约 16 MiB，商用硬件上几毫秒）。
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_SALT_BYTES = 16

# HMAC-SHA256 摘要的长度，作为固定长度后缀附加到已签名的 token
# （无分隔符 — 二进制 HMAC 字节不会与分隔符混淆）。
_SIG_LEN = hashlib.sha256().digest_size


LAST_SKIP_REASON: str = ""


# ---------------------------------------------------------------------------
# 密码哈希（标准库 scrypt）
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """返回 ``scrypt$n$r$p$<salt_b64>$<dk_b64>`` 哈希字符串。

    使用此函数为 config.yaml 预计算 ``password_hash``，使明文
    永不静态存储。作为模块函数暴露，供操作员运行
    ``python -c "from plugins.dashboard_auth.basic import hash_password;
    print(hash_password('pw'))"``.
    """
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=0,
    )
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}$"
        f"{base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"
    )


def _verify_password(password: str, encoded: str) -> bool:
    """恒定时间 scrypt 验证。任何格式错误的哈希字符串返回 False。"""
    try:
        scheme, n_s, r_s, p_s, salt_b64, dk_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
    except (ValueError, TypeError):
        return False
    try:
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=0,
        )
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


# 当用户名未知时用于花费约等时间的固定虚拟哈希，
# 使攻击者无法通过计时区分"无此用户"（快速）和
# "密码错误"（慢速 scrypt）。在导入时计算一次。
_DUMMY_HASH = hash_password("dummy-password-for-constant-time-verify")


# ---------------------------------------------------------------------------
# Token 签名（无状态 HMAC 签名数据块）
# ---------------------------------------------------------------------------


def _sign(payload: dict, secret: bytes) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(secret, raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + sig).decode()


def _unsign(token: str, secret: bytes) -> Optional[dict]:
    try:
        blob = base64.urlsafe_b64decode(token.encode())
        if len(blob) <= _SIG_LEN:
            return None
        raw, sig = blob[:-_SIG_LEN], blob[-_SIG_LEN:]
        expected = hmac.new(secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        return json.loads(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class BasicAuthProvider(DashboardAuthProvider):
    """使用无状态 HMAC 签名会话的用户名/密码 provider。"""

    name = "basic"
    display_name = "Username & Password"
    supports_password = True

    def __init__(
        self,
        *,
        username: str,
        password_hash: str,
        secret: bytes,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        if not username:
            raise ValueError("username must be non-empty")
        if not password_hash:
            raise ValueError("password_hash must be non-empty")
        if len(secret) < 16:
            raise ValueError("secret must be at least 16 bytes")
        self._username = username
        self._password_hash = password_hash
        self._secret = secret
        self._ttl = max(60, int(ttl_seconds))

    # ---- OAuth 方法：未使用（纯密码 provider）------------------

    def start_login(self, *, redirect_uri: str) -> LoginStart:
        raise NotImplementedError(
            "BasicAuthProvider is password-only; there is no OAuth redirect "
            "flow. The login page POSTs to /auth/password-login instead."
        )

    def complete_login(
        self, *, code: str, state: str, code_verifier: str, redirect_uri: str
    ) -> Session:
        raise NotImplementedError(
            "BasicAuthProvider is password-only; use complete_password_login."
        )

    # ---- 密码登录 ----------------------------------------------------

    def complete_password_login(
        self, *, username: str, password: str
    ) -> Session:
        # 近似恒定时间：始终运行 scrypt 验证（若用户名匹配则使用真实
        # 哈希，否则使用虚拟哈希），使未知用户名和错误密码花费相近时间。
        # 也使用 compare_digest 比较用户名，避免用户名本身的
        # 长度/字节计时泄漏。
        username_ok = hmac.compare_digest(
            username.encode("utf-8"), self._username.encode("utf-8")
        )
        target_hash = self._password_hash if username_ok else _DUMMY_HASH
        password_ok = _verify_password(password, target_hash)
        if not (username_ok and password_ok):
            raise InvalidCredentialsError("invalid username or password")
        return self._mint_session(self._username)

    # ---- 会话生命周期 -------------------------------------------------

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        payload = _unsign(access_token, self._secret)
        if (
            payload is None
            or payload.get("kind") != "access"
            or payload.get("exp", 0) <= int(time.time())
        ):
            return None
        return self._session_from_payload(access_token, "", payload)

    def refresh_session(self, *, refresh_token: str) -> Session:
        if not refresh_token:
            raise RefreshExpiredError("no refresh token present in session")
        payload = _unsign(refresh_token, self._secret)
        if (
            payload is None
            or payload.get("kind") != "refresh"
            or payload.get("exp", 0) <= int(time.time())
        ):
            raise RefreshExpiredError("refresh token expired or invalid")
        return self._mint_session(str(payload.get("sub", self._username)))

    def revoke_session(self, *, refresh_token: str) -> None:
        # 无状态 token — 服务器端无需撤销。会话在其 TTL 内过期。
        # 尽力而为的空操作，不得抛出异常。
        _ = refresh_token
        return None

    # ---- 内部方法 ---------------------------------------------------------

    def _mint_session(self, user_id: str) -> Session:
        now = int(time.time())
        exp = now + self._ttl
        access_token = _sign(
            {"sub": user_id, "kind": "access", "exp": exp}, self._secret
        )
        refresh_token = _sign(
            {"sub": user_id, "kind": "refresh", "exp": now + _REFRESH_TTL_SECONDS},
            self._secret,
        )
        return Session(
            user_id=user_id,
            email="",
            display_name=user_id,
            org_id="",
            provider=self.name,
            expires_at=exp,
            access_token=access_token,
            refresh_token=refresh_token,
        )

    def _session_from_payload(
        self, access_token: str, refresh_token: str, payload: dict
    ) -> Session:
        user_id = str(payload.get("sub", ""))
        return Session(
            user_id=user_id,
            email="",
            display_name=user_id,
            org_id="",
            provider=self.name,
            expires_at=int(payload["exp"]),
            access_token=access_token,
            refresh_token=refresh_token,
        )


# ---------------------------------------------------------------------------
# 插件入口点
# ---------------------------------------------------------------------------


def _load_config_basic_auth_section() -> dict:
    """从 config.yaml 返回 ``dashboard.basic_auth``，或 ``{}``。

    对 load_config() 抛出异常、键缺失或值不是字典等情况具有鲁棒性
    — 每种情况都回退到 ``{}``。
    """
    try:
        from hermes_cli.config import cfg_get, load_config

        cfg = load_config()
    except Exception as exc:  # noqa: BLE001 — broad catch is intentional
        logger.debug(
            "dashboard-auth-basic: load_config() raised %s; "
            "falling back to env-only configuration",
            exc,
        )
        return {}
    section = cfg_get(cfg, "dashboard", "basic_auth", default=None)
    return section if isinstance(section, dict) else {}


def _resolve(env_name: str, cfg_section: dict, cfg_key: str) -> str:
    """环境变量优先于配置的解析；空环境变量视为未设置。"""
    env = os.environ.get(env_name, "").strip()
    if env:
        return env
    return str(cfg_section.get(cfg_key, "") or "").strip()


def _resolve_secret(cfg_section: dict) -> bytes:
    """解析 token 签名密钥。

    接受来自配置/环境的 base64、hex 或原始文本。未设置时，
    生成随机的每进程密钥（会话则不能在重启后存活或跨多个工作进程
    — 在 INFO 级别记录）。
    """
    raw = _resolve(
        "HERMES_DASHBOARD_BASIC_AUTH_SECRET", cfg_section, "secret"
    )
    if not raw:
        logger.info(
            "dashboard-auth-basic: no 'secret' configured; generating a "
            "random per-process signing key. Sessions will not survive a "
            "restart or span multiple workers. Set dashboard.basic_auth."
            "secret (or HERMES_DASHBOARD_BASIC_AUTH_SECRET) for stable "
            "sessions."
        )
        return secrets.token_bytes(32)
    # 先尝试 base64，再尝试 hex，最后回退到原始 UTF-8 字节。
    for decoder in (base64.b64decode, bytes.fromhex):
        try:
            decoded = decoder(raw)
            if len(decoded) >= 16:
                return decoded
        except (ValueError, TypeError):
            pass
    return raw.encode("utf-8")


def register(ctx) -> None:
    """插件入口 — 当凭据存在时注册 BasicAuthProvider。

    回环 / ``--insecure`` 操作员和使用 OAuth provider 的人
    不设置 ``dashboard.basic_auth``，因此此插件对他们是空操作。
    当配置了用户名 + （密码或 password_hash）时，注册一个密码 provider，
    登录页面将其渲染为凭据表单。
    """
    global LAST_SKIP_REASON
    LAST_SKIP_REASON = ""

    section = _load_config_basic_auth_section()
    username = _resolve(
        "HERMES_DASHBOARD_BASIC_AUTH_USERNAME", section, "username"
    )
    password_hash = _resolve(
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH", section, "password_hash"
    )
    plaintext = _resolve(
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", section, "password"
    )
    ttl_raw = _resolve(
        "HERMES_DASHBOARD_BASIC_AUTH_TTL_SECONDS", section, "session_ttl_seconds"
    )

    if not username:
        LAST_SKIP_REASON = (
            "dashboard.basic_auth.username is not set (and "
            "HERMES_DASHBOARD_BASIC_AUTH_USERNAME is empty). Set a username "
            "and a password (or password_hash) under dashboard.basic_auth in "
            "config.yaml to enable username/password dashboard login, or use "
            "the OAuth provider, or pass --insecure to skip the auth gate."
        )
        logger.debug("dashboard-auth-basic: %s", LAST_SKIP_REASON)
        return

    if not password_hash and not plaintext:
        LAST_SKIP_REASON = (
            "dashboard.basic_auth.username is set but neither password_hash "
            "nor password is configured. Provide one of them (password_hash "
            "is preferred — compute it with "
            "plugins.dashboard_auth.basic.hash_password)."
        )
        logger.warning("dashboard-auth-basic: %s", LAST_SKIP_REASON)
        return

    # 优先级（环境变量优先约定）：通过 HERMES_DASHBOARD_BASIC_AUTH_PASSWORD
    # 环境变量提供的密码会覆盖 config.yaml 中的 password_hash，
    # 因此操作员可以通过设置环境变量而不编辑配置来轮换密码。
    # 预计算的 password_hash 在同层优先于仅配置的明文密码
    # — 它是首选的静态存储形式。具体来说：
    #   * 环境变量密码已设置       → 哈希它（覆盖任何配置哈希）
    #   * 否则配置 password_hash 已设置 → 使用它
    #   * 否则配置明文密码          → 在内存中哈希它
    plaintext_from_env = os.environ.get(
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", ""
    ).strip()
    if plaintext_from_env:
        password_hash = hash_password(plaintext_from_env)
        logger.info(
            "dashboard-auth-basic: hashed env-supplied password in-memory "
            "(overrides any config password_hash)."
        )
    elif not password_hash:
        # config-only plaintext password.
        password_hash = hash_password(plaintext)
        logger.info(
            "dashboard-auth-basic: hashed plaintext password in-memory. "
            "For production, precompute dashboard.basic_auth.password_hash "
            "and remove the plaintext password from config."
        )

    secret = _resolve_secret(section)

    try:
        ttl = int(ttl_raw) if ttl_raw else _DEFAULT_TTL_SECONDS
    except ValueError:
        ttl = _DEFAULT_TTL_SECONDS

    try:
        provider = BasicAuthProvider(
            username=username,
            password_hash=password_hash,
            secret=secret,
            ttl_seconds=ttl,
        )
    except ValueError as exc:
        LAST_SKIP_REASON = f"BasicAuthProvider construction failed: {exc}"
        logger.warning("dashboard-auth-basic: %s", LAST_SKIP_REASON)
        return

    ctx.register_dashboard_auth_provider(provider)
    logger.info(
        "dashboard-auth-basic: registered password provider (username=%s)",
        username,
    )
