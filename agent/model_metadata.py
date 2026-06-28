"""模型元数据、上下文长度和令牌估算工具。

纯实用函数,无 AIAgent 依赖。由 ContextCompressor 和 run_agent.py
用于飞行前上下文检查。
"""

import ipaddress
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
import yaml

from utils import atomic_json_write, base_url_host_matches, base_url_hostname

from hermes_constants import OPENROUTER_MODELS_URL

logger = logging.getLogger(__name__)


def _resolve_requests_verify() -> bool | str:
    """从环境变量解析 `requests` 调用的 SSL 验证设置。

    `requests` 库默认只遵守 REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE。
    Hermes 也遵守 HERMES_CA_BUNDLE(自己的约定)和 SSL_CERT_FILE(由 stdlib
    `ssl` 模块和 httpx 使用),因此单个环境变量可以覆盖同一进程中的
    `requests` 和 httpx 调用点。

    返回文件系统路径到 CA 包,或 True 以延迟到 requests 默认值(certifi)。
    """
    for env_var in ("HERMES_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        val = os.getenv(env_var)
        if val and os.path.isfile(val):
            return val
    return True

# 可以作为模型 ID 的 "provider:" 前缀出现的提供商名称。
# 只剥离这些 — Ollama 风格的 "model:tag" 冒号(例如 "qwen3.5:27b")
# 被保留,以便完整模型名称到达缓存查找和服务器查询。
_PROVIDER_PREFIXES: frozenset[str] = frozenset({
    "openrouter", "nous", "openai-codex", "copilot", "copilot-acp",
    "gemini", "ollama-cloud", "zai", "kimi-coding", "kimi-coding-cn", "stepfun", "minimax", "minimax-oauth", "minimax-cn", "anthropic", "deepseek",
    "opencode-zen", "opencode-go", "kilocode", "alibaba", "novita",
    "qwen-oauth",
    "xiaomi",
    "arcee",
    "gmi",
    "tencent-tokenhub",
    "custom", "local",
    # 常见别名
    "google", "google-gemini", "google-ai-studio",
    "glm", "z-ai", "z.ai", "zhipu", "github", "github-copilot",
    "github-models", "kimi", "moonshot", "kimi-cn", "moonshot-cn", "claude", "deep-seek",
    "ollama",
    "stepfun", "opencode", "zen", "go", "kilo", "dashscope", "aliyun", "qwen",
    "mimo", "xiaomi-mimo",
    "tencent", "tokenhub", "tencent-cloud", "tencentmaas",
    "arcee-ai", "arceeai",
    "gmi-cloud", "gmicloud",
    "xai", "x-ai", "x.ai", "grok",
    "nvidia", "nim", "nvidia-nim", "nemotron",
    "qwen-portal", "novita-ai", "novitai",
})


_OLLAMA_TAG_PATTERN = re.compile(
    r"^(\d+\.?\d*b|latest|stable|q\d|fp?\d|instruct|chat|coder|vision|text)",
    re.IGNORECASE,
)


# Tailscale 的 CGNAT 范围(RFC 6598)。`ipaddress.is_private` 排除此块,
# 因此如果没有显式检查,通过 Tailscale(例如 `http://100.77.243.5:11434`)
# 到达的 Ollama 不会被视为本地,其流读取/陈旧超时不会自动提升。
# 在导入时构建一次。
_TAILSCALE_CGNAT = ipaddress.IPv4Network("100.64.0.0/10")


def _strip_provider_prefix(model: str) -> str:
    """从模型字符串中剥离已识别的提供商前缀。

    ``"local:my-model"`` → ``"my-model"``
    ``"qwen3.5:27b"``   → ``"qwen3.5:27b"``  (不变 — 不是提供商前缀)
    ``"qwen:0.5b"``     → ``"qwen:0.5b"``    (不变 — Ollama model:tag)
    ``"deepseek:latest"``→ ``"deepseek:latest"``(不变 — Ollama model:tag)
    """
    if ":" not in model or model.startswith("http"):
        return model
    prefix, suffix = model.split(":", 1)
    prefix_lower = prefix.strip().lower()
    if prefix_lower in _PROVIDER_PREFIXES:
        # 如果后缀看起来像 Ollama 标签(例如 "7b"、"latest"、"q4_0")则不剥离
        if _OLLAMA_TAG_PATTERN.match(suffix.strip()):
            return model
        return suffix
    return model

_model_metadata_cache: Dict[str, Dict[str, Any]] = {}
_model_metadata_cache_time: float = 0
_novita_metadata_cache: Dict[str, Dict[str, Any]] = {}
_novita_metadata_cache_time: float = 0
_MODEL_CACHE_TTL = 3600
_endpoint_model_metadata_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
_endpoint_model_metadata_cache_time: Dict[str, float] = {}
_ENDPOINT_MODEL_CACHE_TTL = 300


def _get_model_metadata_cache_path() -> Path:
    """返回 OpenRouter 模型元数据磁盘缓存的路径。"""
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "cache" / "openrouter_model_metadata.json"


def _model_metadata_disk_cache_age_seconds() -> Optional[float]:
    """返回磁盘缓存年龄(秒),或如果新鲜度未知则返回 None。"""
    try:
        cache_path = _get_model_metadata_cache_path()
        if not cache_path.exists():
            return None
        age = time.time() - cache_path.stat().st_mtime
        if age < 0:
            return None
        return age
    except Exception:
        return None


def _load_model_metadata_disk_cache() -> Dict[str, Dict[str, Any]]:
    """从磁盘加载已处理的 OpenRouter 元数据缓存。"""
    try:
        cache_path = _get_model_metadata_cache_path()
        with cache_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {
            str(key): value
            for key, value in data.items()
            if isinstance(value, dict)
        }
    except Exception as e:
        logger.debug("Failed to load OpenRouter model metadata disk cache: %s", e)
        return {}


def _save_model_metadata_disk_cache(data: Dict[str, Dict[str, Any]]) -> None:
    """将已处理的 OpenRouter 元数据缓存原子性地保存到磁盘。"""
    try:
        atomic_json_write(
            _get_model_metadata_cache_path(),
            data,
            indent=0,
            separators=(",", ":"),
        )
    except Exception as e:
        logger.debug("Failed to save OpenRouter model metadata disk cache: %s", e)

# 模型未知时的上下文长度探测降级档。
# 我们从 256K 开始(覆盖 GPT-5.x、许多当前的大上下文模型),
# 并在上下文长度错误时逐步下降,直到找到有效的档位。
# Tier[0] 也是没有任何检测方法成功时的默认回退值。
CONTEXT_PROBE_TIERS = [
    256_000,
    128_000,
    64_000,
    32_000,
    16_000,
    8_000,
]

# 没有任何检测方法成功时的默认上下文长度。
DEFAULT_FALLBACK_CONTEXT = CONTEXT_PROBE_TIERS[0]

# 运行 Hermes Agent 所需的最小上下文长度。令牌较少的模型无法维持足够的
# 工作调用内存。会话、模型切换和定时任务应拒绝低于此值的模型。
MINIMUM_CONTEXT_LENGTH = 64_000

# 简薄的回退默认值 — 仅宽泛的模型家族模式。
# 这些仅在提供商未知且 models.dev/OpenRouter/Anthropic 全部丢失时触发。
# 替换了之前的 80+ 条目字典。
# 对于提供商特定的上下文长度,models.dev 是主要来源。
DEFAULT_CONTEXT_LENGTHS = {
    # Anthropic Claude 4.6 (1M 上下文) — 仅裸 ID,以避免模糊匹配冲突
    # (例如 "anthropic/claude-sonnet-4" 是 "anthropic/claude-sonnet-4.6" 的子字符串)。
    # OpenRouter 前缀的模型通过 OpenRouter 实时 API 或 models.dev 解析。
    "claude-fable-5": 1000000,
    "claude-fable": 1000000,
    "claude-opus-4-8": 1000000,
    "claude-opus-4.8": 1000000,
    "claude-opus-4-7": 1000000,
    "claude-opus-4.7": 1000000,
    "claude-opus-4-6": 1000000,
    "claude-sonnet-4-6": 1000000,
    "claude-opus-4.6": 1000000,
    "claude-sonnet-4.6": 1000000,
    # 较旧 Claude 模型的兜底(必须在特定条目之后排序)
    "claude": 200000,
    # OpenAI — GPT-5 系列(大多数为 400k;特定覆盖优先)
    # 来源: https://developers.openai.com/api/docs/models
    # GPT-5.5(2026 年 4 月 23 日发布)在直接 OpenAI API 上为 1.05M,
    # ChatGPT Codex OAuth 将其限制为 272K;两条路径都通过其自己的
    # 提供商感知分支(_resolve_codex_oauth_context_length + models.dev)解析。
    # 此硬编码值仅在所有探测丢失时到达。
    "gpt-5.5": 1050000,
    "gpt-5.4-nano": 400000,           # 400k(非完整的 5.4 的 1.05M)
    "gpt-5.4-mini": 400000,           # 400k(非完整的 5.4 的 1.05M)
    "gpt-5.4": 1050000,               # GPT-5.4, GPT-5.4 Pro (1.05M 上下文)
    # gpt-5.3-codex-spark 仅限 Codex-OAuth(ChatGPT Pro 权益),
    # 使用比其他 gpt-5.x 变体更小的 128k 窗口。在此作为防御性覆盖列出,
    # 因此最长子字符串回退不会匹配下面的通用 "gpt-5" 条目(400k)
    # 并在 Spark 的上下文需要通过此路径解析时报告错误的限制。
    # 真实使用流经第 ~1113 行的 _CODEX_OAUTH_CONTEXT_FALLBACK。
    "gpt-5.3-codex-spark": 128000,
    "gpt-5.1-chat": 128000,           # Chat 变体具有 128k 上下文
    "gpt-5": 400000,                  # GPT-5.x 基础、mini、codex 变体(400k)
    "gpt-4.1": 1047576,
    "gpt-4": 128000,
    # Google
    "gemini": 1048576,
    # Gemma(通过 AI Studio 提供的开源模型)
    "gemma-4": 256000,  # Gemma 4 系列
    "gemma4": 256000,  # Ollama 风格命名(例如 gemma4:31b-cloud)
    "gemma-4-31b": 256000,
    "gemma-3": 131072,
    "gemma": 8192,  # 较旧 gemma 模型的回退
    # DeepSeek — V4 系列配备 1M 上下文窗口。传统别名
    # ``deepseek-chat`` / ``deepseek-reasoner`` 在服务器端映射到
    # ``deepseek-v4-flash`` 的非思考/思考模式,并继承相同的 1M 窗口。
    # 下面的 ``deepseek`` 子字符串条目保留作为较旧/未知 DeepSeek 模型
    # id 的 128K 回退(例如通过自定义端点)。
    # https://api-docs.deepseek.com/zh-cn/quick_start/pricing
    "deepseek-v4-pro": 1_000_000,
    "deepseek-v4-flash": 1_000_000,
    "deepseek-chat": 1_000_000,
    "deepseek-reasoner": 1_000_000,
    "deepseek": 128000,
    # Meta
    "llama": 131072,
    # Qwen — 特定模型系列优先于兜底。
    # 官方文档: https://help.aliyun.com/zh/model-studio/developer-reference/
    "qwen3.6-plus": 1048576,      # 1M 上下文(DashScope/Alibaba & OpenRouter)
    "qwen3-coder-plus": 1000000,  # 1M 上下文
    "qwen3-coder": 262144,        # 256K 上下文
    "qwen": 131072,
    # MiniMax — M3 为 1M 上下文(最大输出 512K);M2.x 系列为 204,800。
    # 键使用子字符串匹配(最长优先),因此 "minimax-m3" 在每个表面上
    # 都胜过通用的 "minimax" 兜底(M3 原生 MiniMax-M3、OpenRouter/Nous
    # minimax/minimax-m3)。
    # https://platform.minimax.io/docs/api-reference/text-chat-openai
    "minimax-m3": 1000000,
    "minimax": 204800,
    # GLM — GLM-5.2 配备 1M 上下文窗口(经验证:
    # 在 789K 提示令牌时的干草堆检索成功,零错误)。
    # 较旧的 GLM 模型(5、5.1、5-turbo)约为 202K。最长键优先子字符串匹配
    # 确保 "glm-5.2" 解析为 1M,而较旧变体仍命中通用 202K 回退。
    "glm-5.2": 1_048_576,
    "glm": 202752,
    # xAI Grok — xAI /v1/models 不返回 context_length 元数据,
    # 因此这些硬编码回退防止 Hermes 在用户通过自定义提供商指向
    # https://api.x.ai/v1 时探测下降到默认的 128k。
    # 值来源于 models.dev(2026-04)。
    # 键使用子字符串匹配(最长优先),因此例如 "grok-4.20"
    # 匹配 "grok-4.20-0309-reasoning" / "-non-reasoning" / "-multi-agent-0309"。
    # OAuth-only 标识; absent from GET /v1/models。xAI 为 Grok Build 上的
    # Composer 2.5 发布了 200k 可用上下文窗口;/v1/responses 额外强制执行
    # ~262144 输入+输出预算,但可用上下文(我们在此跟踪的)是 200k。
    "grok-composer": 200000,    # grok-composer-2.5-fast (Grok Build CLI)
    "grok-build": 256000,       # grok-build-0.1
    "grok-code-fast": 256000,   # grok-code-fast-1
    "grok-2-vision": 8192,      # grok-2-vision, -1212, -latest
    "grok-4-fast": 2000000,     # grok-4-fast-(non-)reasoning, 也匹配 -reasoning
    "grok-4.20": 2000000,       # grok-4.20-0309-(non-)reasoning, -multi-agent-0309
    "grok-4.3": 1000000,        # grok-4.3, grok-4.3-latest — 根据 docs.x.ai 为 1M 上下文
    "grok-4": 256000,           # grok-4, grok-4-0709
    "grok-3": 131072,           # grok-3, grok-3-mini, grok-3-fast, grok-3-mini-fast
    "grok-2": 131072,           # grok-2, grok-2-1212, grok-2-latest
    "grok": 131072,             # 兜底(grok-beta, 未知 grok-*)
    # Kimi
    "kimi": 262144,
    # Tencent — Hy3 Preview(混元)配备 256K 上下文窗口。
    # OpenRouter 实时元数据报告 262144 (256 × 1024);对齐静态回退,
    # 使缓存和离线都同意(问题 #22268)。
    "hy3-preview": 262144,
    # Nemotron — NVIDIA 的开源权重系列(所有尺寸均为 128K 上下文)
    "nemotron": 131072,
    # Arcee
    "trinity": 262144,
    # OpenRouter
    "elephant": 262144,
    # Hugging Face 推理提供商 — 模型 ID 使用 org/name 格式
    "Qwen/Qwen3.5-397B-A17B": 131072,
    "Qwen/Qwen3.5-35B-A3B": 131072,
    "deepseek-ai/DeepSeek-V3.2": 65536,
    "moonshotai/Kimi-K2.5": 262144,
    "moonshotai/Kimi-K2.6": 262144,
    "moonshotai/Kimi-K2-Thinking": 262144,
    "MiniMaxAI/MiniMax-M2.5": 204800,
    "XiaomiMiMo/MiMo-V2-Flash": 262144,
    "mimo-v2-pro": 1048576,
    "mimo-v2.5-pro": 1048576,
    "mimo-v2.5": 1048576,
    "mimo-v2-omni": 262144,
    "mimo-v2-flash": 262144,
    "zai-org/GLM-5": 202752,
}

# xAI Grok 模型在 api.x.ai 上接受 `reasoning.effort` 参数。
# 2026-05-10 针对实时 /v1/responses 验证:
#
#   接受 effort:  grok-3-mini, grok-3-mini-fast, grok-4.20-multi-agent-0309,
#                    grok-4.3
#   拒绝 effort:  grok-3, grok-4, grok-4-0709, grok-4-fast-(non-)reasoning,
#                    grok-4-1-fast-(non-)reasoning, grok-4.20-0309-(non-)reasoning,
#                    grok-code-fast-1
#
# 拒绝侧模型仍然原生推理 — 它们只是不公开努力拨号 —
# 因此调用方应完全不发送 `reasoning` 键,而不是默认的 `medium`
# (这会 400 并报错"Model X does not support parameter reasoningEffort")。
_GROK_EFFORT_CAPABLE_PREFIXES = (
    "grok-3-mini",
    "grok-4.20-multi-agent",
    "grok-4.3",
)


def grok_supports_reasoning_effort(model: str) -> bool:
    """当 xAI Grok 模型接受 ``reasoning.effort`` 时返回 True。

    通过子字符串允许列表(匹配裸 ``grok-3-mini`` 和
    聚合器前缀的 ``x-ai/grok-3-mini``)。设计上保守:
    如果未来的 Grok 模型未列出,我们不发送努力拨号而不是 400。
    """
    name = (model or "").strip().lower()
    if not name:
        return False
    # 剥离常见聚合器前缀(x-ai/, openrouter/x-ai/, xai/, ...)
    for sep in ("/",):
        if sep in name:
            name = name.rsplit(sep, 1)[-1]
    return any(name.startswith(prefix) for prefix in _GROK_EFFORT_CAPABLE_PREFIXES)


_CONTEXT_LENGTH_KEYS = (
    "context_length",
    "context_window",
    "context_size",
    "max_context_length",
    "max_position_embeddings",
    "max_model_len",
    "max_input_tokens",
    "max_sequence_length",
    "max_seq_len",
    "n_ctx_train",
    "n_ctx",
    "ctx_size",
)

_MAX_COMPLETION_KEYS = (
    "max_completion_tokens",
    "max_output_tokens",
    "max_tokens",
)

# 本地服务器主机名/地址模式
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
# 解析为主机的 Docker/Podman/Lima DNS 名称
_CONTAINER_LOCAL_SUFFIXES = (
    ".docker.internal",
    ".containers.internal",
    ".lima.internal",
)


def _normalize_base_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


def _auth_headers(api_key: str = "") -> Dict[str, str]:
    token = str(api_key or "").strip()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def _is_openrouter_base_url(base_url: str) -> bool:
    return base_url_host_matches(base_url, "openrouter.ai")


def _is_custom_endpoint(base_url: str) -> bool:
    normalized = _normalize_base_url(base_url)
    return bool(normalized) and not _is_openrouter_base_url(normalized)


_URL_TO_PROVIDER: Dict[str, str] = {
    "api.openai.com": "openai",
    "chatgpt.com": "openai",
    "api.anthropic.com": "anthropic",
    "api.z.ai": "zai",
    "open.bigmodel.cn": "zai",
    "api.moonshot.ai": "kimi-coding",
    "api.moonshot.cn": "kimi-coding-cn",
    "api.kimi.com": "kimi-coding",
    "api.stepfun.ai": "stepfun",
    "api.stepfun.com": "stepfun",
    "api.arcee.ai": "arcee",
    "api.minimax": "minimax",
    "dashscope.aliyuncs.com": "alibaba",
    "dashscope-intl.aliyuncs.com": "alibaba",
    "portal.qwen.ai": "qwen-oauth",
    "openrouter.ai": "openrouter",
    "generativelanguage.googleapis.com": "gemini",
    "inference-api.nousresearch.com": "nous",
    "api.deepseek.com": "deepseek",
    "api.githubcopilot.com": "copilot",
    "models.github.ai": "copilot",
    # GitHub Models 免费层(Azure 托管的原型端点) — 与 Copilot API
    # 相同的规范提供商。硬性每请求令牌上限(通常 8K)使其对 Hermes
    # 的系统提示不可用,但在此映射它使我们能够识别端点并发出
    # 有针对性的提示,而不是落入未知自定义端点路径。
    "models.inference.ai.azure.com": "copilot",
    "api.fireworks.ai": "fireworks",
    "opencode.ai": "opencode-go",
    "api.x.ai": "xai",
    "integrate.api.nvidia.com": "nvidia",
    "api.xiaomimimo.com": "xiaomi",
    "xiaomimimo.com": "xiaomi",
    "api.gmi-serving.com": "gmi",
    "api.novita.ai": "novita",
    "tokenhub.tencentmaas.com": "tencent-tokenhub",
    "ollama.com": "ollama-cloud",
}

# 从提供商配置文件自动扩展。
# 任何 base_url 尚未在映射中的提供商都会自动添加。
try:
    from providers import list_providers as _list_providers
    for _pp in _list_providers():
        _host = _pp.get_hostname()
        if _host and _host not in _URL_TO_PROVIDER:
            _URL_TO_PROVIDER[_host] = _pp.name
except Exception:
    pass


def _infer_provider_from_url(base_url: str) -> Optional[str]:
    """从 base URL 推断 models.dev 提供商名称。

    这允许通过 models.dev 为自定义端点(例如 DashScope (Alibaba)、Z.AI、
    Kimi 等)解析上下文长度,而无需用户在配置中显式设置提供商名称。
    """
    normalized = _normalize_base_url(base_url)
    if not normalized:
        return None
    parsed = urlparse(normalized if "://" in normalized else f"https://{normalized}")
    host = parsed.netloc.lower() or parsed.path.lower()
    for url_part, provider in _URL_TO_PROVIDER.items():
        if url_part in host:
            return provider
    return None


def _is_known_provider_base_url(base_url: str) -> bool:
    return _infer_provider_from_url(base_url) is not None


def is_local_endpoint(base_url: str) -> bool:
    """当 base_url 指向本地机器时返回 True。

    识别环回(`localhost`、`127.0.0.0/8`、`::1`)、容器内部 DNS 名称
    (`host.docker.internal` 等)、RFC-1918 私有范围(`10/8`、`172.16/12`、
    `192.168/16`)、链路本地和 Tailscale CGNAT(`100.64.0.0/10`)。
    包含 Tailscale CGNAT,以便通过 Tailscale 网格到达的远程但可信的
    Ollama 盒子获得与 localhost Ollama 相同的超时自动提升。
    """
    normalized = _normalize_base_url(base_url)
    if not normalized:
        return False
    url = normalized if "://" in normalized else f"http://{normalized}"
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
    except Exception:
        return False
    if host in _LOCAL_HOSTS:
        return True
    # Docker / Podman / Lima 内部 DNS 名称(例如 host.docker.internal)
    if any(host.endswith(suffix) for suffix in _CONTAINER_LOCAL_SUFFIXES):
        return True
    # 不限定的主机名(无点)按定义是本地的 — Docker Compose 服务名称、
    # /etc/hosts 条目或 mDNS 名称。
    if host and "." not in host:
        return True
    # RFC-1918 私有范围、链路本地和 Tailscale CGNAT
    try:
        addr = ipaddress.ip_address(host)
        if addr.is_private or addr.is_loopback or addr.is_link_local:
            return True
        if isinstance(addr, ipaddress.IPv4Address) and addr in _TAILSCALE_CGNAT:
            return True
    except ValueError:
        pass
    # 看起来像私有范围的裸 IP(例如 WSL 的 172.26.x.x)
    # 或 Tailscale CGNAT (100.64.x.x–100.127.x.x)。
    parts = host.split(".")
    if len(parts) == 4:
        try:
            first, second = int(parts[0]), int(parts[1])
            if first == 10:
                return True
            if first == 172 and 16 <= second <= 31:
                return True
            if first == 192 and second == 168:
                return True
            if first == 100 and 64 <= second <= 127:
                return True
        except ValueError:
            pass
    return False


def detect_local_server_type(base_url: str, api_key: str = "") -> Optional[str]:
    """通过探测已知端点检测 base_url 处运行的是哪个本地服务器。

    返回以下之一: "ollama"、"lm-studio"、"vllm"、"llamacpp" 或 None。
    """
    import httpx

    normalized = _normalize_base_url(base_url)
    server_url = normalized
    if server_url.endswith("/v1"):
        server_url = server_url[:-3]

    headers = _auth_headers(api_key)

    try:
        with httpx.Client(timeout=2.0, headers=headers) as client:
            # LM Studio 暴露 /api/v1/models — 首先检查(最具体)
            try:
                r = client.get(f"{server_url}/api/v1/models")
                if r.status_code == 200:
                    return "lm-studio"
            except Exception:
                pass
            # Ollama 暴露 /api/tags 并响应 {"models": [...]}
            # LM Studio 在此路径返回 {"error": "Unexpected endpoint"} 状态码 200,
            # 因此我们必须验证响应包含 "models"。
            try:
                r = client.get(f"{server_url}/api/tags")
                if r.status_code == 200:
                    try:
                        data = r.json()
                        if "models" in data:
                            return "ollama"
                    except Exception:
                        pass
            except Exception:
                pass
            # llama.cpp 暴露 /v1/props(较旧构建使用不带 /v1 前缀的 /props)
            try:
                r = client.get(f"{server_url}/v1/props")
                if r.status_code != 200:
                    r = client.get(f"{server_url}/props")  # 较旧构建的回退
                if r.status_code == 200 and "default_generation_settings" in r.text:
                    return "llamacpp"
            except Exception:
                pass
            # vLLM: /version
            try:
                r = client.get(f"{server_url}/version")
                if r.status_code == 200:
                    data = r.json()
                    if "version" in data:
                        return "vllm"
            except Exception:
                pass
    except Exception:
        pass

    return None


def _iter_nested_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _iter_nested_dicts(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_nested_dicts(item)


def _coerce_reasonable_int(value: Any, minimum: int = 1024, maximum: int = 10_000_000) -> Optional[int]:
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, str):
            value = value.strip().replace(",", "")
        result = int(value)
    except (TypeError, ValueError):
        return None
    if minimum <= result <= maximum:
        return result
    return None


def _extract_first_int(payload: Dict[str, Any], keys: tuple[str, ...]) -> Optional[int]:
    keyset = {key.lower() for key in keys}
    for mapping in _iter_nested_dicts(payload):
        for key, value in mapping.items():
            if str(key).lower() not in keyset:
                continue
            coerced = _coerce_reasonable_int(value)
            if coerced is not None:
                return coerced
    return None


def _extract_context_length(payload: Dict[str, Any]) -> Optional[int]:
    return _extract_first_int(payload, _CONTEXT_LENGTH_KEYS)


def _extract_max_completion_tokens(payload: Dict[str, Any]) -> Optional[int]:
    return _extract_first_int(payload, _MAX_COMPLETION_KEYS)


def _extract_pricing(payload: Dict[str, Any]) -> Dict[str, Any]:
    novita_input = payload.get("input_token_price_per_m")
    novita_output = payload.get("output_token_price_per_m")
    if novita_input is not None or novita_output is not None:
        pricing: Dict[str, Any] = {}
        if novita_input is not None:
            pricing["prompt"] = str(float(novita_input) / 10_000 / 1_000_000)
        if novita_output is not None:
            pricing["completion"] = str(float(novita_output) / 10_000 / 1_000_000)
        return pricing

    alias_map = {
        "prompt": ("prompt", "input", "input_cost_per_token", "prompt_token_cost"),
        "completion": ("completion", "output", "output_cost_per_token", "completion_token_cost"),
        "request": ("request", "request_cost"),
        "cache_read": ("cache_read", "cached_prompt", "input_cache_read", "cache_read_cost_per_token"),
        "cache_write": ("cache_write", "cache_creation", "input_cache_write", "cache_write_cost_per_token"),
    }
    for mapping in _iter_nested_dicts(payload):
        normalized = {str(key).lower(): value for key, value in mapping.items()}
        if not any(any(alias in normalized for alias in aliases) for aliases in alias_map.values()):
            continue
        pricing: Dict[str, Any] = {}
        for target, aliases in alias_map.items():
            for alias in aliases:
                if alias in normalized and normalized[alias] not in {None, ""}:
                    pricing[target] = normalized[alias]
                    break
        if pricing:
            return pricing
    return {}


def _add_model_aliases(cache: Dict[str, Dict[str, Any]], model_id: str, entry: Dict[str, Any]) -> None:
    cache[model_id] = entry
    if "/" in model_id:
        bare_model = model_id.split("/", 1)[1]
        cache.setdefault(bare_model, entry)


def fetch_model_metadata(force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
    """从 OpenRouter 获取模型元数据(缓存 1 小时)。"""
    global _model_metadata_cache, _model_metadata_cache_time

    if not force_refresh and _model_metadata_cache and (time.time() - _model_metadata_cache_time) < _MODEL_CACHE_TTL:
        return _model_metadata_cache

    if not force_refresh:
        disk_age = _model_metadata_disk_cache_age_seconds()
        if disk_age is not None and disk_age < _MODEL_CACHE_TTL:
            disk_cache = _load_model_metadata_disk_cache()
            if disk_cache:
                _model_metadata_cache = disk_cache
                _model_metadata_cache_time = time.time() - disk_age
                return _model_metadata_cache

    try:
        response = requests.get(OPENROUTER_MODELS_URL, timeout=10, verify=_resolve_requests_verify())
        response.raise_for_status()
        data = response.json()

        cache = {}
        for model in data.get("data", []):
            model_id = model.get("id", "")
            entry = {
                "context_length": model.get("context_length", 128000),
                "max_completion_tokens": model.get("top_provider", {}).get("max_completion_tokens", 4096),
                "name": model.get("name", model_id),
                "pricing": model.get("pricing", {}),
            }
            _add_model_aliases(cache, model_id, entry)
            canonical = model.get("canonical_slug", "")
            if canonical and canonical != model_id:
                _add_model_aliases(cache, canonical, entry)

        _model_metadata_cache = cache
        _model_metadata_cache_time = time.time()
        _save_model_metadata_disk_cache(cache)
        logger.debug("Fetched metadata for %s models from OpenRouter", len(cache))
        return cache

    except Exception as e:
        logger.warning(f"Failed to fetch model metadata from OpenRouter: {e}")
        if _model_metadata_cache:
            return _model_metadata_cache
        disk_cache = _load_model_metadata_disk_cache()
        if disk_cache:
            _model_metadata_cache = disk_cache
            disk_age = _model_metadata_disk_cache_age_seconds()
            if disk_age is not None:
                _model_metadata_cache_time = time.time() - min(disk_age, _MODEL_CACHE_TTL)
            else:
                _model_metadata_cache_time = time.time() - _MODEL_CACHE_TTL + 1
            return _model_metadata_cache
        return {}


def fetch_endpoint_model_metadata(
    base_url: str,
    api_key: str = "",
    force_refresh: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """从 OpenAI 兼容的 ``/models`` 端点获取模型元数据。

    用于显式自定义端点,其中硬编码的全局模型名称默认值不可靠。
    结果按 base URL 在内存中缓存。
    """
    normalized = _normalize_base_url(base_url)
    if not normalized or _is_openrouter_base_url(normalized):
        return {}

    if not force_refresh:
        cached = _endpoint_model_metadata_cache.get(normalized)
        cached_at = _endpoint_model_metadata_cache_time.get(normalized, 0)
        if cached is not None and (time.time() - cached_at) < _ENDPOINT_MODEL_CACHE_TTL:
            return cached

    candidates = [normalized]
    if normalized.endswith("/v1"):
        alternate = normalized[:-3].rstrip("/")
    else:
        alternate = normalized + "/v1"
    if alternate and alternate not in candidates:
        candidates.append(alternate)

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    last_error: Optional[Exception] = None

    if is_local_endpoint(normalized):
        try:
            if detect_local_server_type(normalized, api_key=api_key) == "lm-studio":
                server_url = normalized[:-3].rstrip("/") if normalized.endswith("/v1") else normalized
                response = requests.get(
                    server_url.rstrip("/") + "/api/v1/models",
                    headers=headers,
                    timeout=10,
                    verify=_resolve_requests_verify(),
                )
                response.raise_for_status()
                payload = response.json()
                cache: Dict[str, Dict[str, Any]] = {}
                for model in payload.get("models", []):
                    if not isinstance(model, dict):
                        continue
                    model_id = model.get("key") or model.get("id")
                    if not model_id:
                        continue
                    entry: Dict[str, Any] = {"name": model.get("name", model_id)}

                    context_length = None
                    for inst in model.get("loaded_instances", []) or []:
                        if not isinstance(inst, dict):
                            continue
                        cfg = inst.get("config", {})
                        ctx = cfg.get("context_length") if isinstance(cfg, dict) else None
                        if isinstance(ctx, int) and ctx > 0:
                            context_length = ctx
                            break
                    if context_length is not None:
                        entry["context_length"] = context_length

                    max_completion_tokens = _extract_max_completion_tokens(model)
                    if max_completion_tokens is not None:
                        entry["max_completion_tokens"] = max_completion_tokens

                    pricing = _extract_pricing(model)
                    if pricing:
                        entry["pricing"] = pricing

                    _add_model_aliases(cache, model_id, entry)
                    alt_id = model.get("id")
                    if isinstance(alt_id, str) and alt_id and alt_id != model_id:
                        _add_model_aliases(cache, alt_id, entry)

                _endpoint_model_metadata_cache[normalized] = cache
                _endpoint_model_metadata_cache_time[normalized] = time.time()
                return cache
        except Exception as exc:
            last_error = exc

    for candidate in candidates:
        url = candidate.rstrip("/") + "/models"
        try:
            response = requests.get(url, headers=headers, timeout=10, verify=_resolve_requests_verify())
            response.raise_for_status()
            payload = response.json()
            cache: Dict[str, Dict[str, Any]] = {}
            for model in payload.get("data", []):
                if not isinstance(model, dict):
                    continue
                model_id = model.get("id")
                if not model_id:
                    continue
                entry: Dict[str, Any] = {"name": model.get("name", model_id)}
                context_length = _extract_context_length(model)
                if context_length is not None:
                    entry["context_length"] = context_length
                max_completion_tokens = _extract_max_completion_tokens(model)
                if max_completion_tokens is not None:
                    entry["max_completion_tokens"] = max_completion_tokens
                pricing = _extract_pricing(model)
                if pricing:
                    entry["pricing"] = pricing
                _add_model_aliases(cache, model_id, entry)

            # 如果是 llama.cpp 服务器,查询 /props 以获取实际分配的上下文
            is_llamacpp = any(
                m.get("owned_by") == "llamacpp"
                for m in payload.get("data", []) if isinstance(m, dict)
            )
            if is_llamacpp:
                try:
                    # 首先尝试 /v1/props(当前 llama.cpp);较旧构建回退到 /props
                    base = candidate.rstrip("/").replace("/v1", "")
                    _verify = _resolve_requests_verify()
                    props_resp = requests.get(base + "/v1/props", headers=headers, timeout=5, verify=_verify)
                    if not props_resp.ok:
                        props_resp = requests.get(base + "/props", headers=headers, timeout=5, verify=_verify)
                    if props_resp.ok:
                        props = props_resp.json()
                        gen_settings = props.get("default_generation_settings", {})
                        n_ctx = gen_settings.get("n_ctx")
                        model_alias = props.get("model_alias", "")
                        if n_ctx and model_alias and model_alias in cache:
                            cache[model_alias]["context_length"] = n_ctx
                except Exception:
                    pass

            _endpoint_model_metadata_cache[normalized] = cache
            _endpoint_model_metadata_cache_time[normalized] = time.time()
            return cache
        except Exception as exc:
            last_error = exc

    if last_error:
        logger.debug("Failed to fetch model metadata from %s/models: %s", normalized, last_error)
    _endpoint_model_metadata_cache[normalized] = {}
    _endpoint_model_metadata_cache_time[normalized] = time.time()
    return {}


def _resolve_endpoint_context_length(
    model: str,
    base_url: str,
    api_key: str = "",
) -> Optional[int]:
    """从端点的实时 ``/models`` 元数据解析上下文长度。"""
    endpoint_metadata = fetch_endpoint_model_metadata(base_url, api_key=api_key)
    matched = endpoint_metadata.get(model)
    if not matched:
        if len(endpoint_metadata) == 1:
            matched = next(iter(endpoint_metadata.values()))
        else:
            for key, entry in endpoint_metadata.items():
                if model in key or key in model:
                    matched = entry
                    break
    if matched:
        context_length = matched.get("context_length")
        if isinstance(context_length, int):
            return context_length
    return None


def _get_context_cache_path() -> Path:
    """返回持久化上下文长度缓存文件的路径。"""
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "context_length_cache.yaml"


def _load_context_cache() -> Dict[str, int]:
    """从磁盘加载模型+提供商 -> 上下文长度缓存。"""
    path = _get_context_cache_path()
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("context_lengths", {})
    except Exception as e:
        logger.debug("Failed to load context length cache: %s", e)
        return {}


def save_context_length(model: str, base_url: str, length: int) -> None:
    """持久化发现的模型+提供商组合的上下文长度。

    缓存键为 ``model@base_url``,因此同一模型名称从不同提供商提供服务
    时可以有不同限制。
    """
    key = f"{model}@{base_url}"
    cache = _load_context_cache()
    if cache.get(key) == length:
        return  # 已存储
    cache[key] = length
    path = _get_context_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump({"context_lengths": cache}, f, default_flow_style=False)
        logger.info("Cached context length %s -> %s tokens", key, f"{length:,}")
    except Exception as e:
        logger.debug("Failed to save context length cache: %s", e)


def get_cached_context_length(model: str, base_url: str) -> Optional[int]:
    """查找先前发现的模型+提供商上下文长度。"""
    key = f"{model}@{base_url}"
    cache = _load_context_cache()
    return cache.get(key)


def _invalidate_cached_context_length(model: str, base_url: str) -> None:
    """丢弃陈旧的缓存条目,以便在下次查找时重新解析。"""
    key = f"{model}@{base_url}"
    cache = _load_context_cache()
    if key not in cache:
        return
    del cache[key]
    path = _get_context_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump({"context_lengths": cache}, f, default_flow_style=False)
    except Exception as e:
        logger.debug("Failed to invalidate context length cache entry %s: %s", key, e)


def get_next_probe_tier(current_length: int) -> Optional[int]:
    """返回下一个较低的探测档位,或如果已在最小值则返回 None。"""
    for tier in CONTEXT_PROBE_TIERS:
        if tier < current_length:
            return tier
    return None


def parse_context_limit_from_error(error_msg: str) -> Optional[int]:
    """尝试从 API 错误消息中提取实际上下文限制。

    许多提供商在错误文本中包含限制,例如:
      - "maximum context length is 32768 tokens"
      - "context_length_exceeded: 131072"
      - "Maximum context size 32768 exceeded"
      - "model's max context length is 65536"
    """
    error_lower = error_msg.lower()
    # 模式:查找上下文相关关键字附近的数字
    patterns = [
        r'(?:max(?:imum)?|limit)\s*(?:context\s*)?(?:length|size|window)?\s*(?:is|of|:)?\s*(\d{4,})',
        r'context\s*(?:length|size|window)\s*(?:is|of|:)?\s*(\d{4,})',
        r'(\d{4,})\s*(?:token)?\s*(?:context|limit)',
        r'>\s*(\d{4,})\s*(?:max|limit|token)',  # "250000 tokens > 200000 maximum"
        r'(\d{4,})\s*(?:max(?:imum)?)\b',  # "200000 maximum"
    ]
    for pattern in patterns:
        match = re.search(pattern, error_lower)
        if match:
            limit = int(match.group(1))
            # 健全检查:必须是合理的上下文长度
            if 1024 <= limit <= 10_000_000:
                return limit
    return None


def get_context_length_from_provider_error(
    error_msg: str,
    current_context_length: int,
) -> Optional[int]:
    """返回提供商报告的较低上下文限制(如果存在)。

    上下文溢出恢复不得发明新的模型窗口大小。某些提供商仅说输入超出
    上下文窗口而不报告实际最大值。在这种情况下,调用方应保持配置的
    context_length 并仅尝试压缩,而不是通过猜测的探测档逐步下降
    (1M → 256K → 128K → ...)。
    """
    parsed_limit = parse_context_limit_from_error(error_msg)
    if parsed_limit is None:
        return None
    if parsed_limit < current_context_length:
        return parsed_limit
    return None


def parse_available_output_tokens_from_error(error_msg: str) -> Optional[int]:
    """检测"输出上限过大"错误并返回有多少输出令牌可用。

    背景 — 存在两种不同的上下文错误:
      1. "Prompt too long"  — 输入本身超出上下文窗口。
           修复:压缩历史,并且仅在提供商明确报告实际较低限制时
           减少 context_length。
      2. "max_tokens too large" — 输入正常,但 input + requested_output > window。
           修复:为此调用减少 max_tokens(输出上限)。
           请勿触碰 context_length — 窗口未缩小。

    Anthropic 的 API 返回如下错误:
      "max_tokens: 32768 > context_window: 200000 - input_tokens: 190000 = available_tokens: 10000"

    返回适合的输出令牌数(例如上面的 10000),如果错误看起来不像
    max_tokens-过大错误则返回 None。
    """
    error_lower = error_msg.lower()

    # 必须看起来像输出上限错误,而不是提示长度错误。
    is_output_cap_error = (
        "max_tokens" in error_lower
        and ("available_tokens" in error_lower or "available tokens" in error_lower)
    ) or (
        # OpenRouter/Nous 相同条件的措辞。
        "in the output" in error_lower
        and "maximum context length" in error_lower
    ) or (
        # LM Studio / llama.cpp / 某些 OpenAI 兼容服务器:
        #   "This model's maximum context length is 65536 tokens. However, you
        #    requested 65536 output tokens and your prompt contains 77409
        #    characters ..."
        # "requested N output tokens" 措辞意味着输出上限是问题
        # (输入本身适合) — 减少 max_tokens,不压缩。
        "maximum context length" in error_lower
        and "requested" in error_lower
        and "output tokens" in error_lower
    )
    if not is_output_cap_error:
        return None

    # 提取 available_tokens 数字。
    # Anthropic 格式: "… = available_tokens: 10000"
    patterns = [
        r'available_tokens[:\s]+(\d+)',
        r'available\s+tokens[:\s]+(\d+)',
        # 回退: "=" 后面表达式中的最后一个数字,如 "200000 - 190000 = 10000"
        r'=\s*(\d+)\s*$',
    ]
    for pattern in patterns:
        match = re.search(pattern, error_lower)
        if match:
            tokens = int(match.group(1))
            if tokens >= 1:
                return tokens

    # OpenRouter/Nous 格式: "maximum context length is N … (A of text input,
    # B of tool input, C in the output)". 可用输出 = ctx - text - tool。
    _m_ctx = re.search(r'maximum context length is (\d+)', error_lower)
    _m_parts = re.search(
        r'\((\d+)\s+of text input,\s*(\d+)\s+of tool input,\s*(\d+)\s+in the output\)',
        error_lower,
    )
    if _m_ctx and _m_parts:
        _available = int(_m_ctx.group(1)) - int(_m_parts.group(1)) - int(_m_parts.group(2))
        if _available >= 1:
            return _available

    # LM Studio / llama.cpp 风格:上下文窗口以令牌报告,但
    # 提示大小以字符报告,例如
    #   "maximum context length is 65536 tokens ... your prompt contains 77409
    #    characters ..."。
    # 保守地估计输入令牌(~3 字符/令牌,超额保留输入以便重试的
    # 输出上限安全地留在窗口内),并将窗口的其余部分留给输出。
    _m_ctx_tok = re.search(r'maximum context length is (\d+)\s*token', error_lower)
    _m_chars = re.search(r'prompt contains (\d+)\s*character', error_lower)
    if _m_ctx_tok and _m_chars:
        _ctx = int(_m_ctx_tok.group(1))
        _est_input = (int(_m_chars.group(1)) + 2) // 3
        _available = _ctx - _est_input
        if _available >= 1:
            return _available

    return None


def _model_id_matches(candidate_id: str, lookup_model: str) -> bool:
    """如果 *candidate_id*(来自服务器)匹配 *lookup_model*(配置)则返回 True。

    支持两种形式:
    - 精确匹配:  "nvidia-nemotron-super-49b-v1" == "nvidia-nemotron-super-49b-v1"
    - Slug 匹配:   "nvidia/nvidia-nemotron-super-49b-v1" 匹配 "nvidia-nemotron-super-49b-v1"
                    (最后一个 "/" 之后的部分等于 lookup_model)

    这覆盖了 LM Studio 的原生 API,它将模型存储为 "publisher/slug",
    而用户通常仅在 "local:" 前缀后配置 slug。
    """
    if candidate_id == lookup_model:
        return True
    # Slug 匹配:候选的 basename 等于查找名称
    if "/" in candidate_id and candidate_id.rsplit("/", 1)[1] == lookup_model:
        return True
    return False


def query_ollama_num_ctx(model: str, base_url: str, api_key: str = "") -> Optional[int]:
    """查询 Ollama 服务器的模型上下文长度。

    通过 ``/api/show`` 从 GGUF 元数据返回模型的最大上下文,
    或如果设置了则返回 Modelfile 中的显式 ``num_ctx``。
    如果服务器不可达或不是 Ollama 则返回 None。

    这是在 Ollama 聊天请求中应作为 ``num_ctx`` 传递的值,
    以覆盖默认的 2048。
    """
    import httpx

    bare_model = _strip_provider_prefix(model)
    server_url = base_url.rstrip("/")
    if server_url.endswith("/v1"):
        server_url = server_url[:-3]

    try:
        server_type = detect_local_server_type(base_url, api_key=api_key)
    except Exception:
        return None
    if server_type != "ollama":
        return None

    headers = _auth_headers(api_key)

    try:
        with httpx.Client(timeout=3.0, headers=headers) as client:
            resp = client.post(f"{server_url}/api/show", json={"name": bare_model})
            if resp.status_code != 200:
                return None
            data = resp.json()

            # 优先使用 Modelfile 参数中的显式 num_ctx(用户覆盖)
            params = data.get("parameters", "")
            if "num_ctx" in params:
                for line in params.split("\n"):
                    if "num_ctx" in line:
                        parts = line.strip().split()
                        if len(parts) >= 2:
                            try:
                                return int(parts[-1])
                            except ValueError:
                                pass

            # 回退到 GGUF model_info context_length(训练最大值)
            model_info = data.get("model_info", {})
            for key, value in model_info.items():
                if "context_length" in key and isinstance(value, (int, float)):
                    return int(value)
    except Exception:
        pass
    return None


def _query_ollama_api_show(model: str, base_url: str, api_key: str = "") -> Optional[int]:
    """查询 Ollama 服务器的原生 ``/api/show`` 以获取上下文长度。

    提供商无关:适用于任何 Ollama 兼容的服务器,无论主机名 — 本地 Ollama、
    Ollama Cloud(``ollama.com``)、反向代理后的自定义 Ollama 托管等。
    对于非 Ollama 服务器,POST 快速返回 404/405;该函数优雅地处理错误。

    对于托管服务器,GGUF ``model_info.*.context_length`` 是权威来源:
    用户无法设置自己的 ``num_ctx``,OpenAI 兼容 ``/v1/models`` 端点根据
    OpenAI 架构正确省略 ``context_length``。

    托管 Ollama 的解析顺序:
      1. ``model_info.*.context_length`` — GGUF 训练最大值(权威)
      2. ``parameters`` → ``num_ctx`` — 服务器端 Modelfile 覆盖
    顺序与 ``query_ollama_num_ctx()`` 相反,因为本地用户自己控制
    ``num_ctx``;托管用户无法控制。
    """
    import httpx

    server_url = base_url.rstrip("/")
    if server_url.endswith("/v1"):
        server_url = server_url[:-3]

    headers = _auth_headers(api_key)

    try:
        with httpx.Client(timeout=5.0, headers=headers) as client:
            resp = client.post(f"{server_url}/api/show", json={"name": model})
            if resp.status_code != 200:
                return None
            data = resp.json()

            # 托管 Ollama: GGUF model_info 是真正的最大值 — 优先于
            # num_ctx,Cloud 运营商可能任意限制它。
            model_info = data.get("model_info", {})
            for key, value in model_info.items():
                if "context_length" in key and isinstance(value, (int, float)):
                    ctx = int(value)
                    if ctx >= 1024:
                        return ctx

            # 回退到 Modelfile 参数中的 num_ctx(Cloud 上罕见)
            params = data.get("parameters", "")
            if "num_ctx" in params:
                for line in params.split("\n"):
                    if "num_ctx" in line:
                        parts = line.strip().split()
                        if len(parts) >= 2:
                            try:
                                ctx = int(parts[-1])
                                if ctx >= 1024:
                                    return ctx
                            except ValueError:
                                pass
    except Exception:
        pass
    return None


def _model_name_suggests_kimi(model: str) -> bool:
    """当模型名称看起来像 Kimi 系列模型时返回 True。

    捕获 ``kimi-k2.6``、``kimi-k2.5``、``kimi-k2-thinking``,
    ``moonshotai/Kimi-K2.6`` 和类似变体。用作守卫,
    防止陈旧的 OpenRouter 元数据将这些模型报告为 32K 上下文,
    而它们实际支持 262K+。
    """
    lower = model.lower()
    return lower.startswith("kimi") or "moonshot" in lower


def _model_name_suggests_minimax_m3(model: str) -> bool:
    """当模型名称看起来像 MiniMax M3 时返回 True。

    捕获 ``MiniMax-M3``、``minimax/minimax-m3`` 和各种表面上的变体
    (原生 MiniMax-M3、OpenRouter/Nous minimax/minimax-m3)。
    用作守卫,防止预编目构建播种的陈旧缓存条目通过
    通用 ``minimax`` 兜底(204,800)解析 M3,而不是 ``minimax-m3`` (1M)
    条目存在于 DEFAULT_CONTEXT_LENGTHS 之前。
    """
    return "minimax-m3" in model.lower()


def _model_name_suggests_grok_4_3(model: str) -> bool:
    """当模型名称看起来像 Grok 4.3 变体时返回 True。

    捕获 ``grok-4.3``、``grok-4.3-latest`` 和类似 slug。
    用作守卫,防止预编目构建播种的陈旧缓存条目
    通过通用 ``grok-4`` 兜底(256,000)解析 grok-4.3,
    而不是在 2026-05-15 添加到 DEFAULT_CONTEXT_LENGTHS 的
    ``grok-4.3`` (1M) 条目。
    """
    return "grok-4.3" in model.lower()


def _query_local_context_length(model: str, base_url: str, api_key: str = "") -> Optional[int]:
    """查询本地服务器的模型上下文长度。"""
    import httpx

    # 剥离已识别的提供商前缀(例如 "local:model-name" → "model-name")。
    # Ollama "model:tag" 冒号(例如 "qwen3.5:27b")被有意保留。
    model = _strip_provider_prefix(model)

    # 剥离 /v1 后缀以获取服务器根目录
    server_url = base_url.rstrip("/")
    if server_url.endswith("/v1"):
        server_url = server_url[:-3]

    headers = _auth_headers(api_key)

    try:
        server_type = detect_local_server_type(base_url, api_key=api_key)
    except Exception:
        server_type = None

    try:
        with httpx.Client(timeout=3.0, headers=headers) as client:
            # Ollama: /api/show 返回带有上下文信息的模型详细信息
            if server_type == "ollama":
                resp = client.post(f"{server_url}/api/show", json={"name": model})
                if resp.status_code == 200:
                    data = resp.json()
                    # 优先使用 Modelfile 参数中的显式 num_ctx:这是
                    # Ollama 实际为其分配 KV 缓存的*运行时*上下文。
                    # GGUF model_info.context_length 是训练最大值,
                    # 可能大于 num_ctx — 在此使用它会让 Hermes 对话增长超过
                    # 运行时限制,Ollama 会静默截断。匹配 query_ollama_num_ctx()。
                    params = data.get("parameters", "")
                    if "num_ctx" in params:
                        for line in params.split("\n"):
                            if "num_ctx" in line:
                                parts = line.strip().split()
                                if len(parts) >= 2:
                                    try:
                                        return int(parts[-1])
                                    except ValueError:
                                        pass
                    # 回退到 GGUF model_info context_length(训练最大值)
                    model_info = data.get("model_info", {})
                    for key, value in model_info.items():
                        if "context_length" in key and isinstance(value, (int, float)):
                            return int(value)

            # LM Studio 原生 API: /api/v1/models 返回 max_context_length。
            # 这比 OpenAI 兼容的 /v1/models 更可靠,后者不包含
            # LM Studio 服务器的上下文窗口信息。使用 _model_id_matches
            # 进行模糊匹配:LM Studio 将模型存储为 "publisher/slug",
            # 但用户仅在 "local:" 前缀后配置 slug。
            if server_type == "lm-studio":
                resp = client.get(f"{server_url}/api/v1/models")
                if resp.status_code == 200:
                    data = resp.json()
                    for m in data.get("models", []):
                        if _model_id_matches(m.get("key", ""), model) or _model_id_matches(m.get("id", ""), model):
                            # 优先使用加载实例上下文(实际运行时值)
                            for inst in m.get("loaded_instances", []):
                                cfg = inst.get("config", {})
                                ctx = cfg.get("context_length")
                                if ctx and isinstance(ctx, (int, float)):
                                    return int(ctx)
                                break

            # LM Studio / vLLM / llama.cpp: 尝试 /v1/models/{model}
            resp = client.get(f"{server_url}/v1/models/{model}")
            if resp.status_code == 200:
                data = resp.json()
                # vLLM 返回 max_model_len
                ctx = data.get("max_model_len") or data.get("context_length") or data.get("max_tokens")
                if ctx and isinstance(ctx, (int, float)):
                    return int(ctx)

            # 尝试 /v1/models 并在列表中查找模型。
            # 使用 _model_id_matches 处理 "publisher/slug" 与裸 "slug"。
            resp = client.get(f"{server_url}/v1/models")
            if resp.status_code == 200:
                data = resp.json()
                models_list = data.get("data", [])
                for m in models_list:
                    if _model_id_matches(m.get("id", ""), model):
                        ctx = m.get("max_model_len") or m.get("context_length") or m.get("max_tokens")
                        if ctx and isinstance(ctx, (int, float)):
                            return int(ctx)
    except Exception:
        pass

    return None


def _normalize_model_version(model: str) -> str:
    """规范化匹配的版本分隔符。

    Nous 使用破折号: claude-opus-4-6, claude-sonnet-4-5
    OpenRouter 使用点号: claude-opus-4.6, claude-sonnet-4.5
    将两者规范化为破折号以进行比较。
    """
    return model.replace(".", "-")


def _query_anthropic_context_length(model: str, base_url: str, api_key: str) -> Optional[int]:
    """查询 Anthropic 的 /v1/models 端点以获取上下文长度。

    仅适用于常规 ANTHROPIC_API_KEY (sk-ant-api*)。
    来自 Claude Code 的 OAuth 令牌(sk-ant-oat*) 返回 401。
    """
    if not api_key or api_key.startswith("sk-ant-oat"):
        return None  # OAuth 令牌无法访问 /v1/models
    try:
        base = base_url.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        url = f"{base}/v1/models?limit=1000"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        resp = requests.get(url, headers=headers, timeout=10, verify=_resolve_requests_verify())
        if resp.status_code != 200:
            return None
        data = resp.json()
        for m in data.get("data", []):
            if m.get("id") == model:
                ctx = m.get("max_input_tokens")
                if isinstance(ctx, int) and ctx > 0:
                    return ctx
    except Exception as e:
        logger.debug("Anthropic /v1/models query failed: %s", e)
    return None


# 已知的 ChatGPT Codex OAuth 上下文窗口(通过实时 chatgpt.com/backend-api/codex/models
# 探测观察到,Apr 2026)。这些是 `context_window` 值,即 Codex 实际强制执行的值 —
# 直接 OpenAI API 对相同 slug 具有更大限制,但 Codex OAuth 上限较低
# (例如 gpt-5.5 在 API 上为 1.05M,在 Codex 上为 272K)。
#
# 用作实时探测失败时的回退(无令牌、网络错误)。最长键优先,
# 因此子字符串匹配选择最具体的条目。
_CODEX_OAUTH_CONTEXT_FALLBACK: Dict[str, int] = {
    "gpt-5.1-codex-max": 272_000,
    "gpt-5.1-codex-mini": 272_000,
    "gpt-5.3-codex": 272_000,
    # Spark 在专门的低延迟硬件上运行,并暴露比其他 Codex OAuth slug
    # 更小的 128k 窗口。显式列出,因此最长键优先回退正确解析它 —
    # 否则 "gpt-5.3-codex" 上的子字符串匹配会胜出并报告 272k。
    # 可用性由 Codex 后端的 ChatGPT Pro 权益门控。
    "gpt-5.3-codex-spark": 128_000,
    "gpt-5.2-codex": 272_000,
    "gpt-5.4-mini": 272_000,
    "gpt-5.5": 272_000,
    "gpt-5.4": 272_000,
    "gpt-5.2": 272_000,
    "gpt-5": 272_000,
}


_codex_oauth_context_cache: Dict[str, int] = {}
_codex_oauth_context_cache_time: float = 0.0
_CODEX_OAUTH_CONTEXT_CACHE_TTL = 3600  # 1 hour


def _fetch_codex_oauth_context_lengths(access_token: str) -> Dict[str, int]:
    """探测 ChatGPT Codex /models 端点以获取每 slug 上下文窗口。

    Codex OAuth 施加其自己的上下文限制,与直接 OpenAI API 不同
    (例如 gpt-5.5 在 API 上为 1.05M,在 Codex 上为 272K)。
    每个模型条目中的 `context_window` 字段是权威来源。

    返回 ``{slug: context_window}`` 字典。失败时为空。
    """
    global _codex_oauth_context_cache, _codex_oauth_context_cache_time
    now = time.time()
    if (
        _codex_oauth_context_cache
        and now - _codex_oauth_context_cache_time < _CODEX_OAUTH_CONTEXT_CACHE_TTL
    ):
        return _codex_oauth_context_cache

    try:
        resp = requests.get(
            "https://chatgpt.com/backend-api/codex/models?client_version=1.0.0",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
            verify=_resolve_requests_verify(),
        )
        if resp.status_code != 200:
            logger.debug(
                "Codex /models probe returned HTTP %s; falling back to hardcoded defaults",
                resp.status_code,
            )
            return {}
        data = resp.json()
    except Exception as exc:
        logger.debug("Codex /models probe failed: %s", exc)
        return {}

    entries = data.get("models", []) if isinstance(data, dict) else []
    result: Dict[str, int] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        ctx = item.get("context_window")
        if isinstance(slug, str) and isinstance(ctx, int) and ctx > 0:
            result[slug.strip()] = ctx

    if result:
        _codex_oauth_context_cache = result
        _codex_oauth_context_cache_time = now
    return result


def _resolve_codex_oauth_context_length(
    model: str, access_token: str = ""
) -> Optional[int]:
    """解析 Codex OAuth 模型的真实上下文窗口。

    优先使用 chatgpt.com/backend-api/codex/models 的实时探测(当我们有 bearer 令牌时),
    然后回退到 ``_CODEX_OAUTH_CONTEXT_FALLBACK``。
    """
    model_bare = _strip_provider_prefix(model).strip()
    if not model_bare:
        return None

    if access_token:
        live = _fetch_codex_oauth_context_lengths(access_token)
        if model_bare in live:
            return live[model_bare]
        # 不区分大小写匹配,以防大小写漂移
        model_lower = model_bare.lower()
        for slug, ctx in live.items():
            if slug.lower() == model_lower:
                return ctx

    # 回退:硬编码默认值的最长键优先子字符串匹配。
    model_lower = model_bare.lower()
    for slug, ctx in sorted(
        _CODEX_OAUTH_CONTEXT_FALLBACK.items(), key=lambda x: len(x[0]), reverse=True
    ):
        if slug in model_lower:
            return ctx

    return None


def _resolve_nous_context_length(
    model: str,
    base_url: str = "",
    api_key: str = "",
) -> Tuple[Optional[int], str]:
    """解析 Nous Portal 模型上下文长度。

    首先尝试实时 Nous 推理端点(权威),然后回退到带有后缀/版本匹配的
    OpenRouter 元数据。

    Nous 模型 ID 在前缀剥离后是裸的(例如 'qwen3.6-plus'、'claude-opus-4-6'),
    而 OpenRouter 使用带前缀的 ID(例如 'qwen/qwen3.6-plus'、
    'anthropic/claude-opus-4.6')。应用版本规范化(点↔破折号)以处理名称漂移。

    返回 ``(context_length, source)`` 其中 ``source`` 为:
      - ``"portal"``    — 实时 /v1/models 响应(权威)
      - ``"openrouter"`` — OpenRouter 缓存回退(非权威;
        调用方绝不得将其持久化到磁盘缓存,否则单个 portal 故障会永远
        冻结错误的值)
      - ``""``           — 无法解析
    """
    # Portal 优先 — Nous /models 端点是我们的基础设施强制执行的权威,
    # 可能与 OR 不同(例如 OR 为 qwen3.6-plus 报告 1M;
    # portal 正确显示 262144)。仅在 portal 未列出模型时才回退到
    # OR 目录。
    if base_url:
        portal_ctx = _resolve_endpoint_context_length(model, base_url, api_key=api_key)
        if portal_ctx is not None:
            return portal_ctx, "portal"

    metadata = fetch_model_metadata()

    def _safe_ctx(or_id: str, entry: dict) -> Optional[int]:
        ctx = entry.get("context_length")
        if ctx is None:
            return None
        if ctx <= 32768 and _model_name_suggests_kimi(or_id):
            logger.info(
                "Rejecting OpenRouter metadata context=%s for %r "
                "(Kimi-family underreport, Nous path); falling through to hardcoded defaults",
                ctx, or_id,
            )
            return None
        return ctx

    if model in metadata:
        ctx = _safe_ctx(model, metadata[model])
        if ctx is not None:
            return ctx, "openrouter"

    normalized = _normalize_model_version(model).lower()

    for or_id, entry in metadata.items():
        bare = or_id.split("/", 1)[1] if "/" in or_id else or_id
        if bare.lower() == model.lower() or _normalize_model_version(bare).lower() == normalized:
            ctx = _safe_ctx(or_id, entry)
            if ctx is not None:
                return ctx, "openrouter"

    model_lower = model.lower()
    for or_id, entry in metadata.items():
        bare = or_id.split("/", 1)[1] if "/" in or_id else or_id
        for candidate, query in [(bare.lower(), model_lower), (_normalize_model_version(bare).lower(), normalized)]:
            if candidate.startswith(query) and (
                len(candidate) == len(query) or candidate[len(query)] in "-:."
            ):
                ctx = _safe_ctx(or_id, entry)
                if ctx is not None:
                    return ctx, "openrouter"

    return None, ""


def get_model_context_length(
    model: str,
    base_url: str = "",
    api_key: str = "",
    config_context_length: int | None = None,
    provider: str = "",
    custom_providers: list | None = None,
) -> int:
    """获取模型的上下文长度。

    解析顺序:
    0. 显式配置覆盖(model.context_length 或 custom_providers 每模型)
    1. 持久缓存(先前通过探测发现)。Nous URL 在此处绕过缓存,
       因此步骤 5b 始终针对权威的 portal /v1/models 响应进行协调。
    1b. AWS Bedrock 静态表(必须在自定义端点探测之前)
    2. 活动端点元数据(/models 用于显式自定义端点)
    3. 本地服务器查询(用于本地端点)
    4. Anthropic /v1/models API(仅 API 密钥用户,非 OAuth)
    5. 提供商感知查找(在通用 OpenRouter 缓存之前):
       a. Copilot 实时 /models API
       b. Nous: 实时 /v1/models 探测优先(权威),然后 OR
          缓存回退,带后缀/版本规范化。仅持久化 portal 派生的值。
       c. Codex OAuth /models 探测
       d. GMI /models 端点
       e. Ollama 原生 /api/show 探测(任何 base_url,提供商无关)
       f. models.dev 注册表查找(带 :cloud/-cloud 后缀回退)
    6. OpenRouter 实时 API 元数据(Kimi 系列 32k 守卫)
    7. 硬编码默认值(宽泛家族模式,最长键优先)
    8. 本地服务器查询(最后手段)
    9. 默认回退(256K)"""
    # 0. 显式配置覆盖 — 用户最了解
    if config_context_length is not None and isinstance(config_context_length, int) and config_context_length > 0:
        return config_context_length

    # 0b. custom_providers 每模型覆盖 — 在任何探测之前检查。
    # 这填补了 /model 切换和显示路径曾经回退到 128K 的差距,
    # 尽管用户设置了每模型的 context_length。
    # 请参阅 #15779。
    if custom_providers and base_url and model:
        try:
            from hermes_cli.config import get_custom_provider_context_length
            cp_ctx = get_custom_provider_context_length(
                model=model,
                base_url=base_url,
                custom_providers=custom_providers,
            )
            if cp_ctx:
                return cp_ctx
        except Exception:
            pass  # 回退到探测

    # 规范化提供商前缀模型名称(例如 "local:model-name" →
    # "model-name"),以便缓存查找和服务器查询使用本地服务器实际知道的裸 ID。
    # Ollama "model:tag" 冒号被保留。
    model = _strip_provider_prefix(model)

    # 1. 检查持久缓存(模型+提供商)
    # LM Studio 被排除 — 其加载的上下文长度是瞬态的(用户可以通过
    # /api/v1/models/load 随时使用不同的 context_length 重新加载模型),
    # 因此陈旧的缓存值会屏蔽重新加载。
    if base_url and provider != "lmstudio":
        cached = get_cached_context_length(model, base_url)
        if cached is not None:
            # 使陈旧的 Codex OAuth 缓存条目无效:预修复 #14935 的构建
            # 通过 models.dev 将 gpt-5.x 解析为直接 API 值(例如 1.05M)
            # 并持久化。Codex OAuth 将每个 slug 限制为 272K,
            # 因此任何 >= 400K 的缓存 Codex 条目是旧解析路径的残留。
            # 删除它并在下面的步骤 5 中通过实时 /models 探测重新解析。
            if provider == "openai-codex" and cached >= 400_000:
                logger.info(
                    "Dropping stale Codex cache entry %s@%s -> %s (pre-fix value); "
                    "re-resolving via live /models probe",
                    model, base_url, f"{cached:,}",
                )
                _invalidate_cached_context_length(model, base_url)
            # 使陈旧的 32k 缓存条目无效(Kimi 系列模型)。
            elif cached <= 32768 and _model_name_suggests_kimi(model):
                logger.info(
                    "Dropping stale Kimi cache entry %s@%s -> %s (OpenRouter underreport); "
                    "re-resolving via hardcoded defaults",
                    model, base_url, f"{cached:,}",
                )
                _invalidate_cached_context_length(model, base_url)
            # 使陈旧的 ≤204,800 缓存条目无效(MiniMax-M3)。预编目
            # 构建通过通用 ``minimax`` 兜底(204,800)解析 M3,并持久化,
            # ``minimax-m3`` (1M) 条目存在之前。该陈旧值会在此永远存在,
            # 在步骤 1 坚持。M3 为 1M,因此任何子 256K 的缓存值是残留 —
            # 删除它并回退到硬编码默认值。
            elif cached <= 204_800 and _model_name_suggests_minimax_m3(model):
                logger.info(
                    "Dropping stale MiniMax-M3 cache entry %s@%s -> %s (pre-catalog value); "
                    "re-resolving via hardcoded defaults",
                    model, base_url, f"{cached:,}",
                )
                _invalidate_cached_context_length(model, base_url)
            # 使陈旧的 ≤256,000 缓存条目无效(Grok-4.3)。``grok-4.3`` (1M)
            # 条目在 2026-05-15 添加到 DEFAULT_CONTEXT_LENGTHS;
            # 在此之前,grok-4.3 slug 通过通用 ``grok-4`` 兜底(256,000)
            # 解析并持久化该值。grok-4.3 为 1M,因此任何子 262K
            # 缓存值是预编目残留 — 删除它并回退到硬编码默认值。
            elif cached <= 256_000 and _model_name_suggests_grok_4_3(model):
                logger.info(
                    "Dropping stale Grok-4.3 cache entry %s@%s -> %s (pre-catalog value); "
                    "re-resolving via hardcoded defaults",
                    model, base_url, f"{cached:,}",
                )
                _invalidate_cached_context_length(model, base_url)
            # Nous Portal: portal /v1/models 端点是权威的。
            # 绕过持久缓存,因此步骤 5b 始终针对它进行协调 —
            # 这纠正了从 OR 目录播种的预修复条目(相同的 OR
            # 低报告类别,即 Kimi/Qwen DEFAULT_CONTEXT_LENGTHS 覆盖存在以缓解),
            # 而无需在 portal 无法访问时触及磁盘文件。内存 300s 端点
            # 元数据使每次调用的成本在同一进程内摊销至 ~0。
            elif _infer_provider_from_url(base_url) == "nous":
                logger.debug(
                    "Bypassing persistent cache for %s@%s (Nous portal authoritative)",
                    model, base_url,
                )
                # 回退;步骤 5b 如果 portal 响应则协调并覆盖。
            else:
                return cached

    # 1b. AWS Bedrock — 使用静态上下文长度表。
    # Bedrock 的 ListFoundationModels API 不公开上下文窗口大小,
    # 因此我们在 bedrock_adapter.py 中维护一个精心策划的表,
    # 反映 AWS 强制的限制(例如 Claude 模型为 200K,而在原生 Anthropic
    # API 上为 1M)。这必须在步骤 2 的自定义端点探测之前运行 —
    # bedrock-runtime.<region>.amazonaws.com 不在 _URL_TO_PROVIDER 中,
    # 因此否则会被视为自定义端点,无法通过 /models 探测(Bedrock
    # 不公开该形状),并在到达原始步骤 4b 分支之前回退到 128K 默认值。
    if provider == "bedrock" or (
        base_url
        and base_url_hostname(base_url).startswith("bedrock-runtime.")
        and base_url_host_matches(base_url, "amazonaws.com")
    ):
        try:
            from agent.bedrock_adapter import get_bedrock_context_length
            return get_bedrock_context_length(model)
        except ImportError:
            pass  # boto3 未安装 — 回退到通用解析

    if provider == "novita" or (base_url and base_url_host_matches(base_url, "api.novita.ai")):
        ctx = _resolve_endpoint_context_length(model, base_url or "https://api.novita.ai/openai/v1", api_key=api_key)
        if ctx is not None:
            if base_url:
                save_context_length(model, base_url, ctx)
            return ctx

    # 2. 真正自定义/未知端点的活动端点元数据。
    # 已知提供商(Copilot、OpenAI、Anthropic 等)跳过此操作 —
    # 它们的 /models 端点可能报告提供商强加的限制
    # (例如 Copilot 返回 128k)而不是模型的完整上下文(400k)。
    # models.dev 具有正确的每提供商值,在步骤 5+ 检查。
    if _is_custom_endpoint(base_url) and not _is_known_provider_base_url(base_url):
        context_length = _resolve_endpoint_context_length(model, base_url, api_key=api_key)
        if context_length is not None:
            return context_length
        if not _is_known_provider_base_url(base_url):
            # 2b. Ollama 原生 /api/show — 任何 URL 都可能是 Ollama 服务器
            # (本地、云端或自定义托管)。非 Ollama 服务器快速返回 404/405。
            # 失败时回退。
            ctx = _query_ollama_api_show(model, base_url, api_key=api_key)
            if ctx is not None:
                save_context_length(model, base_url, ctx)
                return ctx
            # 3. 尝试直接查询本地服务器
            if is_local_endpoint(base_url):
                local_ctx = _query_local_context_length(model, base_url, api_key=api_key)
                if local_ctx and local_ctx > 0:
                    if provider != "lmstudio":
                        save_context_length(model, base_url, local_ctx)
                    return local_ctx
            logger.info(
                "Could not detect context length for model %r at %s — "
                "defaulting to %s tokens (probe-down). Set model.context_length "
                "in config.yaml to override.",
                model, base_url, f"{DEFAULT_FALLBACK_CONTEXT:,}",
            )
            # 3b. 在回退到硬 256K 默认值之前,最后检查硬编码目录。
            # 代理/自定义 Anthropic 网关(例如企业代理)无法通过上述 Ollama/本地
            # 探测,但模型名称可能仍然匹配 DEFAULT_CONTEXT_LENGTHS 中的条目
            # (例如 "claude-opus-4-8" → 1M)。没有此检查,此处的早期返回会短路
            # 步骤 8 的目录查找,并静默地将上下文限制在 256K。
            model_lower = model.lower()
            for default_model, length in sorted(
                DEFAULT_CONTEXT_LENGTHS.items(),
                key=lambda x: len(x[0]),
                reverse=True,
            ):
                if default_model in model_lower:
                    logger.info(
                        "Using hardcoded context length %s for model %r "
                        "(custom endpoint, catalog match on %r)",
                        f"{length:,}", model, default_model,
                    )
                    return length
            return DEFAULT_FALLBACK_CONTEXT

    # 4. Anthropic /v1/models API(仅用于常规 API 密钥,非 OAuth)
    if provider == "anthropic" or (
        base_url and base_url_hostname(base_url) == "api.anthropic.com"
    ):
        ctx = _query_anthropic_context_length(model, base_url or "https://api.anthropic.com", api_key)
        if ctx:
            return ctx

    # 4b. (Bedrock 在步骤 1b 更早处理 — 在自定义端点探测之前。)

    # 5. 提供商感知查找(在通用 OpenRouter 缓存之前)
    # 这些是提供商特定的,优先于通用 OR 缓存,
    # 因为同一模型在不同提供商可能有不同的上下文限制
    # (例如 claude-opus-4.6 在 Anthropic 上为 1M,在 GitHub Copilot 上为 128K)。
    # 如果提供商是通用的(openrouter/custom/empty),尝试从 URL 推断。
    effective_provider = provider
    if not effective_provider or effective_provider in {"openrouter", "custom"}:
        if base_url:
            inferred = _infer_provider_from_url(base_url)
            if inferred:
                effective_provider = inferred

    # 5a. Copilot 实时 /models API — 用户账户的 max_prompt_tokens。
    # 这捕获账户特定模型(例如 claude-opus-4.6-1m),这些模型
    # 不存在于 models.dev 中。对于确实在 models.dev 中的模型,
    # 这返回提供商强制执行的限制,这是用户实际可以使用的限制。
    if effective_provider in {"copilot", "copilot-acp", "github-copilot"}:
        try:
            from hermes_cli.models import get_copilot_model_context
            ctx = get_copilot_model_context(model, api_key=api_key)
            if ctx:
                return ctx
        except Exception:
            pass  # 回退到 models.dev

    if effective_provider == "nous":
        ctx, source = _resolve_nous_context_length(
            model, base_url=base_url or "", api_key=api_key or ""
        )
        if ctx:
            # 仅持久化 portal 派生的值。在此缓存 OR 回退值会冻结错误的数字,
            # 单个 portal 故障 / auth 故障会永久冻结。
            # OR 的目录是社区维护的,这正是 Kimi/Qwen
            # DEFAULT_CONTEXT_LENGTHS 覆盖存在的原因 — 我们不希望它泄漏
            # 到 Nous URL 的持久缓存中。
            if base_url and source == "portal":
                save_context_length(model, base_url, ctx)
            return ctx
    if effective_provider == "openai-codex":
        # Codex OAuth 施加比直接 OpenAI API 更低的上下文限制,
        # 相同 slug 的直接 API(例如 gpt-5.5 在 API 上为 1.05M,在 Codex 上为 272K)。
        # 权威来源是 Codex 自己的 /models 端点。
        codex_ctx = _resolve_codex_oauth_context_length(model, access_token=api_key or "")
        if codex_ctx:
            if base_url:
                save_context_length(model, base_url, codex_ctx)
            return codex_ctx
    if effective_provider == "gmi" and base_url:
        # GMI 通过 /models 暴露权威的 context_length,但它尚不在
        # models.dev 中。保留更高保真的端点查找。
        ctx = _resolve_endpoint_context_length(model, base_url, api_key=api_key)
        if ctx is not None:
            return ctx
    # 5e. Ollama 原生 /api/show 探测 — 为具有 base_url 的任何提供商运行,
    # 不仅仅是 ollama-cloud。Ollama 兼容服务器无论主机名如何都暴露
    # 此端点(本地 Ollama、Ollama Cloud、自定义 Ollama 托管)。
    # OpenAI 兼容 /v1/models 端点根据 OpenAI 架构正确省略 context_length,
    # 但 /api/show 返回权威的 GGUF model_info.context_length。
    # 对于非 Ollama 服务器(OpenAI、Anthropic 等),POST 快速返回 404/405。
    # 结果已缓存,因此命中为每个模型+URL,每小时一次。
    if base_url:
        ctx = _query_ollama_api_show(model, base_url, api_key=api_key)
        if ctx is not None:
            save_context_length(model, base_url, ctx)
            return ctx
    # 5f. OpenRouter 实时 /models 元数据 — OpenRouter 路由模型的权威。
    # OpenRouter 的目录承载每模型 context_length(例如
    # anthropic/claude-fable-5 -> 1M)并在新 slug 发布时刷新,
    # 因此它必须胜过 models.dev(步骤 5g)和硬编码家族兜底(步骤 8)。
    # 在此分支之前,OpenRouter 选择设置 effective_provider="openrouter",
    # 这(a)导致 models.dev 查找错过全新 slug,且(b)跳过步骤 6 OR
    # 回退(受限于 `not effective_provider`),因此像 claude-fable-5 这样的
    # 新 slug 会落入通用 "claude": 200K 条目并低报 1M 窗口。
    # 镜像上面的专用 Nous/Copilot/GMI 分支。
    if effective_provider == "openrouter":
        metadata = fetch_model_metadata()
        entry = metadata.get(model)
        if entry:
            or_ctx = entry.get("context_length")
            # 守卫已知的 OpenRouter Kimi 系列 32k 低报
            # (硬编码覆盖存在的同一类别)。
            if isinstance(or_ctx, int) and or_ctx > 0 and not (
                or_ctx == 32768 and _model_name_suggests_kimi(model)
            ):
                return or_ctx

    if effective_provider:
        from agent.models_dev import lookup_models_dev_context
        ctx = lookup_models_dev_context(effective_provider, model)
        if ctx:
            # MiniMax M3: models.dev 报告 512K,但实际上下文为 1M。
            # 优先使用硬编码目录而非过时的探测值。
            if _model_name_suggests_minimax_m3(model):
                catalog = DEFAULT_CONTEXT_LENGTHS.get("minimax-m3")
                if catalog and ctx < catalog:
                    logger.info(
                        "Rejecting models.dev context=%s for %r "
                        "(MiniMax-M3 underreport); using hardcoded default %s",
                        ctx, model, f"{catalog:,}",
                    )
                    ctx = catalog
            return ctx

    # 6. OpenRouter 实时 API 元数据 — 提商无关回退。
    # 仅当提供商未知(无 effective_provider)时才查询,
    # 因为 OpenRouter 数据是社区维护的,对于属于具有策划默认值的已知
    # 提商可能不正确。
    if not effective_provider:
        metadata = fetch_model_metadata()
        if model in metadata:
            or_ctx = metadata[model].get("context_length", DEFAULT_FALLBACK_CONTEXT)
            # 守卫已知 OpenRouter Kimi 系列模型的 32k 低报。
            if or_ctx == 32768 and _model_name_suggests_kimi(model):
                logger.info(
                    "Rejecting OpenRouter metadata context=%s for %r "
                    "(Kimi-family underreport); falling through to hardcoded defaults",
                    or_ctx, model,
                )
            else:
                return or_ctx

    # 7. (保留)

    # 8. 硬编码默认值(模糊匹配 — 最长键优先以提高特异性)
    # 仅检查 `default_model in model`(键是输入的子字符串)。
    # 反向(`model in default_model`)会导致较短的名称如
    # "claude-sonnet-4" 错误匹配 "claude-sonnet-4-6" 并返回 1M。
    model_lower = model.lower()
    for default_model, length in sorted(
        DEFAULT_CONTEXT_LENGTHS.items(), key=lambda x: len(x[0]), reverse=True
    ):
        if default_model in model_lower:
            return length

    # 9. 最后手段查询本地服务器
    if base_url and is_local_endpoint(base_url):
        local_ctx = _query_local_context_length(model, base_url, api_key=api_key)
        if local_ctx and local_ctx > 0:
            if provider != "lmstudio":
                save_context_length(model, base_url, local_ctx)
            return local_ctx

    # 10. 默认回退 — 256K
    return DEFAULT_FALLBACK_CONTEXT


def estimate_tokens_rough(text: str) -> int:
    """飞行前检查的粗略令牌估计(~4 字符/令牌)。

    使用向上取整除法,因此短文本(1-3 字符)从不估计为
    0 令牌,这会导致压缩器和飞行前检查在存在许多短工具结果时
    系统性低估。
    """
    if not text:
        return 0
    return (len(text) + 3) // 4


def estimate_messages_tokens_rough(messages: List[Dict[str, Any]]) -> int:
    """消息列表的粗略令牌估计(仅飞行前)。

    图像部分(base64 PNG/JPEG)按每个图像~1500 令牌的固定成本计算 —
    Anthropic 定价模型 — 而不是计算原始 base64 字符长度。
    如果没有这一点,单个~1MB 截图估计为~250K 令牌并触发过早的
    上下文压缩。
    """
    _IMAGE_TOKEN_COST = 1500
    total_chars = 0
    image_tokens = 0
    for msg in messages:
        total_chars += _estimate_message_chars(msg)
        image_tokens += _count_image_tokens(msg, _IMAGE_TOKEN_COST)
    return ((total_chars + 3) // 4) + image_tokens


def _count_image_tokens(msg: Dict[str, Any], cost_per_image: int) -> int:
    """计算消息中的类图像内容部分;返回其令牌成本。"""
    count = 0
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype in {"image", "image_url", "input_image"}:
                count += 1
    stashed = msg.get("_anthropic_content_blocks") if isinstance(msg, dict) else None
    if isinstance(stashed, list):
        for part in stashed:
            if isinstance(part, dict) and part.get("type") == "image":
                count += 1
    # 尚未转换的多模态工具结果。
    if isinstance(content, dict) and content.get("_multimodal"):
        inner = content.get("content")
        if isinstance(inner, list):
            for part in inner:
                if isinstance(part, dict) and part.get("type") in {"image", "image_url"}:
                    count += 1
    return count * cost_per_image


def _estimate_message_chars(msg: Dict[str, Any]) -> int:
    """用于令牌估计的字符计数,排除 base64 图像数据。

    Base64 图像通过 `_count_image_tokens` 计算;在此包含其原始字符会
    大规模高估令牌使用。
    """
    if not isinstance(msg, dict):
        return len(str(msg))
    shadow: Dict[str, Any] = {}
    for k, v in msg.items():
        if k == "_anthropic_content_blocks":
            continue
        if k == "content":
            if isinstance(v, list):
                cleaned = []
                for part in v:
                    if isinstance(part, dict):
                        if part.get("type") in {"image", "image_url", "input_image"}:
                            cleaned.append({"type": part.get("type"), "image": "[stripped]"})
                        else:
                            cleaned.append(part)
                    else:
                        cleaned.append(part)
                shadow[k] = cleaned
            elif isinstance(v, dict) and v.get("_multimodal"):
                shadow[k] = v.get("text_summary", "")
            else:
                shadow[k] = v
        else:
            shadow[k] = v
    return len(str(shadow))


def estimate_request_tokens_rough(
    messages: List[Dict[str, Any]],
    *,
    system_prompt: str = "",
    tools: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """完整 chat-completions 请求的粗略令牌估计。

    包括 Hermes 发送给提供商的主要负载桶:
    系统提示、对话消息和工具架构。启用 50+ 工具时,
    仅架构就可增加 20-30K 令牌 — 仅计算消息时的显著盲点。
    图像内容按每个图像的固定成本计算(参见 estimate_messages_tokens_rough)。
    """
    total = 0
    if system_prompt:
        total += (len(system_prompt) + 3) // 4
    if messages:
        total += estimate_messages_tokens_rough(messages)
    if tools:
        total += (len(str(tools)) + 3) // 4
    return total
