"""
用于跨平台消息投递的会话镜像（mirroring）。

当一条消息被发送到某个平台（通过 send_message 或 cron 投递）时，本模块会
向目标会话的 transcript 追加一条“投递镜像（delivery-mirror）”记录，这样
接收侧的 agent 就能获得关于已发送内容的上下文。

独立运行——无需完整的 SessionStore 机制，即可在 CLI、cron 和 gateway
上下文中工作。
"""

import json
import logging
from datetime import datetime
from typing import Optional

from hermes_cli.config import get_hermes_home

logger = logging.getLogger(__name__)

_SESSIONS_DIR = get_hermes_home() / "sessions"
_SESSIONS_INDEX = _SESSIONS_DIR / "sessions.json"


def mirror_to_session(
    platform: str,
    chat_id: str,
    message_text: str,
    source_label: str = "cli",
    thread_id: Optional[str] = None,
    user_id: Optional[str] = None,
    role: str = "assistant",
) -> bool:
    """
    向目标会话的 transcript 追加一条投递镜像消息。

    查找与给定 platform + chat_id 匹配的 gateway 会话，然后向 JSONL
    transcript 和 SQLite DB 同时写入一条镜像条目。

    ``role`` 默认为 ``"assistant"`` —— 这对于交互式 ``send_message``
    镜像是正确的，因为被镜像的文本是 agent 自己发出去的回复（一次真正的
    assistant turn）。对于那些镜像的文本并非 agent 发言的调用方——例如
    一份通过带外方式投递的 cron 简报——必须传入 ``role="user"``：
    ``mirror``/``mirror_source`` 元数据会在 SQLite 边界被丢弃（只有
    role+content 会被持久化），因此在重放时，assistant 角色的镜像与真正的
    assistant turn 无法区分，会产生 ``assistant → assistant`` 对，从而破坏
    严格要求交替的 provider（issue #2221）。而 user 角色的镜像可以通过
    ``repair_message_sequence`` 在每个 provider 上安全地合并连续 user 消息。

    镜像成功时返回 True，找不到匹配会话或出错时返回 False。
    所有错误都会被捕获——此操作绝不会是致命的。
    """
    try:
        session_id = _find_session_id(
            platform,
            str(chat_id),
            thread_id=thread_id,
            user_id=user_id,
        )
        if not session_id:
            logger.debug(
                "Mirror: no session found for %s:%s:%s:%s",
                platform,
                chat_id,
                thread_id,
                user_id,
            )
            return False

        mirror_msg = {
            "role": role,
            "content": message_text,
            "timestamp": datetime.now().isoformat(),
            "mirror": True,
            "mirror_source": source_label,
        }

        _append_to_sqlite(session_id, mirror_msg)

        logger.debug("Mirror: wrote to session %s (from %s)", session_id, source_label)
        return True

    except Exception as e:
        logger.debug(
            "Mirror failed for %s:%s:%s:%s: %s",
            platform,
            chat_id,
            thread_id,
            user_id,
            e,
        )
        return False


def _find_session_id(
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[str]:
    """
    查找 platform + chat_id 对应的活动 session_id。

    扫描 sessions.json 条目，匹配 origin.chat_id == chat_id 且平台正确的
    记录。DM 会话键中并不内嵌 chat_id（例如 "agent:main:telegram:dm"），
    因此我们检查 origin dict。

    当提供了 *user_id* 时，优先匹配精确的发送者。如果存在多个同聊天候选
    且没有一个匹配该用户，则返回 None，而不是猜测并污染其他参与者的会话。
    """
    if not _SESSIONS_INDEX.exists():
        return None

    try:
        with open(_SESSIONS_INDEX, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    platform_lower = platform.lower()
    candidates = []

    for _key, entry in data.items():
        # 跳过文档/元数据哨兵（以 "_" 开头的键，例如 gateway 的 "_README"
        # 备注）——它们不是会话条目。
        if str(_key).startswith("_") or not isinstance(entry, dict):
            continue
        origin = entry.get("origin") or {}
        entry_platform = (origin.get("platform") or entry.get("platform", "")).lower()

        if entry_platform != platform_lower:
            continue

        origin_chat_id = str(origin.get("chat_id", ""))
        if origin_chat_id == str(chat_id):
            origin_thread_id = origin.get("thread_id")
            if thread_id is not None and str(origin_thread_id or "") != str(thread_id):
                continue
            candidates.append(entry)

    if not candidates:
        return None

    if user_id:
        exact_user_matches = [
            entry for entry in candidates
            if str((entry.get("origin") or {}).get("user_id") or "") == str(user_id)
        ]
        if exact_user_matches:
            candidates = exact_user_matches
        elif len(candidates) > 1:
            return None
    elif len(candidates) > 1:
        distinct_user_ids = {
            str((entry.get("origin") or {}).get("user_id") or "").strip()
            for entry in candidates
            if str((entry.get("origin") or {}).get("user_id") or "").strip()
        }
        if len(distinct_user_ids) > 1:
            return None

    best_entry = max(candidates, key=lambda entry: entry.get("updated_at", ""))
    return best_entry.get("session_id")



def _append_to_sqlite(session_id: str, message: dict) -> None:
    """向 SQLite 会话数据库追加一条消息。"""
    db = None
    try:
        from hermes_state import SessionDB
        db = SessionDB()
        db.append_message(
            session_id=session_id,
            role=message.get("role", "assistant"),
            content=message.get("content"),
        )
    except Exception as e:
        logger.debug("Mirror SQLite write failed: %s", e)
    finally:
        if db is not None:
            db.close()
