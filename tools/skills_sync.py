#!/usr/bin/env python3
"""
Skills Sync -- 基于清单的内置技能播种与更新。

把仓库 skills/ 目录下的内置技能拷贝到 ~/.hermes/skills/，并用一份
清单跟踪哪些技能已被同步及其来源哈希。

清单格式（v2）：每行为 "skill_name:origin_hash"，其中 origin_hash
是该内置技能上次同步到用户目录时的 MD5。旧的 v1 清单（只有名称、
没有哈希）会被自动迁移。

更新逻辑：
  - NEW 技能（不在清单中）：拷贝到用户目录，并记录来源哈希。
  - EXISTING 技能（在清单中且存在于用户目录）：
      * 若用户副本与来源哈希一致：用户未改动 → 在内置版本变化时
        可安全地从内置更新。记录新的来源哈希。
      * 若用户副本与来源哈希不一致：用户做了定制 → 跳过。
  - DELETED by user（在清单中但用户目录里不存在）：尊重，不再重新加入。
  - REMOVED from bundled（在清单中但仓库里已移除）：从清单清理。

清单位于 ~/.hermes/skills/.bundled_manifest。
"""

import hashlib
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from hermes_constants import get_bundled_skills_dir, get_hermes_home, get_optional_skills_dir
from agent.skill_utils import is_excluded_skill_path
from typing import Dict, List, Optional, Tuple
from utils import atomic_replace

logger = logging.getLogger(__name__)


HERMES_HOME = get_hermes_home()
SKILLS_DIR = HERMES_HOME / "skills"
MANIFEST_FILE = SKILLS_DIR / ".bundled_manifest"

# 由 `hermes profile create --no-skills`（命名配置文件）和安装器的
# `--no-skills` 标志（默认的 ~/.hermes 配置文件）写入的标记文件。
# 当它出现在 HERMES_HOME 中时，sync_skills() 变成空操作，这样无论是
# 安装器、`hermes update` 还是直接同步，都不会再注入内置技能。
# 删除该文件即可重新开启。对应
# hermes_cli.profiles.NO_BUNDLED_SKILLS_MARKER（此处保留字面量，以
# 免把这个底层同步模块引入 CLI 层）。
NO_BUNDLED_SKILLS_MARKER = ".no-bundled-skills"


def _get_bundled_dir() -> Path:
    """定位内置 skills/ 目录。

    先检查 HERMES_BUNDLED_SKILLS 环境变量（由 Nix 包装脚本设置），
    再检查 wheel 安装的数据目录，最后回退到相对本源文件的路径。
    """
    return get_bundled_skills_dir(Path(__file__).parent.parent / "skills")


def _get_optional_dir() -> Path:
    """定位官方 optional-skills/ 目录。"""
    return get_optional_skills_dir(Path(__file__).parent.parent / "optional-skills")


def _read_manifest() -> Dict[str, str]:
    """
    把清单读取为 {skill_name: origin_hash} 形式的 dict。

    同时兼容 v1（纯名称）和 v2（name:hash）格式。
    v1 条目获得一个空哈希字符串，会在下次同步时触发迁移。
    """
    if not MANIFEST_FILE.exists():
        return {}
    try:
        result = {}
        for line in MANIFEST_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            if ":" in line:
                # v2 格式：name:hash
                name, _, hash_val = line.partition(":")
                result[name.strip()] = hash_val.strip()
            else:
                # v1 格式：纯名称 —— 空哈希触发迁移
                result[line] = ""
        return result
    except (OSError, IOError):
        return {}


def _read_suppressed_names() -> set:
    """被 curator 修剪掉的内置技能 —— 同步时绝不能重新播种。

    委托给 ``tools.skill_usage``（唯一可信源），若该导入在打包/更新
    场景下不可用，则直接回退读取
    ``~/.hermes/skills/.curator_suppressed``。
    """
    try:
        from tools.skill_usage import read_suppressed_names

        return read_suppressed_names()
    except Exception:
        path = SKILLS_DIR / ".curator_suppressed"
        if not path.exists():
            return set()
        names = set()
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    names.add(line)
        except OSError:
            pass
        return names


def _write_manifest(entries: Dict[str, str]):
    """以 v2 格式（name:hash）原子地写入清单文件。

    使用临时文件 + os.replace()，避免进程崩溃或写入中途被打断导致
    文件损坏。
    """
    import tempfile

    MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = "\n".join(f"{name}:{hash_val}" for name, hash_val in sorted(entries.items())) + "\n"

    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(MANIFEST_FILE.parent),
            prefix=".bundled_manifest_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            atomic_replace(tmp_path, MANIFEST_FILE)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.debug("Failed to write skills manifest %s: %s", MANIFEST_FILE, e, exc_info=True)


def _read_skill_name(skill_md: Path, fallback: str) -> str:
    """从 SKILL.md 的 YAML frontmatter 中读取 name 字段，失败则回退到 *fallback*。"""
    try:
        content = skill_md.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return fallback
    in_frontmatter = False
    for line in content.split("\n"):
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


