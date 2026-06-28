"""Provider profile 基类。

ProviderProfile 在一个地方声明了关于推理提供商的所有信息：
认证、端点、客户端特殊行为、请求时的特殊处理。传输层读取此配置，
而不是接收 20 多个布尔标志。

Provider profile 是声明式的 — 它们描述提供商的行为。
它们不负责客户端构建、凭据轮换或流式处理。
这些功能保留在 AIAgent 上。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# "完全省略 temperature" 的哨兵值（Kimi：由服务器管理）
OMIT_TEMPERATURE = object()


def _profile_user_agent() -> str:
    """返回 ``hermes-cli/<version>`` UA 字符串，带有稳定的回退值。

    由 ``ProviderProfile.fetch_models`` 使用，使目录探测不会使用
    默认的 ``Python-urllib/<ver>`` UA — 一些提供商（OpenCode Zen 等）
    位于 WAF 后面，会对该 UA 返回 403。
    """
    try:
        from hermes_cli import __version__ as _ver  # 延迟导入：避免导入时的循环依赖
        return f"hermes-cli/{_ver}"
    except Exception:
        return "hermes-cli"


@dataclass
class ProviderProfile:
    """基础 provider profile — 通过子类化或使用覆盖参数实例化。"""

    # ── 身份标识 ─────────────────────────────────────────────
    name: str
    api_mode: str = "chat_completions"
    aliases: tuple = ()

    # ── 可读元数据 ───────────────────────────────────────────
    display_name: str = ""       # 例如 "GMI Cloud" — 显示在选择器/标签中
    description: str = ""        # 例如 "GMI Cloud (multi-model direct API)" — 选择器副标题
    signup_url: str = ""         # 例如 "https://www.gmicloud.ai/" — 设置期间显示

    # ── 认证与端点 ───────────────────────────────────────────
    env_vars: tuple = ()
    base_url: str = ""
    models_url: str = ""  # 显式的 models 端点；回退到 {base_url}/models
    auth_type: str = "api_key"   # api_key|oauth_device_code|oauth_external|copilot|aws_sdk
    supports_health_check: bool = True  # False → doctor 跳过该提供商的 /models 探测

    # ── 视觉支持 ──────────────────────────────────────────────
    # 当提供商的 API 原生接受 tool-result 消息中的图像内容时为 True。
    # 在通过 tool result 暴露多模态模型的提供商上设置（Anthropic Messages API、
    # OpenAI Chat Completions、Gemini、MiniMax 等）。
    # 当为 False 且提供商没有注册的 profile 时，回退到模型目录查找。
    supports_vision: bool = False

    # 当提供商的 API 接受 list 类型的 tool 消息内容（包含 image_url 部分的多部分
    # 消息）时为 True。默认为 True 以保持向后兼容。对于接受多模态用户消息
    # 但拒绝 list 类型 tool 内容的提供商，设置为 False
    # （例如 Xiaomi MiMo，会返回 400 "text is not set"）。
    supports_vision_tool_messages: bool = True

    # ── 模型目录 ──────────────────────────────────────────────
    # fallback_models：当实时获取失败时在 /model 选择器中显示的精选列表。
    # 只应包含支持 tool calling 的代理模型。
    fallback_models: tuple = ()

    # hostname：在 model_metadata.py 中用于 URL→provider 反向映射的基础主机名，
    # 例如 "api.gmi-serving.com"。为空时从 base_url 派生。
    hostname: str = ""

    # ── 客户端级特殊行为（在客户端构建时设置一次） ───────────
    default_headers: dict[str, str] = field(default_factory=dict)

    # ── 请求级特殊行为 ────────────────────────────────────────
    # Temperature：None = 使用调用方的默认值，OMIT_TEMPERATURE = 不发送
    fixed_temperature: Any = None
    default_max_tokens: int | None = None
    default_aux_model: str = (
        ""  # 用于辅助任务的低成本模型（压缩、视觉等）
    )
    # 空 = 使用主模型

    # ── 钩子（在子类中覆盖以处理复杂的提供商） ────────────────

    def get_hostname(self) -> str:
        """返回提供商的基础主机名，用于基于 URL 的检测。

        如果显式设置了 self.hostname 则使用该值，否则从 base_url 派生。
        例如 'https://api.gmi-serving.com/v1' → 'api.gmi-serving.com'
        """
        if self.hostname:
            return self.hostname
        if self.base_url:
            from urllib.parse import urlparse
            return urlparse(self.base_url).hostname or ""
        return ""

    def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """提供商特定的消息预处理。

        在 codex 字段清理之后、developer 角色替换之前调用。
        默认：直接传递。
        """
        return messages

    def build_extra_body(
        self, *, session_id: str | None = None, **context: Any
    ) -> dict[str, Any]:
        """提供商特定的 extra_body 字段。

        合并到 API kwargs 的 extra_body 中。默认：空字典。
        """
        return {}

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """提供商特定的 kwargs，分别放入 extra_body 和顶层 api_kwargs。

        返回 (extra_body_additions, top_level_kwargs)。
        传输层将 extra_body_additions 合并到 extra_body，
        将 top_level_kwargs 直接合并到 api_kwargs。

        之所以这样拆分，是因为一些提供商将推理配置放在 extra_body 中
        （OpenRouter：extra_body.reasoning），而另一些提供商将其作为
        顶层 api_kwargs（Kimi：api_kwargs.reasoning_effort）。

        默认：({}, {})。
        """
        return {}, {}

    def get_max_tokens(self, model: str | None) -> int | None:
        """返回 *model* 的默认 max_tokens 上限。

        为需要按模型设置输出上限的提供商提供可覆盖的钩子 —
        例如一个代理多个上游后端的 relay，每个后端有不同的
        completion token 限制。当用户没有显式设置 max_tokens 时，
        传输层会调用此方法。

        默认：返回 self.default_max_tokens（静态 profile 字段），
        忽略模型名称。在子类中覆盖以按模型调整上限。
        """
        return self.default_max_tokens

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """从提供商的 models 端点获取实时模型列表。

        返回模型 ID 字符串列表，如果获取失败或提供商不支持
        实时模型列表则返回 None。

        端点 URL 的解析顺序：
          1. self.models_url（显式覆盖 — 当 models 端点与推理基础 URL
             不同时使用，例如 OpenRouter 在 /api/v1/models 暴露公共目录，
             而推理在 /api/v1）
          2. base_url（调用方覆盖 — 用户配置的 model.base_url）
          3. self.base_url + "/models"（标准 OpenAI 兼容回退）

        默认实现在给定 api_key 时发送 Bearer 认证，并转发
        self.default_headers。覆盖此方法以自定义认证、路径、
        响应格式，或为没有 REST 目录的提供商返回 None。

        当此方法返回 None 时，调用方必须始终回退到静态的
        _PROVIDER_MODELS 列表。
        """
        effective_base = base_url or self.base_url
        url = (self.models_url or "").strip()
        if not url:
            if not effective_base:
                return None
            url = effective_base.rstrip("/") + "/models"

        import json
        import urllib.request

        req = urllib.request.Request(url)
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Accept", "application/json")
        # 一些提供商（例如 OpenCode Zen）位于 WAF 后面，会阻止默认的
        # ``Python-urllib/<ver>`` User-Agent。设置通用的 hermes-cli UA
        # 以确保目录端点可访问。
        req.add_header("User-Agent", _profile_user_agent())
        for k, v in self.default_headers.items():
            req.add_header(k, v)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            items = data if isinstance(data, list) else data.get("data", [])
            return [m["id"] for m in items if isinstance(m, dict) and "id" in m]
        except Exception as exc:
            logger.debug("fetch_models(%s): %s", self.name, exc)
            return None
