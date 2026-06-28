"""带 ControlMaster 连接持久化的 SSH 远程执行环境。"""

import hashlib
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from tools.environments.base import BaseEnvironment, _popen_bash
from tools.environments.file_sync import (
    FileSyncManager,
    iter_sync_files,
    quoted_mkdir_command,
    quoted_rm_command,
    unique_parent_dirs,
)

logger = logging.getLogger(__name__)


def _ensure_ssh_available() -> None:
    """当 SSH 客户端不可用时快速失败并给出清晰错误。"""
    if not shutil.which("ssh"):
        raise RuntimeError(
            "SSH is not installed or not in PATH. Install OpenSSH client: apt install openssh-client"
        )
    if not shutil.which("scp"):
        raise RuntimeError(
            "SCP is not installed or not in PATH. Install OpenSSH client: apt install openssh-client"
        )


class SSHEnvironment(BaseEnvironment):
    """通过 SSH 在远程机器上运行命令。

    每次调用单独 spawn：每次 execute() 都会 spawn 一个全新的 ``ssh ... bash -c`` 进程。
    会话快照在多次调用之间保留环境变量。
    CWD 通过带内 stdout 标记持久化。
    使用 SSH ControlMaster 实现连接复用。
    """

    def __init__(self, host: str, user: str, cwd: str = "~",
                 timeout: int = 60, port: int = 22, key_path: str = ""):
        super().__init__(cwd=cwd, timeout=timeout)
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path

        self.control_dir = Path(tempfile.gettempdir()) / "hermes-ssh"
        self.control_dir.mkdir(parents=True, exist_ok=True)
        # 让 socket 文件名保持简短且确定，从而使完整路径不会超过 macOS 对 Unix 域套接字
        # 强制要求的 104 字节 sun_path 上限。原始的 ``user@host:port`` —— 尤其是 IPv6
        # 主机 —— 再加上 ControlMaster 模式下 SSH 追加的 16 字节随机后缀，在 macOS 深层
        # 嵌套的 $TMPDIR（例如 /var/folders/xx/yy/T/）下很容易超出上限。对这个三元组做
        # 哈希能让路径在重连之间保持稳定，从而 ControlMaster 复用仍然有效。
        _socket_id = hashlib.sha256(
            f"{user}@{host}:{port}".encode()
        ).hexdigest()[:16]
        self.control_socket = self.control_dir / f"{_socket_id}.sock"
        _ensure_ssh_available()
        self._establish_connection()
        self._remote_home = self._detect_remote_home()

        self._ensure_remote_dirs()
        self._sync_manager = FileSyncManager(
            get_files_fn=lambda: iter_sync_files(f"{self._remote_home}/.hermes"),
            upload_fn=self._scp_upload,
            delete_fn=self._ssh_delete,
            bulk_upload_fn=self._ssh_bulk_upload,
            bulk_download_fn=self._ssh_bulk_download,
        )
        self._sync_manager.sync(force=True)

        self.init_session()

    def _build_ssh_command(self, extra_args: list | None = None) -> list:
        cmd = ["ssh"]
        cmd.extend(["-o", f"ControlPath={self.control_socket}"])
        cmd.extend(["-o", "ControlMaster=auto"])
        cmd.extend(["-o", "ControlPersist=300"])
        cmd.extend(["-o", "BatchMode=yes"])
        cmd.extend(["-o", "StrictHostKeyChecking=accept-new"])
        cmd.extend(["-o", "ConnectTimeout=10"])
        if self.port != 22:
            cmd.extend(["-p", str(self.port)])
        if self.key_path:
            cmd.extend(["-i", self.key_path])
        if extra_args:
            cmd.extend(extra_args)
        cmd.append(f"{self.user}@{self.host}")
        return cmd

    def _establish_connection(self):
        cmd = self._build_ssh_command()
        cmd.append("echo 'SSH connection established'")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip()
                raise RuntimeError(f"SSH connection failed: {error_msg}")
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"SSH connection to {self.user}@{self.host} timed out")

    def _detect_remote_home(self) -> str:
        """探测远程用户的主目录。"""
        try:
            cmd = self._build_ssh_command()
            cmd.append("echo $HOME")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                stdin=subprocess.DEVNULL,
            )
            home = result.stdout.strip()
            if home and result.returncode == 0:
                logger.debug("SSH: remote home = %s", home)
                return home
        except Exception:
            pass
        if self.user == "root":
            return "/root"
        return f"/home/{self.user}"

    # ------------------------------------------------------------------
    # 文件同步（通过 FileSyncManager）
    # ------------------------------------------------------------------

    def _ensure_remote_dirs(self) -> None:
        """在单次 SSH 调用中于远程创建基础 ~/.hermes 目录树。"""
        base = f"{self._remote_home}/.hermes"
        dirs = [base, f"{base}/skills", f"{base}/credentials", f"{base}/cache"]
        cmd = self._build_ssh_command()
        cmd.append(quoted_mkdir_command(dirs))
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )

    # _get_sync_files 由 FileSyncManager 初始化时的 iter_sync_files 提供

    def _scp_upload(self, host_path: str, remote_path: str) -> None:
        """通过 ControlMaster 之上的 scp 上传单个文件。"""
        parent = str(Path(remote_path).parent)
        mkdir_cmd = self._build_ssh_command()
        mkdir_cmd.append(f"mkdir -p {shlex.quote(parent)}")
        subprocess.run(
            mkdir_cmd,
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )

        scp_cmd = ["scp", "-o", f"ControlPath={self.control_socket}"]
        if self.port != 22:
            scp_cmd.extend(["-P", str(self.port)])
        if self.key_path:
            scp_cmd.extend(["-i", self.key_path])
        scp_cmd.extend([host_path, f"{self.user}@{self.host}:{remote_path}"])
        result = subprocess.run(
            scp_cmd,
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise RuntimeError(f"scp failed: {result.stderr.strip()}")

    def _ssh_bulk_upload(self, files: list[tuple[str, str]]) -> None:
        """通过单条 tar-over-SSH 流上传多个文件。

        把本地的 ``tar c`` 通过 SSH 连接管道到远程的 ``tar x``，用单条 TCP 流传输所有
        文件，而不是为每个文件 spawn 一个子进程。目录创建会事先批量成一次 ``mkdir -p``
        调用。

        典型提升：约 580 个文件从 O(N) 次 scp 往返变成一次流式传输。
        """
        if not files:
            return

        base = f"{self._remote_home}/.hermes"
        parents = unique_parent_dirs(files)
        if parents:
            cmd = self._build_ssh_command()
            cmd.append(quoted_mkdir_command(parents))
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                raise RuntimeError(f"remote mkdir failed: {result.stderr.strip()}")

        # 用符号链接暂存，避免脆弱的 GNU tar --transform 规则。
        # 在未开启开发者模式的 Windows 上，创建符号链接会抛出 winerror 1314
        # （未持有特权）的 OSError。只捕获这一个特定错误并退回到普通拷贝；其余所有
        # OSError（例如磁盘已满、路径错误）都照常重新抛出。
        with tempfile.TemporaryDirectory(prefix="hermes-ssh-bulk-") as staging:
            for host_path, remote_path in files:
                try:
                    rel_remote = os.path.relpath(remote_path, base)
                except ValueError as exc:
                    raise RuntimeError(
                        f"remote path {remote_path!r} is not under sync base {base!r}"
                    ) from exc

                if rel_remote == "." or rel_remote.startswith("../"):
                    raise RuntimeError(
                        f"remote path {remote_path!r} escapes sync base {base!r}"
                    )

                staged = os.path.join(staging, rel_remote)
                os.makedirs(os.path.dirname(staged), exist_ok=True)
                try:
                    os.symlink(os.path.abspath(host_path), staged)
                except OSError as e:
                    # WinError 1314：未持有符号链接特权（未开启开发者模式的 Windows）
                    if getattr(e, "winerror", None) == 1314:
                        shutil.copy2(host_path, staged)
                    else:
                        raise

            tar_cmd = ["tar", "-chf", "-", "-C", staging, "."]
            ssh_cmd = self._build_ssh_command()
            # --no-overwrite-dir 阻止 tar 用暂存目录的 mode 覆盖已存在目录
            # （例如 /home/<user>）的 mode。否则 umask 002 会产生 0775 的目录，
            # 这会破坏 sshd 的 StrictModes（它会拒绝 authorized_keys）。
            ssh_cmd.append(f"tar xf - --no-overwrite-dir -C {shlex.quote(base)}")

            tar_proc = subprocess.Popen(
                tar_cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                ssh_proc = subprocess.Popen(
                    ssh_cmd, stdin=tar_proc.stdout, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except Exception:
                tar_proc.kill()
                tar_proc.wait()
                raise

            # 若 ssh_proc 提前退出，允许 tar_proc 收到 SIGPIPE
            tar_proc.stdout.close()

            try:
                _, ssh_stderr = ssh_proc.communicate(timeout=120)
                # 用 communicate() 而不是 wait() 来排空 stderr，避免 tar 产生的错误
                # 超过 PIPE_BUF 时发生死锁。
                tar_stderr_raw = b""
                if tar_proc.poll() is None:
                    _, tar_stderr_raw = tar_proc.communicate(timeout=10)
                else:
                    tar_stderr_raw = tar_proc.stderr.read() if tar_proc.stderr else b""
            except subprocess.TimeoutExpired:
                tar_proc.kill()
                ssh_proc.kill()
                tar_proc.wait()
                ssh_proc.wait()
                raise RuntimeError("SSH bulk upload timed out")

            if tar_proc.returncode != 0:
                raise RuntimeError(
                    f"tar create failed (rc={tar_proc.returncode}): "
                    f"{tar_stderr_raw.decode(errors='replace').strip()}"
                )
            if ssh_proc.returncode != 0:
                raise RuntimeError(
                    f"tar extract over SSH failed (rc={ssh_proc.returncode}): "
                    f"{ssh_stderr.decode(errors='replace').strip()}"
                )

        logger.debug("SSH: bulk-uploaded %d file(s) via tar pipe", len(files))

    def _ssh_bulk_download(self, dest: Path) -> None:
        """把远程的 .hermes/ 作为 tar 归档下载下来。"""
        # 从 / 开始 tar 并带上完整路径，这样归档条目会保留绝对路径
        # （例如 home/user/.hermes/skills/f.py），与 _pushed_hashes 的键匹配。
        rel_base = f"{self._remote_home}/.hermes".lstrip("/")
        ssh_cmd = self._build_ssh_command()
        ssh_cmd.append(f"tar cf - -C / {shlex.quote(rel_base)}")
        with open(dest, "wb") as f:
            result = subprocess.run(
                ssh_cmd,
                stdin=subprocess.DEVNULL,
                stdout=f,
                stderr=subprocess.PIPE,
                timeout=120,
            )
        if result.returncode != 0:
            raise RuntimeError(f"SSH bulk download failed: {result.stderr.decode(errors='replace').strip()}")

    def _ssh_delete(self, remote_paths: list[str]) -> None:
        """在单次 SSH 调用中批量删除远程文件。"""
        cmd = self._build_ssh_command()
        cmd.append(quoted_rm_command(remote_paths))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise RuntimeError(f"remote rm failed: {result.stderr.strip()}")

    def _before_execute(self) -> None:
        """通过 FileSyncManager 把文件同步到远程（内部已做限流）。"""
        self._sync_manager.sync()

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        """spawn 一个在远程主机上运行 bash 的 SSH 进程。"""
        cmd = self._build_ssh_command()
        if login:
            cmd.extend(["bash", "-l", "-c", shlex.quote(cmd_string)])
        else:
            cmd.extend(["bash", "-c", shlex.quote(cmd_string)])

        return _popen_bash(cmd, stdin_data)

    def cleanup(self):
        if self._sync_manager:
            logger.info("SSH: syncing files from sandbox...")
            self._sync_manager.sync_back()

        if self.control_socket.exists():
            try:
                cmd = ["ssh", "-o", f"ControlPath={self.control_socket}",
                       "-O", "exit", f"{self.user}@{self.host}"]
                subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=5,
                    stdin=subprocess.DEVNULL,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                self.control_socket.unlink()
            except OSError:
                pass
