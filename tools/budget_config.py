"""用于工具结果持久化的可配置预算常量。

按工具解析优先级：pinned > 配置覆盖 > registry > 默认值。
"""

from dataclasses import dataclass, field
from typing import Dict

# 阈值绝不允许被覆盖的工具。
# read_file=inf 防止无限循环 persist->read->persist。
PINNED_THRESHOLDS: Dict[str, float] = {
    "read_file": float("inf"),
}

# 与 tool_result_storage.py 中当前硬编码值一致的默认值。
# 在此作为唯一的真相来源；tool_result_storage.py 会导入这些值。
DEFAULT_RESULT_SIZE_CHARS: int = 100_000
DEFAULT_TURN_BUDGET_CHARS: int = 200_000
DEFAULT_PREVIEW_SIZE_CHARS: int = 1_500


@dataclass(frozen=True)
class BudgetConfig:
    """用于三层工具结果持久化系统的不可变预算常量。

    第 2 层（按结果）：resolve_threshold(tool_name) -> 以字符为单位的阈值。
    第 3 层（按轮次）：turn_budget -> 单个 assistant 轮次内所有工具
                          结果的合计字符预算。
    预览：              preview_size -> 持久化后的内联片段大小。
    """

    default_result_size: int = DEFAULT_RESULT_SIZE_CHARS
    turn_budget: int = DEFAULT_TURN_BUDGET_CHARS
    preview_size: int = DEFAULT_PREVIEW_SIZE_CHARS
    tool_overrides: Dict[str, int] = field(default_factory=dict)

    def resolve_threshold(self, tool_name: str) -> int | float:
        """解析某个工具的持久化阈值。

        优先级：pinned -> tool_overrides -> registry 按工具 -> 默认值。

        registry 的按工具值会被限制在 ``default_result_size`` 以内，这样
        一个按上下文缩放的预算（小模型）才能真正约束那些注册了较大固定
        ``max_result_size_chars`` 的工具（web/terminal/x_search 都注册了 100K）。
        对于默认预算这是空操作，因为两者都等于 100K；对于缩小后的预算，
        它能防止某个按工具的 registry 值把上限重新撑大到超过模型窗口（#23767）。
        """
        if tool_name in PINNED_THRESHOLDS:
            return PINNED_THRESHOLDS[tool_name]
        if tool_name in self.tool_overrides:
            return self.tool_overrides[tool_name]
        from tools.registry import registry
        registry_value = registry.get_max_result_size(tool_name, default=self.default_result_size)
        if registry_value == float("inf"):
            return registry_value
        return min(registry_value, self.default_result_size)


# 默认配置 —— 与当前硬编码行为完全一致。
DEFAULT_BUDGET = BudgetConfig()


# 在把预算缩放到模型的上下文窗口时所用的 token<->字符 转换。
# 刻意采用保守值：更小的除数（= 每个 token 更多字符 = 更大的字符预算）
# 会让小模型保护不足，所以我们采用估算器所用的同样约略的
# 每 token 4 字符比例（agent/model_metadata.py）。
_CHARS_PER_TOKEN: int = 4

# 在持久化/截断之前，我们允许单个工具结果占据模型上下文窗口的比例，
# 以及整个轮次的工具输出可占据的比例。工具输出并不是窗口里唯一的东西
#（系统提示词、工具 schema、对话历史、模型自身的回复都在竞争），
# 所以这些值都远低于 1.0。
_PER_RESULT_WINDOW_FRACTION: float = 0.15
_PER_TURN_WINDOW_FRACTION: float = 0.30

# 下限值：保证即便是一个体量极小但仍被允许的模型也能得到可用的预览/结果，
# 而不是一个 0 字符的预算。
_MIN_RESULT_SIZE_CHARS: int = 8_000
_MIN_TURN_BUDGET_CHARS: int = 16_000


def budget_for_context_window(context_length: int | None) -> BudgetConfig:
    """返回一个按当前模型上下文窗口缩放后的 BudgetConfig。

    固定默认值（100K 结果 / 200K 轮次字符）对于大型
    （200K+ token）模型是正确的，但对小模型却视而不见：在一个 65K-token
    的模型上，按 100K 字符阈值持久化的单个工具结果，或一个 200K 字符的
    轮次预算（约 50K token），仅凭自身就可能接近或超过整个窗口，
    并迫使请求过大（#23767）。

    缩放让大模型与今天逐字节一致（比例值被限制在现有默认值作为上限），
    同时按小模型窗口的比例缩小其预算，并设有下限，
    使一个可用的预览总能保留下来。
    """
    if not context_length or context_length <= 0:
        return DEFAULT_BUDGET

    window_chars = context_length * _CHARS_PER_TOKEN
    per_result = int(window_chars * _PER_RESULT_WINDOW_FRACTION)
    per_turn = int(window_chars * _PER_TURN_WINDOW_FRACTION)

    # 夹取：永不超过历史默认值（让大模型保持不变），
    # 也永不低于下限（让极小模型保持可用）。
    per_result = max(_MIN_RESULT_SIZE_CHARS, min(per_result, DEFAULT_RESULT_SIZE_CHARS))
    per_turn = max(_MIN_TURN_BUDGET_CHARS, min(per_turn, DEFAULT_TURN_BUDGET_CHARS))

    return BudgetConfig(
        default_result_size=per_result,
        turn_budget=per_turn,
        preview_size=DEFAULT_PREVIEW_SIZE_CHARS,
    )
