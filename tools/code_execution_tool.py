#!/usr/bin/env python3
"""
代码执行工具 —— 编程式工具调用（Programmatic Tool Calling，PTC）

让 LLM 编写一段 Python 脚本，通过 RPC 调用 Hermes 工具，
从而把多步工具链压缩为一次推理回合。

架构（两种传输方式）：

  **本地后端（UDS）：**
  1. 父进程生成一个带有 UDS RPC 函数的 `hermes_tools.py` 桩模块
  2. 父进程打开一个 Unix 域套接字并启动 RPC 监听线程
  3. 父进程派生一个运行 LLM 脚本的子进程
  4. 工具调用通过 UDS 传回父进程进行分发

  **远程后端（基于文件的 RPC）：**
  1. 父进程生成带有基于文件 RPC 桩的 `hermes_tools.py`
  2. 父进程把两个文件传送到远程环境
  3. 脚本在终端后端内运行（Docker/SSH/Modal/Daytona 等）
  4. 工具调用被写为请求文件；父进程上的轮询线程通过 env.execute()
     读取、分发并写回响应文件
  5. 脚本轮询响应文件并继续执行

两种情况下，只有脚本的 stdout 会返回给 LLM；中间的工具结果
绝不会进入上下文窗口。

平台：仅限 Linux / macOS（本地使用 Unix 域套接字）。Windows 上禁用。
远程执行还要求终端后端中存在 Python 3。
"""

import base64
import functools
import json
import logging
import os
import platform
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

_IS_WINDOWS = platform.system() == "Windows"
from typing import Any, Dict, List, Optional

from tools.thread_context import propagate_context_to_thread

# 可用性开关。在 Windows 上，沙箱 RPC 传输回退到环回 TCP
# （AF_UNIX 在 Windows Python 上不可靠）——见下方 ``_execute_local``
# 中的 ``_use_tcp_rpc``。这样 execute_code 在 Hermes 自身能运行的
# 每个平台上都可用。
logger = logging.getLogger(__name__)

SANDBOX_AVAILABLE = True

# 沙箱内允许使用的 7 个工具。此列表与会话已启用的工具取交集，
# 决定生成哪些桩函数。
SANDBOX_ALLOWED_TOOLS = frozenset([
    "web_search",
    "web_extract",
    "read_file",
    "write_file",
    "search_files",
    "patch",
    "terminal",
])

# 资源限制默认值（可通过 config.yaml → code_execution.* 覆盖）
DEFAULT_TIMEOUT = 300        # 5 分钟
DEFAULT_MAX_TOOL_CALLS = 50
MAX_STDOUT_BYTES = 50_000    # 50 KB
MAX_STDERR_BYTES = 10_000    # 10 KB

# 环境变量清洗规则（本地与远程后端共享）。先按密钥子串进行阻断；
# 剩下的必须匹配安全前缀、运营性 HERMES_ 允许清单，或（在 Windows 上）
# 操作系统必需的变量名。
#
# 注意：宽泛的 "HERMES_" 前缀已被刻意移除（#27303）——它会泄露不含密钥
# 子串的 HERMES_* 配置（例如 HERMES_BASE_URL、HERMES_KANBAN_DB、
# HERMES_*_WEBHOOK）。子进程只需要下方 _HERMES_CHILD_ALLOWED 中那几个
# 位置/配置文件变量；HERMES_RPC_SOCKET / HERMES_RPC_DIR / TZ / HOME 在
# 清洗之后显式注入。
_SAFE_ENV_PREFIXES = ("PATH", "HOME", "USER", "LANG", "LC_", "TERM",
                      "TMPDIR", "TMP", "TEMP", "SHELL", "LOGNAME",
                      "XDG_", "PYTHONPATH", "VIRTUAL_ENV", "CONDA")
_SECRET_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL",
                      "PASSWD", "AUTH", "DSN", "WEBHOOK")

# 子进程按精确名称合法需要的运营性 HERMES_* 变量——这些是非密钥的
# 运行时位置标志（与 hermes_cli 视为运行时位置的那一组相同），沙箱脚本
# 所 import 的仓库根目录模块可能会在导入时读取它们。它们都不匹配
# _SECRET_SUBSTRINGS。
_HERMES_CHILD_ALLOWED = frozenset({
    "HERMES_HOME",
    "HERMES_PROFILE",
    "HERMES_CONFIG",
    "HERMES_ENV",
})

# 仅 Windows：少数变量是操作系统/CRT 自身所必需的。缺少它们时，连
# ``socket.socket()`` 这样的标准库调用都会以 WinError 10106 失败
# （Winsock 找不到 mswsock.dll），``subprocess`` 也无法解析 cmd.exe。
# 这些都是众所周知的操作系统路径，不是密钥，因此我们按精确名称放行。
# _SECRET_SUBSTRINGS 阻断仍作为安全网运行（这些名称都不匹配那些子串）。
_WINDOWS_ESSENTIAL_ENV_VARS = frozenset({
    "SYSTEMROOT",       # %SYSTEMROOT%\System32 —— Winsock 需要
    "SYSTEMDRIVE",      # C:（或 Windows 安装所在的盘）
    "WINDIR",           # 通常与 SYSTEMROOT 相同
    "COMSPEC",          # cmd.exe 路径 —— subprocess shell=True 需要
    "PATHEXT",          # .COM;.EXE;.BAT;... —— shell 查找
    "OS",               # "Windows_NT" —— 一些工具据此判断
    "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS",
    "PUBLIC",           # C:\Users\Public
    "ALLUSERSPROFILE",  # C:\ProgramData —— 一些标准库路径会用到
    "PROGRAMDATA",      # C:\ProgramData
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "APPDATA",          # %USERPROFILE%\AppData\Roaming —— Python 会用到
    "LOCALAPPDATA",     # %USERPROFILE%\AppData\Local
    "USERPROFILE",      # C:\Users\<name> —— Python 的 expanduser 会用到
    "USERDOMAIN",
    "USERNAME",
    "HOMEDRIVE",        # C:
    "HOMEPATH",         # \Users\<name>
    "COMPUTERNAME",
})


def _scrub_child_env(source_env, is_passthrough=None, is_windows=None):
    """为 execute_code 生成清洗后的子进程环境变量。

    规则（顺序很重要）：
      1. 透传变量（技能或配置声明的）一律放行。
      2. 含密钥子串的名称（KEY/TOKEN/DSN/WEBHOOK 等）被阻断。
      3. 匹配安全前缀的名称放行。
      4. 运营性 HERMES_* 变量（_HERMES_CHILD_ALLOWED）按精确名称放行。
      5. 在 Windows 上，一小份操作系统必需的允许清单按精确名称放行
         ——缺少这些，子进程甚至无法创建套接字或派生子进程。

    抽成一个辅助函数，便于测试在不派生子进程的情况下验证逻辑。
    """
    if is_passthrough is None:
        try:
            from tools.env_passthrough import is_env_passthrough as _ep
        except Exception:
            _ep = lambda _: False  # noqa: E731
        is_passthrough = _ep
    if is_windows is None:
        is_windows = _IS_WINDOWS

    scrubbed = {}
    # 被收紧后的允许清单（#27303）丢弃的非密钥 HERMES_* 变量。过去宽泛的
    # "HERMES_" 前缀会放行它们；现在只有运营性集合放行。这个丢弃是
    # 有意的（这些变量可能携带 HERMES_KANBAN_DB / HERMES_BASE_URL 等
    # 配置），但若沙箱脚本 import 的仓库模块在导入时读取其中某个变量，
    # 原本会看到它被静默置空。这里把丢弃行为显式记录一次，以便诊断
    # 行为变化，并指向 env_passthrough 这个显式 opt-in 的逃生通道。
    _dropped_hermes = []
    for k, v in source_env.items():
        if is_passthrough(k):
            scrubbed[k] = v
            continue
        if any(s in k.upper() for s in _SECRET_SUBSTRINGS):
            continue
        if any(k.startswith(p) for p in _SAFE_ENV_PREFIXES):
            scrubbed[k] = v
            continue
        if k in _HERMES_CHILD_ALLOWED:
            scrubbed[k] = v
            continue
        if is_windows and k.upper() in _WINDOWS_ESSENTIAL_ENV_VARS:
            scrubbed[k] = v
            continue
        if k.startswith("HERMES_"):
            # 非密钥（密钥已在上方被丢弃）且不在任何允许清单中
            # —— 一个被刻意丢弃的 HERMES_* 变量。
            _dropped_hermes.append(k)
    if _dropped_hermes:
        logger.debug(
            "execute_code: dropped %d non-allowlisted HERMES_* var(s) from the "
            "sandbox child env (%s). This is intentional hardening (#27303); if "
            "a sandbox script legitimately needs one, declare it via "
            "env_passthrough in the skill/config so it passes by explicit opt-in.",
            len(_dropped_hermes),
            ", ".join(sorted(_dropped_hermes)),
        )
    return scrubbed


