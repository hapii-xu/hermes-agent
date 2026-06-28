#!/usr/bin/env python3
"""
语音转文字工具模块

提供六种语音转文字（speech-to-text）服务商：

  - **local**（默认，免费）—— 本地运行的 faster-whisper，无需 API key。
    首次使用时自动下载模型（``base`` 约 150 MB）。
  - **groq**（免费额度）—— Groq Whisper API，需要 ``GROQ_API_KEY``。
  - **openai**（付费）—— OpenAI Whisper API，需要 ``VOICE_TOOLS_OPENAI_KEY``。
  - **mistral** —— Mistral Voxtral Transcribe API，需要 ``MISTRAL_API_KEY``。
  - **xai** —— xAI Grok STT API，需要 ``XAI_API_KEY``。准确率高，
    支持逆向文本归一化（Inverse Text Normalization）、说话人分离（diarization）、21 种语言。
  - **elevenlabs** —— ElevenLabs Scribe API，需要 ``ELEVENLABS_API_KEY``。

被消息网关（messaging gateway）用于自动转写用户在 Telegram、Discord、
WhatsApp、Slack 和 Signal 上发送的语音消息。

支持的输入格式：mp3、mp4、mpeg、mpga、m4a、wav、webm、ogg、aac

用法::

    from tools.transcription_tools import transcribe_audio

    result = transcribe_audio("/path/to/audio.ogg")
    if result["success"]:
        print(result["transcript"])
"""

import logging
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Dict, Any
from urllib.parse import urljoin

from utils import is_truthy_value
from tools.managed_tool_gateway import resolve_managed_tool_gateway
from tools.tool_backend_helpers import (
    managed_nous_tools_enabled,
    nous_tool_gateway_unavailable_message,
    resolve_openai_audio_api_key,
)

logger = logging.getLogger(__name__)

def get_env_value(name, default=None):
    """通过实时的 config 模块读取环境变量。

    测试可能会在本模块被导入之前 monkeypatch 并随后恢复
    ``hermes_cli.config.get_env_value``。在调用时解析该辅助函数，
    这样 STT 在测试进程的剩余时间里不会持有一个陈旧的已导入函数。
    """
    try:
        from hermes_cli.config import get_env_value as _get_env_value
    except ImportError:
        return os.getenv(name, default)
    value = _get_env_value(name)
    return default if value is None else value

# ---------------------------------------------------------------------------
# 可选导入 —— 优雅降级
# ---------------------------------------------------------------------------

import importlib.util as _ilu


def _safe_find_spec(module_name: str) -> bool:
    try:
        return _ilu.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return module_name in globals() or module_name in os.sys.modules


_HAS_FASTER_WHISPER = _safe_find_spec("faster_whisper")
_HAS_OPENAI = _safe_find_spec("openai")
_HAS_MISTRAL = _safe_find_spec("mistralai")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_PROVIDER = "local"
DEFAULT_LOCAL_MODEL = "base"
DEFAULT_LOCAL_STT_LANGUAGE = "en"
DEFAULT_STT_MODEL = os.getenv("STT_OPENAI_MODEL", "whisper-1")
DEFAULT_GROQ_STT_MODEL = os.getenv("STT_GROQ_MODEL", "whisper-large-v3-turbo")
DEFAULT_MISTRAL_STT_MODEL = os.getenv("STT_MISTRAL_MODEL", "voxtral-mini-latest")
DEFAULT_ELEVENLABS_STT_MODEL = os.getenv("STT_ELEVENLABS_MODEL", "scribe_v2")
LOCAL_STT_COMMAND_ENV = "HERMES_LOCAL_STT_COMMAND"
LOCAL_STT_LANGUAGE_ENV = "HERMES_LOCAL_STT_LANGUAGE"
COMMON_LOCAL_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")

GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
OPENAI_BASE_URL = os.getenv("STT_OPENAI_BASE_URL", "https://api.openai.com/v1")
XAI_STT_BASE_URL = os.getenv("XAI_STT_BASE_URL", "https://api.x.ai/v1")
ELEVENLABS_STT_BASE_URL = os.getenv("ELEVENLABS_STT_BASE_URL", "https://api.elevenlabs.io/v1")

