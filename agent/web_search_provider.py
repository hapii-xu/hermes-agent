"""
Web Search Provider ABC（抽象基类）
=======================

定义 Web 搜索和内容提取的可插拔后端接口。
Provider 通过 ``PluginContext.register_web_search_provider()`` 注册实例；
当前激活的 provider（通过 ``config.yaml`` 中的 ``web.search_backend`` /
``web.extract_backend`` / ``web.backend`` 选择）负责处理所有 ``web_search`` /
``web_extract`` 工具调用。

Provider 位于 ``<repo>/plugins/web/<name>/``（内置，作为 ``kind: backend``
自动加载）或 ``~/.hermes/plugins/web/<name>/``（用户级，通过
``plugins.enabled`` 手动启用）。

此 ABC 是 Web provider 唯一的插件接口 — 目录树中的每个 provider
（brave-free、ddgs、searxng、exa、parallel、tavily、firecrawl）
都实现了它。旧版的树内 ``tools.web_providers.base`` ABC 以及
``tools/web_tools.py`` 中每个供应商的内联辅助函数已在 PR #25182 中删除；
下方记录的响应格式约定原封不动地保留，以便工具包装器无需进行转换。

响应格式（保留自旧版约定）：

搜索结果::

    {
        "success": True,
        "data": {
            "web": [
                {"title": str, "url": str, "description": str, "position": int},
                ...
            ]
        }
    }

内容提取结果::

    {
        "success": True,
        "data": [
            {"url": str, "title": str, "content": str,
             "raw_content": str, "metadata": dict},
            ...
        ]
    }

任一能力调用失败时::

    {"success": False, "error": str}
"""

from __future__ import annotations

import abc
from typing import Any, Dict, List


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


class WebSearchProvider(abc.ABC):
    """Abstract base class for a web search/extract backend.

    Subclasses must implement :meth:`is_available` and at least one of
    :meth:`search` / :meth:`extract`. The :meth:`supports_search` /
    :meth:`supports_extract` capability flags let the registry route each
    tool call to the right provider, and let multi-capability providers
    (Firecrawl, Tavily, Exa, …) advertise multiple capabilities from a
    single class.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Stable short identifier used in ``web.search_backend`` /
        ``web.extract_backend`` / ``web.backend`` config keys.

        Lowercase, no spaces; hyphens permitted to preserve existing
        user-visible names. Examples: ``brave-free``, ``ddgs``,
        ``searxng``, ``firecrawl``.
        """

    @property
    def display_name(self) -> str:
        """Human-readable label shown in ``hermes tools``. Defaults to ``name``."""
        return self.name

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Return True when this provider can service calls.

        Typically a cheap check (env var present, optional Python dep
        importable, instance URL set). Must NOT make network calls — this
        runs at tool-registration time and on every ``hermes tools`` paint.
        """

    def supports_search(self) -> bool:
        """Return True if this provider implements :meth:`search`."""
        return True

    def supports_extract(self) -> bool:
        """Return True if this provider implements :meth:`extract`.

        Both sync and async :meth:`extract` implementations are valid — the
        dispatcher detects coroutine functions via
        :func:`inspect.iscoroutinefunction` and awaits as needed. Sync
        implementations that perform blocking I/O (HTTP, SDK calls) should
        ideally wrap in :func:`asyncio.to_thread` at the call site; small
        providers can keep their sync shape and let the dispatcher handle
        threading.
        """
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a web search.

        Override when :meth:`supports_search` returns True. The default
        raises NotImplementedError; callers should gate on
        :meth:`supports_search` before calling.
        """
        raise NotImplementedError(
            f"{self.name} does not support search (override supports_search)"
        )

    def extract(self, urls: List[str], **kwargs: Any) -> Any:
        """Extract content from one or more URLs.

        Override when :meth:`supports_extract` returns True. The default
        raises NotImplementedError; callers should gate on
        :meth:`supports_extract` before calling.

        Return shape: a list of result dicts matching what the legacy
        :func:`tools.web_tools.web_extract_tool` post-processing pipeline
        expects::

            [
                {
                    "url": str,
                    "title": str,
                    "content": str,
                    "raw_content": str,
                    "metadata": dict,           # optional
                    "error": str,               # optional, only on per-URL failure
                },
                ...
            ]

        Implementations MAY be ``async def`` — the dispatcher detects
        coroutines via :func:`inspect.iscoroutinefunction` and awaits.

        ``kwargs`` may carry forward-compat fields (``format``, ``include_raw``,
        ``max_chars``) — implementations should ignore unknown keys.
        """
        raise NotImplementedError(
            f"{self.name} does not support extract (override supports_extract)"
        )

    def get_setup_schema(self) -> Dict[str, Any]:
        """Return provider metadata for the ``hermes tools`` picker.

        Used by ``hermes_cli/tools_config.py`` to inject this provider as a
        row in the Web Search / Web Extract picker. Shape::

            {
                "name": "Brave Search (Free)",
                "badge": "free",
                "tag": "No paid tier needed — uses Brave's free API.",
                "env_vars": [
                    {"key": "BRAVE_SEARCH_API_KEY",
                     "prompt": "Brave Search API key",
                     "url": "https://brave.com/search/api/"},
                ],
            }

        Default: minimal entry derived from ``display_name``. Override to
        expose API key prompts, badges, and instance URL fields.
        """
        return {
            "name": self.display_name,
            "badge": "",
            "tag": "",
            "env_vars": [],
        }
