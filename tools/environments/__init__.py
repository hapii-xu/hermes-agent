"""Hermes 执行环境后端。

每个后端都提供相同的接口（BaseEnvironment 抽象类），用于在特定的执行上下文中运行
shell 命令：本地、Docker、SSH、Singularity、Modal 或 Daytona。（Modal 还额外支持
直接模式和 Nous 托管模式，通过 terminal.modal_mode 选择。）

terminal_tool.py 工厂函数（_create_environment）根据 TERMINAL_ENV 配置选择后端。
"""

from tools.environments.base import BaseEnvironment

__all__ = ["BaseEnvironment"]
