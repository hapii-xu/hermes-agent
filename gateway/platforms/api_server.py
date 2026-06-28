"""
OpenAI 兼容的 API 服务器平台适配器。

暴露一个 HTTP 服务器，提供以下端点：
- POST /v1/chat/completions        — OpenAI Chat Completions 格式（无状态；可通过 X-Hermes-Session-Id 头部选择性启用会话连续性；可通过 X-Hermes-Session-Key 头部选择性启用长期记忆作用域）
- POST /v1/responses               — OpenAI Responses API 格式（通过 previous_response_id 保持状态；支持 X-Hermes-Session-Key）
- GET  /v1/responses/{response_id} — 获取已存储的响应
- DELETE /v1/responses/{response_id} — 删除已存储的响应
- GET  /v1/models                  — 将 hermes-agent 列为可用模型
- GET  /v1/capabilities            — 供外部 UI 使用的机器可读 API 能力描述
- GET  /api/sessions               — 列出客户端可见的 Hermes 会话
- POST /api/sessions               — 创建一个空的 Hermes 会话
- GET/PATCH/DELETE /api/sessions/{session_id} — 读取/更新/删除会话
- GET  /api/sessions/{session_id}/messages — 读取会话消息历史
- POST /api/sessions/{session_id}/fork — 使用 SessionDB 血缘关系分叉会话
- POST /api/sessions/{session_id}/chat[/stream] — 与已持久化的会话进行聊天
- POST /v1/runs                    — 启动一个 run，立即返回 run_id（202）
- GET  /v1/runs/{run_id}           — 获取当前 run 状态
- GET  /v1/runs/{run_id}/events    — 结构化生命周期事件的 SSE 流
- POST /v1/runs/{run_id}/approval — 解决一个待处理的 run 审批
- POST /v1/runs/{run_id}/stop       — 中断正在运行的 agent
- GET  /health                     — 健康检查
- GET  /health/detailed            — 用于跨容器仪表盘探测的丰富状态信息

任何 OpenAI 兼容的前端（Open WebUI、LobeChat、LibreChat、
AnythingLLM、NextChat、ChatBox 等）都可以通过将地址指向
http://localhost:8642/v1 并使用 API_SERVER_KEY 进行认证，
从而经由本适配器连接到 hermes-agent。

依赖：
- aiohttp（已在 gateway 中可用）
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import socket as _socket
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    is_network_accessible,
)

logger = logging.getLogger(__name__)


def _hermes_version() -> str:
    """返回 hermes-agent 的版本字符串，无法解析时返回 "dev"。

    首先尝试读取已安装的包元数据（对于 pip/uv 安装是权威来源），
    然后回退到树内的 ``hermes_cli.__version__``（覆盖可编辑安装 /
    源码检出场景，此时元数据可能已过期或缺失）。永不抛出异常 ——
    版本探测绝不能破坏健康检查端点。
    """
    try:
        from importlib.metadata import version

        return version("hermes-agent")
    except Exception:
        pass
    try:
        from hermes_cli import __version__

        return __version__
    except Exception:
        return "dev"


# 默认设置
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8642
MAX_STORED_RESPONSES = 100
MAX_REQUEST_BYTES = 10_000_000  # 10 MB —— 容纳带有工具调用的较长 agent 对话
CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS = 30.0
MAX_NORMALIZED_TEXT_LENGTH = 65_536  # 规范化内容部分的 64 KB 上限
MAX_CONTENT_LIST_SIZE = 1_000  # content 为数组时的最大条目数


def _coerce_port(value: Any, default: int = DEFAULT_PORT) -> int:
    """解析监听端口，避免格式错误的 env/config 值导致启动崩溃。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_TRUE_REQUEST_BOOL_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_REQUEST_BOOL_STRINGS = frozenset({"0", "false", "no", "off"})


