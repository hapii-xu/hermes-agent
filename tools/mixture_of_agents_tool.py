#!/usr/bin/env python3
"""
Mixture-of-Agents 工具模块

本模块实现了 Mixture-of-Agents (MoA) 方法，通过分层架构利用多个 LLM 的集体优势，
在复杂推理任务上实现最先进的性能。

基于研究论文："Mixture-of-Agents Enhances Large Language Model Capabilities"
作者：Junlin Wang 等 (arXiv:2406.04692v1)

核心特性：
- 多层 LLM 协作，增强推理能力
- 参考模型并行处理，提高效率
- 智能聚合与合成多样化响应
- 专注于需要深度推理的极难问题
- 针对编码、数学和复杂分析任务优化

可用工具：
- mixture_of_agents_tool：使用多个前沿模型处理复杂查询

架构：
1. 参考模型并行生成多样化的初始响应
2. 聚合模型将响应合成为高质量输出
3. 可使用多层进行迭代优化（未来增强）

使用的模型（通过 OpenRouter）：
- 参考模型：claude-opus-4.6、gemini-3-pro-preview、gpt-5.4-pro、deepseek-v3.2
- 聚合模型：claude-opus-4.6（用于合成的最强模型）

配置：
    要自定义 MoA 设置，修改本文件顶部的配置常量：
    - REFERENCE_MODELS：用于生成多样化初始响应的模型列表
    - AGGREGATOR_MODEL：用于合成最终响应的模型
    - REFERENCE_TEMPERATURE/AGGREGATOR_TEMPERATURE：采样温度
    - MIN_SUCCESSFUL_REFERENCES：继续处理所需的最少成功模型数

用法：
    from mixture_of_agents_tool import mixture_of_agents_tool
    import asyncio

    # 处理复杂查询
    result = await mixture_of_agents_tool(
        user_prompt="解决这个复杂的数学证明..."
    )
"""

import json
import logging
import os
import asyncio
import datetime
from typing import Dict, Any, List, Optional
from tools.openrouter_client import get_async_client as _get_openrouter_client, check_api_key as check_openrouter_api_key
from agent.auxiliary_client import extract_content_or_reasoning
from tools.debug_helpers import DebugSession
import sys

logger = logging.getLogger(__name__)

# MoA 处理配置
# 参考模型 - 并行生成多样化的初始响应。
# 保持此列表与当前顶级 OpenRouter 前沿选项一致。
REFERENCE_MODELS = [
    "anthropic/claude-opus-4.6",
    "google/gemini-2.5-pro",
    "openai/gpt-5.4-pro",
    "deepseek/deepseek-v3.2",
]

# 聚合模型 - 将参考响应合成为最终输出。
# 优先选择当前 OpenRouter 阵容中最强的合成模型。
AGGREGATOR_MODEL = "anthropic/claude-opus-4.6"

# 针对 MoA 性能优化的温度设置
REFERENCE_TEMPERATURE = 0.6  # 平衡创造性以获得多样化视角
AGGREGATOR_TEMPERATURE = 0.4  # 聚焦合成以保证一致性

# 失败处理配置
MIN_SUCCESSFUL_REFERENCES = 1  # 继续处理所需的最少成功参考模型数

# 聚合模型的系统提示词（来自研究论文）
AGGREGATOR_SYSTEM_PROMPT = """You have been provided with a set of responses from various open-source models to the latest user query. Your task is to synthesize these responses into a single, high-quality response. It is crucial to critically evaluate the information provided in these responses, recognizing that some of it may be biased or incorrect. Your response should not simply replicate the given answers but should offer a refined, accurate, and comprehensive reply to the instruction. Ensure your response is well-structured, coherent, and adheres to the highest standards of accuracy and reliability.

Responses from models:"""

_debug = DebugSession("moa_tools", env_var="MOA_TOOLS_DEBUG")


