#!/usr/bin/env python3
"""
图像生成工具模块

通过 FAL.ai 提供图像生成能力。支持多个 FAL 模型，可通过 ``hermes tools`` →
Image Generation 进行选择；当前生效的模型会持久化到 ``config.yaml`` 的
``image_gen.model`` 中。

架构：
- ``FAL_MODELS`` 是受支持模型的目录，包含每个模型的元数据（尺寸风格族、
  默认值、``supports`` 白名单、放大器开关）。
- ``_build_fal_payload()`` 把 agent 统一的输入（prompt + aspect_ratio）
  翻译成模型专属的 payload，并按 ``supports`` 白名单过滤，确保模型不会
  收到被拒绝的键。
- 通过 FAL 的 Clarity Upscaler 进行放大，按模型用 ``upscale`` 开关控制——
  FLUX 2 Pro 默认开启（向后兼容），所有更快/更新的模型默认关闭，因为对它们
  而言放大要么拖慢延迟，要么带来的画质提升微乎其微。

UI 字符串中显示的价格为初始提交时的价格；我们允许其漂移，发现时再更新。
"""

import json
import logging
import os
import datetime
import threading
import uuid
from typing import Any, Dict, Optional

# fal_client 采用惰性导入——见 _load_fal_client()。如果急切导入，会让每次
# CLI 冷启动多花约 64 ms，因为 discover_builtin_tools() 在注册表遍历时会
# 无条件导入本模块，哪怕根本没用到图像生成。
#
# 测试中 monkeypatch 这个属性（例如
# ``monkeypatch.setattr(image_tool, "fal_client", fake_fal_client)``）
# 仍然有效：当该属性为任何真值时 _load_fal_client() 会短路返回，因此
# 测试安装的 mock 不会被后续的真实导入覆盖。
fal_client: Any = None


def _load_fal_client() -> Any:
    """惰性导入 fal_client，并在首次使用时重新绑定模块级全局变量。

    幂等操作。返回（此时已加载的）``fal_client`` 模块引用。
    如果全局变量已是真值则跳过导入——这样保留了通过 monkeypatch
    模块级全局变量来安装 mock 的测试模式。
    """
    global fal_client
    if fal_client is not None:
        return fal_client
    from tools.fal_common import import_fal_client
    fal_client = import_fal_client()
    return fal_client


