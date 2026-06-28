"""
元宝平台适配器。

连接元宝 WebSocket 网关，处理鉴权（AUTH_BIND）、
心跳、重连、消息接收（T05）与发送（T06）。

在 config.yaml 中配置（或通过环境变量）：
    platforms:
      yuanbao:
        extra:
          app_id: "..."              # 或 YUANBAO_APP_ID
          app_secret: "..."          # 或 YUANBAO_APP_SECRET
          bot_id: "..."              # 或 YUANBAO_BOT_ID（可选，由 sign-token 返回）
          ws_url: "wss://..."        # 或 YUANBAO_WS_URL
          api_domain: "https://..."  # 或 YUANBAO_API_DOMAIN
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import collections
import dataclasses
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
import uuid
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from abc import ABC, abstractmethod
from typing import Any, Callable, ClassVar, Dict, Iterator, List, Optional, Tuple

import sys

import httpx

try:
    import websockets
    import websockets.exceptions
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    websockets = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_document_from_bytes,
    cache_image_from_bytes,
    cache_video_from_bytes,
)
from gateway.platforms.helpers import MessageDeduplicator
from gateway.platforms.yuanbao_media import (
    download_url as media_download_url,
    get_cos_credentials,
    upload_to_cos,
    build_image_msg_body,
    build_file_msg_body,
    guess_mime_type,
    md5_hex,
)
from gateway.platforms.yuanbao_proto import (
    CMD_TYPE,
    _fields_to_dict,
    _get_string,
    _get_varint,
    _parse_fields,
    WS_HEARTBEAT_RUNNING,
    WS_HEARTBEAT_FINISH,
    HERMES_INSTANCE_ID,
    decode_conn_msg,
    decode_inbound_push,
    decode_forward_msg_data,
    decode_query_group_info_rsp,
    decode_get_group_member_list_rsp,
    encode_auth_bind,
    encode_ping,
    encode_push_ack,
    encode_send_c2c_message,
    encode_send_group_message,
    encode_send_private_heartbeat,
    encode_send_group_heartbeat,
    encode_query_group_info,
    encode_get_group_member_list,
    next_seq_no,
)
from gateway.session import build_session_key

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 版本 / 平台常量（用于 AUTH_BIND 和 sign-token 请求头）
# ---------------------------------------------------------------------------
try:
    from hermes_cli import __version__ as _HERMES_VERSION
except ImportError:
    _HERMES_VERSION = "0.0.0"

_APP_VERSION = _HERMES_VERSION
_BOT_VERSION = _HERMES_VERSION
_YUANBAO_INSTANCE_ID = str(HERMES_INSTANCE_ID)  # 唯一来源：yuanbao_proto.HERMES_INSTANCE_ID
_OPERATION_SYSTEM = sys.platform

# ---------------------------------------------------------------------------
# 模块级常量
# ---------------------------------------------------------------------------

DEFAULT_WS_GATEWAY_URL = "wss://bot-wss.yuanbao.tencent.com/wss/connection"
DEFAULT_API_DOMAIN = "https://bot.yuanbao.tencent.com"

HEARTBEAT_INTERVAL_SECONDS = 30.0
CONNECT_TIMEOUT_SECONDS = 15.0
AUTH_TIMEOUT_SECONDS = 10.0
MAX_RECONNECT_ATTEMPTS = 100
DEFAULT_SEND_TIMEOUT = 30.0  # WS 业务请求超时时间

# 拆卸阶段 WS 关闭握手的超时上限（#40383）。
# websockets 连接自身的 close_timeout（5s）会阻塞直到服务器回传 close 帧；
# 闲置/无响应的服务器永远不回复，会让网关关闭卡住整个超时时长。
# 在此对 close 的 await 加上限可以让拆卸保持快速 —— 有响应的服务器在远不到一秒内
# 即可完成握手，所以这里只是限制住病态的卡死。同时也约束了复用 _cleanup_ws() 的
# 重连 / 连接失败清理路径，这些路径本就不需要优雅关闭（socket 即将丢弃并重拨）。
WS_CLOSE_TIMEOUT_S = 1.0

# 表示永久性错误的关闭码 —— 不要重连。
NO_RECONNECT_CLOSE_CODES = {4012, 4013, 4014, 4018, 4019, 4021}

# 心跳超时阈值 —— 连续 N 次未收到 pong 触发重连。
HEARTBEAT_TIMEOUT_THRESHOLD = 2

# 鉴权错误码分类
AUTH_FAILED_CODES = {4001, 4002, 4003}      # 永久性鉴权失败，需重新签发 token
AUTH_RETRYABLE_CODES = {4010, 4011, 4099}   # 暂时性错误，可用同一 token 重试

# 回复心跳配置
REPLY_HEARTBEAT_INTERVAL_S = 2.0   # 每 2 秒发送一次 RUNNING
REPLY_HEARTBEAT_TIMEOUT_S = 30.0   # 闲置 30 秒后自动停止

# 回复引用配置
REPLY_REF_TTL_S = 300.0            # 引用去重 TTL（5 分钟）

# 慢响应提示：当 agent 在此时长（秒）内未产出数据时，推送一条等待中消息
SLOW_RESPONSE_TIMEOUT_S = 120.0
SLOW_RESPONSE_MESSAGE = "任务有点复杂，正在努力处理中，请耐心等待..."

# 匹配会话记录文本中元宝资源引用锚点的正则：
#   [image|ybres:abc123]  [file:report.pdf|ybres:xyz789]  [voice|ybres:...]
_YB_RES_REF_RE = re.compile(
    r"\[(image|voice|video|file(?::[^|\]]*)?)\|ybres:([A-Za-z0-9_\-]+)\]"
)

# 当入站资源已被下载到本地缓存后，改写出的本地媒体锚点。
#   [image: /opt/data/image_cache/img_xxx.bmp]
#   [file: report.pdf → /opt/data/.../report.pdf]
#   （以及未来可能的任何类型，例如 [video: /opt/.../clip.mp4]）
_YB_LOCAL_MEDIA_RE = re.compile(r"\[(\w+):[^\]]*?(/[^\]]+?)\s*\]")

# 可被解析并注入到模型上下文中的媒体类型
_RESOLVABLE_MEDIA_KINDS = frozenset({"image", "file", "video"})

# 去掉 BasePlatformAdapter 追加的页码指示符，例如 (1/3)
_INDICATOR_RE = re.compile(r'\s*\(\d+/\d+\)$')

# 观察到的媒体回填：扫描最近多少条会话记录消息
OBSERVED_MEDIA_BACKFILL_LOOKBACK = 50
# 每个入站轮次最多解析的资源引用数量
OBSERVED_MEDIA_BACKFILL_MAX_RESOLVE_PER_TURN = 12

class MarkdownProcessor:
    """封装元宝平台所有 Markdown 相关的工具方法。

    提供以下静态方法：
    - 代码围栏检测与流式合并
    - 表格行检测与清理
    - 按段落边界切分
    - 原子块抽取与分块
    - 剥离外层 markdown 围栏
    - Markdown 提示词生成
    """

    # -- 代码围栏检测 ---------------------------------------------------

    @staticmethod
    def has_unclosed_fence(text: str) -> bool:
        """
        检测文本中是否存在未闭合的代码块围栏。

        逐行扫描，遇到以 ``` 开头的行时切换进出状态。
        切换次数为奇数则表示存在未闭合的围栏。

        参数：
            text: 待检查的 Markdown 文本

        返回：
            若文本以未闭合的围栏结尾则返回 True，否则返回 False
        """
        in_fence = False
        for line in text.split('\n'):
            if line.startswith('```'):
                in_fence = not in_fence
        return in_fence

    # -- 表格检测 ---------------------------------------------------

    @staticmethod
    def ends_with_table_row(text: str) -> bool:
        """
        检测文本是否以表格行结尾（最后一个非空行以 | 开头并以 | 结尾）。

        参数：
            text: 待检查的文本

        返回：
            若最后一个非空行是表格行则返回 True
        """
        trimmed = text.rstrip()
        if not trimmed:
            return False
        last_line = trimmed.split('\n')[-1].strip()
        return last_line.startswith('|') and last_line.endswith('|')

    # -- 段落边界切分 --------------------------------------

    @staticmethod
    def split_at_paragraph_boundary(
        text: str,
        max_chars: int,
        len_fn: Optional[Callable[[str], int]] = None,
    ) -> tuple[str, str]:
        """
        在 max_chars 范围内查找最近的段落边界切分点，返回 (head, tail)。

        切分优先级：
        1. 空行（段落边界）
        2. 句号/问号/感叹号（中英文）后的换行
        3. 最后一个换行
        4. 在 max_chars 处强制切分

        参数：
            text: 待切分的文本
            max_chars: 最大字符数限制
            len_fn: 可选的自定义长度函数（例如 UTF-16 长度）；默认使用内置 len

        返回：
            (head, tail) 元组，head 为前半部分，tail 为后半部分，满足 head + tail == text
        """
        _len = len_fn or len
        if _len(text) <= max_chars:
            return text, ''

        # 构建一个字符索引窗口，使其长度不超过 max_chars。
        # 当 len_fn != len 时，不能简单地用 [:max_chars] 切片，
        # 因此用二分查找找出最大的、符合长度限制的前缀。
        if _len is len:
            window = text[:max_chars]
        else:
            lo, hi = 0, len(text)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if _len(text[:mid]) <= max_chars:
                    lo = mid
                else:
                    hi = mid - 1
            window = text[:lo]

        # 1. 优先以最后一个空行（\n\n）作为段落边界
        pos = window.rfind('\n\n')
        if pos > 0:
            return text[:pos + 2], text[pos + 2:]

        # 2. 然后查找句末标点之后的最后一个换行
        sentence_end_re = re.compile(r'[。！？.!?]\n')
        best_pos = -1
        for m in sentence_end_re.finditer(window):
            best_pos = m.end()
        if best_pos > 0:
            return text[:best_pos], text[best_pos:]

        # 3. 兜底：查找最后一个换行
        pos = window.rfind('\n')
        if pos > 0:
            return text[:pos + 1], text[pos + 1:]

        # 4. 找不到合适的切分点，在窗口边界强制切分
        cut = len(window)
        return text[:cut], text[cut:]

    # -- 原子块辅助方法（私有） ------------------------------------

    @staticmethod
    def is_fence_atom(text: str) -> bool:
        """判断原子块是否为代码块（以 ``` 开头）。"""
        return text.lstrip().startswith('```')

    @staticmethod
    def is_table_atom(text: str) -> bool:
        """判断原子块是否为表格（第一行以 | 开头）。"""
        first_line = text.split('\n')[0].strip()
        return first_line.startswith('|') and first_line.endswith('|')

    @staticmethod
    def split_into_atoms(text: str) -> list[str]:
        """
        将文本切分为一组「原子块」，每个原子块是一个不可分割的逻辑单元：

        - 代码块（围栏）：从开头的 ``` 到结尾的 ```（包含围栏行本身）
        - 表格：连续的 |...| 行构成的一个整体片段
        - 普通段落：由空行分隔的纯文本片段

        空行作为分隔符，不会出现在任何原子块中。

        参数：
            text: 待切分的 Markdown 文本

        返回：
            原子块字符串列表（均非空）
        """
        lines = text.split('\n')
        atoms: list[str] = []

        current_lines: list[str] = []
        in_fence = False

        def _is_table_line(line: str) -> bool:
            stripped = line.strip()
            return stripped.startswith('|') and stripped.endswith('|')

        def _flush_current() -> None:
            if current_lines:
                atom = '\n'.join(current_lines)
                if atom.strip():
                    atoms.append(atom)
                current_lines.clear()

        for line in lines:
            if in_fence:
                current_lines.append(line)
                if line.startswith('```') and len(current_lines) > 1:
                    in_fence = False
                    _flush_current()
            elif line.startswith('```'):
                _flush_current()
                in_fence = True
                current_lines.append(line)
            elif _is_table_line(line):
                if current_lines and not _is_table_line(current_lines[-1]):
                    _flush_current()
                current_lines.append(line)
            elif line.strip() == '':
                _flush_current()
            else:
                if current_lines and _is_table_line(current_lines[-1]):
                    _flush_current()
                current_lines.append(line)

        _flush_current()

        return atoms

    # -- 核心：分块切分 ---------------------------------------------

    @classmethod
    def chunk_markdown_text(
        cls,
        text: str,
        max_chars: int = 4000,
        len_fn: Optional[Callable[[str], int]] = None,
    ) -> list[str]:
        """
        按 max_chars 将 Markdown 文本切分成多个分块。

        保证：
        - 每个分块 <= max_chars 字符（除非单个代码块/表格本身就超出限制）
        - 代码块（```...```）不会从中间被切开
        - 表格行不会从中间被切开（表格作为原子块输出）
        - 在段落边界处切分（空行、句号之后等）
        - 尽可能将较小的尾部/首部碎片与相邻分块合并

        参数：
            text: 待切分的 Markdown 文本
            max_chars: 每个分块的最大字符数，默认 4000
            len_fn: 可选的自定义长度函数（例如 UTF-16 长度）；默认使用内置 len

        返回：
            切分后的文本分块列表（均非空）
        """
        _len = len_fn or len

        if not text:
            return []

        if _len(text) <= max_chars:
            return [text]

        # 阶段 1：抽取原子块
        atoms = cls.split_into_atoms(text)

        # 阶段 2：贪心合并
        chunks: list[str] = []
        indivisible_set: set[int] = set()
        current_parts: list[str] = []
        current_len = 0

        def _flush_parts() -> None:
            if current_parts:
                chunks.append('\n\n'.join(current_parts))

        for atom in atoms:
            atom_len = _len(atom)
            sep_len = 2 if current_parts else 0
            projected_len = current_len + sep_len + atom_len

            if projected_len > max_chars and current_parts:
                _flush_parts()
                current_parts = []
                current_len = 0
                sep_len = 0

            if (not current_parts
                    and atom_len > max_chars
                    and (cls.is_fence_atom(atom) or cls.is_table_atom(atom))):
                indivisible_set.add(len(chunks))
                chunks.append(atom)
                continue

            current_parts.append(atom)
            current_len += sep_len + atom_len

        _flush_parts()

        # 阶段 3：后处理 —— 对仍然过大的分块按段落边界再次切分
        result: list[str] = []
        for idx, chunk in enumerate(chunks):
            if _len(chunk) <= max_chars:
                result.append(chunk)
                continue

            if idx in indivisible_set:
                result.append(chunk)
                continue

            if cls.has_unclosed_fence(chunk):
                result.append(chunk)
                continue

            remaining = chunk
            while _len(remaining) > max_chars:
                head, remaining = cls.split_at_paragraph_boundary(
                    remaining, max_chars, len_fn=len_fn,
                )
                if not head:
                    head, remaining = remaining[:max_chars], remaining[max_chars:]
                if head:
                    result.append(head)
            if remaining:
                result.append(remaining)

        # 阶段 4：将较小的尾部/首部碎片与相邻分块合并
        if len(result) > 1:
            merged: list[str] = [result[0]]
            for chunk in result[1:]:
                prev = merged[-1]
                combined = prev + '\n\n' + chunk
                if _len(combined) <= max_chars:
                    merged[-1] = combined
                else:
                    merged.append(chunk)
            result = merged

        return [c for c in result if c]

    # -- 块分隔符推断 -----------------------------------------

    @classmethod
    def infer_block_separator(cls, prev_chunk: str, next_chunk: str) -> str:
        """
        推断两个切分块之间应使用的分隔符。

        规则（与 TS 版 markdown-stream.ts 对齐）：
        - 前一个块以代码围栏结尾，或下一个块以围栏开头 → 单换行 '\\n'
        - 前一个块以表格行结尾且下一个块以表格行开头 → 单换行 '\\n'（表格续行）
        - 其他情况 → 双换行 '\\n\\n'（段落分隔符）

        参数：
            prev_chunk: 前一个块
            next_chunk: 下一个块

        返回：
            '\\n' 或 '\\n\\n'
        """
        prev_trimmed = prev_chunk.rstrip()
        next_trimmed = next_chunk.lstrip()

        # 前一个块以围栏结尾，或下一个块以围栏开头
        if prev_trimmed.endswith('```') or next_trimmed.startswith('```'):
            return '\n'

        # 表格续行
        if cls.ends_with_table_row(prev_chunk):
            first_line = next_trimmed.split('\n')[0].strip() if next_trimmed else ''
            if first_line.startswith('|') and first_line.endswith('|'):
                return '\n'

        return '\n\n'

    # -- 流式围栏合并 ---------------------------------------------

    @classmethod
    def merge_block_streaming_fences(cls, chunks: list[str]) -> list[str]:
        """
        面向流式输出、关注围栏完整性的分块合并。

        当流式输出产生多个在围栏中间被截断的分块时，
        尝试合并相邻分块以补全围栏。

        规则：
        - 若第 i 个块存在未闭合围栏，且第 i+1 个块以 ``` 开头，
            则把 i+1 合并进 i（直到围栏闭合或没有更多块）。
        - 合并时使用 infer_block_separator 推断分隔符。

        参数：
            chunks: 原始分块列表

        返回：
            合并后的分块列表（长度 <= 原始长度）
        """
        if not chunks:
            return []

        result: list[str] = []
        i = 0
        while i < len(chunks):
            current = chunks[i]
            # 若当前块存在未闭合围栏，尝试合并后续分块
            while cls.has_unclosed_fence(current) and i + 1 < len(chunks):
                sep = cls.infer_block_separator(current, chunks[i + 1])
                current = current + sep + chunks[i + 1]
                i += 1
            result.append(current)
            i += 1

        return result

    # -- 外层围栏剥离 ---------------------------------------------

    @staticmethod
    def strip_outer_markdown_fence(text: str) -> str:
        """
        剥离外层 Markdown 围栏。

        当 AI 回复整体被 ```markdown\\n...\\n``` 包裹时，去掉外层围栏、
        保留内容。仅当第一行为 ```markdown（不区分大小写）且最后一行为 ``` 时才剥离。

        参数：
            text: 待处理的文本

        返回：
            剥离外层围栏后的文本（不匹配则原样返回）
        """
        if not text:
            return text

        lines = text.split('\n')
        if len(lines) < 3:
            return text

        first_line = lines[0].strip()
        last_line = lines[-1].strip()

        # 第一行必须是 ```markdown（语言标记 md/markdown 可选）
        if not re.match(r'^```(?:markdown|md)?\s*$', first_line, re.IGNORECASE):
            return text

        # 最后一行必须是纯 ```
        if last_line != '```':
            return text

        # 去掉第一行和最后一行
        inner = '\n'.join(lines[1:-1])
        return inner

    # -- 表格清理 ------------------------------------------------

    @staticmethod
    def sanitize_markdown_table(text: str) -> str:
        """
        表格输出清理。

        处理 AI 生成的 Markdown 表格中常见的格式问题：
        1. 去掉表格行前后的多余空白
        2. 确保分隔行（|---|---|）格式正确
        3. 去掉空表格行

        参数：
            text: 含表格的 Markdown 文本

        返回：
            清理后的文本
        """
        if '|' not in text:
            return text

        lines = text.split('\n')
        result_lines: list[str] = []

        for line in lines:
            stripped = line.strip()

            # 表格行处理
            if stripped.startswith('|') and stripped.endswith('|'):
                # 分隔行归一化：| --- | --- | → |---|---|
                if re.match(r'^\|[\s\-:]+(\|[\s\-:]+)+\|$', stripped):
                    cells = stripped.split('|')
                    normalized = '|'.join(
                        cell.strip() if cell.strip() else cell
                        for cell in cells
                    )
                    result_lines.append(normalized)
                elif stripped == '||' or stripped.replace('|', '').strip() == '':
                    # 空表格行 → 跳过
                    continue
                else:
                    result_lines.append(stripped)
            else:
                result_lines.append(line)

        return '\n'.join(result_lines)

    # -- Markdown 提示词 ----------------------------------------------

    @staticmethod
    def markdown_hint_system_prompt() -> str:
        """
        Markdown 渲染提示（追加到系统提示中）。

        告知 AI 元宝平台支持 Markdown 渲染，包括：
        - 代码块（```lang）
        - 表格（| col | col |）
        - 粗体/斜体
        """
        return (
            "The current platform supports Markdown rendering. You can use the following formats:\n"
            "- Code blocks: ```language\\ncode\\n```\n"
            "- Tables: | col1 | col2 |\\n|---|---|\\n| val1 | val2 |\n"
            "- Bold: **text** / Italic: *text*\n"
            "Please use Markdown formatting when appropriate to improve readability."
        )

