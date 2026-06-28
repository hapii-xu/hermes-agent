#!/usr/bin/env python3
"""
MCP OAuth 2.1 客户端支持

为需要 OAuth 认证（而非静态 bearer token）的 MCP 服务器实现基于浏览器的
OAuth 2.1 授权码流程（带 PKCE）。

使用 MCP Python SDK 的 ``OAuthClientProvider``（一个 ``httpx.Auth`` 子类），
它会自动处理服务发现、动态客户端注册、PKCE、token 交换、刷新以及
提升授权（step-up authorization）。

本模块提供以下粘合代码：
    - ``HermesTokenStorage``：将 token/客户端信息持久化到磁盘，使其在
      进程重启后仍然保留。
    - 回调服务器：临时的 localhost HTTP 服务器，用于捕获携带授权码的
      OAuth 重定向。
    - ``build_oauth_auth()``：由 ``mcp_tool.py`` 调用的入口函数，把上述
      组件装配到一起并返回 ``httpx.Auth`` 对象。

config.yaml 中的配置示例::

    mcp_servers:
      my_server:
        url: "https://mcp.example.com/mcp"
        auth: oauth
        oauth:                                  # 所有字段均为可选
          client_id: "pre-registered-id"        # 跳过动态注册
          client_secret: "secret"               # 仅机密客户端使用
          scope: "read write"                   # 默认：由服务器提供
          redirect_port: 0                      # 0 = 自动选取空闲端口
          client_name: "My Custom Client"       # 默认："Hermes Agent"
"""

import asyncio
import json
import logging
import os
import re
import secrets
import socket
import stat
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from hermes_constants import secure_parent_dir

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 懒加载导入 —— 带 OAuth 支持的 MCP SDK 是可选依赖
# ---------------------------------------------------------------------------

_OAUTH_AVAILABLE=False
try:
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import (
        OAuthClientInformationFull,
        OAuthClientMetadata,
        OAuthMetadata,
        OAuthToken,
    )

    _OAUTH_AVAILABLE=True
except ImportError:
    logger.debug("MCP OAuth types not available -- OAuth MCP auth disabled")

try:
    from pydantic import AnyUrl
except ImportError:
    AnyUrl = None  # type: ignore[assignment, misc]


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class OAuthNonInteractiveError(RuntimeError):
    """当 OAuth 在非交互式环境中需要浏览器交互时抛出。"""


# ---------------------------------------------------------------------------
# 模块级状态
# ---------------------------------------------------------------------------

# 最近一次 build_oauth_auth() 调用所使用的端口。暴露出来是为了让
# 测试可以校验回调服务器和 redirect_uri 使用的是同一个端口。
_oauth_port: int | None = None


# 在粘贴提示处接受的跳过令牌 —— 退出 OAuth 且不进行认证。
_SKIP_TOKENS = frozenset({"skip", "cancel", "s", "n", "no", "q", "quit"})

# 当用户通过 stdin 跳过时写入 result["error"] 的哨兵值。
# _wait_for_callback 会将其映射为 OAuthNonInteractiveError（"user_skipped"），
# 从而让 MCP 的安装流程将其视为非致命的「不带此服务器继续」，
# 而不是一次硬性失败。
_USER_SKIPPED_SENTINEL = "__hermes_user_skipped__"


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _get_token_dir() -> Path:
    """返回 MCP OAuth token 文件所在目录。

    使用 HERMES_HOME，使每个 profile 拥有各自的 OAuth token。
    布局：``HERMES_HOME/mcp-tokens/``
    """
    try:
        from hermes_constants import get_hermes_home
        base = Path(get_hermes_home())
    except ImportError:
        base = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    return base / "mcp-tokens"


def _safe_filename(name: str) -> str:
    """将服务器名净化为可作为文件名的字符串（不含路径分隔符）。"""
    return re.sub(r"[^\w\-]", "_", name).strip("_")[:128] or "default"


