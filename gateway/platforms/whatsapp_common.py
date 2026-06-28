"""
传输无关的 WhatsApp 行为，由 Baileys bridge 适配器和
官方 WhatsApp Cloud API 适配器共享。

该 mixin 提供：
- 白名单 / DM / 群组门控
- mention 检测（显式 @-mention + 可配置的 regex 模式）
- 引用回复到 bot 的检测
- 广播 / Channel / Newsletter 过滤
- WhatsApp 风格的 Markdown 转换
- 出站分块长度预算

它是 *行为层*。传输相关的关注点（子进程管理、HTTP webhook、
Graph API 调用、媒体上传协议）位于各适配器中。

Mixin 契约 —— 适配器必须在调用 mixin 的任何方法之前在 ``self`` 上
设置以下属性（通常在 ``__init__`` 中）：

    self.config        # gateway.config.PlatformConfig
    self.name          # str — 适配器名称（用于日志行）
    self._dm_policy             # str: "open" | "allowlist" | "disabled"
    self._allow_from            # set[str]
    self._group_policy          # str: "open" | "allowlist" | "disabled"
    self._group_allow_from      # set[str]
    self._mention_patterns      # list[re.Pattern]
    self._reply_prefix          # Optional[str]

类属性 ``MAX_MESSAGE_LENGTH`` 和 ``DEFAULT_REPLY_PREFIX`` 定义在 mixin
上，如果需要可在各适配器中覆盖。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, Optional


logger = logging.getLogger(__name__)


class WhatsAppBehaviorMixin:
    """所有 WhatsApp 适配器（Baileys + Cloud API）的共享行为。

    属性契约见模块 docstring —— 宿主适配器必须满足。该 mixin 自身不持有
    任何状态 —— 它访问的每个值要么是类属性，要么由适配器的 ``__init__``
    设置。
    """

    # WhatsApp 消息长度上限 —— 实际的 UX 限制，而非协议最大值。
    # WhatsApp 允许约 65K，但长消息在移动端不可读。
    MAX_MESSAGE_LENGTH: int = 4096
    supports_code_blocks = True  # WhatsApp 渲染围栏代码块（monospace）

    DEFAULT_REPLY_PREFIX: str = "⚕ *Hermes Agent*\n────────────\n"

    @property
    def enforces_own_access_policy(self) -> bool:
        """WhatsApp 在入口处通过 dm_policy/group_policy 对 DM/群组访问进行门控。"""
        return True

    # ------------------------------------------------------------------ config
    def _effective_reply_prefix(self) -> str:
        """返回在 self-chat 模式下要附加到出站回复的前缀。

        没有 self-chat 概念的子类（Cloud API 适配器）可覆盖此方法，
        使其始终返回 ``""`` 或应用不同的策略。
        """
        whatsapp_mode = os.getenv("WHATSAPP_MODE", "self-chat")
        if whatsapp_mode != "self-chat":
            return ""
        if self._reply_prefix is not None:
            return self._reply_prefix.replace("\\n", "\n")
        env_prefix = os.getenv("WHATSAPP_REPLY_PREFIX")
        if env_prefix is not None:
            return env_prefix.replace("\\n", "\n")
        return self.DEFAULT_REPLY_PREFIX

    def _outgoing_chunk_limit(self) -> int:
        """为回复前缀预留空间，使最终消息能完整放入。"""
        prefix_len = len(self._effective_reply_prefix())
        # 即使配置了很长的前缀，也要为 truncate_message 的分页指示符和
        # 代码围栏修复保留足够空间。
        return max(1024, self.MAX_MESSAGE_LENGTH - prefix_len)

    def _whatsapp_require_mention(self) -> bool:
        configured = self.config.extra.get("require_mention")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in {"true", "1", "yes", "on"}
            return bool(configured)
        return os.getenv("WHATSAPP_REQUIRE_MENTION", "false").lower() in {
            "true",
            "1",
            "yes",
            "on",
        }

    def _whatsapp_free_response_chats(self) -> set[str]:
        raw = self.config.extra.get("free_response_chats")
        if raw is None:
            raw = os.getenv("WHATSAPP_FREE_RESPONSE_CHATS", "")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}

    @staticmethod
    def _coerce_allow_list(raw) -> set[str]:
        """从 config 或环境变量解析 allow_from / group_allow_from。"""
        if raw is None:
            return set()
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}

    # ------------------------------------------------------------------ JID helpers
    @staticmethod
    def _normalize_whatsapp_id(value: Optional[str]) -> str:
        if not value:
            return ""
        normalized = str(value).strip()
        if ":" in normalized and "@" in normalized:
            normalized = normalized.replace(":", "@", 1)
        return normalized

    @staticmethod
    def _is_broadcast_chat(chat_id: str) -> bool:
        """针对 WhatsApp 中并非真实会话的伪聊天返回 True。

        覆盖 Status 更新（Stories）和 Channel/Newsletter 广播。
        它们在 Baileys 上显示为入站消息，但 agent 永远不应回复 ——
        回复 Story 更新会向联系人的状态 feed 发送垃圾信息，
        而 Channel 帖子本身也不可寻址。
        """
        if not chat_id:
            return False
        cid = chat_id.strip().lower()
        if cid == "status@broadcast":
            return True
        # @broadcast 后缀覆盖 status@broadcast 以及任何未来的
        # 广播列表变体。@newsletter 是 Channel 的 JID 后缀。
        if cid.endswith("@broadcast") or cid.endswith("@newsletter"):
            return True
        return False

    # ------------------------------------------------------------------ gating
    def _is_dm_allowed(self, sender_id: str) -> bool:
        """检查是否应处理来自指定发送者的 DM。"""
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return sender_id in self._allow_from
        # "open" —— 允许所有 DM
        return True

    def _is_group_allowed(self, chat_id: str) -> bool:
        """检查是否应处理某个群组会话。"""
        if self._group_policy == "disabled":
            return False
        if self._group_policy == "allowlist":
            return chat_id in self._group_allow_from
        # "open" —— 允许所有群组
        return True

    def _compile_mention_patterns(self):
        patterns = self.config.extra.get("mention_patterns")
        if patterns is None:
            raw = os.getenv("WHATSAPP_MENTION_PATTERNS", "").strip()
            if raw:
                try:
                    patterns = json.loads(raw)
                except Exception:
                    patterns = [
                        part.strip() for part in raw.splitlines() if part.strip()
                    ]
                    if not patterns:
                        patterns = [
                            part.strip() for part in raw.split(",") if part.strip()
                        ]
        if patterns is None:
            return []
        if isinstance(patterns, str):
            patterns = [patterns]
        if not isinstance(patterns, list):
            logger.warning(
                "[%s] whatsapp mention_patterns must be a list or string; got %s",
                self.name,
                type(patterns).__name__,
            )
            return []

        compiled = []
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            try:
                compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                logger.warning(
                    "[%s] Invalid WhatsApp mention pattern %r: %s",
                    self.name,
                    pattern,
                    exc,
                )
        if compiled:
            logger.info(
                "[%s] Loaded %d WhatsApp mention pattern(s)", self.name, len(compiled)
            )
        return compiled

    def _bot_ids_from_message(self, data: Dict[str, Any]) -> set[str]:
        bot_ids = set()
        for candidate in data.get("botIds") or []:
            normalized = self._normalize_whatsapp_id(candidate)
            if normalized:
                bot_ids.add(normalized)
        return bot_ids

    def _message_is_reply_to_bot(self, data: Dict[str, Any]) -> bool:
        quoted_participant = self._normalize_whatsapp_id(data.get("quotedParticipant"))
        if not quoted_participant:
            return False
        return quoted_participant in self._bot_ids_from_message(data)

    def _message_mentions_bot(self, data: Dict[str, Any]) -> bool:
        bot_ids = self._bot_ids_from_message(data)
        if not bot_ids:
            return False
        mentioned_ids = {
            nid
            for candidate in (data.get("mentionedIds") or [])
            if (nid := self._normalize_whatsapp_id(candidate))
        }
        if mentioned_ids & bot_ids:
            return True

        body = str(data.get("body") or "")
        lower_body = body.lower()
        for bot_id in bot_ids:
            bare_id = bot_id.split("@", 1)[0].lower()
            if bare_id and (f"@{bare_id}" in lower_body or bare_id in lower_body):
                return True
        return False

    def _message_matches_mention_patterns(self, data: Dict[str, Any]) -> bool:
        if not self._mention_patterns:
            return False
        body = str(data.get("body") or "")
        return any(pattern.search(body) for pattern in self._mention_patterns)

    def _clean_bot_mention_text(self, text: str, data: Dict[str, Any]) -> str:
        if not text:
            return text
        bot_ids = self._bot_ids_from_message(data)
        cleaned = text
        for bot_id in bot_ids:
            bare_id = bot_id.split("@", 1)[0]
            if bare_id:
                cleaned = re.sub(
                    rf"@{re.escape(bare_id)}\b[,:\-]*\s*", "", cleaned
                )
        return cleaned.strip() or text

    def _should_process_message(self, data: Dict[str, Any]) -> bool:
        chat_id_raw = str(data.get("chatId") or "")
        # WhatsApp 用伪聊天来承载 Status 更新（Stories）和
        # Channel/Newsletter 广播。这些不是真实会话，agent 永远不应回复 ——
        # 即使在 self-chat 模式下 bridge 可能将它们呈现为 "fromMe" 事件。
        if self._is_broadcast_chat(chat_id_raw):
            return False
        is_group = data.get("isGroup", False)
        if is_group:
            chat_id = chat_id_raw
            if not self._is_group_allowed(chat_id):
                return False
        else:
            sender_id = str(data.get("senderId") or data.get("from") or "")
            if not self._is_dm_allowed(sender_id):
                return False
            # 通过策略门控的 DM 总是被处理
            return True
        # 群组消息：检查 mention / free-response 设置
        chat_id = str(data.get("chatId") or "")
        if chat_id in self._whatsapp_free_response_chats():
            return True
        if not self._whatsapp_require_mention():
            return True
        body = str(data.get("body") or "").strip()
        if body.startswith("/"):
            return True
        if self._message_is_reply_to_bot(data):
            return True
        if self._message_mentions_bot(data):
            return True
        return self._message_matches_mention_patterns(data)

    # ------------------------------------------------------------------ formatting
    def format_message(self, content: str) -> str:
        """将标准 Markdown 转换为 WhatsApp 兼容的格式。

        WhatsApp 支持：*bold*、_italic_、~strikethrough~、```code```，
        以及 monospace 的 `inline`。标准 Markdown 对
        bold/italic/strikethrough 使用不同的语法，因此在此转换。

        代码块（``` 围栏）和 inline code（`）通过占位符替换
        免受转换影响。
        """
        if not content:
            return content

        # --- 1. 保护围栏代码块免受格式化改动 ---
        _FENCE_PH = "\x00FENCE"
        fences: list[str] = []

        def _save_fence(m: re.Match) -> str:
            fences.append(m.group(0))
            return f"{_FENCE_PH}{len(fences) - 1}\x00"

        result = re.sub(r"```[\s\S]*?```", _save_fence, content)

        # --- 2. 保护 inline code ---
        _CODE_PH = "\x00CODE"
        codes: list[str] = []

        def _save_code(m: re.Match) -> str:
            codes.append(m.group(0))
            return f"{_CODE_PH}{len(codes) - 1}\x00"

        result = re.sub(r"`[^`\n]+`", _save_code, result)

        # --- 3. 将 Markdown 格式转换为 WhatsApp 语法 ---
        # Bold：**text** 或 __text__ → *text*
        result = re.sub(r"\*\*(.+?)\*\*", r"*\1*", result)
        result = re.sub(r"__(.+?)__", r"*\1*", result)
        # Strikethrough：~~text~~ → ~text~
        result = re.sub(r"~~(.+?)~~", r"~\1~", result)
        # Italic：*text* 已是 WhatsApp italic —— 保持不变
        # _text_ 已是 WhatsApp italic —— 保持不变

        # --- 4. 将 Markdown 标题转换为 bold 文本 ---
        # # Header → *Header*。去掉第 3 步已生成的 *...* 包裹
        # （例如 "# **Title**" → "*Title*"，而不是 "**Title**"，
        # 后者 WhatsApp 会渲染出字面的星号）。
        def _header_to_bold(m: re.Match) -> str:
            inner = m.group(1).strip()
            while len(inner) > 1 and inner.startswith("*") and inner.endswith("*"):
                inner = inner[1:-1].strip()
            return f"*{inner}*"

        result = re.sub(
            r"^#{1,6}\s+(.+)$", _header_to_bold, result, flags=re.MULTILINE
        )

        # --- 5. 转换 Markdown 链接：[text](url) → text (url) ---
        result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", result)

        # --- 6. 恢复被保护的区段 ---
        for i, fence in enumerate(fences):
            result = result.replace(f"{_FENCE_PH}{i}\x00", fence)
        for i, code in enumerate(codes):
            result = result.replace(f"{_CODE_PH}{i}\x00", code)

        return result


# ---------------------------------------------------------------------------
# CLI 和适配器共享的 bridge 目录解析
# ---------------------------------------------------------------------------

def resolve_whatsapp_bridge_dir() -> Path:
    """解析 WhatsApp bridge 目录，必要时镜像到 HERMES_HOME。

    当安装目录只读时（例如 Docker /opt/hermes），此函数将 bridge 源码
    镜像到一个可写的 HERMES_HOME 位置并返回该路径。
    这确保 npm install 能在 Docker 环境中正常工作。

    返回解析后的 bridge 目录路径。
    """
    import shutil
    from pathlib import Path as _Path

    # 安装目录中的默认位置（可能只读）
    from hermes_constants import get_hermes_home
    install_bridge = _Path(__file__).resolve().parents[2] / "scripts" / "whatsapp-bridge"

    # 优先尝试 HERMES_HOME 位置
    hermes_home = get_hermes_home()
    hermes_home_bridge = hermes_home / "scripts" / "whatsapp-bridge"

    # 检查安装目录是否可写
    try:
        test_file = install_bridge / ".write_test"
        test_file.touch()
        test_file.unlink()
        install_writable = True
    except (OSError, PermissionError):
        install_writable = False

    if install_writable:
        return install_bridge

    # 安装目录只读，必要时镜像到 HERMES_HOME
    if hermes_home_bridge.exists():
        return hermes_home_bridge

    # 将 bridge 源码镜像到 HERMES_HOME
    try:
        hermes_home_bridge.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            install_bridge,
            hermes_home_bridge,
            dirs_exist_ok=False,
        )
        return hermes_home_bridge
    except Exception:
        return install_bridge
