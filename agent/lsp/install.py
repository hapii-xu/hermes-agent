"""LSP 服务器二进制程序的自动安装。

尝试使用合适的包管理器安装缺失的服务器。所有安装都指向
Hermes 管理的 bin 暂存目录 ``<HERMES_HOME>/lsp/bin/``，
以免污染用户的全局工具链。

安装策略：

- ``auto`` — 尝试使用最佳的可用包管理器进行安装。这是默认策略。
- ``manual`` — 永不自动安装；如果二进制程序缺失，则静默跳过该服务器，
  并通过 ``hermes lsp status`` 告知用户。
- ``off`` — 目前与 ``manual`` 相同（保留区分以便将来演化行为，
  例如不同的日志记录方式）。

实际安装会在首次需要某个服务器时同步执行，并且对同一包的
并发 :func:`try_install` 调用会通过每包级别的锁进行去重。

失败模式是非致命的：每个安装路径都用 try/except 包装，
失败时返回 ``None``。然后工具层会回退到进程内的语法检查器，
与用户根本没有启用 LSP 时的行为完全一致。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("agent.lsp.install")

# 包名 → 安装策略提示注册表。每个条目是一个
# 策略名称 + 包名 + 可执行文件名 的元组。安装完成后，
# 我们首先在 ``<HERMES_HOME>/lsp/bin/`` 中查找可执行文件，
# 然后在 PATH 中查找。
#
# 可选字段：
#   - ``extra_pkgs``：需要与 ``pkg`` 一起安装在同一 node_modules
#     树中的兄弟包列表。当 LSP 服务器有 npm 不会自动拉取的
#     运行时对等依赖时使用（例如 typescript-language-server
#     需要 ``typescript``）。
INSTALL_RECIPES: Dict[str, Dict[str, Any]] = {
    # Python
    "pyright": {"strategy": "npm", "pkg": "pyright", "bin": "pyright-langserver"},
    # JS/TS 系列
    "typescript-language-server": {
        "strategy": "npm",
        "pkg": "typescript-language-server",
        "bin": "typescript-language-server",
        # typescript-language-server 要求 `typescript` SDK
        # (tsserver) 可以从同一 node_modules 树中导入；
        # 否则 initialize() 会失败并报错
        # "Could not find a valid TypeScript installation"。
        # 因此将它们一起安装。
        "extra_pkgs": ["typescript"],
    },
    "@vue/language-server": {
        "strategy": "npm",
        "pkg": "@vue/language-server",
        "bin": "vue-language-server",
    },
    "svelte-language-server": {
        "strategy": "npm",
        "pkg": "svelte-language-server",
        "bin": "svelteserver",
    },
    "@astrojs/language-server": {
        "strategy": "npm",
        "pkg": "@astrojs/language-server",
        "bin": "astro-ls",
    },
    "yaml-language-server": {
        "strategy": "npm",
        "pkg": "yaml-language-server",
        "bin": "yaml-language-server",
    },
    "bash-language-server": {
        "strategy": "npm",
        "pkg": "bash-language-server",
        "bin": "bash-language-server",
    },
    "intelephense": {"strategy": "npm", "pkg": "intelephense", "bin": "intelephense"},
    "dockerfile-language-server-nodejs": {
        "strategy": "npm",
        "pkg": "dockerfile-language-server-nodejs",
        "bin": "docker-langserver",
    },
    # Go
    "gopls": {"strategy": "go", "pkg": "golang.org/x/tools/gopls@latest", "bin": "gopls"},
    # Rust — 体积过大（引导程序需要数百 MB）。我们不会
    # 自动安装 rust-analyzer；用户需通过 rustup 安装。
    "rust-analyzer": {"strategy": "manual", "pkg": "", "bin": "rust-analyzer"},
    # C/C++ — 手动安装（clangd 随 LLVM 分发，体积很大）
    "clangd": {"strategy": "manual", "pkg": "", "bin": "clangd"},
    # Lua — 手动安装（LuaLS 是从 GitHub releases 获取的
    # 平台特定二进制程序；情况复杂，交由用户处理）
    "lua-language-server": {"strategy": "manual", "pkg": "", "bin": "lua-language-server"},
}


_install_locks: Dict[str, threading.Lock] = {}
_install_results: Dict[str, Optional[str]] = {}
_install_lock_meta = threading.Lock()
_WINDOWS_WRAPPER_SUFFIXES = (".cmd", ".exe", ".bat")


def _is_windows() -> bool:
    return os.name == "nt"


def hermes_lsp_bin_dir() -> Path:
    """返回 Hermes 管理的 LSP 服务器 bin 暂存目录。"""
    home = os.environ.get("HERMES_HOME")
    if home is None:
        home = os.path.join(os.path.expanduser("~"), ".hermes")
    p = Path(home) / "lsp" / "bin"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _native_binary_candidates(base: Path) -> list[Path]:
    """返回已暂存二进制程序的平台原生可执行文件候选列表。"""
    candidates = [base]
    if _is_windows():
        existing = {str(base).lower()}
        for suffix in _WINDOWS_WRAPPER_SUFFIXES:
            candidate = Path(str(base) + suffix)
            key = str(candidate).lower()
            if key not in existing:
                candidates.append(candidate)
                existing.add(key)
    return candidates


def _existing_binary(name: str) -> Optional[str]:
    """在暂存目录和 PATH 中探测名为 ``name`` 的二进制程序。"""
    for staged in _native_binary_candidates(hermes_lsp_bin_dir() / name):
        if staged.exists() and os.access(staged, os.X_OK):
            return str(staged)
    on_path = shutil.which(name)
    if on_path:
        return on_path
    if _is_windows():
        for suffix in _WINDOWS_WRAPPER_SUFFIXES:
            on_path = shutil.which(f"{name}{suffix}")
            if on_path:
                return on_path
    return None


def _get_lock(pkg: str) -> threading.Lock:
    with _install_lock_meta:
        lock = _install_locks.get(pkg)
        if lock is None:
            lock = threading.Lock()
            _install_locks[pkg] = lock
        return lock


def try_install(pkg: str, strategy: str = "auto") -> Optional[str]:
    """尝试安装 ``pkg``，成功时返回二进制程序路径。

    ``strategy`` 可以是 ``"auto"``、``"manual"`` 或 ``"off"``。
    在 ``manual``/``off`` 模式下，此函数仅探测已有的
    二进制程序，未找到时返回 ``None``。

    安装结果按包缓存——第二次调用会返回相同的路径
    （或 ``None``），不会重新安装。并发调用会被串行化。
    """
    if strategy not in {"auto",}:
        # 只有 ``auto`` 才会触发实际安装。在 manual/off 模式下，
        # 我们仍然检查二进制程序是否已存在。
        recipe = INSTALL_RECIPES.get(pkg, {})
        bin_name = recipe.get("bin", pkg)
        return _existing_binary(bin_name)

    if pkg in _install_results:
        return _install_results[pkg]

    lock = _get_lock(pkg)
    with lock:
        # 获取锁后进行双重检查。
        if pkg in _install_results:
            return _install_results[pkg]
        result = _do_install(pkg)
        _install_results[pkg] = result
        return result


def _do_install(pkg: str) -> Optional[str]:
    recipe = INSTALL_RECIPES.get(pkg)
    if recipe is None:
        # 不在我们的注册表中——尽力而为：仅探测 PATH。
        return shutil.which(pkg)

    strategy = recipe.get("strategy", "manual")
    bin_name = recipe.get("bin", pkg)

    # 检查是否已存在（shutil.which 或暂存目录）
    existing = _existing_binary(bin_name)
    if existing:
        return existing

    if strategy == "manual":
        logger.debug("[install] %s requires manual install (recipe=%s)", pkg, recipe)
        return None

    if strategy == "npm":
        return _install_npm(
            recipe.get("pkg", pkg),
            bin_name,
            extra_pkgs=recipe.get("extra_pkgs") or [],
        )
    if strategy == "go":
        return _install_go(recipe.get("pkg", pkg), bin_name)
    if strategy == "pip":
        return _install_pip(recipe.get("pkg", pkg), bin_name)

    logger.warning("[install] unknown strategy %r for %s", strategy, pkg)
    return None


def _install_npm(
    pkg: str,
    bin_name: str,
    extra_pkgs: Optional[list] = None,
) -> Optional[str]:
    """将 npm 包安装到我们的暂存目录。

    使用 ``npm install --prefix``，使二进制程序落在
    ``<staging>/node_modules/.bin/<bin_name>``，然后我们将它们
    符号链接到上一级目录，以便通过 PATH 方式直接访问。

    ``extra_pkgs`` 是要安装在同一 ``node_modules`` 树中的
    兄弟包列表。用于具有 npm 不会自动拉取的运行时对等依赖
    的 LSP 服务器（typescript-language-server 旁边需要
    ``typescript``；intelephense 独立分发）。
    """
    npm = shutil.which("npm")
    if npm is None:
        logger.info("[install] cannot install %s: npm not on PATH", pkg)
        return None
    staging = hermes_lsp_bin_dir().parent  # <HERMES_HOME>/lsp/
    install_targets = [pkg] + list(extra_pkgs or [])
    try:
        logger.info(
            "[install] npm install --prefix %s %s",
            staging,
            " ".join(install_targets),
        )
        proc = subprocess.run(
            [npm, "install", "--prefix", str(staging), "--silent", "--no-fund", "--no-audit", *install_targets],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
            stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            logger.warning(
                "[install] npm install failed for %s: %s", pkg, proc.stderr.strip()[:500]
            )
            return None
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("[install] npm install errored for %s: %s", pkg, e)
        return None

    # 查找二进制程序
    nm_bin = staging / "node_modules" / ".bin" / bin_name
    for c in _native_binary_candidates(nm_bin):
        if c.exists():
            # 符号链接到我们的 `lsp/bin/` 以获得稳定的 PATH 访问。
            link = hermes_lsp_bin_dir() / c.name
            if not link.exists():
                try:
                    link.symlink_to(c)
                except (OSError, NotImplementedError):
                    # 某些 Windows 环境下符号链接会失败——改用复制。
                    try:
                        shutil.copy2(c, link)
                    except OSError:
                        return str(c)
            return str(link if link.exists() else c)
    logger.warning("[install] npm install for %s succeeded but bin %s not found", pkg, bin_name)
    return None


def _install_go(pkg: str, bin_name: str) -> Optional[str]:
    """将 Go 模块安装到 GOBIN=<staging>。"""
    go = shutil.which("go")
    if go is None:
        logger.info("[install] cannot install %s: go not on PATH", pkg)
        return None
    staging = hermes_lsp_bin_dir()
    env = dict(os.environ)
    env["GOBIN"] = str(staging)
    try:
        logger.info("[install] go install %s (GOBIN=%s)", pkg, staging)
        proc = subprocess.run(
            [go, "install", pkg],
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
            stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            logger.warning(
                "[install] go install failed for %s: %s", pkg, proc.stderr.strip()[:500]
            )
            return None
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("[install] go install errored for %s: %s", pkg, e)
        return None
    bin_path = staging / bin_name
    if _is_windows():
        bin_path = bin_path.with_suffix(".exe")
    if bin_path.exists():
        return str(bin_path)
    logger.warning("[install] go install for %s succeeded but bin %s not found", pkg, bin_name)
    return None


def _install_pip(pkg: str, bin_name: str) -> Optional[str]:
    """将 Python 包安装到 Hermes 管理的目标目录。

    我们使用 ``pip install --target`` 以避免污染用户的
    site-packages。二进制程序会放到
    ``<staging>/python-packages/bin/``，然后符号链接到
    ``<staging>/bin``。注意：这仅适用于包含 console script
    的包。
    """
    pip_target = hermes_lsp_bin_dir().parent / "python-packages"
    pip_target.mkdir(parents=True, exist_ok=True)
    try:
        logger.info("[install] pip install --target %s %s", pip_target, pkg)
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--target", str(pip_target), "--quiet", pkg],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
            stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            logger.warning(
                "[install] pip install failed for %s: %s", pkg, proc.stderr.strip()[:500]
            )
            return None
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("[install] pip install errored for %s: %s", pkg, e)
        return None
    # 查找 console script。POSIX 平台的 wheel 通常写入 bin/，
    # 而 Windows 原生安装使用 Scripts/。
    script_dirs = [pip_target / "bin"]
    if _is_windows():
        script_dirs.append(pip_target / "Scripts")
    for script_dir in script_dirs:
        for bin_path in _native_binary_candidates(script_dir / bin_name):
            if bin_path.exists():
                link = hermes_lsp_bin_dir() / bin_path.name
                if not link.exists():
                    try:
                        link.symlink_to(bin_path)
                    except (OSError, NotImplementedError):
                        try:
                            shutil.copy2(bin_path, link)
                        except OSError:
                            return str(bin_path)
                return str(link if link.exists() else bin_path)
    return None


def detect_status(pkg: str) -> str:
    """返回包的 ``installed``、``missing`` 或 ``manual-only`` 状态。

    供 ``hermes lsp status`` CLI 使用，让用户快速了解
    哪些服务器可用，而无需启动任何进程。
    """
    recipe = INSTALL_RECIPES.get(pkg)
    bin_name = recipe.get("bin", pkg) if recipe else pkg
    if _existing_binary(bin_name):
        return "installed"
    if recipe and recipe.get("strategy") == "manual":
        return "manual-only"
    return "missing"


__all__ = [
    "INSTALL_RECIPES",
    "try_install",
    "detect_status",
    "hermes_lsp_bin_dir",
]
