"""Windows subprocess 兼容性辅助工具。

Hermes 在 Linux / macOS 上开发，也在 Windows 上原生测试。
若干常见的 subprocess 使用方式在 Windows 上会静默失败或报错：

* ``["npm", "install", ...]`` — 在 Windows 上 ``npm`` 是 ``npm.cmd``，
  一个批处理包装脚本。``subprocess.Popen(["npm", ...])`` 会以 WinError 193
  （"不是有效的 Win32 应用程序"）失败，因为 CreateProcessW 无法直接运行
  ``.cmd`` 文件，除非使用 ``shell=True`` 或通过 PATHEXT 解析。

* ``start_new_session=True`` — 在 POSIX 上，这会映射到 ``os.setsid()``，
  真正地分离子进程。在 Windows 上该参数被静默忽略；Windows 等价方式是
  ``CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`` creationflags，
  只有显式传递时 Python 才会应用。

* 控制台窗口闪现 — 在 Windows 上每次 ``subprocess.Popen`` 一个 ``.exe``
  都会短暂弹出 cmd 窗口，除非传入 ``CREATE_NO_WINDOW``。
  对于后台守护进程而言，这虽属视觉问题，但相当干扰。

本模块集中了平台分支逻辑，避免在整个代码库中到处散布
``if sys.platform == "win32":``。

**所有辅助函数在非 Windows 系统上均为空操作** — 在 Linux/macOS 代码路径
中调用它们是安全的，这是"在 POSIX 上无副作用"的保证。
"""

from __future__ import annotations

import shutil
import sys
from typing import Sequence

__all__ = [
    "IS_WINDOWS",
    "resolve_node_command",
    "windows_detach_flags",
    "windows_detach_flags_without_breakaway",
    "windows_hide_flags",
    "windows_detach_popen_kwargs",
]


IS_WINDOWS = sys.platform == "win32"


# -----------------------------------------------------------------------------
# Node 生态系统启动器解析
# -----------------------------------------------------------------------------


def resolve_node_command(name: str, argv: Sequence[str]) -> list[str]:
    """将 Node 生态系统命令名解析为绝对路径的 argv。

    在 Windows 上，``npm``、``npx``、``yarn``、``pnpm``、
    ``playwright``、``prettier`` 等命令以 ``.cmd`` 文件（批处理包装脚本）形式存在。
    ``subprocess.Popen(["npm", "install"])`` 会因 WinError 193 失败，
    因为 CreateProcessW 无法直接执行批处理文件。

    ``shutil.which(name)`` 会通过 PATHEXT 解析 ``.cmd`` 并返回完整路径，
    CreateProcessW 能接受该路径，因为扩展名会告知 Windows 通过 ``cmd.exe /c`` 路由。

    在 POSIX 上，``shutil.which`` 找到命令时也会返回完整路径。
    这与裸名解析（由 OS 自行搜索 PATH）略有不同，但功能上完全等价，
    并且使 argv 在日志中可复现。

    命令不在 PATH 上时的行为：
    - Windows：返回裸名——调用方仍可以 ``shell=True`` 作为最后手段重试，
      或者后续 Popen 会抛出带有可读错误信息的 FileNotFoundError。
    - POSIX：相同。在没有安装 npm 的 Linux 上，裸 ``npm`` 的失败方式
      与此函数存在之前相同。

    参数：
        name: 要解析的命令名（``npm``、``npx``、``node`` 等）。
        argv: 其余参数。不得包含 ``name`` 本身——此函数会构建完整的 argv 列表。

    返回：
        适合传递给 subprocess.Popen/run/call 的列表。
    """
    resolved = shutil.which(name)
    if resolved:
        return [resolved, *argv]
    return [name, *argv]


# -----------------------------------------------------------------------------
# 分离式 / 隐藏式进程创建
# -----------------------------------------------------------------------------


