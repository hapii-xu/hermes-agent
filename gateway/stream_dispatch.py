"""由 adapter 驱动的结构化流式事件分发到投递 sink。

``GatewayEventDispatcher`` 正是 Tobi 要求的那个接缝：agent 发出类型化事件
（gateway/stream_events.py），由 *adapter* 决定每个事件如何投递。dispatcher 持有一个
adapter + stream consumer（sink）+ 已解析的 per-channel 呈现设置（tool-progress 模式、
preview 长度），并通过 adapter 的渲染钩子路由每个事件。

Message/commentary/segment 事件流入 consumer（Telegram DM 用原生 draft，其他平台
就地编辑）。工具事件由 adapter 格式化 —— 在无法渲染 tool chrome 的平台上，adapter
可以返回 None 来 *吃掉* 该事件 —— 渲染出的行被入队到 gateway 已经在排空的同一个
tool progress 队列上，这样二者就不会再通过相互独立的代码路径竞争。

本模块刻意不包含平台知识，也不使用 asyncio：它是一个轻量的同步路由器，可从 agent
的工作线程调用，与它所替换的回调完全一致。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from gateway.stream_events import (
    Commentary,
    GatewayNotice,
    LongToolHint,
    MessageChunk,
    MessageStop,
    StreamEvent,
    ToolCallChunk,
    ToolCallFinished,
)

logger = logging.getLogger("gateway.stream_events")


class GatewayEventDispatcher:
    """把类型化的流式事件通过 adapter 路由到一个投递 sink。

    Parameters
    ----------
    adapter:
        平台 adapter。提供 ``render_message_event`` 和 ``format_tool_event``
        （BasePlatformAdapter 的默认实现复现当前行为；adapter 可重写以实现原生渲染）。
    sink:
        用于投递 assistant 文本的 GatewayStreamConsumer。当流式功能被禁用时可为
        None，此时消息事件会被丢弃（最终响应仍通过常规 send 路径发出）。
    enqueue_tool_line:
        把渲染后的 tool-progress 行放入 gateway 的 progress 队列（即
        ``send_progress_messages`` 所排空的同一个队列）的回调。当此通道的 tool
        progress 被禁用时可传 None。
    tool_mode:
        此通道已解析的 tool-progress 模式（"all" / "new" / "verbose" / "off"）。
    preview_max_len:
        已解析的 ``tool_preview_length``（0 = 在 verbose 模式下不设上限）。
    on_long_tool / on_notice:
        LongToolHint / GatewayNotice 事件的可选钩子，让 gateway 拥有"我是否应在此处
        展示？"的决定权。
    """

    def __init__(
        self,
        adapter: Any,
        sink: Any = None,
        *,
        enqueue_tool_line: Optional[Callable[[Any], None]] = None,
        tool_mode: str = "all",
        preview_max_len: int = 40,
        on_long_tool: Optional[Callable[[LongToolHint], None]] = None,
        on_notice: Optional[Callable[[GatewayNotice], None]] = None,
    ) -> None:
        self.adapter = adapter
        self.sink = sink
        self._enqueue_tool_line = enqueue_tool_line
        self.tool_mode = tool_mode or "all"
        self.preview_max_len = preview_max_len
        self._on_long_tool = on_long_tool
        self._on_notice = on_notice
        # "new" 模式去重 —— 仅在工具变化时上报。
        self._last_tool: Optional[str] = None

    def dispatch(self, event: StreamEvent) -> None:
        """路由单个事件。绝不向 agent 的工作线程抛出异常。"""
        try:
            self._dispatch(event)
        except Exception:  # 呈现层绝不能打断 agent 循环
            logger.debug("stream-event dispatch error", exc_info=True)

    def _dispatch(self, event: StreamEvent) -> None:
        if isinstance(event, (MessageChunk, MessageStop, Commentary)):
            if self.sink is not None:
                self.adapter.render_message_event(event, self.sink)
            return

        if isinstance(event, ToolCallChunk):
            if self.tool_mode == "off" or self._enqueue_tool_line is None:
                return
            # "new" 模式：仅在工具变化时发出。
            if self.tool_mode == "new" and event.tool_name == self._last_tool:
                return
            self._last_tool = event.tool_name
            line = self.adapter.format_tool_event(
                event, mode=self.tool_mode, preview_max_len=self.preview_max_len,
            )
            # None == adapter 选择吃掉此事件（无法渲染 tool chrome）。
            if line:
                self._enqueue_tool_line(line)
            return

        if isinstance(event, ToolCallFinished):
            # 默认：完成时不显示 chrome（与当前一致 —— gateway 只渲染 "started"
            # 事件）。完成事件用于驱动上手提示。
            return

        if isinstance(event, LongToolHint):
            if self._on_long_tool is not None:
                self._on_long_tool(event)
            return

        if isinstance(event, GatewayNotice):
            if self._on_notice is not None:
                self._on_notice(event)
            return


__all__ = ["GatewayEventDispatcher"]