def _construct_aggregator_prompt(system_prompt: str, responses: List[str]) -> str:
    """
    构造聚合器的最终系统提示词，包含所有模型响应。

    Args:
        system_prompt (str)：聚合的基础系统提示词
        responses (List[str])：参考模型的响应列表

    Returns:
        str：包含编号响应的完整系统提示词
    """
    response_text = "\n".join([f"{i+1}. {response}" for i, response in enumerate(responses)])
    return f"{system_prompt}\n\n{response_text}"


async def _run_reference_model_safe(
    model: str,
    user_prompt: str,
    temperature: float = REFERENCE_TEMPERATURE,
    max_tokens: int = 32000,
    max_retries: int = 6
) -> tuple[str, str, bool]:
    """
    运行单个参考模型，带有重试逻辑和优雅失败处理。

    Args:
        model (str)：要使用的模型标识符
        user_prompt (str)：用户的查询
        temperature (float)：响应生成的采样温度
        max_tokens (int)：响应中的最大 token 数
        max_retries (int)：最大重试次数

    Returns:
        tuple[str, str, bool]：(模型名称, 响应内容或错误, 成功标志)
    """
    for attempt in range(max_retries):
        try:
            logger.info("Querying %s (attempt %s/%s)", model, attempt + 1, max_retries)

            # 构造 API 调用的参数
            api_params = {
                "model": model,
                "messages": [{"role": "user", "content": user_prompt}],
                "max_tokens": max_tokens,
                "extra_body": {
                    "reasoning": {
                        "enabled": True,
                        "effort": "xhigh"
                    }
                }
            }

            # GPT 模型（尤其是 gpt-4o-mini）不支持自定义 temperature 值，
            # 因此只对非 GPT 模型传入 temperature
            if not model.lower().startswith('gpt-'):
                api_params["temperature"] = temperature

            response = await _get_openrouter_client().chat.completions.create(**api_params)

            content = extract_content_or_reasoning(response)
            if not content:
                # 仅含推理的响应——交给重试循环处理
                logger.warning("%s returned empty content (attempt %s/%s), retrying", model, attempt + 1, max_retries)
                if attempt < max_retries - 1:
                    await asyncio.sleep(min(2 ** (attempt + 1), 60))
                    continue
            logger.info("%s responded (%s characters)", model, len(content))
            return model, content, True

        except Exception as e:
            error_str = str(e)
            # 重试路径的日志保持简洁；完整的 traceback 留给最终失败路径，
            # 以免长时间运行的 MoA 重试淹没日志。
            if "invalid" in error_str.lower():
                logger.warning("%s invalid request error (attempt %s): %s", model, attempt + 1, error_str)
            elif "rate" in error_str.lower() or "limit" in error_str.lower():
                logger.warning("%s rate limit error (attempt %s): %s", model, attempt + 1, error_str)
            else:
                logger.warning("%s unknown error (attempt %s): %s", model, attempt + 1, error_str)

            if attempt < max_retries - 1:
                # 针对限流的指数退避：2s、4s、8s、16s、32s、60s
                sleep_time = min(2 ** (attempt + 1), 60)
                logger.info("Retrying in %ss...", sleep_time)
                await asyncio.sleep(sleep_time)
            else:
                error_msg = f"{model} failed after {max_retries} attempts: {error_str}"
                logger.error("%s", error_msg, exc_info=True)
                return model, error_msg, False


