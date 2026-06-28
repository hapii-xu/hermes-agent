"""Signal 即时通讯平台适配器。

连接到以 HTTP 模式运行的 signal-cli 守护进程。
入站消息通过 SSE（Server-Sent Events）流式到达。
出站消息和动作通过 HTTP 上的 JSON-RPC 2.0 发送。

基于 ibhagwan 的 PR #268，并经错误修复重构。

要求：
  - 已安装并运行 signal-cli：signal-cli daemon --http 127.0.0.1:8080
  - 设置 SIGNAL_HTTP_URL 和 SIGNAL_ACCOUNT 环境变量
"""

import asyncio
import base64
import json
import logging
import os
import random
import shutil
import subprocess
import tempfile
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

import httpx

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
    cache_image_from_bytes,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_url,
)
from gateway.platforms.helpers import redact_phone
from gateway.platforms.signal_format import markdown_to_signal
from gateway.platforms.signal_rate_limit import (
    SIGNAL_BATCH_PACING_NOTICE_THRESHOLD,
    SIGNAL_MAX_ATTACHMENTS_PER_MSG,
    SIGNAL_RATE_LIMIT_MAX_ATTEMPTS,
    SignalRateLimitError,
    _extract_retry_after_seconds,
    _format_wait,
    _is_signal_rate_limit_error,
    _signal_send_timeout,
    get_scheduler,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
SIGNAL_MAX_ATTACHMENT_SIZE = 100 * 1024 * 1024  # 100 MB
MAX_MESSAGE_LENGTH = 8000  # Signal 消息大小上限
TYPING_INTERVAL = 8.0  # typing 指示符刷新之间的秒数
SSE_RETRY_DELAY_INITIAL = 2.0
SSE_RETRY_DELAY_MAX = 60.0
HEALTH_CHECK_INTERVAL = 30.0  # 健康检查之间的秒数
HEALTH_CHECK_STALE_THRESHOLD = 120.0  # SSE 无活动多久后才需要关注的秒数


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _parse_comma_list(value: str) -> List[str]:
    """将逗号分隔的字符串拆分为列表，去除空白。"""
    return [v.strip() for v in value.split(",") if v.strip()]


def _guess_extension(data: bytes) -> str:
    """根据 magic bytes 猜测文件扩展名。

    Android Signal 以原始 ADTS AAC 帧交付语音消息，它与 MPEG-1/2 Layer 3
    （MP3）共享 ``0xFF 0xFx`` 同步字。byte-1 的布局可用于区分：
    ADTS 将 ``ID layer protection_absent`` 打包进第 3-0 位，其中 ``ID``
    在 MPEG-2/4 AAC 中为 0，而 ``layer`` 在 ADTS 中恒为 0。
    真正的 MP3 帧 ``ID=1`` 且 ``layer`` 属于 {1, 2, 3}。
    """
    if data[:4] == b"\x89PNG":
        return ".png"
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:4] == b"GIF8":
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:4] == b"%PDF":
        return ".pdf"
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return ".mp4"
    if data[:4] == b"OggS":
        return ".ogg"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        # ``0xFF 0xFx`` 由 MP3 和 ADTS AAC 共享。区分依据是 byte 1 的
        # 第 3-1 位：ADTS 满足 ``ID=0`` 且 ``layer=00``
        # （mask 0xF6，目标 0xF0）；MP3 满足 ``ID=1`` 且 ``layer``
        # 属于 {01,10,11}（mask 0xF6，目标属于 {0xF2, 0xF4, 0xF6}）。
        if (data[1] & 0xF6) == 0xF0:
            return ".aac"
        return ".mp3"
    if data[:2] == b"PK":
        return ".zip"
    return ".bin"


def _is_image_ext(ext: str) -> bool:
    return ext.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _is_audio_ext(ext: str) -> bool:
    return ext.lower() in {".mp3", ".wav", ".ogg", ".m4a", ".aac"}


_EXT_TO_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp",
    ".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".mp4": "video/mp4", ".pdf": "application/pdf", ".zip": "application/zip",
}


def _ext_to_mime(ext: str) -> str:
    """将文件扩展名映射为 MIME 类型。"""
    return _EXT_TO_MIME.get(ext.lower(), "application/octet-stream")


