"""QQBot 共享工具函数 — User-Agent、HTTP 辅助函数、配置值类型转换。"""

from __future__ import annotations

import platform
import sys
from typing import Any, Dict, List

from .constants import QQBOT_VERSION


# ---------------------------------------------------------------------------
# User-Agent
# ---------------------------------------------------------------------------

def _get_hermes_version() -> str:
    """返回 hermes-agent 包的版本号，若无法获取则返回 'dev'。"""
    try:
        from importlib.metadata import version
        return version("hermes-agent")
    except Exception:
        return "dev"


def build_user_agent() -> str:
    """构建描述性的 User-Agent 字符串。

    格式::

        QQBotAdapter/<qqbot_version> (Python/<py_version>; <os>; Hermes/<hermes_version>)

    示例::

        QQBotAdapter/1.0.0 (Python/3.11.15; darwin; Hermes/0.9.0)
    """
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    os_name = platform.system().lower()
    hermes_version = _get_hermes_version()
    return f"QQBotAdapter/{QQBOT_VERSION} (Python/{py_version}; {os_name}; Hermes/{hermes_version})"


def get_api_headers() -> Dict[str, str]:
    """返回 QQBot API 请求的标准 HTTP 请求头。

    包含 ``Content-Type``、``Accept`` 和动态生成的 ``User-Agent``。
    ``q.qq.com`` 要求携带 ``Accept: application/json``，否则
    服务器会返回 JavaScript 反爬虫挑战页面。
    """
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": build_user_agent(),
    }


# ---------------------------------------------------------------------------
# 配置辅助函数
# ---------------------------------------------------------------------------

def coerce_list(value: Any) -> List[str]:
    """将配置值转换为去除首尾空白的字符串列表。

    接受逗号分隔的字符串、列表、元组、集合或单一值。
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []
