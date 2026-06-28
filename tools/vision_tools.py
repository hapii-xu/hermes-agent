#!/usr/bin/env python3
"""
视觉工具模块

本模块提供基于图片 URL 的视觉分析工具。
使用集中式的辅助视觉路由器，可以选择 OpenRouter、Nous、Codex、
原生 Anthropic 或自定义的 OpenAI 兼容端点。

可用工具：
- vision_analyze_tool：使用自定义提示词分析来自 URL 的图片

功能特性：
- 从 URL 下载图片并转换为 base64，以保证 API 兼容性
- 全面的图像描述
- 基于用户查询的上下文感知分析
- 自动清理临时文件
- 完善的错误处理与校验
- 调试日志支持

用法：
    from vision_tools import vision_analyze_tool
    import asyncio

    # 分析一张图片
    result = await vision_analyze_tool(
        image_url="https://example.com/image.jpg",
        user_prompt="What architectural style is this building?"
    )
"""

import base64
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Awaitable, Dict, Optional
from urllib.parse import urlparse
import httpx
from agent.auxiliary_client import async_call_llm, extract_content_or_reasoning
from hermes_constants import get_hermes_dir
from tools.debug_helpers import DebugSession
from tools.website_policy import check_website_access
import sys

logger = logging.getLogger(__name__)

_debug = DebugSession("vision_tools", env_var="VISION_TOOLS_DEBUG")

# _download_image() 可配置的 HTTP 下载超时时间。
# 与控制 LLM API 调用的 auxiliary.vision.timeout 相互独立。
# 解析顺序：config.yaml 的 auxiliary.vision.download_timeout → 环境变量 → 默认 30 秒。
def _resolve_download_timeout() -> float:
    env_val = os.getenv("HERMES_VISION_DOWNLOAD_TIMEOUT", "").strip()
    if env_val:
        try:
            return float(env_val)
        except ValueError:
            pass
    try:
        from hermes_cli.config import cfg_get, load_config
        cfg = load_config()
        val = cfg_get(cfg, "auxiliary", "vision", "download_timeout")
        if val is not None:
            return float(val)
    except Exception:
        pass
    return 30.0

_VISION_DOWNLOAD_TIMEOUT = _resolve_download_timeout()

# 下载图片文件大小的硬性上限（50 MB）。防止攻击者托管的多 GB 文件
# 或解压炸弹导致 OOM（内存溢出）。
_VISION_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


def _image_url_shape_ok(url: str) -> bool:
    """仅做 HTTP(S) 格式检查（scheme、netloc），不做 DNS 解析。"""
    if not url or not isinstance(url, str):
        return False
    # 基本的 HTTP/HTTPS URL 检查
    if not url.startswith(("http://", "https://")):
        return False
    # 解析以确保至少存在网络位置；仍然允许没有文件扩展名的 URL
    # （例如会重定向到图片的 CDN 端点）。
    parsed = urlparse(url)
    if not parsed.netloc:
        return False
    return True


def _validate_image_url(url: str) -> bool:
    """为同步调用方和测试校验图片 URL（通过同步 DNS 检查防范 SSRF）。"""
    if not _image_url_shape_ok(url):
        return False
    # 屏蔽私有/内部地址，防止 SSRF
    from tools.url_safety import is_safe_url
    return is_safe_url(url)


async def _validate_image_url_async(url: str) -> bool:
    """校验远程图片 URL，且不会在 DNS 解析时阻塞事件循环。"""
    if not _image_url_shape_ok(url):
        return False
    from tools.url_safety import async_is_safe_url
    return await async_is_safe_url(url)


def _detect_image_mime_type(image_path: Path) -> Optional[str]:
    """当文件看起来是受支持的图片时，返回其 MIME 类型。"""
    with image_path.open("rb") as f:
        header = f.read(64)

    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"BM"):
        return "image/bmp"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if image_path.suffix.lower() == ".svg":
        head = image_path.read_text(encoding="utf-8", errors="ignore")[:4096].lower()
        if "<svg" in head:
            return "image/svg+xml"
    return None


def _is_retryable_download_error(error: Exception) -> bool:
    """仅当图片下载失败属于值得重试的瞬时错误时返回 True。

    不可重试（立即失败）：
      - httpx.HTTPStatusError 且状态码为 429 以外的 4xx（404/403/410/...）：
        资源缺失或被禁止访问；重试无法改变这一点。
      - PermissionError：被网站策略 / SSRF 防护拦截。
      - ValueError：图片过大或重定向被拦截——属于确定性错误。

    可重试（瞬时）：
      - httpx 的 429（限流）和 5xx（服务端）错误。
      - 连接/超时/传输错误（httpx.TransportError）以及任何其他未分类的异常，
        这些可能是网络抖动。
    """
    if isinstance(error, (PermissionError, ValueError)):
        return False
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if 400 <= status < 500 and status != 429:
            return False
        return True
    return True


async def _download_image(image_url: str, destination: Path, max_retries: int = 3) -> Path:
    """
    从 URL 下载图片到本地目标路径（异步），带有重试逻辑。

    参数：
        image_url (str)：要下载的图片 URL
        destination (Path)：图片保存的目标路径
        max_retries (int)：最大重试次数（默认：3）

    返回：
        Path：已下载图片的路径

    抛出：
        Exception：如果所有重试后下载仍然失败
    """
    import asyncio

    # 如果父目录不存在则创建
    destination.parent.mkdir(parents=True, exist_ok=True)

    async def _ssrf_redirect_guard(response):
        """对每个重定向目标重新校验，防止基于重定向的 SSRF。

        如果不做这一步，攻击者可以托管一个公开 URL，让它 302 重定向到
        http://169.254.169.254/，从而绕过预检阶段的 is_safe_url 检查。

        必须是 async 的，因为 httpx.AsyncClient 会 await 事件钩子。
        """
        if response.is_redirect and response.next_request:
            redirect_url = str(response.next_request.url)
            from tools.url_safety import async_is_safe_url
            if not await async_is_safe_url(redirect_url):
                raise ValueError(
                    f"Blocked redirect to private/internal address: {redirect_url}"
                )

    last_error = None
    for attempt in range(max_retries):
        try:
            blocked = check_website_access(image_url)
            if blocked:
                raise PermissionError(blocked["message"])

            # 使用异步 httpx 带上合适的请求头下载图片
            # 开启 follow_redirects 以处理会重定向的图片 CDN（例如 Imgur、Picsum）
            # SSRF 防护：event_hooks 会对每个重定向目标校验是否属于私有 IP 段
            async with httpx.AsyncClient(
                timeout=_VISION_DOWNLOAD_TIMEOUT,
                follow_redirects=True,
                event_hooks={"response": [_ssrf_redirect_guard]},
            ) as client:
                response = await client.get(
                    image_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "image/*,*/*;q=0.8",
                    },
                )
                response.raise_for_status()

                # 通过 Content-Length 头尽早拒绝过大的图片。
                cl = response.headers.get("content-length")
                if cl and int(cl) > _VISION_MAX_DOWNLOAD_BYTES:
                    raise ValueError(
                        f"Image too large ({int(cl)} bytes, max {_VISION_MAX_DOWNLOAD_BYTES})"
                    )

                final_url = str(response.url)
                blocked = check_website_access(final_url)
                if blocked:
                    raise PermissionError(blocked["message"])
                
                # 保存图片内容（再次校验实际大小）
                body = response.content
                if len(body) > _VISION_MAX_DOWNLOAD_BYTES:
                    raise ValueError(
                        f"Image too large ({len(body)} bytes, max {_VISION_MAX_DOWNLOAD_BYTES})"
                    )
                destination.write_bytes(body)
            
            return destination
        except Exception as e:
            last_error = e
            # 错误分类感知的重试：只重试瞬时错误。4xx 客户端错误
            # （404/403/410 等）重试永远不会成功——资源不存在或无权访问——
            # 因此用 2s/4s/8s 的退避消耗 3 次尝试只会徒增延迟。429（限流）
            # 和 5xx 仍然可重试。PermissionError（策略拦截）和 ValueError
            # （过大 / SSRF 重定向）同样是终态错误。
            if not _is_retryable_download_error(e) or attempt >= max_retries - 1:
                logger.error(
                    "Image download failed after %s attempt(s): %s",
                    attempt + 1,
                    str(e)[:100],
                    exc_info=True,
                )
                raise
            wait_time = 2 ** (attempt + 1)  # 2s, 4s, 8s
            logger.warning("Image download failed (attempt %s/%s): %s", attempt + 1, max_retries, str(e)[:50])
            logger.warning("Retrying in %ss...", wait_time)
            await asyncio.sleep(wait_time)

    # 循环总会在成功时返回，或在最后一次/不可重试的尝试时重新抛出异常，
    # 因此到达这里意味着 max_retries 为非正数。
    if last_error is not None:
        raise last_error
    raise RuntimeError(
        f"_download_image exited retry loop without attempting (max_retries={max_retries})"
    )