def check_sandbox_requirements() -> bool:
    """代码执行沙箱需要 POSIX 操作系统以支持 Unix 域套接字。"""
    if not SANDBOX_AVAILABLE:
        return False
    return True


# ---------------------------------------------------------------------------
# hermes_tools.py 代码生成器
# ---------------------------------------------------------------------------

# 各工具的桩模板：(函数名, 签名, docstring, 参数字典表达式)
# args_dict_expr 构造通过 RPC 套接字发送的 JSON 负载。
_TOOL_STUBS = {
    "web_search": (
        "web_search",
        "query: str, limit: int = 5",
        '"""Search the web. Returns dict with data.web list of {url, title, description}."""',
        '{"query": query, "limit": limit}',
    ),
    "web_extract": (
        "web_extract",
        "urls: list",
        '"""Extract content from URLs. Returns dict with results list of {url, title, content, error}."""',
        '{"urls": urls}',
    ),
    "read_file": (
        "read_file",
        "path: str, offset: int = 1, limit: int = 500",
        '"""Read a file (1-indexed lines). Returns dict with "content" and "total_lines"."""',
        '{"path": path, "offset": offset, "limit": limit}',
    ),
    "write_file": (
        "write_file",
        "path: str, content: str, cross_profile: bool = False",
        '"""Write content to a file (always overwrites). Returns dict with status. cross_profile=True opts out of the cross-Hermes-profile soft guard."""',
        '{"path": path, "content": content, "cross_profile": cross_profile}',
    ),
    "search_files": (
        "search_files",
        'pattern: str, target: str = "content", path: str = ".", file_glob: str = None, limit: int = 50, offset: int = 0, output_mode: str = "content", context: int = 0',
        '"""Search file contents (target="content") or find files by name (target="files"). Returns dict with "matches"."""',
        '{"pattern": pattern, "target": target, "path": path, "file_glob": file_glob, "limit": limit, "offset": offset, "output_mode": output_mode, "context": context}',
    ),
    "patch": (
        "patch",
        'path: str = None, old_string: str = None, new_string: str = None, replace_all: bool = False, mode: str = "replace", patch: str = None, cross_profile: bool = False',
        '"""Targeted find-and-replace (mode="replace") or V4A multi-file patches (mode="patch"). Returns dict with status. cross_profile=True opts out of the cross-Hermes-profile soft guard."""',
        '{"path": path, "old_string": old_string, "new_string": new_string, "replace_all": replace_all, "mode": mode, "patch": patch, "cross_profile": cross_profile}',
    ),
    "terminal": (
        "terminal",
        "command: str, timeout: int = None, workdir: str = None",
        '"""Run a shell command (foreground only). Returns dict with "output" and "exit_code"."""',
        '{"command": command, "timeout": timeout, "workdir": workdir}',
    ),
}


def generate_hermes_tools_module(enabled_tools: List[str],
                                 transport: str = "uds") -> str:
    """
    构建 hermes_tools.py 桩模块的源代码。

    只有同时存在于 SANDBOX_ALLOWED_TOOLS 和 enabled_tools 中的工具
    才会生成桩函数。

    参数：
        enabled_tools: 当前会话中启用的工具名列表。
        transport: ``"uds"`` 表示 Unix 域套接字（本地后端），
                   ``"file"`` 表示基于文件的 RPC（远程后端）。
    """
    tools_to_generate = sorted(SANDBOX_ALLOWED_TOOLS & set(enabled_tools))

    stub_functions = []
    export_names = []
    for tool_name in tools_to_generate:
        if tool_name not in _TOOL_STUBS:
            continue
        func_name, sig, doc, args_expr = _TOOL_STUBS[tool_name]
        stub_functions.append(
            f"def {func_name}({sig}):\n"
            f"    {doc}\n"
            f"    return _call({func_name!r}, {args_expr})\n"
        )
        export_names.append(func_name)

    if transport == "file":
        header = _FILE_TRANSPORT_HEADER
    else:
        header = _UDS_TRANSPORT_HEADER

    return header + "\n".join(stub_functions)


# ---- 共享辅助函数段（嵌入到两种传输头中）----------

_COMMON_HELPERS = '''\

# ---------------------------------------------------------------------------
# 便捷辅助函数（规避常见脚本陷阱）
# ---------------------------------------------------------------------------

def json_parse(text: str):
    """解析 JSON，容忍控制字符（strict=False）。
    当解析 terminal() 或 web_extract() 的输出（字符串中可能含有原始制表符/换行）时，
    请用本函数代替 json.loads()。"""
    return json.loads(text, strict=False)


def shell_quote(s: str) -> str:
    """对字符串做 shell 转义，以便安全地插入命令。
    向 terminal() 命令中插入动态内容时使用：
        terminal(f"echo {shell_quote(user_input)}")
    """
    return shlex.quote(s)


def retry(fn, max_attempts=3, delay=2):
    """最多重试 max_attempts 次，采用指数退避。
    用于瞬时失败（网络错误、API 速率限制）：
        result = retry(lambda: terminal("gh issue list ..."))
    """
    last_err = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if attempt < max_attempts - 1:
                time.sleep(delay * (2 ** attempt))
    raise last_err

'''

# ---- UDS 传输（本地后端）------------------------------------------------

_UDS_TRANSPORT_HEADER = '''\
"""Auto-generated Hermes tools RPC stubs."""
import json, os, socket, shlex, threading, time

_sock = None
# RPC 服务端串行处理单个客户端连接，且协议中没有 request-id，
# 因此来自多个线程（例如 ThreadPoolExecutor）的并发 _call() 调用会在共享 socket 上
# 产生竞争，互相拿到对方的响应。需要把整个「发送+接收」往返过程串行化。
_call_lock = threading.Lock()
''' + _COMMON_HELPERS + '''\

def _connect():
    """通过父进程选定的传输方式连接到其 RPC 服务端。

    HERMES_RPC_SOCKET 可以是：
      - 一个文件系统路径（POSIX Unix domain socket —— 在
        Linux 和 macOS 上为默认）
      - 形如 ``tcp://127.0.0.1:<port>`` 的字符串（Windows 上
        AF_UNIX 不可靠 —— 父进程回退到环回 TCP）
    """
    global _sock
    if _sock is None:
        endpoint = os.environ["HERMES_RPC_SOCKET"]
        if endpoint.startswith("tcp://"):
            # tcp://host:port  （实践中 host 恒为 127.0.0.1 —— 我们
            # 服务端只绑定环回地址）
            _host_port = endpoint[len("tcp://"):]
            _host, _, _port = _host_port.rpartition(":")
            _sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            _sock.connect((_host or "127.0.0.1", int(_port)))
        else:
            _sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            _sock.connect(endpoint)
        _sock.settimeout(300)
    return _sock

def _call(tool_name, args):
    """向父进程发送一次工具调用，并返回解析后的结果。"""
    request = json.dumps({"tool": tool_name, "args": args}) + "\\n"
    with _call_lock:
        conn = _connect()
        conn.sendall(request.encode())
        buf = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                raise RuntimeError("Agent process disconnected")
            buf += chunk
            if buf.endswith(b"\\n"):
                break
    raw = buf.decode().strip()
    result = json.loads(raw)
    if isinstance(result, str):
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result
    return result

'''

# ---- 基于文件的传输（远程后端）--------------------------------------------

