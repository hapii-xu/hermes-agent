"""
Telegram 贴纸描述缓存。

当用户发送贴纸时，我们通过 vision 工具对其进行描述，并按 file_unique_id
缓存描述结果，这样就不必在每次发送时都重新分析同一张贴纸图片。
描述保持简短（1-2 句）。

缓存位置：~/.hermes/sticker_cache.json
"""

import json
import os
import tempfile
import time
from typing import Optional

from hermes_cli.config import get_hermes_home


CACHE_PATH = get_hermes_home() / "sticker_cache.json"

# 用于描述贴纸的 vision prompt —— 保持简短以节省 token
STICKER_VISION_PROMPT = (
    "Describe this sticker in 1-2 sentences. Focus on what it depicts -- "
    "character, action, emotion. Be concise and objective."
)


def _load_cache() -> dict:
    """从磁盘加载贴纸缓存。"""
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    """以原子方式将贴纸缓存写入磁盘。"""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(CACHE_PATH.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(CACHE_PATH))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_cached_description(file_unique_id: str) -> Optional[dict]:
    """
    查找已缓存的贴纸描述。

    返回（Returns）：
        包含键 {description, emoji, set_name, cached_at} 的 dict，或 None。
    """
    cache = _load_cache()
    return cache.get(file_unique_id)


def cache_sticker_description(
    file_unique_id: str,
    description: str,
    emoji: str = "",
    set_name: str = "",
) -> None:
    """
    将贴纸描述存入缓存。

    参数（Args）：
        file_unique_id: Telegram 稳定的贴纸标识符。
        description:    由 vision 生成的描述文本。
        emoji:          关联的 emoji（例如 "😀"）。
        set_name:       贴纸包名称（若可用）。
    """
    cache = _load_cache()
    cache[file_unique_id] = {
        "description": description,
        "emoji": emoji,
        "set_name": set_name,
        "cached_at": time.time(),
    }
    _save_cache(cache)


def build_sticker_injection(
    description: str,
    emoji: str = "",
    set_name: str = "",
) -> str:
    """
    为贴纸描述构造“热情风格”的注入文本。

    返回形如下面的字符串：
      [The user sent a sticker 😀 from "MyPack"~ It shows: "A cat waving" (=^.w.^=)]
    """
    context = ""
    if set_name and emoji:
        context = f" {emoji} from \"{set_name}\""
    elif emoji:
        context = f" {emoji}"

    return f"[The user sent a sticker{context}~ It shows: \"{description}\" (=^.w.^=)]"


def build_animated_sticker_injection(emoji: str = "") -> str:
    """
    为我们无法分析的动画/视频贴纸构造注入文本。
    """
    if emoji:
        return (
            f"[The user sent an animated sticker {emoji}~ "
            f"I can't see animated ones yet, but the emoji suggests: {emoji}]"
        )
    return "[The user sent an animated sticker~ I can't see animated ones yet]"
