"""发送消息工具 —— 通过各平台 API 实现跨渠道消息发送。

向任一已连接的即时通讯平台（Telegram、Discord、Slack）上的用户或渠道
发送消息。支持列出可用目标，并将人类可读的渠道名称解析为 ID。在 CLI
和 gateway 两种环境下均可工作。
"""

import asyncio
import json
import logging
import os
import re
import ssl
import time
from email.utils import formatdate

from agent.redact import redact_sensitive_text

logger = logging.getLogger(__name__)

_TELEGRAM_TOPIC_TARGET_RE = re.compile(r"^\s*(-?\d+)(?::(\d+))?\s*$")
_FEISHU_TARGET_RE = re.compile(r"^\s*((?:oc|ou|on|chat|open)_[-A-Za-z0-9]+)(?::([-A-Za-z0-9_]+))?\s*$")
# Slack 会话 ID：C（公开频道）、G（私密/群组频道）、D（私信）。
# 必须是大写字母数字，至少 9 个字符。用户 ID（U...）和工作区 ID
#（W...）不是合法的 chat.postMessage channel 取值 —— 向它们发送会失败，
# 因为 API 要求传会话 ID。要给某个用户发私信，必须先调用
# conversations.open 拿到一个 D... ID。没有这道闸门，Slack ID 会
# 落入按名称解析渠道的分支，而那里只能按名称匹配，最终失败。
_SLACK_TARGET_RE = re.compile(r"^\s*([CGDU][A-Z0-9]{8,})\s*$")
# 由会话派生的 Slack 话题目标使用「<conversation_id>:<thread_ts>」格式。
_SLACK_THREAD_TARGET_RE = re.compile(r"^\s*([CGD][A-Z0-9]{8,}):([^\s:]+)\s*$")
_WEIXIN_TARGET_RE = re.compile(r"^\s*((?:wxid|gh|v\d+|wm|wb)_[A-Za-z0-9_-]+|[A-Za-z0-9._-]+@chatroom|filehelper)\s*$")
_YUANBAO_TARGET_RE = re.compile(r"^\s*((?:group|direct):[^:]+)\s*$")
# Discord 的 snowflake ID 是数字，与 Telegram topic 目标共用同一个正则。
_NUMERIC_TOPIC_RE = _TELEGRAM_TOPIC_TARGET_RE
# 这些平台按手机号寻址收件人，并接受 E.164 格式（带前导「+」）。
# 如果不做这层处理，「+15551234567」会无法通过下面的 isdigit() 校验，
# 进而落入按名称解析渠道的分支，而那里无法解析一个原始手机号。
# 保留「+」是为了维持下游适配器（signal 等）所期望的 E.164 形式。
_PHONE_PLATFORMS = frozenset({"photon", "signal", "sms", "whatsapp"})
_E164_TARGET_RE = re.compile(r"^\s*\+(\d{7,15})\s*$")
# WhatsApp JID：群聊（<digits>@g.us）、个人用户
#（<phone>@s.whatsapp.net）、关联身份（<id>@lid），以及广播 /
# 订阅消息聊天。这些是 bridge 直接接受的显式原生目标 ——
# 它们绝不能落入家渠道解析流程。
_WHATSAPP_JID_RE = re.compile(
    r"^\s*[\w-]+@(?:g\.us|s\.whatsapp\.net|lid|broadcast|newsletter)\s*$",
    re.IGNORECASE,
)
# 邮箱地址 —— 形如「user@domain.com」的合法邮箱应被当作 email 平台的
# 显式目标处理，而不是落入按名称解析渠道的分支（该分支无法解析原始地址）。
_EMAIL_TARGET_RE = re.compile(r"^\s*[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\s*$")
# 大多数平台从「<PLATFORM>_HOME_CHANNEL」读取自己的家渠道，但有少数例外。
# email 读取的是 EMAIL_HOME_ADDRESS（见 gateway/config.py），因此通用的
# 「<PLATFORM>_HOME_CHANNEL」提示会把用户引向一个永远不会被读取的变量。
# 这里把例外映射出来，让错误指引真正具有可操作性。
_HOME_CHANNEL_ENV_OVERRIDES = {"email": "EMAIL_HOME_ADDRESS"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".3gp"}
_AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac"}
_VOICE_EXTS = {".ogg", ".opus"}
# Telegram 的 Bot API sendAudio 只接受 MP3 / M4A。其他音频格式
# 要么走 sendVoice（Opus/OGG），要么回退为文档投递。
_TELEGRAM_SEND_AUDIO_EXTS = {".mp3", ".m4a"}
_URL_SECRET_QUERY_RE = re.compile(
    r"([?&](?:access_token|api[_-]?key|auth[_-]?token|token|signature|sig)=)([^&#\s]+)",
    re.IGNORECASE,
)
_GENERIC_SECRET_ASSIGN_RE = re.compile(
    r"\b(access_token|api[_-]?key|auth[_-]?token|signature|sig)\s*=\s*([^\s,;]+)",
    re.IGNORECASE,
)


def _sanitize_error_text(text) -> str:
    """在错误文本呈现给用户/模型之前，先脱敏其中的密钥信息。"""
    redacted = redact_sensitive_text(text)
    redacted = _URL_SECRET_QUERY_RE.sub(lambda m: f"{m.group(1)}***", redacted)
    redacted = _GENERIC_SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)}=***", redacted)
    return redacted


def _error(message: str) -> dict:
    """构造一个内容已脱敏的标准化错误载荷。"""
    return {"error": _sanitize_error_text(message)}


def _display_chat_id(platform_name: str, chat_id: str) -> str:
    """返回一个对结果安全（可入 transcript/日志）的聊天标识符。"""
    if platform_name == "signal" and str(chat_id).startswith("group:"):
        return "group:***"
    return chat_id


def _telegram_retry_delay(exc: Exception, attempt: int) -> float | None:
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        try:
            return max(float(retry_after), 0.0)
        except (TypeError, ValueError):
            return 1.0

    text = str(exc).lower()
    if "timed out" in text or "timeout" in text:
        return None
    if (
        "bad gateway" in text
        or "502" in text
        or "too many requests" in text
        or "429" in text
        or "service unavailable" in text
        or "503" in text
        or "gateway timeout" in text
        or "504" in text
    ):
        return float(2 ** attempt)
    return None


async def _send_telegram_message_with_retry(bot, *, attempts: int = 3, **kwargs):
    for attempt in range(attempts):
        try:
            return await bot.send_message(**kwargs)
        except Exception as exc:
            delay = _telegram_retry_delay(exc, attempt)
            if delay is None or attempt >= attempts - 1:
                raise
            logger.warning(
                "Transient Telegram send failure (attempt %d/%d), retrying in %.1fs: %s",
                attempt + 1,
                attempts,
                delay,
                _sanitize_error_text(exc),
            )
            await asyncio.sleep(delay)


SEND_MESSAGE_SCHEMA = {
    "name": "send_message",
    "description": (
        "Send a message to a connected messaging platform, or list available targets.\n\n"
        "IMPORTANT: When the user asks to send to a specific channel or person "
        "(not just a bare platform name), call send_message(action='list') FIRST to see "
        "available targets, then send to the correct one.\n"
        "If the user just says a platform name like 'send to telegram', send directly "
        "to the home channel without listing first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["send", "list", "react", "unreact"],
                "description": "Action to perform. 'send' (default) sends a message. 'list' returns all available channels/contacts across connected platforms. 'react' attaches an emoji reaction to a message (platforms that support it, e.g. photon/iMessage tapbacks). 'unreact' retracts a previously-added reaction."
            },
            "target": {
                "type": "string",
                "description": "Delivery target. Format: 'platform' (uses home channel), 'platform:#channel-name', 'platform:chat_id', or 'platform:chat_id:thread_id' for Telegram topics and Discord threads. Examples: 'telegram', 'telegram:-1001234567890:17585', 'discord:999888777:555444333', 'discord:#bot-home', 'slack:#engineering', 'signal:+155****4567', 'matrix:!roomid:server.org', 'matrix:@user:server.org', 'ntfy:alerts-channel' (explicit ntfy topic), 'yuanbao:direct:<account_id>' (DM), 'yuanbao:group:<group_code>' (group chat)"
            },
            "message": {
                "type": "string",
                "description": "The message text to send. To send an image or file, include MEDIA:<local_path> (e.g. 'MEDIA:/tmp/report.pdf') in the message — the platform will deliver it as a native media attachment."
            },
            "emoji": {
                "type": "string",
                "description": "For action='react': the emoji to react with (e.g. '❤️'). On iMessage, ❤️👍👎😂‼️❓ render as native tapbacks; other emoji use custom-emoji reactions."
            },
            "message_id": {
                "type": "string",
                "description": "For action='react'/'unreact': id of the message to react to. Omit to target the most recent message received in that chat (usually the one being replied to)."
            }
        },
        "required": []
    }
}


