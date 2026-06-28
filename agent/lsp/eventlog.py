"""为 LSP 层提供结构化日志，在稳定状态下保持静默。

LSP 层在每次 write_file/patch 时都会触发。在繁忙的会话中，
这意味着数百个事件。我们希望用户能够通过 ``rg`` 在日志中
搜索"LSP 是否在此次编辑中触发？"，而不会被噪音淹没。

日志级别模型：

- ``DEBUG``：无新信号的稳定状态事件：``clean``（干净）、
  ``feature off``（功能关闭）、``extension not mapped``（扩展名未映射）、
  ``no project root for already-announced file``（已宣告文件无项目根目录）、
  ``server unavailable for already-announced binary``（已宣告二进制程序的服务器不可用）。
  在默认 INFO 阈值下，这些不会写入 ``agent.log``。

- ``INFO``：每次会话只需展示一次的状态转换：
  第一次启动某个 (server_id, workspace_root) 客户端时的 ``active for <root>``，
  第一次遇到某个文件时的 ``no project root for <path>``。
  以及所有诊断事件（这些事件本质上很少见，且与每次编辑相关，
  正是用户会搜索的内容）。

- ``WARNING``：需要用户处理的失败：每个 (server_id, binary) 第一次出现的
  ``server unavailable``（服务器不可用，二进制程序不在 PATH 中），
  每种语言出现一次的 ``no server configured``（未配置服务器）。
  超时和意外桥接异常每次调用都会输出 WARNING。

去重通过进程内的模块级集合实现。每个集合的增长量最多为
单个 Python 进程中接触的不同 (server_id, root) 和 (server_id, binary)
对的数量——即使是高强度的单仓会话也只消耗字节级别的内存。
有界 LRU 方案被拒绝：驱逐条目可能导致我们明确想要抑制的
WARNING/INFO 行被重新触发。

Grep 配方::

    tail -f ~/.hermes/logs/agent.log | rg 'lsp\\['
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Tuple

# 使用专用 logger 名称，确保文档中的 grep 配方不会因任何内部模块
# 的 ``logging.getLogger(__name__)`` 重命名而失效。
event_log = logging.getLogger("hermes.lint.lsp")

# ---------------------------------------------------------------------------
# 每类事件的去重集合
# ---------------------------------------------------------------------------

_announce_lock = threading.Lock()
_announced_active: set = set()        # 键：(server_id, workspace_root)
_announced_unavailable: set = set()   # 键：(server_id, binary_path_or_name)
_announced_no_root: set = set()       # 键：(server_id, file_path)
_announced_no_server: set = set()     # 键：(server_id,)


def _short_path(file_path: str) -> str:
    """在合理时将 *file_path* 渲染为相对于 cwd 的路径，否则使用绝对路径。

    在常见情况（用户处于正在编辑的项目内部）下保持日志行的可读性，
    同时避免为跨目录树的情况输出脆弱的 ``../../..`` 链。
    """
    if not file_path:
        return file_path
    try:
        rel = os.path.relpath(file_path)
    except ValueError:
        return file_path
    if rel.startswith(".." + os.sep) or rel == "..":
        return file_path
    return rel


def _emit(server_id: str, level: int, message: str) -> None:
    event_log.log(level, "lsp[%s] %s", server_id, message)


def _announce_once(bucket: set, key: Tuple) -> bool:
    """如果 *key* 尚未在 *bucket* 中宣告过，则返回 True。

    原子性地将键标记为已宣告，防止并发调用者同时竞争成功而导致重复日志。
    """
    with _announce_lock:
        if key in bucket:
            return False
        bucket.add(key)
        return True


# ---------------------------------------------------------------------------
# 公共事件辅助函数——从 LSP 层调用这些函数。
# ---------------------------------------------------------------------------


def log_clean(server_id: str, file_path: str) -> None:
    """*file_path* 未产生任何诊断信息。DEBUG 级别（默认静默）。"""
    _emit(server_id, logging.DEBUG, f"clean ({_short_path(file_path)})")


def log_disabled(server_id: str, file_path: str, reason: str) -> None:
    """LSP 对此文件被有意跳过（功能关闭、扩展名未映射、后端非本地等）。DEBUG 级别。"""
    _emit(server_id, logging.DEBUG, f"skipped: {reason} ({_short_path(file_path)})")


def log_active(server_id: str, workspace_root: str) -> None:
    """为 (server_id, workspace_root) 启动了新的 LSP 客户端。

    每个 (server_id, workspace_root) 只输出一次 INFO；之后降为 DEBUG。
    让用户可以通过单次 grep 验证"LSP 是否真的在运行？"。
    """
    key = (server_id, workspace_root)
    if _announce_once(_announced_active, key):
        _emit(server_id, logging.INFO, f"active for {workspace_root}")
    else:
        _emit(server_id, logging.DEBUG, f"reused client for {workspace_root}")


def log_diagnostics(server_id: str, file_path: str, count: int) -> None:
    """某个文件收到了诊断信息。每次都输出 INFO——这是用户真正想搜索的
    失败信号，且每次编辑本质上很少见。"""
    _emit(server_id, logging.INFO, f"{count} diags ({_short_path(file_path)})")


def log_no_project_root(server_id: str, file_path: str) -> None:
    """文件没有识别到项目标记。每个文件第一次输出 INFO，之后降为 DEBUG。"""
    key = (server_id, file_path)
    if _announce_once(_announced_no_root, key):
        _emit(server_id, logging.INFO, f"no project root for {_short_path(file_path)}")
    else:
        _emit(server_id, logging.DEBUG, f"no project root for {_short_path(file_path)}")


def log_server_unavailable(server_id: str, binary_or_pkg: str) -> None:
    """服务器二进制程序无法解析。每个 (server_id, binary) 组合第一次输出 WARNING，
    之后降为 DEBUG，防止后续数百次 .py 编辑刷屏日志。"""
    key = (server_id, binary_or_pkg)
    if _announce_once(_announced_unavailable, key):
        _emit(
            server_id,
            logging.WARNING,
            f"server unavailable: {binary_or_pkg} not found "
            "(install via `hermes lsp install <id>` or set lsp.servers.<id>.command)",
        )
    else:
        _emit(server_id, logging.DEBUG, f"server still unavailable: {binary_or_pkg}")


def log_no_server_configured(server_id: str) -> None:
    """此语言没有可用的启动配方。只输出一次 WARNING。"""
    if _announce_once(_announced_no_server, (server_id,)):
        _emit(server_id, logging.WARNING, "no server configured")


def log_timeout(server_id: str, file_path: str, kind: str = "diagnostics") -> None:
    """对服务器的请求超时。每次都输出 WARNING——这些本质上是新事件，每次都值得记录。"""
    _emit(
        server_id,
        logging.WARNING,
        f"{kind} timed out for {_short_path(file_path)}",
    )


def log_server_error(server_id: str, file_path: str, exc: BaseException) -> None:
    """从 LSP 层冒泡出来的意外异常。WARNING 级别。"""
    _emit(
        server_id,
        logging.WARNING,
        f"unexpected error for {_short_path(file_path)}: {type(exc).__name__}: {exc}",
    )


def log_spawn_failed(server_id: str, workspace_root: str, exc: BaseException) -> None:
    """LSP 服务器启动或初始化失败。WARNING 级别。"""
    _emit(
        server_id,
        logging.WARNING,
        f"spawn/initialize failed for {workspace_root}: {type(exc).__name__}: {exc}",
    )


def reset_announce_caches() -> None:
    """仅供测试使用：清除去重缓存。生产代码不应调用此函数。"""
    with _announce_lock:
        _announced_active.clear()
        _announced_unavailable.clear()
        _announced_no_root.clear()
        _announced_no_server.clear()


__all__ = [
    "event_log",
    "log_clean",
    "log_disabled",
    "log_active",
    "log_diagnostics",
    "log_no_project_root",
    "log_server_unavailable",
    "log_no_server_configured",
    "log_timeout",
    "log_server_error",
    "log_spawn_failed",
    "reset_announce_caches",
]
