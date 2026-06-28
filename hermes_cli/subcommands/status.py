"""``hermes status`` 子命令解析器。

从 ``hermes_cli/main.py:main()`` 中原样提取（god-file 阶段 2）。
通过注入处理器避免导入 ``main``。
"""

from __future__ import annotations

from typing import Callable


def build_status_parser(subparsers, *, cmd_status: Callable) -> None:
    """将 ``status`` 子命令附加到 ``subparsers``。"""
    # =========================================================================
    # status 命令
    # =========================================================================
    status_parser = subparsers.add_parser(
        "status",
        help="Show status of all components",
        description="Display status of Hermes Agent components",
    )
    status_parser.add_argument(
        "--all", action="store_true", help="Show all details (redacted for sharing)"
    )
    status_parser.add_argument(
        "--deep", action="store_true", help="Run deep checks (may take longer)"
    )
    status_parser.set_defaults(func=cmd_status)
