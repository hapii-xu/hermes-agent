"""QQ Bot 内联键盘 + 审批 / 更新提示发送器。

QQ Bot v2 支持在出站消息中附加内联键盘。当用户点击按钮时，平台会派发
一个包含按钮 ``data`` 载荷的 ``INTERACTION_CREATE`` 网关事件。Bot 必须
通过 ``PUT /interactions/{id}`` 及时确认（ACK）该交互，否则用户将在
按钮上看到错误指示图标。

本模块提供：

- :class:`InlineKeyboard` + 按钮数据类 — 序列化为出站消息体中的
  ``keyboard`` 字段。
- :func:`build_approval_keyboard` — 用于工具审批流程的三按钮键盘：
  ✅ 允许一次 / ⭐ 始终允许 / ❌ 拒绝。
- :func:`build_update_prompt_keyboard` — 用于更新确认的是/否键盘。
- :func:`parse_approval_button_data` / :func:`parse_update_prompt_button_data`
  — 解码来自 ``INTERACTION_CREATE`` 的 ``button_data`` 载荷。
- :class:`ApprovalRequest` + :class:`ApprovalSender` — 高层辅助类，
  构建带键盘的审批消息并发送到 c2c / 群聊。

``button_data`` 格式::

    approve:<session_key>:<decision>      # decision = allow-once|allow-always|deny
    update_prompt:<answer>                # answer = y|n

移植自 WideLee 的 qqbot-agent-sdk v1.2.2（``approval.py`` + ``dto.py``
键盘类型），通过 Co-authored-by 保留原作者信息。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── button_data 前缀与匹配模式 ──────────────────────────────────

APPROVAL_BUTTON_PREFIX = "approve:"
UPDATE_PROMPT_PREFIX = "update_prompt:"

# 模式：approve:<session_key>:<decision>
# session_key 本身可能包含冒号（如 agent:main:qqbot:c2c:OPENID），
# 因此 session_key 分组为贪婪匹配，末尾为 decision。
_APPROVAL_DATA_RE = re.compile(
    r"^approve:(.+):(allow-once|allow-always|deny)$"
)

# 模式：update_prompt:y | update_prompt:n
_UPDATE_PROMPT_RE = re.compile(r"^update_prompt:(y|n)$")


# ── 键盘数据类 ─────────────────────────────────────────────

@dataclass
class KeyboardButtonPermission:
    """按钮权限元数据。``type=2`` 表示所有用户均可点击。"""
    type: int = 2

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type}


@dataclass
class KeyboardButtonAction:
    """按钮被点击时触发的动作。

    :param type: ``1``（回调 — 触发 ``INTERACTION_CREATE``）或
        ``2``（链接 — 打开 URL）。
    :param data: 当 ``type=1`` 时，在 ``data.resolved.button_data`` 中
        传递的载荷。
    :param permission: :class:`KeyboardButtonPermission`。
    :param click_limit: 每位用户的最大点击次数（``1`` = 单次使用）。
    """
    type: int
    data: str
    permission: KeyboardButtonPermission = field(
        default_factory=KeyboardButtonPermission
    )
    click_limit: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "data": self.data,
            "permission": self.permission.to_dict(),
            "click_limit": self.click_limit,
        }


@dataclass
class KeyboardButtonRenderData:
    """按钮的视觉渲染数据。

    :param label: 点击前显示的标签。
    :param visited_label: 点击后显示的标签（按钮保持灰色原位）。
    :param style: ``0`` = 灰色，``1`` = 蓝色。
    """
    label: str
    visited_label: str
    style: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "visited_label": self.visited_label,
            "style": self.style,
        }


@dataclass
class KeyboardButton:
    """键盘中的单个按钮。

    :param group_id: 共享同一 ``group_id`` 的按钮互斥 —
        点击其中一个会使其余按钮变灰。
    """
    id: str
    render_data: KeyboardButtonRenderData
    action: KeyboardButtonAction
    group_id: str = "default"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "render_data": self.render_data.to_dict(),
            "action": self.action.to_dict(),
            "group_id": self.group_id,
        }


@dataclass
class KeyboardRow:
    buttons: List[KeyboardButton] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"buttons": [b.to_dict() for b in self.buttons]}


@dataclass
class KeyboardContent:
    rows: List[KeyboardRow] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"rows": [r.to_dict() for r in self.rows]}


@dataclass
class InlineKeyboard:
    """顶层键盘载荷 — 填入 ``MessageToCreate.keyboard`` 字段。"""
    content: KeyboardContent = field(default_factory=KeyboardContent)

    def to_dict(self) -> Dict[str, Any]:
        return {"content": self.content.to_dict()}


# ── INTERACTION_CREATE 解析 ───────────────────────────────────────

def parse_approval_button_data(button_data: str) -> Optional[tuple[str, str]]:
    """将审批 ``button_data`` 解析为 ``(session_key, decision)``。

    :param button_data: 来自 ``INTERACTION_CREATE`` 的原始
        ``data.resolved.button_data``。
    :returns: ``(session_key, decision)`` 元组，若非审批按钮则返回 ``None``。
    """
    m = _APPROVAL_DATA_RE.match(button_data or "")
    if not m:
        return None
    return m.group(1), m.group(2)


def parse_update_prompt_button_data(button_data: str) -> Optional[str]:
    """将更新提示 ``button_data`` 解析为 ``'y'`` 或 ``'n'``。"""
    m = _UPDATE_PROMPT_RE.match(button_data or "")
    if not m:
        return None
    return m.group(1)


# ── 键盘构建器 ────────────────────────────────────────────────

def _make_callback_button(
    btn_id: str,
    label: str,
    visited_label: str,
    data: str,
    style: int,
    group_id: str,
) -> KeyboardButton:
    return KeyboardButton(
        id=btn_id,
        render_data=KeyboardButtonRenderData(
            label=label,
            visited_label=visited_label,
            style=style,
        ),
        action=KeyboardButtonAction(type=1, data=data),
        group_id=group_id,
    )


def build_approval_keyboard(session_key: str) -> InlineKeyboard:
    """构建三按钮审批键盘。

    布局：``[✅ 允许一次] [⭐ 始终允许] [❌ 拒绝]`` — 三者共享
    ``group_id='approval'``，点击其中一个会使其余按钮变灰。

    :param session_key: 嵌入 ``button_data``，以便决策能路由回正确的
        待处理审批。
    """
    return InlineKeyboard(
        content=KeyboardContent(
            rows=[
                KeyboardRow(buttons=[
                    _make_callback_button(
                        btn_id="allow",
                        label="✅ 允许一次",
                        visited_label="已允许",
                        data=f"{APPROVAL_BUTTON_PREFIX}{session_key}:allow-once",
                        style=1,
                        group_id="approval",
                    ),
                    _make_callback_button(
                        btn_id="always",
                        label="⭐ 始终允许",
                        visited_label="已始终允许",
                        data=f"{APPROVAL_BUTTON_PREFIX}{session_key}:allow-always",
                        style=1,
                        group_id="approval",
                    ),
                    _make_callback_button(
                        btn_id="deny",
                        label="❌ 拒绝",
                        visited_label="已拒绝",
                        data=f"{APPROVAL_BUTTON_PREFIX}{session_key}:deny",
                        style=0,
                        group_id="approval",
                    ),
                ]),
            ]
        )
    )


def build_update_prompt_keyboard() -> InlineKeyboard:
    """构建用于更新确认提示的是/否键盘。"""
    return InlineKeyboard(
        content=KeyboardContent(
            rows=[
                KeyboardRow(buttons=[
                    _make_callback_button(
                        btn_id="yes",
                        label="✓ 确认",
                        visited_label="已确认",
                        data=f"{UPDATE_PROMPT_PREFIX}y",
                        style=1,
                        group_id="update_prompt",
                    ),
                    _make_callback_button(
                        btn_id="no",
                        label="✗ 取消",
                        visited_label="已取消",
                        data=f"{UPDATE_PROMPT_PREFIX}n",
                        style=0,
                        group_id="update_prompt",
                    ),
                ]),
            ]
        )
    )


# ── ApprovalRequest + 文本构建器 ───────────────────────────────────

@dataclass
class ApprovalRequest:
    """结构化的审批请求展示数据。

    :param session_key: 将决策路由回等待中的调用方。
    :param title: 顶部的简短标题。
    :param description: 可选的详细描述。
    :param command_preview: 命令文本（exec 审批）。
    :param cwd: 工作目录（exec 审批）。
    :param tool_name: 工具名称（插件审批）。
    :param severity: ``'critical' | 'info' | ''``。
    :param timeout_sec: 审批过期前的等待秒数。
    """
    session_key: str
    title: str
    description: str = ""
    command_preview: str = ""
    cwd: str = ""
    tool_name: str = ""
    severity: str = ""
    timeout_sec: int = 120


def build_approval_text(req: ApprovalRequest) -> str:
    """将 :class:`ApprovalRequest` 渲染为消息正文（markdown 格式）。"""
    if req.command_preview or req.cwd:
        return _build_exec_text(req)
    return _build_plugin_text(req)


def _build_exec_text(req: ApprovalRequest) -> str:
    lines: List[str] = ["🔐 **命令执行审批**", ""]
    if req.command_preview:
        preview = req.command_preview[:300]
        lines.append(f"```\n{preview}\n```")
    if req.cwd:
        lines.append(f"📁 目录: {req.cwd}")
    if req.title and req.title != req.command_preview:
        lines.append(f"📋 {req.title}")
    if req.description:
        lines.append(f"📝 {req.description}")
    lines.append("")
    lines.append(f"⏱️ 超时: {req.timeout_sec} 秒")
    return "\n".join(lines)


def _build_plugin_text(req: ApprovalRequest) -> str:
    icon = (
        "🔴" if req.severity == "critical"
        else "🔵" if req.severity == "info"
        else "🟡"
    )
    lines: List[str] = [f"{icon} **审批请求**", ""]
    lines.append(f"📋 {req.title}")
    if req.description:
        lines.append(f"📝 {req.description}")
    if req.tool_name:
        lines.append(f"🔧 工具: {req.tool_name}")
    lines.append("")
    lines.append(f"⏱️ 超时: {req.timeout_sec} 秒")
    return "\n".join(lines)


# ── ApprovalSender ───────────────────────────────────────────────────

PostMessageFn = Callable[..., Awaitable[Dict[str, Any]]]
"""向 ``/v2/{users|groups}/{id}/messages`` 发送异步 POST 的函数签名。

