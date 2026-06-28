"""
Hermes Agent 中 provider 身份的唯一事实来源。

两个数据源，在运行时合并：

1. **models.dev 目录** — 109+ 个 provider，包含 base URL、env var、显示
   名称和完整的 model 元数据（context、cost、capabilities）。这是主要
   数据库。

2. **Hermes 覆盖层** — transport 类型、auth 模式、aggregator 标志以及
   models.dev 未跟踪的额外 env var。数据量小，维护在此处。

3. **用户配置**（config.yaml 中的 ``providers:`` 部分）— 用户定义的
   endpoint 和覆盖项。合并到所有其他数据之上。

其他模块从此文件导入。不存在并行注册表。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from utils import base_url_host_matches, base_url_hostname

logger = logging.getLogger(__name__)


# -- Hermes 覆盖层 -----------------------------------------------------------
# models.dev 未提供的 Hermes 特定元数据。

@dataclass(frozen=True)
class HermesOverlay:
    """叠加在 models.dev 之上的 Hermes 特定 provider 元数据。"""

    transport: str = "openai_chat"        # openai_chat | anthropic_messages | codex_responses
    is_aggregator: bool = False
    auth_type: str = "api_key"            # api_key | oauth_device_code | oauth_external | external_process
    extra_env_vars: Tuple[str, ...] = ()  # models.dev 未列出的 env var
    base_url_override: str = ""           # 当 models.dev URL 错误或缺失时覆盖
    base_url_env_var: str = ""            # 用户自定义 base URL 的 env var


HERMES_OVERLAYS: Dict[str, HermesOverlay] = {
    "openrouter": HermesOverlay(
        transport="openai_chat",
        is_aggregator=True,
        base_url_env_var="OPENROUTER_BASE_URL",
    ),
    "nous": HermesOverlay(
        transport="openai_chat",
        auth_type="oauth_device_code",
        base_url_override="https://inference-api.nousresearch.com/v1",
    ),
    "openai-codex": HermesOverlay(
        transport="codex_responses",
        auth_type="oauth_external",
        base_url_override="https://chatgpt.com/backend-api/codex",
    ),
    "openai-api": HermesOverlay(
        transport="codex_responses",
        base_url_override="https://api.openai.com/v1",
        base_url_env_var="OPENAI_BASE_URL",
    ),
    "xai-oauth": HermesOverlay(
        transport="codex_responses",
        auth_type="oauth_external",
        base_url_override="https://api.x.ai/v1",
        base_url_env_var="XAI_BASE_URL",
    ),
    "qwen-oauth": HermesOverlay(
        transport="openai_chat",
        auth_type="oauth_external",
        base_url_override="https://portal.qwen.ai/v1",
        base_url_env_var="HERMES_QWEN_BASE_URL",
    ),
    "lmstudio": HermesOverlay(
        transport="openai_chat",
        auth_type="api_key",
        extra_env_vars=("LM_API_KEY",),
        base_url_override="http://127.0.0.1:1234/v1",
        base_url_env_var="LM_BASE_URL",
    ),
    "copilot-acp": HermesOverlay(
        transport="codex_responses",
        auth_type="external_process",
        base_url_override="acp://copilot",
        base_url_env_var="COPILOT_ACP_BASE_URL",
    ),
    "github-copilot": HermesOverlay(
        transport="openai_chat",
        extra_env_vars=("COPILOT_GITHUB_TOKEN", "GH_TOKEN"),
    ),
    "anthropic": HermesOverlay(
        transport="anthropic_messages",
        extra_env_vars=("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
    ),
    "zai": HermesOverlay(
        transport="openai_chat",
        extra_env_vars=("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"),
        base_url_env_var="GLM_BASE_URL",
    ),
    "kimi-for-coding": HermesOverlay(
        transport="openai_chat",
        base_url_env_var="KIMI_BASE_URL",
    ),
    "stepfun": HermesOverlay(
        transport="openai_chat",
        extra_env_vars=("STEPFUN_API_KEY",),
        base_url_override="https://api.stepfun.ai/step_plan/v1",
        base_url_env_var="STEPFUN_BASE_URL",
    ),
    "minimax": HermesOverlay(
        transport="anthropic_messages",
        base_url_env_var="MINIMAX_BASE_URL",
    ),
    "minimax-oauth": HermesOverlay(
        transport="anthropic_messages",
        auth_type="oauth_external",
        base_url_override="https://api.minimax.io/anthropic",
    ),
    "minimax-cn": HermesOverlay(
        transport="anthropic_messages",
        base_url_env_var="MINIMAX_CN_BASE_URL",
    ),
    "deepseek": HermesOverlay(
        transport="openai_chat",
        base_url_env_var="DEEPSEEK_BASE_URL",
    ),
    "alibaba": HermesOverlay(
        transport="openai_chat",
        base_url_env_var="DASHSCOPE_BASE_URL",
    ),
    "alibaba-coding-plan": HermesOverlay(
        transport="openai_chat",
        base_url_env_var="ALIBABA_CODING_PLAN_BASE_URL",
    ),
    "opencode": HermesOverlay(
        transport="openai_chat",
        is_aggregator=True,
        base_url_env_var="OPENCODE_ZEN_BASE_URL",
    ),
    "opencode-go": HermesOverlay(
        transport="openai_chat",
        is_aggregator=True,
        base_url_env_var="OPENCODE_GO_BASE_URL",
    ),
    "kilo": HermesOverlay(
        transport="openai_chat",
        is_aggregator=True,
        base_url_env_var="KILOCODE_BASE_URL",
    ),
    "huggingface": HermesOverlay(
        transport="openai_chat",
        is_aggregator=True,
        base_url_env_var="HF_BASE_URL",
    ),
    "novita": HermesOverlay(
        transport="openai_chat",
        is_aggregator=True,
        base_url_env_var="NOVITA_BASE_URL",
    ),
    "xai": HermesOverlay(
        transport="codex_responses",
        base_url_override="https://api.x.ai/v1",
        base_url_env_var="XAI_BASE_URL",
    ),
    "nvidia": HermesOverlay(
        transport="openai_chat",
        base_url_override="https://integrate.api.nvidia.com/v1",
        base_url_env_var="NVIDIA_BASE_URL",
    ),
    "xiaomi": HermesOverlay(
        transport="openai_chat",
        base_url_env_var="XIAOMI_BASE_URL",
    ),
    "tencent-tokenhub": HermesOverlay(
        transport="openai_chat",
        base_url_env_var="TOKENHUB_BASE_URL",
    ),
    "arcee": HermesOverlay(
        transport="openai_chat",
        base_url_override="https://api.arcee.ai/api/v1",
        base_url_env_var="ARCEE_BASE_URL",
    ),
    "gmi": HermesOverlay(
        transport="openai_chat",
        extra_env_vars=("GMI_API_KEY",),
        base_url_override="https://api.gmi-serving.com/v1",
        base_url_env_var="GMI_BASE_URL",
    ),
    "ollama-cloud": HermesOverlay(
        transport="openai_chat",
        base_url_override="https://ollama.com/v1",
        base_url_env_var="OLLAMA_BASE_URL",
    ),
    # Azure Foundry：同时支持 OpenAI 风格和 Anthropic 风格的 endpoint。
    # transport 在运行时由 config.yaml 的 model.api_mode 决定。
    "azure-foundry": HermesOverlay(
        transport="openai_chat",  # 默认值；会被 config 中的 api_mode 覆盖
        base_url_env_var="AZURE_FOUNDRY_BASE_URL",
    ),
    "bedrock": HermesOverlay(
        transport="bedrock_converse",
        auth_type="aws_sdk",
    ),
}


# -- 已解析的 provider -------------------------------------------------------
# models.dev + 覆盖层 + 用户配置合并后的结果。

@dataclass
class ProviderDef:
    """完整的 provider 定义 — 从所有来源合并。"""

    id: str
    name: str
    transport: str                        # openai_chat | anthropic_messages | codex_responses
    api_key_env_vars: Tuple[str, ...]     # 用于检查 API key 的所有 env var
    base_url: str = ""
    base_url_env_var: str = ""
    is_aggregator: bool = False
    auth_type: str = "api_key"
    doc: str = ""
    source: str = ""                      # "models.dev"、"hermes"、"user-config"


# -- 别名 ---------------------------------------------------------------------
# 将用户友好 / 旧版名称映射到规范 provider ID。
# 尽可能使用 models.dev ID。

ALIASES: Dict[str, str] = {
    # openrouter
    "openai": "openrouter",     # 裸 "openai" → 通过 aggregator 路由

    # zai
    "glm": "zai",
    "z-ai": "zai",
    "z.ai": "zai",
    "zhipu": "zai",

    # xai
    "x-ai": "xai",
    "x.ai": "xai",
    "grok": "xai",
    "grok-oauth": "xai-oauth",
    "xai-oauth": "xai-oauth",
    "x-ai-oauth": "xai-oauth",
    "xai-grok-oauth": "xai-oauth",

    # nvidia
    "nim": "nvidia",
    "nvidia-nim": "nvidia",
    "build-nvidia": "nvidia",
    "nemotron": "nvidia",

    # kimi-for-coding（models.dev ID）
    "kimi": "kimi-for-coding",
    "kimi-coding": "kimi-for-coding",
    "kimi-coding-cn": "kimi-for-coding",
    "moonshot": "kimi-for-coding",

    # stepfun
    "step": "stepfun",
    "stepfun-coding-plan": "stepfun",

    # minimax-cn
    "minimax-china": "minimax-cn",
    "minimax_cn": "minimax-cn",

    # anthropic
    "claude": "anthropic",
    "claude-code": "anthropic",

    # github-copilot（models.dev ID）
    "copilot": "github-copilot",
    "github": "github-copilot",
    "github-copilot-acp": "copilot-acp",

    # opencode（OpenCode Zen 的 models.dev ID）
    "opencode-zen": "opencode",
    "zen": "opencode",

    # opencode-go
    "go": "opencode-go",
    "opencode-go-sub": "opencode-go",

    # kilo（KiloCode 的 models.dev ID）
    "kilocode": "kilo",
    "kilo-code": "kilo",
    "kilo-gateway": "kilo",

    # deepseek
    "deep-seek": "deepseek",

    # alibaba
    "dashscope": "alibaba",
    "aliyun": "alibaba",
    "qwen": "alibaba",
    "alibaba-cloud": "alibaba",
    "alibaba_coding": "alibaba-coding-plan",
    "alibaba-coding": "alibaba-coding-plan",
    "alibaba_coding_plan": "alibaba-coding-plan",

    # huggingface
    "hf": "huggingface",
    "hugging-face": "huggingface",
    "huggingface-hub": "huggingface",

    # novita
    "novita-ai": "novita",
    "novitaai": "novita",

    # xiaomi
    "mimo": "xiaomi",
    "xiaomi-mimo": "xiaomi",

    # tencent
    "tencent": "tencent-tokenhub",
    "tokenhub": "tencent-tokenhub",
    "tencent-cloud": "tencent-tokenhub",
    "tencentmaas": "tencent-tokenhub",

    # bedrock
    "aws": "bedrock",
    "aws-bedrock": "bedrock",
    "amazon-bedrock": "bedrock",
    "amazon": "bedrock",

    # arcee
    "arcee-ai": "arcee",
    "arceeai": "arcee",

    # gmi
    "gmi-cloud": "gmi",
    "gmicloud": "gmi",

    # 本地 server 别名 → 虚拟 "local" 概念（通过用户配置解析）
    "lmstudio": "lmstudio",
    "lm-studio": "lmstudio",
    "lm_studio": "lmstudio",
    "ollama": "custom",  # 裸 "ollama" = 本地；使用 "ollama-cloud" 访问云端
    "vllm": "local",
    "llamacpp": "local",
    "llama.cpp": "local",
    "llama-cpp": "local",
}


# -- 显示标签 -----------------------------------------------------------------
# 从 models.dev + 覆盖层动态构建。为不在目录中的 provider 提供回退值。

_LABEL_OVERRIDES: Dict[str, str] = {
    "nous": "Nous Portal",
    "openai-codex": "OpenAI Codex",
    "copilot-acp": "GitHub Copilot ACP",
    "stepfun": "StepFun Step Plan",
    "xiaomi": "Xiaomi MiMo",
    "gmi": "GMI Cloud",
    "tencent-tokenhub": "Tencent TokenHub",
    "lmstudio": "LM Studio",
    "local": "Local endpoint",
    "bedrock": "AWS Bedrock",
    "ollama-cloud": "Ollama Cloud",
    "xai-oauth": "xAI Grok OAuth (SuperGrok / Premium+)",
}


# -- Transport → API mode 映射 ------------------------------------------------

TRANSPORT_TO_API_MODE: Dict[str, str] = {
    "openai_chat": "chat_completions",
    "anthropic_messages": "anthropic_messages",
    "codex_responses": "codex_responses",
    "bedrock_converse": "bedrock_converse",
}


# -- 辅助函数 -----------------------------------------------------------------

def normalize_provider(name: str) -> str:
    """将别名和大小写规范化为规范的 provider id。

    返回规范的 id 字符串。*不*验证该 id 是否对应已知的 provider。
    """
    key = name.strip().lower()
    return ALIASES.get(key, key)


def get_provider(name: str) -> Optional[ProviderDef]:
    """通过 id 或别名查找内置 provider。

    解析顺序：
      1. Hermes 覆盖层（用于不在 models.dev 中的 provider：nous、openai-codex 等）
      2. models.dev 目录 + Hermes 覆盖层

    来自 config.yaml 的用户自定义 provider（``providers:`` / ``custom_providers:``）
    由 :func:`resolve_provider_full` 解析，该函数在此基础上叠加了
    ``resolve_user_provider`` 和 ``resolve_custom_provider``。需要
    用户配置支持的调用者应改用 ``resolve_provider_full``。

    返回完全解析的 ProviderDef，未找到则返回 None。
    """
    canonical = normalize_provider(name)

    # 尝试获取 models.dev 数据
    try:
        from agent.models_dev import get_provider_info as _mdev_provider
        mdev_info = _mdev_provider(canonical)
    except Exception:
        mdev_info = None

    overlay = HERMES_OVERLAYS.get(canonical)

    if mdev_info is not None:
        # 合并 models.dev + 覆盖层
        transport = overlay.transport if overlay else "openai_chat"
        is_agg = overlay.is_aggregator if overlay else False
        auth = overlay.auth_type if overlay else "api_key"
        base_url_env = overlay.base_url_env_var if overlay else ""
        base_url_override = overlay.base_url_override if overlay else ""

        # 合并 env var：models.dev env + hermes 额外项
        env_vars = list(mdev_info.env)
        if overlay and overlay.extra_env_vars:
            for ev in overlay.extra_env_vars:
                if ev not in env_vars:
                    env_vars.append(ev)

        return ProviderDef(
            id=canonical,
            name=mdev_info.name,
            transport=transport,
            api_key_env_vars=tuple(env_vars),
            base_url=base_url_override or mdev_info.api,
            base_url_env_var=base_url_env,
            is_aggregator=is_agg,
            auth_type=auth,
            doc=mdev_info.doc,
            source="models.dev",
        )

    if overlay is not None:
        # 仅 Hermes 的 provider（不在 models.dev 中）
        return ProviderDef(
            id=canonical,
            name=_LABEL_OVERRIDES.get(canonical, canonical),
            transport=overlay.transport,
            api_key_env_vars=overlay.extra_env_vars,
            base_url=overlay.base_url_override,
            base_url_env_var=overlay.base_url_env_var,
            is_aggregator=overlay.is_aggregator,
            auth_type=overlay.auth_type,
            source="hermes",
        )

    return None


def get_label(provider_id: str) -> str:
    """获取 provider 的可读显示名称。"""
    canonical = normalize_provider(provider_id)

    # 优先检查 label 覆盖
    if canonical in _LABEL_OVERRIDES:
        return _LABEL_OVERRIDES[canonical]

    # 尝试 models.dev
    pdef = get_provider(canonical)
    if pdef:
        return pdef.name

    return canonical




def is_aggregator(provider: str) -> bool:
    """当 provider 是多模型 aggregator 时返回 True。"""
    provider_norm = normalize_provider(provider or "")
    if provider_norm.startswith("custom:"):
        return True
    pdef = get_provider(provider_norm)
    return pdef.is_aggregator if pdef else False


# 扁平命名空间的转售商（例如 opencode-go、opencode-zen）被标记为
# ``is_aggregator=True``，因为它们的实时 ``/v1/models`` 返回的是裸 model
# ID（"deepseek-v4-flash"），而不是 ``vendor/model`` 格式的路由 slug —
# model-switch 解析器依赖该标志来搜索它们的扁平目录
# （参见 model_switch.py 步骤 d）。但它们并非路由 aggregator：它们列出的
# 每个 model 都是在自己的订阅下提供的一方 model，而不是指向其他 provider
# endpoint 的透传路由。picker 去重（build_models_payload）必须将它们与
# OpenRouter 这样的真正路由器区分对待 — 转售商的一方 "minimax-m3" 绝不能
# 因为用户的自定义代理也恰好提供同名 model 就被剔除。
_FLAT_NAMESPACE_RESELLERS: frozenset[str] = frozenset({
    # 使用规范化的 provider ID：normalize_provider("opencode-zen") -> "opencode"。
    "opencode-go",
    "opencode",
})


def is_routing_aggregator(provider: str) -> bool:
    """仅对真正的路由 aggregator（例如 OpenRouter、名为
    ``custom:*`` 的代理）返回 True — 这些 aggregator 将裸 / vendor-slug
    格式的 model 名称路由到*其他* provider 的 endpoint。

    不同于 :func:`is_aggregator`，后者对扁平命名空间转售商
    （opencode-go/zen）也返回 True，而这些转售商的目录完全是
    一方的。当需要判断"选择此 model 是否会默默将调用重新路由到
    用户意图 provider 之外的地方？"时，使用此检查 — 即 picker
    去重逻辑。转售商的答案为否：它们列出的 model 是它们自己的，
    因此它们的行不能针对用户代理进行去重。
    """
    provider_norm = normalize_provider(provider or "")
    if provider_norm in _FLAT_NAMESPACE_RESELLERS:
        return False
    return is_aggregator(provider_norm)


def determine_api_mode(provider: str, base_url: str = "") -> str:
    """确定 provider/endpoint 的 API 模式（线路协议）。

    解析顺序：
      1. 已知 provider → transport → TRANSPORT_TO_API_MODE。
      2. 未知 / 自定义 provider 的 URL 启发式判断。
      3. 默认值：'chat_completions'。
    """
    pdef = get_provider(provider)
    if pdef is not None:
        # 即使对于已知 provider，也要检查特殊 endpoint 的 URL 启发式判断
        # （例如 kimi /coding endpoint 即使在 'custom' 上也需要 anthropic_messages）
        if base_url:
            url_lower = base_url.rstrip("/").lower()
            if "api.kimi.com/coding" in url_lower:
                return "anthropic_messages"
            if url_lower.endswith("/anthropic") or "api.anthropic.com" in url_lower:
                return "anthropic_messages"
            if "api.openai.com" in url_lower:
                return "codex_responses"
        return TRANSPORT_TO_API_MODE.get(pdef.transport, "chat_completions")

    # 对不在 HERMES_OVERLAYS 中的 provider 进行直接 provider 检查
    if provider == "bedrock":
        return "bedrock_converse"

    # 基于 URL 的启发式判断，用于自定义 / 未知 provider
    if base_url:
        url_lower = base_url.rstrip("/").lower()
        hostname = base_url_hostname(base_url)
        if url_lower.endswith("/anthropic") or hostname == "api.anthropic.com":
            return "anthropic_messages"
        if hostname == "api.kimi.com" and "/coding" in url_lower:
            return "anthropic_messages"
        if hostname == "api.openai.com":
            return "codex_responses"
        if hostname.startswith("bedrock-runtime.") and base_url_host_matches(base_url, "amazonaws.com"):
            return "bedrock_converse"

    return "chat_completions"


# -- 来自用户配置的 Provider ------------------------------------------------

def resolve_user_provider(name: str, user_config: Dict[str, Any]) -> Optional[ProviderDef]:
    """从用户的 config.yaml ``providers:`` 部分解析 provider。

    Args:
        name: 用户提供的 Provider 名称。
        user_config: config.yaml 中的 ``providers:`` 字典。

    Returns:
        找到则返回 ProviderDef，否则返回 None。
    """
    if not user_config or not isinstance(user_config, dict):
        return None

    entry = user_config.get(name)
    if not isinstance(entry, dict):
        return None

    # 提取字段
    display_name = entry.get("name", "") or name
    api_url = entry.get("api", "") or entry.get("url", "") or entry.get("base_url", "") or ""
    key_env = entry.get("key_env", "") or ""
    transport = entry.get("transport", "openai_chat") or "openai_chat"

    env_vars: List[str] = []
    if key_env:
        env_vars.append(key_env)

    return ProviderDef(
        id=name,
        name=display_name,
        transport=transport,
        api_key_env_vars=tuple(env_vars),
        base_url=api_url,
        is_aggregator=False,
        auth_type="api_key",
        source="user-config",
    )


def custom_provider_slug(display_name: str) -> str:
    """为 custom_providers 条目构建规范 slug。

    与 runtime_provider 和 credential_pool 使用的约定一致
    （``custom:<normalized-name>``）。集中在此处，以便所有调用点
    生成相同的 slug。
    """
    return "custom:" + display_name.strip().lower().replace(" ", "-")


def resolve_custom_provider(
    name: str,
    custom_providers: Optional[List[Dict[str, Any]]],
) -> Optional[ProviderDef]:
    """从用户的 config.yaml ``custom_providers`` 列表中解析 provider。"""
    if not custom_providers or not isinstance(custom_providers, list):
        return None

    requested = (name or "").strip().lower()
    if not requested:
        return None

    # 如果存储的 provider 是裸字符串 "custom"（先前 model-switch bug 导致的
    # 损坏状态），回退到第一个自定义 provider 条目，以便现有配置自动修复。
    # (GH #17478)
    bare_custom_fallback = requested == "custom"
    first_valid = None

    for entry in custom_providers:
        if not isinstance(entry, dict):
            continue

        display_name = (entry.get("name") or "").strip()
        api_url = (
            entry.get("base_url", "")
            or entry.get("url", "")
            or entry.get("api", "")
            or ""
        ).strip()
        if not display_name or not api_url:
            continue

        # 暂存第一个有效条目，用于裸 "custom" 回退
        if first_valid is None:
            first_valid = (display_name, api_url)

        slug = custom_provider_slug(display_name)
        if requested not in {display_name.lower(), slug}:
            continue

        return ProviderDef(
            id=slug,
            name=display_name,
            transport="openai_chat",
            api_key_env_vars=(),
            base_url=api_url,
            is_aggregator=False,
            auth_type="api_key",
            source="user-config",
        )

    # 自修复：裸 "custom" 未匹配到任何条目 — 返回第一个有效条目
    if bare_custom_fallback and first_valid:
        dname, aurl = first_valid
        slug = custom_provider_slug(dname)
        return ProviderDef(
            id=slug,
            name=dname,
            transport="openai_chat",
            api_key_env_vars=(),
            base_url=aurl,
            is_aggregator=False,
            auth_type="api_key",
            source="user-config",
        )

    return None


def resolve_provider_full(
    name: str,
    user_providers: Optional[Dict[str, Any]] = None,
    custom_providers: Optional[List[Dict[str, Any]]] = None,
) -> Optional[ProviderDef]:
    """完整解析链：内置 → models.dev → 用户配置。

    这是 --provider 标志解析的主入口点。

    Args:
        name: Provider 名称或别名。
        user_providers: config.yaml 中的 ``providers:`` 字典（可选）。
        custom_providers: config.yaml 中的 ``custom_providers:`` 列表（可选）。

    Returns:
        找到则返回 ProviderDef，否则返回 None。
    """
    canonical = normalize_provider(name)
    raw = name.strip().lower()

    # 0. User-defined config providers win over the built-in alias table.
    #    A user who declares ``providers.<name>`` in config.yaml has stated
    #    explicit intent for that name — it must not be hijacked by a legacy
    #    vendor alias (e.g. bare "openai" → "openrouter"). Resolve the raw
    #    name against user config FIRST so a configured ``providers.openai``
    #    (pointing at api.openai.com) beats the alias that would otherwise
    #    silently route to OpenRouter. Only the raw (pre-alias) name is tried
    #    here; canonical/alias resolution still happens below.
    if user_providers:
        user_pdef = resolve_user_provider(raw, user_providers)
        if user_pdef is not None:
            return user_pdef

    # 1. Built-in (models.dev + overlays)
    pdef = get_provider(canonical)
    if pdef is not None:
        return pdef

    # 2. User-defined providers from config
    if user_providers:
        # Try canonical name
        user_pdef = resolve_user_provider(canonical, user_providers)
        if user_pdef is not None:
            return user_pdef
        # Try original name (in case alias didn't match)
        user_pdef = resolve_user_provider(raw, user_providers)
        if user_pdef is not None:
            return user_pdef

    # 2b. Saved custom providers from config
    custom_pdef = resolve_custom_provider(name, custom_providers)
    if custom_pdef is not None:
        return custom_pdef

    # 3. Try models.dev directly (for providers not in our ALIASES)
    try:
        from agent.models_dev import get_provider_info as _mdev_provider
        mdev_info = _mdev_provider(canonical)
        if mdev_info is not None:
            return ProviderDef(
                id=canonical,
                name=mdev_info.name,
                transport="openai_chat",
                api_key_env_vars=mdev_info.env,
                base_url=mdev_info.api,
                source="models.dev",
            )
    except Exception:
        pass

    return None
