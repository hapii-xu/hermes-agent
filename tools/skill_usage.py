"""Curator 功能的技能使用遥测 + 出处跟踪。

在一个 sidecar JSON 文件（~/.hermes/skills/.usage.json）中按技能名为主键
跟踪每项技能的使用元数据。计数器由现有的技能工具（skill_view、
skill_manage）递增；curator 编排器读取派生的活动时间戳来决定生命周期
状态转换。

设计说明：
  - sidecar，而非 frontmatter。把运维遥测排除在用户编写的 SKILL.md 内容
    之外，避免对内置/hub 技能造成冲突压力。
  - 通过 tempfile + os.replace 做原子写入（与 .bundled_manifest 相同的模式）。
  - 所有计数器递增都是尽力而为：失败时在 DEBUG 级别记录日志并静默返回。
    一个损坏的 sidecar 永远不会破坏底层的工具调用。
  - 出处过滤：通过 skill_manage 创建的 curator 托管技能会被显式标记。
    内置/hub 安装的技能保持不可触碰，手动编写的技能不从位置推断。

生命周期状态：
    active    -> 默认
    stale     -> 超过 stale_after_days 未使用（配置）
    archived  -> 超过 archive_after_days 未使用（配置）；移动到 .archive/
    pinned    -> 退出自动转换（布尔标志，与状态正交）
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from hermes_constants import get_hermes_home
from agent.skill_utils import is_excluded_skill_path

logger = logging.getLogger(__name__)

# fcntl 仅限 Unix；在 Windows 上使用 msvcrt 做文件锁。
msvcrt = None
try:
    import fcntl
except ImportError:  # pragma: no cover - 平台相关回退
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        pass


STATE_ACTIVE = "active"
STATE_STALE = "stale"
STATE_ARCHIVED = "archived"
_VALID_STATES = {STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED}

# curator 绝不能归档或合并的承重型内置技能，无论
# ``curator.prune_builtins``、固定状态还是 LLM 判断如何。这些技能支撑着
# 已宣传的 UX 路径（例如 ``plan`` 驱动 ``/plan`` 斜杠命令流程，并在
# tips/docs/fresh-profile 种子中被引用）；静默归档其中之一会使其斜杠命令
# 变成 "Unknown command"，且对用户没有任何信号。保护以技能的 ``name``
# （frontmatter 的 ``name:``）为准，与本模块中使用的键一致。保持此列表
# 短小且有意为之——它不是 ``curator.prune_builtins: false`` 的替代品，
# 后者豁免所有内置技能。
PROTECTED_BUILTIN_SKILLS: Set[str] = {
    "plan",
}


def is_protected_builtin(skill_name: str) -> bool:
    """*skill_name* 是否是一个 curator 绝不触碰的承重型内置技能。

    受保护的内置技能在每条路径上都豁免于归档和合并：自动状态转换遍历、
    LLM 合并阶段（它们会从候选列表中被丢弃），以及直接的
    ``archive_skill`` 调用。
    """
    return skill_name in PROTECTED_BUILTIN_SKILLS


def _skills_dir() -> Path:
    return get_hermes_home() / "skills"


def _usage_file() -> Path:
    return _skills_dir() / ".usage.json"


@contextmanager
def _usage_file_lock():
    """跨进程串行化 .usage.json 的读-改-写周期。"""
    lock_path = _usage_file().with_suffix(".json.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None and msvcrt is None:
        yield
        return

    if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
        lock_path.write_text(" ", encoding="utf-8")

    fd = open(lock_path, "r+" if msvcrt else "a+", encoding="utf-8")
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
        yield
    finally:
        if fcntl:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except (OSError, IOError):
                pass
        elif msvcrt:
            try:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
            except (OSError, IOError):
                pass
        fd.close()


def _archive_dir() -> Path:
    return _skills_dir() / ".archive"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_timestamp(value: Any) -> Optional[datetime]:
    """防御性地解析一个 ISO 时间戳，用于活动时间比较。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def latest_activity_at(record: Dict[str, Any]) -> Optional[str]:
    """返回某条使用记录中最新的实际活动时间戳。

    "活动"指技能被使用、查看或修补。创建时间被刻意排除，使调用方仍能
    区分从未活跃过的技能；生命周期代码可以回退到 ``created_at`` 作为自己
    的锚点。
    """
    latest_dt: Optional[datetime] = None
    latest_raw: Optional[str] = None
    for key in ("last_used_at", "last_viewed_at", "last_patched_at"):
        raw = record.get(key)
        dt = _parse_iso_timestamp(raw)
        if dt is None:
            continue
        if latest_dt is None or dt > latest_dt:
            latest_dt = dt
            latest_raw = str(raw)
    return latest_raw


