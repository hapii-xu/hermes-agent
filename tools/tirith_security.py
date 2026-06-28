"""Tirith 执行前安全扫描封装。

以子进程方式运行 tirith 二进制，对命令进行内容层面的威胁扫描（同形 URL、
管道输送给解释器、终端注入等）。

退出码是判决的事实来源：
  0 = 放行，1 = 拦截，2 = 警告

JSON stdout 用于丰富 findings/summary，但绝不覆盖判决。运营性故障（派生
错误、超时、未知退出码）遵守 fail_open 配置。编程错误会向上传播。

自动安装：如果在 PATH 或配置路径下找不到 tirith，会自动从 GitHub releases
下载到 $HERMES_HOME/bin/tirith。下载总是会校验 SHA-256 校验和。当 PATH 上
有 cosign 时，还会进行来源验证（GitHub Actions 工作流签名）。如果没有安装
cosign，下载仅用 SHA-256 校验继续进行 —— 通过 HTTPS + 校验和仍然安全，只是
没有供应链来源证明。安装在一个后台线程中运行，启动永远不会阻塞。
"""

import hashlib
import json
import logging
import os
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_REPO = "sheeki03/tirith"

# Cosign 来源验证 —— 固定到具体的发布工作流
_COSIGN_IDENTITY_REGEXP = f"^https://github.com/{_REPO}/\\.github/workflows/release\\.yml@refs/tags/v"
_COSIGN_ISSUER = "https://token.actions.githubusercontent.com"

# ---------------------------------------------------------------------------
# 配置辅助函数
# ---------------------------------------------------------------------------

def _env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.lower() in {"1", "true", "yes"}


def _env_int(key: str, default: int) -> int:
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _load_security_config() -> dict:
    """从 config.yaml 加载安全设置，并支持环境变量覆盖。"""
    defaults = {
        "tirith_enabled": True,
        "tirith_path": "tirith",
        "tirith_timeout": 5,
        "tirith_fail_open": True,
    }
    try:
        from hermes_cli.config import load_config
        cfg = load_config().get("security", {}) or {}
    except Exception:
        cfg = {}

    return {
        "tirith_enabled": _env_bool("TIRITH_ENABLED", cfg.get("tirith_enabled", defaults["tirith_enabled"])),
        "tirith_path": os.getenv("TIRITH_BIN", cfg.get("tirith_path", defaults["tirith_path"])),
        "tirith_timeout": _env_int("TIRITH_TIMEOUT", cfg.get("tirith_timeout", defaults["tirith_timeout"])),
        "tirith_fail_open": _env_bool("TIRITH_FAIL_OPEN", cfg.get("tirith_fail_open", defaults["tirith_fail_open"])),
    }


# ---------------------------------------------------------------------------
# 自动安装
# ---------------------------------------------------------------------------

# 首次解析后缓存的路径（避免每条命令都重复 shutil.which）。
# _INSTALL_FAILED 表示“我们试过且失败了” —— 阻止每条命令都重试。
_resolved_path: str | None | bool = None
_INSTALL_FAILED = False  # 哨兵：与“尚未尝试过”区分开
_install_failure_reason: str = ""  # 当 _resolved_path 为 _INSTALL_FAILED 时的原因标签

# 后台安装线程协调
_install_lock = threading.Lock()
_install_thread: threading.Thread | None = None

# 警告去重。派生/路径警告位于热路径上 —— 没有这个去重集合，一个 PATH 上
# 没有 ``tirith`` 的 Windows 安装（例如后台安装线程仍在运行，或安装被标记为
# 失败）会对每条终端命令都喷一次 ``tirith spawn failed: [WinError 2]...``，
# 轻易就能用数百行相同内容填满 errors.log。
_warned_messages: set[str] = set()
_warned_lock = threading.Lock()


def _warn_once(key: str, message: str, *args) -> None:
    """``logger.warning``，但在进程生命周期内每个 ``key`` 至多一次。
    用于避免 fail-open 的 tirith 配置错误在每条命令上都触发、淹没日志。"""
    with _warned_lock:
        if key in _warned_messages:
            return
        _warned_messages.add(key)
    logger.warning(message, *args)


