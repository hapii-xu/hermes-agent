"""提供商传输层的抽象基类。

一个传输层负责某个 api_mode 的数据通路：
  convert_messages → convert_tools → build_kwargs → normalize_response

不负责：客户端构建、流式传输、凭证刷新、
提示缓存、中断处理或重试逻辑。这些职责保留在 AIAgent 中。
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from agent.transports.types import NormalizedResponse


class ProviderTransport(ABC):
    """提供商特定格式转换与规范化的基类。"""

    @property
    @abstractmethod
    def api_mode(self) -> str:
        """该传输层处理的 api_mode 字符串（例如 'anthropic_messages'）。"""
        ...

    @abstractmethod
    def convert_messages(self, messages: List[Dict[str, Any]], **kwargs) -> Any:
        """将 OpenAI 格式的消息转换为提供商原生格式。

        返回提供商特定的结构（例如 Anthropic 的 (system, messages) 元组，
        或 chat_completions 的原始消息列表）。
        """
        ...

    @abstractmethod
    def convert_tools(self, tools: List[Dict[str, Any]]) -> Any:
        """将 OpenAI 格式的工具定义转换为提供商原生格式。

        返回提供商特定的工具列表（例如 Anthropic input_schema 格式）。
        """
        ...

    @abstractmethod
    def build_kwargs(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **params,
    ) -> Dict[str, Any]:
        """构建完整的 API 调用 kwargs 字典。

        这是主要入口点——通常在内部调用 convert_messages() 和 convert_tools()，
        然后添加模型特定的配置。

        返回一个可直接传递给提供商 SDK 客户端的字典。
        """
        ...

    @abstractmethod
    def normalize_response(self, response: Any, **kwargs) -> NormalizedResponse:
        """将提供商的原始响应规范化为共享的 NormalizedResponse 类型。

        这是唯一返回传输层类型的方法。
        """
        ...

    def validate_response(self, response: Any) -> bool:
        """可选：检查原始响应的结构是否有效。

        有效返回 True，应视为无效时返回 False。
        默认实现始终返回 True。
        """
        return True

    def extract_cache_stats(self, response: Any) -> Optional[Dict[str, int]]:
        """可选：提取提供商特定的缓存命中/创建统计。

        返回包含 'cached_tokens' 和 'creation_tokens' 的字典，或 None。
        默认返回 None。
        """
        return None

    def map_finish_reason(self, raw_reason: str) -> str:
        """可选：将提供商特定的停止原因映射为 OpenAI 等效值。

        默认原样返回原始原因。对于具有不同停止原因词汇表的提供商需重写此方法。
        """
        return raw_reason
