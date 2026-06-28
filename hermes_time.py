"""
Hermes 的时区感知时钟。

提供单个 ``now()`` 辅助函数，根据用户配置的 IANA 时区（例如 ``Asia/Kolkata``）
返回带时区信息的 datetime 对象。

解析顺序：
  1. ``HERMES_TIMEZONE`` 环境变量
  2. ``~/.hermes/config.yaml`` 中的 ``timezone`` 键
  3. 回退到服务器本地时间（``datetime.now().astimezone()``）

无效的时区值会记录一条警告并安全回退 —— Hermes 不会因为
错误的时区字符串而崩溃。
"""

import logging
import os
from datetime import datetime
from hermes_constants import get_config_path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Python 3.8 回退方案（通常不需要 —— Hermes 要求 3.9+）
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

# 缓存状态 —— 解析一次后，每次调用复用。
# 调用 reset_cache() 可强制重新解析（例如配置更改后）。
_cached_tz: Optional[ZoneInfo] = None
_cached_tz_name: Optional[str] = None
_cache_resolved: bool = False


def _resolve_timezone_name() -> str:
    """读取已配置的 IANA 时区字符串（或空字符串）。

    当回退到读取 config.yaml 时会执行文件 I/O，因此调用方
    应缓存结果，而不是在每次调用 ``now()`` 时都重新读取。
    """
    # 1. 环境变量（最高优先级 —— 由 Supervisor 等设置）
    tz_env = os.getenv("HERMES_TIMEZONE", "").strip()
    if tz_env:
        return tz_env

    # 2. config.yaml 中的 ``timezone`` 键
    try:
        import yaml
        config_path = get_config_path()
        if config_path.exists():
            with open(config_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            # 托管作用域：管理员也可以固定 ``timezone``。
            # 通过共享辅助函数进行叠加（失败时开放），因为此处直接读取 config.yaml。
            try:
                from hermes_cli import managed_scope
                cfg = managed_scope.apply_managed_overlay(cfg)
            except Exception:
                pass
            tz_cfg = cfg.get("timezone", "")
            if isinstance(tz_cfg, str) and tz_cfg.strip():
                return tz_cfg.strip()
    except Exception:
        pass

    return ""


def _get_zoneinfo(name: str) -> Optional[ZoneInfo]:
    """Validate and return a ZoneInfo, or None if invalid."""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (KeyError, Exception) as exc:
        logger.warning(
            "Invalid timezone '%s': %s. Falling back to server local time.",
            name, exc,
        )
        return None


def get_timezone() -> Optional[ZoneInfo]:
    """Return the user's configured ZoneInfo, or None (meaning server-local).

    Resolved once and cached. Call ``reset_cache()`` after config changes.
    """
    global _cached_tz, _cached_tz_name, _cache_resolved
    if not _cache_resolved:
        _cached_tz_name = _resolve_timezone_name()
        _cached_tz = _get_zoneinfo(_cached_tz_name)
        _cache_resolved = True
    return _cached_tz


def reset_cache() -> None:
    """Clear the cached timezone so the next call re-resolves it.

    Call this after the configured timezone may have changed (e.g. after a
    config edit or ``HERMES_TIMEZONE`` update) to force ``get_timezone()`` /
    ``now()`` to read the new value instead of the value cached at first use.
    """
    global _cached_tz, _cached_tz_name, _cache_resolved
    _cached_tz = None
    _cached_tz_name = None
    _cache_resolved = False


def now() -> datetime:
    """
    Return the current time as a timezone-aware datetime.

    If a valid timezone is configured, returns wall-clock time in that zone.
    Otherwise returns the server's local time (via ``astimezone()``).
    """
    tz = get_timezone()
    if tz is not None:
        return datetime.now(tz)
    # No timezone configured — use server-local (still tz-aware)
    return datetime.now().astimezone()