def _reset_spawn_warning_state() -> None:
    """清空 warn-once 去重集合。在 tirith 被全新（重新）安装后调用，这样
    下一次失败能再次浮现 —— 例如用户在会话中途删除了二进制。
    """
    with _warned_lock:
        _warned_messages.clear()

# 持久化到磁盘的失败标记 —— 避免跨进程重启重试
_MARKER_TTL = 86400  # 24 小时


def _get_hermes_home() -> str:
    """返回 Hermes home 目录，尊重 HERMES_HOME 环境变量。"""
    return str(get_hermes_home())


def _failure_marker_path() -> str:
    """返回安装失败标记文件的路径。"""
    return os.path.join(_get_hermes_home(), ".tirith-install-failed")


def _read_failure_reason() -> str | None:
    """从磁盘标记读取失败原因。

    返回原因字符串；如果标记不存在或早于 _MARKER_TTL，则返回 None。
    """
    try:
        p = _failure_marker_path()
        mtime = os.path.getmtime(p)
        if (time.time() - mtime) >= _MARKER_TTL:
            return None
        with open(p, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _is_install_failed_on_disk() -> bool:
    """检查最近一次安装失败是否被持久化到了磁盘。

    在以下情况返回 False（允许重试）：
    - 不存在标记
    - 标记早于 _MARKER_TTL（24 小时）
    - 标记原因是 'cosign_missing' 且 cosign 现已在 PATH 上
    """
    reason = _read_failure_reason()
    if reason is None:
        return False
    if reason == "cosign_missing" and shutil.which("cosign"):
        _clear_install_failed()
        return False
    return True


def _mark_install_failed(reason: str = ""):
    """把安装失败持久化到磁盘，避免下一个进程再次重试。

    参数：
        reason: 标识失败原因的简短标签。当 cosign 不在 PATH 上时请用
                "cosign_missing"，这样一旦 cosign 可用，标记就能被自动清除。
    """
    try:
        p = _failure_marker_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(reason)
    except OSError:
        pass


def _clear_install_failed():
    """安装成功后移除失败标记。"""
    # 重置 warn-once 去重集合，使后续失败（例如用户删除了二进制）能再次
    # 浮现在日志里，而不是被修复前留下的过期去重键默默抑制。
    _reset_spawn_warning_state()
    try:
        os.unlink(_failure_marker_path())
    except OSError:
        pass


def _hermes_bin_dir() -> str:
    """返回 $HERMES_HOME/bin，必要时创建它。"""
    d = os.path.join(_get_hermes_home(), "bin")
    os.makedirs(d, exist_ok=True)
    return d


def _detect_target() -> str | None:
    """返回当前平台对应的 Rust target triple，或 None。

    Windows 被有意列为不支持 —— tirith 不提供 Windows 构建。调用方应把
    `None` 视为“本平台永远不会拥有 tirith”，并静默回退到模式匹配守卫。
    """
    system = platform.system()
    machine = platform.machine().lower()

    # Android（Termux）与 Linux ABI 兼容 —— 复用 Linux 二进制。
    if system == "Darwin":
        plat = "apple-darwin"
    elif system in {"Linux", "Android"}:
        plat = "unknown-linux-gnu"
    else:
        return None

    if machine in {"x86_64", "amd64"}:
        arch = "x86_64"
    elif machine in {"aarch64", "arm64"}:
        arch = "aarch64"
    else:
        return None

    return f"{arch}-{plat}"


def is_platform_supported() -> bool:
    """当 tirith 为本 OS+架构提供预编译二进制时返回 True。

    供调用方（CLI banner 等）用来区分“tirith 安装失败”与“tirith 在这里
    根本不会安装” —— 后者保持静默，因为用户对此无能为力。
    """
    return _detect_target() is not None


def _download_file(url: str, dest: str, timeout: int = 10):
    """把一个 URL 下载到本地文件。"""
    req = urllib.request.Request(url)
    token = os.getenv("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"token {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def _verify_cosign(checksums_path: str, sig_path: str, cert_path: str) -> bool | None:
    """校验 checksums.txt 上的 cosign 来源签名。

    返回：
        True  —— cosign 校验通过
        False —— 找到 cosign 但校验失败
        None  —— cosign 不可用（不在 PATH 上，或执行失败）

    调用方把 False 和 None 都视为“中止自动安装” —— 只有 True 才允许安装
    继续。
    """
    cosign = shutil.which("cosign")
    if not cosign:
        logger.info("cosign not found on PATH")
        return None

    try:
        result = subprocess.run(
            [cosign, "verify-blob",
             "--certificate", cert_path,
             "--signature", sig_path,
             "--certificate-identity-regexp", _COSIGN_IDENTITY_REGEXP,
             "--certificate-oidc-issuer", _COSIGN_ISSUER,
             checksums_path],
            capture_output=True,
            text=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            logger.info("cosign provenance verification passed")
            return True
        else:
            logger.warning("cosign verification failed (exit %d): %s",
                          result.returncode, result.stderr.strip())
            return False
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("cosign execution failed: %s", exc)
        return None


def _verify_checksum(archive_path: str, checksums_path: str, archive_name: str) -> bool:
    """对照 checksums.txt 校验归档文件的 SHA-256。"""
    expected = None
    with open(checksums_path, encoding="utf-8") as f:
        for line in f:
            # 格式："<hash>  <filename>"
            parts = line.strip().split("  ", 1)
            if len(parts) == 2 and parts[1] == archive_name:
                expected = parts[0]
                break
    if not expected:
        logger.warning("No checksum entry for %s", archive_name)
        return False

    sha = hashlib.sha256()
    with open(archive_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    actual = sha.hexdigest()
    if actual != expected:
        logger.warning("Checksum mismatch: expected %s, got %s", expected, actual)
        return False
    return True


def _extract_tirith_binary(tar: tarfile.TarFile, dest_dir: str, log) -> tuple[str | None, str]:
    """把 tirith 二进制从发布归档中解压到 dest_dir。"""
    for member in tar.getmembers():
        if member.name == "tirith" or member.name.endswith("/tirith"):
            if ".." in member.name:
                continue
            if not member.isfile():
                log("tirith archive member is not a regular file: %s", member.name)
                return None, "binary_not_regular_file"
            src_file = tar.extractfile(member)
            if src_file is None:
                log("tirith binary could not be read from archive")
                return None, "binary_extract_failed"

            dest_path = os.path.join(dest_dir, "tirith")
            try:
                with open(dest_path, "wb") as out:
                    shutil.copyfileobj(src_file, out)
            finally:
                src_file.close()
            return dest_path, ""

    log("tirith binary not found in archive")
    return None, "binary_not_in_archive"


def _install_tirith(*, log_failures: bool = True) -> tuple[str | None, str]:
    """下载并安装 tirith 到 $HERMES_HOME/bin/tirith。

    通过 cosign 和 SHA-256 校验和校验来源。
    返回 (installed_path, failure_reason)。成功时 failure_reason 为 ""。
    failure_reason 是一个简短标签，供磁盘标记判断该失败是否可重试
    （例如 "cosign_missing" 在 cosign 出现时会被清除）。
    """
    log = logger.warning if log_failures else logger.debug

    target = _detect_target()
    if not target:
        logger.info("tirith auto-install: unsupported platform %s/%s",
                     platform.system(), platform.machine())
        return None, "unsupported_platform"

    archive_name = f"tirith-{target}.tar.gz"
    base_url = f"https://github.com/{_REPO}/releases/latest/download"

    try:
        tmpdir = tempfile.mkdtemp(prefix="tirith-install-")
    except OSError as exc:
        log("tirith install failed: cannot create temp dir: %s", exc)
        return None, "no_space"
    try:
        archive_path = os.path.join(tmpdir, archive_name)
        checksums_path = os.path.join(tmpdir, "checksums.txt")
        sig_path = os.path.join(tmpdir, "checksums.txt.sig")
        cert_path = os.path.join(tmpdir, "checksums.txt.pem")

        logger.info("tirith not found — downloading latest release for %s...", target)

        try:
            _download_file(f"{base_url}/{archive_name}", archive_path)
            _download_file(f"{base_url}/checksums.txt", checksums_path)
        except Exception as exc:
            log("tirith download failed: %s", exc)
            return None, "download_failed"

        # Cosign 来源验证 —— 首选但非强制。当 cosign 可用时，我们验证该发布
        # 确实由预期的 GitHub Actions 工作流产出（完整供应链证明）。没有
        # cosign 时，SHA-256 校验和 + HTTPS 仍然提供完整性与传输层面的真实性。
        cosign_verified = False
        if shutil.which("cosign"):
            try:
                _download_file(f"{base_url}/checksums.txt.sig", sig_path)
                _download_file(f"{base_url}/checksums.txt.pem", cert_path)
            except Exception as exc:
                logger.info("cosign artifacts unavailable (%s), proceeding with SHA-256 only", exc)
            else:
                cosign_result = _verify_cosign(checksums_path, sig_path, cert_path)
                if cosign_result is True:
                    cosign_verified = True
                elif cosign_result is False:
                    # 校验被明确拒绝 —— 中止，该发布可能已被篡改。
                    log("tirith install aborted: cosign provenance verification failed")
                    return None, "cosign_verification_failed"
                else:
                    # None = 执行失败（超时/OSError） —— 因为 cosign 自身坏了，
                    # 仅用 SHA-256 继续。
                    logger.info("cosign execution failed, proceeding with SHA-256 only")
        else:
            logger.info("cosign not on PATH — installing tirith with SHA-256 verification only "
                        "(install cosign for full supply chain verification)")

        if not _verify_checksum(archive_path, checksums_path, archive_name):
            return None, "checksum_failed"

        with tarfile.open(archive_path, "r:gz") as tar:
            src, reason = _extract_tirith_binary(tar, tmpdir, log)
            if src is None:
                return None, reason

        dest = os.path.join(_hermes_bin_dir(), "tirith")
        try:
            shutil.move(src, dest)
        except OSError:
            # 跨设备移动（在 Docker、NFS 中常见）：shutil.move() 会回退到
            # copy2 + unlink，但 copy2 的元数据步骤可能抛出 PermissionError。
            # 改用纯拷贝 + 手动 chmod。
            try:
                shutil.copy(src, dest)
            except OSError:
                # 清理部分写入的 dest，防止不可执行的 retry 循环
                try:
                    os.unlink(dest)
                except OSError:
                    pass
                return None, "cross_device_copy_failed"
        os.chmod(dest, os.stat(dest).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        verification = "cosign + SHA-256" if cosign_verified else "SHA-256 only"
        logger.info("tirith installed to %s (%s)", dest, verification)
        return dest, ""

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _is_explicit_path(configured_path: str) -> bool:
    """当用户显式配置了一个非默认的 tirith 路径时返回 True。"""
    return configured_path != "tirith"


def _resolve_tirith_path(configured_path: str) -> str:
    """解析 tirith 二进制路径，必要时自动安装。

    如果用户显式设置了路径（除了裸 "tirith" 默认值之外的任何值），该路径
    具有权威性 —— 我们绝不会回退去自动下载一个不同的二进制。

    对于默认的 "tirith"：
    1. 通过 shutil.which 查 PATH
    2. $HERMES_HOME/bin/tirith（之前已自动安装过）
    3. 从 GitHub releases 自动安装 → $HERMES_HOME/bin/tirith

    失败的安装会在进程生命周期内被缓存（并持久化到磁盘 24 小时），以避免
    反复发起网络请求。
    """
    global _resolved_path, _install_failure_reason

    # 快路径：在上一次调用中已成功解析。
    if _resolved_path is not None and _resolved_path is not _INSTALL_FAILED:
        return _resolved_path

    expanded = os.path.expanduser(configured_path)
    explicit = _is_explicit_path(configured_path)
    install_failed = _resolved_path is _INSTALL_FAILED

    # 该平台没有 tirith 构建（Windows 等）。缓存判决并返回未展开的配置路径
    # —— 派生循环会通过去重的 OSError 处理器 fail-open，但仅在首次调用之后；
    # 后续调用里上面的快路径会在派生之前短路。
    if not explicit and not is_platform_supported():
        _resolved_path = _INSTALL_FAILED
        _install_failure_reason = "unsupported_platform"
        return expanded

    # 显式路径：检查它然后停止。绝不自动下载替代品。
    if explicit:
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            _resolved_path = expanded
            return expanded
        # 也尝试 shutil.which，以防它是 PATH 上的一个裸名字
        found = shutil.which(expanded)
        if found:
            _resolved_path = found
            return found
        logger.warning("Configured tirith path %r not found; scanning disabled", configured_path)
        _resolved_path = _INSTALL_FAILED
        _install_failure_reason = "explicit_path_missing"
        return expanded

    # 默认 "tirith" —— 总是重跑廉价的本地检查，这样即使在上一次网络失败
    # 之后，手动安装也能被发现（P2 修复：长期运行的 gateway/CLI 无需重启
    # 即可恢复）。
    found = shutil.which("tirith")
    if found:
        _resolved_path = found
        _install_failure_reason = ""
        _clear_install_failed()
        return found

    hermes_bin = os.path.join(_hermes_bin_dir(), "tirith")
    if os.path.isfile(hermes_bin) and os.access(hermes_bin, os.X_OK):
        _resolved_path = hermes_bin
        _install_failure_reason = ""
        _clear_install_failed()
        return hermes_bin

    # 本地检查失败。如果上一次安装尝试已失败，跳过网络重试 —— 除非失败
    # 原因是 "cosign_missing" 且 cosign 现已可用（可重试的诱因已在进程内
    # 解决）。
    if install_failed:
        if _install_failure_reason == "cosign_missing" and shutil.which("cosign"):
            # 可重试诱因已解决 —— 清除哨兵并落入下方重试
            _resolved_path = None
            _install_failure_reason = ""
            _clear_install_failed()
            install_failed = False
        else:
            return expanded

    # 如果已有后台安装线程在运行，不要并行再起一个 —— 返回配置的路径；
    # check_command_security 中的 OSError 处理器会在该线程结束前应用
    # fail_open。
    if _install_thread is not None and _install_thread.is_alive():
        return expanded

    # 在尝试网络下载之前检查磁盘失败标记。保留该标记的真实原因，以便
    # 内存中的重试逻辑能在不重启的情况下检测可重试诱因
    # （例如 cosign_missing）。
    disk_reason = _read_failure_reason()
    if disk_reason is not None and _is_install_failed_on_disk():
        _resolved_path = _INSTALL_FAILED
        _install_failure_reason = disk_reason
        return expanded

    installed, reason = _install_tirith()
    if installed:
        _resolved_path = installed
        _install_failure_reason = ""
        _clear_install_failed()
        return installed

    # 安装失败 —— 缓存该缺失并把原因持久化到磁盘
    _resolved_path = _INSTALL_FAILED
    _install_failure_reason = reason
    _mark_install_failed(reason)
    return expanded


def _background_install(*, log_failures: bool = True):
    """后台线程目标：下载并安装 tirith。"""
    global _resolved_path, _install_failure_reason
    with _install_lock:
        # 获取锁后再次检查（另一个线程可能已解析完成）
        if _resolved_path is not None:
            return

        # 重新检查本地路径（可能已被另一个进程安装）
        found = shutil.which("tirith")
        if found:
            _resolved_path = found
            _install_failure_reason = ""
            return

        hermes_bin = os.path.join(_hermes_bin_dir(), "tirith")
        if os.path.isfile(hermes_bin) and os.access(hermes_bin, os.X_OK):
            _resolved_path = hermes_bin
            _install_failure_reason = ""
            return

        installed, reason = _install_tirith(log_failures=log_failures)
        if installed:
            _resolved_path = installed
            _install_failure_reason = ""
            _clear_install_failed()
        else:
            _resolved_path = _INSTALL_FAILED
            _install_failure_reason = reason
            _mark_install_failed(reason)


def ensure_installed(*, log_failures: bool = True):
    """确保 tirith 可用，必要时在后台下载。

    快速的 PATH/本地检查是同步的；网络下载在一个守护线程里运行，使启动
    永不阻塞。可安全多次调用。如果立即可用则返回解析后的路径，否则返回
    None。
    """
    global _resolved_path, _install_thread, _install_failure_reason

    cfg = _load_security_config()
    if not cfg["tirith_enabled"]:
        return None

    # 已在上一次调用中解析完成
    if _resolved_path is not None and _resolved_path is not _INSTALL_FAILED:
        path = _resolved_path
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
        return None

    # 该平台没有 tirith 构建（例如 Windows） —— 不要探测 PATH，不要启动
    # 下载线程，不要写磁盘失败标记。模式匹配守卫仍然运行；此路径保持静默。
    if not is_platform_supported():
        _resolved_path = _INSTALL_FAILED
        _install_failure_reason = "unsupported_platform"
        return None

    configured_path = cfg["tirith_path"]
    explicit = _is_explicit_path(configured_path)
    expanded = os.path.expanduser(configured_path)

    # 显式路径：仅同步检查，不下载
    if explicit:
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            _resolved_path = expanded
            return expanded
        found = shutil.which(expanded)
        if found:
            _resolved_path = found
            return found
        _resolved_path = _INSTALL_FAILED
        _install_failure_reason = "explicit_path_missing"
        return None

    # 默认 "tirith" —— 先做快速的本地检查（无网络）
    found = shutil.which("tirith")
    if found:
        _resolved_path = found
        _install_failure_reason = ""
        _clear_install_failed()
        return found

    hermes_bin = os.path.join(_hermes_bin_dir(), "tirith")
    if os.path.isfile(hermes_bin) and os.access(hermes_bin, os.X_OK):
        _resolved_path = hermes_bin
        _install_failure_reason = ""
        _clear_install_failed()
        return hermes_bin

    # 如果之前在内存中失败过，检查诱因是否现已解决
    if _resolved_path is _INSTALL_FAILED:
        if _install_failure_reason == "cosign_missing" and shutil.which("cosign"):
            _resolved_path = None
            _install_failure_reason = ""
            _clear_install_failed()
        else:
            return None

    # 检查磁盘失败标记（24 小时内跳过网络尝试，除非 cosign_missing 诱因已
    # 解决 —— 由 _is_install_failed_on_disk 处理）。保留标记的真实原因，
    # 供内存中的重试逻辑使用。
    disk_reason = _read_failure_reason()
    if disk_reason is not None and _is_install_failed_on_disk():
        _resolved_path = _INSTALL_FAILED
        _install_failure_reason = disk_reason
        return None

    # 需要下载 —— 启动后台线程，使启动不阻塞
    if _install_thread is None or not _install_thread.is_alive():
        _install_thread = threading.Thread(
            target=_background_install,
            kwargs={"log_failures": log_failures},
            daemon=True,
        )
        _install_thread.start()

    return None  # 暂不可用；命令会 fail-open 直到就绪


# ---------------------------------------------------------------------------
# 主 API
# ---------------------------------------------------------------------------

_MAX_FINDINGS = 50
_MAX_SUMMARY_LEN = 500


def check_command_security(command: str) -> dict:
    """对一条命令运行 tirith 安全扫描。

    退出码决定动作（0=放行，1=拦截，2=警告）。JSON 用于丰富
    findings/summary。派生失败和超时遵守 fail_open 配置。编程错误向上传播。

    返回：
        {"action": "allow"|"warn"|"block", "findings": [...], "summary": str}
    """
    cfg = _load_security_config()

    if not cfg["tirith_enabled"]:
        return {"action": "allow", "findings": [], "summary": ""}

    # 不支持的平台（Windows 等） —— tirith 在这里没有二进制，也永远不会有。
    # 完全跳过解析器，这样我们连派生都不会尝试。模式匹配守卫仍通过
    # approval.py 的其余部分运行。
    if not is_platform_supported():
        return {"action": "allow", "findings": [], "summary": ""}

    tirith_path = _resolve_tirith_path(cfg["tirith_path"])
    timeout = cfg["tirith_timeout"]
    fail_open = cfg["tirith_fail_open"]

    if tirith_path is None:
        _warn_once(
            "tirith_path_none",
            "tirith path resolved to None; scanning disabled",
        )
        if fail_open:
            return {"action": "allow", "findings": [], "summary": "tirith path unavailable"}
        return {"action": "block", "findings": [], "summary": "tirith path unavailable (fail-closed)"}

    try:
        result = subprocess.run(
            [tirith_path, "check", "--json", "--non-interactive",
             "--shell", "posix", "--", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        # 覆盖 FileNotFoundError、PermissionError、执行格式错误。
        # 按 ``(errno, exc class)`` 去重，使一种瞬态失败模式浮现一次但不会
        # 在每条命令上淹没日志 —— 常见于 Windows 上配置路径 "tirith" 尚未
        # 在 PATH 上（后台安装仍在运行，或当天安装被标记为失败）。
        spawn_key = f"tirith_spawn_failed:{type(exc).__name__}:{getattr(exc, 'errno', '')}"
        _warn_once(spawn_key, "tirith spawn failed: %s", exc)
        if fail_open:
            return {"action": "allow", "findings": [], "summary": f"tirith unavailable: {exc}"}
        return {"action": "block", "findings": [], "summary": f"tirith spawn failed (fail-closed): {exc}"}
    except subprocess.TimeoutExpired:
        _warn_once(
            f"tirith_timeout:{timeout}",
            "tirith timed out after %ds",
            timeout,
        )
        if fail_open:
            return {"action": "allow", "findings": [], "summary": f"tirith timed out ({timeout}s)"}
        return {"action": "block", "findings": [], "summary": "tirith timed out (fail-closed)"}

    # 把退出码映射到动作
    exit_code = result.returncode
    if exit_code == 0:
        action = "allow"
    elif exit_code == 1:
        action = "block"
    elif exit_code == 2:
        action = "warn"
    else:
        # 未知退出码 —— 遵守 fail_open
        logger.warning("tirith returned unexpected exit code %d", exit_code)
        if fail_open:
            return {"action": "allow", "findings": [], "summary": f"tirith exit code {exit_code} (fail-open)"}
        return {"action": "block", "findings": [], "summary": f"tirith exit code {exit_code} (fail-closed)"}

    # 解析 JSON 用于丰富（绝不覆盖退出码判决）
    findings = []
    summary = ""
    try:
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        raw_findings = data.get("findings", [])
        findings = raw_findings[:_MAX_FINDINGS]
        summary = (data.get("summary", "") or "")[:_MAX_SUMMARY_LEN]
    except (json.JSONDecodeError, AttributeError):
        # JSON 解析失败只降低 findings/summary 质量，不影响判决
        logger.debug("tirith JSON parse failed, using exit code only")
        if action == "block":
            summary = "security issue detected (details unavailable)"
        elif action == "warn":
            summary = "security warning detected (details unavailable)"

    # 抑制仅由针对 .app TLD 的 lookalike_tld 单一发现组成的 warn 判决。
    # .app 是被许多生产服务使用的合法 gTLD，而“可能与文件扩展名混淆”这一
    # 启发式会对正常的 API 调用产生误报。任何其他发现（包括针对非 .app TLD
    # 的其他 lookalike_tld 条目）都保留 warn 动作。
    if action == "warn" and findings:
        non_suppressible = [f for f in findings if not _is_app_tld_finding(f)]
        if not non_suppressible:
            action = "allow"
            findings = []
            summary = ""

    return {"action": action, "findings": findings, "summary": summary}


def _is_app_tld_finding(finding: dict) -> bool:
    """当本发现仅是针对 .app TLD 的 lookalike_tld 警告时返回 True。

    检查 rule_id 并查看 Tirith 可能用来承载 TLD 字符串的常见 value/detail
    字段名。
    """
    if not isinstance(finding, dict):
        return False
    if finding.get("rule_id") != "lookalike_tld":
        return False
    for field in ("value", "tld", "detail", "description", "message"):
        val = finding.get(field)
        if val is not None and ".app" in str(val).lower():
            return True
    return False
