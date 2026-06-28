"""
平台适配器基础接口。

所有平台适配器（Telegram、Discord、WhatsApp、Weixin 等）都继承自该接口，
并实现所需的方法。
"""

import asyncio
import inspect
import ipaddress
import logging
import os
import random
import re
import socket as _socket
import subprocess
import sys
import time
import uuid
from abc import ABC, abstractmethod
from urllib.parse import urlsplit

from utils import normalize_proxy_url

logger = logging.getLogger(__name__)

# Hermes 用于原生音频投递所识别的音频文件扩展名。
# 通过下面的 should_send_media_as_audio() 与 tools/send_message_tool.py
# 以及 cron/scheduler.py 保持同步。
_AUDIO_EXTS = frozenset({'.ogg', '.opus', '.mp3', '.wav', '.m4a', '.flac'})
# Telegram 的 Bot API sendAudio 仅接受 MP3 / M4A。其它音频
# 格式要么需要走 sendVoice（Opus/OGG），要么必须作为
# 普通文档来投递。
_TELEGRAM_AUDIO_ATTACHMENT_EXTS = frozenset({'.mp3', '.m4a'})
_TELEGRAM_VOICE_EXTS = frozenset({'.ogg', '.opus'})
_POST_DELIVERY_CALLBACK_TIMEOUT_SECONDS = 30.0


def _platform_name(platform) -> str:
    """将 Platform 枚举 / 原始字符串统一规范为小写名称。"""
    value = getattr(platform, "value", platform)
    return str(value or "").lower()


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _thread_metadata_for_source(source, reply_to_message_id: str | None = None) -> dict | None:
    """为适配器的发送构建具备平台感知能力的 thread 元数据。

    多数平台通过一个通用的 ``thread_id`` 元数据值来路由带 thread 的发送。
    通过 Hermes 的 DM-topic 辅助工具创建的 Telegram 私聊 topic，在更新中
    会以 ``message_thread_id`` 加上一个回复锚点的形式暴露。用户实时消息
    的回复通过 ``message_thread_id`` + ``reply_to_message_id`` 路由；
    没有回复锚点的合成/恢复发送，会在 Bot API 支持时回退到 Telegram 的
    ``direct_messages_topic_id``。
    """
    thread_id = getattr(source, "thread_id", None)
    if thread_id is None:
        return None
    metadata = {"thread_id": thread_id}
    if _platform_name(getattr(source, "platform", None)) == "telegram" and getattr(source, "chat_type", None) == "dm":
        metadata["telegram_dm_topic_reply_fallback"] = True
        tid = str(thread_id)
        if tid and tid not in {"", "1"}:
            metadata["direct_messages_topic_id"] = tid
        anchor = reply_to_message_id or getattr(source, "message_id", None)
        if anchor is not None:
            metadata["telegram_reply_to_message_id"] = str(anchor)
    return metadata


def _mark_notify_metadata(metadata: dict | None) -> dict:
    """克隆元数据，并把一条用户可见的回复标记为值得通知。"""
    notify_metadata = dict(metadata) if metadata else {}
    notify_metadata["notify"] = True
    return notify_metadata


def _reply_anchor_for_event(event) -> str | None:
    """为需要回复语义的平台返回 reply_to id。

    Telegram 论坛/超级群的 topic 应通过 topic 元数据来路由，而不是通过
    回复触发消息来路由。Hermes 创建的 Telegram 私聊 topic 通道更倾向于
    回复触发的用户消息，这样回答能附着在活跃的通道上；在没有可用消息 id
    时，合成/恢复的发送会回退到 ``direct_messages_topic_id`` 元数据。
    """
    source = getattr(event, "source", None)
    platform = _platform_name(getattr(source, "platform", None))
    thread_id = getattr(source, "thread_id", None)
    if platform == "telegram" and thread_id and getattr(source, "chat_type", None) == "dm":
        # 回复触发的用户消息。如果回复 Telegram 较早的
        # topic 种子/锚点，可能会让 bot 的回复出现在活跃通道之外。
        return getattr(event, "message_id", None) or getattr(event, "reply_to_message_id", None)
    if platform == "telegram" and thread_id:
        return None
    if platform == "feishu" and thread_id and getattr(event, "reply_to_message_id", None):
        return getattr(event, "reply_to_message_id", None)
    return getattr(event, "message_id", None)


def should_send_media_as_audio(platform, ext: str, is_voice: bool = False) -> bool:
    """当媒体文件应使用平台的音频发送器时返回 True。

    其它平台：每个被识别的音频扩展名都会通过音频发送器路由。

    Telegram：Bot API 的 sendAudio 只接受 MP3/M4A，sendVoice
    只接受 Opus/OGG。仅当调用方设置 ``is_voice=True`` 时，Opus/OGG
    才会被当作音频路由（这样我们就不会仅仅因为文件恰好是 Opus 就把
    一个普通音频附件变成一个语音气泡）。其它所有情况都通过返回
    ``False`` 落入文档投递路径。
    """
    normalized_ext = (ext or "").lower()
    if normalized_ext not in _AUDIO_EXTS:
        return False
    if _platform_name(platform) == "telegram":
        if normalized_ext in _TELEGRAM_VOICE_EXTS:
            return is_voice
        return normalized_ext in _TELEGRAM_AUDIO_ATTACHMENT_EXTS
    return True


def utf16_len(s: str) -> int:
    """统计 *s* 中的 UTF-16 码元数量。

    Telegram 的消息长度上限（4096）以 UTF-16 码元为单位计量，而
    **不是** Unicode 码点。基本多文种平面（BMP）之外的字符（如 😀 这样的
    emoji、CJK 扩展 B、音乐符号……）会被编码为代理对，因此每个字符会占用
    **两个** UTF-16 码元，尽管 Python 的 ``len()`` 会把它们计为 1。

    移植自 nearai/ironclaw#2304，该 issue 在 Rust 的
    ``chars().count()`` 中发现了同样的差异。
    """
    return len(s.encode("utf-16-le")) // 2


def _prefix_within_utf16_limit(s: str, limit: int) -> str:
    """返回 *s* 中 UTF-16 长度 ≤ *limit* 的最长前缀。

    与单纯的 ``s[:limit]`` 不同，本函数会尊重代理对边界，因此绝不会把一个
    占多个码元的字符从中间切断。
    """
    if utf16_len(s) <= limit:
        return s
    # 二分查找最长的安全前缀
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if utf16_len(s[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return s[:lo]


def _custom_unit_to_cp(s: str, budget: int, len_fn) -> int:
    """返回最大的码点偏移量 *n*，使得 ``len_fn(s[:n]) <= budget``。

    在 *len_fn* 使用与 Python 码点不同的单位计量长度时（例如 UTF-16 码元），
    被 :meth:`BasePlatformAdapter.truncate_message` 使用。
    回退到二分查找，调用 *len_fn* 的次数为 O(log n)。
    """
    if len_fn(s) <= budget:
        return len(s)
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len_fn(s[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo


def is_network_accessible(host: str) -> bool:
    """当 *host* 会使服务器暴露在 loopback 之外时返回 True。

    Loopback 地址（127.0.0.1、::1、IPv4-mapped ::ffff:127.0.0.1）
    仅限本地。未指定地址（0.0.0.0、::）会绑定所有接口。
    主机名会被解析；DNS 失败时按失败（fail closed）处理。
    """
    try:
        addr = ipaddress.ip_address(host)
        if addr.is_loopback:
            return False
        # ::ffff:127.0.0.1 —— 对于 mapped 地址，Python 报告 is_loopback=False，
        # 因此需要显式检查其底层 IPv4。
        if getattr(addr, "ipv4_mapped", None) and addr.ipv4_mapped.is_loopback:
            return False
        return True
    except ValueError:
        # 当 host 变量是一个主机名时，我们在下面尝试解析它
        pass

    try:
        resolved = _socket.getaddrinfo(
            host, None, _socket.AF_UNSPEC, _socket.SOCK_STREAM,
        )
        # 如果主机名至少解析到一个非 loopback 地址，
        # 我们就认为它是网络可达的
        for _family, _type, _proto, _canonname, sockaddr in resolved:
            addr = ipaddress.ip_address(sockaddr[0])
            if not addr.is_loopback:
                return True
        return False
    except (_socket.gaierror, OSError):
        return True


def _detect_macos_system_proxy() -> str | None:
    """通过 ``scutil --proxy`` 读取 macOS 系统 HTTP(S) 代理。

    如果启用了 HTTP 或 HTTPS 代理，则返回 ``http://host:port`` URL 字符串，
    否则返回 *None*。在非 macOS 平台或任何子进程错误时静默回退。
    """
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.check_output(
            ["scutil", "--proxy"], timeout=3, text=True, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None

    props: dict[str, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if " : " in line:
            key, _, val = line.partition(" : ")
            props[key.strip()] = val.strip()

    # 优先 HTTPS，回退到 HTTP
    for enable_key, host_key, port_key in (
        ("HTTPSEnable", "HTTPSProxy", "HTTPSPort"),
        ("HTTPEnable", "HTTPProxy", "HTTPPort"),
    ):
        if props.get(enable_key) == "1":
            host = props.get(host_key)
            port = props.get(port_key)
            if host and port:
                return f"http://{host}:{port}"
    return None


def _split_host_port(value: str) -> tuple[str, int | None]:
    raw = str(value or "").strip()
    if not raw:
        return "", None
    if "://" in raw:
        parsed = urlsplit(raw)
        return (parsed.hostname or "").lower().rstrip("."), parsed.port
    if raw.startswith("[") and "]" in raw:
        host, _, rest = raw[1:].partition("]")
        port = None
        if rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
        return host.lower().rstrip("."), port
    if raw.count(":") == 1:
        host, _, maybe_port = raw.rpartition(":")
        if maybe_port.isdigit():
            return host.lower().rstrip("."), int(maybe_port)
    return raw.lower().strip("[]").rstrip("."), None


def _no_proxy_entries() -> list[str]:
    entries: list[str] = []
    for key in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(key, "")
        entries.extend(part.strip() for part in raw.split(",") if part.strip())
    return entries


def _no_proxy_entry_matches(entry: str, host: str, port: int | None = None) -> bool:
    token = str(entry or "").strip().lower()
    if not token:
        return False
    if token == "*":
        return True

    token_host, token_port = _split_host_port(token)
    if token_port is not None and port is not None and token_port != port:
        return False
    if token_port is not None and port is None:
        return False
    if not token_host:
        return False

    try:
        network = ipaddress.ip_network(token_host, strict=False)
        try:
            return ipaddress.ip_address(host) in network
        except ValueError:
            return False
    except ValueError:
        pass

    try:
        token_ip = ipaddress.ip_address(token_host)
        try:
            return ipaddress.ip_address(host) == token_ip
        except ValueError:
            return False
    except ValueError:
        pass

    if token_host.startswith("*."):
        suffix = token_host[1:]
        return host.endswith(suffix)
    if token_host.startswith("."):
        return host == token_host[1:] or host.endswith(token_host)
    return host == token_host or host.endswith(f".{token_host}")


def should_bypass_proxy(target_hosts: str | list[str] | tuple[str, ...] | set[str] | None) -> bool:
    """当 NO_PROXY/no_proxy 匹配至少一个目标 host 时返回 True。

    支持精确主机名、域名后缀、通配符后缀、IP 字面量、
    CIDR 范围、可选的 host:port 条目以及 ``*``。
    """
    entries = _no_proxy_entries()
    if not entries or not target_hosts:
        return False
    if isinstance(target_hosts, str):
        candidates = [target_hosts]
    else:
        candidates = list(target_hosts)
    for candidate in candidates:
        host, port = _split_host_port(str(candidate))
        if not host:
            continue
        if any(_no_proxy_entry_matches(entry, host, port) for entry in entries):
            return True
    return False


def resolve_proxy_url(
    platform_env_var: str | None = None,
    *,
    target_hosts: str | list[str] | tuple[str, ...] | set[str] | None = None,
) -> str | None:
    """从环境变量或 macOS 系统代理返回一个代理 URL。

    检查顺序：
      0. *platform_env_var*（例如 ``DISCORD_PROXY``）——优先级最高
      1. HTTPS_PROXY / HTTP_PROXY / ALL_PROXY（及其小写变体）
      2. 通过 ``scutil --proxy`` 获取 macOS 系统代理（自动检测）

    如果没有找到代理，或者 NO_PROXY/no_proxy 匹配了某个 ``target_hosts``，
    则返回 *None*。
    """
    if platform_env_var:
        value = (os.environ.get(platform_env_var) or "").strip()
        if value:
            if should_bypass_proxy(target_hosts):
                return None
            return normalize_proxy_url(value)
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
                "https_proxy", "http_proxy", "all_proxy"):
        value = (os.environ.get(key) or "").strip()
        if value:
            if should_bypass_proxy(target_hosts):
                return None
            return normalize_proxy_url(value)
    detected = normalize_proxy_url(_detect_macos_system_proxy())
    if detected and should_bypass_proxy(target_hosts):
        return None
    return detected


def proxy_kwargs_for_bot(proxy_url: str | None) -> dict:
    """为带代理的 ``commands.Bot()`` / ``discord.Client()`` 构建 kwargs。

    返回值：
      - SOCKS URL  → ``{"connector": ProxyConnector(..., rdns=True)}``
      - HTTP URL   → ``{"proxy": url}``
      - *None*     → ``{}``

    ``rdns=True`` 强制通过代理进行远程 DNS 解析——许多 SOCKS 实现
    （Shadowrocket、Clash）需要这样做，对于绕过 GFW 内的 DNS 污染也至关重要。
    """
    if not proxy_url:
        return {}
    if proxy_url.lower().startswith("socks"):
        try:
            from aiohttp_socks import ProxyConnector

            connector = ProxyConnector.from_url(proxy_url, rdns=True)
            return {"connector": connector}
        except ImportError:
            logger.warning(
                "aiohttp_socks not installed — SOCKS proxy %s ignored. "
                "Run: pip install aiohttp-socks",
                proxy_url,
            )
            return {}
    return {"proxy": proxy_url}


def proxy_kwargs_for_aiohttp(proxy_url: str | None) -> tuple[dict, dict]:
    """为独立的 ``aiohttp.ClientSession`` 构建带代理的 kwargs。

    返回 ``(session_kwargs, request_kwargs)``，其中：
      - 使用 aiohttp-socks 时 → 对于*所有*代理协议（SOCKS **以及** HTTP/HTTPS），
        返回 ``({"connector": ProxyConnector(...)}, {})``。
      - 不带 aiohttp-socks 的 HTTP → ``({}, {"proxy": url})``。
      - None → ``({}, {})``。

    优先使用 connector 路径：它能与那些调用 ``session.request()``
    但不转发 per-request ``proxy=`` kwargs 的库（如 mautrix）透明协作。

    用法::

        sess_kw, req_kw = proxy_kwargs_for_aiohttp(proxy_url)
        async with aiohttp.ClientSession(**sess_kw) as session:
            async with session.get(url, **req_kw) as resp:
                ...
    """
    if not proxy_url:
        return {}, {}
    try:
        from aiohttp_socks import ProxyConnector

        connector = ProxyConnector.from_url(proxy_url, rdns=True)
        return {"connector": connector}, {}
    except ImportError:
        if proxy_url.lower().startswith("socks"):
            logger.warning(
                "aiohttp_socks not installed — SOCKS proxy %s ignored. "
                "Run: pip install aiohttp-socks",
                proxy_url,
            )
            return {}, {}
        return {}, {"proxy": proxy_url}


def is_host_excluded_by_no_proxy(hostname: str, no_proxy_value: str | None = None) -> bool:
    """当 ``hostname`` 匹配某个 ``NO_PROXY`` 条目时返回 True。

    支持以逗号或空白分隔的条目，可带可选的前导点和 ``*.`` 通配符，
    这些会同时匹配顶级域名及其子域名。
    """
    raw = no_proxy_value
    if raw is None:
        raw = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""

    raw = raw.strip()
    if not raw:
        return False

    lower_hostname = hostname.lower()
    for entry in re.split(r"[\s,]+", raw):
        normalized = entry.strip().lower()
        if not normalized:
            continue
        if normalized == "*":
            return True

        if normalized.startswith("*."):
            normalized = normalized[2:]
        elif normalized.startswith("."):
            normalized = normalized[1:]

        if lower_hostname == normalized or lower_hostname.endswith(f".{normalized}"):
            return True

    return False


import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable, Awaitable, Tuple, Union
from enum import Enum

from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource, build_session_key
from hermes_constants import get_default_hermes_root, get_hermes_dir, get_hermes_home


GATEWAY_SECRET_CAPTURE_UNSUPPORTED_MESSAGE = (
    "Secure secret entry is not supported over messaging. "
    "Load this skill in the local CLI to be prompted, or add the key to ~/.hermes/.env manually."
)


def safe_url_for_log(url: str, max_len: int = 80) -> str:
    """返回适合日志输出的安全 URL 字符串（不含 query/fragment/userinfo）。"""
    if max_len <= 0:
        return ""

    if url is None:
        return ""

    raw = str(url)
    if not raw:
        return ""

    try:
        parsed = urlsplit(raw)
    except Exception:
        return raw[:max_len]

    if parsed.scheme and parsed.netloc:
        # 剥除可能嵌入的凭据（user:pass@host）。
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        base = f"{parsed.scheme}://{netloc}"
        path = parsed.path or ""
        if path and path != "/":
            basename = path.rsplit("/", 1)[-1]
            safe = f"{base}/.../{basename}" if basename else f"{base}/..."
        else:
            safe = base
    else:
        safe = raw

    if len(safe) <= max_len:
        return safe
    if max_len <= 3:
        return "." * max_len
    return f"{safe[:max_len - 3]}..."


async def _ssrf_redirect_guard(response):
    """对每个重定向目标重新校验，以防止基于重定向的 SSRF。

    如果没有这一层，攻击者可以托管一个公开 URL，将其 302 重定向到
    http://169.254.169.254/，从而绕过 pre-flight 的 is_safe_url() 检查。

    必须是 async 的，因为 httpx.AsyncClient 会 await 响应事件钩子。
    """
    if response.is_redirect and response.next_request:
        redirect_url = str(response.next_request.url)
        from tools.url_safety import is_safe_url
        if not is_safe_url(redirect_url):
            raise ValueError(
                f"Blocked redirect to private/internal address: {safe_url_for_log(redirect_url)}"
            )


# ---------------------------------------------------------------------------
# 图片缓存辅助工具
#
# 当用户在消息平台上发送图片时，我们会把它们下载到一个本地缓存目录，
# 以便 vision 工具（接受本地文件路径）分析。这样可以避免平台 URL
# 过期带来的问题（例如 Telegram 的文件 URL 会在约 1 小时后过期）。
# ---------------------------------------------------------------------------

# 默认位置：{HERMES_HOME}/cache/images/（旧版：image_cache/）
IMAGE_CACHE_DIR = get_hermes_dir("cache/images", "image_cache")

# ---------------------------------------------------------------------------
# 入站媒体大小上限（#13145）
#
# 入站的图片 / 音频 / 视频载荷在被写入缓存目录之前，会完整地缓冲到
# 进程内存中。如果没有上限，单次大上传（Discord Nitro 允许 500 MB）——
# 或者入站消息载荷中一个指向任意大文件的远程 URL——都会让内存飙升，
# 把网关 OOM 杀掉。``cache_*_from_bytes`` 辅助函数（每个平台最终都会
# 走到的共享漏斗）以及 ``cache_*_from_url`` 下载器会强制执行这个上限，
# 因此无论哪个平台适配器或代码路径产生了这些字节，这层保护都成立。
#
# 可通过 config.yaml 中的 ``gateway.max_inbound_media_bytes`` 配置。
# ``0`` 表示禁用上限。默认 128 MiB——对普通照片/语音消息/短视频足够宽裕，
# 同时仍能约束恶意上传。
# ---------------------------------------------------------------------------
DEFAULT_INBOUND_MEDIA_MAX_BYTES = 128 * 1024 * 1024


def get_inbound_media_max_bytes() -> int:
    """返回允许载入内存的入站图片/音频/视频最大字节数。

    从 config.yaml 读取 ``gateway.max_inbound_media_bytes``。``0``（或
    负数 / 无法解析的值）表示禁用上限。如果配置不可读，也不是致命错误——
    回退到默认值。
    """
    try:
        from hermes_cli.config import load_config as _load_config
        cfg = _load_config()
    except Exception:
        return DEFAULT_INBOUND_MEDIA_MAX_BYTES
    gw = cfg.get("gateway", {}) if isinstance(cfg, dict) else {}
    if not isinstance(gw, dict) or "max_inbound_media_bytes" not in gw:
        return DEFAULT_INBOUND_MEDIA_MAX_BYTES
    try:
        return int(gw["max_inbound_media_bytes"])
    except (TypeError, ValueError):
        return DEFAULT_INBOUND_MEDIA_MAX_BYTES


def validate_inbound_media_size(
    size: int,
    *,
    media_type: str = "media",
    max_bytes: Optional[int] = None,
) -> None:
    """当入站媒体载荷超过上限时抛出 ``ValueError``。

    ``max_bytes`` 为 ``0``（或解析出的配置上限为 ``0``）时
    完全禁用该检查。传入 ``max_bytes`` 可让调用方解析一次上限，
    然后在增量读取过程中复用。
    """
    limit = get_inbound_media_max_bytes() if max_bytes is None else max_bytes
    if limit and size > limit:
        raise ValueError(
            f"Inbound {media_type} payload is too large "
            f"({size} bytes > {limit} bytes)"
        )


async def _read_httpx_body_with_limit(response, *, media_type: str) -> bytes:
    """读取 httpx 流式响应体，同时不超过媒体上限。

    在 ``Content-Length`` 头声明的大小超限时及早拒绝，随后在分块到达时
    重新检查累计总量，这样即便头部撒谎或缺失，也无法把一个无界的响应体
    偷渡过上限。
    """
    max_bytes = get_inbound_media_max_bytes()
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError:
            logger.debug(
                "Ignoring invalid Content-Length for inbound %s: %r",
                media_type, content_length,
            )
        else:
            validate_inbound_media_size(
                declared_size, media_type=media_type, max_bytes=max_bytes,
            )

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        validate_inbound_media_size(total, media_type=media_type, max_bytes=max_bytes)
        chunks.append(chunk)
    return b"".join(chunks)


def get_image_cache_dir() -> Path:
    """返回图片缓存目录，如不存在则创建。"""
    IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return IMAGE_CACHE_DIR


def _looks_like_image(data: bytes) -> bool:
    """当 *data* 以已知的图片魔数字节序列开头时返回 True。"""
    if len(data) < 4:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:3] == b"\xff\xd8\xff":
        return True
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return True
    if data[:2] == b"BM":
        return True
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return True
    return False


def cache_image_from_bytes(data: bytes, ext: str = ".jpg") -> str:
    """
    将原始图片字节保存到缓存，并返回绝对文件路径。

    Args:
        data: 原始图片字节。
        ext:  含点号的文件扩展名（如 ".jpg"、".png"）。

    Returns:
        缓存图片文件的绝对路径字符串。

    Raises:
        ValueError: 如果 *data* 看起来不是一个合法的图片（例如上游服务器
            返回的 HTML 错误页面）。
    """
    validate_inbound_media_size(len(data), media_type="image")
    if not _looks_like_image(data):
        snippet = data[:80].decode("utf-8", errors="replace")
        raise ValueError(
            f"Refusing to cache non-image data as {ext} "
            f"(starts with: {snippet!r})"
        )
    cache_dir = get_image_cache_dir()
    filename = f"img_{uuid.uuid4().hex[:12]}{ext}"
    filepath = cache_dir / filename
    filepath.write_bytes(data)
    return str(filepath)


async def cache_image_from_url(url: str, ext: str = ".jpg", retries: int = 2) -> str:
    """
    从 URL 下载图片并保存到本地缓存。

    在瞬时故障（超时、429、5xx）时以指数退避重试，这样一次缓慢的 CDN
    响应不会让媒体丢失。

    Args:
        url: 要下载的 HTTP/HTTPS URL。
        ext: 含点号的文件扩展名（如 ".jpg"、".png"）。
        retries: 针对瞬时故障的重试次数。

    Returns:
        缓存图片文件的绝对路径字符串。

    Raises:
        ValueError: 如果 URL 指向私有/内部网络（SSRF 防护）。
    """
    from tools.url_safety import is_safe_url
    if not is_safe_url(url):
        raise ValueError(f"Blocked unsafe URL (SSRF protection): {safe_url_for_log(url)}")

    import httpx
    _log = logging.getLogger(__name__)

    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        event_hooks={"response": [_ssrf_redirect_guard]},
    ) as client:
        for attempt in range(retries + 1):
            try:
                async with client.stream(
                    "GET",
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)",
                        "Accept": "image/*,*/*;q=0.8",
                    },
                ) as response:
                    response.raise_for_status()
                    content = await _read_httpx_body_with_limit(
                        response, media_type="image",
                    )
                return cache_image_from_bytes(content, ext)
            except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 429:
                    raise
                if attempt < retries:
                    wait = 1.5 * (attempt + 1)
                    _log.debug(
                        "Media cache retry %d/%d for %s (%.1fs): %s",
                        attempt + 1,
                        retries,
                        safe_url_for_log(url),
                        wait,
                        exc,
                    )
                    await asyncio.sleep(wait)
                    continue
                raise


def cleanup_image_cache(max_age_hours: int = 24) -> int:
    """
    删除早于 *max_age_hours* 的缓存图片。

    返回被删除的文件数量。
    """
    import time

    cache_dir = get_image_cache_dir()
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for f in cache_dir.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------------------
# 音频缓存辅助工具
#
# 与图片缓存相同的模式——来自平台的语音消息会下载到这里，
# 以便 STT 工具（OpenAI Whisper）从本地文件转录。
# ---------------------------------------------------------------------------

AUDIO_CACHE_DIR = get_hermes_dir("cache/audio", "audio_cache")


def get_audio_cache_dir() -> Path:
    """返回音频缓存目录，如不存在则创建。"""
    AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return AUDIO_CACHE_DIR


def cache_audio_from_bytes(data: bytes, ext: str = ".ogg") -> str:
    """
    将原始音频字节保存到缓存，并返回绝对文件路径。

    Args:
        data: 原始音频字节。
        ext:  含点号的文件扩展名（如 ".ogg"、".mp3"）。

    Returns:
        缓存音频文件的绝对路径字符串。
    """
    validate_inbound_media_size(len(data), media_type="audio")
    cache_dir = get_audio_cache_dir()
    filename = f"audio_{uuid.uuid4().hex[:12]}{ext}"
    filepath = cache_dir / filename
    filepath.write_bytes(data)
    return str(filepath)


async def cache_audio_from_url(url: str, ext: str = ".ogg", retries: int = 2) -> str:
    """
    从 URL 下载音频文件并保存到本地缓存。

    在瞬时故障（超时、429、5xx）时以指数退避重试，这样一次缓慢的 CDN
    响应不会让媒体丢失。

    Args:
        url: 要下载的 HTTP/HTTPS URL。
        ext: 含点号的文件扩展名（如 ".ogg"、".mp3"）。
        retries: 针对瞬时故障的重试次数。

    Returns:
        缓存音频文件的绝对路径字符串。

    Raises:
        ValueError: 如果 URL 指向私有/内部网络（SSRF 防护）。
    """
    from tools.url_safety import is_safe_url
    if not is_safe_url(url):
        raise ValueError(f"Blocked unsafe URL (SSRF protection): {safe_url_for_log(url)}")

    import httpx
    _log = logging.getLogger(__name__)

    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        event_hooks={"response": [_ssrf_redirect_guard]},
    ) as client:
        for attempt in range(retries + 1):
            try:
                async with client.stream(
                    "GET",
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)",
                        "Accept": "audio/*,*/*;q=0.8",
                    },
                ) as response:
                    response.raise_for_status()
                    content = await _read_httpx_body_with_limit(
                        response, media_type="audio",
                    )
                return cache_audio_from_bytes(content, ext)
            except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 429:
                    raise
                if attempt < retries:
                    wait = 1.5 * (attempt + 1)
                    _log.debug(
                        "Audio cache retry %d/%d for %s (%.1fs): %s",
                        attempt + 1,
                        retries,
                        safe_url_for_log(url),
                        wait,
                        exc,
                    )
                    await asyncio.sleep(wait)
                    continue
                raise


