"""Hermes 工具共享的 OpenRouter API 客户端。

提供一个懒加载（lazy-initialized）的 AsyncOpenAI 客户端，供所有工具模块共享。
通过 agent/auxiliary_client.py 中集中的 provider router（提供方路由）进行路由，
以便统一处理认证、请求头和 API 格式。
"""

import os

_client = None


def get_async_client():
    """返回一个共享的、与 OpenAI 兼容的异步客户端（用于 OpenRouter）。

    客户端在首次调用时懒加载创建，之后被复用。
    使用集中的 provider router 进行认证和客户端构建。
    若未设置 OPENROUTER_API_KEY，则抛出 ValueError。
    """
    global _client
    if _client is None:
        from agent.auxiliary_client import resolve_provider_client
        client, _model = resolve_provider_client("openrouter", async_mode=True)
        if client is None:
            raise ValueError("OPENROUTER_API_KEY environment variable not set")
        _client = client
    return _client


def check_api_key() -> bool:
    """检查 OpenRouter API key 是否存在。"""
    return bool(os.getenv("OPENROUTER_API_KEY"))
