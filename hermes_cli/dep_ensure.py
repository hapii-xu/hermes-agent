"""非 Python 运行时依赖的惰性引导程序。

检测和提示逻辑放在 Python 中而不是 install.sh 中，原因如下：
  1. shutil.which() 在所有平台上都能工作；install.sh 需要 bash。
  2. 检测是瞬时的；为了检查 "node 是否安装" 而启动 bash 是浪费。
  3. Python 控制用户体验（丰富的提示、非交互式回退、TTY 检测）。

install.sh 仍然是 *安装* 后端，因为它有 1900 行经过实战检验的
操作系统检测和包管理器逻辑（apt/brew/pacman/dnf/
zypper/Termux/…）。在 Python 中重新实现这些将是巨大的重复。

可优雅降级的依赖（ripgrep → grep 回退，ffmpeg → 跳过转换）
不需要接入 ensure_dependency —— 只有硬性依赖需要（TUI 需要 node，
浏览器工具需要 agent-browser）。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from hermes_constants import agent_browser_runnable

_IS_WINDOWS = platform.system() == "Windows"

_DEP_CHECKS = {
    "node": lambda: shutil.which("node") is not None,
    "browser": lambda: (
        agent_browser_runnable(shutil.which("agent-browser"))
        or _has_system_browser()
        or _has_hermes_agent_browser()
    ),
    "ripgrep": lambda: shutil.which("rg") is not None,
    "ffmpeg": lambda: shutil.which("ffmpeg") is not None,
}

_DEP_DESCRIPTIONS = {
    "node": "Node.js (required for browser tools and TUI)",
    "browser": "Browser engine (Chromium, for web browsing tools)",
    "ripgrep": "ripgrep (fast file search)",
    "ffmpeg": "ffmpeg (TTS voice messages)",
}


def _has_system_browser() -> bool:
    if _IS_WINDOWS:
        names = ("chrome", "msedge", "chromium")
    else:
        names = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
    for name in names:
        if shutil.which(name):
            return True
    return False


def _has_hermes_agent_browser() -> bool:
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    if _IS_WINDOWS:
        # npm -g --prefix 在 Windows 上直接将 .cmd 启动脚本放在 prefix 目录中
        return (home / "node" / "agent-browser.cmd").is_file()
    # install.sh 通过 npm -g --prefix 全局安装到 $HERMES_HOME/node/bin/
    # 同时检查旧版 node_modules/.bin/ 路径（适用于 git-clone 安装）。
    return (
        (home / "node" / "bin" / "agent-browser").is_file()
        or (home / "node_modules" / ".bin" / "agent-browser").is_file()
    )


def _find_install_script(
    package_dir: Path | None = None,
    repo_root: Path | None = None,
) -> tuple[Path | None, str | None]:
    """定位安装脚本 —— 打包在 wheel 中或在 git 仓库中。

    在 Windows 上，优先使用 install.ps1；在 POSIX 上，优先使用 install.sh。
    返回 (path, shell) 元组，如果都未找到则返回 (None, None)。
    """
    if package_dir is None:
        package_dir = Path(__file__).parent
    if repo_root is None:
        repo_root = package_dir.parent

    if _IS_WINDOWS:
        preferred = ("install.ps1", "powershell")
        fallback = ("install.sh", "bash")
    else:
        preferred = ("install.sh", "bash")
        fallback = ("install.ps1", "powershell")

    for script_name, shell in (preferred, fallback):
        bundled = package_dir / "scripts" / script_name
        if bundled.is_file():
            return bundled, shell
        repo = repo_root / "scripts" / script_name
        if repo.is_file():
            return repo, shell

    return None, None


def ensure_dependency(
    dep: str,
    interactive: bool = True,
) -> bool:
    """确保非 Python 依赖可用。如果可用返回 True。"""
    check = _DEP_CHECKS.get(dep)
    if check is None:
        # 未知依赖 —— 不要静默转发给安装脚本。
        return False
    if check():
        return True

    script, shell = _find_install_script()
    if script is None:
        if interactive:
            desc = _DEP_DESCRIPTIONS.get(dep, dep)
            print(f"  {desc} is not installed and no install script was found.")
            print(f"  Install {dep} manually and try again.")
        return False

    if interactive and sys.stdin.isatty():
        desc = _DEP_DESCRIPTIONS.get(dep, dep)
        try:
            reply = input(f"{desc} is not installed. Install now? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if reply not in ("", "y", "yes"):
            return False

    if shell == "powershell":
        from hermes_constants import get_hermes_home
        ps_bin = shutil.which("powershell") or shutil.which("pwsh")
        if not ps_bin:
            if interactive:
                print("  PowerShell not found. Install PowerShell or run install.ps1 manually.")
            return False
        cmd = [
            ps_bin,
            "-ExecutionPolicy", "Bypass",
            "-File", str(script),
            "-Ensure", dep,
            "-HermesHome", str(get_hermes_home()),
        ]
    else:
        cmd = ["bash", str(script), "--ensure", dep]

    run_env = {**os.environ, "IS_INTERACTIVE": "false"}
    result = subprocess.run(
        cmd,
        env=run_env,
    )
    if result.returncode != 0:
        return False

    if check:
        return check()
    return True
