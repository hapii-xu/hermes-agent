#!/usr/bin/env python3
"""
文本转语音（TTS）工具模块

内置 TTS 提供商：
- Edge TTS（默认，免费，无需 API key）：Microsoft Edge 神经网络语音
- ElevenLabs（高端）：高质量语音，需要 ELEVENLABS_API_KEY
- OpenAI TTS：音质良好，需要 OPENAI_API_KEY
- MiniMax TTS：高质量并支持声音克隆，需要 MINIMAX_API_KEY
- Mistral（Voxtral TTS）：多语言，原生 Opus，需要 MISTRAL_API_KEY
- Google Gemini TTS：可控，30 种预置语音，需要 GEMINI_API_KEY
- xAI TTS：Grok 语音，使用 xAI Grok OAuth 凭据或 XAI_API_KEY
- NeuTTS（本地，免费，无需 API key）：通过 neutts 实现设备端 TTS
- KittenTTS（本地，免费，无需 API key）：设备端 25MB 模型
- Piper（本地，免费，无需 API key）：OHF-Voice/piper1-gpl 神经网络 VITS，支持 44 种语言

自定义命令提供商：
- 用户可以在 ``~/.hermes/config.yaml`` 的 ``tts.providers.<name>`` 下声明
  任意数量的 ``type: command`` 命名提供商。Hermes 把输入文本写入一个临时
  文件，然后运行配置好的 shell 命令，该命令必须在指定路径生成音频文件。
  参见 ``website/docs/user-guide/features/tts.md`` 的 Local Command 章节。

输出格式：
- Opus（.ogg）用于 Telegram 语音气泡（Edge TTS 需要 ffmpeg）
- MP3（.mp3）用于其他所有场景（CLI、Discord、WhatsApp）

配置从 ~/.hermes/config.yaml 的 'tts:' 键下加载。
用户选择提供商和语音；模型只负责发送文本。

用法：
    from tools.tts_tool import text_to_speech_tool, check_tts_requirements

    result = text_to_speech_tool(text="Hello world")
"""

import asyncio
import base64
import datetime
import json
import logging
import os
import queue
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Callable, Dict, Any, Optional
from urllib.parse import urljoin

from hermes_constants import display_hermes_home

logger = logging.getLogger(__name__)
def get_env_value(name, default=None):
    """通过实时的 config 模块读取环境变量的值。

    测试可能会在本模块被导入之前 monkeypatch 并随后恢复
    ``hermes_cli.config.get_env_value``。在调用时才解析这个辅助函数，避免
    TTS 在测试进程的剩余生命周期里持有一个过期的已导入函数。
    """
    try:
        from hermes_cli.config import get_env_value as _get_env_value
    except ImportError:
        return os.getenv(name, default)
    value = _get_env_value(name)
    return default if value is None else value
from tools.managed_tool_gateway import resolve_managed_tool_gateway
from tools.tool_backend_helpers import (
    managed_nous_tools_enabled,
    nous_tool_gateway_unavailable_message,
    prefers_gateway,
    resolve_openai_audio_api_key,
)
from tools.xai_http import hermes_xai_user_agent

# ---------------------------------------------------------------------------
# 惰性导入——只有在真正使用时才导入各提供商，避免在无头环境
# （SSH、Docker、WSL、无 PortAudio）中崩溃。
# ---------------------------------------------------------------------------

def _import_edge_tts():
    """惰性导入 edge_tts。返回该模块，失败则抛出 ImportError。"""
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("tts.edge", prompt=False)
    except ImportError:
        pass
    except Exception as e:
        raise ImportError(str(e))
    import edge_tts
    return edge_tts

def _import_elevenlabs():
    """惰性导入 ElevenLabs 客户端。返回该类，失败则抛出 ImportError。

    会先调用 :func:`tools.lazy_deps.ensure`，以便在用户选择了 ElevenLabs
    作为 TTS 提供商、但从未运行过 post-setup 钩子（例如直接编辑 config.yaml
    启用）时，按需安装该 SDK。惰性安装失败时抛出 ``ImportError``，以保证
    现有调用方的错误处理路径仍然可用。
    """
    try:
        from tools.lazy_deps import FeatureUnavailable, ensure
        ensure("tts.elevenlabs", prompt=False)
    except ImportError:
        # lazy_deps 模块本身缺失——继续走原始 import，
        # 让较早的代码路径仍然得到一个干净的 ImportError。
        pass
    except Exception as e:  # FeatureUnavailable 或任何意外错误
        raise ImportError(str(e))
    from elevenlabs.client import ElevenLabs
    return ElevenLabs

def _import_openai_client():
    """惰性导入 OpenAI 客户端。返回该类，失败则抛出 ImportError。"""
    from openai import OpenAI as OpenAIClient
    return OpenAIClient

def _import_mistral_client():
    """惰性导入 Mistral 客户端。返回该类，失败则抛出 ImportError。

    会先调用 :func:`tools.lazy_deps.ensure`，以便在用户选择了 Mistral
    作为 STT/TTS 提供商、但从未运行过 post-setup 钩子（例如直接编辑
    config.yaml 启用）时，按需安装 ``mistralai`` SDK。与 ElevenLabs 的
    惰性导入路径一致。
    """
    try:
        from tools.lazy_deps import ensure
        ensure("tts.mistral", prompt=False)
    except ImportError:
        pass
    except Exception as e:  # FeatureUnavailable 或任何意外错误
        raise ImportError(str(e))
    from mistralai.client import Mistral
    return Mistral

def _import_sounddevice():
    """惰性导入 sounddevice。返回该模块，失败则抛出 ImportError/OSError。"""
    import sounddevice as sd
    return sd


def _import_kittentts():
    """惰性导入 KittenTTS。返回该类，失败则抛出 ImportError。"""
    from kittentts import KittenTTS
    return KittenTTS


def _import_piper():
    """惰性导入 Piper。返回 PiperVoice 类，失败则抛出 ImportError。

    Piper 是一个可选的、完全本地的神经网络 TTS 引擎（Home Assistant /
    Open Home Foundation）。``pip install piper-tts`` 提供跨平台 wheel
    （Linux / macOS / Windows，x86_64 + ARM64），内嵌 espeak-ng。
    语音模型（.onnx + .onnx.json）在首次使用时下载。
    """
    from piper import PiperVoice
    return PiperVoice


# ===========================================================================
# 默认值
# ===========================================================================
DEFAULT_PROVIDER = "edge"
DEFAULT_EDGE_VOICE = "en-US-AriaNeural"
DEFAULT_ELEVENLABS_VOICE_ID = "pNInz6obpgDQGcFmaJgB"  # Adam
DEFAULT_ELEVENLABS_MODEL_ID = "eleven_multilingual_v2"
DEFAULT_ELEVENLABS_STREAMING_MODEL_ID = "eleven_flash_v2_5"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini-tts"
DEFAULT_KITTENTTS_MODEL = "KittenML/kitten-tts-nano-0.8-int8"  # 25MB
DEFAULT_KITTENTTS_VOICE = "Jasper"
DEFAULT_PIPER_VOICE = "en_US-lessac-medium"  # 体积/质量均衡
DEFAULT_OPENAI_VOICE = "alloy"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MINIMAX_MODEL = "speech-02-hd"
DEFAULT_MINIMAX_VOICE_ID = "English_expressive_narrator"
DEFAULT_MINIMAX_BASE_URL = "https://api.minimax.io/v1/t2a_v2"
DEFAULT_MISTRAL_TTS_MODEL = "voxtral-mini-tts-2603"
DEFAULT_MISTRAL_TTS_VOICE_ID = "c69964a6-ab8b-4f8a-9465-ec0925096ec8"  # Paul - 中性
DEFAULT_XAI_VOICE_ID = "eve"
DEFAULT_XAI_LANGUAGE = "en"
DEFAULT_XAI_SAMPLE_RATE = 24000
DEFAULT_XAI_BIT_RATE = 128000
DEFAULT_XAI_AUTO_SPEECH_TAGS = False
DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"
# xAI TTS 的 `speed` 取值范围为 0.7..1.5；1.0 是 API 默认值（省略 => 使用默认值）。
DEFAULT_XAI_SPEED_MIN = 0.7
DEFAULT_XAI_SPEED_MAX = 1.5
DEFAULT_XAI_SPEED_DEFAULT = 1.0
# xAI TTS 的 `optimize_streaming_latency` 取值为 0、1 或 2；0（最佳音质）是
# API 默认值（省略 => 使用默认值）。大于 0 的值会用音质换取更快的首音频时间。
DEFAULT_XAI_OPTIMIZE_STREAMING_LATENCY_DEFAULT = 0
DEFAULT_GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"
DEFAULT_GEMINI_TTS_VOICE = "Kore"
DEFAULT_GEMINI_TTS_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_AUDIO_TAGS = False
GEMINI_AUDIO_TAG_REWRITE_TASK = "tts_audio_tags"
# Gemini TTS 的 PCM 输出规格（由 API 固定）
GEMINI_TTS_SAMPLE_RATE = 24000
GEMINI_TTS_CHANNELS = 1
GEMINI_TTS_SAMPLE_WIDTH = 2  # 16-bit PCM（L16）

def _get_default_output_dir() -> str:
    from hermes_constants import get_hermes_dir
    return str(get_hermes_dir("cache/audio", "audio_cache"))

DEFAULT_OUTPUT_DIR = _get_default_output_dir()

# ---------------------------------------------------------------------------
# 各提供商的输入字符数上限（来自各提供商的官方文档）。
# 用一个统一的全局上限是错误的：OpenAI 是 4096，xAI 是 15k，MiniMax 是 10k，
# ElevenLabs 随模型而变（5k / 10k / 30k / 40k），Gemini 有 32k token 的
# 上下文窗口。用户可以在 config.yaml 中通过 ``tts.<provider>.max_text_length``
# 覆盖其中任意一项。
# ---------------------------------------------------------------------------
PROVIDER_MAX_TEXT_LENGTH: Dict[str, int] = {
    "edge": 5000,         # edge-tts 同步处理的实际上限
    "openai": 4096,       # https://platform.openai.com/docs/guides/text-to-speech
    "xai": 15000,         # https://docs.x.ai/developers/model-capabilities/audio/text-to-speech
    "minimax": 10000,     # https://platform.minimax.io/docs/api-reference/speech-t2a-http（同步）
    "mistral": 4000,      # 保守值；官方未公布单次请求上限
    "gemini": 32000,      # Gemini TTS 有 32k token 的上下文窗口；字符上限偏保守
    "elevenlabs": 10000,  # 当按模型查找无法解析时（multilingual_v2）的兜底值
    "neutts": 2000,       # 本地模型，长文本音质会下降
    "kittentts": 2000,    # 本地 25MB 模型
    "piper": 5000,        # 本地 VITS 模型，基于音素；实际可用上限
}

# ElevenLabs 的上限随 model_id 不同而变化。https://elevenlabs.io/docs/overview/models
ELEVENLABS_MODEL_MAX_TEXT_LENGTH: Dict[str, int] = {
    "eleven_v3": 5000,
    "eleven_ttv_v3": 5000,
    "eleven_multilingual_v2": 10000,
    "eleven_multilingual_v1": 10000,
    "eleven_english_sts_v2": 10000,
    "eleven_english_sts_v1": 10000,
    "eleven_flash_v2": 30000,
    "eleven_flash_v2_5": 40000,
}


