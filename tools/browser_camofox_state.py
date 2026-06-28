"""Hermes 托管的 Camofox 状态辅助函数。

为 Camofox 持久化浏览器配置提供按 profile 作用域的身份信息和状态目录
路径。当启用了托管式持久化时，Hermes 会发送一个由当前 profile 派生出的
确定性 userId，使 Camofox 在多次重启后仍能把它映射到同一个持久化浏览器
配置目录。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Dict, Optional

from hermes_constants import get_hermes_home

CAMOFOX_STATE_DIR_NAME = "browser_auth"
CAMOFOX_STATE_SUBDIR = "camofox"


def get_camofox_state_dir() -> Path:
    """返回 Camofox 持久化使用的、按 profile 作用域的根目录。"""
    return get_hermes_home() / CAMOFOX_STATE_DIR_NAME / CAMOFOX_STATE_SUBDIR


def get_camofox_identity(task_id: Optional[str] = None) -> Dict[str, str]:
    """返回当前 profile 对应的、稳定的 Hermes 托管 Camofox 身份信息。

    用户身份按 profile 作用域（同一个 Hermes profile = 同一个 userId）。
    会话密钥按逻辑浏览器任务作用域，因此同一 profile 内新建的标签页会
    复用同一份身份契约。
    """
    scope_root = str(get_camofox_state_dir())
    logical_scope = task_id or "default"
    user_digest = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"camofox-user:{scope_root}",
    ).hex[:10]
    session_digest = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"camofox-session:{scope_root}:{logical_scope}",
    ).hex[:16]
    return {
        "user_id": f"hermes_{user_digest}",
        "session_key": f"task_{session_digest}",
    }