# Win32 CreationFlags——在此处定义而非从 subprocess 导入，
# 因为 CREATE_NO_WINDOW 和 DETACHED_PROCESS 在旧版 Python 或非 Windows 构建中
# 不保证出现在标准库 subprocess 中。
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000
# 脱离父进程所属的任何 Win32 作业对象。若不设置此标志，
# 分离的子进程仍会继承父进程的作业对象成员资格，
# 当父进程（Electron、Tauri、桌面 GUI 的引导安装程序）退出时，
# OS 会销毁整个作业——连同"分离的"子进程一起。
# 这对更新后的 gateway 守护进程至关重要：
# Electron 在自己的作业中启动 Tauri 更新程序，更新程序再启动守护子进程；
# 若没有 BREAKAWAY，Electron 退出的瞬间守护进程就会死亡，
# 导致 `hermes update` 从 GUI 触发后 gateway 无法重新启动。
# 参见 fix/windows-gateway-reliability。
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def windows_detach_flags() -> int:
    """返回将子进程从父控制台和进程组分离的 Win32 creationflags。非 Windows 上返回 0。

    调用 subprocess.Popen 时搭配 ``start_new_session=False``（默认值）使用——
    在 POSIX 上请改用 ``start_new_session=True``，该参数在子进程中映射到 ``os.setsid()``。

    各标志说明：
    - ``CREATE_NEW_PROCESS_GROUP``——子进程拥有独立进程组，
      父控制台的 Ctrl+C 不会传播到子进程。
    - ``DETACHED_PROCESS``——子进程完全没有控制台。
      后台守护进程（gateway 守护进程、更新重启程序）必须设置此标志，
      否则关闭控制台会杀死子进程。
    - ``CREATE_NO_WINDOW``——抑制启动控制台应用时短暂出现的 cmd 闪窗。
      与 DETACHED_PROCESS 重复，但明确列出以提高可读性。
    - ``CREATE_BREAKAWAY_FROM_JOB``——脱离父进程所在的任何作业对象。
      Electron（桌面应用）和 Tauri（引导安装程序）会将子进程包裹在作业对象中；
      若不设置此标志，即使用 DETACHED_PROCESS 启动，父进程退出时子进程也会死亡。
      正是由于缺少此标志，导致更新后的 gateway 重启守护进程在
      Electron 桌面更新流程结束、Tauri 更新程序退出时静默死亡。

    若进程所在的作业对象不允许脱离（罕见情况——未设置 JOB_OBJECT_LIMIT_BREAKAWAY_OK），
    CreateProcess 会返回 ERROR_ACCESS_DENIED，Python 会在 ``subprocess.Popen`` 调用时
    将其转化为 ``PermissionError``。本代码库中的调用方已将分离式启动包裹在
    ``try/except OSError`` 中并回退到 cmd.exe 包装器，
    因此脱离被拒绝的情况会优雅降级而非崩溃。
    """
    if not IS_WINDOWS:
        return 0
    return (
        _CREATE_NEW_PROCESS_GROUP
        | _DETACHED_PROCESS
        | _CREATE_NO_WINDOW
        | _CREATE_BREAKAWAY_FROM_JOB
    )


def windows_detach_flags_without_breakaway() -> int:
    """与 :func:`windows_detach_flags` 相同，但去掉了 ``CREATE_BREAKAWAY_FROM_JOB``。

    :func:`windows_detach_flags` 的文档字符串中提到，若进程所在的作业对象
    不允许脱离（未设置 ``JOB_OBJECT_LIMIT_BREAKAWAY_OK``），
    CreateProcess 会返回 ``ERROR_ACCESS_DENIED``，在 ``subprocess.Popen`` 调用时
    表现为 ``OSError``（``PermissionError``）。
    想要恢复——即去掉脱离位重试——的调用方可以将两个辅助函数配对使用，
    而无需在每处都写 ``& ~0x01000000`` 这样的魔法数字：

    .. code-block:: python

        try:
            subprocess.Popen(argv, creationflags=windows_detach_flags(), …)
        except OSError:
            subprocess.Popen(
                argv,
                creationflags=windows_detach_flags_without_breakaway(),
                …,
            )

    该模式的规范实现参见 ``gateway_windows.py::_spawn_detached``。
    非 Windows 上返回 0。
    """
    if not IS_WINDOWS:
        return 0
    return _CREATE_NEW_PROCESS_GROUP | _DETACHED_PROCESS | _CREATE_NO_WINDOW


def windows_hide_flags() -> int:
    """返回仅隐藏子进程控制台窗口、但不分离子进程的 Win32 creationflags。非 Windows 上返回 0。

    适用于作为较大操作一部分启动的短生命周期控制台应用
    （``taskkill``、``where``、版本探测等），
    这类场景需要无闪窗，同时需要同步收集 stdout 和退出码。

    与 :func:`windows_detach_flags` 的核心区别：不含 ``DETACHED_PROCESS``——
    子进程仍然继承 stdio 句柄，因此 ``capture_output=True`` 可正常工作。
    若设置 ``DETACHED_PROCESS`` 会切断 stdio，导致 stdout 捕获失败。
    """
    if not IS_WINDOWS:
        return 0
    return _CREATE_NO_WINDOW


def windows_detach_popen_kwargs() -> dict:
    """Return a dict of Popen kwargs that detach a child on Windows and
    fall back to the POSIX equivalent (``start_new_session=True``) on
    Linux/macOS.

    Usage pattern:

    .. code-block:: python

        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            **windows_detach_popen_kwargs(),
        )

    This replaces the unsafe-on-Windows pattern:

    .. code-block:: python

        subprocess.Popen(..., start_new_session=True)

    which silently fails to detach on Windows (the flag is accepted but
    has no effect — the child stays attached to the parent's console
    and dies when the console closes).
    """
    if IS_WINDOWS:
        return {"creationflags": windows_detach_flags()}
    return {"start_new_session": True}
