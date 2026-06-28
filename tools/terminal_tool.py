#!/usr/bin/env python3
"""
终端工具模块

一个终端工具，可在 local、Docker、Modal、SSH、Singularity 和 Daytona 环境下执行命令。
支持本地执行、容器化后端以及云沙箱，包含托管的 Modal 模式。

支持的环境：
- "local"：直接在宿主机上执行（默认，最快）
- "docker"：在 Docker 容器中执行（隔离，需要 Docker）
- "modal"：在 Modal 云沙箱中执行（直连 Modal 或托管网关）

特性：
- 多种执行后端（local、docker、modal）
- 支持后台任务
- VM/容器生命周期管理
- 闲置后自动清理

云沙箱说明：
- 持久化文件系统会在沙箱重建后保留工作状态
- 持久化文件系统并不保证同一个存活的沙箱或长时间运行的进程能在清理、闲置回收或 Hermes 退出后继续存活

用法：
    from terminal_tool import terminal_tool

    # 执行一条简单命令
    result = terminal_tool("ls -la")

    # 在后台执行
    result = terminal_tool("python server.py", background=True)
"""

import importlib.util
import json
import logging
import os
import platform
import re
import time
import threading
import atexit
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List

from utils import env_var_enabled

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 全局中断事件：当用户中断到来时由 agent 设置。
# 终端工具在命令执行期间轮询该事件，以便能立即终止长时间运行的子进程，
# 而不是阻塞直到超时。
# ---------------------------------------------------------------------------
from tools.interrupt import is_interrupted, _interrupt_event  # noqa: F401 — 重新导出
# display_hermes_home 在调用处懒加载（hermes 更新期间的旧模块安全防护）




# =============================================================================
# 自定义 Singularity 环境（空间更大）
# =============================================================================

# Singularity 辅助函数（scratch 目录、SIF 缓存）现位于 tools/environments/singularity.py
from tools.environments.singularity import _get_scratch_dir
from tools.tool_backend_helpers import (
    coerce_modal_mode,
    has_direct_modal_credentials,
    managed_nous_tools_enabled,
    nous_tool_gateway_unavailable_message,
    resolve_modal_backend_state,
)


def _safe_parse_import_env(
    name: str,
    default: Any,
    converter,
    type_label: str,
):
    """解析模块级数值型环境变量，且不会破坏导入。

    终端工具会被 CLI、ACP、测试和工具发现机制导入。单个格式错误的环境变量
    不能让整个模块在导入时无法加载。
    """
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return converter(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid value for %s: %r (expected %s). Falling back to %r.",
            name,
            raw,
            type_label,
            default,
        )
        return default


# 前台超时的硬上限；可通过 TERMINAL_MAX_FOREGROUND_TIMEOUT 环境变量覆盖。
FOREGROUND_MAX_TIMEOUT = _safe_parse_import_env(
    "TERMINAL_MAX_FOREGROUND_TIMEOUT",
    600,
    int,
    "integer",
)

# 磁盘使用量告警阈值（单位 GB）
DISK_USAGE_WARNING_THRESHOLD_GB = _safe_parse_import_env(
    "TERMINAL_DISK_WARNING_GB",
    500.0,
    float,
    "number",
)


def _check_disk_usage_warning():
    """检查磁盘总使用量是否超过告警阈值。"""
    try:
        scratch_dir = _get_scratch_dir()

        # 统计 hermes 各目录的总大小
        total_bytes = 0
        import glob
        for path in glob.glob(str(scratch_dir / "hermes-*")):
            for f in Path(path).rglob('*'):
                if f.is_file():
                    try:
                        total_bytes += f.stat().st_size
                    except OSError as e:
                        logger.debug("Could not stat file %s: %s", f, e)
        
        total_gb = total_bytes / (1024 ** 3)
        
        if total_gb > DISK_USAGE_WARNING_THRESHOLD_GB:
            logger.warning("Disk usage (%.1fGB) exceeds threshold (%.0fGB). Consider running cleanup_all_environments().",
                           total_gb, DISK_USAGE_WARNING_THRESHOLD_GB)
            return True
        
        return False
    except Exception as e:
        logger.debug("Disk usage warning check failed: %s", e, exc_info=True)
        return False


# 交互式 sudo 密码缓存。
#
# 当存在会话键时，将缓存作用域限定在当前活动会话；其次是回调身份（ACP / CLI
# 交互式回调）；再次是当前线程。这样可以防止在同一个长生命周期进程内，一个
# 交互式会话复用另一个会话已缓存的 sudo 密码。
_sudo_password_cache: dict[str, str] = {}
_sudo_password_cache_lock = threading.Lock()

# 用于交互式提示的可选 UI 回调。设置后，会用它们代替默认的 /dev/tty 或 input()
# 读取器。CLI 会注册这些回调，使提示经由 prompt_toolkit 的事件循环处理。
# 这些回调槽位由审批提示和 sudo 密码提示流程使用。它们存放在线程本地状态中，
# 这样并发运行的 ACP 会话——各自运行在自己的 ThreadPoolExecutor 线程里——
# 不会相互覆盖对方的回调。参见 GHSA-qg5c-hvr5-hjgr。
#
# CLI 模式是单线程的，因此每个线程（也就是唯一那个）持有一个自己的回调，
# 与之前完全一致。Gateway 模式通过 tools.approval 中按会话划分的队列来处理
# 审批，不走这些回调，因此不受影响。
_callback_tls = threading.local()


def _get_sudo_password_callback():
    return getattr(_callback_tls, "sudo_password", None)


def _get_approval_callback():
    return getattr(_callback_tls, "approval", None)


def set_sudo_password_callback(cb):
    """注册一个用于 sudo 密码提示的回调（由 CLI 使用）。

    按线程划分作用域——在 ThreadPoolExecutor 中并发运行的 ACP 会话，
    各自拥有独立的回调槽位。
    """
    _callback_tls.sudo_password = cb


def set_approval_callback(cb):
    """注册一个用于危险命令审批提示的回调。

    按线程划分作用域——在 ThreadPoolExecutor 中并发运行的 ACP 会话，
    各自拥有独立的回调槽位。参见 GHSA-qg5c-hvr5-hjgr。
    """
    _callback_tls.approval = cb


def _get_sudo_password_cache_scope() -> str:
    """返回交互式 sudo 密码的缓存作用域。"""
    try:
        from gateway.session_context import get_session_env

        session_key = get_session_env("HERMES_SESSION_KEY", "")
    except Exception:
        session_key = os.getenv("HERMES_SESSION_KEY", "")
    if session_key:
        return f"session:{session_key}"

    callback = _get_sudo_password_callback()
    if callback is not None:
        owner = getattr(callback, "__self__", None)
        func = getattr(callback, "__func__", None)
        if owner is not None and func is not None:
            return f"callback-owner:{id(owner)}:{id(func)}"
        return f"callback:{id(callback)}"

    return f"thread:{threading.get_ident()}"


def _get_cached_sudo_password() -> str:
    """返回当前作用域下已缓存的 sudo 密码。"""
    scope = _get_sudo_password_cache_scope()
    with _sudo_password_cache_lock:
        return _sudo_password_cache.get(scope, "")


def _set_cached_sudo_password(password: str) -> None:
    """为当前作用域持久化一个 sudo 密码。"""
    scope = _get_sudo_password_cache_scope()
    with _sudo_password_cache_lock:
        if password:
            _sudo_password_cache[scope] = password
        else:
            _sudo_password_cache.pop(scope, None)


def _reset_cached_sudo_passwords() -> None:
    """清空所有已缓存的 sudo 密码。

    供测试和进程退出路径使用的内部辅助函数。
    """
    with _sudo_password_cache_lock:
        _sudo_password_cache.clear()

# =============================================================================
# 危险命令审批系统
# =============================================================================

# 危险命令检测 + 审批现已整合到 tools/approval.py
from tools.approval import (
    check_all_command_guards as _check_all_guards_impl,
)


def _check_all_guards(command: str, env_type: str) -> dict:
    """委托给整合后的守卫（tirith + 危险命令），并附带 CLI 回调。"""
    return _check_all_guards_impl(command, env_type,
                                  approval_callback=_get_approval_callback())


# 允许清单：可以合法出现在目录路径中的字符。
# 包括字母数字、路径分隔符、Windows 驱动器/UNC 分隔符、波浪号、点、连字符、
# 下划线、空格、加号、@、等号和逗号。其余字符一律拒绝。
_WORKDIR_SAFE_RE = re.compile(r'^[A-Za-z0-9/\\:_\-.~ +@=,]+$')


def _validate_workdir(workdir: str) -> str | None:
    """拒绝那些看起来不像文件系统路径的 workdir 值。

    使用安全字符允许清单而非黑名单，因此新型的 shell 元字符无法混入。

    安全时返回 None，危险时返回错误消息字符串。
    """
    if not workdir:
        return None
    if not _WORKDIR_SAFE_RE.match(workdir):
        # 找到第一个违规字符，以便给出有用的提示。
        for ch in workdir:
            if not _WORKDIR_SAFE_RE.match(ch):
                return (
                    f"Blocked: workdir contains disallowed character {repr(ch)}. "
                    "Use a simple filesystem path without shell metacharacters."
                )
        return "Blocked: workdir contains disallowed characters."
    return None


def _handle_sudo_failure(output: str, env_type: str) -> str:
    """
    检查 sudo 失败情况，并为消息上下文追加帮助信息。

    如果在消息上下文中 sudo 失败，则返回增强后的输出；否则返回原始输出。
    """
    is_gateway = env_var_enabled("HERMES_GATEWAY_SESSION")

    if not is_gateway:
        return output

    # 检查 sudo 失败的指示特征
    sudo_failures = [
        "sudo: a password is required",
        "sudo: no tty present",
        "sudo: a terminal is required",
    ]
    
    for failure in sudo_failures:
        if failure in output:
            from hermes_constants import display_hermes_home as _dhh
            return output + f"\n\n💡 Tip: To enable sudo over messaging, add SUDO_PASSWORD to {_dhh()}/.env on the agent machine."
    
    return output


def _prompt_for_sudo_password(timeout_seconds: int = 45) -> str:
    """
    带超时地提示用户输入 sudo 密码。

    如果用户输入了密码则返回该密码；否则返回空字符串，触发条件：
    - 用户未输入直接按回车（跳过）
    - 超时（默认 45 秒）
    - 发生任何错误

    仅在交互模式（HERMES_INTERACTIVE=1）下工作。
    如果已注册 _sudo_password_callback（由 CLI 注册），则委托给它，
    使提示能集成到 prompt_toolkit 的 UI 中。否则直接从 /dev/tty
    读取并关闭回显。
    """
    import sys

    # 当可用时使用已注册的回调（兼容 prompt_toolkit）
    _sudo_cb = _get_sudo_password_callback()
    if _sudo_cb is not None:
        try:
            return _sudo_cb() or ""
        except Exception:
            return ""

    result = {"password": None, "done": False}
    
    def read_password_thread():
        """关闭回显读取密码。Windows 上使用 msvcrt，Unix 上使用 /dev/tty。"""
        tty_fd = None
        old_attrs = None
        try:
            if platform.system() == "Windows":
                import msvcrt
                chars = []
                while True:
                    c = msvcrt.getwch()
                    if c in {"\r", "\n"}:
                        break
                    if c == "\x03":
                        raise KeyboardInterrupt
                    chars.append(c)
                result["password"] = "".join(chars)
            else:
                import termios
                tty_fd = os.open("/dev/tty", os.O_RDONLY)
                old_attrs = termios.tcgetattr(tty_fd)
                new_attrs = termios.tcgetattr(tty_fd)
                new_attrs[3] = new_attrs[3] & ~termios.ECHO
                termios.tcsetattr(tty_fd, termios.TCSAFLUSH, new_attrs)
                chars = []
                while True:
                    b = os.read(tty_fd, 1)
                    if not b or b in {b"\n", b"\r"}:
                        break
                    chars.append(b)
                result["password"] = b"".join(chars).decode("utf-8", errors="replace")
        except (EOFError, KeyboardInterrupt, OSError):
            result["password"] = ""
        except Exception:
            result["password"] = ""
        finally:
            if tty_fd is not None and old_attrs is not None:
                try:
                    import termios as _termios
                    _termios.tcsetattr(tty_fd, _termios.TCSAFLUSH, old_attrs)
                except Exception as e:
                    logger.debug("Failed to restore terminal attributes: %s", e)
            if tty_fd is not None:
                try:
                    os.close(tty_fd)
                except Exception as e:
                    logger.debug("Failed to close tty fd: %s", e)
            result["done"] = True
    
    try:
        os.environ["HERMES_SPINNER_PAUSE"] = "1"
        time.sleep(0.2)
        
        print()
        print("┌" + "─" * 58 + "┐")
        print("│  🔐 SUDO PASSWORD REQUIRED" + " " * 30 + "│")
        print("├" + "─" * 58 + "┤")
        print("│  Enter password below (input is hidden), or:            │")
        print("│    • Press Enter to skip (command fails gracefully)     │")
        print(f"│    • Wait {timeout_seconds}s to auto-skip" + " " * 27 + "│")
        print("└" + "─" * 58 + "┘")
        print()
        print("  Password (hidden): ", end="", flush=True)
        
        password_thread = threading.Thread(target=read_password_thread, daemon=True)
        password_thread.start()
        password_thread.join(timeout=timeout_seconds)
        
        if result["done"]:
            password = result["password"] or ""
            print()  # 隐藏输入后换行
            if password:
                print("  ✓ Password received (cached for this session)")
            else:
                print("  ⏭ Skipped - continuing without sudo")
            print()
            sys.stdout.flush()
            return password
        else:
            print("\n  ⏱ Timeout - continuing without sudo")
            print("    (Press Enter to dismiss)")
            print()
            sys.stdout.flush()
            return ""
            
    except (EOFError, KeyboardInterrupt):
        print()
        print("  ⏭ Cancelled - continuing without sudo")
        print()
        sys.stdout.flush()
        return ""
    except Exception as e:
        print(f"\n  [sudo prompt error: {e}] - continuing without sudo\n")
        sys.stdout.flush()
        return ""
    finally:
        if "HERMES_SPINNER_PAUSE" in os.environ:
            del os.environ["HERMES_SPINNER_PAUSE"]

