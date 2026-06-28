"""agent 工作目录的唯一权威来源。

`TERMINAL_CWD` 是已配置工作目录的运行时载体
（设计 #19214/#19242：`terminal.cwd` 在 gateway/cron 启动时
一次性桥接到 `TERMINAL_CWD`）。
本地 CLI 后端故意不设置该变量，依赖启动目录。
在一处读取此值可确保系统提示词、工具界面和上下文文件发现
对 agent 所在位置保持一致。

多会话 gateway 可通过 `_SESSION_CWD` contextvar 固定逻辑 cwd；
CLI/cron 则回退到 `TERMINAL_CWD`/启动 cwd。
"""

import os
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

_UNSET: Any = object()

_SESSION_CWD: ContextVar = ContextVar("HERMES_SESSION_CWD", default=_UNSET)


def set_session_cwd(cwd: str | None) -> Token:
    """为当前上下文固定逻辑 cwd。"""
    return _SESSION_CWD.set((cwd or "").strip())


def clear_session_cwd() -> None:
    _SESSION_CWD.set("")


def _session_cwd_override() -> str:
    value = _SESSION_CWD.get()
    if value is _UNSET:
        return ""
    return str(value).strip()


def resolve_agent_cwd() -> Path:
    override = _session_cwd_override()
    if override:
        p = Path(override).expanduser()
        if p.is_dir():
            return p
    raw = os.environ.get("TERMINAL_CWD", "").strip()
    if raw:
        p = Path(raw).expanduser()
        if p.is_dir():
            return p
    return Path(os.getcwd())


def resolve_context_cwd() -> Path | None:
    # None 表示"未配置 cwd"：build_context_files_prompt 随后回退到
    # 启动目录（os.getcwd()）—— 对本地 CLI 是正确的。
    # gateway 通过设置 TERMINAL_CWD（见 system_prompt.py）或
    # 逐会话使用上方的 _SESSION_CWD contextvar 来避免读取其安装目录。
    override = _session_cwd_override()
    if override:
        return Path(override).expanduser()
    raw = os.environ.get("TERMINAL_CWD", "").strip()
    return Path(raw).expanduser() if raw else None
