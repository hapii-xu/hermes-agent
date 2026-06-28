"""
平台 adapter 注册表（Platform Adapter Registry）

允许平台 adapter（内置和插件）自行注册，这样 gateway 就能在没有硬编码
if/elif 链的情况下发现并实例化它们。

内置 adapter 目前仍使用 ``_create_adapter()`` 中现有的 if/elif。
插件 adapter 通过 ``PluginContext.register_platform()`` 在此注册，并且会
优先被查找——如果没有找到，gateway 就会回退到遗留代码路径。

用法（插件侧）：

    from gateway.platform_registry import platform_registry, PlatformEntry

    platform_registry.register(PlatformEntry(
        name="irc",
        label="IRC",
        adapter_factory=lambda cfg: IRCAdapter(cfg),
        check_fn=check_requirements,
        validate_config=lambda cfg: bool(cfg.extra.get("server")),
        required_env=["IRC_SERVER"],
        install_hint="pip install irc",
    ))

用法（gateway 侧）：

    adapter = platform_registry.create_adapter("irc", platform_config)
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class PlatformEntry:
    """单个平台 adapter 的元数据与工厂。"""

    # 在 config.yaml 中使用的标识符（例如 "irc"、"viber"）。
    name: str

    # 人类可读的标签（例如 "IRC"、"Viber"）。
    label: str

    # 工厂可调用对象：接收一个 PlatformConfig，返回一个 adapter 实例。
    # 使用工厂而不是裸类，可以让插件做自定义初始化
    # （例如传入额外的 kwargs、用 try/except 包裹）。
    adapter_factory: Callable[[Any], Any]

    # 当平台的依赖可用时返回 True。
    check_fn: Callable[[], bool]

    # 可选：给定一个 PlatformConfig，判断它是否被正确配置。
    # 如果为 None，注册表会跳过配置校验，让 adapter 在 connect() 时
    # 带着一个描述性错误而失败。
    validate_config: Optional[Callable[[Any], bool]] = None

    # 可选：给定一个 PlatformConfig，判断该平台是否已连接/已启用。
    # 由 ``GatewayConfig.get_connected_platforms()`` 和 setup UI 状态使用。
    # 如果为 None，则回退到 ``validate_config`` 或 ``check_fn``。
    is_connected: Optional[Callable[[Any], bool]] = None

    # 本平台所需的环境变量（用于 ``hermes setup`` 的展示）。
    required_env: list = field(default_factory=list)

    # 当 check_fn 返回 False 时展示的提示。
    install_hint: str = ""

    # 可选的交互式配置 setup 函数。
    # 签名：() -> None（提示用户、保存环境变量）。
    # 如果为 None，则回退到 _setup_standard_platform（需要 token_var + vars）
    # 或一个通用的“请设置这些环境变量”展示。
    setup_fn: Optional[Callable[[], None]] = None

    # "builtin" 或 "plugin"
    source: str = "plugin"

    # 注册本条目的 plugin manifest 名称（内置项为空）。
    # ``hermes gateway setup`` 用它在用户配置某平台时自动启用其所属插件。
    plugin_name: str = ""

    # ── 授权用的环境变量名（用于 _is_user_authorized 集成）──
    # 例如 "IRC_ALLOWED_USERS" —— 以逗号分隔的用户 ID 列表。
    allowed_users_env: str = ""
    # 例如 "IRC_ALLOW_ALL_USERS" —— 如果为真，则所有用户都获得授权。
    allow_all_env: str = ""

    # ── 消息长度限制 ──
    # 用于智能分块的最大消息长度。0 = 无限制。
    max_message_length: int = 0

    # ── 隐私 ──
    # 如果为 True，会话描述会脱敏 PII（电话号码等）。
    pii_safe: bool = False

    # ── 展示 ──
    # 用于 CLI/gateway 展示的 emoji（例如 "💬"）
    emoji: str = "🔌"

    # 本平台是否应出现在 _UPDATE_ALLOWED_PLATFORMS 中
    # （允许从该平台执行 /update 命令）。
    allow_update_command: bool = True

    # ── LLM 指引 ──
    # 注入到 system prompt 的平台提示（例如 "You are on IRC.
    # Do not use markdown."）。空字符串 = 无提示。
    platform_hint: str = ""

    # ── 环境驱动的自动配置 ──
    # 可选：读取环境变量，返回一个 ``PlatformConfig.extra`` 字段的 dict，
    # 在平台被自动启用时作为种子。在 ``_apply_env_overrides`` 期间、
    # adapter 构造之前调用，这样 ``gateway status`` 等命令就能在不实例化
    # adapter 的情况下反映仅来自环境变量的配置。返回 ``None``（或空 dict）
    # 表示跳过。
    # 签名：() -> Optional[dict[str, Any]]
    env_enablement_fn: Optional[Callable[[], Optional[dict]]] = None

    # ── YAML→env 配置桥 ──
    # 可选：把本平台的 ``config.yaml`` 键翻译成环境变量，以及/或者直接
    # 填充 ``PlatformConfig.extra``。让插件自己拥有它的 YAML 配置翻译，
    # 而不必强迫核心的 ``gateway/config.py`` 了解每个平台的 schema。
    #
    # 签名：(yaml_cfg: dict, platform_cfg: dict) -> Optional[dict]
    # 在 ``load_gateway_config()`` 中、通用 shared-key 循环之后、
    # ``_apply_env_overrides`` 之前调用。允许修改 ``os.environ``
    # （使用 ``not os.getenv(...)`` 守卫以保持 env > YAML 的优先级）；
    # 任何返回的 dict 会被合并进 ``PlatformConfig.extra``。异常会被捕获
    # 并以 debug 级别记录。
    # 完整契约和一个完整示例见
    # website/docs/developer-guide/adding-platform-adapters.md。
    apply_yaml_config_fn: Optional[Callable[[dict, dict], Optional[dict]]] = None

    # 可选：用于 cron/通知投递的 home-channel 环境变量名
    # （例如 ``"IRC_HOME_CHANNEL"``）。设置后，``cron.scheduler`` 会把
    # 本平台视为一个合法的 ``deliver=<name>`` 目标，并读取该环境变量来
    # 解析默认的 chat/room ID。空 = 不支持 cron home-channel。
    cron_deliver_env_var: str = ""

    # ── 独立（进程外）发送 ──
    # 可选：在没有活动 gateway adapter 的情况下投递消息的 async 协程。
    # 当 ``cron`` 运行在与 gateway 分离的进程中、因此进程内 adapter 的
    # weakref 为 ``None`` 时，由
    # ``tools/send_message_tool._send_via_adapter`` 调用。
    #
    # 签名：
    #     async (pconfig, chat_id, message, *, thread_id=None,
    #            media_files=None, force_document=False) -> dict
    #
    # 成功时返回 ``{"success": True, "message_id": ...}``，失败时返回
    # ``{"error": str}``。插件作者通常会打开一个临时连接 / 获取一个
    # 新的 OAuth token，发送，然后关闭。没有这个钩子的话，当 gateway 与
    # cron 进程不在同一进程时，插件平台就无法作为 cron 的 ``deliver=``
    # 目标。
    standalone_sender_fn: Optional[Callable[..., Awaitable[dict]]] = None


class PlatformRegistry:
    """平台 adapter 的中央注册表。

    读取是线程安全的（在 GIL 下 dict 查找是原子的）。
    写入发生在启动阶段的顺序发现过程中。
    """

    def __init__(self) -> None:
        self._entries: dict[str, PlatformEntry] = {}

    def register(self, entry: PlatformEntry) -> None:
        """注册一个平台 adapter 条目。

        如果已存在同名条目，它会被替换（后写者优先 —— 这让插件在需要时
        可以覆盖内置 adapter）。
        """
        if entry.name in self._entries:
            prev = self._entries[entry.name]
            logger.info(
                "Platform '%s' re-registered (was %s, now %s)",
                entry.name,
                prev.source,
                entry.source,
            )
        self._entries[entry.name] = entry
        logger.debug("Registered platform adapter: %s (%s)", entry.name, entry.source)

    def unregister(self, name: str) -> bool:
        """移除一个平台条目。如果该条目原本存在则返回 True。"""
        return self._entries.pop(name, None) is not None

    def get(self, name: str) -> Optional[PlatformEntry]:
        """按名称查找一个平台条目。"""
        return self._entries.get(name)

    def all_entries(self) -> list[PlatformEntry]:
        """返回所有已注册的平台条目。"""
        return list(self._entries.values())

    def plugin_entries(self) -> list[PlatformEntry]:
        """只返回由插件注册的平台条目。"""
        return [e for e in self._entries.values() if e.source == "plugin"]

    def is_registered(self, name: str) -> bool:
        return name in self._entries

    def create_adapter(self, name: str, config: Any) -> Optional[Any]:
        """为给定平台名创建一个 adapter 实例。

        在以下情况返回 None：
        - 没有为 *name* 注册的条目
        - check_fn() 返回 False（缺少依赖）
        - validate_config() 返回 False（配置错误）
        - 工厂抛出了异常
        """
        entry = self._entries.get(name)
        if entry is None:
            return None

        if not entry.check_fn():
            hint = f" ({entry.install_hint})" if entry.install_hint else ""
            logger.warning(
                "Platform '%s' requirements not met%s",
                entry.label,
                hint,
            )
            return None

        if entry.validate_config is not None:
            try:
                if not entry.validate_config(config):
                    logger.warning(
                        "Platform '%s' config validation failed",
                        entry.label,
                    )
                    return None
            except Exception as e:
                logger.warning(
                    "Platform '%s' config validation error: %s",
                    entry.label,
                    e,
                )
                return None

        try:
            adapter = entry.adapter_factory(config)
            return adapter
        except Exception as e:
            logger.error(
                "Failed to create adapter for platform '%s': %s",
                entry.label,
                e,
                exc_info=True,
            )
            return None


# 模块级单例
platform_registry = PlatformRegistry()
