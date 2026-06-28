import os
import sys

# 阻止启动目录中的 ``utils/``（或 ``proxy/``、``ui/``）包
# 遮蔽 Hermes 自身的顶层模块。``hermes_bootstrap`` 位于
# 仓库根目录，与此包相邻，因此在守卫
# 运行之前导入它是安全的（其名称不会与用户包冲突），
# 它拥有与其他入口点共享的标准路径加固逻辑。
import hermes_bootstrap

hermes_bootstrap.harden_import_path()

import json
import logging
import signal
import time
import traceback

from tui_gateway import server
from tui_gateway.server import _CRASH_LOG, dispatch, resolve_skin, write_json
from tui_gateway.transport import TeeTransport

logger = logging.getLogger(__name__)

# 后台 MCP 工具发现线程的句柄（参见 main()）。第一个
# 代理构建会短暂地 join 此线程，以便已经启动的快速服务器
# 能在代理快照其工具列表之前落地（参见 wait_for_mcp_discovery）。
_mcp_discovery_thread = None


def _install_sidecar_publisher() -> None:
    """通过 WS 将调度器的每次发送镜像到仪表板侧边栏。

    由 `HERMES_TUI_SIDECAR_URL` 激活，该环境变量由仪表板的
    ``/api/pty`` 端点在聊天标签页传递 ``channel`` 查询参数时设置。
    尽力而为：连接失败或运行时断开将回退到仅 stdio。
    """
    url = os.environ.get("HERMES_TUI_SIDECAR_URL")

    if not url:
        return

    from tui_gateway.event_publisher import WsPublisherTransport

    server._stdio_transport = TeeTransport(
        server._stdio_transport, WsPublisherTransport(url)
    )


# 等待有序关闭（atexit + 终结器）的最长时间，超过后
# 回退到 ``os._exit(0)``，防止正在刷新中的 worker
# 使进程挂起。1 秒足以覆盖网关自身的关闭工作
# （线程池排空 + 会话终结），在我们测试过的
# 每台机器上都是如此；通过 ``HERMES_TUI_GATEWAY_SHUTDOWN_GRACE_S`` 覆盖，
# 如果较慢的环境需要更多余量（例如加密磁盘
# 正在刷新检查点），但请注意更长的宽限期也意味着
# 当关闭真正死锁时等待时间更长。
_DEFAULT_SHUTDOWN_GRACE_S = 1.0


def _shutdown_grace_seconds() -> float:
    raw = (os.environ.get("HERMES_TUI_GATEWAY_SHUTDOWN_GRACE_S") or "").strip()
    if not raw:
        return _DEFAULT_SHUTDOWN_GRACE_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_SHUTDOWN_GRACE_S
    return value if value > 0 else _DEFAULT_SHUTDOWN_GRACE_S


