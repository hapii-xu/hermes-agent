"""
Gateway 运行时状态辅助函数。

提供基于 PID 文件的 gateway 守护进程运行检测，被 send_message 的 check_fn
用于在 CLI 中控制可用性。

PID 文件位于 ``{HERMES_HOME}/gateway.pid``。HERMES_HOME 默认为
``~/.hermes``，但可通过环境变量覆盖。这意味着不同的 HERMES_HOME 目录自然
会有各自的 PID 文件 —— 这一特性在我们引入命名 profile（多个 agent 在不同
配置下并发运行）时会很有用。
"""

import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Any, Optional
from utils import atomic_json_write

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

_GATEWAY_KIND = "hermes-gateway"
_RUNTIME_STATUS_FILE = "gateway_state.json"
_LOCKS_DIRNAME = "gateway-locks"
_IS_WINDOWS = sys.platform == "win32"
_UNSET = object()
_GATEWAY_LOCK_FILENAME = "gateway.lock"
_gateway_lock_handle = None
# Windows 的字节范围锁对其他读者具有强制性。锁定一个远超 JSON 载荷的字节，
# 以便运行时状态 / PID 读取者仍能读取该文件，而另一个进程持有互斥锁。
_WINDOWS_LOCK_OFFSET = 1024 * 1024


def _get_pid_path() -> Path:
    """返回 gateway PID 文件的路径，遵循 HERMES_HOME。"""
    home = get_hermes_home()
    return home / "gateway.pid"


def _get_gateway_lock_path(pid_path: Optional[Path] = None) -> Path:
    """返回运行时 gateway 锁文件的路径。"""
    if pid_path is not None:
        return pid_path.with_name(_GATEWAY_LOCK_FILENAME)
    home = get_hermes_home()
    return home / _GATEWAY_LOCK_FILENAME


def _get_runtime_status_path() -> Path:
    """返回持久化的运行时健康/状态文件路径。"""
    return _get_pid_path().with_name(_RUNTIME_STATUS_FILE)


def _get_lock_dir() -> Path:
    """返回用于 token 范围 gateway 锁的本机目录。"""
    override = os.getenv("HERMES_GATEWAY_LOCK_DIR")
    if override:
        return Path(override)
    state_home = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_home / "hermes" / _LOCKS_DIRNAME


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def terminate_pid(pid: int, *, force: bool = False) -> None:
    """以平台合适的强制语义终止一个 PID。

    POSIX 使用 SIGTERM/SIGKILL。Windows 使用 taskkill /T /F 来实现真正的
    强杀，因为 os.kill(..., SIGTERM) 并不等价于杀掉整棵进程树的硬停止。
    """
    if force and _IS_WINDOWS:
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError:
            os.kill(pid, signal.SIGTERM)
            return

        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            raise OSError(details or f"taskkill failed for PID {pid}")
        return

    sig = signal.SIGTERM if not force else getattr(signal, "SIGKILL", signal.SIGTERM)
    os.kill(pid, sig)


