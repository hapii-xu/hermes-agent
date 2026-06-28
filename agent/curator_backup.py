"""Curator 快照与回滚。

在任何变更性质的 curator 执行之前，先对 ``~/.hermes/skills/``（排除
``.curator_backups/`` 自身）进行一次预运行快照。快照是以 tar.gz 压缩的
文件，存放在 ``~/.hermes/skills/.curator_backups/<utc-iso>/`` 目录下，
并附带一个 ``manifest.json`` 描述快照信息（原因、时间、大小、
skill 文件数量）。回滚时选择一个快照，先将当前 ``skills/`` 目录树
移动到另一个快照中（使得回滚本身也是可撤销的），然后将选定的快照
解压到原位。

快照不包含以下内容：
  - ``.curator_backups/``（会导致递归）
  - ``.hub/``（hub 安装的 skill —— 由 hub 管理，不归我们管）

快照包含以下内容：
  - 所有 SKILL.md 文件及其目录（``scripts/``、``references/``、
    ``templates/``、``assets/``）
  - ``.usage.json``（使用遥测数据 —— 干净地恢复状态所需）
  - ``.archive/``（这样回滚也能恢复之前归档的 skill）
  - ``.curator_state``（这样回滚也能恢复上次运行的时间点指针
    —— 否则 curator 会在下一个 tick 立即重新触发）
  - ``.bundled_manifest``（确保保护标记保持一致）
  - ``.curator_suppressed``（这样回滚可以恢复被裁剪的内置 skill 集合，
    重新播种器必须让它们保持归档状态）

除了 skills 压缩包之外，每个快照还会在 ``~/.hermes/cron/jobs.json``
存在时将其副本作为 ``cron-jobs.json`` 一同捕获。Cron 任务在其
``skills``/``skill`` 字段中按名称引用 skill；curator 的合并过程会通过
``cron.jobs.rewrite_skill_refs()`` 就地重写这些引用。如果不捕获预运行
状态，回滚 skills 目录树后将导致 cron 任务仍然指向 umbrella skill，
即使它们最初配置的 narrow skill 已被恢复。我们存储完整的 jobs.json
以保证保真度，但回滚只修改 ``skills``/``skill`` 字段 —— 其余部分
（schedule、next_run_at、enabled、prompt 等）是运行时状态，
我们不做改动。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from hermes_constants import get_hermes_home
from agent.skill_utils import is_excluded_skill_path

logger = logging.getLogger(__name__)


DEFAULT_KEEP = 5

# skills/ 下不应被纳入快照的条目。
# .hub/ 由 skill hub 管理；回滚它会破坏 lockfile 的
# 不变量。.curator_backups 是备份目录本身 —— 会导致递归炸弹。
_EXCLUDE_TOP_LEVEL = {".curator_backups", ".hub"}

# 快照 ID 正则：UTC ISO 格式，冒号替换为短横线，确保文件名
# 可跨平台使用（Windows 安全）。可选的 ``-NN`` 后缀用于处理
# 两个快照落在同一秒内的情况。
_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z(-\d{2})?$")


def _backups_dir() -> Path:
    return get_hermes_home() / "skills" / ".curator_backups"


def _skills_dir() -> Path:
    return get_hermes_home() / "skills"


def _cron_jobs_file() -> Path:
    """实时 cron 任务存储的源路径（``~/.hermes/cron/jobs.json``）。"""
    return get_hermes_home() / "cron" / "jobs.json"


CRON_JOBS_FILENAME = "cron-jobs.json"


def _backup_cron_jobs_into(dest: Path) -> Dict[str, Any]:
    """将实时的 cron jobs.json 复制到 ``dest`` 中，保存为 ``cron-jobs.json``。

    返回一个小字典描述捕获的内容，以便调用方将其合并到 manifest 中。
    永不抛出异常 —— 如果 cron 文件缺失或不可读，返回字典中
    ``backed_up=False`` 及原因，快照照常继续但不含 cron 数据
    （快照对于回滚 skills 仍然有效）。
    """
    src = _cron_jobs_file()
    info: Dict[str, Any] = {"backed_up": False, "jobs_count": 0}
    if not src.exists():
        info["reason"] = "no cron/jobs.json present"
        return info
    try:
        raw = src.read_text(encoding="utf-8")
    except OSError as e:
        logger.debug("Failed to read cron/jobs.json for backup: %s", e)
        info["reason"] = f"read error: {e}"
        return info
    # 统计任务数作为诊断信息 —— 但如果文件无法解析不应让快照失败；
    # 只存储原始文本，让回滚自行处理（或在文件损坏时忽略）。
    # jobs.json 将列表包装为 `{"jobs": [...], "updated_at": ...}` ——
    # 按该结构计数，同时回退到裸列表格式以防格式未来变更。
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            inner = parsed.get("jobs")
            if isinstance(inner, list):
                info["jobs_count"] = len(inner)
        elif isinstance(parsed, list):
            info["jobs_count"] = len(parsed)
    except (json.JSONDecodeError, TypeError):
        info["jobs_count"] = 0
        info["parse_warning"] = "jobs.json was not valid JSON at snapshot time"
    try:
        (dest / CRON_JOBS_FILENAME).write_text(raw, encoding="utf-8")
    except OSError as e:
        logger.debug("Failed to write cron backup file: %s", e)
        info["reason"] = f"write error: {e}"
        return info
    info["backed_up"] = True
    return info


def _utc_id(now: Optional[datetime] = None) -> str:
    """UTC ISO 风格的文件系统安全时间戳：``2026-05-01T13-05-42Z``。"""
    if now is None:
        now = datetime.now(timezone.utc)
    # isoformat → "2026-05-01T13:05:42.123456+00:00"；去掉亚秒和时区。
    s = now.replace(microsecond=0).isoformat()
    if s.endswith("+00:00"):
        s = s[:-6]
    return s.replace(":", "-") + "Z"


def _load_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
    except Exception as e:
        logger.debug("Failed to load config for curator backup: %s", e)
        return {}
    if not isinstance(cfg, dict):
        return {}
    cur = cfg.get("curator") or {}
    if not isinstance(cur, dict):
        return {}
    bk = cur.get("backup") or {}
    return bk if isinstance(bk, dict) else {}


def is_enabled() -> bool:
    """默认开启 —— 备份的意义在于默认保证安全。"""
    return bool(_load_config().get("enabled", True))


def get_keep() -> int:
    cfg = _load_config()
    try:
        n = int(cfg.get("keep", DEFAULT_KEEP))
    except (TypeError, ValueError):
        n = DEFAULT_KEEP
    return max(1, n)


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------

def _count_skill_files(base: Path) -> int:
    try:
        return sum(
            1 for p in base.rglob("SKILL.md") if not is_excluded_skill_path(p)
        )
    except OSError:
        return 0


def _write_manifest(dest: Path, reason: str, archive_path: Path,
                    skills_counted: int,
                    cron_info: Optional[Dict[str, Any]] = None) -> None:
    manifest = {
        "id": dest.name,
        "reason": reason,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "archive": archive_path.name,
        "archive_bytes": archive_path.stat().st_size,
        "skill_files": skills_counted,
    }
    if cron_info is not None:
        manifest["cron_jobs"] = {
            "backed_up": bool(cron_info.get("backed_up", False)),
            "jobs_count": int(cron_info.get("jobs_count", 0)),
        }
        if not cron_info.get("backed_up"):
            manifest["cron_jobs"]["reason"] = cron_info.get("reason", "not captured")
        if cron_info.get("parse_warning"):
            manifest["cron_jobs"]["parse_warning"] = cron_info["parse_warning"]
    (dest / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )


def snapshot_skills(reason: str = "manual", *, protect_ids: Optional[Set[str]] = None) -> Optional[Path]:
    """创建 ``~/.hermes/skills/`` 的 tar.gz 快照并清理旧快照。

    返回快照目录路径，或在以下情况返回 ``None``：
    备份已禁用、skills 目录不存在、或发生 IO 错误
    （此时记录 debug 日志并返回 None，确保 curator 不会因备份失败而中止执行）。

    ``protect_ids`` 会转发给清理步骤，使调用方可以保证
    特定快照 id 即使超出保留窗口也不会被删除
    （rollback 会传入将要从中恢复的快照 id）。
    """
    if not is_enabled():
        logger.debug("Curator backup disabled by config; skipping snapshot")
        return None

    skills = _skills_dir()
    if not skills.exists():
        logger.debug("No ~/.hermes/skills/ directory — nothing to back up")
        return None

    backups = _backups_dir()
    try:
        backups.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.debug("Failed to create backups dir %s: %s", backups, e)
        return None

    # 唯一化：如果同一秒内已存在快照（可能发生在两次 curator 运行处于同一秒时），
    # 追加短计数器。避免覆盖和时间戳冲突。
    base_id = _utc_id()
    snap_id = base_id
    counter = 1
    while (backups / snap_id).exists():
        snap_id = f"{base_id}-{counter:02d}"
        counter += 1

    dest = backups / snap_id
    try:
        dest.mkdir(parents=True, exist_ok=False)
    except OSError as e:
        logger.debug("Failed to create snapshot dir %s: %s", dest, e)
        return None

    archive = dest / "skills.tar.gz"
    try:
        # 流式写入 tarball —— 无需临时目录拷贝。
        with tarfile.open(archive, "w:gz", compresslevel=6) as tf:
            for entry in sorted(skills.iterdir()):
                if entry.name in _EXCLUDE_TOP_LEVEL:
                    continue
                # arcname：以相对于 skills/ 的路径存储，
                # 解压时可直接还原到 skills 目录。
                tf.add(str(entry), arcname=entry.name, recursive=True)
        # 将 cron/jobs.json 与 tarball 一起捕获。永不让快照失败 ——
        # skills 侧是核心保证；cron 是附加项。仍在 manifest 中记录
        # 是否捕获，以便回滚时可提示"此快照无 cron 数据"。
        cron_info = _backup_cron_jobs_into(dest)
        _write_manifest(dest, reason, archive,
                        _count_skill_files(skills),
                        cron_info=cron_info)
    except (OSError, tarfile.TarError) as e:
        logger.debug("Curator snapshot failed: %s", e, exc_info=True)
        # 清理部分快照
        try:
            shutil.rmtree(dest, ignore_errors=True)
        except OSError:
            pass
        return None

    _prune_old(keep=get_keep(), protect=protect_ids)
    logger.info("Curator snapshot created: %s (%s)", snap_id, reason)
    return dest


def _prune_old(keep: int, protect: Optional[Set[str]] = None) -> List[str]:
    """删除超出最新 *keep* 个数的普通快照。返回已删除的 id 列表。
    *protect* 中的快照 id 即使超出保留窗口也不会被删除 ——
    rollback() 使用此功能，确保强制性的预回滚安全快照不会将
    即将恢复的快照本身驱逐掉。
    暂存目录（``.rollback-staging-*``）是实现细节，每次调用都会独立清理。"""
    protect = protect or set()
    backups = _backups_dir()
    if not backups.exists():
        return []
    entries: List[Tuple[str, Path]] = []
    stale_staging: List[Path] = []
    for child in backups.iterdir():
        if not child.is_dir():
            continue
        if child.name.startswith(".rollback-staging-"):
            # 暂存目录仅应在回滚期间短暂存在。
            # 若在此发现（如崩溃的回滚残留），则机会性清理。
            stale_staging.append(child)
            continue
        if _ID_RE.match(child.name):
            entries.append((child.name, child))
    # 最新的在前（词法排序有效，因为 id 是 UTC ISO 格式）。
    entries.sort(key=lambda t: t[0], reverse=True)
    deleted: List[str] = []
    for _, path in entries[keep:]:
        if path.name in protect:
            continue
        try:
            shutil.rmtree(path)
            deleted.append(path.name)
        except OSError as e:
            logger.debug("Failed to prune %s: %s", path, e)
    for path in stale_staging:
        try:
            shutil.rmtree(path)
        except OSError as e:
            logger.debug("Failed to clean stale staging dir %s: %s", path, e)
    return deleted


# ---------------------------------------------------------------------------
# 列表与回滚
# ---------------------------------------------------------------------------

def _read_manifest(snap_dir: Path) -> Dict[str, Any]:
    mf = snap_dir / "manifest.json"
    if not mf.exists():
        return {}
    try:
        return json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def list_backups() -> List[Dict[str, Any]]:
    """返回所有可恢复的快照，最新的在前。只列出有真实
    ``skills.tar.gz`` tarball 的条目 —— 回滚中途创建的
    ``.rollback-staging-*`` 临时目录是实现细节，不予展示。"""
    backups = _backups_dir()
    if not backups.exists():
        return []
    out: List[Dict[str, Any]] = []
    for child in sorted(backups.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        if not _ID_RE.match(child.name):
            continue
        if not (child / "skills.tar.gz").exists():
            continue
        mf = _read_manifest(child)
        mf.setdefault("id", child.name)
        mf.setdefault("path", str(child))
        if "archive_bytes" not in mf:
            arc = child / "skills.tar.gz"
            try:
                mf["archive_bytes"] = arc.stat().st_size
            except OSError:
                mf["archive_bytes"] = 0
        out.append(mf)
    return out


def _resolve_backup(backup_id: Optional[str]) -> Optional[Path]:
    """返回请求的备份路径，若 *backup_id* 为 None 则返回最新的快照。
    找不到匹配时返回 None。"""
    backups = _backups_dir()
    if not backups.exists():
        return None
    if backup_id:
        target = backups / backup_id
        if (
            target.is_dir()
            and _ID_RE.match(backup_id)
            and (target / "skills.tar.gz").exists()
        ):
            return target
        return None
    candidates = [
        c for c in sorted(backups.iterdir(), reverse=True)
        if c.is_dir() and _ID_RE.match(c.name) and (c / "skills.tar.gz").exists()
    ]
    return candidates[0] if candidates else None


def _restore_cron_skill_links(snapshot_dir: Path) -> Dict[str, Any]:
    """将已备份的 cron skill 链接协调到实时的 ``cron/jobs.json`` 中。

    我们不覆盖整个 cron 文件。只恢复 ``skills`` 和 ``skill`` 字段，
    且仅针对当前文件中仍存在（按 ``id`` 匹配）的任务。
    任务的其他内容 —— schedule、next_run_at、last_run_at、enabled、
    prompt、workdir、hooks —— 是快照之后用户/调度器修改的实时状态；
    覆盖这些内容会回退与 skill 无关的 cron 活动。

    规则：
    - 备份和实时中均存在、但 skills 不同的任务 → 恢复 skills。
    - 备份和实时中均存在、skills 相同的任务 → 无操作。
    - 备份中存在但实时中已消失（用户在快照后删除了任务）→ 跳过，记录在返回报告中。
    - 实时中存在但备份中没有（用户在快照后新建了 cron 任务）→ 不动。

    永不抛出异常；失败会捕获在返回字典中。通过 ``cron.jobs``
    写入，以沿用 tick() 使用的相同锁 + 原子写入路径，避免与调度器竞争。
    """
    report: Dict[str, Any] = {
        "attempted": False,
        "restored": [],
        "skipped_missing": [],
        "unchanged": 0,
        "error": None,
    }
    backup_file = snapshot_dir / CRON_JOBS_FILENAME
    if not backup_file.exists():
        report["error"] = f"snapshot has no {CRON_JOBS_FILENAME}"
        return report

    try:
        backup_text = backup_file.read_text(encoding="utf-8")
        backup_parsed = json.loads(backup_text)
    except (OSError, json.JSONDecodeError) as e:
        report["error"] = f"failed to load backed-up jobs: {e}"
        return report
    # jobs.json 格式为 `{"jobs": [...], "updated_at": ...}`；接受该格式
    # 和裸列表格式以兼容未来可能的格式变更。
    if isinstance(backup_parsed, dict):
        backup_jobs = backup_parsed.get("jobs")
    elif isinstance(backup_parsed, list):
        backup_jobs = backup_parsed
    else:
        backup_jobs = None
    if not isinstance(backup_jobs, list):
        report["error"] = "backed-up cron-jobs.json has no jobs list"
        return report

    # 按 job id 构建已备份 skill 状态的查找表。
    # 只需两个 skill 相关字段（旧版单值和新版列表）。
    backup_by_id: Dict[str, Dict[str, Any]] = {}
    for job in backup_jobs:
        if not isinstance(job, dict):
            continue
        jid = job.get("id")
        if not isinstance(jid, str) or not jid:
            continue
        backup_by_id[jid] = {
            "skills": job.get("skills"),
            "skill": job.get("skill"),
            "name": job.get("name") or jid,
        }

    if not backup_by_id:
        report["attempted"] = True  # 尝试过但没有任何内容需要处理
        return report

    # 在调度器的跨进程锁下加载并重写实时任务。
    try:
        from cron.jobs import load_jobs, save_jobs, _jobs_lock
    except ImportError as e:
        report["error"] = f"cron module unavailable: {e}"
        return report

    report["attempted"] = True
    try:
        with _jobs_lock():
            live_jobs = load_jobs()
            changed = False

            live_ids = set()
            for live in live_jobs:
                if not isinstance(live, dict):
                    continue
                jid = live.get("id")
                if not isinstance(jid, str) or not jid:
                    continue
                live_ids.add(jid)

                backup = backup_by_id.get(jid)
                if backup is None:
                    continue  # 该实时任务在快照时不存在

                cur_skills = live.get("skills")
                cur_skill = live.get("skill")
                bkp_skills = backup.get("skills")
                bkp_skill = backup.get("skill")

                if cur_skills == bkp_skills and cur_skill == bkp_skill:
                    report["unchanged"] += 1
                    continue

                # 恢复。保留缺失状态（如果备份中也没有该键，不强制添加）。
                if bkp_skills is None:
                    live.pop("skills", None)
                else:
                    live["skills"] = bkp_skills
                if bkp_skill is None:
                    live.pop("skill", None)
                else:
                    live["skill"] = bkp_skill

                report["restored"].append({
                    "job_id": jid,
                    "job_name": backup.get("name") or jid,
                    "from": {"skills": cur_skills, "skill": cur_skill},
                    "to": {"skills": bkp_skills, "skill": bkp_skill},
                })
                changed = True

            # 备份中有但实时中没有的任务 = 用户在快照后删除了这些任务
            for jid, backup in backup_by_id.items():
                if jid not in live_ids:
                    report["skipped_missing"].append({
                        "job_id": jid,
                        "job_name": backup.get("name") or jid,
                    })

            if changed:
                save_jobs(live_jobs)
    except Exception as e:  # noqa: BLE001 — 回滚不能在恢复中途崩溃
        logger.debug("Cron skill-link restore failed: %s", e, exc_info=True)
        report["error"] = f"restore failed mid-flight: {e}"

    return report



def rollback(backup_id: Optional[str] = None) -> Tuple[bool, str, Optional[Path]]:
    """从快照恢复 ``~/.hermes/skills/``。

    策略：
      1. 解析目标快照（显式 id 或最新的普通快照）。
      2. 在 ``.curator_backups/pre-rollback-<ts>/`` 下对当前 skills 树
         进行安全快照，使回滚本身可撤销。
      3. 将所有当前顶级条目（除 ``.curator_backups`` 和 ``.hub`` 外）
         移入临时目录。
      4. 将所选快照解压到 ``~/.hermes/skills/``。
      5. 若步骤 4 失败，尽力将临时目录内容移回并返回失败。

    返回 ``(ok, message, snapshot_path)``。
    """
    target = _resolve_backup(backup_id)
    if target is None:
        return (
            False,
            f"no matching backup found"
            + (f" for id '{backup_id}'" if backup_id else "")
            + " (use `hermes curator rollback --list` to see available snapshots)",
            None,
        )
    archive = target / "skills.tar.gz"
    if not archive.exists():
        return (False, f"snapshot {target.name} has no skills.tar.gz — corrupted?", None)

    skills = _skills_dir()
    skills.mkdir(parents=True, exist_ok=True)
    backups = _backups_dir()
    backups.mkdir(parents=True, exist_ok=True)

    # 步骤 2：首先对当前状态进行安全快照。如果失败，在触碰任何内容之前退出 ——
    # 否则解压失败可能导致用户没有任何 skills。
    try:
        # 在此次快照的清理步骤中保护目标：在达到稳定保留上限时，
        # 清理最旧快照可能会删除我们即将解压的那个快照。
        snapshot_skills(
            reason=f"pre-rollback to {target.name}",
            protect_ids={target.name},
        )
    except Exception as e:
        return (False, f"pre-rollback safety snapshot failed: {e}", None)

    # 此外，将当前条目移入内部暂存目录，使解压进入空的 skills 树（结果可预期）。
    # 该目录是实现细节 —— 不作为可恢复备份列出。
    # 上面的安全快照才是用户可见的撤销入口。
    staged = backups / f".rollback-staging-{_utc_id()}"
    try:
        staged.mkdir(parents=True, exist_ok=False)
    except OSError as e:
        return (False, f"failed to create staging dir: {e}", None)

    moved: List[Tuple[Path, Path]] = []
    try:
        for entry in list(skills.iterdir()):
            if entry.name in _EXCLUDE_TOP_LEVEL:
                continue
            dest = staged / entry.name
            shutil.move(str(entry), str(dest))
            moved.append((entry, dest))
    except OSError as e:
        # 尽力撤销移动操作
        for orig, dest in moved:
            try:
                shutil.move(str(dest), str(orig))
            except OSError:
                pass
        try:
            shutil.rmtree(staged, ignore_errors=True)
        except OSError:
            pass
        return (False, f"failed to stage current skills: {e}", None)

    # 步骤 4：将快照解压到 skills/
    try:
        with tarfile.open(archive, "r:gz") as tf:
            # Python 3.12+ 支持 filter='data' 以更安全地解压。
            # 对旧解释器回退到无过滤器调用，但仍防御性地拒绝绝对路径和 .. 组件。
            for member in tf.getmembers():
                name = member.name
                if name.startswith("/") or ".." in Path(name).parts:
                    raise tarfile.TarError(
                        f"refusing to extract unsafe path: {name!r}"
                    )
            try:
                tf.extractall(str(skills), filter="data")  # type: ignore[call-arg]
            except TypeError:
                # Python < 3.12 —— 没有 filter 关键字参数
                tf.extractall(str(skills))
    except (OSError, tarfile.TarError) as e:
        # 尽力恢复：将暂存内容移回
        for orig, dest in moved:
            try:
                shutil.move(str(dest), str(orig))
            except OSError:
                pass
        try:
            shutil.rmtree(staged, ignore_errors=True)
        except OSError:
            pass
        return (False, f"snapshot extract failed (state restored): {e}", None)

    # 解压成功 —— 暂存目录已完成使命。
    # 用户的撤销入口是之前创建的安全快照 tarball。
    try:
        shutil.rmtree(staged, ignore_errors=True)
    except OSError:
        pass

    # 协调 cron skill 链接。精确操作：只修改按 id 匹配的任务的
    # skills/skill 字段。jobs.json 中的其他内容是实时状态
    # （schedule、next_run_at、enabled、prompt 等），我们不做改动。
    # 这里的失败不会导致整体回滚失败 —— skills 树已恢复，这是主要保证。
    cron_report = _restore_cron_skill_links(target)

    summary_bits = [f"restored from snapshot {target.name}"]
    if cron_report.get("attempted"):
        restored_n = len(cron_report.get("restored") or [])
        skipped_n = len(cron_report.get("skipped_missing") or [])
        if cron_report.get("error"):
            summary_bits.append(f"cron links: error — {cron_report['error']}")
        elif restored_n == 0 and skipped_n == 0 and cron_report.get("unchanged", 0) == 0:
            # 尝试过但没有匹配 —— 空快照或无重叠 id。
            pass
        else:
            parts = []
            if restored_n:
                parts.append(f"{restored_n} job(s) had skill links restored")
            if skipped_n:
                parts.append(f"{skipped_n} backed-up job(s) no longer exist (skipped)")
            if cron_report.get("unchanged"):
                parts.append(f"{cron_report['unchanged']} already matched")
            summary_bits.append("cron links: " + ", ".join(parts))

    logger.info("Curator rollback: restored from %s (cron_report=%s)",
                target.name, cron_report)
    return (True, "; ".join(summary_bits), target)


# ---------------------------------------------------------------------------
# 面向用户的 CLI 摘要
# ---------------------------------------------------------------------------

def format_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} GB"


def summarize_backups() -> str:
    rows = list_backups()
    if not rows:
        return "No curator snapshots yet."
    lines = [f"{'id':<24}  {'reason':<40}  {'skills':>6}  {'size':>8}"]
    lines.append("─" * len(lines[0]))
    for r in rows:
        lines.append(
            f"{r.get('id','?'):<24}  "
            f"{(r.get('reason','?') or '?')[:40]:<40}  "
            f"{r.get('skill_files', 0):>6}  "
            f"{format_size(int(r.get('archive_bytes', 0))):>8}"
        )
    return "\n".join(lines)
