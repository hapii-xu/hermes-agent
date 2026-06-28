"""Browserbase 云浏览器插件 — 内置，自动加载。

与 ``plugins/web/<vendor>/`` 和 ``plugins/image_gen/openai/`` 的
布局一致：``provider.py`` 存放 provider 类；``__init__.py::register``
通过插件上下文对其进行实例化和注册。
"""

from __future__ import annotations

from plugins.browser.browserbase.provider import BrowserbaseBrowserProvider


def register(ctx) -> None:
    """将 Browserbase provider 注册到插件上下文中。"""
    ctx.register_browser_provider(BrowserbaseBrowserProvider())
