"""OpenAI Chat Completions 传输层。

处理默认的 api_mode（'chat_completions'），被约 16 个 OpenAI 兼容提供商使用
（OpenRouter、Nous、NVIDIA、Qwen、Ollama、DeepSeek、xAI、Kimi 等）。

消息和工具已经是 OpenAI 格式——convert_messages 和 convert_tools 近乎恒等变换。
复杂性集中在 build_kwargs 中，包含针对不同提供商的 max_tokens 默认值、
推理配置、温度处理和 extra_body 组装的条件判断。
"""

import copy
from typing import Any, Dict

from agent.lmstudio_reasoning import resolve_lmstudio_effort
from agent.moonshot_schema import is_moonshot_model, sanitize_moonshot_tools
from agent.prompt_builder import DEVELOPER_ROLE_MODELS
from agent.transports.base import ProviderTransport
from agent.transports.types import NormalizedResponse, ToolCall, Usage


def _build_gemini_thinking_config(model: str, reasoning_config: dict | None) -> dict | None:
    """将 Hermes/OpenRouter 风格的推理配置翻译为 Gemini thinkingConfig。"""
    if reasoning_config is None or not isinstance(reasoning_config, dict):
        return None

    normalized_model = (model or "").strip().lower()
    if normalized_model.startswith("google/"):
        normalized_model = normalized_model.split("/", 1)[1]

    # ``thinking_config`` 是仅限 Gemini 的请求参数。同一个 ``gemini`` 提供商
    # 还服务于 Gemma（以及历史上的 PaLM/Bard）；这些模型会以 HTTP 400
    # "Unknown name 'thinking_config': Cannot find field" 拒绝该字段——
    # 包括友好的 ``{"includeThoughts": False}`` 形式。
    # 在非 Gemini 模型上完全省略该字段。(#17426)
    if not normalized_model.startswith("gemini"):
        return None

    if reasoning_config.get("enabled") is False:
        # Gemini 即使在内部仍然进行思考时也可以隐藏思考部分；
        # 省略 thinkingLevel 以避免模型特定的验证异常。
        return {"includeThoughts": False}

    effort = str(reasoning_config.get("effort", "medium") or "medium").strip().lower()
    if effort == "none":
        return {"includeThoughts": False}

    thinking_config: Dict[str, Any] = {"includeThoughts": True}

    # Gemini 2.5 接受 thinkingBudget；不要从 Hermes 的粗粒度努力级别猜测预算。
    # 仅 ``includeThoughts`` 就足以呈现思考部分，且不会触发请求验证错误。
    if normalized_model.startswith("gemini-2.5-"):
        return thinking_config

    if effort not in {"minimal", "low", "medium", "high", "xhigh"}:
        effort = "medium"

    # Gemini 3 Flash 文档记载了 low/medium/high 思考级别；Gemini 3 Pro
    # 更严格（low/high）。将 Hermes 更宽泛的 effort 集合限制到每个
    # 系列实际接受的值，从不原样转发未记录的级别。
    if normalized_model.startswith(("gemini-3", "gemini-3.1")):
        if "flash" in normalized_model:
            if effort in {"minimal", "low"}:
                thinking_config["thinkingLevel"] = "low"
            elif effort in {"high", "xhigh"}:
                thinking_config["thinkingLevel"] = "high"
            else:
                thinking_config["thinkingLevel"] = "medium"
        elif "pro" in normalized_model:
            thinking_config["thinkingLevel"] = (
                "high" if effort in {"high", "xhigh"} else "low"
            )

    return thinking_config


def _snake_case_gemini_thinking_config(config: dict | None) -> dict | None:
    """将 Gemini thinking config 的键转换为 OpenAI 兼容的字段名（蛇形命名）。"""
    if not isinstance(config, dict) or not config:
        return None

    translated: Dict[str, Any] = {}
    if isinstance(config.get("includeThoughts"), bool):
        translated["include_thoughts"] = config["includeThoughts"]
    if isinstance(config.get("thinkingLevel"), str) and config["thinkingLevel"].strip():
        translated["thinking_level"] = config["thinkingLevel"].strip().lower()
    if isinstance(config.get("thinkingBudget"), (int, float)):
        translated["thinking_budget"] = int(config["thinkingBudget"])
    return translated or None


