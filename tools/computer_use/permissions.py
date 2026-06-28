"""
跨平台的 Computer Use 就绪检查 + macOS 权限辅助函数。

cua-driver 可在 macOS、Windows 和 Linux 上运行，但「可以驱动」在每个
平台上的含义不同：

  * macOS —— 需显式的 TCC 授权（辅助功能 + 屏幕录制）。cua-driver 通过
    ``permissions status`` / ``permissions grant`` 上报/请求这些授权。授权
    绑定在 cua-driver 自己的身份（``com.trycua.driver`` / 已安装的
    ``CuaDriver.app``）上，而非 Hermes——因此不涉及任何 Hermes 权限声明，
    且 ``grant`` 会通过 LaunchServices 启动 CuaDriver，使 macOS 对话框
    能正确归因。
  * Windows —— 没有 TCC 开关；UIAccess 工作进程（``cua-driver-uia.exe``）
    首次运行时可能触发 SmartScreen 提示。就绪 == 驱动健康。
  * Linux —— 通过 X11/XWayland 栈实现辅助控制。就绪 == 驱动健康。

每个平台上的通用信号都是 ``cua-driver doctor --json``（二进制完整性 +
平台支持）。``computer_use_status`` 把它与 macOS 权限细节整合进同一个
负载，供桌面卡片、``hermes computer-use permissions`` CLI 以及
``/api/tools/computer-use/status`` 使用。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

# 拥有 cua-driver 运行时后端的平台（与 toolset 的 platform_gate 一致）。
_RUNTIME_PLATFORMS = frozenset({"darwin", "win32", "linux"})
_BOOLS = ("accessibility", "screen_recording", "screen_recording_capturable")


def _driver_cmd(override: Optional[str]) -> str:
    if override:
        return override
    try:
        from hermes_cli.tools_config import _cua_driver_cmd

        return _cua_driver_cmd()
    except Exception:
        return os.environ.get("HERMES_CUA_DRIVER_CMD", "").strip() or "cua-driver"


def _child_env() -> Dict[str, str]:
    """遵循 Hermes 遥测 opt-in 策略的 cua-driver 子进程环境变量。"""
    try:
        from tools.computer_use.cua_backend import cua_driver_child_env

        return cua_driver_child_env()
    except Exception:
        return dict(os.environ)


def _run(binary: str, *args: str, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_child_env(),
        stdin=subprocess.DEVNULL,
    )


def _json_out(binary: str, *args: str, timeout: float) -> Any:
    """运行 ``binary args`` 并把 stdout 解析为 JSON；任意失败则返回 ``None``。"""
    raw = (_run(binary, *args, timeout=timeout).stdout or "").strip()
    return json.loads(raw) if raw else None


def _doctor(binary: str) -> Optional[Dict[str, Any]]:
    """``cua-driver doctor --json`` → ``{ok, checks:[{label,status,message}]}``。"""
    try:
        data = _json_out(binary, "doctor", "--json", timeout=12)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    checks: List[Dict[str, str]] = [
        {
            "label": str(p.get("label", "")),
            "status": str(p.get("status", "")),
            "message": str(p.get("message", "")),
        }
        for p in data.get("probes", [])
        if isinstance(p, dict)
    ]
    return {"ok": bool(data.get("ok")), "checks": checks}


def _mac_permissions(binary: str, out: Dict[str, Any]) -> None:
    """把 ``cua-driver permissions status --json`` 的布尔值合并进 ``out``。"""
    try:
        data = _json_out(binary, "permissions", "status", "--json", timeout=10)
    except subprocess.TimeoutExpired:
        out["error"] = "cua-driver permissions status timed out"
        return
    except Exception as exc:  # 启动失败或 JSON 格式错误
        out["error"] = f"cua-driver permissions status failed: {exc}"
        return
    if isinstance(data, dict):
        out.update({k: data[k] for k in _BOOLS if isinstance(data.get(k), bool)})
        if isinstance(data.get("source"), dict):
            out["source"] = data["source"]


def computer_use_status(driver_cmd: Optional[str] = None) -> Dict[str, Any]:
    """面向桌面卡片的、感知操作系统的统一 Computer Use 就绪状态。

    ``ready`` 是 UI 依赖的唯一信号：在 macOS 上它等于两项 TCC 授权都满足；
    在其他平台上等于驱动健康（没有 TCC 模型）。``None`` 表示未知（缺少
    二进制 / 探测失败）。``can_grant`` 仅适用于 macOS。
    """
    plat = sys.platform
    binary = shutil.which(_driver_cmd(driver_cmd))
    out: Dict[str, Any] = {
        "platform": plat,
        "platform_supported": plat in _RUNTIME_PLATFORMS,
        "installed": bool(binary),
        "version": None,
        "ready": None,
        "can_grant": plat == "darwin",
        "checks": [],
        "source": None,
        "error": None,
        **{k: None for k in _BOOLS},
    }
    if not binary:
        return out

    try:
        out["version"] = (_run(binary, "--version", timeout=5).stdout or "").strip() or None
    except Exception:
        pass

    doctor = _doctor(binary)
    if doctor is not None:
        out["checks"] = doctor["checks"]

    if plat == "darwin":
        _mac_permissions(binary, out)
        if out["error"] is None:
            out["ready"] = out["accessibility"] is True and out["screen_recording"] is True
    elif doctor is not None:
        # 非 macOS 没有 TCC 模型 —— 就绪即驱动健康。
        out["ready"] = doctor["ok"]
    return out


def request_permissions_grant(driver_cmd: Optional[str] = None) -> int:
    """运行 ``cua-driver permissions grant``（macOS）；流式输出其内容。

    通过 LaunchServices 启动 CuaDriver，使 TCC 对话框归因到
    ``com.trycua.driver``，随后等待授权完成。返回驱动的退出码（0 为成功）；
    二进制缺失返回 2；非 macOS 平台返回 64（该平台没有可授予的 TCC 权限
    模型）。
    """
    if sys.platform != "darwin":
        print("Computer Use permissions are a macOS concept; nothing to grant here.")
        return 64

    binary = shutil.which(_driver_cmd(driver_cmd))
    if not binary:
        print("cua-driver: not installed. Run: hermes computer-use install")
        return 2

    print(
        "Requesting Accessibility + Screen Recording for CuaDriver.\n"
        "macOS will show a dialog attributed to CuaDriver (com.trycua.driver) — "
        "approve it, then return here."
    )
    try:
        return int(
            subprocess.run(
                [binary, "permissions", "grant"],
                env=_child_env(),
                stdin=subprocess.DEVNULL,
            ).returncode
        )
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 130
    except Exception as exc:  # pragma: no cover - defensive
        print(f"cua-driver permissions grant failed: {exc}", file=sys.stderr)
        return 2