def _config_bool(value: Any, default: bool = False) -> bool:
    """把常见的 YAML/环境变量布尔写法强制转换为 bool，不会把随机字符串误判为 true。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    return default

# 当提供商完全无法识别时的最终兜底值。
FALLBACK_MAX_TEXT_LENGTH = 4000

# 向后兼容别名。新代码请使用 ``_resolve_max_text_length()``。
MAX_TEXT_LENGTH = FALLBACK_MAX_TEXT_LENGTH


def _resolve_max_text_length(
    provider: Optional[str],
    tts_config: Optional[Dict[str, Any]] = None,
) -> int:
    """返回 *provider* 的输入字符上限。

    解析顺序：
      1. ``tts.<provider>.max_text_length``（config.yaml 中的用户覆盖值）
      2. 用户声明的命令型提供商使用 ``tts.providers.<provider>.max_text_length``
      3. ElevenLabs 按模型查找的表（以配置的 ``model_id`` 为键）
      4. ``PROVIDER_MAX_TEXT_LENGTH`` 默认值
      5. 当提供商是没有显式上限的命令型用户提供商时，使用
         ``DEFAULT_COMMAND_TTS_MAX_TEXT_LENGTH``
      6. ``FALLBACK_MAX_TEXT_LENGTH``（4000）

    非正数或非整数的覆盖值会回落到默认值，这样即使配置出错也不会
    意外地彻底关闭截断。
    """
    if not provider:
        return FALLBACK_MAX_TEXT_LENGTH
    key = provider.lower().strip()
    cfg = tts_config or {}

    # 位于 tts.<provider>.max_text_length 的内置风格覆盖值优先级最高，
    # 与历史行为保持一致。
    prov_cfg = cfg.get(key) if isinstance(cfg.get(key), dict) else {}
    override = prov_cfg.get("max_text_length") if prov_cfg else None
    if isinstance(override, bool):
        override = None
    if isinstance(override, int) and override > 0:
        return override

    if key == "elevenlabs":
        model_id = (prov_cfg or {}).get("model_id") or DEFAULT_ELEVENLABS_MODEL_ID
        mapped = ELEVENLABS_MODEL_MAX_TEXT_LENGTH.get(str(model_id).strip())
        if mapped:
            return mapped

    if key in PROVIDER_MAX_TEXT_LENGTH:
        return PROVIDER_MAX_TEXT_LENGTH[key]

    # 用户声明的命令型提供商（位于 tts.providers.<name> 下）
    if key not in BUILTIN_TTS_PROVIDERS:
        named = _get_named_provider_config(cfg, key)
        if _is_command_provider_config(named):
            named_override = named.get("max_text_length")
            if isinstance(named_override, bool):
                named_override = None
            if isinstance(named_override, int) and named_override > 0:
                return named_override
            return DEFAULT_COMMAND_TTS_MAX_TEXT_LENGTH

    return FALLBACK_MAX_TEXT_LENGTH


# ===========================================================================
# 配置加载器——从 ~/.hermes/config.yaml 读取 tts: 段
# ===========================================================================
def _load_tts_config() -> Dict[str, Any]:
    """
    从 ~/.hermes/config.yaml 加载 TTS 配置。

    返回包含提供商设置的字典。任何缺失的字段都会回落到默认值。
    """
    try:
        from hermes_cli.config import load_config
        config = load_config()
        return config.get("tts", {})
    except ImportError:
        logger.debug("hermes_cli.config not available, using default TTS config")
        return {}
    except Exception as e:
        logger.warning("Failed to load TTS config: %s", e, exc_info=True)
        return {}


def _get_provider(tts_config: Dict[str, Any]) -> str:
    """获取已配置的 TTS 提供商名称。"""
    return (tts_config.get("provider") or DEFAULT_PROVIDER).lower().strip()


# ===========================================================================
# 自定义命令型提供商（位于 tts.providers.<name> 下的 type: command）
# ===========================================================================
#
# 用户可以在内置提供商之外声明任意数量的命令型提供商，这样就能把任意本地
# CLI（Piper、VoxCPM、Kokoro CLIs、自定义声音克隆脚本等）接入 Hermes，
# 而无需改动任何 Python 代码。配置形如：
#
#     tts:
#       provider: piper-en
#       providers:
#         piper-en:
#           type: command
#           command: "piper -m ~/model.onnx -f {output_path} < {input_path}"
#           output_format: wav
#
# Hermes 把输入文本写入一个临时的 UTF-8 文件，对命令做占位符替换后运行，
# 再读取命令写到 ``{output_path}`` 的音频文件。支持的占位符有：
# ``{input_path}``、``{text_path}``（input_path 的别名）、``{output_path}``、
# ``{format}``、``{voice}``、``{model}``、``{speed}``。要表示字面量大括号，
# 请使用 ``{{`` / ``}}``。
#
# 内置提供商名称总是优先于 ``tts.providers`` 下同名条目，因此用户配置
# 不会无声地遮蔽 ``edge`` 等内置项。
#
# 占位符的值会根据其所在上下文（裸文本 / 单引号 / 双引号）做 shell 转义，
# 因此带空格的路径也能透明地工作。

# 内置提供商名称。任何不在此集合中的 ``tts.provider`` 值都会被解释为对
# ``tts.providers.<name>`` 的引用。
BUILTIN_TTS_PROVIDERS = frozenset({
    "edge",
    "elevenlabs",
    "openai",
    "minimax",
    "xai",
    "mistral",
    "gemini",
    "neutts",
    "kittentts",
    "piper",
})

DEFAULT_COMMAND_TTS_TIMEOUT_SECONDS = 120
DEFAULT_COMMAND_TTS_OUTPUT_FORMAT = "mp3"
COMMAND_TTS_OUTPUT_FORMATS = frozenset({"mp3", "wav", "ogg", "flac"})
DEFAULT_COMMAND_TTS_MAX_TEXT_LENGTH = 5000


def _get_provider_section(tts_config: Dict[str, Any], name: str) -> Dict[str, Any]:
    """如果某提供商配置块是 dict 则返回它，否则返回空 dict。"""
    if not isinstance(tts_config, dict):
        return {}
    section = tts_config.get(name)
    return section if isinstance(section, dict) else {}


def _get_named_provider_config(
    tts_config: Dict[str, Any],
    name: str,
) -> Dict[str, Any]:
    """返回用户声明的提供商配置字典。

    先查找 ``tts.providers.<name>``（规范位置），再回落到 ``tts.<name>``，
    这样按内置布局来写的用户配置仍然可用。当该提供商未声明时返回空 dict。
    """
    providers = _get_provider_section(tts_config, "providers")
    section = providers.get(name) if isinstance(providers, dict) else None
    if isinstance(section, dict):
        return section
    # 向后兼容：对用户声明的提供商也允许 ``tts.<name>``，
    # 但仅当该名称不是内置项时才生效（这样用户的 ``tts.openai`` 块仍然
    # 表示 OpenAI 提供商，而不是一个自定义命令）。
    if name.lower() not in BUILTIN_TTS_PROVIDERS:
        legacy = _get_provider_section(tts_config, name)
        if legacy:
            return legacy
    return {}


def _is_command_provider_config(config: Dict[str, Any]) -> bool:
    """当 *config* 声明了一个命令型提供商时返回 True。"""
    if not isinstance(config, dict):
        return False
    ptype = str(config.get("type") or "").strip().lower()
    if ptype and ptype != "command":
        return False
    command = config.get("command")
    return isinstance(command, str) and bool(command.strip())


def _resolve_command_provider_config(
    provider: str,
    tts_config: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """当 *provider* 解析为命令型时返回其提供商配置。

    内置提供商名称会被拒绝（它们有原生处理函数）。
    当名称是内置项、未知项或不是命令型时返回 None。
    """
    if not provider:
        return None
    key = provider.lower().strip()
    if key in BUILTIN_TTS_PROVIDERS:
        return None
    config = _get_named_provider_config(tts_config, key)
    if _is_command_provider_config(config):
        return config
    return None


def _dispatch_to_plugin_provider(
    text: str,
    output_path: str,
    provider: str,
    tts_config: Dict[str, Any],
) -> Optional[str]:
    """把调用路由到插件注册的 TTS 提供商，否则返回 None。

    分发成功时返回已写入音频文件的路径，返回 ``None`` 则继续
    回落到下一层解析（内置分发或 Edge TTS 默认值）。

    此处强制执行的解析不变量（对应 issue #30398）：

    1. 内置提供商名称短路处理——绝不会进入插件注册表。调用方负责
       处理 ``edge``/``openai`` 等的 elif 链；本函数会防御性地显式
       拒绝这些名称。
    2. 在 ``tts.providers.<name>: type: command`` 下声明的命令型提供商
       （PR #17843）优先于同名插件。调用方只有在自身的命令型提供商检查
       返回 None 时才会走到这里——我们在此再次校验，这样调用方日后
       的重构也不会无声地破坏这一不变量。
    3. 只有当 ``provider`` 匹配到一个已注册的、``name`` 等于配置值的
       :class:`TTSProvider` 时，才会触发插件分发。未知名称返回 None
       （调用方回落到 Edge 默认值）。

    插件抛出的异常会被捕获并重新抛出——外层 ``text_to_speech_tool``
    的 try/except 会把它们转换成标准错误信封，与命令型提供商失败时的
    上报方式一致。
    """
    if not provider:
        return None
    key = provider.lower().strip()
    if key in BUILTIN_TTS_PROVIDERS:
        return None
    # 纵深防御：命令型提供商检查本应在调用方就短路掉。如果存在同名的
    # 命令配置，则直接退出，让命令路径胜出。
    if _is_command_provider_config(_get_named_provider_config(tts_config, key)):
        return None
    try:
        from agent.tts_registry import get_provider
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
        plugin_provider = get_provider(key)
        if plugin_provider is None:
            # 长生命周期的会话可能在捆绑后端被补丁修复之前、或配置变更之前
            # 就已经发现过插件。在对外暴露「回落」之前，先强制刷新后重试一次。
            # 与 image_gen / browser 分发器的恢复模式一致。
            _ensure_plugins_discovered(force=True)
            plugin_provider = get_provider(key)
    except Exception as exc:  # noqa: BLE001 — 发现失败并非致命错误
        logger.debug("tts plugin dispatch skipped (discovery failed): %s", exc)
        return None
    if plugin_provider is None:
        return None

    # 从 tts_config 解析 voice / model / format——各提供商应把这些全部
    # 视为可选，当传入 None 时回落到各自的默认值（与 ``TTSProvider.synthesize``
    # 上文档化的 ABC 契约一致）。
    voice = tts_config.get("voice") if isinstance(tts_config, dict) else None
    model = tts_config.get("model") if isinstance(tts_config, dict) else None
    speed = tts_config.get("speed") if isinstance(tts_config, dict) else None
    fmt = (
        tts_config.get("output_format", DEFAULT_COMMAND_TTS_OUTPUT_FORMAT)
        if isinstance(tts_config, dict)
        else DEFAULT_COMMAND_TTS_OUTPUT_FORMAT
    )

    logger.info(
        "Generating speech with plugin TTS provider '%s'...", key,
    )
    written = plugin_provider.synthesize(
        text,
        output_path,
        voice=voice if isinstance(voice, str) and voice else None,
        model=model if isinstance(model, str) and model else None,
        speed=float(speed) if isinstance(speed, (int, float)) else None,
        format=str(fmt).lower() if fmt else "mp3",
    )
    # 提供商契约：返回（可能被改写过的）输出路径。
    # 对返回 None 或非字符串的情况做防御性处理——回落到调用方期望的 output_path。
    return written if isinstance(written, str) and written else output_path


def _plugin_provider_is_voice_compatible(provider: str) -> bool:
    """当已注册的插件提供商通过其 ``voice_compatible`` 属性启用了语音气泡
    投递时返回 True。

    防御性处理：任何注册表或属性访问失败都视为 False
    （与命令型提供商路径的安全默认值一致）。
    """
    if not provider:
        return False
    key = provider.lower().strip()
    if key in BUILTIN_TTS_PROVIDERS:
        return False
    try:
        from agent.tts_registry import get_provider

        plugin_provider = get_provider(key)
        if plugin_provider is None:
            return False
        return bool(plugin_provider.voice_compatible)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "tts plugin voice_compatible check failed for '%s': %s", key, exc,
        )
        return False


def _iter_command_providers(tts_config: Dict[str, Any]):
    """为每一个已声明的命令型提供商生成 (name, config) 键值对。"""
    if not isinstance(tts_config, dict):
        return
    providers = _get_provider_section(tts_config, "providers")
    for name, cfg in (providers or {}).items():
        if isinstance(name, str) and name.lower() not in BUILTIN_TTS_PROVIDERS:
            if _is_command_provider_config(cfg):
                yield name, cfg


def _get_command_tts_timeout(config: Dict[str, Any]) -> float:
    """返回以秒为单位的超时时间，无效时回落到默认值。"""
    raw = config.get("timeout", config.get("timeout_seconds", DEFAULT_COMMAND_TTS_TIMEOUT_SECONDS))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(DEFAULT_COMMAND_TTS_TIMEOUT_SECONDS)
    if value <= 0:
        return float(DEFAULT_COMMAND_TTS_TIMEOUT_SECONDS)
    return value


def _get_command_tts_output_format(
    config: Dict[str, Any],
    output_path: Optional[str] = None,
) -> str:
    """返回经过校验的输出格式（mp3/wav/ogg/flac）。"""
    if output_path:
        suffix = Path(output_path).suffix.lower().strip().lstrip(".")
        if suffix in COMMAND_TTS_OUTPUT_FORMATS:
            return suffix
    raw = (
        config.get("format")
        or config.get("output_format")
        or DEFAULT_COMMAND_TTS_OUTPUT_FORMAT
    )
    fmt = str(raw).lower().strip().lstrip(".")
    return fmt if fmt in COMMAND_TTS_OUTPUT_FORMATS else DEFAULT_COMMAND_TTS_OUTPUT_FORMAT


def _is_command_tts_voice_compatible(config: Dict[str, Any]) -> bool:
    """仅当用户显式启用语音投递时才返回 True。"""
    value = config.get("voice_compatible", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _shell_quote_context(command_template: str, position: int) -> Optional[str]:
    """返回在 *position* 之前生效的 shell 引号字符。

    当位于模板的单引号 / 双引号区域内时分别返回 ``"'"`` / ``'"'``，
    处于裸文本上下文时返回 ``None``。
    """
    quote: Optional[str] = None
    escaped = False
    i = 0
    while i < position:
        char = command_template[i]
        if quote == "'":
            if char == "'":
                quote = None
        elif quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = None
        elif char == "'":
            quote = "'"
        elif char == '"':
            quote = '"'
        elif char == "\\":
            i += 1
        i += 1
    return quote


def _quote_command_tts_placeholder(value: str, quote_context: Optional[str]) -> str:
    """根据占位符在 shell 命令模板中的位置，对其值进行转义。"""
    if quote_context == "'":
        return value.replace("'", r"'\''")
    if quote_context == '"':
        return (
            value
            .replace("\\", "\\\\")
            .replace('"', r'\"')
            .replace("$", r"\$")
            .replace("`", r"\`")
        )
    if os.name == "nt":
        return subprocess.list2cmdline([value])
    return shlex.quote(value)


def _render_command_tts_template(
    command_template: str,
    placeholders: Dict[str, str],
) -> str:
    """替换支持的占位符，同时保留字面量 ``{{`` / ``}}``。"""
    names = "|".join(re.escape(name) for name in placeholders)
    pattern = re.compile(
        rf"(?<!\$)(?:\{{\{{(?P<double>{names})\}}\}}|\{{(?P<single>{names})\}})"
    )
    replacements: list[tuple[str, str]] = []

    def replace_match(match: re.Match[str]) -> str:
        name = match.group("double") or match.group("single")
        token = f"__HERMES_TTS_PLACEHOLDER_{len(replacements)}__"
        replacements.append((
            token,
            _quote_command_tts_placeholder(
                placeholders[name],
                _shell_quote_context(command_template, match.start()),
            ),
        ))
        return token

    rendered = pattern.sub(replace_match, command_template)
    rendered = rendered.replace("{{", "{").replace("}}", "}")
    for token, value in replacements:
        rendered = rendered.replace(token, value)
    return rendered


def _terminate_command_tts_process_tree(proc: subprocess.Popen) -> None:
    """尽力终止一个 shell 进程及其所有子进程。"""
    if proc.poll() is not None:
        return

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                stdin=subprocess.DEVNULL,
            )
        except Exception:
            proc.kill()
        return

    import psutil
    try:
        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        parent.terminate()
    except psutil.NoSuchProcess:
        return
    except Exception:
        proc.terminate()

    try:
        proc.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        parent.kill()
    except psutil.NoSuchProcess:
        return
    except Exception:
        proc.kill()


def _run_command_tts(command: str, timeout: float) -> subprocess.CompletedProcess:
    """运行命令型提供商的 shell 命令，并在超时时对整个进程树做清理。"""
    popen_kwargs: Dict[str, Any] = {
        "shell": True,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(command, **popen_kwargs, stdin=subprocess.DEVNULL)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_command_tts_process_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=1)
        except Exception:
            stdout = getattr(exc, "output", None)
            stderr = getattr(exc, "stderr", None)
        raise subprocess.TimeoutExpired(
            command,
            timeout,
            output=stdout,
            stderr=stderr,
        ) from exc

    if proc.returncode:
        raise subprocess.CalledProcessError(
            proc.returncode,
            command,
            output=stdout,
            stderr=stderr,
        )
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


def _configured_command_tts_output_path(path: Path, config: Dict[str, Any]) -> Path:
    """返回一个扩展名与该提供商 output_format 匹配的输出路径。"""
    fmt = _get_command_tts_output_format(config)
    return path.with_suffix(f".{fmt}")


def _generate_command_tts(
    text: str,
    output_path: str,
    provider_name: str,
    config: Dict[str, Any],
    tts_config: Dict[str, Any],
) -> str:
    """通过运行用户配置的 shell 命令来生成语音。

    返回命令写出的音频文件的绝对路径。
    当提供商配置无效时抛出 ``ValueError``；超时 / 非零退出 / 输出为空时
    抛出 ``RuntimeError``。
    """
    command_template = str(config.get("command") or "").strip()
    if not command_template:
        raise ValueError(
            f"tts.providers.{provider_name}.command is not configured"
        )

    output = Path(output_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    timeout = _get_command_tts_timeout(config)
    output_format = _get_command_tts_output_format(config, str(output))
    speed = config.get("speed", tts_config.get("speed", ""))

    with tempfile.TemporaryDirectory() as tmpdir:
        text_path = Path(tmpdir) / "input.txt"
        text_path.write_text(text, encoding="utf-8")

        placeholders = {
            "input_path": str(text_path),
            "text_path": str(text_path),
            "output_path": str(output),
            "format": output_format,
            "voice": str(config.get("voice", "")),
            "model": str(config.get("model", "")),
            "speed": str(speed),
        }
        command = _render_command_tts_template(command_template, placeholders)

        try:
            _run_command_tts(command, timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"TTS provider '{provider_name}' timed out after {timeout:g}s"
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail_parts = []
            if exc.stderr:
                detail_parts.append(f"stderr: {exc.stderr.strip()}")
            if exc.stdout:
                detail_parts.append(f"stdout: {exc.stdout.strip()}")
            detail = "; ".join(detail_parts) or "no command output"
            raise RuntimeError(
                f"TTS provider '{provider_name}' exited with code "
                f"{exc.returncode}: {detail}"
            ) from exc

    if not output.exists() or output.stat().st_size <= 0:
        raise RuntimeError(
            f"TTS provider '{provider_name}' produced no output at {output}"
        )
    return str(output)


def _has_any_command_tts_provider(tts_config: Optional[Dict[str, Any]] = None) -> bool:
    """当配置了任意命令型 TTS 提供商时返回 True。"""
    if tts_config is None:
        tts_config = _load_tts_config()
    for _name, _cfg in _iter_command_providers(tts_config):
        return True
    return False


# ===========================================================================
# ffmpeg Opus 转换（Edge TTS 的 MP3 -> OGG Opus，用于 Telegram）
# ===========================================================================
def _has_ffmpeg() -> bool:
    """检查系统是否可用 ffmpeg。"""
    return shutil.which("ffmpeg") is not None


def _convert_to_opus(mp3_path: str) -> Optional[str]:
    """
    将 MP3 文件转换为 OGG Opus 格式，以便用于 Telegram 语音气泡。

    参数：
        mp3_path: 输入 MP3 文件的路径。

    返回：
        生成的 .ogg 文件路径；转换失败时返回 None。
    """
    if not _has_ffmpeg():
        return None

    ogg_path = mp3_path.rsplit(".", 1)[0] + ".ogg"
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", mp3_path, "-acodec", "libopus",
             "-ac", "1", "-b:a", "64k", "-vbr", "off", ogg_path, "-y"],
            capture_output=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            logger.warning("ffmpeg conversion failed with return code %d: %s", 
                          result.returncode, result.stderr.decode('utf-8', errors='ignore')[:200])
            return None
        if os.path.exists(ogg_path) and os.path.getsize(ogg_path) > 0:
            return ogg_path
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg OGG conversion timed out after 30s")
    except FileNotFoundError:
        logger.warning("ffmpeg not found in PATH")
    except Exception as e:
        logger.warning("ffmpeg OGG conversion failed: %s", e, exc_info=True)
    return None


# ===========================================================================
# 提供商：Edge TTS（免费）
# ===========================================================================
async def _generate_edge_tts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    """
    使用 Edge TTS 生成音频。

    参数：
        text: 要转换的文本。
        output_path: MP3 文件保存位置。
        tts_config: TTS 配置字典。

    返回：
        保存的音频文件路径。
    """
    _edge_tts = _import_edge_tts()
    edge_config = tts_config.get("edge", {})
    voice = edge_config.get("voice", DEFAULT_EDGE_VOICE)
    speed = float(edge_config.get("speed", tts_config.get("speed", 1.0)))

    kwargs = {"voice": voice}
    if speed != 1.0:
        pct = round((speed - 1.0) * 100)
        kwargs["rate"] = f"{pct:+d}%"

    communicate = _edge_tts.Communicate(text, **kwargs)
    await communicate.save(output_path)
    return output_path


# ===========================================================================
# 提供商：ElevenLabs（高端）
# ===========================================================================
def _generate_elevenlabs(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    """
    使用 ElevenLabs 生成音频。

    参数：
        text: 要转换的文本。
        output_path: 音频文件保存位置。
        tts_config: TTS 配置字典。

    返回：
        保存的音频文件路径。
    """
    api_key = (get_env_value("ELEVENLABS_API_KEY") or "")
    if not api_key:
        raise ValueError("ELEVENLABS_API_KEY not set. Get one at https://elevenlabs.io/")

    el_config = tts_config.get("elevenlabs", {})
    voice_id = el_config.get("voice_id", DEFAULT_ELEVENLABS_VOICE_ID)
    model_id = el_config.get("model_id", DEFAULT_ELEVENLABS_MODEL_ID)

    # 根据文件扩展名决定输出格式
    if output_path.endswith(".ogg"):
        output_format = "opus_48000_64"
    else:
        output_format = "mp3_44100_128"

    ElevenLabs = _import_elevenlabs()
    client = ElevenLabs(api_key=api_key)
    audio_generator = client.text_to_speech.convert(
        text=text,
        voice_id=voice_id,
        model_id=model_id,
        output_format=output_format,
    )

    # audio_generator 会产出多个数据块——把它们全部写入
    with open(output_path, "wb") as f:
        for chunk in audio_generator:
            f.write(chunk)

    return output_path


# ===========================================================================
# 提供商：OpenAI TTS
# ===========================================================================
def _generate_openai_tts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    """
    使用 OpenAI TTS 生成音频。

    参数：
        text: 要转换的文本。
        output_path: 音频文件保存位置。
        tts_config: TTS 配置字典。

    返回：
        保存的音频文件路径。
    """
    api_key, base_url = _resolve_openai_audio_client_config()

    oai_config = tts_config.get("openai", {})
    model = oai_config.get("model", DEFAULT_OPENAI_MODEL)
    voice = oai_config.get("voice", DEFAULT_OPENAI_VOICE)
    base_url = oai_config.get("base_url", base_url)
    speed = float(oai_config.get("speed", tts_config.get("speed", 1.0)))

    # 根据扩展名决定响应格式
    if output_path.endswith(".ogg"):
        response_format = "opus"
    else:
        response_format = "mp3"

    OpenAIClient = _import_openai_client()
    client = OpenAIClient(api_key=api_key, base_url=base_url)
    try:
        create_kwargs = {
            "model": model,
            "voice": voice,
            "input": text,
            "response_format": response_format,
            "extra_headers": {"x-idempotency-key": str(uuid.uuid4())},
        }
        if speed != 1.0:
            create_kwargs["speed"] = max(0.25, min(4.0, speed))
        response = client.audio.speech.create(**create_kwargs)

        response.stream_to_file(output_path)
        return output_path
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


# ===========================================================================
# 提供商：xAI TTS
# ===========================================================================
_XAI_INLINE_SPEECH_TAGS = (
    "pause",
    "long-pause",
    "hum-tune",
    "laugh",
    "chuckle",
    "giggle",
    "cry",
    "tsk",
    "tongue-click",
    "lip-smack",
    "breath",
    "inhale",
    "exhale",
    "sigh",
)
_XAI_WRAPPING_SPEECH_TAGS = (
    "soft",
    "whisper",
    "loud",
    "build-intensity",
    "decrease-intensity",
    "higher-pitch",
    "lower-pitch",
    "slow",
    "fast",
    "sing-song",
    "singing",
    "laugh-speak",
    "emphasis",
)
_XAI_SPEECH_TAG_RE = re.compile(
    r"(\[(?:" + "|".join(_XAI_INLINE_SPEECH_TAGS) + r")\]|</?(?:" + "|".join(_XAI_WRAPPING_SPEECH_TAGS) + r")>)",
    flags=re.IGNORECASE,
)
_XAI_FIRST_SENTENCE_RE = re.compile(r"^(.{12,120}?[.!?…])\s+(?=\S)", flags=re.DOTALL)


def _xai_bool_config(value: Any, default: bool = False) -> bool:
    return _config_bool(value, default=default)


def _apply_xai_auto_speech_tags(text: str) -> str:
    """为语音模式回复添加 xAI 语音标签，使其更自然。

    首先应用一次保守的本地转换（在段落之间以及第一句之后插入 [pause]）。
    然后如果结果中没有任何显式的用户/模型语音标签，就请求配置好的辅助
    模型用更丰富的 xAI 支持标签集（笑声、叹气、耳语、轻柔/大声、慢/快
    等）改写这段转写文本，让语音输出听起来更有表现力。辅助模型任何失败
    都会回落到本地的转换结果。
    """
    clean = text.strip()
    if not clean:
        return text

    # 保守的本地一遍处理：只插入停顿。
    local = clean
    local = re.sub(r"\n\s*\n+", " [pause] ", local)
    local = re.sub(r"\s*\n\s*", " ", local)
    if not _XAI_SPEECH_TAG_RE.search(local):
        local = _XAI_FIRST_SENTENCE_RE.sub(r"\1 [pause] ", local, count=1)
    local = re.sub(r"\s{2,}", " ", local).strip()

    # 如果用户/模型已经提供了显式语音标签，则信任它们，
    # 不再做二次改写。
    if _XAI_SPEECH_TAG_RE.search(clean):
        return local

    # 辅助模型改写以获取更丰富的情感标签（与 Gemini 的路径一致）。
    inline = ", ".join(_XAI_INLINE_SPEECH_TAGS)
    wrapping = ", ".join(_XAI_WRAPPING_SPEECH_TAGS)
    system_prompt = (
        "You rewrite transcripts for the xAI /v1/tts endpoint by inserting "
        "expressive speech tags.\n\n"
        "Valid inline tags (use as `[tag]`): " + inline + ".\n"
        "Valid wrapping tags (use as `[tag]...[/tag]`): " + wrapping + ".\n\n"
        "Rules:\n"
        "- Preserve the spoken words, order, and meaning.\n"
        "- Do not add new spoken sentences or remove existing spoken words.\n"
        "- Use inline `[tag]` for short modifiers (laughs, sighs, pause, etc.).\n"
        "- Use wrapping `[tag]...[/tag]` for sustained effects (whisper, soft, slow, fast, loud, etc.).\n"
        "- Do not use angle-bracket tags like `<tag>...</tag>` — xAI uses BBCode-style closing tags with `[/tag]`.\n"
        "- Do not use SSML.\n"
        "- Do not explain or comment.\n"
        "- Return only the tagged TTS script."
    )
    try:
        from agent.auxiliary_client import call_llm

        response = call_llm(
            task="tts_audio_tags",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"TRANSCRIPT TO TAG:\n{local}"},
            ],
            temperature=0.7,
        )
        tagged = _extract_auxiliary_message_content(response).strip()
        # 去掉 markdown 代码围栏（LLM 可能会把回复包在里面）。
        fence = re.fullmatch(r"```(?:[A-Za-z0-9_-]+)?\s*(.*?)\s*```", tagged, flags=re.DOTALL)
        if fence:
            tagged = fence.group(1).strip()
        return tagged or local
    except Exception as exc:
        logger.debug("xAI TTS audio tag rewrite failed; using locally-tagged text: %s", exc)
        return local


def _generate_xai_tts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    """
    使用 xAI TTS 生成音频。

    xAI 提供的是一个专用的 /v1/tts 端点，而不是 OpenAI audio.speech 那种
    API 形态，因此这里实现为一个独立的后端。
    """
    import requests

    from tools.xai_http import resolve_xai_http_credentials

    creds = resolve_xai_http_credentials()
    api_key = str(creds.get("api_key") or "").strip()
    if not api_key:
        raise ValueError("No xAI credentials found. Configure xAI OAuth in `hermes model` or set XAI_API_KEY.")

    xai_config = tts_config.get("xai", {})
    voice_id = str(xai_config.get("voice_id", DEFAULT_XAI_VOICE_ID)).strip() or DEFAULT_XAI_VOICE_ID
    language = str(xai_config.get("language", DEFAULT_XAI_LANGUAGE)).strip() or DEFAULT_XAI_LANGUAGE
    sample_rate = int(xai_config.get("sample_rate", DEFAULT_XAI_SAMPLE_RATE))
    bit_rate = int(xai_config.get("bit_rate", DEFAULT_XAI_BIT_RATE))
    auto_speech_tags = _xai_bool_config(
        xai_config.get("auto_speech_tags", xai_config.get("speech_tags")),
        DEFAULT_XAI_AUTO_SPEECH_TAGS,
    )
    # ``tts.xai.speed`` 覆盖全局的 ``tts.speed``；xAI TTS API 的取值范围是
    # 0.7..1.5（1.0 = 正常语速）。超出范围的值会被截断，这样配置错误的
    # agent 也不会让请求返回 400——API 本来就会拒绝区间外的任何值。
    speed = xai_config.get("speed", tts_config.get("speed"))
    if speed is not None and speed != "":
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            speed = None
    if speed is not None:
        speed = max(DEFAULT_XAI_SPEED_MIN, min(DEFAULT_XAI_SPEED_MAX, speed))
    # ``tts.xai.optimize_streaming_latency`` 取值为 0、1 或 2（xAI 专有；
    # 用分块边界的音质换取更快的首音频时间）。
    optimize_streaming_latency = xai_config.get(
        "optimize_streaming_latency",
        tts_config.get("optimize_streaming_latency"),
    )
    if optimize_streaming_latency is not None and optimize_streaming_latency != "":
        try:
            optimize_streaming_latency = int(optimize_streaming_latency)
        except (TypeError, ValueError):
            optimize_streaming_latency = None
    if optimize_streaming_latency is not None:
        optimize_streaming_latency = max(0, min(2, optimize_streaming_latency))
    if auto_speech_tags:
        text = _apply_xai_auto_speech_tags(text)
    base_url = str(
        xai_config.get("base_url")
        or creds.get("base_url")
        or get_env_value("XAI_BASE_URL")
        or DEFAULT_XAI_BASE_URL
    ).strip().rstrip("/")

    # 默认匹配文档中描述的最小 POST /v1/tts 形态。只有当 Hermes 确实需要
    # 非默认格式/覆盖时才发送 output_format。
    codec = "wav" if output_path.endswith(".wav") else "mp3"
    payload: Dict[str, Any] = {
        "text": text,
        "voice_id": voice_id,
        "language": language,
    }
    if (
        codec != "mp3"
        or sample_rate != DEFAULT_XAI_SAMPLE_RATE
        or (codec == "mp3" and bit_rate != DEFAULT_XAI_BIT_RATE)
    ):
        output_format: Dict[str, Any] = {"codec": codec}
        if sample_rate:
            output_format["sample_rate"] = sample_rate
        if codec == "mp3" and bit_rate:
            output_format["bit_rate"] = bit_rate
        payload["output_format"] = output_format
    # 只有当调用方要求了非 API 默认值（1.0）以外的值时才附带 `speed`。
    # 这样对从不调整该旋钮的用户，保持原有的最小 payload 契约不变。
    if speed is not None and speed != DEFAULT_XAI_SPEED_DEFAULT:
        payload["speed"] = speed
    # 只有当调用方显式选用了非默认值（任何不等于 0 的值）时，才附带
    # `optimize_streaming_latency`。
    if (
        optimize_streaming_latency is not None
        and optimize_streaming_latency != DEFAULT_XAI_OPTIMIZE_STREAMING_LATENCY_DEFAULT
    ):
        payload["optimize_streaming_latency"] = optimize_streaming_latency

    response = requests.post(
        f"{base_url}/tts",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": hermes_xai_user_agent(),
        },
        json=payload,
        timeout=60,
    )
    response.raise_for_status()

    with open(output_path, "wb") as f:
        f.write(response.content)

    return output_path


# ===========================================================================
# 提供商：MiniMax TTS
# ===========================================================================
def _generate_minimax_tts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    """
    使用 MiniMax TTS API 生成音频。

    支持两个端点：
    - v1/text_to_speech：payload 简单，返回原始音频（Content-Type: audio/mpeg）
    - v1/t2a_v2：嵌套的 voice_setting/audio_setting，返回带十六进制编码音频的 JSON

    参数：
        text: 要转换的文本（最多 10,000 个字符）。
        output_path: 音频文件保存位置。
        tts_config: TTS 配置字典。

    返回：
        保存的音频文件路径。
    """
    import requests

    api_key = (get_env_value("MINIMAX_API_KEY") or "")
    if not api_key:
        raise ValueError("MINIMAX_API_KEY not set. Get one at https://platform.minimax.io/")

    mm_config = tts_config.get("minimax", {})
    model = mm_config.get("model", DEFAULT_MINIMAX_MODEL)
    voice_id = mm_config.get("voice_id", DEFAULT_MINIMAX_VOICE_ID)
    base_url = mm_config.get("base_url", DEFAULT_MINIMAX_BASE_URL)
    speed = mm_config.get("speed", 1.0)
    vol = mm_config.get("vol", 1.0)
    pitch = mm_config.get("pitch", 0)
    emotion = mm_config.get("emotion", "neutral")
    sample_rate = mm_config.get("sample_rate", 32000)
    bitrate = mm_config.get("bitrate", 128000)

    # MiniMax 账户通过 GroupId 来划分 TTS 请求作用域。当存在时，文档把它
    # 写成 t2a_v2 URL 上的一个 ?GroupId=<id> 查询参数。可以从配置或
    # MINIMAX_GROUP_ID 环境变量中读取；仅当 URL 上还没有该参数时才附加。
    group_id = (
        str(mm_config.get("group_id") or "").strip()
        or (get_env_value("MINIMAX_GROUP_ID") or "").strip()
    )
    if group_id and "GroupId=" not in base_url:
        sep = "&" if "?" in base_url else "?"
        base_url = f"{base_url}{sep}GroupId={group_id}"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    # 根据 URL 判断端点
    is_t2a_v2 = "t2a_v2" in base_url

    if is_t2a_v2:
        # t2a_v2 端点：嵌套的 voice_setting/audio_setting 结构
        payload = {
            "model": model,
            "text": text,
            "voice_setting": {
                "voice_id": voice_id,
                "speed": speed,
                "vol": vol,
                "pitch": pitch,
                "emotion": emotion,
            },
            "audio_setting": {
                "sample_rate": sample_rate,
                "bitrate": bitrate,
                "format": "mp3",
                "channel": 1,
            },
        }
    else:
        # text_to_speech 端点：扁平 payload
        payload = {
            "model": model,
            "text": text,
            "voice_id": voice_id,
        }

    response = requests.post(base_url, json=payload, headers=headers, timeout=60)

    if is_t2a_v2:
        # t2a_v2 返回带十六进制编码音频的 JSON
        response.raise_for_status()
        result = response.json()
        base_resp = result.get("base_resp", {})
        status_code = base_resp.get("status_code", -1)

        if status_code != 0:
            status_msg = base_resp.get("status_msg", "unknown error")
            raise RuntimeError(f"MiniMax TTS API error (code {status_code}): {status_msg}")

        hex_audio = result.get("data", {}).get("audio", "")
        if not hex_audio:
            raise RuntimeError("MiniMax TTS returned empty audio data")

        audio_bytes = bytes.fromhex(hex_audio)
        with open(output_path, "wb") as f:
            f.write(audio_bytes)
        return output_path

    else:
        # text_to_speech 直接返回原始音频
        content_type = response.headers.get("Content-Type", "")

        if "audio/" in content_type:
            with open(output_path, "wb") as f:
                f.write(response.content)
            return output_path

        # 兜底：尝试按 JSON 解析
        try:
            result = response.json()
            base_resp = result.get("base_resp", {})
            status_code = base_resp.get("status_code", -1)
            if status_code != 0:
                status_msg = base_resp.get("status_msg", "unknown error")
                raise RuntimeError(f"MiniMax TTS API error (code {status_code}): {status_msg}")
        except Exception:
            response.raise_for_status()
            raise RuntimeError(
                f"MiniMax TTS returned unexpected Content-Type '{content_type}' "
                f"({len(response.content)} bytes)"
            )

        raise RuntimeError("MiniMax TTS returned no audio data")


# ===========================================================================
# 提供商：Mistral（Voxtral TTS）
# ===========================================================================
def _generate_mistral_tts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    """使用 Mistral Voxtral TTS API 生成音频。

    该 API 返回 base64 编码的音频；本函数会把它解码，并将原始字节写入
    *output_path*。支持为 Telegram 语音气泡输出原生 Opus。
    """
    api_key = (get_env_value("MISTRAL_API_KEY") or "")
    if not api_key:
        raise ValueError("MISTRAL_API_KEY not set. Get one at https://console.mistral.ai/")

    mi_config = tts_config.get("mistral", {})
    model = mi_config.get("model", DEFAULT_MISTRAL_TTS_MODEL)
    voice_id = mi_config.get("voice_id") or DEFAULT_MISTRAL_TTS_VOICE_ID

    if output_path.endswith(".ogg"):
        response_format = "opus"
    elif output_path.endswith(".wav"):
        response_format = "wav"
    elif output_path.endswith(".flac"):
        response_format = "flac"
    else:
        response_format = "mp3"

    Mistral = _import_mistral_client()
    try:
        with Mistral(api_key=api_key) as client:
            response = client.audio.speech.complete(
                model=model,
                input=text,
                voice_id=voice_id,
                response_format=response_format,
            )
            audio_bytes = base64.b64decode(response.audio_data)
    except ValueError:
        raise
    except Exception as e:
        logger.error("Mistral TTS failed: %s", e, exc_info=True)
        raise RuntimeError(f"Mistral TTS failed: {type(e).__name__}") from e

    with open(output_path, "wb") as f:
        f.write(audio_bytes)

    return output_path


# ===========================================================================
# 提供商：Google Gemini TTS
# ===========================================================================
def _wrap_pcm_as_wav(
    pcm_bytes: bytes,
    sample_rate: int = GEMINI_TTS_SAMPLE_RATE,
    channels: int = GEMINI_TTS_CHANNELS,
    sample_width: int = GEMINI_TTS_SAMPLE_WIDTH,
) -> bytes:
    """为原始的有符号小端 PCM 数据加上标准的 WAV RIFF 头。

    Gemini TTS 返回的是 audio/L16;codec=pcm;rate=24000——即没有任何容器的
    原始 PCM 采样。我们加上一个最小的 WAV 头，使文件可以正常播放，并让
    ffmpeg 能在下游把它重新编码为 MP3/Opus。
    """
    import struct

    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    data_size = len(pcm_bytes)
    fmt_chunk = struct.pack(
        "<4sIHHIIHH",
        b"fmt ",
        16,             # fmt chunk 大小（PCM）
        1,              # 音频格式（PCM）
        channels,
        sample_rate,
        byte_rate,
        block_align,
        sample_width * 8,
    )
    data_chunk_header = struct.pack("<4sI", b"data", data_size)
    riff_size = 4 + len(fmt_chunk) + len(data_chunk_header) + data_size
    riff_header = struct.pack("<4sI4s", b"RIFF", riff_size, b"WAVE")
    return riff_header + fmt_chunk + data_chunk_header + pcm_bytes


def _resolve_gemini_persona_prompt_path(gemini_config: Dict[str, Any]) -> Optional[Path]:
    """返回已配置的角色（persona）提示词文件路径（如果有）。"""
    raw = gemini_config.get("persona_prompt_file")
    if not isinstance(raw, str) or not raw.strip():
        return None

    expanded = os.path.expandvars(raw.strip())
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        try:
            from hermes_constants import get_hermes_home
            path = get_hermes_home() / path
        except Exception:
            path = Path.cwd() / path
    return path


def _read_gemini_persona_prompt(gemini_config: Dict[str, Any]) -> str:
    """读取 Gemini 角色提示词文件，配置出错时静默失败。"""
    path = _resolve_gemini_persona_prompt_path(gemini_config)
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(
            "Gemini TTS persona prompt file unavailable at %s: %s",
            path,
            exc,
        )
        return ""


def _gemini_model_supports_audio_tags(model: str) -> bool:
    """对于已知支持表现力音频标签的 Gemini TTS 模型，返回 True。"""
    normalized = (model or "").strip().lower().rsplit("/", 1)[-1]
    return "gemini-3.1" in normalized and "tts" in normalized


def _gemini_audio_tags_enabled(gemini_config: Dict[str, Any], model: str) -> bool:
    raw = gemini_config.get("audio_tags")
    if isinstance(raw, dict):
        raw = raw.get("enabled")
    enabled = _config_bool(raw, default=DEFAULT_GEMINI_AUDIO_TAGS)
    if not enabled:
        return False
    if not _gemini_model_supports_audio_tags(model):
        logger.warning(
            "Gemini TTS audio_tags enabled, but model %s is not known to support "
            "Gemini audio tags; skipping hidden tag rewrite",
            model,
        )
        return False
    return True


def _clean_gemini_audio_tag_rewrite(content: str) -> str:
    clean = (content or "").strip()
    fence = re.fullmatch(r"```(?:[A-Za-z0-9_-]+)?\s*(.*?)\s*```", clean, flags=re.DOTALL)
    if fence:
        clean = fence.group(1).strip()
    return clean


def _extract_auxiliary_message_content(response: Any) -> str:
    try:
        choice = response.choices[0]
        message = getattr(choice, "message", None)
        if isinstance(message, dict):
            return str(message.get("content") or "")
        return str(getattr(message, "content", "") or "")
    except Exception:
        return ""


def _rewrite_gemini_tts_audio_tags(text: str, persona_prompt: str = "") -> str:
    """使用配置好的辅助模型为文本插入 Gemini 音频标签。"""
    transcript = text.strip()
    if not transcript:
        return text

    system_prompt = (
        "You rewrite transcripts for Gemini 3.1 Flash TTS by inserting expressive "
        "audio tags.\n\n"
        "Audio tags are inline square-bracket modifiers such as [whispers], "
        "[excitedly], [very slow], [sarcastically], [laughs], [sighs], or [gasp]. "
        "There is no fixed allowlist. Use creative freeform tags generously but "
        "naturally to control tone, pace, emotional vibe, emphasis, section-level "
        "delivery, and non-verbal sounds. Use English audio tags even when the "
        "spoken transcript is not English.\n\n"
        "Rules:\n"
        "- Preserve the spoken words, order, and meaning.\n"
        "- Do not add new spoken sentences or remove existing spoken words.\n"
        "- Use square brackets for every audio tag.\n"
        "- Do not use SSML or XML tags.\n"
        "- Do not explain or comment.\n"
        "- Return only the tagged TTS script."
    )
    context = persona_prompt.strip() or "(none)"
    user_prompt = (
        "PERSONA AND DIRECTOR CONTEXT:\n"
        f"{context}\n\n"
        "TRANSCRIPT TO TAG:\n"
        f"{transcript}"
    )
    try:
        from agent.auxiliary_client import call_llm

        response = call_llm(
            task=GEMINI_AUDIO_TAG_REWRITE_TASK,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.7,
        )
        tagged = _clean_gemini_audio_tag_rewrite(_extract_auxiliary_message_content(response))
        return tagged or text
    except Exception as exc:
        logger.warning("Gemini TTS audio tag rewrite failed; using untagged text: %s", exc)
        return text


def _compose_gemini_tts_prompt(
    text: str,
    gemini_config: Dict[str, Any],
    persona_prompt: Optional[str] = None,
) -> str:
    """根据角色设定和实时转写文本构建 Gemini 提示词。"""
    transcript = text.strip()
    if persona_prompt is None:
        persona_prompt = _read_gemini_persona_prompt(gemini_config)
    if not persona_prompt:
        return transcript

    preamble = (
        "Synthesize speech from the TRANSCRIPT only. Treat AUDIO PROFILE, "
        "SCENE, DIRECTOR'S NOTES, and SAMPLE CONTEXT as performance direction; "
        "do not speak those sections aloud."
    )

    placeholder_patterns = (
        re.compile(r"\{\{\s*transcript\s*\}\}", flags=re.IGNORECASE),
        re.compile(r"\{\s*transcript\s*\}", flags=re.IGNORECASE),
    )
    prompt = persona_prompt
    for pattern in placeholder_patterns:
        if pattern.search(prompt):
            prompt = pattern.sub(transcript, prompt)
            return f"{preamble}\n\n{prompt}".strip()

    return f"{preamble}\n\n{persona_prompt}\n\n#### TRANSCRIPT\n{transcript}".strip()


def _generate_gemini_tts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    """使用 Google Gemini TTS 生成音频。

    Gemini 的 generateContent 端点（带 responseModalities=["AUDIO"]）会返回
    以 base64 编码的原始 24kHz 单声道 16-bit PCM（L16）。我们用一个 WAV RIFF
    头包住它以得到可播放的文件；如果调用方要求的是 MP3 / Opus 格式，再用
    ffmpeg 转换（与 NeuTTS 的做法相同）。

    参数：
        text: 要转换的文本（提示词风格；支持内联指令，例如
              "Say cheerfully:"，以及音频标签，例如 [whispers]）。
        output_path: 音频文件保存位置（.wav、.mp3 或 .ogg）。
        tts_config: TTS 配置字典。

    返回：
        保存的音频文件路径。
    """
    import requests

    api_key = (get_env_value("GEMINI_API_KEY") or get_env_value("GOOGLE_API_KEY") or "").strip()
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY not set. Get one at https://aistudio.google.com/app/apikey"
        )

    raw_gemini_config = tts_config.get("gemini", {})
    gemini_config = raw_gemini_config if isinstance(raw_gemini_config, dict) else {}
    model = str(gemini_config.get("model", DEFAULT_GEMINI_TTS_MODEL)).strip() or DEFAULT_GEMINI_TTS_MODEL
    voice = str(gemini_config.get("voice", DEFAULT_GEMINI_TTS_VOICE)).strip() or DEFAULT_GEMINI_TTS_VOICE
    base_url = str(
        gemini_config.get("base_url")
        or get_env_value("GEMINI_BASE_URL")
        or DEFAULT_GEMINI_TTS_BASE_URL
    ).strip().rstrip("/")
    persona_prompt = _read_gemini_persona_prompt(gemini_config)
    tts_script = text
    if _gemini_audio_tags_enabled(gemini_config, model):
        tts_script = _rewrite_gemini_tts_audio_tags(text, persona_prompt=persona_prompt)
    prompt_text = _compose_gemini_tts_prompt(
        tts_script,
        gemini_config,
        persona_prompt=persona_prompt,
    )
    max_len = _resolve_max_text_length("gemini", tts_config)
    if len(prompt_text) > max_len:
        logger.warning(
            "Gemini TTS composed prompt too long (%d chars), truncating to %d",
            len(prompt_text), max_len,
        )
        prompt_text = prompt_text[:max_len]

    payload: Dict[str, Any] = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice},
                },
            },
        },
    }

    endpoint = f"{base_url}/models/{model}:generateContent"
    response = requests.post(
        endpoint,
        params={"key": api_key},
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    if response.status_code != 200:
        # 存在时，把 API 的错误消息暴露出来
        try:
            err = response.json().get("error", {})
            detail = err.get("message") or response.text[:300]
        except Exception:
            detail = response.text[:300]
        raise RuntimeError(
            f"Gemini TTS API error (HTTP {response.status_code}): {detail}"
        )

    try:
        data = response.json()
        parts = data["candidates"][0]["content"]["parts"]
        audio_part = next((p for p in parts if "inlineData" in p or "inline_data" in p), None)
        if audio_part is None:
            raise RuntimeError("Gemini TTS response contained no audio data")
        inline = audio_part.get("inlineData") or audio_part.get("inline_data") or {}
        audio_b64 = inline.get("data", "")
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Gemini TTS response was malformed: {e}") from e

    if not audio_b64:
        raise RuntimeError("Gemini TTS returned empty audio data")

    pcm_bytes = base64.b64decode(audio_b64)
    wav_bytes = _wrap_pcm_as_wav(pcm_bytes)

    # 快速路径：调用方直接要 WAV，直接写入即可。
    if output_path.lower().endswith(".wav"):
        with open(output_path, "wb") as f:
            f.write(wav_bytes)
        return output_path

    # 否则把 WAV 写到临时文件，再用 ffmpeg 转换为目标格式（.mp3 或 .ogg）。
    # 如果没有 ffmpeg，则回落为直接把 WAV 改名——这与 NeuTTS 的行为一致，
    # 并保证在没有 ffmpeg 的系统上工具仍可用（音频仍能播放，只是扩展名
    # 可能有误导）。
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(wav_bytes)
        wav_path = tmp.name

    try:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            # 对于 .ogg 输出，强制使用 libopus 编码（Telegram 语音气泡
            # 特别要求 Opus；ffmpeg 对 .ogg 的默认编码是 Vorbis）。
            if output_path.lower().endswith(".ogg"):
                cmd = [
                    ffmpeg, "-i", wav_path,
                    "-acodec", "libopus", "-ac", "1",
                    "-b:a", "64k", "-vbr", "off",
                    "-y", "-loglevel", "error",
                    output_path,
                ]
            else:
                cmd = [ffmpeg, "-i", wav_path, "-y", "-loglevel", "error", output_path]
            result = subprocess.run(cmd, capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="ignore")[:300]
                raise RuntimeError(f"ffmpeg conversion failed: {stderr}")
        else:
            logger.warning(
                "ffmpeg not found; writing raw WAV to %s (extension may be misleading)",
                output_path,
            )
            shutil.copyfile(wav_path, output_path)
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass

    return output_path


# ===========================================================================
# NeuTTS（本地、通过 neutts_cli 实现的设备端 TTS）
# ===========================================================================

def _check_neutts_available() -> bool:
    """检查 neutts 引擎是否可导入（已在本地安装）。"""
    try:
        import importlib.util
        return importlib.util.find_spec("neutts") is not None
    except Exception:
        return False


def _check_kittentts_available() -> bool:
    """检查 kittentts 引擎是否可导入（已在本地安装）。"""
    try:
        import importlib.util
        return importlib.util.find_spec("kittentts") is not None
    except Exception:
        return False


def _default_neutts_ref_audio() -> str:
    """返回内置的默认声音参考音频路径。"""
    return str(Path(__file__).parent / "neutts_samples" / "jo.wav")


def _default_neutts_ref_text() -> str:
    """返回内置的默认声音参考转写文本路径。"""
    return str(Path(__file__).parent / "neutts_samples" / "jo.txt")


def _generate_neutts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    """使用本地 NeuTTS 引擎生成语音。

    通过 tools/neutts_synth.py 在子进程中运行合成，让 ~500MB 的模型留在
    一个独立的进程中，合成结束后即退出。输出 WAV；如需用于 Telegram，
    由调用方负责转换格式。
    """
    import sys

    neutts_config = tts_config.get("neutts", {})
    ref_audio = neutts_config.get("ref_audio", "") or _default_neutts_ref_audio()
    ref_text = neutts_config.get("ref_text", "") or _default_neutts_ref_text()
    model = neutts_config.get("model", "neuphonic/neutts-air-q4-gguf")
    device = neutts_config.get("device", "cpu")

    # NeuTTS 原生输出 WAV——生成时用 .wav 路径，
    # 让调用方在之后转换成最终格式。
    wav_path = output_path
    if not output_path.endswith(".wav"):
        wav_path = output_path.rsplit(".", 1)[0] + ".wav"

    synth_script = str(Path(__file__).parent / "neutts_synth.py")
    cmd = [
        sys.executable, synth_script,
        "--text", text,
        "--out", wav_path,
        "--ref-audio", ref_audio,
        "--ref-text", ref_text,
        "--model", model,
        "--device", device,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        # 从 stderr 中过滤掉 "OK:" 行
        error_lines = [l for l in stderr.splitlines() if not l.startswith("OK:")]
        raise RuntimeError(f"NeuTTS synthesis failed: {chr(10).join(error_lines) or 'unknown error'}")

    # 如果调用方要的是 .mp3 或 .ogg，则从 WAV 转换
    if wav_path != output_path:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            conv_cmd = [ffmpeg, "-i", wav_path, "-y", "-loglevel", "error", output_path]
            subprocess.run(conv_cmd, check=True, timeout=30, stdin=subprocess.DEVNULL)
            os.remove(wav_path)
        else:
            # 没有 ffmpeg——直接把 WAV 改名为期望的路径
            os.rename(wav_path, output_path)

    return output_path


# ===========================================================================
# 提供商：Piper（本地、神经网络 VITS、44 种语言）
# ===========================================================================

# Piper 语音实例的模块级缓存。语音以其 .onnx 模型的绝对路径为键，这样
# 切换语音时不会让之前缓存的语音失效。
_piper_voice_cache: Dict[str, Any] = {}


def _check_piper_available() -> bool:
    """检查 piper-tts 包是否可导入。"""
    try:
        import importlib.util
        return importlib.util.find_spec("piper") is not None
    except Exception:
        return False


def _get_piper_voices_dir() -> Path:
    """返回 Hermes 缓存 Piper 语音模型的目录。

    解析为当前生效的 HERMES_HOME 下的 ``~/.hermes/cache/piper-voices/``，
    这样语音下载会跟随 profile 的边界。
    """
    from hermes_constants import get_hermes_dir
    root = Path(get_hermes_dir("cache/piper-voices", "piper_voices_cache"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_piper_voice_path(voice: str, download_dir: Path) -> str:
    """把 *voice*（模型名或路径）解析为具体的 .onnx 文件路径。

    接受以下任意一种：
      - 用户已有的 .onnx 文件的绝对路径 / 展开后的路径
      - 一个语音 *名称*，例如 ``en_US-lessac-medium``（首次使用时通过
        ``python -m piper.download_voices`` 下载到 ``download_dir``）

    找不到或无法下载模型时抛出 RuntimeError。
    """
    if not voice:
        voice = DEFAULT_PIPER_VOICE

    # 情况 1：用户给了一个直接的文件路径。
    candidate = Path(voice).expanduser()
    if candidate.suffix.lower() == ".onnx" and candidate.exists():
        return str(candidate)

    # 情况 2：用户给了一个语音 *名称*。先看是否已经下载过。
    cached = download_dir / f"{voice}.onnx"
    if cached.exists() and (download_dir / f"{voice}.onnx.json").exists():
        return str(cached)

    # 情况 3：下载该语音。piper 自带了一个下载辅助模块。
    import sys as _sys
    logger.info("[Piper] Downloading voice '%s' to %s (first use)", voice, download_dir)
    try:
        result = subprocess.run(
            [_sys.executable, "-m", "piper.download_voices", voice,
             "--download-dir", str(download_dir)],
            capture_output=True, text=True, timeout=300,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Piper voice download timed out after 300s for '{voice}'"
        ) from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip() or "no stderr output"
        raise RuntimeError(
            f"Piper voice download failed for '{voice}': {stderr[:400]}"
        )

    if not cached.exists():
        raise RuntimeError(
            f"Piper voice download completed but {cached} is missing — "
            f"check voice name (see: https://github.com/OHF-Voice/piper1-gpl/"
            f"blob/main/docs/VOICES.md)"
        )
    return str(cached)


def _generate_piper_tts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    """使用本地 Piper 引擎生成语音。

    每个进程只加载一次语音模型（按绝对路径缓存），并写入一个 WAV 文件。
    当需要不同的输出格式时，由调用方通过 ffmpeg 转换为 MP3/Opus。
    """
    PiperVoice = _import_piper()
    import wave

    piper_config = tts_config.get("piper", {}) if isinstance(tts_config, dict) else {}
    voice_name = piper_config.get("voice") or DEFAULT_PIPER_VOICE
    download_dir = Path(piper_config.get("voices_dir") or _get_piper_voices_dir()).expanduser()
    download_dir.mkdir(parents=True, exist_ok=True)
    use_cuda = bool(piper_config.get("use_cuda", False))

    model_path = _resolve_piper_voice_path(voice_name, download_dir)

    # 宽容地解析 speaker_id：把非法输入（非整数字符串、列表、字典）丢弃，
    # 回落到 0（Piper 自身的默认值）。布尔值会被直接拒绝——True/False 会
    # 被无声地强转为 1/0，从而掩盖配置错误。
    _raw_speaker = piper_config.get("speaker_id", 0)
    if isinstance(_raw_speaker, bool) or not isinstance(_raw_speaker, int):
        speaker_id = 0
    else:
        speaker_id = _raw_speaker

    # speaker_id 通过 syn_config.speaker_id 按次调用生效——同一个 PiperVoice
    # 实例可服务所有说话人，因此它不进入缓存键。多说话人流程共享一次模型加载。
    cache_key = f"{model_path}::cuda={use_cuda}"
    global _piper_voice_cache
    if cache_key not in _piper_voice_cache:
        logger.info("[Piper] Loading voice: %s", model_path)
        _piper_voice_cache[cache_key] = PiperVoice.load(model_path, use_cuda=use_cuda)
        logger.info("[Piper] Voice loaded")
    voice = _piper_voice_cache[cache_key]

    # 可选的合成旋钮——只有当至少配置了一个高级旋钮时才传入 SynthesisConfig，
    # 这样除非确有必要，我们不会依赖比用户已装版本更新的 Piper。
    syn_config = None
    has_advanced = any(
        k in piper_config
        for k in (
            "length_scale",
            "noise_scale",
            "noise_w_scale",
            "volume",
            "normalize_audio",
            "speaker_id",
        )
    )
    if has_advanced:
        try:
            from piper import SynthesisConfig  # type: ignore
            syn_config = SynthesisConfig(
                length_scale=float(piper_config.get("length_scale", 1.0)),
                noise_scale=float(piper_config.get("noise_scale", 0.667)),
                noise_w_scale=float(piper_config.get("noise_w_scale", 0.8)),
                volume=float(piper_config.get("volume", 1.0)),
                normalize_audio=bool(piper_config.get("normalize_audio", True)),
                speaker_id=speaker_id,
            )
        except ImportError:
            logger.warning(
                "[Piper] SynthesisConfig not available in this piper-tts "
                "version — advanced knobs ignored"
            )

    # Piper 输出 WAV。由调用方负责下游的 MP3/Opus 转换。
    wav_path = output_path
    if not output_path.endswith(".wav"):
        wav_path = output_path.rsplit(".", 1)[0] + ".wav"

    with wave.open(wav_path, "wb") as wav_file:
        if syn_config is not None:
            voice.synthesize_wav(text, wav_file, syn_config=syn_config)
        else:
            voice.synthesize_wav(text, wav_file)

    # 如果调用方要求 mp3/ogg，则转换为对应格式
    if wav_path != output_path:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            conv_cmd = [ffmpeg, "-i", wav_path, "-y", "-loglevel", "error", output_path]
            subprocess.run(conv_cmd, check=True, timeout=30, stdin=subprocess.DEVNULL)
            try:
                os.remove(wav_path)
            except OSError:
                pass
        else:
            # 没有 ffmpeg——保留 WAV 并返回该路径
            os.rename(wav_path, output_path)

    return output_path


# ===========================================================================
# 提供商：KittenTTS（本地、轻量）
# ===========================================================================

# KittenTTS 模型实例的模块级缓存
_kittentts_model_cache: Dict[str, Any] = {}


def _generate_kittentts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    """使用本地的 KittenTTS ONNX 模型生成语音。

    KittenTTS 是一个轻量级 TTS 引擎（25-80MB 模型），完全在 CPU 上运行，
    不需要 GPU 或 API key。

    参数：
        text: 要转换为语音的文本。
        output_path: 音频文件保存位置。
        tts_config: TTS 配置字典。

    返回：
        保存的音频文件路径。
    """
    KittenTTS = _import_kittentts()
    kt_config = tts_config.get("kittentts", {})
    model_name = kt_config.get("model", DEFAULT_KITTENTTS_MODEL)
    voice = kt_config.get("voice", DEFAULT_KITTENTTS_VOICE)
    speed = kt_config.get("speed", 1.0)
    clean_text = kt_config.get("clean_text", True)

    # 如有缓存的模型实例则使用它
    global _kittentts_model_cache
    if model_name not in _kittentts_model_cache:
        logger.info("[KittenTTS] Loading model: %s", model_name)
        _kittentts_model_cache[model_name] = KittenTTS(model_name)
        logger.info("[KittenTTS] Model loaded successfully")

    model = _kittentts_model_cache[model_name]

    # 生成音频（返回 24kHz 的 numpy 数组）
    audio = model.generate(text, voice=voice, speed=speed, clean_text=clean_text)

    # 保存为 WAV
    import soundfile as sf
    wav_path = output_path
    if not output_path.endswith(".wav"):
        wav_path = output_path.rsplit(".", 1)[0] + ".wav"

    sf.write(wav_path, audio, 24000)

    # 如有需要，转换为期望的格式
    if wav_path != output_path:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            conv_cmd = [ffmpeg, "-i", wav_path, "-y", "-loglevel", "error", output_path]
            subprocess.run(conv_cmd, check=True, timeout=30, stdin=subprocess.DEVNULL)
            os.remove(wav_path)
        else:
            # 没有 ffmpeg——把 WAV 改名为期望的路径
            os.rename(wav_path, output_path)

    return output_path


# ===========================================================================
# 主工具函数
# ===========================================================================
def text_to_speech_tool(
    text: str,
    output_path: Optional[str] = None,
) -> str:
    """
    把文本转换为语音音频。

    从 ~/.hermes/config.yaml（tts: 段）读取提供商/语音配置。
    模型只发送文本；语音和提供商由用户配置。

    在消息平台上，返回的 MEDIA:<path> 标签会被发送管道拦截，并以原生
    语音消息的形式投递。在 CLI 模式下，文件会保存到 ~/voice-memos/。

    参数：
        text: 要转换为语音的文本。
        output_path: 可选的自定义保存路径。默认为 ~/voice-memos/<timestamp>.mp3

    返回：
        str: JSON 结果，包含 success、file_path，以及可选的 MEDIA 标签。
    """
    if not text or not text.strip():
        return tool_error("Text is required", success=False)

    tts_config = _load_tts_config()
    provider = _get_provider(tts_config)

    # 用户声明的命令型提供商（位于 tts.providers.<name> 下的 type: command）
    # 在内置分发之前解析。内置名称在此处短路，这样用户的
    # ``tts.providers.openai.command`` 就无法覆盖真正的 OpenAI 处理函数。
    command_provider_config = _resolve_command_provider_config(provider, tts_config)

    # 对过长的文本做截断并给出告警。上限按提供商不同而不同
    # （OpenAI 4096、xAI 15k、MiniMax 10k、ElevenLabs 按模型变化等）。
    max_len = _resolve_max_text_length(provider, tts_config)
    if len(text) > max_len:
        logger.warning(
            "TTS text too long for provider %s (%d chars), truncating to %d",
            provider, len(text), max_len,
        )
        text = text[:max_len]

    # 通过 gateway 环境变量检测平台，以选择最合适的输出格式。
    # Telegram 语音气泡要求 Opus（.ogg）；OpenAI 和 ElevenLabs 可以原生
    # 输出 Opus（无需 ffmpeg）。Edge TTS 总是输出 MP3，需要 ffmpeg 来转换。
    from gateway.session_context import get_session_env
    platform = get_session_env("HERMES_SESSION_PLATFORM", "").lower()
    want_opus = (platform == "telegram")

    # 确定输出路径
    if output_path:
        # 拒绝用户提供的路径中的 '..' 路径穿越成分。显式的绝对路径是允许的
        # （agent 会合理地把音频写到用户指定的位置），但用 ``..`` 来逃逸出
        # 其声明的根目录的路径几乎总是 bug 或被提示词注入控制——例如
        # ``output_path="audio/../../etc/cron.d/x"``。terminal 工具在获得
        # 批准后仍可写到任意位置；这里只是避免无人值守的 TTS 接口通过
        # 路径穿越来落盘文件。
        from tools.path_security import has_traversal_component
        if has_traversal_component(output_path):
            return json.dumps({
                "success": False,
                "error": (
                    f"output_path contains '..' traversal component: "
                    f"{output_path}. Use an absolute path or one relative "
                    "to the current directory without '..'."
                ),
            }, ensure_ascii=False)
        file_path = Path(output_path).expanduser()
        if command_provider_config is not None:
            # 尊重调用方提供的路径，但把扩展名对齐到该提供商配置的
            # output_format，使命令写到调用方真正期望的路径。
            file_path = _configured_command_tts_output_path(
                file_path, command_provider_config
            )
    else:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path(DEFAULT_OUTPUT_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        if command_provider_config is not None:
            fmt = _get_command_tts_output_format(command_provider_config)
            file_path = out_dir / f"tts_{timestamp}.{fmt}"
        # 对于支持原生 Opus 输出的提供商，Telegram 平台下使用 .ogg；
        # 否则回落到 .mp3（Edge TTS 之后会尝试用 ffmpeg 转换）。
        elif want_opus and provider in {"openai", "elevenlabs", "mistral", "gemini"}:
            file_path = out_dir / f"tts_{timestamp}.ogg"
        else:
            file_path = out_dir / f"tts_{timestamp}.mp3"

    # 确保父目录存在
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_str = str(file_path)

    try:
        # 用配置好的提供商生成音频
        if command_provider_config is not None:
            logger.info(
                "Generating speech with command TTS provider '%s'...", provider,
            )
            file_str = _generate_command_tts(
                text, file_str, provider, command_provider_config, tts_config,
            )

        # 插件注册的 TTS 后端（issue #30398）。当配置的提供商既不是内置项、
        # 也不是命令型条目，并且有插件以该名称注册时触发。海象运算符只在
        # 分发器返回路径（即确实找到了插件）时才把值绑定到 `_plugin_path`；
        # 返回 None 则继续回落到内置的 elif 链，使未知名称最终落到最下方的
        # Edge TTS 默认分支。分发器自身会防御性地强制执行
        # 「内置必胜」+「命令胜过插件」的不变量。
        elif provider not in BUILTIN_TTS_PROVIDERS and (
            _plugin_path := _dispatch_to_plugin_provider(
                text, file_str, provider, tts_config,
            )
        ) is not None:
            file_str = _plugin_path

        elif provider == "elevenlabs":
            try:
                _import_elevenlabs()
            except ImportError:
                return json.dumps({
                    "success": False,
                    "error": "ElevenLabs provider selected but 'elevenlabs' package not installed. Run: pip install elevenlabs"
                }, ensure_ascii=False)
            logger.info("Generating speech with ElevenLabs...")
            _generate_elevenlabs(text, file_str, tts_config)

        elif provider == "openai":
            try:
                _import_openai_client()
            except ImportError:
                return json.dumps({
                    "success": False,
                    "error": "OpenAI provider selected but 'openai' package not installed."
                }, ensure_ascii=False)
            logger.info("Generating speech with OpenAI TTS...")
            _generate_openai_tts(text, file_str, tts_config)

        elif provider == "minimax":
            logger.info("Generating speech with MiniMax TTS...")
            _generate_minimax_tts(text, file_str, tts_config)

        elif provider == "xai":
            logger.info("Generating speech with xAI TTS...")
            _generate_xai_tts(text, file_str, tts_config)

        elif provider == "mistral":
            try:
                _import_mistral_client()
            except ImportError:
                return json.dumps({
                    "success": False,
                    "error": "Mistral provider selected but 'mistralai' package not installed. "
                             "Run: pip install 'hermes-agent[mistral]'"
                }, ensure_ascii=False)
            logger.info("Generating speech with Mistral Voxtral TTS...")
            _generate_mistral_tts(text, file_str, tts_config)

        elif provider == "gemini":
            logger.info("Generating speech with Google Gemini TTS...")
            _generate_gemini_tts(text, file_str, tts_config)

        elif provider == "neutts":
            if not _check_neutts_available():
                return json.dumps({
                    "success": False,
                    "error": "NeuTTS provider selected but neutts is not installed. "
                             "Run hermes setup and choose NeuTTS, or install espeak-ng and run python -m pip install -U neutts[all]."
                }, ensure_ascii=False)
            logger.info("Generating speech with NeuTTS (local)...")
            _generate_neutts(text, file_str, tts_config)

        elif provider == "kittentts":
            try:
                _import_kittentts()
            except ImportError:
                return json.dumps({
                    "success": False,
                    "error": "KittenTTS provider selected but 'kittentts' package not installed. "
                             "Run 'hermes setup tts' and choose KittenTTS, or install manually: "
                             "pip install https://github.com/KittenML/KittenTTS/releases/download/0.8.1/kittentts-0.8.1-py3-none-any.whl"
                }, ensure_ascii=False)
            logger.info("Generating speech with KittenTTS (local, ~25MB)...")
            _generate_kittentts(text, file_str, tts_config)

        elif provider == "piper":
            try:
                _import_piper()
            except ImportError:
                return json.dumps({
                    "success": False,
                    "error": "Piper provider selected but 'piper-tts' package not installed. "
                             "Run 'hermes tools' and select Piper under TTS, or install manually: "
                             "pip install piper-tts",
                }, ensure_ascii=False)
            logger.info("Generating speech with Piper (local)...")
            _generate_piper_tts(text, file_str, tts_config)

        else:
            # 默认：Edge TTS（免费），并以 NeuTTS 作为本地兜底
            edge_available = True
            try:
                _import_edge_tts()
            except ImportError:
                edge_available = False

            if edge_available:
                logger.info("Generating speech with Edge TTS...")
                try:
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        pool.submit(
                            lambda: asyncio.run(_generate_edge_tts(text, file_str, tts_config))
                        ).result(timeout=60)
                except RuntimeError:
                    asyncio.run(_generate_edge_tts(text, file_str, tts_config))
            elif _check_neutts_available():
                logger.info("Edge TTS not available, falling back to NeuTTS (local)...")
                provider = "neutts"
                _generate_neutts(text, file_str, tts_config)
            else:
                return json.dumps({
                    "success": False,
                    "error": "No TTS provider available. Install edge-tts (pip install edge-tts) "
                             "or set up NeuTTS for local synthesis."
                }, ensure_ascii=False)

        # 检查文件是否确实已创建
        if not os.path.exists(file_str) or os.path.getsize(file_str) == 0:
            return json.dumps({
                "success": False,
                "error": f"TTS generation produced no output (provider: {provider})"
            }, ensure_ascii=False)

        # 尝试转换为 Opus 以兼容 Telegram。
        # Edge TTS 输出 MP3，NeuTTS/KittenTTS 输出 WAV。这些原生格式用于
        # 本地/CLI 播放时保留不变；只有当前平台确实需要 Opus 语音投递时
        # 才做转换。
        voice_compatible = False
        if command_provider_config is not None:
            # 命令型提供商默认按文档处理。只有当用户在其提供商配置中显式
            # 设置 ``voice_compatible: true`` 时，才会启用语音气泡投递。
            if _is_command_tts_voice_compatible(command_provider_config):
                if not file_str.endswith(".ogg"):
                    opus_path = _convert_to_opus(file_str)
                    if opus_path:
                        file_str = opus_path
                voice_compatible = file_str.endswith(".ogg")
        elif provider not in BUILTIN_TTS_PROVIDERS:
            # 插件注册的提供商（issue #30398）。语音气泡投递通过
            # ``TTSProvider.voice_compatible`` 启用（与命令型提供商的启用
            # 方式一致）。已经直接输出 Opus 的插件会跳过 ffmpeg 转换。
            plugin_voice_compatible = _plugin_provider_is_voice_compatible(provider)
            if plugin_voice_compatible:
                if not file_str.endswith(".ogg"):
                    opus_path = _convert_to_opus(file_str)
                    if opus_path:
                        file_str = opus_path
                voice_compatible = file_str.endswith(".ogg")
        elif (
            want_opus
            and provider in {"edge", "neutts", "minimax", "xai", "kittentts", "piper"}
            and not file_str.endswith(".ogg")
        ):
            opus_path = _convert_to_opus(file_str)
            if opus_path:
                file_str = opus_path
                voice_compatible = True
        elif provider in {"elevenlabs", "openai", "mistral", "gemini"}:
            voice_compatible = want_opus and file_str.endswith(".ogg")

        file_size = os.path.getsize(file_str)
        logger.info("TTS audio saved: %s (%s bytes, provider: %s)", file_str, f"{file_size:,}", provider)

        # 构建带 MEDIA 标签的响应，用于平台投递
        media_tag = f"MEDIA:{file_str}"
        if voice_compatible:
            media_tag = f"[[audio_as_voice]]\n{media_tag}"

        return json.dumps({
            "success": True,
            "file_path": file_str,
            "media_tag": media_tag,
            "provider": provider,
            "voice_compatible": voice_compatible,
        }, ensure_ascii=False)

    except ValueError as e:
        # 配置错误（缺少 API key 等）
        error_msg = f"TTS configuration error ({provider}): {e}"
        logger.error("%s", error_msg)
        return tool_error(error_msg, success=False)
    except FileNotFoundError as e:
        # 缺失依赖或文件
        error_msg = f"TTS dependency missing ({provider}): {e}"
        logger.error("%s", error_msg, exc_info=True)
        return tool_error(error_msg, success=False)
    except Exception as e:
        # 意料之外的错误
        error_msg = f"TTS generation failed ({provider}): {e}"
        logger.error("%s", error_msg, exc_info=True)
        return tool_error(error_msg, success=False)


# ===========================================================================
# 依赖检查
# ===========================================================================
def check_tts_requirements() -> bool:
    """
    检查是否至少有一个 TTS 提供商可用。

    Edge TTS 无需 API key 且为默认项，因此只要装了这个包，TTS 即可用。
    用户声明的命令型提供商也满足该要求。

    返回：
        bool: 至少有一个可用提供商时为 True。
    """
    # 任何已配置的命令型提供商都算作可用。
    if _has_any_command_tts_provider():
        return True
    try:
        _import_edge_tts()
        return True
    except ImportError:
        pass
    try:
        _import_elevenlabs()
        if get_env_value("ELEVENLABS_API_KEY"):
            return True
    except ImportError:
        pass
    try:
        _import_openai_client()
        if _has_openai_audio_backend():
            return True
    except ImportError:
        pass
    if get_env_value("MINIMAX_API_KEY"):
        return True
    try:
        from tools.xai_http import resolve_xai_http_credentials

        if resolve_xai_http_credentials().get("api_key"):
            return True
    except Exception:
        pass
    if get_env_value("GEMINI_API_KEY") or get_env_value("GOOGLE_API_KEY"):
        return True
    try:
        _import_mistral_client()
        if get_env_value("MISTRAL_API_KEY"):
            return True
    except ImportError:
        pass
    if _check_neutts_available():
        return True
    if _check_kittentts_available():
        return True
    if _check_piper_available():
        return True
    return False


def _resolve_openai_audio_client_config() -> tuple[str, str]:
    """返回直连的 OpenAI 音频配置，或在托管网关可用时回落到它。

    当配置中设置了 ``tts.use_gateway`` 时，即使存在直连的 OpenAI 凭据，
    也会优先使用 Tool Gateway。
    """
    direct_api_key = resolve_openai_audio_api_key()
    if direct_api_key and not prefers_gateway("tts"):
        return direct_api_key, DEFAULT_OPENAI_BASE_URL

    managed_gateway = resolve_managed_tool_gateway("openai-audio")
    if managed_gateway is None:
        message = "Neither VOICE_TOOLS_OPENAI_KEY nor OPENAI_API_KEY is set"
        if managed_nous_tools_enabled() or prefers_gateway("tts"):
            message += (
                ". "
                + nous_tool_gateway_unavailable_message(
                    "managed OpenAI audio for TTS",
                )
            )
        raise ValueError(message)

    return managed_gateway.nous_user_token, urljoin(
        f"{managed_gateway.gateway_origin.rstrip('/')}/", "v1"
    )


def _has_openai_audio_backend() -> bool:
    """当 OpenAI 音频可使用直连凭据或托管网关时，返回 True。"""
    return bool(resolve_openai_audio_api_key() or resolve_managed_tool_gateway("openai-audio"))


# ===========================================================================
# 流式 TTS：为 ElevenLabs 准备的逐句流水线
# ===========================================================================
# 句子边界模式：标点后跟空格或换行
_SENTENCE_BOUNDARY_RE = re.compile(r'(?<=[.!?])(?:\s|\n)|(?:\n\n)')

# Markdown 剥离模式（与 cli.py 的 _voice_speak_response 相同）
_MD_CODE_BLOCK = re.compile(r'```[\s\S]*?```')
_MD_LINK = re.compile(r'\[([^\]]+)\]\([^)]+\)')
_MD_URL = re.compile(r'https?://\S+')
_MD_BOLD = re.compile(r'\*\*(.+?)\*\*')
_MD_ITALIC = re.compile(r'\*(.+?)\*')
_MD_INLINE_CODE = re.compile(r'`(.+?)`')
_MD_HEADER = re.compile(r'^#+\s*', flags=re.MULTILINE)
_MD_LIST_ITEM = re.compile(r'^\s*[-*]\s+', flags=re.MULTILINE)
_MD_HR = re.compile(r'---+')
_MD_EXCESS_NL = re.compile(r'\n{3,}')


def _strip_markdown_for_tts(text: str) -> str:
    """去掉不应被朗读出来的 markdown 格式。"""
    text = _MD_CODE_BLOCK.sub(' ', text)
    text = _MD_LINK.sub(r'\1', text)
    text = _MD_URL.sub('', text)
    text = _MD_BOLD.sub(r'\1', text)
    text = _MD_ITALIC.sub(r'\1', text)
    text = _MD_INLINE_CODE.sub(r'\1', text)
    text = _MD_HEADER.sub('', text)
    text = _MD_LIST_ITEM.sub('', text)
    text = _MD_HR.sub('', text)
    text = _MD_EXCESS_NL.sub('\n\n', text)
    return text.strip()


def stream_tts_to_speaker(
    text_queue: queue.Queue,
    stop_event: threading.Event,
    tts_done_event: threading.Event,
    display_callback: Optional[Callable[[str], None]] = None,
):
    """从 *text_queue* 中消费文本增量，把它们缓冲成句子，
    并通过 ElevenLabs TTS 把每一句实时流式播放到扬声器。

    协议：
        * 生产方把 ``str`` 增量放入 *text_queue*。
        * 一个 ``None`` 哨兵值表示文本结束（冲刷剩余缓冲区）。
        * 可以设置 *stop_event* 来提前中止（例如用户打断）。
        * *tts_done_event* 会在 ``finally`` 块中被 **set**，这样等待它的
          调用方（持续语音模式）就能知道播放已结束。
    """
    tts_done_event.clear()

    try:
        # --- TTS 客户端初始化（可选——没有它 display_callback 也能工作） ---
        client = None
        output_stream = None
        voice_id = DEFAULT_ELEVENLABS_VOICE_ID
        model_id = DEFAULT_ELEVENLABS_STREAMING_MODEL_ID

        tts_config = _load_tts_config()
        el_config = tts_config.get("elevenlabs", {})
        voice_id = el_config.get("voice_id", voice_id)
        model_id = el_config.get("streaming_model_id",
                                 el_config.get("model_id", model_id))
        # 流式路径的逐句上限。针对 *streaming* model_id（默认是
        # eleven_flash_v2_5 = 40k 字符）查找上限，而不是同步 model_id。
        # 用户的覆盖值（tts.elevenlabs.max_text_length）仍然优先。
        stream_max_len = _resolve_max_text_length(
            "elevenlabs",
            {**tts_config, "elevenlabs": {**el_config, "model_id": model_id}},
        )

        api_key = (get_env_value("ELEVENLABS_API_KEY") or "")
        if not api_key:
            logger.warning("ELEVENLABS_API_KEY not set; streaming TTS audio disabled")
        else:
            try:
                ElevenLabs = _import_elevenlabs()
                client = ElevenLabs(api_key=api_key)
            except ImportError:
                logger.warning("elevenlabs package not installed; streaming TTS disabled")

            # 在本函数的整个生命周期内打开一个 sounddevice 输出流。
            # ElevenLabs 的 pcm_24000 产出的是 24 kHz、有符号 16-bit 小端的
            # 单声道 PCM。
            if client is not None:
                try:
                    sd = _import_sounddevice()
                    output_stream = sd.OutputStream(
                        samplerate=24000, channels=1, dtype="int16",
                    )
                    output_stream.start()
                except (ImportError, OSError) as exc:
                    logger.debug("sounddevice not available: %s", exc)
                    output_stream = None
                except Exception as exc:
                    logger.warning("sounddevice OutputStream failed: %s", exc)
                    output_stream = None

        sentence_buf = ""
        min_sentence_len = 20
        long_flush_len = 100
        queue_timeout = 0.5
        _spoken_sentences: list[str] = []  # 记录已朗读的句子，用于跳过重复
        # 用于从缓冲区中剥离完整 <think>...</think> 块的正则
        _think_block_re = re.compile(r'<think[\s>].*?</think>', flags=re.DOTALL)

        def _speak_sentence(sentence: str):
            """显示句子，并可选地生成音频 + 播放。"""
            if stop_event.is_set():
                return
            cleaned = _strip_markdown_for_tts(sentence).strip()
            if not cleaned:
                return
            # 跳过重复/近似重复的句子（LLM 重复输出）
            cleaned_lower = cleaned.lower().rstrip(".!,")
            for prev in _spoken_sentences:
                if prev.lower().rstrip(".!,") == cleaned_lower:
                    return
            _spoken_sentences.append(cleaned)
            # 在 TTS 处理之前，先把原始句子显示到屏幕上
            if display_callback is not None:
                display_callback(sentence)
            # 如果没有可用的 TTS 客户端，则跳过音频生成
            if client is None:
                return
            # 截断过长的句子（ElevenLabs 流式路径）
            if len(cleaned) > stream_max_len:
                cleaned = cleaned[:stream_max_len]
            try:
                audio_iter = client.text_to_speech.convert(
                    text=cleaned,
                    voice_id=voice_id,
                    model_id=model_id,
                    output_format="pcm_24000",
                )
                if output_stream is not None:
                    for chunk in audio_iter:
                        if stop_event.is_set():
                            break
                        import numpy as _np
                        audio_array = _np.frombuffer(chunk, dtype=_np.int16)
                        output_stream.write(audio_array.reshape(-1, 1))
                else:
                    # 兜底：把数据块写入临时文件，再用系统播放器播放
                    _play_via_tempfile(audio_iter, stop_event)
            except Exception as exc:
                logger.warning("Streaming TTS sentence failed: %s", exc)

        def _play_via_tempfile(audio_iter, stop_evt):
            """把 PCM 数据块写入临时 WAV 文件并播放。"""
            tmp_path = None
            try:
                import wave
                tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                tmp_path = tmp.name
                with wave.open(tmp, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)  # 16-bit
                    wf.setframerate(24000)
                    for chunk in audio_iter:
                        if stop_evt.is_set():
                            break
                        wf.writeframes(chunk)
                from tools.voice_mode import play_audio_file
                play_audio_file(tmp_path)
            except Exception as exc:
                logger.warning("Temp-file TTS fallback failed: %s", exc)
            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

        while not stop_event.is_set():
            # 从队列读取下一个增量
            try:
                delta = text_queue.get(timeout=queue_timeout)
            except queue.Empty:
                # 超时：如果已经累积了较长的缓冲区，则冲刷它
                if len(sentence_buf) > long_flush_len:
                    _speak_sentence(sentence_buf)
                    sentence_buf = ""
                continue

            if delta is None:
                # 文本结束哨兵：剥离剩余的 think 块，然后冲刷
                sentence_buf = _think_block_re.sub('', sentence_buf)
                if sentence_buf.strip():
                    _speak_sentence(sentence_buf)
                break

            sentence_buf += delta

            # --- Think 块过滤 ---
            # 从缓冲区中剥离完整的 <think>...</think> 块。
            # 即使标签跨越多个增量也能正确工作。
            sentence_buf = _think_block_re.sub('', sentence_buf)

            # 如果末尾存在不完整的 <think 标签，则等待更多数据
            # 再提取句子（闭合标签可能随后到达）。
            if '<think' in sentence_buf and '</think>' not in sentence_buf:
                continue

            # 检查句子边界
            while True:
                m = _SENTENCE_BOUNDARY_RE.search(sentence_buf)
                if m is None:
                    break
                end_pos = m.end()
                sentence = sentence_buf[:end_pos]
                sentence_buf = sentence_buf[end_pos:]
                # 把短片段合并到下一个句子里
                if len(sentence.strip()) < min_sentence_len:
                    sentence_buf = sentence + sentence_buf
                    break
                _speak_sentence(sentence)

        # 排空队列里剩余的条目
        while True:
            try:
                text_queue.get_nowait()
            except queue.Empty:
                break

        # output_stream 会在下面的 finally 块中关闭

    except Exception as exc:
        logger.warning("Streaming TTS pipeline error: %s", exc)
    finally:
        # 始终关闭音频输出流，避免锁住设备
        if output_stream is not None:
            try:
                output_stream.stop()
                output_stream.close()
            except Exception:
                pass
        tts_done_event.set()


# ===========================================================================
# 主入口——快速诊断
# ===========================================================================
if __name__ == "__main__":
    print("🔊 Text-to-Speech Tool Module")
    print("=" * 50)

    def _check(importer, label):
        try:
            importer()
            return True
        except ImportError:
            return False

    print("\nProvider availability:")
    print(f"  Edge TTS:   {'installed' if _check(_import_edge_tts, 'edge') else 'not installed (pip install edge-tts)'}")
    print(f"  ElevenLabs: {'installed' if _check(_import_elevenlabs, 'el') else 'not installed (pip install elevenlabs)'}")
    print(f"    API Key:  {'set' if get_env_value('ELEVENLABS_API_KEY') else 'not set'}")
    print(f"  OpenAI:     {'installed' if _check(_import_openai_client, 'oai') else 'not installed'}")
    print(
        "    API Key:  "
        f"{'set' if resolve_openai_audio_api_key() else 'not set (VOICE_TOOLS_OPENAI_KEY or OPENAI_API_KEY)'}"
    )
    print(f"  MiniMax:    {'API key set' if get_env_value('MINIMAX_API_KEY') else 'not set (MINIMAX_API_KEY)'}")
    print(f"  Piper:      {'installed' if _check_piper_available() else 'not installed (pip install piper-tts)'}")
    print(f"  ffmpeg:     {'✅ found' if _has_ffmpeg() else '❌ not found (needed for Telegram Opus)'}")
    print(f"\n  Output dir: {DEFAULT_OUTPUT_DIR}")

    config = _load_tts_config()
    provider = _get_provider(config)
    print(f"  Configured provider: {provider}")


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
from tools.registry import registry, tool_error

TTS_SCHEMA = {
    "name": "text_to_speech",
    "description": "Convert text to speech audio. Returns a MEDIA: path that the platform delivers as native audio. Compatible providers render as a voice bubble on Telegram; otherwise audio is sent as a regular attachment. In CLI mode, saves to ~/voice-memos/. Voice and provider are user-configured (built-in providers like edge/openai or custom command providers under tts.providers.<name>), not model-selected.",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The text to convert to speech. Provider-specific character caps apply and are enforced automatically (OpenAI 4096, xAI 15000, MiniMax 10000, ElevenLabs 5k-40k depending on model); over-long input is truncated."
            },
            "output_path": {
                "type": "string",
                "description": f"Optional custom file path to save the audio. Defaults to {display_hermes_home()}/audio_cache/<timestamp>.mp3"
            }
        },
        "required": ["text"]
    }
}

registry.register(
    name="text_to_speech",
    toolset="tts",
    schema=TTS_SCHEMA,
    handler=lambda args, **kw: text_to_speech_tool(
        text=args.get("text", ""),
        output_path=args.get("output_path")),
    check_fn=check_tts_requirements,
    emoji="🔊",
)