SUPPORTED_FORMATS = {".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".ogg", ".aac", ".flac"}
LOCAL_NATIVE_AUDIO_FORMATS = {".wav", ".aiff", ".aif"}
MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB

# 用于自动纠错的已知模型集合
OPENAI_MODELS = {"whisper-1", "gpt-4o-mini-transcribe", "gpt-4o-transcribe"}
GROQ_MODELS = {"whisper-large-v3", "whisper-large-v3-turbo", "distil-whisper-large-v3-en"}

# 本地模型的单例 —— 只加载一次，后续调用复用
_local_model: Optional[object] = None
_local_model_name: Optional[str] = None

# ---------------------------------------------------------------------------
# 配置辅助函数
# ---------------------------------------------------------------------------



def _load_stt_config() -> dict:
    """从用户配置中加载 ``stt`` 段，失败时回退到默认值。"""
    try:
        from hermes_cli.config import load_config
        return load_config().get("stt", {})
    except Exception:
        return {}


def is_stt_enabled(stt_config: Optional[dict] = None) -> bool:
    """返回配置中是否启用了 STT。"""
    if stt_config is None:
        stt_config = _load_stt_config()
    enabled = stt_config.get("enabled", True)
    return is_truthy_value(enabled, default=True)


def _has_openai_audio_backend() -> bool:
    """当 OpenAI 音频可使用配置凭证、环境变量凭证或托管网关时返回 True。"""
    try:
        _resolve_openai_audio_client_config()
        return True
    except ValueError:
        return False


def _find_binary(binary_name: str) -> Optional[str]:
    """查找本地可执行文件，既检查常见的 Homebrew/本地前缀，也检查 PATH。"""
    for directory in COMMON_LOCAL_BIN_DIRS:
        candidate = Path(directory) / binary_name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(binary_name)


def _find_ffmpeg_binary() -> Optional[str]:
    return _find_binary("ffmpeg")


def _find_whisper_binary() -> Optional[str]:
    return _find_binary("whisper")


def _get_local_command_template() -> Optional[str]:
    configured = os.getenv(LOCAL_STT_COMMAND_ENV, "").strip()
    if configured:
        return configured

    whisper_binary = _find_whisper_binary()
    if whisper_binary:
        quoted_binary = shlex.quote(whisper_binary)
        return (
            f"{quoted_binary} {{input_path}} --model {{model}} --output_format txt "
            "--output_dir {output_dir} --language {language}"
        )
    return None


def _has_local_command() -> bool:
    return _get_local_command_template() is not None


def _normalize_local_model(model_name: Optional[str]) -> str:
    """返回一个有效的 faster-whisper 模型尺寸，把仅限云端的名称映射为默认值。

    像 OpenAI 这样的云端服务商使用 ``whisper-1`` 之类的名称，这些名称
    对 faster-whisper（它期望 ``tiny``、``base``、``small``、``medium``
    或 ``large-v*``）无效。当检测到此类名称时，我们回退到默认的本地模型
    并发出一条警告，让用户知道发生了什么。
    """
    if not model_name or model_name in OPENAI_MODELS or model_name in GROQ_MODELS:
        if model_name and (model_name in OPENAI_MODELS or model_name in GROQ_MODELS):
            logger.warning(
                "STT model '%s' is a cloud-only name and cannot be used with the local "
                "provider. Falling back to '%s'. Set stt.local.model to a valid "
                "faster-whisper size (tiny, base, small, medium, large-v3).",
                model_name,
                DEFAULT_LOCAL_MODEL,
            )
        return DEFAULT_LOCAL_MODEL
    return model_name


def _normalize_local_command_model(model_name: Optional[str]) -> str:
    return _normalize_local_model(model_name)


def _try_lazy_install_stt() -> bool:
    """尝试延迟安装 faster-whisper，成功则返回 True。

    模块级 ``_HAS_FASTER_WHISPER`` 标志在导入时设置并被缓存。如果在启动时
    该包尚未安装，调用 ``ensure()`` 会安装它。本函数在安装完成后动态地
    重新检查，这样服务商就能立即使用它而无需重启进程。
    """
    try:
        from tools.lazy_deps import ensure
        # prompt=False：永不触发阻塞式的 input() 提示。
        # 在交互式 CLI 下 prompt_toolkit 占用了 stdin，因此裸 input()
        # 会卡死终端（#40490）。该安装已由 security.allow_lazy_installs 门控，
        # 所以能走到这里本身就是用户主动选择的。
        ensure("stt.faster_whisper", prompt=False)
        # 安装后动态重新检查
        import importlib.util as _iu
        if _iu.find_spec("faster_whisper"):
            return True
    except Exception as exc:
        logger.debug("Lazy install of faster-whisper failed: %s", exc)
    return False


# 本模块中有原生处理器的 6 个 STT 服务商名称。
# 与 ``agent.transcription_registry._BUILTIN_NAMES`` 保持同步 ——
# 若二者出现偏差，回归测试会失败。issue #30398 式后续的插件钩子
# 会拒绝在此任一名称下注册的插件；``transcribe_audio`` 中的分发器
# 也会防御性地对它们短路处理。
BUILTIN_STT_PROVIDERS = frozenset({
    "local",
    "local_command",
    "groq",
    "openai",
    "mistral",
    "xai",
})


# ---------------------------------------------------------------------------
# 命令行服务商注册表（``stt.providers.<name>: type: command``）
# ---------------------------------------------------------------------------
#
# 镜像 PR #17843 中交付的 TTS 命令行服务商注册表 —— 相同的占位符语法、
# 相同的 shell 引号感知渲染、相同的超时时进程树终止。让任何
# whisper CLI / ASR CLI / curl 管道都能零 Python 代码地成为 STT 后端。
#
# 解析顺序：
#   1. 内置（``local``、``local_command``、``groq``、``openai``、
#      ``mistral``、``xai``）        → 原生处理器。**总是优先。**
#   2. ``stt.providers.<name>: type: command``  → 命令行服务商运行器。
#   3. 插件注册的 TranscriptionProvider  → 插件分发。
#   4. 无匹配                           → "No STT provider available"。
#
# 单环境变量 ``HERMES_LOCAL_STT_COMMAND`` 这个逃生舱通过内置的
# ``local_command`` 路径保持原样不动。当你需要多个 shell 驱动的 STT 引擎，
# 或想要一个能通过 config.yaml 中的 ``stt.provider`` 来选择的具名服务商时，
# 就使用命令行服务商注册表。
DEFAULT_COMMAND_STT_TIMEOUT_SECONDS = 300
DEFAULT_COMMAND_STT_LANGUAGE = "en"
DEFAULT_COMMAND_STT_OUTPUT_FORMAT = "txt"
COMMAND_STT_OUTPUT_FORMATS = frozenset({"txt", "json", "srt", "vtt"})


def _get_stt_section(stt_config: Dict[str, Any], name: str) -> Dict[str, Any]:
    """如果某 stt 子段是 dict 就返回它，否则返回空 dict。"""
    if not isinstance(stt_config, dict):
        return {}
    section = stt_config.get(name)
    return section if isinstance(section, dict) else {}


def _get_named_stt_provider_config(
    stt_config: Dict[str, Any],
    name: str,
) -> Dict[str, Any]:
    """返回用户声明的 STT 命令行服务商的配置 dict。

    先查找 ``stt.providers.<name>``（规范位置），找不到则回退到
    ``stt.<name>``，这样遵循内置布局的用户仍能正常工作。当该服务商
    未声明时返回空 dict。

    内置名称在这里不做特殊处理 —— 调用方在查询本函数之前就已对其短路，
    并且 ``_is_command_stt_provider_config`` 要求显式的 ``command:`` 值，
    因此像 ``stt.openai``（它有 ``model``/``language`` 但没有 ``command``）
    这样的内置段不会被误当作命令行服务商。
    """
    providers = _get_stt_section(stt_config, "providers")
    section = providers.get(name) if isinstance(providers, dict) else None
    if isinstance(section, dict):
        return section
    # 向后兼容：对用户声明的服务商也允许 ``stt.<name>``，
    # 但仅当名称不是内置名称时（这样用户的 ``stt.openai`` 段
    # 仍表示 OpenAI 服务商，而不是自定义命令）。
    if name.lower() not in BUILTIN_STT_PROVIDERS:
        legacy = _get_stt_section(stt_config, name)
        if legacy:
            return legacy
    return {}


def _is_command_stt_provider_config(config: Dict[str, Any]) -> bool:
    """当 *config* 声明了一个 command 类型的 STT 服务商时返回 True。"""
    if not isinstance(config, dict):
        return False
    ptype = str(config.get("type") or "").strip().lower()
    if ptype and ptype != "command":
        return False
    command = config.get("command")
    return isinstance(command, str) and bool(command.strip())


def _resolve_command_stt_provider_config(
    provider: str,
    stt_config: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """当 *provider* 解析为 command 类型时返回该服务商配置。

    内置服务商名称会被拒绝（它们有原生处理器）。当名称是内置、
    ``"none"``、未知或不是 command 类型时返回 None。
    """
    if not provider:
        return None
    key = provider.lower().strip()
    if key in BUILTIN_STT_PROVIDERS or key == "none":
        return None
    config = _get_named_stt_provider_config(stt_config, key)
    if _is_command_stt_provider_config(config):
        return config
    return None


def _iter_command_stt_providers(stt_config: Dict[str, Any]):
    """为每个已声明的 command 类型 STT 服务商生成 (name, config) 对。"""
    if not isinstance(stt_config, dict):
        return
    providers = _get_stt_section(stt_config, "providers")
    for name, cfg in (providers or {}).items():
        if isinstance(name, str) and name.lower() not in BUILTIN_STT_PROVIDERS:
            if _is_command_stt_provider_config(cfg):
                yield name, cfg


def _has_any_command_stt_provider(stt_config: Optional[Dict[str, Any]] = None) -> bool:
    """当配置了任意 command 类型的 STT 服务商时返回 True。"""
    if stt_config is None:
        stt_config = _load_stt_config()
    for _name, _cfg in _iter_command_stt_providers(stt_config):
        return True
    return False


def _get_command_stt_timeout(config: Dict[str, Any]) -> float:
    """返回以秒为单位的超时时间，无效时回退。"""
    raw = config.get("timeout", config.get("timeout_seconds", DEFAULT_COMMAND_STT_TIMEOUT_SECONDS))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(DEFAULT_COMMAND_STT_TIMEOUT_SECONDS)
    if value <= 0:
        return float(DEFAULT_COMMAND_STT_TIMEOUT_SECONDS)
    return value


def _get_command_stt_output_format(config: Dict[str, Any]) -> str:
    """返回校验过的输出格式（txt/json/srt/vtt）。"""
    raw = (
        config.get("format")
        or config.get("output_format")
        or DEFAULT_COMMAND_STT_OUTPUT_FORMAT
    )
    fmt = str(raw).lower().strip().lstrip(".")
    return fmt if fmt in COMMAND_STT_OUTPUT_FORMATS else DEFAULT_COMMAND_STT_OUTPUT_FORMAT


def _shell_quote_context_stt(command_template: str, position: int) -> Optional[str]:
    """返回在 *position* 之前生效的 shell 引号字符。

    镜像 ``tools.tts_tool._shell_quote_context`` —— 保留为本地实现，
    以避免跨模块导入私有辅助函数。当处于引号包围区域内时返回 ``"'"`` / ``'"'``，
    处于裸上下文时返回 ``None``。
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


def _quote_command_stt_placeholder(value: str, quote_context: Optional[str]) -> str:
    """为 shell 命令模板中某位置的占位符值做引号转义。

    镜像 ``tools.tts_tool._quote_command_tts_placeholder``。
    """
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


def _render_command_stt_template(
    command_template: str,
    placeholders: Dict[str, str],
) -> str:
    """替换受支持的占位符，同时保留 ``{{`` / ``}}``。

    镜像 ``tools.tts_tool._render_command_tts_template``。占位符是 shell
    引号感知的：单引号内的 ``{voice}`` 会做单引号安全转义，双引号内会做
    ``$``/`` ` ``/`` " `` 转义，引号外则使用 ``shlex.quote``。连续双花括号
    ``{{`` 和 ``}}`` 会被保留为字面量 ``{`` / ``}``，方便用户在命令里
    嵌入 JSON 片段。
    """
    import re

    names = "|".join(re.escape(name) for name in placeholders)
    pattern = re.compile(
        rf"(?<!\$)(?:\{{\{{(?P<double>{names})\}}\}}|\{{(?P<single>{names})\}})"
    )
    replacements: list[tuple[str, str]] = []

    def replace_match(match: "re.Match[str]") -> str:
        name = match.group("double") or match.group("single")
        token = f"__HERMES_STT_PLACEHOLDER_{len(replacements)}__"
        replacements.append((
            token,
            _quote_command_stt_placeholder(
                placeholders[name],
                _shell_quote_context_stt(command_template, match.start()),
            ),
        ))
        return token

    rendered = pattern.sub(replace_match, command_template)
    rendered = rendered.replace("{{", "{").replace("}}", "}")
    for token, value in replacements:
        rendered = rendered.replace(token, value)
    return rendered


def _terminate_command_stt_process_tree(proc: subprocess.Popen) -> None:
    """尽力终止一个 shell 进程及其所有子进程。

    镜像 ``tools.tts_tool._terminate_command_tts_process_tree``。
    """
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

    try:
        import psutil  # type: ignore
    except ImportError:
        # psutil 是可选依赖 —— 回退到单进程的 terminate/kill
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        return

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


def _run_command_stt(command: str, timeout: float) -> subprocess.CompletedProcess:
    """运行命令行服务商的 shell 命令，并在超时时清理整个进程树。

    镜像 ``tools.tts_tool._run_command_tts``。
    """
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
        _terminate_command_stt_process_tree(proc)
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


def _read_command_stt_output(output_path: Path, stdout: str, fmt: str) -> str:
    """从命令行服务商调用的结果中返回转写文本。

    解析顺序：
      1. 如果 ``output_path`` 存在且非空 → 读取它（原始文本）。
      2. 否则如果 ``stdout`` 非空 → 使用 stdout（让用户可以写 curl 式的
         单行命令，把转写文本输出到 stdout 而不是写入文件）。
      3. 否则 → 抛出 RuntimeError（没有产生可用的输出）。

    对于 JSON 格式，我们仍然返回原始字节 —— 提取 ``text`` 字段不在本函数
    职责范围内；用户要么配置 ``format: txt``，要么在下游自行解析 JSON。
    （与 TTS 同样的取舍：运行器不试图在输出结构上耍聪明。）
    """
    if output_path.exists():
        try:
            content = output_path.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError:
            content = output_path.read_bytes().decode("utf-8", errors="replace").strip()
        if content:
            return content
    if stdout and stdout.strip():
        return stdout.strip()
    raise RuntimeError(
        f"Command STT provider wrote no output file at {output_path} "
        f"and produced no stdout"
    )


def _transcribe_command_stt(
    file_path: str,
    provider_name: str,
    config: Dict[str, Any],
    stt_config: Dict[str, Any],
    model_override: Optional[str] = None,
) -> Dict[str, Any]:
    """通过用户声明的 ``stt.providers.<name>: type: command`` 进行转写。

    占位符语法：

    | 占位符            | 替换为                                                    |
    |-------------------|-----------------------------------------------------------|
    | ``{input_path}``  | 音频文件的绝对路径（原始位置）                              |
    | ``{output_path}`` | 服务商应将转写文本写入的绝对路径                            |
    | ``{output_dir}``  | ``{output_path}`` 的父目录                                 |
    | ``{format}``      | 配置的输出格式（``txt`` / ``json`` / ``srt`` / ``vtt``）   |
    | ``{language}``    | 配置的语言代码（默认 ``en``）                              |
    | ``{model}``       | 配置的模型 id（未设置时为空）                              |

    所有占位符都是 shell 引号感知的（见 ``_render_command_stt_template``）。
    连续双花括号 ``{{`` 和 ``}}`` 会被保留为字面花括号。

    返回标准的转写响应封装（``success``、``transcript``、``provider``、``error``）。
    """
    command_template = str(config.get("command") or "").strip()
    if not command_template:
        return {
            "success": False,
            "transcript": "",
            "provider": provider_name,
            "error": f"stt.providers.{provider_name}.command is not configured",
        }

    audio = Path(file_path).expanduser()
    if not audio.exists():
        return {
            "success": False,
            "transcript": "",
            "provider": provider_name,
            "error": f"Audio file not found: {file_path}",
        }

    timeout = _get_command_stt_timeout(config)
    output_format = _get_command_stt_output_format(config)
    language = (
        config.get("language")
        or stt_config.get("language")
        or DEFAULT_COMMAND_STT_LANGUAGE
    )
    model = model_override or config.get("model") or ""

    try:
        with tempfile.TemporaryDirectory(prefix=f"hermes-cmd-stt-{provider_name}-") as tmpdir:
            output_path = Path(tmpdir) / f"transcript.{output_format}"
            placeholders = {
                "input_path": str(audio.resolve()),
                "output_path": str(output_path),
                "output_dir": str(output_path.parent),
                "format": output_format,
                "language": str(language),
                "model": str(model),
            }
            command = _render_command_stt_template(command_template, placeholders)
            logger.info(
                "Transcribing %s via command STT provider '%s'...",
                audio.name, provider_name,
            )
            try:
                result = _run_command_stt(command, timeout)
            except subprocess.TimeoutExpired:
                return {
                    "success": False,
                    "transcript": "",
                    "provider": provider_name,
                    "error": (
                        f"STT command provider '{provider_name}' timed out after "
                        f"{timeout:g}s"
                    ),
                }
            except subprocess.CalledProcessError as exc:
                detail_parts = []
                if exc.stderr:
                    detail_parts.append(f"stderr: {exc.stderr.strip()}")
                if exc.stdout:
                    detail_parts.append(f"stdout: {exc.stdout.strip()}")
                detail = "; ".join(detail_parts) or "no command output"
                return {
                    "success": False,
                    "transcript": "",
                    "provider": provider_name,
                    "error": (
                        f"STT command provider '{provider_name}' exited with code "
                        f"{exc.returncode}: {detail}"
                    ),
                }

            try:
                transcript_text = _read_command_stt_output(
                    output_path, result.stdout or "", output_format,
                )
            except RuntimeError as exc:
                return {
                    "success": False,
                    "transcript": "",
                    "provider": provider_name,
                    "error": str(exc),
                }

    except OSError as exc:
        return {
            "success": False,
            "transcript": "",
            "provider": provider_name,
            "error": f"STT command provider '{provider_name}' failed: {exc}",
        }

    logger.info(
        "Transcribed %s via command STT provider '%s' (%d chars)",
        audio.name, provider_name, len(transcript_text),
    )
    return {
        "success": True,
        "transcript": transcript_text,
        "provider": provider_name,
    }


def _get_provider(stt_config: dict) -> str:
    """决定使用哪个 STT 服务商。

    当配置中显式设置了 ``stt.provider`` 时，尊重该选择 —— 不会无声地
    回退到云端。当未配置任何服务商时，自动检测顺序为：
    local > groq（免费）> openai（付费）。
    """
    if not is_stt_enabled(stt_config):
        return "none"

    explicit = "provider" in stt_config
    provider = stt_config.get("provider", DEFAULT_PROVIDER)

    # --- 显式服务商：尊重用户的选择 -------------------------------------

    if explicit:
        if provider == "local":
            if _HAS_FASTER_WHISPER:
                return "local"
            if _has_local_command():
                return "local_command"
            # 在放弃之前先尝试延迟安装
            if _try_lazy_install_stt():
                return "local"
            logger.warning(
                "STT provider 'local' configured but unavailable "
                "(install faster-whisper or set HERMES_LOCAL_STT_COMMAND)"
            )
            return "none"

        if provider == "local_command":
            if _has_local_command():
                return "local_command"
            if _HAS_FASTER_WHISPER:
                logger.info("Local STT command unavailable, using local faster-whisper")
                return "local"
            logger.warning(
                "STT provider 'local_command' configured but unavailable"
            )
            return "none"

        if provider == "groq":
            if _HAS_OPENAI and get_env_value("GROQ_API_KEY"):
                return "groq"
            logger.warning(
                "STT provider 'groq' configured but GROQ_API_KEY not set"
            )
            return "none"

        if provider == "openai":
            if _HAS_OPENAI and _has_openai_audio_backend():
                return "openai"
            logger.warning(
                "STT provider 'openai' configured but no API key available"
            )
            return "none"

        if provider == "mistral":
            if _HAS_MISTRAL and get_env_value("MISTRAL_API_KEY"):
                return "mistral"
            logger.warning(
                "STT provider 'mistral' configured but mistralai package "
                "not installed or MISTRAL_API_KEY not set"
            )
            return "none"

        if provider == "xai":
            from tools.xai_http import resolve_xai_http_credentials

            if resolve_xai_http_credentials().get("api_key"):
                return "xai"
            logger.warning(
                "STT provider 'xai' configured but no xAI credentials are available"
            )
            return "none"

        if provider == "elevenlabs":
            if get_env_value("ELEVENLABS_API_KEY"):
                return "elevenlabs"
            logger.warning(
                "STT provider 'elevenlabs' configured but ELEVENLABS_API_KEY not set"
            )
            return "none"

        return provider  # 未知 —— 让它在下游失败

    # --- 自动检测（无显式服务商）：local > groq > openai > xai > elevenlabs -
    # 当 `mistralai` 在 PyPI 被隔离期间（2026-05-12 的恶意 2.4.6 发布），
    # 有意跳过 mistral。

    if _HAS_FASTER_WHISPER:
        return "local"
    if _has_local_command():
        return "local_command"
    # 在回退到云端服务商之前先尝试延迟安装
    if _try_lazy_install_stt():
        return "local"
    if _HAS_OPENAI and get_env_value("GROQ_API_KEY"):
        logger.info("No local STT available, using Groq Whisper API")
        return "groq"
    if _HAS_OPENAI and _has_openai_audio_backend():
        logger.info("No local STT available, using OpenAI Whisper API")
        return "openai"
    # 只有当 SDK 已存在时才自动选择 Mistral —— 在被动自动检测期间
    # 不触发延迟安装。显式的 `provider: mistral`（见上文）会在首次
    # 转写调用时做延迟安装。
    if _HAS_MISTRAL and get_env_value("MISTRAL_API_KEY"):
        logger.info("No local STT available, using Mistral Voxtral Transcribe API")
        return "mistral"
    try:
        from tools.xai_http import resolve_xai_http_credentials

        if resolve_xai_http_credentials().get("api_key"):
            logger.info("No local STT available, using xAI Grok STT API")
            return "xai"
    except Exception:
        pass
    if get_env_value("ELEVENLABS_API_KEY"):
        logger.info("No local STT available, using ElevenLabs Scribe STT API")
        return "elevenlabs"
    return "none"


# ---------------------------------------------------------------------------
# 插件服务商分发（issue 对 #30398 的后续 —— STT 可插拔化）
# ---------------------------------------------------------------------------


def _dispatch_to_plugin_provider(
    file_path: str,
    provider: str,
    stt_config: Optional[Dict[str, Any]] = None,
    *,
    model: Optional[str] = None,
    language: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """把调用路由到插件注册的转写服务商，否则返回 None。

    分发成功时返回转写响应 dict，返回 ``None`` 表示回退到传统的
    "No STT provider available" 错误路径。

    此处强制执行的解析不变式：

    1. 内置服务商名称短路 —— 永远不会进入插件注册表。调用方
       （``transcribe_audio``）通过其既有的 elif 链处理 ``local``、
       ``groq``、``openai`` 等；本函数防御性地拒绝这些名称，
       因此即使某个插件以某种方式绕过了注册表的内置影子守卫，
       也不会在内置名称下被无声地分发。
    2. 在 ``stt.providers.<name>: type: command`` 下声明的同名
       command 类型服务商优先于插件。调用方在到达本函数之前已短路到
       命令运行器，但我们在此重新校验，这样调用方重构时不会无声地
       破坏该不变式（与 TTS PR #17843 的优先级规则一致）。
    3. 仅当 ``provider`` 匹配到某个已注册、且 ``name`` 等于配置值的
       :class:`TranscriptionProvider` 时才触发插件分发。对于没有注册
       插件的未知名称返回 None（调用方会呈现传统的 "No STT provider"
       消息）。
    4. 可用性门控：当匹配到的插件报告 ``is_available() == False``
       （缺少 API key、缺少可选 SDK 等）时，本函数返回一个标识该插件
       不可用的错误封装 —— 是错误封装而**不是** ``None`` —— 因为用户
       是通过 ``stt.provider`` 显式选择了该插件，通用的回退消息会
       产生误导。

    服务商抛出的异常会被捕获并转换为标准错误封装（与传统内置错误
    形状一致 —— 网关/CLI 调用方已经期望失败时得到
    ``{success: False, error: "...", transcript: ""}``）。
    """
    if not provider:
        return None
    key = provider.lower().strip()
    if key in BUILTIN_STT_PROVIDERS or key == "none":
        return None
    # 纵深防御：命令行服务商检查本应已经在调用方短路过了。
    # 如果存在同名命令配置，就退出以便命令路径胜出。
    if stt_config is not None and _is_command_stt_provider_config(
        _get_named_stt_provider_config(stt_config, key)
    ):
        return None
    try:
        from agent.transcription_registry import get_provider
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
        plugin_provider = get_provider(key)
        if plugin_provider is None:
            # 长生命周期会话可能在某个捆绑后端被打补丁进去之前，
            # 或在配置变更之前就已经发现了插件。在呈现回退之前，
            # 先强制刷新并重试一次。镜像 image_gen / browser 分发器的
            # 恢复模式。
            _ensure_plugins_discovered(force=True)
            plugin_provider = get_provider(key)
    except Exception as exc:  # noqa: BLE001 — discovery failure is non-fatal
        logger.debug("STT plugin dispatch skipped (discovery failed): %s", exc)
        return None
    if plugin_provider is None:
        return None

    # 可用性门控：当插件报告自己未配置（缺少 API key、缺少可选 SDK 等）时，
    # 呈现一个干净的错误封装，**而不是**回退到通用的 "No STT provider" 消息。
    # 用户在配置里显式设置了 ``stt.provider: <plugin>`` —— 呈现插件自身的
    # 可用性失败比通用的自动检测失败错误更具可操作性，并且避免把调用
    # 路由到一个即将崩溃得很惨的插件。
    #
    # 按 ABC 契约 ``is_available()`` 不得抛异常；但我们仍防御性处理，
    # 这样一个有 bug 的插件不会搞坏所有人的分发。
    try:
        available = plugin_provider.is_available()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "STT plugin provider '%s' is_available() raised: %s — "
            "treating as unavailable", key, exc, exc_info=True,
        )
        available = False
    if not available:
        logger.info(
            "STT plugin provider '%s' reports not available; returning "
            "unavailability envelope.", key,
        )
        return {
            "success": False,
            "transcript": "",
            "error": (
                f"STT plugin '{key}' is not available — check that its "
                "required credentials / dependencies are configured."
            ),
            "provider": key,
        }

    logger.info("Transcribing with plugin STT provider '%s'...", key)
    try:
        result = plugin_provider.transcribe(
            file_path,
            model=model,
            language=language,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "STT plugin provider '%s' raised: %s", key, exc, exc_info=True,
        )
        return {
            "success": False,
            "transcript": "",
            "error": f"STT plugin '{key}' raised: {exc}",
            "provider": key,
        }

    # 防御性处理：插件应返回符合契约的 dict。如果不是，就呈现一个清晰
    # 的错误封装，而不是把一个奇怪的对象泄露回网关。
    if not isinstance(result, dict):
        return {
            "success": False,
            "transcript": "",
            "error": f"STT plugin '{key}' returned a non-dict result",
            "provider": key,
        }
    # 如果插件忘了盖 provider 戳，就补上。
    result.setdefault("provider", key)
    return result


# ---------------------------------------------------------------------------
# 共享校验
# ---------------------------------------------------------------------------


def _validate_audio_file(file_path: str) -> Optional[Dict[str, Any]]:
    """校验音频文件。返回错误 dict，或没问题则返回 None。"""
    audio_path = Path(file_path)

    if os.path.islink(audio_path):
        return {"success": False, "transcript": "", "error": f"Path is a symbolic link: {file_path}"}
    if not audio_path.exists():
        return {"success": False, "transcript": "", "error": f"Audio file not found: {file_path}"}
    if not audio_path.is_file():
        return {"success": False, "transcript": "", "error": f"Path is not a file: {file_path}"}
    if audio_path.suffix.lower() not in SUPPORTED_FORMATS:
        return {
            "success": False,
            "transcript": "",
            "error": f"Unsupported format: {audio_path.suffix}. Supported: {', '.join(sorted(SUPPORTED_FORMATS))}",
        }
    try:
        file_size = audio_path.stat().st_size
        if file_size > MAX_FILE_SIZE:
            return {
                "success": False,
                "transcript": "",
                "error": f"File too large: {file_size / (1024*1024):.1f}MB (max {MAX_FILE_SIZE / (1024*1024):.0f}MB)",
            }
    except OSError as e:
        return {"success": False, "transcript": "", "error": f"Failed to access file: {e}"}

    return None

# ---------------------------------------------------------------------------
# 服务商：local（faster-whisper）
# ---------------------------------------------------------------------------


# 用于识别缺失/无法加载的 CUDA 运行时库的子串。当 ctranslate2
# （faster-whisper 的后端）无法 dlopen 其中之一时，"auto" 设备选择器
# 已经选定 CUDA，而模型已无法再使用 —— 我们回退到 CPU 并重新加载。
#
# 刻意收窄匹配：我们匹配库名 token 和 dlopen 措辞，这样就不会误伤像
# "CUDA out of memory" 这样的合法运行时失败 —— 那些应该呈现给用户，
# 而不是无声地回退到 CPU（32GB 音频在 CPU 上以 int8 跑也没用）。
_CUDA_LIB_ERROR_MARKERS = (
    "libcublas",
    "libcudnn",
    "libcudart",
    "cannot be loaded",
    "cannot open shared object",
    "no kernel image is available",
    "no CUDA-capable device",
    "CUDA driver version is insufficient",
)


def _looks_like_cuda_lib_error(exc: BaseException) -> bool:
    """启发式判断：这个异常是不是缺失/损坏的 CUDA 运行时库？

    ctranslate2 抛出的是普通 RuntimeError，消息形如
    ``Library libcublas.so.12 is not found or cannot be loaded``。我们想
    捕获的是缺失/无法加载的共享库以及驱动不匹配错误，而不是合法的
    运行时失败（"CUDA out of memory"、模型 bug 等）。
    """
    msg = str(exc)
    return any(marker in msg for marker in _CUDA_LIB_ERROR_MARKERS)


def _load_local_whisper_model(model_name: str):
    """加载 faster-whisper，并在 CUDA → CPU 之间优雅回退。

    faster-whisper 的 ``device="auto"`` 在 ctranslate2 wheel 携带 CUDA
    共享库时就会选择 CUDA，即便宿主机上并未安装 NVIDIA 运行时
    （``libcublas.so.12`` / ``libcudnn*``）—— 这种情况在未启用 CUDA-on-WSL
    的 WSL2、无头服务器以及纯 CPU 开发机上很常见。在这些宿主机上，
    加载本身有时能成功，而 dlopen 失败直到第一次 ``transcribe()`` 调用时
    才暴露出来。

    我们先尝试 ``auto``（可用时是快速的 CUDA 路径），一旦发生任何 CUDA
    库加载失败就回退到 CPU + int8。
    """
    from faster_whisper import WhisperModel
    try:
        return WhisperModel(model_name, device="auto", compute_type="auto")
    except Exception as exc:
        if not _looks_like_cuda_lib_error(exc):
            raise
        logger.warning(
            "faster-whisper CUDA load failed (%s) — falling back to CPU (int8). "
            "Install the NVIDIA CUDA runtime (libcublas/libcudnn) to use GPU.",
            exc,
        )
        return WhisperModel(model_name, device="cpu", compute_type="int8")


def _transcribe_local(file_path: str, model_name: str) -> Dict[str, Any]:
    """使用 faster-whisper 进行转写（本地、免费）。"""
    global _local_model, _local_model_name

    if not _HAS_FASTER_WHISPER:
        if not _try_lazy_install_stt():
            return {"success": False, "transcript": "", "error": "faster-whisper not installed"}

    try:
        # 延迟加载模型（首次使用时下载，'base' 约 150 MB）
        if _local_model is None or _local_model_name != model_name:
            logger.info("Loading faster-whisper model '%s' (first load downloads the model)...", model_name)
            _local_model = _load_local_whisper_model(model_name)
            _local_model_name = model_name

        # 语言优先级：config.yaml (stt.local.language) > 环境变量 > 自动检测。
        _forced_lang = (
            _load_stt_config().get("local", {}).get("language")
            or os.getenv(LOCAL_STT_LANGUAGE_ENV)
            or None
        )
        transcribe_kwargs = {"beam_size": 5}
        if _forced_lang:
            transcribe_kwargs["language"] = _forced_lang

        try:
            segments, info = _local_model.transcribe(file_path, **transcribe_kwargs)
            transcript = " ".join(segment.text.strip() for segment in segments)
        except Exception as exc:
            # CUDA 运行时库有时只在首次使用的 dlopen 阶段才失败，
            # 而这发生在模型成功加载之后。驱逐这个已损坏的缓存模型，
            # 在 CPU 上重新加载并重试一次。否则模块级的 `_local_model`
            # 会被污染，本进程此后每条语音消息都会以同样方式失败，
            # 直到重启进程。
            if not _looks_like_cuda_lib_error(exc):
                raise
            logger.warning(
                "faster-whisper CUDA runtime failed mid-transcribe (%s) — "
                "evicting cached model and retrying on CPU (int8).",
                exc,
            )
            _local_model = None
            _local_model_name = None
            from faster_whisper import WhisperModel
            _local_model = WhisperModel(model_name, device="cpu", compute_type="int8")
            _local_model_name = model_name
            segments, info = _local_model.transcribe(file_path, **transcribe_kwargs)
            transcript = " ".join(segment.text.strip() for segment in segments)

        logger.info(
            "Transcribed %s via local whisper (%s, lang=%s, %.1fs audio)",
            Path(file_path).name, model_name, info.language, info.duration,
        )

        return {"success": True, "transcript": transcript, "provider": "local"}

    except Exception as e:
        logger.error("Local transcription failed: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"Local transcription failed: {e}"}


def _prepare_local_audio(file_path: str, work_dir: str) -> tuple[Optional[str], Optional[str]]:
    """必要时为本地 CLI STT 归一化音频。"""
    audio_path = Path(file_path)
    if audio_path.suffix.lower() in LOCAL_NATIVE_AUDIO_FORMATS:
        return file_path, None

    ffmpeg = _find_ffmpeg_binary()
    if not ffmpeg:
        return None, "Local STT fallback requires ffmpeg for non-WAV inputs, but ffmpeg was not found"

    converted_path = os.path.join(work_dir, f"{audio_path.stem}.wav")
    command = [ffmpeg, "-y", "-i", file_path, converted_path]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        return converted_path, None
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg conversion timed out for %s", file_path)
        return None, "Audio conversion for local STT timed out"
    except subprocess.CalledProcessError as e:
        details = e.stderr.strip() or e.stdout.strip() or str(e)
        logger.error("ffmpeg conversion failed for %s: %s", file_path, details)
        return None, f"Failed to convert audio for local STT: {details}"


def _transcribe_local_command(file_path: str, model_name: str) -> Dict[str, Any]:
    """运行已配置的本地 STT 命令模板，并读回 .txt 转写文本。"""
    command_template = _get_local_command_template()
    if not command_template:
        return {
            "success": False,
            "transcript": "",
            "error": (
                f"{LOCAL_STT_COMMAND_ENV} not configured and no local whisper binary was found"
            ),
        }

    # 语言优先级：config.yaml (stt.local.language) > 环境变量 > "en" 默认值。
    language = (
        _load_stt_config().get("local", {}).get("language")
        or os.getenv(LOCAL_STT_LANGUAGE_ENV)
        or DEFAULT_LOCAL_STT_LANGUAGE
    )
    normalized_model = _normalize_local_command_model(model_name)

    try:
        with tempfile.TemporaryDirectory(prefix="hermes-local-stt-") as output_dir:
            prepared_input, prep_error = _prepare_local_audio(file_path, output_dir)
            if prep_error:
                return {"success": False, "transcript": "", "error": prep_error}

            command = command_template.format(
                input_path=shlex.quote(prepared_input),
                output_dir=shlex.quote(output_dir),
                language=shlex.quote(language),
                model=shlex.quote(normalized_model),
            )
            # 用户提供的模板（环境变量）可能包含 shell 语法；自动检测到的命令可以安全地用 list 模式。
            use_shell = bool(os.getenv(LOCAL_STT_COMMAND_ENV, "").strip())
            if use_shell:
                subprocess.run(command, shell=True, check=True, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
            else:
                subprocess.run(shlex.split(command), check=True, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
            

            txt_files = sorted(Path(output_dir).glob("*.txt"))
            if not txt_files:
                return {
                    "success": False,
                    "transcript": "",
                    "error": "Local STT command completed but did not produce a .txt transcript",
                }

            transcript_text = txt_files[0].read_text(encoding="utf-8").strip()
            logger.info(
                "Transcribed %s via local STT command (%s, %d chars)",
                Path(file_path).name,
                normalized_model,
                len(transcript_text),
            )
            return {"success": True, "transcript": transcript_text, "provider": "local_command"}

    except KeyError as e:
        return {
            "success": False,
            "transcript": "",
            "error": f"Invalid {LOCAL_STT_COMMAND_ENV} template, missing placeholder: {e}",
        }
    except subprocess.CalledProcessError as e:
        details = e.stderr.strip() or e.stdout.strip() or str(e)
        logger.error("Local STT command failed for %s: %s", file_path, details)
        return {"success": False, "transcript": "", "error": f"Local STT failed: {details}"}
    except Exception as e:
        logger.error("Unexpected error during local command transcription: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"Local transcription failed: {e}"}

# ---------------------------------------------------------------------------
# 服务商：groq（Whisper API —— 免费额度）
# ---------------------------------------------------------------------------


def _transcribe_groq(file_path: str, model_name: str) -> Dict[str, Any]:
    """使用 Groq Whisper API 进行转写（提供免费额度）。"""
    api_key = get_env_value("GROQ_API_KEY")
    if not api_key:
        return {"success": False, "transcript": "", "error": "GROQ_API_KEY not set"}

    if not _HAS_OPENAI:
        return {"success": False, "transcript": "", "error": "openai package not installed"}

    # 当调用方传入的是 OpenAI 专属模型时，自动纠错
    if model_name in OPENAI_MODELS:
        logger.info("Model %s not available on Groq, using %s", model_name, DEFAULT_GROQ_STT_MODEL)
        model_name = DEFAULT_GROQ_STT_MODEL

    try:
        from openai import OpenAI, APIError, APIConnectionError, APITimeoutError
        client = OpenAI(api_key=api_key, base_url=GROQ_BASE_URL, timeout=30, max_retries=0)
        try:
            with open(file_path, "rb") as audio_file:
                transcription = client.audio.transcriptions.create(
                    model=model_name,
                    file=audio_file,
                    response_format="text",
                )

            transcript_text = str(transcription).strip()
            logger.info("Transcribed %s via Groq API (%s, %d chars)",
                         Path(file_path).name, model_name, len(transcript_text))

            return {"success": True, "transcript": transcript_text, "provider": "groq"}
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    except PermissionError:
        return {"success": False, "transcript": "", "error": f"Permission denied: {file_path}"}
    except APIConnectionError as e:
        return {"success": False, "transcript": "", "error": f"Connection error: {e}"}
    except APITimeoutError as e:
        return {"success": False, "transcript": "", "error": f"Request timeout: {e}"}
    except APIError as e:
        return {"success": False, "transcript": "", "error": f"API error: {e}"}
    except Exception as e:
        logger.error("Groq transcription failed: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"Transcription failed: {e}"}

# ---------------------------------------------------------------------------
# 服务商：openai（Whisper API）
# ---------------------------------------------------------------------------


def _transcribe_openai(file_path: str, model_name: str) -> Dict[str, Any]:
    """使用 OpenAI Whisper API 进行转写（付费）。"""
    try:
        api_key, base_url = _resolve_openai_audio_client_config()
    except ValueError as exc:
        return {
            "success": False,
            "transcript": "",
            "error": str(exc),
        }

    if not _HAS_OPENAI:
        return {"success": False, "transcript": "", "error": "openai package not installed"}

    # 当调用方传入的是 Groq 专属模型时，自动纠错
    if model_name in GROQ_MODELS:
        logger.info("Model %s not available on OpenAI, using %s", model_name, DEFAULT_STT_MODEL)
        model_name = DEFAULT_STT_MODEL

    try:
        from openai import OpenAI, APIError, APIConnectionError, APITimeoutError
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=30, max_retries=0)
        try:
            with open(file_path, "rb") as audio_file:
                transcription = client.audio.transcriptions.create(
                    model=model_name,
                    file=audio_file,
                    response_format="text" if model_name == "whisper-1" else "json",
                )

            transcript_text = _extract_transcript_text(transcription)
            logger.info("Transcribed %s via OpenAI API (%s, %d chars)",
                         Path(file_path).name, model_name, len(transcript_text))

            return {"success": True, "transcript": transcript_text, "provider": "openai"}
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    except PermissionError:
        return {"success": False, "transcript": "", "error": f"Permission denied: {file_path}"}
    except APIConnectionError as e:
        return {"success": False, "transcript": "", "error": f"Connection error: {e}"}
    except APITimeoutError as e:
        return {"success": False, "transcript": "", "error": f"Request timeout: {e}"}
    except APIError as e:
        return {"success": False, "transcript": "", "error": f"API error: {e}"}
    except Exception as e:
        logger.error("OpenAI transcription failed: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"Transcription failed: {e}"}

# ---------------------------------------------------------------------------
# 服务商：mistral（Voxtral Transcribe API）
# ---------------------------------------------------------------------------


def _transcribe_mistral(file_path: str, model_name: str) -> Dict[str, Any]:
    """使用 Mistral Voxtral Transcribe API 进行转写。

    使用 ``mistralai`` Python SDK 调用 ``/v1/audio/transcriptions``。
    需要 ``MISTRAL_API_KEY`` 环境变量。
    """
    api_key = get_env_value("MISTRAL_API_KEY")
    if not api_key:
        return {"success": False, "transcript": "", "error": "MISTRAL_API_KEY not set"}

    try:
        try:
            from tools.lazy_deps import ensure as _lazy_ensure
            _lazy_ensure("stt.mistral", prompt=False)
        except ImportError:
            pass
        from mistralai.client import Mistral

        with Mistral(api_key=api_key) as client:
            with open(file_path, "rb") as audio_file:
                result = client.audio.transcriptions.complete(
                    model=model_name,
                    file={"content": audio_file, "file_name": Path(file_path).name},
                )

            transcript_text = _extract_transcript_text(result)
            logger.info(
                "Transcribed %s via Mistral API (%s, %d chars)",
                Path(file_path).name, model_name, len(transcript_text),
            )
            return {"success": True, "transcript": transcript_text, "provider": "mistral"}

    except PermissionError:
        return {"success": False, "transcript": "", "error": f"Permission denied: {file_path}"}
    except Exception as e:
        logger.error("Mistral transcription failed: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"Mistral transcription failed: {type(e).__name__}"}


# ---------------------------------------------------------------------------
# 服务商：xAI（Grok STT API）
# ---------------------------------------------------------------------------


def _transcribe_xai(file_path: str, model_name: str) -> Dict[str, Any]:
    """使用 xAI Grok STT API 进行转写。

    使用 ``POST /v1/stt`` REST 端点，以 multipart/form-data 方式提交。
    支持逆向文本归一化（Inverse Text Normalization）、说话人分离（diarization）
    以及词级时间戳。需要 ``XAI_API_KEY`` 环境变量。
    """
    from tools.xai_http import resolve_xai_http_credentials

    creds = resolve_xai_http_credentials()
    api_key = str(creds.get("api_key") or "").strip()
    if not api_key:
        return {
            "success": False,
            "transcript": "",
            "error": "No xAI credentials found. Configure xAI OAuth in `hermes model` or set XAI_API_KEY",
        }

    stt_config = _load_stt_config()
    xai_config = stt_config.get("xai", {})
    base_url = str(
        xai_config.get("base_url")
        or get_env_value("XAI_STT_BASE_URL")
        or creds.get("base_url")
        or XAI_STT_BASE_URL
    ).strip().rstrip("/")
    language = str(
        xai_config.get("language")
        or os.getenv("HERMES_LOCAL_STT_LANGUAGE")
        or DEFAULT_LOCAL_STT_LANGUAGE
    ).strip()
    # .get("format", True) 在键缺失时已经默认为 True；
    # is_truthy_value 只是把配置里的 truthy/falsy 字符串做归一化。
    use_format = is_truthy_value(xai_config.get("format", True))
    use_diarize = is_truthy_value(xai_config.get("diarize", False))

    try:
        import requests
        from tools.xai_http import hermes_xai_user_agent

        data: Dict[str, str] = {}
        if language:
            data["language"] = language
        if use_format:
            data["format"] = "true"
        if use_diarize:
            data["diarize"] = "true"

        with open(file_path, "rb") as audio_file:
            response = requests.post(
                f"{base_url}/stt",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": hermes_xai_user_agent(),
                },
                files={
                    "file": (Path(file_path).name, audio_file),
                },
                data=data,
                timeout=120,
            )

        if response.status_code != 200:
            detail = ""
            try:
                err_body = response.json()
                detail = err_body.get("error", {}).get("message", "") or response.text[:300]
            except Exception:
                detail = response.text[:300]
            return {
                "success": False,
                "transcript": "",
                "error": f"xAI STT API error (HTTP {response.status_code}): {detail}",
            }

        result = response.json()
        transcript_text = result.get("text", "").strip()

        if not transcript_text:
            return {
                "success": False,
                "transcript": "",
                "error": "xAI STT returned empty transcript",
            }

        logger.info(
            "Transcribed %s via xAI Grok STT (lang=%s, %.1fs audio, %d chars)",
            Path(file_path).name,
            result.get("language", language),
            result.get("duration", 0),
            len(transcript_text),
        )

        return {"success": True, "transcript": transcript_text, "provider": "xai"}

    except PermissionError:
        return {"success": False, "transcript": "", "error": f"Permission denied: {file_path}"}
    except Exception as e:
        logger.error("xAI STT transcription failed: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"xAI STT transcription failed: {e}"}


# ---------------------------------------------------------------------------
# 服务商：ElevenLabs（Scribe STT API）
# ---------------------------------------------------------------------------


def _transcribe_elevenlabs(file_path: str, model_name: str) -> Dict[str, Any]:
    """使用 ElevenLabs Scribe STT API 进行转写。"""
    api_key = get_env_value("ELEVENLABS_API_KEY")
    if not api_key:
        return {"success": False, "transcript": "", "error": "ELEVENLABS_API_KEY not set"}

    stt_config = _load_stt_config()
    elevenlabs_config = stt_config.get("elevenlabs", {})
    base_url = str(
        elevenlabs_config.get("base_url")
        or get_env_value("ELEVENLABS_STT_BASE_URL")
        or ELEVENLABS_STT_BASE_URL
    ).strip().rstrip("/")
    language_code = str(elevenlabs_config.get("language_code") or "").strip()
    tag_audio_events = is_truthy_value(elevenlabs_config.get("tag_audio_events", False))
    diarize = is_truthy_value(elevenlabs_config.get("diarize", False))

    try:
        import requests

        data: Dict[str, str] = {
            "model_id": model_name,
            "tag_audio_events": "true" if tag_audio_events else "false",
            "diarize": "true" if diarize else "false",
        }
        if language_code:
            data["language_code"] = language_code

        with open(file_path, "rb") as audio_file:
            response = requests.post(
                f"{base_url}/speech-to-text",
                headers={"xi-api-key": api_key},
                files={"file": (Path(file_path).name, audio_file)},
                data=data,
                timeout=120,
            )

        if response.status_code != 200:
            detail = ""
            try:
                err_body = response.json()
                error_value = err_body.get("detail") or err_body.get("error")
                if isinstance(error_value, dict):
                    detail = str(error_value.get("message") or error_value)
                elif error_value:
                    detail = str(error_value)
                else:
                    detail = response.text[:300]
            except Exception:
                detail = response.text[:300]
            return {
                "success": False,
                "transcript": "",
                "error": f"ElevenLabs STT API error (HTTP {response.status_code}): {detail}",
            }

        result = response.json()
        transcript_text = _extract_transcript_text(result)
        if not transcript_text:
            return {
                "success": False,
                "transcript": "",
                "error": "ElevenLabs STT returned empty transcript",
            }

        logger.info(
            "Transcribed %s via ElevenLabs Scribe (%s, %d chars)",
            Path(file_path).name,
            model_name,
            len(transcript_text),
        )

        return {"success": True, "transcript": transcript_text, "provider": "elevenlabs"}

    except PermissionError:
        return {"success": False, "transcript": "", "error": f"Permission denied: {file_path}"}
    except Exception as e:
        logger.error("ElevenLabs STT transcription failed: %s", e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"ElevenLabs STT transcription failed: {e}"}


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------


def transcribe_audio(file_path: str, model: Optional[str] = None) -> Dict[str, Any]:
    """
    使用已配置的 STT 服务商转写音频文件。

    服务商优先级：
      1. 用户配置（config.yaml 中的 ``stt.provider``）
      2. 自动检测：local > Groq > OpenAI > Mistral > xAI > ElevenLabs

    参数：
        file_path: 要转写的音频文件的绝对路径。
        model:     覆盖模型。若为 None，则使用配置值或服务商默认值。

    返回：
        包含以下键的 dict：
          - "success" (bool)：转写是否成功
          - "transcript" (str)：转写文本（失败时为空）
          - "error" (str, 可选)：当 success 为 False 时的错误消息
          - "provider" (str, 可选)：实际使用的服务商
    """
    # 校验输入
    error = _validate_audio_file(file_path)
    if error:
        return error

    # 加载配置并决定服务商
    stt_config = _load_stt_config()
    if not is_stt_enabled(stt_config):
        return {
            "success": False,
            "transcript": "",
            "error": "STT is disabled in config.yaml (stt.enabled: false).",
        }

    provider = _get_provider(stt_config)

    if provider == "local":
        local_cfg = stt_config.get("local", {})
        model_name = _normalize_local_model(
            model or local_cfg.get("model", DEFAULT_LOCAL_MODEL)
        )
        return _transcribe_local(file_path, model_name)

    if provider == "local_command":
        local_cfg = stt_config.get("local", {})
        model_name = _normalize_local_command_model(
            model or local_cfg.get("model", DEFAULT_LOCAL_MODEL)
        )
        return _transcribe_local_command(file_path, model_name)

    if provider == "groq":
        model_name = model or DEFAULT_GROQ_STT_MODEL
        return _transcribe_groq(file_path, model_name)

    if provider == "openai":
        openai_cfg = stt_config.get("openai", {})
        model_name = model or openai_cfg.get("model", DEFAULT_STT_MODEL)
        return _transcribe_openai(file_path, model_name)

    if provider == "mistral":
        mistral_cfg = stt_config.get("mistral", {})
        model_name = model or mistral_cfg.get("model", DEFAULT_MISTRAL_STT_MODEL)
        return _transcribe_mistral(file_path, model_name)

    if provider == "xai":
        # xAI Grok STT 不使用 model 参数 —— 这里只是透传以便记录日志
        model_name = model or "grok-stt"
        return _transcribe_xai(file_path, model_name)

    if provider == "elevenlabs":
        elevenlabs_cfg = stt_config.get("elevenlabs", {})
        model_name = model or elevenlabs_cfg.get("model_id", DEFAULT_ELEVENLABS_STT_MODEL)
        return _transcribe_elevenlabs(file_path, model_name)

    # 用户声明的 command 类型服务商
    # （``stt.providers.<name>: type: command``）。在内置 elif 链之后触发 ——
    # 内置名称在上游已短路，因此用户的 ``stt.providers.openai.command``
    # 无法覆盖真正的 OpenAI 处理器 —— 并且在插件分发器之前触发，
    # 因为配置比插件安装更"本地"（与 TTS PR #17843 的优先级规则一致）。
    command_provider_config = _resolve_command_stt_provider_config(provider, stt_config)
    if command_provider_config is not None:
        return _transcribe_command_stt(
            file_path,
            provider,
            command_provider_config,
            stt_config,
            model_override=model,
        )

    # 插件注册的 STT 后端（例如 OpenRouter、SenseAudio、Gemini-STT）。
    # 仅当 ``provider`` 既不是内置也不是 ``"none"``、并且不存在同名命令行
    # 服务商时才触发。分发器防御性地强制执行"内置总是优先 + 命令行优于插件"。
    # 当配置的名称没有注册任何插件时返回 None，回退到下文传统的
    # "No STT provider" 错误消息。
    #
    # 插件作用域的配置命名空间镜像了内置模式
    # （``stt.openai.model``、``stt.mistral.model``）：插件从
    # ``stt.<provider>`` 下读取其各自的服务商配置，分发器从那里转发
    # ``language``。顶层的 ``model`` 参数会覆盖任何配置里设置的模型。
    plugin_cfg = stt_config.get(provider, {}) if isinstance(stt_config.get(provider), dict) else {}
    plugin_language = plugin_cfg.get("language")
    plugin_model = model or plugin_cfg.get("model")
    plugin_result = _dispatch_to_plugin_provider(
        file_path,
        provider,
        stt_config,
        model=plugin_model,
        language=plugin_language,
    )
    if plugin_result is not None:
        return plugin_result

    # 没有可用的服务商
    return {
        "success": False,
        "transcript": "",
        "error": (
            "No STT provider available. Install faster-whisper for free local "
            f"transcription, configure {LOCAL_STT_COMMAND_ENV} or install a local whisper CLI, "
            "set GROQ_API_KEY for free Groq Whisper, set MISTRAL_API_KEY for Mistral "
            "Voxtral Transcribe, configure xAI OAuth or set XAI_API_KEY for xAI Grok STT, "
            "set ELEVENLABS_API_KEY for ElevenLabs Scribe, or set VOICE_TOOLS_OPENAI_KEY "
            "or OPENAI_API_KEY for the OpenAI Whisper API."
        ),
    }


def _resolve_openai_audio_client_config() -> tuple[str, str]:
    """返回直接的 OpenAI 音频配置，或托管网关回退。"""
    stt_config = _load_stt_config()
    openai_cfg = stt_config.get("openai", {})
    cfg_api_key = openai_cfg.get("api_key", "")
    cfg_base_url = openai_cfg.get("base_url", "")
    if cfg_api_key:
        return cfg_api_key, (cfg_base_url or OPENAI_BASE_URL)

    direct_api_key = resolve_openai_audio_api_key()
    if direct_api_key:
        return direct_api_key, OPENAI_BASE_URL

    managed_gateway = resolve_managed_tool_gateway("openai-audio")
    if managed_gateway is None:
        message = "Neither stt.openai.api_key in config nor VOICE_TOOLS_OPENAI_KEY/OPENAI_API_KEY is set"
        if managed_nous_tools_enabled():
            message += (
                ". "
                + nous_tool_gateway_unavailable_message(
                    "managed OpenAI audio for transcription",
                )
            )
        raise ValueError(message)

    return managed_gateway.nous_user_token, urljoin(
        f"{managed_gateway.gateway_origin.rstrip('/')}/", "v1"
    )


def _extract_transcript_text(transcription: Any) -> str:
    """把 text 和 JSON 转写响应归一化为纯字符串。"""
    if isinstance(transcription, str):
        return transcription.strip()

    if hasattr(transcription, "text"):
        value = getattr(transcription, "text")
        if isinstance(value, str):
            return value.strip()

    if isinstance(transcription, dict):
        value = transcription.get("text")
        if isinstance(value, str):
            return value.strip()

    return str(transcription).strip()