def send_message_tool(args, **kw):
    """处理跨渠道的 send_message 工具调用。"""
    action = args.get("action", "send")

    if action == "list":
        return _handle_list()

    if action == "react":
        return _handle_react(args)

    if action == "unreact":
        return _handle_react(args, remove=True)

    return _handle_send(args)


def _handle_list():
    """返回格式化后的可用消息目标列表。"""
    try:
        from gateway.channel_directory import format_directory_for_display
        return json.dumps({"targets": format_directory_for_display()})
    except Exception as e:
        return json.dumps(_error(f"Failed to load channel directory: {e}"))


def _handle_react(args, remove=False):
    """通过一个运行中的 gateway 适配器，为某条消息添加（或在
    ``remove=True`` 时撤回）emoji 表态。

    只有暴露了 ``add_reaction(chat_id, emoji, message_id)`` /
    ``remove_reaction(chat_id, message_id)`` 协程的适配器才支持此功能
    （例如 photon/iMessage 的 tapback）。要求 gateway 在本进程内运行 ——
    没有独立的回退路径，因为表态依赖适配器实时的消息 id 状态。
    """
    target = args.get("target", "")
    emoji = (args.get("emoji") or "").strip()
    message_id = (args.get("message_id") or "").strip() or None
    if not target or (not remove and not emoji):
        return tool_error(
            "Both 'target' and 'emoji' are required when action='react'"
            if not remove
            else "'target' is required when action='unreact'"
        )

    parts = target.split(":", 1)
    platform_name = parts[0].strip().lower()
    target_ref = parts[1].strip() if len(parts) > 1 else None
    chat_id = None
    if target_ref:
        chat_id, _thread_id, _ = _parse_target_ref(platform_name, target_ref)
        if not chat_id:
            try:
                from gateway.channel_directory import resolve_channel_name
                resolved = resolve_channel_name(platform_name, target_ref)
            except Exception:
                resolved = None
            # 不透明的平台原生 id（例如形如「any;-;+1555...」的 photon
            # space GUID）匹配不到任何解析模式，也没有目录条目 ——
            # 直接原样透传，由适配器负责校验。
            chat_id = resolved or target_ref

    try:
        from gateway.config import Platform, load_gateway_config
        platform = Platform(platform_name)
    except (ValueError, KeyError):
        return tool_error(f"Unknown platform: {platform_name}")

    if not chat_id:
        try:
            config = load_gateway_config()
            home = config.get_home_channel(platform)
        except Exception:
            home = None
        if not home:
            return tool_error(
                f"No chat specified and no home channel set for {platform_name}. "
                f"Use '{platform_name}:chat_id'."
            )
        chat_id = home.chat_id

    runner = None
    try:
        from gateway.run import _gateway_runner_ref
        runner = _gateway_runner_ref()
    except Exception:
        runner = None
    adapter = runner.adapters.get(platform) if runner is not None else None
    if adapter is None:
        return tool_error(
            f"Reactions require a live {platform_name} adapter in the running "
            "gateway (not available from cron/standalone contexts)."
        )
    fn_name = "remove_reaction" if remove else "add_reaction"
    react_fn = getattr(adapter, fn_name, None)
    if not callable(react_fn):
        return tool_error(
            f"Platform '{platform_name}' does not support message reactions."
        )

    try:
        from model_tools import _run_async
        if remove:
            result = _run_async(
                react_fn(chat_id=chat_id, message_id=message_id)
            )
        else:
            result = _run_async(
                react_fn(chat_id=chat_id, emoji=emoji, message_id=message_id)
            )
    except Exception as e:
        return json.dumps(_error(f"Reaction failed: {e}"))
    if isinstance(result, dict):
        return json.dumps(result)
    return json.dumps({"success": bool(result)})


def _handle_send(args):
    """向某个平台目标发送一条消息。"""
    target = args.get("target", "")
    message = args.get("message", "")
    if not target or not message:
        return tool_error("Both 'target' and 'message' are required when action='send'")

    parts = target.split(":", 1)
    platform_name = parts[0].strip().lower()
    target_ref = parts[1].strip() if len(parts) > 1 else None
    chat_id = None
    thread_id = None

    if target_ref:
        chat_id, thread_id, is_explicit = _parse_target_ref(platform_name, target_ref)
    else:
        is_explicit = False

    # 把人类可读的渠道名称解析为数字 ID
    if target_ref and not is_explicit:
        try:
            from gateway.channel_directory import resolve_channel_name
            resolved = resolve_channel_name(platform_name, target_ref)
            if resolved:
                chat_id, thread_id, _ = _parse_target_ref(platform_name, resolved)
            else:
                return json.dumps({
                    "error": f"Could not resolve '{target_ref}' on {platform_name}. "
                    f"Use send_message(action='list') to see available targets."
                })
        except Exception:
            return json.dumps({
                "error": f"Could not resolve '{target_ref}' on {platform_name}. "
                f"Try using a numeric channel ID instead."
            })

    from tools.interrupt import is_interrupted
    if is_interrupted():
        return tool_error("Interrupted")

    try:
        from gateway.config import load_gateway_config, Platform
        config = load_gateway_config()
    except Exception as e:
        return json.dumps(_error(f"Failed to load gateway config: {e}"))

    # 接受任意平台名 —— 内置名称会解析为对应的枚举成员，
    # 插件平台名称则通过 _missing_() 动态创建成员。
    try:
        platform = Platform(platform_name)
    except (ValueError, KeyError):
        return tool_error(f"Unknown platform: {platform_name}")

    pconfig = config.platforms.get(platform)
    if not pconfig or not pconfig.enabled:
        # Weixin 可以完全通过 .env 配置；这里合成一个 pconfig，让
        # send_message 和 cron 投递在缺少 gateway.yaml 条目时也能工作。
        if platform_name == "weixin":
            wx_token = os.getenv("WEIXIN_TOKEN", "").strip()
            wx_account = os.getenv("WEIXIN_ACCOUNT_ID", "").strip()
            if wx_token and wx_account:
                from gateway.config import PlatformConfig
                pconfig = PlatformConfig(
                    enabled=True,
                    token=wx_token,
                    extra={
                        "account_id": wx_account,
                        "base_url": os.getenv("WEIXIN_BASE_URL", "").strip(),
                        "cdn_base_url": os.getenv("WEIXIN_CDN_BASE_URL", "").strip(),
                    },
                )
            else:
                return tool_error(f"Platform '{platform_name}' is not configured. Set up credentials in ~/.hermes/config.yaml or environment variables.")
        else:
            return tool_error(f"Platform '{platform_name}' is not configured. Set up credentials in ~/.hermes/config.yaml or environment variables.")

    from gateway.platforms.base import BasePlatformAdapter

    # 在 extract_media 把 [[as_document]] 指令剥离之前先捕获它。
    # 本批中图片扩展名的文件会改走 send_document 而非 send_photo，
    # 从而保住原始字节（例如信息图表 JPG —— Telegram 的 sendPhoto 会
    # 把它重压缩到 1280px）。
    force_document_attachments = "[[as_document]]" in message

    media_files, cleaned_message = BasePlatformAdapter.extract_media(message)
    media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
    mirror_text = cleaned_message.strip() or _describe_media_for_mirror(media_files)

    used_home_channel = False
    if not chat_id:
        home = config.get_home_channel(platform)
        if not home and platform_name == "weixin":
            wx_home = os.getenv("WEIXIN_HOME_CHANNEL", "").strip()
            if wx_home:
                from gateway.config import HomeChannel
                home = HomeChannel(platform=platform, chat_id=wx_home, name="Weixin Home")
        if home:
            chat_id = home.chat_id
            used_home_channel = True
        else:
            home_env = _HOME_CHANNEL_ENV_OVERRIDES.get(
                platform_name, f"{platform_name.upper()}_HOME_CHANNEL"
            )
            return json.dumps({
                "error": f"No home channel set for {platform_name} to determine where to send the message. "
                f"Either specify a channel directly with '{platform_name}:CHANNEL_NAME', "
                f"or set a home channel via: hermes config set {home_env} <channel_id>"
            })

    duplicate_skip = _maybe_skip_cron_duplicate_send(platform_name, chat_id, thread_id)
    if duplicate_skip:
        return json.dumps(duplicate_skip)

    # Slack：通过 conversations.open 把用户 ID（U...）解析为私信频道 ID
    if platform_name == "slack" and chat_id and chat_id.startswith("U"):
        try:
            import aiohttp
            async def _open_slack_dm(token, user_id):
                url = "https://slack.com/api/conversations.open"
                headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                    async with session.post(url, headers=headers, json={"users": [user_id]}) as resp:
                        data = await resp.json()
                        if data.get("ok"):
                            return data["channel"]["id"]
                        return None
            from model_tools import _run_async
            dm_channel = _run_async(_open_slack_dm(pconfig.token, chat_id))
            if dm_channel:
                chat_id = dm_channel
            else:
                return json.dumps({"error": f"Could not open DM with Slack user {chat_id}. Check bot permissions (im:write)."})
        except Exception as e:
            return json.dumps({"error": f"Failed to open Slack DM: {e}"})

    try:
        from model_tools import _run_async
        result = _run_async(
            _send_to_platform(
                platform,
                pconfig,
                chat_id,
                cleaned_message,
                thread_id=thread_id,
                media_files=media_files,
                force_document=force_document_attachments,
            )
        )
        if used_home_channel and isinstance(result, dict) and result.get("success"):
            result["note"] = f"Sent to {platform_name} home channel (chat_id: {chat_id})"

        # 把已发送的消息镜像写入目标所在的 gateway 会话
        if isinstance(result, dict) and result.get("success") and mirror_text:
            try:
                from gateway.mirror import mirror_to_session
                from gateway.session_context import get_session_env
                source_label = get_session_env("HERMES_SESSION_PLATFORM", "cli")
                user_id = get_session_env("HERMES_SESSION_USER_ID", "") or None
                if mirror_to_session(
                    platform_name,
                    chat_id,
                    mirror_text,
                    source_label=source_label,
                    thread_id=thread_id,
                    user_id=user_id,
                ):
                    result["mirrored"] = True
            except Exception:
                pass

        if isinstance(result, dict) and "error" in result:
            result["error"] = _sanitize_error_text(result["error"])
        return json.dumps(result)
    except Exception as e:
        return json.dumps(_error(f"Send failed: {e}"))


