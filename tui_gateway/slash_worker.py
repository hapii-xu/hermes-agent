"""持久化斜杠命令 worker — 每个 TUI 会话一个 HermesCLI。

协议：从 stdin 读取 JSON 行 {id, command}，将 {id, ok, output|error} 写入 stdout。
"""

import argparse
import contextlib
import io
import json
import os
import sys
import threading
import time

import psutil

import cli as cli_mod
from cli import HermesCLI
from rich.console import Console

# 可通过环境变量覆盖，以便集成测试驱动亚秒级时序。
def _env_float(name: str, default: float) -> float:
    """解析一个 float 类型的环境变量旋钮，在缺失或格式错误时
    回退到 ``default``。直接的 ``float(os.environ.get(...))`` 在
    拼写错误时（例如 ``HERMES_SLASH_WATCHDOG_POLL_S=2s``）会在
    导入时抛出 ValueError，在 worker 能服务任何命令之前就终止了。
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


_WATCHDOG_POLL_S = max(0.05, _env_float("HERMES_SLASH_WATCHDOG_POLL_S", 2.0))
_ORPHAN_GRACE_S = max(0.0, _env_float("HERMES_SLASH_WATCHDOG_GRACE_S", 5.0))
_in_flight = threading.Event()  # set while a command is executing


def _is_orphaned(original_ppid, parent_create_time, getppid=os.getppid) -> bool:
    """一旦生成我们的网关消失则返回 True。与原始的 ppid 比较
    （永远不会是 1：Linux 会将孤儿进程重新父化到 subreaper）
    并通过 create_time 防护 PID 复用。
    """
    if getppid() != original_ppid:
        return True
    try:
        if not psutil.pid_exists(original_ppid):
            return True
        return psutil.Process(original_ppid).create_time() != parent_create_time
    except psutil.Error:
        return True


def _start_parent_death_watchdog(original_ppid, parent_create_time) -> None:
    def _loop():
        while not _is_orphaned(original_ppid, parent_create_time):
            time.sleep(_WATCHDOG_POLL_S)
        deadline = time.monotonic() + _ORPHAN_GRACE_S
        while _in_flight.is_set() and time.monotonic() < deadline:
            time.sleep(0.05)  # 让正在执行中的命令完成/刷新
        os._exit(0)

    threading.Thread(target=_loop, daemon=True).start()


def _run(cli: HermesCLI, command: str) -> str:
    cmd = (command or "").strip()
    if not cmd:
        return ""
    if not cmd.startswith("/"):
        cmd = f"/{cmd}"

    buf = io.StringIO()

    # Rich Console 在构造时捕获其文件句柄，所以
    # contextlib.redirect_stdout 不会影响它。将 console 的
    # 底层文件交换到我们的缓冲区，使 self.console.print() 被捕获。
    cli.console = Console(file=buf, force_terminal=True, width=120)

    old = getattr(cli_mod, "_cprint", None)
    if old is not None:
        cli_mod._cprint = lambda text: print(text)

    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            cli.process_command(cmd)
    finally:
        if old is not None:
            cli_mod._cprint = old

    return buf.getvalue().rstrip()


def main():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--session-key", required=True)
    p.add_argument("--model", default="")
    args = p.parse_args()

    os.environ["HERMES_SESSION_KEY"] = args.session_key
    os.environ["HERMES_INTERACTIVE"] = "1"

    # 在 HermesCLI 构建（数百毫秒）之前启动 — 这个时间窗口
    # 本身就是一个孤儿风险，如果网关在生成过程中死亡。
    orig_ppid = os.getppid()
    try:
        parent_create_time = psutil.Process(orig_ppid).create_time()
    except psutil.Error:
        parent_create_time = 0.0
    _start_parent_death_watchdog(orig_ppid, parent_create_time)

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        cli = HermesCLI(model=args.model or None, compact=True, resume=args.session_key, verbose=False)

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue

        _in_flight.set()
        rid = None
        try:
            req = json.loads(line)
            rid = req.get("id")
            out = _run(cli, req.get("command", ""))
            sys.stdout.write(json.dumps({"id": rid, "ok": True, "output": out}) + "\n")
            sys.stdout.flush()
        except Exception as e:
            sys.stdout.write(json.dumps({"id": rid, "ok": False, "error": str(e)}) + "\n")
            sys.stdout.flush()
        finally:
            _in_flight.clear()


if __name__ == "__main__":
    main()
