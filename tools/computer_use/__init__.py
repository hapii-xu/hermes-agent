"""Computer use 工具集——通用的（适配任意模型）macOS 桌面控制。

架构
----
本工具集通过 cua-driver 的后台 computer-use 原语（利用 SkyLight 私有 SPI
实现"不前置抢焦"以及按 pid 作用域的事件投递）来驱动 macOS 应用。与 #4562
的 pyautogui 后端不同，它不会抢占用户的光标、键盘焦点或 Space——agent 和
用户可以在同一台机器上协同工作。

与 #4562 的 Anthropic 原生 `computer_20251124` 工具不同，这里的 schema 是
一份普通的 OpenAI function-calling schema，任何支持工具调用的模型都能驱动。
视觉模型会拿到 SOM（set-of-mark）抓图——即在每一个可交互元素上叠加编号的
截图，外加 AX 树——从而通过元素索引而非像素坐标进行点击。非视觉模型则可以
仅凭 AX 树来驱动。

接线
----
* `tool.py`       —— 通过 tools.registry 注册 `computer_use` 工具。
* `backend.py`    —— 抽象类 `ComputerUseBackend`；可替换的实现。
* `cua_backend.py`—— 默认后端；通过 stdio 与 `cua-driver` 进行 MCP 通信。
* `schema.py`     —— 通用 `computer_use` 工具共享的 schema + docstring。
                    与具体模型无关。
* `capture.py`    —— 截图后处理（强制转 PNG、调整尺寸，以及在后端未处理时
                    叠加 SOM）。

其余的外部集成点（多模态工具结果的管线、Anthropic 适配器中的截图淘汰、
感知图像的 token 估算、COMPUTER_USE_GUIDANCE 提示块、审批钩子以及技能）
位于本包之外。从 PR #4562 中抢救出来的代码块见 agent/anthropic_adapter.py
和 agent/prompt_builder.py。
"""

from __future__ import annotations

# 重新导出对外公开的接口，以便 `from tools.computer_use import ...` 能正常工作。
from tools.computer_use.tool import (  # noqa: F401
    handle_computer_use,
    set_approval_callback,
    check_computer_use_requirements,
    get_computer_use_schema,
)
