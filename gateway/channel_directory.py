"""
频道目录 —— 按平台缓存的可达频道/联系人映射。

在 gateway 启动时构建，定期刷新（每 5 分钟），并保存到
~/.hermes/channel_directory.json。send_message 工具读取此文件用于
action="list" 以及将人类友好的频道名解析为数字 ID。
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from hermes_cli.config import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

DIRECTORY_PATH = get_hermes_home() / "channel_directory.json"
# 用户维护的友好名称覆盖层。该目录由定时器从活跃适配器 + session 数据完全
# 重新生成，因此手动编辑 channel_directory.json 不会保留。此处声明的别名
# 会在每次构建和每次加载时重新应用，从而提供持久的人类友好名称（并允许你
# 在某个聊天产生任何流量之前就预先命名它）。
# 格式：{"<platform>": {"<chat_id>": "<friendly name>", ...}, ...}
CHANNEL_ALIASES_PATH = get_hermes_home() / "channel_aliases.json"


def _load_channel_aliases() -> Dict[str, Dict[str, str]]:
    if not CHANNEL_ALIASES_PATH.exists():
        return {}
    try:
        with open(CHANNEL_ALIASES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _apply_channel_aliases(platforms: Dict[str, Any]) -> None:
    """按 chat_id 将友好名称覆盖到目录条目上。

    就地重命名匹配的条目；为尚未被发现的别名 id 注入占位条目（这样
    一个新建的群组在发出第一条消息之前就可以通过名称寻址）。会修改
    *platforms*。
    """
    aliases = _load_channel_aliases()
    for plat_name, id_map in aliases.items():
        if not isinstance(id_map, dict):
            continue
        entries = platforms.setdefault(plat_name, [])
        if not isinstance(entries, list):
            continue
        for chat_id, friendly in id_map.items():
            if not isinstance(friendly, str) or not friendly.strip():
                continue
            chat_id = str(chat_id)
            friendly = friendly.strip()
            matched = False
            for e in entries:
                if isinstance(e, dict) and e.get("id") == chat_id:
                    e["name"] = friendly
                    matched = True
            if not matched:
                entries.append({
                    "id": chat_id,
                    "name": friendly,
                    "type": "group" if str(chat_id).endswith("@g.us") else "dm",
                    "thread_id": None,
                })


def _normalize_channel_query(value: str) -> str:
    return value.lstrip("#").strip().lower()


def _channel_target_name(platform_name: str, channel: Dict[str, Any]) -> str:
    """返回向用户展示的某个频道条目的人类可读目标标签。"""
    name = channel["name"]
    if platform_name == "discord" and channel.get("guild"):
        return f"#{name}"
    if platform_name != "discord" and channel.get("type"):
        return f"{name} ({channel['type']})"
    return name


def _session_entry_id(origin: Dict[str, Any]) -> Optional[str]:
    chat_id = origin.get("chat_id")
    if not chat_id:
        return None
    thread_id = origin.get("thread_id")
    if thread_id:
        return f"{chat_id}:{thread_id}"
    return str(chat_id)


def _session_entry_name(origin: Dict[str, Any]) -> str:
    base_name = origin.get("chat_name") or origin.get("user_name") or str(origin.get("chat_id"))
    thread_id = origin.get("thread_id")
    if not thread_id:
        return base_name

    topic_label = origin.get("chat_topic") or f"topic {thread_id}"
    return f"{base_name} / {topic_label}"


# ---------------------------------------------------------------------------
# 构建 / 刷新
# ---------------------------------------------------------------------------

async def build_channel_directory(adapters: Dict[Any, Any]) -> Dict[str, Any]:
    """
    从已连接的平台适配器和 session 数据构建频道目录。

    返回目录字典并将其写入 DIRECTORY_PATH。
    """
    from gateway.config import Platform

    platforms: Dict[str, List[Dict[str, str]]] = {}

    for platform, adapter in adapters.items():
        try:
            if platform == Platform.DISCORD:
                platforms["discord"] = _build_discord(adapter)
            elif platform == Platform.SLACK:
                platforms["slack"] = await _build_slack(adapter)
        except Exception as e:
            logger.warning("Channel directory: failed to build %s: %s", platform.value, e)

    # 不支持直接频道枚举的平台会自动获得基于 session 的发现。跳过那些不属于
    # 消息平台的基础设施条目 —— 其余的都落到 _build_from_sessions()。
    _SKIP_SESSION_DISCOVERY = frozenset({"local", "api_server", "webhook"})
    for plat in Platform:
        plat_name = plat.value
        if plat_name in _SKIP_SESSION_DISCOVERY or plat_name in platforms:
            continue
        platforms[plat_name] = _build_from_sessions(plat_name)

    # 包含插件注册的平台（动态 enum 成员不在 Platform.__members__ 中，因此
    # 上面的循环会漏掉它们）。
    try:
        from gateway.platform_registry import platform_registry
        for entry in platform_registry.plugin_entries():
            if entry.name not in _SKIP_SESSION_DISCOVERY and entry.name not in platforms:
                platforms[entry.name] = _build_from_sessions(entry.name)
    except Exception:
        pass

    # 持久化之前覆盖用户维护的友好名称。
    _apply_channel_aliases(platforms)

    directory = {
        "updated_at": datetime.now().isoformat(),
        "platforms": platforms,
    }

    try:
        atomic_json_write(DIRECTORY_PATH, directory)
    except Exception as e:
        logger.warning("Channel directory: failed to write: %s", e)

    return directory


def _build_discord(adapter) -> List[Dict[str, str]]:
    """枚举 Discord bot 能看到的全部文本频道和论坛频道。"""
    channels = []
    client = getattr(adapter, "_client", None)
    if not client:
        return channels

    try:
        import discord as _discord  # noqa: F401 — SDK 存在性检查
    except ImportError:
        return channels

    for guild in client.guilds:
        for ch in guild.text_channels:
            channels.append({
                "id": str(ch.id),
                "name": ch.name,
                "guild": guild.name,
                "type": "channel",
            })
        # 论坛频道（type 15）—— 创建一条消息会自动生成一个帖子线程。
        forums = getattr(guild, "forum_channels", None) or []
        for ch in forums:
            channels.append({
                "id": str(ch.id),
                "name": ch.name,
                "guild": guild.name,
                "type": "forum",
            })
        # 通过 guild 枚举来包含我们交互过的、可发起 DM 的用户是不可行的；
        # 那些来自 sessions。

    # 合并 session 历史中的任何 DM
    channels.extend(_build_from_sessions("discord"))
    return channels


async def _build_slack(adapter) -> List[Dict[str, Any]]:
    """列出 bot 在所有工作区中加入的 Slack 频道。

    对每个工作区的 web client 调用 ``users.conversations``。拉取 bot 所属的
    公开 + 私有频道，然后合并从 session 历史中发现的 DM（主动枚举 IM 没意义）。
    """
    team_clients = getattr(adapter, "_team_clients", None) or {}
    if not team_clients:
        return _build_from_sessions("slack")

    channels: List[Dict[str, Any]] = []
    seen_ids: set = set()

    for team_id, client in team_clients.items():
        try:
            cursor: Optional[str] = None
            for _page in range(20):  # 分页安全上限
                response = await client.users_conversations(
                    types="public_channel,private_channel",
                    exclude_archived=True,
                    limit=200,
                    cursor=cursor,
                )
                if not response.get("ok"):
                    logger.warning(
                        "Channel directory: users.conversations not ok for team %s: %s",
                        team_id,
                        response.get("error", "unknown"),
                    )
                    break
                for ch in response.get("channels", []):
                    cid = ch.get("id")
                    name = ch.get("name")
                    if not cid or not name or cid in seen_ids:
                        continue
                    seen_ids.add(cid)
                    channels.append({
                        "id": cid,
                        "name": name,
                        "type": "private" if ch.get("is_private") else "channel",
                    })
                cursor = (response.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        except Exception as e:
            logger.warning(
                "Channel directory: failed to list Slack channels for team %s: %s",
                team_id, e,
            )
            continue

    # 合并从 session 历史中发现的 DM/群组条目。
    for entry in _build_from_sessions("slack"):
        if entry.get("id") not in seen_ids:
            channels.append(entry)
            seen_ids.add(entry.get("id"))

    return channels


def _build_from_sessions(platform_name: str) -> List[Dict[str, str]]:
    """从 sessions.json 的 origin 数据中拉取已知频道/联系人。"""
    sessions_path = get_hermes_home() / "sessions" / "sessions.json"
    if not sessions_path.exists():
        return []

    entries = []
    try:
        with open(sessions_path, encoding="utf-8") as f:
            data = json.load(f)

        seen_ids = set()
        for _key, session in data.items():
            # 跳过文档/元数据哨兵（以 "_" 开头的键，例如 gateway 的 "_README"
            # 备注）——它们不是 session 条目。
            if str(_key).startswith("_") or not isinstance(session, dict):
                continue
            origin = session.get("origin") or {}
            if origin.get("platform") != platform_name:
                continue
            entry_id = _session_entry_id(origin)
            if not entry_id or entry_id in seen_ids:
                continue
            seen_ids.add(entry_id)
            entries.append({
                "id": entry_id,
                "name": _session_entry_name(origin),
                "type": session.get("chat_type", "dm"),
                "thread_id": origin.get("thread_id"),
            })
    except Exception as e:
        logger.debug("Channel directory: failed to read sessions for %s: %s", platform_name, e)

    return entries


# ---------------------------------------------------------------------------
# 读取 / 解析
# ---------------------------------------------------------------------------

def load_directory() -> Dict[str, Any]:
    """从磁盘加载缓存的频道目录。"""
    if not DIRECTORY_PATH.exists():
        base = {"updated_at": None, "platforms": {}}
        _apply_channel_aliases(base["platforms"])
        return base
    try:
        with open(DIRECTORY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        # 读取时重新应用别名，使友好名称立即生效，即便是在定时重建之间
        # 或全新的别名条目也是如此。
        _apply_channel_aliases(data.setdefault("platforms", {}))
        return data
    except Exception:
        base = {"updated_at": None, "platforms": {}}
        _apply_channel_aliases(base["platforms"])
        return base


def lookup_channel_type(platform_name: str, chat_id: str) -> Optional[str]:
    """返回 *chat_id* 对应的频道 ``type`` 字符串（例如 ``"channel"``、``"forum"``），未知则返回 *None*。"""
    directory = load_directory()
    for ch in directory.get("platforms", {}).get(platform_name, []):
        if ch.get("id") == chat_id:
            return ch.get("type")
    return None


def resolve_channel_name(platform_name: str, name: str) -> Optional[str]:
    """
    将人类友好的频道名解析为数字 ID。

    匹配策略（大小写不敏感，取第一个匹配）：
    - Discord: "bot-home"、"#bot-home"、"GuildName/bot-home"
    - Telegram: 显示名或群组名
    - Slack: "engineering"、"#engineering"
    """
    directory = load_directory()
    channels = directory.get("platforms", {}).get(platform_name, [])
    if not channels:
        return None

    # 0. 精确 ID 匹配 —— 大小写敏感，不做规范化。允许调用方传入原始的平台
    # ID（例如 Slack 的 "C0B0QV5434G"），即便 _parse_target_ref 中的格式
    # 守卫没有把它们识别为显式 ID。
    raw = name.strip()
    for ch in channels:
        if ch.get("id") == raw:
            return ch["id"]

    query = _normalize_channel_query(name)

    # 1. 精确名称匹配，包括 send_message(action="list") 展示的显示标签
    for ch in channels:
        if _normalize_channel_query(ch["name"]) == query:
            return ch["id"]
        if _normalize_channel_query(_channel_target_name(platform_name, ch)) == query:
            return ch["id"]

    # 2. Discord 的 guild 限定匹配（"GuildName/channel"）
    if "/" in query:
        guild_part, ch_part = query.rsplit("/", 1)
        for ch in channels:
            guild = ch.get("guild", "").strip().lower()
            if guild == guild_part and _normalize_channel_query(ch["name"]) == ch_part:
                return ch["id"]

    # 3. 部分前缀匹配（仅在无歧义时）
    matches = [ch for ch in channels if _normalize_channel_query(ch["name"]).startswith(query)]
    if len(matches) == 1:
        return matches[0]["id"]

    return None


def format_directory_for_display() -> str:
    """将频道目录格式化为面向模型的、人类可读的列表。"""
    directory = load_directory()
    platforms = directory.get("platforms", {})

    if not any(platforms.values()):
        return "No messaging platforms connected or no channels discovered yet."

    lines = ["Available messaging targets:\n"]

    for plat_name, channels in sorted(platforms.items()):
        if not channels:
            continue

        # 按 guild 分组 Discord 频道
        if plat_name == "discord":
            guilds: Dict[str, List] = {}
            dms: List = []
            for ch in channels:
                guild = ch.get("guild")
                if guild:
                    guilds.setdefault(guild, []).append(ch)
                else:
                    dms.append(ch)

            for guild_name, guild_channels in sorted(guilds.items()):
                lines.append(f"Discord ({guild_name}):")
                for ch in sorted(guild_channels, key=lambda c: c["name"]):
                    lines.append(f"  discord:{_channel_target_name(plat_name, ch)}")
            if dms:
                lines.append("Discord (DMs):")
                for ch in dms:
                    lines.append(f"  discord:{_channel_target_name(plat_name, ch)}")
            lines.append("")
        else:
            lines.append(f"{plat_name.title()}:")
            for ch in channels:
                lines.append(f"  {plat_name}:{_channel_target_name(plat_name, ch)}")
            lines.append("")

    lines.append('Use these as the "target" parameter when sending.')
    lines.append('Bare platform name (e.g. "telegram") sends to home channel.')

    return "\n".join(lines)
