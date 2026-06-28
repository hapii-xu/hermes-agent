"""xAI Grok-Imagine 视频生成后端。

支持：文生视频和图生视频（对输入图片进行动画处理）
通过 xAI 的 ``/videos/generations`` 端点实现。编辑和扩展功能不
在此统一接口中暴露 — xAI 是唯一支持这些功能的后端，
而不一致会迫使 agent 的工具描述中出现按后端区分的文案。

最初从 @Jaaneek 的 PR #10600 中 salvaged；后来重构为
:class:`VideoGenProvider` 插件接口，并精简为仅生成接口。

认证方式：xAI Grok OAuth token（首选 — 计入用户的
SuperGrok 或 X Premium+ 订阅）或 ``XAI_API_KEY``。两种方式均通过
``tools.xai_http.resolve_xai_http_credentials`` 解析，因此一次登录
即可覆盖聊天 + TTS + 图片生成 + 视频生成 + 转录。
输出为 xAI CDN 的 HTTPS URL；网关负责下载并交付。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from agent.video_gen_provider import (
    VideoGenProvider,
    error_response,
    success_response,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"
DEFAULT_TEXT_TO_VIDEO_MODEL = "grok-imagine-video"
DEFAULT_IMAGE_TO_VIDEO_MODEL = "grok-imagine-video-1.5-preview"
DEFAULT_MODEL = DEFAULT_TEXT_TO_VIDEO_MODEL
DEFAULT_DURATION = 8
DEFAULT_ASPECT_RATIO = "16:9"
DEFAULT_RESOLUTION = "720p"
DEFAULT_TIMEOUT_SECONDS = 240
DEFAULT_POLL_INTERVAL_SECONDS = 5

VALID_ASPECT_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"}
VALID_RESOLUTIONS = {"480p", "720p"}
MAX_REFERENCE_IMAGES = 7


_MODELS: Dict[str, Dict[str, Any]] = {
    "grok-imagine-video": {
        "display": "Grok Imagine Video",
        "speed": "~60-240s",
        "strengths": "Text-to-video; legacy image-to-video fallback.",
        "price": "see https://docs.x.ai/developers/models/grok-imagine-video",
        "modalities": ["text", "image"],
    },
    "grok-imagine-video-1.5-preview": {
        "display": "Grok Imagine Video 1.5 Preview",
        "speed": "~60-240s",
        "strengths": "Latest xAI image-to-video model.",
        "price": "see https://docs.x.ai/developers/models/grok-imagine-video-1.5-preview",
        "modalities": ["image"],
        "aliases": ["grok-imagine-video-1.5-2026-05-30"],
    },
}


# ---------------------------------------------------------------------------
# HTTP 辅助函数
# ---------------------------------------------------------------------------


def _resolve_xai_credentials() -> Tuple[str, str]:
    """从共享的 xAI 凭证解析器返回 ``(api_key, base_url)``。

    优先级顺序：运行时 provider（xai-oauth 池条目）→ 单例 ``auth.json``
    OAuth token → ``XAI_API_KEY`` 环境变量。当没有可用的凭证来源时，
    ``api_key`` 为空；调用方必须在使用前进行检查。
    """
    try:
        from tools.xai_http import resolve_xai_http_credentials

        creds = resolve_xai_http_credentials() or {}
    except Exception as exc:
        logger.debug("xAI credential resolver failed: %s", exc)
        creds = {}

    api_key = str(creds.get("api_key") or os.getenv("XAI_API_KEY", "")).strip()
    base_url = str(
        creds.get("base_url")
        or os.getenv("XAI_BASE_URL")
        or DEFAULT_XAI_BASE_URL
    ).strip().rstrip("/")
    return api_key, base_url


def _xai_user_agent() -> str:
    try:
        from tools.xai_http import hermes_xai_user_agent

        return hermes_xai_user_agent()
    except Exception:
        return "hermes-agent/video_gen"


def _xai_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": _xai_user_agent(),
    }


def _image_ref_to_xai_url(value: str) -> str:
    """返回 xAI 图片输入可接受的 URL/data URI。"""
    ref = (value or "").strip()
    if not ref:
        return ""
    lower = ref.lower()
    if lower.startswith(("http://", "https://", "data:image/")):
        return ref

    path = Path(ref).expanduser()
    if not path.is_file():
        return ref

    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if not mime.startswith("image/"):
        return ref

    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _normalize_reference_images(reference_image_urls: Optional[List[str]]):
    refs = []
    for url in reference_image_urls or []:
        normalized = _image_ref_to_xai_url(url)
        if normalized:
            refs.append({"url": normalized})
    return refs or None


def _clamp_duration(duration: Optional[int], has_reference_images: bool) -> int:
    value = duration if duration is not None else DEFAULT_DURATION
    if value < 1:
        value = 1
    if value > 15:
        value = 15
    if has_reference_images and value > 10:
        value = 10
    return value


def _resolve_model_for_modality(
    model: Optional[str],
    *,
    modality: str,
    explicit_model: bool,
) -> str:
    """选择 xAI 的文/视频模型，不将配置视为 prompt 覆盖。

    ``grok-imagine-video-1.5-preview`` 目前拒绝纯文本视频生成，
    但它是所需的图生视频后端。用户通过显式 ``model=`` 仍可
    优先指定其他模型。
    """
    requested = (model or "").strip()
    if explicit_model and requested:
        return requested
    if modality == "image":
        return DEFAULT_IMAGE_TO_VIDEO_MODEL
    if requested == DEFAULT_IMAGE_TO_VIDEO_MODEL:
        return DEFAULT_TEXT_TO_VIDEO_MODEL
    return requested or DEFAULT_TEXT_TO_VIDEO_MODEL


async def _submit(
    client: httpx.AsyncClient,
    payload: Dict[str, Any],
    *,
    api_key: str,
    base_url: str,
) -> str:
    """POST 到 /videos/generations — xAI 用于文生视频和图生视频的唯一公开端点。"""
    response = await client.post(
        f"{base_url}/videos/generations",
        headers={**_xai_headers(api_key), "x-idempotency-key": str(uuid.uuid4())},
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()
    request_id = body.get("request_id")
    if not request_id:
        raise RuntimeError("xAI video response did not include request_id")
    return request_id


async def _poll(
    client: httpx.AsyncClient,
    request_id: str,
    *,
    api_key: str,
    base_url: str,
    timeout_seconds: int,
    poll_interval: int,
) -> Dict[str, Any]:
    elapsed = 0.0
    last_status = "queued"
    while elapsed < timeout_seconds:
        response = await client.get(
            f"{base_url}/videos/{request_id}",
            headers=_xai_headers(api_key),
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        last_status = (body.get("status") or "").lower()

        if last_status == "done":
            return {"status": "done", "body": body}
        if last_status in {"failed", "error", "expired", "cancelled"}:
            return {"status": last_status, "body": body}

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    return {"status": "timeout", "body": {"status": last_status}}


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class XAIVideoGenProvider(VideoGenProvider):
    """xAI Grok Imagine video backend (text-to-video + image-to-video)."""

    @property
    def name(self) -> str:
        return "xai"

    @property
    def display_name(self) -> str:
        return "xAI"

    def is_available(self) -> bool:
        api_key, _ = _resolve_xai_credentials()
        return bool(api_key)

    def list_models(self) -> List[Dict[str, Any]]:
        return [{"id": mid, **meta} for mid, meta in _MODELS.items()]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        # Auth resolution lives entirely in the shared ``xai_grok`` post_setup
        # hook (``hermes_cli/tools_config.py``) so the picker doesn't blindly
        # prompt for an API key when the user is already signed in via xAI
        # Grok OAuth (SuperGrok / Premium+) — TTS / image gen / video gen
        # all share the same credential resolver. The hook offers an
        # OAuth-vs-API-key choice when neither is configured.
        return {
            "name": "xAI Grok Imagine",
            "badge": "paid",
            "tag": "grok-imagine-video for text-to-video; grok-imagine-video-1.5-preview for image-to-video; uses xAI Grok OAuth or XAI_API_KEY",
            "env_vars": [],
            "post_setup": "xai_grok",
        }

    def capabilities(self) -> Dict[str, Any]:
        return {
            "modalities": ["text", "image"],
            "aspect_ratios": sorted(VALID_ASPECT_RATIOS),
            "resolutions": sorted(VALID_RESOLUTIONS),
            "max_duration": 15,
            "min_duration": 1,
            "supports_audio": False,
            "supports_negative_prompt": False,
            "max_reference_images": MAX_REFERENCE_IMAGES,
        }

    def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        duration: Optional[int] = None,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        resolution: str = DEFAULT_RESOLUTION,
        negative_prompt: Optional[str] = None,
        audio: Optional[bool] = None,
        seed: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        try:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(self._generate_async(
                    prompt=prompt,
                    model=model,
                    explicit_model=bool(kwargs.get("_model_override_explicit")),
                    image_url=image_url,
                    reference_image_urls=reference_image_urls,
                    duration=duration,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                ))
            finally:
                loop.close()
        except Exception as exc:
            logger.warning("xAI video gen unexpected failure: %s", exc, exc_info=True)
            return error_response(
                error=f"xAI video generation failed: {exc}",
                error_type="api_error",
                provider="xai",
                model=model or DEFAULT_MODEL,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )

    async def _generate_async(
        self,
        *,
        prompt: str,
        model: Optional[str],
        explicit_model: bool,
        image_url: Optional[str],
        reference_image_urls: Optional[List[str]],
        duration: Optional[int],
        aspect_ratio: str,
        resolution: str,
    ) -> Dict[str, Any]:
        api_key, base_url = _resolve_xai_credentials()
        if not api_key:
            return error_response(
                error=(
                    "No xAI credentials found. Sign in via `hermes auth add xai-oauth` "
                    "(SuperGrok / Premium+) or set XAI_API_KEY from "
                    "https://console.x.ai/."
                ),
                error_type="auth_required",
                provider="xai", prompt=prompt,
            )

        prompt = (prompt or "").strip()
        image_url_norm = _image_ref_to_xai_url(image_url or "") or None
        normalized_aspect_ratio = (aspect_ratio or DEFAULT_ASPECT_RATIO).strip()
        normalized_resolution = (resolution or DEFAULT_RESOLUTION).strip().lower()
        modality_used = "image" if image_url_norm else "text"
        resolved_model = _resolve_model_for_modality(
            model,
            modality=modality_used,
            explicit_model=explicit_model,
        )

        if not prompt:
            return error_response(
                error=(
                    "prompt is required for xAI video generation "
                    "(text-to-video or image-to-video)"
                ),
                error_type="missing_prompt",
                provider="xai", prompt=prompt,
            )

        refs = _normalize_reference_images(reference_image_urls)
        if refs and len(refs) > MAX_REFERENCE_IMAGES:
            return error_response(
                error=f"reference_image_urls supports at most {MAX_REFERENCE_IMAGES} images on xAI",
                error_type="too_many_references",
                provider="xai", prompt=prompt,
            )
        if image_url_norm and refs:
            return error_response(
                error="image_url and reference_image_urls cannot be combined on xAI",
                error_type="conflicting_inputs",
                provider="xai", prompt=prompt,
            )

        clamped_duration = _clamp_duration(duration, has_reference_images=bool(refs))

        if normalized_aspect_ratio not in VALID_ASPECT_RATIOS:
            normalized_aspect_ratio = DEFAULT_ASPECT_RATIO
        if normalized_resolution not in VALID_RESOLUTIONS:
            normalized_resolution = DEFAULT_RESOLUTION

        payload: Dict[str, Any] = {
            "model": resolved_model,
            "prompt": prompt,
            "duration": clamped_duration,
            "aspect_ratio": normalized_aspect_ratio,
            "resolution": normalized_resolution,
        }
        if image_url_norm:
            payload["image"] = {"url": image_url_norm}
        if refs:
            payload["reference_images"] = refs

        async with httpx.AsyncClient() as client:
            try:
                request_id = await _submit(
                    client, payload, api_key=api_key, base_url=base_url
                )
            except httpx.HTTPStatusError as exc:
                detail = ""
                try:
                    detail = exc.response.text[:500]
                except Exception:
                    pass
                return error_response(
                    error=f"xAI submit failed ({exc.response.status_code}): {detail or exc}",
                    error_type="api_error",
                    provider="xai",
                    model=resolved_model,
                    prompt=prompt,
                )

            poll_result = await _poll(
                client, request_id,
                api_key=api_key, base_url=base_url,
                timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                poll_interval=DEFAULT_POLL_INTERVAL_SECONDS,
            )

        status = poll_result["status"]
        body = poll_result["body"]

        if status == "done":
            video = body.get("video") or {}
            url = video.get("url")
            if not url:
                return error_response(
                    error="xAI video generation completed without a video URL",
                    error_type="empty_response",
                    provider="xai",
                    model=body.get("model") or resolved_model,
                    prompt=prompt,
                )
            extra: Dict[str, Any] = {
                "request_id": request_id,
                "resolution": normalized_resolution,
            }
            if body.get("usage"):
                extra["usage"] = body["usage"]
            return success_response(
                video=url,
                model=body.get("model") or resolved_model,
                prompt=prompt,
                modality=modality_used,
                aspect_ratio=normalized_aspect_ratio,
                duration=video.get("duration") or clamped_duration,
                provider="xai",
                extra=extra,
            )

        if status == "timeout":
            return error_response(
                error=f"Timed out waiting for video generation after {DEFAULT_TIMEOUT_SECONDS}s",
                error_type="timeout",
                provider="xai",
                model=resolved_model,
                prompt=prompt,
            )

        message = (
            (body.get("error", {}) or {}).get("message")
            or body.get("message")
            or f"xAI video generation ended with status '{status}'"
        )
        return error_response(
            error=message,
            error_type=f"xai_{status}",
            provider="xai",
            model=resolved_model,
            prompt=prompt,
        )


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Plugin entry point — wire ``XAIVideoGenProvider`` into the registry."""
    ctx.register_video_gen_provider(XAIVideoGenProvider())
