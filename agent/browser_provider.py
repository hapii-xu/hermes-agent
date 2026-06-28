"""
浏览器提供商抽象基类
====================

定义云浏览器提供商的可插拔后端接口
(Browserbase、Browser Use、Firecrawl 等)。提供商通过
:meth:`PluginContext.register_browser_provider` 注册实例；选定的一个
(通过 ``config.yaml`` 中的 ``browser.cloud_provider`` 选择)为每个云模式
``browser_*`` 工具调用提供服务。

提供商位于 ``<repo>/plugins/browser/<name>/`` (内置,作为
``kind: backend`` 自动加载) 或 ``~/.hermes/plugins/browser/<name>/``
(用户自定义,通过 ``plugins.enabled`` 选择使用)。

此 ABC 与 :class:`agent.web_search_provider.WebSearchProvider` 镜像相同
(PR #25182) — 相同的形状、相同的注册流程、相同的选择器集成。
旧的内部 ``tools.browser_providers.base.CloudBrowserProvider`` ABC 已在
PR #25214 (本工作) 中删除,同时也删除了 ``tools/browser_providers/``
中每个供应商的内联模块；下面记录的生命周期合约逐位保留,因此工具包装器
(:mod:`tools.browser_tool`) 无需翻译即可工作。

会话元数据合约(保留自旧的 ``CloudBrowserProvider``)::

    {
        "session_name": str,        # agent-browser --session 的唯一名称
        "bb_session_id": str,       # 提供商会话 ID (用于关闭/清理)
        "cdp_url": str,             # CDP websocket URL
        "features": dict,           # 启用的功能标志
        "external_call_id": str,    # 可选,托管网关计费密钥
    }

``bb_session_id`` 是保留的旧键名,用于与
:mod:`tools.browser_tool` 向后兼容 — 它无论哪个提供商在使用,
都保存提供商的会话 ID。
"""

from __future__ import annotations

import abc
from typing import Any, Dict


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------


class BrowserProvider(abc.ABC):
    """云浏览器后端的抽象基类。

    子类必须实现 :meth:`name`、:meth:`is_available` 和三个生命周期方法:
    :meth:`create_session`、:meth:`close_session`、:meth:`emergency_cleanup`。

    生命周期形状逐位保留旧的 ``CloudBrowserProvider`` 合约,因此
    :mod:`tools.browser_tool` 中的调度程序是纯注册表查找 — 无需每个提供商的
    条件判断,无需形状转换。
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """``browser.cloud_provider`` 配置键中使用的稳定短标识符。

        允许小写、连字符以保留现有的用户可见名称。
        示例: ``browserbase``、``browser-use``、``firecrawl``。
        """

    @property
    def display_name(self) -> str:
        """``hermes tools`` 中显示的可读标签。默认为 ``name``。"""
        return self.name

    @abc.abstractmethod
    def is_available(self) -> bool:
        """当此提供商可以服务调用时返回 True。

        通常是一个廉价检查(存在环境变量、可读托管网关令牌、可选的 Python 依赖可导入)。
        绝不能进行网络调用 — 这在工具注册时间和每次 ``hermes tools`` 绘制时运行。

        镜像旧的 ``CloudBrowserProvider.is_configured()`` 方法;
        为了与 :class:`agent.web_search_provider.WebSearchProvider` 一致而重命名。
        """

    @abc.abstractmethod
    def create_session(self, task_id: str) -> Dict[str, object]:
        """创建云浏览器会话并返回会话元数据。

        必须返回至少包含以下内容的字典::

            {
                "session_name": str,    # agent-browser --session 的唯一名称
                "bb_session_id": str,   # 提供商会话 ID (用于关闭/清理)
                "cdp_url": str,         # CDP websocket URL
                "features": dict,       # 启用的功能标志
            }

        ``bb_session_id`` 是保留的旧键名,用于与
        :mod:`tools.browser_tool` 的其余部分向后兼容 — 它保存提供商的
        会话 ID,无论哪个提供商在使用。

        可能抛出 ``ValueError``(缺少凭证)或 ``RuntimeError``
        (网络/API 失败);调度程序将这些呈现给用户。
        """

    @abc.abstractmethod
    def close_session(self, session_id: str) -> bool:
        """通过提供商会话 ID 释放/终止云会话。

        成功返回 True,失败返回 False。不应抛出异常 — 记录日志并
        在任何异常时返回 False,以便调度程序的清理循环跨会话继续移动。
        """

    @abc.abstractmethod
    def emergency_cleanup(self, session_id: str) -> None:
        """进程退出时的最佳会话清理。

        从 atexit/信号处理程序调用。必须容忍缺失凭证、网络错误等 —
        记录日志并继续。绝不能抛出异常。
        """

    def get_setup_schema(self) -> Dict[str, Any]:
        """返回 ``hermes tools`` 选择器的提供商元数据。

        由 :mod:`hermes_cli.tools_config` 使用,将此提供商作为一行注入
        浏览器自动化选择器中。形状镜像 ``TOOL_CATEGORIES["browser"]``
        中的现有硬编码条目::

            {
                "name": "Browserbase",
                "badge": "paid",
                "tag": "具有隐身和代理的云浏览器",
                "env_vars": [
                    {"key": "BROWSERBASE_API_KEY",
                     "prompt": "Browserbase API key",
                     "url": "https://browserbase.com"},
                ],
                "post_setup": "agent_browser",
            }

        默认:从 :attr:`display_name` 派生的最小条目。
        覆盖以公开 API 密钥提示、徽章、托管 Nous 门控以及
        ``post_setup`` 安装钩子。
        """
        return {
            "name": self.display_name,
            "badge": "",
            "tag": "",
            "env_vars": [],
        }

    # ------------------------------------------------------------------
    # 旧的 CloudBrowserProvider API 的向后兼容填充
    # ------------------------------------------------------------------
    #
    # 旧的 PR-#25214 ABC 暴露了 ``is_configured()`` 和 ``provider_name()``;
    # ``tools.browser_tool`` 有约 6 个调用者仍然使用这些名称。而不是
    # 更改每个调用点(并破坏对 CloudBrowserProvider 进行子类的下游代码),
    # 我们将旧名称作为新的 API 的精简委托公开。子类必须实现
    # :meth:`is_available` 和 :attr:`name`;它们可以覆盖 ``is_configured`` /
    # ``provider_name`` 以与旧 ABC 兼容,但这不是必需的。

    def is_configured(self) -> bool:
        """:meth:`is_available` 的向后兼容别名。"""
        return self.is_available()

    def provider_name(self) -> str:
        """返回 :attr:`display_name` 的向后兼容别名。"""
        return self.display_name
