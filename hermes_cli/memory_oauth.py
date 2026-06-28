"""memory provider OAuth 连接的 HTTP 路由，由 ``web_server`` 挂载。

从 ``web_server.py`` 中分离出来，以便 memory 功能的接口保持在 memory 层。
调度按约定进行：provider 的 flow 位于
``plugins.memory.<provider>.oauth_flow``，暴露 ``start_loopback_flow_background``
和 ``get_flow_status``；没有该模块的 provider 将直接返回 404。此处不显式
引用任何 provider。
"""

from __future__ import annotations

import importlib
from contextlib import contextmanager
from typing import Optional

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/memory/providers")


def _resolve_flow(provider: str):
    """按约定返回 provider 的 OAuth flow 模块，否则抛出 404。"""
    if not provider.isidentifier():
        raise HTTPException(status_code=404, detail=f"unknown memory provider {provider!r}")
    try:
        return importlib.import_module(f"plugins.memory.{provider}.oauth_flow")
    except ImportError:
        raise HTTPException(status_code=404, detail=f"{provider} does not support OAuth connect")


@contextmanager
def _scope_to_profile(profile: Optional[str]):
    """将配置解析范围限定到 ``profile``，使 flow 的即时路径解析
    定位到该 profile 的 honcho.json。None/""/"current" 则保持不变。"""
    requested = (profile or "").strip()
    if not requested or requested.lower() == "current":
        yield
        return

    from hermes_cli import profiles as profiles_mod
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    try:
        profiles_mod.validate_profile_name(requested)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not profiles_mod.profile_exists(requested):
        raise HTTPException(status_code=404, detail=f"Profile '{requested}' does not exist.")

    token = set_hermes_home_override(str(profiles_mod.get_profile_dir(requested)))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


@router.post("/{provider}/oauth/start")
async def start_memory_oauth(provider: str, profile: Optional[str] = None):
    """启动 provider 的零 CLI OAuth flow — 打开浏览器并通过
    环回监听器捕获授权。立即返回；请轮询状态。"""
    flow = _resolve_flow(provider)
    try:
        # flow 在此范围内即时解析其配置路径；它启动的工作线程
        # 的生命周期超过请求和覆盖。
        with _scope_to_profile(profile):
            return flow.start_loopback_flow_background()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to start {provider} OAuth: {exc}")


@router.get("/{provider}/oauth/status")
async def memory_oauth_status(provider: str, profile: Optional[str] = None):
    """轮询 provider 的 OAuth flow 状态：idle | pending | connected | error。"""
    flow = _resolve_flow(provider)
    try:
        with _scope_to_profile(profile):
            return flow.get_flow_status()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read {provider} OAuth status: {exc}")
