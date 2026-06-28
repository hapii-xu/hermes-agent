#!/usr/bin/env python3
"""
原始 Chrome DevTools Protocol (CDP) 透传工具。

对外暴露单个工具 ``browser_cdp``，用于向浏览器的 DevTools WebSocket 端点发送任意
CDP 命令。在已配置 CDP URL 时可用——可通过 ``/browser connect``（会设置
``BROWSER_CDP_URL``）或 ``config.yaml`` 中的 ``browser.cdp_url`` 配置——或在有基于
CDP 的云服务商会话处于活动状态时也可用。

这是主浏览器工具集（``browser_navigate``、``browser_click``、``browser_console``
等）未覆盖的浏览器操作的逃生通道——例如处理原生对话框、iframe 作用域内的求值、
cookie/网络控制、底层标签页管理等。

方法参考：https://chromedevtools.github.io/devtools-protocol/
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

CDP_DOCS_URL = "https://chromedevtools.github.io/devtools-protocol/"

# ``websockets`` 是 hermes-agent 的直接依赖，因为浏览器 CDP
# 监督器和 browser_dialog 工具在工具发现阶段会导入它。这里把导入
# 包裹起来，以便在环境过期或不完整时能抛出清晰的错误。
try:
    import websockets
    from websockets.exceptions import WebSocketException

    _WS_AVAILABLE = True
except ImportError:
    websockets = None  # type: ignore[assignment]
    WebSocketException = Exception  # type: ignore[assignment,misc]
    _WS_AVAILABLE = False


# ---------------------------------------------------------------------------
# 同步调用中桥接异步函数（与 homeassistant_tool.py 中的模式一致）
# ---------------------------------------------------------------------------


def _run_async(coro):
    """从同步处理器中运行异步协程，无论是否已在事件循环内都安全。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result()
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 端点解析
# ---------------------------------------------------------------------------


def _resolve_cdp_endpoint() -> str:
    """返回归一化后的 CDP WebSocket URL，若不可用则返回空字符串。

    委托给 ``tools.browser_tool._get_cdp_override``，使优先级与浏览器工具集的其余
    部分保持一致：

    1. ``BROWSER_CDP_URL`` 环境变量（通过 ``/browser connect`` 实时覆盖）
    2. ``config.yaml`` 中的 ``browser.cdp_url``
    """
    try:
        from tools.browser_tool import _get_cdp_override  # type: ignore[import-not-found]

        return (_get_cdp_override() or "").strip()
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("browser_cdp: failed to resolve CDP endpoint: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# 核心 CDP 调用
# ---------------------------------------------------------------------------


async def _cdp_call(
    ws_url: str,
    method: str,
    params: Dict[str, Any],
    target_id: Optional[str],
    timeout: float,
) -> Dict[str, Any]:
    """发起一次 CDP 调用，可选择先附加到某个 target。

    当传入 ``target_id`` 时，会调用 ``Target.attachToTarget`` 并设置
    ``flatten=True``，从而在同一个浏览器级 WebSocket 上复用一个页面级会话，
    然后用该 ``sessionId`` 发送 ``method``。
    当 ``target_id`` 为 None 时，``method`` 在浏览器级别发送——这适用于
    ``Target.*``、``Browser.*``、``Storage.*`` 以及其他少数全局作用域的域。
    """
    assert websockets is not None  # 在调用处由 _WS_AVAILABLE 保护

    async with websockets.connect(
        ws_url,
        max_size=None,  # CDP 响应（例如 DOM.getDocument）可能很大
        open_timeout=timeout,
        close_timeout=5,
        ping_interval=None,  # CDP 服务器不期望收到 ping
    ) as ws:
        next_id = 1
        session_id: Optional[str] = None

        # --- 步骤 1：如请求则附加到 target ---
        if target_id:
            attach_id = next_id
            next_id += 1
            await ws.send(
                json.dumps(
                    {
                        "id": attach_id,
                        "method": "Target.attachToTarget",
                        "params": {"targetId": target_id, "flatten": True},
                    }
                )
            )
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Timed out attaching to target {target_id}"
                    )
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("id") == attach_id:
                    if "error" in msg:
                        raise RuntimeError(
                            f"Target.attachToTarget failed: {msg['error']}"
                        )
                    session_id = msg.get("result", {}).get("sessionId")
                    if not session_id:
                        raise RuntimeError(
                            "Target.attachToTarget did not return a sessionId"
                        )
                    break
                # 等待期间忽略事件（不带 "id" 的消息）

        # --- 步骤 2：派发真正的方法 ---
        call_id = next_id
        next_id += 1
        req: Dict[str, Any] = {
            "id": call_id,
            "method": method,
            "params": params or {},
        }
        if session_id:
            req["sessionId"] = session_id
        await ws.send(json.dumps(req))

        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"Timed out waiting for response to {method}"
                )
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            msg = json.loads(raw)
            if msg.get("id") == call_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP error: {msg['error']}")
                return msg.get("result", {})
            # 忽略事件 / 乱序响应


