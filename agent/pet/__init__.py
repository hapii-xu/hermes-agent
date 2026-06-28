"""Petdex 宠物引擎 — CLI、TUI 和桌面端的共享核心。

Petdex (https://github.com/crafter-station/petdex) 是一个公开的动画精灵
"宠物" 画廊，供编码代理使用。每个宠物由一个 ``pet.json`` 和一个
``spritesheet.{webp,png}``（192×208 像素单元格）组成。当前 Codex/petdex
spritesheet 使用 8 列 × 9 行的 atlas；较早的 Hermes/petdex spritesheet 使用
8 行的 atlas。Hermes 根据 spritesheet 的形状推断行分类，并将代理活动映射到
idle/run/review/failed/wave/jump 状态。

本包是该功能的 **唯一真相来源**，因此基础 CLI（Python）和 TUI（Ink，通过
``tui_gateway``）无需重复实现核心逻辑：

- :mod:`agent.pet.constants` — 帧几何 + :class:`PetState` 枚举。
- :mod:`agent.pet.state`     — 将代理活动映射到 :class:`PetState`。
- :mod:`agent.pet.manifest`  — 获取公开的 petdex 清单。
- :mod:`agent.pet.store`     — 在磁盘上安装/列出/解析宠物
                               （通过 ``get_hermes_home()`` 感知 profile）。
- :mod:`agent.pet.render`    — 解码 spritesheet 并为终端编码帧
                               （kitty / iTerm2 / sixel 图形协议，
                               并以 Unicode 半块作为后备）。

Electron 桌面端的渲染必然是 TypeScript（canvas），但它复用相同的磁盘存储
和相同的状态语义。

整个功能是一个 *显示* 关注点：它不添加模型工具，不修改系统提示或工具集，
因此对 prompt 缓存没有影响。
"""

from agent.pet.constants import (
    DEFAULT_SCALE,
    FRAME_H,
    FRAME_W,
    FRAMES_PER_STATE,
    LOOP_MS,
    STATE_ROWS,
    PetState,
)
from agent.pet.state import derive_pet_state

__all__ = [
    "DEFAULT_SCALE",
    "FRAME_H",
    "FRAME_W",
    "FRAMES_PER_STATE",
    "LOOP_MS",
    "STATE_ROWS",
    "PetState",
    "derive_pet_state",
]