async def _run_aggregator_model(
    system_prompt: str,
    user_prompt: str,
    temperature: float = AGGREGATOR_TEMPERATURE,
    max_tokens: int = None
) -> str:
    """
    运行聚合模型以合成最终响应。

    Args:
        system_prompt (str)：包含所有参考响应的系统提示词
        user_prompt (str)：原始用户查询
        temperature (float)：聚焦的温度，用于一致的聚合
        max_tokens (int)：最终响应中的最大 token 数

    Returns:
        str：合成后的最终响应
    """
    logger.info("Running aggregator model: %s", AGGREGATOR_MODEL)

    # 构造 API 调用的参数
    api_params = {
        "model": AGGREGATOR_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": max_tokens,
        "extra_body": {
            "reasoning": {
                "enabled": True,
                "effort": "xhigh"
            }
        }
    }

    # GPT 模型（尤其是 gpt-4o-mini）不支持自定义 temperature 值，
    # 因此只对非 GPT 模型传入 temperature
    if not AGGREGATOR_MODEL.lower().startswith('gpt-'):
        api_params["temperature"] = temperature

    response = await _get_openrouter_client().chat.completions.create(**api_params)

    content = extract_content_or_reasoning(response)

    # 内容为空（仅含推理的响应）时重试一次
    if not content:
        logger.warning("Aggregator returned empty content, retrying once")
        response = await _get_openrouter_client().chat.completions.create(**api_params)
        content = extract_content_or_reasoning(response)

    logger.info("Aggregation complete (%s characters)", len(content))
    return content


async def mixture_of_agents_tool(
    user_prompt: str,
    reference_models: Optional[List[str]] = None,
    aggregator_model: Optional[str] = None
) -> str:
    """
    使用 Mixture-of-Agents 方法处理复杂查询。

    本工具利用多个前沿语言模型协作解决需要高强度推理的极难问题。它特别适用于：
    - 复杂的数学证明和计算
    - 高级编码问题与算法设计
    - 多步骤分析推理任务
    - 需要多样化领域专业知识的问题
    - 单一模型存在局限的任务

    MoA 方法采用固定的 2 层架构：
    1. 第 1 层：多个参考模型并行生成多样化响应（temp=0.6）
    2. 第 2 层：聚合模型把最佳要素合成为最终响应（temp=0.4）

    Args:
        user_prompt (str)：要解决的复杂查询或问题
        reference_models (Optional[List[str]])：自定义的参考模型列表
        aggregator_model (Optional[str])：自定义的聚合模型

    Returns:
        str：包含 MoA 结果的 JSON 字符串，结构如下：
             {
                 "success": bool,
                 "response": str,
                 "models_used": {
                     "reference_models": List[str],
                     "aggregator_model": str
                 },
                 "processing_time": float
             }

    Raises:
        Exception：如果 MoA 处理失败或未设置 API key
    """
    start_time = datetime.datetime.now()

    debug_call_data = {
        "parameters": {
            "user_prompt": user_prompt[:200] + "..." if len(user_prompt) > 200 else user_prompt,
            "reference_models": reference_models or REFERENCE_MODELS,
            "aggregator_model": aggregator_model or AGGREGATOR_MODEL,
            "reference_temperature": REFERENCE_TEMPERATURE,
            "aggregator_temperature": AGGREGATOR_TEMPERATURE,
            "min_successful_references": MIN_SUCCESSFUL_REFERENCES
        },
        "error": None,
        "success": False,
        "reference_responses_count": 0,
        "failed_models_count": 0,
        "failed_models": [],
        "final_response_length": 0,
        "processing_time_seconds": 0,
        "models_used": {}
    }

    try:
        logger.info("Starting Mixture-of-Agents processing...")
        logger.info("Query: %s", user_prompt[:100])

        # 校验 API key 是否可用
        if not os.getenv("OPENROUTER_API_KEY"):
            raise ValueError("OPENROUTER_API_KEY environment variable not set")

        # 使用传入的模型或默认值
        ref_models = reference_models or REFERENCE_MODELS
        agg_model = aggregator_model or AGGREGATOR_MODEL

        logger.info("Using %s reference models in 2-layer MoA architecture", len(ref_models))

        # 第 1 层：由参考模型生成多样化响应（带失败处理）
        logger.info("Layer 1: Generating reference responses...")
        model_results = await asyncio.gather(*[
            _run_reference_model_safe(model, user_prompt, REFERENCE_TEMPERATURE)
            for model in ref_models
        ])

        # 分离成功和失败的响应
        successful_responses = []
        failed_models = []

        for model_name, content, success in model_results:
            if success:
                successful_responses.append(content)
            else:
                failed_models.append(model_name)

        successful_count = len(successful_responses)
        failed_count = len(failed_models)

        logger.info("Reference model results: %s successful, %s failed", successful_count, failed_count)

        if failed_models:
            logger.warning("Failed models: %s", ', '.join(failed_models))

        # 检查是否有足够的成功响应可以继续
        if successful_count < MIN_SUCCESSFUL_REFERENCES:
            raise ValueError(f"Insufficient successful reference models ({successful_count}/{len(ref_models)}). Need at least {MIN_SUCCESSFUL_REFERENCES} successful responses.")

        debug_call_data["reference_responses_count"] = successful_count
        debug_call_data["failed_models_count"] = failed_count
        debug_call_data["failed_models"] = failed_models

        # 第 2 层：用聚合模型聚合响应
        logger.info("Layer 2: Synthesizing final response...")
        aggregator_system_prompt = _construct_aggregator_prompt(
            AGGREGATOR_SYSTEM_PROMPT,
            successful_responses
        )

        final_response = await _run_aggregator_model(
            aggregator_system_prompt,
            user_prompt,
            AGGREGATOR_TEMPERATURE
        )

        # 计算处理耗时
        end_time = datetime.datetime.now()
        processing_time = (end_time - start_time).total_seconds()

        logger.info("MoA processing completed in %.2f seconds", processing_time)

        # 准备成功响应（仅最终聚合结果，字段精简）
        result = {
            "success": True,
            "response": final_response,
            "models_used": {
                "reference_models": ref_models,
                "aggregator_model": agg_model
            }
        }

        debug_call_data["success"] = True
        debug_call_data["final_response_length"] = len(final_response)
        debug_call_data["processing_time_seconds"] = processing_time
        debug_call_data["models_used"] = result["models_used"]

        # 记录调试信息
        _debug.log_call("mixture_of_agents_tool", debug_call_data)
        _debug.save()

        return json.dumps(result, indent=2, ensure_ascii=False)

    except Exception as e:
        error_msg = f"Error in MoA processing: {str(e)}"
        logger.error("%s", error_msg, exc_info=True)

        # 即使出错也计算处理耗时
        end_time = datetime.datetime.now()
        processing_time = (end_time - start_time).total_seconds()

        # 准备错误响应（字段精简）
        result = {
            "success": False,
            "response": "MoA processing failed. Please try again or use a single model for this query.",
            "models_used": {
                "reference_models": reference_models or REFERENCE_MODELS,
                "aggregator_model": aggregator_model or AGGREGATOR_MODEL
            },
            "error": error_msg
        }

        debug_call_data["error"] = error_msg
        debug_call_data["processing_time_seconds"] = processing_time
        _debug.log_call("mixture_of_agents_tool", debug_call_data)
        _debug.save()

        return json.dumps(result, indent=2, ensure_ascii=False)