from tools.debug_helpers import DebugSession
from tools.fal_common import (
    _ManagedFalSyncClient,
    _extract_http_status,
    _normalize_fal_queue_url_format,  # noqa: F401 — 为测试重新导出
)
from tools.managed_tool_gateway import resolve_managed_tool_gateway
from tools.tool_backend_helpers import (
    fal_key_is_configured,
    managed_nous_tools_enabled,
    nous_tool_gateway_unavailable_message,
    prefers_gateway,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FAL 模型目录
# ---------------------------------------------------------------------------
#
# 每个条目声明了如何把我们统一的输入翻译成该模型原生的 payload 形状。
# 尺寸规格分为三族：
#
#   "image_size_preset" — 预设枚举（"square_hd"、"landscape_16_9" 等），
#                          flux 系列、z-image、qwen、recraft、ideogram 使用。
#   "aspect_ratio"      — 宽高比枚举（"16:9"、"1:1" 等），nano-banana
#                          （Gemini）使用。
#   "gpt_literal"       — 字面尺寸字符串（"1024x1024" 等），gpt-image-1.5
#                          使用。
#
# ``supports`` 是允许出现在出站 payload 中的键的白名单——任何不在此集合
# 中的键在提交前都会被剥离，确保模型不会收到被拒绝的参数（每个 FAL 模型
# 拒绝未知键的方式各不相同）。
#
# ``upscale`` 控制是否在生成后串联 Clarity Upscaler 进行放大。

FAL_MODELS: Dict[str, Dict[str, Any]] = {
    "fal-ai/flux-2/klein/9b": {
        "display": "FLUX 2 Klein 9B",
        "speed": "<1s",
        "strengths": "Fast, crisp text",
        "price": "$0.006/MP",
        "size_style": "image_size_preset",
        "sizes": {
            "landscape": "landscape_16_9",
            "square": "square_hd",
            "portrait": "portrait_16_9",
        },
        "defaults": {
            "num_inference_steps": 4,
            "output_format": "png",
            "enable_safety_checker": False,
        },
        "supports": {
            "prompt", "image_size", "num_inference_steps", "seed",
            "output_format", "enable_safety_checker",
        },
        "upscale": False,
        # 图生图 / 编辑：FLUX.2 [klein] 9B 的编辑端点接收
        # `image_urls`（列表）。支持自然语言编辑、多参考图。
        "edit_endpoint": "fal-ai/flux-2/klein/9b/edit",
        "edit_supports": {
            "prompt", "image_urls", "num_inference_steps", "seed",
            "output_format", "enable_safety_checker",
        },
        "max_reference_images": 9,
    },
    "fal-ai/flux-2-pro": {
        "display": "FLUX 2 Pro",
        "speed": "~6s",
        "strengths": "Studio photorealism",
        "price": "$0.03/MP",
        "size_style": "image_size_preset",
        "sizes": {
            "landscape": "landscape_16_9",
            "square": "square_hd",
            "portrait": "portrait_16_9",
        },
        "defaults": {
            "num_inference_steps": 50,
            "guidance_scale": 4.5,
            "num_images": 1,
            "output_format": "png",
            "enable_safety_checker": False,
            "safety_tolerance": "5",
            "sync_mode": True,
        },
        "supports": {
            "prompt", "image_size", "num_inference_steps", "guidance_scale",
            "num_images", "output_format", "enable_safety_checker",
            "safety_tolerance", "sync_mode", "seed",
        },
        "upscale": True,   # 向后兼容：保持当前的默认行为。
        # 编辑端点最多接受 9 张参考图。
        "edit_endpoint": "fal-ai/flux-2-pro/edit",
        "edit_supports": {
            "prompt", "image_urls", "num_inference_steps", "guidance_scale",
            "num_images", "output_format", "enable_safety_checker",
            "safety_tolerance", "sync_mode", "seed",
        },
        "max_reference_images": 9,
    },
    "fal-ai/z-image/turbo": {
        "display": "Z-Image Turbo",
        "speed": "~2s",
        "strengths": "Bilingual EN/CN, 6B",
        "price": "$0.005/MP",
        "size_style": "image_size_preset",
        "sizes": {
            "landscape": "landscape_16_9",
            "square": "square_hd",
            "portrait": "portrait_16_9",
        },
        "defaults": {
            "num_inference_steps": 8,
            "num_images": 1,
            "output_format": "png",
            "enable_safety_checker": False,
            "enable_prompt_expansion": False,  # 避免每次请求的额外计费
        },
        "supports": {
            "prompt", "image_size", "num_inference_steps", "num_images",
            "seed", "output_format", "enable_safety_checker",
            "enable_prompt_expansion",
        },
        "upscale": False,
    },
    "fal-ai/nano-banana-pro": {
        "display": "Nano Banana Pro (Gemini 3 Pro Image)",
        "speed": "~8s",
        "strengths": "Gemini 3 Pro, reasoning depth, text rendering",
        "price": "$0.15/image (1K)",
        "size_style": "aspect_ratio",
        "sizes": {
            "landscape": "16:9",
            "square": "1:1",
            "portrait": "9:16",
        },
        "defaults": {
            "num_images": 1,
            "output_format": "png",
            "safety_tolerance": "5",
            # "1K" 是最便宜的档位；4K 会让单图价格翻倍。
            # Nous 订阅用户应保持在 1K，以便账单可预测。
            "resolution": "1K",
        },
        "supports": {
            "prompt", "aspect_ratio", "num_images", "output_format",
            "safety_tolerance", "seed", "sync_mode", "resolution",
            "enable_web_search", "limit_generations",
        },
        "upscale": False,
        # Nano Banana Pro 编辑（Gemini 3 Pro Image）：通过 `image_urls`
        # 进行自然语言编辑，最多支持 2 张参考图。
        "edit_endpoint": "fal-ai/nano-banana-pro/edit",
        "edit_supports": {
            "prompt", "image_urls", "aspect_ratio", "num_images",
            "output_format", "safety_tolerance", "seed", "sync_mode",
            "resolution", "enable_web_search", "limit_generations",
        },
        "max_reference_images": 2,
    },
    "fal-ai/gpt-image-1.5": {
        "display": "GPT Image 1.5",
        "speed": "~15s",
        "strengths": "Prompt adherence",
        "price": "$0.034/image",
        "size_style": "gpt_literal",
        "sizes": {
            "landscape": "1536x1024",
            "square": "1024x1024",
            "portrait": "1024x1536",
        },
        "defaults": {
            # 画质固定为 medium，以保持所有用户的 portal 账单可预测
            # （low 太粗糙，high 要贵 4-6 倍）。
            "quality": "medium",
            "num_images": 1,
            "output_format": "png",
        },
        "supports": {
            "prompt", "image_size", "quality", "num_images", "output_format",
            "background", "sync_mode",
        },
        "upscale": False,
        # 编辑端点：保留构图/光照的高保真编辑。
        "edit_endpoint": "fal-ai/gpt-image-1.5/edit",
        "edit_supports": {
            "prompt", "image_urls", "image_size", "quality", "num_images",
            "output_format", "sync_mode",
        },
        "max_reference_images": 16,
    },
    "fal-ai/gpt-image-2": {
        "display": "GPT Image 2",
        "speed": "~20s",
        "strengths": "SOTA text rendering + CJK, world-aware photorealism",
        "price": "$0.04–0.06/image",
        # GPT Image 2 使用 FAL 的标准预设枚举（不像 1.5 那样用字面尺寸）。
        # 我们映射到 4:3 变体——16:9 预设（1024x576）低于 GPT-Image-2 的
        # 655,360 最小像素要求，会被拒绝。4:3 能让三种宽高比都高于下限。
        "size_style": "image_size_preset",
        "sizes": {
            "landscape": "landscape_4_3",   # 1024x768
            "square": "square_hd",            # 1024x1024
            "portrait": "portrait_4_3",       # 768x1024
        },
        "defaults": {
            # 与 gpt-image-1.5 同样的画质固定：medium 让 Nous Portal
            # 账单可预测。"high" 在同等尺寸下每张图贵 3-4 倍；
            # "low" 对生产环境而言太粗糙。
            "quality": "medium",
            "num_images": 1,
            "output_format": "png",
        },
        "supports": {
            "prompt", "image_size", "quality", "num_images", "output_format",
            "sync_mode",
            # openai_api_key（BYOK）刻意省略——所有用户都走共享的
            # FAL 计费通道。
        },
        "upscale": False,
        # GPT Image 2 的编辑端点位于 FAL 上的 OpenAI 命名空间下
        # （不是 fal-ai/）。接收 `image_urls`（列表）+ 可选 mask。我们
        # 在编辑时不发送 `image_size`，让模型从输入自动推断。
        "edit_endpoint": "openai/gpt-image-2/edit",
        "edit_supports": {
            "prompt", "image_urls", "quality", "num_images", "output_format",
            "sync_mode", "mask_image_url",
        },
        "max_reference_images": 16,
    },
    "fal-ai/ideogram/v3": {
        "display": "Ideogram V3",
        "speed": "~5s",
        "strengths": "Best typography",
        "price": "$0.03-0.09/image",
        "size_style": "image_size_preset",
        "sizes": {
            "landscape": "landscape_16_9",
            "square": "square_hd",
            "portrait": "portrait_16_9",
        },
        "defaults": {
            "rendering_speed": "BALANCED",
            "expand_prompt": True,
            "style": "AUTO",
        },
        "supports": {
            "prompt", "image_size", "rendering_speed", "expand_prompt",
            "style", "seed",
        },
        "upscale": False,
        # Ideogram V3 的编辑端点接收 `image_urls`（列表）。
        "edit_endpoint": "fal-ai/ideogram/v3/edit",
        "edit_supports": {
            "prompt", "image_urls", "rendering_speed", "expand_prompt",
            "style", "seed",
        },
        "max_reference_images": 1,
    },
    "fal-ai/recraft/v4/pro/text-to-image": {
        "display": "Recraft V4 Pro",
        "speed": "~8s",
        "strengths": "Design, brand systems, production-ready",
        "price": "$0.25/image",
        "size_style": "image_size_preset",
        "sizes": {
            "landscape": "landscape_16_9",
            "square": "square_hd",
            "portrait": "portrait_16_9",
        },
        "defaults": {
            # V4 Pro 移除了 V3 必需的 `style` 枚举——现在的默认值已经
            # 负责处理审美取向。
            "enable_safety_checker": False,
        },
        "supports": {
            "prompt", "image_size", "enable_safety_checker",
            "colors", "background_color",
        },
        "upscale": False,
    },
    "fal-ai/qwen-image": {
        "display": "Qwen Image",
        "speed": "~12s",
        "strengths": "LLM-based, complex text",
        "price": "$0.02/MP",
        "size_style": "image_size_preset",
        "sizes": {
            "landscape": "landscape_16_9",
            "square": "square_hd",
            "portrait": "portrait_16_9",
        },
        "defaults": {
            "num_inference_steps": 30,
            "guidance_scale": 2.5,
            "num_images": 1,
            "output_format": "png",
            "acceleration": "regular",
        },
        "supports": {
            "prompt", "image_size", "num_inference_steps", "guidance_scale",
            "num_images", "output_format", "acceleration", "seed", "sync_mode",
        },
        "upscale": False,
        # Qwen 的编辑使用 Qwen Image 2.0 Pro 的编辑端点，它接收
        # `image_urls`（列表）+ 自然语言编辑指令。
        "edit_endpoint": "fal-ai/qwen-image-2/pro/edit",
        "edit_supports": {
            "prompt", "image_urls", "num_inference_steps", "guidance_scale",
            "num_images", "output_format", "acceleration", "seed", "sync_mode",
        },
        "max_reference_images": 3,
    },
    # Krea 2 —— Krea 的首个基础图像模型，在 fal 上作为 day-0 合作伙伴
    # 发布（2026-05-27）。与我们的直接 ``plugins/image_gen/krea`` 后端属于
    # 同一模型族；这里暴露出来，是为了让偏好通过既有 FAL key / Nous
    # Portal 订阅计费的用户使用，而不必直接向 Krea 注册。两个变体的参数
    # schema 完全相同——区别仅在模型 id、价格和推荐用途。
    "fal-ai/krea/v2/medium/text-to-image": {
        "display": "Krea 2 Medium",
        "speed": "~15-25s",
        "strengths": "Illustration, anime, painting, expressive/artistic styles",
        "price": "$0.030 (text) / $0.035 (style refs)",
        "size_style": "aspect_ratio",
        # Krea 原生支持 1:1、4:3、3:2、16:9、2.35:1、4:5、2:3、9:16——
        # 我们把 3 个抽象宽高比映射到最接近的匹配值。
        "sizes": {
            "landscape": "16:9",
            "square": "1:1",
            "portrait": "9:16",
        },
        "defaults": {
            "creativity": "medium",
        },
        "supports": {
            "prompt", "aspect_ratio", "creativity", "seed",
            "image_style_references",
        },
        "upscale": False,
    },
    "fal-ai/krea/v2/large/text-to-image": {
        "display": "Krea 2 Large",
        "speed": "~25-60s",
        "strengths": "Photorealism, raw textured looks (motion blur, grain, film)",
        "price": "$0.060 (text) / $0.065 (style refs)",
        "size_style": "aspect_ratio",
        "sizes": {
            "landscape": "16:9",
            "square": "1:1",
            "portrait": "9:16",
        },
        "defaults": {
            "creativity": "medium",
        },
        "supports": {
            "prompt", "aspect_ratio", "creativity", "seed",
            "image_style_references",
        },
        "upscale": False,
    },
}

# 默认模型是最快的合理选项。保持低价且耗时低于 1 秒。
DEFAULT_MODEL = "fal-ai/flux-2/klein/9b"

DEFAULT_ASPECT_RATIO = "landscape"
VALID_ASPECT_RATIOS = ("landscape", "square", "portrait")


# ---------------------------------------------------------------------------
# 放大器（Clarity Upscaler——与之前实现保持一致）
# ---------------------------------------------------------------------------
UPSCALER_MODEL = "fal-ai/clarity-upscaler"
UPSCALER_FACTOR = 2
UPSCALER_SAFETY_CHECKER = False
UPSCALER_DEFAULT_PROMPT = "masterpiece, best quality, highres"
UPSCALER_NEGATIVE_PROMPT = "(worst quality, low quality, normal quality:2)"
UPSCALER_CREATIVITY = 0.35
UPSCALER_RESEMBLANCE = 0.6
UPSCALER_GUIDANCE_SCALE = 4
UPSCALER_NUM_INFERENCE_STEPS = 18


_debug = DebugSession("image_tools", env_var="IMAGE_TOOLS_DEBUG")
_managed_fal_client = None
_managed_fal_client_config = None
_managed_fal_client_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 托管式 FAL 网关（Nous Subscription）
# ---------------------------------------------------------------------------
def _resolve_managed_fal_gateway():
    """当用户偏好走网关，或缺少直接的 FAL 凭证时，返回托管的 fal-queue
    网关配置。"""
    if fal_key_is_configured() and not prefers_gateway("image_gen"):
        return None
    return resolve_managed_tool_gateway("fal-queue")


def _get_managed_fal_client(managed_gateway):
    """复用托管的 FAL client，避免其内部的 httpx.Client 每次调用都泄漏。"""
    global _managed_fal_client, _managed_fal_client_config

    client_config = (
        managed_gateway.gateway_origin.rstrip("/"),
        managed_gateway.nous_user_token,
    )
    with _managed_fal_client_lock:
        if _managed_fal_client is not None and _managed_fal_client_config == client_config:
            return _managed_fal_client

        # 在旧模块上解析 fal_client——保留 monkey-patch
        # ``image_generation_tool.fal_client`` 的测试模式。
        _load_fal_client()
        _managed_fal_client = _ManagedFalSyncClient(
            fal_client,
            key=managed_gateway.nous_user_token,
            queue_run_origin=managed_gateway.gateway_origin,
        )
        _managed_fal_client_config = client_config
        return _managed_fal_client


def _submit_fal_request(model: str, arguments: Dict[str, Any]):
    """使用直接凭证或托管队列网关提交一个 FAL 请求。"""
    # 首次调用时触发惰性导入。操作幂等。
    _load_fal_client()
    request_headers = {"x-idempotency-key": str(uuid.uuid4())}
    managed_gateway = _resolve_managed_fal_gateway()
    if managed_gateway is None:
        return fal_client.submit(model, arguments=arguments, headers=request_headers)

    managed_client = _get_managed_fal_client(managed_gateway)
    try:
        return managed_client.submit(
            model,
            arguments=arguments,
            headers=request_headers,
        )
    except Exception as exc:
        # 来自托管网关的 4xx 通常意味着 portal 当前并未代理该模型
        # （白名单缺失、计费门槛等）——因此抛出一个更清晰、带可执行
        # 修复建议的消息，而不是 httpx 的原始 HTTP 错误。
        status = _extract_http_status(exc)
        if status is not None and 400 <= status < 500:
            gateway_message = ""
            if status in {401, 402, 403}:
                gateway_message = (
                    "\n\n"
                    + nous_tool_gateway_unavailable_message(
                        "managed FAL image generation",
                        force_fresh=True,
                    )
                )
            raise ValueError(
                f"Nous Subscription gateway rejected model '{model}' "
                f"(HTTP {status}). This model may not yet be enabled on "
                f"the Nous Portal's FAL proxy. Either:\n"
                f"  • Set FAL_KEY in your environment to use FAL.ai directly, or\n"
                f"  • Pick a different model via `hermes tools` → Image Generation."
                f"{gateway_message}"
            ) from exc
        raise


# ---------------------------------------------------------------------------
# 模型解析 + payload 构造
# ---------------------------------------------------------------------------
def _resolve_fal_model() -> tuple:
    """从 config.yaml（主要来源）或默认值解析当前生效的 FAL 模型。

    返回 (model_id, metadata_dict)。如果配置的模型未知，则回退到
    DEFAULT_MODEL（并记录为一条警告）。
    """
    model_id = ""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        img_cfg = cfg.get("image_gen") if isinstance(cfg, dict) else None
        if isinstance(img_cfg, dict):
            raw = img_cfg.get("model")
            if isinstance(raw, str):
                model_id = raw.strip()
    except Exception as exc:
        logger.debug("Could not load image_gen.model from config: %s", exc)

    # 环境变量逃生口（未公开文档；为测试/脚本保留向后兼容）。
    if not model_id:
        model_id = os.getenv("FAL_IMAGE_MODEL", "").strip()

    if not model_id:
        return DEFAULT_MODEL, FAL_MODELS[DEFAULT_MODEL]

    if model_id not in FAL_MODELS:
        logger.warning(
            "Unknown FAL model '%s' in config; falling back to %s",
            model_id, DEFAULT_MODEL,
        )
        return DEFAULT_MODEL, FAL_MODELS[DEFAULT_MODEL]

    return model_id, FAL_MODELS[model_id]


def _build_fal_payload(
    model_id: str,
    prompt: str,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    seed: Optional[int] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """从统一输入为 `model_id` 构造一个 FAL 请求 payload。

    把 aspect_ratio 翻译成模型原生的尺寸规格（预设枚举、宽高比枚举或
    GPT 字面字符串），合并模型默认值，应用调用方覆盖值，最后按模型的
    ``supports`` 白名单进行过滤。
    """
    meta = FAL_MODELS[model_id]
    size_style = meta["size_style"]
    sizes = meta["sizes"]

    aspect = (aspect_ratio or DEFAULT_ASPECT_RATIO).lower().strip()
    if aspect not in sizes:
        aspect = DEFAULT_ASPECT_RATIO

    payload: Dict[str, Any] = dict(meta.get("defaults", {}))
    payload["prompt"] = (prompt or "").strip()

    if size_style in {"image_size_preset", "gpt_literal"}:
        payload["image_size"] = sizes[aspect]
    elif size_style == "aspect_ratio":
        payload["aspect_ratio"] = sizes[aspect]
    else:
        raise ValueError(f"Unknown size_style: {size_style!r}")

    if seed is not None and isinstance(seed, int):
        payload["seed"] = seed

    if overrides:
        for k, v in overrides.items():
            if v is not None:
                payload[k] = v

    supports = meta["supports"]
    # ``prompt`` 是每个 FAL text-to-image 端点都必需的字段；即使模型的
    # ``supports`` 白名单中遗漏了它也保留它，这样白名单里缺失条目就不会
    # 静默剥离 prompt，进而发出一个空请求。
    return {
        k: v for k, v in payload.items()
        if k in supports or k == "prompt"
    }


def _build_fal_edit_payload(
    model_id: str,
    prompt: str,
    image_urls: list,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    seed: Optional[int] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """从统一输入构造一个 FAL *编辑* 请求 payload（图生图）。

    每个 FAL 编辑端点都接收 ``image_urls``（源/参考图像 URL 的列表）加上
    prompt。尺寸处理与 text-to-image 不同：大多数编辑端点会从输入图像自动
    推断输出尺寸，因此我们仅在编辑端点的 ``edit_supports`` 白名单接受该键
    时才发送 ``image_size`` / ``aspect_ratio``。不在 ``edit_supports`` 中
    的键在提交前都会被剥离。
    """
    meta = FAL_MODELS[model_id]
    edit_supports = meta.get("edit_supports") or set()
    size_style = meta["size_style"]
    sizes = meta["sizes"]

    aspect = (aspect_ratio or DEFAULT_ASPECT_RATIO).lower().strip()
    if aspect not in sizes:
        aspect = DEFAULT_ASPECT_RATIO

    payload: Dict[str, Any] = dict(meta.get("defaults", {}))
    payload["prompt"] = (prompt or "").strip()
    payload["image_urls"] = list(image_urls)

    # 仅当编辑端点声明接受该键时才表达输出尺寸。
    # gpt-image-2 的编辑会从输入自动推断尺寸，所以 `image_size` 刻意未
    # 出现在它的 edit_supports 白名单中。
    if size_style in {"image_size_preset", "gpt_literal"} and "image_size" in edit_supports:
        payload["image_size"] = sizes[aspect]
    elif size_style == "aspect_ratio" and "aspect_ratio" in edit_supports:
        payload["aspect_ratio"] = sizes[aspect]

    if seed is not None and isinstance(seed, int):
        payload["seed"] = seed

    if overrides:
        for k, v in overrides.items():
            if v is not None:
                payload[k] = v

    # ``prompt`` 和 ``image_urls`` 是每个 FAL 编辑端点都必需的字段；
    # 即使模型的 ``edit_supports`` 白名单中遗漏了它们也保留它们，这样白
    # 名单里缺失条目就不会静默丢弃 prompt 或源图像，进而发出一个损坏的
    # 编辑请求。
    _required = {"prompt", "image_urls"}
    return {
        k: v for k, v in payload.items()
        if k in edit_supports or k in _required
    }


# ---------------------------------------------------------------------------
# 放大器
# ---------------------------------------------------------------------------
def _upscale_image(image_url: str, original_prompt: str) -> Optional[Dict[str, Any]]:
    """使用 FAL.ai 的 Clarity Upscaler 对图像进行放大。

    返回放大后的图像 dict；失败时返回 None（调用方会回退到原始图像）。
    """
    try:
        logger.info("Upscaling image with Clarity Upscaler...")

        upscaler_arguments = {
            "image_url": image_url,
            "prompt": f"{UPSCALER_DEFAULT_PROMPT}, {original_prompt}",
            "upscale_factor": UPSCALER_FACTOR,
            "negative_prompt": UPSCALER_NEGATIVE_PROMPT,
            "creativity": UPSCALER_CREATIVITY,
            "resemblance": UPSCALER_RESEMBLANCE,
            "guidance_scale": UPSCALER_GUIDANCE_SCALE,
            "num_inference_steps": UPSCALER_NUM_INFERENCE_STEPS,
            "enable_safety_checker": UPSCALER_SAFETY_CHECKER,
        }

        handler = _submit_fal_request(UPSCALER_MODEL, arguments=upscaler_arguments)
        result = handler.get()

        if result and "image" in result:
            upscaled_image = result["image"]
            logger.info(
                "Image upscaled successfully to %sx%s",
                upscaled_image.get("width", "unknown"),
                upscaled_image.get("height", "unknown"),
            )
            return {
                "url": upscaled_image["url"],
                "width": upscaled_image.get("width", 0),
                "height": upscaled_image.get("height", 0),
                "upscaled": True,
                "upscale_factor": UPSCALER_FACTOR,
            }
        logger.error("Upscaler returned invalid response")
        return None

    except Exception as e:
        logger.error("Error upscaling image: %s", e, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# 工具入口
# ---------------------------------------------------------------------------
def _looks_like_absolute_file_path(value: str) -> bool:
    if not value or not isinstance(value, str):
        return False
    lower = value.lower()
    if lower.startswith(("http://", "https://", "data:")):
        return False
    if os.path.isabs(value):
        return True
    return len(value) >= 3 and value[1] == ":" and value[2] in {"/", "\\"}


def _active_terminal_env(task_id: str | None):
    try:
        from tools.terminal_tool import get_active_env

        return get_active_env(task_id or "default")
    except Exception as exc:  # noqa: BLE001 - 制品标注绝不能中断生成流程
        logger.debug("Could not inspect active terminal environment: %s", exc)
        return None


def _agent_cache_base_for_env(env: Any) -> str | None:
    if env is not None:
        # 面向未来的可选覆盖项：某个环境可以通过这个 callable 暴露自己
        # 的 agent 可见缓存根目录。目前还没有后端定义它——它是一个扩展
        # 钩子（hook），不是拼写错误。getattr/callable 的守卫让它成为
        # 一个安全的空操作（no-op），直到有生产者出现为止。
        explicit = getattr(env, "agent_visible_cache_base", None)
        if callable(explicit):
            try:
                value = explicit()
                if value:
                    return str(value).rstrip("/")
            except Exception as exc:  # noqa: BLE001
                logger.debug("active env agent_visible_cache_base failed: %s", exc)

        remote_home = getattr(env, "_remote_home", None)
        if remote_home:
            return f"{str(remote_home).rstrip('/')}/.hermes"

        env_name = env.__class__.__name__
        if env_name in {"DockerEnvironment", "SingularityEnvironment", "ModalEnvironment"}:
            return "/root/.hermes"

    # 如果还没有创建任何环境，只有那些具有确定性 Hermes 缓存根目录的
    # 后端才能在无副作用的情况下被翻译。SSH 仍然可以使用 shell 可见的
    # 波浪号路径；它的首次环境同步会在第一条命令运行之前上传缓存文件。
    backend = (os.getenv("TERMINAL_ENV") or "local").strip().lower()
    if backend in {"docker", "singularity", "modal"}:
        return "/root/.hermes"
    if backend == "ssh":
        return "~/.hermes"
    return None


def _agent_visible_cache_path(host_path: str, env: Any) -> str | None:
    if not _looks_like_absolute_file_path(host_path):
        return None

    cache_base = _agent_cache_base_for_env(env)
    if not cache_base:
        return None

    try:
        from tools.credential_files import map_cache_path_to_container

        return map_cache_path_to_container(host_path, container_base=cache_base)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not translate image cache path for backend: %s", exc)
    return None


def _force_artifact_sync(env: Any) -> None:
    sync_manager = getattr(env, "_sync_manager", None)
    if sync_manager is None:
        return
    try:
        sync_manager.sync(force=True)
    except Exception as exc:  # noqa: BLE001 - 保证生成成功；为运维人员记录日志
        logger.warning("Could not force-sync generated image artifact: %s", exc)


def _postprocess_image_generate_result(raw: str, task_id: str | None = None) -> str:
    """为成功的本地图像结果标注后端可见的路径。

    ``image`` 仍然是主机/网关可投递的路径。当当前活跃的终端后端使用不同
    的文件系统时，``agent_visible_image`` 给出 agent 在使用 terminal/file
    工具时可以使用的路径。
    """
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return raw

    if not isinstance(payload, dict) or not payload.get("success"):
        return raw

    image = payload.get("image")
    if not isinstance(image, str) or not _looks_like_absolute_file_path(image):
        return raw

    env = _active_terminal_env(task_id)
    agent_path = _agent_visible_cache_path(image, env)
    if not agent_path or agent_path == image:
        return raw

    if env is not None:
        _force_artifact_sync(env)

    payload.setdefault("host_image", image)
    payload.setdefault("agent_visible_image", agent_path)
    return json.dumps(payload, ensure_ascii=False)


def image_generate_tool(
    prompt: str,
    aspect_ratio: str = DEFAULT_ASPECT_RATIO,
    num_inference_steps: Optional[int] = None,
    guidance_scale: Optional[float] = None,
    num_images: Optional[int] = None,
    output_format: Optional[str] = None,
    seed: Optional[int] = None,
    image_url: Optional[str] = None,
    reference_image_urls: Optional[list] = None,
) -> str:
    """通过 FAL 根据文本 prompt 生成图像，或对源图像进行编辑。

    路由：当提供了 ``image_url``（或 ``reference_image_urls``）且配置的
    模型声明了 ``edit_endpoint`` 时，本次调用会路由到对应的图生图 / 编辑
    端点；否则就是普通的 text-to-image。

    面向 agent 的 schema 暴露了 ``prompt``、``aspect_ratio``、``image_url``
    和 ``reference_image_urls``；其余 kwargs 是给直接用 Python 调用方用的
    覆盖项，会通过 ``supports`` / ``edit_supports`` 白名单按模型过滤
    （不支持的覆盖项会被静默丢弃，这样切换模型时旧调用方也不会出错）。

    返回一个 JSON 字符串，包含 ``{"success": bool, "image": url | None,
    "modality": "text" | "image", "error": str, "error_type": str}``。
    """
    model_id, meta = _resolve_fal_model()

    # 把所有源图像（主图 + 参考图）收集到一个有序列表中。
    source_images: list = []
    if isinstance(image_url, str) and image_url.strip():
        source_images.append(image_url.strip())
    if isinstance(reference_image_urls, (list, tuple)):
        for ref in reference_image_urls:
            if isinstance(ref, str) and ref.strip():
                source_images.append(ref.strip())

    edit_endpoint = meta.get("edit_endpoint")
    use_edit = bool(source_images) and bool(edit_endpoint)
    modality = "image" if use_edit else "text"

    debug_call_data = {
        "model": model_id,
        "parameters": {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "num_inference_steps": num_inference_steps,
            "guidance_scale": guidance_scale,
            "num_images": num_images,
            "output_format": output_format,
            "seed": seed,
            "modality": modality,
            "source_images": len(source_images),
        },
        "error": None,
        "success": False,
        "images_generated": 0,
        "generation_time": 0,
    }

    start_time = datetime.datetime.now()

    try:
        if not prompt or not isinstance(prompt, str) or len(prompt.strip()) == 0:
            raise ValueError("Prompt is required and must be a non-empty string")

        if not (fal_key_is_configured() or _resolve_managed_fal_gateway()):
            raise ValueError(_build_no_backend_setup_message())

        # 如果调用方提供了源图像，但当前模型没有编辑端点，则用一个清晰、
        # 可执行的消息报错，而不是静默丢弃这些图像并生成一张无关的图片。
        if source_images and not edit_endpoint:
            raise ValueError(
                f"Model '{meta.get('display', model_id)}' ({model_id}) is not "
                f"capable of image-to-image / editing. Provide a text-only "
                f"prompt (omit image_url), or switch to an edit-capable model "
                f"via `hermes tools` → Image Generation."
            )

        aspect_lc = (aspect_ratio or DEFAULT_ASPECT_RATIO).lower().strip()
        if aspect_lc not in VALID_ASPECT_RATIOS:
            logger.warning(
                "Invalid aspect_ratio '%s', defaulting to '%s'",
                aspect_ratio, DEFAULT_ASPECT_RATIO,
            )
            aspect_lc = DEFAULT_ASPECT_RATIO

        overrides: Dict[str, Any] = {}
        if num_inference_steps is not None:
            overrides["num_inference_steps"] = num_inference_steps
        if guidance_scale is not None:
            overrides["guidance_scale"] = guidance_scale
        if num_images is not None:
            overrides["num_images"] = num_images
        if output_format is not None:
            overrides["output_format"] = output_format

        if use_edit:
            # 把参考图数量钳制到模型声明的上限。
            max_refs = int(meta.get("max_reference_images") or 1)
            clamped_sources = source_images[:max_refs] if max_refs > 0 else source_images
            arguments = _build_fal_edit_payload(
                model_id, prompt, clamped_sources, aspect_lc,
                seed=seed, overrides=overrides,
            )
            endpoint = edit_endpoint
            logger.info(
                "Editing image with %s (%s) — %d source image(s), prompt: %s",
                meta.get("display", model_id), endpoint, len(clamped_sources),
                prompt[:80],
            )
        else:
            arguments = _build_fal_payload(
                model_id, prompt, aspect_lc, seed=seed, overrides=overrides,
            )
            endpoint = model_id
            logger.info(
                "Generating image with %s (%s) — prompt: %s",
                meta.get("display", model_id), model_id, prompt[:80],
            )

        handler = _submit_fal_request(endpoint, arguments=arguments)
        result = handler.get()

        generation_time = (datetime.datetime.now() - start_time).total_seconds()

        if not result or "images" not in result:
            raise ValueError("Invalid response from FAL.ai API — no images returned")

        images = result.get("images", [])
        if not images:
            raise ValueError("No images were generated")

        # 编辑端点已经返回最终合成结果；Clarity 放大器是 text-to-image 的
        # 画质增强步骤，所以编辑场景下跳过它。
        should_upscale = bool(meta.get("upscale", False)) and not use_edit

        formatted_images = []
        for img in images:
            if not (isinstance(img, dict) and "url" in img):
                continue
            original_image = {
                "url": img["url"],
                "width": img.get("width", 0),
                "height": img.get("height", 0),
            }

            if should_upscale:
                upscaled_image = _upscale_image(img["url"], prompt.strip())
                if upscaled_image:
                    formatted_images.append(upscaled_image)
                    continue
                logger.warning("Using original image as fallback (upscale failed)")

            original_image["upscaled"] = False
            formatted_images.append(original_image)

        if not formatted_images:
            raise ValueError("No valid image URLs returned from API")

        upscaled_count = sum(1 for img in formatted_images if img.get("upscaled"))
        logger.info(
            "Generated %s image(s) in %.1fs (%s upscaled) via %s [%s]",
            len(formatted_images), generation_time, upscaled_count, endpoint,
            modality,
        )

        response_data = {
            "success": True,
            "image": formatted_images[0]["url"] if formatted_images else None,
            "modality": modality,
        }

        debug_call_data["success"] = True
        debug_call_data["images_generated"] = len(formatted_images)
        debug_call_data["generation_time"] = generation_time
        _debug.log_call("image_generate_tool", debug_call_data)
        _debug.save()

        return json.dumps(response_data, indent=2, ensure_ascii=False)

    except Exception as e:
        generation_time = (datetime.datetime.now() - start_time).total_seconds()
        error_msg = f"Error generating image: {str(e)}"
        logger.error("%s", error_msg, exc_info=True)

        response_data = {
            "success": False,
            "image": None,
            "error": str(e),
            "error_type": type(e).__name__,
        }

        debug_call_data["error"] = error_msg
        debug_call_data["generation_time"] = generation_time
        _debug.log_call("image_generate_tool", debug_call_data)
        _debug.save()

        return json.dumps(response_data, indent=2, ensure_ascii=False)


def check_fal_api_key() -> bool:
    """如果 FAL.ai 的 API key（直接凭证或托管网关）可用，则返回 True。"""
    return bool(fal_key_is_configured() or _resolve_managed_fal_gateway())


def _build_no_backend_setup_message() -> str:
    """当没有任何 FAL 后端可达时，构造一条可执行的错误字符串。

    供树内（in-tree）FAL 路径使用。内容涉及：
      - FAL_KEY 的注册链接
      - 托管网关的状态（如果 Nous 工具已启用）
      - 插件替代方案的指引（这样 ``image_gen.provider`` 处于陈旧值的用户
        也能知道注册表的存在以及如何查看它）
    """
    lines = ["Image generation is unavailable in this environment.", ""]
    lines.append("Missing requirements:")
    if managed_nous_tools_enabled():
        lines.append(
            "  - FAL_KEY is not set and the managed FAL gateway is unreachable"
        )
    else:
        lines.append("  - FAL_KEY environment variable is not set")
        gateway_message = nous_tool_gateway_unavailable_message(
            "managed FAL image generation",
        )
        if gateway_message:
            lines.append(f"  - {gateway_message}")
    lines.append("")
    lines.append("To enable image generation, do one of:")
    lines.append(
        "  1. Get a free API key at https://fal.ai and set "
        "FAL_KEY=<your-key> (then restart the session)"
    )
    if managed_nous_tools_enabled():
        lines.append(
            "  2. Sign in to a Nous account that has the managed FAL "
            "gateway enabled (`hermes setup`)"
        )
    lines.append(
        "  3. Configure a different image_gen provider via `hermes tools` "
        "→ Image Generation (run `hermes plugins list` to see installed "
        "backends)"
    )
    return "\n".join(lines)


def check_image_generation_requirements() -> bool:
    """如果任一图像生成后端可用，则返回 True。

    按以下顺序考虑各 provider：

    1. 树内 FAL 后端（FAL_KEY 或托管网关）。
    2. 任何已通过插件注册且 ``is_available()`` 返回 True 的 provider。

    只有当树内 FAL 路径未就绪时插件才会胜出，这与历史行为一致：随
    hermes 发布但已配置了 FAL key 的环境应当仍然暴露该工具。在已就绪的
    多个 provider 中，当前生效的那个由 ``image_gen.provider`` 在每次调用
    时解析。
    """
    try:
        if check_fal_api_key():
            # 在这里触发 fal_client 的惰性导入，作为 SDK 是否存在的检查。
            # 如果可选的 ``fal-client`` 包未安装，会抛出 ImportError；下面
            # 调用方的 except ImportError 会捕获它并继续进行插件探测。
            _load_fal_client()
            return True
    except ImportError:
        pass

    # 探测插件 provider。发现过程是幂等的，且开销很小。
    try:
        from agent.image_gen_registry import list_providers
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
        for provider in list_providers():
            try:
                if provider.is_available():
                    return True
            except Exception:
                continue
    except Exception:
        pass

    return False


# ---------------------------------------------------------------------------
# 演示 / CLI 入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("🎨 Image Generation Tools — FAL.ai multi-model support")
    print("=" * 60)

    if not check_fal_api_key():
        print("❌ FAL_KEY environment variable not set")
        print("   Set it via: export FAL_KEY='your-key-here'")
        print("   Get a key: https://fal.ai/")
        raise SystemExit(1)
    print("✅ FAL.ai API key found")

    try:
        import fal_client  # noqa: F401
        print("✅ fal_client library available")
    except ImportError:
        print("❌ fal_client library not found — pip install fal-client")
        raise SystemExit(1)

    model_id, meta = _resolve_fal_model()
    print(f"🤖 Active model: {meta.get('display', model_id)} ({model_id})")
    print(f"   Speed: {meta.get('speed', '?')}  ·  Price: {meta.get('price', '?')}")
    print(f"   Upscaler: {'on' if meta.get('upscale') else 'off'}")

    print("\nAvailable models:")
    for mid, m in FAL_MODELS.items():
        marker = " ← active" if mid == model_id else ""
        print(f"  {mid:<32}  {m.get('speed', '?'):<6}  {m.get('price', '?')}{marker}")

    if _debug.active:
        print(f"\n🐛 Debug mode enabled — session {_debug.session_id}")


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
from tools.registry import registry, tool_error

IMAGE_GENERATE_SCHEMA = {
    "name": "image_generate",
    # 占位符——真正的 description 会在 get_tool_definitions() 时被动态重建，
    # 以反映当前后端的真实能力（所选模型是否支持图生图 / 编辑）。参见下方
    # 的 _build_dynamic_image_schema() 以及 dynamic-tool-schemas 这个 skill。
    "description": (
        "Generate high-quality images from text prompts (text-to-image), or "
        "edit / transform an existing image (image-to-image) when the active "
        "model supports it. Pass `image_url` to edit that image; add "
        "`reference_image_urls` for style/composition references; omit both "
        "for text-to-image. The underlying backend (FAL, OpenAI, xAI, etc.) "
        "and model are user-configured and not selectable by the agent. "
        "Returns the result in the `image` field — either a URL or an absolute "
        "file path. To show it to the user, reference that path/URL in your "
        "response using the file-delivery convention for the current platform "
        "(your platform guidance describes how files are delivered here). When "
        "the active terminal backend has a different filesystem, successful "
        "local-file results may also include `agent_visible_image` for "
        "follow-up terminal/file operations."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "The text prompt describing the desired image (text-to-"
                    "image) or the edit to apply (image-to-image). Be detailed "
                    "and descriptive."
                ),
            },
            "aspect_ratio": {
                "type": "string",
                "enum": list(VALID_ASPECT_RATIOS),
                "description": "The aspect ratio of the generated image. 'landscape' is 16:9 wide, 'portrait' is 16:9 tall, 'square' is 1:1.",
                "default": DEFAULT_ASPECT_RATIO,
            },
            "image_url": {
                "type": "string",
                "description": (
                    "Optional source image to edit/transform (image-to-image). "
                    "When provided, the active backend routes to its image "
                    "editing endpoint; when omitted, it generates from text "
                    "alone. Pass a public URL or an absolute local file path "
                    "from the conversation. Only honored by models that "
                    "support editing — the description above indicates whether "
                    "the active model does."
                ),
            },
            "reference_image_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of additional reference image URLs / paths "
                    "(style, character, or composition references) to guide an "
                    "image-to-image edit. Supported only by some models and "
                    "capped per-model; the description above indicates the max."
                ),
            },
        },
        "required": ["prompt"],
    },
}


