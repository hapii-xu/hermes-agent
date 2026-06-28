"""
WhatsApp 平台适配器。

WhatsApp 集成比 Telegram/Discord 更复杂，原因如下：
- 个人账户没有官方 bot API
- Business API 需要 Meta Business 认证
- 大多数解决方案使用基于 Web 的自动化

本适配器支持多种后端：
1. WhatsApp Business API（需要 Meta 认证）
2. whatsapp-web.js（通过 Node.js 子进程）- 用于个人账户
3. Baileys（通过 Node.js 子进程）- 个人账户的替代方案

为简化起见，我们实现一个通用接口，通过桥接模式与不同后端配合工作。
"""

import asyncio
import logging
import os
import platform
import re
import signal
import subprocess

_IS_WINDOWS = platform.system() == "Windows"
from pathlib import Path
from typing import Dict, Optional, Any

from hermes_constants import (
    find_node_executable,
    get_hermes_dir,
    with_hermes_node_path,
)

logger = logging.getLogger(__name__)


def _listener_pids_on_port(port: int) -> list:
    """返回在 ``port`` 上 *监听* 的进程 PID（POSIX）— 绝不包含客户端。

    这必须仅匹配 LISTEN 状态的 socket。直接使用 ``lsof -i :PORT``（或
    ``fuser PORT/tcp``）还会返回 *客户端*，它们的连接只是恰好涉及
    该端口号——例如浏览器打开了一个本地开发服务器的标签页，
    共享该端口。向这些进程发送 SIGTERM 会不规则地关闭用户的浏览器。
    限制为 LISTEN 状态可以为新的桥接释放端口，而不会影响无关的客户端。
    """
    pids: list = []
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            try:
                pids.append(int(line))
            except ValueError:
                pass
        if pids:
            return pids
    except FileNotFoundError:
        pass  # lsof 未安装 — 回退到 ss
    # 回退方案：ss（iproute2，几乎每个现代 Linux 都有）
    try:
        result = subprocess.run(
            ["ss", "-ltnHp", f"sport = :{port}"],
            capture_output=True, text=True, timeout=5,
        )
        for m in re.finditer(r"pid=(\d+)", result.stdout):
            pids.append(int(m.group(1)))
    except FileNotFoundError:
        pass
    return pids


def _kill_port_process(port: int) -> None:
    """杀死在给定 TCP 端口上 *监听* 的任何进程（一个过期的桥接）。"""
    try:
        if _IS_WINDOWS:
            # 使用 netstat 查找绑定到此端口的 PID，然后用 taskkill 结束
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[3] == "LISTENING":
                    local_addr = parts[1]
                    if local_addr.endswith(f":{port}"):
                        try:
                            subprocess.run(
                                ["taskkill", "/PID", parts[4], "/F"],
                                capture_output=True, timeout=5,
                            )
                        except subprocess.SubprocessError:
                            pass
        else:
            # POSIX：仅向在此端口上 LISTENING 的进程发送信号。连接恰好
            # 涉及此端口号的客户端（如本地开发服务器上的浏览器标签页）
            # 绝不能被杀死。
            for pid in _listener_pids_on_port(port):
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
    except Exception:
        pass


def _bridge_pid_is_ours(pid: int, session_path: Path, expected_start) -> bool:
    """仅当 ``pid`` 存活且仍然是此会话的 node 桥接时返回 True。

    PID 是从之前运行写入的文件中读取的。该进程退出并被内核回收后，
    该数字可能被回收分配给无关进程——实际观察中发现它落在了桌面浏览器
    的主进程上，裸存活检查 ``os.kill`` 随后发送 SIGTERM，不规则地关闭
    整个浏览器（每次桥接抖动重启时都会发生）。

    身份通过两种方式确认：写入 pidfile 时记录的内核启动时间（确定的），
    以及——对于没有基线的旧版 pidfile——命令行必须包含 ``node`` 和此
    会话的唯一路径。回收的 PID（不同的启动时间/不同的 cmdline）绝不是我们的。
    """
    from gateway.status import _pid_exists
    if not _pid_exists(pid):
        return False
    if expected_start is not None:
        from gateway.status import get_process_start_time
        # 匹配的 (pid, 启动时间) 对唯一标识一个进程。
        return get_process_start_time(pid) == expected_start
    # 旧版 pidfile（没有记录的启动时间）：回退到命令行签名，
    # 这样回收的 PID 仍然不会被发送信号。如果我们无法读取
    # cmdline，我们拒绝杀死而不是冒误杀陌生进程的风险。
    from gateway.status import _read_process_cmdline
    cmdline = _read_process_cmdline(pid)
    if not cmdline:
        return False
    return ("node" in cmdline) and (str(session_path) in cmdline)


def _kill_stale_bridge_by_pidfile(session_path: Path) -> None:
    """杀死在之前运行的 PID 文件中记录的桥接进程。

    桥接启动时将 ``bridge.pid`` 写入会话目录。如果网关在没有
    干净关闭的情况下崩溃，旧的桥接进程就会变成孤儿——此辅助函数
    会找到并杀死它。

    关键是，在任何信号发送之前，记录的 PID 都会针对实际进程重新验证
    （:func:`_bridge_pid_is_ours`），因此现在指向无关进程（如用户的
    浏览器）的回收 PID 永远不会被杀死。
    """
    pid_file = session_path / "bridge.pid"
    if not pid_file.exists():
        return
    pid = None
    recorded_start = None
    try:
        # 格式：第 1 行 = pid，可选第 2 行 = 内核启动时间。在此
        # 保护机制存在之前写入的旧文件只有 pid。
        lines = pid_file.read_text().split("\n")
        pid = int(lines[0].strip())
        if len(lines) > 1 and lines[1].strip():
            recorded_start = int(lines[1].strip())
    except (ValueError, OSError, TypeError, IndexError):
        try:
            pid_file.unlink()
        except OSError:
            pass
        return
    if _bridge_pid_is_ours(pid, session_path, recorded_start):
        try:
            os.kill(pid, signal.SIGTERM)
            logger.info("[whatsapp] Killed stale bridge PID %d from pidfile", pid)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    else:
        from gateway.status import _pid_exists
        if _pid_exists(pid):
            logger.warning(
                "[whatsapp] Not killing pidfile PID %d: it is no longer the "
                "bridge (recycled onto an unrelated process); skipping to avoid "
                "killing a stranger.", pid,
            )
    try:
        pid_file.unlink()
    except OSError:
        pass