def _discover_bundled_skills(bundled_dir: Path) -> List[Tuple[str, Path]]:
    """
    在内置目录下查找所有 SKILL.md 文件。
    返回 (skill_name, skill_directory_path) 元组的列表。
    """
    skills = []
    if not bundled_dir.exists():
        return skills

    for skill_md in bundled_dir.rglob("SKILL.md"):
        if is_excluded_skill_path(skill_md):
            continue
        skill_dir = skill_md.parent
        skill_name = _read_skill_name(skill_md, skill_dir.name)
        skills.append((skill_name, skill_dir))

    return skills


def _compute_relative_dest(skill_dir: Path, bundled_dir: Path) -> Path:
    """
    计算 SKILLS_DIR 下的目标路径，保留分类结构。
    例如 bundled/skills/mlops/axolotl -> ~/.hermes/skills/mlops/axolotl
    """
    rel = skill_dir.relative_to(bundled_dir)
    return SKILLS_DIR / rel


def _dir_hash(directory: Path) -> str:
    """对目录下所有文件内容计算哈希，用于变更检测。"""
    hasher = hashlib.md5()
    try:
        for fpath in sorted(directory.rglob("*")):
            if fpath.is_file():
                rel = fpath.relative_to(directory)
                hasher.update(str(rel).encode("utf-8"))
                hasher.update(fpath.read_bytes())
    except (OSError, IOError):
        pass
    return hasher.hexdigest()


def _safe_rel_install_path(path: Path, base: Path) -> str:
    """返回规范化的相对 POSIX 路径，拒绝穿越/绝对路径。"""
    rel = path.relative_to(base)
    posix = rel.as_posix()
    pure = PurePosixPath(posix)
    parts = [part for part in pure.parts if part not in {"", "."}]
    if pure.is_absolute() or not parts or any(part == ".." for part in parts):
        raise ValueError(f"Unsafe optional skill path: {posix}")
    return "/".join(parts)


def _skill_file_list(skill_dir: Path) -> List[str]:
    """以 lock-file 格式列出某个技能目录内的文件。"""
    files: List[str] = []
    for fpath in sorted(skill_dir.rglob("*")):
        if fpath.is_file():
            files.append(fpath.relative_to(skill_dir).as_posix())
    return files


def _content_hash(directory: Path) -> str:
    """返回与 skills hub lock 一致的哈希风格，本地则回退。"""
    try:
        from tools.skills_guard import content_hash

        return content_hash(directory)
    except Exception:
        # 哈希仅用于来源元数据；在打包/更新场景下 guard 依赖不可用时
        # 保持同步的健壮性。
        return _dir_hash(directory)


def _optional_skill_index() -> Dict[str, Tuple[str, str, Path]]:
    """以文件夹名和 frontmatter 名为键，返回官方可选技能。

    值为 ``(folder_name, install_path, source_dir)``。多个键可能指向
    同一个技能，这样调用方既能接受 hub lock 使用的文件夹 slug，
    也能接受面向用户的 frontmatter 名。
    """
    optional_dir = _get_optional_dir()
    index: Dict[str, Tuple[str, str, Path]] = {}
    if not optional_dir.exists():
        return index
    for skill_md in sorted(optional_dir.rglob("SKILL.md")):
        if is_excluded_skill_path(skill_md):
            continue
        src = skill_md.parent
        try:
            install_path = _safe_rel_install_path(src, optional_dir)
        except ValueError:
            continue
        folder_name = src.name
        frontmatter_name = _read_skill_name(skill_md, folder_name)
        value = (folder_name, install_path, src)
        index[folder_name] = value
        index[frontmatter_name] = value
    return index


def _move_to_restore_backup(path: Path, backup_root: Path) -> str:
    """把已有技能目录移到恢复备份中，保留相对路径。"""
    rel = path.relative_to(SKILLS_DIR)
    target = backup_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        suffix = 1
        while target.with_name(f"{target.name}-{suffix}").exists():
            suffix += 1
        target = target.with_name(f"{target.name}-{suffix}")
    shutil.move(str(path), str(target))
    return rel.as_posix()