def _determine_mime_type(image_path: Path) -> str:
    """
    根据文件扩展名判断图片的 MIME 类型。

    参数：
        image_path (Path)：图片文件的路径

    返回：
        str：MIME 类型（未知时默认为 image/jpeg）
    """
    extension = image_path.suffix.lower()
    mime_types = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.bmp': 'image/bmp',
        '.webp': 'image/webp',
        '.svg': 'image/svg+xml'
    }
    return mime_types.get(extension, 'image/jpeg')


def _image_to_base64_data_url(image_path: Path, mime_type: Optional[str] = None) -> str:
    """
    将图片文件转换为 base64 编码的 data URL。

    参数：
        image_path (Path)：图片文件的路径
        mime_type (Optional[str])：图片的 MIME 类型（为 None 时自动检测）

    返回：
        str：base64 编码的 data URL（例如 "data:image/jpeg;base64,..."）
    """
    # 以字节方式读取图片
    data = image_path.read_bytes()

    # 编码为 base64
    encoded = base64.b64encode(data).decode("ascii")

    # 确定 MIME 类型
    mime = mime_type or _determine_mime_type(image_path)

    # 创建 data URL
    data_url = f"data:{mime};base64,{encoded}"

    return data_url


# 视觉 API 载荷的绝对硬性上限（20 MB）——超过此大小，没有主流
# 提供商会接受该图片，我们会直接拒绝。
_MAX_BASE64_BYTES = 20 * 1024 * 1024

# 主动嵌入上限（4 MB）。这是我们在把图片嵌入到对话历史之前，将图片
# 缩小到的目标大小，与 20 MB 的硬性上限无关。Anthropic 的单张 base64
# 图片限制是 5 MB；一旦一张过大的图片被固化到历史中（例如某个视觉
# 工具结果），它会在后续每一轮被重新发送，并用一个重试也无法清除的
# 400 错误永久卡死会话（这些坏字节是不可变的历史）。在嵌入时进行限制
# ——并在 5 MB 以下留有余量——是唯一持久的修复方式。此目标值与
# agent.conversation_compression 中失败后缩小到的目标一致，这样无论
# 是主动缩小还是被动缩小，行为都保持一致。
_EMBED_TARGET_BYTES = 4 * 1024 * 1024

# 主动嵌入的尺寸上限（像素，最长边）。Anthropic 在 5 MB 字节上限之外，
# 独立地强制执行 8000px 每边的上限——一张高大的整页截图可能远低于
# 5 MB，却远超 8000px（例如 1200×12000 仅 0.06 MB），因此上面仅基于
# 字节的嵌入检查会让它未经缩小就溜进不可变历史，导致会话因一个不可
# 重试的 400 错误而卡死。我们将上限设为 7900（在 8000 以下留有余量），
# 这样主动缩小就会在嵌入之前把高大的小字节图片缩小。
_EMBED_MAX_DIMENSION = 7900

# API 失败时自动缩小的目标大小（5 MB）。当提供商拒绝某张图片后，
# 我们会缩小到此目标大小并重试一次。
_RESIZE_TARGET_BYTES = 5 * 1024 * 1024


def _is_image_size_error(error: Exception) -> bool:
    """检测某个 API 错误是否与图片或载荷大小相关。"""
    err_str = str(error).lower()
    return any(hint in err_str for hint in (
        "too large", "payload", "413", "content_too_large",
        "request_too_large", "image_url", "invalid_request",
        "exceeds", "size limit",
    ))


def _image_exceeds_dimension(image_path: Path, max_dimension: int) -> bool:
    """当图片的最长边超过 ``max_dimension`` 像素时返回 True。

    Anthropic 在 5 MB 字节上限之外，独立地强制执行 8000px 每边的上限，
    因此一张高大的小字节截图可能通过所有字节检查，却触发一个不可重试的
    400 错误。当 Pillow 不可用或文件无法作为图片读取时返回 False（不强制
    缩小）——基于字节的检查仍然适用，我们绝不希望因为缺少一个软依赖而
    破坏嵌入流程。
    """
    try:
        from PIL import Image as _PILImage
        with _PILImage.open(image_path) as _img:
            return max(_img.size) > max_dimension
    except Exception:
        return False


