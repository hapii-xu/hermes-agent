"""Dashboard auth 事件的审计日志。

支持 Profile 感知路径：``$HERMES_HOME/logs/dashboard-auth.log``。
格式：每行一个 JSON 对象。类似 token 的字段在序列化前会被清除，
以避免将 refresh token 或 JWT 泄露到磁盘。

本模块有意保持最小的依赖面——不从 ``hermes_constants`` 或其他
hermes_cli 模块导入——以便可以在启动序列中早期加载的中间件代码中安全导入。
"""
from __future__ import annotations

import datetime as _dt
import enum
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)
_write_lock = threading.Lock()

# 不允许在日志中原始出现的字段名。任何匹配这些名称的 kwarg 都会被静默丢弃。
_REDACTED_FIELDS: frozenset = frozenset({
    "access_token", "refresh_token", "code", "code_verifier",
    "state", "ticket", "cookie", "Authorization", "authorization",
})


class AuditEvent(enum.Enum):
    """写入 dashboard-auth.log 的事件类型。

    值是 JSON 行中 ``event`` 字段的字面值。
    """

    LOGIN_START = "login_start"
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    LOGOUT = "logout"
    REFRESH_SUCCESS = "refresh_success"
    REFRESH_FAILURE = "refresh_failure"
    REVOKE = "revoke"
    SESSION_VERIFY_FAILURE = "session_verify_failure"
    WS_TICKET_MINTED = "ws_ticket_minted"
    WS_TICKET_REJECTED = "ws_ticket_rejected"


def _resolve_log_path() -> Path:
    """``$HERMES_HOME/logs/dashboard-auth.log``，带标准回退。

    镜像 ``hermes_constants.get_hermes_home`` 的语义：环境变量优先，
    否则使用 ``~/.hermes``。本地副本避免了与位于 ``hermes_cli`` 下层
    的中间件之间的导入循环。
    """
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / "logs" / "dashboard-auth.log"


def audit_log(event: AuditEvent, **fields: Any) -> None:
    """将一个事件追加到审计日志。

    类似 token 的字段会被丢弃。缺失的日志目录会被创建。
    写入失败会以 WARNING 级别记录但不会抛出异常——认证不能因为
    审计日志出错而失败。
    """
    safe_fields = {
        k: v for k, v in fields.items()
        if k not in _REDACTED_FIELDS
    }
    entry = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "event": event.value,
        **safe_fields,
    }
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    path = _resolve_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:
        _log.warning("dashboard-auth audit log write failed: %s", e)
