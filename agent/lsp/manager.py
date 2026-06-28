"""LSP 客户端的服务级编排。

:class:`LSPService` 是同步 file_operations 层与异步
:class:`agent.lsp.client.LSPClient` 之间的桥梁。

设计选择：

- **单一 asyncio 事件循环**运行在后台线程中。所有客户端工作
  都在该循环上执行。来自 ``tools/file_operations.py`` 的同步调用者
  使用 :meth:`get_diagnostics_sync` 在一次阻塞调用中完成
  打开 + 等待 + 获取诊断的全流程。

- 每个 ``(server_id, workspace_root)`` 键对应一个客户端。惰性启动：
  某个键的第一次请求会启动客户端；后续请求复用同一客户端。

- **broken-set（损坏集合）**记录启动或初始化失败的
  ``(server_id, workspace_root)`` 对。在服务的整个生命周期内不再重试。
  与 OpenCode 的设计一致。

- **delta baseline（增量基线）**映射保存每个文件的
  "上次快照时的诊断信息"。``snapshot_baseline()`` 在写入操作**之前**调用；
  下一次 ``get_diagnostics_sync()`` 只返回基线中不存在的诊断信息。
  这是对 Claude Code 的 ``beforeFileEdited`` / ``getNewDiagnostics`` 模式的移植，
  不同之处在于连接到本地 LSP 层而非 MCP IDE RPC。

服务**默认关闭**——调用 :meth:`is_active` 可检查其是否实际运行。
当 LSP 在配置中被禁用、无法检测到 git 工作区、所有配置的服务器都
缺少二进制程序且自动安装已关闭时，``is_active`` 返回 False，
file_operations 层会回退到进程内的语法检查。
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from agent.lsp import eventlog
from agent.lsp.client import (
    DIAGNOSTICS_DOCUMENT_WAIT,
    LSPClient,
)
from agent.lsp.servers import (
    ServerContext,
    find_server_for_file,
    language_id_for,
)
from agent.lsp.workspace import (
    clear_cache,
    resolve_workspace_for_file,
)

logger = logging.getLogger("agent.lsp.manager")

DEFAULT_IDLE_TIMEOUT = 600  # 秒；空闲超过 10 分钟的服务器会被回收


class _BackgroundLoop:
    """拥有一个 asyncio 事件循环的守护线程。

    为同步调用者提供 :meth:`run` 方法——将协程提交到循环并阻塞，
    直到其完成（或超时触发）。
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run_forever,
            name="hermes-lsp-loop",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def _run_forever(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    def run(self, coro, *, timeout: Optional[float] = None) -> Any:
        """将协程提交到事件循环并阻塞直到完成。

        返回协程的结果，或抛出其异常。
        """
        from agent.async_utils import safe_schedule_threadsafe
        if self._loop is None:
            if asyncio.iscoroutine(coro):
                coro.close()
            raise RuntimeError("background loop not started")
        fut = safe_schedule_threadsafe(coro, self._loop)
        if fut is None:
            raise RuntimeError("background loop not running")
        try:
            return fut.result(timeout=timeout)
        except Exception:
            fut.cancel()
            raise

    def stop(self) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._loop = None
        self._thread = None


class LSPService:
    """进程级 LSP 服务。

    通过 :meth:`create_from_config` 创建一次；:func:`agent.lsp.get_service`
    访问器管理单例。大多数调用者应使用该访问器，而非直接构造
    :class:`LSPService`。
    """

    # ------------------------------------------------------------------
    # 构造 + 工厂方法
    # ------------------------------------------------------------------

    def __init__(
        self,
        *,
        enabled: bool,
        wait_mode: str,
        wait_timeout: float,
        install_strategy: str,
        binary_overrides: Optional[Dict[str, List[str]]] = None,
        env_overrides: Optional[Dict[str, Dict[str, str]]] = None,
        init_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
        disabled_servers: Optional[List[str]] = None,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    ) -> None:
        self._enabled = enabled
        self._wait_mode = wait_mode if wait_mode in {"document", "full"} else "document"
        self._wait_timeout = wait_timeout
        self._install_strategy = install_strategy
        self._binary_overrides = binary_overrides or {}
        self._env_overrides = env_overrides or {}
        self._init_overrides = init_overrides or {}
        self._disabled_servers = set(disabled_servers or [])
        self._idle_timeout = idle_timeout

        self._loop = _BackgroundLoop()
        if self._enabled:
            self._loop.start()

        # 每个 (server_id, workspace_root) 的状态
        self._clients: Dict[Tuple[str, str], LSPClient] = {}
        self._broken: set = set()
        self._spawning: Dict[Tuple[str, str], asyncio.Future] = {}
        self._last_used: Dict[Tuple[str, str], float] = {}
        self._state_lock = threading.Lock()

        # 增量基线：文件路径 → 写入操作前立即拍摄的诊断快照。
        # ``get_diagnostics_sync`` 过滤掉基线中已有的内容，
        # 使 agent 只看到当前编辑引入的错误。
        self._delta_baseline: Dict[str, List[Dict[str, Any]]] = {}

    @classmethod
    def create_from_config(cls) -> Optional["LSPService"]:
        """从 ``hermes_cli.config`` 配置构建服务。

        如果配置无法加载，返回 ``None``。当 LSP 被禁用时，
        服务本身的 ``is_active()`` 返回 False。
        """
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
        except Exception as e:  # noqa: BLE001
            logger.debug("LSP config load failed: %s", e)
            return None

        lsp_cfg = (cfg.get("lsp") or {}) if isinstance(cfg, dict) else {}
        if not isinstance(lsp_cfg, dict):
            lsp_cfg = {}

        enabled = bool(lsp_cfg.get("enabled", True))
        wait_mode = lsp_cfg.get("wait_mode", "document")
        wait_timeout = float(lsp_cfg.get("wait_timeout", DIAGNOSTICS_DOCUMENT_WAIT))
        install_strategy = lsp_cfg.get("install_strategy", "auto")
        servers_cfg = lsp_cfg.get("servers") or {}
        disabled = []
        binary_overrides: Dict[str, List[str]] = {}
        env_overrides: Dict[str, Dict[str, str]] = {}
        init_overrides: Dict[str, Dict[str, Any]] = {}
        if isinstance(servers_cfg, dict):
            for name, sub in servers_cfg.items():
                if not isinstance(sub, dict):
                    continue
                if sub.get("disabled"):
                    disabled.append(name)
                cmd = sub.get("command")
                if isinstance(cmd, list) and cmd:
                    binary_overrides[name] = cmd
                env = sub.get("env")
                if isinstance(env, dict):
                    env_overrides[name] = {k: str(v) for k, v in env.items()}
                init = sub.get("initialization_options")
                if isinstance(init, dict):
                    init_overrides[name] = init

        return cls(
            enabled=enabled,
            wait_mode=wait_mode,
            wait_timeout=wait_timeout,
            install_strategy=install_strategy,
            binary_overrides=binary_overrides,
            env_overrides=env_overrides,
            init_overrides=init_overrides,
            disabled_servers=disabled,
        )

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def is_active(self) -> bool:
        """当此服务应被调用时返回 True。"""
        return self._enabled

    def enabled_for(self, file_path: str) -> bool:
        """当 LSP 应针对此特定文件运行时返回 True。

        门控条件：工作区检测（文件或 cwd 在 git 工作区内）、
        是否有注册的服务器匹配该扩展名、以及 (server_id, workspace_root)
        对是否在之前启动失败的 broken-set 中。

        已损坏对中的文件返回 False，使 file_operations 层完全跳过
        LSP 路径——无启动尝试，无超时代价——直到服务重启（``hermes lsp restart``）
        或进程退出。
        """
        if not self._enabled:
            return False
        srv = find_server_for_file(file_path)
        if srv is None or srv.server_id in self._disabled_servers:
            return False
        ws_root, gated_in = resolve_workspace_for_file(file_path)
        if not (ws_root and gated_in):
            return False
        # broken-set 短路。如果可以廉价计算每个服务器的根目录则使用它；
        # 否则回退到工作区根目录作为 broken 键（无论如何这也是
        # _get_or_spawn 失败时使用的键）。
        try:
            per_server_root = srv.resolve_root(file_path, ws_root) or ws_root
        except Exception:  # noqa: BLE001
            per_server_root = ws_root
        if (srv.server_id, per_server_root) in self._broken:
            return False
        return True

    def snapshot_baseline(self, file_path: str) -> None:
        """将 ``file_path`` 当前的诊断信息快照为增量基线。

        在写入操作**之前**调用，使下一次 ``get_diagnostics_sync()``
        能够过滤掉预先存在的错误。尽力而为——失败时静默吞掉，
        防止不稳定的服务器破坏写入操作。

        外部超时（如服务器在初始化期间挂起）会将
        (server_id, workspace_root) 对标记为损坏，使后续编辑
        能立即跳过，而无需再次承受超时代价。
        """
        if not self.enabled_for(file_path):
            return
        try:
            diags = self._loop.run(self._snapshot_async(file_path), timeout=8.0)
            self._delta_baseline[os.path.abspath(file_path)] = diags or []
        except Exception as e:  # noqa: BLE001
            logger.debug("baseline snapshot failed for %s: %s", file_path, e)
            self._mark_broken_for_file(file_path, e)
            self._delta_baseline[os.path.abspath(file_path)] = []

    def get_diagnostics_sync(
        self,
        file_path: str,
        *,
        delta: bool = True,
        timeout: Optional[float] = None,
        line_shift: Optional[Callable[[int], Optional[int]]] = None,
    ) -> List[Dict[str, Any]]:
        """同步地在合适的服务器中打开 ``file_path``，等待诊断信息并返回。

        如果 ``delta`` 为 True（默认值），结果会与之前通过
        :meth:`snapshot_baseline` 捕获的基线进行过滤。
        基线中已存在的诊断信息会被移除，使调用者只看到当前编辑引入的错误。

        当提供 ``line_shift`` 时，基线诊断信息在进行集合差运算前
        会通过它进行重映射。这处理了编辑删除或插入行导致
        编辑点以下的已有诊断在编辑后快照中出现在不同行号的情况——
        没有行移，它们看起来都像是"此次编辑引入的"。
        传入由 :func:`agent.lsp.range_shift.build_line_shift` (pre_text, post_text)
        构建的可调用对象。当前置/后置内容不可用时省略；
        未移位的比较仍能捕获未移动的诊断信息。

        当 LSP 被禁用、无法检测到工作区、没有匹配的服务器，
        或服务器无法启动时，返回空列表。永不抛出异常。
        """
        if not self.enabled_for(file_path):
            return []

        # 尽早解析 server_id，使即使下方请求出错时也能输出结构化日志。
        srv = find_server_for_file(file_path)
        server_id = srv.server_id if srv else "?"

        try:
            t = timeout if timeout is not None else self._wait_timeout + 2.0
            diags = self._loop.run(self._open_and_wait_async(file_path), timeout=t) or []
        except asyncio.TimeoutError as e:
            eventlog.log_timeout(server_id, file_path)
            logger.debug("LSP diagnostics timeout for %s: %s", file_path, e)
            self._mark_broken_for_file(file_path, e)
            return []
        except Exception as e:  # noqa: BLE001
            eventlog.log_server_error(server_id, file_path, e)
            logger.debug("LSP diagnostics fetch failed for %s: %s", file_path, e)
            self._mark_broken_for_file(file_path, e)
            return []

        abs_path = os.path.abspath(file_path)
        if delta:
            baseline = self._delta_baseline.get(abs_path) or []
            if baseline:
                if line_shift is not None:
                    # 将基线诊断信息重映射到编辑后坐标，使移位但内容相同的
                    # 条目在 _diag_key 下哈希相等。映射到已删除区域的条目
                    # 会静默丢弃——它们已不再适用。
                    from agent.lsp.range_shift import shift_baseline
                    baseline = shift_baseline(baseline, line_shift)
                seen = {_diag_key(d) for d in baseline}
                diags = [d for d in diags if _diag_key(d) not in seen]
            # 向前滚动基线——下一次调用返回相对于刚刚发出状态的增量，
            # 与 claude-code 的 diagnosticTracking 行为一致。
            try:
                fresh = self._loop.run(self._current_diags_async(file_path), timeout=2.0) or []
            except Exception:  # noqa: BLE001
                fresh = []
            if fresh:
                self._delta_baseline[abs_path] = fresh

        if diags:
            eventlog.log_diagnostics(server_id, file_path, len(diags))
        else:
            eventlog.log_clean(server_id, file_path)
        return diags

    def _mark_broken_for_file(self, file_path: str, exc: BaseException) -> None:
        """将 (server_id, workspace_root) 对标记为损坏，使后续编辑
        能立即跳过，而无需再次承受超时代价。

        当外部 ``_loop.run`` 超时取消了内部 ``_get_or_spawn`` 任务
        仍在处理的飞行中的启动/初始化时调用。没有此标记，每次后续写入
        都会重新进入启动路径并再次承受完整的 ``snapshot_baseline``
        超时（8 秒），直到二进制程序被修复。

        还会终止在被取消的 future 中存活的任何孤立客户端进程，
        并输出一条 eventlog WARNING，让用户知道是哪个服务器放弃了。

        ``exc`` 是外部包装器捕获的任何异常——仅用于日志记录，永不重新抛出。
        """
        srv = find_server_for_file(file_path)
        if srv is None:
            return
        ws_root, gated = resolve_workspace_for_file(file_path)
        if not (ws_root and gated):
            return
        try:
            per_server_root = srv.resolve_root(file_path, ws_root) or ws_root
        except Exception:  # noqa: BLE001
            per_server_root = ws_root
        key = (srv.server_id, per_server_root)
        already_broken = key in self._broken
        self._broken.add(key)

        # 终止超时前我们成功启动的任何客户端。被取消的 future 从未到达
        # ``_get_or_spawn`` 内部的 broken-set 添加操作，因此客户端可能
        # 仍以半初始化状态挂在 ``_clients`` 中。
        with self._state_lock:
            client = self._clients.pop(key, None)
        if client is not None:
            try:
                # 即发即忘的关闭——给它一秒钟清理，但不阻塞。
                # 我们已经在慢速路径上了。
                self._loop.run(client.shutdown(), timeout=1.0)
            except Exception:  # noqa: BLE001
                pass

        if not already_broken:
            eventlog.log_spawn_failed(srv.server_id, per_server_root, exc)

    def shutdown(self) -> None:
        """拆除所有客户端并停止后台循环。"""
        if not self._enabled:
            return
        try:
            self._loop.run(self._shutdown_async(), timeout=10.0)
        except Exception as e:  # noqa: BLE001
            logger.debug("LSP shutdown error: %s", e)
        self._loop.stop()
        clear_cache()

    # ------------------------------------------------------------------
    # 异步内部实现
    # ------------------------------------------------------------------

    async def _snapshot_async(self, file_path: str) -> List[Dict[str, Any]]:
        client = await self._get_or_spawn(file_path)
        if client is None:
            return []
        try:
            version = await client.open_file(file_path, language_id=language_id_for(file_path))
            await client.wait_for_diagnostics(file_path, version, mode=self._wait_mode)
        except Exception as e:  # noqa: BLE001
            logger.debug("snapshot open/wait failed: %s", e)
            return []
        self._last_used[(client.server_id, client.workspace_root)] = time.time()
        return list(client.diagnostics_for(file_path))

    async def _open_and_wait_async(self, file_path: str) -> List[Dict[str, Any]]:
        client = await self._get_or_spawn(file_path)
        if client is None:
            return []
        try:
            version = await client.open_file(file_path, language_id=language_id_for(file_path))
            await client.save_file(file_path)
            await client.wait_for_diagnostics(file_path, version, mode=self._wait_mode)
        except Exception as e:  # noqa: BLE001
            logger.debug("open/wait failed for %s: %s", file_path, e)
            return []
        self._last_used[(client.server_id, client.workspace_root)] = time.time()
        return list(client.diagnostics_for(file_path))

    async def _current_diags_async(self, file_path: str) -> List[Dict[str, Any]]:
        ws, gated = resolve_workspace_for_file(file_path)
        srv = find_server_for_file(file_path)
        if not (ws and gated and srv):
            return []
        with self._state_lock:
            client = self._clients.get((srv.server_id, ws))
        if client is None:
            return []
        return list(client.diagnostics_for(file_path))

    async def _get_or_spawn(self, file_path: str) -> Optional[LSPClient]:
        srv = find_server_for_file(file_path)
        if srv is None:
            return None
        if srv.server_id in self._disabled_servers:
            eventlog.log_disabled(srv.server_id, file_path, "disabled in config")
            return None
        ws_root, gated = resolve_workspace_for_file(file_path)
        if not (ws_root and gated):
            eventlog.log_no_project_root(srv.server_id, file_path)
            return None
        per_server_root = srv.resolve_root(file_path, ws_root)
        if per_server_root is None:
            eventlog.log_disabled(
                srv.server_id, file_path, "exclude marker hit (server gated off)"
            )
            return None  # exclude marker hit, server gated off

        key = (srv.server_id, per_server_root)
        if key in self._broken:
            return None
        with self._state_lock:
            client = self._clients.get(key)
            if client is not None and client.is_running:
                eventlog.log_active(srv.server_id, per_server_root)
                return client
            spawning = self._spawning.get(key)
        if spawning is not None:
            try:
                return await spawning
            except Exception:  # noqa: BLE001
                return None

        # 开始启动
        loop = asyncio.get_running_loop()
        spawn_future: asyncio.Future = loop.create_future()
        with self._state_lock:
            self._spawning[key] = spawn_future
        try:
            ctx = ServerContext(
                workspace_root=per_server_root,
                install_strategy=self._install_strategy,
                binary_overrides=self._binary_overrides,
                env_overrides=self._env_overrides,
                init_overrides=self._init_overrides,
            )
            spec = srv.build_spawn(per_server_root, ctx)
            if spec is None:
                # ``build_spawn`` 在无法定位二进制程序时返回 None
                # （自动安装已禁用、仅手动安装的服务器，或安装尝试失败）。
                # 通过结构化 logger 展示一次，使用户能够采取行动。
                eventlog.log_server_unavailable(srv.server_id, srv.server_id)
                self._broken.add(key)
                spawn_future.set_result(None)
                return None
            client = LSPClient(
                server_id=srv.server_id,
                workspace_root=spec.workspace_root,
                command=spec.command,
                env=spec.env,
                cwd=spec.cwd,
                initialization_options=spec.initialization_options,
                seed_diagnostics_on_first_push=spec.seed_diagnostics_on_first_push or srv.seed_first_push,
            )
            try:
                await client.start()
            except Exception as e:  # noqa: BLE001
                eventlog.log_spawn_failed(srv.server_id, per_server_root, e)
                self._broken.add(key)
                spawn_future.set_result(None)
                return None
            with self._state_lock:
                self._clients[key] = client
            self._last_used[key] = time.time()
            eventlog.log_active(srv.server_id, per_server_root)
            spawn_future.set_result(client)
            return client
        finally:
            with self._state_lock:
                self._spawning.pop(key, None)

    async def _shutdown_async(self) -> None:
        with self._state_lock:
            clients = list(self._clients.values())
            self._clients.clear()
            self._broken.clear()
            self._last_used.clear()
        await asyncio.gather(
            *(c.shutdown() for c in clients),
            return_exceptions=True,
        )

    # ------------------------------------------------------------------
    # 状态 / 内省（供 ``hermes lsp status`` 使用）
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """返回供 CLI 状态命令使用的服务快照。"""
        with self._state_lock:
            clients = [
                {
                    "server_id": k[0],
                    "workspace_root": k[1],
                    "state": c.state,
                    "running": c.is_running,
                }
                for k, c in self._clients.items()
            ]
            broken = list(self._broken)
        return {
            "enabled": self._enabled,
            "wait_mode": self._wait_mode,
            "wait_timeout": self._wait_timeout,
            "install_strategy": self._install_strategy,
            "clients": clients,
            "broken": broken,
            "disabled_servers": sorted(self._disabled_servers),
        }


def _diag_key(d: Dict[str, Any]) -> str:
    """用于跨编辑增量过滤的内容相等性键。

    包含诊断信息的位置范围——与 :func:`agent.lsp.range_shift.shift_baseline`
    配合使用时，基线在计算此键**之前**被行移到编辑后坐标，
    使内容相同但位置移位的诊断哈希相等。两个确实位于不同行的独立诊断
    （如相同错误类在第二个位置被引入）会哈希不同，并作为新诊断展示。

    与 :func:`agent.lsp.client._diagnostic_key` 保持一致；
    刻意相同，确保两层在诊断身份上达成共识。
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


__all__ = ["LSPService"]
