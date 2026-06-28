#!/usr/bin/env python3
"""
MCP（Model Context Protocol，模型上下文协议）客户端支持

通过 stdio、HTTP/StreamableHTTP 或 SSE 传输连接到外部 MCP 服务器，
发现其工具，并将其注册到 hermes-agent 工具注册表中，使 agent 能像调用
任何内置工具一样调用它们。

配置从 ~/.hermes/config.yaml 的 ``mcp_servers`` 键下读取。
``mcp`` Python 包是可选的——若未安装，本模块为空操作并记录一条 debug 日志。

配置示例::

    mcp_servers:
      filesystem:
        command: "npx"
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        env: {}
        timeout: 120         # 单次工具调用超时（秒），默认 300
        connect_timeout: 60  # 初始连接超时（秒），默认 60
        keepalive_interval: 10  # 存活探测频率（秒），默认
                                # 180。对于快速回收空闲会话的服务器
                                # （例如 Unreal Engine 编辑器 MCP，约 15s），
                                # 需设置小于服务器会话 TTL 的值。下限为 5s。
      github:
        command: "npx"
        args: ["-y", "@modelcontextprotocol/server-github"]
        env:
          GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_..."
        supports_parallel_tool_calls: true  # 来自该服务器的工具可以并发执行
      remote_api:
        url: "https://my-mcp-server.example.com/mcp"
        headers:
          Authorization: "Bearer sk-..."
        timeout: 180
      searxng:
        url: "http://localhost:8000/sse"
        transport: sse       # 使用 SSE 传输而非 Streamable HTTP
        timeout: 180
        connect_timeout: 10
        command: "npx"
        args: ["-y", "analysis-server"]
        sampling:                    # 服务器发起的 LLM 请求
          enabled: true              # 默认：true
          model: "gemini-3-flash"    # 覆盖模型（可选）
          max_tokens_cap: 4096       # 每次请求的最大 token 数
          timeout: 30                # LLM 调用超时（秒）
          max_rpm: 10                # 每分钟最大请求数
          allowed_models: []         # 模型白名单（空表示全部允许）
          max_tool_rounds: 5         # 工具循环上限（0 表示禁用）
          log_level: "info"          # 审计日志详细度

功能特性：
    - stdio 传输（command + args）与 HTTP/StreamableHTTP 传输（url）
    - SSE 传输（transport: sse），用于使用 SSE 协议的 MCP 服务器
    - 带指数退避的自动重连（最多重试 5 次）
    - 对 stdio 子进程进行环境变量过滤（安全措施）
    - 在返回给 LLM 的错误消息中剥离凭据
    - 可按服务器配置工具调用与连接的超时时间
    - 线程安全架构，配备专用后台事件循环
    - Sampling 支持：MCP 服务器可通过 sampling/createMessage 请求 LLM 补全
      （支持文本和工具使用响应）
    - 并行工具调用可选开启：按服务器的 ``supports_parallel_tool_calls``
      标志允许同一服务器的工具并发执行

架构：
    一个专用的后台事件循环（_mcp_loop）运行在守护线程中。
    每个 MCP 服务器作为该循环上一个长期存活的 asyncio Task 运行，
    保持其传输上下文存活。工具调用协程通过
    ``run_coroutine_threadsafe()`` 调度到该循环上。

    关闭时，每个服务器 Task 会收到退出其 ``async with`` 块的信号，
    确保 anyio 的取消作用域清理发生在打开连接的*同一个* Task 中
    （anyio 的要求）。

线程安全：
    _servers 和 _mcp_loop/_mcp_thread 同时被 MCP 后台线程和调用方线程
    访问。所有修改都受 _lock 保护，因此无论是否存在 GIL（例如 Python 3.13+
    的自由线程），代码都是安全的。
"""

import asyncio
import contextvars
import concurrent.futures
import inspect
import json
import logging
import math
import os
import re
import shutil
import sys
import threading
import time
from typing import Callable
from datetime import datetime
from typing import Any, Coroutine, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stdio 子进程 stderr 重定向
# ---------------------------------------------------------------------------
#
# MCP SDK 的 ``stdio_client(server, errlog=sys.stderr)`` 默认把子进程的
# stderr 流指向父进程的真实 stderr，即用户的 TTY。这意味着我们在启动时
# 拉起的任何 MCP 服务器（FastMCP 横幅、slack-mcp-server 的 JSON 启动日志
# 等）都会在 prompt_toolkit / Rich 渲染 TUI 时直接写到终端上——这会破坏
# 显示内容，甚至可能卡死会话。
#
# 因此我们把每个 stdio MCP 子进程的 stderr 重定向到一个按 profile 共享的
# 日志文件（~/.hermes/logs/mcp-stderr.log），并标注服务器名，以便单个服务
# 器仍然可调试。
#
# 如果打开日志文件因任何原因失败，则回退到 os.devnull。

_mcp_stderr_log_fh: Optional[Any] = None
_mcp_stderr_log_lock = threading.Lock()


def _get_mcp_stderr_log() -> Any:
    """返回一个共享的、以追加模式打开的文件句柄，用于 MCP 子进程的 stderr。

    每个进程只打开一次，并被所有 stdio 服务器复用。必须拥有真实的 OS 层
    文件描述符（``fileno()``），因为 asyncio 的子进程机制会把子进程的
    stderr 直接接到该 fd 上。若打开日志文件失败，则回退到
    ``/dev/null``。
    """
    global _mcp_stderr_log_fh
    with _mcp_stderr_log_lock:
        if _mcp_stderr_log_fh is not None:
            return _mcp_stderr_log_fh
        try:
            from hermes_constants import get_hermes_home
            log_dir = get_hermes_home() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "mcp-stderr.log"
            # 行缓冲，保证服务器输出尽快落盘；errors=
            # "replace" 能容忍行为异常的服务器输出的乱码二进制内容。
            fh = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
            # 健全性检查：在正式启用前确认能拿到真实的 fd。
            fh.fileno()
            _mcp_stderr_log_fh = fh
        except Exception as exc:  # pragma: no cover — best-effort fallback
            logger.debug("Failed to open MCP stderr log, using devnull: %s", exc)
            try:
                _mcp_stderr_log_fh = open(os.devnull, "w", encoding="utf-8")
            except Exception:
                # 最后手段：真实 stderr。对 TUI 用户不理想，但与修复前的
                # 行为一致。
                _mcp_stderr_log_fh = sys.stderr
        return _mcp_stderr_log_fh


def _write_stderr_log_header(server_name: str) -> None:
    """在启动服务器之前写入一段人类可读的会话标记。

    让运维人员在共享的 ``mcp-stderr.log`` 文件中能定位到每个服务器的
    输出，而无需逐行加前缀（那需要 pipe + 读取线程，会让关闭流程变复杂）。
    """
    fh = _get_mcp_stderr_log()
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fh.write(f"\n===== [{ts}] starting MCP server '{server_name}' =====\n")
        fh.flush()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 优雅导入——MCP SDK 是可选依赖
# ---------------------------------------------------------------------------

_MCP_AVAILABLE = False
_MCP_HTTP_AVAILABLE = False
_MCP_SAMPLING_TYPES = False
_MCP_NOTIFICATION_TYPES = False
_MCP_ELICITATION_TYPES = False
_MCP_MESSAGE_HANDLER_SUPPORTED = False
# 针对未导出 LATEST_PROTOCOL_VERSION 的 SDK 构建的保守回退值。
# Streamable HTTP 在 2025-03-26 引入，因此即便在较旧但仍受支持的 SDK 版本
# 上，该值对 HTTP 传输路径依然有效。
LATEST_PROTOCOL_VERSION = "2025-03-26"
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    _MCP_AVAILABLE = True
    try:
        from mcp.client.streamable_http import streamablehttp_client
        _MCP_HTTP_AVAILABLE = True
    except ImportError:
        _MCP_HTTP_AVAILABLE = False
    # 优先使用非弃用的 API（mcp >= 1.24.0）；对较旧的 SDK 版本回退到
    # 已弃用的封装。
    try:
        from mcp.client.streamable_http import streamable_http_client
        _MCP_NEW_HTTP = True
    except ImportError:
        _MCP_NEW_HTTP = False
    try:
        from mcp.types import LATEST_PROTOCOL_VERSION
    except ImportError:
        logger.debug("mcp.types.LATEST_PROTOCOL_VERSION not available -- using fallback protocol version")
    # SSE 传输客户端（用于使用 SSE 传输而非 Streamable HTTP 的 MCP 服务器）
    try:
        from mcp.client.sse import sse_client
    except ImportError:
        sse_client = None
        logger.debug("mcp.client.sse.sse_client not available -- SSE transport disabled")
    # Sampling 类型——单独分开，以免较旧的 SDK 版本破坏 MCP 支持
    try:
        from mcp.types import (
            CreateMessageResult,
            CreateMessageResultWithTools,
            ErrorData,
            SamplingCapability,
            SamplingToolsCapability,
            TextContent,
            ToolUseContent,
        )
        _MCP_SAMPLING_TYPES = True
    except ImportError:
        logger.debug("MCP sampling types not available -- sampling disabled")
    # Elicitation 类型——出于与 sampling 相同的原因单独门控。
    # 在 mcp Python SDK 1.11.0（2025 年 7 月）中引入；服务器通过 elicitation
    # 在工具调用过程中向客户端请求结构化输入（例如支付授权）。类型缺失只会
    # 禁用该特性，其余功能照常工作。
    try:
        from mcp.types import ElicitRequestParams, ElicitResult
        _MCP_ELICITATION_TYPES = True
    except ImportError:
        logger.debug("MCP elicitation types not available -- elicitation disabled")
    # 用于动态工具发现的通知类型（tools/list_changed）
    try:
        from mcp.types import (
            ServerNotification,
            ToolListChangedNotification,
            PromptListChangedNotification,
            ResourceListChangedNotification,
        )
        _MCP_NOTIFICATION_TYPES = True
    except ImportError:
        logger.debug("MCP notification types not available -- dynamic tool discovery disabled")
except ImportError:
    logger.debug("mcp package not installed -- MCP tool support disabled")


def _check_message_handler_support() -> bool:
    """检查 ClientSession 是否接受 ``message_handler`` 关键字参数。

    通过检查构造函数签名来兼容不支持通知处理器的较旧 MCP SDK 版本。
    """
    if not _MCP_AVAILABLE:
        return False
    try:
        return "message_handler" in inspect.signature(ClientSession).parameters
    except (TypeError, ValueError):
        return False


_MCP_MESSAGE_HANDLER_SUPPORTED = _check_message_handler_support()
if _MCP_AVAILABLE and not _MCP_MESSAGE_HANDLER_SUPPORTED:
    logger.debug("MCP SDK does not support message_handler -- dynamic tool discovery disabled")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_DEFAULT_TOOL_TIMEOUT = 300      # 工具调用的秒数
_DEFAULT_CONNECT_TIMEOUT = 60    # 每个服务器初始连接的秒数
_MAX_RECONNECT_RETRIES = 5
_MAX_INITIAL_CONNECT_RETRIES = 3 # 首次连接尝试的重试次数
_MAX_BACKOFF_SECONDS = 60

# HTTP/SSE 会话的存活探测频率。MCP 规范允许服务器以任意 TTL 过期空闲会话
# （Streamable HTTP 的 "Session Management"），因此希望会话在空闲期存活的
# 客户端必须以快于该 TTL 的频率刷新。默认值适合较长的 LB/NAT 空闲窗口
# （通常为 300-600s）；会话 TTL 较短的服务器（例如 Unreal Engine 的编辑器
# MCP，约 15s）需要在配置中设置更小的 ``keepalive_interval``，否则每次空闲
# 工具调用都会落到一个已死的会话上，并付出完整的重连代价。下限值可防止
# 误配的过小间隔让存活探测忙循环。
_DEFAULT_KEEPALIVE_INTERVAL = 180  # 两次存活探测之间的秒数
_MIN_KEEPALIVE_INTERVAL = 5        # 已配置间隔的钳位下限

# 可以安全传给 stdio 子进程的环境变量
_SAFE_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR",
})

_SAFE_ENV_KEYS_CASE_INSENSITIVE = frozenset({
    # Windows 进程/位置变量。这些变量是启动器类工具（例如 Docker Desktop
    # 的 MCP 插件发现）所需要的，且不携带密钥。
    "ALLUSERSPROFILE",
    "APPDATA",
    "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)",
    "COMMONPROGRAMW6432",
    "COMPUTERNAME",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "PUBLIC",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERDOMAIN",
    "USERNAME",
    "USERPROFILE",
    "WINDIR",
})

# 用于从错误消息中剥离的凭据模式正则
_CREDENTIAL_PATTERN = re.compile(
    r"(?:"
    r"ghp_[A-Za-z0-9_]{1,255}"           # GitHub PAT
    r"|sk-[A-Za-z0-9_]{1,255}"           # OpenAI 风格的 key
    r"|Bearer\s+\S+"                      # Bearer token
    r"|token=[^\s&,;\"']{1,255}"         # token=...
    r"|key=[^\s&,;\"']{1,255}"           # key=...
    r"|API_KEY=[^\s&,;\"']{1,255}"       # API_KEY=...
    r"|password=[^\s&,;\"']{1,255}"      # password=...
    r"|secret=[^\s&,;\"']{1,255}"        # secret=...
    r")",
    re.IGNORECASE,
)

# 预编译的 ${VAR_NAME} 风格环境变量插值正则。
# 支持变量名中的任意非 } 字符（连字符、点号等），
# 因此形如 MY-VAR 或 my.var 这样的提供者能正常工作。
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


# ---------------------------------------------------------------------------
# 安全相关辅助函数
# ---------------------------------------------------------------------------

def _build_safe_env(user_env: Optional[dict]) -> dict:
    """为 stdio 子进程构建一个经过过滤的环境变量字典。

    只从当前进程环境中放行安全的基础变量（PATH、HOME 等）和 XDG_*
    变量，再加上用户在服务器配置中显式指定的变量。

    这样可以避免把 API key、token 或凭据等密钥意外泄漏给 MCP 服务器子进程。
    """
    env = {}
    for key, value in os.environ.items():
        if (
            key in _SAFE_ENV_KEYS
            or key.upper() in _SAFE_ENV_KEYS_CASE_INSENSITIVE
            or key.startswith("XDG_")
        ):
            env[key] = value
    if user_env:
        env.update(user_env)
    return env


def _sanitize_error(text: str) -> str:
    """在把错误文本返回给 LLM 之前，剥离其中类似凭据的模式。

    将 token、key 和其他密钥替换为 [REDACTED]，以防在工具错误响应中意外
    暴露凭据。
    """
    return _CREDENTIAL_PATTERN.sub("[REDACTED]", text)


def _exc_str(exc: BaseException) -> str:
    """返回 *exc* 的非空人类可读字符串。

    某些异常类（例如 ``anyio.ClosedResourceError``）被抛出时不带消息参数，
    因此 ``str(exc)`` 为 ``""``。本辅助函数回退到 ``repr(exc)``，以确保展示
    给用户并写入磁盘的错误消息总是带有*一些*诊断信息。
    """
    text = str(exc).strip()
    return text if text else repr(exc)


# JSON-RPC "method not found"——服务器未实现被请求的方法时返回的错误
# （例如一个具备工具能力但从未接入可选 ``ping`` 工具的服务器）。这里本地定义
# 并带回退值，以便即使在不导出该常量的 SDK 构建上也能检测。
try:
    from mcp.types import METHOD_NOT_FOUND as _JSONRPC_METHOD_NOT_FOUND
except Exception:  # pragma: no cover — older/newer SDK without the constant
    _JSONRPC_METHOD_NOT_FOUND = -32601


def _is_method_not_found_error(exc: BaseException) -> bool:
    """当 *exc* 是 JSON-RPC ``method not found``（-32601）时返回 True。

    ``ping`` 是一个*可选*的 MCP 工具（规范原文："optional ping mechanism"）。
    未实现它的服务器会用 -32601 而非空结果来回应 ping。先结构化检查
    ``McpError.error.code``，再回退到子串匹配，使检测能扛住 SDK 版本漂移，
    以及把该情况作为普通消息抛出的服务器。

    当服务器不带结构化 ``-32601`` 码而报告 method-not-found（例如作为普通
    异常字符串抛出）时，子串回退就很关键。除了规范的 "method not found" 外，
    许多 JSON-RPC 实现将其表述为 "Unknown method: <name>"——agentmemory 的
    MCP 服务器就是一例（#50028）。如果不匹配这种表述，ping→list_tools 的
    回退就永远不会锁定，存活探测会陷入重连循环。
    """
    # 结构化：mcp.shared.exceptions.McpError 携带 ErrorData.code。
    err = getattr(exc, "error", None)
    code = getattr(err, "code", None)
    if code == _JSONRPC_METHOD_NOT_FOUND:
        return True
    msg = str(exc).lower()
    if not msg:
        return False
    return (
        str(_JSONRPC_METHOD_NOT_FOUND) in msg
        or "method not found" in msg
        or "unknown method" in msg
        or "not found: ping" in msg
    )


# ---------------------------------------------------------------------------
# MCP 工具描述内容扫描
# ---------------------------------------------------------------------------

# 指示 MCP 工具描述中可能存在提示词注入（prompt injection）的模式。
# 这些是 WARNING 级别的——我们只记录日志而不拦截，因为误报会破坏合法的
# MCP 服务器。
_MCP_INJECTION_PATTERNS = [
    (re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I),
     "prompt override attempt ('ignore previous instructions')"),
    (re.compile(r"you\s+are\s+now\s+a", re.I),
     "identity override attempt ('you are now a...')"),
    (re.compile(r"your\s+new\s+(task|role|instructions?)\s+(is|are)", re.I),
     "task override attempt"),
    (re.compile(r"system\s*:\s*", re.I),
     "system prompt injection attempt"),
    (re.compile(r"<\s*(system|human|assistant)\s*>", re.I),
     "role tag injection attempt"),
    (re.compile(r"do\s+not\s+(tell|inform|mention|reveal)", re.I),
     "concealment instruction"),
    (re.compile(r"(curl|wget|fetch)\s+https?://", re.I),
     "network command in description"),
    (re.compile(r"base64\.(b64decode|decodebytes)", re.I),
     "base64 decode reference"),
    (re.compile(r"exec\s*\(|eval\s*\(", re.I),
     "code execution reference"),
    (re.compile(r"import\s+(subprocess|os|shutil|socket)", re.I),
     "dangerous import reference"),
]


def _scan_mcp_description(server_name: str, tool_name: str, description: str) -> List[str]:
    """扫描 MCP 工具描述中的提示词注入模式。

    返回命中字符串列表（空列表表示干净）。
    """
    findings = []
    if not description:
        return findings
    for pattern, reason in _MCP_INJECTION_PATTERNS:
        if pattern.search(description):
            findings.append(reason)
    if findings:
        logger.warning(
            "MCP server '%s' tool '%s': suspicious description content — %s. "
            "Description: %.200s",
            server_name, tool_name, "; ".join(findings),
            description,
        )
    return findings


def _prepend_path(env: dict, directory: str) -> dict:
    """如果 *directory* 尚未存在，则把它前置到 env 的 PATH 中。"""
    updated = dict(env or {})
    if not directory:
        return updated

    existing = updated.get("PATH", "")
    parts = [part for part in existing.split(os.pathsep) if part]
    if directory not in parts:
        parts = [directory, *parts]
    updated["PATH"] = os.pathsep.join(parts) if parts else directory
    return updated


def _resolve_stdio_command(command: str, env: dict) -> tuple[str, dict]:
    """在子进程的确切环境下解析 stdio MCP 命令。

    这主要是为了让裸的 ``npx``/``npm``/``node`` 命令即便在 MCP 子进程运行于
    过滤后的 PATH 下也能可靠工作。
    """
    resolved_command = os.path.expanduser(str(command).strip())
    resolved_env = dict(env or {})

    if os.sep not in resolved_command:
        path_arg = resolved_env["PATH"] if "PATH" in resolved_env else None
        which_hit = shutil.which(resolved_command, path=path_arg)
        if which_hit:
            resolved_command = which_hit
        elif resolved_command in {"npx", "npm", "node"}:
            hermes_home = os.path.expanduser(
                os.getenv(
                    "HERMES_HOME", os.path.join(os.path.expanduser("~"), ".hermes")
                )
            )
            candidates = [
                os.path.join(hermes_home, "node", "bin", resolved_command),
                os.path.join(os.path.expanduser("~"), ".local", "bin", resolved_command),
                # /usr/local/bin 是 Linux 上 Node 的规范安装位置——包括
                # 从源码构建、上游 node:bookworm-slim 镜像（Hermes 的 Docker
                # 镜像自 #4977 起从中复制 node + npm + corepack），以及
                # Intel 上的 macOS Homebrew。没有这个候选路径，任何配置了
                # 不含 /usr/local/bin 的 env.PATH 的 MCP 服务器（用户手工编写
                # PATH 做沙箱时的常见做法）都会在 execvp 时以 ENOENT 失败；
                # 而在用户 PATH 里建符号链接这种简陋的变通只会失败得更深一层，
                # 因为 npx 的 shebang 会重新执行 /usr/bin/env node，而后者
                # 同样需要这个目录。
                os.path.join(os.sep, "usr", "local", "bin", resolved_command),
            ]
            for candidate in candidates:
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    resolved_command = candidate
                    break

    command_dir = os.path.dirname(resolved_command)
    if command_dir:
        resolved_env = _prepend_path(resolved_env, command_dir)

    return resolved_command, resolved_env


# ---------------------------------------------------------------------------
# MCP ImageContent 块 → Hermes MEDIA 标签
# ---------------------------------------------------------------------------