def _scope_hash(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _get_scope_lock_path(scope: str, identity: str) -> Path:
    return _get_lock_dir() / f"{scope}-{_scope_hash(identity)}.lock"


def _get_process_start_time(pid: int) -> Optional[int]:
    """返回稳定的、按进程的启动时间指纹，或 None。

    用作 PID 复用守卫：一对 ``(pid, start_time)`` 唯一标识一个进程，因此
    被回收的 PID（相同的数字、不同的进程）会产生不同的值，绝不会和原来的
    进程混淆。

    在 Linux 上，这是 ``/proc/<pid>/stat`` 的第 22 个字段（自启动以来的
    时钟滴答数，整数）。在没有 ``/proc`` 的平台（macOS、Windows）上，我们
    回退到 ``psutil.Process(pid).create_time()`` —— 一个浮点 epoch
    时间戳 —— 并量化为整数（厘秒）以获得稳定的相等性。

    这两个来源绝不会在单一平台上混用：``/proc`` 在 Linux 上总是先成功，
    而在 macOS/Windows 上总是失败，因此在那里总是使用 psutil。由于该守卫
    只比较启动时记录的值和*同一主机*上的实时值，跨平台不同的单位无关紧要
    —— 只有同来源的相等性才重要。
    """
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        # /proc/<pid>/stat 的第 22 个字段是进程启动时间（时钟滴答数）。
        return int(stat_path.read_text(encoding="utf-8").split()[21])
    except (FileNotFoundError, IndexError, PermissionError, ValueError, OSError):
        pass

    # 没有 /proc（macOS / Windows）：psutil 是硬依赖，暴露了跨平台的创建
    # 时间。量化为厘秒，使对同一进程的重复读取相等，避免浮点精度的脆弱性。
    try:
        import psutil  # type: ignore
        return int(round(psutil.Process(pid).create_time() * 100))
    except Exception:
        return None


def get_process_start_time(pid: int) -> Optional[int]:
    """用于在可用时获取进程启动时间的公开包装函数。"""
    return _get_process_start_time(pid)


def _read_process_cmdline(pid: int) -> Optional[str]:
    """以空格分隔的字符串形式返回进程命令行。

    在 Linux 上，直接读取 /proc/<pid>/cmdline。在没有 /proc 的 macOS 及
    其他平台上，回退到 ``ps -p <pid> -o command=``。在 Windows 上
    （没有 /proc，也没有 ps），使用 psutil。
    """
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    try:
        raw = cmdline_path.read_bytes()
    except (FileNotFoundError, PermissionError, OSError):
        pass
    else:
        if raw:
            return raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip()

    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass

    # Windows 回退：psutil（_pid_exists 已在使用）
    try:
        import psutil  # type: ignore
        proc = psutil.Process(pid)
        cmdline_parts = proc.cmdline()
        if cmdline_parts:
            return " ".join(cmdline_parts)
    except Exception:
        pass

    return None


def _gateway_command_subcommand(command: str | None) -> str | None:
    """从命令行返回 Hermes gateway 的生命周期子命令。

    生命周期决策（gateway 起来了吗？restart 是否重新启动了它？）绝不能基于
    松散的子串匹配触发。之前的 ``"... gateway" in cmdline`` 测试也会匹配
    ``hermes_cli.main gateway status``，甚至匹配不相关的进程，例如
    ``python -m tui_gateway`` —— 这使得 ``restart()`` 与仍在排空的旧进程
    产生竞态，并让 ``status``/``start`` 报告假阳性。这要求真正的
    ``gateway`` 子命令后跟 ``run``（或某个 gateway 专用的入口点），排除其他
    ``gateway`` 管理子命令，以及任何只是恰好包含 "gateway" 这个词的进程。

    使用引号感知的分词（``shlex``），这样带空格的带引号 Windows 路径
    （``"C:\\Program Files\\...\\hermes-gateway.exe"``）能保留下来，并从
    argv 的任意位置剥离 ``--profile``/``-p`` 选择器 —— Hermes 的
    ``_apply_profile_override`` 在 argparse 之前会移除它们，所以 profile
    标志（以及一个名字就叫 ``gateway`` 的 profile）可以合法地出现在
    ``gateway`` 子命令的任意一侧。
    """
    if not command:
        return None

    try:
        raw_tokens = shlex.split(command, posix=False)
    except ValueError:
        raw_tokens = command.split()
    # 剥离两侧的引号，按 token 规范化斜杠和大小写。
    tokens = [t.strip("\"'").replace("\\", "/").lower() for t in raw_tokens]
    if not tokens:
        return None

    # gateway 专用的入口点没有可检查的子命令。
    for token in tokens:
        if token == "gateway/run.py" or token.endswith("/gateway/run.py"):
            return "run"
        basename = token.rsplit("/", 1)[-1]
        if basename in ("hermes-gateway", "hermes-gateway.exe"):
            return "run"

    joined = " ".join(tokens)
    has_gateway_entry = (
        "hermes_cli.main" in joined
        or "hermes_cli/main.py" in joined
        or any(t.rsplit("/", 1)[-1] in ("hermes", "hermes.exe") for t in tokens)
    )
    if not has_gateway_entry:
        return None

    # 从任意位置丢弃 profile 选择器：--profile X / -p X / --profile=X / -p=X。
    # 这也会消费掉值为 "gateway" 的 profile，因此真正的子命令 token 就是
    # 下面落到的那个。
    filtered: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in ("--profile", "-p"):
            skip_next = True
            continue
        if token.startswith("--profile=") or token.startswith("-p="):
            continue
        filtered.append(token)

    for i, token in enumerate(filtered):
        if token != "gateway":
            continue
        if i + 1 >= len(filtered):
            return "run"  # 裸的 `hermes gateway` 默认为 `run`
        return filtered[i + 1]
    return None


def looks_like_gateway_command_line(command: str | None) -> bool:
    """仅当是真正的 ``gateway run`` 进程命令行时返回 True。"""
    return _gateway_command_subcommand(command) == "run"


def looks_like_gateway_runtime_command_line(command: str | None) -> bool:
    """对于可以承载 gateway 运行时的命令行返回 True。

    ``gateway restart`` 通常是管理命令，而不是 gateway 运行时。但在没有
    服务管理器的主机上，手动 restart 回退会在同一进程中执行
    ``run_gateway()``，因此当它占用 webhook 端口并写入运行时状态时，其 argv
    仍然是 ``gateway restart``。保持公开的
    ``looks_like_gateway_command_line()`` 严格，仅在验证 Hermes 拥有的运行时
    记录或无 supervisor 的清理扫描时使用这个更宽的匹配器。
    """
    return _gateway_command_subcommand(command) in {"run", "restart"}


def _looks_like_gateway_process(pid: int) -> bool:
    """当存活的 PID 仍像是 Hermes gateway 时返回 True。"""
    cmdline = _read_process_cmdline(pid)
    if not cmdline:
        return False
    return looks_like_gateway_command_line(cmdline)


def _record_looks_like_gateway(record: dict[str, Any]) -> bool:
    """当 cmdline 不可用时，从 PID 文件元数据校验 gateway 身份。"""
    if record.get("kind") != _GATEWAY_KIND:
        return False

    argv = record.get("argv")
    if not isinstance(argv, list) or not argv:
        return False

    cmdline = " ".join(str(part) for part in argv)
    return looks_like_gateway_runtime_command_line(cmdline)


def _profile_name_for_home(profile_home: Path) -> Optional[str]:
    """返回一个 HERMES_HOME 目录所代表的 profile id，或 None。

    命名 profile 的 home 是 ``<root>/profiles/<name>``（直接父目录是
    ``profiles``）。根/默认 home（``~/.hermes`` 或 ``$HERMES_HOME``）没有
    这样的父目录，因此映射到默认 profile（这里为 ``None``，调用方将其视为
    "裸的、无标志的 gateway"）。
    """
    if profile_home.parent.name == "profiles":
        return profile_home.name
    return None


def _command_line_belongs_to_profile(command: str, profile_home: Path) -> bool:
    """当 gateway 命令行属于 ``profile_home`` 时返回 True。

    镜像 ``hermes_cli.gateway._matches_current_profile``，以便 dashboard 的
    跨 profile 存活性回退能把一个存活 PID 归到*正确的* profile。在按 profile
    隔离的容器中，某个 profile 过期的 ``gateway_state.json`` 可能记录了一个
    PID，而 OS 随后把该 PID 回收给了另一个 profile 存活的 gateway。那个被
    回收的 PID 的命令行仍然 ``looks_like_gateway`` —— 因此如果不做 profile
    检查，死掉的 profile 就会被报告为运行中。命名 profile 的 gateway 在其
    argv 上携带 ``-p <name>``/``--profile <name>``（或罕见的显式
    ``HERMES_HOME=<path>``）；默认/根 gateway 裸运行，没有 profile 标志。
    """
    command_lc = command.lower()
    profile_name = _profile_name_for_home(profile_home)
    home_lc = str(profile_home).lower()

    if profile_name is not None and profile_name != "default":
        profile_lc = profile_name.lower()
        return (
            f"--profile {profile_lc}" in command_lc
            or f"-p {profile_lc}" in command_lc
            or f"hermes_home={home_lc}" in command_lc
        )

    # 默认/根 profile：gateway 不带 profile 标志运行。除非命令宣告了*其他*
    # profile（显式的 -p/--profile）或 argv 上有不匹配的显式 HERMES_HOME=，
    # 否则都接受。HERMES_HOME 通常通过环境传递（命令行上不可见），因此它
    # 仅仅缺失并不构成不合格 —— 只有冲突的显式值才不合格。
    if "--profile " in command_lc or " -p " in command_lc:
        return False
    if "hermes_home=" in command_lc and f"hermes_home={home_lc}" not in command_lc:
        return False
    return True


def _record_matches_live_gateway_pid(
    record: dict[str, Any],
    pid: int,
    *,
    expected_home: Optional[Path] = None,
) -> bool:
    """当存活 PID 仍然标识为这个 gateway 记录时返回 True。

    只要在可读时就优先使用实时命令行。运行时状态文件可能比它们所描述的
    gateway 进程活得更久；如果 PID 复用导致同一个 PID 被 s6 的
    supervisor/log 进程占用，过期记录的 argv 不应让那个无关进程被算作正在
    运行的 gateway。

    当提供了 ``expected_home``（dashboard 枚举某个特定 profile 的状态文件）
    时，可读的实时命令行必须额外属于*那个* profile —— 否则一个被回收到
    另一个 profile 存活 gateway 上的 PID 会让死掉的 profile 看起来是活的。
    当实时命令行无法读取（Windows/权限）时，回退到持久化的记录，以保持跨
    平台行为一致。
    """
    live_cmdline = _read_process_cmdline(pid)
    if live_cmdline:
        if not looks_like_gateway_runtime_command_line(live_cmdline):
            return False
        if expected_home is not None and not _command_line_belongs_to_profile(
            live_cmdline, expected_home
        ):
            return False
        return True
    return _record_looks_like_gateway(record)


def _build_pid_record() -> dict:
    return {
        "pid": os.getpid(),
        "kind": _GATEWAY_KIND,
        "argv": list(sys.argv),
        "start_time": _get_process_start_time(os.getpid()),
    }


def _build_runtime_status_record() -> dict[str, Any]:
    payload = _build_pid_record()
    payload.update({
        "gateway_state": "starting",
        "exit_reason": None,
        "restart_requested": False,
        "active_agents": 0,
        "platforms": {},
        "updated_at": _utc_now_iso(),
    })
    return payload


def _read_json_file(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        # OSError：文件在 exists() 和 read 之间消失，或权限被翻转。
        # UnicodeDecodeError：文件持有非 UTF-8 / 二进制垃圾（被截断或被破坏的
        # 状态文件）。无论哪种都不可用。
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    atomic_json_write(path, payload, indent=None, separators=(",", ":"))


def _read_pid_record(pid_path: Optional[Path] = None) -> Optional[dict]:
    pid_path = pid_path or _get_pid_path()
    if not pid_path.exists():
        return None

    try:
        raw = pid_path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        # 文件在 exists() 和 read_text() 之间被删除、权限被翻转，或者持有
        # 非 UTF-8 / 二进制垃圾。
        return None
    if not raw:
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        try:
            return {"pid": int(raw)}
        except ValueError:
            return None

    if isinstance(payload, int):
        return {"pid": payload}
    if isinstance(payload, dict):
        return payload
    return None


def _read_gateway_lock_record(lock_path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    return _read_pid_record(lock_path or _get_gateway_lock_path())


def _pid_from_record(record: Optional[dict[str, Any]]) -> Optional[int]:
    if not record:
        return None
    try:
        return int(record["pid"])
    except (KeyError, TypeError, ValueError):
        return None


def _cleanup_invalid_pid_path(pid_path: Path, *, cleanup_stale: bool) -> None:
    """删除过期的 gateway PID 文件（及其同级的锁元数据）。

    在 ``get_running_pid()`` 确认运行时锁已不活跃后调用，因此磁盘上的元数据
    已知属于一个死掉的进程。与 ``remove_pid_file()``（它会防御性地拒绝删除
    ``pid`` 字段与 ``os.getpid()`` 不同的 PID 文件，以保护 ``--replace``
    交接）不同，此路径强制取消两个文件的链接，使下次启动看到干净的状态。
    """
    if not cleanup_stale:
        return
    try:
        pid_path.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        _get_gateway_lock_path(pid_path).unlink(missing_ok=True)
    except Exception:
        pass


def _write_gateway_lock_record(handle) -> None:
    handle.seek(0)
    handle.truncate()
    json.dump(_build_pid_record(), handle)
    handle.flush()
    try:
        os.fsync(handle.fileno())
    except OSError:
        pass


def _try_acquire_file_lock(handle) -> bool:
    try:
        if _IS_WINDOWS:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("\n")
                handle.flush()
            handle.seek(_WINDOWS_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, OSError):
        return False


def _pid_exists(pid: int) -> bool:
    """跨平台的"这个 PID 是否存活"检查，不会杀死目标。

    在 Windows 上至关重要：Python 的 ``os.kill(pid, 0)`` 并不像 POSIX 上那样
    是一个空操作。CPython 的 Windows 实现
    （``Modules/posixmodule.c::os_kill_impl``）把 ``sig=0`` 当作
    ``CTRL_C_EVENT``，因为这两个值在 C 层面碰撞，并通过
    ``GenerateConsoleCtrlEvent(0, pid)`` 路由 —— 这会向包含目标 PID 的整个
    控制台进程组发送 Ctrl+C，而不只是该 PID 本身。任何想在 Windows 上通过
    ``os.kill(pid, 0)`` "检查这个 PID 是否存活"的调用方，都在悄悄杀死该进程
    （通常还包括同一控制台组里无关的进程）。由来已久的 Python 怪行为；见
    bpo-14484。

    实现：优先用 :mod:`psutil`（硬依赖 —— 由 Giampaolo Rodolà 维护的权威
    跨平台答案，在 Windows 内部使用
    ``OpenProcess + GetExitCodeProcess``）。如果 psutil 因某种原因不可用
    （例如精简安装，或在 ``psutil`` 被 pip 安装前的脚手架阶段的导入错误），
    在 Windows 上回退到手写的 ctypes ``OpenProcess`` /
    ``WaitForSingleObject`` 对，在 POSIX 上回退到 ``os.kill(pid, 0)``。
    """
    try:
        import psutil  # type: ignore
        return bool(psutil.pid_exists(int(pid)))
    except ImportError:
        pass  # 进入标准库回退。

    if _IS_WINDOWS:
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            # 固定返回类型 —— 默认 ctypes 的 restype 是 c_int（有符号），会把
            # WAIT_* 的 DWORD 返回码扭曲成负数。
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.WaitForSingleObject.restype = ctypes.c_uint
            kernel32.GetLastError.restype = ctypes.c_uint
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            SYNCHRONIZE = 0x100000  # WaitForSingleObject 所需
            WAIT_TIMEOUT = 0x00000102
            ERROR_INVALID_PARAMETER = 87
            ERROR_ACCESS_DENIED = 5
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, int(pid)
            )
            if not handle:
                err = kernel32.GetLastError()
                if err == ERROR_INVALID_PARAMETER:
                    return False  # PID 肯定已消失
                if err == ERROR_ACCESS_DENIED:
                    return True   # 存在但由其他用户/session 拥有
                return False      # 未知错误的保守默认
            try:
                wait_result = kernel32.WaitForSingleObject(handle, 0)
                # WAIT_TIMEOUT = 仍在运行；其他任何情况
                # （通过退出的 WAIT_OBJECT_0、通过句柄问题的 WAIT_FAILED）
                # = 当作已消失。
                return wait_result == WAIT_TIMEOUT
            finally:
                kernel32.CloseHandle(handle)
        except (OSError, AttributeError):
            return False
    else:
        try:
            os.kill(int(pid), 0)  # windows-footgun: ok — 仅 POSIX 分支（这正是 _pid_exists 的全部意义）
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # 进程存在但我们无法向其发信号 —— 仍然存活。
            return True
        except OSError:
            return False



def _release_file_lock(handle) -> None:
    try:
        if _IS_WINDOWS:
            handle.seek(_WINDOWS_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def acquire_gateway_runtime_lock() -> bool:
    """为 gateway 声明跨进程的运行时锁。

    与 PID 文件不同，该锁由存活进程本身拥有。如果进程突然死亡，OS 会自动
    释放锁。
    """
    global _gateway_lock_handle
    if _gateway_lock_handle is not None:
        return True

    path = _get_gateway_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    if not _try_acquire_file_lock(handle):
        handle.close()
        return False
    _write_gateway_lock_record(handle)
    _gateway_lock_handle = handle
    return True


def release_gateway_runtime_lock() -> None:
    """当本进程持有 gateway 运行时时锁，将其释放。"""
    global _gateway_lock_handle
    handle = _gateway_lock_handle
    if handle is None:
        return
    _gateway_lock_handle = None
    _release_file_lock(handle)
    try:
        handle.close()
    except OSError:
        pass


def is_gateway_runtime_lock_active(lock_path: Optional[Path] = None) -> bool:
    """当某个进程当前持有 gateway 运行时锁时返回 True。"""
    global _gateway_lock_handle
    resolved_lock_path = lock_path or _get_gateway_lock_path()
    if _gateway_lock_handle is not None and resolved_lock_path == _get_gateway_lock_path():
        return True

    if not resolved_lock_path.exists():
        return False

    handle = open(resolved_lock_path, "a+", encoding="utf-8")
    try:
        if _try_acquire_file_lock(handle):
            _release_file_lock(handle)
            return False
        return True
    finally:
        try:
            handle.close()
        except OSError:
            pass


def write_pid_file() -> None:
    """将当前进程的 PID 和元数据写入 gateway PID 文件。

    使用原子的 O_CREAT | O_EXCL 创建方式，使并发的 --replace 调用产生竞态：
    恰好一个进程胜出，其余的得到 FileExistsError。
    """
    path = _get_pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = json.dumps(_build_pid_record())
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise  # 让调用方决定：另一个 gateway 正在与我们竞态
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(record)
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def write_runtime_status(
    *,
    gateway_state: Any = _UNSET,
    exit_reason: Any = _UNSET,
    restart_requested: Any = _UNSET,
    active_agents: Any = _UNSET,
    platform: Any = _UNSET,
    platform_state: Any = _UNSET,
    error_code: Any = _UNSET,
    error_message: Any = _UNSET,
    served_profiles: Any = _UNSET,
) -> None:
    """持久化 gateway 的运行时健康信息，用于诊断/状态查询。"""
    path = _get_runtime_status_path()
    payload = _read_json_file(path) or _build_runtime_status_record()
    current_record = _build_pid_record()
    payload.setdefault("platforms", {})
    payload["kind"] = current_record["kind"]
    payload["pid"] = current_record["pid"]
    payload["argv"] = current_record["argv"]
    payload["start_time"] = current_record["start_time"]
    payload["updated_at"] = _utc_now_iso()

    if gateway_state is not _UNSET:
        payload["gateway_state"] = gateway_state
    if exit_reason is not _UNSET:
        payload["exit_reason"] = exit_reason
    if restart_requested is not _UNSET:
        payload["restart_requested"] = bool(restart_requested)
    if active_agents is not _UNSET:
        payload["active_agents"] = parse_active_agents(active_agents)
    if served_profiles is not _UNSET:
        # 此 gateway 多路复用的 profile（多 profile 模式）。对于单 profile 的
        # gateway 则缺失/为空。让 `hermes status` 无需二次探测即可显示按
        # profile 的覆盖情况。
        payload["served_profiles"] = list(served_profiles or [])

    if platform is not _UNSET:
        platform_payload = payload["platforms"].get(platform, {})
        if platform_state is not _UNSET:
            platform_payload["state"] = platform_state
        if error_code is not _UNSET:
            platform_payload["error_code"] = error_code
        if error_message is not _UNSET:
            platform_payload["error_message"] = error_message
        platform_payload["updated_at"] = _utc_now_iso()
        payload["platforms"][platform] = platform_payload

    _write_json_file(path, payload)


def read_runtime_status(path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """读取持久化的 gateway 运行时健康/状态信息。

    ``path`` 是可选的，这样需要检查*另一个* profile 的状态文件
    （例如 dashboard 枚举每个 profile）的调用方可以在不修改进程内
    ``HERMES_HOME`` 的情况下完成。默认为活动 profile 的
    ``gateway_state.json``。
    """
    return _read_json_file(path or _get_runtime_status_path())


def parse_active_agents(raw: Any) -> int:
    """把持久化的 ``active_agents`` 值强制转换为钳制过的非负整数。

    用于在途 gateway 轮次数的共享强制转换。在写入侧
    （``write_runtime_status``）以及两个 HTTP 读取面
    （``/api/status`` 和 ``/health/detailed``）使用，使计数遵循单一契约
    —— 永不为负，永不对手动编辑或其他非数字值抛异常（降级为 ``0``）。
    """
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


# gateway 存活且可被要求排空的状态。其他任何状态（已排空、停止中、已停止、
# 启动失败、None）都不是有效的开始排空目标。
_DRAINABLE_GATEWAY_STATES = frozenset({"running"})


def derive_gateway_busy(
    *, gateway_running: bool, gateway_state: Any, active_agents: Any
) -> bool:
    """gateway 是否正在主动处理在途的轮次。

    NAS 门控生命周期动作所依赖的契约。当且仅当 gateway 存活
    （``gateway_running``）、处于 ``running`` 状态、且至少一个 agent 在轮途
    中（``active_agents > 0``）时为忙。当存活状态未知、状态不是 ``running``、
    或计数缺失/不可解析时，降级为 ``False`` —— 即一个宕掉或文件缺失的
    gateway 读作"不忙"，绝不会是虚假的"忙"。

    NOTE：存活判断依据 ``gateway_running``（存活的 PID / 健康探针），绝不是
    ``updated_at`` —— 一个健康的空闲 gateway 永远不会推进该时间戳。
    """
    if not gateway_running:
        return False
    if gateway_state not in _DRAINABLE_GATEWAY_STATES:
        return False
    try:
        return int(active_agents) > 0
    except (TypeError, ValueError):
        return False


def derive_gateway_drainable(*, gateway_running: bool, gateway_state: Any) -> bool:
    """gateway 现在能否接受开始排空的请求。

    当且仅当 gateway 存活且处于 ``running`` 状态时为 True —— 即没有已经在
    排空/停止中/已停止，也不在启动失败状态。这与 ``active_agents`` 无关：
    一个空闲的运行中 gateway 是可排空的（排空只是立即完成）。对于宕掉或非
    运行中的 gateway 降级为 ``False``。
    """
    return bool(gateway_running) and gateway_state in _DRAINABLE_GATEWAY_STATES


def get_runtime_status_running_pid(
    runtime: Optional[dict[str, Any]] = None,
    *,
    expected_home: Optional[Path] = None,
) -> Optional[int]:
    """如果有效，从运行时状态记录返回存活的 gateway PID。

    ``get_running_pid()`` 是首要的存活判定来源，因为它校验运行时锁和 PID
    文件。但启动服务管理器仍可能让我们得到一个存活进程和一份新鲜的
    ``gateway_state.json``，却没有 ``gateway.pid``；这里作为保守的回退，通过
    同时检查持久化状态和 OS 进程身份来判定。

    ``expected_home`` 把 OS 身份检查限定到某个特定 profile 的 HERMES_HOME。
    在验证*另一个* profile 的状态文件（dashboard 枚举每个 profile）时传入它：
    一条过期记录的 PID 若被 OS 回收到另一个 profile 存活的 gateway 上，绝不能
    为死掉的 profile 报告为运行中。对活动 profile 省略它（默认），那里任何
    存活的 gateway 命令行都可以接受。
    """
    payload = runtime if runtime is not None else read_runtime_status()
    if not isinstance(payload, dict):
        return None
    if payload.get("gateway_state") in {None, "stopped", "startup_failed"}:
        return None

    pid = _pid_from_record(payload)
    if pid is None or not _pid_exists(pid):
        return None

    recorded_start = payload.get("start_time")
    current_start = _get_process_start_time(pid)
    if (
        recorded_start is not None
        and current_start is not None
        and current_start != recorded_start
    ):
        return None

    if _record_matches_live_gateway_pid(payload, pid, expected_home=expected_home):
        return pid
    return None


def remove_pid_file() -> None:
    """移除 gateway PID 文件，但仅当它属于本进程时。

    在 --replace 交接期间，旧进程的 atexit 处理器可能在新进程写入自己的
    PID 文件之后才触发。盲目删除文件会删掉新进程的记录，使 gateway 在没有
    PID 文件的情况下运行（对 ``get_running_pid()`` 不可见）。
    """
    try:
        path = _get_pid_path()
        record = _read_json_file(path)
        if record is not None:
            try:
                file_pid = int(record["pid"])
            except (KeyError, TypeError, ValueError):
                file_pid = None
            if file_pid is not None and file_pid != os.getpid():
                # PID 文件属于另一个进程 —— 不要动它。
                return
        path.unlink(missing_ok=True)
    except Exception:
        pass


def acquire_scoped_lock(scope: str, identity: str, metadata: Optional[dict[str, Any]] = None) -> tuple[bool, Optional[dict[str, Any]]]:
    """获取一个以 scope + identity 为键的本机锁。

    用于防止多个本地 gateway 同时使用同一个外部身份（例如跨不同 HERMES_HOME
    目录使用同一个 Telegram bot token）。
    """
    lock_path = _get_scope_lock_path(scope, identity)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        **_build_pid_record(),
        "scope": scope,
        "identity_hash": _scope_hash(identity),
        "metadata": metadata or {},
        "updated_at": _utc_now_iso(),
    }

    existing = _read_json_file(lock_path)
    if existing is None and lock_path.exists():
        # 锁文件存在但为空或包含无效 JSON —— 当作过期处理。这发生在前一个
        # 进程在 O_CREAT|O_EXCL 和随后的 json.dump() 之间被杀掉时
        # （例如 Slack 快速重连重试期间的 DNS 故障）。
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
    if existing:
        try:
            existing_pid = int(existing["pid"])
        except (KeyError, TypeError, ValueError):
            existing_pid = None

        if existing_pid == os.getpid() and existing.get("start_time") == record.get("start_time"):
            _write_json_file(lock_path, record)
            return True, existing

        stale = existing_pid is None
        if not stale:
            if not _pid_exists(existing_pid):
                stale = True
            else:
                current_start = _get_process_start_time(existing_pid)
                if (
                    existing.get("start_time") is not None
                    and current_start is not None
                    and current_start != existing.get("start_time")
                ):
                    stale = True
                # 当 start_time 比较不可用时（macOS / Windows 没有 /proc，所以
                # 两边都是 None），回退到检查实时进程命令行。当 cmdline 也
                # 不可读时（Windows 没有 ps），查阅锁记录自身的 argv ——
                # gateway 在启动时写入它，在没有 ps 的平台上这是唯一的身份
                # 信号。两个判断都必须表明"不是 gateway"才能标记为过期。
                if (
                    not stale
                    and existing.get("start_time") is None
                    and current_start is None
                    and not _looks_like_gateway_process(existing_pid)
                ):
                    live_cmdline = _read_process_cmdline(existing_pid)
                    if live_cmdline is not None or not _record_looks_like_gateway(existing):
                        stale = True
                # 对启动时 PID+start_time 碰撞的二次防御：systemd 确定性地
                # 拉起核心服务，因此一个无关进程（例如 cron）可能恰好落在与
                # 之前的 gateway 完全相同的 PID 和 jiffy 计数上。如果两个
                # start_time 都已知且匹配，但实时进程不是 gateway，并且我们
                # 能通过读取其 cmdline 确认这一点，那么锁就是过期的。
                if (
                    not stale
                    and existing.get("start_time") is not None
                    and current_start is not None
                    and not _looks_like_gateway_process(existing_pid)
                ):
                    live_cmdline = _read_process_cmdline(existing_pid)
                    if live_cmdline is not None:
                        stale = True
                # 检查进程是否被停止（Ctrl+Z / SIGTSTP）—— 停止的进程对
                # _pid_exists 仍然显示存活，但实际上并未运行。把它们当作
                # 过期处理，以便 --replace 能工作。
                if not stale:
                    try:
                        _proc_status = Path(f"/proc/{existing_pid}/status")
                        if _proc_status.exists():
                            for _line in _proc_status.read_text(encoding="utf-8").splitlines():
                                if _line.startswith("State:"):
                                    _state = _line.split()[1]
                                    if _state in {"T", "t"}:  # 停止或追踪停止
                                        stale = True
                                    break
                    except (OSError, PermissionError):
                        pass
        if stale:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            return False, existing

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False, _read_json_file(lock_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
    except Exception:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return True, None


def release_scoped_lock(scope: str, identity: str) -> None:
    """当本进程持有时，释放之前获取的 scope 锁。"""
    lock_path = _get_scope_lock_path(scope, identity)
    existing = _read_json_file(lock_path)
    if not existing:
        return
    if existing.get("pid") != os.getpid():
        return
    if existing.get("start_time") != _get_process_start_time(os.getpid()):
        return
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def release_all_scoped_locks(
    *,
    owner_pid: Optional[int] = None,
    owner_start_time: Optional[int] = None,
) -> int:
    """移除锁目录中的 scoped 锁文件。

    在 --replace 期间调用，清理被停止/杀掉的 gateway 进程未优雅释放的过期锁。
    当提供 ``owner_pid`` 时，只移除属于该 gateway 进程的锁记录。
    ``owner_start_time`` 进一步缩小匹配范围，以防 PID 复用。

    当未提供 owner 时，保留旧行为，移除目录中的每个 scoped 锁文件。

    返回被移除的锁文件数量。
    """
    lock_dir = _get_lock_dir()
    removed = 0
    if lock_dir.exists():
        for lock_file in lock_dir.glob("*.lock"):
            if owner_pid is not None:
                record = _read_json_file(lock_file)
                if not isinstance(record, dict):
                    continue
                try:
                    record_pid = int(record.get("pid"))
                except (TypeError, ValueError):
                    continue
                if record_pid != owner_pid:
                    continue
                if (
                    owner_start_time is not None
                    and record.get("start_time") != owner_start_time
                ):
                    continue
            try:
                lock_file.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
    return removed


# ── --replace 接管标记 ─────────────────────────────────────────
#
# 当一个新 gateway 以 ``--replace`` 启动时，它会向现有的 gateway 发送
# SIGTERM，以便接管 bot token。PR #5646 让 SIGTERM 以退出码 1 退出
# gateway，这样 ``Restart=on-failure`` 可以在意外杀掉后恢复它 —— 但这
# 也意味着 --replace 的接管目标会以 1 退出，这会诱使 systemd 在 30 秒后
# 恢复它，当两个服务都在用户的 systemd 中启用时（例如 ``hermes.service``
# + ``hermes-gateway.service``），就会与接管方开始抖动循环。
#
# 接管标记打破了这个循环：接管方在发送 SIGTERM 之前写入一个短暂的文件，
# 指明目标 PID + start_time。目标的关闭处理器读取该标记，如果它指明了本
# 进程，就把这次 SIGTERM 当作计划中的接管并以 0 退出。标记在目标消费它
# 之后被取消链接，因此崩溃的接管方留下的过期标记最多只能祸害同一 PID
# 上的一次未来关闭 —— 而且只在 _TAKEOVER_MARKER_TTL_S 之内。

_TAKEOVER_MARKER_FILENAME = ".gateway-takeover.json"
_TAKEOVER_MARKER_TTL_S = 60  # 早于此时长的标记被视为过期
_PLANNED_STOP_MARKER_FILENAME = ".gateway-planned-stop.json"
_PLANNED_STOP_MARKER_TTL_S = 60


def _get_takeover_marker_path() -> Path:
    """返回 --replace 接管标记文件的路径。"""
    home = get_hermes_home()
    return home / _TAKEOVER_MARKER_FILENAME


def _get_planned_stop_marker_path() -> Path:
    """返回主动 gateway 停止标记文件的路径。"""
    home = get_hermes_home()
    return home / _PLANNED_STOP_MARKER_FILENAME


def _marker_is_stale(written_at: str, ttl_s: int) -> bool:
    try:
        written_dt = datetime.fromisoformat(written_at)
        age = (datetime.now(timezone.utc) - written_dt).total_seconds()
        return age > ttl_s
    except (TypeError, ValueError):
        return True


def _consume_pid_marker_for_self(
    path: Path,
    *,
    pid_field: str,
    start_time_field: str,
    ttl_s: int,
) -> bool:
    record = _read_json_file(path)
    if not record:
        return False

    try:
        target_pid = int(record[pid_field])
        target_start_time = record.get(start_time_field)
        written_at = record.get("written_at") or ""
    except (KeyError, TypeError, ValueError):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return False

    if _marker_is_stale(written_at, ttl_s):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return False

    our_pid = os.getpid()
    our_start_time = _get_process_start_time(our_pid)
    # start_time 是一个 PID 复用守卫。它只在两边都确实有它时才有意义：
    # ``_get_process_start_time`` 在没有 ``/proc`` 的平台（macOS、原生
    # Windows —— 正是 planned-stop 监视器所针对的平台）上返回 None。在那里
    # 要求非 None 的匹配会让每次消费都返回 False，于是 Windows 上合法的
    # ``hermes gateway stop`` 会被误判为意外的 ``UNKNOWN`` 退出（退出码 1）
    # 并被服务管理器恢复。所以：当两个 start_time 都已知时必须匹配；当任一
    # 未知时，仅回退到 PID 相等性（受标记的短 TTL 约束）。这镜像了
    # ``planned_stop_marker_targets_self``，使监视器的非破坏性探针与此处
    # 权威的消费在每个平台上一致（issue #34597）。
    if target_pid != our_pid:
        matches = False
    elif target_start_time is not None and our_start_time is not None:
        matches = target_start_time == our_start_time
    else:
        matches = True

    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass

    return matches


def write_takeover_marker(target_pid: int) -> bool:
    """记录 ``target_pid`` 正在被当前进程接管。

    捕获目标的 ``start_time``，以便目标退出后的 PID 复用不会在之后匹配该
    标记。还记录接管方的 PID 和一个 UTC 时间戳，用于基于 TTL 的过期检查。

    写入成功返回 True，任何失败返回 False。即使写入失败，调用方也应继续
    发送 SIGTERM（该标记是尽力而为的信号，不是正确性要求）。
    """
    try:
        target_start_time = _get_process_start_time(target_pid)
        record = {
            "target_pid": target_pid,
            "target_start_time": target_start_time,
            "replacer_pid": os.getpid(),
            "written_at": _utc_now_iso(),
        }
        _write_json_file(_get_takeover_marker_path(), record)
        return True
    except (OSError, PermissionError):
        return False


def consume_takeover_marker_for_self() -> bool:
    """检查并在接管标记指明当前进程时取消其链接。

    仅当一个有效（未过期）的标记指明本 PID + start_time 时返回 True。返回
    True 表示当前的 SIGTERM 是计划中的 --replace 接管；调用方应以 0 退出，
    而不是发信号给 ``_signal_initiated_shutdown``。

    匹配时（以及检测到过期时）总是取消标记链接，以便后续无关信号不会重复
    触发。
    """
    return _consume_pid_marker_for_self(
        _get_takeover_marker_path(),
        pid_field="target_pid",
        start_time_field="target_start_time",
        ttl_s=_TAKEOVER_MARKER_TTL_S,
    )


def clear_takeover_marker() -> None:
    """无条件移除接管标记。可安全地重复调用。"""
    try:
        _get_takeover_marker_path().unlink(missing_ok=True)
    except OSError:
        pass


def write_planned_stop_marker(target_pid: int) -> bool:
    """记录 ``target_pid`` 正在被主动停止。

    gateway 在收到意外的 SIGTERM 时以非零退出，以便服务管理器能恢复它。服务
    停止命令发送相同的 SIGTERM，因此 CLI 先写入这个短暂标记，让目标进程干净
    地退出。
    """
    try:
        target_start_time = _get_process_start_time(target_pid)
        record = {
            "target_pid": target_pid,
            "target_start_time": target_start_time,
            "stopper_pid": os.getpid(),
            "written_at": _utc_now_iso(),
        }
        _write_json_file(_get_planned_stop_marker_path(), record)
        return True
    except (OSError, PermissionError):
        return False


def consume_planned_stop_marker_for_self() -> bool:
    """当当前进程正被主动停止时返回 True。"""
    return _consume_pid_marker_for_self(
        _get_planned_stop_marker_path(),
        pid_field="target_pid",
        start_time_field="target_start_time",
        ttl_s=_PLANNED_STOP_MARKER_TTL_S,
    )


def planned_stop_marker_targets_self() -> bool:
    """仅当一份存活的 planned-stop 标记指明当前进程时返回 True。

    这是一个**非破坏性**探针，由监视器线程
    （``gateway/run.py:_run_planned_stop_watcher``）用于决定是否触发关闭。
    与 :func:`consume_planned_stop_marker_for_self` 不同，它从不取消指明
    本进程的标记链接 —— 关闭处理器会在自己的线程上做权威的消费。

    它*确实*会清理永远不可能适用于本进程的标记：畸形标记和早于 TTL 的标记
    会被取消链接，以免前一个 gateway 实例留下的过期文件卡住新的实例。指明
    不同 PID/start_time 的标记会留在原处（它们仍可能被它们所指明的进程合法
    消费），但在此处报告 False。

    任何读取/解析错误都返回 False（不抛异常）。
    """
    path = _get_planned_stop_marker_path()
    record = _read_json_file(path)
    if not record:
        return False

    try:
        target_pid = int(record["target_pid"])
        target_start_time = record.get("target_start_time")
        written_at = record.get("written_at") or ""
    except (KeyError, TypeError, ValueError):
        # 畸形标记永远不可能匹配任何人 —— 丢弃它。
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return False

    if _marker_is_stale(written_at, _PLANNED_STOP_MARKER_TTL_S):
        # 如此旧的标记无论目标如何都已过有效期 —— 清理它，以免它让新启动的
        # gateway 崩溃循环。
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return False

    our_pid = os.getpid()
    if target_pid != our_pid:
        return False

    # start_time 是一个 PID 复用守卫。它只在两边都确实有它时才有意义：
    # ``_get_process_start_time`` 在没有 ``/proc`` 的平台（macOS、原生
    # Windows —— 正是此监视器所针对的平台）上返回 None。在那里要求非 None
    # 的匹配会让监视器永不触发，并重新破坏 #33778 的 Windows session-resume
    # 路径。所以：当两个 start_time 都已知时必须匹配；当任一未知时，仅
    # 回退到 PID 相等性（标记在 60s TTL 下是短暂的，限制了复用风险）。
    our_start_time = _get_process_start_time(our_pid)
    if target_start_time is not None and our_start_time is not None:
        return target_start_time == our_start_time
    return True


def clear_planned_stop_marker() -> None:
    """无条件移除 planned-stop 标记。"""
    try:
        _get_planned_stop_marker_path().unlink(missing_ok=True)
    except OSError:
        pass


def get_running_pid(
    pid_path: Optional[Path] = None,
    *,
    cleanup_stale: bool = True,
) -> Optional[int]:
    """返回正在运行的 gateway 实例的 PID，或 ``None``。

    检查 PID 文件并验证进程确实存活。自动清理过期的 PID 文件。
    """
    resolved_pid_path = pid_path or _get_pid_path()
    resolved_lock_path = _get_gateway_lock_path(resolved_pid_path)
    lock_active = is_gateway_runtime_lock_active(resolved_lock_path)
    if not lock_active:
        if pid_path is None:
            runtime_pid = get_runtime_status_running_pid()
            if runtime_pid is not None:
                return runtime_pid
        _cleanup_invalid_pid_path(resolved_pid_path, cleanup_stale=cleanup_stale)
        return None

    primary_record = _read_pid_record(resolved_pid_path)
    fallback_record = _read_gateway_lock_record(resolved_lock_path)

    for record in (primary_record, fallback_record):
        pid = _pid_from_record(record)
        if pid is None:
            continue

        if not _pid_exists(pid):
            continue

        recorded_start = record.get("start_time")
        current_start = _get_process_start_time(pid)
        if recorded_start is not None and current_start is not None and current_start != recorded_start:
            continue

        if _record_matches_live_gateway_pid(record, pid):
            return pid

    _cleanup_invalid_pid_path(resolved_pid_path, cleanup_stale=cleanup_stale)
    if pid_path is None:
        runtime_pid = get_runtime_status_running_pid()
        if runtime_pid is not None:
            return runtime_pid
    return None


def is_gateway_running(
    pid_path: Optional[Path] = None,
    *,
    cleanup_stale: bool = True,
) -> bool:
    """检查 gateway 守护进程当前是否在运行。"""
    return get_running_pid(pid_path, cleanup_stale=cleanup_stale) is not None