def restore_official_optional_skill(name: str, *, restore: bool = False) -> dict:
    """从仓库源恢复一个或全部官方可选技能。

    ``restore=False`` 仅执行精确匹配的来源回填。``restore=True`` 通过
    备份匹配的活动副本并把官方可选源拷贝到其规范路径，修复已被改动/
    重组的技能。
    """
    index = _optional_skill_index()
    if not index:
        return {"ok": False, "message": "No official optional skills directory found.", "restored": [], "backfilled": [], "backed_up": []}

    targets = sorted(set(index.values()), key=lambda item: item[1]) if name in {"all", "*"} else []
    if not targets:
        target = index.get(name)
        if target is None:
            return {"ok": False, "message": f"Official optional skill not found: {name}", "restored": [], "backfilled": [], "backed_up": []}
        targets = [target]

    restored: List[str] = []
    backed_up: List[str] = []
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_root = SKILLS_DIR / ".restore-backups" / f"official-optional-{timestamp}"

    for folder_name, install_path, src in targets:
        dest = SKILLS_DIR / Path(*install_path.split("/"))
        src_hash = _dir_hash(src)
        canonical_ok = dest.exists() and _dir_hash(dest) == src_hash

        # 按 frontmatter 名或文件夹 slug 查找该官方技能已经活动的副本，
        # 即便 curator 把它移到了另一个分类下。
        src_frontmatter = _read_skill_name(src / "SKILL.md", folder_name)
        matches: List[Path] = []
        if SKILLS_DIR.exists():
            for skill_md in sorted(SKILLS_DIR.rglob("SKILL.md")):
                if is_excluded_skill_path(skill_md):
                    continue
                candidate = skill_md.parent
                try:
                    candidate.relative_to(SKILLS_DIR)
                except ValueError:
                    continue
                candidate_name = _read_skill_name(skill_md, candidate.name)
                if candidate == dest:
                    continue
                if candidate.name == folder_name or candidate_name in {folder_name, src_frontmatter}:
                    matches.append(candidate)

        if restore:
            for match in matches:
                if match.exists():
                    backed_up.append(_move_to_restore_backup(match, backup_root))
            if dest.exists() and not canonical_ok:
                backed_up.append(_move_to_restore_backup(dest, backup_root))
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src, dest)
                restored.append(folder_name)
        elif not canonical_ok:
            continue

    backfilled = _backfill_optional_provenance(quiet=True)
    return {
        "ok": True,
        "message": "Official optional skill repair complete.",
        "restored": restored,
        "backfilled": backfilled,
        "backed_up": backed_up,
        "backup_dir": str(backup_root) if backed_up else "",
    }


