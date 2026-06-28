"""CapabilityDescriptor —— relay 握手载荷。EXPERIMENTAL。

connector 在握手时把一个 ``CapabilityDescriptor`` 交给 gateway 的
``RelayAdapter``；它告知 adapter 正在代理哪个平台，以及要向
``GatewayStreamConsumer`` 声明哪些能力（字符上限、draft-streaming、edit/threading
支持、markdown 方言、长度单位）。它是通用化的关键支点：一个 gateway adapter 即可
服务 Discord、Telegram、Matrix、Signal 等，而无需 per-platform 的分支。

EXPERIMENTAL：在至少两个真实的 Class-1 平台验证通过之前，该 schema 可能不经
deprecation 周期而变更。实验阶段的演进是 additive-only 的，由 ``contract_version``
把关（见 docs/relay-connector-contract.md）。

字段来源（多数是 ``PlatformEntry`` 的可序列化投影，加上 ``BasePlatformAdapter`` 上
的 per-instance 能力方法）：

- ``max_message_length`` -> ``PlatformEntry.max_message_length`` / adapter 的
  ``MAX_MESSAGE_LENGTH`` 属性（由 stream_consumer 读取）。
- ``len_unit``           -> 决定 adapter 安装哪个 ``message_len_fn``
  （"chars" = 内置 len；"utf16" = Telegram 风格的 UTF-16 code-unit 计数）。
- ``supports_draft_streaming`` -> adapter 的 ``supports_draft_streaming()`` 探测。
- ``supports_edit``      -> 是否可以做基于 edit 的流式（Discord/Telegram 可以；
  Signal/SMS 不可以 -> consumer 退化为每段一条消息）。
- ``supports_threads``   -> ``create_handoff_thread`` 能力标志。
- ``markdown_dialect``   -> 展示提示（例如 "markdown_v2"、"discord"）。
- ``emoji`` / ``platform_hint`` / ``pii_safe`` -> ``PlatformEntry`` 同名字段。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

# 实验阶段以 additive 方式递增（绝不重新解释已有字段）；破坏性变更需要两个仓库同步
# 更新。
CONTRACT_VERSION = 1


@dataclass(frozen=True)
class CapabilityDescriptor:
    """在 relay 握手时协商出的、不可变的能力 descriptor。

    设为 frozen，以便 descriptor 在握手之后无法被修改——adapter 在连接的整个生命周期
    内声明一份固定的能力画像。
    """

    contract_version: int
    platform: str
    label: str
    max_message_length: int
    supports_draft_streaming: bool
    supports_edit: bool
    supports_threads: bool
    markdown_dialect: str
    len_unit: str  # "chars" | "utf16"
    emoji: str = "\U0001f50c"  # 🔌 默认值（与 PlatformEntry 默认值一致）
    platform_hint: str = ""
    pii_safe: bool = False

    def to_json(self) -> str:
        """序列化为紧凑、稳定的 JSON 字符串，用于握手帧。"""
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> "CapabilityDescriptor":
        """从握手 JSON 字符串反序列化。

        未知 key 会被忽略（前向兼容：一个更新的 connector 可能发送本 gateway 尚不认识
        的字段）；缺失的可选 key 会回退到 dataclass 默认值。
        """
        raw = json.loads(data)
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in raw.items() if k in known}
        return cls(**filtered)

    @classmethod
    def from_platform_entry(
        cls,
        entry,
        *,
        len_unit: str = "chars",
        supports_draft_streaming: bool = False,
        supports_edit: bool = True,
        supports_threads: bool = False,
        markdown_dialect: str = "plain",
    ) -> "CapabilityDescriptor":
        """把一个 ``gateway.platform_registry.PlatformEntry`` 投影为 descriptor。

        用以说明 descriptor 只是 ``PlatformEntry`` 已编码内容的*子集/投影*，而不是一个
        并行的概念：``label``、``max_message_length``、``emoji``、``platform_hint``、
        ``pii_safe`` 以及平台名都直接取自该 entry。``PlatformEntry`` 未编码的运行时
        能力位（长度单位、draft/edit/thread/markdown 行为）由调用方提供——在生产环境中
        connector 从活跃 adapter 的能力方法中填充这些字段。

        ``PlatformEntry`` 上为 0 的 ``max_message_length`` 表示“无限制”；我们把它映射
        为 stream_consumer 的默认值 4096，以便 descriptor 始终携带一个具体的分块上限。
        """
        max_len = getattr(entry, "max_message_length", 0) or 4096
        return cls(
            contract_version=CONTRACT_VERSION,
            platform=entry.name,
            label=entry.label,
            max_message_length=max_len,
            supports_draft_streaming=supports_draft_streaming,
            supports_edit=supports_edit,
            supports_threads=supports_threads,
            markdown_dialect=markdown_dialect,
            len_unit=len_unit,
            emoji=getattr(entry, "emoji", "\U0001f50c"),
            platform_hint=getattr(entry, "platform_hint", ""),
            pii_safe=getattr(entry, "pii_safe", False),
        )