def _remux_aac_to_m4a(aac_data: bytes) -> Optional[Tuple[bytes, str]]:
    """将原始 ADTS AAC 字节无损重封装到 MP4（.m4a）容器中。

    由 Signal 附件缓存使用，使 Android 语音消息落盘为所有主流 STT API
    （Groq、OpenAI、xAI、Mistral Voxtral）都能接受的容器格式。
    ``ffmpeg -c:a copy`` 只是单次解封装/重封装 —— 无重编码、无质量损失，
    对常见语音消息体量耗时在 100ms 以内。

    成功时返回 ``(m4a_bytes, ".m4a")``；如果 ffmpeg 缺失、输入非法，
    或因任何原因重封装失败则返回 ``None``。调用方必须将 ``None`` 视为
    "原样透传"，且不得抛出异常。
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        # macOS 开发主机上常见的 Homebrew/本地路径前缀。
        for prefix in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
            if os.path.isfile(prefix) and os.access(prefix, os.X_OK):
                ffmpeg = prefix
                break
    if not ffmpeg:
        logger.debug("Signal: ffmpeg not found, skipping AAC→M4A remux")
        return None
    try:
        with tempfile.NamedTemporaryFile(suffix=".aac", delete=False) as src:
            src.write(aac_data)
            src_path = src.name
        dst_path = src_path[:-4] + ".m4a"
        try:
            proc = subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", src_path,
                 "-c:a", "copy", "-movflags", "+faststart", dst_path],
                capture_output=True, timeout=10,
            )
            if proc.returncode != 0:
                logger.warning(
                    "Signal: AAC→M4A remux failed (ffmpeg exit %d): %s",
                    proc.returncode, proc.stderr.decode("utf-8", "replace")[:300],
                )
                return None
            with open(dst_path, "rb") as f:
                return f.read(), ".m4a"
        finally:
            for p in (src_path, dst_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
    except subprocess.TimeoutExpired:
        logger.warning("Signal: AAC→M4A remux timed out (>10s)")
        return None
    except Exception:
        logger.exception("Signal: AAC→M4A remux error")
        return None


def _render_mentions(text: str, mentions: list) -> str:
    """\u5C06 Signal mention \u5360\u4F4D\u7B26\uFF08\\uFFFC\uFF09\u66FF\u6362\u4E3A\u53EF\u8BFB\u7684 @identifiers\u3002

    Signal \u5C06 @mention \u7F16\u7801\u4E3A Unicode \u5BF9\u8C61\u66FF\u6362\u5B57\u7B26\uFF0C
    \u5E76\u5728\u5E26\u5916\u5143\u6570\u636E\u4E2D\u643A\u5E26\u88AB\u63D0\u53CA\u7528\u6237\u7684 UUID/\u53F7\u7801\u3002
    """
    if not mentions or "\uFFFC" not in text:
        return text
    # \u6309 start \u4F4D\u7F6E\uFF08\u5012\u5E8F\uFF09\u6392\u5E8F\uFF0C\u4ECE\u5C3E\u5230\u5934\u66FF\u6362\uFF0C
    # \u8FD9\u6837\u66FF\u6362\u65F6\u7D22\u5F15\u4E0D\u4F1A\u504F\u79FB
    sorted_mentions = sorted(mentions, key=lambda m: m.get("start", 0), reverse=True)
    for mention in sorted_mentions:
        start = mention.get("start", 0)
        length = mention.get("length", 1)
        # \u4F7F\u7528 mention \u7684 number \u6216 UUID \u4F5C\u4E3A\u66FF\u6362\u6587\u672C
        identifier = mention.get("number") or mention.get("uuid") or "user"
        replacement = f"@{identifier}"
        text = text[:start] + replacement + text[start + length:]
    return text


def _is_signal_service_id(value: str) -> bool:
    """\u5982\u679C *value* \u770B\u8D77\u6765\u5DF2\u7ECF\u662F Signal service identifier \u5219\u8FD4\u56DE True\u3002"""
    if not value:
        return False
    if value.startswith("PNI:") or value.startswith("u:"):
        return True
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _looks_like_e164_number(value: str) -> bool:
    """\u5BF9\u4E8E\u770B\u8D77\u6765\u5408\u7406\u7684 E.164 \u7535\u8BDD\u53F7\u7801\u8FD4\u56DE True\u3002"""
    if not value or not value.startswith("+"):
        return False
    digits = value[1:]
    return digits.isdigit() and 7 <= len(digits) <= 15


def check_signal_requirements() -> bool:
    """\u68C0\u67E5 Signal \u662F\u5426\u5DF2\u914D\u7F6E\uFF08\u5177\u6709 URL \u548C account\uFF09\u3002"""
    return bool(os.getenv("SIGNAL_HTTP_URL") and os.getenv("SIGNAL_ACCOUNT"))


# ---------------------------------------------------------------------------
# Signal \u9002\u914D\u5668
# ---------------------------------------------------------------------------

class SignalAdapter(BasePlatformAdapter):
    """\u4F7F\u7528 signal-cli HTTP \u5B88\u62A4\u8FDB\u7A0B\u7684 Signal \u5373\u65F6\u901A\u8BAF\u9002\u914D\u5668\u3002"""

    platform = Platform.SIGNAL
    # Signal \u6CA1\u6709\u9488\u5BF9\u5DF2\u53D1\u9001\u6D88\u606F\u7684\u771F\u5B9E\u7F16\u8F91 API\u3002\u663E\u5F0F\u6807\u8BB0\uFF0C
    # \u4F7F\u6D41\u5F0F\u8F93\u51FA\u6291\u5236\u53EF\u89C1\u5149\u6807\uFF0C\u907F\u514D\u7F16\u8F91\u5931\u8D25\u65F6\u5728\u804A\u5929\u5BA2\u6237\u7AEF\u7559\u4E0B
    # \u8FC7\u671F\u7684 tofu \u65B9\u5757\u3002
    SUPPORTS_MESSAGE_EDITING = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.SIGNAL)

        extra = config.extra or {}
        self.http_url = extra.get("http_url", "http://127.0.0.1:8080").rstrip("/")
        self.account = extra.get("account", "")
        self.ignore_stories = extra.get("ignore_stories", True)

        # 解析白名单 —— 群组策略由是否存在群组白名单推导而来
        group_allowed_str = os.getenv("SIGNAL_GROUP_ALLOWED_USERS", "")
        self.group_allow_from = set(_parse_comma_list(group_allowed_str))

        # Mention 过滤 —— 仅在群组中 @mention bot 账号时才响应。
        # 先从 config extra 读取，再回退到 SIGNAL_REQUIRE_MENTION 环境变量。
        _rm_cfg = extra.get("require_mention")
        if _rm_cfg is not None:
            self.require_mention = bool(_rm_cfg)
        else:
            self.require_mention = os.getenv("SIGNAL_REQUIRE_MENTION", "false").lower() in ("true", "1", "yes", "on")

        # DM 白名单 —— 与 run.py 检查的 SIGNAL_ALLOWED_USERS 对应。
        # 在此处保存，以便 reaction 钩子可以跳过未授权发送者
        # （reaction 在 run.py 的鉴权门控之前触发，因此如果没有此检查，
        # 任何联系人发来的每条入站 DM 都会得到一个 👀 reaction）。
        # "*" 表示允许所有用户（开放模式）；为空表示适配器层不记录限制
        # （run.py 仍会单独执行鉴权）。
        dm_allowed_str = os.getenv("SIGNAL_ALLOWED_USERS", "*")
        self.dm_allow_from = set(_parse_comma_list(dm_allowed_str))

        # HTTP 客户端
        self.client: Optional[httpx.AsyncClient] = None

        # 后台任务
        self._sse_task: Optional[asyncio.Task] = None
        self._health_monitor_task: Optional[asyncio.Task] = None
        self._typing_tasks: Dict[str, asyncio.Task] = {}
        # 按聊天维度的 typing 指示符退避。当 signal-cli 报告
        # NETWORK_FAILURE（接收者离线 / 不可路由）时，否则 base.py 的
        # _keep_typing 刷新循环会无限制地每约 2s 猛轰 sendTyping，
        # 产生 WARNING 级别的日志刷屏和无意义的 RPC 流量。
        # 我们按聊天追踪连续失败次数，并在冷却窗口内跳过 RPC。
        self._typing_failures: Dict[str, int] = {}
        self._typing_skip_until: Dict[str, float] = {}
        self._running = False
        self._last_sse_activity = 0.0
        self._sse_response: Optional[httpx.Response] = None

        # 规范化 account，用于自身消息过滤
        self._account_normalized = self.account.strip()

        # 跟踪最近发送的消息时间戳，以防止 Note to Self / self-chat
        # 模式以及关联设备群组同步发送中的回声循环。
        # OrderedDict[timestamp_ms -> insertion_monotonic_seconds] 提供
        # LRU 淘汰（popitem(last=False) 丢弃最旧）外加 TTL，这样在活跃
        # 群组中仍待处理的回声不会被仅因为发生了 >50 次出站而淘汰。
        # 在 5 分钟 TTL 下，上限只对失控的生产者有意义，对正常流量突发无影响。
        self._recent_sent_timestamps: "OrderedDict[int, float]" = OrderedDict()
        self._max_recent_timestamps = 512
        self._recent_sent_ttl_seconds = 300.0
        # 维护一个独立的、有界的出站 Signal 消息时间戳缓存。
        # Signal 的 quote.id 是被引用消息的时间戳，因此这使得入站回复
        # 即便在上方过滤掉 self-sync 回声后，仍能识别出用户回复的是
        # 本 bot 发送的消息。
        # 使用 OrderedDict（而非 set）使上限按 FIFO 顺序淘汰最旧的时间戳 ——
        # 普通 set.pop() 会移除任意元素，可能丢弃仍然较新的时间戳，
        # 从而漏掉真正的回复到自身消息。
        self._sent_message_timestamps: "OrderedDict[str, None]" = OrderedDict()
        self._max_sent_message_timestamps = 500
        # Signal 越来越多地暴露 ACI/PNI UUID 作为稳定的接收者 ID。
        # 维护一份尽力而为的映射，使出站发送可在 signal-cli 偏好时
        # 将电话号码升级为对应的 UUID。
        self._recipient_uuid_by_number: Dict[str, str] = {}
        self._recipient_number_by_uuid: Dict[str, str] = {}
        self._recipient_cache_lock = asyncio.Lock()

        logger.info("Signal adapter initialized: url=%s account=%s groups=%s",
                     self.http_url, redact_phone(self.account),
                     "enabled" if self.group_allow_from else "disabled")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """连接到 signal-cli 守护进程并启动 SSE 监听器。"""
        if not self.http_url or not self.account:
            logger.error("Signal: SIGNAL_HTTP_URL and SIGNAL_ACCOUNT are required")
            return False

        # 获取带作用域的锁，防止同一手机号出现重复的 Signal 监听器
        lock_acquired = False
        try:
            if not self._acquire_platform_lock('signal-phone', self.account, 'Signal account'):
                return False
            lock_acquired = True
        except Exception as e:
            logger.warning("Signal: Could not acquire phone lock (non-fatal): %s", e)

        # 收紧 keepalive，使空闲的 CLOSE_WAIT 尽快排空（#18451）。
        from gateway.platforms._http_client_limits import platform_httpx_limits
        self.client = httpx.AsyncClient(timeout=30.0, limits=platform_httpx_limits())
        try:
            # 健康检查 —— 验证 signal-cli 守护进程可达
            try:
                resp = await self.client.get(f"{self.http_url}/api/v1/check", timeout=10.0)
                if resp.status_code != 200:
                    logger.error("Signal: health check failed (status %d)", resp.status_code)
                    return False
            except Exception as e:
                logger.error("Signal: cannot reach signal-cli at %s: %s", self.http_url, e)
                return False

            self._running = True
            self._last_sse_activity = time.time()
            self._sse_task = asyncio.create_task(self._sse_listener())
            self._health_monitor_task = asyncio.create_task(self._health_monitor())

            logger.info("Signal: connected to %s", self.http_url)
            return True
        finally:
            if not self._running:
                if self.client:
                    await self.client.aclose()
                    self.client = None
                if lock_acquired:
                    self._release_platform_lock()

    async def disconnect(self) -> None:
        """停止 SSE 监听器并清理。"""
        self._running = False

        if self._sse_task:
            self._sse_task.cancel()
            try:
                await self._sse_task
            except asyncio.CancelledError:
                pass

        if self._health_monitor_task:
            self._health_monitor_task.cancel()
            try:
                await self._health_monitor_task
            except asyncio.CancelledError:
                pass

        # 取消所有 typing 任务
        for task in self._typing_tasks.values():
            task.cancel()
        self._typing_tasks.clear()

        if self.client:
            await self.client.aclose()
            self.client = None

        self._release_platform_lock()

        logger.info("Signal: disconnected")

    # ------------------------------------------------------------------
    # SSE 流式传输（入站消息）
    # ------------------------------------------------------------------

    async def _sse_listener(self) -> None:
        """监听来自 signal-cli 守护进程的 SSE 事件。"""
        url = f"{self.http_url}/api/v1/events?account={quote(self.account, safe='')}"
        backoff = SSE_RETRY_DELAY_INITIAL

        while self._running:
            try:
                logger.debug("Signal SSE: connecting to %s", url)
                async with self.client.stream(
                    "GET", url,
                    headers={"Accept": "text/event-stream"},
                    timeout=None,
                ) as response:
                    self._sse_response = response
                    backoff = SSE_RETRY_DELAY_INITIAL  # 成功连接后重置
                    self._last_sse_activity = time.time()
                    logger.info("Signal SSE: connected")

                    buffer = ""
                    async for chunk in response.aiter_text():
                        if not self._running:
                            break
                        buffer += chunk
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            # SSE keepalive 注释（":"）证明连接仍然存活 ——
                            # 更新活动时间，使健康监控器不会误报空闲警告。
                            if line.startswith(":"):
                                self._last_sse_activity = time.time()
                                continue
                            # 解析 SSE data 行
                            if line.startswith("data:"):
                                data_str = line[5:].strip()
                                if not data_str:
                                    continue
                                self._last_sse_activity = time.time()
                                try:
                                    data = json.loads(data_str)
                                    await self._handle_envelope(data)
                                except json.JSONDecodeError:
                                    logger.debug("Signal SSE: invalid JSON: %s", data_str[:100])
                                except Exception:
                                    logger.exception("Signal SSE: error handling event")

            except asyncio.CancelledError:
                break
            except httpx.HTTPError as e:
                if self._running:
                    logger.warning("Signal SSE: HTTP error: %s (reconnecting in %.0fs)", e, backoff)
            except Exception as e:
                if self._running:
                    logger.warning("Signal SSE: error: %s (reconnecting in %.0fs)", e, backoff)

            if self._running:
                # 添加 20% 抖动以避免重连时的惊群效应
                jitter = backoff * 0.2 * random.random()
                await asyncio.sleep(backoff + jitter)
                backoff = min(backoff * 2, SSE_RETRY_DELAY_MAX)

        self._sse_response = None

    # ------------------------------------------------------------------
    # 健康监控
    # ------------------------------------------------------------------

    async def _health_monitor(self) -> None:
        """监控 SSE 连接健康状态，并在长时间空闲时强制重连。"""
        while self._running:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            if not self._running:
                break

            elapsed = time.time() - self._last_sse_activity
            if elapsed > HEALTH_CHECK_STALE_THRESHOLD:
                logger.warning("Signal: SSE idle for %.0fs, checking daemon health", elapsed)
                try:
                    resp = await self.client.get(
                        f"{self.http_url}/api/v1/check", timeout=10.0
                    )
                    if resp.status_code == 200:
                        # 守护进程存活但 SSE 空闲 —— 更新活动时间以避免重复
                        # 警告（连接可能只是安静）
                        self._last_sse_activity = time.time()
                        logger.debug("Signal: daemon healthy, SSE idle")
                    else:
                        logger.warning("Signal: health check failed (%d), forcing reconnect", resp.status_code)
                        self._force_reconnect()
                except Exception as e:
                    logger.warning("Signal: health check error: %s, forcing reconnect", e)
                    self._force_reconnect()

    def _force_reconnect(self) -> None:
        """通过关闭当前响应来强制 SSE 重连。"""
        if self._sse_response and not self._sse_response.is_stream_consumed:
            try:
                task = asyncio.create_task(self._sse_response.aclose())
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            except Exception:
                pass
            self._sse_response = None

    # ------------------------------------------------------------------
    # 消息处理
    # ------------------------------------------------------------------

    async def _handle_envelope(self, envelope: dict) -> None:
        """处理入站的 signal-cli envelope。"""
        # 如果存在嵌套 envelope 则解包
        envelope_data = envelope.get("envelope", envelope)

        # 处理 syncMessage：提取 "Note to Self" 消息（发送给自身账号），
        # 同时仍过滤其他 sync 事件（已读回执、typing 等）
        is_note_to_self = False
        if "syncMessage" in envelope_data:
            sync_msg = envelope_data.get("syncMessage")
            if sync_msg and isinstance(sync_msg, dict):
                sent_msg = sync_msg.get("sentMessage")
                if sent_msg and isinstance(sent_msg, dict):
                    dest = sent_msg.get("destinationNumber") or sent_msg.get("destination")
                    sent_ts = sent_msg.get("timestamp")
                    sent_msg_group_info = sent_msg.get("groupInfo") or {}
                    sent_msg_group_id = sent_msg_group_info.get("groupId") if sent_msg_group_info else None
                    if dest == self._account_normalized or sent_msg_group_id:
                        # 检查这是否是我们自身出站回复的回声
                        if self._consume_sent_timestamp(sent_ts):
                            return
                        # 真正的用户 Note to Self —— 提升为 dataMessage
                        is_note_to_self = True
                        envelope_data = {**envelope_data, "dataMessage": sent_msg}
            if not is_note_to_self:
                return

        # 提取发送者信息
        sender = (
            envelope_data.get("sourceNumber")
            or envelope_data.get("sourceUuid")
            or envelope_data.get("source")
        )
        sender_name = envelope_data.get("sourceName", "")
        sender_uuid = envelope_data.get("sourceUuid", "")
        self._remember_recipient_identifiers(sender, sender_uuid)

        if not sender:
            logger.debug("Signal: ignoring envelope with no sender")
            return

        # 自身消息过滤 —— 防止回复循环（但允许 Note to Self）
        if self._account_normalized and sender == self._account_normalized and not is_note_to_self:
            return

        # 过滤 stories
        if self.ignore_stories and envelope_data.get("storyMessage"):
            return

        # 获取 data message —— 同时检查 editMessage（编辑过的消息将更新后的
        # dataMessage 放在 editMessage.dataMessage 内）
        data_message = (
            envelope_data.get("dataMessage")
            or (envelope_data.get("editMessage") or {}).get("dataMessage")
        )
        if not data_message:
            return

        # 检查是否为群组消息
        group_info = data_message.get("groupInfo")
        group_id = group_info.get("groupId") if group_info else None
        is_group = bool(group_id)

        # 群组消息过滤 —— 由 SIGNAL_GROUP_ALLOWED_USERS 推导：
        # - 未设置环境变量 → 禁用群组（默认安全行为）
        # - 环境变量设为群组 ID → 仅允许这些群组
        # - 环境变量设为 "*" → 允许所有群组
        # DM 鉴权完全由 run.py 处理（_is_user_authorized）
        if is_group:
            if not self.group_allow_from:
                logger.debug("Signal: ignoring group message (no SIGNAL_GROUP_ALLOWED_USERS)")
                return
            if "*" not in self.group_allow_from and group_id not in self.group_allow_from:
                logger.debug("Signal: group %s not in allowlist", group_id[:8] if group_id else "?")
                return

        # 构建聊天信息
        chat_id = sender if not is_group else f"group:{group_id}"
        chat_type = "group" if is_group else "dm"

        # 提取文本并渲染 mention
        text = data_message.get("message", "")
        mentions = data_message.get("mentions", [])
        if text and mentions:
            text = _render_mentions(text, mentions)

        # Mention 过滤：在群组中仅处理 @mention bot 账号的消息
        if is_group and self.require_mention:
            account_norm = self._account_normalized
            # 检查渲染后的 mention 标签或原始 mention 元数据
            mentioned_in_text = account_norm and (
                f"@{account_norm}" in (text or "")
            )
            mentioned_in_metadata = any(
                m.get("number") == account_norm or m.get("uuid") == account_norm
                for m in (data_message.get("mentions") or [])
            )
            if not mentioned_in_text and not mentioned_in_metadata:
                logger.debug(
                    "Signal: ignoring group message (require_mention=true, bot not mentioned)"
                )
                return

        # 从任何群组消息中剥离 bot 自身的 @mention，使 agent 不会把
        # "@+155****4567 say hello" 误解为联系该号码的指令。
        # _render_mentions 将 Signal ￼ 占位符替换为 @<number-or-uuid>，
        # 这在 LLM 看来像是收件人，而非自指。适用于所有群组
        # （不仅是 require_mention 群组），使 self-mention 在任何地方都被清理。
        if is_group and text:
            account_norm = self._account_normalized
            if account_norm:
                text = text.replace(f"@{account_norm}", "")
                # 如果 mention 是用 bot 的 UUID 渲染的，也要剥离
                bot_uuid = self._recipient_uuid_by_number.get(account_norm)
                if bot_uuid:
                    text = text.replace(f"@{bot_uuid}", "")
                # 整理 mention 移除后留下的空格：折叠句中移除产生的双空格，
                # 并修剪两端。仅触及移除所引入的双空格，因此多行消息中的
                # 有意换行会被保留。
                text = text.replace("  ", " ").strip()

        # 从 Signal dataMessage 提取 quote（回复到）上下文。Signal 的
        # quote.id 是被引用消息的时间戳；quote.author（若可用）指向被引用的
        # 发送者。保留两者，使 gateway 能告知 agent 用户回复了某条特定
        # 的 assistant 消息。
        quote_data = data_message.get("quote") or {}
        reply_to_id = str(quote_data.get("id")) if quote_data.get("id") else None
        reply_to_text = quote_data.get("text")
        reply_to_author = self._extract_quote_author(quote_data)
        reply_to_author_name = quote_data.get("authorName") or quote_data.get("authorProfileName")
        reply_to_is_own = self._quote_references_own_message(reply_to_id, reply_to_author)

        # 处理附件
        attachments_data = data_message.get("attachments", [])
        media_urls = []
        media_types = []

        if attachments_data and not getattr(self, "ignore_attachments", False):
            for att in attachments_data:
                att_id = att.get("id")
                att_size = att.get("size", 0)
                if not att_id:
                    continue
                if att_size > SIGNAL_MAX_ATTACHMENT_SIZE:
                    logger.warning("Signal: attachment too large (%d bytes), skipping", att_size)
                    continue
                try:
                    cached_path, ext = await self._fetch_attachment(att_id)
                    if cached_path:
                        # 如可用则使用 Signal 的 contentType，否则按扩展名映射
                        content_type = att.get("contentType") or _ext_to_mime(ext)
                        media_urls.append(cached_path)
                        media_types.append(content_type)
                except Exception:
                    logger.exception("Signal: failed to fetch attachment %s", att_id)

        # 跳过无实质内容（无文本、无附件）的 envelope。
        # 捕获 profile key 更新、空消息以及其他仅携带元数据、仍包裹
        # dataMessage 但没有任何值得处理内容的 envelope。参见 issue：
        # signal-cli 记录 "Profile key update" + Hermes 收到 msg=''，
        # 触发一次完整的 agent turn 却什么也没做。
        if (not text or not text.strip()) and not media_urls:
            logger.debug(
                "Signal: skipping contentless envelope from %s (%d attachments)",
                redact_phone(sender), len(media_urls) if media_urls else 0,
            )
            return

        # 构建会话 source
        source = self.build_source(
            chat_id=chat_id,
            chat_name=group_info.get("groupName") if group_info else sender_name,
            chat_type=chat_type,
            user_id=sender,
            user_name=sender_name or sender,
            user_id_alt=sender_uuid if sender_uuid else None,
            chat_id_alt=group_id if is_group else None,
        )

        # 根据媒体确定消息类型
        msg_type = MessageType.TEXT
        if media_types:
            if any(mt.startswith("audio/") for mt in media_types):
                msg_type = MessageType.VOICE
            elif any(mt.startswith("image/") for mt in media_types):
                msg_type = MessageType.PHOTO
            elif any(mt.startswith("video/") for mt in media_types):
                msg_type = MessageType.VIDEO
            else:
                # 兜底：application/*、text/* 以及未知 MIME 类型一律视为
                # document，使 run.py 的 document-context 注入能把缓存的
                # 文件路径暴露给 agent（与 WhatsApp/Slack/BlueBubbles/
                # Mattermost 的模式一致）。
                msg_type = MessageType.DOCUMENT

        # 从 envelope 数据解析时间戳（自 epoch 起的毫秒）
        ts_ms = envelope_data.get("timestamp", 0)
        if ts_ms:
            try:
                timestamp = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            except (ValueError, OSError):
                timestamp = datetime.now(tz=timezone.utc)
        else:
            timestamp = datetime.now(tz=timezone.utc)

        # 构建并分发事件。
        # 将原始 envelope 数据存入 raw_message，使 on_processing_start/
        # complete 可以为 sendReaction 提取 targetAuthor + targetTimestamp。
        event = MessageEvent(
            source=source,
            text=text or "",
            message_type=msg_type,
            media_urls=media_urls,
            media_types=media_types,
            timestamp=timestamp,
            raw_message={
                "sender": sender,
                "timestamp_ms": ts_ms,
                "quote": quote_data if quote_data else None,
            },
            reply_to_message_id=reply_to_id,
            reply_to_text=reply_to_text,
            reply_to_author_id=reply_to_author,
            reply_to_author_name=reply_to_author_name,
            reply_to_is_own_message=reply_to_is_own,
        )

        logger.debug("Signal: message from %s in %s: %s",
                      redact_phone(sender), chat_id[:20], (text or "")[:50])

        await self.handle_message(event)

    def _remember_recipient_identifiers(self, number: Optional[str], service_id: Optional[str]) -> None:
        """缓存从 Signal envelope 观察到的任何 number↔UUID 映射。"""
        if not number or not service_id or not _is_signal_service_id(service_id):
            return
        self._recipient_uuid_by_number[number] = service_id
        self._recipient_number_by_uuid[service_id] = number

    @staticmethod
    def _extract_quote_author(quote_data: Any) -> Optional[str]:
        """从 quote 元数据返回最佳的可用 Signal 发送者标识符。"""
        if not isinstance(quote_data, dict):
            return None
        for key in (
            "author",
            "authorNumber",
            "authorUuid",
            "authorAci",
            "authorServiceId",
            "authorServiceIdString",
        ):
            value = quote_data.get(key)
            if value:
                return str(value)
        return None

    def _quote_references_own_message(
        self,
        reply_to_id: Optional[str],
        reply_to_author: Optional[str],
    ) -> bool:
        """当 Signal quote 指向本适配器的出站消息时返回 True。"""
        if reply_to_id and str(reply_to_id) in self._sent_message_timestamps:
            return True
        if not reply_to_author:
            return False
        author = str(reply_to_author).strip()
        if self._account_normalized and author == self._account_normalized:
            return True
        cached_uuid = self._recipient_uuid_by_number.get(self._account_normalized)
        if cached_uuid and author == cached_uuid:
            return True
        cached_number = self._recipient_number_by_uuid.get(author)
        return bool(cached_number and cached_number == self._account_normalized)

    def _remember_sent_message_timestamp(self, timestamp: Any) -> None:
        """为 quote 匹配维护一个有界的出站 Signal 时间戳缓存。"""
        if timestamp is None:
            return
        key = str(timestamp)
        # 重新插入以标记为最近使用，使淘汰丢弃真正过旧的时间戳，
        # 而非最近再次见到的时间戳。
        self._sent_message_timestamps.pop(key, None)
        self._sent_message_timestamps[key] = None
        # 超过上限时按 FIFO 淘汰最旧的条目。
        while len(self._sent_message_timestamps) > self._max_sent_message_timestamps:
            self._sent_message_timestamps.popitem(last=False)

    def _extract_contact_uuid(self, contact: Any, phone_number: str) -> Optional[str]:
        """尽力从 listContacts 输出中提取 Signal service ID。"""
        if not isinstance(contact, dict):
            return None

        number = contact.get("number")
        recipient = contact.get("recipient")
        service_id = contact.get("uuid") or contact.get("serviceId")
        if not service_id:
            profile = contact.get("profile")
            if isinstance(profile, dict):
                service_id = profile.get("serviceId") or profile.get("uuid")

        if service_id and _is_signal_service_id(service_id):
            matches_number = number == phone_number or recipient == phone_number
            if matches_number:
                return service_id
        return None

    async def _resolve_recipient(self, chat_id: str) -> str:
        """返回私聊的首选 Signal 接收者标识符。"""
        if (
            not chat_id
            or chat_id.startswith("group:")
            or _is_signal_service_id(chat_id)
            or not _looks_like_e164_number(chat_id)
        ):
            return chat_id

        cached = self._recipient_uuid_by_number.get(chat_id)
        if cached:
            return cached

        async with self._recipient_cache_lock:
            cached = self._recipient_uuid_by_number.get(chat_id)
            if cached:
                return cached

            contacts = await self._rpc("listContacts", {
                "account": self.account,
                "allRecipients": True,
            })
            if isinstance(contacts, list):
                for contact in contacts:
                    number = contact.get("number") if isinstance(contact, dict) else None
                    service_id = self._extract_contact_uuid(contact, chat_id)
                    if number and service_id:
                        self._remember_recipient_identifiers(number, service_id)

            return self._recipient_uuid_by_number.get(chat_id, chat_id)

    # ------------------------------------------------------------------
    # 附件处理
    # ------------------------------------------------------------------

    async def _fetch_attachment(self, attachment_id: str) -> tuple:
        """通过 JSON-RPC 获取附件并缓存。返回 (path, ext)。"""
        result = await self._rpc("getAttachment", {
            "account": self.account,
            "id": attachment_id,
        })

        if not result:
            return None, ""

        # 处理 dict 响应（signal-cli 返回 {"data": "base64..."}）
        if isinstance(result, dict):
            result = result.get("data")
            if not result:
                logger.warning("Signal: attachment response missing 'data' key")
                return None, ""

        # result 是 base64 编码的文件内容
        raw_data = base64.b64decode(result)
        ext = _guess_extension(raw_data)

        # Android Signal 语音消息是原始 ADTS AAC 流。大多数 STT 提供方
        # （Groq Whisper、OpenAI Whisper）拒绝原始 ADTS —— 它们要求
        # AAC 被封装进 MP4 容器。用 ``ffmpeg -c:a copy`` 无损重封装，
        # 使缓存文件成为普通的 .m4a。无重编码，在 Pi 5 上耗时 <100ms。
        # 如果 ffmpeg 缺失则优雅地不操作：原始 ADTS 文件按原样缓存，
        # STT 可能拒绝它（下游没有嗅探后重封装的兜底）。
        if ext == ".aac":
            remuxed: Optional[Tuple[bytes, str]] = await asyncio.to_thread(_remux_aac_to_m4a, raw_data)
            if remuxed is not None:
                raw_data, ext = remuxed

        if _is_image_ext(ext):
            path = cache_image_from_bytes(raw_data, ext)
        elif _is_audio_ext(ext):
            path = cache_audio_from_bytes(raw_data, ext)
        else:
            path = cache_document_from_bytes(raw_data, ext)

        return path, ext

    # ------------------------------------------------------------------
    # JSON-RPC 通信
    # ------------------------------------------------------------------

    async def _rpc(
        self,
        method: str,
        params: dict,
        rpc_id: str = None,
        *,
        log_failures: bool = True,
        raise_on_rate_limit: bool = False,
        timeout: float = 30.0,
    ) -> Any:
        """向 signal-cli 守护进程发送 JSON-RPC 2.0 请求。

        当 ``log_failures=False`` 时，错误和异常路径以 DEBUG 而非
        WARNING 记录 —— 用于 typing 指示符路径，对不可达的接收者
        静默重复的 NETWORK_FAILURE 刷屏，同时仍为首次出现以及无关
        的 RPC 保留可见性。

        当 ``raise_on_rate_limit=True`` 时，Signal 的 ``[429]`` /
        ``RateLimitException`` 响应会抛出 ``SignalRateLimitError``
        而非被吞掉 —— 使调用方（多附件发送）可选择退避重试而不改变
        默认行为。
        """
        if not self.client:
            logger.warning("Signal: RPC called but client not connected")
            return None

        if rpc_id is None:
            rpc_id = f"{method}_{int(time.time() * 1000)}"

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": rpc_id,
        }

        try:
            resp = await self.client.post(
                f"{self.http_url}/api/v1/rpc",
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()

            if "error" in data:
                err = data["error"]
                if raise_on_rate_limit:
                    if _is_signal_rate_limit_error(err):
                        err_msg = str(err.get("message", "")) if isinstance(err, dict) else str(err)
                        retry_after = _extract_retry_after_seconds(err)
                        raise SignalRateLimitError(err_msg, retry_after=retry_after)
                if log_failures:
                    logger.warning("Signal RPC error (%s): %s", method, err)
                else:
                    logger.debug("Signal RPC error (%s): %s", method, err)
                return None

            result = data.get("result")
            if isinstance(result, dict) and raise_on_rate_limit:
                results = result.get("results")
                if isinstance(results, list):
                    for r in results:
                        if isinstance(r, dict) and r.get("type") == "RATE_LIMIT_FAILURE":
                            retry_after = r.get("retryAfterSeconds")
                            raise SignalRateLimitError("Rate limit exceeded for recipient", retry_after=retry_after)

            return result

        except SignalRateLimitError:
            raise
        except Exception as e:
            if log_failures:
                logger.warning("Signal RPC %s failed: %s", method, e)
            else:
                logger.debug("Signal RPC %s failed: %s", method, e)
            return None

    # ------------------------------------------------------------------
    # 格式化 —— markdown → Signal body ranges
    # ------------------------------------------------------------------

    @staticmethod
    def _markdown_to_signal(text: str) -> tuple[str, list[str]]:
        """围绕共享 Signal 格式化辅助函数的向后兼容封装。"""
        return markdown_to_signal(text)

    def format_message(self, content: str) -> str:
        """为纯文本兜底剥离 markdown（由 base class 使用）。

        实际的富格式化发生在 send() 中，通过 _markdown_to_signal() 完成。
        """
        # 仅当有人使用 base-class send 路径时才会被调用。
        # 我们的 send() 覆盖完全绕过此方法。
        return content

    def _validate_send_result(self, result: Any) -> tuple[bool, Optional[str]]:
        """校验 signal-cli send 响应结果。

        返回 (success, error_message)。
        """
        if not result or not isinstance(result, dict):
            return True, None

        results = result.get("results")
        if isinstance(results, list):
            for r in results:
                if not isinstance(r, dict):
                    continue
                rtype = r.get("type")
                if rtype and rtype != "SUCCESS":
                    return False, str(rtype)
                if "success" in r and not r.get("success"):
                    fail = r.get("failure")
                    if fail:
                        return False, str(fail)
                    return False, "Recipient delivery failed"
        return True, None

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """发送带有原生 Signal 格式的文本消息。"""
        await self._stop_typing_indicator(chat_id)

        plain_text, text_styles = self._markdown_to_signal(content)

        params: Dict[str, Any] = {
            "account": self.account,
            "message": plain_text,
        }

        if text_styles:
            if len(text_styles) == 1:
                params["textStyle"] = text_styles[0]
            else:
                params["textStyles"] = text_styles

        if chat_id.startswith("group:"):
            params["groupId"] = chat_id[6:]
        else:
            params["recipient"] = [await self._resolve_recipient(chat_id)]

        logger.info("[Signal] Sending response (%d chars) to %s", len(plain_text), chat_id)
        result = await self._rpc("send", params)

        if result is not None:
            success, err_msg = self._validate_send_result(result)
            if not success:
                return SendResult(success=False, error=err_msg, raw_response=result)
            self._track_sent_timestamp(result)
            # Signal 没有可编辑的消息标识符。返回 None 让流式消费方
            # 走非编辑的兜底路径，而不是假装后续编辑能从聊天会话中
            # 移除进行中的光标。
            return SendResult(success=True, message_id=None)
        return SendResult(success=False, error="RPC send failed")

    def _track_sent_timestamp(self, rpc_result) -> None:
        """记录出站消息时间戳，用于回声过滤。"""
        ts = rpc_result.get("timestamp") if isinstance(rpc_result, dict) else None
        if ts:
            self._remember_sent_message_timestamp(ts)
            now = time.monotonic()
            # 重新插入以标记为最近使用。
            self._recent_sent_timestamps.pop(ts, None)
            self._recent_sent_timestamps[ts] = now
            # 先丢弃超过 TTL 的条目（开销低，O(k)，k 为过期数量）。
            cutoff = now - self._recent_sent_ttl_seconds
            while self._recent_sent_timestamps:
                oldest_ts, oldest_at = next(iter(self._recent_sent_timestamps.items()))
                if oldest_at < cutoff:
                    self._recent_sent_timestamps.popitem(last=False)
                else:
                    break
            # 硬上限作为对失控生产者的最后兜底。
            while len(self._recent_sent_timestamps) > self._max_recent_timestamps:
                self._recent_sent_timestamps.popitem(last=False)

    def _consume_sent_timestamp(self, ts) -> bool:
        """若时间戳与我们发送的某条匹配则弹出。回声时返回 True。"""
        if ts and ts in self._recent_sent_timestamps:
            self._recent_sent_timestamps.pop(ts, None)
            return True
        return False

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """发送 typing 指示符。

        base.py 的 ``_keep_typing`` 刷新循环在 agent 处理期间每约 2s
        调用一次。如果 signal-cli 对该接收者返回 NETWORK_FAILURE
        （离线、不可路由、群组成员关系丢失等），未缓解的行为是：
        只要 agent 还在运行，就每 2 秒产生一条 WARNING 日志。取而代之，
        我们：

        - 在首次连续失败后静默 WARNING（后续尝试以 DEBUG 记录），
          使传输问题仍可见一次但不会淹没日志，
        - 在连续三次失败后，于指数退避窗口内完全跳过 RPC，
          停止对 signal-cli 发送它无法投递的请求。

        一次成功的 sendTyping 会清零计数器。
        """
        now = time.monotonic()
        skip_until = self._typing_skip_until.get(chat_id, 0.0)
        if now < skip_until:
            return

        params: Dict[str, Any] = {
            "account": self.account,
        }

        if chat_id.startswith("group:"):
            params["groupId"] = chat_id[6:]
        else:
            params["recipient"] = [await self._resolve_recipient(chat_id)]

        fails = self._typing_failures.get(chat_id, 0)
        result = await self._rpc(
            "sendTyping",
            params,
            rpc_id="typing",
            log_failures=(fails == 0),
        )

        if result is None:
            fails += 1
            self._typing_failures[chat_id] = fails
            # 连续 3 次失败后，按指数退避（16s、32s，上限 60s），
            # 停止对当前明显不可达的接收者猛轰 signal-cli。
            if fails >= 3:
                backoff = min(60.0, 16.0 * (2 ** (fails - 3)))
                self._typing_skip_until[chat_id] = now + backoff
        else:
            self._typing_failures.pop(chat_id, None)
            self._typing_skip_until.pop(chat_id, None)

    async def send_multiple_images(
        self,
        chat_id: str,
        images: List[Tuple[str, str]],
        metadata: Optional[Dict[str, Any]] = None,
        human_delay: float = 0.0,
    ) -> None:
        """通过分块的 Signal RPC 调用发送一批图片。

        每张图片的 alt 文本会被丢弃 —— Signal 的 send RPC 只携带一个
        共享消息正文。坏图片（下载失败、文件缺失、超大）会被跳过并
        记录警告，使一张坏 URL 不会丢失整批其余图片。
        ``human_delay`` 被忽略：速率限制调度器负责批次间的节奏。
        """
        if not images:
            return

        scheduler = get_scheduler()
        logger.info(
            "Signal send_multiple_images: received %d image(s) for %s — "
            "scheduler state: %s",
            len(images), chat_id[:30], scheduler.state(),
        )

        await self._stop_typing_indicator(chat_id)

        attachments: List[str] = []
        skipped_download = 0
        skipped_missing = 0
        skipped_oversize = 0
        for image_url, _alt_text in images:
            if image_url.startswith("file://"):
                file_path = unquote(image_url[7:])
            else:
                try:
                    file_path = await cache_image_from_url(image_url)
                except Exception as e:
                    logger.warning("Signal: failed to download image %s: %s", image_url, e)
                    skipped_download += 1
                    continue

            if not file_path or not Path(file_path).exists():
                logger.warning("Signal: image file not found for %s", image_url)
                skipped_missing += 1
                continue

            file_size = Path(file_path).stat().st_size
            if file_size > SIGNAL_MAX_ATTACHMENT_SIZE:
                logger.warning(
                    "Signal: image too large (%d bytes), skipping %s", file_size, image_url
                )
                skipped_oversize += 1
                continue

            attachments.append(file_path)

        if not attachments:
            logger.error(
                "Signal: no valid images in batch of %d "
                "(download=%d missing=%d oversize=%d)",
                len(images), skipped_download, skipped_missing, skipped_oversize,
            )
            return

        logger.info(
            "Signal send_multiple_images: %d/%d images valid, sending in chunks",
            len(attachments), len(images),
        )

        base_params: Dict[str, Any] = {
            "account": self.account,
            "message": "",
        }
        if chat_id.startswith("group:"):
            base_params["groupId"] = chat_id[6:]
        else:
            base_params["recipient"] = [await self._resolve_recipient(chat_id)]

        att_batches = [
            attachments[i:i + SIGNAL_MAX_ATTACHMENTS_PER_MSG]
            for i in range(0, len(attachments), SIGNAL_MAX_ATTACHMENTS_PER_MSG)
        ]

        for idx, att_batch in enumerate(att_batches):
            n = len(att_batch)
            estimated = scheduler.estimate_wait(n)
            logger.debug(
                "Signal batch %d/%d: %d attachments, estimated wait=%.1fs",
                idx + 1, len(att_batches), n, estimated,
            )
            if estimated >= SIGNAL_BATCH_PACING_NOTICE_THRESHOLD:
                await self._notify_batch_pacing(
                    chat_id, idx + 1, len(att_batches), estimated
                )

            params = dict(base_params, attachments=att_batch)
            send_timeout = _signal_send_timeout(n)

            for attempt in range(1, SIGNAL_RATE_LIMIT_MAX_ATTEMPTS + 1):
                await scheduler.acquire(n)
                try:
                    _rpc_t0 = time.monotonic()
                    result = await self._rpc(
                        "send", params, raise_on_rate_limit=True, timeout=send_timeout,
                    )
                    _rpc_duration = time.monotonic() - _rpc_t0
                    if result is not None:
                        success, err_msg = self._validate_send_result(result)
                        if success:
                            self._track_sent_timestamp(result)
                            await scheduler.report_rpc_duration(_rpc_duration, n)
                            logger.info(
                                "Signal batch %d/%d: %d attachments sent in %.1fs "
                                "(attempt %d/%d)",
                                idx + 1, len(att_batches), n, _rpc_duration,
                                attempt, SIGNAL_RATE_LIMIT_MAX_ATTEMPTS,
                            )
                        else:
                            logger.error(
                                "Signal: RPC send failed for batch %d/%d (%d attachments, "
                                "attempt %d/%d, rpc_duration=%.1fs): %s",
                                idx + 1, len(att_batches), n,
                                attempt, SIGNAL_RATE_LIMIT_MAX_ATTEMPTS,
                                _rpc_duration, err_msg,
                            )
                            # 对瞬时（非速率限制）失败重试一次
                            if attempt < SIGNAL_RATE_LIMIT_MAX_ATTEMPTS:
                                backoff = 2.0 ** attempt
                                logger.info(
                                    "Signal: retrying batch %d/%d after %.1fs backoff",
                                    idx + 1, len(att_batches), backoff,
                                )
                                await asyncio.sleep(backoff)
                                continue
                    else:
                        # 假设服务器未接受该批次，不扣除令牌
                        logger.error(
                            "Signal: RPC send failed for batch %d/%d (%d attachments, "
                            "attempt %d/%d, rpc_duration=%.1fs)",
                            idx + 1, len(att_batches), n,
                            attempt, SIGNAL_RATE_LIMIT_MAX_ATTEMPTS,
                            _rpc_duration,
                        )
                        # 对瞬时（非速率限制）失败重试一次
                        if attempt < SIGNAL_RATE_LIMIT_MAX_ATTEMPTS:
                            backoff = 2.0 ** attempt
                            logger.info(
                                "Signal: retrying batch %d/%d after %.1fs backoff",
                                idx + 1, len(att_batches), backoff,
                            )
                            await asyncio.sleep(backoff)
                            continue
                    break
                except SignalRateLimitError as e:
                    scheduler.feedback(e.retry_after, n)
                    if attempt >= SIGNAL_RATE_LIMIT_MAX_ATTEMPTS:
                        logger.error(
                            "Signal: rate-limit retries exhausted on batch %d/%d "
                            "(%d attachments lost, server retry_after=%s)",
                            idx + 1, len(att_batches), n,
                            f"{e.retry_after:.0f}s" if e.retry_after else "unknown",
                        )
                        break
                    logger.warning(
                        "Signal: rate-limited on batch %d/%d "
                        "(attempt %d/%d, server retry_after=%s); "
                        "scheduler will pace the retry",
                        idx + 1, len(att_batches),
                        attempt, SIGNAL_RATE_LIMIT_MAX_ATTEMPTS,
                        f"{e.retry_after:.0f}s" if e.retry_after else "unknown",
                    )

    async def _notify_batch_pacing(
        self,
        chat_id: str,
        next_batch_idx: int,
        total_batches: int,
        wait_s: float,
    ) -> None:
        """当批次间节奏等待超过提示阈值时通知用户。
        尽力而为；失败时记录日志并继续。"""
        try:
            await self.send(
                chat_id,
                f"(More images coming — pausing ~{_format_wait(wait_s)} "
                f"for Signal rate limit, batch {next_batch_idx}/{total_batches}.)",
            )
        except Exception as e:
            logger.warning("Signal: failed to send pacing notice: %s", e)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """发送一张图片。支持 http(s):// 和 file:// URL。"""
        await self._stop_typing_indicator(chat_id)

        # 将图片解析为本地路径
        if image_url.startswith("file://"):
            file_path = unquote(image_url[7:])
        else:
            # 将远程图片下载到缓存
            try:
                file_path = await cache_image_from_url(image_url)
            except Exception as e:
                logger.warning("Signal: failed to download image: %s", e)
                return SendResult(success=False, error=str(e))

        if not file_path or not Path(file_path).exists():
            return SendResult(success=False, error="Image file not found")

        # 校验大小
        file_size = Path(file_path).stat().st_size
        if file_size > SIGNAL_MAX_ATTACHMENT_SIZE:
            return SendResult(success=False, error=f"Image too large ({file_size} bytes)")

        params: Dict[str, Any] = {
            "account": self.account,
            "message": caption or "",
            "attachments": [file_path],
        }

        if chat_id.startswith("group:"):
            params["groupId"] = chat_id[6:]
        else:
            params["recipient"] = [await self._resolve_recipient(chat_id)]

        result = await self._rpc("send", params)
        if result is not None:
            success, err_msg = self._validate_send_result(result)
            if not success:
                return SendResult(success=False, error=err_msg, raw_response=result)
            self._track_sent_timestamp(result)
            return SendResult(success=True)
        return SendResult(success=False, error="RPC send with attachment failed")

    async def _send_attachment(
        self,
        chat_id: str,
        file_path: str,
        media_label: str,
        caption: Optional[str] = None,
    ) -> SendResult:
        """通过 RPC 将任意文件作为 Signal 附件发送。

        send_document、send_image_file、send_voice 和 send_video 共用此
        实现 —— 避免重复校验/路由/RPC 逻辑。
        """
        await self._stop_typing_indicator(chat_id)

        try:
            file_size = Path(file_path).stat().st_size
        except FileNotFoundError:
            return SendResult(success=False, error=f"{media_label} file not found: {file_path}")

        if file_size > SIGNAL_MAX_ATTACHMENT_SIZE:
            return SendResult(success=False, error=f"{media_label} too large ({file_size} bytes)")

        params: Dict[str, Any] = {
            "account": self.account,
            "message": caption or "",
            "attachments": [file_path],
        }

        if chat_id.startswith("group:"):
            params["groupId"] = chat_id[6:]
        else:
            params["recipient"] = [await self._resolve_recipient(chat_id)]

        result = await self._rpc("send", params)
        if result is not None:
            success, err_msg = self._validate_send_result(result)
            if not success:
                return SendResult(success=False, error=err_msg, raw_response=result)
            self._track_sent_timestamp(result)
            return SendResult(success=True)
        return SendResult(success=False, error=f"RPC send {media_label.lower()} failed")

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        filename: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """发送一个文档/文件附件。"""
        return await self._send_attachment(chat_id, file_path, "File", caption)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """将本地图片文件作为原生 Signal 附件发送。

        在 gateway 的媒体投递流程从 agent 响应中提取出包含图片路径的
        MEDIA: 标签时被调用。
        """
        return await self._send_attachment(chat_id, image_path, "Image", caption)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """将音频文件作为 Signal 附件发送。

        Signal 在 API 层面不区分语音消息和文件附件，
        因此这里走相同的 RPC send 路径。
        """
        return await self._send_attachment(chat_id, audio_path, "Audio", caption)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """将视频文件作为 Signal 附件发送。"""
        return await self._send_attachment(chat_id, video_path, "Video", caption)

    # ------------------------------------------------------------------
    # Typing 指示符
    # ------------------------------------------------------------------

    async def _stop_typing_indicator(self, chat_id: str) -> None:
        """停止某个聊天的 typing 指示符循环。"""
        task = self._typing_tasks.pop(chat_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # 发送显式的 stop-typing RPC，使接收者设备立即丢弃指示符，
        # 而非等待 Signal 约 5s 的内置超时。失败时尽力而为 —— 退避状态
        # 仍必须清除，使下一个 agent turn 干净启动。
        try:
            params: Dict[str, Any] = {"account": self.account}
            if chat_id.startswith("group:"):
                params["groupId"] = chat_id[6:]
            else:
                params["recipient"] = [await self._resolve_recipient(chat_id)]
            params["stop"] = True
            await self._rpc(
                "sendTyping",
                params,
                rpc_id="typing-stop",
                log_failures=False,
            )
        except Exception:
            # 尽力而为：任何 RPC 失败（或接收者解析失败）
            # 都不得阻止退避状态的清理。
            pass

        self._typing_failures.pop(chat_id, None)
        self._typing_skip_until.pop(chat_id, None)

    async def stop_typing(self, chat_id: str) -> None:
        """停止 typing 的公开接口 —— 由 base adapter 的
        _keep_typing finally 块调用，清理平台级 typing 任务。"""
        await self._stop_typing_indicator(chat_id)

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------

    async def send_reaction(
        self,
        chat_id: str,
        emoji: str,
        target_author: str,
        target_timestamp: int,
    ) -> bool:
        """通过 signal-cli RPC 向特定消息发送一个 reaction emoji。

        Args:
            chat_id: 聊天（电话号码或 "group:<id>"）
            emoji: Reaction emoji 字符串（例如 "👀"、"✅"）
            target_author: 消息作者的 phone number / UUID
            target_timestamp: 要对其 react 的消息的 Signal 时间戳（ms）
        """
        params: Dict[str, Any] = {
            "account": self.account,
            "emoji": emoji,
            "targetAuthor": target_author,
            "targetTimestamp": target_timestamp,
        }

        if chat_id.startswith("group:"):
            params["groupId"] = chat_id[6:]
        else:
            params["recipient"] = [chat_id]

        result = await self._rpc("sendReaction", params)
        if result is not None:
            return True
        logger.debug("Signal: sendReaction failed (chat=%s, emoji=%s)", chat_id[:20], emoji)
        return False

    async def remove_reaction(
        self,
        chat_id: str,
        target_author: str,
        target_timestamp: int,
    ) -> bool:
        """通过发送空字符串 emoji 来移除一个 reaction。"""
        params: Dict[str, Any] = {
            "account": self.account,
            "emoji": "",
            "targetAuthor": target_author,
            "targetTimestamp": target_timestamp,
            "remove": True,
        }

        if chat_id.startswith("group:"):
            params["groupId"] = chat_id[6:]
        else:
            params["recipient"] = [chat_id]

        result = await self._rpc("sendReaction", params)
        return result is not None

    # ------------------------------------------------------------------
    # 处理生命周期钩子（reactions 作为进度指示符）
    # ------------------------------------------------------------------

    def _extract_reaction_target(self, event: MessageEvent) -> Optional[tuple]:
        """从 MessageEvent 提取 (target_author, target_timestamp)。

        如果事件未携带 sendReaction 所需的原始 Signal envelope 数据，
        则返回 None。
        """
        raw = event.raw_message
        if not isinstance(raw, dict):
            return None
        author = raw.get("sender")
        ts = raw.get("timestamp_ms")
        if not author or not ts:
            return None
        return (author, ts)

    def _reactions_enabled(self, event: "MessageEvent" = None) -> bool:
        """检查是否对此事件启用消息 reactions。

        两道门控：
        1. SIGNAL_REACTIONS 环境变量 —— 设为 false/0/no 可全局禁用。
        2. DM 白名单 —— 如果设置了 SIGNAL_ALLOWED_USERS，仅对来自
           该列表发送者的消息 react。这防止未授权联系人看到 👀
           reaction（它在 run.py 的鉴权门控之前触发，否则会暴露
           有一个 bot 正在监听）。
        """
        if os.getenv("SIGNAL_REACTIONS", "true").lower() in {"false", "0", "no"}:
            return False
        if event is not None:
            sender = getattr(getattr(event, "source", None), "user_id", None)
            if sender and "*" not in self.dm_allow_from and sender not in self.dm_allow_from:
                return False
        return True

    async def on_processing_start(self, event: MessageEvent) -> None:
        """在处理开始时以 👀 作 reaction。"""
        if not self._reactions_enabled(event):
            return
        target = self._extract_reaction_target(event)
        if target:
            await self.send_reaction(event.source.chat_id, "👀", *target)

    async def on_processing_complete(self, event: MessageEvent, outcome: "ProcessingOutcome") -> None:
        """将 👀 reaction 替换为 ✅（成功）或 ❌（失败）。

        对于 CANCELLED 我们保留 👀 —— 没有终态结果意味着 reaction 应
        继续表示 "进行中"（与 Telegram 一致）。
        """
        if not self._reactions_enabled(event):
            return
        if outcome == ProcessingOutcome.CANCELLED:
            return
        target = self._extract_reaction_target(event)
        if not target:
            return
        chat_id = event.source.chat_id
        # 先移除进行中的 reaction，再添加最终 reaction
        await self.remove_reaction(chat_id, *target)
        if outcome == ProcessingOutcome.SUCCESS:
            await self.send_reaction(chat_id, "✅", *target)
        elif outcome == ProcessingOutcome.FAILURE:
            await self.send_reaction(chat_id, "❌", *target)

    # ------------------------------------------------------------------
    # 聊天信息
    # ------------------------------------------------------------------

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """获取某个聊天/联系人的信息。"""
        if chat_id.startswith("group:"):
            return {
                "name": chat_id,
                "type": "group",
                "chat_id": chat_id,
            }

        # 尝试解析联系人名称
        result = await self._rpc("getContact", {
            "account": self.account,
            "contactAddress": chat_id,
        })

        name = chat_id
        if result and isinstance(result, dict):
            name = result.get("name") or result.get("profileName") or chat_id

        return {
            "name": name,
            "type": "dm",
            "chat_id": chat_id,
        }
