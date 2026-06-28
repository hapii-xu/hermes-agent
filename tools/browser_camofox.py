"""Camofox 浏览器后端 — 通过 REST API 实现的本地反检测浏览器。

Camofox-browser 是一个自托管的 Node.js 服务器，封装了 Camoufox（Firefox
的 C++ 指纹欺骗分支）。它暴露的 REST API 与我们的浏览器工具接口一一对应：
包含元素 ref 的无障碍快照、通过 ref 执行点击/输入/滚动、截图等。

当 ``CAMOFOX_URL`` 被设置（例如 ``http://localhost:9377``）时，浏览器
工具会通过此模块路由，而不是使用 ``agent-browser`` CLI。

Setup::

    # Option 1: npm
    git clone https://github.com/jo-inc/camofox-browser && cd camofox-browser
    npm install && npm start   # downloads Camoufox (~300MB) on first run

    # Option 2: Docker
    docker run -p 9377:9377 -e CAMOFOX_PORT=9377 jo-inc/camofox-browser

Then set ``CAMOFOX_URL=http://localhost:9377`` in ``~/.hermes/.env``.
For Docker Camofox, optionally set ``CAMOFOX_REWRITE_LOOPBACK_URLS=true``
so page URLs like ``http://127.0.0.1:3000`` are opened inside the
container as ``http://host.docker.internal:3000``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import uuid
from typing import Any, Dict, Optional
from urllib.parse import SplitResult, urlsplit, urlunsplit

import requests

from hermes_cli.config import cfg_get, load_config
from tools.browser_camofox_state import get_camofox_identity
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT = 30  # 每个 HTTP 请求的秒数
_SNAPSHOT_MAX_CHARS = 80_000  # camofox 在此限制处分页
_vnc_url: Optional[str] = None  # 从 /health 响应缓存而来
_vnc_url_checked = False  # 每个进程只探测一次


def get_camofox_url() -> str:
    """返回配置的 Camofox 服务器 URL，为空则返回空字符串。"""
    return os.getenv("CAMOFOX_URL", "").rstrip("/")


def is_camofox_mode() -> bool:
    """当 Camofox 后端已配置且没有活动的 CDP 覆盖时返回 True。

    当用户通过 ``/browser connect`` 显式连接到一个活跃的 Chromium 系浏览器时
    （这会设置 ``BROWSER_CDP_URL``），CDP 连接优先于 Camofox，使浏览器工具
    操作的是真实浏览器，而不是被静默路由到 Camofox 后端。
    """
    if os.getenv("BROWSER_CDP_URL", "").strip():
        return False
    return bool(get_camofox_url())


def check_camofox_available() -> bool:
    """校验 Camofox 服务器是否可达。"""
    global _vnc_url, _vnc_url_checked
    url = get_camofox_url()
    if not url:
        return False
    try:
        resp = requests.get(f"{url}/health", timeout=5)
        if resp.status_code == 200 and not _vnc_url_checked:
            try:
                data = resp.json()
                vnc_port = data.get("vncPort")
                if isinstance(vnc_port, int) and 1 <= vnc_port <= 65535:
                    from urllib.parse import urlparse
                    parsed = urlparse(url)
                    host = parsed.hostname or "localhost"
                    _vnc_url = f"http://{host}:{vnc_port}"
            except (ValueError, KeyError):
                pass
            _vnc_url_checked = True
        return resp.status_code == 200
    except Exception:
        return False


def get_vnc_url() -> Optional[str]:
    """如果 Camofox 服务器暴露了 VNC URL 则返回它，否则返回 None。"""
    if not _vnc_url_checked:
        check_camofox_available()
    return _vnc_url


def _get_camofox_config() -> Dict[str, Any]:
    """返回 ``browser.camofox`` 配置块，否则返回空 dict。"""
    try:
        camofox_cfg = load_config().get("browser", {}).get("camofox", {})
    except Exception as exc:
        logger.warning("camofox config check failed, defaulting to disabled: %s", exc)
        return {}
    return camofox_cfg if isinstance(camofox_cfg, dict) else {}


def _managed_persistence_enabled() -> bool:
    """返回是否为 Camofox 启用了 Hermes 托管的持久化。

    启用时，会话使用一个稳定的、profile 作用域的 userId，以便 Camofox
    服务器能把它映射到一个持久的浏览器 profile 目录。禁用时（默认），每个
    会话获得一个随机 userId（临时）。

    由 config.yaml 中的 ``browser.camofox.managed_persistence`` 控制。
    """
    return bool(_get_camofox_config().get("managed_persistence"))


def _camofox_identity_override(task_id: Optional[str], camofox_cfg: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """返回外部配置的 Camofox 身份（如果已设置）。

    拥有可见 Camofox 浏览器的集成可以设置一个共享的 user ID，使 Hermes
    在同一个浏览器 profile 中操作，而不是创建一个单独的私有会话。
    """
    user_id = os.getenv("CAMOFOX_USER_ID", "").strip() or str(camofox_cfg.get("user_id") or "").strip()
    if not user_id:
        return None

    session_key = (
        os.getenv("CAMOFOX_SESSION_KEY", "").strip()
        or str(camofox_cfg.get("session_key") or "").strip()
        or f"task_{(task_id or 'default')[:16]}"
    )
    return {"user_id": user_id, "session_key": session_key}


def _env_flag(name: str) -> Optional[bool]:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return None
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    logger.debug("Ignoring invalid boolean env %s=%r", name, raw)
    return None


def _adopt_existing_tab_enabled(camofox_cfg: Dict[str, Any]) -> bool:
    """返回 Hermes 是否应接管一个已存在的 Camofox 标签页 ID。"""
    env_value = _env_flag("CAMOFOX_ADOPT_EXISTING_TAB")
    if env_value is not None:
        return env_value
    return bool(camofox_cfg.get("adopt_existing_tab"))


def _loopback_rewrite_enabled(camofox_cfg: Dict[str, Any]) -> bool:
    """返回是否应为了 Docker 重写环回导航 URL。

    ``CAMOFOX_URL`` 本身常常指向一个发布到宿主机的 Docker 端口，例如
    ``http://127.0.0.1:9377``。这对 Hermes 与 Camofox 控制通信是正确的，
    但像 ``http://127.0.0.1:3000`` 这样的页面 URL 是由浏览器在 Docker 容器
    *内部* 打开的。在该上下文里，环回地址指向容器，而不是运行 Web 应用的
    宿主机。

    该重写是可选的，因为非 Docker 的 Camofox 安装在宿主机上运行浏览器，
    那里环回 URL 本来就是正确的。
    """
    env_value = _env_flag("CAMOFOX_REWRITE_LOOPBACK_URLS")
    if env_value is not None:
        return env_value
    return bool(camofox_cfg.get("rewrite_loopback_urls"))


def _loopback_rewrite_host(camofox_cfg: Dict[str, Any]) -> str:
    """返回重写环回页面 URL 时使用的主机别名。"""
    return (
        os.getenv("CAMOFOX_LOOPBACK_HOST_ALIAS", "").strip()
        or str(camofox_cfg.get("loopback_host_alias") or "").strip()
        or "host.docker.internal"
    )


def _is_loopback_hostname(hostname: Optional[str]) -> bool:
    """对 localhost/127.0.0.0/8/::1 类主机名返回 True。"""
    if not hostname:
        return False
    host = hostname.strip().strip("[]").lower()
    if host in {"localhost", "localhost.localdomain"}:
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _rewrite_loopback_url_for_camofox(url: str) -> tuple[str, Optional[Dict[str, str]]]:
    """如果已配置，为 Docker 托管的 Camofox 重写环回页面 URL。

    返回 ``(rewritten_url, metadata)``。``metadata`` 仅在发生重写时存在，
    以便工具结果能向模型披露这一变更。
    """
    camofox_cfg = _get_camofox_config()
    if not _loopback_rewrite_enabled(camofox_cfg):
        return url, None

    try:
        parsed = urlsplit(url)
    except ValueError:
        return url, None

    if parsed.scheme not in {"http", "https"} or not _is_loopback_hostname(parsed.hostname):
        return url, None

    alias = _loopback_rewrite_host(camofox_cfg)
    if not alias:
        return url, None

    userinfo = ""
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo += f":{parsed.password}"
        userinfo += "@"
    host_part = f"[{alias}]" if ":" in alias and not alias.startswith("[") else alias
    port_part = f":{parsed.port}" if parsed.port else ""
    rewritten = urlunsplit(
        SplitResult(parsed.scheme, f"{userinfo}{host_part}{port_part}", parsed.path, parsed.query, parsed.fragment)
    )
    return rewritten, {
        "from": parsed.hostname or "",
        "to": alias,
        "original_url": url,
        "rewritten_url": rewritten,
    }


# ---------------------------------------------------------------------------
# 会话管理
# ---------------------------------------------------------------------------
# task_id -> {"user_id": str, "tab_id": str|None} 的映射
_sessions: Dict[str, Dict[str, Any]] = {}
_sessions_lock = threading.Lock()


def _adopt_existing_tab(session: Dict[str, Any]) -> Dict[str, Any]:
    """把进程本地的状态挂接到一个已打开的、被托管的 Camofox 标签页。

    有些集成在 Hermes 之外拥有可见的 Camofox 标签页。网关重启可能让本模块
    的内存会话缓存变空，而 Camofox 仍保留着那个标签页，因此在创建新标签页
    之前先重新水合 tab_id。
    """
    if session.get("tab_id") or not session.get("adopt_existing_tab"):
        return session

    if not get_camofox_url():
        return session

    try:
        tabs = _get("/tabs", params={"userId": session["user_id"]}, timeout=5).get("tabs", [])
    except Exception as exc:
        logger.debug("Camofox tab adoption failed for %s: %s", session.get("user_id"), exc)
        return session

    if not isinstance(tabs, list) or not tabs:
        return session

    session_key = session.get("session_key")
    matching_tabs = [
        tab
        for tab in tabs
        if isinstance(tab, dict) and tab.get("listItemId") == session_key
    ]
    candidates = matching_tabs or [tab for tab in tabs if isinstance(tab, dict)]
    latest = candidates[-1] if candidates else None
    tab_id = latest.get("tabId") if isinstance(latest, dict) else None
    if isinstance(tab_id, str) and tab_id:
        session["tab_id"] = tab_id
        logger.debug("Adopted existing Camofox tab %s for %s", tab_id, session.get("user_id"))

    return session


def _get_session(task_id: Optional[str]) -> Dict[str, Any]:
    """为给定 task 获取或创建一个 camofox 会话。

    启用了托管持久化时，使用从 Hermes profile 派生出的确定性 userId，以便
    Camofox 服务器能在重启之间把它映射到同一个持久浏览器 profile。
    """
    task_id = task_id or "default"
    with _sessions_lock:
        if task_id in _sessions:
            return _adopt_existing_tab(_sessions[task_id])

        camofox_cfg = _get_camofox_config()
        identity_override = _camofox_identity_override(task_id, camofox_cfg)
        if identity_override:
            session = {
                "user_id": identity_override["user_id"],
                "tab_id": None,
                "session_key": identity_override["session_key"],
                "managed": True,
                "adopt_existing_tab": _adopt_existing_tab_enabled(camofox_cfg),
            }
        elif bool(camofox_cfg.get("managed_persistence")):
            identity = get_camofox_identity(task_id)
            session = {
                "user_id": identity["user_id"],
                "tab_id": None,
                "session_key": identity["session_key"],
                "managed": True,
                "adopt_existing_tab": _adopt_existing_tab_enabled(camofox_cfg),
            }
        else:
            session = {
                "user_id": f"hermes_{uuid.uuid4().hex[:10]}",
                "tab_id": None,
                "session_key": f"task_{task_id[:16]}",
                "managed": False,
                "adopt_existing_tab": False,
            }
        _sessions[task_id] = session
        return _adopt_existing_tab(session)


def _ensure_tab(task_id: Optional[str], url: str = "about:blank") -> Dict[str, Any]:
    """确保会话存在一个标签页，需要时创建一个。"""
    session = _get_session(task_id)
    if session["tab_id"]:
        return session
    base = get_camofox_url()
    resp = requests.post(
        f"{base}/tabs",
        json={
            "userId": session["user_id"],
            "sessionKey": session["session_key"],
            "url": url,
        },
        timeout=_DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    session["tab_id"] = data.get("tabId")
    return session


def _drop_session(task_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """移除并返回会话信息。"""
    task_id = task_id or "default"
    with _sessions_lock:
        return _sessions.pop(task_id, None)


def camofox_soft_cleanup(task_id: Optional[str] = None) -> bool:
    """释放内存中的会话，但不销毁服务端上下文。

    启用了托管持久化时，浏览器 profile（及其 cookies）必须在 agent 任务
    之间保留。本辅助函数只丢弃本地跟踪条目并返回 ``True``。未启用托管持久化
    时，它什么都不做并返回 ``False``，以便调用方回退到
    :func:`camofox_close`。
    """
    camofox_cfg = _get_camofox_config()
    if bool(camofox_cfg.get("managed_persistence")) or _camofox_identity_override(task_id, camofox_cfg):
        _drop_session(task_id)
        logger.debug("Camofox soft cleanup for task %s (managed persistence)", task_id)
        return True
    return False


# ---------------------------------------------------------------------------
# HTTP 辅助函数
# ---------------------------------------------------------------------------

def _post(path: str, body: dict, timeout: int = _DEFAULT_TIMEOUT) -> dict:
    """向 camofox 发送 JSON POST 请求，并返回解析后的响应。"""
    url = f"{get_camofox_url()}{path}"
    resp = requests.post(url, json=body, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _get(path: str, params: dict = None, timeout: int = _DEFAULT_TIMEOUT) -> dict:
    """从 camofox 发起 GET 请求，并返回解析后的响应。"""
    url = f"{get_camofox_url()}{path}"
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _get_raw(path: str, params: dict = None, timeout: int = _DEFAULT_TIMEOUT) -> requests.Response:
    """从 camofox 发起 GET 请求，并返回原始响应（用于二进制数据）。"""
    url = f"{get_camofox_url()}{path}"
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp


def _delete(path: str, body: dict = None, timeout: int = _DEFAULT_TIMEOUT) -> dict:
    """向 camofox 发起 DELETE 请求，并返回解析后的响应。"""
    url = f"{get_camofox_url()}{path}"
    resp = requests.delete(url, json=body, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------

def camofox_navigate(url: str, task_id: Optional[str] = None) -> str:
    """通过 Camofox 导航到一个 URL。"""
    try:
        browser_url, rewrite_info = _rewrite_loopback_url_for_camofox(url)
        session = _get_session(task_id)
        if not session["tab_id"]:
            # 用目标 URL 直接创建标签页
            session = _ensure_tab(task_id, browser_url)
            data = {"ok": True, "url": browser_url}
        else:
            # 导航已有标签页
            data = _post(
                f"/tabs/{session['tab_id']}/navigate",
                {"userId": session["user_id"], "url": browser_url},
                timeout=60,
            )
        result = {
            "success": True,
            "url": data.get("url", browser_url),
            "title": data.get("title", ""),
        }
        if rewrite_info:
            result["requested_url"] = url
            result["url_rewrite"] = rewrite_info
            result["warning"] = (
                "Rewrote loopback URL for Docker-hosted Camofox: "
                f"{rewrite_info['from']} -> {rewrite_info['to']}"
            )
        vnc = get_vnc_url()
        if vnc:
            result["vnc_url"] = vnc
            result["vnc_hint"] = (
                "Browser is visible via VNC. "
                "Share this link with the user so they can watch the browser live."
            )

        # 自动拍一份精简快照，让模型能立即行动
        try:
            snap_data = _get(
                f"/tabs/{session['tab_id']}/snapshot",
                params={"userId": session["user_id"]},
            )
            snapshot_text = snap_data.get("snapshot", "")
            from tools.browser_tool import (
                SNAPSHOT_SUMMARIZE_THRESHOLD,
                _truncate_snapshot,
            )
            if len(snapshot_text) > SNAPSHOT_SUMMARIZE_THRESHOLD:
                snapshot_text = _truncate_snapshot(snapshot_text)
            result["snapshot"] = snapshot_text
            result["element_count"] = snap_data.get("refsCount", 0)
        except Exception:
            pass  # 导航已成功；快照是附带的奖励

        return json.dumps(result)
    except requests.HTTPError as e:
        return tool_error(f"Navigation failed: {e}", success=False)
    except requests.ConnectionError:
        return json.dumps({
            "success": False,
            "error": f"Cannot connect to Camofox at {get_camofox_url()}. "
                     "Is the server running? Start with: npm start (in camofox-browser dir) "
                     "or: docker run -p 9377:9377 -e CAMOFOX_PORT=9377 jo-inc/camofox-browser",
        })
    except Exception as e:
        return tool_error(str(e), success=False)


def camofox_snapshot(full: bool = False, task_id: Optional[str] = None,
                     user_task: Optional[str] = None) -> str:
    """从 Camofox 获取无障碍树快照。"""
    try:
        session = _get_session(task_id)
        if not session["tab_id"]:
            return tool_error("No browser session. Call browser_navigate first.", success=False)

        data = _get(
            f"/tabs/{session['tab_id']}/snapshot",
            params={"userId": session["user_id"]},
        )

        snapshot = data.get("snapshot", "")
        refs_count = data.get("refsCount", 0)

        # 应用与主浏览器工具相同的摘要逻辑
        from tools.browser_tool import (
            SNAPSHOT_SUMMARIZE_THRESHOLD,
            _extract_relevant_content,
            _truncate_snapshot,
        )

        if len(snapshot) > SNAPSHOT_SUMMARIZE_THRESHOLD:
            if user_task:
                snapshot = _extract_relevant_content(snapshot, user_task)
            else:
                snapshot = _truncate_snapshot(snapshot)

        return json.dumps({
            "success": True,
            "snapshot": snapshot,
            "element_count": refs_count,
        })
    except Exception as e:
        return tool_error(str(e), success=False)


def camofox_click(ref: str, task_id: Optional[str] = None) -> str:
    """通过 Camofox 按 ref 点击一个元素。"""
    try:
        session = _get_session(task_id)
        if not session["tab_id"]:
            return tool_error("No browser session. Call browser_navigate first.", success=False)

        # 去掉可能存在的 @ 前缀（我们的工具约定）
        clean_ref = ref.lstrip("@")

        data = _post(
            f"/tabs/{session['tab_id']}/click",
            {"userId": session["user_id"], "ref": clean_ref},
        )
        return json.dumps({
            "success": True,
            "clicked": clean_ref,
            "url": data.get("url", ""),
        })
    except Exception as e:
        return tool_error(str(e), success=False)


def camofox_type(ref: str, text: str, task_id: Optional[str] = None) -> str:
    """通过 Camofox 按 ref 向元素输入文本。"""
    try:
        session = _get_session(task_id)
        if not session["tab_id"]:
            return tool_error("No browser session. Call browser_navigate first.", success=False)

        clean_ref = ref.lstrip("@")

        _post(
            f"/tabs/{session['tab_id']}/type",
            {"userId": session["user_id"], "ref": clean_ref, "text": text},
        )
        return json.dumps({
            "success": True,
            "typed": text,
            "element": clean_ref,
        })
    except Exception as e:
        return tool_error(str(e), success=False)


def camofox_scroll(direction: str, task_id: Optional[str] = None) -> str:
    """通过 Camofox 滚动页面。"""
    try:
        session = _get_session(task_id)
        if not session["tab_id"]:
            return tool_error("No browser session. Call browser_navigate first.", success=False)

        _post(
            f"/tabs/{session['tab_id']}/scroll",
            {"userId": session["user_id"], "direction": direction},
        )
        return json.dumps({"success": True, "scrolled": direction})
    except Exception as e:
        return tool_error(str(e), success=False)


def camofox_back(task_id: Optional[str] = None) -> str:
    """通过 Camofox 后退导航。"""
    try:
        session = _get_session(task_id)
        if not session["tab_id"]:
            return tool_error("No browser session. Call browser_navigate first.", success=False)

        data = _post(
            f"/tabs/{session['tab_id']}/back",
            {"userId": session["user_id"]},
        )
        return json.dumps({"success": True, "url": data.get("url", "")})
    except Exception as e:
        return tool_error(str(e), success=False)


def camofox_press(key: str, task_id: Optional[str] = None) -> str:
    """通过 Camofox 按下键盘按键。"""
    try:
        session = _get_session(task_id)
        if not session["tab_id"]:
            return tool_error("No browser session. Call browser_navigate first.", success=False)

        _post(
            f"/tabs/{session['tab_id']}/press",
            {"userId": session["user_id"], "key": key},
        )
        return json.dumps({"success": True, "pressed": key})
    except Exception as e:
        return tool_error(str(e), success=False)


def camofox_close(task_id: Optional[str] = None) -> str:
    """通过 Camofox 关闭浏览器会话。"""
    try:
        session = _drop_session(task_id)
        if not session:
            return json.dumps({"success": True, "closed": True})

        _delete(
            f"/sessions/{session['user_id']}",
        )
        return json.dumps({"success": True, "closed": True})
    except Exception as e:
        return json.dumps({"success": True, "closed": True, "warning": str(e)})


def camofox_get_images(task_id: Optional[str] = None) -> str:
    """通过 Camofox 获取当前页面上的图片。

    由于 Camofox 未提供专门的 /images 端点，从无障碍树快照中提取图片信息。
    """
    try:
        session = _get_session(task_id)
        if not session["tab_id"]:
            return tool_error("No browser session. Call browser_navigate first.", success=False)

        import re

        data = _get(
            f"/tabs/{session['tab_id']}/snapshot",
            params={"userId": session["user_id"]},
        )
        snapshot = data.get("snapshot", "")

        # 从无障碍树解析 img 元素。
        # 格式：img "alt text" 或 img "alt text" [eN]
        # URL 出现在 img 条目之后的 /url: 行上
        images = []
        lines = snapshot.split("\n")
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(("- img ", "img ")):
                alt_match = re.search(r'img\s+"([^"]*)"', stripped)
                alt = alt_match.group(1) if alt_match else ""
                # 在下一行查找 URL
                src = ""
                if i + 1 < len(lines):
                    url_match = re.search(r'/url:\s*(\S+)', lines[i + 1].strip())
                    if url_match:
                        src = url_match.group(1)
                if alt or src:
                    images.append({"src": src, "alt": alt})

        return json.dumps({
            "success": True,
            "images": images,
            "count": len(images),
        })
    except Exception as e:
        return tool_error(str(e), success=False)


def camofox_vision(question: str, annotate: bool = False,
                   task_id: Optional[str] = None) -> str:
    """通过 Camofox 截图并用视觉 AI 分析它。"""
    try:
        session = _get_session(task_id)
        if not session["tab_id"]:
            return tool_error("No browser session. Call browser_navigate first.", success=False)

        # 获取二进制 PNG 截图
        resp = _get_raw(
            f"/tabs/{session['tab_id']}/screenshot",
            params={"userId": session["user_id"]},
        )

        # 把截图保存到缓存
        from hermes_constants import get_hermes_home
        screenshots_dir = get_hermes_home() / "browser_screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = str(screenshots_dir / f"browser_screenshot_{uuid.uuid4().hex[:8]}.png")

        with open(screenshot_path, "wb") as f:
            f.write(resp.content)

        # 为视觉 LLM 编码
        img_b64 = base64.b64encode(resp.content).decode("utf-8")

        # 如有请求，也获取带标注的快照
        annotation_context = ""
        if annotate:
            try:
                snap_data = _get(
                    f"/tabs/{session['tab_id']}/snapshot",
                    params={"userId": session["user_id"]},
                )
                annotation_context = f"\n\nAccessibility tree (element refs for interaction):\n{snap_data.get('snapshot', '')[:3000]}"
            except Exception:
                pass

        # 在发送给视觉 LLM 之前，从标注上下文中脱敏掉密钥。
        # 截图图像本身无法脱敏，但至少基于文本的无障碍树片段不会泄漏密钥值。
        from agent.redact import redact_sensitive_text
        annotation_context = redact_sensitive_text(annotation_context)

        # 发送给视觉 LLM
        from agent.auxiliary_client import call_llm

        vision_prompt = (
            f"Analyze this browser screenshot and answer: {question}"
            f"{annotation_context}"
        )

        try:
            _cfg = load_config()
            _vision_cfg = cfg_get(_cfg, "auxiliary", "vision", default={})
            _vision_timeout = float(_vision_cfg.get("timeout", 120))
            _vision_temperature = float(_vision_cfg.get("temperature", 0.1))
        except Exception:
            _vision_timeout = 120.0
            _vision_temperature = 0.1

        response = call_llm(
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": vision_prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{img_b64}",
                        },
                    },
                ],
            }],
            task="vision",
            temperature=_vision_temperature,
            timeout=_vision_timeout,
        )
        analysis = (response.choices[0].message.content or "").strip() if response.choices else ""

        # 脱敏视觉 LLM 可能从截图里读到的密钥。
        from agent.redact import redact_sensitive_text
        analysis = redact_sensitive_text(analysis)

        return json.dumps({
            "success": True,
            "analysis": analysis,
            "screenshot_path": screenshot_path,
        })
    except Exception as e:
        return tool_error(str(e), success=False)


def camofox_console(clear: bool = False, task_id: Optional[str] = None) -> str:
    """获取控制台输出 —— Camofox 中支持有限。

    Camofox 不通过其 REST API 暴露浏览器控制台日志。
    返回带说明的空结果。
    """
    return json.dumps({
        "success": True,
        "console_messages": [],
        "js_errors": [],
        "total_messages": 0,
        "total_errors": 0,
        "note": "Console log capture is not available with the Camofox backend. "
                "Use browser_snapshot or browser_vision to inspect page state.",
    })



