#!/usr/bin/env python3
"""
独立 Web 工具模块

本模块提供与多种后端提供方协同工作的通用 Web 工具。
后端在 ``hermes tools`` 设置期间选择（config.yaml 中的 web.backend）。
可用时，Hermes 可以为 Nous 订阅者通过 Nous 托管的 tool-gateway 路由 Firecrawl 调用。

可用工具：
- web_search_tool：搜索网络以获取信息
- web_extract_tool：从特定网页提取内容

后端兼容性：
- Exa：https://exa.ai（搜索、提取）
- Firecrawl：https://docs.firecrawl.dev/introduction（搜索、提取；直连，或针对 Nous 订阅者派生自 firecrawl-gateway.<domain>）
- Parallel：https://docs.parallel.ai（搜索、提取）
- Tavily：https://tavily.com（搜索、提取）

LLM 处理：
- 使用 OpenRouter API 调用 Gemini 3 Flash Preview 进行智能内容提取
- 提取关键摘录并生成 markdown 摘要以减少 token 用量

调试模式：
- 设置 WEB_TOOLS_DEBUG=true 以启用详细日志
- 在 ./logs 目录下创建 web_tools_debug_UUID.json
- 捕获所有工具调用、结果和压缩指标

用法：
    from web_tools import web_search_tool, web_extract_tool

    # 搜索网络
    results = web_search_tool("Python machine learning libraries", limit=3)

    # 从 URL 提取内容
    content = web_extract_tool(["https://example.com"], format="markdown")
"""

import json
import logging
import os
import re
import asyncio
from typing import List, Dict, Any, Optional, TYPE_CHECKING
import httpx  # noqa: F401 — kept at module top so tests can patch tools.web_tools.httpx
# 在 web-provider 插件迁移（PR #25182）之后，Firecrawl SDK
# 代理、客户端构造和响应结构归一化器都位于
# plugins.web.firecrawl.provider 中。我们重新导出外部代码、集成测试
# 和单元测试补丁所引用的名称，以保持公共接口稳定。
if TYPE_CHECKING:
    from firecrawl import Firecrawl  # noqa: F401 — type hints only
from plugins.web.firecrawl.provider import (
    Firecrawl,  # noqa: F401  # re-exported for tests that mock.patch("tools.web_tools.Firecrawl")
    _firecrawl_backend_help_suffix,
    _get_firecrawl_client,  # noqa: F401  # re-exported for tests that `from tools.web_tools import _get_firecrawl_client`
    _get_firecrawl_gateway_url,
    _is_tool_gateway_ready,
    check_firecrawl_api_key,
)
# 重新导出 Tavily 辅助函数，以向后兼容现有单元测试
# （tests/tools/test_web_tools_tavily.py 直接导入这些名称）。
from plugins.web.tavily.provider import (  # noqa: F401 — backward-compat names
    _normalize_tavily_documents,
    _normalize_tavily_search_results,
    _tavily_request,
)
# 重新导出 Parallel + Exa 客户端，以向后兼容现有单元测试
# （tests/tools/test_web_tools_config.py 直接导入 _get_parallel_client
# / _get_async_parallel_client / _get_exa_client）。
from plugins.web.parallel.provider import (  # noqa: F401 — backward-compat names
    _get_async_parallel_client,
    _get_parallel_client,
)
from plugins.web.exa.provider import _get_exa_client  # noqa: F401

# 各厂商客户端的模块级缓存槽位。插件通过 tools.web_tools
# 读写这些槽位，这样在不同用例之间重置
# ``tools.web_tools._<vendor>_client = None`` 的单元测试仍能正常工作。
_firecrawl_client: Optional[Any] = None
_firecrawl_client_config: Optional[Any] = None
_parallel_client: Optional[Any] = None
_async_parallel_client: Optional[Any] = None
_exa_client: Optional[Any] = None

from agent.auxiliary_client import (
    async_call_llm,
    extract_content_or_reasoning,
    get_async_text_auxiliary_client,
)
from tools.debug_helpers import DebugSession
# 仅为单元测试能在 tools.web_tools 上 monkeypatch 这些名称而导入
# （firecrawl 插件通过自身的导入链读取它们）。
from tools.managed_tool_gateway import (  # noqa: F401 — backward-compat names for tests
    build_vendor_gateway_url,
    peek_nous_access_token as _peek_nous_access_token,
    read_nous_access_token as _read_nous_access_token,
    resolve_managed_tool_gateway,
)
from tools.tool_backend_helpers import (  # noqa: F401
    managed_nous_tools_enabled,
    nous_tool_gateway_unavailable_message,
    prefers_gateway,
)
from tools.url_safety import async_is_safe_url, normalize_url_for_request
import sys

logger = logging.getLogger(__name__)


# ─── 后端选择 ────────────────────────────────────────────────────────────────

def _env_value(name: str) -> str:
    """通过 Hermes 配置感知的 env 解析 ``name``，回退到进程环境变量。

    镜像 SearXNG 提供方的 ``_searxng_url()``，这样通过 Hermes 的
    config/.env 层（``hermes config set``、``hermes tools``）设置的值
    在这里也会被采纳——而不仅仅是原始的进程环境变量导出。否则，
    一个仅在 config 中设置的 ``SEARXNG_URL``（或任何提供方密钥）会
    脱离后端自动检测级联，并使 ``check_web_api_key()`` 对其无感知。
    见 #34290。
    """
    try:
        from hermes_cli.config import get_env_value

        val = get_env_value(name)
    except Exception:
        val = None
    if val is None:
        val = os.getenv(name, "")
    return (val or "").strip()


def _has_env(name: str) -> bool:
    return bool(_env_value(name))

def _load_web_config() -> dict:
    """从 ~/.hermes/config.yaml 加载 ``web:`` 段。"""
    try:
        from hermes_cli.config import load_config
        return load_config().get("web", {})
    except (ImportError, Exception):
        return {}

