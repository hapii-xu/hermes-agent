"""关闭取证（Shutdown forensics）—— 在 gateway 收到 SIGTERM/SIGINT 时捕获上下文。

gateway 的 ``shutdown_signal_handler`` 在 asyncio 事件循环内部同步运行。
我们无法安全地长时间阻塞它，但我们确实希望持久化地记录是谁/什么触发了
关闭，这样“gateway 一直在死”这类事故就能在事后被诊断。

本模块暴露 :func:`snapshot_shutdown_context`，这是一个快速（<10ms）、
非阻塞的探测，返回一个结构化的 dict，信号处理器可以立即记录它；以及
:func:`spawn_async_diagnostic`，一个 fire-and-forget 的 ``ps`` 遍历，
它作为一个分离的子进程运行，因此即使 /proc 卡住也无法阻塞拆卸过程。

任何需要等待的操作（例如 shell 出去执行 ``ps aux``）都属于 async 辅助
函数，绝不能放在同步探测里。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


_SIGNAL_NAME_BY_NUM: Dict[int, str] = {}
for _name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT", "SIGUSR1", "SIGUSR2"):
    _val = getattr(signal, _name, None)
    if _val is not None:
        _SIGNAL_NAME_BY_NUM[int(_val)] = _name


def _signal_name(sig: Any) -> str:
    """返回人类可读的信号名（失败时回退为 ``str(sig)``）。"""
    if sig is None:
        return "UNKNOWN"
    try:
        sig_int = int(sig)
    except (TypeError, ValueError):
        return str(sig)
    return _SIGNAL_NAME_BY_NUM.get(sig_int, f"signal#{sig_int}")


def _read_proc_field(pid: int, key: str) -> Optional[str]:
    """从 /proc/<pid>/status 读取单个字段。仅 Linux；其他平台返回 None。"""
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(key + ":"):
                    return line.split(":", 1)[1].strip()
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return None


def _read_proc_cmdline(pid: int) -> Optional[str]:
    """把 /proc/<pid>/cmdline 作为可打印字符串读取。仅 Linux；其他平台返回 None。"""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            data = fh.read()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    if not data:
        return None
    # cmdline 使用 NUL 作为分隔符
    return data.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def _proc_summary(pid: int) -> Dict[str, Any]:
    """/proc/<pid> 的紧凑快照：pid、ppid、state、uid、cmdline。

    best-effort。缺失的字段会被直接省略，而不是抛异常。
    """
    summary: Dict[str, Any] = {"pid": pid}
    if pid <= 0:
        return summary
    name = _read_proc_field(pid, "Name")
    if name is not None:
        summary["name"] = name
    state = _read_proc_field(pid, "State")
    if state is not None:
        summary["state"] = state
    ppid = _read_proc_field(pid, "PPid")
    if ppid is not None:
        try:
            summary["ppid"] = int(ppid)
        except ValueError:
            pass
    uid = _read_proc_field(pid, "Uid")
    if uid is not None:
        # "real effective saved fs"（real effective saved fs）
        summary["uid"] = uid.split()[0] if uid else uid
    cmdline = _read_proc_cmdline(pid)
    if cmdline:
        # 激进地截断——这些可能长达 4KB
        summary["cmdline"] = cmdline[:300]
    return summary


def snapshot_shutdown_context(received_signal: Any = None) -> Dict[str, Any]:
    """对“是谁/什么在要求我们关闭”的快速（<10ms）快照。

    捕获：

    * 信号编号/名称（这样 SIGINT 与 SIGTERM 就能区分开）
    * 我们自己的 PID/ppid + 来自 /proc 的父进程信息（Linux）
    * systemd 是否是我们的父进程（``ppid==1`` 或设置了 ``INVOCATION_ID``）
    * 是否存在 takeover/planned-stop 标记（由调用方惰性消费）
    * /proc/self 的 limits + 负载均值（1 分钟）
    * 挂钟时间和单调时钟时间戳，用于后续阶段的交叉关联

    纯标准库，绝不抛异常，也绝不阻塞在子进程上。
    """
    now = time.time()
    monotonic = time.monotonic()
    pid = os.getpid()
    ppid = os.getppid()

    ctx: Dict[str, Any] = {
        "ts": now,
        "ts_monotonic": monotonic,
        "signal": _signal_name(received_signal),
        "signal_num": int(received_signal) if received_signal is not None else None,
        "pid": pid,
        "ppid": ppid,
        "parent": _proc_summary(ppid),
        "self": _proc_summary(pid),
    }

    # systemd 上下文。如果我们是由某个 systemd unit 启动的，我们的环境里
    # 会设置 INVOCATION_ID。ppid==1（init）也是一个强烈的信号，表明 systemd
    # 回收并转发了这个 SIGTERM。
    invocation_id = os.environ.get("INVOCATION_ID")
    if invocation_id:
        ctx["systemd_invocation_id"] = invocation_id
    journal_stream = os.environ.get("JOURNAL_STREAM")
    if journal_stream:
        ctx["systemd_journal_stream"] = journal_stream
    ctx["under_systemd"] = bool(invocation_id) or ppid == 1

    # 负载均值 —— 高负载会指向“有别的东西在压垮这台机器”，而不是
    # “外部杀手”。
    try:
        ctx["loadavg_1m"] = os.getloadavg()[0]
    except (OSError, AttributeError):
        pass

    # /proc/self/status 的 TracerPid：非零意味着有调试器 / strace 附着。
    # 当“幻影 SIGKILL”最终被证实是一次手动 gdb 会话时，这一项很有用。
    try:
        tracer = _read_proc_field(pid, "TracerPid")
        if tracer is not None and tracer != "0":
            ctx["tracer_pid"] = int(tracer) if tracer.isdigit() else tracer
            ctx["tracer"] = _proc_summary(int(tracer)) if tracer.isdigit() else None
    except (TypeError, ValueError):
        pass

    # 竞态检测提示：是否有人最近用 --replace 启动了一个兄弟 gateway？
    # 我们无法在这里直接看到新进程，但如果磁盘上有一个 takeover 标记，
    # 并且它 *没有* 指向我们，那就是“另一个 --replace 实例在杀我们”的
    # 铁证。文件名与 gateway.status 对应
    # （._TAKEOVER_MARKER_FILENAME / _PLANNED_STOP_MARKER_FILENAME）；
    # 我们在这里使用字符串字面量，以保持信号处理器路径的导入足够轻量。
    try:
        hermes_home_str = os.environ.get("HERMES_HOME")
        if hermes_home_str:
            takeover_path = Path(hermes_home_str) / ".gateway-takeover.json"
            if takeover_path.exists():
                try:
                    raw = takeover_path.read_text(encoding="utf-8")
                    ctx["takeover_marker"] = raw[:300]
                    ctx["takeover_marker_for_self"] = (
                        f'"target_pid": {pid}' in raw
                        or f"'target_pid': {pid}" in raw
                    )
                except OSError:
                    pass
            planned_stop_path = Path(hermes_home_str) / ".gateway-planned-stop.json"
            if planned_stop_path.exists():
                try:
                    raw = planned_stop_path.read_text(encoding="utf-8")
                    ctx["planned_stop_marker"] = raw[:300]
                except OSError:
                    pass
    except Exception:  # noqa: BLE001 —— 绝不在信号处理器里抛异常
        pass

    return ctx


def spawn_async_diagnostic(
    log_path: Path,
    signal_name: str,
    *,
    timeout_seconds: float = 5.0,
) -> Optional[int]:
    """fire-and-forget 的 ``ps`` 风格快照，写入 ``log_path``。

    作为一个分离的子进程运行，因此它不会阻塞 asyncio 事件循环，也不会与
    平台拆卸竞争。子进程使用它自己的 ``timeout``，所以即使 ``ps`` 卡住，
    也会在 ``timeout_seconds`` 内自我清理。

    成功时返回子进程的 PID，失败时返回 ``None``。绝不抛异常。

    我们刻意避免在信号处理器内部使用 ``subprocess.run(["ps", "aux"])``
    （既有的模式）：在一个有数百个进程的繁忙主机上，``ps aux`` 遍历
    /proc 可能需要 >2 秒，在此期间 asyncio 循环会被冻结，adapter 的
    拆卸无法开始。
    """
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    # 内联 shell，这样我们就不必随包附带一个辅助脚本。bash -c 在我们支持
    # 的每个 POSIX 目标上都可用；在 Windows 上我们直接跳过快照
    # （该平台本来也不自带 ps）。
    if sys.platform == "win32":
        return None

    script = (
        f"echo '=== shutdown diagnostic @ {signal_name} ==='; "
        "echo '--- date ---'; date -u +%Y-%m-%dT%H:%M:%SZ; "
        "echo '--- ps auxf (top 60 by cpu) ---'; "
        "ps auxf --sort=-pcpu 2>/dev/null | head -60; "
        "echo '--- pstree of self ---'; "
        f"pstree -plau {os.getpid()} 2>/dev/null | head -40 || true; "
        "echo '--- /proc/loadavg ---'; "
        "cat /proc/loadavg 2>/dev/null || true; "
        "echo '--- recent dmesg (oom/killed) ---'; "
        "dmesg -T 2>/dev/null | tail -20 || journalctl --user -n 20 --no-pager 2>/dev/null | tail -20 || true; "
        "echo '=== end ==='"
    )

    try:
        # 以 append 模式打开日志文件，并让子进程继承。我们使用 os.O_APPEND，
        # 这样来自快速连续信号的并发诊断就不会互相覆盖。
        fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    except OSError:
        return None

    try:
        # 与我们的进程组分离，这样即使 systemd 用 KillMode=control-group
        # 杀掉我们的 cgroup（反正那也会把我们回收掉，但这是纵深防御），
        # 子进程也能存活。没有 start_new_session 的话，对我们 cgroup 的
        # 一次 SIGKILL 会在诊断刷新前就把它干掉。
        proc = subprocess.Popen(
            ["timeout", f"{timeout_seconds:.0f}", "bash", "-c", script],
            stdout=fd,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except (FileNotFoundError, OSError):
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    finally:
        # 子进程已继承该 fd；我们可以丢弃自己的句柄。
        try:
            os.close(fd)
        except OSError:
            pass

    return proc.pid


def format_context_for_log(ctx: Dict[str, Any]) -> str:
    """把关闭上下文 dict 渲染成单行、便于扫描的日志行。"""
    sig = ctx.get("signal", "?")
    parent = ctx.get("parent") or {}
    parent_cmd = parent.get("cmdline", "(unknown)")
    parent_name = parent.get("name") or "?"
    parent_pid = parent.get("pid") or "?"
    under_systemd = "yes" if ctx.get("under_systemd") else "no"
    load = ctx.get("loadavg_1m")
    load_str = f"{load:.2f}" if isinstance(load, (int, float)) else "?"
    extras: List[str] = []
    if ctx.get("takeover_marker") is not None:
        for_self = ctx.get("takeover_marker_for_self")
        extras.append(
            f"takeover_marker_present={'self' if for_self else 'other'}"
        )
    if ctx.get("planned_stop_marker") is not None:
        extras.append("planned_stop_marker_present=yes")
    if ctx.get("tracer_pid"):
        extras.append(f"tracer_pid={ctx['tracer_pid']}")
    extras_str = (" " + " ".join(extras)) if extras else ""
    # 父进程的 cmdline 是最有用的单一信号——醒目地记录它。
    return (
        f"signal={sig} "
        f"under_systemd={under_systemd} "
        f"parent_pid={parent_pid} "
        f"parent_name={parent_name} "
        f"loadavg_1m={load_str}"
        f"{extras_str} "
        f"parent_cmdline={parent_cmd!r}"
    )


def context_as_json(ctx: Dict[str, Any]) -> str:
    """把上下文 dict JSON 序列化，用于结构化摄入。绝不抛异常。"""
    try:
        return json.dumps(ctx, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return "{}"


def check_systemd_timing_alignment(drain_timeout: float) -> Optional[Dict[str, Any]]:
    """在启动时，做一次健全性检查，确保 systemd 的 TimeoutStopSec >= drain_timeout。

    当 gateway 运行在一个过期的 systemd unit 文件下时（例如用户升级了
    hermes-agent 但从未重新运行 ``hermes setup`` 来重新生成 unit），
    ``TimeoutStopSec`` 可能小于已配置的 ``restart_drain_timeout``。结果是：
    SIGTERM 到达，drain 开始，然后 systemd 在 drain 进行到一半时对 cgroup
    发出 SIGKILL——这在 journal 里看起来像是一次幻影 kill，因为 journal
    只记录了 ``code=killed status=9``。

    当对齐正常，或我们无法判断（不是在 systemd 下运行、``systemctl`` 不可用
    等）时返回 ``None``。当我们有数据可报告时，返回一个包含
    ``timeout_stop_sec`` + ``drain_timeout`` + ``mismatch`` 布尔值的 dict。

    best-effort。绝不抛异常。
    """
    invocation_id = os.environ.get("INVOCATION_ID")
    if not invocation_id:
        return None  # 不是在 systemd 下运行（至少不是直接运行）

    # 尝试识别我们的 unit 名称，并向 systemctl 询问它的配置。
    unit_name: Optional[str] = None
    try:
        # /proc/self/cgroup 给我们 "0::/user.slice/.../hermes-gateway.service"
        with open("/proc/self/cgroup", encoding="utf-8") as fh:
            for line in fh:
                # systemd 的 cgroup 行以 unit 名称结尾
                if ".service" in line:
                    parts = line.strip().split("/")
                    for p in reversed(parts):
                        if p.endswith(".service"):
                            unit_name = p
                            break
                    if unit_name:
                        break
    except (OSError, FileNotFoundError):
        pass
    if not unit_name:
        return None

    # 向 systemctl 查询 TimeoutStopUSec。根据实际拥有该 unit 的 manager，
    # 使用 --user 或 system。先尝试 user，因为那是 hermes 的常见情况。
    timeout_us: Optional[int] = None
    for flag in (["--user"], []):
        try:
            result = subprocess.run(
                ["systemctl", *flag, "show", unit_name, "--property=TimeoutStopUSec"],
                capture_output=True, text=True, timeout=2.0,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode != 0:
            continue
        # 输出："TimeoutStopUSec=1min 30s" 或 "TimeoutStopUSec=90000000"
        for line in result.stdout.splitlines():
            if line.startswith("TimeoutStopUSec="):
                value = line.split("=", 1)[1].strip()
                # 先尝试数字形式的微秒
                if value.isdigit():
                    timeout_us = int(value)
                else:
                    timeout_us = _parse_systemd_duration_to_us(value)
                if timeout_us is not None:
                    break
        if timeout_us is not None:
            break

    if timeout_us is None:
        return None

    timeout_stop_sec = timeout_us / 1_000_000.0
    # systemd 需要余量用于：中断后的 kill、adapter 断开、SessionDB 关闭、
    # 文件取消链接等。30s 与 hermes_cli/gateway.py 中 unit 模板里的常量一致。
    headroom = 30.0
    expected = drain_timeout + headroom
    return {
        "unit": unit_name,
        "timeout_stop_sec": timeout_stop_sec,
        "drain_timeout": drain_timeout,
        "expected_min": expected,
        "mismatch": timeout_stop_sec < expected,
    }


def _parse_systemd_duration_to_us(raw: str) -> Optional[int]:
    """把 'TimeoutStopUSec=1min 30s' / '90s' 风格的值解析为微秒。

    systemd 接受很宽泛的语法；我们覆盖常见情况（s、ms、min、h），
    对任何无法识别的值返回 None。绝不抛异常。
    """
    if not raw:
        return None
    units = {
        "us": 1,
        "ms": 1_000,
        "s": 1_000_000,
        "sec": 1_000_000,
        "min": 60_000_000,
        "h": 3_600_000_000,
        "hr": 3_600_000_000,
    }
    total_us = 0
    token = ""
    digits = ""
    for ch in raw + " ":
        if ch.isdigit() or ch == ".":
            if token:
                # 结束上一个单位，开始新的数字
                multiplier = units.get(token.lower())
                if multiplier is None or not digits:
                    return None
                try:
                    total_us += int(float(digits) * multiplier)
                except ValueError:
                    return None
                digits = ""
                token = ""
            digits += ch
        elif ch.isalpha():
            token += ch
        elif digits and token:
            multiplier = units.get(token.lower())
            if multiplier is None:
                return None
            try:
                total_us += int(float(digits) * multiplier)
            except ValueError:
                return None
            digits = ""
            token = ""
        elif digits and not token:
            # 裸数字 = 秒（罕见但合法）
            try:
                total_us += int(float(digits) * 1_000_000)
            except ValueError:
                return None
            digits = ""
    return total_us if total_us > 0 else None
