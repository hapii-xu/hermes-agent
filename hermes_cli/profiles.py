"""
多个隔离 Hermes 实例的 profile 管理。

每个 profile 是一个完全独立的 HERMES_HOME 目录，拥有自己的
config.yaml、.env、memory、sessions、skills、gateway、cron 和 logs。
Profile 默认位于 ``~/.hermes/profiles/<name>/``。

"default" profile 就是 ``~/.hermes`` 本身 — 向后兼容，
无需迁移。

用法::

    hermes profile create coder          # 全新 profile + 内置 skills
    hermes profile create coder --clone  # 同时复制 config、.env、SOUL.md、skills
    hermes profile create coder --clone-all  # 完整复制源 profile
    coder chat                           # 通过 wrapper 别名使用
    hermes -p coder chat                 # 或通过 flag
    hermes profile use coder             # 设为粘性默认值
    hermes profile delete coder          # 移除 profile + 别名 + 服务
"""

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import List, Optional, Tuple

from agent.skill_utils import is_excluded_skill_path

_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# 每个新 profile 中引导创建的目录
_PROFILE_DIRS = [
    "memories",
    "sessions",
    "skills",
    "skins",
    "logs",
    "plans",
    "workspace",
    "cron",
    # 工具子进程的向后兼容/Docker HOME。宿主机子进程默认保留
    # 用户的真实 HOME，以便常规 CLI 凭证仍然可见；
    # 容器仍使用此目录作为持久化 HOME 状态。
    # 参见 hermes_constants.get_subprocess_home()。
    "home",
]

# --clone 期间复制的文件（如果源中存在）
_CLONE_CONFIG_FILES = [
    "config.yaml",
    ".env",
    "SOUL.md",
]

# --clone 期间复制的子目录文件（路径相对于 profile 根目录）。
# Memory 文件是 agent 精心策划的身份的一部分 — 对克隆 profile 时
# 的连续性而言与 SOUL.md 同等重要。
_CLONE_SUBDIR_FILES = [
    "memories/MEMORY.md",
    "memories/USER.md",
]

# --clone-all 后剥离的运行时文件（不应延续）。
# 作为复制后步骤而非忽略过滤器保留，因为这些文件
# 在正常使用期间动态创建，复制时可能不存在。
_CLONE_ALL_STRIP: list[str] = [
    "gateway.pid",
    "gateway_state.json",
    "processes.json",
]

# 当源为默认 profile（``~/.hermes``）时从 --clone-all 中排除的基础设施
# 制品。命名 profile 在根目录下永远不会包含这些目录，因此排除仅
# 在条件满足时生效，以避免静默丢弃来自命名 profile 源的用户数据。
#
# 逐项说明：
#   hermes-agent  — git 仓库检出（约 84 MB 源码 + 约 3 GB venv）
#   .worktrees    — git worktree
#   profiles      — 同级命名 profile（绝不意图递归复制）
#   bin           — 已安装的二进制文件（tirith 等，约 10 MB），每台主机共享
#   node_modules  — npm 包（数百 MB）
#
# 参见下方 ``_DEFAULT_EXPORT_EXCLUDE_ROOT`` 了解更广泛的导出侧
# 排除列表（导出还会丢弃 logs / caches，因为归档是便携快照；
# clone-all 保留这些是因为克隆的 profile 需要立即继续使用）。
_CLONE_ALL_DEFAULT_EXCLUDE_ROOT: frozenset[str] = frozenset({
    "hermes-agent",
    ".worktrees",
    "profiles",
    "bin",
    "node_modules",
})

# 无论源 profile 如何，从 --clone-all 中排除的每个 profile 的历史
# 制品。新 profile 是全新的工作区 — 继承源 profile 的 session 历史、
# 备份归档或快速备份快照毫无用处（在克隆中恢复其中一个会复活
# 源 profile 的状态），且可能使副本膨胀数十 GB。与
# ``_CLONE_ALL_DEFAULT_EXCLUDE_ROOT`` 不同，此集合不依赖于默认
# profile：命名 profile 同样会积累相同的制品。
#
# 逐项说明：
#   state.db (+wal/shm) — SQLite session 存储（可达数 GB）
#   sessions            — 每个 session 的转录/数据目录
#   backups             — `hermes backup` 归档
#   state-snapshots     — 快速备份快照树
#   checkpoints         — session 检查点数据
_CLONE_ALL_HISTORY_EXCLUDE_ROOT: frozenset[str] = frozenset({
    "state.db",
    "state.db-wal",
    "state.db-shm",
    "sessions",
    "backups",
    "state-snapshots",
    "checkpoints",
})

# 由 `hermes profile create --no-skills` 写入的标记文件。当存在于
# profile 根目录时，seed_profile_skills() 的调用者（新建、`hermes
# update` 的全 profile 同步、web dashboard）会跳过该 profile 的内置
# skill 播种。用户仍可通过 `hermes skills install` 手动安装 skill，
# 或将 SKILL.md 文件放入 profile 的 skills/ 目录。
# 删除此标记文件即可重新启用。
NO_BUNDLED_SKILLS_MARKER = ".no-bundled-skills"


def has_bundled_skills_opt_out(profile_dir: Path) -> bool:
    """如果 profile 选择退出内置 skill 播种则返回 True。"""
    try:
        return (profile_dir / NO_BUNDLED_SKILLS_MARKER).exists()
    except OSError:
        return False


def _clone_all_copytree_ignore(source_dir: Path):
    """通过 --clone-all 克隆 profile 时排除基础设施制品。

    三个类别：
      1. ``_CLONE_ALL_HISTORY_EXCLUDE_ROOT`` 中的根级条目 — 属于
         源 profile 的 session 历史、备份和快照，绝不应带入全新
         克隆。适用于任何源。
      2. ``_CLONE_ALL_DEFAULT_EXCLUDE_ROOT`` 中的根级条目 — 仅
         默认 profile（``~/.hermes``）才包含的已知 Hermes 基础设施
         目录。仅在 ``source_dir`` 确实是默认 profile 时才生效，
         避免命名 profile 源的数据被静默丢弃。
      3. 任意深度的通用排除 — 过时或可重新生成的 Python 字节码
         缓存（``__pycache__``、``*.pyc``、``*.pyo``）以及运行时
         socket / 临时文件（``*.sock``、``*.tmp``）。

    导出侧的忽略（``_default_export_ignore``）使用相同的双层模式
    配合更广泛的 ``_DEFAULT_EXPORT_EXCLUDE_ROOT`` 集合，因为导出归档
    是便携快照而非活跃克隆。
    """
    source_resolved = source_dir.resolve()
    is_default_source = source_resolved == _get_default_hermes_home().resolve()

    def _ignore(directory: str, names: List[str]) -> List[str]:
        ignored: list[str] = []
        for entry in names:
            # 任意深度的通用排除。
            if (
                entry == "__pycache__"
                or entry.endswith((".pyc", ".pyo", ".sock", ".tmp"))
            ):
                ignored.append(entry)
                continue
            try:
                at_root = Path(directory).resolve() == source_resolved
            except (OSError, ValueError):
                # ``resolve()`` 在异常 FS 布局（断裂的符号链接、
                # 缺失的父目录）上可能失败。选择放行 — 宁可
                # 多复制也不要静默丢弃用户数据。
                at_root = False
            if at_root:
                # 历史制品：任何源 profile 均排除。
                if entry in _CLONE_ALL_HISTORY_EXCLUDE_ROOT:
                    ignored.append(entry)
                    continue
                # 基础设施：仅默认 profile 包含这些。
                if is_default_source and entry in _CLONE_ALL_DEFAULT_EXCLUDE_ROOT:
                    ignored.append(entry)
        return ignored

    return _ignore


