"""
hermes CLI 的备份和导入命令。

`hermes backup` 创建整个 ~/.hermes/ 目录的 zip 归档
（不包括 hermes-agent 仓库和临时文件）。

`hermes import` 从备份 zip 中恢复，覆盖到当前 HERMES_HOME 根目录。
"""

import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_default_hermes_root, get_hermes_home, display_hermes_home

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 排除规则
# ---------------------------------------------------------------------------

# 要完全跳过的目录名（与每个路径组件匹配）
# ``hermes-agent`` 在 ``_should_exclude`` 中仅在根级别做特殊处理，
# 以防止 ``skills/autonomous-ai-agents/hermes-agent/`` 等技能目录被意外排除。
#
# 以下依赖/缓存条目的重要性不只是整洁：若不排除它们，
# 位于 HERMES_HOME 下的单个插件 venv、MCP 服务器安装或 pip/uv 缓存
# 会被逐文件遍历，使备份膨胀到数十万条目，耗时数小时 ——
# 正是用户遇到的"备份卡几天 / 426543 个文件"症状。
# 依赖/测试环境名称大多镜像 ``agent.skill_utils.EXCLUDED_SKILL_DIRS``
# （项目规范的"可重新生成目录"集合）；``.cache`` 是额外的仅备份条目，
# 涵盖了广泛的可重新生成缓存约定（pip/uv 等），技能扫描器无需剪枝，
# 但备份遍历需要。我们有意不在此排除 ``.archive``，
# 因为策展器的 ``skills/.archive/`` 存放可恢复的用户技能，必须保留在备份中。
_EXCLUDED_DIRS = {
    "hermes-agent",     # 代码库仓库 — 改为重新克隆
    "__pycache__",      # 字节码缓存 — 导入时自动重新生成
    ".git",             # 嵌套 git 目录（profile 不应有，但安全起见排除）
    "node_modules",     # JS 依赖 — 按需重新安装
    "backups",          # 之前的自动备份 — 避免备份指数级嵌套
    "checkpoints",      # 会话本地轨迹缓存 — 每次会话重新生成，
                        # 以会话哈希为键，无论如何无法迁移到另一台机器
    # Python 依赖树（HERMES_HOME 下的插件 / MCP 服务器 venv）—
    # 重新安装即可重新生成；绝非不可替代的状态。
    ".venv",
    "venv",
    "site-packages",
    # 工具 / 构建缓存 — 全部可重新生成。
    ".cache",
    ".tox",
    ".nox",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}

# 要跳过的文件名后缀
_EXCLUDED_SUFFIXES = (
    ".pyc",
    ".pyo",
    # SQLite 附属文件 — 备份通过 ``sqlite3.backup()`` 对 ``*.db`` 取一致快照，
    # 因此随同发布活跃的 WAL / 共享内存 / 回滚日志会将新鲜快照与陈旧附属文件
    # 配对，导致下次打开时恢复出现撕裂。这些文件是临时的，
    # 首次连接时会自动重新生成。
    ".db-wal",
    ".db-shm",
    ".db-journal",
)

# 要跳过的文件名（在另一台机器上毫无意义的运行时状态）
_EXCLUDED_NAMES = {
    "gateway.pid",
    "cron.pid",
}

# ``hermes import`` 绝不能覆盖的文件名，按 basename 匹配，
# 以便同时捕获根 profile（``gateway_state.json``）和命名 profile
# （``profiles/<name>/gateway_state.json``）。
#
# 这些文件保存*以备份源机器或容器为命名空间的易失 gateway/进程运行时状态* ——
# 已死进程命名空间中的 PID、运行时锁、进程注册表，
# 以及 gateway 最后记录的运行/期望状态。将它们恢复到另一台主机（或托管容器）
# 轻则毫无意义，重则有害：
#
#   - ``gateway_state.json`` 驱动容器启动协调器
#     (``container_boot._read_desired_state``)，后者仅自动启动
#     记录状态为 ``running`` 的 gateway。从 gateway 已停止的机器
#     获取的备份（或携带过时/外来值）会覆盖容器自身状态，
#     使 gateway 陷入"starting"/"cooking"卡死状态，
#     断开与 Nous 门户的连接（NS-508 / NS-501 的后半部分）。
#   - ``gateway.pid`` / ``cron.pid`` / ``gateway.lock`` / ``processes.json``
#     引用了*源*机器进程命名空间中的 PID 和锁；
#     新环境中数值相等的 PID 是不同的进程。
#     这些文件恰好镜像了 ``container_boot._STALE_RUNTIME_FILES``
#     在每次容器启动时已清扫的内容。
#
# 较旧的备份早于备份端排除规则，因此导入时也要过滤，
# 而不是信任归档内容。
_IMPORT_SKIP_NAMES = {
    "gateway_state.json",
    "gateway.pid",
    "cron.pid",
    "gateway.lock",
    "processes.json",
}

# zipfile.open() 解压时丢失 Unix 权限位；恢复时将其收紧为 0600。
_SECRET_FILE_NAMES = {".env", "auth.json", "state.db"}

# 为存在于 HERMES_HOME 之外的 provider 状态保留的归档子树
#（例如 ~/.honcho、~/.hindsight）。活跃内存 provider 通过
# MemoryProvider.backup_paths() 声明这些路径；它们以相对于用户主目录的编码
# 存储在此前缀下，并在导入时恢复到原始的相对主目录位置。
# 不在主目录下的内容会被跳过。
_EXTERNAL_PREFIX = "_external/"


