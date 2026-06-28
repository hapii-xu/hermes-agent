"""基于 stdin/stdout 的异步 LSP 客户端。

每个 :class:`LSPClient` 对应一个 ``(language_server, workspace_root)``
配对——这恰好是 OpenCode 用于区分客户端的键，也是 Claude Code 使用的
相同模式。客户端负责管理一个子进程、驱动 JSON-RPC 通信，并暴露以下接口：

- :meth:`open_file` / :meth:`change_file` — 文本文档同步
- :meth:`wait_for_diagnostics` — 阻塞等待服务端为指定文件发出新的
  诊断信息（或超时触发）
- :meth:`diagnostics_for` — 读取当前按文件存储的诊断信息
- :meth:`shutdown` — 优雅关闭 + SIGTERM/SIGKILL 回退

此类设计用于单个 asyncio 事件循环中的异步使用。
:class:`agent.lsp.manager.LSPService` 在后台线程中运行事件循环，
以便同步的 file_operations 层可以通过
:func:`agent.lsp.manager.LSPService.touch_file` 调用它。

实现说明：

- Push 诊断信息按 URI 存储在 :attr:`_push_diagnostics` 中，来源于
  ``textDocument/publishDiagnostics`` 通知。Pull 诊断信息存储在
  :attr:`_pull_diagnostics` 中。合并视图按内容去重。

- 全文档同步。即使服务端声明支持增量同步，我们也发送单个
  ``contentChanges`` 条目来替换整个文档。假装增量但实际发送全量替换
  已被所有主流服务端良好容忍，并且节省了范围记录开销。
  参见 OpenCode 的 ``client.ts:584-659`` 了解相同的技巧。

- "touch-file 操作"：每次 ``open_file`` 调用还会触发
  ``workspace/didChangeWatchedFiles`` 通知（首次打开时发送 CREATED，
  之后发送 CHANGED）。某些服务端（clangd、eslint）仅在此通知触发时
  才重新扫描，尽管 LSP 规范并未严格要求这样做。

- ``ContentModified`` (-32801) 错误会以指数退避方式重试最多 3 次。
  这与 Claude Code 的 ``LSPServerInstance.sendRequest`` 行为一致。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set
from urllib.parse import quote, unquote

from agent.lsp.protocol import (
    ERROR_CONTENT_MODIFIED,
    ERROR_METHOD_NOT_FOUND,
    LSPProtocolError,
    LSPRequestError,
    classify_message,
    encode_message,
    make_error_response,
    make_notification,
    make_request,
    make_response,
    read_message,
)

logger = logging.getLogger("agent.lsp.client")

# 超时时间（秒）—— 对应 OpenCode 的常量，换算为秒。
INITIALIZE_TIMEOUT = 45.0
DIAGNOSTICS_DOCUMENT_WAIT = 5.0
DIAGNOSTICS_FULL_WAIT = 10.0
DIAGNOSTICS_REQUEST_TIMEOUT = 3.0
PUSH_DEBOUNCE = 0.15
SHUTDOWN_GRACE = 1.0  # SIGTERM 和 SIGKILL 之间的等待秒数

# 针对临时性 ContentModified 错误的重试策略。
MAX_CONTENT_MODIFIED_RETRIES = 3
RETRY_BASE_DELAY = 0.5  # 0.5, 1.0, 2.0 — 指数递增


def file_uri(path: str) -> str:
    """返回绝对文件系统路径对应的 ``file://`` URI。

    对应 Node 的 ``pathToFileURL``——处理空格、Unicode 字符
    以及 Windows 盘符（``C:\\foo`` → ``file:///C:/foo``）。
    """
    abs_path = os.path.abspath(path)
    if os.name == "nt":
        # Windows：反斜杠转换为正斜杠，并在前面加一个斜杠，
        # 使盘符作为路径组件的一部分出现。
        abs_path = abs_path.replace("\\", "/")
        if not abs_path.startswith("/"):
            abs_path = "/" + abs_path
    return "file://" + quote(abs_path, safe="/:")


def uri_to_path(uri: str) -> str:
    """:func:`file_uri` 的逆操作。"""
    if not uri.startswith("file://"):
        return uri
    raw = uri[len("file://"):]
    if os.name == "nt" and raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
        raw = raw[1:]  # 去掉盘符前的前导斜杠
    return os.path.normpath(unquote(raw))


def _end_position(text: str) -> Dict[str, int]:
    """返回 ``text`` 末尾处的 LSP Position。

    用于构建 ``textDocument/didChange`` 的"替换整个文档"变更范围，
    无论服务器声明支持哪种同步模式。
    """
    if not text:
        return {"line": 0, "character": 0}
    lines = text.splitlines(keepends=False)
    last_line = len(lines) - 1
    last_col = len(lines[-1]) if lines else 0
    # 如果文本以换行符结尾，``splitlines`` 不会体现该换行符。
    # 此时结束位置是下一行（空行）的起始——行号为 len(lines)，列号为 0。
    if text.endswith(("\n", "\r")):
        return {"line": last_line + 1, "character": 0}
    return {"line": last_line, "character": last_col}


class LSPClient:
    """绑定到一个服务器进程和一个工作区根目录的异步 LSP 客户端。

    生命周期：

        c = LSPClient(server_id, workspace_root, command, args, init_options)
        await c.start()       # 启动子进程 + 完成初始化握手
        ver = await c.open_file("/path/to/foo.py")
        await c.wait_for_diagnostics("/path/to/foo.py", ver)
        diags = c.diagnostics_for("/path/to/foo.py")
        await c.shutdown()
    """

    # ------------------------------------------------------------------
    # 构造 + 生命周期
    # ------------------------------------------------------------------

    def __init__(
        self,
        *,
        server_id: str,
        workspace_root: str,
        command: List[str],
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        initialization_options: Optional[Dict[str, Any]] = None,
        seed_diagnostics_on_first_push: bool = False,
    ) -> None:
        self.server_id = server_id
        self.workspace_root = workspace_root
        self._command = list(command)
        self._env = env
        self._cwd = cwd or workspace_root
        self._init_options = initialization_options or {}
        self._seed_first_push = seed_diagnostics_on_first_push

        # 进程 + 流
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._reader_task: Optional[asyncio.Task] = None

        # 请求/响应关联
        self._next_id: int = 0
        self._pending: Dict[int, asyncio.Future] = {}

        # 服务端向客户端发起的请求处理器（server → client）。
        # 保持精简且显式；其余全部返回 method-not-found。
        self._request_handlers: Dict[str, Callable[[Any], Awaitable[Any]]] = {
            "window/workDoneProgress/create": self._handle_work_done_create,
            "workspace/configuration": self._handle_workspace_configuration,
            "client/registerCapability": self._handle_register_capability,
            "client/unregisterCapability": self._handle_unregister_capability,
            "workspace/workspaceFolders": self._handle_workspace_folders,
            "workspace/diagnostic/refresh": self._handle_diagnostic_refresh,
        }
        # 我们关心的通知（server → client）。
        self._notification_handlers: Dict[str, Callable[[Any], None]] = {
            "textDocument/publishDiagnostics": self._handle_publish_diagnostics,
            # 其余通知（window/showMessage、$/progress 等）默认静默丢弃。
        }

        # 已追踪的文件状态——用于 didChange 版本号递增。
        self._files: Dict[str, Dict[str, Any]] = {}
        # 诊断信息存储，以文件路径（非 URI）为键。
        self._push_diagnostics: Dict[str, List[Dict[str, Any]]] = {}
        self._pull_diagnostics: Dict[str, List[Dict[str, Any]]] = {}
        # 每个路径的"最后发布"时间，供等待最新诊断的逻辑使用。
        self._published: Dict[str, float] = {}
        # 每个路径最新推送的版本号（当服务器遵守时与 didChange 版本号一致）。
        self._published_version: Dict[str, int] = {}
        # 首次推送标志，用于 typescript 风格的"首次推送时种子化"。
        self._first_push_seen: Set[str] = set()
        # 能力注册——只追踪诊断相关的注册。
        self._diagnostic_registrations: Dict[str, Dict[str, Any]] = {}

        # 状态机
        self._state: str = "stopped"
        self._initialize_result: Optional[Dict[str, Any]] = None
        self._sync_kind: int = 1  # 1=Full（全量），2=Incremental（增量）
        self._stopping: bool = False

        # 供等待者使用的推送事件。
        self._push_event = asyncio.Event()
        # 每次收到 publishDiagnostics 推送时递增的单调计数器。
        # 等待者在进入时快照该值，任何增加都意味着"有新事件，重新检查条件"。
        # 这避免了 asyncio.Event 的粘性状态陷阱。
        self._push_counter = 0
        # 注册变更事件，使 wait_for_diagnostics 在服务器宣告新动态提供者时能重新循环。
        self._registration_event = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._state == "running" and self._proc is not None and self._proc.returncode is None

    @property
    def state(self) -> str:
        return self._state

    async def start(self) -> None:
        """启动服务器并完成初始化握手。

        如果启动/初始化过程中发生任何异常，则抛出该异常。失败时进程被终止，
        客户端处于 ``"error"`` 状态——重新调用 ``start()`` 可重试。
        """
        if self._state in {"running", "starting"}:
            return
        self._state = "starting"
        try:
            await self._spawn()
            await self._initialize()
            self._state = "running"
        except Exception:
            self._state = "error"
            await self._cleanup_process()
            raise

    @staticmethod
    def _win_wrap_cmd(cmd: List[str]) -> List[str]:
        """在 Windows 上，将 .cmd/.bat 包装器包裹一层，使 CreateProcess 能执行它们。"""
        exe = cmd[0]
        if exe.lower().endswith((".cmd", ".bat")):
            return ["cmd.exe", "/c", *cmd]
        return cmd

    async def _spawn(self) -> None:
        env = dict(os.environ)
        if self._env:
            env.update(self._env)

        cmd = self._command
        if sys.platform == "win32":
            cmd = self._win_wrap_cmd(cmd)

        try:
            self._proc = await asyncio.create_subprocess_exec(
                cmd[0],
                *cmd[1:],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self._cwd,
            )
        except FileNotFoundError as e:
            raise LSPProtocolError(
                f"LSP server binary not found: {cmd[0]} ({e})"
            ) from e

        # 以 debug 级别消耗 stderr——如果不这样做，管道缓冲区会填满导致服务器挂起。
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        # 启动读取循环。
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.debug("[%s] stderr: %s", self.server_id, text[:1000])
        except (asyncio.CancelledError, OSError):
            pass

    async def _reader_loop(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        try:
            while True:
                msg = await read_message(self._proc.stdout)
                if msg is None:
                    logger.debug("[%s] 服务器已正常关闭 stdout", self.server_id)
                    break
                kind, key = classify_message(msg)
                if kind == "response":
                    self._dispatch_response(key, msg)
                elif kind == "request":
                    asyncio.create_task(self._dispatch_request(key, msg))
                elif kind == "notification":
                    self._dispatch_notification(key, msg)
                else:
                    logger.warning("[%s] dropping invalid message: %r", self.server_id, msg)
        except LSPProtocolError as e:
            logger.warning("[%s] protocol error in reader loop: %s", self.server_id, e)
        except (asyncio.CancelledError, OSError):
            pass
        finally:
            # 唤醒所有待处理请求，使其能够快速失败。
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(LSPProtocolError("server connection closed"))
            self._pending.clear()

    async def _initialize(self) -> None:
        params = {
            "rootUri": file_uri(self.workspace_root),
            "rootPath": self.workspace_root,
            "processId": os.getpid(),
            "workspaceFolders": [
                {"name": "workspace", "uri": file_uri(self.workspace_root)}
            ],
            "initializationOptions": self._init_options,
            "capabilities": {
                "window": {"workDoneProgress": True},
                "workspace": {
                    "configuration": True,
                    "workspaceFolders": True,
                    "didChangeWatchedFiles": {"dynamicRegistration": True},
                    "diagnostics": {"refreshSupport": False},
                },
                "textDocument": {
                    "synchronization": {
                        "dynamicRegistration": False,
                        "didOpen": True,
                        "didChange": True,
                        "didSave": True,
                        "willSave": False,
                        "willSaveWaitUntil": False,
                    },
                    "diagnostic": {
                        "dynamicRegistration": True,
                        "relatedDocumentSupport": True,
                    },
                    "publishDiagnostics": {
                        "relatedInformation": True,
                        "tagSupport": {"valueSet": [1, 2]},
                        "versionSupport": True,
                        "codeDescriptionSupport": True,
                        "dataSupport": False,
                    },
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "definition": {"linkSupport": True},
                    "references": {},
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                },
                "general": {"positionEncodings": ["utf-16"]},
            },
        }

        result = await asyncio.wait_for(
            self._send_request("initialize", params),
            timeout=INITIALIZE_TIMEOUT,
        )
        self._initialize_result = result
        self._sync_kind = self._extract_sync_kind(result.get("capabilities") or {})

        await self._send_notification("initialized", {})
        if self._init_options:
            # 某些服务器（vtsls、eslint）希望通过 didChangeConfiguration
            # 推送配置，即使已经在 initializationOptions 中发送过了。
            await self._send_notification(
                "workspace/didChangeConfiguration",
                {"settings": self._init_options},
            )

    @staticmethod
    def _extract_sync_kind(capabilities: dict) -> int:
        sync = capabilities.get("textDocumentSync")
        if isinstance(sync, int):
            return sync
        if isinstance(sync, dict):
            change = sync.get("change")
            if isinstance(change, int):
                return change
        return 1  # default to Full

    async def shutdown(self) -> None:
        """尽力而为的优雅关闭。

        发送 ``shutdown`` + ``exit``，若进程未能干净退出则发送 SIGTERM/SIGKILL。
        可重复调用（幂等）。
        """
        if self._stopping:
            return
        self._stopping = True
        try:
            if self.is_running:
                try:
                    await asyncio.wait_for(self._send_request("shutdown", None), timeout=2.0)
                except (asyncio.TimeoutError, LSPRequestError, LSPProtocolError):
                    pass
                try:
                    await self._send_notification("exit", None)
                except Exception:
                    pass
        finally:
            self._state = "stopped"
            await self._cleanup_process()

    async def _cleanup_process(self) -> None:
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._stderr_task is not None and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.returncode is None:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=SHUTDOWN_GRACE)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                        await proc.wait()
                    except ProcessLookupError:
                        pass
            except ProcessLookupError:
                pass

    # ------------------------------------------------------------------
    # 请求 / 通知管道
    # ------------------------------------------------------------------

    async def _send_request(self, method: str, params: Any) -> Any:
        if self._proc is None or self._proc.stdin is None or self._proc.stdin.is_closing():
            raise LSPProtocolError(f"cannot send {method!r}: stdin closed")
        loop = asyncio.get_running_loop()
        req_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        try:
            self._proc.stdin.write(encode_message(make_request(req_id, method, params)))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            self._pending.pop(req_id, None)
            raise LSPProtocolError(f"send failed for {method!r}: {e}") from e
        try:
            return await fut
        finally:
            self._pending.pop(req_id, None)

    async def _send_request_with_retry(self, method: str, params: Any, *, timeout: float) -> Any:
        """发送请求，遇到 ``ContentModified`` (-32801) 时进行重试。

        其他错误直接抛出。重试策略与 Claude Code 的
        ``LSPServerInstance.sendRequest`` 一致——共 3 次尝试，
        延迟分别为 0.5s、1.0s、2.0s。
        """
        for attempt in range(MAX_CONTENT_MODIFIED_RETRIES + 1):
            try:
                return await asyncio.wait_for(self._send_request(method, params), timeout=timeout)
            except LSPRequestError as e:
                if e.code == ERROR_CONTENT_MODIFIED and attempt < MAX_CONTENT_MODIFIED_RETRIES:
                    await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                    continue
                raise

    async def _send_notification(self, method: str, params: Any) -> None:
        if self._proc is None or self._proc.stdin is None or self._proc.stdin.is_closing():
            return
        try:
            self._proc.stdin.write(encode_message(make_notification(method, params)))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.debug("[%s] notify %s failed: %s", self.server_id, method, e)

    async def _send_response(self, req_id: Any, result: Any) -> None:
        if self._proc is None or self._proc.stdin is None or self._proc.stdin.is_closing():
            return
        try:
            self._proc.stdin.write(encode_message(make_response(req_id, result)))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    async def _send_error_response(self, req_id: Any, code: int, message: str) -> None:
        if self._proc is None or self._proc.stdin is None or self._proc.stdin.is_closing():
            return
        try:
            self._proc.stdin.write(encode_message(make_error_response(req_id, code, message)))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _dispatch_response(self, req_id: int, msg: dict) -> None:
        fut = self._pending.get(req_id)
        if fut is None or fut.done():
            return
        if "error" in msg:
            err = msg["error"] or {}
            fut.set_exception(
                LSPRequestError(
                    code=int(err.get("code", -32000)),
                    message=str(err.get("message", "unknown")),
                    data=err.get("data"),
                )
            )
        else:
            fut.set_result(msg.get("result"))

    async def _dispatch_request(self, req_id: Any, msg: dict) -> None:
        method = msg.get("method", "")
        params = msg.get("params")
        handler = self._request_handlers.get(method)
        if handler is None:
            await self._send_error_response(req_id, ERROR_METHOD_NOT_FOUND, f"method not found: {method}")
            return
        try:
            result = await handler(params)
        except Exception as e:  # noqa: BLE001 — protocol must not blow up
            logger.warning("[%s] request handler %s failed: %s", self.server_id, method, e)
            await self._send_error_response(req_id, -32000, f"handler failed: {e}")
            return
        await self._send_response(req_id, result)

    def _dispatch_notification(self, method: str, msg: dict) -> None:
        handler = self._notification_handlers.get(method)
        if handler is None:
            return
        try:
            handler(msg.get("params"))
        except Exception as e:  # noqa: BLE001
            logger.debug("[%s] notification handler %s failed: %s", self.server_id, method, e)

    # ------------------------------------------------------------------
    # 内置的服务端→客户端请求处理器
    # ------------------------------------------------------------------

    async def _handle_work_done_create(self, params: Any) -> Any:
        # 确认进度令牌——某些服务器需要此响应。
        return None

    async def _handle_workspace_configuration(self, params: Any) -> Any:
        # 通过 initializationOptions 遍历带点分隔符的 section。
        # 对应 OpenCode 的 `client.ts:198-220`——缺失时返回 null。
        if not isinstance(params, dict):
            return [None]
        items = params.get("items") or []
        out: List[Any] = []
        for item in items:
            if not isinstance(item, dict):
                out.append(None)
                continue
            section = item.get("section")
            if not section or not self._init_options:
                out.append(self._init_options or None)
                continue
            cur: Any = self._init_options
            for part in str(section).split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    cur = None
                    break
            out.append(cur)
        return out

    async def _handle_register_capability(self, params: Any) -> Any:
        if not isinstance(params, dict):
            return None
        for reg in params.get("registrations") or []:
            if not isinstance(reg, dict):
                continue
            method = reg.get("method")
            reg_id = reg.get("id")
            if method == "textDocument/diagnostic" and reg_id:
                self._diagnostic_registrations[str(reg_id)] = reg
                self._registration_event.set()
        return None

    async def _handle_unregister_capability(self, params: Any) -> Any:
        if not isinstance(params, dict):
            return None
        for unreg in params.get("unregisterations") or []:
            if not isinstance(unreg, dict):
                continue
            reg_id = unreg.get("id")
            if reg_id:
                self._diagnostic_registrations.pop(str(reg_id), None)
        return None

    async def _handle_workspace_folders(self, params: Any) -> Any:
        return [{"name": "workspace", "uri": file_uri(self.workspace_root)}]

    async def _handle_diagnostic_refresh(self, params: Any) -> Any:
        # 我们不响应 refresh 请求——每次 touchFile 时都会重新拉取。
        return None

    # ------------------------------------------------------------------
    # publishDiagnostics 处理器
    # ------------------------------------------------------------------

    def _handle_publish_diagnostics(self, params: Any) -> None:
        if not isinstance(params, dict):
            return
        uri = params.get("uri")
        if not isinstance(uri, str):
            return
        path = uri_to_path(uri)
        diagnostics = params.get("diagnostics") or []
        if not isinstance(diagnostics, list):
            diagnostics = []
        version = params.get("version")
        loop_time = asyncio.get_event_loop().time()

        if self._seed_first_push and path not in self._first_push_seen:
            # 首次推送：种子化但不触发事件，防止等待者在第一次推送时就解除等待
            # （因为该推送是在用户触发的 didChange 产生新诊断之前到达的）。
            self._first_push_seen.add(path)
            self._push_diagnostics[path] = diagnostics
            self._published[path] = loop_time
            if isinstance(version, int):
                self._published_version[path] = version
            return

        self._push_diagnostics[path] = diagnostics
        self._published[path] = loop_time
        if isinstance(version, int):
            self._published_version[path] = version
        self._first_push_seen.add(path)
        # 递增单调推送计数器并唤醒所有等待者。我们保持 Event 的粘性状态，
        # 使任何正在等待的操作都能解除；等待者唤醒后会重新检查条件，
        # 决定是否继续等待。``_push_counter`` 才是它们实际用于检测新事件的依据。
        self._push_counter += 1
        self._push_event.set()

    # ------------------------------------------------------------------
    # 公共文件同步 API
    # ------------------------------------------------------------------

    async def open_file(self, path: str, *, language_id: str = "plaintext") -> int:
        """为 ``path`` 发送 didOpen（首次）或 didChange（后续）。

        返回新的文档版本号，供 agent 的 ``wait_for_diagnostics`` 进行匹配。
        """
        if not self.is_running:
            raise LSPProtocolError("client not running")

        abs_path = os.path.abspath(path)
        try:
            text = Path(abs_path).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise LSPProtocolError(f"cannot read {abs_path}: {e}") from e

        uri = file_uri(abs_path)
        existing = self._files.get(abs_path)

        if existing is not None:
            # 重新打开：递增版本号，触发 didChangeWatchedFiles + didChange。
            await self._send_notification(
                "workspace/didChangeWatchedFiles",
                {"changes": [{"uri": uri, "type": 2}]},  # 2 = CHANGED（已变更）
            )
            new_version = existing["version"] + 1
            old_text = existing["text"]
            content_changes: List[Dict[str, Any]]
            if self._sync_kind == 2:
                content_changes = [
                    {
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": _end_position(old_text),
                        },
                        "text": text,
                    }
                ]
            else:
                content_changes = [{"text": text}]
            await self._send_notification(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": new_version},
                    "contentChanges": content_changes,
                },
            )
            self._files[abs_path] = {"version": new_version, "text": text}
            return new_version

        # 首次打开：发送 didChangeWatchedFiles CREATED + didOpen。
        await self._send_notification(
            "workspace/didChangeWatchedFiles",
            {"changes": [{"uri": uri, "type": 1}]},  # 1 = CREATED（已创建）
        )
        # 清除所有过期的推送/拉取条目——首次打开应从干净状态开始。
        self._push_diagnostics.pop(abs_path, None)
        self._pull_diagnostics.pop(abs_path, None)
        self._published.pop(abs_path, None)
        self._published_version.pop(abs_path, None)
        await self._send_notification(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": 0,
                    "text": text,
                }
            },
        )
        self._files[abs_path] = {"version": 0, "text": text}
        return 0

    async def save_file(self, path: str) -> None:
        """为 ``path`` 发送 didSave。某些 linter 只在保存时重新扫描。"""
        if not self.is_running:
            return
        abs_path = os.path.abspath(path)
        await self._send_notification(
            "textDocument/didSave",
            {"textDocument": {"uri": file_uri(abs_path)}},
        )

    # ------------------------------------------------------------------
    # 诊断：拉取 + 等待
    # ------------------------------------------------------------------

    async def _pull_document_diagnostics(self, path: str) -> None:
        """为单个文件发送 ``textDocument/diagnostic`` 拉取请求。

        将结果存储到 :attr:`_pull_diagnostics`。出错时静默不处理
        （服务器可能不支持拉取端点）。
        """
        try:
            params: Dict[str, Any] = {
                "textDocument": {"uri": file_uri(os.path.abspath(path))}
            }
            result = await self._send_request_with_retry(
                "textDocument/diagnostic",
                params,
                timeout=DIAGNOSTICS_REQUEST_TIMEOUT,
            )
        except (LSPRequestError, LSPProtocolError, asyncio.TimeoutError) as e:
            logger.debug("[%s] document diagnostic pull failed: %s", self.server_id, e)
            return
        if not isinstance(result, dict):
            return
        items = result.get("items")
        if isinstance(items, list):
            self._pull_diagnostics[os.path.abspath(path)] = items
        related = result.get("relatedDocuments")
        if isinstance(related, dict):
            for uri, sub in related.items():
                if not isinstance(sub, dict):
                    continue
                sub_items = sub.get("items")
                if isinstance(sub_items, list):
                    self._pull_diagnostics[uri_to_path(uri)] = sub_items

    async def wait_for_diagnostics(
        self,
        path: str,
        version: int,
        *,
        mode: str = "document",
    ) -> None:
        """等待服务器为 ``path`` 在 ``version`` 版本发布诊断信息。

        ``mode`` 为 ``"document"``（5 秒预算，文档拉取）或
        ``"full"``（10 秒预算，同时进行工作区拉取）。尽力而为——
        超时时静默返回。若服务器不支持拉取诊断，不会抛出异常；
        我们仍然会收到推送侧的诊断。
        """
        budget = DIAGNOSTICS_FULL_WAIT if mode == "full" else DIAGNOSTICS_DOCUMENT_WAIT
        deadline = asyncio.get_event_loop().time() + budget
        abs_path = os.path.abspath(path)

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return

            # 并发：文档拉取 + 推送等待。
            pull_task = asyncio.create_task(self._pull_document_diagnostics(abs_path))
            push_task = asyncio.create_task(self._wait_for_fresh_push(abs_path, version, remaining))
            done, pending = await asyncio.wait(
                {pull_task, push_task},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            for t in pending:
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

            # 如果收到了我们版本的最新推送，则完成等待。
            current_v = self._published_version.get(abs_path)
            if abs_path in self._published and (
                current_v is None or current_v >= version
            ):
                return

            # 拉取可能已经填充了 _pull_diagnostics——这也算成功。
            if abs_path in self._pull_diagnostics:
                return

            # 循环直到预算耗尽。

    async def _wait_for_fresh_push(self, path: str, version: int, timeout: float) -> None:
        """等待针对 ``path`` 在 ``version``+ 版本的 publishDiagnostics 到达。"""
        deadline = asyncio.get_event_loop().time() + timeout
        baseline = self._push_counter
        while True:
            current_v = self._published_version.get(path)
            if path in self._published and (current_v is None or current_v >= version):
                # 防抖——等待一小段时间以防更多诊断立即到达。
                # TypeScript 通常成对发送诊断。我们快照计数器，
                # 以便在收到*新*推送时唤醒，而非在刚才满足条件的那次推送上唤醒。
                debounce_baseline = self._push_counter
                debounce_deadline = asyncio.get_event_loop().time() + PUSH_DEBOUNCE
                while self._push_counter == debounce_baseline:
                    remaining = debounce_deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    self._push_event.clear()
                    try:
                        await asyncio.wait_for(self._push_event.wait(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                return
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return
            if self._push_counter > baseline:
                # 收到了新事件，但条件仍未满足——立即重新检查，不再等待。
                baseline = self._push_counter
                continue
            self._push_event.clear()
            try:
                await asyncio.wait_for(self._push_event.wait(), timeout=min(remaining, 0.5))
            except asyncio.TimeoutError:
                continue

    def diagnostics_for(self, path: str) -> List[Dict[str, Any]]:
        """返回单个文件当前合并且去重后的诊断信息。

        推送存储和拉取存储中的诊断信息会被拼接，并按
        ``(severity, code, message, range)`` 内容键去重。
        若服务器尚未发布任何内容，则返回空列表。
        """
        abs_path = os.path.abspath(path)
        push = self._push_diagnostics.get(abs_path) or []
        pull = self._pull_diagnostics.get(abs_path) or []
        return _dedupe(push, pull)


def _dedupe(*lists: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Set[str] = set()
    out: List[Dict[str, Any]] = []
    for lst in lists:
        for d in lst:
            if not isinstance(d, dict):
                continue
            key = _diagnostic_key(d)
            if key in seen:
                continue
            seen.add(key)
            out.append(d)
    return out


def _diagnostic_key(d: Dict[str, Any]) -> str:
    """诊断信息的内容相等性键。

    与 claude-code 的 ``areDiagnosticsEqual`` 使用的结构相等性一致——
    包含 message + severity + source + code + range 坐标。
    range 被简化为元组，以确保键在不同 dict 排序下保持稳定。
    """
    rng = d.get("range") or {}
    start = rng.get("start") or {}
    end = rng.get("end") or {}
    code = d.get("code")
    if code is not None and not isinstance(code, str):
        code = str(code)
    return "\x00".join(
        [
            str(d.get("severity") or 1),
            str(code or ""),
            str(d.get("source") or ""),
            str(d.get("message") or "").strip(),
            f"{start.get('line', 0)}:{start.get('character', 0)}-{end.get('line', 0)}:{end.get('character', 0)}",
        ]
    )


__all__ = [
    "LSPClient",
    "file_uri",
    "uri_to_path",
    "INITIALIZE_TIMEOUT",
    "DIAGNOSTICS_DOCUMENT_WAIT",
    "DIAGNOSTICS_FULL_WAIT",
]