class SignManager:
    """封装元宝平台所有 sign-token 相关逻辑。

    管理 token 获取、缓存、签名计算以及自动重试。所有状态（缓存、锁）
    都以类级属性保存，这样一个共享的 client 就能服务整个进程。
    """

    # -- 常量 ---------------------------------------------------------

    TOKEN_PATH = "/api/v5/robotLogic/sign-token"

    RETRYABLE_CODE = 10099
    MAX_RETRIES = 3
    RETRY_DELAY_S = 1.0

    #: 提前刷新余量（秒），按实际过期时间提前 60 秒视为已过期
    CACHE_REFRESH_MARGIN_S = 60

    #: HTTP 超时（秒）
    HTTP_TIMEOUT_S = 10.0

    # -- 类级共享状态 ------------------------------------------

    # key: app_key → {"token", "bot_id", "expire_ts", ...}
    _cache: dict[str, dict[str, Any]] = {}

    # 每个 app_key 的刷新锁 —— 防止并发重复的 sign-token 请求。
    # 在 get_refresh_lock() 中惰性创建，该方法只在 async 上下文中调用，
    # 因此 Lock 总是绑定到正确的事件循环。
    # disconnect() 会清空这个字典，以避免跨重连出现失效的锁。
    _locks: dict[str, asyncio.Lock] = {}

    # -- 内部辅助方法 --------------------------------------------------

    @classmethod
    def get_refresh_lock(cls, app_key: str) -> asyncio.Lock:
        """返回（必要时创建）该 app_key 对应的刷新锁。

        必须在运行中的事件循环内（async 上下文）调用。
        """
        if app_key not in cls._locks:
            cls._locks[app_key] = asyncio.Lock()
        return cls._locks[app_key]

    @staticmethod
    def compute_signature(nonce: str, timestamp: str, app_key: str, app_secret: str) -> str:
        """计算 HMAC-SHA256 签名（与 TypeScript 原版对齐）。

        plain     = nonce + timestamp + app_key + app_secret
        signature = HMAC-SHA256(key=app_secret, msg=plain).hexdigest()
        """
        plain = nonce + timestamp + app_key + app_secret
        return hmac.new(app_secret.encode(), plain.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def build_timestamp() -> str:
        """构造北京时间 ISO-8601 时间戳（不含毫秒）。

        格式：2006-01-02T15:04:05+08:00
        """
        bjtime = datetime.now(tz=timezone(timedelta(hours=8)))
        return bjtime.strftime("%Y-%m-%dT%H:%M:%S+08:00")

    @classmethod
    def is_cache_valid(cls, entry: dict[str, Any]) -> bool:
        """判断缓存条目是否有效（未过期且留有余量）。"""
        return entry["expire_ts"] - time.time() > cls.CACHE_REFRESH_MARGIN_S

    @classmethod
    def clear_locks(cls) -> None:
        """清空所有 app_key 的刷新锁（断开连接时调用）。"""
        cls._locks.clear()

    @classmethod
    def purge_expired(cls) -> int:
        """从 token 缓存中移除所有过期条目。

        返回被清理的条目数量。由 ``get_token()`` 惰性调用，
        以免长时间运行的进程中堆积失效的 app_key 条目。
        """
        now = time.time()
        expired_keys = [
            k for k, v in cls._cache.items()
            if now - v.get("expire_ts", 0) > 0
        ]
        for k in expired_keys:
            cls._cache.pop(k, None)
        return len(expired_keys)

    # -- 核心：请求 -------------------------------------------------------

    @classmethod
    async def fetch(
        cls,
        app_key: str,
        app_secret: str,
        api_domain: str,
        route_env: str = "",
    ) -> dict[str, Any]:
        """发送 sign-ticket HTTP 请求，并自动重试（最多 MAX_RETRIES 次）。"""
        url = f"{api_domain.rstrip('/')}{cls.TOKEN_PATH}"
        async with httpx.AsyncClient(timeout=cls.HTTP_TIMEOUT_S) as client:
            for attempt in range(cls.MAX_RETRIES + 1):
                nonce = secrets.token_hex(16)
                timestamp = cls.build_timestamp()
                signature = cls.compute_signature(nonce, timestamp, app_key, app_secret)

                payload = {
                    "app_key": app_key,
                    "nonce": nonce,
                    "signature": signature,
                    "timestamp": timestamp,
                }

                headers = {
                    "Content-Type": "application/json",
                    "X-AppVersion": _APP_VERSION,
                    "X-OperationSystem": _OPERATION_SYSTEM,
                    "X-Instance-Id": _YUANBAO_INSTANCE_ID,
                    "X-Bot-Version": _BOT_VERSION,
                }
                if route_env:
                    headers["X-Route-Env"] = route_env

                logger.info(
                    "Sign token request: url=%s%s",
                    url,
                    f" (retry {attempt}/{cls.MAX_RETRIES})" if attempt > 0 else "",
                )

                response = await client.post(url, json=payload, headers=headers)

                if response.status_code != 200:
                    body = response.text
                    raise RuntimeError(f"Sign token API returned {response.status_code}: {body[:200]}")

                try:
                    result_data: dict[str, Any] = response.json()
                except Exception as exc:
                    raise ValueError(f"Sign token response parse error: {exc}") from exc

                code = result_data.get("code")
                if code == 0:
                    data = result_data.get("data")
                    if not isinstance(data, dict):
                        raise ValueError(f"Sign token response missing 'data' field: {result_data}")
                    logger.info("Sign token success: bot_id=%s", data.get("bot_id"))
                    return data

                if code == cls.RETRYABLE_CODE and attempt < cls.MAX_RETRIES:
                    logger.warning(
                        "Sign token retryable: code=%s, retrying in %ss (attempt=%d/%d)",
                        code,
                        cls.RETRY_DELAY_S,
                        attempt + 1,
                        cls.MAX_RETRIES,
                    )
                    await asyncio.sleep(cls.RETRY_DELAY_S)
                    continue

                msg = result_data.get("msg", "")
                raise RuntimeError(f"Sign token error: code={code}, msg={msg}")

        raise RuntimeError("Sign token failed: max retries exceeded")

    # -- 公共 API：获取（带缓存） --------------------------------------

    @classmethod
    async def get_token(
        cls,
        app_key: str,
        app_secret: str,
        api_domain: str,
        route_env: str = "",
    ) -> dict[str, Any]:
        """获取 WS 鉴权 token（带缓存）。

        缓存命中时直接返回、不再请求；按实际过期时间提前 60 秒视为已过期，
        以触发刷新。
        """
        # 惰性清理其他 app_key 的过期条目
        cls.purge_expired()

        cached = cls._cache.get(app_key)
        if cached and cls.is_cache_valid(cached):
            remain = int(cached["expire_ts"] - time.time())
            logger.info("Using cached token (%ds remaining)", remain)
            return dict(cached)

        async with cls.get_refresh_lock(app_key):
            cached = cls._cache.get(app_key)
            if cached and cls.is_cache_valid(cached):
                return dict(cached)

            data = await cls.fetch(app_key, app_secret, api_domain, route_env)

            duration: int = data.get("duration", 0)
            expire_ts = time.time() + duration if duration > 0 else time.time() + 3600

            cls._cache[app_key] = {
                "token": data.get("token", ""),
                "bot_id": data.get("bot_id", ""),
                "duration": duration,
                "product": data.get("product", ""),
                "source": data.get("source", ""),
                "expire_ts": expire_ts,
            }

        return dict(cls._cache[app_key])

    # -- 公共 API：强制刷新 -----------------------------------------

    @classmethod
    async def force_refresh(
        cls,
        app_key: str,
        app_secret: str,
        api_domain: str,
        route_env: str = "",
    ) -> dict[str, Any]:
        """强制刷新 token（清空缓存并重新签名）。"""
        logger.warning("[force-refresh] Clearing cache and re-signing token: app_key=****%s", app_key[-4:])
        async with cls.get_refresh_lock(app_key):
            cls._cache.pop(app_key, None)
            data = await cls.fetch(app_key, app_secret, api_domain, route_env)

            duration: int = data.get("duration", 0)
            expire_ts = time.time() + duration if duration > 0 else time.time() + 3600

            cls._cache[app_key] = {
                "token": data.get("token", ""),
                "bot_id": data.get("bot_id", ""),
                "duration": duration,
                "product": data.get("product", ""),
                "source": data.get("source", ""),
                "expire_ts": expire_ts,
            }

        return dict(cls._cache[app_key])


from dataclasses import dataclass, field as dc_field

@dataclass
class InboundContext:
    """在入站中间件管道中流转的可变上下文。

    每个中间件读取/写入该上下文中的字段。管道引擎按注册顺序
    将其传递给每一个中间件。
    """

    adapter: Any  # YuanbaoAdapter（前向引用以避免循环导入）
    raw_frames: list = dc_field(default_factory=list)  # 原始 bytes 帧（防抖聚合）

    # 由 DecodeMiddleware 填充
    push: Optional[dict] = None
    decoded_via: str = ""  # "json" | "protobuf"

    # 由 FieldExtractMiddleware 从 push 中提取
    from_account: str = ""
    group_code: str = ""
    group_name: str = ""
    sender_nickname: str = ""
    msg_body: list = dc_field(default_factory=list)
    msg_id: str = ""
    cloud_custom_data: str = ""

    # 由 ChatRoutingMiddleware 推导
    chat_id: str = ""
    chat_type: str = ""  # "dm" | "group"
    chat_name: str = ""

    # 由 ContentExtractMiddleware 填充
    raw_text: str = ""
    media_refs: list = dc_field(default_factory=list)

    # 由 ExtractContentMiddleware 为 elem_type 1009（微信转发）填充。
    # 包含解析后的 ForwardMsgData dict（sub_type / nick_name / msg 列表）。
    forwarded_records: Optional[dict] = None

    # owner 命令检测
    owner_command: Optional[str] = None

    # 由 BuildSourceMiddleware 构建的 source
    source: Optional[Any] = None  # SessionSource

    # 由 ClassifyMessageTypeMiddleware 填充
    msg_type: Optional[Any] = None  # MessageType | YuanbaoMessageType

    # 由 QuoteContextMiddleware 填充
    reply_to_message_id: Optional[str] = None
    reply_to_text: Optional[str] = None
    quote_media_refs: list = dc_field(default_factory=list)  # (rid, kind, filename) 列表

    # 由 MediaResolveMiddleware 填充。合并后的已解析本地路径列表，来源最多有三处
    # （去重后按以下顺序）：
    #   1) 当前消息自身携带的媒体（总是包含），
    #   2) 被引用消息中的媒体（当 reply_to_message_id 已设置时），
    #   3) 最近的群内观察媒体（仅当 chat_type == "group" 且不存在引用时）。
    media_urls: list = dc_field(default_factory=list)
    media_types: list = dc_field(default_factory=list)

    # 由 ExtractContentMiddleware 填充
    link_urls: list = dc_field(default_factory=list)

    # 由 GroupAttributionMiddleware 填充
    channel_prompt: Optional[str] = None


class InboundMiddleware(ABC):
    """所有入站管道中间件的抽象基类。

    子类必须：
      - 将 ``name`` 设为类级属性（用于管道注册以及动态插入/移除）。
      - 实现 ``async handle(ctx, next_fn)``，包含中间件逻辑。

    约定：
      - 调用 ``await next_fn()`` 将控制权传递给下一个中间件。
      - 不调用 ``next_fn`` 直接返回则会**停止**管道。
    """

    name: str = ""  # 在每个子类中覆写

    @abstractmethod
    async def handle(self, ctx: InboundContext, next_fn: Callable) -> None:
        """处理 *ctx*，并可选地调用 *next_fn* 以继续管道。"""

    async def __call__(self, ctx: InboundContext, next_fn: Callable) -> None:
        """允许中间件实例被直接调用（鸭子类型兼容）。"""
        return await self.handle(ctx, next_fn)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r}>"


class InboundPipeline:
    """洋葱模型的中间件管道引擎，用于处理入站消息。

    灵感来自 OpenClaw 的 MessagePipeline（extensions/yuanbao/src/business/
    pipeline/engine.ts）。支持具名中间件、条件守卫（``when``），
    以及 ``use_before`` / ``use_after`` / ``remove`` 进行动态组合。

    同时接受 ``InboundMiddleware`` 实例（OOP 风格）和普通的
    ``async def(ctx, next_fn)`` 可调用对象（函数式风格），以提供灵活性。
    """

    def __init__(self) -> None:
        self._middlewares: list = []  # (name, handler, when_fn | None) 列表

    # -- 内部辅助方法 --------------------------------------------------

    @staticmethod
    def _normalize(name_or_mw, handler=None):
        """将 (name, handler) 或 (InboundMiddleware,) 归一化为 (name, callable)。"""
        if isinstance(name_or_mw, InboundMiddleware):
            return name_or_mw.name, name_or_mw
        # 函数式风格：name 为字符串，handler 为可调用对象
        return name_or_mw, handler

    # -- 注册 API --------------------------------------------------

    def use(self, name_or_mw, handler=None, when=None) -> "InboundPipeline":
        """在管道末尾追加一个中间件。

        接受以下两种形式之一：
          - ``pipeline.use(SomeMiddleware())``  —— OOP 风格
          - ``pipeline.use("name", some_fn)``   —— 函数式风格
        """
        name, h = self._normalize(name_or_mw, handler)
        self._middlewares.append((name, h, when))
        return self

    def use_before(self, target: str, name_or_mw, handler=None, when=None) -> "InboundPipeline":
        """在 *target*（按名称）之前插入一个中间件。找不到则追加到末尾。"""
        name, h = self._normalize(name_or_mw, handler)
        idx = next((i for i, (n, _, _) in enumerate(self._middlewares) if n == target), None)
        entry = (name, h, when)
        if idx is None:
            self._middlewares.append(entry)
        else:
            self._middlewares.insert(idx, entry)
        return self

    def use_after(self, target: str, name_or_mw, handler=None, when=None) -> "InboundPipeline":
        """在 *target*（按名称）之后插入一个中间件。找不到则追加到末尾。"""
        name, h = self._normalize(name_or_mw, handler)
        idx = next((i for i, (n, _, _) in enumerate(self._middlewares) if n == target), None)
        entry = (name, h, when)
        if idx is None:
            self._middlewares.append(entry)
        else:
            self._middlewares.insert(idx + 1, entry)
        return self

    def remove(self, name: str) -> "InboundPipeline":
        """按名称移除一个中间件。"""
        self._middlewares = [(n, h, w) for n, h, w in self._middlewares if n != name]
        return self

    @property
    def middleware_names(self) -> list:
        """返回已注册中间件名称的有序列表（用于测试）。"""
        return [n for n, _, _ in self._middlewares]

    # -- 执行 ---------------------------------------------------------

    async def execute(self, ctx: InboundContext) -> None:
        """按顺序运行所有中间件。每个中间件接收 ``(ctx, next_fn)``。"""
        chain = self._middlewares
        index = 0

        async def next_fn() -> None:
            nonlocal index
            while index < len(chain):
                name, handler, when_fn = chain[index]
                index += 1
                # 条件守卫：when 返回 False 时跳过
                if when_fn is not None and not when_fn(ctx):
                    continue
                try:
                    await handler(ctx, next_fn)
                except Exception:
                    logger.error("[InboundPipeline] middleware [%s] error", name, exc_info=True)
                    raise
                return
            # 链末尾 —— 无更多操作

        await next_fn()
class DecodeMiddleware(InboundMiddleware):
    """将原始入站帧从 JSON 或 Protobuf 解码到 ctx.push。

    封装 JSON push 解析（与 TS decodeFromContent 对齐）
    以及通过 ``decode_inbound_push`` 进行的 Protobuf 解码。
    """

    name = "decode"

    # -- JSON push 解析 -------------------------------------------------

    @staticmethod
    def convert_json_msg_body(raw_body: list) -> list:
        """将原始 JSON msg_body 数组归一化为 [{"msg_type": str, "msg_content": dict}]。

        同时兼容 PascalCase（MsgType/MsgContent）和
        snake_case（msg_type/msg_content）命名。
        """
        result = []
        for item in raw_body or []:
            if not isinstance(item, dict):
                continue
            msg_type = item.get("msg_type") or item.get("MsgType", "")
            msg_content = item.get("msg_content") or item.get("MsgContent", {})
            if isinstance(msg_content, str):
                try:
                    msg_content = json.loads(msg_content)
                except Exception:
                    msg_content = {"text": msg_content}
            result.append({"msg_type": msg_type, "msg_content": msg_content or {}})
        return result

    @staticmethod
    def parse_json_push(raw_json: dict) -> dict | None:
        """将 JSON 格式的 push 转换为与 ``decode_inbound_push`` 相同结构的 dict。

        支持标准回调格式（callback_command + from_account +
        msg_body）以及遗留格式字段（GroupId、MsgSeq、MsgKey、MsgBody 等）。
        """
        if not raw_json:
            return None

        # 腾讯 IM 回调格式使用 PascalCase（From_Account、To_Account、MsgBody）。
        # 内部格式使用 snake_case（from_account、to_account、msg_body）。
        # 两者均支持。
        from_account = (
            raw_json.get("from_account", "")
            or raw_json.get("From_Account", "")
        )
        group_code = (
            raw_json.get("group_code", "")
            or raw_json.get("GroupId", "")
            or raw_json.get("group_id", "")
        )
        msg_body_raw = (
            raw_json.get("msg_body", [])
            or raw_json.get("MsgBody", [])
        )
        msg_body = DecodeMiddleware.convert_json_msg_body(msg_body_raw)

        # 撤回回调可能既没有 from_account 也没有 msg_body。
        if not from_account and not msg_body and not raw_json.get("callback_command"):
            return None

        return {
            "callback_command": raw_json.get("callback_command", ""),
            "from_account": from_account,
            "to_account": raw_json.get("to_account", "") or raw_json.get("To_Account", ""),
            "sender_nickname": raw_json.get("sender_nickname", "") or raw_json.get("nick_name", ""),
            "group_code": group_code,
            "group_name": raw_json.get("group_name", ""),
            "msg_seq": raw_json.get("msg_seq", 0) or raw_json.get("MsgSeq", 0),
            "msg_id": raw_json.get("msg_id", "") or raw_json.get("msg_key", "") or raw_json.get("MsgKey", ""),
            "msg_body": msg_body,
            "cloud_custom_data": raw_json.get("cloud_custom_data", "") or raw_json.get("CloudCustomData", ""),
            "bot_owner_id": raw_json.get("bot_owner_id", "") or raw_json.get("botOwnerId", ""),
            "recall_msg_seq_list": raw_json.get("recall_msg_seq_list") or None,
            "trace_id": (raw_json.get("log_ext") or {}).get("trace_id", "") if isinstance(raw_json.get("log_ext"), dict) else "",
        }

    # -- 管道 handler --------------------------------------------------

    def _decode_single(self, adapter, data: bytes) -> tuple:
        """将单个原始帧解码为 (push_dict, decoded_via)，或 (None, '')。"""
        try:
            conn_json = json.loads(data.decode("utf-8"))
        except Exception:
            conn_json = None

        if isinstance(conn_json, dict):
            push = self.parse_json_push(conn_json)
            if push:
                return push, "json"
        else:
            try:
                push = decode_inbound_push(data)
            except Exception:
                push = None
            if push:
                return push, "protobuf"

        return None, ""

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        data_list = ctx.raw_frames
        if not data_list:
            return  # 停止管道 —— 无内容可解码

        merged_push = None
        decoded_via = ""

        for data in data_list:
            push, via = self._decode_single(ctx.adapter, data)
            if not push:
                logger.info(
                "[%s] Push decoded but no valid message. raw hex(first64)=%s",
                    ctx.adapter.name, data.hex()[:128] if data else "(empty)",
                )
                continue

            if merged_push is None:
                # 第一个有效 push 作为基础
                merged_push = push
                decoded_via = via
                logger.info(
                "[%s] Frame decoded (via=%s): len=%d",
                    ctx.adapter.name, via, len(data),
                )
            else:
                # 后续 push：将其 msg_body 合并到基础 push 中
                extra_body = push.get("msg_body", [])
                if extra_body:
                    _sep = {"msg_type": "TIMTextElem", "msg_content": {"text": "\n"}}
                    merged_push["msg_body"] = merged_push.get("msg_body", []) + [_sep] + extra_body
                    logger.info(
                        "[%s] Merged %d extra msg_body elements from aggregated push",
                        ctx.adapter.name, len(extra_body),
                    )

        if not merged_push:
            return  # 停止管道

        ctx.push = merged_push
        ctx.decoded_via = decoded_via

        logger.info(
            "[%s] Push decoded (via=%s): from=%s group=%s msg_id=%s msg_types=%s",
            ctx.adapter.name, ctx.decoded_via,
            ctx.push.get("from_account", ""),
            ctx.push.get("group_code", ""),
            ctx.push.get("msg_id", ""),
            [e.get("msg_type", "") for e in ctx.push.get("msg_body", [])],
        )
        logger.debug("[%s] Push payload: %s", ctx.adapter.name, ctx.push)

        await next_fn()


class ExtractFieldsMiddleware(InboundMiddleware):
    """从 ctx.push 中提取公共字段到 ctx 属性。"""

    name = "extract-fields"

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        push = ctx.push
        ctx.from_account = push.get("from_account", "")
        ctx.group_code = push.get("group_code", "")
        ctx.group_name = push.get("group_name", "")
        ctx.sender_nickname = push.get("sender_nickname", "")
        ctx.msg_body = push.get("msg_body", [])
        ctx.msg_id = push.get("msg_id", "")
        ctx.cloud_custom_data = push.get("cloud_custom_data", "")
        await next_fn()


class DedupMiddleware(InboundMiddleware):
    """入站消息去重。"""

    name = "dedup"

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        if ctx.msg_id and ctx.adapter._dedup.is_duplicate(ctx.msg_id):
            logger.debug("[%s] Duplicate message ignored: msg_id=%s", ctx.adapter.name, ctx.msg_id)
            return  # 停止管道
        await next_fn()


class RecallGuardMiddleware(InboundMiddleware):
    """拦截 Group.CallbackAfterRecallMsg / C2C.CallbackAfterMsgWithDraw。

    分支 A：消息在会话记录中（已观察但尚未消费）→ 抹除内容
    分支 B：消息不在会话记录中 → 追加系统提示
    分支 C：消息正在处理中 → 静默中断 + 延迟抹除
    """

    name = "recall_guard"

    _RECALL_COMMANDS = frozenset({
        "Group.CallbackAfterRecallMsg",
        "C2C.CallbackAfterMsgWithDraw",
    })
    _REDACTED = "[This message was recalled/withdrawn by the sender; original content removed]"

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        cmd = (ctx.push or {}).get("callback_command", "")
        if cmd not in self._RECALL_COMMANDS:
            await next_fn()
            return
        self._handle_recall(ctx, cmd)

    @staticmethod
    def _build_source(adapter, group_code: str, from_account: str):
        return adapter.build_source(
            chat_id=(f"group:{group_code}" if group_code else f"direct:{from_account}"),
            chat_type="group" if group_code else "dm",
            user_id=from_account or None,
            thread_id="main" if group_code else None,
        )

    def _handle_recall(self, ctx: InboundContext, cmd: str) -> None:
        adapter = ctx.adapter
        push = ctx.push or {}

        if cmd == "Group.CallbackAfterRecallMsg":
            seq_list = push.get("recall_msg_seq_list") or []
        else:
            mid = push.get("msg_id") or ""
            seq = push.get("msg_seq")
            seq_list = [{"msg_id": mid, "msg_seq": seq}] if (mid or seq) else []

        if not seq_list:
            logger.debug("[%s] Recall callback with empty seq_list, skipping", adapter.name)
            return

        group_code = (push.get("group_code") or "").strip()
        from_account = (push.get("from_account") or "").strip()

        for seq_entry in seq_list:
            recalled_id = seq_entry.get("msg_id") or str(seq_entry.get("msg_seq") or "")
            if not recalled_id:
                continue

            matched_sk = self._find_processing_session(adapter, recalled_id)
            if matched_sk is not None:
                self._interrupt_for_recall(adapter, matched_sk, recalled_id, group_code, from_account)
            else:
                recalled_content = adapter._msg_content_cache.get(recalled_id)
                self._patch_transcript(adapter, recalled_id, group_code, from_account, recalled_content)

    # -- 分支 C：中断当前正在处理的消息 ---------------

    @staticmethod
    def _find_processing_session(adapter, recalled_id: str) -> Optional[str]:
        for sk, mid in adapter._processing_msg_ids.items():
            if mid == recalled_id and sk in adapter._active_sessions:
                return sk
        return None

    @classmethod
    def _interrupt_for_recall(cls, adapter, session_key: str, recalled_id: str,
                              group_code: str, from_account: str) -> None:
        where = f"group {group_code}" if group_code else f"direct chat with {from_account}"
        recall_text = (
            f"[CRITICAL — MESSAGE RECALLED] The user message that triggered "
            f"your current task (message_id=\"{recalled_id}\") in {where} has "
            f"been recalled/withdrawn by the sender. "
            f"IGNORE any prior system note asking you to finish processing "
            f"tool results — the original request is void. "
            f"Do NOT continue the task, do NOT call more tools, do NOT "
            f"reference the recalled content. "
            f"Reply only with a brief acknowledgment such as "
            f"\"The message has been recalled.\" in the "
            f"language the user was using."
        )

        synth_event = MessageEvent(
            text=recall_text,
            message_type=MessageType.TEXT,
            source=cls._build_source(adapter, group_code, from_account),
            internal=True,
        )
        # 直接设置 pending 并触发信号（绕过 handle_message 以避免忙应答）。
        # 可能覆盖同一 ~200ms 窗口内 pending 的用户消息 —— 可以接受。
        adapter._pending_messages[session_key] = synth_event
        active_event = adapter._active_sessions.get(session_key)
        if active_event is not None:
            active_event.set()

        logger.info("[%s] Recall interrupt: msg_id=%s session=%s", adapter.name, recalled_id, session_key[:30])

        # 被中断的轮次会在我们的中断*之后*才持久化被撤回的内容 ——
        # 安排一次延迟抹除来清理它。
        recalled_text = adapter._processing_msg_texts.get(session_key, "")
        if recalled_text:
            cls._schedule_content_redact(adapter, session_key, recalled_text, group_code, from_account)

    @classmethod
    def _schedule_content_redact(cls, adapter, session_key: str, recalled_text: str,
                                 group_code: str, from_account: str) -> None:
        async def _redact() -> None:
            store = getattr(adapter, "_session_store", None)
            if not store:
                return
            try:
                sid = store.get_or_create_session(
                    cls._build_source(adapter, group_code, from_account),
                ).session_id
            except Exception:
                return
            # 轮询直到被撤回的内容出现在会话记录中 —— 调度时
            # 被中断的轮次尚未完成写入。
            for _ in range(30):
                await asyncio.sleep(0.5)
                try:
                    transcript = store.load_transcript(sid)
                except Exception:
                    continue
                for entry in transcript:
                    if entry.get("role") == "user" and entry.get("content") == recalled_text:
                        entry["content"] = cls._REDACTED
                        try:
                            store.rewrite_transcript(sid, transcript)
                            logger.info("[%s] Recall redact: session %s", adapter.name, session_key[:30])
                        except Exception as exc:
                            logger.warning("[%s] Recall redact failed: %s", adapter.name, exc)
                        return
            logger.debug("[%s] Recall redact: content not found after polling, session %s", adapter.name, session_key[:30])

        task = asyncio.create_task(_redact())
        adapter._background_tasks.add(task)
        task.add_done_callback(adapter._background_tasks.discard)

    # -- 分支 A/B：修改会话记录（会话闲置时） --------------------

    @classmethod
    def _patch_transcript(cls, adapter, recalled_id: str, group_code: str,
                          from_account: str, recalled_content: Optional[str] = None) -> None:
        store = getattr(adapter, "_session_store", None)
        if not store:
            return
        try:
            sid = store.get_or_create_session(cls._build_source(adapter, group_code, from_account)).session_id
        except Exception as exc:
            logger.warning("[%s] Recall: failed to resolve session: %s", adapter.name, exc)
            return

        # 从权威存储（state.db）加载会话记录。由于 PR #29278 在 messages 表中
        # 新增了 ``platform_message_id`` 列，且 ``append_to_transcript`` 会把
        # 入站 dict 的 ``message_id`` 写入该列，因此 ``load_transcript``
        # 返回的行中，凡是被观察到带有 message_id 的消息都会带上该字段 ——
        # 分支 A1（精确 id 匹配）再次成为权威路径。
        try:
            transcript = store.load_transcript(sid)
        except Exception as exc:
            logger.warning("[%s] Recall: failed to load transcript: %s", adapter.name, exc)
            return

        # 分支 A1：精确 platform message_id 匹配。当行在持久化时带有
        # platform_message_id（被观察的群消息，以及任何 adapter 携带了
        # msg_id 的入站消息），此分支为权威路径。
        target = None
        branch_label = ""
        for entry in transcript:
            if entry.get("message_id") == recalled_id:
                target = entry
                branch_label = "branch A1: id match"
                break
        # 分支 A2：内容匹配兜底，用于行上缺少精确 platform id 的消息 ——
        # 例如 agent 处理过的 @bot 消息（run.py 不会把 msg_id 透传过来），
        # 或在 platform_message_id 列出现之前持久化的旧行。
        if target is None and recalled_content:
            for entry in transcript:
                if entry.get("role") == "user" and entry.get("content") == recalled_content:
                    target = entry
                    branch_label = "branch A2: content match"
                    break
        if target is not None:
            target["content"] = cls._REDACTED
            try:
                store.rewrite_transcript(sid, transcript)
                logger.info("[%s] Recall: redacted msg_id=%s (%s)", adapter.name, recalled_id, branch_label)
            except Exception as exc:
                logger.warning("[%s] Recall: rewrite_transcript failed: %s", adapter.name, exc)
            return

        # 分支 B：会话记录中找不到 → 追加系统提示
        store.append_to_transcript(sid, {
            "role": "system",
            "content": f'[recall] message_id="{recalled_id}" has been recalled; do not quote or reference it.',
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        })
        logger.info("[%s] Recall: system note for msg_id=%s (branch B)", adapter.name, recalled_id)


