#!/usr/bin/env python3
"""基于 xAI 内置 ``x_search`` Responses API 工具的 X Search 工具。

认证
----
当**任一** xAI 凭据路径可用时，该工具即被注册：

* ``~/.hermes/.env`` 或进程环境中设置了 ``XAI_API_KEY``
  （付费 xAI API key），或者
* 用户已通过 xAI Grok OAuth 登录——即 SuperGrok 订阅——
  也就是已执行 ``hermes auth add xai-oauth`` 且存储的 refresh
  token 仍然有效。

调用时的凭据优先级与
:func:`tools.xai_http.resolve_xai_http_credentials` 一致：SuperGrok OAuth
优先，其次是直接 OAuth 解析器，最后是 ``XAI_API_KEY``。该辅助函数还会在
OAuth access token 处于刷新偏差窗口内时自动刷新，因此
:func:`check_x_search_requirements` 返回 ``True`` 意味着 bearer 可获取且
非空。

防御性输出
----------
除 xAI 的原始响应外，该工具还额外暴露两个信号，以便调用方能区分带有真实
引用的答案和无来源的答案：

* ``from_date`` / ``to_date`` 会在发起 HTTP 调用之前在客户端做校验。
  格式错误（非 ``YYYY-MM-DD``）、倒序（``from_date > to_date``）以及纯
  未来区间（``from_date`` 晚于今天 UTC）会快速失败并返回清晰的错误，而
  不是白白消耗一次 API 调用。``to_date`` 在未来仍然被允许，以便调用方
  合法地请求「从昨天到明天」。
* 成功的响应会携带 ``degraded`` 和 ``degraded_reason`` 字段。
  当任何收窄过滤器（handles 或日期）生效，且 xAI 在顶层的 ``citations``
  数组或行内的 ``url_citation`` 注解中都没有返回任何引用时，
  ``degraded`` 为 ``True``。此时 ``answer`` 来自模型自身的知识，而非 X
  索引，调用方应将该结果视为无来源。

Salvage 自 PR #10786（最初由 @Jaaneek 贡献）；凭据解析经过重写以同时
兼容两种认证模式，遵循 Teknium 的设计。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from tools.registry import registry, tool_error
from tools.xai_http import hermes_xai_user_agent, resolve_xai_http_credentials

logger = logging.getLogger(__name__)

DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"
DEFAULT_X_SEARCH_MODEL = "grok-4.20-reasoning"
DEFAULT_X_SEARCH_TIMEOUT_SECONDS = 180
DEFAULT_X_SEARCH_RETRIES = 2
MAX_HANDLES = 10


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def _load_x_search_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        return load_config().get("x_search", {}) or {}
    except Exception:
        return {}


def _get_x_search_model() -> str:
    cfg = _load_x_search_config()
    return (str(cfg.get("model") or "").strip() or DEFAULT_X_SEARCH_MODEL)


def _get_x_search_timeout_seconds() -> int:
    cfg = _load_x_search_config()
    raw_value = cfg.get("timeout_seconds", DEFAULT_X_SEARCH_TIMEOUT_SECONDS)
    try:
        return max(30, int(raw_value))
    except Exception:
        return DEFAULT_X_SEARCH_TIMEOUT_SECONDS


def _get_x_search_retries() -> int:
    cfg = _load_x_search_config()
    raw_value = cfg.get("retries", DEFAULT_X_SEARCH_RETRIES)
    try:
        return max(0, int(raw_value))
    except Exception:
        return DEFAULT_X_SEARCH_RETRIES


# ---------------------------------------------------------------------------
# 凭据解析
# ---------------------------------------------------------------------------

def _resolve_xai_bearer() -> Tuple[str, str, str]:
    """返回 ``(api_key, base_url, source)``。

    ``source`` 取值为 ``"xai-oauth"`` 或 ``"xai"``，以便调用方（和测试）
    判断是哪条凭据路径胜出。当没有可用的凭据时抛出 ``RuntimeError``——
    注册时的 :func:`check_x_search_requirements` 门槛使这种情况在正常运行中
    不可达，但运行时检查的存在是为了让在注册与调用之间过期的凭据产生一个
    清晰的工具错误，而不是 401。
    """
    creds = resolve_xai_http_credentials()
    api_key = str(creds.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError(
            "No xAI credentials available. Run `hermes auth add xai-oauth` "
            "to sign in with your SuperGrok subscription, or set XAI_API_KEY."
        )
    base_url = str(creds.get("base_url") or DEFAULT_XAI_BASE_URL).strip().rstrip("/")
    source = str(creds.get("provider") or "xai")
    return api_key, base_url, source


def check_x_search_requirements() -> bool:
    """当 xAI 凭据可用且有效时返回 True。

    ``resolve_xai_http_credentials`` 会调用
    :func:`hermes_cli.auth.resolve_xai_oauth_runtime_credentials`，后者会在
    OAuth access token 即将过期时自动刷新；因此成功返回即意味着存在一个
    可用的 bearer。
    """
    try:
        creds = resolve_xai_http_credentials()
        return bool(str(creds.get("api_key") or "").strip())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _normalize_handles(handles: Optional[List[str]], field_name: str) -> List[str]:
    cleaned: List[str] = []
    for handle in handles or []:
        normalized = str(handle or "").strip().lstrip("@")
        if normalized:
            cleaned.append(normalized)
    if len(cleaned) > MAX_HANDLES:
        raise ValueError(f"{field_name} supports at most {MAX_HANDLES} handles")
    return cleaned


def _parse_iso_date(value: str, field_name: str) -> date:
    """把严格的 YYYY-MM-DD 字符串解析为 ``date``。

    xAI 会接受 ``from_date``/``to_date`` 槽位中的任意字符串，并在值格式
    错误或指向一个不可能存在帖子的时间窗口时，静默返回一个没有引用的
    答案。这种行为会白白消耗一次计费的 API 调用，并产出一个听起来很自信、
    但调用方很难与真实结果区分的空话答案。在客户端做校验可以快速失败，
    并给 agent 一个清晰可操作的错误。
    """
    raw = value.strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be YYYY-MM-DD (got {raw!r})"
        ) from exc


def _validate_date_range(from_date: str, to_date: str) -> None:
    """在 ``from_date`` / ``to_date`` 到达 xAI 之前校验它们。

    规则：
      * 任一字段如果非空，都必须能解析为 ``YYYY-MM-DD``。
      * 当两者都设置时，``from_date <= to_date``。
      * ``from_date`` 不得晚于今天 UTC——一个尚未开始的时间窗口里不可能
        存在帖子，因此该调用注定返回零引用。``to_date`` 在未来是允许的
        （调用方可以合法地设置「从昨天到明天」）。
    """
    parsed_from: Optional[date] = None
    parsed_to: Optional[date] = None
    if from_date.strip():
        parsed_from = _parse_iso_date(from_date, "from_date")
    if to_date.strip():
        parsed_to = _parse_iso_date(to_date, "to_date")
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise ValueError(
            f"from_date ({parsed_from.isoformat()}) must be on or before "
            f"to_date ({parsed_to.isoformat()})"
        )
    if parsed_from is not None:
        today_utc = datetime.now(timezone.utc).date()
        if parsed_from > today_utc:
            raise ValueError(
                f"from_date ({parsed_from.isoformat()}) is in the future; "
                f"X Search only indexes past posts (today UTC is "
                f"{today_utc.isoformat()})"
            )


def _extract_response_text(payload: Dict[str, Any]) -> str:
    output_text = str(payload.get("output_text") or "").strip()
    if output_text:
        return output_text

    parts: List[str] = []
    for item in payload.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            ctype = content.get("type")
            if ctype in {"output_text", "text"}:
                text = str(content.get("text") or "").strip()
                if text:
                    parts.append(text)
    return "\n\n".join(parts).strip()


def _extract_inline_citations(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    citations: List[Dict[str, Any]] = []
    for item in payload.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            for annotation in content.get("annotations", []) or []:
                if annotation.get("type") != "url_citation":
                    continue
                citations.append(
                    {
                        "url": annotation.get("url", ""),
                        "title": annotation.get("title", ""),
                        "start_index": annotation.get("start_index"),
                        "end_index": annotation.get("end_index"),
                    }
                )
    return citations


def _http_error_message(exc: requests.HTTPError) -> str:
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)

    try:
        payload = response.json()
    except Exception:
        payload = None

    if isinstance(payload, dict):
        code = str(payload.get("code") or "").strip()
        error = str(payload.get("error") or "").strip()
        message = error or str(payload)
        if code and code not in message:
            message = f"{code}: {message}"
        return message or str(exc)

    text = str(getattr(response, "text", "") or "").strip()
    if text:
        return text[:500]
    return str(exc)


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------

def x_search_tool(
    query: str,
    allowed_x_handles: Optional[List[str]] = None,
    excluded_x_handles: Optional[List[str]] = None,
    from_date: str = "",
    to_date: str = "",
    enable_image_understanding: bool = False,
    enable_video_understanding: bool = False,
) -> str:
    if not query or not query.strip():
        return tool_error("query is required for x_search")

    try:
        api_key, base_url, source = _resolve_xai_bearer()
    except RuntimeError as exc:
        return tool_error(str(exc))

    try:
        allowed = _normalize_handles(allowed_x_handles, "allowed_x_handles")
        excluded = _normalize_handles(excluded_x_handles, "excluded_x_handles")
        if allowed and excluded:
            return tool_error("allowed_x_handles and excluded_x_handles cannot be used together")

        try:
            _validate_date_range(from_date, to_date)
        except ValueError as exc:
            return tool_error(str(exc))

        tool_def: Dict[str, Any] = {"type": "x_search"}
        if allowed:
            tool_def["allowed_x_handles"] = allowed
        if excluded:
            tool_def["excluded_x_handles"] = excluded
        if from_date.strip():
            tool_def["from_date"] = from_date.strip()
        if to_date.strip():
            tool_def["to_date"] = to_date.strip()
        if enable_image_understanding:
            tool_def["enable_image_understanding"] = True
        if enable_video_understanding:
            tool_def["enable_video_understanding"] = True

        payload = {
            "model": _get_x_search_model(),
            "input": [
                {
                    "role": "user",
                    "content": query.strip(),
                }
            ],
            "tools": [tool_def],
            "store": False,
        }

        timeout_seconds = _get_x_search_timeout_seconds()
        max_retries = _get_x_search_retries()
        response: Optional[requests.Response] = None
        for attempt in range(max_retries + 1):
            try:
                response = requests.post(
                    f"{base_url}/responses",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "User-Agent": hermes_xai_user_agent(),
                    },
                    json=payload,
                    timeout=timeout_seconds,
                )
                response.raise_for_status()
                break
            except requests.HTTPError as e:
                status_code = getattr(getattr(e, "response", None), "status_code", None)
                if status_code is None or status_code < 500 or attempt >= max_retries:
                    raise
                logger.warning(
                    "x_search upstream failure on attempt %s/%s: %s",
                    attempt + 1,
                    max_retries + 1,
                    _http_error_message(e),
                )
                time.sleep(min(5.0, 1.5 * (attempt + 1)))
            except (requests.ReadTimeout, requests.ConnectionError) as e:
                if attempt >= max_retries:
                    raise
                logger.warning(
                    "x_search transient failure on attempt %s/%s: %s",
                    attempt + 1,
                    max_retries + 1,
                    e,
                )
                time.sleep(min(5.0, 1.5 * (attempt + 1)))

        if response is None:
            raise RuntimeError("x_search request did not return a response")

        data = response.json()

        answer = _extract_response_text(data)
        citations = list(data.get("citations") or [])
        inline_citations = _extract_inline_citations(data)

        # 降级结果检测。
        #
        # 即便其 X 索引中没有匹配调用方收窄过滤器的帖子，xAI 仍会返回 200 OK
        # 和一个合成的答案。该答案随后来自模型的训练数据，这具有误导性，因为
        # 它看起来与一个真实的、带引用的结果完全一样。当任何收窄过滤器处于
        # 激活状态且两个引用通道都为空时，把响应标记为降级，以便调用方可以
        # 决定放宽过滤器、重试，或回退到其他来源。
        active_filters: List[str] = []
        if allowed:
            active_filters.append("allowed_x_handles")
        if excluded:
            active_filters.append("excluded_x_handles")
        if from_date.strip():
            active_filters.append("from_date")
        if to_date.strip():
            active_filters.append("to_date")
        degraded = bool(active_filters) and not citations and not inline_citations
        degraded_reason = (
            f"no citations returned despite filters: {', '.join(active_filters)}"
            if degraded
            else None
        )

        return json.dumps(
            {
                "success": True,
                "provider": "xai",
                "credential_source": source,
                "tool": "x_search",
                "model": payload["model"],
                "query": query.strip(),
                "answer": answer,
                "citations": citations,
                "inline_citations": inline_citations,
                "degraded": degraded,
                "degraded_reason": degraded_reason,
            },
            ensure_ascii=False,
        )
    except requests.HTTPError as e:
        logger.error("x_search failed: %s", e, exc_info=True)
        return json.dumps(
            {
                "success": False,
                "provider": "xai",
                "tool": "x_search",
                "error": _http_error_message(e),
                "error_type": type(e).__name__,
            },
            ensure_ascii=False,
        )
    except requests.ReadTimeout as e:
        logger.error("x_search timed out: %s", e, exc_info=True)
        return json.dumps(
            {
                "success": False,
                "provider": "xai",
                "tool": "x_search",
                "error": f"xAI x_search timed out after {_get_x_search_timeout_seconds()} seconds",
                "error_type": type(e).__name__,
            },
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error("x_search failed: %s", e, exc_info=True)
        return json.dumps(
            {
                "success": False,
                "provider": "xai",
                "tool": "x_search",
                "error": str(e),
                "error_type": type(e).__name__,
            },
            ensure_ascii=False,
        )


X_SEARCH_SCHEMA = {
    "name": "x_search",
    "description": (
        "Search X (Twitter) posts, profiles, and threads using xAI's built-in "
        "X Search tool. Use this for current discussion, reactions, or claims "
        "on X rather than general web pages. Available when xAI credentials "
        "are configured (SuperGrok OAuth or XAI_API_KEY)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look up on X.",
            },
            "allowed_x_handles": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of X handles to include exclusively (max 10).",
            },
            "excluded_x_handles": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of X handles to exclude (max 10).",
            },
            "from_date": {
                "type": "string",
                "description": "Optional start date in YYYY-MM-DD format.",
            },
            "to_date": {
                "type": "string",
                "description": "Optional end date in YYYY-MM-DD format.",
            },
            "enable_image_understanding": {
                "type": "boolean",
                "description": "Whether xAI should analyze images attached to matching X posts.",
                "default": False,
            },
            "enable_video_understanding": {
                "type": "boolean",
                "description": "Whether xAI should analyze videos attached to matching X posts.",
                "default": False,
            },
        },
        "required": ["query"],
    },
}


def _handle_x_search(args, **kw):
    return x_search_tool(
        query=args.get("query", ""),
        allowed_x_handles=args.get("allowed_x_handles"),
        excluded_x_handles=args.get("excluded_x_handles"),
        from_date=args.get("from_date", ""),
        to_date=args.get("to_date", ""),
        enable_image_understanding=bool(args.get("enable_image_understanding", False)),
        enable_video_understanding=bool(args.get("enable_video_understanding", False)),
    )


registry.register(
    name="x_search",
    toolset="x_search",
    schema=X_SEARCH_SCHEMA,
    handler=_handle_x_search,
    check_fn=check_x_search_requirements,
    requires_env=["XAI_API_KEY"],
    emoji="🐦",
    max_result_size_chars=100_000,
)