def _get_backend() -> str:
    """确定使用哪个 web 后端（共享回退）。

    从 config.yaml 读取 ``web.backend``（由 ``hermes tools`` 设置）。
    对于手动配置了密钥而未运行设置的用户，回退到当前存在的
    API 密钥对应的那个后端。
    """
    configured = (_load_web_config().get("backend") or "").lower().strip()
    if configured in {"parallel", "firecrawl", "tavily", "exa", "searxng", "brave-free", "ddgs", "xai"}:
        return configured

    # 针对手动 / 旧式配置的回退——挑选优先级最高的可用后端。
    # 显式用户凭据（TAVILY_API_KEY 等）优先于 managed-tool-gateway
    # 探测，这样刻意设置的配置就不会被一个订阅等级可能并未实际授予
    # web 搜索访问权限的 Nous OAuth token 抢占（网关随后会在运行时
    # 以 "no subscription" 失败，而工具返回错误给 agent 且不会回退）。
    # 免费等级后端排在付费后端之后。
    backend_candidates = (
        ("tavily", _has_env("TAVILY_API_KEY")),
        ("exa", _has_env("EXA_API_KEY")),
        ("parallel", _has_env("PARALLEL_API_KEY")),
        ("firecrawl", _has_env("FIRECRAWL_API_KEY") or _has_env("FIRECRAWL_API_URL")),
        ("firecrawl", _is_tool_gateway_ready()),
        ("searxng", _has_env("SEARXNG_URL")),
        ("brave-free", _has_env("BRAVE_SEARCH_API_KEY")),
        ("ddgs", _ddgs_package_importable()),
    )
    for backend, available in backend_candidates:
        if available:
            return backend

    return "firecrawl"  # 默认（向后兼容）


def _get_search_backend() -> str:
    """确定 web_search 专用后端。

    选择优先级：
    1. ``web.search_backend``（按能力覆盖）
    2. ``web.backend``（共享回退——现有行为）
    3. 从环境变量自动检测

    这使得搜索与提取可以使用不同的提供方
    （例如 SearXNG 用于搜索 + Firecrawl 用于提取）。
    """
    return _get_capability_backend("search")


def _get_extract_backend() -> str:
    """确定 web_extract 专用后端。

    选择优先级：
    1. ``web.extract_backend``（按能力覆盖）
    2. ``web.backend``（共享回退——现有行为）
    3. 从环境变量自动检测
    """
    return _get_capability_backend("extract")


def _get_capability_backend(capability: str) -> str:
    """按能力选择后端的共享辅助函数。

    从 config 读取 ``web.{capability}_backend``；如果已设置且可用，
    就使用它。否则回退到共享的 ``_get_backend()``。
    """
    cfg = _load_web_config()
    specific = (cfg.get(f"{capability}_backend") or "").lower().strip()
    if specific and _is_backend_available(specific):
        return specific
    return _get_backend()


def _is_backend_available(backend: str) -> bool:
    """当选中的后端当前可用时返回 True。"""
    if backend == "exa":
        return _has_env("EXA_API_KEY")
    if backend == "parallel":
        return _has_env("PARALLEL_API_KEY")
    if backend == "firecrawl":
        return check_firecrawl_api_key()
    if backend == "tavily":
        return _has_env("TAVILY_API_KEY")
    if backend == "searxng":
        return _has_env("SEARXNG_URL")
    if backend == "brave-free":
        return _has_env("BRAVE_SEARCH_API_KEY")
    if backend == "ddgs":
        return _ddgs_package_importable()
    if backend == "xai":
        # 廉价探测——环境变量或 auth.json 中有 OAuth token。这里
        # 绝不能调用 resolve_xai_http_credentials()，因为 OAuth 路径
        # 可能触发网络 token 刷新，而 _is_backend_available 在每次
        # web_search 派发 + 每次 `hermes tools` 重绘时都会运行。
        try:
            from tools.xai_http import has_xai_credentials
            return has_xai_credentials()
        except Exception:
            return False
    return False


def _ddgs_package_importable() -> bool:
    """当 ``ddgs`` Python 包可被导入时返回 True。

    ddgs 是唯一一个可用性由包是否存在（而非环境变量 / config 条目）
    决定的后端。封装在辅助函数中，以便自动检测和
    ``_is_backend_available`` 共享同一套检查（测试也可以
    monkeypatch 单个符号）。
    """
    try:
        import ddgs  # noqa: F401
        return True
    except ImportError:
        return False

# ─── Firecrawl 客户端 ────────────────────────────────────────────────────────

# ─── Firecrawl 客户端 ────────────────────────────────────────────────────────
# PR #25182 之后，firecrawl 客户端、惰性 SDK 代理、双重认证 config
# 解析、响应归一化器以及 check_firecrawl_api_key() 都位于
# plugins.web.firecrawl.provider 中，并在本模块顶部重新导出，以便
# 外部调用者（集成测试、工具注册门控）以及对
# tools.web_tools.<name> 打补丁的单元测试继续可用。


def _web_requires_env() -> list[str]:
    """返回当前已启用 web 后端的工具元数据环境变量。

    网关环境变量总是会被上报——它们是工具注册表用来在该变量被设置时
    点亮工具的元数据字符串。仅当 ``managed_nous_tools_enabled()`` 为真
    时才对它们做门控，只省下了元数据列表中的字符串噪声，但代价是
    每次 CLI 启动（在工具注册时调用）都要对 Nous portal 做一次同步
    HTTP 刷新。其行为契约是：如果环境变量已设置，工具就能看到它；
    如果没有，就看不到。未登录的用户根本没有设置这些变量，因此
    多出来的条目是无害的。
    """
    return [
        "EXA_API_KEY",
        "PARALLEL_API_KEY",
        "TAVILY_API_KEY",
        "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL",
        "FIRECRAWL_GATEWAY_URL",
        "TOOL_GATEWAY_DOMAIN",
        "TOOL_GATEWAY_SCHEME",
        "TOOL_GATEWAY_USER_TOKEN",
    ]


# ─── Parallel / Tavily / Firecrawl 辅助函数 —— 已移入插件 ──────────────────────
# PR #25182 之后，各厂商的客户端构造、请求辅助函数和响应归一化器
# 都位于 plugins.web.<vendor>.provider 中：
#   - parallel: plugins/web/parallel/provider.py
#   - tavily:   plugins/web/tavily/provider.py
#   - firecrawl: plugins/web/firecrawl/provider.py
# 来自 firecrawl 插件的名称（Firecrawl 代理、_get_firecrawl_client、
# _to_plain_object、_normalize_result_list、_extract_web_search_results、
# _extract_scrape_payload、_is_tool_gateway_ready 等）在本模块顶部
# 重新导出，以向后兼容集成测试和单元测试补丁。


DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION = 5000