def _backfill_optional_provenance(quiet: bool = False) -> List[str]:
    """把已存在的官方可选技能标记为 hub 安装。

    这覆盖了这样的迁移场景：某个技能曾经是内置的（或被手动拷贝进
    活动技能树），后来又出现在 optional-skills/ 下。若活动副本与官方
    可选源逐字节一致，就记录官方 hub 来源，而无需拷贝或重装。已改动/
    本地化的技能保持不动。
    """
    optional_dir = _get_optional_dir()
    if not optional_dir.exists():
        return []

    lock_path = SKILLS_DIR / ".hub" / "lock.json"
    try:
        data = json.loads(lock_path.read_text()) if lock_path.exists() else {"version": 1, "installed": {}}
    except (json.JSONDecodeError, OSError):
        data = {"version": 1, "installed": {}}
    installed = data.setdefault("installed", {})
    existing_paths = {
        entry.get("install_path")
        for entry in installed.values()
        if isinstance(entry, dict)
    }

    backfilled: List[str] = []
    changed = False
    for skill_md in sorted(optional_dir.rglob("SKILL.md")):
        if is_excluded_skill_path(skill_md):
            continue
        src = skill_md.parent
        try:
            install_path = _safe_rel_install_path(src, optional_dir)
        except ValueError as e:
            logger.debug("Skipping optional skill with unsafe path %s: %s", src, e)
            continue
        dest = SKILLS_DIR / Path(*install_path.split("/"))
        if not dest.exists() or not dest.is_dir():
            continue
        if _dir_hash(dest) != _dir_hash(src):
            continue

        lock_name = src.name
        if lock_name in installed or install_path in existing_paths:
            continue

        timestamp = datetime.now(timezone.utc).isoformat()
        installed[lock_name] = {
            "source": "official",
            "identifier": f"official/{install_path}",
            "trust_level": "builtin",
            "scan_verdict": "backfilled",
            "content_hash": _content_hash(dest),
            "install_path": install_path,
            "files": _skill_file_list(dest),
            "metadata": {"backfilled_from": "optional-skills"},
            "installed_at": timestamp,
            "updated_at": timestamp,
        }
        existing_paths.add(install_path)
        backfilled.append(lock_name)
        changed = True
        if not quiet:
            print(f"  = {lock_name} (official optional provenance backfilled)")

    if changed:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写入，这样写入中途崩溃不会因为上面的 JSONDecodeError 回退
        # （它会把 `installed` 重置为空 dict）而静默抹掉全部来源。
        import tempfile

        payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        fd, tmp_path = tempfile.mkstemp(
            dir=str(lock_path.parent),
            prefix=".lock_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            atomic_replace(tmp_path, lock_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    return backfilled


def sync_skills(quiet: bool = False) -> dict:
    """
    用清单把内置技能同步到 ~/.hermes/skills/。

    返回：
        dict，键为：copied（list）、updated（list）、skipped（int）、
                        user_modified（list）、cleaned（list）、total_bundled（int）
    """
    # 主动退出：写入过 .no-bundled-skills 标记的配置文件（命名或默认
    # ~/.hermes）不会播种任何内置技能。返回带 skipped_opt_out 的空
    # 结果结构，让调用方报告「已退出」而非「同步 0 / 失败」。这是
    # seed_profile_skills() 针对命名配置文件的标记检查在默认配置
    # 文件上的对应物。
    if (HERMES_HOME / NO_BUNDLED_SKILLS_MARKER).exists():
        if not quiet:
            print("  (skipped — profile opted out of bundled skills via .no-bundled-skills)")
        return {
            "copied": [], "updated": [], "skipped": 0,
            "user_modified": [], "cleaned": [], "total_bundled": 0,
            "optional_provenance_backfilled": [], "skipped_opt_out": True,
        }

    bundled_dir = _get_bundled_dir()
    if not bundled_dir.exists():
        return {
            "copied": [], "updated": [], "skipped": 0,
            "user_modified": [], "cleaned": [], "suppressed": [], "total_bundled": 0,
            "optional_provenance_backfilled": [],
        }

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = _read_manifest()
    bundled_skills = _discover_bundled_skills(bundled_dir)
    bundled_names = {name for name, _ in bundled_skills}
    suppressed = _read_suppressed_names()

    copied = []
    updated = []
    user_modified = []
    suppressed_skipped: List[str] = []
    skipped = 0

    for skill_name, skill_src in bundled_skills:
        # curator 修剪过的内置技能：不重新播种。抑制列表
        # （~/.hermes/skills/.curator_suppressed）在 curator 归档某个
        # 内置技能且 curator.prune_builtins 启用时写入。若不跳过，每
        # 次 `hermes update` 都会让用户刻意修剪掉的技能死灰复燃。
        # 恢复该技能会清除其抑制条目。
        if skill_name in suppressed:
            suppressed_skipped.append(skill_name)
            continue

        dest = _compute_relative_dest(skill_src, bundled_dir)
        bundled_hash = _dir_hash(skill_src)

        # 在分类前先恢复孤立的备份。若上一次更新在「把 dest 移到一旁」
        # 和「拷入新版本」之间被打断，用户唯一的副本就在 ``dest.bak``
        # 里，而 dest 已不存在 —— 若不恢复，下面「在清单中但磁盘上
        # 没有」分支会把该技能误判为用户删除，于是它悄无声息地从
        # 发现结果中消失。
        _orphan = dest.with_suffix(".bak")
        if _orphan.exists() and not dest.exists():
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(_orphan), str(dest))
                logger.info("Recovered orphaned skill backup: %s", _orphan)
            except (OSError, IOError):
                logger.warning(
                    "Could not recover orphaned skill backup %s", _orphan,
                    exc_info=True,
                )

        if skill_name not in manifest:
            # ── 新技能 —— 从未提供过 ──
            try:
                if dest.exists():
                    # 用户已有一个同名技能 —— 不覆盖。仅当磁盘上的副本
                    # 与内置逐字节一致时（例如重置后重新同步，或恰好
                    # 相同的安装）才记入清单；这种情况无害可跟踪。若
                    # 副本不同（自定义技能、hub 安装或用户编辑过），
                    # 跳过清单写入：在那里记录 bundled_hash 会让
                    # user_hash != origin_hash 在随后的每次同步中都被
                    # 读作「用户改动」，从而毒化更新检测，永久阻断
                    # 内置更新。
                    skipped += 1
                    if _dir_hash(dest) == bundled_hash:
                        manifest[skill_name] = bundled_hash
                    elif not quiet:
                        print(
                            f"  ⚠ {skill_name}: bundled version shipped but you "
                            f"already have a local skill by this name — yours "
                            f"was kept. Run `hermes skills reset {skill_name}` "
                            f"to replace it with the bundled version."
                        )
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(skill_src, dest)
                    copied.append(skill_name)
                    manifest[skill_name] = bundled_hash
                    if not quiet:
                        print(f"  + {skill_name}")
            except (OSError, IOError) as e:
                if not quiet:
                    print(f"  ! Failed to copy {skill_name}: {e}")
                # 不要加入清单 —— 下次同步应重试

        elif dest.exists():
            # ── 既有技能 —— 在清单中且磁盘上存在 ──
            origin_hash = manifest.get(skill_name, "")
            user_hash = _dir_hash(dest)

            if not origin_hash:
                # v1 迁移：没有记录来源哈希。用用户当前副本设定基线，
                # 以便后续同步能检测改动。
                manifest[skill_name] = user_hash
                if user_hash == bundled_hash:
                    skipped += 1  # 已同步
                else:
                    # 无法判断是用户改动还是内置变化 —— 保险起见跳过
                    skipped += 1
                continue

            if _is_tracked_user_modification(origin_hash, user_hash):
                # 用户改动了该技能 —— 不覆盖他们的改动
                user_modified.append(skill_name)
                if not quiet:
                    print(f"  ~ {skill_name} (user-modified, skipping)")
                continue

            # 用户副本与来源一致 —— 检查内置是否有更新版本
            if bundled_hash != origin_hash:
                try:
                    # 把旧副本移到备份，以便失败时恢复
                    backup = dest.with_suffix(".bak")
                    # 早先失败留下的陈旧备份会让 shutil.move() 把
                    # dest 嵌套进它里面（或直接失败），并毒化下方的
                    # 恢复路径。当前 dest 才是权威副本 —— 清掉遗留。
                    if backup.exists():
                        _rmtree_writable(backup)
                    shutil.move(str(dest), str(backup))
                    try:
                        shutil.copytree(skill_src, dest)
                        manifest[skill_name] = bundled_hash
                        updated.append(skill_name)
                        if not quiet:
                            print(f"  ↑ {skill_name} (updated)")
                        # 拷贝成功后移除备份
                        try:
                            _rmtree_writable(backup)
                        except (OSError, IOError):
                            logger.debug("Could not remove backup %s", backup, exc_info=True)
                    except (OSError, IOError):
                        # 从备份恢复。写了一半的 dest 绝不能遮蔽用户
                        # 副本或阻碍恢复 —— 先清掉它，再把备份移回原位。
                        if backup.exists():
                            if dest.exists():
                                try:
                                    _rmtree_writable(dest)
                                except (OSError, IOError):
                                    logger.warning(
                                        "Could not clear partial copy %s during restore",
                                        dest, exc_info=True,
                                    )
                            if not dest.exists():
                                shutil.move(str(backup), str(dest))
                        raise
                except (OSError, IOError) as e:
                    if not quiet:
                        print(f"  ! Failed to update {skill_name}: {e}")
            else:
                skipped += 1  # 内置未变，用户未变

        else:
            # ── 在清单中但磁盘上不存在 —— 用户删除了它 ──
            skipped += 1

    # 清理陈旧的清单条目（已从内置目录移除的技能）
    cleaned = sorted(set(manifest.keys()) - bundled_names)
    for name in cleaned:
        del manifest[name]

    # 同时拷贝分类的 DESCRIPTION.md 文件（若尚未存在）
    for desc_md in bundled_dir.rglob("DESCRIPTION.md"):
        rel = desc_md.relative_to(bundled_dir)
        dest_desc = SKILLS_DIR / rel
        if not dest_desc.exists():
            try:
                dest_desc.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(desc_md, dest_desc)
            except (OSError, IOError) as e:
                logger.debug("Could not copy %s: %s", desc_md, e)

    _write_manifest(manifest)
    optional_provenance_backfilled = _backfill_optional_provenance(quiet=quiet)

    return {
        "copied": copied,
        "updated": updated,
        "skipped": skipped,
        "user_modified": user_modified,
        "cleaned": cleaned,
        "suppressed": suppressed_skipped,
        "total_bundled": len(bundled_skills),
        "optional_provenance_backfilled": optional_provenance_backfilled,
    }