class SkipSelfMiddleware(InboundMiddleware):
    """过滤掉 bot 自身发送的消息。"""

    name = "skip-self"

    @staticmethod
    def _is_self_reference(from_account: str, bot_id: Optional[str]) -> bool:
        """检测消息是否来自 bot 自身。"""
        if not from_account or not bot_id:
            return False
        return from_account == bot_id

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        if self._is_self_reference(ctx.from_account, ctx.adapter._bot_id):
            logger.debug("[%s] Ignoring self-sent message from %s", ctx.adapter.name, ctx.from_account)
            return  # 停止管道
        await next_fn()


class ChatRoutingMiddleware(InboundMiddleware):
    """从 push 字段推导 chat_id、chat_type、chat_name。"""

    name = "chat-routing"

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        if ctx.group_code:
            ctx.chat_id = f"group:{ctx.group_code}"
            ctx.chat_type = "group"
            ctx.chat_name = ctx.group_name or ctx.group_code
        else:
            ctx.chat_id = f"direct:{ctx.from_account}"
            ctx.chat_type = "dm"
            ctx.chat_name = ctx.sender_nickname or ctx.from_account
        await next_fn()


class AccessPolicy:
    """平台级 DM / Group 访问控制策略。

    封装允许/拒绝逻辑，使入站中间件和出站 ``send_dm``
    可以共享同一套规则，而无需深入 adapter 内部实现。
    """

    def __init__(
        self,
        dm_policy: str,
        dm_allow_from: list[str],
        group_policy: str,
        group_allow_from: list[str],
    ) -> None:
        self._dm_policy = dm_policy
        self._dm_allow_from = dm_allow_from
        self._group_policy = group_policy
        self._group_allow_from = group_allow_from

    def is_dm_allowed(self, sender_id: str) -> bool:
        """平台级 DM 入站过滤（open / allowlist / disabled）。"""
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return sender_id.strip() in self._dm_allow_from
        return True

    def is_group_allowed(self, group_code: str) -> bool:
        """平台级群聊入站过滤（open / allowlist / disabled）。"""
        if self._group_policy == "disabled":
            return False
        if self._group_policy == "allowlist":
            return group_code.strip() in self._group_allow_from
        return True

    @property
    def dm_policy(self) -> str:
        return self._dm_policy

    @property
    def group_policy(self) -> str:
        return self._group_policy


class AccessGuardMiddleware(InboundMiddleware):
    """平台级 DM/Group 访问控制过滤器。"""

    name = "access-guard"

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        adapter = ctx.adapter
        policy: AccessPolicy = adapter._access_policy
        if ctx.chat_type == "dm":
            if not policy.is_dm_allowed(ctx.from_account):
                logger.debug(
                    "[%s] DM from %s blocked by dm_policy=%s",
                    adapter.name, ctx.from_account, policy.dm_policy,
                )
                return  # Stop pipeline
        elif ctx.chat_type == "group":
            if not policy.is_group_allowed(ctx.group_code):
                logger.debug(
                    "[%s] Group %s blocked by group_policy=%s",
                    adapter.name, ctx.group_code, policy.group_policy,
                )
                return  # Stop pipeline
        await next_fn()


class AutoSetHomeMiddleware(InboundMiddleware):
    """自动将第一个入站会话指定为元宝 home channel。

    在未配置 home channel 时触发，或在已有的群聊 home 被首个 DM 取代时触发
    （direct > group 升级）。
    静默执行：写入 config.yaml 和环境变量，不产生面向用户的消息。
    """

    name = "auto-sethome"

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        adapter = ctx.adapter
        if not adapter._auto_sethome_done:
            _cur_home = os.getenv("YUANBAO_HOME_CHANNEL", "")
            _should_set = (
                not _cur_home
                or (_cur_home.startswith("group:") and ctx.chat_type == "dm")
            )
            if ctx.chat_type == "dm":
                adapter._auto_sethome_done = True  # DM seen — no further upgrades needed
            if _should_set:
                try:
                    from hermes_constants import get_hermes_home
                    from utils import atomic_yaml_write
                    import yaml

                    _home = get_hermes_home()
                    config_path = _home / "config.yaml"
                    user_config: dict = {}
                    if config_path.exists():
                        with open(config_path, encoding="utf-8") as f:
                            user_config = yaml.safe_load(f) or {}
                    user_config["YUANBAO_HOME_CHANNEL"] = ctx.chat_id
                    atomic_yaml_write(config_path, user_config)
                    os.environ["YUANBAO_HOME_CHANNEL"] = str(ctx.chat_id)
                    logger.info(
                        "[%s] Auto-sethome: designated %s (%s) as Yuanbao home channel",
                        adapter.name, ctx.chat_id, ctx.chat_name,
                    )
                    # 静默 auto-sethome：无面向用户的消息，仅记录日志
                except Exception as e:
                    logger.warning("[%s] Auto-sethome failed: %s", adapter.name, e)
        await next_fn()


class ExtractContentMiddleware(InboundMiddleware):
    """从 msg_body 中提取原始文本和媒体引用。"""

    name = "extract-content"

    _CARD_CONTENT_MAX_LENGTH = 1000

    @staticmethod
    def _format_shared_link(custom: dict) -> str:
        """将 elem_type 1010（分享卡片）格式化为方括号占位符文本。"""
        title = custom.get("title", "")
        link = custom.get("link", "")
        header = f"[share_card: {title} | {link}]" if link else f"[share_card: {title}]"
        lines = [header]
        max_len = ExtractContentMiddleware._CARD_CONTENT_MAX_LENGTH
        for field in ("card_content", "wechat_des"):
            val = custom.get(field)
            if val and isinstance(val, str):
                preview = val[:max_len] + "...(truncated)" if len(val) > max_len else val
                lines.append(f"Preview: {preview}")
                break
        if link:
            lines.append("[visit link for full content]")
        return "\n".join(lines)

    @staticmethod
    def _format_link_understanding(custom: dict) -> Optional[str]:
        """将 elem_type 1007（链接理解卡片）格式化为方括号占位符文本。"""
        content = custom.get("content")
        if not content:
            return None
        try:
            parsed = json.loads(content)
            link = parsed.get("link") if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            link = None
        if not link or not isinstance(link, str):
            return None
        return f"[link: {link} | visit link for full content]"

    @staticmethod
    def _parse_resource_id(url: str) -> str:
        """从元宝资源 URL 的查询参数中提取 resourceId。

        参数：
            url: 资源 URL（如 https://...?resourceId=abc123）

        返回：
            Resource ID 字符串，未找到时返回空字符串
        """
        if not url:
            return ""
        try:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            ids = query.get("resourceId") or query.get("resourceid") or []
            return str(ids[0]).strip() if ids else ""
        except Exception:
            return ""

    @classmethod
    def _extract_text(cls, msg_body: list) -> str:
        """从 MsgBody 中提取纯文本内容。

        - TIMTextElem      -> text 字段
        - TIMImageElem     -> "[image]" / "[image|ybres:RID]"
        - TIMFileElem      -> "[file: {filename}]" / "[file:{name}|ybres:RID]"
        - TIMSoundElem     -> "[voice]" / "[voice|ybres:RID]"
        - TIMVideoFileElem -> "[video]" / "[video|ybres:RID]"
        - TIMFaceElem      -> "[emoji: {name}]" 或 "[emoji]"
        - TIMCustomElem    -> 尝试提取 data 字段，否则为 "[custom message]"
        - 多个元素以空格连接
        """
        parts: list[str] = []
        for elem in msg_body:
            elem_type: str = elem.get("msg_type", "")
            content: dict = elem.get("msg_content", {})

            if elem_type == "TIMTextElem":
                text = content.get("text", "")
                if text:
                    parts.append(text)
            elif elem_type == "TIMImageElem":
                # 从 image_info_array URL 中提取 resourceId
                image_info_array = content.get("image_info_array")
                if not isinstance(image_info_array, list):
                    image_info_array = []
                image_info = None
                # 优先取中图（index 1），其次取 index 0
                if len(image_info_array) > 1 and isinstance(image_info_array[1], dict):
                    image_info = image_info_array[1]
                elif len(image_info_array) > 0 and isinstance(image_info_array[0], dict):
                    image_info = image_info_array[0]
                image_url = str((image_info or {}).get("url") or "").strip()
                rid = cls._parse_resource_id(image_url)
                parts.append(f"[image|ybres:{rid}]" if rid else "[image]")
            elif elem_type == "TIMFileElem":
                filename = content.get("file_name", content.get("fileName", content.get("filename", "")))
                file_url = str(content.get("url") or "").strip()
                rid = cls._parse_resource_id(file_url)
                if rid:
                    parts.append(f"[file:{filename}|ybres:{rid}]" if filename else f"[file|ybres:{rid}]")
                else:
                    parts.append(f"[file: {filename}]" if filename else "[file]")
            elif elem_type == "TIMSoundElem":
                sound_url = str(content.get("url") or "").strip()
                rid = cls._parse_resource_id(sound_url)
                parts.append(f"[voice|ybres:{rid}]" if rid else "[voice]")
            elif elem_type == "TIMVideoFileElem":
                video_url = str(content.get("url") or "").strip()
                rid = cls._parse_resource_id(video_url)
                parts.append(f"[video|ybres:{rid}]" if rid else "[video]")
            elif elem_type == "TIMCustomElem":
                data_val = content.get("data", "")
                if data_val:
                    try:
                        custom = json.loads(data_val)
                        if not isinstance(custom, dict):
                            parts.append("[unsupported message type]")
                            continue
                        ctype = custom.get("elem_type")
                        if ctype == 1002:
                            parts.append(custom.get("text", "[mention]"))
                        elif ctype == 1010:
                            parts.append(cls._format_shared_link(custom))
                        elif ctype == 1007:
                            text = cls._format_link_understanding(custom)
                            if text:
                                parts.append(text)
                            else:
                                parts.append("[unsupported message type]")
                        elif ctype == 1009:
                            # 微信转发聊天记录：使用截断后的摘要文本。
                            parts.append(custom.get("text", "[chat record]"))
                        else:
                            parts.append("[unsupported message type]")
                    except (json.JSONDecodeError, TypeError):
                        parts.append(data_val)
                else:
                    parts.append("[unsupported message type]")
            elif elem_type == "TIMFaceElem":
                # 贴纸/表情：从 data JSON 中提取名称
                raw_data = content.get("data", "")
                face_name = ""
                if raw_data:
                    try:
                        face_data = json.loads(raw_data)
                        face_name = (face_data.get("name") or "").strip()
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        pass
                parts.append(f"[emoji: {face_name}]" if face_name else "[emoji]")
            elif elem_type:
                # 未知元素类型 —— 将类型作为占位符包含进来
                parts.append(f"[{elem_type}]")

        return " ".join(parts) if parts else ""

    @staticmethod
    def _rewrite_slash_command(text: str) -> str:
        """归一化输入文本：去除空白，并将全角斜杠（中文输入法）转换为
        ASCII 斜杠，使命令能被正确识别。
        """
        text = text.strip()
        if text.startswith('\uff0f'):  # Full-width slash
            text = '/' + text[1:]
        return text

    @staticmethod
    def _extract_inbound_media_refs(msg_body: list) -> List[Dict[str, str]]:
        """从 TIM msg_body 中提取入站图片/文件引用。

        返回示例：
          [{"kind": "image", "url": "https://..."}, {"kind": "file", "url": "...", "name": "a.pdf"}]
        """
        refs: List[Dict[str, str]] = []
        for elem in msg_body or []:
            if not isinstance(elem, dict):
                continue
            msg_type = elem.get("msg_type", "")
            content = elem.get("msg_content", {}) or {}
            if not isinstance(content, dict):
                continue

            if msg_type == "TIMImageElem":
                # 优先取中图（index 1），其次取 index 0。
                image_info_array = content.get("image_info_array")
                if not isinstance(image_info_array, list):
                    image_info_array = []
                image_info = None
                if len(image_info_array) > 1 and isinstance(image_info_array[1], dict):
                    image_info = image_info_array[1]
                elif len(image_info_array) > 0 and isinstance(image_info_array[0], dict):
                    image_info = image_info_array[0]
                image_url = str((image_info or {}).get("url") or "").strip()
                if image_url:
                    refs.append({"kind": "image", "url": image_url})
                continue

            if msg_type == "TIMFileElem":
                file_url = str(content.get("url") or "").strip()
                file_name = (
                    str(content.get("file_name") or "").strip()
                    or str(content.get("fileName") or "").strip()
                    or str(content.get("filename") or "").strip()
                )
                if file_url:
                    ref: Dict[str, str] = {"kind": "file", "url": file_url}
                    if file_name:
                        ref["name"] = file_name
                    refs.append(ref)
        return refs

    @staticmethod
    def _extract_link_urls(msg_body: list) -> list:
        """从分享卡片（1010）和链接理解（1007）自定义元素中提取链接 URL。"""
        urls: list[str] = []
        for elem in msg_body or []:
            if not isinstance(elem, dict) or elem.get("msg_type") != "TIMCustomElem":
                continue
            data_str = (elem.get("msg_content") or {}).get("data", "")
            if not data_str:
                continue
            try:
                custom = json.loads(data_str)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(custom, dict):
                continue
            ctype = custom.get("elem_type")
            if ctype == 1010:
                link = custom.get("link")
                if link and isinstance(link, str):
                    urls.append(link)
            elif ctype == 1007:
                content = custom.get("content")
                if content:
                    try:
                        parsed = json.loads(content)
                        link = parsed.get("link") if isinstance(parsed, dict) else None
                        if link and isinstance(link, str):
                            urls.append(link)
                    except (json.JSONDecodeError, TypeError):
                        pass
        return urls

    @staticmethod
    def _extract_forwarded_records(msg_body: list, user_id: str = "") -> Optional[dict]:
        """为 elem_type 1009（微信转发）从 ext_map 中提取 ForwardMsgData。

        详细的聊天记录 payload 存放在 ``msg_content.ext_map``
        （protobuf field 999，``map<string, string>``）中：
          - key 格式：``wexin_forward_msg_[forward_msg_id]_[userid]``
          - value：**base64 编码的 protobuf** ``ForwardMsgData``（不是 JSON）。
            先用 base64 解码，再用 ``decode_forward_msg_data`` 还原出
            ``sub_type`` / ``nick_name`` / ``msg`` 结构。

        匹配策略：取第一个解码后为有效 ``ForwardMsgData``
        （``sub_type == 1``）的 ``wexin_forward_msg_`` 条目。

        返回解析后的 ``ForwardMsgData`` dict，或 ``None``。
        """
        for elem in msg_body or []:
            if not isinstance(elem, dict) or elem.get("msg_type") != "TIMCustomElem":
                continue
            content = elem.get("msg_content", {}) or {}
            if not isinstance(content, dict):
                continue
            data_str = content.get("data", "")
            if not data_str:
                continue
            try:
                custom = json.loads(data_str)
            except (json.JSONDecodeError, TypeError):
                continue
            if not (isinstance(custom, dict) and custom.get("elem_type") == 1009):
                continue

            ext_map = content.get("ext_map") or {}
            if not isinstance(ext_map, dict) or not ext_map:
                return None

            def _parse_value(value):
                # ext_map 的值是 base64 编码的 ForwardMsgData protobuf。
                if not isinstance(value, str) or not value:
                    return None
                try:
                    pb = base64.b64decode(value)
                except (binascii.Error, ValueError):
                    return None
                data = decode_forward_msg_data(pb)
                if isinstance(data, dict) and data.get("sub_type") == 1:
                    return data
                return None

            # 取第一个有效的 wexin_forward_msg_ 条目。
            for key, value in ext_map.items():
                if not key.startswith("wexin_forward_msg_"):
                    continue
                parsed = _parse_value(value)
                if parsed is not None:
                    return parsed

        return None

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        ctx.raw_text = self._rewrite_slash_command(self._extract_text(ctx.msg_body))
        ctx.media_refs = self._extract_inbound_media_refs(ctx.msg_body)
        ctx.link_urls = self._extract_link_urls(ctx.msg_body)
        ctx.forwarded_records = self._extract_forwarded_records(ctx.msg_body, ctx.from_account)
        await next_fn()

class PlaceholderFilterMiddleware(InboundMiddleware):
    """跳过纯占位符消息（例如无媒体的 '[image]'）。"""

    name = "placeholder-filter"

    SKIPPABLE_PLACEHOLDERS: frozenset = frozenset({
        "[image]", "[图片]", "[file]", "[文件]",
        "[video]", "[视频]", "[voice]", "[语音]",
    })

    @classmethod
    def is_skippable_placeholder(cls, text: str, media_count: int = 0) -> bool:
        """检测消息是否为纯占位符（应被跳过）。"""
        if media_count > 0:
            return False
        stripped = text.strip()
        return stripped in cls.SKIPPABLE_PLACEHOLDERS

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        if self.is_skippable_placeholder(ctx.raw_text, len(ctx.media_refs)):
            logger.debug("[%s] Skipping placeholder message: %r", ctx.adapter.name, ctx.raw_text)
            return  # 停止管道
        await next_fn()


class OwnerCommandMiddleware(InboundMiddleware):
    """在群聊中检测 bot 拥有者的斜杠命令。

    识别群内已加入白名单的斜杠命令，并判定发送者身份。
    拥有者命令跳过 @Bot 检测；非拥有者的尝试会被拒绝。
    """

    name = "owner-command"

    # bot 拥有者在群内无需 @Bot 即可执行的白名单斜杠命令
    ALLOWLIST: frozenset = frozenset({
        "/new", "/reset", "/retry", "/undo", "/stop",
        "/approve", "/deny", "/background", "/bg",
        "/btw", "/queue", "/q",
    })

    @staticmethod
    def _rewrite_slash_command(text: str) -> str:
        """将全角斜杠归一化为 ASCII 斜杠并去除空白。"""
        text = text.strip()
        if text.startswith('\uff0f'):  # Full-width slash
            text = '/' + text[1:]
        return text

    @classmethod
    def _detect_owner_command(
        cls,
        *,
        push: dict,
        msg_body: list,
        chat_type: str,
        from_account: str,
    ) -> Tuple[Optional[str], Optional[str], bool]:
        """识别白名单斜杠命令并判定发送者身份。

        返回 (cmd, cmd_line, is_owner)：
          - (None, None, False)：不是白名单命令
          - (cmd, cmd_line, True)：拥有者匹配
          - (cmd, cmd_line, False)：白名单命令，但发送者不是拥有者
        """
        if chat_type != "group" or not cls.ALLOWLIST:
            return None, None, False

        # 提取 TIMTextElem：仅在恰好有一段文本时才做命令识别
        text_elems = [
            e for e in (msg_body or [])
            if e.get("msg_type") == "TIMTextElem"
        ]
        if len(text_elems) != 1:
            return None, None, False

        text = (text_elems[0].get("msg_content") or {}).get("text", "")
        cmd_line = cls._rewrite_slash_command(text)
        if not cmd_line.startswith("/"):
            return None, None, False
        cmd = cmd_line.split(maxsplit=1)[0].lower()
        if cmd not in cls.ALLOWLIST:
            return None, None, False

        # 发送者身份校验：bot 拥有者 <-> push.from_account == push.bot_owner_id。
        # 白名单命令（/approve、/deny、/stop、/reset、...）属于特权命令 ——
        # 一旦泄露给非拥有者，任何群成员都能批准危险的工具调用、
        # 终止拥有者的任务，或清空会话状态。
        owner_id = str((push or {}).get("bot_owner_id") or "").strip()
        is_owner = bool(owner_id) and owner_id == from_account
        return cmd, cmd_line, is_owner

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        adapter = ctx.adapter
        matched_cmd, cmd_line, is_owner = self._detect_owner_command(
            push=ctx.push,
            msg_body=ctx.msg_body,
            chat_type=ctx.chat_type,
            from_account=ctx.from_account,
        )
        if matched_cmd and not is_owner:
            # 非拥有者尝试了 owner 专属命令 —— 拒绝并停止
            logger.info(
                "[%s] Reject non-owner slash command: chat=%s from=%s cmd=%s",
                adapter.name, ctx.chat_id, ctx.from_account, matched_cmd,
            )
            adapter._track_task(asyncio.create_task(
                adapter.send(ctx.chat_id, f"⚠️ {matched_cmd} is only available to the creator in private chat mode"),
                name=f"yuanbao-owner-cmd-denial-{matched_cmd}",
            ))
            return  # Stop pipeline

        if matched_cmd and is_owner and cmd_line:
            logger.info(
                "[%s] Bot owner slash command: chat=%s from=%s cmd=%s",
                adapter.name, ctx.chat_id, ctx.from_account, matched_cmd,
            )
            ctx.owner_command = matched_cmd
            ctx.raw_text = cmd_line  # Override with clean command text
        await next_fn()