# 导出默认 (~/.hermes) profile 时要排除的目录/文件。
# 默认 profile 包含命名 profile 所没有的基础设施（仓库检出、worktree、
# 数据库、缓存、二进制文件）。我们排除这些以使导出成为实际 profile
# 数据的便携、大小合理的归档。
_DEFAULT_EXPORT_EXCLUDE_ROOT = frozenset({
    # 基础设施
    "hermes-agent",         # 仓库检出（数 GB）
    ".worktrees",           # git worktree
    "profiles",             # 其他 profile — 绝不递归导出
    "bin",                  # 已安装的二进制文件（tirith 等）
    "node_modules",         # npm 包
    # 数据库与运行时状态
    "state.db", "state.db-shm", "state.db-wal",
    "hermes_state.db",
    "response_store.db", "response_store.db-shm", "response_store.db-wal",
    "gateway.pid", "gateway_state.json", "processes.json",
    "auth.json",            # API 密钥、OAuth token、凭证池
    ".env",                 # API 密钥（dotenv）
    "auth.lock", "active_profile", ".update_check",
    "errors.log",
    ".hermes_history",
    # 缓存（使用时重新生成）
    "image_cache", "audio_cache", "document_cache",
    "browser_screenshots", "checkpoints",
    "sandboxes",
    "logs",                 # gateway 日志
})

# 不能用作 profile 别名的名称
_RESERVED_NAMES = frozenset({
    "hermes", "default", "test", "tmp", "root", "sudo",
})

# 不能用作 profile 名称/别名的 Hermes 子命令
_HERMES_SUBCOMMANDS = frozenset({
    "chat", "model", "gateway", "setup", "whatsapp", "login", "logout",
    "status", "cron", "doctor", "dump", "config", "pairing", "skills", "tools",
    "mcp", "sessions", "insights", "version", "update", "uninstall",
    "profile", "plugins", "honcho", "acp",
})


# ---------------------------------------------------------------------------
# 路径辅助函数
# ---------------------------------------------------------------------------

def _get_profiles_root() -> Path:
    """返回存储命名 profile 的目录。

    锚定到 hermes 根目录，而非当前的 HERMES_HOME
    （后者本身可能就是一个 profile）。这确保 ``coder profile list``
    能看到所有 profile。

    在 Docker/自定义部署中，如果 HERMES_HOME 指向 ``~/.hermes`` 之外，
    profile 则位于 ``HERMES_HOME/profiles/`` 下，以便持久化到
    已挂载的卷。
    """
    return _get_default_hermes_home() / "profiles"


def _get_default_hermes_home() -> Path:
    """返回默认（pre-profile）HERMES_HOME 路径。

    在标准部署中为 ``~/.hermes``。
    在 Docker/自定义部署中，如果 HERMES_HOME 位于 ``~/.hermes`` 之外
    （例如 ``/opt/data``），则直接返回 HERMES_HOME。
    """
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def _get_active_profile_path() -> Path:
    """返回粘性 active_profile 文件的路径。"""
    return _get_default_hermes_home() / "active_profile"


def _get_wrapper_dir() -> Path:
    """返回 wrapper 脚本的目录。"""
    return Path.home() / ".local" / "bin"


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def normalize_profile_name(name: str) -> str:
    """返回在磁盘和 CLI ``-p`` 参数中使用的规范 profile id。

    命名 profile 以小写存储在 ``profiles/<id>/`` 下。特殊别名
    ``default`` 不区分大小写匹配（``Default`` → ``default``）。
    Dashboard 和工具可能传入标题大写显示标签；在验证、赋值和
    子进程启动之前进行规范化（参见 issue #18498）。
    """
    if not isinstance(name, str):
        name = str(name)
    stripped = name.strip()
    if not stripped:
        raise ValueError("profile name cannot be empty")
    if stripped.casefold() == "default":
        return "default"
    return stripped.lower()


def validate_profile_name(name: str) -> None:
    """如果 *name* 不是有效的 profile 标识符则抛出 ``ValueError``。

    按原样验证输入 — 严格小写匹配。接受混合大小写或标题大写输入
    的调用者（dashboard UI、CLI 参数）应先调用
    :func:`normalize_profile_name`。这种分离使 validate 对磁盘上
    目录名称的要求保持诚实，同时入口处的规范化处理 UX 灵活性
    （参见 #18498）。

    还会拒绝 :data:`_RESERVED_NAMES` 中的名称（``hermes``、``test``、
    ``tmp``、``root``、``sudo``），因为这些名称会在磁盘上产生令人困惑的
    冲突（``~/.hermes/`` 内的 ``hermes`` profile），或者在别名创建时
    无论如何都会被拒绝。``default`` 是特殊放行 — 它是内置根
    profile 的有效别名。
    """
    if name == "default":
        return  # special alias for ~/.hermes
    if not _PROFILE_ID_RE.match(name):
        raise ValueError(
            f"Invalid profile name {name!r}. Must match "
            f"[a-z0-9][a-z0-9_-]{{0,63}}"
        )
    if name in _RESERVED_NAMES:
        raise ValueError(
            f"Profile name {name!r} is reserved — it collides with either "
            f"the Hermes installation itself or a common system binary.  "
            f"Pick a different name."
        )


def get_profile_dir(name: str) -> Path:
    """将 profile 名称解析为其 HERMES_HOME 目录。"""
    canon = normalize_profile_name(name)
    if canon == "default":
        return _get_default_hermes_home()
    return _get_profiles_root() / canon


def profile_exists(name: str) -> bool:
    """检查 profile 目录是否存在。"""
    canon = normalize_profile_name(name)
    if canon == "default":
        return True
    return get_profile_dir(canon).is_dir()


# ---------------------------------------------------------------------------
# 别名 / wrapper 脚本管理
# ---------------------------------------------------------------------------