# ---------------------------------------------------------------------------
# 公共工具函数
# ---------------------------------------------------------------------------


def _browser_cdp_via_supervisor(
    task_id: str,
    frame_id: str,
    method: str,
    params: Optional[Dict[str, Any]],
    timeout: float,
) -> str:
    """通过 OOPIF frame 对应的活动监督器会话来路由一次 CDP 调用。

    在监督器的快照中查找该 frame，取出其子级的 ``cdp_session_id``，然后通过
    监督器已连接的 WebSocket（使用 ``asyncio.run_coroutine_threadsafe``
    调度到监督器的事件循环上）以该 sessionId 派发 ``method``。
    """
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover — defensive
        return tool_error(
            f"CDP supervisor is not available: {exc}. frame_id routing requires "
            f"a running supervisor attached via /browser connect or an active "
            f"Browserbase session."
        )

    supervisor = SUPERVISOR_REGISTRY.get(task_id)
    if supervisor is None:
        return tool_error(
            f"No CDP supervisor is attached for task={task_id!r}. Call "
            f"browser_navigate or /browser connect first so the supervisor "
            f"can attach. Once attached, browser_snapshot will populate "
            f"frame_tree with frame_ids you can pass here."
        )

    snap = supervisor.snapshot()
    # 同时在顶层 frame 和子 frame 中查找所请求的 id。
    top = snap.frame_tree.get("top")
    frame_info: Optional[Dict[str, Any]] = None
    if top and top.get("frame_id") == frame_id:
        frame_info = top
    else:
        for child in snap.frame_tree.get("children", []) or []:
            if child.get("frame_id") == frame_id:
                frame_info = child
                break
    if frame_info is None:
        # 同时检查原始 frames 字典（frame_tree 最多保留 30 条）
        with supervisor._state_lock:  # type: ignore[attr-defined]
            raw = supervisor._frames.get(frame_id)  # type: ignore[attr-defined]
        if raw is not None:
            frame_info = raw.to_dict()

    if frame_info is None:
        return tool_error(
            f"frame_id {frame_id!r} not found in supervisor state. "
            f"Call browser_snapshot to see current frame_tree."
        )

    child_sid = frame_info.get("session_id")
    if not child_sid:
        # 不是 OOPIF —— 回退到顶层会话（在页面作用域内求值）。
        # 同源 iframe 不会获得自己的 sessionId；agent 仍可从父级使用
        # contentWindow/contentDocument。
        return tool_error(
            f"frame_id {frame_id!r} is not an out-of-process iframe (no "
            f"dedicated CDP session). For same-origin iframes, use "
            f"`browser_cdp(method='Runtime.evaluate', params={{'expression': "
            f"\"document.querySelector('iframe').contentDocument.title\"}})` "
            f"at the top-level page instead."
        )

    # 派发到监督器的事件循环上。
    loop = supervisor._loop  # type: ignore[attr-defined]
    if loop is None or not loop.is_running():
        return tool_error(
            "CDP supervisor loop is not running. Try reconnecting with "
            "/browser connect."
        )

    async def _do_cdp():
        return await supervisor._cdp(  # type: ignore[attr-defined]
            method,
            params or {},
            session_id=child_sid,
            timeout=timeout,
        )

    try:
        from agent.async_utils import safe_schedule_threadsafe
        fut = safe_schedule_threadsafe(_do_cdp(), loop)
        if fut is None:
            return tool_error(
                "CDP call via supervisor failed: loop unavailable",
                cdp_docs=CDP_DOCS_URL,
            )
        result_msg = fut.result(timeout=timeout + 2)
    except Exception as exc:
        return tool_error(
            f"CDP call via supervisor failed: {type(exc).__name__}: {exc}",
            cdp_docs=CDP_DOCS_URL,
        )

    payload: Dict[str, Any] = {
        "success": True,
        "method": method,
        "frame_id": frame_id,
        "session_id": child_sid,
        "result": result_msg.get("result", {}),
    }
    return json.dumps(payload, ensure_ascii=False)


