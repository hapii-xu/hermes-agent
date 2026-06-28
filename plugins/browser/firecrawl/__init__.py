"""Firecrawl 云浏览器插件 — 内置，自动加载。

与 ``plugins/web/firecrawl/``（网页搜索/提取/爬取插件）不同；
两者共用 FIRECRAWL_API_KEY，但访问不同端点
（此处为 ``/v2/browser``，而非 ``/v2/search`` / ``/v2/scrape`` / ``/v2/crawl``）。
"""

from __future__ import annotations

from plugins.browser.firecrawl.provider import FirecrawlBrowserProvider


def register(ctx) -> None:
    """将 Firecrawl 云浏览器 provider 注册到插件上下文中。"""
    ctx.register_browser_provider(FirecrawlBrowserProvider())