def _mcp_image_extension_for_mime_type(mime_type: str) -> str:
    """为 MCP 图像 MIME 类型返回一个合理的文件扩展名。"""
    import mimetypes
    normalized = (mime_type or "").split(";", 1)[0].strip().lower()
    if normalized in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    return mimetypes.guess_extension(normalized) or ".png"


def _cache_mcp_image_block(block) -> str:
    """把 MCP ``ImageContent`` 块缓存到共享图像缓存，并返回一个 Hermes
    网关知道如何渲染的 ``MEDIA:<path>`` 标签。

    当 *block* 不是图像、base64 负载格式错误，或缓存辅助函数拒绝这些字节
    （例如伪装成图像的非图像 MIME）时返回空字符串。错误只记录日志而不抛出：
    单个坏块不应毁掉整个工具结果，调用方会继续处理其他已成功解析的文本块。
    """
    import base64

    data = getattr(block, "data", None)
    mime_type = getattr(block, "mimeType", None)
    normalized_mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    if data is None or not normalized_mime.startswith("image/"):
        return ""

    try:
        raw_bytes = base64.b64decode(data)
    except (TypeError, ValueError) as exc:
        logger.warning("MCP image block decode failed (%s): %s", normalized_mime, exc)
        return ""

    try:
        from gateway.platforms.base import cache_image_from_bytes

        image_path = cache_image_from_bytes(
            raw_bytes,
            ext=_mcp_image_extension_for_mime_type(normalized_mime),
        )
    except ImportError:
        # 本进程中无法导入 gateway.platforms.base（例如不带 gateway 依赖的
        # cron）。回退为静默丢弃——调用方仍能拿到已成功解析的文本块。
        logger.debug("MCP image caching skipped — gateway.platforms.base unavailable")
        return ""
    except Exception as exc:
        logger.warning("MCP image block cache failed: %s", exc)
        return ""

    return f"MEDIA:{image_path}"


# ---------------------------------------------------------------------------
# 远程 MCP URL 校验
# ---------------------------------------------------------------------------


class InvalidMcpUrlError(ValueError):
    """当远程 MCP 服务器的 ``url`` 无法被解析为 http(s):// 时抛出。

    在启动时校验一次，以便以清晰的错误消息快速失败，而不是每次尝试都白白
    烧掉重连退避循环。（移植自 anomalyco/opencode#25019。）
    """


class NonMcpEndpointError(ConnectionError):
    """当 HTTP MCP URL 返回了非 MCP 响应时抛出。

    真正的 MCP Streamable-HTTP 端点会以 ``application/json`` 或
    ``text/event-stream`` 应答。在 2xx 响应上出现其他类型（通常是来自
    Web 应用根的 ``text/html``）意味着配置的 ``url`` 指向了错误的位置。
    该错误不可重试：每次尝试都返回同一个页面，因此跳过重连退避循环，
    并立即以可操作的错误消息报告服务器失败。

    继承自 :class:`ConnectionError`，以便只捕获宽泛异常类的调用方仍会将其
    视为连接问题。
    """


def _validate_remote_mcp_url(server_name: str, url: Any) -> str:
    """如果是一个合法的 http(s) 远程 MCP URL，则返回该 URL 字符串。

    否则抛出 :class:`InvalidMcpUrlError`，消息中会指明出问题的服务器名，
    以便用户在配置中定位到错误条目。

    接受：
    - ``http://host`` / ``https://host``，可带可选端口、路径、查询
    - IPv4、IPv6（带方括号）、DNS 主机名

    拒绝：
    - 非字符串值（``None``、字典、整数）
    - 缺少 scheme（``example.com/mcp``）
    - 非 http(s) 的 scheme（``file://``、``ws://``、``stdio:`` —— stdio
      服务器使用 ``command`` 键，而不是 ``url``）
    - 主机为空（``http://``、``https:///path``）
    """
    if not isinstance(url, str):
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': expected a string, got "
            f"{type(url).__name__}"
        )
    stripped = url.strip()
    if not stripped:
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': empty url"
        )
    try:
        parsed = urlparse(stripped)
    except Exception as exc:  # urlparse 非常宽松——双保险
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': {stripped!r} ({exc})"
        ) from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': scheme must be http or "
            f"https, got {parsed.scheme!r} ({stripped!r})"
        )
    if not parsed.netloc:
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': missing host ({stripped!r})"
        )
    # ``urlparse`` 会接受 ``http://:8080``（主机为空、显式端口）。
    # 拒绝这种情况——我们需要真实的主机。
    if not parsed.hostname:
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': missing hostname "
            f"({stripped!r})"
        )
    return stripped


def _resolve_client_cert(server_name: str, config: dict):
    """为 mTLS 解析 ``client_cert`` / ``client_key`` 配置。

    返回 ``httpx`` 的 ``cert=`` 参数所接受的任何形式；未配置客户端证书时
    返回 ``None``：

      - 若 ``client_cert`` 和 ``client_key`` 都未设置，则为 ``None``。
      - 若 ``client_cert`` 是字符串且 ``client_key`` 未设置（证书和密钥合并在
        一个 PEM 文件中），则为单个绝对路径字符串。
      - 当两者都设置，或 ``client_cert`` 是 2 元素列表/元组时，为
        ``(cert_path, key_path)`` 元组。
      - 当 ``client_cert`` 是 3 元素列表/元组时，为
        ``(cert_path, key_path, password)`` 元组——第三个元素是密钥口令。

    用户路径支持 ``~`` 展开。缺失的文件会抛出 ``FileNotFoundError`` 并带有
    按服务器作用域的消息，使失败以清晰的配置错误呈现，而非晦涩的 TLS 握手
    错误。
    """
    raw_cert = config.get("client_cert")
    raw_key = config.get("client_key")

    if raw_cert is None and raw_key is None:
        return None

    def _expand(path: Any, label: str) -> str:
        if not isinstance(path, str) or not path.strip():
            raise ValueError(
                f"MCP server '{server_name}': {label} must be a non-empty "
                f"string path (got {type(path).__name__})"
            )
        expanded = os.path.expanduser(path.strip())
        if not os.path.isfile(expanded):
            raise FileNotFoundError(
                f"MCP server '{server_name}': {label} not found at "
                f"{expanded!r}"
            )
        return expanded

    # client_cert 的元组/列表形式——(cert, key) 或 (cert, key, password)。
    if isinstance(raw_cert, (list, tuple)):
        if raw_key is not None:
            raise ValueError(
                f"MCP server '{server_name}': specify either client_cert as "
                f"a list [cert, key] OR client_cert + client_key, not both"
            )
        if len(raw_cert) == 2:
            cert_path = _expand(raw_cert[0], "client_cert[0]")
            key_path = _expand(raw_cert[1], "client_cert[1]")
            return (cert_path, key_path)
        if len(raw_cert) == 3:
            cert_path = _expand(raw_cert[0], "client_cert[0]")
            key_path = _expand(raw_cert[1], "client_cert[1]")
            password = raw_cert[2]
            if not isinstance(password, str):
                raise ValueError(
                    f"MCP server '{server_name}': client_cert[2] (key "
                    f"passphrase) must be a string"
                )
            return (cert_path, key_path, password)
        raise ValueError(
            f"MCP server '{server_name}': client_cert list form must have 2 "
            f"or 3 elements (got {len(raw_cert)})"
        )

    # client_cert 的字符串形式。
    cert_path = _expand(raw_cert, "client_cert")
    if raw_key is not None:
        key_path = _expand(raw_key, "client_key")
        return (cert_path, key_path)
    # 单个合并的 PEM 文件（证书和密钥在一个文件里）。
    return cert_path


def _format_connect_error(exc: BaseException) -> str:
    """把嵌套的 MCP 连接错误渲染成一条可操作的简短消息。"""

    def _find_missing(current: BaseException) -> Optional[str]:
        nested = getattr(current, "exceptions", None)
        if nested:
            for child in nested:
                missing = _find_missing(child)
                if missing:
                    return missing
            return None
        if isinstance(current, FileNotFoundError):
            if getattr(current, "filename", None):
                return str(current.filename)
            match = re.search(r"No such file or directory: '([^']+)'", str(current))
            if match:
                return match.group(1)
        for attr in ("__cause__", "__context__"):
            nested_exc = getattr(current, attr, None)
            if isinstance(nested_exc, BaseException):
                missing = _find_missing(nested_exc)
                if missing:
                    return missing
        return None

    def _flatten_messages(current: BaseException) -> List[str]:
        nested = getattr(current, "exceptions", None)
        if nested:
            flattened: List[str] = []
            for child in nested:
                flattened.extend(_flatten_messages(child))
            return flattened
        messages = []
        text = str(current).strip()
        if text:
            messages.append(text)
        for attr in ("__cause__", "__context__"):
            nested_exc = getattr(current, attr, None)
            if isinstance(nested_exc, BaseException):
                messages.extend(_flatten_messages(nested_exc))
        return messages or [current.__class__.__name__]

    missing = _find_missing(exc)
    if missing:
        message = f"missing executable '{missing}'"
        if os.path.basename(missing) in {"npx", "npm", "node"}:
            message += (
                " (ensure Node.js is installed and PATH includes its bin directory, "
                "or set mcp_servers.<name>.command to an absolute path and include "
                "that directory in mcp_servers.<name>.env.PATH)"
            )
        return _sanitize_error(message)

    deduped: List[str] = []
    for item in _flatten_messages(exc):
        if item not in deduped:
            deduped.append(item)
    return _sanitize_error("; ".join(deduped[:3]))


# ---------------------------------------------------------------------------
# Sampling——服务器发起的 LLM 请求（MCP sampling/createMessage）
# ---------------------------------------------------------------------------

def _safe_numeric(value, default, coerce=int, minimum=1):
    """把配置值强制转换为数值类型，失败时返回 *default*。

    处理来自 YAML 的字符串值（例如 ``"10"`` 而非 ``10``）、非有限浮点数，
    以及低于 *minimum* 的值。
    """
    try:
        result = coerce(value)
        if isinstance(result, float) and not math.isfinite(result):
            return default
        return max(result, minimum)
    except (TypeError, ValueError, OverflowError):
        return default


class SamplingHandler:
    """处理单个 MCP 服务器的 sampling/createMessage 请求。

    每个开启了 sampling 的 MCPServerTask 都会创建一个 SamplingHandler。
    该 handler 可调用，并作为 ``sampling_callback`` 直接传给
    ``ClientSession``。所有状态（限速时间戳、指标、工具循环计数器）都存放在
    实例上——没有模块级全局变量。

    该回调是异步的，运行在 MCP 后台事件循环上。同步的 LLM 调用通过
    ``asyncio.to_thread()`` 卸载到线程中执行，因此不会阻塞事件循环。
    """

    _STOP_REASON_MAP = {"stop": "endTurn", "length": "maxTokens", "tool_calls": "toolUse"}

    def __init__(self, server_name: str, config: dict):
        self.server_name = server_name
        self.max_rpm = _safe_numeric(config.get("max_rpm", 10), 10, int)
        self.timeout = _safe_numeric(config.get("timeout", 30), 30, float)
        self.max_tokens_cap = _safe_numeric(config.get("max_tokens_cap", 4096), 4096, int)
        self.max_tool_rounds = _safe_numeric(
            config.get("max_tool_rounds", 5), 5, int, minimum=0,
        )
        self.model_override = config.get("model")
        self.allowed_models = config.get("allowed_models", [])

        _log_levels = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING}
        self.audit_level = _log_levels.get(
            str(config.get("log_level", "info")).lower(), logging.INFO,
        )

        # 每个实例独有的状态
        self._rate_timestamps: List[float] = []
        self._tool_loop_count = 0
        self.metrics = {"requests": 0, "errors": 0, "tokens_used": 0, "tool_use_count": 0}

    # -- 限速 -------------------------------------------------------

    def _check_rate_limit(self) -> bool:
        """滑动窗口限速器。请求被允许时返回 True。"""
        now = time.time()
        window = now - 60
        self._rate_timestamps[:] = [t for t in self._rate_timestamps if t > window]
        if len(self._rate_timestamps) >= self.max_rpm:
            return False
        self._rate_timestamps.append(now)
        return True

    # -- 模型解析 ----------------------------------------------------

    def _resolve_model(self, preferences) -> Optional[str]:
        """配置覆盖 > 服务器提示 > None（使用默认值）。"""
        if self.model_override:
            return self.model_override
        if preferences and hasattr(preferences, "hints") and preferences.hints:
            for hint in preferences.hints:
                if hasattr(hint, "name") and hint.name:
                    return hint.name
        return None

    # -- 消息转换 --------------------------------------------------

    @staticmethod
    def _extract_tool_result_text(block) -> str:
        """从 ToolResultContent 块中提取文本。"""
        if not hasattr(block, "content") or block.content is None:
            return ""
        items = block.content if isinstance(block.content, list) else [block.content]
        return "\n".join(item.text for item in items if hasattr(item, "text"))

    def _convert_messages(self, params) -> List[dict]:
        """把 MCP SamplingMessages 转换为 OpenAI 格式。

        使用 ``msg.content_as_list``（SDK 辅助方法），使单块和块列表得到统一
        处理。在可用时通过 ``isinstance`` 对真实 SDK 类型分派，否则回退到通过
        ``hasattr`` 做鸭子类型以保持兼容性。
        """
        messages: List[dict] = []
        for msg in params.messages:
            blocks = msg.content_as_list if hasattr(msg, "content_as_list") else (
                msg.content if isinstance(msg.content, list) else [msg.content]
            )

            # 按类别分块
            tool_results = [b for b in blocks if hasattr(b, "toolUseId")]
            tool_uses = [b for b in blocks if hasattr(b, "name") and hasattr(b, "input") and not hasattr(b, "toolUseId")]
            content_blocks = [b for b in blocks if not hasattr(b, "toolUseId") and not (hasattr(b, "name") and hasattr(b, "input"))]

            # 产出工具结果消息（role: tool）
            for tr in tool_results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tr.toolUseId,
                    "content": self._extract_tool_result_text(tr),
                })

            # 产出 assistant 的 tool_calls 消息
            if tool_uses:
                tc_list = []
                for tu in tool_uses:
                    tc_list.append({
                        "id": getattr(tu, "id", f"call_{len(tc_list)}"),
                        "type": "function",
                        "function": {
                            "name": tu.name,
                            "arguments": json.dumps(tu.input, ensure_ascii=False) if isinstance(tu.input, dict) else str(tu.input),
                        },
                    })
                msg_dict: dict = {"role": msg.role, "tool_calls": tc_list}
                # 包含任何伴随的文本
                text_parts = [b.text for b in content_blocks if hasattr(b, "text")]
                if text_parts:
                    msg_dict["content"] = "\n".join(text_parts)
                messages.append(msg_dict)
            elif content_blocks:
                # 纯文本/图像内容
                if len(content_blocks) == 1 and hasattr(content_blocks[0], "text"):
                    messages.append({"role": msg.role, "content": content_blocks[0].text})
                else:
                    parts = []
                    for block in content_blocks:
                        if hasattr(block, "text"):
                            parts.append({"type": "text", "text": block.text})
                        elif hasattr(block, "data") and hasattr(block, "mimeType"):
                            parts.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{block.mimeType};base64,{block.data}"},
                            })
                        else:
                            logger.warning(
                                "Unsupported sampling content block type: %s (skipped)",
                                type(block).__name__,
                            )
                    if parts:
                        messages.append({"role": msg.role, "content": parts})

        return messages

    # -- 错误辅助 --------------------------------------------------------

    @staticmethod
    def _error(message: str, code: int = -1):
        """返回 ErrorData（MCP 规范），否则作为回退抛出。"""
        if _MCP_SAMPLING_TYPES:
            return ErrorData(code=code, message=message)
        raise Exception(message)

    # -- 响应构建 ---------------------------------------------------

    def _build_tool_use_result(self, choice, response):
        """从 LLM tool_calls 响应构建 CreateMessageResultWithTools。"""
        self.metrics["tool_use_count"] += 1

        # 工具循环治理
        if self.max_tool_rounds == 0:
            self._tool_loop_count = 0
            return self._error(
                f"Tool loops disabled for server '{self.server_name}' (max_tool_rounds=0)"
            )

        self._tool_loop_count += 1
        if self._tool_loop_count > self.max_tool_rounds:
            self._tool_loop_count = 0
            return self._error(
                f"Tool loop limit exceeded for server '{self.server_name}' "
                f"(max {self.max_tool_rounds} rounds)"
            )

        content_blocks = []
        for tc in choice.message.tool_calls:
            args = tc.function.arguments
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                except (json.JSONDecodeError, ValueError):
                    logger.warning(
                        "MCP server '%s': malformed tool_calls arguments "
                        "from LLM (wrapping as raw): %.100s",
                        self.server_name, args,
                    )
                    parsed = {"_raw": args}
            else:
                parsed = args if isinstance(args, dict) else {"_raw": str(args)}

            content_blocks.append(ToolUseContent(
                type="tool_use",
                id=tc.id,
                name=tc.function.name,
                input=parsed,
            ))

        logger.log(
            self.audit_level,
            "MCP server '%s' sampling response: model=%s, tokens=%s, tool_calls=%d",
            self.server_name, response.model,
            getattr(getattr(response, "usage", None), "total_tokens", "?"),
            len(content_blocks),
        )

        return CreateMessageResultWithTools(
            role="assistant",
            content=content_blocks,
            model=response.model,
            stopReason="toolUse",
        )

    def _build_text_result(self, choice, response):
        """从普通文本响应构建 CreateMessageResult。"""
        self._tool_loop_count = 0  # 文本响应时重置
        response_text = choice.message.content or ""

        logger.log(
            self.audit_level,
            "MCP server '%s' sampling response: model=%s, tokens=%s",
            self.server_name, response.model,
            getattr(getattr(response, "usage", None), "total_tokens", "?"),
        )

        return CreateMessageResult(
            role="assistant",
            content=TextContent(type="text", text=_sanitize_error(response_text)),
            model=response.model,
            stopReason=self._STOP_REASON_MAP.get(choice.finish_reason, "endTurn"),
        )

    # -- 会话 kwargs 辅助 -----------------------------------------------

    def session_kwargs(self) -> dict:
        """返回传给 ClientSession 以支持 sampling 的 kwargs。"""
        return {
            "sampling_callback": self,
            "sampling_capabilities": SamplingCapability(
                tools=SamplingToolsCapability(),
            ),
        }

    # -- 主回调 -------------------------------------------------------

    async def __call__(self, context, params):
        """由 MCP SDK 调用的 sampling 回调。

        遵循 ``SamplingFnT`` 协议。返回
        ``CreateMessageResult``、``CreateMessageResultWithTools`` 或
        ``ErrorData``。
        """
        # 限速
        if not self._check_rate_limit():
            logger.warning(
                "MCP server '%s' sampling rate limit exceeded (%d/min)",
                self.server_name, self.max_rpm,
            )
            self.metrics["errors"] += 1
            return self._error(
                f"Sampling rate limit exceeded for server '{self.server_name}' "
                f"({self.max_rpm} requests/minute)"
            )

        # 解析模型
        model = self._resolve_model(getattr(params, "modelPreferences", None))

        # 通过集中式路由器获取辅助 LLM 客户端
        from agent.auxiliary_client import call_llm

        # 模型白名单检查（需在调用前解析模型）
        resolved_model = model or self.model_override or ""

        if self.allowed_models and resolved_model and resolved_model not in self.allowed_models:
            logger.warning(
                "MCP server '%s' requested model '%s' not in allowed_models",
                self.server_name, resolved_model,
            )
            self.metrics["errors"] += 1
            return self._error(
                f"Model '{resolved_model}' not allowed for server "
                f"'{self.server_name}'. Allowed: {', '.join(self.allowed_models)}"
            )

        # 转换消息
        messages = self._convert_messages(params)
        if hasattr(params, "systemPrompt") and params.systemPrompt:
            messages.insert(0, {"role": "system", "content": params.systemPrompt})

        # 构建 LLM 调用 kwargs
        max_tokens = min(params.maxTokens, self.max_tokens_cap)
        call_temperature = None
        if hasattr(params, "temperature") and params.temperature is not None:
            call_temperature = params.temperature

        # 转发服务器提供的工具
        call_tools = None
        server_tools = getattr(params, "tools", None)
        if server_tools:
            call_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": getattr(t, "name", ""),
                        "description": getattr(t, "description", "") or "",
                        "parameters": _normalize_mcp_input_schema(
                            getattr(t, "inputSchema", None)
                        ),
                    },
                }
                for t in server_tools
            ]

        logger.log(
            self.audit_level,
            "MCP server '%s' sampling request: model=%s, max_tokens=%d, messages=%d",
            self.server_name, resolved_model, max_tokens, len(messages),
        )

        # 把同步 LLM 调用卸载到线程（非阻塞）
        def _sync_call():
            return call_llm(
                task="mcp",
                model=resolved_model or None,
                messages=messages,
                temperature=call_temperature,
                max_tokens=max_tokens,
                tools=call_tools,
                timeout=self.timeout,
            )

        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(_sync_call), timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            self.metrics["errors"] += 1
            return self._error(
                f"Sampling LLM call timed out after {self.timeout}s "
                f"for server '{self.server_name}'"
            )
        except Exception as exc:
            self.metrics["errors"] += 1
            return self._error(
                f"Sampling LLM call failed: {_sanitize_error(_exc_str(exc))}"
            )

        # 防御空 choices（内容过滤、提供者错误）
        if not getattr(response, "choices", None):
            self.metrics["errors"] += 1
            return self._error(
                f"LLM returned empty response (no choices) for server "
                f"'{self.server_name}'"
            )

        # 统计指标
        choice = response.choices[0]
        self.metrics["requests"] += 1
        total_tokens = getattr(getattr(response, "usage", None), "total_tokens", 0)
        if isinstance(total_tokens, int):
            self.metrics["tokens_used"] += total_tokens

        # 按响应类型分派
        if (
            choice.finish_reason == "tool_calls"
            and hasattr(choice.message, "tool_calls")
            and choice.message.tool_calls
        ):
            return self._build_tool_use_result(choice, response)

        return self._build_text_result(choice, response)


