"""
Gateway 配置管理。

负责加载和校验以下内容的配置：
- 已连接的平台（Telegram、Discord、WhatsApp、Weixin 等）
- 每个平台的 home 频道
- session 重置策略
- 投递偏好
"""

import logging
import os
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Callable
from enum import Enum

from hermes_cli.config import get_hermes_home
from utils import env_int, is_truthy_value

logger = logging.getLogger(__name__)


def _coerce_bool(value: Any, default: bool = True) -> bool:
    """强制转换布尔型配置值，保留调用方提供的默认值。"""
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        return default
    return is_truthy_value(value, default=default)


def _coerce_float(value: Any, default: float) -> float:
    """强制转换数值型配置值，遇到畸形输入时回退。"""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int) -> int:
    """强制转换整数型配置值，遇到畸形输入时回退。"""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_optional_positive_int(value: Any, key: str) -> Optional[int]:
    """强制转换一个可选的正整数配置值。

    ``None``/0/负数会禁用该设置。畸形值会被忽略并给出警告，这样一次拼写错误
    绝不会阻止 gateway 启动。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        logger.warning(
            "Ignoring invalid %s=%r (expected a positive integer; 0/null disables)",
            key,
            value,
        )
        return None
    try:
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError(value)
            parsed = int(value)
        elif isinstance(value, str):
            parsed = int(value.strip(), 10)
        else:
            parsed = int(value)
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring invalid %s=%r (expected a positive integer; 0/null disables)",
            key,
            value,
        )
        return None
    if parsed <= 0:
        return None
    return parsed


def _normalize_unauthorized_dm_behavior(value: Any, default: str = "pair") -> str:
    """将未授权 DM 行为规范化为受支持的值。"""
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"pair", "ignore"}:
            return normalized
    return default


def _normalize_notice_delivery(value: Any, default: str = "public") -> str:
    """将通知投递模式规范化为受支持的值。"""
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"public", "private"}:
            return normalized
    return default


def _ensure_platform_extra_dict(platforms_data: dict, name: str) -> tuple[dict, dict]:
    """获取或创建 ``platforms_data[name]`` 及其嵌套的 ``extra`` 字典。

    遇到非字典值时，两个槽都被强制为 ``{}``，以便调用方无需类型检查即可
    安全写入键。返回 ``(plat_data, extra)`` 供就地修改。
    """
    plat_data = platforms_data.setdefault(name, {})
    if not isinstance(plat_data, dict):
        plat_data = {}
        platforms_data[name] = plat_data
    extra = plat_data.setdefault("extra", {})
    if not isinstance(extra, dict):
        extra = {}
        plat_data["extra"] = extra
    return plat_data, extra


# 用于捆绑平台插件名的模块级缓存（放在 enum 之外，以免它意外成为 enum 成员）。
_Platform__bundled_plugin_names: Optional[set] = None


class Platform(Enum):
    """受支持的消息平台。

    内置平台有显式成员。插件平台使用由 ``_missing_()`` 按需创建的动态成员，
    这样 ``Platform("irc")`` 无需修改此 enum 即可工作。动态成员缓存在
    ``_value2member_map_`` 中，以保证身份稳定的比较。
    """
    LOCAL = "local"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    WHATSAPP = "whatsapp"
    WHATSAPP_CLOUD = "whatsapp_cloud"
    SLACK = "slack"
    SIGNAL = "signal"
    MATTERMOST = "mattermost"
    MATRIX = "matrix"
    HOMEASSISTANT = "homeassistant"
    EMAIL = "email"
    SMS = "sms"
    DINGTALK = "dingtalk"
    API_SERVER = "api_server"
    WEBHOOK = "webhook"
    MSGRAPH_WEBHOOK = "msgraph_webhook"
    FEISHU = "feishu"
    WECOM = "wecom"
    WECOM_CALLBACK = "wecom_callback"
    WEIXIN = "weixin"
    BLUEBUBBLES = "bluebubbles"
    QQBOT = "qqbot"
    YUANBAO = "yuanbao"
    RELAY = "relay"  # 由 connector 托管的通用 relay 适配器（实验性）
    @classmethod
    def _missing_(cls, value):
        """仅对已知插件适配器接受未知的平台名。

        创建一个缓存在 ``_value2member_map_`` 中的伪成员，使
        ``Platform("irc") is Platform("irc")`` 成立（身份稳定）。任意字符串会被
        拒绝以防 enum 污染。
        """
        if not isinstance(value, str) or not value.strip():
            return None
        # 规范化为小写，避免配置中的大小写不匹配
        value = value.strip().lower()
        # 先查缓存（另一个调用可能已经创建了它）
        if value in cls._value2member_map_:
            return cls._value2member_map_[value]

        # 仅为捆绑的插件平台（通过文件系统扫描发现）或运行时注册的插件平台
        # 创建伪成员。
        global _Platform__bundled_plugin_names
        if _Platform__bundled_plugin_names is None:
            _Platform__bundled_plugin_names = cls._scan_bundled_plugin_platforms()
        if value in _Platform__bundled_plugin_names:
            pseudo = object.__new__(cls)
            pseudo._value_ = value
            pseudo._name_ = value.upper().replace("-", "_").replace(" ", "_")
            cls._value2member_map_[value] = pseudo
            cls._member_map_[pseudo._name_] = pseudo
            return pseudo

        # 运行时注册的插件（例如用户安装的，在 enum 定义之后才发现）。
        try:
            from gateway.platform_registry import platform_registry
            if platform_registry.is_registered(value):
                pseudo = object.__new__(cls)
                pseudo._value_ = value
                pseudo._name_ = value.upper().replace("-", "_").replace(" ", "_")
                cls._value2member_map_[value] = pseudo
                cls._member_map_[pseudo._name_] = pseudo
                return pseudo
        except Exception:
            pass

        return None

    @classmethod
    def _scan_bundled_plugin_platforms(cls) -> set:
        """返回 ``plugins/platforms/`` 下捆绑的平台插件的名称。"""
        names: set = set()
        try:
            platforms_dir = Path(__file__).parent.parent / "plugins" / "platforms"
            if platforms_dir.is_dir():
                for child in platforms_dir.iterdir():
                    if (
                        child.is_dir()
                        and (child / "__init__.py").exists()
                        and (
                            (child / "plugin.yaml").exists()
                            or (child / "plugin.yml").exists()
                        )
                    ):
                        names.add(child.name.lower())
        except Exception:
            pass
        return names


# 在任何动态 _missing_ 查找之前，内置平台值的快照。用于区分真实平台与
# 任意字符串。
_BUILTIN_PLATFORM_VALUES = frozenset(m.value for m in Platform.__members__.values())


@dataclass
class HomeChannel:
    """
    平台的默认目标地。

    当 cron 任务指定 deliver="telegram" 但不带具体 chat ID 时，消息会被发送
    到此 home 频道。支持 thread 的平台还可以存储一个 thread/topic ID，这样
    裸的平台目标会路由到运行 /sethome 时所在的精确会话。
    """
    platform: Platform
    chat_id: str
    name: str  # 用于展示的人类可读名称
    thread_id: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        result = {
            "platform": self.platform.value,
            "chat_id": self.chat_id,
            "name": self.name,
        }
        if self.thread_id:
            result["thread_id"] = self.thread_id
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HomeChannel":
        return cls(
            platform=Platform(data["platform"]),
            chat_id=str(data["chat_id"]),
            name=data.get("name", "Home"),
            thread_id=str(data["thread_id"]) if data.get("thread_id") else None,
        )


@dataclass
class SessionResetPolicy:
    """
    控制 session 何时重置（丢失上下文）。

    模式：
    - "daily"：每天在指定小时重置
    - "idle"：在 N 分钟不活动后重置
    - "both"：两者中先触发的（每日边界 或 空闲超时）
    - "none"：从不自动重置（上下文仅由压缩管理）
    """
    mode: str = "both"  # "daily"、"idle"、"both" 或 "none"
    at_hour: int = 4  # 每日重置的小时（0-23，本地时间）
    idle_minutes: int = 1440  # 重置前的不活动分钟数（24 小时）
    notify: bool = True  # 自动重置时向用户发送通知
    notify_exclude_platforms: tuple = ("api_server", "webhook")  # 不接收重置通知的平台
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "at_hour": self.at_hour,
            "idle_minutes": self.idle_minutes,
            "notify": self.notify,
            "notify_exclude_platforms": list(self.notify_exclude_platforms),
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionResetPolicy":
        # 同时处理缺失的键和显式的 null 值（YAML null → None）
        mode = data.get("mode")
        at_hour = data.get("at_hour")
        idle_minutes = data.get("idle_minutes")
        notify = data.get("notify")
        exclude = data.get("notify_exclude_platforms")
        return cls(
            mode=mode if mode is not None else "both",
            at_hour=at_hour if at_hour is not None else 4,
            idle_minutes=idle_minutes if idle_minutes is not None else 1440,
            notify=_coerce_bool(notify, True),
            notify_exclude_platforms=tuple(exclude) if exclude is not None else ("api_server", "webhook"),
        )


@dataclass
class PlatformConfig:
    """单个消息平台的配置。"""
    enabled: bool = False
    token: Optional[str] = None  # Bot token（Telegram、Discord）
    api_key: Optional[str] = None  # 与 token 不同的 API key
    home_channel: Optional[HomeChannel] = None

    # 回复 thread 模式（Telegram/Slack）
    # - "off"：回复永不 thread 到原消息
    # - "first"：仅第一块 thread 到用户的消息（默认）
    # - "all"：多部分回复中的所有块都 thread 到用户的消息
    reply_to_mode: str = "first"

    # gateway 是否被允许在此平台上发送 "♻️ Gateway online" /
    # "♻ Gateway restarted" 生命周期通知。默认 True 保持既有行为。在面向
    # 最终用户的平台（例如 Slack）上设为 False，那里操作员风格的 restart
    # 提醒是噪声；在操作员希望接收的后台频道上保持 True。
    gateway_restart_notification: bool = True

    # 平台特定设置
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "enabled": self.enabled,
            "extra": self.extra,
            "reply_to_mode": self.reply_to_mode,
            "gateway_restart_notification": self.gateway_restart_notification,
        }
        if self.token:
            result["token"] = self.token
        if self.api_key:
            result["api_key"] = self.api_key
        if self.home_channel:
            result["home_channel"] = self.home_channel.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlatformConfig":
        home_channel = None
        if "home_channel" in data:
            home_channel = HomeChannel.from_dict(data["home_channel"])

        # gateway_restart_notification 可能通过 load_gateway_config() 中的
        # 共享键循环被桥接进 extra；同时检查顶层和 extra，以便 YAML
        # ``discord: gateway_restart_notification: false`` 无需单独的 platforms:
        # 块即可工作。
        _grn = data.get("gateway_restart_notification")
        if _grn is None:
            _grn = data.get("extra", {}).get("gateway_restart_notification")

        return cls(
            enabled=_coerce_bool(data.get("enabled"), False),
            token=data.get("token"),
            api_key=data.get("api_key"),
            home_channel=home_channel,
            reply_to_mode=data.get("reply_to_mode", "first"),
            gateway_restart_notification=_coerce_bool(_grn, True),
            extra=data.get("extra", {}),
        )


# 流式输出默认值 —— 唯一事实来源，使 StreamingConfig 和 StreamConsumerConfig
# 在开箱即用的编辑节奏上保持一致。针对 Telegram 约 1 edit/s 的 flood 包络调优：
# 略低于 1s 让节奏有呼吸空间而不撞上速率限制，较小的 buffer 阈值使 DM 中的
# 短回复近乎即时。
DEFAULT_STREAMING_EDIT_INTERVAL: float = 0.8
DEFAULT_STREAMING_BUFFER_THRESHOLD: int = 24
DEFAULT_STREAMING_CURSOR: str = " ▉"


@dataclass
class StreamingConfig:
    """面向消息平台的实时 token 流式输出配置。"""
    enabled: bool = False
    # 传输选择：
    #   "auto"  —— 在平台支持时优先使用原生 streaming-draft 更新
    #             （Telegram sendMessageDraft，Bot API 9.5+）；不支持时回退到
    #             基于编辑的方式。
    #   "draft" —— 显式请求原生 draft；平台/聊天不支持时回退到编辑。
    #   "edit"  —— 仅渐进式 editMessageText（旧行为）。
    #   "off"   —— 完全禁用流式输出。
    #
    # 默认为 "auto"：在支持原生 draft 流式输出的平台（通过 sendMessageDraft
    # 的 Telegram DM，Bot API 9.5+）上优先使用，其他地方回退到基于编辑的流式
    # 输出。作为全局默认值是安全的，因为不支持 draft 的适配器（Discord、Slack、
    # Matrix、…）报告 supports_draft_streaming() == False 并透明地使用编辑路径
    # —— 所以 "auto" 永远不会让非 Telegram 平台退化，只会升级能渲染更平滑
    # 原生预览的聊天。
    transport: str = "auto"
    edit_interval: float = DEFAULT_STREAMING_EDIT_INTERVAL
    buffer_threshold: int = DEFAULT_STREAMING_BUFFER_THRESHOLD
    cursor: str = DEFAULT_STREAMING_CURSOR
    # 移植自 openclaw/openclaw#72038。当 >0 时，如果一个长时间运行的流式
    # 响应的原预览已可见至少这么多秒，则其最终编辑作为一条新消息投递，以便
    # 平台的可见时间戳反映完成时间而非预览创建时间。目前仅应用于 Telegram
    # （其他平台忽略该设置）。默认 0 禁用新消息替换路径；设为 >0 以启用。
    fresh_final_after_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "transport": self.transport,
            "edit_interval": self.edit_interval,
            "buffer_threshold": self.buffer_threshold,
            "cursor": self.cursor,
            "fresh_final_after_seconds": self.fresh_final_after_seconds,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StreamingConfig":
        if not data:
            return cls()
        return cls(
            enabled=_coerce_bool(data.get("enabled"), False),
            transport=data.get("transport", "auto"),
            edit_interval=_coerce_float(
                data.get("edit_interval"), DEFAULT_STREAMING_EDIT_INTERVAL,
            ),
            buffer_threshold=_coerce_int(
                data.get("buffer_threshold"), DEFAULT_STREAMING_BUFFER_THRESHOLD,
            ),
            cursor=data.get("cursor", DEFAULT_STREAMING_CURSOR),
            fresh_final_after_seconds=_coerce_float(
                data.get("fresh_final_after_seconds"), 0.0
            ),
        )


# -----------------------------------------------------------------------------
# 内置的平台连接检查器
# -----------------------------------------------------------------------------
# 每个可调用对象接收一个 ``PlatformConfig``，当平台配置得足以被视为"已连接"时
# 返回 ``True``。依赖通用 ``token or api_key`` 检查的平台（Telegram、Discord、
# Slack、Matrix、Mattermost、HomeAssistant）不需要在此处有条目。
_PLATFORM_CONNECTED_CHECKERS: dict[Platform, Callable[[PlatformConfig], bool]] = {
    Platform.WEIXIN: lambda cfg: bool(
        cfg.extra.get("account_id") and (cfg.token or cfg.extra.get("token"))
    ),
    Platform.WHATSAPP_CLOUD: lambda cfg: bool(
        cfg.extra.get("phone_number_id") and cfg.extra.get("access_token")
    ),
    Platform.SIGNAL: lambda cfg: bool(cfg.extra.get("http_url")),
    Platform.API_SERVER: lambda cfg: True,
    Platform.WEBHOOK: lambda cfg: True,
    Platform.MSGRAPH_WEBHOOK: lambda cfg: bool(
        str(cfg.extra.get("client_state") or "").strip()
    ),
    Platform.BLUEBUBBLES: lambda cfg: bool(
        cfg.extra.get("server_url") and cfg.extra.get("password")
    ),
    Platform.QQBOT: lambda cfg: bool(
        cfg.extra.get("app_id") and cfg.extra.get("client_secret")
    ),
    Platform.YUANBAO: lambda cfg: bool(
        cfg.extra.get("app_id") and cfg.extra.get("app_secret")
    ),
    # Relay 主动拨出连接到 connector；一旦配置了端点 URL
    # （extra["relay_url"] 或 extra["url"]）就视为"已连接"。能力描述符在握手时
    # 协商，因此 URL 是实验阶段唯一的配置级信号。实验性 —— 可能变更。
    Platform.RELAY: lambda cfg: bool(
        cfg.extra.get("relay_url") or cfg.extra.get("url")
    ),
}


@dataclass
class GatewayConfig:
    """
    主 gateway 配置。

    管理所有平台连接、session 策略和投递设置。
    """
    # 平台配置
    platforms: Dict[Platform, PlatformConfig] = field(default_factory=dict)

    # 按类型的 session 重置策略
    default_reset_policy: SessionResetPolicy = field(default_factory=SessionResetPolicy)
    reset_by_type: Dict[str, SessionResetPolicy] = field(default_factory=dict)
    reset_by_platform: Dict[Platform, SessionResetPolicy] = field(default_factory=dict)

    # 重置触发命令
    reset_triggers: List[str] = field(default_factory=lambda: ["/new", "/reset"])

    # 用户自定义快捷命令（绕过 agent 循环的 slash 命令）
    quick_commands: Dict[str, Any] = field(default_factory=dict)

    # 存储路径
    sessions_dir: Path = field(default_factory=lambda: get_hermes_home() / "sessions")

    # 投递设置
    always_log_local: bool = True  # 总是把 cron 输出保存到本地文件
    # 在发送前丢弃出站的"沉默旁白"消息（例如 *(silent)*、🔇、一个裸的
    # "."）。这些是 persona 无话可说时模型产生的幻觉；在 bot-to-bot 频道里
    # 它们会来回镜像，烧掉 token 并让模型崩溃。这是一个跨 provider、能在
    # SOUL.md/prompt 漂移下存活的底层守卫。设为 False 可选择原始透传。
    filter_silence_narration: bool = True

    # STT 设置
    stt_enabled: bool = True  # 是否自动转写入站的语音消息

    # 共享聊天中的 session 隔离
    group_sessions_per_user: bool = True  # 当 user ID 可用时，按参与者隔离 group/channel 的 session
    thread_sessions_per_user: bool = False  # 为 False（默认）时，thread 在所有参与者间共享
    max_concurrent_sessions: Optional[int] = None  # 正整数限制同时活跃的聊天 session 数

    # 多 profile 多路复用（可选；默认关闭，保持一 gateway 一 profile）。
    # 为 True 时，默认 profile 的 gateway 为主机上的每个 profile 服务入站消息：
    # profile 被盖戳进 session 键，并（在后续阶段）解析按 profile 的适配器/凭证。
    # 为 False 时，gateway 行为与之前完全一致 —— 单一 HERMES_HOME，不做
    # profile 盖戳。
    multiplex_profiles: bool = False

    # 未授权 DM 策略
    unauthorized_dm_behavior: str = "pair"  # "pair" 或 "ignore"

    # 流式输出配置
    streaming: StreamingConfig = field(default_factory=StreamingConfig)

    # session 存储修剪：从内存字典和 sessions.json 中丢弃早于这么多天的
    # SessionEntry 记录。防止存储在服务众多聊天/thread/user 数月的 gateway 中
    # 无限增长。修剪对用户不可见 —— 如果他们 resume，会得到一个全新的
    # session，就像重置策略触发了一样。0 = 禁用。
    session_store_max_age_days: int = 90

    def get_connected_platforms(self) -> List[Platform]:
        """返回已启用且已配置的平台列表。"""
        connected = []
        for platform, config in self.platforms.items():
            if not config.enabled:
                continue
            if self._is_platform_connected(platform, config):
                connected.append(platform)
        return connected

    def _is_platform_connected(self, platform: Platform, config: PlatformConfig) -> bool:
        """检查单个平台是否配置充分。"""
        # Weixin 需要 token 和 account_id 两者（先检查，以免通用 token 分支
        # 在没有 account_id 的情况下放行）。
        if platform == Platform.WEIXIN:
            return bool(
                config.extra.get("account_id")
                and (config.token or config.extra.get("token"))
            )

        # 通用 token/api_key 认证覆盖 Telegram、Discord、Slack 等。
        if config.token or config.api_key:
            return True

        # 平台特定检查
        checker = _PLATFORM_CONNECTED_CHECKERS.get(platform)
        if checker is not None:
            return checker(config)

        # 插件注册的平台。先强制插件发现，以便即使在直接构造
        # GatewayConfig 时（例如测试中，或绕过 load_gateway_config() 的调用方，
        # 后者正是正常路径中触发发现的方式）也能工作。discover_plugins()
        # 是幂等的。
        try:
            from gateway.platform_registry import platform_registry
            try:
                from hermes_cli.plugins import discover_plugins
                discover_plugins()
            except Exception:
                pass
            entry = platform_registry.get(platform.value)
            if entry:
                if entry.is_connected is not None:
                    return entry.is_connected(config)
                if entry.validate_config is not None:
                    return entry.validate_config(config)
                return True
        except Exception:
            pass  # 早期导入期间注册表尚未初始化

        return False

    def get_home_channel(self, platform: Platform) -> Optional[HomeChannel]:
        """获取平台的 home 频道。"""
        config = self.platforms.get(platform)
        if config:
            return config.home_channel
        return None

    def get_reset_policy(
        self,
        platform: Optional[Platform] = None,
        session_type: Optional[str] = None
    ) -> SessionResetPolicy:
        """
        获取 session 的合适重置策略。

        优先级：平台覆盖 > 类型覆盖 > 默认
        """
        # 平台特定覆盖优先
        if platform and platform in self.reset_by_platform:
            return self.reset_by_platform[platform]

        # 类型特定覆盖（dm、group、thread）
        if session_type and session_type in self.reset_by_type:
            return self.reset_by_type[session_type]

        return self.default_reset_policy
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "platforms": {
                p.value: c.to_dict() for p, c in self.platforms.items()
            },
            "default_reset_policy": self.default_reset_policy.to_dict(),
            "reset_by_type": {
                k: v.to_dict() for k, v in self.reset_by_type.items()
            },
            "reset_by_platform": {
                p.value: v.to_dict() for p, v in self.reset_by_platform.items()
            },
            "reset_triggers": self.reset_triggers,
            "quick_commands": self.quick_commands,
            "sessions_dir": str(self.sessions_dir),
            "always_log_local": self.always_log_local,
            "filter_silence_narration": self.filter_silence_narration,
            "stt_enabled": self.stt_enabled,
            "group_sessions_per_user": self.group_sessions_per_user,
            "thread_sessions_per_user": self.thread_sessions_per_user,
            "max_concurrent_sessions": self.max_concurrent_sessions,
            "multiplex_profiles": self.multiplex_profiles,
            "unauthorized_dm_behavior": self.unauthorized_dm_behavior,
            "streaming": self.streaming.to_dict(),
            "session_store_max_age_days": self.session_store_max_age_days,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GatewayConfig":
        platforms = {}
        for platform_name, platform_data in data.get("platforms", {}).items():
            try:
                platform = Platform(platform_name)
                platforms[platform] = PlatformConfig.from_dict(platform_data)
            except ValueError:
                pass  # 跳过未知平台
        
        reset_by_type = {}
        for type_name, policy_data in data.get("reset_by_type", {}).items():
            reset_by_type[type_name] = SessionResetPolicy.from_dict(policy_data)
        
        reset_by_platform = {}
        for platform_name, policy_data in data.get("reset_by_platform", {}).items():
            try:
                platform = Platform(platform_name)
                reset_by_platform[platform] = SessionResetPolicy.from_dict(policy_data)
            except ValueError:
                pass
        
        default_policy = SessionResetPolicy()
        if "default_reset_policy" in data:
            default_policy = SessionResetPolicy.from_dict(data["default_reset_policy"])
        
        sessions_dir = get_hermes_home() / "sessions"
        if "sessions_dir" in data:
            sessions_dir = Path(data["sessions_dir"])
        
        quick_commands = data.get("quick_commands", {})
        if not isinstance(quick_commands, dict):
            quick_commands = {}

        stt_enabled = data.get("stt_enabled")
        if stt_enabled is None:
            stt_enabled = data.get("stt", {}).get("enabled") if isinstance(data.get("stt"), dict) else None

        group_sessions_per_user = data.get("group_sessions_per_user")
        thread_sessions_per_user = data.get("thread_sessions_per_user")
        multiplex_profiles = data.get("multiplex_profiles")
        nested_gateway = data.get("gateway") if isinstance(data.get("gateway"), dict) else {}
        if multiplex_profiles is None and isinstance(nested_gateway, dict):
            # 也尊重由 ``hermes config set gateway.multiplex_profiles true``
            # 写入的 gateway.multiplex_profiles。
            multiplex_profiles = nested_gateway.get("multiplex_profiles")
        if "max_concurrent_sessions" in data:
            max_concurrent_raw = data.get("max_concurrent_sessions")
            max_concurrent_key = "max_concurrent_sessions"
        else:
            max_concurrent_raw = nested_gateway.get("max_concurrent_sessions")
            max_concurrent_key = "gateway.max_concurrent_sessions"
        max_concurrent_sessions = _coerce_optional_positive_int(
            max_concurrent_raw,
            max_concurrent_key,
        )
        unauthorized_dm_behavior = _normalize_unauthorized_dm_behavior(
            data.get("unauthorized_dm_behavior"),
            "pair",
        )

        try:
            session_store_max_age_days = int(data.get("session_store_max_age_days", 90))
            session_store_max_age_days = max(session_store_max_age_days, 0)
        except (TypeError, ValueError):
            session_store_max_age_days = 90

        return cls(
            platforms=platforms,
            default_reset_policy=default_policy,
            reset_by_type=reset_by_type,
            reset_by_platform=reset_by_platform,
            reset_triggers=data.get("reset_triggers", ["/new", "/reset"]),
            quick_commands=quick_commands,
            sessions_dir=sessions_dir,
            always_log_local=_coerce_bool(data.get("always_log_local"), True),
            filter_silence_narration=_coerce_bool(
                data.get("filter_silence_narration"), True
            ),
            stt_enabled=_coerce_bool(stt_enabled, True),
            group_sessions_per_user=_coerce_bool(group_sessions_per_user, True),
            thread_sessions_per_user=_coerce_bool(thread_sessions_per_user, False),
            multiplex_profiles=_coerce_bool(multiplex_profiles, False),
            max_concurrent_sessions=max_concurrent_sessions,
            unauthorized_dm_behavior=unauthorized_dm_behavior,
            streaming=StreamingConfig.from_dict(data.get("streaming", {})),
            session_store_max_age_days=session_store_max_age_days,
        )

    def get_unauthorized_dm_behavior(self, platform: Optional[Platform] = None) -> str:
        """返回平台的有效未授权 DM 行为。

        Email 是收件箱形态，不是聊天形态，因此默认为 ``"ignore"``，除非
        ``platforms.email.unauthorized_dm_behavior`` 显式选择配对。全局默认值
        不会让 email 进入配对。
        """
        if platform:
            platform_cfg = self.platforms.get(platform)
            if platform_cfg and "unauthorized_dm_behavior" in platform_cfg.extra:
                return _normalize_unauthorized_dm_behavior(
                    platform_cfg.extra.get("unauthorized_dm_behavior"),
                    self.unauthorized_dm_behavior,
                )
            if platform == Platform.EMAIL:
                return "ignore"
        return self.unauthorized_dm_behavior

    def get_notice_delivery(self, platform: Optional[Platform] = None) -> str:
        """返回平台的有效通知投递模式。"""
        if platform:
            platform_cfg = self.platforms.get(platform)
            if platform_cfg and "notice_delivery" in platform_cfg.extra:
                return _normalize_notice_delivery(
                    platform_cfg.extra.get("notice_delivery"),
                    "public",
                )
        return "public"


def load_gateway_config() -> GatewayConfig:
    """
    从多个来源加载 gateway 配置。

    优先级（从高到低）：
    1. 环境变量
    2. ~/.hermes/config.yaml（主要的面向用户配置）
    3. ~/.hermes/gateway.json（遗留 —— 在 config.yaml 之下提供默认值）
    4. 内置默认值
    """
    _home = get_hermes_home()
    gw_data: dict = {}

    # 遗留回退：gateway.json 提供基础层。当两者指定相同设置时，config.yaml
    # 的键总是胜出。
    gateway_json_path = _home / "gateway.json"
    if gateway_json_path.exists():
        try:
            with open(gateway_json_path, "r", encoding="utf-8") as f:
                gw_data = json.load(f) or {}
            logger.info(
                "Loaded legacy %s — consider moving settings to config.yaml",
                gateway_json_path,
            )
        except Exception as e:
            logger.warning("Failed to load %s: %s", gateway_json_path, e)

    # 主要来源：config.yaml
    try:
        import yaml
        config_yaml_path = _home / "config.yaml"
        if config_yaml_path.exists():
            with open(config_yaml_path, encoding="utf-8") as f:
                yaml_cfg = yaml.safe_load(f) or {}

            # 受管范围：覆盖管理员钉住的值，以便 gateway 也遵守它们。此加载器
            # 构建自己的字典，而不是通过 hermes_cli.config.load_config，因此
            # 没有这一步，受管的 session_reset / quick_commands / stt / model
            # 会被消息 gateway 忽略。通过共享辅助函数实现 fail-open。
            from hermes_cli import managed_scope
            yaml_cfg = managed_scope.apply_managed_overlay(yaml_cfg)

            # 把 config.yaml 的键映射到 GatewayConfig.from_dict() 的 schema。
            # 每个键都会覆盖 gateway.json 可能已设置的值。
            sr = yaml_cfg.get("session_reset")
            if sr and isinstance(sr, dict):
                gw_data["default_reset_policy"] = sr

            qc = yaml_cfg.get("quick_commands")
            if qc is not None:
                if isinstance(qc, dict):
                    gw_data["quick_commands"] = qc
                else:
                    logger.warning(
                        "Ignoring invalid quick_commands in config.yaml "
                        "(expected mapping, got %s)",
                        type(qc).__name__,
                    )

            stt_cfg = yaml_cfg.get("stt")
            if isinstance(stt_cfg, dict):
                gw_data["stt"] = stt_cfg

            if "group_sessions_per_user" in yaml_cfg:
                gw_data["group_sessions_per_user"] = yaml_cfg["group_sessions_per_user"]

            if "thread_sessions_per_user" in yaml_cfg:
                gw_data["thread_sessions_per_user"] = yaml_cfg["thread_sessions_per_user"]

            # 多路复用标志：同时接受顶层键和嵌套的
            # gateway.multiplex_profiles 形式（from_dict 解析嵌套回退，但在此处
            # 暴露顶层键，以与上面的其他 session 范围标志保持对等）。
            if "multiplex_profiles" in yaml_cfg:
                gw_data["multiplex_profiles"] = yaml_cfg["multiplex_profiles"]

            gateway_section = yaml_cfg.get("gateway")
            if isinstance(gateway_section, dict) and "max_concurrent_sessions" in gateway_section:
                gw_data["max_concurrent_sessions"] = gateway_section["max_concurrent_sessions"]

            if "max_concurrent_sessions" in yaml_cfg:
                gw_data["max_concurrent_sessions"] = yaml_cfg["max_concurrent_sessions"]

            streaming_cfg = yaml_cfg.get("streaming")
            if not isinstance(streaming_cfg, dict):
                # 回退到由 ``hermes config set gateway.streaming.*`` 写入的
                # 嵌套 gateway.streaming
                streaming_cfg = yaml_cfg.get("gateway", {}).get("streaming")
            if isinstance(streaming_cfg, dict):
                gw_data["streaming"] = streaming_cfg

            if "reset_triggers" in yaml_cfg:
                gw_data["reset_triggers"] = yaml_cfg["reset_triggers"]

            if "always_log_local" in yaml_cfg:
                gw_data["always_log_local"] = yaml_cfg["always_log_local"]

            if "filter_silence_narration" in yaml_cfg:
                gw_data["filter_silence_narration"] = yaml_cfg[
                    "filter_silence_narration"
                ]

            if "unauthorized_dm_behavior" in yaml_cfg:
                gw_data["unauthorized_dm_behavior"] = _normalize_unauthorized_dm_behavior(
                    yaml_cfg.get("unauthorized_dm_behavior"),
                    "pair",
                )

            # 把平台配置合并到 gw_data 中，以便 ``gateway.platforms`` 之下的
            # 仅运行时设置以与顶层 ``platforms`` 相同的方式加载。先合并嵌套的，
            # 使顶层配置保持优先级，与现有的 gateway.streaming 回退一致。
            gateway_cfg = yaml_cfg.get("gateway")
            gateway_platforms = gateway_cfg.get("platforms") if isinstance(gateway_cfg, dict) else None
            platforms_data = gw_data.setdefault("platforms", {})
            if not isinstance(platforms_data, dict):
                platforms_data = {}
                gw_data["platforms"] = platforms_data

            def _merge_platform_map(source_platforms: Any) -> None:
                if not isinstance(source_platforms, dict):
                    return
                for plat_name, plat_block in source_platforms.items():
                    if not isinstance(plat_block, dict):
                        continue
                    existing = platforms_data.get(plat_name, {})
                    if not isinstance(existing, dict):
                        existing = {}
                    # 深度合并 extra 字典，使 gateway.json 的默认值得以保留
                    merged_extra = {**existing.get("extra", {}), **plat_block.get("extra", {})}
                    if "enabled" in plat_block:
                        merged_extra["_enabled_explicit"] = True
                    merged = {**existing, **plat_block}
                    if merged_extra:
                        merged["extra"] = merged_extra
                    platforms_data[plat_name] = merged

            _merge_platform_map(gateway_platforms)
            _merge_platform_map(yaml_cfg.get("platforms"))
            if platforms_data:
                gw_data["platforms"] = platforms_data
            # 遍历内置平台加上任何已注册的插件平台，以便插件作者获得相同的
            # 共享键桥接（#24836）。
            try:
                from hermes_cli.plugins import discover_plugins
                discover_plugins()  # 幂等
                from gateway.platform_registry import platform_registry as _pr
            except Exception as e:
                logger.debug("plugin discovery skipped: %s", e)
                _pr = None

            _shared_loop_targets: list = list(Platform)
            if _pr is not None:
                for _entry in _pr.plugin_entries():
                    try:
                        _plat = Platform(_entry.name)
                    except (ValueError, KeyError):
                        continue
                    if _plat not in _shared_loop_targets:
                        _shared_loop_targets.append(_plat)

            for plat in _shared_loop_targets:
                if plat == Platform.LOCAL:
                    continue
                platform_cfg = yaml_cfg.get(plat.value)
                _cfg_toplevel = isinstance(platform_cfg, dict)
                # 回退到平台在 ``platforms`` / ``gateway.platforms`` 之下的块，
                # 以便当用户仅在这些嵌套路径下配置了平台、而没有顶层块时，共享
                # 键桥接（allow_from、require_mention、free_response_channels、…）
                # 仍会运行。镜像下面已应用到 apply_yaml_config_fn 分发的相同
                # 回退（#44f3e51）。
                # 注意：``enabled`` 仅从顶层块（``_cfg_toplevel``）写入到
                # plat_data；对于仅嵌套配置，``_merge_platform_map`` 已经以正确
                # 的优先级合并了它，因此在这里重新应用会覆盖它。
                if not _cfg_toplevel:
                    for _src in (gateway_platforms, yaml_cfg.get("platforms")):
                        if isinstance(_src, dict):
                            _candidate = _src.get(plat.value)
                            if isinstance(_candidate, dict):
                                platform_cfg = _candidate
                                break
                if not isinstance(platform_cfg, dict):
                    continue
                # 从该平台区块收集可桥接的键
                bridged = {}
                if "unauthorized_dm_behavior" in platform_cfg:
                    bridged["unauthorized_dm_behavior"] = _normalize_unauthorized_dm_behavior(
                        platform_cfg.get("unauthorized_dm_behavior"),
                        gw_data.get("unauthorized_dm_behavior", "pair"),
                    )
                if "notice_delivery" in platform_cfg:
                    bridged["notice_delivery"] = _normalize_notice_delivery(
                        platform_cfg.get("notice_delivery"),
                        "public",
                    )
                if "reply_prefix" in platform_cfg:
                    bridged["reply_prefix"] = platform_cfg["reply_prefix"]
                if "reply_in_thread" in platform_cfg:
                    bridged["reply_in_thread"] = platform_cfg["reply_in_thread"]
                if "require_mention" in platform_cfg:
                    bridged["require_mention"] = platform_cfg["require_mention"]
                if plat == Platform.TELEGRAM and "allowed_chats" in platform_cfg:
                    bridged["allowed_chats"] = platform_cfg["allowed_chats"]
                if plat == Platform.TELEGRAM and "group_allowed_chats" in platform_cfg:
                    bridged["group_allowed_chats"] = platform_cfg["group_allowed_chats"]
                if plat == Platform.TELEGRAM and "allowed_topics" in platform_cfg:
                    bridged["allowed_topics"] = platform_cfg["allowed_topics"]
                if "free_response_channels" in platform_cfg:
                    bridged["free_response_channels"] = platform_cfg["free_response_channels"]
                if "mention_patterns" in platform_cfg:
                    bridged["mention_patterns"] = platform_cfg["mention_patterns"]
                if "exclusive_bot_mentions" in platform_cfg:
                    bridged["exclusive_bot_mentions"] = platform_cfg["exclusive_bot_mentions"]
                if plat == Platform.TELEGRAM and "observe_unmentioned_group_messages" in platform_cfg:
                    bridged["observe_unmentioned_group_messages"] = platform_cfg["observe_unmentioned_group_messages"]
                if "dm_policy" in platform_cfg:
                    bridged["dm_policy"] = platform_cfg["dm_policy"]
                if "allow_from" in platform_cfg:
                    bridged["allow_from"] = platform_cfg["allow_from"]
                if "allow_admin_from" in platform_cfg:
                    bridged["allow_admin_from"] = platform_cfg["allow_admin_from"]
                if "user_allowed_commands" in platform_cfg:
                    bridged["user_allowed_commands"] = platform_cfg["user_allowed_commands"]
                if "group_policy" in platform_cfg:
                    bridged["group_policy"] = platform_cfg["group_policy"]
                if "group_allow_from" in platform_cfg:
                    bridged["group_allow_from"] = platform_cfg["group_allow_from"]
                if "group_allow_admin_from" in platform_cfg:
                    bridged["group_allow_admin_from"] = platform_cfg["group_allow_admin_from"]
                if "group_user_allowed_commands" in platform_cfg:
                    bridged["group_user_allowed_commands"] = platform_cfg["group_user_allowed_commands"]
                if plat in {Platform.DISCORD, Platform.SLACK} and "channel_skill_bindings" in platform_cfg:
                    bridged["channel_skill_bindings"] = platform_cfg["channel_skill_bindings"]
                if "channel_prompts" in platform_cfg:
                    channel_prompts = platform_cfg["channel_prompts"]
                    if isinstance(channel_prompts, dict):
                        bridged["channel_prompts"] = {str(k): v for k, v in channel_prompts.items()}
                    else:
                        bridged["channel_prompts"] = channel_prompts
                if "gateway_restart_notification" in platform_cfg:
                    bridged["gateway_restart_notification"] = platform_cfg["gateway_restart_notification"]
                enabled_was_explicit = _cfg_toplevel and "enabled" in platform_cfg
                if not bridged and not enabled_was_explicit:
                    continue
                plat_data, extra = _ensure_platform_extra_dict(platforms_data, plat.value)
                if enabled_was_explicit:
                    plat_data["enabled"] = platform_cfg["enabled"]
                    # 标记显式的 enable/disable，以便 _apply_env_overrides 中
                    # 由注册表驱动的插件启用遍历能尊重已迁移插件平台
                    # （slack、telegram、matrix、dingtalk、whatsapp、feishu …）
                    # 的显式 ``enabled: false``，而不是在检测到 token/SDK 时
                    # 重新启用它们。#41112。
                    extra["_enabled_explicit"] = True
                extra.update(bridged)

            # 插件拥有的 YAML→env 配置桥接（#24836）。参见
            # ``PlatformEntry.apply_yaml_config_fn`` 的钩子契约。
            # 顺序：共享键循环（上面）→ 此分发 → 旧版硬编码块
            # （下面；当钩子已设置它们的 env 变量时为 no-op）→
            # ``GatewayConfig.from_dict`` 之后的 ``_apply_env_overrides()``。
            if _pr is not None:
                for entry in _pr.all_entries():
                    if entry.apply_yaml_config_fn is None:
                        continue
                    platform_cfg = yaml_cfg.get(entry.name)
                    # 回退到平台在 ``platforms`` / ``gateway.platforms`` 之下的
                    # 块，以便当用户仅在这些嵌套路径下配置了平台（例如
                    # ``platforms.discord.extra.allow_from``）、而没有顶层
                    # ``discord:`` 块时，适配器钩子仍会运行。
                    if not isinstance(platform_cfg, dict):
                        for _src in (gateway_platforms, yaml_cfg.get("platforms")):
                            if isinstance(_src, dict):
                                _candidate = _src.get(entry.name)
                                if isinstance(_candidate, dict):
                                    platform_cfg = _candidate
                                    break
                    if not isinstance(platform_cfg, dict):
                        continue
                    try:
                        seeded = entry.apply_yaml_config_fn(yaml_cfg, platform_cfg)
                    except Exception as e:
                        logger.debug(
                            "apply_yaml_config_fn for %s raised: %s",
                            entry.name, e,
                        )
                        continue
                    if not isinstance(seeded, dict) or not seeded:
                        continue
                    _, extra = _ensure_platform_extra_dict(platforms_data, entry.name)
                    extra.update(seeded)

            # Slack 设置 → env 变量：已迁移到 slack 插件的
            # ``apply_yaml_config_fn`` 钩子（见 plugins/platforms/slack/
            # adapter.py::_apply_yaml_config），在上面 ``apply_yaml_config_fn``
            # 循环中分发。#41112 / #3823。

            # 当 telegram: 区块尚未提供 require_mention 时，把顶层
            # require_mention 桥接到 Telegram。用户经常把
            # "require_mention: true" 写在顶层、紧挨 group_sessions_per_user，
            # 期望它以相同方式工作（#3979）。
            _tl_require_mention = yaml_cfg.get("require_mention")
            if _tl_require_mention is not None:
                _tg_section = yaml_cfg.get("telegram") or {}
                if "require_mention" not in _tg_section:
                    _tg_plat = platforms_data.setdefault(Platform.TELEGRAM.value, {})
                    _tg_extra = _tg_plat.setdefault("extra", {})
                    _tg_extra.setdefault("require_mention", _tl_require_mention)
                    # 同时桥接到适配器在运行时读取的 TELEGRAM_REQUIRE_MENTION
                    # env 变量。它过去位于 core 的 telegram_cfg 区块中；它留在
                    # core 是因为它依据的是顶层 require_mention（而不是
                    # telegram: 区块），所以 telegram 插件的 apply_yaml_config_fn
                    # 钩子 —— 只在存在 telegram 配置块时运行 —— 无法覆盖无
                    # telegram 区块的情况（#3979）。
                    if not os.getenv("TELEGRAM_REQUIRE_MENTION"):
                        os.environ["TELEGRAM_REQUIRE_MENTION"] = str(_tl_require_mention).lower()

            # Telegram 设置 → env 变量 / extra：已迁移到 telegram 插件的
            # apply_yaml_config_fn 钩子（plugins/platforms/telegram/adapter.py）。
            # #41112 / #3823。

            # WhatsApp 设置 → env 变量：已迁移到 whatsapp 插件的
            # apply_yaml_config_fn 钩子（plugins/platforms/whatsapp/adapter.py）。
            # #41112 / #3823。

            # Signal 设置 → env 变量（env 变量优先）
            signal_cfg = yaml_cfg.get("signal", {})
            if isinstance(signal_cfg, dict):
                if "require_mention" in signal_cfg and not os.getenv("SIGNAL_REQUIRE_MENTION"):
                    os.environ["SIGNAL_REQUIRE_MENTION"] = str(signal_cfg["require_mention"]).lower()

            # DingTalk 设置 → env 变量：已迁移到 dingtalk 插件的
            # apply_yaml_config_fn 钩子（plugins/platforms/dingtalk/adapter.py）。
            # #41112 / #3823。

            # Mattermost 配置桥接已移入 plugins/platforms/mattermost/
            # adapter.py::_apply_yaml_config —— 见 #25443（apply_yaml_config_fn）。

            # Matrix 设置 → env 变量：已迁移到 matrix 插件的
            # apply_yaml_config_fn 钩子（plugins/platforms/matrix/adapter.py）。
            # #41112 / #3823。

            # Feishu 设置 → env 变量：已迁移到 feishu 插件的
            # apply_yaml_config_fn 钩子（plugins/platforms/feishu/adapter.py）。
            # #41112 / #3823。

    except Exception as e:
        logger.warning(
            "Failed to process config.yaml — falling back to .env / gateway.json values. "
            "Check %s for syntax errors. Error: %s",
            _home / "config.yaml",
            e,
        )

    config = GatewayConfig.from_dict(gw_data)

    # 用环境变量覆盖
    _apply_env_overrides(config)

    # --- 校验加载的值 ---
    _validate_gateway_config(config)

    return config


def _validate_gateway_config(config: "GatewayConfig") -> None:
    """就地校验并净化已加载的 GatewayConfig。

    在所有配置源合并后由 ``load_gateway_config()`` 调用。抽取为独立函数以便
    测试。
    """
    policy = config.default_reset_policy

    if not (0 <= policy.at_hour <= 23):
        logger.warning(
            "Invalid at_hour=%s (must be 0-23). Using default 4.", policy.at_hour
        )
        policy.at_hour = 4

    if policy.idle_minutes is None or policy.idle_minutes <= 0:
        logger.warning(
            "Invalid idle_minutes=%s (must be positive). Using default 1440.",
            policy.idle_minutes,
        )
        policy.idle_minutes = 1440

    # 警告空的 bot token —— 加载了空字符串的平台无法连接，如果没有日志行，
    # 原因会让人困惑。
    _token_env_names = {
        Platform.TELEGRAM: "TELEGRAM_BOT_TOKEN",
        Platform.DISCORD: "DISCORD_BOT_TOKEN",
        Platform.SLACK: "SLACK_BOT_TOKEN",
        Platform.MATTERMOST: "MATTERMOST_TOKEN",
        Platform.MATRIX: "MATRIX_ACCESS_TOKEN",
        Platform.WEIXIN: "WEIXIN_TOKEN",
    }
    for platform, pconfig in config.platforms.items():
        if not pconfig.enabled:
            continue
        env_name = _token_env_names.get(platform)
        if env_name and pconfig.token is not None and not pconfig.token.strip():
            logger.warning(
                "%s is enabled but %s is empty. "
                "The adapter will likely fail to connect.",
                platform.value, env_name,
            )

    # 拒绝已知弱占位 token。
    # 移植自 openclaw/openclaw#64586：复制 .env.example 而不修改占位值的用户
    # 会得到一个清晰的启动错误，而不是来自平台 API 的令人困惑的 "auth failed"。
    try:
        from hermes_cli.auth import has_usable_secret
    except ImportError:
        has_usable_secret = None  # type: ignore[assignment]

    if has_usable_secret is not None:
        for platform, pconfig in config.platforms.items():
            if not pconfig.enabled:
                continue
            env_name = _token_env_names.get(platform)
            if not env_name:
                continue
            token = pconfig.token
            if token and token.strip() and not has_usable_secret(token, min_length=4):
                logger.error(
                    "%s is enabled but %s is set to a placeholder value ('%s'). "
                    "Set a real bot token before starting the gateway. "
                    "The adapter will NOT be started.",
                    platform.value, env_name, token.strip()[:6] + "...",
                )
                pconfig.enabled = False


def _apply_env_overrides(config: GatewayConfig) -> None:
    """把环境变量覆盖应用到配置上。"""

    def _enable_from_env(platform: Platform) -> PlatformConfig:
        if platform not in config.platforms:
            config.platforms[platform] = PlatformConfig(enabled=True)
            return config.platforms[platform]

        platform_config = config.platforms[platform]
        # 读取（不要 pop）显式启用标记：本函数稍后由注册表驱动的插件启用
        # 遍历也需要它，以避免重新启用用户显式禁用的平台（已迁移的插件平台
        # —— telegram、matrix —— 也流经此处，#41112）。该标志在
        # _apply_env_overrides 末尾的最终清理中一次性清除。
        enabled_was_explicit = bool(platform_config.extra.get("_enabled_explicit", False))
        if not platform_config.enabled and not enabled_was_explicit:
            platform_config.enabled = True
        return platform_config

    # Telegram
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if telegram_token:
        telegram_config = _enable_from_env(Platform.TELEGRAM)
        telegram_config.token = telegram_token

    # Telegram 的回复 thread 模式（off/first/all）
    telegram_reply_mode = os.getenv("TELEGRAM_REPLY_TO_MODE", "").lower()
    if telegram_reply_mode in {"off", "first", "all"}:
        if Platform.TELEGRAM not in config.platforms:
            config.platforms[Platform.TELEGRAM] = PlatformConfig()
        config.platforms[Platform.TELEGRAM].reply_to_mode = telegram_reply_mode
    
    telegram_fallback_ips = os.getenv("TELEGRAM_FALLBACK_IPS", "")
    if telegram_fallback_ips:
        if Platform.TELEGRAM not in config.platforms:
            config.platforms[Platform.TELEGRAM] = PlatformConfig()
        config.platforms[Platform.TELEGRAM].extra["fallback_ips"] = [
            ip.strip() for ip in telegram_fallback_ips.split(",") if ip.strip()
        ]

    telegram_home = os.getenv("TELEGRAM_HOME_CHANNEL")
    if telegram_home and Platform.TELEGRAM in config.platforms:
        config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
            platform=Platform.TELEGRAM,
            chat_id=telegram_home,
            name=os.getenv("TELEGRAM_HOME_CHANNEL_NAME", "Home"),
            thread_id=os.getenv("TELEGRAM_HOME_CHANNEL_THREAD_ID") or None,
        )
    
    # Discord
    discord_token = os.getenv("DISCORD_BOT_TOKEN")
    if discord_token:
        discord_config = _enable_from_env(Platform.DISCORD)
        discord_config.token = discord_token
    
    discord_home = os.getenv("DISCORD_HOME_CHANNEL")
    if discord_home and Platform.DISCORD in config.platforms:
        config.platforms[Platform.DISCORD].home_channel = HomeChannel(
            platform=Platform.DISCORD,
            chat_id=discord_home,
            name=os.getenv("DISCORD_HOME_CHANNEL_NAME", "Home"),
            thread_id=os.getenv("DISCORD_HOME_CHANNEL_THREAD_ID") or None,
        )
    
    # Discord 的回复 thread 模式（off/first/all）
    discord_reply_mode = os.getenv("DISCORD_REPLY_TO_MODE", "").lower()
    if discord_reply_mode in {"off", "first", "all"}:
        if Platform.DISCORD not in config.platforms:
            config.platforms[Platform.DISCORD] = PlatformConfig()
        config.platforms[Platform.DISCORD].reply_to_mode = discord_reply_mode
    
    # WhatsApp（通常使用不同的认证机制）
    whatsapp_enabled = os.getenv("WHATSAPP_ENABLED", "").lower() in {"true", "1", "yes"}
    whatsapp_disabled_explicitly = os.getenv("WHATSAPP_ENABLED", "").lower() in {"false", "0", "no"}
    if Platform.WHATSAPP in config.platforms:
        # YAML 配置存在 —— 尊重显式禁用
        wa_cfg = config.platforms[Platform.WHATSAPP]
        if whatsapp_disabled_explicitly:
            wa_cfg.enabled = False
        elif whatsapp_enabled:
            wa_cfg.enabled = True
        # 否则：保持 YAML 所设
    elif whatsapp_enabled:
        config.platforms[Platform.WHATSAPP] = PlatformConfig(enabled=True)
    whatsapp_home = os.getenv("WHATSAPP_HOME_CHANNEL")
    if whatsapp_home and Platform.WHATSAPP in config.platforms:
        config.platforms[Platform.WHATSAPP].home_channel = HomeChannel(
            platform=Platform.WHATSAPP,
            chat_id=whatsapp_home,
            name=os.getenv("WHATSAPP_HOME_CHANNEL_NAME", "Home"),
            thread_id=os.getenv("WHATSAPP_HOME_CHANNEL_THREAD_ID") or None,
        )

    # WhatsApp Cloud API（通过 Meta 的官方 Business Platform）。
    # 与 Baileys 桥接不同：出站是纯 HTTP graph.facebook.com 调用，入站是公开
    # webhook。两个适配器可以针对不同手机号并行运行。
    whatsapp_cloud_phone_id = os.getenv("WHATSAPP_CLOUD_PHONE_NUMBER_ID")
    whatsapp_cloud_token = os.getenv("WHATSAPP_CLOUD_ACCESS_TOKEN")
    if whatsapp_cloud_phone_id and whatsapp_cloud_token:
        if Platform.WHATSAPP_CLOUD not in config.platforms:
            config.platforms[Platform.WHATSAPP_CLOUD] = PlatformConfig()
        config.platforms[Platform.WHATSAPP_CLOUD].enabled = True
        config.platforms[Platform.WHATSAPP_CLOUD].extra.update({
            "phone_number_id": whatsapp_cloud_phone_id,
            "access_token": whatsapp_cloud_token,
        })
        # 可选：app_id / app_secret（签名校验）
        wa_cloud_app_id = os.getenv("WHATSAPP_CLOUD_APP_ID")
        if wa_cloud_app_id:
            config.platforms[Platform.WHATSAPP_CLOUD].extra["app_id"] = wa_cloud_app_id
        wa_cloud_app_secret = os.getenv("WHATSAPP_CLOUD_APP_SECRET")
        if wa_cloud_app_secret:
            config.platforms[Platform.WHATSAPP_CLOUD].extra["app_secret"] = wa_cloud_app_secret
        # 可选：WABA id（分析，未来使用）
        wa_cloud_waba_id = os.getenv("WHATSAPP_CLOUD_WABA_ID")
        if wa_cloud_waba_id:
            config.platforms[Platform.WHATSAPP_CLOUD].extra["waba_id"] = wa_cloud_waba_id
        # webhook verify token —— Meta 的 hub.verify_token 共享密钥
        wa_cloud_verify_token = os.getenv("WHATSAPP_CLOUD_VERIFY_TOKEN")
        if wa_cloud_verify_token:
            config.platforms[Platform.WHATSAPP_CLOUD].extra["verify_token"] = wa_cloud_verify_token
        # webhook 服务器绑定配置（默认值内置在适配器中）
        wa_cloud_host = os.getenv("WHATSAPP_CLOUD_WEBHOOK_HOST")
        if wa_cloud_host:
            config.platforms[Platform.WHATSAPP_CLOUD].extra["webhook_host"] = wa_cloud_host
        wa_cloud_port = os.getenv("WHATSAPP_CLOUD_WEBHOOK_PORT")
        if wa_cloud_port:
            try:
                config.platforms[Platform.WHATSAPP_CLOUD].extra["webhook_port"] = int(wa_cloud_port)
            except ValueError:
                pass
        wa_cloud_path = os.getenv("WHATSAPP_CLOUD_WEBHOOK_PATH")
        if wa_cloud_path:
            config.platforms[Platform.WHATSAPP_CLOUD].extra["webhook_path"] = wa_cloud_path
        # Graph API 版本覆盖（很少需要）
        wa_cloud_api_version = os.getenv("WHATSAPP_CLOUD_API_VERSION")
        if wa_cloud_api_version:
            config.platforms[Platform.WHATSAPP_CLOUD].extra["api_version"] = wa_cloud_api_version
    whatsapp_cloud_home = os.getenv("WHATSAPP_CLOUD_HOME_CHANNEL")
    if whatsapp_cloud_home and Platform.WHATSAPP_CLOUD in config.platforms:
        config.platforms[Platform.WHATSAPP_CLOUD].home_channel = HomeChannel(
            platform=Platform.WHATSAPP_CLOUD,
            chat_id=whatsapp_cloud_home,
            name=os.getenv("WHATSAPP_CLOUD_HOME_CHANNEL_NAME", "Home"),
            thread_id=os.getenv("WHATSAPP_CLOUD_HOME_CHANNEL_THREAD_ID") or None,
        )

    # Slack
    slack_token = os.getenv("SLACK_BOT_TOKEN")
    if slack_token:
        if Platform.SLACK not in config.platforms:
            # Slack 没有 yaml 配置 —— 仅 env 设置，启用它
            config.platforms[Platform.SLACK] = PlatformConfig()
            config.platforms[Platform.SLACK].enabled = True
        else:
            slack_config = config.platforms[Platform.SLACK]
            # 读取（不要 pop）显式启用标记：下面由注册表驱动的插件启用遍历也
            # 需要它，以避免重新启用用户显式禁用的平台（Slack 现在是插件条目
            # —— #41112）。该标志在 _apply_env_overrides 末尾的最终清理中一次
            # 性清除。
            enabled_was_explicit = bool(slack_config.extra.get("_enabled_explicit", False))
            if not slack_config.enabled and not enabled_was_explicit:
                # 顶层的 Slack 设置（例如 channel 提示）不应把一个 env-token
                # 设置变成禁用的平台。只有显式的 slack.enabled/
                # platforms.slack.enabled false 才应该禁用。
                slack_config.enabled = True
        # 如果 yaml 配置存在，尊重其 enabled 标志（不要覆盖显式的
        # enabled: false）。token 仍然被存储，以便发送 Slack 消息的 skill 可以
        # 使用它，而无需激活 gateway 适配器。
        config.platforms[Platform.SLACK].token = slack_token
    slack_home = os.getenv("SLACK_HOME_CHANNEL")
    if slack_home and Platform.SLACK in config.platforms:
        config.platforms[Platform.SLACK].home_channel = HomeChannel(
            platform=Platform.SLACK,
            chat_id=slack_home,
            name=os.getenv("SLACK_HOME_CHANNEL_NAME", ""),
            thread_id=os.getenv("SLACK_HOME_CHANNEL_THREAD_ID") or None,
        )
    
    # Signal
    signal_url = os.getenv("SIGNAL_HTTP_URL")
    signal_account = os.getenv("SIGNAL_ACCOUNT")
    if signal_url and signal_account:
        signal_config = _enable_from_env(Platform.SIGNAL)
        signal_config.extra.update({
            "http_url": signal_url,
            "account": signal_account,
            "ignore_stories": os.getenv("SIGNAL_IGNORE_STORIES", "true").lower() in {"true", "1", "yes"},
        })
    signal_home = os.getenv("SIGNAL_HOME_CHANNEL")
    if signal_home and Platform.SIGNAL in config.platforms:
        config.platforms[Platform.SIGNAL].home_channel = HomeChannel(
            platform=Platform.SIGNAL,
            chat_id=signal_home,
            name=os.getenv("SIGNAL_HOME_CHANNEL_NAME", "Home"),
            thread_id=os.getenv("SIGNAL_HOME_CHANNEL_THREAD_ID") or None,
        )

    # Mattermost 平台
    mattermost_token = os.getenv("MATTERMOST_TOKEN")
    if mattermost_token:
        mattermost_url = os.getenv("MATTERMOST_URL", "")
        if not mattermost_url:
            logger.warning("MATTERMOST_TOKEN set but MATTERMOST_URL is missing")
        mattermost_config = _enable_from_env(Platform.MATTERMOST)
        mattermost_config.token = mattermost_token
        mattermost_config.extra["url"] = mattermost_url
    mattermost_home = os.getenv("MATTERMOST_HOME_CHANNEL")
    if mattermost_home and Platform.MATTERMOST in config.platforms:
        config.platforms[Platform.MATTERMOST].home_channel = HomeChannel(
            platform=Platform.MATTERMOST,
            chat_id=mattermost_home,
            name=os.getenv("MATTERMOST_HOME_CHANNEL_NAME", "Home"),
            thread_id=os.getenv("MATTERMOST_HOME_CHANNEL_THREAD_ID") or None,
        )

    # Matrix 平台
    matrix_token = os.getenv("MATRIX_ACCESS_TOKEN")
    matrix_homeserver = os.getenv("MATRIX_HOMESERVER", "")
    if matrix_token or os.getenv("MATRIX_PASSWORD"):
        if not matrix_homeserver:
            logger.warning("MATRIX_ACCESS_TOKEN/MATRIX_PASSWORD set but MATRIX_HOMESERVER is missing")
        matrix_config = _enable_from_env(Platform.MATRIX)
        if matrix_token:
            matrix_config.token = matrix_token
        matrix_config.extra["homeserver"] = matrix_homeserver
        matrix_user = os.getenv("MATRIX_USER_ID", "")
        if matrix_user:
            matrix_config.extra["user_id"] = matrix_user
        matrix_password = os.getenv("MATRIX_PASSWORD", "")
        if matrix_password:
            matrix_config.extra["password"] = matrix_password
        matrix_e2ee_mode = os.getenv("MATRIX_E2EE_MODE", "").strip().lower()
        matrix_e2ee = (
            matrix_e2ee_mode in ("required", "require", "optional", "prefer", "preferred")
            or os.getenv("MATRIX_ENCRYPTION", "").lower() in ("true", "1", "yes")
        )
        matrix_config.extra["encryption"] = matrix_e2ee
        if matrix_e2ee_mode:
            matrix_config.extra["e2ee_mode"] = matrix_e2ee_mode
        matrix_device_id = os.getenv("MATRIX_DEVICE_ID", "")
        if matrix_device_id:
            matrix_config.extra["device_id"] = matrix_device_id
    matrix_home = os.getenv("MATRIX_HOME_ROOM")
    if matrix_home and Platform.MATRIX in config.platforms:
        config.platforms[Platform.MATRIX].home_channel = HomeChannel(
            platform=Platform.MATRIX,
            chat_id=matrix_home,
            name=os.getenv("MATRIX_HOME_ROOM_NAME", "Home"),
            thread_id=os.getenv("MATRIX_HOME_ROOM_THREAD_ID") or None,
        )

    # Home Assistant 平台
    hass_token = os.getenv("HASS_TOKEN")
    if hass_token:
        if Platform.HOMEASSISTANT not in config.platforms:
            config.platforms[Platform.HOMEASSISTANT] = PlatformConfig()
        config.platforms[Platform.HOMEASSISTANT].enabled = True
        config.platforms[Platform.HOMEASSISTANT].token = hass_token
        hass_url = os.getenv("HASS_URL")
        if hass_url:
            config.platforms[Platform.HOMEASSISTANT].extra["url"] = hass_url

    # Email 平台
    email_addr = os.getenv("EMAIL_ADDRESS")
    email_pwd = os.getenv("EMAIL_PASSWORD")
    email_imap = os.getenv("EMAIL_IMAP_HOST")
    email_smtp = os.getenv("EMAIL_SMTP_HOST")
    if all([email_addr, email_pwd, email_imap, email_smtp]):
        if Platform.EMAIL not in config.platforms:
            config.platforms[Platform.EMAIL] = PlatformConfig()
        config.platforms[Platform.EMAIL].enabled = True
        config.platforms[Platform.EMAIL].extra.update({
            "address": email_addr,
            "imap_host": email_imap,
            "smtp_host": email_smtp,
        })
    email_home = os.getenv("EMAIL_HOME_ADDRESS")
    if email_home and Platform.EMAIL in config.platforms:
        config.platforms[Platform.EMAIL].home_channel = HomeChannel(
            platform=Platform.EMAIL,
            chat_id=email_home,
            name=os.getenv("EMAIL_HOME_ADDRESS_NAME", "Home"),
            thread_id=os.getenv("EMAIL_HOME_ADDRESS_THREAD_ID") or None,
        )

    # SMS（Twilio）
    twilio_sid = os.getenv("TWILIO_ACCOUNT_SID")
    if twilio_sid:
        if Platform.SMS not in config.platforms:
            config.platforms[Platform.SMS] = PlatformConfig()
        config.platforms[Platform.SMS].enabled = True
        config.platforms[Platform.SMS].api_key = os.getenv("TWILIO_AUTH_TOKEN", "")
    sms_home = os.getenv("SMS_HOME_CHANNEL")
    if sms_home and Platform.SMS in config.platforms:
        config.platforms[Platform.SMS].home_channel = HomeChannel(
            platform=Platform.SMS,
            chat_id=sms_home,
            name=os.getenv("SMS_HOME_CHANNEL_NAME", "Home"),
            thread_id=os.getenv("SMS_HOME_CHANNEL_THREAD_ID") or None,
        )

    # API Server 平台
    api_server_enabled = os.getenv("API_SERVER_ENABLED", "").lower() in {"true", "1", "yes"}
    api_server_key = os.getenv("API_SERVER_KEY", "")
    api_server_cors_origins = os.getenv("API_SERVER_CORS_ORIGINS", "")
    api_server_port = os.getenv("API_SERVER_PORT")
    api_server_host = os.getenv("API_SERVER_HOST")
    if api_server_enabled or api_server_key:
        if Platform.API_SERVER not in config.platforms:
            config.platforms[Platform.API_SERVER] = PlatformConfig()
        config.platforms[Platform.API_SERVER].enabled = True
        if api_server_key:
            config.platforms[Platform.API_SERVER].extra["key"] = api_server_key
        if api_server_cors_origins:
            origins = [origin.strip() for origin in api_server_cors_origins.split(",") if origin.strip()]
            if origins:
                config.platforms[Platform.API_SERVER].extra["cors_origins"] = origins
        if api_server_port:
            try:
                config.platforms[Platform.API_SERVER].extra["port"] = int(api_server_port)
            except ValueError:
                pass
        if api_server_host:
            config.platforms[Platform.API_SERVER].extra["host"] = api_server_host
        api_server_model_name = os.getenv("API_SERVER_MODEL_NAME", "")
        if api_server_model_name:
            config.platforms[Platform.API_SERVER].extra["model_name"] = api_server_model_name

    # Webhook 平台
    webhook_enabled = os.getenv("WEBHOOK_ENABLED", "").lower() in {"true", "1", "yes"}
    webhook_port = os.getenv("WEBHOOK_PORT")
    webhook_secret = os.getenv("WEBHOOK_SECRET", "")
    if webhook_enabled:
        if Platform.WEBHOOK not in config.platforms:
            config.platforms[Platform.WEBHOOK] = PlatformConfig()
        config.platforms[Platform.WEBHOOK].enabled = True
        if webhook_port:
            try:
                config.platforms[Platform.WEBHOOK].extra["port"] = int(webhook_port)
            except ValueError:
                pass
        if webhook_secret:
            config.platforms[Platform.WEBHOOK].extra["secret"] = webhook_secret

    # Microsoft Graph webhook 平台
    msgraph_webhook_enabled = os.getenv("MSGRAPH_WEBHOOK_ENABLED", "").lower() in {
        "true",
        "1",
        "yes",
    }
    msgraph_webhook_port = os.getenv("MSGRAPH_WEBHOOK_PORT")
    msgraph_webhook_client_state = os.getenv("MSGRAPH_WEBHOOK_CLIENT_STATE", "")
    msgraph_webhook_resources = os.getenv("MSGRAPH_WEBHOOK_ACCEPTED_RESOURCES", "")
    msgraph_webhook_allowed_cidrs = os.getenv(
        "MSGRAPH_WEBHOOK_ALLOWED_SOURCE_CIDRS", ""
    )
    if (
        msgraph_webhook_enabled
        or Platform.MSGRAPH_WEBHOOK in config.platforms
        or msgraph_webhook_port
        or msgraph_webhook_client_state
        or msgraph_webhook_resources
        or msgraph_webhook_allowed_cidrs
    ):
        if Platform.MSGRAPH_WEBHOOK not in config.platforms:
            config.platforms[Platform.MSGRAPH_WEBHOOK] = PlatformConfig()
        if msgraph_webhook_enabled:
            config.platforms[Platform.MSGRAPH_WEBHOOK].enabled = True
        if msgraph_webhook_port:
            try:
                config.platforms[Platform.MSGRAPH_WEBHOOK].extra["port"] = int(
                    msgraph_webhook_port
                )
            except ValueError:
                pass
        if msgraph_webhook_client_state:
            config.platforms[Platform.MSGRAPH_WEBHOOK].extra["client_state"] = (
                msgraph_webhook_client_state
            )
        if msgraph_webhook_resources:
            resources = [
                resource.strip()
                for resource in msgraph_webhook_resources.split(",")
                if resource.strip()
            ]
            if resources:
                config.platforms[Platform.MSGRAPH_WEBHOOK].extra[
                    "accepted_resources"
                ] = resources
        if msgraph_webhook_allowed_cidrs:
            cidrs = [
                cidr.strip()
                for cidr in msgraph_webhook_allowed_cidrs.split(",")
                if cidr.strip()
            ]
            if cidrs:
                config.platforms[Platform.MSGRAPH_WEBHOOK].extra[
                    "allowed_source_cidrs"
                ] = cidrs

    # DingTalk 平台
    dingtalk_client_id = os.getenv("DINGTALK_CLIENT_ID")
    dingtalk_client_secret = os.getenv("DINGTALK_CLIENT_SECRET")
    if dingtalk_client_id and dingtalk_client_secret:
        if Platform.DINGTALK not in config.platforms:
            config.platforms[Platform.DINGTALK] = PlatformConfig()
        config.platforms[Platform.DINGTALK].enabled = True
        config.platforms[Platform.DINGTALK].extra.update({
            "client_id": dingtalk_client_id,
            "client_secret": dingtalk_client_secret,
        })
        dingtalk_home = os.getenv("DINGTALK_HOME_CHANNEL")
        if dingtalk_home:
            config.platforms[Platform.DINGTALK].home_channel = HomeChannel(
                platform=Platform.DINGTALK,
                chat_id=dingtalk_home,
                name=os.getenv("DINGTALK_HOME_CHANNEL_NAME", "Home"),
                thread_id=os.getenv("DINGTALK_HOME_CHANNEL_THREAD_ID") or None,
            )

    # 飞书 / Lark
    feishu_app_id = os.getenv("FEISHU_APP_ID")
    feishu_app_secret = os.getenv("FEISHU_APP_SECRET")
    if feishu_app_id and feishu_app_secret:
        if Platform.FEISHU not in config.platforms:
            config.platforms[Platform.FEISHU] = PlatformConfig()
        config.platforms[Platform.FEISHU].enabled = True
        config.platforms[Platform.FEISHU].extra.update({
            "app_id": feishu_app_id,
            "app_secret": feishu_app_secret,
            "domain": os.getenv("FEISHU_DOMAIN", "feishu"),
            "connection_mode": os.getenv("FEISHU_CONNECTION_MODE", "websocket"),
        })
        feishu_encrypt_key = os.getenv("FEISHU_ENCRYPT_KEY", "")
        if feishu_encrypt_key:
            config.platforms[Platform.FEISHU].extra["encrypt_key"] = feishu_encrypt_key
        feishu_verification_token = os.getenv("FEISHU_VERIFICATION_TOKEN", "")
        if feishu_verification_token:
            config.platforms[Platform.FEISHU].extra["verification_token"] = feishu_verification_token
        feishu_home = os.getenv("FEISHU_HOME_CHANNEL")
        if feishu_home:
            config.platforms[Platform.FEISHU].home_channel = HomeChannel(
                platform=Platform.FEISHU,
                chat_id=feishu_home,
                name=os.getenv("FEISHU_HOME_CHANNEL_NAME", "Home"),
                thread_id=os.getenv("FEISHU_HOME_CHANNEL_THREAD_ID") or None,
            )

    # WeCom（企业微信）
    wecom_bot_id = os.getenv("WECOM_BOT_ID")
    wecom_secret = os.getenv("WECOM_SECRET")
    if wecom_bot_id and wecom_secret:
        if Platform.WECOM not in config.platforms:
            config.platforms[Platform.WECOM] = PlatformConfig()
        config.platforms[Platform.WECOM].enabled = True
        config.platforms[Platform.WECOM].extra.update({
            "bot_id": wecom_bot_id,
            "secret": wecom_secret,
        })
        wecom_ws_url = os.getenv("WECOM_WEBSOCKET_URL", "")
        if wecom_ws_url:
            config.platforms[Platform.WECOM].extra["websocket_url"] = wecom_ws_url
        wecom_home = os.getenv("WECOM_HOME_CHANNEL")
        if wecom_home:
            config.platforms[Platform.WECOM].home_channel = HomeChannel(
                platform=Platform.WECOM,
                chat_id=wecom_home,
                name=os.getenv("WECOM_HOME_CHANNEL_NAME", "Home"),
                thread_id=os.getenv("WECOM_HOME_CHANNEL_THREAD_ID") or None,
            )

    # WeCom 回调模式（自建应用）
    wecom_callback_corp_id = os.getenv("WECOM_CALLBACK_CORP_ID")
    wecom_callback_corp_secret = os.getenv("WECOM_CALLBACK_CORP_SECRET")
    if wecom_callback_corp_id and wecom_callback_corp_secret:
        if Platform.WECOM_CALLBACK not in config.platforms:
            config.platforms[Platform.WECOM_CALLBACK] = PlatformConfig()
        config.platforms[Platform.WECOM_CALLBACK].enabled = True
        config.platforms[Platform.WECOM_CALLBACK].extra.update({
            "corp_id": wecom_callback_corp_id,
            "corp_secret": wecom_callback_corp_secret,
            "agent_id": os.getenv("WECOM_CALLBACK_AGENT_ID", ""),
            "token": os.getenv("WECOM_CALLBACK_TOKEN", ""),
            "encoding_aes_key": os.getenv("WECOM_CALLBACK_ENCODING_AES_KEY", ""),
            "host": os.getenv("WECOM_CALLBACK_HOST", "0.0.0.0"),
            "port": env_int("WECOM_CALLBACK_PORT", 8645),
        })

    # Weixin（通过 iLink Bot API 的个人微信）
    weixin_token = os.getenv("WEIXIN_TOKEN")
    weixin_account_id = os.getenv("WEIXIN_ACCOUNT_ID")
    if weixin_token or weixin_account_id:
        if Platform.WEIXIN not in config.platforms:
            config.platforms[Platform.WEIXIN] = PlatformConfig()
        config.platforms[Platform.WEIXIN].enabled = True
        if weixin_token:
            config.platforms[Platform.WEIXIN].token = weixin_token
        extra = config.platforms[Platform.WEIXIN].extra
        if weixin_account_id:
            extra["account_id"] = weixin_account_id
        weixin_base_url = os.getenv("WEIXIN_BASE_URL", "").strip()
        if weixin_base_url:
            extra["base_url"] = weixin_base_url.rstrip("/")
        weixin_cdn_base_url = os.getenv("WEIXIN_CDN_BASE_URL", "").strip()
        if weixin_cdn_base_url:
            extra["cdn_base_url"] = weixin_cdn_base_url.rstrip("/")
        weixin_dm_policy = os.getenv("WEIXIN_DM_POLICY", "").strip().lower()
        if weixin_dm_policy:
            extra["dm_policy"] = weixin_dm_policy
        weixin_group_policy = os.getenv("WEIXIN_GROUP_POLICY", "").strip().lower()
        if weixin_group_policy:
            extra["group_policy"] = weixin_group_policy
        weixin_allowed_users = os.getenv("WEIXIN_ALLOWED_USERS", "").strip()
        if weixin_allowed_users:
            extra["allow_from"] = weixin_allowed_users
        weixin_group_allowed_users = os.getenv("WEIXIN_GROUP_ALLOWED_USERS", "").strip()
        if weixin_group_allowed_users:
            extra["group_allow_from"] = weixin_group_allowed_users
        weixin_split_multiline = os.getenv("WEIXIN_SPLIT_MULTILINE_MESSAGES", "").strip()
        if weixin_split_multiline:
            extra["split_multiline_messages"] = weixin_split_multiline
        weixin_home = os.getenv("WEIXIN_HOME_CHANNEL", "").strip()
        if weixin_home:
            config.platforms[Platform.WEIXIN].home_channel = HomeChannel(
                platform=Platform.WEIXIN,
                chat_id=weixin_home,
                name=os.getenv("WEIXIN_HOME_CHANNEL_NAME", "Home"),
                thread_id=os.getenv("WEIXIN_HOME_CHANNEL_THREAD_ID") or None,
            )

    # BlueBubbles（iMessage）
    bluebubbles_server_url = os.getenv("BLUEBUBBLES_SERVER_URL")
    bluebubbles_password = os.getenv("BLUEBUBBLES_PASSWORD")
    if bluebubbles_server_url and bluebubbles_password:
        if Platform.BLUEBUBBLES not in config.platforms:
            config.platforms[Platform.BLUEBUBBLES] = PlatformConfig()
        config.platforms[Platform.BLUEBUBBLES].enabled = True
        config.platforms[Platform.BLUEBUBBLES].extra.update({
            "server_url": bluebubbles_server_url.rstrip("/"),
            "password": bluebubbles_password,
            "webhook_host": os.getenv("BLUEBUBBLES_WEBHOOK_HOST", "127.0.0.1"),
            "webhook_port": env_int("BLUEBUBBLES_WEBHOOK_PORT", 8645),
            "webhook_path": os.getenv("BLUEBUBBLES_WEBHOOK_PATH", "/bluebubbles-webhook"),
            "send_read_receipts": os.getenv("BLUEBUBBLES_SEND_READ_RECEIPTS", "true").lower() in {"true", "1", "yes"},
        })
        bluebubbles_require_mention = os.getenv("BLUEBUBBLES_REQUIRE_MENTION")
        if bluebubbles_require_mention is not None:
            config.platforms[Platform.BLUEBUBBLES].extra["require_mention"] = (
                bluebubbles_require_mention.lower() in {"true", "1", "yes", "on"}
            )
        bluebubbles_mention_patterns = os.getenv("BLUEBUBBLES_MENTION_PATTERNS")
        if bluebubbles_mention_patterns:
            try:
                parsed_patterns = json.loads(bluebubbles_mention_patterns)
            except Exception:
                parsed_patterns = [
                    part.strip()
                    for part in bluebubbles_mention_patterns.replace("\n", ",").split(",")
                    if part.strip()
                ]
            config.platforms[Platform.BLUEBUBBLES].extra["mention_patterns"] = parsed_patterns
    bluebubbles_home = os.getenv("BLUEBUBBLES_HOME_CHANNEL")
    if bluebubbles_home and Platform.BLUEBUBBLES in config.platforms:
        config.platforms[Platform.BLUEBUBBLES].home_channel = HomeChannel(
            platform=Platform.BLUEBUBBLES,
            chat_id=bluebubbles_home,
            name=os.getenv("BLUEBUBBLES_HOME_CHANNEL_NAME", "Home"),
            thread_id=os.getenv("BLUEBUBBLES_HOME_CHANNEL_THREAD_ID") or None,
        )

    # QQ（官方 Bot API v2）
    qq_app_id = os.getenv("QQ_APP_ID")
    qq_client_secret = os.getenv("QQ_CLIENT_SECRET")
    if qq_app_id or qq_client_secret:
        if Platform.QQBOT not in config.platforms:
            config.platforms[Platform.QQBOT] = PlatformConfig()
        config.platforms[Platform.QQBOT].enabled = True
        extra = config.platforms[Platform.QQBOT].extra
        if qq_app_id:
            extra["app_id"] = qq_app_id
        if qq_client_secret:
            extra["client_secret"] = qq_client_secret
        qq_allowed_users = os.getenv("QQ_ALLOWED_USERS", "").strip()
        if qq_allowed_users:
            extra["allow_from"] = qq_allowed_users
        qq_group_allowed = os.getenv("QQ_GROUP_ALLOWED_USERS", "").strip()
        if qq_group_allowed:
            extra["group_allow_from"] = qq_group_allowed
        qq_home = os.getenv("QQBOT_HOME_CHANNEL", "").strip()
        qq_home_name_env = "QQBOT_HOME_CHANNEL_NAME"
        if not qq_home:
            # 向后兼容：接受重命名前的名字并记录一次性警告。
            legacy_home = os.getenv("QQ_HOME_CHANNEL", "").strip()
            if legacy_home:
                qq_home = legacy_home
                qq_home_name_env = "QQ_HOME_CHANNEL_NAME"
                logging.getLogger(__name__).warning(
                    "QQ_HOME_CHANNEL is deprecated; rename to QQBOT_HOME_CHANNEL "
                    "in your .env for consistency with the platform key."
                )
        if qq_home:
            config.platforms[Platform.QQBOT].home_channel = HomeChannel(
                platform=Platform.QQBOT,
                chat_id=qq_home,
                name=os.getenv("QQBOT_HOME_CHANNEL_NAME") or os.getenv(qq_home_name_env, "Home"),
                thread_id=(
                    os.getenv("QQBOT_HOME_CHANNEL_THREAD_ID")
                    or os.getenv("QQ_HOME_CHANNEL_THREAD_ID")
                    or None
                ),
            )

    # Yuanbao —— YUANBAO_APP_ID 优先
    yuanbao_app_id = os.getenv("YUANBAO_APP_ID") or os.getenv("YUANBAO_APP_KEY")
    yuanbao_app_secret = os.getenv("YUANBAO_APP_SECRET")
    if yuanbao_app_id and yuanbao_app_secret:
        if Platform.YUANBAO not in config.platforms:
            config.platforms[Platform.YUANBAO] = PlatformConfig()
        config.platforms[Platform.YUANBAO].enabled = True
        extra = config.platforms[Platform.YUANBAO].extra
        extra["app_id"] = yuanbao_app_id
        extra["app_secret"] = yuanbao_app_secret
        yuanbao_bot_id = os.getenv("YUANBAO_BOT_ID")
        if yuanbao_bot_id:
            extra["bot_id"] = yuanbao_bot_id
        yuanbao_ws_url = os.getenv("YUANBAO_WS_URL")
        if yuanbao_ws_url:
            extra["ws_url"] = yuanbao_ws_url
        yuanbao_api_domain = os.getenv("YUANBAO_API_DOMAIN")
        if yuanbao_api_domain:
            extra["api_domain"] = yuanbao_api_domain
        yuanbao_route_env = os.getenv("YUANBAO_ROUTE_ENV")
        if yuanbao_route_env:
            extra["route_env"] = yuanbao_route_env
        yuanbao_home = os.getenv("YUANBAO_HOME_CHANNEL")
        if yuanbao_home:
            config.platforms[Platform.YUANBAO].home_channel = HomeChannel(
                platform=Platform.YUANBAO,
                chat_id=yuanbao_home,
                name=os.getenv("YUANBAO_HOME_CHANNEL_NAME", "Home"),
                thread_id=os.getenv("YUANBAO_HOME_CHANNEL_THREAD_ID") or None,
            )
        yuanbao_dm_policy = os.getenv("YUANBAO_DM_POLICY")
        if yuanbao_dm_policy:
            extra["dm_policy"] = yuanbao_dm_policy.strip().lower()
        yuanbao_dm_allow_from = os.getenv("YUANBAO_DM_ALLOW_FROM")
        if yuanbao_dm_allow_from:
            extra["dm_allow_from"] = yuanbao_dm_allow_from
        yuanbao_group_policy = os.getenv("YUANBAO_GROUP_POLICY")
        if yuanbao_group_policy:
            extra["group_policy"] = yuanbao_group_policy.strip().lower()
        yuanbao_group_allow_from = os.getenv("YUANBAO_GROUP_ALLOW_FROM")
        if yuanbao_group_allow_from:
            extra["group_allow_from"] = yuanbao_group_allow_from

    # session 设置
    idle_minutes = os.getenv("SESSION_IDLE_MINUTES")
    if idle_minutes:
        try:
            config.default_reset_policy.idle_minutes = int(idle_minutes)
        except ValueError:
            pass
    
    reset_hour = os.getenv("SESSION_RESET_HOUR")
    if reset_hour:
        try:
            config.default_reset_policy.at_hour = int(reset_hour)
        except ValueError:
            pass

    # 由注册表驱动的插件平台启用。内置平台在上方有显式块；插件暴露
    # check_fn()，它是"我的 env 变量设置了吗？"的唯一事实来源。当它返回
    # True 时，确保平台已启用，以便 start() 会创建其适配器。需要从 env 变量
    # 为 ``PlatformConfig.extra`` 填值的插件（例如 Google Chat 的
    # project_id / subscription_name）可在其 PlatformEntry 上提供
    # ``env_enablement_fn`` —— 在此处于适配器构造之前调用。
    #
    # 启用门控（#31116）：当插件注册了 ``is_connected``
    # （"用户是否真的为此配置了凭证？"的检查）时，我们必须在翻转
    # ``enabled = True`` 之前咨询它。否则仅靠 ``check_fn`` —— 对于适配器
    # 插件，它通常只验证 SDK 可导入 / 懒安装它 —— 会悄悄启用用户从未选择
    # 启用的平台，然后 gateway 试图在没有 token 的情况下连接 Discord / Teams /
    # Google Chat，并发出吵闹的永久重试错误。``_platform_status`` 已在提交
    # 7849a3d73 中针对同一类 bug 修复；这是运行时对应的修复。
    try:
        from hermes_cli.plugins import discover_plugins
        discover_plugins()  # 幂等
        from gateway.platform_registry import platform_registry
        for entry in platform_registry.plugin_entries():
            try:
                platform = Platform(entry.name)
            except Exception as e:
                logger.debug("unknown platform name %r: %s", entry.name, e)
                continue
            existing_cfg = config.platforms.get(platform)
            # 尊重显式的 ``enabled: false``（YAML / gateway.json / dashboard
            # PUT）。``_enabled_explicit`` 在 load_gateway_config() 中
            # （通过 _merge_platform_map / 共享键循环）当用户为该平台写了
            # ``enabled`` 时设置；如果他们显式禁用了它，仅因为 check_fn() /
            # is_connected() 通过就在此处重新启用（例如存在 token 但用户设置了
            # telegram.enabled: false）。#41112。
            if (
                existing_cfg is not None
                and not existing_cfg.enabled
                and bool((existing_cfg.extra or {}).get("_enabled_explicit", False))
            ):
                continue
            # 从 ``env_enablement_fn`` 填充候选 extra，以便那些 ``is_connected``
            # 读取 ``config.extra`` 的插件（例如 Google Chat 的
            # ``_is_connected`` 检查 ``config.extra["project_id"]``）能看到与
            # 启用后相同的状态。没有这一步，仅用 env 变量配置 Google Chat 的
            # 设置会悄悄无法通过下面的门控，即便用户已配置。那些
            # ``is_connected`` 直接读取 env 变量的插件（Discord、IRC、Teams、
            # LINE、ntfy、Simplex）不受影响；这仅恢复 Google Chat。
            seed_for_probe = None
            if entry.env_enablement_fn is not None:
                try:
                    seed_for_probe = entry.env_enablement_fn()
                except Exception as e:
                    logger.debug(
                        "env_enablement_fn for %s raised: %s", entry.name, e
                    )
                    seed_for_probe = None

            # 仅对尚未在 YAML / env 中显式配置的平台（existing_cfg 且
            # enabled=True 意味着用户自己写的，或另一个 env 变量桥接启用了它
            # —— 保留该决策）咨询 is_connected。
            if existing_cfg is None or not existing_cfg.enabled:
                if entry.is_connected is not None:
                    try:
                        # 用 ``enabled=True`` 探测，因为我们问的是"如果启用
                        # 这个插件，它会配置好吗？"而不是"它当前启用了吗？"。
                        # Google Chat 的 ``_is_connected`` 在
                        # ``config.enabled`` 为 False 时短路，这在默认的
                        # ``PlatformConfig()`` 上即便正确设置了 env 变量也会
                        # 导致门控失败。
                        if existing_cfg is not None:
                            probe_cfg = existing_cfg
                            if not probe_cfg.enabled:
                                probe_cfg = PlatformConfig(
                                    enabled=True,
                                    extra=dict(probe_cfg.extra or {}),
                                )
                        else:
                            probe_cfg = PlatformConfig(enabled=True)
                        if isinstance(seed_for_probe, dict) and seed_for_probe:
                            # 不要修改 ``existing_cfg``；探测获得一个瞬态视图，
                            # env 填充的 extra 叠加在已存在的内容之上。
                            probe_extra = dict(getattr(probe_cfg, "extra", {}) or {})
                            for k, v in seed_for_probe.items():
                                if k == "home_channel":
                                    continue
                                probe_extra.setdefault(k, v)
                            probe_cfg = PlatformConfig(
                                enabled=True,
                                extra=probe_extra,
                            )
                        configured = bool(entry.is_connected(probe_cfg))
                    except Exception as exc:
                        logger.debug(
                            "is_connected for %s raised: %s — skipping enablement",
                            entry.name, exc,
                        )
                        configured = False
                    if not configured:
                        logger.debug(
                            "Plugin platform '%s' available but not configured "
                            "(is_connected returned False) — skipping enable",
                            entry.name,
                        )
                        continue
            # 最后验证依赖 —— 仅对已启用或通过了上面凭证门控的平台。对于适配器
            # 插件，``check_fn`` 会作为副作用懒安装（pip）平台 SDK，所以把它作为
            # 对每个已注册平台的无条件扫描，会让 ``load_gateway_config()`` 在
            # 每次调用时 pip-install Discord/Telegram/Slack/Feishu/Dingtalk
            # —— 包括桌面/dashboard 就绪探针（``GET /api/status``，它会同步等待
            # 此函数）—— 即便用户一个都没配置。那会阻塞启动直到每次安装完成，
            # 并导致桌面应用超时和启动循环（卡在 94%）。
            try:
                if not entry.check_fn():
                    continue
            except Exception as e:
                logger.debug("check_fn for %s raised: %s", entry.name, e)
                continue
            if platform not in config.platforms:
                config.platforms[platform] = PlatformConfig()
            config.platforms[platform].enabled = True
            # 把 env 填充的 extra 提交到现已启用的平台上。
            # 我们已经在上面调用过 ``env_enablement_fn``（用于探测）；复用该
            # 结果而不是调用两次。
            if isinstance(seed_for_probe, dict) and seed_for_probe:
                seed = dict(seed_for_probe)
                # 提取 home_channel 字典（如果提供），以便我们把它接成一个
                # 正规的 HomeChannel dataclass。其他所有内容都合并进 ``extra``。
                home = seed.pop("home_channel", None)
                config.platforms[platform].extra.update(seed)
                if isinstance(home, dict) and home.get("chat_id"):
                    config.platforms[platform].home_channel = HomeChannel(
                        platform=platform,
                        chat_id=str(home["chat_id"]),
                        name=str(home.get("name") or "Home"),
                        thread_id=(
                            str(home["thread_id"])
                            if home.get("thread_id")
                            else None
                        ),
                    )
    except Exception as e:
        logger.debug("Plugin platform enable pass failed: %s", e)

    # Relay（由 connector 托管的通用平台，实验性）。当通过 GATEWAY_RELAY_URL
    # （env）或 gateway.relay_url（config.yaml）配置了 connector relay URL 时
    # 启用。适配器在 gateway 启动时注册进 platform_registry
    # （gateway.relay.register_relay_adapter），并主动拨出连接到 connector ——
    # 因此，与 Telegram/Matrix 一样，它没有公开的入站端口，只需要
    # Platform.RELAY 出现并启用在 config.platforms 中，start_gateway() 的
    # 连接循环就会把它拉起。连接检查器（_PLATFORM_CONNECTED_CHECKERS 中的
    # Platform.RELAY）依据 extra["relay_url"]，所以在此把 URL 镜像进 extra。
    relay_url_env = os.getenv("GATEWAY_RELAY_URL", "").strip()
    relay_url_yaml = ""
    existing_relay = config.platforms.get(Platform.RELAY)
    if existing_relay is not None:
        relay_url_yaml = str(existing_relay.extra.get("relay_url") or "").strip()
    relay_url_val = relay_url_env or relay_url_yaml
    if relay_url_val:
        relay_config = _enable_from_env(Platform.RELAY)
        relay_config.extra["relay_url"] = relay_url_val.rstrip("/")

    for platform_config in config.platforms.values():
        platform_config.extra.pop("_enabled_explicit", None)
