"""将生成的语音馈送到 Chrome 麦克风的虚拟音频桥。

v2 模块。为 Meet bot 的 Chromium 实例配置特定于平台的虚拟音频设备，
使其可以指向我们控制的输入源。OpenAI Realtime 客户端将 PCM 字节写入此设备；
Chrome 读取这些字节，就好像它们来自麦克风一样。

Linux（主要平台）：使用 pactl (PulseAudio) 创建一个 null-sink 以及
一个以 null-sink 的 monitor 为 master 的虚拟 source。调用方在 Chrome 的环境中
设置 PULSE_SOURCE=<source_name> 并传入 fake-mic 标志。

macOS：需要安装 BlackHole 2ch。此模块仅验证其存在并返回设备名称；
将操作系统默认输入路由到该设备留给用户（或未来的 switchaudio-osx 集成）处理，
以避免意外更改用户的系统音频状态。

Windows：v2 不支持。
"""

from __future__ import annotations

import platform
import subprocess
from typing import Optional


_BLACKHOLE_DEVICE = "BlackHole 2ch"


class AudioBridge:
    """管理用于 Chrome 假麦克风输入的虚拟音频设备。

    在启动 Meet bot 之前调用一次 ``setup()``，
    在会话结束时调用 ``teardown()``。``teardown()`` 是幂等的。
    """

    def __init__(self, name_prefix: str = "hermes_meet") -> None:
        self._name_prefix = name_prefix
        self._platform: Optional[str] = None
        self._device_name: Optional[str] = None
        self._write_target: Optional[str] = None
        self._module_ids: list[int] = []
        self._torn_down = False

    # ── public properties ─────────────────────────────────────────────────

    @property
    def device_name(self) -> str:
        if not self._device_name:
            raise RuntimeError("AudioBridge not set up yet")
        return self._device_name

    @property
    def write_target(self) -> str:
        if not self._write_target:
            raise RuntimeError("AudioBridge not set up yet")
        return self._write_target

    # ── lifecycle ─────────────────────────────────────────────────────────

    def setup(self) -> dict:
        """配置虚拟音频设备。

        返回描述设备的字典。在不支持的平台上
        或缺少所需系统工具时抛出 RuntimeError。
        """
        system = platform.system()
        if system == "Linux":
            return self._setup_linux()
        if system == "Darwin":
            return self._setup_darwin()
        if system == "Windows":
            raise RuntimeError("windows not supported in v2")
        raise RuntimeError(f"unsupported platform: {system}")

    def teardown(self) -> None:
        """释放虚拟音频设备。幂等操作。"""
        if self._torn_down:
            return
        # 只有 Linux 需要显式卸载。
        if self._platform == "linux" and self._module_ids:
            # 按逆序卸载（先 virtual-source，再 null-sink）。
            for mod_id in reversed(self._module_ids):
                try:
                    subprocess.run(
                        ["pactl", "unload-module", str(mod_id)],
                        check=False,
                        capture_output=True,
                        stdin=subprocess.DEVNULL,
                    )
                except Exception:
                    # 尽力而为的清理 — 此处永远不抛出异常。
                    pass
            self._module_ids = []
        self._torn_down = True

    # ── platform impls ────────────────────────────────────────────────────

    def _setup_linux(self) -> dict:
        sink_name = f"{self._name_prefix}_sink"
        src_name = f"{self._name_prefix}_src"

        try:
            sink_out = subprocess.run(
                [
                    "pactl",
                    "load-module",
                    "module-null-sink",
                    f"sink_name={sink_name}",
                    f"sink_properties=device.description=HermesMeetSink",
                ],
                check=True,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "pactl not found — install PulseAudio/pipewire-pulse"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"pactl load-module null-sink failed: {exc.stderr or exc}"
            ) from exc

        sink_mod_id = self._parse_module_id(sink_out.stdout)

        try:
            src_out = subprocess.run(
                [
                    "pactl",
                    "load-module",
                    "module-virtual-source",
                    f"source_name={src_name}",
                    f"master={sink_name}.monitor",
                ],
                check=True,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError as exc:
            # 回滚我们刚刚创建的 null-sink，避免泄漏。
            subprocess.run(
                ["pactl", "unload-module", str(sink_mod_id)],
                check=False,
                capture_output=True,
                stdin=subprocess.DEVNULL,
            )
            raise RuntimeError(
                f"pactl load-module virtual-source failed: {exc.stderr or exc}"
            ) from exc

        src_mod_id = self._parse_module_id(src_out.stdout)

        self._platform = "linux"
        self._device_name = src_name
        self._write_target = sink_name
        self._module_ids = [sink_mod_id, src_mod_id]
        self._torn_down = False

        return {
            "platform": "linux",
            "device_name": src_name,
            "sample_rate": 48000,
            "channels": 2,
            "module_ids": list(self._module_ids),
            "write_target": sink_name,
        }

    def _setup_darwin(self) -> dict:
        try:
            out = subprocess.check_output(
                ["system_profiler", "SPAudioDataType"],
                text=True,
                stderr=subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "system_profiler not found (macOS-only command)"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"system_profiler failed: {exc.output}"
            ) from exc

        if "BlackHole" not in out:
            raise RuntimeError(
                "BlackHole virtual audio device not installed. "
                "Install via: brew install blackhole-2ch"
            )

        self._platform = "darwin"
        self._device_name = _BLACKHOLE_DEVICE
        self._write_target = _BLACKHOLE_DEVICE
        self._module_ids = []
        self._torn_down = False

        return {
            "platform": "darwin",
            "device_name": _BLACKHOLE_DEVICE,
            "sample_rate": 48000,
            "channels": 2,
            "module_ids": [],
            "write_target": _BLACKHOLE_DEVICE,
        }

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _parse_module_id(stdout: str) -> int:
        """pactl load-module 会将新的模块 ID 打印到 stdout。"""
        text = (stdout or "").strip()
        if not text:
            raise RuntimeError("pactl load-module returned empty stdout")
        # 取第一个非空行的最后一个空白分隔的 token。
        first = text.splitlines()[0].strip()
        token = first.split()[-1]
        try:
            return int(token)
        except ValueError as exc:
            raise RuntimeError(
                f"could not parse pactl module id from: {stdout!r}"
            ) from exc


def chrome_fake_audio_flags(bridge_info: dict) -> list[str]:
    """返回用于使用假音频输入的 Chrome 标志。

    PulseAudio source 通过 ``PULSE_SOURCE`` 环境变量选择，
    调用方必须在启动 Chrome 之前将其设置到 Chrome 的环境中：

        env["PULSE_SOURCE"] = bridge_info["device_name"]

    在 macOS 上，调用方须确保系统默认音频输入
    已设置为返回的 BlackHole 设备（我们不负责切换该设置）。
    """
    system = platform.system()
    if system == "Linux":
        # Linux 上的 Chromium 通过 PULSE_SOURCE 环境变量选择 PulseAudio source；
        # fake-ui 标志跳过权限提示，使 bot 可以在没有用户输入的情况下选择"使用我的麦克风"。
        return ["--use-fake-ui-for-media-stream"]
    if system == "Darwin":
        return ["--use-fake-ui-for-media-stream"]
    if system == "Windows":
        raise RuntimeError("windows not supported in v2")
    raise RuntimeError(f"unsupported platform: {system}")
