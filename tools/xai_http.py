"""直接 xAI HTTP 集成的共享辅助函数。"""

from __future__ import annotations

import json
import os
from typing import Dict


def has_xai_credentials() -> bool:
    """轻量探针 —— 当 xAI 凭据*可能*可用时返回 True。

    刻意避免 :func:`resolve_xai_http_credentials`，这样热绘制路径
    （``hermes tools`` 重绘、工具注册扫描、
    ``WebSearchProvider.is_available()``）中的调用方就不会产生磁盘锁，
    或者 —— 在 OAuth 路径下 —— 一次网络 token 刷新。
    :meth:`agent.web_search_provider.WebSearchProvider.is_available`
    上的 ABC 契约正是出于这个原因明确禁止网络调用。

    解析顺序，由快到慢：

    1. ``XAI_API_KEY`` 环境变量（最廉价；覆盖显式 key 的用户）。
    2. ``~/.hermes/auth.json`` 含有非空的 ``providers.xai-oauth.tokens.access_token``
       （单次文件读取，不做过期检查，不刷新）。

    任何异常都返回 False，这样损坏的 auth 存储不会阻塞
    其他可用性扫描。真正的刷新 + 过期处理发生在
    ``search()``（或实际发起请求的那个调用方）中。
    """
    if os.environ.get("XAI_API_KEY", "").strip():
        return True
    try:
        from hermes_constants import get_hermes_home

        auth_path = get_hermes_home() / "auth.json"
        if not auth_path.exists():
            return False
        store = json.loads(auth_path.read_text())
        providers = store.get("providers") if isinstance(store, dict) else None
        xai_state = providers.get("xai-oauth") if isinstance(providers, dict) else None
        tokens = xai_state.get("tokens") if isinstance(xai_state, dict) else None
        access_token = tokens.get("access_token") if isinstance(tokens, dict) else None
        return bool(str(access_token or "").strip())
    except Exception:
        return False


def get_env_value(name: str, default=None):
    """优先从 ``~/.hermes/.env`` 读取 ``name``，其次从 ``os.environ`` 读取。

    包装 :func:`hermes_cli.config.get_env_value`，以便测试可以 patch
    ``tools.xai_http.get_env_value``，向 xAI 凭据解析器注入仅存在于
    dotenv 的密钥。
    """
    try:
        from hermes_cli.config import get_env_value as _hermes_get_env_value

        value = _hermes_get_env_value(name)
        if value is not None:
            return value
    except Exception:
        pass
    return os.environ.get(name, default)


def hermes_xai_user_agent() -> str:
    """返回一个稳定的、Hermes 专用的 User-Agent，用于 xAI HTTP 调用。"""
    try:
        from hermes_cli import __version__
    except Exception:
        __version__ = "unknown"
    return f"Hermes-Agent/{__version__}"


def resolve_xai_http_credentials(*, force_refresh: bool = False) -> Dict[str, str]:
    """为直接的 xAI HTTP 端点解析 bearer 凭据。

    优先使用 Hermes 托管的 xAI OAuth 凭据（如果可用），然后回退到
    经由 ``hermes_cli.config.get_env_value`` 解析的 ``XAI_API_KEY``，
    这样存储在 ``~/.hermes/.env``（Hermes 的标准位置）中的 key 也会被采纳 ——
    而不仅仅是那些已经导出到 ``os.environ`` 中的。这使直接 xAI
    端点（图片、TTS、STT 等）与主运行时 auth 模型保持一致，
    并保留 PR #17140 / #17163 的回归契约。

    设置 ``force_refresh=True`` 可绕过解析器的 JWT 过期短路，并执行
    一次无条件的 OAuth 刷新。调用方应仅把它作为服务器返回 401 之后的
    反应式补救手段（窗口期中途吊销、JWT 主动检查形同空操作的不透明
    token 等），而不是作为默认行为 —— 因为刷新期间会一直持有 auth 存储锁。
    """
    if not force_refresh:
        try:
            from hermes_cli.runtime_provider import resolve_runtime_provider

            runtime = resolve_runtime_provider(requested="xai-oauth")
            access_token = str(runtime.get("api_key") or "").strip()
            base_url = str(runtime.get("base_url") or "").strip().rstrip("/")
            if access_token:
                return {
                    "provider": "xai-oauth",
                    "api_key": access_token,
                    "base_url": base_url or "https://api.x.ai/v1",
                }
        except Exception:
            pass

    try:
        from hermes_cli.auth import resolve_xai_oauth_runtime_credentials

        creds = resolve_xai_oauth_runtime_credentials(force_refresh=force_refresh)
        access_token = str(creds.get("api_key") or "").strip()
        base_url = str(creds.get("base_url") or "").strip().rstrip("/")
        if access_token:
            return {
                "provider": "xai-oauth",
                "api_key": access_token,
                "base_url": base_url or "https://api.x.ai/v1",
            }
    except Exception:
        pass

    api_key = str(get_env_value("XAI_API_KEY") or "").strip()
    base_url = str(get_env_value("XAI_BASE_URL") or "https://api.x.ai/v1").strip().rstrip("/")
    return {
        "provider": "xai",
        "api_key": api_key,
        "base_url": base_url,
    }