_FILE_TRANSPORT_HEADER = '''\
"""Auto-generated Hermes tools RPC stubs (file-based transport)."""
import json, os, shlex, tempfile, threading, time

_RPC_DIR = os.environ.get("HERMES_RPC_DIR") or os.path.join(tempfile.gettempdir(), "hermes_rpc")
_seq = 0
# `_seq += 1` 不是原子操作（读-改-写），因此来自多个线程的并发 _call()
# 调用可能分配到相同的序列号，并互相覆盖对方的请求文件。用锁保护序列号分配。
_seq_lock = threading.Lock()
''' + _COMMON_HELPERS + '''\

def _call(tool_name, args):
    """通过基于文件的 RPC 发送一次工具调用请求并等待响应。"""
    global _seq
    with _seq_lock:
        _seq += 1
        seq = _seq
    seq_str = f"{seq:06d}"
    req_file = os.path.join(_RPC_DIR, f"req_{seq_str}")
    res_file = os.path.join(_RPC_DIR, f"res_{seq_str}")

    # 原子地写入请求（先写到 .tmp，再 rename）。
    # encoding="utf-8" 至关重要：在 Windows 托管的远程后端
    # （或任何非 UTF-8 的 locale）上，默认的 open() 模式会在把工具参数
    # 编码为 JSON 时破坏其中的非 ASCII 字符。
    tmp = req_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"tool": tool_name, "args": args, "seq": seq}, f)
    os.rename(tmp, req_file)

    # 以自适应轮询等待响应
    deadline = time.monotonic() + 300  # 5-minute timeout per tool call
    poll_interval = 0.05  # Start at 50ms
    while not os.path.exists(res_file):
        if time.monotonic() > deadline:
            raise RuntimeError(f"RPC timeout: no response for {tool_name} after 300s")
        time.sleep(poll_interval)
        poll_interval = min(poll_interval * 1.2, 0.25)  # Back off to 250ms

    with open(res_file, encoding="utf-8") as f:
        raw = f.read()

    # 清理响应文件
    try:
        os.unlink(res_file)
    except OSError:
        pass

    result = json.loads(raw)
    if isinstance(result, str):
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result
    return result

'''


# ---------------------------------------------------------------------------
# RPC 服务器（在父进程内的一个线程中运行）
# ---------------------------------------------------------------------------

# 临时沙箱脚本不得使用的终端参数
_TERMINAL_BLOCKED_PARAMS = {"background", "pty", "notify_on_complete", "watch_patterns"}


def _rpc_server_loop(
    server_sock: socket.socket,
    task_id: str,
    tool_call_log: list,
    tool_call_counter: list,   # 可变的 [int]，便于线程自增
    max_tool_calls: int,
    allowed_tools: frozenset,
    stop_event: threading.Event,
):
    """
    接受一个客户端连接并分发工具调用请求，直到客户端断开连接
    或达到调用次数上限。
    """
    from model_tools import handle_function_call

    conn = None
    try:
        server_sock.settimeout(0.05)
        while not stop_event.is_set():
            try:
                conn, _ = server_sock.accept()
                break
            except socket.timeout:
                continue
        if conn is None:
            return
        conn.settimeout(300)

        buf = b""
        while True:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk

            # 处理缓冲区中所有完整的换行符分隔消息
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue

                call_start = time.monotonic()
                try:
                    request = json.loads(line.decode())
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    resp = tool_error(f"Invalid RPC request: {exc}")
                    conn.sendall((resp + "\n").encode())
                    continue

                tool_name = request.get("tool", "")
                tool_args = request.get("args", {})

                # 校验允许清单
                if tool_name not in allowed_tools:
                    available = ", ".join(sorted(allowed_tools))
                    resp = json.dumps({
                        "error": (
                            f"Tool '{tool_name}' is not available in execute_code. "
                            f"Available: {available}"
                        )
                    })
                    conn.sendall((resp + "\n").encode())
                    continue

                # 校验工具调用次数上限
                if tool_call_counter[0] >= max_tool_calls:
                    resp = json.dumps({
                        "error": (
                            f"Tool call limit reached ({max_tool_calls}). "
                            "No more tool calls allowed in this execution."
                        )
                    })
                    conn.sendall((resp + "\n").encode())
                    continue

                # 剥离被禁用的终端参数
                if tool_name == "terminal" and isinstance(tool_args, dict):
                    for param in _TERMINAL_BLOCKED_PARAMS:
                        tool_args.pop(param, None)

                # 通过标准工具处理器进行分发。
                # 抑制内部工具处理器的 stdout/stderr，避免它们的状态
                # 打印泄露到 CLI 的旋转动画中。
                try:
                    _real_stdout, _real_stderr = sys.stdout, sys.stderr
                    devnull = open(os.devnull, "w", encoding="utf-8")
                    try:
                        sys.stdout = devnull
                        sys.stderr = devnull
                        result = handle_function_call(
                            tool_name, tool_args, task_id=task_id
                        )
                    finally:
                        sys.stdout, sys.stderr = _real_stdout, _real_stderr
                        devnull.close()
                except Exception as exc:
                    logger.error("Tool call failed in sandbox: %s", exc, exc_info=True)
                    result = tool_error(str(exc))

                tool_call_counter[0] += 1
                call_duration = time.monotonic() - call_start

                # 记录以便可观测性
                args_preview = str(tool_args)[:80]
                tool_call_log.append({
                    "tool": tool_name,
                    "args_preview": args_preview,
                    "duration": round(call_duration, 2),
                })

                conn.sendall((result + "\n").encode())

    except socket.timeout:
        logger.debug("RPC listener socket timeout")
    except OSError as e:
        logger.debug("RPC listener socket error: %s", e, exc_info=True)
    finally:
        if conn:
            try:
                conn.close()
            except OSError as e:
                logger.debug("RPC conn close error: %s", e)


# ---------------------------------------------------------------------------
# 远程执行支持（通过终端后端进行基于文件的 RPC）
# ---------------------------------------------------------------------------

def _get_or_create_env(task_id: str):
    """获取或创建 *task_id* 对应的终端环境。

    复用终端工具和文件工具所使用的同一个环境（容器/沙箱/SSH 会话），
    若尚不存在则创建一个。返回 ``(env, env_type)`` 元组。
    """
    from tools.terminal_tool import (
        _active_environments, _env_lock, _create_environment,
        _get_env_config, _last_activity, _start_cleanup_thread,
        _creation_locks, _creation_locks_lock, _task_env_overrides,
        _resolve_container_task_id,
    )

    effective_task_id = _resolve_container_task_id(task_id)

    # 快速路径：环境已存在
    with _env_lock:
        if effective_task_id in _active_environments:
            _last_activity[effective_task_id] = time.time()
            return _active_environments[effective_task_id], _get_env_config()["env_type"]

    # 慢速路径：创建环境（与 file_tools._get_file_ops 相同的模式）
    with _creation_locks_lock:
        if effective_task_id not in _creation_locks:
            _creation_locks[effective_task_id] = threading.Lock()
        task_lock = _creation_locks[effective_task_id]

    with task_lock:
        with _env_lock:
            if effective_task_id in _active_environments:
                _last_activity[effective_task_id] = time.time()
                return _active_environments[effective_task_id], _get_env_config()["env_type"]

        config = _get_env_config()
        env_type = config["env_type"]
        overrides = _task_env_overrides.get(effective_task_id, {})

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

        container_config = None
        if env_type in {"docker", "singularity", "modal", "daytona"}:
            container_config = {
                "container_cpu": config.get("container_cpu", 1),
                "container_memory": config.get("container_memory", 5120),
                "container_disk": config.get("container_disk", 51200),
                "container_persistent": config.get("container_persistent", True),
                "docker_volumes": config.get("docker_volumes", []),
                "docker_run_as_host_user": config.get("docker_run_as_host_user", False),
            }

        ssh_config = None
        if env_type == "ssh":
            ssh_config = {
                "host": config.get("ssh_host", ""),
                "user": config.get("ssh_user", ""),
                "port": config.get("ssh_port", 22),
                "key": config.get("ssh_key", ""),
                "persistent": config.get("ssh_persistent", False),
            }

        local_config = None
        if env_type == "local":
            local_config = {
                "persistent": config.get("local_persistent", False),
            }

        logger.info("Creating new %s environment for execute_code task %s...",
                     env_type, effective_task_id[:8])
        env = _create_environment(
            env_type=env_type,
            image=image,
            cwd=cwd,
            timeout=config["timeout"],
            ssh_config=ssh_config,
            container_config=container_config,
            local_config=local_config,
            task_id=effective_task_id,
            host_cwd=config.get("host_cwd"),
        )

        with _env_lock:
            _active_environments[effective_task_id] = env
            _last_activity[effective_task_id] = time.time()

        _start_cleanup_thread()
        logger.info("%s environment ready for execute_code task %s",
                     env_type, effective_task_id[:8])
        return env, env_type


def _ship_file_to_remote(env, remote_path: str, content: str) -> None:
    """把 *content* 写到远程环境上的 *remote_path*。

    使用 ``echo … | base64 -d`` 而非 stdin 管道，因为某些后端（Modal）
    不能可靠地把 stdin_data 传给链式命令。Base64 输出是 shell 安全的
    （仅含 [A-Za-z0-9+/=]），所以用单引号即可。
    """
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    quoted_remote_path = shlex.quote(remote_path)
    env.execute(
        f"echo '{encoded}' | base64 -d > {quoted_remote_path}",
        cwd="/",
        timeout=30,
    )


