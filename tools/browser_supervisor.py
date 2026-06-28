"""浏览器对话框 + 帧检测的持久化 CDP 监督器。

每个拥有可达 CDP 端点的 Hermes ``task_id`` 对应一个运行中的
``CDPSupervisor``。它持有单一持久化 WebSocket 连接到后端，在每个已附加的
会话上（顶层页面以及每个自动附加的 OOPIF / worker 目标）订阅
``Page`` / ``Runtime`` / ``Target`` 事件，并通过一个线程安全的快照对象
将可观察状态——待处理对话框和帧树——同步暴露给工具处理器消费。

监督器不在 agent 的工具 schema 中。它的输出通过两个渠道到达 agent：

1. ``browser_snapshot`` 将监督器状态合并到其返回负载中
   （见 ``tools/browser_tool.py``）。
2. ``browser_dialog`` 工具通过调用活跃监督器上的
   ``respond_to_dialog()`` 来响应待处理对话框。

设计规范：``website/docs/developer-guide/browser-supervisor.md``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)


# ── 配置默认值 ─────────────────────────────────────────────────────────────────

DIALOG_POLICY_MUST_RESPOND = "must_respond"
DIALOG_POLICY_AUTO_DISMISS = "auto_dismiss"
DIALOG_POLICY_AUTO_ACCEPT = "auto_accept"

_VALID_POLICIES = frozenset(
    {DIALOG_POLICY_MUST_RESPOND, DIALOG_POLICY_AUTO_DISMISS, DIALOG_POLICY_AUTO_ACCEPT}
)

DEFAULT_DIALOG_POLICY = DIALOG_POLICY_MUST_RESPOND
DEFAULT_DIALOG_TIMEOUT_S = 300.0

# frame_tree 的快照上限——在广告密集的页面上保持负载有界。
FRAME_TREE_MAX_ENTRIES = 30
FRAME_TREE_MAX_OOPIF_DEPTH = 2

# 近期 console 级别事件的环形缓冲区（后续由 PR 2 的诊断功能使用）。
CONSOLE_HISTORY_MAX = 50

# 在 ``recent_dialogs`` 中保留最近 N 个已关闭的对话框，这样在服务端
# 自动关闭对话框的后端（例如 Browserbase）上，agent 仍然能观察到
# 对话框曾触发过——即使来不及响应。
RECENT_DIALOGS_MAX = 20

# 注入的对话框桥接 XHR 请求所使用的魔法主机。在任何网络解析发生之前
# 就通过 CDP Fetch 域拦截，因此该主机名永远不必真实存在。请保持其为
# ASCII 且 URL 安全；我们还会基于它来限定 Fetch 的匹配模式。
DIALOG_BRIDGE_HOST = "hermes-dialog-bridge.invalid"
DIALOG_BRIDGE_URL_PATTERN = f"http://{DIALOG_BRIDGE_HOST}/*"

# 通过 Page.addScriptToEvaluateOnNewDocument 注入到每个帧的脚本。
# 它覆盖 alert/confirm/prompt，使其通过一个同步 XHR 往返，我们再通过
# Fetch.requestPaused 拦截该 XHR。这在 Browserbase 上也能工作（其 CDP
# 代理会在我们响应之前自动关闭真正的原生对话框），因为原生对话框根本
# 不会触发——覆盖逻辑具有更高优先级。
_DIALOG_BRIDGE_SCRIPT = r"""
(() => {
  if (window.__hermesDialogBridgeInstalled) return;
  window.__hermesDialogBridgeInstalled = true;
  const ENDPOINT = "http://hermes-dialog-bridge.invalid/";
  function ask(kind, message, defaultPrompt) {
    try {
      const xhr = new XMLHttpRequest();
      // Use GET with query params so we don't need to worry about request
      // body encoding in the Fetch interceptor.
      const params = new URLSearchParams({
        kind: String(kind || ""),
        message: String(message == null ? "" : message),
        default_prompt: String(defaultPrompt == null ? "" : defaultPrompt),
      });
      xhr.open("GET", ENDPOINT + "?" + params.toString(), false);  // sync
      xhr.send(null);
      if (xhr.status !== 200) return null;
      const body = xhr.responseText || "";
      let parsed;
      try { parsed = JSON.parse(body); } catch (e) { return null; }
      if (kind === "alert") return undefined;
      if (kind === "confirm") return Boolean(parsed && parsed.accept);
      if (kind === "prompt") {
        if (!parsed || !parsed.accept) return null;
        return parsed.prompt_text == null ? "" : String(parsed.prompt_text);
      }
      return null;
    } catch (e) {
      // If the bridge is unreachable, fall back to the native call so the
      // page still sees *some* behavior (the backend will auto-dismiss).
      return null;
    }
  }
  const realAlert   = window.alert;
  const realConfirm = window.confirm;
  const realPrompt  = window.prompt;
  window.alert   = function(message) { ask("alert",   message, ""); };
  window.confirm = function(message) {
    const r = ask("confirm", message, "");
    return r === null ? false : Boolean(r);
  };
  window.prompt  = function(message, def) {
    const r = ask("prompt", message, def == null ? "" : def);
    return r === null ? null : String(r);
  };
  // onbeforeunload — we can't really synchronously prompt the user from this
  // event without racing navigation.  Leave native behavior for now; the
  // supervisor's native-dialog fallback path still surfaces them in
  // recent_dialogs.
})();
"""


# ── 数据模型 ────────────────────────────────────────────────────────────────


@dataclass
class PendingDialog:
    """某个帧会话上当前打开的 JS 对话框。"""

    id: str
    type: str  # "alert" | "confirm" | "prompt" | "beforeunload"
    message: str
    default_prompt: str
    opened_at: float
    cdp_session_id: str  # 对话框在哪个已附加的 CDP 会话中触发
    frame_id: Optional[str] = None
    # 设置时，表示该对话框是通过桥接 XHR 路径（Fetch 域）捕获的。
    # 响应必须通过 Fetch.fulfillRequest 交付，而不是
    # Page.handleJavaScriptDialog——原生对话框根本没触发。
    bridge_request_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "message": self.message,
            "default_prompt": self.default_prompt,
            "opened_at": self.opened_at,
            "frame_id": self.frame_id,
        }


@dataclass
class DialogRecord:
    """一个已打开随后被处理的对话框的历史记录。

    保留在 ``recent_dialogs`` 中一小段时间，这样在服务端自动关闭
    对话框的后端（Browserbase）上，agent 仍然能观察到对话框曾触发过，
    即便它来不及响应。
    """

    id: str
    type: str
    message: str
    opened_at: float
    closed_at: float
    closed_by: str  # "agent" | "auto_policy" | "remote" | "watchdog"
    frame_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "message": self.message,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "closed_by": self.closed_by,
            "frame_id": self.frame_id,
        }


@dataclass
class FrameInfo:
    """页面帧树中的一个帧。

    ``is_oopif`` 表示该帧拥有自己的 CDP 目标（独立进程，可通过
    ``cdp_session_id`` 访问）。同源 / srcdoc 的 iframe 共享父进程，
    因此 ``is_oopif=False`` 且 ``cdp_session_id=None``。
    """

    frame_id: str
    url: str
    origin: str
    parent_frame_id: Optional[str]
    is_oopif: bool
    cdp_session_id: Optional[str] = None
    name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "frame_id": self.frame_id,
            "url": self.url,
            "origin": self.origin,
            "is_oopif": self.is_oopif,
        }
        if self.cdp_session_id:
            d["session_id"] = self.cdp_session_id
        if self.parent_frame_id:
            d["parent_frame_id"] = self.parent_frame_id
        if self.name:
            d["name"] = self.name
        return d


@dataclass
class ConsoleEvent:
    """console + 异常流量的环形缓冲区条目。"""

    ts: float
    level: str  # "log" | "error" | "warning" | "exception"
    text: str
    url: Optional[str] = None


@dataclass(frozen=True)
class SupervisorSnapshot:
    """监督器状态的只读快照。

    冻结的 dataclass，这样工具处理器可以自由解引用，无需担心状态
    在使用过程中被修改。
    """

    pending_dialogs: Tuple[PendingDialog, ...]
    recent_dialogs: Tuple[DialogRecord, ...]
    frame_tree: Dict[str, Any]
    console_errors: Tuple[ConsoleEvent, ...]
    active: bool  # 监督器已分离/停止时为 False
    cdp_url: str
    task_id: str

    def to_dict(self) -> Dict[str, Any]:
        """序列化以便包含在 ``browser_snapshot`` 输出中。"""
        out: Dict[str, Any] = {
            "pending_dialogs": [d.to_dict() for d in self.pending_dialogs],
            "frame_tree": self.frame_tree,
        }
        if self.recent_dialogs:
            out["recent_dialogs"] = [d.to_dict() for d in self.recent_dialogs]
        return out


# ── 监督器核心 ───────────────────────────────────────────────────────────────


class CDPSupervisor:
    """每个 (task_id, cdp_url) 对应一个监督器。

    生命周期：
      * ``start()`` —— 由 ``SupervisorRegistry.get_or_start`` 启动；派生
        一个运行自身 asyncio 循环的守护线程，连接 WebSocket，附加到
        第一个页面目标，启用各域，并开始自动附加到子目标。
      * ``snapshot()`` —— 同步、线程安全，由工具处理器调用。
      * ``respond_to_dialog(action, ...)`` —— 同步桥接；在监督器循环上
        调度一个协程并（带超时地）等待 CDP 确认。
      * ``stop()`` —— 取消任务，关闭 WebSocket，join 线程。

    所有 CDP I/O 都在监督器自己的循环上执行。外部调用者绝不直接
    接触该循环；它们通过上面的同步 API 访问。
    """

    def __init__(
        self,
        task_id: str,
        cdp_url: str,
        *,
        dialog_policy: str = DEFAULT_DIALOG_POLICY,
        dialog_timeout_s: float = DEFAULT_DIALOG_TIMEOUT_S,
    ) -> None:
        if dialog_policy not in _VALID_POLICIES:
            raise ValueError(
                f"Invalid dialog_policy {dialog_policy!r}; "
                f"must be one of {sorted(_VALID_POLICIES)}"
            )
        self.task_id = task_id
        self.cdp_url = cdp_url
        self.dialog_policy = dialog_policy
        self.dialog_timeout_s = float(dialog_timeout_s)

        # 受 ``_state_lock`` 保护的状态，用于跨线程读取。
        self._state_lock = threading.Lock()
        self._pending_dialogs: Dict[str, PendingDialog] = {}
        self._recent_dialogs: List[DialogRecord] = []
        self._frames: Dict[str, FrameInfo] = {}
        self._console_events: List[ConsoleEvent] = []
        self._active = False

        # 监督器循环机制——在 start() 中填充。
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready_event = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._stop_requested = False

        # CDP 调用跟踪（仅在监督器循环上运行）。
        self._next_call_id = 1
        self._pending_calls: Dict[int, asyncio.Future] = {}
        self._ws: Optional[ClientConnection] = None
        self._page_session_id: Optional[str] = None
        self._child_sessions: Dict[str, Dict[str, Any]] = {}  # session_id -> 信息

        # 对话框自动关闭看门狗的句柄（按对话框 id）。
        self._dialog_watchdogs: Dict[str, asyncio.TimerHandle] = {}
        # 对话框的单调 id 生成器（在快照中人类可读）。
        self._dialog_seq = 0

    # ── 公共同步 API ────────────────────────────────────────────────────────

    def start(self, timeout: float = 15.0) -> None:
        """启动后台循环并等待，直到附加完成。

        抛出附加失败时的任何异常（连接错误、错误的 WebSocket URL、
        CDP 域启用失败等）。成功时，监督器已完全连接好——从 ``start()``
        返回的那一刻起，就会捕获待处理对话框事件。
        """
        if self._thread and self._thread.is_alive():
            return
        self._ready_event.clear()
        self._start_error = None
        self._stop_requested = False
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"cdp-supervisor-{self.task_id}",
            daemon=True,
        )
        self._thread.start()
        if not self._ready_event.wait(timeout=timeout):
            self.stop()
            raise TimeoutError(
                f"CDP supervisor did not attach within {timeout}s "
                f"(cdp_url={self.cdp_url[:80]}...)"
            )
        if self._start_error is not None:
            err = self._start_error
            self.stop()
            raise err

    def stop(self, timeout: float = 5.0) -> None:
        """取消监督器任务并 join 线程。"""
        self._stop_requested = True
        loop = self._loop
        if loop is not None and loop.is_running():
            # 在循环内部关闭 WebSocket——这让 ``async for raw in self._ws``
            # 干净地返回，``_run`` 命中其 ``finally``，待处理任务按顺序被
            # 取消，然后线程才退出。
            async def _close_ws():
                ws = self._ws
                self._ws = None
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass

            try:
                from agent.async_utils import safe_schedule_threadsafe
                fut = safe_schedule_threadsafe(_close_ws(), loop)
                if fut is not None:
                    try:
                        fut.result(timeout=2.0)
                    except Exception:
                        pass
            except RuntimeError:
                pass  # 循环已在关闭中
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        with self._state_lock:
            self._active = False

    def snapshot(self) -> SupervisorSnapshot:
        """返回当前状态的不可变快照。"""
        with self._state_lock:
            dialogs = tuple(self._pending_dialogs.values())
            recent = tuple(self._recent_dialogs[-RECENT_DIALOGS_MAX:])
            frames_tree = self._build_frame_tree_locked()
            console = tuple(self._console_events[-CONSOLE_HISTORY_MAX:])
            active = self._active
        return SupervisorSnapshot(
            pending_dialogs=dialogs,
            recent_dialogs=recent,
            frame_tree=frames_tree,
            console_errors=console,
            active=active,
            cdp_url=self.cdp_url,
            task_id=self.task_id,
        )

    def respond_to_dialog(
        self,
        action: str,
        *,
        prompt_text: Optional[str] = None,
        dialog_id: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """接受/关闭一个待处理对话框。桥接到监督器循环的同步接口。

        成功时返回 ``{"ok": True, "dialog": {...}}``，
        可恢复错误（无对话框、dialog_id 有歧义、监督器未激活）时返回
        ``{"ok": False, "error": "..."}``。
        """
        if action not in {"accept", "dismiss"}:
            return {"ok": False, "error": f"action must be 'accept' or 'dismiss', got {action!r}"}

        with self._state_lock:
            if not self._active:
                return {"ok": False, "error": "supervisor is not active"}
            pending = list(self._pending_dialogs.values())
            if not pending:
                return {"ok": False, "error": "no dialog is currently open"}
            if dialog_id:
                dialog = self._pending_dialogs.get(dialog_id)
                if dialog is None:
                    return {
                        "ok": False,
                        "error": f"dialog_id {dialog_id!r} not found "
                        f"(known: {sorted(self._pending_dialogs)})",
                    }
            elif len(pending) > 1:
                return {
                    "ok": False,
                    "error": (
                        f"{len(pending)} pending dialogs; specify dialog_id. "
                        f"Candidates: {[d.id for d in pending]}"
                    ),
                }
            else:
                dialog = pending[0]
            snapshot_copy = dialog

        loop = self._loop
        if loop is None:
            return {"ok": False, "error": "supervisor loop is not running"}

        async def _do_respond():
            return await self._handle_dialog_cdp(
                snapshot_copy, accept=(action == "accept"), prompt_text=prompt_text or ""
            )

        try:
            from agent.async_utils import safe_schedule_threadsafe
            fut = safe_schedule_threadsafe(_do_respond(), loop)
            if fut is None:
                return {"ok": False, "error": "Browser supervisor loop unavailable"}
            fut.result(timeout=timeout)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "dialog": snapshot_copy.to_dict()}

    def evaluate_runtime(
        self,
        expression: str,
        *,
        return_by_value: bool = True,
        await_promise: bool = True,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """在页面的 Runtime 上下文中通过活跃 WS 求值 ``expression``。

        复用监督器已连接的 WebSocket——相比 agent-browser CLI 的 ``eval``
        命令（每次调用都要 fork+exec+Node 启动+CDP 建立），零子进程
        启动开销。

        成功时返回形如 ``{"ok": True, "result": <value>, "result_type": "..."}``
        的字典，失败时返回 ``{"ok": False, "error": "..."}``。

        ``return_by_value=True`` 要求浏览器在返回结果前先做 JSON 序列化，
        对基本类型 / 普通对象表达式这与 DevTools 控制台的语义一致。对于
        DOM 节点或不可序列化的对象，浏览器会在 ``result_type`` 中返回一段
        描述字符串。
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            return {"ok": False, "error": "supervisor loop is not running"}

        with self._state_lock:
            if not self._active:
                return {"ok": False, "error": "supervisor is not active"}
            session_id = self._page_session_id

        if not session_id:
            return {"ok": False, "error": "supervisor has no attached page session"}

        async def _do_eval(by_value: bool) -> Dict[str, Any]:
            return await self._cdp(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "returnByValue": by_value,
                    "awaitPromise": await_promise,
                    # userGesture 对于剪贴板 / 全屏等需要用户激活上下文的
                    # API 很重要。
                    "userGesture": True,
                },
                session_id=session_id,
                timeout=timeout,
            )

        from agent.async_utils import safe_schedule_threadsafe

        def _run_eval(by_value: bool) -> Dict[str, Any]:
            fut = safe_schedule_threadsafe(_do_eval(by_value), loop)
            if fut is None:
                raise RuntimeError("Browser supervisor loop unavailable")
            return fut.result(timeout=timeout + 1)

        try:
            response = _run_eval(return_by_value)
        except Exception as exc:
            # ``returnByValue=True`` 要求 Chrome 对结果做深度序列化。
            # 对于活跃的 DOM 节点 / NodeList / Window，这种序列化可能
            # 超出 CDP 的递归保护，从而以
            # ``Object reference chain is too long``（协议级错误，而非 JS
            # 异常）使整个调用失败。这里用 ``returnByValue=False`` 重试一次，
            # 让 Chrome 返回该对象的描述字符串——即 ``document.querySelector(...)``
            # 结果所使用的同一条优雅降级路径——而不是让求值崩溃。
            if return_by_value and "reference chain is too long" in str(exc).lower():
                try:
                    response = _run_eval(False)
                except Exception as exc2:
                    return {"ok": False, "error": f"{type(exc2).__name__}: {exc2}"}
            else:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        # Runtime.evaluate 的响应结构：
        #   {"id": N, "result": {"result": {"type": "...", "value": ..., ...},
        #                         "exceptionDetails": {...} (仅出错时存在)}}
        result_payload = response.get("result", {}) if isinstance(response, dict) else {}
        exception_details = result_payload.get("exceptionDetails")
        if exception_details:
            # 用一条干净的消息把 JS 侧的异常暴露出来。
            exc_text = exception_details.get("text") or "JavaScript exception"
            exc_obj = exception_details.get("exception") or {}
            description = exc_obj.get("description")
            if description:
                exc_text = f"{exc_text}: {description}"
            return {"ok": False, "error": exc_text}

        result_obj = result_payload.get("result", {})
        result_type = result_obj.get("type", "undefined")

        if "value" in result_obj:
            value = result_obj["value"]
        elif result_type == "undefined":
            value = None
        else:
            # 不可序列化（函数、DOM 节点等）——返回浏览器的字符串描述，
            # 让模型至少拿到*一些*信息。
            value = result_obj.get("description") or result_obj.get("unserializableValue")

        return {"ok": True, "result": value, "result_type": result_type}

    # ── 监督器循环内部实现 ──────────────────────────────────────────────────

    def _thread_main(self) -> None:
        """监督器专用线程的入口点。"""
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._run())
        except BaseException as e:  # noqa: BLE001 — 通过 _start_error 传播
            if not self._ready_event.is_set():
                self._start_error = e
                self._ready_event.set()
            else:
                logger.warning("CDP supervisor %s crashed: %s", self.task_id, e)
        finally:
            # 在关闭循环前清理所有剩余任务，以免触发
            # "Task was destroyed but it is pending" 警告。
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass
            with self._state_lock:
                self._active = False

    async def _run(self) -> None:
        """监督器顶层协程。

        持有一个可重连的循环，以便在远端关闭 WebSocket 时存活下来——
        尤其是 Browserbase，每当一个短生命周期的客户端（例如
        agent-browser 的逐命令 CDP 客户端）断开时，它都会拆掉 CDP
        socket。我们会丢弃依赖于特定 CDP 会话 id 的状态快照键，
        重新附加，然后继续运行。
        """
        attempt = 0
        last_success_at = 0.0
        backoff = 0.5
        while not self._stop_requested:
            try:
                self._ws = await asyncio.wait_for(
                    websockets.connect(self.cdp_url, max_size=50 * 1024 * 1024),
                    timeout=10.0,
                )
            except Exception as e:
                attempt += 1
                if not self._ready_event.is_set():
                    # 从未连接成功过——对 start() 而言是致命错误。
                    self._start_error = e
                    self._ready_event.set()
                    return
                logger.warning(
                    "CDP supervisor %s: connect failed (attempt %s): %s",
                    self.task_id, attempt, e,
                )
                await asyncio.sleep(min(backoff, 10.0))
                backoff = min(backoff * 2, 10.0)
                continue

            reader_task = asyncio.create_task(self._read_loop(), name="cdp-reader")
            try:
                # 重置每连接的会话状态，以免过期的 id 在重连后残留。
                self._page_session_id = None
                self._child_sessions.clear()
                # 我们刻意保留 `_pending_dialogs` 和 `_frames`——
                # 它们会在监督器重新订阅并收到新事件时被对账。最坏的
                # 情况：agent 看到一条过期的对话框条目，而新会话的
                # handleJavaScriptDialog 调用以 "no dialog is showing"
                # 拒绝（记入日志，不暴露给上层）。
                await self._attach_initial_page()
                with self._state_lock:
                    self._active = True
                last_success_at = time.time()
                backoff = 0.5  # 成功附加后重置退避
                if not self._ready_event.is_set():
                    self._ready_event.set()
                # 运行直到读取循环返回。
                await reader_task
            except BaseException as e:
                if not self._ready_event.is_set():
                    # 从未就绪过——传播给 start()。
                    self._start_error = e
                    self._ready_event.set()
                    raise
                logger.warning(
                    "CDP supervisor %s: session dropped after %.1fs: %s",
                    self.task_id,
                    time.time() - last_success_at,
                    e,
                )
            finally:
                with self._state_lock:
                    self._active = False
                if not reader_task.done():
                    reader_task.cancel()
                    try:
                        await reader_task
                    except (asyncio.CancelledError, Exception):
                        pass
                for handle in list(self._dialog_watchdogs.values()):
                    handle.cancel()
                self._dialog_watchdogs.clear()
                ws = self._ws
                self._ws = None
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass

            if self._stop_requested:
                return

            # 重连：短暂退避后重新附加。
            logger.debug(
                "CDP supervisor %s: reconnecting in %.1fs...", self.task_id, backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    async def _attach_initial_page(self) -> None:
        """查找页面目标，附加扁平会话，启用各域，并安装对话框桥接。"""
        resp = await self._cdp("Target.getTargets")
        targets = resp.get("result", {}).get("targetInfos", [])
        page_target = next((t for t in targets if t.get("type") == "page"), None)
        if page_target is None:
            created = await self._cdp("Target.createTarget", {"url": "about:blank"})
            target_id = created["result"]["targetId"]
        else:
            target_id = page_target["targetId"]

        attach = await self._cdp(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
        )
        self._page_session_id = attach["result"]["sessionId"]
        await self._cdp("Page.enable", session_id=self._page_session_id)
        await self._cdp("Runtime.enable", session_id=self._page_session_id)
        await self._cdp(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
            session_id=self._page_session_id,
        )
        # 安装对话框桥接——用我们通过 Fetch 域拦截的同步 XHR 覆盖原生
        # alert/confirm/prompt。这就是我们让对话框响应在 Browserbase 上
        # 也能工作的方式（其 CDP 代理会在我们调用 handleJavaScriptDialog
        # 之前就自动关闭真正的原生对话框）。
        await self._install_dialog_bridge(self._page_session_id)

    async def _install_dialog_bridge(self, session_id: str) -> None:
        """在某个会话上安装对话框桥接初始化脚本 + Fetch 拦截器。

        两次 CDP 调用：
          1. ``Page.addScriptToEvaluateOnNewDocument`` —— JS 覆盖逻辑在
             每个帧中、任何页面脚本之前运行。用发往我们桥接 URL 的同步
             XHR 替换 alert/confirm/prompt。
          2. ``Fetch.enable``，作用域限定到桥接 URL——我们捕获这些 XHR，
             将其作为待处理对话框暴露出来，待 agent 响应后再履行。

        在 CDP 层面是幂等的：Chromium 会按源码对相同的 add-script 调用
        去重，而 Fetch.enable 会替换之前的匹配模式。
        """
        try:
            await self._cdp(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": _DIALOG_BRIDGE_SCRIPT, "runImmediately": True},
                session_id=session_id,
                timeout=5.0,
            )
        except Exception as e:
            logger.debug(
                "dialog bridge: addScriptToEvaluateOnNewDocument failed on sid=%s: %s",
                (session_id or "")[:16], e,
            )
        try:
            await self._cdp(
                "Fetch.enable",
                {
                    "patterns": [
                        {
                            "urlPattern": DIALOG_BRIDGE_URL_PATTERN,
                            "requestStage": "Request",
                        }
                    ],
                    "handleAuthRequests": False,
                },
                session_id=session_id,
                timeout=5.0,
            )
        except Exception as e:
            logger.debug(
                "dialog bridge: Fetch.enable failed on sid=%s: %s",
                (session_id or "")[:16], e,
            )
        # 同时尝试注入到已加载的文档中，以便已有页面在重连时也能
        # 应用覆盖逻辑。尽力而为。
        try:
            await self._cdp(
                "Runtime.evaluate",
                {"expression": _DIALOG_BRIDGE_SCRIPT, "returnByValue": True},
                session_id=session_id,
                timeout=3.0,
            )
        except Exception:
            pass

    async def _cdp(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        session_id: Optional[str] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """发送一条 CDP 命令并等待其响应。"""
        if self._ws is None:
            raise RuntimeError("supervisor WebSocket is not connected")
        call_id = self._next_call_id
        self._next_call_id += 1
        payload: Dict[str, Any] = {"id": call_id, "method": method}
        if params:
            payload["params"] = params
        if session_id:
            payload["sessionId"] = session_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_calls[call_id] = fut
        await self._ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending_calls.pop(call_id, None)

    async def _read_loop(self) -> None:
        """持续分发传入的 CDP 帧。"""
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if self._stop_requested:
                    break
                try:
                    msg = json.loads(raw)
                except Exception:
                    logger.debug("CDP supervisor: non-JSON frame dropped")
                    continue
                if "id" in msg:
                    fut = self._pending_calls.pop(msg["id"], None)
                    if fut is not None and not fut.done():
                        if "error" in msg:
                            fut.set_exception(
                                RuntimeError(f"CDP error on id={msg['id']}: {msg['error']}")
                            )
                        else:
                            fut.set_result(msg)
                elif "method" in msg:
                    await self._on_event(msg["method"], msg.get("params", {}), msg.get("sessionId"))
        except Exception as e:
            logger.debug("CDP read loop exited: %s", e)

    # ── 事件分发 ────────────────────────────────────────────────────────────

    async def _on_event(
        self, method: str, params: Dict[str, Any], session_id: Optional[str]
    ) -> None:
        if method == "Page.javascriptDialogOpening":
            await self._on_dialog_opening(params, session_id)
        elif method == "Page.javascriptDialogClosed":
            await self._on_dialog_closed(params, session_id)
        elif method == "Fetch.requestPaused":
            await self._on_fetch_paused(params, session_id)
        elif method == "Page.frameAttached":
            self._on_frame_attached(params, session_id)
        elif method == "Page.frameNavigated":
            self._on_frame_navigated(params, session_id)
        elif method == "Page.frameDetached":
            self._on_frame_detached(params, session_id)
        elif method == "Target.attachedToTarget":
            await self._on_target_attached(params)
        elif method == "Target.detachedFromTarget":
            self._on_target_detached(params)
        elif method == "Runtime.consoleAPICalled":
            self._on_console(params, level_from="api")
        elif method == "Runtime.exceptionThrown":
            self._on_console(params, level_from="exception")

    async def _on_dialog_opening(
        self, params: Dict[str, Any], session_id: Optional[str]
    ) -> None:
        self._dialog_seq += 1
        dialog = PendingDialog(
            id=f"d-{self._dialog_seq}",
            type=str(params.get("type") or ""),
            message=str(params.get("message") or ""),
            default_prompt=str(params.get("defaultPrompt") or ""),
            opened_at=time.time(),
            cdp_session_id=session_id or self._page_session_id or "",
            frame_id=params.get("frameId"),
        )

        if self.dialog_policy == DIALOG_POLICY_AUTO_DISMISS:
            # 立即归档并打上策略标签，这样在我们调用 handleJavaScriptDialog
            # 之后紧接着到达的 ``closed`` 事件不会把它再次归档为 "remote"。
            with self._state_lock:
                self._archive_dialog_locked(dialog, "auto_policy")
            asyncio.create_task(
                self._auto_handle_dialog(dialog, accept=False, prompt_text="")
            )
        elif self.dialog_policy == DIALOG_POLICY_AUTO_ACCEPT:
            with self._state_lock:
                self._archive_dialog_locked(dialog, "auto_policy")
            asyncio.create_task(
                self._auto_handle_dialog(
                    dialog, accept=True, prompt_text=dialog.default_prompt
                )
            )
        else:
            # must_respond → 加入待处理并启动看门狗。
            with self._state_lock:
                self._pending_dialogs[dialog.id] = dialog
            loop = asyncio.get_running_loop()
            handle = loop.call_later(
                self.dialog_timeout_s,
                lambda: asyncio.create_task(self._dialog_timeout_expired(dialog.id)),
            )
            self._dialog_watchdogs[dialog.id] = handle

    async def _auto_handle_dialog(
        self, dialog: PendingDialog, *, accept: bool, prompt_text: str
    ) -> None:
        """为 auto_dismiss/auto_accept 发送 handleJavaScriptDialog。

        对话框已由调用方（``_on_dialog_opening``）归档；这里只是发出
        CDP 调用以解除页面的阻塞。
        """
        params: Dict[str, Any] = {"accept": accept}
        if dialog.type == "prompt":
            params["promptText"] = prompt_text
        try:
            await self._cdp(
                "Page.handleJavaScriptDialog",
                params,
                session_id=dialog.cdp_session_id or None,
                timeout=5.0,
            )
        except Exception as e:
            logger.debug("auto-handle CDP call failed for %s: %s", dialog.id, e)

    async def _dialog_timeout_expired(self, dialog_id: str) -> None:
        with self._state_lock:
            dialog = self._pending_dialogs.get(dialog_id)
        if dialog is None:
            return
        logger.warning(
            "CDP supervisor %s: dialog %s (%s) auto-dismissed after %ss timeout",
            self.task_id,
            dialog_id,
            dialog.type,
            self.dialog_timeout_s,
        )
        try:
            # 在履行 / 关闭之前先用看门狗标签归档。
            with self._state_lock:
                if dialog_id in self._pending_dialogs:
                    self._pending_dialogs.pop(dialog_id, None)
                    self._archive_dialog_locked(dialog, "watchdog")
            # 解除页面阻塞——桥接对话框走 Fetch 履行路径，
            # 真正的原生对话框则走 Page.handleJavaScriptDialog。
            if dialog.bridge_request_id:
                await self._fulfill_bridge_request(dialog, accept=False, prompt_text="")
            else:
                await self._cdp(
                    "Page.handleJavaScriptDialog",
                    {"accept": False},
                    session_id=dialog.cdp_session_id or None,
                    timeout=5.0,
                )
        except Exception as e:
            logger.debug("auto-dismiss failed for %s: %s", dialog_id, e)

    def _archive_dialog_locked(self, dialog: PendingDialog, closed_by: str) -> None:
        """把一个待处理对话框移入 recent_dialogs 环形缓冲区。必须持有 state_lock。"""
        record = DialogRecord(
            id=dialog.id,
            type=dialog.type,
            message=dialog.message,
            opened_at=dialog.opened_at,
            closed_at=time.time(),
            closed_by=closed_by,
            frame_id=dialog.frame_id,
        )
        self._recent_dialogs.append(record)
        if len(self._recent_dialogs) > RECENT_DIALOGS_MAX * 2:
            self._recent_dialogs = self._recent_dialogs[-RECENT_DIALOGS_MAX:]

    async def _handle_dialog_cdp(
        self, dialog: PendingDialog, *, accept: bool, prompt_text: str
    ) -> None:
        """发送 Page.handleJavaScriptDialog CDP 命令（仅 agent 路径）。

        当对话框是通过注入的 XHR 覆盖捕获时（见 ``_on_fetch_paused``），
        路由到桥接履行路径。
        """
        if dialog.bridge_request_id:
            try:
                await self._fulfill_bridge_request(
                    dialog, accept=accept, prompt_text=prompt_text
                )
            finally:
                with self._state_lock:
                    if dialog.id in self._pending_dialogs:
                        self._pending_dialogs.pop(dialog.id, None)
                        self._archive_dialog_locked(dialog, "agent")
                handle = self._dialog_watchdogs.pop(dialog.id, None)
                if handle is not None:
                    handle.cancel()
            return

        params: Dict[str, Any] = {"accept": accept}
        if dialog.type == "prompt":
            params["promptText"] = prompt_text
        try:
            await self._cdp(
                "Page.handleJavaScriptDialog",
                params,
                session_id=dialog.cdp_session_id or None,
                timeout=5.0,
            )
        finally:
            # 无论成败都清理——CDP 出错通常意味着对话框已经关闭
            # （浏览器在导航等之后自动关闭）。
            with self._state_lock:
                if dialog.id in self._pending_dialogs:
                    self._pending_dialogs.pop(dialog.id, None)
                    self._archive_dialog_locked(dialog, "agent")
            handle = self._dialog_watchdogs.pop(dialog.id, None)
            if handle is not None:
                handle.cancel()

    async def _on_dialog_closed(
        self, params: Dict[str, Any], session_id: Optional[str]
    ) -> None:
        # ``Page.javascriptDialogClosed`` 规范里只有 ``result``（bool）和
        # ``userInput``（字符串），没有原始的 ``message``。按会话 id 匹配并
        # 清除该会话上最早的对话框——如果 Chrome 替我们关闭了一个（例如
        # 我们的断开导致自动关闭，或浏览器发生了导航，或 Browserbase 的 CDP
        # 代理自动关闭），每个会话同时不应该有多个在飞行的对话框，因为
        # 对话框存在时 JS 线程是被阻塞的。
        with self._state_lock:
            candidate_ids = [
                d.id
                for d in self._pending_dialogs.values()
                if d.cdp_session_id == session_id
                # 桥接捕获的对话框不会被原生关闭事件清除；它们通过
                # Fetch.fulfillRequest 来解决。只有真正的原生对话框路径
                # 才使用 Page.javascriptDialogClosed。
                and d.bridge_request_id is None
            ]
            if candidate_ids:
                did = candidate_ids[0]
                dialog = self._pending_dialogs.pop(did, None)
                if dialog is not None:
                    self._archive_dialog_locked(dialog, "remote")
                handle = self._dialog_watchdogs.pop(did, None)
                if handle is not None:
                    handle.cancel()

    async def _on_fetch_paused(
        self, params: Dict[str, Any], session_id: Optional[str]
    ) -> None:
        """桥接 XHR 在传输途中被捕获——物化为一个待处理对话框。

        注入的脚本（``_DIALOG_BRIDGE_SCRIPT``）在页面代码调用
        alert/confirm/prompt 时会向 ``DIALOG_BRIDGE_HOST`` 发起一个同步
        XHR。我们通过 Fetch.enable 的匹配模式捕获它；页面的 JS 线程会
        阻塞在该 XHR 的响应上，直到我们调用 Fetch.fulfillRequest（从
        ``respond_to_dialog`` 中发生）或看门狗触发（此时我们用取消响应
        来履行）。
        """
        url = str(params.get("request", {}).get("url") or "")
        request_id = params.get("requestId")
        if not request_id:
            return
        # 只关心我们的桥接 URL。如果匹配模式被放宽，Fetch 仍可能投递
        # 其他被拦截的请求。
        if DIALOG_BRIDGE_HOST not in url:
            # 不是我们的——原样放行，让页面看到自己的请求。
            try:
                await self._cdp(
                    "Fetch.continueRequest", {"requestId": request_id},
                    session_id=session_id, timeout=3.0,
                )
            except Exception:
                pass
            return

        # 解析查询字符串中的对话框元数据。用 urllib 以保证健壮性。
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(url).query)

        def _q(name: str) -> str:
            v = q.get(name, [""])
            return v[0] if v else ""

        kind = _q("kind") or "alert"
        message = _q("message")
        default_prompt = _q("default_prompt")

        self._dialog_seq += 1
        dialog = PendingDialog(
            id=f"d-{self._dialog_seq}",
            type=kind,
            message=message,
            default_prompt=default_prompt,
            opened_at=time.time(),
            cdp_session_id=session_id or self._page_session_id or "",
            frame_id=params.get("frameId"),
            bridge_request_id=str(request_id),
        )

        # 与原生对话框完全一样地应用策略。
        if self.dialog_policy == DIALOG_POLICY_AUTO_DISMISS:
            with self._state_lock:
                self._archive_dialog_locked(dialog, "auto_policy")
            asyncio.create_task(
                self._fulfill_bridge_request(dialog, accept=False, prompt_text="")
            )
        elif self.dialog_policy == DIALOG_POLICY_AUTO_ACCEPT:
            with self._state_lock:
                self._archive_dialog_locked(dialog, "auto_policy")
            asyncio.create_task(
                self._fulfill_bridge_request(
                    dialog, accept=True, prompt_text=default_prompt
                )
            )
        else:
            # must_respond —— 加入待处理并启动看门狗。
            with self._state_lock:
                self._pending_dialogs[dialog.id] = dialog
            loop = asyncio.get_running_loop()
            handle = loop.call_later(
                self.dialog_timeout_s,
                lambda: asyncio.create_task(self._dialog_timeout_expired(dialog.id)),
            )
            self._dialog_watchdogs[dialog.id] = handle

    async def _fulfill_bridge_request(
        self, dialog: PendingDialog, *, accept: bool, prompt_text: str
    ) -> None:
        """通过 Fetch.fulfillRequest 解决一个桥接 XHR，以解除页面阻塞。"""
        if not dialog.bridge_request_id:
            return
        payload = {
            "accept": bool(accept),
            "prompt_text": prompt_text if dialog.type == "prompt" else "",
            "dialog_id": dialog.id,
        }
        body = json.dumps(payload).encode()
        try:
            import base64 as _b64
            await self._cdp(
                "Fetch.fulfillRequest",
                {
                    "requestId": dialog.bridge_request_id,
                    "responseCode": 200,
                    "responseHeaders": [
                        {"name": "Content-Type", "value": "application/json"},
                        {"name": "Access-Control-Allow-Origin", "value": "*"},
                    ],
                    "body": _b64.b64encode(body).decode(),
                },
                session_id=dialog.cdp_session_id or None,
                timeout=5.0,
            )
        except Exception as e:
            logger.debug("bridge fulfill failed for %s: %s", dialog.id, e)

    # ── 帧 / 目标跟踪 ─────────────────────────────────────────────────────

    def _on_frame_attached(
        self, params: Dict[str, Any], session_id: Optional[str]
    ) -> None:
        frame_id = params.get("frameId")
        if not frame_id:
            return
        with self._state_lock:
            self._frames[frame_id] = FrameInfo(
                frame_id=frame_id,
                url="",
                origin="",
                parent_frame_id=params.get("parentFrameId"),
                is_oopif=False,
                cdp_session_id=session_id,
            )

    def _on_frame_navigated(
        self, params: Dict[str, Any], session_id: Optional[str]
    ) -> None:
        frame = params.get("frame") or {}
        frame_id = frame.get("id")
        if not frame_id:
            return
        with self._state_lock:
            existing = self._frames.get(frame_id)
            info = FrameInfo(
                frame_id=frame_id,
                url=str(frame.get("url") or ""),
                origin=str(frame.get("securityOrigin") or frame.get("origin") or ""),
                parent_frame_id=frame.get("parentId") or (existing.parent_frame_id if existing else None),
                is_oopif=bool(existing.is_oopif if existing else False),
                cdp_session_id=existing.cdp_session_id if existing else session_id,
                name=str(frame.get("name") or (existing.name if existing else "")),
            )
            self._frames[frame_id] = info

    def _on_frame_detached(
        self, params: Dict[str, Any], session_id: Optional[str]
    ) -> None:
        """仅当帧真正消失时才从我们的状态中移除它。

        CDP 发出 ``Page.frameDetached`` 时带有一个 ``reason``，取值为
        ``"remove"``（帧确实从 DOM 中消失了）或 ``"swap"``（帧正在迁移
        到新进程——典型场景是同进程 iframe 变为 OOPIF，或历史记录导航）。
        在 ``swap`` 时丢弃会在 Chromium 把它们提升为独立进程的那一刻
        把 OOPIF 对 agent 隐藏掉，因此把 swap 当作无操作处理。

        即使 ``reason=remove``，从父页面的角度看也是"子帧离开了我的
        进程树"——这正是同源 iframe 被提升为 OOPIF 时发生的情况。如果
        我们已经为该 frame_id 附加了一个活跃的子 CDP 会话，该帧其实
        仍然存活；只有当我们没有会话记录时才丢弃它。
        """
        frame_id = params.get("frameId")
        if not frame_id:
            return
        reason = str(params.get("reason") or "remove").lower()
        if reason == "swap":
            return
        with self._state_lock:
            existing = self._frames.get(frame_id)
            # 即使父级说帧被 "移除" 也保留 OOPIF 记录——iframe 仍然可见，
            # 只是在不同的进程中。如果帧后来真的消失了，Target.detached
            # 加上下一次没有活跃会话的 Page.frameDetached 会清除它。
            if existing and existing.is_oopif and existing.cdp_session_id:
                return
            self._frames.pop(frame_id, None)

    async def _on_target_attached(self, params: Dict[str, Any]) -> None:
        info = params.get("targetInfo") or {}
        sid = params.get("sessionId")
        target_type = info.get("type")
        if not sid or target_type not in {"iframe", "worker"}:
            return
        self._child_sessions[sid] = {"info": info, "type": target_type}

        # 记录该帧及其 OOPIF 会话 id，用于交互路由。
        if target_type == "iframe":
            target_id = info.get("targetId")
            with self._state_lock:
                existing = self._frames.get(target_id)
                self._frames[target_id] = FrameInfo(
                    frame_id=target_id,
                    url=str(info.get("url") or ""),
                    origin="",  # 由子会话上的 frameNavigated 填充
                    parent_frame_id=(existing.parent_frame_id if existing else None),
                    is_oopif=True,
                    cdp_session_id=sid,
                    name=str(info.get("title") or (existing.name if existing else "")),
                )

        # 在循环之外启用子会话的各域，这样读取循环可以继续泵送消息。
        # 在这里 await CDP 回复会死锁，因为只有读取循环才能解决这些回复
        # 的 Future。
        asyncio.create_task(self._enable_child_domains(sid))

    async def _enable_child_domains(self, sid: str) -> None:
        """在子 CDP 会话上启用 Page+Runtime（+ 嵌套的 setAutoAttach）。

        同时安装对话框桥接，让 iframe 作用域内的 alert/confirm/prompt
        调用也能通过 Fetch 往返。
        """
        try:
            await self._cdp("Page.enable", session_id=sid, timeout=3.0)
            await self._cdp("Runtime.enable", session_id=sid, timeout=3.0)
            await self._cdp(
                "Target.setAutoAttach",
                {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
                session_id=sid,
                timeout=3.0,
            )
        except Exception as e:
            logger.debug("child session %s setup failed: %s", sid[:16], e)
        # 在子会话上安装对话框桥接，以便捕获 iframe 内的对话框。
        await self._install_dialog_bridge(sid)

    def _on_target_detached(self, params: Dict[str, Any]) -> None:
        """处理子 CDP 会话的分离。

        我们刻意不在这里从 ``_frames`` 中丢弃帧——Browserbase 在页面
        切换期间会发出瞬时的分离事件，即便 iframe 对用户仍然可见，而
        丢弃记录会在分离到下一次 ``Target.attachedToTarget`` 之间把
        OOPIF 对 agent 隐藏。相反，我们只清除会话绑定，以免过期的
        ``cdp_session_id`` 值被用于路由。如果 iframe 真的消失了，
        ``Page.frameDetached`` 会负责清理。
        """
        sid = params.get("sessionId")
        if not sid:
            return
        self._child_sessions.pop(sid, None)
        with self._state_lock:
            for fid, frame in list(self._frames.items()):
                if frame.cdp_session_id == sid:
                    # 用一个清除了 cdp_session_id 的副本替换，这样重试时
                    # 路由会回退到顶层页面会话。
                    self._frames[fid] = FrameInfo(
                        frame_id=frame.frame_id,
                        url=frame.url,
                        origin=frame.origin,
                        parent_frame_id=frame.parent_frame_id,
                        is_oopif=frame.is_oopif,
                        cdp_session_id=None,
                        name=frame.name,
                    )

    # ── Console / 异常环形缓冲区 ───────────────────────────────────────────

    def _on_console(self, params: Dict[str, Any], *, level_from: str) -> None:
        if level_from == "exception":
            details = params.get("exceptionDetails") or {}
            text = str(details.get("text") or "")
            url = details.get("url")
            event = ConsoleEvent(ts=time.time(), level="exception", text=text, url=url)
        else:
            raw_level = str(params.get("type") or "log")
            level = "error" if raw_level in {"error", "assert"} else (
                "warning" if raw_level == "warning" else "log"
            )
            args = params.get("args") or []
            parts: List[str] = []
            for a in args[:4]:
                if isinstance(a, dict):
                    parts.append(str(a.get("value") or a.get("description") or ""))
            event = ConsoleEvent(ts=time.time(), level=level, text=" ".join(parts))
        with self._state_lock:
            self._console_events.append(event)
            if len(self._console_events) > CONSOLE_HISTORY_MAX * 2:
                # 保留最近 CONSOLE_HISTORY_MAX 条；允许 2 倍的余量以减少频繁整理。
                self._console_events = self._console_events[-CONSOLE_HISTORY_MAX:]

    # ── 帧树构建（有上限） ─────────────────────────────────────────────────

    def _build_frame_tree_locked(self) -> Dict[str, Any]:
        """构建有上限的 frame_tree 负载。必须在持有状态锁的情况下调用。"""
        frames = self._frames
        if not frames:
            return {"top": None, "children": [], "truncated": False}

        # 找出顶层帧——没有父帧的那个，优先 is_oopif=False 的。
        tops = [f for f in frames.values() if not f.parent_frame_id]
        top = next((f for f in tops if not f.is_oopif), tops[0] if tops else None)

        # 从顶层做 BFS，受 FRAME_TREE_MAX_ENTRIES 以及针对 OOPIF 分支的
        # FRAME_TREE_MAX_OOPIF_DEPTH 限制。
        children: List[Dict[str, Any]] = []
        truncated = False
        if top is None:
            return {"top": None, "children": [], "truncated": False}

        queue: List[Tuple[FrameInfo, int]] = [
            (f, 1) for f in frames.values() if f.parent_frame_id == top.frame_id
        ]
        visited: set[str] = {top.frame_id}
        while queue and len(children) < FRAME_TREE_MAX_ENTRIES:
            frame, depth = queue.pop(0)
            if frame.frame_id in visited:
                continue
            visited.add(frame.frame_id)
            if frame.is_oopif and depth > FRAME_TREE_MAX_OOPIF_DEPTH:
                truncated = True
                continue
            children.append(frame.to_dict())
            for f in frames.values():
                if f.parent_frame_id == frame.frame_id and f.frame_id not in visited:
                    queue.append((f, depth + 1))
        if queue:
            truncated = True

        return {
            "top": top.to_dict(),
            "children": children,
            "truncated": truncated,
        }


# ── 注册表 ─────────────────────────────────────────────────────────────────


class _SupervisorRegistry:
    """进程全局的 (task_id → 监督器) 映射，具有幂等的启动/停止。

    单实例，对外暴露为 ``SUPERVISOR_REGISTRY``。可从任意线程安全
    调用——变更都通过 ``_lock`` 进行。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_task: Dict[str, CDPSupervisor] = {}

    def get(self, task_id: str) -> Optional[CDPSupervisor]:
        """返回 ``task_id`` 对应的监督器（若在运行），否则返回 ``None``。"""
        with self._lock:
            return self._by_task.get(task_id)

    def get_or_start(
        self,
        task_id: str,
        cdp_url: str,
        *,
        dialog_policy: str = DEFAULT_DIALOG_POLICY,
        dialog_timeout_s: float = DEFAULT_DIALOG_TIMEOUT_S,
        start_timeout: float = 15.0,
    ) -> CDPSupervisor:
        """幂等地确保 ``(task_id, cdp_url)`` 对应的监督器在运行。

        如果该任务已存在一个监督器，但它绑定的是不同的 ``cdp_url``，
        则停止旧的并启动一个新的。
        """
        with self._lock:
            existing = self._by_task.get(task_id)
            if existing is not None:
                if existing.cdp_url == cdp_url:
                    thread_ok = existing._thread is not None and existing._thread.is_alive()
                    loop_ok = existing._loop is not None and existing._loop.is_running()
                    if thread_ok and loop_ok:
                        return existing
                    # 不健康——拆除并重建。
                # URL 已变或不健康——拆除，落到下面重新创建。
                self._by_task.pop(task_id, None)
        if existing is not None:
            existing.stop()

        supervisor = CDPSupervisor(
            task_id=task_id,
            cdp_url=cdp_url,
            dialog_policy=dialog_policy,
            dialog_timeout_s=dialog_timeout_s,
        )
        supervisor.start(timeout=start_timeout)
        with self._lock:
            # 防范来自其他线程的并发 get_or_start。
            already = self._by_task.get(task_id)
            if already is not None and already.cdp_url == cdp_url:
                supervisor.stop()
                return already
            self._by_task[task_id] = supervisor
        return supervisor

    def stop(self, task_id: str) -> None:
        """停止并丢弃 ``task_id`` 对应的监督器（若存在）。"""
        with self._lock:
            supervisor = self._by_task.pop(task_id, None)
        if supervisor is not None:
            supervisor.stop()

    def stop_all(self) -> None:
        """停止所有运行中的监督器。用于关闭 / 测试收尾。"""
        with self._lock:
            items = list(self._by_task.items())
            self._by_task.clear()
        for _, supervisor in items:
            supervisor.stop()


SUPERVISOR_REGISTRY = _SupervisorRegistry()


__all__ = [
    "CDPSupervisor",
    "ConsoleEvent",
    "DEFAULT_DIALOG_POLICY",
    "DEFAULT_DIALOG_TIMEOUT_S",
    "DIALOG_POLICY_AUTO_ACCEPT",
    "DIALOG_POLICY_AUTO_DISMISS",
    "DIALOG_POLICY_MUST_RESPOND",
    "DialogRecord",
    "FrameInfo",
    "PendingDialog",
    "SUPERVISOR_REGISTRY",
    "SupervisorSnapshot",
    "_SupervisorRegistry",
]