def _read_configured_image_model():
    """返回 config.yaml 中 ``image_gen.model`` 的值，没有则为 None。"""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        if isinstance(section, dict):
            value = section.get("model")
            if isinstance(value, str) and value.strip():
                return value.strip()
    except Exception as exc:
        logger.debug("Could not read image_gen.model: %s", exc)
    return None


def _read_configured_image_provider():
    """返回 config.yaml 中 ``image_gen.provider`` 的值，没有则为 None。

    只有在这个值被显式设置时我们才会去查询插件注册表——未设置时，即使
    恰好注册了其他 provider（例如某个用户为了别的功能设置了
    OPENAI_API_KEY，但从未要求使用 OpenAI 图像生成），用户也仍会停留在
    树内 FAL 这个回退路径上。``"fal"`` 会显式地经由
    ``plugins/image_gen/fal/`` 路由（该插件通过调用时的间接引用回退到本
    模块的管线中——参见 issue #26241）。
    """
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        if isinstance(section, dict):
            value = section.get("provider")
            if isinstance(value, str) and value.strip():
                return value.strip()
    except Exception as exc:
        logger.debug("Could not read image_gen.provider: %s", exc)
    return None


def _dispatch_to_plugin_provider(
    prompt: str,
    aspect_ratio: str,
    image_url: Optional[str] = None,
    reference_image_urls: Optional[list] = None,
):
    """当选中了某个插件注册的 provider 时，把调用路由给它。

    派发成功时返回一个 JSON 字符串；返回 ``None`` 则表示回退到
    ``image_generate_tool`` 中的树内 FAL 路径。

    当 ``image_gen.provider`` 被显式设置时触发派发——包括 ``"fal"`` 本身，
    它现在会解析为 ``plugins/image_gen/fal/`` 插件（该插件通过 ``_it``
    间接引用重新进入本模块的管线，所以行为与直接调用完全一致，只是经由
    注册表路由而已）。

    ``image_url`` / ``reference_image_urls`` 用于启用图生图 / 编辑功能：它们
    会被转发给 provider 的 ``generate()``，以便后端路由到其编辑端点。
    """
    configured = _read_configured_image_provider()
    if not configured:
        return None

    # 同时读取已配置的模型，以便把它传给插件
    configured_model = _read_configured_image_model()

    try:
        # 在本地导入，这样仅导入本模块就不会触发插件发现（测试依赖这一行为）。
        from agent.image_gen_registry import get_provider
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
        provider = get_provider(configured)
    except Exception as exc:
        logger.debug("image_gen plugin dispatch skipped: %s", exc)
        return None

    if provider is None:
        try:
            # 长时间运行的会话可能在某个内置后端被补丁加入之前、或配置变更
            # 之前就已经发现了插件。在抛出 provider 缺失错误之前，先强制刷新
            # 重试一次。
            _ensure_plugins_discovered(force=True)
            provider = get_provider(configured)
        except Exception as exc:
            logger.debug("image_gen plugin force-refresh skipped: %s", exc)

    if provider is None:
        return json.dumps({
            "success": False,
            "image": None,
            "error": (
                f"image_gen.provider='{configured}' is set but no plugin "
                f"registered that name. Run `hermes plugins list` to see "
                f"available image gen backends."
            ),
            "error_type": "provider_not_registered",
        })

    kwargs: Dict[str, Any] = {"prompt": prompt, "aspect_ratio": aspect_ratio}
    try:
        if configured_model:
            kwargs["model"] = configured_model
        if isinstance(image_url, str) and image_url.strip():
            kwargs["image_url"] = image_url.strip()
        norm_refs = None
        if reference_image_urls is not None:
            from agent.image_gen_provider import normalize_reference_images

            norm_refs = normalize_reference_images(reference_image_urls)
        if norm_refs:
            kwargs["reference_image_urls"] = norm_refs
        result = provider.generate(**kwargs)
    except TypeError as exc:
        # 某个 provider 的 generate() 签名早于 image_url 支持的引入（第三方
        # 插件尚未更新）——去掉这些新的 kwargs 重试，以保证 text-to-image
        # 仍然可用；但当用户确实发起的是编辑请求时，给出一条清晰的提示。
        if "image_url" in kwargs or "reference_image_urls" in kwargs:
            logger.warning(
                "image_gen provider '%s' rejected image-to-image kwargs "
                "(signature too narrow): %s",
                getattr(provider, "name", "?"), exc,
            )
            return json.dumps({
                "success": False,
                "image": None,
                "error": (
                    f"Provider '{getattr(provider, 'name', '?')}' does not "
                    f"support image-to-image / editing (its generate() "
                    f"signature is out of date with the image_generate schema). "
                    f"Omit image_url for text-to-image, or pick a backend that "
                    f"supports editing via `hermes tools` → Image Generation."
                ),
                "error_type": "modality_unsupported",
            })
        logger.warning(
            "Image gen provider '%s' raised TypeError: %s",
            getattr(provider, "name", "?"), exc,
        )
        return json.dumps({
            "success": False,
            "image": None,
            "error": f"Provider '{getattr(provider, 'name', '?')}' error: {exc}",
            "error_type": "provider_exception",
        })
    except Exception as exc:
        logger.warning(
            "Image gen provider '%s' raised: %s",
            getattr(provider, "name", "?"), exc,
        )
        return json.dumps({
            "success": False,
            "image": None,
            "error": f"Provider '{getattr(provider, 'name', '?')}' error: {exc}",
            "error_type": "provider_exception",
        })
    if not isinstance(result, dict):
        return json.dumps({
            "success": False,
            "image": None,
            "error": "Provider returned a non-dict result",
            "error_type": "provider_contract",
        })
    return json.dumps(result)


