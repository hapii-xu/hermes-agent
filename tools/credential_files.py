"""供远程终端后端使用的文件透传注册表。

远程后端（Docker、Modal、SSH）创建的沙箱里没有宿主机文件。本模块负责把凭证文件、
技能目录，以及宿主机侧的缓存目录（文档、图片、音频、截图）挂载或同步到这些沙箱里，
让 agent 能够访问它们。

**凭证与技能** —— 会话级注册表，数据来自技能声明
（``required_credential_files``）和用户配置（``terminal.credential_files``）。

**缓存目录** —— 网关缓存的上传文件、浏览器截图、TTS 音频以及处理过的图片。
以只读方式挂载，这样远程终端就能引用宿主机侧创建的文件（例如对上传的归档执行
``unzip``）。

远程后端在创建沙箱时以及每条命令之前（用于 Modal 上的重新同步）会调用
:func:`get_credential_file_mounts`、
:func:`get_skills_directory_mount` / :func:`iter_skills_files`，以及
:func:`get_cache_directory_mounts` / :func:`iter_cache_files`。
"""

from __future__ import annotations

import logging
import os
import posixpath
from contextvars import ContextVar
from pathlib import Path
from typing import Dict, List, Optional
from hermes_cli.config import cfg_get

logger = logging.getLogger(__name__)

# 会话级的待挂载凭证文件列表。
# 用 ContextVar 承载，以防止网关流水线中出现跨会话数据串流。
_registered_files_var: ContextVar[Dict[str, str]] = ContextVar("_registered_files")


def _get_registered() -> Dict[str, str]:
    """获取或创建当前上下文/会话对应的已注册凭证文件 dict。"""
    try:
        return _registered_files_var.get()
    except LookupError:
        val: Dict[str, str] = {}
        _registered_files_var.set(val)
        return val


# 基于配置的文件列表缓存（每个进程只加载一次）。
_config_files: List[Dict[str, str]] | None = None


def _resolve_hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def register_credential_file(
    relative_path: str,
    container_base: str = "/root/.hermes",
) -> bool:
    """注册一个凭证文件，以便挂载到远程沙箱中。

    *relative_path* 相对于 ``HERMES_HOME``（例如 ``google_token.json``）。
    若文件在宿主机上存在并已注册，则返回 True。

    安全性：拒绝绝对路径以及路径穿越序列（``..``）。解析后的宿主机路径必须仍位于
    HERMES_HOME 之内，这样恶意的技能才无法声明
    ``required_credential_files: ['../../.ssh/id_rsa']`` 并把敏感的宿主机文件
    窃取到容器沙箱中。
    """
    hermes_home = _resolve_hermes_home()

    # 拒绝绝对路径 —— 它们会完全绕过 HERMES_HOME 沙箱。
    if os.path.isabs(relative_path):
        logger.warning(
            "credential_files: rejected absolute path %r (must be relative to HERMES_HOME)",
            relative_path,
        )
        return False

    host_path = hermes_home / relative_path

    # 在做包含性检查之前先解析符号链接并归一化 ``..``，这样 ``../.ssh/id_rsa``
    # 之类的穿越就无法逃出 HERMES_HOME。
    from tools.path_security import validate_within_dir

    containment_error = validate_within_dir(host_path, hermes_home)
    if containment_error:
        logger.warning(
            "credential_files: rejected path traversal %r (%s)",
            relative_path,
            containment_error,
        )
        return False

    resolved = host_path.resolve()
    if not resolved.is_file():
        logger.debug("credential_files: skipping %s (not found)", resolved)
        return False

    container_path = f"{container_base.rstrip('/')}/{relative_path}"
    _get_registered()[container_path] = str(resolved)
    logger.debug("credential_files: registered %s -> %s", resolved, container_path)
    return True