class BuildSourceMiddleware(InboundMiddleware):
    """从上下文字段构建 SessionSource。"""

    name = "build-source"

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        adapter = ctx.adapter
        ctx.source = adapter.build_source(
            chat_id=ctx.chat_id,
            chat_type=ctx.chat_type,
            chat_name=ctx.chat_name,
            user_id=ctx.from_account or None,
            user_name=ctx.sender_nickname or ctx.from_account,
            thread_id="main" if ctx.chat_type == "group" else None,
        )
        await next_fn()


class GroupAtGuardMiddleware(InboundMiddleware):
    """在群聊中观察非 @bot 消息；仅在 @Bot 时才回复。

    拥有者命令跳过 @Bot 检测（拥有者无需 @Bot）。
    """

    name = "group-at-guard"

    @staticmethod
    def _is_at_bot(msg_body: list, bot_id: Optional[str]) -> bool:
        """检测消息是否 @Bot。

        AT 元素格式：TIMCustomElem，msg_content.data 为 JSON 字符串：
            {"elem_type": 1002, "text": "@xxx", "user_id": "<botId>"}
        当 elem_type == 1002 且 user_id == bot_id 时视为 @Bot。
        """
        if not bot_id:
            return False
        for elem in msg_body:
            if elem.get("msg_type") != "TIMCustomElem":
                continue
            data_str = elem.get("msg_content", {}).get("data", "")
            if not data_str:
                continue
            try:
                custom = json.loads(data_str)
            except (json.JSONDecodeError, TypeError):
                continue
            if custom.get("elem_type") == 1002 and custom.get("user_id") == bot_id:
                return True
        return False

    @staticmethod
    def _extract_bot_mention_text(msg_body: list, bot_id: Optional[str]) -> str:
        """提取用于 @-提及本 bot 的展示文本（例如 ``@yuanbao-bot``）。"""
        if not bot_id:
            return ""
        for elem in msg_body:
            if elem.get("msg_type") != "TIMCustomElem":
                continue
            data_str = elem.get("msg_content", {}).get("data", "")
            if not data_str:
                continue
            try:
                custom = json.loads(data_str)
            except (json.JSONDecodeError, TypeError):
                continue
            if custom.get("elem_type") == 1002 and custom.get("user_id") == bot_id:
                mention_text = str(custom.get("text") or "").strip()
                if mention_text:
                    return mention_text
        return ""

    @staticmethod
    def _build_group_channel_prompt(msg_body: list, bot_id: Optional[str]) -> str:
        """构建一个按轮次的群聊提示词，强调应回复哪条消息。"""
        bid = str(bot_id or "unknown")
        bot_mention = GroupAtGuardMiddleware._extract_bot_mention_text(msg_body, bot_id) or "unknown"
        return (
            "You are handling a Yuanbao group chat message.\n"
            f"- Your identity: user_id={bid}, @-mention name in this group={bot_mention}\n"
            "- Lines in history prefixed with `[nickname|user_id]` are observed group context "
            "and are not necessarily addressed to you.\n"
            "- Treat only the current new message as a request explicitly directed at you, "
            "and answer it directly."
        )

    @classmethod
    def _observe_group_message(
        cls,
        adapter, source, sender_display: str, text: str,
        *,
        ctx: InboundContext,
        msg_id: Optional[str] = None,
        forwarded_records: Optional[dict] = None,
    ) -> None:
        """将一条群消息写入会话记录，但不触发 agent。

        这样当模型最终通过 @bot 被调用时，能看到完整的群对话。
        消息以 ``role: "user"`` 存储，格式为
        ``[nickname|user_id]\\n<content>``，便于模型区分参与者及其 user id。
        """
        store = getattr(adapter, "_session_store", None)
        if not store:
            return
        try:
            session_entry = store.get_or_create_session(source)
            user_id = source.user_id or "unknown"
            body_text = text
            if forwarded_records:
                summary = ForwardedRecordsParseMiddleware.build_forward_text(
                    forwarded_records, ctx=ctx, is_dispatch=False,
                )
                if summary:
                    body_text = f"{text}\n{summary}" if text else summary
            attributed = f"[{sender_display}|{user_id}]\n{body_text}"
            entry: dict = {
                "role": "user",
                "content": attributed,
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "observed": True,
            }
            if msg_id:
                entry["message_id"] = msg_id
            store.append_to_transcript(
                session_entry.session_id,
                entry,
            )
        except Exception as exc:
            logger.warning("[%s] Failed to observe group message: %s", adapter.name, exc)

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        adapter = ctx.adapter
        if ctx.chat_type == "group" and not ctx.owner_command and not self._is_at_bot(ctx.msg_body, adapter._bot_id):
            self._observe_group_message(
                adapter, ctx.source, ctx.sender_nickname or ctx.from_account, ctx.raw_text,
                msg_id=ctx.msg_id or None,
                forwarded_records=ctx.forwarded_records,
                ctx=ctx,
            )
            logger.info(
                "[%s] Group message observed (no @bot): chat=%s from=%s",
                adapter.name, ctx.chat_id, ctx.from_account,
            )
            return  # 停止管道 —— 消息已被观察但未派发
        await next_fn()


class GroupAttributionMiddleware(InboundMiddleware):
    """为群 @bot 消息打上 [nickname|user_id] 归因标签和 channel_prompt。

    对于通过 @bot 守卫的群消息（即 bot 被提及），该中间件：
      - 构建按轮次的 channel_prompt，让模型知道自身身份和归因方案。
      - 将 ctx.raw_text 改写为 ``[nickname|user_id]\\n<content>``，以匹配
        已观察历史的格式。
      - 通过清空 ``source.user_name`` 来抑制 runner 默认的
        ``[user_name]`` 共享线程前缀。
    """

    name = "group-attribution"

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        if ctx.chat_type == "group" and not ctx.owner_command:
            adapter = ctx.adapter
            ctx.channel_prompt = GroupAtGuardMiddleware._build_group_channel_prompt(
                ctx.msg_body, adapter._bot_id,
            )
            user_id_label = ctx.from_account or "unknown"
            nickname_label = ctx.sender_nickname or ctx.from_account or "unknown"
            ctx.raw_text = f"[{nickname_label}|{user_id_label}]\n{ctx.raw_text}"
            # 抑制 runner 默认的 ``[user_name]`` 共享线程前缀，使模型
            # 看到的文本与已观察历史的格式一致。
            if ctx.source is not None:
                ctx.source = dataclasses.replace(ctx.source, user_name=None)
        await next_fn()


class YuanbaoMessageType(Enum):
    """元宝本地的消息子类型；在离开 adapter 之前会被强制转换回
    :class:`MessageType`（见 :class:`DispatchMiddleware`）。"""

    # 微信转发聊天记录（TIMCustomElem，elem_type 1009）。
    CHAT_RECORD = "chat_record"


class ClassifyMessageTypeMiddleware(InboundMiddleware):
    """根据文本内容和 msg_body 元素判定 MessageType。"""

    name = "classify-msg-type"

    @staticmethod
    def _classify(text: str, msg_body: list):
        """根据文本和 msg_body 判定消息类型。

        返回一个基础 :class:`MessageType`，或针对平台特定子类型
        返回一个元宝本地的 :class:`YuanbaoMessageType`。
        """
        if text.startswith("/"):
            return MessageType.COMMAND
        for elem in msg_body:
            etype = elem.get("msg_type", "")
            if etype == "TIMImageElem":
                return MessageType.PHOTO
            if etype == "TIMSoundElem":
                return MessageType.VOICE
            if etype == "TIMVideoFileElem":
                return MessageType.VIDEO
            if etype == "TIMFileElem":
                return MessageType.DOCUMENT
            if etype == "TIMCustomElem":
                data_str = (elem.get("msg_content") or {}).get("data", "")
                try:
                    custom = json.loads(data_str)
                except (json.JSONDecodeError, TypeError):
                    custom = None
                if isinstance(custom, dict) and custom.get("elem_type") == 1009:
                    return YuanbaoMessageType.CHAT_RECORD
        return MessageType.TEXT

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        ctx.msg_type = self._classify(ctx.raw_text, ctx.msg_body)
        await next_fn()


class QuoteContextMiddleware(InboundMiddleware):
    """从 cloud_custom_data 中提取引用/回复上下文。"""

    name = "quote-context"

    def _extract_quote_context(self, cloud_custom_data: str) -> Tuple[Optional[str], Optional[str]]:
        """提取引用文本上下文，映射到 MessageEvent.reply_to_*。
        """
        if not cloud_custom_data:
            return None, None
        try:
            parsed = json.loads(cloud_custom_data)
        except (json.JSONDecodeError, TypeError):
            return None, None

        quote = parsed.get("quote") if isinstance(parsed, dict) else None
        if not isinstance(quote, dict):
            return None, None

        quote_id = str(quote.get("id") or "").strip() or None
        desc = str(quote.get("desc") or "").strip()
        sender = str(quote.get("sender_nickname") or quote.get("sender_id") or "").strip()
        quote_text = (f"{sender}: {desc}" if sender else desc) if desc else None

        return quote_id, quote_text

    async def _extract_media_refs_from_transcript(
        self, ctx: InboundContext
    ) -> List[Tuple[str, str, str]]:
        """在会话历史中查找被引用的消息，并返回其内容中找到的
        ``[kind|ybres:RID]`` 锚点，形式为 ``(rid, kind, filename)`` 元组。

        当 ``ctx.reply_to_message_id`` 未设置、会话存储/source 不可用，
        或被引用消息不含可解析的媒体锚点时，返回 ``[]``。
        """
        if ctx.reply_to_message_id is None:
            return []
        adapter = ctx.adapter
        media_refs: List[Tuple[str, str, str]] = []
        try:
            store = getattr(adapter, "_session_store", None)
            if not store or ctx.source is None:
                return []
            session_entry = store.get_or_create_session(ctx.source)
            history = store.load_transcript(session_entry.session_id)
            for msg in reversed(history or []):
                mid = msg.get("message_id", "")
                if not mid or mid != ctx.reply_to_message_id:
                    continue
                _content = msg.get("content", "")
                if isinstance(_content, str) and "|ybres:" in _content:
                    for m in _YB_RES_REF_RE.finditer(_content):
                        head = m.group(1)
                        rid = m.group(2)
                        kind, _, filename = head.partition(":")
                        kind = kind.strip()
                        if kind in _RESOLVABLE_MEDIA_KINDS:
                            media_refs.append((rid, kind, filename.strip()))
                break
        except Exception as exc:
            logger.warning(
                "[%s] quote transcript lookup failed: %s",
                getattr(adapter, "name", "yuanbao"), exc,
            )
        return media_refs

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        ctx.reply_to_message_id, ctx.reply_to_text = self._extract_quote_context(ctx.cloud_custom_data)
        ctx.quote_media_refs = await self._extract_media_refs_from_transcript(ctx)
        await next_fn()


class ForwardedRecordsParseMiddleware(InboundMiddleware):
    """为派发而深度解析微信转发聊天记录（elem_type 1009）。

    当当前轮次可获得完整的 ``ForwardMsgData`` dict（由当前消息通过
    ``ctx.forwarded_records`` 携带）时激活。将媒体解析为
    ``[kind|ybres:RID]`` 占位符，把可下载的 ref 追加到
    ``ctx.media_refs``（供 :class:`MediaResolveMiddleware` 使用），
    并改写 ``ctx.raw_text``。

    群 @bot 轮次若*未*在当前消息上携带转发，则依赖
    :class:`GroupAtGuardMiddleware` 在观察时写入会话记录的预渲染摘要 ——
    此处没有运行时摘要兜底。

    任何失败情况下，该中间件都保持 ``ctx.raw_text`` 不变
    （优雅降级，design §2.8）。
    """

    name = "forwarded-records-parse"

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        try:
            if ctx.forwarded_records:
                self._send_loading_heartbeat(ctx)
                ctx.raw_text = self.build_forward_text(ctx.forwarded_records, ctx=ctx, is_dispatch=True)
        except Exception as exc:
            # 优雅降级：保持 ctx.raw_text 原样。
            logger.warning(
                "[%s] forwarded-records deep parse failed: %s",
                getattr(ctx.adapter, "name", "yuanbao"), exc,
            )

        await next_fn()

    # -- 心跳 ---------------------------------------------------------

    @staticmethod
    async def _send_loading_heartbeat(ctx: InboundContext) -> None:
        """尽力发送一次 RUNNING 心跳，让用户看到加载气泡。"""
        try:
            await ctx.adapter._outbound.heartbeat.send_heartbeat_once(
                ctx.chat_id, WS_HEARTBEAT_RUNNING,
            )
        except Exception:
            pass

    # -- 记录渲染辅助方法 -----------------------------------------

    @classmethod
    def _media_marker(
        cls, media: dict, plain_text: str = "",
    ) -> Tuple[str, Optional[Dict[str, str]]]:
        """将一个 ``msgContent.multimedia`` 条目渲染为文本标记。

        返回 ``(marker, ref)``。当存在可用的 RID/URL 时，可下载媒体会产生
        ``[kind|ybres:RID]`` 标记和一个 ``ctx.media_refs`` ref dict；
        否则产生纯 ``[kind] name`` 标记且 ``ref=None``。
        """
        media_type = (media.get("type", "") or media.get("doc_type", "")).strip().lower()
        url = str(media.get("url") or "").strip()
        media_id = str(media.get("media_id") or "").strip()
        file_name = str(media.get("file_name") or "").strip()
        # media_id 可直接作为 ybres RID 使用（design §2.10.9）；
        # 否则回退到从 URL 中解析 resourceId。
        rid = media_id or ExtractContentMiddleware._parse_resource_id(url)

        if media_type == "image":
            if url and rid:
                return f"[image|ybres:{rid}] {file_name}".rstrip(), {"kind": "image", "url": url}
            return f"[image] {file_name or plain_text}".rstrip(), None

        if media_type in ("file", "document", "code"):
            if url and rid:
                ref: Dict[str, str] = {"kind": "file", "url": url}
                if file_name:
                    ref["name"] = file_name
                return f"[file|ybres:{rid}] {file_name}".rstrip(), ref
            return f"[file] {file_name}".rstrip(), None

        if media_type == "url":
            # 链接分享（如微信文章）—— 保留 URL 给 agent。
            link_title = file_name or str(media.get("title") or "")
            return f"[link] {link_title} {url}".rstrip(), None

        if media_type == "video":
            if url and rid:
                return f"[video|ybres:{rid}] {file_name}".rstrip(), {"kind": "video", "url": url}
            return f"[video] {file_name or url}".rstrip(), None

        return f"[{media_type or 'media'}] {url or file_name}".rstrip(), None

    # 单条记录合并文本的上限；记录数量不做上限（design §2.10.3）。
    FORWARD_MSG_TEXT_MAX_CHARS = 1000

    @classmethod
    def _walk_forward_msgs(
        cls,
        forward_data: dict,
    ) -> Iterator[Tuple[str, str, List[Dict[str, str]]]]:
        """遍历 ``ForwardMsgData['msg']``，产出 ``(sender, body, refs)``。

        对每条记录基于 ``msgContent`` 进行派发（文本 / 多媒体 / 嵌套转发 /
        兜底）；``body`` 以 :attr:`FORWARD_MSG_TEXT_MAX_CHARS` 为上限。
        媒体经过 :meth:`_media_marker` 处理，始终构建完整的
        ``[kind|ybres:RID]`` 标记；``refs`` 按文本顺序保存该记录的可下载
        ``ctx.media_refs`` 条目 —— 这一顺序是 PatchAnchorsMiddleware 所依赖的
        （design §2.10.6）。头部/尾部由调用方负责。
        """
        for msg in (forward_data.get("msg") if isinstance(forward_data, dict) else None) or []:
            if not isinstance(msg, dict):
                continue
            sender = msg.get("sender", "")
            plain_text = msg.get("plainText", "")
            msg_contents = msg.get("msgContent", []) or []

            refs: List[Dict[str, str]] = []
            if not msg_contents:
                rendered = plain_text
            else:
                parts: List[str] = []
                for mc in msg_contents:
                    if not isinstance(mc, dict):
                        continue
                    mc_type = mc.get("type", 0)  # EnumMsgContentType
                    if mc_type == 1:  # TEXT
                        parts.append(mc.get("text", ""))
                    elif mc_type == 2:  # MULTIMEDIA
                        for media in mc.get("multimedia", []) or []:
                            if isinstance(media, dict):
                                marker, ref = cls._media_marker(
                                    media, plain_text,
                                )
                                parts.append(marker)
                                if ref is not None:
                                    refs.append(ref)
                    elif mc_type == 3:  # nested FORWARD_MSG (design §2.10.10)
                        parts.append("[嵌套聊天记录]")
                    else:
                        if plain_text:
                            parts.append(plain_text)
                rendered = "  ".join(p for p in parts if p) or plain_text

            if len(rendered) > cls.FORWARD_MSG_TEXT_MAX_CHARS:
                rendered = rendered[: cls.FORWARD_MSG_TEXT_MAX_CHARS] + "…(已截断)"
            yield sender, rendered, refs

    # -- 提示词构建器 ---------------------------------------------------

    @classmethod
    def build_forward_text(
        cls, forward_data: dict, *, ctx: InboundContext, is_dispatch: bool,
    ) -> str:
        """将 ``ForwardMsgData`` 渲染为转发文本。

        正文行为 ``发送人：正文``，并保留完整的 ``[kind|ybres:RID]`` 媒体
        标记。当 ``is_dispatch`` 为 true 时，refs 会追加到
        ``ctx.media_refs`` 以供下游解析，并添加 ``用户附言：{ctx.raw_text}``
        尾部；观察型调用方两者都跳过，因为后续没有中间件再运行。
        """
        nickname = ctx.sender_nickname or "用户"
        lines = [f"当前用户的昵称为{nickname}", "以下为用户的聊天记录"]
        for sender, body, refs in cls._walk_forward_msgs(forward_data):
            lines.append(f"{sender}：{body}")
            if is_dispatch:
                ctx.media_refs.extend(refs)
        text = "\n".join(lines)
        if is_dispatch and ctx.raw_text.strip():
            text += f"\n\n用户附言：{ctx.raw_text.strip()}"
        return text