def _log_signal(signum: int, frame) -> None:
    """捕获是哪个线程以及在哪里收到了终止信号。

    SIGPIPE 的 SIG_DFL 会在任何后台线程（TTS 播放、蜂鸣、
    语音状态发射器等）写入 TUI 已停止读取的 stdout 时
    立即静默杀死进程。没有这个
    处理器，TUI 中的 gateway-exited 横幅将没有任何痕迹 —
    崩溃日志看不到 Python 异常，因为内核在
    解释器运行任何代码之前就已经回收了进程。

    终止语义：此处的 ``sys.exit(0)`` 曾经与 worker
    池竞争 — 持有 ``_stdout_lock`` 正在刷新的线程会
    无限期阻塞解释器关闭。我们现在记录堆栈，
    给进程配置的关闭宽限期
    （``HERMES_TUI_GATEWAY_SHUTDOWN_GRACE_S``，默认
    ``_DEFAULT_SHUTDOWN_GRACE_S``）让其在后台线程上自然排空，
    然后回退到 ``os._exit(0)``，使卡住的写入/刷新
    永远不会使进程挂起。
    """
    # SIGPIPE 和 SIGHUP 在 Windows 上不存在 — 从
    # 当前平台上实际存在的属性构建查找字典。
    _signal_names: dict[int, str] = {}
    for _attr in ("SIGPIPE", "SIGTERM", "SIGHUP", "SIGINT", "SIGBREAK"):
        _sig = getattr(signal, _attr, None)
        if _sig is not None:
            _signal_names[int(_sig)] = _attr
    name = _signal_names.get(signum, f"signal {signum}")
    try:
        os.makedirs(os.path.dirname(_CRASH_LOG), exist_ok=True)
        with open(_CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(
                f"\n=== {name} received · {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
            )
            if frame is not None:
                f.write("main-thread stack at signal delivery:\n")
                traceback.print_stack(frame, file=f)
            # 所有活跃线程 — 信号可能由后台线程触发
            # （例如 TTS 写入损坏的 stdout）。
            import threading as _threading
            for tid, th in _threading._active.items():
                f.write(f"\n--- thread {th.name} (id={tid}) ---\n")
                f.write("".join(traceback.format_stack(sys._current_frames().get(tid))))
    except Exception:
        pass
    print(f"[gateway-signal] {name}", file=sys.stderr, flush=True)

    import threading as _threading

    def _hard_exit() -> None:
        # 如果 worker 线程仍在半关闭的管道上刷新，
        # ``sys.exit(0)`` 会在解释器关闭时无限期等待它
        # 释放 GIL。``os._exit`` 跳过 atexit 处理器但可以
        # 打破死锁。上面的崩溃日志 + stderr 行是
        # 取证线索。
        os._exit(0)

    timer = _threading.Timer(_shutdown_grace_seconds(), _hard_exit)
    timer.daemon = True
    timer.start()

    # ── 退出前刷新会话 ───────────────────────────────────
    # atexit 处理器（_shutdown_sessions）注册在
    # tui_gateway/server.py 中，但持有 GIL 或
    # _stdout_lock 的 worker 线程可能会阻止 atexit 在宽限
    # 窗口内完成。在此处显式终结会话，
    # 使未持久化的消息在硬退出定时器触发之前
    # 到达 state.db。
    try:
        from tui_gateway.server import _shutdown_sessions

        _shutdown_sessions()
    except Exception:
        pass

    try:
        sys.exit(0)
    except SystemExit:
        # 重新抛出，使主线程解释器展开并运行
        # atexit + 终结器（在宽限窗口内）。Python 信号
        # 处理器始终在主线程上运行，但持有
        # ``_stdout_lock`` 正在刷新的 worker 线程可能会使展开
        # 无限期等待；上面的守护定时器就是针对
        # 这种情况的安全网。
        raise


# SIGPIPE：忽略，不退出。旧的 SIG_DFL 会在任何 *后台*
# 线程（TTS 播放链、语音调试 stderr 发射器、蜂鸣线程）
# 写入 TUI 已不再读取的管道时静默杀死进程 —
# 即使主线程在 stdin 上等待完全正常。
# 忽略该信号让 Python 在违规写入时抛出 BrokenPipeError
# （write_json 已经通过干净的 sys.exit(0) + _log_exit
# 处理这种情况），只要主命令管道仍可读，
# 网关就保持存活。终端信号仍然
# 通过 _log_signal 路由，使终止和挂断可以被诊断。
#
# SIGPIPE 和 SIGHUP 在 Windows 上不存在；用 hasattr 保护
# 每次安装，使 ``python -m tui_gateway.entry``（由
# ``hermes --tui`` 生成）能在 Windows 上正常导入。
# SIGBREAK（Windows 的 Ctrl+Break）在可用时作为
# SIGHUP 的较弱等价物安装。
if hasattr(signal, "SIGPIPE"):
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, _log_signal)
if hasattr(signal, "SIGHUP"):
    signal.signal(signal.SIGHUP, _log_signal)
elif hasattr(signal, "SIGBREAK"):
    # 仅 Windows：控制台窗口中的 Ctrl+Break 会发送 SIGBREAK。
    # 通过相同的处理器路由，使终止可以被诊断。
    signal.signal(signal.SIGBREAK, _log_signal)