def activity_count(record: Dict[str, Any]) -> int:
    """返回跨使用/查看/修补事件的观测活动总数。"""
    total = 0
    for key in ("use_count", "view_count", "patch_count"):
        try:
            total += int(record.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return total


# ---------------------------------------------------------------------------
# 出处 —— 哪些技能是 agent 创建的（因此有资格被 curator 整理）
# ---------------------------------------------------------------------------

def _read_bundled_manifest_names() -> Set[str]:
    """返回从内置仓库播种的技能名集合。

    读取 ~/.hermes/skills/.bundled_manifest（格式：每行 "name:hash"）。
    文件缺失或不可读时返回空集合。
    """
    manifest = _skills_dir() / ".bundled_manifest"
    if not manifest.exists():
        return set()
    names: Set[str] = set()
    try:
        for line in manifest.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            name = line.split(":", 1)[0].strip()
            if name:
                names.add(name)
    except OSError as e:
        logger.debug("Failed to read bundled manifest: %s", e)
    return names


def _read_hub_installed_names() -> Set[str]:
    """返回通过 Skills Hub 安装的技能名集合。

    读取 ~/.hermes/skills/.hub/lock.json（见 tools/skills_hub.py :: HubLockFile）。
    """
    lock_path = _skills_dir() / ".hub" / "lock.json"
    if not lock_path.exists():
        return set()
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            installed = data.get("installed") or {}
            if isinstance(installed, dict):
                names = {str(k) for k in installed.keys()}
                skills_dir = _skills_dir()
                for entry in installed.values():
                    if not isinstance(entry, dict):
                        continue
                    install_path = entry.get("install_path")
                    if not isinstance(install_path, str) or not install_path.strip():
                        continue
                    skill_dir = Path(install_path)
                    if not skill_dir.is_absolute():
                        skill_dir = skills_dir / skill_dir
                    try:
                        resolved = skill_dir.resolve()
                        resolved.relative_to(skills_dir.resolve())
                    except (OSError, ValueError):
                        continue
                    skill_md = resolved / "SKILL.md"
                    if skill_md.exists():
                        names.add(_read_skill_name(skill_md, fallback=resolved.name))
                return names
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Failed to read hub lock file: %s", e)
    return set()


def _prune_builtins_enabled() -> bool:
    """内置技能是否有资格被 curator 修剪。

    从配置读取 ``curator.prune_builtins``（默认 True）。懒导入使本模块在
    没有 CLI 配置层时（例如在 update/sync 上下文中）也能被导入；任何失败
    时我们回退到默认值。针对大规模修剪的真正安全保障是 curator 的首次
    见到即种子，而不是这个标志——内置技能只在一个新的不活跃窗口之后才会
    被归档。
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        cur = cfg.get("curator") if isinstance(cfg, dict) else None
        if isinstance(cur, dict):
            return bool(cur.get("prune_builtins", True))
    except Exception as e:  # pragma: no cover —— 尽力而为的配置读取
        logger.debug("Failed to read curator.prune_builtins: %s", e)
    return True


def _suppressed_file() -> Path:
    return _skills_dir() / ".curator_suppressed"


def read_suppressed_names() -> Set[str]:
    """curator 修剪过的内置技能 —— 重新种子器必须让它们保持归档状态。

    ``~/.hermes/skills/.curator_suppressed`` 中每行一个技能名。这正是使
    修剪一个内置技能变得持久的原因：没有它，``hermes update`` 会在下次
    同步时重新复制该内置技能。
    """
    path = _suppressed_file()
    if not path.exists():
        return set()
    names: Set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                names.add(line)
    except OSError as e:
        logger.debug("Failed to read curator suppression list: %s", e)
    return names


def _write_suppressed_names(names: Set[str]) -> None:
    path = _suppressed_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = "\n".join(sorted(names)) + ("\n" if names else "")
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".curator_suppressed_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.debug("Failed to write curator suppression list: %s", e, exc_info=True)


def add_suppressed_name(skill_name: str) -> None:
    """记录某个内置技能已被修剪，使 sync 不会恢复它。"""
    if not skill_name:
        return
    names = read_suppressed_names()
    if skill_name not in names:
        names.add(skill_name)
        _write_suppressed_names(names)


def remove_suppressed_name(skill_name: str) -> None:
    """清除某个内置技能的抑制条目（例如恢复时）。"""
    if not skill_name:
        return
    names = read_suppressed_names()
    if skill_name in names:
        names.discard(skill_name)
        _write_suppressed_names(names)


def list_agent_created_skill_names() -> List[str]:
    """枚举 curator 可以管理的技能。

    始终包含 agent 编写的技能（那些通过 ``skill_manage(action="create")``
    在 ``.usage.json`` 中标记的）。当启用了 ``curator.prune_builtins`` 时，
    内置技能也会被包含，即使它们没有 agent 创建的使用记录——它们的不活跃
    时钟以首次见到为锚点（见 ``apply_automatic_transitions``）。Hub 安装
    的技能永不包含；手动编写的技能不从文件系统位置推断。
    """
    base = _skills_dir()
    if not base.exists():
        return []
    hub = _read_hub_installed_names()
    bundled = _read_bundled_manifest_names()
    prune_builtins = _prune_builtins_enabled()
    usage = load_usage()

    names: List[str] = []
    # 顶层的 SKILL.md 文件（扁平布局）以及嵌套的 category/skill/SKILL.md
    for skill_md in base.rglob("SKILL.md"):
        # 跳过 Hermes 元数据、VCS、虚拟环境/依赖以及缓存目录
        if is_excluded_skill_path(skill_md):
            continue
        try:
            skill_md.relative_to(base)
        except ValueError:
            continue
        name = _read_skill_name(skill_md, fallback=skill_md.parent.name)
        # Hub 安装的技能始终不可触碰。
        if name in hub:
            continue
        # 受保护的内置技能永不作为整理候选——豁免于自动转换遍历和
        # LLM 合并阶段。
        if is_protected_builtin(name):
            continue
        if name in bundled:
            # 内置技能仅在启用了修剪时才是候选。它们从不携带 curator 托管
            # 记录，因此跳过记录闸门。
            if not prune_builtins:
                continue
            names.append(name)
            continue
        # Agent 编写（或本地手动）的技能必须通过其记录 opt-in。
        if not _is_curator_managed_record(usage.get(name)):
            continue
        names.append(name)
    return sorted(set(names))


def list_archived_skill_names() -> List[str]:
    """枚举 ``~/.hermes/skills/.archive/`` 中的技能。

    归档布局是扁平的（``.archive/<skill>/``），由 ``archive_skill`` 设置，
    因此目录名就是技能名。供 ``hermes curator list-archived`` 使用，帮助
    用户把名称传给 ``hermes curator restore``。
    """
    archive_root = _archive_dir()
    if not archive_root.exists():
        return []
    return sorted({p.name for p in archive_root.iterdir() if p.is_dir()})


def _read_skill_name(skill_md: Path, fallback: str) -> str:
    """从 SKILL.md 的 YAML frontmatter 中解析 `name:` 字段。"""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return fallback
    in_frontmatter = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped == "---":
            if in_frontmatter:
                break
            in_frontmatter = True
            continue
        if in_frontmatter and stripped.startswith("name:"):
            value = stripped.split(":", 1)[1].strip().strip("\"'")
            if value:
                return value
    return fallback


def is_agent_created(skill_name: str) -> bool:
    """*skill_name* 是否既非内置也非 hub 安装。"""
    off_limits = _read_bundled_manifest_names() | _read_hub_installed_names()
    return skill_name not in off_limits


def is_hub_installed(skill_name: str) -> bool:
    """*skill_name* 是否通过 Skills Hub 安装。"""
    return skill_name in _read_hub_installed_names()


def is_bundled(skill_name: str) -> bool:
    """*skill_name* 是否从内置仓库技能播种而来。"""
    return skill_name in _read_bundled_manifest_names()


def is_curation_eligible(skill_name: str) -> bool:
    """curator 是否可以跟踪/归档 *skill_name*。

    Agent 创建的技能始终有资格。内置技能仅在启用
    ``curator.prune_builtins`` 时才有资格。Hub 安装的技能永不有资格——
    它们有一个外部的上游所有者。受保护的内置技能
    （``PROTECTED_BUILTIN_SKILLS``）无论任何标志都永无资格——它们支撑着
    承重型 UX，绝不能被归档或合并。
    """
    if is_protected_builtin(skill_name):
        return False
    if is_hub_installed(skill_name):
        return False
    if is_bundled(skill_name):
        return _prune_builtins_enabled()
    return True


def _is_curator_managed_record(record: Any) -> bool:
    """当某条使用记录把一个技能纳入 curator 管理时返回 True。"""
    if not isinstance(record, dict):
        return False
    return record.get("created_by") == "agent" or record.get("agent_created") is True


# ---------------------------------------------------------------------------
# Sidecar I/O
# ---------------------------------------------------------------------------

def _empty_record() -> Dict[str, Any]:
    return {
        "created_by": None,
        "use_count": 0,
        "view_count": 0,
        "last_used_at": None,
        "last_viewed_at": None,
        "patch_count": 0,
        "last_patched_at": None,
        "created_at": _now_iso(),
        "state": STATE_ACTIVE,
        "pinned": False,
        "archived_at": None,
    }


def load_usage() -> Dict[str, Dict[str, Any]]:
    """读取整个 .usage.json 映射。缺失/损坏时返回空字典。"""
    path = _usage_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Failed to read %s: %s", path, e)
        return {}
    if not isinstance(data, dict):
        return {}
    # 防御性：把任何非字典值强制转换为一个全新的空记录
    clean: Dict[str, Dict[str, Any]] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            clean[str(k)] = v
    return clean


def save_usage(data: Dict[str, Dict[str, Any]]) -> None:
    """原子地写入使用映射。尽力而为——错误被记录而非抛出。"""
    path = _usage_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=".usage_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.debug("Failed to write %s: %s", path, e, exc_info=True)


def get_record(skill_name: str) -> Dict[str, Any]:
    """返回 *skill_name* 的记录，缺失时创建一个新记录。"""
    data = load_usage()
    rec = data.get(skill_name)
    if not isinstance(rec, dict):
        return _empty_record()
    # 回填任何缺失的键，使调用方不必处理旧文件
    base = _empty_record()
    for k, v in base.items():
        rec.setdefault(k, v)
    return rec


def seed_record_if_missing(skill_name: str) -> None:
    """为一个有整理资格的技能持久化一条基线使用记录。

    内置技能在被触碰之前不携带使用记录，这使其不活跃时钟没有锚点。在此
    种子化一条记录会把 ``created_at`` 固定到 curator 首次见到该技能的
    时刻，使归档/过期时钟从那时起衡量不活跃——而不是从纪元起。当记录已
    存在或技能无整理资格时为 no-op。
    """
    if not skill_name or not is_curation_eligible(skill_name):
        return
    try:
        with _usage_file_lock():
            data = load_usage()
            if isinstance(data.get(skill_name), dict):
                return
            data[skill_name] = _empty_record()
            save_usage(data)
    except Exception as e:
        logger.debug("skill_usage.seed_record_if_missing(%s) failed: %s", skill_name, e, exc_info=True)


def _mutate(skill_name: str, mutator, *, require_curation_eligible: bool = False) -> None:
    """加载，原地应用 *mutator(record)*，保存。尽力而为。

    默认情况下，这会为任何技能记录遥测——内置、hub 安装或 agent 创建——
    因为使用跟踪是纯观测性，与一个技能是否曾被整理正交。生命周期变更器
    （``set_state``、``set_pinned``、``mark_agent_created``）传入
    ``require_curation_eligible=True``，使其永远不会在 curator 无法管理的
    技能上写入无意义的状态（例如在一个 hub 安装的技能上的 ``archived``
    标志）。
    """
    if not skill_name:
        return
    try:
        if require_curation_eligible and not is_curation_eligible(skill_name):
            return
        with _usage_file_lock():
            data = load_usage()
            rec = data.get(skill_name)
            if not isinstance(rec, dict):
                rec = _empty_record()
            mutator(rec)
            data[skill_name] = rec
            save_usage(data)
    except Exception as e:
        logger.debug("skill_usage._mutate(%s) failed: %s", skill_name, e, exc_info=True)


# ---------------------------------------------------------------------------
# 公开的计数器递增辅助 —— 为所有技能做遥测（仅观测性）
# ---------------------------------------------------------------------------

def bump_view(skill_name: str) -> None:
    """递增 view_count 和 last_viewed_at。由 skill_view() 调用。

    跟踪每个技能，无论出处——包括内置和 hub 技能。使用遥测是观测性，
    而非整理信号。
    """
    def _apply(rec: Dict[str, Any]) -> None:
        rec["view_count"] = int(rec.get("view_count") or 0) + 1
        rec["last_viewed_at"] = _now_iso()
    _mutate(skill_name, _apply)


def bump_use(skill_name: str) -> None:
    """递增 use_count 和 last_used_at。在某个技能被主动使用时调用
    （例如被加载到提示路径中或从助手回合中被引用）。

    跟踪每个技能，无论出处。
    """
    def _apply(rec: Dict[str, Any]) -> None:
        rec["use_count"] = int(rec.get("use_count") or 0) + 1
        rec["last_used_at"] = _now_iso()
    _mutate(skill_name, _apply)


def bump_patch(skill_name: str) -> None:
    """递增 patch_count 和 last_patched_at。由 skill_manage（修补/编辑）调用。

    跟踪每个技能，无论出处。
    """
    def _apply(rec: Dict[str, Any]) -> None:
        rec["patch_count"] = int(rec.get("patch_count") or 0) + 1
        rec["last_patched_at"] = _now_iso()
    _mutate(skill_name, _apply)


def mark_agent_created(skill_name: str) -> None:
    """把一个由 skill_manage 创建的技能纳入 curator 管理。

    查看或调用一个手动编写的技能可能仍会创建遥测，但只有这个显式标记
    才使其有资格被自动整理。
    """
    def _apply(rec: Dict[str, Any]) -> None:
        rec["created_by"] = "agent"
    _mutate(skill_name, _apply, require_curation_eligible=True)


def set_state(skill_name: str, state: str) -> None:
    """设置生命周期状态。如果 *state* 无效或技能不可被 curator 管理
    （hub 技能，或禁用了修剪的内置技能），则为 no-op。"""
    if state not in _VALID_STATES:
        logger.debug("set_state: invalid state %r for %s", state, skill_name)
        return
    def _apply(rec: Dict[str, Any]) -> None:
        rec["state"] = state
        if state == STATE_ARCHIVED:
            rec["archived_at"] = _now_iso()
        elif state == STATE_ACTIVE:
            rec["archived_at"] = None
    _mutate(skill_name, _apply, require_curation_eligible=True)


def set_pinned(skill_name: str, pinned: bool) -> None:
    def _apply(rec: Dict[str, Any]) -> None:
        rec["pinned"] = bool(pinned)
    _mutate(skill_name, _apply, require_curation_eligible=True)


def forget(skill_name: str) -> None:
    """彻底删除某个技能的使用条目。在技能被删除时调用。"""
    if not skill_name:
        return
    try:
        with _usage_file_lock():
            data = load_usage()
            if skill_name in data:
                del data[skill_name]
                save_usage(data)
    except Exception as e:
        logger.debug("skill_usage.forget(%s) failed: %s", skill_name, e, exc_info=True)


# ---------------------------------------------------------------------------
# 归档 / 恢复
# ---------------------------------------------------------------------------

def archive_skill(skill_name: str) -> Tuple[bool, str]:
    """把一个有整理资格的技能目录移动到 ~/.hermes/skills/.archive/。

    返回 (ok, message)。永不归档 hub 安装的技能。内置技能仅在启用了
    ``curator.prune_builtins`` 时才可归档；当某个被归档时，其名称会被加入
    抑制列表，使更新时的重新种子器让它保持归档而非恢复它。
    """
    if not is_curation_eligible(skill_name):
        if is_protected_builtin(skill_name):
            return False, (
                f"skill '{skill_name}' is a protected built-in; it backs "
                "load-bearing UX and is never archived or consolidated"
            )
        if is_hub_installed(skill_name):
            return False, f"skill '{skill_name}' is hub-installed; never archive"
        return False, (
            f"skill '{skill_name}' is a bundled built-in; enable "
            "curator.prune_builtins to allow pruning it"
        )

    skill_dir = _find_skill_dir(skill_name)
    if skill_dir is None:
        return False, f"skill '{skill_name}' not found"

    archive_root = _archive_dir()
    try:
        archive_root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"failed to create archive dir: {e}"

    # 把任何分类嵌套展平为单一的 ".archive/<skill>/"，使恢复简单。如果存在
    # 冲突，追加一个时间戳。
    dest = archive_root / skill_dir.name
    if dest.exists():
        dest = archive_root / f"{skill_dir.name}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

    try:
        skill_dir.rename(dest)
    except OSError as e:
        # 跨设备 —— 回退到 shutil.move
        import shutil
        try:
            shutil.move(str(skill_dir), str(dest))
        except Exception as e2:
            return False, f"failed to archive: {e2}"

    # 修剪一个内置技能只有在告诉重新种子器别管它时才会持久。
    if is_bundled(skill_name):
        add_suppressed_name(skill_name)

    set_state(skill_name, STATE_ARCHIVED)
    return True, f"archived to {dest}"


def restore_skill(skill_name: str) -> Tuple[bool, str]:
    """把一个已归档的技能移回 ~/.hermes/skills/。恢复到扁平的顶层布局；
    原始的分类嵌套不会被重建。

    拒绝在一个现在与 hub 安装技能冲突的名称下恢复——那会遮蔽上游版本。
    也拒绝在一个内置技能之上恢复，除非启用了 ``curator.prune_builtins``
    （此时内置技能是 curator 托管的，恢复是解除修剪的文档化方式）。恢复
    会清除任何抑制条目，使未来的更新可以再次为该内置技能重新种子。
    """
    # hub 技能始终有一个外部的上游所有者 —— 永不遮蔽它们。
    if is_hub_installed(skill_name):
        return False, (
            f"skill '{skill_name}' is now hub-installed; "
            "restore would shadow the upstream version"
        )
    # 内置技能是上游拥有的，除非开启了 prune_builtins。标志关闭时，在其
    # 之上恢复会遮蔽内置版本。
    if is_bundled(skill_name) and not _prune_builtins_enabled():
        return False, (
            f"skill '{skill_name}' is now bundled; "
            "restore would shadow the upstream version"
        )
    archive_root = _archive_dir()
    if not archive_root.exists():
        return False, "no archive directory"

    # 先尝试精确名称匹配，再尝试带时间戳的重复回退。
    # 递归遍历处理嵌套的归档布局（例如 .archive/<category>/<skill>/），
    # 这些是旧归档路径或外部导入留下的。
    candidates = [p for p in archive_root.rglob("*") if p.is_dir() and p.name == skill_name]
    if not candidates:
        # 名称冲突会使 archive_skill() 通过追加其 UTC 时间戳
        # （"<skill>-YYYYMMDDHHMMSS"，一个 14 位后缀）来消歧，因此只有那个
        # 精确形状才是此技能的另一份副本。一个裸
        # startswith(f"{skill_name}-") 也会吞下不相关的兄弟技能——恢复
        # "git" 否则会把一个已归档的 "git-helpers" 从归档中拉出并重命名
        # 为 "git"，破坏兄弟技能的唯一副本。要求后缀是 archive_skill 写入
        # 的时间戳。
        prefix = f"{skill_name}-"
        candidates = sorted(
            [
                p for p in archive_root.rglob("*")
                if p.is_dir()
                and p.name.startswith(prefix)
                and len(p.name) - len(prefix) == 14
                and p.name[len(prefix):].isdigit()
            ],
            reverse=True,
        )
    if not candidates:
        return False, f"skill '{skill_name}' not found in archive"

    src = candidates[0]
    dest = _skills_dir() / skill_name
    if dest.exists():
        return False, f"destination already exists: {dest}"

    try:
        src.rename(dest)
    except OSError:
        import shutil
        try:
            shutil.move(str(src), str(dest))
        except Exception as e:
            return False, f"failed to restore: {e}"

    # 恢复一个被修剪的内置技能会解除其抑制，使更新可以管理它。
    remove_suppressed_name(skill_name)

    set_state(skill_name, STATE_ACTIVE)
    return True, f"restored to {dest}"


def _find_skill_dir(skill_name: str) -> Optional[Path]:
    """通过技能 frontmatter 的 `name:` 字段定位其目录。

    同时处理扁平（~/.hermes/skills/<skill>/SKILL.md）和分类嵌套
    （~/.hermes/skills/<category>/<skill>/SKILL.md）布局。
    """
    base = _skills_dir()
    if not base.exists():
        return None
    for skill_md in base.rglob("SKILL.md"):
        if is_excluded_skill_path(skill_md):
            continue
        if _read_skill_name(skill_md, fallback=skill_md.parent.name) == skill_name:
            return skill_md.parent
    return None


# ---------------------------------------------------------------------------
# 报告 —— 供 curator CLI / 斜杠命令使用
# ---------------------------------------------------------------------------

def agent_created_report() -> List[Dict[str, Any]]:
    """为每个 curator 托管的技能返回一条
    {name, state, pinned, last_activity_at, ...} 记录。缺失的使用记录会用
    默认值回填，使调用方始终能索引字段。

    每一行携带 ``_persisted``：当 ``.usage.json`` 中存在真实记录时为 True，
    当该行是全新回填时（例如首次见到的内置技能）为 False。curator 用此来
    种子化不活跃时钟，而不是把一个未记录的技能当作古老。
    """
    data = load_usage()
    rows: List[Dict[str, Any]] = []
    for name in list_agent_created_skill_names():
        raw = data.get(name)
        persisted = isinstance(raw, dict)
        rec: Dict[str, Any] = raw if isinstance(raw, dict) else _empty_record()
        base = _empty_record()
        for k, v in base.items():
            rec.setdefault(k, v)
        row = {"name": name, **rec, "_persisted": persisted}
        row["last_activity_at"] = latest_activity_at(row)
        row["activity_count"] = activity_count(row)
        rows.append(row)
    return rows


def provenance(skill_name: str) -> str:
    """分类一个技能的来源：'hub'、'bundled' 或 'agent'。

    'agent' 涵盖 agent 编写和本地手动编写的技能——任何不从内置仓库种子
    或不经 hub 安装的技能。
    """
    if is_hub_installed(skill_name):
        return "hub"
    if is_bundled(skill_name):
        return "bundled"
    return "agent"


def usage_report() -> List[Dict[str, Any]]:
    """为磁盘上的每个技能返回带出处的使用遥测。

    与 ``agent_created_report()``（范围限于 curator 托管候选）不同，这会
    暴露所有技能——包括内置技能和 hub 安装技能——使调用方可以独立于
    一个技能是否曾被整理来回答"这个技能被使用的频率如何"。行携带一个
    ``provenance`` 字段（'agent' | 'bundled' | 'hub'）和 ``_persisted``
    （是否有真实的 ``.usage.json`` 记录支撑该行）。
    """
    base = _skills_dir()
    if not base.exists():
        return []
    data = load_usage()
    rows: List[Dict[str, Any]] = []
    seen: set = set()
    for skill_md in base.rglob("SKILL.md"):
        if is_excluded_skill_path(skill_md):
            continue
        name = _read_skill_name(skill_md, fallback=skill_md.parent.name)
        if name in seen:
            continue
        seen.add(name)
        raw = data.get(name)
        persisted = isinstance(raw, dict)
        rec: Dict[str, Any] = raw if isinstance(raw, dict) else _empty_record()
        base_rec = _empty_record()
        for k, v in base_rec.items():
            rec.setdefault(k, v)
        row = {
            "name": name,
            **rec,
            "provenance": provenance(name),
            "_persisted": persisted,
        }
        row["last_activity_at"] = latest_activity_at(row)
        row["activity_count"] = activity_count(row)
        rows.append(row)
    return sorted(rows, key=lambda r: r["name"])