def _rmtree_writable(path: Path) -> None:
    """移除一个目录树，先把只读条目改为可写。

    处理不可变包源（Nix store、deb/rpm 安装）—— 它们对拷贝出来的
    文件*和*目录都保留只读权限（``r-xr-xr-x``）。删除子项需要其父
    目录的写权限，因此重试处理器在再次尝试前会把失败路径**及其
    父目录**都改为可写。参见 #34860、#34972。
    """
    # 纵深防御（#48200）：拒绝 rmtree ``HERMES_HOME/skills/`` 之外的
    # 任何东西，以防早先某次事故中出现的灾难性清空
    # ``~/.hermes/``（``.env``、``MEMORY.md``、``kanban.db``、自定义
    # 技能、脚本……）。本文件中有五处调用点使用此辅助函数；只要其中
    # 任何一处——通过糟糕的路径拼接、缺失的 ``HERMES_HOME`` 默认值、
    # 恶意的内置清单条目，或飞行途中异常留下的陈旧路径——把目标算
    # 出了技能根之外，这道防线就会把随之而来的
    # ``shutil.rmtree(~/.hermes)`` 变成响亮、可恢复的
    # ``ValueError``，而非静默销毁用户的安装。
    target = Path(path).resolve()
    skills_root = SKILLS_DIR.resolve()
    # 每个合法调用方传入的都是一个技能目录或其 ``.bak`` 兄弟目录 ——
    # 永远是技能根的严格子项。技能根本身绝不能移除：一个坍缩为
    # ``SKILLS_DIR`` 的 ``dest``（例如相对路径解析为 ``.``）会清空
    # 每个已安装技能，而其 ``.bak`` 兄弟目录会落到上一层的
    # ``HERMES_HOME`` 里。要求严格子项关系，从而无论逃逸进技能根
    # 还是逃逸出技能根都被拒绝。
    if skills_root not in target.parents:
        raise ValueError(
            f"refusing to rmtree {target!r}: not strictly under {skills_root!r} "
            f"(scope guard — see #48200)"
        )
    import stat

    def _on_error(func, fpath, exc_info):
        # 删除子项需要其父目录可写，因此对失败路径和其父目录都做
        # chmod，然后重试。
        for target in (os.path.dirname(fpath), fpath):
            try:
                os.chmod(target, stat.S_IRWXU)
            except OSError:
                pass
        func(fpath)

    shutil.rmtree(path, onerror=_on_error)