def register_credential_files(
    entries: list,
    container_base: str = "/root/.hermes",
) -> List[str]:
    """从技能 frontmatter 条目批量注册多个凭证文件。

    每个条目要么是字符串（相对路径），要么是带 ``path`` 键的 dict。返回在宿主机上
    *未找到* 的相对路径列表（即缺失的文件）。
    """
    missing = []
    for entry in entries:
        if isinstance(entry, str):
            rel_path = entry.strip()
        elif isinstance(entry, dict):
            rel_path = (entry.get("path") or entry.get("name") or "").strip()
        else:
            continue
        if not rel_path:
            continue
        if not register_credential_file(rel_path, container_base):
            missing.append(rel_path)
    return missing


def _load_config_files() -> List[Dict[str, str]]:
    """从 config.yaml 加载 ``terminal.credential_files``（带缓存）。"""
    global _config_files
    if _config_files is not None:
        return _config_files

    result: List[Dict[str, str]] = []
    try:
        from hermes_cli.config import read_raw_config
        hermes_home = _resolve_hermes_home()
        cfg = read_raw_config()
        cred_files = cfg_get(cfg, "terminal", "credential_files")
        if isinstance(cred_files, list):
            from tools.path_security import validate_within_dir

            for item in cred_files:
                if isinstance(item, str) and item.strip():
                    rel = item.strip()
                    if os.path.isabs(rel):
                        logger.warning(
                            "credential_files: rejected absolute config path %r", rel,
                        )
                        continue
                    host_path = hermes_home / rel
                    containment_error = validate_within_dir(host_path, hermes_home)
                    if containment_error:
                        logger.warning(
                            "credential_files: rejected config path traversal %r (%s)",
                            rel, containment_error,
                        )
                        continue
                    resolved_path = host_path.resolve()
                    if resolved_path.is_file():
                        container_path = f"/root/.hermes/{rel}"
                        result.append({
                            "host_path": str(resolved_path),
                            "container_path": container_path,
                        })
    except Exception as e:
        logger.warning("Could not read terminal.credential_files from config: %s", e)

    _config_files = result
    return _config_files


def get_credential_file_mounts() -> List[Dict[str, str]]:
    """返回所有应当挂载到远程沙箱中的凭证文件。

    每个条目都带 ``host_path`` 和 ``container_path`` 键。
    合并了技能注册的文件和用户配置的文件。
    """
    mounts: Dict[str, str] = {}

    # 技能注册的文件
    for container_path, host_path in _get_registered().items():
        # 重新检查存在性（注册之后文件可能已被删除）
        if Path(host_path).is_file():
            mounts[container_path] = host_path

    # 基于配置的文件
    for entry in _load_config_files():
        cp = entry["container_path"]
        if cp not in mounts and Path(entry["host_path"]).is_file():
            mounts[cp] = entry["host_path"]

    return [
        {"host_path": hp, "container_path": cp}
        for cp, hp in mounts.items()
    ]


def get_skills_directory_mount(
    container_base: str = "/root/.hermes",
) -> list[Dict[str, str]]:
    """返回所有技能目录（本地 + 外部）的挂载信息。

    技能可能包含 ``scripts/``、``templates/`` 和 ``references/`` 子目录，
    agent 需要在远程沙箱内执行它们。

    **安全性：** 绑定挂载会跟随符号链接，因此技能树里一个恶意的符号链接可能把任意
    宿主机文件暴露给容器。当检测到符号链接时，本函数会在一个临时目录里创建一份
    经过净化的副本（仅含普通文件），并返回该路径。当不存在符号链接时（常见情况），
    直接返回原始目录，开销为零。

    返回一个由带 ``host_path`` 和 ``container_path`` 键的 dict 组成的列表。
    本地技能目录挂载到 ``<container_base>/skills``，外部目录挂载到
    ``<container_base>/external_skills/<index>``。
    """
    mounts = []
    hermes_home = _resolve_hermes_home()
    skills_dir = hermes_home / "skills"
    if skills_dir.is_dir():
        host_path = _safe_skills_path(skills_dir)
        mounts.append({
            "host_path": host_path,
            "container_path": f"{container_base.rstrip('/')}/skills",
        })

    # 挂载外部技能目录
    try:
        from agent.skill_utils import get_external_skills_dirs
        for idx, ext_dir in enumerate(get_external_skills_dirs()):
            if ext_dir.is_dir():
                host_path = _safe_skills_path(ext_dir)
                mounts.append({
                    "host_path": host_path,
                    "container_path": f"{container_base.rstrip('/')}/external_skills/{idx}",
                })
    except ImportError:
        pass

    return mounts