def _collect_memory_provider_external_paths() -> List[Path]:
    """返回活跃内存 provider 在 HERMES_HOME 之外存储的现有绝对路径，
    仅从配置解析（无网络、无初始化）。

    从配置读取 ``memory.provider``，仅加载该 provider，
    并请求其 ``backup_paths()``。当无外部 provider 活跃或无法加载时返回空列表 ——
    备份绝不能因不稳定的插件而失败。
    """
    try:
        from plugins.memory import _get_active_memory_provider, load_memory_provider
    except Exception:
        return []

    try:
        active = _get_active_memory_provider()
    except Exception:
        active = None
    if not active:
        return []

    try:
        provider = load_memory_provider(active)
    except Exception:
        provider = None
    if provider is None:
        return []

    try:
        declared = provider.backup_paths() or []
    except Exception as exc:
        logger.warning("backup_paths() failed for memory provider %r: %s", active, exc)
        return []

    out: List[Path] = []
    seen: set = set()
    for raw in declared:
        try:
            p = Path(raw).expanduser()
        except Exception:
            continue
        if not p.exists():
            continue
        try:
            resolved = p.resolve()
        except (OSError, ValueError):
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(p)
    return out


def _iter_external_files(base: Path) -> List[Path]:
    """生成 *base*（文件或目录）下的普通文件，跳过符号链接、缓存和 pyc 文件。
    *base* 本身可以是文件。"""
    files: List[Path] = []
    if base.is_file() and not base.is_symlink():
        files.append(base)
        return files
    if not base.is_dir():
        return files
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dp = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
        for fname in filenames:
            fpath = dp / fname
            if fpath.is_symlink():
                continue
            if fpath.name in _EXCLUDED_NAMES or fpath.name.endswith(_EXCLUDED_SUFFIXES):
                continue
            files.append(fpath)
    return files


def _should_exclude(rel_path: Path) -> bool:
    """如果 *rel_path*（相对于 hermes 根目录）应被跳过则返回 True。"""
    parts = rel_path.parts

    for part in parts:
        if part not in _EXCLUDED_DIRS:
            continue
        # ``hermes-agent`` 仅在根级别（第一个组件）匹配。
        # 同名的嵌套目录 — 例如
        # ``skills/autonomous-ai-agents/hermes-agent/`` — 必须保留。
        if part == "hermes-agent" and part != parts[0]:
            continue
        return True

    name = rel_path.name

    if name in _EXCLUDED_NAMES:
        return True

    if name.endswith(_EXCLUDED_SUFFIXES):
        return True

    return False


def _should_skip_backup_file(abs_path: Path, rel_path: Path, out_path: Path) -> bool:
    """当候选文件不应写入备份 zip 时返回 True。"""
    if _should_exclude(rel_path):
        return True

    # zipfile.write() 会跟随文件符号链接，因此在任何归档写入
    # 可能将 HERMES_HOME 之外的数据复制进来之前，先跳过链接。
    if abs_path.is_symlink():
        return True

    try:
        return abs_path.resolve() == out_path.resolve()
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# SQLite 安全复制
# ---------------------------------------------------------------------------

def _safe_copy_db(src: Path, dst: Path) -> bool:
    """使用 backup() API 安全地复制 SQLite 数据库。

    处理 WAL 模式 — 即使数据库正在被写入，也能生成一致的快照。
    失败时回退为原始复制。
    """
    try:
        conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        backup_conn = sqlite3.connect(str(dst))
        conn.backup(backup_conn)
        backup_conn.close()
        conn.close()
        return True
    except Exception as exc:
        logger.warning("SQLite safe copy failed for %s: %s", src, exc)
        try:
            shutil.copy2(src, dst)
            return True
        except Exception as exc2:
            logger.error("Raw copy also failed for %s: %s", src, exc2)
            return False


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------

