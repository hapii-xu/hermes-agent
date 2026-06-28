"""
视频生成 Provider ABC
=============================

定义视频生成的可插拔后端接口。Provider 通过
``PluginContext.register_video_gen_provider()`` 注册实例；当前活跃的
provider（通过 ``config.yaml`` 中的 ``video_gen.provider`` 选择）
处理每次 ``video_generate`` 工具调用。

Provider 位于 ``<repo>/plugins/video_gen/<name>/``（内置，作为
``kind: backend`` 自动加载）或 ``~/.hermes/plugins/video_gen/<name>/``
（用户安装，通过 ``plugins.enabled`` 选择性启用）。

镜像 ``image_gen`` provider 设计（``agent/image_gen_provider.py``），
使两个接口可以一起学习。

统一接口
---------------
一个工具 — ``video_generate`` — 涵盖 **text-to-video** 和 **image-to-video**。
路由依据是 ``image_url`` 的存在：如果设置了，provider 路由到
image-to-video 端点；如果省略，provider 路由到 text-to-video。
用户选择一个 **模型族**（例如 Pixverse v6、Veo 3.1、
Kling O3 Standard）；provider 负责处理要访问的底层 FAL/xAI 端点。

视频编辑和视频延展有意不在此接口中暴露——
各后端之间的差异太大，无法用一个统一的工具覆盖。如果
这些用例日后需要关注，可以作为单独的工具发布。

响应结构
--------------
所有 provider 返回由 :func:`success_response` /
:func:`error_response` 构建的字典。键值：

    success         bool
    video           str | None      URL 或绝对文件路径
    model           str             provider 特定的模型标识符
    prompt          str             回显的 prompt
    modality        str             "text" | "image"（使用了哪种模式）
    aspect_ratio    str             provider 原生格式（例如 "16:9"）或 ""
    duration        int             秒数（不适用时为 0）
    provider        str             provider 名称（用于诊断）
    error           str             仅在 success=False 时
    error_type      str             仅在 success=False 时
"""

from __future__ import annotations

import abc
import base64
import datetime
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# 各 provider 通用的宽高比（Veo / Kling / xAI / Pixverse）。
# 工具 schema 将此集合作为 enum 提示公开，但 provider 可能接受
# 更窄或更宽的集合——它们负责做范围限制。
COMMON_ASPECT_RATIOS: Tuple[str, ...] = ("16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3")
DEFAULT_ASPECT_RATIO = "16:9"

COMMON_RESOLUTIONS: Tuple[str, ...] = ("480p", "540p", "720p", "1080p")
DEFAULT_RESOLUTION = "720p"


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


