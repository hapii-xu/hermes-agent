"""Voice Mode -- CLI 的按键即说（push-to-talk）录音与回放。

通过 sounddevice 进行音频采集，通过标准库 wave 进行 WAV 编码，
通过 tools.transcription_tools 分派 STT，通过 sounddevice 或系统
音频播放器进行 TTS 回放。

依赖（可选）：
    pip install sounddevice numpy
    或：pip install hermes-agent[voice]
"""

import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 懒加载音频导入 —— 绝不在模块级别导入，以免在无头环境
# （SSH、Docker、WSL、无 PortAudio）下崩溃。
# ---------------------------------------------------------------------------

def _import_audio():
    """懒加载 sounddevice 和 numpy。返回 (sd, np)。

    若库不可用（例如无头服务器上缺少 PortAudio），抛出
    ImportError 或 OSError。
    """
    import sounddevice as sd
    import numpy as np
    return sd, np


def _audio_available() -> bool:
    """若音频库可导入则返回 True。"""
    try:
        _import_audio()
        return True
    except (ImportError, OSError):
        return False


from hermes_constants import is_termux as _is_termux_environment


def _voice_capture_install_hint() -> str:
    if _is_termux_environment():
        return "pkg install python-numpy portaudio && python -m pip install sounddevice"
    return "pip install sounddevice numpy"


def _termux_microphone_command() -> Optional[str]:
    if not _is_termux_environment():
        return None
    return shutil.which("termux-microphone-record")



