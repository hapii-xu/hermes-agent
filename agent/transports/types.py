"""规范化提供商响应的共享类型。

这些数据类定义了所有提供商适配器将响应规范化后的标准结构。
共享接口面被刻意保持最小——只有每个下游消费者都会读取的字段才作为顶层字段。
协议特定的状态存放在 ``provider_data`` 字典中（响应级别和每个工具调用级别），
以便协议感知的代码路径可以访问而不污染共享类型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """来自任何提供商的规范化工具调用。

    ``id`` 是协议的标准标识符——用于构建工具结果消息时的
    ``tool_call_id`` / ``tool_use_id``。当提供商省略此字段时可能为 ``None``；
    agent 会在存入历史记录前通过 ``_deterministic_call_id()`` 填充它。

    ``provider_data`` 携带每个工具调用的协议元数据，仅由协议感知的代码读取：

    * Codex: ``{"call_id": "call_XXX", "response_item_id": "fc_XXX"}``
    * Gemini: ``{"extra_content": {"google": {"thought_signature": "..."}}}``
    * Others: ``None``
    """

    id: str | None
    name: str
    arguments: str  # JSON string
    provider_data: dict[str, Any] | None = field(default=None, repr=False)

    # ── 向后兼容 ──────────────────────────────────────────────────
    # agent 循环在 run_agent.py 中（45+ 处）读取 tc.function.name / tc.function.arguments。
    # 这些属性让 NormalizedResponse 能够直接传递而无需 _nr_to_assistant_message 垫片，
    # 同时保持 ToolCall 的标准字段扁平化。
    @property
    def type(self) -> str:
        return "function"

    @property
    def function(self) -> ToolCall:
        """返回 self，使 tc.function.name / tc.function.arguments 可用。"""
        return self

    @property
    def call_id(self) -> str | None:
        """provider_data 中的 Codex call_id，由 _build_assistant_message 通过 getattr 访问。"""
        return (self.provider_data or {}).get("call_id")

    @property
    def response_item_id(self) -> str | None:
        """provider_data 中的 Codex response_item_id。"""
        return (self.provider_data or {}).get("response_item_id")

    @property
    def extra_content(self) -> dict[str, Any] | None:
        """provider_data 中的 Gemini extra_content（thought_signature）。

        Gemini 3 思考模型会在每个工具调用上附加带有 ``thought_signature`` 的 ``extra_content``。
        该签名必须在后续 API 调用中重放——若缺失，API 会以 HTTP 400 拒绝请求。
        chat_completions 传输层将其存储在 ``provider_data["extra_content"]`` 中；
        此属性将其暴露出来，以便 ``_build_assistant_message`` 可以统一使用
        ``getattr(tc, "extra_content")``。
        """
        return (self.provider_data or {}).get("extra_content")


@dataclass
class Usage:
    """API 响应中的 token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0


@dataclass
class NormalizedResponse:
    """来自任何提供商的规范化 API 响应。

    共享字段是真正跨提供商的——每个调用者都可以依赖它们，
    无需根据 api_mode 进行分支。协议特定的状态存放在 ``provider_data``
    中，以便只有协议感知的代码路径才读取它。

    响应级 ``provider_data`` 示例：

    * Anthropic: ``{"reasoning_details": [...]}``
    * Codex: ``{"codex_reasoning_items": [...], "codex_message_items": [...]}``
    * Others: ``None``
    """

    content: str | None
    tool_calls: list[ToolCall] | None
    finish_reason: str  # "stop"（停止）、"tool_calls"（工具调用）、"length"（长度限制）、"content_filter"（内容过滤）
    reasoning: str | None = None
    usage: Usage | None = None
    provider_data: dict[str, Any] | None = field(default=None, repr=False)

    # ── 向后兼容 ──────────────────────────────────────────────────
    # 垫片 _nr_to_assistant_message() 曾从 provider_data 映射这些字段。
    # 这些属性让 NormalizedResponse 可以直接传递。
    @property
    def reasoning_content(self) -> str | None:
        pd = self.provider_data or {}
        return pd.get("reasoning_content")

    @property
    def reasoning_details(self):
        pd = self.provider_data or {}
        return pd.get("reasoning_details")

    @property
    def anthropic_content_blocks(self):
        """某轮次中逐字、保序的 Anthropic 内容块。

        仅当 Anthropic 轮次将有签名的思考块与 tool_use 交替出现时才存在——
        这是并行 reasoning_details + tool_calls 列表重建时顺序错误的唯一情形，
        会使重放时思考块签名失效。参见 agent/transports/anthropic.py。
        """
        pd = self.provider_data or {}
        return pd.get("anthropic_content_blocks")

    @property
    def codex_reasoning_items(self):
        pd = self.provider_data or {}
        return pd.get("codex_reasoning_items")

    @property
    def codex_message_items(self):
        pd = self.provider_data or {}
        return pd.get("codex_message_items")


# ---------------------------------------------------------------------------
# 工厂辅助函数
# ---------------------------------------------------------------------------


def build_tool_call(
    id: str | None,
    name: str,
    arguments: Any,
    **provider_fields: Any,
) -> ToolCall:
    """构建一个 ``ToolCall``，若 *arguments* 是字典则自动序列化。

    任何额外的关键字参数都会被收集到 ``provider_data`` 中。
    """
    args_str = json.dumps(arguments) if isinstance(arguments, dict) else str(arguments)
    pd = dict(provider_fields) if provider_fields else None
    return ToolCall(id=id, name=name, arguments=args_str, provider_data=pd)


def map_finish_reason(reason: str | None, mapping: dict[str, str]) -> str:
    """将提供商特定的停止原因翻译为规范化集合中的值。

    对于未知或 ``None`` 的原因，回退为 ``"stop"``。
    """
    if reason is None:
        return "stop"
    return mapping.get(reason, "stop")
