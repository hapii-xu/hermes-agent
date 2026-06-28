"""tui_gateway JSON-RPC 服务器的传输抽象。

历史上，网关将每个 JSON 帧直接写入真实的 stdout。此
模块将 I/O 接收端与处理器逻辑解耦，使同一个调度器
可以通过 stdio（``tui_gateway.entry``）或 WebSocket
（``tui_gateway.ws``）驱动，而无需重复代码。

:class:`Transport` 是任何能接收 JSON 可序列化字典并
将其转发给对端的东西。当前请求的活跃传输
在 :class:`contextvars.ContextVar` 中跟踪，因此处理器 — 包括那些
在工作池上调度的处理器 — 将写入路由到正确的对端。

向后兼容
----------------------
``tui_gateway.server.write_json`` 在没有绑定传输时仍然有效。
当上下文变量上没有绑定且未找到会话级传输时，
它回退到模块级的 :class:`StdioTransport`，后者包装了
原始的 ``_real_stdout`` + ``_stdout_lock`` 对。对
``server._real_stdout`` 进行 monkey-patch 的测试
继续有效，因为 stdio 传输通过回调延迟解析流。
"""

from __future__ import annotations

import contextvars
import errno
import json
import logging
import os
import threading
from typing import Any, Callable, Optional, Protocol, runtime_checkable

# 表示"对端已消失"而非"主机有
# 真正 I/O 问题"的 errno 值。此集合之外的值会重新抛出，
# 使其在崩溃日志中显示，而不是看起来像干净的断开连接。
_PEER_GONE_ERRNOS = frozenset({
    errno.EPIPE,        # 写入已关闭的管道 (POSIX)
    errno.ECONNRESET,   # 对端重置了连接
    errno.EBADF,        # fd 在我们不知情的情况下被关闭
    errno.ESHUTDOWN,    # 传输端点已关闭
    getattr(errno, "WSAECONNRESET", -1),  # win32 映射（POSIX 上无操作）
    getattr(errno, "WSAESHUTDOWN", -1),
} - {-1})

logger = logging.getLogger(__name__)

