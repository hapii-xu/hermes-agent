"""用于只渲染一次 gateway 消息时间戳的辅助函数。

gateway 消息在 LLM 上下文中需要时间戳以具备时间感知，但持久化的消息内容
应当保持干净，这样重放时就不会在多个 turn 之间累积
``[timestamp] [timestamp] ...`` 前缀。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional, Tuple


# 当前 gateway 格式：[Tue 2026-04-28 13:40:53 CEST]
_HUMAN_TIMESTAMP_RE = re.compile(
    r"^\[(?P<dow>[A-Z][a-z]{2}) "
    r"(?P<date>\d{4}-\d{2}-\d{2}) "
    r"(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?: (?P<tz>[A-Za-z0-9_+\-/:]+))?\]\s*"
)

# 较早的 gateway 格式：[2026-04-13T17:02:06+0200] 或 [+02:00]
_ISO_TIMESTAMP_RE = re.compile(
    r"^\[(?P<iso>\d{4}-\d{2}-\d{2}T[^\]]+)\]\s*"
)


def coerce_message_timestamp(ts_value: Any, tz=None) -> Optional[float]:
    """把类时间戳的值强制转换为 Unix epoch 秒。

    接受 Unix epoch 数字、datetime 对象、ISO 字符串，以及 gateway 的方括号
    包裹的人类可读时间戳格式。当值无法被解析时返回 ``None``。
    """
    if ts_value is None:
        return None

    if isinstance(ts_value, (int, float)):
        return float(ts_value)

    if hasattr(ts_value, "timestamp"):
        try:
            return float(ts_value.timestamp())
        except Exception:
            return None

    if isinstance(ts_value, str):
        text = ts_value.strip()
        if not text:
            return None
        parsed = _parse_timestamp_prefix(text, tz=tz)
        if parsed is not None:
            return parsed
        try:
            return float(text)
        except (TypeError, ValueError):
            pass
        try:
            dt = datetime.fromisoformat(text)
        except (TypeError, ValueError):
            try:
                dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")
            except (TypeError, ValueError):
                return None
        if dt.tzinfo is None:
            if tz is not None:
                dt = dt.replace(tzinfo=tz)
            else:
                dt = dt.astimezone()
        return float(dt.timestamp())

    return None


def format_message_timestamp(ts_value: Any, tz=None) -> str:
    """把时间戳值格式化为 ``[Tue 2026-04-28 13:40:53 CEST]``。"""
    epoch = coerce_message_timestamp(ts_value, tz=tz)
    if epoch is None:
        return ""
    if tz is not None:
        dt = datetime.fromtimestamp(epoch, tz=tz)
    else:
        dt = datetime.fromtimestamp(epoch).astimezone()
    return "[" + dt.strftime("%a %Y-%m-%d %H:%M:%S %Z") + "]"


def strip_leading_message_timestamps(content: str, tz=None) -> Tuple[str, Optional[float]]:
    """从 ``content`` 中剥离一个或多个前导的 gateway 时间戳前缀。

    返回 ``(clean_content, embedded_epoch)``。如果存在多个时间戳前缀，
    最接近实际消息文本的那个时间戳胜出。这样可以为历史遗留的受污染行
    （如 ``[processing time] [platform time] [sender] message``）
    保留原始的平台发送时间。
    """
    if not isinstance(content, str) or not content:
        return content, None

    text = content
    embedded_epoch: Optional[float] = None

    while True:
        match = _HUMAN_TIMESTAMP_RE.match(text) or _ISO_TIMESTAMP_RE.match(text)
        if not match:
            break
        parsed = _parse_timestamp_match(match, tz=tz)
        if parsed is not None:
            embedded_epoch = parsed
        text = text[match.end():]

    return text, embedded_epoch


def render_user_content_with_timestamp(content: str, ts_value: Any = None, tz=None) -> str:
    """为 LLM 上下文渲染用户消息，并附带恰好一个时间戳前缀。

    首先移除已有的前导时间戳前缀。如果存在这样的前缀，其解析出的时间
    优先于 ``ts_value``；否则格式化 ``ts_value`` 并前置。如果没有可用
    的时间戳，则原样返回清理后的内容。
    """
    clean_content, embedded_epoch = strip_leading_message_timestamps(content, tz=tz)
    effective_ts = embedded_epoch if embedded_epoch is not None else ts_value
    prefix = format_message_timestamp(effective_ts, tz=tz)
    if not prefix:
        return clean_content
    if clean_content:
        return f"{prefix} {clean_content}"
    return prefix


def _parse_timestamp_prefix(text: str, tz=None) -> Optional[float]:
    match = _HUMAN_TIMESTAMP_RE.match(text) or _ISO_TIMESTAMP_RE.match(text)
    if not match:
        return None
    return _parse_timestamp_match(match, tz=tz)


def _parse_timestamp_match(match: re.Match, tz=None) -> Optional[float]:
    if "iso" in match.groupdict() and match.group("iso"):
        iso_text = match.group("iso")
        try:
            dt = datetime.fromisoformat(iso_text)
        except ValueError:
            try:
                dt = datetime.strptime(iso_text, "%Y-%m-%dT%H:%M:%S%z")
            except ValueError:
                return None
        if dt.tzinfo is None:
            if tz is not None:
                dt = dt.replace(tzinfo=tz)
            else:
                dt = dt.astimezone()
        return float(dt.timestamp())

    date_part = match.group("date")
    time_part = match.group("time")
    try:
        dt = datetime.strptime(f"{date_part} {time_part}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if tz is not None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone()
    return float(dt.timestamp())