def _is_nous_auxiliary_client(client: Any) -> bool:
    """当解析出的辅助后端是 Nous Portal 时返回 True。"""
    from urllib.parse import urlparse

    base_url = str(getattr(client, "base_url", "") or "")
    host = (urlparse(base_url).hostname or "").lower()
    return host == "nousresearch.com" or host.endswith(".nousresearch.com")


def _resolve_web_extract_auxiliary(model: Optional[str] = None) -> tuple[Optional[Any], Optional[str], Dict[str, Any]]:
    """解析当前的 web-extract 辅助客户端、模型和额外请求体。"""
    client, default_model = get_async_text_auxiliary_client("web_extract")
    configured_model = os.getenv("AUXILIARY_WEB_EXTRACT_MODEL", "").strip()
    effective_model = model or configured_model or default_model

    extra_body: Dict[str, Any] = {}
    if client is not None and _is_nous_auxiliary_client(client):
        from agent.auxiliary_client import get_auxiliary_extra_body
        from agent.portal_tags import nous_portal_tags
        extra_body = get_auxiliary_extra_body() or {"tags": nous_portal_tags()}

    return client, effective_model, extra_body


def _get_default_summarizer_model() -> Optional[str]:
    """返回当前 web 提取摘要使用的默认模型。"""
    _, model, _ = _resolve_web_extract_auxiliary()
    return model

_debug = DebugSession("web_tools", env_var="WEB_TOOLS_DEBUG")


async def process_content_with_llm(
    content: str,
    url: str = "",
    title: str = "",
    model: Optional[str] = None,
    min_length: int = DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION
) -> Optional[str]:
    """
    使用 LLM 处理 web 内容，生成带有重点摘录的智能摘要。

    本函数通过 OpenRouter API 调用 Gemini 3 Flash Preview（或指定模型），
    智能地提取关键信息并生成 markdown 摘要，在保留全部重要信息的同时
    大幅减少 token 用量。

    对于非常大的内容（>50 万字符），采用分块处理后再综合。
    对于超大的内容（>200 万字符），完全拒绝处理。

    参数：
        content (str): 要处理的原始内容
        url (str): 来源 URL（用于上下文，可选）
        title (str): 页面标题（用于上下文，可选）
        model (str): 处理所用的模型（默认：google/gemini-3-flash-preview）
        min_length (int): 触发处理的最小内容长度（默认：5000）

    返回：
        Optional[str]: 处理后的 markdown 内容；若内容太短或处理失败则返回 None
    """
    # 大小阈值
    MAX_CONTENT_SIZE = 2_000_000  # 200 万字符——超过此值完全拒绝
    CHUNK_THRESHOLD = 500_000     # 50 万字符——超过此值使用分块处理
    CHUNK_SIZE = 100_000          # 每块 10 万字符
    MAX_OUTPUT_SIZE = 5000        # 最终输出大小的硬上限

    try:
        content_len = len(content)

        # 内容大得离谱时拒绝处理
        if content_len > MAX_CONTENT_SIZE:
            size_mb = content_len / 1_000_000
            logger.warning("Content too large (%.1fMB > 2MB limit). Refusing to process.", size_mb)
            return f"[Content too large to process: {size_mb:.1f}MB. Try a more focused source URL.]"

        # 内容太短则跳过处理
        if content_len < min_length:
            logger.debug("Content too short (%d < %d chars), skipping LLM processing", content_len, min_length)
            return None

        # 构造上下文信息
        context_info = []
        if title:
            context_info.append(f"Title: {title}")
        if url:
            context_info.append(f"Source: {url}")
        context_str = "\n".join(context_info) + "\n\n" if context_info else ""

        # 检查是否需要分块处理
        if content_len > CHUNK_THRESHOLD:
            logger.info("Content large (%d chars). Using chunked processing...", content_len)
            return await _process_large_content_chunked(
                content, context_str, model, CHUNK_SIZE, MAX_OUTPUT_SIZE
            )

        # 普通内容的标准单趟处理
        logger.info("Processing content with LLM (%d characters)", content_len)

        processed_content = await _call_summarizer_llm(content, context_str, model)

        if processed_content:
            # 强制输出上限
            if len(processed_content) > MAX_OUTPUT_SIZE:
                processed_content = processed_content[:MAX_OUTPUT_SIZE] + "\n\n[... summary truncated for context management ...]"

            # 记录压缩指标
            processed_length = len(processed_content)
            compression_ratio = processed_length / content_len if content_len > 0 else 1.0
            logger.info("Content processed: %d -> %d chars (%.1f%%)", content_len, processed_length, compression_ratio * 100)

        return processed_content

    except Exception as e:
        logger.warning(
            "web_extract LLM summarization failed (%s). "
            "Tip: increase auxiliary.web_extract.timeout in config.yaml "
            "or switch to a faster auxiliary model.",
            str(e)[:120],
        )
        # 回退到截断的原始内容，而不是返回一个无用的错误消息。
        # 对模型而言，前约 5000 字符几乎总是比 "[Failed to process content: ...]"
        # 更有用。
        truncated = content[:MAX_OUTPUT_SIZE]
        if len(content) > MAX_OUTPUT_SIZE:
            truncated += (
                f"\n\n[Content truncated — showing first {MAX_OUTPUT_SIZE:,} of "
                f"{len(content):,} chars. LLM summarization timed out. "
                f"To fix: increase auxiliary.web_extract.timeout in config.yaml, "
                f"or use a faster auxiliary model. Use browser_navigate for the full page.]"
            )
        return truncated