def browser_cdp(
    method: str,
    params: Optional[Dict[str, Any]] = None,
    target_id: Optional[str] = None,
    frame_id: Optional[str] = None,
    timeout: float = 30.0,
    task_id: Optional[str] = None,
) -> str:
    """发送一条原始 CDP 命令。方法文档见 ``CDP_DOCS_URL``。

    参数：
        method: CDP 方法名，例如 ``"Target.getTargets"``。
        params: 方法专用参数；默认为 ``{}``。
        target_id: 用于页面级方法的可选 target/标签页 ID。设置后，
            会先附加到该 target（``flatten=True``），再用得到的
            ``sessionId`` 发送 ``method``。使用全新的无状态 CDP 连接。
        frame_id: 可选的跨源（OOPIF）iframe ``frame_id``，取自
            ``browser_snapshot.frame_tree.children[]``。设置后（且该
            frame 是被 CDP 监督器跟踪的、拥有活动会话的 OOPIF），会
            通过监督器已有的 WebSocket 路由本次调用——这就是在那些「逐次
            新建 CDP 连接会触发签名 URL 过期（Browserbase）或昂贵重连」
            的后端上，在 iframe *内部* 执行 Runtime.evaluate 的方式。
        timeout: 等待调用完成的秒数。
        task_id: 用于监督器查找的任务标识符。当设置了 ``frame_id`` 时，
            它指定使用哪个任务的监督器；否则处理器默认为 ``"default"``。

    返回：
        成功时返回 JSON 字符串 ``{"success": True, "method": ..., "result": {...}}``，
        失败时返回 ``{"error": "..."}``。
    """
    # --- 将 iframe 作用域的调用经由监督器路由 ---------------
    if frame_id:
        return _browser_cdp_via_supervisor(
            task_id=task_id or "default",
            frame_id=frame_id,
            method=method,
            params=params,
            timeout=timeout,
        )
    del task_id  # 下方为无状态路径

    if not method or not isinstance(method, str):
        return tool_error(
            "'method' is required (e.g. 'Target.getTargets')",
            cdp_docs=CDP_DOCS_URL,
        )

    if not _WS_AVAILABLE:
        return tool_error(
            "The 'websockets' Python package is required but not installed. "
            "Install it with: pip install websockets"
        )

    endpoint = _resolve_cdp_endpoint()
    if not endpoint:
        return tool_error(
            "No CDP endpoint is available. Run '/browser connect' to attach "
            "to a running Chrome, Brave, Chromium, or Edge browser, or set "
            "'browser.cdp_url' in config.yaml. The Camofox backend is REST-only "
            "and does not expose CDP.",
            cdp_docs=CDP_DOCS_URL,
        )

    if not endpoint.startswith(("ws://", "wss://")):
        return tool_error(
            f"CDP endpoint is not a WebSocket URL: {endpoint!r}. "
            "Expected ws://... or wss://... — the /browser connect "
            "resolver should have rewritten this. Check that a Chromium-family "
            "browser is actually listening on the debug port."
        )

    call_params: Dict[str, Any] = params or {}
    if not isinstance(call_params, dict):
        return tool_error(
            f"'params' must be an object/dict, got {type(call_params).__name__}"
        )

    try:
        safe_timeout = float(timeout) if timeout else 30.0
    except (TypeError, ValueError):
        safe_timeout = 30.0
    safe_timeout = max(1.0, min(safe_timeout, 300.0))

    try:
        result = _run_async(
            _cdp_call(endpoint, method, call_params, target_id, safe_timeout)
        )
    except asyncio.TimeoutError as exc:
        return tool_error(
            f"CDP call timed out after {safe_timeout}s: {exc}",
            method=method,
        )
    except TimeoutError as exc:
        return tool_error(str(exc), method=method)
    except RuntimeError as exc:
        return tool_error(str(exc), method=method)
    except WebSocketException as exc:
        return tool_error(
            f"WebSocket error talking to CDP at {endpoint}: {exc}. The "
            "browser may have disconnected — try '/browser connect' again.",
            method=method,
        )
    except Exception as exc:  # pragma: no cover — unexpected
        logger.exception("browser_cdp unexpected error")
        return tool_error(
            f"Unexpected error: {type(exc).__name__}: {exc}",
            method=method,
        )

    payload: Dict[str, Any] = {
        "success": True,
        "method": method,
        "result": result,
    }
    if target_id:
        payload["target_id"] = target_id
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


