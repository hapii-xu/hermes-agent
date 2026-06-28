"""Profile distribution — 通过 git 共享、打包的 Hermes profile。

Distribution 是以 git 仓库形式发布的 Hermes profile（也可从
本地目录安装以便开发）。一条命令从 git URL 安装，就地更新，
并保留本地 memories / sessions / credentials 不受影响。

与现有组件的关系：

* ``hermes profile export/import`` — 本机 profile 的本地备份/恢复。
  不是 distribution 格式。保持原样。
* ``hermes skills install <url>`` — 我们参照的 URL 安装模式，
  但粒度为 profile 级别。

子命令（均位于 ``hermes profile`` 下，而非独立的命令树）：

    hermes profile install <source> [--name N] [--alias] [--force] [--yes]
    hermes profile update  <name>  [--force-config] [--yes]
    hermes profile info    <name>

``<source>`` 为以下之一：

* git URL（``github.com/user/repo``、``https://github.com/...``、``git@...``、
  ``ssh://``、``git://``），可选 ``#<ref>`` 固定 tag / branch / commit SHA。
* 已包含 ``distribution.yaml`` 的本地目录 — 用于首次推送前的
  profile 开发阶段。

清单格式（``distribution.yaml`` 位于 profile 根目录）::

    name: telemetry
    version: 0.1.0
    description: "Compliance monitoring harness"
    hermes_requires: ">=0.12.0"
    author: "..."
    license: "..."
    env_requires:
      - name: OPENAI_API_KEY
        description: "OpenAI API key"
        required: true
      - name: GRAPHITI_MCP_URL
        description: "Memory graph URL"
        required: false
        default: "http://127.0.0.1:8000/sse"
    distribution_owned:      # 可选；有合理默认值
      - SOUL.md
      - skills/
      - cron/
      - mcp.json

更新语义：

* Distribution 拥有的路径（SOUL.md、mcp.json、skills/、cron/、
  distribution.yaml）从新源替换。
* ``config.yaml`` 属于 distribution，但更新时默认保留，除非
  传入 ``--force-config``（用户覆盖通常存放于此）。
* 用户拥有的路径（memories/、sessions/、state.db、auth.json、.env、
  logs/、workspace/、home/、plans/、*_cache/ 以及 ``local/`` 下的
  所有内容）永远不会被修改。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.skill_utils import is_excluded_skill_path


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

MANIFEST_FILENAME = "distribution.yaml"
ENV_TEMPLATE_FILENAME = ".env.template"
ENV_EXAMPLE_FILENAME = ".env.EXAMPLE"

# 默认 distribution 拥有的路径（相对于 profile 根目录）。作者可通过
# 清单中的 ``distribution_owned:`` 覆盖。config.yaml 属于 distribution
# 但在更新时特殊处理（见 _is_config_like）。
DEFAULT_DIST_OWNED: Tuple[str, ...] = (
    "SOUL.md",
    "config.yaml",
    "mcp.json",
    "skills",
    "cron",
    MANIFEST_FILENAME,
)

# 永不属于 distribution 的路径。这些归用户所有，更新时受保护。
# 必须与 ``profiles.py::_DEFAULT_EXPORT_EXCLUDE_ROOT`` 以及
# 用户自定义的 ``local/`` 约定保持一致。
USER_OWNED_EXCLUDE: frozenset = frozenset({
    # 凭证与运行时密钥
    "auth.json", ".env",
    # 数据库与运行时状态
    "state.db", "state.db-shm", "state.db-wal",
    "hermes_state.db", "response_store.db",
    "response_store.db-shm", "response_store.db-wal",
    "gateway.pid", "gateway_state.json", "processes.json",
    "auth.lock", "active_profile", ".update_check",
    "errors.log", ".hermes_history",
    # 用户数据
    "memories", "sessions", "logs", "plans", "workspace", "home",
    "image_cache", "audio_cache", "document_cache",
    "browser_screenshots", "checkpoints", "sandboxes",
    "backups", "cache",
    # 基础设施
    "hermes-agent", ".worktrees", "profiles", "bin", "node_modules",
    # 用户自定义命名空间
    "local",
})


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


class DistributionError(Exception):
    """distribution 安装/更新失败时抛出。"""


# ---------------------------------------------------------------------------
# 清单
# ---------------------------------------------------------------------------


@dataclass
class EnvRequirement:
    name: str
    description: str = ""
    required: bool = True
    default: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Any) -> "EnvRequirement":
        if not isinstance(data, dict):
            raise DistributionError(
                f"env_requires entry must be a mapping, got {type(data).__name__}"
            )
        name = str(data.get("name") or "").strip()
        if not name:
            raise DistributionError("env_requires entry missing 'name'")
        return cls(
            name=name,
            description=str(data.get("description") or ""),
            required=bool(data.get("required", True)),
            default=data.get("default"),
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"name": self.name, "description": self.description}
        if not self.required:
            out["required"] = False
        if self.default is not None:
            out["default"] = self.default
        return out


@dataclass
class DistributionManifest:
    name: str
    version: str = "0.1.0"
    description: str = ""
    hermes_requires: str = ""
    author: str = ""
    license: str = ""
    env_requires: List[EnvRequirement] = field(default_factory=list)
    distribution_owned: List[str] = field(default_factory=list)
    # 安装后跟踪 — 记录来源，以便 ``update`` 可以重新拉取。
    source: str = ""
    # ISO-8601 UTC 时间戳，在安装/更新时写入，以便 ``info`` 和
    # ``list`` 显示 distribution 何时落地到磁盘。对于仓库中自带的
    # 清单为空（作者不填写此字段）。
    installed_at: str = ""

    @classmethod
    def from_dict(cls, data: Any) -> "DistributionManifest":
        if not isinstance(data, dict):
            raise DistributionError(
                f"{MANIFEST_FILENAME} must be a mapping, got {type(data).__name__}"
            )
        name = str(data.get("name") or "").strip()
        if not name:
            raise DistributionError(f"{MANIFEST_FILENAME} missing 'name'")
        env_raw = data.get("env_requires") or []
        if not isinstance(env_raw, list):
            raise DistributionError("env_requires must be a list")
        env_requires = [EnvRequirement.from_dict(e) for e in env_raw]
        dist_owned_raw = data.get("distribution_owned") or []
        if dist_owned_raw and not isinstance(dist_owned_raw, list):
            raise DistributionError("distribution_owned must be a list")
        distribution_owned = [str(p).strip().strip("/") for p in dist_owned_raw if str(p).strip()]
        return cls(
            name=name,
            version=str(data.get("version") or "0.1.0"),
            description=str(data.get("description") or ""),
            hermes_requires=str(data.get("hermes_requires") or ""),
            author=str(data.get("author") or ""),
            license=str(data.get("license") or ""),
            env_requires=env_requires,
            distribution_owned=distribution_owned,
            source=str(data.get("source") or ""),
            installed_at=str(data.get("installed_at") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "version": self.version,
        }
        if self.description:
            out["description"] = self.description
        if self.hermes_requires:
            out["hermes_requires"] = self.hermes_requires
        if self.author:
            out["author"] = self.author
        if self.license:
            out["license"] = self.license
        if self.env_requires:
            out["env_requires"] = [e.to_dict() for e in self.env_requires]
        if self.distribution_owned:
            out["distribution_owned"] = self.distribution_owned
        if self.source:
            out["source"] = self.source
        if self.installed_at:
            out["installed_at"] = self.installed_at
        return out

    def owned_paths(self) -> List[str]:
        """解析哪些路径属于 distribution 所有。"""
        if self.distribution_owned:
            return list(self.distribution_owned)
        return list(DEFAULT_DIST_OWNED)


def _load_yaml(text: str) -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover — pyyaml 是硬依赖
        raise DistributionError("PyYAML is required for distribution manifests") from exc
    return yaml.safe_load(text)


def _dump_yaml(data: Any) -> str:
    import yaml

    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def read_manifest(profile_dir: Path) -> Optional[DistributionManifest]:
    """返回 *profile_dir* 的清单，如果不是 distribution 则返回 None。"""
    mf_path = profile_dir / MANIFEST_FILENAME
    if not mf_path.is_file():
        return None
    try:
        data = _load_yaml(mf_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise DistributionError(f"Failed to parse {mf_path}: {exc}") from exc
    return DistributionManifest.from_dict(data or {})


def write_manifest(profile_dir: Path, manifest: DistributionManifest) -> Path:
    mf_path = profile_dir / MANIFEST_FILENAME
    mf_path.write_text(_dump_yaml(manifest.to_dict()), encoding="utf-8")
    return mf_path


# ---------------------------------------------------------------------------
# 版本检查
# ---------------------------------------------------------------------------


_VERSION_OP_RE = re.compile(r"^\s*(>=|<=|==|!=|>|<)\s*(.+?)\s*$")


def _parse_semver(v: str) -> Tuple[int, int, int]:
    """极简 semver 解析器 — 仅 major.minor.patch。额外标签会被去除。"""
    s = str(v).strip().lstrip("v")
    # 去除任何预发布/构建元数据（例如 "0.12.0-rc1+abc"）
    s = re.split(r"[-+]", s, 1)[0]
    parts = s.split(".")
    while len(parts) < 3:
        parts.append("0")
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError as exc:
        raise DistributionError(f"Unparseable version: {v!r}") from exc


def check_hermes_requires(spec: str, current_version: str) -> None:
    """当 ``current_version`` 不满足 ``spec`` 时抛出 DistributionError。

    ``spec`` 接受单个比较符（``>=0.12.0``、``==0.12.0`` 等）。
    空或空白的 spec 为无操作 — 即无版本要求。
    """
    if not spec or not spec.strip():
        return
    m = _VERSION_OP_RE.match(spec)
    if not m:
        # 裸版本号 → 视为 ``>=``
        op, target = ">=", spec.strip()
    else:
        op, target = m.group(1), m.group(2)
    cur = _parse_semver(current_version)
    tgt = _parse_semver(target)
    ok = {
        ">=": cur >= tgt,
        "<=": cur <= tgt,
        "==": cur == tgt,
        "!=": cur != tgt,
        ">":  cur > tgt,
        "<":  cur < tgt,
    }[op]
    if not ok:
        raise DistributionError(
            f"This distribution requires Hermes {op}{target}, "
            f"but you have {current_version}."
        )


# ---------------------------------------------------------------------------
# 环境变量模板辅助
# ---------------------------------------------------------------------------


def _env_template_from_manifest(manifest: DistributionManifest) -> str:
    """从 env_requires 生成 ``.env.template`` 内容。"""
    lines = [
        "# Environment variables required by this Hermes distribution.",
        "# Copy to `.env` and fill in your own values before running.",
        "",
    ]
    for req in manifest.env_requires:
        if req.description:
            lines.append(f"# {req.description}")
        status = "required" if req.required else "optional"
        lines.append(f"# ({status})")
        default_val = req.default if req.default is not None else ""
        prefix = "" if req.required else "# "
        lines.append(f"{prefix}{req.name}={default_val}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# 源暂存 — git clone 或本地目录
# ---------------------------------------------------------------------------


def _looks_like_git_url(s: str) -> bool:
    s = s.strip()
    if s.endswith(".git"):
        return True
    if s.startswith(("git@", "ssh://", "git://")):
        return True
    if s.startswith(("http://", "https://")):
        # 任何 http(s) URL 都视为 git 仓库。我们不再接受
        # tar.gz URL — git 是唯一的远程传输方式。
        return True
    # github.com/user/repo 简写形式
    if re.match(r"^github\.com/[\w.-]+/[\w.-]+/?$", s):
        return True
    return False


def _git_clone(url: str, dest: Path) -> None:
    # 规范化 github.com/user/repo 简写形式
    if re.match(r"^github\.com/[\w.-]+/[\w.-]+/?$", url):
        url = f"https://{url.rstrip('/')}"
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", url, str(dest)],
            check=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise DistributionError("git is required for git-URL installs") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        raise DistributionError(f"git clone failed: {stderr.strip()}") from exc


def _stage_source(source: str, workdir: Path) -> Tuple[Path, str]:
    """将 *source* 解析为包含 distribution.yaml 的本地目录。

    返回 ``(staged_dir, provenance)``，其中 ``provenance`` 存储在
    已安装清单的 ``source:`` 字段中，以便 ``hermes profile update``
    可以从同一位置重新拉取。

    接受：
      * git URL（https / ssh / git@ / github.com 简写）— 克隆到
        临时目录；克隆后移除 ``.git``。
      * 已包含 ``distribution.yaml`` 的本地目录。
    """
    src_str = source.strip()

    # git URL
    if _looks_like_git_url(src_str):
        cloned = workdir / "clone"
        _git_clone(src_str, cloned)
        # 移除 .git 以保持暂存目录树整洁
        shutil.rmtree(cloned / ".git", ignore_errors=True)
        if not (cloned / MANIFEST_FILENAME).is_file():
            raise DistributionError(
                f"No {MANIFEST_FILENAME} at the root of {src_str!r}. "
                "This repository is not a Hermes profile distribution."
            )
        return cloned, src_str

    # 本地目录
    path_guess = Path(src_str).expanduser()
    if path_guess.is_dir():
        if not (path_guess / MANIFEST_FILENAME).is_file():
            raise DistributionError(
                f"No {MANIFEST_FILENAME} in {path_guess}. "
                "A local-directory source must contain a distribution.yaml at its root."
            )
        return path_guess.resolve(), str(path_guess.resolve())

    raise DistributionError(
        f"Cannot resolve distribution source: {source!r}. "
        "Expected a git URL (e.g. github.com/user/repo) or a local directory."
    )


def _reject_distribution_symlinks(staged: Path) -> None:
    """在读取或复制 distribution 文件之前拒绝符号链接。"""
    for entry in staged.rglob("*"):
        if not entry.is_symlink():
            continue
        try:
            rel = entry.relative_to(staged)
        except ValueError:
            rel = entry
        raise DistributionError(
            f"Profile distributions cannot contain symlinks: {rel}"
        )


# ---------------------------------------------------------------------------
# 安装
# ---------------------------------------------------------------------------


@dataclass
class InstallPlan:
    """安装将执行的操作摘要，用于用户确认。"""
    manifest: DistributionManifest
    staged_dir: Path
    provenance: str
    target_dir: Path
    existing: bool  # True if target profile already exists (update path)
    preserves_config: bool = True
    has_cron: bool = False
    has_skills: bool = False


def _has_cron_jobs(staged: Path) -> bool:
    cron_dir = staged / "cron"
    if not cron_dir.is_dir():
        return False
    for _ in cron_dir.rglob("*.json"):
        return True
    for _ in cron_dir.rglob("*.yaml"):
        return True
    return False


def _count_skills(staged: Path) -> int:
    skills_dir = staged / "skills"
    if not skills_dir.is_dir():
        return 0
    return sum(
        1 for p in skills_dir.rglob("SKILL.md") if not is_excluded_skill_path(p)
    )


def plan_install(
    source: str,
    workdir: Path,
    override_name: Optional[str] = None,
) -> InstallPlan:
    """暂存 *source* 并生成描述安装将执行操作的计划。"""
    from hermes_cli.profiles import (
        get_profile_dir,
        normalize_profile_name,
        validate_profile_name,
    )
    from hermes_cli import __version__ as hermes_version

    staged, provenance = _stage_source(source, workdir)
    _reject_distribution_symlinks(staged)
    manifest = read_manifest(staged)
    if manifest is None:
        raise DistributionError(
            f"No {MANIFEST_FILENAME} found at the distribution root — "
            "this source is not a Hermes distribution."
        )

    # 提前进行版本检查以便快速失败
    check_hermes_requires(manifest.hermes_requires, hermes_version)

    # 解析目标 profile 名称
    target_name = override_name or manifest.name
    canon = normalize_profile_name(target_name)
    validate_profile_name(canon)
    if canon == "default":
        raise DistributionError(
            "Cannot install a distribution as 'default' — that is the built-in "
            "root profile (~/.hermes).  Pass --name <name> to install under a "
            "new profile."
        )
    manifest.name = canon
    manifest.source = provenance
    # 在此处盖上时间戳，以便 plan_install() 的调用者（全新安装和
    # 更新）通过 _copy_dist_payload 传递新生成的时间戳。
    manifest.installed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    target_dir = get_profile_dir(canon)
    existing = target_dir.is_dir()
    has_cron = _has_cron_jobs(staged)
    skill_count = _count_skills(staged)

    return InstallPlan(
        manifest=manifest,
        staged_dir=staged,
        provenance=provenance,
        target_dir=target_dir,
        existing=existing,
        preserves_config=existing,
        has_cron=has_cron,
        has_skills=skill_count > 0,
    )


def _copy_dist_payload(
    staged: Path,
    target: Path,
    manifest: DistributionManifest,
    preserve_config: bool,
) -> None:
    """将 distribution 拥有的文件从 *staged* 复制到 *target*。

    用户拥有的路径永远不会被修改。``config.yaml`` 仅在
    ``preserve_config`` 为 False 时替换（全新安装或 ``--force-config``
    更新）。``.env.template`` 在目标中重命名为 ``.env.EXAMPLE``
    以避免遮蔽真实的 ``.env``。
    """
    target.mkdir(parents=True, exist_ok=True)

    for entry in staged.iterdir():
        name = entry.name

        if name in USER_OWNED_EXCLUDE:
            continue
        if name == ENV_TEMPLATE_FILENAME:
            shutil.copy2(entry, target / ENV_EXAMPLE_FILENAME)
            continue
        if name == "config.yaml" and preserve_config and (target / "config.yaml").exists():
            # 更新时保留用户的 config.yaml
            continue

        dest = target / name
        if entry.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            staged_resolved = staged.resolve()
            shutil.copytree(
                entry,
                dest,
                ignore=lambda d, names: (
                    [n for n in names if n in USER_OWNED_EXCLUDE]
                    if Path(d).resolve() == staged_resolved
                    else []
                ),
            )
        else:
            shutil.copy2(entry, dest)

    # 如果暂存目录未自带 .env.template，则从清单生成 .env.EXAMPLE
    if manifest.env_requires and not (target / ENV_EXAMPLE_FILENAME).exists():
        (target / ENV_EXAMPLE_FILENAME).write_text(
            _env_template_from_manifest(manifest), encoding="utf-8"
        )

    # 确保磁盘上的清单反映已解析的 name + source
    write_manifest(target, manifest)


def _bootstrap_user_dirs(target: Path) -> None:
    """创建全新 profile 所需的引导目录。"""
    for d in ("memories", "sessions", "skills", "skins", "logs",
              "plans", "workspace", "cron", "home"):
        (target / d).mkdir(parents=True, exist_ok=True)


def install_distribution(
    source: str,
    name: Optional[str] = None,
    force: bool = False,
    create_alias: bool = False,
) -> InstallPlan:
    """从 *source* 安装 distribution 到新 profile。

    返回已解析的 :class:`InstallPlan`。如果需要在调用前预览并
    提示用户，请先使用 :func:`plan_install`。
    """
    from hermes_cli.profiles import (
        check_alias_collision,
        create_wrapper_script,
    )

    with tempfile.TemporaryDirectory(prefix="hermes_dist_install_") as tmp:
        plan = plan_install(source, Path(tmp), override_name=name)

        if plan.existing and not force:
            raise DistributionError(
                f"Profile '{plan.manifest.name}' already exists at {plan.target_dir}. "
                "Use `hermes profile update` to upgrade in place, "
                "or pass --force to overwrite."
            )

        # 全新安装：config.yaml 来自 distribution。
        _bootstrap_user_dirs(plan.target_dir)
        _copy_dist_payload(
            plan.staged_dir,
            plan.target_dir,
            plan.manifest,
            preserve_config=False,
        )

        if create_alias:
            collision = check_alias_collision(plan.manifest.name)
            if collision is None:
                create_wrapper_script(plan.manifest.name)

        return plan


def update_distribution(
    profile_name: str,
    force_config: bool = False,
) -> InstallPlan:
    """重新拉取现有 profile 的 distribution 并应用更新。

    来源从已安装 profile 的 ``distribution.yaml`` 的 ``source:`` 字段读取。
    Distribution 拥有的文件会被覆盖；用户拥有的数据（memories、sessions、
    auth）永远不会被修改。``config.yaml`` 默认保留，除非 ``force_config``
    为 True。
    """
    from hermes_cli.profiles import (
        get_profile_dir,
        normalize_profile_name,
        validate_profile_name,
    )

    canon = normalize_profile_name(profile_name)
    validate_profile_name(canon)
    target = get_profile_dir(canon)
    if not target.is_dir():
        raise DistributionError(f"Profile '{canon}' does not exist.")

    existing_manifest = read_manifest(target)
    if existing_manifest is None:
        raise DistributionError(
            f"Profile '{canon}' is not a distribution (no {MANIFEST_FILENAME}). "
            "Only profiles installed via `hermes profile install` can be updated."
        )
    if not existing_manifest.source:
        raise DistributionError(
            f"Profile '{canon}' has no recorded source.  Re-install with "
            "`hermes profile install <source> --name {canon} --force`."
        )

    with tempfile.TemporaryDirectory(prefix="hermes_dist_update_") as tmp:
        plan = plan_install(
            existing_manifest.source,
            Path(tmp),
            override_name=canon,
        )
        plan.preserves_config = not force_config

        _copy_dist_payload(
            plan.staged_dir,
            plan.target_dir,
            plan.manifest,
            preserve_config=plan.preserves_config,
        )
        return plan


# ---------------------------------------------------------------------------
# 信息 — 渲染清单摘要
# ---------------------------------------------------------------------------


def describe_distribution(profile_name: str) -> Dict[str, Any]:
    """返回 profile 的 distribution 元数据的结构化视图。

    如果 profile 存在但没有清单则返回空字典。
    如果 profile 本身不存在则抛出 DistributionError。
    """
    from hermes_cli.profiles import (
        get_profile_dir,
        normalize_profile_name,
        validate_profile_name,
    )

    canon = normalize_profile_name(profile_name)
    validate_profile_name(canon)
    target = get_profile_dir(canon)
    if not target.is_dir():
        raise DistributionError(f"Profile '{canon}' does not exist.")
    manifest = read_manifest(target)
    if manifest is None:
        return {}
    return manifest.to_dict()
