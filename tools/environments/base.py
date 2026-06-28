"""所有 Hermes 执行环境后端的基类。

统一的每次调用生成模型：每条命令都会生成一个新的 ``bash -c`` 进程。
会话快照（环境变量、函数、别名）在初始化时捕获一次，并在每条命令执行前
重新加载。CWD 通过带内 stdout 标记（远程）或临时文件（本地）持久化。
"""

import codecs
import json
import logging
import os
import select
import shlex
import subprocess
import threading
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import IO, Callable, Protocol

from hermes_constants import get_hermes_home
from tools.interrupt import is_interrupted

logger = logging.getLogger(__name__)

# 中断/活动/轮询机制的可选调试跟踪。设置
# HERMES_DEBUG_INTERRUPT=1 以记录循环进入/退出、周期性心跳以及
# _wait_for_process 中每次 is_interrupted() 的状态变化。默认关闭
# 以避免生产网关日志过多。
_DEBUG_INTERRUPT = bool(os.getenv("HERMES_DEBUG_INTERRUPT"))

if _DEBUG_INTERRUPT:
    # AIAgent 的 quiet_mode 路径（run_agent.py）会在 CLI 启动时把 `tools`
    # 日志器强制设为 ERROR，这会静默吞掉我们发出的每一条跟踪。把本模块
    # 自己的日志器强制设回 INFO，使跟踪无论 quiet-mode 如何都能在
    # agent.log 中可见。仅作用于 opt-in 的情况。
    logger.setLevel(logging.INFO)

# 线程局部的活动回调。agent 在一次工具调用之前设置它，使长时间运行的
# _wait_for_process 循环可以向网关报告存活性。
_activity_callback_local = threading.local()


def set_activity_callback(cb: Callable[[str], None] | None) -> None:
    """注册一个由 _wait_for_process 周期性触发的回调。"""
    _activity_callback_local.callback = cb


def _get_activity_callback() -> Callable[[str], None] | None:
    return getattr(_activity_callback_local, "callback", None)


def touch_activity_if_due(
    state: dict,
    label: str,
) -> None:
    """最多每 ``state['interval']`` 秒触发一次活动回调。

    *state* 必须包含 ``last_touch``（单调时间戳）和 ``start``
    （操作开始的单调时间戳）。可选的 ``interval`` 键可覆盖默认的 10 秒
    节奏。

    吞掉所有异常，使调用方不需要自己的 try/except。
    """
    now = time.monotonic()
    interval = state.get("interval", 10.0)
    if now - state["last_touch"] < interval:
        return
    state["last_touch"] = now
    try:
        cb = _get_activity_callback()
        if cb:
            elapsed = int(now - state["start"])
            cb(f"{label} ({elapsed}s elapsed)")
    except Exception:
        pass


def get_sandbox_dir() -> Path:
    """返回所有沙箱存储的主机侧根目录（Docker 工作区、
    Singularity overlay/SIF 缓存等）。

    可通过 TERMINAL_SANDBOX_DIR 配置。默认为 {HERMES_HOME}/sandboxes/。
    """
    custom = os.getenv("TERMINAL_SANDBOX_DIR")
    if custom:
        p = Path(custom)
    else:
        p = get_hermes_home() / "sandboxes"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# 共享常量与工具
# ---------------------------------------------------------------------------


def _pipe_stdin(proc: subprocess.Popen, data: str) -> None:
    """在一个守护线程上把 *data* 写入 proc.stdin，以避免管道缓冲区死锁。

    在 Windows 上，文本模式的 stdin（``text=True`` / ``encoding="utf-8"``）
    在数据流经管道时会把 ``\\n`` 转换为 ``\\r\\n``——这会破坏每一次
    write_file / patch 调用，因为落到磁盘上的字节包含了被注入的回车符。
    文件*确实*被创建了，但随后每一次针对调用方 ``\\n``-only 字符串的
    字节计数/内容比较都会失败。

    变通方法：通过 ``proc.stdin.buffer``（底层字节缓冲区）写入，由我们
    自己编码为 UTF-8。这在每个平台上都完全绕过了 Python 的换行转换。
    POSIX 上行为不变——字节序列与文本模式在那里产生的完全一致。
    """

    def _write():
        try:
            # 当设置了 text=True 时，proc.stdin 是一个 TextIOWrapper。
            # 它的 ``.buffer`` 属性是绕过换行转换的原始 BufferedWriter。
            # 当 Popen 以字节模式创建时，proc.stdin 已经是一个
            # BufferedWriter，没有 ``.buffer`` 属性——回退到直接 .write()。
            raw = data.encode("utf-8") if isinstance(data, str) else data
            target = getattr(proc.stdin, "buffer", proc.stdin)
            target.write(raw)
            target.close()
        except (BrokenPipeError, OSError):
            pass

    threading.Thread(target=_write, daemon=True).start()