def _safe_command_preview(command: Any, limit: int = 200) -> str:
    """为可能非法的命令值返回一个可安全记录日志的预览。"""
    if command is None:
        return "<None>"
    if isinstance(command, str):
        return command[:limit]
    try:
        return repr(command)[:limit]
    except Exception:
        return f"<{type(command).__name__}>"

def _looks_like_env_assignment(token: str) -> bool:
    """当 *token* 是位于行首的 shell 环境变量赋值时返回 True。"""
    if "=" not in token or token.startswith("="):
        return False
    name, _value = token.split("=", 1)
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name))


def _read_shell_token(command: str, start: int) -> tuple[str, int]:
    """从 *start* 处读取一个 shell token，保留其中的引号/转义。"""
    i = start
    n = len(command)

    while i < n:
        ch = command[i]
        if ch.isspace() or ch in ";|&()":
            break
        if ch == "'":
            i += 1
            while i < n and command[i] != "'":
                i += 1
            if i < n:
                i += 1
            continue
        if ch == '"':
            i += 1
            while i < n:
                inner = command[i]
                if inner == "\\" and i + 1 < n:
                    i += 2
                    continue
                if inner == '"':
                    i += 1
                    break
                i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        i += 1

    return command[start:i], i


def _rewrite_real_sudo_invocations(command: str) -> tuple[str, bool]:
    """仅改写真正的、未加引号的 sudo 命令词，不处理纯文本中的提及。"""
    out: list[str] = []
    i = 0
    n = len(command)
    command_start = True
    found = False

    while i < n:
        ch = command[i]

        if ch.isspace():
            out.append(ch)
            if ch == "\n":
                command_start = True
            i += 1
            continue

        if ch == "#" and command_start:
            comment_end = command.find("\n", i)
            if comment_end == -1:
                out.append(command[i:])
                break
            out.append(command[i:comment_end])
            i = comment_end
            continue

        if command.startswith("&&", i) or command.startswith("||", i) or command.startswith(";;", i):
            out.append(command[i:i + 2])
            i += 2
            command_start = True
            continue

        if ch in ";|&(":
            out.append(ch)
            i += 1
            command_start = True
            continue

        if ch == ")":
            out.append(ch)
            i += 1
            command_start = False
            continue

        token, next_i = _read_shell_token(command, i)
        if command_start and token == "sudo":
            out.append("sudo -S -p ''")
            found = True
        else:
            out.append(token)

        if command_start and _looks_like_env_assignment(token):
            command_start = True
        else:
            command_start = False
        i = next_i

    return "".join(out), found