def _write_bridge_pidfile(session_path: Path, pid: int) -> None:
    """写入桥接 PID（及其内核启动时间）以供后续清理。

    第 2 行的启动时间让未来的运行可以在发送信号前证明 PID 仍然指向
    这个确切的进程，这样回收的 PID 就不会被当作"过期桥接"杀死。
    旧的单行文件仍然可以读取。
    """
    try:
        from gateway.status import get_process_start_time
        start = get_process_start_time(pid)
        text = str(pid) if start is None else "{}\n{}".format(pid, start)
        (session_path / "bridge.pid").write_text(text)
    except OSError:
        pass


def _terminate_bridge_process(proc, *, force: bool = False) -> None:
    """尽可能使用进程树语义终止桥接进程。"""
    if _IS_WINDOWS:
        cmd = ["taskkill", "/PID", str(proc.pid), "/T"]
        if force:
            cmd.append("/F")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError:
            if force:
                proc.kill()
            else:
                proc.terminate()
            return

        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            raise OSError(details or f"taskkill failed for PID {proc.pid}")
        return

    import psutil
    try:
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        if force:
            for child in children:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            parent.kill()
        else:
            for child in children:
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            parent.terminate()
    except psutil.NoSuchProcess:
        return

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from gateway.config import Platform, PlatformConfig
from gateway.platforms.whatsapp_common import WhatsAppBehaviorMixin
from gateway.whatsapp_identity import to_whatsapp_jid
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    SUPPORTED_DOCUMENT_TYPES,
    cache_image_from_url,
    cache_audio_from_url,
)
from utils import env_int


