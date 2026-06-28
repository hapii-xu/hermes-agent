"""
Web Search Provider Registry
============================

已注册 Web 提供商的中央映射表。由插件在导入时通过
:meth:`PluginContext.register_web_search_provider` 注册；
被 :mod:`tools.web_tools` 中的 ``web_search`` 和 ``web_extract``
工具包装器消费，用于将每次调用分派到当前活跃的后端。

Active selection
----------------
活跃提供商按以下优先级通过配置选择：

1. ``web.search_backend`` / ``web.extract_backend``
   （按能力的覆盖配置）。
2. ``web.backend``（共享回退）。
3. 如果恰好只有一个符合能力条件的提供商已注册且可用，则使用它。
4. 旧版偏好顺序 — ``firecrawl`` → ``parallel`` → ``tavily`` →
   ``exa`` → ``searxng`` → ``brave-free`` → ``ddgs`` — 按可用性过滤。
   与历史 ``tools.web_tools._get_backend()`` 候选顺序一致，因此从未设置
   配置项的安装在插件迁移后会继续命中相同的提供商。
5. 否则返回 ``None`` — 工具会显示一条有用的错误信息，指向
   ``hermes tools``。

能力过滤器（``supports_search`` / ``supports_extract``）在每一步都会应用，
因此配置为 ``web.extract_backend`` 的仅搜索提供商（``brave-free``）会正确
回退到支持 extract 的后端。
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)


_providers: Dict[str, WebSearchProvider] = {}
_lock = threading.Lock()


def register_provider(provider: WebSearchProvider) -> None:
    """注册一个 Web 搜索/提取提供商。

    重复注册（相同 ``name``）会覆盖前一个条目并记录一条调试信息——
    使热重载场景（测试、开发循环）行为可预测。
    """
    if not isinstance(provider, WebSearchProvider):
        raise TypeError(
            f"register_provider() expects a WebSearchProvider instance, "
            f"got {type(provider).__name__}"
        )
    name = provider.name
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Web provider .name must be a non-empty string")
    with _lock:
        existing = _providers.get(name)
        _providers[name] = provider
    if existing is not None:
        logger.debug(
            "Web provider '%s' re-registered (was %r)",
            name, type(existing).__name__,
        )
    else:
        logger.debug(
            "Registered web provider '%s' (%s)",
            name, type(provider).__name__,
        )


def list_providers() -> List[WebSearchProvider]:
    """返回所有已注册的提供商，按名称排序。"""
    with _lock:
        items = list(_providers.values())
    return sorted(items, key=lambda p: p.name)


def get_provider(name: str) -> Optional[WebSearchProvider]:
    """返回在 *name* 下注册的提供商，不存在则返回 None。"""
    if not isinstance(name, str):
        return None
    with _lock:
        return _providers.get(name.strip())


# ---------------------------------------------------------------------------
# 活跃提供商解析
# ---------------------------------------------------------------------------


def _read_config_key(*path: str) -> Optional[str]:
    """从 ``config.yaml`` 解析一个点分隔的配置键。未找到则返回 None。"""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        cur = cfg
        for segment in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(segment)
        if isinstance(cur, str) and cur.strip():
            return cur.strip()
    except Exception as exc:
        logger.debug("Could not read config %s: %s", ".".join(path), exc)
    return None


# 旧版偏好顺序 — 为未设置任何 ``web.backend`` /
# ``web.<capability>_backend`` 配置键的用户保留原有行为。与
# :func:`tools.web_tools._get_backend` 中的历史候选顺序一致
# （付费提供商优先，这样已有的付费配置在升级后不会被降级到免费版）。
# 在遍历时按 ``is_available()`` 过滤，因此不会返回用户没有凭据的提供商。
_LEGACY_PREFERENCE = (
    "firecrawl",
    "parallel",
    "tavily",
    "exa",
    "searxng",
    "brave-free",
    "ddgs",
)


def _resolve(configured: Optional[str], *, capability: str) -> Optional[WebSearchProvider]:
    """Resolve the active provider for a capability ("search" | "extract").

    Resolution rules (in order):

    1. **Explicit config wins, ignoring availability.** If
       ``web.{capability}_backend`` or ``web.backend`` names a registered
       provider that supports *capability*, return it even if its
       :meth:`is_available` returns False — the dispatcher will surface a
       precise "X_API_KEY is not set" error to the user instead of silently
       routing somewhere else. Matches legacy
       :func:`tools.web_tools._get_backend` behavior for configured names.

    2. **Single-provider shortcut.** When only one registered provider
       supports *capability* AND ``is_available()`` reports True, return it.

    3. **Legacy preference walk, filtered by availability.** Walk the
       :data:`_LEGACY_PREFERENCE` order (firecrawl → parallel → tavily →
       exa → searxng → brave-free → ddgs) looking for a provider whose
       ``supports_<capability>()`` is True AND whose ``is_available()`` is
       True. Matches the historic ``tools.web_tools._get_backend()``
       candidate order so users with credentials but no explicit config
       key keep landing on the same provider as pre-migration. This is
       the path that fires when no config key is set — pick the
       highest-priority backend the user actually has credentials for.

    Returns None when no provider is configured AND no available provider
    matches the legacy preference; the dispatcher then returns a "set up a
    provider" error to the user.
    """
    with _lock:
        snapshot = dict(_providers)

    def _capable(p: WebSearchProvider) -> bool:
        if capability == "search":
            return bool(p.supports_search())
        if capability == "extract":
            return bool(p.supports_extract())
        return False

    def _is_available_safe(p: WebSearchProvider) -> bool:
        """Wrap ``is_available()`` so a buggy provider doesn't kill resolution."""
        try:
            return bool(p.is_available())
        except Exception as exc:  # noqa: BLE001
            logger.debug("provider %s.is_available() raised %s", p.name, exc)
            return False

    # 1. Explicit config wins — return regardless of is_available() so the
    #    user gets a precise downstream error message rather than a silent
    #    backend switch. Matches _get_backend() in web_tools.py.
    if configured:
        provider = snapshot.get(configured)
        if provider is not None and _capable(provider):
            return provider
        if provider is None:
            logger.debug(
                "web backend '%s' configured but not registered; falling back",
                configured,
            )
        else:
            logger.debug(
                "web backend '%s' configured but does not support '%s'; falling back",
                configured, capability,
            )

    # 2. + 3. Fallback path — filter by availability so we don't surface
    #    a provider the user has no credentials for. Without this filter,
    #    a registered-but-unconfigured provider could end up "active" on
    #    a fresh install with no API keys at all.
    eligible = [
        p for p in snapshot.values()
        if _capable(p) and _is_available_safe(p)
    ]
    if len(eligible) == 1:
        return eligible[0]

    for legacy in _LEGACY_PREFERENCE:
        provider = snapshot.get(legacy)
        if (
            provider is not None
            and _capable(provider)
            and _is_available_safe(provider)
        ):
            return provider

    return None


def get_active_search_provider() -> Optional[WebSearchProvider]:
    """Resolve the currently-active web search provider.

    Reads ``web.search_backend`` (preferred) or ``web.backend`` (shared
    fallback) from config.yaml; falls back per the module docstring.
    """
    explicit = _read_config_key("web", "search_backend") or _read_config_key("web", "backend")
    return _resolve(explicit, capability="search")


def get_active_extract_provider() -> Optional[WebSearchProvider]:
    """Resolve the currently-active web extract provider.

    Reads ``web.extract_backend`` (preferred) or ``web.backend`` (shared
    fallback) from config.yaml; falls back per the module docstring.
    """
    explicit = _read_config_key("web", "extract_backend") or _read_config_key("web", "backend")
    return _resolve(explicit, capability="extract")


def _reset_for_tests() -> None:
    """Clear the registry. **Test-only.**"""
    with _lock:
        _providers.clear()