def _resize_image_for_vision(image_path: Path, mime_type: Optional[str] = None,
                              max_base64_bytes: int = _RESIZE_TARGET_BYTES,
                              max_dimension: Optional[int] = None) -> str:
    """将图片转换为 base64 data URL，如果过大则自动缩小。

    优先使用 Pillow 逐步缩小过大的图片。如果未安装 Pillow 或缩小后仍
    超出限制，则回退到原始字节，由调用方处理大小检查。

    参数：
        max_dimension：设置后，最长边超过此像素数的图片会被强制缩小，
            即使它们在字节预算之内。Anthropic 在 5 MB 字节上限之外，
            独立地强制执行 8000px 每边的上限。

    返回 base64 data URL 字符串。
    """
    # 快速估算文件大小：base64 会膨胀约 4/3，再加上 data URL 头部开销。
    # 如果 Pillow 可以直接缩小，就跳过昂贵的完整读取 + 编码。
    file_size = image_path.stat().st_size
    estimated_b64 = (file_size * 4) // 3 + 100  # ~header overhead
    needs_resize_for_bytes = estimated_b64 > max_base64_bytes

    # 即使字节没问题，也要检查像素尺寸。
    needs_resize_for_dims = False
    if max_dimension is not None:
        try:
            from PIL import Image as _PILQuick
            with _PILQuick.open(image_path) as _quick_img:
                if max(_quick_img.size) > max_dimension:
                    needs_resize_for_dims = True
        except Exception:
            pass  # 无法检查；下面的 Pillow 路径会处理或跳过

    if not needs_resize_for_bytes and not needs_resize_for_dims:
        # 足够小——直接编码。
        data_url = _image_to_base64_data_url(image_path, mime_type=mime_type)
        if len(data_url) <= max_base64_bytes:
            return data_url
    else:
        data_url = None  # 推迟完整编码；先尝试 Pillow 缩小

    # 尝试用 Pillow 自动缩小（软依赖）
    try:
        from PIL import Image
        import io as _io
    except ImportError:
        # Pillow 是一个可按需安装的软依赖。尝试尽力安装（遵循
        # security.allow_lazy_installs；如果被禁用或离线则为空操作），
        # 然后重新导入。如果仍然无法导入，则回退到原始字节，让调用方
        # 抛出大小错误。
        try:
            from tools.lazy_deps import ensure as _ensure_dep
            # prompt=False：在会话过程中绝不弹出阻塞式的 input() 提示。
            # 在交互式 CLI 下，prompt_toolkit 占用了 stdin，因此裸的
            # input() 会卡死终端（#40490）。安装本身已受
            # security.allow_lazy_installs 控制，所以走到这里是用户主动选择。
            _ensure_dep("tool.vision", prompt=False)
            from PIL import Image
            import io as _io
        except Exception:
            logger.info("Pillow not installed — cannot auto-resize oversized image")
            if data_url is None:
                data_url = _image_to_base64_data_url(image_path, mime_type=mime_type)
            return data_url  # 调用方会抛出大小错误

    logger.info("Image file is %.1f MB (estimated base64 %.1f MB, limit %.1f MB, max_dimension=%s), auto-resizing...",
                file_size / (1024 * 1024), estimated_b64 / (1024 * 1024),
                max_base64_bytes / (1024 * 1024), max_dimension)

    mime = mime_type or _determine_mime_type(image_path)
    # 选择输出格式：照片用 JPEG（更小），需要透明度时用 PNG
    pil_format = "PNG" if mime == "image/png" else "JPEG"
    out_mime = "image/png" if pil_format == "PNG" else "image/jpeg"

    try:
        img = Image.open(image_path)
    except Exception as exc:
        logger.info("Pillow cannot open image for resizing: %s", exc)
        if data_url is None:
            data_url = _image_to_base64_data_url(image_path, mime_type=mime_type)
        return data_url  # 回落到调用方的大小检查
    # 为 JPEG 输出将 RGBA 转换为 RGB
    if pil_format == "JPEG" and img.mode in {"RGBA", "P"}:
        img = img.convert("RGB")

    # 策略：不断将尺寸减半，直到 base64 装得下且像素尺寸也在限制之内，
    # 最多 4 轮。
    # 对于 JPEG，还会在每个尺寸步骤尝试降低质量。
    # 对于 PNG，质量无关紧要——只有缩小尺寸才有效。
    quality_steps = (85, 70, 50) if pil_format == "JPEG" else (None,)
    prev_dims = (img.width, img.height)
    candidate = None  # 会在第一次循环迭代时被赋值

    def _dims_ok(w: int, h: int) -> bool:
        """当两个像素尺寸都在限制之内时返回 True。"""
        if max_dimension is None:
            return True
        return max(w, h) <= max_dimension

    for attempt in range(5):
        if attempt > 0:
            # 按比例缩放：将较长边减半，并缩放较短边以保持宽高比（最小尺寸 64）。
            scale = 0.5
            new_w = max(int(img.width * scale), 64)
            new_h = max(int(img.height * scale), 64)
            # 从触碰到下限的那个维度重新推导缩放比例，
            # 使两个轴按相同比例缩小。
            if new_w == 64 and img.width > 0:
                effective_scale = 64 / img.width
                new_h = max(int(img.height * effective_scale), 64)
            elif new_h == 64 and img.height > 0:
                effective_scale = 64 / img.height
                new_w = max(int(img.width * effective_scale), 64)
            # 如果尺寸无法进一步缩小则停止
            if (new_w, new_h) == prev_dims:
                break
            img = img.resize((new_w, new_h), Image.LANCZOS)
            prev_dims = (new_w, new_h)
            logger.info("Resized to %dx%d (attempt %d)", new_w, new_h, attempt)

        for q in quality_steps:
            buf = _io.BytesIO()
            save_kwargs = {"format": pil_format}
            if q is not None:
                save_kwargs["quality"] = q
            img.save(buf, **save_kwargs)
            encoded = base64.b64encode(buf.getvalue()).decode("ascii")
            candidate = f"data:{out_mime};base64,{encoded}"
            if len(candidate) <= max_base64_bytes and _dims_ok(img.width, img.height):
                logger.info("Auto-resized image fits: %.1f MB (quality=%s, %dx%d)",
                            len(candidate) / (1024 * 1024), q,
                            img.width, img.height)
                return candidate

    # 如果仍然无法缩小到足够小，则返回最佳尝试结果，
    # 让调用方决定
    if candidate is not None:
        logger.warning("Auto-resize could not fit image under %.1f MB (best: %.1f MB)",
                       max_base64_bytes / (1024 * 1024), len(candidate) / (1024 * 1024))
        return candidate

    # 不应到达这里，但作为兜底回退到完整编码
    return data_url or _image_to_base64_data_url(image_path, mime_type=mime_type)


# ---------------------------------------------------------------------------
# 原生快速路径：当当前主模型支持原生视觉时，短路辅助 LLM。
# 我们不再请求一个独立的 LLM 来描述图片并返回文本，而是加载图片、
# 进行 base64 编码，并返回一个多模态工具结果封装。agent 循环会把该封装
# 解包成 `tool` 角色上的 OpenAI 风格 content 列表；provider 适配器
# （anthropic、codex_responses、chat_completions）将其转换为 Anthropic 的
# tool_result image 块 / Responses 的 input_image / OpenAI 的 image_url 工具
# content。随后主模型在其下一轮直接“看到”像素。
# ---------------------------------------------------------------------------


