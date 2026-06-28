"""传输层类型与注册表，用于提供商响应的规范化处理。

用法：
    from agent.transports import get_transport
    transport = get_transport("anthropic_messages")
    result = transport.normalize_response(raw_response)
"""

from agent.transports.types import (
    NormalizedResponse,
    ToolCall,
    Usage,
    build_tool_call,
    map_finish_reason,
)  # noqa: F401

_REGISTRY: dict = {}
_discovered: bool = False


def register_transport(api_mode: str, transport_cls: type) -> None:
    """为指定的 api_mode 字符串注册一个传输类。"""
    _REGISTRY[api_mode] = transport_cls


def get_transport(api_mode: str):
    """获取指定 api_mode 对应的传输实例。

    如果该 api_mode 没有注册传输，则返回 None。
    这支持渐进式迁移——调用方可以判断返回值是否为 None，
    并回退到旧代码路径。
    """
    global _discovered
    if not _discovered:
        _discover_transports()
    cls = _REGISTRY.get(api_mode)
    if cls is None:
        # 当某个具体传输模块被直接导入时（例如在 codex 之前导入了 chat_completions），
        # 注册表可能只被部分填充。在未命中时重新发现，而不仅在注册表为空时，
        # 这样测试/顺序依赖的导入就不会让有效的 api_mode 变得不可用。
        _discover_transports()
        cls = _REGISTRY.get(api_mode)
    if cls is None:
        return None
    return cls()


def _discover_transports() -> None:
    """导入所有传输模块以触发自动注册。"""
    global _discovered
    _discovered = True
    try:
        import agent.transports.anthropic  # noqa: F401
    except ImportError:
        pass
    try:
        import agent.transports.codex  # noqa: F401
    except ImportError:
        pass
    try:
        import agent.transports.chat_completions  # noqa: F401
    except ImportError:
        pass
    try:
        import agent.transports.bedrock  # noqa: F401
    except ImportError:
        pass