class VideoGenProvider(abc.ABC):
    """视频生成后端的抽象基类。

    子类必须实现 :meth:`generate`。其他方法都有合理的
    默认值——只需覆盖你的 provider 需要的方法。
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """在 ``video_gen.provider`` 配置中使用的稳定短标识符。

        小写，无空格。示例：``xai``、``fal``、``google``。
        """

    @property
    def display_name(self) -> str:
        """在 ``hermes tools`` 中显示的人类可读标签。默认为 ``name.title()``。"""
        return self.name.title()

    def is_available(self) -> bool:
        """当此 provider 可以处理调用时返回 True。

        通常检查必需的 API key 和可选依赖的导入。默认值：True。
        """
        return True

    def list_models(self) -> List[Dict[str, Any]]:
        """返回 ``hermes tools`` 模型选择器的目录条目。

        每个条目代表一个支持内部 text-to-video 和/或 image-to-video
        路由的 **模型族**::

            {
                "id": "veo-3.1",                       # 必需
                "display": "Veo 3.1",                  # 可选；默认为 id
                "speed": "~60s",                       # 可选
                "strengths": "...",                    # 可选
                "price": "$0.20/s",                    # 可选
                "modalities": ["text", "image"],       # 可选，仅供参考
            }

        默认值：空列表（provider 没有用户可选的模型）。
        """
        return []

    def get_setup_schema(self) -> Dict[str, Any]:
        """返回 ``hermes tools`` 选择器的 provider 元数据。"""
        return {
            "name": self.display_name,
            "badge": "",
            "tag": "",
            "env_vars": [],
        }

    def default_model(self) -> Optional[str]:
        """返回默认模型 id，不适用时返回 None。"""
        models = self.list_models()
        if models:
            return models[0].get("id")
        return None

    def capabilities(self) -> Dict[str, Any]:
        """返回此 provider 支持的功能。

        返回的字典（所有键可选）::

            {
                "modalities": ["text", "image"],      # 后端接受的输入类型
                "aspect_ratios": ["16:9", "9:16", ...],
                "resolutions": ["720p", "1080p"],
                "max_duration": 15,                   # 秒数
                "min_duration": 1,
                "supports_audio": True,
                "supports_negative_prompt": True,
                "max_reference_images": 7,
            }

        由工具层用于软验证，由 ``hermes tools`` 用于选择器。
        默认值：仅 text。
        """
        return {
            "modalities": ["text"],
            "aspect_ratios": list(COMMON_ASPECT_RATIOS),
            "resolutions": list(COMMON_RESOLUTIONS),
            "max_duration": 10,
            "min_duration": 1,
            "supports_audio": False,
            "supports_negative_prompt": False,
            "max_reference_images": 0,
        }

    @abc.abstractmethod
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
        """从 prompt 生成视频（text-to-video）或将图片动画化
        （image-to-video）。

        路由规则：如果提供了 ``image_url``，provider 应路由到
        image-to-video 端点；否则路由到 text-to-video。插件
        负责在用户选择的模型族中选取正确的底层端点。

        实现应返回 :func:`success_response` 或 :func:`error_response`
        的字典。``kwargs`` 可能包含 schema 未来版本将暴露的
        前向兼容参数——实现必须忽略未知键（不抛出 TypeError）。
        """


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _videos_cache_dir() -> Path:
    """返回 ``$HERMES_HOME/cache/videos/``，根据需要创建父目录。"""
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "cache" / "videos"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_b64_video(
    b64_data: str,
    *,
    prefix: str = "video",
    extension: str = "mp4",
) -> Path:
    """解码 base64 视频数据并写入 ``$HERMES_HOME/cache/videos/``。

    返回保存文件的绝对 :class:`Path`。

    文件名格式：``<prefix>_<YYYYMMDD_HHMMSS>_<short-uuid>.<ext>``。
    """
    raw = base64.b64decode(b64_data)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    short = uuid.uuid4().hex[:8]
    path = _videos_cache_dir() / f"{prefix}_{ts}_{short}.{extension}"
    path.write_bytes(raw)
    return path


def save_bytes_video(
    raw: bytes,
    *,
    prefix: str = "video",
    extension: str = "mp4",
) -> Path:
    """将原始视频字节（例如 HTTP 下载响应体）写入缓存。"""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    short = uuid.uuid4().hex[:8]
    path = _videos_cache_dir() / f"{prefix}_{ts}_{short}.{extension}"
    path.write_bytes(raw)
    return path


def success_response(
    *,
    video: str,
    model: str,
    prompt: str,
    modality: str = "text",
    aspect_ratio: str = "",
    duration: int = 0,
    provider: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构建统一的成功响应字典。

    ``video`` 可以是 HTTP URL 或绝对文件系统路径。
    ``modality`` 为 ``"text"``（text-to-video）或 ``"image"``（image-to-video）——
    指示实际访问了哪个端点，对诊断有用。
    """
    payload: Dict[str, Any] = {
        "success": True,
        "video": video,
        "model": model,
        "prompt": prompt,
        "modality": modality,
        "aspect_ratio": aspect_ratio,
        "duration": int(duration) if duration else 0,
        "provider": provider,
    }
    if extra:
        for k, v in extra.items():
            payload.setdefault(k, v)
    return payload


def error_response(
    *,
    error: str,
    error_type: str = "provider_error",
    provider: str = "",
    model: str = "",
    prompt: str = "",
    aspect_ratio: str = "",
) -> Dict[str, Any]:
    """构建统一的错误响应字典。"""
    return {
        "success": False,
        "video": None,
        "error": error,
        "error_type": error_type,
        "model": model,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "provider": provider,
    }
