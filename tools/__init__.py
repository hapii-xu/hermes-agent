#!/usr/bin/env python3
"""Tools 包命名空间。

保持包导入的副作用尽可能小。导入 ``tools`` 时不应该急切地导入整个工具栈，
因为有多个子系统会在 ``hermes_cli.config`` 尚未完成初始化时加载工具。

调用方应当直接导入具体的子模块，例如：

    import tools.web_tools
    from tools import browser_tool

Python 会通过包路径解析这些子模块，无需在此处重新导出。
"""


def check_file_requirements():
    """文件工具仅要求终端后端可用。"""
    from .terminal_tool import check_terminal_requirements

    return check_terminal_requirements()


__all__ = ["check_file_requirements"]