# 可选旋钮：为 true 时，StdioTransport 在写入后不调用
# ``stream.flush``。在半关闭管道（TUI
# Node 父进程退出而网关仍在发送事件）导致
# flush 阻塞足够长时间以致饿死工作池其余部分的环境中
# 使用此选项。
#
# 重要：Python 文本 stdout 在连接到管道时
# 是完全缓冲的（TUI 场景），所以此旋钮仅在
# 网关使用 ``-u`` 或 ``PYTHONUNBUFFERED=1`` 启动时
# 才有意义。没有其中之一，JSON-RPC 帧会在缓冲区中
# 累积，TUI 会挂起等待 ``gateway.ready``。默认保持关闭，
# 因此现有的写入后刷新行为不变。
_DISABLE_FLUSH = (os.environ.get("HERMES_TUI_GATEWAY_NO_FLUSH", "") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


@runtime_checkable
class Transport(Protocol):
    """每个传输实现的最小接口。"""

    def write(self, obj: dict) -> bool:
        """发送一个 JSON 帧。当对端已消失时返回 ``False``。"""

    def close(self) -> None:
        """释放此传输拥有的任何资源。"""


_current_transport: contextvars.ContextVar[Optional[Transport]] = (
    contextvars.ContextVar(
        "hermes_gateway_transport",
        default=None,
    )
)


def current_transport() -> Optional[Transport]:
    """返回当前请求绑定的传输（如果有）。"""
    return _current_transport.get()


def bind_transport(transport: Optional[Transport]):
    """为当前上下文绑定 *transport*。返回一个供 :func:`reset_transport` 使用的令牌。"""
    return _current_transport.set(transport)


def reset_transport(token) -> None:
    """恢复 :func:`bind_transport` 捕获的传输绑定。"""
    _current_transport.reset(token)


class StdioTransport:
    """将 JSON 帧写入流（通常是 ``sys.stdout``）。

    流通过可调用对象解析，以便运行时对底层流的 monkey-patch
    继续有效 — 这保留了现有测试套件依赖的行为
    （``monkeypatch.setattr(server, "_real_stdout", ...)``）。
    """

    __slots__ = ("_stream_getter", "_lock")

    def __init__(self, stream_getter: Callable[[], Any], lock: threading.Lock) -> None:
        self._stream_getter = stream_getter
        self._lock = lock

    def write(self, obj: dict) -> bool:
        """成功时返回 ``True``，``False`` 仅在对端已消失时返回。

        返回 ``False`` 是调度器的"stdout 管道损坏"信号 —
        ``entry.py`` 在 ``write_json`` 报告 ``False`` 时调用
        ``sys.exit(0)``。因此编程错误（非 JSON 安全的负载、
        编码配置错误、意外的 ValueError、主机 I/O 错误如
        ENOSPC）绝不能返回 ``False``，否则真正的 bug 看起来像
        干净的断开连接，更难诊断。这些情况会重新抛出，
        使现有的崩溃日志基础设施记录回溯。

        对端消失的分支：
          * ``BrokenPipeError``
          * ``ValueError("...closed file...")``
          * ``OSError``，其 errno 在 :data:`_PEER_GONE_ERRNOS` 中
            （EPIPE / ECONNRESET / EBADF / ESHUTDOWN；加上 Windows 上的
            WSA 映射）。其他 OSError errno（ENOSPC、EACCES 等）是
            真正的主机问题，会重新抛出。
        """
        # 序列化在锁外进行，这样大负载不会
        # 阻塞其他线程发送自己的帧。非 JSON 安全的
        # 负载是编程错误：重新抛出使崩溃日志
        # 捕获它，而不是通过 False 路径静默退出。
        line = json.dumps(obj, ensure_ascii=False) + "\n"

        with self._lock:
            stream = self._stream_getter()
            try:
                stream.write(line)
            except BrokenPipeError:
                return False
            except ValueError as e:
                # ValueError("I/O operation on closed file") 是
                # 唯一表示"对端已消失"的 ValueError。其他
                # 任何情况 — 包括 UnicodeEncodeError（它是
                # 配置错误的语言环境的 ValueError 子类）—
                # 都是真正的 bug；重新抛出使其在崩溃日志中显示。
                if isinstance(e, UnicodeEncodeError) or "closed file" not in str(e):
                    raise
                return False
            except OSError as e:
                if e.errno not in _PEER_GONE_ERRNOS:
                    raise
                logger.debug("StdioTransport write peer gone: %s", e)
                return False

            # 如果 flush *抛出* 了对端消失的 errno，意味着
            # 调度器应该干净退出。如果 flush 在半关闭的
            # 管道上 *挂起*，会持有锁直到返回 —
            # 参见 ``_DISABLE_FLUSH`` 了解"完全跳过 flush"的
            # 逃生舱。
            if not _DISABLE_FLUSH:
                try:
                    stream.flush()
                except BrokenPipeError:
                    return False
                except ValueError as e:
                    if isinstance(e, UnicodeEncodeError) or "closed file" not in str(e):
                        raise
                    return False
                except OSError as e:
                    if e.errno not in _PEER_GONE_ERRNOS:
                        raise
                    logger.debug("StdioTransport flush peer gone: %s", e)
                    return False

        return True

    def close(self) -> None:
        return None


class TeeTransport:
    """将写入镜像到一个主传输和 N 个尽力而为的副传输。

    主传输的返回值（和异常）决定结果 —
    副传输吞没失败，使卡住的 sidecar 永远不会阻塞
    主 IO 路径。由 PTY 子进程使用，使每次调度器发送
    同时到达 stdio（Ink）和反向 WS（馈送到仪表板侧边栏）。
    """

    __slots__ = ("_primary", "_secondaries")

    def __init__(self, primary: "Transport", *secondaries: "Transport") -> None:
        self._primary = primary
        self._secondaries = secondaries

    def write(self, obj: dict) -> bool:
        # 先写主传输，使慢的 sidecar（WS 发布器）永远不会延迟 Ink/stdio。
        ok = self._primary.write(obj)
        for sec in self._secondaries:
            try:
                sec.write(obj)
            except Exception:
                pass
        return ok

    def close(self) -> None:
        try:
            self._primary.close()
        finally:
            for sec in self._secondaries:
                try:
                    sec.close()
                except Exception:
                    pass