# ---------------------------------------------------------------------------
# 视频缓存辅助工具
#
# 与图片/音频缓存相同的模式——来自平台的视频会下载到这里，
# 以便 agent 通过本地文件路径引用它们。
# ---------------------------------------------------------------------------

VIDEO_CACHE_DIR = get_hermes_dir("cache/videos", "video_cache")

SUPPORTED_VIDEO_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
}


def get_video_cache_dir() -> Path:
    """返回视频缓存目录，如不存在则创建。"""
    VIDEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return VIDEO_CACHE_DIR


def cache_video_from_bytes(data: bytes, ext: str = ".mp4") -> str:
    """将原始视频字节保存到缓存，并返回绝对文件路径。"""
    validate_inbound_media_size(len(data), media_type="video")
    cache_dir = get_video_cache_dir()
    filename = f"video_{uuid.uuid4().hex[:12]}{ext}"
    filepath = cache_dir / filename
    filepath.write_bytes(data)
    return str(filepath)


# ---------------------------------------------------------------------------
# 文档缓存辅助工具
#
# 与图片/音频缓存相同的模式——来自平台的文档会下载到这里，
# 以便 agent 通过本地文件路径引用它们。
# ---------------------------------------------------------------------------

DOCUMENT_CACHE_DIR = get_hermes_dir("cache/documents", "document_cache")
SCREENSHOT_CACHE_DIR = get_hermes_dir("cache/screenshots", "browser_screenshots")
_HERMES_HOME = get_hermes_home()
_HERMES_ROOT = get_default_hermes_root()
MEDIA_DELIVERY_ALLOW_DIRS_ENV = "HERMES_MEDIA_ALLOW_DIRS"
MEDIA_DELIVERY_TRUST_RECENT_ENV = "HERMES_MEDIA_TRUST_RECENT_FILES"
MEDIA_DELIVERY_TRUST_RECENT_SECONDS_ENV = "HERMES_MEDIA_TRUST_RECENT_SECONDS"
# 严格模式切换原始的 allowlist+recency 路径校验行为。
# 默认关闭——与入站对称（我们接受用户上传的任何文档类型），
# 同时 denylist 仍然会拦截明显的凭据 / 系统路径。运行面向公众网关的
# 运维者，如果某个用户的 prompt 注入可能把宿主机的机密外泄给同一个用户，
# 就应把此项设为 true。
MEDIA_DELIVERY_STRICT_ENV = "HERMES_MEDIA_DELIVERY_STRICT"
MEDIA_DELIVERY_SAFE_ROOTS = (
    IMAGE_CACHE_DIR,
    AUDIO_CACHE_DIR,
    VIDEO_CACHE_DIR,
    DOCUMENT_CACHE_DIR,
    SCREENSHOT_CACHE_DIR,
    _HERMES_HOME / "image_cache",
    _HERMES_HOME / "audio_cache",
    _HERMES_HOME / "video_cache",
    _HERMES_HOME / "document_cache",
    _HERMES_HOME / "browser_screenshots",
    # 规范的缓存布局——与旧版的 *_cache 目录一并列出，这样在两者
    # 同时存在的安装上也能投递生成的产物（#31733）。
    _HERMES_HOME / "cache" / "images",
    _HERMES_HOME / "cache" / "audio",
    _HERMES_HOME / "cache" / "videos",
    _HERMES_HOME / "cache" / "documents",
    _HERMES_HOME / "cache" / "screenshots",
)

# 默认的、用于信任新生成文件的 recency 时间窗口（秒）。
# agent 的实际工作通常在 10 分钟内完成；合法的构建产物
# （pandoc 生成的 PDF、matplotlib 生成的图表等）几乎总是在投递前几秒落地。
# 旧系统文件（/etc/passwd、~/.ssh/id_rsa、散落的凭据）的 mtime 以天或月计——
# 远在这个窗口之外——因此指向宿主机既有文件的 prompt 注入路径仍会被拒绝。
_MEDIA_DELIVERY_TRUST_RECENT_DEFAULT_SECONDS = 600

# 硬性 denylist，即便某条路径原本能通过 recency 信任也照样适用。
# 这些前缀存放着凭据、系统状态或进程内省信息，无论文件看起来多新，
# 都绝不应作为网关附件上传。缓存目录的 allowlist 仍然优先于此——
# 运维者配置的允许根目录可以有意位于这些前缀之下（罕见，但属其选择）。
_MEDIA_DELIVERY_DENIED_PREFIXES = (
    "/etc",
    "/proc",
    "/sys",
    "/dev",
    "/root",
    "/boot",
    "/var/log",
    "/var/lib",
    "/var/run",
)

# 在 $HOME 内，我们额外禁止常见的凭据 / 配置目录。
# 在校验时依据实时 $HOME 解析，这样容器和 alt-home 设置也能正常工作。
_MEDIA_DELIVERY_DENIED_HOME_SUBPATHS = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".docker",
    ".config",
    ".azure",
    ".gcloud",
    "Library/Keychains",  # macOS
)


def _media_delivery_allowed_roots() -> List[Path]:
    """返回允许投递模型生成的本地媒体的根目录。"""
    roots = [Path(root) for root in MEDIA_DELIVERY_SAFE_ROOTS]
    extra_roots = os.environ.get(MEDIA_DELIVERY_ALLOW_DIRS_ENV, "")
    for chunk in extra_roots.split(os.pathsep):
        for raw_root in chunk.split(","):
            raw_root = raw_root.strip()
            if not raw_root:
                continue
            root = Path(os.path.expanduser(raw_root))
            if root.is_absolute():
                roots.append(root)
    return roots


def _media_delivery_recency_seconds() -> float:
    """返回信任新生成文件的 recency 时间窗口。

    0 表示完全禁用基于 recency 的信任（纯 allowlist 模式）。
    """
    raw = os.environ.get(MEDIA_DELIVERY_TRUST_RECENT_ENV, "1").strip().lower()
    if raw in ("0", "false", "no", "off", ""):
        return 0.0
    try:
        custom = os.environ.get(MEDIA_DELIVERY_TRUST_RECENT_SECONDS_ENV, "").strip()
        if custom:
            seconds = float(custom)
            return max(0.0, seconds)
    except (TypeError, ValueError):
        pass
    return float(_MEDIA_DELIVERY_TRUST_RECENT_DEFAULT_SECONDS)


def _media_delivery_strict_mode() -> bool:
    """当路径校验应要求匹配 allowlist/recency 时返回 True。

    默认关闭。在非严格模式下，``validate_media_delivery_path``
    接受任何不在凭据 / 系统路径 denylist 下的既有普通文件——
    这为单用户场景恢复了 #29523 之前的行为。严格模式为运行面向公众
    网关的运维者保留了原始的 allowlist+recency 窗口逻辑，在这种场景下，
    某个用户的 prompt 注入不应能把宿主机的机密外泄给同一个用户。
    """
    raw = os.environ.get(MEDIA_DELIVERY_STRICT_ENV, "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _media_delivery_denied_paths() -> List[Path]:
    """返回绝不允许投递的绝对 denylist 路径。"""
    denied = [Path(p) for p in _MEDIA_DELIVERY_DENIED_PREFIXES]
    home = Path(os.path.expanduser("~"))
    for sub in _MEDIA_DELIVERY_DENIED_HOME_SUBPATHS:
        denied.append(home / sub)
    # 活动的 Hermes profile 和共享的 Hermes root 都包含控制文件和凭据。
    # 只有它们之下的缓存子目录在上面被显式 allowlist（在
    # validate_media_delivery_path 中先于此 denylist 匹配，因此生成的媒体仍会投递）。
    #
    # 这些是位于 HERMES_HOME 根目录下的逐文件凭据 / 机密存储。该集合
    # 与 agent/file_safety.py（get_read_block_error / build_write_denied_*）
    # 中的规范读取守卫保持一致，使投递（读取/外泄）侧不会滞后于写入侧：
    # 一个禁止 agent 写入或读取的凭据也绝不应被自动附加到聊天回复中。
    # 此处逐文件显式枚举而非整棵树禁止，这样 skills/、logs/ 以及 ~/.hermes 下
    # agent 临时写入的文件仍可投递（见 #32090、#34425）。
    _ROOT_CREDENTIAL_FILES = (
        ".env",
        "auth.json",
        "auth.lock",
        "credentials",
        "config.yaml",
        # Anthropic PKCE / OAuth 刷新凭据存储。
        ".anthropic_oauth.json",
        # Google Workspace skill：自动刷新的 OAuth token（mtime 每轮都会
        # 更新，这曾让严格模式的 recency 窗口失效）以及待交换的
        # session/verifier 文件。
        "google_token.json",
        "google_oauth_pending.json",
        os.path.join("auth", "google_oauth.json"),
        # Webhook 订阅的 HMAC 机密。
        "webhook_subscriptions.json",
        # Bitwarden Secrets Manager 明文磁盘缓存。
        os.path.join("cache", "bws_cache.json"),
    )
    # 每个子节点都是凭据材料的目录树。（mcp-tokens/ 下的 MCP OAuth
    # token 由同期的定向 PR #37222 处理；session/kanban 的 SQLite 存储由
    # #41071 处理——为避免重叠，未纳入本次改动。）
    _ROOT_CREDENTIAL_DIRS = (
        "pairing",
    )
    for hermes_root in (_HERMES_HOME, _HERMES_ROOT):
        for rel in _ROOT_CREDENTIAL_FILES:
            denied.append(hermes_root / rel)
        for rel in _ROOT_CREDENTIAL_DIRS:
            denied.append(hermes_root / rel)
    return denied


def _path_under_denied_prefix(resolved: Path) -> bool:
    """当 ``resolved`` 位于某个被 deny 的系统路径之下时返回 True。

    一个狭窄的例外：当某个被禁前缀恰好就是当前运行用户自己的 home 时，
    home 本身不被视为被禁。``/root`` 在系统路径 denylist 中，是为了让
    非 root 的网关无法投递另一个用户的 home；但在 root 运行的网关里
    ``$HOME=/root``，运维者自己的可投递物（``/root/work/proposal.docx``）
    就直接位于其下。home 内的凭据子目录（``~/.ssh``、``~/.aws``、……）
    以及 Hermes 机密（``~/.hermes/.env``、``auth.json``）是*独立的、更具体的*
    被禁路径，因此无论该例外如何都保持拦截——该例外只能放行位于当前
    运行用户 home 树中的普通文件，绝不会放行某个凭据位置或另一个用户的 home。
    """
    try:
        home = Path(os.path.expanduser("~")).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        home = None
    for denied in _media_delivery_denied_paths():
        try:
            resolved_denied = denied.expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if not (_path_is_within(resolved, resolved_denied) or resolved == resolved_denied):
            continue
        # 放行当前运行用户自己的 home 树；其凭据子目录由上面各自
        # （更具体的）denylist 条目捕获。
        if home is not None and resolved_denied == home:
            continue
        return True
    return False


def _file_is_recently_produced(resolved: Path, window_seconds: float) -> bool:
    """当文件的 mtime 距现在不超过 ``window_seconds`` 时返回 True。

    用作 session 级的信任信号：agent 几乎总是在请求发送后几秒内产出投递物，
    而指向宿主机既有文件（/etc/passwd、~/.ssh/id_rsa）的 prompt 注入路径
    的 mtime 通常以天或月计。
    """
    if window_seconds <= 0:
        return False
    try:
        mtime = resolved.stat().st_mtime
    except OSError:
        return False
    return (time.time() - mtime) <= window_seconds


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_media_delivery_path(path: str) -> Optional[str]:
    """为原生媒体投递返回一个安全的绝对文件路径，否则返回 None。

    默认模式（单用户 / 私有网关）：接受任何不在凭据 / 系统路径
    denylist（``_MEDIA_DELIVERY_DENIED_PREFIXES`` + ``~/.ssh``、``~/.aws``
    等）之下的既有普通文件。这与入站投递的对称性一致——
    Telegram/Discord/Slack 会把用户上传的任何文件交给 agent，而 agent
    也可以把任何非凭据文件交还给用户。

    严格模式（通过 ``config.yaml`` 中的 ``gateway.strict`` 或
    ``HERMES_MEDIA_DELIVERY_STRICT=1`` 开启）：文件必须位于 Hermes 管理的
    缓存下、位于运维者 allowlist 的根目录（``HERMES_MEDIA_ALLOW_DIRS``）下，
    或者在配置的 recency 窗口内新生成。适用于面向公众的 bot，在这种场景下，
    某个用户的 prompt 注入不应能把宿主机的机密外泄给同一个用户。

    符号链接会在任何归属 / denylist 校验之前被解析。
    """
    if not path:
        return None

    candidate = str(path).strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in "`\"'":
        candidate = candidate[1:-1].strip()
    candidate = candidate.lstrip("`\"'").rstrip("`\"',.;:)}]")
    if not candidate:
        return None

    try:
        expanded = Path(os.path.expanduser(candidate))
    except (OSError, RuntimeError, ValueError):
        # expanduser raises ValueError("embedded null byte") for a ~\x00 path.
        return None
    if not expanded.is_absolute():
        return None

    try:
        resolved = expanded.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None

    if not resolved.is_file():
        return None

    # 缓存 / 运维者的 allowlist 始终生效——这些被无条件信任，与模式无关。
    for root in _media_delivery_allowed_roots():
        try:
            resolved_root = root.expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if _path_is_within(resolved, resolved_root):
            return str(resolved)

    # 非严格模式（默认）：接受任何不在 denylist 中的内容。
    # denylist 仍然会拦截 /etc、/proc、~/.ssh、~/.aws，以及 Hermes 根目录下
    # 的凭据/机密存储（~/.hermes/.env、auth.json、.anthropic_oauth.json、
    # google_token.json、pairing/、……）——因此那些显而易见的 prompt 注入 /
    # 凭据外泄入口（``MEDIA:/etc/passwd``、``MEDIA:~/.ssh/id_rsa``、
    # ``MEDIA:~/.hermes/google_token.json``）仍然被拒绝。
    if not _media_delivery_strict_mode():
        if _path_under_denied_prefix(resolved):
            return None
        return str(resolved)

    # 严格模式：对于新生成文件（例如 ``pandoc -o /tmp/report.pdf`` 或
    # ``write_file("/home/user/report.pdf", ...)``），回退到基于 recency 的信任。
    # 即便“最近”生成，系统路径和凭据位置仍然被拦截——denylist 见
    # ``_MEDIA_DELIVERY_DENIED_PREFIXES``。
    window = _media_delivery_recency_seconds()
    if window > 0 and not _path_under_denied_prefix(resolved):
        if _file_is_recently_produced(resolved, window):
            return str(resolved)

    return None


# 中和控制字符以及被 str.splitlines() / 日志聚合器视为换行的 Unicode 行分隔符
# （NEL、LS、PS），这样模型输出的路径就无法伪造出第二条日志行。截断以保持记录有界。
_LOG_UNSAFE_CHARS = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")


def _log_safe_path(path: str) -> str:
    """返回适合日志输出的、单行且有长度上限的路径。"""
    return _LOG_UNSAFE_CHARS.sub("?", str(path))[:200]


SUPPORTED_DOCUMENT_TYPES = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".log": "text/plain",
    ".json": "application/json",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".zip": "application/zip",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ts": "text/plain",
    ".py": "text/plain",
    ".sh": "text/plain",
}


# ---------------------------------------------------------------------------
# 文本注入扩展名 allowlist
#
# 当文件足够小时，其内容可以安全地内联进 prompt（UTF-8 文本）。
# 这里有意做的是扩展名/MIME 门控，而不是盲目 UTF-8 解码：
# 像 PDF/zip/docx 这样的二进制格式可能以可解码的 ASCII 头部开头，
# 绝不能被内联。任何上传的文件无论是否落在该集合内，都会被缓存并呈现给
# agent——这仅控制 prompt 中是内联还是路径指针。
# ---------------------------------------------------------------------------

_TEXT_INJECT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".log",
    ".json", ".jsonl", ".ndjson", ".xml", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".env", ".properties",
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat",
    ".c", ".h", ".cpp", ".cc", ".hpp", ".cs", ".java", ".kt",
    ".go", ".rs", ".rb", ".php", ".pl", ".lua", ".r", ".jl",
    ".swift", ".m", ".scala", ".clj", ".ex", ".exs", ".erl",
    ".sql", ".graphql", ".proto", ".tf", ".hcl",
    ".dockerfile", ".makefile", ".cmake", ".gradle",
    ".rst", ".tex", ".srt", ".vtt", ".diff", ".patch",
}


# ---------------------------------------------------------------------------
# 图片文档类型
#
# 平台可能以“文档”而非原生照片附件形式投递的图片扩展名（Telegram 用户通过
# 文件选择器上传、把贴纸/截图包装成文件的客户端等）。当我们看到其中之一时，
# 会把这些字节路由到图片缓存和正常的视觉/照片处理路径，而不是当作
# 不支持的文档拒绝。
# ---------------------------------------------------------------------------

SUPPORTED_IMAGE_DOCUMENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


# ---------------------------------------------------------------------------
# 媒体投递扩展名 allowlist —— 唯一事实来源
#
# 两个把响应文本转换为原生附件的提取器都从该元组派生各自的扩展名集合：
#   * ``extract_media()``       —— 显式的 ``MEDIA:<path>`` 标签
#   * ``extract_local_files()`` —— agent 提及的裸绝对/家目录路径
#
# 历史上这两者维护着各自独立的扩展名列表。``extract_media`` 列表很窄
# （没有 .md/.json/.yaml/.xml/.html/...），而 ``extract_local_files``
# 列表很宽。再加上分发点处无条件的 ``MEDIA:\\s*\\S+`` 清理，这种不一致
# 制造了一个无声黑洞：一个 ``MEDIA:/report.md`` 标签没能通过窄列表的
# extract_media 匹配，又被宽松的清理正则从正文中剥除，接着对
# extract_local_files 不可见——文件永远不会被投递（issue #34517）。
# 保持单一列表消除了这种漂移；从同一集合构造清理正则意味着：只有当
# 标签的扩展名确实能投递时才剥除，因此未知扩展名的路径会在正文中保留，
# 而不是凭空消失。
#
# 覆盖图片（内联）、视频（受支持处内联）、音频（语音/音频）、
# 文档/电子表格/演示文稿（send_document）、归档以及渲染后的 web 输出。
# 分发划分（image vs video vs document）位于 ``gateway/run.py``。
# ---------------------------------------------------------------------------

MEDIA_DELIVERY_EXTS: Tuple[str, ...] = (
    # 图片（内联嵌入）
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg",
    # 视频（受支持处内联嵌入）
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    # 音频（受支持处以语音/音频投递）
    ".mp3", ".wav", ".ogg", ".opus", ".m4a", ".flac",
    # 文档（作为文件附件上传）
    ".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt", ".md", ".epub",
    # 电子表格 / 数据
    ".xlsx", ".xls", ".ods", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml",
    # 演示文稿
    ".pptx", ".ppt", ".odp", ".key",
    # 归档
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".apk", ".ipa",
    # Web / 渲染输出
    ".html", ".htm",
)

# 裸扩展名（无前导点）的正则 alternation 片段，例如 ``png|jpe?g|...``。
# ``jpe?g`` 把 jpg/jpeg 合并成一个分支。按长度从长到短排序，这样 alternation
# 就不会把一个较短的扩展名匹配成较长扩展名的前缀（例如 ``.tar`` 优先于
# ``.tar.gz`` 的组成部分）。
_MEDIA_EXT_ALTERNATION = "|".join(
    sorted((e.lstrip(".") for e in MEDIA_DELIVERY_EXTS), key=len, reverse=True)
)