async def _call_summarizer_llm(
    content: str,
    context_str: str,
    model: Optional[str],
    max_tokens: int = 20000,
    is_chunk: bool = False,
    chunk_info: str = ""
) -> Optional[str]:
    """
    发起单次 LLM 调用来摘要内容。

    参数：
        content: 要摘要的内容
        context_str: 上下文信息（标题、URL）
        model: 要使用的模型
        max_tokens: 最大输出 token 数
        is_chunk: 这是否是更大文档中的一个分块
        chunk_info: 关于分块位置的信息（例如 "Chunk 2/5"）

    返回：
        摘要后的内容，失败时返回 None
    """
    if is_chunk:
        # 针对分块的提示词——意识到这是部分内容
        system_prompt = """You are an expert content analyst processing a SECTION of a larger document. Your job is to extract and summarize the key information from THIS SECTION ONLY.

Important guidelines for chunk processing:
1. Do NOT write introductions or conclusions - this is a partial document
2. Focus on extracting ALL key facts, figures, data points, and insights from this section
3. Preserve important quotes, code snippets, and specific details verbatim
4. Use bullet points and structured formatting for easy synthesis later
5. Note any references to other sections (e.g., "as mentioned earlier", "see below") without trying to resolve them

Your output will be combined with summaries of other sections, so focus on thorough extraction rather than narrative flow."""

        user_prompt = f"""Extract key information from this SECTION of a larger document:

{context_str}{chunk_info}

SECTION CONTENT:
{content}

Extract all important information from this section in a structured format. Focus on facts, data, insights, and key details. Do not add introductions or conclusions."""

    else:
        # 标准的整文档提示词
        system_prompt = """You are an expert content analyst. Your job is to process web content and create a comprehensive yet concise summary that preserves all important information while dramatically reducing bulk.

Create a well-structured markdown summary that includes:
1. Key excerpts (quotes, code snippets, important facts) in their original format
2. Comprehensive summary of all other important information
3. Proper markdown formatting with headers, bullets, and emphasis

Your goal is to preserve ALL important information while reducing length. Never lose key facts, figures, insights, or actionable information. Make it scannable and well-organized."""

        user_prompt = f"""Please process this web content and create a comprehensive markdown summary:

{context_str}CONTENT TO PROCESS:
{content}

Create a markdown summary that captures all key information in a well-organized, scannable format. Include important quotes and code snippets in their original formatting. Focus on actionable information, specific details, and unique insights."""

    # 带重试逻辑地调用 LLM——重试次数保持较低，因为摘要只是锦上添花；
    # 调用方在失败时会回退到截断内容。
    max_retries = 2
    retry_delay = 2
    last_error = None

    for attempt in range(max_retries):
        try:
            aux_client, effective_model, extra_body = _resolve_web_extract_auxiliary(model)
            if aux_client is None or not effective_model:
                logger.warning("No auxiliary model available for web content processing")
                return None
            call_kwargs = {
                "task": "web_extract",
                "model": effective_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
                "max_tokens": max_tokens,
                # 无显式超时——async_call_llm 从 config.yaml 读取 auxiliary.web_extract.timeout。
                # 全新配置默认 360s；若该键缺失，运行时默认为 30s
                # （agent/auxiliary_client.py 中的 _DEFAULT_AUX_TIMEOUT）。使用慢速本地
                # 模型的用户应在 config.yaml 中设置或增大 auxiliary.web_extract.timeout。
            }
            if extra_body:
                call_kwargs["extra_body"] = extra_body
            response = await async_call_llm(**call_kwargs)
            content = extract_content_or_reasoning(response)
            if content:
                return content
            # 仅有推理 / 空响应——交给重试循环处理
            logger.warning("LLM returned empty content (attempt %d/%d), retrying", attempt + 1, max_retries)
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
                continue
            return content  # 耗尽重试后返回拿到的任何内容
        except RuntimeError:
            logger.warning("No auxiliary model available for web content processing")
            return None
        except Exception as api_error:
            last_error = api_error
            if attempt < max_retries - 1:
                logger.warning("LLM API call failed (attempt %d/%d): %s", attempt + 1, max_retries, str(api_error)[:100])
                logger.warning("Retrying in %ds...", retry_delay)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
            else:
                raise last_error

    return None


