"""后台 MCP 发现的 CLI/TUI 安全辅助函数。"""

from __future__ import annotations

import threading
from typing import Optional

_mcp_discovery_lock = threading.Lock()
_mcp_discovery_started = False
_mcp_discovery_thread: Optional[threading.Thread] = None


def _has_configured_mcp_servers() -> bool:
    """轻量级 config 探测，避免非 MCP 用户导入 MCP 栈。"""
    try:
        from hermes_cli.config import read_raw_config

        mcp_servers = (read_raw_config() or {}).get("mcp_servers")
        return isinstance(mcp_servers, dict) and len(mcp_servers) > 0
    except Exception:
        # 保守策略：如果 config 探测失败，在后台尝试发现，
        # 这样启动仍然不会阻塞。
        return True


def start_background_mcp_discovery(*, logger, thread_name: str) -> None:
    """为此进程生成一个共享的后台 MCP 发现线程。"""
    global _mcp_discovery_started, _mcp_discovery_thread

    with _mcp_discovery_lock:
        if _mcp_discovery_started:
            return
        _mcp_discovery_started = True
        if not _has_configured_mcp_servers():
            return

        def _discover() -> None:
            try:
                from tools.mcp_tool import discover_mcp_tools

                discover_mcp_tools()
            except Exception:
                logger.debug("Background MCP tool discovery failed", exc_info=True)

        thread = threading.Thread(
            target=_discover,
            name=thread_name,
            daemon=True,
        )
        _mcp_discovery_thread = thread
        thread.start()


def _resolve_discovery_timeout(explicit: "float | None") -> float:
    """解析 MCP 发现等待超时：显式参数 > config > 默认值。

    从 config.yaml 读取 ``mcp_discovery_timeout``，当键缺失时默认为
    ``DEFAULT_CONFIG`` 中的值（单一真实来源）。保持惰性且安全——
    缺失/无效值或损坏的 config 会回退到较短的安全边界，
    这样启动永远不会挂起或崩溃。
    """
    if explicit is not None:
        return explicit
    try:
        from hermes_cli.config import load_config, DEFAULT_CONFIG

        default = float(DEFAULT_CONFIG.get("mcp_discovery_timeout", 1.5))
        raw = (load_config() or {}).get("mcp_discovery_timeout", default)
        val = float(raw)
        return val if val > 0 else default
    except Exception:
        return 1.5


def wait_for_mcp_discovery(timeout: "float | None" = None) -> None:
    """在第一次工具快照之前等待后台 MCP 发现完成。

    ``thread.join(timeout)`` 在发现完成的瞬间返回，所以这
    只会阻塞仍在等待的服务器的实际连接时间——
    没有 MCP 服务器或服务器响应快的用户等待约 0 秒。
    边界（来自 config 中的 ``mcp_discovery_timeout``）只是限制等待时间，
    这样挂掉的服务器不会冻结启动；错过边界的服务会被
    自动后期绑定刷新捕获。
    """
    thread = _mcp_discovery_thread
    if thread is None or not thread.is_alive():
        return
    thread.join(timeout=_resolve_discovery_timeout(timeout))