# 锚定的 ``MEDIA:<path>`` 清理模式。与旧的宽松 ``MEDIA:\\s*\\S+`` 不同，
# 它只会剥除路径以已知可投递扩展名结尾（可选地被引号/反引号包裹）的标签。
# 带未知扩展名的 ``MEDIA:`` 标签会保留在文本中，以便下游的裸路径检测器
# （extract_local_files）仍能拾取它，而不是被静默删除。被非流式分发路径和
# 流式 consumer 共享，使二者行为一致。
# 路径锚点：``~/``（Unix 家目录相对路径）、``/``（Unix 绝对路径）、
# ``X:\\`` 或 ``X:/``（Windows 盘符绝对路径 —— #34632）。
MEDIA_TAG_CLEANUP_RE = re.compile(
    r'''[`"']?MEDIA:\s*'''
    r'''(?P<path>`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|'''
    r'''(?:~/|/|[A-Za-z]:[/\\])\S+(?:[^\S\n]+\S+)*?\.(?:''' + _MEDIA_EXT_ALTERNATION + r'''))'''
    r'''(?=[\s`"',;:)\]}]|$)[`"']?''',
    re.IGNORECASE,
)


def get_document_cache_dir() -> Path:
    """返回文档缓存目录，如不存在则创建。"""
    DOCUMENT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return DOCUMENT_CACHE_DIR


def cache_document_from_bytes(data: bytes, filename: str) -> str:
    """
    将原始文档字节保存到缓存，并返回绝对文件路径。

    缓存的文件名会保留原始可读名称，并加上唯一前缀：
    ``doc_{uuid12}_{原始文件名}``。

    Args:
        data: 原始文档字节。
        filename: 原始文件名（例如 "report.pdf"）。

    Returns:
        缓存文档文件的绝对路径字符串。

    Raises:
        ValueError: 如果净化后的路径逃逸出缓存目录。
    """
    cache_dir = get_document_cache_dir()
    # 净化：剥除目录成分、空字节和控制字符
    safe_name = Path(filename).name if filename else "document"
    safe_name = safe_name.replace("\x00", "").strip()
    if not safe_name or safe_name in {".", ".."}:
        safe_name = "document"
    cached_name = f"doc_{uuid.uuid4().hex[:12]}_{safe_name}"
    filepath = cache_dir / cached_name
    # 最终安全检查：确保路径仍在缓存目录内
    if not filepath.resolve().is_relative_to(cache_dir.resolve()):
        raise ValueError(f"Path traversal rejected: {filename!r}")
    filepath.write_bytes(data)
    return str(filepath)


def cleanup_document_cache(max_age_hours: int = 24) -> int:
    """
    删除早于 *max_age_hours* 的缓存文档。

    返回被删除的文件数量。
    """
    import time

    cache_dir = get_document_cache_dir()
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for f in cache_dir.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------------------
# 统一的媒体缓存
#
# “我拿到了来自平台的原始附件字节——缓存它们并告诉我得到了什么”的
# 单一入口。按扩展名/MIME 对照上面共享的注册表分类，路由到正确的
# cache_*_from_bytes 辅助函数，并返回一个小结果对象，调用方可以存储它
# 和/或在 transcript 中描述。被 addressed-message 路径和
# observed-group-context 路径共同使用，适用于任何平台——并非 Telegram 专用。
# ---------------------------------------------------------------------------

@dataclass
class CachedMedia:
    """缓存一个附件字节的结果。"""

    path: str                 # 绝对缓存路径，agent 可见（已做沙箱翻译）
    media_type: str           # 记录在 MessageEvent 上的 MIME 类型
    kind: str                 # "image" | "video" | "audio" | "document"
    display_name: str         # 用于 transcript 备注的可读名称

    def context_note(self) -> str:
        """指向该文件的单行 transcript 标注。"""
        return f"[{self.kind} '{self.display_name}' saved at: {self.path}]"


def _resolve_media_ext(filename: str, mime_type: str) -> str:
    """尽力从文件名推断扩展名，再回退到 MIME。"""
    if filename:
        ext = os.path.splitext(filename)[1].lower()
        if ext:
            return ext
    mime = (mime_type or "").lower()
    if not mime:
        return ""
    for table in (
        SUPPORTED_IMAGE_DOCUMENT_TYPES,
        SUPPORTED_VIDEO_TYPES,
        SUPPORTED_DOCUMENT_TYPES,
    ):
        for ext, m in table.items():
            if m == mime:
                return ext
    return ""


def cache_media_bytes(
    data: bytes,
    *,
    filename: str = "",
    mime_type: str = "",
    default_kind: Optional[str] = None,
) -> Optional[CachedMedia]:
    """对原始附件字节分类并缓存；返回一个 CachedMedia 或 None。

    ``default_kind``（"image"/"video"/"audio"/"document"）在扩展名/MIME
    不明确时影响分类——例如一个没有可用文件名的 Telegram 原生照片。任何
    非图片/视频/音频的文件都会作为文档缓存并呈现给 agent（任意类型都会得到
    ``application/octet-stream``）；只有校验失败的图片
    （``cache_image_from_bytes`` 抛出 ValueError）才会返回 None。
    """
    from tools.credential_files import to_agent_visible_cache_path

    ext = _resolve_media_ext(filename, mime_type)
    mime = (mime_type or "").lower()
    display = re.sub(r"[^\w.\- ]", "_", filename) if filename else (ext.lstrip(".") or "file")

    is_image = (
        mime.startswith("image/")
        or ext in SUPPORTED_IMAGE_DOCUMENT_TYPES
        or default_kind == "image"
    )
    is_video = mime.startswith("video/") or ext in SUPPORTED_VIDEO_TYPES or default_kind == "video"
    is_audio = mime.startswith("audio/") or default_kind == "audio"

    if is_image:
        img_ext = ext if ext in SUPPORTED_IMAGE_DOCUMENT_TYPES else ".jpg"
        try:
            path = cache_image_from_bytes(data, ext=img_ext)
        except ValueError:
            return None
        out_mime = mime if mime.startswith("image/") else SUPPORTED_IMAGE_DOCUMENT_TYPES.get(img_ext, "image/jpeg")
        return CachedMedia(to_agent_visible_cache_path(path), out_mime, "image", display)

    if is_video:
        vid_ext = ext if ext in SUPPORTED_VIDEO_TYPES else ".mp4"
        path = cache_video_from_bytes(data, ext=vid_ext)
        return CachedMedia(to_agent_visible_cache_path(path), SUPPORTED_VIDEO_TYPES.get(vid_ext, "video/mp4"), "video", display)

    if is_audio:
        aud_ext = ext if ext in {".ogg", ".mp3", ".wav", ".m4a", ".opus", ".flac"} else ".ogg"
        path = cache_audio_from_bytes(data, ext=aud_ext)
        out_mime = mime if mime.startswith("audio/") else f"audio/{aud_ext.lstrip('.')}"
        return CachedMedia(to_agent_visible_cache_path(path), out_mime, "audio", display)

    # 任何其它文件类型都被缓存并以本地路径呈现给 agent，
    # 以便用 terminal / read_file 等检查。授权与 agent 对话才是关键门槛——
    # 一旦允许用户向 agent 发消息，文件扩展名 allowlist 就不应静默丢弃其
    # 上传。已知扩展名保留其精确 MIME；其余一律标记为
    # application/octet-stream（或调用方提供的 MIME），让 agent 知道这是一个
    # 任意文件，转而使用 terminal 工具。
    fallback_name = filename or (f"document{ext}" if ext else "document.bin")
    path = cache_document_from_bytes(data, fallback_name)
    if ext in SUPPORTED_DOCUMENT_TYPES:
        out_mime = SUPPORTED_DOCUMENT_TYPES[ext]
    else:
        out_mime = mime if mime else "application/octet-stream"
    return CachedMedia(to_agent_visible_cache_path(path), out_mime, "document", display or fallback_name)


class MessageType(Enum):
    """入站消息的类型。"""
    TEXT = "text"
    LOCATION = "location"
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"
    STICKER = "sticker"
    COMMAND = "command"  # /command 风格


class ProcessingOutcome(Enum):
    """消息处理生命周期钩子的结果分类。"""

    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


@dataclass
class MessageEvent:
    """
    来自某个平台的入站消息。

    所有适配器都会产出的规范化表示。
    """
    # 消息内容
    text: str
    message_type: MessageType = MessageType.TEXT

    # 来源信息
    source: SessionSource = None

    # 原始平台数据
    raw_message: Any = None
    message_id: Optional[str] = None

    # 平台特定的更新标识符。对于 Telegram，这是 PTB Update 包装器中的
    # ``update_id``；其它平台目前忽略它。``/restart`` 用它记录触发该动作的
    # 更新，以便新网关能把 Telegram offset 推进到它之后，避免在 PTB 的
    # 优雅关闭 ACK 超时时重复处理同一条 ``/restart``
    # （gateway.log 中会出现 "Error while calling `get_updates` one more
    # time to mark all fetched updates"）。
    platform_update_id: Optional[int] = None

    # 媒体附件
    # media_urls：本地文件路径（供 vision 工具访问）
    media_urls: List[str] = field(default_factory=list)
    media_types: List[str] = field(default_factory=list)

    # 回复上下文
    reply_to_message_id: Optional[str] = None
    reply_to_text: Optional[str] = None  # 被回复消息的文本（用于上下文注入）
    reply_to_author_id: Optional[str] = None
    reply_to_author_name: Optional[str] = None
    reply_to_is_own_message: bool = False  # 当用户回复了本 bot/assistant 的消息时为 True

    # 为 topic/channel 绑定自动加载的 skill（例如 Telegram DM Topics、
    # Discord channel_skill_bindings）。单个名称或有序列表。
    auto_skill: Optional[str | list[str]] = None

    # 每个 channel 的临时系统 prompt（例如 Discord channel_prompts）。
    # 在 API 调用时应用，从不持久化到 transcript 历史中。
    channel_prompt: Optional[str] = None

    # 由历史回填恢复的 channel 上下文（例如由于 require_mention 而错过的、
    # bot 轮次之间的消息）。与 ``text`` 分开存放，这样 run.py 中的发送者前缀
    # 逻辑可以只针对触发消息处理，之后再前置这段上下文。
    channel_context: Optional[str] = None

    # 内部标志——为合成事件（例如后台进程完成通知）设置，必须绕过用户授权检查。
    internal: bool = False

    # 时间戳
    timestamp: datetime = field(default_factory=datetime.now)
    
    def is_command(self) -> bool:
        """检查这是否是一条命令消息（例如 /new、/reset）。"""
        return self.text.startswith("/")
    
    def get_command(self) -> Optional[str]:
        """如果是命令消息，则提取命令名。"""
        if not self.is_command():
            return None
        # 按空格切分取第一个词，去掉前导 /
        parts = self.text.split(maxsplit=1)
        raw = parts[0][1:].lower() if parts else None
        if raw and "@" in raw:
            raw = raw.split("@", 1)[0]
        # 拒绝文件路径：合法命令名绝不会包含 /
        if raw and "/" in raw:
            return None
        return raw
    
    def get_command_args(self) -> str:
        """获取命令之后的参数。"""
        if not self.is_command():
            return self.text
        parts = self.text.split(maxsplit=1)
        args = parts[1] if len(parts) > 1 else ""
        # iOS 会自动把 -- 改成 —（em dash），把 - 改成 –（en dash）
        args = args.replace("\u2014\u2014", "--").replace("\u2014", "--").replace("\u2013", "-")
        return args


@dataclass
class TextDebounceState:
    event: MessageEvent
    task: asyncio.Task | None
    first_ts: float
    last_ts: float


_PLAINTEXT_GATEWAY_RESTART_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?:please\s+)?restart\s+(?:the\s+)?gateway[.!?\s]*$", re.IGNORECASE),
    re.compile(r"^(?:please\s+)?restart\s+(?:the\s+)?hermes\s+gateway[.!?\s]*$", re.IGNORECASE),
    re.compile(r"^(?:please\s+)?restart\s+hermes[.!?\s]*$", re.IGNORECASE),
)


def coerce_plaintext_gateway_command(event: "MessageEvent") -> None:
    """把一小撮 DM 明文管理短语改写成斜杠命令。

    这样可以把像 ``restart gateway`` 这类高影响的运维短语排除在
    LLM/tool 路径之外——在那里它们可能从当前运行的 agent 内部触发
    自重启，导致网关卡在 ``draining`` 状态等待同一个 agent 结束。

    作用范围有意收窄：仅限 DM 文本消息，仅限精确的 restart 风格短语。
    群聊保留自然语言语义。
    """
    try:
        if event is None or event.message_type != MessageType.TEXT:
            return
        text = (event.text or "").strip()
        if not text or text.startswith("/"):
            return
        source = getattr(event, "source", None)
        if getattr(source, "chat_type", None) != "dm":
            return
        for pattern in _PLAINTEXT_GATEWAY_RESTART_PATTERNS:
            if pattern.match(text):
                event.text = "/restart"
                return
    except Exception:
        return


@dataclass
class SendResult:
    """发送一条消息的结果。"""
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    raw_response: Any = None
    # 适配器特定的元数据。影响投递语义的跨层契约必须在生产者和消费端都记录。
    # 当前已知契约：Telegram edit overflow 的分片会把
    # delivered_chunks、total_chunks、last_message_id、delivered_prefix 和
    # continuation_message_ids 写入 raw_response["partial_overflow"]，以便
    # stream consumer 能补发缺失的尾部，而不是把一个被截断的响应标记为完成。
    retryable: bool = False  # 对于瞬时连接错误为 True——base 会自动重试
    # 服务器要求的重试延迟（秒）（例如 Telegram FloodWait 的 retry_after）。
    # 存在时，_send_with_retry() 会优先采用它而非默认退避。
    retry_after: Optional[float] = None
    # 当适配器不得不把超大载荷拆分到多条平台消息时（例如 Telegram
    # edit_message 溢出的 split-and-deliver），``message_id`` 是最后一条可见
    # 消息 id（这样后续 edit 会瞄准最新的分片），而这些则是组成完整载荷的
    # 额外消息 id，按发送顺序排列。常见的单消息场景下为空 tuple。
    continuation_message_ids: tuple = ()
    # 机器可读的失败类别（仅在 ``success`` 为 False 时设置）。
    # ``error`` 仍是人类可读的细节字符串；``error_kind`` 让消费端可以
    # 确定性地分支，而不是对原始 provider 消息做子串匹配。取值为
    # :data:`SEND_ERROR_KINDS` 中的一个，或 ``None``（未设置 / 未分类）。
    # 生产者应通过 :func:`classify_send_error` 设置它。
    error_kind: Optional[str] = None


# 机器可读的发送失败类别。保持平台中立，使每个适配器都能用同一套词汇
# 填充 ``SendResult.error_kind``，并让网关能在一处统一决定某次失败是否
# 值得呈现给用户。
#
#   too_long      内容超出了平台单条消息的大小上限；适配器通常通过续发/拆分
#                 恢复，因此这是提示性信息而非硬失败。
#   bad_format    平台拒绝了消息的标记/entities（解析错误）；可行的修复是
#                 纯文本重试。
#   forbidden     bot 被屏蔽、踢出，或缺少向目标发消息的权限——bot 无法触达
#                 用户，因此无处呈现通知。
#   not_found     目标 chat/thread/message 已不存在。
#   rate_limited  平台对发送做了限流（flood control）。
#   transient     可安全重试的连接级失败。
#   unknown       分类未匹配任何已知形态。
SEND_ERROR_KINDS = frozenset(
    {
        "too_long",
        "bad_format",
        "forbidden",
        "not_found",
        "rate_limited",
        "transient",
        "unknown",
    }
)


def classify_send_error(exc: Optional[BaseException], error_text: str = "") -> str:
    """把发送异常 / 错误字符串映射到某个 :data:`SEND_ERROR_KINDS` 值。

    平台中立的实现：针对主要消息 API 所用的子串，对 ``exc``（和/或显式
    ``error_text``）的小写文本做匹配。采取保守策略——任何无法识别的都返回
    ``"unknown"``，使调用方绝不会把未分类的失败误当成良性失败。
    """
    parts = []
    if error_text:
        parts.append(error_text)
    if exc is not None:
        parts.append(str(exc))
        parts.append(exc.__class__.__name__)
    blob = " ".join(parts).lower()
    if not blob.strip():
        return "unknown"
    if "message_too_long" in blob or "too long" in blob or "message is too long" in blob:
        return "too_long"
    if (
        "can't parse entities" in blob
        or "cant parse entities" in blob
        or "can't find end" in blob
        or "unsupported start tag" in blob
        or ("entity" in blob and "parse" in blob)
        or ("bad request" in blob and "entit" in blob)
    ):
        return "bad_format"
    if (
        "forbidden" in blob
        or "bot was blocked" in blob
        or "blocked by the user" in blob
        or "user is deactivated" in blob
        or "not enough rights" in blob
        or "have no rights" in blob
        or "not a member" in blob
    ):
        return "forbidden"
    if (
        "chat not found" in blob
        or "message to edit not found" in blob
        or "message to reply not found" in blob
        or "thread not found" in blob
        or "topic_deleted" in blob
        or "message_id_invalid" in blob
    ):
        return "not_found"
    if (
        "flood" in blob
        or "too many requests" in blob
        or "retry after" in blob
        or "rate limit" in blob
    ):
        return "rate_limited"
    for pat in _RETRYABLE_ERROR_PATTERNS:
        if pat in blob:
            return "transient"
    if "connecttimeout" in blob:
        return "transient"
    return "unknown"


class EphemeralReply(str):
    """在 TTL 之后自动删除的系统通知回复。

    ``gateway/run.py`` 中的斜杠命令处理器可以返回这个包装器，而不是普通
    字符串，以请求在支持 ``delete_message`` 的平台上，回复消息在
    ``ttl_seconds`` 后被删除。

    继承 ``str`` 让该包装器对任何把处理器返回值当作文本处理的代码保持
    透明（既有测试使用 ``in`` / ``startswith`` / 相等比较；
    ``_process_message_background`` 流水线会从字符串内容中提取附件）。
    ``isinstance(r, EphemeralReply)`` 仍能把临时回复与普通字符串区分开，
    使发送路径可以安排删除。

    未覆盖 :meth:`BasePlatformAdapter.delete_message` 的平台会静默忽略
    TTL——消息照常发送并保留在原处。当 ``ttl_seconds`` 为 ``None`` 时，
    流水线会使用配置的 ``display.ephemeral_system_ttl`` 默认值。默认值为
    ``0`` 则全局禁用自动删除，保留旧行为。
    """

    ttl_seconds: Optional[int]

    def __new__(cls, text: str, ttl_seconds: Optional[int] = None):
        instance = super().__new__(cls, text)
        instance.ttl_seconds = ttl_seconds
        return instance

    @property
    def text(self) -> str:
        """返回底层文本。

        供希望显式转换为字符串的调用点使用；不过 ``str(reply)`` 以及在期望
        字符串的地方直接使用 ``reply`` 效果完全相同。
        """
        return str.__str__(self)


def merge_pending_message_event(
    pending_messages: Dict[str, MessageEvent],
    session_key: str,
    event: MessageEvent,
    *,
    merge_text: bool = False,
) -> None:
    """存储或合并某个 session 的待处理事件。

    照片连拍/相册通常以多个近乎同时到达的 PHOTO 事件出现。把这些合并进
    已排队的事件中，使下一轮能看到整组连拍。

    当启用 ``merge_text`` 时，快速的后续 TEXT 事件会被追加，而不是替换
    待处理轮次。这用于 Telegram 的突发式跟进，使多段式的用户想法不会
    被静默截断为只剩最后排队的片段。
    """
    existing = pending_messages.get(session_key)
    if existing:
        existing_is_photo = getattr(existing, "message_type", None) == MessageType.PHOTO
        incoming_is_photo = event.message_type == MessageType.PHOTO
        existing_has_media = bool(existing.media_urls)
        incoming_has_media = bool(event.media_urls)

        if existing_is_photo and incoming_is_photo:
            existing.media_urls.extend(event.media_urls)
            existing.media_types.extend(event.media_types)
            if event.text:
                existing.text = BasePlatformAdapter._merge_caption(existing.text, event.text)
            return

        if existing_has_media or incoming_has_media:
            if incoming_has_media:
                existing.media_urls.extend(event.media_urls)
                existing.media_types.extend(event.media_types)
            if event.text:
                if existing.text:
                    existing.text = BasePlatformAdapter._merge_caption(existing.text, event.text)
                else:
                    existing.text = event.text
            if existing_is_photo or incoming_is_photo:
                existing.message_type = MessageType.PHOTO
            elif (
                getattr(existing, "message_type", None) == MessageType.TEXT
                and event.message_type != MessageType.TEXT
            ):
                existing.message_type = event.message_type
            return

        if (
            merge_text
            and getattr(existing, "message_type", None) == MessageType.TEXT
            and event.message_type == MessageType.TEXT
        ):
            if event.text:
                existing.text = f"{existing.text}\n{event.text}" if existing.text else event.text
            return

    pending_messages[session_key] = event


# 表示值得重试的瞬时*连接*失败的错误子串。
# 有意排除 "timeout" / "timed out" / "readtimeout" / "writetimeout"：在
# 非幂等调用（例如 send_message）上的读/写超时意味着请求可能已到达服务器——
# 重试有重复投递的风险。"connecttimeout" 是安全的，因为连接从未建立。
# 知道某次超时可安全重试的平台应显式设置 SendResult.retryable = True。
_RETRYABLE_ERROR_PATTERNS = (
    "connecterror",
    "connectionerror",
    "connectionreset",
    "connectionrefused",
    "connecttimeout",
    "network",
    "broken pipe",
    "remotedisconnected",
    "eoferror",
)


# 消息处理器的类型。处理器可以返回普通字符串（正常回复）、一个
# ``EphemeralReply``（让回复参与自动删除），或 ``None``（当响应已投递，
# 例如通过流式）。
MessageHandler = Callable[[MessageEvent], Awaitable[Optional[Union[str, "EphemeralReply"]]]]


def resolve_channel_prompt(
    config_extra: dict,
    channel_id: str,
    parent_id: str | None = None,
) -> str | None:
    """从平台配置解析每个 channel 的临时 prompt。

    在适配器的 ``config.extra`` 字典中查找 ``channel_prompts``。
    优先精确匹配 *channel_id*；回退到 *parent_id*（适用于论坛 thread /
    子 channel 继承父 prompt 的场景）。

    返回 prompt 字符串，未匹配则返回 None。空白/纯空白的 prompt 视同不存在。
    """
    prompts = config_extra.get("channel_prompts") or {}
    if not isinstance(prompts, dict):
        return None

    for key in (channel_id, parent_id):
        if not key:
            continue
        prompt = prompts.get(key)
        if prompt is None:
            continue
        prompt = str(prompt).strip()
        if prompt:
            return prompt
    return None


def resolve_channel_skills(
    config_extra: dict,
    channel_id: str,
    parent_id: str | None = None,
) -> list[str] | None:
    """从平台配置解析某 channel/thread 自动加载的 skill。

    在适配器的 ``config.extra`` 字典中查找 ``channel_skill_bindings``。

    配置格式::

        channel_skill_bindings:
          - id: "C0123"          # Slack channel ID 或 Discord channel/forum ID
            skills: ["skill-a", "skill-b"]
          - id: "D0ABCDE"
            skill: "solo-skill"  # 也接受单个字符串

    优先精确匹配 *channel_id*；回退到 *parent_id*（适用于论坛 thread /
    Slack thread 继承父 channel 绑定的场景）。

    返回去重后的 skill 名称列表（保留顺序），未匹配则返回 None。
    """
    bindings = config_extra.get("channel_skill_bindings") or []
    if not isinstance(bindings, list) or not bindings:
        return None
    ids_to_check: set[str] = set()
    if channel_id:
        ids_to_check.add(str(channel_id))
    if parent_id:
        ids_to_check.add(str(parent_id))
    if not ids_to_check:
        return None
    for entry in bindings:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id", ""))
        if entry_id in ids_to_check:
            skills = entry.get("skills") or entry.get("skill")
            if isinstance(skills, str):
                s = skills.strip()
                return [s] if s else None
            if isinstance(skills, list) and skills:
                seen: list[str] = []
                for name in skills:
                    if not isinstance(name, str):
                        continue
                    nm = name.strip()
                    if nm and nm not in seen:
                        seen.append(nm)
                return seen or None
    return None


