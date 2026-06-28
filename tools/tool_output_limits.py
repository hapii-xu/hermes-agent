"""可配置的工具输出截断限制。

移植自 anomalyco/opencode PR #23770（``feat(truncate): allow
configuring tool output truncation limits``）。

OpenCode 把 ``MAX_LINES = 2000`` 和 ``MAX_BYTES = 50 * 1024``
硬编码为工具输出的截断阈值。Hermes-agent 此前在两处使用了同样的
硬编码常量：

* ``tools/terminal_tool.py`` — ``MAX_OUTPUT_CHARS = 50000``（终端
  stdout/stderr 上限）
* ``tools/file_operations.py`` — ``MAX_LINES = 2000`` /
  ``MAX_LINE_LENGTH = 2000``（read_file 分页上限 + 单行上限）

本模块把这些值集中到一个统一的配置节（``config.yaml`` 中的
``tool_output``）背后，让进阶用户无需改动源码即可调整。原有的
硬编码数值保留为默认值，因此在缺少该配置键时行为保持不变。

示例 ``config.yaml``::

    tool_output:
      max_bytes: 100000        # 终端输出上限（字符数）
      max_lines: 5000          # read_file 分页 + 截断上限
      max_line_length: 2000    # 触发 '... [truncated]' 前的单行长度上限

限制读取器采用防御式实现：任何错误（配置文件缺失、值类型非法等）
都会回退到内置默认值，因此工具绝不会因为配置格式错误而失败。
"""

from __future__ import annotations

from typing import Any, Dict

# 硬编码默认值——这些与既有数值一致，因此对于未在 config.yaml 中设置
# ``tool_output`` 的用户，加入本模块不会改变任何行为。
DEFAULT_MAX_BYTES = 50_000       # terminal_tool.MAX_OUTPUT_CHARS
DEFAULT_MAX_LINES = 2000         # file_operations.MAX_LINES
DEFAULT_MAX_LINE_LENGTH = 2000   # file_operations.MAX_LINE_LENGTH

# 模块级缓存——在首次调用时填充。
# 避免每次工具调用都重复读取配置文件造成 I/O。
_cached_limits: dict | None = None


def _coerce_positive_int(value: Any, default: int) -> int:
    """将 ``value`` 转为正整数；若出现任何问题则返回 ``default``。"""
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return default
    if iv <= 0:
        return default
    return iv


def get_tool_output_limits() -> Dict[str, int]:
    """返回解析后的工具输出限制，从配置中读取 ``tool_output``。

    键包括：``max_bytes``、``max_lines``、``max_line_length``。缺失或
    非法的条目会回退到 ``DEFAULT_*`` 常量。本函数绝不抛出异常。

    结果会在进程生命周期内被缓存，避免每次工具调用都重复磁盘 I/O。
    在配置变更后需要重新读取的测试中，可调用
    ``_reset_tool_output_limits_cache()``。
    """
    global _cached_limits
    if _cached_limits is not None:
        return _cached_limits
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        section = cfg.get("tool_output") if isinstance(cfg, dict) else None
        if not isinstance(section, dict):
            section = {}
    except Exception:
        section = {}

    _cached_limits = {
        "max_bytes": _coerce_positive_int(section.get("max_bytes"), DEFAULT_MAX_BYTES),
        "max_lines": _coerce_positive_int(section.get("max_lines"), DEFAULT_MAX_LINES),
        "max_line_length": _coerce_positive_int(
            section.get("max_line_length"), DEFAULT_MAX_LINE_LENGTH
        ),
    }
    return _cached_limits


def _reset_tool_output_limits_cache() -> None:
    """重置缓存的限制——用于测试或配置热重载之后。"""
    global _cached_limits
    _cached_limits = None


def get_max_bytes() -> int:
    """快捷方式：供只需要字节上限的 terminal-tool 调用方使用。"""
    return get_tool_output_limits()["max_bytes"]


def get_max_lines() -> int:
    """快捷方式：供只需要行数上限的 file-ops 调用方使用。"""
    return get_tool_output_limits()["max_lines"]


def get_max_line_length() -> int:
    """快捷方式：供只需要单行长度上限的 file-ops 调用方使用。"""
    return get_tool_output_limits()["max_line_length"]
