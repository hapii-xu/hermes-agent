"""启动时安全态势审计（加载时警告，绝不阻塞）。

在进程启动时暴露危险的主机 / 部署姿态，让运维人员一眼就能看到
"你已暴露" 的信号。起因是 2026 年 6 月的
MCP 配置持久化攻击活动：被入侵的机器以 root 身份运行，暴露了
dashboard / API server，且没有防火墙——但没有任何东西通知
运维人员。这些检查是建议性的：它们发出 ``logger.warning`` 记录
并返回人类可读的字符串；从不抛出异常或阻塞启动。

检查项（每项独立且失败安全——任何内部错误都会被静默忽略
且不产生发现项）：

1. 以 root 身份运行（POSIX uid 0）。
2. SSH daemon 存在且启用了密码认证。
3. 在容器内运行，但 HERMES_HOME 数据目录上没有持久卷挂载
   （状态是临时的——容器重启后丢失）。
4. 网络可访问的 gateway 监听器（dashboard / API server）未
   配置认证。

跨平台：root 和 SSH 检查仅适用于 POSIX 系统，在 Windows 上为空操作。
所有检查都是尽力而为且只读的。
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hermes.security_audit")

# 哨兵值，确保审计在每个进程中只运行一次，即使 CLI 和
# gateway 启动路径都调用它。
_AUDIT_RAN = False


def _is_root() -> bool:
    """当进程以 POSIX uid 0 运行时返回 True。在 Windows 上始终返回 False。"""
    getuid = getattr(os, "geteuid", None) or getattr(os, "getuid", None)
    if getuid is None:
        return False
    try:
        return getuid() == 0
    except Exception:
        return False


def _running_as_root() -> Optional[str]:
    if not _is_root():
        return None
    return (
        "Running as ROOT. The agent's terminal/file tools execute with full "
        "root privileges — a single prompt-injection or exposed endpoint is a "
        "full host compromise. Run Hermes as an unprivileged user (or in a "
        "sandboxed terminal backend / container with a non-root user)."
    )


_SSHD_CONFIG_PATHS = (
    "/etc/ssh/sshd_config",
)
_SSHD_CONFIG_DIR = "/etc/ssh/sshd_config.d"


def _iter_sshd_config_lines() -> list[str]:
    """从 sshd_config 及其 drop-in 目录生成非注释行。"""
    lines: list[str] = []
    paths: list[Path] = [Path(p) for p in _SSHD_CONFIG_PATHS]
    try:
        d = Path(_SSHD_CONFIG_DIR)
        if d.is_dir():
            paths.extend(sorted(d.glob("*.conf")))
    except Exception:
        pass
    for p in paths:
        try:
            for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
                stripped = raw.strip()
                if stripped and not stripped.startswith("#"):
                    lines.append(stripped)
        except Exception:
            continue
    return lines


def _ssh_password_auth_enabled() -> Optional[str]:
    """当 SSH daemon 启用密码认证时发出警告。

    公开 SSH daemon 上的密码认证是经典的暴力破解攻击面，
    与具有 root 能力的 agent 机器搭配使用时非常危险。仅限 POSIX；
    当没有可读取的 sshd 配置时返回 None（例如 Windows，或未安装 SSH）。
    """
    lines = _iter_sshd_config_lines()
    if not lines:
        return None
    # sshd_config 中最后一条指令生效。默认值（无指令）为 "yes"。
    verdict = "yes"
    saw_directive = False
    for line in lines:
        m = re.match(r"(?i)^PasswordAuthentication\s+(\w+)", line)
        if m:
            verdict = m.group(1).lower()
            saw_directive = True
    if verdict == "no":
        return None
    qualifier = "" if saw_directive else " (default — no explicit directive)"
    return (
        f"SSH password authentication is ENABLED{qualifier}. Password auth is "
        "brute-forceable and dangerous on an internet-facing box. Set "
        "'PasswordAuthentication no' in sshd_config and use key-based auth."
    )


def _in_container() -> bool:
    """尽力检测容器环境（Docker / Podman / 通用 OCI）。"""
    if os.path.exists("/.dockerenv"):
        return True
    if os.environ.get("HERMES_DESKTOP_CHILD_PID"):
        return False  # desktop child, not a server container
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
        if any(tok in cgroup for tok in ("docker", "containerd", "kubepods", "libpod")):
            return True
    except Exception:
        pass
    return False


def _path_is_mounted(path: Path) -> bool:
    """True if *path* sits on (or under) a real mount point per /proc/mounts.

    Container overlay/root filesystems are ephemeral; a bind/volume mount over
    the data dir shows up as a distinct mount entry. We treat the path as
    persisted when a mountpoint at or above it is NOT the container root
    overlay.
    """
    try:
        target = path.resolve()
    except Exception:
        target = path
    try:
        mounts = Path("/proc/mounts").read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return True  # can't tell — fail safe (no warning)
    best = None
    best_fstype = ""
    for line in mounts:
        parts = line.split()
        if len(parts) < 3:
            continue
        mountpoint, fstype = parts[1], parts[2]
        try:
            mp = Path(mountpoint)
        except Exception:
            continue
        if mp == target or mp in target.parents:
            # Longest matching mountpoint wins (most specific).
            if best is None or len(str(mp)) > len(str(best)):
                best = mp
                best_fstype = fstype
    if best is None:
        return True
    # overlay / tmpfs over the data dir = ephemeral container storage.
    return best_fstype not in ("overlay", "tmpfs", "aufs")


def _container_no_volume_mount(hermes_home: Optional[Path]) -> Optional[str]:
    if not _in_container():
        return None
    home = hermes_home or Path(
        os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    )
    try:
        if _path_is_mounted(home):
            return None
    except Exception:
        return None
    return (
        f"Running in a container but the data dir ({home}) is NOT on a "
        "persistent volume mount — sessions, memory, skills, and API keys are "
        "ephemeral and lost on container restart. Mount a host volume over the "
        "HERMES_HOME data directory."
    )


def _network_listener_without_auth(config: Optional[dict]) -> list[str]:
    """Warn about network-accessible gateway listeners with no auth.

    Covers the API server (no API_SERVER_KEY) and the dashboard (non-loopback
    bind with no auth provider). Read-only against config + env; overlaps the
    hard fail-closed guards but surfaces the posture proactively at startup.
    """
    findings: list[str] = []
    try:
        from gateway.platforms.base import is_network_accessible
    except Exception:
        return findings

    cfg = config or {}

    # API server.
    try:
        plats = (cfg.get("platforms") or {})
        api = plats.get("api_server") if isinstance(plats, dict) else None
        if isinstance(api, dict) and api.get("enabled"):
            extra = api.get("extra") or {}
            host = extra.get("host") or os.environ.get("API_SERVER_HOST", "127.0.0.1")
            key = extra.get("key") or os.environ.get("API_SERVER_KEY", "")
            if is_network_accessible(str(host)) and not str(key).strip():
                findings.append(
                    f"OpenAI-compatible API server is network-accessible ({host}) "
                    "with NO API_SERVER_KEY. It dispatches terminal-capable agent "
                    "work — an unauthenticated network endpoint is remote code "
                    "execution. Set a strong API_SERVER_KEY."
                )
    except Exception:
        pass

    return findings


def run_security_audit(
    *, hermes_home: Optional[Path] = None, config: Optional[dict] = None
) -> list[str]:
    """Run all checks and return a list of human-readable warning strings.

    Pure: no logging, no side effects. Each check is independently
    fail-safe. Used directly by tests; the logging wrapper is
    :func:`log_startup_security_warnings`.
    """
    findings: list[str] = []
    for check in (
        _running_as_root,
        _ssh_password_auth_enabled,
    ):
        try:
            r = check()
            if r:
                findings.append(r)
        except Exception:
            continue
    try:
        r = _container_no_volume_mount(hermes_home)
        if r:
            findings.append(r)
    except Exception:
        pass
    try:
        findings.extend(_network_listener_without_auth(config))
    except Exception:
        pass
    return findings


def log_startup_security_warnings(
    *,
    hermes_home: Optional[Path] = None,
    config: Optional[dict] = None,
    force: bool = False,
) -> list[str]:
    """Run the audit once per process and emit each finding via logger.warning.

    Returns the findings (also for tests). Never raises. Idempotent unless
    ``force=True`` (used by tests).
    """
    global _AUDIT_RAN
    if _AUDIT_RAN and not force:
        return []
    _AUDIT_RAN = True
    try:
        findings = run_security_audit(hermes_home=hermes_home, config=config)
    except Exception:
        return []
    if findings:
        logger.warning(
            "Security posture audit found %d issue(s) — review your deployment:",
            len(findings),
        )
        for i, f in enumerate(findings, 1):
            logger.warning("  [security %d/%d] %s", i, len(findings), f)
    return findings
