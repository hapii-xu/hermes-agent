"""
浏览器提供商注册表
===================

已注册云浏览器提供商的中央映射。在导入时通过
:meth:`PluginContext.register_browser_provider` 填充;由
:func:`tools.browser_tool._get_cloud_provider` 使用,将每个云模式
``browser_*`` 工具调用路由到活动后端。

活动选择
--------
通过配置按以下优先级选择活动提供商:

1. ``config.yaml`` 中的 ``browser.cloud_provider`` (显式覆盖)。
2. 旧的首选顺序 — ``browser-use`` → ``browserbase`` — 按可用性过滤。
   匹配 :func:`tools.browser_tool._get_cloud_provider` 中的历史自动检测顺序
   (Browser Use 首先检查,因为它涵盖托管 Nous 网关和直接 API 密钥路径;
   Browserbase 作为较旧的直接凭证后备)。
   只有用户显式设置 ``browser.cloud_provider: firecrawl`` 时,``firecrawl``
   才会出现在旧遍历中,匹配迁移前的行为,其中 Firecrawl 从未被自动选择。
3. 否则为 ``None`` — 调度程序回退到本地浏览器模式。

显式配置分支(规则 1)故意忽略 ``is_available()``,因此调度程序向用户
显示类型化的"X_API_KEY 未设置"错误,而不是静默切换后端。匹配
已配置名称的旧 :func:`tools.browser_tool._get_cloud_provider` 行为。

注意:这里没有"功能"分离(与 Web 子系统不同,后者有
搜索/提取/爬网)。每个浏览器提供商都实现完整的
:class:`agent.browser_provider.BrowserProvider` 生命周期;注册表的
工作纯粹是选择,而不是功能路由。
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional

from agent.browser_provider import BrowserProvider

logger = logging.getLogger(__name__)


_providers: Dict[str, BrowserProvider] = {}
_lock = threading.Lock()


def register_provider(provider: BrowserProvider) -> None:
    """注册云浏览器提供商。

    重新注册(相同的 ``name``)会覆盖之前的条目并记录调试消息 —
    使热重载场景(测试、开发循环)行为可预测。
    """
    if not isinstance(provider, BrowserProvider):
        raise TypeError(
            f"register_provider() 期望 BrowserProvider 实例, "
            f"得到 {type(provider).__name__}"
        )
    name = provider.name
    if not isinstance(name, str) or not name.strip():
        raise ValueError("浏览器提供商 .name 必须是非空字符串")
    with _lock:
        existing = _providers.get(name)
        _providers[name] = provider
    if existing is not None:
        logger.debug(
            "浏览器提供商 '%s' 重新注册(原为 %r)",
            name, type(existing).__name__,
        )
    else:
        logger.debug(
            "已注册浏览器提供商 '%s' (%s)",
            name, type(provider).__name__,
        )


def list_providers() -> List[BrowserProvider]:
    """返回所有已注册的提供商,按名称排序。"""
    with _lock:
        items = list(_providers.values())
    return sorted(items, key=lambda p: p.name)


def get_provider(name: str) -> Optional[BrowserProvider]:
    """返回在 *name* 下注册的提供商,或 None。"""
    if not isinstance(name, str):
        return None
    with _lock:
        return _providers.get(name.strip())


# ---------------------------------------------------------------------------
# 活动提供商解析
# ---------------------------------------------------------------------------


# 旧自动检测顺序 — 当未设置 ``browser.cloud_provider`` 时使用。
# 匹配 :func:`tools.browser_tool._get_cloud_provider` 中的迁移前遍历。
# Firecrawl 故意缺席,因此设置 ``FIRECRAWL_API_KEY`` 用于 web-extract 的
# 用户不会被静默路由到付费云浏览器。请参阅
# :func:`_resolve` 了解完整的理由。
_LEGACY_PREFERENCE = (
    "browser-use",
    "browserbase",
)


def _resolve(configured: Optional[str]) -> Optional[BrowserProvider]:
    """解析活动浏览器提供商。

    解析规则(按顺序):

    1. **显式"local"。** 返回 None — 调度程序完全禁用云模式。
       镜像 :func:`tools.browser_tool._get_cloud_provider` 中的旧短路。
    2. **显式配置优先,忽略可用性。** 如果 ``configured`` 命名了已注册的提供商,
       即使其 :meth:`is_available` 返回 False 也返回它 — 调度程序将显示
       精确的"X_API_KEY 未设置"错误,而不是静默路由到其他地方。
    3. **旧首选遍历,按可用性过滤。** 遍历 :data:`_LEGACY_PREFERENCE`
       (``browser-use`` → ``browserbase``) 寻找 ``is_available()`` 为 True 的提供商。

    故意没有"单个符合条件快捷方式"规则(与
    :func:`agent.web_search_registry._resolve` 不同)。迁移前,
    ``tools.browser_tool._get_cloud_provider`` 中的自动检测分支只考虑
    Browser Use 和 Browserbase;只有通过显式 ``browser.cloud_provider: firecrawl``
    配置键才能访问 Firecrawl。保留此门控很重要,因为 Firecrawl 与 *web*
    提取插件 (``plugins/web/firecrawl/``) 共享其 API 密钥,因此为
    web 提取设置 ``FIRECRAWL_API_KEY`` 的用户绝不能被静默路由到
    付费云浏览器。在 ``~/.hermes/plugins/browser/<vendor>/`` 下添加的第三方
    浏览器提供商插件受同一门控 — 它们必须显式配置才能生效。

    当没有配置提供商且没有可用提供商匹配旧首选项时返回 None;
    然后调度程序回退到本地浏览器模式。
    """
    with _lock:
        snapshot = dict(_providers)

    def _is_available_safe(p: BrowserProvider) -> bool:
        """包装 ``is_available()`` 以便有缺陷的提供商不会终止解析。"""
        try:
            return bool(p.is_available())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "浏览器提供商 %s.is_available() 引发 %s — 视为不可用",
                p.name, exc, exc_info=True,
            )
            return False

    # 1. 显式"local"短路。
    if configured == "local":
        return None

    # 2. 显式配置优先 — 无论 is_available() 如何都返回,
    #    因此用户获得精确的下游错误消息,而不是静默后端切换。
    #    匹配 browser_tool.py 中的 _get_cloud_provider()。
    if configured:
        provider = snapshot.get(configured)
        if provider is not None:
            return provider
        logger.debug(
            "配置了 browser cloud_provider '%s' 但未注册; "
            "回退到自动检测",
            configured,
        )

    # 3. 旧首选遍历 — 只有 _LEGACY_PREFERENCE 中的提供商
    #    自动符合条件。按可用性过滤,因此我们不会向没有凭证的用户显示提供商。
    #    请参阅文档字符串,了解我们为什么不回退到"任何单个符合条件的已注册提供商"。
    for legacy in _LEGACY_PREFERENCE:
        provider = snapshot.get(legacy)
        if provider is not None and _is_available_safe(provider):
            return provider

    return None


def _reset_for_tests() -> None:
    """清空注册表。**仅用于测试。**"""
    with _lock:
        _providers.clear()