def _supports_media_in_tool_results(provider: str, model: str) -> bool:
    """判断给定的 provider+model 组合是否接受工具结果消息中的图片内容。

    目前覆盖的提供商（依据 2026 年 4 月核实的规范文档）：

      * Anthropic Messages API（``anthropic`` provider，以及代理 Claude 的
        聚合器——``openrouter``、``nous``、``vertex``、``bedrock``）：
        ``tool_result`` 块接受 ``image`` content 块。
      * OpenAI Chat Completions：tool 消息接受带有 ``image_url`` 部分的数组
        content。
      * OpenAI Responses（``openai-codex``）：``function_call_output.output``
        接受 ``input_text``/``input_image`` 项的数组。
      * Gemini 3（以及通过聚合器代理）：支持多模态工具结果。旧版 Gemini 不支持。

    对于未知 / 遗留 provider，我们保守地返回 False——调用方会回退到遗留的
    aux-LLM 文本路径。当 provider 的 ``ProviderProfile`` 声明了
    ``supports_vision=True`` 时，此检查会放宽。
    """
    if not isinstance(provider, str):
        return False
    p = provider.strip().lower()
    if not p:
        return False

    # 路由到多个厂商的聚合器——假设其支持，因为使用这些聚合器的用户
    # 通常使用的是具备视觉能力的前沿模型。回退到文本对他们来说是一种
    # 退化。
    _AGGREGATORS = {
        "openrouter", "nous", "vertex", "bedrock", "anthropic-vertex",
        "google-vertex",
    }
    if p in _AGGREGATORS:
        return True

    # 原生 Anthropic
    if p in {"anthropic", "claude", "anthropic-direct"}:
        return True

    # OpenAI Chat Completions 和 Responses
    if p in {"openai", "openai-chat", "openai-codex", "azure-openai"}:
        return True

    # Gemini——依据模型名判断；旧版 Gemini 变体不支持多模态
    # functionResponse。Gemini 3.x 支持。
    if p in {"google", "gemini", "google-gemini", "google-vertex-gemini"}:
        if not isinstance(model, str):
            return False
        m = model.strip().lower()
        if "gemini-3" in m or "gemini-pro-3" in m or "gemini-flash-3" in m:
            return True
        return False

    # 检查 provider 注册的 profile 中的 supports_vision 标志。
    # 这覆盖了上面硬编码列表中没有的、具备视觉能力的 provider，例如
    # xiaomi、minimax 等。
    try:
        from providers import get_provider_profile
        profile = get_provider_profile(p)
        if profile is not None and profile.supports_vision:
            return True
    except Exception:
        pass

    # 其他具备视觉能力的 provider 栈。保守默认值：False。
    # 随着我们逐一经验性地核实每个 provider 的工具结果多模态支持，
    # 在此添加显式条目。
    return False


def _should_use_native_vision_fast_path() -> bool:
    """判断视觉工具是否应直接把图片附加给主模型，而不是路由到辅助视觉 LLM。

    当图片路由解析结果为 ``native``，并且 provider 已知接受工具结果中的图片，
    或者用户通过 ``model.supports_vision`` 配置覆盖显式声明该模型具备视觉能力时，
    返回 True。该覆盖是针对不在静态白名单中的自定义/本地 provider 的逃生通道。
    尽力而为：任何解析失败都返回 False，使调用方回退到遗留的 aux-LLM 路径。
    """
    try:
        from agent.auxiliary_client import _read_main_provider, _read_main_model
        from agent.image_routing import decide_image_input_mode, _lookup_supports_vision
        from hermes_cli.config import load_config

        provider = _read_main_provider()
        model = _read_main_model()
        cfg = load_config()
        if decide_image_input_mode(provider, model, cfg) != "native":
            return False
        return (
            _supports_media_in_tool_results(provider, model)
            or _lookup_supports_vision(provider, model, cfg) is True
        )
    except Exception as exc:
        logger.debug("Native vision fast-path check failed: %s", exc)
        return False


def _build_native_vision_tool_result(
    image_url: str,
    question: str,
    image_data_url: str,
    image_size_bytes: int,
) -> Dict[str, Any]:
    """构建由快速路径返回的多模态工具结果封装。

    结构：
      {
        "_multimodal": True,
        "content": [
          {"type": "text", "text": "<简短说明 + 用户的问题>"},
          {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
        ],
        "text_summary": "<纯文本回退>",
        "meta": {"image_url": ..., "size_bytes": N},
      }

    文本部分的存在有两个原因：(1) 既然像素已在上下文中，它给模型一个
    可立即执行的指令；(2) 不支持多模态工具结果的 provider 可以回退到
    ``text_summary``。
    """
    # 工具结果的文本部分刻意保持精简。模型在上下文中已经有用户的原始问题；
    # 这里只是确认图片现在可见，并提醒它被问到了什么。
    text_part = (
        "Image loaded into your context — you can see it natively now. "
        "Use your built-in vision to answer the user."
    )
    if isinstance(question, str) and question.strip():
        text_part += f"\n\nQuestion: {question.strip()}"

    summary = (
        f"Image attached natively for the main model "
        f"({image_size_bytes / 1024:.1f} KB). "
        "Answer using built-in vision."
    )

    return {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": text_part},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ],
        "text_summary": summary,
        "meta": {
            "image_url": image_url[:200],
            "size_bytes": image_size_bytes,
            "native_vision": True,
        },
    }


async def _vision_analyze_native(
    image_url: str,
    question: str,
) -> Any:
    """具备视觉能力的主模型的快速路径。

    加载图片（本地文件或远程 URL），进行 base64 编码，并返回一个多模态
    工具结果封装。agent 循环会将其解包；provider 适配器将其序列化为每个
    后端所需的“带图片的工具结果”格式。

    返回：
        成功时返回 ``_multimodal`` 封装字典。
        失败时返回 JSON 错误字符串（与现有的工具结果约定一致，以便 agent
        循环正常显示错误）。
    """
    if not isinstance(image_url, str) or not image_url.strip():
        return tool_error("image_url is required", success=False)

    temp_image_path: Optional[Path] = None
    should_cleanup = False
    try:
        from tools.interrupt import is_interrupted
        if is_interrupted():
            return tool_error("Interrupted", success=False)

        # 解析图片来源（与 vision_analyze_tool 的逻辑完全镜像，
        # 以保证行为一致）。
        resolved_url = image_url
        if resolved_url.startswith("file://"):
            resolved_url = resolved_url[len("file://"):]
        local_path = Path(os.path.expanduser(resolved_url))

        if local_path.is_file():
            temp_image_path = local_path
            should_cleanup = False
        elif await _validate_image_url_async(image_url):
            blocked = check_website_access(image_url)
            if blocked:
                return tool_error(blocked["message"], success=False)
            temp_dir = get_hermes_dir("cache/vision", "temp_vision_images")
            temp_image_path = temp_dir / f"temp_image_{uuid.uuid4()}.jpg"
            await _download_image(image_url, temp_image_path)
            should_cleanup = True
        else:
            return tool_error(
                "Invalid image source. Provide an HTTP/HTTPS URL or a "
                "valid local file path.",
                success=False,
            )

        image_size_bytes = temp_image_path.stat().st_size
        detected_mime_type = _detect_image_mime_type(temp_image_path)
        if not detected_mime_type:
            return tool_error(
                "Only real image files are supported for vision analysis.",
                success=False,
            )

        image_data_url = _image_to_base64_data_url(
            temp_image_path, mime_type=detected_mime_type,
        )

        # 主动嵌入上限：这张图片会被固化到对话历史中，并在后续每一轮重新发送。
        # Anthropic 会以 400 拒绝任何超过 5 MB 或每边超过 8000px 的单张
        # base64 图片，而由于历史是不可变的，一个过大的嵌入会永久卡死会话——
        # 重试无法清除已存在于请求中的字节（或像素）。只要载荷超过任一限制，
        # 就缩小到嵌入目标（4 MB / 7900px，在两个上限之下留有余量），
        # 而不仅仅是在 20 MB 硬性上限处。
        _over_bytes = len(image_data_url) > _EMBED_TARGET_BYTES
        _over_dims = _image_exceeds_dimension(temp_image_path, _EMBED_MAX_DIMENSION)
        if _over_bytes or _over_dims:
            image_data_url = _resize_image_for_vision(
                temp_image_path, mime_type=detected_mime_type,
                max_base64_bytes=_EMBED_TARGET_BYTES,
                max_dimension=_EMBED_MAX_DIMENSION,
            )
            # 如果即使缩小也无法低于绝对硬性上限，
            # 那就无能为力了——拒绝而不是嵌入一个会卡死会话的载荷。
            if len(image_data_url) > _MAX_BASE64_BYTES:
                return tool_error(
                    f"Image too large for vision API: base64 payload is "
                    f"{len(image_data_url) / (1024 * 1024):.1f} MB "
                    f"(limit {_MAX_BASE64_BYTES / (1024 * 1024):.0f} MB) "
                    f"even after resizing. Install Pillow "
                    f"(`pip install Pillow`) for better auto-resize, "
                    f"or compress the image manually.",
                    success=False,
                )

        return _build_native_vision_tool_result(
            image_url=image_url,
            question=question,
            image_data_url=image_data_url,
            image_size_bytes=image_size_bytes,
        )

    except Exception as exc:
        logger.warning("Native vision fast path failed: %s", exc)
        return tool_error(f"Native vision failed: {exc}", success=False)
    finally:
        # 只删除我们创建的临时文件——绝不删除用户提供的路径。
        if should_cleanup and temp_image_path is not None:
            try:
                if temp_image_path.exists():
                    temp_image_path.unlink()
            except Exception:
                pass