def reset_bundled_skill(name: str, restore: bool = False) -> dict:
    """
    重置某个内置技能的清单跟踪，使后续同步正常工作。

    当用户编辑了某个内置技能，后续同步会把它标记为
    ``user_modified`` 并永远跳过 —— 即便用户后来把内置版本原样拷回，
    因为清单里仍持有*旧*的来源哈希。本函数打破该循环。

    参数：
        name: 技能名（与清单键 / 技能 frontmatter 名一致）。
        restore: 若为 True，还删除用户在 SKILLS_DIR 的副本，让下次
                 同步重新拷贝当前内置版本。若为 False（默认），仅清
                 除清单条目 —— 用户当前副本被保留，但未来的更新重新
                 生效。

    返回：
        dict，键为：
          - ok: bool，重置是否成功
          - action: 取值为 "manifest_cleared"、"restored"、
                    "not_in_manifest"、"bundled_missing" 之一
          - message: 人类可读的描述
          - synced: 若触发了同步则为 sync_skills() 的 dict，否则为 None
    """
    manifest = _read_manifest()
    bundled_dir = _get_bundled_dir()
    bundled_skills = _discover_bundled_skills(bundled_dir)
    bundled_by_name = dict(bundled_skills)

    in_manifest = name in manifest
    is_bundled = name in bundled_by_name

    if not in_manifest and not is_bundled:
        return {
            "ok": False,
            "action": "not_in_manifest",
            "message": (
                f"'{name}' is not a tracked bundled skill. Nothing to reset. "
                f"(Hub-installed skills use `hermes skills uninstall`.)"
            ),
            "synced": None,
        }

    # 第 1 步（可选）：删除用户副本，以便下次同步重新拷贝内置。
    # 必须在清单删除之前进行，这样 rmtree 失败时不会让技能陷入
    # 无清单的中间状态（参见 #34972）。
    deleted_user_copy = False
    if restore:
        if not is_bundled:
            return {
                "ok": False,
                "action": "bundled_missing",
                "message": (
                    f"'{name}' has no bundled source — manifest entry preserved "
                    f"but cannot restore from bundled (skill was removed upstream)."
                ),
                "synced": None,
            }
        dest = _compute_relative_dest(bundled_by_name[name], bundled_dir)
        if dest.exists():
            try:
                _rmtree_writable(dest)
                deleted_user_copy = True
            except (OSError, IOError) as e:
                return {
                    "ok": False,
                    "action": "not_reset",
                    "message": (
                        f"Could not delete user copy at {dest}: {e}. "
                        f"Manifest entry preserved — nothing was changed."
                    ),
                    "synced": None,
                }

    # 第 2 步：删除清单条目，以便下次同步把它当作新技能
    if in_manifest:
        del manifest[name]
        _write_manifest(manifest)

    # 第 3 步：运行同步重新设定基线（或若我们删除过则重新拷贝）
    synced = sync_skills(quiet=True)

    if restore and deleted_user_copy:
        action = "restored"
        message = f"Restored '{name}' from bundled source."
    elif restore:
        # 磁盘上没有可删除的东西，但我们重新同步了 —— 行为类似全新安装
        action = "restored"
        message = f"Restored '{name}' (no prior user copy, re-copied from bundled)."
    else:
        action = "manifest_cleared"
        message = (
            f"Cleared manifest entry for '{name}'. Future `hermes update` runs "
            f"will re-baseline against your current copy and accept upstream changes."
        )

    return {"ok": True, "action": action, "message": message, "synced": synced}


def _is_tracked_user_modification(origin_hash: str, user_hash: str) -> bool:
    """磁盘上的某技能是否算作 ``hermes update`` 保留的用户改动。

    由同步循环（决定跳过什么）和 ``list_user_modified_bundled_skills``
    （暴露名称）共享，使两者永不会漂移。仅当技能有记录的来源哈希
    （未设定基线 / v1 条目带空哈希则不算）且其当前内容哈希与该来源
    不同时，才算作被跟踪的改动。
    """
    return bool(origin_hash) and user_hash != origin_hash