def _find_free_port() -> int:
    """在 localhost 上查找一个可用的 TCP 端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _is_interactive() -> bool:
    """当我们可以合理预期与用户交互时返回 True。"""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _can_open_browser() -> bool:
    """当打开浏览器大致可行时返回 True。"""
    # 明确的 SSH 会话 → 没有本地显示
    if os.environ.get("SSH_CLIENT") or os.environ.get("SSH_TTY"):
        return False
    # macOS 和 Windows 通常有显示
    if os.name == "nt":
        return True
    try:
        if os.uname().sysname == "Darwin":
            return True
    except AttributeError:
        pass
    # Linux/其他 posix：需要 DISPLAY 或 WAYLAND_DISPLAY
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return True
    return False


def _read_json(path: Path) -> dict | None:
    """读取一个 JSON 文件，若文件不存在或内容无效则返回 None。"""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return None


def _write_json(path: Path, data: dict) -> None:
    """将 dict 以 JSON 写入，并施加受限权限（0o600）。

    使用带 ``O_EXCL`` 和显式 mode 的 ``os.open``，使文件在创建时即原子地
    设为 0o600。早先的 ``write_text`` + 写后 ``chmod`` 方案会留下一个
    TOCTOU（time-of-check/time-of-use）窗口：临时文件在那段时间会继承
    进程的 umask（通常是 0o644 = 对所有用户可读），在创建与 chmod 之间
    把 OAuth token 暴露给其他本地用户。此处镜像了
    ``agent/google_oauth.py`` 中的修复（#19673）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # 将父目录权限收紧为 0o700，使同级目录无法遍历到凭据。
    # 在 Windows 上是空操作（POSIX 权限位不被强制执行）；忽略失败。
    # secure_parent_dir 会拒绝 chmod 根目录或顶层目录（#25821）。
    secure_parent_dir(path)
    # 每进程随机后缀，避免并发写入者之间相互碰撞，
    # 也避免上一次崩溃写入留下的残留文件。
    tmp = path.with_suffix(f".tmp.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        fd = os.open(
            str(tmp),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# HermesTokenStorage —— 将 token/客户端信息持久化到磁盘
# ---------------------------------------------------------------------------


class HermesTokenStorage:
    """将 OAuth token 和客户端注册信息持久化为 JSON 文件。

    文件布局::

        HERMES_HOME/mcp-tokens/<server_name>.json         -- token
        HERMES_HOME/mcp-tokens/<server_name>.client.json   -- 客户端信息
        HERMES_HOME/mcp-tokens/<server_name>.meta.json     -- OAuth 服务器元数据
    """

    def __init__(self, server_name: str):
        self._server_name = _safe_filename(server_name)

    def _tokens_path(self) -> Path:
        return _get_token_dir() / f"{self._server_name}.json"

    def _client_info_path(self) -> Path:
        return _get_token_dir() / f"{self._server_name}.client.json"

    def _meta_path(self) -> Path:
        return _get_token_dir() / f"{self._server_name}.meta.json"

    # -- tokens ------------------------------------------------------------

    async def get_tokens(self) -> "OAuthToken | None":
        data = _read_json(self._tokens_path())
        if data is None:
            return None
        # Hermes 在 SDK 序列化后的 token 旁边记录了一个绝对的挂钟时间
        # ``expires_at``（见 ``set_tokens``）。读取时我们把 ``expires_in``
        # 改写为剩余秒数，使 SDK 下游的 ``update_token_expiry`` 能计算出
        # 正确的绝对时间，并让 ``is_token_valid()`` 对那些在进程停机
        # 期间过期的 token 正确返回 False。
        #
        # 旧版 token 文件（Fix-A 之前）只有 ``expires_in`` 而没有
        # ``expires_at``。此时我们退而使用文件 mtime 作为「token 写入时间」
        # 的尽力而为的挂钟代理：若 (mtime + expires_in) 已是过去时间，
        # 则将 ``expires_in`` 钳为零，让 SDK 在第一次请求前先刷新。
        # 这会在下一次成功的 ``set_tokens``（写入新的 ``expires_at``
        # 字段）时自我修复一次。存储的 ``expires_at`` 在 model_validate
        # 之前会被剥离，因为它不属于 SDK 的 OAuthToken schema。
        absolute_expiry = data.pop("expires_at", None)
        if absolute_expiry is not None:
            data["expires_in"] = int(max(absolute_expiry - time.time(), 0))
        elif data.get("expires_in") is not None:
            try:
                file_mtime = self._tokens_path().stat().st_mtime
            except OSError:
                file_mtime = None
            if file_mtime is not None:
                try:
                    implied_expiry = file_mtime + int(data["expires_in"])
                    data["expires_in"] = int(max(implied_expiry - time.time(), 0))
                except (TypeError, ValueError):
                    pass
        try:
            return OAuthToken.model_validate(data)
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("Corrupt tokens at %s -- ignoring: %s", self._tokens_path(), exc)
            return None

    async def set_tokens(self, tokens: "OAuthToken") -> None:
        payload = tokens.model_dump(mode="json", exclude_none=True)
        # 持久化一个绝对的 ``expires_at``，使进程重启后能够重建正确的
        # 剩余 TTL。若不这样做，MCP SDK 的 ``_initialize`` 会重新加载一个
        # 相对的 ``expires_in``，而它没有挂钟参照，导致
        # ``context.token_expiry_time=None``，``is_token_valid()`` 会
        # 错误地返回 True。参见 ``mcp-oauth-token-diagnosis`` 技能中的
        # Fix A，以及 Claude Code 的 ``OAuthTokens.expiresAt`` 持久化
        # （auth.ts ~180 行）。
        expires_in = payload.get("expires_in")
        if expires_in is not None:
            try:
                payload["expires_at"] = time.time() + int(expires_in)
            except (TypeError, ValueError):
                # 模拟 token 或异常结构：跳过 expires_at 的写入，
                # 而不是让持久化失败。
                pass
        _write_json(self._tokens_path(), payload)
        logger.debug("OAuth tokens saved for %s", self._server_name)

    # -- client info -------------------------------------------------------

    async def get_client_info(self) -> "OAuthClientInformationFull | None":
        data = _read_json(self._client_info_path())
        if data is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate(data)
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("Corrupt client info at %s -- ignoring: %s", self._client_info_path(), exc)
            return None

    async def set_client_info(self, client_info: "OAuthClientInformationFull") -> None:
        _write_json(self._client_info_path(), client_info.model_dump(mode="json", exclude_none=True))
        logger.debug("OAuth client info saved for %s", self._server_name)

    # -- oauth server metadata --------------------------------------------
    # MCP SDK 仅在内存中保存已发现的 ``OAuthMetadata``（token 端点 URL 等）。
    # 在此持久化它，可以让重启后的进程在刷新 token 时无需重新跑一遍元数据
    # 发现。否则冷启动刷新请求会退回到 SDK 猜测的 ``{server_url}/token``，
    # 这在大多数真实 provider 上会返回 404，并迫使用户重新走一遍完整的
    # 浏览器授权。

    def save_oauth_metadata(self, metadata: "OAuthMetadata") -> None:
        _write_json(self._meta_path(), metadata.model_dump(exclude_none=True, mode="json"))
        logger.debug("OAuth metadata saved for %s", self._server_name)

    def load_oauth_metadata(self) -> "OAuthMetadata | None":
        data = _read_json(self._meta_path())
        if data is None:
            return None
        try:
            return OAuthMetadata.model_validate(data)
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("Corrupt OAuth metadata at %s -- ignoring: %s", self._meta_path(), exc)
            return None

    # -- cleanup -----------------------------------------------------------

    def remove(self) -> None:
        """删除该服务器存储的全部 OAuth 状态。"""
        for p in (self._tokens_path(), self._client_info_path(), self._meta_path()):
            p.unlink(missing_ok=True)

    def has_cached_tokens(self) -> bool:
        """当磁盘上存有 token（可能已过期）时返回 True。"""
        return self._tokens_path().exists()


# ---------------------------------------------------------------------------
# 回调处理器工厂 —— 每次调用都会得到自己的 result dict
# ---------------------------------------------------------------------------


def _make_callback_handler() -> tuple[type, dict]:
    """创建一个带独立 result dict 的、按流程隔离的回调 HTTP handler 类。

    返回 ``(HandlerClass, result_dict)``，其中 *result_dict* 是一个可变
    dict，handler 会在 OAuth 重定向到达时把 ``auth_code`` 和 ``state``
    写入其中。每次调用都返回一个全新的配对，因此并发流程之间不会
    互相踩踏。
    """
    result: dict[str, Any] = {"auth_code": None, "state": None, "error": None}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            params = parse_qs(urlparse(self.path).query)
            code = params.get("code", [None])[0]
            state = params.get("state", [None])[0]
            error = params.get("error", [None])[0]

            result["auth_code"] = code
            result["state"] = state
            result["error"] = error

            body = (
                "<html><body><h2>Authorization Successful</h2>"
                "<p>You can close this tab and return to Hermes.</p></body></html>"
            ) if code else (
                "<html><body><h2>Authorization Failed</h2>"
                f"<p>Error: {error or 'unknown'}</p></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug("OAuth callback: %s", fmt % args)

    return _Handler, result


# ---------------------------------------------------------------------------
# 供 OAuthClientProvider 使用的异步重定向 + 回调处理器
# ---------------------------------------------------------------------------


async def _redirect_handler(authorization_url: str) -> None:
    """向用户展示授权 URL。

    在可能的情况下自动打开浏览器；对于无头/SSH/gateway 环境，始终打印
    URL 作为兜底。
    """
    msg = (
        f"\n  MCP OAuth: authorization required.\n"
        f"  Open this URL in your browser:\n\n"
        f"    {authorization_url}\n"
    )
    print(msg, file=sys.stderr)

    # 在远程 SSH 会话中，OAuth provider 会重定向到
    # http://127.0.0.1:<port>/callback，该地址到达的是*远程*机器上的
    # 回调服务器 —— 而不是用户浏览器所在的本地机器。有两条出路：
    # 把重定向 URL 粘贴回来（交互式 TTY 下的默认兜底，由
    # _wait_for_callback 提供），或设置一个 SSH 端口转发，让重定向
    # 能通过隧道送达。
    if _oauth_port and (os.getenv("SSH_CLIENT") or os.getenv("SSH_TTY")):
        print(
            f"  Remote session detected. After you authorize, the provider redirects to\n"
            f"    http://127.0.0.1:{_oauth_port}/callback\n"
            f"  which only the listener on THIS machine can receive. Two options:\n"
            f"\n"
            f"    1. Easiest — when your browser shows a connection error after\n"
            f"       authorizing, copy the full URL from the address bar and paste\n"
            f"       it at the prompt below. The pasted ``code=...&state=...`` is\n"
            f"       enough to complete the flow.\n"
            f"\n"
            f"    2. Or forward the port first in a separate terminal:\n"
            f"         ssh -N -L {_oauth_port}:127.0.0.1:{_oauth_port} <user>@<this-host>\n"
            f"       then open the URL above and let it redirect normally.\n"
            f"\n"
            f"  See: https://hermes-agent.nousresearch.com/docs/guides/oauth-over-ssh\n",
            file=sys.stderr,
        )

    if _can_open_browser():
        try:
            opened = webbrowser.open(authorization_url)
            if opened:
                print("  (Browser opened automatically.)\n", file=sys.stderr)
            else:
                print("  (Could not open browser — please open the URL manually.)\n", file=sys.stderr)
        except Exception:
            print("  (Could not open browser — please open the URL manually.)\n", file=sys.stderr)
    else:
        print("  (Headless environment detected — open the URL manually.)\n", file=sys.stderr)


async def _wait_for_callback() -> tuple[str, str | None]:
    """等待 OAuth 回调到达本地回调服务器。

    使用模块级的 ``_oauth_port``，它由 ``build_oauth_auth`` 在本函数被
    调用之前设置好。在不阻塞事件循环的前提下轮询结果。

    在交互式 TTY 上，让 HTTP 监听器与一个 stdin 粘贴兜底竞争，这样
    没有 SSH 隧道的用户也能从另一台机器的浏览器里复制重定向 URL
    （或仅复制 ``code=...&state=...`` 查询串）粘贴回来。当重定向先
    到达 HTTP 监听器时它获胜；否则粘贴兜底获胜。

    Raises:
        OAuthNonInteractiveError: 回调超时（没有用户在场完成浏览器授权）。
        RuntimeError: ``_oauth_port`` 尚未被设置，这表明
            ``build_oauth_auth`` 被跳过了 —— 在以 ``-O``/``-OO`` 运行
            Python 时，断言形式曾是一个静默 bug。
    """
    if _oauth_port is None:
        raise RuntimeError(
            "OAuth callback port not set — build_oauth_auth must be called "
            "before _wait_for_oauth_callback"
        )

    # 回调服务器已在运行（在 build_oauth_auth 中启动）。
    # 我们只需轮询结果。
    handler_cls, result = _make_callback_handler()

    # 在已知端口上启动一个临时服务器
    try:
        server = HTTPServer(("127.0.0.1", _oauth_port), handler_cls)
    except OSError:
        # 端口已被占用 —— build_oauth_auth 启动的服务器仍在运行。
        # 退回轮询 build_oauth_auth 启动的那个服务器。
        raise OAuthNonInteractiveError(
            "OAuth callback timed out — could not bind callback port. "
            "Complete the authorization in a browser first, then retry."
        )

    server_thread = threading.Thread(target=server.handle_request, daemon=True)
    server_thread.start()

    # 可选的粘贴兜底线程：仅在交互式 TTY 上启用。从 stdin 读取一行，
    # 解析出 code/state 并写入共享的 result dict。HTTP 监听器与本线程
    # 竞争结果；谁先填上谁获胜。
    paste_thread: threading.Thread | None = None
    if _is_interactive():
        print(
            "\n  Or paste the redirect URL here (or the ``?code=...&state=...`` "
            "portion) and press Enter. Type ``skip`` + Enter to continue "
            "without this server:",
            file=sys.stderr,
            flush=True,
        )
        paste_thread = threading.Thread(
            target=_paste_callback_reader, args=(result,), daemon=True
        )
        paste_thread.start()

    timeout = 300.0
    poll_interval = 0.5
    elapsed = 0.0
    try:
        while elapsed < timeout:
            if result["auth_code"] is not None or result["error"] is not None:
                break
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
    finally:
        server.server_close()

    if result["error"] == _USER_SKIPPED_SENTINEL:
        raise OAuthNonInteractiveError("user_skipped")
    if result["error"]:
        raise RuntimeError(f"OAuth authorization failed: {result['error']}")
    if result["auth_code"] is None:
        raise OAuthNonInteractiveError(
            "OAuth callback timed out — no authorization code received. "
            "Ensure you completed the browser authorization flow."
        )

    return result["auth_code"], result["state"]


def _paste_callback_reader(result: dict) -> None:
    """从 stdin 读取一行，按 OAuth 重定向解析，并写入 result。

    接受以下任一形式：
      - 完整的重定向 URL：``http://127.0.0.1:37949/callback?code=...&state=...``
      - provider 自己的回调 URL：``https://mcp.example.com/callback?code=...&state=...``
      - 仅查询串：``?code=...&state=...`` 或 ``code=...&state=...``
      - 跳过令牌（``skip``、``cancel``、``s``、``n``、``no``、``q``、``quit``）
        —— 干净地退出 OAuth 流程且不进行认证。调用方会抛出
        :class:`OAuthNonInteractiveError`，使 MCP 连接设置将其视为
        非致命的「用户主动放弃」并继续运行而不加载该服务器。

    解析失败、EOF 或中断都会被吞掉 —— 这是与 HTTP 监听器（仍是主路径）
    并行的尽力而为兜底。
    """
    try:
        line = sys.stdin.readline()
    except (KeyboardInterrupt, OSError, ValueError):
        return
    if not line:
        return  # EOF
    line = line.strip()
    if not line:
        return

    # 若 HTTP 监听器已先获胜则跳过。
    if result.get("auth_code") is not None or result.get("error") is not None:
        return

    # 跳过令牌：用户明确放弃授权。用一个哨兵错误字符串标记 result，
    # _wait_for_callback 会将其映射为 OAuthNonInteractiveError
    # （mcp_tool.py 已将其作为非致命的「跳过该服务器并继续启动」路径处理）。
    if line.lower() in _SKIP_TOKENS:
        if result.get("auth_code") is not None or result.get("error") is not None:
            return
        result["error"] = _USER_SKIPPED_SENTINEL
        print(
            "  OAuth skipped. Run `hermes mcp login <server>` later to "
            "authenticate, or set ``enabled: false`` on that server in "
            "config.yaml to disable persistently.",
            file=sys.stderr,
        )
        return

    # 若用户只粘贴了查询串，去掉开头的 "?"。
    query = line
    if "?" in line:
        # 可能是完整 URL，也可能是 "?code=..."。取第一个 "?" 之后的所有内容。
        query = line.split("?", 1)[1]
    if query.startswith("?"):
        query = query[1:]

    try:
        params = parse_qs(query)
    except (ValueError, TypeError):
        print(
            "  Could not parse pasted input as an OAuth redirect — ignoring.",
            file=sys.stderr,
        )
        return

    code = params.get("code", [None])[0]
    state = params.get("state", [None])[0]
    error = params.get("error", [None])[0]

    if not code and not error:
        print(
            "  Pasted input did not contain ``code=`` or ``error=`` — ignoring.",
            file=sys.stderr,
        )
        return

    # 写入前再做一次竞争检查。
    if result.get("auth_code") is not None or result.get("error") is not None:
        return

    result["auth_code"] = code
    result["state"] = state
    result["error"] = error
    if code:
        print("  Got authorization code from paste — completing flow.", file=sys.stderr)


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------


def remove_oauth_tokens(server_name: str) -> None:
    """删除某个服务器存储的 OAuth token 和客户端信息。"""
    storage = HermesTokenStorage(server_name)
    storage.remove()
    logger.info("OAuth tokens removed for '%s'", server_name)


# ---------------------------------------------------------------------------
# 抽取出的辅助函数（MCP OAuth 合并工作的 Task 3）
#
# 它们组合进下方的 ``build_oauth_auth``，同时也被
# ``tools.mcp_oauth_manager.MCPOAuthManager._build_provider`` 使用，
# 从而让两条构造路径共用同一套实现。
# ---------------------------------------------------------------------------


def _configure_callback_port(cfg: dict) -> int:
    """选取或校验 OAuth 回调端口。

    将解析出的端口存入 ``cfg['_resolved_port']``，使兄弟辅助函数
    （以及 manager）能从同一个 dict 中读取。返回解析出的端口。

    说明：同时设置了遗留的模块级 ``_oauth_port``，使对
    ``_wait_for_callback`` 的现有调用继续可用。该遗留全局变量正是
    issue #5344（并发 OAuth 流程上的端口冲突）的根因；用 ContextVar
    替换它不在本次合并 PR 的范围内。
    """
    global _oauth_port
    requested = int(cfg.get("redirect_port", 0))
    port = _find_free_port() if requested == 0 else requested
    cfg["_resolved_port"] = port
    _oauth_port = port  # 遗留消费者：_wait_for_callback 读取此值
    return port


def _build_client_metadata(cfg: dict) -> "OAuthClientMetadata":
    """根据 oauth 配置 dict 构建 OAuthClientMetadata。

    要求 ``cfg['_resolved_port']`` 已由 :func:`_configure_callback_port`
    预先填好。
    """
    port = cfg.get("_resolved_port")
    if port is None:
        raise ValueError(
            "_configure_callback_port() must be called before _build_client_metadata()"
        )
    client_name = cfg.get("client_name", "Hermes Agent")
    scope = cfg.get("scope")
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    metadata_kwargs: dict[str, Any] = {
        "client_name": client_name,
        "redirect_uris": [AnyUrl(redirect_uri)],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if scope:
        metadata_kwargs["scope"] = scope
    if cfg.get("client_secret"):
        metadata_kwargs["token_endpoint_auth_method"] = "client_secret_post"

    return OAuthClientMetadata.model_validate(metadata_kwargs)


def _maybe_preregister_client(
    storage: "HermesTokenStorage",
    cfg: dict,
    client_metadata: "OAuthClientMetadata",
) -> None:
    """若 cfg 中带有预注册的 client_id，则将其持久化到 storage。"""
    client_id = cfg.get("client_id")
    if not client_id:
        return
    port = cfg["_resolved_port"]
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    info_dict: dict[str, Any] = {
        "client_id": client_id,
        "redirect_uris": [redirect_uri],
        "grant_types": client_metadata.grant_types,
        "response_types": client_metadata.response_types,
        "token_endpoint_auth_method": client_metadata.token_endpoint_auth_method,
    }
    if cfg.get("client_secret"):
        info_dict["client_secret"] = cfg["client_secret"]
    if cfg.get("client_name"):
        info_dict["client_name"] = cfg["client_name"]
    if cfg.get("scope"):
        info_dict["scope"] = cfg["scope"]

    client_info = OAuthClientInformationFull.model_validate(info_dict)
    _write_json(storage._client_info_path(), client_info.model_dump(mode="json", exclude_none=True))
    logger.debug("Pre-registered client_id=%s for '%s'", client_id, storage._server_name)


def build_oauth_auth(
    server_name: str,
    server_url: str,
    oauth_config: dict | None = None,
) -> "OAuthClientProvider | None":
    """为某个 MCP 服务器构建一个与 ``httpx.Auth`` 兼容的 OAuth handler。

    为向后兼容而保留的公共 API。新代码应改用
    :func:`tools.mcp_oauth_manager.get_manager`，以便在配置时、运行时
    以及重连路径之间共享 OAuth 状态。

    Args:
        server_name: mcp_servers 配置中的服务器键名（用于存储）。
        server_url: MCP 服务器端点 URL。
        oauth_config: 可选的 dict，来自 config.yaml 中的 ``oauth:`` 块。

    Returns:
        一个 ``OAuthClientProvider`` 实例；若 MCP SDK 缺少 OAuth 支持
        则返回 None。
    """
    if not _OAUTH_AVAILABLE:
        logger.warning(
            "MCP OAuth requested for '%s' but SDK auth types are not available. "
            "Install with: pip install 'mcp>=1.26.0'",
            server_name,
        )
        return None

    cfg = dict(oauth_config or {})  # 拷贝 —— 我们会改写 _resolved_port
    storage = HermesTokenStorage(server_name)

    if not _is_interactive() and not storage.has_cached_tokens():
        raise OAuthNonInteractiveError(
            "MCP OAuth for "
            f"'{server_name}': non-interactive environment and no cached tokens "
            "found. The OAuth flow requires browser authorization. Run "
            f"`hermes mcp login {server_name}` interactively first to complete "
            "initial authorization, then cached tokens will be reused."
        )

    _configure_callback_port(cfg)
    client_metadata = _build_client_metadata(cfg)
    _maybe_preregister_client(storage, cfg, client_metadata)

    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=client_metadata,
        storage=storage,
        redirect_handler=_redirect_handler,
        callback_handler=_wait_for_callback,
        timeout=float(cfg.get("timeout", 300)),
    )