async def vision_analyze_tool(
    image_url: str,
    user_prompt: str,
    model: str = None,
) -> str:
    """
    使用视觉 AI 分析来自 URL 或本地文件路径的图片。

    本工具接受 HTTP/HTTPS URL 或本地文件路径。对于 URL，会先下载图片。
    两种情况下，图片都会被转换为 base64，并通过 OpenRouter API 使用
    Gemini 3 Flash Preview 进行处理。

    user_prompt 参数应由调用方（通常是 model_tools.py）预先格式化，以同时
    包含完整的描述请求和具体问题。

    参数：
        image_url (str)：要分析的图片 URL 或本地文件路径。
                         接受 http://、https:// URL 或绝对/相对文件路径。
        user_prompt (str)：为视觉模型预先格式化的提示词
        model (str)：要使用的视觉模型（默认：google/gemini-3-flash-preview）

    返回：
        str：包含分析结果的 JSON 字符串，结构如下：
             {
                 "success": bool,
                 "analysis": str (为 None 时默认为错误信息)
             }

    抛出：
        Exception：如果下载失败、分析失败或未设置 API key

    说明：
        - 对于 URL，临时图片会存放在 $HERMES_HOME/cache/vision/ 下并会被清理
        - 对于本地文件路径，文件会被直接使用且不会被删除
        - 支持常见图片格式（JPEG、PNG、GIF、WebP 等）
    """
    if not isinstance(user_prompt, str):
        user_prompt = str(user_prompt) if user_prompt is not None else ""
    debug_call_data = {
        "parameters": {
            "image_url": image_url,
            "user_prompt": user_prompt[:200] + "..." if len(user_prompt) > 200 else user_prompt,
            "model": model
        },
        "error": None,
        "success": False,
        "analysis_length": 0,
        "model_used": model,
        "image_size_bytes": 0
    }
    
    temp_image_path = None
    # 跟踪处理完成后是否应清理该文件。
    # 本地文件（例如来自图片缓存）不应被删除。
    should_cleanup = True
    detected_mime_type = None
    
    try:
        from tools.interrupt import is_interrupted
        if is_interrupted():
            return tool_error("Interrupted", success=False)

        logger.info("Analyzing image: %s", image_url[:60])
        logger.info("User prompt: %s", user_prompt[:100])
        
        # 判断这是本地文件路径还是远程 URL
        # 去掉 file:// 前缀，使 file URI 能解析为本地路径。
        resolved_url = image_url
        if resolved_url.startswith("file://"):
            resolved_url = resolved_url[len("file://"):]
        local_path = Path(os.path.expanduser(resolved_url))
        if local_path.is_file():
            # 本地文件路径（例如来自平台图片缓存）——跳过下载
            logger.info("Using local image file: %s", image_url)
            temp_image_path = local_path
            should_cleanup = False  # 不删除缓存/本地文件
        elif await _validate_image_url_async(image_url):
            # 远程 URL——下载到临时位置
            blocked = check_website_access(image_url)
            if blocked:
                raise PermissionError(blocked["message"])
            logger.info("Downloading image from URL...")
            temp_dir = get_hermes_dir("cache/vision", "temp_vision_images")
            temp_image_path = temp_dir / f"temp_image_{uuid.uuid4()}.jpg"
            await _download_image(image_url, temp_image_path)
            should_cleanup = True
        else:
            raise ValueError(
                "Invalid image source. Provide an HTTP/HTTPS URL or a valid local file path."
            )
        
        # 获取图片文件大小用于日志记录
        image_size_bytes = temp_image_path.stat().st_size
        image_size_kb = image_size_bytes / 1024
        logger.info("Image ready (%.1f KB)", image_size_kb)

        detected_mime_type = _detect_image_mime_type(temp_image_path)
        if not detected_mime_type:
            raise ValueError("Only real image files are supported for vision analysis.")
        
        # 将图片转换为 base64——先以全分辨率发送。
        # 如果提供商因其过大而拒绝，我们会自动缩小并重试。
        logger.info("Converting image to base64...")
        image_data_url = _image_to_base64_data_url(temp_image_path, mime_type=detected_mime_type)
        data_size_kb = len(image_data_url) / 1024
        logger.info("Image converted to base64 (%.1f KB)", data_size_kb)

        # 硬性限制（20 MB）——没有提供商会接受这么大的载荷。
        if len(image_data_url) > _MAX_BASE64_BYTES:
            # 在放弃之前尝试缩小到 5 MB。
            image_data_url = _resize_image_for_vision(
                temp_image_path, mime_type=detected_mime_type)
            if len(image_data_url) > _MAX_BASE64_BYTES:
                raise ValueError(
                    f"Image too large for vision API: base64 payload is "
                    f"{len(image_data_url) / (1024 * 1024):.1f} MB "
                    f"(limit {_MAX_BASE64_BYTES / (1024 * 1024):.0f} MB) "
                    f"even after resizing. "
                    f"Install Pillow (`pip install Pillow`) for better auto-resize, "
                    f"or compress the image manually."
                )

        debug_call_data["image_size_bytes"] = image_size_bytes
        
        # 按原样使用提示词（model_tools.py 现在负责完整的描述格式化）
        comprehensive_prompt = user_prompt

        # 用 base64 编码的图片构造消息
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": comprehensive_prompt
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": image_data_url
                        }
                    }
                ]
            }
        ]
        
        logger.info("Processing image with vision model...")
        
        # 通过集中式路由器调用视觉 API。
        # 从 config.yaml 读取超时时间（auxiliary.vision.timeout），默认 120 秒。
        # 本地视觉模型（llama.cpp、ollama）可能需要远超 30 秒。
        vision_timeout = 120.0
        vision_temperature = 0.1
        try:
            from hermes_cli.config import cfg_get, load_config
            _cfg = load_config()
            _vision_cfg = cfg_get(_cfg, "auxiliary", "vision", default={})
            _vt = _vision_cfg.get("timeout")
            if _vt is not None:
                vision_timeout = float(_vt)
            _vtemp = _vision_cfg.get("temperature")
            if _vtemp is not None:
                vision_temperature = float(_vtemp)
        except Exception:
            pass
        call_kwargs = {
            "task": "vision",
            "messages": messages,
            "temperature": vision_temperature,
            "max_tokens": 2000,
            "timeout": vision_timeout,
        }
        if model:
            call_kwargs["model"] = model
        # 先尝试全尺寸图片；如果因大小被拒绝，则缩小并重试。
        try:
            response = await async_call_llm(**call_kwargs)
        except Exception as _api_err:
            if (_is_image_size_error(_api_err)
                    and len(image_data_url) > _RESIZE_TARGET_BYTES):
                logger.info(
                    "API rejected image (%.1f MB, likely too large); "
                    "auto-resizing to ~%.0f MB and retrying...",
                    len(image_data_url) / (1024 * 1024),
                    _RESIZE_TARGET_BYTES / (1024 * 1024),
                )
                image_data_url = _resize_image_for_vision(
                    temp_image_path, mime_type=detected_mime_type)
                messages[0]["content"][1]["image_url"]["url"] = image_data_url
                response = await async_call_llm(**call_kwargs)
            else:
                raise
        
        # 提取分析结果——如果 content 为空则回退到 reasoning
        analysis = extract_content_or_reasoning(response)

        # 内容为空时（仅含 reasoning 的响应）重试一次
        if not analysis:
            logger.warning("Vision LLM returned empty content, retrying once")
            response = await async_call_llm(**call_kwargs)
            analysis = extract_content_or_reasoning(response)

        analysis_length = len(analysis)
        
        logger.info("Image analysis completed (%s characters)", analysis_length)
        
        # 准备成功响应
        result = {
            "success": True,
            "analysis": analysis or "There was a problem with the request and the image could not be analyzed."
        }
        
        debug_call_data["success"] = True
        debug_call_data["analysis_length"] = analysis_length
        
        # 记录调试信息
        _debug.log_call("vision_analyze_tool", debug_call_data)
        _debug.save()
        
        return json.dumps(result, indent=2, ensure_ascii=False)
        
    except Exception as e:
        error_msg = f"Error analyzing image: {str(e)}"
        logger.error("%s", error_msg, exc_info=True)
        
        # 检测视觉能力相关的错误——给模型一个清晰的信息，
        # 以便它能通知用户，而不是一个晦涩的 API 错误。
        err_str = str(e).lower()
        if any(hint in err_str for hint in (
            "402", "insufficient", "payment required", "credits", "billing",
        )):
            analysis = (
                "Insufficient credits or payment required. Please top up your "
                f"API provider account and try again. Error: {e}"
            )
        elif any(hint in err_str for hint in (
            "does not support", "not support image",
            "content_policy", "multimodal",
            "unrecognized request argument", "image input",
        )):
            analysis = (
                f"{model} does not support vision or our request was not "
                f"accepted by the server. Error: {e}"
            )
        elif "invalid_request" in err_str or "image_url" in err_str:
            analysis = (
                "The vision API rejected the image. This can happen when the "
                "image is in an unsupported format, corrupted, or still too "
                "large after auto-resize. Try a smaller JPEG/PNG and retry. "
                f"Error: {e}"
            )
        else:
            analysis = (
                "There was a problem with the request and the image could not "
                f"be analyzed. Error: {e}"
            )
        
        # 准备错误响应
        result = {
            "success": False,
            "error": error_msg,
            "analysis": analysis,
        }
        
        debug_call_data["error"] = error_msg
        _debug.log_call("vision_analyze_tool", debug_call_data)
        _debug.save()
        
        return json.dumps(result, indent=2, ensure_ascii=False)
    
    finally:
        # 清理临时图片文件（但不清理本地/缓存文件）
        if should_cleanup and temp_image_path and temp_image_path.exists():
            try:
                temp_image_path.unlink()
                logger.debug("Cleaned up temporary image file")
            except Exception as cleanup_error:
                logger.warning(
                    "Could not delete temporary file: %s", cleanup_error, exc_info=True
                )


