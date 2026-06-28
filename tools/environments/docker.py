"""用于沙箱化命令执行的 Docker 执行环境。

安全加固（cap-drop ALL、no-new-privileges、PID 限制），
可配置的资源限制（CPU、内存、磁盘），以及通过 bind mount 实现的
可选文件系统持久化。
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional

from tools.environments.base import BaseEnvironment, _popen_bash
from tools.environments.local import _HERMES_PROVIDER_ENV_BLOCKLIST

logger = logging.getLogger(__name__)


# 当 'docker' 不在 PATH 中时检查的常见 Docker Desktop 安装路径。
# macOS Intel：/usr/local/bin，macOS Apple Silicon（Homebrew）：/opt/homebrew/bin，
# Docker Desktop 应用包：/Applications/Docker.app/Contents/Resources/bin
_DOCKER_SEARCH_PATHS = [
    "/usr/local/bin/docker",
    "/opt/homebrew/bin/docker",
    "/Applications/Docker.app/Contents/Resources/bin/docker",
]

_docker_executable: Optional[str] = None  # 解析一次后缓存
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _normalize_forward_env_names(forward_env: list[str] | None) -> list[str]:
    """返回去重后的合法环境变量名列表。"""
    normalized: list[str] = []
    seen: set[str] = set()

    for item in forward_env or []:
        if not isinstance(item, str):
            logger.warning("Ignoring non-string docker_forward_env entry: %r", item)
            continue

        key = item.strip()
        if not key:
            continue
        if not _ENV_VAR_NAME_RE.match(key):
            logger.warning("Ignoring invalid docker_forward_env entry: %r", item)
            continue
        if key in seen:
            continue

        seen.add(key)
        normalized.append(key)

    return normalized


def _normalize_env_dict(env: dict | None) -> dict[str, str]:
    """校验并归一化 docker_env 字典为 {str: str}。

    过滤掉变量名非法或值非字符串的条目。
    """
    if not env:
        return {}
    if not isinstance(env, dict):
        logger.warning("docker_env is not a dict: %r", env)
        return {}

    normalized: dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str) or not _ENV_VAR_NAME_RE.match(key.strip()):
            logger.warning("Ignoring invalid docker_env key: %r", key)
            continue
        key = key.strip()
        if not isinstance(value, str):
            # 将简单标量类型（int、bool、float）强制转为字符串；
            # 拒绝复杂类型。
            if isinstance(value, (int, float, bool)):
                value = str(value)
            else:
                logger.warning("Ignoring non-string docker_env value for %r: %r", key, value)
                continue
        normalized[key] = value

    return normalized


def _load_hermes_env_vars() -> dict[str, str]:
    """加载 ~/.hermes/.env 的值，且不让 Docker 命令执行失败。"""
    try:
        from hermes_cli.config import load_env

        return load_env() or {}
    except Exception:
        return {}


# Docker 标签值必须匹配 [a-zA-Z0-9_.-] 且长度 ≤63 字符，才能安全地通过
# `docker ps --filter label=key=value` 往返。Profile 和 task 名技术上
# 可以包含其他字符；这里做防御性清洗。
_LABEL_VALUE_OK_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _sanitize_label_value(value: str) -> str:
    """将 *value* 强制转换为 Docker 标签安全的格式（字母数字 + ``_.-``，≤63 字符）。

    空输入或全非法字符的输入会塌缩为 ``"unknown"``，这样结果标签始终
    可查询。在容器创建时使用；切勿把清洗后的值再回传到应用逻辑中。
    """
    if not isinstance(value, str) or not value:
        return "unknown"
    cleaned = _LABEL_VALUE_OK_RE.sub("_", value)
    cleaned = cleaned[:63] or "unknown"
    return cleaned


def _get_active_profile_name() -> str:
    """返回活跃的 Hermes profile 名，出错时返回 ``"default"``。

    在容器创建时解析，这样一个容器会被永久打上创建它的 profile 的
    标签。同一进程内的 profile 切换不会追溯地重新标记运行中的容器。
    """
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name() or "default"
    except Exception:
        return "default"


def reap_orphan_containers(
    *,
    max_age_seconds: int = 600,
    profile_filter: str | None = None,
    docker_exe: str | None = None,
) -> int:
    """移除先前进程遗留的、带有 hermes 标签的过期容器。

    目标容器需同时满足：

    * ``label=hermes-agent=1``（由本代码库创建）
    * ``status=exited``（运行中的容器绝不会被回收——它们可能属于
      一个兄弟 Hermes 进程，其复用路径会拾取它们；杀掉它们会在
      命令执行中途使兄弟进程崩溃）
    * （可选）``label=hermes-profile=<profile_filter>``（默认只清扫
      调用方的 profile；profile A 中的 hermes 进程绝不能拆除
      profile B 的容器）
    * ``State.FinishedAt`` 早于 *max_age_seconds* 之前（这样一个
      刚退出、即将被替换的兄弟进程的容器不会被从它脚下抽走）

    返回移除的容器数量。尽力而为：任何失败（docker 守护进程不可达、
    inspect 缓慢、解析错误）都以 debug 级别记录日志，函数返回失败前
    已处理掉的数量。可重复调用；幂等。

    Issue #20561——这是 SIGKILL / OOM / 崩溃的终端退出绕过
    ``atexit`` 清理钩子的安全网。没有它，即便有先前提交中的清理修复，
    一个被硬杀的 Hermes 进程也会永久留下它的容器，因为没有后续
    Hermes 进程被安排去复用那个确切的 (task, profile) 对。
    """
    docker = docker_exe or find_docker() or "docker"
    filters = ["--filter", "label=hermes-agent=1", "--filter", "status=exited"]
    if profile_filter:
        filters.extend(["--filter", f"label=hermes-profile={_sanitize_label_value(profile_filter)}"])

    try:
        listing = subprocess.run(
            [docker, "ps", "-a", *filters, "--format", "{{.ID}}"],
            capture_output=True, text=True, timeout=15, check=False,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug("orphan reaper docker ps failed: %s", e)
        return 0
    if listing.returncode != 0:
        logger.debug(
            "orphan reaper docker ps returned %d: %s",
            listing.returncode, listing.stderr.strip(),
        )
        return 0

    candidate_ids = [ln.strip() for ln in listing.stdout.splitlines() if ln.strip()]
    if not candidate_ids:
        return 0

    # 检查每个候选容器的 FinishedAt；只回收退出时间足够久的。
    # 逐容器检查（而非批量 inspect）可将失败波及范围限制为一次一个容器。
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    removed = 0
    for cid in candidate_ids:
        finished_at = _container_finished_at(docker, cid)
        if finished_at is None:
            # 无法确定时长——保守起见，不处理它。
            continue
        age = (now - finished_at).total_seconds()
        if age < max_age_seconds:
            continue
        try:
            result = subprocess.run(
                [docker, "rm", "-f", cid],
                capture_output=True, text=True, timeout=30,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                removed += 1
                logger.info(
                    "Reaped orphan container %s (exited %d seconds ago)",
                    cid[:12], int(age),
                )
            else:
                logger.debug(
                    "docker rm -f %s failed: %s",
                    cid[:12], result.stderr.strip(),
                )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.debug("orphan reaper docker rm %s failed: %s", cid[:12], e)
    return removed


def _container_finished_at(docker_exe: str, container_id: str):
    """解析 *container_id* 的 ``docker inspect`` FinishedAt。

    返回一个带时区的 datetime；如果该字段缺失、无法解析，或者是
    Docker 为从未结束的容器发出的零值
    ``0001-01-01T00:00:00Z``，则返回 ``None``。``None`` 表示"不要回收"
    ——调用方会放过该容器。
    """
    try:
        result = subprocess.run(
            [docker_exe, "inspect", "--format", "{{.State.FinishedAt}}", container_id],
            capture_output=True, text=True, timeout=10, check=False,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug("orphan reaper docker inspect %s failed: %s", container_id[:12], e)
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw or raw.startswith("0001-01-01"):
        return None
    # Docker 发出带纳秒的 RFC3339（例如 "2026-05-28T13:45:00.123456789Z"）。
    # Python 的 fromisoformat 处理微秒但不处理纳秒；这里截断。
    import re as _re
    raw = _re.sub(r"(\.\d{6})\d+", r"\1", raw)
    raw = raw.replace("Z", "+00:00")
    try:
        import datetime
        return datetime.datetime.fromisoformat(raw)
    except ValueError as e:
        logger.debug("could not parse FinishedAt %r for %s: %s", raw, container_id[:12], e)
        return None


def find_docker() -> Optional[str]:
    """定位 docker（或 podman）CLI 二进制文件。

    解析顺序：
    1. ``HERMES_DOCKER_BINARY`` 环境变量——显式覆盖（例如 ``/usr/bin/podman``）
    2. PATH 上的 ``docker``，通过 ``shutil.which``
    3. PATH 上的 ``podman``，通过 ``shutil.which``
    4. 常见的 macOS Docker Desktop 安装位置

    返回绝对路径，若两种运行时都找不到则返回 ``None``。
    """
    global _docker_executable
    if _docker_executable is not None:
        return _docker_executable

    # 1. 通过环境变量显式覆盖（例如不可变发行版上使用 Podman）
    override = os.getenv("HERMES_DOCKER_BINARY")
    if override and os.path.isfile(override) and os.access(override, os.X_OK):
        _docker_executable = override
        logger.info("Using HERMES_DOCKER_BINARY override: %s", override)
        return override

    # 2. PATH 上的 docker
    found = shutil.which("docker")
    if found:
        _docker_executable = found
        return found

    # 3. PATH 上的 podman（对我们的用例而言是即插即用兼容的）
    found = shutil.which("podman")
    if found:
        _docker_executable = found
        logger.info("Using podman as container runtime: %s", found)
        return found

    # 4. 常见的 macOS Docker Desktop 位置
    for path in _DOCKER_SEARCH_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            _docker_executable = path
            logger.info("Found docker at non-PATH location: %s", path)
            return path

    return None


# 应用于每个容器的安全标志。
# 容器本身就是安全边界（与主机隔离）。
# 我们丢弃所有能力，然后只加回所需的最小集：
#   DAC_OVERRIDE - root 可写入由主机用户拥有的 bind mount 目录
#   CHOWN/FOWNER - 包管理器（pip、npm、apt）需要设置文件属主
#   SETUID/SETGID - 镜像的 init 从 root 降权到 'hermes'
#       用户（通过打包镜像中的 `s6-setuidgid`，或用户镜像使用的
#       任何降权辅助工具），这需要这些能力。配合
#       `no-new-privileges`，降权后的进程仍无法提权回 root，因此
#       安全态势得以保持。当容器通过 --user 以非 root 用户启动时
#       完全省略，因为该模式下不需要降权。
# 阻止提权并限制 PID。
# /tmp 大小受限且 nosuid，但允许 exec（pip/npm 构建需要）。
_BASE_SECURITY_ARGS = [
    "--cap-drop", "ALL",
    "--cap-add", "DAC_OVERRIDE",
    "--cap-add", "CHOWN",
    "--cap-add", "FOWNER",
    "--security-opt", "no-new-privileges",
    "--pids-limit", "256",
    "--tmpfs", "/tmp:rw,nosuid,size=512m",
    "--tmpfs", "/var/tmp:rw,noexec,nosuid,size=256m",
]

# /run 从 _BASE_SECURITY_ARGS 中拆分出来，因为 s6-overlay 镜像需要
# 将其以 ``exec`` 挂载：s6 stage0 稍后会运行
# ``exec /run/s6/basedir/bin/init``，在 ``noexec`` 挂载上会以
# "Permission denied"（退出码 126）失败。对所有其他镜像我们保持
# 加固的 ``noexec`` 默认值。
_RUN_TMPFS_NOEXEC = "--tmpfs", "/run:rw,noexec,nosuid,size=64m"
_RUN_TMPFS_EXEC = "--tmpfs", "/run:rw,exec,nosuid,size=64m"

# 当容器以 root 启动且 init/entrypoint 必须降权（通过 `s6-setuidgid`、
# `gosu`、`su` 或类似工具）时所需的额外能力。
# 传入 --user 时跳过，因为容器已经以非特权身份启动，永远不需要切换。
_PRIVDROP_CAP_ARGS = [
    "--cap-add", "SETUID",
    "--cap-add", "SETGID",
]


def _build_security_args(run_as_host_user: bool, run_exec: bool = False) -> list[str]:
    """返回根据特权模式定制的 security/cap/tmpfs 参数。

    ``run_exec`` 会以 ``exec`` 而非加固的 ``noexec`` 默认值挂载
    ``/run``。这是 s6-overlay 镜像所必需的——其 ``/init`` 入口在
    启动期间 exec ``/run/s6/basedir/bin/init``；见
    ``_image_uses_init_entrypoint``。
    """
    run_tmpfs = list(_RUN_TMPFS_EXEC if run_exec else _RUN_TMPFS_NOEXEC)
    args = list(_BASE_SECURITY_ARGS) + run_tmpfs
    if run_as_host_user:
        return args
    return args + list(_PRIVDROP_CAP_ARGS)


def _image_uses_init_entrypoint(docker_exe: str, image: str) -> bool:
    """当 ``image`` 的入口是 s6-overlay 的 ``/init`` 时返回 True。

    此类镜像（例如任何基于 ``s6-overlay`` 构建的镜像，包括
    ``hermes-agent:latest``）已经提供了自己的 PID-1 init，并在
    stage0 启动期间执行 ``/run/s6/basedir/bin/init``。它们与 Docker
    的 ``--init``（两个竞争的 PID-1 init）以及 ``noexec`` 的 ``/run``
    挂载不兼容。检测是尽力而为的：任何 inspect 失败时我们都返回
    False 并保持加固的默认值。
    """
    try:
        result = subprocess.run(
            [docker_exe, "image", "inspect", image,
             "--format", "{{json .Config.Entrypoint}}"],
            capture_output=True,
            text=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.SubprocessError, OSError) as e:
        logger.debug("Docker: could not inspect entrypoint for %s: %s", image, e)
        return False
    if result.returncode != 0:
        # 镜像可能尚未拉取；运行时会拉取它。默认值对非 s6 镜像是
        # 安全的，因此不要在此阻塞。
        logger.debug(
            "Docker: image inspect for %s returned %d (stderr=%s)",
            image, result.returncode, result.stderr.strip(),
        )
        return False
    raw = (result.stdout or "").strip()
    if not raw or raw == "null":
        return False
    try:
        entrypoint = json.loads(raw)
    except (ValueError, TypeError):
        return False
    if isinstance(entrypoint, str):
        entrypoint = [entrypoint]
    if not isinstance(entrypoint, list) or not entrypoint:
        return False
    first = str(entrypoint[0]).strip()
    return first in ("/init", "/package/admin/s6-overlay/command/init")


def _resolve_host_user_spec() -> Optional[str]:
    """返回当前主机用户的 ``<uid>:<gid>``，在不具备此含义的平台（例如
    没有 posix id 的 Windows）上返回 ``None``。

    我们刻意直接读取 ``os.getuid()`` / ``os.getgid()`` 而非通过
    ``getpass`` / ``pwd``，这样可保持低开销，且对无名 UID 永不抛出
    （nss 查询在沙箱化启动器中可能失败）。
    """
    get_uid = getattr(os, "getuid", None)
    get_gid = getattr(os, "getgid", None)
    if get_uid is None or get_gid is None:
        return None
    try:
        return f"{get_uid()}:{get_gid()}"
    except Exception:  # pragma: no cover - 防御性
        return None


_storage_opt_ok: Optional[bool] = None  # 跨实例缓存的结果


def _ensure_docker_available() -> None:
    """使用前对 docker CLI 是否可用的尽力检查。

    复用 ``find_docker()``，以便此预检与 Docker 后端的其余部分保持
    一致，包括已知的非 PATH Docker Desktop 位置。
    """
    docker_exe = find_docker()
    if not docker_exe:
        logger.error(
            "Docker backend selected but no docker executable was found in PATH "
            "or known install locations. Install Docker Desktop and ensure the "
            "CLI is available."
        )
        raise RuntimeError(
            "Docker executable not found in PATH or known install locations. "
            "Install Docker and ensure the 'docker' command is available."
        )

    try:
        result = subprocess.run(
            [docker_exe, "version"],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        logger.error(
            "Docker backend selected but the resolved docker executable '%s' could "
            "not be executed.",
            docker_exe,
            exc_info=True,
        )
        raise RuntimeError(
            "Docker executable could not be executed. Check your Docker installation."
        )
    except subprocess.TimeoutExpired:
        logger.error(
            "Docker backend selected but '%s version' timed out. "
            "The Docker daemon may not be running.",
            docker_exe,
            exc_info=True,
        )
        raise RuntimeError(
            "Docker daemon is not responding. Ensure Docker is running and try again."
        )
    except Exception:
        logger.error(
            "Unexpected error while checking Docker availability.",
            exc_info=True,
        )
        raise
    else:
        if result.returncode != 0:
            logger.error(
                "Docker backend selected but '%s version' failed "
                "(exit code %d, stderr=%s)",
                docker_exe,
                result.returncode,
                result.stderr.strip(),
            )
            raise RuntimeError(
                "Docker command is available but 'docker version' failed. "
                "Check your Docker installation."
            )


class DockerEnvironment(BaseEnvironment):
    """带资源限制和持久化的加固 Docker 容器执行。

    安全性：丢弃所有能力、禁止提权、PID 限制、scratch 目录使用大小
    受限的 tmpfs。容器本身就是安全边界——内部文件系统可写，这样 agent
    可以按需安装包（pip、npm、apt）。通过 tmpfs 或 bind mount 提供
    可写工作区。

    持久化：启用时，bind mount 在容器重启之间保留 /workspace 和 /root。
    """

    def __init__(
        self,
        image: str,
        cwd: str = "/root",
        timeout: int = 60,
        cpu: float = 0,
        memory: int = 0,
        disk: int = 0,
        persistent_filesystem: bool = False,
        task_id: str = "default",
        volumes: list = None,
        forward_env: list[str] | None = None,
        env: dict | None = None,
        network: bool = True,
        host_cwd: str = None,
        auto_mount_cwd: bool = False,
        run_as_host_user: bool = False,
        extra_args: list = None,
        persist_across_processes: bool = True,
    ):
        if cwd == "~":
            cwd = "/root"
        super().__init__(cwd=cwd, timeout=timeout)
        self._persistent = persistent_filesystem
        self._persist_across_processes = persist_across_processes
        self._task_id = task_id
        self._forward_env = _normalize_forward_env_names(forward_env)
        self._env = _normalize_env_dict(env)
        self._container_id: Optional[str] = None
        self._labels: dict[str, str] = {}
        self._image: str = ""
        self._container_name: str = ""
        self._image_uses_s6_init: bool = False
        self._all_run_args: list[str] = []
        logger.info(f"DockerEnvironment volumes: {volumes}")
        # 确保 volumes 是一个列表（config.yaml 可能格式有误）
        if volumes is not None and not isinstance(volumes, list):
            logger.warning(f"docker_volumes config is not a list: {volumes!r}")
            volumes = []

        # Docker 不可用时快速失败。
        _ensure_docker_available()

        # 构造资源限制参数
        resource_args = []
        if cpu > 0:
            resource_args.extend(["--cpus", str(cpu)])
        if memory > 0:
            resource_args.extend(["--memory", f"{memory}m"])
        if disk > 0 and sys.platform != "darwin":
            if self._storage_opt_supported():
                resource_args.extend(["--storage-opt", f"size={disk}m"])
            else:
                logger.warning(
                    "Docker storage driver does not support per-container disk limits "
                    "(requires overlay2 on XFS with pquota). Container will run without disk quota."
                )
        if not network:
            resource_args.append("--network=none")

        # 通过来自可配置主机目录（TERMINAL_SANDBOX_DIR，默认
        # ~/.hermes/sandboxes/）的 bind mount 实现持久化工作区。非持久化
        # 模式使用 tmpfs（临时、快速、清理时消失）。
        from tools.environments.base import get_sandbox_dir

        # 用户配置的卷挂载（来自 config.yaml 的 docker_volumes）
        volume_args = []
        workspace_explicitly_mounted = False
        for vol in (volumes or []):
            if not isinstance(vol, str):
                logger.warning(f"Docker volume entry is not a string: {vol!r}")
                continue
            vol = vol.strip()
            if not vol:
                continue
            if ":" in vol:
                volume_args.extend(["-v", vol])
                if ":/workspace" in vol:
                    workspace_explicitly_mounted = True
            else:
                logger.warning(f"Docker volume '{vol}' missing colon, skipping")

        host_cwd_abs = os.path.abspath(os.path.expanduser(host_cwd)) if host_cwd else ""
        bind_host_cwd = (
            auto_mount_cwd
            and bool(host_cwd_abs)
            and os.path.isdir(host_cwd_abs)
            and not workspace_explicitly_mounted
        )
        if auto_mount_cwd and host_cwd and not os.path.isdir(host_cwd_abs):
            logger.debug(f"Skipping docker cwd mount: host_cwd is not a valid directory: {host_cwd}")

        self._workspace_dir: Optional[str] = None
        self._home_dir: Optional[str] = None
        writable_args = []
        if self._persistent:
            sandbox = get_sandbox_dir() / "docker" / task_id
            self._home_dir = str(sandbox / "home")
            os.makedirs(self._home_dir, exist_ok=True)
            writable_args.extend([
                "-v", f"{self._home_dir}:/root",
            ])
            if not bind_host_cwd and not workspace_explicitly_mounted:
                self._workspace_dir = str(sandbox / "workspace")
                os.makedirs(self._workspace_dir, exist_ok=True)
                writable_args.extend([
                    "-v", f"{self._workspace_dir}:/workspace",
                ])
        else:
            if not bind_host_cwd and not workspace_explicitly_mounted:
                writable_args.extend([
                    "--tmpfs", "/workspace:rw,exec,size=10g",
                ])
            writable_args.extend([
                "--tmpfs", "/home:rw,exec,size=1g",
                "--tmpfs", "/root:rw,exec,size=1g",
            ])

        if bind_host_cwd:
            logger.info(f"Mounting configured host cwd to /workspace: {host_cwd_abs}")
            volume_args = ["-v", f"{host_cwd_abs}:/workspace", *volume_args]
        elif workspace_explicitly_mounted:
            logger.debug("Skipping docker cwd mount: /workspace already mounted by user config")

        # 挂载由 skill 声明的凭据文件（OAuth token 等）。
        # 只读，这样容器可以认证但不能修改主机凭据。
        try:
            from tools.credential_files import (
                get_credential_file_mounts,
                get_skills_directory_mount,
                get_cache_directory_mounts,
            )

            for mount_entry in get_credential_file_mounts():
                src = Path(mount_entry["host_path"])
                if src.is_dir():
                    # Docker-in-Docker：当主机上不存在源路径时，Docker 会
                    # 自动将其创建为目录。把一个目录挂载到一个文件目的
                    # 路径上会导致退出码 125。
                    logger.warning(
                        "Docker: skipping credential mount — source is a directory "
                        "(likely Docker-in-Docker auto-creation): %s",
                        src,
                    )
                    continue
                if not src.is_file():
                    logger.warning(
                        "Docker: skipping credential mount — source not found: %s", src,
                    )
                    continue
                volume_args.extend([
                    "-v",
                    f"{mount_entry['host_path']}:{mount_entry['container_path']}:ro",
                ])
                logger.info(
                    "Docker: mounting credential %s -> %s",
                    mount_entry["host_path"],
                    mount_entry["container_path"],
                )

            # 挂载 skill 目录（本地 + 外部），以便 skill 脚本/模板在
            # 容器内可用。
            for skills_mount in get_skills_directory_mount():
                src = Path(skills_mount["host_path"])
                if not src.is_dir():
                    logger.warning(
                        "Docker: skipping skills mount — source is not a directory: %s",
                        src,
                    )
                    continue
                volume_args.extend([
                    "-v",
                    f"{skills_mount['host_path']}:{skills_mount['container_path']}:ro",
                ])
                logger.info(
                    "Docker: mounting skills dir %s -> %s",
                    skills_mount["host_path"],
                    skills_mount["container_path"],
                )

            # 挂载主机侧缓存目录（文档、图片、音频、截图），以便 agent
            # 可以从容器内访问上传的文件和其他缓存的媒体。只读——
            # 容器读取这些内容，但由主机网关管理写入。
            for cache_mount in get_cache_directory_mounts():
                src = Path(cache_mount["host_path"])
                if not src.is_dir():
                    logger.warning(
                        "Docker: skipping cache mount — source is not a directory: %s",
                        src,
                    )
                    continue
                volume_args.extend([
                    "-v",
                    f"{cache_mount['host_path']}:{cache_mount['container_path']}:ro",
                ])
                logger.info(
                    "Docker: mounting cache dir %s -> %s",
                    cache_mount["host_path"],
                    cache_mount["container_path"],
                )
        except Exception as e:
            logger.debug("Docker: could not load credential file mounts: %s", e)

        # 显式环境变量（docker_env 配置）——在容器创建时设置，这样它们
        # 对所有进程（包括 entrypoint）可用。
        env_args = []
        for key in sorted(self._env):
            env_args.extend(["-e", f"{key}={self._env[key]}"])

        # 可选：以主机用户身份运行容器，这样写入 bind mount 目录
        # （/workspace、/root、docker_volumes 条目）的文件在主机上
        # 归该用户而非 root 所有。在没有 POSIX uid/gid 的平台
        # （例如原生 Windows Docker）上干净地跳过。
        user_args: list[str] = []
        if run_as_host_user:
            user_spec = _resolve_host_user_spec()
            if user_spec is not None:
                user_args = ["--user", user_spec]
                logger.info("Docker: running container as host user %s", user_spec)
            else:
                logger.warning(
                    "docker_run_as_host_user is enabled but this platform does "
                    "not expose POSIX uid/gid; container will start as its "
                    "image default user."
                )
                # 回退到完整能力集——没有 --user 时，镜像的 init 仍可能
                # 需要 s6-setuidgid/gosu/su 来降权。

        # 解析 docker 可执行文件一次，这样即便 /usr/local/bin 不在
        # PATH 中（在 macOS 网关/服务上很常见）也能工作。
        self._docker_exe = find_docker() or "docker"

        # s6-overlay 镜像（例如 hermes-agent:latest）已经使用 /init 作为
        # PID 1，并在启动期间 exec /run/s6/basedir/bin/init。对于这些镜像，
        # 我们必须 (a) 跳过 Docker 的 --init（两个竞争的 PID-1 init）并且
        # (b) 以 exec 而非 noexec 挂载 /run，否则 s6 stage0 会以退出码 126
        # "Permission denied" 死掉。这里检测一次；任何 inspect 失败都保持
        # 默认值。见 issue #34628。
        image_uses_s6_init = _image_uses_init_entrypoint(self._docker_exe, image)
        if image_uses_s6_init:
            logger.info(
                "Docker: image %s uses /init (s6-overlay) as entrypoint — "
                "skipping --init and mounting /run with exec.",
                image,
            )
        security_args = _build_security_args(
            run_as_host_user and bool(user_args),
            run_exec=image_uses_s6_init,
        )

        logger.info(f"Docker volume_args: {volume_args}")
        # 用户提供的额外 docker run 标志（config.yaml 中的 docker_extra_args）。
        # 最后追加，这样它们可以在需要时覆盖默认值。
        validated_extra = []
        for arg in (extra_args or []):
            if not isinstance(arg, str):
                logger.warning("Ignoring non-string docker_extra_args entry: %r", arg)
                continue
            validated_extra.append(arg)

        all_run_args = (
            security_args
            + user_args
            + writable_args
            + resource_args
            + volume_args
            + env_args
            + validated_extra
        )
        logger.info(f"Docker run_args: {all_run_args}")

        # 直接通过 `docker run -d` 启动容器。
        container_name = f"hermes-{uuid.uuid4().hex[:8]}"
        # 标签使 hermes 创建的容器可被识别：
        #   * 孤儿回收器（`hermes-agent=1` 用于全局清扫过滤）
        #   * 未来的跨进程复用（`hermes-task-id`、`hermes-profile`）
        #   * 运行 `docker ps --filter label=hermes-agent=1` 的运维人员
        # 值被限制在 _sanitize_label_value() 定义的安全字符集内；
        # 活跃的 Hermes profile 在容器启动时捕获，且在容器生命周期内
        # 永不改变。
        profile_name = _sanitize_label_value(_get_active_profile_name())
        task_label = _sanitize_label_value(task_id)
        label_args = [
            "--label", "hermes-agent=1",
            "--label", f"hermes-task-id={task_label}",
            "--label", f"hermes-profile={profile_name}",
        ]
        # 保存参数，用于 "No such container" 恢复时的容器重建。
        self._image = image
        self._container_name = container_name
        self._image_uses_s6_init = image_uses_s6_init
        self._all_run_args = all_run_args

        self._labels = {
            "hermes-agent": "1",
            "hermes-task-id": task_label,
            "hermes-profile": profile_name,
        }

        # 跨进程容器复用（issue #20561——文档声称"一个跨会话共享的
        # 长生命周期容器"）。如果先前的 Hermes 进程已经为这个
        # (task_id, profile) 启动了一个容器且它仍然存在，就附加到它
        # 而非启动一个新的。这恢复了文档约定的契约；可通过
        # ``terminal.docker_persist_across_processes: false`` 退出。
        #
        # 复用仅按标签匹配——我们刻意不比较 image / 挂载 / 资源。
        # 需要在更改这些设置后获得全新容器的运维人员应设置
        # ``docker_persist_across_processes: false``（或对带标签的容器
        # 运行 ``docker rm -f``）以强制干净启动。
        reused = False
        if persist_across_processes:
            existing = self._find_reusable_container(task_label, profile_name)
            if existing is not None:
                container_id, state = existing
                self._container_id = container_id
                if state != "running":
                    try:
                        subprocess.run(
                            [self._docker_exe, "start", container_id],
                            capture_output=True,
                            text=True,
                            timeout=30,
                            check=True,
                            stdin=subprocess.DEVNULL,
                        )
                    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                        logger.warning(
                            "Failed to start existing container %s (state=%s): "
                            "%s — falling back to a fresh container.",
                            container_id[:12], state, e,
                        )
                        self._container_id = None
                if self._container_id:
                    logger.info(
                        "Reusing container %s (task=%s, profile=%s, prior state=%s)",
                        container_id[:12], task_label, profile_name, state,
                    )
                    reused = True

        if not reused:
            # tini/catatonit 作为 PID 1 回收僵尸子进程——但 s6-overlay
            # 镜像已经提供了自己的 /init PID 1，因此在那里加 --init 会
            # 创建两个竞争的 init 并破坏启动（#34628）。
            init_args = [] if image_uses_s6_init else ["--init"]
            run_cmd = [
                self._docker_exe, "run", "-d",
                *init_args,
                "--name", container_name,
                *label_args,
                "-w", cwd,
                *all_run_args,
                image,
                "sleep", "infinity",  # 无固定生命周期——空闲回收器负责清理
            ]
            logger.debug(f"Starting container: {' '.join(run_cmd)}")
            try:
                result = subprocess.run(
                    run_cmd,
                    capture_output=True,
                    text=True,
                    timeout=120,  # 镜像拉取可能耗时较长
                    check=True,
                    stdin=subprocess.DEVNULL,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                # Docker 可能在 `docker run` 启动失败之前就已创建了容器
                # 对象（例如守护进程未就绪时的退出码 125，或拉取中途
                # 超时）。那个孤儿会留在 "Created" 状态——仅针对 exited
                # 的孤儿回收器（reap_orphan_containers，status=exited）
                # 永远抓不到它，于是它会永久泄漏。在重新抛出之前按其
                # 已知名称移除它。见 #7439。
                logger.warning(
                    "docker run failed for %s, cleaning up orphaned container: %s",
                    container_name, e,
                )
                subprocess.run(
                    [self._docker_exe, "rm", "-f", container_name],
                    capture_output=True, timeout=10,
                    stdin=subprocess.DEVNULL,
                )
                raise
            self._container_id = result.stdout.strip()
            logger.info(f"Started container {container_name} ({self._container_id[:12]})")

        # 构造初始化时的环境变量转发参数（仅由 init_session 使用，
        # 用于把主机环境变量注入快照；后续命令从快照文件中获取它们）。
        self._init_env_args = self._build_init_env_args()

        # 在容器内初始化会话快照
        self.init_session()

    def _build_init_env_args(self) -> list[str]:
        """构造 -e KEY=VALUE 参数，用于把主机环境变量注入 init_session。

        仅在 init_session() 期间使用一次，以便 export -p 能把它们
        捕获进快照。后续的 execute() 调用不需要 -e 标志。
        """
        exec_env: dict[str, str] = dict(self._env)

        explicit_forward_keys = set(self._forward_env)
        passthrough_keys: set[str] = set()
        try:
            from tools.env_passthrough import get_all_passthrough
            passthrough_keys = set(get_all_passthrough())
        except Exception:
            pass
        # 显式的 docker_forward_env 条目是刻意的 opt-in，必须优先于
        # 通用的 Hermes 密钥黑名单。只有隐式透传的键才会被过滤。
        forward_keys = explicit_forward_keys | (passthrough_keys - _HERMES_PROVIDER_ENV_BLOCKLIST)
        hermes_env = _load_hermes_env_vars() if forward_keys else {}
        for key in sorted(forward_keys):
            value = os.getenv(key)
            if not value:
                value = hermes_env.get(key)
            if value:
                exec_env[key] = value

        args = []
        for key in sorted(exec_env):
            args.extend(["-e", f"{key}={exec_env[key]}"])
        return args

    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        """在 Docker 容器内派生一个 bash 进程。"""
        assert self._container_id, "Container not started"
        cmd = [self._docker_exe, "exec"]
        if stdin_data is not None:
            cmd.append("-i")

        # 仅在 init_session 期间注入 -e 环境变量参数。
        # 后续命令从快照中获取环境变量。
        if login:
            cmd.extend(self._init_env_args)

        cmd.extend([self._container_id])

        if login:
            cmd.extend(["bash", "-l", "-c", cmd_string])
        else:
            cmd.extend(["bash", "-c", cmd_string])

        return _popen_bash(cmd, stdin_data)

    # ------------------------------------------------------------------
    # "No such container" 恢复（issue #36266）
    # ------------------------------------------------------------------

    _NO_CONTAINER_PATTERNS = (
        "No such container",
        "is not running",
        "no such container",
    )

    def _is_container_gone(self, output: str) -> bool:
        """当输出表明容器已不存在时返回 True。"""
        return any(p in output for p in self._NO_CONTAINER_PATTERNS)

    def _recreate_container(self) -> bool:
        """在容器被带外移除后重建它。

        先尝试基于标签的复用；如果没找到现有容器，就用相同的镜像和
        运行参数启动一个新的。成功返回 True，重建失败返回 False
        （调用方应暴露原始错误）。
        """
        old_id = (self._container_id or "")[:12]
        logger.warning(
            "Container %s appears to be gone — attempting recovery", old_id,
        )
        self._container_id = None

        # 1. 尝试基于标签的复用（另一个进程可能已经重建了它）。
        task_label = self._labels.get("hermes-task-id", "")
        profile_label = self._labels.get("hermes-profile", "")
        existing = self._find_reusable_container(task_label, profile_label)
        if existing is not None:
            cid, state = existing
            if state == "running":
                self._container_id = cid
                logger.info("Recovery: reusing running container %s", cid[:12])
            else:
                try:
                    subprocess.run(
                        [self._docker_exe, "start", cid],
                        capture_output=True, text=True, timeout=30, check=True,
                        stdin=subprocess.DEVNULL,
                    )
                    self._container_id = cid
                    logger.info("Recovery: restarted container %s", cid[:12])
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                    logger.warning("Recovery: failed to start container %s: %s", cid[:12], e)

        # 2. 没有可复用容器——创建一个新的。
        if not self._container_id:
            if not self._image:
                logger.error("Recovery: no saved image name, cannot recreate container")
                return False
            try:
                import uuid as _uuid
                new_name = f"hermes-{_uuid.uuid4().hex[:8]}"
                init_args = [] if self._image_uses_s6_init else ["--init"]
                label_args = []
                for k, v in self._labels.items():
                    label_args.extend(["--label", f"{k}={v}"])
                run_cmd = [
                    self._docker_exe, "run", "-d",
                    *init_args,
                    "--name", new_name,
                    *label_args,
                    "-w", self.cwd,
                    *self._all_run_args,
                    self._image,
                    "sleep", "infinity",
                ]
                result = subprocess.run(
                    run_cmd, capture_output=True, text=True, timeout=120, check=True,
                    stdin=subprocess.DEVNULL,
                )
                self._container_id = result.stdout.strip()
                self._container_name = new_name
                logger.info(
                    "Recovery: created fresh container %s (%s)",
                    new_name, self._container_id[:12],
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
                logger.error("Recovery: failed to create new container: %s", e)
                return False

        # 3. 在（重建的）容器中重新初始化会话快照。
        try:
            self._snapshot_ready = False
            self.init_session()
        except Exception as e:
            logger.error("Recovery: init_session failed in new container: %s", e)
            return False

        logger.info("Recovery successful — new container %s", (self._container_id or "")[:12])
        return True

    def execute(self, command: str, cwd: str = "", **kwargs) -> dict:
        """执行一条命令，对已死容器自动恢复。

        如果容器被带外移除（空闲回收器、docker prune、OOM 杀死、
        守护进程重启），则检测到该错误并透明地重建容器后重试一次。
        """
        result = super().execute(command, cwd, **kwargs)
        if (
            result.get("returncode", 0) != 0
            and self._is_container_gone(result.get("output", ""))
            and self._persist_across_processes
        ):
            if self._recreate_container():
                result = super().execute(command, cwd, **kwargs)
        return result

    @staticmethod
    def _storage_opt_supported() -> bool:
        """检查 Docker 的存储驱动是否支持 --storage-opt size=。

        只有 XFS 上带 pquota 的 overlay2 才支持按容器磁盘配额。
        Ubuntu（以及大多数发行版）默认使用 ext4，此标志会报错。
        """
        global _storage_opt_ok
        if _storage_opt_ok is not None:
            return _storage_opt_ok
        try:
            docker = find_docker() or "docker"
            result = subprocess.run(
                [docker, "info", "--format", "{{.Driver}}"],
                capture_output=True, text=True, timeout=10,
                stdin=subprocess.DEVNULL,
            )
            driver = result.stdout.strip().lower()
            if driver != "overlay2":
                _storage_opt_ok = False
                return False
            # overlay2 仅在带 pquota 的 XFS 上支持 storage-opt。
            # 通过尝试一次近乎 dry 的运行来探测——这是最快的可靠检查。
            probe = subprocess.run(
                [docker, "create", "--storage-opt", "size=1m", "hello-world"],
                capture_output=True, text=True, timeout=15,
                stdin=subprocess.DEVNULL,
            )
            if probe.returncode == 0:
                # 清理创建的容器
                container_id = probe.stdout.strip()
                if container_id:
                    subprocess.run([docker, "rm", container_id],
                                   capture_output=True, timeout=5,
                                   stdin=subprocess.DEVNULL)
                _storage_opt_ok = True
            else:
                _storage_opt_ok = False
        except Exception:
            _storage_opt_ok = False
        logger.debug("Docker --storage-opt support: %s", _storage_opt_ok)
        return _storage_opt_ok

    def _find_reusable_container(self, task_label: str, profile_label: str) -> Optional[tuple[str, str]]:
        """查找一个已标注为该 (task, profile) 的现有容器。

        命中时返回 ``(container_id, state)``，未命中或任何失败
        （包括 ``docker ps`` 本身失败）时返回 ``None``。state 是
        Docker 通过 ``{{.State}}`` 报告的值之一——例如 ``running``、
        ``exited``、``created``、``paused``、``restarting``、
        ``dead``。由调用方决定该状态是否需要在复用前执行
        ``docker start``。

        仅限本类创建的、存储在 docker 中的标签集合；绝不匹配那些
        碰巧命名为 ``hermes-*`` 但由其他工具启动的容器。
        """
        try:
            result = subprocess.run(
                [
                    self._docker_exe, "ps", "-a",
                    "--filter", "label=hermes-agent=1",
                    "--filter", f"label=hermes-task-id={task_label}",
                    "--filter", f"label=hermes-profile={profile_label}",
                    "--format", "{{.ID}}\t{{.State}}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.debug("docker ps probe failed: %s — will start a fresh container", e)
            return None
        if result.returncode != 0:
            logger.debug(
                "docker ps probe returned %d: %s — will start a fresh container",
                result.returncode, result.stderr.strip(),
            )
            return None
        lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        if not lines:
            return None
        # 多个匹配并不常见（一个 (task, profile) 应只产生一个容器），
        # 但可能发生在先前 Hermes 进程在清理中途崩溃时。如果存在
        # running 的就优先选它；否则选第一个列出的。过期的重复项会
        # 由后续提交中的孤儿回收器处理；这里不对它们做英勇处理。
        running = None
        first = None
        for ln in lines:
            parts = ln.split("\t", 1)
            if len(parts) != 2:
                continue
            cid, state = parts[0], parts[1].lower()
            if first is None:
                first = (cid, state)
            if state == "running" and running is None:
                running = (cid, state)
        return running or first

    def cleanup(self, *, force_remove: bool = False):
        """根据持久化模式和 *force_remove* 拆除容器。

        持久化模式（``persist_across_processes=True``，默认）保持容器
        **运行中**不动。文档承诺"一个跨会话共享的长生命周期容器"，
        每次 Hermes 退出都停止它会破坏这一承诺：

        * 容器内的后台进程（``npm run dev``、watcher、长时间运行的
          pytest）在用户每次运行 ``/quit`` 时都会被杀掉。
        * 每次复用都需要 ``docker start`` + 等待容器恢复，给新会话的
          第一次工具调用增加 1–2 秒。
        * "一个长生命周期容器"与"一个碰巧共享状态的新容器"之间
          用户可见的差异恰恰在于：前者的进程存活，后者的会死。

        持久化模式下的资源回收位于 ``reap_orphan_containers()`` 路径中
        （见 issue #20561 提交 3）：如果没有 Hermes 进程在
        ``2 × lifetime_seconds`` 内触碰某个带标签容器，它会在下一次
        Hermes 启动时被 ``docker rm -f``。这覆盖了 SIGKILL / OOM /
        笔记本被合上无人看管的情况，而无需我们在每次优雅退出时停止
        容器。

        退出模式（``persist_across_processes=False``）仍在每次清理时
        执行 ``docker stop`` + ``docker rm -f``，为明确想要按进程隔离
        的用户提供 PR 之前的行为。

        ``force_remove=True`` 覆盖持久化模式并总是拆除容器
        （``docker stop`` + ``docker rm -f``）。这是 ``/reset``、
        ``cleanup_vm(task_id)`` 驱动的重置或任何希望下次
        ``DockerEnvironment(task_id=...)`` 时获得保证全新容器的调用方的
        显式拆除路径。当前没有调用方传入 ``force_remove=True``；
        该参数的存在是为了让显式拆除语义稍后可以接入而无需更改本方法的
        签名。

        清理在守护线程上运行，使用有界的 ``subprocess.run`` 调用
        （而非 PR #33645 之前那种有竞态的 ``Popen(... &)`` 模式）。
        ``tools/terminal_tool.py`` 中的 atexit 钩子会等待该线程最多
        15s 才让解释器退出，因此当我们确实触发清理时，
        ``docker stop`` / ``docker rm`` 能真正完成。
        """
        container_id = self._container_id
        if not container_id:
            # 如果分配过 bind mount 目录且我们不在持久化模式下
            # （持久化模式会保留它们），仍然删除这些目录。
            if not self._persistent:
                for d in (self._workspace_dir, self._home_dir):
                    if d:
                        shutil.rmtree(d, ignore_errors=True)
            return

        # 决定实际做什么。三种情况：
        #
        #   force_remove=True             → stop + rm（显式拆除）
        #   persist_across_processes=True → 无操作（保持容器运行）
        #   persist_across_processes=False → stop + rm（按进程隔离）
        #
        # 持久化模式的无操作是 issue-#20561 的契约：容器比 Hermes 进程
        # 活得长，其中的进程保持存活，且下次启动时复用是即时的。
        if force_remove:
            should_stop = True
            should_remove = True
        elif self._persist_across_processes:
            # 对容器无操作。丢弃进程内句柄，这样全新的 __init__ 会
            # 通过标签重新探测（并找到运行中的容器），而不是尝试复用
            # 一个过期的 Python 引用。
            self._container_id = None
            return
        else:
            should_stop = True
            should_remove = True

        # 在我们置空这些属性之前捕获工作线程所需的状态——
        # 工作线程可能比 ``self`` 活得更久。
        docker_exe = self._docker_exe
        log_id = container_id[:12]

        def _do_cleanup() -> None:
            if should_stop:
                try:
                    subprocess.run(
                        [docker_exe, "stop", "-t", "10", container_id],
                        capture_output=True, timeout=30,
                        stdin=subprocess.DEVNULL,
                    )
                except (subprocess.TimeoutExpired, OSError) as e:
                    logger.warning("docker stop %s timed out / failed: %s", log_id, e)
            if should_remove:
                try:
                    subprocess.run(
                        [docker_exe, "rm", "-f", container_id],
                        capture_output=True, timeout=30,
                        stdin=subprocess.DEVNULL,
                    )
                except (subprocess.TimeoutExpired, OSError) as e:
                    logger.warning("docker rm -f %s failed: %s", log_id, e)

        # 守护线程：不会阻塞解释器退出（atexit 迅速返回），但与旧的
        # ``Popen(... &)`` shell 技巧不同，Python 层的 join 语义让该线程
        # 在解释器仍存活时能真正运行完毕。terminal_tool.py 中的 atexit
        # 注册了 ``_atexit_cleanup``，它会等待未完成的清理最多约 60s，
        # 因此大多数退出都能干净地完成工作。
        import threading
        t = threading.Thread(target=_do_cleanup, daemon=True, name=f"hermes-cleanup-{log_id}")
        t.start()
        self._cleanup_thread = t
        self._container_id = None

        # bind mount 目录的拆除仅在我们确实移除了容器时才运行
        # （这些目录是容器的文件系统状态；在无容器的情况下保留它们
        # 会在磁盘上留下孤儿数据）。
        if should_remove and not self._persistent:
            for d in (self._workspace_dir, self._home_dir):
                if d:
                    shutil.rmtree(d, ignore_errors=True)

    def wait_for_cleanup(self, timeout: float = 30.0) -> bool:
        """阻塞最多 *timeout* 秒等待清理工作线程。

        线程完成（或未启动线程）时返回 ``True``，超时返回 ``False``。
        terminal_tool.py 中的 atexit 钩子在每个活跃环境上调用本方法，
        以便 docker stop/rm 在 Python 进程退出前真正完成——没有它，
        ``hermes /quit`` 会与解释器关闭赛跑并留下已停止的容器。
        """
        thread = getattr(self, "_cleanup_thread", None)
        if thread is None or not thread.is_alive():
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()