async def _process_large_content_chunked(
    content: str,
    context_str: str,
    model: Optional[str],
    chunk_size: int,
    max_output_size: int
) -> Optional[str]:
    """
    通过分块来处理大内容：并行摘要每个分块，再综合各摘要。

    参数：
        content: 要处理的大内容
        context_str: 上下文信息
        model: 要使用的模型
        chunk_size: 每块的字符大小
        max_output_size: 最终输出的最大大小

    返回：
        综合后的摘要，失败时返回 None
    """
    # 将内容切分为分块
    chunks = []
    for i in range(0, len(content), chunk_size):
        chunk = content[i:i + chunk_size]
        chunks.append(chunk)

    logger.info("Split into %d chunks of ~%d chars each", len(chunks), chunk_size)

    # 并行摘要每个分块
    async def summarize_chunk(chunk_idx: int, chunk_content: str) -> tuple[int, Optional[str]]:
        """摘要单个分块。"""
        try:
            chunk_info = f"[Processing chunk {chunk_idx + 1} of {len(chunks)}]"
            summary = await _call_summarizer_llm(
                chunk_content,
                context_str,
                model,
                max_tokens=10000,
                is_chunk=True,
                chunk_info=chunk_info
            )
            if summary:
                logger.info("Chunk %d/%d summarized: %d -> %d chars", chunk_idx + 1, len(chunks), len(chunk_content), len(summary))
            return chunk_idx, summary
        except Exception as e:
            logger.warning("Chunk %d/%d failed: %s", chunk_idx + 1, len(chunks), str(e)[:50])
            return chunk_idx, None

    # 并行运行所有分块摘要
    tasks = [summarize_chunk(i, chunk) for i, chunk in enumerate(chunks)]
    # 使用 return_exceptions=True，这样单个任务失败不会丢弃
    # 所有其他已成功摘要的分块。
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 过滤掉异常，然后按顺序收集成功的摘要
    successful_results = []
    for result_item in results:
        if isinstance(result_item, BaseException):
            logger.warning("Chunk summarization task failed: %s", result_item)
            continue
        successful_results.append(result_item)

    summaries = []
    for chunk_idx, summary in sorted(successful_results, key=lambda x: x[0]):
        if summary:
            summaries.append(f"## Section {chunk_idx + 1}\n{summary}")

    if not summaries:
        logger.debug("All chunk summarizations failed")
        return "[Failed to process large content: all chunk summarizations failed]"

    logger.info("Got %d/%d chunk summaries", len(summaries), len(chunks))

    # 如果只有一个分块成功，直接返回它（带上限）
    if len(summaries) == 1:
        result = summaries[0]
        if len(result) > max_output_size:
            result = result[:max_output_size] + "\n\n[... truncated ...]"
        return result

    # 将各摘要综合为最终摘要
    logger.info("Synthesizing %d summaries...", len(summaries))

    combined_summaries = "\n\n---\n\n".join(summaries)

    synthesis_prompt = f"""You have been given summaries of different sections of a large document.
Synthesize these into ONE cohesive, comprehensive summary that:
1. Removes redundancy between sections
2. Preserves all key facts, figures, and actionable information
3. Is well-organized with clear structure
4. Is under {max_output_size} characters

{context_str}SECTION SUMMARIES:
{combined_summaries}

Create a single, unified markdown summary."""

    try:
        aux_client, effective_model, extra_body = _resolve_web_extract_auxiliary(model)
        if aux_client is None or not effective_model:
            logger.warning("No auxiliary model for synthesis, concatenating summaries")
            fallback = "\n\n".join(summaries)
            if len(fallback) > max_output_size:
                fallback = fallback[:max_output_size] + "\n\n[... truncated ...]"
            return fallback

        call_kwargs = {
            "task": "web_extract",
            "model": effective_model,
            "messages": [
                {"role": "system", "content": "You synthesize multiple summaries into one cohesive, comprehensive summary. Be thorough but concise."},
                {"role": "user", "content": synthesis_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 20000,
        }
        if extra_body:
            call_kwargs["extra_body"] = extra_body
        response = await async_call_llm(**call_kwargs)
        final_summary = extract_content_or_reasoning(response)

        # 内容为空时（仅有推理的响应）重试一次
        if not final_summary:
            logger.warning("Synthesis LLM returned empty content, retrying once")
            response = await async_call_llm(**call_kwargs)
            final_summary = extract_content_or_reasoning(response)

        # 重试后仍为 None，则回退到拼接的摘要
        if not final_summary:
            logger.warning("Synthesis failed after retry — concatenating chunk summaries")
            fallback = "\n\n".join(summaries)
            if len(fallback) > max_output_size:
                fallback = fallback[:max_output_size] + "\n\n[... truncated ...]"
            return fallback

        # 强制硬上限
        if len(final_summary) > max_output_size:
            final_summary = final_summary[:max_output_size] + "\n\n[... summary truncated for context management ...]"

        original_len = len(content)
        final_len = len(final_summary)
        compression = final_len / original_len if original_len > 0 else 1.0

        logger.info("Synthesis complete: %d -> %d chars (%.2f%%)", original_len, final_len, compression * 100)
        return final_summary

    except Exception as e:
        logger.warning("Synthesis failed: %s", str(e)[:100])
        # 回退到带截断的拼接摘要
        fallback = "\n\n".join(summaries)
        if len(fallback) > max_output_size:
            fallback = fallback[:max_output_size] + "\n\n[... truncated due to synthesis failure ...]"
        return fallback


def clean_base64_images(text: str) -> str:
    """
    从文本中移除 base64 编码的图片，以减少 token 数和杂乱内容。

    本函数查找并移除多种格式的 base64 编码图片：
    - (data:image/png;base64,...)
    - (data:image/jpeg;base64,...)
    - (data:image/svg+xml;base64,...)
    - data:image/[type];base64,...（不带括号）

    参数：
        text: 要清理的文本内容

    返回：
        清理后的文本，其中的 base64 图片被替换为占位符
    """
    # 匹配被括号包裹的 base64 编码图片的正则
    # 匹配：(data:image/[type];base64,[base64-string])
    base64_with_parens_pattern = r'\(data:image/[^;]+;base64,[A-Za-z0-9+/=]+\)'
    # 匹配不带括号的 base64 编码图片的正则
    # 匹配：data:image/[type];base64,[base64-string]
    base64_pattern = r'data:image/[^;]+;base64,[A-Za-z0-9+/=]+'

    # 先替换带括号的图片
    cleaned_text = re.sub(base64_with_parens_pattern, '[BASE64_IMAGE_REMOVED]', text)

    # 再替换剩余的不带括号的图片
    cleaned_text = re.sub(base64_pattern, '[BASE64_IMAGE_REMOVED]', cleaned_text)

    return cleaned_text


# ─── Exa / Parallel 内联辅助函数 —— 已移入插件 ──────────────────────────────
# PR #25182 之后，exa 客户端 + 搜索/提取以及 parallel 客户端 +
# 搜索/提取辅助函数都位于各自的插件中：
#   - plugins/web/exa/provider.py
#   - plugins/web/parallel/provider.py
# 两个插件都通过 agent.web_search_registry 注册，本文件中的
# 分发器通过 get_active_*_provider() 来解析它们。


def _ensure_web_plugins_loaded() -> None:
    """幂等地触发插件发现，以填充 web 注册表。

    每个内置的 web 提供方（brave-free、ddgs、searxng、exa、parallel、
    tavily、firecrawl）都在插件发现期间通过 ``plugins/web/<vendor>/__init__.py``
    注册自身。工具分发可能从尚未触发发现的上下文中到达——子进程 agent
    运行、委派子进程、独立脚本、某些测试路径——若不如此，注册表为空，
    即便用户配置了 ``web.extract_backend: firecrawl`` 并设置了
    ``FIRECRAWL_API_KEY``，``get_provider('firecrawl')`` 也会返回 ``None``。
    其症状是一条有误导性的 "No web extract provider configured" 错误
    （issue #27580）。

    与 :func:`tools.browser_tool._ensure_browser_plugins_loaded` 完全一致：
    底层的发现调用是幂等的，后续调用开销很小。
    """
    try:
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
    except Exception as exc:  # noqa: BLE001
        # 用 warning 而非 debug：如果某个插件导入确实坏了，用户
        # 否则会撞上本辅助函数本想消除的那条有误导性的 "No web extract
        # provider configured" 错误，而正常日志里毫无真实原因的线索。
        logger.warning("Web plugin discovery failed (non-fatal): %s", exc)


def web_search_tool(query: str, limit: int = 5) -> str:
    """
    使用可用的搜索 API 后端在网络上搜索信息。

    本函数提供了一个通用的 web 搜索接口，可与多种后端
    （Parallel 或 Firecrawl）协同工作。

    注意：本函数仅返回搜索结果元数据（URL、标题、描述）。
    请使用 web_extract_tool 获取特定 URL 的完整内容。

    参数：
        query (str): 要查询的搜索词
        limit (int): 返回结果的最大数量（默认：5）

    返回：
        str: 包含搜索结果的 JSON 字符串，结构如下：
             {
                 "success": bool,
                 "data": {
                     "web": [
                         {
                             "title": str,
                             "url": str,
                             "description": str,
                             "position": int
                         },
                         ...
                     ]
                 }
             }

    抛出：
        Exception: 搜索失败或未设置 API 密钥时
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 5
    limit = min(max(limit, 1), 100)

    debug_call_data = {
        "parameters": {
            "query": query,
            "limit": limit
        },
        "error": None,
        "results_count": 0,
        "original_response_size": 0,
        "final_response_size": 0
    }

    try:
        from tools.interrupt import is_interrupted
        if is_interrupted():
            return tool_error("Interrupted", success=False)

        # 通过 web 搜索注册表分发。全部 7 个提供方
        # （brave-free、ddgs、searxng、exa、parallel、tavily、firecrawl）
        # 现在都作为插件存在；分发器只是一次注册表查找 + 委派。仅同步——
        # 每个提供方的 search() 都是同步的。
        _ensure_web_plugins_loaded()
        from agent.web_search_registry import (
            get_active_search_provider,
            get_provider as _wsp_get_provider,
        )

        backend = _get_search_backend()
        provider = _wsp_get_provider(backend) if backend else None
        if provider is None or not provider.supports_search():
            # 当配置的后端不是一个已注册的搜索提供方（拼写错误、
            # 未安装的插件或能力不匹配）时，回退到按可用性遍历的
            # 活跃提供方。
            provider = get_active_search_provider()

        if provider is None:
            response_data = {
                "success": False,
                "error": (
                    "No web search provider configured. "
                    "Run `hermes tools` to set one up."
                ),
            }
        else:
            logger.info(
                "Web search via %s: '%s' (limit: %d)",
                provider.name, query, limit,
            )
            response_data = provider.search(query, limit)

        debug_call_data["results_count"] = len(response_data.get("data", {}).get("web", []))
        result_json = json.dumps(response_data, indent=2, ensure_ascii=False)
        debug_call_data["final_response_size"] = len(result_json)
        _debug.log_call("web_search_tool", debug_call_data)
        _debug.save()
        return result_json

    except Exception as e:
        error_msg = f"Error searching web: {str(e)}"
        logger.debug("%s", error_msg)

        debug_call_data["error"] = error_msg
        _debug.log_call("web_search_tool", debug_call_data)
        _debug.save()

        return tool_error(error_msg)


async def web_extract_tool(
    urls: List[str],
    format: str = None,
    use_llm_processing: bool = True,
    model: Optional[str] = None,
    min_length: int = DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION
) -> str:
    """
    使用可用的提取 API 后端从特定网页提取内容。

    本函数提供了一个通用的 web 内容提取接口，可与多种后端协同工作。
    目前使用 Firecrawl。

    参数：
        urls (List[str]): 要提取内容的 URL 列表
        format (str): 期望的输出格式（"markdown" 或 "html"，可选）
        use_llm_processing (bool): 是否用 LLM 处理内容以生成摘要（默认：True）
        model (Optional[str]): LLM 处理所用的模型（默认为当前辅助后端模型）
        min_length (int): 触发 LLM 处理的最小内容长度（默认：5000）

    安全性：在抓取前会检查 URL 中是否嵌入了密钥。

    返回：
        str: 包含提取内容的 JSON 字符串。如果启用了 LLM 处理且成功，
             'content' 字段将包含处理后的 markdown 摘要而非原始内容。

    抛出：
        Exception: 提取失败或未设置 API 密钥时
    """
    # 拦截包含嵌入密钥的 URL（防止数据外泄）。
    # 先做 URL 解码，这样百分号编码的密钥（%73k- = sk-）也能被捕获。
    from agent.redact import _PREFIX_RE
    from urllib.parse import unquote
    normalized_urls: List[str] = []
    for _url in urls:
        normalized_url = normalize_url_for_request(_url)
        if (
            _PREFIX_RE.search(_url)
            or _PREFIX_RE.search(unquote(_url))
            or _PREFIX_RE.search(normalized_url)
            or _PREFIX_RE.search(unquote(normalized_url))
        ):
            return json.dumps({
                "success": False,
                "error": "Blocked: URL contains what appears to be an API key or token. "
                         "Secrets must not be sent in URLs.",
            })
        normalized_urls.append(normalized_url)

    debug_call_data = {
        "parameters": {
            "urls": normalized_urls,
            "format": format,
            "use_llm_processing": use_llm_processing,
            "model": model,
            "min_length": min_length
        },
        "error": None,
        "pages_extracted": 0,
        "pages_processed_with_llm": 0,
        "original_response_size": 0,
        "final_response_size": 0,
        "compression_metrics": [],
        "processing_applied": []
    }

    try:
        logger.info("Extracting content from %d URL(s)", len(normalized_urls))

        # ── SSRF 防护——在任何后端之前过滤掉私有 / 内部 URL ──
        safe_urls = []
        ssrf_blocked: List[Dict[str, Any]] = []
        for url in normalized_urls:
            if not await async_is_safe_url(url):
                ssrf_blocked.append({
                    "url": url, "title": "", "content": "",
                    "error": "Blocked: URL targets a private or internal network address",
                })
            else:
                safe_urls.append(url)

        # 仅将安全 URL 分发给配置的后端
        if not safe_urls:
            results = []
        else:
            backend = _get_extract_backend()

            # 全部七个提供方（brave-free、ddgs、searxng、exa、parallel、
            # tavily、firecrawl）现在都作为插件存在。分发器是一次
            # 注册表查找 + 委派。某些提供方的 extract() 是异步的
            # （parallel、firecrawl），另一些是同步的（exa、tavily）——
            # 我们检测协程函数并 await；同步函数则内联运行（策略门控、
            # SSRF 复查等位于提供方内部 firecrawl 的逐 URL 循环中）。
            _ensure_web_plugins_loaded()
            from agent.web_search_registry import (
                get_active_extract_provider,
                get_provider as _wsp_get_provider,
            )

            provider = _wsp_get_provider(backend) if backend else None
            if provider is None or not provider.supports_extract():
                # 当配置的名称已注册但不支持提取（仅搜索的提供方如
                # brave-free / ddgs / searxng）时，将其作为一个类型化的
                # "search-only" 错误暴露出来，而不是静默切换后端。当名称
                # 根本未注册（拼写错误 / 未安装的插件）时，回退到活跃
                # 提供方遍历。
                if provider is not None and not provider.supports_extract():
                    return json.dumps(
                        {
                            "success": False,
                            "error": (
                                f"{provider.display_name} is a search-only "
                                "backend and cannot extract URL content. "
                                "Set web.extract_backend to firecrawl, "
                                "tavily, exa, or parallel."
                            ),
                        },
                        ensure_ascii=False,
                    )
                provider = get_active_extract_provider()
                if provider is None:
                    return json.dumps(
                        {
                            "success": False,
                            "error": (
                                "No web extract provider configured. "
                                "Set web.extract_backend to firecrawl, "
                                "tavily, exa, or parallel."
                            ),
                        },
                        ensure_ascii=False,
                    )

            logger.info(
                "Web extract via %s: %d URL(s)", provider.name, len(safe_urls)
            )

            # 异步或同步分发：parallel + firecrawl 有异步 extract()；
            # exa + tavily 是同步的。
            import inspect
            if inspect.iscoroutinefunction(provider.extract):
                results = await provider.extract(safe_urls, format=format)
            else:
                # 在线程中运行同步 extract()，以免在网络 I/O 上阻塞事件循环。
                results = await asyncio.to_thread(
                    provider.extract, safe_urls, format=format
                )

        # 将任何 SSRF 拦截的结果合并回来
        if ssrf_blocked:
            results = ssrf_blocked + results

        response = {"results": results}

        pages_extracted = len(response.get('results', []))
        logger.info("Extracted content from %d pages", pages_extracted)

        debug_call_data["pages_extracted"] = pages_extracted
        debug_call_data["original_response_size"] = len(json.dumps(response))
        effective_model = model or _get_default_summarizer_model()
        auxiliary_available = check_auxiliary_model()

        # 如果启用，则用 LLM 处理每个结果
        if use_llm_processing and auxiliary_available:
            logger.info("Processing extracted content with LLM (parallel)...")
            debug_call_data["processing_applied"].append("llm_processing")

            # 为并行处理准备任务
            async def process_single_result(result):
                """用 LLM 处理单个结果，并返回带有指标的更新后结果。"""
                url = result.get('url', 'Unknown URL')
                title = result.get('title', '')
                raw_content = result.get('raw_content', '') or result.get('content', '')

                if not raw_content:
                    return result, None, "no_content"

                original_size = len(raw_content)

                # 用 LLM 处理内容
                processed = await process_content_with_llm(
                    raw_content, url, title, effective_model, min_length
                )

                if processed:
                    processed_size = len(processed)
                    compression_ratio = processed_size / original_size if original_size > 0 else 1.0

                    # 用处理后的内容更新结果
                    result['content'] = processed
                    result['raw_content'] = raw_content

                    metrics = {
                        "url": url,
                        "original_size": original_size,
                        "processed_size": processed_size,
                        "compression_ratio": compression_ratio,
                        "model_used": effective_model
                    }
                    return result, metrics, "processed"
                else:
                    metrics = {
                        "url": url,
                        "original_size": original_size,
                        "processed_size": original_size,
                        "compression_ratio": 1.0,
                        "model_used": None,
                        "reason": "content_too_short"
                    }
                    return result, metrics, "too_short"

            # 并行运行所有 LLM 处理
            results_list = response.get('results', [])
            tasks = [process_single_result(result) for result in results_list]
            # 使用 return_exceptions=True，这样单个任务失败不会丢弃
            # 所有其他已成功处理的结果。
            processed_results = await asyncio.gather(*tasks, return_exceptions=True)

            # 收集指标并打印结果
            for result_item in processed_results:
                if isinstance(result_item, BaseException):
                    logger.warning("Web result processing task failed: %s", result_item)
                    continue
                result, metrics, status = result_item
                url = result.get('url', 'Unknown URL')
                if status == "processed":
                    debug_call_data["compression_metrics"].append(metrics)
                    debug_call_data["pages_processed_with_llm"] += 1
                    logger.info("%s (processed)", url)
                elif status == "too_short":
                    debug_call_data["compression_metrics"].append(metrics)
                    logger.info("%s (no processing - content too short)", url)
                else:
                    logger.warning("%s (no content to process)", url)
        else:
            if use_llm_processing and not auxiliary_available:
                logger.warning("LLM processing requested but no auxiliary model available, returning raw content")
                debug_call_data["processing_applied"].append("llm_processing_unavailable")
            # 打印已提取页面的摘要以供调试（原有行为）
            for result in response.get('results', []):
                url = result.get('url', 'Unknown URL')
                content_length = len(result.get('raw_content', ''))
                logger.info("%s (%d characters)", url, content_length)

        # 将输出裁剪为每个条目的最小字段集：title、content、error
        trimmed_results = [
            {
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "content": r.get("content", ""),
                "error": r.get("error"),
                **({  "blocked_by_policy": r["blocked_by_policy"]} if "blocked_by_policy" in r else {}),
            }
            for r in response.get("results", [])
        ]
        trimmed_response = {"results": trimmed_results}

        if trimmed_response.get("results") == []:
            result_json = tool_error("Content was inaccessible or not found")

            cleaned_result = clean_base64_images(result_json)

        else:
            result_json = json.dumps(trimmed_response, indent=2, ensure_ascii=False)

            cleaned_result = clean_base64_images(result_json)

        debug_call_data["final_response_size"] = len(cleaned_result)
        debug_call_data["processing_applied"].append("base64_image_removal")

        # 记录调试信息
        _debug.log_call("web_extract_tool", debug_call_data)
        _debug.save()

        return cleaned_result

    except Exception as e:
        error_msg = f"Error extracting content: {str(e)}"
        logger.debug("%s", error_msg)

        debug_call_data["error"] = error_msg
        _debug.log_call("web_extract_tool", debug_call_data)
        _debug.save()

        return tool_error(error_msg)


# 检查 Firecrawl 凭据的便捷函数
def check_web_api_key() -> bool:
    """检查配置的 web 后端是否可用。"""
    configured = _load_web_config().get("backend", "").lower().strip()
    if configured in {"exa", "parallel", "firecrawl", "tavily", "searxng", "brave-free", "ddgs", "xai"}:
        return _is_backend_available(configured)
    return any(
        _is_backend_available(backend)
        for backend in ("exa", "parallel", "firecrawl", "tavily", "searxng", "brave-free", "ddgs", "xai")
    )


def check_auxiliary_model() -> bool:
    """检查是否有可用的辅助文本模型用于 LLM 内容处理。"""
    client, _, _ = _resolve_web_extract_auxiliary()
    return client is not None




if __name__ == "__main__":
    """
    直接运行时的简单测试/演示
    """
    print("🌐 Standalone Web Tools Module")
    print("=" * 40)

    # 检查是否有可用的 API 密钥
    web_available = check_web_api_key()
    tool_gateway_available = _is_tool_gateway_ready()
    firecrawl_key_available = bool(os.getenv("FIRECRAWL_API_KEY", "").strip())
    firecrawl_url_available = bool(os.getenv("FIRECRAWL_API_URL", "").strip())
    nous_available = check_auxiliary_model()
    default_summarizer_model = _get_default_summarizer_model()

    if web_available:
        backend = _get_backend()
        print(f"✅ Web backend: {backend}")
        if backend == "exa":
            print("   Using Exa API (https://exa.ai)")
        elif backend == "parallel":
            print("   Using Parallel API (https://parallel.ai)")
        elif backend == "tavily":
            print("   Using Tavily API (https://tavily.com)")
        elif backend == "searxng":
            print(f"   Using SearXNG (search only): {_env_value('SEARXNG_URL')}")
        elif backend == "brave-free":
            print("   Using Brave Search free tier (search only)")
        elif backend == "ddgs":
            print("   Using DuckDuckGo via ddgs package (search only)")
        elif firecrawl_url_available:
            print(f"   Using self-hosted Firecrawl: {os.getenv('FIRECRAWL_API_URL').strip().rstrip('/')}")
        elif firecrawl_key_available:
            print("   Using direct Firecrawl cloud API")
        elif tool_gateway_available:
            print(f"   Using Firecrawl tool-gateway: {_get_firecrawl_gateway_url()}")
        else:
            print("   Firecrawl backend selected but not configured")
    else:
        print("❌ No web search backend configured")
        print(
            "Set EXA_API_KEY, PARALLEL_API_KEY, TAVILY_API_KEY, FIRECRAWL_API_KEY, FIRECRAWL_API_URL"
            f"{_firecrawl_backend_help_suffix()}"
        )

    if not nous_available:
        print("❌ No auxiliary model available for LLM content processing")
        print("Set OPENROUTER_API_KEY, configure Nous Portal, or set OPENAI_BASE_URL + OPENAI_API_KEY")
        print("⚠️  Without an auxiliary model, LLM content processing will be disabled")
    else:
        print(f"✅ Auxiliary model available: {default_summarizer_model}")

    if not web_available:
        sys.exit(1)

    print("🛠️  Web tools ready for use!")

    if nous_available:
        print(f"🧠 LLM content processing available with {default_summarizer_model}")
        print(f"   Default min length for processing: {DEFAULT_MIN_LENGTH_FOR_SUMMARIZATION} chars")

    # 显示调试模式状态
    if _debug.active:
        print(f"🐛 Debug mode ENABLED - Session ID: {_debug.session_id}")
        print(f"   Debug logs will be saved to: {_debug.log_dir}/web_tools_debug_{_debug.session_id}.json")
    else:
        print("🐛 Debug mode disabled (set WEB_TOOLS_DEBUG=true to enable)")

    print("\nBasic usage:")
    print("  from web_tools import web_search_tool, web_extract_tool")
    print("  import asyncio")
    print("")
    print("  # Search (synchronous)")
    print("  results = web_search_tool('Python tutorials')")
    print("")
    print("  # Extract (asynchronous)")
    print("  async def main():")
    print("      content = await web_extract_tool(['https://example.com'])")
    print("  asyncio.run(main())")

    if nous_available:
        print("\nLLM-enhanced usage:")
        print("  # Content automatically processed for pages >5000 chars (default)")
        print("  content = await web_extract_tool(['https://python.org/about/'])")
        print("")
        print("  # Customize processing parameters")
        print("  content = await web_extract_tool(")
        print("      ['https://docs.python.org'],")
        print("      model='google/gemini-3-flash-preview',")
        print("      min_length=3000")
        print("  )")
        print("")
        print("  # Disable LLM processing")
        print("  raw_content = await web_extract_tool(['https://example.com'], use_llm_processing=False)")

    print("\nDebug mode:")
    print("  # Enable debug logging")
    print("  export WEB_TOOLS_DEBUG=true")
    print("  # Debug logs capture:")
    print("  # - All tool calls with parameters")
    print("  # - Original API responses")
    print("  # - LLM compression metrics")
    print("  # - Final processed results")
    print("  # Logs saved to: ./logs/web_tools_debug_UUID.json")

    print("\n📝 Run 'python test_web_tools_llm.py' to test LLM processing capabilities")


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
from tools.registry import registry, tool_error

WEB_SEARCH_SCHEMA = {
    "name": "web_search",
    "description": "Search the web for information. Returns up to 5 results by default with titles, URLs, and descriptions. The query is passed through to the configured backend, so operators such as site:domain, filetype:pdf, intitle:word, -term, and \"exact phrase\" may work when the backend supports them.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to look up on the web. You may include backend-supported operators such as site:example.com, filetype:pdf, intitle:word, -term, or \"exact phrase\"."
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return. Defaults to 5.",
                "minimum": 1,
                "maximum": 100,
                "default": 5
            }
        },
        "required": ["query"]
    }
}

WEB_EXTRACT_SCHEMA = {
    "name": "web_extract",
    "description": "Extract content from web page URLs. Returns page content in markdown format. Also works with PDF URLs (arxiv papers, documents, etc.) — pass the PDF link directly and it converts to markdown text. Pages under 5000 chars return full markdown; larger pages are LLM-summarized and capped at ~5000 chars per page. Pages over 2M chars are refused. If a URL fails or times out, use the browser tool to access it instead.",
    "parameters": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of URLs to extract content from (max 5 URLs per call)",
                "maxItems": 5
            }
        },
        "required": ["urls"]
    }
}

registry.register(
    name="web_search",
    toolset="web",
    schema=WEB_SEARCH_SCHEMA,
    handler=lambda args, **kw: web_search_tool(args.get("query", ""), limit=args.get("limit", 5)),
    check_fn=check_web_api_key,
    requires_env=_web_requires_env(),
    emoji="🔍",
    max_result_size_chars=100_000,
)
registry.register(
    name="web_extract",
    toolset="web",
    schema=WEB_EXTRACT_SCHEMA,
    handler=lambda args, **kw: web_extract_tool(
        args.get("urls", [])[:5] if isinstance(args.get("urls"), list) else [], "markdown"),
    check_fn=check_web_api_key,
    requires_env=_web_requires_env(),
    is_async=True,
    emoji="📄",
    max_result_size_chars=100_000,
)
