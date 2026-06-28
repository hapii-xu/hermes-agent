"""
yuanbao_tools.py - 元宝平台工具集

提供以下工具函数，供 hermes-agent 的 "hermes-yuanbao" toolset 使用：
  - get_group_info        : 查询群基本信息（群名、群主、成员数）
  - query_group_members   : 查询群成员（按名搜索、列举 bot、列举全部）
  - search_sticker        : 按关键词搜索内置贴纸（返回候选列表，含 sticker_id/name/description）
  - send_sticker          : 向当前会话或指定 chat_id 发送贴纸（TIMFaceElem）
  - send_dm               : 发送私聊消息（按昵称查找用户并发送）

对齐 chatbot-web/yuanbao-openclaw-plugin 的 sticker-search/sticker-send 行为：
LLM 应先用 search_sticker 找到合适的 sticker_id（或直接传中文 name），再用 send_sticker
发送。不要在文本中夹杂裸的 Unicode emoji 当作贴纸。

当前激活的适配器单例位于 ``gateway.platforms.yuanbao``，通过
``get_active_adapter()`` 访问。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


def _get_active_adapter():
    """懒加载导入，以避免 gateway.platforms.yuanbao 不可用时引发 ImportError。"""
    try:
        from gateway.platforms.yuanbao import get_active_adapter
        return get_active_adapter()
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# 角色标签
# ---------------------------------------------------------------------------

_USER_TYPE_LABEL = {0: "unknown", 1: "user", 2: "yuanbao_ai", 3: "bot"}

MENTION_HINT = (
    'To @mention a user, you MUST use the format: '
    'space + @ + nickname + space (e.g. " @Alice ").'
)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

async def get_group_info(group_code: str) -> dict:
    """查询群基本信息（群名、群主、成员数）。"""
    if not group_code:
        return {"success": False, "error": "group_code is required"}

    adapter = _get_active_adapter()
    if adapter is None:
        return {"success": False, "error": "Yuanbao adapter is not connected"}

    try:
        gi = await adapter.query_group_info(group_code)
        if gi is None:
            return {"success": False, "error": "query_group_info returned None"}
        return {
            "success": True,
            "group_code": group_code,
            "group_name": gi.get("group_name", ""),
            "member_count": gi.get("member_count", 0),
            "owner": {
                "user_id": gi.get("owner_id", ""),
                "nickname": gi.get("owner_nickname", ""),
            },
            "note": 'The group is called "派 (Pai)" in the app.',
        }
    except Exception as exc:
        logger.exception("[yuanbao_tools] get_group_info error")
        return {"success": False, "error": str(exc)}


async def query_group_members(
    group_code: str,
    action: str = "list_all",
    name: str = "",
    mention: bool = False,
) -> dict:
    """
    统一的群成员查询工具（对齐 TS query_session_members）。

    action:
      - find      : 按昵称模糊搜索
      - list_bots : 列出 bot 和元宝 AI
      - list_all  : 列出全部成员
    """
    if not group_code:
        return {"success": False, "error": "group_code is required"}

    adapter = _get_active_adapter()
    if adapter is None:
        return {"success": False, "error": "Yuanbao adapter is not connected"}

    try:
        raw = await adapter.get_group_member_list(group_code)
        if raw is None:
            return {"success": False, "error": "get_group_member_list returned None"}

        all_members = [
            {
                "user_id": m.get("user_id", ""),
                "nickname": m.get("nickname", m.get("nick_name", "")),
                "role": _USER_TYPE_LABEL.get(
                    m.get("user_type", m.get("role", 0)), "unknown"
                ),
            }
            for m in raw.get("members", [])
        ]

        if not all_members:
            return {"success": False, "error": "No members found in this group."}

        hint = {"mention_hint": MENTION_HINT} if mention else {}

        if action == "list_bots":
            bots = [m for m in all_members if m["role"] in {"yuanbao_ai", "bot"}]
            if not bots:
                return {"success": False, "error": "No bots found in this group."}
            return {
                "success": True,
                "msg": f"Found {len(bots)} bot(s).",
                "members": bots,
                **hint,
            }

        if action == "find":
            if name:
                filt = name.strip().lower()
                matched = [m for m in all_members if filt in m["nickname"].lower()]
                if matched:
                    return {
                        "success": True,
                        "msg": f'Found {len(matched)} member(s) matching "{name}".',
                        "members": matched,
                        **hint,
                    }
                return {
                    "success": False,
                    "msg": f'No match for "{name}". All members listed below.',
                    "members": all_members,
                    **hint,
                }
            return {
                "success": True,
                "msg": f"Found {len(all_members)} member(s).",
                "members": all_members,
                **hint,
            }

        # list_all（默认）
        return {
            "success": True,
            "msg": f"Found {len(all_members)} member(s).",
            "members": all_members,
            **hint,
        }

    except Exception as exc:
        logger.exception("[yuanbao_tools] query_group_members error")
        return {"success": False, "error": str(exc)}


async def search_sticker(query: str = "", limit: int = 10) -> dict:
    """
    在内置贴纸表中按关键词模糊搜索，返回 Top-N 候选。

    返回每条候选的 sticker_id / name / description / package_id，
    供 LLM 选择后传给 send_sticker。空 query 时返回前 N 条。
    """
    from gateway.platforms.yuanbao_sticker import search_stickers

    try:
        safe_limit = max(1, min(50, int(limit) if limit else 10))
    except (TypeError, ValueError):
        safe_limit = 10

    try:
        matches = search_stickers(query or "", limit=safe_limit)
    except Exception as exc:
        logger.exception("[yuanbao_tools] search_sticker error")
        return {"success": False, "error": str(exc)}

    return {
        "success": True,
        "query": query or "",
        "count": len(matches),
        "results": [
            {
                "sticker_id": s.get("sticker_id", ""),
                "name": s.get("name", ""),
                "description": s.get("description", ""),
                "package_id": s.get("package_id", ""),
            }
            for s in matches
        ],
    }


async def send_sticker(
    sticker: str = "",
    chat_id: str = "",
    reply_to: str = "",
) -> dict:
    """
    向 chat_id（缺省取当前会话）发送一张内置贴纸（TIMFaceElem）。

    参数：
        sticker:   贴纸名称（如 "六六六"）或 sticker_id（如 "278"）。为空时随机发送一张。
        chat_id:   目标会话；缺省时使用当前会话上下文（HERMES_SESSION_CHAT_ID）。
                   格式：``direct:{account_id}`` / ``group:{group_code}`` / 或裸 account_id。
        reply_to:  群聊场景的引用消息 ID（可选）。

    返回： ``{"success": bool, ...}``
    """
    from gateway.session_context import get_session_env
    from gateway.platforms.yuanbao_sticker import (
        get_sticker_by_id,
        get_sticker_by_name,
        get_random_sticker,
    )

    target = (chat_id or "").strip() or get_session_env("HERMES_SESSION_CHAT_ID", "")
    if not target:
        return {
            "success": False,
            "error": "chat_id is required (no active yuanbao session detected)",
        }

    adapter = _get_active_adapter()
    if adapter is None:
        return {"success": False, "error": "Yuanbao adapter is not connected"}

    raw = (sticker or "").strip()
    sticker_obj: Optional[dict] = None
    if not raw:
        sticker_obj = get_random_sticker()
    else:
        if raw.isdigit():
            sticker_obj = get_sticker_by_id(raw)
        if sticker_obj is None:
            sticker_obj = get_sticker_by_name(raw)

    if sticker_obj is None:
        return {
            "success": False,
            "error": f"Sticker not found: {raw!r}. "
                     f"Use search_sticker first to discover available stickers.",
        }

    try:
        result = await adapter.send_sticker(
            chat_id=target,
            sticker_name=sticker_obj.get("name", ""),
            reply_to=reply_to or None,
        )
    except Exception as exc:
        logger.exception("[yuanbao_tools] send_sticker error")
        return {"success": False, "error": str(exc)}

    if getattr(result, "success", False):
        return {
            "success": True,
            "chat_id": target,
            "sticker": {
                "sticker_id": sticker_obj.get("sticker_id", ""),
                "name": sticker_obj.get("name", ""),
            },
            "message_id": getattr(result, "message_id", None),
            "note": "Sticker delivered to the chat. If you have additional text to say, reply now; otherwise end your turn without generating text.",
        }
    return {
        "success": False,
        "error": getattr(result, "error", "send_sticker failed"),
    }


# 图片扩展名，用于媒体分发（镜像自 MessageSender.IMAGE_EXTS）
_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})


async def send_dm(
    group_code: str,
    name: str,
    message: str,
    user_id: str = "",
    media_files: Optional[List[Tuple[str, bool]]] = None,
) -> dict:
    """
    向群成员发送一条私信（私聊消息），可附带媒体文件。

    工作流程：
      1. 若提供了 user_id，则直接发送。
      2. 否则，按 name 在群成员列表中搜索以解析出 user_id。
      3. 通过 adapter.send_dm() 发送文本，随后按扩展名逐一处理 media_files。

    参数：
        group_code: 目标用户所在的群。
        name: 目标用户的昵称（部分匹配，不区分大小写）。
        message: 要发送的消息文本。
        user_id:（可选）若已知，则跳过成员查找。
        media_files:（可选）(file_path, is_voice) 元组列表，在文本消息
                     之后发送。图片通过 send_image_file 发送；
                     其他文件通过 send_document 发送。
    """
    if not message and not media_files:
        return {"success": False, "error": "message or media_files is required"}

    adapter = _get_active_adapter()
    if adapter is None:
        return {"success": False, "error": "Yuanbao adapter is not connected"}

    resolved_user_id = user_id.strip() if user_id else ""
    resolved_nickname = name.strip()

    # 第 1 步：若未提供 user_id，则从群成员列表中解析
    if not resolved_user_id:
        if not group_code:
            return {"success": False, "error": "group_code is required when user_id is not provided"}
        if not name:
            return {"success": False, "error": "name is required when user_id is not provided"}

        try:
            raw = await adapter.get_group_member_list(group_code)
            if raw is None:
                return {"success": False, "error": "get_group_member_list returned None"}

            members = raw.get("members", [])
            filt = name.strip().lower()
            matched = [
                m for m in members
                if filt in (m.get("nickname") or m.get("nick_name") or "").lower()
            ]

            if not matched:
                return {
                    "success": False,
                    "error": f'No member matching "{name}" found in group {group_code}.',
                }
            if len(matched) > 1:
                # 多个匹配 —— 返回候选项供消歧
                candidates = [
                    {
                        "user_id": m.get("user_id", ""),
                        "nickname": m.get("nickname", m.get("nick_name", "")),
                    }
                    for m in matched
                ]
                return {
                    "success": False,
                    "error": f'Multiple members match "{name}". Please specify which one.',
                    "candidates": candidates,
                }

            resolved_user_id = matched[0].get("user_id", "")
            resolved_nickname = matched[0].get("nickname", matched[0].get("nick_name", name))
        except Exception as exc:
            logger.exception("[yuanbao_tools] send_dm member lookup error")
            return {"success": False, "error": str(exc)}

    if not resolved_user_id:
        return {"success": False, "error": "Could not resolve user_id"}

    # 第 2 步：发送文本私信 + 媒体
    chat_id = f"direct:{resolved_user_id}"
    last_result = None
    errors: list[str] = []
    try:
        if message and message.strip():
            last_result = await adapter.send_dm(resolved_user_id, message, group_code=group_code)
            if not last_result.success:
                errors.append(last_result.error or "text send failed")

        # 第 3 步：发送媒体文件
        for media_path, _is_voice in media_files or []:
            ext = Path(media_path).suffix.lower()
            if ext in _IMAGE_EXTS:
                last_result = await adapter.send_image_file(chat_id, media_path, group_code=group_code)
            else:
                last_result = await adapter.send_document(chat_id, media_path, group_code=group_code)
            if not last_result.success:
                errors.append(last_result.error or "media send failed")

        if last_result is None:
            return {"success": False, "error": "No deliverable text or media remained"}

        if errors and (last_result is None or not last_result.success):
            return {"success": False, "error": "; ".join(errors)}

        result = {
            "success": True,
            "user_id": resolved_user_id,
            "nickname": resolved_nickname,
            "message_id": last_result.message_id,
            "note": f'DM sent to "{resolved_nickname}" successfully.',
        }
        if errors:
            result["note"] += f" (partial failure: {'; '.join(errors)})"
        return result
    except Exception as exc:
        logger.exception("[yuanbao_tools] send_dm error")
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 注册表注册
# ---------------------------------------------------------------------------

from tools.registry import registry, tool_result  # noqa: E402


def _check_yuanbao():
    """工具集可用性检查 —— 当运行于元宝 gateway 会话中时返回 True。"""
    try:
        from gateway.session_context import get_session_env
        if get_session_env("HERMES_SESSION_PLATFORM", "") == "yuanbao":
            return True
    except Exception:
        pass
    return _get_active_adapter() is not None


async def _handle_yb_query_group_info(args, **kw):
    return tool_result(await get_group_info(
        group_code=args.get("group_code", ""),
    ))


async def _handle_yb_query_group_members(args, **kw):
    return tool_result(await query_group_members(
        group_code=args.get("group_code", ""),
        action=args.get("action", "list_all"),
        name=args.get("name", ""),
        mention=bool(args.get("mention", False)),
    ))


async def _handle_yb_send_dm(args, **kw):
    # 解析 group_code：优先使用显式参数，否则回退到会话上下文。
    group_code = args.get("group_code", "")
    if not group_code:
        try:
            from gateway.session_context import get_session_env
            chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
            # chat_id 格式："group:<code>" → 提取 code 部分
            if chat_id.startswith("group:"):
                group_code = chat_id.split(":", 1)[1]
        except Exception:
            pass

    # 解析 media_files：{{"path": str, "is_voice": bool}} 列表 → List[Tuple[str, bool]]
    raw_media = args.get("media_files") or []
    media_files = []
    for item in raw_media:
        if isinstance(item, dict):
            media_files.append((item.get("path", ""), bool(item.get("is_voice", False))))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            media_files.append((str(item[0]), bool(item[1])))

    # 提取嵌入在消息文本中的 MEDIA:<path> 标签（LLM 经常把文件路径放在
    # 这里，而不是使用 media_files 参数）。
    message = args.get("message", "")
    from gateway.platforms.base import BasePlatformAdapter
    embedded_media, message = BasePlatformAdapter.extract_media(message)
    if embedded_media:
        media_files.extend(embedded_media)
    media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)

    return tool_result(await send_dm(
        group_code=group_code,        name=args.get("name", ""),
        message=message,
        user_id=args.get("user_id", ""),
        media_files=media_files or None,
    ))


async def _handle_yb_search_sticker(args, **kw):
    return tool_result(await search_sticker(
        query=args.get("query", ""),
        limit=args.get("limit", 10),
    ))


async def _handle_yb_send_sticker(args, **kw):
    return tool_result(await send_sticker(
        sticker=args.get("sticker", ""),
        chat_id=args.get("chat_id", ""),
        reply_to=args.get("reply_to", ""),
    ))


_TOOLSET = "hermes-yuanbao"

registry.register(
    name="yb_query_group_info",
    toolset=_TOOLSET,
    schema={
        "name": "yb_query_group_info",
        "description": (
            "Query basic info about a group (called '派/Pai' in the app), "
            "including group name, owner, and member count."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "group_code": {
                    "type": "string",
                    "description": "The unique group identifier (group_code).",
                },
            },
            "required": ["group_code"],
        },
    },
    handler=_handle_yb_query_group_info,
    check_fn=_check_yuanbao,
    is_async=True,
    emoji="👥",
)

registry.register(
    name="yb_query_group_members",
    toolset=_TOOLSET,
    schema={
        "name": "yb_query_group_members",
        "description": (
            "Query members of a group (called '派/Pai' in the app). "
            "Use this tool when you need to @mention someone, find a user by name, "
            "list bots (including Yuanbao AI), or list all members. "
            "IMPORTANT: You MUST call this tool before @mentioning any user, "
            "because you need the exact nickname to construct the @mention format."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "group_code": {
                    "type": "string",
                    "description": "The unique group identifier (group_code).",
                },
                "action": {
                    "type": "string",
                    "enum": ["find", "list_bots", "list_all"],
                    "description": (
                        "find — search a user by name (use when you need to @mention or look up someone); "
                        "list_bots — list bots and Yuanbao AI assistants; "
                        "list_all — list all members."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": (
                        "User name to search (partial match, case-insensitive). "
                        "Required for 'find'. Use the name the user mentioned in the conversation."
                    ),
                },
                "mention": {
                    "type": "boolean",
                    "description": (
                        "Set to true when you need to @mention/at someone in your reply. "
                        "The response will include the exact @mention format to use."
                    ),
                },
            },
            "required": ["group_code", "action"],
        },
    },
    handler=_handle_yb_query_group_members,
    check_fn=_check_yuanbao,
    is_async=True,
    emoji="📋",
)

registry.register(
    name="yb_send_dm",
    toolset=_TOOLSET,
    schema={
        "name": "yb_send_dm",
        "description": (
            "Send a private/direct message (DM) to a user in a group, with optional media files. "
            "This tool automatically looks up the user by name in the group member list "
            "and sends the message. Use this when someone asks to privately message / 私信 / DM a user. "
            "Supports text, images, and file attachments. "
            "You can also provide user_id directly if already known."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "group_code": {
                    "type": "string",
                    "description": (
                        "The group where the target user belongs. "
                        "Extract from chat_id: 'group:328306697' → '328306697'. "
                        "Required when user_id is not provided."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Target user's display name (partial match, case-insensitive). "
                        "Required when user_id is not provided."
                    ),
                },
                "message": {
                    "type": "string",
                    "description": "The message text to send as a DM. Can be empty if only sending media.",
                },
                "user_id": {
                    "type": "string",
                    "description": (
                        "Target user's account ID. If provided, skips the member lookup. "
                        "Usually obtained from a previous yb_query_group_members call."
                    ),
                },
                "media_files": {
                    "type": "array",
                    "description": (
                        "Optional list of media files to send along with the DM. "
                        "Images (.jpg/.png/.gif/.webp/.bmp) are sent as image messages; "
                        "other files are sent as document attachments."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Absolute local file path of the media to send.",
                            },
                            "is_voice": {
                                "type": "boolean",
                                "description": "Whether this file is a voice message (default false).",
                            },
                        },
                        "required": ["path"],
                    },
                },
            },
            "required": [],
        },
    },
    handler=_handle_yb_send_dm,
    check_fn=_check_yuanbao,
    is_async=True,
    emoji="✉️",
)


registry.register(
    name="yb_search_sticker",
    toolset=_TOOLSET,
    schema={
        "name": "yb_search_sticker",
        "description": (
            "Search the built-in Yuanbao sticker (TIM face / 表情包) catalogue by keyword. "
            "Returns the top matching candidates with sticker_id, name, and description. "
            "Use this BEFORE yb_send_sticker to discover the right sticker_id. "
            "Sticker = 贴纸 = TIM face — NOT a message reaction. "
            "Prefer sending a sticker over bare Unicode emoji when reacting/expressing emotion."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search keyword (Chinese or English, e.g. '666', '比心', 'cool', '吃瓜'). "
                        "Empty string returns the first N stickers."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of candidates to return (default 10, max 50).",
                },
            },
            "required": [],
        },
    },
    handler=_handle_yb_search_sticker,
    check_fn=_check_yuanbao,
    is_async=True,
    emoji="🔍",
)


registry.register(
    name="yb_send_sticker",
    toolset=_TOOLSET,
    schema={
        "name": "yb_send_sticker",
        "description": (
            "Send a built-in sticker (TIMFaceElem / 贴纸表情) to the current Yuanbao chat. "
            "Call yb_search_sticker first if you don't know the sticker_id/name. "
            "Sticker = 贴纸 = TIM face — NOT a message reaction. "
            "CRITICAL: Whenever the user asks you to send a sticker / 贴纸 / 表情包, you MUST "
            "use this tool. DO NOT draw a PNG via execute_code / Pillow / matplotlib and "
            "then call send_image_file — that produces a fake 'sticker' image instead of a "
            "real TIM face and is the WRONG path. If no suitable sticker_id is known, call "
            "yb_search_sticker first. When the recent thread shows users sending stickers, "
            "prefer matching that tone by replying with a sticker instead of (or in "
            "addition to) text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sticker": {
                    "type": "string",
                    "description": (
                        "Sticker name (e.g. '六六六', '比心', 'ok') or numeric sticker_id "
                        "(e.g. '278'). Empty string sends a random built-in sticker."
                    ),
                },
                "chat_id": {
                    "type": "string",
                    "description": (
                        "Target chat. Defaults to the current session. "
                        "Format: 'direct:{account_id}', 'group:{group_code}', or bare account_id."
                    ),
                },
                "reply_to": {
                    "type": "string",
                    "description": "Optional ref_msg_id to quote-reply (group chat only).",
                },
            },
            "required": [],
        },
    },
    handler=_handle_yb_send_sticker,
    check_fn=_check_yuanbao,
    is_async=True,
    emoji="🎨",
)