def _format_size(nbytes: int) -> str:
    """人类可读的文件大小。"""
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}" if unit != "B" else f"{nbytes} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def run_backup(args) -> None:
    """创建 Hermes 主目录的 zip 备份。"""
    hermes_root = get_default_hermes_root()

    if not hermes_root.is_dir():
        print(f"Error: Hermes home directory not found at {hermes_root}")
        sys.exit(1)

    # 确定输出路径
    if args.output:
        out_path = Path(args.output).expanduser().resolve()
        # 如果用户指定了目录，将 zip 放在目录内
        if out_path.is_dir():
            stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            out_path = out_path / f"hermes-backup-{stamp}.zip"
    else:
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        out_path = Path.home() / f"hermes-backup-{stamp}.zip"

    # 确保后缀为 .zip
    if out_path.suffix.lower() != ".zip":
        out_path = out_path.with_suffix(out_path.suffix + ".zip")

    # 确保父目录存在
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 收集文件
    print(f"Scanning {display_hermes_home()} ...")
    files_to_add: list[tuple[Path, Path]] = []  # (absolute, relative)
    skipped_dirs = set()

    for dirpath, dirnames, filenames in os.walk(hermes_root, followlinks=False):
        dp = Path(dirpath)
        rel_dir = dp.relative_to(hermes_root)

        # 就地剪枝已排除目录，使 os.walk 不再深入遍历
        # ``hermes-agent`` 仅在根级别剪枝；同名的嵌套目录
        #（例如 skills/ 中的）必须保留。
        is_root = rel_dir == Path(".")
        orig_dirnames = dirnames[:]
        dirnames[:] = [
            d for d in dirnames
            if d not in _EXCLUDED_DIRS or (d == "hermes-agent" and not is_root)
        ]
        for removed in set(orig_dirnames) - set(dirnames):
            skipped_dirs.add(str(rel_dir / removed))

        for fname in filenames:
            fpath = dp / fname
            rel = fpath.relative_to(hermes_root)

            if _should_skip_backup_file(fpath, rel, out_path):
                continue

            files_to_add.append((fpath, rel))

    # 外部内存 provider 状态（例如 ~/.honcho、~/.hindsight）
    # 位于 HERMES_HOME 之外，因此上面的遍历不会看到它。
    # 向活跃 provider 请求其声明的路径，并将其存储在保留的
    # ``_external/`` 归档前缀下，以相对于用户主目录的方式编码。
    # 仅捕获主目录下的路径（安全性 + 可移植性）；其他内容跳过并记录说明。
    home_dir = Path.home().resolve()
    external_to_add: list[tuple[Path, str]] = []  # (absolute, arcname)
    skipped_external: list[str] = []
    for base in _collect_memory_provider_external_paths():
        try:
            base_resolved = base.resolve()
            base_resolved.relative_to(home_dir)
        except (ValueError, OSError):
            skipped_external.append(str(base))
            continue
        for fpath in _iter_external_files(base):
            try:
                rel_to_home = fpath.resolve().relative_to(home_dir)
            except (ValueError, OSError):
                continue
            arcname = _EXTERNAL_PREFIX + rel_to_home.as_posix()
            external_to_add.append((fpath, arcname))

    if not files_to_add and not external_to_add:
        print("No files to back up.")
        return

    # Create the zip
    file_count = len(files_to_add) + len(external_to_add)
    print(f"Backing up {file_count} files ...")

    total_bytes = 0
    errors = []
    t0 = time.monotonic()

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for i, (abs_path, rel_path) in enumerate(files_to_add, 1):
            try:
                # SQLite 数据库的安全复制（处理 WAL 模式）
                if abs_path.suffix == ".db":
                    # 将快照暂存在输出 zip 旁边，使临时文件处于同一文件系统。
                    # 系统默认路径（/tmp）可能是无法容纳大型数据库的小 tmpfs，
                    # 会导致备份静默不完整。
                    with tempfile.NamedTemporaryFile(
                        suffix=".db", delete=False, dir=str(out_path.parent)
                    ) as tmp:
                        tmp_db = Path(tmp.name)
                    if _safe_copy_db(abs_path, tmp_db):
                        zf.write(tmp_db, arcname=str(rel_path))
                        total_bytes += tmp_db.stat().st_size
                        tmp_db.unlink(missing_ok=True)
                    else:
                        tmp_db.unlink(missing_ok=True)
                        errors.append(f"  {rel_path}: SQLite safe copy failed")
                        continue
                else:
                    zf.write(abs_path, arcname=str(rel_path))
                    total_bytes += abs_path.stat().st_size
            except (PermissionError, OSError, ValueError) as exc:
                errors.append(f"  {rel_path}: {exc}")
                continue

            # 每 500 个文件显示一次进度
            if i % 500 == 0:
                print(f"  {i}/{file_count} files ...")

        # 外部内存 provider 状态，存储在 ``_external/`` 归档前缀下。
        # 实践中这些文件不含 ``.db`` 文件（均为配置/环境 blob），
        # 因此直接使用 zf.write 即可。
        for abs_path, arcname in external_to_add:
            try:
                zf.write(abs_path, arcname=arcname)
                total_bytes += abs_path.stat().st_size
            except (PermissionError, OSError, ValueError) as exc:
                errors.append(f"  {arcname}: {exc}")
                continue

    elapsed = time.monotonic() - t0
    zip_size = out_path.stat().st_size

    # 摘要
    print()
    print(f"Backup complete: {out_path}")
    print(f"  Files:       {file_count}")
    print(f"  Original:    {_format_size(total_bytes)}")
    print(f"  Compressed:  {_format_size(zip_size)}")
    print(f"  Time:        {elapsed:.1f}s")

    if external_to_add:
        print(
            f"\n  Included {len(external_to_add)} memory-provider file(s) "
            f"stored outside {display_hermes_home()}."
        )

    if skipped_external:
        print(
            f"\n  Skipped {len(skipped_external)} memory-provider path(s) "
            f"outside your home directory (not portable):"
        )
        for p in sorted(skipped_external)[:10]:
            print(f"    {p}")

    if skipped_dirs:
        print(f"\n  Excluded directories:")
        for d in sorted(skipped_dirs):
            print(f"    {d}/")

    if errors:
        print(f"\n  Warnings ({len(errors)} files skipped):")
        for e in errors[:10]:
            print(e)
        if len(errors) > 10:
            print(f"  ... and {len(errors) - 10} more")

    print(f"\nRestore with: hermes import {out_path.name}")


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------

def _validate_backup_zip(zf: zipfile.ZipFile) -> tuple[bool, str]:
    """检查 zip 是否为 Hermes 备份。

    返回 (ok, reason)。
    """
    names = zf.namelist()
    if not names:
        return False, "zip archive is empty"

    # 查找 hermes 主目录特有的标志性文件
    markers = {"config.yaml", ".env", "state.db"}
    found = set()
    for n in names:
        # Could be at the root or one level deep (if someone zipped the directory)
        basename = Path(n).name
        if basename in markers:
            found.add(basename)

    if not found:
        return False, (
            "zip does not appear to be a Hermes backup "
            "(no config.yaml, .env, or state databases found)"
        )

    return True, ""