_safe_skills_tempdir: Path | None = None


def _safe_skills_path(skills_dir: Path) -> str:
    """若 *skills_dir* 不含符号链接则返回它本身，否则返回一份净化过的临时副本。"""
    global _safe_skills_tempdir

    symlinks = [p for p in skills_dir.rglob("*") if p.is_symlink()]
    if not symlinks:
        return str(skills_dir)

    for link in symlinks:
        logger.warning("credential_files: skipping symlink in skills dir: %s -> %s",
                       link, os.readlink(link))

    import atexit
    import shutil
    import tempfile

    # 跨调用复用同一个临时目录，避免不断堆积。
    if _safe_skills_tempdir and _safe_skills_tempdir.is_dir():
        shutil.rmtree(_safe_skills_tempdir, ignore_errors=True)

    safe_dir = Path(tempfile.mkdtemp(prefix="hermes-skills-safe-"))
    _safe_skills_tempdir = safe_dir

    for item in skills_dir.rglob("*"):
        if item.is_symlink():
            continue
        rel = item.relative_to(skills_dir)
        target = safe_dir / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(item), str(target))

    def _cleanup():
        if safe_dir.is_dir():
            shutil.rmtree(safe_dir, ignore_errors=True)

    atexit.register(_cleanup)
    logger.info("credential_files: created symlink-safe skills copy at %s", safe_dir)
    return str(safe_dir)


def iter_skills_files(
    container_base: str = "/root/.hermes",
) -> List[Dict[str, str]]:
    """逐个产出技能文件的 (host_path, container_path) 条目。

    同时包含本地技能目录和任何通过 skills.external_dirs 配置的外部目录。
    完全跳过符号链接。适用于逐个上传文件的后端（Daytona、Modal），而非挂载整个
    目录的后端。
    """
    result: List[Dict[str, str]] = []

    hermes_home = _resolve_hermes_home()
    skills_dir = hermes_home / "skills"
    if skills_dir.is_dir():
        container_root = f"{container_base.rstrip('/')}/skills"
        for item in skills_dir.rglob("*"):
            if item.is_symlink() or not item.is_file():
                continue
            rel = item.relative_to(skills_dir)
            result.append({
                "host_path": str(item),
                "container_path": f"{container_root}/{rel}",
            })

    # 包含外部技能目录
    try:
        from agent.skill_utils import get_external_skills_dirs
        for idx, ext_dir in enumerate(get_external_skills_dirs()):
            if not ext_dir.is_dir():
                continue
            container_root = f"{container_base.rstrip('/')}/external_skills/{idx}"
            for item in ext_dir.rglob("*"):
                if item.is_symlink() or not item.is_file():
                    continue
                rel = item.relative_to(ext_dir)
                result.append({
                    "host_path": str(item),
                    "container_path": f"{container_root}/{rel}",
                })
    except ImportError:
        pass

    return result


# ---------------------------------------------------------------------------
# 缓存目录挂载（文档、图片、音频、视频、截图）
# ---------------------------------------------------------------------------

# 应当镜像到远程后端的缓存子目录。
# 每个 tuple 为 (new_subpath, old_name)，与 hermes_constants.get_hermes_dir() 对应。
_CACHE_DIRS: list[tuple[str, str]] = [
    ("cache/documents", "document_cache"),
    ("cache/images", "image_cache"),
    ("cache/audio", "audio_cache"),
    ("cache/videos", "video_cache"),
    ("cache/screenshots", "browser_screenshots"),
]