BROWSER_CDP_SCHEMA: Dict[str, Any] = {
    "name": "browser_cdp",
    "description": (
        "Send a raw Chrome DevTools Protocol (CDP) command. Escape hatch for "
        "browser operations not covered by browser_navigate, browser_click, "
        "browser_console, etc.\n\n"
        "**Requires a reachable CDP endpoint.** Available when the user has "
        "run '/browser connect' to attach to a running Chrome, Brave, Chromium, "
        "or Edge browser, or when 'browser.cdp_url' is set in config.yaml. "
        "Not currently wired up for cloud backends (Browserbase, Browser Use, "
        "Firecrawl) — those expose CDP per session but live-session routing is "
        "a follow-up. Camofox is REST-only and will never support CDP. If the "
        "tool is in your toolset at all, a CDP endpoint is already reachable.\n\n"
        f"**CDP method reference:** {CDP_DOCS_URL} — use web_extract on a "
        "method's URL (e.g. '/tot/Page/#method-handleJavaScriptDialog') "
        "to look up parameters and return shape.\n\n"
        "**Common patterns:**\n"
        "- List tabs: method='Target.getTargets', params={}\n"
        "- Handle a native JS dialog: method='Page.handleJavaScriptDialog', "
        "params={'accept': true, 'promptText': ''}, target_id=<tabId>\n"
        "- Get all cookies: method='Network.getAllCookies', params={}\n"
        "- Eval in a specific tab: method='Runtime.evaluate', "
        "params={'expression': '...', 'returnByValue': true}, "
        "target_id=<tabId>\n"
        "- Set viewport for a tab: method='Emulation.setDeviceMetricsOverride', "
        "params={'width': 1280, 'height': 720, 'deviceScaleFactor': 1, "
        "'mobile': false}, target_id=<tabId>\n\n"
        "**Usage rules:**\n"
        "- Browser-level methods (Target.*, Browser.*, Storage.*): omit "
        "target_id and frame_id.\n"
        "- Page-level methods (Page.*, Runtime.*, DOM.*, Emulation.*, "
        "Network.* scoped to a tab): pass target_id from Target.getTargets.\n"
        "- **Cross-origin iframe scope** (Runtime.evaluate inside an OOPIF, "
        "Page.* targeting a frame target, etc.): pass frame_id from the "
        "browser_snapshot frame_tree output. This routes through the CDP "
        "supervisor's live connection — the only reliable way on "
        "Browserbase where stateless CDP calls hit signed-URL expiry.\n"
        "- Each stateless call (without frame_id) is independent — sessions "
        "and event subscriptions do not persist between calls. For stateful "
        "workflows, prefer the dedicated browser tools or use frame_id "
        "routing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "method": {
                "type": "string",
                "description": (
                    "CDP method name, e.g. 'Target.getTargets', "
                    "'Runtime.evaluate', 'Page.handleJavaScriptDialog'."
                ),
            },
            "params": {
                "type": "object",
                "description": (
                    "Method-specific parameters as a JSON object. Omit or "
                    "pass {} for methods that take no parameters."
                ),
                "properties": {},
                "additionalProperties": True,
            },
            "target_id": {
                "type": "string",
                "description": (
                    "Optional. Target/tab ID from Target.getTargets result "
                    "(each entry's 'targetId'). Use for page-level methods "
                    "at the top-level tab scope. Mutually exclusive with "
                    "frame_id."
                ),
            },
            "frame_id": {
                "type": "string",
                "description": (
                    "Optional. Out-of-process iframe (OOPIF) frame_id from "
                    "browser_snapshot.frame_tree.children[] where "
                    "is_oopif=true. When set, routes the call through the "
                    "CDP supervisor's live session for that iframe. "
                    "Essential for Runtime.evaluate inside cross-origin "
                    "iframes, especially on Browserbase where fresh "
                    "per-call CDP connections can't keep up with signed "
                    "URL rotation. For same-origin iframes, use parent "
                    "contentWindow/contentDocument from Runtime.evaluate "
                    "at the top-level page instead."
                ),
            },
            "timeout": {
                "type": "number",
                "description": (
                    "Timeout in seconds (default 30, max 300)."
                ),
                "default": 30,
            },
        },
        "required": ["method"],
    },
}