def _strip_media_directives(text: str) -> str:
    """剥除内部投递指令（[[audio_as_voice]]、[[as_document]]、MEDIA:<path>），
    使它们绝不会渲染为可见文本。

    仅作为兜底：应先运行 ``extract_media``。MEDIA 清理使用共享的
    ``MEDIA_TAG_CLEANUP_RE``（只移除路径以已知可投递扩展名结尾的标签；
    故意保留未知扩展名的标签，以便下游的裸路径检测器仍能拾取它，见 #34517）。
    [[...]] 是精确匹配。
    """
    if not text:
        return text
    text = text.replace("[[audio_as_voice]]", "").replace("[[as_document]]", "")
    return MEDIA_TAG_CLEANUP_RE.sub("", text)


class BasePlatformAdapter(ABC):
    """
    平台适配器的基类。

    子类为以下方面实现平台特定逻辑：
    - 连接与认证
    - 接收消息
    - 发送消息/响应
    - 处理媒体
    """

    # 本平台是否渲染三反引号的 fenced 代码块（即 ``format_message`` 把
    # markdown fence 翻译/保留为真正的代码块）。这是用于 markdown 感知呈现
    # 选择的能力标志。默认 False（纯文本平台）；markdown 渲染型适配器设为 True。
    # tool-progress 用它把一条 terminal 命令渲染为裸 fenced 代码块
    # （不带语言标签——Slack mrkdwn 会把标签当作字面首行代码打印）。
    # 纯文本平台回退到短的截断预览（见 gateway/run.py 的 progress_callback）。
    supports_code_blocks: bool = False

    # 本适配器能否在一轮结束*之后*把 ASYNC 通知投递回 agent——即唤醒一个
    # 新轮次以呈现后台进程完成（terminal 的 notify_on_complete /
    # watch_patterns）或一个分离的 subagent 结果（delegate_task background=True）。
    #
    # 对于持有持久出站通道的适配器（Telegram、Discord、Slack、……——它们有
    # 真正的 ``send()``，且网关运行 watcher/drain 循环）为 True。对于无状态
    # request/response 适配器（API server）为 False：每个路由在轮次结束时都
    # 关闭自己的通道，因此无处推送稍后的完成事件。网关在 session 绑定时把它
    # 传播进 ``HERMES_SESSION_ASYNC_DELIVERY`` contextvar；工具通过
    # ``async_delivery_supported()`` 读取它，并拒绝做出无法兑现的投递承诺。
    # 一个新的无状态适配器只需把它设为 False 即可默认保持正确。
    supports_async_delivery: bool = True

    # 本适配器的 ``send()`` 是否通过 ``truncate_message()`` 把长内容拆分成
    # 多条消息。为 True 时，投递路由器（gateway/delivery.py）跳过网关层
    # 截断，让适配器原生分块——在支持多消息投递的平台（Discord、Telegram、……）
    # 上保留完整输出。默认 False（保守）；经核实会在 ``send()`` 中分块的适配器
    # 设为 True。
    splits_long_messages: bool = False

    # 用户在本平台上始终可以“键入”以触达 Hermes 命令的命令前缀。默认 "/"
    # （多数平台把 "/approve" 等当作普通消息文本投递）。对于键入前导 "/" 会被
    # 客户端拦截或限制的平台（Slack 在 thread 内屏蔽原生斜杠命令；Matrix 客户端
    # 把 "/" 留作客户端本地命令），其适配器会内置一个 "!" 别名改写，并把此项设为
    # "!"，这样面向用户的指引文本（"Reply `!approve` ..."）就告诉用户那种在各处
    # 都真正可用的形式。这是能力标志——共享 prompt 构建器通过
    # getattr(adapter, "typed_command_prefix", "/") 读取它；调用点无需按平台分支。
    typed_command_prefix: str = "/"

    def __init__(self, config: PlatformConfig, platform: Platform):
        self.config = config
        self.platform = platform
        self._message_handler: Optional[MessageHandler] = None
        # 可选的钩子（例如 Telegram DM topic 恢复），在 session keying 之前
        # 改写 ``event.source.thread_id``。返回修正后的 thread_id，或返回
        # None 以保持 source 不变。
        self._topic_recovery_fn: Optional[Callable[[Any], Optional[str]]] = None
        self._running = False
        self._fatal_error_code: Optional[str] = None
        self._fatal_error_message: Optional[str] = None
        self._fatal_error_retryable = True
        self._fatal_error_handler: Optional[Callable[["BasePlatformAdapter"], Awaitable[None] | None]] = None
        
        # 为支持中断，按 session 跟踪活动的消息处理器。
        # _active_sessions 存放每个 session 的中断 Event；_session_tasks 把
        # session 映射到当前正在处理它的具体 Task，使终止 session 的命令
        # （/stop、/new、/reset）能取消正确的任务并确定性地释放适配器级守卫。
        # 没有这个 owner-task 映射的话，旧任务的 finally 块可能删掉新任务的
        # 守卫，留下陈旧的 busy 状态。
        self._active_sessions: Dict[str, asyncio.Event] = {}
        self._pending_messages: Dict[str, MessageEvent] = {}
        self._session_tasks: Dict[str, asyncio.Task] = {}
        # 旧版 busy_text_mode 环境变量；未设置时，runner 会在构造之后
        # （gateway/run.py）把解析后的值（由 busy_input_mode 驱动）同步到适配器。
        # 默认为 "interrupt"，这样一次游离的 pre-sync 读取会匹配单旋钮默认值，
        # 而不是静默排队。
        self._busy_text_mode: str = (
            os.environ.get("HERMES_GATEWAY_BUSY_TEXT_MODE", "interrupt").strip().lower()
            or "interrupt"
        )
        self._busy_text_debounce_seconds: float = _float_env(
            "HERMES_GATEWAY_BUSY_TEXT_DEBOUNCE_SECONDS", 0.35
        )
        self._busy_text_hard_cap_seconds: float = _float_env(
            "HERMES_GATEWAY_BUSY_TEXT_HARD_CAP_SECONDS", 1.0
        )
        self._text_debounce: dict[str, TextDebounceState] = {}
        # 由 handle_message() 派生的后台消息处理任务。
        # 网关关停时会取消它们，使旧的网关实例在 --replace 或手动重启之后
        # 不会继续处理某个任务。
        self._background_tasks: set[asyncio.Task] = set()
        # 在主响应投递完成后触发的一次性回调。
        # 以 session_key 为键。值既可以是裸回调（旧版），也可以是
        # ``(generation, callback)`` 元组，使 GatewayRunner 能让延迟投递具备
        # generation 感知，避免陈旧的运行清空同一 session 上由更新运行注册的回调。
        self._post_delivery_callbacks: Dict[str, Any] = {}
        self._expected_cancelled_tasks: set[asyncio.Task] = set()
        self._busy_session_handler: Optional[Callable[[MessageEvent, str], Awaitable[bool]]] = None
        # 语音输入时自动 TTS：``_auto_tts_default`` 是全局默认值
        # （config.yaml 中的 ``voice.auto_tts``，由 GatewayRunner 在连接时推入）。
        # 每个 chat 的覆盖存放在两个集合中，从 ``_voice_mode`` 填充：
        #   - ``_auto_tts_enabled_chats``：通过 ``/voice on`` 或 ``/voice tts``
        #     显式开启的 chat（mode 为 ``voice_only`` 或 ``all``）。即便全局
        #     默认为 False 也会触发。
        #   - ``_auto_tts_disabled_chats``：通过 ``/voice off`` 显式关闭的 chat
        #     （mode 为 ``off``）。即便全局默认为 True 也会抑制自动 TTS。
        # _process_message() 中的门控为：
        #   当 chat 在 _auto_tts_enabled_chats 中时触发
        #     或（_auto_tts_default 为真且 chat 不在 _auto_tts_disabled_chats 中）
        self._auto_tts_default: bool = False
        self._auto_tts_enabled_chats: set = set()
        self._auto_tts_disabled_chats: set = set()
        # 暂停了 typing 指示器的 chat（例如在等待审批期间）。
        # 当 chat_id 在此集合中时，_keep_typing 会跳过 send_typing。
        self._typing_paused: set = set()

    @property
    def message_len_fn(self) -> Callable[[str], int]:
        """返回本平台上用于度量消息大小的长度函数。

        对于平台计数字符方式与 Python ``len`` 不同的适配器（例如 Telegram
        按 UTF-16 码元计数），需在子类中覆盖。
        """
        return len

    @property
    def enforces_own_access_policy(self) -> bool:
        """本适配器是否在分发之前对入站访问做门控。

        某些适配器（WeCom、Weixin、Yuanbao、QQBot、WhatsApp）实现了一个
        有文档记载、由配置驱动的访问界面——``PlatformConfig.extra`` 中的
        ``dm_policy`` / ``group_policy`` / ``allow_from`` /
        ``group_allow_from``——并在 intake 处强制执行：消息在适配器内部被丢弃，
        除非已通过该策略，否则绝不会到达网关。

        网关基于 env 的 allowlist 检查在适配器*之后*运行。当没有配置 env
        allowlist 时，网关会参考此标志，以便兑现仅配置式的
        ``dm_policy: allowlist`` / ``allow_from``（适配器已强制执行），而不是
        对其二次拒绝。关键在于，该标志本身并不等同于“已授权”：这些适配器把
        ``dm_policy`` / ``group_policy`` 默认设为 ``"open"``，会转发每个发送者，
        因此网关只有在适配器对该 chat 类型的有效策略是真正的
        ``"allowlist"`` 限制时才信任它——绝不会信任 ``"open"``（那将是
        SECURITY.md §2.6 所禁止的网络暴露 fail-open）。开放访问仍需要显式的
        ``{PLATFORM}_ALLOW_ALL_USERS`` / ``GATEWAY_ALLOW_ALL_USERS`` opt-in。

        拥有自身访问策略的适配器把它覆盖为返回 ``True``。把访问控制委托给
        网关的适配器保持 ``False``（默认）。
        """
        return False

    @property
    def authorization_is_upstream(self) -> bool:
        """本适配器上的入站是否已在 UPSTREAM 完成授权。

        与 ``enforces_own_access_policy`` 不同：那个标志描述的是一个执行
        本地、配置驱动访问界面（``dm_policy: allowlist`` / ``allow_from``）
        的适配器，网关可以镜像它。本标志描述的是其授权由一个受信任的
        UPSTREAM 在已认证的 transport 上完成的适配器——没有本地策略可供
        参考，env allowlist（``{PLATFORM}_ALLOWED_USERS``）也不适用，因为
        发送者身份不是运维者在此配置的平台账号。

        relay 适配器是唯一的使用者：它通过按实例认证的 WebSocket 接在
        Team Gateway connector 之前，且 connector 在投递*之前*执行仅 owner 的
        author-binding 解析——一条消息之所以能到达本网关，是因为 connector 把它
        解析为*本*实例绑定的用户（``user_instance_binding``）。author id 是从
        connector 观察到的事件中读取的，从不由网关断言。因此一条入站 relay
        事件携带的授权决定已由受信任、已认证的 upstream 做出；对其默认拒绝
        （无 env allowlist ⇒ 拒绝）是不正确的。

        这并不是 fail-open：这是把授权*委托*给一个认证了 transport
        （relay WS secret）并强制了仅 owner 绑定的可信 upstream，与授权
        *缺失*截然不同。它只对显式把它覆盖为 ``True`` 的适配器生效；每个
        网络暴露的直连适配器都保持 ``False``，env-allowlist 的默认拒绝
        继续原样适用。
        """
        return False

    def supports_draft_streaming(
        self,
        chat_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """本适配器是否支持原生的流式 draft 更新。

        Telegram Bot API 9.5 引入了 ``sendMessageDraft``，当 bot 用同一个
        ``draft_id`` 和不断增长的文本反复调用它时，会渲染出动画式流式预览。
        实现了 ``send_draft`` 的适配器，应在平台支持的 chat 类型上返回 True
        （Telegram 把 draft 限制在私聊 DM 中）。

        默认实现返回 False。当此处返回 False 或 ``send_draft`` 抛出异常时，
        stream consumer 回退到基于 edit 的路径（``send`` + ``edit_message``）。
        """
        return False

    def prefers_fresh_final_streaming(
        self,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """stream consumer 是否应通过发送一条*全新的*最终消息（并删除预览）
        来终结流式回复，而不是对预览做 final-edit。

        某些适配器能发送比其当前 edit 实现所支持的更丰富的最终消息。
        Telegram 是驱动案例：Hermes 通过 ``sendRichMessage`` 发送最终回复，
        但仍通过其既有的 MarkdownV2 edit 路径终结流式预览，直到 Bot API 10.1
        的 ``rich_message`` edit 参数被直接接入。此类适配器覆盖此方法，
        请求 consumer 把已完成答案作为新的 rich 消息重新投递，并尽力删除
        陈旧预览，使最终渲染与 rich 发送路径一致。

        默认实现返回 False——遗留平台保持原地的 edit 终结路径。
        """
        return False

    def streaming_overflow_limit(self) -> Optional[int]:
        """当适配器能投递比其旧版单条消息上限更大的消息时，stream consumer
        在拆分前可累计的最大单条消息长度（以本适配器 ``message_len_fn`` 的
        单位计）。

        Telegram Bot API 10.1 的 Rich Messages 在单条 ``sendRichMessage`` /
        ``sendRichMessageDraft`` 中最多接受 32,768 字符，远高于 4,096 的
        MarkdownV2 上限。具备此类更丰富 send/draft 路径的适配器覆盖此方法，
        使 consumer 不会把一条本可放入单条 rich 消息的回复拆碎；实时 edit
        预览仍受平台 edit 上限约束，但最终回复（以及 DM draft 预览）会完整投递。

        返回 ``None``（默认）表示使用 ``MAX_MESSAGE_LENGTH``。
        """
        return None

    async def send_draft(
        self,
        chat_id: str,
        draft_id: int,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """发送或更新一个动画式流式 draft 预览。

        在单次响应内的连续调用中复用同一个 ``draft_id``（任意非零 int），
        使平台对预览做动画而不是重新创建它。不同的响应在同一 chat 内必须
        使用不同的 ``draft_id`` 值，避免在之前的气泡上做动画。

        draft 没有 message_id，无法通过常规消息 API 编辑、回复或删除。
        当响应完成时，调用方把最终答案作为常规 ``send`` 投递，draft 预览会在
        客户端自然清除。

        默认实现抛出 NotImplementedError；同样从 :meth:`supports_draft_streaming`
        返回 True 的适配器必须覆盖此方法。
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement send_draft"
        )

    # ── 结构化 stream-event 渲染 ────────────────────────────────
    #
    # 这些方法让适配器决定*如何*呈现每个结构化流式事件（见
    # gateway/stream_events.py）。默认实现原样复刻历史行为：assistant 的
    # text/commentary/segment 事件委托给 stream consumer，tool 事件则渲染
    # 网关一直产出的同款 "emoji tool_name: preview" 外观。适配器可覆盖这些
    # 方法以更贴合自身平台（例如 Telegram 把 MarkdownV2 ```bash``` 块作为
    # draft 流式输出；iMessage 吞掉它无法格式化的 tool 外观）。
    #
    # 契约仅限呈现：此处渲染的任何内容都不会持久化到对话历史。历史归 agent
    # 所有；适配器选择"吞掉"的内容绝不能改变 agent 存储的字节。

    def render_message_event(self, event: Any, sink: Any) -> None:
        """把 MessageChunk / MessageStop / Commentary 渲染到 sink 上。

        默认：映射到 stream consumer 既有的原语上，1:1 保留今日行为。
        ``sink`` 是一个 GatewayStreamConsumer。
        """
        from gateway.stream_events import MessageChunk, MessageStop, Commentary

        if isinstance(event, MessageChunk):
            if event.text:
                sink.on_delta(event.text)
        elif isinstance(event, MessageStop):
            # 中途的 stop（text → tool → text）是一个分段断点；
            # 终止 stop 由网关通过 finish() 发出信号，而不是这里，
            # 因此我们只在非最终 stop 时断开分段。
            if not event.final:
                sink.on_segment_break()
        elif isinstance(event, Commentary):
            if event.text:
                sink.on_commentary(event.text)

    def format_tool_event(self, event: Any, *, mode: str = "all",
                          preview_max_len: int = 40) -> Optional[str]:
        """返回 ToolCallChunk 渲染后的外观，或返回 None 以吞掉它。

        复刻网关历史的 tool-progress 格式：一个工具 emoji、工具名，以及一段
        简短的参数预览（或在 ``verbose`` 模式下的完整 args 字典）。无法渲染
        tool 外观的适配器（不支持消息编辑、仅纯文本）应覆盖为返回 None，使
        事件被丢弃，而不是刷出多条单独气泡。

        ``mode`` 是解析后的 tool-progress 模式（"all" / "new" / "verbose"）；
        ``preview_max_len`` 对应 ``tool_preview_length`` 配置（verbose 模式下
        0 表示"无上限"）。
        """
        from gateway.stream_events import ToolCallChunk
        if not isinstance(event, ToolCallChunk):
            return None

        from agent.display import get_tool_emoji
        emoji = get_tool_emoji(event.tool_name, default="⚙️")

        if mode == "verbose":
            if event.args:
                import json
                args_str = json.dumps(event.args, ensure_ascii=False, default=str)
                if preview_max_len > 0 and len(args_str) > preview_max_len:
                    args_str = args_str[:preview_max_len - 3] + "..."
                return f"{emoji} {event.tool_name}({list(event.args.keys())})\n{args_str}"
            if event.preview:
                return f"{emoji} {event.tool_name}: \"{event.preview}\""
            return f"{emoji} {event.tool_name}..."

        # "all" / "new"：简短预览，有上限（默认 40，以保持网关 progress 气泡
        # 紧凑——它们会作为永久消息保留）。
        preview = event.preview
        if preview:
            cap = preview_max_len if preview_max_len > 0 else 40
            if len(preview) > cap:
                preview = preview[:cap - 3] + "..."
            return f"{emoji} {event.tool_name}: \"{preview}\""
        return f"{emoji} {event.tool_name}..."

    @property
    def has_fatal_error(self) -> bool:
        return self._fatal_error_message is not None

    @property
    def fatal_error_message(self) -> Optional[str]:
        return self._fatal_error_message

    @property
    def fatal_error_code(self) -> Optional[str]:
        return self._fatal_error_code

    @property
    def fatal_error_retryable(self) -> bool:
        return self._fatal_error_retryable

    def _should_auto_tts_for_chat(self, chat_id: str) -> bool:
        """语音输入时的自动 TTS 是否应对 ``chat_id`` 触发。

        决策层次（Issue #16007）：
          1. 显式 ``/voice on`` 或 ``/voice tts`` → 始终触发（即便
             ``voice.auto_tts`` 为 False）。
          2. 显式 ``/voice off`` → 从不触发。
          3. 回退到全局 ``voice.auto_tts`` 配置默认值。
        """
        if chat_id in self._auto_tts_enabled_chats:
            return True
        if chat_id in self._auto_tts_disabled_chats:
            return False
        return bool(self._auto_tts_default)

    def set_fatal_error_handler(self, handler: Callable[["BasePlatformAdapter"], Awaitable[None] | None]) -> None:
        self._fatal_error_handler = handler

    def _mark_connected(self) -> None:
        self._running = True
        self._fatal_error_code = None
        self._fatal_error_message = None
        self._fatal_error_retryable = True
        self._write_runtime_status_safe("connected", platform_state="connected", error_code=None, error_message=None)

    def _mark_disconnected(self) -> None:
        self._running = False
        if self.has_fatal_error:
            return
        self._write_runtime_status_safe("disconnected", platform_state="disconnected", error_code=None, error_message=None)

    def _set_fatal_error(self, code: str, message: str, *, retryable: bool) -> None:
        self._running = False
        self._fatal_error_code = code
        self._fatal_error_message = message
        self._fatal_error_retryable = retryable
        self._write_runtime_status_safe("fatal", platform_state="fatal", error_code=code, error_message=message)

    def _write_runtime_status_safe(self, context: str, **kwargs) -> None:
        """写入运行时状态；每个 context 的首次失败以 warning 记录，其余以 debug 记录。

        状态写入可能因权限、ENOSPC、status 目录缺失等原因失败。一个持续失败的
        status 目录过去是静默的（``except: pass``）。每次失败都记日志会在重连
        循环里刷屏，因此本方法按 (platform, context) 把首次失败以 warning 级别
        呈现，后续失败降级到 debug。
        """
        try:
            from gateway.status import write_runtime_status
            write_runtime_status(platform=self.platform.value, **kwargs)
        except Exception as exc:
            # 使用 getattr，这样跳过 __init__ 的 object.__new__(...) 测试桩
            # 不会在属性访问时崩溃。
            logged = getattr(self, "_status_write_logged", None)
            if logged is None:
                logged = set()
                try:
                    self._status_write_logged = logged
                except Exception:
                    pass
            key = (self.platform.value, context)
            if key not in logged:
                logger.warning(
                    "Failed to write runtime status (%s) for %s: %s (further failures at debug level)",
                    context, self.platform.value, exc,
                )
                logged.add(key)
            else:
                logger.debug("Failed to write runtime status (%s) for %s: %s", context, self.platform.value, exc)

    async def _notify_fatal_error(self) -> None:
        handler = self._fatal_error_handler
        if not handler:
            return
        result = handler(self)
        if asyncio.iscoroutine(result):
            await result

    def _acquire_platform_lock(self, scope: str, identity: str, resource_desc: str) -> bool:
        """为本适配器获取一个带作用域的锁。成功返回 True。"""
        from gateway.status import acquire_scoped_lock
        self._platform_lock_scope = scope
        self._platform_lock_identity = identity
        acquired, existing = acquire_scoped_lock(
            scope, identity, metadata={'platform': self.platform.value}
        )
        if acquired:
            return True
        owner_pid = existing.get('pid') if isinstance(existing, dict) else None
        message = (
            f'{resource_desc} already in use'
            + (f' (PID {owner_pid})' if owner_pid else '')
            + '. Stop the other gateway first.'
        )
        logger.error('[%s] %s', self.name, message)
        self._set_fatal_error(f'{scope}_lock', message, retryable=False)
        return False

    def _release_platform_lock(self) -> None:
        """释放由 _acquire_platform_lock 获取的带作用域锁。"""
        identity = getattr(self, '_platform_lock_identity', None)
        if not identity:
            return
        from gateway.status import release_scoped_lock
        release_scoped_lock(self._platform_lock_scope, identity)
        self._platform_lock_identity = None

    @property
    def name(self) -> str:
        """本适配器的人类可读名称。"""
        return self.platform.value.title()

    @property
    def is_connected(self) -> bool:
        """检查适配器当前是否已连接。"""
        return self._running

    def set_message_handler(self, handler: MessageHandler) -> None:
        """
        设置入站消息的处理器。

        处理器接收一个 MessageEvent，并应返回一个可选的响应字符串。
        """
        self._message_handler = handler

    def set_topic_recovery_fn(
        self,
        fn: Optional[Callable[[Any], Optional[str]]],
    ) -> None:
        """安装一个 thread_id 恢复钩子（Telegram DM topic 模式）。

        该钩子在 session keying 之前以 ``event.source`` 调用；非 None 的返回值
        会替换 ``source.thread_id``。传入 ``None`` 可清除钩子。
        """
        # 防御那些在测试中通过 ``object.__new__`` 初始化、从不运行
        # ``BasePlatformAdapter.__init__`` 的子类。
        self._topic_recovery_fn = fn  # type: ignore[attr-defined]

    def _apply_topic_recovery(self, event: MessageEvent) -> None:
        """当钩子返回 thread_id 时，原地改写 ``event.source.thread_id``。"""
        recover = getattr(self, "_topic_recovery_fn", None)
        if recover is None:
            return
        source = getattr(event, "source", None)
        if source is None:
            return
        try:
            recovered = recover(source)
        except Exception:
            logger.debug("topic recovery hook failed", exc_info=True)
            return
        if recovered is None or str(recovered) == str(source.thread_id or ""):
            return
        try:
            event.source = dataclasses.replace(source, thread_id=str(recovered))
        except Exception:
            logger.debug("topic recovery rewrite failed", exc_info=True)

    def set_busy_session_handler(self, handler: Optional[Callable[[MessageEvent, str], Awaitable[bool]]]) -> None:
        """为活动 session 期间到达的消息设置一个可选处理器。"""
        self._busy_session_handler = handler

    def set_session_store(self, session_store: Any) -> None:
        """
        设置用于检查活动 session 的 session 存储。

        供那些在处理消息之前需要检查某个 thread/会话是否有活动 session 的
        适配器使用（例如 Slack thread 回复但未显式 @ 的情况）。
        """
        self._session_store = session_store

    @abstractmethod
    async def connect(self) -> bool:
        """
        连接到平台并开始接收消息。

        连接成功则返回 True。
        """
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """从平台断开连接。"""
        pass

    @abstractmethod
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> SendResult:
        """
        向某个 chat 发送一条消息。

        Args:
            chat_id: 要发送到的 chat/channel ID
            content: 消息内容（可以是 markdown）
            reply_to: 可选，要回复的消息 ID
            metadata: 额外的平台特定选项

        Returns:
            包含成功状态和消息 ID 的 SendResult
        """
        pass

    # 默认：适配器把 edit_message 上的 ``finalize=True`` 当作 no-op，并乐意让
    # stream consumer 跳过冗余的 final edit。*要求*显式 finalize 调用来收尾
    # 消息生命周期的子类（例如 rich card / AI assistant 呈现面，如钉钉 AI Cards）
    # 把它覆盖为 True（类属性或 property），让 stream consumer 知道不要短路。
    REQUIRES_EDIT_FINALIZE: bool = False

    async def create_handoff_thread(
        self,
        parent_chat_id: str,
        name: str,
    ) -> Optional[str]:
        """为 session handoff 在 ``parent_chat_id`` 之下创建一个新 thread。

        在把 CLI session 转移到支持 thread 的平台时，由网关的 handoff watcher
        使用——新 thread 把被交接的对话与 home channel 中既有 chat 隔离开，
        为用户提供干净的、按 handoff 区分的 scrollback。

        成功时返回新的 thread/topic id（字符串），若平台不支持 threading 或
        尝试失败（权限、topics-mode 关闭等）则返回 ``None``。返回 ``None`` 时，
        watcher 回退到直接使用 ``parent_chat_id``。

        默认实现返回 ``None``——支持 thread 的适配器覆盖此方法。参见：
          - Telegram：群组内的 forum topics、bot API 9.4+ 的 DM topics
          - Discord：text-channel threads（1440 分钟自动归档）
          - Slack：seed-message thread 锚定
        """
        return None


    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """
        编辑一条已发送的消息。可选——不支持编辑的平台返回 success=False，
        调用方回退到发送一条新消息。

        ``finalize`` 表示这是流式序列中的最后一次 edit。多数平台
        （Telegram、Slack、Discord、Matrix 等）把它当作 no-op，因为它们的
        edit API 没有消息生命周期状态的概念——一次 edit 就是一次 edit。
        对于那些用独立的"in progress"状态渲染流式更新、并要求显式收尾的
        平台（例如 rich card / AI assistant 呈现面，如钉钉 AI Cards），用它
        来 finalize 消息并把 UI 从流式指示器中切出——这些平台还应设置
        ``REQUIRES_EDIT_FINALIZE = True``，使调用方即使在内容未变时也路由一次
        final edit。调用方应在流式响应的最后一次 edit 上设置 ``finalize=True``
        （通常在 stream consumer 的 ``got_done`` 触发时），中间 edit 保持
        ``False``。
        """
        return SendResult(success=False, error="Not supported")

    async def delete_message(
        self,
        chat_id: str,
        message_id: str,
    ) -> bool:
        """
        删除一条已发送的消息。可选——不支持删除的平台返回 ``False``，
        调用方回退到保留该消息。

        供 stream consumer 的 fresh-final 清理路径使用（见
        openclaw/openclaw#72038），在把完成回复作为新消息发送之后，移除
        长寿命的预览消息，使平台的可见时间戳反映完成时间。

        成功删除返回 ``True``，否则返回 ``False``。对于具备删除 API 的平台
        （例如 Telegram 的 ``deleteMessage``），子类应覆盖此方法。
        """
        return False

    def _get_ephemeral_system_ttl_default(self) -> int:
        """从配置读取 ``display.ephemeral_system_ttl``。

        返回当 :class:`EphemeralReply` 未显式指定 TTL 时使用的 TTL（秒）。
        ``0``（默认）禁用自动删除。配置不可读时也不是致命错误。
        """
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            return 0
        try:
            cfg = _load_config()
        except Exception:
            return 0
        display = cfg.get("display", {}) if isinstance(cfg, dict) else {}
        if not isinstance(display, dict):
            return 0
        raw = display.get("ephemeral_system_ttl", 0)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    def _schedule_ephemeral_delete(
        self,
        chat_id: str,
        message_id: str,
        ttl_seconds: int,
    ) -> None:
        """派生一个分离的任务，在 ``ttl_seconds`` 后删除 ``message_id``。

        尽力而为——失败（网关重启、权限拒绝、消息对 Telegram 的 48 小时窗口
        而言过旧）会在 debug 级别被吞掉。不会阻塞调用方。
        """

        async def _run_delete() -> None:
            try:
                await asyncio.sleep(max(1, int(ttl_seconds)))
                await self.delete_message(chat_id=chat_id, message_id=message_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(
                    "[%s] Ephemeral delete failed for %s/%s: %s",
                    self.name, chat_id, message_id, e,
                )

        coro = _run_delete()
        try:
            asyncio.create_task(coro)
        except RuntimeError:
            # No running loop (e.g. unit tests that never reach the async
            # path).  Close the coroutine cleanly so Python doesn't warn
            # about it never being awaited, then drop silently.
            coro.close()

    async def send_slash_confirm(
        self,
        chat_id: str,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """发送一个三选项的斜杠命令确认提示。

        由网关的通用 slash-confirm 原语使用（见
        ``GatewayRunner._request_slash_confirm``），用于那些具有非破坏性但代价
        昂贵、用户应显式确认的副作用命令——当前调用者是 ``/reload-mcp``，
        它会使 provider 的 prompt 缓存失效。

        支持 inline-button 的平台（Telegram、Discord、Slack、Matrix、Feishu）
        应覆盖此方法以渲染三个按钮：Approve Once / Always Approve / Cancel。
        按钮回调必须通过调用
        ``GatewayRunner._resolve_slash_confirm(confirm_id, choice)`` 路由回网关，
        其中 ``choice`` 为 ``"once"`` / ``"always"`` / ``"cancel"``。

        没有按钮 UI 的平台保持默认实现，落入网关的文本回退（它把 ``message``
        作为纯文本发送，并拦截下一条 ``/approve`` / ``/always`` / ``/cancel``
        回复）。

        ``confirm_id`` 是由网关生成的短字符串；适配器把它与路由回调所需的
        任何平台特定状态一起存储（例如 Telegram 的 ``_approval_state`` 字典）。
        """
        return SendResult(success=False, error="Not supported")

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[list],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """向用户发送一个 clarify 提示。

        两种渲染模式：

          * **多选**（``choices`` 为非空列表）——覆盖此方法的适配器应渲染
            inline 按钮（每个选项一个，外加一个最后的 "Other" / 自由文本选项）。
            按钮回调必须通过
            ``tools.clarify_gateway.resolve_gateway_clarify(clarify_id, response)``
            以所选字符串来 resolve。选择 "Other" 按钮会调用
            ``mark_awaiting_text(clarify_id)``，使 session 中的下一条消息被
            捕获为响应。

          * **开放式**（``choices`` 为 None 或空）——把问题渲染为纯文本消息；
            session 中的下一条用户消息会被网关的文本拦截捕获，并自动 resolve
            该 clarify（见 ``GatewayRunner._maybe_intercept_clarify_text``）。

        默认实现回退到带编号的文本列表，这在任何平台上都可用——用户以数字
        （"2"）或字面选项文本回复，网关拦截并 resolve。对于文本回退路径，
        默认实现会调用 ``mark_awaiting_text()``，使网关的文本拦截
        （:meth:`GatewayRunner._maybe_intercept_clarify_text`）捕获用户回复，
        而不是超时。
        具备原生按钮 UI 的适配器（Telegram、Discord）应覆盖此方法以获得
        更丰富的 UX。
        """
        if choices:
            lines = [f"❓ {question}", ""]
            for i, choice in enumerate(choices, start=1):
                lines.append(f"  {i}. {choice}")
            lines.append("")
            lines.append("Reply with the number, the option text, or your own answer.")
            text = "\n".join(lines)
            # 文本回退：启用文本捕获，使网关拦截拾取用户键入的回复
            # （例如 "2" 或选项文本）。
            from tools.clarify_gateway import mark_awaiting_text
            mark_awaiting_text(clarify_id)
        else:
            text = f"❓ {question}"
        return await self.send(
            chat_id=chat_id,
            content=text,
            metadata=metadata,
        )

    async def send_private_notice(
        self,
        chat_id: str,
        user_id: Optional[str],
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """在平台支持时私密地发送一条通知。

        默认实现回退到普通 send，使调用方在各平台上可以用同一条代码路径。
        """
        return await self.send(
            chat_id=chat_id,
            content=content,
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """
        发送一个 typing 指示器。

        若平台支持，在子类中覆盖。
        metadata：可选字典，携带平台特定上下文（例如 Slack 的 thread_id）。
        """
        pass

    async def stop_typing(self, chat_id: str) -> None:
        """停止持久的 typing 指示器（如果平台使用了的话）。

        在启动后台 typing 循环的子类中覆盖。
        对于使用一次性 typing 指示器的平台，默认为 no-op。
        """
        pass

    async def send_multiple_images(
        self,
        chat_id: str,
        images: List[Tuple[str, str]],
        metadata: Optional[Dict[str, Any]] = None,
        human_delay: float = 0.0,
    ) -> None:
        """批量发送一组图片。

        接受 tuple 第一个元素为 ``http(s)://``、``file://`` URI。

        默认实现逐条发送，把动态 GIF 走 ``send_animation``，把本地文件走
        ``send_image_file``。

        在子类中覆盖以打包成单次原生 API 调用（例如 Signal 的多附件 RPC）。
        """
        from urllib.parse import unquote as _unquote

        for image_url, alt_text in images:
            if human_delay > 0:
                await asyncio.sleep(human_delay)
            try:
                logger.info(
                    "[%s] Sending image: %s (alt=%s)",
                    self.name,
                    safe_url_for_log(image_url),
                    alt_text[:30] if alt_text else "",
                )
                if image_url.startswith("file://"):
                    img_result = await self.send_image_file(
                        chat_id=chat_id,
                        image_path=_unquote(image_url[7:]),
                        caption=alt_text if alt_text else None,
                        metadata=metadata,
                    )
                elif self._is_animation_url(image_url):
                    img_result = await self.send_animation(
                        chat_id=chat_id,
                        animation_url=image_url,
                        caption=alt_text if alt_text else None,
                        metadata=metadata,
                    )
                else:
                    img_result = await self.send_image(
                        chat_id=chat_id,
                        image_url=image_url,
                        caption=alt_text if alt_text else None,
                        metadata=metadata,
                    )
                if not img_result.success:
                    logger.error("[%s] Failed to send image: %s", self.name, img_result.error)
            except Exception as img_err:
                logger.error("[%s] Error sending image: %s", self.name, img_err, exc_info=True)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """
        通过平台 API 原生地发送一张图片。

        在子类中覆盖，把图片作为真正的附件发送，而不是纯文本 URL。
        默认回退为把 URL 作为文本消息发送。
        """
        # 回退：把 URL 作为文本发送（子类为原生图片覆盖此方法）
        text = f"{caption}\n{image_url}" if caption else image_url
        return await self.send(chat_id=chat_id, content=text, reply_to=reply_to, metadata=metadata)
    
    async def send_animation(
        self,
        chat_id: str,
        animation_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """
        通过平台 API 原生地发送一个动态 GIF。

        在子类中覆盖，把 GIF 作为真正的动画发送（例如 Telegram 的
        send_animation），使其内联自动播放。默认回退到 send_image。
        """
        return await self.send_image(chat_id=chat_id, image_url=animation_url, caption=caption, reply_to=reply_to, metadata=metadata)
    
    @staticmethod
    def _is_animation_url(url: str) -> bool:
        """检查某个 URL 是否指向动态 GIF（相对于静态图片）。"""
        lower = url.lower().split('?')[0]  # 剥除 query 参数
        return lower.endswith('.gif')

    @staticmethod
    def extract_images(content: str) -> Tuple[List[Tuple[str, str]], str]:
        """
        从响应中的 markdown 和 HTML 图片标签里提取图片 URL。

        查找如下模式：
        - ![alt text](https://example.com/image.png)
        - <img src="https://example.com/image.png">
        - <img src="https://example.com/image.png"></img>

        Args:
            content: 要扫描的响应文本。

        Returns:
            一个 tuple：(url, alt_text) 对的列表，以及移除图片标签后的清洁内容。
        """
        images = []
        cleaned = content

        # 匹配 markdown 图片：![alt](url)
        md_pattern = r'!\[([^\]]*)\]\((https?://[^\s\)]+)\)'
        for match in re.finditer(md_pattern, content):
            alt_text = match.group(1)
            url = match.group(2)
            # 只提取看起来是真实图片的 URL
            if any(url.lower().endswith(ext) or ext in url.lower() for ext in
                   ['.png', '.jpg', '.jpeg', '.gif', '.webp', 'fal.media', 'fal-cdn', 'replicate.delivery']):
                images.append((url, alt_text))

        # 匹配 HTML img 标签：<img src="url"> 或 <img src="url"></img> 或 <img src="url"/>
        html_pattern = r'<img\s+src=["\']?(https?://[^\s"\'<>]+)["\']?\s*/?>\s*(?:</img>)?'
        for match in re.finditer(html_pattern, content):
            url = match.group(1)
            images.append((url, ""))

        # 仅从内容中移除已匹配到的图片标签（不是所有 markdown 图片）
        if images:
            extracted_urls = {url for url, _ in images}
            def _remove_if_extracted(match):
                url = match.group(2) if match.lastindex >= 2 else match.group(1)
                return '' if url in extracted_urls else match.group(0)
            cleaned = re.sub(md_pattern, _remove_if_extracted, cleaned)
            cleaned = re.sub(html_pattern, _remove_if_extracted, cleaned)
            # 清理残留的空行
            cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
        
        return images, cleaned
    
    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """
        通过平台 API 把音频文件作为原生语音消息发送。

        在子类中覆盖，把音频作为语音气泡（Telegram）或文件附件（Discord）
        发送。默认回退为把文件路径作为文本发送。
        """
        text = f"🔊 Audio: {audio_path}"
        if caption:
            text = f"{caption}\n{text}"
        return await self.send(chat_id=chat_id, content=text, reply_to=reply_to, metadata=metadata)

    def prepare_tts_text(self, text: str) -> str:
        """为 TTS 准备文本。覆盖此方法以过滤 tool 输出、代码等。

        默认剥除 markdown 格式并截断到 4000 字符。
        """
        return re.sub(r'[*_`#\[\]()]', '', text)[:4000].strip()

    async def play_tts(
        self,
        chat_id: str,
        audio_path: str,
        **kwargs,
    ) -> SendResult:
        """
        为语音回复播放自动 TTS 音频。

        在子类中覆盖以实现不可见播放（例如 Web UI）。
        默认回退到 send_voice（显示音频播放器）。
        """
        return await self.send_voice(chat_id=chat_id, audio_path=audio_path, **kwargs)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """
        通过平台 API 原生地发送一段视频。

        在子类中覆盖，把视频作为可内联播放的媒体发送。
        默认回退为把文件路径作为文本发送。
        """
        text = f"🎬 Video: {video_path}"
        if caption:
            text = f"{caption}\n{text}"
        return await self.send(chat_id=chat_id, content=text, reply_to=reply_to, metadata=metadata)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """
        通过平台 API 原生地发送一个文档/文件。

        在子类中覆盖，把文件作为可下载附件发送。
        默认回退为把文件路径作为文本发送。
        """
        text = f"📎 File: {file_path}"
        if caption:
            text = f"{caption}\n{text}"
        return await self.send(chat_id=chat_id, content=text, reply_to=reply_to, metadata=metadata)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """
        通过平台 API 原生地发送一个本地图片文件。

        与接受 URL 的 send_image() 不同，本方法接受本地文件路径。
        在子类中覆盖以实现原生照片附件。
        默认回退为把文件路径作为文本发送。
        """
        text = f"🖼️ Image: {image_path}"
        if caption:
            text = f"{caption}\n{text}"
        return await self.send(chat_id=chat_id, content=text, reply_to=reply_to, metadata=metadata)

    @staticmethod
    def validate_media_delivery_path(path: str) -> Optional[str]:
        """如果路径对原生附件上传是安全的，则返回解析后的路径。"""
        return validate_media_delivery_path(path)

    @staticmethod
    def filter_media_delivery_paths(media_files) -> List[Tuple[str, bool]]:
        """丢弃不安全的 MEDIA 路径，并规范化被接受的路径。"""
        safe_media: List[Tuple[str, bool]] = []
        for media_path, is_voice in media_files or []:
            raw = str(media_path)
            safe_path = validate_media_delivery_path(raw)
            if safe_path:
                safe_media.append((safe_path, bool(is_voice)))
            else:
                logger.warning("Skipping unsafe MEDIA directive path: %s", _log_safe_path(raw))
        return safe_media

    @staticmethod
    def filter_local_delivery_paths(file_paths) -> List[str]:
        """丢弃不安全的裸本地文件路径，并规范化被接受的路径。"""
        safe_paths: List[str] = []
        for file_path in file_paths or []:
            raw = str(file_path)
            safe_path = validate_media_delivery_path(raw)
            if safe_path:
                safe_paths.append(safe_path)
            else:
                logger.warning("Skipping unsafe local file path: %s", _log_safe_path(raw))
        return safe_paths


    @staticmethod
    def _mask_protected_spans(content: str) -> str:
        """把 fenced 代码块、inline code span 和 blockquote 内部的内容替换为
        空格，以防止 MEDIA: 出现误报。

        保留字符数，使正则匹配偏移量保持有效。
        跳过对 MEDIA: 标签中反引号引用路径的遮蔽（例如
        ``MEDIA:`/path/to/file.png` ``），以免破坏路径提取。
        """
        chars = list(content)
        n = len(chars)

        # 构建要遮蔽的 (start, end) 区间列表
        spans: list = []

        # fenced 代码块：```...```
        for m in re.finditer(r'```[^\n]*\n.*?```', content, re.DOTALL):
            spans.append((m.start(), m.end()))

        # inline code：`...`，但不包括 MEDIA: 标签中反引号引用的路径
        for m in re.finditer(r'`[^`\n]+`', content):
            start = m.start()
            # 检查这是否是 MEDIA: 之后的反引号引用路径
            prefix = content[max(0, start - 20):start]
            if re.search(r'MEDIA:\s*$', prefix):
                continue  # 这是一个 MEDIA 路径引用，不是 inline code
            spans.append((start, m.end()))

        # blockquote 行：行首的 >
        for m in re.finditer(r'^>.*$', content, re.MULTILINE):
            spans.append((m.start(), m.end()))

        # 应用遮蔽
        for start, end in spans:
            for i in range(start, end):
                if chars[i] != '\n':
                    chars[i] = ' '

        return ''.join(chars)


    @staticmethod
    def _mask_json_string_media(content: str) -> str:
        """把位于 JSON 字符串*值*内部的 ``MEDIA:<bare-path>`` 出现处清空，
        使它们绝不会作为真实附件投递。

        序列化的 tool 结果经常嵌入了之前回复的文本，例如::

            {"result": "MEDIA:/Users/x/.hermes/media/generated/stale.png"}

        这里的 ``MEDIA:`` 是存储文本的一部分，不是出站指令，但
        ``MEDIA_TAG_CLEANUP_RE`` 的裸路径分支仍会匹配它并重新投递一个陈旧
        文件。（回归报告 #34375。）

        判别条件很精确，因此合法标签不受影响：

        * 只考虑由 JSON 值上下文引号（``"`` 紧跟在 ``:``、``,``、``{`` 或
          ``[`` 之后）开启的 span。
        * 在这样的 span 内，只有后跟**裸**路径（``/``、``~/`` 或 ``X:\\``）
          的 ``MEDIA:`` 才会被遮蔽。``MEDIA:"..."`` 引号路径标签——一种
          extractor 支持的真实 LLM 输出格式——不是裸路径，保持原样。
        * 位于行首、散文空白之后或缩进的标签不在任何 JSON 值 span 内，从不
          受影响。

        偏移量被保留（匹配字符替换为空格，换行保留），使下游匹配位置保持有效。
        """
        if '"' not in content or "MEDIA:" not in content:
            return content
        chars = list(content)
        # JSON 值上下文字符串：一个引号前缀为 : , { 或 [（可选空白），
        # 捕获（转义感知的）字符串体直到结束引号。
        for m in re.finditer(r'(?<=[:,{\[])\s*"((?:[^"\\\n]|\\.)*)"', content):
            seg = m.group(1)
            if re.search(r'MEDIA:\s*(?:~/|/|[A-Za-z]:[/\\])', seg):
                for i in range(m.start(1), m.end(1)):
                    if chars[i] != '\n':
                        chars[i] = ' '
        return ''.join(chars)

    @staticmethod
    def extract_media(content: str) -> Tuple[List[Tuple[str, bool]], str]:
        """
        从响应文本中提取 MEDIA:<path> 标签和 [[audio_as_voice]] 指令。

        TTS 工具返回的响应形如：
            [[audio_as_voice]]
            MEDIA:/path/to/audio.ogg

        产出大体积/无损图片的 skill（例如 info-graph，渲染出的 JPG 有 1-2 MB，
        但 Telegram 的 sendPhoto 会在 1280px 下重压缩到约 200 KB）可以用
        ``[[as_document]]`` 请求通过 sendDocument 而非 sendPhoto/sendMediaGroup
        原样投递。该指令在分发点（能访问原始响应）处被检测；本方法只是把它剥除，
        使它绝不泄漏到用户可见文本中。有意不暴露逐文件粒度——当 agent 一次
        发出 ``[[as_document]]``，同一响应中的每个图片路径都会作为文档投递，
        与 ``[[audio_as_voice]]`` 的全有或全无作用域一致。

        Args:
            content: 要扫描的响应文本。

        Returns:
            一个 tuple：((path, is_voice) 对的列表，移除标签后的清洁内容)。
        """
        media = []
        cleaned = content

        # 检查 [[audio_as_voice]] 指令
        has_voice_tag = "[[audio_as_voice]]" in content
        cleaned = cleaned.replace("[[audio_as_voice]]", "")
        # 剥除 [[as_document]] 指令——调用方在原始 ``content`` 上检查它
        # （以便仍能对其作出反应）；此处只是让它不出现在用户可见的清洁文本中。
        cleaned = cleaned.replace("[[as_document]]", "")

        # 提取 MEDIA:<path> 标签，允许冒号后可选空白，以及为 LLM 格式化输出
        # 而设的引号/反引号路径。扩展名集合是共享的 MEDIA_DELIVERY_EXTS 唯一
        # 事实来源（一次构建进 MEDIA_TAG_CLEANUP_RE），因此绝不会与
        # extract_local_files 漂移。
        media_pattern = MEDIA_TAG_CLEANUP_RE
        # 在扫描前遮蔽示例/存储的 MEDIA: 路径，使它们绝不会作为真实附件投递：
        #  - 代码块 / inline code / blockquote 承载散文示例（#35695）
        #  - 序列化的 JSON 字符串值承载存储的 tool-result 文本（#34375）
        # 两个遮蔽器都保留偏移量（字符 -> 空格），因此匹配偏移量保持有效；
        # 串联使用它们会遮蔽两块保护区域的并集。
        scan_content = BasePlatformAdapter._mask_protected_spans(content)
        scan_content = BasePlatformAdapter._mask_json_string_media(scan_content)
        for match in media_pattern.finditer(scan_content):
            path = match.group("path").strip()
            if len(path) >= 2 and path[0] == path[-1] and path[0] in "`\"'":
                path = path[1:-1].strip()
            path = path.lstrip("`\"'").rstrip("`\"',.;:)}]")
            if path:
                try:
                    media.append((os.path.expanduser(path), has_voice_tag))
                except (OSError, RuntimeError, ValueError):
                    # 跳过一个精心构造的 ~\x00 路径，而不是中止提取并丢弃
                    # 响应中的其它所有附件。
                    continue

        # 从用户可见文本中移除已投递的 MEDIA 标签。对 ``cleaned`` 的等长副本
        # 做遮蔽（相同的保护区并集）以*定位*真实标签区间，然后从*未遮蔽*的
        # ``cleaned`` 中精确删除这些区间。遮蔽仅作为定位器——受保护区
        # （代码块、引用、JSON 内嵌的 MEDIA: 文本）必须在投递文本中原样保留，
        # 而不是被清成空格。遮蔽 ``cleaned``（而非 ``content``）可在
        # [[audio_as_voice]] / [[as_document]] 指令被移除后保持偏移量有效。
        if media:
            masked_cleaned = BasePlatformAdapter._mask_protected_spans(cleaned)
            masked_cleaned = BasePlatformAdapter._mask_json_string_media(masked_cleaned)
            spans = [m.span() for m in media_pattern.finditer(masked_cleaned)]
            if spans:
                chars = list(cleaned)
                for start, end in sorted(spans, reverse=True):
                    del chars[start:end]
                cleaned = "".join(chars)
                cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
        
        return media, cleaned

    @staticmethod
    def extract_local_files(content: str) -> Tuple[List[str], str]:
        """
        检测响应文本中的裸本地文件路径，用于原生投递。

        匹配绝对路径（/...）和波浪号路径（~/），以常见图片、视频、音频或
        文档扩展名结尾。对每个候选路径用 ``os.path.isfile()`` 校验，以避免
        来自 URL 或不存在路径的误报。

        扩展名列表比单纯图片/视频更宽，使 agent 可以产出任意产物（图表、PDF、
        电子表格、代码归档、CSV）并作为原生上传投递给用户，而无需显式的
        ``MEDIA:`` 标签。图片/视频扩展名仍会在平台支持处内联嵌入；文档扩展名
        走 ``send_document``。分发划分位于 ``gateway/run.py``。

        fenced 代码块（``` ... ```）和 inline code（`...`）内的路径会被忽略，
        使代码示例绝不会遭到破坏。

        Returns:
            一个 tuple：(展开后的文件路径列表，移除原始路径字符串后的清洁文本)。
        """
        _LOCAL_MEDIA_EXTS = MEDIA_DELIVERY_EXTS
        ext_part = '|'.join(e.lstrip('.') for e in _LOCAL_MEDIA_EXTS)

        # (?<![/:\w.]) 阻止在 URL（例如 https://…/img.png）
        #             和相对路径（./foo.png）内部匹配
        # (?:~/|/)    锚定到绝对或家目录相对的 Unix 路径
        # (?:[A-Za-z]:[/\\]) 锚定到 Windows 盘符路径（#34632）
        path_re = re.compile(
            r'(?<![/:\w.])(?:~/|/|[A-Za-z]:[/\\])(?:[\w.\-]+[/\\])*[\w.\-]+\.(?:' + ext_part + r')\b',
            re.IGNORECASE,
        )

        # 构建 fenced 代码块和 inline code 覆盖的区间
        code_spans: list = []
        for m in re.finditer(r'```[^\n]*\n.*?```', content, re.DOTALL):
            code_spans.append((m.start(), m.end()))
        for m in re.finditer(r'`[^`\n]+`', content):
            code_spans.append((m.start(), m.end()))

        def _in_code(pos: int) -> bool:
            return any(s <= pos < e for s, e in code_spans)

        found: list = []  # (原始匹配文本, 展开后的路径)
        for match in path_re.finditer(content):
            if _in_code(match.start()):
                continue
            raw = match.group(0)
            expanded = os.path.expanduser(raw)
            if os.path.isfile(expanded):
                found.append((raw, expanded))
            else:
                # 回复提及了一个看起来可投递、但磁盘上不存在的路径，因此从
                # 原生投递中静默丢弃。这是承诺的文件从未送达的最常见原因
                # （模型说"这是你的文件"却从未写入，或引用了错误路径）。
                # 记录日志使该缺口在 gateway.log 中可见，而不是无痕消失。
                logger.info(
                    "Skipping bare file path in reply (no file on disk): %s",
                    _log_safe_path(raw),
                )

        # 按展开后的路径去重，保留发现顺序
        seen: set = set()
        unique: list = []
        for raw, expanded in found:
            if expanded not in seen:
                seen.add(expanded)
                unique.append((raw, expanded))

        paths = [expanded for _, expanded in unique]

        cleaned = content
        if unique:
            for raw, _exp in unique:
                cleaned = cleaned.replace(raw, '')
            cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()

        return paths, cleaned

    async def _keep_typing(
        self,
        chat_id: str,
        interval: float = 2.0,
        metadata=None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """
        持续发送 typing 指示器，直到被取消。

        Telegram/Discord 的 typing 状态约 5 秒后过期，因此我们每 2 秒刷新一次，
        以便在进度消息打断它之后快速恢复。

        当 chat 处于 ``_typing_paused`` 时（例如 agent 正在等待危险命令审批），
        跳过 send_typing。这对 Slack 的 Assistant API 至关重要——在那里
        ``assistant_threads_setStatus`` 会禁用 compose 框——暂停可让用户键入
        ``/approve`` 或 ``/deny``。

        每次 ``send_typing`` 调用都受约 1.5 秒超时约束，使缓慢的网络往返不会
        拖住刷新节奏。Telegram 和 Discord 端的 typing 约 5 秒后过期；如果某次
        send_typing 耗时超过刷新间隔，气泡会熄灭并保持熄灭直到该调用返回。
        放弃慢调用让下一个 tick 按计划发出新的 send_typing——只要其中一次
        在 5 秒的平台端窗口内成功，气泡就能在 provider 停滞 / 上游 API 超时
        期间保持可见。
        """
        # 为每次 send_typing 往返设置上限，使刷新节奏不受网络健康状况制约。
        # 必须小于 ``interval``，这样慢调用会在下一个计划 tick 之前被放弃。
        _send_typing_timeout = max(0.25, min(1.5, interval - 0.25))
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    return
                if chat_id not in self._typing_paused:
                    try:
                        await asyncio.wait_for(
                            self.send_typing(chat_id, metadata=metadata),
                            timeout=_send_typing_timeout,
                        )
                    except asyncio.TimeoutError:
                        # 网络慢——放弃本次 tick，保持循环按计划进行，
                        # 以便下一次 send_typing 新鲜发出。
                        pass
                    except asyncio.CancelledError:
                        raise
                    except Exception as typing_err:
                        logger.debug(
                            "[%s] send_typing error (non-fatal): %s",
                            self.name, typing_err,
                        )
                if stop_event is None:
                    await asyncio.sleep(interval)
                    continue
                loop = asyncio.get_running_loop()
                deadline = loop.time() + interval
                while not stop_event.is_set():
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    # 用轮询代替 wait_for(stop_event.wait())。取消 wait_for 时，
                    # 若它持有内部 Event.wait 任务，可能让关闭路径在 Python
                    # 3.11/pytest-asyncio 上卡在等待 typing 任务；而 sleep 的
                    # 取消是立即的。
                    await asyncio.sleep(min(0.25, remaining))
                if stop_event.is_set():
                    return
        except asyncio.CancelledError:
            pass  # 处理器完成时的正常取消
        finally:
            # Ensure the underlying platform typing loop is stopped.
            # _keep_typing may have called send_typing() after an outer
            # stop_typing() cleared the task dict, recreating the loop.
            # Cancelling _keep_typing alone won't clean that up.
            if hasattr(self, "stop_typing"):
                try:
                    await self.stop_typing(chat_id)
                except Exception:
                    pass
            self._typing_paused.discard(chat_id)

    async def _stop_typing_refresh(
        self,
        chat_id: str,
        typing_task: asyncio.Task | None = None,
        *,
        timeout: float = 0.5,
        stop_attempts: int = 2,
    ) -> None:
        """把刷新任务和平台 typing 状态作为一次操作停止。"""
        self._typing_paused.add(chat_id)
        try:
            if typing_task is not None and not typing_task.done():
                typing_task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(typing_task), timeout=timeout)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    # 任务已被取消；不要让缓慢的适配器特定清理阻塞响应投递或关闭。
                    pass
            if not hasattr(self, "stop_typing"):
                return
            attempts = max(1, stop_attempts)
            for attempt in range(attempts):
                try:
                    await self.stop_typing(chat_id)
                except Exception:
                    pass
                if attempt < attempts - 1:
                    await asyncio.sleep(0)
        finally:
            self._typing_paused.discard(chat_id)

    def pause_typing_for_chat(self, chat_id: str) -> None:
        """为某个 chat 暂停 typing 指示器（例如在等待审批期间）。

        线程安全（CPython GIL）——可在同步 agent 线程中调用，同时
        ``_keep_typing`` 运行在 async 事件循环上。
        """
        self._typing_paused.add(chat_id)

    def resume_typing_for_chat(self, chat_id: str) -> None:
        """在审批完成后恢复某个聊天的输入指示器。"""
        self._typing_paused.discard(chat_id)

    async def interrupt_session_activity(self, session_key: str, chat_id: str) -> None:
        """通知活动的会话循环停止，并立即清除输入指示器。"""
        if session_key:
            interrupt_event = self._active_sessions.get(session_key)
            if interrupt_event is not None:
                interrupt_event.set()
        try:
            await self.stop_typing(chat_id)
        except Exception:
            pass

    def register_post_delivery_callback(
        self,
        session_key: str,
        callback: Callable,
        *,
        generation: int | None = None,
    ) -> None:
        """注册一个延迟回调，在主响应发出之后触发。

        ``generation`` 让调用方将回调与某次具体的 gateway 运行世代绑定，
        这样陈旧的运行就不会清除更新世代所拥有的回调。

        如果同一个 ``session_key``（以及 generation，若已设置）已注册过回调，
        新回调会被串联起来——两者按注册顺序触发，且每个回调之间相互隔离异常。
        这样可以让彼此独立的功能（后台评审释放 + 临时气泡清理）共存而不互相覆盖。
        陈旧世代的调用方永远不会覆盖更新世代的槽位。
        """
        if not session_key or not callable(callback):
            return

        existing = self._post_delivery_callbacks.get(session_key)
        if existing is not None:
            if isinstance(existing, tuple) and len(existing) == 2:
                existing_gen, existing_cb = existing
            else:
                existing_gen, existing_cb = None, existing
            # 陈旧世代的注册绝不会覆盖更新的槽位。
            if (
                existing_gen is not None
                and generation is not None
                and int(generation) < int(existing_gen)
            ):
                return
            # 相同或更新的世代：与既有回调串联，使两者按注册顺序触发。
            if callable(existing_cb) and (
                existing_gen is None
                or generation is None
                or int(existing_gen) == int(generation)
            ):
                _prev = existing_cb
                _new = callback

                def _chained() -> None:
                    try:
                        _prev()
                    except Exception:
                        logger.debug("Post-delivery callback failed", exc_info=True)
                    try:
                        _new()
                    except Exception:
                        logger.debug("Post-delivery callback failed", exc_info=True)

                callback = _chained

        if generation is None:
            self._post_delivery_callbacks[session_key] = callback
        else:
            self._post_delivery_callbacks[session_key] = (int(generation), callback)

    def pop_post_delivery_callback(
        self,
        session_key: str,
        *,
        generation: int | None = None,
    ) -> Callable | None:
        """弹出一个延迟回调，可选地要求世代归属。"""
        if not session_key:
            return None
        entry = self._post_delivery_callbacks.get(session_key)
        if entry is None:
            return None
        if isinstance(entry, tuple) and len(entry) == 2:
            entry_generation, callback = entry
            if generation is not None and int(entry_generation) != int(generation):
                return None
            self._post_delivery_callbacks.pop(session_key, None)
            return callback if callable(callback) else None
        if generation is not None:
            return None
        self._post_delivery_callbacks.pop(session_key, None)
        return entry if callable(entry) else None

    # ── 处理生命周期钩子 ──────────────────────────────────────────
    # 子类覆盖这些方法以对消息处理事件作出反应
    # （例如 Discord 会添加 👀/✅/❌ 反应）。

    async def on_processing_start(self, event: MessageEvent) -> None:
        """后台处理开始时调用的钩子。"""

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """后台处理完成时调用的钩子。"""

    async def _run_processing_hook(self, hook_name: str, *args: Any, **kwargs: Any) -> None:
        """运行一个生命周期钩子，不让其失败打断消息流。"""
        hook = getattr(self, hook_name, None)
        if not callable(hook):
            return
        try:
            await hook(*args, **kwargs)
        except Exception as e:
            logger.warning("[%s] %s hook failed: %s", self.name, hook_name, e)

    @staticmethod
    def _is_retryable_error(error: Optional[str]) -> bool:
        """当错误字符串看起来是瞬时网络失败时返回 True。"""
        if not error:
            return False
        lowered = error.lower()
        return any(pat in lowered for pat in _RETRYABLE_ERROR_PATTERNS)

    @staticmethod
    def _is_timeout_error(error: Optional[str]) -> bool:
        """当错误字符串表明是读/写超时时返回 True。

        超时错误不可重试，也不应触发纯文本回退——请求可能已被投递。
        """
        if not error:
            return False
        lowered = error.lower()
        return "timed out" in lowered or "readtimeout" in lowered or "writetimeout" in lowered

    def _unwrap_ephemeral(self, response: Any) -> Tuple[Optional[str], int]:
        """把处理器响应解包为 (text, ttl_seconds)。

        接受普通字符串、``None`` 或 :class:`EphemeralReply`。
        返回 ``(text, ttl)``，其中 ``ttl > 0`` 表示调用方应在发送成功后
        通过 :meth:`_schedule_ephemeral_delete` 安排删除。当适配器未覆盖
        :meth:`delete_message` 时，``ttl`` 被强制为 0，使不支持的平台静默降级为
        正常发送。
        """
        if isinstance(response, EphemeralReply):
            ttl = response.ttl_seconds
            if ttl is None:
                try:
                    ttl = int(self._get_ephemeral_system_ttl_default())
                except Exception:
                    ttl = 0
            if ttl and ttl > 0 and type(self).delete_message is BasePlatformAdapter.delete_message:
                ttl = 0
            return response.text, int(ttl or 0)
        return response, 0

    async def _send_with_retry(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Any = None,
        max_retries: int = 2,
        base_delay: float = 2.0,
    ) -> "SendResult":
        """
        发送一条消息，对瞬时网络错误自动重试。

        在永久性失败（例如格式 / 权限错误）时，放弃前会回退到纯文本版本。
        如果所有尝试都因网络错误失败，则向用户发送一条简短的投递失败通知，
        使他们知道应重试，而不是无限等待。
        """

        result = await self.send(
            chat_id=chat_id,
            content=content,
            reply_to=reply_to,
            metadata=metadata,
        )

        if result.success:
            return result

        error_str = result.error or ""
        is_network = result.retryable or self._is_retryable_error(error_str)

        # 超时错误不可安全重试（消息可能已被投递），也不是格式错误——原样返回失败。
        if not is_network and self._is_timeout_error(error_str):
            return result

        if is_network:
            # 对瞬时错误以指数退避重试。
            # 存在服务器要求的 retry_after（例如 Telegram FloodWait）时予以采纳——
            # 它比我们的退避计划更具权威性。
            server_retry_after = result.retry_after
            for attempt in range(1, max_retries + 1):
                if server_retry_after is not None:
                    delay = server_retry_after + random.uniform(0, 1)
                    server_retry_after = None  # 每次 send 只采纳一次
                else:
                    delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
                logger.warning(
                    "[%s] Send failed (attempt %d/%d, retrying in %.1fs): %s",
                    self.name, attempt, max_retries, delay, error_str,
                )
                await asyncio.sleep(delay)
                result = await self.send(
                    chat_id=chat_id,
                    content=content,
                    reply_to=reply_to,
                    metadata=metadata,
                )
                if result.success:
                    logger.info("[%s] Send succeeded on retry %d", self.name, attempt)
                    return result
                error_str = result.error or ""
                if result.retry_after is not None:
                    server_retry_after = result.retry_after
                if not (result.retryable or self._is_retryable_error(error_str)):
                    break  # 错误切换为非瞬时——落入纯文本回退
            else:
                # 所有重试用尽（循环完成而未 break）——通知用户
                logger.error("[%s] Failed to deliver response after %d retries: %s", self.name, max_retries, error_str)
                notice = (
                    "\u26a0\ufe0f Message delivery failed after multiple attempts. "
                    "Please try again \u2014 your request was processed but the response could not be sent."
                )
                try:
                    await self.send(chat_id=chat_id, content=notice, reply_to=reply_to, metadata=metadata)
                except Exception as notify_err:
                    logger.debug("[%s] Could not send delivery-failure notice: %s", self.name, notify_err)
                return result

        # 非网络 / 重试后的格式化失败：尝试纯文本作为回退
        logger.warning("[%s] Send failed: %s — trying plain-text fallback", self.name, error_str)
        fallback_result = await self.send(
            chat_id=chat_id,
            content=f"(Response formatting failed, plain text:)\n\n{content[:3500]}",
            reply_to=reply_to,
            metadata=metadata,
        )
        if not fallback_result.success:
            logger.error("[%s] Fallback send also failed: %s", self.name, fallback_result.error)
        return fallback_result

    @staticmethod
    def _merge_caption(existing_text: Optional[str], new_text: str) -> str:
        """把新说明文字合并进既有文本，避免重复。

        使用逐行精确匹配（而非子串）以防止误报：避免较短的说明文字因为作为
        较长说明文字的子串出现而被静默丢弃（例如 "Meeting" 出现在
        "Meeting agenda" 中）。比较时会归一化空白。
        """
        if not existing_text:
            return new_text
        existing_captions = [c.strip() for c in existing_text.split("\n\n")]
        if new_text.strip() not in existing_captions:
            return f"{existing_text}\n\n{new_text}".strip()
        return existing_text

    def _text_debounce_store(self) -> dict[str, TextDebounceState]:
        store = getattr(self, "_text_debounce", None)
        if store is None:
            store = {}
            self._text_debounce = store
        return store

    def _is_queue_text_debounce_candidate(self, event: MessageEvent) -> bool:
        """Return True for normal text eligible for queue-mode debounce."""
        result = (
            getattr(self, "_busy_text_mode", "interrupt") == "queue"
            and event.message_type == MessageType.TEXT
            and not getattr(event, "internal", False)
            and not event.is_command()
            and bool((event.text or "").strip())
        )
        if result:
            logger.debug(
                "[%s] Queue-text debounce candidate accepted: session=%s text_len=%d",
                self.name,
                getattr(event, "session_key", "?"),
                len(event.text or ""),
            )
        return result

    def _can_merge_text_debounce_events(self, existing: MessageEvent, event: MessageEvent) -> bool:
        """当两个文本 debounce 事件来自同一发送者时返回 True。"""

        def _identity(candidate: MessageEvent) -> tuple[str, ...] | None:
            source = getattr(candidate, "source", None)
            if source is None:
                return None
            platform = _platform_name(getattr(source, "platform", None))
            sender = getattr(source, "user_id_alt", None) or getattr(source, "user_id", None)
            if sender:
                return (platform, str(sender))
            if getattr(source, "chat_type", None) in {"dm", "private"} and getattr(source, "chat_id", None):
                return (platform, "dm", str(source.chat_id))
            return None

        existing_sender = _identity(existing)
        incoming_sender = _identity(event)
        return existing_sender is not None and existing_sender == incoming_sender

    def _text_debounce_delay(self, session_key: str) -> float:
        """返回 ``session_key`` 的有界 busy-text debounce 延迟。"""
        state = self._text_debounce_store().get(session_key)
        if state is None:
            return 0.0
        now = time.monotonic()
        window_deadline = state.last_ts + self._busy_text_debounce_seconds
        hard_cap_deadline = state.first_ts + self._busy_text_hard_cap_seconds
        return max(0.0, min(window_deadline, hard_cap_deadline) - now)

    async def _queue_text_debounce(self, session_key: str, event: MessageEvent) -> None:
        """缓冲常规 queue 模式的 busy 文本，并安排一次有界的 flush。"""
        store = self._text_debounce_store()
        state = store.get(session_key)

        if state is not None and not self._can_merge_text_debounce_events(state.event, event):
            # 在共享 session 中保留发送者归属。当前缓冲成为下一个待处理轮次；
            # 新发送者在待处理槽位允许时开始一段新的 debounce 突发。
            await self._flush_text_debounce_now(session_key)
            state = store.get(session_key)
            if state is not None and not self._can_merge_text_debounce_events(state.event, event):
                existing_pending = self._pending_messages.get(session_key)
                if existing_pending is not None and self._can_merge_text_debounce_events(existing_pending, event):
                    merge_pending_message_event(
                        self._pending_messages,
                        session_key,
                        event,
                        merge_text=True,
                    )
                return

        now = time.monotonic()
        if state is None:
            state = TextDebounceState(
                event=event,
                task=None,
                first_ts=now,
                last_ts=now,
            )
            store[session_key] = state
        else:
            if event.text:
                state.event.text = (
                    f"{state.event.text}\n{event.text}"
                    if state.event.text
                    else event.text
                )
            latest_message_id = getattr(event, "message_id", None)
            latest_anchor = latest_message_id or getattr(event, "reply_to_message_id", None)
            if latest_message_id is not None:
                state.event.message_id = str(latest_message_id)
            if latest_anchor is not None and hasattr(state.event, "reply_to_message_id"):
                state.event.reply_to_message_id = str(latest_anchor)
            state.last_ts = now

        if state.task is not None and not state.task.done():
            state.task.cancel()

        delay = self._text_debounce_delay(session_key)
        state.task = asyncio.create_task(self._flush_text_debounce(session_key, delay))

    async def _flush_text_debounce(self, session_key: str, delay: float) -> None:
        """刷新 debounce 文本缓冲的计时器任务。"""
        try:
            await asyncio.sleep(delay)
            await self._flush_text_debounce_now(session_key)
        except asyncio.CancelledError:
            return
        finally:
            current = asyncio.current_task()
            state = self._text_debounce_store().get(session_key)
            if state is not None and state.task is current:
                state.task = None

    async def _flush_text_debounce_now(self, session_key: str) -> bool:
        """强制把一次 debounce 的 busy 文本突发 flush 进待处理槽位。"""
        store = self._text_debounce_store()
        state = store.get(session_key)
        if state is None:
            return False

        current = asyncio.current_task()
        if state.task is not None and state.task is not current and not state.task.done():
            state.task.cancel()
        state.task = None

        existing_pending = self._pending_messages.get(session_key)
        if (
            existing_pending is not None
            and not self._can_merge_text_debounce_events(existing_pending, state.event)
        ):
            return False

        state = store.pop(session_key, None)
        if state is None:
            return False
        merge_pending_message_event(
            self._pending_messages,
            session_key,
            state.event,
            merge_text=True,
        )
        return True

    def _discard_text_debounce(self, session_key: str) -> None:
        """为控制命令取消并丢弃待处理文本 debounce 状态。"""
        state = self._text_debounce_store().pop(session_key, None)
        if state is not None and state.task is not None and not state.task.done():
            state.task.cancel()

    # ------------------------------------------------------------------
    # Session task + guard 归属辅助方法
    # ------------------------------------------------------------------
    # 这些与 _session_tasks owner 映射一同引入，以使 session 生命周期调和在
    # 以下路径中保持确定性：(a) 正常完成路径，(b) /stop/ /new/ /reset 绕过
    # 命令，(c) 下一条入站消息上的陈旧锁自愈。

    def _release_session_guard(
        self,
        session_key: str,
        *,
        guard: Optional[asyncio.Event] = None,
    ) -> None:
        """释放某个 session 的适配器级守卫。

        当提供了 ``guard`` 时，只有当条目仍指向那个确切的 Event 时才释放。
        这让 reset 类命令可以在旧处理任务收尾时换入一个临时守卫，而不会让
        旧任务的清理意外清除替换守卫。
        """
        current_guard = self._active_sessions.get(session_key)
        if current_guard is None:
            return
        if guard is not None and current_guard is not guard:
            return
        del self._active_sessions[session_key]

    def _session_task_is_stale(self, session_key: str) -> bool:
        """当 ``session_key`` 的 owner 任务已完成/取消时返回 True。

        当适配器仍持有 ``_active_sessions[key]``、且 ``_session_tasks`` 中
        有一个已知 owner 任务已经退出时，这个锁就是"陈旧"的。当根本不存在
        owner 任务时，通常意味着守卫是由 handle_message() 以外的某条路径
        安装的（测试有时会直接安装守卫）——不要把它视为陈旧。入口处的自愈
        只需处理生产环境的 split-brain 场景：owner 任务被记录后，未清除其
        守卫就退出了。
        """
        task = self._session_tasks.get(session_key)
        if task is None:
            return False
        done = getattr(task, "done", None)
        return bool(done and done())

    def _heal_stale_session_lock(self, session_key: str) -> bool:
        """当 owner 任务已经不在时清除陈旧的 session 锁。

        如果治愈了一个陈旧锁则返回 True。如果没有锁，或 owner 任务仍存活
        （正常的 busy 场景），则返回 False。

        这是 sidbin 的 issue #11016 分析所要求的入口安全网：没有它，一个
        split-brain——适配器仍认为 session 是活动的，但实际上没有任何东西
        在处理——会把 chat 困在无限的 "Interrupting current task..." 中，
        直到网关重启。
        """
        if session_key not in self._active_sessions:
            return False
        if not self._session_task_is_stale(session_key):
            return False
        logger.warning(
            "[%s] Healing stale session lock for %s (owner task is done/absent)",
            self.name,
            session_key,
        )
        self._active_sessions.pop(session_key, None)
        self._pending_messages.pop(session_key, None)
        self._session_tasks.pop(session_key, None)
        self._discard_text_debounce(session_key)
        return True

    def _start_session_processing(
        self,
        event: MessageEvent,
        session_key: str,
        *,
        interrupt_event: Optional[asyncio.Event] = None,
    ) -> bool:
        """在给定的 session 守卫下派生一个后台处理任务。

        成功返回 True。如果运行时把 ``create_task`` 桩成了非 Task 哨兵
        （某些测试会这样做），则回滚守卫并返回 False，使调用方不会持有
        一个半安装的 session 锁。
        """
        guard = interrupt_event or asyncio.Event()
        self._active_sessions[session_key] = guard

        task = asyncio.create_task(self._process_message_background(event, session_key))
        self._session_tasks[session_key] = task
        try:
            self._background_tasks.add(task)
        except TypeError:
            # 测试用不可哈希、也不支持生命周期回调的轻量哨兵桩掉
            # create_task()。
            self._session_tasks.pop(session_key, None)
            self._release_session_guard(session_key, guard=guard)
            return False
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)
            task.add_done_callback(self._expected_cancelled_tasks.discard)
        return True

    async def cancel_session_processing(
        self,
        session_key: str,
        *,
        release_guard: bool = True,
        discard_pending: bool = True,
    ) -> None:
        """取消单个 session 的进行中处理。

        ``release_guard=False`` 保留适配器级的 session 守卫，使 reset 类命令
        能在后续消息被允许启动新的后台任务之前原子地完成。

        受 5 秒超时约束，使被取消任务中卡住的 finally 块（typing 任务清理、
        on_processing_complete 钩子等）不能拖住调用方的 dispatch 协程——尤其
        在 pytest-asyncio 下，事件循环的取消传播语义与裸 ``asyncio.run`` 框架
        存在细微差别。
        """
        task = self._session_tasks.pop(session_key, None)
        if task is not None and not task.done():
            logger.debug(
                "[%s] Cancelling active processing for session %s",
                self.name,
                session_key,
            )
            self._expected_cancelled_tasks.add(task)
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                logger.warning(
                    "[%s] Cancelled task for %s did not exit within 5s; "
                    "unblocking dispatch and letting the task unwind in the background",
                    self.name, session_key,
                )
            except Exception:
                logger.debug(
                    "[%s] Session cancellation raised while unwinding %s",
                    self.name,
                    session_key,
                    exc_info=True,
                )
        if discard_pending:
            self._pending_messages.pop(session_key, None)
            self._discard_text_debounce(session_key)
        if release_guard:
            self._release_session_guard(session_key)

    async def _drain_pending_after_session_command(
        self,
        session_key: str,
        command_guard: asyncio.Event,
    ) -> None:
        """在某个 session 命令完成后，恢复最新排队的跟进消息。

        在 /stop、/new 和 /reset 分发的尾部调用。释放命令作用域的守卫，
        然后——如果命令运行期间有一条跟进消息到达——为它派生一个新的处理任务。
        """
        await self._flush_text_debounce_now(session_key)
        pending_event = self._pending_messages.pop(session_key, None)
        self._release_session_guard(session_key, guard=command_guard)
        if pending_event is None:
            return
        self._start_session_processing(pending_event, session_key)

    async def _dispatch_active_session_command(
        self,
        event: MessageEvent,
        session_key: str,
        cmd: str,
    ) -> None:
        """分发一个 reset 类绕过命令，同时保留守卫顺序。

        /stop、/new 和 /reset 必须：
          1. 在 runner 处理命令期间保持 session 守卫已安装（这样竞态的跟进
             消息会保持排队，而不是作为第二次并行运行被分发）。
          2. 只在 runner 完成命令处理*之后*取消旧的进行中适配器任务
            （使 runner 看到一致状态，其响应按顺序发送）。
          3. 在 1 和 2 完成后，释放命令作用域守卫，并精确一次地排出最新
             排队的跟进消息。
        """
        logger.debug(
            "[%s] Command '/%s' bypassing active-session guard for %s",
            self.name,
            cmd,
            session_key,
        )

        current_guard = self._active_sessions.get(session_key)
        command_guard = asyncio.Event()
        self._active_sessions[session_key] = command_guard
        thread_meta = _thread_metadata_for_source(event.source, _reply_anchor_for_event(event))

        try:
            response = await self._message_handler(event)
            _text, _eph_ttl = self._unwrap_ephemeral(response)
            # 在取消旧任务*之前*发送响应，使发送不会受任务取消副作用影响
            # （竞态条件修复 —— issue #18912）。此前发送发生在
            # cancel_session_processing 之后，当 agent 正在运行时可能静默丢弃
            # "/new" 确认。
            if _text:
                logger.info(
                    "[%s] Sending command '/%s' response (%d chars) to %s",
                    self.name,
                    cmd,
                    len(_text),
                    event.source.chat_id,
                )
                _r = await self._send_with_retry(
                    chat_id=event.source.chat_id,
                    content=_text,
                    reply_to=_reply_anchor_for_event(event),
                    metadata=_mark_notify_metadata(thread_meta),
                )
                if _eph_ttl > 0 and _r.success and _r.message_id:
                    self._schedule_ephemeral_delete(
                        chat_id=event.source.chat_id,
                        message_id=_r.message_id,
                        ttl_seconds=_eph_ttl,
                    )
            # 旧的适配器任务（如果有）在响应发送*之后*才被取消——保持顺序
            # 确定性，避免竞态。
            await self.cancel_session_processing(
                session_key,
                release_guard=False,
                discard_pending=False,
            )
        except Exception:
            # 失败时，如果原守卫仍存在则恢复它，避免让 session 处于半 reset 状态。
            if self._active_sessions.get(session_key) is command_guard:
                if session_key in self._session_tasks and current_guard is not None:
                    self._active_sessions[session_key] = current_guard
                else:
                    self._release_session_guard(session_key, guard=command_guard)
            raise

        await self._drain_pending_after_session_command(session_key, command_guard)

    async def handle_message(self, event: MessageEvent) -> None:
        """
        处理一条入站消息。

        本方法通过派生后台任务快速返回。
        这使得即便 agent 正在运行，新消息也能被处理，从而支持中断。
        """
        if not self._message_handler:
            return

        coerce_plaintext_gateway_command(event)

        # 通过已安装的恢复钩子（Telegram DM topic 模式）改写
        # ``event.source.thread_id``，使 session key、守卫检查和下游投递
        # 都在同一个通道上达成一致。
        self._apply_topic_recovery(event)

        session_key = build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )

        # 入口处自愈：如果适配器仍持有该 key 的 _active_sessions 条目，
        # 但 owner 任务已经退出（完成或取消），这个锁就是陈旧的。清除它并
        # 落入正常分发，使用户不会被卡在一个死守卫之后——这就是 issue #11016
        # 描述的 split-brain 尾部场景。
        if session_key in self._active_sessions:
            self._heal_stale_session_lock(session_key)

        # 检查该 session 是否已有活动处理器
        if session_key in self._active_sessions:
            # 某些命令必须绕过活动 session 守卫，直接分发给网关 runner。
            # 否则它们会被当作待处理消息排队，然后：
            #   - 作为用户文本泄漏进对话（/stop、/new），或
            #   - 死锁（/approve、/deny —— agent 阻塞在 Event.wait 上）
            #
            # 内联分发：直接调用消息处理器并发送响应。不要使用
            # _process_message_background——它管理 session 生命周期，其清理
            # 会与正在运行的任务竞态（见 PR #4926）。
            cmd = event.get_command()
            from hermes_cli.commands import should_bypass_active_session

            if should_bypass_active_session(cmd):
                # /stop、/new、/reset 必须取消进行中的适配器任务，并保留
                # 排队跟进消息的顺序。把这些路由到专门的 handoff 路径，
                # 它会串行化取消 + runner 响应 + 待处理排出。
                if cmd in {"stop", "new", "reset"}:
                    self._discard_text_debounce(session_key)
                    try:
                        await self._dispatch_active_session_command(event, session_key, cmd)
                    except Exception as e:
                        logger.error(
                            "[%s] Command '/%s' dispatch failed: %s",
                            self.name, cmd, e, exc_info=True,
                        )
                    return

                # 其它绕过命令（/approve、/deny、/status、/background、
                # /restart）只需直接分发——它们不会取消正在运行的任务。
                logger.debug(
                    "[%s] Command '/%s' bypassing active-session guard for %s",
                    self.name, cmd, session_key,
                )
                try:
                    _thread_meta = _thread_metadata_for_source(event.source, _reply_anchor_for_event(event))
                    response = await self._message_handler(event)
                    _text, _eph_ttl = self._unwrap_ephemeral(response)
                    if _text:
                        _r = await self._send_with_retry(
                            chat_id=event.source.chat_id,
                            content=_text,
                            reply_to=_reply_anchor_for_event(event),
                            metadata=_mark_notify_metadata(_thread_meta),
                        )
                        if _eph_ttl > 0 and _r.success and _r.message_id:
                            self._schedule_ephemeral_delete(
                                chat_id=event.source.chat_id,
                                message_id=_r.message_id,
                                ttl_seconds=_eph_ttl,
                            )
                except Exception as e:
                    logger.error("[%s] Command '/%s' dispatch failed: %s", self.name, cmd, e, exc_info=True)
                return

            # Clarify 文本捕获绕过：如果 agent 阻塞在 clarify_tool 调用上，
            # 等待自由格式文本响应（开放式 clarify，或用户选择了 "Other"），
            # 那么本 session 中的下一条非命令消息必须到达 runner，以便
            # clarify 拦截能 resolve 它并解除 agent 阻塞。
            #
            # 没有这个绕过：消息会作为跟进轮次排队进 _pending_messages，
            # 而不是到达 clarify resolver，使 agent 保持阻塞并丢弃用户的回答。
            # 形态与 /approve 死锁修复（PR #4926）相同——两种情况都是
            # "agent 线程阻塞在 Event.wait 上，消息在被当作新轮次之前必须
            # 到达 resolver"。
            if not cmd:
                try:
                    from tools import clarify_gateway as _clarify_mod
                    _has_text_clarify = (
                        _clarify_mod.get_pending_for_session(session_key) is not None
                    )
                except Exception:
                    _has_text_clarify = False

                if _has_text_clarify:
                    logger.debug(
                        "[%s] Routing message to clarify text-intercept for %s",
                        self.name, session_key,
                    )
                    try:
                        _thread_meta = _thread_metadata_for_source(
                            event.source, _reply_anchor_for_event(event)
                        )
                        response = await self._message_handler(event)
                        _text, _eph_ttl = self._unwrap_ephemeral(response)
                        if _text:
                            _r = await self._send_with_retry(
                                chat_id=event.source.chat_id,
                                content=_text,
                                reply_to=_reply_anchor_for_event(event),
                                metadata=_mark_notify_metadata(_thread_meta),
                            )
                            if _eph_ttl > 0 and _r.success and _r.message_id:
                                self._schedule_ephemeral_delete(
                                    chat_id=event.source.chat_id,
                                    message_id=_r.message_id,
                                    ttl_seconds=_eph_ttl,
                                )
                    except Exception as e:
                        logger.error(
                            "[%s] Clarify text-intercept dispatch failed: %s",
                            self.name, e, exc_info=True,
                        )
                    return

            if self._busy_session_handler is not None:
                try:
                    if await self._busy_session_handler(event, session_key):
                        return
                except Exception as e:
                    logger.error("[%s] Busy-session handler failed: %s", self.name, e, exc_info=True)

            # 特殊情况：照片连拍/相册经常以多条近乎同时的消息到达。把它们
            # 排队而不打断活动运行，然后在当前任务结束后立即处理。
            if event.message_type == MessageType.PHOTO:
                logger.debug("[%s] Queuing photo follow-up for session %s without interrupt", self.name, session_key)
                merge_pending_message_event(self._pending_messages, session_key, event)
                return  # 现在不打断——当前任务完成后会运行

            if self._is_queue_text_debounce_candidate(event):
                logger.debug(
                    "[%s] New text message while session %s is active — "
                    "debouncing follow-up (busy_text_mode=queue, window=%.2fs)",
                    self.name,
                    session_key,
                    self._busy_text_debounce_seconds,
                )
                await self._queue_text_debounce(session_key, event)
            else:
                logger.debug(
                    "[%s] New message while session %s is active — queuing follow-up "
                    "(no interrupt, will cascade after current turn)",
                    self.name,
                    session_key,
                )
                merge_pending_message_event(
                    self._pending_messages,
                    session_key,
                    event,
                    merge_text=event.message_type == MessageType.TEXT,
                )
            return  # 现在不处理——当前任务完成后会处理

        # 在派生后台任务*之前*把 session 标记为活动，以关闭一个竞态窗口：
        # 否则任务启动前到达的第二条消息也会通过 _active_sessions 检查并
        # 派生重复任务。（grammY sequentialize / aiogram EventIsolation 模式——
        # 同步设置守卫，而不是在任务内部。）
        # _start_session_processing 原子地安装守卫和 owner 任务映射，使陈旧锁
        # 检测能够工作。
        self._start_session_processing(event, session_key)
    
    @staticmethod
    def _get_human_delay() -> float:
        """
        返回一个随机延迟（秒），用于拟人化的响应节奏。

        从环境变量读取：
          HERMES_HUMAN_DELAY_MODE："off"（默认）| "natural" | "custom"
          HERMES_HUMAN_DELAY_MIN_MS：最小延迟（毫秒，默认 800，custom 模式）
          HERMES_HUMAN_DELAY_MAX_MS：最大延迟（毫秒，默认 2500，custom 模式）
        """
        mode = os.getenv("HERMES_HUMAN_DELAY_MODE", "off").lower()
        if mode == "off":
            return 0.0
        if mode == "natural":
            min_ms, max_ms = 800, 2500
            return random.uniform(min_ms / 1000.0, max_ms / 1000.0)
        # custom 模式——容忍格式错误的环境变量，而不是崩溃。
        try:
            min_ms = int(os.getenv("HERMES_HUMAN_DELAY_MIN_MS", "800"))
        except (TypeError, ValueError):
            min_ms = 800
        try:
            max_ms = int(os.getenv("HERMES_HUMAN_DELAY_MAX_MS", "2500"))
        except (TypeError, ValueError):
            max_ms = 2500
        return random.uniform(min_ms / 1000.0, max_ms / 1000.0)

    async def _process_message_background(self, event: MessageEvent, session_key: str) -> None:
        """实际处理消息的后台任务。"""
        # 为处理完成钩子跟踪投递结果
        delivery_attempted = False
        delivery_succeeded = False

        def _record_delivery(result):
            nonlocal delivery_attempted, delivery_succeeded
            if result is None:
                return
            delivery_attempted = True
            if getattr(result, "success", False):
                delivery_succeeded = True

        # 复用 handle_message() 设置的中断事件（它在派生本任务之前把 session
        # 标记为活动以防竞态）。仅当条目被外部移除时才回退到一个新 Event。
        interrupt_event = self._active_sessions.get(session_key) or asyncio.Event()
        self._active_sessions[session_key] = interrupt_event

        # 启动持续 typing 指示器（每 2 秒刷新）
        _thread_metadata = _thread_metadata_for_source(event.source, _reply_anchor_for_event(event))
        _keep_typing_kwargs = {"metadata": _thread_metadata}
        try:
            _keep_typing_sig = inspect.signature(self._keep_typing)
        except (TypeError, ValueError):
            _keep_typing_sig = None
        if _keep_typing_sig is None or "stop_event" in _keep_typing_sig.parameters:
            _keep_typing_kwargs["stop_event"] = interrupt_event
        typing_task = asyncio.create_task(
            self._keep_typing(
                event.source.chat_id,
                **_keep_typing_kwargs,
            )
        )

        async def _stop_typing_task() -> None:
            await self._stop_typing_refresh(
                event.source.chat_id,
                typing_task,
            )
        
        try:
            await self._run_processing_hook("on_processing_start", event)

            # 调用处理器（含 tool 调用时会比较耗时）
            response = await self._message_handler(event)
            is_ephemeral_response = isinstance(response, EphemeralReply)

            # 斜杠命令处理器可能返回一个 EphemeralReply 哨兵，请求其回复消息
            # 在 TTL 后自动删除（用于像 "✨ New session started!" 这种用户无需
            # 在 thread 中保留的系统通知）。在此解包，使下游所有
            # extract_media / 文本处理逻辑看到的是普通字符串，并记住 TTL +
            # 平台能力，以便发送后代码块能安排删除。
            response, _ephemeral_ttl = self._unwrap_ephemeral(response)

            # 如有则发送响应。None/空响应在流式已投递文本（already_sent=True）
            # 或消息被排在活动 agent 之后时是正常的。以 DEBUG 级别记录，避免对
            # 预期行为产生嘈杂的警告。
            #
            # 当 session 被一条尚未消费的新消息中断时，抑制陈旧响应。待处理
            # 消息由下面的待处理消息处理器处理（#8221/#2483）。
            if (
                response
                and interrupt_event.is_set()
                and session_key in self._pending_messages
            ):
                logger.info(
                    "[%s] Suppressing stale response for interrupted session %s",
                    self.name,
                    session_key,
                )
                response = None
            if not response:
                logger.debug("[%s] Handler returned empty/None response for %s", self.name, event.source.chat_id)
            if response:
                # Capture [[as_document]] before extract_media strips it, so the
                # dispatch partition below can route image-extension files
                # through send_document instead of send_multiple_images. Used
                # by skills that produce large/lossless images (e.g. info-graph)
                # where Telegram's sendPhoto recompression destroys legibility.
                force_document_attachments = "[[as_document]]" in response

                # Pre-extract snapshot for the #29346 recovery/invariant below.
                _response_pre_extract = response

                # Extract MEDIA:<path> tags (from TTS tool) before other processing
                media_files, response = self.extract_media(response)
                media_files = self.filter_media_delivery_paths(media_files)

                # Extract image URLs and send them as native platform attachments
                images, text_content = self.extract_images(response)
                # 剥除消息体中任何残留的内部指令（修复 #1561）。
                # _strip_media_directives 共享 MEDIA_TAG_CLEANUP_RE，因此带未知
                # 扩展名的 MEDIA: 标签会被有意保留在正文中，供下面的
                # extract_local_files 拾取，而不是被静默丢弃（#34517）。
                text_content = _strip_media_directives(text_content).strip()
                if images:
                    logger.info("[%s] extract_images found %d image(s) in response (%d chars)", self.name, len(images), len(response))

                local_files = []
                if not is_ephemeral_response:
                    # 自动检测裸本地文件路径，用于原生媒体投递
                    # （帮助不使用 MEDIA: 语法的小模型）。跳过系统/命令通知，
                    # 这样配置路径仍是可见文本，而不是变成原生上传。
                    local_files, text_content = self.extract_local_files(text_content)
                    local_files = self.filter_local_delivery_paths(local_files)
                    if local_files:
                        logger.info("[%s] extract_local_files found %d file(s) in response", self.name, len(local_files))

                # A2（#29346）：提取可能把一个非空响应缩减为无附件的空文本，
                # 下面 `if text_content` 守卫会随之静默丢弃它。在每个平台上
                # 都做恢复（#33842 曾仅限 Discord）；该守卫避免重复投递附件。
                if not (text_content or images or local_files or media_files):
                    # 从 extract_media 之后的 `response` 恢复，而不是从原始快照：
                    # extract_media 已用其完整语法剥除了 MEDIA（含带空格路径），
                    # 因此不会有片段泄漏。
                    _recovered = _strip_media_directives(response).strip()
                    if _recovered:
                        logger.warning(
                            "[%s] response_delivery_recovered: extract pipeline "
                            "reduced a non-empty response (%d chars) to empty with "
                            "no attachment; delivering recovered original to %s",
                            self.name, len(_response_pre_extract), event.source.chat_id,
                        )
                        text_content = _recovered

                # 最终用户可见内容（文本、TTS、媒体、文件）会获得既有的
                # notify=True 标记。克隆一次，使 typing/status 元数据保持未标记，
                # progress 气泡保持 thread 严格。
                _final_thread_metadata = _mark_notify_metadata(_thread_metadata)

                # 自动 TTS：如果是语音消息，先（在发送文本之前）生成音频
                # 通过 ``_should_auto_tts_for_chat`` 门控：当 chat 有显式
                # ``/voice on|tts`` opt-in，或全局 ``voice.auto_tts`` 为真
                # 且未发出过 ``/voice off`` 时触发。
                _tts_path = None
                if (self._should_auto_tts_for_chat(event.source.chat_id)
                        and event.message_type == MessageType.VOICE
                        and text_content
                        and not media_files):
                    try:
                        from tools.tts_tool import text_to_speech_tool, check_tts_requirements
                        if check_tts_requirements():
                            import json as _json
                            speech_text = self.prepare_tts_text(text_content)
                            if not speech_text:
                                raise ValueError("Empty text after markdown cleanup")
                            tts_result_str = await asyncio.to_thread(
                                text_to_speech_tool, text=speech_text
                            )
                            tts_data = _json.loads(tts_result_str)
                            _tts_path = tts_data.get("file_path")
                    except Exception as tts_err:
                        logger.warning("[%s] Auto-TTS failed: %s", self.name, tts_err)

                # 在文本之前播放 TTS 音频（语音优先体验）
                _tts_caption_delivered = False
                if _tts_path and Path(_tts_path).exists():
                    try:
                        telegram_tts_caption = None
                        if (
                            self.platform == Platform.TELEGRAM
                            and text_content
                            and text_content[:1024] == text_content
                        ):
                            telegram_tts_caption = text_content
                        tts_result = await self.play_tts(
                            chat_id=event.source.chat_id,
                            audio_path=_tts_path,
                            caption=telegram_tts_caption,
                            metadata=_final_thread_metadata,
                        )
                        _tts_caption_delivered = bool(
                            telegram_tts_caption and getattr(tts_result, "success", False)
                        )
                    finally:
                        try:
                            os.remove(_tts_path)
                        except OSError:
                            pass

                # 发送文本部分
                if text_content and not _tts_caption_delivered:
                    logger.info("[%s] Sending response (%d chars) to %s", self.name, len(text_content), event.source.chat_id)
                    _reply_anchor = _reply_anchor_for_event(event)
                    result = await self._send_with_retry(
                        chat_id=event.source.chat_id,
                        content=text_content,
                        reply_to=_reply_anchor,
                        metadata=_final_thread_metadata,
                    )
                    _record_delivery(result)

                    # 安排系统通知回复的自动删除。
                    # 分离执行，使处理器立即返回；错误（权限拒绝、消息过旧）被吞掉。
                    if (
                        _ephemeral_ttl
                        and _ephemeral_ttl > 0
                        and result.success
                        and result.message_id
                    ):
                        self._schedule_ephemeral_delete(
                            chat_id=event.source.chat_id,
                            message_id=result.message_id,
                            ttl_seconds=_ephemeral_ttl,
                        )

                # 文本与媒体之间拟人化的节奏延迟
                human_delay = self._get_human_delay()

                # 把提取出的图片作为原生附件发送
                if images:
                    logger.info("[%s] Extracted %d image(s) to send as attachments", self.name, len(images))
                    try:
                        await self.send_multiple_images(
                            chat_id=event.source.chat_id,
                            images=images,
                            metadata=_final_thread_metadata,
                            human_delay=human_delay,
                        )
                    except Exception as batch_err:
                        logger.warning("[%s] Error batching images: %s", self.name, batch_err, exc_info=True)


                # 发送提取出的媒体文件——按文件类型路由
                _VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.3gp'}
                _IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}

                # 把图片从 media_files + local_files 中分出来，以便作为单批
                # 发送（Signal RPC）。当原始响应设置了 ``[[as_document]]`` 时，
                # 图片文件跳过 photo 路径，路由到下面的 send_document，使其以
                # 原始字节投递（不做 Telegram sendPhoto 重压缩）。
                from urllib.parse import quote as _quote
                _image_paths: list = []
                _non_image_media: list = []
                for media_path, is_voice in media_files:
                    _ext = Path(media_path).suffix.lower()
                    if (_ext in _IMAGE_EXTS
                            and not is_voice
                            and not force_document_attachments):
                        _image_paths.append(media_path)
                    else:
                        _non_image_media.append((media_path, is_voice))
                _non_image_local: list = []
                for file_path in local_files:
                    if (Path(file_path).suffix.lower() in _IMAGE_EXTS
                            and not force_document_attachments):
                        _image_paths.append(file_path)
                    else:
                        _non_image_local.append(file_path)

                if _image_paths:
                    try:
                        _batch = [(f"file://{_quote(p)}", "") for p in _image_paths]
                        await self.send_multiple_images(
                            chat_id=event.source.chat_id,
                            images=_batch,
                            metadata=_final_thread_metadata,
                            human_delay=human_delay,
                        )
                    except Exception as batch_err:
                        logger.warning("[%s] Error batching images: %s", self.name, batch_err, exc_info=True)

                for media_path, is_voice in _non_image_media:
                    if human_delay > 0:
                        await asyncio.sleep(human_delay)
                    try:
                        ext = Path(media_path).suffix.lower()
                        if should_send_media_as_audio(self.platform, ext, is_voice=is_voice):
                            media_result = await self.send_voice(
                                chat_id=event.source.chat_id,
                                audio_path=media_path,
                                metadata=_final_thread_metadata,
                            )
                        elif ext in _VIDEO_EXTS:
                            media_result = await self.send_video(
                                chat_id=event.source.chat_id,
                                video_path=media_path,
                                metadata=_final_thread_metadata,
                            )
                        else:
                            media_result = await self.send_document(
                                chat_id=event.source.chat_id,
                                file_path=media_path,
                                metadata=_final_thread_metadata,
                            )

                        if not media_result.success:
                            logger.warning("[%s] Failed to send media (%s): %s", self.name, ext, media_result.error)
                    except Exception as media_err:
                        logger.warning("[%s] Error sending media: %s", self.name, media_err)

                # 把自动检测到的本地非图片文件作为原生附件发送
                for file_path in _non_image_local:
                    if human_delay > 0:
                        await asyncio.sleep(human_delay)
                    try:
                        ext = Path(file_path).suffix.lower()
                        if ext in _VIDEO_EXTS:
                            await self.send_video(
                                chat_id=event.source.chat_id,
                                video_path=file_path,
                                metadata=_final_thread_metadata,
                            )
                        else:
                            await self.send_document(
                                chat_id=event.source.chat_id,
                                file_path=file_path,
                                metadata=_final_thread_metadata,
                            )
                    except Exception as file_err:
                        logger.error("[%s] Error sending local file %s: %s", self.name, file_path, file_err)

                # A3（#29346）：如果非空响应没有产出任何可投递内容，
                # 大声失败，而不是静默丢弃。
                _anything_delivered = (
                    delivery_attempted or _tts_caption_delivered
                    or images or local_files or media_files
                )
                if not _anything_delivered and _response_pre_extract.strip():
                    logger.error(
                        "[%s] response_delivery_dropped: non-empty response "
                        "(%d chars) produced no delivered message or attachment "
                        "for %s (empty after extract, recovery yielded nothing).",
                        self.name, len(_response_pre_extract), event.source.chat_id,
                    )

            # 为处理钩子判定整体成功与否
            processing_ok = delivery_succeeded if delivery_attempted else not bool(response)
            await self._run_processing_hook(
                "on_processing_complete",
                event,
                ProcessingOutcome.SUCCESS if processing_ok else ProcessingOutcome.FAILURE,
            )

            # 活动排出拥有 debounce 状态。如果某个 queue 模式计时器尚未触发，
            # 在此处强制 flush 进 _pending_messages，让本任务交接跟进消息。
            await self._flush_text_debounce_now(session_key)

            # 检查在我们处理期间是否有待处理消息被排队
            if session_key in self._pending_messages:
                pending_event = self._pending_messages.pop(session_key)
                logger.debug("[%s] Processing queued follow-up message", self.name)
                # 在轮次链中保持 _active_sessions 条目存活，只清除中断 Event——
                # 不要删除条目。如果在此删除，下面 awaits 期间到达的并发入站消息
                # 会通过 Level-1 守卫，派生自己的 _process_message_background，
                # 与下面的递归排出同时运行。一个 session_key 上两个 agent = 重复
                # 响应、重复 tool 调用。清除 Event 让守卫保持存活，使跟进消息按
                # 预期走 busy-handler 路径。
                _active = self._active_sessions.get(session_key)
                if _active is not None:
                    _active.clear()
                await _stop_typing_task()
                # Spawn a fresh task for the pending message instead of
                # recursing.  Issue #17758: `await
                # self._process_message_background(...)` here grew the
                # call stack one frame per chained follow-up, and under
                # sustained pending-queue activity the C stack would
                # exhaust at ~2000 frames and SIGSEGV the process.
                # Mirror the late-arrival drain pattern below: hand off
                # to a new task and return so this frame can unwind.
                drain_task = asyncio.create_task(
                    self._process_message_background(pending_event, session_key)
                )
                # Hand ownership of the session to the drain task so
                # stale-lock detection keeps working while it runs.
                self._session_tasks[session_key] = drain_task
                try:
                    self._background_tasks.add(drain_task)
                    drain_task.add_done_callback(self._background_tasks.discard)
                except TypeError:
                    # Tests stub create_task() with non-hashable sentinels; tolerate.
                    pass
                return  # Drain task owns the session now.
                
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            outcome = ProcessingOutcome.CANCELLED
            if current_task is None or current_task not in self._expected_cancelled_tasks:
                outcome = ProcessingOutcome.FAILURE
            await self._run_processing_hook("on_processing_complete", event, outcome)
            raise
        except Exception as e:
            await self._run_processing_hook("on_processing_complete", event, ProcessingOutcome.FAILURE)
            logger.error("[%s] Error handling message: %s", self.name, e, exc_info=True)
            # Send the error to the user so they aren't left with radio silence
            try:
                error_type = type(e).__name__
                error_detail = str(e)[:300] if str(e) else "no details available"
                _thread_metadata = _thread_metadata_for_source(event.source, _reply_anchor_for_event(event))
                await self.send(
                    chat_id=event.source.chat_id,
                    content=(
                        f"Sorry, I encountered an error ({error_type}).\n"
                        f"{error_detail}\n"
                        "Try again or use /reset to start a fresh session."
                    ),
                    metadata=_thread_metadata,
                )
            except Exception:
                pass  # Last resort — don't let error reporting crash the handler
        finally:
            # Stop typing before any deferred callback work.  Post-delivery
            # callbacks may perform platform I/O; a stuck callback must not
            # leave the typing refresh task running indefinitely.
            await _stop_typing_task()
            # Fire any one-shot post-delivery callback registered for this
            # session (e.g. deferred background-review notifications).
            #
            # Snapshot the callback generation HERE (after the agent has run),
            # not at the top of this task.  _hermes_run_generation is set on
            # the interrupt event by GatewayRunner._bind_adapter_run_generation
            # during _handle_message_with_agent — which happens DURING the
            # self._message_handler(event) await above.  Snapshotting earlier
            # always captured None, which bypassed the generation-ownership
            # check in pop_post_delivery_callback and let stale runs fire a
            # fresher run's callbacks.
            _callback_generation = getattr(
                interrupt_event,
                "_hermes_run_generation",
                None,
            )
            if hasattr(self, "pop_post_delivery_callback"):
                _post_cb = self.pop_post_delivery_callback(
                    session_key,
                    generation=_callback_generation,
                )
            else:
                _post_cb = getattr(self, "_post_delivery_callbacks", {}).pop(session_key, None)
            if callable(_post_cb):
                try:
                    _post_result = _post_cb()
                    if inspect.isawaitable(_post_result):
                        await asyncio.wait_for(
                            _post_result,
                            timeout=_POST_DELIVERY_CALLBACK_TIMEOUT_SECONDS,
                        )
                except (asyncio.TimeoutError, Exception):
                    pass
            # Some adapters keep platform-level typing tasks.  If callback
            # work or a late refresh recreated one, make one final bounded stop
            # before releasing the session guard.
            await self._stop_typing_refresh(
                event.source.chat_id,
                None,
                stop_attempts=1,
            )
            # Final drain/release boundary: force-flush any timer that missed
            # the in-band drain before deciding whether the guard can clear.
            await self._flush_text_debounce_now(session_key)
            # Late-arrival drain: a message may have arrived during the
            # cleanup awaits above (typing_task cancel, stop_typing).  Such
            # messages passed the Level-1 guard (entry still live, Event
            # possibly set) and landed in _pending_messages via the
            # busy-handler path.  Without this block, we would delete the
            # active-session entry and the queued message would be silently
            # dropped (user never gets a reply).
            late_pending = self._pending_messages.pop(session_key, None)
            if late_pending is not None:
                current_task = asyncio.current_task()
                existing_task = self._session_tasks.get(session_key)
                if (
                    existing_task is not None
                    and existing_task is not current_task
                ):
                    # The in-band drain (or an earlier late-arrival drain)
                    # already spawned a follow-up task that owns this
                    # session.  Re-queue the late-arrival event so that
                    # task picks it up — avoids spawning two concurrent
                    # _process_message_background tasks for the same key
                    # (#17758 follow-up: prevents the create_task path
                    # from racing with itself across the in-band/finally
                    # boundary).
                    self._pending_messages[session_key] = late_pending
                else:
                    logger.debug(
                        "[%s] Late-arrival pending message during cleanup — spawning drain task",
                        self.name,
                    )
                    _active = self._active_sessions.get(session_key)
                    if _active is not None:
                        _active.clear()
                    drain_task = asyncio.create_task(
                        self._process_message_background(late_pending, session_key)
                    )
                    # Hand ownership of the session to the drain task so stale-lock
                    # detection keeps working while it runs.
                    self._session_tasks[session_key] = drain_task
                    try:
                        self._background_tasks.add(drain_task)
                        drain_task.add_done_callback(self._background_tasks.discard)
                    except TypeError:
                        # Tests stub create_task() with non-hashable sentinels; tolerate.
                        pass
                # Leave _active_sessions[session_key] populated — the drain
                # task's own lifecycle will clean it up.
            else:
                # Clean up session tracking.  Guard-match both deletes so a
                # reset-like command that already swapped in its own
                # command_guard (and cancelled us) can't be accidentally
                # cleared by our unwind.  The command owns the session now.
                #
                # The owner-check also covers the in-band drain handoff
                # above: when we spawned a drain_task and transferred
                # ownership via ``_session_tasks[session_key] = drain_task``,
                # ``_session_tasks.get(session_key) is current_task`` is
                # False, so we leave _active_sessions populated.  Without
                # this guard, the drain task picks up the same
                # interrupt_event in its own _process_message_background
                # entry, _release_session_guard's guard-match succeeds,
                # and we'd delete the entry while the drain task is still
                # running — letting a concurrent inbound message pass
                # the Level-1 guard and spawn a second handler for the
                # same session.
                current_task = asyncio.current_task()
                if current_task is not None and self._session_tasks.get(session_key) is current_task:
                    self._cleanup_finished_session_task(session_key, interrupt_event)
    
    def _cleanup_finished_session_task(
        self, session_key: str, interrupt_event: Optional[asyncio.Event]
    ) -> None:
        """Release the session guard for a finished owner task, then drop its
        ``_session_tasks`` entry ONLY if the guard was actually released.

        Release-then-conditional-delete is the #48300 fix: when a concurrent
        path (reset/new command, drain handoff) swapped ``_active_sessions[key]``
        to a different guard, ``_release_session_guard`` skips on the guard
        mismatch and the lock stays installed. If we deleted ``_session_tasks``
        unconditionally (the old order), ``_session_task_is_stale`` would later
        see no owner task and report "not stale", so the orphaned guard would
        never be healed — a permanent session deadlock. Keeping the done-task
        entry when the guard survives lets the on-entry self-heal detect the
        stale lock and clear it on the next inbound message.
        """
        self._release_session_guard(session_key, guard=interrupt_event)
        if session_key not in self._active_sessions:
            self._session_tasks.pop(session_key, None)
    
    async def cancel_background_tasks(self) -> None:
        """Cancel any in-flight background message-processing tasks.

        Used during gateway shutdown/replacement so active sessions from the old
        process do not keep running after adapters are being torn down.

        Each cancelled task is awaited with a 5s bound so a wedged finally
        (typing-task cleanup, on_processing_complete hook) can't stall the
        whole shutdown path.  Stragglers are released from our tracking and
        allowed to finish unwinding on their own.
        """
        # Loop until no new tasks appear.  Without this, a message
        # arriving during the `await asyncio.gather` below would spawn
        # a fresh _process_message_background task (added to
        # self._background_tasks at line ~1668 via handle_message),
        # and the _background_tasks.clear() at the end of this method
        # would drop the reference — the task runs untracked against a
        # disconnecting adapter, logs send-failures, and may linger
        # until it completes on its own.  Retrying the drain until the
        # task set stabilizes closes the window.
        MAX_DRAIN_ROUNDS = 5
        for _ in range(MAX_DRAIN_ROUNDS):
            tasks = [task for task in self._background_tasks if not task.done()]
            if not tasks:
                break
            for task in tasks:
                self._expected_cancelled_tasks.add(task)
                task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(asyncio.shield(t) for t in tasks),
                        return_exceptions=True,
                    ),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[%s] %d background task(s) did not exit within 5s; "
                    "releasing tracking and letting them unwind in the background",
                    self.name, len([t for t in tasks if not t.done()]),
                )
                break
            # Loop: late-arrival tasks spawned during the gather above
            # will be in self._background_tasks now.  Re-check.
        self._background_tasks.clear()
        self._expected_cancelled_tasks.clear()
        self._session_tasks.clear()
        self._pending_messages.clear()
        self._active_sessions.clear()
        for state in list(self._text_debounce_store().values()):
            if state.task is not None and not state.task.done():
                state.task.cancel()
        self._text_debounce_store().clear()

    def has_pending_interrupt(self, session_key: str) -> bool:
        """Check if there's a pending interrupt for a session."""
        return session_key in self._active_sessions and self._active_sessions[session_key].is_set()
    
    def get_pending_message(self, session_key: str) -> Optional[MessageEvent]:
        """Get and clear any pending message for a session."""
        return self._pending_messages.pop(session_key, None)
    
    def build_source(
        self,
        chat_id: str,
        chat_name: Optional[str] = None,
        chat_type: str = "dm",
        user_id: Optional[str] = None,
        user_name: Optional[str] = None,
        thread_id: Optional[str] = None,
        chat_topic: Optional[str] = None,
        user_id_alt: Optional[str] = None,
        chat_id_alt: Optional[str] = None,
        is_bot: bool = False,
        guild_id: Optional[str] = None,
        parent_chat_id: Optional[str] = None,
        message_id: Optional[str] = None,
        role_authorized: bool = False,
    ) -> SessionSource:
        """Helper to build a SessionSource for this platform."""
        # Normalize empty topic to None
        if chat_topic is not None and not chat_topic.strip():
            chat_topic = None
        return SessionSource(
            platform=self.platform,
            chat_id=str(chat_id),
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=str(user_id) if user_id else None,
            user_name=user_name,
            thread_id=str(thread_id) if thread_id else None,
            chat_topic=chat_topic.strip() if chat_topic else None,
            user_id_alt=user_id_alt,
            chat_id_alt=chat_id_alt,
            is_bot=is_bot,
            guild_id=str(guild_id) if guild_id else None,
            parent_chat_id=str(parent_chat_id) if parent_chat_id else None,
            message_id=str(message_id) if message_id else None,
            role_authorized=role_authorized,
        )
    
    @abstractmethod
    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """
        Get information about a chat/channel.
        
        Returns dict with at least:
        - name: Chat name
        - type: "dm", "group", "channel"
        """
        pass
    
    def format_message(self, content: str) -> str:
        """
        Format a message for this platform.
        
        Override in subclasses to handle platform-specific formatting
        (e.g., Telegram MarkdownV2, Discord markdown).
        
        Default implementation returns content as-is.
        """
        return content
    
    @staticmethod
    def truncate_message(
        content: str,
        max_length: int = 4096,
        len_fn: Optional["Callable[[str], int]"] = None,
    ) -> List[str]:
        """
        Split a long message into chunks, preserving code block boundaries.

        When a split falls inside a triple-backtick code block, the fence is
        closed at the end of the current chunk and reopened (with the original
        language tag) at the start of the next chunk.  Multi-chunk responses
        receive indicators like ``(1/3)``.

        Args:
            content: The full message content
            max_length: Maximum length per chunk (platform-specific)
            len_fn: Optional length function for measuring string length.
                     Defaults to ``len`` (Unicode code-points).  Pass
                     ``utf16_len`` for platforms that measure message
                     length in UTF-16 code units (e.g. Telegram).

        Returns:
            List of message chunks
        """
        _len = len_fn or len
        if _len(content) <= max_length:
            return [content]

        INDICATOR_RESERVE = 10   # room for " (XX/XX)"
        FENCE_CLOSE = "\n```"

        chunks: List[str] = []
        remaining = content
        # When the previous chunk ended mid-code-block, this holds the
        # language tag (possibly "") so we can reopen the fence.
        carry_lang: Optional[str] = None

        while remaining:
            # If we're continuing a code block from the previous chunk,
            # prepend a new opening fence with the same language tag.
            prefix = f"```{carry_lang}\n" if carry_lang is not None else ""

            # How much body text we can fit after accounting for the prefix,
            # a potential closing fence, and the chunk indicator.
            headroom = max_length - INDICATOR_RESERVE - _len(prefix) - _len(FENCE_CLOSE)
            if headroom < 1:
                headroom = max_length // 2

            # Everything remaining fits in one final chunk
            if _len(prefix) + _len(remaining) <= max_length - INDICATOR_RESERVE:
                chunks.append(prefix + remaining)
                break

            # Find a natural split point (prefer newlines, then spaces).
            # When _len != len (e.g. utf16_len for Telegram), headroom is
            # measured in the custom unit.  We need codepoint-based slice
            # positions that stay within the custom-unit budget.
            #
            # _safe_slice_pos() maps a custom-unit budget to the largest
            # codepoint offset whose custom length ≤ budget.
            if _len is not len:
                # Map headroom (custom units) → codepoint slice length
                _cp_limit = _custom_unit_to_cp(remaining, headroom, _len)
            else:
                _cp_limit = headroom
            region = remaining[:_cp_limit]
            split_at = region.rfind("\n")
            if split_at < _cp_limit // 2:
                split_at = region.rfind(" ")
            if split_at < 1:
                split_at = _cp_limit

            # Avoid splitting inside an inline code span (`...`).
            # If the text before split_at has an odd number of unescaped
            # backticks, the split falls inside inline code — the resulting
            # chunk would have an unpaired backtick and any special characters
            # (like parentheses) inside the broken span would be unescaped,
            # causing MarkdownV2 parse errors on Telegram.
            candidate = remaining[:split_at]
            backtick_count = candidate.count("`") - candidate.count("\\`")
            if backtick_count % 2 == 1:
                # Find the last unescaped backtick and split before it
                last_bt = candidate.rfind("`")
                while last_bt > 0 and candidate[last_bt - 1] == "\\":
                    last_bt = candidate.rfind("`", 0, last_bt)
                if last_bt > 0:
                    # Try to find a space or newline just before the backtick
                    safe_split = candidate.rfind(" ", 0, last_bt)
                    nl_split = candidate.rfind("\n", 0, last_bt)
                    safe_split = max(safe_split, nl_split)
                    if safe_split > _cp_limit // 4:
                        split_at = safe_split

            chunk_body = remaining[:split_at]
            remaining = remaining[split_at:].lstrip()

            full_chunk = prefix + chunk_body

            # Walk only the chunk_body (not the prefix we prepended) to
            # determine whether we end inside an open code block.
            in_code = carry_lang is not None
            lang = carry_lang or ""
            for line in chunk_body.split("\n"):
                stripped = line.strip()
                if stripped.startswith("```"):
                    if in_code:
                        in_code = False
                        lang = ""
                    else:
                        in_code = True
                        tag = stripped[3:].strip()
                        lang = tag.split()[0] if tag else ""

            if in_code:
                # Close the orphaned fence so the chunk is valid on its own
                full_chunk += FENCE_CLOSE
                carry_lang = lang
            else:
                carry_lang = None

            chunks.append(full_chunk)

        # Append chunk indicators when the response spans multiple messages
        if len(chunks) > 1:
            total = len(chunks)
            chunks = [
                f"{chunk} ({i + 1}/{total})" for i, chunk in enumerate(chunks)
            ]

        return chunks