def get_cache_directory_mounts(
    container_base: str = "/root/.hermes",
) -> List[Dict[str, str]]:
    """返回磁盘上每个已存在的缓存目录的挂载条目。

    供 Docker 用来创建绑定挂载。每个条目都带 ``host_path`` 和 ``container_path``
    键。宿主机路径通过 ``get_hermes_dir()`` 解析，以向后兼容旧的目录布局。
    """
    from hermes_constants import get_hermes_dir

    mounts: List[Dict[str, str]] = []
    for new_subpath, old_name in _CACHE_DIRS:
        host_dir = get_hermes_dir(new_subpath, old_name)
        if host_dir.is_dir():
            # 无论宿主机布局如何，始终映射到 *新* 的容器布局。
            container_path = f"{container_base.rstrip('/')}/{new_subpath}"
            mounts.append({
                "host_path": str(host_dir),
                "container_path": container_path,
            })
    return mounts


def map_cache_path_to_container(
    host_path: str,
    container_base: str = "/root/.hermes",
) -> Optional[str]:
    """把一个宿主机缓存路径映射到 *container_base* 下对应的挂载路径。

    当 *host_path* 位于某个自动挂载的缓存目录之下时，返回 POSIX 容器路径；否则
    返回 ``None``。与后端无关：由调用方决定采用哪个 ``container_base``
    （Docker 的 ``/root/.hermes``、SSH 的 ``<remote_home>/.hermes`` 等），以及是否
    需要这种转换。始终用 ``posixpath`` 拼接，因为容器/远程路径都是 POSIX 路径，
    与宿主机操作系统无关。
    """
    path = Path(host_path)
    for mount in get_cache_directory_mounts(container_base=container_base):
        host_dir = Path(mount["host_path"])
        try:
            rel = path.relative_to(host_dir)
        except ValueError:
            continue
        return posixpath.join(mount["container_path"], rel.as_posix())
    return None


def to_agent_visible_cache_path(
    host_path: str,
    container_base: str = "/root/.hermes",
) -> str:
    """把宿主机缓存路径转换为沙箱内对应的挂载路径。

    如果输入不在任何自动挂载的缓存目录之下，或者当前终端后端不需要路径转换
    （目前只有 Docker 需要），则原样返回输入。
    """
    # 目前只有 Docker 后端需要转换。其他后端（Modal、Daytona）使用不同的挂载语义，
    # 如有需要会另行处理。后端由 TERMINAL_ENV 标识
    # （与 tools/terminal_tool.py 在 _get_environment_config 中读取的是同一个环境变量）。
    if os.environ.get("TERMINAL_ENV", "local") != "docker":
        return host_path

    mapped = map_cache_path_to_container(host_path, container_base=container_base)
    return mapped if mapped is not None else host_path


def iter_cache_files(
    container_base: str = "/root/.hermes",
) -> List[Dict[str, str]]:
    """返回缓存文件逐个的 (host_path, container_path) 条目。

    供 Modal 用来逐个上传文件并在每条命令之前重新同步。跳过符号链接。
    容器路径使用新的 ``cache/<subdir>`` 布局。
    """
    from hermes_constants import get_hermes_dir

    result: List[Dict[str, str]] = []
    for new_subpath, old_name in _CACHE_DIRS:
        host_dir = get_hermes_dir(new_subpath, old_name)
        if not host_dir.is_dir():
            continue
        container_root = f"{container_base.rstrip('/')}/{new_subpath}"
        for item in host_dir.rglob("*"):
            if item.is_symlink() or not item.is_file():
                continue
            rel = item.relative_to(host_dir)
            result.append({
                "host_path": str(item),
                "container_path": f"{container_root}/{rel}",
            })
    return result


def clear_credential_files() -> None:
    """重置技能级注册表（例如在会话重置时）。"""
    _get_registered().clear()

