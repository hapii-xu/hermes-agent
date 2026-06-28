"""Hermes Agent 的 Language Server Protocol (LSP) 集成。

Hermes 将完整的 language server（pyright、gopls、rust-analyzer、
typescript-language-server 等）作为子进程运行，并将其
``textDocument/publishDiagnostics`` 输出导入到 ``write_file`` 和
``patch`` 使用的写入后 lint delta 过滤器中。

LSP **以 git 工作区检测为门控** — 如果 agent 的 cwd 位于 git
仓库内，LSP 将针对该工作区运行；否则 file_operations 层会回退
到其现有的进程内语法检查。这可以避免在用户主目录 cwd（例如
Telegram gateway 聊天）中启动不必要的守护进程。

公共 API：

    from agent.lsp import get_service

    svc = get_service()
    if svc and svc.enabled_for(path):
        await svc.touch_file(path)
        diags = svc.diagnostics_for(path)

大部分连接逻辑是内部的 — 大多数调用者只需要
:func:`tools.file_operations.FileOperations._check_lint_delta` 中的层，
该层已经连接好了（参见该模块）。

架构文档见 ``website/docs/user-guide/features/lsp.md``。
"""
from __future__ import annotations

import atexit
import logging
import threading
from typing import Optional

from agent.lsp.manager import LSPService

logger = logging.getLogger("agent.lsp")

_service: Optional[LSPService] = None
_atexit_registered = False
_service_lock = threading.Lock()


def get_service() -> Optional[LSPService]:
    """返回进程级 LSP service 单例，禁用时返回 None。

    service 在首次调用时延迟创建。当 LSP 在配置中被禁用、
    无法检测到工作区、或平台不支持基于子进程的 LSP server 时，
    返回 ``None``。

    首次创建时，注册一个 :mod:`atexit` 处理器，在 Python 退出时
    拆除已启动的 language server，避免长时间运行的 CLI 或 gateway
    会话在终止时泄漏 pyright/gopls 等进程。
    """
    global _service, _atexit_registered
    if _service is not None:
        return _service if _service.is_active() else None
    with _service_lock:
        if _service is not None:
            return _service if _service.is_active() else None
        _service = LSPService.create_from_config()
        if not _atexit_registered:
            # ``atexit`` 处理器在正常 Python 退出和 SystemExit 时
            # 按 LIFO 顺序运行，但在 os._exit() 或未捕获的信号时
            # 不会运行。Language server 是无状态子进程 — 在 SIGKILL
            # 时丢失它们没有问题；内核会连同父进程一起回收它们。
            # 我们关心的是 Python 在终止前刷新 stdio 的正常退出；
            # 没有这个钩子，每次 ``hermes chat`` 退出都会泄漏
            # pyright 进程，这些进程会在 stdout 缓冲区排空前比
            # 父进程多存活几秒钟。
            atexit.register(_atexit_shutdown)
            _atexit_registered = True
    return _service if (_service is not None and _service.is_active()) else None


def shutdown_service() -> None:
    """如果已启动 LSP service 则将其拆除。

    可安全地多次调用；可安全地在未创建 service 时调用。
    """
    global _service
    with _service_lock:
        svc = _service
        _service = None
    if svc is not None:
        try:
            svc.shutdown()
        except Exception as e:  # noqa: BLE001
            logger.debug("LSP shutdown error: %s", e)


def _atexit_shutdown() -> None:
    """atexit 注册的包装器。以 debug 级别记录，因为 atexit
    触发时用户已经看到了 agent 的最终输出 — 在上面加一行
    嘈杂的关闭信息只是多余的。"""
    try:
        shutdown_service()
    except Exception as e:  # noqa: BLE001
        logger.debug("atexit LSP shutdown failed: %s", e)


__all__ = ["get_service", "shutdown_service", "LSPService"]
