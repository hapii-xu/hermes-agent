"""Agent 内部模块 —— 从 run_agent.py 提取的子模块。

这些模块包含纯工具函数和自包含的类，
原本嵌入在 3600 行的 run_agent.py 中。
提取之后，run_agent.py 可以专注于 AIAgent 编排器类本身。
"""

from . import jiter_preload as _jiter_preload  # noqa: F401
