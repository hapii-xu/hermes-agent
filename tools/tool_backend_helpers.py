"""工具后端选择的共享辅助函数。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from utils import is_truthy_value


_DEFAULT_BROWSER_PROVIDER = "local"
_DEFAULT_MODAL_MODE = "auto"
_VALID_MODAL_MODES = {"auto", "direct", "managed"}


def managed_nous_tools_enabled(*, force_fresh: bool = False) -> bool:
    """当用户有权使用 Nous 工具网关时返回 True。

    资格来自付费的 Nous Portal 服务访问，或一个有效的免费工具池
    （``tool_gateway_entitled``）。各分类的覆盖范围（例如池子资助图片但
    不资助视频等）由调用方通过 ``tool_gateway_entitled_for`` 进一步收窄；
    本粗粒度门禁只回答「是否有任何托管工具可用」。

    资格未知/出错时，工具网关可用性偏向关闭。我们有意捕获所有异常并返回
    False——绝不阻塞启动。``force_fresh=True`` 用于交互式配置流程，应
    立即反映刚购买的订阅、积分或池子授予。
    """
    try:
        from hermes_cli.nous_account import get_nous_portal_account_info

        if force_fresh:
            account_info = get_nous_portal_account_info(force_fresh=True)
        else:
            account_info = get_nous_portal_account_info()
        if not account_info.logged_in:
            return False
        return account_info.tool_gateway_entitled
    except Exception:
        return False


def nous_tool_gateway_unavailable_message(
    capability: str = "the Nous Tool Gateway",
    *,
    force_fresh: bool = False,
) -> str:
    """针对不可用的 Nous 工具网关路径，返回感知账户状态的指引。"""
    try:
        from hermes_cli.nous_account import (
            format_nous_portal_entitlement_message,
            get_nous_portal_account_info,
        )

        account_info = get_nous_portal_account_info(force_fresh=force_fresh)
        message = format_nous_portal_entitlement_message(
            account_info,
            capability=capability,
        )
        if message:
            return message
    except Exception:
        pass
    return (
        f"{capability} is unavailable. Run `hermes model` to refresh your "
        "Nous Portal login and billing status."
    )


def normalize_browser_cloud_provider(value: object | None) -> str:
    """返回规范化的浏览器 provider 键。"""
    provider = str(value or _DEFAULT_BROWSER_PROVIDER).strip().lower()
    return provider or _DEFAULT_BROWSER_PROVIDER


def coerce_modal_mode(value: object | None) -> str:
    """当 modal 模式合法时返回该模式，否则返回默认值。"""
    mode = str(value or _DEFAULT_MODAL_MODE).strip().lower()
    if mode in _VALID_MODAL_MODES:
        return mode
    return _DEFAULT_MODAL_MODE


def normalize_modal_mode(value: object | None) -> str:
    """返回规范化的 modal 执行模式。"""
    return coerce_modal_mode(value)


def has_direct_modal_credentials() -> bool:
    """当存在直连 Modal 的凭据/配置时返回 True。"""
    try:
        modal_file_exists = (Path.home() / ".modal.toml").exists()
    except (PermissionError, OSError):
        modal_file_exists = False
    return bool(
        (os.getenv("MODAL_TOKEN_ID") and os.getenv("MODAL_TOKEN_SECRET"))
        or modal_file_exists
    )


def resolve_modal_backend_state(
    modal_mode: object | None,
    *,
    has_direct: bool,
    managed_ready: bool,
    managed_enabled: bool | None = None,
) -> Dict[str, Any]:
    """解析直连与托管 Modal 后端之间的选择。

    语义：
    - ``direct`` 表示仅直连
    - ``managed`` 表示仅托管
    - ``auto`` 优先使用托管（当可用时），否则回退到直连
    """
    requested_mode = coerce_modal_mode(modal_mode)
    normalized_mode = normalize_modal_mode(modal_mode)
    if managed_enabled is None:
        managed_enabled = managed_nous_tools_enabled()
    managed_mode_blocked = (
        requested_mode == "managed" and not managed_enabled
    )

    if normalized_mode == "managed":
        selected_backend = "managed" if managed_enabled and managed_ready else None
    elif normalized_mode == "direct":
        selected_backend = "direct" if has_direct else None
    else:
        selected_backend = "managed" if managed_enabled and managed_ready else "direct" if has_direct else None

    return {
        "requested_mode": requested_mode,
        "mode": normalized_mode,
        "has_direct": has_direct,
        "managed_ready": managed_ready,
        "managed_mode_blocked": managed_mode_blocked,
        "selected_backend": selected_backend,
    }


def resolve_openai_audio_api_key() -> str:
    """优先使用语音工具专用密钥，否则回退到普通的 OpenAI 密钥。"""
    return (
        os.getenv("VOICE_TOOLS_OPENAI_KEY", "")
        or os.getenv("OPENAI_API_KEY", "")
    ).strip()


def prefers_gateway(config_section: str) -> bool:
    """当用户为该工具选择启用工具网关时返回 True。

    从 config.yaml 读取 ``<section>.use_gateway``。永不抛出异常。
    """
    try:
        from hermes_cli.config import load_config
        section = (load_config() or {}).get(config_section)
        if isinstance(section, dict):
            return is_truthy_value(section.get("use_gateway"), default=False)
    except Exception:
        pass
    return False


def fal_key_is_configured() -> bool:
    """当 FAL_KEY 被设为非空白值时返回 True。

    同时查询 ``os.environ`` 与 ``~/.hermes/.env``（在可用时通过
    ``hermes_cli.config.get_env_value``），使工具侧检查与 CLI 安装时检查
    保持一致。仅含空白的值在所有地方都视为未设置。
    """
    value = os.getenv("FAL_KEY")
    if value is None:
        # 回退到 .env 文件，以覆盖那些可能在 dotenv 加载进 os.environ
        # 之前就运行的 CLI 路径。
        try:
            from hermes_cli.config import get_env_value

            value = get_env_value("FAL_KEY")
        except Exception:
            value = None
    return bool(value and value.strip())