def _coerce_request_bool(value: Any, default: bool = False) -> bool:
    """将布尔值形式的 API 负载值规范化。

    外部客户端应当发送真正的 JSON 布尔值，但一些 OpenAI 兼容的前端
    和中间件会将 ``stream`` 之类的标志序列化为字符串。对这些值使用
    Python 的真值判断会导致请求被错误路由，因为 ``"false"`` 仍然为真。
    仅将显式的布尔式标量当作布尔值处理；其余情况一律回退到调用方提供的默认值。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_REQUEST_BOOL_STRINGS:
            return True
        if normalized in _FALSE_REQUEST_BOOL_STRINGS:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _normalize_chat_content(
    content: Any, *, _max_depth: int = 10, _depth: int = 0,
) -> str:
    """将 OpenAI 聊天消息内容规范化为纯文本字符串。

    一些客户端（Open WebUI、LobeChat 等）会将 content 以带类型的
    数组形式发送，而非纯字符串::

        [{"type": "text", "text": "hello"}, {"type": "input_text", "text": "..."}]

    本函数将这些内容拍平为单个字符串，以免 agent 流水线（其期望字符串）
    出现解析失败。

    防御性限制可防止滥用：递归深度、列表大小和输出长度均有上限。
    """
    if _depth > _max_depth:
        return ""
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:MAX_NORMALIZED_TEXT_LENGTH] if len(content) > MAX_NORMALIZED_TEXT_LENGTH else content

    if isinstance(content, list):
        parts: List[str] = []
        total_len = 0
        items = content[:MAX_CONTENT_LIST_SIZE] if len(content) > MAX_CONTENT_LIST_SIZE else content
        for item in items:
            if isinstance(item, str):
                if item:
                    part = item[:MAX_NORMALIZED_TEXT_LENGTH]
                    parts.append(part)
                    total_len += len(part)
            elif isinstance(item, dict):
                item_type = str(item.get("type") or "").strip().lower()
                if item_type in {"text", "input_text", "output_text"}:
                    text = item.get("text", "")
                    if text:
                        try:
                            part = str(text)[:MAX_NORMALIZED_TEXT_LENGTH]
                            parts.append(part)
                            total_len += len(part)
                        except Exception:
                            pass
                # 静默跳过 image_url 及其他非文本部分
            elif isinstance(item, list):
                nested = _normalize_chat_content(item, _max_depth=_max_depth, _depth=_depth + 1)
                if nested:
                    parts.append(nested)
                    total_len += len(nested)
            # 检查累积大小
            if total_len >= MAX_NORMALIZED_TEXT_LENGTH:
                break
        result = "\n".join(parts)
        return result[:MAX_NORMALIZED_TEXT_LENGTH] if len(result) > MAX_NORMALIZED_TEXT_LENGTH else result

    # 针对意外类型（int、float、bool 等）的回退处理
    try:
        result = str(content)
        return result[:MAX_NORMALIZED_TEXT_LENGTH] if len(result) > MAX_NORMALIZED_TEXT_LENGTH else result
    except Exception:
        return ""


# OpenAI Chat Completions 和 Responses API 使用的内容部分类型别名。
# 我们在输入时同时接受两种写法，并输出单一的规范化内部形态
# （``{"type": "text", ...}`` / ``{"type": "image_url", ...}``），
# 该形态已被 agent 流水线的其余部分所理解。
_TEXT_PART_TYPES = frozenset({"text", "input_text", "output_text"})
_IMAGE_PART_TYPES = frozenset({"image_url", "input_image"})
_FILE_PART_TYPES = frozenset({"file", "input_file"})


def _normalize_multimodal_content(content: Any) -> Any:
    """为 API 服务器校验并规范化多模态内容。

    当内容仅包含文本时返回纯字符串；当存在图片时返回
    ``{"type": "text"|"image_url", ...}`` 部分的列表。
    输出形态是原生的 OpenAI Chat Completions 视觉格式，
    agent 流水线可以直接接受（OpenAI 线上协议的 provider）或
    进行转换（Anthropic 的 ``_preprocess_anthropic_content``）。

    当输入无效时抛出带有 OpenAI 风格 code 的 ``ValueError``：
      * ``unsupported_content_type`` — file/input_file/file_id 部分，
        或非图片的 ``data:`` URL。
      * ``invalid_image_url`` — 缺少 URL 或不支持的协议。
      * ``invalid_content_part`` — 格式错误的 text/image 对象。

    调用方负责将 ValueError 转换为 400 响应。
    """
    # 标量直接透传，与 ``_normalize_chat_content`` 保持一致。
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:MAX_NORMALIZED_TEXT_LENGTH] if len(content) > MAX_NORMALIZED_TEXT_LENGTH else content
    if not isinstance(content, list):
        # 与旧版文本规范化器的回退保持一致，这样在图片支持出现之前
        # 就存在的调用方仍然能拿到字符串。
        return _normalize_chat_content(content)

    items = content[:MAX_CONTENT_LIST_SIZE] if len(content) > MAX_CONTENT_LIST_SIZE else content
    normalized_parts: List[Dict[str, Any]] = []
    text_accum_len = 0

    for part in items:
        if isinstance(part, str):
            if part:
                trimmed = part[:MAX_NORMALIZED_TEXT_LENGTH]
                normalized_parts.append({"type": "text", "text": trimmed})
                text_accum_len += len(trimmed)
            continue

        if not isinstance(part, dict):
            # 忽略未知标量，以便向前兼容未来 Responses API 的新增内容
            # （例如 ``refusal``）。这与文本规范化器采用相同策略。
            continue

        raw_type = part.get("type")
        part_type = str(raw_type or "").strip().lower()

        if part_type in _TEXT_PART_TYPES:
            text = part.get("text")
            if text is None:
                continue
            if not isinstance(text, str):
                text = str(text)
            if text:
                trimmed = text[:MAX_NORMALIZED_TEXT_LENGTH]
                normalized_parts.append({"type": "text", "text": trimmed})
                text_accum_len += len(trimmed)
            continue

        if part_type in _IMAGE_PART_TYPES:
            detail = part.get("detail")
            image_ref = part.get("image_url")
            # OpenAI Responses 在 ``input_image`` 中发送顶层 ``image_url``
            # 字符串；Chat Completions 则将 ``image_url`` 发送为
            # ``{"url": "...", "detail": "..."}``。两种写法都支持。
            if isinstance(image_ref, dict):
                url_value = image_ref.get("url")
                detail = image_ref.get("detail", detail)
            else:
                url_value = image_ref
            if not isinstance(url_value, str) or not url_value.strip():
                raise ValueError("invalid_image_url:Image parts must include a non-empty image URL.")
            url_value = url_value.strip()
            lowered = url_value.lower()
            if lowered.startswith("data:"):
                if not lowered.startswith("data:image/") or "," not in url_value:
                    raise ValueError(
                        "unsupported_content_type:Only image data URLs are supported. "
                        "Non-image data payloads are not supported."
                    )
            elif not (lowered.startswith("http://") or lowered.startswith("https://")):
                raise ValueError(
                    "invalid_image_url:Image inputs must use http(s) URLs or data:image/... URLs."
                )
            image_part: Dict[str, Any] = {"type": "image_url", "image_url": {"url": url_value}}
            if detail is not None:
                if not isinstance(detail, str) or not detail.strip():
                    raise ValueError("invalid_content_part:Image detail must be a non-empty string when provided.")
                image_part["image_url"]["detail"] = detail.strip()
            normalized_parts.append(image_part)
            continue

        if part_type in _FILE_PART_TYPES:
            raise ValueError(
                "unsupported_content_type:Inline image inputs are supported, "
                "but uploaded files and document inputs are not supported on this endpoint."
            )

        # 未知部分类型 —— 显式拒绝，以便客户端收到清晰错误，
        # 而不是该轮对话被静默丢弃。
        raise ValueError(
            f"unsupported_content_type:Unsupported content part type {raw_type!r}. "
            "Only text and image_url/input_image parts are supported."
        )

    if not normalized_parts:
        return ""

    # 仅文本：折叠为纯字符串，以便下游的日志/轨迹代码看到原生形态，
    # 且不影响纯文本轮次的 prompt 缓存。
    if all(p.get("type") == "text" for p in normalized_parts):
        return "\n".join(p["text"] for p in normalized_parts if p.get("text"))

    return normalized_parts


def _content_has_visible_payload(content: Any) -> bool:
    """当 content 含有任何文本或图片附件时返回 True。用于拒绝空轮对话。"""
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                ptype = str(part.get("type") or "").strip().lower()
                if ptype in _TEXT_PART_TYPES and str(part.get("text") or "").strip():
                    return True
                if ptype in _IMAGE_PART_TYPES:
                    return True
    return False


def _multimodal_validation_error(exc: ValueError, *, param: str) -> "web.Response":
    """将 ``_normalize_multimodal_content`` 抛出的 ValueError 转换为 400 响应。"""
    raw = str(exc)
    code, _, message = raw.partition(":")
    if not message:
        code, message = "invalid_content_part", raw
    return web.json_response(
        _openai_error(message, code=code, param=param),
        status=400,
    )


def _session_chat_user_message(body: Dict[str, Any], *, param: str = "message") -> tuple[Any, Optional["web.Response"]]:
    """像 chat completions 一样解析并规范化 session chat 的 ``message`` / ``input``。"""
    user_message = body.get("message") or body.get("input")
    if not _content_has_visible_payload(user_message):
        return None, web.json_response(
            _openai_error("Missing 'message' field", code="missing_message"),
            status=400,
        )
    try:
        return _normalize_multimodal_content(user_message), None
    except ValueError as exc:
        return None, _multimodal_validation_error(exc, param=param)


def check_api_server_requirements() -> bool:
    """检查 API 服务器的依赖是否可用。"""
    return AIOHTTP_AVAILABLE


class ResponseStore:
    """
    基于 SQLite 的 LRU 存储，用于 Responses API 状态。

    每个存储的响应都包含完整的内部对话历史（含工具调用及其结果），
    以便后续请求通过 previous_response_id 重建对话。

    跨 gateway 重启持久化。当磁盘路径不可用时，回退到内存中的 SQLite。
    """

    def __init__(self, max_size: int = MAX_STORED_RESPONSES, db_path: str = None):
        self._max_size = max_size
        if db_path is None:
            try:
                from hermes_cli.config import get_hermes_home
                db_path = str(get_hermes_home() / "response_store.db")
            except Exception:
                db_path = ":memory:"
        self._db_path: Optional[str] = db_path if db_path != ":memory:" else None
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
        except Exception:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._db_path = None
        # 使用共享的 WAL 回退辅助函数，使 response_store.db 在挂载于
        # NFS/SMB/FUSE 的 HERMES_HOME 上能够优雅降级（这与 state.db/kanban.db
        # 所解决的同一类文件系统问题 —— 参见
        # hermes_state._WAL_INCOMPAT_MARKERS）。
        from hermes_state import apply_wal_with_fallback
        apply_wal_with_fallback(self._conn, db_label="response_store.db")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS responses (
                response_id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                accessed_at REAL NOT NULL
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS conversations (
                name TEXT PRIMARY KEY,
                response_id TEXT NOT NULL
            )"""
        )
        self._conn.commit()
        # response_store.db 包含对话历史（工具负载、prompt、结果）。
        # 创建后收紧为仅属主可访问，避免共享机器上的其他本地用户读取。
        # 在 __init__ 时执行一次即可，而不是每次 commit 后都执行 ——
        # 在热路径上对每次写入都做 chmod 是浪费系统调用。
        self._tighten_file_permissions()

    def _tighten_file_permissions(self) -> None:
        """强制 DB 及 SQLite 附属文件使用仅属主可访问的权限。"""
        if not self._db_path:
            return
        for candidate in (
            Path(self._db_path),
            Path(f"{self._db_path}-wal"),
            Path(f"{self._db_path}-shm"),
        ):
            try:
                if candidate.exists():
                    candidate.chmod(0o600)
            except OSError:
                logger.debug(
                    "Failed to restrict response store permissions for %s",
                    candidate,
                    exc_info=True,
                )

    def get(self, response_id: str) -> Optional[Dict[str, Any]]:
        """根据 ID 获取已存储的响应（会更新访问时间以用于 LRU）。"""
        row = self._conn.execute(
            "SELECT data FROM responses WHERE response_id = ?", (response_id,)
        ).fetchone()
        if row is None:
            return None
        self._conn.execute(
            "UPDATE responses SET accessed_at = ? WHERE response_id = ?",
            (time.time(), response_id),
        )
        self._conn.commit()
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "Corrupted JSON in response store for id=%s, evicting entry",
                response_id,
            )
            self._conn.execute(
                "DELETE FROM responses WHERE response_id = ?",
                (response_id,),
            )
            self._conn.commit()
            return None

    def put(self, response_id: str, data: Dict[str, Any]) -> None:
        """存储一个响应，容量已满时淘汰最旧的条目。"""
        self._conn.execute(
            "INSERT OR REPLACE INTO responses (response_id, data, accessed_at) VALUES (?, ?, ?)",
            (response_id, json.dumps(data, default=str), time.time()),
        )
        # 淘汰超出 max_size 的最旧条目
        count = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
        if count > self._max_size:
            # 收集将要被淘汰的 ID
            evict_ids = [
                row[0]
                for row in self._conn.execute(
                    "SELECT response_id FROM responses ORDER BY accessed_at ASC LIMIT ?",
                    (count - self._max_size,),
                ).fetchall()
            ]
            if evict_ids:
                placeholders = ",".join("?" for _ in evict_ids)
                # 清理指向被淘汰响应的会话映射
                self._conn.execute(
                    f"DELETE FROM conversations WHERE response_id IN ({placeholders})",
                    evict_ids,
                )
                # 删除被淘汰的响应
                self._conn.execute(
                    f"DELETE FROM responses WHERE response_id IN ({placeholders})",
                    evict_ids,
                )
        self._conn.commit()

    def delete(self, response_id: str) -> bool:
        """从存储中移除一个响应。找到并删除则返回 True。"""
        # 清理指向该响应的会话映射
        self._conn.execute(
            "DELETE FROM conversations WHERE response_id = ?", (response_id,)
        )
        cursor = self._conn.execute(
            "DELETE FROM responses WHERE response_id = ?", (response_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get_conversation(self, name: str) -> Optional[str]:
        """获取某个会话名称对应的最新 response_id。"""
        row = self._conn.execute(
            "SELECT response_id FROM conversations WHERE name = ?", (name,)
        ).fetchone()
        return row[0] if row else None

    def set_conversation(self, name: str, response_id: str) -> None:
        """将一个会话名称映射到其最新的 response_id。"""
        self._conn.execute(
            "INSERT OR REPLACE INTO conversations (name, response_id) VALUES (?, ?)",
            (name, response_id),
        )
        self._conn.commit()

    def close(self) -> None:
        """关闭数据库连接。"""
        try:
            self._conn.close()
        except Exception:
            pass

    def __len__(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()
        return row[0] if row else 0


# ---------------------------------------------------------------------------
# CORS 中间件
# ---------------------------------------------------------------------------

_CORS_HEADERS = {
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Idempotency-Key",
}


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def cors_middleware(request, handler):
        """为显式允许的来源添加 CORS 头部；处理 OPTIONS 预检请求。"""
        adapter = request.app.get("api_server_adapter")
        origin = request.headers.get("Origin", "")
        cors_headers = None
        if adapter is not None:
            if not adapter._origin_allowed(origin):
                return web.Response(status=403)
            cors_headers = adapter._cors_headers_for_origin(origin)

        if request.method == "OPTIONS":
            if cors_headers is None:
                return web.Response(status=403)
            return web.Response(status=200, headers=cors_headers)

        response = await handler(request)
        if cors_headers is not None:
            response.headers.update(cors_headers)
        return response
else:
    cors_middleware = None  # type: ignore[assignment]


def _openai_error(message: str, err_type: str = "invalid_request_error", param: str = None, code: str = None) -> Dict[str, Any]:
    """OpenAI 风格的错误信封。"""
    return {
        "error": {
            "message": message,
            "type": err_type,
            "param": param,
            "code": code,
        }
    }


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def body_limit_middleware(request, handler):
        """根据 Content-Length 尽早拒绝过大的请求体。"""
        if request.method in {"POST", "PUT", "PATCH"}:
            cl = request.headers.get("Content-Length")
            if cl is not None:
                try:
                    if int(cl) > MAX_REQUEST_BYTES:
                        return web.json_response(_openai_error("Request body too large.", code="body_too_large"), status=413)
                except ValueError:
                    return web.json_response(_openai_error("Invalid Content-Length header.", code="invalid_content_length"), status=400)
        return await handler(request)
else:
    body_limit_middleware = None  # type: ignore[assignment]

_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "0",
    "Referrer-Policy": "no-referrer",
}


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def security_headers_middleware(request, handler):
        """为所有响应（包括错误响应）添加安全头部。"""
        response = await handler(request)
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response
else:
    security_headers_middleware = None  # type: ignore[assignment]


class _IdempotencyCache:
    """带 TTL 和基本 LRU 语义的内存幂等缓存。"""
    def __init__(self, max_items: int = 1000, ttl_seconds: int = 300):
        from collections import OrderedDict
        self._store = OrderedDict()
        self._inflight: Dict[tuple[str, str], "asyncio.Task[Any]"] = {}
        self._ttl = ttl_seconds
        self._max = max_items

    def _purge(self):
        now = time.time()
        expired = [k for k, v in self._store.items() if now - v["ts"] > self._ttl]
        for k in expired:
            self._store.pop(k, None)
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    async def get_or_set(self, key: str, fingerprint: str, compute_coro):
        self._purge()
        item = self._store.get(key)
        if item and item["fp"] == fingerprint:
            return item["resp"]

        inflight_key = (key, fingerprint)
        task = self._inflight.get(inflight_key)
        if task is None:
            async def _compute_and_store():
                resp = await compute_coro()
                import time as _t
                self._store[key] = {"resp": resp, "fp": fingerprint, "ts": _t.time()}
                self._purge()
                return resp

            task = asyncio.create_task(_compute_and_store())
            self._inflight[inflight_key] = task

            def _clear_inflight(done_task: "asyncio.Task[Any]") -> None:
                if self._inflight.get(inflight_key) is done_task:
                    self._inflight.pop(inflight_key, None)

            task.add_done_callback(_clear_inflight)

        return await asyncio.shield(task)


_idem_cache = _IdempotencyCache()


def _make_request_fingerprint(body: Dict[str, Any], keys: List[str]) -> str:
    from hashlib import sha256
    subset = {k: body.get(k) for k in keys}
    return sha256(repr(subset).encode("utf-8")).hexdigest()


def _derive_chat_session_id(
    system_prompt: Optional[str],
    first_user_message: str,
) -> str:
    """从对话的首条用户消息推导出一个稳定的 session ID。

    OpenAI 兼容的前端（Open WebUI、LibreChat 等）在每次请求时都会发送
    完整的对话历史。系统 prompt 和首条用户消息在同一对话的所有轮次中
    都保持不变，因此对它们做哈希可以产生确定性的 session ID，使 API
    服务器能在多轮之间复用同一个 Hermes session（从而复用同一个 Docker
    容器沙箱目录）。
    """
    seed = f"{system_prompt or ''}\n{first_user_message}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"api-{digest}"


_CRON_AVAILABLE = False
try:
    from cron.jobs import (
        list_jobs as _cron_list,
        get_job as _cron_get,
        create_job as _cron_create,
        update_job as _cron_update,
        remove_job as _cron_remove,
        pause_job as _cron_pause,
        resume_job as _cron_resume,
        trigger_job as _cron_trigger,
    )
    _CRON_AVAILABLE = True
except ImportError:
    _cron_list = None
    _cron_get = None
    _cron_create = None
    _cron_update = None
    _cron_remove = None
    _cron_pause = None
    _cron_resume = None
    _cron_trigger = None


def _notify_cron_provider_jobs_changed() -> None:
    """在 REST 变更之后通知当前活跃的 cron 调度器 provider 任务集合已变化
    （对内置 provider 是空操作）。尽力而为 —— 绝不会破坏 handler。"""
    try:
        from cron.scheduler import _notify_provider_jobs_changed
        _notify_provider_jobs_changed()
    except Exception:
        pass

# 纵深防御：镜像 agent 侧的 cronjob 工具，在创建/更新时扫描用户提供的
# prompt 中的数据外泄/注入负载（tools/cronjob_tools.py）。REST cron 端点
# 都是经过鉴权的（每个 handler 都会运行 _check_auth，且 connect() 在没有
# API_SERVER_KEY 时拒绝启动），所以这里不是信任边界 —— 它只是与工具路径
# 保持对齐，使得无论从哪个入口创建任务，恶意 prompt 都会被同样地拒绝。
# 防御性导入：缺少扫描器绝不能让 cron REST API 不可用。
try:
    from tools.cronjob_tools import _scan_cron_prompt as _scan_cron_prompt
except Exception:  # pragma: no cover - 扫描器是可选的加固
    _scan_cron_prompt = None


class APIServerAdapter(BasePlatformAdapter):
    """
    OpenAI 兼容的 HTTP API 服务器适配器。

    运行一个 aiohttp web 服务器，接收 OpenAI 格式的请求，并通过
    hermes-agent 的 AIAgent 进行路由处理。
    """

    # 无状态请求/响应：每个路由（OpenAI 规范的 /v1/chat/completions 和
    # /v1/responses，以及自有的 /v1/runs SSE 流）都在一轮结束时拆除其
    # channel。不存在持久的出站 channel 向一个已经收到响应的客户端推送
    # 后台补全结果，且 ``send()`` 是一个空操作 stub。因此异步投递类工具
    # （终端的 notify_on_complete / watch_patterns、delegate_task 的
    # background=True）绝不能承诺在此路径上投递 —— 参见
    # ``async_delivery_supported()``。
    supports_async_delivery: bool = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.API_SERVER)
        extra = config.extra or {}
        self._host: str = extra.get("host", os.getenv("API_SERVER_HOST", DEFAULT_HOST))
        raw_port = extra.get("port")
        if raw_port is None:
            raw_port = os.getenv("API_SERVER_PORT", str(DEFAULT_PORT))
        self._port: int = _coerce_port(raw_port, DEFAULT_PORT)
        self._api_key: str = extra.get("key", os.getenv("API_SERVER_KEY", ""))
        self._cors_origins: tuple[str, ...] = self._parse_cors_origins(
            extra.get("cors_origins", os.getenv("API_SERVER_CORS_ORIGINS", "")),
        )
        self._model_name: str = self._resolve_model_name(
            extra.get("model_name", os.getenv("API_SERVER_MODEL_NAME", "")),
        )
        self._app: Optional["web.Application"] = None
        self._runner: Optional["web.AppRunner"] = None
        self._site: Optional["web.TCPSite"] = None
        self._response_store = ResponseStore()
        # 活跃的 run 流：run_id -> asyncio.Queue of SSE event dicts
        self._run_streams: Dict[str, "asyncio.Queue[Optional[Dict]]"] = {}
        # 用于孤立 run TTL 清扫的创建时间戳
        self._run_streams_created: Dict[str, float] = {}
        # 用于支持 stop 的活跃 run agent/task 引用
        self._active_run_agents: Dict[str, Any] = {}
        self._active_run_tasks: Dict[str, "asyncio.Task"] = {}
        # 供 dashboard 和外部控制面 UI 轮询的 run 状态。
        self._run_statuses: Dict[str, Dict[str, Any]] = {}
        # 每个 run_id 对应的活跃审批 session key。审批核心按 session key
        # 解析请求，而 API 客户端则按 run_id 寻址在途的 run。
        self._run_approval_sessions: Dict[str, str] = {}
        self._session_db: Optional[Any] = None  # 为会话连续性惰性初始化的 SessionDB
        # 跨所有 agent 服务端点（/v1/chat/completions、/v1/responses、
        # /v1/runs）共享的并发上限。从 config.yaml 的
        # gateway.api_server.max_concurrent_runs 读取；0 表示禁用上限。
        # 限制请求洪流导致的 CPU / 内存 / 上游 LLM 配额耗尽（#7483）。
        self._max_concurrent_runs: int = self._resolve_max_concurrent_runs()
        # 非流式 chat/responses 路径上在途 run 的数量
        # （/v1/runs 路径通过 _run_streams 自行跟踪其在途集合）。
        self._inflight_agent_runs: int = 0

    @staticmethod
    def _parse_cors_origins(value: Any) -> tuple[str, ...]:
        """将配置的 CORS 来源规范化为稳定的 tuple。"""
        if not value:
            return ()

        if isinstance(value, str):
            items = value.split(",")
        elif isinstance(value, (list, tuple, set)):
            items = value
        else:
            items = [str(value)]

        return tuple(str(item).strip() for item in items if str(item).strip())

    @staticmethod
    def _resolve_max_concurrent_runs() -> int:
        """从 config.yaml 读取并发 run 上限（0 表示禁用）。

        gateway.api_server.max_concurrent_runs。未设置或格式错误时回退到
        历史默认值 10。负值会被钳制到 0（禁用）。
        """
        default = 10
        try:
            from hermes_cli.config import cfg_get, load_config

            raw = cfg_get(
                load_config(),
                "gateway",
                "api_server",
                "max_concurrent_runs",
                default=default,
            )
            value = int(raw)
        except Exception:
            return default
        return max(0, value)

    @staticmethod
    def _resolve_model_name(explicit: str) -> str:
        """推导用于 /v1/models 的对外模型名称。

        优先级：
        1. 显式覆盖（config extra 或 API_SERVER_MODEL_NAME 环境变量）
        2. 活跃 profile 名称（使每个 profile 对外暴露不同的模型）
        3. 回退值："hermes-agent"
        """
        if explicit and explicit.strip():
            return explicit.strip()
        try:
            from hermes_cli.profiles import get_active_profile_name
            profile = get_active_profile_name()
            if profile and profile not in {"default", "custom"}:
                return profile
        except Exception:
            pass
        return "hermes-agent"

    def _cors_headers_for_origin(self, origin: str) -> Optional[Dict[str, str]]:
        """为被允许的浏览器来源返回 CORS 头部。"""
        if not origin or not self._cors_origins:
            return None

        if "*" in self._cors_origins:
            headers = dict(_CORS_HEADERS)
            headers["Access-Control-Allow-Origin"] = "*"
            headers["Access-Control-Max-Age"] = "600"
            return headers

        if origin not in self._cors_origins:
            return None

        headers = dict(_CORS_HEADERS)
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
        headers["Access-Control-Max-Age"] = "600"
        return headers

    def _origin_allowed(self, origin: str) -> bool:
        """允许非浏览器客户端以及显式配置的浏览器来源。"""
        if not origin:
            return True

        if not self._cors_origins:
            return False

        return "*" in self._cors_origins or origin in self._cors_origins

    @staticmethod
    def _clean_log_value(value: Any, *, max_len: int = 200) -> str:
        """在请求元数据进入安全日志之前对其进行净化。"""
        if value is None:
            return ""
        text = str(value).replace("\r", " ").replace("\n", " ").strip()
        return text[:max_len]

    def _request_audit_context(self, request: "web.Request") -> Dict[str, str]:
        """返回用于安全/审计告警的非敏感来源元数据。"""
        peer_ip = ""
        try:
            peer = request.transport.get_extra_info("peername") if request.transport else None
            if isinstance(peer, (tuple, list)) and peer:
                peer_ip = str(peer[0])
        except Exception:
            peer_ip = ""

        return {
            "remote": self._clean_log_value(getattr(request, "remote", "") or peer_ip),
            "peer_ip": self._clean_log_value(peer_ip),
            "forwarded_for": self._clean_log_value(request.headers.get("X-Forwarded-For", "")),
            "real_ip": self._clean_log_value(request.headers.get("X-Real-IP", "")),
            "method": self._clean_log_value(request.method, max_len=16),
            "path": self._clean_log_value(request.path_qs, max_len=500),
            "user_agent": self._clean_log_value(request.headers.get("User-Agent", ""), max_len=300),
        }

    def _request_audit_log_suffix(self, request: "web.Request") -> str:
        ctx = self._request_audit_context(request)
        fields = [f"{key}={value!r}" for key, value in ctx.items() if value]
        return " ".join(fields) if fields else "source='unknown'"

    def _cron_origin_from_request(self, request: "web.Request") -> Dict[str, str]:
        """将通过 HTTP 创建的 cron 任务的来源安全元数据持久化到任务上。"""
        ctx = self._request_audit_context(request)
        origin = {
            "platform": "api_server",
            "chat_id": "api",
        }
        if ctx.get("remote"):
            origin["source_ip"] = ctx["remote"]
        if ctx.get("peer_ip"):
            origin["peer_ip"] = ctx["peer_ip"]
        if ctx.get("forwarded_for"):
            origin["forwarded_for"] = ctx["forwarded_for"]
        if ctx.get("real_ip"):
            origin["real_ip"] = ctx["real_ip"]
        if ctx.get("user_agent"):
            origin["user_agent"] = ctx["user_agent"]
        return origin

    # ------------------------------------------------------------------
    # 鉴权辅助
    # ------------------------------------------------------------------

    def _check_auth(self, request: "web.Request") -> Optional["web.Response"]:
        """
        校验来自 Authorization 头部的 Bearer token。

        鉴权通过返回 None，失败则返回 401 web.Response。
        connect() 在没有 API_SERVER_KEY 时拒绝启动 API 服务器，因此
        无密钥分支仅为测试或不被支持的手动接线而存在。
        """
        if not self._api_key:
            return None

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if hmac.compare_digest(token, self._api_key):
                return None  # 鉴权通过

        logger.warning(
            "API server rejected invalid API key: %s",
            self._request_audit_log_suffix(request),
        )
        return web.json_response(
            {"error": {"message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key"}},
            status=401,
        )

    # ------------------------------------------------------------------
    # Session 头部辅助
    # ------------------------------------------------------------------

    # session 标识符的软长度上限。头部总体上受 aiohttp 约束
    # （``client_max_size`` / 默认每个头部 8 KiB），但我们对 session
    # 头部施加更紧的限制，这样调用方就无法通过传入多 KB 的 "session
    # key" 来耗费内存。256 字符远高于任何现实的稳定 channel 标识符
    # （例如 ``agent:main:webui:dm:user-42``），同时仍足够小，使得
    # 净化后的形式能安全地传入 Honcho / state.db。
    _MAX_SESSION_HEADER_LEN = 256

    def _parse_session_key_header(
        self, request: "web.Request"
    ) -> tuple[Optional[str], Optional["web.Response"]]:
        """提取并校验 ``X-Hermes-Session-Key`` 头部。

        session key 是一个稳定的、按 channel 划分的标识符，用于跨
        transcript 划定长期记忆（例如 Honcho session）的作用域。它独立于
        ``X-Hermes-Session-Id``：调用方可以只发其中之一、两者都发，或都不发。

        成功时返回 ``(session_key, None)``（头部为空/缺失时 key 取
        ``None``），校验失败时返回 ``(None, error_response)``。

        安全性：与会话延续一样，接受调用方提供的记忆作用域需要 API key
        鉴权，这样仅本地的服务器上的未鉴权客户端就无法通过猜 key 把自己
        注入到其他用户的长期记忆作用域中。
        """
        raw = request.headers.get("X-Hermes-Session-Key", "").strip()
        if not raw:
            return None, None

        if not self._api_key:
            logger.warning(
                "X-Hermes-Session-Key rejected: no API key configured. "
                "Set API_SERVER_KEY to enable long-term memory scoping."
            )
            return None, web.json_response(
                _openai_error(
                    "X-Hermes-Session-Key requires API key authentication. "
                    "Configure API_SERVER_KEY to enable this feature."
                ),
                status=403,
            )

        # 拒绝控制字符，以免在回显路径上引发 header 注入。
        if re.search(r'[\r\n\x00]', raw):
            return None, web.json_response(
                {"error": {"message": "Invalid session key", "type": "invalid_request_error"}},
                status=400,
            )

        if len(raw) > self._MAX_SESSION_HEADER_LEN:
            return None, web.json_response(
                {"error": {"message": "Session key too long", "type": "invalid_request_error"}},
                status=400,
            )

        return raw, None

    # ------------------------------------------------------------------
    # Session DB 辅助
    # ------------------------------------------------------------------

    def _ensure_session_db(self):
        """惰性初始化并返回共享的 SessionDB 实例。

        会话被持久化到 ``state.db``，这样 ``hermes sessions list`` 就能
        把 API 服务器的对话与 CLI、gateway 的对话一起展示。
        """
        if self._session_db is None:
            try:
                from hermes_state import SessionDB
                self._session_db = SessionDB()
            except Exception as e:
                logger.debug("SessionDB unavailable for API server: %s", e)
        return self._session_db

    # ------------------------------------------------------------------
    # Agent 创建辅助
    # ------------------------------------------------------------------

    def _create_agent(
        self,
        ephemeral_system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        stream_delta_callback=None,
        tool_progress_callback=None,
        tool_start_callback=None,
        tool_complete_callback=None,
        gateway_session_key: Optional[str] = None,
    ) -> Any:
        """
        使用 gateway 的运行时配置创建一个 AIAgent 实例。

        使用 _resolve_runtime_agent_kwargs() 从 config.yaml / 环境变量中
        获取 model、api_key、base_url 等。工具集从 config.yaml 的
        platform_toolsets.api_server 解析（与所有其他 gateway 平台相同），
        回退到 hermes-api-server 默认值。

        ``gateway_session_key`` 是由客户端（通过 ``X-Hermes-Session-Key``）
        提供的稳定的、按 channel 划分的标识符。与划定短期 transcript
        作用域、并在 /new 时轮换的 ``session_id`` 不同，此 key 旨在跨
        transcript 持久存在，使长期记忆 provider（例如 Honcho）能正确地
        按对话划定其 per-chat 状态的作用域 —— 与原生 gateway 的
        ``session_key`` 语义一致。
        """
        from run_agent import AIAgent
        from gateway.run import (
            _current_max_iterations,
            _resolve_runtime_agent_kwargs,
            _resolve_gateway_model,
            _load_gateway_config,
            GatewayRunner,
        )
        from hermes_cli.tools_config import _get_platform_tools

        runtime_kwargs = _resolve_runtime_agent_kwargs()
        reasoning_config = GatewayRunner._load_reasoning_config()
        model = _resolve_gateway_model()

        user_config = _load_gateway_config()
        enabled_toolsets = sorted(_get_platform_tools(user_config, "api_server"))

        max_iterations = _current_max_iterations()

        # 加载 fallback provider 链，使 API 服务器平台拥有与
        # Telegram/Discord/Slack 相同的 fallback 行为（修复 #4954）。
        fallback_model = GatewayRunner._load_fallback_model()

        agent = AIAgent(
            model=model,
            **runtime_kwargs,
            max_iterations=max_iterations,
            quiet_mode=True,
            verbose_logging=False,
            ephemeral_system_prompt=ephemeral_system_prompt or None,
            enabled_toolsets=enabled_toolsets,
            session_id=session_id,
            platform="api_server",
            stream_delta_callback=stream_delta_callback,
            tool_progress_callback=tool_progress_callback,
            tool_start_callback=tool_start_callback,
            tool_complete_callback=tool_complete_callback,
            session_db=self._ensure_session_db(),
            fallback_model=fallback_model,
            reasoning_config=reasoning_config,
            gateway_session_key=gateway_session_key,
        )
        return agent

    # ------------------------------------------------------------------
    # HTTP Handler
    # ------------------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """GET /health —— 简单的健康检查。"""
        return web.json_response(
            {"status": "ok", "platform": "hermes-agent", "version": _hermes_version()}
        )

    async def _handle_health_detailed(self, request: "web.Request") -> "web.Response":
        """GET /health/detailed —— 用于跨容器 dashboard 探测的丰富状态信息。

        返回 gateway 状态、已连接平台、PID 和运行时长，使 dashboard 无需
        共享 PID 文件或 /proc 访问即可展示完整状态。无需鉴权。
        """
        from gateway.status import (
            derive_gateway_busy,
            derive_gateway_drainable,
            parse_active_agents,
            read_runtime_status,
        )

        runtime = read_runtime_status() or {}
        gw_state = runtime.get("gateway_state")
        gw_active = parse_active_agents(runtime.get("active_agents", 0))
        # 该端点由 gateway 进程提供服务，因此按定义它就是存活的 ——
        # gateway_running 为 True。从 /api/status 所用的同一份共享契约推导
        # busy/drainable，使这两个入口永远保持一致。
        return web.json_response({
            "status": "ok",
            "platform": "hermes-agent",
            "version": _hermes_version(),
            "gateway_state": gw_state,
            "platforms": runtime.get("platforms", {}),
            "active_agents": gw_active,
            "gateway_busy": derive_gateway_busy(
                gateway_running=True,
                gateway_state=gw_state,
                active_agents=gw_active,
            ),
            "gateway_drainable": derive_gateway_drainable(
                gateway_running=True,
                gateway_state=gw_state,
            ),
            "exit_reason": runtime.get("exit_reason"),
            "updated_at": runtime.get("updated_at"),
            "pid": os.getpid(),
        })

    async def _handle_models(self, request: "web.Request") -> "web.Response":
        """GET /v1/models —— 将 hermes-agent 作为可用模型返回。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        return web.json_response({
            "object": "list",
            "data": [
                {
                    "id": self._model_name,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "hermes",
                    "permission": [],
                    "root": self._model_name,
                    "parent": None,
                }
            ],
        })

    async def _handle_capabilities(self, request: "web.Request") -> "web.Response":
        """GET /v1/capabilities —— 对外声明稳定的 API 接口面。

        外部 UI 和编排器使用此端点发现 API 服务器的插件安全契约，无需
        抓取文档，也无需假设每个 Hermes 版本都暴露相同的端点。
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        return web.json_response({
            "object": "hermes.api_server.capabilities",
            "platform": "hermes-agent",
            "model": self._model_name,
            "auth": {
                "type": "bearer",
                "required": bool(self._api_key),
            },
            "runtime": {
                "mode": "server_agent",
                "tool_execution": "server",
                "split_runtime": False,
                "description": (
                    "The API server creates a server-side Hermes AIAgent; "
                    "tools execute on the API-server host unless a future "
                    "explicit split-runtime mode is enabled."
                ),
            },
            "features": {
                "chat_completions": True,
                "chat_completions_streaming": True,
                "responses_api": True,
                "responses_streaming": True,
                "run_submission": True,
                "run_status": True,
                "run_events_sse": True,
                "run_stop": True,
                "run_approval_response": True,
                "tool_progress_events": True,
                "approval_events": True,
                "session_resources": True,
                "session_chat": True,
                "session_chat_streaming": True,
                "session_fork": True,
                "admin_config_rw": False,
                "jobs_admin": False,
                "memory_write_api": False,
                "skills_api": True,
                "audio_api": False,
                "realtime_voice": False,
                "session_continuity_header": "X-Hermes-Session-Id",
                "session_key_header": "X-Hermes-Session-Key",
                "cors": bool(self._cors_origins),
            },
            "endpoints": {
                "health": {"method": "GET", "path": "/health"},
                "health_detailed": {"method": "GET", "path": "/health/detailed"},
                "models": {"method": "GET", "path": "/v1/models"},
                "chat_completions": {"method": "POST", "path": "/v1/chat/completions"},
                "responses": {"method": "POST", "path": "/v1/responses"},
                "runs": {"method": "POST", "path": "/v1/runs"},
                "run_status": {"method": "GET", "path": "/v1/runs/{run_id}"},
                "run_events": {"method": "GET", "path": "/v1/runs/{run_id}/events"},
                "run_approval": {"method": "POST", "path": "/v1/runs/{run_id}/approval"},
                "run_stop": {"method": "POST", "path": "/v1/runs/{run_id}/stop"},
                "skills": {"method": "GET", "path": "/v1/skills"},
                "toolsets": {"method": "GET", "path": "/v1/toolsets"},
                "sessions": {"method": "GET", "path": "/api/sessions"},
                "session_create": {"method": "POST", "path": "/api/sessions"},
                "session": {"method": "GET", "path": "/api/sessions/{session_id}"},
                "session_update": {"method": "PATCH", "path": "/api/sessions/{session_id}"},
                "session_delete": {"method": "DELETE", "path": "/api/sessions/{session_id}"},
                "session_messages": {"method": "GET", "path": "/api/sessions/{session_id}/messages"},
                "session_fork": {"method": "POST", "path": "/api/sessions/{session_id}/fork"},
                "session_chat": {"method": "POST", "path": "/api/sessions/{session_id}/chat"},
                "session_chat_stream": {"method": "POST", "path": "/api/sessions/{session_id}/chat/stream"},
            },
        })

    async def _handle_skills(self, request: "web.Request") -> "web.Response":
        """GET /v1/skills —— 列出 API 服务器 agent 可见的已安装技能。

        只读列表，面向需要在不发送聊天消息询问模型的情况下了解有哪些
        技能可用的外部客户端。镜像 gateway/CLI 通过 ``/skills list``
        暴露的内容，但以确定性的 JSON 负载形式返回。

        返回与技能中心内部使用的相同的技能元数据（名称、描述、分类）。
        被禁用的技能会被排除，使列表与 agent 实际加载的内容一致。
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        try:
            from tools.skills_tool import _find_all_skills, _sort_skills
            skills = _sort_skills(_find_all_skills(skip_disabled=False))
        except Exception:
            logger.exception("GET /v1/skills failed")
            return web.json_response(
                _openai_error("Failed to enumerate skills", err_type="server_error"),
                status=500,
            )

        return web.json_response({
            "object": "list",
            "data": skills,
        })

    async def _handle_toolsets(self, request: "web.Request") -> "web.Response":
        """GET /v1/toolsets —— 列出工具集及其解析后的工具。

        返回 api_server 平台实际向其 agent 暴露的工具集接口面：每个
        工具集的启用/配置状态，以及它展开后的具体工具名称。这是客户端
        本需要通过询问模型它能调用哪些工具才能还原内容的确定性等价物。
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        try:
            from hermes_cli.config import load_config
            from hermes_cli.tools_config import (
                _get_effective_configurable_toolsets,
                _get_platform_tools,
                _toolset_has_keys,
            )
            from toolsets import resolve_toolset

            config = load_config()
            enabled_toolsets = _get_platform_tools(
                config,
                "api_server",
                include_default_mcp_servers=False,
            )
            data: List[Dict[str, Any]] = []
            for name, label, desc in _get_effective_configurable_toolsets():
                try:
                    tools = sorted(set(resolve_toolset(name)))
                except Exception:
                    tools = []
                is_enabled = name in enabled_toolsets
                data.append({
                    "name": name,
                    "label": label,
                    "description": desc,
                    "enabled": is_enabled,
                    "configured": _toolset_has_keys(name, config),
                    "tools": tools,
                })
        except Exception:
            logger.exception("GET /v1/toolsets failed")
            return web.json_response(
                _openai_error("Failed to enumerate toolsets", err_type="server_error"),
                status=500,
            )

        return web.json_response({
            "object": "list",
            "platform": "api_server",
            "data": data,
        })

    # ------------------------------------------------------------------
    # /api/sessions —— 轻量客户端/session 资源 API
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_nonnegative_int(value: Any, default: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        if parsed < 0:
            return default
        return min(parsed, maximum)

    @staticmethod
    def _session_response(session: Dict[str, Any]) -> Dict[str, Any]:
        """返回稳定的、对客户端安全的 session 表示。"""
        safe_keys = (
            "id", "source", "user_id", "model", "title", "started_at", "ended_at",
            "end_reason", "message_count", "tool_call_count", "input_tokens",
            "output_tokens", "cache_read_tokens", "cache_write_tokens",
            "reasoning_tokens", "estimated_cost_usd", "actual_cost_usd",
            "api_call_count", "parent_session_id", "last_active", "preview",
            "_lineage_root_id",
        )
        payload = {key: session.get(key) for key in safe_keys if key in session}
        # 避免通过客户端 API 暴露完整的 system_prompt/model_config；
        # 调用方只需要知道这些快照是否存在。
        payload["has_system_prompt"] = bool(session.get("system_prompt"))
        payload["has_model_config"] = bool(session.get("model_config"))
        return payload

    @staticmethod
    def _message_response(message: Dict[str, Any]) -> Dict[str, Any]:
        safe_keys = (
            "id", "session_id", "role", "content", "tool_call_id", "tool_calls",
            "tool_name", "timestamp", "token_count", "finish_reason", "reasoning",
            "reasoning_content",
        )
        return {key: message.get(key) for key in safe_keys if key in message}

    async def _read_json_body(self, request: "web.Request") -> tuple[Dict[str, Any], Optional["web.Response"]]:
        try:
            body = await request.json()
        except Exception:
            return {}, web.json_response(_openai_error("Invalid JSON in request body"), status=400)
        if not isinstance(body, dict):
            return {}, web.json_response(_openai_error("Request body must be a JSON object"), status=400)
        return body, None

    def _get_existing_session_or_404(self, session_id: str) -> tuple[Optional[Dict[str, Any]], Optional["web.Response"]]:
        db = self._ensure_session_db()
        if db is None:
            return None, web.json_response(_openai_error("Session database unavailable", code="session_db_unavailable"), status=503)
        session = db.get_session(session_id)
        if not session:
            return None, web.json_response(_openai_error(f"Session not found: {session_id}", code="session_not_found"), status=404)
        return session, None

    def _conversation_history_for_session(self, session_id: str) -> List[Dict[str, Any]]:
        db = self._ensure_session_db()
        if db is None:
            return []
        try:
            return db.get_messages_as_conversation(session_id)
        except Exception as exc:
            logger.warning("Failed to load session history for %s: %s", session_id, exc)
            return []

    async def _handle_list_sessions(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions —— 列出已持久化的 Hermes 会话。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        db = self._ensure_session_db()
        if db is None:
            return web.json_response(_openai_error("Session database unavailable", code="session_db_unavailable"), status=503)

        limit = self._parse_nonnegative_int(request.query.get("limit"), default=50, maximum=200)
        offset = self._parse_nonnegative_int(request.query.get("offset"), default=0, maximum=1_000_000)
        source = request.query.get("source") or None
        include_children = _coerce_request_bool(request.query.get("include_children"), default=False)
        sessions = db.list_sessions_rich(
            source=source,
            limit=limit,
            offset=offset,
            include_children=include_children,
            order_by_last_active=True,
        )
        return web.json_response({
            "object": "list",
            "data": [self._session_response(s) for s in sessions],
            "limit": limit,
            "offset": offset,
            "has_more": len(sessions) == limit,
        })

    async def _handle_create_session(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions —— 创建一个空的 Hermes session 记录。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        body, err = await self._read_json_body(request)
        if err:
            return err

        db = self._ensure_session_db()
        if db is None:
            return web.json_response(_openai_error("Session database unavailable", code="session_db_unavailable"), status=503)

        raw_id = body.get("id") or body.get("session_id")
        session_id = str(raw_id).strip() if raw_id else f"api_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        if not session_id or re.search(r'[\r\n\x00]', session_id):
            return web.json_response(_openai_error("Invalid session ID", code="invalid_session_id"), status=400)
        if len(session_id) > self._MAX_SESSION_HEADER_LEN:
            return web.json_response(_openai_error("Session ID too long", code="invalid_session_id"), status=400)
        if db.get_session(session_id):
            return web.json_response(_openai_error(f"Session already exists: {session_id}", code="session_exists"), status=409)

        model = body.get("model") or self._model_name
        system_prompt = body.get("system_prompt")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_prompt must be a string", code="invalid_system_prompt"), status=400)
        db.create_session(session_id, "api_server", model=str(model) if model else None, system_prompt=system_prompt)
        title = body.get("title")
        if title is not None:
            try:
                db.set_session_title(session_id, str(title))
            except ValueError as exc:
                db.delete_session(session_id)
                return web.json_response(_openai_error(str(exc), code="invalid_title"), status=400)
        session = db.get_session(session_id) or {"id": session_id, "source": "api_server", "model": model, "title": title}
        return web.json_response({"object": "hermes.session", "session": self._session_response(session)}, status=201)

    async def _handle_get_session(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions/{session_id}。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session, err = self._get_existing_session_or_404(request.match_info["session_id"])
        if err:
            return err
        return web.json_response({"object": "hermes.session", "session": self._session_response(session)})

    async def _handle_patch_session(self, request: "web.Request") -> "web.Response":
        """PATCH /api/sessions/{session_id} —— 更新对客户端安全的 session 元数据。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        session, err = self._get_existing_session_or_404(session_id)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        allowed = {"title", "end_reason"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            return web.json_response(_openai_error(f"Unsupported session fields: {', '.join(unknown)}", code="unsupported_session_field"), status=400)

        db = self._ensure_session_db()
        if "title" in body:
            try:
                db.set_session_title(session_id, "" if body["title"] is None else str(body["title"]))
            except ValueError as exc:
                return web.json_response(_openai_error(str(exc), code="invalid_title"), status=400)
        if body.get("end_reason"):
            db.end_session(session_id, str(body["end_reason"]))
        session = db.get_session(session_id) or session
        return web.json_response({"object": "hermes.session", "session": self._session_response(session)})

    async def _handle_delete_session(self, request: "web.Request") -> "web.Response":
        """DELETE /api/sessions/{session_id}。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        session, err = self._get_existing_session_or_404(session_id)
        if err:
            return err
        db = self._ensure_session_db()
        deleted = db.delete_session(session_id)
        return web.json_response({"object": "hermes.session.deleted", "id": session_id, "deleted": bool(deleted)})

    async def _handle_session_messages(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions/{session_id}/messages。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        _, err = self._get_existing_session_or_404(session_id)
        if err:
            return err
        db = self._ensure_session_db()
        resolved_id = db.resolve_resume_session_id(session_id)
        messages = db.get_messages(resolved_id)
        return web.json_response({
            "object": "list",
            "session_id": resolved_id,
            "data": [self._message_response(m) for m in messages],
        })

    async def _handle_fork_session(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions/{session_id}/fork —— 通过当前 SessionDB 原语进行分支。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        source_id = request.match_info["session_id"]
        source, err = self._get_existing_session_or_404(source_id)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        db = self._ensure_session_db()
        fork_id = str(body.get("id") or body.get("session_id") or f"api_{int(time.time())}_{uuid.uuid4().hex[:8]}").strip()
        if not fork_id or re.search(r'[\r\n\x00]', fork_id):
            return web.json_response(_openai_error("Invalid session ID", code="invalid_session_id"), status=400)
        if db.get_session(fork_id):
            return web.json_response(_openai_error(f"Session already exists: {fork_id}", code="session_exists"), status=409)

        # 与 CLI 的 /branch 语义保持一致：将原会话标记为 branched，然后创建
        # 一个承接 transcript 的子会话。这里使用 SessionDB 原生的
        # parent_session_id/end_reason 可见性模型，而不是另起一套并行的
        # fork 存储。
        db.end_session(source_id, "branched")
        db.create_session(
            fork_id,
            "api_server",
            model=source.get("model"),
            system_prompt=source.get("system_prompt"),
            parent_session_id=source_id,
        )
        messages = db.get_messages(source_id)
        db.replace_messages(fork_id, messages)
        title = body.get("title")
        if title is None:
            base = source.get("title") or "fork"
            try:
                title = db.get_next_title_in_lineage(base)
            except Exception:
                title = f"{base} fork"
        try:
            db.set_session_title(fork_id, str(title))
        except ValueError as exc:
            return web.json_response(_openai_error(str(exc), code="invalid_title"), status=400)
        fork = db.get_session(fork_id) or {"id": fork_id, "parent_session_id": source_id}
        return web.json_response({"object": "hermes.session", "session": self._session_response(fork)}, status=201)

    async def _handle_session_chat(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions/{session_id}/chat —— 一次同步的 agent 轮次。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err
        session_id = request.match_info["session_id"]
        _, err = self._get_existing_session_or_404(session_id)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        user_message, err = _session_chat_user_message(body)
        if err is not None:
            return err
        system_prompt = body.get("system_message") or body.get("instructions")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_message must be a string", code="invalid_system_message"), status=400)
        history = self._conversation_history_for_session(session_id)
        result, usage = await self._run_agent(
            user_message=user_message,
            conversation_history=history,
            ephemeral_system_prompt=system_prompt,
            session_id=session_id,
            gateway_session_key=gateway_session_key,
        )
        effective_session_id = result.get("session_id") if isinstance(result, dict) else session_id
        final_response = result.get("final_response", "") if isinstance(result, dict) else ""
        headers = {"X-Hermes-Session-Id": effective_session_id or session_id}
        if gateway_session_key:
            headers["X-Hermes-Session-Key"] = gateway_session_key
        return web.json_response(
            {
                "object": "hermes.session.chat.completion",
                "session_id": effective_session_id or session_id,
                "message": {"role": "assistant", "content": final_response},
                "usage": usage,
            },
            headers=headers,
        )

    async def _handle_session_chat_stream(self, request: "web.Request") -> "web.StreamResponse":
        """POST /api/sessions/{session_id}/chat/stream —— 对 _run_agent 的 SSE 封装。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err
        session_id = request.match_info["session_id"]
        _, err = self._get_existing_session_or_404(session_id)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        user_message, err = _session_chat_user_message(body)
        if err is not None:
            return err
        system_prompt = body.get("system_message") or body.get("instructions")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_message must be a string", code="invalid_system_message"), status=400)

        loop = asyncio.get_running_loop()
        queue: "asyncio.Queue[Optional[tuple[str, Dict[str, Any]]]]" = asyncio.Queue()
        message_id = f"msg_{uuid.uuid4().hex}"
        run_id = f"run_{uuid.uuid4().hex}"
        seq = 0

        def _event_payload(name: str, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
            nonlocal seq
            seq += 1
            payload.setdefault("session_id", session_id)
            payload.setdefault("run_id", run_id)
            payload.setdefault("seq", seq)
            payload.setdefault("ts", time.time())
            return name, payload

        def _enqueue(name: str, payload: Dict[str, Any]) -> None:
            event = _event_payload(name, payload)
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            try:
                if running_loop is loop:
                    queue.put_nowait(event)
                else:
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                pass

        def _delta(delta: str) -> None:
            if delta:
                _enqueue("assistant.delta", {"message_id": message_id, "delta": delta})

        def _tool_progress(event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs) -> None:
            if event_type == "reasoning.available":
                _enqueue("tool.progress", {"message_id": message_id, "tool_name": tool_name or "_thinking", "delta": preview or ""})
            elif event_type in {"tool.started", "tool.completed", "tool.failed"}:
                event_name = event_type.replace("tool.", "tool.")
                _enqueue(event_name, {"message_id": message_id, "tool_name": tool_name, "preview": preview, "args": args})

        async def _run_and_signal() -> None:
            try:
                await queue.put(_event_payload("run.started", {"user_message": {"role": "user", "content": user_message}}))
                await queue.put(_event_payload("message.started", {"message": {"id": message_id, "role": "assistant"}}))
                history = self._conversation_history_for_session(session_id)
                result, usage = await self._run_agent(
                    user_message=user_message,
                    conversation_history=history,
                    ephemeral_system_prompt=system_prompt,
                    session_id=session_id,
                    stream_delta_callback=_delta,
                    tool_progress_callback=_tool_progress,
                    gateway_session_key=gateway_session_key,
                )
                final_response = result.get("final_response", "") if isinstance(result, dict) else ""
                effective_session_id = result.get("session_id", session_id) if isinstance(result, dict) else session_id
                turn_messages = self._turn_transcript_messages(history, user_message, result) if isinstance(result, dict) else []
                await queue.put(_event_payload("assistant.completed", {
                    "session_id": effective_session_id,
                    "message_id": message_id,
                    "content": final_response,
                    "completed": True,
                    "partial": False,
                    "interrupted": False,
                }))
                await queue.put(_event_payload("run.completed", {
                    "session_id": effective_session_id,
                    "message_id": message_id,
                    "completed": True,
                    "messages": turn_messages,
                    "usage": usage,
                }))
            except Exception as exc:
                logger.exception("[api_server] session chat stream failed")
                await queue.put(_event_payload("error", {"message": str(exc)}))
            finally:
                await queue.put(_event_payload("done", {}))
                await queue.put(None)

        task = asyncio.create_task(_run_and_signal())
        try:
            self._background_tasks.add(task)
        except TypeError:
            pass
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)

        headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Hermes-Session-Id": session_id,
        }
        if gateway_session_key:
            headers["X-Hermes-Session-Key"] = gateway_session_key
        response = web.StreamResponse(status=200, headers=headers)
        await response.prepare(request)
        last_write = time.monotonic()
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
                    last_write = time.monotonic()
                    continue
                if item is None:
                    break
                name, payload = item
                data = json.dumps(payload, ensure_ascii=False)
                await response.write(f"event: {name}\ndata: {data}\n\n".encode("utf-8"))
                last_write = time.monotonic()
        except (asyncio.CancelledError, ConnectionResetError):
            task.cancel()
            raise
        except Exception as exc:
            logger.debug("[api_server] session SSE stream error: %s", exc)
        return response

    async def _handle_chat_completions(self, request: "web.Request") -> "web.Response":
        """POST /v1/chat/completions —— OpenAI Chat Completions 格式。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # 限制在途 agent run 的总数（可配置；#7483）。
        limited = self._concurrency_limited_response()
        if limited is not None:
            return limited

        # 解析请求体
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(_openai_error("Invalid JSON in request body"), status=400)

        messages = body.get("messages")
        if not messages or not isinstance(messages, list):
            return web.json_response(
                {"error": {"message": "Missing or invalid 'messages' field", "type": "invalid_request_error"}},
                status=400,
            )

        stream = _coerce_request_bool(body.get("stream"), default=False)

        # 提取 system 消息（成为叠加在核心之上的临时 system prompt）
        system_prompt = None
        conversation_messages: List[Dict[str, str]] = []

        for idx, msg in enumerate(messages):
            role = msg.get("role", "")
            raw_content = msg.get("content", "")
            if role == "system":
                # system 消息不支持图片（Anthropic 会拒绝，OpenAI 的文本模型
                # system 也不会渲染它们）。拍平为文本。
                content = _normalize_chat_content(raw_content)
                if system_prompt is None:
                    system_prompt = content
                else:
                    system_prompt = system_prompt + "\n" + content
            elif role in {"user", "assistant"}:
                try:
                    content = _normalize_multimodal_content(raw_content)
                except ValueError as exc:
                    return _multimodal_validation_error(exc, param=f"messages[{idx}].content")
                conversation_messages.append({"role": role, "content": content})

        # 提取最后一条 user 消息作为主要输入
        user_message: Any = ""
        history = []
        if conversation_messages:
            user_message = conversation_messages[-1].get("content", "")
            history = conversation_messages[:-1]

        if not _content_has_visible_payload(user_message):
            return web.json_response(
                {"error": {"message": "No user message found in messages", "type": "invalid_request_error"}},
                status=400,
            )

        # 允许调用方通过 X-Hermes-Session-Key 以稳定的、按 channel 划分的
        # 标识符划定长期记忆（例如 Honcho）的作用域。它独立于
        # X-Hermes-Session-Id：key 跨 transcript 持久存在，而 id 在调用方
        # 开启新 transcript（即 /new 语义）时轮换。参见
        # _parse_session_key_header。
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err

        # 允许调用方通过传入 X-Hermes-Session-Id 来继续一个已存在的会话。
        # 提供该头部时，历史会从 state.db 加载，而不是来自请求体。
        #
        # 安全性：会话延续会暴露对话历史，因此仅在配置了 API key 且请求
        # 已通过鉴权时才允许。没有这道关卡，任何未鉴权的客户端都能通过
        # 猜测/枚举 session ID 读取任意会话的历史。
        provided_session_id = request.headers.get("X-Hermes-Session-Id", "").strip()
        if provided_session_id:
            if not self._api_key:
                logger.warning(
                    "Session continuation via X-Hermes-Session-Id rejected: "
                    "no API key configured.  Set API_SERVER_KEY to enable "
                    "session continuity."
                )
                return web.json_response(
                    _openai_error(
                        "Session continuation requires API key authentication. "
                        "Configure API_SERVER_KEY to enable this feature."
                    ),
                    status=403,
                )
            # 净化：拒绝可能引发 header 注入的控制字符。
            if re.search(r'[\r\n\x00]', provided_session_id):
                return web.json_response(
                    {"error": {"message": "Invalid session ID", "type": "invalid_request_error"}},
                    status=400,
                )
            session_id = provided_session_id
            try:
                db = self._ensure_session_db()
                if db is not None:
                    history = db.get_messages_as_conversation(session_id)
            except Exception as e:
                logger.warning("Failed to load session history for %s: %s", session_id, e)
                history = []
        else:
            # 从对话指纹推导出一个稳定的 session ID，使来自同一个 Open WebUI
            # （或类似前端）对话的连续消息映射到同一个 Hermes session。
            # 首条 user 消息 + system prompt 在所有轮次中都是常量。
            first_user = ""
            for cm in conversation_messages:
                if cm.get("role") == "user":
                    first_user = cm.get("content", "")
                    break
            session_id = _derive_chat_session_id(system_prompt, first_user)
            # history 已在上方从请求体中设置

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:29]}"
        model_name = body.get("model", self._model_name)
        created = int(time.time())

        if stream:
            import queue as _q
            _stream_q: _q.Queue = _q.Queue()

            def _on_delta(delta):
                # 过滤掉 None —— agent 触发 stream_delta_callback(None) 是
                # 为了通知 CLI 显示层在工具执行前关闭响应框，但 SSE 写入端
                # 把 None 用作流结束哨兵。转发它会过早关闭 HTTP 响应，导致
                # Open WebUI（及类似前端）在工具调用后错过最终答案。SSE
                # 循环改为通过 agent_task.done() 检测完成。
                if delta is not None:
                    _stream_q.put(delta)

            # 记录我们已经为其发过 "running" 生命周期事件的 tool_call_id，
            # 这样没有匹配 "running" 的 "completed" 事件（例如内部/被过滤的
            # 工具）会被静默丢弃，而不是产生客户端无法关联的孤立事件。
            _started_tool_call_ids: set[str] = set()

            def _on_tool_start(tool_call_id, function_name, function_args):
                """发出携带 ``status: running`` 的 ``hermes.tool.progress``。

                替换旧的 ``tool_progress_callback("tool.started", ...)``
                发射，使 SSE 消费方在每个工具开始时收到单一事件，同时携带
                旧的 ``tool``/``emoji``/``label`` 负载（面向 #6972 前端）
                和新的 ``toolCallId``/``status`` 关联字段（#16588）。

                跳过名字以 ``_`` 开头的工具，使内部事件（``_thinking``、…）
                不出现在线路上 —— 与之前 ``_on_tool_progress`` 的过滤器
                保持完全一致。
                """
                if not tool_call_id or function_name.startswith("_"):
                    return
                _started_tool_call_ids.add(tool_call_id)
                from agent.display import build_tool_preview, get_tool_emoji
                label = build_tool_preview(function_name, function_args) or function_name
                _stream_q.put(("__tool_progress__", {
                    "tool": function_name,
                    "emoji": get_tool_emoji(function_name),
                    "label": label,
                    "toolCallId": tool_call_id,
                    "status": "running",
                }))

            def _on_tool_complete(tool_call_id, function_name, function_args, function_result):
                """发出匹配的 ``status: completed`` 事件。

                如果开始被过滤掉（内部工具、缺少 id 或从未见过），则丢弃
                此事件，使客户端永远不会收到无法与之前 ``running`` 关联的
                孤立 ``completed``。
                """
                if not tool_call_id or tool_call_id not in _started_tool_call_ids:
                    return
                _started_tool_call_ids.discard(tool_call_id)
                _stream_q.put(("__tool_progress__", {
                    "tool": function_name,
                    "toolCallId": tool_call_id,
                    "status": "completed",
                }))

            # 在后台启动 agent。agent_ref 是一个可变容器，使 SSE 写入端
            # 可以在客户端断开连接时打断 agent。
            #
            # 这里有意不接 ``tool_progress_callback``：它会导致每次发射
            # 重复，因为 ``run_agent`` 会把它和
            # ``tool_start_callback``/``tool_complete_callback`` 并排触发。
            # 结构化回调的信息更丰富（携带 tool_call id），因此由它们
            # 负责占据 chat-completions 的 SSE channel。
            agent_ref = [None]
            agent_task = asyncio.ensure_future(self._run_agent(
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=system_prompt,
                session_id=session_id,
                stream_delta_callback=_on_delta,
                tool_start_callback=_on_tool_start,
                tool_complete_callback=_on_tool_complete,
                agent_ref=agent_ref,
                gateway_session_key=gateway_session_key,
            ))
            # 确保 SSE 排空循环能够终止，而无需依赖轮询 agent_task.done()，
            # 后者可能与队列超时检查产生竞态。
            agent_task.add_done_callback(lambda _fut: _stream_q.put(None))

            return await self._write_sse_chat_completion(
                request, completion_id, model_name, created, _stream_q,
                agent_task, agent_ref, session_id=session_id,
                gateway_session_key=gateway_session_key,
            )

        # 非流式：运行 agent（可带可选的 Idempotency-Key）
        async def _compute_completion():
            return await self._run_agent(
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=system_prompt,
                session_id=session_id,
                gateway_session_key=gateway_session_key,
            )

        idempotency_key = request.headers.get("Idempotency-Key")
        if idempotency_key:
            fp = _make_request_fingerprint(body, keys=["model", "messages", "tools", "tool_choice", "stream"])
            try:
                result, usage = await _idem_cache.get_or_set(idempotency_key, fp, _compute_completion)
            except Exception as e:
                logger.error("Error running agent for chat completions: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )
        else:
            try:
                result, usage = await _compute_completion()
            except Exception as e:
                logger.error("Error running agent for chat completions: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )

        final_response = result.get("final_response") or ""
        is_partial = bool(result.get("partial"))
        is_failed = bool(result.get("failed"))
        completed = bool(result.get("completed", True))
        err_msg = result.get("error")

        # 决定 finish_reason。OpenAI 用 "length" 表示截断，用 "stop" 表示
        # 正常完成，下游 SDK 也接受 "error" / 自定义 code。参见 #22496。
        if is_partial and err_msg and "truncat" in err_msg.lower():
            finish_reason = "length"
        elif is_failed or (not completed and err_msg):
            finish_reason = "error"
        else:
            finish_reason = "stop"

        response_headers = {
            "X-Hermes-Session-Id": result.get("session_id", session_id),
        }
        if gateway_session_key:
            response_headers["X-Hermes-Session-Key"] = gateway_session_key

        # 硬失败路径：没有可用的 assistant 文本且存在真正的失败 → 返回 5xx
        # 和 OpenAI 风格的错误信封，使 SDK 客户端抛出异常，而不是静默地把
        # 内部失败字符串渲染成 message.content。
        if not final_response and (is_failed or is_partial):
            err_body = _openai_error(
                err_msg or "Agent run did not produce a response.",
                err_type="server_error",
                code="agent_incomplete",
            )
            err_body["error"]["hermes"] = {
                "completed": completed,
                "partial": is_partial,
                "failed": is_failed,
            }
            response_headers["X-Hermes-Completed"] = "false"
            response_headers["X-Hermes-Partial"] = "true" if is_partial else "false"
            return web.json_response(err_body, status=502, headers=response_headers)

        # 软部分失败路径：我们有 *一些* 文本但 run 未完成（例如截断后还留下
        # 部分缓冲输出）。仍返回 200，但通过 finish_reason="length" + Hermes
        # 专有扩展来标示截断。
        response_data = {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": final_response,
                    },
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }
        if is_partial or is_failed or not completed:
            response_data["hermes"] = {
                "completed": completed,
                "partial": is_partial,
                "failed": is_failed,
                "error": err_msg,
                "error_code": "output_truncated" if finish_reason == "length" else "agent_error",
            }
            response_headers["X-Hermes-Completed"] = "false"
            response_headers["X-Hermes-Partial"] = "true" if is_partial else "false"
            if err_msg:
                response_headers["X-Hermes-Error"] = err_msg[:200]

        return web.json_response(response_data, headers=response_headers)

    async def _write_sse_chat_completion(
        self, request: "web.Request", completion_id: str, model: str,
        created: int, stream_q, agent_task, agent_ref=None, session_id: str = None,
        gateway_session_key: str = None,
    ) -> "web.StreamResponse":
        """将 agent 的 stream_delta_callback 队列写入真正的流式 SSE。

        如果客户端在流传输过程中断开（网络掉线、关闭浏览器标签页），agent
        会通过 ``agent.interrupt()`` 被打断，从而停止发起 LLM API 调用，
        并且其 asyncio 任务包装器会被取消。
        """
        import queue as _q

        sse_headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        # CORS 中间件无法在 prepare() 刷新头部之后向 StreamResponse 注入
        # 头部，因此需要预先解析 CORS 头部。
        origin = request.headers.get("Origin", "")
        cors = self._cors_headers_for_origin(origin) if origin else None
        if cors:
            sse_headers.update(cors)
        if session_id:
            sse_headers["X-Hermes-Session-Id"] = session_id
        if gateway_session_key:
            sse_headers["X-Hermes-Session-Key"] = gateway_session_key
        response = web.StreamResponse(status=200, headers=sse_headers)
        await response.prepare(request)

        try:
            last_activity = time.monotonic()

            # 角色 chunk
            role_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            await response.write(f"data: {json.dumps(role_chunk)}\n\n".encode())
            last_activity = time.monotonic()

            # 辅助函数 —— 把一个队列项路由到正确的 SSE 事件。
            async def _emit(item):
                """向 SSE 流写入单个队列项。

                纯字符串作为普通的 ``delta.content`` chunk 发送。带标签的
                元组 ``("__tool_progress__", payload)`` 则作为自定义的
                ``event: hermes.tool.progress`` SSE 事件发送，使前端能够展示
                它们而不必将这些标记存入对话历史。原事件见 #6972，
                ``toolCallId``/``status`` 生命周期字段见 #16588。
                """
                if isinstance(item, tuple) and len(item) == 2 and item[0] == "__tool_progress__":
                    event_data = json.dumps(item[1])
                    await response.write(
                        f"event: hermes.tool.progress\ndata: {event_data}\n\n".encode()
                    )
                else:
                    content_chunk = {
                        "id": completion_id, "object": "chat.completion.chunk",
                        "created": created, "model": model,
                        "choices": [{"index": 0, "delta": {"content": item}, "finish_reason": None}],
                    }
                    await response.write(f"data: {json.dumps(content_chunk)}\n\n".encode())
                return time.monotonic()

            # 随 agent 产出持续发送 content chunk
            loop = asyncio.get_running_loop()
            while True:
                try:
                    delta = await loop.run_in_executor(None, lambda: stream_q.get(timeout=0.5))
                except _q.Empty:
                    if agent_task.done():
                        # 排空剩余项
                        while True:
                            try:
                                delta = stream_q.get_nowait()
                                if delta is None:
                                    break
                                last_activity = await _emit(delta)
                            except _q.Empty:
                                break
                        break
                    if time.monotonic() - last_activity >= CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS:
                        await response.write(b": keepalive\n\n")
                        last_activity = time.monotonic()
                    continue

                if delta is None:  # 流结束哨兵
                    break

                last_activity = await _emit(delta)

            # 从已完成的 agent 获取 usage
            usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            try:
                result, agent_usage = await agent_task
                usage = agent_usage or usage
            except Exception as exc:
                logger.warning("Agent task %s failed, usage data lost: %s", completion_id, exc)

            # 结束 chunk
            finish_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                },
            }
            await response.write(f"data: {json.dumps(finish_chunk)}\n\n".encode())
            await response.write(b"data: [DONE]\n\n")
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            # 客户端在流传输过程中断开。打断 agent，使其在下次循环迭代时停止
            # 发起 LLM API 调用，然后取消 asyncio 任务包装器。
            agent = agent_ref[0] if agent_ref else None
            if agent is not None:
                try:
                    agent.interrupt("SSE client disconnected")
                except Exception:
                    pass
            if not agent_task.done():
                agent_task.cancel()
                try:
                    await agent_task
                except (asyncio.CancelledError, Exception):
                    pass
            logger.info("SSE client disconnected; interrupted agent task %s", completion_id)
        except Exception as _exc:
            # agent 在流传输过程中崩溃。尝试发出一个错误 chunk，使客户端
            # 得到正确的响应，而不是因为不完整的 chunked encoding 而收到
            # TransferEncodingError。
            import traceback as _tb
            logger.error("Agent crashed mid-stream for %s: %s", completion_id, _tb.format_exc()[:300])
            try:
                error_chunk = {
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                }
                await response.write(f"data: {json.dumps(error_chunk)}\n\n".encode())
                await response.write(b"data: [DONE]\n\n")
            except Exception:
                pass

        return response

    async def _write_sse_responses(
        self,
        request: "web.Request",
        response_id: str,
        model: str,
        created_at: int,
        stream_q,
        agent_task,
        agent_ref,
        conversation_history: List[Dict[str, str]],
        user_message: str,
        instructions: Optional[str],
        conversation: Optional[str],
        store: bool,
        session_id: str,
        gateway_session_key: Optional[str] = None,
    ) -> "web.StreamResponse":
        """为 POST /v1/responses（OpenAI Responses API）写入 SSE 流。

        在 agent 运行过程中发出符合规范的事件类型：

        - ``response.created`` —— 初始信封（status=in_progress）
        - ``response.output_text.delta`` / ``response.output_text.done`` ——
          流式发送的 assistant 文本
        - ``response.output_item.added`` / ``response.output_item.done``，
          且 ``item.type == "function_call"`` —— 当 agent 调用工具时
          （两个事件都会触发；``done`` 事件携带最终确定的 ``arguments``
          字符串）
        - ``response.output_item.added``，且
          ``item.type == "function_call_output"`` —— 工具结果，包含
          ``{call_id, output, status}``
        - ``response.completed`` —— 终止事件，携带包含全部 output item
          和 usage 的完整 response 对象（与非流式路径保持相同的负载形态
          以保证一致性）
        - ``response.failed`` —— agent 出错时的终止事件

        如果客户端在流传输过程中断开，会调用 ``agent.interrupt()`` 使
        agent 停止发起上游 LLM 调用，然后取消 asyncio 任务。当
        ``store=True`` 时，会在 ``response.created`` 之后立即持久化一份
        初始的 ``in_progress`` 快照，断开连接时则会将其更新为
        ``incomplete`` 快照，使 GET /v1/responses/{id} 和
        ``previous_response_id`` 链式调用仍有可恢复的内容。
        """
        import queue as _q

        sse_headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        origin = request.headers.get("Origin", "")
        cors = self._cors_headers_for_origin(origin) if origin else None
        if cors:
            sse_headers.update(cors)
        if session_id:
            sse_headers["X-Hermes-Session-Id"] = session_id
        if gateway_session_key:
            sse_headers["X-Hermes-Session-Key"] = gateway_session_key
        response = web.StreamResponse(status=200, headers=sse_headers)
        await response.prepare(request)

        # 流传输过程中累积的状态
        final_text_parts: List[str] = []
        # 按名称跟踪尚未闭合的 function_call 项，以便工具完成时发出匹配的
        # ``done`` 事件。保留顺序。
        pending_tool_calls: List[Dict[str, Any]] = []
        # 我们目前发出过的 output item（用于构造终止的 response.completed
        # 负载）。按出现顺序保留。
        emitted_items: List[Dict[str, Any]] = []
        # output_index 的单调计数器（规范要求）。
        output_index = 0
        # 当 agent 未提供 call_id 时用于生成 call_id 的单调计数器
        # （它确实不会从 tool_progress_callback 提供）。
        call_counter = 0
        # 规范的 Responses SSE 事件包含单调递增的 sequence_number。在服务端
        # 为每个发出的事件都加上它，使校验 OpenAI 事件 schema 的客户端能
        # 解析我们的流。
        sequence_number = 0
        # 跟踪 assistant 消息 item id + content index 用于文本 delta 事件 ——
        # 规范将 delta 绑定到具体的 item。
        message_item_id = f"msg_{uuid.uuid4().hex[:24]}"
        message_output_index: Optional[int] = None
        message_opened = False

        async def _write_event(event_type: str, data: Dict[str, Any]) -> None:
            nonlocal sequence_number
            if "sequence_number" not in data:
                data["sequence_number"] = sequence_number
            sequence_number += 1
            payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
            await response.write(payload.encode())

        def _envelope(status: str) -> Dict[str, Any]:
            env: Dict[str, Any] = {
                "id": response_id,
                "object": "response",
                "status": status,
                "created_at": created_at,
                "model": model,
            }
            return env

        final_response_text = ""
        agent_error: Optional[str] = None
        usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        terminal_snapshot_persisted = False

        def _persist_response_snapshot(
            response_env: Dict[str, Any],
            *,
            conversation_history_snapshot: Optional[List[Dict[str, Any]]] = None,
        ) -> None:
            if not store:
                return
            if conversation_history_snapshot is None:
                conversation_history_snapshot = list(conversation_history)
                conversation_history_snapshot.append({"role": "user", "content": user_message})
            self._response_store.put(response_id, {
                "response": response_env,
                "conversation_history": conversation_history_snapshot,
                "instructions": instructions,
                "session_id": session_id,
            })
            if conversation:
                self._response_store.set_conversation(conversation, response_id)

        def _persist_incomplete_if_needed() -> None:
            """如果尚未写入终止快照，则持久化一份 ``incomplete`` 快照。

            从客户端断开（``ConnectionResetError``）和服务端取消
            （``asyncio.CancelledError``）两条路径都会调用，使
            GET /v1/responses/{id} 和 ``previous_response_id`` 链式调用在
            流被突然终止后仍能继续工作。
            """
            if not store or terminal_snapshot_persisted:
                return
            incomplete_text = "".join(final_text_parts) or final_response_text
            incomplete_items: List[Dict[str, Any]] = list(emitted_items)
            if incomplete_text:
                incomplete_items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": incomplete_text}],
                })
            incomplete_env = _envelope("incomplete")
            incomplete_env["output"] = incomplete_items
            incomplete_env["usage"] = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }
            incomplete_history = list(conversation_history)
            incomplete_history.append({"role": "user", "content": user_message})
            if incomplete_text:
                incomplete_history.append({"role": "assistant", "content": incomplete_text})
            _persist_response_snapshot(
                incomplete_env,
                conversation_history_snapshot=incomplete_history,
            )

        try:
            # response.created —— 初始信封，status=in_progress
            created_env = _envelope("in_progress")
            created_env["output"] = []
            await _write_event("response.created", {
                "type": "response.created",
                "response": created_env,
            })
            _persist_response_snapshot(created_env)
            last_activity = time.monotonic()

            async def _open_message_item() -> None:
                """在首个文本 delta 到达时为 assistant 消息发出
                response.output_item.added。"""
                nonlocal message_opened, message_output_index, output_index
                if message_opened:
                    return
                message_opened = True
                message_output_index = output_index
                output_index += 1
                item = {
                    "id": message_item_id,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                }
                await _write_event("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": message_output_index,
                    "item": item,
                })

            async def _emit_text_delta(delta_text: str) -> None:
                await _open_message_item()
                final_text_parts.append(delta_text)
                await _write_event("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "item_id": message_item_id,
                    "output_index": message_output_index,
                    "content_index": 0,
                    "delta": delta_text,
                    "logprobs": [],
                })

            async def _emit_tool_started(payload: Dict[str, Any]) -> str:
                """为 function_call 发出 response.output_item.added。

                返回 call_id，以便匹配的完成事件能引用它。优先使用 agent
                提供的真实 ``tool_call_id``；在测试或旧代码路径中回退到生成
                的 call id 以保证安全。
                """
                nonlocal output_index, call_counter
                call_counter += 1
                call_id = payload.get("tool_call_id") or f"call_{response_id[5:]}_{call_counter}"
                args = payload.get("arguments", {})
                if isinstance(args, dict):
                    arguments_str = json.dumps(args)
                else:
                    arguments_str = str(args)
                item = {
                    "id": f"fc_{uuid.uuid4().hex[:24]}",
                    "type": "function_call",
                    "status": "in_progress",
                    "name": payload.get("name", ""),
                    "call_id": call_id,
                    "arguments": arguments_str,
                }
                idx = output_index
                output_index += 1
                pending_tool_calls.append({
                    "call_id": call_id,
                    "name": payload.get("name", ""),
                    "arguments": arguments_str,
                    "item_id": item["id"],
                    "output_index": idx,
                })
                emitted_items.append({
                    "type": "function_call",
                    "name": payload.get("name", ""),
                    "arguments": arguments_str,
                    "call_id": call_id,
                })
                await _write_event("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": idx,
                    "item": item,
                })
                return call_id

            async def _emit_tool_completed(payload: Dict[str, Any]) -> None:
                """先发出 response.output_item.done（function_call），随后发出
                response.output_item.added（function_call_output）。"""
                nonlocal output_index
                call_id = payload.get("tool_call_id")
                result = payload.get("result", "")
                pending = None
                if call_id:
                    for i, p in enumerate(pending_tool_calls):
                        if p["call_id"] == call_id:
                            pending = pending_tool_calls.pop(i)
                            break
                if pending is None:
                    # 没有匹配的 start 就到达完成 —— 跳过，避免发出孤立的
                    # done 事件。
                    return

                # function_call 完成
                done_item = {
                    "id": pending["item_id"],
                    "type": "function_call",
                    "status": "completed",
                    "name": pending["name"],
                    "call_id": pending["call_id"],
                    "arguments": pending["arguments"],
                }
                await _write_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": pending["output_index"],
                    "item": done_item,
                })

                # function_call_output 已添加（结果）
                result_str = result if isinstance(result, str) else json.dumps(result)
                output_parts = [{"type": "input_text", "text": result_str}]
                output_item = {
                    "id": f"fco_{uuid.uuid4().hex[:24]}",
                    "type": "function_call_output",
                    "call_id": pending["call_id"],
                    "output": output_parts,
                    "status": "completed",
                }
                idx = output_index
                output_index += 1
                emitted_items.append({
                    "type": "function_call_output",
                    "call_id": pending["call_id"],
                    "output": output_parts,
                })
                await _write_event("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": idx,
                    "item": output_item,
                })
                await _write_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": idx,
                    "item": output_item,
                })

            # 主排空循环 —— 由 agent 回调馈送的线程安全队列。
            async def _dispatch(it) -> None:
                """把一个队列项路由到正确的 SSE 发射器。

                纯字符串是文本 delta —— 它们会被批处理（50ms），以减少
                Open WebUI 的重渲染风暴。带 ``__tool_started__`` /
                ``__tool_completed__`` 前缀的带标签元组是工具生命周期事件，
                会在发射前先刷新缓冲区。
                """
                nonlocal _batch_timer
                if isinstance(it, tuple) and len(it) == 2 and isinstance(it[0], str):
                    tag, payload = it
                    # 工具事件之前先刷新批处理的文本
                    if _batch_buf:
                        await _flush_batch()
                    if tag == "__tool_started__":
                        await _emit_tool_started(payload)
                    elif tag == "__tool_completed__":
                        await _emit_tool_completed(payload)
                elif isinstance(it, str):
                    # 批处理文本 delta —— 追加到缓冲区，按定时器刷新
                    _batch_buf.append(it)
                    if _batch_timer is None:
                        _batch_timer = asyncio.create_task(_batch_flush_after(0.05))
                # 其他类型被静默丢弃。

            # ── 批处理状态 ──
            _batch_buf: List[str] = []
            _batch_timer: Optional[asyncio.Task] = None
            _batch_lock = asyncio.Lock()

            async def _batch_flush_after(delay: float) -> None:
                """等待 delay 秒，然后刷新累积的文本 delta。"""
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return
                # 在刷新之前先清除定时器引用，使新的 delta 能在我们发射时
                # 启动一个全新的定时器
                nonlocal _batch_buf, _batch_timer
                _batch_timer = None
                await _flush_batch()

            async def _flush_batch() -> None:
                """为所有累积的文本发出单个 SSE delta。"""
                nonlocal _batch_buf
                async with _batch_lock:
                    if _batch_buf:
                        combined = "".join(_batch_buf)
                        _batch_buf = []
                        await _emit_text_delta(combined)

            loop = asyncio.get_running_loop()
            while True:
                try:
                    item = await loop.run_in_executor(None, lambda: stream_q.get(timeout=0.5))
                except _q.Empty:
                    if agent_task.done():
                        # 排空剩余项
                        while True:
                            try:
                                item = stream_q.get_nowait()
                                if item is None:
                                    break
                                await _dispatch(item)
                                last_activity = time.monotonic()
                            except _q.Empty:
                                break
                        break
                    if time.monotonic() - last_activity >= CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS:
                        await response.write(b": keepalive\n\n")
                        last_activity = time.monotonic()
                    continue

                if item is None:  # 流结束哨兵
                    # 取消待处理的定时器并刷新剩余的批处理文本
                    if _batch_timer and not _batch_timer.done():
                        _batch_timer.cancel()
                        _batch_timer = None
                    if _batch_buf:
                        await _flush_batch()
                    break

                await _dispatch(item)
                last_activity = time.monotonic()

            # 在处理结果之前刷新任何最后的批处理文本
            if _batch_buf:
                await _flush_batch()

            # 从已完成的任务中获取 agent 结果和 usage
            try:
                result, agent_usage = await agent_task
                usage = agent_usage or usage
                # 如果 agent 产生了 final_response 但没有流式发送文本 delta
                # （例如某些 provider 只在最后发出完整响应），则发出一个
                # 回退 delta，使 Responses 客户端仍能收到实时的文本部分。
                agent_final = result.get("final_response", "") if isinstance(result, dict) else ""
                if agent_final and not final_text_parts:
                    await _emit_text_delta(agent_final)
                if agent_final and not final_response_text:
                    final_response_text = agent_final
                if isinstance(result, dict) and result.get("error") and not final_response_text:
                    agent_error = result["error"]
            except Exception as e:  # noqa: BLE001
                logger.error("Error running agent for streaming responses: %s", e, exc_info=True)
                agent_error = str(e)

            # 如果消息 item 已经打开，则关闭它
            final_response_text = "".join(final_text_parts) or final_response_text
            if message_opened:
                await _write_event("response.output_text.done", {
                    "type": "response.output_text.done",
                    "item_id": message_item_id,
                    "output_index": message_output_index,
                    "content_index": 0,
                    "text": final_response_text,
                    "logprobs": [],
                })
                msg_done_item = {
                    "id": message_item_id,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": final_response_text}
                    ],
                }
                await _write_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": message_output_index,
                    "item": msg_done_item,
                })

            # 始终在 completed 的 response 信封中追加一个最终 message item，
            # 使只解析终止负载的客户端仍能看到 assistant 文本。这镜像了
            # 批处理路径中 _extract_output_items 所产生的形态。
            final_items: List[Dict[str, Any]] = list(emitted_items)

            # 裁剪工具调用 arguments 中的大块内容，使 response.completed
            # 事件保持在 ~100KB 以内。客户端已经通过增量事件收到了完整细节。
            for _item in final_items:
                if _item.get("type") == "function_call":
                    try:
                        _args = json.loads(_item.get("arguments", "{}")) if isinstance(_item.get("arguments"), str) else _item.get("arguments", {})
                        if isinstance(_args, dict):
                            for _k in ("content", "query", "pattern", "old_string", "new_string"):
                                if isinstance(_args.get(_k), str) and len(_args[_k]) > 500:
                                    _args[_k] = "[" + str(len(_args[_k])) + " chars — truncated for response.completed]"
                            _item["arguments"] = json.dumps(_args)
                    except Exception:
                        pass
                elif _item.get("type") == "function_call_output":
                    _output = _item.get("output", [])
                    if isinstance(_output, list) and _output:
                        _first = _output[0]
                        if isinstance(_first, dict) and _first.get("type") == "input_text":
                            _text = _first.get("text", "")
                            if len(_text) > 1000:
                                _first["text"] = _text[:500] + "...[" + str(len(_text) - 500) + " more chars]"
                                _item["output"] = [_first]

            final_items.append({
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": final_response_text or (agent_error or "")}
                ],
            })

            if agent_error:
                failed_env = _envelope("failed")
                failed_env["output"] = final_items
                failed_env["error"] = {"message": agent_error, "type": "server_error"}
                failed_env["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                _failed_history = list(conversation_history)
                _failed_history.append({"role": "user", "content": user_message})
                if final_response_text or agent_error:
                    _failed_history.append({
                        "role": "assistant",
                        "content": final_response_text or agent_error,
                    })
                _persist_response_snapshot(
                    failed_env,
                    conversation_history_snapshot=_failed_history,
                )
                terminal_snapshot_persisted = True
                await _write_event("response.failed", {
                    "type": "response.failed",
                    "response": failed_env,
                })
            else:
                completed_env = _envelope("completed")
                completed_env["output"] = final_items
                completed_env["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                full_history = self._build_response_conversation_history(
                    conversation_history,
                    user_message,
                    result,
                    final_response_text,
                )
                _persist_response_snapshot(
                    completed_env,
                    conversation_history_snapshot=full_history,
                )
                terminal_snapshot_persisted = True
                await _write_event("response.completed", {
                    "type": "response.completed",
                    "response": completed_env,
                })

        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            _persist_incomplete_if_needed()
            # 客户端断开连接 —— 打断 agent 使其停止发起上游 LLM 调用，
            # 然后取消任务。
            agent = agent_ref[0] if agent_ref else None
            if agent is not None:
                try:
                    agent.interrupt("SSE client disconnected")
                except Exception:
                    pass
            if not agent_task.done():
                agent_task.cancel()
                try:
                    await agent_task
                except (asyncio.CancelledError, Exception):
                    pass
            logger.info("SSE client disconnected; interrupted agent task %s", response_id)
        except asyncio.CancelledError:
            # 服务端取消（例如关机、请求超时）—— 持久化一份 incomplete 快照
            # 使 GET /v1/responses/{id} 和 previous_response_id 链式调用仍能
            # 工作，然后重新抛出，以遵守运行时的取消语义。
            _persist_incomplete_if_needed()
            agent = agent_ref[0] if agent_ref else None
            if agent is not None:
                try:
                    agent.interrupt("SSE task cancelled")
                except Exception:
                    pass
            if not agent_task.done():
                agent_task.cancel()
            logger.info("SSE task cancelled; persisted incomplete snapshot for %s", response_id)
            raise
        except Exception as _exc:
            # agent 因未处理的错误而崩溃（例如 BadRequestError、
            # AuthenticationError 之类的 model API 错误）。发出一个
            # response.failed 事件并正确终止 SSE 流，使客户端不会因为
            # 不完整的 chunked encoding 而收到 TransferEncodingError。
            import traceback as _tb
            _persist_incomplete_if_needed()
            agent_error = _tb.format_exc()
            try:
                failed_env = _envelope("failed")
                failed_env["output"] = list(emitted_items)
                failed_env["error"] = {"message": str(_exc)[:500], "type": "server_error"}
                failed_env["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                await _write_event("response.failed", {
                    "type": "response.failed",
                    "response": failed_env,
                })
            except Exception:
                pass
            logger.error("Agent crashed mid-stream for %s: %s", response_id, str(agent_error)[:300])

        return response

    async def _handle_responses(self, request: "web.Request") -> "web.Response":
        """POST /v1/responses —— OpenAI Responses API 格式。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # 限制在途 agent run 的总数（可配置；#7483）。
        limited = self._concurrency_limited_response()
        if limited is not None:
            return limited

        # 长期记忆作用域头部（详见 chat_completions）。
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err

        # 解析请求体
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(
                {"error": {"message": "Invalid JSON in request body", "type": "invalid_request_error"}},
                status=400,
            )

        raw_input = body.get("input")
        if raw_input is None:
            return web.json_response(_openai_error("Missing 'input' field"), status=400)

        instructions = body.get("instructions")
        previous_response_id = body.get("previous_response_id")
        conversation = body.get("conversation")
        store = _coerce_request_bool(body.get("store"), default=True)

        # conversation 和 previous_response_id 互斥
        if conversation and previous_response_id:
            return web.json_response(_openai_error("Cannot use both 'conversation' and 'previous_response_id'"), status=400)

        # 将 conversation 名称解析为最新的 response_id
        if conversation:
            previous_response_id = self._response_store.get_conversation(conversation)
            # 如果 conversation 尚不存在也不报错 —— 这是一个全新的 conversation

        # 将 input 规范化为消息列表
        input_messages: List[Dict[str, Any]] = []
        if isinstance(raw_input, str):
            input_messages = [{"role": "user", "content": raw_input}]
        elif isinstance(raw_input, list):
            for idx, item in enumerate(raw_input):
                if isinstance(item, str):
                    input_messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    role = item.get("role", "user")
                    try:
                        content = _normalize_multimodal_content(item.get("content", ""))
                    except ValueError as exc:
                        return _multimodal_validation_error(exc, param=f"input[{idx}].content")
                    input_messages.append({"role": role, "content": content})
        else:
            return web.json_response(_openai_error("'input' must be a string or array"), status=400)

        # 接受来自请求体的显式 conversation_history。
        # 这让无状态客户端可以提供自己的历史，而不必依赖通过
        # previous_response_id 进行的服务端响应链式拼接。
        # 优先级：显式 conversation_history > previous_response_id。
        conversation_history: List[Dict[str, Any]] = []
        raw_history = body.get("conversation_history")
        if raw_history:
            if not isinstance(raw_history, list):
                return web.json_response(
                    _openai_error("'conversation_history' must be an array of message objects"),
                    status=400,
                )
            for i, entry in enumerate(raw_history):
                if not isinstance(entry, dict) or "role" not in entry or "content" not in entry:
                    return web.json_response(
                        _openai_error(f"conversation_history[{i}] must have 'role' and 'content' fields"),
                        status=400,
                    )
                try:
                    entry_content = _normalize_multimodal_content(entry["content"])
                except ValueError as exc:
                    return _multimodal_validation_error(exc, param=f"conversation_history[{i}].content")
                conversation_history.append({"role": str(entry["role"]), "content": entry_content})
            if previous_response_id:
                logger.debug("Both conversation_history and previous_response_id provided; using conversation_history")

        stored_session_id = None
        if not conversation_history and previous_response_id:
            stored = self._response_store.get(previous_response_id)
            if stored is None:
                return web.json_response(_openai_error(f"Previous response not found: {previous_response_id}"), status=404)
            conversation_history = list(stored.get("conversation_history", []))
            stored_session_id = stored.get("session_id")
            # 如果未提供 instructions，则沿用上一次的
            if instructions is None:
                instructions = stored.get("instructions")

        # 将新的 input 消息追加到历史（除最后一条外其余都成为历史）
        for msg in input_messages[:-1]:
            conversation_history.append(msg)

        # 最后一条 input 消息即 user_message
        user_message: Any = input_messages[-1].get("content", "") if input_messages else ""
        if not _content_has_visible_payload(user_message):
            return web.json_response(_openai_error("No user message found in input"), status=400)

        # 截断支持
        if body.get("truncation") == "auto" and len(conversation_history) > 100:
            conversation_history = conversation_history[-100:]

        # 复用 previous_response_id 链上的 session，使 dashboard 把整段
        # 对话归到同一个 session 条目下。
        session_id = stored_session_id or str(uuid.uuid4())

        stream = _coerce_request_bool(body.get("stream"), default=False)
        if stream:
            # 流式分支 —— 在 agent 运行过程中发出 OpenAI Responses SSE
            # 事件，使前端能实时渲染文本 delta 和工具调用。详见
            # _write_sse_responses。
            import queue as _q
            _stream_q: _q.Queue = _q.Queue()

            def _on_delta(delta):
                # agent 的 None 是 CLI 的关闭响应框信号，而不是 EOS（流结束）。
                # 转发它会过早终止 SSE 流；SSE 写入端通过
                # agent_task.done() 检测完成。
                if delta is not None:
                    _stream_q.put(delta)

            def _on_tool_progress(event_type, name, preview, args, **kwargs):
                """如有需要，将来可在此排入非 start 的工具进度事件。

                结构化的 Responses 流使用 ``tool_start_callback`` 和
                ``tool_complete_callback`` 进行精确的 call-id 关联，因此
                进度事件目前在此被忽略。
                """
                return

            def _on_tool_start(tool_call_id, function_name, function_args):
                """将一个已开始的工具排入队列，用于实时的 function_call 流式传输。"""
                _stream_q.put(("__tool_started__", {
                    "tool_call_id": tool_call_id,
                    "name": function_name,
                    "arguments": function_args or {},
                }))

            def _on_tool_complete(tool_call_id, function_name, function_args, function_result):
                """将一个已完成的工具结果排入队列，用于实时的 function_call_output 流式传输。"""
                _stream_q.put(("__tool_completed__", {
                    "tool_call_id": tool_call_id,
                    "name": function_name,
                    "arguments": function_args or {},
                    "result": function_result,
                }))

            agent_ref = [None]
            agent_task = asyncio.ensure_future(self._run_agent(
                user_message=user_message,
                conversation_history=conversation_history,
                ephemeral_system_prompt=instructions,
                session_id=session_id,
                stream_delta_callback=_on_delta,
                tool_progress_callback=_on_tool_progress,
                tool_start_callback=_on_tool_start,
                tool_complete_callback=_on_tool_complete,
                agent_ref=agent_ref,
                gateway_session_key=gateway_session_key,
            ))
            # 确保 SSE 排空循环能够终止，而无需依赖轮询 agent_task.done()，
            # 后者可能与队列超时检查产生竞态。
            agent_task.add_done_callback(lambda _fut: _stream_q.put(None))

            response_id = f"resp_{uuid.uuid4().hex[:28]}"
            model_name = body.get("model", self._model_name)
            created_at = int(time.time())

            return await self._write_sse_responses(
                request=request,
                response_id=response_id,
                model=model_name,
                created_at=created_at,
                stream_q=_stream_q,
                agent_task=agent_task,
                agent_ref=agent_ref,
                conversation_history=conversation_history,
                user_message=user_message,
                instructions=instructions,
                conversation=conversation,
                store=store,
                session_id=session_id,
                gateway_session_key=gateway_session_key,
            )

        async def _compute_response():
            return await self._run_agent(
                user_message=user_message,
                conversation_history=conversation_history,
                ephemeral_system_prompt=instructions,
                session_id=session_id,
                gateway_session_key=gateway_session_key,
            )

        idempotency_key = request.headers.get("Idempotency-Key")
        if idempotency_key:
            fp = _make_request_fingerprint(
                body,
                keys=["input", "instructions", "previous_response_id", "conversation", "model", "tools"],
            )
            try:
                result, usage = await _idem_cache.get_or_set(idempotency_key, fp, _compute_response)
            except Exception as e:
                logger.error("Error running agent for responses: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )
        else:
            try:
                result, usage = await _compute_response()
            except Exception as e:
                logger.error("Error running agent for responses: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )

        final_response = result.get("final_response", "")
        if not final_response:
            final_response = result.get("error", "(No response generated)")

        response_id = f"resp_{uuid.uuid4().hex[:28]}"
        created_at = int(time.time())

        # 构造用于存储的完整对话历史
        # （包含 agent 运行产生的工具调用）
        full_history = self._build_response_conversation_history(
            conversation_history,
            user_message,
            result,
            final_response,
        )

        # 仅从当前轮次构造 output item。AIAgent 会在 result["messages"] 中
        # 返回完整 transcript，而较旧/被 mock 的路径可能只返回当前轮次的
        # 后缀。
        output_start_index = self._response_messages_turn_start_index(
            conversation_history,
            user_message,
            result,
        )
        output_items = self._extract_output_items(result, start_index=output_start_index)

        response_data = {
            "id": response_id,
            "object": "response",
            "status": "completed",
            "created_at": created_at,
            "model": body.get("model", self._model_name),
            "output": output_items,
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }

        # 存储完整的 response 对象，供将来链式拼接 / GET 检索使用
        if store:
            self._response_store.put(response_id, {
                "response": response_data,
                "conversation_history": full_history,
                "instructions": instructions,
                "session_id": session_id,
            })
            # 更新 conversation 映射，使下一个携带相同 conversation 名称的
            # 请求自动链式指向此响应
            if conversation:
                self._response_store.set_conversation(conversation, response_id)

        response_headers = {"X-Hermes-Session-Id": session_id}
        if gateway_session_key:
            response_headers["X-Hermes-Session-Key"] = gateway_session_key
        return web.json_response(response_data, headers=response_headers)

    # ------------------------------------------------------------------
    # GET / DELETE response 端点
    # ------------------------------------------------------------------

    async def _handle_get_response(self, request: "web.Request") -> "web.Response":
        """GET /v1/responses/{response_id} —— 获取一个已存储的响应。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        response_id = request.match_info["response_id"]
        stored = self._response_store.get(response_id)
        if stored is None:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        return web.json_response(stored["response"])

    async def _handle_delete_response(self, request: "web.Request") -> "web.Response":
        """DELETE /v1/responses/{response_id} —— 删除一个已存储的响应。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        response_id = request.match_info["response_id"]
        deleted = self._response_store.delete(response_id)
        if not deleted:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        return web.json_response({
            "id": response_id,
            "object": "response",
            "deleted": True,
        })

    # ------------------------------------------------------------------
    # Cron 任务 API
    # ------------------------------------------------------------------

    _JOB_ID_RE = __import__("re").compile(r"[a-f0-9]{12}")
    # 更新时允许的字段 —— 阻止客户端注入任意键
    _UPDATE_ALLOWED_FIELDS = {"name", "schedule", "prompt", "deliver", "skills", "skill", "repeat", "enabled"}
    _MAX_NAME_LENGTH = 200
    _MAX_PROMPT_LENGTH = 5000

    @staticmethod
    def _check_jobs_available() -> Optional["web.Response"]:
        """如果 cron 模块不可用则返回错误响应。"""
        if not _CRON_AVAILABLE:
            return web.json_response(
                {"error": "Cron module not available"}, status=501,
            )
        return None

    def _check_job_id(self, request: "web.Request") -> tuple:
        """校验并提取 job_id。返回 (job_id, error_response)。"""
        job_id = request.match_info["job_id"]
        if not self._JOB_ID_RE.fullmatch(job_id):
            logger.warning(
                "Cron jobs API rejected invalid job_id %r: %s",
                job_id,
                self._request_audit_log_suffix(request),
            )
            return job_id, web.json_response(
                {"error": "Invalid job ID format"}, status=400,
            )
        return job_id, None

    async def _handle_list_jobs(self, request: "web.Request") -> "web.Response":
        """GET /api/jobs —— 列出全部 cron 任务。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        try:
            include_disabled = request.query.get("include_disabled", "").lower() in {"true", "1"}
            jobs = _cron_list(include_disabled=include_disabled)
            return web.json_response({"jobs": jobs})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_create_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs —— 创建一个新的 cron 任务。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            schedule = (body.get("schedule") or "").strip()
            prompt = body.get("prompt", "")
            deliver = body.get("deliver", "local")
            skills = body.get("skills")
            repeat = body.get("repeat")

            if not name:
                return web.json_response({"error": "Name is required"}, status=400)
            if len(name) > self._MAX_NAME_LENGTH:
                return web.json_response(
                    {"error": f"Name must be ≤ {self._MAX_NAME_LENGTH} characters"}, status=400,
                )
            if not schedule:
                return web.json_response({"error": "Schedule is required"}, status=400)
            if len(prompt) > self._MAX_PROMPT_LENGTH:
                return web.json_response(
                    {"error": f"Prompt must be ≤ {self._MAX_PROMPT_LENGTH} characters"}, status=400,
                )
            if prompt and _scan_cron_prompt is not None:
                scan_error = _scan_cron_prompt(prompt)
                if scan_error:
                    return web.json_response({"error": scan_error}, status=400)
            if repeat is not None and (not isinstance(repeat, int) or repeat < 1):
                return web.json_response({"error": "Repeat must be a positive integer"}, status=400)

            kwargs = {
                "prompt": prompt,
                "schedule": schedule,
                "name": name,
                "deliver": deliver,
                "origin": self._cron_origin_from_request(request),
            }
            if skills:
                kwargs["skills"] = skills
            if repeat is not None:
                kwargs["repeat"] = repeat

            job = _cron_create(**kwargs)
            _notify_cron_provider_jobs_changed()
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_get_job(self, request: "web.Request") -> "web.Response":
        """GET /api/jobs/{job_id} —— 获取单个 cron 任务。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = _cron_get(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_update_job(self, request: "web.Request") -> "web.Response":
        """PATCH /api/jobs/{job_id} —— 更新一个 cron 任务。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            body = await request.json()
            # 白名单允许的字段，防止任意键注入
            sanitized = {k: v for k, v in body.items() if k in self._UPDATE_ALLOWED_FIELDS}
            if not sanitized:
                return web.json_response({"error": "No valid fields to update"}, status=400)
            # 如果存在则校验长度
            if "name" in sanitized and len(sanitized["name"]) > self._MAX_NAME_LENGTH:
                return web.json_response(
                    {"error": f"Name must be ≤ {self._MAX_NAME_LENGTH} characters"}, status=400,
                )
            if "prompt" in sanitized and len(sanitized["prompt"]) > self._MAX_PROMPT_LENGTH:
                return web.json_response(
                    {"error": f"Prompt must be ≤ {self._MAX_PROMPT_LENGTH} characters"}, status=400,
                )
            if sanitized.get("prompt") and _scan_cron_prompt is not None:
                scan_error = _scan_cron_prompt(sanitized["prompt"])
                if scan_error:
                    return web.json_response({"error": scan_error}, status=400)
            job = _cron_update(job_id, sanitized)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            _notify_cron_provider_jobs_changed()
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_delete_job(self, request: "web.Request") -> "web.Response":
        """DELETE /api/jobs/{job_id} —— 删除一个 cron 任务。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            success = _cron_remove(job_id)
            if not success:
                return web.json_response({"error": "Job not found"}, status=404)
            _notify_cron_provider_jobs_changed()
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_pause_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/pause —— 暂停一个 cron 任务。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = _cron_pause(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            _notify_cron_provider_jobs_changed()
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_resume_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/resume —— 恢复一个已暂停的 cron 任务。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = _cron_resume(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            _notify_cron_provider_jobs_changed()
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_run_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/run —— 触发立即执行。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = _cron_trigger(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_cron_fire(self, request: "web.Request") -> "web.Response":
        """POST /api/cron/fire —— Chronos 托管 cron 的 fire webhook（NAS → agent）。

        由 NAS 签发的 JWT（通过可插拔的 fire-verifier 校验）鉴权，而
        非使用 API_SERVER_KEY —— NAS 不持有任何 API 服务器密钥，并且这是
        唯一能够触发远程任务执行的入站请求，因此它有自己专用作用域的
        token 校验。

        返回 202 并在后台运行任务，使一次漫长的 agent 轮次永远不会触发
        NAS 的 HTTP 超时。fire_due 内部的 store CAS 认领可防止在
        NAS/调度器重试时重复触发。
        """
        from hermes_cli.config import cfg_get, load_config
        from plugins.cron_providers.chronos.verify import get_fire_verifier

        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""

        cfg = load_config()
        claims = get_fire_verifier()(
            token=token,
            expected_audience=cfg_get(cfg, "cron", "chronos", "expected_audience", default=""),
            jwks_or_key=cfg_get(cfg, "cron", "chronos", "nas_jwks_url", default="") or None,
            issuer=cfg_get(cfg, "cron", "chronos", "portal_url", default="") or None,
        )
        if claims is None:
            logger.warning(
                "cron fire: rejected invalid token: %s",
                self._request_audit_log_suffix(request),
            )
            return web.json_response({"error": "invalid fire token"}, status=401)

        try:
            body = await request.json()
        except Exception:
            body = {}
        job_id = (body or {}).get("job_id")
        if not job_id:
            return web.json_response({"error": "missing job_id"}, status=400)

        from cron.scheduler_provider import resolve_cron_scheduler
        provider = resolve_cron_scheduler()

        loop = asyncio.get_running_loop()
        # 在后台触发（立即返回 202）。fire_due 通过 store CAS 认领，
        # 因此在途期间的重试会被去重。
        task = asyncio.create_task(
            asyncio.to_thread(provider.fire_due, job_id, adapters=None, loop=loop)
        )
        try:
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except (TypeError, AttributeError):
            pass

        return web.json_response({"status": "accepted", "job_id": job_id}, status=202)


    # ------------------------------------------------------------------
    # 输出抽取辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _build_response_conversation_history(
        conversation_history: List[Dict[str, Any]],
        user_message: Any,
        result: Dict[str, Any],
        final_response: Any,
    ) -> List[Dict[str, Any]]:
        """构造存储用的 Responses transcript，且不重复历史。"""
        prior = list(conversation_history)
        current_user = {"role": "user", "content": user_message}
        agent_messages = result.get("messages") if isinstance(result, dict) else None

        if isinstance(agent_messages, list) and agent_messages:
            turn_start = APIServerAdapter._response_messages_turn_start_index(
                conversation_history,
                user_message,
                result,
            )
            if turn_start:
                return list(agent_messages)

            full_history = prior
            full_history.append(current_user)
            full_history.extend(agent_messages)
            return full_history

        full_history = prior
        full_history.append(current_user)
        full_history.append({"role": "assistant", "content": final_response})
        return full_history

    @staticmethod
    def _response_messages_turn_start_index(
        conversation_history: List[Dict[str, Any]],
        user_message: Any,
        result: Dict[str, Any],
    ) -> int:
        """检测 transcript 形态的 result["messages"] 并返回该轮的起始位置。"""
        agent_messages = result.get("messages") if isinstance(result, dict) else None
        if not isinstance(agent_messages, list) or not agent_messages:
            return 0

        prior = list(conversation_history)
        current_user = {"role": "user", "content": user_message}
        expected_prefix = prior + [current_user]
        if agent_messages[:len(expected_prefix)] == expected_prefix:
            return len(expected_prefix)
        if prior and agent_messages[:len(prior)] == prior:
            return len(prior)
        return 0

    @classmethod
    def _turn_transcript_messages(
        cls,
        conversation_history: List[Dict[str, Any]],
        user_message: Any,
        result: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """以对客户端安全的形态返回本轮的 assistant/工具消息。

        流式 SSE 契约把所有 assistant 文本作为同一个 ``message_id`` 下的
        ``assistant.delta`` 事件发送，并与 ``tool.*`` 事件交织，最后由一个
        只携带最终回复的 ``assistant.completed`` 收尾。把 delta 累积进单个
        缓冲区的客户端无法重建工具调用 *之前* 的 *中间* assistant 文本片段
        —— 因此在流传输中途/之后重新打开页面时，这些片段看起来像是丢失了，
        尽管 state.db 已正确持久化它们。

        在 ``run.completed`` 上发出权威的逐轮 transcript，使任何 SSE 消费方
        都能将自己的实时视图与事实基准核对，而无需单独往返一次
        ``GET /messages``。纯增量：忽略该字段的客户端不受影响。参见 #34703。
        """
        agent_messages = result.get("messages") if isinstance(result, dict) else None
        if not isinstance(agent_messages, list) or not agent_messages:
            return []
        start = cls._response_messages_turn_start_index(
            conversation_history, user_message, result
        )
        turn = agent_messages[start:]
        out: List[Dict[str, Any]] = []
        for msg in turn:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") not in {"assistant", "tool"}:
                continue
            out.append(cls._message_response(msg))
        return out

    @staticmethod
    def _extract_output_items(result: Dict[str, Any], start_index: int = 0) -> List[Dict[str, Any]]:
        """
        根据 agent 的 messages 构造 output item 数组。

        从 *start_index* 起遍历 *result["messages"]*，并发出：
        - 为 assistant 消息上的每个 tool_call 发出 ``function_call`` 项
        - 为每个 tool 角色的消息发出 ``function_call_output`` 项
        - 发出一个携带 assistant 文本回复的最终 ``message`` 项
        """
        items: List[Dict[str, Any]] = []
        messages = result.get("messages", [])
        if start_index > 0:
            messages = messages[start_index:]

        for msg in messages:
            role = msg.get("role")
            if role == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    items.append({
                        "type": "function_call",
                        "name": func.get("name", ""),
                        "arguments": func.get("arguments", ""),
                        "call_id": tc.get("id", ""),
                    })
            elif role == "tool":
                items.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": msg.get("content", ""),
                })

        # 最终的 assistant 消息
        final = result.get("final_response", "")
        if not final:
            final = result.get("error", "(No response generated)")

        items.append({
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": final,
                }
            ],
        })
        return items

    # ------------------------------------------------------------------
    # Agent 执行
    # ------------------------------------------------------------------

    def _concurrency_limited_response(self) -> Optional["web.Response"]:
        """如果达到并发 run 上限则返回 429 响应，否则返回 None。

        该上限约束跨所有 agent 服务端点的在途 agent 活动总量：非流式的
        chat/responses 路径（由 ``_inflight_agent_runs`` 跟踪）加上
        ``/v1/runs`` 流式路径（由 ``_run_streams`` 跟踪）。配置值为 0 时
        完全禁用该上限。
        """
        limit = self._max_concurrent_runs
        if limit <= 0:
            return None
        inflight = self._inflight_agent_runs + len(self._run_streams)
        if inflight >= limit:
            return web.json_response(
                _openai_error(
                    f"Too many concurrent runs (max {limit})",
                    err_type="rate_limit_error",
                    code="rate_limit_exceeded",
                ),
                status=429,
                headers={"Retry-After": "1"},
            )
        return None

    @staticmethod
    def _bind_api_server_session(
        *,
        chat_id: str = "",
        session_key: str = "",
        session_id: str = "",
    ) -> list:
        """为一次 API 服务器的 agent 运行绑定 session contextvar。

        这是每个 API 服务器 agent 入口路径都必须用来初始化 session 上下文的
        唯一结构性咽喉点 —— 它硬编码 ``platform="api_server"`` 和
        ``async_delivery=False``，使新路由在物理上不可能因为忘记把 channel
        标记为非投递型而重新引入那个静默空操作的 bug（#10760）。这里没有
        ``async_delivery`` 参数可供写错；无状态的 HTTP 路径在任何路由上都
        永远无法在一轮结束后唤醒 agent。

        返回重置 token；请在 ``finally`` 块中将其传给
        ``clear_session_vars``（该绑定是请求作用域的，不得超出该轮的生存期
        —— 之后在投递型接口（例如 CLI 或 gateway 平台）上恢复的 session 会
        重新绑定，且不会被阻塞）。
        """
        from gateway.session_context import set_session_vars

        return set_session_vars(
            platform="api_server",
            chat_id=chat_id,
            session_key=session_key,
            session_id=session_id,
            async_delivery=False,
        )

    async def _run_agent(
        self,
        user_message: str,
        conversation_history: List[Dict[str, str]],
        ephemeral_system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        stream_delta_callback=None,
        tool_progress_callback=None,
        tool_start_callback=None,
        tool_complete_callback=None,
        agent_ref: Optional[list] = None,
        gateway_session_key: Optional[str] = None,
    ) -> tuple:
        """
        创建一个 agent，并在一个线程 executor 中运行一次对话。

        返回 ``(result_dict, usage_dict)``，其中 *usage_dict* 包含
        ``input_tokens``、``output_tokens`` 和 ``total_tokens``。

        如果 *agent_ref* 是单元素列表，则 AIAgent 实例会在
        ``run_conversation`` 开始之前被存入 ``agent_ref[0]``。这使调用方
        （例如 SSE 写入端）能够从另一个线程调用 ``agent.interrupt()`` 来
        停止进行中的 LLM 调用。
        """
        loop = asyncio.get_running_loop()

        def _run():
            from gateway.session_context import clear_session_vars

            tokens = self._bind_api_server_session(
                chat_id=session_id or "",
                session_key=gateway_session_key or session_id or "",
                session_id=session_id or "",
            )
            try:
                agent = self._create_agent(
                    ephemeral_system_prompt=ephemeral_system_prompt,
                    session_id=session_id,
                    stream_delta_callback=stream_delta_callback,
                    tool_progress_callback=tool_progress_callback,
                    tool_start_callback=tool_start_callback,
                    tool_complete_callback=tool_complete_callback,
                    gateway_session_key=gateway_session_key,
                )
                if agent_ref is not None:
                    agent_ref[0] = agent
                effective_task_id = session_id or str(uuid.uuid4())
                result = agent.run_conversation(
                    user_message=user_message,
                    conversation_history=conversation_history,
                    task_id=effective_task_id,
                )
                usage = {
                    "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                    "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                    "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
                }
                # 将有效的 session ID 包含在结果里，使调用方
                # （例如 X-Hermes-Session-Id 头部）能够跟踪因压缩触发的
                # session 轮换。（#16938）
                _eff_sid = getattr(agent, "session_id", session_id)
                if isinstance(_eff_sid, str) and _eff_sid:
                    result["session_id"] = _eff_sid
                return result, usage
            finally:
                clear_session_vars(tokens)

        self._inflight_agent_runs += 1
        try:
            return await loop.run_in_executor(None, _run)
        finally:
            self._inflight_agent_runs -= 1

    # ------------------------------------------------------------------
    # /v1/runs —— 结构化事件流
    # ------------------------------------------------------------------

    _RUN_STREAM_TTL = 300  # 孤立 run 被清扫前的秒数
    _RUN_STATUS_TTL = 3600  # 为轮询保留终止 run 状态的秒数

    def _set_run_status(self, run_id: str, status: str, **fields: Any) -> Dict[str, Any]:
        """更新可轮询的 run 状态，且不暴露私有的 agent 对象。"""
        now = time.time()
        current = self._run_statuses.get(run_id, {})
        current.update({
            "object": "hermes.run",
            "run_id": run_id,
            "status": status,
            "updated_at": now,
        })
        current.setdefault("created_at", fields.pop("created_at", now))
        current.update(fields)
        self._run_statuses[run_id] = current
        return current

    def _make_run_event_callback(self, run_id: str, loop: "asyncio.AbstractEventLoop"):
        """返回一个 tool_progress_callback，把结构化事件推送到该 run 的 SSE 队列。"""
        def _push(event: Dict[str, Any]) -> None:
            self._set_run_status(
                run_id,
                self._run_statuses.get(run_id, {}).get("status", "running"),
                last_event=event.get("event"),
            )
            q = self._run_streams.get(run_id)
            if q is None:
                return
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except Exception:
                pass

        def _callback(event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs):
            ts = time.time()
            if event_type == "tool.started":
                _push({
                    "event": "tool.started",
                    "run_id": run_id,
                    "timestamp": ts,
                    "tool": tool_name,
                    "preview": preview,
                })
            elif event_type == "tool.completed":
                _push({
                    "event": "tool.completed",
                    "run_id": run_id,
                    "timestamp": ts,
                    "tool": tool_name,
                    "duration": round(kwargs.get("duration", 0), 3),
                    "error": kwargs.get("is_error", False),
                })
            elif event_type == "reasoning.available":
                _push({
                    "event": "reasoning.available",
                    "run_id": run_id,
                    "timestamp": ts,
                    "text": preview or "",
                })
            # _thinking 和 subagent_progress 有意不转发

        return _callback

    async def _handle_runs(self, request: "web.Request") -> "web.Response":
        """POST /v1/runs —— 启动一个 agent run，立即返回 run_id。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        # 长期记忆作用域头部（详见 chat_completions）。
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err

        # 强制并发上限（跨所有 agent 服务端点共享；可通过
        # gateway.api_server.max_concurrent_runs 配置）。
        limited = self._concurrency_limited_response()
        if limited is not None:
            return limited

        try:
            body = await request.json()
        except Exception:
            return web.json_response(_openai_error("Invalid JSON"), status=400)

        raw_input = body.get("input")
        if not raw_input:
            return web.json_response(_openai_error("Missing 'input' field"), status=400)

        user_message = raw_input if isinstance(raw_input, str) else (raw_input[-1].get("content", "") if isinstance(raw_input, list) else "")
        if not user_message:
            return web.json_response(_openai_error("No user message found in input"), status=400)

        instructions = body.get("instructions")
        previous_response_id = body.get("previous_response_id")

        # 接受来自请求体的显式 conversation_history。
        # 优先级：显式 conversation_history > previous_response_id。
        conversation_history: List[Dict[str, str]] = []
        raw_history = body.get("conversation_history")
        if raw_history:
            if not isinstance(raw_history, list):
                return web.json_response(
                    _openai_error("'conversation_history' must be an array of message objects"),
                    status=400,
                )
            for i, entry in enumerate(raw_history):
                if not isinstance(entry, dict) or "role" not in entry or "content" not in entry:
                    return web.json_response(
                        _openai_error(f"conversation_history[{i}] must have 'role' and 'content' fields"),
                        status=400,
                    )
                conversation_history.append({"role": str(entry["role"]), "content": str(entry["content"])})
            if previous_response_id:
                logger.debug("Both conversation_history and previous_response_id provided; using conversation_history")

        stored_session_id = None
        if not conversation_history and previous_response_id:
            stored = self._response_store.get(previous_response_id)
            if stored:
                conversation_history = list(stored.get("conversation_history", []))
                stored_session_id = stored.get("session_id")
                if instructions is None:
                    instructions = stored.get("instructions")

        # 当 input 是多消息数组时，将除最后一条外的所有消息抽取为对话历史
        # （最后一条成为 user_message）。仅在未提供显式历史时触发。
        if not conversation_history and isinstance(raw_input, list) and len(raw_input) > 1:
            for msg in raw_input[:-1]:
                if isinstance(msg, dict) and msg.get("role") and msg.get("content"):
                    content = msg["content"]
                    if isinstance(content, list):
                        # 将多部分内容块拍平为文本
                        content = " ".join(
                            part.get("text", "") for part in content
                            if isinstance(part, dict) and part.get("type") == "text"
                        )
                    conversation_history.append({"role": msg["role"], "content": str(content)})

        run_id = f"run_{uuid.uuid4().hex}"
        session_id = body.get("session_id") or stored_session_id or run_id
        approval_session_key = gateway_session_key or session_id or run_id
        ephemeral_system_prompt = instructions
        loop = asyncio.get_running_loop()
        q: "asyncio.Queue[Optional[Dict]]" = asyncio.Queue()
        created_at = time.time()
        self._run_streams[run_id] = q
        self._run_streams_created[run_id] = created_at
        self._run_approval_sessions[run_id] = approval_session_key

        event_cb = self._make_run_event_callback(run_id, loop)

        # 同时接入 stream_delta_callback，使 message.delta 事件得以流通。
        def _text_cb(delta: Optional[str]) -> None:
            if delta is None:
                return
            try:
                loop.call_soon_threadsafe(q.put_nowait, {
                    "event": "message.delta",
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "delta": delta,
                })
            except Exception:
                pass

        self._set_run_status(
            run_id,
            "queued",
            created_at=created_at,
            session_id=session_id,
            model=body.get("model", self._model_name),
        )

        async def _run_and_close():
            try:
                self._set_run_status(run_id, "running")
                agent = self._create_agent(
                    ephemeral_system_prompt=ephemeral_system_prompt,
                    session_id=session_id,
                    stream_delta_callback=_text_cb,
                    tool_progress_callback=event_cb,
                    gateway_session_key=gateway_session_key,
                )
                self._active_run_agents[run_id] = agent

                def _approval_notify(approval_data: Dict[str, Any]) -> None:
                    event = dict(approval_data or {})
                    # 在命令进入 SSE/API 事件流之前对其中的凭据进行打码 —— 与
                    # #48456 同一类出口 bug，只是换了传输通道：否则 API/桌面
                    # 客户端会收到 Tirith 标记的原始命令。复用 gateway 的接缝。
                    if "command" in event:
                        from gateway.run import _redact_approval_command

                        event["command"] = _redact_approval_command(event.get("command"))
                    event.update({
                        "event": "approval.request",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "choices": ["once", "session", "always", "deny"],
                    })
                    self._set_run_status(
                        run_id,
                        "waiting_for_approval",
                        last_event="approval.request",
                    )
                    try:
                        loop.call_soon_threadsafe(q.put_nowait, event)
                    except Exception:
                        pass

                def _run_sync():
                    from gateway.session_context import clear_session_vars
                    from tools.approval import (
                        register_gateway_notify,
                        reset_current_session_key,
                        set_current_session_key,
                        unregister_gateway_notify,
                    )

                    effective_task_id = session_id or run_id
                    approval_token = None
                    session_tokens = []
                    try:
                        # 通过 contextvar 为本次 API run 绑定审批/session 身份，
                        # 使并发 run 不共享进程级的环境状态。
                        approval_token = set_current_session_key(approval_session_key)
                        session_tokens = self._bind_api_server_session(
                            session_key=approval_session_key,
                        )
                        register_gateway_notify(approval_session_key, _approval_notify)
                        r = agent.run_conversation(
                            user_message=user_message,
                            conversation_history=conversation_history,
                            task_id=effective_task_id,
                        )
                    finally:
                        try:
                            unregister_gateway_notify(approval_session_key)
                        finally:
                            if approval_token is not None:
                                try:
                                    reset_current_session_key(approval_token)
                                except Exception:
                                    pass
                            if session_tokens:
                                try:
                                    clear_session_vars(session_tokens)
                                except Exception:
                                    pass
                    u = {
                        "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                        "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                        "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
                    }
                    return r, u

                result, usage = await asyncio.get_running_loop().run_in_executor(None, _run_sync)
                # 检查结构化失败（像 401/400 这类不可重试的客户端错误会返回
                # failed=True 而不是抛出异常，因此下方的 except 块永远不会触发
                # —— #15561）。
                if isinstance(result, dict) and result.get("failed"):
                    error_msg = result.get("error") or "agent run failed"
                    q.put_nowait({
                        "event": "run.failed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "error": error_msg,
                    })
                    self._set_run_status(
                        run_id,
                        "failed",
                        error=error_msg,
                        last_event="run.failed",
                    )
                else:
                    final_response = result.get("final_response", "") if isinstance(result, dict) else ""
                    q.put_nowait({
                        "event": "run.completed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "output": final_response,
                        "usage": usage,
                    })
                    self._set_run_status(
                        run_id,
                        "completed",
                        output=final_response,
                        usage=usage,
                        last_event="run.completed",
                    )
            except asyncio.CancelledError:
                self._set_run_status(
                    run_id,
                    "cancelled",
                    last_event="run.cancelled",
                )
                try:
                    q.put_nowait({
                        "event": "run.cancelled",
                        "run_id": run_id,
                        "timestamp": time.time(),
                    })
                except Exception:
                    pass
                raise
            except Exception as exc:
                logger.exception("[api_server] run %s failed", run_id)
                self._set_run_status(
                    run_id,
                    "failed",
                    error=str(exc),
                    last_event="run.failed",
                )
                try:
                    q.put_nowait({
                        "event": "run.failed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "error": str(exc),
                    })
                except Exception:
                    pass
            finally:
                # 如果 asyncio 包装器被取消（例如通过 /stop），executor 线程
                # 仍可能阻塞在等待一个 approval Event 上。在此反注册可以立即
                # 释放这些等待；线程内的反注册在正常完成时是幂等的、无害的。
                try:
                    from tools.approval import unregister_gateway_notify

                    unregister_gateway_notify(approval_session_key)
                except Exception:
                    pass
                # 哨兵：通知 SSE 流关闭
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
                self._active_run_agents.pop(run_id, None)
                self._active_run_tasks.pop(run_id, None)
                self._run_approval_sessions.pop(run_id, None)

        task = asyncio.create_task(_run_and_close())
        self._active_run_tasks[run_id] = task
        try:
            self._background_tasks.add(task)
        except TypeError:
            pass
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)

        response_headers = (
            {"X-Hermes-Session-Key": gateway_session_key} if gateway_session_key else {}
        )
        return web.json_response(
            {"run_id": run_id, "status": "started"},
            status=202,
            headers=response_headers,
        )

    async def _handle_get_run(self, request: "web.Request") -> "web.Response":
        """GET /v1/runs/{run_id} —— 为外部 UI 返回可轮询的 run 状态。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        status = self._run_statuses.get(run_id)
        if status is None:
            return web.json_response(
                _openai_error(f"Run not found: {run_id}", code="run_not_found"),
                status=404,
            )
        return web.json_response(status)

    async def _handle_run_events(self, request: "web.Request") -> "web.StreamResponse":
        """GET /v1/runs/{run_id}/events —— 结构化 agent 生命周期事件的 SSE 流。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]

        # 允许在 run 注册之前稍早订阅（竞态条件窗口）
        for _ in range(20):
            if run_id in self._run_streams:
                break
            await asyncio.sleep(0.05)
        else:
            return web.json_response(_openai_error(f"Run not found: {run_id}", code="run_not_found"), status=404)

        q = self._run_streams[run_id]

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
                    continue
                if event is None:
                    # run 结束 —— 发送最后的 SSE 注释并关闭
                    await response.write(b": stream closed\n\n")
                    break
                payload = f"data: {json.dumps(event)}\n\n"
                await response.write(payload.encode())
        except Exception as exc:
            logger.debug("[api_server] SSE stream error for run %s: %s", run_id, exc)
        finally:
            self._run_streams.pop(run_id, None)
            self._run_streams_created.pop(run_id, None)

        return response


    async def _handle_run_approval(self, request: "web.Request") -> "web.Response":
        """POST /v1/runs/{run_id}/approval —— 解决一个待处理的 run 审批。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        status = self._run_statuses.get(run_id)
        if status is None:
            return web.json_response(
                _openai_error(f"Run not found: {run_id}", code="run_not_found"),
                status=404,
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response(_openai_error("Invalid JSON"), status=400)

        raw_choice = str(body.get("choice", "")).strip().lower()
        aliases = {"approve": "once", "approved": "once", "allow": "once"}
        choice = aliases.get(raw_choice, raw_choice)
        allowed = {"once", "session", "always", "deny"}
        if choice not in allowed:
            return web.json_response(
                _openai_error(
                    "Invalid approval choice; expected one of: once, session, always, deny",
                    code="invalid_approval_choice",
                ),
                status=400,
            )

        approval_session_key = self._run_approval_sessions.get(run_id)
        if not approval_session_key:
            return web.json_response(
                _openai_error(
                    f"Run has no active approval session: {run_id}",
                    code="approval_not_active",
                ),
                status=409,
            )

        resolve_all = (
            _coerce_request_bool(body.get("all"), default=False)
            or _coerce_request_bool(body.get("resolve_all"), default=False)
        )
        try:
            from tools.approval import resolve_gateway_approval

            resolved = resolve_gateway_approval(
                approval_session_key,
                choice,
                resolve_all=resolve_all,
            )
        except Exception as exc:
            logger.exception("[api_server] approval resolution failed for run %s", run_id)
            return web.json_response(_openai_error(str(exc)), status=500)

        if resolved <= 0:
            return web.json_response(
                _openai_error(
                    f"Run has no pending approval: {run_id}",
                    code="approval_not_pending",
                ),
                status=409,
            )

        self._set_run_status(run_id, "running", last_event="approval.responded")
        q = self._run_streams.get(run_id)
        if q is not None:
            try:
                q.put_nowait({
                    "event": "approval.responded",
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "choice": choice,
                    "resolved": resolved,
                })
            except Exception:
                pass

        return web.json_response({
            "object": "hermes.run.approval_response",
            "run_id": run_id,
            "choice": choice,
            "resolved": resolved,
        })

    async def _handle_stop_run(self, request: "web.Request") -> "web.Response":
        """POST /v1/runs/{run_id}/stop —— 中断正在运行的 agent。"""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        agent = self._active_run_agents.get(run_id)
        task = self._active_run_tasks.get(run_id)

        if agent is None and task is None:
            return web.json_response(_openai_error(f"Run not found: {run_id}", code="run_not_found"), status=404)

        self._set_run_status(run_id, "stopping", last_event="run.stopping")

        if agent is not None:
            try:
                agent.interrupt("Stop requested via API")
            except Exception:
                pass

        if task is not None and not task.done():
            task.cancel()
            # 有界等待：run_conversation() 在默认 executor 线程中执行，
            # task.cancel() 无法抢占它 —— 我们依赖上方的
            # agent.interrupt() 来打断循环。给等待设上限，避免一个缓慢/
            # 无响应的 interrupt 把这个 handler 挂死。
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "[api_server] stop for run %s timed out after 5s; "
                    "agent may still be finishing the current step",
                    run_id,
                )
            except (asyncio.CancelledError, Exception):
                pass

        return web.json_response({"run_id": run_id, "status": "stopping"})

    async def _sweep_orphaned_runs(self) -> None:
        """定期清理从未被消费的 run 流。"""
        while True:
            await asyncio.sleep(60)
            now = time.time()
            stale = [
                run_id
                for run_id, created_at in list(self._run_streams_created.items())
                if now - created_at > self._RUN_STREAM_TTL
            ]
            for run_id in stale:
                logger.debug("[api_server] sweeping orphaned run %s", run_id)
                try:
                    from tools.approval import unregister_gateway_notify

                    approval_session_key = self._run_approval_sessions.get(run_id)
                    if approval_session_key:
                        unregister_gateway_notify(approval_session_key)
                except Exception:
                    pass
                self._run_streams.pop(run_id, None)
                self._run_streams_created.pop(run_id, None)
                self._active_run_agents.pop(run_id, None)
                self._active_run_tasks.pop(run_id, None)
                self._run_approval_sessions.pop(run_id, None)

            stale_statuses = [
                run_id
                for run_id, status in list(self._run_statuses.items())
                if status.get("status") in {"completed", "failed", "cancelled"}
                and now - float(status.get("updated_at", 0) or 0) > self._RUN_STATUS_TTL
            ]
            for run_id in stale_statuses:
                self._run_statuses.pop(run_id, None)

    # ------------------------------------------------------------------
    # BasePlatformAdapter 接口
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """启动 aiohttp web 服务器。"""
        if not AIOHTTP_AVAILABLE:
            logger.warning("[%s] aiohttp not installed", self.name)
            return False

        try:
            mws = [mw for mw in (cors_middleware, body_limit_middleware, security_headers_middleware) if mw is not None]
            self._app = web.Application(middlewares=mws, client_max_size=MAX_REQUEST_BYTES)
            assert self._app is not None
            self._app.router.add_get("/health", self._handle_health)
            self._app.router.add_get("/health/detailed", self._handle_health_detailed)
            self._app.router.add_get("/v1/health", self._handle_health)
            self._app.router.add_get("/v1/models", self._handle_models)
            self._app.router.add_get("/v1/capabilities", self._handle_capabilities)
            self._app.router.add_get("/v1/skills", self._handle_skills)
            self._app.router.add_get("/v1/toolsets", self._handle_toolsets)
            # Session/客户端控制面（SessionDB + _run_agent 的薄封装）
            self._app.router.add_get("/api/sessions", self._handle_list_sessions)
            self._app.router.add_post("/api/sessions", self._handle_create_session)
            self._app.router.add_get("/api/sessions/{session_id}", self._handle_get_session)
            self._app.router.add_patch("/api/sessions/{session_id}", self._handle_patch_session)
            self._app.router.add_delete("/api/sessions/{session_id}", self._handle_delete_session)
            self._app.router.add_get("/api/sessions/{session_id}/messages", self._handle_session_messages)
            self._app.router.add_post("/api/sessions/{session_id}/fork", self._handle_fork_session)
            self._app.router.add_post("/api/sessions/{session_id}/chat", self._handle_session_chat)
            self._app.router.add_post("/api/sessions/{session_id}/chat/stream", self._handle_session_chat_stream)
            self._app.router.add_post("/v1/chat/completions", self._handle_chat_completions)
            self._app.router.add_post("/v1/responses", self._handle_responses)
            self._app.router.add_get("/v1/responses/{response_id}", self._handle_get_response)
            self._app.router.add_delete("/v1/responses/{response_id}", self._handle_delete_response)
            # Cron 任务管理 API
            self._app.router.add_get("/api/jobs", self._handle_list_jobs)
            self._app.router.add_post("/api/jobs", self._handle_create_job)
            self._app.router.add_get("/api/jobs/{job_id}", self._handle_get_job)
            self._app.router.add_patch("/api/jobs/{job_id}", self._handle_update_job)
            self._app.router.add_delete("/api/jobs/{job_id}", self._handle_delete_job)
            self._app.router.add_post("/api/jobs/{job_id}/pause", self._handle_pause_job)
            self._app.router.add_post("/api/jobs/{job_id}/resume", self._handle_resume_job)
            self._app.router.add_post("/api/jobs/{job_id}/run", self._handle_run_job)

            # Chronos 托管 cron 的 fire webhook（NAS → agent）。由 NAS 签发
            # 的 JWT 鉴权（而非 API_SERVER_KEY），因此它有自己的鉴权路径。
            if _CRON_AVAILABLE:
                self._app.router.add_post("/api/cron/fire", self._handle_cron_fire)
            # 结构化事件流
            self._app.router.add_post("/v1/runs", self._handle_runs)
            self._app.router.add_get("/v1/runs/{run_id}", self._handle_get_run)
            self._app.router.add_get("/v1/runs/{run_id}/events", self._handle_run_events)
            self._app.router.add_post("/v1/runs/{run_id}/approval", self._handle_run_approval)
            self._app.router.add_post("/v1/runs/{run_id}/stop", self._handle_stop_run)
            # 在注册原生路由之后再存储 adapter。本地的 Hermes-Relay 引导
            # shim 把这个 key 用作特性探测钩子；先注册原生路由可让这些
            # shim 变为空操作，而不是遮蔽上游的 session 控制 handler。
            self._app["api_server_adapter"] = self

            # 启动后台清扫，清理孤立的（未被消费的）run 流
            sweep_task = asyncio.create_task(self._sweep_orphaned_runs())
            try:
                self._background_tasks.add(sweep_task)
            except TypeError:
                pass
            if hasattr(sweep_task, "add_done_callback"):
                sweep_task.add_done_callback(self._background_tasks.discard)

            # 无鉴权则拒绝启动。API 服务器可以分派具备终端能力的 agent 工作，
            # 因此每个部署都需要显式的 API_SERVER_KEY，无论绑定地址如何。
            if not self._api_key:
                logger.error(
                    "[%s] Refusing to start: API_SERVER_KEY is required for the API server, "
                    "including loopback-only binds on %s.",
                    self.name, self._host,
                )
                return False

            # 在网络可访问的绑定上，若使用占位符或弱密钥则拒绝启动。
            # 移植自 openclaw/openclaw#64586；在 2026 年 6 月的
            # hermes-0day 加固中，熵下限提升到 16（在公网绑定上用一个 8
            # 字符的密钥分派具备终端能力的 agent 工作是可暴力破解的）。
            if is_network_accessible(self._host) and self._api_key:
                try:
                    from hermes_cli.auth import has_usable_secret
                    if not has_usable_secret(self._api_key, min_length=16):
                        logger.error(
                            "[%s] Refusing to start: API_SERVER_KEY is a "
                            "placeholder or too short (<16 chars) for a "
                            "network-accessible bind. This endpoint dispatches "
                            "terminal-capable agent work — a guessable key is "
                            "remote code execution. Generate a strong secret "
                            "(e.g. `openssl rand -hex 32`) and set "
                            "API_SERVER_KEY before exposing it on %s.",
                            self.name, self._host,
                        )
                        return False
                except ImportError:
                    pass

            # 当网络可访问的 API 服务器运行在未沙箱化的本地终端后端上时，给出
            # 显著告警。API 服务器可以以宿主用户的身份驱动 agent 的终端/文件
            # 工具；在公网绑定上，这正是 hermes-0day 攻击活动所滥用、用于写入
            # ~/.hermes/config.yaml 并植入持久化的那一面。沙箱化（Docker /
            # 远程后端）可以控制影响范围。只告警、不拒绝 —— 运维方可能已配备
            # 外部防火墙 / 强密钥。
            if is_network_accessible(self._host):
                try:
                    from hermes_cli.config import load_config as _load_cfg
                    _backend = (
                        ((_load_cfg() or {}).get("terminal") or {}).get(
                            "backend", "local"
                        )
                    )
                except Exception:
                    _backend = "local"
                if str(_backend).lower() == "local":
                    logger.warning(
                        "[%s] API server is network-accessible (%s) AND the "
                        "terminal backend is 'local' (unsandboxed). Agent work "
                        "dispatched through this endpoint runs as the host user "
                        "with full terminal/file access. Strongly consider a "
                        "sandboxed backend (terminal.backend: docker) and "
                        "firewalling this port to trusted networks only.",
                        self.name, self._host,
                    )

            # 端口冲突检测 —— 端口已被占用则快速失败
            try:
                with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _s:
                    _s.settimeout(1)
                    _s.connect(('127.0.0.1', self._port))
                logger.error('[%s] Port %d already in use. Set a different port in config.yaml: platforms.api_server.port', self.name, self._port)
                return False
            except (ConnectionRefusedError, OSError):
                pass  # 端口空闲

            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()

            self._mark_connected()
            logger.info(
                "[%s] API server listening on http://%s:%d (model: %s)",
                self.name, self._host, self._port, self._model_name,
            )
            return True

        except Exception as e:
            logger.error("[%s] Failed to start API server: %s", self.name, e)
            return False

    async def disconnect(self) -> None:
        """停止 aiohttp web 服务器并释放所有持有的资源。

        除了停止 aiohttp web 服务器之外，还会关闭 ResponseStore 的 SQLite
        连接。如果不这么做，每个 adapter 实例都会泄漏 2 个文件描述符
        （数据库文件及其 WAL sidecar）—— ``gateway.run`` 中的重连循环在
        每次重试时都会构造一个新的 adapter，因此 2 个 fd/次重试 × 300s
        退避上限 ≈ 12 个 fd/小时，这会在约 12 小时的失败重连之后耗尽默认
        的 2560 fd 上限，并使整个 gateway 变成僵尸进程
        （OSError: [Errno 24] Too many open files，#37011）。
        """
        self._mark_disconnected()
        if self._response_store is not None:
            try:
                self._response_store.close()
            except Exception:
                logger.debug(
                    "Failed to close response store for %s", self.name, exc_info=True,
                )
        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        logger.info("[%s] API server stopped", self.name)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """
        未使用 —— HTTP 请求/响应周期直接处理投递。
        """
        return SendResult(success=False, error="API server uses HTTP request/response, not send()")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """返回 API 服务器的基本信息。"""
        return {
            "name": "API Server",
            "type": "api",
            "host": self._host,
            "port": self._port,
        }