def _parse_target_ref(platform_name: str, target_ref: str):
    """把工具目标解析为 chat_id/thread_id，并判断它是否为显式目标。"""
    if platform_name == "telegram":
        match = _TELEGRAM_TOPIC_TARGET_RE.fullmatch(target_ref)
        if match:
            return match.group(1), match.group(2), True
    if platform_name == "feishu":
        match = _FEISHU_TARGET_RE.fullmatch(target_ref)
        if match:
            return match.group(1), match.group(2), True
    if platform_name == "discord":
        match = _NUMERIC_TOPIC_RE.fullmatch(target_ref)
        if match:
            return match.group(1), match.group(2), True
    if platform_name == "slack":
        match = _SLACK_THREAD_TARGET_RE.fullmatch(target_ref)
        if match:
            return match.group(1), match.group(2), True
        match = _SLACK_TARGET_RE.fullmatch(target_ref)
        if match:
            chat_id = match.group(1)
            # Slack 的用户 ID（U...）和工作区 ID（W...）不是合法的
            # 显式发送目标 —— chat.postMessage 会拒绝它们。必须先通过
            # conversations.open 开一个私信，拿到 D... 会话 ID。
            # 这里仍把 chat_id 返回给调用方，以便 send_message() 中
            # 的 U→D 解析路径得以执行。
            is_explicit = chat_id[0] not in {"U", "W"}
            return chat_id, None, is_explicit
    if platform_name == "matrix":
        trimmed = target_ref.strip()
        split_idx = trimmed.rfind(":$")
        if split_idx > 0:
            return trimmed[:split_idx], trimmed[split_idx + 1 :], True
    if platform_name == "weixin":
        match = _WEIXIN_TARGET_RE.fullmatch(target_ref)
        if match:
            return match.group(1), None, True
    if platform_name == "yuanbao":
        match = _YUANBAO_TARGET_RE.fullmatch(target_ref)
        if match:
            return match.group(1), None, True
        if target_ref.strip().isdigit():
            return f"group:{target_ref.strip()}", None, True
        return None, None, False
    if platform_name == "ntfy":
        topic = target_ref.strip()
        if topic:
            return topic, None, True
    if platform_name == "email":
        match = _EMAIL_TARGET_RE.fullmatch(target_ref)
        if match:
            return target_ref.strip(), None, True
    if platform_name == "whatsapp":
        # 原生 WhatsApp JID（群组 @g.us、用户 @s.whatsapp.net、@lid 等）
        # 是显式目标 —— 直接原样透传。E.164 的「+」号码会落入
        # 下面的 _PHONE_PLATFORMS 处理逻辑。
        if _WHATSAPP_JID_RE.fullmatch(target_ref):
            return target_ref.strip(), None, True
    stripped_target = target_ref.strip()
    if platform_name == "signal" and stripped_target.startswith("group:"):
        group_id = stripped_target[len("group:"):].strip()
        if group_id:
            return f"group:{group_id}", None, True
        return None, None, False
    if platform_name in _PHONE_PLATFORMS:
        match = _E164_TARGET_RE.fullmatch(target_ref)
        if match:
            # 保留前导「+」—— signal-cli 和 sms/whatsapp 适配器对
            # 直连收件人期望的就是 E.164 格式。
            return target_ref.strip(), None, True
    if target_ref.lstrip("-").isdigit():
        return target_ref, None, True
    # Matrix 的房间 ID（以 ! 开头）和用户 ID（以 @ 开头）属于显式目标
    if platform_name == "matrix" and (target_ref.startswith("!") or target_ref.startswith("@")):
        return target_ref, None, True
    # XMPP JID（user@server 或 room@conference.server）属于显式目标
    if platform_name == "xmpp" and "@" in target_ref:
        return target_ref, None, True
    return None, None, False


def _describe_media_for_mirror(media_files):
    """当一条消息只包含媒体时，返回一段人类可读的镜像摘要。"""
    if not media_files:
        return ""
    if len(media_files) == 1:
        media_path, is_voice = media_files[0]
        ext = os.path.splitext(media_path)[1].lower()
        if is_voice and ext in _VOICE_EXTS:
            return "[Sent voice message]"
        if ext in _IMAGE_EXTS:
            return "[Sent image attachment]"
        if ext in _VIDEO_EXTS:
            return "[Sent video attachment]"
        if ext in _AUDIO_EXTS:
            return "[Sent audio attachment]"
        return "[Sent document attachment]"
    return f"[Sent {len(media_files)} media attachments]"


def _get_cron_auto_delivery_target():
    """返回当前运行中 cron 调度器的自动投递目标（若存在）。"""
    from gateway.session_context import get_session_env
    platform = get_session_env("HERMES_CRON_AUTO_DELIVER_PLATFORM", "").strip().lower()
    chat_id = get_session_env("HERMES_CRON_AUTO_DELIVER_CHAT_ID", "").strip()
    if not platform or not chat_id:
        return None
    thread_id = get_session_env("HERMES_CRON_AUTO_DELIVER_THREAD_ID", "").strip() or None
    return {
        "platform": platform,
        "chat_id": chat_id,
        "thread_id": thread_id,
    }


def _maybe_skip_cron_duplicate_send(platform_name: str, chat_id: str, thread_id: str | None):
    """当调度器会自动投递到同一目标时，跳过多余的 cron send_message 调用。"""
    auto_target = _get_cron_auto_delivery_target()
    if not auto_target:
        return None

    same_target = (
        auto_target["platform"] == platform_name
        and str(auto_target["chat_id"]) == str(chat_id)
        and auto_target.get("thread_id") == thread_id
    )
    if not same_target:
        return None

    target_label = f"{platform_name}:{chat_id}"
    if thread_id is not None:
        target_label += f":{thread_id}"

    return {
        "success": True,
        "skipped": True,
        "reason": "cron_auto_delivery_duplicate_target",
        "target": target_label,
        "note": (
            f"Skipped send_message to {target_label}. This cron job will already auto-deliver "
            "its final response to that same target. Put the intended user-facing content in "
            "your final response instead, or use a different target if you want an additional message."
        ),
    }


