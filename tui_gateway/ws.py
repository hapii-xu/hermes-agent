"""tui_gateway JSON-RPC 服务器的 WebSocket 传输。

重用 :func:`tui_gateway.server.dispatch`，使每个 RPC 方法、每个
斜杠命令、每个审批/澄清/sudo 流程，以及每个代理事件都通过
相同的处理器，无论客户端是 stdio 上的 Ink 还是
iOS / Web 客户端通过 WebSocket。

线路协议
-------------
与 stdio 相同：双向都是换行分隔的 JSON-RPC。服务器
在连接接受后立即发送 ``gateway.ready`` 事件，然后
回应入站请求的响应/事件。没有帧格式差异。

挂载方式
--------
    from fastapi import WebSocket
    from tui_gateway.ws import handle_ws

    @app.websocket("/api/ws")
    async def ws(ws: WebSocket):
        await handle_ws(ws)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import socket
from typing import Any

from tui_gateway import server

_log = logging.getLogger(__name__)

# 池调度的处理器在等待事件循环
# 刷新 WS 帧时最多阻塞的秒数，超过后标记传输为死亡。
# 保护处理器线程免受卡住的套接字影响。
_WS_WRITE_TIMEOUT_S = 10.0
_WS_LOG_PAYLOAD_PREVIEW = 240

# 在导入时保持 starlette 为可选；handle_ws 在可用时使用真实类，
# 否则回退到通用的 Exception 哨兵。
try:
    from starlette.websockets import WebSocketDisconnect as _WebSocketDisconnect
except ImportError:  # pragma: no cover - starlette 是必需的安装路径
    _WebSocketDisconnect = Exception  # type: ignore[assignment]


class WSTransport:
    """每个连接的 WS 传输。

    ``write`` 可以安全地从 *除了* 拥有套接字的事件循环
    线程之外的任何线程调用。池工作器（唯一的实际调用者）
    在各自的线程中运行，因此通过
    :func:`asyncio.run_coroutine_threadsafe` + ``future.result()``
    编组到循环上是正确且无死锁的。

    当从循环线程本身调用时（例如 ``handle_ws`` 用于
    内联响应），相同的调用会死锁：我们会将工作调度到
    我们正在阻塞的循环上。我们检测这种情况并改为
    即发即忘。需要知道字节何时到达线上的调用者
    应从循环线程使用 :meth:`write_async`。
    """

    def __init__(
        self,
        ws: Any,
        loop: asyncio.AbstractEventLoop,
        *,
        peer: str = "unknown",
    ) -> None:
        self._ws = ws
        self._loop = loop
        self._peer = peer
        self._closed = False

    def write(self, obj: dict) -> bool:
        if self._closed:
            return False

        line = json.dumps(obj, ensure_ascii=False)

        try:
            on_loop = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            on_loop = False

        if on_loop:
            # 即发即忘 — 不要阻塞循环等待自身。
            self._loop.create_task(self._safe_send(line))
            return True

        try:
            from agent.async_utils import safe_schedule_threadsafe
            fut = safe_schedule_threadsafe(self._safe_send(line), self._loop)
            if fut is None:
                self._closed = True
                return False
            fut.result(timeout=_WS_WRITE_TIMEOUT_S)
            return not self._closed
        except concurrent.futures.TimeoutError:  # builtin TimeoutError on 3.11+
            # 事件循环停滞了（GIL 密集的代理轮次、
            # 委托运行 N 个子进程），而不是套接字死了。
            # 发送协程已经调度，一旦循环恢复就会刷新 —
            # 在此处锁定 _closed 会在一次慢写入后永久静默
            # 活跃的窗口（"子代理窗口显示零流式传输" bug）。
            # 解除工作线程的阻塞并保持传输活跃；
            # _safe_send 在帧实际失败时会对真实套接字错误进行锁定。
            _log.warning(
                "ws write slow (loop stalled >%ss) peer=%s — frame left in flight",
                _WS_WRITE_TIMEOUT_S, self._peer,
            )
            return not self._closed
        except Exception as exc:
            self._closed = True
            _log.warning(
                "ws write failed peer=%s error_type=%s error=%s",
                self._peer, type(exc).__name__, exc,
            )
            return False

    async def write_async(self, obj: dict) -> bool:
        """从拥有的事件循环发送。等待直到帧到达线上。"""
        if self._closed:
            return False
        await self._safe_send(json.dumps(obj, ensure_ascii=False))
        return not self._closed

    async def _safe_send(self, line: str) -> None:
        try:
            await self._ws.send_text(line)
        except Exception as exc:
            self._closed = True
            _log.warning(
                "ws send failed peer=%s error_type=%s error=%s",
                self._peer, type(exc).__name__, exc,
            )

    def close(self) -> None:
        self._closed = True


def _ws_peer_label(ws: Any) -> str:
    """可用时返回 ``host:port``，否则返回稳定的占位符。"""
    client = getattr(ws, "client", None)
    if client is None:
        return "unknown"
    host = getattr(client, "host", None) or "unknown"
    port = getattr(client, "port", None)
    return f"{host}:{port}" if port is not None else host


def _disable_nagle(ws: Any) -> None:
    """禁用 Nagle 算法，使流式 JSON-RPC 帧单独发送。

    否则内核会将每个小 token 帧合并，导致模型思考停顿后
    的突发在一个 tick 内到达客户端，客户端侧的任何平滑
    都无法恢复节奏。仅限 GUI/WS；聊天平台不会走此路径。
    尽力而为 — 如果套接字不可达则静默跳过。
    """
    try:
        scope = getattr(ws, "scope", None) or {}
        transport = (scope.get("extensions") or {}).get("transport") or getattr(ws, "transport", None)
        sock = transport.get_extra_info("socket") if transport is not None else None
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception as exc:  # pragma: no cover - best-effort tuning
        _log.debug("ws TCP_NODELAY skip: %s", exc)


async def handle_ws(ws: Any) -> None:
    """运行一个 WebSocket 会话。与 ``tui_gateway.entry`` 线路兼容。"""
    peer = _ws_peer_label(ws)
    transport: WSTransport | None = None
    messages = 0
    parse_errors = 0
    dispatch_crashes = 0
    send_failures = 0
    disconnect_reason = "not_connected"

    try:
        await ws.accept()
        disconnect_reason = "connected"
        # 立即推送小的流式帧，而不是让 Nagle
        # 批量发送 — 保持 GUI 客户端的实时 token 节奏。
        _disable_nagle(ws)
        _log.info("ws accepted peer=%s", peer)

        transport = WSTransport(ws, asyncio.get_running_loop(), peer=peer)

        ready_ok = await transport.write_async(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "gateway.ready",
                    "payload": {"skin": server.resolve_skin()},
                },
            }
        )
        if not ready_ok:
            disconnect_reason = "ready_send_failed"
            send_failures += 1
            _log.error("ws ready frame send failed peer=%s", peer)
            return

        while True:
            try:
                raw = await ws.receive_text()
            except _WebSocketDisconnect as exc:
                disconnect_reason = (
                    "client_disconnect("
                    f"code={getattr(exc, 'code', None)},"
                    f"reason={getattr(exc, 'reason', None)})"
                )
                break
            except Exception:
                disconnect_reason = "receive_failed"
                _log.exception("ws receive failed peer=%s", peer)
                break

            line = raw.strip()
            if not line:
                continue
            messages += 1

            try:
                req = json.loads(line)
            except json.JSONDecodeError as exc:
                parse_errors += 1
                _log.warning(
                    "ws parse error peer=%s index=%d error=%s payload=%r",
                    peer,
                    messages,
                    exc,
                    line[:_WS_LOG_PAYLOAD_PREVIEW],
                )
                ok = await transport.write_async(
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -32700, "message": "parse error"},
                        "id": None,
                    }
                )
                if not ok:
                    disconnect_reason = "send_failed_after_parse_error"
                    send_failures += 1
                    _log.warning("ws parse-error reply send failed peer=%s", peer)
                    break
                continue

            # dispatch() 可能在线程池上调度长时间运行的处理器；
            # 在这种情况下返回 None，worker 通过我们传入的传输
            # 自行写入响应（单独的线程，所以 transport.write
            # 在那里走安全路径）。对于内联处理器，它返回
            # 响应字典，我们从循环中在此处写入。
            req_id = req.get("id") if isinstance(req, dict) else None
            req_method = req.get("method") if isinstance(req, dict) else None
            try:
                resp = await asyncio.to_thread(server.dispatch, req, transport)
            except Exception:
                dispatch_crashes += 1
                _log.exception(
                    "ws dispatch crash peer=%s id=%s method=%s",
                    peer,
                    req_id,
                    req_method,
                )
                ok = await transport.write_async(
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -32603, "message": "internal error"},
                        "id": req_id if req_id is not None else None,
                    }
                )
                if not ok:
                    disconnect_reason = "send_failed_after_dispatch_crash"
                    send_failures += 1
                    _log.warning(
                        "ws dispatch-crash reply send failed peer=%s id=%s method=%s",
                        peer,
                        req_id,
                        req_method,
                    )
                    break
                continue
            if resp is not None and not await transport.write_async(resp):
                disconnect_reason = "send_failed_after_response"
                send_failures += 1
                _log.warning(
                    "ws response send failed peer=%s id=%s method=%s",
                    peer,
                    req_id,
                    req_method,
                )
                break
    finally:
        reaped_sessions = 0
        detached_sessions = 0
        if transport is not None:
            transport.close()

            # 清理由此传输拥有的会话（close_on_disconnect 的 sidecar
            # 会话）或将其与会话分离到 drop 哨兵，使后续的发送
            # 不会崩溃到已关闭的套接字上或落入桌面 stdout
            # 日志。分离的会话交给有限宽限窗口的 WS 孤儿
            # 清道夫处理（在 _close_sessions_for_transport 中；
            # 快速重连 / session.resume 可以取消它）。
            # 这是唯一的 WS 断开拆除路径。
            #
            # 已卸载：_close_session_by_id 执行阻塞的 worker.close()
            # （终止 + 等待）加上同步 DB 写入 — 内联执行
            # 会冻结 uvicorn 事件循环中所有其他活跃的
            # 连接。
            try:
                reaped_sessions, detached_sessions = await asyncio.to_thread(
                    server._close_sessions_for_transport,
                    transport,
                    end_reason="ws_disconnect",
                )
            except Exception:
                _log.exception("ws transport teardown failed peer=%s", peer)
        try:
            await ws.close()
        except Exception as exc:
            _log.debug("ws close failed peer=%s error=%s", peer, exc)
        _log.info(
            "ws closed peer=%s reason=%s messages=%d parse_errors=%d "
            "dispatch_crashes=%d send_failures=%d reaped_sessions=%d detached_sessions=%d",
            peer,
            disconnect_reason,
            messages,
            parse_errors,
            dispatch_crashes,
            send_failures,
            reaped_sessions,
            detached_sessions,
        )
