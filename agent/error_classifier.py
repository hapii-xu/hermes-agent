"""API 错误分类，用于智能故障切换和恢复。

提供 API 错误的结构化分类体系，以及一个按优先级排序的分类管道，
用于确定正确的恢复动作（重试、轮换凭证、回退到其他提供商、
压缩上下文或中止）。

用集中化的分类器替代分散的内联字符串匹配，
run_agent.py 中的主重试循环在每次 API 失败时都会调用该分类器。
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ── 错误分类体系 ──────────────────────────────────────────────────────

class FailoverReason(enum.Enum):
    """API 调用失败的原因 —— 决定恢复策略。"""

    # 认证 / 授权
    auth = "auth"                        # 瞬时认证失败 (401/403) —— 刷新/轮换
    auth_permanent = "auth_permanent"    # 刷新后仍认证失败 —— 中止

    # 账单 / 配额
    billing = "billing"                  # 402 或确认的积分耗尽 —— 立即轮换
    rate_limit = "rate_limit"            # 429 或基于配额的限流 —— 退避后轮换

    # 服务端
    overloaded = "overloaded"            # 503/529 —— 提供商过载，退避
    server_error = "server_error"        # 500/502 —— 内部服务器错误，重试

    # 传输
    timeout = "timeout"                  # 连接/读取超时 —— 重建客户端 + 重试

    # 上下文 / 载荷
    context_overflow = "context_overflow"  # 上下文过大 —— 压缩，而非故障切换
    payload_too_large = "payload_too_large"  # 413 —— 压缩载荷
    image_too_large = "image_too_large"   # 原生图片超过提供商单图上限 —— 缩小后重试

    # 模型 / 提供商策略
    model_not_found = "model_not_found"  # 404 或无效模型 —— 回退到不同模型
    provider_policy_blocked = "provider_policy_blocked"  # 聚合器（如 OpenRouter）因账户数据/隐私策略屏蔽了唯一可用 endpoint
    content_policy_blocked = "content_policy_blocked"  # 提供商安全过滤器拒绝本次请求 —— 对每次请求确定性，不重试未变内容

    # 请求格式
    format_error = "format_error"        # 400 错误请求 —— 中止或剥离后重试
    invalid_encrypted_content = "invalid_encrypted_content"  # Responses 重放 blob 被拒绝 —— 剥离重放状态后重试
    multimodal_tool_content_unsupported = "multimodal_tool_content_unsupported"  # 提供商拒绝 tool 消息中的列表类型内容（如小米 MiMo）—— 降级为文本后重试

    # 提供商特定
    thinking_signature = "thinking_signature"  # Anthropic thinking 块签名无效
    long_context_tier = "long_context_tier"    # Anthropic "extra usage" 层级门控
    oauth_long_context_beta_forbidden = "oauth_long_context_beta_forbidden"  # Anthropic OAuth 订阅拒绝 1M 上下文 beta —— 禁用 beta 后重试
    llama_cpp_grammar_pattern = "llama_cpp_grammar_pattern"  # llama.cpp 的 json-schema-to-grammar 拒绝 `pattern`/`format` 中的正则转义 —— 从 tools 中剥离后重试

    # 兜底
    unknown = "unknown"                  # 无法分类 —— 带退避重试


# ── 分类结果 ───────────────────────────────────────────────

@dataclass
class ClassifiedError:
    """带恢复提示的 API 错误结构化分类。"""

    reason: FailoverReason
    status_code: Optional[int] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    message: str = ""
    error_context: Dict[str, Any] = field(default_factory=dict)

    # 恢复动作提示 —— 重试循环检查这些字段，而非重新分类错误本身。
    retryable: bool = True
    should_compress: bool = False
    should_rotate_credential: bool = False
    should_fallback: bool = False

    @property
    def is_auth(self) -> bool:
        return self.reason in {FailoverReason.auth, FailoverReason.auth_permanent}



# ── 提供商特定模式 ──────────────────────────────────────────

# 表明账单耗尽（而非瞬时速率限制）的模式
_BILLING_PATTERNS = [
    "insufficient credits",
    "insufficient_quota",
    "insufficient balance",
    "credit balance",
    "credits exhausted",
    "credits have been exhausted",
    "no usable credits",
    "top up your credits",
    "payment required",
    "billing hard limit",
    "exceeded your current quota",
    "account is deactivated",
    "plan does not include",
    "out of funds",
    "run out of funds",
    "balance_depleted",
    "model_not_supported_on_free_tier",
    "not available on the free tier",
]

# 表明速率限制（瞬时，会自动恢复）的模式
_RATE_LIMIT_PATTERNS = [
    "rate limit",
    "rate_limit",
    "too many requests",
    "throttled",
    "requests per minute",
    "tokens per minute",
    "requests per day",
    "try again in",
    "please retry after",
    "resource_exhausted",
    "rate increased too quickly",  # 阿里巴巴/DashScope 限流
    # AWS Bedrock 限流
    "throttlingexception",
    "too many concurrent requests",
    "servicequotaexceededexception",
]

# 需要歧义消解的用量限制模式（可能是账单或速率限制）
_USAGE_LIMIT_PATTERNS = [
    "usage limit",
    "quota",
    "limit exceeded",
    "key limit exceeded",
]

# 确认用量限制为瞬时（非账单）的信号
_USAGE_LIMIT_TRANSIENT_SIGNALS = [
    "try again",
    "retry",
    "resets at",
    "reset in",
    "wait",
    "requests remaining",
    "periodic",
    "window",
]

# 从消息文本检测的载荷过大模式（无 status_code 属性）。
# 代理和某些后端将 HTTP 状态嵌入错误消息中。
_PAYLOAD_TOO_LARGE_PATTERNS = [
    "request entity too large",
    "payload too large",
    "error code: 413",
]

# 图片大小模式。与 400 响应体匹配（而非 413），因为大多数提供商在整个请求
# 达到 413 大小限制之前，会先以特定的图片过大消息返回 400。
# Anthropic 的措辞最为重要（单图硬性 5 MB 限制，返回为
# "messages.N.content.K.image.source.base64: image exceeds 5 MB maximum"）。
_IMAGE_TOO_LARGE_PATTERNS = [
    "image exceeds",        # Anthropic: "image exceeds 5 MB maximum"
    "image too large",      # 通用
    "image_too_large",      # error_code 变体
    "image size exceeds",   # 变体
    "image dimensions exceed",  # Anthropic: "image dimensions exceed max allowed size: 8000 pixels"
    "dimensions exceed max allowed size",  # Anthropic 尺寸上限（措辞变体）
    "max allowed size: 8000",  # Anthropic 尺寸上限（明确的像素上限）
    # 已知包含图片的请求中的 "request_too_large" → 图片可能是罪魁祸首；
    # 在放弃前仍尝试缩小路径。
]

# 严格遵循 OpenAI 规范的提供商要求 tool 消息的 ``content`` 为字符串。
# 部分提供商（Anthropic native、Codex Responses、Gemini native、
# 第一方 OpenAI）扩展支持内容部件列表（text + image_url），
# 使 computer_use 截图得以传递。其他提供商（小米 MiMo、部分阿里巴巴
# endpoint、众多 OpenAI 兼容提供商）以 400 拒绝列表 ——
# 下列模式是最常见的错误形式。恢复方式：就地剥离 tool 消息中的图片部件，
# 记录 (provider, model) 以供本次会话后续使用，避免再次学习相同教训，然后重试。
#
# 参见：https://github.com/NousResearch/hermes-agent/issues/27344
_MULTIMODAL_TOOL_CONTENT_PATTERNS = [
    # 小米 MiMo: {"error":{"code":"400","message":"Param Incorrect","param":"text is not set"}}
    "text is not set",
    # 通用 "tool message must be string" 形式
    "tool message content must be a string",
    "tool content must be a string",
    "tool message must be a string",
    # 以 schema 验证消息拒绝列表类型 tool 内容的 OpenAI 兼容服务器
    "expected string, got list",
    "expected string, got array",
    # 阿里巴巴/DashScope 变体
    "tool_call.content must be string",
]

# 上下文溢出模式
_CONTEXT_OVERFLOW_PATTERNS = [
    "context length",
    "context size",
    "maximum context",
    "token limit",
    "too many tokens",
    "reduce the length",
    "exceeds the limit",
    "context window",
    "prompt is too long",
    "prompt exceeds max length",
    "max_tokens",
    "maximum number of tokens",
    # vLLM / 本地推理服务器模式
    "exceeds the max_model_len",
    "max_model_len",
    "prompt length",             # "engine prompt length X exceeds"
    "input is too long",
    "maximum model length",
    # Ollama 模式
    "context length exceeded",
    "truncating input",
    # llama.cpp / llama-server 模式
    "slot context",              # "slot context: N tokens, prompt N tokens"
    "n_ctx_slot",
    # 中文错误消息（部分提供商返回此类消息）
    "超过最大长度",
    "上下文长度",
    # AWS Bedrock Converse API 错误模式
    "input is too long",
    "max input token",
    "input token",
    "exceeds the maximum number of input tokens",
]

# 模型未找到模式
_MODEL_NOT_FOUND_PATTERNS = [
    "is not a valid model",
    "invalid model",
    "model not found",
    "model_not_found",
    "does not exist",
    "no such model",
    "unknown model",
    "unsupported model",
]

# 请求验证模式 —— 请求格式错误，每次重试都会相同失败。
# 某些 OpenAI 兼容 gateway（尤其是 codex.nekos.me）以 5xx 而非标准 4xx
# 返回这些错误，导致通用"5xx → 可重试 server_error"规则误触发：
# 重试循环对同一确定性拒绝冲击 3+ 次，然后传输恢复路径重置计数器再次冲击，
# 产生请求洪泛。当 5xx 响应体携带这些明确的请求验证信号时，
# 分类为不可重试的 format_error，使循环快速失败并回退而非循环。
_REQUEST_VALIDATION_PATTERNS = [
    "unknown parameter",
    "unsupported parameter",
    "unrecognized request argument",
    "invalid_request_error",
    "unknown_parameter",
    "unsupported_parameter",
]

# OpenRouter 聚合器策略屏蔽模式。
#
# 当用户的 OpenRouter 账户隐私设置（或每次请求的
# `provider.data_collection: deny` 偏好）排除了服务某模型的唯一 endpoint 时，
# OpenRouter 返回 404，附带与"模型未找到"不同的*特定*消息：
#
#   "No endpoints available matching your guardrail restrictions and
#    data policy. Configure: https://openrouter.ai/settings/privacy"
#
# 我们将其分类为 `provider_policy_blocked` 而非 `model_not_found`，因为：
#   - 模型*存在* —— model_not_found 在日志中具有误导性
#   - 提供商回退无济于事：账户级设置适用于同一 OpenRouter 账户的每次调用
#   - 错误体已包含修复 URL，用户无需我们重写消息即可获得可操作的指引
_PROVIDER_POLICY_BLOCKED_PATTERNS = [
    "no endpoints available matching your guardrail",
    "no endpoints available matching your data policy",
    "no endpoints found matching your data policy",
]

# 提供商内容策略/安全过滤器屏蔽。与上方 ``provider_policy_blocked``
# 不同（后者是 OpenRouter *账户*级数据/隐私护栏）——
# 这些是上游模型提供商做出的*每请求*安全决策。
# 对于未修改的请求，它们是确定性的，因此重试同一提示词三次
# 只会重现相同的屏蔽并在拒绝上浪费付费尝试。
# 恢复方式是立即切换到已配置的回退模型/提供商，
# 或在没有回退时向用户展示屏蔽信息及可操作的指引。
#
# 模式有意设计得很窄 —— 每个短语都是特定提供商安全管道的逐字字符串，
# 而非可能与账单/认证/格式错误冲突的通用词汇（如"policy"或"violation"）：
#   • OpenAI Codex 网络安全拒绝（gpt-5.5，来自 #18028 的情形）
#   • OpenAI 内容审核拒绝（"violates our usage policies"，
#     "usage policies" 可与账单的"exceeded ... policy"区分）
#   • Anthropic 安全拒绝（"prompt was flagged by ... safety system"）
#   • OpenAI Responses 内容过滤器
_CONTENT_POLICY_BLOCKED_PATTERNS = [
    # OpenAI Codex (#18028) —— 消息可能不带 HTTP 状态
    "flagged for possible cybersecurity risk",
    "trusted access for cyber",
    # OpenAI 内容审核 —— chat completions / responses
    "violates our usage policies",
    "violates openai's usage policies",
    "your request was flagged by",
    # Anthropic 安全系统
    "prompt was flagged by our safety",
    "responses cannot be generated due to safety",
    # Azure / OpenAI Responses 上常见的通用内容过滤措辞。
    # ``content_filter``（下划线）是 OpenAI 标准错误/结束
    # token，由其 SDK 在请求被屏蔽时逐字输出。
    # ``responsibleaipolicyviolation`` 是 Azure OpenAI 的错误代码。
    # 有意不匹配空格变体（"content filter"）—— 它出现在
    # 提供商回显的良性配置描述和工具提示文本中；
    # 下划线形式足够具有提供商特异性。
    "content_filter",
    "responsibleaipolicyviolation",
]

# 认证模式（非状态码信号）
_AUTH_PATTERNS = [
    "invalid api key",
    "invalid_api_key",
    "authentication",
    "unauthorized",
    "forbidden",
    "invalid token",
    "token expired",
    "token revoked",
    "access denied",
]

# Anthropic thinking 块签名模式
_THINKING_SIG_PATTERNS = [
    "signature",  # 与 "thinking" 检查组合使用
]

# 即使异常类型为通用类型（如来自包装子进程超时的本地 shim 的 RuntimeError），
# 也能表明提供商侧超时的消息字符串模式。
# 在基于类型的传输启发式之前检查，以防自定义提供商的"timed out"错误
# 落入 unknown 桶并被误报为空响应。
_TIMEOUT_MESSAGE_PATTERNS = [
    "timed out",
    "turn timed out",
    "request timed out",
    "deadline exceeded",
    "operation timed out",
    "upstream timed out",
]

# 传输错误类型名称
_TRANSPORT_ERROR_TYPES = frozenset({
    "ReadTimeout", "ConnectTimeout", "PoolTimeout",
    "ConnectError", "RemoteProtocolError",
    "ConnectionError", "ConnectionResetError",
    "ConnectionAbortedError", "BrokenPipeError",
    "TimeoutError", "ReadError",
    "ServerDisconnectedError",
    # SSL/TLS 传输错误 —— 流中瞬时的握手/记录失败，应重试而非表现为会话卡住。
    # ssl.SSLError 是 OSError 的子类（被 isinstance 捕获），但我们在此列出
    # 类型名称，以便提供商包装的 SSL 错误（如 SDK 重新抛出时未保留异常链）
    # 仍分类为传输错误，而非落入 unknown 桶。
    "SSLError", "SSLZeroReturnError", "SSLWantReadError",
    "SSLWantWriteError", "SSLEOFError", "SSLSyscallError",
    # OpenAI SDK 错误（非 Python 内置类型的子类）
    "APIConnectionError",
    "APITimeoutError",
})

# 服务器断连模式（无状态码，但属于传输层）。
# 这些是"模糊"模式 —— 普通的连接关闭可能是瞬时传输故障，
# 也可能是服务端上下文溢出拒绝（当 API gateway 因请求过大而断开连接而非
# 返回 HTTP 错误时很常见）。大型会话 + 这些模式之一会触发
# 带压缩的上下文溢出恢复路径。
_SERVER_DISCONNECT_PATTERNS = [
    "server disconnected",
    "peer closed connection",
    "connection reset by peer",
    "connection was closed",
    "network connection lost",
    "unexpected eof",
    "incomplete chunked read",
]

# SSL/TLS 瞬时失败模式 —— 有意与上方 _SERVER_DISCONNECT_PATTERNS 区分。
#
# 流中的 SSL alert 几乎总是传输层故障
# （不稳定网络、会话中 TLS 重协商失败、负载均衡器断开连接）
# —— 而非服务端上下文溢出信号。
# 因此我们需要重试路径但不需要压缩路径；将这些模式归入
# _SERVER_DISCONNECT_PATTERNS 会在任何大型会话的 SSL 故障时触发
# 不必要（且代价高昂）的上下文压缩。
#
# OpenSSL 库通过在大写告警原因前添加格式字符串来构造错误代码；
# OpenSSL 3.x 更改了分隔符
# （如 `SSLV3_ALERT_BAD_RECORD_MAC` → `SSL/TLS_ALERT_BAD_RECORD_MAC`），
# 导致静默地停止匹配任何显式内容。
# 匹配稳定的子字符串（`bad record mac`、`ssl alert`、`tls alert` 等）
# 可在未来 OpenSSL 格式变化中无需代码修改即可生存。
_SSL_TRANSIENT_PATTERNS = [
    # 空格分隔（人类可读形式，Python ssl 模块，大多数 SDK）
    "bad record mac",
    "ssl alert",
    "tls alert",
    "ssl handshake failure",
    "tlsv1 alert",
    "sslv3 alert",
    # 下划线分隔（OpenSSL 错误代码标记，如
    # `ERR_SSL_SSL/TLS_ALERT_BAD_RECORD_MAC`、`SSLV3_ALERT_BAD_RECORD_MAC`）
    "bad_record_mac",
    "ssl_alert",
    "tls_alert",
    "tls_alert_internal_error",
    # Python ssl 模块前缀，如 "[SSL: BAD_RECORD_MAC]"
    "[ssl:",
]


# ── 分类管道 ─────────────────────────────────────────────

def classify_api_error(
    error: Exception,
    *,
    provider: str = "",
    model: str = "",
    approx_tokens: int = 0,
    context_length: int = 200000,
    num_messages: int = 0,
) -> ClassifiedError:
    """将 API 错误分类为结构化的恢复建议。

    按优先级排序的管道：
      1. 特殊处理提供商特定模式（thinking 签名、层级门控）
      2. HTTP 状态码 + 消息感知的细化
      3. 错误代码分类（来自 body）
      4. 消息模式匹配（账单 vs 速率限制 vs 上下文 vs 认证）
      5. SSL/TLS 瞬时告警模式 → 作为 timeout 重试
      6. 服务器断连 + 大型会话 → 上下文溢出
      7. 传输错误启发式
      8. 兜底：unknown（带退避重试）

    Args:
        error: API 调用产生的异常。
        provider: 当前提供商名称（如 "openrouter"、"anthropic"）。
        model: 当前模型标识。
        approx_tokens: 当前上下文的近似 token 数。
        context_length: 当前模型的最大上下文长度。

    Returns:
        带原因和恢复动作提示的 ClassifiedError。
    """
    status_code = _extract_status_code(error)
    error_type = type(error).__name__
    # Copilot/GitHub Models 的 RateLimitError 可能未设置 .status_code；强制设为 429，
    # 使下游速率限制处理（分类器原因、池轮换、回退门控）正确触发，
    # 而非被误分类为通用错误。
    if status_code is None and error_type == "RateLimitError":
        status_code = 429
    body = _extract_error_body(error)
    error_code = _extract_error_code(body)

    # 构建用于模式匹配的综合错误消息字符串。
    # 单独的 str(error) 可能不包含 body 消息（如 OpenAI SDK 的
    # APIStatusError.__str__ 返回第一个参数而非 body）。
    # 追加 body 消息，使 402 歧义消解中"try again"等模式
    # 即使仅出现在结构化 body 中也能被检测到。
    #
    # 同时提取 metadata.raw —— OpenRouter 将上游提供商错误包装在
    # {"error": {"message": "Provider returned error", "metadata":
    # {"raw": "<actual error JSON>"}}} 中，真正的错误消息
    # （如 "context length exceeded"）仅在内层 JSON 中。
    _raw_msg = str(error).lower()
    _body_msg = ""
    _metadata_msg = ""
    if isinstance(body, dict):
        _err_obj = body.get("error", {})
        if isinstance(_err_obj, dict):
            _body_msg = str(_err_obj.get("message") or "").lower()
            # Parse metadata.raw for wrapped provider errors
            _metadata = _err_obj.get("metadata", {})
            if isinstance(_metadata, dict):
                _raw_json = _metadata.get("raw") or ""
                if isinstance(_raw_json, str) and _raw_json.strip():
                    try:
                        import json
                        _inner = json.loads(_raw_json)
                        if isinstance(_inner, dict):
                            _inner_err = _inner.get("error", {})
                            if isinstance(_inner_err, dict):
                                _metadata_msg = str(_inner_err.get("message") or "").lower()
                    except (json.JSONDecodeError, TypeError):
                        pass
        if not _body_msg:
            _body_msg = str(body.get("message") or "").lower()
    # 合并所有消息来源用于模式匹配
    parts = [_raw_msg]
    if _body_msg and _body_msg not in _raw_msg:
        parts.append(_body_msg)
    if _metadata_msg and _metadata_msg not in _raw_msg and _metadata_msg not in _body_msg:
        parts.append(_metadata_msg)
    error_msg = " ".join(parts)
    provider_lower = (provider or "").strip().lower()
    model_lower = (model or "").strip().lower()

    def _result(reason: FailoverReason, **overrides) -> ClassifiedError:
        defaults = {
            "reason": reason,
            "status_code": status_code,
            "provider": provider,
            "model": model,
            "message": _extract_message(error, body),
        }
        defaults.update(overrides)
        return ClassifiedError(**defaults)

    # ── 1. 提供商特定模式（最高优先级） ────────────

    # 提供商内容策略/安全过滤器屏蔽。提供商已对本次请求做出确定性拒绝决定
    # —— 未修改地重试只会重现相同的拒绝并浪费付费尝试。
    # 必须在基于状态码的分类之前运行，以防 400 安全屏蔽被降级为
    # 通用 ``format_error``，以及无状态码的屏蔽（OpenAI Codex SDK
    # 可能不带状态码抛出）被留在可重试的 ``unknown`` 桶中。
    # 参见 issue #18028。
    if any(p in error_msg for p in _CONTENT_POLICY_BLOCKED_PATTERNS):
        return _result(
            FailoverReason.content_policy_blocked,
            retryable=False,
            should_fallback=True,
        )

    # Anthropic thinking 块恢复（400）。两种不同的失败模式，
    # 相同的恢复方式（剥离所有 reasoning_details 并不带 thinking 块重试
    # —— 参见 conversation_loop.py 中的 thinking_signature 处理器）：
    #   1. 签名不匹配：thinking 块针对完整 turn 内容签名；
    #      任何上游修改（上下文压缩、会话截断、消息合并）都会使签名失效。
    #      模式："signature" + "thinking"。
    #   2. 冻结块修改：Anthropic 拒绝对*最新* assistant 消息中的
    #      thinking/redacted_thinking 块的任何修改 ——
    #      "最新 assistant 消息中的 `thinking` 或 `redacted_thinking` 块不能被修改，
    #      这些块必须与原始响应中的完全一致。"
    #      此情形不带 "signature" token，因此原始模式未能捕获，
    #      turn 以不可重试的客户端错误硬中止，而非自愈。
    #      模式："thinking" + ("cannot be modified" | "must remain as they were")。
    # 不对 provider 进行门控 —— OpenRouter 代理 Anthropic 错误，
    # 因此 provider 可能是 "openrouter"，即使错误是 Anthropic 特定的。
    # 组合模式已足够唯一。
    if (
        status_code == 400
        and "thinking" in error_msg
        and (
            "signature" in error_msg
            or "cannot be modified" in error_msg
            or "must remain as they were" in error_msg
        )
    ):
        return _result(
            FailoverReason.thinking_signature,
            retryable=True,
            should_compress=False,
        )

    # Anthropic 长上下文层级门控（429 "extra usage" + "long context"）
    if (
        status_code == 429
        and "extra usage" in error_msg
        and "long context" in error_msg
    ):
        return _result(
            FailoverReason.long_context_tier,
            retryable=True,
            should_compress=True,
        )

    # Anthropic OAuth 订阅拒绝 1M 上下文 beta 请求头。
    # 观察到的错误体："The long context beta is not yet available for this subscription."
    # 当订阅不包含 1M 上下文时，即使请求携带
    # ``anthropic-beta: context-1m-2025-08-07``，也从原生 Anthropic 返回 HTTP 400。
    # run_agent.py 中的恢复路径会剥离 beta 头重建 Anthropic 客户端并重试一次。
    # 模式足够窄，不会与上方的 429 层级门控模式冲突（不同状态码、不同短语）。
    if (
        status_code == 400
        and "long context beta" in error_msg
        and "not yet available" in error_msg
    ):
        return _result(
            FailoverReason.oauth_long_context_beta_forbidden,
            retryable=True,
            should_compress=False,
        )

    # llama.cpp 的 ``json-schema-to-grammar`` 转换器（其 OAI 服务器用于
    # 构建 GBNF tool 调用解析器）拒绝 ``\d``/``\w``/``\s`` 等正则转义类
    # 和大多数 ``format`` 值。MCP 服务器经常为日期/电话/邮件参数
    # 输出 ``"pattern": "\\d{4}-\\d{2}-\\d{2}"``。
    # llama.cpp 以 HTTP 400 和几个可识别的短语之一来表达；
    # 匹配时我们在重试循环中从 ``self.tools`` 剥离 ``pattern``/``format``
    # 并重试一次。云提供商不受影响 —— 它们接受这些关键词，我们永远不会进入此分支。
    if (
        status_code == 400
        and (
            "error parsing grammar" in error_msg
            or "json-schema-to-grammar" in error_msg
            or (
                "unable to generate parser" in error_msg
                and "template" in error_msg
            )
        )
    ):
        return _result(
            FailoverReason.llama_cpp_grammar_pattern,
            retryable=True,
            should_compress=False,
        )

    # xAI Grok 订阅权限错误。
    #
    # xAI 通过两条不同的代码路径返回
    # "You have either run out of available resources or do not have an active Grok subscription"：
    #
    #   • HTTP 403 —— status_code 已设置；_classify_by_status（步骤 2）
    #     正确将其路由到 FailoverReason.auth，然后 _is_entitlement_failure
    #     阻止凭证刷新循环。
    #
    #   • SSE ``type=error`` 帧 —— 以 status_code=None 的 _StreamErrorEvent 出现。
    #     _classify_by_status 被完全跳过，
    #     "grok subscription" / "out of available resources" 未出现在
    #     下方任何消息模式列表中。没有此守卫，错误会落入
    #     FailoverReason.unknown（retryable=True），在 agent 停止前
    #     耗尽 max_retries —— 且 _is_entitlement_failure 永远不会被调用，
    #     因为它只在 FailoverReason.auth 下运行。
    #
    # X Premium+ 和 SuperGrok 订阅者在其订阅层级不覆盖请求的模型或功能时
    # 都会触发此路径。
    if (
        "do not have an active grok subscription" in error_msg
        or ("out of available resources" in error_msg and "grok" in error_msg)
    ):
        return _result(
            FailoverReason.auth,
            retryable=False,
            should_fallback=True,
        )

    # ── 2. HTTP 状态码分类 ──────────────────────────

    if status_code is not None:
        classified = _classify_by_status(
            status_code, error_msg, error_code, body,
            provider=provider_lower, model=model_lower,
            approx_tokens=approx_tokens, context_length=context_length,
            num_messages=num_messages,
            result_fn=_result,
        )
        if classified is not None:
            return classified

    # ── 3. 错误代码分类 ────────────────────────────────

    if error_code:
        classified = _classify_by_error_code(error_code, error_msg, _result)
        if classified is not None:
            return classified

    # ── 4. 消息模式匹配（无状态码） ────────────────

    classified = _classify_by_message(
        error_msg, error_type,
        approx_tokens=approx_tokens,
        context_length=context_length,
        result_fn=_result,
    )
    if classified is not None:
        return classified

    # ── 5. SSL/TLS 瞬时错误 → 作为 timeout 重试（非压缩） ──
    # 流中的 SSL alert 是传输层故障，而非服务端上下文溢出信号。
    # 在断连检查之前分类，以防大型会话在真实原因是不稳定的 TLS 握手时
    # 错误触发上下文压缩。当错误被包装在通用异常中、消息字符串携带
    # SSL alert 文本但类型不是 ssl.SSLError 时也能匹配
    # （某些重新抛出时不保留异常链的 SDK 会出现此情况）。
    if any(p in error_msg for p in _SSL_TRANSIENT_PATTERNS):
        return _result(FailoverReason.timeout, retryable=True)

    # ── 6. 服务器断连 + 大型会话 → 上下文溢出 ─────
    # 必须在通用传输错误捕获之前 —— 大型会话中的断连更可能是上下文溢出，
    # 而非瞬时传输故障。没有这个顺序，RemoteProtocolError
    # 总是映射到 timeout，无论会话大小如何。

    is_disconnect = any(p in error_msg for p in _SERVER_DISCONNECT_PATTERNS)
    if is_disconnect and not status_code:
        # 绝对 token/消息数阈值只是较小上下文窗口的代理。
        # 大上下文会话可能有数百条消息，但仍远低于其实际 token 预算。
        is_large = approx_tokens > context_length * 0.6 or (
            context_length <= 256000 and (approx_tokens > 120000 or num_messages > 200)
        )
        if is_large:
            return _result(
                FailoverReason.context_overflow,
                retryable=True,
                should_compress=True,
            )
        return _result(FailoverReason.timeout, retryable=True)

    # ── 7. 传输/超时启发式 ───────────────────────────

    if error_type in _TRANSPORT_ERROR_TYPES or isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return _result(FailoverReason.timeout, retryable=True)

    # ── 8. 兜底：unknown ────────────────────────────────────────

    return _result(FailoverReason.unknown, retryable=True)


# ── 状态码分类 ──────────────────────────────────────────

def _classify_by_status(
    status_code: int,
    error_msg: str,
    error_code: str,
    body: dict,
    *,
    provider: str,
    model: str,
    approx_tokens: int,
    context_length: int,
    num_messages: int = 0,
    result_fn,
) -> Optional[ClassifiedError]:
    """基于 HTTP 状态码（带消息感知细化）进行分类。"""

    if status_code == 401:
        # 本身不可重试 —— 凭证池轮换和提供商特定刷新（Codex、Anthropic、Nous）
        # 在 run_agent.py 中的可重试性检查之前运行。
        # 若成功，循环 `continue`；若失败，retryable=False 确保
        # 进入客户端错误中止路径（该路径会先尝试回退）。
        return result_fn(
            FailoverReason.auth,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    if status_code == 403:
        # OpenRouter 的 403 "key limit exceeded" 实际上是账单问题。
        # 其他提供商也使用 403 表示账户计划或积分耗尽。
        if (
            "key limit exceeded" in error_msg
            or "spending limit" in error_msg
            or any(p in error_msg for p in _BILLING_PATTERNS)
        ):
            return result_fn(
                FailoverReason.billing,
                retryable=False,
                should_rotate_credential=True,
                should_fallback=True,
            )
        return result_fn(
            FailoverReason.auth,
            retryable=False,
            should_fallback=True,
        )

    if status_code == 402:
        return _classify_402(error_msg, result_fn)

    if status_code == 404:
        # Nous API 目前将 HA/NAS 积分耗尽表现为付费模型在免费层不可用，
        # 以 404 而非 402 返回。将其视为权限/账单耗尽，而非模型缺失，
        # 以便重试循环可以显示积分/充值指引。
        if any(p in error_msg for p in _BILLING_PATTERNS):
            return result_fn(
                FailoverReason.billing,
                retryable=False,
                should_rotate_credential=True,
                should_fallback=True,
            )
        # OpenRouter 策略屏蔽 404 —— 与"模型未找到"不同。
        # 模型存在；用户的账户隐私设置排除了服务该模型的唯一 endpoint。
        # 回退到另一个提供商无济于事（相同的账户设置适用）。
        # 错误体已包含修复 URL，直接展示即可。
        if any(p in error_msg for p in _PROVIDER_POLICY_BLOCKED_PATTERNS):
            return result_fn(
                FailoverReason.provider_policy_blocked,
                retryable=False,
                should_fallback=False,
            )
        if any(p in error_msg for p in _MODEL_NOT_FOUND_PATTERNS):
            return result_fn(
                FailoverReason.model_not_found,
                retryable=False,
                should_fallback=True,
            )
        # 不带"模型未找到"信号的通用 404 —— 可能是错误的 endpoint 路径
        # （本地 llama.cpp / Ollama / vLLM URL 略微配置错误时很常见）、
        # 代理路由故障或瞬时后端问题。
        # 将这些分类为 model_not_found 会静默回退到不同提供商，
        # 并告知模型该模型缺失，这是错误的且浪费一次 turn。
        # 视为 unknown 以便重试循环展示真实错误。
        return result_fn(
            FailoverReason.unknown,
            retryable=True,
        )

    if status_code == 413:
        return result_fn(
            FailoverReason.payload_too_large,
            retryable=True,
            should_compress=True,
        )

    if status_code == 429:
        # 已在上方检查过 long_context_tier；这是正常的速率限制
        return result_fn(
            FailoverReason.rate_limit,
            retryable=True,
            should_rotate_credential=True,
            should_fallback=True,
        )

    if status_code == 400:
        return _classify_400(
            error_msg, error_code, body,
            provider=provider, model=model,
            approx_tokens=approx_tokens,
            context_length=context_length,
            num_messages=num_messages,
            result_fn=result_fn,
        )

    if status_code in {500, 502}:
        # 某些 OpenAI 兼容 gateway 以 5xx 状态返回请求验证错误
        # （codex.nekos.me 对未知/不支持的参数返回 502）。
        # 这些是确定性的 —— 每次重试都会得到相同的拒绝 ——
        # 因此通用的"5xx → 可重试 server_error"规则会将一个坏请求
        # 变成重试洪泛。检测明确的请求验证信号（在消息文本或
        # 结构化错误代码中）并快速失败。
        if (
            any(p in error_msg for p in _REQUEST_VALIDATION_PATTERNS)
            or error_code.lower() in {"invalid_request_error", "unknown_parameter",
                                      "unsupported_parameter"}
        ):
            return result_fn(
                FailoverReason.format_error,
                retryable=False,
                should_fallback=True,
            )
        return result_fn(FailoverReason.server_error, retryable=True)

    if status_code in {503, 529}:
        return result_fn(FailoverReason.overloaded, retryable=True)

    # 其他 4xx —— 不可重试
    if 400 <= status_code < 500:
        return result_fn(
            FailoverReason.format_error,
            retryable=False,
            should_fallback=True,
        )

    # 其他 5xx —— 可重试
    if 500 <= status_code < 600:
        return result_fn(FailoverReason.server_error, retryable=True)

    return None


def _classify_402(error_msg: str, result_fn) -> ClassifiedError:
    """消解 402 的歧义：账单耗尽 vs 瞬时用量限制。

    来自 OpenClaw 的关键洞察：部分 402 是伪装成支付错误的瞬时速率限制。
    "Usage limit, try again in 5 minutes" 不是账单问题
    —— 它是一个会重置的周期性配额。
    """
    # 首先检查瞬时用量限制信号
    has_usage_limit = any(p in error_msg for p in _USAGE_LIMIT_PATTERNS)
    has_transient_signal = any(p in error_msg for p in _USAGE_LIMIT_TRANSIENT_SIGNALS)

    if has_usage_limit and has_transient_signal:
        # 瞬时配额 —— 视为速率限制，而非账单
        return result_fn(
            FailoverReason.rate_limit,
            retryable=True,
            should_rotate_credential=True,
            should_fallback=True,
        )

    # 确认的账单耗尽
    return result_fn(
        FailoverReason.billing,
        retryable=False,
        should_rotate_credential=True,
        should_fallback=True,
    )


def _classify_400(
    error_msg: str,
    error_code: str,
    body: dict,
    *,
    provider: str,
    model: str,
    approx_tokens: int,
    context_length: int,
    num_messages: int = 0,
    result_fn,
) -> ClassifiedError:
    """分类 400 Bad Request —— 上下文溢出、格式错误或通用错误。"""

    # 400 中被拒绝的多模态 tool 内容。必须在 image_too_large 之前检查，
    # 因为恢复方式不同（从 tool 消息中剥离图片部件，将模型标记为
    # 本次会话中不支持列表类型 tool 内容），并在 context_overflow 之前检查，
    # 因为部分模式（"text is not set"）单独来看是模糊的，
    # 但与已知包含多模态 tool 内容的请求的 400 组合时会变得具体。
    if any(p in error_msg for p in _MULTIMODAL_TOOL_CONTENT_PATTERNS):
        return result_fn(
            FailoverReason.multimodal_tool_content_unsupported,
            retryable=True,
        )

    # 400 中的图片过大（Anthropic 的每图 5 MB 检查以此方式触发）。
    # 必须在 context_overflow 之前检查，因为消息可能同时触发两个模式
    # （"exceeds" + "image"），且图片缩小是更廉价的恢复方式。
    if any(p in error_msg for p in _IMAGE_TOO_LARGE_PATTERNS):
        return result_fn(
            FailoverReason.image_too_large,
            retryable=True,
        )

    # 无效的加密推理重放 blob（OpenAI Responses API）。必须在 context_overflow
    # 之前检查，因为某些界面输出的消息包含类似上下文的措辞
    # （"encrypted content … could not be verified"），否则可能触发
    # context_overflow 启发式。``error_msg`` 在上游已转为小写 —— 相应地匹配。
    error_code_lower = (error_code or "").lower()
    if (
        error_code_lower == "invalid_encrypted_content"
        or "invalid_encrypted_content" in error_msg
        or (
            "encrypted content for item" in error_msg
            and "could not be verified" in error_msg
        )
    ):
        return result_fn(
            FailoverReason.invalid_encrypted_content,
            retryable=True,
            should_fallback=False,
        )

    # 请求验证错误（不支持的/未知参数）必须在 context_overflow 之前检查。
    # 拒绝 max_tokens 的 GPT-5 模型返回：
    #   "Unsupported parameter: 'max_tokens' is not supported with this model.
    #    Use 'max_completion_tokens' instead."
    # 该字符串包含字面子字符串 "max_tokens"，这是 _CONTEXT_OVERFLOW_PATTERNS
    # 之一 —— 因此没有此守卫时，400 会被误分类为 context_overflow，
    # 进入压缩循环，以相同的错误参数重发，最终以"Cannot compress further"结束。
    # 这些错误是确定性的（每次重试都得到相同的拒绝），
    # 因此分类为不可重试的 format_error 并回退。
    #
    # 注意：我们有意不对通用 ``invalid_request_error`` 代码进行键控 ——
    # OpenAI 也将相同代码用于真正的上下文溢出 400，
    # 匹配它会将真实溢出从压缩路径错误路由走。
    # 明确的信号是显式的"unsupported/unknown parameter"消息文本
    # 和特定的参数级错误代码。
    if (
        any(p in error_msg for p in _REQUEST_VALIDATION_PATTERNS
            if p != "invalid_request_error")
        or error_code_lower in {"unknown_parameter", "unsupported_parameter"}
    ):
        return result_fn(
            FailoverReason.format_error,
            retryable=False,
            should_fallback=True,
        )

    # 400 中的上下文溢出
    if any(p in error_msg for p in _CONTEXT_OVERFLOW_PATTERNS):
        return result_fn(
            FailoverReason.context_overflow,
            retryable=True,
            should_compress=True,
        )

    # 部分提供商将模型未找到以 400 而非 404 返回（如 OpenRouter）。
    if any(p in error_msg for p in _PROVIDER_POLICY_BLOCKED_PATTERNS):
        return result_fn(
            FailoverReason.provider_policy_blocked,
            retryable=False,
            should_fallback=False,
        )
    if any(p in error_msg for p in _MODEL_NOT_FOUND_PATTERNS):
        return result_fn(
            FailoverReason.model_not_found,
            retryable=False,
            should_fallback=True,
        )

    # 部分提供商将速率限制/账单错误以 400 而非 429/402 返回。
    # 在落入 format_error 之前检查这些模式。
    if any(p in error_msg for p in _RATE_LIMIT_PATTERNS):
        return result_fn(
            FailoverReason.rate_limit,
            retryable=True,
            should_rotate_credential=True,
            should_fallback=True,
        )
    if any(p in error_msg for p in _BILLING_PATTERNS):
        return result_fn(
            FailoverReason.billing,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    # 通用 400 + 大型会话 → 可能的上下文溢出
    # Anthropic 有时在上下文过大时返回裸"Error"消息
    err_body_msg = ""
    if isinstance(body, dict):
        err_obj = body.get("error", {})
        if isinstance(err_obj, dict):
            err_body_msg = str(err_obj.get("message") or "").strip().lower()
        # Responses API（以及部分提供商）使用扁平 body：{"message": "..."}
        if not err_body_msg:
            err_body_msg = str(body.get("message") or "").strip().lower()
    is_generic = len(err_body_msg) < 30 or err_body_msg in {"error", ""}
    # 绝对 token/消息数阈值只是较小上下文窗口的代理。
    # 大上下文会话可能有很多消息，但仍远低于其实际 token 预算。
    is_large = approx_tokens > context_length * 0.4 or (
        context_length <= 256000 and (approx_tokens > 80000 or num_messages > 80)
    )

    if is_generic and is_large:
        return result_fn(
            FailoverReason.context_overflow,
            retryable=True,
            should_compress=True,
        )

    # 不可重试的格式错误
    return result_fn(
        FailoverReason.format_error,
        retryable=False,
        should_fallback=True,
    )


# ── 错误代码分类 ───────────────────────────────────────────

def _classify_by_error_code(
    error_code: str, error_msg: str, result_fn,
) -> Optional[ClassifiedError]:
    """通过响应体中的结构化错误代码进行分类。"""
    code_lower = error_code.lower()

    if code_lower in {"resource_exhausted", "throttled", "rate_limit_exceeded"}:
        return result_fn(
            FailoverReason.rate_limit,
            retryable=True,
            should_rotate_credential=True,
        )

    if code_lower in {
        "insufficient_quota",
        "billing_not_active",
        "payment_required",
        "insufficient_credits",
        "no_usable_credits",
        "balance_depleted",
        "model_not_supported_on_free_tier",
    }:
        return result_fn(
            FailoverReason.billing,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    if code_lower in {"model_not_found", "model_not_available", "invalid_model"}:
        return result_fn(
            FailoverReason.model_not_found,
            retryable=False,
            should_fallback=True,
        )

    if code_lower in {"context_length_exceeded", "max_tokens_exceeded"}:
        return result_fn(
            FailoverReason.context_overflow,
            retryable=True,
            should_compress=True,
        )

    if code_lower == "invalid_encrypted_content":
        return result_fn(
            FailoverReason.invalid_encrypted_content,
            retryable=True,
            should_fallback=False,
        )

    return None


# ── 消息模式分类 ──────────────────────────────────────────

def _classify_by_message(
    error_msg: str,
    error_type: str,
    *,
    approx_tokens: int,
    context_length: int,
    result_fn,
) -> Optional[ClassifiedError]:
    """在无状态码时基于错误消息模式进行分类。"""

    # 载荷过大模式（无 status_code 时从消息文本检测）
    if any(p in error_msg for p in _PAYLOAD_TOO_LARGE_PATTERNS):
        return result_fn(
            FailoverReason.payload_too_large,
            retryable=True,
            should_compress=True,
        )

    # 多模态 tool 内容模式（无 status_code 时从消息文本检测）
    if any(p in error_msg for p in _MULTIMODAL_TOOL_CONTENT_PATTERNS):
        return result_fn(
            FailoverReason.multimodal_tool_content_unsupported,
            retryable=True,
        )

    # 图片过大模式（无 status_code 时从消息文本检测）
    if any(p in error_msg for p in _IMAGE_TOO_LARGE_PATTERNS):
        return result_fn(
            FailoverReason.image_too_large,
            retryable=True,
        )

    # 用量限制模式需要与 402 相同的歧义消解：部分提供商不带 HTTP 状态码
    # 输出"usage limit"错误。瞬时信号（"try again"、"resets at"等）
    # 意味着这是周期性配额，而非账单耗尽。
    has_usage_limit = any(p in error_msg for p in _USAGE_LIMIT_PATTERNS)
    if has_usage_limit:
        has_transient_signal = any(p in error_msg for p in _USAGE_LIMIT_TRANSIENT_SIGNALS)
        if has_transient_signal:
            return result_fn(
                FailoverReason.rate_limit,
                retryable=True,
                should_rotate_credential=True,
                should_fallback=True,
            )
        return result_fn(
            FailoverReason.billing,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    # 账单模式
    if any(p in error_msg for p in _BILLING_PATTERNS):
        return result_fn(
            FailoverReason.billing,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    # 速率限制模式
    if any(p in error_msg for p in _RATE_LIMIT_PATTERNS):
        return result_fn(
            FailoverReason.rate_limit,
            retryable=True,
            should_rotate_credential=True,
            should_fallback=True,
        )

    # 上下文溢出模式
    if any(p in error_msg for p in _CONTEXT_OVERFLOW_PATTERNS):
        return result_fn(
            FailoverReason.context_overflow,
            retryable=True,
            should_compress=True,
        )

    # 认证模式
    # 认证错误不应直接重试 —— 凭证无效，用相同密钥重试总会失败。
    # 设置 retryable=False，使调用方触发凭证轮换（should_rotate_credential=True）
    # 或提供商回退，而非立即重试循环。
    if any(p in error_msg for p in _AUTH_PATTERNS):
        return result_fn(
            FailoverReason.auth,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    # 提供商策略屏蔽（聚合器侧护栏）—— 在 model_not_found 之前检查，
    # 以防被误标为模型缺失。
    if any(p in error_msg for p in _PROVIDER_POLICY_BLOCKED_PATTERNS):
        return result_fn(
            FailoverReason.provider_policy_blocked,
            retryable=False,
            should_fallback=False,
        )

    # 模型未找到模式
    if any(p in error_msg for p in _MODEL_NOT_FOUND_PATTERNS):
        return result_fn(
            FailoverReason.model_not_found,
            retryable=False,
            should_fallback=True,
        )

    # 超时消息模式 —— 本地 shim 或内部包装子进程/HTTP 超时的自定义提供商
    # 抛出的通用异常类型（如 RuntimeError）。
    # 分类为传输超时，使重试循环重建客户端，而非将 turn 视为空模型响应。
    if any(p in error_msg for p in _TIMEOUT_MESSAGE_PATTERNS):
        return result_fn(FailoverReason.timeout, retryable=True)

    return None


# ── 辅助函数 ─────────────────────────────────────────────────────────────

def _extract_status_code(error: Exception) -> Optional[int]:
    """遍历错误及其原因链以查找 HTTP 状态码。"""
    current = error
    for _ in range(5):  # 最大深度以防无限循环
        code = getattr(current, "status_code", None)
        if isinstance(code, int):
            return code
        # 部分 SDK 使用 .status 而非 .status_code
        code = getattr(current, "status", None)
        if isinstance(code, int) and 100 <= code < 600:
            return code
        # 遍历原因链
        cause = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        if cause is None or cause is current:
            break
        current = cause
    return None


def _extract_error_body(error: Exception) -> dict:
    """从 SDK 异常中提取结构化错误体。"""
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        return body
    # 部分错误具有 .response.json()
    response = getattr(error, "response", None)
    if response is not None:
        try:
            json_body = response.json()
            if isinstance(json_body, dict):
                return json_body
        except Exception:
            pass
    return {}


def _extract_error_code(body: dict) -> str:
    """从响应体中提取错误代码字符串。"""
    if not body:
        return ""

    def _code_from_payload(payload) -> str:
        """从嵌套错误载荷 dict 中防御性地提取 code/type。"""
        if not isinstance(payload, dict):
            return ""
        payload_error = payload.get("error", {})
        if isinstance(payload_error, dict):
            nested = payload_error.get("code") or payload_error.get("type") or ""
            if isinstance(nested, str) and nested.strip() and nested.strip() != "400":
                return nested.strip()
        code = payload.get("code") or payload.get("error_code") or ""
        if isinstance(code, (str, int)):
            text = str(code).strip()
            if text and text != "400":
                return text
        return ""

    error_obj = body.get("error", {})
    if isinstance(error_obj, dict):
        code = error_obj.get("code") or error_obj.get("type") or ""
        if isinstance(code, str) and code.strip() and code.strip() != "400":
            return code.strip()

        # 部分提供商将真实 JSON 错误体作为字符串包装在 error.message 中
        # —— 从中窥视嵌套代码（如 Responses API 以此方式
        # 输出 ``invalid_encrypted_content``）。
        message = error_obj.get("message")
        if isinstance(message, str) and message.strip().startswith("{"):
            import json
            try:
                inner = json.loads(message)
            except (json.JSONDecodeError, TypeError):
                inner = None
            nested_code = _code_from_payload(inner)
            if nested_code:
                return nested_code

    # 顶层代码
    code = body.get("code") or body.get("error_code") or ""
    if isinstance(code, (str, int)):
        text = str(code).strip()
        if text and text != "400":
            return text
    return ""


def _extract_message(error: Exception, body: dict) -> str:
    """提取最具信息量的错误消息。"""
    # 首先尝试结构化 body
    if body:
        error_obj = body.get("error", {})
        if isinstance(error_obj, dict):
            msg = error_obj.get("message", "")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()[:500]
        msg = body.get("message", "")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()[:500]
    # 回退到 str(error)
    return str(error)[:500]
