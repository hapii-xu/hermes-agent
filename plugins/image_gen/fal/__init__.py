"""FAL.ai 图像生成后端。

将包含 18 个模型的 FAL 目录（FLUX 2、Z-Image、Nano Banana、GPT
Image 1.5、Recraft、Imagen 4、Qwen、Ideogram 等）封装为
:class:`ImageGenProvider` 实现。

核心功能——模型目录、请求体构建、请求提交、managed-Nous-gateway 选择、
Clarity Upscaler 链式调用——均位于 :mod:`tools.image_generation_tool` 模块中。
本插件通过调用时间接引用（``import tools.image_generation_tool as _it``）
访问该模块，因此：

* 现有测试套件（``tests/tools/test_image_generation.py``、
  ``tests/tools/test_managed_media_gateways.py``）无需修改即可继续 patch
  ``image_tool._submit_fal_request`` / ``image_tool.fal_client`` /
  ``image_tool._managed_fal_client``；
* 磁盘上仅存在唯一的规范 FAL 代码路径——本插件仅为注册适配器，
  而非并行的重复实现。

迁移计划及本文件所遵循的
``plugin-extraction-test-patch-compatibility.md`` 规则详见 issue #26241。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    resolve_aspect_ratio,
)

logger = logging.getLogger(__name__)


class FalImageGenProvider(ImageGenProvider):
    """FAL.ai image generation backend.

    Delegates to ``tools.image_generation_tool.image_generate_tool`` so
    the in-tree FAL implementation (model catalog, payload builder,
    managed-gateway selection, Clarity Upscaler chaining) is the single
    source of truth. Everything is resolved at call time via the
    ``_it`` indirection so tests can monkey-patch the legacy module.
    """

    @property
    def name(self) -> str:
        return "fal"

    @property
    def display_name(self) -> str:
        return "FAL.ai"

    def is_available(self) -> bool:
        # Available when direct FAL_KEY is set OR the managed Nous
        # gateway resolves a fal-queue origin. Both checks come from the
        # legacy module so this provider tracks whatever logic ships
        # there.
        import tools.image_generation_tool as _it
        try:
            return bool(_it.check_fal_api_key())
        except Exception:  # noqa: BLE001 — defensive; never break the picker
            return False

    def list_models(self) -> List[Dict[str, Any]]:
        import tools.image_generation_tool as _it
        return [
            {
                "id": model_id,
                "display": meta.get("display", model_id),
                "speed": meta.get("speed", ""),
                "strengths": meta.get("strengths", ""),
                "price": meta.get("price", ""),
            }
            for model_id, meta in _it.FAL_MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        import tools.image_generation_tool as _it
        return _it.DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "FAL.ai",
            "badge": "paid",
            "tag": "Pick from flux-2-klein, flux-2-pro, gpt-image, nano-banana, etc. — text-to-image & image editing",
            "env_vars": [
                {
                    "key": "FAL_KEY",
                    "prompt": "FAL API key",
                    "url": "https://fal.ai/dashboard/keys",
                },
            ],
        }

    def capabilities(self) -> Dict[str, Any]:
        # Whether image-to-image is available depends on the currently-
        # selected FAL model (each model entry declares an edit_endpoint or
        # not). Report the active model's actual surface so the dynamic tool
        # schema is accurate.
        import tools.image_generation_tool as _it

        try:
            _model_id, meta = _it._resolve_fal_model()
        except Exception:  # noqa: BLE001
            return {"modalities": ["text"], "max_reference_images": 0}
        if meta.get("edit_endpoint"):
            return {
                "modalities": ["text", "image"],
                "max_reference_images": int(meta.get("max_reference_images") or 1),
            }
        return {"modalities": ["text"], "max_reference_images": 0}

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Generate or edit an image via the legacy FAL pipeline.

        Forwards prompt + aspect_ratio + image_url/reference_image_urls (and
        any forward-compat extras the schema supports) into
        :func:`tools.image_generation_tool.image_generate_tool`, then reshapes
        its JSON-string response into the provider-ABC dict format consumed by
        ``_dispatch_to_plugin_provider``.
        """
        import tools.image_generation_tool as _it

        aspect = resolve_aspect_ratio(aspect_ratio)
        passthrough = {
            key: kwargs[key]
            for key in (
                "num_inference_steps",
                "guidance_scale",
                "num_images",
                "output_format",
                "seed",
            )
            if key in kwargs and kwargs[key] is not None
        }
        # Only forward the image-to-image inputs when actually supplied, so a
        # plain text-to-image call delegates exactly as it did before (no
        # noisy None kwargs).
        if image_url is not None:
            passthrough["image_url"] = image_url
        if reference_image_urls is not None:
            passthrough["reference_image_urls"] = reference_image_urls

        try:
            raw = _it.image_generate_tool(
                prompt=prompt,
                aspect_ratio=aspect,
                **passthrough,
            )
        except Exception as exc:  # noqa: BLE001 — never raise out of generate
            logger.warning("FAL image_generate_tool raised: %s", exc, exc_info=True)
            return {
                "success": False,
                "image": None,
                "error": f"FAL image generation failed: {exc}",
                "error_type": type(exc).__name__,
                "provider": "fal",
                "prompt": prompt,
                "aspect_ratio": aspect,
            }

        try:
            response = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:  # noqa: BLE001
            response = {"success": False, "image": None, "error": "Invalid JSON from FAL pipeline"}

        if not isinstance(response, dict):
            response = {
                "success": False,
                "image": None,
                "error": "FAL pipeline returned a non-dict response",
                "error_type": "provider_contract",
            }

        # Stamp provider/prompt/aspect_ratio so downstream consumers see
        # the uniform shape declared in ``agent.image_gen_provider``.
        response.setdefault("provider", "fal")
        response.setdefault("prompt", prompt)
        response.setdefault("aspect_ratio", aspect)
        # Annotate model best-effort — the legacy pipeline resolves it
        # internally, so query it after the fact for the response shape.
        if "model" not in response:
            try:
                model_id, _meta = _it._resolve_fal_model()
                response["model"] = model_id
            except Exception:  # noqa: BLE001
                pass
        return response


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Plugin entry point — wire ``FalImageGenProvider`` into the registry."""
    ctx.register_image_gen_provider(FalImageGenProvider())
