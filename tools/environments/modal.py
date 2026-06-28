"""基于原生 Modal SDK 的 Modal 云执行环境。

使用 ``Sandbox.create()`` + ``Sandbox.exec()`` 而不是旧的 runtime 包装器，
同时保留 Hermes 在多次会话之间持久化的快照行为。
"""

import asyncio
import base64
import io
import logging
import shlex
import tarfile
import threading
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home
from tools.environments.base import (
    BaseEnvironment,
    _ThreadedProcessHandle,
    _load_json_store,
    _save_json_store,
)
from tools.environments.file_sync import (
    FileSyncManager,
    iter_sync_files,
    quoted_mkdir_command,
    quoted_rm_command,
    unique_parent_dirs,
)

logger = logging.getLogger(__name__)

_SNAPSHOT_STORE = get_hermes_home() / "modal_snapshots.json"
_DIRECT_SNAPSHOT_NAMESPACE = "direct"


def _load_snapshots() -> dict:
    return _load_json_store(_SNAPSHOT_STORE)


def _save_snapshots(data: dict) -> None:
    _save_json_store(_SNAPSHOT_STORE, data)


def _direct_snapshot_key(task_id: str) -> str:
    return f"{_DIRECT_SNAPSHOT_NAMESPACE}:{task_id}"


def _get_snapshot_restore_candidate(task_id: str) -> tuple[str | None, bool]:
    snapshots = _load_snapshots()
    namespaced_key = _direct_snapshot_key(task_id)
    snapshot_id = snapshots.get(namespaced_key)
    if isinstance(snapshot_id, str) and snapshot_id:
        return snapshot_id, False
    legacy_snapshot_id = snapshots.get(task_id)
    if isinstance(legacy_snapshot_id, str) and legacy_snapshot_id:
        return legacy_snapshot_id, True
    return None, False


def _store_direct_snapshot(task_id: str, snapshot_id: str) -> None:
    snapshots = _load_snapshots()
    snapshots[_direct_snapshot_key(task_id)] = snapshot_id
    snapshots.pop(task_id, None)
    _save_snapshots(snapshots)


def _delete_direct_snapshot(task_id: str, snapshot_id: str | None = None) -> None:
    snapshots = _load_snapshots()
    updated = False
    for key in (_direct_snapshot_key(task_id), task_id):
        value = snapshots.get(key)
        if value is None:
            continue
        if snapshot_id is None or value == snapshot_id:
            snapshots.pop(key, None)
            updated = True
    if updated:
        _save_snapshots(snapshots)


def _ensure_modal_sdk() -> None:
    """按需懒安装 modal。幂等 —— 安装后是快速的无操作。"""
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("terminal.modal", prompt=False)
    except ImportError:
        pass
    except Exception as e:
        raise ImportError(str(e))


def _resolve_modal_image(image_spec: Any) -> Any:
    """把注册表引用或快照 id 转换成 Modal image 对象。

    包含对 ubuntu/debian 镜像的 add_python 支持（吸收自 PR 4511）。
    """
    _ensure_modal_sdk()
    import modal as _modal

    if not isinstance(image_spec, str):
        return image_spec

    if image_spec.startswith("im-"):
        return _modal.Image.from_id(image_spec)

    # PR 4511：为没有自带 python 的 ubuntu/debian 镜像添加 python。
    lower = image_spec.lower()
    add_python = any(base in lower for base in ("ubuntu", "debian"))

    setup_commands = [
        "RUN rm -rf /usr/local/lib/python*/site-packages/pip* 2>/dev/null; "
        "python -m ensurepip --upgrade --default-pip 2>/dev/null || true",
    ]
    if add_python:
        setup_commands.insert(0,
            "RUN apt-get update -qq && apt-get install -y -qq python3 python3-venv > /dev/null 2>&1 || true"
        )

    return _modal.Image.from_registry(
        image_spec,
        setup_dockerfile_commands=setup_commands,
    )