# ---------------------------------------------------------------------------
# Elicitation 处理器
# ---------------------------------------------------------------------------

def _format_elicitation_schema_summary(schema: dict, server_name: str) -> str:
    """把类 JSON-schema 的 requested_schema 渲染成人类可读的字段列表。

    Elicitation schema 被限制为带命名顶层属性的扁平对象。我们呈现字段名、
    类型和描述，以便用户在批准前能了解服务器在请求什么。
    """
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not props:
        return f"Approval requested by MCP server '{server_name}'."

    lines = [f"Fields requested by MCP server '{server_name}':"]
    for field_name, field_spec in props.items():
        field_type = ""
        field_desc = ""
        if isinstance(field_spec, dict):
            field_type = str(field_spec.get("type", "") or "")
            field_desc = str(field_spec.get("description", "") or "")
        suffix = f" ({field_type})" if field_type else ""
        if field_desc:
            lines.append(f"  - {field_name}{suffix}: {field_desc}")
        else:
            lines.append(f"  - {field_name}{suffix}")
    return "\n".join(lines)


class ElicitationHandler:
    """处理单个 MCP 服务器的 ``elicitation/create`` 请求。

    每个开启了 elicitation 的 ``MCPServerTask`` 都会创建一个 handler。该
    handler 可调用，并作为 ``elicitation_callback`` 直接传给
    ``ClientSession``（在 mcp Python SDK 1.11.0 中引入）。

    Elicitation 允许服务器在工具调用过程中请求客户端从用户处收集结构化输入
    （例如支付授权、OAuth 确认）。Form 模式的 elicitation 通过 Hermes 既有的
    审批系统（``tools.approval.prompt_dangerous_approval``）路由，该系统会在
    当前会话所使用的任何界面上弹出提示——CLI、TUI、Telegram、Slack 等。
    URL 模式的 elicitation 因不支持而被拒绝。

    失败模式采用失败即关闭（fail-closed）：任何超时、异常或意外状态都会返回
    ``decline``/``cancel``，而不是静默接受。服务器会将其视为用户未批准。
    """

    # 审批等待的外部上限。``prompt_dangerous_approval`` 通过审批配置值运行自己
    # 的 input() 超时；这里是一个 asyncio 侧的安全网，以防内部超时机制被绕过
    # 时 MCP 事件循环无限阻塞。
    _OUTER_TIMEOUT_GRACE_SECONDS = 5

    def __init__(self, server_name: str, config: dict, owner: Optional["MCPServerTask"] = None):
        self.server_name = server_name
        # 单次 elicitation 的超时。默认 5 分钟，与网关审批默认值一致，使异步
        # 界面（Telegram、Slack）上的用户在服务器放弃前有时间响应。
        self.timeout = _safe_numeric(config.get("timeout", 300), 300, float)
        # 指向 MCPServerTask 的反向引用，以便在 elicitation 时读取 agent 捕获
        # 的 contextvars 快照。可选，使 handler 在隔离环境下仍可单元测试。
        self.owner = owner
        self.metrics = {
            "requests": 0,
            "accepted": 0,
            "declined": 0,
            "errors": 0,
        }

    def session_kwargs(self) -> dict:
        """返回传给 ClientSession 以支持 elicitation 的 kwargs。"""
        return {"elicitation_callback": self}

    async def __call__(self, context, params):
        """由 MCP SDK 调用的 elicitation 回调。

        遵循 ``ElicitationFnT`` 协议。返回 ``ElicitResult`` 或
        ``ErrorData``。
        """
        self.metrics["requests"] += 1

        # URL 模式的 elicitation 会把用户指向一个外部 URL 以进行敏感的带外
        # 流程（OAuth、支付处理）。支持它需要打开浏览器访问该 URL 并等待服务器
        # 的 notifications/elicitation/complete——这超出了初始实现的范围。干净
        # 地拒绝，以免服务器挂起。
        mode = getattr(params, "mode", "form")
        if mode == "url":
            logger.info(
                "MCP server '%s' requested URL-mode elicitation; "
                "declining (URL-mode elicitation not implemented)",
                self.server_name,
            )
            self.metrics["declined"] += 1
            return ElicitResult(action="decline")

        message = getattr(params, "message", "") or (
            f"MCP server '{self.server_name}' is requesting your approval"
        )
        schema = getattr(params, "requested_schema", {}) or {}
        description = _format_elicitation_schema_summary(schema, self.server_name)

        logger.info(
            "MCP server '%s' elicitation request: %s",
            self.server_name, _sanitize_error(message)[:200],
        )

        # 懒加载：tools.approval 在进程引导早期就被导入；沿用 _fire_approval_hook
        # 的懒加载模式可避免任何导入顺序耦合。
        try:
            from tools.approval import request_elicitation_consent
        except Exception as exc:  # pragma: no cover -- defensive
            logger.error(
                "MCP server '%s' elicitation: approval system unavailable: %s",
                self.server_name, exc,
            )
            self.metrics["errors"] += 1
            return ElicitResult(action="decline")

        # 把同步审批流程卸载到工作线程。内联运行会冻结 MCP 后台事件循环，阻塞
        # 该会话上的所有其他 RPC。request_elicitation_consent() 会自行路由到
        # 正确的界面（Telegram / Slack 等走网关 notify_cb，CLI / TUI 走
        # prompt_dangerous_approval），并将答案归一化为 accept / decline /
        # cancel 之一。
        #
        # 触发本回调的 recv-loop 任务不会继承 agent 的 contextvars
        # （HERMES_SESSION_PLATFORM 等）。当 MCP 工具封装把 agent 的上下文捕获到
        # owner._pending_call_context 后，我们在此通过
        # contextvars.Context.run 重放它，使 request_elicitation_consent 中的
        # 网关平台检测能定位到正确的会话。
        captured = getattr(self.owner, "_pending_call_context", None) if self.owner else None

        def _invoke_consent() -> str:
            if captured is None:
                return request_elicitation_consent(
                    message,
                    description,
                    timeout_seconds=int(self.timeout),
                    surface=f"mcp-elicitation/{self.server_name}",
                )
            # Context.run 只能执行一次上下文——复制以允许单次工具调用内有
            # 多次 elicitation。
            return captured.copy().run(
                request_elicitation_consent,
                message,
                description,
                timeout_seconds=int(self.timeout),
                surface=f"mcp-elicitation/{self.server_name}",
            )

        try:
            answer = await asyncio.wait_for(
                asyncio.to_thread(_invoke_consent),
                timeout=self.timeout + self._OUTER_TIMEOUT_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "MCP server '%s' elicitation timed out after %ds",
                self.server_name, int(self.timeout),
            )
            self.metrics["errors"] += 1
            return ElicitResult(action="cancel")
        except Exception as exc:
            logger.error(
                "MCP server '%s' elicitation failed: %s",
                self.server_name, exc, exc_info=True,
            )
            self.metrics["errors"] += 1
            return ElicitResult(action="decline")

        if answer == "accept":
            self.metrics["accepted"] += 1
            return ElicitResult(action="accept", content={})
        if answer == "cancel":
            self.metrics["errors"] += 1
            return ElicitResult(action="cancel")
        self.metrics["declined"] += 1
        return ElicitResult(action="decline")


# ---------------------------------------------------------------------------
# 服务器任务——每个 MCP 服务器存活于一个长期存活的 asyncio Task 中
# ---------------------------------------------------------------------------

