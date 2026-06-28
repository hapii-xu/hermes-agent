#!/usr/bin/env python3
"""
视频生成工具
============

对外提供单个 ``video_generate`` 工具，负责派发到由插件注册的视频生成
provider。该设计与 ``image_generate`` 保持一致：

- ``agent/video_gen_provider.py`` 定义了 :class:`VideoGenProvider` 抽象基类。
- ``agent/video_gen_registry.py`` 持有当前可用的 provider（在导入阶段由插件
  填充）。
- 每个 provider 位于 ``plugins/video_gen/<name>/`` 目录下。

该工具本身刻意与后端无关，并且**不内置任何 provider**——通过启用插件
（``hermes plugins enable video_gen/<name>``）并在 ``hermes tools`` → 视频生成
中选中它来开启某个后端。

统一接口
--------
单个工具即可覆盖常见场景——文生视频、图生视频、视频编辑、视频续拍——
schema 紧凑：

    prompt                   文本指令（generate/edit 时必填）
    operation                "generate" | "edit" | "extend"
    image_url                operation=generate 时驱动图生视频
    video_url                edit/extend 的源视频
    reference_image_urls     列表，上限由 provider 声明
    duration                 秒数（由 provider 钳制）
    aspect_ratio             "16:9" | "9:16" | "1:1" | ...
    resolution               "480p" | "540p" | "720p" | "1080p"
    negative_prompt          可选（Pixverse/Kling 风格）
    audio                    可选（Veo3/Pixverse 的计价档位）
    seed                     可选
    model                    可选，覆盖当前 provider 的默认模型

provider 会忽略它不支持的参数。工具层只做**轻量级**校验（类型/必填
prompt），把实际的钳制工作交给每个 provider 在
:meth:`VideoGenProvider.generate` 内部完成——这样随着新 provider 带
着不同能力上线，工具接口仍能保持稳定。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from agent.video_gen_provider import (
    COMMON_ASPECT_RATIOS,
    COMMON_RESOLUTIONS,
    DEFAULT_ASPECT_RATIO,
    DEFAULT_RESOLUTION,
    error_response,
)
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


VIDEO_GENERATE_SCHEMA: Dict[str, Any] = {
    "name": "video_generate",
    # 占位符——真正的 description 会在 get_tool_definitions() 时动态构建，
    # 以反映当前后端的实际能力（用户当前选中的模型支持哪些模态 / 分辨率 /
    # 时长范围）。见下文 _build_dynamic_video_schema() 以及 dynamic-tool-schemas
    # 技能文档 github/hermes-agent-dev/references/dynamic-tool-schemas.md。
    "description": "(rebuilt at get_definitions() time — see _build_dynamic_video_schema)",
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "Text instruction describing the desired video, motion, "
                    "subject, style, camera movement, etc."
                ),
            },
            "image_url": {
                "type": "string",
                "description": (
                    "Optional public URL of a still image. When provided, "
                    "the active backend routes to its image-to-video "
                    "endpoint (animate the image); when omitted, it routes "
                    "to text-to-video. Pass either a URL the user supplied "
                    "or a path/URL from the conversation."
                ),
            },
            "reference_image_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of reference image URLs (style or "
                    "character refs). Only supported by some backends; "
                    "the active backend's description below indicates whether "
                    "this is honored and what the max is."
                ),
            },
            "duration": {
                "type": "integer",
                "description": (
                    "Desired video duration in seconds. Providers clamp to "
                    "their supported range (commonly 4-15s). Omit to use the "
                    "provider's default."
                ),
            },
            "aspect_ratio": {
                "type": "string",
                "enum": list(COMMON_ASPECT_RATIOS),
                "description": (
                    "Output aspect ratio. Providers clamp to their supported "
                    "set."
                ),
                "default": DEFAULT_ASPECT_RATIO,
            },
            "resolution": {
                "type": "string",
                "enum": list(COMMON_RESOLUTIONS),
                "description": (
                    "Output resolution. Providers clamp to their supported "
                    "set."
                ),
                "default": DEFAULT_RESOLUTION,
            },
            "negative_prompt": {
                "type": "string",
                "description": (
                    "Optional negative prompt — content to avoid in the "
                    "output. Supported by Pixverse, Kling, and similar; "
                    "ignored by providers that do not support it."
                ),
            },
            "audio": {
                "type": "boolean",
                "description": (
                    "Optional audio generation toggle. Supported by Veo3 and "
                    "Pixverse (affects pricing tier); ignored elsewhere."
                ),
            },
            "seed": {
                "type": "integer",
                "description": (
                    "Optional seed for reproducible outputs (provider-"
                    "dependent)."
                ),
            },
            "model": {
                "type": "string",
                "description": (
                    "Optional model override. If omitted, the user's "
                    "configured ``video_gen.model`` (set via `hermes tools` "
                    "→ Video Generation) is used. Models that the active "
                    "provider does not know are rejected."
                ),
            },
        },
        "required": ["prompt"],
    },
}


# ---------------------------------------------------------------------------
# 配置读取（与 image_generation_tool.py 保持一致）
# ---------------------------------------------------------------------------


def _read_video_gen_section() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("video_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:
        logger.debug("Could not read video_gen config: %s", exc)
        return {}


def _read_configured_video_provider() -> Optional[str]:
    value = _read_video_gen_section().get("provider")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _read_configured_video_model() -> Optional[str]:
    value = _read_video_gen_section().get("model")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


# ---------------------------------------------------------------------------
# 可用性检查
# ---------------------------------------------------------------------------


def check_video_generation_requirements() -> bool:
    """当至少有一个已注册的 provider 报告可用时返回 True。

    会触发插件发现（幂等），以便用户安装的插件能被工具集门槛看到。
    """
    try:
        from agent.video_gen_registry import list_providers
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
# 派发
# ---------------------------------------------------------------------------


def _resolve_active_provider():
    """返回当前活动的 provider 对象，没有则返回 None。

    在检查注册表之前强制触发插件发现——以处理某个长寿会话在插件安装
    之前就已经启动的情况。
    """
    try:
        from agent.video_gen_registry import get_active_provider
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
        provider = get_active_provider()
        if provider is None:
            _ensure_plugins_discovered(force=True)
            provider = get_active_provider()
        return provider
    except Exception as exc:
        logger.debug("video_gen provider resolution failed: %s", exc)
        return None


def _missing_provider_error(configured: Optional[str]) -> str:
    if configured:
        msg = (
            f"video_gen.provider='{configured}' is set but no plugin "
            f"registered that name. Run `hermes plugins list` to see "
            f"installed video gen backends, or `hermes tools` → Video "
            f"Generation to pick one."
        )
        return json.dumps(error_response(
            error=msg, error_type="provider_not_registered",
            provider=configured,
        ))
    msg = (
        "No video generation backend is configured. Run `hermes tools` → "
        "Video Generation to enable one (xAI, FAL, or Google Veo)."
    )
    return json.dumps(error_response(
        error=msg, error_type="no_provider_configured",
    ))


# ---------------------------------------------------------------------------
# 处理器
# ---------------------------------------------------------------------------


def _coerce_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "1", "yes", "on"}:
            return True
        if v in {"false", "0", "no", "off"}:
            return False
    return None


def _normalize_reference_images(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return None
    out: List[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out or None


def _handle_video_generate(args: Dict[str, Any], **_kw: Any) -> str:
    prompt = (args.get("prompt") or "").strip()
    image_url = (args.get("image_url") or "").strip() or None
    reference_image_urls = _normalize_reference_images(args.get("reference_image_urls"))
    duration = _coerce_int(args.get("duration"))
    aspect_ratio = (args.get("aspect_ratio") or DEFAULT_ASPECT_RATIO).strip() or DEFAULT_ASPECT_RATIO
    resolution = (args.get("resolution") or DEFAULT_RESOLUTION).strip() or DEFAULT_RESOLUTION
    negative_prompt = (args.get("negative_prompt") or "").strip() or None
    audio = _coerce_bool(args.get("audio"))
    seed = _coerce_int(args.get("seed"))
    model_override = (args.get("model") or "").strip() or None

    # 软校验——真正的校验由 provider 自己做。schema 要求 prompt 必填；
    # 后端在其图生视频端点上可能仍然接受纯图片输入，但本工具接口始终需要
    # 一个 prompt。
    if not prompt:
        return tool_error("prompt is required for video generation")

    # 解析当前活动的 provider。
    configured = _read_configured_video_provider()
    provider = _resolve_active_provider()
    if provider is None:
        return _missing_provider_error(configured)

    # 解析模型：显式参数优先，其次是配置，最后是 provider 默认值。
    model = model_override or _read_configured_video_model() or provider.default_model()

    kwargs: Dict[str, Any] = {
        "model": model,
        "_model_override_explicit": bool(model_override),
        "image_url": image_url,
        "reference_image_urls": reference_image_urls,
        "duration": duration,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "negative_prompt": negative_prompt,
        "audio": audio,
        "seed": seed,
    }
    # 去掉为 None 的项，以便 provider 看到干净的默认值。
    kwargs = {k: v for k, v in kwargs.items() if v is not None}

    try:
        result = provider.generate(prompt=prompt, **kwargs)
    except TypeError as exc:
        # provider 没有扩展其函数签名属于 bug，而非调用方错误——
        # 记录日志并返回一条清晰的契约错误信息。
        logger.warning(
            "video_gen provider '%s' rejected kwargs (signature too narrow): %s",
            getattr(provider, "name", "?"), exc,
        )
        return json.dumps(error_response(
            error=(
                f"Provider '{getattr(provider, 'name', '?')}' signature is "
                f"out of date with the video_generate schema. Report this "
                f"to the plugin author."
            ),
            error_type="provider_contract",
            provider=getattr(provider, "name", ""),
            model=model or "",
            prompt=prompt,
        ))
    except Exception as exc:
        logger.warning(
            "video_gen provider '%s' raised: %s",
            getattr(provider, "name", "?"), exc,
        )
        return json.dumps(error_response(
            error=f"Provider '{getattr(provider, 'name', '?')}' error: {exc}",
            error_type="provider_exception",
            provider=getattr(provider, "name", ""),
            model=model or "",
            prompt=prompt,
        ))

    if not isinstance(result, dict):
        return json.dumps(error_response(
            error="Provider returned a non-dict result",
            error_type="provider_contract",
            provider=getattr(provider, "name", ""),
            model=model or "",
            prompt=prompt,
        ))

    return json.dumps(result)


# ---------------------------------------------------------------------------
# 动态 schema——反映当前后端的实际能力
# ---------------------------------------------------------------------------
#
# 为什么要动态：用户配置的后端决定了哪些操作（generate/edit/extend）、
# 模态（文本 / 图片 / 参考）、宽高比、分辨率、时长，以及 audio/
# negative-prompt 开关是真实存在的。如果模型在不知道当前后端的情况下
# 调用 video_generate，就会浪费一回合，比如「fal-ai/veo3.1/image-to-video
# requires image_url」这种错误。在 description 中暴露每个模型的接口，
# 意味着模型通常能在第一次就调用正确。
#
# 记忆化：model_tools.get_tool_definitions() 以 config.yaml 的 mtime 作为
# 缓存键，因此当用户通过 `hermes tools` 或 `/skills` 更改 provider/model 时，
# schema 会自动重建。


_GENERIC_DESCRIPTION = (
    "Generate a video from a text prompt (text-to-video) or animate a "
    "still image (image-to-video) using the user's configured video "
    "generation backend. Pass `image_url` to animate that image; omit it "
    "to generate from text alone. The backend auto-routes to the right "
    "endpoint. The backend and model family are user-configured via "
    "`hermes tools` → Video Generation; the agent does not pick them. "
    "Long-running generations may take 30 seconds to several minutes — "
    "the call blocks until the video is ready. Returns the result in the "
    "`video` field — either an HTTP URL or an absolute file path. To show "
    "it to the user, reference that path/URL in your response using the "
    "file-delivery convention for the current platform (your platform "
    "guidance describes how files are delivered here)."
)


def _format_model_caveats(
    model_meta: Dict[str, Any],
    backend_caps: Dict[str, Any],
) -> List[str]:
    """从某个模型的目录元数据中提取人类可读的注意事项。

    只暴露那些与后端整体能力有实质差异的内容——重复默认值只会是噪音。
    """
    caveats: List[str] = []

    modalities = set(model_meta.get("modalities") or [])
    modality = model_meta.get("modality")  # FAL 的插件为单模态条目使用此键
    if modality:
        modalities.add(modality)

    if "image" in modalities and "text" not in modalities:
        caveats.append(
            "this model is image-to-video only — image_url is REQUIRED; "
            "text-only calls will be rejected"
        )
    elif "text" in modalities and "image" not in modalities:
        caveats.append(
            "this model is text-to-video only — image_url is not supported"
        )

    return caveats


def _build_dynamic_video_schema() -> Dict[str, Any]:
    """构建一段反映当前后端实际接口的描述。

    开销很小：读取配置（已由调用方记忆化），向当前 provider 请求
    `capabilities()` 以及当前模型的目录条目，并格式化几行说明文字。当没有
    配置或注册任何 provider 时，回退到通用描述。
    """
    parts: List[str] = [_GENERIC_DESCRIPTION]

    configured = _read_configured_video_provider()
    configured_model = _read_configured_video_model()

    if not configured:
        parts.append(
            "\nNo video backend is configured. Calls will return an error "
            "until the user picks one via `hermes tools` → Video Generation."
        )
        return {"description": "\n".join(parts)}

    try:
        from agent.video_gen_registry import get_provider
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
        provider = get_provider(configured)
    except Exception:
        provider = None

    if provider is None:
        parts.append(
            f"\nActive backend: {configured} (plugin not yet loaded — the "
            f"tool will retry discovery on first call)."
        )
        return {"description": "\n".join(parts)}

    try:
        caps = provider.capabilities() or {}
    except Exception:
        caps = {}
    try:
        models = provider.list_models() or []
    except Exception:
        models = []

    active_model = configured_model or provider.default_model()
    model_meta = next(
        (m for m in models if isinstance(m, dict) and m.get("id") == active_model),
        {},
    )

    backend_label = provider.display_name
    line = f"\nActive backend: {backend_label}"
    if active_model:
        line += f" · model: {active_model}"
    parts.append(line)

    # 模型专用注意事项（高信号内容）
    for c in _format_model_caveats(model_meta, caps):
        parts.append(f"- {c}")

    # 后端模态概要——仅在后端同时支持文本和图片时才有用。单模态后端已由
    # 上方的模型注意事项覆盖。
    modalities = set(caps.get("modalities") or [])
    if "text" in modalities and "image" in modalities and not model_meta.get("modality"):
        parts.append(
            "- supports both text-to-video (omit image_url) and "
            "image-to-video (pass image_url) — routes automatically"
        )

    if caps.get("aspect_ratios"):
        parts.append(f"- aspect_ratio choices: {', '.join(caps['aspect_ratios'])}")
    if caps.get("resolutions"):
        parts.append(f"- resolution choices: {', '.join(caps['resolutions'])}")
    if caps.get("min_duration") and caps.get("max_duration"):
        parts.append(
            f"- duration range: {caps['min_duration']}-{caps['max_duration']}s"
        )
    if caps.get("supports_audio"):
        parts.append("- audio: pass `audio=true` to enable native audio (pricing tier)")
    if caps.get("supports_negative_prompt"):
        parts.append("- negative_prompt: supported")
    max_refs = caps.get("max_reference_images") or 0
    if max_refs:
        parts.append(f"- reference_image_urls: up to {max_refs} images")

    return {"description": "\n".join(parts)}


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


registry.register(
    name="video_generate",
    toolset="video_gen",
    schema=VIDEO_GENERATE_SCHEMA,
    handler=_handle_video_generate,
    check_fn=check_video_generation_requirements,
    requires_env=[],
    is_async=False,
    emoji="🎬",
    dynamic_schema_overrides=_build_dynamic_video_schema,
)
