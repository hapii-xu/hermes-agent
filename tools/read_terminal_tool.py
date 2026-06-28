#!/usr/bin/env python3
"""读取 Hermes 桌面 GUI 中的应用内终端面板。

内嵌终端的缓冲区位于桌面渲染器（xterm.js）中，因此本工具会经由 gateway
的阻塞式 prompt 桥接往返一次——也就是 `clarify` 使用的同一个桥接：
tui_gateway 发出 ``terminal.read.request``，渲染器以
``terminal.read.respond`` 作答。本模块仅是 schema 加一层薄薄的分发器，
构建在平台注入的回调之上。
"""

import json
import os
from typing import Callable, Optional

from tools.registry import registry, tool_error


def read_terminal_tool(
    start_line: Optional[int] = None,
    count: Optional[int] = None,
    callback: Optional[Callable] = None,
) -> str:
    """以 JSON 字符串形式返回应用内终端的内容（含行元数据）。"""
    if callback is None:
        return tool_error("read_terminal is only available in the Hermes desktop app.")

    try:
        window = {
            key: max(floor, int(val))
            for key, val, floor in (("start", start_line, 0), ("count", count, 1))
            if val is not None
        }
    except (TypeError, ValueError):
        return tool_error("start_line and count must be integers.")

    try:
        raw = callback(**window)
    except Exception as exc:
        return tool_error(f"Failed to read terminal: {exc}")

    if not raw:
        return tool_error("No in-app terminal is open, or the read timed out.")

    # 桌面端返回的是一个 JSON 对象；直接透传，否则把原始文本包裹一层。
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps({"text": str(raw)}, ensure_ascii=False)


def check_read_terminal_requirements() -> bool:
    """仅限桌面 GUI——应用启动的 gateway 上会设置 HERMES_DESKTOP。"""
    return (os.getenv("HERMES_DESKTOP") or "").strip().lower() in ("1", "true", "yes")


READ_TERMINAL_SCHEMA = {
    "name": "read_terminal",
    "description": (
        "Read what's currently shown in the in-app terminal pane of the Hermes "
        "desktop GUI (the embedded shell beside this chat). Call with no arguments "
        "to get the visible screen plus the total line count (`total_lines`). To "
        "page through scrollback, pass `start_line` (0 = oldest line) and `count`; "
        "valid lines are [0, total_lines). Returns JSON: "
        "{total_lines, start, end, viewport_rows, cursor_row, text}."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "start_line": {
                "type": "integer",
                "description": "0-indexed first line (0 = oldest). Omit for the visible screen.",
            },
            "count": {
                "type": "integer",
                "description": "Lines to read from start_line. Defaults to the visible row count.",
            },
        },
    },
}


registry.register(
    name="read_terminal",
    toolset="terminal",
    schema=READ_TERMINAL_SCHEMA,
    handler=lambda args, **kw: read_terminal_tool(
        start_line=args.get("start_line"),
        count=args.get("count"),
        callback=kw.get("callback"),
    ),
    check_fn=check_read_terminal_requirements,
    emoji="🖥️",
)