def check_moa_requirements() -> bool:
    """
    检查 MoA 工具的所有依赖是否满足。

    Returns:
        bool：依赖满足返回 True，否则返回 False
    """
    return check_openrouter_api_key()



def get_moa_configuration() -> Dict[str, Any]:
    """
    获取当前的 MoA 配置设置。

    Returns:
        Dict[str, Any]：包含所有配置参数的字典
    """
    return {
        "reference_models": REFERENCE_MODELS,
        "aggregator_model": AGGREGATOR_MODEL,
        "reference_temperature": REFERENCE_TEMPERATURE,
        "aggregator_temperature": AGGREGATOR_TEMPERATURE,
        "min_successful_references": MIN_SUCCESSFUL_REFERENCES,
        "total_reference_models": len(REFERENCE_MODELS),
        "failure_tolerance": f"{len(REFERENCE_MODELS) - MIN_SUCCESSFUL_REFERENCES}/{len(REFERENCE_MODELS)} models can fail"
    }


if __name__ == "__main__":
    """
    直接运行时的简单测试/演示
    """
    print("🤖 Mixture-of-Agents Tool Module")
    print("=" * 50)

    # 检查 API key 是否可用
    api_available = check_openrouter_api_key()

    if not api_available:
        print("❌ OPENROUTER_API_KEY environment variable not set")
        print("Please set your API key: export OPENROUTER_API_KEY='your-key-here'")
        print("Get API key at: https://openrouter.ai/")
        sys.exit(1)
    else:
        print("✅ OpenRouter API key found")

    print("🛠️  MoA tools ready for use!")

    # 显示当前配置
    config = get_moa_configuration()
    print("\n⚙️  Current Configuration:")
    print(f"  🤖 Reference models ({len(config['reference_models'])}): {', '.join(config['reference_models'])}")
    print(f"  🧠 Aggregator model: {config['aggregator_model']}")
    print(f"  🌡️  Reference temperature: {config['reference_temperature']}")
    print(f"  🌡️  Aggregator temperature: {config['aggregator_temperature']}")
    print(f"  🛡️  Failure tolerance: {config['failure_tolerance']}")
    print(f"  📊 Minimum successful models: {config['min_successful_references']}")

    # 显示调试模式状态
    if _debug.active:
        print(f"\n🐛 Debug mode ENABLED - Session ID: {_debug.session_id}")
        print(f"   Debug logs will be saved to: ./logs/moa_tools_debug_{_debug.session_id}.json")
    else:
        print("\n🐛 Debug mode disabled (set MOA_TOOLS_DEBUG=true to enable)")

    print("\nBasic usage:")
    print("  from mixture_of_agents_tool import mixture_of_agents_tool")
    print("  import asyncio")
    print("")
    print("  async def main():")
    print("      result = await mixture_of_agents_tool(")
    print("          user_prompt='Solve this complex mathematical proof...'")
    print("      )")
    print("      print(result)")
    print("  asyncio.run(main())")

    print("\nBest use cases:")
    print("  - Complex mathematical proofs and calculations")
    print("  - Advanced coding problems and algorithm design")
    print("  - Multi-step analytical reasoning tasks")
    print("  - Problems requiring diverse domain expertise")
    print("  - Tasks where single models show limitations")

    print("\nPerformance characteristics:")
    print("  - Higher latency due to multiple model calls")
    print("  - Significantly improved quality for complex tasks")
    print("  - Parallel processing for efficiency")
    print(f"  - Optimized temperatures: {REFERENCE_TEMPERATURE} for reference models, {AGGREGATOR_TEMPERATURE} for aggregation")
    print("  - Token-efficient: only returns final aggregated response")
    print("  - Resilient: continues with partial model failures")
    print("  - Configurable: easy to modify models and settings at top of file")
    print("  - State-of-the-art results on challenging benchmarks")

    print("\nDebug mode:")
    print("  # Enable debug logging")
    print("  export MOA_TOOLS_DEBUG=true")
    print("  # Debug logs capture all MoA processing steps and metrics")
    print("  # Logs saved to: ./logs/moa_tools_debug_UUID.json")


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
from tools.registry import registry

MOA_SCHEMA = {
    "name": "mixture_of_agents",
    "description": "Route a hard problem through multiple frontier LLMs collaboratively. Makes 5 API calls (4 reference models + 1 aggregator) with maximum reasoning effort — use sparingly for genuinely difficult problems. Best for: complex math, advanced algorithms, multi-step analytical reasoning, problems benefiting from diverse perspectives.",
    "parameters": {
        "type": "object",
        "properties": {
            "user_prompt": {
                "type": "string",
                "description": "The complex query or problem to solve using multiple AI models. Should be a challenging problem that benefits from diverse perspectives and collaborative reasoning."
            }
        },
        "required": ["user_prompt"]
    }
}

registry.register(
    name="mixture_of_agents",
    toolset="moa",
    schema=MOA_SCHEMA,
    handler=lambda args, **kw: mixture_of_agents_tool(user_prompt=args.get("user_prompt", "")),
    check_fn=check_moa_requirements,
    requires_env=["OPENROUTER_API_KEY"],
    is_async=True,
    emoji="🧠",
)
