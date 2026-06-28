"""
gateway 的会话管理。

负责：
- 会话上下文跟踪（消息来自哪里）
- 会话存储（对话持久化到磁盘）
- 重置策略评估（何时重新开始）
- 动态系统提示注入（agent 了解自己的上下文）
"""

import hashlib
import logging
import os
import json
import threading
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


def _now() -> datetime:
    """返回当前本地时间。"""
    return datetime.now()


# ---------------------------------------------------------------------------
# PII 脱敏辅助函数
# ---------------------------------------------------------------------------

def _hash_id(value: str) -> str:
    """对标识符生成确定性的 12 字符十六进制哈希。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _hash_sender_id(value: str) -> str:
    """把发送者 ID 哈希为 ``user_<12hex>`` 形式。"""
    return f"user_{_hash_id(value)}"


def _hash_chat_id(value: str) -> str:
    """对 chat ID 的数字部分做哈希，保留平台前缀。

    ``telegram:12345`` → ``telegram:<hash>``
    ``12345``          → ``<hash>``
    """
    colon = value.find(":")
    if colon > 0:
        prefix = value[:colon]
        return f"{prefix}:{_hash_id(value[colon + 1:])}"
    return _hash_id(value)


from .config import (
    Platform,
    GatewayConfig,
    SessionResetPolicy,  # noqa: F401 — 通过 gateway/__init__.py 重新导出
    HomeChannel,
)
from .whatsapp_identity import (
    canonical_whatsapp_identifier,
    normalize_whatsapp_identifier,  # noqa: F401 — 为 gateway.session 的调用方重新导出
)
from utils import atomic_replace

# session key/id 会在下游进入文件系统路径（例如 hermes_state 中的
# ``sessions_dir / f"{session_id}.json"``，agent_runtime_helpers 中的请求 dump 文件名）。
# 任何可能作为路径逃逸出 sessions 目录的值都必须在入口边界处被拒绝。
# 拒绝项：父目录遍历（``..``）、任意位置出现的路径分隔符（``/`` 或 ``\``，这样非
# 开头的 Windows 分隔符也无法漏过）、以及开头的 Windows 盘符（``C:``）。合法的
# session key 是冒号分隔的多段 id（``agent:main:<platform>:...``），从不包含这些，
# 因此实践中不会出现误报。
def _is_path_unsafe(value: object) -> bool:
    """如果 ``value`` 可能遍历到 sessions 目录之外，则返回 True。"""
    if not value:
        return False
    s = str(value)
    if ".." in s or "/" in s or "\\" in s:
        return True
    # 开头的 Windows 盘符路径，例如 "C:\..." 或 "d:/..."。不带后续分隔符的裸 "x:"
    # 不是可用的绝对路径，分隔符形式在上面已被捕获 —— 但保留一个对盘符前缀的显式
    # 守卫，以防某个分隔符被规范化掉。
    return len(s) >= 2 and s[0].isalpha() and s[1] == ":"


@dataclass
class SessionSource:
    """
    描述一条消息的来源。

    此信息用于：
    1. 把响应路由回正确位置
    2. 把上下文注入系统提示
    3. 跟踪 cron job 投递的来源
    """
    platform: Platform
    chat_id: str
    chat_name: Optional[str] = None
    chat_type: str = "dm"  # "dm"、"group"、"channel"、"thread"
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    thread_id: Optional[str] = None  # 用于 forum topic、Discord thread 等
    chat_topic: Optional[str] = None  # 频道 topic/描述（Discord、Slack）
    user_id_alt: Optional[str] = None  # 平台相关的稳定备用 ID（Signal UUID、Feishu union_id）
    chat_id_alt: Optional[str] = None  # Signal 群组内部 ID
    is_bot: bool = False  # 当消息作者是 bot/webhook 时为 True（Discord）
    guild_id: Optional[str] = None  # Discord guild / Slack workspace / Matrix server 作用域
    parent_chat_id: Optional[str] = None  # 当 chat_id 指代一个 thread 时的父频道
    message_id: Optional[str] = None  # 触发消息的 ID（用于置顶/回复/响应）
    role_authorized: bool = False  # 当 adapter 通过角色（而非用户 ID）授予访问时为 True
    # 在多路复用 gateway 中，本入站消息被路由到的 profile（来自 /p/<profile>/ URL
    # 前缀，或按凭据的 adapter 归属）。None => gateway 的活动/默认 profile。同时驱动
    # session-key 命名空间和 per-turn 的 config/credential 作用域。
    profile: Optional[str] = None

    # 内部、对网络不可见的信任信号：当本事件是通过按实例认证的 relay WebSocket
    #（Team Gateway connector）投递到 gateway 时为 True。connector 在投递之前，先用
    # 按实例的密钥认证 gateway 的 socket，并解析仅属主的作者绑定，因此经 relay 投递
    # 的事件已经作为本实例绑定的用户被授权。``platform`` 携带的是*底层*平台（例如
    # ``discord``），用于 session-keying/egress，而不是 ``relay`` —— 因此 authz 必须以
    # 此 flag 作为上游信任决策的依据，而非 ``platform``。由 relay 传输在本地设置
    #（``ws_transport._event_from_wire``）；刻意从 ``to_dict``/``from_dict`` 中排除，
    # 使对端永远无法在网络中伪造它，也无法从持久化中恢复它。
    delivered_via_upstream_relay: bool = False

    @property
    def description(self) -> str:
        """来源的人类可读描述。"""
        if self.platform == Platform.LOCAL:
            return "CLI terminal"
        
        parts = []
        if self.chat_type == "dm":
            parts.append(f"DM with {self.user_name or self.user_id or 'user'}")
        elif self.chat_type == "group":
            parts.append(f"group: {self.chat_name or self.chat_id}")
        elif self.chat_type == "channel":
            parts.append(f"channel: {self.chat_name or self.chat_id}")
        else:
            parts.append(self.chat_name or self.chat_id)
        
        if self.thread_id:
            parts.append(f"thread: {self.thread_id}")
        
        return ", ".join(parts)
    
    def to_dict(self) -> Dict[str, Any]:
        d = {
            "platform": self.platform.value,
            "chat_id": self.chat_id,
            "chat_name": self.chat_name,
            "chat_type": self.chat_type,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "thread_id": self.thread_id,
            "chat_topic": self.chat_topic,
        }
        if self.user_id_alt:
            d["user_id_alt"] = self.user_id_alt
        if self.chat_id_alt:
            d["chat_id_alt"] = self.chat_id_alt
        if self.guild_id:
            d["guild_id"] = self.guild_id
        if self.parent_chat_id:
            d["parent_chat_id"] = self.parent_chat_id
        if self.message_id:
            d["message_id"] = self.message_id
        if self.profile:
            d["profile"] = self.profile
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionSource":
        return cls(
            platform=Platform(data["platform"]),
            chat_id=str(data["chat_id"]),
            chat_name=data.get("chat_name"),
            chat_type=data.get("chat_type", "dm"),
            user_id=data.get("user_id"),
            user_name=data.get("user_name"),
            thread_id=data.get("thread_id"),
            chat_topic=data.get("chat_topic"),
            user_id_alt=data.get("user_id_alt"),
            chat_id_alt=data.get("chat_id_alt"),
            guild_id=data.get("guild_id"),
            parent_chat_id=data.get("parent_chat_id"),
            message_id=data.get("message_id"),
            profile=data.get("profile"),
        )
    


@dataclass
class SessionContext:
    """
    一个 session 的完整上下文，用于动态系统提示注入。

    agent 接收此信息以了解：
    - 消息来自哪里
    - 有哪些可用平台
    - 它可以把计划任务的输出发到哪里
    """
    source: SessionSource
    connected_platforms: List[Platform]
    home_channels: Dict[Platform, HomeChannel]
    shared_multi_user_session: bool = False

    # 会话元数据
    session_key: str = ""
    session_id: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "connected_platforms": [p.value for p in self.connected_platforms],
            "home_channels": {
                p.value: hc.to_dict() for p, hc in self.home_channels.items()
            },
            "shared_multi_user_session": self.shared_multi_user_session,
            "session_key": self.session_key,
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


_PII_SAFE_PLATFORMS = frozenset({
    Platform.WHATSAPP,
    Platform.SIGNAL,
    Platform.TELEGRAM,
    Platform.BLUEBUBBLES,
})
"""用户 ID 可以安全脱敏的平台（没有要求原始 ID 的消息内 @ 提及系统）。Discord 被排除，
因为提及使用 ``<@user_id>``，LLM 需要真实 ID 才能 @ 用户。"""


def _discord_tools_loaded() -> bool:
    """当 agent 本次会话确实拥有 Discord 工具时返回 True。

    需要同时满足两个条件：
      1. 通过 `hermes tools` 为 Discord 平台启用了 `discord` 或 `discord_admin`
         工具集（可选启用，默认关闭）。
      2. `DISCORD_BOT_TOKEN` 已设置 —— 工具的 `check_fn` 在注册时以此做门控，
         因此仅在 config 中启用工具集而未配置 token 是不够的。

    任何错误时都返回 False（安全默认值 —— 保留过时 API 的免责声明），以免错误
    的 config 静默地承诺 agent 并不具备的工具。
    """
    if not (os.environ.get("DISCORD_BOT_TOKEN") or "").strip():
        return False
    try:
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools
        cfg = load_config()
        enabled = _get_platform_tools(cfg, "discord", include_default_mcp_servers=False)
        return "discord" in enabled or "discord_admin" in enabled
    except Exception:
        return False


def build_session_context_prompt(
    context: SessionContext,
    *,
    redact_pii: bool = False,
) -> str:
    """
    构建动态系统提示部分，告诉 agent 它的上下文。

    这会被注入到系统提示中，使 agent 知道：
    - 消息来自哪里
    - 连接了哪些平台
    - 它可以把计划任务输出发到哪里

    当 *redact_pii* 为 True **且** 来源平台属于 ``_PII_SAFE_PLATFORMS`` 时，电话
    号码会被剥离，user/chat ID 在发送给 LLM 之前会被替换为确定性哈希。像 Discord
    这样的平台被排除，因为提及需要真实 ID。路由仍使用原始值（它们保留在
    SessionSource 中）。
    """
    # 仅在不需 ID 用于提及的平台上应用脱敏。
    # 同时检查硬编码集合（内置）和插件注册表。
    _is_pii_safe = context.source.platform in _PII_SAFE_PLATFORMS
    if not _is_pii_safe:
        try:
            from gateway.platform_registry import platform_registry
            entry = platform_registry.get(context.source.platform.value)
            if entry and entry.pii_safe:
                _is_pii_safe = True
        except Exception:
            pass
    redact_pii = redact_pii and _is_pii_safe
    lines = [
        "## Current Session Context",
        "",
    ]

    # 来源信息
    platform_name = context.source.platform.value.title()
    if context.source.platform == Platform.LOCAL:
        lines.append(f"**Source:** {platform_name} (the machine running this agent)")
    else:
        # 构建一个尊重 PII 脱敏的描述
        src = context.source
        if redact_pii:
            # 构建一个不含原始 ID 的安全描述
            _uname = src.user_name or (
                _hash_sender_id(src.user_id) if src.user_id else "user"
            )
            _cname = src.chat_name or _hash_chat_id(src.chat_id)
            if src.chat_type == "dm":
                desc = f"DM with {_uname}"
            elif src.chat_type == "group":
                desc = f"group: {_cname}"
            elif src.chat_type == "channel":
                desc = f"channel: {_cname}"
            else:
                desc = _cname
        else:
            desc = src.description
        lines.append(f"**Source:** {platform_name} ({desc})")

    # 频道 topic（如果可用 —— 提供关于频道用途的上下文）
    if context.source.chat_topic:
        lines.append(f"**Channel Topic:** {context.source.chat_topic}")

    if context.source.platform == Platform.MATRIX:
        src = context.source
        room_name = src.chat_name or src.chat_id
        room_id = _hash_chat_id(src.chat_id) if redact_pii else src.chat_id
        lines.append("")
        lines.append(f"**Matrix Room:** {room_name}")
        lines.append(f"**Matrix Room ID:** {room_id}")
        if src.thread_id:
            thread_id = _hash_chat_id(src.thread_id) if redact_pii else src.thread_id
            lines.append(f"**Matrix Thread:** {thread_id}")
        lines.append(
            "**Matrix room boundary:** Treat this turn as scoped to the current "
            "Matrix room/thread only. Do not assume unresolved references are "
            "about other Matrix rooms or projects unless the user explicitly says so."
        )

    # 用户身份。
    # 在共享多用户会话中（共享 thread，或 group_sessions_per_user=False 时的共享
    # 非 thread 群组），多个用户参与同一段对话。不要在系统提示中固定单个用户名 ——
    # 它每个 turn 都会变，会让 prompt cache 失效。改为标注这是一个多用户会话；每个
    # 用户消息的发送者名字由 gateway 加在前缀上。
    if context.shared_multi_user_session:
        session_label = "Multi-user thread" if context.source.thread_id else "Multi-user session"
        lines.append(
            f"**Session type:** {session_label} — messages are prefixed "
            "with [sender name]. Multiple users may participate."
        )
    elif context.source.user_name:
        lines.append(f"**User:** {context.source.user_name}")
    elif context.source.user_id:
        uid = context.source.user_id
        if redact_pii:
            uid = _hash_sender_id(uid)
        lines.append(f"**User ID:** {uid}")

    # 平台相关的行为说明
    if context.source.platform == Platform.SLACK:
        lines.append("")
        lines.append(
            "**Platform notes:** You are running inside Slack. "
            "You do NOT have access to Slack-specific APIs — you cannot search "
            "channel history, pin/unpin messages, manage channels, or list users. "
            "Do not promise to perform these actions. The gateway may inline the "
            "current message's Slack block/attachment payload when available, but "
            "you still cannot call Slack APIs yourself."
        )
    elif context.source.platform == Platform.DISCORD:
        # 仅当 agent 本次会话确实已加载 Discord 工具时才注入 Discord IDs 块 —— 即
        # 用户通过 `hermes tools` 启用了 `discord` / `discord_admin` 且配置了 bot
        # token。否则保留过时 API 的免责声明，以免承诺 agent 并不具备的工具。
        if _discord_tools_loaded():
            src = context.source
            id_lines = ["", "**Discord IDs (for the `discord` / `discord_admin` tools):**"]
            if src.guild_id:
                id_lines.append(f"  - Guild: `{src.guild_id}`")
            if src.thread_id and src.parent_chat_id:
                id_lines.append(f"  - Parent channel: `{src.parent_chat_id}`")
                id_lines.append(f"  - Thread: `{src.thread_id}` (use as `channel_id` for fetch_messages etc.)")
            else:
                id_lines.append(f"  - Channel: `{src.chat_id}`")
            if src.message_id:
                id_lines.append(f"  - Triggering message: `{src.message_id}`")
            lines.extend(id_lines)
        else:
            lines.append("")
            lines.append(
                "**Platform notes:** You are running inside Discord. "
                "You do NOT have access to Discord-specific APIs — you cannot search "
                "channel history, pin messages, manage roles, or list server members. "
                "Do not promise to perform these actions. If the user asks, explain "
                "that you can only read messages sent directly to you and respond."
            )
    elif context.source.platform == Platform.BLUEBUBBLES:
        lines.append("")
        lines.append(
            "**Platform notes:** You are responding via iMessage. "
            "Keep responses short and conversational — think texts, not essays. "
            "Structure longer replies as separate short thoughts, each separated "
            "by a blank line (double newline). Each block between blank lines "
            "will be delivered as its own iMessage bubble, so write accordingly: "
            "one idea per bubble, 1–3 sentences each. "
            "If the user needs a detailed answer, give the short version first "
            "and offer to elaborate."
        )
    elif context.source.platform == Platform.YUANBAO:
        lines.append("")
        lines.append(
            "**Platform notes:** You are running inside Yuanbao. "
            "To send a private (DM) message to a user in the current group, "
            "use the yb_send_dm tool (look up the recipient by name or pass "
            "their user_id). Your normal reply is delivered to the group you "
            "are responding in."
        )

    # 已连接平台
    platforms_list = ["local (files on this machine)"]
    for p in context.connected_platforms:
        if p != Platform.LOCAL:
            platforms_list.append(f"{p.value}: Connected ✓")

    lines.append(f"**Connected Platforms:** {', '.join(platforms_list)}")

    # home channel
    if context.home_channels:
        lines.append("")
        lines.append("**Home Channels (default destinations):**")
        for platform, home in context.home_channels.items():
            hc_id = _hash_chat_id(home.chat_id) if redact_pii else home.chat_id
            lines.append(f"  - {platform.value}: {home.name} (ID: {hc_id})")

    # 计划任务的投递选项
    lines.append("")
    lines.append("**Delivery options for scheduled tasks:**")

    from hermes_constants import display_hermes_home

    # 来源投递
    if context.source.platform == Platform.LOCAL:
        lines.append("- `\"origin\"` → Local output (saved to files)")
    else:
        _origin_label = context.source.chat_name or (
            _hash_chat_id(context.source.chat_id) if redact_pii else context.source.chat_id
        )
        lines.append(f"- `\"origin\"` → Back to this chat ({_origin_label})")

    # local 始终可用
    lines.append(
        f"- `\"local\"` → Save to local files only ({display_hermes_home()}/cron/output/)"
    )

    # 平台 home channel
    for platform, home in context.home_channels.items():
        lines.append(f"- `\"{platform.value}\"` → Home channel ({home.name})")

    # 关于显式定向的说明
    lines.append("")
    lines.append("*For explicit targeting, use `\"platform:chat_id\"` format if the user provides a specific chat ID.*")

    return "\n".join(lines)


@dataclass
class SessionEntry:
    """
    会话存储中的一条记录。

    把一个 session key 映射到其当前 session ID 及元数据。
    """
    session_key: str
    session_id: str
    created_at: datetime
    updated_at: datetime

    # 用于投递路由的来源元数据
    origin: Optional[SessionSource] = None

    # 展示元数据
    display_name: Optional[str] = None
    platform: Optional[Platform] = None
    chat_type: str = "dm"

    # token 跟踪
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    cost_status: str = "unknown"

    # 上一次 API 上报的 prompt token 数（用于精确的压缩预检查）
    last_prompt_tokens: int = 0

    # 当一个 session 是因为前一个 session 过期而创建时设置；由消息处理器消费一次，
    # 用于把通知注入上下文
    was_auto_reset: bool = False
    auto_reset_reason: Optional[str] = None  # "idle" 或 "daily"
    reset_had_activity: bool = False  # 过期的 session 是否曾有消息

    # 由 reset_session() 在用户显式发送 /new 或 /reset 时设置。由
    # _handle_message_with_agent 消费一次，用于在新 session 的第一条消息上触发
    # topic/channel skill 重新注入。我们不能复用 was_auto_reset，因为那个 flag 会触发
    # 面向用户的"session 因不活跃而过期"通知，以及误导性的上下文备注前缀 —— 这两者
    # 对于显式手动重置都是错误的。参见 issue #6508。
    is_fresh_reset: bool = False

    # 由后台过期 watcher 在完成一个过期 session 的收尾（调用 on_session_finalize
    # 钩子并驱逐缓存的 agent）之后设置。持久化到 sessions.json，使该 flag 能在
    # gateway 重启后保留 —— 避免冗余的收尾运行。
    expiry_finalized: bool = False

    # 为 True 时，下次调用 get_or_create_session() 会自动重置本 session（创建新的
    # session_id），让用户从头开始。由 /stop 设置，用于打破卡住的 resume 循环
    #（#7536）。
    suspended: bool = False

    # 为 True 时表示该 session 被一次 gateway 重启/关机排空超时打断，但恢复仍可期。
    # 与 ``suspended`` 不同，``resume_pending`` 在下次访问时保留现有 session_id ——
    # 用户停留在同一份 transcript 上，agent 从中断处自动继续。在下一次成功的 turn
    # 之后清除。升级为 ``suspended`` 由现有的 ``.restart_failure_counts`` 卡循环
    # 计数器（#7536）处理，而非本记录上的并行计数器。
    resume_pending: bool = False
    resume_reason: Optional[str] = None  # 例如 "restart_timeout"
    last_resume_marked_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "session_key": self.session_key,
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "display_name": self.display_name,
            "platform": self.platform.value if self.platform else None,
            "chat_type": self.chat_type,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
            "last_prompt_tokens": self.last_prompt_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "cost_status": self.cost_status,
            "expiry_finalized": self.expiry_finalized,
            "suspended": self.suspended,
            "resume_pending": self.resume_pending,
            "resume_reason": self.resume_reason,
            "last_resume_marked_at": (
                self.last_resume_marked_at.isoformat()
                if self.last_resume_marked_at
                else None
            ),
            "is_fresh_reset": self.is_fresh_reset,
            "was_auto_reset": self.was_auto_reset,
            "auto_reset_reason": self.auto_reset_reason,
            "reset_had_activity": self.reset_had_activity,
        }
        if self.origin:
            result["origin"] = self.origin.to_dict()
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionEntry":
        origin = None
        if "origin" in data and isinstance(data["origin"], dict):
            origin = SessionSource.from_dict(data["origin"])
        
        platform = None
        if data.get("platform"):
            try:
                platform = Platform(data["platform"])
            except ValueError as e:
                logger.debug("Unknown platform value %r: %s", data["platform"], e)

        last_resume_marked_at = None
        _lrma = data.get("last_resume_marked_at")
        if _lrma:
            try:
                last_resume_marked_at = datetime.fromisoformat(_lrma)
            except (TypeError, ValueError):
                last_resume_marked_at = None

        session_key = data["session_key"]
        session_id = data["session_id"]

        # 校验对路径敏感的字段以防止目录遍历（CWE-22）
        for _field, _val in (("session_key", session_key), ("session_id", session_id)):
            if _is_path_unsafe(_val):
                raise ValueError(
                    f"Invalid {_field}: potential directory traversal detected"
                )

        return cls(
            session_key=session_key,
            session_id=session_id,
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            origin=origin,
            display_name=data.get("display_name"),
            platform=platform,
            chat_type=data.get("chat_type", "dm"),
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            cache_read_tokens=data.get("cache_read_tokens", 0),
            cache_write_tokens=data.get("cache_write_tokens", 0),
            total_tokens=data.get("total_tokens", 0),
            last_prompt_tokens=data.get("last_prompt_tokens", 0),
            estimated_cost_usd=data.get("estimated_cost_usd", 0.0),
            cost_status=data.get("cost_status", "unknown"),
            expiry_finalized=data.get("expiry_finalized", data.get("memory_flushed", False)),
            suspended=data.get("suspended", False),
            resume_pending=data.get("resume_pending", False),
            resume_reason=data.get("resume_reason"),
            last_resume_marked_at=last_resume_marked_at,
            is_fresh_reset=data.get("is_fresh_reset", False),
            was_auto_reset=data.get("was_auto_reset", False),
            auto_reset_reason=data.get("auto_reset_reason"),
            reset_had_activity=data.get("reset_had_activity", False),
        )


def is_shared_multi_user_session(
    source: SessionSource,
    *,
    group_sessions_per_user: bool = True,
    thread_sessions_per_user: bool = False,
) -> bool:
    """当非 DM 会话在参与者之间共享时返回 True。

    对应 :func:`build_session_key` 中的隔离规则：
      - DM 从不共享。
      - thread 除非 ``thread_sessions_per_user`` 为 True，否则共享。
      - 非 thread 的群组/频道会话除非 ``group_sessions_per_user`` 为 True（默认：
        True = 隔离），否则共享。
    """
    if source.chat_type == "dm":
        return False
    if source.thread_id:
        return not thread_sessions_per_user
    return not group_sessions_per_user


def _session_key_namespace(profile: Optional[str]) -> str:
    """返回 session key 的 ``agent:<ns>`` 命名空间前缀。

    历史上的 key 格式是 ``agent:main:<platform>:<chat_type>:...``，其中 ``main``
    是一个静态命名空间字面量（不是 branch 名 —— branch 的 key 取自
    ``session_id``，而非此槽位）。多 profile 多路复用复用此槽位来承载 profile：

    - 默认 profile（或 ``None``/``""``/``"default"``）→ ``agent:main`` —— 与历来
      生成的每个 key 字节完全一致，因此现有 session 和所有按位置解析的解析器
      （``parts[2]`` == platform 等）都不受影响。
    - 命名 profile ``coder`` → ``agent:coder`` —— 保持相同的位置布局，只是命名空间
      不同，因此服务同一 platform/chat 的两个 profile 永不冲突。
    """
    if not profile or profile == "default":
        return "agent:main"
    return f"agent:{profile}"


def build_session_key(
    source: SessionSource,
    group_sessions_per_user: bool = True,
    thread_sessions_per_user: bool = False,
    profile: Optional[str] = None,
) -> str:
    """根据消息来源构建确定性的 session key。

    这是 session key 构造的唯一真相来源。

    ``profile`` 选择 key 命名空间（参见 :func:`_session_key_namespace`）。默认为
    ``None`` ⇒ 旧版 ``agent:main`` 命名空间，不做多路复用的调用方生成与此前字节
    完全一致的 key。只有多路复用 gateway 才传入非默认 profile。

    DM 规则：
      - DM 在有 chat_id 时包含 chat_id，使每个私聊会话相互隔离。
      - thread_id 进一步区分同一 DM chat 内的 threaded DM。
      - 没有 chat_id 时，thread_id 作为尽力而为的回退。
      - 没有 thread_id 或 chat_id 时，DM 共享单个 session。

    群组/频道规则：
      - chat_id 标识父群组/频道。
      - 当启用 ``group_sessions_per_user`` 且可用时，user_id/user_id_alt 在该父
        chat 内隔离参与者。
      - thread_id 区分该父 chat 内的 thread。当 ``thread_sessions_per_user`` 为
        False（默认）时，thread 在所有参与者之间 *共享* —— 不追加 user_id，因此
        thread 中的每个用户共享单个 session。这是 threaded 对话（Telegram forum
        topic、Discord thread、Slack thread）的预期 UX。
      - 没有参与者标识符，或禁用隔离时，消息回退为每个 chat 一个共享 session。
      - 没有标识符时，消息回退为每个 platform/chat_type 一个 session。
    """
    ns = _session_key_namespace(profile)
    platform = source.platform.value
    if source.chat_type == "dm":
        dm_chat_id = source.chat_id
        if source.platform == Platform.WHATSAPP:
            dm_chat_id = canonical_whatsapp_identifier(source.chat_id)

        if dm_chat_id:
            if source.thread_id:
                return f"{ns}:{platform}:dm:{dm_chat_id}:{source.thread_id}"
            return f"{ns}:{platform}:dm:{dm_chat_id}"
        # 没有 chat_id —— 在落到裸的 per-platform sink 之前，回退到发送者自己的
        # 标识符。否则每个不带 chat_id 的用户的 DM（非标准 adapter / 合成来源）都会
        # 塌缩进一个共享的 "<ns>:<platform>:dm" session，单个缓存的 agent 会同时服务
        # 多人的对话 —— 跨用户历史泄漏。participant_id 让 DM 按用户隔离。
        dm_participant_id = source.user_id_alt or source.user_id
        if dm_participant_id and source.platform == Platform.WHATSAPP:
            dm_participant_id = (
                canonical_whatsapp_identifier(str(dm_participant_id))
                or dm_participant_id
            )
        if dm_participant_id:
            if source.thread_id:
                return f"{ns}:{platform}:dm:{dm_participant_id}:{source.thread_id}"
            return f"{ns}:{platform}:dm:{dm_participant_id}"
        if source.thread_id:
            return f"{ns}:{platform}:dm:{source.thread_id}"
        return f"{ns}:{platform}:dm"

    participant_id = source.user_id_alt or source.user_id
    if participant_id and source.platform == Platform.WHATSAPP:
        # 与 DM 情况相同的 JID/LID 翻转 bug：不做规范化时，当 bridge 重新打乱别名
        # 形式时，单个群组成员会得到两个隔离的 per-user session。
        participant_id = canonical_whatsapp_identifier(str(participant_id)) or participant_id
    key_parts = [ns, platform, source.chat_type]

    if source.chat_id:
        key_parts.append(source.chat_id)
    if source.thread_id:
        key_parts.append(source.thread_id)

    # 在 thread 中，默认共享 session（所有参与者看到同一段对话）。仅当通过
    # thread_sessions_per_user 显式启用、或没有 thread（普通群组）时才按用户隔离。
    isolate_user = group_sessions_per_user
    if source.thread_id and not thread_sessions_per_user:
        isolate_user = False

    if isolate_user and participant_id:
        key_parts.append(str(participant_id))

    return ":".join(key_parts)


class SessionStore:
    """
    管理会话存储与检索。

    使用 SQLite（通过 SessionDB）存储会话元数据和消息 transcript。当 SQLite 不可用
    时回退到旧版 JSONL 文件。
    """
    
    def __init__(self, sessions_dir: Path, config: GatewayConfig,
                 has_active_processes_fn=None):
        self.sessions_dir = sessions_dir
        self.config = config
        self._entries: Dict[str, SessionEntry] = {}
        self._loaded = False
        self._lock = threading.Lock()
        self._has_active_processes_fn = has_active_processes_fn
        
        # 初始化 SQLite 会话数据库
        self._db = None
        try:
            from hermes_state import SessionDB
            self._db = SessionDB()
        except Exception as e:
            print(f"[gateway] Warning: SQLite session store unavailable, falling back to JSONL: {e}")

    def _ensure_loaded(self) -> None:
        """如果尚未从磁盘加载会话索引，则加载之。"""
        with self._lock:
            self._ensure_loaded_locked()

    def _ensure_loaded_locked(self) -> None:
        """从磁盘加载会话索引。必须在持有 self._lock 时调用。"""
        if self._loaded:
            return

        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        sessions_file = self.sessions_dir / "sessions.json"

        if sessions_file.exists():
            try:
                with open(sessions_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for key, entry_data in data.items():
                    # 以 "_" 开头的 key 是文档/元数据哨兵（例如 _save 写入的
                    # "_README" 说明），不是会话记录。跳过它们，使其永远不会到达
                    # SessionEntry.from_dict。
                    if key.startswith("_"):
                        continue
                    # 跳过非 dict 记录（损坏的 sessions.json，例如本应是 dict 的地方
                    # 出现裸 bool 或字符串）。否则 from_dict 会在 `"origin" in data`
                    # 上抛出 TypeError，它会逃出内层 except（ValueError、KeyError）并
                    # 中止加载所有剩余 session（#46994）。
                    if not isinstance(entry_data, dict):
                        logger.warning(
                            "Skipping invalid session entry %r: "
                            "expected dict, got %s",
                            key, type(entry_data).__name__,
                        )
                        continue
                    try:
                        self._entries[key] = SessionEntry.from_dict(entry_data)
                    except (ValueError, KeyError, TypeError) as e:
                        logger.warning("Skipping invalid session entry %r: %s", key, e)
            except Exception as e:
                print(f"[gateway] Warning: Failed to load sessions: {e}")

        self._loaded = True

    def _save(self) -> None:
        """把会话索引保存到磁盘（保留 session key -> ID 映射）。"""
        import tempfile
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        sessions_file = self.sessions_dir / "sessions.json"

        data = {key: entry.to_dict() for key, entry in self._entries.items()}
        # 自解释哨兵，使任何直接查看此文件的人都能理解它是什么，以及 CLI/TUI 会话实际
        # 存在哪里。以 "_" 开头的 key 在加载时会被跳过（见
        # _ensure_loaded_locked），因此这永远不会往返变成一个 SessionEntry。通过一个
        # 全新 dict 把它排在最前，使其渲染在美化打印 JSON 的顶部。
        data = {
            "_README": (
                "Gateway routing index ONLY: maps messaging session keys "
                "(agent:main:<platform>:...) to active session IDs. This is NOT "
                "the session list. ALL sessions (CLI, TUI, and gateway) live in "
                "~/.hermes/state.db and are shown by `hermes sessions list` and "
                "`/sessions`. Seeing only gateway entries here is expected and "
                "does not mean CLI sessions are missing."
            ),
            **data,
        }
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.sessions_dir), suffix=".tmp", prefix=".sessions_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            atomic_replace(tmp_path, sessions_file)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError as e:
                logger.debug("Could not remove temp file %s: %s", tmp_path, e)
            raise

    def _resolve_profile_for_key(self, source: Optional[SessionSource] = None) -> Optional[str]:
        """返回用于 session key 的 profile 命名空间，关闭时返回 None。

        当 ``multiplex_profiles`` 禁用时（默认），返回 ``None``，使 key 留在旧版
        ``agent:main`` 命名空间 —— 与此前字节完全一致。启用时，优先使用入站来源被
        路由到的 profile（``source.profile`` —— 由 /p/<profile>/ URL 前缀或按凭据
        adapter 设置），回退到活动 profile 名。
        """
        if not getattr(self.config, "multiplex_profiles", False):
            return None
        if source is not None and source.profile:
            return source.profile
        try:
            from hermes_cli.profiles import get_active_profile_name
            return get_active_profile_name() or "default"
        except Exception:
            return None

    def _generate_session_key(self, source: SessionSource) -> str:
        """根据来源生成 session key。"""
        return build_session_key(
            source,
            group_sessions_per_user=getattr(self.config, "group_sessions_per_user", True),
            thread_sessions_per_user=getattr(self.config, "thread_sessions_per_user", False),
            profile=self._resolve_profile_for_key(source),
        )

    def _is_session_expired(self, entry: SessionEntry) -> bool:
        """根据重置策略检查一个 session 是否已过期。

        仅凭记录即可工作 —— 不需要 SessionSource。
        供后台过期 watcher 用于主动刷新 memory。
        有活动后台进程的 session 永远不算过期。
        """
        if self._has_active_processes_fn:
            if self._has_active_processes_fn(entry.session_key):
                return False

        policy = self.config.get_reset_policy(
            platform=entry.platform,
            session_type=entry.chat_type,
        )

        if policy.mode == "none":
            return False

        now = _now()

        if policy.mode in {"idle", "both"}:
            idle_deadline = entry.updated_at + timedelta(minutes=policy.idle_minutes)
            if now > idle_deadline:
                return True

        if policy.mode in {"daily", "both"}:
            today_reset = now.replace(
                hour=policy.at_hour,
                minute=0, second=0, microsecond=0,
            )
            if now.hour < policy.at_hour:
                today_reset -= timedelta(days=1)
            if entry.updated_at < today_reset:
                return True

        return False

    def _should_reset(self, entry: SessionEntry, source: SessionSource) -> Optional[str]:
        """
        根据策略检查一个 session 是否应被重置。

        若需要重置则返回重置原因（"idle" 或 "daily"），否则返回 None（session 仍
        有效）。

        有活动后台进程的 session 永不重置。
        """
        if self._has_active_processes_fn:
            session_key = self._generate_session_key(source)
            if self._has_active_processes_fn(session_key):
                return None

        policy = self.config.get_reset_policy(
            platform=source.platform,
            session_type=source.chat_type
        )
        
        if policy.mode == "none":
            return None
        
        now = _now()
        
        if policy.mode in {"idle", "both"}:
            idle_deadline = entry.updated_at + timedelta(minutes=policy.idle_minutes)
            if now > idle_deadline:
                return "idle"
        
        if policy.mode in {"daily", "both"}:
            today_reset = now.replace(
                hour=policy.at_hour, 
                minute=0, 
                second=0, 
                microsecond=0
            )
            if now.hour < policy.at_hour:
                today_reset -= timedelta(days=1)
            
            if entry.updated_at < today_reset:
                return "daily"
        
        return None
    
    def has_any_sessions(self) -> bool:
        """检查是否曾创建过任何 session（跨所有平台）。

        以 SQLite 数据库为真相来源，因为它保留历史会话记录（已结束的 session 仍计数）。
        内存中的 ``_entries`` 字典在重置时会替换记录，因此对单平台用户
        ``len(_entries)`` 会一直停在 1 —— 这正是本修复所解决的 bug。

        当前 session 在调用此方法时已经位于 DB 中（get_or_create_session 先运行），
        因此我们检查 ``> 1``。
        """
        if self._db:
            try:
                return self._db.session_count() > 1
            except Exception:
                pass  # 落到启发式
        # 回退：检查 sessions.json 加载时是否已有现存数据。
        # 这覆盖了 DB 不可用的罕见情况。
        with self._lock:
            self._ensure_loaded_locked()
            return len(self._entries) > 1

    def get_or_create_session(
        self,
        source: SessionSource,
        force_new: bool = False
    ) -> SessionEntry:
        """
        获取现有 session，或创建新 session。

        评估重置策略以判断现有 session 是否已过期。新 session 启动时在 SQLite 中
        创建会话记录。
        """
        session_key = self._generate_session_key(source)
        now = _now()

        # SQLite 调用在锁之外进行，避免在 I/O 期间持有锁。
        # 所有 _entries / _loaded 的修改都由 self._lock 保护。
        db_end_session_id = None
        db_create_kwargs = None

        with self._lock:
            self._ensure_loaded_locked()

            if session_key in self._entries and not force_new:
                entry = self._entries[session_key]

                # 自动重置被标记为 suspended 的 session（例如 /stop 打破了卡住的
                # 循环 —— #7536）。``suspended`` 是硬性强制清除信号，总是优先于
                # ``resume_pending``，因此通过现有 ``.restart_failure_counts`` 卡循环
                # 计数器升级的反复中断重启仍会收敛到一个干净状态。
                if entry.suspended:
                    reset_reason = "suspended"
                elif entry.resume_pending:
                    # 重启中断的 session：保留 session_id 并返回现有记录，使
                    # transcript 完整重载。``resume_pending`` 在下一次成功的 turn 完成
                    # 之后清除（不是此处），这意味着再次被中断的重试会持续尝试 ——
                    # 卡循环计数器负责终局升级。
                    entry.updated_at = now
                    self._save()
                    return entry
                else:
                    reset_reason = self._should_reset(entry, source)
                if not reset_reason:
                    entry.updated_at = now
                    self._save()
                    return entry
                else:
                    # session 正在被自动重置。
                    was_auto_reset = True
                    auto_reset_reason = reset_reason
                    # 跟踪过期 session 是否曾有过真实对话
                    reset_had_activity = entry.total_tokens > 0
                    db_end_session_id = entry.session_id
            else:
                was_auto_reset = False
                auto_reset_reason = None
                reset_had_activity = False

            # 创建新 session
            session_id = f"{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

            entry = SessionEntry(
                session_key=session_key,
                session_id=session_id,
                created_at=now,
                updated_at=now,
                origin=source,
                display_name=source.chat_name,
                platform=source.platform,
                chat_type=source.chat_type,
                was_auto_reset=was_auto_reset,
                auto_reset_reason=auto_reset_reason,
                reset_had_activity=reset_had_activity,
            )

            self._entries[session_key] = entry
            self._save()
            db_create_kwargs = {
                "session_id": session_id,
                "source": source.platform.value,
                "user_id": source.user_id,
            }

        # 在锁之外进行 SQLite 操作
        if self._db and db_end_session_id:
            try:
                self._db.end_session(db_end_session_id, "session_reset")
            except Exception as e:
                logger.debug("Session DB operation failed: %s", e)

        if self._db and db_create_kwargs:
            try:
                self._db.create_session(**db_create_kwargs)
            except Exception as e:
                print(f"[gateway] Warning: Failed to create SQLite session: {e}")

        return entry

    def update_session(
        self,
        session_key: str,
        last_prompt_tokens: int = None,
    ) -> None:
        """在一次交互之后更新轻量级会话元数据。"""
        with self._lock:
            self._ensure_loaded_locked()

            if session_key in self._entries:
                entry = self._entries[session_key]
                entry.updated_at = _now()
                if last_prompt_tokens is not None:
                    entry.last_prompt_tokens = last_prompt_tokens
                self._save()

    def suspend_session(self, session_key: str) -> bool:
        """把一个 session 标记为 suspended，使其在下次访问时自动重置。

        由 ``/stop`` 使用，防止卡住的 session 在 gateway 重启后被恢复（#7536）。
        如果 session 存在且已被标记，则返回 True。
        """
        with self._lock:
            self._ensure_loaded_locked()
            if session_key in self._entries:
                self._entries[session_key].suspended = True
                self._save()
                return True
        return False

    def mark_resume_pending(
        self,
        session_key: str,
        reason: str = "restart_timeout",
    ) -> bool:
        """在一次重启中断之后把一个 session 标记为可恢复。

        与 ``suspend_session()`` 不同，此方法保留现有 ``session_id`` 和 transcript。
        下次对该 key 调用 ``get_or_create_session()`` 时返回同一条记录，使用户在
        同一条对话通道上自动恢复。

        如果 session 存在且已被标记，则返回 True。
        """
        with self._lock:
            self._ensure_loaded_locked()
            if session_key in self._entries:
                entry = self._entries[session_key]
                # 永不覆盖显式的 ``suspended`` —— 那是一个硬性强制清除信号（来自
                # /stop 或卡循环升级）。
                if entry.suspended:
                    return False
                entry.resume_pending = True
                entry.resume_reason = reason
                entry.last_resume_marked_at = _now()
                self._save()
                return True
        return False

    def clear_resume_pending(self, session_key: str) -> bool:
        """在一次成功的恢复 turn 之后清除 resume-pending flag。

        由 gateway 在 ``run_conversation()`` 为某个曾设置 ``resume_pending=True``
        的 session 返回最终响应之后调用，表示恢复成功。

        如果清除了某个 flag，则返回 True。
        """
        with self._lock:
            self._ensure_loaded_locked()
            entry = self._entries.get(session_key)
            if entry is None or not entry.resume_pending:
                return False
            entry.resume_pending = False
            entry.resume_reason = None
            entry.last_resume_marked_at = None
            self._save()
            return True

    def prune_old_entries(self, max_age_days: int) -> int:
        """丢弃超过 max_age_days 的 SessionEntry 记录。

        修剪基于 ``updated_at``（最后活动时间），而非 ``created_at``。在窗口内有活动
        的 session 无论多旧都会保留。标记为 ``suspended`` 的记录会保留 —— 用户显式
        暂停它们以备稍后恢复。由活动进程持有（通过 has_active_processes_fn）的记录也
        会保留，使长时间运行的后台工作不会成为孤儿。

        修剪在功能上等价于一次自然的重置策略过期：SQLite 中的 transcript 保留，但
        session_key → session_id 的映射被丢弃，用户回来时开启一个新 session。

        ``max_age_days <= 0`` 禁用修剪；立即返回 0。
        返回被移除的记录数。
        """
        if max_age_days is None or max_age_days <= 0:
            return 0
        from datetime import timedelta

        cutoff = _now() - timedelta(days=max_age_days)
        removed_keys: list[str] = []

        with self._lock:
            self._ensure_loaded_locked()
            for key, entry in list(self._entries.items()):
                if entry.suspended:
                    continue
                # 永不修剪挂有活动后台进程的 session —— 用户可能仍在等待输出。
                # 该回调以 session_key 为键（见 process_registry.
                # has_active_for_session）；以前传 session_id 永远不会匹配，因此活动
                # session 仍会被修剪。
                if self._has_active_processes_fn is not None:
                    try:
                        if self._has_active_processes_fn(entry.session_key):
                            continue
                    except Exception as exc:
                        logger.debug(
                            "has_active_processes_fn raised during prune for %s: %s",
                            entry.session_key, exc,
                        )
                if entry.updated_at < cutoff:
                    removed_keys.append(key)
            for key in removed_keys:
                self._entries.pop(key, None)
            if removed_keys:
                self._save()

        if removed_keys:
            logger.info(
                "SessionStore pruned %d entries older than %d days",
                len(removed_keys), max_age_days,
            )
        return len(removed_keys)

    def suspend_recently_active(self, max_age_seconds: int = 120) -> int:
        """在一次意外退出之后，把最近活动的 session 标记为可恢复。

        在 gateway 因崩溃或快速重启启动时调用，以保留进行中的 session 而不是销毁其
        对话历史（#7536）。仅标记在 *max_age_seconds* 内有更新的 session，避免触碰
        长期空闲的 session。设置 ``resume_pending=True``，使同一 session_key 上的
        下一条入站消息从现有 transcript 自动恢复。

        已标记 ``resume_pending=True`` 的记录被跳过。显式 ``suspended=True``（来自
        /stop 或卡循环升级）的记录也被跳过。真正卡住 session 的终局升级仍由现有
        ``.restart_failure_counts`` 计数器（阈值 3）处理，它在本次方法之后运行并设置
        ``suspended=True``。

        返回被标记为可恢复的 session 数。
        """
        from datetime import timedelta

        cutoff = _now() - timedelta(seconds=max_age_seconds)
        count = 0
        with self._lock:
            self._ensure_loaded_locked()
            for entry in self._entries.values():
                if entry.resume_pending:
                    continue
                if not entry.suspended and entry.updated_at >= cutoff:
                    entry.resume_pending = True
                    entry.resume_reason = "restart_interrupted"
                    entry.last_resume_marked_at = _now()
                    count += 1
            if count:
                self._save()
        return count

    def reset_session(self, session_key: str, display_name: Optional[str] = None) -> Optional[SessionEntry]:
        """强制重置一个 session，创建新的 session ID。"""
        db_end_session_id = None
        db_create_kwargs = None
        new_entry = None

        with self._lock:
            self._ensure_loaded_locked()

            if session_key not in self._entries:
                return None

            old_entry = self._entries[session_key]
            db_end_session_id = old_entry.session_id

            now = _now()
            session_id = f"{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

            new_entry = SessionEntry(
                session_key=session_key,
                session_id=session_id,
                created_at=now,
                updated_at=now,
                origin=old_entry.origin,
                display_name=display_name if display_name is not None else old_entry.display_name,
                platform=old_entry.platform,
                chat_type=old_entry.chat_type,
                is_fresh_reset=True,
            )

            self._entries[session_key] = new_entry
            self._save()
            db_create_kwargs = {
                "session_id": session_id,
                "source": old_entry.platform.value if old_entry.platform else "unknown",
                "user_id": old_entry.origin.user_id if old_entry.origin else None,
            }

        if self._db and db_end_session_id:
            try:
                self._db.end_session(db_end_session_id, "session_reset")
            except Exception as e:
                logger.debug("Session DB operation failed: %s", e)

        if self._db and db_create_kwargs:
            try:
                self._db.create_session(**db_create_kwargs)
            except Exception as e:
                logger.debug("Session DB operation failed: %s", e)

        return new_entry

    def switch_session(self, session_key: str, target_session_id: str) -> Optional[SessionEntry]:
        """把一个 session key 切换到指向一个现有 session ID。

        由 ``/resume`` 使用，用于恢复之前命名的 session。在 SQLite 中结束当前
        session（类似 reset），但不生成新的 session ID，而是复用
        ``target_session_id``，使旧 transcript 在下一条消息时被加载。如果目标
        session 此前已结束，则重新打开它，使 gateway 的恢复语义与 CLI 一致。
        """
        db_end_session_id = None
        new_entry = None

        with self._lock:
            self._ensure_loaded_locked()

            if session_key not in self._entries:
                return None

            old_entry = self._entries[session_key]

            # 如果已在该 session 上则不切换
            if old_entry.session_id == target_session_id:
                return old_entry

            db_end_session_id = old_entry.session_id

            now = _now()
            new_entry = SessionEntry(
                session_key=session_key,
                session_id=target_session_id,
                created_at=now,
                updated_at=now,
                origin=old_entry.origin,
                display_name=old_entry.display_name,
                platform=old_entry.platform,
                chat_type=old_entry.chat_type,
            )

            self._entries[session_key] = new_entry
            self._save()

        if self._db and db_end_session_id:
            try:
                self._db.end_session(db_end_session_id, "session_switch")
            except Exception as e:
                logger.debug("Session DB end_session failed: %s", e)

        if self._db:
            try:
                self._db.reopen_session(target_session_id)
            except Exception as e:
                logger.debug("Session DB reopen_session failed: %s", e)

        return new_entry

    def list_sessions(self, active_minutes: Optional[int] = None) -> List[SessionEntry]:
        """列出所有 session，可选按活动时间过滤。"""
        with self._lock:
            self._ensure_loaded_locked()
            entries = list(self._entries.values())

        if active_minutes is not None:
            cutoff = _now() - timedelta(minutes=active_minutes)
            entries = [e for e in entries if e.updated_at >= cutoff]

        entries.sort(key=lambda e: e.updated_at, reverse=True)

        return entries

    def lookup_by_session_id(self, session_id: str) -> Optional[SessionEntry]:
        """返回某个已持久化 session ID 对应的活动会话记录（如有）。"""
        if not session_id:
            return None
        with self._lock:
            self._ensure_loaded_locked()
            for entry in self._entries.values():
                if entry.session_id == session_id:
                    return entry
        return None

    def append_to_transcript(self, session_id: str, message: Dict[str, Any], skip_db: bool = False) -> None:
        """向一个 session 的 transcript 追加一条消息（SQLite）。

        Args:
            skip_db: 为 True 时跳过 SQLite 写入。当 agent 已通过其自身的
                     _flush_messages_to_session_db() 把消息持久化到 SQLite 时使用，
                     以避免重复写入 bug（#860）。
        """
        if self._db and not skip_db:
            try:
                self._db.append_message(
                    session_id=session_id,
                    role=message.get("role", "unknown"),
                    content=message.get("content"),
                    tool_name=message.get("tool_name"),
                    tool_calls=message.get("tool_calls"),
                    tool_call_id=message.get("tool_call_id"),
                    reasoning=message.get("reasoning") if message.get("role") == "assistant" else None,
                    reasoning_content=message.get("reasoning_content") if message.get("role") == "assistant" else None,
                    reasoning_details=message.get("reasoning_details") if message.get("role") == "assistant" else None,
                    codex_reasoning_items=message.get("codex_reasoning_items") if message.get("role") == "assistant" else None,
                    codex_message_items=message.get("codex_message_items") if message.get("role") == "assistant" else None,
                    # 平台侧消息 id（yuanbao msg_id、telegram update_id ……）。
                    # 接受显式的 ``platform_message_id``，或旧 JSONL transcript 使用
                    # 的 ``message_id`` key。
                    platform_message_id=(
                        message.get("platform_message_id") or message.get("message_id")
                    ),
                    observed=bool(message.get("observed")),
                    timestamp=message.get("timestamp"),
                )
            except Exception as e:
                logger.debug("Session DB operation failed: %s", e)

    def rewrite_transcript(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """用一个新消息集合替换某个 session 的整个 transcript。

        由 /retry、/undo、/compress 使用，用于持久化修改后的对话历史。state.db 是
        权威存储。
        """
        if self._db:
            try:
                self._db.replace_messages(session_id, messages)
            except Exception as e:
                logger.debug("Failed to rewrite transcript in DB: %s", e)

    def load_transcript(self, session_id: str) -> List[Dict[str, Any]]:
        """加载一个 session 的 transcript 中的所有消息。

        state.db 是权威存储。旧版 JSONL 回退已在 spec 002 中移除 —— 现有磁盘上
        DB 之前的 session 已被迁移（其 DB 行持有完整消息历史）。
        """
        if not self._db:
            return []
        try:
            return self._db.get_messages_as_conversation(session_id)
        except Exception as e:
            logger.debug("Could not load messages from DB: %s", e)
            return []

    def rewind_session(self, session_id: str, n: int = 1) -> Optional[Dict[str, Any]]:
        """通过软删除回退 ``n`` 个用户 turn，保留行供审计。

        与 :meth:`rewrite_transcript`（/retry 使用的硬替换）不同，此方法把被截断的
        行在 state.db 中翻转为 ``active=0``，使其留待审计并对重新提示和搜索隐藏。
        通过 ``SessionDB.rewind_to_message`` 对应 CLI/TUI 的 ``/undo [N]`` 行为。

        成功时返回 dict ``{"rewound_count", "turns_undone", "target_text"}``，没有
        DB 或没有可回退的用户消息时返回 ``None``。当 ``n`` 超过 turn 数时会被钳制到
        最旧的用户 turn。
        """
        if not self._db:
            return None
        if n < 1:
            n = 1
        try:
            recents = self._db.list_recent_user_messages(session_id, limit=max(n, 10))
        except Exception as e:
            logger.debug("rewind_session: failed to list user messages: %s", e)
            return None
        if not recents:
            return None
        target_idx = min(n - 1, len(recents) - 1)
        target_id = recents[target_idx]["id"]
        try:
            result = self._db.rewind_to_message(session_id, target_id)
        except ValueError as e:
            logger.debug("rewind_session: %s", e)
            return None
        except Exception as e:
            logger.debug("rewind_session: rewind_to_message failed: %s", e)
            return None
        target_msg = result.get("target_message") or {}
        content = target_msg.get("content") or ""
        if isinstance(content, list):
            parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            target_text = "\n".join(t for t in parts if t)
        elif isinstance(content, str):
            target_text = content
        else:
            target_text = ""
        return {
            "rewound_count": result.get("rewound_count", 0),
            "turns_undone": target_idx + 1,
            "target_text": target_text,
        }


def build_session_context(
    source: SessionSource,
    config: GatewayConfig,
    session_entry: Optional[SessionEntry] = None
) -> SessionContext:
    """
    根据来源和 config 构建完整会话上下文。

    用于把上下文注入 agent 的系统提示。
    """
    connected = config.get_connected_platforms()
    
    home_channels = {}
    for platform in connected:
        home = config.get_home_channel(platform)
        if home:
            home_channels[platform] = home
    
    context = SessionContext(
        source=source,
        connected_platforms=connected,
        home_channels=home_channels,
        shared_multi_user_session=is_shared_multi_user_session(
            source,
            group_sessions_per_user=getattr(config, "group_sessions_per_user", True),
            thread_sessions_per_user=getattr(config, "thread_sessions_per_user", False),
        ),
    )
    
    if session_entry:
        context.session_key = session_entry.session_key
        context.session_id = session_entry.session_id
        context.created_at = session_entry.created_at
        context.updated_at = session_entry.updated_at
    
    return context