def _env_temp_dir(env: Any) -> str:
    """为基于环境的 execute_code 沙箱返回一个可写的临时目录。"""
    get_temp_dir = getattr(env, "get_temp_dir", None)
    if callable(get_temp_dir):
        try:
            temp_dir = get_temp_dir()
            if isinstance(temp_dir, str) and temp_dir.startswith("/"):
                return temp_dir.rstrip("/") or "/"
        except Exception as exc:
            logger.debug("Could not resolve execute_code env temp dir: %s", exc)
    candidate = tempfile.gettempdir()
    if isinstance(candidate, str) and candidate.startswith("/"):
        return candidate.rstrip("/") or "/"
    return "/tmp"


def _rpc_poll_loop(
    env,
    rpc_dir: str,
    task_id: str,
    tool_call_log: list,
    tool_call_counter: list,
    max_tool_calls: int,
    allowed_tools: frozenset,
    stop_event: threading.Event,
):
    """轮询远程文件系统，获取工具调用请求并分发它们。

    在后台线程中运行。每次 ``env.execute()`` 都会派生一个独立进程，
    因此这些调用可以安全地与脚本执行线程并发运行。
    """
    from model_tools import handle_function_call

    poll_interval = 0.1  # 100 毫秒

    quoted_rpc_dir = shlex.quote(rpc_dir)
    while not stop_event.is_set():
        try:
            # 列出待处理的请求文件（跳过 .tmp 的半成品）
            ls_result = env.execute(
                f"ls -1 {quoted_rpc_dir}/req_* 2>/dev/null || true",
                cwd="/",
                timeout=10,
            )
            output = ls_result.get("output", "").strip()
            if not output:
                stop_event.wait(poll_interval)
                continue

            req_files = sorted([
                f.strip() for f in output.split("\n")
                if f.strip()
                and not f.strip().endswith(".tmp")
                and "/req_" in f.strip()
            ])

            for req_file in req_files:
                if stop_event.is_set():
                    break

                call_start = time.monotonic()

                quoted_req_file = shlex.quote(req_file)
                # 读取请求
                read_result = env.execute(
                    f"cat {quoted_req_file}",
                    cwd="/",
                    timeout=10,
                )
                try:
                    request = json.loads(read_result.get("output", ""))
                except (json.JSONDecodeError, ValueError):
                    logger.debug("Malformed RPC request in %s", req_file)
                    # 删除坏请求以避免无限重试
                    env.execute(f"rm -f {quoted_req_file}", cwd="/", timeout=5)
                    continue

                tool_name = request.get("tool", "")
                tool_args = request.get("args", {})
                seq = request.get("seq", 0)
                seq_str = f"{seq:06d}"
                res_file = f"{rpc_dir}/res_{seq_str}"
                quoted_res_file = shlex.quote(res_file)

                # 校验允许清单
                if tool_name not in allowed_tools:
                    available = ", ".join(sorted(allowed_tools))
                    tool_result = json.dumps({
                        "error": (
                            f"Tool '{tool_name}' is not available in execute_code. "
                            f"Available: {available}"
                        )
                    })
                # 校验工具调用次数上限
                elif tool_call_counter[0] >= max_tool_calls:
                    tool_result = json.dumps({
                        "error": (
                            f"Tool call limit reached ({max_tool_calls}). "
                            "No more tool calls allowed in this execution."
                        )
                    })
                else:
                    # 剥离被禁用的终端参数
                    if tool_name == "terminal" and isinstance(tool_args, dict):
                        for param in _TERMINAL_BLOCKED_PARAMS:
                            tool_args.pop(param, None)

                    # 通过标准工具处理器进行分发
                    try:
                        _real_stdout, _real_stderr = sys.stdout, sys.stderr
                        devnull = open(os.devnull, "w", encoding="utf-8")
                        try:
                            sys.stdout = devnull
                            sys.stderr = devnull
                            tool_result = handle_function_call(
                                tool_name, tool_args, task_id=task_id
                            )
                        finally:
                            sys.stdout, sys.stderr = _real_stdout, _real_stderr
                            devnull.close()
                    except Exception as exc:
                        logger.error("Tool call failed in remote sandbox: %s",
                                     exc, exc_info=True)
                        tool_result = tool_error(str(exc))

                    tool_call_counter[0] += 1
                    call_duration = time.monotonic() - call_start
                    tool_call_log.append({
                        "tool": tool_name,
                        "args_preview": str(tool_args)[:80],
                        "duration": round(call_duration, 2),
                    })

                # 原子化地写响应（先 tmp 再 rename）。
                # 使用 echo 管道（而非 stdin_data），因为 Modal 不能可靠地
                # 把 stdin 传给链式命令。
                encoded_result = base64.b64encode(
                    tool_result.encode("utf-8")
                ).decode("ascii")
                env.execute(
                    f"echo '{encoded_result}' | base64 -d > {quoted_res_file}.tmp"
                    f" && mv {quoted_res_file}.tmp {quoted_res_file}",
                    cwd="/",
                    timeout=60,
                )

                # 删除请求文件
                env.execute(f"rm -f {quoted_req_file}", cwd="/", timeout=5)

        except Exception as e:
            if not stop_event.is_set():
                logger.debug("RPC poll error: %s", e, exc_info=True)

        if not stop_event.is_set():
            stop_event.wait(poll_interval)


