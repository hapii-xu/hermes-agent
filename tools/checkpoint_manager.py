"""
检查点管理器 —— 通过单一共享的影子 git 仓库实现透明的文件系统快照。

在执行会改动文件的操作（``write_file``、``patch``、带破坏性参数的
``terminal``）之前，自动为工作目录创建快照，每个会话轮次触发一次。
支持回滚到任意一个历史检查点。

它不是一个工具 —— LLM 永远看不到它。它是受 ``checkpoints`` 配置项或
``--checkpoints`` CLI 标志控制的透明基础设施。

存储布局（单一共享仓库，git 对象在项目之间去重）
-----------------------------------------------------------------------------

    ~/.hermes/checkpoints/
        store/                          — 单一的类裸仓库
            HEAD, config, objects/      — 标准 git 内部结构（共享）
            refs/hermes/<hash16>        — 每个项目的分支 tip
            indexes/<hash16>            — 每个项目的 git index
            projects/<hash16>.json      — {workdir, created_at, last_touch}
            info/exclude                — 默认排除规则（共享）
        .last_prune                     — 自动清理的幂等标记
        legacy-<timestamp>/             — 归档的 v2 之前各项目独立影子
                                          仓库（首次初始化时自动迁移）

为什么用单一仓库？
-------------------

v2 之前的设计为每个工作目录保留一个完整的影子仓库。每个仓库都会在
各自的 ``objects/`` 树下重新存储项目的大部分文件，同一项目的多个
worktree 之间毫无共享。一个用户对同一个 repo 开十几个 worktree，
每个都烧掉约 40 MB（总共约 500 MB），反复存储同样的 blob。单一共享
仓库让 git 基于内容寻址的对象库能在项目和轮次之间去重，于是新增一个
worktree 的成本接近于零。

影子仓库使用 ``GIT_DIR`` + ``GIT_WORK_TREE`` + ``GIT_INDEX_FILE``，
确保不会有任何 git 状态泄漏到用户的项目目录里。

自动维护
--------

影子状态会随时间累积。``prune_checkpoints`` 会删除其记录的工作目录
已不存在（孤儿）或其最后触碰时间早于 ``retention_days``（陈旧）的
引用，然后运行 ``git gc --prune=now`` 回收对象存储。容量上限这一步
会逐个项目丢弃最旧的检查点，直到仓库总大小低于 ``max_total_size_mb``。
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Dict, List, Optional, Set, Tuple

from utils import env_int

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

CHECKPOINT_BASE = get_hermes_home() / "checkpoints"

# CHECKPOINT_BASE 下的单一共享仓库目录。
_STORE_DIRNAME = "store"
_REFS_PREFIX = "refs/hermes"
_INDEXES_DIRNAME = "indexes"
_PROJECTS_DIRNAME = "projects"
_LEGACY_PREFIX = "legacy-"

DEFAULT_EXCLUDES = [
    # 依赖 / 构建产物
    "node_modules/",
    "dist/",
    "build/",
    "target/",
    "out/",
    ".next/",
    ".nuxt/",
    # 缓存
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    ".cache/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    "coverage/",
    ".coverage",
    # 虚拟环境
    ".venv/",
    "venv/",
    "env/",
    # 版本控制（VCS）
    ".git/",
    ".hg/",
    ".svn/",
    # Worktree（Hermes 约定 —— 不递归快照同级 worktree）
    ".worktrees/",
    # 原生 / 编译产物
    "*.so",
    "*.dylib",
    "*.dll",
    "*.o",
    "*.a",
    "*.jar",
    "*.class",
    "*.exe",
    "*.obj",
    # 媒体 / 大体积二进制
    "*.mp4",
    "*.mov",
    "*.mkv",
    "*.webm",
    "*.zip",
    "*.tar",
    "*.tar.gz",
    "*.tgz",
    "*.7z",
    "*.rar",
    "*.iso",
    # 密钥
    ".env",
    ".env.*",
    ".env.local",
    ".env.*.local",
    # 系统垃圾文件
    ".DS_Store",
    "Thumbs.db",
    # 日志
    "*.log",
]

# git 子进程超时（秒）。
_GIT_TIMEOUT: int = max(10, min(60, env_int("HERMES_CHECKPOINT_TIMEOUT", 30)))

# 快照文件数上限 —— 跳过过大的目录以避免拖慢。
_MAX_FILES = 50_000

# 合法的 git commit hash 模式：4–64 位十六进制字符（短或完整 SHA-1/SHA-256）。
_COMMIT_HASH_RE = re.compile(r'^[0-9a-fA-F]{4,64}$')


# ---------------------------------------------------------------------------
# 输入校验辅助函数
# ---------------------------------------------------------------------------

def _validate_commit_hash(commit_hash: str) -> Optional[str]:
    """校验 commit hash，以防止 git 参数注入。

    无效时返回错误字符串，有效时返回 None。
    以「-」开头的取值会被 git 当作标志（例如「--patch」、「-p」），
    而不是版本指定符。
    """
    if not commit_hash or not commit_hash.strip():
        return "Empty commit hash"
    if commit_hash.startswith("-"):
        return f"Invalid commit hash (must not start with '-'): {commit_hash!r}"
    if not _COMMIT_HASH_RE.match(commit_hash):
        return f"Invalid commit hash (expected 4-64 hex characters): {commit_hash!r}"
    return None


def _validate_file_path(file_path: str, working_dir: str) -> Optional[str]:
    """校验文件路径，以防止通过路径穿越逃出工作目录。

    无效时返回错误字符串，有效时返回 None。
    """
    if not file_path or not file_path.strip():
        return "Empty file path"
    if os.path.isabs(file_path):
        return f"File path must be relative, got absolute path: {file_path!r}"
    abs_workdir = _normalize_path(working_dir)
    resolved = (abs_workdir / file_path).resolve()
    try:
        resolved.relative_to(abs_workdir)
    except ValueError:
        return f"File path escapes the working directory via traversal: {file_path!r}"
    return None


# ---------------------------------------------------------------------------
# 路径 / 哈希辅助函数
# ---------------------------------------------------------------------------

def _normalize_path(path_value: str) -> Path:
    """返回检查点操作所用的规范化绝对路径。"""
    return Path(path_value).expanduser().resolve()


def _project_hash(working_dir: str) -> str:
    """确定性的项目级哈希：sha256(abs_path)[:16]。"""
    abs_path = str(_normalize_path(working_dir))
    return hashlib.sha256(abs_path.encode()).hexdigest()[:16]


def _store_path(base: Optional[Path] = None) -> Path:
    """返回单一共享影子仓库的路径。"""
    return (base or CHECKPOINT_BASE) / _STORE_DIRNAME


def _shadow_repo_path(working_dir: str) -> Path:  # pragma: no cover —— 为向后兼容保留
    """返回共享仓库的路径。

    为导入过此辅助函数的调用方/测试保留，用于向后兼容。在 v2 下，影子
    git 存储是跨所有项目共享的 —— 项目级隔离体现在 refs 和 index 里，
    而不是在各自独立的仓库目录里。
    """
    return _store_path()


def _index_path(store: Path, dir_hash: str) -> Path:
    return store / _INDEXES_DIRNAME / dir_hash


def _ref_name(dir_hash: str) -> str:
    return f"{_REFS_PREFIX}/{dir_hash}"


def _project_meta_path(store: Path, dir_hash: str) -> Path:
    return store / _PROJECTS_DIRNAME / f"{dir_hash}.json"


# ---------------------------------------------------------------------------
# Git 环境变量
# ---------------------------------------------------------------------------

def _git_env(
    store: Path,
    working_dir: str,
    index_file: Optional[Path] = None,
) -> dict:
    """构造一个把 git 重定向到共享仓库的环境变量字典。

    共享仓库是 Hermes 的内部基础设施 —— 它绝不能继承用户的全局或
    系统 git 配置。用户级的设置（比如 ``commit.gpgsign = true``、签名
    钩子、凭据助手）要么会破坏后台快照，要么更糟 —— 每写一个文件就
    在会话中途弹出交互式提示（pinentry 的 GUI 窗口）。

    隔离策略：
    * ``GIT_CONFIG_GLOBAL=<os.devnull>`` —— 忽略 ``~/.gitconfig``（git 2.32+）。
    * ``GIT_CONFIG_SYSTEM=<os.devnull>`` —— 忽略 ``/etc/gitconfig``（git 2.32+）。
    * ``GIT_CONFIG_NOSYSTEM=1`` —— 针对更老 git 的双保险。

    若给定 ``index_file``，则强制 git 使用位于 ``store/indexes/<hash>``
    的项目级 index，避免多个项目在共享 index 上争用。
    """
    normalized_working_dir = _normalize_path(working_dir)
    env = os.environ.copy()
    env["GIT_DIR"] = str(store)
    env["GIT_WORK_TREE"] = str(normalized_working_dir)
    env.pop("GIT_NAMESPACE", None)
    env.pop("GIT_ALTERNATE_OBJECT_DIRECTORIES", None)
    if index_file is not None:
        env["GIT_INDEX_FILE"] = str(index_file)
    else:
        env.pop("GIT_INDEX_FILE", None)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _repair_bare_repo_dirs(store: Path) -> None:
    """重建可能被 ``git gc`` 删除的 refs/ 和 branches/ 目录。

    在一个只有 packed refs 的裸仓库上执行 ``git gc --prune=now``，可能
    会删掉空的 ``refs/heads/`` 目录。Git 2.34+ 要求即使所有 ref 都打包
    在 ``packed-refs`` 里，``refs/``（部分版本还要求 ``branches/``）也
    必须存在。少了它们，``git add -A`` 会返回
    ``fatal: not a git repository``，所有检查点操作都会静默失败。
    """
    for subdir in ("refs/heads", "branches"):
        path = store / subdir
        if not path.exists():
            try:
                path.mkdir(parents=True, exist_ok=True)
                logger.debug("Repaired missing %s in checkpoint store", subdir)
            except OSError as exc:
                logger.warning(
                    "Cannot create %s in checkpoint store: %s", subdir, exc,
                )


def _run_git(
    args: List[str],
    store: Path,
    working_dir: str,
    timeout: int = _GIT_TIMEOUT,
    allowed_returncodes: Optional[Set[int]] = None,
    index_file: Optional[Path] = None,
) -> Tuple[bool, str, str]:
    """针对共享仓库运行一条 git 命令。返回 (ok, stdout, stderr)。

    ``allowed_returncodes`` 用于对已知/预期的非零退出码抑制错误日志，
    同时仍保持正常的 ``ok = (returncode == 0)`` 约定。
    例如：``git diff --cached --quiet`` 在存在改动时返回 1。
    """
    normalized_working_dir = _normalize_path(working_dir)
    if not normalized_working_dir.exists():
        msg = f"working directory not found: {normalized_working_dir}"
        logger.error("Git command skipped: %s (%s)", " ".join(["git"] + list(args)), msg)
        return False, "", msg
    if not normalized_working_dir.is_dir():
        msg = f"working directory is not a directory: {normalized_working_dir}"
        logger.error("Git command skipped: %s (%s)", " ".join(["git"] + list(args)), msg)
        return False, "", msg

    env = _git_env(store, str(normalized_working_dir), index_file=index_file)
    cmd = ["git"] + list(args)
    allowed_returncodes = allowed_returncodes or set()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=str(normalized_working_dir),
            stdin=subprocess.DEVNULL,
        )
        ok = result.returncode == 0
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if not ok and result.returncode not in allowed_returncodes:
            logger.error(
                "Git command failed: %s (rc=%d) stderr=%s",
                " ".join(cmd), result.returncode, stderr,
            )
        return ok, stdout, stderr
    except subprocess.TimeoutExpired:
        msg = f"git timed out after {timeout}s: {' '.join(cmd)}"
        logger.error(msg, exc_info=True)
        return False, "", msg
    except FileNotFoundError as exc:
        missing_target = getattr(exc, "filename", None)
        if missing_target == "git":
            logger.error("Git executable not found: %s", " ".join(cmd), exc_info=True)
            return False, "", "git not found"
        msg = f"working directory not found: {normalized_working_dir}"
        logger.error("Git command failed before execution: %s (%s)", " ".join(cmd), msg, exc_info=True)
        return False, "", msg
    except Exception as exc:
        logger.error("Unexpected git error running %s: %s", " ".join(cmd), exc, exc_info=True)
        return False, "", str(exc)


# ---------------------------------------------------------------------------
# 仓库初始化 + 旧版迁移
# ---------------------------------------------------------------------------

def _migrate_legacy_store(base: Path) -> Optional[Path]:
    """把 v2 之前各项目独立的影子仓库挪进一个 ``legacy-<ts>/`` 目录。

    v2 之前的布局是：每个工作目录在 ``CHECKPOINT_BASE`` 下直接对应一个
    影子 git 仓库。v2 布局则希望只有一个 ``store/`` 目录。这里不直接
    删除旧数据（用户可能想恢复），而是把除我们自身 v2 条目之外的所有
    内容重命名进 ``legacy-<timestamp>/``。该 legacy 目录会接受同样的
    保留期清理，也可以用 ``hermes checkpoints clear-legacy`` 手动清空。

    返回 legacy 归档路径；若无可迁移内容，则返回 None。
    """
    if not base.exists():
        return None
    store = _store_path(base)
    legacy_root: Optional[Path] = None
    # 由 v2 管理的保留顶层条目。
    reserved = {_STORE_DIRNAME, _PRUNE_MARKER_NAME}
    for child in list(base.iterdir()):
        name = child.name
        if name in reserved or name.startswith(_LEGACY_PREFIX):
            continue
        # 候选项：v2 之前的影子仓库（含 HEAD）或游离目录。无论哪种，
        # 都先归档，让 v2 从干净状态开始。
        if legacy_root is None:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            legacy_root = base / f"{_LEGACY_PREFIX}{stamp}"
            try:
                legacy_root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("Could not create legacy archive dir: %s", exc)
                return None
        dest = legacy_root / name
        try:
            shutil.move(str(child), str(dest))
        except OSError as exc:
            logger.warning("Could not archive legacy checkpoint %s: %s", child, exc)
    # 如果仓库此时仍未创建，则在这里创建它。
    _ = store
    if legacy_root is not None:
        logger.info(
            "Migrated pre-v2 checkpoint repos to %s. "
            "Clear with `hermes checkpoints clear-legacy` when safe.",
            legacy_root,
        )
    return legacy_root


def _init_store(store: Path, working_dir: str) -> Optional[str]:
    """按需初始化共享影子仓库。返回错误或 None。

    同时执行一次性的旧版迁移，把 v2 之前各目录独立的影子仓库搬进
    ``legacy-<timestamp>/``。
    """
    base = store.parent
    # 在创建仓库之前做一次性的旧版迁移。
    if not store.exists():
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"Could not create checkpoint base: {exc}"
        # 仅当 base 目录里有非我们 v2 布局的既有内容时才迁移。
        _migrate_legacy_store(base)

    if (store / "HEAD").exists():
        return None

    store.mkdir(parents=True, exist_ok=True)
    (store / _INDEXES_DIRNAME).mkdir(exist_ok=True)
    (store / _PROJECTS_DIRNAME).mkdir(exist_ok=True)

    # ``git init --bare`` 会拒绝 GIT_WORK_TREE，所以这里不能用 _run_git
    #（后者总是会同时设置 GIT_DIR + GIT_WORK_TREE）。改用裸 subprocess，
    # 只带配置隔离的环境变量。
    init_env = os.environ.copy()
    init_env["GIT_CONFIG_GLOBAL"] = os.devnull
    init_env["GIT_CONFIG_SYSTEM"] = os.devnull
    init_env["GIT_CONFIG_NOSYSTEM"] = "1"
    # 丢弃任何会干扰的、继承来的 GIT_*。
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_NAMESPACE",
              "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        init_env.pop(k, None)
    try:
        result = subprocess.run(
            ["git", "init", "--bare", str(store)],
            capture_output=True, text=True,
            env=init_env, timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return f"Shadow store init failed: {result.stderr.strip()}"
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return f"Shadow store init failed: {exc}"

    # 仓库级配置（上面已用环境变量隔离，这里再加一层双保险）。
    # 配置命令用 base 目录作为 working_dir —— 它一定存在，因为我们
    # 刚刚在其中创建了仓库。
    cfg_wd = str(base)
    _run_git(["config", "user.email", "hermes@local"], store, cfg_wd)
    _run_git(["config", "user.name", "Hermes Checkpoint"], store, cfg_wd)
    _run_git(["config", "commit.gpgsign", "false"], store, cfg_wd)
    _run_git(["config", "tag.gpgSign", "false"], store, cfg_wd)
    _run_git(["config", "gc.auto", "0"], store, cfg_wd)

    info_dir = store / "info"
    info_dir.mkdir(exist_ok=True)
    (info_dir / "exclude").write_text(
        "\n".join(DEFAULT_EXCLUDES) + "\n", encoding="utf-8"
    )

    logger.debug("Initialised checkpoint store at %s", store)
    return None


def _register_project(store: Path, working_dir: str) -> None:
    """创建或更新 ``projects/<hash>.json``，写入 workdir + 时间戳。"""
    dir_hash = _project_hash(working_dir)
    meta_path = _project_meta_path(store, dir_hash)
    now = time.time()
    meta: Dict = {"workdir": str(_normalize_path(working_dir)),
                  "created_at": now, "last_touch": now}
    if meta_path.exists():
        try:
            existing = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                meta["created_at"] = existing.get("created_at", now)
        except (OSError, ValueError):
            pass
    try:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    except OSError as exc:
        logger.debug("Could not write project metadata %s: %s", meta_path, exc)


def _touch_project(store: Path, working_dir: str) -> None:
    """更新某个项目的 last_touch，同时保留 created_at。"""
    dir_hash = _project_hash(working_dir)
    meta_path = _project_meta_path(store, dir_hash)
    if not meta_path.exists():
        _register_project(store, working_dir)
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta["workdir"] = str(_normalize_path(working_dir))
    meta["last_touch"] = time.time()
    meta.setdefault("created_at", meta["last_touch"])
    try:
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    except OSError as exc:
        logger.debug("Could not update project metadata %s: %s", meta_path, exc)


def _list_projects(store: Path) -> List[Dict]:
    """返回该仓库下所有已注册的项目。"""
    projects_dir = store / _PROJECTS_DIRNAME
    if not projects_dir.exists():
        return []
    out: List[Dict] = []
    for meta_path in projects_dir.glob("*.json"):
        dir_hash = meta_path.stem
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        meta["_hash"] = dir_hash
        out.append(meta)
    return out


def _dir_file_count(path: str) -> int:
    """快速估算文件数（一旦超过 _MAX_FILES 就提前停止）。"""
    count = 0
    try:
        for _ in Path(path).rglob("*"):
            count += 1
            if count > _MAX_FILES:
                return count
    except (PermissionError, OSError):
        pass
    return count


def _dir_size_bytes(path: Path) -> int:
    """尽力而为地递归统计字节数。出错时返回 0。"""
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


# 向后兼容垫片 —— 部分测试会导入 ``_init_shadow_repo``，并检查
# ``HEAD``/``info/exclude``/``HERMES_WORKDIR``。在 v2 中我们同样会写
# 这些标记，但放在共享仓库内部、且额外写入 ``projects/<hash>.json``。
# 这个垫片会初始化仓库并注册项目，使旧接口大致保持原来的形态。
def _init_shadow_repo(shadow_repo: Path, working_dir: str) -> Optional[str]:
    """向后兼容的初始化器。

    在 v1 中，``shadow_repo`` 是每个项目各一个的目录；在 v2 中，它是
    共享的 ``store/`` 路径（或一个我们尊重的测试路径）。我们在
    ``shadow_repo`` 处初始化仓库、创建项目级标记，成功时返回 None。
    """
    err = _init_store(shadow_repo, working_dir)
    if err:
        return err
    _register_project(shadow_repo, working_dir)
    # 给那些检查 HERMES_WORKDIR 的测试用的兼容标记
    #（在 JSON 元数据之外额外写入）。
    try:
        (shadow_repo / "HERMES_WORKDIR").write_text(
            str(_normalize_path(working_dir)) + "\n", encoding="utf-8"
        )
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# CheckpointManager
# ---------------------------------------------------------------------------

class CheckpointManager:
    """管理自动的文件系统检查点。

    设计为由 AIAgent 持有。在每个会话轮次开始时调用 ``new_turn()``，
    在任何会改动文件的工具调用之前调用
    ``ensure_checkpoint(dir, reason)``。本管理器会做去重，确保每个目录
    每个轮次最多只拍一次快照。

    参数
    ----------
    enabled : bool
        总开关（取自配置 / CLI 标志）。
    max_snapshots : int
        每个目录最多保留多少个检查点。
    max_total_size_mb : int
        仓库总大小的硬上限。当 commit 后仓库超出该上限时，会逐个项目
        丢弃最旧的检查点。
    max_file_size_mb : int
        超过该大小的单个文件不会加入检查点。
        （通过 ``.gitignore`` 排除 + 入库后的大小检查来实现。）
    """

    def __init__(
        self,
        enabled: bool = False,
        max_snapshots: int = 20,
        max_total_size_mb: int = 500,
        max_file_size_mb: int = 10,
    ):
        self.enabled = enabled
        self.max_snapshots = max(1, int(max_snapshots))
        self.max_total_size_mb = max(0, int(max_total_size_mb))
        self.max_file_size_mb = max(0, int(max_file_size_mb))
        self._checkpointed_dirs: Set[str] = set()
        self._git_available: Optional[bool] = None  # 惰性探测

    # ------------------------------------------------------------------
    # 轮次生命周期
    # ------------------------------------------------------------------

    def new_turn(self) -> None:
        """重置轮次级去重。在每个 agent 迭代开始时调用。"""
        self._checkpointed_dirs.clear()

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def ensure_checkpoint(self, working_dir: str, reason: str = "auto") -> bool:
        """若已启用且本轮次尚未做过检查点，则拍一个检查点。

        若已拍了检查点返回 True，否则返回 False。
        永不抛异常 —— 所有错误都静默记录日志。
        """
        if not self.enabled:
            return False

        if self._git_available is None:
            self._git_available = shutil.which("git") is not None
            if not self._git_available:
                logger.debug("Checkpoints disabled: git not found")
        if not self._git_available:
            return False

        abs_dir = str(_normalize_path(working_dir))

        # 跳过根目录、家目录以及其他过于宽泛的目录
        if abs_dir in {"/", str(Path.home())}:
            logger.debug("Checkpoint skipped: directory too broad (%s)", abs_dir)
            return False

        if abs_dir in self._checkpointed_dirs:
            return False

        self._checkpointed_dirs.add(abs_dir)

        try:
            return self._take(abs_dir, reason)
        except Exception as e:
            logger.debug("Checkpoint failed (non-fatal): %s", e)
            return False

    def list_checkpoints(self, working_dir: str) -> List[Dict]:
        """列出某个目录可用的检查点（最新的在前）。"""
        abs_dir = str(_normalize_path(working_dir))
        store = _store_path(CHECKPOINT_BASE)

        if not (store / "HEAD").exists():
            return []

        ref = _ref_name(_project_hash(abs_dir))
        ok, stdout, _ = _run_git(
            ["log", ref, f"--format=%H|%h|%aI|%s", "-n", str(self.max_snapshots)],
            store, abs_dir,
            allowed_returncodes={128, 129},
        )

        if not ok or not stdout:
            return []

        results: List[Dict] = []
        for line in stdout.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                entry = {
                    "hash": parts[0],
                    "short_hash": parts[1],
                    "timestamp": parts[2],
                    "reason": parts[3],
                    "files_changed": 0,
                    "insertions": 0,
                    "deletions": 0,
                }
                stat_ok, stat_out, _ = _run_git(
                    ["diff", "--shortstat", f"{parts[0]}~1", parts[0]],
                    store, abs_dir,
                    allowed_returncodes={128, 129},
                )
                if stat_ok and stat_out:
                    self._parse_shortstat(stat_out, entry)
                results.append(entry)
        return results

    @staticmethod
    def _parse_shortstat(stat_line: str, entry: Dict) -> None:
        """把 git --shortstat 的输出解析进 entry 字典。"""
        m = re.search(r'(\d+) file', stat_line)
        if m:
            entry["files_changed"] = int(m.group(1))
        m = re.search(r'(\d+) insertion', stat_line)
        if m:
            entry["insertions"] = int(m.group(1))
        m = re.search(r'(\d+) deletion', stat_line)
        if m:
            entry["deletions"] = int(m.group(1))

    def diff(self, working_dir: str, commit_hash: str) -> Dict:
        """展示某个检查点与当前工作树之间的 diff。"""
        hash_err = _validate_commit_hash(commit_hash)
        if hash_err:
            return {"success": False, "error": hash_err}

        abs_dir = str(_normalize_path(working_dir))
        store = _store_path(CHECKPOINT_BASE)

        if not (store / "HEAD").exists():
            return {"success": False, "error": "No checkpoints exist for this directory"}

        ok, _, err = _run_git(
            ["cat-file", "-t", commit_hash], store, abs_dir,
        )
        if not ok:
            return {"success": False, "error": f"Checkpoint '{commit_hash}' not found"}

        dir_hash = _project_hash(abs_dir)
        index_file = _index_path(store, dir_hash)

        # 把当前状态暂存到项目级 index，以便对比。
        _run_git(["add", "-A"], store, abs_dir,
                 timeout=_GIT_TIMEOUT * 2, index_file=index_file)

        ok_stat, stat_out, _ = _run_git(
            ["diff", "--stat", commit_hash, "--cached"],
            store, abs_dir, index_file=index_file,
        )
        ok_diff, diff_out, _ = _run_git(
            ["diff", commit_hash, "--cached", "--no-color"],
            store, abs_dir, index_file=index_file,
        )

        # 把暂存树重置回项目上一个检查点，避免 index 与 ref 之间
        # 产生不同步。
        ref = _ref_name(dir_hash)
        _run_git(["read-tree", ref], store, abs_dir,
                 index_file=index_file,
                 allowed_returncodes={128})

        if not ok_stat and not ok_diff:
            return {"success": False, "error": "Could not generate diff"}

        return {
            "success": True,
            "stat": stat_out if ok_stat else "",
            "diff": diff_out if ok_diff else "",
        }

    def restore(self, working_dir: str, commit_hash: str, file_path: str = None) -> Dict:
        """把文件恢复到某个检查点的状态。"""
        hash_err = _validate_commit_hash(commit_hash)
        if hash_err:
            return {"success": False, "error": hash_err}

        abs_dir = str(_normalize_path(working_dir))

        if file_path:
            path_err = _validate_file_path(file_path, abs_dir)
            if path_err:
                return {"success": False, "error": path_err}

        store = _store_path(CHECKPOINT_BASE)

        if not (store / "HEAD").exists():
            return {"success": False, "error": "No checkpoints exist for this directory"}

        ok, _, err = _run_git(
            ["cat-file", "-t", commit_hash], store, abs_dir,
        )
        if not ok:
            return {"success": False, "error": f"Checkpoint '{commit_hash}' not found",
                    "debug": err or None}

        # 先拍一个回滚前的快照，这样就可以撤销这次撤销。
        self._take(abs_dir, f"pre-rollback snapshot (restoring to {commit_hash[:8]})")

        dir_hash = _project_hash(abs_dir)
        index_file = _index_path(store, dir_hash)

        restore_target = file_path if file_path else "."
        ok, stdout, err = _run_git(
            ["checkout", commit_hash, "--", restore_target],
            store, abs_dir, timeout=_GIT_TIMEOUT * 2,
            index_file=index_file,
        )

        if not ok:
            return {"success": False, "error": f"Restore failed: {err}",
                    "debug": err or None}

        ok2, reason_out, _ = _run_git(
            ["log", "--format=%s", "-1", commit_hash], store, abs_dir,
        )
        reason = reason_out if ok2 else "unknown"

        result = {
            "success": True,
            "restored_to": commit_hash[:8],
            "reason": reason,
            "directory": abs_dir,
        }
        if file_path:
            result["file"] = file_path
        return result

    def get_working_dir_for_path(self, file_path: str) -> str:
        """把一个文件路径解析为其所属的、用于建立检查点的工作目录。"""
        path = _normalize_path(file_path)
        if path.is_dir():
            candidate = path
        else:
            candidate = path.parent

        markers = {".git", "pyproject.toml", "package.json", "Cargo.toml",
                    "go.mod", "Makefile", "pom.xml", ".hg", "Gemfile"}
        check = candidate
        while check != check.parent:
            if any((check / m).exists() for m in markers):
                return str(check)
            check = check.parent

        return str(candidate)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _take(self, working_dir: str, reason: str) -> bool:
        """拍一个快照。成功返回 True。"""
        store = _store_path(CHECKPOINT_BASE)

        err = _init_store(store, working_dir)
        if err:
            logger.debug("Checkpoint store init failed: %s", err)
            return False

        _touch_project(store, working_dir)

        # 快速体积守卫 —— 不要试图快照过大的目录
        if _dir_file_count(working_dir) > _MAX_FILES:
            logger.debug("Checkpoint skipped: >%d files in %s", _MAX_FILES, working_dir)
            return False

        dir_hash = _project_hash(working_dir)
        index_file = _index_path(store, dir_hash)
        ref = _ref_name(dir_hash)

        # 用上一个检查点（若有）来填充项目级 index，这样 diff/commit
        # 流程只会看到自那以后的改动。首次调用时，清空 index，使
        # ``git add -A`` 生成一棵干净的树。
        if index_file.exists():
            # 把 index 重置到当前 ref tip，避免累积出陈旧的路径。
            ok_ref, ref_commit, _ = _run_git(
                ["rev-parse", "--verify", ref + "^{commit}"],
                store, working_dir,
                allowed_returncodes={128},
            )
            if ok_ref and ref_commit:
                _run_git(
                    ["read-tree", ref_commit],
                    store, working_dir,
                    index_file=index_file,
                    allowed_returncodes={128},
                )
            else:
                try:
                    index_file.unlink()
                except OSError:
                    pass
        else:
            # 本项目的首次快照。
            index_file.parent.mkdir(parents=True, exist_ok=True)

        # 用项目级 index 暂存。我们「不」想用 ``core.bigFileThreshold``
        # 这种按文件大小过滤的暂存机制 —— 取而代之的是：依靠 exclude
        # 文件处理大类模式，并在入库后剔除任何超过 max_file_size_mb 的路径。
        ok, _, err = _run_git(
            ["add", "-A"], store, working_dir,
            timeout=_GIT_TIMEOUT * 2, index_file=index_file,
        )
        if not ok:
            logger.debug("Checkpoint git-add failed: %s", err)
            return False

        if self.max_file_size_mb > 0:
            self._drop_oversize_from_index(store, working_dir, index_file)

        # 与当前 ref tip 对比（而不是 HEAD —— 在裸仓库上 HEAD 指向一个
        # 不存在的分支，所以拿 HEAD 做 ``diff --cached`` 会把每个暂存
        # 路径都显示成「new file」）。
        ok_ref, ref_commit, _ = _run_git(
            ["rev-parse", "--verify", ref + "^{commit}"],
            store, working_dir,
            allowed_returncodes={128},
        )
        has_ref = ok_ref and bool(ref_commit)

        if has_ref:
            ok_diff, _, _ = _run_git(
                ["diff-index", "--cached", "--quiet", ref_commit],
                store, working_dir,
                allowed_returncodes={1},
                index_file=index_file,
            )
            if ok_diff:
                logger.debug("Checkpoint skipped: no changes in %s", working_dir)
                return False
        else:
            # 还没有 ref —— 仅当 index 为空时才跳过。
            ok_ls, ls_out, _ = _run_git(
                ["ls-files", "--cached"],
                store, working_dir,
                index_file=index_file,
            )
            if ok_ls and not ls_out.strip():
                logger.debug("Checkpoint skipped: empty tree in %s", working_dir)
                return False

        # 从项目级 index 写出树。
        ok_tree, tree_sha, err = _run_git(
            ["write-tree"], store, working_dir,
            index_file=index_file,
        )
        if not ok_tree or not tree_sha:
            logger.debug("Checkpoint write-tree failed: %s", err)
            return False

        # 构造 commit（父提交 = 当前 ref tip，若存在）。
        commit_args = ["commit-tree", tree_sha, "-m", reason, "--no-gpg-sign"]
        if has_ref:
            commit_args = ["commit-tree", tree_sha, "-p", ref_commit, "-m", reason, "--no-gpg-sign"]
        ok_commit, new_sha, err = _run_git(
            commit_args, store, working_dir,
            index_file=index_file,
        )
        if not ok_commit or not new_sha:
            logger.debug("Checkpoint commit-tree failed: %s", err)
            return False

        # 更新项目级 ref。
        update_args = ["update-ref", ref, new_sha]
        if has_ref:
            update_args = ["update-ref", ref, new_sha, ref_commit]
        ok_update, _, err = _run_git(
            update_args, store, working_dir,
        )
        if not ok_update:
            logger.debug("Checkpoint update-ref failed: %s", err)
            return False

        logger.debug("Checkpoint taken in %s: %s (%s)", working_dir, reason, new_sha[:8])

        # 真正的清理 —— 丢弃超过 max_snapshots 的旧 commit。
        self._prune(store, working_dir, ref)

        # 执行全局容量上限。
        self._enforce_size_cap(store)

        return True

    def _drop_oversize_from_index(
        self, store: Path, working_dir: str, index_file: Path,
    ) -> None:
        """从 index 中移除任何大于 ``max_file_size_mb`` 的已暂存文件。

        让 agent 继续为源代码建立快照，同时拒绝吞下生成的资源
        （数据集、模型权重、日志、视频）。
        """
        cap = self.max_file_size_mb * 1024 * 1024
        if cap <= 0:
            return
        ok, stdout, _ = _run_git(
            ["ls-files", "--cached", "-z"],
            store, working_dir, index_file=index_file,
        )
        if not ok or not stdout:
            return
        # ls-files -z 的输出以 NUL 分隔。_run_git 会去除末尾空白，
        # 但不会动 NUL；这里重建列表。
        paths = [p for p in stdout.split("\x00") if p]
        abs_workdir = _normalize_path(working_dir)
        oversize: List[str] = []
        for rel in paths:
            try:
                size = (abs_workdir / rel).stat().st_size
            except OSError:
                continue
            if size > cap:
                oversize.append(rel)
        if not oversize:
            return
        logger.debug(
            "Checkpoint: dropping %d oversize file(s) (>%d MB) from index",
            len(oversize), self.max_file_size_mb,
        )
        # 为安全起见用 --pathspec-from-file（路径多时）。
        # 切成可控的小批次。
        BATCH = 200
        for i in range(0, len(oversize), BATCH):
            chunk = oversize[i:i + BATCH]
            _run_git(
                ["rm", "--cached", "--quiet", "--"] + chunk,
                store, working_dir, index_file=index_file,
                allowed_returncodes={128},
            )

    def _prune(self, store: Path, working_dir: str, ref: str) -> None:
        """只保留项目级 ref 上最近 ``max_snapshots`` 个 commit。

        v1 的 ``_prune`` 文档里说它是空操作（本应由 ``git`` 的打包机制来
        处理，但实际只限制了日志视图 —— 松散对象会一直累积）。v2 则真正
        会重写 ref，丢弃早于 ``max_snapshots`` 的 commit，然后对仓库运行
        ``git gc``，回收不可达对象。
        """
        ok, stdout, _ = _run_git(
            ["rev-list", "--count", ref], store, working_dir,
            allowed_returncodes={128},
        )
        if not ok:
            return
        try:
            count = int(stdout)
        except ValueError:
            return
        if count <= self.max_snapshots:
            return

        # 收集 commit（从最旧到最新），取最后 N 个。
        ok_list, list_out, _ = _run_git(
            ["rev-list", "--reverse", ref], store, working_dir,
        )
        if not ok_list or not list_out:
            return
        commits = list_out.splitlines()
        keep = commits[-self.max_snapshots:]

        # 基于 keep[0] 的树重建一条线性链。
        new_parent: Optional[str] = None
        for sha in keep:
            ok_tree, tree_sha, _ = _run_git(
                ["rev-parse", f"{sha}^{{tree}}"], store, working_dir,
            )
            if not ok_tree or not tree_sha:
                return
            ok_msg, msg, _ = _run_git(
                ["log", "--format=%s", "-1", sha], store, working_dir,
            )
            commit_msg = msg if ok_msg and msg else "checkpoint"
            args = ["commit-tree", tree_sha, "-m", commit_msg, "--no-gpg-sign"]
            if new_parent is not None:
                args = ["commit-tree", tree_sha, "-p", new_parent,
                        "-m", commit_msg, "--no-gpg-sign"]
            ok_commit, new_sha, _ = _run_git(args, store, working_dir)
            if not ok_commit or not new_sha:
                return
            new_parent = new_sha

        if new_parent is None:
            return
        _run_git(["update-ref", ref, new_parent], store, working_dir)

        # 回收被丢弃 commit 的对象。
        _run_git(
            ["reflog", "expire", "--expire=now", "--all"],
            store, working_dir,
        )
        _run_git(
            ["gc", "--prune=now", "--quiet"],
            store, working_dir, timeout=_GIT_TIMEOUT * 3,
        )
        _repair_bare_repo_dirs(store)

    def _enforce_size_cap(self, store: Path) -> None:
        """当仓库总大小超过 ``max_total_size_mb`` 时，跨「所有」项目
        丢弃最旧的检查点，直到低于上限。
        """
        if self.max_total_size_mb <= 0:
            return
        cap_bytes = self.max_total_size_mb * 1024 * 1024
        size = _dir_size_bytes(store)
        if size <= cap_bytes:
            return
        logger.info(
            "Checkpoint store exceeded %d MB (actual %d MB) — pruning oldest",
            self.max_total_size_mb, size // (1024 * 1024),
        )

        # 跨所有项目级 ref 收集 (commit_time, ref, sha)。
        ok, stdout, _ = _run_git(
            ["for-each-ref", "--format=%(refname)", _REFS_PREFIX],
            store, str(store.parent),
            allowed_returncodes={128},
        )
        if not ok or not stdout:
            return
        refs = [r for r in stdout.splitlines() if r.strip()]

        any_dropped = False
        # 轮流从每个 ref 丢弃最旧的 commit，直到低于上限。
        for _ in range(20):  # 硬上限，避免病态循环
            size = _dir_size_bytes(store)
            if size <= cap_bytes:
                break
            for ref in refs:
                ok_count, count_out, _ = _run_git(
                    ["rev-list", "--count", ref], store, str(store.parent),
                    allowed_returncodes={128},
                )
                try:
                    count = int(count_out) if ok_count else 0
                except ValueError:
                    count = 0
                if count <= 1:
                    continue  # 每个项目至少保留一个快照
                ok_list, list_out, _ = _run_git(
                    ["rev-list", "--reverse", ref], store, str(store.parent),
                )
                if not ok_list or not list_out:
                    continue
                commits = list_out.splitlines()
                keep = commits[1:]  # 丢弃最旧的
                new_parent: Optional[str] = None
                fail = False
                for sha in keep:
                    ok_tree, tree_sha, _ = _run_git(
                        ["rev-parse", f"{sha}^{{tree}}"], store, str(store.parent),
                    )
                    if not ok_tree or not tree_sha:
                        fail = True
                        break
                    ok_msg, msg, _ = _run_git(
                        ["log", "--format=%s", "-1", sha], store, str(store.parent),
                    )
                    commit_msg = msg if ok_msg and msg else "checkpoint"
                    args = ["commit-tree", tree_sha, "-m", commit_msg, "--no-gpg-sign"]
                    if new_parent is not None:
                        args = ["commit-tree", tree_sha, "-p", new_parent,
                                "-m", commit_msg, "--no-gpg-sign"]
                    ok_commit, new_sha, _ = _run_git(args, store, str(store.parent))
                    if not ok_commit or not new_sha:
                        fail = True
                        break
                    new_parent = new_sha
                if fail or new_parent is None:
                    continue
                _run_git(["update-ref", ref, new_parent], store, str(store.parent))
                any_dropped = True
            if not any_dropped:
                break

        _run_git(
            ["reflog", "expire", "--expire=now", "--all"],
            store, str(store.parent),
        )
        _run_git(
            ["gc", "--prune=now", "--quiet"],
            store, str(store.parent), timeout=_GIT_TIMEOUT * 3,
        )
        _repair_bare_repo_dirs(store)


def format_checkpoint_list(checkpoints: List[Dict], directory: str) -> str:
    """把检查点列表格式化为给用户展示的文本。"""
    if not checkpoints:
        return f"No checkpoints found for {directory}"

    lines = [f"📸 Checkpoints for {directory}:\n"]
    for i, cp in enumerate(checkpoints, 1):
        ts = cp["timestamp"]
        if "T" in ts:
            ts = ts.split("T")[1].split("+")[0].split("-")[0][:5]
            date = cp["timestamp"].split("T")[0]
            ts = f"{date} {ts}"

        files = cp.get("files_changed", 0)
        ins = cp.get("insertions", 0)
        dele = cp.get("deletions", 0)
        if files:
            stat = f"  ({files} file{'s' if files != 1 else ''}, +{ins}/-{dele})"
        else:
            stat = ""

        lines.append(f"  {i}. {cp['short_hash']}  {ts}  {cp['reason']}{stat}")

    lines.append("\n  /rollback <N>             restore to checkpoint N")
    lines.append("  /rollback diff <N>        preview changes since checkpoint N")
    lines.append("  /rollback <N> <file>      restore a single file from checkpoint N")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 自动维护
# ---------------------------------------------------------------------------
#
# v2 重写。清理现在针对共享仓库内的项目级 ref 进行，而非针对各项目独立
# 的影子仓库。旧版归档目录（``legacy-<ts>/``）也按相同的保留策略清理。

_PRUNE_MARKER_NAME = ".last_prune"


def _delete_ref(store: Path, ref: str) -> bool:
    """从仓库删除一个 ref。成功返回 True。"""
    ok, _, _ = _run_git(
        ["update-ref", "-d", ref], store, str(store.parent),
        allowed_returncodes={128},
    )
    return ok


def prune_checkpoints(
    retention_days: int = 7,
    delete_orphans: bool = True,
    checkpoint_base: Optional[Path] = None,
    max_total_size_mb: int = 0,
) -> Dict[str, int]:
    """删除陈旧/孤儿的检查点，并回收仓库空间。

    当满足以下任一条件时，删除某个项目条目：

    * ``delete_orphans=True`` 且其 ``workdir`` 在磁盘上已不存在
      （原项目被删除/移动）；或
    * 其 ``last_touch`` 早于 ``retention_days`` 天。

    此外，若 ``max_total_size_mb > 0`` 且在孤儿/陈旧清理后仓库仍超出该
    上限，会逐个剩余项目丢弃最旧的 commit，直到仓库低于上限。

    早于 ``retention_days`` 的旧版归档目录（``legacy-*``）也会被删除。

    返回一个计数字典：``{"scanned", "deleted_orphan",
    "deleted_stale", "errors", "bytes_freed"}``。

    永不抛异常 —— 维护工作绝不能阻塞交互式启动。
    """
    base = checkpoint_base or CHECKPOINT_BASE
    result = {
        "scanned": 0,
        "deleted_orphan": 0,
        "deleted_stale": 0,
        "errors": 0,
        "bytes_freed": 0,
    }
    if not base.exists():
        return result

    size_before = _dir_size_bytes(base)

    # --- v2 之前、直接放在 base 下的各项目独立影子仓库 ---
    # v2 之前的布局：``base/<hash>/HEAD`` 等。这里完全照搬 v1 清理器
    # 的处理方式，使任何仍在该布局下、或正处于迁移中途的系统的行为
    # 保持不变。
    cutoff = 0.0
    if retention_days > 0:
        cutoff = time.time() - retention_days * 86400

    for child in base.iterdir():
        if not child.is_dir():
            continue
        if child.name == _STORE_DIRNAME:
            continue
        if child.name.startswith(_LEGACY_PREFIX):
            # 旧版归档：按目录 mtime 用同样的保留规则清理。
            if retention_days <= 0:
                continue
            try:
                m = child.stat().st_mtime
            except OSError:
                continue
            if m >= cutoff:
                continue
            try:
                size = _dir_size_bytes(child)
                shutil.rmtree(child)
                result["bytes_freed"] += size
                result["deleted_stale"] += 1
            except OSError as exc:
                result["errors"] += 1
                logger.warning("Failed to delete legacy archive %s: %s", child, exc)
            continue
        # 只有含 HEAD 的才算是 v2 之前的影子仓库。
        if not (child / "HEAD").exists():
            continue
        result["scanned"] += 1
        reason: Optional[str] = None
        if delete_orphans:
            workdir: Optional[str] = None
            wd_marker = child / "HERMES_WORKDIR"
            if wd_marker.exists():
                try:
                    workdir = wd_marker.read_text(encoding="utf-8").strip()
                except (OSError, UnicodeDecodeError):
                    workdir = None
            if workdir is None or not Path(workdir).exists():
                reason = "orphan"
        if reason is None and retention_days > 0:
            newest = 0.0
            try:
                for p in child.rglob("*"):
                    try:
                        mt = p.stat().st_mtime
                        newest = max(newest, mt)
                    except OSError:
                        continue
            except OSError:
                pass
            if newest > 0 and newest < cutoff:
                reason = "stale"
        if reason is None:
            continue
        try:
            size = _dir_size_bytes(child)
            shutil.rmtree(child)
            result["bytes_freed"] += size
            if reason == "orphan":
                result["deleted_orphan"] += 1
            else:
                result["deleted_stale"] += 1
        except OSError as exc:
            result["errors"] += 1
            logger.warning("Failed to prune checkpoint repo %s: %s", child.name, exc)

    # --- v2 共享仓库：通过元数据对项目级 ref 做清理 ---
    store = _store_path(base)
    if (store / "HEAD").exists():
        for meta in _list_projects(store):
            dir_hash = meta.get("_hash") or ""
            workdir = meta.get("workdir") or ""
            if not dir_hash:
                continue
            result["scanned"] += 1
            reason = None
            if delete_orphans and (not workdir or not Path(workdir).exists()):
                reason = "orphan"
            elif retention_days > 0:
                last_touch = float(meta.get("last_touch", 0) or 0)
                if last_touch > 0 and last_touch < cutoff:
                    reason = "stale"
            if reason is None:
                continue
            ref = _ref_name(dir_hash)
            _delete_ref(store, ref)
            # 删除项目级 index 和元数据。
            try:
                idx = _index_path(store, dir_hash)
                if idx.exists():
                    idx.unlink()
            except OSError:
                pass
            try:
                mp = _project_meta_path(store, dir_hash)
                if mp.exists():
                    mp.unlink()
            except OSError:
                pass
            if reason == "orphan":
                result["deleted_orphan"] += 1
            else:
                result["deleted_stale"] += 1

        # 对仓库做 GC，回收被丢弃 ref 留下的不可达对象。
        _run_git(
            ["reflog", "expire", "--expire=now", "--all"],
            store, str(base),
        )
        _run_git(
            ["gc", "--prune=now", "--quiet"],
            store, str(base), timeout=_GIT_TIMEOUT * 3,
        )
        _repair_bare_repo_dirs(store)

        # 对剩余项目执行容量上限这一步。
        if max_total_size_mb > 0:
            cap_bytes = max_total_size_mb * 1024 * 1024
            for _i in range(20):
                size = _dir_size_bytes(store)
                if size <= cap_bytes:
                    break
                ok, stdout, _ = _run_git(
                    ["for-each-ref", "--format=%(refname)", _REFS_PREFIX],
                    store, str(base),
                    allowed_returncodes={128},
                )
                refs = [r for r in stdout.splitlines() if r.strip()] if ok else []
                if not refs:
                    break
                any_drop = False
                for ref in refs:
                    ok_c, count_out, _ = _run_git(
                        ["rev-list", "--count", ref], store, str(base),
                        allowed_returncodes={128},
                    )
                    try:
                        count = int(count_out) if ok_c else 0
                    except ValueError:
                        count = 0
                    if count <= 1:
                        continue
                    ok_l, lo, _ = _run_git(
                        ["rev-list", "--reverse", ref], store, str(base),
                    )
                    if not ok_l or not lo:
                        continue
                    commits = lo.splitlines()
                    keep = commits[1:]
                    new_parent: Optional[str] = None
                    fail = False
                    for sha in keep:
                        ok_t, tsha, _ = _run_git(
                            ["rev-parse", f"{sha}^{{tree}}"], store, str(base),
                        )
                        if not ok_t or not tsha:
                            fail = True
                            break
                        ok_m, m, _ = _run_git(
                            ["log", "--format=%s", "-1", sha], store, str(base),
                        )
                        msg = m if ok_m and m else "checkpoint"
                        args = ["commit-tree", tsha, "-m", msg, "--no-gpg-sign"]
                        if new_parent is not None:
                            args = ["commit-tree", tsha, "-p", new_parent,
                                    "-m", msg, "--no-gpg-sign"]
                        ok_cm, new_sha, _ = _run_git(args, store, str(base))
                        if not ok_cm or not new_sha:
                            fail = True
                            break
                        new_parent = new_sha
                    if fail or new_parent is None:
                        continue
                    _run_git(["update-ref", ref, new_parent], store, str(base))
                    any_drop = True
                if not any_drop:
                    break
            _run_git(
                ["reflog", "expire", "--expire=now", "--all"],
                store, str(base),
            )
            _run_git(
                ["gc", "--prune=now", "--quiet"],
                store, str(base), timeout=_GIT_TIMEOUT * 3,
            )
            _repair_bare_repo_dirs(store)

    size_after = _dir_size_bytes(base)
    delta = size_before - size_after
    result["bytes_freed"] = max(result["bytes_freed"], delta)

    return result


def maybe_auto_prune_checkpoints(
    retention_days: int = 7,
    min_interval_hours: int = 24,
    delete_orphans: bool = True,
    checkpoint_base: Optional[Path] = None,
    max_total_size_mb: int = 0,
) -> Dict[str, object]:
    """为启动钩子准备的 ``prune_checkpoints`` 幂等封装。

    完成后会写入 ``CHECKPOINT_BASE/.last_prune``，使得在
    ``min_interval_hours`` 内的后续调用直接短路跳过。

    返回 ``{"skipped": bool, "result": prune_checkpoints 的字典,
    "error": 可选字符串}``。
    """
    base = checkpoint_base or CHECKPOINT_BASE
    out: Dict[str, object] = {"skipped": False}

    try:
        if not base.exists():
            out["result"] = {
                "scanned": 0, "deleted_orphan": 0, "deleted_stale": 0,
                "errors": 0, "bytes_freed": 0,
            }
            return out

        marker = base / _PRUNE_MARKER_NAME
        now = time.time()
        if marker.exists():
            try:
                last_ts = float(marker.read_text(encoding="utf-8").strip())
                if now - last_ts < min_interval_hours * 3600:
                    out["skipped"] = True
                    return out
            except (OSError, ValueError):
                pass  # 标记文件损坏 —— 视作从未运行过

        result = prune_checkpoints(
            retention_days=retention_days,
            delete_orphans=delete_orphans,
            checkpoint_base=base,
            max_total_size_mb=max_total_size_mb,
        )
        out["result"] = result

        try:
            marker.write_text(str(now), encoding="utf-8")
        except OSError as exc:
            logger.debug("Could not write checkpoint prune marker: %s", exc)

        total = result["deleted_orphan"] + result["deleted_stale"]
        if total > 0:
            logger.info(
                "checkpoint auto-maintenance: pruned %d entry(ies) "
                "(%d orphan, %d stale), reclaimed %.1f MB",
                total,
                result["deleted_orphan"],
                result["deleted_stale"],
                result["bytes_freed"] / (1024 * 1024),
            )
    except Exception as exc:
        logger.warning("checkpoint auto-maintenance failed: %s", exc)
        out["error"] = str(exc)

    return out


# ---------------------------------------------------------------------------
# 供 `hermes checkpoints` CLI 使用的公共辅助函数
# ---------------------------------------------------------------------------

def store_status(checkpoint_base: Optional[Path] = None) -> Dict:
    """返回影子仓库的概览。

    ``{"base": path, "store_size_bytes": N, "legacy_size_bytes": N,
       "total_size_bytes": N, "project_count": N, "projects": [...],
       "legacy_archives": [...]}``
    """
    base = checkpoint_base or CHECKPOINT_BASE
    out: Dict = {
        "base": str(base),
        "store_size_bytes": 0,
        "legacy_size_bytes": 0,
        "total_size_bytes": 0,
        "project_count": 0,
        "projects": [],
        "legacy_archives": [],
    }
    if not base.exists():
        return out

    store = _store_path(base)
    if store.exists():
        out["store_size_bytes"] = _dir_size_bytes(store)
        if (store / "HEAD").exists():
            for meta in _list_projects(store):
                dir_hash = meta.get("_hash") or ""
                workdir = meta.get("workdir") or ""
                ref = _ref_name(dir_hash)
                ok, count_out, _ = _run_git(
                    ["rev-list", "--count", ref], store, str(base),
                    allowed_returncodes={128},
                )
                try:
                    commits = int(count_out) if ok else 0
                except ValueError:
                    commits = 0
                out["projects"].append({
                    "hash": dir_hash,
                    "workdir": workdir,
                    "exists": bool(workdir) and Path(workdir).exists(),
                    "created_at": meta.get("created_at"),
                    "last_touch": meta.get("last_touch"),
                    "commits": commits,
                })
    out["project_count"] = len(out["projects"])

    for child in base.iterdir():
        if child.is_dir() and child.name.startswith(_LEGACY_PREFIX):
            try:
                size = _dir_size_bytes(child)
            except OSError:
                size = 0
            out["legacy_size_bytes"] += size
            try:
                mt = child.stat().st_mtime
            except OSError:
                mt = 0
            out["legacy_archives"].append({
                "name": child.name,
                "size_bytes": size,
                "mtime": mt,
            })

    out["total_size_bytes"] = _dir_size_bytes(base)
    return out


def clear_all(checkpoint_base: Optional[Path] = None) -> Dict[str, int]:
    """核平整个检查点 base（仓库 + legacy）。不可逆。

    返回 ``{"bytes_freed": N, "deleted": bool}``。
    """
    base = checkpoint_base or CHECKPOINT_BASE
    out = {"bytes_freed": 0, "deleted": False}
    if not base.exists():
        return out
    size = _dir_size_bytes(base)
    try:
        shutil.rmtree(base)
        out["bytes_freed"] = size
        out["deleted"] = True
    except OSError as exc:
        logger.warning("Could not clear checkpoint base %s: %s", base, exc)
    return out


def clear_legacy(checkpoint_base: Optional[Path] = None) -> Dict[str, int]:
    """删除所有 ``legacy-*`` 归档目录。

    返回 ``{"bytes_freed": N, "deleted": count}``。
    """
    base = checkpoint_base or CHECKPOINT_BASE
    out = {"bytes_freed": 0, "deleted": 0}
    if not base.exists():
        return out
    for child in list(base.iterdir()):
        if not child.is_dir() or not child.name.startswith(_LEGACY_PREFIX):
            continue
        try:
            size = _dir_size_bytes(child)
            shutil.rmtree(child)
            out["bytes_freed"] += size
            out["deleted"] += 1
        except OSError as exc:
            logger.warning("Could not delete legacy archive %s: %s", child, exc)
    return out
