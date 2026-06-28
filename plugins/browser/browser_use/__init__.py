"""Browser Use 云浏览器插件 — 内置，自动加载。

遵循 ``plugins/web/<vendor>/`` 的目录结构：``provider.py`` 存放
provider 类；``__init__.py::register`` 负责实例化并注册它。
"""

from __future__ import annotations

from plugins.browser.browser_use.provider import BrowserUseBrowserProvider


def register(ctx) -> None:
    """向插件上下文注册 Browser Use provider。"""
    ctx.register_browser_provider(BrowserUseBrowserProvider())
