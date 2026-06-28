"""google_meet 插件 — 让 agent 加入 Meet 通话、转录并跟进。

v1：仅转录。通过 Playwright 启动无头 Chromium，加入 Meet
URL，启用实时字幕，将其抓取为转录文件。agent 随后
在工作区中获得转录文件，可以使用常规工具完成所需的后续工作。

v2（不在本 PR 中）：实时双工音频，使 agent 可以在
会议中发言，通过 OpenAI Realtime / Gemini Live + BlackHole / PulseAudio null-sink。
``meet_say`` 目前作为存根存在，以使工具接口稳定。

设计上明确：仅加入显式传入的 ``https://meet.google.com/`` URL。
不进行日历扫描、自动拨号或同意声明。
"""

from __future__ import annotations

import logging
import platform

from plugins.google_meet import process_manager as pm
from plugins.google_meet.cli import register_cli as _register_meet_cli
from plugins.google_meet.cli import meet_command as _meet_command
from plugins.google_meet.tools import (
    MEET_JOIN_SCHEMA,
    MEET_LEAVE_SCHEMA,
    MEET_SAY_SCHEMA,
    MEET_STATUS_SCHEMA,
    MEET_TRANSCRIPT_SCHEMA,
    check_meet_requirements,
    handle_meet_join,
    handle_meet_leave,
    handle_meet_say,
    handle_meet_status,
    handle_meet_transcript,
)

logger = logging.getLogger(__name__)


_TOOLS = (
    ("meet_join",       MEET_JOIN_SCHEMA,       handle_meet_join,       "📞"),
    ("meet_status",     MEET_STATUS_SCHEMA,     handle_meet_status,     "🟢"),
    ("meet_transcript", MEET_TRANSCRIPT_SCHEMA, handle_meet_transcript, "📝"),
    ("meet_leave",      MEET_LEAVE_SCHEMA,      handle_meet_leave,      "👋"),
    ("meet_say",        MEET_SAY_SCHEMA,        handle_meet_say,        "🗣️"),
)


def _on_session_end(**kwargs) -> None:
    """尽力而为的清理 — 如果 meet 机器人在会话结束时仍在运行，
    离开通话以免孤立无头 Chromium。

    没有活跃内容时为空操作。吞噬所有异常 — 会话结束不得因
    机器人清理遇到边缘情况而失败。
    """
    try:
        status = pm.status()
        if status.get("ok") and status.get("alive"):
            pm.stop(reason="session ended")
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("google_meet on_session_end cleanup failed: %s", e)


def register(ctx) -> None:
    """注册工具、CLI 和生命周期钩子。

    当通过 config.yaml 中的 ``plugins.enabled`` 启用插件时，
    由插件加载器调用一次。
    """
    # v1 不支持 Windows — v2 的音频路由在 Windows 上没有经过测试的路径，
    # 且访客加入 Chromium 更不稳定。拒绝注册而非半工作状态。
    system = platform.system().lower()
    if system not in {"linux", "darwin"}:
        logger.info(
            "google_meet plugin: platform=%s not supported (linux/macos only)",
            system,
        )
        return

    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="google_meet",
            schema=schema,
            handler=handler,
            check_fn=check_meet_requirements,
            emoji=emoji,
        )

    ctx.register_cli_command(
        name="meet",
        help="Google Meet bot (join, transcribe, follow up)",
        setup_fn=_register_meet_cli,
        handler_fn=_meet_command,
        description=(
            "Let the hermes agent join a Google Meet call and scrape live "
            "captions into a transcript. See: hermes meet setup"
        ),
    )

    ctx.register_hook("on_session_end", _on_session_end)