def list_user_modified_bundled_skills() -> List[dict]:
    """返回 ``hermes update`` 因用户本地编辑而保留的内置技能。

    当某技能的磁盘副本不再与清单中上次同步时记录的来源哈希匹配，
    即算作用户改动 —— 这正是同步循环用来决定跳过什么的判据。这是
    该行为的发现侧，让用户能找到 ``~ N user-modified (kept)`` 提示
    实际计入的那些名称。

    返回按名称排序的 dict 列表：
        ``{"name": str, "dest": Path, "bundled_src": Path}``
    其中 ``dest`` 是用户副本，``bundled_src`` 是当前内置副本（便于
    调用方 diff 或恢复）。
    """
    manifest = _read_manifest()
    if not manifest:
        return []
    bundled_dir = _get_bundled_dir()
    modified: List[dict] = []
    for skill_name, skill_dir in _discover_bundled_skills(bundled_dir):
        origin_hash = manifest.get(skill_name, "")
        # 无条目，或尚未设定基线的 v1 条目（空哈希）：不是被跟踪的
        # 改动 —— 由下次同步处理。
        if not origin_hash:
            continue
        dest = _compute_relative_dest(skill_dir, bundled_dir)
        if not dest.exists():
            continue
        if _is_tracked_user_modification(origin_hash, _dir_hash(dest)):
            modified.append(
                {"name": skill_name, "dest": dest, "bundled_src": skill_dir}
            )
    modified.sort(key=lambda e: e["name"])
    return modified