def _execute_remote(
    code: str,
    task_id: Optional[str],
    enabled_tools: Optional[List[str]],
) -> str:
    """通过基于文件的 RPC 在远程终端后端上运行脚本。

    脚本和生成的 hermes_tools.py 模块被传送到远程环境，工具调用则由
    一个轮询线程代理，该线程通过请求/响应文件进行通信。
    """

    _cfg = _load_config()
    timeout = _cfg.get("timeout", DEFAULT_TIMEOUT)
    max_tool_calls = _cfg.get("max_tool_calls", DEFAULT_MAX_TOOL_CALLS)

    session_tools = set(enabled_tools) if enabled_tools else set()
    sandbox_tools = frozenset(SANDBOX_ALLOWED_TOOLS & session_tools)
    if not sandbox_tools:
        sandbox_tools = SANDBOX_ALLOWED_TOOLS

    effective_task_id = task_id or "default"
    env, env_type = _get_or_create_env(effective_task_id)

    sandbox_id = uuid.uuid4().hex[:12]
    temp_dir = _env_temp_dir(env)
    sandbox_dir = f"{temp_dir}/hermes_exec_{sandbox_id}"
    quoted_sandbox_dir = shlex.quote(sandbox_dir)
    quoted_rpc_dir = shlex.quote(f"{sandbox_dir}/rpc")

    tool_call_log: list = []
    tool_call_counter = [0]
    exec_start = time.monotonic()
    stop_event = threading.Event()
    rpc_thread = None

    try:
        # 校验远程上是否有 Python 可用
        py_check = env.execute(
            "command -v python3 >/dev/null 2>&1 && echo OK",
            cwd="/", timeout=15,
        )
        if "OK" not in py_check.get("output", ""):
            return json.dumps({
                "status": "error",
                "error": (
                    f"Python 3 is not available in the {env_type} terminal "
                    "environment. Install Python to use execute_code with "
                    "remote backends."
                ),
                "tool_calls_made": 0,
                "duration_seconds": 0,
            })

        # 在远程上创建沙箱目录
        env.execute(
            f"mkdir -p {quoted_rpc_dir}", cwd="/", timeout=10,
        )

        # 生成并传送文件
        tools_src = generate_hermes_tools_module(
            list(sandbox_tools), transport="file",
        )
        _ship_file_to_remote(env, f"{sandbox_dir}/hermes_tools.py", tools_src)
        _ship_file_to_remote(env, f"{sandbox_dir}/script.py", code)

        # 包装一层，使线程继承当前回合的审批上下文 + 回调
        # （见 tools.thread_context）——否则沙箱 RPC 工具调用会丢失审批
        # 路由（#33057）。
        rpc_thread = threading.Thread(
            target=propagate_context_to_thread(_rpc_poll_loop),
            args=(
                env, f"{sandbox_dir}/rpc", effective_task_id,
                tool_call_log, tool_call_counter, max_tool_calls,
                sandbox_tools, stop_event,
            ),
            daemon=True,
        )
        rpc_thread.start()

        # 为脚本构造环境变量前缀
        env_prefix = (
            f"HERMES_RPC_DIR={shlex.quote(f'{sandbox_dir}/rpc')} "
            f"PYTHONDONTWRITEBYTECODE=1"
        )
        tz = os.getenv("HERMES_TIMEZONE", "").strip()
        if tz:
            env_prefix += f" TZ={shlex.quote(tz)}"

        # 在远程后端上执行脚本
        logger.info("Executing code on %s backend (task %s)...",
                     env_type, effective_task_id[:8])
        script_result = env.execute(
            f"cd {quoted_sandbox_dir} && {env_prefix} python3 script.py",
            timeout=timeout,
        )

        stdout_text = script_result.get("output", "")
        exit_code = script_result.get("returncode", -1)
        status = "success"

        # 检查后端返回的超时/中断
        if exit_code == 124:
            status = "timeout"
        elif exit_code == 130:
            status = "interrupted"

    except Exception as exc:
        duration = round(time.monotonic() - exec_start, 2)
        logger.error(
            "execute_code remote failed after %ss with %d tool calls: %s: %s",
            duration, tool_call_counter[0], type(exc).__name__, exc,
            exc_info=True,
        )
        return json.dumps({
            "status": "error",
            "error": str(exc),
            "tool_calls_made": tool_call_counter[0],
            "duration_seconds": duration,
        }, ensure_ascii=False)

    finally:
        # 停止轮询线程
        stop_event.set()
        if rpc_thread is not None:
            rpc_thread.join(timeout=5)

        # 清理远程沙箱目录
        try:
            env.execute(
                f"rm -rf {quoted_sandbox_dir}", cwd="/", timeout=15,
            )
        except Exception:
            logger.debug("Failed to clean up remote sandbox %s", sandbox_dir)

    duration = round(time.monotonic() - exec_start, 2)

    # --- 后处理输出（与本地路径相同）---

    # 按上限截断 stdout
    if len(stdout_text) > MAX_STDOUT_BYTES:
        head_bytes = int(MAX_STDOUT_BYTES * 0.4)
        tail_bytes = MAX_STDOUT_BYTES - head_bytes
        head = stdout_text[:head_bytes]
        tail = stdout_text[-tail_bytes:]
        omitted = len(stdout_text) - len(head) - len(tail)
        stdout_text = (
            head
            + f"\n\n... [OUTPUT TRUNCATED - {omitted:,} chars omitted "
            f"out of {len(stdout_text):,} total] ...\n\n"
            + tail
        )

    # 去除 ANSI 转义序列
    from tools.ansi_strip import strip_ansi
    stdout_text = strip_ansi(stdout_text)

    # 脱敏密钥
    from agent.redact import redact_sensitive_text
    stdout_text = redact_sensitive_text(stdout_text)

    # 构造响应
    result: Dict[str, Any] = {
        "status": status,
        "output": stdout_text,
        "tool_calls_made": tool_call_counter[0],
        "duration_seconds": duration,
    }

    if status == "timeout":
        timeout_msg = f"Script timed out after {timeout}s and was killed."
        result["error"] = timeout_msg
        # 把超时消息放进输出，以便 LLM 总是能把它呈现给用户
        # （见本地路径的注释——同样的理由，#10807）。
        if stdout_text:
            result["output"] = stdout_text + f"\n\n⏰ {timeout_msg}"
        else:
            result["output"] = f"⏰ {timeout_msg}"
        logger.warning(
            "execute_code (remote) timed out after %ss (limit %ss) with %d tool calls",
            duration, timeout, tool_call_counter[0],
        )
    elif status == "interrupted":
        result["output"] = (
            stdout_text + "\n[execution interrupted — user sent a new message]"
        )
    elif exit_code != 0:
        result["status"] = "error"
        result["error"] = f"Script exited with code {exit_code}"

    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def execute_code(
    code: str,
    task_id: Optional[str] = None,
    enabled_tools: Optional[List[str]] = None,
) -> str:
    """
    在沙箱子进程中运行 Python 脚本，并通过 RPC 访问一部分 Hermes 工具。

    根据配置的终端后端，分发到本地（UDS）或远程（基于文件的 RPC）路径。

    参数：
        code:          要执行的 Python 源代码。
        task_id:       用于工具隔离的会话任务 ID（终端环境等）。
        enabled_tools: 当前会话中启用的工具名列表。沙箱获得其与
                       SANDBOX_ALLOWED_TOOLS 的交集。

    返回：
        包含执行结果的 JSON 字符串。
    """
    if not SANDBOX_AVAILABLE:
        return json.dumps({
            "error": "execute_code sandbox is unavailable in this environment. "
                     "Use normal tool calls (terminal, read_file, write_file, ...) instead."
        })

    if not code or not code.strip():
        return tool_error("No code provided.")

    # 分发：远程后端使用基于文件的 RPC，本地使用 UDS
    from tools.terminal_tool import _get_env_config
    env_type = _get_env_config()["env_type"]

    # execute_code 会运行任意 Python 代码（subprocess/os.system/...），这些
    # 代码从不经过 terminal()/DANGEROUS_PATTERNS，因此在任一分发路径派生它
    # 之前，先在这里对整个脚本做守卫。它在调用方（工具执行器）线程中同步
    # 运行，该线程持有会话上下文（#30882）。
    from tools.approval import check_execute_code_guard
    _guard = check_execute_code_guard(code, env_type)
    if not _guard.get("approved", False):
        return json.dumps({
            "status": "error",
            "error": _guard.get("message") or "execute_code blocked by approval guard.",
            "tool_calls_made": 0,
            "duration_seconds": 0,
        }, ensure_ascii=False)

    if env_type != "local":
        return _execute_remote(code, task_id, enabled_tools)

    # --- 本地执行路径（UDS）--- 此行以下保持不变 ---

    # 导入每线程中断检查（协作式取消）
    from tools.interrupt import is_interrupted as _is_interrupted

    # 解析配置
    _cfg = _load_config()
    timeout = _cfg.get("timeout", DEFAULT_TIMEOUT)
    max_tool_calls = _cfg.get("max_tool_calls", DEFAULT_MAX_TOOL_CALLS)

    # 确定沙箱可以调用哪些工具
    session_tools = set(enabled_tools) if enabled_tools else set()
    sandbox_tools = frozenset(SANDBOX_ALLOWED_TOOLS & session_tools)

    if not sandbox_tools:
        sandbox_tools = SANDBOX_ALLOWED_TOOLS

    # --- 搭建含 hermes_tools.py 和 script.py 的临时目录 ---
    tmpdir = tempfile.mkdtemp(prefix="hermes_sandbox_")
    # macOS 上使用 /tmp，以避免过长的 /var/folders/... 路径把
    # Unix 域套接字路径顶到 macOS AF_UNIX 的 104 字节上限之外。
    # Linux 上 tempfile.gettempdir() 已经返回 /tmp。
    #
    # Windows：Python 3.9+ 增加了对 AF_UNIX 的部分支持，但基于文件的
    # 变体在不同 Windows 构建间不稳定（要求 Windows 10 1803+，在某些
    # 配置下仍会失败，且套接字文件不能与脚本位于同一个临时驱动器上）。
    # 回退到环回 TCP——同样的临时端口、同样的 1 连接监听队列、同样的
    # 串行化请求/响应帧格式。生成的客户端从 HERMES_RPC_SOCKET 读取
    # 传输选择器（路径 vs. ``tcp://host:port``）。
    _sock_tmpdir = "/tmp" if sys.platform == "darwin" else tempfile.gettempdir()
    _use_tcp_rpc = _IS_WINDOWS
    if _use_tcp_rpc:
        sock_path = None  # Windows 上不使用；TCP 端点存放在下方
        rpc_endpoint = None  # 在 bind() 之后设置
    else:
        sock_path = os.path.join(_sock_tmpdir, f"hermes_rpc_{uuid.uuid4().hex}.sock")
        rpc_endpoint = sock_path

    tool_call_log: list = []
    tool_call_counter = [0]  # mutable so the RPC thread can increment
    exec_start = time.monotonic()
    server_sock = None
    stop_event = threading.Event()

    try:
        # 写入自动生成的 hermes_tools 模块。
        # Windows 上要求 encoding="utf-8"——桩代码和用户代码都包含
        # 非 ASCII 字符（docstring 中的破折号，以及用户脚本所带的任何字符）。
        # Python 默认的 open() 在 Windows 上使用系统区域设置（通常是
        # cp1252），这会破坏这些字节；子进程随后会以 SyntaxError 导入失败
        # （"'utf-8' codec can't decode byte 0x97 in position ..."），
        # 因为 Python 源文件默认按 UTF-8 解码（PEP 3120）。
        # sandbox_tools 已经是正确的集合（与会话工具取交集，或以
        # SANDBOX_ALLOWED_TOOLS 作为回退——见上方代码）。
        tools_src = generate_hermes_tools_module(list(sandbox_tools))
        with open(os.path.join(tmpdir, "hermes_tools.py"), "w", encoding="utf-8") as f:
            f.write(tools_src)

        # 写入用户脚本
        with open(os.path.join(tmpdir, "script.py"), "w", encoding="utf-8") as f:
            f.write(code)

        # --- 启动 RPC 服务器 ---
        # 两种传输方式：
        #   POSIX：sock_path 上的 AF_UNIX 流套接字，chmod 0600 以实现
        #   仅属主访问。文件系统权限控制着该套接字。
        #   Windows：127.0.0.1 上带临时端口的 AF_INET 流套接字。
        #   没有文件系统权限机制，但仅绑定环回地址意味着只有当前
        #   用户的进程（而非远程）可以连接。HERMES_RPC_SOCKET 被设为
        #   ``tcp://127.0.0.1:<port>``，由生成的客户端解析以选择 AF_INET。
        if _use_tcp_rpc:
            server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server_sock.bind(("127.0.0.1", 0))  # ephemeral port
            _host, _port = server_sock.getsockname()[:2]
            rpc_endpoint = f"tcp://{_host}:{_port}"
        else:
            server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server_sock.bind(sock_path)
            os.chmod(sock_path, 0o600)
        server_sock.listen(1)

        # 包装一层，使线程继承当前回合的审批上下文 + 回调
        # （见 tools.thread_context）——否则网关沙箱的工具调用会静默地
        # 自动批准危险命令（#33057、#30882）。
        rpc_thread = threading.Thread(
            target=propagate_context_to_thread(_rpc_server_loop),
            args=(
                server_sock, task_id, tool_call_log,
                tool_call_counter, max_tool_calls, sandbox_tools, stop_event,
            ),
            daemon=True,
        )
        rpc_thread.start()

        # --- 派生子进程 ---
        # 为子进程构造一个最小化的环境。我们刻意排除 API 密钥和令牌，
        # 以防 LLM 生成的脚本泄露凭据。子进程通过 RPC 访问工具，而非直连 API。
        # 例外：由已加载技能声明的环境变量（通过 env_passthrough 注册表）
        # 或用户在 config.yaml（terminal.env_passthrough）中显式允许的变量
        # 会透传。在 Windows 上，一小份操作系统必需的允许清单
        # （SYSTEMROOT、WINDIR、COMSPEC 等）也会透传——缺少这些，子进程
        # 无法创建套接字或派生子进程。规则见 ``_scrub_child_env``。
        child_env = _scrub_child_env(os.environ)
        child_env["HERMES_RPC_SOCKET"] = rpc_endpoint
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        # 强制子进程的 stdio 和默认文件编码为 UTF-8。
        #
        # 否则在 Windows 上，sys.stdout 会绑定到控制台代码页
        # （美式区域安装下为 cp1252），任何执行如下脚本的代码都会崩溃：
        # ``print("café")`` 或 ``print("→")`` 都会崩溃，报错：
        #
        #   UnicodeEncodeError: 'charmap' codec can't encode character
        #   '\u2192' in position N: character maps to <undefined>
        #
        # PYTHONIOENCODING 修复 sys.stdin/stdout/stderr。
        # PYTHONUTF8=1 启用 "UTF-8 模式"（PEP 540），额外地让 ``open()``
        # 的默认编码变为 UTF-8，这样用户脚本在未指定 encoding= 写文件时
        # 也能正常工作。
        #
        # 在 POSIX 上这两个值通常已经与区域默认一致，因此设置它们是
        # 无害的双保险，适用于 C/POSIX 区域的环境（容器、最小化基础镜像）。
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
        # 确保 hermes-agent 根目录在沙箱中可导入，使仓库根目录模块对
        # 子脚本可用。我们还把暂存 tmpdir 放到最前面，这样即使子进程的
        # CWD 不是 tmpdir（project 模式），``from hermes_tools import ...``
        # 也能正确解析。
        _hermes_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _existing_pp = child_env.get("PYTHONPATH", "")
        _pp_parts = [tmpdir, _hermes_root]
        if _existing_pp:
            _pp_parts.append(_existing_pp)
        child_env["PYTHONPATH"] = os.pathsep.join(_pp_parts)
        # 注入用户配置的时区，使沙箱代码中的 datetime.now() 反映正确的
        # 挂钟时间。只设置 TZ——HERMES_TIMEZONE 是 Hermes 的内部设置，
        # 不得泄露到子进程中。
        _tz_name = os.getenv("HERMES_TIMEZONE", "").strip()
        if _tz_name:
            child_env["TZ"] = _tz_name
        child_env.pop("HERMES_TIMEZONE", None)

        from hermes_constants import apply_subprocess_home_env
        apply_subprocess_home_env(child_env)

        # 根据 execute_code 模式解析解释器 + CWD。
        #   - strict：当前的行为（sys.executable + tmpdir 作为 CWD）。
        #   - project：用户的 venv python + 会话工作目录，使 pandas 等
        #              项目依赖和用户文件能正确解析。
        # 两种模式下，环境清洗和工具白名单完全一致地生效。
        _mode = _get_execution_mode()
        _child_python = _resolve_child_python(_mode)
        _child_cwd = _resolve_child_cwd(_mode, tmpdir)
        _script_path = os.path.join(tmpdir, "script.py")

        proc = subprocess.Popen(
            [_child_python, _script_path],
            cwd=_child_cwd,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            preexec_fn=None if _IS_WINDOWS else os.setsid,
            creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0,
        )

        # --- 轮询循环：监视退出、超时和中断 ---
        deadline = time.monotonic() + timeout
        stderr_chunks: list = []

        # 后台读取器，避免管道缓冲区死锁。
        # 对 stdout 采用 head+tail 策略：保留前 HEAD_BYTES 和最后
        # TAIL_BYTES 的滚动窗口，确保最终的 print() 输出不会丢失。
        # stderr 只保留 head（错误出现得早）。
        _STDOUT_HEAD_BYTES = int(MAX_STDOUT_BYTES * 0.4)   # 40% 头部
        _STDOUT_TAIL_BYTES = MAX_STDOUT_BYTES - _STDOUT_HEAD_BYTES  # 60% 尾部

        def _drain(pipe, chunks, max_bytes):
            """简单的仅保留头部的读取（用于 stderr）。"""
            total = 0
            try:
                while True:
                    data = pipe.read(4096)
                    if not data:
                        break
                    if total < max_bytes:
                        keep = max_bytes - total
                        chunks.append(data[:keep])
                    total += len(data)
            except (ValueError, OSError) as e:
                logger.debug("Error reading process output: %s", e, exc_info=True)

        stdout_total_bytes = [0]  # 可变引用，记录已看到的总字节数

        def _drain_head_tail(pipe, head_chunks, tail_chunks, head_bytes, tail_bytes, total_ref):
            """读取 stdout，同时保留头部和尾部数据。"""
            head_collected = 0
            from collections import deque
            tail_buf = deque()
            tail_collected = 0
            try:
                while True:
                    data = pipe.read(4096)
                    if not data:
                        break
                    total_ref[0] += len(data)
                    # 先填满头部缓冲区
                    if head_collected < head_bytes:
                        keep = min(len(data), head_bytes - head_collected)
                        head_chunks.append(data[:keep])
                        head_collected += keep
                        data = data[keep:]  # 剩余部分进入尾部
                        if not data:
                            continue
                    # 头部之外的所有数据进入滚动尾部缓冲区
                    tail_buf.append(data)
                    tail_collected += len(data)
                    # 逐出旧的尾部数据以不超过 tail_bytes 预算
                    while tail_collected > tail_bytes and tail_buf:
                        oldest = tail_buf.popleft()
                        tail_collected -= len(oldest)
            except (ValueError, OSError):
                pass
            # 把最终的尾部转移到输出列表
            tail_chunks.extend(tail_buf)

        stdout_head_chunks: list = []
        stdout_tail_chunks: list = []

        stdout_reader = threading.Thread(
            target=_drain_head_tail,
            args=(proc.stdout, stdout_head_chunks, stdout_tail_chunks,
                  _STDOUT_HEAD_BYTES, _STDOUT_TAIL_BYTES, stdout_total_bytes),
            daemon=True
        )
        stderr_reader = threading.Thread(
            target=_drain, args=(proc.stderr, stderr_chunks, MAX_STDERR_BYTES), daemon=True
        )
        stdout_reader.start()
        stderr_reader.start()

        status = "success"
        _activity_state = {
            "last_touch": time.monotonic(),
            "start": exec_start,
        }
        try:
            from tools.environments.base import touch_activity_if_due
        except Exception:
            touch_activity_if_due = None
        poll_interval = 0.005
        while proc.poll() is None:
            if _is_interrupted():
                _kill_process_group(proc)
                status = "interrupted"
                break
            now = time.monotonic()
            if now > deadline:
                _kill_process_group(proc, escalate=True)
                status = "timeout"
                break
            # 周期性触碰活动状态，使网关的非活动超时不会在长时间代码
            # 执行期间杀死 agent（#10807）。
            if touch_activity_if_due is not None:
                try:
                    touch_activity_if_due(_activity_state, "execute_code running")
                except Exception:
                    pass
            try:
                proc.wait(timeout=min(poll_interval, max(0.0, deadline - now)))
            except subprocess.TimeoutExpired:
                pass
            poll_interval = min(0.2, poll_interval * 1.5)

        # 等待读取器完成读取
        stdout_reader.join(timeout=3)
        stderr_reader.join(timeout=3)

        stdout_head = b"".join(stdout_head_chunks).decode("utf-8", errors="replace")
        stdout_tail = b"".join(stdout_tail_chunks).decode("utf-8", errors="replace")
        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")

        # 用 head+tail 截断拼装 stdout
        total_stdout = stdout_total_bytes[0]
        if total_stdout > MAX_STDOUT_BYTES and stdout_tail:
            omitted = total_stdout - len(stdout_head) - len(stdout_tail)
            truncated_notice = (
                f"\n\n... [OUTPUT TRUNCATED - {omitted:,} chars omitted "
                f"out of {total_stdout:,} total] ...\n\n"
            )
            stdout_text = stdout_head + truncated_notice + stdout_tail
        else:
            stdout_text = stdout_head + stdout_tail

        exit_code = proc.returncode if proc.returncode is not None else -1
        duration = round(time.monotonic() - exec_start, 2)

        # 等待 RPC 线程结束
        stop_event.set()
        server_sock.close()  # 打断 accept() 使线程尽快退出
        server_sock = None  # 防止在 finally 中重复关闭
        rpc_thread.join(timeout=3)

        # 去除 ANSI 转义序列，使模型永远不会看到终端格式
        # ——避免它把转义序列复制到文件写入中。
        from tools.ansi_strip import strip_ansi
        stdout_text = strip_ansi(stdout_text)
        stderr_text = strip_ansi(stderr_text)

        # 从沙箱输出中脱敏密钥（API 密钥、令牌等）。
        # 沙箱的环境变量过滤器（第 434-454 行）阻断了 os.environ 访问，
        # 但脚本仍可从磁盘读取密钥（例如 open('~/.hermes/.env')）。
        # 这一步确保泄露的密钥永远不会进入模型上下文。
        from agent.redact import redact_sensitive_text
        stdout_text = redact_sensitive_text(stdout_text)
        stderr_text = redact_sensitive_text(stderr_text)

        # 构造响应
        result: Dict[str, Any] = {
            "status": status,
            "output": stdout_text,
            "tool_calls_made": tool_call_counter[0],
            "duration_seconds": duration,
        }

        if status == "timeout":
            timeout_msg = f"Script timed out after {timeout}s and was killed."
            result["error"] = timeout_msg
            # 把超时消息放进输出，以便 LLM 总是能把它呈现给用户。
            # 当输出为空时，模型常常把结果当作 "什么也没发生" 并产生空响应，
            # 而网关的流消费者会静默丢弃它（#10807）。
            if stdout_text:
                result["output"] = stdout_text + f"\n\n⏰ {timeout_msg}"
            else:
                result["output"] = f"⏰ {timeout_msg}"
            logger.warning(
                "execute_code timed out after %ss (limit %ss) with %d tool calls",
                duration, timeout, tool_call_counter[0],
            )
        elif status == "interrupted":
            result["output"] = stdout_text + "\n[execution interrupted — user sent a new message]"
        elif exit_code != 0:
            result["status"] = "error"
            result["error"] = stderr_text or f"Script exited with code {exit_code}"
            # 把 stderr 放进输出，以便 LLM 看到回溯信息
            if stderr_text:
                result["output"] = stdout_text + "\n--- stderr ---\n" + stderr_text

        return json.dumps(result, ensure_ascii=False)

    except Exception as exc:
        duration = round(time.monotonic() - exec_start, 2)
        logger.error(
            "execute_code failed after %ss with %d tool calls: %s: %s",
            duration,
            tool_call_counter[0],
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        return json.dumps({
            "status": "error",
            "error": str(exc),
            "tool_calls_made": tool_call_counter[0],
            "duration_seconds": duration,
        }, ensure_ascii=False)

    finally:
        # 清理临时目录和套接字
        if server_sock is not None:
            try:
                server_sock.close()
            except OSError as e:
                logger.debug("Server socket close error: %s", e)
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
        try:
            # 只有 UDS 有需要 unlink 的文件系统套接字；TCP 套接字
            # 已由上方的 server_sock.close() 释放。
            if sock_path:
                os.unlink(sock_path)
        except OSError:
            pass  # 已清理或从未创建


def _kill_process_group(proc, escalate: bool = False):
    """杀死子进程及其整个进程树（通过 psutil 跨平台实现）。"""
    import psutil
    try:
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        try:
            parent.terminate()
        except psutil.NoSuchProcess:
            pass
    except psutil.NoSuchProcess:
        pass
    except (PermissionError, OSError) as e:
        logger.debug("Could not terminate process tree: %s", e, exc_info=True)
        try:
            proc.kill()
        except Exception as e2:
            logger.debug("Could not kill process: %s", e2, exc_info=True)

    if escalate:
        # 给进程 5 秒时间在 SIGTERM 后退出，否则 SIGKILL
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                parent = psutil.Process(proc.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                try:
                    parent.kill()
                except psutil.NoSuchProcess:
                    pass
            except psutil.NoSuchProcess:
                pass
            except (PermissionError, OSError) as e:
                logger.debug("Could not kill process tree: %s", e, exc_info=True)
                try:
                    proc.kill()
                except Exception as e2:
                    logger.debug("Could not kill process: %s", e2, exc_info=True)


def _load_config() -> dict:
    """在不导入交互式 CLI 的情况下加载 code_execution 配置。

    这个辅助函数在工具发现阶段构建模块级 execute_code schema 时被调用。
    在这里 import ``cli`` 会把 prompt_toolkit/Rich 以及经典 REPL 的很大
    一部分拉到每条 agent 启动路径上，包括根本用不到它的 ``hermes --tui``。
    改为读取轻量级的原始配置；配置层已按 (mtime, size) 缓存，缺失的键
    会干净地回退到 DEFAULT_EXECUTION_MODE。
    """
    try:
        from hermes_cli.config import read_raw_config

        cfg = read_raw_config().get("code_execution", {})
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 执行模式解析（strict vs project）
# ---------------------------------------------------------------------------

# code_execution.mode 的合法取值。保留为模块常量，便于测试和配置层
# 引用这套规范集合。
EXECUTION_MODES = ("project", "strict")
DEFAULT_EXECUTION_MODE = "project"


def _get_execution_mode() -> str:
    """返回当前生效的 execute_code 模式——'project' 或 'strict'。

    从 config.yaml 读取 ``code_execution.mode``；非法值回退到
    ``DEFAULT_EXECUTION_MODE``（'project'）并记录一条警告日志。

    模式语义：
      - ``project``（默认）：脚本在会话工作目录中、用活动虚拟环境的
        python 运行，使项目依赖（pandas、torch、项目包）和文件自然解析。
      - ``strict``：脚本在隔离的临时目录中、用 ``sys.executable``
        （hermes-agent 的 python）运行。可复现且解释器保证可用，但项目
        依赖和相对路径无法解析。

    两种模式下，环境清洗和工具白名单完全一致地生效。
    """
    cfg_value = str(_load_config().get("mode", DEFAULT_EXECUTION_MODE)).strip().lower()
    if cfg_value in EXECUTION_MODES:
        return cfg_value
    logger.warning(
        "Ignoring code_execution.mode=%r (expected one of %s), falling back to %r",
        cfg_value, EXECUTION_MODES, DEFAULT_EXECUTION_MODE,
    )
    return DEFAULT_EXECUTION_MODE


@functools.lru_cache(maxsize=32)
def _is_usable_python(python_path: str) -> bool:
    """检查候选 Python 解释器是否可用于 execute_code。

    要求 Python 3.8+（RPC 桩所需的 f-string 和标准库模块）。
    结果被缓存，避免每次 execute_code 调用都 fork 一个子进程。
    """
    try:
        result = subprocess.run(
            [python_path, "-c",
             "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)"],
            timeout=5,
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0,
            stdin=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return False


def _resolve_child_python(mode: str) -> str:
    """为 execute_code 子进程挑选 Python 解释器。

    在 ``strict`` 模式下，始终使用 ``sys.executable``——保证可用，并使
    行为在不同会话间完全可复现。

    在 ``project`` 模式下，优先使用用户活动 virtualenv/conda 环境的
    python，使 ``import pandas`` 等能正常工作。若未检测到 venv、候选
    二进制缺失/不可执行、或未通过 Python 3.8+ 版本检查，则回退到
    ``sys.executable``。
    """
    if mode != "project":
        return sys.executable

    if _IS_WINDOWS:
        exe_names = ("python.exe", "python3.exe")
        subdirs = ("Scripts",)
    else:
        exe_names = ("python", "python3")
        subdirs = ("bin",)

    for var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        root = os.environ.get(var, "").strip()
        if not root:
            continue
        for subdir in subdirs:
            for exe in exe_names:
                candidate = os.path.join(root, subdir, exe)
                if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
                    continue
                if _is_usable_python(candidate):
                    return candidate
                # 找到了解释器但未通过版本检查——
                # 记录一次日志并回退到 sys.executable。
                logger.info(
                    "execute_code: skipping %s=%s (Python version < 3.8 or broken). "
                    "Using sys.executable instead.", var, candidate,
                )
                return sys.executable

    return sys.executable


def _resolve_child_cwd(mode: str, staging_dir: str) -> str:
    """解析 execute_code 子进程的工作目录。

    - ``strict``：暂存 tmpdir（当前行为）。
    - ``project``：会话的 TERMINAL_CWD（与终端工具相同），若 TERMINAL_CWD
      未设置或不是真实目录则用 ``os.getcwd()``。最后回退到暂存 tmpdir，
      确保永远不会用不存在的 cwd 调用 Popen。
    """
    if mode != "project":
        return staging_dir
    raw = os.environ.get("TERMINAL_CWD", "").strip()
    if raw:
        expanded = os.path.expanduser(raw)
        if os.path.isdir(expanded):
            return expanded
    here = os.getcwd()
    if os.path.isdir(here):
        return here
    return staging_dir


# ---------------------------------------------------------------------------
# OpenAI 函数调用 Schema
# ---------------------------------------------------------------------------

# execute_code 描述中各工具的文档行。
# 顺序与规范展示顺序一致。
_TOOL_DOC_LINES = [
    ("web_search",
     "  web_search(query: str, limit: int = 5) -> dict\n"
     "    Returns {\"data\": {\"web\": [{\"url\", \"title\", \"description\"}, ...]}}"),
    ("web_extract",
     "  web_extract(urls: list[str]) -> dict\n"
     "    Returns {\"results\": [{\"url\", \"title\", \"content\", \"error\"}, ...]} where content is markdown"),
    ("read_file",
     "  read_file(path: str, offset: int = 1, limit: int = 500) -> dict\n"
     "    Lines are 1-indexed. Returns {\"content\": \"...\", \"total_lines\": N}"),
    ("write_file",
     "  write_file(path: str, content: str) -> dict\n"
     "    Always overwrites the entire file."),
    ("search_files",
     "  search_files(pattern: str, target=\"content\", path=\".\", file_glob=None, limit=50) -> dict\n"
     "    target: \"content\" (search inside files) or \"files\" (find files by name). Returns {\"matches\": [...]}"),
    ("patch",
     "  patch(path: str, old_string: str, new_string: str, replace_all: bool = False) -> dict\n"
     "    Replaces old_string with new_string in the file."),
    ("terminal",
     "  terminal(command: str, timeout=None, workdir=None) -> dict\n"
     "    Foreground only (no background/pty). Returns {\"output\": \"...\", \"exit_code\": N}"),
]


def build_execute_code_schema(enabled_sandbox_tools: set = None,
                              mode: str = None) -> dict:
    """构建 execute_code schema，其描述中只列出已启用的工具。

    当通过 ``hermes tools`` 禁用某些工具（例如关闭 web）时，schema 描述
    不应再提及 web_search / web_extract——否则模型会以为它们可用并不断尝试。

    ``mode`` 控制描述中关于工作目录的句子：
      - ``'strict'``：脚本在临时目录中运行（而非会话的 CWD）
      - ``'project'``（默认）：脚本在会话的 CWD 中、用活动 venv 的
        python 运行
    若 ``mode`` 为 None，则读取当前 ``code_execution.mode`` 配置。
    """
    if enabled_sandbox_tools is None:
        enabled_sandbox_tools = SANDBOX_ALLOWED_TOOLS
    if mode is None:
        mode = _get_execution_mode()

    # 只为已启用的工具构建文档行
    tool_lines = "\n".join(
        doc for name, doc in _TOOL_DOC_LINES if name in enabled_sandbox_tools
    )

    # 从已启用的工具中构建示例 import 列表
    import_examples = [n for n in ("web_search", "terminal") if n in enabled_sandbox_tools]
    if not import_examples:
        import_examples = sorted(enabled_sandbox_tools)[:2]
    if import_examples:
        import_str = ", ".join(import_examples) + ", ..."
    else:
        import_str = "..."

    # 模式相关的 CWD 指引。project 模式是默认值，与 terminal() 的
    # 文件系统/解释器一致；strict 模式保留隔离的临时目录暂存和
    # hermes-agent 自带的 python。
    if mode == "strict":
        cwd_note = (
            "Scripts run in their own temp dir, not the session's CWD — use absolute paths "
            "(os.path.expanduser('~/.hermes/.env')) or terminal()/read_file() for user files."
        )
    else:
        cwd_note = (
            "Scripts run in the session's working directory with the active venv's python, "
            "so project deps (pandas, etc.) and relative paths work like in terminal()."
        )

    description = (
        "Run a Python script that can call Hermes tools programmatically. "
        "Use this when you need 3+ tool calls with processing logic between them, "
        "need to filter/reduce large tool outputs before they enter your context, "
        "need conditional branching (if X then Y else Z), or need to loop "
        "(fetch N pages, process N files, retry on failure).\n\n"
        "Use normal tool calls instead when: single tool call with no processing, "
        "you need to see the full result and apply complex reasoning, "
        "or the task requires interactive user input.\n\n"
        f"Available via `from hermes_tools import ...`:\n\n"
        f"{tool_lines}\n\n"
        "Limits: 5-minute timeout, 50KB stdout cap, max 50 tool calls per script. "
        "terminal() is foreground-only (no background or pty).\n\n"
        f"{cwd_note}\n\n"
        "Print your final result to stdout. Use Python stdlib (json, re, math, csv, "
        "datetime, collections, etc.) for processing between tool calls.\n\n"
        "Also available (no import needed — built into hermes_tools):\n"
        "  json_parse(text: str) — json.loads with strict=False; use for terminal() output with control chars\n"
        "  shell_quote(s: str) — shlex.quote(); use when interpolating dynamic strings into shell commands\n"
        "  retry(fn, max_attempts=3, delay=2) — retry with exponential backoff for transient failures"
    )

    return {
        "name": "execute_code",
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "Python code to execute. Import tools with "
                        f"`from hermes_tools import {import_str}` "
                        "and print your final result to stdout."
                    ),
                },
            },
            "required": ["code"],
        },
    }


# 注册时使用的默认 schema（列出所有沙箱工具，使用当前配置的模式）。
# model_tools.py 无论如何都会按会话重建。
EXECUTE_CODE_SCHEMA = build_execute_code_schema()


# --- 注册表 ---
from tools.registry import registry, tool_error

registry.register(
    name="execute_code",
    toolset="code_execution",
    schema=EXECUTE_CODE_SCHEMA,
    handler=lambda args, **kw: execute_code(
        code=args.get("code", ""),
        task_id=kw.get("task_id"),
        enabled_tools=kw.get("enabled_tools")),
    check_fn=check_sandbox_requirements,
    emoji="🐍",
    max_result_size_chars=100_000,
)