def _detect_prefix(zf: zipfile.ZipFile) -> str:
    """检测 zip 是否有包裹所有条目的公共目录前缀。

    某些工具将路径压缩为 `.hermes/config.yaml` 而非 `config.yaml`。
    返回需要去除的前缀（无则返回空字符串）。
    """
    names = [n for n in zf.namelist() if not n.endswith("/")]
    if not names:
        return ""

    # 查找公共前缀
    parts_list = [Path(n).parts for n in names]

    # 检查所有条目是否共享公共的第一级目录
    first_parts = {p[0] for p in parts_list if len(p) > 1}
    if len(first_parts) == 1:
        prefix = first_parts.pop()
        # 仅在看起来像 hermes 目录名时去除
        if prefix in {".hermes", "hermes"}:
            return prefix + "/"

    return ""


def run_import(args) -> None:
    """从 zip 文件中恢复 Hermes 备份。"""
    zip_path = Path(args.zipfile).expanduser().resolve()

    if not zip_path.is_file():
        print(f"Error: File not found: {zip_path}")
        sys.exit(1)

    if not zipfile.is_zipfile(zip_path):
        print(f"Error: Not a valid zip file: {zip_path}")
        sys.exit(1)

    hermes_root = get_default_hermes_root()

    with zipfile.ZipFile(zip_path, "r") as zf:
        # 验证
        ok, reason = _validate_backup_zip(zf)
        if not ok:
            print(f"Error: {reason}")
            sys.exit(1)

        prefix = _detect_prefix(zf)
        members = [n for n in zf.namelist() if not n.endswith("/")]
        file_count = len(members)

        print(f"Backup contains {file_count} files")
        print(f"Target: {display_hermes_home()}")

        if prefix:
            print(f"Detected archive prefix: {prefix!r} (will be stripped)")

        # 检查是否存在已有安装
        has_config = (hermes_root / "config.yaml").exists()
        has_env = (hermes_root / ".env").exists()

        if (has_config or has_env) and not args.force:
            print()
            print("Warning: Target directory already has Hermes configuration.")
            print("Importing will overwrite existing files with backup contents.")
            print()
            try:
                answer = input("Continue? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                sys.exit(1)
            if answer not in {"y", "yes"}:
                print("Aborted.")
                return

        # 解压
        print(f"\nImporting {file_count} files ...")
        hermes_root.mkdir(parents=True, exist_ok=True)

        errors = []
        restored = 0
        restored_external = 0
        skipped_runtime: list[str] = []
        home_dir = Path.home().resolve()
        t0 = time.monotonic()

        for member in members:
            # 在保留的 ``_external/`` 归档前缀下捕获的外部内存 provider 状态，
            # 恢复到其原始的相对主目录位置（例如 ~/.honcho/config.json），
            # 而非 HERMES_HOME 下。
            if member.startswith(_EXTERNAL_PREFIX):
                ext_rel = member[len(_EXTERNAL_PREFIX):]
                if not ext_rel:
                    continue
                target = home_dir / ext_rel
                # Security: the resolved target must stay under the home dir.
                try:
                    target.resolve().relative_to(home_dir)
                except ValueError:
                    errors.append(f"  {member}: path traversal blocked")
                    continue
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(target, "wb") as dst:
                        dst.write(src.read())
                    # 外部 provider 配置通常包含凭据。
                    if target.suffix in {".json", ".env", ".conf"} or target.name in _SECRET_FILE_NAMES:
                        try:
                            os.chmod(target, 0o600)
                        except OSError:
                            pass
                    restored += 1
                    restored_external += 1
                except (PermissionError, OSError) as exc:
                    errors.append(f"  {member}: {exc}")
                if restored % 500 == 0:
                    print(f"  {restored}/{file_count} files ...")
                continue

            # 如果检测到前缀则去除
            if prefix and member.startswith(prefix):
                rel = member[len(prefix):]
            else:
                rel = member

            if not rel:
                continue

            # 绝不覆盖易失的 gateway/进程运行时状态。这些文件
            # 以备份源机器/容器为命名空间；覆盖它们（尤其是 gateway_state.json）
            # 会破坏目标上的 gateway 协调器，并断开托管实例与 Nous 门户的连接。
            # 按 basename 匹配，同时覆盖根 profile 和命名 profile
            #（profiles/<name>/gateway_state.json）。
            if Path(rel).name in _IMPORT_SKIP_NAMES:
                skipped_runtime.append(rel)
                continue

            target = hermes_root / rel

            # 安全：拒绝绝对路径和路径穿越
            try:
                target.resolve().relative_to(hermes_root.resolve())
            except ValueError:
                errors.append(f"  {rel}: path traversal blocked")
                continue

            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    dst.write(src.read())
                if target.name in _SECRET_FILE_NAMES:
                    os.chmod(target, 0o600)
                restored += 1
            except (PermissionError, OSError) as exc:
                errors.append(f"  {rel}: {exc}")

            if restored % 500 == 0:
                print(f"  {restored}/{file_count} files ...")

        elapsed = time.monotonic() - t0

        # 摘要
        print()
        print(f"Import complete: {restored} files restored in {elapsed:.1f}s")
        print(f"  Target: {display_hermes_home()}")

        if restored_external:
            print(
                f"\n  Restored {restored_external} memory-provider file(s) to "
                f"their original location(s) outside {display_hermes_home()}."
            )

        if errors:
            print(f"\n  Warnings ({len(errors)} files skipped):")
            for e in errors[:10]:
                print(e)
            if len(errors) > 10:
                print(f"  ... and {len(errors) - 10} more")

        if skipped_runtime:
            print(
                f"\n  Preserved {len(skipped_runtime)} runtime state "
                f"file(s) (kept this machine's, not the backup's):"
            )
            for rel in sorted(skipped_runtime)[:10]:
                print(f"    {rel}")
            if len(skipped_runtime) > 10:
                print(f"    ... and {len(skipped_runtime) - 10} more")

        # 导入后：恢复 profile 包装脚本
        profiles_dir = hermes_root / "profiles"
        restored_profiles = []
        if profiles_dir.is_dir():
            try:
                from hermes_cli.profiles import (
                    create_wrapper_script, check_alias_collision,
                    _is_wrapper_dir_in_path, _get_wrapper_dir,
                )
                for entry in sorted(profiles_dir.iterdir()):
                    if not entry.is_dir():
                        continue
                    profile_name = entry.name
                    # 仅为有配置文件的目录创建包装脚本
                    if not (entry / "config.yaml").exists() and not (entry / ".env").exists():
                        continue
                    collision = check_alias_collision(profile_name)
                    if collision:
                        print(f"  Skipped alias '{profile_name}': {collision}")
                        restored_profiles.append((profile_name, False))
                    else:
                        wrapper = create_wrapper_script(profile_name)
                        restored_profiles.append((profile_name, wrapper is not None))

                if restored_profiles:
                    created = [n for n, ok in restored_profiles if ok]
                    skipped = [n for n, ok in restored_profiles if not ok]
                    if created:
                        print(f"\n  Profile aliases restored: {', '.join(created)}")
                    if skipped:
                        print(f"  Profile aliases skipped:  {', '.join(skipped)}")
                    if not _is_wrapper_dir_in_path():
                        print(f"\n  Note: {_get_wrapper_dir()} is not in your PATH.")
                        print('  Add to your shell config (~/.bashrc or ~/.zshrc):')
                        print('    export PATH="$HOME/.local/bin:$PATH"')
            except ImportError:
                # hermes_cli.profiles might not be available (fresh install)
                if any(profiles_dir.iterdir()):
                    print(f"\n  Profiles detected but aliases could not be created.")
                    print(f"  Run: hermes profile list  (after installing hermes)")

        # 指导信息
        print()
        if not (hermes_root / "hermes-agent").is_dir():
            print("Note: The hermes-agent codebase was not included in the backup.")
            print("  If this is a fresh install, run: hermes update")

        if restored_profiles:
            gw_profiles = [n for n, _ in restored_profiles]
            print("\nTo re-enable gateway services for profiles:")
            for pname in gw_profiles:
                print(f"  hermes -p {pname} gateway install")

        print("Done. Your Hermes configuration has been restored.")


# ---------------------------------------------------------------------------
# 快速状态快照（被 /snapshot 斜杠命令和 hermes backup --quick 使用）
# ---------------------------------------------------------------------------

# 快速快照中包含的关键状态文件（相对于 HERMES_HOME）。
# 其余内容要么是可重新生成的（日志、缓存），要么是单独管理的
#（技能、仓库、sessions/）。
#
# 条目可以是单个文件或目录。目录会被递归捕获；缺失的条目会静默跳过。
# 配对数据存储在 state.db 之外的平台特定 JSON blob 中，
# 因此在此处明确列出 — `hermes update` 在拉取前对此集合进行快照，
# 以便在发生任何问题时可恢复已批准用户列表（issue #15733）。
_QUICK_STATE_FILES = (
    "state.db",
    "config.yaml",
    ".env",
    "auth.json",
    "cron/jobs.json",
    "gateway_state.json",
    "channel_directory.json",
    "channel_aliases.json",
    "processes.json",
    # Pairing stores (generic + per-platform JSONs outside state.db)
    "pairing",                          # 旧版位置 (gateway/pairing.py)
    "platforms/pairing",                # 新版位置 (gateway/pairing.py)
    "feishu_comment_pairing.json",      # 飞书评论订阅配对
)

_QUICK_SNAPSHOTS_DIR = "state-snapshots"
_QUICK_DEFAULT_KEEP = 20


def _quick_snapshot_root(hermes_home: Optional[Path] = None) -> Path:
    home = hermes_home or get_hermes_home()
    return home / _QUICK_SNAPSHOTS_DIR


def create_quick_snapshot(
    label: Optional[str] = None,
    hermes_home: Optional[Path] = None,
    keep: Optional[int] = None,
) -> Optional[str]:
    """创建关键文件的快速状态快照。

    将 STATE_FILES 复制到 state-snapshots/ 下带时间戳的目录中。
    超过保留限制时自动修剪旧快照。

    Returns:
        快照 ID（基于时间戳），若未找到文件则返回 None。
    """
    home = hermes_home or get_hermes_home()
    root = _quick_snapshot_root(home)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    snap_id = f"{ts}-{label}" if label else ts
    snap_dir = root / snap_id
    snap_dir.mkdir(parents=True, exist_ok=True)

    manifest: Dict[str, int] = {}  # rel_path -> file size

    for rel in _QUICK_STATE_FILES:
        src = home / rel
        if not src.exists():
            continue

        if src.is_dir():
            # 遍历目录并在 manifest 中逐一记录每个文件，
            # 以便恢复时可以统一处理。空目录会被跳过（无需快照）。
            for sub in src.rglob("*"):
                if not sub.is_file():
                    continue
                sub_rel = sub.relative_to(home).as_posix()
                dst = snap_dir / sub_rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(sub, dst)
                    manifest[sub_rel] = dst.stat().st_size
                except (OSError, PermissionError) as exc:
                    logger.warning("Could not snapshot %s: %s", sub_rel, exc)
            continue

        if not src.is_file():
            continue

        dst = snap_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)

        try:
            if src.suffix == ".db":
                if not _safe_copy_db(src, dst):
                    continue
            else:
                shutil.copy2(src, dst)
            manifest[rel] = dst.stat().st_size
        except (OSError, PermissionError) as exc:
            logger.warning("Could not snapshot %s: %s", rel, exc)

    if not manifest:
        shutil.rmtree(snap_dir, ignore_errors=True)
        return None

    # 写入 manifest
    meta = {
        "id": snap_id,
        "timestamp": ts,
        "label": label,
        "file_count": len(manifest),
        "total_size": sum(manifest.values()),
        "files": manifest,
    }
    with open(snap_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    # 自动修剪。默认值保持历史 /snapshot 手动行为；
    # 对于已知高频率安全快照（例如更新前）的调用方，可传入较小的 keep 值，
    # 防止大型 state.db 副本无限积累。
    _prune_quick_snapshots(root, keep=_QUICK_DEFAULT_KEEP if keep is None else keep)

    logger.info("State snapshot created: %s (%d files)", snap_id, len(manifest))
    return snap_id


def list_quick_snapshots(
    limit: int = 20,
    hermes_home: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """列出现有的快速状态快照，最新的排在最前。"""
    root = _quick_snapshot_root(hermes_home)
    if not root.exists():
        return []

    results = []
    for d in sorted(root.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        manifest_path = d / "manifest.json"
        if manifest_path.exists():
            try:
                with open(manifest_path, encoding="utf-8") as f:
                    results.append(json.load(f))
            except (json.JSONDecodeError, OSError):
                results.append({"id": d.name, "file_count": 0, "total_size": 0})
        if len(results) >= limit:
            break

    return results


def restore_quick_snapshot(
    snapshot_id: str,
    hermes_home: Optional[Path] = None,
) -> bool:
    """从快速快照恢复状态。

    用快照的副本覆盖当前状态文件。
    至少恢复一个文件时返回 True。
    """
    home = hermes_home or get_hermes_home()
    root = _quick_snapshot_root(home)

    # 安全：拒绝包含路径分隔符或路径穿越序列的 snapshot_id，
    # 确保 `root / snapshot_id` 保持在 root 内部。
    if not snapshot_id or "/" in snapshot_id or "\\" in snapshot_id or snapshot_id in (".", ".."):
        logger.error("Invalid snapshot_id: %s", snapshot_id)
        return False

    snap_dir = root / snapshot_id

    # 确认解析后的路径仍在 root 内部（处理符号链接等）
    try:
        snap_dir.resolve().relative_to(root.resolve())
    except ValueError:
        logger.error("Snapshot path traversal blocked for id: %s", snapshot_id)
        return False

    if not snap_dir.is_dir():
        return False

    manifest_path = snap_dir / "manifest.json"
    if not manifest_path.exists():
        return False

    with open(manifest_path, encoding="utf-8") as f:
        meta = json.load(f)

    restored = 0
    for rel in meta.get("files", {}):
        # 安全：拒绝 manifest 条目中的绝对路径和路径穿越
        src = snap_dir / rel
        try:
            src.resolve().relative_to(snap_dir.resolve())
        except ValueError:
            logger.error("Manifest path traversal blocked: %s", rel)
            continue

        dst = home / rel
        try:
            dst.resolve().relative_to(home.resolve())
        except ValueError:
            logger.error("Manifest path traversal blocked: %s", rel)
            continue

        if not src.exists():
            continue

        dst.parent.mkdir(parents=True, exist_ok=True)

        try:
            if dst.suffix == ".db":
                # 数据库的近原子替换
                tmp = dst.parent / f".{dst.name}.snap_restore"
                shutil.copy2(src, tmp)
                dst.unlink(missing_ok=True)
                shutil.move(str(tmp), str(dst))
            else:
                shutil.copy2(src, dst)
            restored += 1
        except (OSError, PermissionError) as exc:
            logger.error("Failed to restore %s: %s", rel, exc)

    logger.info("Restored %d files from snapshot %s", restored, snapshot_id)
    return restored > 0


# cron 任务数据库在 HERMES_HOME 内的相对路径。与 ``_QUICK_STATE_FILES``
# 中的条目及 ``cron/jobs.py`` 的 ``JOBS_FILE`` 保持同步。
_CRON_JOBS_REL = "cron/jobs.json"


def _count_cron_jobs(path: Path) -> Optional[int]:
    """返回存储在 ``path`` 中的 cron 任务数量。

    磁盘上的规范格式为 ``{"jobs": [...]}``（参见 ``cron/jobs.py``）。
    旧版裸列表格式（``[...]``）也受支持。

    Returns:
        任何*有效且可读*的 JSON 文档的任务数量，
        若文件缺失或无法解析则返回 ``None``。``None`` 表示"未知" ——
        调用方不得将其视为"零任务"，因为操作不可读文件
        可能掩盖用户需要看到的真实损坏。
    """
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        jobs = data.get("jobs", [])
        return len(jobs) if isinstance(jobs, list) else None
    if isinstance(data, list):
        return len(data)
    return None


def restore_cron_jobs_if_emptied(
    snapshot_id: str,
    hermes_home: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """``hermes update`` 期间 cron 任务静默丢失的安全网。

    已观察到配置版本迁移在更新后将 ``cron/jobs.json`` 留在
    有效但为空的状态，静默丢失所有已排期任务（issue #34600）。
    ``cron/jobs.py`` 中现有的格式异常检查无法捕获此情况，
    因为 ``{"jobs": []}`` 是完全有效的 JSON。

    此函数将*当前*任务数量与更新前的快照进行比较。
    若当前文件有**零**个任务而快照记录了**一个或多个**，
    则原地恢复快照中的 ``cron/jobs.json``。

    检查故意保守 — 仅在有明确丢失证据时（快照有任务，当前文件无任务）
    才进行恢复，因此在更新期间/之后真正删除了所有任务的用户
    不会被干涉，且不可读的当前文件（数量 ``None``）保持不变，
    以便真实损坏仍可显现。

    Args:
        snapshot_id: 更新前的快速快照 ID（来自
            :func:`create_quick_snapshot`）。
        hermes_home: Hermes 主目录的覆盖值（用于测试）。

    Returns:
        未执行任何操作时返回 ``None``（常见的正常路径）。
        成功恢复时返回 dict ``{"restored": True, "job_count": N,
        "snapshot_id": ...}``，以便调用方向用户发出警告。
    """
    if not snapshot_id:
        return None

    home = hermes_home or get_hermes_home()
    live_path = home / _CRON_JOBS_REL

    live_count = _count_cron_jobs(live_path)
    # 仅在当前文件可读且为空时采取行动。``None``（缺失或无法解析）
    # 有意保持不变 — 那是用户应该看到的不同故障模式，而非被掩盖。
    if live_count is None or live_count > 0:
        return None

    snap_path = _quick_snapshot_root(home) / snapshot_id / _CRON_JOBS_REL
    snap_count = _count_cron_jobs(snap_path)
    if not snap_count:  # None 或 0 — 无值得恢复的内容
        return None

    try:
        live_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snap_path, live_path)
    except (OSError, PermissionError) as exc:
        logger.error(
            "Cron jobs were emptied during update but auto-restore failed: %s", exc
        )
        return None

    logger.warning(
        "Restored %d cron job(s) from pre-update snapshot %s "
        "(cron/jobs.json was emptied during migration)",
        snap_count,
        snapshot_id,
    )
    return {"restored": True, "job_count": snap_count, "snapshot_id": snapshot_id}


def _prune_quick_snapshots(root: Path, keep: int = _QUICK_DEFAULT_KEEP) -> int:
    """删除超过保留限制的最旧快速快照。返回删除数量。"""
    if not root.exists():
        return 0

    dirs = sorted(
        (d for d in root.iterdir() if d.is_dir()),
        key=lambda d: d.name,
        reverse=True,
    )

    deleted = 0
    for d in dirs[keep:]:
        try:
            shutil.rmtree(d)
            deleted += 1
        except OSError as exc:
            logger.warning("Failed to prune snapshot %s: %s", d.name, exc)

    return deleted


def prune_quick_snapshots(
    keep: int = _QUICK_DEFAULT_KEEP,
    hermes_home: Optional[Path] = None,
) -> int:
    """手动修剪快速快照。返回删除数量。"""
    return _prune_quick_snapshots(_quick_snapshot_root(hermes_home), keep=keep)


def run_quick_backup(args) -> None:
    """hermes backup --quick 的 CLI 入口点。"""
    label = getattr(args, "label", None)
    snap_id = create_quick_snapshot(label=label)
    if snap_id:
        print(f"State snapshot created: {snap_id}")
        snaps = list_quick_snapshots()
        print(f"  {len(snaps)} snapshot(s) stored in {display_hermes_home()}/state-snapshots/")
        print(f"  Restore with: /snapshot restore {snap_id}")
    else:
        print("No state files found to snapshot.")


# ---------------------------------------------------------------------------
# 共享的全量 zip 备份辅助函数
# ---------------------------------------------------------------------------

def _write_full_zip_backup(out_path: Path, hermes_root: Path) -> Optional[Path]:
    """将 ``hermes_root`` 的全量 zip 快照写入 ``out_path``。

    使用与 :func:`run_backup` 相同的排除规则和 SQLite 安全复制。
    成功时返回输出路径，失败时返回 None（无文件可备份
    或写入错误 — 调用方应呈现结果而不抛出异常）。
    """
    files_to_add: list[tuple[Path, Path]] = []
    try:
        for dirpath, dirnames, filenames in os.walk(hermes_root, followlinks=False):
            dp = Path(dirpath)
            # Prune excluded directories in-place so os.walk doesn't descend
            dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]

            for fname in filenames:
                fpath = dp / fname
                try:
                    rel = fpath.relative_to(hermes_root)
                except ValueError:
                    continue

                if _should_skip_backup_file(fpath, rel, out_path):
                    continue

                files_to_add.append((fpath, rel))
    except OSError as exc:
        logger.warning("全量 zip 备份：遍历失败：%s", exc)
        return None

    if not files_to_add:
        return None

    try:
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for abs_path, rel_path in files_to_add:
                try:
                    if abs_path.suffix == ".db":
                        # 将快照暂存在输出 zip 旁边，使临时文件处于同一文件系统。
                        # 系统默认路径（/tmp）可能是无法容纳大型数据库的小 tmpfs，
                        # 会导致备份静默不完整。
                        with tempfile.NamedTemporaryFile(
                            suffix=".db", delete=False, dir=str(out_path.parent)
                        ) as tmp:
                            tmp_db = Path(tmp.name)
                        try:
                            if _safe_copy_db(abs_path, tmp_db):
                                zf.write(tmp_db, arcname=str(rel_path))
                        finally:
                            tmp_db.unlink(missing_ok=True)
                    else:
                        zf.write(abs_path, arcname=str(rel_path))
                except (PermissionError, OSError, ValueError) as exc:
                    logger.debug("Skipping %s in zip backup: %s", rel_path, exc)
                    continue
    except OSError as exc:
        logger.warning("全量 zip 备份：zip 写入失败：%s", exc)
        # 尽力清理不完整的文件
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    return out_path


# ---------------------------------------------------------------------------
# 更新前自动备份
# ---------------------------------------------------------------------------

_PRE_UPDATE_BACKUPS_DIR = "backups"
_PRE_UPDATE_PREFIX = "pre-update-"
_PRE_UPDATE_DEFAULT_KEEP = 5


def _pre_update_backup_dir(hermes_home: Optional[Path] = None) -> Path:
    home = hermes_home or get_hermes_home()
    return home / _PRE_UPDATE_BACKUPS_DIR


def _prune_pre_update_backups(backup_dir: Path, keep: int) -> int:
    """删除超过保留限制的最旧更新前备份。

    返回删除的文件数量。仅处理匹配 ``pre-update-*.zip`` 的文件，
    因此同目录中的手动 zip 文件不会被触及。

    ``keep`` 被限制为最小值 1，因为此辅助函数仅在新备份写入后立即调用：
    在用户支付磁盘/CPU 成本创建备份后立即删除，会比没有备份更糟
    （且 ``main.py`` 中的包装器仍会为已不存在的文件打印误导性的 ``Saved: <path>``）。
    确实不需要备份的操作者应在配置中设置
    ``updates.pre_update_backup: false`` — 那才是控制创建的开关。
    """
    keep = max(keep, 1)
    if not backup_dir.exists():
        return 0

    backups = sorted(
        (p for p in backup_dir.iterdir()
         if p.is_file() and p.name.startswith(_PRE_UPDATE_PREFIX) and p.suffix.lower() == ".zip"),
        key=lambda p: p.name,
        reverse=True,
    )

    deleted = 0
    for p in backups[keep:]:
        try:
            p.unlink()
            deleted += 1
        except OSError as exc:
            logger.warning("Failed to prune backup %s: %s", p.name, exc)

    return deleted


def create_pre_update_backup(
    hermes_home: Optional[Path] = None,
    keep: int = _PRE_UPDATE_DEFAULT_KEEP,
) -> Optional[Path]:
    """在 ``backups/`` 下创建 HERMES_HOME 的全量 zip 备份。

    镜像 :func:`run_backup`（相同的排除规则，相同的 SQLite 安全复制），
    但写入 ``<HERMES_HOME>/backups/pre-update-<timestamp>.zip``
    并自动修剪旧的更新前备份。

    返回已创建 zip 的路径，若未找到文件或无法创建备份则返回 ``None``。
    永不抛出异常 — 调用方（``hermes update``）即使备份失败也应继续。
    """
    hermes_root = hermes_home or get_default_hermes_root()
    if not hermes_root.is_dir():
        return None

    backup_dir = _pre_update_backup_dir(hermes_root)
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Could not create pre-update backup dir %s: %s", backup_dir, exc)
        return None

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    out_path = backup_dir / f"{_PRE_UPDATE_PREFIX}{stamp}.zip"

    result = _write_full_zip_backup(out_path, hermes_root)
    if result is None:
        return None

    _prune_pre_update_backups(backup_dir, keep=keep)
    return out_path


# ---------------------------------------------------------------------------
# 迁移前自动备份（被 `hermes claw migrate` 使用）
# ---------------------------------------------------------------------------

_PRE_MIGRATION_PREFIX = "pre-migration-"
_PRE_MIGRATION_DEFAULT_KEEP = 5


def _prune_pre_migration_backups(backup_dir: Path, keep: int) -> int:
    """删除超过保留限制的最旧迁移前备份。

    仅处理匹配 ``pre-migration-*.zip`` 的文件，
    因此同目录中的其他备份不会被触及。
    """
    keep = max(keep, 0)
    if not backup_dir.exists():
        return 0

    backups = sorted(
        (p for p in backup_dir.iterdir()
         if p.is_file() and p.name.startswith(_PRE_MIGRATION_PREFIX) and p.suffix.lower() == ".zip"),
        key=lambda p: p.name,
        reverse=True,
    )

    deleted = 0
    for p in backups[keep:]:
        try:
            p.unlink()
            deleted += 1
        except OSError as exc:
            logger.warning("Failed to prune pre-migration backup %s: %s", p.name, exc)

    return deleted


def create_pre_migration_backup(
    hermes_home: Optional[Path] = None,
    keep: int = _PRE_MIGRATION_DEFAULT_KEEP,
) -> Optional[Path]:
    """在 ``hermes claw migrate`` 应用前，在 ``backups/`` 下创建 HERMES_HOME 的全量 zip 备份。

    通过 ``_write_full_zip_backup`` 与 :func:`create_pre_update_backup` 共享实现 ——
    相同的排除规则、相同的 SQLite 安全复制，
    可用 ``hermes import <archive>`` 恢复。写入
    ``<HERMES_HOME>/backups/pre-migration-<timestamp>.zip`` 并自动修剪旧的迁移前备份。

    返回已创建 zip 的路径，若没有内容可备份（全新安装）或写入失败则返回 ``None``。
    永不抛出异常 — 调用方决定是中止还是继续。
    """
    hermes_root = hermes_home or get_default_hermes_root()
    if not hermes_root.is_dir():
        return None

    # 复用共享的 backups/ 目录，以便 `hermes import` 和更新备份列表
    # 也能发现迁移前的归档。
    backup_dir = _pre_update_backup_dir(hermes_root)
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Could not create pre-migration backup dir %s: %s", backup_dir, exc)
        return None

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    out_path = backup_dir / f"{_PRE_MIGRATION_PREFIX}{stamp}.zip"

    result = _write_full_zip_backup(out_path, hermes_root)
    if result is None:
        return None

    _prune_pre_migration_backups(backup_dir, keep=keep)
    return out_path