def _sudo_nopasswd_works() -> bool:
    """当本地 sudo 当前可以免密执行时返回 True。

    仅对 `local` 终端后端进行探测；Docker/SSH/Modal 等不得继承宿主机的
    sudo 状态。每次调用都重新探测（不做进程级缓存），这样过期的 sudo
    时间戳就不会让后续命令在等待密码时静默阻塞。
    """
    terminal_env = os.getenv("TERMINAL_ENV", "local").strip().lower() or "local"
    if terminal_env != "local":
        return False

    try:
        probe = subprocess.run(
            ["sudo", "-n", "true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
        return probe.returncode == 0
    except Exception:
        return False


def _rewrite_compound_background(command: str) -> str:
    """在深度 0 处将 `A && B &`（或 `A || B &`）改写为 `A && { B & }`。

    Bash 解析 ``A && B &`` 时，`&&` 的优先级高于 `&`，因此它会为整个
    `A && B` 复合命令 fork 一个子 shell 并将其置入后台。在该子 shell 内，
    `B` 是前台运行的，所以子 shell 会等待 `B` 执行完毕。当 `B` 是一个长
    时间运行的进程（`python3 -m http.server`、`yes > /dev/null`，或任何
    不会自然退出的进程）时，子 shell 永远不会退出。它会泄漏为一个永远卡在
    ``wait4`` 上的进程——而且在此过程中，其打开的 stdout 管道可能会阻碍
    终端工具及时返回。

    将尾部改写为 `A && { B & }` 既保留了 `&&` 的错误语义（A 失败则跳过 B），
    又用一个花括号组替换了子 shell。花括号组在当前 shell 中运行（不 fork），
    把 B 作为一条简单命令放入后台（bash 在非交互模式下不会等待它），然后立即
    退出。B 作为一个正常的后台子进程运行，在父 shell 退出时成为孤儿进程。

    会处理重定向（``&>``、``2>&1``），并跳过引号字符串和括号子 shell 内的
    内容。对于简单的 ``cmd &`` 不做改动——该写法不存在子 shell 等待的 bug。
    """
    n = len(command)
    i = 0
    paren_depth = 0
    brace_depth = 0
    # *command* 中，当前语句在深度 0 处最近一次 `&&` / `||` 之后的位置；
    # 当没有链式运算符生效时为 -1。
    last_chain_op_end = -1
    rewrites: list[tuple[int, int]] = []  # (chain_op_end, amp_pos)

    while i < n:
        ch = command[i]

        # 换行符在深度 0 处结束一条语句——重置链式状态。
        # 在跳过空白之前先检查，以免漏掉它。
        if ch == "\n" and paren_depth == 0 and brace_depth == 0:
            last_chain_op_end = -1
            i += 1
            continue

        if ch.isspace():
            i += 1
            continue

        # 注释（仅在语句开头——保守处理：任何不在 token 内的 `#` 都结束该行）。
        # 下面的 `_read_shell_token` 会处理引号字符串，因此引号内的 `#` 是安全的。
        if ch == "#":
            nl = command.find("\n", i)
            if nl == -1:
                break
            i = nl
            continue

        if ch == "\\" and i + 1 < n:
            i += 2
            continue

        # 带引号的 token——通过共享的分词器整体消费掉。
        if ch in {"'", '"'}:
            _, next_i = _read_shell_token(command, i)
            i = max(next_i, i + 1)
            continue

        if ch == "(":
            paren_depth += 1
            i += 1
            continue

        if ch == ")":
            paren_depth = max(0, paren_depth - 1)
            i += 1
            continue

        # 花括号组：`{ ... }` 是一个组（不 fork 子 shell），并且 bash 要求
        # `{` 后面跟空白。我们跟踪深度，使得已经改写过的输出（`A && { B & }`）
        # 具有幂等性——内部的 `&` 属于该组的一部分，而不是需要重新改写的新复合
        # 命令。同时跳过组内部的内容，因为那里的 `A && B &` 本身已是合法结构。
        if ch == "{" and i + 1 < n and (command[i + 1].isspace() or command[i + 1] == "\n"):
            brace_depth += 1
            i += 1
            continue
        if ch == "}" and brace_depth > 0:
            brace_depth -= 1
            # 关闭一个组意味着复合语句结束；重置链式状态。
            last_chain_op_end = -1
            i += 1
            continue

        # 在括号或花括号组内部时跳过运算符——它们在自己的作用域内解析。
        # `(...)` 子 shell 存在同类 bug，但不是 agent 的常见写法；留待后续处理。
        if paren_depth > 0 or brace_depth > 0:
            i += 1
            continue

        # 深度 0 处的链式运算符
        if command.startswith("&&", i) or command.startswith("||", i):
            last_chain_op_end = i + 2
            i += 2
            continue

        # 语句终结符重置链式状态
        if ch == ";":
            last_chain_op_end = -1
            i += 1
            continue

        # 单个 `|`（管道）开始一个新的管道阶段；不要跨管道改写。
        # `||` 已在上面处理。
        if ch == "|":
            last_chain_op_end = -1
            i += 1
            continue

        # `&` 的处理：区分 `&&`、`&>`、fd 重定向（`>&`、`<&`）以及真正
        # 表示后台运行的 `&`。
        if ch == "&":
            # `&&` 已在上面处理；不会走到这里
            if i + 1 < n and command[i + 1] == ">":
                # `&>` 重定向——消费掉
                i += 2
                continue
            # `>&` / `<&` fd 目标——向前跳过空白查找
            j = i - 1
            while j >= 0 and command[j].isspace():
                j -= 1
            if j >= 0 and command[j] in "<>":
                i += 1
                continue
            # 真正的后台运算符
            if last_chain_op_end >= 0:
                rewrites.append((last_chain_op_end, i))
            last_chain_op_end = -1
            i += 1
            continue

        # 常规的未加引号 token——通过共享分词器跳过它
        _, next_i = _read_shell_token(command, i)
        i = max(next_i, i + 1)

    if not rewrites:
        return command

    # 从后往前应用改写，这样靠前的索引仍然有效。
    result = command
    for chain_end, amp_pos in reversed(rewrites):
        # 跳过 `&&`/`||` 之后的空白，使花括号组紧贴内部命令开始。
        insert_pos = chain_end
        while insert_pos < amp_pos and result[insert_pos].isspace():
            insert_pos += 1
        prefix = result[:insert_pos]
        middle = result[insert_pos:amp_pos]  # 内部命令 + 尾部空格
        suffix = result[amp_pos + 1 :]
        # bash 中 `{` 后需要一个空格；闭合的 `}` 前必须是 `;` 或 `&`——
        # 我们这里通过后台 `&` 提供了它。
        result = prefix + "{ " + middle + "& }" + suffix

    return result


def _transform_sudo_command(command: str | None) -> tuple[str | None, str | None]:
    """
    在 SUDO_PASSWORD 可用时，将 sudo 命令转换为使用 -S 标志。

    这是一个所有执行环境共用的辅助函数，用于在 local、SSH 和容器环境下
    提供一致的 sudo 处理。

    返回：
        (transformed_command, sudo_stdin)，其中：
        - transformed_command 将每一个裸 ``sudo`` 都替换成了
          ``sudo -S -p ''``，使 sudo 从 stdin 读取密码。
        - sudo_stdin 是带尾部换行的密码字符串，调用方必须将其前置到
          进程的 stdin 流上。sudo -S 只读取一行（即密码），并把 stdin 的
          其余部分透传给子命令，因此即使调用方还要管道传入自己的
          stdin_data，前置也是安全的。
        - 如果没有可用密码，sudo_stdin 为 None，命令原样返回，从而以
          "sudo: a password is required" 优雅失败。

    直接驱动子进程的调用方（local、ssh、docker、singularity）应将
    sudo_stdin 前置到自己的 stdin_data，并把合并后的字节传给 Popen 的
    stdin 管道。

    无法管道传入子进程 stdin 的调用方（modal、daytona）必须自行把密码
    嵌入到命令字符串中；具体如何处理非 None 的 sudo_stdin，请参见它们
    的 execute() 方法。

    如果未设置 SUDO_PASSWORD 且存在可用的交互式 UI
    （HERMES_INTERACTIVE=1 或已注册的 sudo 密码回调）：
      以 45 秒超时提示用户输入密码，并按会话缓存。

    如果未设置 SUDO_PASSWORD 且不是交互模式：
      命令按原样运行（以 "sudo: a password is required" 优雅失败）。
    """
    if command is None:
        return None, None
    transformed, has_real_sudo = _rewrite_real_sudo_invocations(command)
    if not has_real_sudo:
        return command, None

    has_configured_password = "SUDO_PASSWORD" in os.environ
    sudo_password = (
        os.environ.get("SUDO_PASSWORD", "")
        if has_configured_password
        else _get_cached_sudo_password()
    )

    # 配置了 sudoers NOPASSWD 的本地宿主机不应被强制走交互式 Hermes 密码
    # 提示或 sudo -S 密码管道路径。仅对 local 终端后端生效，这样
    # Docker/SSH/Modal 等就不能继承宿主机的 sudo 状态。每次调用都重新探测
    # （不做进程级缓存），因此过期的 sudo 时间戳不会让后续命令在不经 Hermes
    # 提示的情况下静默阻塞。
    if not has_configured_password and not sudo_password and _sudo_nopasswd_works():
        return command, None

    has_sudo_prompt_callback = _get_sudo_password_callback() is not None
    should_prompt_for_sudo = (
        env_var_enabled("HERMES_INTERACTIVE") or has_sudo_prompt_callback
    )
    if not has_configured_password and not sudo_password and should_prompt_for_sudo:
        sudo_password = _prompt_for_sudo_password(timeout_seconds=45)
        if sudo_password:
            _set_cached_sudo_password(sudo_password)

    if has_configured_password or sudo_password:
        # 需要尾部换行：sudo -S 只读取一行作为密码。
        return transformed, sudo_password + "\n"

    return command, None


# 环境类现在位于 tools/environments/
from tools.environments.local import LocalEnvironment as _LocalEnvironment
from tools.environments.singularity import SingularityEnvironment as _SingularityEnvironment
from tools.environments.ssh import SSHEnvironment as _SSHEnvironment
from tools.environments.docker import DockerEnvironment as _DockerEnvironment
from tools.environments.modal import ModalEnvironment as _ModalEnvironment
from tools.environments.managed_modal import ManagedModalEnvironment as _ManagedModalEnvironment
from tools.managed_tool_gateway import is_managed_tool_gateway_ready
import sys


# 面向 LLM 的工具描述
TERMINAL_TOOL_DESCRIPTION = """Execute shell commands on a Linux environment. Filesystem, current working directory, and exported environment variables persist between calls.

Do NOT use cat/head/tail to read files — use read_file instead.
Do NOT use grep/rg/find to search — use search_files instead.
Do NOT use ls to list directories — use search_files(target='files') instead.
Do NOT use sed/awk to edit files — use patch instead.
Do NOT use echo/cat heredoc to create files — use write_file instead.
Reserve terminal for: builds, installs, git, processes, scripts, network, package managers, and anything that needs a shell.
Because exported environment state persists, activate a virtualenv or export setup variables once per session; do not re-source the same environment before every command unless a command proves the shell state was reset.

Foreground (default): Commands return INSTANTLY when done, even if the timeout is high. Set timeout=300 for long builds/scripts — you'll still get the result in seconds if it's fast. Prefer foreground for short commands.
Background: Set background=true to get a session_id. Almost always pair with notify_on_complete=true — bg without notify runs SILENTLY and you have no way to learn it finished short of calling process(action='poll') yourself. Two legitimate uses:
  (1) Long-lived processes that never exit (servers, watchers, daemons) — silent is correct, there's no exit to notify on.
  (2) Long-running bounded tasks (tests, builds, deploys, CI pollers, batch jobs) — MUST set notify_on_complete=true. Without it you'll either forget to poll or sit blocked waiting for the user to surface the result.
For servers/watchers, do NOT use shell-level background wrappers (nohup/disown/setsid/trailing '&') in foreground mode. Use background=true so Hermes can track lifecycle and output.
After starting a server, verify readiness with a health check or log signal, then run tests in a separate terminal() call. Avoid blind sleep loops.
Use process(action="poll") for progress checks, process(action="wait") to block until done.
Working directory: Use 'workdir' for per-command cwd.
PTY mode: Set pty=true for interactive CLI tools (Codex, Claude Code, Python REPL).

Do NOT use vim/nano/interactive tools without pty=true — they hang without a pseudo-terminal. Pipe git output to cat if it might page.
"""

# 用于环境生命周期管理的全局状态
_active_environments: Dict[str, Any] = {}
_last_activity: Dict[str, float] = {}
_env_lock = threading.Lock()
_creation_locks: Dict[str, threading.Lock] = {}  # 按任务划分的沙箱创建锁
_creation_locks_lock = threading.Lock()  # 保护 _creation_locks 字典本身
_cleanup_thread = None
_cleanup_running = False

# docker 孤儿回收器的「每进程一次」守卫（issue #20561）。
# 在 _maybe_reap_docker_orphans 首次运行时设置；并行子 agent 的并发
# _create_environment 调用不会重复触发扫描。
_docker_orphan_reaper_ran = False
_docker_orphan_reaper_lock = threading.Lock()


def _maybe_reap_docker_orphans(container_config: Dict[str, Any]) -> None:
    """如果已启用，则每个进程运行一次 docker 孤儿回收器。

    扫描当前 profile 下带有 ``hermes-agent=1`` 标签且已长时间处于 Exited 状态
    的容器，这些容器属于 issue #20561 描述的泄漏类别——即 Hermes 进程在没有触发
    ``atexit`` 的情况下退出（SIGKILL、OOM、关闭终端窗口）所遗留的容器。回收器
    默认是保守的：只回收退出时间超过 ``2 × lifetime_seconds`` 且属于当前 profile
    的 Exited 容器。

    门控条件：

    * ``terminal.docker_orphan_reaper: false`` 会完全禁用它（运维人员主动选择
      退出——通常是因为他们在同一个 profile 下运行多个 Hermes 进程，且不信任这些
      保守的默认值）。
    * ``_docker_orphan_reaper_ran`` 标志——扫描每个 Python 解释器只运行一次，
      而不是在每次子 agent / RL rollout / 并行 ``terminal()`` 调用时都运行。
    """
    global _docker_orphan_reaper_ran
    if not container_config.get("docker_orphan_reaper", True):
        return
    # 低开销的双重检查锁定：先不加锁读取，仅在首次运行时加锁，
    # 并在锁内再次检查。
    if _docker_orphan_reaper_ran:
        return
    with _docker_orphan_reaper_lock:
        if _docker_orphan_reaper_ran:
            return
        _docker_orphan_reaper_ran = True

    # 2 × lifetime_seconds 为兄弟 Hermes 进程留出宽裕的宽限窗口。
    # 下限为 60 秒，这样设置 TERMINAL_LIFETIME_SECONDS=0 的运维人员不会
    # 遇到与自身初始化竞争的「立即回收」。
    # ``container_config`` 只包含 container_* 相关键，因此从模块其余部分
    # 使用的环境变量中读取 lifetime_seconds。
    try:
        lifetime = int(os.getenv("TERMINAL_LIFETIME_SECONDS", "300"))
    except (TypeError, ValueError):
        lifetime = 300
    lifetime = max(60, lifetime)
    max_age = lifetime * 2

    try:
        from tools.environments.docker import (
            reap_orphan_containers, _get_active_profile_name,
        )
    except ImportError:
        return
    try:
        profile = _get_active_profile_name()
        removed = reap_orphan_containers(
            max_age_seconds=max_age, profile_filter=profile,
        )
        if removed:
            logger.info(
                "Docker orphan reaper removed %d stale container(s) for profile %s",
                removed, profile,
            )
    except Exception as e:
        # 绝不因为清理工作的问题而导致环境创建路径失败。
        logger.debug("Docker orphan reaper raised: %s", e)


# 按任务划分的环境覆盖注册表。
# 允许环境（例如 TerminalBench2Env）在 agent 循环开始之前，为某个具体的
# task_id 指定自定义的 Docker/Modal 镜像。当终端或文件工具为该 task_id 创建
# 新沙箱时，会先检查此注册表；若没有对应的覆盖设置，则回退到
# TERMINAL_MODAL_IMAGE（等）环境变量。
#
# 这永远不会暴露给模型——只有基础设施代码会调用它。
# 因每个 task_id 在单次 rollout 中是唯一的，所以是线程安全的。
_task_env_overrides: Dict[str, Dict[str, Any]] = {}


def register_task_env_overrides(task_id: str, overrides: Dict[str, Any]):
    """
    为某个具体的任务/rollout 注册环境覆盖配置。

    由 Atropos 环境在 agent 循环之前调用，用于配置按任务划分的沙箱设置
    （例如为 Modal 镜像指定自定义 Dockerfile）。

    支持的覆盖键：
        - modal_image: str -- Dockerfile 路径或 Docker Hub 镜像名
        - docker_image: str -- Docker 镜像名
        - cwd: str -- 沙箱内部的工作目录

    参数：
        task_id: 该 rollout 的唯一任务标识符
        overrides: 要覆盖的配置键字典
    """
    _task_env_overrides[task_id] = overrides

    # 如果该任务已存在一个存活的环境，那么新注册的 ``cwd`` 覆盖（例如 ACP 客户端
    # 在会话过程中通过 ``session/load`` / ``session/resume`` 切换编辑器的项目根目录）
    # 必须也对已缓存的环境生效。``terminal_tool`` 将每条命令的 cwd 解析顺序定为
    # ``workdir > env.cwd > config/override cwd``，从而保留会话内的普通 ``cd`` 状态；
    # 如果不在此处同步，覆盖配置会位于（已设置的）``env.cwd`` 之下，并在任何命令运行
    # 后被静默忽略。将其推送到存活环境上，可以保持 ``cd`` 跟踪完整，同时让显式的
    # ACP cwd 修改按客户端预期那样生效。
    new_cwd = overrides.get("cwd")
    if isinstance(new_cwd, str) and new_cwd.strip():
        # 存活环境对于按会话划分的界面（ACP/gateway/dashboard）以原始 task_id 缓存，
        # 而对于以隔离键划分的 rollout 则以折叠后的容器 id 缓存。先尝试原始 id，
        # 再尝试容器 id，这样仅含 CWD 的覆盖（会折叠为 "default"）仍能找到并更新
        # 发起会话的环境。
        container_id = _resolve_container_task_id(task_id)
        with _env_lock:
            env = _active_environments.get(task_id) or _active_environments.get(container_id)
        if env is not None and getattr(env, "cwd", None) is not None:
            env.cwd = new_cwd


def clear_task_env_overrides(task_id: str):
    """
    在 rollout 完成后清除某个任务的环境覆盖配置。

    在清理阶段调用，以避免陈旧条目堆积。
    """
    _task_env_overrides.pop(task_id, None)


def _resolve_container_task_id(task_id: Optional[str]) -> str:
    """
    将工具调用的 ``task_id`` 映射为 ``_active_environments`` 使用的
    容器/沙箱键。

    顶层 agent 传入 ``task_id=None``，落到 ``"default"`` 上。
    ``delegate_task`` 的子任务传入各自的 subagent ID，使文件状态跟踪、
    活动子 agent 注册表和 TUI 事件在每个子任务中保持独立——但我们在此
    处故意将该 ID 折叠回 ``"default"``，这样子 agent 就能共享父级的
    长生命周期容器（一个 bash、一个 /workspace、一套已安装的包）。

    例外：RL / 基准测试环境（TerminalBench2、HermesSweEnv 等）会调用
    ``register_task_env_overrides(task_id, {...})`` 来请求按任务划分的
    Docker/Modal 镜像。当为某个 task_id 注册了覆盖配置时，我们尊重它，
    原样返回该 task_id——这些 rollout 需要自己独立的沙箱，这正是覆盖配置
    的全部意义所在。

    仅含 CWD 的覆盖（由 ACP 适配器为工作区跟踪而注册）*不是* 隔离信号——
    它们不应导致每个会话都启动各自的容器。只有包含后端专用镜像键或
    ``env_type`` 的覆盖才会触发隔离。
    """
    _ISOLATION_KEYS = frozenset({
        "docker_image", "modal_image", "singularity_image",
        "daytona_image", "env_type",
    })
    if task_id and task_id in _task_env_overrides:
        overrides = _task_env_overrides[task_id]
        if set(overrides.keys()) & _ISOLATION_KEYS:
            return task_id
    return "default"


def resolve_task_overrides(task_id: Optional[str]) -> Dict[str, Any]:
    """返回 *task_id* 对应的环境覆盖配置，先查原始键再查折叠后的键。

    ``register_task_env_overrides`` 写入时用的是 *原始* 的任务/会话 id，但
    仅含 CWD 的覆盖会折叠（:func:`_resolve_container_task_id`）到共享的
    ``"default"`` 容器上，这样按会话划分的界面（ACP/gateway/dashboard）
    就不会各自启动自己的沙箱。因此，需要该覆盖配置的调用方（终端命令初始化、
    文件工具 cwd 解析）必须先读取原始 id，只有在找不到时才回退到折叠后的
    容器 id，否则发起会话的覆盖配置会被静默丢弃。这是该查找的唯一来源，
    以保证终端层和文件层不会产生不一致。
    """
    raw = task_id or "default"
    return (
        _task_env_overrides.get(raw)
        or _task_env_overrides.get(_resolve_container_task_id(raw))
        or {}
    )


# 从环境变量读取配置

def _parse_env_var(name: str, default: str, converter: Any = int, type_label: str = "integer"):
    """用 *converter* 解析环境变量，遇到非法值时抛出清晰的错误。

    如果没有这个封装，单个格式错误的环境变量（例如 TERMINAL_TIMEOUT=5m）
    就会引发未处理的 ValueError，从而导致所有终端命令失效。
    """
    raw = os.getenv(name, default)
    try:
        return converter(raw)
    except (ValueError, json.JSONDecodeError):
        raise ValueError(
            f"Invalid value for {name}: {raw!r} (expected {type_label}). "
            f"Check ~/.hermes/.env or environment variables."
        )


def _safe_getcwd() -> str:
    """返回当前工作目录，并容忍 CWD 已被删除的情况。

    当进程的工作目录在其下方被删除时（例如会话过程中被清理掉的 scratch
    工作区），``os.getcwd()`` 会抛出 FileNotFoundError。此时回退到
    TERMINAL_CWD，再回退到用户主目录，确保终端初始化不会因陈旧的 CWD 而崩溃。
    """
    try:
        return os.getcwd()
    except FileNotFoundError:
        return os.getenv("TERMINAL_CWD") or os.path.expanduser("~")


# 用于识别 *宿主机* 工作目录（无法存在于容器沙箱内部）的路径前缀。
# 涵盖 POSIX 用户目录和 Windows 驱动器路径（``C:\Users\...`` /
# ``C:/Users/...``）——后者正是 Windows 宿主机的 cwd 泄漏到 Linux 容器的
# ``-w`` 参数时所呈现的样子。
_HOST_CWD_PREFIXES = ("/Users/", "/home/", "C:\\", "C:/")

_CONTAINER_BACKENDS = frozenset({"docker", "singularity", "modal", "daytona"})


def _is_unusable_container_cwd(cwd: str) -> bool:
    """当 *cwd* 是无法作为容器沙箱内部工作目录的宿主机路径/相对路径时返回 True。

    容器的 cwd 必须是一个在沙箱 *内部* 存在的绝对路径（例如 ``/workspace``
    或 ``/root``）。宿主机路径（``/home/user``、``C:\\Users\\me``）或相对路径
    （``.``、``src/``）对 ``docker run -w`` 来说是无意义的，并会使容器启动失败
    （退出码 125）。
    """
    if not cwd:
        return False
    if any(cwd.startswith(p) for p in _HOST_CWD_PREFIXES):
        return True
    # 相对路径（"."、"src/"）同样不能作为容器工作目录。Windows 驱动器路径在
    # Windows 上是绝对路径，但在 POSIX 宿主机上 os.path.isabs() 返回 False，
    # 所以上面的前缀检查已经把它们捕获了。
    if not os.path.isabs(cwd):
        return True
    return False


def _get_env_config() -> Dict[str, Any]:
    """从环境变量获取终端环境配置。"""
    # 默认镜像包含 Python 和 Node.js，以实现最大兼容性
    default_image = "nikolaik/python-nodejs:python3.11-nodejs20"
    env_type = os.getenv("TERMINAL_ENV", "local")
    
    mount_docker_cwd = os.getenv("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "false").lower() in {"true", "1", "yes"}
    container_backend = env_type in {"docker", "singularity", "modal", "daytona"}
    docker_backend = env_type == "docker"

    # 仅用于 Docker/容器的环境变量即使当前后端是 local/ssh，也可能从 config.yaml
    # 桥接过来。在选择能够使用它们的后端之前，不要解析其 JSON/数值载荷；一个
    # 陈旧或非法的 Docker 值不应让本地 terminal/execute_code 无法使用。
    if container_backend:
        container_cpu = _parse_env_var("TERMINAL_CONTAINER_CPU", "1", float, "number")
        container_memory = _parse_env_var("TERMINAL_CONTAINER_MEMORY", "5120")
        container_disk = _parse_env_var("TERMINAL_CONTAINER_DISK", "51200")
    else:
        container_cpu = 1.0
        container_memory = 5120
        container_disk = 51200

    if docker_backend:
        docker_forward_env = _parse_env_var("TERMINAL_DOCKER_FORWARD_ENV", "[]", json.loads, "valid JSON")
        docker_volumes = _parse_env_var("TERMINAL_DOCKER_VOLUMES", "[]", json.loads, "valid JSON")
        docker_env = _parse_env_var("TERMINAL_DOCKER_ENV", "{}", json.loads, "valid JSON")
        docker_extra_args = _parse_env_var("TERMINAL_DOCKER_EXTRA_ARGS", "[]", json.loads, "valid JSON")
    else:
        docker_forward_env = []
        docker_volumes = []
        docker_env = {}
        docker_extra_args = []

    # 默认 cwd：local 使用宿主机当前目录，ssh 使用远程主目录，其余后端
    # 都从该后端默认的类根目录 cwd 启动。
    if env_type == "local":
        default_cwd = _safe_getcwd()
    elif env_type == "ssh":
        default_cwd = "~"
    else:
        default_cwd = "/root"

    # 读取 TERMINAL_CWD，但对容器后端进行合理性检查。
    # 如果显式启用了 Docker cwd 透传，则将宿主机路径重新映射到 /workspace，
    # 并单独记录原始宿主机路径。否则保持正常的沙箱行为，丢弃宿主机路径。
    cwd = os.getenv("TERMINAL_CWD", default_cwd)
    if cwd:
        cwd = os.path.expanduser(cwd)
    host_cwd = None
    if env_type == "docker" and mount_docker_cwd:
        docker_cwd_source = os.getenv("TERMINAL_CWD") or _safe_getcwd()
        candidate = os.path.abspath(os.path.expanduser(docker_cwd_source))
        if (
            any(candidate.startswith(p) for p in _HOST_CWD_PREFIXES)
            or (os.path.isabs(candidate) and os.path.isdir(candidate) and not candidate.startswith(("/workspace", "/root")))
        ):
            host_cwd = candidate
            cwd = "/workspace"
    elif env_type in _CONTAINER_BACKENDS and cwd:
        # 在容器内部无法使用的宿主机路径和相对路径
        if _is_unusable_container_cwd(cwd) and cwd != default_cwd:
            logger.info("Ignoring TERMINAL_CWD=%r for %s backend "
                        "(host/relative path won't work in sandbox). Using %r instead.",
                        cwd, env_type, default_cwd)
            cwd = default_cwd

    return {
        "env_type": env_type,
        "modal_mode": coerce_modal_mode(os.getenv("TERMINAL_MODAL_MODE", "auto")),
        "docker_image": os.getenv("TERMINAL_DOCKER_IMAGE", default_image),
        "docker_forward_env": docker_forward_env,
        "singularity_image": os.getenv("TERMINAL_SINGULARITY_IMAGE", f"docker://{default_image}"),
        "modal_image": os.getenv("TERMINAL_MODAL_IMAGE", default_image),
        "daytona_image": os.getenv("TERMINAL_DAYTONA_IMAGE", default_image),
        "cwd": cwd,
        "host_cwd": host_cwd,
        "docker_mount_cwd_to_workspace": mount_docker_cwd,
        "timeout": _parse_env_var("TERMINAL_TIMEOUT", "180"),
        "lifetime_seconds": _parse_env_var("TERMINAL_LIFETIME_SECONDS", "300"),
        # SSH 专用配置
        "ssh_host": os.getenv("TERMINAL_SSH_HOST", ""),
        "ssh_user": os.getenv("TERMINAL_SSH_USER", ""),
        "ssh_port": _parse_env_var("TERMINAL_SSH_PORT", "22"),
        "ssh_key": os.getenv("TERMINAL_SSH_KEY", ""),
        # 持久化 shell：SSH 默认使用配置层面的 persistent_shell 设置
        # （非本地后端默认为 true）；local 则始终需要显式开启。
        # 各后端的环境变量在显式设置时会覆盖该默认值。
        "ssh_persistent": os.getenv(
            "TERMINAL_SSH_PERSISTENT",
            os.getenv("TERMINAL_PERSISTENT_SHELL", "true"),
        ).lower() in {"true", "1", "yes"},
        "local_persistent": os.getenv("TERMINAL_LOCAL_PERSISTENT", "false").lower() in {"true", "1", "yes"},
        # 容器资源配置（适用于 docker、singularity、modal、daytona——
        # 对 local/ssh 无效）
        "container_cpu": container_cpu,
        "container_memory": container_memory,     # MB（默认 5GB）
        "container_disk": container_disk,        # MB（默认 50GB）
        "container_persistent": os.getenv("TERMINAL_CONTAINER_PERSISTENT", "true").lower() in {"true", "1", "yes"},
        "docker_volumes": docker_volumes,
        "docker_env": docker_env,
        "docker_run_as_host_user": os.getenv("TERMINAL_DOCKER_RUN_AS_HOST_USER", "false").lower() in {"true", "1", "yes"},
        "docker_extra_args": docker_extra_args,
        # 跨进程容器复用（issue #20561）。文档宣称「跨会话共享一个长生命周期
        # 容器」——该开关通过在启动时探测带标签的容器并接入它，而不是总启动
        # 一个新容器，使这一点成为现实。设置为 ``false`` 可实现严格的进程级
        # 隔离（不复用，退出时删除容器）。
        "docker_persist_across_processes": os.getenv(
            "TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES", "true"
        ).lower() in {"true", "1", "yes"},
        # 启动时回收由崩溃 / 被 SIGKILL 的先前进程遗留的、带 hermes 标签的
        # 容器（这些进程绕过了 atexit）。
        # 保守策略：只回收退出时间超过 2 倍闲置回收窗口且属于当前 profile
        # 的 Exited 容器。Issue #20561。
        "docker_orphan_reaper": os.getenv(
            "TERMINAL_DOCKER_ORPHAN_REAPER", "true"
        ).lower() in {"true", "1", "yes"},
    }


def _get_modal_backend_state(modal_mode: object | None) -> Dict[str, Any]:
    """解析直连与托管 Modal 后端的选择。"""
    return resolve_modal_backend_state(
        modal_mode,
        has_direct=has_direct_modal_credentials(),
        managed_ready=is_managed_tool_gateway_ready("modal"),
    )


def _create_environment(env_type: str, image: str, cwd: str, timeout: int,
                        ssh_config: dict = None, container_config: dict = None,
                        local_config: dict = None,
                        task_id: str = "default",
                        host_cwd: str = None):
    """
    为沙箱化的命令执行创建一个执行环境。

    参数：
        env_type: 取值为 "local"、"docker"、"singularity"、"modal"、
            "daytona"、"ssh" 之一
        image: Docker/Singularity/Modal 镜像名（local/ssh 时忽略）
        cwd: 工作目录
        timeout: 默认命令超时时间
        ssh_config: SSH 连接配置（当 env_type="ssh" 时使用）
        container_config: 容器后端的资源配置（cpu、memory、disk、persistent）
        task_id: 用于环境复用和快照键管理的任务标识符
        host_cwd: 可选的宿主机工作目录，在显式启用时会绑定到 Docker 中

    返回：
        带有 execute() 方法的环境实例
    """
    cc = container_config or {}
    cpu = cc.get("container_cpu", 1)
    memory = cc.get("container_memory", 5120)
    disk = cc.get("container_disk", 51200)
    persistent = cc.get("container_persistent", True)
    volumes = cc.get("docker_volumes", [])
    docker_forward_env = cc.get("docker_forward_env", [])
    docker_env = cc.get("docker_env", {})
    docker_extra_args = cc.get("docker_extra_args", [])

    if env_type == "local":
        return _LocalEnvironment(cwd=cwd, timeout=timeout)
    
    elif env_type == "docker":
        # 一次性孤儿回收器：清理先前 Hermes 进程在 atexit 清理钩子运行之前
        # 因 SIGKILL / OOM / 终端关闭而遗留的带标签容器。限制为每进程一次，
        # 这样并发的 _create_environment 调用（并行子 agent、RL 基准测试）
        # 不会把回收器运行 N 次。
        # 通过 ``terminal.docker_orphan_reaper: false`` 禁用（issue #20561）。
        _maybe_reap_docker_orphans(cc)
        return _DockerEnvironment(
            image=image, cwd=cwd, timeout=timeout,
            cpu=cpu, memory=memory, disk=disk,
            persistent_filesystem=persistent, task_id=task_id,
            volumes=volumes,
            host_cwd=host_cwd,
            auto_mount_cwd=cc.get("docker_mount_cwd_to_workspace", False),
            forward_env=docker_forward_env,
            env=docker_env,
            run_as_host_user=cc.get("docker_run_as_host_user", False),
            extra_args=docker_extra_args,
            persist_across_processes=cc.get("docker_persist_across_processes", True),
        )
    
    elif env_type == "singularity":
        return _SingularityEnvironment(
            image=image, cwd=cwd, timeout=timeout,
            cpu=cpu, memory=memory, disk=disk,
            persistent_filesystem=persistent, task_id=task_id,
        )
    
    elif env_type == "modal":
        sandbox_kwargs = {}
        if cpu > 0:
            sandbox_kwargs["cpu"] = cpu
        if memory > 0:
            sandbox_kwargs["memory"] = memory
        if disk > 0:
            try:
                import inspect, modal
                if "ephemeral_disk" in inspect.signature(modal.Sandbox.create).parameters:
                    sandbox_kwargs["ephemeral_disk"] = disk
            except Exception:
                pass

        modal_state = _get_modal_backend_state(cc.get("modal_mode"))

        if modal_state["selected_backend"] == "managed":
            return _ManagedModalEnvironment(
                image=image, cwd=cwd, timeout=timeout,
                modal_sandbox_kwargs=sandbox_kwargs,
                persistent_filesystem=persistent, task_id=task_id,
            )

        if modal_state["selected_backend"] != "direct":
            if modal_state["managed_mode_blocked"]:
                raise ValueError(
                    "Modal backend is configured for managed mode, but "
                    "Nous Tool Gateway access is not currently available and no direct "
                    "Modal credentials/config were found. "
                    + nous_tool_gateway_unavailable_message(
                        "managed Modal execution",
                    )
                    + " Choose TERMINAL_MODAL_MODE=direct/auto to use direct Modal credentials."
                )
            if modal_state["mode"] == "managed":
                raise ValueError(
                    "Modal backend is configured for managed mode, but the managed tool gateway is unavailable. "
                    + nous_tool_gateway_unavailable_message(
                        "managed Modal execution",
                    )
                )
            if modal_state["mode"] == "direct":
                raise ValueError(
                    "Modal backend is configured for direct mode, but no direct Modal credentials/config were found."
                )
            message = "Modal backend selected but no direct Modal credentials/config was found."
            if managed_nous_tools_enabled():
                message = (
                    "Modal backend selected but no direct Modal credentials/config or managed tool gateway was found."
                )
            raise ValueError(message)

        return _ModalEnvironment(
            image=image, cwd=cwd, timeout=timeout,
            modal_sandbox_kwargs=sandbox_kwargs,
            persistent_filesystem=persistent, task_id=task_id,
        )
    
    elif env_type == "daytona":
        # 懒加载导入，这样只有在该后端被选中时才需要 daytona SDK。
        from tools.environments.daytona import DaytonaEnvironment as _DaytonaEnvironment
        return _DaytonaEnvironment(
            image=image, cwd=cwd, timeout=timeout,
            cpu=int(cpu), memory=memory, disk=disk,
            persistent_filesystem=persistent, task_id=task_id,
        )

    elif env_type == "ssh":
        if not ssh_config or not ssh_config.get("host") or not ssh_config.get("user"):
            raise ValueError("SSH environment requires ssh_host and ssh_user to be configured")
        return _SSHEnvironment(
            host=ssh_config["host"],
            user=ssh_config["user"],
            port=ssh_config.get("port", 22),
            key_path=ssh_config.get("key", ""),
            cwd=cwd,
            timeout=timeout,
        )

    else:
        raise ValueError(
            f"Unknown environment type: {env_type}. Use 'local', 'docker', "
            f"'singularity', 'modal', 'daytona', or 'ssh'"
        )


def _cleanup_inactive_envs(lifetime_seconds: int = 300):
    """清理闲置时间超过 lifetime_seconds 的环境。"""
    current_time = time.time()

    # 检查进程注册表——对含有活动后台进程的沙箱跳过清理（它们的
    # _last_activity 会被刷新以保持存活）。
    try:
        from tools.process_registry import process_registry
        for task_id in list(_last_activity.keys()):
            if process_registry.has_active_processes(task_id):
                _last_activity[task_id] = current_time  # 保持沙箱存活
    except ImportError:
        pass

    # 第一阶段：在持锁期间收集陈旧条目并将它们从跟踪字典中移除。不要在锁内
    # 调用 env.cleanup()——Modal 和 Docker 的拆除可能阻塞 10-15 秒，这会
    # 拖慢所有等待 _env_lock 的并发终端/文件工具调用。
    envs_to_stop = []  # 由 (task_id, env) 组成的列表

    with _env_lock:
        for task_id, last_time in list(_last_activity.items()):
            if current_time - last_time > lifetime_seconds:
                env = _active_environments.pop(task_id, None)
                _last_activity.pop(task_id, None)
                if env is not None:
                    envs_to_stop.append((task_id, env))

        # 同时清除已清理任务对应的按任务创建锁
        with _creation_locks_lock:
            for task_id, _ in envs_to_stop:
                _creation_locks.pop(task_id, None)

    # 第二阶段：在锁外停止实际的沙箱，这样在 Modal/Docker 沙箱关闭期间，
    # 其他工具调用不会被阻塞。
    for task_id, env in envs_to_stop:
        # 使陈旧的 file_ops 缓存条目失效（Bug 修复：防止 ShellFileOperations
        # 引用一个已死的沙箱）
        try:
            from tools.file_tools import clear_file_ops_cache
            clear_file_ops_cache(task_id)
        except ImportError:
            pass

        try:
            if hasattr(env, 'cleanup'):
                env.cleanup()
            elif hasattr(env, 'stop'):
                env.stop()
            elif hasattr(env, 'terminate'):
                env.terminate()

            logger.info("Cleaned up inactive environment for task: %s", task_id)

        except Exception as e:
            error_str = str(e)
            if "404" in error_str or "not found" in error_str.lower():
                logger.info("Environment for task %s already cleaned up", task_id)
            else:
                logger.warning("Error cleaning up environment for task %s: %s", task_id, e)


def _cleanup_thread_worker():
    """后台线程工作函数，周期性清理闲置环境。"""
    while _cleanup_running:
        try:
            config = _get_env_config()
            _cleanup_inactive_envs(config["lifetime_seconds"])
        except Exception as e:
            logger.warning("Error in cleanup thread: %s", e, exc_info=True)

        for _ in range(60):
            if not _cleanup_running:
                break
            time.sleep(1)


def _start_cleanup_thread():
    """如果后台清理线程尚未运行，则启动它。"""
    global _cleanup_thread, _cleanup_running

    with _env_lock:
        if _cleanup_thread is None or not _cleanup_thread.is_alive():
            _cleanup_running = True
            _cleanup_thread = threading.Thread(target=_cleanup_thread_worker, daemon=True)
            _cleanup_thread.start()


def _stop_cleanup_thread():
    """停止后台清理线程。"""
    global _cleanup_running
    _cleanup_running = False
    if _cleanup_thread is not None:
        try:
            _cleanup_thread.join(timeout=5)
        except (SystemExit, KeyboardInterrupt):
            pass


def get_active_env(task_id: str):
    """返回 *task_id* 对应的活动 BaseEnvironment，若没有则返回 None。"""
    lookup = _resolve_container_task_id(task_id)
    with _env_lock:
        return _active_environments.get(lookup) or _active_environments.get(task_id)


def is_persistent_env(task_id: str) -> bool:
    """当 task_id 对应的活动环境配置为跨轮次持久化
    （``persistent_filesystem=True``）时返回 True。

    agent 循环用它来跳过那些本就以在轮次间存活为目的的后端（如带
    ``container_persistent`` 的 docker、daytona、modal 等）的每轮拆除。
    非持久化后端（例如 Morph）仍会在每轮结束时被拆除以防止泄漏。闲置
    回收器（``_cleanup_inactive_envs``）会在持久化环境超过
    ``terminal.lifetime_seconds`` 后负责处理它们。
    """
    env = get_active_env(task_id)
    if env is None:
        return False
    return bool(getattr(env, "_persistent", False))




def cleanup_all_environments():
    """清理所有活动环境。请谨慎使用。"""
    task_ids = list(_active_environments.keys())
    cleaned = 0

    for task_id in task_ids:
        try:
            cleanup_vm(task_id)
            cleaned += 1
        except Exception as e:
            logger.error("Error cleaning %s: %s", task_id, e, exc_info=True)

    # 同时清理任何孤立的目录
    scratch_dir = _get_scratch_dir()
    import glob
    for path in glob.glob(str(scratch_dir / "hermes-*")):
        try:
            shutil.rmtree(path, ignore_errors=True)
            logger.info("Removed orphaned: %s", path)
        except OSError as e:
            logger.debug("Failed to remove orphaned path %s: %s", path, e)
    
    if cleaned > 0:
        logger.info("Cleaned %d environments", cleaned)
    return cleaned


def cleanup_vm(task_id: str, *, force_remove: bool = False):
    """按 task_id 手动清理某个具体环境。

    *force_remove*（默认 False）会转发给接受它的后端——目前只有
    ``DockerEnvironment``。默认为 False 符合会话生命周期语义：本函数会被
    ``AIAgent.close()``（TUI 会话关闭、gateway 会话拆除）以及非持久化环境的
    每轮清理分支调用，这两者都应尊重用户的持久化模式偏好。在此处停止容器
    会破坏「跨会话共享一个长生命周期容器」的契约——这正是 Ben 报告的 bug：
    每次关闭 TUI 会话时容器都会被杀掉。

    若要进行真正的用户主动拆除（例如尚未接入的 ``/reset`` 类流程，或未来的
    「销毁我的沙箱」命令），请传入 ``force_remove=True``。

    闲置回收器会直接通过 ``env.cleanup()`` 处理环境（而非经由本函数），
    因此持久化模式的闲置环境同样会被无操作处理——只有下次启动时的孤儿
    回收器才会回收它们。
    """
    # 在持锁期间从跟踪字典中移除，但把实际的（可能很慢的）env.cleanup()
    # 调用推迟到锁外执行，以免阻塞其他工具调用。
    env = None
    with _env_lock:
        env = _active_environments.pop(task_id, None)
        _last_activity.pop(task_id, None)

    # 清理按任务划分的创建锁
    with _creation_locks_lock:
        _creation_locks.pop(task_id, None)

    # 使陈旧的 file_ops 缓存条目失效
    try:
        from tools.file_tools import clear_file_ops_cache
        clear_file_ops_cache(task_id)
    except ImportError:
        pass

    if env is None:
        return

    try:
        if hasattr(env, 'cleanup'):
            # 只有当环境的 cleanup() 接受 force_remove 时才传入
            # （issue #20561 之后的 DockerEnvironment；其他后端不接受）。
            import inspect
            sig = inspect.signature(env.cleanup)
            if "force_remove" in sig.parameters:
                env.cleanup(force_remove=force_remove)
            else:
                env.cleanup()
        elif hasattr(env, 'stop'):
            env.stop()
        elif hasattr(env, 'terminate'):
            env.terminate()

        logger.info("Manually cleaned up environment for task: %s", task_id)

    except Exception as e:
        error_str = str(e)
        if "404" in error_str or "not found" in error_str.lower():
            logger.info("Environment for task %s already cleaned up", task_id)
        else:
            logger.warning("Error cleaning up environment for task %s: %s", task_id, e)


def _atexit_cleanup():
    """停止清理线程，并在退出时关闭所有剩余的沙箱。"""
    _stop_cleanup_thread()
    if _active_environments:
        count = len(_active_environments)
        logger.info("Shutting down %d remaining sandbox(es)...", count)
        # 在 cleanup_all_environments 清空字典之前，先对环境对象做一次快照；
        # 我们需要在注册表被清空后，用它们来等待 docker 清理线程结束。
        envs_to_wait = list(_active_environments.values())
        cleanup_all_environments()
        # 短暂阻塞，使 docker stop/rm 在解释器退出前真正完成。Issue #20561——
        # 如果没有这个 join，守护清理线程会在 `docker stop` 执行中途被拆除，
        # 导致 Exited 容器在宿主机上堆积。
        for env in envs_to_wait:
            wait_fn = getattr(env, "wait_for_cleanup", None)
            if wait_fn is None:
                continue
            try:
                wait_fn(timeout=15.0)
            except Exception as e:  # 绝不因为某个后端异常而阻塞关闭
                logger.debug("wait_for_cleanup raised on exit: %s", e)

atexit.register(_atexit_cleanup)


# =============================================================================
# 常见 CLI 工具的退出码上下文
# =============================================================================
# 许多 Unix 命令出于信息性目的使用非零退出码，而不是表示失败。模型看到
# `grep` 返回 raw exit_code=1 时，会浪费一个轮次去调查其实只是「没有匹配」
# 的情况。这个查找表会补充一条人类可读的说明，让 agent 能继续往下走。

def _interpret_exit_code(command: str, exit_code: int) -> str | None:
    """当某个非零退出码并非错误时，返回一条人类可读的说明。

    当退出码为 0 或确实表示错误时返回 None。该说明会被追加到工具结果中，
    以免模型浪费轮次去调查预期内的退出码。
    """
    if exit_code == 0:
        return None

    # 提取管道/链式命令中的最后一条命令——它决定了退出码。可处理
    # `cmd1 && cmd2`、`cmd1 | cmd2`、`cmd1; cmd2`。
    # 刻意保持简单：按 shell 运算符切分并取最后一段。
    segments = re.split(r'\s*(?:\|\||&&|[|;])\s*', command)
    last_segment = (segments[-1] if segments else command).strip()

    # 获取基础命令名（第一个词），并剥离形如 VAR=val cmd ... 的环境变量赋值
    words = last_segment.split()
    base_cmd = ""
    for w in words:
        if "=" in w and not w.startswith("-"):
            continue  # 跳过 VAR=val
        base_cmd = w.split("/")[-1]  # 处理 /usr/bin/grep -> grep
        break

    if not base_cmd:
        return None

    # 各命令特定的语义
    semantics: dict[str, dict[int, str]] = {
        # grep/rg/ag/ack：1=未找到匹配（正常），2+=真正的错误
        "grep":  {1: "No matches found (not an error)"},
        "egrep": {1: "No matches found (not an error)"},
        "fgrep": {1: "No matches found (not an error)"},
        "rg":    {1: "No matches found (not an error)"},
        "ag":    {1: "No matches found (not an error)"},
        "ack":   {1: "No matches found (not an error)"},
        # diff：1=文件存在差异（预期内），2+=真正的错误
        "diff":  {1: "Files differ (expected, not an error)"},
        "colordiff": {1: "Files differ (expected, not an error)"},
        # find：1=部分目录不可访问，但结果可能仍然有效
        "find":  {1: "Some directories were inaccessible (partial results may still be valid)"},
        # test/[：1=条件为假（预期内）
        "test":  {1: "Condition evaluated to false (expected, not an error)"},
        "[":     {1: "Condition evaluated to false (expected, not an error)"},
        # curl：常见的非错误退出码
        "curl":  {
            6: "Could not resolve host",
            7: "Failed to connect to host",
            22: "HTTP response code indicated error (e.g. 404, 500)",
            28: "Operation timed out",
        },
        # git：1 视上下文而定，但通常正常（例如 git diff 在文件有改动时返回 1）
        "git":   {1: "Non-zero exit (often normal — e.g. 'git diff' returns 1 when files differ)"},
    }

    cmd_semantics = semantics.get(base_cmd)
    if cmd_semantics and exit_code in cmd_semantics:
        return cmd_semantics[exit_code]

    return None


def _command_requires_pipe_stdin(command: str) -> bool:
    """当 PTY 模式会破坏依赖 stdin 的命令时返回 True。

    某些 CLI 在 stdin 是 TTY 时会改变行为。具体而言，`gh auth login --with-token`
    期望 token 通过管道 stdin 传入并等待 EOF；当我们在 PTY 下启动它时，
    `process.submit()` 只会发送一个换行符，于是该命令看起来会永远挂起，且
    没有任何可见进展。
    """
    normalized = " ".join(command.lower().split())
    return (
        normalized.startswith("gh auth login")
        and "--with-token" in normalized
    )


_SHELL_LEVEL_BACKGROUND_RE = re.compile(
    r"(?:^|[;&|]\s*|&&\s*|\|\|\s*|\$\(\s*)(?:nohup|disown|setsid)\b", re.IGNORECASE | re.MULTILINE
)
_INLINE_BACKGROUND_AMP_RE = re.compile(r"\s&\s")
_TRAILING_BACKGROUND_AMP_RE = re.compile(r"\s&\s*(?:#.*)?$")


def _strip_quotes(command: str) -> str:
    """去除单引号和双引号包裹的内容，使正则检查不会在字符串内部命中。

    这样可以避免当 'nohup' 或 'setsid' 等关键词出现在提交消息、Python -c 代码、
    echo 参数或 PR 正文中时产生误报。同时也会去除反引号包裹的内容和 heredoc
    风格的内联文本。
    """
    # 去除单引号字符串（shell 中单引号内没有转义）
    result = re.sub(r"'[^']*'", "''", command)
    # 去除双引号字符串（处理转义引号）
    result = re.sub(r'"(?:[^"\\]|\\.)*"', '""', result)
    # 去除反引号字符串
    result = re.sub(r"`[^`]*`", "``", result)
    return result


_LONG_LIVED_FOREGROUND_PATTERNS = (
    re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:dev|start|serve|watch)\b", re.IGNORECASE),
    re.compile(r"\bdocker\s+compose\s+up\b", re.IGNORECASE),
    re.compile(r"\bnext\s+dev\b", re.IGNORECASE),
    re.compile(r"\bvite(?:\s|$)", re.IGNORECASE),
    re.compile(r"\bnodemon\b", re.IGNORECASE),
    re.compile(r"\buvicorn\b", re.IGNORECASE),
    re.compile(r"\bgunicorn\b", re.IGNORECASE),
    re.compile(r"\bpython(?:3)?\s+-m\s+http\.server\b", re.IGNORECASE),
)


def _looks_like_help_or_version_command(command: str) -> bool:
    """对于纯信息查询类调用（不应被阻止）返回 True。"""
    normalized = " ".join(command.lower().split())
    return (
        " --help" in normalized
        or normalized.endswith(" -h")
        or " --version" in normalized
        or normalized.endswith(" -v")
    )


def _foreground_background_guidance(command: str) -> str | None:
    """当某个前台命令看起来会长时间运行时，建议改用后台模式。

    用于避免那种启动了服务/监听进程、却在后续检查或测试命令运行前就卡住
    的工作流。
    """
    if _looks_like_help_or_version_command(command):
        return None

    # 去除引号包裹的内容，使字符串/参数内的关键词不会触发误报
    # （例如 git commit -m "... setsid ..."、python3 -c "os.setsid"）。
    unquoted = _strip_quotes(command)

    if _SHELL_LEVEL_BACKGROUND_RE.search(unquoted):
        return (
            "Foreground command uses shell-level background wrappers (nohup/disown/setsid). "
            "Use terminal(background=true) so Hermes can track the process, then run "
            "readiness checks and tests in separate commands."
        )

    if _INLINE_BACKGROUND_AMP_RE.search(unquoted) or _TRAILING_BACKGROUND_AMP_RE.search(unquoted):
        return (
            "Foreground command uses '&' backgrounding. Use terminal(background=true) for long-lived "
            "processes, then run health checks and tests in follow-up terminal calls."
        )

    for pattern in _LONG_LIVED_FOREGROUND_PATTERNS:
        if pattern.search(unquoted):
            return (
                "This foreground command appears to start a long-lived server/watch process. "
                "Run it with background=true, verify readiness (health endpoint/log signal), "
                "then execute tests in a separate command."
            )

    return None


def _resolve_notification_flag_conflict(
    *,
    notify_on_complete: bool,
    watch_patterns,
    background: bool,
) -> tuple:
    """当 notify_on_complete 和 watch_patterns 同时设置时，决定如何处理。

    这两个标志组合在一起会产生重复、延迟的通知——每个 watch-pattern 匹配
    都通知一次，进程退出时还通知一次，且是异步投递，可能在进程结束很久之后
    还在打扰用户。当两者同时设置时，我们会丢弃 watch_patterns，改用
    notify_on_complete（更有用的「完成时告诉我」信号），并返回一条人类可读
    的说明。

    返回：
        (watch_patterns_to_use, conflict_note)。无冲突时 conflict_note 为 ""。
    """
    if background and notify_on_complete and watch_patterns:
        note = (
            "watch_patterns ignored because notify_on_complete=True; "
            "these two flags produce duplicate notifications when combined"
        )
        return None, note
    return watch_patterns, ""


def _resolve_command_cwd(
    *,
    workdir: Optional[str],
    env: Any,
    default_cwd: str,
) -> str:
    """返回某条命令使用的 cwd，优先使用当前会话的 cwd。

    ``terminal_tool`` 过去会在每次调用时都重新发送初始化时/配置中的 cwd。
    这会破坏会话本地的 ``cd`` 状态：环境会在 ``env.cwd`` 中跟踪新目录，但
    前台/后台调用却一直通过 ``env.execute(..., cwd=...)`` 把旧的 cwd 强行
    传回去。显式的 ``workdir=`` 仍必须覆盖一切。
    """
    if workdir:
        return workdir

    live_cwd = getattr(env, "cwd", None)
    if isinstance(live_cwd, str) and live_cwd.strip():
        return live_cwd

    return default_cwd


def terminal_tool(
    command: str,
    background: bool = False,
    timeout: Optional[int] = None,
    task_id: Optional[str] = None,
    session_id: Optional[str] = None,
    force: bool = False,
    workdir: Optional[str] = None,
    pty: bool = False,
    notify_on_complete: bool = False,
    watch_patterns: Optional[List[str]] = None,
) -> str:
    """
    在已配置的终端环境中执行一条命令。

    参数：
        command: 要执行的命令
        background: 是否在后台运行（默认：False）
        timeout: 命令超时时间，单位秒（默认：取自配置）
        task_id: 用于环境隔离的唯一标识符（可选）
        session_id: 用于持久化可观测性的会话/对话标识符
        force: 为 True 时跳过危险命令检查（在用户确认后使用）
        workdir: 本条命令的工作目录（可选，未设置时使用会话 cwd）
        pty: 为 True 时，为交互式 CLI 工具使用伪终端（仅限 local 后端）
        notify_on_complete: 为 True 且 background=True 时，进程退出时会精确通知你一次。对于几乎所有长任务这都是正确选择。与 watch_patterns 互斥。
        watch_patterns: 要在后台输出中监视的字符串列表。硬性速率限制：每个进程每 15 秒最多 1 次通知。连续 3 次命中限制窗口后，watch_patterns 会被禁用，会话会自动升级为 notify_on_complete。仅用于长时间运行且不会自行退出的进程上罕见的、一次性的进程中途信号（服务器就绪、迁移完成标记）。绝不用于循环/批处理任务——那里的错误模式会很快命中限制并被禁用。与 notify_on_complete 互斥——二选一，不要同时设置。

    返回：
        str: 包含 output、exit_code 和 error 字段的 JSON 字符串

    示例：
        # 执行一条简单命令
        >>> result = terminal_tool(command="ls -la /tmp")

        # 运行一个后台任务
        >>> result = terminal_tool(command="python server.py", background=True)

        # 使用自定义超时
        >>> result = terminal_tool(command="long_task.sh", timeout=300)

        # 用户确认后强制运行
        # 注意：force 参数仅供内部使用，不暴露给模型 API
    """
    try:
        if not isinstance(command, str):
            logger.warning(
                "Rejected invalid terminal command value: %s",
                type(command).__name__,
            )
            return json.dumps({
                "output": "",
                "exit_code": -1,
                "error": f"Invalid command: expected string, got {type(command).__name__}",
                "status": "error",
            }, ensure_ascii=False)

        # 获取配置
        config = _get_env_config()
        env_type = config["env_type"]

        # 使用 task_id 进行环境隔离。默认情况下，所有子 agent 的 task_id 都会
        # 折叠回 "default"，这样顶层 agent 和每个 delegate_task 子任务共享一个
        # 容器；只有注册了环境覆盖（RL 基准测试）的 task_id 才会获得隔离的沙箱。
        effective_task_id = _resolve_container_task_id(task_id)

        # 检查按任务划分的覆盖配置（由 TerminalBench2Env 等环境设置），
        # 之后再回退到全局环境变量配置。``resolve_task_overrides`` 会先读取
        # 原始 task id，再读取折叠后的容器 id，因此仅含 CWD 的覆盖（会把
        # ``effective_task_id`` 折叠为 ``"default"``）仍能在其发起会话 id 下
        # 被找到，而以隔离键划分的 RL/基准测试覆盖则一如既往地解析。
        overrides = resolve_task_overrides(task_id)

        # 根据环境类型选择镜像，并支持按任务覆盖
        if env_type == "docker":
            image = overrides.get("docker_image") or config["docker_image"]
        elif env_type == "singularity":
            image = overrides.get("singularity_image") or config["singularity_image"]
        elif env_type == "modal":
            image = overrides.get("modal_image") or config["modal_image"]
        elif env_type == "daytona":
            image = overrides.get("daytona_image") or config["daytona_image"]
        else:
            image = ""

        cwd = overrides.get("cwd") or config["cwd"]
        # 按任务的 cwd 覆盖（由 gateway/TUI 用于工作区跟踪，或由 RL/基准测试
        # 环境注册）优先级高于 config["cwd"]——但 config["cwd"] 在
        # _get_env_config() 中已针对容器后端做了清理，而覆盖值是原始的。在容器
        # 后端上，一个原始的宿主机路径（例如 Windows 桌面会话的
        # C:\Users\<user>，或 POSIX 的 /home/<user>）会传到
        # `docker run -w <host-path>`，导致容器启动失败（退出码 125）。对
        # *解析后* 的 cwd 重新施加同样的宿主机/相对路径守卫，使覆盖值无法绕过它。
        # 合法的容器内覆盖路径（将 cwd 设为 /workspace、/root 等的 RL/基准测试
        # 沙箱）是绝对的非宿主机路径，会原样通过。
        if env_type in _CONTAINER_BACKENDS and _is_unusable_container_cwd(cwd):
            if cwd != config["cwd"]:
                logger.info(
                    "Ignoring host/relative cwd override %r for %s backend "
                    "(won't exist in sandbox). Using %r instead.",
                    cwd, env_type, config["cwd"],
                )
            cwd = config["cwd"]
        default_timeout = config["timeout"]
        effective_timeout = timeout or default_timeout

        # 拒绝模型为前台命令显式请求超过 FOREGROUND_MAX_TIMEOUT 的超时——
        # 引导它改用后台模式。
        if not background and timeout and timeout > FOREGROUND_MAX_TIMEOUT:
            return json.dumps({
                "error": (
                    f"Foreground timeout {timeout}s exceeds the maximum of "
                    f"{FOREGROUND_MAX_TIMEOUT}s. Use background=true with "
                    f"notify_on_complete=true for long-running commands."
                ),
            }, ensure_ascii=False)

        # 护栏：长时间运行的服务器/监听命令应作为受管的后台会话运行，
        # 而不是前台 shell 取巧写法。
        if not background:
            guidance = _foreground_background_guidance(command)
            if guidance:
                return json.dumps({
                    "output": "",
                    "exit_code": -1,
                    "error": guidance,
                    "status": "error",
                }, ensure_ascii=False)

        # 启动清理线程
        _start_cleanup_thread()

        # 获取或创建环境。
        # 使用按任务划分的创建锁，这样同一 task_id 的并发工具调用会等待第一个
        # 调用完成沙箱创建，而不是各自创建自己的（浪费 Modal 资源）。
        with _env_lock:
            # 优先使用折叠后的容器 id，但回退到以原始 task_id 缓存的环境。
            # 带有仅 CWD 覆盖的按会话界面（ACP/gateway/dashboard）会为共享容器
            # 折叠为 "default"，但环境可能已经以发起 task_id 缓存了；此时应优先
            # 复用它，而不是重复创建。
            _existing_key = (
                effective_task_id if effective_task_id in _active_environments
                else (task_id if task_id and task_id in _active_environments else None)
            )
            if _existing_key is not None:
                _last_activity[_existing_key] = time.time()
                env = _active_environments[_existing_key]
                needs_creation = False
            else:
                needs_creation = True

        if needs_creation:
            # 按任务的锁：只有一个线程创建沙箱，其余线程等待
            with _creation_locks_lock:
                if effective_task_id not in _creation_locks:
                    _creation_locks[effective_task_id] = threading.Lock()
                task_lock = _creation_locks[effective_task_id]

            with task_lock:
                # 获取按任务的锁后再次检查
                with _env_lock:
                    _existing_key = (
                        effective_task_id if effective_task_id in _active_environments
                        else (task_id if task_id and task_id in _active_environments else None)
                    )
                    if _existing_key is not None:
                        _last_activity[_existing_key] = time.time()
                        env = _active_environments[_existing_key]
                        needs_creation = False

                if needs_creation:
                    if env_type == "singularity":
                        _check_disk_usage_warning()
                    logger.info("Creating new %s environment for task %s...", env_type, effective_task_id[:8])
                    try:
                        ssh_config = None
                        if env_type == "ssh":
                            ssh_config = {
                                "host": config.get("ssh_host", ""),
                                "user": config.get("ssh_user", ""),
                                "port": config.get("ssh_port", 22),
                                "key": config.get("ssh_key", ""),
                                "persistent": config.get("ssh_persistent", False),
                            }

                        container_config = None
                        if env_type in {"docker", "singularity", "modal", "daytona"}:
                            container_config = {
                                "container_cpu": config.get("container_cpu", 1),
                                "container_memory": config.get("container_memory", 5120),
                                "container_disk": config.get("container_disk", 51200),
                                "container_persistent": config.get("container_persistent", True),
                                "modal_mode": config.get("modal_mode", "auto"),
                                "docker_volumes": config.get("docker_volumes", []),
                                "docker_mount_cwd_to_workspace": config.get("docker_mount_cwd_to_workspace", False),
                                "docker_forward_env": config.get("docker_forward_env", []),
                                "docker_env": config.get("docker_env", {}),
                                "docker_run_as_host_user": config.get("docker_run_as_host_user", False),
                                "docker_extra_args": config.get("docker_extra_args", []),
                                "docker_persist_across_processes": config.get("docker_persist_across_processes", True),
                                "docker_orphan_reaper": config.get("docker_orphan_reaper", True),
                            }

                        local_config = None
                        if env_type == "local":
                            local_config = {
                                "persistent": config.get("local_persistent", False),
                            }

                        new_env = _create_environment(
                            env_type=env_type,
                            image=image,
                            cwd=cwd,
                            timeout=effective_timeout,
                            ssh_config=ssh_config,
                            container_config=container_config,
                            local_config=local_config,
                            task_id=effective_task_id,
                            host_cwd=config.get("host_cwd"),
                        )
                    except ImportError as e:
                        return json.dumps({
                            "output": "",
                            "exit_code": -1,
                            "error": f"Terminal tool disabled: environment creation failed ({e})",
                            "status": "disabled"
                        }, ensure_ascii=False)

                    with _env_lock:
                        _active_environments[effective_task_id] = new_env
                        _last_activity[effective_task_id] = time.time()
                        env = new_env
                    logger.info("%s environment ready for task %s", env_type, effective_task_id[:8])

        # 硬性阻止：gateway 生命周期命令（systemctl/launchctl/hermes
        # restart|stop 且目标是 hermes-gateway）绝不能在 gateway 进程自身内部
        # 运行。重启会向 gateway 发送 SIGTERM，从而在当前子进程完成之前就把它
        # 杀掉——服务可能永远无法重启。这与 hermes_cli/gateway.py 中的
        # `hermes gateway restart` 守卫以及 hermes_cli/cron.py 中的 cron 路径
        # 守卫相对应，但这里是无条件应用（force=True 也无法绕过）。
        if os.environ.get("_HERMES_GATEWAY") == "1":
            from hermes_cli.cron import _contains_gateway_lifecycle_command
            if _contains_gateway_lifecycle_command(command):
                return json.dumps({
                    "output": "",
                    "exit_code": 1,
                    "error": (
                        "Blocked: cannot restart or stop the gateway from inside the "
                        "gateway process. The gateway would kill this command before "
                        "it could complete (SIGTERM propagates to child processes). "
                        "Run `hermes gateway restart` from a separate shell outside "
                        "the running gateway."
                    ),
                    "status": "error",
                }, ensure_ascii=False)

        # 执行前安全检查（tirith + 危险命令检测）
        # 当 force=True 时跳过检查（用户已确认要运行它）
        approval_note = None
        if not force:
            approval = _check_all_guards(command, env_type)
            if not approval["approved"]:
                # 检查这是否是 approval_required（gateway 询问模式）
                if approval.get("status") == "pending_approval":
                    return json.dumps({
                        "output": "",
                        "exit_code": -1,
                        "error": "",
                        "status": "pending_approval",
                        "approval_pending": True,
                        "command": approval.get("command", command),
                        "description": approval.get("description", "command flagged"),
                        "pattern_key": approval.get("pattern_key", ""),
                    }, ensure_ascii=False)
                # 命令被阻止
                desc = approval.get("description", "command flagged")
                fallback_msg = (
                    f"Command denied: {desc}. "
                    "Use the approval prompt to allow it, or rephrase the command."
                )
                return json.dumps({
                    "output": "",
                    "exit_code": -1,
                    "error": approval.get("message", fallback_msg),
                    "status": "blocked"
                }, ensure_ascii=False)
            # 跟踪审批是否由用户显式批准
            if approval.get("user_approved"):
                desc = approval.get("description", "flagged as dangerous")
                approval_note = f"Command required approval ({desc}) and was approved by the user."
            elif approval.get("smart_approved"):
                desc = approval.get("description", "flagged as dangerous")
                approval_note = f"Command was flagged ({desc}) and auto-approved by smart approval."

        # 对 workdir 进行 shell 注入校验
        if workdir:
            workdir_error = _validate_workdir(workdir)
            if workdir_error:
                logger.warning("Blocked dangerous workdir: %s (command: %s)",
                               workdir[:200], _safe_command_preview(command))
                return json.dumps({
                    "output": "",
                    "exit_code": -1,
                    "error": workdir_error,
                    "status": "blocked"
                }, ensure_ascii=False)

        # 准备命令以供执行
        pty_disabled_reason = None
        effective_pty = pty
        if pty and _command_requires_pipe_stdin(command):
            effective_pty = False
            pty_disabled_reason = (
                "PTY disabled for this command because it expects piped stdin/EOF "
                "(for example gh auth login --with-token). For local background "
                "processes, call process(action='close') after writing so it receives "
                "EOF."
            )

        if background:
            # 通过进程注册表派生一个受跟踪的后台进程。
            # 对于本地后端：使用 subprocess.Popen 并带输出缓冲。
            # 对于非本地后端：通过 env.execute() 在沙箱内运行。
            from tools.approval import get_current_session_key
            from tools.process_registry import process_registry

            session_key = get_current_session_key(default="")
            effective_cwd = _resolve_command_cwd(
                workdir=workdir,
                env=env,
                default_cwd=cwd,
            )
            try:
                if env_type == "local":
                    proc_session = process_registry.spawn_local(
                        command=command,
                        cwd=effective_cwd,
                        task_id=effective_task_id,
                        session_key=session_key,
                        env_vars=env.env if hasattr(env, 'env') else None,
                        use_pty=effective_pty,
                    )
                else:
                    proc_session = process_registry.spawn_via_env(
                        env=env,
                        command=command,
                        cwd=effective_cwd,
                        task_id=effective_task_id,
                        session_key=session_key,
                    )

                result_data = {
                    "output": "Background process started",
                    "session_id": proc_session.id,
                    "pid": proc_session.pid,
                    "exit_code": 0,
                    "error": None,
                }
                if approval_note:
                    result_data["approval"] = approval_note
                if pty_disabled_reason:
                    result_data["pty_note"] = pty_disabled_reason

                # 提示：background=True 但没有 notify_on_complete=True 或
                # watch_patterns，意味着这是一个静默进程。除非 agent 显式调用
                # process(action="poll"/"wait")，否则没有任何办法知道它已结束。
                # 这种情况只对真正长时间运行且永不退出的进程（服务器、监听器）才是
                # 正确的。对于每一个有界的任务（测试、构建、CI 轮询、部署、批处理），
                # agent 几乎肯定是想要通知却忘了设置该标志。2026 年 5 月 PR #31231
                # 事故：后台 CI 轮询器运行正常、绿色退出，但 agent 一直没察觉——
                # 用户不得不
                # 亲自把结果展示出来。这里的低成本提示对服务器场景来说只需多一次
                # 读取（误报），却能避免有界任务场景下的静默盲区（漏报）。
                if background and not notify_on_complete and not watch_patterns:
                    result_data["hint"] = (
                        "background=true without notify_on_complete=true means "
                        "this process runs SILENTLY — you will not be told when "
                        "it exits. If this is a bounded task (test suite, build, "
                        "CI poller, deploy, anything with a defined end), you "
                        "almost certainly wanted notify_on_complete=true so the "
                        "system pings you on exit. Re-launch with "
                        "notify_on_complete=true, or call process(action='poll') "
                        "/ process(action='wait') yourself to learn the outcome. "
                        "Only ignore this hint for genuine long-lived processes "
                        "that never exit (servers, watchers, daemons)."
                    )

                # 提示：用 `gh pr view` 的 `--json statusCheckRollup` 或
                # 把 `gh pr checks` 通过管道交给 `jq` 拼出来的自制 CI 监视器，
                # 是 hermes-agent 开发中静默 CI 监视器失败的头号原因。2026 年
                # 5 月暴露出这一确切失败模式的 PR：#31329、#31448、#31695、
                # #31709、#31745、#32264、#33131。出现过的失败模式：
                #   * `gh pr view --json statusCheckRollup --jq ...` 中，
                #     `from_entries` 在遇到 null 的 `conclusion` 键时出错，循环
                #     带着空状态静默退出，永不终止。
                #   * `for i in $(seq 1 60); do ... 2>&1` 的块缓冲 stdout 从未
                #     刷新到后台进程捕获；SIGTERM 在刷新前截断了缓冲区；
                #     `process(action='log')` 永远返回 total_lines=0。
                #   * conclusion 与 status 字段混淆：在 `.conclusion` 中过滤
                #     `PENDING`，而进行中的检查 conclusion 为空 → 轮询器在 18/23
                #     个检查仍处于 IN_PROGRESS 时就宣告全部通过。
                #   * 在 grep 匹配仅在 TTY 下出现的横幅（"All checks were
                #     successful"），而这些横幅在 stdout 被管道处理时根本不会出现。
                # green-ci-policy 技能中的标准模式可以避免上述每一个问题——让循环
                # 依据退出码，或依据以制表符分隔的
                # `awk -F"\t" "$2==\"pending\""`（第 2 列）。
                # 这里的检测器刻意收窄范围：它只标记 statusCheckRollup 的
                # JSON-API 路径，以及 `gh pr checks` + jq 的组合，但不标记标准的
                # 第 2 列 awk 轮询器（后者是在制表符上使用 awk，而不是作为通用的
                # stdout 解析器）。当我们检测到这种自制写法时，会把 agent 指向
                # 标准代码片段，而不是任由它再交付一个坏掉的轮询器。
                if background and command:
                    _gh = ("gh pr view" in command or "gh pr checks" in command)
                    _has_jq = (
                        " jq " in command or "| jq" in command or "$(jq" in command
                    )
                    _bad_shape = (
                        # JSON-API 反模式。即使没有 jq，走 `--json statusCheckRollup`
                        # + 解析这条路，也会让你陷入 conclusion 与 status 字段混淆的
                        # 噩梦。
                        "statusCheckRollup" in command
                        # 把 gh pr checks 通过管道交给 jq 同样是错的——`gh pr
                        # checks` 并不输出 JSON，所以这里的任何 `| jq` 都属于
                        # 意图混乱。标准的第 2 列轮询器用的是 awk-on-tabs，而不是 jq。
                        or (_gh and _has_jq)
                    )
                    if _bad_shape:
                        existing = result_data.get("hint", "")
                        canonical_hint = (
                            "This looks like a homebrewed CI poller built from "
                            "`gh pr view --json statusCheckRollup` and/or "
                            "`gh pr checks | jq`. That shape has burned us "
                            "repeatedly in hermes-agent dev work (PRs #31329, "
                            "#31448, #31695, #31709, #31745, #32264, #33131) — "
                            "stdout buffering kills output capture, jq null-key "
                            "edge cases silently exit the loop, conclusion-vs-"
                            "status field confusion exits early with bogus "
                            "all-green verdicts, TTY-only summary banners "
                            "never appear when piped. Use the canonical "
                            "snippets in the green-ci-policy skill instead: "
                            "the exit-code-driven `gh pr checks $PR >/dev/null` "
                            "(rc 0 = green, 8 = pending, else fail) for "
                            "exit-on-first-fail behavior, or the column-2 "
                            "awk-on-tabs poller "
                            "(`awk -F\"\\t\" \"$2==\\\"pending\\\"\"`) for "
                            "sharded matrices. Load skill_view("
                            "name='github/hermes-agent-dev', "
                            "file_path='references/green-ci-policy.md') for "
                            "the verbatim snippets. If you must roll a custom "
                            "loop with rich structured output, write each tick "
                            "to a known file (`tee -a /tmp/ci.log`) and rely "
                            "on `process(action='log')` to read THAT file — "
                            "do not rely on background-process stdout capture "
                            "for line-buffered shell loops."
                        )
                        result_data["hint"] = (
                            existing + "\n\n" + canonical_hint if existing
                            else canonical_hint
                        )

                # 在会话上填充路由元数据，以便 watch-pattern 和完成通知能
                # 被路由回正确的聊天/线程。
                if background and (notify_on_complete or watch_patterns):
                    from gateway.session_context import (
                        async_delivery_supported as _async_ok,
                        get_session_env as _gse,
                    )

                    # 无状态的请求/响应会话（API server / WebUI 路径）无法在
                    # 轮次结束后把完成事件路由回 agent——没有持久通道，send() 是
                    # 空操作。在那里注册监视器会静默地变成空操作（issue #10760）。
                    # 与其如此，不如拒绝这个承诺：丢弃这些标志，并告诉 agent 去轮询。
                    if not _async_ok():
                        notify_on_complete = False
                        watch_patterns = None
                        result_data["notify_on_complete"] = False
                        result_data["notify_unsupported"] = (
                            "notify_on_complete / watch_patterns are not available on "
                            "this endpoint (stateless HTTP API — no channel to deliver "
                            "an async completion after the turn ends). The process is "
                            "running in the background; retrieve its result with "
                            "process(action='poll') or process(action='wait')."
                        )
                        logger.info(
                            "background proc %s: async delivery unsupported on this "
                            "session; notify_on_complete/watch_patterns disabled",
                            proc_session.id,
                        )
                    else:
                        _gw_platform = _gse("HERMES_SESSION_PLATFORM", "")
                        if _gw_platform:
                            _gw_chat_id = _gse("HERMES_SESSION_CHAT_ID", "")
                            _gw_thread_id = _gse("HERMES_SESSION_THREAD_ID", "")
                            _gw_user_id = _gse("HERMES_SESSION_USER_ID", "")
                            _gw_user_name = _gse("HERMES_SESSION_USER_NAME", "")
                            _gw_message_id = _gse("HERMES_SESSION_MESSAGE_ID", "")
                            proc_session.watcher_platform = _gw_platform
                            proc_session.watcher_chat_id = _gw_chat_id
                            proc_session.watcher_user_id = _gw_user_id
                            proc_session.watcher_user_name = _gw_user_name
                            proc_session.watcher_thread_id = _gw_thread_id
                            proc_session.watcher_message_id = _gw_message_id

                # 互斥处理：如果 notify_on_complete 和 watch_patterns 同时设置，
                # 则丢弃 watch_patterns。这种组合会产生重复通知（每次匹配一次 +
                # 退出时一次），且是异步投递，可能在进程结束很久之后还在打扰用户。
                # notify_on_complete 是更有用的「任务完成时告诉我」信号；
                # watch_patterns 应保留给长时间运行进程上的独立进程中途信号。
                watch_patterns, conflict_note = _resolve_notification_flag_conflict(
                    notify_on_complete=bool(notify_on_complete),
                    watch_patterns=watch_patterns,
                    background=bool(background),
                )
                if conflict_note:
                    logger.warning("background proc %s: %s", proc_session.id, conflict_note)
                    result_data["watch_patterns_ignored"] = conflict_note

                # 标记在完成时通知 agent
                if notify_on_complete and background:
                    proc_session.notify_on_complete = True
                    result_data["notify_on_complete"] = True

                    # 在 gateway 模式下，自动注册一个快速监视器，以便 gateway
                    # 能检测到完成并触发新的 agent 轮次。CLI 模式则直接使用
                    # completion_queue。
                    if proc_session.watcher_platform:
                        proc_session.watcher_interval = 5
                        process_registry.pending_watchers.append({
                            "session_id": proc_session.id,
                            "check_interval": 5,
                            "session_key": session_key,
                            "platform": proc_session.watcher_platform,
                            "chat_id": proc_session.watcher_chat_id,
                            "user_id": proc_session.watcher_user_id,
                            "user_name": proc_session.watcher_user_name,
                            "thread_id": proc_session.watcher_thread_id,
                            "message_id": proc_session.watcher_message_id,
                            "notify_on_complete": True,
                        })

                # 为输出监视设置 watch patterns
                if watch_patterns and background:
                    proc_session.watch_patterns = list(watch_patterns)
                    result_data["watch_patterns"] = proc_session.watch_patterns

                return json.dumps(result_data, ensure_ascii=False)
            except Exception as e:
                return json.dumps({
                    "output": "",
                    "exit_code": -1,
                    "error": f"Failed to start background process: {str(e)}"
                }, ensure_ascii=False)
        else:
            # 运行前台命令并带重试逻辑
            max_retries = 3
            retry_count = 0
            result = None
            command_cwd = None
            
            while retry_count <= max_retries:
                try:
                    command_cwd = _resolve_command_cwd(
                        workdir=workdir,
                        env=env,
                        default_cwd=cwd,
                    )
                    execute_kwargs = {
                        "timeout": effective_timeout,
                        "cwd": command_cwd,
                    }
                    result = env.execute(command, **execute_kwargs)
                except Exception as e:
                    error_str = str(e).lower()
                    if "timeout" in error_str:
                        return json.dumps({
                            "output": "",
                            "exit_code": 124,
                            "error": f"Command timed out after {effective_timeout} seconds"
                        }, ensure_ascii=False)
                    
                    # 遇到瞬时错误时重试
                    if retry_count < max_retries:
                        retry_count += 1
                        wait_time = 2 ** retry_count
                        logger.warning("Execution error, retrying in %ds (attempt %d/%d) - Command: %s - Error: %s: %s - Task: %s, Backend: %s",
                                       wait_time, retry_count, max_retries, _safe_command_preview(command), type(e).__name__, e, effective_task_id, env_type)
                        time.sleep(wait_time)
                        continue
                    
                    logger.error("Execution failed after %d retries - Command: %s - Error: %s: %s - Task: %s, Backend: %s",
                                 max_retries, _safe_command_preview(command), type(e).__name__, e, effective_task_id, env_type)
                    return json.dumps({
                        "output": "",
                        "exit_code": -1,
                        "error": f"Command execution failed: {type(e).__name__}: {str(e)}"
                    }, ensure_ascii=False)
                
                # 得到结果
                break

            # 提取输出
            output = result.get("output", "")
            returncode = result.get("returncode", 0)

            # 为消息上下文中的 sudo 失败追加帮助信息
            output = _handle_sudo_failure(output, env_type)

            # 前台终端输出的规范化接入点：插件在默认截断之前会收到完整的
            # 输出字符串，并且只有在 transform_terminal_output 中返回一个字符串
            # 才能替换它。该钩子是失败放行的，第一个有效的字符串返回值生效。
            try:
                from hermes_cli.plugins import invoke_hook
                hook_results = invoke_hook(
                    "transform_terminal_output",
                    command=command,
                    output=output,
                    returncode=returncode,
                    task_id=effective_task_id or "",
                    env_type=env_type,
                )
                for hook_result in hook_results:
                    if isinstance(hook_result, str):
                        output = hook_result
                        break
            except Exception:
                pass
            
            # 如果输出过长则截断，同时保留头部和尾部
            from tools.tool_output_limits import get_max_bytes
            MAX_OUTPUT_CHARS = get_max_bytes()
            if len(output) > MAX_OUTPUT_CHARS:
                head_chars = int(MAX_OUTPUT_CHARS * 0.4)  # 40% 头部（错误信息常出现在开头）
                tail_chars = MAX_OUTPUT_CHARS - head_chars  # 60% 尾部（最新/最相关的输出）
                omitted = len(output) - head_chars - tail_chars
                truncated_notice = (
                    f"\n\n... [OUTPUT TRUNCATED - {omitted} chars omitted "
                    f"out of {len(output)} total] ...\n\n"
                )
                output = output[:head_chars] + truncated_notice + output[-tail_chars:]

            # 去除 ANSI 转义序列，使模型永远不会看到终端格式——防止它把转义
            # 字符复制到文件写入中。
            from tools.ansi_strip import strip_ansi
            output = strip_ansi(output)

            # 对命令输出中的密钥进行脱敏（捕获 env/printenv 泄漏密钥的情况）
            from agent.redact import redact_sensitive_text
            output = redact_sensitive_text(output.strip()) if output else ""

            # 解释那些并非真正错误的非零退出码
            # （例如 grep=1 表示「没有匹配」，diff=1 表示「文件存在差异」）
            exit_note = _interpret_exit_code(command, returncode)

            result_dict = {
                "output": output,
                "exit_code": returncode,
                "error": None,
            }
            try:
                from agent.verification_evidence import record_terminal_result

                evidence = record_terminal_result(
                    command=command,
                    cwd=command_cwd,
                    session_id=session_id or task_id or effective_task_id or "default",
                    exit_code=returncode,
                    output=output,
                )
                if evidence:
                    result_dict["verification_evidence"] = {
                        "status": evidence.get("status"),
                        "kind": evidence.get("kind"),
                        "scope": evidence.get("scope"),
                        "canonical_command": evidence.get("canonical_command"),
                    }
            except Exception:
                logger.debug("verification evidence recording failed", exc_info=True)
            if approval_note:
                result_dict["approval"] = approval_note
            if exit_note:
                result_dict["exit_code_meaning"] = exit_note

            return json.dumps(result_dict, ensure_ascii=False)

    except Exception as e:
        import traceback
        tb_str = traceback.format_exc()
        logger.error("terminal_tool exception:\n%s", tb_str)
        return json.dumps({
            "output": "",
            "exit_code": -1,
            "error": f"Failed to execute command: {str(e)}",
            "traceback": tb_str,
            "status": "error"
        }, ensure_ascii=False)


def check_terminal_requirements() -> bool:
    """检查终端工具的所有依赖是否就绪。"""
    try:
        config = _get_env_config()
        env_type = config["env_type"]

        if env_type == "local":
            return True

        elif env_type == "docker":
            from tools.environments.docker import find_docker
            docker = find_docker()
            if not docker:
                logger.error("Docker executable not found in PATH or common install locations")
                return False
            result = subprocess.run([docker, "version"], capture_output=True, timeout=5, stdin=subprocess.DEVNULL)
            return result.returncode == 0

        elif env_type == "singularity":
            executable = shutil.which("apptainer") or shutil.which("singularity")
            if executable:
                result = subprocess.run([executable, "--version"], capture_output=True, timeout=5, stdin=subprocess.DEVNULL)
                return result.returncode == 0
            return False

        elif env_type == "ssh":
            if not config.get("ssh_host") or not config.get("ssh_user"):
                logger.error(
                    "SSH backend selected but TERMINAL_SSH_HOST and TERMINAL_SSH_USER "
                    "are not both set. Configure both or switch TERMINAL_ENV to 'local'."
                )
                return False
            return True

        elif env_type == "modal":
            modal_state = _get_modal_backend_state(config.get("modal_mode"))
            if modal_state["selected_backend"] == "managed":
                return True

            if modal_state["selected_backend"] != "direct":
                if modal_state["managed_mode_blocked"]:
                    logger.error(
                        "Modal backend selected with TERMINAL_MODAL_MODE=managed, but "
                        "Nous Tool Gateway access is not currently available and no direct "
                        "Modal credentials/config were found. %s Choose "
                        "TERMINAL_MODAL_MODE=direct/auto to use direct Modal credentials.",
                        nous_tool_gateway_unavailable_message(
                            "managed Modal execution",
                        ),
                    )
                    return False
                if modal_state["mode"] == "managed":
                    logger.error(
                        "Modal backend selected with TERMINAL_MODAL_MODE=managed, but the managed "
                        "tool gateway is unavailable. %s",
                        nous_tool_gateway_unavailable_message(
                            "managed Modal execution",
                        ),
                    )
                    return False
                elif modal_state["mode"] == "direct":
                    if managed_nous_tools_enabled():
                        logger.error(
                            "Modal backend selected with TERMINAL_MODAL_MODE=direct, but no direct "
                            "Modal credentials/config were found. Configure Modal or choose "
                            "TERMINAL_MODAL_MODE=managed/auto."
                        )
                    else:
                        logger.error(
                            "Modal backend selected with TERMINAL_MODAL_MODE=direct, but no direct "
                            "Modal credentials/config were found. Configure Modal or choose "
                            "TERMINAL_MODAL_MODE=auto."
                        )
                    return False
                else:
                    if managed_nous_tools_enabled():
                        logger.error(
                            "Modal backend selected but no direct Modal credentials/config or managed "
                            "tool gateway was found. Configure Modal, set up the managed gateway, "
                            "or choose a different TERMINAL_ENV."
                        )
                    else:
                        logger.error(
                            "Modal backend selected but no direct Modal credentials/config was found. "
                            "Configure Modal or choose a different TERMINAL_ENV."
                        )
                    return False

            if importlib.util.find_spec("modal") is None:
                logger.error("modal is required for direct modal terminal backend: pip install modal")
                return False

            return True

        elif env_type == "daytona":
            from daytona import Daytona  # noqa: F401 — SDK presence check
            return os.getenv("DAYTONA_API_KEY") is not None

        else:
            logger.error(
                "Unknown TERMINAL_ENV '%s'. Use one of: local, docker, singularity, "
                "modal, daytona, ssh.",
                env_type,
            )
            return False
    except Exception as e:
        logger.error("Terminal requirements check failed: %s", e, exc_info=True)
        return False


if __name__ == "__main__":
    # 直接运行时的简单测试
    print("Terminal Tool Module")
    print("=" * 50)
    
    config = _get_env_config()
    print("\nCurrent Configuration:")
    print(f"  Environment type: {config['env_type']}")
    print(f"  Docker image: {config['docker_image']}")
    print(f"  Modal image: {config['modal_image']}")
    print(f"  Working directory: {config['cwd']}")
    print(f"  Default timeout: {config['timeout']}s")
    print(f"  Lifetime: {config['lifetime_seconds']}s")

    if not check_terminal_requirements():
        print("\n❌ Requirements not met. Please check the messages above.")
        sys.exit(1)

    print("\n✅ All requirements met!")
    print("\nAvailable Tool:")
    print("  - terminal_tool: Execute commands in sandboxed environments")

    print("\nUsage Examples:")
    print("  # Execute a command")
    print("  result = terminal_tool(command='ls -la')")
    print("  ")
    print("  # Run a background task")
    print("  result = terminal_tool(command='python server.py', background=True)")

    print("\nEnvironment Variables:")
    default_img = "nikolaik/python-nodejs:python3.11-nodejs20"
    print(
        "  TERMINAL_ENV: "
        f"{os.getenv('TERMINAL_ENV', 'local')} "
        "(local/docker/singularity/modal/daytona/ssh)"
    )
    print(f"  TERMINAL_DOCKER_IMAGE: {os.getenv('TERMINAL_DOCKER_IMAGE', default_img)}")
    print(f"  TERMINAL_SINGULARITY_IMAGE: {os.getenv('TERMINAL_SINGULARITY_IMAGE', f'docker://{default_img}')}")
    print(f"  TERMINAL_MODAL_IMAGE: {os.getenv('TERMINAL_MODAL_IMAGE', default_img)}")
    print(f"  TERMINAL_DAYTONA_IMAGE: {os.getenv('TERMINAL_DAYTONA_IMAGE', default_img)}")
    print(f"  TERMINAL_CWD: {os.getenv('TERMINAL_CWD', _safe_getcwd())}")
    from hermes_constants import display_hermes_home as _dhh
    print(f"  TERMINAL_SANDBOX_DIR: {os.getenv('TERMINAL_SANDBOX_DIR', f'{_dhh()}/sandboxes')}")
    print(f"  TERMINAL_TIMEOUT: {os.getenv('TERMINAL_TIMEOUT', '60')}")
    print(f"  TERMINAL_LIFETIME_SECONDS: {os.getenv('TERMINAL_LIFETIME_SECONDS', '300')}")


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
from tools.registry import registry

TERMINAL_SCHEMA = {
    "name": "terminal",
    "description": TERMINAL_TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The command to execute on the VM"
            },
            "background": {
                "type": "boolean",
                "description": "Run the command in the background. Almost always pair with notify_on_complete=true — without it, the process runs silently and you'll have no way to learn it finished short of calling process(action='poll') yourself (easy to forget, leading to silent blindness on long jobs). Two legitimate patterns: (1) Long-lived processes that never exit (servers, watchers, daemons) — these stay silent because there's no exit to notify on. (2) Long-running bounded tasks (tests, builds, deploys, CI pollers, batch jobs) — these MUST set notify_on_complete=true. For short commands, prefer foreground with a generous timeout instead.",
                "default": False
            },
            "timeout": {
                "type": "integer",
                "description": f"Max seconds to wait (default: 180, foreground max: {FOREGROUND_MAX_TIMEOUT}). Returns INSTANTLY when command finishes — set high for long tasks, you won't wait unnecessarily. Foreground timeout above {FOREGROUND_MAX_TIMEOUT}s is rejected; use background=true for longer commands.",
                "minimum": 1
            },
            "workdir": {
                "type": "string",
                "description": "Working directory for this command (absolute path). Defaults to the session working directory."
            },
            "pty": {
                "type": "boolean",
                "description": "Run in pseudo-terminal (PTY) mode for interactive CLI tools like Codex, Claude Code, or Python REPL. Only works with local and SSH backends. Default: false.",
                "default": False
            },
            "notify_on_complete": {
                "type": "boolean",
                "description": "When true (and background=true), you'll be automatically notified exactly once when the process finishes. **This is the right choice for almost every long-running task** — tests, builds, deployments, multi-item batch jobs, anything that takes over a minute and has a defined end. Use this and keep working on other things; the system notifies you on exit. MUTUALLY EXCLUSIVE with watch_patterns — when both are set, watch_patterns is dropped.",
                "default": False
            },
            "watch_patterns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Strings to watch for in background process output. HARD RATE LIMIT: at most 1 notification per 15 seconds per process — matches arriving inside the cooldown are dropped. After 3 consecutive 15-second windows with dropped matches, watch_patterns is automatically disabled for that process and promoted to notify_on_complete behavior (one notification on exit, no more mid-process spam). USE ONLY for truly rare, one-shot mid-process signals on LONG-LIVED processes that will never exit on their own — e.g. ['Application startup complete'] on a server so you know when to hit its endpoint, or ['migration done'] on a daemon. DO NOT use for: (1) end-of-run markers like 'DONE'/'PASS' — use notify_on_complete instead; (2) error patterns like 'ERROR'/'Traceback' in loops or multi-item batch jobs — they fire on every iteration and you'll hit the strike limit fast; (3) anything you'd ever combine with notify_on_complete. When in doubt, choose notify_on_complete. MUTUALLY EXCLUSIVE with notify_on_complete — set one, not both."
            }
        },
        "required": ["command"]
    }
}


def _handle_terminal(args, **kw):
    return terminal_tool(
        command=args.get("command"),
        background=args.get("background", False),
        timeout=args.get("timeout"),
        task_id=kw.get("task_id"),
        session_id=kw.get("session_id"),
        workdir=args.get("workdir"),
        pty=args.get("pty", False),
        notify_on_complete=args.get("notify_on_complete", False),
        watch_patterns=args.get("watch_patterns"),
    )


registry.register(
    name="terminal",
    toolset="terminal",
    schema=TERMINAL_SCHEMA,
    handler=_handle_terminal,
    check_fn=check_terminal_requirements,
    emoji="💻",
    max_result_size_chars=100_000,
)