def _termux_api_app_installed() -> bool:
    if not _is_termux_environment():
        return False
    try:
        result = subprocess.run(
            ["pm", "list", "packages", "com.termux.api"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        return "package:com.termux.api" in (result.stdout or "")
    except Exception:
        return False


def _termux_voice_capture_available() -> bool:
    return _termux_microphone_command() is not None and _termux_api_app_installed()


def _pulse_socket_reachable() -> bool:
    """若磁盘上存在可达的 PulseAudio/PipeWire 套接字则返回 True。

    覆盖了声音服务器在本地运行（例如在远程 SSH 主机上）却未设置
    ``PULSE_SERVER``/``PIPEWIRE_REMOTE`` 的常见场景 —— 客户端会直接
    连接运行时目录下的默认套接字。我们会在 ``PULSE_SERVER`` 的 unix
    路径、``PULSE_RUNTIME_PATH`` 以及 ``XDG_RUNTIME_DIR`` 下查找
    ``pulse/native`` 或 ``pipewire-0`` 套接字（issue #35622）。
    """
    import socket
    import stat

    candidates: List[str] = []

    pulse_server = os.environ.get('PULSE_SERVER', '')
    # PULSE_SERVER 可能是 "unix:/path"、"unix:/path;..." 或一个裸路径。
    for part in pulse_server.split(';'):
        part = part.strip()
        if part.startswith('unix:'):
            candidates.append(part[len('unix:'):])

    pulse_runtime = os.environ.get('PULSE_RUNTIME_PATH')
    if pulse_runtime:
        candidates.append(os.path.join(pulse_runtime, 'native'))

    xdg_runtime = os.environ.get('XDG_RUNTIME_DIR')
    if xdg_runtime:
        candidates.append(os.path.join(xdg_runtime, 'pulse', 'native'))
        candidates.append(os.path.join(xdg_runtime, 'pipewire-0'))

    for path in candidates:
        if not path:
            continue
        try:
            if not stat.S_ISSOCK(os.stat(path).st_mode):
                continue
        except OSError:
            continue
        # 确认套接字确实能接受连接 —— 已死服务器留下的陈旧套接字
        # 文件不应算作可达。
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(0.5)
            sock.connect(path)
            return True
        except OSError:
            continue
        finally:
            sock.close()
    return False


def detect_audio_environment() -> dict:
    """检测当前环境是否支持音频输入输出。

    返回的 dict 包含 'available'（bool）、'warnings'（会阻断语音模式
    的硬失败原因列表）以及 'notices'（不会阻断语音模式的信息性消息
    列表）。
    """
    warnings = []   # 硬失败：这些会阻断语音模式
    notices = []     # 信息性：仅记录日志，不阻断
    termux_mic_cmd = _termux_microphone_command()
    termux_app_installed = _termux_api_app_installed()
    termux_capture = bool(termux_mic_cmd and termux_app_installed)
    has_forwarded_audio = bool(
        os.environ.get('PULSE_SERVER')
        or os.environ.get('PIPEWIRE_REMOTE')
        or _pulse_socket_reachable()
    )

    # SSH 检测 —— 通常没有音频设备，但尊重可达的声音服务器
    # （PulseAudio/PipeWire 套接字或转发环境变量），它在 SSH 下也能
    # 正常工作（issue #35622）。
    if any(os.environ.get(v) for v in ('SSH_CLIENT', 'SSH_TTY', 'SSH_CONNECTION')):
        if has_forwarded_audio:
            notices.append("Running over SSH with a reachable PulseAudio/PipeWire sound server")
        else:
            warnings.append(
                "Running over SSH -- no audio devices available.\n"
                "  If a sound server (PulseAudio/PipeWire) is running on this host,\n"
                "  point Hermes at it, e.g.:\n"
                "    export XDG_RUNTIME_DIR=/run/user/$(id -u)\n"
                "    # or: export PULSE_SERVER=unix:$XDG_RUNTIME_DIR/pulse/native"
            )

    # Docker/Podman 容器检测 —— 尊重宿主音频转发。当用户把
    # PulseAudio/PipeWire 套接字挂载进容器并把 PULSE_SERVER /
    # PIPEWIRE_REMOTE 指向它时，音频可正常工作（issue #21203）。
    # 仅在未配置转发时才阻断。
    from hermes_constants import is_container
    if is_container():
        if has_forwarded_audio:
            notices.append("Running inside container (Docker/Podman/LXC) with host audio forwarding")
        else:
            warnings.append(
                "Running inside container (Docker/Podman/LXC) -- no audio devices.\n"
                "  Forward host audio with one of (substitute $XDG_RUNTIME_DIR for your runtime dir,\n"
                "  typically /run/user/$UID):\n"
                "    PulseAudio:  -v $XDG_RUNTIME_DIR/pulse/native:$XDG_RUNTIME_DIR/pulse/native \\\n"
                "                 -e PULSE_SERVER=unix:$XDG_RUNTIME_DIR/pulse/native\n"
                "    PipeWire:    -e PIPEWIRE_REMOTE=$XDG_RUNTIME_DIR/pipewire-0"
            )

    # WSL 检测 —— PulseAudio 桥接让 WSL 中也能用音频。
    # 仅在未配置 PULSE_SERVER 时才阻断。
    try:
        with open('/proc/version', 'r', encoding="utf-8") as f:
            if 'microsoft' in f.read().lower():
                if os.environ.get('PULSE_SERVER'):
                    notices.append("Running in WSL with PulseAudio bridge")
                else:
                    warnings.append(
                        "Running in WSL -- audio requires PulseAudio bridge.\n"
                        "  1. Set PULSE_SERVER=unix:/mnt/wslg/PulseServer\n"
                        "  2. Create ~/.asoundrc pointing ALSA at PulseAudio\n"
                        "  3. Verify with: arecord -d 3 /tmp/test.wav && aplay /tmp/test.wav"
                    )
    except (FileNotFoundError, PermissionError, OSError):
        pass

    # 检查音频库
    try:
        sd, _ = _import_audio()
        try:
            devices = sd.query_devices()
            if not devices:
                if has_forwarded_audio:
                    notices.append(
                        "No PortAudio devices detected but host audio forwarding is configured -- continuing"
                    )
                elif termux_capture:
                    notices.append("No PortAudio devices detected, but Termux:API microphone capture is available")
                else:
                    warnings.append("No audio input/output devices detected")
        except Exception:
            # 在配了 PulseAudio 的 WSL 中，即便录音/回放正常，设备查询
            # 也可能失败。若配置了宿主音频转发，则不阻断。
            if has_forwarded_audio:
                notices.append(
                    "Audio device query failed but host audio forwarding is configured -- continuing"
                )
            elif termux_capture:
                notices.append("PortAudio device query failed, but Termux:API microphone capture is available")
            else:
                warnings.append("Audio subsystem error (PortAudio cannot query devices)")
    except ImportError:
        if termux_capture:
            notices.append("Termux:API microphone recording available (sounddevice not required)")
        elif termux_mic_cmd and not termux_app_installed:
            warnings.append(
                "Termux:API Android app is not installed. Install/update the Termux:API app to use termux-microphone-record."
            )
        else:
            warnings.append(f"Audio libraries not installed ({_voice_capture_install_hint()})")
    except OSError:
        if termux_capture:
            notices.append("Termux:API microphone recording available (PortAudio not required)")
        elif termux_mic_cmd and not termux_app_installed:
            warnings.append(
                "Termux:API Android app is not installed. Install/update the Termux:API app to use termux-microphone-record."
            )
        elif _is_termux_environment():
            warnings.append(
                "PortAudio system library not found -- install it first:\n"
                "  Termux: pkg install portaudio\n"
                "Then retry /voice on."
            )
        else:
            warnings.append(
                "PortAudio system library not found -- install it first:\n"
                "  Linux:  sudo apt-get install libportaudio2\n"
                "  macOS:  brew install portaudio\n"
                "Then retry /voice on."
            )

    return {
        "available": not warnings,
        "warnings": warnings,
        "notices": notices,
    }

# ---------------------------------------------------------------------------
# 录音参数
# ---------------------------------------------------------------------------
SAMPLE_RATE = 16000  # Whisper 原生采样率
CHANNELS = 1  # 单声道
DTYPE = "int16"  # 16 位 PCM
SAMPLE_WIDTH = 2  # 每个采样的字节数（int16）

# 静音检测默认值
SILENCE_RMS_THRESHOLD = 200  # RMS 低于此值视为静音（int16 范围 0-32767）
SILENCE_DURATION_SECONDS = 3.0  # 连续静音达到多少秒后自动停止

# 语音录音的临时目录
_TEMP_DIR = os.path.join(tempfile.gettempdir(), "hermes_voice")


# ============================================================================
# 音频提示音（蜂鸣）
# ============================================================================
def play_beep(frequency: int = 880, duration: float = 0.12, count: int = 1) -> None:
    """使用 numpy + sounddevice 播放一段短促的提示音。

    参数：
        frequency: 音调频率，单位 Hz（默认 880 = A5）。
        duration: 每声蜂鸣的持续时长，单位秒。
        count: 蜂鸣次数（各声之间有短暂间隔）。
    """
    try:
        sd, np = _import_audio()
    except (ImportError, OSError):
        return
    try:
        gap = 0.06  # 各声蜂鸣之间的间隔秒数
        samples_per_beep = int(SAMPLE_RATE * duration)
        samples_per_gap = int(SAMPLE_RATE * gap)

        parts = []
        for i in range(count):
            t = np.linspace(0, duration, samples_per_beep, endpoint=False)
            # 应用淡入淡出以避免咔哒声伪影
            tone = np.sin(2 * np.pi * frequency * t)
            fade_len = min(int(SAMPLE_RATE * 0.01), samples_per_beep // 4)
            tone[:fade_len] *= np.linspace(0, 1, fade_len)
            tone[-fade_len:] *= np.linspace(1, 0, fade_len)
            parts.append((tone * 0.3 * 32767).astype(np.int16))
            if i < count - 1:
                parts.append(np.zeros(samples_per_gap, dtype=np.int16))

        audio = np.concatenate(parts)
        sd.play(audio, samplerate=SAMPLE_RATE)
        # sd.wait() 调用 Event.wait() 且无超时 —— 若音频设备卡死会
        # 永久挂起。改为带 2 秒上限轮询，到点强制停止。
        deadline = time.monotonic() + 2.0
        while sd.get_stream() and sd.get_stream().active and time.monotonic() < deadline:
            time.sleep(0.01)
        sd.stop()
    except Exception as e:
        logger.debug("Beep playback failed: %s", e)


# ============================================================================
# Termux 录音器
# ============================================================================
class TermuxAudioRecorder:
    """使用 Termux:API 麦克风采集命令的录音后端。"""

    supports_silence_autostop = False

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._recording = False
        self._start_time = 0.0
        self._recording_path: Optional[str] = None
        self._current_rms = 0

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def elapsed_seconds(self) -> float:
        if not self._recording:
            return 0.0
        return time.monotonic() - self._start_time

    @property
    def current_rms(self) -> int:
        return self._current_rms

    def start(self, on_silence_stop=None) -> None:
        del on_silence_stop  # Termux:API 不提供实时静音回调。
        mic_cmd = _termux_microphone_command()
        if not mic_cmd:
            raise RuntimeError(
                "Termux voice capture requires the termux-api package and app.\n"
                "Install with: pkg install termux-api\n"
                "Then install/update the Termux:API Android app."
            )
        if not _termux_api_app_installed():
            raise RuntimeError(
                "Termux voice capture requires the Termux:API Android app.\n"
                "Install/update the Termux:API app, then retry /voice on."
            )

        with self._lock:
            if self._recording:
                return
            os.makedirs(_TEMP_DIR, exist_ok=True)
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            self._recording_path = os.path.join(_TEMP_DIR, f"recording_{timestamp}.aac")

        command = [
            mic_cmd,
            "-f", self._recording_path,
            "-l", "0",
            "-e", "aac",
            "-r", str(SAMPLE_RATE),
            "-c", str(CHANNELS),
        ]
        try:
            subprocess.run(command, capture_output=True, text=True, timeout=15, check=True, stdin=subprocess.DEVNULL)
        except subprocess.CalledProcessError as e:
            details = (e.stderr or e.stdout or str(e)).strip()
            raise RuntimeError(f"Termux microphone start failed: {details}") from e
        except Exception as e:
            raise RuntimeError(f"Termux microphone start failed: {e}") from e

        with self._lock:
            self._start_time = time.monotonic()
            self._recording = True
            self._current_rms = 0
        logger.info("Termux voice recording started")

    def _stop_termux_recording(self) -> None:
        mic_cmd = _termux_microphone_command()
        if not mic_cmd:
            return
        subprocess.run([mic_cmd, "-q"], capture_output=True, text=True, timeout=15, check=False, stdin=subprocess.DEVNULL)

    def stop(self) -> Optional[str]:
        with self._lock:
            if not self._recording:
                return None
            self._recording = False
            path = self._recording_path
            self._recording_path = None
            started_at = self._start_time
            self._current_rms = 0

        self._stop_termux_recording()
        if not path or not os.path.isfile(path):
            return None
        if time.monotonic() - started_at < 0.3:
            try:
                os.unlink(path)
            except OSError:
                pass
            return None
        if os.path.getsize(path) <= 0:
            try:
                os.unlink(path)
            except OSError:
                pass
            return None
        logger.info("Termux voice recording stopped: %s", path)
        return path

    def cancel(self) -> None:
        with self._lock:
            path = self._recording_path
            self._recording = False
            self._recording_path = None
            self._current_rms = 0
        try:
            self._stop_termux_recording()
        except Exception:
            pass
        if path and os.path.isfile(path):
            try:
                os.unlink(path)
            except OSError:
                pass
        logger.info("Termux voice recording cancelled")

    def shutdown(self) -> None:
        self.cancel()


# ============================================================================
# AudioRecorder
# ============================================================================
class AudioRecorder:
    """使用 sounddevice.InputStream 的线程安全录音器。

    用法::

        recorder = AudioRecorder()
        recorder.start(on_silence_stop=my_callback)
        # ... 用户说话 ...
        wav_path = recorder.stop()   # 返回 WAV 文件路径
        # 或
        recorder.cancel()            # 丢弃，不保存

    若提供了 ``on_silence_stop``，当用户静默 ``silence_duration``
    秒后会自动停止录音并调用该回调。
    """

    supports_silence_autostop = True

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stream: Any = None
        self._frames: List[Any] = []
        self._recording = False
        self._start_time: float = 0.0
        # 静音检测状态
        self._has_spoken = False
        self._speech_start: float = 0.0  # 语音尝试开始时刻
        self._dip_start: float = 0.0  # 当前低于阈值下陷开始时刻
        self._min_speech_duration: float = 0.3  # 确认语音所需秒数
        self._max_dip_tolerance: float = 0.3  # 重置语音前的最大下陷时长
        self._silence_start: float = 0.0
        self._resume_start: float = 0.0  # 静音开始后跟踪持续语音
        self._resume_dip_start: float = 0.0  # 恢复检测的下陷容忍跟踪器
        self._on_silence_stop = None
        self._silence_threshold: int = SILENCE_RMS_THRESHOLD
        self._silence_duration: float = SILENCE_DURATION_SECONDS
        self._max_wait: float = 15.0  # 等待语音的最长秒数，到点自动停止
        # 录音期间出现的峰值 RMS（用于 stop() 中的语音存在性判断）
        self._peak_rms: int = 0
        # 实时音频电平（供 UI 读取以做可视化反馈）
        self._current_rms: int = 0

    # -- 公开属性 ---------------------------------------------------

    @property
    def elapsed_seconds(self) -> float:
        if not self._recording:
            return 0.0
        return time.monotonic() - self._start_time

    @property
    def current_rms(self) -> int:
        """当前音频输入 RMS 电平（0-32767）。每个音频块更新一次。"""
        return self._current_rms

    @property
    def is_recording(self) -> bool:
        """当前是否正在进行音频录制。"""
        return self._recording

    # -- 公开方法 --------------------------------------------------

    def _ensure_stream(self) -> None:
        """一次性创建音频 InputStream 并保持其常驻。

        流在录音器的整个生命周期内保持打开。两次录音之间，回调
        仅丢弃音频块（此时 ``_recording`` 为 ``False``）。这样做是为
        了规避 CoreAudio 在 macOS 上反复关闭又重新打开
        ``InputStream`` 会无限挂起的 bug。
        """
        if self._stream is not None:
            return  # 已在运行

        sd, np = _import_audio()

        def _callback(indata, frames, time_info, status):  # noqa: ARG001
            if status:
                logger.debug("sounddevice status: %s", status)
            # 不在录音时流处于空闲 —— 丢弃音频。
            if not self._recording:
                return
            self._frames.append(indata.copy())

            # 计算 RMS，用于电平显示和静音检测
            rms = int(np.sqrt(np.mean(indata.astype(np.float64) ** 2)))
            self._current_rms = rms
            self._peak_rms = max(self._peak_rms, rms)

            # 静音检测
            if self._on_silence_stop is not None:
                now = time.monotonic()
                elapsed = now - self._start_time

                if rms > self._silence_threshold:
                    # 音频高于阈值 —— 这是语音（或噪声）。
                    self._dip_start = 0.0  # 重置下陷跟踪器
                    if self._speech_start == 0.0:
                        self._speech_start = now
                    elif not self._has_spoken and now - self._speech_start >= self._min_speech_duration:
                        self._has_spoken = True
                        logger.debug("Speech confirmed (%.2fs above threshold)",
                                     now - self._speech_start)
                    # 语音确认后，仅当语音持续（高于阈值超过 0.3 秒）
                    # 时才重置静音计时器。环境噪声的短暂尖峰不应重置
                    # 计时器。
                    if not self._has_spoken:
                        self._silence_start = 0.0
                    else:
                        # 跟踪恢复的语音，并容忍下陷。
                        # 语音期间短暂跌破阈值是正常现象，因此我们
                        # 复刻初始语音检测的模式：开始跟踪、容忍短促
                        # 下陷、0.3 秒后确认。
                        self._resume_dip_start = 0.0  # 高于阈值 —— 无下陷
                        if self._resume_start == 0.0:
                            self._resume_start = now
                        elif now - self._resume_start >= self._min_speech_duration:
                            self._silence_start = 0.0
                            self._resume_start = 0.0
                elif self._has_spoken:
                    # 语音确认后又跌破阈值。
                    # 重置恢复跟踪器前使用下陷容忍 —— 自然语音在
                    # 阈值下方会有短暂下陷。
                    if self._resume_start > 0:
                        if self._resume_dip_start == 0.0:
                            self._resume_dip_start = now
                        elif now - self._resume_dip_start >= self._max_dip_tolerance:
                            # 持续下陷 —— 用户确实停止说话了
                            self._resume_start = 0.0
                            self._resume_dip_start = 0.0
                elif self._speech_start > 0:
                    # 曾处于语音尝试中，但 RMS 下陷了。
                    # 容忍短暂下陷（音节之间的微停顿）。
                    if self._dip_start == 0.0:
                        self._dip_start = now
                    elif now - self._dip_start >= self._max_dip_tolerance:
                        # 下陷持续过久 —— 确为静音，重置
                        logger.debug("Speech attempt reset (dip lasted %.2fs)",
                                     now - self._dip_start)
                        self._speech_start = 0.0
                        self._dip_start = 0.0

                # 满足下列条件时触发静音回调：
                # 1. 用户说过话后静默了 silence_duration 秒，或
                # 2. max_wait 秒内完全未检测到语音
                should_fire = False
                if self._has_spoken and rms <= self._silence_threshold:
                    # 用户曾在说话，现在安静了
                    if self._silence_start == 0.0:
                        self._silence_start = now
                    elif now - self._silence_start >= self._silence_duration:
                        logger.info("Silence detected (%.1fs), auto-stopping",
                                    self._silence_duration)
                        should_fire = True
                elif not self._has_spoken and elapsed >= self._max_wait:
                    logger.info("No speech within %.0fs, auto-stopping",
                                self._max_wait)
                    should_fire = True

                if should_fire:
                    with self._lock:
                        cb = self._on_silence_stop
                        self._on_silence_stop = None  # 只触发一次
                    if cb:
                        def _safe_cb():
                            try:
                                cb()
                            except Exception as e:
                                logger.error("Silence callback failed: %s", e, exc_info=True)
                        threading.Thread(target=_safe_cb, daemon=True).start()

        # 创建流 —— 可能因 CoreAudio 而阻塞（仅首次调用）。
        stream = None
        try:
            stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=DTYPE,
                callback=_callback,
            )
            stream.start()
        except Exception as e:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            raise RuntimeError(
                f"Failed to open audio input stream: {e}. "
                "Check that a microphone is connected and accessible."
            ) from e
        self._stream = stream

    def start(self, on_silence_stop=None) -> None:
        """从默认输入设备开始采集音频。

        底层 InputStream 只创建一次并跨多次录音保持常驻。后续调用
        仅重置检测状态，并通过 ``_recording`` 切换帧收集。

        参数：
            on_silence_stop: 可选回调，在语音后检测到静音时（在守护
                线程中）调用。回调不接收任何参数。可用它来自动停止
                录音并触发转写。

        当 sounddevice/numpy 未安装，或已有录音正在进行时，抛出
        ``RuntimeError``。
        """
        try:
            _import_audio()
        except (ImportError, OSError) as e:
            raise RuntimeError(
                "Voice mode requires sounddevice and numpy.\n"
                f"Install with: {sys.executable} -m pip install sounddevice numpy"
            ) from e

        with self._lock:
            if self._recording:
                return  # 已在录音

            self._frames = []
            self._start_time = time.monotonic()
            self._has_spoken = False
            self._speech_start = 0.0
            self._dip_start = 0.0
            self._silence_start = 0.0
            self._resume_start = 0.0
            self._resume_dip_start = 0.0
            self._peak_rms = 0
            self._current_rms = 0
            self._on_silence_stop = on_silence_stop

        # 确保常驻流存活（首次调用后为空操作）。
        self._ensure_stream()

        with self._lock:
            self._recording = True
        logger.info("Voice recording started (rate=%d, channels=%d)", SAMPLE_RATE, CHANNELS)

    def _close_stream_with_timeout(self, timeout: float = 3.0) -> None:
        """关闭音频流并带超时，以防止 CoreAudio 卡死。"""
        if self._stream is None:
            return

        stream = self._stream
        self._stream = None

        def _do_close():
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

        t = threading.Thread(target=_do_close, daemon=True)
        t.start()
        # 以短间隔轮询，使 Ctrl+C 不被阻塞
        deadline = __import__("time").monotonic() + timeout
        while t.is_alive() and __import__("time").monotonic() < deadline:
            t.join(timeout=0.1)
        if t.is_alive():
            logger.warning("Audio stream close timed out after %.1fs — forcing ahead", timeout)

    def stop(self) -> Optional[str]:
        """停止录音并把采集到的音频写入 WAV 文件。

        底层流保持存活以便复用 —— 仅停止帧收集。

        返回：
            WAV 文件路径；若未采集到音频则返回 ``None``。
        """
        with self._lock:
            if not self._recording:
                return None

            self._recording = False
            self._current_rms = 0
            # 流保持存活 —— 无需关闭。

            if not self._frames:
                return None

            # 拼接帧并写入 WAV
            _, np = _import_audio()
            audio_data = np.concatenate(self._frames, axis=0)
            self._frames = []

            elapsed = time.monotonic() - self._start_time
            logger.info("Voice recording stopped (%.1fs, %d samples)", elapsed, len(audio_data))

            # 跳过过短的录音（音频不足 0.3 秒）
            min_samples = int(SAMPLE_RATE * 0.3)
            if len(audio_data) < min_samples:
                logger.debug("Recording too short (%d samples), discarding", len(audio_data))
                return None

            # 用峰值 RMS 跳过静音录音（而非整体均值，因为均值会被
            # 录音末尾的静音稀释）。
            if self._peak_rms < SILENCE_RMS_THRESHOLD:
                logger.info("Recording too quiet (peak RMS=%d < %d), discarding",
                            self._peak_rms, SILENCE_RMS_THRESHOLD)
                return None

            return self._write_wav(audio_data)

    def cancel(self) -> None:
        """停止录音并丢弃所有已采集音频。

        底层流保持存活以便复用。
        """
        with self._lock:
            self._recording = False
            self._frames = []
            self._on_silence_stop = None
            self._current_rms = 0
        logger.info("Voice recording cancelled")

    def shutdown(self) -> None:
        """释放音频流。在禁用语音模式时调用。"""
        with self._lock:
            self._recording = False
            self._frames = []
            self._on_silence_stop = None
        # 在锁之外关闭流，避免与音频回调死锁
        self._close_stream_with_timeout()
        logger.info("AudioRecorder shut down")

    # -- 私有辅助 --------------------------------------------------

    @staticmethod
    def _write_wav(audio_data) -> str:
        """把 numpy int16 音频数据写入 WAV 文件。

        返回文件路径。
        """
        os.makedirs(_TEMP_DIR, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        wav_path = os.path.join(_TEMP_DIR, f"recording_{timestamp}.wav")

        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_data.tobytes())

        file_size = os.path.getsize(wav_path)
        logger.info("WAV written: %s (%d bytes)", wav_path, file_size)
        return wav_path


def create_audio_recorder() -> AudioRecorder | TermuxAudioRecorder:
    """为当前环境返回最合适的录音后端。"""
    if _termux_voice_capture_available():
        return TermuxAudioRecorder()
    return AudioRecorder()


# ============================================================================
# Whisper 幻觉过滤器
# ============================================================================
# Whisper 在静音/近静音音频上常会幻觉出这些短语。
WHISPER_HALLUCINATIONS = {
    "thank you.",
    "thank you",
    "thanks for watching.",
    "thanks for watching",
    "subscribe to my channel.",
    "subscribe to my channel",
    "like and subscribe.",
    "like and subscribe",
    "please subscribe.",
    "please subscribe",
    "thank you for watching.",
    "thank you for watching",
    "bye.",
    "bye",
    "you",
    "the end.",
    "the end",
    # 非英文幻觉（静音时常见）
    "продолжение следует",
    "продолжение следует...",
    "sous-titres",
    "sous-titres réalisés par la communauté d'amara.org",
    "sottotitoli creati dalla comunità amara.org",
    "untertitel von stephanie geiges",
    "amara.org",
    "www.mooji.org",
    "ご視聴ありがとうございました",
}

# 重复性幻觉的正则模式（例如 "Thank you. Thank you. Thank you."）
_HALLUCINATION_REPEAT_RE = re.compile(
    r'^(?:thank you|thanks|bye|you|ok|okay|the end|\.|\s|,|!)+$',
    flags=re.IGNORECASE,
)


def is_whisper_hallucination(transcript: str) -> bool:
    """检查一段转写文本是否为静音上的已知 Whisper 幻觉。"""
    cleaned = transcript.strip().lower()
    if not cleaned:
        return True
    # 与已知短语精确匹配
    if cleaned.rstrip('.!') in WHISPER_HALLUCINATIONS or cleaned in WHISPER_HALLUCINATIONS:
        return True
    # 重复性模式（例如 "Thank you. Thank you. Thank you. you"）
    if _HALLUCINATION_REPEAT_RE.match(cleaned):
        return True
    return False


# ============================================================================
# STT 分派
# ============================================================================
def transcribe_recording(wav_path: str, model: Optional[str] = None) -> Dict[str, Any]:
    """使用既有的 Whisper 流水线对一段 WAV 录音进行转写。

    委托给 ``tools.transcription_tools.transcribe_audio()``。
    过滤掉静音音频上的已知 Whisper 幻觉。

    参数：
        wav_path: WAV 文件路径。
        model: Whisper 模型名（默认：取自配置或 ``whisper-1``）。

    返回：
        含 ``success``、``transcript`` 以及可选 ``error`` 的 dict。
    """
    from tools.transcription_tools import MAX_FILE_SIZE, transcribe_audio

    if _should_chunk_for_transcription(wav_path, MAX_FILE_SIZE):
        result = _transcribe_wav_in_chunks(wav_path, model=model, max_file_size=MAX_FILE_SIZE)
    else:
        result = transcribe_audio(wav_path, model=model)

    # 过滤 Whisper 幻觉（静音/近静音音频上常见）
    if result.get("success") and is_whisper_hallucination(result.get("transcript", "")):
        logger.info("Filtered Whisper hallucination: %r", result["transcript"])
        return {"success": True, "transcript": "", "filtered": True}

    return result


def _should_chunk_for_transcription(file_path: str, max_file_size: int) -> bool:
    """判断一段 CLI WAV 录音在送入 STT 前是否需要切分。"""
    if not file_path.lower().endswith(".wav"):
        return False
    try:
        return os.path.getsize(file_path) > max_file_size
    except OSError:
        return False


def _transcribe_wav_in_chunks(
    wav_path: str,
    *,
    model: Optional[str],
    max_file_size: int,
) -> Dict[str, Any]:
    """把超大 WAV 切成符合 provider 大小限制的块，并拼接转写结果。"""
    from tools.transcription_tools import transcribe_audio

    chunk_paths: List[str] = []
    transcripts: List[str] = []

    try:
        chunk_paths = _split_wav_for_transcription(wav_path, max_file_size=max_file_size)
        if not chunk_paths:
            return {"success": False, "transcript": "", "error": "No audio chunks were created"}

        logger.info("Transcribing oversized WAV in %d chunks: %s", len(chunk_paths), wav_path)
        for index, chunk_path in enumerate(chunk_paths, start=1):
            result = transcribe_audio(chunk_path, model=model)
            if not result.get("success"):
                error = result.get("error", "Unknown transcription error")
                return {
                    "success": False,
                    "transcript": "",
                    "error": f"Chunk {index}/{len(chunk_paths)} failed: {error}",
                }

            transcript = result.get("transcript", "").strip()
            if transcript and not is_whisper_hallucination(transcript):
                transcripts.append(transcript)

        return {
            "success": True,
            "transcript": " ".join(transcripts).strip(),
            "provider": result.get("provider"),
            "chunks": len(chunk_paths),
        }
    except Exception as e:
        logger.error("Chunked transcription failed for %s: %s", wav_path, e, exc_info=True)
        return {"success": False, "transcript": "", "error": f"Chunked transcription failed: {e}"}
    finally:
        for chunk_path in chunk_paths:
            try:
                if os.path.isfile(chunk_path):
                    os.unlink(chunk_path)
            except OSError:
                pass


def _split_wav_for_transcription(wav_path: str, *, max_file_size: int) -> List[str]:
    """写出足够小、能通过共享 STT 文件大小关卡的 WAV 块。"""
    os.makedirs(_TEMP_DIR, exist_ok=True)
    chunk_paths: List[str] = []
    header_reserve = 64 * 1024

    with wave.open(wav_path, "rb") as source:
        params = source.getparams()
        block_align = max(1, params.nchannels * params.sampwidth)
        max_data_bytes = max_file_size - header_reserve
        if max_data_bytes < block_align:
            raise ValueError("STT max_file_size is too small for WAV chunking")

        frames_per_chunk = max(1, max_data_bytes // block_align)
        index = 0
        while True:
            frames = source.readframes(frames_per_chunk)
            if not frames:
                break

            index += 1
            temp = tempfile.NamedTemporaryFile(
                prefix=f"{os.path.splitext(os.path.basename(wav_path))[0]}_chunk{index:03d}_",
                suffix=".wav",
                dir=_TEMP_DIR,
                delete=False,
            )
            chunk_path = temp.name
            temp.close()

            try:
                with wave.open(chunk_path, "wb") as chunk:
                    chunk.setnchannels(params.nchannels)
                    chunk.setsampwidth(params.sampwidth)
                    chunk.setframerate(params.framerate)
                    chunk.setcomptype(params.comptype, params.compname)
                    chunk.writeframes(frames)
                chunk_paths.append(chunk_path)
            except Exception:
                try:
                    os.unlink(chunk_path)
                except OSError:
                    pass
                raise

    return chunk_paths


# ============================================================================
# 音频回放（可打断）
# ============================================================================

# 对当前回放进程的全局引用，以便能被打断。
_active_playback: Optional[subprocess.Popen] = None
_playback_lock = threading.Lock()


def stop_playback() -> None:
    """打断当前正在回放的音频（若有）。"""
    global _active_playback
    with _playback_lock:
        proc = _active_playback
        _active_playback = None
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            logger.info("Audio playback interrupted")
        except Exception:
            pass
    # 若 sounddevice 正在回放，也一并停止
    try:
        sd, _ = _import_audio()
        sd.stop()
    except Exception:
        pass


def play_audio_file(file_path: str) -> bool:
    """通过默认输出设备回放一个音频文件。

    策略：
    1. 可用时通过 ``sounddevice.play()`` 回放 WAV 文件。
    2. 系统命令：``afplay``（macOS）、``ffplay``（跨平台）、
       ``aplay``（Linux ALSA）。

    可通过调用 ``stop_playback()`` 打断回放。

    返回：
        回放成功返回 ``True``，否则返回 ``False``。
    """
    global _active_playback

    if not os.path.isfile(file_path):
        logger.warning("Audio file not found: %s", file_path)
        return False

    # 对 WAV 文件优先尝试 sounddevice
    if file_path.endswith(".wav"):
        try:
            sd, np = _import_audio()
            with wave.open(file_path, "rb") as wf:
                frames = wf.readframes(wf.getnframes())
                audio_data = np.frombuffer(frames, dtype=np.int16)
                sample_rate = wf.getframerate()

            sd.play(audio_data, samplerate=sample_rate)
            # sd.wait() 调用 Event.wait() 且无超时 —— 若音频设备卡死
            # 会永久挂起。改为带上限轮询，到点强制停止。
            duration_secs = len(audio_data) / sample_rate
            deadline = time.monotonic() + duration_secs + 2.0
            while sd.get_stream() and sd.get_stream().active and time.monotonic() < deadline:
                time.sleep(0.01)
            sd.stop()
            return True
        except (ImportError, OSError):
            pass  # 音频库不可用，回落到系统播放器
        except Exception as e:
            logger.debug("sounddevice playback failed: %s", e)

    # 回落到系统音频播放器（用 Popen 以便可打断）
    system = platform.system()
    players = []

    if system == "Darwin":
        players.append(["afplay", file_path])
    players.append(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", file_path])
    if system == "Linux":
        players.append(["aplay", "-q", file_path])

    for cmd in players:
        exe = shutil.which(cmd[0])
        if exe:
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
                with _playback_lock:
                    _active_playback = proc
                proc.wait(timeout=300)
                with _playback_lock:
                    _active_playback = None
                return True
            except subprocess.TimeoutExpired:
                logger.warning("System player %s timed out, killing process", cmd[0])
                proc.kill()
                proc.wait()
                with _playback_lock:
                    _active_playback = None
            except Exception as e:
                logger.debug("System player %s failed: %s", cmd[0], e)
                with _playback_lock:
                    _active_playback = None

    logger.warning("No audio player available for %s", file_path)
    return False


# ============================================================================
# 依赖检查
# ============================================================================
def check_voice_requirements() -> Dict[str, Any]:
    """检查是否满足语音模式的全部依赖。

    返回：
        含 ``available``、``audio_available``、``stt_available``、
        ``missing_packages`` 和 ``details`` 的 dict。
    """
    # 判定 STT provider 是否可用
    from tools.transcription_tools import _get_provider, _load_stt_config, is_stt_enabled
    stt_config = _load_stt_config()
    stt_enabled = is_stt_enabled(stt_config)
    stt_provider = _get_provider(stt_config)
    stt_available = stt_enabled and stt_provider != "none"

    missing: List[str] = []
    termux_capture = _termux_voice_capture_available()
    has_audio = _audio_available() or termux_capture

    if not has_audio:
        missing.extend(["sounddevice", "numpy"])

    # 环境检测
    env_check = detect_audio_environment()

    available = has_audio and stt_available and env_check["available"]
    details_parts = []

    if termux_capture:
        details_parts.append("Audio capture: OK (Termux:API microphone)")
    elif has_audio:
        details_parts.append("Audio capture: OK")
    else:
        details_parts.append(f"Audio capture: MISSING ({_voice_capture_install_hint()})")

    if not stt_enabled:
        details_parts.append("STT provider: DISABLED in config (stt.enabled: false)")
    elif stt_provider == "local":
        details_parts.append("STT provider: OK (local faster-whisper)")
    elif stt_provider == "groq":
        details_parts.append("STT provider: OK (Groq)")
    elif stt_provider == "openai":
        details_parts.append("STT provider: OK (OpenAI)")
    else:
        details_parts.append(
            "STT provider: MISSING (uv pip install faster-whisper — "
            "`pip install faster-whisper` also works if pip is on PATH, "
            "or set GROQ_API_KEY / VOICE_TOOLS_OPENAI_KEY)"
        )

    for warning in env_check["warnings"]:
        details_parts.append(f"Environment: {warning}")
    for notice in env_check.get("notices", []):
        details_parts.append(f"Environment: {notice}")

    return {
        "available": available,
        "audio_available": has_audio,
        "stt_available": stt_available,
        "missing_packages": missing,
        "details": "\n".join(details_parts),
        "environment": env_check,
    }


# ============================================================================
# 临时文件清理
# ============================================================================
def cleanup_temp_recordings(max_age_seconds: int = 3600) -> int:
    """移除陈旧的临时语音录音文件。

    参数：
        max_age_seconds: 删除超过此时长的文件（默认：1 小时）。

    返回：
        已删除的文件数。
    """
    if not os.path.isdir(_TEMP_DIR):
        return 0

    deleted = 0
    now = time.time()

    for entry in os.scandir(_TEMP_DIR):
        if entry.is_file() and entry.name.startswith("recording_") and entry.name.endswith(".wav"):
            try:
                age = now - entry.stat().st_mtime
                if age > max_age_seconds:
                    os.unlink(entry.path)
                    deleted += 1
            except OSError:
                pass

    if deleted:
        logger.debug("Cleaned up %d old voice recordings", deleted)
    return deleted