def _read_for_diff(path: Path) -> Tuple[Optional[bytes], Optional[str]]:
    """为 diff 一次性读取文件。

    返回 ``(raw_bytes, text)``，其中文件为二进制时 ``text`` 为
    ``None``；无法读取时为 ``(None, None)``。返回原始字节让调用方
    无需重读即可比较二进制文件。
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None, None
    if b"\x00" in data:
        return data, None
    try:
        return data, data.decode("utf-8")
    except UnicodeDecodeError:
        return data, None


def diff_bundled_skill(name: str) -> dict:
    """把用户副本与当前内置版本做 diff。

    让用户在决定保留自己的编辑还是 ``hermes skills reset`` 回到上游
    之前，看清到底有哪些分歧。

    返回一个 dict：
        ``ok``（bool）、``name``（str）、``found``（bool —— 内置源
        是否存在）、``modified``（bool）、``message``（str）、
        ``diffs``：``{"path": str, "status": str, "diff": str}`` 的
        列表，其中 status 取值为 ``modified`` / ``added``（仅用户副本
        有）/ ``removed``（仅内置有）/ ``binary`` 之一。
    """
    import difflib

    bundled_dir = _get_bundled_dir()
    bundled_by_name = dict(_discover_bundled_skills(bundled_dir))
    bundled_src = bundled_by_name.get(name)
    if bundled_src is None:
        return {
            "ok": False,
            "name": name,
            "found": False,
            "modified": False,
            "diffs": [],
            "message": (
                f"'{name}' is not a tracked bundled skill (no stock version to "
                f"diff against). Hub-installed skills use `hermes skills inspect`."
            ),
        }
    dest = _compute_relative_dest(bundled_src, bundled_dir)
    if not dest.exists():
        return {
            "ok": False,
            "name": name,
            "found": True,
            "modified": False,
            "diffs": [],
            "message": f"No local copy of '{name}' found at {dest}.",
        }

    user_files = set(_skill_file_list(dest))
    stock_files = set(_skill_file_list(bundled_src))

    diffs: List[dict] = []
    for rel in sorted(user_files | stock_files):
        in_user = rel in user_files
        in_stock = rel in stock_files
        user_bytes, user_text = (
            _read_for_diff(dest / rel) if in_user else (None, None)
        )
        stock_bytes, stock_text = (
            _read_for_diff(bundled_src / rel) if in_stock else (None, None)
        )

        if in_user and in_stock:
            if user_text is None or stock_text is None:
                # 至少一侧是二进制 —— 仅在字节不同时报告
                # （复用上面已读到的字节，不再读第二次）。
                if user_bytes != stock_bytes:
                    diffs.append(
                        {"path": rel, "status": "binary", "diff": "<binary file differs>"}
                    )
                continue
            if user_text == stock_text:
                continue
            text = "".join(
                difflib.unified_diff(
                    stock_text.splitlines(keepends=True),
                    user_text.splitlines(keepends=True),
                    fromfile=f"stock/{rel}",
                    tofile=f"yours/{rel}",
                )
            )
            diffs.append({"path": rel, "status": "modified", "diff": text})
        elif in_user:
            diffs.append(
                {"path": rel, "status": "added", "diff": f"+ only in your copy: {rel}"}
            )
        else:
            diffs.append(
                {"path": rel, "status": "removed", "diff": f"- only in stock: {rel}"}
            )

    modified = bool(diffs)
    return {
        "ok": True,
        "name": name,
        "found": True,
        "modified": modified,
        "diffs": diffs,
        "message": (
            f"'{name}' matches the stock version."
            if not modified
            else f"'{name}' differs from the stock version in {len(diffs)} file(s)."
        ),
    }


def set_bundled_skills_opt_out(enabled: bool) -> dict:
    """切换当前配置文件的 .no-bundled-skills 退出标记。

    当 ``enabled`` 为 True 时，写入 HERMES_HOME/.no-bundled-skills，
    使安装器、``hermes update`` 以及任何直接同步都停止播种内置技能。
    为 False 时移除该标记，使下次同步恢复播种。这是
    ``hermes skills opt-out`` / ``opt-in`` 的磁盘状态半部分；移除
    已存在的技能是独立的、显式步骤（见
    ``remove_pristine_bundled_skills``）。

    返回：
        dict，键为：ok（bool）、changed（bool）、marker（str 路径）、
                        message（str）。
    """
    marker = HERMES_HOME / NO_BUNDLED_SKILLS_MARKER
    existed = marker.exists()
    try:
        if enabled:
            HERMES_HOME.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                "This profile opted out of bundled-skill seeding "
                "(`hermes skills opt-out`).\n"
                "Delete this file to re-enable sync on the next `hermes update`.\n",
                encoding="utf-8",
            )
            changed = not existed
            message = (
                "Opted out of bundled skills. Future install / update / sync "
                "runs will not seed bundled skills into this profile."
                if changed
                else "Already opted out — marker was already present."
            )
        else:
            if existed:
                marker.unlink()
            changed = existed
            message = (
                "Opted back in. The next `hermes update` (or `hermes skills "
                "opt-in --sync`) will re-seed bundled skills."
                if changed
                else "Not opted out — no marker to remove."
            )
    except OSError as e:
        return {
            "ok": False, "changed": False, "marker": str(marker),
            "message": f"Could not update opt-out marker at {marker}: {e}",
        }
    return {"ok": True, "changed": changed, "marker": str(marker), "message": message}


def is_bundled_skills_opt_out() -> bool:
    """当前配置文件是否带有退出标记，是则返回 True。"""
    return (HERMES_HOME / NO_BUNDLED_SKILLS_MARKER).exists()


def remove_pristine_bundled_skills(dry_run: bool = False) -> dict:
    """删除存在、被清单跟踪且未被改动的内置技能。

    安全是本函数的全部意义。只有当下列条件全部满足时，才会移除
    磁盘上的某技能：
      - 它记录在同步清单里（即确为内置技能，而非 hub 安装或手写），
        且
      - 它仍存在于内置源中（这样我们才能做哈希比较），且
      - 它的磁盘副本与清单来源哈希逐字节一致（即用户未编辑过）。

    任何用户改动过、hub 安装或本地编写的技能都原样保留，并在
    ``skipped`` 中报告。每个被移除技能的清单条目都会被丢弃，这样
    之后的重新开启播种会把它当作新技能。

    参数：
        dry_run: 为 True 时，只计算会被移除什么而不实际删除。

    返回：
        dict，键为：ok（bool）、removed（list[str]）、
                        skipped（list[dict]），其中每个 dict 为
                        {name, reason}、dry_run（bool）、message（str）。
    """
    manifest = _read_manifest()
    bundled_dir = _get_bundled_dir()
    bundled_by_name = dict(_discover_bundled_skills(bundled_dir))

    removed: List[str] = []
    skipped: List[dict] = []

    for name, origin_hash in sorted(manifest.items()):
        src = bundled_by_name.get(name)
        if src is None:
            # 被跟踪但上游已不再内置 —— 保留；轮不到我们评判。
            skipped.append({"name": name, "reason": "no bundled source (removed upstream)"})
            continue
        dest = _compute_relative_dest(src, bundled_dir)
        if not dest.exists():
            # 磁盘上已不存在；仅遗忘陈旧的清单条目。
            if not dry_run and name in manifest:
                del manifest[name]
            continue
        on_disk = _dir_hash(dest)
        if on_disk != origin_hash:
            skipped.append({"name": name, "reason": "user-modified (kept)"})
            continue
        # 干净的内置副本 —— 可安全移除。
        if dry_run:
            removed.append(name)
            continue
        try:
            _rmtree_writable(dest)
        except (OSError, IOError) as e:
            skipped.append({"name": name, "reason": f"delete failed: {e}"})
            continue
        if name in manifest:
            del manifest[name]
        removed.append(name)

    if not dry_run and removed:
        _write_manifest(manifest)

    verb = "Would remove" if dry_run else "Removed"
    message = f"{verb} {len(removed)} pristine bundled skill(s); kept {len(skipped)}."
    return {
        "ok": True, "removed": removed, "skipped": skipped,
        "dry_run": dry_run, "message": message,
    }


if __name__ == "__main__":
    print("Syncing bundled skills into ~/.hermes/skills/ ...")
    result = sync_skills(quiet=False)
    parts = [
        f"{len(result['copied'])} new",
        f"{len(result['updated'])} updated",
        f"{result['skipped']} unchanged",
    ]
    if result["user_modified"]:
        names = result["user_modified"]
        MAX_SHOW = 5
        shown = ", ".join(names[:MAX_SHOW])
        if len(names) > MAX_SHOW:
            shown += f", +{len(names) - MAX_SHOW} more"
        parts.append(f"{len(names)} user-modified (kept): {shown}")
    if result["cleaned"]:
        parts.append(f"{len(result['cleaned'])} cleaned from manifest")
    if result.get("optional_provenance_backfilled"):
        parts.append(f"{len(result['optional_provenance_backfilled'])} official optional backfilled")
    print(f"\nDone: {', '.join(parts)}. {result['total_bundled']} total bundled.")