class MediaResolveMiddleware(InboundMiddleware):
    """将入站媒体引用解析为可下载的 URL。"""

    name = "media-resolve"

    # --- 资源下载缓存（以 resourceId 为 key） ---
    # 避免在 TTL 窗口内重复下载同一资源。
    _resource_cache: ClassVar[Dict[str, Tuple[str, str, float]]] = {}  # rid -> (local_path, mime, ts)
    _RESOURCE_CACHE_TTL_S: ClassVar[int] = 24 * 60 * 60  # 24 hours
    _RESOURCE_CACHE_MAX_SIZE: ClassVar[int] = 256

    @classmethod
    def _get_cached_resource(cls, resource_id: str) -> Optional[Tuple[str, str]]:
        """若缓存仍有效且文件存在，返回缓存的 ``(local_path, mime)``，否则返回 None。"""
        if not resource_id:
            return None
        entry = cls._resource_cache.get(resource_id)
        if entry is None:
            return None
        local_path, mime, ts = entry
        if time.time() - ts > cls._RESOURCE_CACHE_TTL_S:
            cls._resource_cache.pop(resource_id, None)
            return None
        # 校验缓存文件在磁盘上仍然存在（缓存目录可能被清理）。
        if not os.path.isfile(local_path):
            cls._resource_cache.pop(resource_id, None)
            return None
        return local_path, mime

    @classmethod
    def _put_cached_resource(cls, resource_id: str, local_path: str, mime: str) -> None:
        """将下载结果存入缓存。超容量时淘汰最旧的条目。"""
        if not resource_id:
            return
        if len(cls._resource_cache) >= cls._RESOURCE_CACHE_MAX_SIZE:
            # 按时间戳丢弃最旧的 25% 条目。
            sorted_keys = sorted(cls._resource_cache, key=lambda k: cls._resource_cache[k][2])
            for k in sorted_keys[: cls._RESOURCE_CACHE_MAX_SIZE // 4]:
                cls._resource_cache.pop(k, None)
        cls._resource_cache[resource_id] = (local_path, mime, time.time())

    @staticmethod
    def _guess_image_ext_from_url(url: str) -> str:
        """根据 URL 路径猜测图片扩展名。"""
        path = urllib.parse.urlparse(url).path
        ext = os.path.splitext(path)[1].lower()
        if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".tiff"}:
            return ext
        return ".jpg"

    @staticmethod
    async def _fetch_resource_url(adapter, resource_id: str) -> str:
        """底层辅助方法：用 ``resourceId`` 换取直接下载 URL。

        处理 token 获取、``/api/resource/v1/download`` API 调用，
        以及一次带 token 强制刷新的 401 重试。失败时抛出异常。
        """
        resource_id = resource_id.strip()
        if not resource_id:
            raise RuntimeError("missing resource_id")

        token_data = await adapter._get_cached_token()
        token = str(token_data.get("token") or "").strip()
        source = str(token_data.get("source") or "web").strip() or "web"
        bot_id = str(token_data.get("bot_id") or adapter._bot_id or adapter._app_key).strip()
        if not token or not bot_id:
            raise RuntimeError("missing token or bot_id for resource download")

        api_url = f"{adapter._api_domain}/api/resource/v1/download"
        headers = {
            "Content-Type": "application/json",
            "X-ID": bot_id,
            "X-Token": token,
            "X-Source": source,
        }

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            for attempt in range(2):
                resp = await client.get(api_url, params={"resourceId": resource_id}, headers=headers)
                if resp.status_code == 401 and attempt == 0:
                    # 过期时强制刷新 token 一次并重试
                    token_data = await SignManager.force_refresh(
                        adapter._app_key, adapter._app_secret, adapter._api_domain,
                    )
                    token = str(token_data.get("token") or "").strip()
                    source = str(token_data.get("source") or source or "web").strip() or "web"
                    bot_id = str(token_data.get("bot_id") or adapter._bot_id or adapter._app_key).strip()
                    if not token or not bot_id:
                        break
                    headers["X-ID"] = bot_id
                    headers["X-Token"] = token
                    headers["X-Source"] = source
                    continue

                resp.raise_for_status()
                payload = resp.json()
                code = payload.get("code")
                if code not in {None, 0}:
                    raise RuntimeError(
                        f"resource/v1/download failed: code={code}, msg={payload.get('msg', '')}"
                    )
                data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                real_url = str((data or {}).get("url") or (data or {}).get("realUrl") or "").strip()
                if real_url:
                    return real_url
                raise RuntimeError("resource/v1/download missing url/realUrl")

        raise RuntimeError("resource/v1/download did not return a URL")

    @staticmethod
    async def _resolve_download_url(adapter, url: str) -> str:
        """将元宝资源占位 URL 解析为可直接获取的真实 URL。

        常见 URL 模式：
          https://hunyuan.tencent.com/api/resource/download?resourceId=...
        直接 GET 会返回 401；需要走业务 API：
          GET /api/resource/v1/download?resourceId=...
        """
        try:
            parsed = urllib.parse.urlparse(url)
        except Exception:
            return url

        query = urllib.parse.parse_qs(parsed.query)
        resource_ids = query.get("resourceId") or query.get("resourceid") or []
        resource_id = str(resource_ids[0]).strip() if resource_ids else ""
        if not resource_id:
            return url

        try:
            return await MediaResolveMiddleware._fetch_resource_url(adapter, resource_id)
        except Exception:
            return url

    @classmethod
    async def _download_and_cache(
        cls, adapter, *, fetch_url: str, kind: str,
        file_name: Optional[str] = None, log_tag: str = "",
        resource_id: str = "",
    ) -> Optional[Tuple[str, str]]:
        """下载元宝资源并缓存到本地。返回 ``(local_path, mime)`` 或 ``None``。

        当提供 *resource_id* 时，会先查询以 resourceId 为 key 的内存缓存，
        以便在 TTL 窗口内跳过对同一资源的重复下载。
        """
        if resource_id:
            hit = cls._get_cached_resource(resource_id)
            if hit is not None:
                logger.debug(
                    "[%s] resource cache hit: rid=%s path=%s",
                    adapter.name, resource_id, hit[0],
                )
                return hit

        try:
            file_bytes, content_type = await media_download_url(
                fetch_url, max_size_mb=adapter.MEDIA_MAX_SIZE_MB,
            )
        except Exception as exc:
            logger.warning(
                "[%s] inbound media download failed: kind=%s %s err=%s",
                adapter.name, kind, log_tag, exc,
            )
            return None

        if kind == "image":
            ext = cls._guess_image_ext_from_url(fetch_url)
            try:
                local_path = cache_image_from_bytes(file_bytes, ext=ext)
            except ValueError as exc:
                logger.warning(
                    "[%s] inbound image cache rejected: %s err=%s",
                    adapter.name, log_tag, exc,
                )
                return None
            mime = guess_mime_type(f"image{ext}")
            if not mime.startswith("image/"):
                mime = content_type if content_type.startswith("image/") else "image/jpeg"
            cls._put_cached_resource(resource_id, local_path, mime)
            return local_path, mime

        if kind == "video":
            # 元宝视频资源没有可靠的扩展名；默认使用 mp4。
            local_path = cache_video_from_bytes(file_bytes)
            mime = guess_mime_type(local_path) or (
                content_type if content_type.startswith("video/") else "video/mp4"
            )
            cls._put_cached_resource(resource_id, local_path, mime)
            return local_path, mime

        # kind == "file"
        if not file_name:
            parsed = urllib.parse.urlparse(fetch_url)
            file_name = os.path.basename(parsed.path) or "file"
        try:
            local_path = cache_document_from_bytes(file_bytes, file_name)
        except Exception as exc:
            logger.warning(
                "[%s] inbound file cache failed: %s err=%s",
                adapter.name, log_tag, exc,
            )
            return None
        mime = guess_mime_type(file_name) or content_type or "application/octet-stream"
        cls._put_cached_resource(resource_id, local_path, mime)
        return local_path, mime

    @classmethod
    async def _resolve_media_urls(
        cls, adapter, media_refs: List[Dict[str, str]]
    ) -> Tuple[List[str], List[str]]:
        """解析入站媒体引用：下载到本地缓存，返回 (local_paths, mime_types)。

        元宝 COS 主机名会解析到内网 IP，从而触发 vision_tools 中的 SSRF
        守卫。我们自行下载并返回本地缓存路径。
        """
        media_urls: List[str] = []
        media_types: List[str] = []

        for ref in media_refs:
            kind = str(ref.get("kind") or "").strip().lower()
            url = str(ref.get("url") or "").strip()
            filename = str(ref.get("name") or "").strip()
            if kind not in _RESOLVABLE_MEDIA_KINDS or not url:
                continue

            # 从占位 URL 中提取 resourceId 以便缓存去重。
            rid = ExtractContentMiddleware._parse_resource_id(url)

            try:
                fetch_url = await cls._resolve_download_url(adapter, url)
            except Exception as exc:
                logger.warning(
                    "[%s] inbound media resolve failed: kind=%s url=%s err=%s",
                    adapter.name, kind, url, exc,
                )
                continue

            cached = await cls._download_and_cache(
                adapter,
                fetch_url=fetch_url,
                kind=kind,
                file_name=filename or None,
                log_tag=f"placeholder_url={url[:80]}",
                resource_id=rid,
            )
            if cached is None:
                continue
            local_path, mime = cached
            media_urls.append(local_path)
            media_types.append(mime)

        return media_urls, media_types

    @classmethod
    async def _resolve_ybres_refs(
        cls,
        adapter,
        refs: List[Tuple[str, str, str]],
        *,
        log_prefix: str,
    ) -> Tuple[List[str], List[str]]:
        """将一组 ``(rid, kind, filename)`` ybres 元组解析为本地路径。
        """
        media_paths: List[str] = []
        mimes: List[str] = []
        for rid, kind, filename in refs:
            if kind not in _RESOLVABLE_MEDIA_KINDS:
                continue
            try:
                fresh_url = await cls._fetch_resource_url(adapter, rid)
            except Exception as exc:
                logger.warning(
                    "[%s] %s resolve failed: rid=%s kind=%s err=%s",
                    adapter.name, log_prefix, rid, kind, exc,
                )
                continue
            cached = await cls._download_and_cache(
                adapter,
                fetch_url=fresh_url,
                kind=kind,
                file_name=filename or None,
                log_tag=f"{log_prefix} rid={rid}",
                resource_id=rid,
            )
            if cached is None:
                continue
            path, mime = cached
            media_paths.append(path)
            mimes.append(mime)
        return media_paths, mimes

    @classmethod
    async def _collect_observed_media(
        cls, adapter, source,
    ) -> Tuple[List[str], List[str]]:
        """将最近观察到的图片/文件锚点从会话记录解析为 ``(local_paths, mimes)``。"""
        store = getattr(adapter, "_session_store", None)
        if not store:
            return [], []
        try:
            session_entry = store.get_or_create_session(source)
            history = store.load_transcript(session_entry.session_id)
        except Exception as exc:
            logger.warning(
                "[%s] Observed-media hydration setup failed: %s",
                adapter.name, exc,
            )
            return [], []
        if not history:
            return [], []

        # 从最近 LOOKBACK 条消息按 新→旧 顺序遍历，这样当触及每轮解析上限时，
        # 保留的是*最新*的媒体引用，而非窗口中最旧的。在单条消息内也反向
        # 遍历匹配项，使平局时最后加入的图片胜出。最终的 ``order`` 在
        # 交给 ``_resolve_ybres_refs`` 之前会再次反转为时间顺序（旧→新），
        # 以便下游提示词插入保持自然的阅读顺序。
        window = history[-OBSERVED_MEDIA_BACKFILL_LOOKBACK:]
        order: List[Tuple[str, str, str]] = []  # (rid, kind, filename)
        seen: set = set()
        for msg in reversed(window):
            content = msg.get("content")
            if not isinstance(content, str) or "|ybres:" not in content:
                continue
            matches = list(_YB_RES_REF_RE.finditer(content))
            for m in reversed(matches):
                head = m.group(1)  # "image" | "file:<name>" | "voice" | "video"
                rid = m.group(2)
                kind, _, filename = head.partition(":")
                kind = kind.strip()
                if kind not in _RESOLVABLE_MEDIA_KINDS:
                    continue
                if rid in seen:
                    continue
                seen.add(rid)
                order.append((rid, kind, filename.strip()))
                if len(order) >= OBSERVED_MEDIA_BACKFILL_MAX_RESOLVE_PER_TURN:
                    break
            if len(order) >= OBSERVED_MEDIA_BACKFILL_MAX_RESOLVE_PER_TURN:
                break

        # 恢复时间顺序（旧→新）以供下游解析。
        order.reverse()

        if not order:
            return [], []

        return await cls._resolve_ybres_refs(
            adapter, order, log_prefix="observed-media",
        )

    @classmethod
    async def _resolve_quote_media(
        cls, adapter, quote_media_refs: List[Tuple[str, str, str]],
    ) -> Tuple[List[str], List[str]]:
        """解析被引用消息所携带的媒体锚点。

        ``quote_media_refs`` 是一组 ``(rid, kind, filename)`` 元组，由
        :class:`QuoteContextMiddleware` 从会话记录中产出。
        """
        return await cls._resolve_ybres_refs(
            adapter, quote_media_refs, log_prefix="quote",
        )

    @staticmethod
    def _collect_quote_local_media(ctx: InboundContext) -> Tuple[List[str], List[str]]:
        """私聊兜底：恢复已是本地的被引用媒体。

        此处只处理已经是本地的媒体：到某一轮被缓存时，
        ``PatchAnchorsMiddleware`` 已把解析过的 ``|ybres:`` 锚点改写为
        ``[image: /path]`` / ``[file: name → /path]``。未解析的锚点属于
        原始轮次的解析失败，归该轮次处理，而非此引用兜底 —— 因此此处
        不会重新下载。

        返回 ``(local_paths, mimes)``，即在其原始轮次已下载到本地缓存的
        媒体，可直接原样注入。
        """
        paths: List[str] = []
        mimes: List[str] = []
        rid_key = ctx.reply_to_message_id
        if not rid_key:
            return paths, mimes
        cache = getattr(ctx.adapter, "_msg_content_cache", None)
        if not cache:
            return paths, mimes
        text = cache.get(rid_key)
        if not isinstance(text, str) or not text:
            return paths, mimes

        # PatchAnchorsMiddleware 写入的已是本地的媒体路径。
        seen: set = set()
        for m in _YB_LOCAL_MEDIA_RE.finditer(text):
            kind = (m.group(1) or "").strip().lower()
            path = (m.group(2) or "").strip()
            if not path or path in seen:
                continue
            if not os.path.exists(path):
                continue
            seen.add(path)
            mime = guess_mime_type(os.path.basename(path)) or (
                "image/jpeg" if kind == "image" else "application/octet-stream"
            )
            paths.append(path)
            mimes.append(mime)

        return paths, mimes

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        # NOTE：在群聊中到达此中间件，意味着消息已 @-提及 bot（或是 owner 命令）。
        # GroupAtGuardMiddleware 会在管道更早处短路非 @bot 的群消息，因此此处
        # 在下载媒体前无需再次检查 @bot 状态。
        adapter = ctx.adapter

        urls: List[str] = []
        types: List[str] = []
        seen: set = set()

        def _add_unique_pairs(pair_lists: Tuple[List[str], List[str]]) -> None:
            u_list, m_list = pair_lists
            for u, m in zip(u_list, m_list):
                if not u or u in seen:
                    continue
                seen.add(u)
                urls.append(u)
                types.append(m)

        # 1) 当前消息自身携带的媒体。
        own_pairs = await self._resolve_media_urls(adapter, ctx.media_refs)
        own_count = sum(1 for u in own_pairs[0] if u)
        _add_unique_pairs(own_pairs)

        # 2) 第二来源 —— 被引用媒体优先；否则仅在群聊中回退到
        #    observed-media 回填（DM 已在发送当轮解析过媒体）。
        if ctx.reply_to_message_id is not None:
            if ctx.quote_media_refs:
                _add_unique_pairs(await self._resolve_quote_media(adapter, ctx.quote_media_refs))
            else:
                # DM 引用兜底：会话记录中没有 message_id 匹配（DM 用户行
                # 不携带 platform message_id），因此从 adapter msg 缓存中
                # 恢复已是本地的媒体。在其原始轮次已处理 —— 无需重新下载，
                # 直接原样注入。
                _add_unique_pairs(self._collect_quote_local_media(ctx))
        elif ctx.chat_type == "group":
            # 群聊：只有 @-bot 的轮次才会到达此中间件
            # （见 handle() 顶部 GroupAtGuardMiddleware 的说明），
            # 因此此处无条件进行 observed-media 注入是安全的。
            try:
                _add_unique_pairs(await self._collect_observed_media(adapter, ctx.source))
            except Exception as exc:
                logger.warning(
                    "[%s] observed-image hydration raised, continuing anyway: %s",
                    adapter.name, exc,
                )

        ctx.media_urls = urls
        ctx.media_types = types

        # 媒体解析后再次检查占位符。
        # 使用 ``own_count``（而非 ``len(urls)``）以保留原始语义：
        # 仅伴随引用/观察媒体（即没有自身的新附件）的占位符文本仍可跳过。
        if PlaceholderFilterMiddleware.is_skippable_placeholder(ctx.raw_text, own_count):
            logger.debug("[%s] Skip placeholder after media download: %r", adapter.name, ctx.raw_text)
            return  # 停止管道
        await next_fn()


class PatchAnchorsMiddleware(InboundMiddleware):
    """将 ``ctx.raw_text`` 中的 ``[kind|ybres:RID]`` 锚点替换为本地路径。

    在 :class:`MediaResolveMiddleware` 之后运行，以便
    ``ctx.media_urls`` / ``ctx.media_types`` 已填充下载好的资源
    （自身媒体 + 引用媒体或群观察媒体）。这样下游写入的会话记录中，
    记录的就是模型可用的本地路径，而非不透明的 ``ybres:`` 引用。

    仅替换已解析的媒体（以 ``/`` 开头的路径）；任何没有对应本地资源的
    锚点保持不变。
    """

    name = "patch-anchors"

    @staticmethod
    def _patch(text: str, urls: List[str], types: List[str]) -> str:
        if not text or not urls:
            return text
        patched = text
        for u, m in zip(urls, types):
            if not u.startswith("/"):
                continue
            anchor_match = _YB_RES_REF_RE.search(patched)
            if not anchor_match:
                break
            head = anchor_match.group(1)
            kind, _, filename = head.partition(":")
            kind = kind.strip()
            if kind == "image" and m.startswith("image/"):
                replacement = f"[image: {u}]"
            elif kind == "file":
                label = filename.strip() or os.path.basename(u)
                replacement = f"[file: {label} → {u}]"
            elif kind == "video":
                replacement = f"[video: {u}]"
            else:
                continue
            patched = (
                patched[: anchor_match.start()]
                + replacement
                + patched[anchor_match.end():]
            )
        return patched

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        ctx.raw_text = self._patch(ctx.raw_text, ctx.media_urls, ctx.media_types)
        await next_fn()