def check_vision_requirements() -> bool:
    """检查已配置的运行时视觉路径能否解析出一个客户端。

    镜像了 ``call_llm(task="vision")`` 在运行时实际使用的回退链：
    先尝试显式的 ``auxiliary.vision.provider``（如果有），如果失败再用
    自动链（主 provider → openrouter → nous）。如果没有自动回退这一步，
    那么每当显式 provider 名称无法解析时，该工具就会从模型的工具列表中
    消失，即使自动链本可以处理该请求（issue #31179）。
    """
    try:
        from agent.auxiliary_client import resolve_vision_provider_client
    except ImportError:
        return False
    try:
        _provider, client, _model = resolve_vision_provider_client()
        if client is not None:
            return True
        # 与 call_llm 在配置的 provider 无法解析时执行的“auto”回退相同。
        _provider, client, _model = resolve_vision_provider_client(provider="auto")
        return client is not None
    except Exception:
        return False



if __name__ == "__main__":
    """
    直接运行时的简单测试/演示
    """
    print("👁️ Vision Tools Module")
    print("=" * 40)

    # 检查视觉模型是否可用
    api_available = check_vision_requirements()
    
    if not api_available:
        print("❌ No auxiliary vision model available")
        print("Configure a supported multimodal backend (OpenRouter, Nous, Codex, Anthropic, or a custom OpenAI-compatible endpoint).")
        sys.exit(1)
    else:
        print("✅ Vision model available")
    
    print("🛠️ Vision tools ready for use!")
    
    # 显示调试模式状态
    if _debug.active:
        print(f"🐛 Debug mode ENABLED - Session ID: {_debug.session_id}")
        print(f"   Debug logs will be saved to: ./logs/vision_tools_debug_{_debug.session_id}.json")
    else:
        print("🐛 Debug mode disabled (set VISION_TOOLS_DEBUG=true to enable)")
    
    print("\nBasic usage:")
    print("  from vision_tools import vision_analyze_tool")
    print("  import asyncio")
    print("")
    print("  async def main():")
    print("      result = await vision_analyze_tool(")
    print("          image_url='https://example.com/image.jpg',")
    print("          user_prompt='What do you see in this image?'")
    print("      )")
    print("      print(result)")
    print("  asyncio.run(main())")
    
    print("\nExample prompts:")
    print("  - 'What architectural style is this building?'")
    print("  - 'Describe the emotions and mood in this image'")
    print("  - 'What text can you read in this image?'")
    print("  - 'Identify any safety hazards visible'")
    print("  - 'What products or brands are shown?'")
    
    print("\nDebug mode:")
    print("  # Enable debug logging")
    print("  export VISION_TOOLS_DEBUG=true")
    print("  # Debug logs capture all vision analysis calls and results")
    print("  # Logs saved to: ./logs/vision_tools_debug_UUID.json")


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
from tools.registry import registry, tool_error