async def _send_via_adapter(
    platform,
    pconfig,
    chat_id,
    chunk,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
):
    """通过运行中的 gateway 适配器发送消息，并为进程外的调用方（例如
    与 gateway 分开运行的 cron）提供一个独立回退路径。

    尝试顺序：
      1. 经由 ``_gateway_runner_ref()`` 的进程内运行中适配器（本次改动
         之前就已存在的路径）。
      2. 插件在其 ``PlatformEntry`` 上注册的 ``standalone_sender_fn``
        （当 gateway 不在本进程时使用，此时 runner 的弱引用为 ``None``）。
      3. 一个描述性错误，解释上述两种选项。
    """
    platform_name = platform.value if hasattr(platform, "value") else str(platform)
    runner = None
    try:
        from gateway.run import _gateway_runner_ref
        runner = _gateway_runner_ref()
    except Exception:
        runner = None

    if runner is not None:
        try:
            adapter = runner.adapters.get(platform)
        except Exception:
            adapter = None
        if adapter is not None:
            try:
                metadata = {}
                if thread_id:
                    metadata["thread_id"] = thread_id
                if platform_name == "ntfy" and chat_id:
                    metadata["publish_topic"] = chat_id
                if not metadata:
                    metadata = None
                result = await adapter.send(chat_id=chat_id, content=chunk, metadata=metadata)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                return {"error": f"Plugin platform send failed: {e}"}
            if result.success:
                return {"success": True, "message_id": result.message_id}
            return {"error": f"Adapter send failed: {result.error}"}

    entry = None
    try:
        from gateway.platform_registry import platform_registry
        entry = platform_registry.get(platform_name)
    except Exception:
        entry = None

    if entry is not None and entry.standalone_sender_fn is not None:
        try:
            result = await entry.standalone_sender_fn(
                pconfig,
                chat_id,
                chunk,
                thread_id=thread_id,
                media_files=media_files,
                force_document=force_document,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("Plugin standalone send for %s raised", platform_name, exc_info=True)
            return {"error": f"Plugin standalone send failed: {e}"}

        if isinstance(result, dict) and (result.get("success") or result.get("error")):
            return result
        return {
            "error": (
                f"Plugin standalone send for '{platform_name}' returned an "
                f"invalid result: expected a dict with 'success' or 'error' "
                f"keys, got {type(result).__name__}"
            )
        }

    return {
        "error": (
            f"No live adapter for platform '{platform_name}'. Is the gateway "
            f"running with this platform connected? For out-of-process delivery "
            f"(e.g. cron in a separate process), the platform plugin must "
            f"register a standalone_sender_fn on its PlatformEntry."
        )
    }


async def _send_to_platform(platform, pconfig, chat_id, message, thread_id=None, media_files=None, force_document=False):
    """把消息路由到对应的平台发送器。

    长消息会自动分块以适配平台限制，使用与 gateway 适配器相同的智能
    分割算法（保留代码块边界、添加分片指示符）。
    """
    from gateway.config import Platform

    media_files = media_files or []

    # Weixin 在自己的原生辅助函数内部处理文本/媒体投递，不需要下面那些
    # 可选的平台适配器导入。把这条分支放在前面，这样一次 Weixin 发送
    # 就不会被无关的可选依赖（例如 lark-oapi 那条很重的 Feishu 导入
    # 路径）所阻塞。
    if platform == Platform.WEIXIN:
        return await _send_weixin(pconfig, chat_id, message, media_files=media_files)

    from gateway.platforms.base import BasePlatformAdapter, utf16_len

    # Telegram 适配器的导入是可选的（需要 python-telegram-bot）
    try:
        from plugins.platforms.telegram.adapter import TelegramAdapter
        _telegram_available = True
    except ImportError:
        _telegram_available = False

    # Feishu 适配器已迁移为插件（#41112）；它的 max_message_length
    #（8000）现在通过下面的注册表回退路径生效。

    media_files = media_files or []

    # Slack 的 mrkdwn 格式化是在 slack 插件的 _standalone_send
    #（即注册表的 standalone_sender_fn）内部应用的，而不是在这里 ——
    # SlackAdapter 在 #41112 中已迁移到 plugins/platforms/slack/。

    # 平台消息长度上限（内置平台取自适配器的类属性；插件取自
    # PlatformEntry.max_message_length，通过下面的注册表回退路径解析 ——
    # 覆盖了在 #41112 中迁移为插件的 Slack 和 Feishu）。
    _MAX_LENGTHS = {
        Platform.TELEGRAM: TelegramAdapter.MAX_MESSAGE_LENGTH if _telegram_available else 4096,
    }

    # 检查插件注册表里是否有 max_message_length
    if platform not in _MAX_LENGTHS:
        try:
            from gateway.platform_registry import platform_registry
            entry = platform_registry.get(platform.value)
            if entry and entry.max_message_length > 0:
                _MAX_LENGTHS[platform] = entry.max_message_length
        except Exception:
            pass

    # 对消息做智能分块以适配平台限制。
    # 对于短消息或没有已知上限的平台，这里是空操作。
    # Telegram 按 UTF-16 码元而非 Unicode 码点来计量长度。
    max_len = _MAX_LENGTHS.get(platform)
    if max_len:
        _len_fn = utf16_len if platform == Platform.TELEGRAM else None
        chunks = BasePlatformAdapter.truncate_message(message, max_len, len_fn=_len_fn)
    else:
        chunks = [message]

    # --- Telegram：媒体附件的特殊处理 ---
    if platform == Platform.TELEGRAM:
        last_result = None
        disable_link_previews = bool(getattr(pconfig, "extra", {}) and pconfig.extra.get("disable_link_previews"))
        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            result = await _send_telegram(
                pconfig.token,
                chat_id,
                chunk,
                media_files=media_files if is_last else [],
                thread_id=thread_id,
                disable_link_previews=disable_link_previews,
                force_document=force_document,
            )
            if isinstance(result, dict) and result.get("error"):
                return result
            last_result = result
        return last_result

    # --- Discord：通过注册表的 standalone_sender_fn 做分块投递。
    # 插件的 ``_standalone_send``（在 plugins/platforms/discord/adapter.py
    # 中注册）负责处理论坛频道、话题，以及多分片媒体上传。
    # ``_send_via_adapter`` 会先经由 ``adapter.send()`` 尝试进程内的运行中
    # 适配器，但历史上 Discord 的这条 elif 是直接走 HTTP 路径的；这里
    # 通过显式调用注册表钩子来保留原有行为，使其保持不变。
    if platform == Platform.DISCORD:
        from gateway.platform_registry import platform_registry
        entry = platform_registry.get("discord")
        if entry is None or entry.standalone_sender_fn is None:
            return {"error": "Discord plugin not registered or missing standalone_sender_fn"}
        last_result = None
        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            result = await entry.standalone_sender_fn(
                pconfig,
                chat_id,
                chunk,
                thread_id=thread_id,
                media_files=media_files if is_last else [],
            )
            if isinstance(result, dict) and result.get("error"):
                return result
            last_result = result
        return last_result

    # --- Matrix：当存在媒体时使用原生适配器辅助函数 ---
    if platform == Platform.MATRIX and media_files:
        last_result = None
        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            result = await _send_matrix_via_adapter(
                pconfig,
                chat_id,
                chunk,
                media_files=media_files if is_last else [],
                thread_id=thread_id,
            )
            if isinstance(result, dict) and result.get("error"):
                return result
            last_result = result
        return last_result

    # --- Signal：通过 JSON-RPC 的 attachments 参数提供原生附件支持 ---
    if platform == Platform.SIGNAL and media_files:
        last_result = None
        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            result = await _send_signal(
                pconfig.extra,
                chat_id,
                chunk,
                media_files=media_files if is_last else [],
            )
            if isinstance(result, dict) and result.get("error"):
                return result
            last_result = result
        return last_result

    # --- Yuanbao：通过运行中的 gateway 适配器提供原生媒体附件支持 ---
    if platform == Platform.YUANBAO and media_files:
        last_result = None
        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            result = await _send_yuanbao(
                chat_id,
                chunk,
                media_files=media_files if is_last else None,
            )
            if isinstance(result, dict) and result.get("error"):
                return result
            last_result = result
        return last_result

    # --- Feishu：通过注册表的 standalone_sender_fn（plugins/platforms/feishu/
    # adapter.py::_standalone_send）提供原生媒体附件支持。#41112
    if platform == Platform.FEISHU and media_files:
        from gateway.platform_registry import platform_registry as _pr_feishu
        from hermes_cli.plugins import discover_plugins as _dp_feishu
        _dp_feishu()
        _feishu_entry = _pr_feishu.get("feishu")
        if _feishu_entry is None or _feishu_entry.standalone_sender_fn is None:
            return {"error": "Feishu plugin not registered or missing standalone_sender_fn"}
        last_result = None
        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            result = await _feishu_entry.standalone_sender_fn(
                pconfig,
                chat_id,
                chunk,
                media_files=media_files if is_last else None,
                thread_id=thread_id,
            )
            if isinstance(result, dict) and result.get("error"):
                return result
            last_result = result
        return last_result

    # --- 非媒体平台 ---
    if media_files and not message.strip():
        return {
            "error": (
                f"send_message MEDIA delivery is currently only supported for telegram, discord, matrix, weixin, signal, yuanbao and feishu; "
                f"target {platform.value} had only media attachments"
            )
        }
    warning = None
    if media_files:
        warning = (
            f"MEDIA attachments were omitted for {platform.value}; "
            "native send_message media delivery is currently only supported for telegram, discord, matrix, weixin, signal, yuanbao and feishu"
        )

    last_result = None
    for chunk in chunks:
        if platform == Platform.SLACK:
            # Slack 已迁移为内置插件（#41112）；投递通过注册表的
            # standalone_sender_fn 进行，它负责应用 mrkdwn 格式化，
            # 并经由 Slack Web API 发送。
            from gateway.platform_registry import platform_registry
            _slack_entry = platform_registry.get("slack")
            if _slack_entry is None or _slack_entry.standalone_sender_fn is None:
                result = {"error": "Slack plugin not registered or missing standalone_sender_fn"}
            else:
                result = await _slack_entry.standalone_sender_fn(
                    pconfig, chat_id, chunk, thread_id=thread_id
                )
        elif platform == Platform.WHATSAPP:
            result = await _registry_standalone_send("whatsapp", pconfig, chat_id, chunk, thread_id)
        elif platform == Platform.SIGNAL:
            result = await _send_signal(pconfig.extra, chat_id, chunk)
        elif platform == Platform.EMAIL:
            result = await _registry_standalone_send("email", pconfig, chat_id, chunk, thread_id)
        elif platform == Platform.SMS:
            result = await _registry_standalone_send("sms", pconfig, chat_id, chunk, thread_id)
        elif platform == Platform.MATRIX:
            result = await _registry_standalone_send("matrix", pconfig, chat_id, chunk, thread_id)
        elif platform == Platform.DINGTALK:
            result = await _registry_standalone_send("dingtalk", pconfig, chat_id, chunk, thread_id)
        elif platform == Platform.FEISHU:
            result = await _registry_standalone_send("feishu", pconfig, chat_id, chunk, thread_id)
        elif platform == Platform.WECOM:
            result = await _registry_standalone_send("wecom", pconfig, chat_id, chunk, thread_id)
        elif platform == Platform.BLUEBUBBLES:
            result = await _send_bluebubbles(pconfig.extra, chat_id, chunk)
        elif platform == Platform.QQBOT:
            result = await _send_qqbot(pconfig, chat_id, chunk)
        elif platform == Platform.YUANBAO:
            result = await _send_yuanbao(chat_id, chunk)
        else:
            # 插件平台：优先走 gateway 运行中的适配器；若不可用，
            # 则走插件的 standalone_sender_fn。
            result = await _send_via_adapter(
                platform,
                pconfig,
                chat_id,
                chunk,
                thread_id=thread_id,
                media_files=media_files,
                force_document=force_document,
            )

        if isinstance(result, dict) and result.get("error"):
            return result
        last_result = result

    if warning and isinstance(last_result, dict) and last_result.get("success"):
        warnings = list(last_result.get("warnings", []))
        warnings.append(warning)
        last_result["warnings"] = warnings
    return last_result


def _is_telegram_thread_not_found(error: Exception) -> bool:
    """检查某个 Telegram 错误是否属于「话题未找到」失败。

    与 gateway 适配器的 ``_is_thread_not_found_error`` 保持一致，用于
    独立的 ``_send_telegram`` 路径（issue #27012）。
    """
    return "thread not found" in str(error).lower()


async def _send_telegram(token, chat_id, message, media_files=None, thread_id=None, disable_link_previews=False, force_document=False):
    """通过 Telegram Bot API 发送（一次性，无需轮询）。

    应用 markdown→MarkdownV2 格式化（与 gateway 适配器一致），让粗体、
    链接、标题正确渲染。如果消息里已包含 HTML 标签，则改用
    ``parse_mode='HTML'`` 发送，跳过 MarkdownV2 转换。
    """
    try:
        from telegram import Bot
        from telegram.constants import ParseMode

        # 自动检测 HTML 标签 —— 若存在，则跳过 MarkdownV2，按 HTML 发送。
        # 受 github.com/ashaney 启发 —— PR #1568。
        _has_html = bool(re.search(r'<[a-zA-Z/][^>]*>', message))

        if _has_html:
            formatted = message
            send_parse_mode = ParseMode.HTML
        else:
            # 复用 gateway 适配器的 format_message 完成 markdown→MarkdownV2
            try:
                from plugins.platforms.telegram.adapter import TelegramAdapter
                _adapter = TelegramAdapter.__new__(TelegramAdapter)
                formatted = _adapter.format_message(message)
            except Exception:
                # 回退：若格式化不可用，则原样发送
                formatted = message
            send_parse_mode = ParseMode.MARKDOWN_V2

        # 遵循已配置的代理（config.yaml 里的 telegram.proxy_url，由
        # load_gateway_config 导出为 TELEGRAM_PROXY 环境变量）。否则，
        # 独立发送路径会绕过代理，在 api.telegram.org 被封锁的地区会
        # 超时。gateway 内的适配器在 gateway/platforms/telegram.py 里
        # 做的是同样的事。
        try:
            from gateway.platforms.base import resolve_proxy_url
            _tg_proxy = resolve_proxy_url("TELEGRAM_PROXY", target_hosts=["api.telegram.org"])
        except Exception:
            _tg_proxy = None
        if _tg_proxy:
            try:
                from telegram.request import HTTPXRequest
                logger.info("send_message: standalone Telegram send routed through proxy %s", _tg_proxy)
                bot = Bot(
                    token=token,
                    request=HTTPXRequest(proxy=_tg_proxy),
                    get_updates_request=HTTPXRequest(proxy=_tg_proxy),
                )
            except Exception as _proxy_err:
                logger.warning("send_message: failed to attach Telegram proxy (%s), falling back to direct connection", _proxy_err)
                bot = Bot(token=token)
        else:
            bot = Bot(token=token)
        int_chat_id = int(chat_id)
        media_files = media_files or []
        thread_kwargs = {}
        if thread_id is not None:
            # 复用 gateway 适配器的 General topic 映射：在 Telegram 论坛
            # 超级群里，General 话题在收到的更新中以 message_thread_id="1"
            # 表示，但 Bot API 的 sendMessage 会以「Message thread not found」
            # 拒绝 message_thread_id=1。适配器的辅助函数正是为此把「1」
            # 映射为 None；send_message 工具需要同样的映射，否则向论坛
            # 群的 General 话题发送时永远会报错（见 issue #22267）。
            try:
                from plugins.platforms.telegram.adapter import TelegramAdapter
                effective_thread_id = TelegramAdapter._message_thread_id_for_send(
                    str(thread_id)
                )
            except Exception:
                # 回退：当适配器导入失败（例如本 venv 缺少
                # python-telegram-bot）时的显式映射。
                effective_thread_id = (
                    None if str(thread_id) == "1" else int(thread_id)
                )
            if effective_thread_id is not None:
                thread_kwargs["message_thread_id"] = effective_thread_id
        # disable_web_page_preview 只对 send_message 有效，对
        # send_photo/send_video 等无效。把它单独放一份，避免媒体发送
        # 继承到一个非法参数（issue #27012）。
        text_kwargs = dict(thread_kwargs)
        if disable_link_previews:
            text_kwargs["disable_web_page_preview"] = True

        last_msg = None
        warnings = []

        if formatted.strip():
            try:
                last_msg = await _send_telegram_message_with_retry(
                    bot,
                    chat_id=int_chat_id, text=formatted,
                    parse_mode=send_parse_mode, **text_kwargs
                )
            except Exception as md_error:
                # 话题未找到 —— 去掉 message_thread_id 后重试，让消息仍能
                # 投递出去（与 gateway 适配器的回退行为一致，issue #27012）。
                if _is_telegram_thread_not_found(md_error) and thread_kwargs:
                    logger.warning(
                        "Thread %s not found in _send_telegram, retrying without message_thread_id",
                        thread_kwargs.get("message_thread_id"),
                    )
                    text_kwargs.pop("message_thread_id", None)
                    last_msg = await _send_telegram_message_with_retry(
                        bot,
                        chat_id=int_chat_id, text=formatted,
                        parse_mode=send_parse_mode, **text_kwargs
                    )
                elif "parse" in str(md_error).lower() or "markdown" in str(md_error).lower() or "html" in str(md_error).lower():
                    logger.warning(
                        "Parse mode %s failed in _send_telegram, falling back to plain text: %s",
                        send_parse_mode,
                        _sanitize_error_text(md_error),
                    )
                    if not _has_html:
                        try:
                            from plugins.platforms.telegram.adapter import _strip_mdv2
                            plain = _strip_mdv2(formatted)
                        except Exception:
                            plain = message
                    else:
                        plain = message
                    last_msg = await _send_telegram_message_with_retry(
                        bot,
                        chat_id=int_chat_id, text=plain,
                        parse_mode=None, **text_kwargs
                    )
                else:
                    raise

        for media_path, is_voice in media_files:
            if not os.path.exists(media_path):
                warning = f"Media file not found, skipping: {media_path}"
                logger.warning(warning)
                warnings.append(warning)
                continue

            ext = os.path.splitext(media_path)[1].lower()
            try:
                with open(media_path, "rb") as f:
                    media_kwargs = dict(thread_kwargs)
                    try:
                        if ext in _IMAGE_EXTS and not force_document:
                            last_msg = await bot.send_photo(
                                chat_id=int_chat_id, photo=f, **media_kwargs
                            )
                        elif ext in _VIDEO_EXTS:
                            last_msg = await bot.send_video(
                                chat_id=int_chat_id, video=f, **media_kwargs
                            )
                        elif ext in _VOICE_EXTS and is_voice:
                            last_msg = await bot.send_voice(
                                chat_id=int_chat_id, voice=f, **media_kwargs
                            )
                        elif ext in _TELEGRAM_SEND_AUDIO_EXTS:
                            last_msg = await bot.send_audio(
                                chat_id=int_chat_id, audio=f, **media_kwargs
                            )
                        else:
                            last_msg = await bot.send_document(
                                chat_id=int_chat_id, document=f, **media_kwargs
                            )
                    except Exception as media_err:
                        if _is_telegram_thread_not_found(media_err) and media_kwargs.get("message_thread_id"):
                            # 媒体发送时话题未找到 —— 去掉
                            # message_thread_id 后重试（issue #27012）。
                            logger.warning(
                                "Thread %s not found for media send, retrying without message_thread_id",
                                media_kwargs["message_thread_id"],
                            )
                            # 由于第一次尝试已读取过文件，这里重新 seek 回开头
                            f.seek(0)
                            media_kwargs.pop("message_thread_id", None)
                            if ext in _IMAGE_EXTS and not force_document:
                                last_msg = await bot.send_photo(
                                    chat_id=int_chat_id, photo=f, **media_kwargs
                                )
                            elif ext in _VIDEO_EXTS:
                                last_msg = await bot.send_video(
                                    chat_id=int_chat_id, video=f, **media_kwargs
                                )
                            elif ext in _VOICE_EXTS and is_voice:
                                last_msg = await bot.send_voice(
                                    chat_id=int_chat_id, voice=f, **media_kwargs
                                )
                            elif ext in _TELEGRAM_SEND_AUDIO_EXTS:
                                last_msg = await bot.send_audio(
                                    chat_id=int_chat_id, audio=f, **media_kwargs
                                )
                            else:
                                last_msg = await bot.send_document(
                                    chat_id=int_chat_id, document=f, **media_kwargs
                                )
                        else:
                            raise
            except Exception as e:
                warning = _sanitize_error_text(f"Failed to send media {media_path}: {e}")
                logger.error(warning)
                warnings.append(warning)

        if last_msg is None:
            error = "No deliverable text or media remained after processing MEDIA tags"
            if warnings:
                return {"error": error, "warnings": warnings}
            return {"error": error}

        result = {
            "success": True,
            "platform": "telegram",
            "chat_id": chat_id,
            "message_id": str(last_msg.message_id),
        }
        if warnings:
            result["warnings"] = warnings
        return result
    except ImportError:
        return {"error": "python-telegram-bot not installed. Run: pip install python-telegram-bot"}
    except Exception as e:
        return _error(f"Telegram send failed: {e}")


# _send_slack 已迁移到 slack 插件中，成为 _standalone_send
#（plugins/platforms/slack/adapter.py），通过 standalone_sender_fn 接入。#41112.


async def _registry_standalone_send(platform_name, pconfig, chat_id, message, thread_id=None):
    """通过已迁移平台插件的 standalone_sender_fn（注册表钩子）派发一次
    一次性发送。适用于适配器已从 gateway/platforms/ 迁出到
    plugins/platforms/<name>/ 的平台（#41112）：原先内联的
    ``_send_<platform>`` 辅助函数现在以 ``_standalone_send`` 的形式
    存在于插件中，并经由平台注册表被调用。
    """
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import discover_plugins
    discover_plugins()  # 幂等 —— 确保条目已注册
    entry = platform_registry.get(platform_name)
    if entry is None or entry.standalone_sender_fn is None:
        return {"error": f"{platform_name} plugin not registered or missing standalone_sender_fn"}
    return await entry.standalone_sender_fn(pconfig, chat_id, message, thread_id=thread_id)


# _send_whatsapp 已迁移到 plugins/platforms/whatsapp/adapter.py::_standalone_send，
# 通过 standalone_sender_fn 接入，并经由 _registry_standalone_send 调用。#41112.


async def _send_signal(extra, chat_id, message, media_files=None):
    """通过 signal-cli 的 JSON-RPC API 发送。

    既支持纯文本，也支持带附件（图片/音频/文档）的文本。
    多附件发送会被切成每批 SIGNAL_MAX_ATTACHMENTS_PER_MSG 个的批次，
    并由进程级的 SignalAttachmentScheduler 计量节流 —— 与 gateway 适配器
    用的是同一个桶，因此本工具的发送和入站驱动的回复共享限流状态。
    """
    try:
        import httpx
    except ImportError:
        return {"error": "httpx not installed"}

    from gateway.platforms.signal_rate_limit import (
        SIGNAL_BATCH_PACING_NOTICE_THRESHOLD,
        SIGNAL_MAX_ATTACHMENTS_PER_MSG,
        SIGNAL_RATE_LIMIT_MAX_ATTEMPTS,
        _extract_retry_after_seconds,
        _format_wait,
        _is_signal_rate_limit_error,
        _signal_send_timeout,
        get_scheduler,
    )
    from gateway.platforms.signal_format import markdown_to_signal

    try:
        http_url = extra.get("http_url", "http://127.0.0.1:8080").rstrip("/")
        account = extra.get("account", "")
        if not account:
            return {"error": "Signal account not configured"}

        valid_media = media_files or []
        attachment_paths = []
        for media_path, _is_voice in valid_media:
            if os.path.exists(media_path):
                attachment_paths.append(media_path)
            else:
                logger.warning("Signal media file not found, skipping: %s", media_path)

        # 对附件分块。没有附件时仍然发送一批（仅文本）。有附件时，
        # 文本搭在第 0 批，这样说明文字就不会在每个分块里重复出现。
        if attachment_paths:
            att_batches = [
                attachment_paths[i:i + SIGNAL_MAX_ATTACHMENTS_PER_MSG]
                for i in range(0, len(attachment_paths), SIGNAL_MAX_ATTACHMENTS_PER_MSG)
            ]
        else:
            att_batches = [[]]

        plain_text, text_styles = markdown_to_signal(message)

        async def _post(batch_attachments, batch_message):
            params = {"account": account, "message": batch_message}
            if batch_message and text_styles:
                if len(text_styles) == 1:
                    params["textStyle"] = text_styles[0]
                else:
                    params["textStyles"] = text_styles
            if chat_id.startswith("group:"):
                params["groupId"] = chat_id[6:]
            else:
                params["recipient"] = [chat_id]
            if batch_attachments:
                params["attachments"] = batch_attachments

            payload = {
                "jsonrpc": "2.0",
                "method": "send",
                "params": params,
                "id": f"send_{int(time.time() * 1000)}",
            }
            timeout = _signal_send_timeout(len(batch_attachments) if batch_attachments else 0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(f"{http_url}/api/v1/rpc", json=payload)
                resp.raise_for_status()
                return resp.json()

        async def _send_inline_notice(text: str) -> None:
            """尽力而为的一次性 RPC，用于给用户发送节流提示。"""
            notice_params = {"account": account, "message": text}
            if chat_id.startswith("group:"):
                notice_params["groupId"] = chat_id[6:]
            else:
                notice_params["recipient"] = [chat_id]
            try:
                async with httpx.AsyncClient(timeout=30.0) as _client:
                    await _client.post(
                        f"{http_url}/api/v1/rpc",
                        json={
                            "jsonrpc": "2.0",
                            "method": "send",
                            "params": notice_params,
                            "id": f"notice_{int(time.time() * 1000)}",
                        },
                    )
            except Exception as _e:
                logger.warning("Signal: inline notice failed: %s", _e)

        scheduler = get_scheduler()
        logger.info(
            "send_message Signal: scheduler state=%s, %d attachment(s) in %d batch(es)",
            scheduler.state(), len(attachment_paths), len(att_batches),
        )
        failed_batches: list[int] = []
        for idx, att_batch in enumerate(att_batches):
            n = len(att_batch)
            if n > 0:
                estimated = scheduler.estimate_wait(n)
                if estimated >= SIGNAL_BATCH_PACING_NOTICE_THRESHOLD:
                    await _send_inline_notice(
                        f"(More images coming — pausing ~{_format_wait(estimated)} "
                        f"for Signal rate limit, batch {idx + 1}/{len(att_batches)}.)"
                    )

            batch_message = plain_text if idx == 0 else ""

            for attempt in range(1, SIGNAL_RATE_LIMIT_MAX_ATTEMPTS + 1):
                try:
                    await scheduler.acquire(n)
                    _rpc_t0 = time.monotonic()
                    data = await _post(att_batch, batch_message)
                    _rpc_duration = time.monotonic() - _rpc_t0
                    if "error" not in data:
                        await scheduler.report_rpc_duration(_rpc_duration, n)
                        break

                    err = data["error"]

                    if not _is_signal_rate_limit_error(err):
                        return _error(f"Signal RPC error on batch {idx + 1}/{len(att_batches)}: {err}")

                    server_retry_after = _extract_retry_after_seconds(err)
                    scheduler.feedback(server_retry_after, n)

                    if attempt >= SIGNAL_RATE_LIMIT_MAX_ATTEMPTS:
                        failed_batches.append(idx + 1)
                        logger.error(
                            "Signal: rate-limit retries exhausted on batch %d/%d "
                            "(%d attachments lost, server retry_after=%s)",
                            idx + 1, len(att_batches), n,
                            f"{server_retry_after:.0f}s" if server_retry_after else "unknown",
                        )
                        break
                    logger.warning(
                        "Signal: rate-limited on batch %d/%d "
                        "(attempt %d/%d, server retry_after=%s); "
                        "scheduler will pace the retry",
                        idx + 1, len(att_batches),
                        attempt, SIGNAL_RATE_LIMIT_MAX_ATTEMPTS,
                        f"{server_retry_after:.0f}s" if server_retry_after else "unknown",
                    )
                except Exception as e:
                    if attempt >= SIGNAL_RATE_LIMIT_MAX_ATTEMPTS:
                        failed_batches.append(idx + 1)
                        logger.error(
                            "Signal: send error on batch %d/%d after %d attempts: %s",
                            idx + 1, len(att_batches), attempt, str(e)
                        )
                        break
                    logger.warning(
                        "Signal: transient error on batch %d/%d (attempt %d/%d): %s; will retry",
                        idx + 1, len(att_batches), attempt, SIGNAL_RATE_LIMIT_MAX_ATTEMPTS, str(e)
                    )

        warnings = []
        if len(attachment_paths) < len(valid_media):
            warnings.append("Some media files were skipped (not found on disk)")
        if failed_batches:
            warnings.append(
                f"Signal rate-limited {len(failed_batches)} batch(es) "
                f"(#{', #'.join(str(b) for b in failed_batches)})"
            )

        if failed_batches and len(failed_batches) == len(att_batches):
            return _error(
                f"Signal: every batch ({len(att_batches)}) hit rate limit; "
                f"no attachments delivered"
            )

        result = {"success": True, "platform": "signal", "chat_id": _display_chat_id("signal", chat_id)}
        if warnings:
            result["warnings"] = warnings
        return result
    except Exception as e:
        return _error(f"Signal send failed: {e}")


# _send_email 已迁移到 plugins/platforms/email/adapter.py::_standalone_send；
# _send_sms 已迁移到 plugins/platforms/sms/adapter.py::_standalone_send。
# 二者都通过 standalone_sender_fn 接入，经由 _registry_standalone_send 调用。#41112.


# _send_matrix 已迁移到 plugins/platforms/matrix/adapter.py::_standalone_send，
# 通过 standalone_sender_fn 接入，经由 _registry_standalone_send 调用。#41112.
#（下面的 _send_matrix_via_adapter 保留 —— 它是原生媒体上传路径。）


async def _send_matrix_via_adapter(pconfig, chat_id, message, media_files=None, thread_id=None):
    """经由 Matrix 适配器发送，以保留原生的 Matrix 媒体上传。"""
    try:
        from plugins.platforms.matrix.adapter import MatrixAdapter
    except ImportError:
        return {"error": "Matrix dependencies not installed. Run: pip install 'mautrix[encryption]'"}

    media_files = media_files or []

    try:
        adapter = MatrixAdapter(pconfig)
        connected = await adapter.connect()
        if not connected:
            return _error("Matrix connect failed")

        metadata = {"thread_id": thread_id} if thread_id else None
        last_result = None

        if message.strip():
            last_result = await adapter.send(chat_id, message, metadata=metadata)
            if not last_result.success:
                return _error(f"Matrix send failed: {last_result.error}")

        for media_path, is_voice in media_files:
            if not os.path.exists(media_path):
                return _error(f"Media file not found: {media_path}")

            ext = os.path.splitext(media_path)[1].lower()
            if ext in _IMAGE_EXTS:
                last_result = await adapter.send_image_file(chat_id, media_path, metadata=metadata)
            elif ext in _VIDEO_EXTS:
                last_result = await adapter.send_video(chat_id, media_path, metadata=metadata)
            elif ext in _VOICE_EXTS and is_voice:
                last_result = await adapter.send_voice(chat_id, media_path, metadata=metadata)
            elif ext in _AUDIO_EXTS:
                last_result = await adapter.send_voice(chat_id, media_path, metadata=metadata)
            else:
                last_result = await adapter.send_document(chat_id, media_path, metadata=metadata)

            if not last_result.success:
                return _error(f"Matrix media send failed: {last_result.error}")

        if last_result is None:
            return {"error": "No deliverable text or media remained after processing MEDIA tags"}

        return {
            "success": True,
            "platform": "matrix",
            "chat_id": chat_id,
            "message_id": last_result.message_id,
        }
    except Exception as e:
        return _error(f"Matrix send failed: {e}")
    finally:
        try:
            await adapter.disconnect()
        except Exception:
            pass


# _send_dingtalk 已迁移到 plugins/platforms/dingtalk/adapter.py::_standalone_send，
# 通过 standalone_sender_fn 接入，经由 _registry_standalone_send 调用。#41112.


# _send_wecom 已迁移到 plugins/platforms/wecom/adapter.py::_standalone_send，
# 通过 standalone_sender_fn 接入，经由 _registry_standalone_send 调用。#41112.


async def _send_weixin(pconfig, chat_id, message, media_files=None):
    """使用原生适配器辅助函数，通过 Weixin iLink 发送。"""
    try:
        from gateway.platforms.weixin import check_weixin_requirements, send_weixin_direct
        if not check_weixin_requirements():
            return {"error": "Weixin requirements not met. Need aiohttp + cryptography."}
    except ImportError:
        return {"error": "Weixin adapter not available."}

    try:
        return await send_weixin_direct(
            extra=pconfig.extra,
            token=pconfig.token,
            chat_id=chat_id,
            message=message,
            media_files=media_files,
        )
    except Exception as e:
        return _error(f"Weixin send failed: {e}")


async def _send_bluebubbles(extra, chat_id, message):
    """通过 BlueBubbles iMessage 服务器，使用适配器的 REST API 发送。"""
    try:
        from gateway.platforms.bluebubbles import BlueBubblesAdapter, check_bluebubbles_requirements
        if not check_bluebubbles_requirements():
            return {"error": "BlueBubbles requirements not met (need aiohttp + httpx)."}
    except ImportError:
        return {"error": "BlueBubbles adapter not available."}

    try:
        from gateway.config import PlatformConfig
        pconfig = PlatformConfig(extra=extra)
        adapter = BlueBubblesAdapter(pconfig)
        connected = await adapter.connect()
        if not connected:
            return _error("BlueBubbles: failed to connect to server")
        try:
            result = await adapter.send(chat_id, message)
            if not result.success:
                return _error(f"BlueBubbles send failed: {result.error}")
            return {"success": True, "platform": "bluebubbles", "chat_id": chat_id, "message_id": result.message_id}
        finally:
            await adapter.disconnect()
    except Exception as e:
        return _error(f"BlueBubbles send failed: {e}")


# _send_feishu 已迁移到 plugins/platforms/feishu/adapter.py::_standalone_send，
# 通过 standalone_sender_fn 接入，经由 _registry_standalone_send
#（以及上面的 feishu 媒体分支）调用。#41112.


def _check_send_message():
    """以「gateway 是否运行」作为 send_message 的前置条件（在即时通讯平台上
    始终可用）。

    对 kanban worker 也放行 —— 调度器会给每个派生的 worker 设置
    ``HERMES_KANBAN_TASK``，但这些 worker 运行在指派人 profile 的
    ``HERMES_HOME`` 下，那里没有 ``gateway.pid``，于是「gateway 是否在运行」
    的检查会失败，哪怕父 gateway 实际是存活的。遵守这个环境变量，可以让
    worker 调用 ``send_message`` 把富内容直接投递到发起聊天（与
    ``kanban_complete`` 配合，后者负责简短的 notifier 摘要），这也是任何
    需要回复比 kanban notifier 那条约 200 字符首行截断更多内容的 worker
    所遵循的标准模式。
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True
    from gateway.session_context import get_session_env
    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    if platform and platform != "local":
        return True
    try:
        from gateway.status import is_gateway_running
        return is_gateway_running()
    except Exception:
        return False


async def _send_qqbot(pconfig, chat_id, message):
    """通过 REST API 直接经由 QQBot 发送（无需 WebSocket）。

    使用 QQ Bot 开放平台的 REST 端点获取 access token 并发送消息。
    通过尝试相应端点，支持频道（guild channel）、C2C（私聊）以及
    群聊。
    """
    try:
        import httpx
    except ImportError:
        return _error("QQBot direct send requires httpx. Run: pip install httpx")

    extra = pconfig.extra or {}
    appid = extra.get("app_id") or os.getenv("QQ_APP_ID", "")
    secret = (pconfig.token or extra.get("client_secret")
              or os.getenv("QQ_CLIENT_SECRET", ""))
    if not appid or not secret:
        return _error("QQBot: QQ_APP_ID / QQ_CLIENT_SECRET not configured.")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # 第 1 步：获取 access token
            token_resp = await client.post(
                "https://bots.qq.com/app/getAppAccessToken",
                json={"appId": str(appid), "clientSecret": str(secret)},
            )
            if token_resp.status_code != 200:
                return _error(f"QQBot token request failed: {token_resp.status_code}")
            token_data = token_resp.json()
            access_token = token_data.get("access_token")
            if not access_token:
                return _error(f"QQBot: no access_token in response")

            # 第 2 步：通过 REST 发送消息
            # QQ Bot API 对频道、C2C、群组有各自的端点。
            # 按顺序尝试：先频道，失败再回退到 C2C。
            headers = {
                "Authorization": f"QQBot {access_token}",
                "Content-Type": "application/json",
            }
            payload = {"content": message[:4000], "msg_type": 0}

            # 先尝试频道端点（适用于 guild 频道）
            url = f"https://api.sgroup.qq.com/channels/{chat_id}/messages"
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code in {200, 201}:
                data = resp.json()
                return {"success": True, "platform": "qqbot", "chat_id": chat_id,
                        "message_id": data.get("id")}

            # 若频道端点失败（多半是「频道不存在」），尝试 C2C 端点
            url_c2c = f"https://api.sgroup.qq.com/v2/users/{chat_id}/messages"
            resp_c2c = await client.post(url_c2c, json=payload, headers=headers)
            if resp_c2c.status_code in {200, 201}:
                data = resp_c2c.json()
                return {"success": True, "platform": "qqbot", "chat_id": chat_id,
                        "message_id": data.get("id")}

            # 若 C2C 也失败，尝试群组端点
            url_group = f"https://api.sgroup.qq.com/v2/groups/{chat_id}/messages"
            resp_group = await client.post(url_group, json=payload, headers=headers)
            if resp_group.status_code in {200, 201}:
                data = resp_group.json()
                return {"success": True, "platform": "qqbot", "chat_id": chat_id,
                        "message_id": data.get("id")}

            # 所有端点都失败 —— 返回信息量最大的错误
            return _error(f"QQBot send failed: channel={resp.status_code} c2c={resp_c2c.status_code} group={resp_group.status_code}")
    except Exception as e:
        return _error(f"QQBot send failed: {e}")


async def _send_yuanbao(chat_id, message, media_files=None):
    """通过运行中的 gateway 适配器的 WebSocket 连接，经由 Yuanbao 发送。

    Yuanbao 使用持久化 WebSocket —— 与基于 HTTP 的平台不同，我们无法
    创建一次性的临时客户端。我们从适配器模块本身（``get_active_adapter``）
    获取运行中的单例。

    chat_id 格式：
      - 群组："group:<group_code>"
      - 私信："direct:<account_id>" 或仅 "<account_id>"
    """
    try:
        from gateway.platforms.yuanbao import get_active_adapter, send_yuanbao_direct
    except ImportError:
        return _error("Yuanbao adapter module not available.")

    adapter = get_active_adapter()
    if adapter is None:
        return _error(
            "Yuanbao adapter is not running. "
            "Start the gateway with yuanbao platform enabled first."
        )

    try:
        return await send_yuanbao_direct(adapter, chat_id, message, media_files=media_files)
    except Exception as e:
        return _error(f"Yuanbao send failed: {e}")


# --- 注册表 ---
from tools.registry import tool_error

# 注意：``send_message`` 被刻意「不」注册为 agent 可调用的模型工具。
# agent 不应自行决定去发起跨平台消息或表态。本模块里的发送引擎
#（``_send_to_platform``、``_send_via_adapter``、``_parse_target_ref``，
# 以及各平台的 ``_send_*`` 辅助函数）仍是以下调用方共享的传输层：
#   - cron 投递（cron/scheduler.py）
#   - ``hermes send`` CLI 命令（hermes_cli/send_cmd.py）
#   - gateway kanban notifier（由 dashboard 开关控制，不在 agent 控制范围内）
#   - 独立的 MCP server（mcp_serve.py），这是一个可选启用面
# 这些调用方都是直接导入辅助函数；它们都不需要这里的注册表条目。