def _popen_bash(
    cmd: list[str], stdin_data: str | None = None, **kwargs
) -> subprocess.Popen:
    """用标准的 stdout/stderr/stdin 设置派生一个子进程。

    如果提供了 *stdin_data*，通过 :func:`_pipe_stdin` 异步写入它。有特殊
    Popen 需求的后端（例如 local 的 ``preexec_fn``）可以绕过此处并直接
    调用 :func:`_pipe_stdin`。
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        text=True,
        **kwargs,
    )
    if stdin_data is not None:
        _pipe_stdin(proc, stdin_data)
    return proc


def _load_json_store(path: Path) -> dict:
    """把一个 JSON 文件加载为字典，任何错误时返回 ``{}``。"""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_json_store(path: Path, data: dict) -> None:
    """把 *data* 作为美化打印的 JSON 写入 *path*。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _file_mtime_key(host_path: str) -> tuple[float, int] | None:
    """返回用于缓存比较的 ``(mtime, size)``，不可读则返回 ``None``。"""
    try:
        st = Path(host_path).stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# ProcessHandle 协议
# ---------------------------------------------------------------------------


class ProcessHandle(Protocol):
    """每个后端的 _run_bash() 必须返回的鸭子类型。

    subprocess.Popen 原生满足此类型。SDK 后端（Modal、Daytona）返回
    _ThreadedProcessHandle，它适配了它们的阻塞调用。
    """

    def poll(self) -> int | None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...

    @property
    def stdout(self) -> IO[str] | None: ...

    @property
    def returncode(self) -> int | None: ...


class _ThreadedProcessHandle:
    """用于没有真实子进程的 SDK 后端（Modal、Daytona）的适配器。

    把一个阻塞的 ``exec_fn() -> (output_str, exit_code)`` 包装在一个
    后台线程中，并暴露一个与 ProcessHandle 兼容的接口。可选的
    ``cancel_fn`` 在 ``kill()`` 时被调用，用于后端特定的取消
    （例如 Modal sandbox.terminate、Daytona sandbox.stop）。
    """

    def __init__(
        self,
        exec_fn: Callable[[], tuple[str, int]],
        cancel_fn: Callable[[], None] | None = None,
    ):
        self._cancel_fn = cancel_fn
        self._done = threading.Event()
        self._returncode: int | None = None
        self._error: Exception | None = None

        # 用于 stdout 的管道 —— _wait_for_process 中的排空线程读取读端。
        read_fd, write_fd = os.pipe()
        self._stdout = os.fdopen(read_fd, "r", encoding="utf-8", errors="replace")
        self._write_fd = write_fd

        def _worker():
            try:
                output, exit_code = exec_fn()
                self._returncode = exit_code
                # 把输出写入管道，使排空线程能拿到它。
                try:
                    os.write(self._write_fd, output.encode("utf-8", errors="replace"))
                except OSError:
                    pass
            except Exception as exc:
                self._error = exc
                self._returncode = 1
            finally:
                try:
                    os.close(self._write_fd)
                except OSError:
                    pass
                self._done.set()

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    @property
    def stdout(self):
        return self._stdout

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def poll(self) -> int | None:
        return self._returncode if self._done.is_set() else None

    def kill(self):
        if self._cancel_fn:
            try:
                self._cancel_fn()
            except Exception:
                pass

    def wait(self, timeout: float | None = None) -> int:
        self._done.wait(timeout=timeout)
        return self._returncode


