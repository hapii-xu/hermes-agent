"""供远程执行后端共享使用的文件同步管理器。

通过 mtime+size 跟踪本地文件变更、检测删除，并以事务方式同步到远程环境。供 SSH、
Modal 和 Daytona 使用。Docker 和 Singularity 使用绑定挂载（实时宿主机文件系统视图），
不需要本模块。
"""

import hashlib
import logging
import os
import posixpath
import shlex
import shutil
import signal
import tarfile
import tempfile
import threading
import time

try:
    import fcntl
except ImportError:
    fcntl = None  # Windows —— 跳过文件锁
from pathlib import Path
from typing import Callable

from hermes_constants import get_hermes_home
from tools.environments.base import _file_mtime_key

logger = logging.getLogger(__name__)

# 让重试睡眠可被打补丁，而不必修改共享的 stdlib ``time`` 模块。给
# ``tools.environments.file_sync.time.sleep`` 打补丁会全局替换 ``time.sleep``，
# 因为 ``time`` 就是模块对象；在 xdist 下这会让无关的后台线程虚增重试测试的调用计数。
_sleep = time.sleep

_SYNC_INTERVAL_SECONDS = 5.0
_FORCE_SYNC_ENV = "HERMES_FORCE_FILE_SYNC"

# 由各后端提供的传输回调
UploadFn = Callable[[str, str], None]  # (host_path, remote_path) -> 失败时抛异常
BulkUploadFn = Callable[[list[tuple[str, str]]], None]  # [(host_path, remote_path), ...] -> 失败时抛异常
BulkDownloadFn = Callable[[Path], None]  # (dest_tar_path) -> 写入 tar 归档，失败时抛异常
DeleteFn = Callable[[list[str]], None]  # (remote_paths) -> 失败时抛异常
GetFilesFn = Callable[[], list[tuple[str, str]]]  # () -> [(host_path, remote_path), ...]


def iter_sync_files(container_base: str = "/root/.hermes") -> list[tuple[str, str]]:
    """枚举所有应当同步到远程环境的文件。

    把凭证、技能和缓存合并成一个扁平的 (host_path, remote_path) 对列表。凭证路径会从
    硬编码的 /root/.hermes 重新映射到 *container_base*，因为远程用户的主目录可能不同
    （例如 /home/daytona、/home/user）。
    """
    # 延迟导入：credential_files 导入的 agent 模块若在 file_sync 模块级别加载会造成
    # 循环依赖。
    from tools.credential_files import (
        get_credential_file_mounts,
        iter_cache_files,
        iter_skills_files,
    )

    files: list[tuple[str, str]] = []
    for entry in get_credential_file_mounts():
        remote = entry["container_path"].replace(
            "/root/.hermes", container_base, 1
        )
        files.append((entry["host_path"], remote))
    for entry in iter_skills_files(container_base=container_base):
        files.append((entry["host_path"], entry["container_path"]))
    for entry in iter_cache_files(container_base=container_base):
        files.append((entry["host_path"], entry["container_path"]))
    return files


def quoted_rm_command(remote_paths: list[str]) -> str:
    """为一批远程路径构造 shell ``rm -f`` 命令。"""
    return "rm -f " + " ".join(shlex.quote(p) for p in remote_paths)


def quoted_mkdir_command(dirs: list[str]) -> str:
    """为一批目录构造 shell ``mkdir -p`` 命令。"""
    return "mkdir -p " + " ".join(shlex.quote(d) for d in dirs)


def unique_parent_dirs(files: list[tuple[str, str]]) -> list[str]:
    """从 (host, remote) 对中提取排序去重后的父目录。"""
    return sorted({posixpath.dirname(remote) for _, remote in files})