VISION_ANALYZE_SCHEMA = {
    "name": "vision_analyze",
    "description": (
        "Load an image into the conversation so you can see it. Accepts a "
        "URL, local file path, or data URL. When your active model has "
        "native vision, the image is attached to your context directly "
        "and you read the pixels yourself on the next turn — call this "
        "any time the user references an image (filepath in their message, "
        "URL in tool output, screenshot from the browser, etc.). For "
        "non-vision models, falls back to an auxiliary vision model that "
        "returns a text description."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "image_url": {
                "type": "string",
                "description": "Image URL (http/https), local file path, or data: URL to load."
            },
            "question": {
                "type": "string",
                "description": "Your specific question or request about the image. Optional context the model uses on the next turn after seeing the image."
            }
        },
        "required": ["image_url", "question"]
    }
}


def _handle_vision_analyze(args: Dict[str, Any], **kw: Any) -> Awaitable[str]:
    image_url = args.get("image_url", "")
    question = args.get("question", "")

    # 快速路径：当当前主模型的图片原生路由生效时（provider 接受工具结果中的
    # 图片，或用户设置了 model.supports_vision 覆盖），短路辅助 LLM，并将图片
    # 字节作为多模态工具结果封装返回。主模型在其下一轮直接看到像素——
    # 没有 aux 调用、没有信息损失、没有额外延迟。
    if _should_use_native_vision_fast_path():
        logger.info("vision_analyze: native fast path")
        return _vision_analyze_native(image_url, question)

    # 遗留路径：aux LLM 描述图片，我们返回其文本。
    full_prompt = (
        "Fully describe and explain everything about this image, then answer the "
        f"following question:\n\n{question}"
    )
    model = os.getenv("AUXILIARY_VISION_MODEL", "").strip() or None
    return vision_analyze_tool(image_url, full_prompt, model)


registry.register(
    name="vision_analyze",
    toolset="vision",
    schema=VISION_ANALYZE_SCHEMA,
    handler=_handle_vision_analyze,
    check_fn=check_vision_requirements,
    is_async=True,
    emoji="👁️",
)


# ---------------------------------------------------------------------------
# 视频分析工具
# ---------------------------------------------------------------------------

# 扩展名 → MIME。avi/mkv 回退为 mp4。
_VIDEO_MIME_TYPES = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/mov",
    ".avi": "video/mp4",
    ".mkv": "video/mp4",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
}

_MAX_VIDEO_BASE64_BYTES = 50 * 1024 * 1024  # 50 MB 硬性上限
_VIDEO_SIZE_WARN_BYTES = 20 * 1024 * 1024


def _detect_video_mime_type(video_path: Path) -> Optional[str]:
    """根据文件扩展名返回视频 MIME 类型，不支持时返回 None。"""
    ext = video_path.suffix.lower()
    return _VIDEO_MIME_TYPES.get(ext)


def _video_to_base64_data_url(video_path: Path, mime_type: Optional[str] = None) -> str:
    """将视频文件转换为 base64 编码的 data URL。"""
    data = video_path.read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    mime = mime_type or _VIDEO_MIME_TYPES.get(video_path.suffix.lower(), "video/mp4")
    return f"data:{mime};base64,{encoded}"


async def _download_video(video_url: str, destination: Path, max_retries: int = 3) -> Path:
    """从 URL 下载视频，带 SSRF 防护和重试。"""
    import asyncio

    destination.parent.mkdir(parents=True, exist_ok=True)

    async def _ssrf_redirect_guard(response):
        if response.is_redirect and response.next_request:
            redirect_url = str(response.next_request.url)
            from tools.url_safety import async_is_safe_url
            if not await async_is_safe_url(redirect_url):
                raise ValueError(
                    f"Blocked redirect to private/internal address: {redirect_url}"
                )

    last_error = None
    for attempt in range(max_retries):
        try:
            blocked = check_website_access(video_url)
            if blocked:
                raise PermissionError(blocked["message"])

            async with httpx.AsyncClient(
                timeout=60.0,
                follow_redirects=True,
                event_hooks={"response": [_ssrf_redirect_guard]},
            ) as client:
                response = await client.get(
                    video_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "video/*,*/*;q=0.8",
                    },
                )
                response.raise_for_status()

                cl = response.headers.get("content-length")
                if cl and int(cl) > _MAX_VIDEO_BASE64_BYTES:
                    raise ValueError(
                        f"Video too large ({int(cl)} bytes, max {_MAX_VIDEO_BASE64_BYTES})"
                    )

                final_url = str(response.url)
                blocked = check_website_access(final_url)
                if blocked:
                    raise PermissionError(blocked["message"])

                body = response.content
                if len(body) > _MAX_VIDEO_BASE64_BYTES:
                    raise ValueError(
                        f"Video too large ({len(body)} bytes, max {_MAX_VIDEO_BASE64_BYTES})"
                    )
                destination.write_bytes(body)

            return destination
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                wait_time = 2 ** (attempt + 1)
                logger.warning("Video download failed (attempt %s/%s): %s", attempt + 1, max_retries, str(e)[:50])
                await asyncio.sleep(wait_time)
            else:
                logger.error(
                    "Video download failed after %s attempts: %s",
                    max_retries, str(e)[:100], exc_info=True,
                )

    if last_error is None:
        raise RuntimeError(
            f"_download_video exited retry loop without attempting (max_retries={max_retries})"
        )
    raise last_error