def _handle_image_generate(args, **kw):
    prompt = args.get("prompt", "")
    if not prompt:
        return tool_error("prompt is required for image generation")
    aspect_ratio = args.get("aspect_ratio", DEFAULT_ASPECT_RATIO)
    image_url = args.get("image_url")
    reference_image_urls = args.get("reference_image_urls")
    task_id = kw.get("task_id")

    # 如果当前激活了某个插件 provider（且不是树内 FAL 路径），则路由给它。
    dispatched = _dispatch_to_plugin_provider(
        prompt, aspect_ratio,
        image_url=image_url,
        reference_image_urls=reference_image_urls,
    )
    if dispatched is not None:
        return _postprocess_image_generate_result(dispatched, task_id=task_id)

    raw = image_generate_tool(
        prompt=prompt,
        aspect_ratio=aspect_ratio,
        image_url=image_url,
        reference_image_urls=reference_image_urls,
    )
    return _postprocess_image_generate_result(raw, task_id=task_id)


# ---------------------------------------------------------------------------
# 动态 schema —— 反映当前后端的图生图能力
# ---------------------------------------------------------------------------
#
# 为什么用动态：当前模型是否支持图生图 / 编辑，完全取决于用户配置的后端
# + 模型。提前告知模型（"当前模型只支持 text-to-image——image_url 会被
# 拒绝"）可以省下一个浪费的回合。在 model_tools.get_tool_definitions()
# 中按 config.yaml 的 mtime 做了 memoize，所以当用户通过 `hermes tools`
# 或 `/skills` 切换模型/provider 时它会重新构建。


