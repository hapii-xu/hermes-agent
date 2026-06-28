"""用于规范化 WhatsApp 发送者身份的共享辅助函数。

WhatsApp 的 bridge 可能在同一次对话中以两种不同的 JID 形态呈现同一个人：

- LID 形态：``999999999999999@lid``
- 电话形态：``15551234567@s.whatsapp.net``

授权路径（:mod:`gateway.run`）和会话键路径（:mod:`gateway.session`）都
需要把这些别名收敛为单一稳定的身份。本模块是该解析逻辑的唯一真相来源，
从而保证两条路径永远不会彼此漂移。

公开的辅助函数：

- :func:`normalize_whatsapp_identifier` —— 剥离 JID/LID/device/plus 语法，
  只保留纯数字标识符。
- :func:`canonical_whatsapp_identifier` —— 遍历 bridge 的
  ``lid-mapping-*.json`` 文件，并跨电话/LID 变体返回一个稳定的规范身份。
- :func:`expand_whatsapp_aliases` —— 返回某个标识符的完整别名集合。供
  授权代码使用，这类代码需要将发送者的任意已知形态与 allow-list 匹配。

需要在 WhatsApp 上做按发送者行为的插件（基于角色的路由、按联系人的授权、
gateway 钩子里的策略门控）应当使用 ``canonical_whatsapp_identifier``，
这样它们自己的记录才能与 Hermes 自身的会话键保持一致。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Set

logger = logging.getLogger(__name__)

# WhatsApp JID 是数字（或带 plus 前缀的数字），并可带有可选的 ``@``、
# ``.`` 和 ``:`` 分隔符。``\w`` 被固定为 ASCII，这样全角数字 /
# Unicode 单词字符就无法混进来。
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9@.+\-]+$")

from hermes_constants import get_hermes_home


def normalize_whatsapp_identifier(value: str) -> str:
    """剥离 WhatsApp JID/LID 语法，只保留稳定的数字标识符。

    接受 WhatsApp bridge 可能输出的任意标识符形态：
    ``"60123456789@s.whatsapp.net"``、``"60123456789:47@s.whatsapp.net"``、
    ``"60123456789@lid"``，或纯 ``"+601****6789"`` / ``"60123456789"``。
    返回纯数字标识符（``"60123456789"``），适合用于相等性比较。

    对于那些希望把发送者 ID 与用户提供的配置（``config.yaml`` 中的电话
    号码）做匹配、而不必关心 bridge 恰好投递的是哪种变体的插件，本函数
    很有用。
    """
    return (
        str(value or "")
        .strip()
        .replace("+", "", 1)
        .split(":", 1)[0]
        .split("@", 1)[0]
    )


# 一个“只是电话号码”的目标 —— 可选的前导 ``+``，后跟数字以及常见的
# 人类分隔符（空格、点、短横线、括号）。任何已经带有 ``@`` 的都是完全
# 合格的 JID，必须原样放行（群组 ``@g.us``、LID ``@lid``、
# ``status@broadcast`` 等）。
_BARE_PHONE_RE = re.compile(r"^\+?[\d\s().\-]+$")


def to_whatsapp_jid(value: str) -> str:
    """把一个 *出站（outbound）* 的 WhatsApp 目标规范化为 bridge 安全的 JID。

    Baileys 的 ``jidDecode`` 在遇到纯电话号码时会崩溃——它期望的是一个
    完全合格的 JID，例如 ``50766715226@s.whatsapp.net``。本辅助函数是
    :func:`normalize_whatsapp_identifier` 的逆操作：它不是把 JID 剥离成
    数字核心用于比较，而是 *构造* 出发送时必须使用的 JID。

    行为：

    - ``"+50766715226"`` / ``"50766715226"`` → ``"50766715226@s.whatsapp.net"``
    - ``"50766715226@s.whatsapp.net"`` → 不变
    - ``"group-id@g.us"`` / ``"130631430344750@lid"`` → 不变
    - ``"user:device@s.whatsapp.net"`` 这种冒号在 ``@`` 之前的形态 → ``@`` 形态
    - 任何无法识别为纯电话号码的内容 → 原样返回，这样 bridge 就能给出
      一个有意义的错误，而不是被我们弄坏。

    对空/空白输入返回 ``""``。
    """
    if not value:
        return ""

    normalized = str(value).strip()
    # 去掉域名之前的 device 后缀：``user:device@domain`` 是 Baileys 的一种
    # 遗留形态，其 ``:device`` 部分不可寻址——把它折叠为 ``user@domain``。
    # （与 normalize_whatsapp_identifier 对应，后者出于同样的原因按 ``:``
    # 拆分纯 id。）
    if ":" in normalized and "@" in normalized:
        prefix, _, domain = normalized.partition("@")
        normalized = f"{prefix.split(':', 1)[0]}@{domain}"

    # 已经是完全合格的 JID —— 原样保留。
    if "@" in normalized:
        return normalized

    if _BARE_PHONE_RE.fullmatch(normalized):
        digits = re.sub(r"\D+", "", normalized)
        if digits:
            return f"{digits}@s.whatsapp.net"

    return normalized


def expand_whatsapp_aliases(identifier: str) -> Set[str]:
    """通过 bridge 的会话映射文件解析 WhatsApp 电话/LID 别名。

    返回从 ``identifier`` 出发、经由 bridge 的
    ``$HERMES_HOME/whatsapp/session/lid-mapping-*.json`` 文件传递可达的
    所有标识符集合。结果中总是包含规范化的输入本身，因此调用方可以
    安全地用 ``in`` 检查返回值，而无需单独的回退分支。

    如果 ``identifier`` 规范化后为空，则返回空集合。
    """
    normalized = normalize_whatsapp_identifier(identifier)
    if not normalized:
        return set()

    session_dir = get_hermes_home() / "whatsapp" / "session"
    resolved: Set[str] = set()
    queue = [normalized]

    while queue:
        current = queue.pop(0)
        if not current or current in resolved:
            continue
        # 纵深防御：拒绝那些可能把路径分隔符 / 遍历片段混入下方
        # ``lid-mapping-{current}`` 文件名的标识符。硬编码的 ``lid-mapping-``
        # 前缀已经能通过 pathlib 的组件拆分阻止逃逸（攻击者无法在
        # session_dir 中创建一个真正的 ``lid-mapping-..`` 目录），但这一步
        # 进一步把标识符的字符空间限制在 WhatsApp JID 实际使用的字符范围内，
        # 避免依赖那个文件系统布局的不变性。
        if not _SAFE_IDENTIFIER_RE.match(current):
            continue

        resolved.add(current)
        for suffix in ("", "_reverse"):
            mapping_path = session_dir / f"lid-mapping-{current}{suffix}.json"
            if not mapping_path.exists():
                continue
            try:
                mapped = normalize_whatsapp_identifier(
                    json.loads(mapping_path.read_text(encoding="utf-8"))
                )
            except (OSError, json.JSONDecodeError) as exc:
                logger.debug("whatsapp_identity: failed to read %s: %s", mapping_path, exc)
                continue
            if mapped and mapped not in resolved:
                queue.append(mapped)

    return resolved


def canonical_whatsapp_identifier(identifier: str) -> str:
    """跨电话-JID/LID 变体返回稳定的 WhatsApp 发送者身份。

    WhatsApp 可能在电话格式 JID（``60123456789@s.whatsapp.net``）或 LID
    （``1234567890@lid``）这两种形态下呈现同一个人。这既适用于 DM 的
    ``chat_id``，也适用于群聊中某个成员的 ``participant_id`` —— 两者都
    代表一个用户身份，而且 bridge 可能为同一个人在两者之间来回切换。

    本辅助函数读取 bridge 的 ``whatsapp/session/lid-mapping-*.json``
    文件，传递地遍历映射，并挑选最短（优先数字）的别名作为规范身份。
    :func:`gateway.session.build_session_key` 对 WhatsApp DM chat_id 和
    WhatsApp 群组 participant_id 都使用了本函数，因此调用方会得到与
    Hermes 自身使用的相同的会话键身份。

    需要按发送者行为的插件（基于角色的路由、授权、按联系人的策略）
    应当使用本函数，这样即使 bridge 重新洗牌别名，它们自己的记录也能
    与 Hermes 的会话记录保持一致。

    如果 ``identifier`` 规范化后为空，则返回空字符串。如果还没有任何
    映射文件（全新 bridge 安装），则原样返回规范化的输入。
    """
    normalized = normalize_whatsapp_identifier(identifier)
    if not normalized:
        return ""

    # expand_whatsapp_aliases 返回的集合总是包含 `normalized` 自身，
    # 因此下面的 min() 在没有任何 lid-mapping 文件时会优雅地回退为
    # `normalized`。
    aliases = expand_whatsapp_aliases(normalized)
    return min(aliases, key=lambda candidate: (len(candidate), candidate))
