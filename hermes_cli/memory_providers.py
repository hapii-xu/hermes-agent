"""桌面 memory provider 的声明式配置 schema。

每个 memory provider 在此*声明*其可配置接口 — 字段、类型、哪些值是
secret，以及（对于 select）允许的选项。桌面 UI 中的一个通用渲染器和
一组通用的 ``GET/PUT /api/memory/providers/{name}/config`` 端点对驱动
整个体验，因此添加新的 provider（mem0、honcho 等）只需纯声明，
无需任何定制的 UI 组件或端点。

本模块有意保持为纯数据：不从 config/env 层导入任何内容。
``web_server`` 负责通用的读写逻辑，针对 config.yaml、provider 配置文件
和 env store 解释这些声明。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field

# 通用渲染器理解的字段类型。
KIND_TEXT = "text"
KIND_SELECT = "select"
KIND_SECRET = "secret"


@dataclass(frozen=True)
class ProviderFieldOption:
    """``select`` 字段的单个选项。"""

    value: str
    label: str
    description: str = ""


@dataclass(frozen=True)
class ProviderField:
    """memory provider 的一个可配置字段。

    字段仅存储在一个位置，由 ``kind`` 决定：

    * ``text`` / ``select`` — 持久化到 provider 的 JSON 配置文件
      （``<hermes_home>/<provider>/config.json``）中的 ``key`` 下。
    * ``secret`` — 持久化到 env store 中的 ``env_key`` 下，且永不通过
      API 读出（仅暴露 ``is_set`` 标志）。

    ``aliases`` 和 ``env_fallbacks`` 允许字段读取由早期 CLI/env 设置
    写入的旧值，而无需重新引入每个 provider 的特定代码。
    """

    key: str
    label: str
    kind: str = KIND_TEXT
    default: str = ""
    description: str = ""
    placeholder: str = ""
    options: tuple[ProviderFieldOption, ...] = ()
    env_key: str | None = None
    aliases: tuple[str, ...] = ()
    env_fallbacks: tuple[str, ...] = ()

    @property
    def is_secret(self) -> bool:
        return self.kind == KIND_SECRET

    def allowed_values(self) -> set[str]:
        return {opt.value for opt in self.options}


@dataclass(frozen=True)
class MemoryProvider:
    """已声明的 memory provider 及其可配置字段。"""

    name: str
    label: str
    fields: tuple[ProviderField, ...] = dataclass_field(default_factory=tuple)


HINDSIGHT = MemoryProvider(
    name="hindsight",
    label="Hindsight",
    fields=(
        ProviderField(
            key="mode",
            label="Mode",
            kind=KIND_SELECT,
            default="cloud",
            description="Hermes 如何连接到 Hindsight。",
            options=(
                ProviderFieldOption(
                    "cloud",
                    "Cloud",
                    "Hindsight Cloud API（轻量级，只需 API key）",
                ),
                ProviderFieldOption(
                    "local_external",
                    "Local External",
                    "连接到现有的 Hindsight 实例",
                ),
            ),
        ),
        ProviderField(
            key="api_key",
            label="API key",
            kind=KIND_SECRET,
            env_key="HINDSIGHT_API_KEY",
            description="用于向 Hindsight API 进行身份验证。",
            placeholder="输入 Hindsight API key",
        ),
        ProviderField(
            key="api_url",
            label="API URL",
            kind=KIND_TEXT,
            default="https://api.hindsight.vectorize.io",
            aliases=("apiUrl",),
            env_fallbacks=("HINDSIGHT_API_URL",),
        ),
        ProviderField(
            key="bank_id",
            label="Bank ID",
            kind=KIND_TEXT,
            default="hermes",
            aliases=("bankId",),
        ),
        ProviderField(
            key="recall_budget",
            label="Recall budget",
            kind=KIND_SELECT,
            default="mid",
            aliases=("budget",),
            options=(
                ProviderFieldOption("low", "low"),
                ProviderFieldOption("mid", "mid"),
                ProviderFieldOption("high", "high"),
            ),
        ),
    ),
)


# 公开桌面配置的 provider 注册表。没有条目的 provider
# （例如 ``builtin``）不会渲染任何配置面板。
MEMORY_PROVIDERS: dict[str, MemoryProvider] = {
    HINDSIGHT.name: HINDSIGHT,
}


def get_memory_provider(name: str) -> MemoryProvider | None:
    """返回 ``name`` 对应的已声明 provider，未声明则返回 ``None``。"""

    return MEMORY_PROVIDERS.get(name)