_GENERIC_IMAGE_DESCRIPTION = IMAGE_GENERATE_SCHEMA["description"]


def _active_image_capabilities() -> Dict[str, Any]:
    """尽力而为：返回当前后端/模型的图像能力。

    解析顺序与运行时派发一致：
    1. 如果设置了 ``image_gen.provider``，就询问该插件 provider。
    2. 否则在树内 FAL 模型目录中查找当前模型。

    返回形如 ``{"modalities": [...], "max_reference_images": N,
    "model": "...", "provider": "..."}`` 的 dict。永不抛异常。
    """
    info: Dict[str, Any] = {"modalities": ["text"], "max_reference_images": 0}

    configured_provider = _read_configured_image_provider()
    if configured_provider and configured_provider != "fal":
        try:
            from agent.image_gen_registry import get_provider
            from hermes_cli.plugins import _ensure_plugins_discovered

            _ensure_plugins_discovered()
            provider = get_provider(configured_provider)
            if provider is not None:
                caps = {}
                try:
                    caps = provider.capabilities() or {}
                except Exception:  # noqa: BLE001
                    caps = {}
                info["provider"] = provider.display_name
                info["model"] = _read_configured_image_model() or (provider.default_model() or "")
                if caps.get("modalities"):
                    info["modalities"] = list(caps["modalities"])
                if caps.get("max_reference_images"):
                    info["max_reference_images"] = int(caps["max_reference_images"])
                return info
        except Exception:  # noqa: BLE001
            pass

    # 树内 FAL 路径（provider 未设置或 == "fal"）。
    try:
        model_id, meta = _resolve_fal_model()
        info["provider"] = "FAL.ai"
        info["model"] = meta.get("display", model_id)
        if meta.get("edit_endpoint"):
            info["modalities"] = ["text", "image"]
            info["max_reference_images"] = int(meta.get("max_reference_images") or 1)
        else:
            info["modalities"] = ["text"]
            info["max_reference_images"] = 0
    except Exception:  # noqa: BLE001
        pass

    return info


