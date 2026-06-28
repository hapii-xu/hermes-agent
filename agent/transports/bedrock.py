"""AWS Bedrock Converse API 传输层。

委托给 agent/bedrock_adapter.py 中已有的适配器函数。
Bedrock 使用自己的 boto3 客户端（而非 OpenAI SDK），因此该传输层
负责格式转换与规范化，而客户端构建和 boto3 调用保留在 AIAgent 中。
"""

from typing import Any, Dict, List, Optional

from agent.transports.base import ProviderTransport
from agent.transports.types import NormalizedResponse, ToolCall, Usage


class BedrockTransport(ProviderTransport):
    """api_mode='bedrock_converse' 的传输实现。"""

    @property
    def api_mode(self) -> str:
        return "bedrock_converse"

    def convert_messages(self, messages: List[Dict[str, Any]], **kwargs) -> Any:
        """将 OpenAI 消息转换为 Bedrock Converse 格式。"""
        from agent.bedrock_adapter import convert_messages_to_converse
        return convert_messages_to_converse(messages)

    def convert_tools(self, tools: List[Dict[str, Any]]) -> Any:
        """将 OpenAI 工具 schema 转换为 Bedrock Converse toolConfig。"""
        from agent.bedrock_adapter import convert_tools_to_converse
        return convert_tools_to_converse(tools)

    def build_kwargs(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **params,
    ) -> Dict[str, Any]:
        """构建 Bedrock converse() 的关键字参数。

        内部会调用 convert_messages 和 convert_tools。

        params:
            max_tokens: int — 输出 token 限制（默认 4096）
            temperature: float | None
            guardrail_config: dict | None — Bedrock 护栏配置
            region: str — AWS 区域（默认 'us-east-1'）
        """
        from agent.bedrock_adapter import build_converse_kwargs

        region = params.get("region", "us-east-1")
        guardrail = params.get("guardrail_config")

        kwargs = build_converse_kwargs(
            model=model,
            messages=messages,
            tools=tools,
            max_tokens=params.get("max_tokens", 4096),
            temperature=params.get("temperature"),
            guardrail_config=guardrail,
        )
        # 用于分发的哨兵键——agent 在调用 boto3 前会弹出这些键
        kwargs["__bedrock_converse__"] = True
        kwargs["__bedrock_region__"] = region
        return kwargs

    def normalize_response(self, response: Any, **kwargs) -> NormalizedResponse:
        """将 Bedrock 响应规范化为 NormalizedResponse。

        处理两种形态：
        1. 原始 boto3 字典（来自直接 converse() 调用）
        2. 已规范化的 SimpleNamespace（带有 .choices，来自分发侧）
        """
        from agent.bedrock_adapter import normalize_converse_response

        # 规范化为 OpenAI 兼容的 SimpleNamespace
        if hasattr(response, "choices") and response.choices:
            # 已在分发侧完成规范化
            ns = response
        else:
            # 原始 boto3 字典
            ns = normalize_converse_response(response)

        choice = ns.choices[0]
        msg = choice.message
        finish_reason = choice.finish_reason or "stop"

        tool_calls = None
        if msg.tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=tc.function.arguments,
                )
                for tc in msg.tool_calls
            ]

        usage = None
        if hasattr(ns, "usage") and ns.usage:
            u = ns.usage
            usage = Usage(
                prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(u, "completion_tokens", 0) or 0,
                total_tokens=getattr(u, "total_tokens", 0) or 0,
            )

        reasoning = getattr(msg, "reasoning", None) or getattr(msg, "reasoning_content", None)

        return NormalizedResponse(
            content=msg.content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning=reasoning,
            usage=usage,
        )

    def validate_response(self, response: Any) -> bool:
        """检查 Bedrock 响应结构。

        经 normalize_converse_response 处理后，响应具有 OpenAI 兼容的
        .choices——与 chat_completions 的检查方式相同。
        """
        if response is None:
            return False
        # 原始 Bedrock 字典响应——检查是否包含 'output' 键
        if isinstance(response, dict):
            return "output" in response
        # 已规范化的 SimpleNamespace
        if hasattr(response, "choices"):
            return bool(response.choices)
        return False

    def map_finish_reason(self, raw_reason: str) -> str:
        """将 Bedrock 停止原因映射为 OpenAI finish_reason。

        适配器已在 normalize_converse_response 内部完成此映射，
        因此此方法仅用于直接访问原始响应时。
        """
        _MAP = {
            "end_turn": "stop",
            "tool_use": "tool_calls",
            "max_tokens": "length",
            "stop_sequence": "stop",
            "guardrail_intervened": "content_filter",
            "content_filtered": "content_filter",
        }
        return _MAP.get(raw_reason, "stop")


# 导入时自动注册
from agent.transports import register_transport  # noqa: E402

register_transport("bedrock_converse", BedrockTransport)