def check_alias_collision(name: str) -> Optional[str]:
    """返回人类可读的冲突消息，如果名称安全则返回 None。

    检查：保留名称、hermes 子命令、PATH 中已有的二进制文件。
    """
    canon = normalize_profile_name(name)
    if canon in _RESERVED_NAMES:
        return f"'{canon}' is a reserved name"
    if canon in _HERMES_SUBCOMMANDS:
        return f"'{canon}' conflicts with a hermes subcommand"

    # 检查 PATH 中已有的命令
    wrapper_dir = _get_wrapper_dir()
    is_windows = sys.platform == "win32"
    try:
        result = subprocess.run(
            ["where" if is_windows else "which", canon],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            existing_path = result.stdout.strip().splitlines()[0]
            # 允许覆盖我们自己的 wrapper
            expected = wrapper_dir / (f"{canon}.bat" if is_windows else canon)
            if existing_path == str(expected):
                try:
                    content = expected.read_text()
                    if "hermes -p" in content:
                        return None  # 是我们自己的 wrapper，可以安全覆盖
                except Exception:
                    pass
            return f"'{canon}' conflicts with an existing command ({existing_path})"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return None  # 安全


def _is_wrapper_dir_in_path() -> bool:
    """检查 ~/.local/bin 是否在 PATH 中。"""
    wrapper_dir = str(_get_wrapper_dir())
    return wrapper_dir in os.environ.get("PATH", "").split(os.pathsep)


def create_wrapper_script(name: str, target: Optional[str] = None) -> Optional[Path]:
    """在 ~/.local/bin/<name> 创建一个 shell wrapper 脚本。

    wrapper 文件以 ``name``（别名）命名。激活的 profile 是 ``target``
    （如果给定），否则是 ``name`` — 这允许自定义别名指向不同名称的
    profile 而无需事后重写。

    在 Windows 上，创建 ``.bat`` 文件而非 POSIX shell 脚本。
    返回创建的 wrapper 路径，如果创建失败则返回 None。
    """
    canon = normalize_profile_name(name)
    profile = normalize_profile_name(target) if target else canon
    wrapper_dir = _get_wrapper_dir()
    try:
        wrapper_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"⚠ Could not create {wrapper_dir}: {e}")
        return None

    is_windows = sys.platform == "win32"
    if is_windows:
        wrapper_path = wrapper_dir / f"{canon}.bat"
        try:
            wrapper_path.write_text(f"@echo off\r\nhermes -p {profile} %*\r\n")
            return wrapper_path
        except OSError as e:
            print(f"⚠ Could not create wrapper at {wrapper_path}: {e}")
            return None
    else:
        wrapper_path = wrapper_dir / canon
        try:
            hermes_exe = shutil.which("hermes") or "hermes"
            wrapper_path.write_text(f'#!/bin/sh\nexec {shlex.quote(hermes_exe)} -p {profile} "$@"\n')
            wrapper_path.chmod(wrapper_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            return wrapper_path
        except OSError as e:
            print(f"⚠ Could not create wrapper at {wrapper_path}: {e}")
            return None


def remove_wrapper_script(name: str) -> bool:
    """移除 profile 的 wrapper 脚本。移除成功返回 True。"""
    wrapper_dir = _get_wrapper_dir()
    canon = normalize_profile_name(name)
    is_windows = sys.platform == "win32"

    # 同时检查无扩展名路径（POSIX）和 .bat（Windows）
    candidates = [wrapper_dir / canon]
    if is_windows:
        candidates.insert(0, wrapper_dir / f"{canon}.bat")

    for wrapper_path in candidates:
        if wrapper_path.exists():
            try:
                # 删除前验证是我们自己的 wrapper
                content = wrapper_path.read_text()
                if "hermes -p" in content:
                    wrapper_path.unlink()
                    return True
            except Exception:
                pass
    return False


def _migrate_profile_config_if_outdated(profile_dir: Path) -> None:
    """将复制的 profile config.yaml 更新到当前 schema。

    Profile 创建可能克隆了一个早于 schema 跟踪（没有
    ``_config_version``）或 просто 比当前运行的 Hermes 更旧的 config 文件。
    如果我们不做处理，新 profile 首次被 desktop/doctor 查看时会显示
    可怕的 ``v0 → latest`` 警告，即使我们刚刚创建了该 profile。
    将正常迁移管道限定到新 profile 并保持非交互模式。
    """
    config_path = profile_dir / "config.yaml"
    if not config_path.exists():
        return

    try:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from hermes_cli.config import check_config_version, migrate_config

        token = set_hermes_home_override(str(profile_dir))
        try:
            current_ver, latest_ver = check_config_version()
            if current_ver < latest_ver:
                migrate_config(interactive=False, quiet=True)
        finally:
            reset_hermes_home_override(token)
    except Exception:
        # Profile 创建不应因为旧复制的 config 无法迁移而失败。
        # 下一次 `hermes doctor --fix` 仍可以在目标 profile 中
        # 显示详细错误。
        pass


def find_alias_for_profile(profile_name: str) -> Optional[str]:
    """返回激活 *profile_name* 的 wrapper 的别名名称，如果没有则返回 None。

    由 :func:`create_wrapper_script` 创建的 wrapper 是一个以别名命名的文件，
    其内容调用 ``hermes -p <profile>``。当别名等于 profile 名称时这是平凡的，
    但自定义别名（``hermes profile alias <profile> --name <custom>``）产生
    不同命名的文件 — 因此显示侧不能假设 ``wrapper == profile``，必须进行
    反向查找。

    自定义别名（name != profile）优先于 profile 同名 wrapper，
    以便 ``profile list``/``show`` 显示用户实际输入的命令。
    当多个别名匹配时，结果排序以确保输出确定性。
    """
    wrapper_dir = _get_wrapper_dir()
    if not wrapper_dir.is_dir():
        return None
    canon = normalize_profile_name(profile_name)
    is_windows = sys.platform == "win32"
    needle = f"hermes -p {canon}"

    custom: Optional[str] = None
    profile_named: Optional[str] = None
    for entry in sorted(wrapper_dir.iterdir()):
        if not entry.is_file():
            continue
        # 只有我们自己的 wrapper 才以别名命名，（在 Windows 上）带 .bat 后缀。
        if is_windows and entry.suffix != ".bat":
            continue
        if not is_windows and entry.suffix:
            continue
        try:
            content = entry.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if needle not in content:
            continue
        alias = entry.stem if is_windows else entry.name
        if alias == canon:
            profile_named = alias
        elif custom is None:
            custom = alias
    return custom if custom is not None else profile_named


# ---------------------------------------------------------------------------
# ProfileInfo
# ---------------------------------------------------------------------------

@dataclass
class ProfileInfo:
    """Summary information about a profile."""
    name: str
    path: Path
    is_default: bool
    gateway_running: bool
    model: Optional[str] = None
    provider: Optional[str] = None
    has_env: bool = False
    skill_count: int = 0
    alias_path: Optional[Path] = None
    # Custom alias name (the wrapper file name) when it differs from ``name``;
    # falls back to ``name`` when a profile-named wrapper exists. None if no
    # wrapper points at this profile. See ``find_alias_for_profile``.
    alias_name: Optional[str] = None
    # Distribution metadata (None if the profile wasn't installed from a distribution).
    distribution_name: Optional[str] = None
    distribution_version: Optional[str] = None
    distribution_source: Optional[str] = None
    # Free-form description (1-2 sentences) of what this profile is good
    # at. Persisted in ``<profile_dir>/profile.yaml``. Empty when the
    # user has not described the profile (legacy profiles, fresh
    # installs). Surfaced to the kanban decomposer so it can route work
    # to the right profile based on role rather than name alone.
    description: str = ""
    # When True, ``description`` was auto-generated by the LLM
    # describer and has not been confirmed by the user. The dashboard
    # surfaces a "review" badge in this case so the user can edit or
    # accept.
    description_auto: bool = False


def _read_distribution_meta(profile_dir: Path) -> tuple:
    """Return ``(name, version, source)`` from the profile's ``distribution.yaml``
    if present; ``(None, None, None)`` otherwise.

    Failures (missing file, bad YAML) are swallowed — a bad manifest should
    never break ``hermes profile list`` for an unrelated profile.
    """
    mf_path = profile_dir / "distribution.yaml"
    if not mf_path.is_file():
        return None, None, None
    try:
        import yaml
        with open(mf_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            return None, None, None
        return (
            data.get("name"),
            data.get("version"),
            data.get("source"),
        )
    except Exception:
        return None, None, None


def _read_config_model(profile_dir: Path) -> tuple:
    """Read model/provider from a profile's config.yaml. Returns (model, provider)."""
    config_path = profile_dir / "config.yaml"
    if not config_path.exists():
        return None, None
    try:
        import yaml
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        model_cfg = cfg.get("model", {})
        if isinstance(model_cfg, str):
            return model_cfg, None
        if isinstance(model_cfg, dict):
            return model_cfg.get("default") or model_cfg.get("model"), model_cfg.get("provider")
        return None, None
    except Exception:
        return None, None


def _check_gateway_running(profile_dir: Path) -> bool:
    """Check if a gateway is running for a given profile directory.

    Primary signal is the profile's ``gateway.pid`` (verified against the
    runtime lock).  That check fails closed whenever the lock isn't held by
    *this* reader — which is exactly the case for a dashboard process that is
    a separate s6 service from the gateway it's reporting on (Docker), or any
    launch-service-managed gateway that left a fresh ``gateway_state.json`` but
    no live PID file.  In those cases fall back to validating the PID recorded
    in the profile's own ``gateway_state.json`` against the live process table,
    mirroring the ``/api/status`` sidebar's liveness logic so the two surfaces
    agree.  Parameterized by ``profile_dir`` so it never mutates ``HERMES_HOME``.
    """
    try:
        from gateway.status import get_running_pid
        if (
            get_running_pid(profile_dir / "gateway.pid", cleanup_stale=False)
            is not None
        ):
            return True
    except Exception:
        pass
    try:
        from gateway.status import (
            get_runtime_status_running_pid,
            read_runtime_status,
        )
        runtime = read_runtime_status(profile_dir / "gateway_state.json")
        return get_runtime_status_running_pid(runtime, expected_home=profile_dir) is not None
    except Exception:
        return False


def _count_skills(profile_dir: Path) -> int:
    """Count installed skills in a profile."""
    skills_dir = profile_dir / "skills"
    if not skills_dir.is_dir():
        return 0
    count = 0
    for md in skills_dir.rglob("SKILL.md"):
        if is_excluded_skill_path(md):
            continue
        count += 1
    return count


# ---------------------------------------------------------------------------
# profile.yaml — per-profile metadata (description, role, etc.)
# ---------------------------------------------------------------------------
#
# We keep this file deliberately tiny and separate from the profile's
# ``config.yaml``. ``config.yaml`` is the user-facing Hermes config
# (~5000 lines of defaults); ``profile.yaml`` is metadata ABOUT the
# profile itself (its role, who described it). Mixing them makes both
# harder to read.
#
# Missing file -> empty defaults; never an error. The kanban decomposer
# tolerates empty descriptions and just falls back to the profile name.


def _profile_yaml_path(profile_dir: Path) -> Path:
    return profile_dir / "profile.yaml"


def read_profile_meta(profile_dir: Path) -> dict:
    """Read ``<profile_dir>/profile.yaml`` and return a dict.

    Returns ``{"description": "", "description_auto": False}`` when the
    file is missing or unreadable. Never raises — a corrupt
    profile.yaml on an unrelated profile must not break
    ``hermes profile list``.
    """
    path = _profile_yaml_path(profile_dir)
    if not path.is_file():
        return {"description": "", "description_auto": False}
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return {"description": "", "description_auto": False}
    if not isinstance(data, dict):
        return {"description": "", "description_auto": False}
    return {
        "description": str(data.get("description") or "").strip(),
        "description_auto": bool(data.get("description_auto", False)),
    }


def write_profile_meta(
    profile_dir: Path,
    *,
    description: Optional[str] = None,
    description_auto: Optional[bool] = None,
) -> None:
    """Update ``<profile_dir>/profile.yaml`` in place.

    Only the explicitly passed fields are overwritten; unspecified
    fields preserve existing values. Creates the file if missing.
    Profile directory itself must exist.
    """
    if not profile_dir.is_dir():
        raise FileNotFoundError(f"profile directory does not exist: {profile_dir}")
    import yaml
    path = _profile_yaml_path(profile_dir)
    existing: dict = {}
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            existing = {}
    if description is not None:
        existing["description"] = description.strip()
    if description_auto is not None:
        existing["description_auto"] = bool(description_auto)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(existing, f, sort_keys=False, default_flow_style=False)


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------

def list_profiles() -> List[ProfileInfo]:
    """Return info for all profiles, including the default."""
    profiles = []
    wrapper_dir = _get_wrapper_dir()

    # Default profile
    default_home = _get_default_hermes_home()
    if default_home.is_dir():
        model, provider = _read_config_model(default_home)
        dist_name, dist_version, dist_source = _read_distribution_meta(default_home)
        meta = read_profile_meta(default_home)
        profiles.append(ProfileInfo(
            name="default",
            path=default_home,
            is_default=True,
            gateway_running=_check_gateway_running(default_home),
            model=model,
            provider=provider,
            has_env=(default_home / ".env").exists(),
            skill_count=_count_skills(default_home),
            distribution_name=dist_name,
            distribution_version=dist_version,
            distribution_source=dist_source,
            description=meta.get("description", ""),
            description_auto=meta.get("description_auto", False),
        ))

    # Named profiles
    profiles_root = _get_profiles_root()
    if profiles_root.is_dir():
        for entry in sorted(profiles_root.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            if name == "default":
                continue  # already added as the built-in default above
            if not _PROFILE_ID_RE.match(name):
                continue
            model, provider = _read_config_model(entry)
            alias_name = find_alias_for_profile(name)
            if alias_name:
                is_windows = sys.platform == "win32"
                alias_path = wrapper_dir / (f"{alias_name}.bat" if is_windows else alias_name)
            else:
                alias_path = None
            dist_name, dist_version, dist_source = _read_distribution_meta(entry)
            meta = read_profile_meta(entry)
            profiles.append(ProfileInfo(
                name=name,
                path=entry,
                is_default=False,
                gateway_running=_check_gateway_running(entry),
                model=model,
                provider=provider,
                has_env=(entry / ".env").exists(),
                skill_count=_count_skills(entry),
                alias_path=alias_path if (alias_path and alias_path.exists()) else None,
                alias_name=alias_name,
                distribution_name=dist_name,
                distribution_version=dist_version,
                distribution_source=dist_source,
                description=meta.get("description", ""),
                description_auto=meta.get("description_auto", False),
            ))

    return profiles


def profiles_to_serve(multiplex: bool) -> List[Tuple[str, Path]]:
    """Return the ``(profile_name, hermes_home)`` pairs a gateway should serve.

    This is the single chokepoint for "which profiles does the inbound gateway
    handle" so later multiplexing phases never re-derive the set.

    - ``multiplex=False`` (default): returns exactly one entry for the *active*
      profile — byte-for-byte the single-profile behavior the gateway has
      always had. The name is ``"default"`` for the default profile or the
      active named profile's id.
    - ``multiplex=True``: returns the default profile plus every valid named
      profile under ``profiles/``, each paired with its own HERMES_HOME.

    Intentionally lightweight (a directory scan + name validation only): no
    per-profile config reads, gateway-running probes, or skill counts like
    :func:`list_profiles`. It runs on gateway startup and must stay cheap.

    The returned ``hermes_home`` is the path to pass to
    ``set_hermes_home_override`` when scoping a turn to that profile.
    """
    active = get_active_profile_name() or "default"
    if not multiplex:
        return [(active, get_profile_dir(active))]

    serve: List[Tuple[str, Path]] = [("default", _get_default_hermes_home())]

    profiles_root = _get_profiles_root()
    if profiles_root.is_dir():
        for entry in sorted(profiles_root.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            if name == "default":
                continue  # default is the built-in entry already added above
            if not _PROFILE_ID_RE.match(name):
                continue
            serve.append((name, entry))

    return serve


def create_profile(
    name: str,
    clone_from: Optional[str] = None,
    clone_all: bool = False,
    clone_config: bool = False,
    no_alias: bool = False,
    no_skills: bool = False,
    description: Optional[str] = None,
) -> Path:
    """Create a new profile directory.

    Parameters
    ----------
    name:
        Profile identifier (lowercase, alphanumeric, hyphens, underscores).
    clone_from:
        Source profile to clone from. If ``None`` and clone_config/clone_all
        is True, defaults to the currently active profile.
    clone_all:
        If True, do a full copytree of the source (all state).
    clone_config:
        If True, copy config files (config.yaml, .env, SOUL.md), installed
        skills, and selected profile identity files from the source profile.
    no_alias:
        If True, skip wrapper script creation.
    no_skills:
        If True, create an empty profile with no bundled skills, and write
        a marker file so ``hermes update`` skips re-seeding this profile's
        skills. Mutually exclusive with ``clone_config``/``clone_all`` (those
        explicitly copy skills from the source).

    Returns
    -------
    Path
        The newly created profile directory.
    """
    if no_skills and (clone_from is not None or clone_config or clone_all):
        raise ValueError(
            "--no-skills is mutually exclusive with --clone / --clone-from / --clone-all "
            "(cloning explicitly copies skills from the source profile)."
        )
    canon = normalize_profile_name(name)
    validate_profile_name(canon)

    if canon == "default":
        raise ValueError(
            "Cannot create a profile named 'default' — it is the built-in profile (~/.hermes)."
        )

    profile_dir = get_profile_dir(canon)
    if profile_dir.exists():
        raise FileExistsError(f"Profile '{canon}' already exists at {profile_dir}")

    # Resolve clone source
    source_dir = None
    if clone_from is not None or clone_all or clone_config:
        if clone_from is None:
            # Default: clone from active profile
            from hermes_constants import get_hermes_home
            source_dir = get_hermes_home()
        else:
            clone_from = normalize_profile_name(clone_from)
            validate_profile_name(clone_from)
            source_dir = get_profile_dir(clone_from)
        if not source_dir.is_dir():
            raise FileNotFoundError(
                f"Source profile '{clone_from or 'active'}' does not exist at {source_dir}"
            )

    if clone_all and source_dir:
        # Full copy of source profile (exclude sibling ~/.hermes/profiles/)
        shutil.copytree(
            source_dir,
            profile_dir,
            ignore=_clone_all_copytree_ignore(source_dir),
        )
        # Strip runtime files
        for stale in _CLONE_ALL_STRIP:
            (profile_dir / stale).unlink(missing_ok=True)
    else:
        # Bootstrap directory structure
        profile_dir.mkdir(parents=True, exist_ok=True)
        for subdir in _PROFILE_DIRS:
            (profile_dir / subdir).mkdir(parents=True, exist_ok=True)

        # Clone config files from source
        if source_dir is not None:
            for filename in _CLONE_CONFIG_FILES:
                src = source_dir / filename
                if src.exists():
                    dst = profile_dir / filename
                    shutil.copy2(src, dst)
                    # Tighten .env to owner-only after copy. shutil.copy2
                    # preserves source mode bits, but if the source's .env
                    # was loose (host umask 0o022 leaving 0o644), tighten
                    # explicitly so the clone doesn't inherit weak perms.
                    if filename == ".env":
                        try:
                            os.chmod(str(dst), 0o600)
                        except OSError:
                            pass

            # Clone installed skills from the source profile. The dashboard's
            # "clone from default" flow is expected to preserve both bundled
            # and user-installed skills so the new profile immediately has the
            # same agent capabilities as the source profile.
            source_skills = source_dir / "skills"
            if source_skills.is_dir():
                shutil.copytree(source_skills, profile_dir / "skills", dirs_exist_ok=True)

            # Clone memory and other subdirectory files
            for relpath in _CLONE_SUBDIR_FILES:
                src = source_dir / relpath
                if src.exists():
                    dst = profile_dir / relpath
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)

    # Seed an empty .env so the profile has its own credentials file from
    # day one. Without it, profile-scoped env writes (dashboard Channels /
    # Keys pages, `hermes -p <name> auth add`) had no file until first
    # write, and the profile silently inherited API keys from the shell
    # environment — users reasonably read that as "the new profile reads
    # the root .env". Skipped when --clone/--clone-all already copied one.
    env_path = profile_dir / ".env"
    if not env_path.exists():
        try:
            env_path.write_text(
                "# Per-profile secrets for this Hermes profile.\n"
                "# API keys and tokens set here override the shell environment.\n"
                "# Behavioral settings belong in config.yaml, not here.\n",
                encoding="utf-8",
            )
            os.chmod(str(env_path), 0o600)
        except OSError:
            pass  # best-effort — save_env_value creates the file on demand

    # Seed a default SOUL.md so the user has a file to customize immediately.
    # Skipped when the profile already has one (from --clone / --clone-all).
    soul_path = profile_dir / "SOUL.md"
    if not soul_path.exists():
        try:
            from hermes_cli.default_soul import DEFAULT_SOUL_MD
            soul_path.write_text(DEFAULT_SOUL_MD, encoding="utf-8")
        except Exception:
            pass  # best-effort — don't fail profile creation over this

    # Write the opt-out marker so seed_profile_skills() and `hermes update`'s
    # all-profile sync loop both skip this profile for bundled-skill seeding.
    if no_skills:
        try:
            (profile_dir / NO_BUNDLED_SKILLS_MARKER).write_text(
                "This profile opted out of bundled-skill seeding "
                "(`hermes profile create --no-skills`).\n"
                "Delete this file to re-enable sync on the next `hermes update`.\n",
                encoding="utf-8",
            )
        except OSError:
            pass  # best-effort — the feature still works via the empty skills/ dir

    # Cloned configs can be older than the running Hermes (or predate schema
    # tracking entirely). Migrate config-only clones immediately so
    # desktop/status surfaces don't warn that a just-created profile is
    # v0/outdated. Leave --clone-all snapshots byte-for-byte apart from the
    # explicit runtime/history stripping above.
    if not clone_all:
        _migrate_profile_config_if_outdated(profile_dir)

    # Persist description if the caller provided one. Done last so a
    # partial-create failure doesn't strand a description file in an
    # incomplete profile.
    if description and description.strip():
        try:
            write_profile_meta(
                profile_dir,
                description=description.strip(),
                description_auto=False,
            )
        except Exception:
            pass  # non-fatal — user can describe later with `hermes profile describe`

    # Phase 4: when running inside a container under s6, register the
    # new profile's gateway as a runtime s6 service so
    # `hermes -p <profile> gateway start` can supervise it via
    # `s6-svc -u` instead of spawning a bare process. On host (systemd
    # / launchd / windows) this is a no-op — the existing per-profile
    # unit-generation paths handle gateway lifecycle.
    _maybe_register_gateway_service(canon)

    return profile_dir


def seed_profile_skills(profile_dir: Path, quiet: bool = False) -> Optional[dict]:
    """Seed bundled skills into a profile via subprocess.

    Uses subprocess because sync_skills() caches HERMES_HOME at module level.
    Returns the sync result dict, or None on failure.

    Profiles that opted out of bundled skills (via ``hermes profile create
    --no-skills`` — which writes ``.no-bundled-skills`` to the profile root)
    are skipped and get an empty-result dict so callers can report
    "opted out" instead of "failed".
    """
    if has_bundled_skills_opt_out(profile_dir):
        return {
            "copied": [],
            "updated": [],
            "user_modified": [],
            "skipped_opt_out": True,
        }
    project_root = Path(__file__).parent.parent.resolve()
    try:
        result = subprocess.run(
            [sys.executable, "-c",
             "import json; from tools.skills_sync import sync_skills; "
             "r = sync_skills(quiet=True); print(json.dumps(r))"],
            env={**os.environ, "HERMES_HOME": str(profile_dir)},
            cwd=str(project_root),
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
        if not quiet:
            print(f"⚠ Skill seeding returned exit code {result.returncode}")
            if result.stderr.strip():
                print(f"  {result.stderr.strip()[:200]}")
        return None
    except subprocess.TimeoutExpired:
        if not quiet:
            print("⚠ Skill seeding timed out (60s)")
        return None
    except Exception as e:
        if not quiet:
            print(f"⚠ Skill seeding failed: {e}")
        return None


def backfill_profile_envs(quiet: bool = False) -> List[str]:
    """Give every named profile that predates per-profile ``.env`` files one.

    Profiles created before the dashboard/CLI started seeding a ``.env``
    (PR #44792) have none, so once the Channels/Keys endpoints became
    profile-scoped those profiles stopped inheriting the root install's
    credentials and showed everything as unconfigured. To avoid breaking
    anyone on update, copy the DEFAULT install's ``.env`` into each named
    profile that lacks one — that preserves the effective credentials those
    profiles were already running with (they previously read the root
    ``.env`` via the process environment). Users can then diverge per
    profile from there.

    Falls back to the placeholder header when the default install has no
    ``.env`` itself. Never overwrites an existing profile ``.env``.

    Returns the list of profile names that received a backfilled ``.env``.
    """
    backfilled: List[str] = []
    profiles_root = _get_profiles_root()
    if not profiles_root.is_dir():
        return backfilled

    default_env = _get_default_hermes_home() / ".env"

    for entry in sorted(profiles_root.iterdir()):
        if not entry.is_dir() or not _PROFILE_ID_RE.match(entry.name):
            continue
        if entry.name == "default":
            continue
        env_path = entry / ".env"
        if env_path.exists():
            continue
        try:
            if default_env.is_file():
                shutil.copy2(default_env, env_path)
            else:
                env_path.write_text(
                    "# Per-profile secrets for this Hermes profile.\n"
                    "# API keys and tokens set here override the shell environment.\n"
                    "# Behavioral settings belong in config.yaml, not here.\n",
                    encoding="utf-8",
                )
            os.chmod(str(env_path), 0o600)
            backfilled.append(entry.name)
        except OSError as e:
            if not quiet:
                print(f"⚠ Could not seed .env for profile '{entry.name}': {e}")

    return backfilled


def delete_profile(name: str, yes: bool = False) -> Path:
    """Delete a profile, its wrapper script, and its gateway service.

    Stops the gateway if running. Disables systemd/launchd service first
    to prevent auto-restart.

    Returns the path that was removed.
    """
    canon = normalize_profile_name(name)
    validate_profile_name(canon)

    if canon == "default":
        raise ValueError(
            "Cannot delete the default profile (~/.hermes).\n"
            "To remove everything, use: hermes uninstall"
        )

    profile_dir = get_profile_dir(canon)
    if not profile_dir.is_dir():
        raise FileNotFoundError(f"Profile '{canon}' does not exist.")

    # Show what will be deleted
    model, provider = _read_config_model(profile_dir)
    gw_running = _check_gateway_running(profile_dir)
    skill_count = _count_skills(profile_dir)
    dist_name, dist_version, dist_source = _read_distribution_meta(profile_dir)

    print(f"\nProfile: {canon}")
    print(f"Path:    {profile_dir}")
    if model:
        print(f"Model:   {model}" + (f" ({provider})" if provider else ""))
    if skill_count:
        print(f"Skills:  {skill_count}")
    if dist_name:
        print(f"Distribution: {dist_name}@{dist_version or '?'}")
        if dist_source:
            print(f"Installed from: {dist_source}")

    items = [
        "All config, API keys, memories, sessions, skills, cron jobs",
    ]

    # Check for service
    wrapper_path = _get_wrapper_dir() / canon
    has_wrapper = wrapper_path.exists()
    if has_wrapper:
        items.append(f"Command alias ({wrapper_path})")

    print(f"\nThis will permanently delete:")
    for item in items:
        print(f"  • {item}")
    if gw_running:
        print(f"  ⚠ Gateway is running — it will be stopped.")

    # Confirmation
    if not yes:
        print()
        try:
            confirm = input(f"Type '{canon}' to confirm: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return profile_dir
        if confirm != canon:
            print("Cancelled.")
            return profile_dir

    # 1. Disable service (prevents auto-restart)
    _cleanup_gateway_service(canon, profile_dir)
    # 1b. Phase 4: unregister the s6 service slot (container path).
    # On host this is a no-op; on container it removes
    # /run/service/gateway-<profile>/ so s6-supervise drops it.
    _maybe_unregister_gateway_service(canon)

    # 2. Stop running gateway
    if gw_running:
        _stop_gateway_process(profile_dir)

    # 3. Remove wrapper script
    if has_wrapper:
        if remove_wrapper_script(canon):
            print(f"✓ Removed {wrapper_path}")

    # 4. Remove profile directory
    remove_error: Exception | None = None
    try:
        def _make_writable(func, path, exc):
            """onexc/onerror handler: add +w on PermissionError so rmtree can proceed.

            Handles two cases on NixOS (and other systems with read-only
            copies from immutable stores):
            1. The path itself isn't writable (e.g. a file with mode 0444)
            2. The *parent* directory isn't writable (e.g. mode 0555)

            Compatible with both the ``onexc`` API (3.12+, receives an
            exception instance) and the ``onerror`` API (3.11-, receives
            ``sys.exc_info()`` tuple).
            """
            import stat as _stat

            # Normalise the two callback signatures:
            #   onexc(func, path, exc_instance)   — 3.12+
            #   onerror(func, path, exc_info_tuple) — 3.11
            if isinstance(exc, tuple):
                exc = exc[1]  # exc_info → actual exception object

            if isinstance(exc, PermissionError):
                # Make the path writable
                try:
                    os.chmod(path, os.stat(path).st_mode | _stat.S_IWUSR)
                except OSError:
                    pass
                # Also make the parent writable (needed for unlink/rmdir)
                parent = os.path.dirname(path)
                if parent:
                    try:
                        os.chmod(parent, os.stat(parent).st_mode | _stat.S_IWUSR)
                    except OSError:
                        pass
                func(path)
            else:
                raise

        # ``onexc`` was added in 3.12; fall back to ``onerror`` on 3.11.
        try:
            shutil.rmtree(profile_dir, onexc=_make_writable)
        except TypeError:
            shutil.rmtree(profile_dir, onerror=_make_writable)
        print(f"✓ Removed {profile_dir}")
    except Exception as e:
        print(f"⚠ Could not remove {profile_dir}: {e}")
        remove_error = e

    # 5. Clear active_profile if it pointed to this profile
    try:
        active = get_active_profile()
        if active == canon:
            set_active_profile("default")
            print("✓ Active profile reset to default")
    except Exception:
        pass

    if remove_error is not None:
        raise RuntimeError(f"Could not remove profile directory {profile_dir}: {remove_error}") from remove_error

    print(f"\nProfile '{canon}' deleted.")
    return profile_dir


def _maybe_register_gateway_service(profile_name: str) -> None:
    """Register a profile's gateway with s6 inside the container.

    No-op on host (systemd/launchd/windows) — those backends raise
    ``NotImplementedError`` on ``register_profile_gateway`` and the
    existing per-profile unit-generation paths handle lifecycle.

    Best-effort: any error (no backend detected, s6 not yet ready,
    etc.) is logged and swallowed so profile creation doesn't fail
    because the s6 supervision tree is in a weird state. The user
    can re-register manually later via the gateway start command,
    which goes through the same dispatch path.

    Port selection: each supervised profile gateway loads its own
    ``HERMES_HOME`` and binds the port resolved by ``gateway/config.py``
    from that profile's environment — ``API_SERVER_PORT`` (or
    ``platforms.api_server.extra.port`` in the profile's
    ``config.yaml``), defaulting to 8642. There is no ``[gateway] port``
    key and no Python-side allocator (PR #30136 review item I5 retired
    the SHA-256-derived range [9200, 9800) as dead code), so two
    profiles that both leave the port at its default will both try to
    bind 8642 — give each profile a distinct ``API_SERVER_PORT`` in its
    ``.env``.

    Host short-circuit: check ``detect_service_manager()`` first and
    return immediately if it isn't ``"s6"``. This keeps host
    (systemd/launchd/windows) profile creation completely silent —
    no ``get_service_manager()`` call, no exception path, no chance
    of the ``⚠ Could not register s6 gateway service`` warning ever
    rendering on a non-container machine. The earlier
    ``supports_runtime_registration()`` check still catches the case
    where detection somehow returns ``"s6"`` but the backend isn't
    actually the S6 one.
    """
    try:
        from hermes_cli.service_manager import detect_service_manager
        if detect_service_manager() != "s6":
            return  # host path — silent, no registration needed
        from hermes_cli.service_manager import get_service_manager
        mgr = get_service_manager()
    except RuntimeError:
        return  # no backend on this host — nothing to do
    except Exception:
        # Defensive: detect_service_manager failed for some other
        # reason. Stay silent on host rather than printing a confusing
        # s6 warning to users who have never touched the container.
        return
    if not mgr.supports_runtime_registration():
        return  # host backend; no-op
    try:
        mgr.register_profile_gateway(profile_name, start_now=False)
    except ValueError:
        # Already registered (e.g. the container-boot reconciler ran
        # first and brought up a stale slot). That's fine.
        pass
    except Exception as exc:
        # Don't fail profile create over a supervision-tree hiccup.
        print(f"⚠ Could not register s6 gateway service: {exc}")


def _maybe_unregister_gateway_service(profile_name: str) -> None:
    """Tear down a profile's s6 gateway service inside the container.

    No-op on host. Idempotent: absent services are silently skipped
    by ``unregister_profile_gateway``.

    Same host short-circuit as :func:`_maybe_register_gateway_service`
    — see that docstring.
    """
    try:
        from hermes_cli.service_manager import detect_service_manager
        if detect_service_manager() != "s6":
            return  # host path — silent
        from hermes_cli.service_manager import get_service_manager
        mgr = get_service_manager()
    except RuntimeError:
        return
    except Exception:
        return
    if not mgr.supports_runtime_registration():
        return
    try:
        mgr.unregister_profile_gateway(profile_name)
    except Exception as exc:
        print(f"⚠ Could not unregister s6 gateway service: {exc}")


def _cleanup_gateway_service(name: str, profile_dir: Path) -> None:
    """Disable and remove systemd/launchd service for a profile."""
    import platform as _platform

    # Derive service name for this profile
    # Temporarily set HERMES_HOME so _profile_suffix resolves correctly
    old_home = os.environ.get("HERMES_HOME")
    try:
        os.environ["HERMES_HOME"] = str(profile_dir)
        from hermes_cli.gateway import get_service_name, get_launchd_plist_path

        if _platform.system() == "Linux":
            svc_name = get_service_name()
            svc_file = Path.home() / ".config" / "systemd" / "user" / f"{svc_name}.service"
            if svc_file.exists():
                subprocess.run(
                    ["systemctl", "--user", "disable", svc_name],
                    capture_output=True, check=False, timeout=10,
                )
                subprocess.run(
                    ["systemctl", "--user", "stop", svc_name],
                    capture_output=True, check=False, timeout=10,
                )
                svc_file.unlink(missing_ok=True)
                subprocess.run(
                    ["systemctl", "--user", "daemon-reload"],
                    capture_output=True, check=False, timeout=10,
                )
                print(f"✓ Service {svc_name} removed")

        elif _platform.system() == "Darwin":
            plist_path = get_launchd_plist_path()
            if plist_path.exists():
                subprocess.run(
                    ["launchctl", "unload", str(plist_path)],
                    capture_output=True, check=False, timeout=10,
                )
                plist_path.unlink(missing_ok=True)
                print(f"✓ Launchd service removed")
    except Exception as e:
        print(f"⚠ Service cleanup: {e}")
    finally:
        if old_home is not None:
            os.environ["HERMES_HOME"] = old_home
        elif "HERMES_HOME" in os.environ:
            del os.environ["HERMES_HOME"]


def _stop_gateway_process(profile_dir: Path) -> None:
    """Stop a running gateway process via its PID file."""
    import time as _time

    pid_file = profile_dir / "gateway.pid"
    if not pid_file.exists():
        return

    try:
        raw = pid_file.read_text().strip()
        data = json.loads(raw) if raw.startswith("{") else {"pid": int(raw)}
        pid = int(data["pid"])
        # Route through terminate_pid so Windows uses the appropriate
        # primitive (taskkill / TerminateProcess) — raw os.kill with
        # _signal.SIGKILL raises AttributeError at import time on Windows,
        # and raw os.kill with SIGTERM doesn't cascade to child processes
        # the same way taskkill /T does.
        from gateway.status import terminate_pid as _terminate_pid
        from gateway.status import _pid_exists
        _terminate_pid(pid)  # graceful first
        # Wait up to 10s for graceful shutdown. On Windows, os.kill(pid, 0)
        # is NOT a no-op — use the handle-based existence check.
        for _ in range(20):
            _time.sleep(0.5)
            if not _pid_exists(pid):
                print(f"✓ Gateway stopped (PID {pid})")
                return
        # Force kill
        try:
            _terminate_pid(pid, force=True)
        except (ProcessLookupError, OSError):
            pass
        print(f"✓ Gateway force-stopped (PID {pid})")
    except (ProcessLookupError, PermissionError):
        print("✓ Gateway already stopped")
    except Exception as e:
        print(f"⚠ Could not stop gateway: {e}")


# ---------------------------------------------------------------------------
# Active profile (sticky default)
# ---------------------------------------------------------------------------

def get_active_profile() -> str:
    """Read the sticky active profile name.

    Returns ``"default"`` if no active_profile file exists or it's empty.
    """
    path = _get_active_profile_path()
    try:
        name = path.read_text().strip()
        if not name:
            return "default"
        return name
    except (FileNotFoundError, UnicodeDecodeError, OSError):
        return "default"


def set_active_profile(name: str) -> None:
    """Set the sticky active profile.

    Writes to ``~/.hermes/active_profile``. Use ``"default"`` to clear.
    """
    canon = normalize_profile_name(name)
    validate_profile_name(canon)
    if canon != "default" and not profile_exists(canon):
        raise FileNotFoundError(
            f"Profile '{canon}' does not exist. "
            f"Create it with: hermes profile create {canon}"
        )

    path = _get_active_profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if canon == "default":
        # Remove the file to indicate default
        path.unlink(missing_ok=True)
    else:
        # Atomic write
        tmp = path.with_suffix(".tmp")
        tmp.write_text(canon + "\n")
        tmp.replace(path)


def get_active_profile_name() -> str:
    """Infer the current profile name from HERMES_HOME.

    Returns ``"default"`` if HERMES_HOME is not set or points to ``~/.hermes``.
    Returns the profile name if HERMES_HOME points into ``~/.hermes/profiles/<name>``.
    Returns ``"custom"`` if HERMES_HOME is set to an unrecognized path.
    """
    from hermes_constants import get_hermes_home
    hermes_home = get_hermes_home()
    resolved = hermes_home.resolve()

    default_resolved = _get_default_hermes_home().resolve()
    if resolved == default_resolved:
        return "default"

    profiles_root = _get_profiles_root().resolve()
    try:
        rel = resolved.relative_to(profiles_root)
        parts = rel.parts
        if len(parts) == 1 and _PROFILE_ID_RE.match(parts[0]):
            return parts[0]
    except ValueError:
        pass

    return "custom"


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------

def _default_export_ignore(root_dir: Path):
    """Return an *ignore* callable for :func:`shutil.copytree`.

    At the root level it excludes everything in ``_DEFAULT_EXPORT_EXCLUDE_ROOT``.
    At all levels it excludes ``__pycache__``, sockets, and temp files.
    """

    def _ignore(directory: str, contents: list) -> set:
        ignored: set = set()
        for entry in contents:
            # Universal exclusions (any depth)
            if entry == "__pycache__" or entry.endswith((".sock", ".tmp")):
                ignored.add(entry)
            # npm lockfiles can appear at root
            elif entry in {"package.json", "package-lock.json"}:
                ignored.add(entry)
        # Root-level exclusions
        if Path(directory) == root_dir:
            ignored.update(c for c in contents if c in _DEFAULT_EXPORT_EXCLUDE_ROOT)
        return ignored

    return _ignore


def export_profile(name: str, output_path: str) -> Path:
    """Export a profile to a tar.gz archive.

    Returns the output file path.
    """
    import tempfile

    canon = normalize_profile_name(name)
    validate_profile_name(canon)
    profile_dir = get_profile_dir(canon)
    if not profile_dir.is_dir():
        raise FileNotFoundError(f"Profile '{canon}' does not exist.")

    output = Path(output_path)
    # shutil.make_archive wants the base name without extension
    base = str(output).removesuffix(".tar.gz").removesuffix(".tgz")

    if canon == "default":
        # The default profile IS ~/.hermes itself — its parent is ~/ and its
        # directory name is ".hermes", not "default".  We stage a clean copy
        # under a temp dir so the archive contains ``default/...``.
        with tempfile.TemporaryDirectory() as tmpdir:
            staged = Path(tmpdir) / "default"
            shutil.copytree(
                profile_dir,
                staged,
                ignore=_default_export_ignore(profile_dir),
            )
            result = shutil.make_archive(base, "gztar", tmpdir, "default")
            return Path(result)

    # Named profiles — stage a filtered copy to exclude credentials
    with tempfile.TemporaryDirectory() as tmpdir:
        staged = Path(tmpdir) / canon
        _CREDENTIAL_FILES = {"auth.json", ".env"}
        shutil.copytree(
            profile_dir,
            staged,
            ignore=lambda d, contents: _CREDENTIAL_FILES & set(contents),
        )
        result = shutil.make_archive(base, "gztar", tmpdir, canon)
        return Path(result)


def _normalize_profile_archive_parts(member_name: str) -> List[str]:
    """Return safe path parts for a profile archive member."""
    normalized_name = member_name.replace("\\", "/")
    posix_path = PurePosixPath(normalized_name)
    windows_path = PureWindowsPath(member_name)

    if (
        not normalized_name
        or posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
    ):
        raise ValueError(f"Unsafe archive member path: {member_name}")

    parts = [part for part in posix_path.parts if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"Unsafe archive member path: {member_name}")
    return parts


def _safe_extract_profile_archive(archive: Path, destination: Path) -> None:
    """Extract a profile archive without allowing path escapes or links."""
    import tarfile

    with tarfile.open(archive, "r:gz") as tf:
        for member in tf.getmembers():
            parts = _normalize_profile_archive_parts(member.name)
            target = destination.joinpath(*parts)

            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            if not member.isfile():
                raise ValueError(
                    f"Unsupported archive member type: {member.name}"
                )

            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tf.extractfile(member)
            if extracted is None:
                raise ValueError(f"Cannot read archive member: {member.name}")

            with extracted, open(target, "wb") as dst:
                shutil.copyfileobj(extracted, dst)

            try:
                os.chmod(target, member.mode & 0o777)
            except OSError:
                pass


def _inspect_profile_archive_roots(archive: Path) -> set[str]:
    """Return the archive's top-level directory names.

    Profile imports expect exactly one root directory. Inspecting the archive
    before extraction lets us stage the import safely instead of mutating a
    live profile tree first and reconciling names later.
    """
    import tarfile

    with tarfile.open(archive, "r:gz") as tf:
        top_dirs = {
            parts[0]
            for member in tf.getmembers()
            for parts in [_normalize_profile_archive_parts(member.name)]
            if len(parts) > 1 or member.isdir()
        }
        if not top_dirs:
            top_dirs = {
                _normalize_profile_archive_parts(member.name)[0]
                for member in tf.getmembers()
                if member.isdir()
            }
    return top_dirs


def import_profile(archive_path: str, name: Optional[str] = None) -> Path:
    """Import a profile from a tar.gz archive.

    If *name* is not given, infers it from the archive's top-level directory.
    Returns the imported profile directory.
    """
    import tempfile

    archive = Path(archive_path)
    if not archive.exists():
        raise FileNotFoundError(f"Archive not found: {archive}")

    top_dirs = _inspect_profile_archive_roots(archive)
    archive_root = top_dirs.pop() if len(top_dirs) == 1 else None
    inferred_name = name or archive_root
    if not inferred_name:
        raise ValueError(
            "Cannot determine profile name from archive. "
            "Specify it explicitly: hermes profile import <archive> --name <name>"
        )
    if archive_root is None:
        raise ValueError(
            "Profile archive must contain exactly one top-level directory."
        )

    # Archives exported from the default profile have "default/" as top-level
    # dir.  Importing as "default" would target ~/.hermes itself — disallow
    # that and guide the user toward a named profile.
    canon = normalize_profile_name(inferred_name)
    validate_profile_name(canon)
    if canon == "default":
        raise ValueError(
            "Cannot import as 'default' — that is the built-in root profile (~/.hermes). "
            "Specify a different name: hermes profile import <archive> --name <name>"
        )

    profile_dir = get_profile_dir(canon)
    if profile_dir.exists():
        raise FileExistsError(f"Profile '{canon}' already exists at {profile_dir}")

    profiles_root = _get_profiles_root()
    profiles_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="hermes_profile_import_") as tmpdir:
        staging_root = Path(tmpdir)
        _safe_extract_profile_archive(archive, staging_root)

        extracted = staging_root / archive_root
        if not extracted.is_dir():
            raise ValueError(
                f"Profile archive root is missing or invalid: {archive_root}"
            )

        final_source = extracted
        if archive_root != canon:
            final_source = staging_root / canon
            extracted.rename(final_source)

        shutil.move(str(final_source), str(profile_dir))

    return profile_dir


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------

def _migrate_honcho_profile_host(old_name: str, new_name: str, new_dir: Path) -> None:
    """Rename Honcho host blocks for a renamed profile without changing peers."""
    old_host = f"hermes_{old_name}"
    legacy_old_host = f"hermes.{old_name}"
    new_host = f"hermes_{new_name}"

    candidates = [
        new_dir / "honcho.json",
        _get_default_hermes_home() / "honcho.json",
        Path.home() / ".honcho" / "config.json",
    ]

    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        hosts = raw.get("hosts")
        if not isinstance(hosts, dict):
            continue
        source_host = old_host if old_host in hosts else legacy_old_host
        if source_host not in hosts:
            continue

        if new_host in hosts:
            print(f"⚠ Honcho host block not migrated: {new_host} already exists in {path}")
            continue

        block = hosts[source_host]
        if isinstance(block, dict) and "aiPeer" not in block:
            if source_host.startswith("hermes_"):
                bare = source_host.split("_", 1)[1]
            else:
                bare = source_host.split(".", 1)[1] if "." in source_host else source_host
            block["aiPeer"] = bare
        hosts[new_host] = hosts.pop(source_host)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            tmp.replace(path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            continue

        print(f"✓ Honcho host updated: {source_host} → {new_host}")


def rename_profile(old_name: str, new_name: str) -> Path:
    """Rename a profile: directory, wrapper script, service, active_profile.

    Returns the new profile directory.
    """
    old_canon = normalize_profile_name(old_name)
    new_canon = normalize_profile_name(new_name)
    validate_profile_name(old_canon)
    validate_profile_name(new_canon)

    if old_canon == "default":
        raise ValueError("Cannot rename the default profile.")
    if new_canon == "default":
        raise ValueError("Cannot rename to 'default' — it is reserved.")

    old_dir = get_profile_dir(old_canon)
    new_dir = get_profile_dir(new_canon)

    if not old_dir.is_dir():
        raise FileNotFoundError(f"Profile '{old_canon}' does not exist.")
    if new_dir.exists():
        raise FileExistsError(f"Profile '{new_canon}' already exists.")

    # 1. Stop gateway if running
    if _check_gateway_running(old_dir):
        _cleanup_gateway_service(old_canon, old_dir)
        _stop_gateway_process(old_dir)

    # 2. Rename directory
    old_dir.rename(new_dir)
    print(f"✓ Renamed {old_dir.name} → {new_dir.name}")

    # 3. Update profile-scoped Honcho host blocks, preserving aiPeer identity
    _migrate_honcho_profile_host(old_canon, new_canon, new_dir)

    # 4. Update wrapper script
    remove_wrapper_script(old_canon)
    collision = check_alias_collision(new_canon)
    if not collision:
        create_wrapper_script(new_canon)
        print(f"✓ Alias updated: {new_canon}")
    else:
        print(f"⚠ Cannot create alias '{new_canon}' — {collision}")

    # 5. Update active_profile if it pointed to old name
    try:
        if get_active_profile() == old_canon:
            set_active_profile(new_canon)
            print(f"✓ Active profile updated: {new_canon}")
    except Exception:
        pass

    return new_dir


# ---------------------------------------------------------------------------
# Profile env resolution (called from _apply_profile_override)
# ---------------------------------------------------------------------------

def resolve_profile_env(profile_name: str) -> str:
    """Resolve a profile name to a HERMES_HOME path string.

    Called early in the CLI entry point, before any hermes modules
    are imported, to set the HERMES_HOME environment variable.
    """
    canon = normalize_profile_name(profile_name)
    validate_profile_name(canon)
    profile_dir = get_profile_dir(canon)

    if canon != "default" and not profile_dir.is_dir():
        raise FileNotFoundError(
            f"Profile '{canon}' does not exist. "
            f"Create it with: hermes profile create {canon}"
        )

    return str(profile_dir)