class _AsyncWorker:
    """带独立事件循环的后台线程，用于 async 安全的 Modal 调用。"""

    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._started.wait(timeout=30)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._started.set()
        self._loop.run_forever()

    def run_coroutine(self, coro, timeout=600):
        from agent.async_utils import safe_schedule_threadsafe
        if self._loop is None or self._loop.is_closed():
            if asyncio.iscoroutine(coro):
                coro.close()
            raise RuntimeError("AsyncWorker loop is not running")
        future = safe_schedule_threadsafe(coro, self._loop)
        if future is None:
            raise RuntimeError("AsyncWorker loop is not running")
        return future.result(timeout=timeout)

    def stop(self):
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=10)


class ModalEnvironment(BaseEnvironment):
    """通过原生 Modal 沙箱实现的 Modal 云执行。

    通过 _ThreadedProcessHandle 包装 async SDK 调用实现每次调用单独 spawn。
    cancel_fn 接到 sandbox.terminate 以支持中断。
    """

    _stdin_mode = "heredoc"
    _snapshot_timeout = 60  # Modal 冷启动可能很慢

    def __init__(
        self,
        image: str,
        cwd: str = "/root",
        timeout: int = 60,
        modal_sandbox_kwargs: Optional[dict[str, Any]] = None,
        persistent_filesystem: bool = True,
        task_id: str = "default",
    ):
        super().__init__(cwd=cwd, timeout=timeout)

        self._persistent = persistent_filesystem
        self._task_id = task_id
        self._sandbox = None
        self._app = None
        self._worker = _AsyncWorker()
        self._sync_manager: FileSyncManager | None = None  # 在沙箱创建之后初始化

        sandbox_kwargs = dict(modal_sandbox_kwargs or {})

        restored_snapshot_id = None
        restored_from_legacy_key = False
        if self._persistent:
            restored_snapshot_id, restored_from_legacy_key = _get_snapshot_restore_candidate(
                self._task_id
            )
            if restored_snapshot_id:
                logger.info("Modal: restoring from snapshot %s", restored_snapshot_id[:20])

        _ensure_modal_sdk()
        import modal as _modal

        cred_mounts = []
        try:
            from tools.credential_files import (
                get_credential_file_mounts,
                iter_skills_files,
                iter_cache_files,
            )

            for mount_entry in get_credential_file_mounts():
                cred_mounts.append(
                    _modal.Mount.from_local_file(
                        mount_entry["host_path"],
                        remote_path=mount_entry["container_path"],
                    )
                )
            for entry in iter_skills_files():
                cred_mounts.append(
                    _modal.Mount.from_local_file(
                        entry["host_path"],
                        remote_path=entry["container_path"],
                    )
                )
            cache_files = iter_cache_files()
            for entry in cache_files:
                cred_mounts.append(
                    _modal.Mount.from_local_file(
                        entry["host_path"],
                        remote_path=entry["container_path"],
                    )
                )
        except Exception as e:
            logger.debug("Modal: could not load credential file mounts: %s", e)

        self._worker.start()

        async def _create_sandbox(image_spec: Any):
            app = await _modal.App.lookup.aio("hermes-agent", create_if_missing=True)
            create_kwargs = dict(sandbox_kwargs)
            if cred_mounts:
                existing_mounts = list(create_kwargs.pop("mounts", []))
                existing_mounts.extend(cred_mounts)
                create_kwargs["mounts"] = existing_mounts
            sandbox = await _modal.Sandbox.create.aio(
                "sleep", "infinity",
                image=image_spec,
                app=app,
                timeout=int(create_kwargs.pop("timeout", 3600)),
                **create_kwargs,
            )
            return app, sandbox

        try:
            target_image_spec = restored_snapshot_id or image
            try:
                effective_image = _resolve_modal_image(target_image_spec)
                self._app, self._sandbox = self._worker.run_coroutine(
                    _create_sandbox(effective_image), timeout=300,
                )
            except Exception as exc:
                if not restored_snapshot_id:
                    raise
                logger.warning(
                    "Modal: failed to restore snapshot %s, retrying with base image: %s",
                    restored_snapshot_id[:20], exc,
                )
                _delete_direct_snapshot(self._task_id, restored_snapshot_id)
                base_image = _resolve_modal_image(image)
                self._app, self._sandbox = self._worker.run_coroutine(
                    _create_sandbox(base_image), timeout=300,
                )
            else:
                if restored_snapshot_id and restored_from_legacy_key:
                    _store_direct_snapshot(self._task_id, restored_snapshot_id)
        except Exception:
            self._worker.stop()
            raise

        logger.info("Modal: sandbox created (task=%s)", self._task_id)

        self._sync_manager = FileSyncManager(
            get_files_fn=lambda: iter_sync_files("/root/.hermes"),
            upload_fn=self._modal_upload,
            delete_fn=self._modal_delete,
            bulk_upload_fn=self._modal_bulk_upload,
            bulk_download_fn=self._modal_bulk_download,
        )
        self._sync_manager.sync(force=True)
        self.init_session()

    def _modal_upload(self, host_path: str, remote_path: str) -> None:
        """通过 stdin 管道传输 base64 来上传单个文件。"""
        content = Path(host_path).read_bytes()
        b64 = base64.b64encode(content).decode("ascii")
        container_dir = str(Path(remote_path).parent)
        cmd = (
            f"mkdir -p {shlex.quote(container_dir)} && "
            f"base64 -d > {shlex.quote(remote_path)}"
        )

        async def _write():
            proc = await self._sandbox.exec.aio("bash", "-c", cmd)
            offset = 0
            chunk_size = self._STDIN_CHUNK_SIZE
            while offset < len(b64):
                proc.stdin.write(b64[offset:offset + chunk_size])
                await proc.stdin.drain.aio()
                offset += chunk_size
            proc.stdin.write_eof()
            await proc.stdin.drain.aio()
            await proc.wait.aio()

        self._worker.run_coroutine(_write(), timeout=30)

    # Modal SDK 的 stdin 缓冲区上限（旧的服务端路径）。命令路由器
    # 路径允许 16 MB，但为兼容起见必须保持在更小的 2 MB 上限以下。
    # 分块写入时低于该阈值，并通过 drain() 逐块刷新。
    _STDIN_CHUNK_SIZE = 1 * 1024 * 1024  # 1 MB —— 对两种传输路径都安全

    def _modal_bulk_upload(self, files: list[tuple[str, str]]) -> None:
        """通过 stdin 管道传输 tar 归档来批量上传多个文件。

        在内存中构建一个 gzip 压缩的 tar 归档，并通过进程的 stdin 以流式方式送入
        ``base64 -d | tar xzf -`` 管道，从而规避 Modal SDK 64 KB 的
        ``ARG_MAX_BYTES`` 执行参数上限。
        """
        if not files:
            return

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for host_path, remote_path in files:
                tar.add(host_path, arcname=remote_path.lstrip("/"))
        payload = base64.b64encode(buf.getvalue()).decode("ascii")

        parents = unique_parent_dirs(files)
        mkdir_part = quoted_mkdir_command(parents)
        cmd = f"{mkdir_part} && base64 -d | tar xzf - -C /"

        async def _bulk():
            proc = await self._sandbox.exec.aio("bash", "-c", cmd)

            # 以分块方式通过 stdin 流式传输 payload，以保持在 SDK 每次写入的
            # 缓冲区上限（旧路径 2 MB / 路由器 16 MB）之下。
            offset = 0
            chunk_size = self._STDIN_CHUNK_SIZE
            while offset < len(payload):
                proc.stdin.write(payload[offset:offset + chunk_size])
                await proc.stdin.drain.aio()
                offset += chunk_size

            proc.stdin.write_eof()
            await proc.stdin.drain.aio()

            exit_code = await proc.wait.aio()
            if exit_code != 0:
                stderr_text = await proc.stderr.read.aio()
                raise RuntimeError(
                    f"Modal bulk upload failed (exit {exit_code}): {stderr_text}"
                )

        self._worker.run_coroutine(_bulk(), timeout=120)

    def _modal_bulk_download(self, dest: Path) -> None:
        """把远程的 .hermes/ 作为 tar 归档下载下来。

        Modal 沙箱始终以 root 运行，因此 /root/.hermes 被硬编码
        （与第 269 行 iter_sync_files 的调用保持一致）。
        """
        async def _download():
            proc = await self._sandbox.exec.aio(
                "bash", "-c", "tar cf - -C / root/.hermes"
            )
            data = await proc.stdout.read.aio()
            exit_code = await proc.wait.aio()
            if exit_code != 0:
                raise RuntimeError(f"Modal bulk download failed (exit {exit_code})")
            return data

        tar_bytes = self._worker.run_coroutine(_download(), timeout=120)
        if isinstance(tar_bytes, str):
            tar_bytes = tar_bytes.encode()
        dest.write_bytes(tar_bytes)

    def _modal_delete(self, remote_paths: list[str]) -> None:
        """通过 exec 批量删除远程文件。"""
        rm_cmd = quoted_rm_command(remote_paths)

        async def _rm():
            proc = await self._sandbox.exec.aio("bash", "-c", rm_cmd)
            await proc.wait.aio()

        self._worker.run_coroutine(_rm(), timeout=15)

    def _before_execute(self) -> None:
        """通过 FileSyncManager 把文件同步到沙箱（内部已做限流）。"""
        self._sync_manager.sync()

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120,
                  stdin_data: str | None = None):
        """返回一个包装了 async Modal 沙箱 exec 的 _ThreadedProcessHandle。"""
        sandbox = self._sandbox
        worker = self._worker

        def cancel():
            worker.run_coroutine(sandbox.terminate.aio(), timeout=15)

        def exec_fn() -> tuple[str, int]:
            async def _do():
                args = ["bash"]
                if login:
                    args.extend(["-l", "-c", cmd_string])
                else:
                    args.extend(["-c", cmd_string])
                process = await sandbox.exec.aio(*args, timeout=timeout)
                stdout = await process.stdout.read.aio()
                stderr = await process.stderr.read.aio()
                exit_code = await process.wait.aio()
                if isinstance(stdout, bytes):
                    stdout = stdout.decode("utf-8", errors="replace")
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", errors="replace")
                output = stdout
                if stderr:
                    output = f"{stdout}\n{stderr}" if stdout else stderr
                return output, exit_code

            return worker.run_coroutine(_do(), timeout=timeout + 30)

        return _ThreadedProcessHandle(exec_fn, cancel_fn=cancel)

    def cleanup(self):
        """（若为持久化）对文件系统做快照，然后停止沙箱。"""
        if self._sandbox is None:
            return

        if self._sync_manager:
            logger.info("Modal: syncing files from sandbox...")
            self._sync_manager.sync_back()

        if self._persistent:
            try:
                async def _snapshot():
                    img = await self._sandbox.snapshot_filesystem.aio()
                    return img.object_id

                try:
                    snapshot_id = self._worker.run_coroutine(_snapshot(), timeout=60)
                except Exception:
                    snapshot_id = None

                if snapshot_id:
                    _store_direct_snapshot(self._task_id, snapshot_id)
                    logger.info(
                        "Modal: saved filesystem snapshot %s for task %s",
                        snapshot_id[:20], self._task_id,
                    )
            except Exception as e:
                logger.warning("Modal: filesystem snapshot failed: %s", e)

        try:
            self._worker.run_coroutine(self._sandbox.terminate.aio(), timeout=15)
        except Exception:
            pass
        finally:
            self._worker.stop()
            self._sandbox = None
            self._app = None