class DispatchMiddleware(InboundMiddleware):
    """构建 MessageEvent 并派发给 AI handler。"""

    name = "dispatch"

    async def handle(self, ctx: InboundContext, next_fn) -> None:
        adapter = ctx.adapter

        _sk = build_session_key(
            ctx.source,
            group_sessions_per_user=adapter.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=adapter.config.extra.get("thread_sessions_per_user", False),
        )

        async def _dispatch_inbound_event() -> None:
            event = MessageEvent(
                text=ctx.raw_text,
                message_type=(
                    MessageType.DOCUMENT
                    if any(mt.startswith(("application/", "text/")) for mt in ctx.media_types)
                    # 将元宝本地子类型（如 CHAT_RECORD）强制转换回基础
                    # MessageType：聊天记录已被深度解析为文本提示词，
                    # 因此 TEXT 才是下游路由正确的类型。
                    else ctx.msg_type if isinstance(ctx.msg_type, MessageType)
                    else MessageType.TEXT
                ),
                source=ctx.source,
                message_id=ctx.msg_id or None,
                raw_message=ctx.push,
                media_urls=list(ctx.media_urls),
                media_types=list(ctx.media_types),
                reply_to_message_id=ctx.reply_to_message_id,
                reply_to_text=ctx.reply_to_text,
                channel_prompt=ctx.channel_prompt,
            )
            if _sk and ctx.msg_id:
                adapter._processing_msg_ids[_sk] = ctx.msg_id
                adapter._processing_msg_texts[_sk] = ctx.raw_text or ""
            if ctx.msg_id and ctx.raw_text:
                cache = adapter._msg_content_cache
                cache[ctx.msg_id] = ctx.raw_text
                if len(cache) > 200:
                    for k in list(cache)[:len(cache) - 200]:
                        del cache[k]
            await adapter.handle_message(event)

        if ctx.chat_type == "group":
            is_new = _sk not in adapter._group_queues
            queue = adapter._group_queues.setdefault(_sk, asyncio.Queue())
            queue.put_nowait(_dispatch_inbound_event)
            logger.info(
                "[%s] Group message enqueued (qsize=%d) for %s",
                adapter.name, queue.qsize(), (_sk or "")[:50],
            )
            if is_new:
                consumer = asyncio.create_task(
                    self._consume_group_queue(adapter, _sk),
                    name=f"yuanbao-group-consumer-{(_sk or '')[:30]}",
                )
                adapter._inbound_tasks.add(consumer)
                consumer.add_done_callback(adapter._inbound_tasks.discard)
        else:
            task = asyncio.create_task(
                _dispatch_inbound_event(),
                name=f"yuanbao-inbound-{ctx.msg_id or 'unknown'}",
            )
            adapter._inbound_tasks.add(task)
            task.add_done_callback(adapter._inbound_tasks.discard)

        await next_fn()

    @staticmethod
    async def _consume_group_queue(adapter: "YuanbaoAdapter", session_key: str) -> None:
        """逐个排空群队列，每次派发并等待其完成后才继续下一个。"""
        _IDLE_TIMEOUT = 2.0
        queue = adapter._group_queues.get(session_key)
        if not queue:
            return
        try:
            while True:
                try:
                    dispatch_fn = await asyncio.wait_for(queue.get(), timeout=_IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    break
                logger.debug(
                    "[%s] Group queue: dispatching for %s (remaining=%d)",
                    adapter.name, (session_key or "")[:50], queue.qsize(),
                )
                try:
                    await dispatch_fn()
                    while session_key in adapter._active_sessions:
                        await asyncio.sleep(0.1)
                except Exception:
                    logger.exception("[%s] Group queue consumer error", adapter.name)
        finally:
            adapter._group_queues.pop(session_key, None)


class InboundPipelineBuilder:
    """构建 InboundPipeline 实例的工厂。

    将管道装配（业务知识）与管道引擎（InboundPipeline）分离，
    使引擎保持通用且可复用。
    """

    # 元宝入站消息处理的默认中间件顺序。
    _DEFAULT_MIDDLEWARES: list[type] = [
        DecodeMiddleware,
        ExtractFieldsMiddleware,
        RecallGuardMiddleware,
        DedupMiddleware,
        SkipSelfMiddleware,
        ChatRoutingMiddleware,
        AccessGuardMiddleware,
        AutoSetHomeMiddleware,
        ExtractContentMiddleware,
        PlaceholderFilterMiddleware,
        OwnerCommandMiddleware,
        BuildSourceMiddleware,
        GroupAtGuardMiddleware,
        GroupAttributionMiddleware,
        ClassifyMessageTypeMiddleware,
        QuoteContextMiddleware,
        ForwardedRecordsParseMiddleware,
        MediaResolveMiddleware,
        PatchAnchorsMiddleware,
        DispatchMiddleware,
    ]

    @classmethod
    def build(cls) -> InboundPipeline:
        """构建默认的入站消息处理管道。"""
        pipeline = InboundPipeline()
        for mw_cls in cls._DEFAULT_MIDDLEWARES:
            pipeline.use(mw_cls())
        return pipeline

class ConnectionManager:
    """管理 YuanbaoAdapter 的 WebSocket 连接生命周期。

    职责：
      - 打开和关闭 WebSocket
      - AUTH_BIND 握手
      - 心跳（ping/pong）循环
      - 接收循环（帧分发）
      - 带指数退避的重连
    """

    def __init__(self, adapter: "YuanbaoAdapter") -> None:
        self._adapter = adapter
        self._ws = None  # websockets 连接
        self._connect_id: Optional[str] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._pending_acks: Dict[str, asyncio.Future] = {}
        self._pending_pong: Optional[asyncio.Future] = None
        self._consecutive_hb_timeouts: int = 0
        self._reconnect_attempts: int = 0
        self._reconnecting: bool = False
        # 防抖缓冲区，用于聚合多部分的入站消息
        self._inbound_buffer: Dict[str, list] = {}  # key -> [raw_data_frames, ...]
        self._inbound_timers: Dict[str, asyncio.TimerHandle] = {}  # key -> timer

    # -- 属性 --------------------------------------------------------

    @property
    def ws(self):
        return self._ws

    @property
    def connect_id(self) -> Optional[str]:
        return self._connect_id

    @property
    def reconnect_attempts(self) -> int:
        return self._reconnect_attempts

    @property
    def is_connected(self) -> bool:
        if self._ws is None:
            return False
        open_attr = getattr(self._ws, "open", None)
        if open_attr is True:
            return True
        if callable(open_attr):
            try:
                return bool(open_attr())
            except Exception:
                return False
        return False

    # -- 打开 / 关闭 ------------------------------------------------------

    async def open(self) -> bool:
        """打开 WebSocket 连接：sign-token → WS 连接 → AUTH_BIND → 启动循环。

        成功返回 True，失败返回 False。
        """
        adapter = self._adapter

        if not WEBSOCKETS_AVAILABLE:
            msg = "Yuanbao startup failed: 'websockets' package not installed"
            adapter._set_fatal_error("yuanbao_missing_dependency", msg, retryable=True)
            logger.warning("[%s] %s. Run: pip install websockets", adapter.name, msg)
            return False

        if not adapter._app_key or not adapter._app_secret:
            msg = (
                "Yuanbao startup failed: "
                "YUANBAO_APP_ID and YUANBAO_APP_SECRET are required"
            )
            adapter._set_fatal_error("yuanbao_missing_credentials", msg, retryable=False)
            logger.error("[%s] %s", adapter.name, msg)
            return False

        # 幂等性保护
        if self._ws is not None:
            try:
                open_attr = getattr(self._ws, "open", None)
                if open_attr is True or (callable(open_attr) and open_attr()):
                    logger.debug("[%s] Already connected, skipping connect()", adapter.name)
                    return True
            except Exception:
                pass

        # 获取平台级锁以防止重复连接
        if not adapter._acquire_platform_lock(
            'yuanbao-app-key', adapter._app_key, 'Yuanbao app key'
        ):
            return False

        try:
            # 步骤 1：获取 sign token
            logger.info("[%s] Fetching sign token from %s", adapter.name, adapter._api_domain)
            token_data = await SignManager.get_token(
                adapter._app_key, adapter._app_secret, adapter._api_domain,
                route_env=adapter._route_env,
            )

            # 若 sign-token API 返回了 bot_id 则更新
            if token_data.get("bot_id"):
                adapter._bot_id = str(token_data["bot_id"])

            # 步骤 2：打开 WebSocket 连接（禁用内置 ping/pong）
            logger.info("[%s] Connecting to %s", adapter.name, adapter._ws_url)
            self._ws = await asyncio.wait_for(
                websockets.connect(  # type: ignore[attr-defined]
                    adapter._ws_url,
                    ping_interval=None,
                    ping_timeout=None,
                    close_timeout=5,
                ),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )

            # 步骤 3：鉴权（AUTH_BIND + 等待 BIND_ACK）
            authed = await self._authenticate(token_data)
            if not authed:
                await self._cleanup_ws()
                return False

            # 步骤 4：启动后台任务
            self._reconnect_attempts = 0
            adapter._mark_connected()
            adapter._loop = asyncio.get_running_loop()
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name=f"yuanbao-heartbeat-{self._connect_id}"
            )
            self._recv_task = asyncio.create_task(
                self._receive_loop(), name=f"yuanbao-recv-{self._connect_id}"
            )
            logger.info(
                "[%s] Connected. connectId=%s botId=%s",
                adapter.name, self._connect_id, adapter._bot_id,
            )

            YuanbaoAdapter.set_active(adapter)

            return True

        except asyncio.TimeoutError:
            logger.error("[%s] Connection timed out", adapter.name)
            await self._cleanup_ws()
            adapter._release_platform_lock()
            return False
        except Exception as exc:
            logger.error("[%s] connect() failed: %s", adapter.name, exc, exc_info=True)
            await self._cleanup_ws()
            adapter._release_platform_lock()
            return False

    async def close(self) -> None:
        """取消后台任务、让 pending future 失败，并关闭 WebSocket。"""

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
            self._recv_task = None

        # 让所有 pending ACK future 失败
        disc_exc = RuntimeError("YuanbaoAdapter disconnected")
        for fut in self._pending_acks.values():
            if not fut.done():
                fut.set_exception(disc_exc)
        self._pending_acks.clear()

        # 清理刷新锁，以避免来自前一个事件循环的失效锁
        SignManager.clear_locks()

        await self._cleanup_ws()

    # -- 鉴权 ----------------------------------------------------

    async def _authenticate(self, token_data: dict) -> bool:
        """发送 AUTH_BIND 并读取帧，直到收到 BIND_ACK。

        成功返回 True，失败/超时返回 False。
        """
        adapter = self._adapter
        if self._ws is None:
            return False

        token = token_data.get("token", "")
        uid = adapter._bot_id or token_data.get("bot_id", "")
        source = token_data.get("source") or "bot"
        route_env = adapter._route_env or token_data.get("route_env", "") or ""

        msg_id = str(uuid.uuid4())

        auth_bytes = encode_auth_bind(
            biz_id="ybBot",
            uid=uid,
            source=source,
            token=token,
            msg_id=msg_id,
            app_version=_APP_VERSION,
            operation_system=_OPERATION_SYSTEM,
            bot_version=_BOT_VERSION,
            route_env=route_env,
        )
        await self._ws.send(auth_bytes)
        logger.debug("[%s] AUTH_BIND sent (msg_id=%s uid=%s)", adapter.name, msg_id, uid)

        try:
            _loop = asyncio.get_running_loop()
            deadline = _loop.time() + AUTH_TIMEOUT_SECONDS
            while True:
                remaining = deadline - _loop.time()
                if remaining <= 0:
                    logger.error("[%s] AUTH_BIND timeout waiting for BIND_ACK", adapter.name)
                    return False

                raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
                if not isinstance(raw, (bytes, bytearray)):
                    continue

                try:
                    msg = decode_conn_msg(bytes(raw))
                except Exception:
                    continue

                head = msg.get("head", {})
                cmd_type = head.get("cmd_type", -1)
                cmd = head.get("cmd", "")

                if cmd_type == CMD_TYPE["Response"] and cmd == "auth-bind":
                    connect_id = self._extract_connect_id(msg)
                    if connect_id:
                        self._connect_id = connect_id
                        logger.info("[%s] BIND_ACK received: connectId=%s", adapter.name, connect_id)
                        return True
                    else:
                        logger.error("[%s] BIND_ACK missing connectId", adapter.name)
                        return False

        except asyncio.TimeoutError:
            logger.error("[%s] AUTH_BIND timeout", adapter.name)
            return False
        except Exception as exc:
            logger.error("[%s] AUTH_BIND error: %s", adapter.name, exc, exc_info=True)
            return False

    def _extract_connect_id(self, decoded_msg: dict) -> Optional[str]:
        """从解码后的 BIND_ACK 消息中提取 connectId。"""
        data: bytes = decoded_msg.get("data", b"")
        if not data:
            return None
        try:
            fdict = _fields_to_dict(_parse_fields(data))
            code = _get_varint(fdict, 1)
            if code != 0:
                message = _get_string(fdict, 2)
                logger.error(
                    "[%s] AuthBindRsp error: code=%d message=%r",
                    self._adapter.name, code, message,
                )
                return None
            connect_id = _get_string(fdict, 3)
            return connect_id if connect_id else None
        except Exception as exc:
            logger.warning("[%s] Failed to extract connectId: %s", self._adapter.name, exc)
            return None

    # -- 心跳 ---------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """每 30s 发送一次 HEARTBEAT（ping）；连续丢失超过阈值则触发重连。"""
        adapter = self._adapter
        try:
            while adapter._running:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                if self._ws is None:
                    continue
                try:
                    msg_id = str(uuid.uuid4())
                    ping_bytes = encode_ping(msg_id)
                    loop = asyncio.get_running_loop()
                    pong_future: asyncio.Future = loop.create_future()
                    self._pending_pong = pong_future
                    self._pending_acks[msg_id] = pong_future
                    await self._ws.send(ping_bytes)
                    logger.debug("[%s] PING sent (msg_id=%s)", adapter.name, msg_id)
                    try:
                        await asyncio.wait_for(pong_future, timeout=10.0)
                        self._consecutive_hb_timeouts = 0
                    except asyncio.TimeoutError:
                        self._pending_acks.pop(msg_id, None)
                        self._consecutive_hb_timeouts += 1
                        logger.warning(
                            "[%s] PONG timeout (%d/%d)",
                            adapter.name, self._consecutive_hb_timeouts, HEARTBEAT_TIMEOUT_THRESHOLD,
                        )
                        if self._consecutive_hb_timeouts >= HEARTBEAT_TIMEOUT_THRESHOLD:
                            logger.warning("[%s] Heartbeat threshold exceeded, triggering reconnect", adapter.name)
                            self.schedule_reconnect()
                            return
                    finally:
                        self._pending_acks.pop(msg_id, None)
                        self._pending_pong = None
                except Exception as exc:
                    logger.debug("[%s] Heartbeat send failed: %s", adapter.name, exc)
        except asyncio.CancelledError:
            pass

    # -- 接收循环 ------------------------------------------------------

    async def _receive_loop(self) -> None:
        """读取 WS 帧并按 cmd_type 分发。"""
        adapter = self._adapter
        try:
            async for raw in self._ws:  # type: ignore[union-attr]
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                await self._handle_frame(bytes(raw))
        except asyncio.CancelledError:
            pass
        except websockets.exceptions.ConnectionClosed as close_exc:  # type: ignore[union-attr]
            close_code = getattr(close_exc, 'code', None)
            logger.warning(
                "[%s] WebSocket connection closed: code=%s reason=%s",
                adapter.name, close_code, getattr(close_exc, 'reason', ''),
            )
            if close_code and close_code in NO_RECONNECT_CLOSE_CODES:
                logger.error(
                    "[%s] Close code %d is non-recoverable, NOT reconnecting",
                    adapter.name, close_code,
                )
                adapter._mark_disconnected()
            else:
                self.schedule_reconnect()
        except Exception as exc:
            logger.warning("[%s] receive_loop exited: %s", adapter.name, exc)
            self.schedule_reconnect()

    async def _handle_frame(self, raw: bytes) -> None:
        """处理单个 WebSocket 帧。"""
        adapter = self._adapter
        try:
            msg = decode_conn_msg(raw)
        except Exception as exc:
            logger.debug("[%s] Failed to decode frame: %s", adapter.name, exc)
            return

        head = msg.get("head", {})
        cmd_type = head.get("cmd_type", -1)
        cmd = head.get("cmd", "")
        msg_id = head.get("msg_id", "")
        need_ack = head.get("need_ack", False)
        data: bytes = msg.get("data", b"")

        # HEARTBEAT_ACK
        if cmd_type == CMD_TYPE["Response"] and cmd == "ping":
            logger.debug("[%s] HEARTBEAT_ACK received (msg_id=%s)", adapter.name, msg_id)
            if self._pending_pong is not None and not self._pending_pong.done():
                self._pending_pong.set_result(True)
            elif msg_id and msg_id in self._pending_acks:
                fut = self._pending_acks.pop(msg_id)
                if not fut.done():
                    fut.set_result(True)
            return

        # 即发即忘的心跳 ACK —— 服务器总会响应，但调用方不会等待这些；
        # 静默丢弃以避免 "Unmatched Response" 噪音。
        if cmd_type == CMD_TYPE["Response"] and cmd in {
            "send_group_heartbeat",
            "send_private_heartbeat",
        }:
            logger.debug("[%s] Heartbeat ACK received: cmd=%s msg_id=%s", adapter.name, cmd, msg_id)
            return

        # 出站 RPC 调用的响应
        if cmd_type == CMD_TYPE["Response"]:
            if msg_id and msg_id in self._pending_acks:
                fut = self._pending_acks.pop(msg_id)
                if not fut.done():
                    result = {"head": head}
                    if data:
                        result["data"] = data
                    fut.set_result(result)
            else:
                logger.debug(
                    "[%s] Unmatched Response: cmd=%s msg_id=%s",
                    adapter.name, cmd, msg_id,
                )
            return

        # 服务器主动发起的 Push
        if cmd_type == CMD_TYPE["Push"]:
            logger.info("[%s] Push received: cmd=%s msg_id=%s data_len=%d", adapter.name, cmd, msg_id, len(data))
            if need_ack and self._ws is not None:
                try:
                    ack_bytes = encode_push_ack(head)
                    await self._ws.send(ack_bytes)
                except Exception as ack_exc:
                    logger.debug("[%s] Failed to send PushAck: %s", adapter.name, ack_exc)

            if msg_id and msg_id in self._pending_acks:
                fut = self._pending_acks.pop(msg_id)
                if not fut.done():
                    try:
                        decoded = decode_inbound_push(data) if data else {"head": head}
                        fut.set_result(decoded)
                    except Exception as exc:
                        fut.set_exception(exc)
                return

            # 真正的入站消息 —— 派发给 AI
            if data:
                logger.info(
                    "[%s] WS received inbound push, decoding and dispatching: cmd=%s, data_len=%d",
                    adapter.name, cmd, len(data),
                )
                self._push_to_inbound(data)
            return

        logger.debug(
            "[%s] Ignoring frame: cmd_type=%d cmd=%s msg_id=%s",
            adapter.name, cmd_type, cmd, msg_id,
        )

    # -- 入站分发 ---------------------------------------------------

    _DEBOUNCE_WINDOW: float = 1.5  # 等待配套消息的秒数

    def _extract_sender_key(self, raw_data: bytes) -> str:
        """轻量解码以提取用于防抖分组的 sender key。

        返回 'from_account:group_code'，或一个兜底的唯一 key。
        """
        try:
            parsed = json.loads(raw_data.decode("utf-8"))
            if isinstance(parsed, dict):
                from_account = (
                    parsed.get("from_account", "")
                    or parsed.get("From_Account", "")
                )
                group_code = (
                    parsed.get("group_code", "")
                    or parsed.get("GroupId", "")
                    or parsed.get("group_id", "")
                )
                if from_account:
                    return f"{from_account}:{group_code}"
        except Exception:
            pass
        # Protobuf：尝试用 decode_inbound_push 提取发送者信息
        try:
            push = decode_inbound_push(raw_data)
            if push:
                return f"{push.get('from_account', '')}:{push.get('group_code', '')}"
        except Exception:
            pass
        # 兜底：唯一 key（不进行聚合）
        return f"__unknown_{id(raw_data)}"

    def _push_to_inbound(self, raw_data: bytes) -> None:
        """防抖式入站分发。

        在一个短时间窗口内缓冲来自同一发送者的原始帧，然后将所有缓冲
        数据作为一次聚合的管道执行来分发。这会将多部分消息（例如图片
        + 文本作为独立的 WS push 发送）合并为一次管道运行。
        """
        key = self._extract_sender_key(raw_data)

        # 取消该 key 已存在的定时器（重置防抖窗口）
        existing_timer = self._inbound_timers.pop(key, None)
        if existing_timer:
            existing_timer.cancel()

        # 追加到缓冲区
        if key not in self._inbound_buffer:
            self._inbound_buffer[key] = []
        self._inbound_buffer[key].append(raw_data)

        logger.debug(
            "[%s] Debounce: buffered frame for key=%s, count=%d",
            self._adapter.name, key, len(self._inbound_buffer[key]),
        )

        # 在防抖窗口结束后调度 flush
        loop = asyncio.get_running_loop()
        timer = loop.call_later(
            self._DEBOUNCE_WINDOW,
            self._flush_inbound_buffer,
            key,
        )
        self._inbound_timers[key] = timer

    def _flush_inbound_buffer(self, key: str) -> None:
        """flush 指定 key 的防抖缓冲区 —— 执行管道。"""
        self._inbound_timers.pop(key, None)
        data_list = self._inbound_buffer.pop(key, [])
        if not data_list:
            return

        adapter = self._adapter
        logger.info(
            "[%s] Debounce flush: key=%s, aggregated %d frames",
            adapter.name, key, len(data_list),
        )

        ctx = InboundContext(adapter=adapter, raw_frames=data_list)

        adapter._track_task(asyncio.create_task(
            adapter._inbound_pipeline.execute(ctx),
            name=f"yuanbao-pipeline-{key}",
        ))

    # -- 发送业务请求 ---------------------------------------------

    async def send_biz_request(
        self,
        encoded_conn_msg: bytes,
        req_id: str,
        timeout: float = DEFAULT_SEND_TIMEOUT,
    ) -> dict:
        """发送业务层请求并等待响应。

        1. 在 pending_acks[req_id] 中注册一个 Future
        2. 将 encoded_conn_msg（bytes）发送到 WS
        3. asyncio.wait_for(future, timeout)
        4. 超时/异常时清理 pending_acks
        """
        if self._ws is None:
            raise RuntimeError("Not connected")

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_acks[req_id] = future
        try:
            await self._ws.send(encoded_conn_msg)
            result = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            return result
        except asyncio.TimeoutError:
            raise
        except Exception:
            raise
        finally:
            self._pending_acks.pop(req_id, None)

    # -- 重连 ---------------------------------------------------------

    def schedule_reconnect(self) -> None:
        """仅在运行中且未正在重连时调度一次重连。"""
        if self._adapter._running and not self._reconnecting:
            asyncio.create_task(self._reconnect_with_backoff())

    async def _reconnect_with_backoff(self) -> bool:
        """带指数退避的重连（1s、2s、4s、…… 上限 60s）。"""
        if self._reconnecting:
            logger.debug("[%s] Reconnect already in progress, skipping", self._adapter.name)
            return False
        self._reconnecting = True
        try:
            return await self._do_reconnect()
        finally:
            self._reconnecting = False

    async def _do_reconnect(self) -> bool:
        """内部重连循环，在 _reconnecting 保护下调用。"""
        adapter = self._adapter
        for attempt in range(MAX_RECONNECT_ATTEMPTS):
            self._reconnect_attempts = attempt + 1
            wait = min(2 ** attempt, 60)
            logger.info(
                "[%s] Reconnect attempt %d/%d in %ds",
                adapter.name, attempt + 1, MAX_RECONNECT_ATTEMPTS, wait,
            )
            await asyncio.sleep(wait)

            await self._cleanup_ws()

            try:
                token_data = await SignManager.force_refresh(
                    adapter._app_key, adapter._app_secret, adapter._api_domain,
                    route_env=adapter._route_env,
                )
                if token_data.get("bot_id"):
                    adapter._bot_id = str(token_data["bot_id"])

                self._ws = await asyncio.wait_for(
                    websockets.connect(  # type: ignore[attr-defined]
                        adapter._ws_url,
                        ping_interval=None,
                        ping_timeout=None,
                        close_timeout=5,
                    ),
                    timeout=CONNECT_TIMEOUT_SECONDS,
                )

                authed = await self._authenticate(token_data)
                if not authed:
                    logger.warning("[%s] Re-auth failed on attempt %d", adapter.name, attempt + 1)
                    await self._cleanup_ws()
                    continue

                self._reconnect_attempts = 0
                self._consecutive_hb_timeouts = 0
                adapter._mark_connected()

                if self._heartbeat_task and not self._heartbeat_task.done():
                    self._heartbeat_task.cancel()
                self._heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(),
                    name=f"yuanbao-heartbeat-{self._connect_id}",
                )

                if self._recv_task and not self._recv_task.done():
                    self._recv_task.cancel()
                self._recv_task = asyncio.create_task(
                    self._receive_loop(),
                    name=f"yuanbao-recv-{self._connect_id}",
                )

                logger.info(
                    "[%s] Reconnected on attempt %d. connectId=%s",
                    adapter.name, attempt + 1, self._connect_id,
                )
                return True

            except asyncio.TimeoutError:
                logger.warning("[%s] Reconnect attempt %d timed out", adapter.name, attempt + 1)
            except Exception as exc:
                logger.warning(
                    "[%s] Reconnect attempt %d failed: %s", adapter.name, attempt + 1, exc
                )

        logger.error(
            "[%s] Giving up after %d reconnect attempts", adapter.name, MAX_RECONNECT_ATTEMPTS
        )
        adapter._mark_disconnected()
        return False

    async def _cleanup_ws(self) -> None:
        """关闭并清理 WebSocket 连接，受 ``WS_CLOSE_TIMEOUT_S`` 上限约束，
        以防无响应的服务器卡住拆卸过程（完整理由见该常量的定义）。"""
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), timeout=WS_CLOSE_TIMEOUT_S)
            except asyncio.TimeoutError:
                # 服务器在上限内始终未回传 close 帧；丢弃连接。
                # websockets 在取消时会强制关闭传输层，且关闭时事件循环
                # 本身也正在拆除。
                logger.debug(
                    "[%s] WS close handshake exceeded %.1fs — dropping connection",
                    self._adapter.name, WS_CLOSE_TIMEOUT_S,
                )
            except Exception:
                pass

class MediaSendHandler(ABC):
    """媒体发送策略的抽象基类。

    子类实现：
      - acquire_file()：如何获取文件字节（下载 URL / 读取本地）
      - build_msg_body()：如何从上传结果构建 TIMxxxElem

    共享流程（检查 ws → 取消通知 → 校验 → COS 上传
    → 加锁 → 分发）由基类 handle() 模板方法处理。
    """

    @abstractmethod
    async def acquire_file(
        self, adapter: "YuanbaoAdapter", **kwargs: Any,
    ) -> Tuple[bytes, str, str]:
        """返回 (file_bytes, filename, content_type)。

        异常：
            ValueError: 无法获取文件时（未找到、为空等）。
        """

    @abstractmethod
    def build_msg_body(self, upload_result: dict, **kwargs: Any) -> list:
        """从 COS 上传结果构建平台特定的 MsgBody 列表。"""

    def needs_cos_upload(self) -> bool:
        """对非 COS 媒体（如贴纸）覆写为返回 False。"""
        return True

    async def handle(
        self,
        adapter: "YuanbaoAdapter",
        chat_id: str,
        reply_to: Optional[str] = None,
        caption: Optional[str] = None,
        **kwargs: Any,
    ) -> "SendResult":
        """模板方法：共享的媒体发送流程。"""
        conn = adapter._connection
        sender = adapter._outbound.sender

        if conn.ws is None:
            return SendResult(success=False, error="Not connected", retryable=True)

        adapter._outbound.cancel_slow_notifier(chat_id)

        try:
            # 1. 获取文件字节
            file_bytes, filename, content_type = await self.acquire_file(
                adapter, **kwargs,
            )

            # 2. 校验（仅对上传到 COS 的 handler；贴纸使用 TIMFaceElem，
            # 合理地不携带文件字节，因此此处跳过 validate_media 可避免
            # 误报 "Empty file: sticker"）。
            if self.needs_cos_upload():
                validation_err = MessageSender.validate_media(
                    file_bytes, filename, adapter.MEDIA_MAX_SIZE_MB,
                )
                if validation_err:
                    return SendResult(success=False, error=validation_err)

            if self.needs_cos_upload():
                file_uuid = md5_hex(file_bytes)

                # 3. 获取 COS 上传凭证
                token_data = await adapter._get_cached_token()
                token: str = token_data.get("token", "")
                bot_id: str = (
                    token_data.get("bot_id", "") or adapter._bot_id or ""
                )

                credentials = await get_cos_credentials(
                    app_key=adapter._app_key,
                    api_domain=adapter._api_domain,
                    token=token,
                    filename=filename,
                    bot_id=bot_id,
                    route_env=adapter._route_env,
                )

                # 4. 上传到 COS
                upload_result = await upload_to_cos(
                    file_bytes=file_bytes,
                    filename=filename,
                    content_type=content_type,
                    credentials=credentials,
                    bucket=credentials["bucketName"],
                    region=credentials["region"],
                )

                # 5. 构建 MsgBody
                # 移除已显式传入的 key，以避免 "multiple values" TypeError
                fwd_kwargs = {
                    k: v for k, v in kwargs.items()
                    if k not in {"file_uuid", "filename", "content_type"}
                }
                msg_body = self.build_msg_body(
                    upload_result,
                    file_uuid=file_uuid,
                    filename=filename,
                    content_type=content_type,
                    **fwd_kwargs,
                )
            else:
                # 非 COS 媒体（如贴纸）：直接构建 MsgBody
                msg_body = self.build_msg_body({}, **kwargs)

            # 6. 若提供了 caption 则追加
            if caption:
                msg_body.append(
                    {"msg_type": "TIMTextElem", "msg_content": {"text": caption}},
                )

            # 7. 加锁 + 分发
            gc = kwargs.get("group_code", "")
            return await sender.dispatch_msg_body(chat_id, msg_body, reply_to, group_code=gc)

        except ValueError as ve:
            return SendResult(success=False, error=str(ve))
        except Exception as exc:
            handler_name = type(self).__name__
            logger.error(
                "[%s] %s.handle() failed: %s",
                adapter.name, handler_name, exc, exc_info=True,
            )
            return SendResult(success=False, error=str(exc))


class ImageUrlHandler(MediaSendHandler):
    """策略：从 URL 发送图片（下载 → COS → TIMImageElem）。"""

    async def acquire_file(self, adapter, **kwargs):
        image_url: str = kwargs["image_url"]
        logger.info("[%s] ImageUrlHandler: downloading %s", adapter.name, image_url)
        file_bytes, content_type = await media_download_url(
            image_url, max_size_mb=adapter.MEDIA_MAX_SIZE_MB,
        )
        if not content_type or content_type == "application/octet-stream":
            path_part = image_url.split("?")[0]
            content_type = guess_mime_type(path_part) or "image/jpeg"
        filename = os.path.basename(image_url.split("?")[0]) or "image.jpg"
        return file_bytes, filename, content_type

    def build_msg_body(self, upload_result, **kwargs):
        return build_image_msg_body(
            url=upload_result["url"],
            uuid=kwargs["file_uuid"],
            filename=kwargs["filename"],
            size=upload_result["size"],
            width=upload_result.get("width", 0),
            height=upload_result.get("height", 0),
            mime_type=kwargs["content_type"],
        )