实现接受一个 body 字典并返回原始 API 响应。
"""


class ApprovalSender:
    """发送带有内联键盘的审批请求消息。

    通过可调用对象与适配器解耦，以便可以独立进行单元测试。
    将适配器的 ``_send_message_with_keyboard`` 辅助方法
    （或任意等价实现）作为 ``post_message`` 传入。
    """

    def __init__(
        self,
        post_c2c: PostMessageFn,
        post_group: PostMessageFn,
        log_tag: str = "QQBot",
    ) -> None:
        self._post_c2c = post_c2c
        self._post_group = post_group
        self._log_tag = log_tag

    async def send(
        self,
        chat_type: str,
        chat_id: str,
        req: ApprovalRequest,
        msg_id: Optional[str] = None,
    ) -> bool:
        """向 *chat_id* 发送审批消息。

        :param chat_type: ``'c2c'`` 或 ``'group'``。
        :param chat_id: 用户 openid 或群组 openid。
        :param req: :class:`ApprovalRequest`。
        :param msg_id: 回复的消息 id（被动消息必须提供）。
        :returns: 成功返回 ``True``，失败返回 ``False``。
        """
        text = build_approval_text(req)
        keyboard = build_approval_keyboard(req.session_key)

        logger.info(
            "[%s] Sending approval request to %s:%s (session=%.20s…)",
            self._log_tag, chat_type, chat_id, req.session_key,
        )

        try:
            if chat_type == "c2c":
                await self._post_c2c(chat_id, text, msg_id, keyboard)
            elif chat_type == "group":
                await self._post_group(chat_id, text, msg_id, keyboard)
            else:
                logger.warning(
                    "[%s] Approval: unsupported chat_type %r",
                    self._log_tag, chat_type,
                )
                return False
            logger.info(
                "[%s] Approval message sent to %s:%s",
                self._log_tag, chat_type, chat_id,
            )
            return True
        except Exception as exc:
            logger.error(
                "[%s] Failed to send approval message to %s:%s: %s",
                self._log_tag, chat_type, chat_id, exc,
            )
            return False


# ── INTERACTION_CREATE 事件结构 ───────────────────────────────────

@dataclass
class InteractionEvent:
    """已解析的 ``INTERACTION_CREATE`` 事件载荷。

    参见 https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html
    """
    id: str = ""
    """交互事件 id — ``PUT /interactions/{id}`` ACK 必须使用此值。"""

    type: int = 0
    """事件类型码（``11`` = 消息按钮）。"""

    chat_type: int = 0
    """``0`` = 频道，``1`` = 群组，``2`` = c2c。"""

    scene: str = ""
    """``'guild'`` | ``'group'`` | ``'c2c'`` — 人类可读的场景标识。"""

    group_openid: str = ""
    group_member_openid: str = ""
    user_openid: str = ""
    channel_id: str = ""
    guild_id: str = ""

    button_data: str = ""
    button_id: str = ""
    resolver_user_id: str = ""

    @property
    def operator_openid(self) -> str:
        """可用的最佳操作者 openid（群组 → 成员；c2c → 用户）。"""
        return (
            self.group_member_openid
            or self.user_openid
            or self.resolver_user_id
        )


def parse_interaction_event(raw: Dict[str, Any]) -> InteractionEvent:
    """解析原始 ``INTERACTION_CREATE`` 派发载荷（``d`` 字段）。"""
    data_raw = raw.get("data") or {}
    resolved = data_raw.get("resolved") or {}
    scene_code = int(raw.get("chat_type", 0) or 0)
    scene = {0: "guild", 1: "group", 2: "c2c"}.get(scene_code, "")
    return InteractionEvent(
        id=str(raw.get("id", "")),
        type=int(data_raw.get("type", 0) or 0),
        chat_type=scene_code,
        scene=scene,
        group_openid=str(raw.get("group_openid", "")),
        group_member_openid=str(raw.get("group_member_openid", "")),
        user_openid=str(raw.get("user_openid", "")),
        channel_id=str(raw.get("channel_id", "")),
        guild_id=str(raw.get("guild_id", "")),
        button_data=str(resolved.get("button_data", "")),
        button_id=str(resolved.get("button_id", "")),
        resolver_user_id=str(resolved.get("user_id", "")),
    )
