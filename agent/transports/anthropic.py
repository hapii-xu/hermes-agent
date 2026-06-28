"""Anthropic Messages API 传输层。

委托给 agent/anthropic_adapter.py 中已有的适配器函数。
该传输层负责格式转换与规范化——不负责客户端生命周期管理。
"""

from typing import Any, Dict, List, Optional

from agent.transports.base import ProviderTransport
from agent.transports.types import NormalizedResponse


class AnthropicTransport(ProviderTransport):
    """api_mode='anthropic_messages' 的传输实现。

    将 anthropic_adapter.py 中的现有函数封装在 ProviderTransport 抽象类后面。
    每个方法均委托调用——不重复任何逻辑。
    """

    @property
    def api_mode(self) -> str:
        return "anthropic_messages"

    def convert_messages(self, messages: List[Dict[str, Any]], **kwargs) -> Any:
        """将 OpenAI 消息格式转换为 Anthropic (system, messages) 元组。

        kwargs:
            base_url: Optional[str] — 影响思考签名的处理方式。
        """
        from agent.anthropic_adapter import convert_messages_to_anthropic

        base_url = kwargs.get("base_url")
        return convert_messages_to_anthropic(messages, base_url=base_url)

    def convert_tools(self, tools: List[Dict[str, Any]]) -> Any:
        """将 OpenAI 工具 schema 转换为 Anthropic input_schema 格式。"""
        from agent.anthropic_adapter import convert_tools_to_anthropic

        return convert_tools_to_anthropic(tools)

    def build_kwargs(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **params,
    ) -> Dict[str, Any]:
        """构建 Anthropic messages.create() 的关键字参数。

        内部会调用 convert_messages 和 convert_tools。

        params（均为可选）:
            max_tokens: int
            reasoning_config: dict | None
            tool_choice: str | None
            is_oauth: bool
            preserve_dots: bool
            context_length: int | None
            base_url: str | None
            fast_mode: bool
            drop_context_1m_beta: bool
        """
        from agent.anthropic_adapter import build_anthropic_kwargs

        return build_anthropic_kwargs(
            model=model,
            messages=messages,
            tools=tools,
            max_tokens=params.get("max_tokens", 16384),
            reasoning_config=params.get("reasoning_config"),
            tool_choice=params.get("tool_choice"),
            is_oauth=params.get("is_oauth", False),
            preserve_dots=params.get("preserve_dots", False),
            context_length=params.get("context_length"),
            base_url=params.get("base_url"),
            fast_mode=params.get("fast_mode", False),
            drop_context_1m_beta=params.get("drop_context_1m_beta", False),
        )

    def normalize_response(self, response: Any, **kwargs) -> NormalizedResponse:
        """将 Anthropic 响应规范化为 NormalizedResponse。

        解析内容块（text、thinking、tool_use），将 stop_reason 映射为 OpenAI finish_reason，
        并将 reasoning_details 收集到 provider_data 中。
        """
        import json
        from agent.anthropic_adapter import _to_plain_data, _sanitize_replay_block
        from agent.transports.types import ToolCall

        strip_tool_prefix = kwargs.get("strip_tool_prefix", False)
        _MCP_PREFIX = "mcp__"

        text_parts = []
        reasoning_parts = []
        reasoning_details = []
        tool_calls = []
        # 逐字、保序地复制该轮次中的每个内容块。
        # Anthropic 对每个 thinking 块的签名基于该块在轮次中前面的内容；
        # 当一轮交替出现 thinking 和 tool_use（自适应/交替思考，Claude 4.6+），
        # 下面的 reasoning_details + tool_calls 列表会丢失跨类型的顺序。
        # 以错误顺序重放最新的助手消息会使签名失效 -> HTTP 400 "thinking ... blocks in the
        # latest assistant message cannot be modified"。在此保留精确的块序列，
        # 以便适配器能原样重放。参见 tests/agent/test_anthropic_thinking_block_order.py。
        ordered_blocks = []

        for block in response.content:
            block_dict = _to_plain_data(block)
            clean_block = None
            if isinstance(block_dict, dict):
                # 在捕获时进行清理，防止仅用于输出的 SDK 字段（parsed_output、
                # caller、citations=None 等）持久化到 state.db，并在重放时作为请求输入泄漏
                # → HTTP 400 "Extra inputs are not permitted"。与重放侧的清理形成纵深防御。
                clean_block = _sanitize_replay_block(block_dict)
                if clean_block is not None:
                    ordered_blocks.append(clean_block)
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type in ("thinking", "redacted_thinking"):
                if block.type == "thinking":
                    reasoning_parts.append(block.thinking)
                # reasoning_details 也使用清理后的块（clean_block），
                # 因为 _extract_preserved_thinking_blocks 在非排序路径上会重放这些块。
                # 仅当清理丢弃了块时，才回退到原始值。
                if isinstance(clean_block, dict):
                    reasoning_details.append(clean_block)
                elif isinstance(block_dict, dict):
                    reasoning_details.append(block_dict)
            elif block.type == "tool_use":
                name = block.name
                if strip_tool_prefix and name.startswith(_MCP_PREFIX):
                    # 在 OAuth 通信协议中，每个工具都带有双下划线 ``mcp__`` 前缀
                    # （在 build_anthropic_kwargs 中添加，以规避 Anthropic 对单下划线第三方工具的分类器）。
                    # 将其还原为注册表/分发器所知道的名称。
                    # 两种原始形式映射到同一个 ``mcp__`` 线协议名：
                    #   ``mcp__read_file``        <- 裸原生工具 ``read_file``
                    #   ``mcp__linear_get_issue`` <- MCP 服务器工具 ``mcp_linear_get_issue``
                    # 通过注册表查找来解析，优先选择实际已注册的原始名称；
                    # 不要重写 LLM 使用的已能原生解析的名称。GH-25255。
                    from tools.registry import registry as _tool_registry
                    if not _tool_registry.get_entry(name):
                        bare = name[len(_MCP_PREFIX):]            # read_file
                        single = "mcp_" + bare                    # mcp_read_file / mcp_linear_get_issue
                        if _tool_registry.get_entry(single):
                            name = single
                        elif _tool_registry.get_entry(bare):
                            name = bare
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=name,
                        arguments=json.dumps(block.input),
                    )
                )

        finish_reason = self._STOP_REASON_MAP.get(response.stop_reason, "stop")

        provider_data = {}
        if reasoning_details:
            provider_data["reasoning_details"] = reasoning_details
        # 只有当该轮次确实将有签名的思考块与 tool_use 交替出现时，
        # 才需要携带有序块通道——这是并行列表无法正确重建的唯一情形。
        # 纯文本轮次，或只有单个领头 thinking 块的思考后工具调用，无需此通道也能正确重放。
        _has_signed_thinking = any(
            isinstance(b, dict)
            and b.get("type") in ("thinking", "redacted_thinking")
            and (b.get("signature") or b.get("data"))
            for b in ordered_blocks
        )
        _has_tool_use = any(
            isinstance(b, dict) and b.get("type") == "tool_use"
            for b in ordered_blocks
        )
        if _has_signed_thinking and _has_tool_use:
            provider_data["anthropic_content_blocks"] = ordered_blocks

        return NormalizedResponse(
            content="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls or None,
            finish_reason=finish_reason,
            reasoning="\n\n".join(reasoning_parts) if reasoning_parts else None,
            usage=None,
            provider_data=provider_data or None,
        )

    def validate_response(self, response: Any) -> bool:
        """检查 Anthropic 响应结构是否有效。

        对于不携带文本内容的终止停止原因，空内容列表是合法的：

        - ``end_turn`` —— 模型在工具轮次完成后发出的标准"无需补充"信号，
          该轮次已向用户返回了文本。
        - ``refusal`` —— 模型拒绝响应（Claude 4.5+）。Messages API 在该停止原因下
          返回空的 ``content`` 列表。若将其视为无效则会把确定性的拒绝行为送入无效响应重试循环，
          每次尝试均复现拒绝，并呈现出误导性的"限速/无效响应"错误，而非拒绝信息。
          ``normalize_response`` 将 ``refusal`` 映射为 ``content_filter``，
          以便 agent 循环的拒绝处理器能正常呈现。

        将任一情形视为无效都会对已完成的响应进行虚假重试。
        """
        if response is None:
            return False
        content_blocks = getattr(response, "content", None)
        if not isinstance(content_blocks, list):
            return False
        if not content_blocks:
            return getattr(response, "stop_reason", None) in {"end_turn", "refusal"}
        return True

    def extract_cache_stats(self, response: Any) -> Optional[Dict[str, int]]:
        """提取 Anthropic 的缓存读取和缓存创建 token 计数。"""
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        written = getattr(usage, "cache_creation_input_tokens", 0) or 0
        if cached or written:
            return {"cached_tokens": cached, "creation_tokens": written}
        return None

    # 将适配器的标准映射提升到模块级别以便共享
    _STOP_REASON_MAP = {
        "end_turn": "stop",
        "tool_use": "tool_calls",
        "max_tokens": "length",
        "stop_sequence": "stop",
        "refusal": "content_filter",
        "model_context_window_exceeded": "length",
    }

    def map_finish_reason(self, raw_reason: str) -> str:
        """将 Anthropic stop_reason 映射为 OpenAI finish_reason。"""
        return self._STOP_REASON_MAP.get(raw_reason, "stop")


# 导入时自动注册
from agent.transports import register_transport  # noqa: E402

register_transport("anthropic_messages", AnthropicTransport)
