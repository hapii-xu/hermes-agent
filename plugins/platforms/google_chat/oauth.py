"""Google Chat 网关适配器的用户 OAuth 辅助模块。

Google Chat 的 ``media.upload`` REST 端点会硬性拒绝 service account
身份验证：

    "This method doesn't support app authentication with a service
     account. Authenticate with a user account."

（参见 https://developers.google.com/workspace/chat/api/reference/rest/v1/media/upload
和 https://developers.google.com/chat/api/guides/auth/users。）

为了让 bot 能够发送原生文件附件——即用户手动上传时看到的相同拖放
文件小部件——每个用户必须在自己的 DM 中一次性授予 bot
``chat.messages.create`` 权限。Bot 会存储每个用户的 refresh token，
并在需要发送文件时以*请求该文件的用户*身份调用 ``media.upload`` 及
随后的 ``messages.create``。

本模块既是 CLI 工具（由 agent 通过斜杠命令或终端命令驱动），
也是 ``google_chat.py`` 导入的库：

    库函数（运行时由适配器调用）：
        load_user_credentials(email=None) -> Credentials | None
        refresh_or_none(creds, email=None) -> Credentials | None
        build_user_chat_service(creds) -> chat_v1.Resource
        list_authorized_emails() -> List[str]

    CLI 命令（由 agent 通过 /setup-files 斜杠命令驱动，
    模仿 skills/productivity/google-workspace/scripts/setup.py）：
        --check                          如果 auth 有效则退出码为 0，否则为 1
        --client-secret /path/to.json    持久化 OAuth client 凭据
        --auth-url                       打印供用户访问的 OAuth URL
        --auth-code CODE                 用 auth code 换取 token
        --revoke                         撤销并删除已存储的 token
        --install-deps                   安装 Python 依赖
        --email EMAIL                    将 CLI 操作限定到特定用户
                                         （省略时退回到旧版单用户模式）

该流程与现有的 google-workspace skill 完全一致，因此熟悉该流程的
人可以无障碍地理解本模块。

Token 存储布局
--------------------
- 按用户存储的 token（以发送者 email 为键）：
    ``${HERMES_HOME}/google_chat_user_tokens/<sanitized_email>.json``
- 旧版单用户 token（向后兼容的后备，不做改动）：
    ``${HERMES_HOME}/google_chat_user_token.json``
- /setup-files 开始到 exchange 期间按用户存储的待处理 OAuth state：
    ``${HERMES_HOME}/google_chat_user_oauth_pending/<sanitized_email>.json``
- 旧版待处理 state：
    ``${HERMES_HOME}/google_chat_user_oauth_pending.json``
- OAuth client secret（按 profile 隔离——每个 profile 注册自己的）：
    ``${HERMES_HOME}/google_chat_user_client_secret.json``
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

# 固定旧版 logger 名称，以便在从项目内代码迁移到 plugin 后，
# 运维侧的日志过滤器仍能继续匹配。参见 adapter.py 了解上下文。
logger = logging.getLogger("gateway.platforms.google_chat_user_oauth")

# 使用项目的 HERMES_HOME 辅助函数，以便 token 跟随用户的 profile
# （例如测试可以通过 HERMES_HOME=/tmp/... 进行覆盖）。
try:
    from hermes_constants import display_hermes_home, get_hermes_home
except (ModuleNotFoundError, ImportError):
    # 当 hermes_constants 无法导入时的后备方案
    # （与 google-workspace skill 的 _hermes_home.py shim
    # 使用的后备方案相同）。
    def get_hermes_home() -> Path:
        val = os.environ.get("HERMES_HOME", "").strip()
        return Path(val) if val else Path.home() / ".hermes"

    def display_hermes_home() -> str:
        home = get_hermes_home()
        try:
            return "~/" + str(home.relative_to(Path.home()))
        except ValueError:
            return str(home)

from utils import atomic_replace


def _hermes_home() -> Path:
    """在调用时解析 HERMES_HOME（而非模块导入时）。

    测试和 ``HERMES_HOME=...`` 环境变量覆盖需要延迟绑定。
    如果在导入时缓存路径，切换 profile 或在测试中调整环境变量
    将会悄悄继续使用旧路径。"""
    return get_hermes_home()


# 文件系统安全的键：小写，允许 ``[a-z0-9._-@]``，其他字符替换为 ``_``。
# ``ramon.fernandez@nttdata.com`` 保持人类可读
# （``ramon.fernandez@nttdata.com.json``），使管理员通过
# ``ls ~/.hermes/google_chat_user_tokens/`` 调试变得简单。
_EMAIL_FS_RE = re.compile(r"[^a-z0-9._@-]+")


def _sanitize_email(email: str) -> str:
    cleaned = _EMAIL_FS_RE.sub("_", (email or "").strip().lower())
    return cleaned or "_unknown_"


def _legacy_token_path() -> Path:
    return _hermes_home() / "google_chat_user_token.json"


def _user_tokens_dir() -> Path:
    return _hermes_home() / "google_chat_user_tokens"


def _legacy_pending_path() -> Path:
    return _hermes_home() / "google_chat_user_oauth_pending.json"


def _user_pending_dir() -> Path:
    return _hermes_home() / "google_chat_user_oauth_pending"


def _token_path(email: Optional[str] = None) -> Path:
    """返回 ``email`` 对应的磁盘 token 路径，若无则返回旧版路径。"""
    if email:
        return _user_tokens_dir() / f"{_sanitize_email(email)}.json"
    return _legacy_token_path()


def _client_secret_path() -> Path:
    return _hermes_home() / "google_chat_user_client_secret.json"


def _pending_auth_path(email: Optional[str] = None) -> Path:
    if email:
        return _user_pending_dir() / f"{_sanitize_email(email)}.json"
    return _legacy_pending_path()


# 原生 Chat 附件投递所需的最小 scope。
# `chat.messages.create` 同时覆盖 `media.upload` 和随后引用
# attachmentDataRef 的 `messages.create`。我们刻意不请求
# drive.file 或其他 scope——最小权限原则。
SCOPES: List[str] = [
    "https://www.googleapis.com/auth/chat.messages.create",
]

# OAuth 流程所需的 pip 包。
_REQUIRED_PACKAGES = [
    "google-api-python-client",
    "google-auth-oauthlib",
    "google-auth-httplib2",
]

# 带外重定向：Google 已弃用 ``urn:ietf:wg:oauth:2.0:oob``
# 流程，因此我们使用预期会失败的 localhost 重定向。用户
# 从失败的浏览器 URL 栏中复制 auth code 并粘贴回聊天。
# skills/productivity/google-workspace/scripts/setup.py 也使用了相同技巧。
_REDIRECT_URI = "http://localhost:1"


# =============================================================================
# 库 API — 运行时由适配器调用
# =============================================================================


def load_user_credentials(email: Optional[str] = None) -> Optional[Any]:
    """加载并验证已持久化的用户 OAuth 凭据。

    ``email`` 选择按用户的 token 文件；``None`` 回退到旧版单用户路径
    （保留用于运行过 pre-multi-user 流程的安装）。返回一个可直接使用的
    ``google.oauth2.credentials.Credentials`` 实例，如果没有存储 token、
    token 损坏或 refresh 失败则返回 ``None``。适配器调用方应将 ``None``
    视为"用户尚未运行 /setup-files"并向用户显示 setup 指引后备方案。

    在无 token 的情况下不会抛出异常——这是预期行为。
    """
    token_path = _token_path(email)
    if not token_path.exists():
        return None

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except ImportError:
        logger.warning(
            "[google_chat_user_oauth] google-auth not installed; user-OAuth "
            "attachment delivery is disabled. Install hermes-agent[google_chat]."
        )
        return None

    try:
        # 不传递 scopes——用户可能只授权了子集，
        # 传递 scopes 会使 refresh 严格验证它们。与 google-workspace skill 的逻辑相同。
        creds = Credentials.from_authorized_user_file(str(token_path))
    except Exception as exc:
        logger.warning(
            "[google_chat_user_oauth] token at %s is corrupt: %s",
            token_path, exc,
        )
        return None

    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            logger.warning(
                "[google_chat_user_oauth] token refresh failed (user "
                "should re-run /setup-files): %s", exc,
            )
            return None
        # 持久化刷新后的 token，以便下次启动时直接使用新的 access
        # token，而无需多余的 refresh 往返。
        _persist_credentials(creds, token_path)
        return creds

    # Token 存在但不可用（例如已被撤销，或无 refresh token）。
    return None


def refresh_or_none(creds: Any, email: Optional[str] = None) -> Optional[Any]:
    """如果 ``creds`` 已过期则进行 refresh。返回凭据或 ``None``。

    适配器在调用 media.upload 之前使用此函数以确保 token 是最新的。
    如果 refresh 失败则返回 ``None``——调用方回退到文本通知路径。
    ``email`` 控制刷新后的 token 写回位置；``None`` 使用旧版单文件路径。
    """
    if creds is None:
        return None

    if creds.valid:
        return creds

    try:
        from google.auth.transport.requests import Request
    except ImportError:
        return None

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _persist_credentials(creds, _token_path(email))
            return creds
        except Exception as exc:
            logger.warning(
                "[google_chat_user_oauth] refresh failed: %s", exc,
            )
            return None

    return None


def build_user_chat_service(creds: Any) -> Any:
    """构建以用户身份认证的 Google Chat API 客户端。

    用于 media.upload 及随后引用 attachmentDataRef 的 messages.create。
    Bot 的独立 SA 认证客户端（适配器中的 ``self._chat_api``）用于其他所有操作。
    """
    from googleapiclient.discovery import build as build_service
    return build_service("chat", "v1", credentials=creds, cache_discovery=False)


def list_authorized_emails() -> List[str]:
    """返回已存储按用户 token 的用户 email 集合。

    列出按用户 token 目录中的文件；不包含旧版单用户 token
    （其所有者未知）。经过清理的文件名会丢失 plus-addressed email
    的 ``+suffix`` 部分——接受这一限制，此列表仅用于管理员显示，
    而非用于信任决策。
    """
    d = _user_tokens_dir()
    if not d.exists():
        return []
    out: List[str] = []
    for f in d.iterdir():
        if f.is_file() and f.suffix == ".json":
            out.append(f.stem)
    out.sort()
    return out


def _persist_credentials(creds: Any, token_path: Path) -> None:
    """以原子方式持久化刷新后的凭据，并设置私有权限。"""
    try:
        _write_private_json(
            token_path,
            _normalize_authorized_user_payload(json.loads(creds.to_json())),
        )
    except Exception:
        logger.debug(
            "[google_chat_user_oauth] failed to persist credentials at %s",
            token_path, exc_info=True,
        )


# =============================================================================
# CLI 命令 — 由 agent 通过 /setup-files 斜杠命令驱动
# =============================================================================


def _normalize_authorized_user_payload(payload: dict) -> dict:
    """确保持久化的 token JSON 包含 google-auth 所需的 type 字段。"""
    normalized = dict(payload)
    if not normalized.get("type"):
        normalized["type"] = "authorized_user"
    return normalized


def _write_private_json(path: Path, data: Any) -> None:
    """以原子方式写入 JSON，并在支持的系统上设置 0o600 权限。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass

    tmp_path = path.with_suffix(f".tmp.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        fd = os.open(
            str(tmp_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        atomic_replace(tmp_path, path)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _ensure_deps() -> None:
    """Check deps available; install if not; exit on failure."""
    try:
        import googleapiclient  # noqa: F401
        import google_auth_oauthlib  # noqa: F401
    except ImportError:
        if not install_deps():
            sys.exit(1)


def install_deps() -> bool:
    try:
        import googleapiclient  # noqa: F401
        import google_auth_oauthlib  # noqa: F401
        print("Dependencies already installed.")
        return True
    except ImportError:
        pass

    print("Installing Google Chat OAuth dependencies...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet"] + _REQUIRED_PACKAGES,
            stdout=subprocess.DEVNULL,
        )
        print("Dependencies installed.")
        return True
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: Failed to install dependencies: {exc}")
        print("Or install via the optional extra:")
        print("  pip install 'hermes-agent[google_chat]'")
        return False


def check_auth(email: Optional[str] = None) -> bool:
    """Print status; return True if creds are usable.

    Per-user when ``email`` given, legacy single-user when omitted.
    """
    token_path = _token_path(email)
    if not token_path.exists():
        print(f"NOT_AUTHENTICATED: No token at {token_path}")
        return False

    creds = load_user_credentials(email)
    if creds is None:
        print(f"TOKEN_INVALID: Re-run /setup-files (path: {token_path})")
        return False

    print(f"AUTHENTICATED: Token valid at {token_path}")
    return True


def store_client_secret(path: str) -> None:
    """Validate and copy the user's OAuth client_secret.json into HERMES_HOME."""
    src = Path(path).expanduser().resolve()
    if not src.exists():
        print(f"ERROR: File not found: {src}")
        sys.exit(1)

    try:
        data = json.loads(src.read_text())
    except json.JSONDecodeError:
        print("ERROR: File is not valid JSON.")
        sys.exit(1)

    if "installed" not in data and "web" not in data:
        print(
            "ERROR: Not a Google OAuth client secret file (missing "
            "'installed' or 'web' key)."
        )
        print(
            "Download from: https://console.cloud.google.com/apis/credentials"
        )
        sys.exit(1)

    target = _client_secret_path()
    _write_private_json(target, data)
    print(f"OK: Client secret saved to {target}")


def _save_pending_auth(*, state: str, code_verifier: str,
                      email: Optional[str] = None) -> None:
    pending = _pending_auth_path(email)
    _write_private_json(
        pending,
        {
            "state": state,
            "code_verifier": code_verifier,
            "redirect_uri": _REDIRECT_URI,
            "email": email or "",
        },
    )


def _load_pending_auth(email: Optional[str] = None) -> dict:
    pending = _pending_auth_path(email)
    if not pending.exists():
        print("ERROR: No pending OAuth session found. Run --auth-url first.")
        sys.exit(1)
    try:
        data = json.loads(pending.read_text())
    except Exception as exc:
        print(f"ERROR: Could not read pending OAuth session: {exc}")
        print("Run --auth-url again to start a fresh session.")
        sys.exit(1)
    if not data.get("state") or not data.get("code_verifier"):
        print("ERROR: Pending OAuth session is missing PKCE data.")
        print("Run --auth-url again.")
        sys.exit(1)
    return data


def _extract_code_and_state(code_or_url: str) -> Tuple[str, Optional[str]]:
    """Accept a raw auth code OR the full failed-redirect URL the user pastes."""
    if not code_or_url.startswith("http"):
        return code_or_url, None

    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(code_or_url)
    params = parse_qs(parsed.query)
    if "code" not in params:
        print("ERROR: No 'code' parameter found in URL.")
        sys.exit(1)
    state = params.get("state", [None])[0]
    return params["code"][0], state


def get_auth_url(email: Optional[str] = None) -> None:
    """Print the OAuth URL for the user to visit. Persists PKCE state.

    ``email`` namespaces the pending state so two users can be mid-flow
    in parallel without trampling each other's PKCE verifier.
    """
    if not _client_secret_path().exists():
        print("ERROR: No client secret stored. Run --client-secret first.")
        sys.exit(1)

    _ensure_deps()
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_secrets_file(
        str(_client_secret_path()),
        scopes=SCOPES,
        redirect_uri=_REDIRECT_URI,
        autogenerate_code_verifier=True,
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )
    _save_pending_auth(state=state, code_verifier=flow.code_verifier, email=email)
    print(auth_url)


def exchange_auth_code(code: str, email: Optional[str] = None) -> None:
    """Exchange an auth code (or pasted redirect URL) for a refresh token.

    ``email`` selects the destination token path. ``None`` writes to the
    legacy single-user path (kept for the existing CLI entrypoint and for
    pre-multi-user installs).
    """
    if not _client_secret_path().exists():
        print("ERROR: No client secret stored. Run --client-secret first.")
        sys.exit(1)

    pending_auth = _load_pending_auth(email)
    raw_callback = code
    code, returned_state = _extract_code_and_state(code)
    if returned_state and returned_state != pending_auth["state"]:
        print(
            "ERROR: OAuth state mismatch. Run --auth-url again to start a "
            "fresh session."
        )
        sys.exit(1)

    _ensure_deps()
    from google_auth_oauthlib.flow import Flow
    from urllib.parse import parse_qs, urlparse

    granted_scopes = list(SCOPES)
    if isinstance(raw_callback, str) and raw_callback.startswith("http"):
        params = parse_qs(urlparse(raw_callback).query)
        scope_val = (params.get("scope") or [""])[0].strip()
        if scope_val:
            granted_scopes = scope_val.split()

    flow = Flow.from_client_secrets_file(
        str(_client_secret_path()),
        scopes=granted_scopes,
        redirect_uri=pending_auth.get("redirect_uri", _REDIRECT_URI),
        state=pending_auth["state"],
        code_verifier=pending_auth["code_verifier"],
    )

    try:
        # Accept partial scopes — user may deselect items in the consent screen.
        os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
        flow.fetch_token(code=code)
    except Exception as exc:
        print(f"ERROR: Token exchange failed: {exc}")
        print("The code may have expired. Run --auth-url to get a fresh URL.")
        sys.exit(1)

    creds = flow.credentials
    token_payload = _normalize_authorized_user_payload(json.loads(creds.to_json()))

    actually_granted = (
        list(creds.granted_scopes or [])
        if hasattr(creds, "granted_scopes") and creds.granted_scopes
        else []
    )
    if actually_granted:
        token_payload["scopes"] = actually_granted
    elif granted_scopes != SCOPES:
        token_payload["scopes"] = granted_scopes

    token_path = _token_path(email)
    _write_private_json(token_path, token_payload)
    _pending_auth_path(email).unlink(missing_ok=True)

    print(f"OK: Authenticated. Token saved to {token_path}")
    rel_label = (
        f"{display_hermes_home()}/google_chat_user_tokens/{_sanitize_email(email)}.json"
        if email
        else f"{display_hermes_home()}/google_chat_user_token.json"
    )
    print(f"Profile path: {rel_label}")


def revoke(email: Optional[str] = None) -> None:
    """Revoke the stored token with Google and delete it locally.

    Per-user when ``email`` given, legacy single-user when omitted.
    """
    token_path = _token_path(email)
    if not token_path.exists():
        print("No token to revoke.")
        return

    _ensure_deps()
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())

        import urllib.request
        urllib.request.urlopen(
            urllib.request.Request(
                f"https://oauth2.googleapis.com/revoke?token={creds.token}",
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ),
            timeout=15,
        )
        print("Token revoked with Google.")
    except Exception as exc:
        print(f"Remote revocation failed (token may already be invalid): {exc}")

    token_path.unlink(missing_ok=True)
    _pending_auth_path(email).unlink(missing_ok=True)
    print(f"Deleted {token_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Google Chat user-OAuth setup for Hermes (native attachment delivery)"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true",
                       help="Check if auth is valid (exit 0=yes, 1=no)")
    group.add_argument("--client-secret", metavar="PATH",
                       help="Store OAuth client_secret.json")
    group.add_argument("--auth-url", action="store_true",
                       help="Print OAuth URL for user to visit")
    group.add_argument("--auth-code", metavar="CODE",
                       help="Exchange auth code for token")
    group.add_argument("--revoke", action="store_true",
                       help="Revoke and delete stored token")
    group.add_argument("--install-deps", action="store_true",
                       help="Install Python dependencies")
    parser.add_argument("--email", metavar="EMAIL", default=None,
                       help="Scope operation to a specific user's token "
                            "(default: legacy single-user path)")
    args = parser.parse_args()

    email = args.email or None
    if args.check:
        sys.exit(0 if check_auth(email) else 1)
    elif args.client_secret:
        store_client_secret(args.client_secret)
    elif args.auth_url:
        get_auth_url(email)
    elif args.auth_code:
        exchange_auth_code(args.auth_code, email)
    elif args.revoke:
        revoke(email)
    elif args.install_deps:
        sys.exit(0 if install_deps() else 1)


if __name__ == "__main__":
    main()