class MCPServerTask:
    """在一个专用 asyncio Task 中管理单个 MCP 服务器连接。

    整个连接生命周期（连接、发现、服务、断开）都在一个 asyncio Task 内
    运行，以便传输客户端创建的 anyio 取消作用域在同一个 Task 上下文中进入
    和退出。

    同时支持 stdio 和 HTTP/StreamableHTTP 传输。
    """

    __slots__ = (
        "name", "session", "tool_timeout",
        "_task", "_ready", "_shutdown_event", "_reconnect_event",
        "_tools", "_error", "_config",
        "_sampling", "_elicitation",
        "_registered_tool_names", "_auth_type", "_refresh_lock",
        "_rpc_lock", "_pending_refresh_tasks",
        "_pending_call_context",
        "initialize_result", "_ping_unsupported",
    )

    def __init__(self, name: str):
        self.name = name
        self.session: Optional[Any] = None
        self.tool_timeout: float = _DEFAULT_TOOL_TIMEOUT
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        # 在 manager.handle_401() 确认可恢复后，由工具处理器在鉴权失败时设置。
        # 设置后，_run_http / _run_stdio 会干净地退出其 async-with 块（不抛
        # 异常），外层 run() 循环重新进入传输，从而用新凭据重建 MCP 会话。
        self._reconnect_event = asyncio.Event()
        self._tools: list = []
        self._error: Optional[Exception] = None
        self._config: dict = {}
        self._sampling: Optional[SamplingHandler] = None
        self._elicitation: Optional[ElicitationHandler] = None
        self._registered_tool_names: list[str] = []
        self._auth_type: str = ""
        self._refresh_lock = asyncio.Lock()
        # MCP stdio 会话是单条 JSON-RPC 流。某些服务器在启动期间会发出
        # list_changed 通知；如果通知处理器在一次普通工具调用进行中调用
        # list_tools，流可能卡住，导致用户可见的工具调用超时。因此对每个
        # 服务器串行化客户端发起的 RPC。该锁也应用于 HTTP 传输，以保守地
        # 保证按服务器排序。
        self._rpc_lock = asyncio.Lock()
        self._pending_refresh_tasks: set[asyncio.Task] = set()
        # 当前正在 session.call_tool() 的 agent 任务的 contextvars 快照。
        # MCP recv 循环在一个独立的 asyncio 任务上分派进入的
        # elicitation/create 请求，该任务的上下文不会继承
        # HERMES_SESSION_PLATFORM，因此 elicitation 处理器无法检测触发调用的
        # 网关会话。在此捕获 agent 的上下文，并在 elicitation 回调内重放，
        # 即可恢复网关平台归属，把审批提示路由到正确的界面（Telegram、Slack
        # 等）。
        self._pending_call_context: Optional[contextvars.Context] = None
        # 捕获 ``await session.initialize()`` 返回的 ``InitializeResult``，
        # 以便下游代码能检查服务器真正声明的能力
        # （``.capabilities.resources``、``.capabilities.prompts``），而不是
        # 假设每个 ``ClientSession`` 方法属性都对应一个受支持的服务器方法。
        # 见 #18051。
        self.initialize_result: Optional[Any] = None
        # 当存活探测的 ``ping`` 首次返回 JSON-RPC -32601（method not found）
        # 时置为 True：服务器具备工具能力但未实现可选的 ``ping`` 工具。后续
        # 存活探测回退到 ``list_tools``（ping 之前的探测方式），这样既不会
        # 狂发 ping 也不会陷入重连循环。在每次新的传输连接时重置。
        self._ping_unsupported: bool = False

    def _is_http(self) -> bool:
        """检查该服务器是否使用 HTTP 传输。"""
        return "url" in self._config

    def _advertises_tools(self) -> bool:
        """服务器是否声明了 ``tools`` 能力。

        按 MCP 规范，``InitializeResult.capabilities.tools`` 非 None 当且仅当
        服务器实现了 ``tools/*`` 请求族。仅 prompt 或仅 resource 的服务器会
        省略它，对其调用 ``tools/list`` 会抛出
        ``McpError(-32601 Method not found)``——这在过去会在发现阶段杀掉连接，
        并使每次存活探测都失败。（移植自 anomalyco/opencode#31271。）

        当未捕获到能力信息时返回 True（遗留回退：保留旧的总是调用
        list_tools 的行为，而不是让在此门控之前能正常工作的服务器出现回归）。
        """
        init_result = self.initialize_result
        caps = getattr(init_result, "capabilities", None) if init_result is not None else None
        if caps is None:
            return True
        return getattr(caps, "tools", None) is not None

    # ----- 动态工具发现（notifications/tools/list_changed）-----

    async def _refresh_tools_task(self):
        """运行一次动态工具刷新，并记录后台任务的失败。"""
        try:
            await self._refresh_tools()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("MCP server '%s': dynamic tool refresh failed", self.name)

    def _schedule_tools_refresh(self) -> asyncio.Task:
        """调度一次后台工具刷新，并保持对它的强引用。"""
        task = asyncio.create_task(self._refresh_tools_task())
        self._pending_refresh_tasks.add(task)
        task.add_done_callback(self._pending_refresh_tasks.discard)
        return task

    def _make_message_handler(self):
        """为 ``ClientSession`` 构建一个 ``message_handler`` 回调。

        按通知类型分派。只有 ``ToolListChangedNotification`` 会触发刷新；
        prompt 和 resource 的变更通知仅作为占位记录日志，留待后续实现。
        """
        async def _handler(message):
            try:
                if isinstance(message, Exception):
                    logger.debug("MCP message handler (%s): exception: %s", self.name, message)
                    return
                if _MCP_NOTIFICATION_TYPES and isinstance(message, ServerNotification):
                    match message.root:
                        case ToolListChangedNotification():
                            logger.info(
                                "MCP server '%s': received tools/list_changed notification",
                                self.name,
                            )
                            # 某些服务器（尤其是 mongodb-mcp-server）会在
                            # initialize 之后立即发出 tools/list_changed，而此
                            # 时客户端可能正在执行另一个请求。在 SDK 通知处理器
                            # 中同步刷新会与该请求竞争，并卡死 stdio 的
                            # JSON-RPC 流，导致后续所有工具调用超时。因此把刷新
                            # 放到独立任务中执行，让处理器及时返回。
                            self._schedule_tools_refresh()
                            # 让出一个循环节拍，使测试和短生命周期的通知上下文
                            # 能观察到已调度的刷新，而无需等待完整的服务器 RPC。
                            await asyncio.sleep(0)
                        case PromptListChangedNotification():
                            logger.debug("MCP server '%s': prompts/list_changed (ignored)", self.name)
                        case ResourceListChangedNotification():
                            logger.debug("MCP server '%s': resources/list_changed (ignored)", self.name)
                        case _:
                            pass
            except Exception:
                logger.exception("Error in MCP message handler for '%s'", self.name)
        return _handler

    async def _refresh_tools(self):
        """从服务器重新拉取工具并更新注册表。

        在服务器发送 ``notifications/tools/list_changed`` 时调用。
        该锁可防止密集通知导致的重叠刷新。在初始的 ``await``（list_tools）
        之后，所有修改都是同步的——从事件循环视角看是原子的。
        """
        from tools.registry import registry

        if not self._advertises_tools():
            # 不实现 tools/* 的服务器不应发送 tools/list_changed，但还是做防护
            # ——调用 tools/list 会抛出 McpError(-32601)。
            return

        async with self._refresh_lock:
            # 捕获旧工具名用于变更对比
            old_tool_names = set(self._registered_tool_names)

            # 1. 从服务器拉取当前工具列表
            async with self._rpc_lock:
                tools_result = await self.session.list_tools()
            new_mcp_tools = tools_result.tools if hasattr(tools_result, "tools") else []

            # 2. 用新工具列表重新注册。避免对所有名字做"全部清空再重建"：
            # 进行中的 agent 轮次可能已有工具调用 ID 指向既有的处理器函数。
            # 对未变更的名字就地替换条目即可，并可避免启动期通知期间的瞬态
            # "tool not connected" / 处理器过期竞争。不在新列表中的工具已不可
            # 调用，因此只先移除这些过期的注册表条目。
            stale_tool_names = old_tool_names - {
                f"mcp_{sanitize_mcp_name_component(self.name)}_"
                f"{sanitize_mcp_name_component(tool.name)}"
                for tool in new_mcp_tools
            }
            for tool_name in stale_tool_names:
                registry.deregister(tool_name)
                _forget_mcp_tool_server(tool_name)

            # 3. 用新工具列表重新注册
            self._tools = new_mcp_tools
            self._registered_tool_names = _register_server_tools(
                self.name, self, self._config
            )

            # 5. 记录变更内容（用户可见的通知）
            new_tool_names = set(self._registered_tool_names)
            added = new_tool_names - old_tool_names
            removed = old_tool_names - new_tool_names
            changes = []
            if added:
                changes.append(f"added: {', '.join(sorted(added))}")
            if removed:
                changes.append(f"removed: {', '.join(sorted(removed))}")
            if changes:
                logger.warning(
                    "MCP server '%s': tools changed dynamically — %s. "
                    "Verify these changes are expected.",
                    self.name, "; ".join(changes),
                )
            else:
                logger.info(
                    "MCP server '%s': dynamically refreshed %d tool(s) (no changes)",
                    self.name, len(self._registered_tool_names),
                )

    async def _keepalive_probe(self) -> None:
        """驱动会话以检测陈旧/过期的连接。

        默认使用 ``ping``（廉价、与传输无关的存活检测）。``ping`` 是一个可选的
        MCP 工具：未实现它的服务器会以 JSON-RPC -32601 应答。首次发生这种情况
        时，我们会锁定 ``_ping_unsupported`` 并回退到 ping 之前的探测方式——在
        能力允许时使用 ``list_tools``；否则 ``ping`` 是唯一选择，-32601 会向上
        传播（一个既没有可用 ping 又没有工具的服务器已没有可用的存活原语）。
        该锁在每次新的传输连接时重置，以便重连后获得 ping 支持的服务器能重新
        走廉价路径探测。

        真正的连接失败会抛出异常，以便调用方触发重连；会话存活时正常返回。
        """
        if not self._ping_unsupported:
            try:
                await asyncio.wait_for(self.session.send_ping(), timeout=30.0)
                return
            except Exception as exc:
                # 只有 "method not found" 才表示 ping 不被支持。任何其他错误
                # （超时、传输关闭、会话过期）都是真正的存活失败——向上传播以便
                # 重连。
                if not _is_method_not_found_error(exc):
                    raise
                if not self._advertises_tools():
                    # 没有 ping，也没有工具 → 没有更廉价的探测可回退。
                    raise
                self._ping_unsupported = True
                logger.info(
                    "MCP server '%s': does not implement the optional 'ping' "
                    "utility (-32601); using 'list_tools' for keepalive on "
                    "this connection.",
                    self.name,
                )

        # 针对 ping 不支持的服务器的回退探测。
        await asyncio.wait_for(self.session.list_tools(), timeout=30.0)

    async def _wait_for_lifecycle_event(self) -> str:
        """阻塞，直到 _shutdown_event 或 _reconnect_event 触发。

        返回：
            "shutdown"  表示服务器应完全退出运行循环。
            "reconnect" 表示服务器应拆除当前 MCP 会话并重新进入传输（刷新
                        OAuth token、新的 session ID 等）。返回前会清除
                        reconnect 事件，使下一个周期以新的信号开始。

        若两个事件同时被设置，关闭优先。

        周期性地发送轻量存活探测（``ping``，对于不实现可选 ping 工具的服务器
        回退到 ``list_tools``——见 :meth:`_keepalive_probe`），以防空闲期间
        TCP/会话状态变陈旧（#17003）。如果存活探测失败，则触发重连。

        节奏取自服务器配置的 ``keepalive_interval``（默认
        :data:`_DEFAULT_KEEPALIVE_INTERVAL`，下限为
        :data:`_MIN_KEEPALIVE_INTERVAL`）。在短 TTL 上回收空闲会话的服务器
        （例如 Unreal Engine 的编辑器 MCP，约 15s）需要一个小于该 TTL 的间隔，
        否则每次空闲工具调用都会落到一个已过期的会话上，并付出完整的重连代价。
        """
        # 比服务器的会话 TTL 更快地刷新。使用 ``ping``（MCP 基础协议存活检测）
        # 而非 ``list_tools``，使探测始终保持几字节，无论服务器暴露多少工具——
        # 对一个有 830 个工具的服务器做 ``list_tools`` 存活探测每轮会拉取约
        # 1 MB。工具列表变更仍通过带外的
        # ``notifications/tools/list_changed`` → ``_refresh_tools`` 到达。
        keepalive_interval = max(
            _MIN_KEEPALIVE_INTERVAL,
            float(self._config.get("keepalive_interval", _DEFAULT_KEEPALIVE_INTERVAL)),
        )

        shutdown_task = asyncio.create_task(self._shutdown_event.wait())
        reconnect_task = asyncio.create_task(self._reconnect_event.wait())
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {shutdown_task, reconnect_task},
                    timeout=keepalive_interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if done:
                    break

                # 超时——没有生命周期事件触发。探测连接以检测陈旧/过期的会话。
                # 优先用 ``ping``（MCP 基础协议存活检测）：它统一工作且无论工具
                # 数量多少都保持几字节，不像 ``list_tools``（在有 830 个工具的服
                # 务器上约 1 MB）。``ping`` 是可选工具，因此具备工具能力但不实现
                # 它的服务器会以 -32601 应答；此时在本次连接的剩余时间里回退到
                # ping 之前的 ``list_tools`` 探测，而不是陷入重连循环。
                if self.session:
                    try:
                        await self._keepalive_probe()
                    except Exception as exc:
                        logger.warning(
                            "MCP server '%s' keepalive failed, "
                            "triggering reconnect: %s",
                            self.name, exc,
                        )
                        self._reconnect_event.set()
                        break
        finally:
            for t in (shutdown_task, reconnect_task):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

        if self._shutdown_event.is_set():
            return "shutdown"
        self._reconnect_event.clear()
        return "reconnect"

    async def _run_stdio(self, config: dict):
        """使用 stdio 传输运行服务器。"""
        if not _MCP_AVAILABLE:
            raise ImportError(
                f"MCP server '{self.name}' requires the 'mcp' Python SDK, but "
                "it is not installed. Install with:\n"
                "  pip install 'hermes-agent[mcp]'\n"
                "or (full install):\n"
                "  pip install 'hermes-agent[all]'"
            )

        command = config.get("command")
        args = config.get("args", [])
        user_env = config.get("env")

        if not command:
            raise ValueError(
                f"MCP server '{self.name}' has no 'command' in config"
            )

        safe_env = _build_safe_env(user_env)
        command, safe_env = _resolve_stdio_command(command, safe_env)

        # 在拉起子进程前，对照 OSV 恶意软件数据库检查包
        from tools.osv_check import check_package_for_malware
        malware_error = check_package_for_malware(command, args)
        if malware_error:
            raise ValueError(
                f"MCP server '{self.name}': {malware_error}"
            )

        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=safe_env if safe_env else None,
        )

        sampling_kwargs = self._sampling.session_kwargs() if self._sampling else {}
        if self._elicitation:
            sampling_kwargs.update(self._elicitation.session_kwargs())
        if _MCP_NOTIFICATION_TYPES and _MCP_MESSAGE_HANDLER_SUPPORTED:
            sampling_kwargs["message_handler"] = self._make_message_handler()

        # 在拉起前快照子进程 PID，以便追踪新增的子进程。
        pids_before = _snapshot_child_pids()
        new_pids: set = set()
        # 把子进程 stderr 重定向到共享日志文件，以免 MCP 服务器（FastMCP 横幅、
        # slack-mcp 启动 JSON 等）把内容倒到用户 TTY 上破坏 TUI。通过
        # ~/.hermes/logs/mcp-stderr.log 保留可调试性。
        _write_stderr_log_header(self.name)
        _errlog = _get_mcp_stderr_log()
        try:
            async with stdio_client(server_params, errlog=_errlog) as (
                read_stream,
                write_stream,
            ):
                # 捕获新拉起的子进程 PID，用于强制终止清理。
                new_pids = _snapshot_child_pids() - pids_before
                if new_pids:
                    # 在子进程存活时捕获 pgid——一旦它退出，我们就无法再对它调用
                    # ``os.getpgid``，而清理扫描需要 pgid 才能触达任何被重新
                    # 归父的子孙进程（例如由 stdio 封装拉起的
                    # ``claude mcp serve``）。
                    new_pgids: Dict[int, int] = {}
                    for _pid in new_pids:
                        try:
                            new_pgids[_pid] = os.getpgid(_pid)
                        except (AttributeError, ProcessLookupError, OSError):
                            # AttributeError：Windows（os.getpgid 仅 POSIX 可用）
                            # ProcessLookupError：子进程竞争性地已退出
                            pass
                    with _lock:
                        for _pid in new_pids:
                            _stdio_pids[_pid] = self.name
                        _stdio_pgids.update(new_pgids)
                async with ClientSession(
                    read_stream, write_stream, **sampling_kwargs
                ) as session:
                    self.initialize_result = await session.initialize()
                    self.session = session
                    await self._discover_tools()
                    self._ready.set()
                    # stdio 传输不使用 OAuth，但我们仍遵守 _reconnect_event
                    # （例如未来的手动 /mcp 刷新），以与 _run_http 保持一致。
                    await self._wait_for_lifecycle_event()
        finally:
            # 在正常退出、异常以及 asyncio 取消时都会运行。
            # 如果有任何被拉起的 PID 仍然存活，说明 SDK 的拆卸失败了（当任务在
            # Linux 上被中途取消时很常见，setsid() 子进程会脱离父 cgroup）。
            # 把它们标记为孤儿，以便下一次清理扫描能回收它们。
            if new_pids:
                from gateway.status import _pid_exists
                _killpg = getattr(os, "killpg", None)
                with _lock:
                    for _pid in new_pids:
                        _stdio_pids.pop(_pid, None)
                    for pid in new_pids:
                        # ``os.kill(pid, 0)`` 在 Windows 上并非无操作
                        # （bpo-14484）。使用跨平台检查。
                        pid_alive = _pid_exists(pid)
                        pgroup_alive = False
                        pgid = _stdio_pgids.get(pid)
                        if not pid_alive and pgid is not None and _killpg is not None:
                            # 直接子进程已退出，但其子孙可能仍在其 pgroup 中
                            # （例如先退出的 MCP 封装拉起的
                            # ``claude mcp serve``）。用信号 0 探测——仅当
                            # pgroup 中有成员存活时才成功。
                            try:
                                _killpg(pgid, 0)
                                pgroup_alive = True
                            except (ProcessLookupError, PermissionError, OSError):
                                pgroup_alive = False
                        if pid_alive or pgroup_alive:
                            _orphan_stdio_pids.add(pid)
                        else:
                            # 没有可回收的进程了——删除 pgid 条目，以免 PID 复用
                            # 在日后暴露陈旧的 pgroup 状态。
                            _stdio_pgids.pop(pid, None)

    # 真正的 MCP Streamable-HTTP 端点在初始 POST/GET 上可能返回的内容类型。
    # 在 2xx 响应上出现其他类型意味着该 URL 不是 MCP 端点。
    _MCP_CONTENT_TYPES = ("application/json", "text/event-stream")

    async def _preflight_content_type(
        self,
        url: str,
        *,
        headers: Optional[dict] = None,
        ssl_verify: bool = True,
        client_cert=None,
        timeout: float = 5.0,
    ) -> None:
        """在 SDK 连接之前，探测 *url* 是否返回 MCP 形态的响应。

        配置错误的 ``mcp_servers.<name>.url`` 若指向普通 Web 应用，会返回 HTML
        （或其他非 MCP 内容）。MCP SDK 随后会卡在连接上整整 ``connect_timeout``
        （默认 60s），最后才暴露一个晦涩的 ``CancelledError``。这里用一个廉价、
        短超时的探测在 ≤ ``timeout`` 秒内捕获这种情况，并抛出
        :class:`NonMcpEndpointError` 携带可操作的错误消息。

        检测基于白名单：仅当 2xx 响应带有明确且不属于 MCP 端点所使用的内容类型
        （``application/json`` / ``text/event-stream``）时才拒绝。缺失或空的内容
        类型、非 2xx 状态、或任何网络/传输错误都会静默放行——该探测严格属于尽力
        而为，真正的握手仍是除"这是一个网页而非 MCP"这种明确情况外所有判定的
        真相来源。

        在 SDK 的 anyio 任务组之外、用自己的 httpx 客户端上运行，因此抛出的错误
        会以其本身形态传播，而不是被包装在 ``ExceptionGroup`` 中（正是后者会
        击败安装在 SDK 传输内的钩子）。
        """
        try:
            import httpx as _httpx
        except ImportError:
            return  # 无 httpx → 跳过探测；SDK 导入会先失败。

        client_kwargs: dict = {
            "verify": ssl_verify,
            "follow_redirects": True,
            "timeout": _httpx.Timeout(timeout),
        }
        if client_cert is not None:
            client_kwargs["cert"] = client_cert

        probe_headers = dict(headers) if headers else {}
        try:
            async with _httpx.AsyncClient(**client_kwargs) as client:
                # HEAD 最廉价；若服务器不实现则回退到 GET
                # （405 Method Not Allowed / 501 Not Implemented）。
                resp = await client.head(url, headers=probe_headers)
                if resp.status_code in (405, 501):
                    resp = await client.get(url, headers=probe_headers)
        except _httpx.HTTPError:
            return  # DNS/连接/超时/传输错误——交给 SDK 尝试。

        # 只判断成功响应。4xx/5xx 可能是鉴权挑战，或真实握手能正确处理的瞬态
        # 错误。
        if not (200 <= resp.status_code < 300):
            return

        ct_base = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if not ct_base:
            return  # 未声明内容类型——不替 SDK 擅作主张。
        if ct_base in self._MCP_CONTENT_TYPES:
            return  # 看起来是真正的 MCP 端点。

        raise NonMcpEndpointError(
            f"MCP server '{self.name}' at {url} returned Content-Type "
            f"'{ct_base}', not an MCP response (expected one of: "
            f"{', '.join(self._MCP_CONTENT_TYPES)}). The URL most likely "
            "points at a web page rather than an MCP endpoint — check it "
            "resolves to a Streamable HTTP / SSE endpoint "
            "(e.g. https://host/mcp, not https://host/)."
        )

    async def _run_http(self, config: dict):
        """使用 HTTP/StreamableHTTP 传输运行服务器。"""
        if not _MCP_HTTP_AVAILABLE:
            raise ImportError(
                f"MCP server '{self.name}' requires HTTP transport but "
                "mcp.client.streamable_http is not available. "
                "Upgrade the mcp package to get HTTP support."
            )

        url = config["url"]
        headers = dict(config.get("headers") or {})
        # 某些 MCP 服务器要求在初始 initialize 请求上带 MCP-Protocol-Version，
        # 否则拒绝无会话的 POST。把它作为客户端级默认值注入，但对用户覆盖做
        # 大小写不敏感处理，以保留常规大小写。
        if not any(key.lower() == "mcp-protocol-version" for key in headers):
            headers["mcp-protocol-version"] = LATEST_PROTOCOL_VERSION
        connect_timeout = config.get("connect_timeout", _DEFAULT_CONNECT_TIMEOUT)
        ssl_verify = config.get("ssl_verify", True)
        client_cert = _resolve_client_cert(self.name, config)

        # OAuth 2.1 PKCE：通过中心化 MCPOAuthManager 路由，使同一个提供者实例
        # 在重连间复用、pre-flow 磁盘监听处于激活状态，且配置时的 CLI 代码路径
        # 共享状态。若 OAuth 设置失败（例如无缓存 token 的非交互环境），则重新
        # 抛出，使该服务器被报告为失败，但不阻塞其他 MCP 服务器连接。
        _oauth_auth = None
        if self._auth_type == "oauth":
            try:
                from tools.mcp_oauth_manager import get_manager
                _oauth_auth = get_manager().get_or_build_provider(
                    self.name, url, config.get("oauth"),
                )
            except Exception as exc:
                logger.warning("MCP OAuth setup failed for '%s': %s", self.name, exc)
                raise

        sampling_kwargs = self._sampling.session_kwargs() if self._sampling else {}
        if self._elicitation:
            sampling_kwargs.update(self._elicitation.session_kwargs())
        if _MCP_NOTIFICATION_TYPES and _MCP_MESSAGE_HANDLER_SUPPORTED:
            sampling_kwargs["message_handler"] = self._make_message_handler()

        # SSE 传输（用于实现 SSE 传输协议而非 Streamable HTTP 的 MCP 服务器）。
        # 在 config.yaml 的 mcp_servers 条目中用 ``transport: sse`` 配置。
        if config.get("transport") == "sse":
            if sse_client is None:
                raise ImportError(
                    f"MCP server '{self.name}' requires SSE transport but "
                    "mcp.client.sse.sse_client is not available. "
                    "Upgrade the mcp package to get SSE support."
                )
            # sse_read_timeout 控制 sse_client 在 SSE 流上两次事件之间等待多久。
            # 这里用 tool_timeout（默认 60s）是错的：SSE 服务器常在事件之间让流
            # 空闲数分钟，因此 60s 的读超时会在第一段慢区间后断开连接。300s 与
            # 下方 Streamable HTTP 代码路径的 httpx 读超时一致。原始观察来自
            # @amiller 在 PR #5981（Router Teamwork，Cloudflare Workers 上的
            # Supermemory 在约 60s 空闲断连）。
            _sse_kwargs: dict = {
                "url": url,
                "headers": headers or None,
                "timeout": float(connect_timeout),
                "sse_read_timeout": 300.0,
            }
            if _oauth_auth is not None:
                # 把 OAuth 鉴权传给 sse_client，使位于 OAuth 2.1 PKCE 之后的
                # SSE MCP 服务器能工作。此前构建了却从未转发——SSE OAuth 会静默
                # 失败并返回 401。
                _sse_kwargs["auth"] = _oauth_auth
            if client_cert is not None or ssl_verify is not True:
                # SSE 传输不把 verify/cert 作为 kwargs 暴露，因此通过一个
                # httpx_client_factory 路由它们——该工厂封装 SDK 默认值
                # （follow_redirects=True）并加上我们的 TLS 设置。SDK 用
                # (headers, auth, timeout) 调用该工厂；我们全部转发，并在其上
                # 叠加 verify/cert。
                import httpx as _httpx_mod

                _cert_for_factory = client_cert
                _verify_for_factory = ssl_verify

                def _mcp_http_client_factory(
                    headers=None, timeout=None, auth=None,
                ):
                    kwargs: dict = {
                        "follow_redirects": True,
                        "verify": _verify_for_factory,
                    }
                    if timeout is not None:
                        kwargs["timeout"] = timeout
                    else:
                        kwargs["timeout"] = _httpx_mod.Timeout(30.0, read=300.0)
                    if headers is not None:
                        kwargs["headers"] = headers
                    if auth is not None:
                        kwargs["auth"] = auth
                    if _cert_for_factory is not None:
                        kwargs["cert"] = _cert_for_factory
                    return _httpx_mod.AsyncClient(**kwargs)

                _sse_kwargs["httpx_client_factory"] = _mcp_http_client_factory
            async with sse_client(**_sse_kwargs) as (read_stream, write_stream):
                async with ClientSession(
                    read_stream, write_stream, **sampling_kwargs
                ) as session:
                    self.initialize_result = await session.initialize()
                    self.session = session
                    await self._discover_tools()
                    self._ready.set()
                    reason = await self._wait_for_lifecycle_event()
                    if reason == "reconnect":
                        logger.info(
                            "MCP server '%s': reconnect requested — "
                            "tearing down SSE session", self.name,
                        )
            return

        if _MCP_NEW_HTTP:
            # 新 API（mcp >= 1.24.0）：构建一个显式的 httpx.AsyncClient，与 SDK
            # 自身的 create_mcp_http_client 默认值匹配。
            import httpx

            _original_url = httpx.URL(url)

            async def _strip_auth_on_cross_origin_redirect(response):
                """当被重定向到不同源时，剥离 Authorization 头。"""
                if response.is_redirect and response.next_request:
                    target = response.next_request.url
                    if (target.scheme, target.host, target.port) != (
                        _original_url.scheme, _original_url.host, _original_url.port,
                    ):
                        response.next_request.headers.pop("authorization", None)
                        response.next_request.headers.pop("Authorization", None)

            client_kwargs: dict = {
                "follow_redirects": True,
                "timeout": httpx.Timeout(float(connect_timeout), read=300.0),
                "verify": ssl_verify,
                "event_hooks": {"response": [_strip_auth_on_cross_origin_redirect]},
            }
            if headers:
                client_kwargs["headers"] = headers
            if _oauth_auth is not None:
                client_kwargs["auth"] = _oauth_auth
            if client_cert is not None:
                client_kwargs["cert"] = client_cert

            # 调用方拥有客户端生命周期——SDK 在提供 http_client 时会跳过清理，
            # 因此我们用 async-with 包裹。
            async with httpx.AsyncClient(**client_kwargs) as http_client:
                async with streamable_http_client(url, http_client=http_client) as (
                    read_stream, write_stream, _get_session_id,
                ):
                    async with ClientSession(read_stream, write_stream, **sampling_kwargs) as session:
                        self.initialize_result = await session.initialize()
                        self.session = session
                        await self._discover_tools()
                        self._ready.set()
                        reason = await self._wait_for_lifecycle_event()
                        if reason == "reconnect":
                            logger.info(
                                "MCP server '%s': reconnect requested — "
                                "tearing down HTTP session", self.name,
                            )
        else:
            # 已弃用的 API（mcp < 1.24.0）：在内部管理 httpx 客户端。
            _http_kwargs: dict = {
                "headers": headers,
                "timeout": float(connect_timeout),
                "verify": ssl_verify,
            }
            if _oauth_auth is not None:
                _http_kwargs["auth"] = _oauth_auth
            async with streamablehttp_client(url, **_http_kwargs) as (
                read_stream, write_stream, _get_session_id,
            ):
                async with ClientSession(read_stream, write_stream, **sampling_kwargs) as session:
                    self.initialize_result = await session.initialize()
                    self.session = session
                    await self._discover_tools()
                    self._ready.set()
                    reason = await self._wait_for_lifecycle_event()
                    if reason == "reconnect":
                        logger.info(
                            "MCP server '%s': reconnect requested — "
                            "tearing down legacy HTTP session", self.name,
                        )

    async def _discover_tools(self):
        """从已连接的会话发现工具。

        按能力门控：仅 prompt / 仅 resource 的 MCP 服务器不实现
        ``tools/list``，调用它会抛出 ``McpError(-32601)``，这在过去会中止
        连接——这些服务器永远无法为其 prompts/resources 保持连接。当服务器
        不声明 ``tools`` 能力时跳过该调用。
        （移植自 anomalyco/opencode#31271。）
        """
        # 全新的传输连接 → 用廉价的 ``ping`` 路径重新探测。
        # 清除来自先前连接的锁存，以防服务器在重连后获得了 ping 支持。
        self._ping_unsupported = False
        if self.session is None:
            return
        if not self._advertises_tools():
            logger.info(
                "MCP server '%s': does not advertise 'tools' capability — "
                "skipping tools/list (prompts/resources remain available)",
                self.name,
            )
            self._tools = []
            return
        async with self._rpc_lock:
            tools_result = await self.session.list_tools()
        self._tools = (
            tools_result.tools
            if hasattr(tools_result, "tools")
            else []
        )

    async def run(self, config: dict):
        """长期存活的协程：连接、发现工具、等待、断开。

        若连接意外断开（除非请求了关闭），会以指数退避自动重连。
        """
        self._config = config
        self.tool_timeout = config.get("timeout", _DEFAULT_TOOL_TIMEOUT)
        self._auth_type = (config.get("auth") or "").lower().strip()

        # 若启用且 SDK 类型可用，则设置 sampling 处理器
        sampling_config = config.get("sampling", {})
        if sampling_config.get("enabled", True) and _MCP_SAMPLING_TYPES:
            self._sampling = SamplingHandler(self.name, sampling_config)
        else:
            self._sampling = None

        # 若启用且 SDK 类型可用，则设置 elicitation 处理器。
        # 服务器通过 elicitation/create 在工具调用过程中向客户端请求结构化输入
        # （例如支付授权）。该处理器把这些请求路由到 Hermes 的审批系统。
        elicitation_config = config.get("elicitation", {})
        if elicitation_config.get("enabled", True) and _MCP_ELICITATION_TYPES:
            self._elicitation = ElicitationHandler(self.name, elicitation_config, owner=self)
        else:
            self._elicitation = None

        # 校验：当 url 和 command 同时存在时发出警告
        if "url" in config and "command" in config:
            logger.warning(
                "MCP server '%s' has both 'url' and 'command' in config. "
                "Using HTTP transport ('url'). Remove 'command' to silence "
                "this warning.",
                self.name,
            )

        # 在前端一次性校验远程 URL。在此处抛出（而不是让它在每次重试时都在
        # SDK 的 httpx 层内爆炸），意味着 config.yaml 中的拼写错误会以清晰的
        # 错误快速失败——而且关键是不消耗重连退避。（移植自
        # anomalyco/opencode#25019。）
        if self._is_http():
            try:
                _validate_remote_mcp_url(self.name, config.get("url"))
            except InvalidMcpUrlError as exc:
                logger.warning("%s", exc)
                self._error = exc
                self._ready.set()
                return

            # 预检内容类型探测（仅 Streamable HTTP；SSE 由其自己的客户端驱动，
            # 合法地返回 text/event-stream）。指向 Web 应用根的 URL 会返回
            # HTML，使 SDK 卡住整个 connect_timeout 后才暴露一个晦涩的
            # CancelledError。在此处——在 SDK 任务组之外——探测一次，能以可
            # 操作的消息快速且不可重试地失败，与上方的 URL 校验路径一致。
            # 当 _ready 已设置时跳过探测：那只发生在先前成功连接之后，因此本次
            # run() 调用是一次重连（OAuth 恢复 / 手动刷新）。端点已被校验过一次；
            # 重新探测会在每次重连时对已知良好的服务器消耗一次多余的网络往返。
            if config.get("transport") != "sse" and not self._ready.is_set():
                try:
                    _probe_headers = dict(config.get("headers") or {})
                    await self._preflight_content_type(
                        config["url"],
                        headers=_probe_headers,
                        ssl_verify=config.get("ssl_verify", True),
                        client_cert=_resolve_client_cert(self.name, config),
                    )
                except NonMcpEndpointError as exc:
                    logger.warning("%s", exc)
                    self._error = exc
                    self._ready.set()
                    return

        retries = 0
        initial_retries = 0
        backoff = 1.0

        while True:
            try:
                if self._is_http():
                    await self._run_http(config)
                else:
                    await self._run_stdio(config)
                # 传输干净返回。两种情况：
                #  - _shutdown_event 被设置：完全退出运行循环。
                #  - _reconnect_event 被设置（鉴权恢复）：回到循环顶部，用新凭据
                #    重建 MCP 会话。不要动重试计数器——这不是失败。
                if self._shutdown_event.is_set():
                    break
                logger.info(
                    "MCP server '%s': reconnecting (OAuth recovery or "
                    "manual refresh)",
                    self.name,
                )
                # 重置会话引用；_run_http/_run_stdio 会在成功重新进入时重新填充。
                self.session = None
                # 重连期间保持 _ready 已设置，以便工具处理器仍能检测到瞬态的
                # 进行中状态——它会在新会话初始化后被重新设置。
                continue
            except asyncio.CancelledError:
                # 任务被取消（关闭、网关重启、显式 task.cancel()）。不要将其视为
                # 连接失败——在 Python 3.11+ 中 CancelledError 继承自
                # BaseException（而非 Exception），因此下方宽泛的
                # ``except Exception`` 不会捕获它；我们会静默退出重连循环，
                # MCP 服务器会一直死着，直到 Hermes 完全重启。重新抛出，使任务的
                # 取消正确传播到 asyncio 的任务机制，并让 ``shutdown()`` 的
                # ``await self._task`` 完成。见 #9930。
                self.session = None
                raise
            except Exception as exc:
                self.session = None

                # 如果这是首次连接尝试，则在放弃前以退避重试。启动时的瞬态
                # DNS/网络抖动不应永久杀死服务器。
                # （移植自 Kilo Code 的 MCP 韧性修复。）
                if not self._ready.is_set():
                    if _is_auth_error(exc):
                        logger.warning(
                            "MCP server '%s' failed initial OAuth authentication, "
                            "not retrying automatically: %s",
                            self.name, exc,
                        )
                        self._error = exc
                        self._ready.set()
                        return

                    initial_retries += 1
                    if initial_retries > _MAX_INITIAL_CONNECT_RETRIES:
                        logger.warning(
                            "MCP server '%s' failed initial connection after "
                            "%d attempts, giving up: %s",
                            self.name, _MAX_INITIAL_CONNECT_RETRIES, exc,
                        )
                        self._error = exc
                        self._ready.set()
                        return

                    logger.warning(
                        "MCP server '%s' initial connection failed "
                        "(attempt %d/%d), retrying in %.0fs: %s",
                        self.name, initial_retries,
                        _MAX_INITIAL_CONNECT_RETRIES, backoff, exc,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

                    # 检查在睡眠期间是否请求了关闭
                    if self._shutdown_event.is_set():
                        self._error = exc
                        self._ready.set()
                        return
                    continue

                # 若请求了关闭，则不重连
                if self._shutdown_event.is_set():
                    logger.debug(
                        "MCP server '%s' disconnected during shutdown: %s",
                        self.name, exc,
                    )
                    return

                retries += 1
                if retries > _MAX_RECONNECT_RETRIES:
                    logger.warning(
                        "MCP server '%s' failed after %d reconnection attempts, "
                        "giving up: %s",
                        self.name, _MAX_RECONNECT_RETRIES, exc,
                    )
                    return

                logger.warning(
                    "MCP server '%s' connection lost (attempt %d/%d), "
                    "reconnecting in %.0fs: %s",
                    self.name, retries, _MAX_RECONNECT_RETRIES,
                    backoff, exc,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

                # 睡眠后再次检查
                if self._shutdown_event.is_set():
                    return
            finally:
                self.session = None

    async def start(self, config: dict):
        """创建后台 Task 并等待直到就绪（或失败）。"""
        self._task = asyncio.ensure_future(self.run(config))
        await self._ready.wait()
        if self._error:
            raise self._error

    async def shutdown(self):
        """通知 Task 退出并等待资源被干净地拆除。"""
        from tools.registry import registry

        self._shutdown_event.set()
        # 防御性：若 _wait_for_lifecycle_event 正在阻塞，我们需要任意事件来解除
        # 阻塞。仅 _shutdown_event 就足够（辅助函数会先检查 shutdown），但同时
        # 设置 reconnect 可确保不会出现辅助函数在返回 "reconnect" 后错过关闭标志
        # 的竞争。
        self._reconnect_event.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except asyncio.TimeoutError:
                logger.warning(
                    "MCP server '%s' shutdown timed out, cancelling task",
                    self.name,
                )
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
        if self._pending_refresh_tasks:
            for task in list(self._pending_refresh_tasks):
                task.cancel()
            await asyncio.gather(*self._pending_refresh_tasks, return_exceptions=True)
            self._pending_refresh_tasks.clear()
        for tool_name in list(getattr(self, "_registered_tool_names", [])):
            registry.deregister(tool_name)
            _forget_mcp_tool_server(tool_name)
        self._registered_tool_names = []
        self.session = None


# ---------------------------------------------------------------------------
# 模块级状态
# ---------------------------------------------------------------------------

_servers: Dict[str, MCPServerTask] = {}
_server_connecting: set[str] = set()
_server_connect_errors: Dict[str, str] = {}

# 熔断器：每个服务器的连续错误计数。在连续失败达到
# _CIRCUIT_BREAKER_THRESHOLD 次后，处理器返回一条 "服务器不可达" 消息，告知
# 模型停止重试，以防止 #10447 中描述的 90 次迭代空转。
#
# 状态机：
#   closed（关闭）   —— 错误数低于阈值；所有调用都放行。
#   open（打开）     —— 达到阈值；调用短路，直到冷却时间结束。
#   half-open（半开）—— 冷却结束；下一次调用是真正打到会话的探测。探测成功
#                       → 关闭。探测失败 → 重新打开（冷却重新计时）。
#
# ``_server_breaker_opened_at`` 记录熔断器最近一次进入打开状态的单调时间戳。
# 使用 ``_bump_server_error`` / ``_reset_server_error`` 辅助函数修改此状态——
# 它们会保持计数和时间戳同步。
_server_error_counts: Dict[str, int] = {}
_server_breaker_opened_at: Dict[str, float] = {}
_CIRCUIT_BREAKER_THRESHOLD = 3
_CIRCUIT_BREAKER_COOLDOWN_SEC = 60.0


def _bump_server_error(server_name: str) -> None:
    """递增 ``server_name`` 的连续失败计数。

    当计数跨过 :data:`_CIRCUIT_BREAKER_THRESHOLD` 时，记录熔断器打开时间戳，
    以便冷却时钟启动（或在半开状态下探测失败时重新启动）。
    """
    n = _server_error_counts.get(server_name, 0) + 1
    _server_error_counts[server_name] = n
    if n >= _CIRCUIT_BREAKER_THRESHOLD:
        _server_breaker_opened_at[server_name] = time.monotonic()


def _reset_server_error(server_name: str) -> None:
    """完全关闭 ``server_name`` 的熔断器。

    同时清除失败计数和熔断器打开时间戳。在任何明确的成功信号（成功的工具调用、
    成功的重连、手动 /mcp 刷新）时调用。
    """
    _server_error_counts[server_name] = 0
    _server_breaker_opened_at.pop(server_name, None)

# ---------------------------------------------------------------------------
# 鉴权失败检测辅助函数（MCP OAuth 整合的任务 6）
# ---------------------------------------------------------------------------

# 鉴权相关异常类型的缓存元组。懒加载，以便在缺少 MCP SDK OAuth 模块时本模块
# 仍能干净导入。
_AUTH_ERROR_TYPES: tuple = ()


def _get_auth_error_types() -> tuple:
    """返回指示 MCP OAuth 失败的异常类型元组。

    首次调用后缓存。包括：
      - ``mcp.client.auth.OAuthFlowError`` / ``OAuthTokenError``——当发现、刷新
        或完整重新鉴权失败时由 SDK 的鉴权流程抛出。
      - ``mcp.client.auth.UnauthorizedError``（较旧的 MCP SDK）——作为可选导入
        保留，用于前向/后向兼容。
      - ``tools.mcp_oauth.OAuthNonInteractiveError``——当没有用户在场完成浏览器
        流程时，由我们的回调处理器抛出。
      - ``httpx.HTTPStatusError``——调用方必须额外通过 :func:`_is_auth_error`
        检查 ``status_code == 401``。
    """
    global _AUTH_ERROR_TYPES
    if _AUTH_ERROR_TYPES:
        return _AUTH_ERROR_TYPES
    types: list = []
    try:
        from mcp.client.auth import OAuthFlowError, OAuthTokenError
        types.extend([OAuthFlowError, OAuthTokenError])
    except ImportError:
        pass
    try:
        # 较旧的 MCP SDK 变体导出了这个
        from mcp.client.auth import UnauthorizedError  # type: ignore
        types.append(UnauthorizedError)
    except ImportError:
        pass
    try:
        from tools.mcp_oauth import OAuthNonInteractiveError
        types.append(OAuthNonInteractiveError)
    except ImportError:
        pass
    try:
        import httpx
        types.append(httpx.HTTPStatusError)
    except ImportError:
        pass
    _AUTH_ERROR_TYPES = tuple(types)
    return _AUTH_ERROR_TYPES


def _is_auth_error(exc: BaseException) -> bool:
    """当 ``exc`` 指示一次 MCP OAuth 失败时返回 True。

    ``httpx.HTTPStatusError`` 仅在响应状态码为 401 时才被视为鉴权相关。其他
    HTTP 错误会落入工具处理器中的通用错误路径。
    """
    types = _get_auth_error_types()
    if not types or not isinstance(exc, types):
        return False
    try:
        import httpx
        if isinstance(exc, httpx.HTTPStatusError):
            return getattr(exc.response, "status_code", None) == 401
    except ImportError:
        pass
    return True


def _handle_auth_error_and_retry(
    server_name: str,
    exc: BaseException,
    retry_call,
    op_description: str,
):
    """尝试鉴权恢复并重试一次；返回 None 以便落入后续处理。

    在 ``session.<op>()`` 抛出鉴权相关异常时，由 5 个 MCP 工具处理器调用。
    流程：

      1. 询问 :class:`tools.mcp_oauth_manager.MCPOAuthManager.handle_401` 恢复
         是否可行（即磁盘上有新 token，或 SDK 可原地刷新）。
      2. 若可行，设置服务器的 ``_reconnect_event``，使服务器任务拆除当前 MCP
         会话并用新凭据重建。短暂等待 ``_ready`` 重新触发。
      3. 重试操作一次。若重试产生了非错误的 JSON 负载则返回该结果。否则返回
         ``needs_reauth`` 错误字典，使模型停止幻想手动刷新。
      4. 若 ``exc`` 不是鉴权错误则返回 None，通知调用方走通用错误路径。

    参数：
        server_name: 抛出异常的 MCP 服务器名。
        exc: 失败的工具调用抛出的异常。
        retry_call: 无参可调用对象，重新运行工具调用，返回与处理器相同的
            JSON 字符串格式。
        op_description: 操作的人类可读名称（用于日志）。

    返回：
        若尝试了鉴权恢复则返回 JSON 字符串，否则返回 None 以落入调用方的
        通用错误路径。
    """
    if not _is_auth_error(exc):
        return None

    from tools.mcp_oauth_manager import get_manager
    manager = get_manager()

    async def _recover():
        return await manager.handle_401(server_name, None)

    try:
        recovered = _run_on_mcp_loop(_recover, timeout=10)
    except Exception as rec_exc:
        logger.warning(
            "MCP OAuth '%s': recovery attempt failed: %s",
            server_name, rec_exc,
        )
        recovered = False

    if recovered:
        with _lock:
            srv = _servers.get(server_name)
        if srv is not None and hasattr(srv, "_reconnect_event"):
            loop = _mcp_loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(srv._reconnect_event.set)

                # 短暂等待会话恢复就绪。设上限，使卡住的重连落入错误路径，
                # 而非挂起调用方。该异步辅助函数通过 _run_on_mcp_loop 运行在
                # MCP 事件循环上，因此在轮询间隔期间不会阻塞事件循环。
                async def _await_ready() -> bool:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        if srv.session is not None and srv._ready.is_set():
                            return True
                        await asyncio.sleep(0.25)
                    return False

                try:
                    _run_on_mcp_loop(_await_ready(), timeout=15)
                except Exception as exc:
                    logger.warning(
                        "MCP OAuth '%s': ready poll failed: %s",
                        server_name, exc,
                    )

        # 一次成功的 OAuth 恢复本身就是服务器重新可用的独立证据，因此在此处
        # 关闭熔断器——而不只是在重试成功时。若不这样做，一次重连后紧跟一次失败
        # 的重试会让熔断器永远卡在阈值之上（下方重试异常分支会再次递增计数）。
        # 重置后的重试在失败时仍会经过 _bump_server_error，因此真正坏掉的服务器
        # 会照常重新触发熔断器。
        _reset_server_error(server_name)

        try:
            result = retry_call()
            try:
                parsed = json.loads(result)
                if "error" not in parsed:
                    _reset_server_error(server_name)
                    return result
            except (json.JSONDecodeError, TypeError):
                _reset_server_error(server_name)
                return result
        except Exception as retry_exc:
            logger.warning(
                "MCP %s/%s retry after auth recovery failed: %s",
                server_name, op_description, retry_exc,
            )

    # 无可用恢复，或重试也失败：返回结构化的 needs_reauth 错误。
    # 递增熔断器计数，使模型停止重试该工具。
    _bump_server_error(server_name)
    return json.dumps({
        "error": (
            f"MCP server '{server_name}' requires re-authentication. "
            f"Run `hermes mcp login {server_name}` (or delete the tokens "
            f"file under ~/.hermes/mcp-tokens/ and restart). Do NOT retry "
            f"this tool — ask the user to re-authenticate."
        ),
        "needs_reauth": True,
        "server": server_name,
    }, ensure_ascii=False)


# 指示 MCP 服务器因其服务端传输会话过期/被垃圾回收而拒绝请求的子串
# （小写匹配）。调用方的 OAuth token 仍然有效——只需重建传输层会话状态。
# 见 #13383。
_SESSION_EXPIRED_MARKERS: tuple = (
    "invalid or expired session",
    "expired session",
    "session expired",
    "session not found",
    "unknown session",
    "session terminated",
    "closedresourceerror",
    "closed resource",
    "transport is closed",
    "connection closed",
    "broken pipe",
    "end of file",
)


def _is_session_expired_error(exc: BaseException) -> bool:
    """当 ``exc`` 看起来像 MCP 传输会话过期时返回 True。

    Streamable HTTP MCP 服务器可能在 OAuth token 仍然有效时垃圾回收服务端会话
    状态——空闲 TTL、服务器重启、横向扩缩容的 pod 轮换等。SDK 会将其作为
    JSON-RPC 错误暴露，消息中含有类似 ``"Invalid or expired session"`` 的
    短语。这类失败有别于 :func:`_is_auth_error`：重新跑 OAuth 刷新流程毫无意义，
    因为 access token 没问题。真正需要的是传输重连——拆除并重建
    ``streamablehttp_client`` + ``ClientSession`` 对，而这正是
    ``MCPServerTask._reconnect_event`` 触发的。
    """
    if isinstance(exc, InterruptedError):
        return False
    # 异常消息在 SDK 版本和服务器实现间差异很大，因此匹配一小撮稳定的子串
    # 白名单，而非异常类型。保持窄范围以避免对无关服务器错误的误报。
    msg = str(exc).lower()
    if not msg:
        return False
    return any(marker in msg for marker in _SESSION_EXPIRED_MARKERS)


def _handle_session_expired_and_retry(
    server_name: str,
    exc: BaseException,
    retry_call,
    op_description: str,
):
    """在会话过期时触发传输重连并重试一次。

    与 :func:`_handle_auth_error_and_retry` 不同，本函数**不会**调用 OAuth
    管理器的 ``handle_401``——access token 仍然有效，只是服务端会话状态陈旧。
    设置 ``_reconnect_event`` 会使服务器任务的生命周期循环拆除当前的
    ``streamablehttp_client`` + ``ClientSession`` 并重建它们，复用既有的 OAuth
    提供者实例。见 #13383。

    参数：
        server_name: 抛出异常的 MCP 服务器名。
        exc: 失败调用抛出的异常。
        retry_call: 无参可调用对象，重新运行该操作，返回与处理器相同的
            JSON 字符串格式。
        op_description: 操作的人类可读名称（日志）。

    返回：
        若尝试了重连 + 重试并产生了响应则返回 JSON 字符串，否则返回
        ``None`` 以落入调用方的通用错误路径（非会话过期错误、无服务器记录、
        重连未在时限内就绪、或重试也失败）。
    """
    if not _is_session_expired_error(exc):
        return None

    with _lock:
        srv = _servers.get(server_name)
    if srv is None or not hasattr(srv, "_reconnect_event"):
        return None

    loop = _mcp_loop
    if loop is None or not loop.is_running():
        return None

    logger.info(
        "MCP server '%s': %s failed with session-expired error (%s); "
        "signalling transport reconnect and retrying once.",
        server_name, op_description, exc,
    )

    # 触发与 OAuth 恢复路径相同的重连机制，然后短暂等待新会话恢复就绪。
    loop.call_soon_threadsafe(srv._reconnect_event.set)
    deadline = time.monotonic() + 15
    ready = False
    while time.monotonic() < deadline:
        if srv.session is not None and srv._ready.is_set():
            ready = True
            break
        time.sleep(0.25)
    if not ready:
        logger.warning(
            "MCP server '%s': reconnect did not ready within 15s after "
            "session-expired error; falling through to error response.",
            server_name,
        )
        return None

    try:
        result = retry_call()
        try:
            parsed = json.loads(result)
            if "error" not in parsed:
                _server_error_counts[server_name] = 0
                return result
        except (json.JSONDecodeError, TypeError):
            _server_error_counts[server_name] = 0
            return result
    except Exception as retry_exc:
        logger.warning(
            "MCP %s/%s retry after session reconnect failed: %s",
            server_name, op_description, retry_exc,
        )
    return None


# 已净化的、其 ``supports_parallel_tool_calls`` 配置为 True 的服务器名集合。
# 在 ``register_mcp_servers()`` 期间填充，由
# ``is_mcp_tool_parallel_safe()`` 查询，用于 run_agent 中的并行执行检查。
_parallel_safe_servers: set = set()

# MCP 工具名的精确来源。MCP 工具名格式为
# ``mcp_{sanitized_server}_{sanitized_tool}``，当服务器名含下划线时会有歧义
# （``mcp_a_b_tool`` 可能是服务器 ``a`` + 工具 ``b_tool``，也可能是服务器
# ``a_b`` + 工具 ``tool``）。在注册时捕获服务器部分，使并行安全性判断永远
# 不依赖前缀猜测。
_mcp_tool_server_names: Dict[str, str] = {}

# 运行在后台守护线程中的专用事件循环。
_mcp_loop: Optional[asyncio.AbstractEventLoop] = None
_mcp_thread: Optional[threading.Thread] = None

# 保护 _mcp_loop、_mcp_thread、_servers、MCP 连接状态映射、
# _parallel_safe_servers、_mcp_tool_server_names 以及 _stdio_pids。
_lock = threading.Lock()

# stdio MCP 服务器子进程的 PID。跟踪它们是为了在优雅清理（SDK 上下文管理器
# 拆除）失败或超时时，能在关闭时强制杀死。PID 在连接后添加，在正常服务器关闭
# 时移除。
_stdio_pids: Dict[int, str] = {}  # pid -> server_name

# 在其会话上下文退出后仍存活的 PID（SDK 拆除未能终止它们）。这些在 _run_stdio
# 的 finally 块中被检测到，可由 _kill_orphaned_mcp_children() 异步清理。
# 与 _stdio_pids 分开存放，以便清理扫描永远不会与活动会话（例如并发 cron 作业
# 或进行中的用户聊天）竞争。
_orphan_stdio_pids: set = set()

# stdio MCP 子进程的进程组 ID，在拉起时捕获。MCP SDK 用
# ``start_new_session=True`` 拉起 stdio 子进程，因此每个直接子进程都成为自己
# 的会话/进程组首领（PGID == 其自身 PID）。该子进程拉起的孙进程（例如一个
# 自身又拉起辅助子进程（如 ``claude mcp serve``）的封装 MCP 服务器）会继承该
# PGID，除非它们自己调用 ``setsid``。当直接子进程退出时，这些孙进程会被重新
# 归父到 init/systemd-user，但保留原 PGID，因此 ``killpg(pgid, sig)`` 仍能触达
# 它们。与 ``_stdio_pids`` 分开跟踪，以便即便直接子进程已退出并从活动映射中
# 移除后，我们仍保留 PGID。在 Windows 上为空（``os.getpgid`` 仅 POSIX 可用）。
_stdio_pgids: Dict[int, int] = {}  # pid -> pgid


def _snapshot_child_pids() -> set:
    """返回当前子进程 PID 的集合。

    在 Linux 上读取 /proc，回退到 psutil，再回退到空集合。
    供 _run_stdio 用于识别 stdio_client 拉起的子进程。
    """
    my_pid = os.getpid()

    # Linux：从 /proc 读取
    try:
        children_path = f"/proc/{my_pid}/task/{my_pid}/children"
        with open(children_path, encoding="utf-8") as f:
            return {int(p) for p in f.read().split() if p.strip()}
    except (FileNotFoundError, OSError, ValueError):
        pass

    # 回退：psutil
    try:
        import psutil
        return {c.pid for c in psutil.Process(my_pid).children()}
    except Exception:
        pass

    return set()


def _mcp_loop_exception_handler(loop, context):
    """在关闭期间抑制无害的 'Event loop is closed' 噪音。

    当 MCP 事件循环被停止并关闭时，httpx/httpcore 异步传输可能触发 __del__
    终结器，在其中调用已死循环的 call_soon()。asyncio 捕获该 RuntimeError
    并路由到这里。我们静默它，因为连接反正正在被拆除；所有其他异常都转发给
    默认处理器。
    """
    exc = context.get("exception")
    if isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc):
        return  # 无害的关闭竞争——抑制
    loop.default_exception_handler(context)


def _ensure_mcp_loop():
    """若后台事件循环线程尚未运行，则启动它。"""
    global _mcp_loop, _mcp_thread
    with _lock:
        if _mcp_loop is not None and _mcp_loop.is_running():
            return
        _mcp_loop = asyncio.new_event_loop()
        _mcp_loop.set_exception_handler(_mcp_loop_exception_handler)
        _mcp_thread = threading.Thread(
            target=_mcp_loop.run_forever,
            name="mcp-event-loop",
            daemon=True,
        )
        _mcp_thread.start()


def _wrap_with_home_override(coro: "Coroutine") -> "Coroutine":
    """把调用方上下文本地的 HERMES_HOME 覆盖带入 ``coro``。

    当没有活动的覆盖时原样返回 ``coro``。否则包裹它，使覆盖在协程自身的
    （任务本地的）上下文中、于 MCP 循环上被设置，并在完成时重置——携带不同
    作用域的并发调用互不干扰。
    """
    try:
        from hermes_constants import (
            get_hermes_home_override,
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        home_override = get_hermes_home_override()
    except Exception:
        return coro
    if not home_override:
        return coro

    async def _scoped():
        token = set_hermes_home_override(home_override)
        try:
            return await coro
        finally:
            reset_hermes_home_override(token)

    return _scoped()


def _run_on_mcp_loop(coro_or_factory, timeout: float = 30):
    """在 MCP 事件循环上调度一个协程并阻塞直到完成。

    接受一个协程对象，或一个返回协程的无参可调用对象。调用方可传入工厂以
    避免 在 MCP 循环不可用时构造协程对象（否则会泄漏协程帧并发出
    ``"coroutine was never awaited"`` 警告）。

    以短间隔轮询，使调用方 agent 线程能在 MCP 工作仍在后台循环上运行时响应
    用户中断。
    """
    from tools.interrupt import is_interrupted
    from agent.async_utils import safe_schedule_threadsafe

    with _lock:
        loop = _mcp_loop
    if loop is None or not loop.is_running():
        if asyncio.iscoroutine(coro_or_factory):
            coro_or_factory.close()
        raise RuntimeError("MCP event loop is not running")

    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory

    # 把上下文本地的 HERMES_HOME 覆盖传播到 MCP 循环上。
    # 通过 run_coroutine_threadsafe 调度的任务是在循环线程*内部*创建的，因此
    # 它们复制的是循环线程的上下文——而非调度线程的。一个按请求的 profile 作用域
    # （仪表盘的 ?profile= 端点，例如 MCP "Test server" 探测）会在此处静默消失：
    # 协程内的 OAuth token 存储以及任何其他 get_hermes_home() 解析都会读取进程
    # 主目录，而非所选 profile 的目录。在任务自身的上下文（任务本地——携带不同
    # 作用域的并发调用互不干扰）内重新建立该覆盖。无活动覆盖时为空操作。
    coro = _wrap_with_home_override(coro)

    future = safe_schedule_threadsafe(
        coro, loop,
        logger=logger,
        log_message="MCP scheduling failed",
    )
    if future is None:
        raise RuntimeError("MCP event loop unavailable (failed to schedule)")
    start_time = time.monotonic()
    deadline = None if timeout is None else start_time + timeout

    while True:
        if is_interrupted():
            future.cancel()
            raise InterruptedError("User sent a new message")

        wait_timeout = 0.1
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                future.cancel()
                elapsed = time.monotonic() - start_time
                raise TimeoutError(
                    f"MCP call timed out after {elapsed:.1f}s "
                    f"(configured timeout: {float(timeout):.1f}s)"
                )
            wait_timeout = min(wait_timeout, remaining)

        try:
            return future.result(timeout=wait_timeout)
        except concurrent.futures.TimeoutError:
            continue


def _interrupted_call_result() -> str:
    """用户中断的 MCP 工具调用所用的标准化 JSON 错误。"""
    return json.dumps({
        "error": "MCP call interrupted: user sent a new message"
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

def _interpolate_env_vars(value):
    """递归解析 ``${VAR}`` 占位符。

    在开启多路复用时，从活动 profile 的密钥作用域解析（使 MCP 服务器配置中的
    ``${API_KEY}`` 取到被路由 profile 的值，而非可能持有另一个 profile 值的
    进程级 ``os.environ``），否则回退到 ``os.environ``。未设置的变量保留字面量
    ``${VAR}`` 占位符，与之前一致。
    """
    from agent.secret_scope import get_secret as _get_secret

    if isinstance(value, str):
        def _replace(m):
            return _get_secret(m.group(1), m.group(0)) or m.group(0)
        return _ENV_VAR_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _interpolate_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env_vars(v) for v in value]
    return value


def _filter_suspicious_mcp_servers(servers: Dict[str, dict]) -> Dict[str, dict]:
    """在任何 stdio 拉起路径之前，丢弃具有数据外泄形态的 MCP 配置。"""
    try:
        from hermes_cli.mcp_security import validate_mcp_server_entry as _validate_mcp_server_entry
    except Exception:
        _validate_mcp_server_entry: Callable[[str, dict[str, Any]], list[str]] | None = None

    if _validate_mcp_server_entry is None:
        return servers

    safe_servers = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            safe_servers[name] = cfg
            continue
        issues = _validate_mcp_server_entry(name, cfg)
        if issues:
            logger.warning(
                "Skipping suspicious MCP server '%s': %s",
                name,
                "; ".join(issues),
            )
            continue
        safe_servers[name] = cfg
    return safe_servers


def _load_mcp_config() -> Dict[str, dict]:
    """从 Hermes 配置文件读取 ``mcp_servers``。

    返回 ``{server_name: server_config}`` 字典，或空字典。
    服务器配置可包含用于 stdio 传输的 ``command``/``args``/``env``，或用于
    HTTP 传输的 ``url``/``headers``，外加可选的 ``timeout``、
    ``connect_timeout`` 和 ``auth`` 覆盖。

    字符串值中的 ``${ENV_VAR}`` 占位符从 ``os.environ``（包括启动时加载的
    ``~/.hermes/.env``）解析。
    """
    try:
        from hermes_cli.config import load_config
        # 安全模式（--safe-mode / HERMES_SAFE_MODE=1）：禁用所有自定义的排错运行
        # ——无 MCP 服务器连接。
        from utils import env_var_enabled as _env_enabled
        if _env_enabled("HERMES_SAFE_MODE"):
            return {}
        config = load_config()
        servers = config.get("mcp_servers")
        if not servers or not isinstance(servers, dict):
            return {}
        # 确保 .env 变量可用于插值
        try:
            from hermes_cli.env_loader import load_hermes_dotenv
            load_hermes_dotenv()
        except Exception:
            pass
        safe_servers: Dict[str, dict] = {}
        for name, cfg in _filter_suspicious_mcp_servers(servers).items():
            interpolated = _interpolate_env_vars(cfg)
            if isinstance(interpolated, dict):
                safe_servers[name] = interpolated
        return safe_servers
    except Exception as exc:
        logger.debug("Failed to load MCP config: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# 服务器连接辅助
# ---------------------------------------------------------------------------

async def _connect_server(name: str, config: dict) -> MCPServerTask:
    """创建一个 MCPServerTask，启动它，并在就绪后返回。

    服务器 Task 会在后台保持连接存活。调用 ``server.shutdown()``（在同一个
    事件循环上）来拆除它。

    抛出：
        ValueError：缺少必需的配置键。
        ImportError：需要 HTTP 传输但不可用。
        Exception：连接或初始化失败。
    """
    server = MCPServerTask(name)
    await server.start(config)
    return server


# ---------------------------------------------------------------------------
# 处理器 / 检查函数工厂
# ---------------------------------------------------------------------------

def _make_tool_handler(server_name: str, tool_name: str, tool_timeout: float):
    """返回一个同步处理器，通过后台循环调用 MCP 工具。

    该处理器遵循注册表的分派接口：
    ``handler(args_dict, **kwargs) -> str``
    """

    def _handler(args: dict, **kwargs) -> str:
        # 熔断器：若该服务器连续失败次数过多，则以清晰消息短路，使模型停止
        # 重试并改用其他方法（#10447）。
        #
        # 冷却时间过后，熔断器转为半开：我们放行*下一次*调用作为探测。成功时
        # 下方的成功路径会重置熔断器；失败时下方的错误路径会再次递增计数，
        # 这会通过 _bump_server_error 重新标记打开时间（重新计时冷却）。
        if _server_error_counts.get(server_name, 0) >= _CIRCUIT_BREAKER_THRESHOLD:
            opened_at = _server_breaker_opened_at.get(server_name, 0.0)
            age = time.monotonic() - opened_at
            if age < _CIRCUIT_BREAKER_COOLDOWN_SEC:
                remaining = max(1, int(_CIRCUIT_BREAKER_COOLDOWN_SEC - age))
                return json.dumps({
                    "error": (
                        f"MCP server '{server_name}' is unreachable after "
                        f"{_server_error_counts[server_name]} consecutive "
                        f"failures. Auto-retry available in ~{remaining}s. "
                        f"Do NOT retry this tool yet — use alternative "
                        f"approaches or ask the user to check the MCP server."
                    )
                }, ensure_ascii=False)
            # 冷却结束 → 作为半开探测放行。

        with _lock:
            server = _servers.get(server_name)
        if not server or not server.session:
            _bump_server_error(server_name)
            return json.dumps({
                "error": f"MCP server '{server_name}' is not connected"
            }, ensure_ascii=False)

        async def _call():
            async with server._rpc_lock:
                # 快照 agent 的上下文，以便本次调用期间触发的 elicitation 回调
                # （在 MCP recv 循环任务上触发，该任务不继承我们的 contextvars）
                # 能重放它并检测网关平台 / 会话以做路由。
                server._pending_call_context = contextvars.copy_context()
                try:
                    result = await server.session.call_tool(tool_name, arguments=args)
                finally:
                    server._pending_call_context = None
            # MCP CallToolResult 有 .content（内容块列表）和 .isError
            if result.isError:
                error_text = ""
                for block in (result.content or []):
                    if hasattr(block, "text"):
                        error_text += block.text
                return json.dumps({
                    "error": _sanitize_error(
                        error_text or "MCP tool returned an error"
                    )
                }, ensure_ascii=False)

            # 从内容块中收集文本。MCP 工具结果还可能包含 ImageContent 块
            # （截图 / Blockbench / Playwright 等）；通过网关的图像缓存辅助函数
            # 缓存它们，使其按 Hermes 的 MEDIA: 标签约定流转，并输出到原生渲染
            # 图像的消息适配器。若不这样做，图像块会被静默丢弃，agent 会得到
            # 空响应。
            #
            # 提炼自 #17915（c3115644151）和 #10848（gnanirahulnutakki），两者
            # 都过于陈旧无法 cherry-pick。#10848 的做法（与 Hermes 的 MEDIA 标签
            # + cache_image_from_bytes 集成）是两者中更干净的——接入既有基础设施。
            parts: List[str] = []
            for block in (result.content or []):
                if hasattr(block, "text") and block.text:
                    parts.append(block.text)
                    continue
                image_tag = _cache_mcp_image_block(block)
                if image_tag:
                    parts.append(image_tag)
            text_result = "\n".join(parts) if parts else ""

            # 当两者都存在时，合并 content + structuredContent。
            # MCP 规范：content 面向模型（文本），structuredContent 面向机器
            # （JSON 元数据）。对 AI agent 而言，content 是主要负载；
            # structuredContent 起补充作用。
            structured = getattr(result, "structuredContent", None)
            if structured is not None:
                if text_result:
                    return json.dumps({
                        "result": text_result,
                        "structuredContent": structured,
                    }, ensure_ascii=False)
                return json.dumps({"result": structured}, ensure_ascii=False)
            return json.dumps({"result": text_result}, ensure_ascii=False)

        def _call_once():
            return _run_on_mcp_loop(_call, timeout=tool_timeout)

        try:
            result = _call_once()
            # 检查 MCP 工具本身是否返回了错误
            try:
                parsed = json.loads(result)
                if "error" in parsed:
                    _bump_server_error(server_name)
                else:
                    _reset_server_error(server_name)  # 成功——重置
            except (json.JSONDecodeError, TypeError):
                _reset_server_error(server_name)  # 非 JSON = 成功
            return result
        except InterruptedError:
            return _interrupted_call_result()
        except Exception as exc:
            # 鉴权专用恢复路径：咨询管理器，可行则发出重连信号，重试一次。
            # 对非鉴权异常返回 None 以落入后续处理。
            recovered = _handle_auth_error_and_retry(
                server_name, exc, _call_once,
                f"tools/call {tool_name}",
            )
            if recovered is not None:
                return recovered

            # 传输会话过期（#13383）：相同的重连流程，但跳过 OAuth 恢复，因为
            # access token 仍然有效——只是服务端会话陈旧。
            recovered = _handle_session_expired_and_retry(
                server_name, exc, _call_once,
                f"tools/call {tool_name}",
            )
            if recovered is not None:
                return recovered

            _bump_server_error(server_name)
            logger.error(
                "MCP tool %s/%s call failed: %s",
                server_name, tool_name, exc,
            )
            return json.dumps({
                "error": _sanitize_error(
                    f"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}"
                )
            }, ensure_ascii=False)

    return _handler


def _make_list_resources_handler(server_name: str, tool_timeout: float):
    """返回一个同步处理器，用于列出 MCP 服务器的资源。"""

    def _handler(args: dict, **kwargs) -> str:
        with _lock:
            server = _servers.get(server_name)
        if not server or not server.session:
            return json.dumps({
                "error": f"MCP server '{server_name}' is not connected"
            }, ensure_ascii=False)

        async def _call():
            async with server._rpc_lock:
                result = await server.session.list_resources()
            resources = []
            for r in (result.resources if hasattr(result, "resources") else []):
                entry = {}
                if hasattr(r, "uri"):
                    entry["uri"] = str(r.uri)
                if hasattr(r, "name"):
                    entry["name"] = r.name
                if hasattr(r, "description") and r.description:
                    entry["description"] = r.description
                if hasattr(r, "mimeType") and r.mimeType:
                    entry["mimeType"] = r.mimeType
                resources.append(entry)
            return json.dumps({"resources": resources}, ensure_ascii=False)

        def _call_once():
            return _run_on_mcp_loop(_call, timeout=tool_timeout)

        try:
            return _call_once()
        except InterruptedError:
            return _interrupted_call_result()
        except Exception as exc:
            recovered = _handle_auth_error_and_retry(
                server_name, exc, _call_once, "resources/list",
            )
            if recovered is not None:
                return recovered
            recovered = _handle_session_expired_and_retry(
                server_name, exc, _call_once, "resources/list",
            )
            if recovered is not None:
                return recovered
            logger.error(
                "MCP %s/list_resources failed: %s", server_name, exc,
            )
            return json.dumps({
                "error": _sanitize_error(
                    f"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}"
                )
            }, ensure_ascii=False)

    return _handler


def _make_read_resource_handler(server_name: str, tool_timeout: float):
    """返回一个同步处理器，用于按 URI 从 MCP 服务器读取资源。"""

    def _handler(args: dict, **kwargs) -> str:
        from tools.registry import tool_error

        with _lock:
            server = _servers.get(server_name)
        if not server or not server.session:
            return json.dumps({
                "error": f"MCP server '{server_name}' is not connected"
            }, ensure_ascii=False)

        uri = args.get("uri")
        if not uri:
            return tool_error("Missing required parameter 'uri'")

        async def _call():
            async with server._rpc_lock:
                result = await server.session.read_resource(uri)
            # read_resource 返回 ReadResourceResult，带 .contents 列表
            parts: List[str] = []
            contents = result.contents if hasattr(result, "contents") else []
            for block in contents:
                if hasattr(block, "text"):
                    parts.append(block.text)
                elif hasattr(block, "blob"):
                    parts.append(f"[binary data, {len(block.blob)} bytes]")
            return json.dumps({"result": "\n".join(parts) if parts else ""}, ensure_ascii=False)

        def _call_once():
            return _run_on_mcp_loop(_call, timeout=tool_timeout)

        try:
            return _call_once()
        except InterruptedError:
            return _interrupted_call_result()
        except Exception as exc:
            recovered = _handle_auth_error_and_retry(
                server_name, exc, _call_once, "resources/read",
            )
            if recovered is not None:
                return recovered
            recovered = _handle_session_expired_and_retry(
                server_name, exc, _call_once, "resources/read",
            )
            if recovered is not None:
                return recovered
            logger.error(
                "MCP %s/read_resource failed: %s", server_name, exc,
            )
            return json.dumps({
                "error": _sanitize_error(
                    f"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}"
                )
            }, ensure_ascii=False)

    return _handler


def _make_list_prompts_handler(server_name: str, tool_timeout: float):
    """返回一个同步处理器，用于列出 MCP 服务器的 prompts。"""

    def _handler(args: dict, **kwargs) -> str:
        with _lock:
            server = _servers.get(server_name)
        if not server or not server.session:
            return json.dumps({
                "error": f"MCP server '{server_name}' is not connected"
            }, ensure_ascii=False)

        async def _call():
            async with server._rpc_lock:
                result = await server.session.list_prompts()
            prompts = []
            for p in (result.prompts if hasattr(result, "prompts") else []):
                entry = {}
                if hasattr(p, "name"):
                    entry["name"] = p.name
                if hasattr(p, "description") and p.description:
                    entry["description"] = p.description
                if hasattr(p, "arguments") and p.arguments:
                    entry["arguments"] = [
                        {
                            "name": a.name,
                            **({"description": a.description} if hasattr(a, "description") and a.description else {}),
                            **({"required": a.required} if hasattr(a, "required") else {}),
                        }
                        for a in p.arguments
                    ]
                prompts.append(entry)
            return json.dumps({"prompts": prompts}, ensure_ascii=False)

        def _call_once():
            return _run_on_mcp_loop(_call, timeout=tool_timeout)

        try:
            return _call_once()
        except InterruptedError:
            return _interrupted_call_result()
        except Exception as exc:
            recovered = _handle_auth_error_and_retry(
                server_name, exc, _call_once, "prompts/list",
            )
            if recovered is not None:
                return recovered
            recovered = _handle_session_expired_and_retry(
                server_name, exc, _call_once, "prompts/list",
            )
            if recovered is not None:
                return recovered
            logger.error(
                "MCP %s/list_prompts failed: %s", server_name, exc,
            )
            return json.dumps({
                "error": _sanitize_error(
                    f"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}"
                )
            }, ensure_ascii=False)

    return _handler


def _make_get_prompt_handler(server_name: str, tool_timeout: float):
    """返回一个同步处理器，用于按名称从 MCP 服务器获取 prompt。"""

    def _handler(args: dict, **kwargs) -> str:
        from tools.registry import tool_error

        with _lock:
            server = _servers.get(server_name)
        if not server or not server.session:
            return json.dumps({
                "error": f"MCP server '{server_name}' is not connected"
            }, ensure_ascii=False)

        name = args.get("name")
        if not name:
            return tool_error("Missing required parameter 'name'")
        arguments = args.get("arguments", {})

        async def _call():
            async with server._rpc_lock:
                result = await server.session.get_prompt(name, arguments=arguments)
            # GetPromptResult 有 .messages 列表
            messages = []
            for msg in (result.messages if hasattr(result, "messages") else []):
                entry = {}
                if hasattr(msg, "role"):
                    entry["role"] = msg.role
                if hasattr(msg, "content"):
                    content = msg.content
                    if hasattr(content, "text"):
                        entry["content"] = content.text
                    elif isinstance(content, str):
                        entry["content"] = content
                    else:
                        entry["content"] = str(content)
                messages.append(entry)
            resp = {"messages": messages}
            if hasattr(result, "description") and result.description:
                resp["description"] = result.description
            return json.dumps(resp, ensure_ascii=False)

        def _call_once():
            return _run_on_mcp_loop(_call, timeout=tool_timeout)

        try:
            return _call_once()
        except InterruptedError:
            return _interrupted_call_result()
        except Exception as exc:
            recovered = _handle_auth_error_and_retry(
                server_name, exc, _call_once, "prompts/get",
            )
            if recovered is not None:
                return recovered
            recovered = _handle_session_expired_and_retry(
                server_name, exc, _call_once, "prompts/get",
            )
            if recovered is not None:
                return recovered
            logger.error(
                "MCP %s/get_prompt failed: %s", server_name, exc,
            )
            return json.dumps({
                "error": _sanitize_error(
                    f"MCP call failed: {type(exc).__name__}: {_exc_str(exc)}"
                )
            }, ensure_ascii=False)

    return _handler


def _make_check_fn(server_name: str):
    """返回一个检查函数，用于校验 MCP 连接是否存活。"""

    def _check() -> bool:
        with _lock:
            server = _servers.get(server_name)
        return server is not None and server.session is not None

    return _check


# ---------------------------------------------------------------------------
# 发现与注册
# ---------------------------------------------------------------------------

def _normalize_mcp_input_schema(schema: dict | None) -> dict:
    """规范化 MCP 输入 schema，以兼容 LLM 工具调用。

    MCP 服务器可能发出带 ``definitions`` / ``#/definitions/...`` 引用的普通
    JSON Schema。Kimi / Moonshot 拒绝这种形式，要求本地引用指向
    ``#/$defs/...``。在此规范化常见的 draft-07 形态，使 MCP 工具 schema 在各
    OpenAI 兼容提供者间保持可移植。

    额外递归应用的 MCP 服务器健壮性修复：

    * 对象形态节点上缺失或为 ``null`` 的 ``type`` 被强制为 ``"object"``（某些
      服务器会省略它）。见 PR #4897。
    * 当 ``object`` 节点缺少 ``properties`` 时，补充一个空的 ``properties``
      字典，使 ``required`` 条目不悬空。
    * ``required`` 数组被修剪为只保留 ``properties`` 中存在的名字；否则
      Google AI Studio / Gemini 会以 ``property is not defined`` 返回 400。
      见 PR #4651。
    * MCP/Pydantic 的可选字段通常以
      ``anyOf: [{...}, {"type": "null"}], default: null`` 形式到来。Anthropic
      拒绝工具输入 schema 中的可空分支，因此可空联合被折叠为非空分支，可空性
      仅通过父对象的 ``required`` 列表来表示。

    所有修复都与提供者无关，理想情况下能一次产出在 OpenAI、Anthropic、Gemini
    和 Moonshot 上都有效的 schema。
    """
    if not schema:
        return {"type": "object", "properties": {}}

    def _rewrite_local_refs(node):
        if isinstance(node, dict):
            normalized = {}
            for key, value in node.items():
                out_key = "$defs" if key == "definitions" else key
                normalized[out_key] = _rewrite_local_refs(value)
            ref = normalized.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/definitions/"):
                normalized["$ref"] = "#/$defs/" + ref[len("#/definitions/"):]
            return normalized
        if isinstance(node, list):
            return [_rewrite_local_refs(item) for item in node]
        return node

    def _strip_nullable_union(node):
        """把 JSON Schema 的可空联合折叠为对提供者安全的非空 schema。

        委托给 ``tools.schema_sanitizer.strip_nullable_unions``，使 MCP 摄取、
        Anthropic 守卫和全局净化器共享同一实现。保留 ``nullable: true`` 提示，
        以便运行时参数强制转换仍能针对此可选字段，把模型发出的 ``"null"`` 字符串
        映射为 Python ``None``。
        """
        from tools.schema_sanitizer import strip_nullable_unions

        return strip_nullable_unions(node, keep_nullable_hint=True)

    def _repair_object_shape(node):
        """递归修复对象形态的节点：补全 type，修剪 required。"""
        if isinstance(node, list):
            return [_repair_object_shape(item) for item in node]
        if not isinstance(node, dict):
            return node

        repaired = {k: _repair_object_shape(v) for k, v in node.items()}

        # 当形态明显是对象（有 properties 或 required 但没有 type）时，把缺失/
        # 为 null 的 type 强制为 object。
        if not repaired.get("type") and (
            "properties" in repaired or "required" in repaired
        ):
            repaired["type"] = "object"

        if repaired.get("type") == "object":
            # 确保 properties 存在，使 required 能安全引用
            if "properties" not in repaired or not isinstance(
                repaired.get("properties"), dict
            ):
                repaired["properties"] = {} if "properties" not in repaired else repaired["properties"]
                if not isinstance(repaired.get("properties"), dict):
                    repaired["properties"] = {}

            # 把 required 修剪为只保留 properties 中存在的名字
            required = repaired.get("required")
            if isinstance(required, list):
                props = repaired.get("properties") or {}
                valid = [r for r in required if isinstance(r, str) and r in props]
                if len(valid) != len(required):
                    if valid:
                        repaired["required"] = valid
                    else:
                        repaired.pop("required", None)

        return repaired

    normalized = _rewrite_local_refs(schema)
    normalized = _strip_nullable_union(normalized)
    normalized = _repair_object_shape(normalized)

    # 确保顶层是一个格式良好的对象 schema
    if not isinstance(normalized, dict):
        return {"type": "object", "properties": {}}
    if normalized.get("type") == "object" and "properties" not in normalized:
        normalized = {**normalized, "properties": {}}

    return normalized


def sanitize_mcp_name_component(value: str) -> str:
    """返回一个安全的 MCP 名称组件，可用于工具名和前缀生成。

    保留 Hermes 把连字符转换为下划线的历史行为，同时把 ``[A-Za-z0-9_]`` 之外
    的任何字符替换为 ``_``，使生成的工具名与提供者的校验规则兼容。
    """
    return re.sub(r"[^A-Za-z0-9_]", "_", str(value or ""))


def _convert_mcp_schema(server_name: str, mcp_tool) -> dict:
    """把 MCP 工具列表转换为 Hermes 注册表的 schema 格式。

    参数：
        server_name: 用于前缀的逻辑服务器名。
        mcp_tool:    一个 MCP ``Tool`` 对象，带 ``.name``、``.description``
                     和 ``.inputSchema``。

    返回：
        适合 ``registry.register(schema=...)`` 的字典。
    """
    safe_tool_name = sanitize_mcp_name_component(mcp_tool.name)
    safe_server_name = sanitize_mcp_name_component(server_name)
    prefixed_name = f"mcp_{safe_server_name}_{safe_tool_name}"
    return {
        "name": prefixed_name,
        "description": mcp_tool.description or f"MCP tool {mcp_tool.name} from {server_name}",
        "parameters": _normalize_mcp_input_schema(getattr(mcp_tool, "inputSchema", None)),
    }


def _build_utility_schemas(server_name: str) -> List[dict]:
    """为 MCP 实用工具（resources 和 prompts）构建 schema。

    返回 (schema, handler_factory_name) 元组列表，编码为带 schema、handler_key
    键的字典。
    """
    safe_name = sanitize_mcp_name_component(server_name)
    return [
        {
            "schema": {
                "name": f"mcp_{safe_name}_list_resources",
                "description": f"List available resources from MCP server '{server_name}'",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
            "handler_key": "list_resources",
        },
        {
            "schema": {
                "name": f"mcp_{safe_name}_read_resource",
                "description": f"Read a resource by URI from MCP server '{server_name}'",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "uri": {
                            "type": "string",
                            "description": "URI of the resource to read",
                        },
                    },
                    "required": ["uri"],
                },
            },
            "handler_key": "read_resource",
        },
        {
            "schema": {
                "name": f"mcp_{safe_name}_list_prompts",
                "description": f"List available prompts from MCP server '{server_name}'",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
            "handler_key": "list_prompts",
        },
        {
            "schema": {
                "name": f"mcp_{safe_name}_get_prompt",
                "description": f"Get a prompt by name from MCP server '{server_name}'",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the prompt to retrieve",
                        },
                        "arguments": {
                            "type": "object",
                            "description": "Optional arguments to pass to the prompt",
                            "properties": {},
                            "additionalProperties": True,
                        },
                    },
                    "required": ["name"],
                },
            },
            "handler_key": "get_prompt",
        },
    ]


def _normalize_name_filter(value: Any, label: str) -> set[str]:
    """把 include/exclude 配置规范化为工具名集合。"""
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple, set)):
        return {str(item) for item in value}
    logger.warning("MCP config %s must be a string or list of strings; ignoring %r", label, value)
    return set()