class ImageFileHandler(MediaSendHandler):
    """策略：从本地文件路径发送图片（读取 → COS → TIMImageElem）。"""

    async def acquire_file(self, adapter, **kwargs):
        image_path: str = kwargs["image_path"]
        if not os.path.isfile(image_path):
            raise ValueError(f"File not found: {image_path}")
        logger.info("[%s] ImageFileHandler: reading %s", adapter.name, image_path)
        with open(image_path, "rb") as f:
            file_bytes = f.read()
        filename = os.path.basename(image_path) or "image.jpg"
        content_type = guess_mime_type(filename) or "image/jpeg"
        return file_bytes, filename, content_type

    def build_msg_body(self, upload_result, **kwargs):
        return build_image_msg_body(
            url=upload_result["url"],
            uuid=kwargs["file_uuid"],
            filename=kwargs["filename"],
            size=upload_result["size"],
            width=upload_result.get("width", 0),
            height=upload_result.get("height", 0),
            mime_type=kwargs["content_type"],
        )


class FileUrlHandler(MediaSendHandler):
    """策略：从 URL 发送文件（下载 → COS → TIMFileElem）。"""

    async def acquire_file(self, adapter, **kwargs):
        file_url: str = kwargs["file_url"]
        logger.info("[%s] FileUrlHandler: downloading %s", adapter.name, file_url)
        file_bytes, content_type = await media_download_url(
            file_url, max_size_mb=adapter.MEDIA_MAX_SIZE_MB,
        )
        filename = kwargs.get("filename")
        if not filename:
            path_part = file_url.split("?")[0]
            filename = os.path.basename(path_part) or "file"
        if not content_type or content_type == "application/octet-stream":
            content_type = guess_mime_type(filename) or "application/octet-stream"
        return file_bytes, filename, content_type

    def build_msg_body(self, upload_result, **kwargs):
        return build_file_msg_body(
            url=upload_result["url"],
            filename=kwargs["filename"],
            uuid=kwargs["file_uuid"],
            size=upload_result["size"],
        )


class DocumentHandler(MediaSendHandler):
    """策略：发送本地文件/文档（读取 → COS → TIMFileElem）。"""

    async def acquire_file(self, adapter, **kwargs):
        file_path: str = kwargs["file_path"]
        if not os.path.isfile(file_path):
            raise ValueError(f"File not found: {file_path}")
        logger.info("[%s] DocumentHandler: reading %s", adapter.name, file_path)
        with open(file_path, "rb") as f:
            file_bytes = f.read()
        filename = kwargs.get("filename") or os.path.basename(file_path) or "document"
        content_type = guess_mime_type(filename) or "application/octet-stream"
        return file_bytes, filename, content_type

    def build_msg_body(self, upload_result, **kwargs):
        return build_file_msg_body(
            url=upload_result["url"],
            filename=kwargs["filename"],
            uuid=kwargs["file_uuid"],
            size=upload_result["size"],
        )


class StickerHandler(MediaSendHandler):
    """策略：发送贴纸/表情（TIMFaceElem，无需 COS 上传）。"""

    def needs_cos_upload(self) -> bool:
        return False

    async def acquire_file(self, adapter, **kwargs):
        # 贴纸不需要文件字节；返回占位值
        return b"", "sticker", "application/octet-stream"

    def build_msg_body(self, upload_result, **kwargs):
        from gateway.platforms.yuanbao_sticker import (
            get_sticker_by_name,
            get_random_sticker,
            build_face_msg_body,
            build_sticker_msg_body,
        )
        sticker_name = kwargs.get("sticker_name")
        face_index = kwargs.get("face_index")

        if sticker_name is not None:
            sticker = get_sticker_by_name(sticker_name)
            if sticker is None:
                raise ValueError(f"Sticker not found: {sticker_name!r}")
            return build_sticker_msg_body(sticker)
        elif face_index is not None:
            return build_face_msg_body(face_index=face_index)
        else:
            sticker = get_random_sticker()
            return build_sticker_msg_body(sticker)

class GroupQueryService:
    """封装所有群查询操作（包括底层 WS 调用和面向 AI 工具的高层封装）。

    职责：
      - 群信息和成员列表查询的底层 WS 编解码
      - 面向 AI 工具的 chat_id 解析、错误包装和结果过滤
      - 在 adapter 上填充成员缓存
    """

    def __init__(self, adapter: "YuanbaoAdapter") -> None:
        self._adapter = adapter

    # ------------------------------------------------------------------
    # 底层 WS 查询方法
    # ------------------------------------------------------------------

    async def query_group_info_raw(self, group_code: str) -> Optional[dict]:
        """通过 WS 查询群信息（群名、群主、成员数等）。

        返回：
            解码后的 dict，失败返回 None。
        """
        adapter = self._adapter
        if adapter._connection.ws is None:
            return None
        encoded = encode_query_group_info(group_code)
        from gateway.platforms.yuanbao_proto import decode_conn_msg as _decode
        decoded = _decode(encoded)
        req_id = decoded["head"]["msg_id"]
        try:
            response = await adapter._connection.send_biz_request(encoded, req_id=req_id)
            head = response.get("head", {})
            status = head.get("status", 0)
            if status != 0:
                logger.warning("[%s] query_group_info failed: status=%d", adapter.name, status)
                return None
            biz_data = response.get("data", b"") or response.get("body", b"")
            if biz_data and isinstance(biz_data, bytes):
                return decode_query_group_info_rsp(biz_data)
            return {"group_code": group_code}
        except asyncio.TimeoutError:
            logger.warning("[%s] query_group_info timeout: group=%s", adapter.name, group_code)
            return None
        except Exception as exc:
            logger.warning("[%s] query_group_info failed: %s", adapter.name, exc)
            return None

    async def get_group_member_list_raw(
        self, group_code: str, offset: int = 0, limit: int = 200
    ) -> Optional[dict]:
        """通过 WS 查询群成员列表。

        返回：
            解码后的 dict，失败返回 None。同时会填充 adapter._member_cache。
        """
        adapter = self._adapter
        if adapter._connection.ws is None:
            return None
        encoded = encode_get_group_member_list(group_code, offset=offset, limit=limit)
        from gateway.platforms.yuanbao_proto import decode_conn_msg as _decode
        decoded = _decode(encoded)
        req_id = decoded["head"]["msg_id"]
        try:
            response = await adapter._connection.send_biz_request(encoded, req_id=req_id)
            head = response.get("head", {})
            status = head.get("status", 0)
            if status != 0:
                logger.warning("[%s] get_group_member_list failed: status=%d", adapter.name, status)
                return None
            biz_data = response.get("data", b"") or response.get("body", b"")
            if biz_data and isinstance(biz_data, bytes):
                result = decode_get_group_member_list_rsp(biz_data)
            else:
                result = {"members": [], "next_offset": 0, "is_complete": True}
            if result and result.get("members"):
                adapter._member_cache[group_code] = (time.time(), result["members"])
            return result
        except asyncio.TimeoutError:
            logger.warning("[%s] get_group_member_list timeout: group=%s", adapter.name, group_code)
            return None
        except Exception as exc:
            logger.warning("[%s] get_group_member_list failed: %s", adapter.name, exc)
            return None

    # ------------------------------------------------------------------
    # 面向 AI 工具的封装（chat_id 解析 + 过滤）
    # ------------------------------------------------------------------

    async def query_group_info(self, chat_id: str) -> dict:
        """AI 工具：查询当前群信息。

        无需参数（group_code 从会话上下文中提取）。
        返回群名、群主、成员数等。
        """
        if not chat_id.startswith("group:"):
            return {"error": "This command is only available in group chats"}
        group_code = chat_id[len("group:"):]
        result = await self.query_group_info_raw(group_code)
        if result is None:
            return {"error": "Failed to query group info"}
        return result

    async def query_session_members(
        self,
        chat_id: str,
        action: str = "list_all",
        name: Optional[str] = None,
    ) -> dict:
        """AI 工具：查询群成员列表。

        参数：
            chat_id: 会话 ID（从会话上下文提取）
            action: 'find'（按名称搜索）| 'list_bots'（列出 bot）| 'list_all'（列出全部）
            name: action='find' 时的搜索关键词

        返回：
            {"members": [...], "total": int, "mentionHint": str}
        """
        if not chat_id.startswith("group:"):
            return {"error": "This command is only available in group chats"}
        group_code = chat_id[len("group:"):]
        result = await self.get_group_member_list_raw(group_code)
        if result is None:
            return {"error": "Failed to query group members"}

        members = result.get("members", [])

        if action == "find" and name:
            query = name.lower()
            members = [
                m for m in members
                if query in (m.get("nickname", "") or "").lower()
                or query in (m.get("name_card", "") or "").lower()
                or query in (m.get("user_id", "") or "").lower()
            ]
        elif action == "list_bots":
            members = [m for m in members if "bot" in (m.get("nickname", "") or "").lower()]

        # 构建 mentionHint
        mention_hint = ""
        if members and len(members) <= 10:
            names = [m.get("name_card") or m.get("nickname") or m.get("user_id", "") for m in members]
            mention_hint = "Mention with @name: " + ", ".join(names)

        return {
            "members": members[:50],  # 限制返回数量
            "total": len(members),
            "mentionHint": mention_hint,
        }


class HeartbeatManager:
    """管理回复心跳（RUNNING / FINISH）的生命周期。

    职责：
      - 周期性 RUNNING 心跳发送（每 2s 一次）
      - 闲置 30s 后自动 FINISH
      - 显式停止，可选择发送 FINISH 信号
    """

    def __init__(self, adapter: "YuanbaoAdapter") -> None:
        self._adapter = adapter
        self._reply_heartbeat_tasks: Dict[str, asyncio.Task] = {}
        self._reply_hb_last_active: Dict[str, float] = {}

    async def send_heartbeat_once(self, chat_id: str, heartbeat_val: int) -> None:
        """发送单次心跳（RUNNING 或 FINISH），尽力而为。"""
        adapter = self._adapter
        conn = adapter._connection
        if conn.ws is None or not adapter._bot_id:
            return
        try:
            if chat_id.startswith("group:"):
                group_code = chat_id[len("group:"):]
                encoded = encode_send_group_heartbeat(
                    from_account=adapter._bot_id,
                    group_code=group_code,
                    heartbeat=heartbeat_val,
                )
            else:
                to_account = chat_id.removeprefix("direct:")
                encoded = encode_send_private_heartbeat(
                    from_account=adapter._bot_id,
                    to_account=to_account,
                    heartbeat=heartbeat_val,
                )
            await conn.ws.send(encoded)
            status_name = "RUNNING" if heartbeat_val == WS_HEARTBEAT_RUNNING else "FINISH"
            logger.debug(
                "[%s] Reply heartbeat %s sent: chat=%s",
                adapter.name, status_name, chat_id,
            )
        except Exception as exc:
            logger.debug("[%s] send_heartbeat_once failed: %s", adapter.name, exc)

    async def start(self, chat_id: str) -> None:
        """启动或续期 Reply Heartbeat 周期发送器（RUNNING，每 2s 一次）。"""
        adapter = self._adapter
        conn = adapter._connection
        if conn.ws is None or not adapter._bot_id:
            return

        existing = self._reply_heartbeat_tasks.get(chat_id)
        if existing and not existing.done():
            self._reply_hb_last_active[chat_id] = time.time()
            return

        self._reply_hb_last_active[chat_id] = time.time()

        task = asyncio.create_task(
            self._worker(chat_id),
            name=f"yuanbao-reply-hb-{chat_id}",
        )
        self._reply_heartbeat_tasks[chat_id] = task

    async def _worker(self, chat_id: str) -> None:
        """后台协程：每 2s 发送一次 RUNNING 心跳。
        30s 未续期 -> 发送 FINISH 并退出。
        """
        try:
            await self.send_heartbeat_once(chat_id, WS_HEARTBEAT_RUNNING)

            while True:
                await asyncio.sleep(REPLY_HEARTBEAT_INTERVAL_S)

                last_active = self._reply_hb_last_active.get(chat_id, 0)
                if time.time() - last_active > REPLY_HEARTBEAT_TIMEOUT_S:
                    break

                conn = self._adapter._connection
                if conn.ws is None:
                    break

                await self.send_heartbeat_once(chat_id, WS_HEARTBEAT_RUNNING)

        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            cancelled = False
        else:
            cancelled = False
        finally:
            if not cancelled:
                try:
                    await self.send_heartbeat_once(chat_id, WS_HEARTBEAT_FINISH)
                except Exception:
                    pass
            self._reply_heartbeat_tasks.pop(chat_id, None)
            self._reply_hb_last_active.pop(chat_id, None)

    async def stop(self, chat_id: str, send_finish: bool = True) -> None:
        """停止 Reply Heartbeat，可选择发送 FINISH。"""
        task = self._reply_heartbeat_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if send_finish:
            try:
                await self.send_heartbeat_once(chat_id, WS_HEARTBEAT_FINISH)
            except Exception:
                pass

    async def close(self) -> None:
        """取消所有回复心跳任务。"""
        for task in list(self._reply_heartbeat_tasks.values()):
            if not task.done():
                task.cancel()
        self._reply_heartbeat_tasks.clear()
        self._reply_hb_last_active.clear()


class SlowResponseNotifier:
    """管理针对 agent 慢响应的延迟“请稍候”通知。

    每个 chat_id 启动一个定时器；若 agent 在 SLOW_RESPONSE_TIMEOUT_S 秒内
    未回复，则发送一条礼貌提示消息。
    """

    def __init__(self, adapter: "YuanbaoAdapter", sender: "MessageSender") -> None:
        self._adapter = adapter
        self._sender = sender
        self._tasks: Dict[str, asyncio.Task] = {}

    async def start(self, chat_id: str) -> None:
        """启动一个延迟任务，在 agent 响应慢时通知用户。"""
        self.cancel(chat_id)
        task = asyncio.create_task(
            self._notifier(chat_id),
            name=f"yuanbao-slow-resp-{chat_id}",
        )
        self._tasks[chat_id] = task

    async def _notifier(self, chat_id: str) -> None:
        """等待 SLOW_RESPONSE_TIMEOUT_S 后，推送一条“请稍候”消息。"""
        try:
            await asyncio.sleep(SLOW_RESPONSE_TIMEOUT_S)
            logger.info(
                "[%s] Agent response exceeded %ds for %s, sending wait notice",
                self._adapter.name, int(SLOW_RESPONSE_TIMEOUT_S), chat_id,
            )
            await self._sender.send_text_chunk(chat_id, SLOW_RESPONSE_MESSAGE)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("[%s] Slow-response notifier failed: %s", self._adapter.name, exc)

    def cancel(self, chat_id: str) -> None:
        """取消 *chat_id* 对应的待执行慢响应通知（如果存在）。"""
        task = self._tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    async def close(self) -> None:
        """取消所有慢响应任务。"""
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()


class MessageSender:
    """YuanbaoAdapter 的核心消息发送分发器。

    职责：
      - 每个 chat-id 的锁管理（串行发送顺序）
      - 带重试的文本分块发送
      - C2C / Group 消息的编码与分发
      - 媒体发送辅助方法（图片、文件、贴纸、文档）
      - 直接发送辅助（文本 + 媒体，供 send_message 工具使用）
    """

    IMAGE_EXTS: ClassVar[frozenset] = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})
    CHAT_DICT_MAX_SIZE: ClassVar[int] = 1000  # _chat_locks 中不同 chat ID 的最大数量

    def __init__(self, adapter: "YuanbaoAdapter") -> None:
        self._adapter = adapter
        self._chat_locks: collections.OrderedDict[str, asyncio.Lock] = collections.OrderedDict()

        # 由 OutboundManager 注入的可选协调钩子
        self._on_send_start: Optional[Callable[[str], Any]] = None   # 取消慢响应通知
        self._on_send_finish: Optional[Callable[[str], Any]] = None  # 发送 FINISH 心跳

        # 媒体发送 handler（策略模式）
        self._media_handlers: Dict[str, MediaSendHandler] = {
            "image_url": ImageUrlHandler(),
            "image_file": ImageFileHandler(),
            "file_url": FileUrlHandler(),
            "document": DocumentHandler(),
            "sticker": StickerHandler(),
        }

    # -- 媒体 handler 注册表 ---------------------------------------------

    def register_handler(self, name: str, handler: MediaSendHandler) -> None:
        """注册（或替换）一个具名的媒体发送 handler。"""
        self._media_handlers[name] = handler

    # -- 聊天锁 ---------------------------------------------------------

    def get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        """返回（或创建）一个按 chat-id 的锁，带安全的 LRU 淘汰。"""
        if chat_id in self._chat_locks:
            self._chat_locks.move_to_end(chat_id)
            return self._chat_locks[chat_id]
        if len(self._chat_locks) >= self.CHAT_DICT_MAX_SIZE:
            evicted = False
            for key in list(self._chat_locks):
                if not self._chat_locks[key].locked():
                    self._chat_locks.pop(key)
                    evicted = True
                    break
            if not evicted:
                self._chat_locks.pop(next(iter(self._chat_locks)))
        self._chat_locks[chat_id] = asyncio.Lock()
        return self._chat_locks[chat_id]

    # -- 文本发送 ---------------------------------------------------------

    async def send_text(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        group_code: str = "",
    ) -> "SendResult":
        """发送文本消息，带自动分块和按 chat-id 的顺序保证。"""
        adapter = self._adapter
        conn = adapter._connection
        if conn.ws is None:
            return SendResult(success=False, error="Not connected", retryable=True)

        if self._on_send_start:
            self._on_send_start(chat_id)

        lock = self.get_chat_lock(chat_id)
        async with lock:
            content_to_send = self.strip_cron_wrapper(content)
            chunks = self.truncate_message(content_to_send, adapter.MAX_TEXT_CHUNK)
            logger.info(
                "[%s] truncate_message: input=%d chars, max=%d, output=%d chunk(s) sizes=%s",
                adapter.name, len(content_to_send), adapter.MAX_TEXT_CHUNK,
                len(chunks), [len(c) for c in chunks],
            )
            for i, chunk in enumerate(chunks):
                r_to = reply_to if i == 0 else None
                result = await self.send_text_chunk(chat_id, chunk, r_to, group_code=group_code)
                if not result.success:
                    return result

        # 通知出站协调器发送已完成（例如 FINISH 心跳）
        if self._on_send_finish:
            try:
                await self._on_send_finish(chat_id)
            except Exception:
                pass
        return SendResult(success=True)

    async def send_media(
        self,
        chat_id: str,
        handler_name: str,
        reply_to: Optional[str] = None,
        caption: Optional[str] = None,
        **kwargs: Any,
    ) -> "SendResult":
        """将媒体发送分发给具名的 handler 策略。"""
        handler = self._media_handlers.get(handler_name)
        if handler is None:
            return SendResult(
                success=False,
                error=f"Unknown media handler: {handler_name!r}",
            )
        return await handler.handle(
            self._adapter, chat_id,
            reply_to=reply_to, caption=caption, **kwargs,
        )

    # -- 直接发送（文本 + 媒体，供 send_message 工具使用） -------------

    async def send_direct(
        self,
        chat_id: str,
        message: str,
        media_files: Optional[List[Tuple[str, bool]]] = None,
    ) -> Dict[str, Any]:
        """通过元宝发送文本 + 媒体（供 ``send_message`` 工具使用）。

        与每次调用都创建新 adapter 的 Weixin 不同，元宝复用运行中的
        网关 adapter（持久 WebSocket）。逻辑与 send_weixin_direct 对齐：
        先发送文本，再按扩展名遍历 media_files。
        """
        adapter = self._adapter
        last_result: Optional["SendResult"] = None

        # 1. 发送文本
        if message.strip():
            last_result = await adapter.send(chat_id, message)
            if not last_result.success:
                return {"error": f"Yuanbao send failed: {last_result.error}"}

        # 2. 遍历 media_files，按文件扩展名分发
        for media_path, _is_voice in media_files or []:
            ext = Path(media_path).suffix.lower()
            if ext in self.IMAGE_EXTS:
                last_result = await adapter.send_image_file(chat_id, media_path)
            else:
                last_result = await adapter.send_document(chat_id, media_path)

            if not last_result.success:
                return {"error": f"Yuanbao media send failed: {last_result.error}"}

        if last_result is None:
            return {"error": "No deliverable text or media remained after processing"}

        return {
            "success": True,
            "platform": "yuanbao",
            "chat_id": chat_id,
            "message_id": last_result.message_id if last_result else None,
        }

    async def dispatch_msg_body(
        self,
        chat_id: str,
        msg_body: list,
        reply_to: Optional[str] = None,
        group_code: str = "",
    ) -> "SendResult":
        """加锁并将任意 MsgBody 分发到 C2C 或群。"""
        lock = self.get_chat_lock(chat_id)
        async with lock:
            if chat_id.startswith("group:"):
                grp = chat_id[len("group:"):]
                result = await self.send_group_msg_body(grp, msg_body, reply_to)
            else:
                to_account = chat_id.removeprefix("direct:")
                result = await self.send_c2c_msg_body(to_account, msg_body, group_code=group_code)

        if result.get("success"):
            return SendResult(success=True, message_id=result.get("msg_key"))
        return SendResult(success=False, error=result.get("error", "Unknown error"))

    async def send_text_chunk(
        self,
        chat_id: str,
        text: str,
        reply_to: Optional[str] = None,
        retry: int = 3,
        group_code: str = "",
    ) -> "SendResult":
        """发送单个文本分块，带重试（指数退避：1s、2s、4s）。"""
        adapter = self._adapter
        last_error: str = "Unknown error"
        for attempt in range(retry):
            try:
                if chat_id.startswith("group:"):
                    grp = chat_id[len("group:"):]
                    raw = await self.send_group_message(grp, text, reply_to)
                else:
                    to_account = chat_id.removeprefix("direct:")
                    raw = await self.send_c2c_message(to_account, text, group_code=group_code)

                if raw.get("success"):
                    return SendResult(success=True, message_id=raw.get("msg_key"))

                last_error = raw.get("error", "Unknown error")
                logger.warning(
                    "[%s] send_text_chunk attempt %d/%d failed: %s",
                    adapter.name, attempt + 1, retry, last_error,
                )
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "[%s] send_text_chunk attempt %d/%d exception: %s",
                    adapter.name, attempt + 1, retry, last_error,
                )

            if attempt < retry - 1:
                await asyncio.sleep(2 ** attempt)

        logger.error(
            "[%s] send_text_chunk max retries (%d) exceeded. Last error: %s",
            adapter.name, retry, last_error,
        )
        return SendResult(success=False, error=f"Max retries exceeded: {last_error}")

    # -- C2C / Group 消息 -----------------------------------------------

    async def send_c2c_message(self, to_account: str, text: str, group_code: str = "") -> dict:
        """发送 C2C 文本消息，返回 {success: bool, msg_key: str}。"""
        msg_body = [{"msg_type": "TIMTextElem", "msg_content": {"text": text}}]
        return await self.send_c2c_msg_body(to_account, msg_body, group_code=group_code)

    async def send_group_message(
        self,
        group_code: str,
        text: str,
        reply_to: Optional[str] = None,
    ) -> dict:
        """发送群文本消息，自动将 @nickname 转换为 TIMCustomElem。"""
        msg_body = self._build_msg_body_with_mentions(text, group_code)
        return await self.send_group_msg_body(group_code, msg_body, reply_to)

    # @mention 模式：（空白或起始）+ @ + 昵称 +（空白或结束）
    _AT_USER_RE = re.compile(r'(?:(?<=\s)|(?<=^))@(\S+?)(?=\s|$)', re.MULTILINE)

    def _build_msg_body_with_mentions(self, text: str, group_code: str) -> list:
        """解析 @nickname 模式，构建混合的 TIMTextElem + TIMCustomElem msg_body。"""
        cached = self._adapter._member_cache.get(group_code)
        if cached:
            ts, member_list = cached
            members = member_list if (time.time() - ts < self._adapter.MEMBER_CACHE_TTL_S) else []
        else:
            members = []
        if not members:
            return [{"msg_type": "TIMTextElem", "msg_content": {"text": text}}]

        nickname_to_uid = {}
        for m in members:
            nick = m.get("nickname") or m.get("nick_name") or ""
            uid = m.get("user_id") or ""
            if nick and uid:
                nickname_to_uid[nick.lower()] = (nick, uid)

        msg_body: list = []
        last_idx = 0
        for match in self._AT_USER_RE.finditer(text):
            start = match.start()
            if start > last_idx:
                seg = text[last_idx:start].strip()
                if seg:
                    msg_body.append({"msg_type": "TIMTextElem", "msg_content": {"text": seg}})

            nickname = match.group(1)
            entry = nickname_to_uid.get(nickname.lower())
            if entry:
                real_nick, uid = entry
                msg_body.append({
                    "msg_type": "TIMCustomElem",
                    "msg_content": {
                        "data": json.dumps({"elem_type": 1002, "text": f"@{real_nick}", "user_id": uid}),
                    },
                })
            else:
                msg_body.append({"msg_type": "TIMTextElem", "msg_content": {"text": f"@{nickname}"}})

            last_idx = match.end()

        if last_idx < len(text):
            tail = text[last_idx:].strip()
            if tail:
                msg_body.append({"msg_type": "TIMTextElem", "msg_content": {"text": tail}})

        if not msg_body:
            msg_body.append({"msg_type": "TIMTextElem", "msg_content": {"text": text}})

        return msg_body

    async def send_c2c_msg_body(self, to_account: str, msg_body: list, group_code: str = "") -> dict:
        """发送带任意 MsgBody 的 C2C 消息。"""
        adapter = self._adapter
        req_id = f"c2c_{next_seq_no()}"
        encoded = encode_send_c2c_message(
            to_account=to_account,
            msg_body=msg_body,
            from_account=adapter._bot_id or "",
            msg_id=req_id,
            group_code=group_code,
        )
        return await self._dispatch_encoded(adapter, encoded, req_id)

    async def send_group_msg_body(
        self,
        group_code: str,
        msg_body: list,
        reply_to: Optional[str] = None,
    ) -> dict:
        """发送带任意 MsgBody 的群消息。"""
        adapter = self._adapter
        req_id = f"grp_{next_seq_no()}"
        encoded = encode_send_group_message(
            group_code=group_code,
            msg_body=msg_body,
            from_account=adapter._bot_id or "",
            msg_id=req_id,
            ref_msg_id=reply_to or "",
        )
        return await self._dispatch_encoded(adapter, encoded, req_id)

    # -- 通用分发辅助 --------------------------------------------

    @staticmethod
    async def _dispatch_encoded(
        adapter: "YuanbaoAdapter", encoded: bytes, req_id: str,
    ) -> dict:
        """通过 WS 发送已编码的 bytes，并返回归一化的结果 dict。"""
        try:
            response = await adapter._connection.send_biz_request(encoded, req_id=req_id)
            return {"success": True, "msg_key": response.get("msg_id", "")}
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Request timeout after {DEFAULT_SEND_TIMEOUT}s"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    # -- 媒体校验 ---------------------------------------------------

    @staticmethod
    def validate_media(
        file_bytes: Optional[bytes], filename: str, max_size_mb: int = 20
    ) -> Optional[str]:
        """媒体预校验：在发送/上传前检查文件有效性。

        返回：
            校验失败时返回错误描述 (str)，否则返回 None。
        """
        if file_bytes is None or len(file_bytes) == 0:
            return f"Empty file: {filename}"
        max_bytes = max_size_mb * 1024 * 1024
        if len(file_bytes) > max_bytes:
            size_mb = len(file_bytes) / 1024 / 1024
            return f"File too large: {filename} ({size_mb:.1f}MB > {max_size_mb}MB)"
        return None

    # -- 文本截断（表格感知） --------------------------------------

    @staticmethod
    def truncate_message(
        content: str,
        max_length: int = 4000,
        len_fn: Optional[Callable[[str], int]] = None,
    ) -> List[str]:
        """
        将长消息拆分为多个分块，具备表格感知能力。

        将核心拆分委托给 ``MarkdownProcessor.chunk_markdown_text``，
        并从输出中去掉形如 ``(1/3)`` 的页码指示符。

        对于非表格内容，以及整体文本能放入单个分块的情况，
        回退到 ``BasePlatformAdapter.truncate_message``。
        """
        _len = len_fn or len
        if _len(content) <= max_length:
            return [content]

        # 委托给 MarkdownProcessor 进行表格/围栏感知的分块
        chunks = MarkdownProcessor.chunk_markdown_text(
            content, max_length, len_fn=len_fn,
        )

        # 去掉 BasePlatformAdapter 可能添加的形如 (1/3) 的页码指示符
        chunks = [_INDICATOR_RE.sub('', c) for c in chunks]

        return chunks if chunks else [content]

    # -- Cron 包装剥离 ---------------------------------------------

    @staticmethod
    def strip_cron_wrapper(content: str) -> str:
        """剥离调度器 cron 的头部/尾部包装，以获得更干净的元宝输出。"""
        if not content.startswith("Cronjob Response: "):
            return content

        divider = "\n-------------\n\n"
        footer_prefix = '\n\nTo stop or manage this job, send me a new message (e.g. "stop reminder '
        divider_pos = content.find(divider)
        footer_pos = content.rfind(footer_prefix)
        if divider_pos < 0 or footer_pos < 0 or footer_pos <= divider_pos:
            return content

        header = content[:divider_pos]
        if "\n(job_id: " not in header:
            return content

        body_start = divider_pos + len(divider)
        body = content[body_start:footer_pos].strip()
        return body or content

    # -- 断开连接时的清理 ---------------------------------------------

    async def close(self) -> None:
        """释放聊天锁（目前为空操作；为未来清理预留）。"""
        self._chat_locks.clear()


