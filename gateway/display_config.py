"""按平台的显示/详尽程度配置解析器。

提供 ``resolve_display_setting()`` ——读取显示设置的唯一入口，支持按平台
覆盖并提供合理的默认值。

解析顺序（取第一个非 None 的值）：
    1. ``display.platforms.<platform>.<key>``  ——显式的按平台用户覆盖
    2. ``display.<key>``                       ——全局用户设置
    3. ``_PLATFORM_DEFAULTS[<platform>][<key>]``  ——内置的合理默认值
    4. ``_GLOBAL_DEFAULTS[<key>]``              ——内置全局默认值

例外：``display.streaming`` 仅对 CLI 生效。Gateway 的流式输出遵循顶层
``streaming`` 配置，除非 ``display.platforms.<platform>.streaming`` 设置了
显式的按平台覆盖。

向后兼容：当不存在 ``display.platforms`` 条目时，``display.tool_progress_overrides``
仍作为 ``tool_progress`` 的回退读取。配置迁移（版本号提升）会自动将旧格式
迁移到新的 ``display.platforms`` 结构。
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 可按平台覆盖的显示设置及其全局默认值
# ---------------------------------------------------------------------------
# 这些是可按平台配置的设置。其他显示设置（compact、personality、skin 等）
# 仅对 CLI 生效，不参与按平台解析。

_GLOBAL_DEFAULTS: dict[str, Any] = {
    "tool_progress": "all",
    "tool_progress_grouping": "accumulate",  # "accumulate" = 编辑同一条气泡；"separate" = 每个工具一条消息
    "show_reasoning": False,
    # 当 show_reasoning 开启时，推理/思考摘要的渲染方式。
    #   "code"      -> 💭 **Reasoning:** + 围栏代码块（旧默认值）
    #   "blockquote"-> 每行加 "> " 前缀
    #   "subtext"   -> 每行加 "-# " 前缀（Discord 的小号灰色子文本）
    # Discord 默认为 "subtext"；其他平台默认为 "code"。
    "reasoning_style": "code",
    "tool_preview_length": 0,
    "streaming": None,  # None = 遵循顶层 streaming 配置
    # 仅对 Gateway 生效的助手/status 喋喋控制。这些默认开启以保持向后兼容，
    # 但移动端平台可以选择“先给最终答案”。
    "interim_assistant_messages": True,
    "long_running_notifications": True,
    "busy_ack_detail": True,
    # 为 True 时，在支持消息删除的平台（例如 Telegram）上，当最终回复发出后
    # 删除工具进度 / "⏳ Working — N min" / status 气泡。默认关闭 —— 进度仍然
    # 实时显示，只是在成功后清理掉，以免聊天里堆满过期的痕迹。失败的运行会
    # 保留气泡作为痕迹。
    "cleanup_progress": False,
}

# ---------------------------------------------------------------------------
# 按平台的合理默认值 —— 按平台能力分层
# ---------------------------------------------------------------------------
# Tier 1（高）：支持消息编辑，通常是个人/团队使用
# Tier 2（中）：支持编辑但常用于工作区/面向客户
# Tier 3（低）：不支持编辑 —— 每条进度消息都是永久的
# Tier 4（极简）：批量/非交互式投递

_TIER_HIGH = {
    "tool_progress": "all",
    "show_reasoning": False,
    "tool_preview_length": 40,
    "streaming": None,  # follow global
    "interim_assistant_messages": True,
    "long_running_notifications": True,
    "busy_ack_detail": True,
}

_TIER_MEDIUM = {
    "tool_progress": "new",
    "show_reasoning": False,
    "tool_preview_length": 40,
    "streaming": None,
    "interim_assistant_messages": True,
    "long_running_notifications": True,
    "busy_ack_detail": True,
}

_TIER_LOW = {
    "tool_progress": "off",
    "show_reasoning": False,
    "tool_preview_length": 40,
    "streaming": False,
    "interim_assistant_messages": False,
    "long_running_notifications": False,
    "busy_ack_detail": False,
}

_TIER_MINIMAL = {
    "tool_progress": "off",
    "show_reasoning": False,
    "tool_preview_length": 0,
    "streaming": False,
    "interim_assistant_messages": False,
    "long_running_notifications": False,
    "busy_ack_detail": False,
}

_PLATFORM_DEFAULTS: dict[str, dict[str, Any]] = {
    # Tier 1 —— 完整编辑支持，个人/团队使用
    # Telegram 通常是移动端的收件箱：保持 tool_progress 安静，跳过冗长的
    # busy-ack 迭代计数器，但务必呈现真正的轮中途助手评论
    # （interim_assistant_messages），并定期发送心跳
    # （long_running_notifications），让用户在轮开始到最终答案之间有信号。
    # 否则看起来就像"正在输入..."持续 30 分钟却什么都没发生。可通过
    # display.platforms.telegram.busy_ack_detail / tool_progress 显式开启
    # 冗长的迭代细节。
    "telegram":    {
        **_TIER_HIGH,
        "tool_progress": "off",
        "busy_ack_detail": False,
    },
    # Discord 有原生的 "subtext" 原语（-# 小号灰色文本），读起来像元数据而
    # 不是内容，因此这里推理摘要默认使用它，而不是其他地方使用的围栏代码块。
    "discord":     {**_TIER_HIGH, "reasoning_style": "subtext"},

    # Tier 2 —— 支持编辑，常用于客户/工作区频道
    # Slack：默认关闭 tool_progress —— Bolt 发布的消息无法像 CLI 那样编辑；
    # "new"/"all" 会在频道里刷出永久的行（hermes-agent#14663）。
    "slack":           {**_TIER_MEDIUM, "tool_progress": "off"},
    "mattermost":      _TIER_MEDIUM,
    "matrix":          _TIER_MEDIUM,
    "feishu":          _TIER_MEDIUM,

    # Tier 3 —— 不支持编辑，进度消息是永久的
    "signal":          _TIER_LOW,
    "whatsapp":        _TIER_MEDIUM,  # Baileys 桥接支持 /edit
    # WhatsApp Cloud API：Meta 在 2023 年增加了消息编辑功能，但 Hermes Cloud
    # 适配器尚未实现 edit_message，所以我们保持在 TIER_LOW
    # （tool_progress off），以免每次 status 更新都作为单独消息刷屏。等
    # Cloud 的 edit_message 落地后再升级到 TIER_MEDIUM。
    "whatsapp_cloud":  _TIER_LOW,
    "bluebubbles":     _TIER_LOW,
    "weixin":          _TIER_LOW,
    "wecom":           _TIER_LOW,
    "wecom_callback":  _TIER_LOW,
    "dingtalk":        _TIER_LOW,

    # Tier 4 —— 批量或非交互式投递
    "email":           _TIER_MINIMAL,
    "sms":             _TIER_MINIMAL,
    "webhook":         _TIER_MINIMAL,
    "homeassistant":   _TIER_MINIMAL,
    "api_server":      {**_TIER_HIGH, "tool_preview_length": 0},
}

# 可按平台覆盖的键的规范集合（用于校验）。
OVERRIDEABLE_KEYS = frozenset(_GLOBAL_DEFAULTS.keys())


def resolve_display_setting(
    user_config: dict,
    platform_key: str,
    setting: str,
    fallback: Any = None,
) -> Any:
    """解析带按平台覆盖支持的显示设置。

    Parameters
    ----------
    user_config : dict
        完整解析后的 config.yaml 字典。
    platform_key : str
        平台配置键（例如 ``"telegram"``、``"slack"``）。使用
        gateway/run.py 中的 ``_platform_config_key(source.platform)``。
    setting : str
        显示设置名（例如 ``"tool_progress"``、``"show_reasoning"``）。
    fallback : Any
        当设置在任何地方都找不到时的回退值。

    Returns
    -------
    解析出的值，或未配置任何内容时的 *fallback*。
    """
    display_cfg = user_config.get("display") or {}

    # 1. 显式的按平台覆盖（display.platforms.<platform>.<key>）
    platforms = display_cfg.get("platforms") or {}
    plat_overrides = platforms.get(platform_key)
    if isinstance(plat_overrides, dict):
        val = plat_overrides.get(setting)
        if val is not None:
            return _normalise(setting, val)

    # 1b. 向后兼容：display.tool_progress_overrides.<platform>
    if setting == "tool_progress":
        legacy = display_cfg.get("tool_progress_overrides")
        if isinstance(legacy, dict):
            val = legacy.get(platform_key)
            if val is not None:
                return _normalise(setting, val)

    # 2. 全局用户设置（display.<key>）。跳过 display.streaming，因为该键
    # 只控制 CLI 终端的流式输出；gateway 的 token 流式输出由顶层 streaming
    # 配置加上按平台覆盖来决定。
    if setting != "streaming":
        val = display_cfg.get(setting)
        if val is not None:
            return _normalise(setting, val)

    # 3. 内置的平台默认值
    plat_defaults = _PLATFORM_DEFAULTS.get(platform_key)
    if plat_defaults:
        val = plat_defaults.get(setting)
        if val is not None:
            return val

    # 4. 内置的全局默认值
    val = _GLOBAL_DEFAULTS.get(setting)
    if val is not None:
        return val

    return fallback


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _normalise(setting: str, value: Any) -> Any:
    """规范化 YAML 的怪异行为（YAML 1.1 中裸 ``off`` → False）。"""
    if setting == "tool_progress":
        if value is False:
            return "off"
        if value is True:
            return "all"
        return str(value).lower()
    if setting in {
        "show_reasoning",
        "streaming",
        "interim_assistant_messages",
        "long_running_notifications",
        "busy_ack_detail",
    }:
        if isinstance(value, str):
            return value.lower() in {"true", "1", "yes", "on"}
        return bool(value)
    if setting == "cleanup_progress":
        if isinstance(value, str):
            return value.lower() in {"true", "1", "yes", "on"}
        return bool(value)
    if setting == "tool_progress_grouping":
        val = str(value).lower()
        return val if val in ("accumulate", "separate") else "accumulate"
    if setting == "reasoning_style":
        val = str(value).lower()
        return val if val in ("code", "blockquote", "subtext") else "code"
    if setting == "tool_preview_length":
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return value
