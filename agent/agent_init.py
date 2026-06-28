"""实现 :meth:`AIAgent.__init__` —— 提取为模块函数。

``AIAgent.__init__`` 是代码库中最长的方法之一（60+ 个参数，
约 1,400 行属性初始化、提供商自动检测、凭据解析、上下文引擎引导等）。
将其保留在 ``run_agent.py`` 中会使该文件膨胀，其中大部分代码是
"设置状态后即遗忘"。

提取后，函数主体作为 ``init_agent(agent, ...)`` 存在于此处，
:meth:`AIAgent.__init__` 是一个简单的包装器，调用
``init_agent(self, ...)``。模块加载时主体所需的所有导入都列在下面；
主体还在其自身作用域内执行许多延迟导入，这些导入保持不变。

测试在 ``run_agent.*`` 上打补丁的符号（``OpenAI``、``cleanup_vm`` 等）
通过 :func:`_ra` 解析，因此补丁契约得以保留。
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse, parse_qs, urlunparse

from agent.context_compressor import ContextCompressor
from agent.iteration_budget import IterationBudget
from agent.memory_manager import StreamingContextScrubber
from agent.model_metadata import (
    MINIMUM_CONTEXT_LENGTH,
    fetch_model_metadata,
    is_local_endpoint,
    query_ollama_num_ctx,
)
from agent.process_bootstrap import _install_safe_stdio
from agent.subdirectory_hints import SubdirectoryHintTracker
from agent.think_scrubber import StreamingThinkScrubber
from agent.tool_guardrails import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
    ToolGuardrailDecision,
)
from hermes_cli.config import cfg_get
from hermes_cli.timeouts import get_provider_request_timeout
from hermes_constants import get_hermes_home
from utils import base_url_host_matches, is_truthy_value

# 使用与 run_agent 相同的记录器名称，以便测试打补丁 ``run_agent.logger``
# 能够捕获我们的警告。（run_agent.py 也执行
# ``logger = logging.getLogger(__name__)``，在该模块内解析为 "run_agent"。）
logger = logging.getLogger("run_agent")


def _ra():
    """对 ``run_agent`` 的延迟引用，以便调用方可以打补丁
    ``run_agent.OpenAI`` / ``run_agent.cleanup_vm`` / ...，并使这些补丁
    到达此代码路径。
    """
    import run_agent
    return run_agent


def _build_codex_gpt55_autoraise_notice(autoraise: Dict[str, float]) -> str:
    """构建当 Codex gpt-5.5 提升压缩比例时显示的一次性通知。

    ``autoraise`` 是 ``{"from": <old_ratio>, "to": <new_ratio>}``。
    相同的文本为 CLI 用户内联打印，并通过 ``status_callback``
    为网关用户重播，因此它必须是自包含的，并包含确切的
    退出选择命令。
    """
    from_pct = int(round(autoraise["from"] * 100))
    to_pct = int(round(autoraise["to"] * 100))
    return (
        f"ℹ Codex gpt-5.5 将上下文限制在 272K，因此自动压缩比例已提高到 "
        f"{to_pct}%（从 {from_pct}%），以便在总结前使用更多窗口。\n"
        f"  退出选择：hermes config set compression.codex_gpt55_autoraise false"
    )


def _normalized_custom_base_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/")


def _custom_provider_model_matches(agent_model: str, entry: Dict[str, Any]) -> bool:
    provider_model = str(entry.get("model", "") or "").strip().lower()
    if not provider_model:
        return True
    return provider_model == str(agent_model or "").strip().lower()


def _custom_provider_extra_body_for_agent(
    *,
    provider: str,
    model: str,
    base_url: str,
    custom_providers: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    provider_norm = (provider or "").strip().lower()
    if provider_norm == "custom":
        provider_key_filter = ""
    elif provider_norm.startswith("custom:"):
        provider_key_filter = provider_norm.split(":", 1)[1].strip()
    else:
        return None

    target_url = _normalized_custom_base_url(base_url)
    if not target_url:
        return None

    fallback: Optional[Dict[str, Any]] = None
    for entry in custom_providers or []:
        if not isinstance(entry, dict):
            continue
        if provider_key_filter:
            entry_keys = {
                str(entry.get("provider_key", "") or "").strip().lower(),
                str(entry.get("name", "") or "").strip().lower(),
            }
            if provider_key_filter not in entry_keys:
                continue
        if _normalized_custom_base_url(entry.get("base_url")) != target_url:
            continue
        extra_body = entry.get("extra_body")
        if not isinstance(extra_body, dict) or not extra_body:
            continue
        provider_model = str(entry.get("model", "") or "").strip()
        if provider_model:
            if _custom_provider_model_matches(model, entry):
                return dict(extra_body)
        elif fallback is None:
            fallback = dict(extra_body)

    return fallback


def _merge_custom_provider_extra_body(agent, custom_providers: List[Dict[str, Any]]) -> None:
    extra_body = _custom_provider_extra_body_for_agent(
        provider=agent.provider,
        model=agent.model,
        base_url=agent.base_url,
        custom_providers=custom_providers,
    )
    if not extra_body:
        return

    overrides = dict(getattr(agent, "request_overrides", {}) or {})
    merged_extra_body = dict(extra_body)
    existing_extra_body = overrides.get("extra_body")
    if isinstance(existing_extra_body, dict):
        merged_extra_body.update(existing_extra_body)
    overrides["extra_body"] = merged_extra_body
    agent.request_overrides = overrides


def init_agent(
    agent,
    base_url: str = None,
    api_key: str = None,
    provider: str = None,
    api_mode: str = None,
    acp_command: str = None,
    acp_args: list[str] | None = None,
    command: str = None,
    args: list[str] | None = None,
    model: str = "",
    max_iterations: int = 90,  # 默认工具调用迭代次数（与子代理共享）
    tool_delay: float = 1.0,
    enabled_toolsets: List[str] = None,
    disabled_toolsets: List[str] = None,
    save_trajectories: bool = False,
    verbose_logging: bool = False,
    quiet_mode: bool = False,
    tool_progress_mode: str = "all",
    ephemeral_system_prompt: str = None,
    log_prefix_chars: int = 100,
    log_prefix: str = "",
    providers_allowed: List[str] = None,
    providers_ignored: List[str] = None,
    providers_order: List[str] = None,
    provider_sort: str = None,
    provider_require_parameters: bool = False,
    provider_data_collection: str = None,
    openrouter_min_coding_score: Optional[float] = None,
    session_id: str = None,
    tool_progress_callback: callable = None,
    tool_start_callback: callable = None,
    tool_complete_callback: callable = None,
    thinking_callback: callable = None,
    reasoning_callback: callable = None,
    clarify_callback: callable = None,
    read_terminal_callback: callable = None,
    step_callback: callable = None,
    stream_delta_callback: callable = None,
    interim_assistant_callback: callable = None,
    tool_gen_callback: callable = None,
    status_callback: callable = None,
    notice_callback: callable = None,
    notice_clear_callback: callable = None,
    event_callback: Optional[Callable[[str, dict], None]] = None,
    max_tokens: int = None,
    reasoning_config: Dict[str, Any] = None,
    service_tier: str = None,
    request_overrides: Dict[str, Any] = None,
    prefill_messages: List[Dict[str, Any]] = None,
    platform: str = None,
    user_id: str = None,
    user_id_alt: str = None,
    user_name: str = None,
    chat_id: str = None,
    chat_name: str = None,
    chat_type: str = None,
    thread_id: str = None,
    gateway_session_key: str = None,
    skip_context_files: bool = False,
    load_soul_identity: bool = False,
    skip_memory: bool = False,
    session_db=None,
    parent_session_id: str = None,
    iteration_budget: "IterationBudget" = None,
    fallback_model: Dict[str, Any] = None,
    credential_pool=None,
    checkpoints_enabled: bool = False,
    checkpoint_max_snapshots: int = 20,
    checkpoint_max_total_size_mb: int = 500,
    checkpoint_max_file_size_mb: int = 10,
    pass_session_id: bool = False,
):
    """
    初始化 AI 代理。

    参数：
        base_url (str): 模型 API 的基础 URL（可选）
        api_key (str): 用于身份验证的 API 密钥（可选，如未提供则使用环境变量）
        provider (str): 提供商标识符（可选；用于遥测/路由提示）
        api_mode (str): API 模式覆盖："chat_completions" 或 "codex_responses"
        model (str): 要使用的模型名称（默认："anthropic/claude-opus-4.6"）
        max_iterations (int): 工具调用迭代的最大次数（默认：90）
        tool_delay (float): 工具调用之间的延迟（秒）（默认：1.0）
        enabled_toolsets (List[str]): 仅启用这些工具集中的工具（可选）
        disabled_toolsets (List[str]): 禁用这些工具集中的工具（可选）
        save_trajectories (bool): 是否将对话轨迹保存到 JSONL 文件（默认：False）
        verbose_logging (bool): 启用详细日志记录以进行调试（默认：False）
        quiet_mode (bool): 抑制进度输出以获得干净的 CLI 体验（默认：False）
        ephemeral_system_prompt (str): 代理执行期间使用的系统提示，但不保存到轨迹（可选）
        log_prefix_chars (int): 在日志预览中显示的工具调用/响应字符数（默认：100）
        log_prefix (str): 添加到所有日志消息的前缀，用于并行处理中的标识（默认：""）
        providers_allowed (List[str]): 允许的 OpenRouter 提供商（可选）
        providers_ignored (List[str]): 忽略的 OpenRouter 提供商（可选）
        providers_order (List[str]): 按顺序尝试的 OpenRouter 提供商（可选）
        provider_sort (str): 按价格/吞吐量/延迟对提供商排序（可选）
        openrouter_min_coding_score (float): 编码分数下限（0.0-1.0），用于
            openrouter/pareto-code 路由器。仅在 model == "openrouter/pareto-code" 时应用。
            None 或空值 = 让 OpenRouter 选择最强大的可用编码器。
        session_id (str): 预生成的会话 ID 用于日志记录（可选，如未提供则自动生成）
        tool_progress_callback (callable): 回调函数(tool_name, args_preview) 用于进度通知
        clarify_callback (callable): 回调函数(question, choices) -> str 用于交互式用户问题。
            由平台层（CLI 或网关）提供。如果为 None，则澄清工具返回错误。
        max_tokens (int): 模型响应的最大令牌数（可选，如未设置则使用模型默认值）
        reasoning_config (Dict): OpenRouter 推理配置覆盖（例如 {"effort": "none"} 以禁用思考）。
            如果为 None，则默认为 {"enabled": True, "effort": "medium"} 用于 OpenRouter。
            设置为禁用/自定义推理。
        prefill_messages (List[Dict]): 作为预填充上下文添加到对话历史之前的消息。
            用于注入少样本示例或引导模型响应风格。
            示例：[{"role": "user", "content": "嗨！"}, {"role": "assistant", "content": "你好！"}]
            注意：Anthropic Sonnet 4.6+ 和 Opus 4.6+ 拒绝以助手角色消息结尾的对话
            （400 错误）。对于这些模型，请使用结构化输出或 output_config.format，
            而不是尾随助手的预填充。
        platform (str): 用户所在的界面平台（例如 "cli"、"telegram"、"discord"、"whatsapp"）。
            用于向系统提示注入特定于平台的格式提示。
        skip_context_files (bool): 如果为 True，则跳过项目上下文文件的自动注入
            （SOUL.md、.hermes.md、AGENTS.md、CLAUDE.md、.cursorrules），从 cwd / HERMES_HOME
            注入到系统提示。用于批处理和数据生成，以避免用用户特定的角色或项目指令
            污染轨迹。
        load_soul_identity (bool): 如果为 True，即使当 skip_context_files=True 时，
            仍然使用 ~/.hermes/SOUL.md 作为主要身份。来自 cwd 的项目上下文文件仍然被跳过。
    """
    _install_safe_stdio()

    agent.model = model
    agent.max_iterations = max_iterations
    # 共享迭代预算 — 父级创建，子级继承。
    # 在父级 + 所有子代理中消耗的每个 LLM 轮次。
    agent.iteration_budget = iteration_budget or IterationBudget(max_iterations)
    agent.tool_delay = tool_delay
    agent.save_trajectories = save_trajectories
    agent.verbose_logging = verbose_logging
    agent.quiet_mode = quiet_mode
    agent.tool_progress_mode = tool_progress_mode
    agent.ephemeral_system_prompt = ephemeral_system_prompt
    agent.platform = platform  # "cli"、"telegram"、"discord"、"whatsapp" 等
    agent._user_id = user_id  # 平台用户标识符（网关会话）
    agent._user_id_alt = user_id_alt  # 可选的稳定备用平台标识符
    agent._user_name = user_name
    agent._chat_id = chat_id
    agent._chat_name = chat_name
    agent._chat_type = chat_type
    agent._thread_id = thread_id
    agent._gateway_session_key = gateway_session_key  # 每个聊天的稳定密钥（例如 agent:main:telegram:dm:123）
    # 可插拔打印函数 — CLI 用 _cprint 替换此项，以便原始 ANSI 状态行通过
    # prompt_toolkit 的渲染器路由，而不是直接进入 stdout，在 stdout 中
    # patch_stdout 的 StdoutProxy 会破坏转义序列。
    # None = 使用 builtins.print。
    agent._print_fn = None
    agent.background_review_callback = None  # 网关传递的可选同步回调
    agent.memory_notifications = "on"  # 内存更新通知："off"、"on"、"verbose"
    agent.skip_context_files = skip_context_files
    agent.load_soul_identity = load_soul_identity
    agent.pass_session_id = pass_session_id
    agent._credential_pool = credential_pool
    agent.log_prefix_chars = log_prefix_chars
    agent.log_prefix = f"{log_prefix} " if log_prefix else ""
    # 存储有效的基础 URL 用于功能检测（提示缓存、推理等）
    agent.base_url = base_url or ""
    provider_name = provider.strip().lower() if isinstance(provider, str) and provider.strip() else None
    agent.provider = provider_name or ""
    agent.acp_command = acp_command or command
    agent.acp_args = list(acp_args or args or [])
    if api_mode in {"chat_completions", "codex_responses", "anthropic_messages", "bedrock_converse", "codex_app_server"}:
        agent.api_mode = api_mode
    elif agent.provider == "openai-codex":
        agent.api_mode = "codex_responses"
    elif agent.provider in {"xai", "xai-oauth"}:
        agent.api_mode = "codex_responses"
    elif (provider_name is None) and (
        agent._base_url_hostname == "chatgpt.com"
        and "/backend-api/codex" in agent._base_url_lower
    ):
        agent.api_mode = "codex_responses"
        agent.provider = "openai-codex"
    elif (provider_name is None) and agent._base_url_hostname == "api.x.ai":
        agent.api_mode = "codex_responses"
        agent.provider = "xai"
    elif agent.provider == "anthropic" or (provider_name is None and agent._base_url_hostname == "api.anthropic.com"):
        agent.api_mode = "anthropic_messages"
        agent.provider = "anthropic"
    elif agent._base_url_lower.rstrip("/").endswith("/anthropic"):
        # 第三方 Anthropic 兼容端点（例如 MiniMax、DashScope）
        # 使用以 /anthropic 结尾的 URL 约定。自动检测这些，以便
        # 使用 Anthropic 消息 API 适配器而不是聊天补全。
        agent.api_mode = "anthropic_messages"
    elif agent.provider == "bedrock" or (
        agent._base_url_hostname.startswith("bedrock-runtime.")
        and base_url_host_matches(agent._base_url_lower, "amazonaws.com")
    ):
        # AWS Bedrock — 从提供商名称或基础 URL 自动检测
        # （bedrock-runtime.<region>.amazonaws.com）。
        agent.api_mode = "bedrock_converse"
    else:
        agent.api_mode = "chat_completions"

    # 急切预热传输缓存，以便导入错误在初始化时出现，
    # 而不是在对话中期。还验证 api_mode 是否已注册。
    try:
        agent._get_transport()
    except Exception:
        pass  # 非致命 — 传输可能尚未针对所有模式存在

    try:
        from hermes_cli.model_normalize import (
            _AGGREGATOR_PROVIDERS,
            normalize_model_for_provider,
        )

        if agent.provider not in _AGGREGATOR_PROVIDERS:
            agent.model = normalize_model_for_provider(agent.model, agent.provider)
    except Exception:
        pass

    # GPT-5.x 模型通常需要 Responses API 路径，但某些提供商有例外
    # （例如 Copilot 的 gpt-5-mini 仍使用聊天补全）。
    # 还为直接 OpenAI URL 自动升级（api.openai.com），因为所有较新的工具调用模型
    # 在那里更倾向于 Responses。ACP 运行时被排除：CopilotACPClient
    # 处理自己的路由，并且不实现 Responses API 表面。
    # 当明确提供 api_mode 时，请尊重它 — 用户知道其端点支持什么（#10473）。
    # 例外：Azure OpenAI 在 /chat/completions 上提供 gpt-5.x，并且
    # 不支持 Responses API — 跳过 Azure 的升级
    # （openai.azure.com），即使它看起来像 OpenAI 兼容。
    if (
        api_mode is None
        and agent.api_mode == "chat_completions"
        and agent.provider != "copilot-acp"
        and not str(agent.base_url or "").lower().startswith("acp://copilot")
        and not str(agent.base_url or "").lower().startswith("acp+tcp://")
        and not agent._is_azure_openai_url()
        and (
            agent._is_direct_openai_url()
            or agent._provider_model_requires_responses_api(
                agent.model,
                provider=agent.provider,
            )
        )
    ):
        agent.api_mode = "codex_responses"
        # 使急切预热的传输缓存失效 — api_mode 从 chat_completions 更改为
        # codex_responses，在 __init__ 预热之后。
        if hasattr(agent, "_transport_cache"):
            agent._transport_cache.clear()

    # 在后台线程中预热 OpenRouter 模型元数据缓存。
    # fetch_model_metadata() 缓存 1 小时；这避免了在估算定价时
    # 对第一个 API 响应进行阻塞的 HTTP 请求。
    # 使用进程级 Event，因此此线程仅生成一次 — 为每个网关请求创建
    # 一个新的 AIAgent，因此如果没有保护，每条消息都会泄漏一个 OS 线程，
    # 进程最终会耗尽系统线程限制（RuntimeError: can't start new thread）。
    if (agent.provider == "openrouter" or agent._is_openrouter_url()) and \
            not _ra()._openrouter_prewarm_done.is_set():
        _ra()._openrouter_prewarm_done.set()
        threading.Thread(
            target=fetch_model_metadata,
            daemon=True,
            name="openrouter-prewarm",
        ).start()

    agent.tool_progress_callback = tool_progress_callback
    agent.tool_start_callback = tool_start_callback
    agent.tool_complete_callback = tool_complete_callback
    agent.suppress_status_output = False
    agent.thinking_callback = thinking_callback
    agent.reasoning_callback = reasoning_callback
    agent.clarify_callback = clarify_callback
    agent.read_terminal_callback = read_terminal_callback
    agent.step_callback = step_callback
    agent.stream_delta_callback = stream_delta_callback
    agent.interim_assistant_callback = interim_assistant_callback
    agent.status_callback = status_callback
    agent.notice_callback = notice_callback
    agent.notice_clear_callback = notice_clear_callback
    agent.event_callback = event_callback
    agent.tool_gen_callback = tool_gen_callback


    # 工具执行状态 — 即使在注册了流消费者时（那时没有令牌流），
    # 也允许在工具执行期间进行 _vprint
    agent._executing_tools = False
    agent._tool_guardrails = ToolCallGuardrailController()
    agent._tool_guardrail_halt_decision: ToolGuardrailDecision | None = None

    # 中断机制，用于跳出工具循环
    agent._interrupt_requested = False
    agent._interrupt_message = None  # 触发中断的可选消息
    agent._execution_thread_id: int | None = None  # 在 run_conversation() 开始时设置
    agent._interrupt_thread_signal_pending = False
    agent._client_lock = threading.RLock()

    # /steer 机制 — 在不中断代理的情况下将用户注释注入到下一个工具结果中。
    # 与 interrupt() 不同，steer() 不会设置 _interrupt_requested；
    # 它等待当前工具批次自然完成，然后排空钩子将文本附加到
    # 最后一个工具结果的内容，以便模型在下一次迭代时看到它。
    # 保留消息角色交替（我们修改现有的工具消息，而不是插入新的用户轮次）。
    agent._pending_steer: Optional[str] = None
    agent._pending_steer_lock = threading.Lock()

    # 并发工具工作线程跟踪。``_execute_tool_calls_concurrent`` 在每个
    # ThreadPoolExecutor 工作线程上运行每个工具 — 这些工作线程具有与
    # ``_execution_thread_id`` 不同的 tid，因此仅 ``_set_interrupt(True, _execution_thread_id)``
    # 不会导致工作线程内的 ``is_interrupted()`` 返回 True。
    # 在此处跟踪工作线程，以便 ``interrupt()`` / ``clear_interrupt()``
    # 可以明确地分发到它们的 tid。
    agent._tool_worker_threads: set[int] = set()
    agent._tool_worker_threads_lock = threading.Lock()

    # 子代理委托状态
    agent._delegate_depth = 0        # 0 = 顶级代理，为子级递增
    agent._active_children = []      # 运行中的子 AIAgent（用于中断传播）
    agent._active_children_lock = threading.Lock()

    # 存储 OpenRouter 提供商偏好
    agent.providers_allowed = providers_allowed
    agent.providers_ignored = providers_ignored
    agent.providers_order = providers_order
    agent.provider_sort = provider_sort
    agent.provider_require_parameters = provider_require_parameters
    agent.provider_data_collection = provider_data_collection
    agent.openrouter_min_coding_score = openrouter_min_coding_score

    # 存储工具集过滤选项
    agent.enabled_toolsets = enabled_toolsets
    agent.disabled_toolsets = disabled_toolsets

    # 模型响应配置
    agent.max_tokens = max_tokens  # None = 使用模型默认值
    agent.reasoning_config = reasoning_config  # None = 使用默认值（OpenRouter 为中等）
    agent.service_tier = service_tier
    agent.request_overrides = dict(request_overrides or {})
    agent.prefill_messages = prefill_messages or []  # 预填充的对话轮次
    agent._force_ascii_payload = False

    # Anthropic 提示缓存：为本机 Anthropic、OpenRouter 以及
    # 讲述 Anthropic 协议的第三方网关的 Claude 模型自动启用
    # （``api_mode == 'anthropic_messages'``）。在多轮对话中减少输入成本约 75%。
    # 使用 system_and_3 策略（4 个断点）。参见
    # ``_anthropic_prompt_cache_policy`` 了解布局与传输决策。
    agent._use_prompt_caching, agent._use_native_cache_layout = (
        agent._anthropic_prompt_cache_policy()
    )
    # Anthropic 支持 "5m"（默认）和 "1h" 缓存 TTL 层。
    # 从 prompt_caching.cache_ttl 下的 config.yaml 读取；未知值保持 "5m"。
    # 1h 层写入成本是 5m 的 2 倍，但在超过 5 分钟暂停的长会话中分摊（#14971）。
    agent._cache_ttl = "5m"
    try:
        from hermes_cli.config import load_config as _load_pc_cfg

        _pc_cfg = _load_pc_cfg().get("prompt_caching", {}) or {}
        _ttl = _pc_cfg.get("cache_ttl", "5m")
        if _ttl in {"5m", "1h"}:
            agent._cache_ttl = _ttl
    except Exception:
        pass

    # 迭代预算：仅当 LLM 实际耗尽迭代预算时才会通知它
    # （api_call_count >= max_iterations）。此时我们注入一条消息，
    # 允许最后一次 API 调用，如果模型不产生文本响应，则强制一条用户消息
    # 要求它总结。没有中间压力警告 — 它们导致模型在复杂任务上过早"放弃"（#7915）。
    agent._budget_exhausted_injected = False
    agent._budget_grace_call = False

    # 活动跟踪 — 在每次 API 调用、工具执行和流块时更新。
    # 由网关超时处理程序使用，以报告代理在被终止时正在做什么，
    # 以及由"仍在工作"通知使用以显示进度。
    agent._last_activity_ts: float = time.time()
    agent._last_activity_desc: str = "初始化中"
    agent._current_tool: str | None = None
    agent._api_call_count: int = 0
    # 轮次间 MCP 工具刷新的退出标志（build_turn_context）。
    # 在内部分支上设置（例如 background_review），必须保持 ``tools[]`` 字节相同
    # 到父级，以实现提供商缓存奇偶性。
    agent._skip_mcp_refresh = False
    # 当前工具快照派生自的注册表生成。让延迟/并发刷新拒绝过时的（较旧生成）
    # 重建，而不是覆盖更新的重建。在下面的工具快照旁边设置。
    agent._tool_snapshot_generation = 0
    # 速率限制跟踪 — 在每次 API 调用后从 x-ratelimit-* 响应头更新。
    # 由 /usage 斜杠命令访问。
    agent._rate_limit_state: Optional["RateLimitState"] = None

    # 积分跟踪（仅限开发，L0 使用感知积分） — 在每次 API 调用后从
    # x-nous-credits-* 响应头更新。会话开始剩余值在第一次看到头时锁存，
    # 以便我们可以报告累积的微秒消耗。在 HERMES_DEV_CREDITS 后面公开。
    agent._credits_state = None
    agent._credits_session_start_micros = None
    # 阈值通知锁存器（L4）：活动粘性通知键 + warn90 越过门。
    agent._credits_latch = {"active": set(), "seen_below_90": False, "usage_band": None}

    # OpenRouter 响应缓存命中计数器 — 当在流式响应头中看到
    # X-OpenRouter-Cache-Status: HIT 时递增。
    agent._or_cache_hits: int = 0

    # 集中式日志记录 — agent.log（INFO+）和 errors.log（WARNING+）
    # 都位于 ~/.hermes/logs/ 下。幂等，因此网关模式（为每条消息创建新的 AIAgent）
    # 不会重复处理程序。
    from hermes_logging import setup_logging, setup_verbose_logging
    setup_logging(hermes_home=_ra()._hermes_home)

    if agent.verbose_logging:
        setup_verbose_logging()
        _ra().logger.info("已启用详细日志记录（第三方库日志被抑制）")
    elif agent.quiet_mode:
        # 在安静模式（CLI 默认）下，保持控制台输出干净 —
        # 但不要提高每个记录器的级别。这样做会阻止
        # 根记录器的文件处理程序（agent.log、errors.log）
        # 永远看到记录，因为 Python 在处理程序传播之前检查
        # logger.isEnabledFor()。我们依赖
        # hermes_logging.setup_logging() 在安静模式下不安装控制台 StreamHandler —
        # 因此 INFO 记录流向文件处理程序，但永远不会到达控制台。
        # 任何未来的噪音削减应该在 hermes_logging.py 内的处理程序级别进行，而不是在这里。
        pass

    # 内部流回调（在流式 TTS 期间设置）。
    # 在此处初始化，以便 _vprint 可以在 run_conversation 之前引用它。
    agent._stream_callback = None
    # 延迟的段落中断标志 — 在工具迭代后设置，以便在下一个真实文本增量之前
    # 预置单个 "\n\n"。
    agent._stream_needs_break = False
    # 有状态清理器，用于跨流增量分割的 <memory-context> 跨度（#5719）。
    # 仅 sanitize_context() 无法在块边界中生存，因为块正则表达式需要
    # 在一个字符串中同时包含两个标签。
    agent._stream_context_scrubber = StreamingContextScrubber()
    # 有状态清理器，用于流增量中的推理/思考标签（#17924）。
    # 替换每增量 _strip_think_blocks 正则表达式，该正则表达式
    # 破坏了下游状态（例如 MiniMax-M2.7 流式传输 '作为 delta1，
    # '让我检查' 作为 delta2 — 正则表达式擦除了 delta1，因此下游状态机
    # 从未得知块已打开，并将 delta2 泄漏为内容）。
    agent._stream_think_scrubber = StreamingThinkScrubber()
    # 在当前模型响应期间通过实时令牌回调传递的可见助手文本。
    # 用于避免在提供商稍后将其作为完成的临时助手消息返回时重新发送相同的注释。
    agent._current_streamed_assistant_text = ""

    # 可选的当前轮次用户消息覆盖，当 API 面向的用户消息故意不同于
    # 持久化的轨迹时使用（例如 CLI 语音模式仅为实时调用添加临时前缀）。
    agent._persist_user_message_idx = None
    agent._persist_user_message_override = None
    agent._persist_user_message_timestamp = None

    # 缓存每个图像有效负载/URL 的 anthropic 图像到文本后备，以便
    # 单个工具循环不会在相同的图像历史记录上重复运行辅助视觉。
    agent._anthropic_image_fallback_cache: Dict[str, str] = {}

    # 通过集中式提供商路由器初始化 LLM 客户端。
    # 路由器处理所有已知提供商的身份验证解析、基础 URL、标头
    # 以及 Codex/Anthropic 包装。
    # raw_codex=True，因为主代理需要直接 responses.stream() 访问
    # 用于 Codex Responses API 流式传输。
    agent._anthropic_client = None
    agent._is_anthropic_oauth = False

    # 提前解析每个提供商/每个模型的请求超时，以便下面的每个客户端构造路径
    #（Anthropic 本机、OpenAI-wire、基于路由器的隐式身份验证）
    # 可以一致地应用它。Bedrock Claude 使用自己的超时路径，此处未涵盖。
    _provider_timeout = get_provider_request_timeout(agent.provider, agent.model)

    if agent.api_mode == "anthropic_messages":
        from agent.anthropic_adapter import build_anthropic_client, resolve_anthropic_token
        # Bedrock + Claude → 使用 AnthropicBedrock SDK 实现完整的功能奇偶性
        #（提示缓存、思考预算、自适应思考）。
        _is_bedrock_anthropic = agent.provider == "bedrock"
        if _is_bedrock_anthropic:
            from agent.anthropic_adapter import build_anthropic_bedrock_client
            _region_match = re.search(r"bedrock-runtime\.([a-z0-9-]+)\.", base_url or "")
            _br_region = _region_match.group(1) if _region_match else "us-east-1"
            agent._bedrock_region = _br_region
            agent._anthropic_client = build_anthropic_bedrock_client(_br_region)
            agent._anthropic_api_key = "aws-sdk"
            agent._anthropic_base_url = base_url
            agent._is_anthropic_oauth = False
            agent.api_key = "aws-sdk"
            agent.client = None
            agent._client_kwargs = {}
            if not agent.quiet_mode:
                print(f"🤖 AI 代理已初始化，模型：{agent.model}（AWS Bedrock + AnthropicBedrock SDK，{_br_region}）")
        else:
            # 仅在提供商实际是 Anthropic 时才回退到 ANTHROPIC_TOKEN。
            # 其他 anthropic_messages 提供商（MiniMax、Alibaba 等）必须使用自己的 API 密钥。
            # 回退会将 Anthropic 凭据发送到第三方端点（修复 #1739、#minimax-401）。
            _is_native_anthropic = agent.provider == "anthropic"
            effective_key = (api_key or resolve_anthropic_token() or "") if _is_native_anthropic else (api_key or "")

            # MiniMax OAuth 发出短期（约 15 分钟）访问令牌。
            # Anthropic SDK 在客户端构造时将 ``api_key`` 缓存为静态字符串，
            # 因此在启动时解析一次承载令牌的会话将不断发送相同的令牌，
            # 直到 MiniMax 在会话中返回 401。将静态字符串替换为可调用令牌提供程序 —
            # ``build_anthropic_client`` 识别可调用对象并安装 httpx 事件钩子，
            # 该钩子为每个出站请求铸造新的承载（重新读取 auth.json，以便
            # 另一个进程持久化的刷新立即可见）。
            # 缓存的刷新路径在令牌仍有 ``MINIMAX_OAUTH_REFRESH_SKEW_SECONDS`` 生命周期时为空操作，
            # 因此稳态成本是每个请求一次文件读取 + 一次时间戳比较。
            if agent.provider == "minimax-oauth" and isinstance(effective_key, str) and effective_key:
                try:
                    from hermes_cli.auth import build_minimax_oauth_token_provider
                    effective_key = build_minimax_oauth_token_provider()
                except Exception as _mm_exc:  # noqa: BLE001 — 从不因此阻止启动
                    import logging as _logging
                    _logging.getLogger(__name__).warning(
                        "MiniMax OAuth：未能安装每请求令牌提供程序"
                        "(%s)；回退到将在约 15 分钟内过期的静态承载。",
                        _mm_exc,
                    )

            agent.api_key = effective_key
            agent._anthropic_api_key = effective_key
            agent._anthropic_base_url = base_url
            # 仅当令牌真正属于本机 Anthropic 时，才会话标记为 OAuth 身份验证。
            # 讲述 Anthropic 协议的第三方提供商（MiniMax、Kimi、GLM、LiteLLM 代理）
            # 绝不能触发 OAuth 代码路径 — 这样做会注入 Claude-Code 身份标头和系统提示，
            # 在其端点上导致 401/403。保护 #1739 和第三方身份注入错误。
            from agent.anthropic_adapter import _is_oauth_token as _is_oat
            agent._is_anthropic_oauth = _is_oat(effective_key) if (_is_native_anthropic and isinstance(effective_key, str)) else False
            agent._anthropic_client = build_anthropic_client(effective_key, base_url, timeout=_provider_timeout)
            # Anthropic 模式不需要 OpenAI 客户端
            agent.client = None
            agent._client_kwargs = {}
            if not agent.quiet_mode:
                print(f"🤖 AI 代理已初始化，模型：{agent.model}（Anthropic 本机）")
                # ``effective_key`` 可能是 Azure Foundry anthropic_messages 模式的
                # 可调用 Entra ID 承载提供程序。
                # Anthropic 适配器安装 httpx 事件钩子，
                # 该钩子为每个请求铸造新的 JWT — 我们
                # 从不在横幅中调用或检查可调用对象。
                from agent.azure_identity_adapter import is_token_provider

                if is_token_provider(effective_key):
                    print("🔑 使用凭据：Microsoft Entra ID")
                elif isinstance(effective_key, str) and len(effective_key) > 12:
                    print(f"🔑 使用令牌：{effective_key[:8]}...{effective_key[-4:]}")
    elif agent.api_mode == "bedrock_converse":
        # AWS Bedrock — 直接使用 boto3，不需要 OpenAI 客户端。
        # 区域从 base_url 提取或默认为 us-east-1。
        _region_match = re.search(r"bedrock-runtime\.([a-z0-9-]+)\.", base_url or "")
        agent._bedrock_region = _region_match.group(1) if _region_match else "us-east-1"
        # Guardrail 配置 — 在初始化时从 config.yaml 读取。
        agent._bedrock_guardrail_config = None
        try:
            from hermes_cli.config import load_config as _load_br_cfg
            _gr = _load_br_cfg().get("bedrock", {}).get("guardrail", {})
            if _gr.get("guardrail_identifier") and _gr.get("guardrail_version"):
                agent._bedrock_guardrail_config = {
                    "guardrailIdentifier": _gr["guardrail_identifier"],
                    "guardrailVersion": _gr["guardrail_version"],
                }
                if _gr.get("stream_processing_mode"):
                    agent._bedrock_guardrail_config["streamProcessingMode"] = _gr["stream_processing_mode"]
                if _gr.get("trace"):
                    agent._bedrock_guardrail_config["trace"] = _gr["trace"]
        except Exception:
            pass
        agent.client = None
        agent._client_kwargs = {}
        if not agent.quiet_mode:
            _gr_label = " + Guardrails" if agent._bedrock_guardrail_config else ""
            print(f"🤖 AI 代理已初始化，模型：{agent.model}（AWS Bedrock，{agent._bedrock_region}{_gr_label}）")
    else:
        if api_key and base_url:
            # 来自 CLI/网关的显式凭据 — 直接构造。
            # 运行时提供商解析器已为我们处理了身份验证。
            # 从 base_url 提取查询参数（例如 Azure api-version）并通过 default_query 传递，
            # 以防止在 SDK URL 连接期间丢失（httpx 在连接路径时删除查询字符串）。
            _parsed_url = urlparse(base_url)
            if _parsed_url.query:
                _clean_url = urlunparse(_parsed_url._replace(query=""))
                _query_params = {
                    k: v[0] for k, v in parse_qs(_parsed_url.query).items()
                }
                client_kwargs = {
                    "api_key": api_key,
                    "base_url": _clean_url,
                    "default_query": _query_params,
                }
            else:
                client_kwargs = {"api_key": api_key, "base_url": base_url}
            if _provider_timeout is not None:
                client_kwargs["timeout"] = _provider_timeout
            if agent.provider == "copilot-acp":
                client_kwargs["command"] = agent.acp_command
                client_kwargs["args"] = agent.acp_args
            effective_base = base_url
            if base_url_host_matches(effective_base, "openrouter.ai"):
                from agent.auxiliary_client import build_or_headers
                client_kwargs["default_headers"] = build_or_headers()
            elif base_url_host_matches(effective_base, "integrate.api.nvidia.com"):
                from agent.auxiliary_client import build_nvidia_nim_headers
                client_kwargs["default_headers"] = build_nvidia_nim_headers(effective_base)
            elif base_url_host_matches(effective_base, "api.routermint.com"):
                client_kwargs["default_headers"] = _ra()._routermint_headers()
            elif base_url_host_matches(effective_base, "api.githubcopilot.com"):
                from hermes_cli.models import copilot_default_headers

                client_kwargs["default_headers"] = copilot_default_headers()
            elif base_url_host_matches(effective_base, "api.kimi.com"):
                client_kwargs["default_headers"] = {
                    "User-Agent": "claude-code/0.1.0",
                }
            elif base_url_host_matches(effective_base, "portal.qwen.ai"):
                client_kwargs["default_headers"] = _ra()._qwen_portal_headers()
            elif base_url_host_matches(effective_base, "chatgpt.com"):
                from agent.auxiliary_client import _codex_cloudflare_headers
                client_kwargs["default_headers"] = _codex_cloudflare_headers(api_key)
            elif "default_headers" not in client_kwargs:
                # 回退到 profile.default_headers 用于声明自定义标头的提供商
                #（例如非 kimi.com 端点上的 Kimi User-Agent）。
                try:
                    from providers import get_provider_profile as _gpf
                    _ph = _gpf(agent.provider)
                    if _ph and _ph.default_headers:
                        client_kwargs["default_headers"] = dict(_ph.default_headers)
                except Exception:
                    pass
        else:
            # 没有显式凭据 — 使用集中式提供商路由器
            from agent.auxiliary_client import resolve_provider_client
            _routed_client, _ = resolve_provider_client(
                agent.provider or "auto", model=agent.model, raw_codex=True)
            if _routed_client is not None:
                client_kwargs = {
                    "api_key": _routed_client.api_key,
                    "base_url": str(_routed_client.base_url),
                }
                if _provider_timeout is not None:
                    client_kwargs["timeout"] = _provider_timeout
                # 保留路由器设置的提供商特定标头。
                # OpenAI SDK 将调用者提供的 default_headers 存储在
                # _custom_headers 中；较旧的/模拟的客户端可能改为公开
                # _default_headers。
                _routed_headers = getattr(_routed_client, "_custom_headers", None)
                if not _routed_headers:
                    _routed_headers = getattr(_routed_client, "default_headers", None)
                if not _routed_headers:
                    _routed_headers = getattr(_routed_client, "_default_headers", None)
                if _routed_headers:
                    client_kwargs["default_headers"] = dict(_routed_headers)
            else:
                # 当用户明确选择了非 OpenRouter 提供商但未找到凭据时，
                # 以明确的消息快速失败，而不是默默通过 OpenRouter 路由。
                _explicit = (agent.provider or "").strip().lower()
                if _explicit and _explicit not in {"auto", "openrouter", "custom"}:
                    # 从提供商配置中查找实际的环境变量名称 —
                    # 某些提供商使用非标准名称（例如 alibaba → DASHSCOPE_API_KEY，而不是 ALIBABA_API_KEY）。
                    _env_hint = f"{_explicit.upper()}_API_KEY"
                    try:
                        from hermes_cli.auth import PROVIDER_REGISTRY
                        _pcfg = PROVIDER_REGISTRY.get(_explicit)
                        if _pcfg and _pcfg.api_key_env_vars:
                            _env_hint = _pcfg.api_key_env_vars[0]
                    except Exception:
                        pass
                    # --- 初始化时回退（#17929）---
                    _fb_entries = []
                    if isinstance(fallback_model, list):
                        _fb_entries = [
                            f for f in fallback_model
                            if isinstance(f, dict) and f.get("provider") and f.get("model")
                        ]
                    elif isinstance(fallback_model, dict) and fallback_model.get("provider") and fallback_model.get("model"):
                        _fb_entries = [fallback_model]
                    _fb_resolved = False
                    for _fb in _fb_entries:
                        _fb_explicit_key = (_fb.get("api_key") or "").strip() or None
                        if not _fb_explicit_key:
                            _fb_key_env = (_fb.get("key_env") or _fb.get("api_key_env") or "").strip()
                            if _fb_key_env:
                                _fb_explicit_key = os.getenv(_fb_key_env, "").strip() or None
                        _fb_client, _fb_model = resolve_provider_client(
                            _fb["provider"], model=_fb["model"], raw_codex=True,
                            explicit_base_url=_fb.get("base_url"),
                            explicit_api_key=_fb_explicit_key,
                        )
                        if _fb_client is not None:
                            agent.provider = _fb["provider"]
                            agent.model = _fb_model or _fb["model"]
                            agent._fallback_activated = True
                            client_kwargs = {
                                "api_key": _fb_client.api_key,
                                "base_url": str(_fb_client.base_url),
                            }
                            if _provider_timeout is not None:
                                client_kwargs["timeout"] = _provider_timeout
                            _fb_headers = getattr(_fb_client, "_custom_headers", None)
                            if not _fb_headers:
                                _fb_headers = getattr(_fb_client, "default_headers", None)
                            if not _fb_headers:
                                _fb_headers = getattr(_fb_client, "_default_headers", None)
                            if _fb_headers:
                                client_kwargs["default_headers"] = dict(_fb_headers)
                            _fb_resolved = True
                            break
                    if not _fb_resolved:
                        raise RuntimeError(
                            f"提供商 '{_explicit}' 已在 config.yaml 中设置，但未找到 API 密钥。"
                            f"设置 {_env_hint} 环境变量，或使用 `hermes model` 切换到不同的提供商。"
                        )
                if not getattr(agent, "_fallback_activated", False):
                    # 未配置提供商 — 以明确的消息拒绝。
                    raise RuntimeError(
                        "未配置 LLM 提供商。运行 `hermes model` 选择提供商，"
                        "或运行 `hermes setup` 进行首次配置。"
                    )

        agent._client_kwargs = client_kwargs  # 存储用于在中断后重建

        # 为 OpenRouter 上的 Claude 启用细粒度工具流式传输。
        # 如果没有这个，Anthropic 会缓冲整个工具调用并在思考时静默几分钟 —
        # OpenRouter 的上游代理在静默期间超时。
        # beta 头使 Anthropic 逐令牌流式传输工具调用参数，保持连接活动。
        _effective_base = str(client_kwargs.get("base_url", "")).lower()
        if base_url_host_matches(_effective_base, "openrouter.ai") and "claude" in (agent.model or "").lower():
            headers = client_kwargs.get("default_headers") or {}
            existing_beta = headers.get("x-anthropic-beta", "")
            _FINE_GRAINED = "fine-grained-tool-streaming-2025-05-14"
            if _FINE_GRAINED not in existing_beta:
                if existing_beta:
                    headers["x-anthropic-beta"] = f"{existing_beta},{_FINE_GRAINED}"
                else:
                    headers["x-anthropic-beta"] = _FINE_GRAINED
                client_kwargs["default_headers"] = headers

        # 用户配置的请求标头（config.yaml 中的 model.default_headers）
        # 覆盖提供商/SDK 默认值。允许自定义 OpenAI 兼容端点位于网关/WAF 后面，
        # 该网关/WAF 拒绝 OpenAI SDK 的标识标头并交换纯 User-Agent。（#40033）
        # client_kwargs 是与 agent._client_kwargs 相同的字典对象，因此
        # 此更改反映在刚刚构建的客户端中。
        agent._apply_user_default_headers()

        agent.api_key = client_kwargs.get("api_key", "")
        agent.base_url = client_kwargs.get("base_url", agent.base_url)
        try:
            from agent.ssl_guard import verify_ca_bundle_with_fallback

            verify_ca_bundle_with_fallback()
            agent.client = agent._create_openai_client(client_kwargs, reason="agent_init", shared=True)
            if not agent.quiet_mode:
                print(f"🤖 AI 代理已初始化，模型：{agent.model}")
                if base_url:
                    print(f"🔗 使用自定义基础 URL：{base_url}")
                # ``api_key`` 可能是 Azure Foundry anthropic_messages 模式的
                # 可调用 Entra ID 承载提供程序。
                # OpenAI SDK 在内部为每个请求铸造新的 JWT —
                # 横幅从不调用或检查可调用对象。
                from agent.azure_identity_adapter import is_token_provider

                key_used = client_kwargs.get("api_key", "none")
                if is_token_provider(key_used):
                    print("🔑 使用凭据：Microsoft Entra ID")
                elif isinstance(key_used, str) and key_used and key_used != "dummy-key" and len(key_used) > 12:
                    print(f"🔑 使用 API 密钥：{key_used[:8]}...{key_used[-4:]}")
                else:
                    print("⚠️  警告：API 密钥似乎无效或缺失")
        except Exception as e:
            raise RuntimeError(f"初始化 OpenAI 客户端失败：{e}")

    # 提供商回退链 — 尝试的备份提供商的有序列表，
    # 当主提供商耗尽时（速率限制、过载、连接失败）。
    # 支持传统的单个字典 ``fallback_model`` 和新列表 ``fallback_providers`` 格式。
    if isinstance(fallback_model, list):
        agent._fallback_chain = [
            f for f in fallback_model
            if isinstance(f, dict) and f.get("provider") and f.get("model")
        ]
    elif isinstance(fallback_model, dict) and fallback_model.get("provider") and fallback_model.get("model"):
        agent._fallback_chain = [fallback_model]
    else:
        agent._fallback_chain = []
    agent._fallback_index = 0
    agent._fallback_activated = getattr(agent, "_fallback_activated", False)
    # 保留旧属性以实现向后兼容（测试、外部调用者）
    agent._fallback_model = agent._fallback_chain[0] if agent._fallback_chain else None
    if agent._fallback_chain and not agent.quiet_mode:
        if len(agent._fallback_chain) == 1:
            fb = agent._fallback_chain[0]
            print(f"🔄 回退模型：{fb['model']}（{fb['provider']}）")
        else:
            print(f"🔄 回退链（{len(agent._fallback_chain)} 个提供商）：" +
                  " → ".join(f"{f['model']}（{f['provider']}）" for f in agent._fallback_chain))

    # 获取可用工具并进行过滤。首先捕获此快照派生自的注册表生成，
    # 以便稍后的并发刷新可以判断它持有更新还是更旧的视图
    #（参见 refresh_agent_mcp_tools）。
    try:
        from tools.registry import registry as _snapshot_registry
        agent._tool_snapshot_generation = _snapshot_registry._generation
    except Exception:
        agent._tool_snapshot_generation = 0
    agent.tools = _ra().get_tool_definitions(
        enabled_toolsets=enabled_toolsets,
        disabled_toolsets=disabled_toolsets,
        quiet_mode=agent.quiet_mode,
    )

    # 显示工具配置并存储有效的工具名称以进行验证
    agent.valid_tool_names = set()
    if agent.tools:
        agent.valid_tool_names = {tool["function"]["name"] for tool in agent.tools}
        tool_names = sorted(agent.valid_tool_names)
        if not agent.quiet_mode:
            print(f"🛠️  已加载 {len(agent.tools)} 个工具：{', '.join(tool_names)}")
            # 如果应用了过滤，显示过滤信息
            if enabled_toolsets:
                print(f"   ✅ 已启用工具集：{', '.join(enabled_toolsets)}")
            if disabled_toolsets:
                print(f"   ❌ 已禁用工具集：{', '.join(disabled_toolsets)}")
    elif not agent.quiet_mode:
        print("🛠️  未加载工具（所有工具被过滤或不可用）")

    # 看板/编排器生命周期指导是会话静态的：
    # 调度程序在生成时决定此进程是否为看板工作线程
    #（当且仅当设置了 HERMES_KANBAN_TASK 时，kanban_show 工具存在）。
    # 在此处解析一次 ~835 令牌块，避免了在每次系统提示重建上
    # 重新运行成员资格测试 + 引用（初始化 + 每次上下文压缩）。
    from agent.prompt_builder import KANBAN_GUIDANCE
    agent._kanban_worker_guidance = (
        KANBAN_GUIDANCE if "kanban_show" in agent.valid_tool_names else ""
    )

    # 检查工具要求
    if agent.tools and not agent.quiet_mode:
        requirements = _ra().check_toolset_requirements()
        missing_reqs = [name for name, available in requirements.items() if not available]
        if missing_reqs:
            print(f"⚠️  由于缺少要求，某些工具可能无法工作：{missing_reqs}")

    # 显示轨迹保存状态
    if agent.save_trajectories and not agent.quiet_mode:
        print("📝 轨迹保存已启用")

    # 显示临时系统提示状态
    if agent.ephemeral_system_prompt and not agent.quiet_mode:
        prompt_preview = agent.ephemeral_system_prompt[:60] + "..." if len(agent.ephemeral_system_prompt) > 60 else agent.ephemeral_system_prompt
        print(f"🔒 临时系统提示：'{prompt_preview}'（不保存到轨迹）")

    # 显示提示缓存状态
    if agent._use_prompt_caching and not agent.quiet_mode:
        if agent._use_native_cache_layout and agent.provider == "anthropic":
            source = "本机 Anthropic"
        elif agent._use_native_cache_layout:
            source = "Anthropic 兼容端点"
        else:
            source = "Claude 通过 OpenRouter"
        print(f"💾 提示缓存：已启用（{source}，{agent._cache_ttl} TTL）")

    # 会话日志设置 - 自动保存对话轨迹以进行调试
    agent.session_start = datetime.now()
    if session_id:
        # 使用提供的会话 ID（例如来自 CLI）
        agent.session_id = session_id
    else:
        # 生成新的会话 ID
        timestamp_str = agent.session_start.strftime("%Y%m%d_%H%M%S")
        short_uuid = uuid.uuid4().hex[:6]
        agent.session_id = f"{timestamp_str}_{short_uuid}"

    # 向工具（终端、execute_code）公开会话 ID，以便代理可以引用自己的会话
    # 用于 --resume 命令、跨会话协调和日志记录。
    # 保持 ContextVar 和 os.environ 回退同步，因为不同的工具路径仍会读取两者。
    try:
        from gateway.session_context import set_current_session_id

        set_current_session_id(agent.session_id)
    except Exception:
        os.environ["HERMES_SESSION_ID"] = agent.session_id

    # 会话日志进入 ~/.hermes/sessions/，与网关会话一起
    hermes_home = get_hermes_home()
    agent.logs_dir = hermes_home / "sessions"
    agent.logs_dir.mkdir(parents=True, exist_ok=True)
    # 每会话 JSON 快照写入程序（~/.hermes/sessions/session_{sid}.json）
    # 通过 sessions.write_json_snapshots 选择启用（默认为 False）。
    # state.db 是规范的 — 快照仅对直接读取 JSON 文件的外部工具有用。
    # 参见 run_agent._save_session_log。
    agent._session_json_enabled = False
    try:
        from hermes_cli.config import load_config as _load_sess_cfg
        _sess_cfg = (_load_sess_cfg().get("sessions") or {})
        agent._session_json_enabled = bool(_sess_cfg.get("write_json_snapshots", False))
    except Exception:
        pass
    # logs_dir 无条件保留用于 request_dump_*.json（调试面包屑路径，
    # 由 agent_runtime_helpers.dump_api_request_debug 写入）。

    # 跟踪会话消息以进行会话日志记录
    agent._session_messages: List[Dict[str, Any]] = []
    # 响应加密推理重放状态。某些 OpenAI 兼容路由接受 GPT-5 Responses 请求，
    # 但稍后拒绝重放的加密推理块（HTTP 400 ``invalid_encrypted_content``）。
    # 发生这种情况时，我们会话的其余部分禁用重放并回退到无状态连续性。
    # 参见 agent/conversation_loop.py 的 invalid_encrypted_content 重试分支。
    agent._codex_reasoning_replay_enabled = True
    agent._memory_write_origin = "assistant_tool"
    agent._memory_write_context = "foreground"

    # 缓存的系统提示 -- 每会话构建一次，仅在压缩时重建
    agent._cached_system_prompt: Optional[str] = None

    # 文件系统检查点管理器（透明 — 不是工具）
    from tools.checkpoint_manager import CheckpointManager
    agent._checkpoint_mgr = CheckpointManager(
        enabled=checkpoints_enabled,
        max_snapshots=checkpoint_max_snapshots,
        max_total_size_mb=checkpoint_max_total_size_mb,
        max_file_size_mb=checkpoint_max_file_size_mb,
    )

    # SQLite 会话存储（可选 -- 由 CLI 或网关提供）
    agent._session_db = session_db
    agent._parent_session_id = parent_session_id
    agent._last_flushed_db_idx = 0  # 跟踪 DB 写入游标以防止重复写入
    agent._session_db_created = False  # DB 行延迟到 run_conversation()
    # 大多数代理拥有自己的会话行，并应在 close() 时完成它。
    # 某些临时助手代理（手动压缩/会话清理/background-review 分支）
    # 轮换或将会话转发到必须保持打开的继续行，即使助手已拆除；
    # 这些调用者明确将此标志设置为 False。
    agent._end_session_on_close = True
    agent._session_init_model_config = {
        "max_iterations": agent.max_iterations,
        "reasoning_config": reasoning_config,
        "max_tokens": max_tokens,
    }

    # 用于任务规划的内存待办事项列表（每个代理/会话一个）
    from tools.todo_tool import TodoStore
    agent._todo_store = TodoStore()

    # 加载一次配置用于内存、技能和压缩部分
    try:
        from hermes_cli.config import load_config as _load_agent_config
        _agent_cfg = _load_agent_config()
    except Exception:
        _agent_cfg = {}
    try:
        agent._tool_guardrails = ToolCallGuardrailController(
            ToolCallGuardrailConfig.from_mapping(
                _agent_cfg.get("tool_loop_guardrails", {})
            )
        )
    except Exception as _tlg_err:
        _ra().logger.warning("工具循环护栏配置被忽略：%s", _tlg_err)
    # 仅缓存稍后启动可行性检查所需的派生辅助压缩上下文覆盖。
    # 避免在代理实例上暴露广泛的伪公共配置对象。
    agent._aux_compression_context_length_config = None

    # 持久内存（MEMORY.md + USER.md） -- 从磁盘加载
    agent._memory_store = None
    agent._memory_enabled = False
    agent._user_profile_enabled = False
    agent._memory_nudge_interval = 10
    agent._turns_since_memory = 0
    agent._iters_since_skill = 0
    if not skip_memory:
        try:
            mem_config = _agent_cfg.get("memory", {})
            agent._memory_enabled = mem_config.get("memory_enabled", False)
            agent._user_profile_enabled = mem_config.get("user_profile_enabled", False)
            agent._memory_nudge_interval = int(mem_config.get("nudge_interval", 10))
            if agent._memory_enabled or agent._user_profile_enabled:
                from tools.memory_tool import MemoryStore
                agent._memory_store = MemoryStore(
                    memory_char_limit=mem_config.get("memory_char_limit", 2200),
                    user_char_limit=mem_config.get("user_char_limit", 1375),
                )
                agent._memory_store.load_from_disk()
        except Exception:
            pass  # 内存是可选的 — 不要破坏代理初始化


    # 内存提供程序插件（外部 — 一次一个，与内置一起）
    # 从配置读取 memory.provider 以选择要激活的插件。
    agent._memory_manager = None
    if not skip_memory:
        try:
            _mem_provider_name = mem_config.get("provider", "") if mem_config else ""

            if _mem_provider_name and _mem_provider_name.strip():
                from agent.memory_manager import MemoryManager as _MemoryManager
                from plugins.memory import load_memory_provider as _load_mem
                agent._memory_manager = _MemoryManager()
                _mp = _load_mem(_mem_provider_name)
                if _mp and _mp.is_available():
                    agent._memory_manager.add_provider(_mp)
                if agent._memory_manager.providers:
                    _init_kwargs = {
                        "session_id": agent.session_id,
                        "platform": platform or "cli",
                        "hermes_home": str(get_hermes_home()),
                        "agent_context": "primary",
                    }
                    if _init_kwargs["platform"] == "cli":
                        _init_kwargs["warning_callback"] = agent._emit_warning
                        _init_kwargs["status_callback"] = agent._emit_status
                    # 线程会话标题用于内存提供程序范围划分
                    #（例如 honcho 使用它来派生聊天范围的会话密钥）
                    if agent._session_db:
                        try:
                            _st = agent._session_db.get_session_title(agent.session_id)
                            if _st:
                                _init_kwargs["session_title"] = _st
                        except Exception:
                            pass
                    # 线程网关用户身份用于每用户内存范围划分
                    if agent._user_id:
                        _init_kwargs["user_id"] = agent._user_id
                    if agent._user_id_alt:
                        _init_kwargs["user_id_alt"] = agent._user_id_alt
                    if agent._user_name:
                        _init_kwargs["user_name"] = agent._user_name
                    if agent._chat_id:
                        _init_kwargs["chat_id"] = agent._chat_id
                    if agent._chat_name:
                        _init_kwargs["chat_name"] = agent._chat_name
                    if agent._chat_type:
                        _init_kwargs["chat_type"] = agent._chat_type
                    if agent._thread_id:
                        _init_kwargs["thread_id"] = agent._thread_id
                    # 线程网关会话密钥用于稳定的每聊天 Honcho 会话隔离
                    if agent._gateway_session_key:
                        _init_kwargs["gateway_session_key"] = agent._gateway_session_key
                    # 配置文件身份用于每配置文件提供程序范围划分
                    try:
                        from hermes_cli.profiles import get_active_profile_name
                        _profile = get_active_profile_name()
                        _init_kwargs["agent_identity"] = _profile
                        _init_kwargs["agent_workspace"] = "hermes"
                    except Exception:
                        pass
                    agent._memory_manager.initialize_all(**_init_kwargs)
                    _ra().logger.info("内存提供程序 '%s' 已激活", _mem_provider_name)
                else:
                    _ra().logger.debug("内存提供程序 '%s' 未找到或不可用", _mem_provider_name)
                    agent._memory_manager = None
        except Exception as _mpe:
            _ra().logger.warning("内存提供程序插件初始化失败：%s", _mpe)
            agent._memory_manager = None

    from agent.memory_manager import inject_memory_provider_tools as _inject_memory_provider_tools
    _inject_memory_provider_tools(agent)

    # 技能配置：技能创建提醒的推动间隔
    agent._skill_nudge_interval = 10
    try:
        skills_config = _agent_cfg.get("skills", {})
        agent._skill_nudge_interval = int(skills_config.get("creation_nudge_interval", 10))
    except Exception:
        pass

    # 工具使用强制配置："auto"（默认 — 匹配硬编码的模型列表）、
    # true（始终）、false（从不）或子字符串列表。
    _agent_section = _agent_cfg.get("agent", {})
    if not isinstance(_agent_section, dict):
        _agent_section = {}
    agent._tool_use_enforcement = _agent_section.get("tool_use_enforcement", "auto")

    # 通用任务完成指导开关。默认为 True。
    # 作为与 tool_use_enforcement 分开的标志公开，因为指导
    # 适用于所有模型，而不仅仅是执法针对的模型系列。
    agent._task_completion_guidance = bool(_agent_section.get("task_completion_guidance", True))

    # 通用并行工具调用指导开关。默认为 True。
    # 与 task_completion_guidance 分开的标志，因为用户可能想要其中一个但不是另一个。
    # 引导模型将独立的工具调用批处理到单个轮次中；
    # 运行时已经并发执行此类批次。
    agent._parallel_tool_call_guidance = bool(_agent_section.get("parallel_tool_call_guidance", True))

    # 本地 Python 工具链探测开关。默认为 True。
    # 如果为 False，则完全跳过探测（无子进程调用，无系统提示行）。
    # 对于探测启发式嘈杂的奇异设置上的用户很有用。
    agent._environment_probe = bool(_agent_section.get("environment_probe", True))

    # 每平台提示提示覆盖（config.yaml → platform_hints）。
    # 允许企业管理员附加或替换 Hermes 的内置提示
    # 用于单个消息平台（例如 WhatsApp），而不影响其他平台。形状：
    #   platform_hints:
    #     whatsapp:
    #       append: "当表格输出有帮助时，调用 ... 技能。"
    #     slack:
    #       replace: "完全替换默认值的自定义 Slack 提示。"
    # 逐字存储；解析在 agent/system_prompt.py 中针对活动平台进行。
    # 无效形状被防御性地忽略，以便错误的配置条目永远不会破坏提示组装。
    _platform_hints_cfg = _agent_cfg.get("platform_hints", {})
    if not isinstance(_platform_hints_cfg, dict):
        _platform_hints_cfg = {}
    agent._platform_hint_overrides = _platform_hints_cfg

    # 应用级 API 重试计数（包装每个模型 API 调用）。默认为 3，
    # 可通过 config.yaml 中的 agent.api_max_retries 覆盖。参见 #11616。
    try:
        _raw_api_retries = _agent_section.get("api_max_retries", 3)
        _api_retries = int(_raw_api_retries)
        _api_retries = max(_api_retries, 1)  # 1 = 无重试（单次尝试）
    except (TypeError, ValueError):
        _api_retries = 3
    agent._api_max_retries = _api_retries

    # 初始化上下文压缩器用于自动上下文管理
    # 在接近模型的上下文限制时压缩对话
    # 通过 config.yaml（压缩部分）配置
    _compression_cfg = _agent_cfg.get("compression", {})
    if not isinstance(_compression_cfg, dict):
        _compression_cfg = {}
    compression_threshold = float(_compression_cfg.get("threshold", 0.50))
    # 每模型/路由压缩阈值覆盖。Codex gpt-5.5 提高到
    # 85%（Codex 后端将窗口限制在 272K，因此默认 50%
    # 会在 ~136K 处压缩 — 一半的可用上下文）。
    # 由退出配置标志保护，因此用户可以回退到全局阈值；
    # 当覆盖触发时，我们会存储一次性通知（在第一轮重播），
    # 告诉用户更改了什么以及如何恢复。
    _codex_gpt55_autoraise = str(
        _compression_cfg.get("codex_gpt55_autoraise", True)
    ).lower() in {"true", "1", "yes"}
    agent._compression_threshold_autoraised = None
    try:
        from agent.auxiliary_client import (
            _compression_threshold_for_model as _cthresh_fn,
            _is_codex_gpt55 as _is_codex_gpt55_fn,
        )
        _model_cthresh = _cthresh_fn(
            agent.model,
            agent.provider,
            allow_codex_gpt55_autoraise=_codex_gpt55_autoraise,
        )
        if _model_cthresh is not None:
            _prev_threshold = compression_threshold
            compression_threshold = _model_cthresh
            # 仅当 Codex gpt-5.5 自动提升时通知（Arcee Trinity 覆盖是长期存在的静默默认）。
            # 当用户的全局阈值已经满足/超过提升的值时跳过通知，
            # 因为对他们来说实际上没有任何改变。
            if (
                _is_codex_gpt55_fn(agent.model, agent.provider)
                and _model_cthresh > _prev_threshold + 1e-9
            ):
                agent._compression_threshold_autoraised = {
                    "from": _prev_threshold,
                    "to": _model_cthresh,
                }
    except Exception:
        pass
    compression_enabled = str(_compression_cfg.get("enabled", True)).lower() in {"true", "1", "yes"}
    compression_target_ratio = float(_compression_cfg.get("target_ratio", 0.20))
    compression_protect_last = int(_compression_cfg.get("protect_last_n", 20))
    # protect_first_n 是在头部保护的非系统消息数，除了系统提示（总是
    # 由压缩器隐式保护）。下限为 0 — 值为 0 意味着"仅保留系统提示 + 摘要 + 尾部"，
    # 这是长时间运行的滚动压缩会话的合法（且常见）配置。
    compression_protect_first = max(
        0, int(_compression_cfg.get("protect_first_n", 3))
    )
    compression_abort_on_summary_failure = str(
        _compression_cfg.get("abort_on_summary_failure", False)
    ).lower() in {"true", "1", "yes"}
    # 就地压缩：如果为 True，则 compress_context() 重写消息列表并重建系统提示，
    # 而不旋转会话 ID（无 parent_session_id 链，无 `name #N` 重新编号）。
    # 参见 #38763 和 agent/conversation_compression.py。由 compress_context() 使用，
    # 而不是压缩器，因此它依赖于代理。
    compression_in_place = is_truthy_value(
        _compression_cfg.get("in_place"), default=False
    )

    # 读取可选的显式 context_length 覆盖用于辅助压缩模型。
    # 自定义端点通常无法通过 /models 报告此值，因此启动可行性检查需要配置提示。
    try:
        _aux_cfg = cfg_get(_agent_cfg, "auxiliary", "compression", default={})
    except Exception:
        _aux_cfg = {}
    if isinstance(_aux_cfg, dict):
        _aux_context_config = _aux_cfg.get("context_length")
    else:
        _aux_context_config = None
    if _aux_context_config is not None:
        try:
            _aux_context_config = int(_aux_context_config)
        except (TypeError, ValueError):
            _aux_context_config = None
    agent._aux_compression_context_length_config = _aux_context_config

    # 当调用者未直接传递一个时，从配置读取显式模型输出令牌覆盖。
    _model_cfg = _agent_cfg.get("model", {})
    if agent.max_tokens is None and isinstance(_model_cfg, dict):
        _config_max_tokens = _model_cfg.get("max_tokens")
        if _config_max_tokens is not None:
            try:
                if isinstance(_config_max_tokens, bool):
                    raise ValueError
                _parsed_max_tokens = int(_config_max_tokens)
                if _parsed_max_tokens <= 0:
                    raise ValueError
                agent.max_tokens = _parsed_max_tokens
            except (TypeError, ValueError):
                _ra().logger.warning(
                    "config.yaml 中的无效 model.max_tokens：%r — "
                    "必须是正整数（例如 4096）。"
                    "回退到提供商默认值。",
                    _config_max_tokens,
                )
                print(
                    f"\n⚠ config.yaml 中的无效 model.max_tokens：{_config_max_tokens!r}\n"
                    f"  必须是正整数（例如 4096）。\n"
                    f"  回退到提供商默认值。\n",
                    file=sys.stderr,
                )
    agent._session_init_model_config["max_tokens"] = agent.max_tokens

    # 从模型配置读取显式 context_length 覆盖
    if isinstance(_model_cfg, dict):
        _config_context_length = _model_cfg.get("context_length")
    else:
        _config_context_length = None
    if _config_context_length is not None:
        try:
            _config_context_length = int(_config_context_length)
        except (TypeError, ValueError):
            _ra().logger.warning(
                "config.yaml 中的无效 model.context_length：%r — "
                "必须是纯整数（例如 256000，而不是 '256K'）。"
                "回退到自动检测。",
                _config_context_length,
            )
            print(
                f"\n⚠ config.yaml 中的无效 model.context_length：{_config_context_length!r}\n"
                f"  必须是纯整数（例如 256000，而不是 '256K'）。\n"
                f"  回退到自动检测的上下文窗口。\n",
                file=sys.stderr,
            )
            _config_context_length = None

    # 解析 custom_providers 列表一次以供下面重用（启动
    # context-length 覆盖和插件上下文引擎初始化）。
    try:
        from hermes_cli.config import get_compatible_custom_providers
        _custom_providers = get_compatible_custom_providers(_agent_cfg)
    except Exception:
        _custom_providers = _agent_cfg.get("custom_providers")
        if not isinstance(_custom_providers, list):
            _custom_providers = []

    # 存储以供 _check_compression_model_feasibility 重用（辅助
    # 压缩模型 context-length 检测需要相同的列表）。
    agent._custom_providers = _custom_providers
    _merge_custom_provider_extra_body(agent, _custom_providers)

    # 检查 custom_providers 每模型 context_length
    if _config_context_length is None and _custom_providers:
        try:
            from hermes_cli.config import get_custom_provider_context_length
            _cp_ctx_resolved = get_custom_provider_context_length(
                model=agent.model,
                base_url=agent.base_url,
                custom_providers=_custom_providers,
            )
            if _cp_ctx_resolved:
                _config_context_length = int(_cp_ctx_resolved)
        except Exception:
            _cp_ctx_resolved = None

        # 如果用户设置了 context_length 但它不是有效的正整数，则发出明确的警告 —
        # 帮助程序会默默跳过这些。
        if _config_context_length is None:
            _target = agent.base_url.rstrip("/") if agent.base_url else ""
            for _cp_entry in _custom_providers:
                if not isinstance(_cp_entry, dict):
                    continue
                _cp_url = (_cp_entry.get("base_url") or "").rstrip("/")
                if _target and _cp_url == _target:
                    _cp_models = _cp_entry.get("models", {})
                    if isinstance(_cp_models, dict):
                        _cp_model_cfg = _cp_models.get(agent.model, {})
                        if isinstance(_cp_model_cfg, dict):
                            _cp_ctx = _cp_model_cfg.get("context_length")
                            if _cp_ctx is not None:
                                try:
                                    _parsed = int(_cp_ctx)
                                    if _parsed <= 0:
                                        raise ValueError
                                except (TypeError, ValueError):
                                    _ra().logger.warning(
                                        "custom_providers 中模型 %r 的无效 context_length："
                                        "%r — 必须是正整数（例如 256000，而不是 '256K'）。"
                                        "回退到自动检测。",
                                        agent.model, _cp_ctx,
                                    )
                                    print(
                                        f"\n⚠ custom_providers 中模型 {agent.model!r} 的无效 context_length：{_cp_ctx!r}\n"
                                        f"  必须是正整数（例如 256000，而不是 '256K'）。\n"
                                        f"  回退到自动检测的上下文窗口。\n",
                                        file=sys.stderr,
                                    )
                    break

    # 持久化以供 switch_model / 回退激活时重用。
    # 必须在 custom_providers 分支之后，以便每模型覆盖不会丢失。
    agent._config_context_length = _config_context_length

    agent._ensure_lmstudio_runtime_loaded(_config_context_length)



    # 选择上下文引擎：配置驱动（如内存提供程序）。
    # 1. 检查 config.yaml context.engine 设置
    # 2. 检查 plugins/context_engine/<name>/ 目录（仓库提供的）
    # 3. 检查通用插件系统（用户安装的插件）
    # 4. 回退到内置 ContextCompressor
    _selected_engine = None
    _copy_failed = False
    _engine_name = "compressor"  # 默认
    try:
        _ctx_cfg = _agent_cfg.get("context", {}) if isinstance(_agent_cfg, dict) else {}
        _engine_name = _ctx_cfg.get("engine", "compressor") or "compressor"
    except Exception:
        pass

    if _engine_name != "compressor":
        # 尝试从 plugins/context_engine/<name>/ 加载
        try:
            from plugins.context_engine import load_context_engine
            _selected_engine = load_context_engine(_engine_name)
        except Exception as _ce_load_err:
            _ra().logger.debug("从 plugins/context_engine/ 加载上下文引擎：%s", _ce_load_err)

        # 尝试通用插件系统作为回退
        if _selected_engine is None:
            _candidate = None
            try:
                from hermes_cli.plugins import get_plugin_context_engine
                _candidate = get_plugin_context_engine()
            except Exception:
                _candidate = None
            if _candidate is not None and _candidate.name == _engine_name:
                # 深拷贝共享的插件单例，以便子代理的
                # update_model() 不会改变父代理的压缩器（#42449）。
                # 对于持有不可复制状态（锁、数据库连接、客户端）的引擎，复制可能失败；
                # 在这种情况下，回退到内置压缩器，并使用准确的消息，而不是
                # 默默地将其错误标记为"未找到"。
                import copy
                try:
                    _selected_engine = copy.deepcopy(_candidate)
                except Exception as _copy_err:
                    _copy_failed = True
                    _ra().logger.warning(
                        "上下文引擎 '%s' 无法为此代理安全复制"
                        "（%s） — 回退到内置压缩器。持有不可复制状态"
                        "（锁、数据库连接）的插件引擎应实现 __deepcopy__ 以仅复制"
                        "可变的预算状态。",
                        _engine_name, _copy_err,
                    )
                    _selected_engine = None

        if _selected_engine is None and not _copy_failed:
            _ra().logger.warning(
                "未找到上下文引擎 '%s' — 回退到内置压缩器",
                _engine_name,
            )
    # 否则：配置说 "compressor" — 使用内置的，不要自动激活插件

    if _selected_engine is not None:
        agent.context_compressor = _selected_engine
        # 解析插件引擎的 context_length — 镜像 switch_model() 路径
        from agent.model_metadata import get_model_context_length
        _plugin_ctx_len = get_model_context_length(
            agent.model,
            base_url=agent.base_url,
            api_key=getattr(agent, "api_key", ""),
            config_context_length=_config_context_length,
            provider=agent.provider,
            custom_providers=_custom_providers,
        )
        agent.context_compressor.update_model(
            model=agent.model,
            context_length=_plugin_ctx_len,
            base_url=agent.base_url,
            api_key=getattr(agent, "api_key", ""),
            provider=agent.provider,
            api_mode=agent.api_mode,
        )
        if not agent.quiet_mode:
            _ra().logger.info("使用上下文引擎：%s", _selected_engine.name)
    else:
        agent.context_compressor = ContextCompressor(
            model=agent.model,
            threshold_percent=compression_threshold,
            protect_first_n=compression_protect_first,
            protect_last_n=compression_protect_last,
            summary_target_ratio=compression_target_ratio,
            summary_model_override=None,
            quiet_mode=agent.quiet_mode,
            base_url=agent.base_url,
            api_key=getattr(agent, "api_key", ""),
            config_context_length=_config_context_length,
            provider=agent.provider,
            api_mode=agent.api_mode,
            abort_on_summary_failure=compression_abort_on_summary_failure,
            max_tokens=agent.max_tokens,
        )
    agent.compression_enabled = compression_enabled
    agent.compression_in_place = compression_in_place

    # 拒绝上下文窗口低于可靠工具调用工作流所需最小值的模型（64K 令牌）。
    _ctx = getattr(agent.context_compressor, "context_length", 0)
    if _ctx and _ctx < MINIMUM_CONTEXT_LENGTH:
        raise ValueError(
            f"模型 {agent.model} 的上下文窗口为 {_ctx:,} 个令牌，"
            f"低于 Hermes Agent 所需的最小值 {MINIMUM_CONTEXT_LENGTH:,}。"
            f"选择至少具有 {MINIMUM_CONTEXT_LENGTH // 1000}K 上下文的模型，或在 config.yaml 中设置 "
            f"model.context_length 以覆盖。"
        )

    # 注入上下文引擎工具模式（例如 lcm_grep、lcm_describe、lcm_expand）。
    # 跳过已存在的名称 — _ra().get_tool_definitions() quiet_mode 缓存在
    # #17335 之前返回共享列表，因此此处的意外突变会污染同一网关进程中
    # 后续代理初始化，并触发提供商端的"重复工具名称"错误。
    # 即使有缓存修复，去重也是对可能通过 ctx.register_tool() 注册相同模式
    # 的插件路径的正确防御。镜像上面的内存工具去重。
    #
    # 尊重平台的 enabled_toolsets 配置（#5544）：
    # 上下文引擎工具遵循与内存提供程序工具相同的门控模式 —
    # 没有门控，`platform_toolsets: telegram: []` 仍会泄漏 lcm_* 工具到工具表面，
    # 并招致相同的本地模型延迟惩罚。
    agent._context_engine_tool_names: set = set()
    if (
        hasattr(agent, "context_compressor")
        and agent.context_compressor
        and agent.tools is not None
        and (
            agent.enabled_toolsets is None
            or "context_engine" in agent.enabled_toolsets
        )
    ):
        _existing_tool_names = {
            t.get("function", {}).get("name")
            for t in agent.tools
            if isinstance(t, dict)
        }
        from agent.memory_manager import normalize_tool_schema as _normalize_tool_schema
        for _raw_schema in agent.context_compressor.get_tool_schemas():
            _schema = _normalize_tool_schema(_raw_schema)
            if _schema is None:
                # 没有可解析名称的模式（例如已包装的条目）会附加无名称工具，
                # 严格的提供商会对此 400，禁用整个工具集（#47707）。跳过它。
                _ra().logger.warning(
                    "上下文引擎返回的工具模式没有可解析的名称；"
                    "跳过以避免污染请求（%r）",
                    _raw_schema,
                )
                continue
            _tname = _schema["name"]
            if _tname in _existing_tool_names:
                continue  # 已通过插件/缓存路径注册
            _wrapped = {"type": "function", "function": _schema}
            agent.tools.append(_wrapped)
            agent.valid_tool_names.add(_tname)
            agent._context_engine_tool_names.add(_tname)
            _existing_tool_names.add(_tname)

    # 通知上下文引擎会话开始
    if hasattr(agent, "context_compressor") and agent.context_compressor:
        try:
            agent.context_compressor.on_session_start(
                agent.session_id,
                hermes_home=str(get_hermes_home()),
                platform=agent.platform or "cli",
                model=agent.model,
                context_length=getattr(agent.context_compressor, "context_length", 0),
                conversation_id=getattr(agent, "_gateway_session_key", None),
            )
        except Exception as _ce_err:
            _ra().logger.debug("上下文引擎 on_session_start：%s", _ce_err)

    agent._subdirectory_hints = SubdirectoryHintTracker(
        working_dir=os.getenv("TERMINAL_CWD") or None,
    )
    agent._user_turn_count = 0

    # 会话的累积令牌使用情况
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    agent.session_api_calls = 0
    agent.session_input_tokens = 0
    agent.session_output_tokens = 0
    agent.session_cache_read_tokens = 0
    agent.session_cache_write_tokens = 0
    agent.session_reasoning_tokens = 0
    agent.session_estimated_cost_usd = 0.0
    agent.session_cost_status = "unknown"
    agent.session_cost_source = "none"

    # ── Ollama num_ctx 注入 ──
    # Ollama 默认为 2048 上下文，无论模型的功能如何。
    # 当针对 Ollama 服务器运行时，检测模型的最大上下文并在每次聊天请求时传递
    # num_ctx，以便使用完整窗口。
    # 用户覆盖：在 config.yaml 中设置 model.ollama_num_ctx 以限制 VRAM 使用。
    # 如果设置了 model.context_length，它限制 num_ctx，以便即使用户在 config.yaml 中设置了
    # 较小的 context_length，也会尊重用户的 VRAM 预算。
    agent._ollama_num_ctx: int | None = None
    _ollama_num_ctx_override = None
    if isinstance(_model_cfg, dict):
        _ollama_num_ctx_override = _model_cfg.get("ollama_num_ctx")
    if _ollama_num_ctx_override is not None:
        try:
            agent._ollama_num_ctx = int(_ollama_num_ctx_override)
        except (TypeError, ValueError):
            _ra().logger.debug("无效的 ollama_num_ctx 配置值：%r", _ollama_num_ctx_override)
    if agent._ollama_num_ctx is None and agent.base_url and is_local_endpoint(agent.base_url):
        try:
            # ``agent.api_key`` 可能是可调用对象（Entra 令牌提供程序）。
            # Ollama 检测发出手动 HTTP 请求并期望字符串 —
            # Azure Foundry 不是本地端点，因此此分支永远不会为 Entra 触发，
            # 但防御性地保护。
            _key_for_ollama = agent.api_key if isinstance(agent.api_key, str) else ""
            _detected = query_ollama_num_ctx(agent.model, agent.base_url, api_key=_key_for_ollama or "")
            if _detected and _detected > 0:
                agent._ollama_num_ctx = _detected
        except Exception as exc:
            _ra().logger.debug("Ollama num_ctx 检测失败：%s", exc)
    # 将自动检测的 ollama_num_ctx 限制为用户的显式 context_length。
    # 如果没有这个，GGUF 元数据可以通告 256K+，Ollama 通过分配那么多 VRAM 来遵守 —
    # 即使 GGUF 元数据通告更大的窗口，也会使小 GPU 爆炸，即使用户在 config.yaml 中设置了
    # 较小的 context_length。
    if (
        agent._ollama_num_ctx
        and _config_context_length
        and _ollama_num_ctx_override is None  # 不要覆盖显式 ollama_num_ctx
        and agent._ollama_num_ctx > _config_context_length
    ):
        _ra().logger.info(
            "Ollama num_ctx 已限制：%d -> %d（model.context_length 覆盖）",
            agent._ollama_num_ctx, _config_context_length,
        )
        agent._ollama_num_ctx = _config_context_length
    if agent._ollama_num_ctx and not agent.quiet_mode:
        _ra().logger.info(
            "Ollama num_ctx：将请求 %d 个令牌（来自 /api/show 的模型最大值）",
            agent._ollama_num_ctx,
        )

    if not agent.quiet_mode:
        if compression_enabled:
            print(f"📊 上下文限制：{agent.context_compressor.context_length:,} 个令牌（在 {int(compression_threshold*100)}% 处压缩 = {agent.context_compressor.threshold_tokens:,}）")
        else:
            print(f"📊 上下文限制：{agent.context_compressor.context_length:,} 个令牌（自动压缩已禁用）")
        # 当 Codex gpt-5.5 自动提升启动时的一次性通知，包含确切的退出选择命令。
        # 在启动时为 CLI 用户内联打印；
        # 网关用户通过第一轮上的 _compression_warning 收到相同的文本重播
        #（在下面初始化警告槽之后设置）。
        _autoraise = getattr(agent, "_compression_threshold_autoraised", None)
        if _autoraise and compression_enabled:
            print(_build_codex_gpt55_autoraise_notice(_autoraise))

    # 立即检查，以便 CLI 用户在启动时看到警告。
    # 网关 status_callback 尚未连接，因此任何警告都存储在
    # _compression_warning 中，并在第一次 run_conversation() 中重播。
    agent._compression_warning = None
    # Codex gpt-5.5 自动提升通知的网关奇偶性：上面的启动打印仅到达 CLI，
    # 因此在此处存储相同的文本，以便通过 status_callback 在第一轮上重播
    #（Telegram/Discord/Slack 等）。
    _autoraise = getattr(agent, "_compression_threshold_autoraised", None)
    if _autoraise and compression_enabled:
        agent._compression_warning = _build_codex_gpt55_autoraise_notice(_autoraise)
    # 延迟可行性检查：延迟到接近压缩阈值的第一轮。
    # 在此处急切运行它会在每次代理初始化上花费约 400ms 冷（辅助提供商链的
    # 网络探测 + /models 查找），包括从未达到阈值的短 ``chat -q`` 运行。
    # ``ensure_compression_feasibility_checked``（从 ``run_conversation`` 的预飞调用）
    # 对每个代理最多运行一次。
    agent._compression_feasibility_checked = False

    # 快照主运行时以供每轮恢复。
    # 当回退在轮次中激活时，下一轮恢复这些值，以便首选模型每次都获得新的尝试。
    # 使用单个字典，因此很容易添加新的状态字段，而无需 N 个单独的属性。
    _cc = agent.context_compressor
    agent._primary_runtime = {
        "model": agent.model,
        "provider": agent.provider,
        "base_url": agent.base_url,
        "api_mode": agent.api_mode,
        "api_key": getattr(agent, "api_key", ""),
        "client_kwargs": dict(agent._client_kwargs),
        "use_prompt_caching": agent._use_prompt_caching,
        "use_native_cache_layout": agent._use_native_cache_layout,
        # _try_activate_fallback() 覆盖的上下文引擎状态。
        # 对 model/base_url/api_key/provider 使用 getattr，因为插件引擎
        # 可能没有这些（它们特定于 ContextCompressor）。
        "compressor_model": getattr(_cc, "model", agent.model),
        "compressor_base_url": getattr(_cc, "base_url", agent.base_url),
        "compressor_api_key": getattr(_cc, "api_key", ""),
        "compressor_provider": getattr(_cc, "provider", agent.provider),
        "compressor_context_length": _cc.context_length,
        "compressor_threshold_tokens": _cc.threshold_tokens,
    }
    if agent.api_mode == "anthropic_messages":
        agent._primary_runtime.update({
            "anthropic_api_key": agent._anthropic_api_key,
            "anthropic_base_url": agent._anthropic_base_url,
            "is_anthropic_oauth": agent._is_anthropic_oauth,
        })



__all__ = ["init_agent"]