# ---------------------------------------------------------------------------
# 远程后端的 CWD 标记
# ---------------------------------------------------------------------------


def _cwd_marker(session_id: str) -> str:
    return f"__HERMES_CWD_{session_id}__"


# ---------------------------------------------------------------------------
# BaseEnvironment
# ---------------------------------------------------------------------------


class BaseEnvironment(ABC):
    """所有 Hermes 后端的公共接口和统一执行流程。

    子类实现 ``_run_bash()`` 和 ``cleanup()``。基类提供带会话快照源、
    CWD 跟踪、中断处理和超时强制的 ``execute()``。
    """

    # 把 stdin 作为 heredoc 嵌入的子类（Modal、Daytona）设置此项。
    _stdin_mode: str = "pipe"  # "pipe" 或 "heredoc"

    # 快照创建超时（为缓慢的冷启动覆盖）。
    _snapshot_timeout: int = 30

    def get_temp_dir(self) -> str:
        """返回用于会话制品的后端临时目录。

        大多数沙箱后端使用目标环境内的 ``/tmp``。LocalEnvironment 在
        ``/tmp`` 可能缺失且 ``TMPDIR`` 是可移植可写位置的平台（如 Termux）
        上覆盖此项。
        """
        return "/tmp"

    def __init__(self, cwd: str, timeout: int, env: dict = None):
        self.cwd = cwd
        self.timeout = timeout
        self.env = env or {}

        self._session_id = uuid.uuid4().hex[:12]
        temp_dir = self.get_temp_dir().rstrip("/") or "/"
        self._snapshot_path = f"{temp_dir}/hermes-snap-{self._session_id}.sh"
        self._cwd_file = f"{temp_dir}/hermes-cwd-{self._session_id}.txt"
        self._cwd_marker = _cwd_marker(self._session_id)
        self._snapshot_ready = False

    # ------------------------------------------------------------------
    # 抽象方法
    # ------------------------------------------------------------------

    def _run_bash(
        self,
        cmd_string: str,
        *,
        login: bool = False,
        timeout: int = 120,
        stdin_data: str | None = None,
    ) -> ProcessHandle:
        """派生一个 bash 进程来运行 *cmd_string*。

        返回一个 ProcessHandle（subprocess.Popen 或 _ThreadedProcessHandle）。
        必须由每个后端覆盖。
        """
        raise NotImplementedError(f"{type(self).__name__} must implement _run_bash()")

    @abstractmethod
    def cleanup(self):
        """释放后端资源（容器、实例、连接）。"""
        ...

    # ------------------------------------------------------------------
    # 会话快照（init_session）
    # ------------------------------------------------------------------

    def init_session(self):
        """把登录 shell 环境捕获到一个快照文件中。

        在后端构造之后调用一次。成功时设置 ``_snapshot_ready = True``，
        使后续命令 source 该快照，而不是用 ``bash -l`` 运行。
        """
        # 完整捕获：环境变量、函数（已过滤）、别名、shell 选项。
        # 在登录 shell profile 脚本之后恢复已配置的 cwd，因为这些脚本可能
        # 改变工作目录（例如 bashrc 的 `cd ~`）。没有这一步，pwd -P 捕获
        # 的是 profile 的目录，而不是 terminal.cwd。
        _quoted_cwd = shlex.quote(self.cwd)
        # 给快照/cwd 文件路径加引号，使 Windows 上的 Git Bash 能处理
        # ``C:/Users/...`` 形状的路径，而不会把冒号做 glob 拆分或在驱动器
        # 字母上绊倒。POSIX 上这是 no-op（/tmp 路径中没有冒号/特殊字符）。
        # 以前不加引号的插值会在 Windows 上导致
        # ``C:/Users/.../hermes-snap-*.sh: No such file or directory`` 错误，
        # 并通过 stderr（在 Linux 后端上合并进 stdout）泄漏到每个终端工具
        # 响应中。
        _quoted_snap = shlex.quote(self._snapshot_path)
        _quoted_cwd_file = shlex.quote(self._cwd_file)
        bootstrap = (
            f"export -p > {_quoted_snap}\n"
            f"declare -f | grep -vE '^_[^_]' >> {_quoted_snap}\n"
            f"alias -p >> {_quoted_snap}\n"
            f"echo 'shopt -s expand_aliases' >> {_quoted_snap}\n"
            f"echo 'set +e' >> {_quoted_snap}\n"
            f"echo 'set +u' >> {_quoted_snap}\n"
            f"builtin cd {_quoted_cwd} 2>/dev/null || true\n"
            f"pwd -P > {_quoted_cwd_file} 2>/dev/null || true\n"
            f"printf '\\n{self._cwd_marker}%s{self._cwd_marker}\\n' \"$(pwd -P)\"\n"
        )
        try:
            proc = self._run_bash(bootstrap, login=True, timeout=self._snapshot_timeout)
            result = self._wait_for_process(proc, timeout=self._snapshot_timeout)
            self._snapshot_ready = True
            self._update_cwd(result)
            logger.info(
                "Session snapshot created (session=%s, cwd=%s)",
                self._session_id,
                self.cwd,
            )
        except Exception as exc:
            logger.warning(
                "init_session failed (session=%s): %s — "
                "falling back to bash -l per command",
                self._session_id,
                exc,
            )
            self._snapshot_ready = False

    # ------------------------------------------------------------------
    # 命令包装
    # ------------------------------------------------------------------

    @staticmethod
    def _quote_cwd_for_cd(cwd: str) -> str:
        """给 ``cd`` 目标加引号，同时保留 ``~`` 展开。"""
        if cwd == "~":
            return cwd
        if cwd == "~/":
            return "$HOME"
        if cwd.startswith("~/"):
            return f"$HOME/{shlex.quote(cwd[2:])}"
        return shlex.quote(cwd)

    def _wrap_command(self, command: str, cwd: str) -> str:
        """构建完整的 bash 脚本：source 快照、cd、运行命令、
        重新导出环境变量，并发出 CWD 标记。"""
        escaped = command.replace("'", "'\\''")

        # 给快照/cwd 文件路径加引号，使 Windows 上的 Git Bash 能处理
        # ``C:/Users/...`` 形状的路径，而不会把冒号做 glob 拆分或在驱动器
        # 字母上绊倒。POSIX 路径不受影响。引导块上的相同修复见
        # :meth:`init_session`。
        _quoted_snap = shlex.quote(self._snapshot_path)
        _quoted_cwd_file = shlex.quote(self._cwd_file)

        parts = []

        # source 快照（来自先前命令的环境变量）。
        # 把 stdout 重定向到 /dev/null：在 macOS 上（bash 3.2 和某些
        # Homebrew bash 构建）source 一个包含 ``declare -x`` 的文件会
        # 把声明输出到 stdout，把约 60 行环境变量泄漏进每个工具响应
        # （issue #15459）。Linux bash 在这里是静默的，但重定向无害。
        if self._snapshot_ready:
            parts.append(
                f"source {_quoted_snap} >/dev/null 2>&1 || true"
            )

        # 保留裸 ``~`` 展开，但通过 ``$HOME`` 重写 ``~/...``，使带空格的
        # 后缀仍是一个单一的 shell word。
        quoted_cwd = self._quote_cwd_for_cd(cwd)
        # ``--`` 阻止以连字符为前缀的目录名被解析为选项。
        parts.append(f"builtin cd -- {quoted_cwd} || exit 126")

        # 运行实际命令
        parts.append(f"eval '{escaped}'")
        parts.append("__hermes_ec=$?")

        # 把环境变量重新导出到快照（对并发调用是最后写入者获胜）
        if self._snapshot_ready:
            parts.append(f"export -p > {_quoted_snap} 2>/dev/null || true")

        # 把 CWD 写入文件（local 读取此项）和 stdout 标记（remote 解析此项）
        parts.append(f"pwd -P > {_quoted_cwd_file} 2>/dev/null || true")
        # 为标记使用单独一行。前导的 \n 确保标记即使命令不以换行符结尾
        # （例如 printf 'exact'）也能另起一行。我们会在
        # _extract_cwd_from_output 中剥掉这个注入的换行符。
        parts.append(
            f"printf '\\n{self._cwd_marker}%s{self._cwd_marker}\\n' \"$(pwd -P)\""
        )
        parts.append("exit $__hermes_ec")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Stdin heredoc 嵌入（用于 SDK 后端）
    # ------------------------------------------------------------------

    @staticmethod
    def _embed_stdin_heredoc(command: str, stdin_data: str) -> str:
        """把 stdin_data 作为 shell heredoc 追加到命令字符串。"""
        delimiter = f"HERMES_STDIN_{uuid.uuid4().hex[:12]}"
        return f"{command} << '{delimiter}'\n{stdin_data}\n{delimiter}"

    # ------------------------------------------------------------------
    # 进程生命周期
    # ------------------------------------------------------------------

    def _wait_for_process(self, proc: ProcessHandle, timeout: int = 120) -> dict:
        """基于轮询的等待，带中断检查和 stdout 排空。

        所有后端共享 —— 不被覆盖。

        在进程运行期间每 10s 触发一次 ``activity_callback``（如果在此实例上
        设置了的话），使网关的不活跃超时不会杀掉长时间运行的命令。

        还把轮询循环包装在一个 ``try/finally`` 中，保证当我们通过
        ``KeyboardInterrupt`` 或 ``SystemExit`` 退出时调用
        ``self._kill_process(proc)``。没有这一步，本地后端（它用
        ``os.setsid`` 把子进程派生到自己的进程组中）在 python 中途关闭工具
        时会留下一个 ``PPID=1`` 的孤儿——即 Physikal 和我都遇到过的
        ``sleep 300`` 存活 30 分钟的 bug。
        """
        output_chunks: list[str] = []

        # 通过 select() 做非阻塞排空。
        #
        # 旧模式 —— ``for line in proc.stdout`` —— 会阻塞在
        # ``readline()`` 上直到管道到达 EOF。当用户的命令后台化一个进程
        # （``cmd &``、``setsid cmd & disown`` 等）时，那个被后台化的孙进程
        # 通过 ``fork()`` 继承了我们 stdout 管道的写端。即使在 ``bash`` 本身
        # 退出之后，管道仍保持打开，因为孙进程仍持有它——于是排空线程永不
        # 返回，工具会挂起整个孙进程的生命周期（issue #8340：用户报告在用
        # ``setsid ... & disown`` 重启 uvicorn 时出现无限挂起）。
        #
        # 修复：用短轮询间隔的 select()，并在 ``bash`` 退出后不久停止排空，
        # 即使管道尚未 EOF。孙进程在那之后写入的任何输出都进入一个孤儿管道
        # （无害——当我们这一端关闭时内核会回收它）。
        #
        # 解码：我们以固定大小块（4096）``os.read()`` 原始字节，因此一个
        # 多字节 UTF-8 字符可能跨读取被拆分。增量解码器跨块缓冲部分序列，
        # 而 ``errors="replace"`` 镜像基线 ``TextIOWrapper``（它在
        # ``Popen`` 上以 ``encoding="utf-8", errors="replace"`` 构造），
        # 使二进制或错误编码的输出以 U+FFFD 替换保留，而不是破坏整个缓冲区。
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        def _drain_iterable(stream):
            # 回退路径：``stream`` 不由一个真实的 OS 文件描述符支撑
            # （没有可用的 ``fileno()``）。这覆盖了把 stdout 暴露为已收集
            # 输出的普通迭代器的内存 ProcessHandle 适配器（传统的
            # ``for line in proc.stdout`` 契约），而不是一个实时管道。迭代
            # 它到 EOF。没有这一步，排空线程会抛出一个未处理异常并静默死掉，
            # 丢失进程的所有输出。
            try:
                for piece in stream:
                    if piece is None:
                        continue
                    if isinstance(piece, bytes):
                        output_chunks.append(decoder.decode(piece))
                    else:
                        output_chunks.append(str(piece))
            except Exception:
                pass
            finally:
                try:
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        output_chunks.append(tail)
                except Exception:
                    pass

        def _drain():
            # 预先解析一个真实的 OS 文件描述符。真实的子进程和 SDK
            # ``_ThreadedProcessHandle``（os.pipe 支撑）都在此返回一个整数
            # fd。Mock / 迭代器式 stdout 流要么完全没有 ``fileno()``，要么
            # 返回一个非整数——此时回退为以可迭代对象方式排空流，而不是
            # 让线程崩溃（issue: 'list_iterator' object has no attribute
            # 'fileno'）。
            stream = proc.stdout
            if stream is None:
                return
            fileno = getattr(stream, "fileno", None)
            try:
                fd = fileno() if callable(fileno) else None
            except Exception:
                fd = None
            if not isinstance(fd, int) or fd < 0:
                _drain_iterable(stream)
                return
            # select.select 在 Windows 上对管道 fd 不起作用（仅对 socket）。
            # 改为在守护线程中使用阻塞 os.read —— 安全，因为 bash 退出时
            # EOF 会及时到达。
            if os.name == "nt":
                try:
                    while True:
                        chunk = os.read(fd, 4096)
                        if not chunk:
                            break
                        output_chunks.append(decoder.decode(chunk))
                except (ValueError, OSError):
                    pass
                finally:
                    try:
                        tail = decoder.decode(b"", final=True)
                        if tail:
                            output_chunks.append(tail)
                    except Exception:
                        pass
                return
            idle_after_exit = 0
            try:
                while True:
                    try:
                        ready, _, _ = select.select([fd], [], [], 0.1)
                    except (ValueError, OSError):
                        break  # fd 已关闭
                    if ready:
                        try:
                            chunk = os.read(fd, 4096)
                        except (ValueError, OSError):
                            break
                        if not chunk:
                            break  # 真正的 EOF —— 所有写入者都关闭了
                        output_chunks.append(decoder.decode(chunk))
                        idle_after_exit = 0
                    elif proc.poll() is not None:
                        # bash 已退出且管道空闲了约 100ms。再给它两个周期
                        # 来捕获任何缓冲的尾部，然后停止——否则我们会永远
                        # 等待一个孙进程管道。
                        idle_after_exit += 1
                        if idle_after_exit >= 3:
                            break
            finally:
                # 刷新任何在序列中途缓冲的字节。使用 ``errors="replace"``
                # 时，这会为任何最终不完整序列发出 U+FFFD 而不是抛异常。
                try:
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        output_chunks.append(tail)
                except Exception:
                    pass

        drain_thread = threading.Thread(target=_drain, daemon=True)
        drain_thread.start()
        deadline = time.monotonic() + timeout
        _now = time.monotonic()
        _activity_state = {
            "last_touch": _now,
            "start": _now,
        }

        # --- 调试跟踪（通过 HERMES_DEBUG_INTERRUPT=1 opt-in）-------------
        # 捕获循环进入/退出、中断状态变化和周期性心跳，使我们能在不本地
        # 复现的情况下诊断"agent 永远看不到中断"的报告。
        _tid = threading.current_thread().ident
        _pid = getattr(proc, "pid", None)
        _iter_count = 0
        _last_heartbeat = _now
        _last_interrupt_state = False
        _cb_was_none = _get_activity_callback() is None
        if _DEBUG_INTERRUPT:
            logger.info(
                "[interrupt-debug] _wait_for_process ENTER tid=%s pid=%s "
                "timeout=%ss activity_cb=%s initial_interrupt=%s",
                _tid, _pid, timeout,
                "set" if not _cb_was_none else "MISSING",
                is_interrupted(),
            )

        try:
            _poll_sleep = 0.005
            while proc.poll() is None:
                _iter_count += 1
                if is_interrupted():
                    if _DEBUG_INTERRUPT:
                        logger.info(
                            "[interrupt-debug] _wait_for_process INTERRUPT DETECTED "
                            "tid=%s pid=%s iter=%d elapsed=%.1fs — killing process group",
                            _tid, _pid, _iter_count, time.monotonic() - _activity_state["start"],
                        )
                    self._kill_process(proc)
                    drain_thread.join(timeout=2)
                    return {
                        "output": "".join(output_chunks) + "\n[Command interrupted]",
                        "returncode": 130,
                    }
                if time.monotonic() > deadline:
                    if _DEBUG_INTERRUPT:
                        logger.info(
                            "[interrupt-debug] _wait_for_process TIMEOUT "
                            "tid=%s pid=%s iter=%d timeout=%ss",
                            _tid, _pid, _iter_count, timeout,
                        )
                    self._kill_process(proc)
                    drain_thread.join(timeout=2)
                    partial = "".join(output_chunks)
                    timeout_msg = f"\n[Command timed out after {timeout}s]"
                    return {
                        "output": partial + timeout_msg
                        if partial
                        else timeout_msg.lstrip(),
                        "returncode": 124,
                    }
                # 周期性活动触碰，使网关知道我们还活着
                touch_activity_if_due(_activity_state, "terminal command running")

                # 每约 30s 一次心跳：证明循环存活，并报告活动回调状态
                # （线程局部，可能被嵌套工具调用或执行器线程复用破坏）。
                if _DEBUG_INTERRUPT and time.monotonic() - _last_heartbeat >= 30.0:
                    _cb_now_none = _get_activity_callback() is None
                    logger.info(
                        "[interrupt-debug] _wait_for_process HEARTBEAT "
                        "tid=%s pid=%s iter=%d elapsed=%.0fs "
                        "interrupt=%s activity_cb=%s%s",
                        _tid, _pid, _iter_count,
                        time.monotonic() - _activity_state["start"],
                        is_interrupted(),
                        "set" if not _cb_now_none else "MISSING",
                        " (LOST during run)" if _cb_now_none and not _cb_was_none else "",
                    )
                    _last_heartbeat = time.monotonic()
                    _cb_was_none = _cb_now_none

                # 自适应轮询：从 5ms 起步，使快速命令（echo、pwd、
                # date、短文件 cat）在约 6ms 内返回，而不是卡在等待下一个
                # 200ms 的 tick。指数退避到 200ms，使长时间运行的命令
                # （构建、测试、sleep）在轮询循环中不付出可衡量的 CPU。
                # 对一个 `echo` 这每次工具调用节省约 195ms；对一个 10s 的
                # 构建来说，稳态轮询速率与旧行为一致。
                time.sleep(_poll_sleep)
                if _poll_sleep < 0.2:
                    _poll_sleep = min(_poll_sleep * 1.5, 0.2)
        except (KeyboardInterrupt, SystemExit):
            # 信号到达（SIGTERM/SIGHUP/SIGINT）或在我们轮询时调用了
            # sys.exit()。本地后端用 os.setsid 派生子进程，这把它们放进自己
            # 的进程组——因此如果我们让中断传播而不杀掉子进程，python 退出
            # 而子进程被重新归属于 init（PPID=1）并作为孤儿继续运行。在这里
            # 杀掉进程组保证工具的副作用在 agent 停止时停止。
            if _DEBUG_INTERRUPT:
                logger.info(
                    "[interrupt-debug] _wait_for_process EXCEPTION_EXIT "
                    "tid=%s pid=%s iter=%d elapsed=%.1fs — killing subprocess group before re-raise",
                    _tid, _pid, _iter_count,
                    time.monotonic() - _activity_state["start"],
                )
            try:
                self._kill_process(proc)
                drain_thread.join(timeout=2)
            except Exception:
                pass  # 清理是尽力而为
            raise

        # 排空线程现在在 bash 之后及时退出（约 300ms 空闲检查）。一个短的
        # join 就够了；长的 join 会是一个 bug，因为那意味着非阻塞循环本身
        # 停止了协作。
        drain_thread.join(timeout=2)

        try:
            proc.stdout.close()
        except Exception:
            pass

        if _DEBUG_INTERRUPT:
            logger.info(
                "[interrupt-debug] _wait_for_process EXIT (natural) "
                "tid=%s pid=%s iter=%d elapsed=%.1fs returncode=%s",
                _tid, _pid, _iter_count,
                time.monotonic() - _activity_state["start"],
                proc.returncode,
            )

        return {"output": "".join(output_chunks), "returncode": proc.returncode}

    def _kill_process(self, proc: ProcessHandle):
        """终止一个进程。子类可覆盖以做进程组 kill。"""
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    # ------------------------------------------------------------------
    # CWD 提取
    # ------------------------------------------------------------------

    def _update_cwd(self, result: dict):
        """从命令输出中提取 CWD。本地基于文件读取时覆盖。"""
        self._extract_cwd_from_output(result)

    def _extract_cwd_from_output(self, result: dict):
        """从 stdout 输出中解析 __HERMES_CWD_{session}__ 标记。

        更新 self.cwd 并从 result["output"] 中剥除标记。供远程后端使用
        （Docker、SSH、Modal、Daytona、Singularity）。
        """
        output = result.get("output", "")
        marker = self._cwd_marker
        last = output.rfind(marker)
        if last == -1:
            return

        # 在此闭合标记之前找到开始标记
        search_start = max(0, last - 4096)  # CWD 路径不会 >4KB
        first = output.rfind(marker, search_start, last)
        if first == -1 or first == last:
            return

        cwd_path = output[first + len(marker) : last].strip()
        if cwd_path:
            self.cwd = cwd_path

        # 剥除标记行 AND 我们在它之前注入的 \n。
        # 包装器发出：printf '\n__MARKER__%s__MARKER__\n'
        # 所以输出形如：<cmd output>\n__MARKER__path__MARKER__\n
        # 我们想移除从注入的 \n 起到结尾的所有内容。
        line_start = output.rfind("\n", 0, first)
        if line_start == -1:
            line_start = first
        line_end = output.find("\n", last + len(marker))
        line_end = line_end + 1 if line_end != -1 else len(output)

        result["output"] = output[:line_start] + output[line_end:]

    # ------------------------------------------------------------------
    # 钩子
    # ------------------------------------------------------------------

    def _before_execute(self) -> None:
        """每次命令执行之前调用的钩子。

        远程后端（SSH、Modal、Daytona）覆盖此项以触发它们的
        FileSyncManager。绑定挂载后端（Docker、Singularity）和 Local 不
        需要文件同步——主机文件系统在容器/进程内直接可见。
        """
        pass

    # ------------------------------------------------------------------
    # 统一的 execute()
    # ------------------------------------------------------------------

    def execute(
        self,
        command: str,
        cwd: str = "",
        *,
        timeout: int | None = None,
        stdin_data: str | None = None,
        rewrite_compound_background: bool = True,
    ) -> dict:
        """执行一条命令，返回 {"output": str, "returncode": int}。"""
        self._before_execute()

        exec_command, sudo_stdin = self._prepare_command(command)
        # 默认防范 `A && B &` 的 subshell 等待陷阱。某些调用方
        # （spawn_via_env）已经生成了 shell 安全的包装器，并传入
        # rewrite_compound_background=False。
        if rewrite_compound_background:
            from tools.terminal_tool import _rewrite_compound_background
            exec_command = _rewrite_compound_background(exec_command)
        effective_timeout = timeout or self.timeout
        effective_cwd = cwd or self.cwd

        # 合并 sudo stdin 与调用方 stdin
        if sudo_stdin is not None and stdin_data is not None:
            effective_stdin = sudo_stdin + stdin_data
        elif sudo_stdin is not None:
            effective_stdin = sudo_stdin
        else:
            effective_stdin = stdin_data

        # 为需要它的后端把 stdin 作为 heredoc 嵌入
        if effective_stdin and self._stdin_mode == "heredoc":
            exec_command = self._embed_stdin_heredoc(exec_command, effective_stdin)
            effective_stdin = None

        wrapped = self._wrap_command(exec_command, effective_cwd)

        # 快照失败时使用登录 shell（使用户的 profile 仍会加载）
        login = not self._snapshot_ready

        proc = self._run_bash(
            wrapped, login=login, timeout=effective_timeout, stdin_data=effective_stdin
        )
        result = self._wait_for_process(proc, timeout=effective_timeout)
        self._update_cwd(result)

        return result

    # ------------------------------------------------------------------
    # 共享辅助
    # ------------------------------------------------------------------

    def stop(self):
        """cleanup 的别名（兼容较老的调用方）。"""
        self.cleanup()

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass

    def _prepare_command(self, command: str) -> tuple[str, str | None]:
        """当 SUDO_PASSWORD 可用时转换 sudo 命令。"""
        from tools.terminal_tool import _transform_sudo_command

        return _transform_sudo_command(command)
