"""Cua-driver 后端（macOS、Windows、Linux）。

通过 stdio 与 `cua-driver` 进行 MCP 通信。Python 的 `mcp` SDK 是异步的，
因此我们在后台线程上运行一个专用的 asyncio 事件循环，并通过它把同步
调用编组（marshal）进去。

同一套 `cua-driver call <tool>` 表面（click、type_text、hotkey、drag、
scroll、screenshot、launch_app、list_apps、list_windows、get_window_state、
move_cursor、wait）在 macOS、Windows 和 Linux 上工作方式完全一致 ——
cua-driver 的 PARITY 矩阵把这些操作工具在跨平台 Rust 移植
（`cua-driver-rs`）中标记为已在 macOS 和 Windows 上 VERIFIED。

Linux 是最近加入的运行时（当前为 X11，Wayland 通过 XWayland 支持；纯
Wayland 的进展在上游跟踪中）。它在 `check_computer_use_requirements`
中与 macOS、Windows 一同启用。本文件中的底层管线是操作系统无关的；
单机差异（无 DISPLAY、缺少 AT-SPI 等）会通过 `hermes computer-use doctor`
呈现为具体的受阻检查，而不是无声失败。

安装：
  - **macOS**：
      /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)"
  - **Windows**（PowerShell）：
      irm https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.ps1 | iex

安装后，`cua-driver` 会出现在 $PATH 上，并支持 `cua-driver mcp`（stdio
传输），这正是我们调用的方式。

macOS 路径使用私有的 SkyLight SPI（SLEventPostToPid、
SLPSPostEventRecordTo、_AXObserverAddNotificationAndCheckRemote），这些
并非 Apple 公开接口，可能在系统更新时失效。cua-driver-rs 中的 Windows
路径使用稳定的 Win32 API（SendInput + UI Automation）—— 不受同类 SPI
失效问题影响。
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from typing import Any, Dict, List, Optional, Tuple

from tools.computer_use.backend import (
    ActionResult,
    CaptureResult,
    ComputerUseBackend,
    UIElement,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 更新检查
# ---------------------------------------------------------------------------
#
# cua-driver 自带原生的 `check-update` 动词（以及一个 `check_for_update` MCP
# 工具），用于把已安装的二进制与最新的 GitHub release —— 真相来源 ——
# 进行比较，并缓存结果（约 20 小时）。我们更倾向于这种方式，而不是写死
# 一个版本下限，因为版本下限会过时，也无法知道"最新"是什么。
#
# 这里有意没有版本*锁定*旋钮：上游安装器总是抓取最新 release，因此一个
# `HERMES_CUA_DRIVER_VERSION` 环境变量只会*看起来*像被锁定。要得到可复现
# 的版本，请把 `HERMES_CUA_DRIVER_CMD` 指向某个具体的二进制。

_CUA_DRIVER_CMD = os.environ.get("HERMES_CUA_DRIVER_CMD", "cua-driver")
_CUA_DRIVER_ARGS = ["mcp"]  # stdio MCP 传输（当驱动未暴露 `manifest` 时的
                            # 回退方案 —— 见下文 `_resolve_mcp_invocation`）

# 全屏 / 桌面截取。cua-driver 是一个面向窗口（window-oriented）的驱动 ——
# 它的 `get_window_state` / `screenshot` 工具截取的是单个窗口（按
# pid + window_id），并且没有哪个 MCP 工具能把整个虚拟桌面或任意显示器
# 作为一张图截取出来。但操作系统 shell 表面本身（桌面背景和
# 任务栏/菜单栏）是真实窗口，会出现在 `list_windows` 中，因此"给我看屏幕"
# /"点击任务栏"可以通过定位这些窗口来实现。当 `app` 是这些哨兵值之一时，
# capture() 会解析到桌面/shell 窗口，而不是某个应用窗口。
_SCREEN_CAPTURE_SENTINELS = {"screen", "desktop", "fullscreen", "full screen", "all"}

# 跨平台的已知 shell/桌面窗口标识符。作为子串、大小写不敏感地同时匹配
# 窗口的 app_name 及其标题（cua-driver 在这里呈现的是 Win32 类名 / 应用名）。
#   Windows：Progman / WorkerW 支撑桌面；Shell_TrayWnd 是任务栏。
#   macOS：Finder 拥有桌面；菜单栏 / Dock 是 shell。
_DESKTOP_WINDOW_NAMES = (
    "progman", "workerw", "program manager",  # Windows 桌面
    "shell_traywnd", "taskbar",               # Windows 任务栏
    "finder", "desktop", "dock",              # macOS 桌面 / shell
)


# cua-driver 读取该环境变量以门控其匿名使用遥测（PostHog）。
# 设为 "0" 即禁用遥测；缺失时 => 二进制自身的默认值（上游默认开启遥测）。
_CUA_TELEMETRY_ENV_VAR = "CUA_DRIVER_RS_TELEMETRY_ENABLED"


def _cua_telemetry_disabled() -> bool:
    """当 Hermes 应当为该用户禁用 cua-driver 遥测时返回 True。

    从 config.yaml 读取 ``computer_use.cua_telemetry``。默认为 False
    （遥测关闭）。任何读取配置失败时都安全失败 —— 朝着保护隐私的
    "禁用遥测"默认值方向失败。
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        cu = cfg.get("computer_use") or {}
        # 主动选择标志：True => 用户想要遥测 => 不要禁用。
        return not bool(cu.get("cua_telemetry", False))
    except Exception:
        # 配置无法读取 —— 默认禁用遥测（安全失败）。
        return True