def _build_dynamic_image_schema() -> Dict[str, Any]:
    """Build a description reflecting whether the active model supports editing."""
    parts = [_GENERIC_IMAGE_DESCRIPTION]

    try:
        info = _active_image_capabilities()
    except Exception:  # noqa: BLE001
        return {"description": _GENERIC_IMAGE_DESCRIPTION}

    provider = info.get("provider")
    model = info.get("model")
    modalities = set(info.get("modalities") or ["text"])

    line = "\nActive backend"
    if provider:
        line += f": {provider}"
    if model:
        line += f" · model: {model}"
    parts.append(line)

    if "image" in modalities and "text" in modalities:
        max_refs = info.get("max_reference_images") or 0
        ref_note = (
            f"; up to {max_refs} reference image(s) via reference_image_urls"
            if max_refs and max_refs > 1
            else ""
        )
        parts.append(
            "- supports both text-to-image (omit image_url) and "
            f"image-to-image / editing (pass image_url){ref_note} — "
            "routes automatically"
        )
    elif "image" in modalities and "text" not in modalities:
        parts.append(
            "- this model is image-to-image / edit only — image_url is REQUIRED"
        )
    else:
        parts.append(
            "- this model is text-to-image only — it is NOT capable of "
            "image-to-image / editing; do not pass image_url or "
            "reference_image_urls (they will be rejected). Provide a "
            "text-only prompt."
        )

    return {"description": "\n".join(parts)}


registry.register(
    name="image_generate",
    toolset="image_gen",
    schema=IMAGE_GENERATE_SCHEMA,
    handler=_handle_image_generate,
    check_fn=check_image_generation_requirements,
    requires_env=[],
    is_async=False,   # sync fal_client API to avoid "Event loop is closed" in gateway
    emoji="🎨",
    dynamic_schema_overrides=_build_dynamic_image_schema,
)