async def video_analyze_tool(
    video_url: str,
    user_prompt: str,
    model: str = None,
) -> str:
    """通过多模态 LLM 分析视频。返回 JSON {success, analysis}。"""
    if not isinstance(user_prompt, str):
        user_prompt = str(user_prompt) if user_prompt is not None else ""
    debug_call_data = {
        "parameters": {
            "video_url": video_url,
            "user_prompt": user_prompt[:200] + "..." if len(user_prompt) > 200 else user_prompt,
            "model": model,
        },
        "error": None,
        "success": False,
        "analysis_length": 0,
        "model_used": model,
        "video_size_bytes": 0,
    }

    temp_video_path = None
    should_cleanup = True

    try:
        from tools.interrupt import is_interrupted
        if is_interrupted():
            return tool_error("Interrupted", success=False)

        logger.info("Analyzing video: %s", video_url[:60])
        logger.info("User prompt: %s", user_prompt[:100])

        # 解析本地路径还是远程 URL
        resolved_url = video_url
        if resolved_url.startswith("file://"):
            resolved_url = resolved_url[len("file://"):]
        local_path = Path(os.path.expanduser(resolved_url))

        if local_path.is_file():
            logger.info("Using local video file: %s", video_url)
            temp_video_path = local_path
            should_cleanup = False
        elif await _validate_image_url_async(video_url):
            blocked = check_website_access(video_url)
            if blocked:
                raise PermissionError(blocked["message"])
            temp_dir = get_hermes_dir("cache/video", "temp_video_files")
            temp_video_path = temp_dir / f"temp_video_{uuid.uuid4()}.mp4"
            await _download_video(video_url, temp_video_path)
            should_cleanup = True
        else:
            raise ValueError(
                "Invalid video source. Provide an HTTP/HTTPS URL or a valid local file path."
            )

        video_size_bytes = temp_video_path.stat().st_size
        video_size_mb = video_size_bytes / (1024 * 1024)
        logger.info("Video ready (%.1f MB)", video_size_mb)

        detected_mime = _detect_video_mime_type(temp_video_path)
        if not detected_mime:
            raise ValueError(
                f"Unsupported video format: '{temp_video_path.suffix}'. "
                f"Supported: {', '.join(sorted(_VIDEO_MIME_TYPES.keys()))}"
            )

        if video_size_bytes > _VIDEO_SIZE_WARN_BYTES:
            logger.warning("Video is %.1f MB — may be slow or rejected", video_size_mb)

        video_data_url = _video_to_base64_data_url(temp_video_path, mime_type=detected_mime)
        data_size_mb = len(video_data_url) / (1024 * 1024)

        if len(video_data_url) > _MAX_VIDEO_BASE64_BYTES:
            raise ValueError(
                f"Video too large for API: base64 payload is {data_size_mb:.1f} MB "
                f"(limit {_MAX_VIDEO_BASE64_BYTES / (1024 * 1024):.0f} MB). "
                f"Compress or trim the video and retry."
            )

        debug_call_data["video_size_bytes"] = video_size_bytes

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": user_prompt,
                    },
                    {
                        "type": "video_url",
                        "video_url": {
                            "url": video_data_url,
                        },
                    },
                ],
            }
        ]

        vision_timeout = 180.0
        vision_temperature = 0.1
        try:
            from hermes_cli.config import cfg_get, load_config
            _cfg = load_config()
            _vision_cfg = cfg_get(_cfg, "auxiliary", "vision", default={})
            _vt = _vision_cfg.get("timeout")
            if _vt is not None:
                vision_timeout = max(float(_vt), 180.0)
            _vtemp = _vision_cfg.get("temperature")
            if _vtemp is not None:
                vision_temperature = float(_vtemp)
        except Exception:
            pass

        call_kwargs = {
            "task": "vision",
            "messages": messages,
            "temperature": vision_temperature,
            "max_tokens": 4000,
            "timeout": vision_timeout,
        }
        if model:
            call_kwargs["model"] = model

        response = await async_call_llm(**call_kwargs)
        analysis = extract_content_or_reasoning(response)

        if not analysis:
            logger.warning("Empty video response, retrying once")
            response = await async_call_llm(**call_kwargs)
            analysis = extract_content_or_reasoning(response)

        analysis_length = len(analysis) if analysis else 0
        logger.info("Video analysis completed (%s characters)", analysis_length)

        result = {
            "success": True,
            "analysis": analysis or "There was a problem with the request and the video could not be analyzed.",
        }

        debug_call_data["success"] = True
        debug_call_data["analysis_length"] = analysis_length
        _debug.log_call("video_analyze_tool", debug_call_data)
        _debug.save()

        return json.dumps(result, indent=2, ensure_ascii=False)

    except Exception as e:
        error_msg = f"Error analyzing video: {str(e)}"
        logger.error("%s", error_msg, exc_info=True)

        err_str = str(e).lower()
        if any(hint in err_str for hint in (
            "402", "insufficient", "payment required", "credits", "billing",
        )):
            analysis = (
                "Insufficient credits or payment required. Please top up your "
                f"API provider account and try again. Error: {e}"
            )
        elif any(hint in err_str for hint in (
            "does not support", "not support video",
            "content_policy", "multimodal",
            "unrecognized request argument", "video input",
            "video_url",
        )):
            analysis = (
                f"The model does not support video analysis or the request was "
                f"rejected. Ensure you're using a video-capable model "
                f"(e.g. google/gemini-2.5-flash). Error: {e}"
            )
        elif any(hint in err_str for hint in (
            "too large", "payload", "413", "content_too_large",
            "request_too_large", "exceeds", "size limit",
        )):
            analysis = (
                "The video is too large for the API. Try compressing or trimming "
                f"the video (max ~50 MB). Error: {e}"
            )
        else:
            analysis = (
                "There was a problem with the request and the video could not "
                f"be analyzed. Error: {e}"
            )

        result = {
            "success": False,
            "error": error_msg,
            "analysis": analysis,
        }

        debug_call_data["error"] = error_msg
        _debug.log_call("video_analyze_tool", debug_call_data)
        _debug.save()

        return json.dumps(result, indent=2, ensure_ascii=False)

    finally:
        if should_cleanup and temp_video_path and temp_video_path.exists():
            try:
                temp_video_path.unlink()
                logger.debug("Cleaned up temporary video file")
            except Exception as cleanup_error:
                logger.warning(
                    "Could not delete temporary file: %s", cleanup_error, exc_info=True
                )


VIDEO_ANALYZE_SCHEMA = {
    "name": "video_analyze",
    "description": (
        "Analyze a video from a URL or local file path using a multimodal AI model. "
        "Sends the video to a video-capable model (e.g. Gemini) for understanding. "
        "Use this for video files — for images, use vision_analyze instead. "
        "Supports mp4, webm, mov, avi, mkv, mpeg formats. "
        "Note: large videos (>20 MB) may be slow; max ~50 MB."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "video_url": {
                "type": "string",
                "description": "Video URL (http/https) or local file path to analyze.",
            },
            "question": {
                "type": "string",
                "description": "Your specific question about the video. The AI will describe what happens in the video and answer your question.",
            },
        },
        "required": ["video_url", "question"],
    },
}


def _handle_video_analyze(args: Dict[str, Any], **kw: Any) -> Awaitable[str]:
    video_url = args.get("video_url", "")
    question = args.get("question", "")
    full_prompt = (
        "Fully describe and explain everything happening in this video, "
        "including visual content, motion, audio cues, text overlays, and scene "
        f"transitions. Then answer the following question:\n\n{question}"
    )
    model = os.getenv("AUXILIARY_VIDEO_MODEL", "").strip() or os.getenv("AUXILIARY_VISION_MODEL", "").strip() or None
    return video_analyze_tool(video_url, full_prompt, model)


registry.register(
    name="video_analyze",
    toolset="video",
    schema=VIDEO_ANALYZE_SCHEMA,
    handler=_handle_video_analyze,
    check_fn=check_vision_requirements,
    is_async=True,
    emoji="🎬",
)