def _is_gemini_openai_compat_base_url(base_url: Any) -> bool:
    normalized = str(base_url or "").strip().rstrip("/").lower()
    if not normalized:
        return False
    if "generativelanguage.googleapis.com" not in normalized:
        return False
    return normalized.endswith("/openai")


def _model_consumes_thought_signature(model: Any) -> bool:
    """当目标模型是需要在工具调用中重放 ``extra_content``（thought_signature）的
    Gemini 系列模型时返回 True。

    Gemini 3 思考模型会在每个工具调用上附加 ``extra_content``，
    若后续请求中缺失则以 HTTP 400 拒绝。其他所有严格的 OpenAI 兼容提供商
    （Fireworks、Mistral 等）若 ``extra_content`` *存在* 则以 400 拒绝请求。
    因此该字段只有在目标模型本身属于 Gemini 系列时才保留，否则一律去除——
    包括非 Gemini 模型从混合提供商会话早期继承了过期 Gemini ``extra_content`` 的情况。
    """
    m = str(model or "").lower()
    return "gemini" in m or "gemma" in m


class ChatCompletionsTransport(ProviderTransport):
    """api_mode='chat_completions' 的传输实现。

    OpenAI 兼容提供商的默认路径。
    """

    @property
    def api_mode(self) -> str:
        return "chat_completions"

    def convert_messages(
        self, messages: list[dict[str, Any]], **kwargs
    ) -> list[dict[str, Any]]:
        """消息已经是 OpenAI 格式——去除严格的 chat-completions 提供商
        会以 HTTP 400/422（或部分 OpenAI 兼容网关的 5xx）拒绝的内部字段：

        - Codex Responses API 字段：消息上的 ``codex_reasoning_items`` /
          ``codex_message_items``，``tool_calls`` 条目上的 ``call_id`` /
          ``response_item_id``。
        - ``tool_calls`` 上的 ``extra_content``（Gemini thought_signature）——
          除非目标 ``model`` 本身是 Gemini 系列，否则去除。
          Gemini 3 思考模型会附加它用于重放，但严格的提供商（Fireworks、Mistral）
          会以 ``Extra inputs are not permitted, field: 'messages[N].tool_calls[M].extra_content'``
          拒绝任何包含它的 payload。Gemini 目标必须保留（需要重放），
          其他所有情况都应丢弃，包括在混合提供商会话中继承了过期 Gemini ``extra_content`` 的非 Gemini 模型。
        - 工具结果消息上的 ``tool_name``——由 ``make_tool_result_message()`` 为 SQLite FTS 索引写入，
          但不是 Chat Completions schema 的一部分。严格的提供商（Fireworks、Moonshot/Kimi）会以
          ``Extra inputs are not permitted, field: 'messages[N].tool_name'`` 拒绝任何包含它的 payload。
          宽松的提供商（OpenRouter、MiniMax）会静默忽略该字段，这掩盖了这个 bug 数月之久。
        - Hermes 内部脚手架标记——任何以 ``_`` 开头的顶层消息键
          （如 ``_empty_recovery_synthetic``、``_empty_terminal_sentinel``、``_thinking_prefill``）。
          这些是 agent 循环附加到消息上的记账标志，以便持久化层后续可以剥离自身脚手架；
          它们绝不能到达网络层。宽松的提供商（真实 OpenAI、Anthropic）会静默丢弃未知消息键，
          但严格的网关（如 opencode-go、codex.nekos.me）会以
          ``Extra inputs are not permitted, field: 'messages[N]._empty_recovery_synthetic'`` 拒绝，
          进而污染会话中的每一个后续请求。
        """
        strip_extra_content = not _model_consumes_thought_signature(
            kwargs.get("model")
        )
        needs_sanitize = False
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if (
                "codex_reasoning_items" in msg
                or "codex_message_items" in msg
                or "tool_name" in msg
                or "timestamp" in msg  # #47868 — 严格提供商会拒绝此字段
            ):
                needs_sanitize = True
                break
            if any(isinstance(k, str) and k.startswith("_") for k in msg):
                needs_sanitize = True
                break
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict) and (
                        "call_id" in tc
                        or "response_item_id" in tc
                        or (strip_extra_content and "extra_content" in tc)
                    ):
                        needs_sanitize = True
                        break
                if needs_sanitize:
                    break

        if not needs_sanitize:
            return messages

        sanitized = copy.deepcopy(messages)
        for msg in sanitized:
            if not isinstance(msg, dict):
                continue
            msg.pop("codex_reasoning_items", None)
            msg.pop("codex_message_items", None)
            msg.pop("tool_name", None)
            msg.pop("timestamp", None)  # #47868 — 避免泄露到严格提供商
            # 丢弃所有 Hermes 内部脚手架标记（以 ``_`` 开头的键）。
            # OpenAI 的消息 schema 中没有 ``_`` 前缀的字段，所以这是安全的，
            # 也能对未来新增标记进行前瞻性防护。
            for key in [k for k in msg if isinstance(k, str) and k.startswith("_")]:
                msg.pop(key, None)
            tool_calls = msg.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        tc.pop("call_id", None)
                        tc.pop("response_item_id", None)
                        if strip_extra_content:
                            tc.pop("extra_content", None)
        return sanitized

    def convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """工具已经是 OpenAI 格式——恒等变换。"""
        return tools

    def build_kwargs(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **params,
    ) -> dict[str, Any]:
        """构建 chat.completions.create() 的关键字参数。

        params（均为可选）:
            timeout: float — API 调用超时
            max_tokens: int | None — 用户配置的最大 token 数
            ephemeral_max_output_tokens: int | None — 单次覆盖
            max_tokens_param_fn: callable — 返回 {max_tokens: N} 或 {max_completion_tokens: N}
            reasoning_config: dict | None
            request_overrides: dict | None
            session_id: str | None
            model_lower: str — 用于模式匹配的小写模型名
            # 提供商配置文件路径（所有提供商特定的怪癖都在 providers/ 中）
            provider_profile: ProviderProfile | None — 存在时委托给
                _build_kwargs_from_profile()；以下所有标志参数均被绕过。
            # 传统路径标志——仅在 provider_profile 为 None 时使用
            # （即自定义/未注册提供商）。已知提供商都通过 provider_profile。
            is_openrouter: bool
            is_nous: bool
            is_qwen_portal: bool
            is_github_models: bool
            is_nvidia_nim: bool
            is_kimi: bool
            is_tokenhub: bool
            is_lmstudio: bool
            is_custom_provider: bool
            ollama_num_ctx: int | None
            # 提供商路由
            provider_preferences: dict | None
            # Qwen 特定
            qwen_prepare_fn: callable | None — 在 codex 清理之后运行
            qwen_prepare_inplace_fn: callable | None — 用于深拷贝列表的原地变体
            qwen_session_metadata: dict | None
            # 温度
            fixed_temperature: Any — 来自 _fixed_temperature_for_model()
            omit_temperature: bool
            # 推理
            supports_reasoning: bool
            github_reasoning_extra: dict | None
            lmstudio_reasoning_options: list[str] | None  # 来自 /api/v1/models 的原始 allowed_options
            # Claude 在 OpenRouter/Nous 上的最大输出
            anthropic_max_output: int | None
            extra_body_additions: dict | None
        """
        # Codex 清理：丢弃 reasoning_items / call_id / response_item_id。
        # 传入 model，以便 Gemini thought_signature（extra_content）在 Gemini 目标中保留，
        # 在严格的非 Gemini 提供商中被去除。
        sanitized = self.convert_messages(messages, model=model)

        # ── 提供商配置文件：存在时走单一路径 ──────────────────
        _profile = params.get("provider_profile")
        if _profile:
            return self._build_kwargs_from_profile(
                _profile, model, sanitized, tools, params
            )

        # ── 传统回退（未注册/未知提供商）───────────
        # 仅当 get_provider_profile() 返回 None 时到达此处。
        # 已知提供商均通过上面的配置文件路径。

        # GPT-5/Codex 模型的开发者角色替换
        model_lower = params.get("model_lower", (model or "").lower())
        if (
            sanitized
            and isinstance(sanitized[0], dict)
            and sanitized[0].get("role") == "system"
            and any(p in model_lower for p in DEVELOPER_ROLE_MODELS)
        ):
            sanitized = list(sanitized)
            sanitized[0] = {**sanitized[0], "role": "developer"}

        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": sanitized,
        }

        timeout = params.get("timeout")
        if timeout is not None:
            api_kwargs["timeout"] = timeout

        # 工具
        if tools:
            # Moonshot/Kimi 使用更严格的 JSON Schema 变体。在此重写工具参数
            # 可使聚合器路由（Nous、OpenRouter 等）以及直接的 moonshot.ai 端点保持兼容。
            if is_moonshot_model(model):
                tools = sanitize_moonshot_tools(tools)
            api_kwargs["tools"] = tools

        # max_tokens 解析——优先级：ephemeral > 用户配置 > 提供商默认
        max_tokens_fn = params.get("max_tokens_param_fn")
        ephemeral = params.get("ephemeral_max_output_tokens")
        max_tokens = params.get("max_tokens")
        anthropic_max_out = params.get("anthropic_max_output")
        is_nvidia_nim = params.get("is_nvidia_nim", False)
        is_kimi = params.get("is_kimi", False)
        is_tokenhub = params.get("is_tokenhub", False)
        reasoning_config = params.get("reasoning_config")

        if ephemeral is not None and max_tokens_fn:
            api_kwargs.update(max_tokens_fn(ephemeral))
        elif max_tokens is not None and max_tokens_fn:
            api_kwargs.update(max_tokens_fn(max_tokens))
        elif anthropic_max_out is not None:
            api_kwargs["max_tokens"] = anthropic_max_out

        # Kimi：顶层 reasoning_effort（除非思考被禁用）
        if is_kimi:
            _kimi_thinking_off = bool(
                reasoning_config
                and isinstance(reasoning_config, dict)
                and reasoning_config.get("enabled") is False
            )
            if not _kimi_thinking_off:
                _kimi_effort = "medium"
                if reasoning_config and isinstance(reasoning_config, dict):
                    _e = (reasoning_config.get("effort") or "").strip().lower()
                    if _e in {"low", "medium", "high"}:
                        _kimi_effort = _e
                api_kwargs["reasoning_effort"] = _kimi_effort

        # 腾讯 TokenHub：顶层 reasoning_effort（除非思考被禁用）
        if is_tokenhub:
            _tokenhub_thinking_off = bool(
                reasoning_config
                and isinstance(reasoning_config, dict)
                and reasoning_config.get("enabled") is False
            )
            if not _tokenhub_thinking_off:
                _tokenhub_effort = "high"
                if reasoning_config and isinstance(reasoning_config, dict):
                    _e = (reasoning_config.get("effort") or "").strip().lower()
                    if _e in {"low", "medium", "high"}:
                        _tokenhub_effort = _e
                api_kwargs["reasoning_effort"] = _tokenhub_effort

        # LM Studio：顶层 reasoning_effort。仅当模型通过 /api/v1/models 能力
        # 声明支持推理时才发出（由上游 params["supports_reasoning"] 门控）。
        # resolve_lmstudio_effort 与 run_agent 的摘要路径共享，以保持同步。
        if params.get("is_lmstudio", False) and params.get("supports_reasoning", False):
            _lm_effort = resolve_lmstudio_effort(
                reasoning_config,
                params.get("lmstudio_reasoning_options"),
            )
            if _lm_effort is not None:
                api_kwargs["reasoning_effort"] = _lm_effort

        # extra_body 组装
        extra_body: dict[str, Any] = {}

        is_openrouter = params.get("is_openrouter", False)
        is_nous = params.get("is_nous", False)
        is_github_models = params.get("is_github_models", False)
        provider_name = str(params.get("provider_name") or "").strip().lower()
        base_url = params.get("base_url")

        provider_prefs = params.get("provider_preferences")
        if provider_prefs and is_openrouter:
            extra_body["provider"] = provider_prefs

        # Pareto Code 路由插件——受模型门控。与
        # plugins/model-providers/openrouter/__init__.py 中的配置文件路径形状相同；
        # 此分支仅在 OpenRouter 配置文件未加载时运行。
        if is_openrouter and model == "openrouter/pareto-code":
            _pareto_score = params.get("openrouter_min_coding_score")
            if _pareto_score is not None and _pareto_score != "":
                try:
                    _pareto_score_f = float(_pareto_score)
                except (TypeError, ValueError):
                    _pareto_score_f = None
                if _pareto_score_f is not None and 0.0 <= _pareto_score_f <= 1.0:
                    extra_body["plugins"] = [
                        {"id": "pareto-router", "min_coding_score": _pareto_score_f}
                    ]

        # Kimi extra_body.thinking 配置
        if is_kimi:
            _kimi_thinking_enabled = True
            if reasoning_config and isinstance(reasoning_config, dict):
                if reasoning_config.get("enabled") is False:
                    _kimi_thinking_enabled = False
            extra_body["thinking"] = {
                "type": "enabled" if _kimi_thinking_enabled else "disabled",
            }

        # 推理配置。LM Studio 已通过上面的顶层 reasoning_effort 处理，
        # 因此跳过为其发出 extra_body.reasoning。
        if params.get("supports_reasoning", False) and not params.get("is_lmstudio", False):
            if is_github_models:
                gh_reasoning = params.get("github_reasoning_extra")
                if gh_reasoning is not None:
                    extra_body["reasoning"] = gh_reasoning
            else:
                extra_body["reasoning"] = {"enabled": True, "effort": "medium"}

        if provider_name == "gemini":
            raw_thinking_config = _build_gemini_thinking_config(model, reasoning_config)
            if _is_gemini_openai_compat_base_url(base_url):
                thinking_config = _snake_case_gemini_thinking_config(raw_thinking_config)
                if thinking_config:
                    openai_compat_extra = extra_body.get("extra_body", {})
                    google_extra = openai_compat_extra.get("google", {})
                    google_extra["thinking_config"] = thinking_config
                    openai_compat_extra["google"] = google_extra
                    extra_body["extra_body"] = openai_compat_extra
            elif raw_thinking_config:
                extra_body["thinking_config"] = raw_thinking_config

        # 合并任何预构建的 extra_body 附加内容
        additions = params.get("extra_body_additions")
        if additions:
            extra_body.update(additions)

        if extra_body:
            api_kwargs["extra_body"] = extra_body

        # 最后应用请求覆盖（service_tier 等）
        overrides = params.get("request_overrides")
        if overrides:
            api_kwargs.update(overrides)

        return api_kwargs

    def _build_kwargs_from_profile(self, profile, model, sanitized, tools, params):
        """使用 ProviderProfile 构建 API kwargs——单一路径，无传统标志。

        当传入 provider_profile 时，此方法替换整个基于标志的 kwargs 组装。
        所有特殊处理均来自配置文件对象。
        """
        from providers.base import OMIT_TEMPERATURE

        # 消息预处理
        sanitized = profile.prepare_messages(sanitized)

        # 开发者角色替换——基于模型名称，适用于所有提供商
        _model_lower = (model or "").lower()
        if (
            sanitized
            and isinstance(sanitized[0], dict)
            and sanitized[0].get("role") == "system"
            and any(p in _model_lower for p in DEVELOPER_ROLE_MODELS)
        ):
            sanitized = list(sanitized)
            sanitized[0] = {**sanitized[0], "role": "developer"}

        api_kwargs: dict[str, Any] = {
            "model": model,
            "messages": sanitized,
        }

        # 温度
        if profile.fixed_temperature is OMIT_TEMPERATURE:
            pass  # 完全不包含 temperature
        elif profile.fixed_temperature is not None:
            api_kwargs["temperature"] = profile.fixed_temperature
        else:
            # 若调用方提供了 temperature 则使用
            temp = params.get("temperature")
            if temp is not None:
                api_kwargs["temperature"] = temp

        # 超时
        timeout = params.get("timeout")
        if timeout is not None:
            api_kwargs["timeout"] = timeout

        # 工具——无论路径如何都应用 Moonshot/Kimi schema 清理
        if tools:
            if is_moonshot_model(model):
                tools = sanitize_moonshot_tools(tools)
            api_kwargs["tools"] = tools

        # max_tokens 解析——优先级：ephemeral > 用户配置 > 配置文件默认
        max_tokens_fn = params.get("max_tokens_param_fn")
        ephemeral = params.get("ephemeral_max_output_tokens")
        user_max = params.get("max_tokens")
        anthropic_max = params.get("anthropic_max_output")
        # 每个模型的默认上限——当配置文件前置多个具有不同 completion token 限制的后端时
        # 会覆盖 get_max_tokens()（例如 opencode-go: mimo-v2.5-pro = 131072）。
        profile_max = profile.get_max_tokens(model)

        if ephemeral is not None and max_tokens_fn:
            api_kwargs.update(max_tokens_fn(ephemeral))
        elif user_max is not None and max_tokens_fn:
            api_kwargs.update(max_tokens_fn(user_max))
        elif profile_max and max_tokens_fn:
            api_kwargs.update(max_tokens_fn(profile_max))
        elif anthropic_max is not None:
            api_kwargs["max_tokens"] = anthropic_max

        # 提供商特定的 api_kwargs 附加内容（reasoning_effort、metadata 等）
        reasoning_config = params.get("reasoning_config")
        extra_body_from_profile, top_level_from_profile = (
            profile.build_api_kwargs_extras(
                reasoning_config=reasoning_config,
                supports_reasoning=params.get("supports_reasoning", False),
                qwen_session_metadata=params.get("qwen_session_metadata"),
                model=model,
                base_url=params.get("base_url"),
                ollama_num_ctx=params.get("ollama_num_ctx"),
                session_id=params.get("session_id"),
            )
        )
        api_kwargs.update(top_level_from_profile)

        # extra_body 组装
        extra_body: dict[str, Any] = {}

        # 配置文件的 extra_body（tags、提供商偏好、vl_high_resolution 等）
        profile_body = profile.build_extra_body(
            session_id=params.get("session_id"),
            provider_preferences=params.get("provider_preferences"),
            model=model,
            base_url=params.get("base_url"),
            reasoning_config=reasoning_config,
            openrouter_min_coding_score=params.get("openrouter_min_coding_score"),
        )
        if profile_body:
            extra_body.update(profile_body)

        # 配置文件的推理/思考 extra_body 条目
        if extra_body_from_profile:
            extra_body.update(extra_body_from_profile)

        # 合并调用方的任何预构建 extra_body 附加内容
        additions = params.get("extra_body_additions")
        if additions:
            extra_body.update(additions)

        # 请求覆盖（用户配置）
        overrides = params.get("request_overrides")
        if overrides:
            for k, v in overrides.items():
                if k == "extra_body" and isinstance(v, dict):
                    extra_body.update(v)
                else:
                    api_kwargs[k] = v

        if extra_body:
            # 原生 Gemini（generativelanguage.googleapis.com，非 /openai 路径）
            # 使用 Google 的 REST schema 而非 OpenAI 的。OpenAI 风格的 extra_body
            # 键（tags、reasoning、provider、plugins 等）在那里是未知字段，
            # Gemini 会以不可重试的 HTTP 400（"Invalid JSON payload received. Unknown name 'tags'"）
            # 拒绝整个请求。当一个会发出 extra_body 的配置文件（如 Nous 配置文件的 portal `tags`）
            # 处于活动状态，但解析出的端点是 Gemini base_url 时就会发生这种情况——
            # 通常是只设置了 Google 凭证，fallback/aux 调用落到了 Gemini 上。
            # 原生客户端只读取 extra_body 中的 thinking_config，所以在此丢弃其他所有内容。
            try:
                from agent.gemini_native_adapter import is_native_gemini_base_url
                _native_gemini = is_native_gemini_base_url(params.get("base_url"))
            except Exception:
                _native_gemini = False
            if _native_gemini:
                extra_body = {
                    k: v for k, v in extra_body.items()
                    if k in ("thinking_config", "thinkingConfig")
                }
            if extra_body:
                api_kwargs["extra_body"] = extra_body

        return api_kwargs

    def normalize_response(self, response: Any, **kwargs) -> NormalizedResponse:
        """Normalize OpenAI ChatCompletion to NormalizedResponse.

        For chat_completions, this is near-identity — the response is already
        in OpenAI format.  extra_content on tool_calls (Gemini thought_signature)
        is preserved via ToolCall.provider_data.  reasoning_details (OpenRouter
        unified format) and reasoning_content (DeepSeek/Moonshot) are also
        preserved for downstream replay.
        """
        choice = response.choices[0]
        msg = choice.message
        finish_reason = choice.finish_reason or "stop"

        tool_calls = None
        if msg.tool_calls:
            tool_calls = []
            for tc in msg.tool_calls:
                # Preserve provider-specific extras on the tool call.
                # Gemini 3 thinking models attach extra_content with
                # thought_signature — without replay on the next turn the API
                # rejects the request with 400.
                tc_provider_data: dict[str, Any] = {}
                extra = getattr(tc, "extra_content", None)
                if extra is None and hasattr(tc, "model_extra"):
                    extra = (tc.model_extra or {}).get("extra_content")
                if extra is not None:
                    if hasattr(extra, "model_dump"):
                        try:
                            extra = extra.model_dump()
                        except Exception:
                            pass
                    tc_provider_data["extra_content"] = extra
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=tc.function.arguments,
                        provider_data=tc_provider_data or None,
                    )
                )

        usage = None
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            usage = Usage(
                prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(u, "completion_tokens", 0) or 0,
                total_tokens=getattr(u, "total_tokens", 0) or 0,
            )

        # Preserve reasoning fields separately.  DeepSeek/Moonshot use
        # ``reasoning_content``; others use ``reasoning``.  Downstream code
        # (_extract_reasoning, thinking-prefill retry) reads both distinctly,
        # so keep them apart in provider_data rather than merging.
        reasoning = getattr(msg, "reasoning", None)
        reasoning_content = getattr(msg, "reasoning_content", None)
        if reasoning_content is None and hasattr(msg, "model_extra"):
            model_extra = getattr(msg, "model_extra", None) or {}
            if isinstance(model_extra, dict) and "reasoning_content" in model_extra:
                reasoning_content = model_extra["reasoning_content"]

        provider_data: Dict[str, Any] = {}
        if reasoning_content is not None:
            provider_data["reasoning_content"] = reasoning_content
        rd = getattr(msg, "reasoning_details", None)
        if rd:
            provider_data["reasoning_details"] = rd

        # OpenAI structured-refusal field. When a model declines, the SDK
        # populates ``message.refusal`` with the explanation and leaves
        # ``content`` empty. OpenAI-compatible proxies that front Anthropic /
        # Bedrock (e.g. Nous Portal) surface a Claude refusal this way — or via
        # ``finish_reason="content_filter"`` — instead of the native
        # ``stop_reason="refusal"``. Without capturing it the refusal looks
        # like an empty response, so the agent loop retries a deterministic
        # refusal three times and gives up with "no content after retries".
        # Promote it to content + a ``content_filter`` finish reason so the
        # loop's refusal handler surfaces it clearly and stops. ``refusal`` is
        # ``None`` for normal responses, so this is a no-op in the common case.
        content = msg.content
        refusal = getattr(msg, "refusal", None)
        if refusal is None and hasattr(msg, "model_extra"):
            _msg_extra = getattr(msg, "model_extra", None) or {}
            if isinstance(_msg_extra, dict):
                refusal = _msg_extra.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            # Record the refusal explanation regardless — it's useful provider
            # metadata even when the model also returned a usable payload.
            provider_data["refusal"] = refusal
            _has_text = isinstance(content, str) and content.strip()
            _has_tool_calls = bool(tool_calls)
            # Only promote to a terminal ``content_filter`` when the refusal is
            # the *sole* payload — no visible text and no tool calls. A response
            # that carries real content (or tool calls) alongside a refusal note
            # is a normal, usable turn: surfacing it as a failed safety refusal
            # would discard the model's actual work. In the empty-payload case,
            # adopt the refusal as content so the loop has something to show.
            if not _has_text and not _has_tool_calls:
                content = refusal
                if finish_reason in (None, "stop"):
                    finish_reason = "content_filter"

        return NormalizedResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning=reasoning,
            usage=usage,
            provider_data=provider_data or None,
        )

    def validate_response(self, response: Any) -> bool:
        """Check that response has valid choices."""
        if response is None:
            return False
        if not hasattr(response, "choices") or response.choices is None:
            return False
        if not response.choices:
            return False
        return True

    def extract_cache_stats(self, response: Any) -> dict[str, int] | None:
        """Extract OpenRouter/OpenAI cache stats from prompt_tokens_details."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        details = getattr(usage, "prompt_tokens_details", None)
        if details is None:
            return None
        cached = getattr(details, "cached_tokens", 0) or 0
        written = getattr(details, "cache_write_tokens", 0) or 0
        if cached or written:
            return {"cached_tokens": cached, "creation_tokens": written}
        return None


# Auto-register on import
from agent.transports import register_transport  # noqa: E402

register_transport("chat_completions", ChatCompletionsTransport)