def _browser_cdp_check() -> bool:
    """browser_cdp 的可用性检查。

    仅当 Python 端此刻确实能访问到某个 CDP 端点时才提供该工具——即通过
    ``/browser connect``（``BROWSER_CDP_URL``）或 ``config.yaml`` 中的
    ``browser.cdp_url`` 设置了一个静态 URL。

    目前*不*向我们暴露 CDP 的后端——Camofox（仅 REST）、默认的本地
    agent-browser 模式（Playwright 隐藏了其内部 CDP 端口），以及尚未暴露
    每会话 ``cdp_url`` 的云服务商——会被挡在外面，以免模型看到一个必然
    失败的工具。云服务商的 CDP 路由是后续工作。

    保留在一个薄包装函数里，使注册语句保持在模块顶层（工具发现的 AST
    扫描只会拾取顶层的 ``registry.register(...)`` 调用）。
    """
    try:
        from tools.browser_tool import (  # type: ignore[import-not-found]
            _get_cdp_override,
            check_browser_requirements,
        )
    except ImportError as exc:  # pragma: no cover — defensive
        logger.debug("browser_cdp check: browser_tool import failed: %s", exc)
        return False
    if not check_browser_requirements():
        return False
    return bool(_get_cdp_override())


registry.register(
    name="browser_cdp",
    toolset="browser-cdp",
    schema=BROWSER_CDP_SCHEMA,
    handler=lambda args, **kw: browser_cdp(
        method=args.get("method", ""),
        params=args.get("params"),
        target_id=args.get("target_id"),
        frame_id=args.get("frame_id"),
        timeout=args.get("timeout", 30.0),
        task_id=kw.get("task_id"),
    ),
    check_fn=_browser_cdp_check,
    emoji="🧪",
)