def _parse_boolish(value: Any, default: bool = True) -> bool:
    """解析一个类布尔配置值，带回退安全保护。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    logger.warning("MCP config expected a boolean-ish value, got %r; using default=%s", value, default)
    return default


_UTILITY_CAPABILITY_METHODS = {
    "list_resources": "list_resources",
    "read_resource": "read_resource",
    "list_prompts": "list_prompts",
    "get_prompt": "get_prompt",
}

# 把每个实用工具处理器映射到：在服务器的 ``initialize`` 响应上必须非 None 的
# MCP 能力键，该处理器才会被注册。
# 真相来源：MCP 规范——capabilities.resources / capabilities.prompts 仅在服务器
# 真正实现这些请求族时才出现在响应上。没有这个门控，仅工具的服务器（例如
# Context7 @upstash/context7-mcp，只声明 ``tools``）会被注册全部四个实用工具
# 桩，而模型对它们的每次调用都会返回 JSON-RPC ``-32601 Method not found``，
# 使模型即便在真正的工具能工作时也认为服务器坏了。见 #18051。
_UTILITY_CAPABILITY_ATTRS = {
    "list_resources": "resources",
    "read_resource": "resources",
    "list_prompts": "prompts",
    "get_prompt": "prompts",
}


def _track_mcp_tool_server(tool_name: str, server_name: str) -> None:
    """记住注册了 *tool_name* 的确切 MCP 服务器。"""
    safe_server_name = sanitize_mcp_name_component(server_name)
    with _lock:
        _mcp_tool_server_names[tool_name] = safe_server_name


def _forget_mcp_tool_server(tool_name: str) -> None:
    """遗忘已注销工具的 MCP 服务器来源。"""
    with _lock:
        _mcp_tool_server_names.pop(tool_name, None)


def _select_utility_schemas(server_name: str, server: MCPServerTask, config: dict) -> List[dict]:
    """根据配置和服务器能力选择实用工具 schema。"""
    tools_filter = config.get("tools") or {}
    resources_enabled = _parse_boolish(tools_filter.get("resources"), default=True)
    prompts_enabled = _parse_boolish(tools_filter.get("prompts"), default=True)

    # ``initialize_result.capabilities`` 是真相来源：它的子对象
    # （``resources``、``prompts``）非 None 当且仅当服务器声明了该请求族。
    # ``hasattr(server.session, ...)`` 是旧的门控，但 ClientSession 总是在类上
    # 定义了这四个方法属性，因此它从未过滤掉任何东西。
    advertised_caps = None
    init_result = getattr(server, "initialize_result", None)
    if init_result is not None:
        advertised_caps = getattr(init_result, "capabilities", None)

    selected: List[dict] = []
    for entry in _build_utility_schemas(server_name):
        handler_key = entry["handler_key"]
        if handler_key in {"list_resources", "read_resource"} and not resources_enabled:
            logger.debug("MCP server '%s': skipping utility '%s' (resources disabled)", server_name, handler_key)
            continue
        if handler_key in {"list_prompts", "get_prompt"} and not prompts_enabled:
            logger.debug("MCP server '%s': skipping utility '%s' (prompts disabled)", server_name, handler_key)
            continue

        # 首选门控：检查服务器声明的能力。若能力被显式声明为不存在则跳过。
        if advertised_caps is not None:
            cap_attr = _UTILITY_CAPABILITY_ATTRS[handler_key]
            if getattr(advertised_caps, cap_attr, None) is None:
                logger.debug(
                    "MCP server '%s': skipping utility '%s' "
                    "(server does not advertise '%s' capability)",
                    server_name,
                    handler_key,
                    cap_attr,
                )
                continue
        else:
            # 针对未捕获 initialize_result 的测试夹具或较旧代码路径的遗留回退。
            # 在那种情况下保留注册每个桩的旧行为，而不是让在此修复之前能工作的
            # 服务器出现回归。
            required_method = _UTILITY_CAPABILITY_METHODS[handler_key]
            if not hasattr(server.session, required_method):
                logger.debug(
                    "MCP server '%s': skipping utility '%s' (session lacks %s)",
                    server_name,
                    handler_key,
                    required_method,
                )
                continue
        selected.append(entry)
    return selected


def _existing_tool_names() -> List[str]:
    """返回所有当前已连接服务器的工具名。"""
    names: List[str] = []
    for _sname, server in _servers.items():
        if hasattr(server, "_registered_tool_names"):
            names.extend(server._registered_tool_names)
            continue
        for mcp_tool in server._tools:
            schema = _convert_mcp_schema(server.name, mcp_tool)
            names.append(schema["name"])
    return names


def _register_server_tools(name: str, server: MCPServerTask, config: dict) -> List[str]:
    """把已连接服务器的工具注册到注册表中。

    处理 include/exclude 过滤和实用工具。``mcp-{server}`` 和原始服务器名别名的
    工具集解析派生自实时注册表，而非在运行时修改 ``toolsets.TOOLSETS``。

    同时用于初始发现和动态刷新（list_changed）。

    返回：
        已注册的带前缀工具名列表。
    """
    from tools.registry import registry

    registered_names: List[str] = []
    toolset_name = f"mcp-{name}"

    # 选择性工具加载：遵守配置中的 include/exclude 列表。
    # 规则（对应 issue #690 规范）：
    #   tools.include —— 白名单：仅注册这些工具名
    #   tools.exclude —— 黑名单：注册除这些之外的所有工具
    #   include 优先于 exclude
    #   两者都未设置 → 注册所有工具（向后兼容的默认值）
    tools_filter = config.get("tools") or {}
    include_set = _normalize_name_filter(tools_filter.get("include"), f"mcp_servers.{name}.tools.include")
    exclude_set = _normalize_name_filter(tools_filter.get("exclude"), f"mcp_servers.{name}.tools.exclude")

    def _should_register(tool_name: str) -> bool:
        if include_set:
            return tool_name in include_set
        if exclude_set:
            return tool_name not in exclude_set
        return True

    for mcp_tool in server._tools:
        if not _should_register(mcp_tool.name):
            logger.debug("MCP server '%s': skipping tool '%s' (filtered by config)", name, mcp_tool.name)
            continue

        # 扫描工具描述中的提示词注入模式
        _scan_mcp_description(name, mcp_tool.name, mcp_tool.description or "")

        schema = _convert_mcp_schema(name, mcp_tool)
        tool_name_prefixed = schema["name"]

        # 防止与内置（非 MCP）工具冲突。
        existing_toolset = registry.get_toolset_for_tool(tool_name_prefixed)
        if existing_toolset and not existing_toolset.startswith("mcp-"):
            logger.warning(
                "MCP server '%s': tool '%s' (→ '%s') collides with built-in "
                "tool in toolset '%s' — skipping to preserve built-in",
                name, mcp_tool.name, tool_name_prefixed, existing_toolset,
            )
            continue

        registry.register(
            name=tool_name_prefixed,
            toolset=toolset_name,
            schema=schema,
            handler=_make_tool_handler(name, mcp_tool.name, server.tool_timeout),
            check_fn=_make_check_fn(name),
            is_async=False,
            description=schema["description"],
        )
        _track_mcp_tool_server(tool_name_prefixed, name)
        registered_names.append(tool_name_prefixed)

    # 注册 MCP 的 Resources 与 Prompts 实用工具，按配置过滤，且仅在服务器实际
    # 支持相应能力时注册。
    _handler_factories = {
        "list_resources": _make_list_resources_handler,
        "read_resource": _make_read_resource_handler,
        "list_prompts": _make_list_prompts_handler,
        "get_prompt": _make_get_prompt_handler,
    }
    check_fn = _make_check_fn(name)
    for entry in _select_utility_schemas(name, server, config):
        schema = entry["schema"]
        handler_key = entry["handler_key"]
        handler = _handler_factories[handler_key](name, server.tool_timeout)
        util_name = schema["name"]

        # 实用工具采用相同的冲突防护。
        existing_toolset = registry.get_toolset_for_tool(util_name)
        if existing_toolset and not existing_toolset.startswith("mcp-"):
            logger.warning(
                "MCP server '%s': utility tool '%s' collides with built-in "
                "tool in toolset '%s' — skipping to preserve built-in",
                name, util_name, existing_toolset,
            )
            continue

        registry.register(
            name=util_name,
            toolset=toolset_name,
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            is_async=False,
            description=schema["description"],
        )
        _track_mcp_tool_server(util_name, name)
        registered_names.append(util_name)

    if registered_names:
        registry.register_toolset_alias(name, toolset_name)

    return registered_names


async def _discover_and_register_server(name: str, config: dict) -> List[str]:
    """连接单个 MCP 服务器，发现工具并注册。

    返回已注册工具名列表。
    """
    connect_timeout = config.get("connect_timeout", _DEFAULT_CONNECT_TIMEOUT)
    server = await asyncio.wait_for(
        _connect_server(name, config),
        timeout=connect_timeout,
    )
    with _lock:
        _server_connecting.discard(name)
        _server_connect_errors.pop(name, None)
        _servers[name] = server

    registered_names = _register_server_tools(name, server, config)
    server._registered_tool_names = list(registered_names)

    transport_type = "HTTP" if "url" in config else "stdio"
    logger.info(
        "MCP server '%s' (%s): registered %d tool(s): %s",
        name, transport_type, len(registered_names),
        ", ".join(registered_names),
    )
    return registered_names


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------

def register_mcp_servers(servers: Dict[str, dict]) -> List[str]:
    """连接显式指定的 MCP 服务器并注册其工具。

    对已连接的服务器名幂等。``enabled: false`` 的服务器会被跳过，但不会断开
    既有会话。

    参数：
        servers: ``{server_name: server_config}`` 映射。

    返回：
        当前所有已注册 MCP 工具名的列表。
    """
    if not _MCP_AVAILABLE:
        logger.debug("MCP SDK not available -- skipping explicit MCP registration")
        return []

    servers = _filter_suspicious_mcp_servers(servers)
    if not servers:
        logger.debug("No explicit MCP servers provided")
        return []

    # 只尝试尚未连接且已启用的服务器
    # （enabled: false 会完全跳过该服务器，但不移除其配置）
    with _lock:
        new_servers = {
            k: v
            for k, v in servers.items()
            if k not in _servers and _parse_boolish(v.get("enabled", True), default=True)
        }
        _server_connecting.update(new_servers)
        for srv_name in new_servers:
            _server_connect_errors.pop(srv_name, None)
        # 跟踪哪些服务器选择了并行工具调用（幂等）。
        for srv_name, srv_cfg in servers.items():
            if _parse_boolish(srv_cfg.get("supports_parallel_tool_calls", False), default=False):
                _parallel_safe_servers.add(sanitize_mcp_name_component(srv_name))
            else:
                _parallel_safe_servers.discard(sanitize_mcp_name_component(srv_name))

    if not new_servers:
        return _existing_tool_names()

    # 为 MCP 连接启动后台事件循环
    _ensure_mcp_loop()

    async def _discover_one(name: str, cfg: dict) -> List[str]:
        """连接单个服务器并返回其已注册的工具名。"""
        return await _discover_and_register_server(name, cfg)

    async def _discover_all():
        server_names = list(new_servers.keys())
        # 并行连接所有服务器
        results = await asyncio.gather(
            *(_discover_one(name, cfg) for name, cfg in new_servers.items()),
            return_exceptions=True,
        )
        for name, result in zip(server_names, results):
            if isinstance(result, BaseException):
                command = new_servers.get(name, {}).get("command")
                message = _format_connect_error(result)
                with _lock:
                    _server_connecting.discard(name)
                    _server_connect_errors[name] = message
                logger.warning(
                    "Failed to connect to MCP server '%s'%s: %s",
                    name,
                    f" (command={command})" if command else "",
                    message,
                )
            else:
                with _lock:
                    _server_connecting.discard(name)
                    _server_connect_errors.pop(name, None)

    # 按服务器的超时在 _discover_and_register_server 内部处理。
    # 外层超时较宽松：并行发现总计 120s。
    #
    # 临时清除当前线程的中断标志，使 MCP 发现永远不会被来自先前 agent 会话的
    # 陈旧中断取消（执行器线程会被复用，可能携带旧的中断状态）。
    from tools.interrupt import is_interrupted as _is_interrupted, set_interrupt as _set_interrupt
    _was_interrupted = _is_interrupted()
    if _was_interrupted:
        _set_interrupt(False)
    try:
        _run_on_mcp_loop(_discover_all, timeout=120)
    finally:
        if _was_interrupted:
            _set_interrupt(True)

    # 记录汇总，使 ACP 调用方能了解注册了什么。
    with _lock:
        connected = [n for n in new_servers if n in _servers]
        new_tool_count = sum(
            len(getattr(_servers[n], "_registered_tool_names", []))
            for n in connected
        )
    failed = len(new_servers) - len(connected)
    if new_tool_count or failed:
        summary = f"MCP: registered {new_tool_count} tool(s) from {len(connected)} server(s)"
        if failed:
            summary += f" ({failed} failed)"
        logger.info(summary)

    return _existing_tool_names()


def discover_mcp_tools() -> List[str]:
    """入口点：加载配置、连接 MCP 服务器、注册工具。

    在 ``discover_builtin_tools()`` 之后由 ``model_tools`` 调用。即使未安装
    ``mcp`` 包也可安全调用（返回空列表）。

    对已连接的服务器幂等。若部分服务器在上一次调用中失败，则只重试缺失的
    服务器。

    返回：
        所有已注册 MCP 工具名的列表。
    """
    if not _MCP_AVAILABLE:
        logger.debug("MCP SDK not available -- skipping MCP tool discovery")
        return []

    servers = _load_mcp_config()
    if not servers:
        logger.debug("No MCP servers configured")
        return []

    with _lock:
        new_server_names = [
            name
            for name, cfg in servers.items()
            if name not in _servers and _parse_boolish(cfg.get("enabled", True), default=True)
        ]

    tool_names = register_mcp_servers(servers)
    if not new_server_names:
        return tool_names

    with _lock:
        connected_server_names = [name for name in new_server_names if name in _servers]
        new_tool_count = sum(
            len(getattr(_servers[name], "_registered_tool_names", []))
            for name in connected_server_names
        )

    failed_count = len(new_server_names) - len(connected_server_names)
    if new_tool_count or failed_count:
        summary = f"  MCP: {new_tool_count} tool(s) from {len(connected_server_names)} server(s)"
        if failed_count:
            summary += f" ({failed_count} failed)"
        logger.info(summary)

    return tool_names


def is_mcp_tool_parallel_safe(tool_name: str) -> bool:
    """检查一个 MCP 工具是否属于支持并行工具调用的服务器。

    MCP 工具名遵循 ``mcp_{server}_{tool}`` 模式，但当服务器名含下划线时该
    字符串形态会有歧义。使用注册时捕获的确切服务器来源，而非前缀匹配，然后
    检查该服务器的配置是否包含 ``supports_parallel_tool_calls: true``。

    对非 MCP 工具或来自未设置该标志的服务器的工具返回 False。
    """
    if not tool_name.startswith("mcp_"):
        return False
    with _lock:
        server_name = _mcp_tool_server_names.get(tool_name)
        return bool(server_name and server_name in _parallel_safe_servers)


def get_mcp_status() -> List[dict]:
    """返回所有已配置 MCP 服务器的状态，用于横幅展示。

    返回一个字典列表，键为：name、transport、tools、connected、disabled 和
    status。包括已连接的服务器、已禁用的服务器、进行中的连接尝试、记录的失败，
    以及已配置但本进程尚未启动的服务器。
    """
    result: List[dict] = []

    # 从配置获取已配置的服务器
    configured = _load_mcp_config()
    if not configured:
        return result

    with _lock:
        active_servers = dict(_servers)
        connecting = set(_server_connecting)
        connect_errors = dict(_server_connect_errors)

    for name, cfg in configured.items():
        transport = cfg.get("transport", "http") if "url" in cfg else "stdio"
        enabled = _parse_boolish(cfg.get("enabled", True), default=True)
        server = active_servers.get(name)
        if server and server.session is not None:
            entry = {
                "name": name,
                "transport": transport,
                "tools": len(server._registered_tool_names) if hasattr(server, "_registered_tool_names") else len(server._tools),
                "connected": True,
                "disabled": False,
                "status": "connected",
            }
            if server._sampling:
                entry["sampling"] = dict(server._sampling.metrics)
            result.append(entry)
        elif not enabled:
            # enabled: false 的服务器是有意不连接的——它是被禁用，而非失败。
            # 呈现这一区别，使消费方（横幅、TUI）能渲染 "disabled" 而非令人警觉
            # 的 "failed"。
            result.append({
                "name": name,
                "transport": transport,
                "tools": 0,
                "connected": False,
                "disabled": True,
                "status": "disabled",
            })
        elif name in connecting:
            result.append({
                "name": name,
                "transport": transport,
                "tools": 0,
                "connected": False,
                "disabled": False,
                "status": "connecting",
            })
        elif name in connect_errors:
            result.append({
                "name": name,
                "transport": transport,
                "tools": 0,
                "connected": False,
                "disabled": False,
                "status": "failed",
                "error": connect_errors[name],
            })
        else:
            result.append({
                "name": name,
                "transport": transport,
                "tools": 0,
                "connected": False,
                "disabled": False,
                "status": "configured",
            })

    return result


def probe_mcp_server_tools() -> Dict[str, List[tuple]]:
    """临时连接已配置的 MCP 服务器并列出其工具。

    专为 ``hermes tools`` 交互式配置设计——连接每个已启用的服务器，抓取工具
    名和描述，然后断开。不会在 Hermes 注册表中注册工具。

    返回：
        服务器名到 (tool_name, description) 元组列表的映射字典。
        连接失败的服务器会从结果中省略。
    """
    if not _MCP_AVAILABLE:
        return {}

    servers_config = _load_mcp_config()
    if not servers_config:
        return {}

    enabled = {
        k: v for k, v in servers_config.items()
        if _parse_boolish(v.get("enabled", True), default=True)
    }
    if not enabled:
        return {}

    _ensure_mcp_loop()

    result: Dict[str, List[tuple]] = {}
    probed_servers: List[MCPServerTask] = []

    async def _probe_all():
        names = list(enabled.keys())
        coros = []
        for name, cfg in enabled.items():
            ct = cfg.get("connect_timeout", _DEFAULT_CONNECT_TIMEOUT)
            coros.append(asyncio.wait_for(_connect_server(name, cfg), timeout=ct))

        outcomes = await asyncio.gather(*coros, return_exceptions=True)

        for name, outcome in zip(names, outcomes):
            if isinstance(outcome, Exception):
                logger.debug("Probe: failed to connect to '%s': %s", name, outcome)
                continue
            probed_servers.append(outcome)
            tools = []
            for t in outcome._tools:
                desc = getattr(t, "description", "") or ""
                tools.append((t.name, desc))
            result[name] = tools

        # 关闭所有探测连接
        await asyncio.gather(
            *(s.shutdown() for s in probed_servers),
            return_exceptions=True,
        )

    try:
        _run_on_mcp_loop(_probe_all, timeout=120)
    except Exception as exc:
        logger.debug("MCP probe failed: %s", exc)
    finally:
        _stop_mcp_loop_if_idle()

    return result


# 串行化对 agent 工具快照的原地修改。重载 RPC、网关重载和延迟绑定刷新线程
# 都会在 agent 构建之后交换 ``agent.tools`` / ``agent.valid_tool_names``；
# agent 的运行循环在工具迭代期间读取它们，因此读期间的并发写入可能会暴露一个
# 更新了一半的列表。
_agent_tools_lock = threading.Lock()


def has_registered_mcp_tools() -> bool:
    """若任意 MCP 服务器已实际把工具注册到注册表中则返回 True。

    廉价操作——在 ``_lock`` 下检查全局的 MCP-工具→服务器名映射，无需遍历注册表。
    供每轮刷新钩子使用，使没有 MCP 工具的会话（常见情况，也包括连接但零工具/
    仅 prompt 的服务器）完全跳过 ``get_tool_definitions`` 重建。检查的是已注册的
    工具而非已连接的服务器，因此注册了零工具的服务器不会让钩子每轮都触发。
    """
    with _lock:
        return bool(_mcp_tool_server_names)


def refresh_agent_mcp_tools(
    agent,
    *,
    enabled_override=None,
    disabled_override=None,
    quiet_mode: bool = True,
) -> set:
    """从实时注册表重新推导一个已构建 agent 的工具快照。

    agent 在构建时对 ``agent.tools`` 做一次快照，之后再也不重新读取注册表
    （见 ``run_agent`` / ``agent_init``）。当 MCP 服务器在该快照*之后*连接——
    一个慢速的 HTTP/OAuth 服务器错过了有界的启动等待，或一次 ``/reload-mcp``——
    它们的工具在快照重建前是不可见的。这是所有此类调用方（TUI 的
    ``reload.mcp`` RPC、网关重载、延迟绑定刷新线程，以及每轮的轮间刷新）使用的
    唯一共享重建，使它们不会再产生分歧。

    重建遵守 agent 自身的 ``enabled_toolsets`` / ``disabled_toolsets``
    （与构建时相同的过滤），并按工具**名**做 diff（而非按数量——按数量比较会漏掉
    等量的增删交换）。

    关键在于它是**增量保留**的：``get_tool_definitions`` 只返回注册表派生的工具，
    但 ``agent_init`` 在那*之后*又直接把两个额外的工具族追加到 ``agent.tools``
    上——外部 memory-provider 工具（mem0/honcho/…）和 context-engine 工具
    （``lcm_*``）。天真的 ``agent.tools = get_tool_definitions(...)`` 会静默
    删除它们。因此，在重建注册表集合后，我们会重新运行 ``agent_init`` 使用的
    相同构建后注入器，重建完整的工具表面。新的 ``(tools, valid_tool_names)``
    对在 ``_agent_tools_lock`` 下一起发布，使并发读者永远不会看到跨属性的半交换。

    返回新增的工具名集合（无变化时为空），以便调用方决定是否通知用户 / 重新发送
    会话信息。调用方拥有提示词缓存契约：本辅助函数不检查轮次状态，因为每个调用
    方有不同的策略（``/reload-mcp`` 在用户明确同意后重建；延迟绑定和轮间路径只
    在轮次边界、即该轮的 ``tools=`` 前缀组装之前重建）。
    """
    from model_tools import get_tool_definitions
    from tools.registry import registry

    # 显式重载（/reload-mcp）会传入新解析的工具集，以便用户刚在配置中启用的服务器
    # 被拾取；随后 agent 存储的选择会被更新以匹配。自动路径（轮间、延迟绑定）不
    # 传入任何内容，并原样复用 agent 的构建时选择。
    if enabled_override is not None or disabled_override is not None:
        enabled = enabled_override if enabled_override is not None else getattr(agent, "enabled_toolsets", None)
        disabled = disabled_override if disabled_override is not None else getattr(agent, "disabled_toolsets", None)
        agent.enabled_toolsets = enabled
        agent.disabled_toolsets = disabled
    else:
        enabled = getattr(agent, "enabled_toolsets", None)
        disabled = getattr(agent, "disabled_toolsets", None)

    # 在（可能很慢的）get_tool_definitions 调用*之前*，捕获本次重建所派生的注册表
    # 代次。用于在发布时拒绝陈旧写入：若两个调用方竞争（例如延迟刷新守护进程与
    # 第 1 轮附近的轮间序言），算出更旧集合的较慢调用方不得覆盖另一个调用方已发布
    # 的更新集合。``registry._generation`` 在每次（注）销时递增。
    snapshot_generation = registry._generation

    # 注册表派生的工具（内置 + MCP），按 agent 的工具集过滤。
    # 在锁之外计算（get_tool_definitions 可能很慢）；下方的 diff 与发布在*一个*
    # 临界区内一起完成，使两个并发调用方不会出现撕裂发布或计算出重叠的 ``added``
    # 集合。
    new_defs = list(
        get_tool_definitions(
            enabled_toolsets=enabled,
            disabled_toolsets=disabled,
            quiet_mode=quiet_mode,
        )
        or []
    )
    new_names = {t["function"]["name"] for t in new_defs}

    # 重新追加 get_tool_definitions 不会重现的构建后注入工具族，使刷新永远不会
    # 剥离它们（memory-provider + context-engine 工具）。完全暂存在局部变量上——
    # 实时的 ``agent.tools`` / ``valid_tool_names`` /
    # ``_context_engine_tool_names`` 在下方单次原子发布前绝不会被触碰，使并发
    # 读者（``build_api_kwargs``）看不到部分重建或跨属性的半交换。
    # ``staged_engine_names`` 是本次重建实际追加的 context-engine 路由名
    # （与 agent_init 的去重感知添加一致）。
    staged_engine_names = _reinject_post_build_tools(agent, new_defs, new_names)

    # 单次原子地读-diff-发布，使返回的 ``added`` 与实际发布的内容一致（即便在
    # 并发调用方下），且陈旧的（更旧代次的）重建不会覆盖较新的已发布版本。
    with _agent_tools_lock:
        # 防御性：已发布的代次应为 int，但要容忍从未设置它（或设置了非 int 值，
        # 例如测试 mock）的 agent，而不是在比较时抛出 TypeError 并静默失败整个
        # 刷新。
        published_gen_raw = getattr(agent, "_tool_snapshot_generation", -1)
        published_gen = published_gen_raw if isinstance(published_gen_raw, int) else -1
        if snapshot_generation < published_gen:
            # 更新的快照已胜出；我们的集合陈旧——丢弃。
            return set()
        current = {
            t["function"]["name"]
            for t in (getattr(agent, "tools", None) or [])
        }
        if new_names == current:
            # 无变化 → 保持实时快照不变（不抖动），但记录代次，使进行中的较旧
            # 调用方无法覆盖。
            agent._tool_snapshot_generation = max(published_gen, snapshot_generation)
            return set()
        agent.tools = new_defs
        agent.valid_tool_names = new_names
        # 与快照一起原子地发布 context-engine 路由名。
        engine_names = getattr(agent, "_context_engine_tool_names", None)
        if isinstance(engine_names, set):
            engine_names.clear()
            engine_names.update(staged_engine_names)
        agent._tool_snapshot_generation = max(published_gen, snapshot_generation)
        return new_names - current


def _reinject_post_build_tools(agent, tools_list: list, name_set: set) -> set:
    """把 memory-provider 和 context-engine 工具追加到暂存的局部变量上。

    镜像 ``agent_init`` 中 ``get_tool_definitions`` 之后的注入，使快照重建能
    重构完整的工具表面，而不仅仅是注册表派生的子集。只在调用方暂存的
    ``tools_list`` / ``name_set`` 上操作（绝不动实时的 agent 属性），使重建保持
    原子性。幂等（跳过已存在的名字）且失败柔和。

    返回本次重建实际追加的 context-engine 路由名集合——与 ``agent_init`` 的去重
    行为一致（已由注册表/插件工具提供的名字不会被认领为 context-engine 路由）。
    调用方会把它与快照一起原子地发布到 ``agent._context_engine_tool_names``。
    """
    def _add(schema: dict) -> bool:
        name = schema.get("name", "")
        if not name or name in name_set:
            return False
        tools_list.append({"type": "function", "function": schema})
        name_set.add(name)
        return True

    # memory-provider 工具（mem0/honcho/byterover/supermemory/…）。
    try:
        memory_manager = getattr(agent, "_memory_manager", None)
        get_mem_schemas = getattr(memory_manager, "get_all_tool_schemas", None) if memory_manager else None
        if callable(get_mem_schemas):
            # 遵守与 inject_memory_provider_tools 相同的启用门控。
            from agent.memory_manager import memory_provider_tools_enabled
            if "memory" in name_set or memory_provider_tools_enabled(getattr(agent, "enabled_toolsets", None)):
                for schema in get_mem_schemas():
                    if isinstance(schema, dict):
                        _add(schema)
    except Exception:
        logger.debug("Memory-provider tool re-injection skipped", exc_info=True)

    # context-engine 工具（lcm_grep/lcm_describe/…）——`context_engine` 工具集
    # 被刻意留空，因此它们只能通过这里的追加存在。遵守与 agent_init 相同的
    # enabled_toolsets 门控（#5544）：没有它，一个受限工具集的平台（例如
    # platform_toolsets: telegram: []）会重新泄漏构建时刻意排除的 lcm_* 工具，
    # 并付出本地模型的延迟代价。
    staged_engine_names: set = set()
    try:
        enabled = getattr(agent, "enabled_toolsets", None)
        context_engine_allowed = enabled is None or "context_engine" in enabled
        compressor = getattr(agent, "context_compressor", None)
        get_schemas = getattr(compressor, "get_tool_schemas", None) if compressor else None
        if context_engine_allowed and callable(get_schemas):
            for schema in get_schemas():
                if not isinstance(schema, dict):
                    continue
                name = schema.get("name", "")
                # 仅当我们自己追加了 schema 时才认领路由名，使已由注册表/插件工具
                # 拥有的名字保持其自己的分派（与 agent_init.py 的
                # `continue`-before-claim 一致）。
                if _add(schema) and name:
                    staged_engine_names.add(name)
    except Exception:
        logger.debug("Context-engine tool re-injection skipped", exc_info=True)

    return staged_engine_names


def shutdown_mcp_servers():
    """关闭所有 MCP 服务器连接并停止后台循环。

    每个服务器 Task 都会收到退出其 ``async with`` 块的信号，以便 anyio 取消
    作用域的清理发生在打开它的同一个 Task 中。所有服务器通过
    ``asyncio.gather`` 并行关闭。
    """
    with _lock:
        servers_snapshot = list(_servers.values())

    # 快速路径：无需关闭。
    if not servers_snapshot:
        _stop_mcp_loop()
        return

    async def _shutdown():
        results = await asyncio.gather(
            *(server.shutdown() for server in servers_snapshot),
            return_exceptions=True,
        )
        for server, result in zip(servers_snapshot, results):
            if isinstance(result, Exception):
                logger.debug(
                    "Error closing MCP server '%s': %s", server.name, result,
                )
        with _lock:
            _servers.clear()

    with _lock:
        loop = _mcp_loop
    if loop is not None and loop.is_running():
        from agent.async_utils import safe_schedule_threadsafe
        future = safe_schedule_threadsafe(
            _shutdown(), loop,
            logger=logger,
            log_message="MCP shutdown: failed to schedule",
        )
        if future is not None:
            try:
                future.result(timeout=15)
            except BaseException as exc:
                logger.debug("Error during MCP shutdown: %s", exc)

    _stop_mcp_loop()


def _kill_orphaned_mcp_children(include_active: bool = False) -> None:
    """尽力优雅关闭 stdio MCP 子进程以回收孤儿。

    孤儿是指在会话上下文退出后仍存活的 PID（SDK 拆卸未终止进程——在取消时 stdio
    子进程脱离父 cgroup 的 Linux 上很常见）。默认只回收 ``_orphan_stdio_pids``
    中的条目，以免干扰并发的 cron 作业和进行中的用户会话。

    先发 SIGTERM，等待 2 秒，再对仍存活的进程升级为 SIGKILL，以避免多个 hermes
    进程在同一主机上运行时的共享资源冲突（每个都有自己的 ``_stdio_pids`` 字典）。

    在 POSIX 上，当跟踪到了拉起时的 pgid 时，通过 ``os.killpg`` 向该 pgid 发送
    信号，使同一进程组中被重新归父的孙进程（例如先退出的 stdio MCP 封装拉起的
    ``claude mcp serve``）随直接子进程一起被回收。在 Windows 上以及未记录 pgid
    时回退到 ``os.kill``。

    当 ``include_active=True`` 时，也会杀死 ``_stdio_pids`` 中的每个 PID——仅在
    最终关闭、MCP 事件循环已停止且不可能还有会话进行中时使用。
    """
    import signal as _signal

    with _lock:
        pids: Dict[int, str] = {}
        for opid in _orphan_stdio_pids:
            pids[opid] = "orphan"
        _orphan_stdio_pids.clear()
        if include_active:
            pids.update(dict(_stdio_pids))
            _stdio_pids.clear()
        # 为即将杀死的 pid 快照 pgid，然后删除条目，使未来的拉起不会与陈旧
        # 状态冲突。
        pgids: Dict[int, int] = {pid: _stdio_pgids[pid] for pid in pids if pid in _stdio_pgids}
        for pid in pgids:
            _stdio_pgids.pop(pid, None)

    # 快速路径：没有可回收的已跟踪 stdio PID。完全跳过 SIGTERM/睡眠/SIGKILL 流程
    # ——否则每次无 MCP 的关闭都要付出 2s 睡眠代价。
    if not pids:
        return

    # 预先计算网关自身的 pgid，以便 _send_signal 能避免杀死它。
    try:
        _my_pgid = os.getpgrp()
    except (AttributeError, OSError):
        _my_pgid = None  # Windows 或受限环境

    def _send_signal(pid: int, sig: int, server_name: str) -> None:
        """在 POSIX 上通过进程组发 SIGTERM/SIGKILL，否则回退到按 pid 发信号。"""
        pgid = pgids.get(pid)
        killpg = getattr(os, "killpg", None)
        if pgid is not None and killpg is not None:
            if _my_pgid is not None and pgid == _my_pgid:
                # MCP 子进程与网关共享同一进程组。使用 killpg 会把信号也发给网关，
                # 导致其崩溃（见 #47134）。回退到按 pid 的 kill() 路径。发出警告，
                # 因为按 pid 的 kill 无法触达此共享组中的孙进程——若直接子进程已
                # 退出，它们可能泄漏（固有局限：按组杀死它们也会杀死网关）。
                logger.warning(
                    "MCP server '%s' pgid %d matches gateway pgid; skipping "
                    "killpg to avoid self-kill and using per-pid kill — any "
                    "grandchildren in this group may not be reaped",
                    server_name, pgid,
                )
            else:
                try:
                    killpg(pgid, sig)
                    return
                except (ProcessLookupError, PermissionError, OSError) as exc:
                    # 进程组已不在（所有成员退出）或被拒绝——回退到按 pid 的路径，
                    # 以便在直接子进程仍存活时还能尝试。
                    logger.debug(
                        "killpg(%d, %d) failed for MCP server '%s': %s; falling back to kill(pid)",
                        pgid, sig, server_name, exc,
                    )
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    # 阶段 1：SIGTERM（优雅）
    for pid, server_name in pids.items():
        _send_signal(pid, _signal.SIGTERM, server_name)
        logger.debug("Sent SIGTERM to orphaned MCP process %d (%s)", pid, server_name)

    # 阶段 2：等待优雅退出
    time.sleep(2)

    # 阶段 3：对仍存活的进程发 SIGKILL
    _sigkill = getattr(_signal, "SIGKILL", _signal.SIGTERM)
    # ``os.kill(pid, 0)`` 在 Windows 上并非无操作。在升级为 SIGKILL 前使用
    # 跨平台的存在性检查。
    from gateway.status import _pid_exists
    for pid, server_name in pids.items():
        if not _pid_exists(pid):
            continue  # 很好——SIGTERM 后已退出
        _send_signal(pid, _sigkill, server_name)
        logger.warning(
            "Force-killed MCP process %d (%s) after SIGTERM timeout",
            pid, server_name,
        )


def _stop_mcp_loop_if_idle() -> bool:
    """仅当没有已注册服务器仍占用 MCP 循环时才停止它。

    探测路径会创建临时的 MCPServerTask 实例，它们不放入 ``_servers``。它们应清理
    一个本就空闲的循环，但在有实时 agent 工具注册其上时，绝不能拆除进程级循环。
    否则仪表盘/CLI 探测会让后续 MCP 工具调用以
    ``MCP event loop is not running`` 失败。
    """
    return _stop_mcp_loop(only_if_idle=True)


def _stop_mcp_loop(*, only_if_idle: bool = False) -> bool:
    """停止后台事件循环并 join 其线程。"""
    global _mcp_loop, _mcp_thread
    with _lock:
        if only_if_idle and (_servers or _server_connecting):
            logger.debug("Leaving MCP event loop running; active servers are registered or connecting")
            return False
        loop = _mcp_loop
        thread = _mcp_thread
        _mcp_loop = None
        _mcp_thread = None
    if loop is not None:
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)
        try:
            loop.close()
        except Exception:
            pass
        # 关闭循环后，任何在优雅关闭中幸存的 stdio 子进程现在都成了孤儿——
        # 因为循环已不在、不可能还有会话进行中，所以也把活动 PID 一并包含进来。
        _kill_orphaned_mcp_children(include_active=True)
    return True
