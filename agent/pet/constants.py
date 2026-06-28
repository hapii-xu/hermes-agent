"""宠物精灵几何 + 动画状态分类。

这些值是通用的 petdex/Codex 宠物几何参数。实际的 ``pet.json`` 通常只包含
``id``/``displayName``/``description``/``spritesheetPath``；行分类从 atlas 的
形状推断，因此 Hermes 可以渲染传统的 8 行 spritesheet 和当前 9 行的 Codex
spritesheet。
"""

from __future__ import annotations

from enum import Enum

# 帧几何（像素）。当前 Codex/petdex spritesheet 为 8 列 x 9 行
# （1536x1872），而较早的 Hermes/petdex spritesheet 使用 9 列 x 8 行
# （1728x1664）。渲染器从实际 spritesheet 派生行分类和真实列数，
# 因此两种尺寸都可以工作。
FRAME_W = 192
FRAME_H = 208

# 每个动画状态消耗的帧数（petdex Web 应用使用 CSS ``steps(6)``）。
# spritesheet 实际可能包含更多列；我们只步进前 ``FRAMES_PER_STATE`` 列。
FRAMES_PER_STATE = 6

# 一个状态的完整循环时长，毫秒（petdex 默认值）。
LOOP_MS = 1100

# 相对于原生帧尺寸的屏幕显示缩放比例。``display.pet.scale``
# 是唯一的总缩放因子：桌面 canvas 将原生像素乘以它，每个终端界面
# 都从它派生半块/kitty 列宽（参见 :func:`cols_for_scale`），因此一个数值
# 同时控制三个界面的缩放。（petdex 自身的客户端以 0.7 渲染；我们默认更小，
# 使 kitty/GUI 吉祥物保持为可一瞥的角落精灵。半块后备无法缩得那么小
# — 参见 ``UNICODE_MIN_COLS`` — 而是钳位到其可读下限。）
DEFAULT_SCALE = 0.33

# 用户可设置的缩放范围（``/pet scale``、桌面端滑块）。下限保持宠物可点击/可见；
# 上限防止误操作值占满屏幕。Unicode 后备还会钳位到 ``UNICODE_MIN_COLS``。
MIN_SCALE = 0.1
MAX_SCALE = 3.0


def clamp_scale(scale: float) -> float:
    """将 *scale* 钳位到 ``[MIN_SCALE, MAX_SCALE]``（唯一的验证点）。"""
    return max(MIN_SCALE, min(MAX_SCALE, scale))

# 在 ``scale == 1.0`` 时一个原生帧占据的终端单元格数。一个单元格约 8px
# 宽，一帧为 ``FRAME_W``（192）px → 24 个单元格。这与 kitty 图形放置
# （``scaled_px // 8``）一致，因此在满缩放时所有渲染器结果相同。
BASE_UNICODE_COLS = FRAME_W // 8

# 半块后备的可读下限。半块单元格仅以 1 个水平 + 2 个垂直采样点对精灵
# 进行采样，因此低于此宽度时，192×208 的宠物 *无论* 缩放比例如何都会
# 变成不可读的色块。kitty/GUI 绘制真实像素，没有这样的下限 — 这就是为什么
# 相同的 ``scale: 0.33`` 在 kitty 中清晰而在半块中模糊。``scale`` 将 Unicode
# 宠物缩小到此下限（并放大超过它），而不是缩小到噪声之中。
UNICODE_MIN_COLS = 16


def cols_for_scale(scale: float) -> int:
    """*scale* 所对应的半块宽度，钳位到可读下限。

    在下限之上，它跟踪 kitty 单元格框（``scaled_px // 8``），使两个渲染器在
    较大尺寸时趋于一致；在下限之下，下限保持精灵可读而不是退化成色块。
    """
    return max(UNICODE_MIN_COLS, round(BASE_UNICODE_COLS * (scale or DEFAULT_SCALE)))


def resolve_cols(scale: float, unicode_cols: int = 0) -> int:
    """解析终端宽度：显式 *unicode_cols* 覆盖，否则从 *scale* 计算。"""
    return int(unicode_cols) if unicode_cols and int(unicode_cols) > 0 else cols_for_scale(scale)


class PetState(str, Enum):
    """宠物可以显示的动画状态。

    这些是 Hermes 的活动状态名称。它们与源 atlas 行名称并不总是完全相同：
    Codex 格式的宠物使用 ``jumping`` / ``running`` 等行名，而 UI 保留较短的
    ``jump`` / ``run`` 名称。
    """

    IDLE = "idle"
    WAVE = "wave"
    RUN = "run"
    FAILED = "failed"
    REVIEW = "review"
    JUMP = "jump"
    WAITING = "waiting"


# 较早的 8 行、9 列 atlas 形状所使用的传统 Hermes/petdex 行顺序（从上到下）。
LEGACY_STATE_ROWS: list[str] = [
    PetState.IDLE.value,
    PetState.WAVE.value,
    PetState.RUN.value,
    PetState.FAILED.value,
    PetState.REVIEW.value,
    PetState.JUMP.value,
    "extra1",
    "extra2",
]

# 当前 Petdex 行顺序（从上到下），用于 1536x1872 atlas：
# 8 列 x 9 行，每个单元格 192x208。
CODEX_STATE_ROWS: list[str] = [
    PetState.IDLE.value,
    "running-right",
    "running-left",
    "waving",
    "jumping",
    PetState.FAILED.value,
    PetState.WAITING.value,
    "running",
    PetState.REVIEW.value,
]

# 没有 spritesheet 的调用者使用的默认/后备值。优先使用当前 9 行 Codex
# 格式，因为生成的宠物和公开的 Codex 宠物契约都使用它。
STATE_ROWS: list[str] = CODEX_STATE_ROWS

# Canonical Hermes activity names -> accepted row-name aliases in descending
# preference. This keeps our internal state names stable (`wave`/`jump`/`run`)
# while matching Petdex's current `waving`/`jumping`/`running` taxonomy.
STATE_ALIASES: dict[str, tuple[str, ...]] = {
    PetState.IDLE.value: (PetState.IDLE.value,),
    PetState.WAVE.value: (PetState.WAVE.value, "waving"),
    PetState.JUMP.value: (PetState.JUMP.value, "jumping"),
    PetState.RUN.value: (PetState.RUN.value, "running"),
    PetState.FAILED.value: (PetState.FAILED.value,),
    PetState.REVIEW.value: (PetState.REVIEW.value,),
    PetState.WAITING.value: (PetState.WAITING.value,),
}


def state_aliases_for(state: "PetState | str") -> tuple[str, ...]:
    """Return accepted row-name aliases for *state* (always non-empty)."""
    value = state.value if isinstance(state, PetState) else str(state)
    aliases = STATE_ALIASES.get(value)
    return aliases if aliases else (value,)


def state_rows_for_grid(row_count: int | None) -> list[str]:
    """Return the row taxonomy for a spritesheet with *row_count* rows."""
    try:
        rows = int(row_count or 0)
    except (TypeError, ValueError):
        rows = 0

    if rows >= len(CODEX_STATE_ROWS):
        return CODEX_STATE_ROWS
    return LEGACY_STATE_ROWS


def state_row_index(state: "PetState | str", row_count: int | None = None) -> int:
    """Return the spritesheet row index for *state* (clamped, never raises)."""
    rows = state_rows_for_grid(row_count)
    for name in state_aliases_for(state):
        try:
            return rows.index(name)
        except ValueError:
            continue
    return 0  # fall back to the idle row