def _file_content_hash(path: Path) -> str:
    """返回 *path* 内容的 SHA-256 的前 16 个十六进制字符。

    用于桥接过期握手：bridge.js 在 ``/health`` 中报告其自身的
    源代码哈希（``scriptHash``），适配器将其与磁盘上当前 bridge.js
    的哈希进行比较。不匹配意味着长期运行的桥接进程正在提供
    更新之前的代码。当文件无法读取时返回 ``""``。
    """
    import hashlib
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def check_whatsapp_requirements() -> bool:
    """
    检查 WhatsApp 依赖是否可用。

    WhatsApp 大多数实现需要 Node.js 桥接。
    """
    # 优先使用 Hermes 管理的 Node/npm，这样 Windows 安装不会被 PATH 上
    # 有问题的或需要提权的系统 Node 破坏。
    _node = find_node_executable("node")
    if not _node:
        return False
    try:
        result = subprocess.run(
            [_node, "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


class WhatsAppAdapter(WhatsAppBehaviorMixin, BasePlatformAdapter):
    """
    WhatsApp 适配器。

    此实现使用简单的 HTTP 桥接模式：
    1. Node.js 进程运行 WhatsApp Web 客户端
    2. 消息通过 HTTP/IPC 转发到此 Python 适配器
    3. 响应通过桥接发回

    实际的 Node.js 桥接实现可以是：
    - 基于 whatsapp-web.js
    - 基于 Baileys
    - 基于 Business API

    配置项：
    - bridge_script: Node.js 桥接脚本的路径
    - bridge_port: HTTP 通信端口（默认：3000）
    - session_path: 存储 WhatsApp 会话数据的路径
    - dm_policy: "open" | "allowlist" | "disabled" — 私信处理方式（默认："open"）
    - allow_from: 私信中允许的发送者 ID 列表（当 dm_policy="allowlist" 时）
    - group_policy: "open" | "allowlist" | "disabled" — 处理哪些群组（默认："open"）
    - group_allow_from: 允许的群组 JID 列表（当 group_policy="allowlist" 时）

    行为（门控、提及解析、markdown 转换、分块）由 ``WhatsAppBehaviorMixin``
    提供，以便 Cloud API 适配器可以共享。此处只包含传输特定的代码。
    """

    # 默认桥接位置，通过共享辅助函数解析
    _DEFAULT_BRIDGE_DIR = None  # 在 __init__ 中解析
    splits_long_messages = True  # send() 通过 truncate_message() 进行分块

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WHATSAPP)
        # 使用共享辅助函数解析桥接目录（处理只读安装树）
        if WhatsAppAdapter._DEFAULT_BRIDGE_DIR is None:
            from gateway.platforms.whatsapp_common import resolve_whatsapp_bridge_dir
            WhatsAppAdapter._DEFAULT_BRIDGE_DIR = resolve_whatsapp_bridge_dir()
        self._bridge_process: Optional[subprocess.Popen] = None
        self._bridge_port: int = config.extra.get("bridge_port", 3000)
        self._bridge_script: Optional[str] = config.extra.get(
            "bridge_script",
            str(self._DEFAULT_BRIDGE_DIR / "bridge.js"),
        )
        self._session_path: Path = Path(config.extra.get(
            "session_path",
            get_hermes_dir("platforms/whatsapp/session", "whatsapp/session")
        ))
        self._reply_prefix: Optional[str] = config.extra.get("reply_prefix")
        self._dm_policy = str(config.extra.get("dm_policy") or os.getenv("WHATSAPP_DM_POLICY", "open")).strip().lower()
        self._allow_from = self._coerce_allow_list(config.extra.get("allow_from") or config.extra.get("allowFrom"))
        self._group_policy = str(config.extra.get("group_policy") or os.getenv("WHATSAPP_GROUP_POLICY", "open")).strip().lower()
        self._group_allow_from = self._coerce_allow_list(config.extra.get("group_allow_from") or config.extra.get("groupAllowFrom"))
        self._mention_patterns = self._compile_mention_patterns()
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._bridge_log_fh = None
        self._bridge_log: Optional[Path] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._http_session: Optional["aiohttp.ClientSession"] = None
        # 在 disconnect() 发送 SIGTERM 给我们的子桥接之前设为 True，
        # 以便 _check_managed_bridge_exit() 可以区分故意的关闭时退出
        # （returncode -15 / -2 / 0）和真正的崩溃。
        # 没有这个，每次网关优雅关闭/重启都会记录
        # "Fatal whatsapp adapter error" 并在正常的 "✓ whatsapp disconnected"
        # 触发之前发送致命错误通知。
        self._shutting_down: bool = False

        # 文本防抖批处理（与 Telegram 适配器模式一致）。
        # WhatsApp 经常快速连续发送多条消息（例如转发批次、粘贴分割）
        # ——没有防抖的话，每条消息都会触发单独的 agent 调用，浪费 token
        # 并以回复片段轰炸用户。默认 5 秒延迟 / 10 秒分割延迟对于
        # WhatsApp 的发送节奏来说比较保守。可通过 config.yaml 中的
        # ``gateway.platforms.whatsapp.extra.text_batch_delay_seconds`` /
        # ``text_batch_split_delay_seconds`` 调整。
        self._text_batch_delay_seconds = self._coerce_float_extra(
            "text_batch_delay_seconds", 5.0
        )
        self._text_batch_split_delay_seconds = self._coerce_float_extra(
            "text_batch_split_delay_seconds", 10.0
        )
        self._pending_text_batches: Dict[str, MessageEvent] = {}
        self._pending_text_batch_tasks: Dict[str, asyncio.Task] = {}

    def _coerce_float_extra(self, key: str, default: float) -> float:
        """从 ``config.extra`` 读取浮点数，防止错误/非有限值。

        结果直接传给 ``asyncio.sleep()``，因此 NaN/Inf 和无法解析的值
        会回退到 ``default``。
        """
        import math

        value = self.config.extra.get(key) if getattr(self.config, "extra", None) else None
        if value is None:
            return float(default)
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return float(default)
        if not math.isfinite(parsed) or parsed < 0:
            return float(default)
        return parsed

    async def connect(self) -> bool:
        """
        启动 WhatsApp 桥接。

        这会启动 Node.js 桥接进程并等待其准备就绪。
        """
        if not check_whatsapp_requirements():
            logger.warning("[%s] Node.js not found. WhatsApp requires Node.js.", self.name)
            self._set_fatal_error(
                "whatsapp_node_missing",
                "Node.js is not installed — install Node.js and re-run `hermes gateway`.",
                retryable=False,
            )
            return False
        
        bridge_path = Path(self._bridge_script)
        if not bridge_path.exists():
            logger.warning("[%s] Bridge script not found: %s", self.name, bridge_path)
            self._set_fatal_error(
                "whatsapp_bridge_missing",
                f"WhatsApp bridge script missing at {bridge_path}.",
                retryable=False,
            )
            return False

        # Pre-flight: skip the 30s bridge bootstrap entirely if the user
        # never finished pairing.  Without creds.json the bridge prints
        # QR codes to its log file and never reaches status:connected,
        # so every gateway restart paid the 30s timeout + queued WhatsApp
        # for indefinite retries.  Mark non-retryable so the user gets a
        # clear "run hermes whatsapp" message instead of the watcher
        # silently hammering an unconfigured platform.
        creds_path = self._session_path / "creds.json"
        if not creds_path.exists():
            logger.warning(
                "[%s] WhatsApp is enabled but not paired (no creds.json at %s). "
                "Run `hermes whatsapp` to pair, or remove WHATSAPP_ENABLED from "
                "your .env to disable.",
                self.name, creds_path,
            )
            self._set_fatal_error(
                "whatsapp_not_paired",
                "WhatsApp enabled but not paired — run `hermes whatsapp` to pair.",
                retryable=False,
            )
            return False

        logger.info("[%s] Bridge found at %s", self.name, bridge_path)
        
        # Acquire scoped lock to prevent duplicate sessions
        lock_acquired = False
        try:
            if not self._acquire_platform_lock('whatsapp-session', str(self._session_path), 'WhatsApp session'):
                return False
            lock_acquired = True
        except Exception as e:
            logger.warning("[%s] Could not acquire session lock (non-fatal): %s", self.name, e)

        try:
            # Auto-install npm dependencies when node_modules is missing OR
            # package.json changed since the last install (e.g. after
            # `hermes update` bumps the Baileys pin).  The stamp file records
            # the package.json hash of the last successful install.
            bridge_dir = bridge_path.parent
            _pkg_json = bridge_dir / "package.json"
            _dep_stamp = bridge_dir / "node_modules" / ".hermes-pkg-hash"
            _pkg_hash = _file_content_hash(_pkg_json)
            _deps_fresh = False
            if (bridge_dir / "node_modules").exists():
                try:
                    _deps_fresh = (_dep_stamp.read_text().strip() == _pkg_hash) and bool(_pkg_hash)
                except OSError:
                    _deps_fresh = False
            if not _deps_fresh:
                print(f"[{self.name}] Installing WhatsApp bridge dependencies...")
                # Resolve npm path so Windows uses npm.cmd from the
                # Hermes-managed portable Node before falling back to PATH.
                _npm_bin = find_node_executable("npm") or "npm"
                try:
                    # Read timeout from environment variable, default to 300 seconds (5 minutes)
                    # to accommodate slower systems like Unraid NAS
                    npm_install_timeout = env_int("WHATSAPP_NPM_INSTALL_TIMEOUT", 300)
                    install_result = subprocess.run(
                        [_npm_bin, "install", "--silent"],
                        cwd=str(bridge_dir),
                        capture_output=True,
                        text=True,
                        timeout=npm_install_timeout,
                        env=with_hermes_node_path(),
                    )
                    if install_result.returncode != 0:
                        print(f"[{self.name}] npm install failed: {install_result.stderr}")
                        return False
                    print(f"[{self.name}] Dependencies installed")
                    if _pkg_hash:
                        try:
                            _dep_stamp.write_text(_pkg_hash)
                        except OSError:
                            pass  # Stamp is an optimization; install still succeeded
                except Exception as e:
                    print(f"[{self.name}] Failed to install dependencies: {e}")
                    return False

            # Ensure session directory exists
            self._session_path.mkdir(parents=True, exist_ok=True)
            
            # Check if bridge is already running and connected
            import aiohttp
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"http://127.0.0.1:{self._bridge_port}/health",
                        timeout=aiohttp.ClientTimeout(total=2)
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            bridge_status = data.get("status", "unknown")
                            if bridge_status == "connected":
                                # Staleness handshake: only reuse a running
                                # bridge if it is serving the same bridge.js
                                # that is on disk right now.  A long-lived
                                # bridge survives gateway restarts AND
                                # `hermes update`, so without this check it
                                # keeps serving pre-update code forever
                                # (e.g. no inbound media download).  Old
                                # bridges that don't report scriptHash are
                                # treated as stale by definition.
                                running_hash = data.get("scriptHash", "")
                                disk_hash = _file_content_hash(bridge_path)
                                if running_hash and disk_hash and running_hash == disk_hash:
                                    print(f"[{self.name}] Using existing bridge (status: {bridge_status})")
                                    self._mark_connected()
                                    self._bridge_process = None  # Not managed by us
                                    self._http_session = aiohttp.ClientSession()
                                    self._poll_task = asyncio.create_task(self._poll_messages())
                                    return True
                                print(
                                    f"[{self.name}] Running bridge is stale "
                                    f"(running={running_hash or 'unversioned'}, disk={disk_hash}), restarting"
                                )
                            else:
                                print(f"[{self.name}] Bridge found but not connected (status: {bridge_status}), restarting")
            except Exception:
                pass  # Bridge not running, start a new one
            
            # Kill any orphaned bridge from a previous gateway run
            _kill_stale_bridge_by_pidfile(self._session_path)
            _kill_port_process(self._bridge_port)
            await asyncio.sleep(1)
            
            # Start the bridge process in its own process group.
            # Route output to a log file so QR codes, errors, and reconnection
            # messages are preserved for troubleshooting.
            whatsapp_mode = os.getenv("WHATSAPP_MODE", "self-chat")
            self._bridge_log = self._session_path.parent / "bridge.log"
            bridge_log_fh = open(self._bridge_log, "a", encoding="utf-8")
            self._bridge_log_fh = bridge_log_fh

            # Build bridge subprocess environment.
            # Pass WHATSAPP_REPLY_PREFIX from config.yaml so the Node bridge
            # can use it without the user needing to set a separate env var.
            # with_hermes_node_path() copies os.environ when called with no arg.
            bridge_env = with_hermes_node_path()
            if self._reply_prefix is not None:
                bridge_env["WHATSAPP_REPLY_PREFIX"] = self._reply_prefix
            # Pass the profile-aware cache directories so the bridge writes
            # media where the Python side reads it.  Without these the bridge
            # hardcodes ~/.hermes/{image,audio,document}_cache, which diverges
            # under HERMES_HOME overrides, profiles, and the new cache/ layout.
            from gateway.platforms.base import (
                get_audio_cache_dir as _get_audio_dir,
                get_document_cache_dir as _get_doc_dir,
                get_image_cache_dir as _get_img_dir,
            )
            bridge_env["HERMES_IMAGE_CACHE_DIR"] = str(_get_img_dir())
            bridge_env["HERMES_AUDIO_CACHE_DIR"] = str(_get_audio_dir())
            bridge_env["HERMES_DOCUMENT_CACHE_DIR"] = str(_get_doc_dir())

            self._bridge_process = subprocess.Popen(
                [
                    find_node_executable("node") or "node",
                    str(bridge_path),
                    "--port", str(self._bridge_port),
                    "--session", str(self._session_path),
                    "--mode", whatsapp_mode,
                ],
                stdout=bridge_log_fh,
                stderr=bridge_log_fh,
                preexec_fn=None if _IS_WINDOWS else os.setsid,
                env=bridge_env,
            )
            _write_bridge_pidfile(self._session_path, self._bridge_process.pid)
            
            # Wait for the bridge to connect to WhatsApp.
            # Phase 1: wait for the HTTP server to come up (up to 15s).
            # Phase 2: wait for WhatsApp status: connected (up to 15s more).
            import aiohttp
            http_ready = False
            data = {}
            for attempt in range(15):
                await asyncio.sleep(1)
                if self._bridge_process.poll() is not None:
                    print(f"[{self.name}] Bridge process died (exit code {self._bridge_process.returncode})")
                    print(f"[{self.name}] Check log: {self._bridge_log}")
                    self._close_bridge_log()
                    return False
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            f"http://127.0.0.1:{self._bridge_port}/health",
                            timeout=aiohttp.ClientTimeout(total=2)
                        ) as resp:
                            if resp.status == 200:
                                http_ready = True
                                data = await resp.json()
                                if data.get("status") == "connected":
                                    print(f"[{self.name}] Bridge ready (status: connected)")
                                    break
                except Exception:
                    continue

            if not http_ready:
                print(f"[{self.name}] Bridge HTTP server did not start in 15s")
                print(f"[{self.name}] Check log: {self._bridge_log}")
                self._close_bridge_log()
                return False
            
            # Phase 2: HTTP is up but WhatsApp may still be connecting.
            # Give it more time to authenticate with saved credentials.
            if data.get("status") != "connected":
                print(f"[{self.name}] Bridge HTTP ready, waiting for WhatsApp connection...")
                for attempt in range(15):
                    await asyncio.sleep(1)
                    if self._bridge_process.poll() is not None:
                        print(f"[{self.name}] Bridge process died during connection")
                        print(f"[{self.name}] Check log: {self._bridge_log}")
                        self._close_bridge_log()
                        return False
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(
                                f"http://127.0.0.1:{self._bridge_port}/health",
                                timeout=aiohttp.ClientTimeout(total=2)
                            ) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    if data.get("status") == "connected":
                                        print(f"[{self.name}] Bridge ready (status: connected)")
                                        break
                    except Exception:
                        continue
                else:
                    # Still not connected — warn but proceed (bridge may
                    # auto-reconnect later, e.g. after a code 515 restart).
                    print(f"[{self.name}] ⚠ WhatsApp not connected after 30s")
                    print(f"[{self.name}]   Bridge log: {self._bridge_log}")
                    print(f"[{self.name}]   If session expired, re-pair: hermes whatsapp")
            
            # Create a persistent HTTP session for all bridge communication
            self._http_session = aiohttp.ClientSession()

            # Start message polling task
            self._poll_task = asyncio.create_task(self._poll_messages())
            
            self._mark_connected()
            print(f"[{self.name}] Bridge started on port {self._bridge_port}")
            return True
            
        except Exception as e:
            logger.error("[%s] Failed to start bridge: %s", self.name, e, exc_info=True)
            return False
        finally:
            if not self._running:
                if lock_acquired:
                    self._release_platform_lock()
                self._close_bridge_log()
    
    def _close_bridge_log(self) -> None:
        """Close the bridge log file handle if open."""
        if self._bridge_log_fh:
            try:
                self._bridge_log_fh.close()
            except Exception:
                pass
            self._bridge_log_fh = None

    async def _check_managed_bridge_exit(self) -> Optional[str]:
        """Return a fatal error message if the managed bridge child exited."""
        if self._bridge_process is None:
            return None

        returncode = self._bridge_process.poll()
        if returncode is None:
            return None

        # Planned shutdown: disconnect() sets _shutting_down before it sends
        # SIGTERM to the bridge, so a returncode of -15 (SIGTERM), -2 (SIGINT),
        # or 0 (clean exit) at that point is expected, not a crash. Treat it
        # as informational and skip the fatal-error path.
        # getattr-with-default keeps tests that construct the adapter via
        # ``WhatsAppAdapter.__new__`` (bypassing __init__) working without
        # every _make_adapter() helper having to seed the attribute.
        if getattr(self, "_shutting_down", False) and returncode in {0, -2, -15}:
            logger.info(
                "[%s] Bridge exited during shutdown (code %d).",
                self.name,
                returncode,
            )
            return None

        message = f"WhatsApp bridge process exited unexpectedly (code {returncode})."
        if not self.has_fatal_error:
            logger.error("[%s] %s", self.name, message)
            self._set_fatal_error("whatsapp_bridge_exited", message, retryable=True)
            self._close_bridge_log()
            await self._notify_fatal_error()
        return self.fatal_error_message or message

    async def disconnect(self) -> None:
        """Stop the WhatsApp bridge and clean up any orphaned processes."""
        # Flip the shutdown flag BEFORE signalling the child so the exit-check
        # path (which runs from other tasks like send() and the poll loop)
        # doesn't race us and report the intentional termination as fatal.
        self._shutting_down = True
        if self._bridge_process:
            try:
                try:
                    _terminate_bridge_process(self._bridge_process, force=False)
                except (ProcessLookupError, PermissionError):
                    self._bridge_process.terminate()
                await asyncio.sleep(1)
                if self._bridge_process.poll() is None:
                    try:
                        _terminate_bridge_process(self._bridge_process, force=True)
                    except (ProcessLookupError, PermissionError):
                        self._bridge_process.kill()
            except Exception as e:
                print(f"[{self.name}] Error stopping bridge: {e}")
        else:
            # Bridge was not started by us, don't kill it
            print(f"[{self.name}] Disconnecting (external bridge left running)")

        # Clean up PID file
        try:
            (self._session_path / "bridge.pid").unlink(missing_ok=True)
        except OSError:
            pass

        # Cancel the poll task explicitly
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
        self._poll_task = None

        # Close the persistent HTTP session
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        self._http_session = None

        self._release_platform_lock()

        self._mark_disconnected()
        self._bridge_process = None
        self._close_bridge_log()
        print(f"[{self.name}] Disconnected")
    
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> SendResult:
        """Send a message via the WhatsApp bridge.

        Formats markdown for WhatsApp, splits long messages into chunks
        that preserve code block boundaries, and sends each chunk sequentially.
        """
        if not self._running or not self._http_session:
            return SendResult(success=False, error="Not connected")
        bridge_exit = await self._check_managed_bridge_exit()
        if bridge_exit:
            return SendResult(success=False, error=bridge_exit)

        if not content or not content.strip():
            return SendResult(success=True, message_id=None)

        chat_id = to_whatsapp_jid(chat_id)

        try:
            import aiohttp

            # Format and chunk the message
            formatted = self.format_message(content)
            chunks = self.truncate_message(formatted, self._outgoing_chunk_limit())

            last_message_id = None
            for chunk in chunks:
                payload: Dict[str, Any] = {
                    "chatId": chat_id,
                    "message": chunk,
                }
                if reply_to and last_message_id is None:
                    # Only reply-to on the first chunk
                    payload["replyTo"] = reply_to

                async with self._http_session.post(
                    f"http://127.0.0.1:{self._bridge_port}/send",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        last_message_id = data.get("messageId")
                    else:
                        error = await resp.text()
                        return SendResult(success=False, error=error)

                # Small delay between chunks to avoid rate limiting
                if len(chunks) > 1:
                    await asyncio.sleep(0.3)

            return SendResult(
                success=True,
                message_id=last_message_id,
            )
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """Edit a previously sent message via the WhatsApp bridge."""
        if not self._running or not self._http_session:
            return SendResult(success=False, error="Not connected")
        bridge_exit = await self._check_managed_bridge_exit()
        if bridge_exit:
            return SendResult(success=False, error=bridge_exit)
        try:
            import aiohttp
            async with self._http_session.post(
                f"http://127.0.0.1:{self._bridge_port}/edit",
                json={
                    "chatId": to_whatsapp_jid(chat_id),
                    "messageId": message_id,
                    "message": content,
                },
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    return SendResult(success=True, message_id=message_id)
                else:
                    error = await resp.text()
                    return SendResult(success=False, error=error)
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def _send_media_to_bridge(
        self,
        chat_id: str,
        file_path: str,
        media_type: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
    ) -> SendResult:
        """Send any media file via bridge /send-media endpoint."""
        if not self._running or not self._http_session:
            return SendResult(success=False, error="Not connected")
        bridge_exit = await self._check_managed_bridge_exit()
        if bridge_exit:
            return SendResult(success=False, error=bridge_exit)
        try:
            import aiohttp

            if not os.path.exists(file_path):
                return SendResult(success=False, error=f"File not found: {file_path}")

            payload: Dict[str, Any] = {
                "chatId": to_whatsapp_jid(chat_id),
                "filePath": file_path,
                "mediaType": media_type,
            }
            if caption:
                payload["caption"] = caption
            if file_name:
                payload["fileName"] = file_name

            async with self._http_session.post(
                f"http://127.0.0.1:{self._bridge_port}/send-media",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return SendResult(
                        success=True,
                        message_id=data.get("messageId"),
                        raw_response=data,
                    )
                else:
                    error = await resp.text()
                    return SendResult(success=False, error=error)

        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Download image URL to cache, send natively via bridge.

        ``metadata`` is accepted to honor the base-class contract — the
        batch sender ``send_multiple_images`` passes it through to every
        send path. The bridge media call doesn't use it, matching the
        sibling overrides (send_video / send_voice / send_document).
        """
        try:
            local_path = await cache_image_from_url(image_url)
            return await self._send_media_to_bridge(chat_id, local_path, "image", caption)
        except Exception:
            return await super().send_image(chat_id, image_url, caption, reply_to, metadata)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local image file natively via bridge."""
        return await self._send_media_to_bridge(chat_id, image_path, "image", caption)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a video natively via bridge — plays inline in WhatsApp."""
        return await self._send_media_to_bridge(chat_id, video_path, "video", caption)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send an audio file as a WhatsApp voice message via bridge."""
        return await self._send_media_to_bridge(chat_id, audio_path, "audio", caption)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a document/file as a downloadable attachment via bridge."""
        return await self._send_media_to_bridge(
            chat_id, file_path, "document", caption,
            file_name or os.path.basename(file_path),
        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Send typing indicator via bridge."""
        if not self._running or not self._http_session:
            return
        if await self._check_managed_bridge_exit():
            return
        
        try:
            import aiohttp

            # Must wrap in `async with` — a bare `await session.post(...)`
            # leaves the response object alive until GC, holding its TCP
            # socket in CLOSE_WAIT. See #18451.
            async with self._http_session.post(
                f"http://127.0.0.1:{self._bridge_port}/typing",
                json={"chatId": to_whatsapp_jid(chat_id)},
                timeout=aiohttp.ClientTimeout(total=5)
            ):
                pass
        except Exception:
            pass  # Ignore typing indicator failures
    
    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get information about a WhatsApp chat."""
        if not self._running or not self._http_session:
            return {"name": "Unknown", "type": "dm"}
        if await self._check_managed_bridge_exit():
            return {"name": chat_id, "type": "dm"}
        
        try:
            import aiohttp

            async with self._http_session.get(
                f"http://127.0.0.1:{self._bridge_port}/chat/{to_whatsapp_jid(chat_id)}",
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {
                        "name": data.get("name", chat_id),
                        "type": "group" if data.get("isGroup") else "dm",
                        "participants": data.get("participants", []),
                    }
        except Exception as e:
            logger.debug("Could not get WhatsApp chat info for %s: %s", chat_id, e)
        
        return {"name": chat_id, "type": "dm"}
    
    async def _poll_messages(self) -> None:
        """Poll the bridge for incoming messages."""
        import aiohttp

        while self._running:
            if not self._http_session:
                break
            bridge_exit = await self._check_managed_bridge_exit()
            if bridge_exit:
                print(f"[{self.name}] {bridge_exit}")
                break
            try:
                async with self._http_session.get(
                    f"http://127.0.0.1:{self._bridge_port}/messages",
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        messages = await resp.json()
                        for msg_data in messages:
                            event = await self._build_message_event(msg_data)
                            if event:
                                if event.message_type == MessageType.TEXT:
                                    self._enqueue_text_event(event)
                                else:
                                    await self.handle_message(event)
            except asyncio.CancelledError:
                break
            except Exception as e:
                bridge_exit = await self._check_managed_bridge_exit()
                if bridge_exit:
                    print(f"[{self.name}] {bridge_exit}")
                    break
                print(f"[{self.name}] Poll error: {e}")
                await asyncio.sleep(5)
            
            await asyncio.sleep(1)  # Poll interval

    # ── Text debounce batching ──────────────────────────────────────

    _SPLIT_THRESHOLD = 6000  # WhatsApp supports ~65K chars; generous threshold

    def _text_batch_key(self, event: MessageEvent) -> str:
        """Session-scoped key for text message batching."""
        from gateway.session import build_session_key
        return build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )

    def _enqueue_text_event(self, event: MessageEvent) -> None:
        """Buffer a text event and reset the flush timer.

        When WhatsApp delivers rapid-fire messages (e.g. forwarded
        batches), this concatenates them and waits for a short quiet
        period before dispatching the combined message.
        """
        key = self._text_batch_key(event)
        existing = self._pending_text_batches.get(key)
        chunk_len = len(event.text or "")
        if existing is None:
            event._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            self._pending_text_batches[key] = event
        else:
            if event.text:
                existing.text = f"{existing.text}\n{event.text}" if existing.text else event.text
            existing._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            if event.media_urls:
                existing.media_urls.extend(event.media_urls)
                existing.media_types.extend(event.media_types)

        prior_task = self._pending_text_batch_tasks.get(key)
        if prior_task and not prior_task.done():
            prior_task.cancel()
        self._pending_text_batch_tasks[key] = asyncio.create_task(
            self._flush_text_batch(key)
        )

    async def _flush_text_batch(self, key: str) -> None:
        """Wait for quiet period then dispatch aggregated text."""
        current_task = asyncio.current_task()
        try:
            pending = self._pending_text_batches.get(key)
            last_len = getattr(pending, "_last_chunk_len", 0) if pending else 0
            if last_len >= self._SPLIT_THRESHOLD:
                delay = self._text_batch_split_delay_seconds
            else:
                delay = self._text_batch_delay_seconds
            await asyncio.sleep(delay)
            event = self._pending_text_batches.pop(key, None)
            if not event:
                return
            await self.handle_message(event)
        finally:
            if self._pending_text_batch_tasks.get(key) is current_task:
                self._pending_text_batch_tasks.pop(key, None)

    async def _build_message_event(self, data: Dict[str, Any]) -> Optional[MessageEvent]:
        """Build a MessageEvent from bridge message data, downloading images to cache."""
        try:
            if not self._should_process_message(data):
                return None

            # Determine message type
            msg_type = MessageType.TEXT
            if data.get("hasMedia"):
                media_type = data.get("mediaType", "")
                if "image" in media_type:
                    msg_type = MessageType.PHOTO
                elif "video" in media_type:
                    msg_type = MessageType.VIDEO
                elif "audio" in media_type or "ptt" in media_type:  # ptt = voice note
                    msg_type = MessageType.VOICE
                else:
                    msg_type = MessageType.DOCUMENT
            
            # Determine chat type
            is_group = data.get("isGroup", False)
            chat_type = "group" if is_group else "dm"
            
            # Build source
            source = self.build_source(
                chat_id=data.get("chatId", ""),
                chat_name=data.get("chatName"),
                chat_type=chat_type,
                user_id=data.get("senderId"),
                user_name=data.get("senderName"),
            )
            
            # Download media URLs to the local cache so agent tools
            # can access them reliably regardless of URL expiration.
            raw_urls = data.get("mediaUrls", [])
            cached_urls = []
            media_types = []
            for url in raw_urls:
                if msg_type == MessageType.PHOTO and url.startswith(("http://", "https://")):
                    try:
                        cached_path = await cache_image_from_url(url, ext=".jpg")
                        cached_urls.append(cached_path)
                        media_types.append("image/jpeg")
                        print(f"[{self.name}] Cached user image: {cached_path}", flush=True)
                    except Exception as e:
                        print(f"[{self.name}] Failed to cache image: {e}", flush=True)
                        cached_urls.append(url)
                        media_types.append("image/jpeg")
                elif msg_type == MessageType.PHOTO and os.path.isabs(url):
                    # Local file path — bridge already downloaded the image
                    cached_urls.append(url)
                    media_types.append("image/jpeg")
                    print(f"[{self.name}] Using bridge-cached image: {url}", flush=True)
                elif msg_type == MessageType.VOICE and url.startswith(("http://", "https://")):
                    try:
                        cached_path = await cache_audio_from_url(url, ext=".ogg")
                        cached_urls.append(cached_path)
                        media_types.append("audio/ogg")
                        print(f"[{self.name}] Cached user voice: {cached_path}", flush=True)
                    except Exception as e:
                        print(f"[{self.name}] Failed to cache voice: {e}", flush=True)
                        cached_urls.append(url)
                        media_types.append("audio/ogg")
                elif msg_type == MessageType.VOICE and os.path.isabs(url):
                    # Local file path — bridge already downloaded the audio
                    cached_urls.append(url)
                    media_types.append("audio/ogg")
                    print(f"[{self.name}] Using bridge-cached audio: {url}", flush=True)
                elif msg_type == MessageType.DOCUMENT and os.path.isabs(url):
                    # Local file path — bridge already downloaded the document
                    cached_urls.append(url)
                    ext = Path(url).suffix.lower()
                    mime = SUPPORTED_DOCUMENT_TYPES.get(ext, "application/octet-stream")
                    media_types.append(mime)
                    print(f"[{self.name}] Using bridge-cached document: {url}", flush=True)
                elif msg_type == MessageType.VIDEO and os.path.isabs(url):
                    cached_urls.append(url)
                    media_types.append("video/mp4")
                    print(f"[{self.name}] Using bridge-cached video: {url}", flush=True)
                else:
                    cached_urls.append(url)
                    media_types.append("unknown")

            # For text-readable documents, inject file content directly into
            # the message text so the agent can read it inline.
            # Cap at 100KB to match Telegram/Discord/Slack behaviour.
            body = data.get("body", "")
            if data.get("isGroup"):
                body = self._clean_bot_mention_text(body, data)

            # If this is a reply, include the quoted message text so the agent
            # knows exactly what the user is responding to (fixes "approve" context issue)
            quoted_text = str(data.get("quotedText") or "").strip()
            if quoted_text and data.get("hasQuotedMessage"):
                # Truncate long quoted text to keep prompts reasonable
                if len(quoted_text) > 300:
                    quoted_text = quoted_text[:297] + "..."
                body = f"[Replying to: \"{quoted_text}\"]\n{body}"
            MAX_TEXT_INJECT_BYTES = 100 * 1024
            if msg_type == MessageType.DOCUMENT and cached_urls:
                for doc_path in cached_urls:
                    ext = Path(doc_path).suffix.lower()
                    if ext in {".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".log", ".py", ".js", ".ts", ".html", ".css"}:
                        try:
                            file_size = Path(doc_path).stat().st_size
                            if file_size > MAX_TEXT_INJECT_BYTES:
                                print(f"[{self.name}] Skipping text injection for {doc_path} ({file_size} bytes > {MAX_TEXT_INJECT_BYTES})", flush=True)
                                continue
                            content = Path(doc_path).read_text(encoding="utf-8", errors="replace")
                            fname = Path(doc_path).name
                            # Remove the doc_<hex>_ prefix for display
                            display_name = fname
                            if "_" in fname:
                                parts = fname.split("_", 2)
                                if len(parts) >= 3:
                                    display_name = parts[2]
                            injection = f"[Content of {display_name}]:\n{content}"
                            if body:
                                body = f"{injection}\n\n{body}"
                            else:
                                body = injection
                            print(f"[{self.name}] Injected text content from: {doc_path}", flush=True)
                        except Exception as e:
                            print(f"[{self.name}] Failed to read document text: {e}", flush=True)

            return MessageEvent(
                text=body,
                message_type=msg_type,
                source=source,
                raw_message=data,
                message_id=data.get("messageId"),
                media_urls=cached_urls,
                media_types=media_types,
            )
        except Exception as e:
            print(f"[{self.name}] Error building event: {e}")
            return None


# ──────────────────────────────────────────────────────────────────────────
# Plugin migration glue (#41112 / #3823)
#
# Added when the WhatsApp adapter moved from gateway/platforms/whatsapp.py into
# this bundled plugin. Mirrors the Discord (#24356) / Slack migrations: a
# register(ctx) entry point plus hook implementations that replace the
# per-platform core touchpoints (the Platform.WHATSAPP elif in gateway/run.py,
# the whatsapp_cfg YAML→env block + _PLATFORM_CONNECTED_CHECKERS entry in
# gateway/config.py, the _setup_whatsapp wizard + _PLATFORMS["whatsapp"] static
# dict in hermes_cli/gateway.py, and the _send_whatsapp dispatch in
# tools/send_message_tool.py).  WhatsApp auth is handled by the Node.js bridge,
# so is_connected is always True (matches the legacy checker).
# ──────────────────────────────────────────────────────────────────────────


async def _standalone_send(
    pconfig,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
):
    """Out-of-process WhatsApp delivery via the local bridge HTTP API.

    Implements the standalone_sender_fn contract so deliver=whatsapp cron jobs
    succeed when cron runs separately from the gateway. Replaces the legacy
    _send_whatsapp helper.
    """
    extra = getattr(pconfig, "extra", {}) or {}
    try:
        import aiohttp
    except ImportError:
        return {"error": "aiohttp not installed. Run: pip install aiohttp"}
    try:
        bridge_port = extra.get("bridge_port", 3000)
        normalized_chat_id = to_whatsapp_jid(chat_id)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://localhost:{bridge_port}/send",
                json={"chatId": normalized_chat_id, "message": message},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {
                        "success": True,
                        "platform": "whatsapp",
                        "chat_id": normalized_chat_id,
                        "message_id": data.get("messageId"),
                    }
                body = await resp.text()
                return {"error": f"WhatsApp bridge error ({resp.status}): {body}"}
    except Exception as e:
        return {"error": f"WhatsApp send failed: {e}"}


def interactive_setup() -> None:
    """Guide the user through WhatsApp setup.

    Replaces the central _setup_whatsapp in hermes_cli/gateway.py and the
    static _PLATFORMS["whatsapp"] dict. CLI helpers are lazy-imported so the
    plugin's module-load surface stays minimal.
    """
    from hermes_cli.config import get_env_value, save_env_value
    from hermes_cli.cli_output import (
        prompt,
        prompt_yes_no,
        print_header,
        print_info,
        print_success,
    )

    print_header("WhatsApp")
    print_info("WhatsApp uses a local Node.js bridge (WhatsApp Web client).")
    print_info("Start the bridge separately; the gateway connects to it over HTTP.")
    existing = get_env_value("WHATSAPP_ENABLED")
    if existing and existing.lower() in {"true", "1", "yes"}:
        print_info("WhatsApp: already enabled")
        if not prompt_yes_no("Reconfigure WhatsApp?", False):
            return

    if prompt_yes_no("Enable WhatsApp?", True):
        save_env_value("WHATSAPP_ENABLED", "true")
        print_success("WhatsApp enabled")
    else:
        save_env_value("WHATSAPP_ENABLED", "false")
        print_info("WhatsApp left disabled")
        return

    allowed_users = prompt(
        "Allowed user IDs (comma-separated, leave empty for no allowlist)"
    )
    if allowed_users:
        save_env_value("WHATSAPP_ALLOWED_USERS", allowed_users.replace(" ", ""))
        print_success("WhatsApp allowlist configured")

    home_channel = prompt("Home chat ID for cron delivery (leave empty to skip)")
    if home_channel:
        save_env_value("WHATSAPP_HOME_CHANNEL", home_channel.strip())


def _apply_yaml_config(yaml_cfg: dict, whatsapp_cfg: dict) -> dict | None:
    """Translate config.yaml whatsapp: keys into WHATSAPP_* env vars.

    Implements the apply_yaml_config_fn contract (#24849). Mirrors the legacy
    whatsapp_cfg block from gateway/config.py::load_gateway_config(). Env vars
    take precedence over YAML. Returns None — everything flows through env.
    """
    import json as _json
    if "require_mention" in whatsapp_cfg and not os.getenv("WHATSAPP_REQUIRE_MENTION"):
        os.environ["WHATSAPP_REQUIRE_MENTION"] = str(whatsapp_cfg["require_mention"]).lower()
    if "mention_patterns" in whatsapp_cfg and not os.getenv("WHATSAPP_MENTION_PATTERNS"):
        os.environ["WHATSAPP_MENTION_PATTERNS"] = _json.dumps(whatsapp_cfg["mention_patterns"])
    frc = whatsapp_cfg.get("free_response_chats")
    if frc is not None and not os.getenv("WHATSAPP_FREE_RESPONSE_CHATS"):
        if isinstance(frc, list):
            frc = ",".join(str(v) for v in frc)
        os.environ["WHATSAPP_FREE_RESPONSE_CHATS"] = str(frc)
    if "dm_policy" in whatsapp_cfg and not os.getenv("WHATSAPP_DM_POLICY"):
        os.environ["WHATSAPP_DM_POLICY"] = str(whatsapp_cfg["dm_policy"]).lower()
    af = whatsapp_cfg.get("allow_from")
    if af is not None and not os.getenv("WHATSAPP_ALLOWED_USERS"):
        if isinstance(af, list):
            af = ",".join(str(v) for v in af)
        os.environ["WHATSAPP_ALLOWED_USERS"] = str(af)
    if "group_policy" in whatsapp_cfg and not os.getenv("WHATSAPP_GROUP_POLICY"):
        os.environ["WHATSAPP_GROUP_POLICY"] = str(whatsapp_cfg["group_policy"]).lower()
    gaf = whatsapp_cfg.get("group_allow_from")
    if gaf is not None and not os.getenv("WHATSAPP_GROUP_ALLOWED_USERS"):
        if isinstance(gaf, list):
            gaf = ",".join(str(v) for v in gaf)
        os.environ["WHATSAPP_GROUP_ALLOWED_USERS"] = str(gaf)
    return None


def _is_connected(config) -> bool:
    """WhatsApp is considered connected when the user has explicitly enabled it
    via ``WHATSAPP_ENABLED`` (or the YAML-bridged equivalent on the config).

    Auth itself is handled by the external Node.js bridge — we can't verify the
    bridge token here — so the opt-in flag is the connection signal. The legacy
    built-in path keyed off ``WHATSAPP_ENABLED`` in both the connected-platforms
    check and the setup-status display; returning an unconditional True here
    would make WhatsApp always show as "configured" in ``hermes setup`` even
    when the user never enabled it. #41112.
    """
    extra = getattr(config, "extra", {}) or {}
    if config is not None and getattr(config, "enabled", False) and extra:
        # An explicitly-enabled PlatformConfig with seeded extras (e.g. from
        # YAML) counts as configured.
        return True
    # Read via hermes_cli.gateway.get_env_value (not os.getenv) so setup-status
    # callers that patch get_env_value — and the gateway connected-platforms
    # check — observe the same value. Matches the discord/slack plugin pattern.
    import hermes_cli.gateway as gateway_mod
    val = (gateway_mod.get_env_value("WHATSAPP_ENABLED") or "").strip().lower()
    return val in {"true", "1", "yes"}


def _build_adapter(config):
    """Factory wrapper that constructs WhatsAppAdapter from a PlatformConfig."""
    return WhatsAppAdapter(config)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="whatsapp",
        label="WhatsApp",
        adapter_factory=_build_adapter,
        check_fn=check_whatsapp_requirements,
        is_connected=_is_connected,
        required_env=["WHATSAPP_ENABLED"],
        install_hint="WhatsApp requires a Node.js bridge — see the WhatsApp messaging docs",
        setup_fn=interactive_setup,
        apply_yaml_config_fn=_apply_yaml_config,
        allowed_users_env="WHATSAPP_ALLOWED_USERS",
        allow_all_env="WHATSAPP_ALLOW_ALL_USERS",
        cron_deliver_env_var="WHATSAPP_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=4096,
        emoji="💬",
        allow_update_command=True,
    )