class OutboundManager:
    """出站协调器，统筹发送、心跳和慢响应。

    组合：
      - MessageSender   —— 核心文本/媒体发送
      - HeartbeatManager —— 回复心跳（RUNNING / FINISH）生命周期
      - SlowResponseNotifier —— 延迟的“请稍候”通知

    YuanbaoAdapter 持有单个 ``_outbound: OutboundManager``，并通过它
    委托所有出站操作。
    """

    # 暴露 MessageSender 的类级常量，以保持向后兼容
    CHAT_DICT_MAX_SIZE: ClassVar[int] = MessageSender.CHAT_DICT_MAX_SIZE

    def __init__(self, adapter: "YuanbaoAdapter") -> None:
        self._adapter = adapter
        self.sender: MessageSender = MessageSender(adapter)
        self.heartbeat: HeartbeatManager = HeartbeatManager(adapter)
        self.slow_notifier: SlowResponseNotifier = SlowResponseNotifier(adapter, self.sender)

        # 将协调钩子接到 MessageSender
        self.sender._on_send_start = self._handle_send_start
        self.sender._on_send_finish = self._handle_send_finish

    # -- 协调钩子 ------------------------------------------------

    def _handle_send_start(self, chat_id: str) -> None:
        """由 MessageSender 在发送前调用：取消慢响应通知。"""
        self.slow_notifier.cancel(chat_id)

    async def _handle_send_finish(self, chat_id: str) -> None:
        """由 MessageSender 在发送后调用：发送 FINISH 心跳。"""
        await self.heartbeat.send_heartbeat_once(chat_id, WS_HEARTBEAT_FINISH)

    # -- 委托的公共 API（供 YuanbaoAdapter 使用） ---------------------

    async def send_text(
        self, chat_id: str, content: str, reply_to: Optional[str] = None,
        group_code: str = "",
    ) -> "SendResult":
        """发送文本消息，带自动分块。"""
        return await self.sender.send_text(chat_id, content, reply_to, group_code=group_code)

    async def send_media(
        self, chat_id: str, handler_name: str, **kwargs: Any,
    ) -> "SendResult":
        """将媒体发送分发给具名的 handler 策略。"""
        return await self.sender.send_media(chat_id, handler_name, **kwargs)

    async def send_direct(
        self, chat_id: str, message: str,
        media_files: Optional[List[Tuple[str, bool]]] = None,
    ) -> Dict[str, Any]:
        """发送文本 + 媒体（供 send_message 工具使用）。"""
        return await self.sender.send_direct(chat_id, message, media_files)

    async def start_typing(self, chat_id: str) -> None:
        """启动回复心跳（RUNNING）。"""
        await self.heartbeat.start(chat_id)

    async def stop_typing(self, chat_id: str, send_finish: bool = False) -> None:
        """停止回复心跳。"""
        await self.heartbeat.stop(chat_id, send_finish=send_finish)

    async def start_slow_notifier(self, chat_id: str) -> None:
        """启动慢响应通知。"""
        await self.slow_notifier.start(chat_id)

    def cancel_slow_notifier(self, chat_id: str) -> None:
        """取消慢响应通知。"""
        self.slow_notifier.cancel(chat_id)

    def get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        """代理到 MessageSender.get_chat_lock，以保持向后兼容。"""
        return self.sender.get_chat_lock(chat_id)

    @property
    def _chat_locks(self) -> collections.OrderedDict:
        """代理到 MessageSender._chat_locks，以保持向后兼容。"""
        return self.sender._chat_locks

    @staticmethod
    def validate_media(
        file_bytes: Optional[bytes], filename: str, max_size_mb: int = 20,
    ) -> Optional[str]:
        """代理到 MessageSender.validate_media。"""
        return MessageSender.validate_media(file_bytes, filename, max_size_mb)

    async def close(self) -> None:
        """关闭所有子管理器。"""
        await self.sender.close()
        await self.heartbeat.close()
        await self.slow_notifier.close()


class YuanbaoAdapter(BasePlatformAdapter):
    """基于持久 WebSocket 连接的元宝 AI Bot adapter。"""

    PLATFORM = Platform.YUANBAO
    MAX_TEXT_CHUNK: int = 4000  # 元宝单条消息字符数上限
    splits_long_messages = True  # send() 通过 truncate_message(MAX_TEXT_CHUNK) 自动分块
    MEDIA_MAX_SIZE_MB: int = 50  # 上传校验时的最大媒体文件大小（MB）
    REPLY_REF_MAX_ENTRIES: ClassVar[int] = 500  # 引用去重 dict 的最大容量

    # -- 活跃实例注册表（类级单例） -------------------

    _active_instance: ClassVar[Optional["YuanbaoAdapter"]] = None

    @classmethod
    def get_active(cls) -> Optional["YuanbaoAdapter"]:
        """返回当前已连接的 YuanbaoAdapter，或 None。"""
        return cls._active_instance

    @classmethod
    def set_active(cls, adapter: Optional["YuanbaoAdapter"]) -> None:
        """注册（或清除）活跃的 adapter 实例。"""
        cls._active_instance = adapter

    def __init__(self, config: PlatformConfig, **kwargs: Any) -> None:
        super().__init__(config, Platform.YUANBAO)

        # 来自 config.extra 的凭证/端点（由 config.py 从环境变量/yaml 填充）
        _extra = config.extra or {}
        self._app_key: str = (_extra.get("app_id") or "").strip()
        self._app_secret: str = (_extra.get("app_secret") or "").strip()
        self._bot_id: Optional[str] = _extra.get("bot_id") or None
        self._ws_url: str = (_extra.get("ws_url") or DEFAULT_WS_GATEWAY_URL).strip()
        self._api_domain: str = (_extra.get("api_domain") or DEFAULT_API_DOMAIN).rstrip("/")
        self._route_env: str = (_extra.get("route_env") or "").strip()

        # 核心管理器（UML 组合关系）
        self._connection: ConnectionManager = ConnectionManager(self)
        self._outbound: OutboundManager = OutboundManager(self)

        # 入站分发任务 —— 跟踪以便 disconnect() 能取消它们
        self._inbound_tasks: set[asyncio.Task] = set()

        # 后台任务集合 —— 防止 GC 回收即发即忘的任务
        self._background_tasks: set[asyncio.Task] = set()

        # 成员缓存：group_code -> (updated_ts, [{"user_id":..., "nickname":..., ...}, ...])
        # 由 get_group_member_list() 填充，供 @mention 解析使用。
        # 早于 MEMBER_CACHE_TTL_S 的条目视为过期。
        self._member_cache: Dict[str, Tuple[float, list]] = {}
        self.MEMBER_CACHE_TTL_S: float = 300.0  # 5 分钟

        # 入站消息去重（WS 重连 / 网络抖动）
        self._dedup = MessageDeduplicator(ttl_seconds=300)

        # 群聊串行分发队列（session_key → asyncio.Queue）。
        self._group_queues: Dict[str, asyncio.Queue] = {}

        # 撤回支持：按 session_key 跟踪正在处理的 msg_id，
        # 以便 RecallGuardMiddleware 检测“正在处理”的消息。
        self._processing_msg_ids: Dict[str, str] = {}
        self._processing_msg_texts: Dict[str, str] = {}
        # 有界的 msg_id → 归因内容缓存，用于最近的消息。
        # 当会话记录条目缺少 message_id 字段时（agent 处理过的 @bot 消息），
        # 由 _patch_transcript 作为内容匹配兜底使用。
        self._msg_content_cache: Dict[str, str] = {}

        # Reply-to 去重：inbound_msg_id -> expire_ts
        # ------------------------------------------------------------------
        # 访问控制策略（DM / Group）
        # ------------------------------------------------------------------
        dm_policy: str = (
            _extra.get("dm_policy")
            or os.getenv("YUANBAO_DM_POLICY", "open")
        ).strip().lower()

        _dm_allow_from_raw: str = (
            _extra.get("dm_allow_from")
            or os.getenv("YUANBAO_DM_ALLOW_FROM", "")
        )
        dm_allow_from: list[str] = [x.strip() for x in _dm_allow_from_raw.split(",") if x.strip()]

        group_policy: str = (
            _extra.get("group_policy")
            or os.getenv("YUANBAO_GROUP_POLICY", "open")
        ).strip().lower()

        _group_allow_from_raw: str = (
            _extra.get("group_allow_from")
            or os.getenv("YUANBAO_GROUP_ALLOW_FROM", "")
        )
        group_allow_from: list[str] = [x.strip() for x in _group_allow_from_raw.split(",") if x.strip()]

        self._access_policy = AccessPolicy(
            dm_policy=dm_policy,
            dm_allow_from=dm_allow_from,
            group_policy=group_policy,
            group_allow_from=group_allow_from,
        )

        # 群查询服务（AI 工具后端）
        self._group_query = GroupQueryService(self)

        # 入站消息处理管道（中间件模式）
        self._inbound_pipeline: InboundPipeline = InboundPipelineBuilder.build()

        # ------------------------------------------------------------------
        # Auto-sethome：第一个给 bot 发消息的用户成为拥有者。
        # 若未配置 home channel，第一个会话会被自动设为 home channel。
        # 当现有 home channel 是群聊（group:xxx）时，它仍可被升级 ——
        # 第一个 DM 会用 direct:xxx 覆盖它。
        # ------------------------------------------------------------------
        _existing_home = os.getenv("YUANBAO_HOME_CHANNEL") or (
            config.home_channel.chat_id if config.home_channel else ""
        )
        self._auto_sethome_done: bool = bool(_existing_home) and not _existing_home.startswith("group:")

    # ------------------------------------------------------------------
    # 任务跟踪辅助
    # ------------------------------------------------------------------

    def _track_task(self, task: asyncio.Task) -> asyncio.Task:
        """注册一个即发即忘的任务，以免被 GC 过早回收。"""
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    # ------------------------------------------------------------------
    # 抽象方法实现
    # ------------------------------------------------------------------

    @property
    def enforces_own_access_policy(self) -> bool:
        """元宝在入口处通过 dm_policy/group_policy 控制 DM/group 访问。"""
        return True

    async def connect(self) -> bool:
        """连接元宝 WS 网关并进行鉴权。

        委托给 ConnectionManager.open()。
        """
        return await self._connection.open()

    async def disconnect(self) -> None:
        """取消后台任务并关闭 WebSocket 连接。"""
        if YuanbaoAdapter._active_instance is self:
            YuanbaoAdapter.set_active(None)

        self._running = False
        self._mark_disconnected()
        self._release_platform_lock()

        # 委托给各管理器
        await self._connection.close()
        await self._outbound.close()

        # 取消所有进行中的入站分发任务
        for task in list(self._inbound_tasks):
            if not task.done():
                task.cancel()
        self._inbound_tasks.clear()

        self._group_queues.clear()

        logger.info("[%s] Disconnected", self.name)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        group_code: str = "",
    ) -> SendResult:
        """发送文本消息，带自动分块。委托给 OutboundManager。"""
        return await self._outbound.send_text(chat_id, content, reply_to, group_code=group_code)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """返回由 chat_id 前缀推导的基础聊天元数据。

        chat_id 约定：
          "group:<group_code>"  → 群聊
          "direct:<account>"   → C2C / 私聊（默认）

        TODO (T06)：从元宝 API 获取真实的聊天名/成员数。
        """
        if chat_id.startswith("group:"):
            return {"name": chat_id, "type": "group"}
        return {"name": chat_id, "type": "dm"}

    async def send_typing(self, chat_id: str, metadata: Optional[dict] = None) -> None:
        """发送 "typing" 状态心跳（RUNNING）。委托给 OutboundManager。"""
        try:
            await self._outbound.start_typing(chat_id)
        except Exception:
            pass

    async def stop_typing(self, chat_id: str) -> None:
        """停止 RUNNING 心跳循环，但不立即发送 FINISH。

        FINISH 由 send() 在消息实际送达后发送，以保证正确顺序：
        RUNNING... -> 消息到达 -> FINISH。
        """
        try:
            await self._outbound.stop_typing(chat_id, send_finish=False)
        except Exception:
            pass

    async def _process_message_background(self, event, session_key: str) -> None:
        """用慢响应通知包装基类的消息处理。"""
        chat_id = event.source.chat_id
        await self._outbound.start_slow_notifier(chat_id)
        try:
            await super()._process_message_background(event, session_key)
        finally:
            self._outbound.cancel_slow_notifier(chat_id)

    # ------------------------------------------------------------------
    # 群查询（委托给 GroupQueryService）
    # ------------------------------------------------------------------

    async def query_group_info(self, group_code: str) -> Optional[dict]:
        """查询群信息（委托给 GroupQueryService）。"""
        return await self._group_query.query_group_info_raw(group_code)

    async def get_group_member_list(
        self, group_code: str, offset: int = 0, limit: int = 200
    ) -> Optional[dict]:
        """查询群成员列表（委托给 GroupQueryService）。"""
        return await self._group_query.get_group_member_list_raw(group_code, offset=offset, limit=limit)

    # ------------------------------------------------------------------
    # DM 主动私聊 + 访问控制
    # ------------------------------------------------------------------

    DM_MAX_CHARS = 10000  # DM 文本上限

    async def send_dm(self, user_id: str, text: str, group_code: str = "") -> SendResult:
        """
        主动发送 C2C 私聊消息。

        参数：
            user_id: 目标用户 ID
            text: 消息文本（上限 10000 字符）
            group_code: 来源群 code（用于群发起的 DM 上下文）

        返回：
            SendResult
        """
        if not self._access_policy.is_dm_allowed(user_id):
            return SendResult(success=False, error="DM access denied for this user")
        if len(text) > self.DM_MAX_CHARS:
            text = text[:self.DM_MAX_CHARS] + "\n...(truncated)"
        chat_id = f"direct:{user_id}"
        return await self.send(chat_id, text, group_code=group_code)

    # ------------------------------------------------------------------
    # 媒体发送方法
    # ------------------------------------------------------------------

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
        **kwargs: Any,
    ) -> SendResult:
        """发送图片消息（URL）。通过 ImageUrlHandler 委托给 OutboundManager。"""
        return await self._outbound.send_media(
            chat_id, "image_url",
            reply_to=reply_to, caption=caption, image_url=image_url,
            **kwargs,
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
        **kwargs: Any,
    ) -> SendResult:
        """发送本地图片文件。通过 ImageFileHandler 委托给 OutboundManager。"""
        return await self._outbound.send_media(
            chat_id, "image_file",
            reply_to=reply_to, caption=caption, image_path=image_path,
            **kwargs,
        )

    async def send_file(
        self,
        chat_id: str,
        file_url: str,
        filename: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
        **kwargs: Any,
    ) -> SendResult:
        """发送文件消息（URL）。通过 FileUrlHandler 委托给 OutboundManager。"""
        return await self._outbound.send_media(
            chat_id, "file_url",
            reply_to=reply_to, file_url=file_url, filename=filename,
            **kwargs,
        )

    async def send_sticker(
        self,
        chat_id: str,
        sticker_name: Optional[str] = None,
        face_index: Optional[int] = None,
        reply_to: Optional[str] = None,
        **kwargs: Any,
    ) -> SendResult:
        """发送贴纸/表情。通过 StickerHandler 委托给 OutboundManager。"""
        return await self._outbound.send_media(
            chat_id, "sticker",
            reply_to=reply_to,
            sticker_name=sticker_name, face_index=face_index,
            **kwargs,
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        filename: Optional[str] = None,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
        **kwargs: Any,
    ) -> SendResult:
        """发送本地文件（文档）。通过 DocumentHandler 委托给 OutboundManager。"""
        return await self._outbound.send_media(
            chat_id, "document",
            reply_to=reply_to, caption=caption,
            file_path=file_path, filename=filename,
            **kwargs,
        )

    async def _get_cached_token(self) -> dict:
        """获取当前有效的 sign token（使用模块级缓存）。"""
        return await SignManager.get_token(
            self._app_key, self._app_secret, self._api_domain,
            route_env=self._route_env,
        )

    def get_status(self) -> dict:
        """返回当前连接状态的快照。"""
        conn = self._connection
        return {
            "connected": conn.is_connected,
            "bot_id": self._bot_id,
            "connect_id": conn.connect_id,
            "reconnect_attempts": conn.reconnect_attempts,
            "ws_url": self._ws_url,
        }


# ---------------------------------------------------------------------------
# 模块级轻量委托（为外部调用方保持导入兼容性）
# ---------------------------------------------------------------------------


def get_active_adapter() -> Optional["YuanbaoAdapter"]:
    """委托给 ``YuanbaoAdapter.get_active()``。"""
    return YuanbaoAdapter.get_active()


async def send_yuanbao_direct(
    adapter: "YuanbaoAdapter",
    chat_id: str,
    message: str,
    media_files: Optional[List[Tuple[str, bool]]] = None,
) -> Dict[str, Any]:
    """委托给 ``OutboundManager.send_direct``。"""
    return await adapter._outbound.send_direct(chat_id, message, media_files)
