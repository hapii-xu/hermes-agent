"""PTY 侧网关的尽力而为 WebSocket 发布传输。

仪表板的 `/api/pty` 会生成 `hermes --tui` 作为子进程，该子进程
又会生成自己的 ``tui_gateway.entry``。工具/推理/状态事件在
*那个* 网关的传输层上触发 — 距离仪表板
服务器本身有三层进程之遥。为了在仪表板侧边栏（`/api/events`）中
展示这些事件，PTY 侧网关在启动时向仪表板打开一个反向 WS 连接，
并通过此传输镜像每一次发送。

线路协议：以换行符分隔的 JSON 字典（与调度器
已经传递给 ``write`` 的格式相同）。此处没有 JSON-RPC 信封 — 仪表板的
``/api/pub`` 端点只是将字节原样重播给订阅者。

失败模式：静默。代理循环绝不能阻塞等待
sidecar 排空。断开的 WS 会使所有后续写入短路。
实际的 ``send`` 调用在守护线程上运行，因此 TeeTransport 的
``write`` 在入队后即返回（尽力而为；队列满时丢弃）。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from typing import Optional

try:
    from websockets.sync.client import connect as ws_connect
except ImportError:  # pragma: no cover - websockets 是必需的安装路径
    ws_connect = None  # type: ignore[assignment]

_log = logging.getLogger(__name__)

_DRAIN_STOP = object()

_QUEUE_MAX = 256


class WsPublisherTransport:
    __slots__ = ("_url", "_lock", "_ws", "_dead", "_q", "_worker")

    def __init__(self, url: str, *, connect_timeout: float = 2.0) -> None:
        self._url = url
        self._lock = threading.Lock()
        self._ws: Optional[object] = None
        self._dead = False
        self._q: queue.Queue[object] = queue.Queue(maxsize=_QUEUE_MAX)
        self._worker: Optional[threading.Thread] = None

        if ws_connect is None:
            self._dead = True

            return

        try:
            self._ws = ws_connect(url, open_timeout=connect_timeout, max_size=None)
        except Exception as exc:
            _log.debug("event publisher connect failed: %s", exc)
            self._dead = True
            self._ws = None

            return

        self._worker = threading.Thread(
            target=self._drain,
            name="hermes-ws-pub",
            daemon=True,
        )
        self._worker.start()

    def _drain(self) -> None:
        while True:
            item = self._q.get()
            if item is _DRAIN_STOP:
                return
            if not isinstance(item, str):
                continue
            if self._ws is None:
                continue
            try:
                with self._lock:
                    if self._ws is not None:
                        self._ws.send(item)  # type: ignore[union-attr]
            except Exception as exc:
                _log.debug("event publisher write failed: %s", exc)
                self._dead = True
                self._ws = None

    def write(self, obj: dict) -> bool:
        if self._dead or self._ws is None or self._worker is None:
            return False

        line = json.dumps(obj, ensure_ascii=False)

        try:
            self._q.put_nowait(line)

            return True
        except queue.Full:
            return False

    def close(self) -> None:
        self._dead = True
        w = self._worker
        if w is not None and w.is_alive():
            try:
                self._q.put_nowait(_DRAIN_STOP)
            except queue.Full:
                # 尽力而为：如果队列卡住，守护线程
                # 会随进程一起被销毁。
                pass
            w.join(timeout=3.0)
        self._worker = None

        if self._ws is None:
            return

        try:
            with self._lock:
                if self._ws is not None:
                    self._ws.close()  # type: ignore[union-attr]
        except Exception:
            pass

        self._ws = None