def _sha256_file(path: str) -> str:
    """返回文件的十六进制 SHA-256 摘要。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


_SYNC_BACK_MAX_RETRIES = 3
_SYNC_BACK_BACKOFF = (2, 4, 8)  # 重试之间的秒数
_SYNC_BACK_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB —— 拒绝解压更大的 tar


class FileSyncManager:
    """跟踪本地文件变更并同步到远程环境。

    后端用传输回调（上传、删除）和一个文件来源可调用对象来实例化本类。管理器负责基于
    mtime 的变更检测、删除跟踪、限流以及事务化状态。

    绑定挂载的后端（Docker、Singularity）不使用本类 —— 它们拿到的是实时的宿主机文件
    系统视图，不需要文件同步。
    """

    def __init__(
        self,
        get_files_fn: GetFilesFn,
        upload_fn: UploadFn,
        delete_fn: DeleteFn,
        sync_interval: float = _SYNC_INTERVAL_SECONDS,
        bulk_upload_fn: BulkUploadFn | None = None,
        bulk_download_fn: BulkDownloadFn | None = None,
    ):
        self._get_files_fn = get_files_fn
        self._upload_fn = upload_fn
        self._bulk_upload_fn = bulk_upload_fn
        self._bulk_download_fn = bulk_download_fn
        self._delete_fn = delete_fn
        self._synced_files: dict[str, tuple[float, int]] = {}  # remote_path -> (mtime, size)
        self._pushed_hashes: dict[str, str] = {}  # remote_path -> sha256 十六进制摘要
        self._last_sync_time: float = 0.0  # 单调时钟；0 确保首次同步会运行
        self._sync_interval = sync_interval

    def sync(self, *, force: bool = False) -> None:
        """运行一次同步周期：上传已变更文件，删除已移除文件。

        除非 *force* 为 True 或设置了 ``HERMES_FORCE_FILE_SYNC=1``，否则每个
        ``sync_interval`` 最多同步一次。

        事务化：只有所有操作都成功才会提交状态。失败时状态回滚，以便下一周期全部重试。
        """
        if not force and not os.environ.get(_FORCE_SYNC_ENV):
            now = time.monotonic()
            if now - self._last_sync_time < self._sync_interval:
                return

        current_files = self._get_files_fn()
        current_remote_paths = {remote for _, remote in current_files}

        # --- 上传：新增或变更的文件 ---
        to_upload: list[tuple[str, str]] = []
        new_files = dict(self._synced_files)
        for host_path, remote_path in current_files:
            file_key = _file_mtime_key(host_path)
            if file_key is None:
                continue
            if self._synced_files.get(remote_path) == file_key:
                continue
            to_upload.append((host_path, remote_path))
            new_files[remote_path] = file_key

        # --- 删除：已同步但已不在当前集合中的路径 ---
        to_delete = [p for p in self._synced_files if p not in current_remote_paths]

        if not to_upload and not to_delete:
            self._last_sync_time = time.monotonic()
            return

        # 为回滚做快照（仅当确有工作要做时）
        prev_files = dict(self._synced_files)
        prev_hashes = dict(self._pushed_hashes)

        if to_upload:
            logger.debug("file_sync: uploading %d file(s)", len(to_upload))
        if to_delete:
            logger.debug("file_sync: deleting %d stale remote file(s)", len(to_delete))

        try:
            if to_upload and self._bulk_upload_fn is not None:
                self._bulk_upload_fn(to_upload)
                logger.debug("file_sync: bulk-uploaded %d file(s)", len(to_upload))
            else:
                for host_path, remote_path in to_upload:
                    self._upload_fn(host_path, remote_path)
                    logger.debug("file_sync: uploaded %s -> %s", host_path, remote_path)

            if to_delete:
                self._delete_fn(to_delete)
                logger.debug("file_sync: deleted %s", to_delete)

            # --- 提交（全部成功） ---
            for host_path, remote_path in to_upload:
                self._pushed_hashes[remote_path] = _sha256_file(host_path)

            for p in to_delete:
                new_files.pop(p, None)
                self._pushed_hashes.pop(p, None)

            self._synced_files = new_files
            self._last_sync_time = time.monotonic()

        except Exception as exc:
            self._synced_files = prev_files
            self._pushed_hashes = prev_hashes
            self._last_sync_time = time.monotonic()
            logger.warning("file_sync: sync failed, rolled back state: %s", exc)

    # ------------------------------------------------------------------
    # 回拉同步：在拆卸时把远程变更拉回宿主机
    # ------------------------------------------------------------------

    def sync_back(self, hermes_home: Path | None = None) -> None:
        """把远程变更拉回到宿主机文件系统。

        把远程 ``.hermes/`` 目录作为 tar 归档下载下来、解压，并且只应用那些与最初推送
        时不同的文件（依据 SHA-256 内容哈希）。

        针对 SIGINT 做了保护（把信号推迟到完成后再处理），并通过文件锁在并发的网关
        沙箱之间串行化。
        """
        if self._bulk_download_fn is None:
            return

        # 从未有内容通过本管理器提交过 —— 初始推送失败或根本没运行。跳过 sync_back，
        # 以免对着一个未初始化的远程 .hermes/ 目录引发重试风暴。
        if not self._pushed_hashes and not self._synced_files:
            logger.debug("sync_back: no prior push state — skipping")
            return

        lock_path = (hermes_home or get_hermes_home()) / ".sync.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        last_exc: Exception | None = None
        for attempt in range(_SYNC_BACK_MAX_RETRIES):
            try:
                self._sync_back_once(lock_path)
                return
            except Exception as exc:
                last_exc = exc
                if attempt < _SYNC_BACK_MAX_RETRIES - 1:
                    delay = _SYNC_BACK_BACKOFF[attempt]
                    logger.warning(
                        "sync_back: attempt %d failed (%s), retrying in %ds",
                        attempt + 1, exc, delay,
                    )
                    _sleep(delay)

        logger.warning("sync_back: all %d attempts failed: %s", _SYNC_BACK_MAX_RETRIES, last_exc)

    def _sync_back_once(self, lock_path: Path) -> None:
        """单次回拉尝试，带 SIGINT 保护和文件锁。"""
        # signal.signal() 只能在主线程中调用。在网关上下文中 cleanup() 可能运行于
        # 工作线程 —— 此时跳过 SIGINT 延迟，而不是直接崩溃。
        on_main_thread = threading.current_thread() is threading.main_thread()

        deferred_sigint: list[object] = []
        original_handler = None
        if on_main_thread:
            original_handler = signal.getsignal(signal.SIGINT)

            def _defer_sigint(signum, frame):
                deferred_sigint.append((signum, frame))
                logger.debug("sync_back: SIGINT deferred until sync completes")

            signal.signal(signal.SIGINT, _defer_sigint)
        try:
            self._sync_back_locked(lock_path)
        finally:
            if on_main_thread and original_handler is not None:
                signal.signal(signal.SIGINT, original_handler)
                if deferred_sigint:
                    os.kill(os.getpid(), signal.SIGINT)

    def _sync_back_locked(self, lock_path: Path) -> None:
        """在文件锁之下做回拉同步（串行化并发的网关）。"""
        if fcntl is None:
            # Windows：没有 flock —— 不做串行化直接运行
            self._sync_back_impl()
            return
        lock_fd = open(lock_path, "w", encoding="utf-8")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            self._sync_back_impl()
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except (OSError, IOError):
                pass
            lock_fd.close()

    def _sync_back_impl(self) -> None:
        """下载、比对，并把远程变更应用到宿主机。"""
        if self._bulk_download_fn is None:
            raise RuntimeError("_sync_back_impl called without bulk_download_fn")

        # 把文件映射缓存一次，避免因反复迭代造成 O(n*m)
        try:
            file_mapping = list(self._get_files_fn())
        except Exception:
            file_mapping = []

        with tempfile.NamedTemporaryFile(suffix=".tar") as tf:
            self._bulk_download_fn(Path(tf.name))

            # 防御性大小上限：行为异常的沙箱可能产生任意大的 tar。若超过上限则拒绝解压。
            try:
                tar_size = os.path.getsize(tf.name)
            except OSError:
                tar_size = 0
            if tar_size > _SYNC_BACK_MAX_BYTES:
                logger.warning(
                    "sync_back: remote tar is %d bytes (cap %d) — skipping extraction",
                    tar_size, _SYNC_BACK_MAX_BYTES,
                )
                return

            with tempfile.TemporaryDirectory(prefix="hermes-sync-back-") as staging:
                with tarfile.open(tf.name) as tar:
                    tar.extractall(staging, filter="data")

                applied = 0
                for dirpath, _dirnames, filenames in os.walk(staging):
                    for fname in filenames:
                        staged_file = os.path.join(dirpath, fname)
                        rel = os.path.relpath(staged_file, staging)
                        remote_path = "/" + rel

                        pushed_hash = self._pushed_hashes.get(remote_path)

                        # 对自推送以来未变更的文件跳过哈希计算
                        if pushed_hash is not None:
                            remote_hash = _sha256_file(staged_file)
                            if remote_hash == pushed_hash:
                                continue
                        else:
                            remote_hash = None  # 远程新增文件

                        # 从缓存映射解析宿主机路径
                        host_path = self._resolve_host_path(remote_path, file_mapping)
                        if host_path is None:
                            host_path = self._infer_host_path(remote_path, file_mapping)
                            if host_path is None:
                                logger.debug(
                                    "sync_back: skipping %s (no host mapping)",
                                    remote_path,
                                )
                                continue

                        if os.path.exists(host_path) and pushed_hash is not None:
                            host_hash = _sha256_file(host_path)
                            if host_hash != pushed_hash:
                                logger.warning(
                                    "sync_back: conflict on %s — host modified "
                                    "since push, remote also changed. Applying "
                                    "remote version (last-write-wins).",
                                    remote_path,
                                )

                        os.makedirs(os.path.dirname(host_path), exist_ok=True)
                        shutil.copy2(staged_file, host_path)
                        applied += 1

                if applied:
                    logger.info("sync_back: applied %d changed file(s)", applied)
                else:
                    logger.debug("sync_back: no remote changes detected")

    def _resolve_host_path(self, remote_path: str,
                           file_mapping: list[tuple[str, str]] | None = None) -> str | None:
        """从文件映射中为已知的远程路径查找对应的宿主机路径。"""
        mapping = file_mapping if file_mapping is not None else []
        for host, remote in mapping:
            if remote == remote_path:
                return host
        return None

    def _infer_host_path(self, remote_path: str,
                         file_mapping: list[tuple[str, str]] | None = None) -> str | None:
        """通过匹配路径前缀，为新的远程文件推断宿主机路径。

        利用已有的文件映射找到一个「远程->宿主机」目录对，然后对新文件套用相同的前缀
        替换。例如，映射中有 ``/root/.hermes/skills/a.md`` → ``~/.hermes/skills/a.md``，
        那么位于 ``/root/.hermes/skills/b.md`` 的新远程文件就映射到
        ``~/.hermes/skills/b.md``。
        """
        mapping = file_mapping if file_mapping is not None else []
        for host, remote in mapping:
            remote_dir = str(Path(remote).parent)
            if remote_path.startswith(remote_dir + "/"):
                host_dir = str(Path(host).parent)
                suffix = remote_path[len(remote_dir):]
                return host_dir + suffix
        return None