if hasattr(signal, "SIGINT"):
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _log_exit(reason: str) -> None:
    """记录网关子进程关闭的原因。

    三条退出路径（启动时写入失败、解析错误响应写入失败、
    调度响应写入失败、stdin EOF）全部折叠为此处的静默
    sys.exit(0)。没有这条记录，TUI 会显示 "gateway exited"
    但没有任何关于哪个管道损坏或哪条消息
    触发了它的可操作线索 — 这就是为什么语音模式会话
    看起来像幽灵崩溃，而真正的原因是 "TUI 读取管道
    在此事件上关闭了"。
    """
    try:
        os.makedirs(os.path.dirname(_CRASH_LOG), exist_ok=True)
        with open(_CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(
                f"\n=== gateway exit · {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"· reason={reason} ===\n"
            )
    except Exception:
        pass
    print(f"[gateway-exit] {reason}", file=sys.stderr, flush=True)


def wait_for_mcp_discovery(timeout: "float | None" = None) -> None:
    """阻塞直到后台 MCP 发现完成，最多等待解析后的时间上限。

    MCP 发现在启动时生成的守护线程中运行（参见 main()），
    使慢/死服务器不会冻结 ``gateway.ready``。但代理在构建时
    只快照一次工具列表，之后不再重新读取，因此一个可达但
    慢的服务器如果在第一次提示 *之后* 才完成连接，
    在整个会话中都将不可见。在第一次代理构建之前
    以有限超时 join，让已经启动的服务器落地，
    同时不会重新引入启动挂起：``thread.join(timeout)``
    一旦发现完成就立即返回（因此快速/无 MCP 启动
    约 0 秒开销），而死服务器 simply 不会被等待超过上限。
    未启动发现线程时为空操作。

    上限来自配置中的 ``mcp_discovery_timeout``（通过
    ``hermes_cli.mcp_startup`` 与 CLI 路径共享）；``timeout`` 参数覆盖它。
    """
    thread = _mcp_discovery_thread
    if thread is None or not thread.is_alive():
        return
    try:
        from hermes_cli.mcp_startup import _resolve_discovery_timeout

        bound = _resolve_discovery_timeout(timeout)
    except Exception:
        bound = timeout if timeout is not None else 0.75
    thread.join(timeout=bound)


def mcp_discovery_in_flight() -> bool:
    """如果后台 MCP 发现线程仍在运行则返回 True。

    代理构建路径使用此方法决定是否安排延迟工具
    快照刷新：如果发现未在有限的
    ``wait_for_mcp_discovery`` join 内落地，代理构建时就没有这些工具，
    横幅/工具计数将是过时的，直到它们到达。
    """
    thread = _mcp_discovery_thread
    return thread is not None and thread.is_alive()


def join_mcp_discovery(timeout: float | None = None) -> bool:
    """阻塞直到后台 MCP 发现完成，最多等待 ``timeout`` 秒。

    如果发现已完成（线程不存在或不再活跃）则返回 True，
    如果超时后仍在运行则返回 False。与
    ``wait_for_mcp_discovery`` 不同，此方法接受无界/长时间等待并报告
    结果，供非关键路径的延迟刷新等待者使用。
    """
    thread = _mcp_discovery_thread
    if thread is None:
        return True
    thread.join(timeout=timeout)
    return not thread.is_alive()


def main():
    _install_sidecar_publisher()

    # MCP 工具发现 — 在后台守护线程中运行，使慢或
    # 不可达的 MCP 服务器不会冻结 TUI 启动。此前这是在
    # ``gateway.ready`` 之前内联运行的，这意味着任何已配置但宕机的
    # 服务器都会在整个连接重试退避期间阻塞整个 shell
    # （例如一个死的 stdio/http 服务器会消耗 1+2+4 秒的
    # 重试 → 在编辑器出现之前约 7 秒的死等）。发现是
    # 幂等的，在服务器连接时将工具注册到共享注册表中。
    # 代理直到第一次提示时才构建，此时
    # ``_make_agent`` 会短暂 join 此线程（``wait_for_mcp_discovery``，
    # 有限时），使已启动的快速服务器落入工具快照 —
    # 死服务器 simply 不会被等待超过上限。``/reload-mcp``
    # 为会话中稍后连接的服务器重建快照。
    #
    # 冷启动保护：导入 ``tools.mcp_tool`` 会传递性地拉入
    # 完整的 MCP SDK（mcp、pydantic、httpx、jsonschema、starlette 解析器 —
    # 在 macOS 上约 200 毫秒）。绝大多数用户没有
    # 配置 ``mcp_servers``，在这种情况下每一字节的导入都是
    # 浪费的。先检查配置（开销低），只在确实有 MCP 工作
    # 要做时才生成发现线程，这样常见情况下
    # 导入成本完全不会出现在路径上。
    try:
        from hermes_cli.config import read_raw_config
        _mcp_servers = (read_raw_config() or {}).get("mcp_servers")
        _has_mcp_servers = isinstance(_mcp_servers, dict) and len(_mcp_servers) > 0
    except Exception:
        # 保守策略：如果无法确定，回退到尝试
        # 发现（仍然在后台，不会阻塞启动）。
        _has_mcp_servers = True
    if _has_mcp_servers:
        def _discover_mcp_background() -> None:
            try:
                from tools.mcp_tool import discover_mcp_tools
                discover_mcp_tools()
            except Exception:
                logger.warning(
                    "Background MCP tool discovery failed", exc_info=True
                )

        import threading as _mcp_threading
        _mcp_thread = _mcp_threading.Thread(
            target=_discover_mcp_background,
            name="tui-mcp-discovery",
            daemon=True,
        )
        _mcp_thread.start()
        # 发布句柄，使第一次代理构建可以短暂等待
        # 已启动的快速服务器落地（参见 wait_for_mcp_discovery）。
        global _mcp_discovery_thread
        _mcp_discovery_thread = _mcp_thread

    if not write_json({
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": "gateway.ready", "payload": {"skin": resolve_skin()}},
    }):
        _log_exit("startup write failed (broken stdout pipe before first event)")
        sys.exit(0)

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            if not write_json({"jsonrpc": "2.0", "error": {"code": -32700, "message": "parse error"}, "id": None}):
                _log_exit("parse-error-response write failed (broken stdout pipe)")
                sys.exit(0)
            continue

        method = req.get("method") if isinstance(req, dict) else None
        resp = dispatch(req)
        if resp is not None:
            if not write_json(resp):
                _log_exit(f"response write failed for method={method!r} (broken stdout pipe)")
                sys.exit(0)

    _log_exit("stdin EOF (TUI closed the command pipe)")


if __name__ == "__main__":
    main()