def cua_driver_child_env(base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """返回用于拉起 cua-driver 的环境变量 dict。

    从 ``base_env``（默认为 ``os.environ``）开始，当遥测被禁用（默认情况）
    时注入 ``CUA_DRIVER_RS_TELEMETRY_ENABLED=0``。当用户已主动选择开启时，
    该变量保持不动，让 cua-driver 使用其自身默认值。所有 cua-driver 拉起
    点（MCP 后端、status、doctor、install）都使用本函数，从而一致地应用
    该策略。
    """
    env = dict(base_env if base_env is not None else os.environ)
    if _cua_telemetry_disabled():
        env[_CUA_TELEMETRY_ENV_VAR] = "0"
    return env


def _resolve_mcp_invocation(
    driver_cmd: str,
    *,
    timeout: float = 6.0,
) -> Tuple[str, List[str]]:
    """返回用于拉起 cua-driver 的 stdio MCP 服务器的 ``(command, args)``。

    NousResearch/hermes-agent#47072 的 Surface 8：我们不写死 ``["mcp"]``，
    而是通过 ``cua-driver manifest``（trycua/cua#1961）询问驱动自身。该
    manifest 携带一个稳定的 ``mcp_invocation`` 指针，其中同时包含
    ``command`` 和 ``args``，因此未来某个重命名或迁移了该子命令的
    cua-driver 仍能继续工作，而无需 Hermes 打补丁。

    对于不暴露 ``manifest`` 的旧驱动，或任何不确定的失败，回退到
    ``(driver_cmd, ["mcp"])`` —— 封装器不能仅仅因为这一跳发现失败就
    拒绝启动。
    """
    try:
        proc = subprocess.run(
            [driver_cmd, "manifest"],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except Exception:
        return driver_cmd, list(_CUA_DRIVER_ARGS)
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        return driver_cmd, list(_CUA_DRIVER_ARGS)
    try:
        manifest = json.loads(out)
    except (ValueError, TypeError):
        return driver_cmd, list(_CUA_DRIVER_ARGS)
    if not isinstance(manifest, dict):
        return driver_cmd, list(_CUA_DRIVER_ARGS)
    invocation = manifest.get("mcp_invocation")
    if not isinstance(invocation, dict):
        return driver_cmd, list(_CUA_DRIVER_ARGS)
    args = invocation.get("args")
    command = invocation.get("command")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return driver_cmd, list(_CUA_DRIVER_ARGS)
    if not isinstance(command, str) or not command:
        # 驱动知道子命令，但没有暴露它自己的路径。
        # 保留我们已解析出的 driver_cmd；args 仍然是权威的。
        return driver_cmd, args
    return command, args

# 用于从 get_window_state 的 AX 树 markdown 中解析元素行的正则。
#
# 处理不同 cua-driver 版本输出的两种格式：
#   经典： "  - [N] AXRole \"label\""
#   新版： "[N] AXRole (order) id=Label"
#
# 分组 1：元素索引
# 分组 2：AX 角色
# 分组 3：带引号的 label（经典格式）
# 分组 4：id= label（新格式）
_ELEMENT_LINE_RE = re.compile(
    r'^\s*(?:-\s+)?\[(\d+)\]\s+(\w+)(?:\s+"([^"]*)"|(?:\s+\(\d+\))?\s+id=([^\s\[\]]*))?' ,
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _is_macos() -> bool:
    return sys.platform == "darwin"


def cua_driver_binary_available() -> bool:
    """当 `cua-driver` 在 $PATH 上或 HERMES_CUA_DRIVER_CMD 可解析时返回 True。"""
    return bool(shutil.which(_CUA_DRIVER_CMD))


def cua_driver_update_check(*, timeout: float = 8.0) -> Optional[Dict[str, Any]]:
    """运行 ``cua-driver check-update --json`` 并返回其解析后的状态。

    该 payload 镜像了 ``check_for_update`` MCP 工具：
    ``{current_version, latest_version, update_available, ...}``。

    当结果不确定时返回 ``None``（调用方应保持沉默）：二进制缺失、驱动
    太旧而不支持该动词（早于 trycua/cua#1734）、GitHub 检查失败（设置了
    ``error`` 字段）、或输出无法解析。尽力而为；永不抛异常。
    """
    try:
        proc = subprocess.run(
            [_CUA_DRIVER_CMD, "check-update", "--json"],
            capture_output=True, text=True, timeout=timeout,
            # 某些较旧的驱动没有该动词，会进入读 stdin 的模式而不是报错 ——
            # DEVNULL 给它们 EOF，让它们快速退出，而不是阻塞到超时。
            stdin=subprocess.DEVNULL,
            env=cua_driver_child_env(),
        )
    except Exception:
        return None
    out = (proc.stdout or "").strip()
    if not out:
        # 较旧的驱动没有该动词：用法说明输出到 stderr，stdout 为空。
        return None
    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("error"):
        # 一次失败的检查（exit 1）会把原因放在 `error` 里 —— 不确定。
        return None
    return data


def cua_driver_update_nudge() -> Optional[str]:
    """返回一行"有可用更新"的消息；当已是最新、结果不确定、或驱动太旧
    而无法上报时返回 ``None``。"""
    state = cua_driver_update_check()
    if not state or not state.get("update_available"):
        return None
    latest = state.get("latest_version") or "?"
    current = state.get("current_version") or "?"
    return (
        f"cua-driver {latest} is available (you have {current}); "
        f"update with `hermes computer-use install --upgrade`."
    )


_update_checked = False


def _maybe_nudge_update() -> None:
    """每个进程最多发出一次更新提醒，在线程外执行，这样（缓存约 20 小时的）
    GitHub 轮询永远不会阻塞第一次 computer_use 动作。"""
    global _update_checked
    if _update_checked:
        return
    _update_checked = True

    def _run() -> None:
        try:
            msg = cua_driver_update_nudge()
        except Exception:
            return
        if msg:
            logger.info("computer_use: %s", msg)

    threading.Thread(
        target=_run, name="cua-driver-update-check", daemon=True
    ).start()


def cua_driver_install_hint() -> str:
    if sys.platform == "win32":
        installer = (
            '  irm https://raw.githubusercontent.com/trycua/cua/main/'
            'libs/cua-driver/scripts/install.ps1 | iex'
        )
    else:
        installer = (
            '  /bin/bash -c "$(curl -fsSL '
            'https://raw.githubusercontent.com/trycua/cua/main/'
            'libs/cua-driver/scripts/install.sh)"'
        )
    return (
        "cua-driver is not installed. Install with one of:\n"
        "  hermes computer-use install\n"
        "Or run the upstream installer directly:\n"
        f"{installer}\n"
        "Or run `hermes tools` and enable the Computer Use toolset to install it automatically."
    )


def _parse_elements_from_tree(markdown: str) -> List[UIElement]:
    """从 get_window_state 的 AX 树 markdown 中解析 UIElement 列表。

    对于不携带规范 ``structuredContent.elements`` 数组的 cua-driver 构建，
    这是最后手段的回退方案（见 ``_parse_elements_from_structured`` ——
    #47072 的 Surface 2 更优先那条路径）。

    同时处理经典 ``"label"`` 引号格式和 cua-driver v0.1.6 引入的较新
    ``id=Label`` 格式。Bounds 总是返回 ``(0, 0, 0, 0)``，因为 markdown
    表面并不携带它们 —— 这也是优先使用结构化路径的又一个原因。
    """
    elements = []
    for m in _ELEMENT_LINE_RE.finditer(markdown):
        # group(3) = 带引号的 label（经典）；group(4) = id= label（新版）
        label = m.group(3) or m.group(4) or ""
        elements.append(UIElement(
            index=int(m.group(1)),
            role=m.group(2),
            label=label,
            bounds=(0, 0, 0, 0),
        ))
    return elements


def _parse_elements_from_structured(raw_elements: List[Dict[str, Any]]) -> List[UIElement]:
    """NousResearch/hermes-agent#47072 的 Surface 2：读取 cua-driver-rs 在
    每次 ``get_window_state`` 响应中输出的规范
    ``structuredContent.elements`` 数组（trycua/cua#1961）。

    每个条目至少包含 ``element_index``、``role``、``label``；只要 AT-SPI /
    AXFrame 调用返回了可用的 bounds，就会附带 ``frame``
    （``{x, y, w, h}``）。旧代码通过正则从 markdown 树里解析同样的信息
    （有损：bounds 总是 ``(0, 0, 0, 0)``）—— 这条路径保留了真实的
    frame，这样下游消费者（例如 ``UIElement.center()``）就能基于像素坐标
    工作，而不仅仅是索引查找。

    未知 / 格式错误的条目会被跳过，而不是让整次遍历失败 —— 遇到坏行时
    封装器降级为"更少的元素"，而不是"没有元素"。
    """
    elements: List[UIElement] = []
    for raw in raw_elements:
        if not isinstance(raw, dict):
            continue
        idx = raw.get("element_index")
        if not isinstance(idx, int):
            continue
        role = raw.get("role") if isinstance(raw.get("role"), str) else ""
        label = raw.get("label") if isinstance(raw.get("label"), str) else ""
        frame = raw.get("frame") if isinstance(raw.get("frame"), dict) else None
        bounds: Tuple[int, int, int, int] = (0, 0, 0, 0)
        if frame:
            try:
                bounds = (
                    int(frame.get("x", 0)),
                    int(frame.get("y", 0)),
                    int(frame.get("w", 0)),
                    int(frame.get("h", 0)),
                )
            except (TypeError, ValueError):
                bounds = (0, 0, 0, 0)
        # Surface 6：不透明的 element_token。cua-driver-rs 的格式是
        # `s{snapshot_hex}:{index}`。我们把它当作黑箱字符串对待 ——
        # 驱动自身负责解析 + LRU 语义。
        raw_token = raw.get("element_token")
        token = raw_token if isinstance(raw_token, str) and raw_token else None
        elements.append(UIElement(
            index=idx,
            role=role,
            label=label,
            bounds=bounds,
            element_token=token,
        ))
    return elements


def _image_dimensions_from_bytes(raw: bytes) -> Tuple[int, int]:
    """尽力嗅探 PNG/JPEG 的尺寸，不引入额外依赖。"""
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
        width = int.from_bytes(raw[16:20], "big")
        height = int.from_bytes(raw[20:24], "big")
        if width > 0 and height > 0:
            return width, height

    if raw.startswith(b"\xff\xd8"):
        i = 2
        n = len(raw)
        while i + 9 < n:
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            i += 2
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if i + 2 > n:
                break
            segment_len = int.from_bytes(raw[i:i + 2], "big")
            if segment_len < 2 or i + segment_len > n:
                break
            if marker in {
                0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
            }:
                if segment_len >= 7:
                    height = int.from_bytes(raw[i + 3:i + 5], "big")
                    width = int.from_bytes(raw[i + 5:i + 7], "big")
                    if width > 0 and height > 0:
                        return width, height
                break
            i += segment_len

    return 0, 0


def _split_tree_text(full_text: str) -> Tuple[str, str]:
    """把 get_window_state 的文本拆分为 (summary_line, tree_markdown)。"""
    lines = full_text.split("\n", 1)
    summary = lines[0]
    tree = lines[1] if len(lines) > 1 else ""
    return summary, tree


def _parse_key_combo(keys: str) -> Tuple[Optional[str], List[str]]:
    """把 'cmd+s' 这样的按键字符串解析为 (key, modifiers)。

    返回 (key, modifiers)，其中 key 是非修饰键，modifiers 是修饰键名称
    列表（cmd、shift、option、ctrl）。
    """
    MODIFIER_NAMES = {"cmd", "command", "shift", "option", "alt", "ctrl", "control", "fn"}
    KEY_ALIASES = {"command": "cmd", "alt": "option", "control": "ctrl"}

    parts = [p.strip().lower() for p in re.split(r'[+\-]', keys) if p.strip()]
    modifiers = []
    key = None
    for part in parts:
        normalized = KEY_ALIASES.get(part, part)
        if normalized in MODIFIER_NAMES:
            modifiers.append(normalized)
        else:
            key = part  # 最后一个非修饰键胜出
    return key, modifiers


# ---------------------------------------------------------------------------
# Asyncio 桥接 —— 在后台线程上跑一个长生命周期循环
# ---------------------------------------------------------------------------

class _AsyncBridge:
    """在 daemon 线程上运行一个 asyncio 循环；把协程从调用方编组进来。"""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()

        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._ready.set()
            try:
                self._loop.run_forever()
            finally:
                try:
                    self._loop.close()
                except Exception:
                    pass

        self._thread = threading.Thread(target=_run, daemon=True, name="cua-driver-loop")
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("cua-driver asyncio bridge failed to start")

    def run(self, coro, timeout: Optional[float] = 30.0) -> Any:
        from agent.async_utils import safe_schedule_threadsafe
        if not self._loop or not self._thread or not self._thread.is_alive():
            if asyncio.iscoroutine(coro):
                coro.close()
            raise RuntimeError("cua-driver bridge not started")
        fut = safe_schedule_threadsafe(coro, self._loop)
        if fut is None:
            raise RuntimeError("cua-driver bridge not started")
        return fut.result(timeout=timeout)

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._loop = None


# ---------------------------------------------------------------------------
# MCP 会话（延迟创建，在工具调用间共享）
# ---------------------------------------------------------------------------

class _CuaDriverSession:
    """持有 mcp 的 ClientSession。延迟拉起；丢弃后重新进入。

    生命周期归属：单个长运行协程（`_lifecycle_coro`）打开
    stdio_client 和 ClientSession 两个上下文，填充能力集，设置
    `_ready_event`，然后等待 `_shutdown_event`。当 shutdown 被触发时，
    同一个协程关闭这些上下文 —— 这保持了 anyio 的 cancel-scope 任务
    身份不变式（桥接把每个 `bridge.run(coro)` 当作新任务调度，因此如果
    在一个任务里打开上下文、在另一个任务里关闭，会抛出 "Attempted to
    exit cancel scope in a different task"）。工具调用在各自短生命周期的
    任务里运行；它们只触碰 session 对象，从不触碰外围上下文。
    """

    def __init__(self, bridge: _AsyncBridge) -> None:
        self._bridge = bridge
        self._session = None
        self._lock = threading.Lock()
        self._started = False
        # NousResearch/hermes-agent#47072 的 Surface 4：每个工具的能力 token
        # 集合，在会话初始化时从 `tools/list` 填充。键是工具名（例如
        # "click"、"get_window_state"）；值是能力字符串集合（例如
        # "accessibility.element_tokens"、"input.keyboard.type.terminal_safe"）。
        # 在会话启动前为空；消费者应调用 `supports_capability`，而不是直接读取。
        self._capabilities: Dict[str, set] = {}
        self._capability_version: str = ""
        # 生命周期管线 —— 见上方类 docstring。
        self._ready_event = threading.Event()
        self._shutdown_event: Optional[asyncio.Event] = None  # 在桥接循环上创建
        self._lifecycle_future = None  # concurrent.futures.Future
        self._setup_error: Optional[BaseException] = None

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("cua-driver session not started")

    async def _lifecycle_coro(self) -> None:
        """stdio MCP 上下文的长生命周期拥有者。打开、发出就绪信号、阻塞
        等待 shutdown，然后清理。enter + exit 发生在同一个 asyncio 任务中，
        因此 anyio 的 cancel-scope 不变式成立 —— 修复了之前 _aenter/_aexit
        分离时发出的 "Attempted to exit cancel scope in a different task
        than it was entered in" 警告。
        """
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from tools.environments.local import _sanitize_subprocess_env

        # 在循环所在线程上构建 shutdown 事件，这样该 asyncio 原语属于
        # 正确的循环。
        self._shutdown_event = asyncio.Event()

        try:
            if not cua_driver_binary_available():
                raise RuntimeError(cua_driver_install_hint())

            # Surface 8：询问 cua-driver 自身哪个子命令会拉起 MCP 服务器，
            # 而不是写死 ["mcp"]。对旧驱动 / 任何发现失败都透明回退。
            command, args = _resolve_mcp_invocation(_CUA_DRIVER_CMD)
            params = StdioServerParameters(
                command=command,
                args=args,
                # 先应用遥测策略（默认：禁用），再把 Hermes 管理的密钥
                # 从子进程环境中清理掉。
                env=_sanitize_subprocess_env(cua_driver_child_env()),
            )

            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    # 在把会话暴露给调用方之前，先填充能力集 +
                    # capability_version，这样第一次工具调用就能看到它们。
                    await self._populate_capabilities(session)
                    self._session = session
                    self._ready_event.set()
                    # 保持上下文打开，直到 stop() / restart 要求我们收尾。
                    # 工具调用作为各自的任务在同一个循环上运行，并直接
                    # 触碰 self._session。
                    await self._shutdown_event.wait()
        except BaseException as e:
            # 捕获普通错误和 anyio 的 CancelledError。
            # 调用方（start()）会检查它，以便把安装失败呈现给同步世界。
            self._setup_error = e
            self._ready_event.set()
            raise
        finally:
            # 在上下文展开之前清空 _session 会让一个竞争的 call_tool 在
            # 拆卸期间看到 None —— 但外层 context-manager 在本代码块之后
            # 才退出，因此在这里置为 None 没问题：stop() 已经翻转了 _started。
            self._session = None

    async def _populate_capabilities(self, session: Any) -> None:
        """Surface 4：从 tools/list 缓存每个工具的能力集合 + capability_version。
        软前提 —— 发现失败会让该映射为空，supports_capability 降级为 False。"""
        try:
            tools_list = await session.list_tools()
            for tool in getattr(tools_list, "tools", []) or []:
                tool_name = getattr(tool, "name", None)
                if not isinstance(tool_name, str):
                    continue
                caps = getattr(tool, "capabilities", None)
                if caps is None:
                    # 某些 MCP SDK 通过 `model_extra`（Pydantic v2）转发
                    # 自定义字段，而不是作为属性。
                    extra = getattr(tool, "model_extra", None) or {}
                    caps = extra.get("capabilities")
                if isinstance(caps, list):
                    self._capabilities[tool_name] = {
                        c for c in caps if isinstance(c, str)
                    }
                else:
                    self._capabilities[tool_name] = set()
            # capability_version 是 tools/list 响应中与 `tools` 同级的顶层
            # 字段。cua-driver-core/src/tool.rs:354 会输出它；
            # cua-driver-core/src/protocol.rs:150 在 initialize 中不输出它 ——
            # 因此我们在这里发现，而不是在那里。
            cv = getattr(tools_list, "capability_version", None)
            if cv is None:
                extra = getattr(tools_list, "model_extra", None) or {}
                cv = extra.get("capability_version")
            if isinstance(cv, str):
                self._capability_version = cv
        except Exception as e:
            logger.debug("cua-driver tools/list capability discovery failed: %s", e)

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._bridge.start()
            self._start_lifecycle_locked()
            self._started = True

    def _start_lifecycle_locked(self) -> None:
        """拉起生命周期拥有者并等待它到达就绪状态。
        调用方必须持有 self._lock。"""
        # 重置每次会话的状态。
        self._ready_event = threading.Event()
        self._setup_error = None
        self._shutdown_event = None
        # 在桥接循环上做 fire-and-forget 式调度。该 future 追踪的是整段
        # 生命周期（打开 → 等待 → 关闭）的完成，而不仅仅是打开步骤 ——
        # start() 单独在 _ready_event 上等待。
        loop = self._bridge._loop
        if loop is None:
            raise RuntimeError("cua-driver bridge not started")
        self._lifecycle_future = asyncio.run_coroutine_threadsafe(
            self._lifecycle_coro(), loop
        )
        if not self._ready_event.wait(timeout=15.0):
            # 尽力而为：如果 future 仍存活，就发 shutdown 信号。
            self._signal_shutdown_locked()
            raise RuntimeError("cua-driver session never reached ready (timeout 15s)")
        # 如果安装失败，生命周期协程会在设置 _ready_event 之前设置
        # _setup_error。在调用方线程上重新抛出它。
        if self._setup_error is not None:
            raise RuntimeError(
                f"cua-driver session setup failed: {self._setup_error}"
            ) from self._setup_error

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._started = False
            self._stop_lifecycle_locked()

    def _stop_lifecycle_locked(self) -> None:
        """发出 shutdown 信号 + 等待生命周期协程收尾。
        调用方必须持有 self._lock。"""
        self._signal_shutdown_locked()
        fut = self._lifecycle_future
        if fut is None:
            return
        try:
            # 给上下文展开（stdio_client 拆卸）5 秒预算。
            fut.result(timeout=5.0)
        except concurrent.futures.TimeoutError:
            logger.warning("cua-driver session shutdown timed out (5s)")
        except Exception as e:
            # 真实的 shutdown 错误（不再是之前那个 cancel-scope 竞争，
            # 那个现在结构上已不可能）仍然会被呈现出来。
            logger.warning("cua-driver shutdown error: %s", e)
        finally:
            self._lifecycle_future = None

    def _signal_shutdown_locked(self) -> None:
        """从调用方线程设置 asyncio 的 shutdown 事件。"""
        loop = self._bridge._loop
        event = self._shutdown_event
        if loop is not None and event is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                # 循环已关闭 —— 无可发信号。
                pass

    async def _call_tool_async(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        result = await self._session.call_tool(name, args)
        return _extract_tool_result(result)

    # ── 能力检测（#47072 的 Surface 4） ───────────────────────────────
    def supports_capability(self, capability: str, tool: Optional[str] = None) -> bool:
        """当已连接的 cua-driver 宣告了给定的能力 token
        （trycua/cua#1961 能力词汇表）时返回 True。

        当给出 ``tool`` 时，把检查范围限定在该特定工具宣告的能力集合。
        省略时，只要任一工具宣告了该能力就返回 True —— 这对"该功能在
        驱动上是否可用"的探测很有用。

        在会话启动之前总是返回 False（这样处于死亡/未初始化封装器上的
        消费者会降级而不是崩溃）。
        """
        if tool is not None:
            return capability in self._capabilities.get(tool, set())
        return any(capability in caps for caps in self._capabilities.values())

    def _has_tool(self, name: str) -> bool:
        """当 ``tools/list`` 宣告了该名称的工具时返回 True。

        用于 capture() 的路由：cua-driver 删除了独立的 ``screenshot`` 工具，
        并把整窗口 PNG 截图折叠进了 ``get_window_state``（它自身的描述指出
        它"Also captures a PNG screenshot of the specified window"）。仍然暴露
        ``screenshot`` 的旧驱动继续使用它；较新的驱动回退到
        ``get_window_state``。

        当发现过程尚未填充该映射时返回 False —— 调用方把这种情况当作
        "未知"，并防御性地探测，而不是轻信它。
        """
        return name in self._capabilities

    @property
    def capabilities_discovered(self) -> bool:
        """一旦 ``tools/list`` 填充了每个工具的映射就返回 True。当为 False 时，
        ``_has_tool`` 的回答不可信（发现失败或会话未启动），capture() 应
        防御性地探测。"""
        return bool(self._capabilities)

    @property
    def capability_version(self) -> str:
        """驱动宣告的能力词汇表版本（当驱动早于该字段时为空字符串 ——
        旧构建没有版本）。"""
        return self._capability_version

    @staticmethod
    def _is_closed_session_error(exc: Exception) -> bool:
        """对于可通过重连恢复的 MCP/stdio 失败返回 True。"""
        name = exc.__class__.__name__
        module = getattr(exc.__class__, "__module__", "")
        return (
            name in {"ClosedResourceError", "BrokenResourceError", "EndOfStream"}
            or (module.startswith("anyio") and "Resource" in name)
            or isinstance(exc, (BrokenPipeError, EOFError))
        )

    def _restart_session_locked(self) -> None:
        """在 daemon/stdin 传输被关闭后重建 MCP 会话。
        调用方必须持有 self._lock（仅重连一次的重试路径持有它）。"""
        if self._started:
            try:
                self._stop_lifecycle_locked()
            except Exception as e:
                logger.debug("cua-driver session cleanup before reconnect failed: %s", e)
        self._started = False
        # 清除陈旧的能力状态；下一次 start 会从头填充。
        self._capabilities = {}
        self._capability_version = ""
        self._start_lifecycle_locked()
        self._started = True

    def call_tool(self, name: str, args: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        self._require_started()
        try:
            return self._bridge.run(self._call_tool_async(name, args), timeout=timeout)
        except Exception as e:
            if not self._is_closed_session_error(e):
                raise
            # daemon 重启会关闭缓存的 stdio 通道。重连一次并恰好再重试一次 ——
            # 永不循环，以免猛砸一个真正死掉的 daemon。
            logger.warning("cua-driver MCP session closed during %s; reconnecting once", name)
            with self._lock:
                self._restart_session_locked()
            return self._bridge.run(self._call_tool_async(name, args), timeout=timeout)


def _extract_tool_result(mcp_result: Any) -> Dict[str, Any]:
    """把一个 mcp CallToolResult 转换为普通 dict。

    cua-driver 返回的是 text parts、image parts 和 structuredContent 的混合。
    我们把它们展平为：
      {
        "data": <文本或解析后的 json>,
        "images": [b64, ...],
        "image_mime_types": [mime, ...],   # 与 `images` 一一对应，缺失时为 ""
        "structuredContent": <dict|None>,
        "isError": bool,
      }
    structuredContent 从 MCP 结果的 structuredContent 字段填充
    （MCP 规范 §2024-11-05+），对于像 list_windows 窗口数组这样的结构化
    数据，它优先于其他来源。

    `image_mime_types` 是 cua-driver 自 trycua/cua#1961（NousResearch/
    hermes-agent#47072 的 Surface 7）起在每个 image part 上显式输出的
    `mimeType`。每个条目与 `images` 一一对应；空字符串条目表示该 part
    没有携带 mimeType（较旧的 cua-driver 构建），调用方应回退到 base64
    前缀嗅探。
    """
    data: Any = None
    images: List[str] = []
    image_mime_types: List[str] = []
    is_error = bool(getattr(mcp_result, "isError", False))
    structured: Optional[Dict] = getattr(mcp_result, "structuredContent", None) or None
    text_chunks: List[str] = []
    for part in getattr(mcp_result, "content", []) or []:
        ptype = getattr(part, "type", None)
        if ptype == "text":
            text_chunks.append(getattr(part, "text", "") or "")
        elif ptype == "image":
            b64 = getattr(part, "data", None)
            if b64:
                images.append(b64)
                mime = getattr(part, "mimeType", None) or ""
                image_mime_types.append(mime)
    if text_chunks:
        joined = "\n".join(t for t in text_chunks if t)
        try:
            data = json.loads(joined) if joined.strip().startswith(("{", "[")) else joined
        except json.JSONDecodeError:
            data = joined
    return {
        "data": data,
        "images": images,
        "image_mime_types": image_mime_types,
        "structuredContent": structured,
        "isError": is_error,
    }


def _image_from_tool_result(out: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """从展平后的工具结果中提取 (png_b64, mime_type) 对。

    cua-driver 根据工具 + 传输方式，以两种形态投递窗口截图：

      * 作为 MCP 的 ``image`` 内容 part —— 由 ``_extract_tool_result``
        呈现在 ``out["images"]`` 中，并带有一个并列的
        ``image_mime_types`` 条目。这是 ``get_window_state`` 通过
        stdio MCP 传输时输出的形态。
      * 作为 ``structuredContent`` 内的一个 base64 字段 ——
        ``screenshot_png_b64``（+ ``screenshot_mime_type``）。这是当
        ``get_window_state`` 的结构化 payload 携带图像而不是内容 part 时
        返回的形态（较新的驱动构建；也是通过 ``cua-driver call`` CLI
        表面看到的形态）。

    同时检查两者让 capture() 对任一投递形态都健壮，因此图像不会仅仅因为
    驱动把它在内容列表与 structuredContent 之间挪动就无声丢失。当两个
    位置都没有图像时返回 ``(None, None)``。
    """
    images = out.get("images") or []
    if images and images[0]:
        mimes = out.get("image_mime_types") or []
        mime = mimes[0] if mimes and mimes[0] else None
        return images[0], mime

    structured = out.get("structuredContent") or {}
    b64 = structured.get("screenshot_png_b64") or structured.get("png_b64")
    if b64:
        mime = (
            structured.get("screenshot_mime_type")
            or structured.get("mime_type")
            or None
        )
        return b64, mime

    return None, None


# ---------------------------------------------------------------------------
# 后端本体
# ---------------------------------------------------------------------------

class CuaDriverBackend(ComputerUseBackend):
    """默认的 computer-use 后端。通过 cua-driver MCP 实现跨平台。"""

    def __init__(self) -> None:
        self._bridge = _AsyncBridge()
        self._session = _CuaDriverSession(self._bridge)
        # 黏性上下文 —— 由 capture() 更新，被动作工具使用。
        self._active_pid: Optional[int] = None
        self._active_window_id: Optional[int] = None
        self._last_app: Optional[str] = None  # 通过 capture/focus_app 定位的上一个应用名
        # NousResearch/hermes-agent#47072 的 Surface 6：每次截图的
        # `element_index -> element_token` 映射，在 capture() 时填充。
        # 动作工具（click/scroll/set_value/...）在 `element_index` 旁边附上
        # 匹配的 token，这样 cua-driver 会显式检测"陈旧"，而不是无声地重新
        # 解析到另一个元素。每当一次新的截图覆盖截图上下文时就清空它。
        self._snapshot_tokens: Dict[int, str] = {}
        # 每个实例的 cua-driver 会话 id。cua-driver 的 MCP 服务器说明要求
        # 每个消费者在一次运行开始时声明一个稳定会话（start_session），
        # 并在结束时拆除它（end_session）。这样做：
        #   - 让每次 Hermes 运行得到一种独特的 agent 光标颜色，并附带
        #     覆盖层渲染，可视化动作落在何处（而不移动真实的 OS 光标）。
        #   - 隔离每次会话的配置 + 录制归属，这样并发的 Hermes 运行 /
        #     子代理不会互相踩踏。
        # 我们为每个 CuaDriverBackend 实例铸造一次基于 UUID4 的 id ——
        # 一次 Hermes 运行 = 一个后端 = 一个会话 —— 并在每次 cua-driver
        # 工具调用时把它作为 `session` 传入。会话在 cua-driver 一侧是
        # 附加功能：当我们的 id 对驱动未知时（较旧的构建），工具调用会
        # 降级到 MCP 服务器说明中记录的匿名 / 不同步路径。
        self._session_id: str = f"hermes-{uuid.uuid4().hex[:12]}"

    # ── 生命周期 ────────────────────────────────────────────────────
    def start(self) -> None:
        _maybe_nudge_update()
        # MCP 客户端 SDK（`mcp`）是可选依赖（`computer-use` / `mcp`
        # extras），不属于 Hermes 的最小核心。在首次使用时延迟安装 ——
        # 与所有其他可选后端相同的模式 —— 这样用户永远不会在调用时
        # 遇到 `No module named 'mcp'` 这种晦涩错误。自动安装由
        # `security.allow_lazy_installs` 门控（默认开启）；当它被禁用或
        # 失败时，ensure() 会抛出 FeatureUnavailable，携带可操作的
        # `uv pip install mcp==…` 提示，并通过 tool.py 的后端不可用路径
        # 呈现出来。
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("tool.computer_use", prompt=False)
        # 刚安装的包在刷新本进程的导入机制缓存之前可能无法导入。
        import importlib
        importlib.invalidate_caches()
        self._session.start()

        # 向 cua-driver 声明本次运行的会话身份。引自 cua-driver 服务器
        # 说明："start_session(session) once at the start of a run →
        # 声明本次运行的身份（由你选择的一个稳定 id）。在下面的每个动作上
        # 都传入同一个 `session`。它拥有你的 agent 光标（每个 id 一种独特
        # 颜色），并跟随本次运行跨越应用/窗口。"
        # 启动会话失败是非致命的 —— cua-driver 的工具接受匿名调用
        # （只是光标不会渲染），因此我们降级而不是中止。
        try:
            self._session.call_tool("start_session", {"session": self._session_id})
        except Exception as e:
            logger.debug("cua-driver start_session failed (continuing anonymous): %s", e)

    def stop(self) -> None:
        # 在断开连接之前先拆除 cua-driver 会话，以便驱动清理每次会话的
        # 状态（光标覆盖层、录制归属、配置覆盖）。尽力而为 —— 即使它
        # 失败，下面的连接断开也会通过 cua-driver 内部注册的 session_end
        # 钩子释放 daemon 侧的状态。
        if self._session._started:
            try:
                self._session.call_tool("end_session", {"session": self._session_id})
            except Exception as e:
                logger.debug("cua-driver end_session failed (continuing teardown): %s", e)
        try:
            self._session.stop()
        finally:
            self._bridge.stop()

    def is_available(self) -> bool:
        # cua-driver 在 macOS、Windows 和 Linux 上运行。Linux 路径是最近
        # 加入的（截至 2026 年中，上游已同时支持 X11 + Wayland）。自行
        # 风险地覆盖平台检查：其他类 Unix 系统尚未端到端验证过。
        if sys.platform not in ("darwin", "win32", "linux"):
            return False
        return cua_driver_binary_available()

    # ── 截图 ────────────────────────────────────────────────────────
    def capture(self, mode: str = "som", app: Optional[str] = None) -> CaptureResult:
        """截取最前面的屏幕窗口（可按应用名过滤）。

        把 hermes 的 `capture(mode, app)` 映射到 cua-driver 的
        `list_windows` + `get_window_state`（ax/som）或 `screenshot`
        （vision）。
        """
        # 第 1 步：枚举屏幕上的窗口，找到目标 pid/window_id。
        # NousResearch/hermes-agent#47072 的 Surface 3：直接读取规范的
        # `structuredContent.windows` 数组。修复之前，封装器还保留了一个
        # 文本行正则（`_WINDOW_LINE_RE`）作为早于 structuredContent 的
        # cua-driver 构建的回退；取代 PR 的有效最低版本
        # （trycua/cua#1961 + #1908）早已过了那个阶段，因此回退已移除 ——
        # 封装器现在把结构化形态视为唯一契约。
        lw_out = self._session.call_tool(
            "list_windows",
            {"on_screen_only": True, "session": self._session_id},
        )
        raw_windows = (lw_out.get("structuredContent") or {}).get("windows") or []
        windows = [
            {
                "app_name": w.get("app_name", ""),
                "pid": int(w["pid"]),
                "window_id": int(w["window_id"]),
                "off_screen": not w.get("is_on_screen", True),
                "title": w.get("title", ""),
                "z_index": w.get("z_index", 0),
            }
            for w in raw_windows
        ]
        # 按 z_index 降序排序（z_index 最低 = macOS 上最靠前）。
        windows.sort(key=lambda w: w["z_index"])

        if not windows:
            return CaptureResult(mode=mode, width=0, height=0, png_b64=None,
                                 elements=[], app="", window_title="", png_bytes_len=0)

        # 如果请求了，则按应用名过滤（大小写不敏感的子串匹配）。
        # 当过滤器什么都没匹配到时，显式呈现这一点，而不是无声地截取最前面
        # 的窗口 —— 在 macOS 上 list_windows 返回的 `app_name` 是本地化名称
        # （例如 "計算機"），因此 `app="Calculator"` 在非英语系统上合法地
        # 匹配不到任何窗口，调用方需要用本地化名称重试。
        if app and app.strip().lower() in _SCREEN_CAPTURE_SENTINELS:
            # 全屏 / 桌面请求。cua-driver 没有虚拟桌面截取工具，因此解析到
            # OS shell/桌面窗口（桌面背景或任务栏/菜单栏），list_windows
            # 确实会呈现它们。这让"给我看屏幕"和"点击任务栏"能工作；
            # 单张图像仍然无法跨越多个显示器 —— 这是驱动的限制，而非封装器
            # 的限制。
            def _is_desktop_window(w: Dict[str, Any]) -> bool:
                haystack = f"{w.get('app_name', '')} {w.get('title', '')}".lower()
                return any(name in haystack for name in _DESKTOP_WINDOW_NAMES)

            desktop = [w for w in windows if _is_desktop_window(w)]
            if not desktop:
                return CaptureResult(
                    mode=mode, width=0, height=0, png_b64=None,
                    elements=[], app="",
                    window_title=(
                        f"<no desktop/shell window found for app={app!r}; "
                        f"cua-driver captures one window at a time and exposes "
                        f"no whole-virtual-desktop or per-monitor capture. "
                        f"Call list_apps / capture(app='<AppName>') to target a "
                        f"specific window instead. On Windows the taskbar is "
                        f"'Shell_TrayWnd' and the desktop is 'Progman'.>"
                    ),
                    png_bytes_len=0,
                )
            # 当桌面背景和任务栏同时存在时，优先选桌面背景
            # （Progman/WorkerW/Finder），这样裸的 "screen" 截图展示的是
            # 完整桌面，而不仅仅是任务栏条。
            windows = sorted(
                desktop,
                key=lambda w: 0 if any(
                    n in f"{w.get('app_name', '')} {w.get('title', '')}".lower()
                    for n in ("progman", "workerw", "program manager", "finder", "desktop")
                ) else 1,
            )
        elif app:
            app_lower = app.lower()
            filtered = [w for w in windows if app_lower in w["app_name"].lower()]
            if not filtered:
                return CaptureResult(
                    mode=mode, width=0, height=0, png_b64=None,
                    elements=[], app="",
                    window_title=(
                        f"<no on-screen window matched app={app!r}; "
                        f"call list_apps to see available app names "
                        f"(macOS reports localized names, e.g. '計算機' "
                        f"instead of 'Calculator')>"
                    ),
                    png_bytes_len=0,
                )
            windows = filtered

        # 选第一个屏幕上的窗口（上面按 z_index / z-order 排序过）。
        target = next((w for w in windows if not w["off_screen"]), windows[0])
        self._active_pid = target["pid"]
        self._active_window_id = target["window_id"]
        app_name = target["app_name"]
        # 记录解析出的应用名，这样 capture_after= 后续动作可以重新定位
        # 同一个应用，而不是回退到最前面的窗口。
        if app or not self._last_app:
            self._last_app = app_name

        # 第 2 步：截图。
        png_b64: Optional[str] = None
        image_mime_type: Optional[str] = None
        elements: List[UIElement] = []
        width = height = 0
        window_title = ""

        if mode == "vision":
            # 纯截图，不走 AX 遍历。cua-driver 删除了独立的 `screenshot` 工具
            # （≥0.5.x），并把整窗口 PNG 截图折叠进了 `get_window_state`。据此
            # 路由：
            #   * 驱动宣告了 `screenshot`（较旧的构建）→ 使用它；它是最廉价
            #     的路径（服务器侧不遍历 AX 树）。
            #   * 否则（当前驱动）→ 调用 `get_window_state` 但丢弃 AX 树/
            #     元素，只返回 PNG。vision 模式的全部契约就是"只要像素，不要
            #     元素噪声"，因此我们丢弃图像之外的一切。
            # 当能力发现尚未运行（映射为空）时，我们不信任否定的
            # `_has_tool` 回答 —— 我们仍先尝试 `screenshot`，如果驱动拒绝
            # 就回退，这样该路径在任何驱动版本上都能自愈。
            use_screenshot = (
                self._session._has_tool("screenshot")
                or not self._session.capabilities_discovered
            )
            sc_out: Optional[Dict[str, Any]] = None
            if use_screenshot:
                sc_out = self._session.call_tool(
                    "screenshot",
                    {
                        "window_id": self._active_window_id,
                        "format": "jpeg",
                        "quality": 85,
                        "session": self._session_id,
                    },
                )
                png_b64, image_mime_type = _image_from_tool_result(sc_out)
                if not png_b64:
                    # 驱动没有可用的 `screenshot`（例如 ≥0.5.x 上的
                    # "Unknown tool: screenshot"，或一个空的 image part）。
                    # 回退到下面的 get_window_state 路径。
                    sc_out = None

            if sc_out is None:
                gws_out = self._session.call_tool(
                    "get_window_state",
                    {
                        "pid": self._active_pid,
                        "window_id": self._active_window_id,
                        "session": self._session_id,
                    },
                )
                png_b64, image_mime_type = _image_from_tool_result(gws_out)
                # 仍然抓取窗口标题 —— 它很廉价且在 vision 响应中很有用 ——
                # 但刻意让 `elements` 为空，这样 vision 就没有 AX 树噪声。
                text = gws_out["data"] if isinstance(gws_out["data"], str) else ""
                _, tree = _split_tree_text(text)
                wt = re.search(r'AXWindow\s+"([^"]+)"', tree)
                if wt:
                    window_title = wt.group(1)
        else:
            # get_window_state：AX 树 + 截图。
            gws_out = self._session.call_tool(
                "get_window_state",
                {
                    "pid": self._active_pid,
                    "window_id": self._active_window_id,
                    "session": self._session_id,
                },
            )
            text = gws_out["data"] if isinstance(gws_out["data"], str) else ""
            summary, tree = _split_tree_text(text)

            # 从摘要中解析元素数量，例如 "✅ AppName — 42 elements, turn 3..."
            m = re.search(r'(\d+)\s+elements?', summary)

            # NousResearch/hermes-agent#47072 的 Surface 2：优先使用规范的
            # structuredContent.elements 数组（trycua/cua#1961）。对于不携带
            # 结构化形态的 cua-driver 构建，回退到 markdown 正则解析 —— 那些
            # bounds 会返回 (0,0,0,0)；结构化路径则保留真实 frame。
            sc_elements = (gws_out.get("structuredContent") or {}).get("elements")
            if isinstance(sc_elements, list) and sc_elements:
                elements = _parse_elements_from_structured(sc_elements)
            else:
                elements = _parse_elements_from_tree(tree) if tree else []

            # Surface 6：从本次截图刷新 snapshot-token 缓存。token 与特定的
            # cua-driver 截图绑定 —— 当一次新的截图到来时，之前截图的 token
            # 已陈旧，因此我们覆盖整个映射（并在新截图不携带 token 时完全
            # 清空它）。
            self._snapshot_tokens = {
                e.index: e.element_token
                for e in elements
                if e.element_token
            }

            # 图像可能作为 MCP image part 到达，也可能在 structuredContent
            # （screenshot_png_b64）内部，取决于驱动构建 ——
            # _image_from_tool_result 处理两种情况。
            png_b64, image_mime_type = _image_from_tool_result(gws_out)

            # 从 AX 树的第一行 AXWindow 提取窗口标题。
            wt = re.search(r'AXWindow\s+"([^"]+)"', tree)
            if wt:
                window_title = wt.group(1)

        png_bytes_len = 0
        if png_b64:
            try:
                raw = base64.b64decode(png_b64, validate=False)
                png_bytes_len = len(raw)
                detected_width, detected_height = _image_dimensions_from_bytes(raw)
                if detected_width and detected_height:
                    width = detected_width
                    height = detected_height
            except Exception:
                png_bytes_len = len(png_b64) * 3 // 4

        return CaptureResult(
            mode=mode,
            width=width,
            height=height,
            png_b64=png_b64,
            elements=elements,
            app=app_name,
            window_title=window_title,
            png_bytes_len=png_bytes_len,
            image_mime_type=image_mime_type,
        )

    # ── Pointer ────────────────────────────────────────────────────
    def click(
        self,
        *,
        element: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
        click_count: int = 1,
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult:
        pid = self._active_pid
        if pid is None:
            return ActionResult(ok=False, action="click",
                                message="No active window — call capture() first.")

        # 仅按 click_count 选择工具 —— 单击还是双击 —— 并把 button 透传
        # 给 `click` 的 `button` 枚举（NousResearch/hermes-agent#47072 的
        # Surface 5）。cua-driver-rs 在 trycua/cua#1961 中为 `click` 增加了
        # 显式的 `button: "left"|"right"|"middle"` 参数，它会拒绝未知按钮；
        # 在此之前，`middle` 通过名称路由经 `right_click` 被无声地映射为
        # 左键单击。`right_click`/`middle_click` 这两个 MCP 工具是已废弃的
        # 别名 —— 保留着，但这里不再调用它们。
        button_norm = (button or "left").lower()
        if button_norm not in {"left", "right", "middle"}:
            return ActionResult(ok=False, action="click",
                                message=f"unknown button {button!r} — expected left, right, middle.")
        tool = "double_click" if click_count == 2 else "click"

        args: Dict[str, Any] = {"pid": pid, "button": button_norm}
        if element is not None:
            if self._active_window_id is None:
                return ActionResult(ok=False, action=tool,
                                    message="No active window_id for element_index click.")
            args["element_index"] = element
            args["window_id"] = self._active_window_id
        elif x is not None and y is not None:
            args["x"] = x
            args["y"] = y
        else:
            return ActionResult(ok=False, action=tool,
                                message="click requires element= or x/y.")
        if modifiers:
            args["modifier"] = modifiers

        return self._action(tool, args)

    def drag(
        self,
        *,
        from_element: Optional[int] = None,
        to_element: Optional[int] = None,
        from_xy: Optional[Tuple[int, int]] = None,
        to_xy: Optional[Tuple[int, int]] = None,
        button: str = "left",
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult:
        pid = self._active_pid
        if pid is None:
            return ActionResult(ok=False, action="drag",
                                message="No active window — call capture() first.")
        args: Dict[str, Any] = {"pid": pid}
        if from_element is not None and to_element is not None:
            if self._active_window_id is None:
                return ActionResult(ok=False, action="drag",
                                    message="No active window_id for element-based drag.")
            args["from_element"] = from_element
            args["to_element"] = to_element
            args["window_id"] = self._active_window_id
        elif from_xy is not None and to_xy is not None:
            args["from_x"], args["from_y"] = int(from_xy[0]), int(from_xy[1])
            args["to_x"], args["to_y"] = int(to_xy[0]), int(to_xy[1])
        else:
            return ActionResult(ok=False, action="drag",
                                message="drag requires from_element/to_element or from_coordinate/to_coordinate.")
        return self._action("drag", args)

    def scroll(
        self,
        *,
        direction: str,
        amount: int = 3,
        element: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult:
        pid = self._active_pid
        if pid is None:
            return ActionResult(ok=False, action="scroll",
                                message="No active window — call capture() first.")
        args: Dict[str, Any] = {
            "pid": pid,
            "direction": direction,
            "amount": max(1, min(50, amount)),
        }
        if element is not None and self._active_window_id is not None:
            args["element_index"] = element
            args["window_id"] = self._active_window_id
        elif x is not None and y is not None:
            args["x"] = x
            args["y"] = y
        return self._action("scroll", args)

    # ── Keyboard ───────────────────────────────────────────────────
    def type_text(self, text: str) -> ActionResult:
        pid = self._active_pid
        if pid is None:
            return ActionResult(ok=False, action="type_text",
                                message="No active window — call capture() first.")
        return self._action("type_text", {"pid": pid, "text": text})

    def key(self, keys: str) -> ActionResult:
        pid = self._active_pid
        if pid is None:
            return ActionResult(ok=False, action="key",
                                message="No active window — call capture() first.")

        key_name, modifiers = _parse_key_combo(keys)
        if not key_name:
            return ActionResult(ok=False, action="key",
                                message=f"Could not parse key from '{keys}'.")

        if modifiers:
            # hotkey 至少需要一个修饰键 + 一个普通键。
            return self._action("hotkey", {"pid": pid, "keys": modifiers + [key_name]})
        else:
            return self._action("press_key", {"pid": pid, "key": key_name})

    # ── 值设置器 ────────────────────────────────────────────────────
    def set_value(self, value: str, element: Optional[int] = None) -> ActionResult:
        """为元素设置一个值。原生处理 AXPopUpButton 的选择。"""
        pid = self._active_pid
        window_id = self._active_window_id
        if pid is None or window_id is None:
            return ActionResult(ok=False, action="set_value",
                                message="No active window — call capture() first.")
        if element is None:
            return ActionResult(ok=False, action="set_value",
                                message="set_value requires element= (element index).")
        args: Dict[str, Any] = {
            "pid": pid,
            "window_id": window_id,
            "element_index": element,
            "value": value,
        }
        return self._action("set_value", args)

    # ── 自省 ────────────────────────────────────────────────────────
    def list_apps(self) -> List[Dict[str, Any]]:
        out = self._session.call_tool("list_apps", {"session": self._session_id})
        data = out["data"]
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("apps", [])
        # list_apps 返回纯文本 —— 解析应用行。
        if isinstance(data, str):
            apps = []
            for line in data.splitlines():
                m = re.search(r'(.+?)\s+\(pid\s+(\d+)\)', line)
                if m:
                    apps.append({"name": m.group(1).strip(), "pid": int(m.group(2))})
            return apps
        return []

    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        """为后续动作定位一个应用，而不抢占系统焦点。

        cua-driver 的后台自动化永远不需要把窗口提到最前：capture(app=...)
        已经通过 list_windows 选中了正确的窗口。我们把 focus_app 实现为
        一个纯粹的窗口选择器 —— 枚举屏幕上的窗口，为 *app* 找到最佳匹配，
        并存储其 pid/window_id，这样后续的 click/type 调用就能命中正确的
        进程。

        raise_window=True 被有意忽略：抢占用户焦点正是这个后端设计上要
        避免的。
        """
        lw_out = self._session.call_tool(
            "list_windows",
            {"on_screen_only": True, "session": self._session_id},
        )
        raw_windows = (lw_out.get("structuredContent") or {}).get("windows") or []
        windows = [
            {
                "app_name": w.get("app_name", ""),
                "pid": int(w["pid"]),
                "window_id": int(w["window_id"]),
                "z_index": w.get("z_index", 0),
            }
            for w in raw_windows
        ]
        windows.sort(key=lambda w: w["z_index"])

        app_lower = app.lower()
        matched = [w for w in windows if app_lower in w["app_name"].lower()]
        # 当过滤器什么都没匹配到时，不要无声地回退到最前面的窗口 ——
        # 那会掩盖真正的失败（通常是 macOS 应用名的本地化不匹配，例如
        # 调用方传入 "Calculator"，但 list_windows 返回 "計算機"）。
        target = matched[0] if matched else None
        if target:
            self._active_pid = target["pid"]
            self._active_window_id = target["window_id"]
            self._last_app = target["app_name"]  # 为 capture_after= 后续动作保留
            return ActionResult(
                ok=True, action="focus_app",
                message=f"Targeted {target['app_name']} (pid {self._active_pid}, "
                        f"window {self._active_window_id}) without raising window.",
            )
        return ActionResult(ok=False, action="focus_app",
                            message=f"No on-screen window found for app '{app}'.")

    # ── 应用生命周期 ──────────────────────────────────────────────────
    #
    # cua-driver 把 launch_app / kill_app / bring_to_front 作为一套完整的
    # 集合暴露出来。上面的 focus_app() 是一个*窗口选择器*（不改变进程状态）；
    # 这些方法驱动的是进程层。

    def launch_app(
        self,
        *,
        bundle_id: Optional[str] = None,
        name: Optional[str] = None,
        urls: Optional[List[str]] = None,
        additional_arguments: Optional[List[str]] = None,
        creates_new_application_instance: bool = False,
    ) -> Dict[str, Any]:
        """幂等启动。返回 ``{pid, bundle_id, name, windows[]}``，这样调用方
        就能在 ``get_window_state`` 之前省掉一次额外的 ``list_windows``
        往返。

        ``creates_new_application_instance=True`` 会强制启动一个新实例，即使
        应用已经在运行 —— 当并发运行可能触碰同一个应用时使用它，这样每个
        会话都能得到自己隔离的窗口。"""
        if not bundle_id and not name:
            raise ValueError("launch_app requires either bundle_id or name")
        args: Dict[str, Any] = {"session": self._session_id}
        if bundle_id:
            args["bundle_id"] = bundle_id
        if name:
            args["name"] = name
        if urls:
            args["urls"] = list(urls)
        if additional_arguments:
            args["additional_arguments"] = list(additional_arguments)
        if creates_new_application_instance:
            args["creates_new_application_instance"] = True
        out = self._session.call_tool("launch_app", args)
        return out["structuredContent"] or {"data": out["data"]}

    def kill_app(self, *, pid: int) -> ActionResult:
        """按 pid 终止。等价于 POSIX 上的 ``kill -9``、Windows 上的
        ``taskkill /F``。"""
        return self._action("kill_app", {"pid": int(pid)})

    def bring_to_front(self, *, pid: int,
                       window_id: Optional[int] = None) -> ActionResult:
        """激活一个窗口，使后续前台派发的输入落到它上面。cua-driver 的
        docstring 指出，这比每次调用都做 SetForegroundWindow 闪烁更廉价。"""
        args: Dict[str, Any] = {"pid": int(pid)}
        if window_id is not None:
            args["window_id"] = int(window_id)
        return self._action("bring_to_front", args)

    # ── 指针 + 显示器自省 ───────────────────────────────────────────

    def move_cursor(self, x: int, y: int) -> ActionResult:
        """把 agent 光标*覆盖层*移动到屏幕上的某点。这是一个视觉提示 ——
        它不会移动真实的 OS 指针（cua-driver 明确避免抢占指针焦点）。覆盖层
        会平滑地滑向目标，因此消费者在点击之前用它来给出一个可见的
        "agent 即将前往何处"的提示。"""
        return self._action("move_cursor", {"x": int(x), "y": int(y)})

    def get_cursor_position(self) -> Tuple[int, int]:
        """返回*真实的* OS 光标位置，以屏幕点为单位（原点在左上角）。"""
        out = self._session.call_tool(
            "get_cursor_position", {"session": self._session_id}
        )
        sc = out.get("structuredContent") or {}
        return int(sc.get("x", 0)), int(sc.get("y", 0))

    def get_screen_size(self) -> Dict[str, Any]:
        """返回主显示器的逻辑尺寸（以点为单位）及其 backing scale factor。
        形状：``{width, height, backing_scale_factor}``。"""
        out = self._session.call_tool(
            "get_screen_size", {"session": self._session_id}
        )
        return out.get("structuredContent") or {}

    def zoom(self, *, window_id: int, x: float, y: float, w: float, h: float,
             factor: float = 1.0, format: str = "jpeg",
             quality: int = 85) -> Dict[str, Any]:
        """返回某个窗口子区域的 JPEG / PNG，可选择缩放。cua-driver 支持
        zoom-to-rect，供需要对特定元素获得更高分辨率视图的调用方使用。"""
        return self._session.call_tool("zoom", {
            "window_id": int(window_id),
            "x": float(x), "y": float(y), "w": float(w), "h": float(h),
            "factor": float(factor),
            "format": format, "quality": int(quality),
            "session": self._session_id,
        })

    # ── Agent 光标（覆盖层） ────────────────────────────────────────
    #
    # 会话（start_session/end_session，在 start/stop 中接入）拥有该光标。
    # 这些旋钮按会话调整它的外观 + 行为。它们都接受一个可选的 `cursor_id`，
    # 以便在某次运行驱动多个光标时（罕见）定位特定光标；默认是本次运行的
    # 会话 id。

    def set_agent_cursor_enabled(self, enabled: bool, *,
                                 cursor_id: Optional[str] = None) -> ActionResult:
        """为本次运行切换 agent 光标覆盖层的可见性。"""
        args: Dict[str, Any] = {"enabled": bool(enabled)}
        if cursor_id:
            args["cursor_id"] = cursor_id
        return self._action("set_agent_cursor_enabled", args)

    def set_agent_cursor_motion(self, *,
                                glide_ms: Optional[float] = None,
                                dwell_ms: Optional[float] = None,
                                idle_hide_ms: Optional[float] = None,
                                cursor_id: Optional[str] = None) -> ActionResult:
        """调整覆盖层的运动时序 —— 滑行时长、点击后停留、空闲隐藏延迟。
        每个 None 都表示"保持当前值"。"""
        args: Dict[str, Any] = {}
        if glide_ms is not None:
            args["glide_ms"] = float(glide_ms)
        if dwell_ms is not None:
            args["dwell_ms"] = float(dwell_ms)
        if idle_hide_ms is not None:
            args["idle_hide_ms"] = float(idle_hide_ms)
        if cursor_id:
            args["cursor_id"] = cursor_id
        return self._action("set_agent_cursor_motion", args)

    def set_agent_cursor_style(self, *,
                               gradient_colors: Optional[List[str]] = None,
                               bloom_color: Optional[str] = None,
                               image_path: Optional[str] = None,
                               cursor_id: Optional[str] = None) -> ActionResult:
        """自定义光标主体。``gradient_colors`` 是 CSS 十六进制字符串，从
        尖端到尾端；``bloom_color`` 是径向光晕；``image_path``
        （.svg/.png/.ico）会完全替换轮廓剪影。空值会恢复为调色板默认值。"""
        args: Dict[str, Any] = {}
        if gradient_colors is not None:
            args["gradient_colors"] = list(gradient_colors)
        if bloom_color is not None:
            args["bloom_color"] = bloom_color
        if image_path is not None:
            args["image_path"] = image_path
        if cursor_id:
            args["cursor_id"] = cursor_id
        return self._action("set_agent_cursor_style", args)

    def get_agent_cursor_state(self, *,
                               cursor_id: Optional[str] = None) -> Dict[str, Any]:
        """返回本次运行光标（或具名 ``cursor_id``）的
        ``{x, y, config: {cursor_color, cursor_icon, ...}, enabled}``。"""
        args: Dict[str, Any] = {"session": self._session_id}
        if cursor_id:
            args["cursor_id"] = cursor_id
        out = self._session.call_tool("get_agent_cursor_state", args)
        return out.get("structuredContent") or {}

    # ── 录制 / 回放 ──────────────────────────────────────────────────

    def start_recording(self, *, output_dir: str,
                        record_video: bool = False) -> Dict[str, Any]:
        """启用轨迹录制（每轮截图 + 动作 JSON）到 ``output_dir``。
        ``record_video=True`` 还会把主显示器录制到
        ``<output_dir>/recording.mp4``（H.264）。录制归属以本次运行的
        会话 id 为键，这样并发运行不会争抢录制器。"""
        out = self._session.call_tool("start_recording", {
            "output_dir": output_dir,
            "record_video": bool(record_video),
            "session": self._session_id,
        })
        return out.get("structuredContent") or {}

    def stop_recording(self) -> Dict[str, Any]:
        """禁用录制并最终化 mp4（如果开启了视频）。返回录制器的最终状态，
        包含 ``last_video_path``。"""
        out = self._session.call_tool("stop_recording", {
            "session": self._session_id,
        })
        return out.get("structuredContent") or {}

    def get_recording_state(self) -> Dict[str, Any]:
        """返回当前录制器状态，但不改变它。
        形状：``{recording, enabled, output_dir, next_turn,
        last_video_path, last_error, owner, video_active}``。"""
        out = self._session.call_tool(
            "get_recording_state", {"session": self._session_id}
        )
        return out.get("structuredContent") or {}

    def replay_trajectory(self, *, trajectory_dir: str,
                          dry_run: bool = False,
                          speed_factor: float = 1.0) -> Dict[str, Any]:
        """通过按词法顺序重新调用每轮的工具调用来回放先前录制的轮次流。
        ``dry_run=True`` 只记录日志，并不真正触发工具。"""
        return self._session.call_tool("replay_trajectory", {
            "trajectory_dir": trajectory_dir,
            "dry_run": bool(dry_run),
            "speed_factor": float(speed_factor),
            "session": self._session_id,
        })

    def install_ffmpeg(self) -> Dict[str, Any]:
        """在 Linux / Windows 上为 ``start_recording(record_video=True)``
        引导安装 ffmpeg。macOS 通过 ScreenCaptureKit 原生录制，不需要 ffmpeg。"""
        return self._session.call_tool(
            "install_ffmpeg", {"session": self._session_id}
        )

    # ── 配置 ────────────────────────────────────────────────────────

    def get_config(self) -> Dict[str, Any]:
        """返回当前 cua-driver 运行时配置。"""
        out = self._session.call_tool(
            "get_config", {"session": self._session_id}
        )
        return out.get("structuredContent") or {}

    def set_config(self, **config) -> ActionResult:
        """设置 cua-driver 的配置键。常见的键包括
        ``max_image_dimension``（图像输出缩放）、录制标志等。未知键会原样
        透传 —— cua-driver 会根据其自身的 schema 做校验。"""
        return self._action("set_config", dict(config))

    # ── 更底层的自省 ─────────────────────────────────────────────────

    def get_accessibility_tree(self) -> Dict[str, Any]:
        """返回一个轻量的快照：正在运行的常规应用 + 屏幕上可见的窗口，
        带有 bounds、z-order、归属 pid。大致是 ``list_windows`` 在一次
        调用中暴露的数据。大多数调用方应优先使用 ``capture()`` /
        ``focus_app()``，它们内部已经在使用这种形态。"""
        out = self._session.call_tool(
            "get_accessibility_tree", {"session": self._session_id}
        )
        return out.get("structuredContent") or {"data": out["data"]}

    # ── 浏览器页面工具 ──────────────────────────────────────────────

    def page(self, *, pid: int, action: str,
             **page_args: Any) -> Dict[str, Any]:
        """与运行中应用（Chrome、Safari、Edge 等）里加载的浏览器页面交互。
        cua-driver 根据目标通过 CDP / Apple Events / AX 树来路由。
        ``action`` + ``page_args`` 的形态取决于所请求的操作（例如
        ``action="eval"`` 接受 ``js: str``）；完整语法见 cua-driver 的
        ``page`` 工具描述。"""
        args: Dict[str, Any] = {
            "pid": int(pid),
            "action": action,
            "session": self._session_id,
        }
        args.update(page_args)
        return self._session.call_tool("page", args)

    # ── 通用逃生舱 ──────────────────────────────────────────────────

    def call_tool(self, name: str, args: Optional[Dict[str, Any]] = None,
                  *, timeout: float = 30.0) -> Dict[str, Any]:
        """按名称调用任意 cua-driver MCP 工具并传入任意参数。
        会注入 ``session``（通过 setdefault 保留调用方显式传入的值）。
        对于封装器尚未做类型包装的工具，这是受支持的逃生舱 —— 优于
        直接使用 ``self._session.call_tool``，因为它让 session-id 契约与
        其他一切保持一致。"""
        payload = dict(args) if args else {}
        payload.setdefault("session", self._session_id)
        return self._session.call_tool(name, payload, timeout=timeout)

    # ── 内部 ────────────────────────────────────────────────────────
    def _maybe_attach_element_token(self, tool: str, args: Dict[str, Any]) -> None:
        """Surface 6：当封装器即将调用一个支持 token 的工具并带有
        `element_index` 时，从上次截图中查找匹配的 `element_token` 并附上。
        cua-driver-rs 对组合参数的契约记录在 trycua/cua#1961：

          "element_token takes precedence over element_index when both
           supplied. Returns an explicit 'stale' error if the snapshot
           has been superseded."

        以每个工具的能力声明为门控，这样我们就不会把该字段发给早于该
        surface 的驱动（那些驱动会用 `additionalProperties: false` 拒绝
        该 schema）。
        """
        idx = args.get("element_index")
        if not isinstance(idx, int):
            return
        token = self._snapshot_tokens.get(idx)
        if not token:
            return
        if not self._session.supports_capability(
            "accessibility.element_tokens", tool=tool
        ):
            return
        args["element_token"] = token

    def _action(self, name: str, args: Dict[str, Any]) -> ActionResult:
        # 只要调用带有 element_index 且目标工具宣告支持，就附上截图的
        # element_token。
        self._maybe_attach_element_token(name, args)
        # 带上本次运行的 session id，这样 cua-driver 的 agent 光标和
        # 每会话状态（配置覆盖、录制归属）就与本次运行保持绑定。
        # setdefault 保留调用方已显式传入的任何 session。
        args.setdefault("session", self._session_id)
        try:
            out = self._session.call_tool(name, args)
        except Exception as e:
            logger.exception("cua-driver %s call failed", name)
            return ActionResult(ok=False, action=name, message=f"cua-driver error: {e}")
        ok = not out["isError"]
        message = ""
        data = out["data"]
        if isinstance(data, dict):
            message = str(data.get("message", ""))
        elif isinstance(data, str):
            message = data
        return ActionResult(ok=ok, action=name, message=message,
                            meta=data if isinstance(data, dict) else {})
