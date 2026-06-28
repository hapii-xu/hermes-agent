"""
进程注册表 —— 用于托管后台进程的内存级注册表。

跟踪通过 terminal(background=true) 启动的进程，提供：
  - 输出缓冲（200KB 的滚动窗口）
  - 状态轮询与日志读取
  - 支持中断的阻塞式等待
  - 进程杀死
  - 基于 JSON checkpoint 文件的崩溃恢复
  - 面向会话的跟踪，用于网关重置保护

后台进程通过环境接口执行 —— 除非 TERMINAL_ENV=local，否则不会在
宿主机上运行任何内容。对于 Docker、Singularity、Modal、Daytona
和 SSH 后端，命令在沙箱内部运行。

用法：
    from tools.process_registry import process_registry

    # 启动一个后台进程（由 terminal_tool 调用）
    session = process_registry.spawn(env, "pytest -v", task_id="task_123")

    # 轮询状态
    result = process_registry.poll(session.id)

    # 阻塞直到完成
    result = process_registry.wait(session.id, timeout=300)

    # 杀死它
    process_registry.kill(session.id)
"""

import json
import logging
import os
import platform
import shlex
import signal
import subprocess
import threading
import time
import uuid

_IS_WINDOWS = platform.system() == "Windows"
from tools.environments.local import _find_shell, _resolve_safe_cwd, _sanitize_subprocess_env
from hermes_cli._subprocess_compat import windows_hide_flags
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from hermes_cli.config import get_hermes_home

logger = logging.getLogger(__name__)


# 用于崩溃恢复的 checkpoint 文件（仅网关使用）
CHECKPOINT_PATH = get_hermes_home() / "processes.json"

# 各类上限
MAX_OUTPUT_CHARS = 200_000      # 200KB 的滚动输出缓冲区
FINISHED_TTL_SECONDS = 1800     # 已完成进程保留 30 分钟
MAX_PROCESSES = 64              # 最多同时跟踪的进程数（LRU 淘汰）

# watch 模式限流 —— 按会话计算。
# 硬性规则：每 WATCH_MIN_INTERVAL_SECONDS 内最多发送一次 watch 命中通知。
# 在该冷却窗口内到达的任何匹配都会被丢弃，并计为一次违规（strike）。
# 连续 WATCH_STRIKE_LIMIT 个违规窗口后，该会话的 watch_patterns 将被永久
# 禁用，会话退回到 notify_on_complete 语义（进程真正退出时发一次通知）。
WATCH_MIN_INTERVAL_SECONDS = 15   # 连续两次 watch 匹配之间的最小间隔
WATCH_STRIKE_LIMIT = 3            # 连续违规次数 → 禁用 watch 并升级为 notify_on_complete

# 全局熔断器 —— 跨所有会话。作为第二道安全网，防止并发的兄弟进程即便各自
# 都在自己的上限之内，合起来仍会把用户淹没。
WATCH_GLOBAL_MAX_PER_WINDOW = 15
WATCH_GLOBAL_WINDOW_SECONDS = 10
WATCH_GLOBAL_COOLDOWN_SECONDS = 30


def format_uptime_short(seconds: int) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    mins, secs = divmod(s, 60)
    if mins < 60:
        return f"{mins}m {secs}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}m"


@dataclass
class ProcessSession:
    """带有输出缓冲的被跟踪后台进程。"""
    id: str                                     # 唯一会话 ID（"proc_xxxxxxxxxxxx"）
    command: str                                 # 原始命令字符串
    task_id: str = ""                           # 任务/沙箱隔离键
    session_key: str = ""                       # 网关会话键（用于重置保护）
    pid: Optional[int] = None                   # 操作系统进程 ID
    process: Optional[subprocess.Popen] = None  # Popen 句柄（仅本地）
    env_ref: Any = None                         # 环境对象的引用
    cwd: Optional[str] = None                   # 工作目录
    started_at: float = 0.0                     # 启动时的 time.time()（墙上时钟）
    host_start_time: Optional[int] = None       # 内核启动滴答数（/proc/<pid>/stat f22）—— 防 PID 复用
    exited: bool = False                        # 进程是否已结束
    exit_code: Optional[int] = None             # 退出码（仍在运行时为 None）
    completion_reason: str = "exited"           # exited|killed|lost|failed_start|already_exited
    termination_source: str = ""                # process.kill|kill_all|backend_lost|failed_start
    output_buffer: str = ""                     # 滚动输出（最后 MAX_OUTPUT_CHARS 字节）
    max_output_chars: int = MAX_OUTPUT_CHARS
    detached: bool = False                      # 若为 True 表示从崩溃中恢复（无管道）
    pid_scope: str = "host"                     # "host" 表示本地/PTY 的 PID，"sandbox" 表示环境内 PID
    # 监视器/通知元数据（持久化以便崩溃恢复）
    watcher_platform: str = ""
    watcher_chat_id: str = ""
    watcher_user_id: str = ""
    watcher_user_name: str = ""
    watcher_thread_id: str = ""
    watcher_message_id: str = ""                # 触发消息 id —— 用于话题路由的回复锚点
    watcher_interval: int = 0                   # 0 = 未配置监视器
    notify_on_complete: bool = False             # 退出时排队发送 agent 通知
    # watch 模式 —— 输出匹配任一模式时触发 agent 通知
    watch_patterns: List[str] = field(default_factory=list)
    _watch_hits: int = field(default=0, repr=False)          # 已投递的匹配总数
    _watch_suppressed: int = field(default=0, repr=False)    # 被限流丢弃的匹配数
    _watch_disabled: bool = field(default=False, repr=False) # 连续违规达到上限后被永久禁用
    # 每会话限流状态：每 WATCH_MIN_INTERVAL_SECONDS 最多一次匹配。
    # 发生一次投递时，_watch_cooldown_until 被设为 now + interval，并且
    # _watch_strike_candidate 变为 True。在该截止时间之前到达的下一次匹配
    # 计为一次违规（无论期间丢弃了多少次匹配 —— 违规针对的是窗口而非单次
    # 匹配）。连续 WATCH_STRIKE_LIMIT 次违规后，watch_patterns 被禁用，
    # 会话升级为 notify_on_complete。
    _watch_last_emit_at: float = field(default=0.0, repr=False)
    _watch_cooldown_until: float = field(default=0.0, repr=False)
    _watch_strike_candidate: bool = field(default=False, repr=False)
    _watch_consecutive_strikes: int = field(default=0, repr=False)
    _completion_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _reader_thread: Optional[threading.Thread] = field(default=None, repr=False)
    _pty: Any = field(default=None, repr=False)  # ptyprocess 句柄（当 use_pty=True 时）


class ProcessRegistry:
    """
    运行中与已完成后台进程的内存级注册表。

    线程安全。访问方包括：
      - 执行线程（terminal_tool、process 工具处理器）
      - 网关 asyncio 事件循环（监视器任务、会话重置检查）
      - 清理线程（沙箱回收协调）
    """

    _SHELL_NOISE_SUBSTRINGS = (
        "bash: cannot set terminal process group",
        "bash: no job control in this shell",
        "no job control in this shell",
        "cannot set terminal process group",
        "tcsetattr: Inappropriate ioctl for device",
    )

    def __init__(self):
        self._running: Dict[str, ProcessSession] = {}
        self._finished: Dict[str, ProcessSession] = {}
        self._lock = threading.Lock()

        # check_interval 监视器的旁路通道（网关在 agent 运行后读取）
        self.pending_watchers: List[Dict[str, Any]] = []

        # 通知队列 —— 所有后台进程事件的统一队列。
        # 完成通知（notify_on_complete）和 watch 模式匹配都进入这里，
        # 通过 "type" 字段区分。CLI 的 process_loop 与网关会在每个 agent
        # 回合后排出该队列，以自动触发新回合。
        import queue as _queue_mod
        self.completion_queue: _queue_mod.Queue = _queue_mod.Queue()

        # 跟踪那些 agent 已通过 wait/log 消费了完成事件的会话。
        # 排出循环以及网关/tui 监视器都会跳过这些会话的通知 —— 阻塞式
        # wait() 或完整 read_log() 意味着 agent 本回合已拿到输出并据此
        # 行动。
        self._completion_consumed: set = set()

        # 跟踪 agent 仅通过 poll() *观察* 到已退出的会话。poll() 是只读
        # 状态检查，因此不会标记 _completion_consumed（那样会让一次状态
        # 检查抑制网关/tui 监视器的自主投递回合 —— #10156）。但在 CLI 上
        # poll 结果在同一回合内联返回，因此空闲/回合后排出仍需跳过已排队
        # 的完成事件，以避免重复注入 [SYSTEM: ...]（这正是 bug #8228 当初
        # 修复的问题）。drain_notifications() 会查询该集合；网关/tui 监视器
        # 则刻意不查询。
        self._poll_observed: set = set()

        # 全局 watch 匹配熔断器 —— 跨所有会话。
        # 防止兄弟进程即便各自都低于自身每会话上限，合起来仍把用户淹没。
        self._global_watch_lock = threading.Lock()
        self._global_watch_window_start: float = 0.0
        self._global_watch_window_hits: int = 0
        self._global_watch_tripped_until: float = 0.0
        self._global_watch_suppressed_during_trip: int = 0

    @staticmethod
    def _clean_shell_noise(text: str) -> str:
        """去掉输出开头的 shell 启动告警信息。"""
        lines = text.split("\n")
        while lines and any(noise in lines[0] for noise in ProcessRegistry._SHELL_NOISE_SUBSTRINGS):
            lines.pop(0)
        return "\n".join(lines)

    def _check_watch_patterns(self, session: ProcessSession, new_text: str) -> None:
        """扫描新输出以查找 watch 模式并入队通知。

        由读取线程调用，new_text 为刚读取到的数据块。

        每会话限流：每个 WATCH_MIN_INTERVAL_SECONDS 内最多一次 watch 命中
        通知。在冷却窗口内到达的任何匹配都会被丢弃，并计为该窗口的一次
        违规。连续 WATCH_STRIKE_LIMIT 个违规窗口后，本会话的 watch_patterns
        将被禁用，会话升级为 notify_on_complete 语义 —— 进程真正退出时仅
        发一次通知，不再有进程运行中途的刷屏。
        """
        if not session.watch_patterns or session._watch_disabled:
            return
        # 退出后抑制：一旦读取循环已声明进程退出，我们仍看到的任何迟到
        # 数据块都是退出后的噪声。丢弃它们可以避免在 completion_queue 的
        # 消费方异步运行时出现“进程结束几分钟后才投递的过期通知”刷屏。
        if session.exited:
            return

        # 逐行扫描新文本，查找模式匹配
        matched_lines = []
        matched_pattern = None
        for line in new_text.splitlines():
            for pat in session.watch_patterns:
                if pat in line:
                    matched_lines.append(line.rstrip())
                    if matched_pattern is None:
                        matched_pattern = pat
                    break  # 每行匹配一次即可

        if not matched_lines:
            return

        now = time.time()
        should_disable = False
        with session._lock:
            # 情况 1：仍处于上一次投递后的冷却期内。
            # 将其计为当前窗口的一次违规（每个窗口仅计一次）并丢弃事件。
            # 若已达到违规上限，则禁用 watch 并升级为 notify_on_complete。
            if session._watch_cooldown_until and now < session._watch_cooldown_until:
                session._watch_suppressed += len(matched_lines)
                if not session._watch_strike_candidate:
                    # 本窗口内首次丢弃 —— 计一次违规。
                    session._watch_strike_candidate = True
                    session._watch_consecutive_strikes += 1
                    if session._watch_consecutive_strikes >= WATCH_STRIKE_LIMIT:
                        session._watch_disabled = True
                        # 升级为 notify_on_complete，保证进程真正结束时 agent
                        # 仍能恰好收到一次通知。
                        session.notify_on_complete = True
                        should_disable = True
                return_early = True
            else:
                # 情况 2：冷却期已过。
                # 判断本窗口是“干净”的（无丢弃）还是违规窗口。如果上一个
                # 冷却期内未设置违规候选，则重置连续违规计数 —— 我们回到了
                # 健康的投递节奏。
                if (
                    session._watch_cooldown_until
                    and not session._watch_strike_candidate
                ):
                    session._watch_consecutive_strikes = 0
                session._watch_strike_candidate = False

                # 投递通知并开启新的冷却窗口。
                session._watch_last_emit_at = now
                session._watch_cooldown_until = now + WATCH_MIN_INTERVAL_SECONDS
                session._watch_hits += 1
                suppressed = session._watch_suppressed
                session._watch_suppressed = 0
                return_early = False

        if return_early:
            if should_disable:
                # 恰好投递一次“watch 已禁用，回退到 notify_on_complete”的汇总
                # 事件，让 agent/用户明白为什么突然安静了。
                self.completion_queue.put({
                    "session_id": session.id,
                    "session_key": session.session_key,
                    "command": session.command,
                    "type": "watch_disabled",
                    "suppressed": session._watch_suppressed,
                    "platform": session.watcher_platform,
                    "chat_id": session.watcher_chat_id,
                    "user_id": session.watcher_user_id,
                    "user_name": session.watcher_user_name,
                    "thread_id": session.watcher_thread_id,
                    "message_id": session.watcher_message_id,
                    "message": (
                        f"Watch patterns disabled for process {session.id} — "
                        f"{WATCH_STRIKE_LIMIT} consecutive rate-limit windows triggered "
                        f"(min spacing {WATCH_MIN_INTERVAL_SECONDS}s). "
                        f"Falling back to notify_on_complete semantics; you'll get "
                        f"exactly one notification when the process exits."
                    ),
                })
            return

        # 将匹配到的输出裁剪到合理大小
        output = "\n".join(matched_lines[:20])
        if len(output) > 2000:
            output = output[:2000] + "\n...(truncated)"

        # 全局熔断器 —— 跨所有会话（第二道安全网）。
        if not self._global_watch_admit(now):
            return

        self.completion_queue.put({
            "session_id": session.id,
            "session_key": session.session_key,
            "command": session.command,
            "type": "watch_match",
            "pattern": matched_pattern,
            "output": output,
            "suppressed": suppressed,
            "platform": session.watcher_platform,
            "chat_id": session.watcher_chat_id,
            "user_id": session.watcher_user_id,
            "user_name": session.watcher_user_name,
            "thread_id": session.watcher_thread_id,
            "message_id": session.watcher_message_id,
        })

    def _global_watch_admit(self, now: float) -> bool:
        """若本次 watch_match 事件被全局熔断器放行则返回 True。

        语义：
        - 若当前处于冷却期，则丢弃事件并计数。
        - 否则滑动滚动窗口并检查全局上限。
        - 若超出上限，则触发熔断 WATCH_GLOBAL_COOLDOWN_SECONDS 秒，
          并投递“一个”汇总事件，让 agent/用户看到“已抑制 N 条通知”，
          而不是逐条收到。
        - 冷却期结束时，投递一个解除汇总并重置计数器。
        """
        with self._global_watch_lock:
            # 先处理冷却期到期，以便投递解除汇总。
            if self._global_watch_tripped_until and now >= self._global_watch_tripped_until:
                suppressed = self._global_watch_suppressed_during_trip
                self._global_watch_tripped_until = 0.0
                self._global_watch_suppressed_during_trip = 0
                self._global_watch_window_start = now
                self._global_watch_window_hits = 0
                if suppressed > 0:
                    # 在锁外排队一个汇总事件（见下方）。
                    release_msg = {
                        "session_id": "",
                        "session_key": "",
                        "command": "",
                        "type": "watch_overflow_released",
                        "suppressed": suppressed,
                        "message": (
                            f"Watch-pattern notifications resumed. "
                            f"{suppressed} match event(s) were suppressed during the flood."
                        ),
                        "platform": "",
                        "chat_id": "",
                        "user_id": "",
                        "user_name": "",
                        "thread_id": "",
                    }
                else:
                    release_msg = None
            else:
                release_msg = None

            # 仍在冷却期内 —— 丢弃并计数。
            if self._global_watch_tripped_until and now < self._global_watch_tripped_until:
                self._global_watch_suppressed_during_trip += 1
                admit = False
                trip_now = None
            else:
                # 滑动窗口。
                if now - self._global_watch_window_start >= WATCH_GLOBAL_WINDOW_SECONDS:
                    self._global_watch_window_start = now
                    self._global_watch_window_hits = 0

                if self._global_watch_window_hits >= WATCH_GLOBAL_MAX_PER_WINDOW:
                    # 触发熔断。
                    self._global_watch_tripped_until = now + WATCH_GLOBAL_COOLDOWN_SECONDS
                    self._global_watch_suppressed_during_trip += 1
                    trip_now = now
                    admit = False
                else:
                    self._global_watch_window_hits += 1
                    trip_now = None
                    admit = True

        # 在锁外排队汇总事件。
        if release_msg is not None:
            self.completion_queue.put(release_msg)
        if trip_now is not None:
            self.completion_queue.put({
                "session_id": "",
                "session_key": "",
                "command": "",
                "type": "watch_overflow_tripped",
                "message": (
                    f"Watch-pattern overflow: >{WATCH_GLOBAL_MAX_PER_WINDOW} "
                    f"notifications in {WATCH_GLOBAL_WINDOW_SECONDS}s across all processes. "
                    f"Suppressing further watch_match events for "
                    f"{WATCH_GLOBAL_COOLDOWN_SECONDS}s."
                ),
                "platform": "",
                "chat_id": "",
                "user_id": "",
                "user_name": "",
                "thread_id": "",
            })
        return admit

    @staticmethod
    def _is_host_pid_alive(pid: Optional[int]) -> bool:
        """对宿主机可见 PID 做尽力而为的存活检查。"""
        if not pid:
            return False
        # ``os.kill(pid, 0)`` 在 Windows 上并非 no-op（bpo-14484）—— 使用
        # 跨平台的存在性检查。
        from gateway.status import _pid_exists
        return _pid_exists(pid)

    @staticmethod
    def _safe_host_start_time(pid: Optional[int]) -> Optional[int]:
        """返回宿主机 PID 的内核启动滴答数，不可用时返回 None。"""
        if not pid:
            return None
        try:
            from gateway.status import get_process_start_time
            return get_process_start_time(pid)
        except Exception:
            return None

    @classmethod
    def _host_pid_is_ours(cls, pid: Optional[int], expected_start: Optional[int]) -> bool:
        """仅当 ``pid`` 存活且仍是我们启动的同一进程时才返回 True。

        内核在一个进程退出并被回收后会复用其 PID/PGID 编号，因此存储下来的
        PID 之后可能指向一个*不相关*的进程 —— 实际案例中被复用的编号落到了
        某个桌面浏览器的会话 leader 上，随后被我们的 tree-kill 发了 SIGTERM
        （Firefox 以不规则间隔被杀掉）。我们将启动时捕获的内核启动时间与
        当前值进行比较；不匹配意味着编号已被复用，绝不能对其发送信号。

        当没有捕获基线时（旧的 checkpoint 文件，或没有 ``/proc`` 的平台），
        我们退化为仅做存活检查而不是拒绝动作，以保留先前的尽力而为行为。
        """
        if not cls._is_host_pid_alive(pid):
            return False
        if expected_start is None:
            return True
        return cls._safe_host_start_time(pid) == expected_start

    def _refresh_detached_session(self, session: Optional[ProcessSession]) -> Optional[ProcessSession]:
        """当底层进程已退出时，更新通过宿主 PID 恢复的会话。"""
        if session is None or session.exited or not session.detached or session.pid_scope != "host":
            return session

        # 身份感知的存活检查：被复用的 PID（存活但已不是我们启动的那个进程）
        # 必须被视为“我们的进程已退出”，这样它会被移到已完成集合，且后续
        # 的 kill() 永远不会对它做 tree-kill。
        if self._host_pid_is_ours(session.pid, session.host_start_time):
            return session

        with session._lock:
            if session.exited:
                return session
            session.exited = True
            # 恢复的会话不再有可等待的句柄，因此一旦原始进程对象消失，
            # 真正的退出码就不可得了。
            session.exit_code = None

        self._move_to_finished(session)
        return session

    @staticmethod
    def _proc_alive(proc) -> bool:
        """若 psutil.Process 仍在运行且不是僵尸进程则返回 True。

        僵尸进程已经死了（只是尚未被回收），因此无需再 SIGKILL。
        """
        try:
            import psutil
            if not proc.is_running():
                return False
            return proc.status() != psutil.STATUS_ZOMBIE
        except Exception:
            return False

    @staticmethod
    def _daemon_term_grace_seconds() -> float:
        """SIGTERM 与升级为 SIGKILL 之间的宽限窗口（秒）。

        从 config.yaml 的 ``terminal.daemon_term_grace_seconds`` 读取；下限
        为 0（0 表示禁用升级）。当配置不可读时回退到 DEFAULT_CONFIG 的值，
        因此调用方总能拿到一个合理数值。
        """
        try:
            from hermes_cli.config import read_raw_config, cfg_get, DEFAULT_CONFIG
            cfg = read_raw_config()
            val = cfg_get(cfg, "terminal", "daemon_term_grace_seconds")
            if val is None:
                val = DEFAULT_CONFIG["terminal"]["daemon_term_grace_seconds"]
            return max(float(val), 0.0)
        except Exception:
            return 2.0

    @classmethod
    def _terminate_host_pid(cls, pid: int, expected_start: Optional[int] = None) -> None:
        """终止一个宿主机可见的 PID 及其子孙进程。

        ``expected_start`` 是我们启动进程时捕获的内核启动时间。提供该值时，
        在发送任何信号之前会先与当前 PID 重新校验；不匹配（或 PID 已死）意味
        着该编号已被复用到不相关的进程上，我们拒绝触碰它，因此一个过期的后台
        会话 PID 永远不会 tree-kill 一个浏览器或其他陌生进程。

        POSIX：用 ``psutil`` 遍历进程树，并在父进程之前先对子进程发送
        SIGTERM，这样子进程树（例如 ``agent-browser`` 守护进程派生的
        Chromium 渲染进程/GPU 辅助进程）不会重新挂到 init 下而躲过清理。
        在有限的宽限窗口（``terminal.daemon_term_grace_seconds``）之后，任何
        忽略 SIGTERM 的树成员（卡在信号处理函数里的守护进程）都会被升级为
        SIGKILL，避免无限泄漏。将宽限值设为 0 可禁用升级（仅 SIGTERM）。

        Windows：通过 shell 调用 ``taskkill /PID <pid> /T /F``。这是
        Microsoft 官方文档化的 tree-kill 原语，与 ``gateway.status.terminate_pid``
        中的既有约定一致。``/F`` 本身就是硬杀，因此不需要单独的升级步骤。
        我们无法在 Windows 上复用 POSIX 的 psutil 路径，原因如下：

          1. Windows 不维护 Unix 风格的进程树 ——
             ``psutil.Process.children(recursive=True)`` 走的是 PPID 链接，
             当中间进程退出时这些链接就会过期，因此枚举只是尽力而为，
             会漏掉被孤立的子孙进程。
          2. ``psutil.Process.terminate()`` 在 Windows 上等价于
             ``TerminateProcess()``，只杀目标句柄且是硬杀 —— Windows 没有
             能在进程组中级联的 SIGTERM 等价物。（参见
             ``gateway/status.py::terminate_pid`` 中的警告：在 Windows 上
             “带 SIGTERM 的 os.kill 并不等价于 tree-kill 式的硬杀”。）
             无头 Chromium 没有 GUI 窗口，因此不带 ``/F`` 的较温和的
             ``taskkill /T`` 也触及不到它。

        ``psutil`` 是硬依赖（见 ``pyproject.toml``）；裸 ``os.kill`` 回退
        路径用于处理 POSIX 上的 OSError / PermissionError，以及 Windows 上
        缺失 ``taskkill.exe`` 的情况（在真实 Windows 安装上基本不可达，
        但作为廉价保险保留）。
        """
        if expected_start is not None and not cls._host_pid_is_ours(pid, expected_start):
            # PID 已被复用（启动时间变化）或已消失 —— 绝不对陌生进程发送信号。
            # 泄漏一个孤儿进程总好过杀掉例如某个浏览器——它的会话 leader 恰好
            # 复用了这个已死会话的 PID。
            logger.warning(
                "Refusing to terminate host pid %d: start-time mismatch — "
                "PID was recycled onto an unrelated process.", pid,
            )
            return
        if _IS_WINDOWS:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    creationflags=windows_hide_flags(),
                    stdin=subprocess.DEVNULL,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except (OSError, ProcessLookupError, PermissionError):
                    pass
            return

        import psutil
        try:
            parent = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        except (OSError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError, PermissionError):
                pass
            return

        # 对整棵树打快照（子进程在父进程之前）并对每个进程发送 SIGTERM。
        try:
            targets = parent.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            targets = []
        targets.append(parent)

        for proc in targets:
            try:
                proc.terminate()
            except psutil.NoSuchProcess:
                pass
            except (psutil.AccessDenied, OSError):
                pass

        # 在宽限窗口内对任何忽略 SIGTERM 的进程升级为 SIGKILL —— 否则卡在
        # 信号处理函数里的守护进程会无限泄漏。
        grace = cls._daemon_term_grace_seconds()
        if grace <= 0:
            return
        # 睡满宽限窗口后，独立地重新探测每个目标并对幸存者发送 SIGKILL。
        # 我们刻意不信任 ``psutil.wait_procs`` 的 gone/alive 划分：它通过
        # ``Process.wait()`` 回收，当目标过渡到僵尸状态，或在父/子进程树
        # 间回收存在竞争时，可能错误划分，导致幸存者未被杀掉。直接重新
        # 探测存活状态是确定性的。
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not any(cls._proc_alive(_p) for _p in targets):
                break
            time.sleep(0.05)
        for proc in targets:
            try:
                if not cls._proc_alive(proc):
                    continue
                proc.kill()  # SIGKILL on POSIX
                logger.info(
                    "Escalated to SIGKILL for pid %d (ignored SIGTERM within "
                    "%.1fs grace)", proc.pid, grace,
                )
            except psutil.NoSuchProcess:
                pass
            except (psutil.AccessDenied, OSError):
                pass

    # ----- 启动（Spawn） -----

    @staticmethod
    def _env_temp_dir(env: Any) -> str:
        """返回由环境承载的后台任务所用的可写沙箱临时目录。"""
        get_temp_dir = getattr(env, "get_temp_dir", None)
        if callable(get_temp_dir):
            try:
                temp_dir = get_temp_dir()
                if isinstance(temp_dir, str) and temp_dir.startswith("/"):
                    return temp_dir.rstrip("/") or "/"
            except Exception as exc:
                logger.debug("Could not resolve environment temp dir: %s", exc)
        return "/tmp"

    def spawn_local(
        self,
        command: str,
        cwd: str = None,
        task_id: str = "",
        session_key: str = "",
        env_vars: dict = None,
        use_pty: bool = False,
    ) -> ProcessSession:
        """
        在本地启动一个后台进程。

        仅用于 TERMINAL_ENV=local。其他后端使用 spawn_via_env()。

        参数：
            use_pty: 若为 True，则通过 ptyprocess 使用伪终端，适用于交互式
                     CLI 工具（Codex、Claude Code、Python REPL）。若未安装
                     ptyprocess 则回退到 subprocess.Popen。
        """
        session = ProcessSession(
            id=f"proc_{uuid.uuid4().hex[:12]}",
            command=command,
            task_id=task_id,
            session_key=session_key,
            cwd=_resolve_safe_cwd(cwd or os.getcwd()),
            started_at=time.time(),
        )

        if use_pty:
            # 为交互式 CLI 工具尝试 PTY 模式
            try:
                if _IS_WINDOWS:
                    from winpty import PtyProcess as _PtyProcessCls
                else:
                    from ptyprocess import PtyProcess as _PtyProcessCls
                user_shell = _find_shell()
                pty_env = _sanitize_subprocess_env(os.environ, env_vars)
                pty_env["PYTHONUNBUFFERED"] = "1"
                pty_proc = _PtyProcessCls.spawn(
                    [user_shell, "-lic", f"set +m; {command}"],
                    cwd=session.cwd,
                    env=pty_env,
                    dimensions=(30, 120),
                )
                session.pid = pty_proc.pid
                session.host_start_time = self._safe_host_start_time(session.pid)
                # 将 pty 句柄存到会话上，以便读写
                session._pty = pty_proc

                # PTY 读取线程
                reader = threading.Thread(
                    target=self._pty_reader_loop,
                    args=(session,),
                    daemon=True,
                    name=f"proc-pty-reader-{session.id}",
                )
                session._reader_thread = reader
                reader.start()

                with self._lock:
                    self._prune_if_needed()
                    self._running[session.id] = session

                self._write_checkpoint()
                return session

            except ImportError:
                logger.warning("ptyprocess not installed, falling back to pipe mode")
            except Exception as e:
                logger.warning("PTY spawn failed (%s), falling back to pipe mode", e)

        # 标准 Popen 路径（非 PTY 或 PTY 回退）
        # 使用用户的登录 shell 以与 LocalEnvironment 保持一致 —— 保证 rc
        # 文件被加载、用户工具可用。
        user_shell = _find_shell()
        # 强制 Python 脚本输出不缓冲，以便后台执行时进度可见（tqdm/datasets
        # 之类的库在 stdout 是管道时会缓冲，导致 process(action="poll") 看不到
        # 输出）。
        bg_env = _sanitize_subprocess_env(os.environ, env_vars)
        bg_env["PYTHONUNBUFFERED"] = "1"
        _popen_kwargs = {"creationflags": windows_hide_flags()} if _IS_WINDOWS else {}

        proc = subprocess.Popen(
            [user_shell, "-lic", f"set +m; {command}"],
            text=True,
            cwd=session.cwd,
            env=bg_env,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            preexec_fn=None if _IS_WINDOWS else os.setsid,
            **_popen_kwargs,
        )

        session.process = proc
        session.pid = proc.pid
        session.host_start_time = self._safe_host_start_time(session.pid)

        try:
            # 启动输出读取线程
            reader = threading.Thread(
                target=self._reader_loop,
                args=(session,),
                daemon=True,
                name=f"proc-reader-{session.id}",
            )
            session._reader_thread = reader
            reader.start()

            with self._lock:
                self._prune_if_needed()
                self._running[session.id] = session

            self._write_checkpoint()
        except Exception:
            # Popen 之后的初始化失败 —— 在重新抛出之前杀死这个孤儿子进程
            # （以及通过 setsid 派生的任何子孙进程），避免它们作为未被跟踪
            # 的后台进程泄漏。
            try:
                if not _IS_WINDOWS:
                    try:
                        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
                        os.killpg(os.getpgid(proc.pid), kill_signal)  # windows-footgun: ok - 上面已由 _IS_WINDOWS 守护
                    except (ProcessLookupError, PermissionError, OSError):
                        proc.kill()
                else:
                    proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            raise

        return session

    def spawn_via_env(
        self,
        env: Any,
        command: str,
        cwd: str = None,
        task_id: str = "",
        session_key: str = "",
        timeout: int = 10,
    ) -> ProcessSession:
        """
        通过非本地环境后端启动一个后台进程。

        对于 Docker/Singularity/Modal/Daytona/SSH：使用环境的 execute()
        接口在沙箱内部运行命令。我们包装该命令以捕获沙箱内的 PID，并把
        输出重定向到沙箱内的一个日志文件，然后通过后续的 execute() 调用
        轮询该日志。

        该方式不如本地启动功能强大（没有实时 stdout 管道、没有 stdin），
        但能保证命令在正确的沙箱上下文中运行。
        """
        session = ProcessSession(
            id=f"proc_{uuid.uuid4().hex[:12]}",
            command=command,
            task_id=task_id,
            session_key=session_key,
            cwd=cwd,
            started_at=time.time(),
            env_ref=env,
            pid_scope="sandbox",
        )

        # 在沙箱中运行命令并捕获输出
        temp_dir = self._env_temp_dir(env)
        log_path = f"{temp_dir}/hermes_bg_{session.id}.log"
        pid_path = f"{temp_dir}/hermes_bg_{session.id}.pid"
        exit_path = f"{temp_dir}/hermes_bg_{session.id}.exit"
        quoted_command = shlex.quote(command)
        quoted_temp_dir = shlex.quote(temp_dir)
        quoted_log_path = shlex.quote(log_path)
        quoted_pid_path = shlex.quote(pid_path)
        quoted_exit_path = shlex.quote(exit_path)
        bg_command = (
            f"mkdir -p {quoted_temp_dir} && "
            f"( nohup bash -lc {quoted_command} > {quoted_log_path} 2>&1; "
            f"rc=$?; printf '%s\\n' \"$rc\" > {quoted_exit_path} ) & "
            f"echo $! > {quoted_pid_path} && cat {quoted_pid_path}"
        )

        try:
            result = env.execute(
                bg_command,
                timeout=timeout,
                rewrite_compound_background=False,
            )
            output = result.get("output", "").strip()
            # 尝试从输出中提取 PID
            for line in output.splitlines():
                line = line.strip()
                if line.isdigit():
                    session.pid = int(line)
                    break
            # 如果包装脚本无法给出 PID（例如语法错误或重定向损坏），则视为
            # 启动失败，而不是暴露一个假的“运行中”会话。
            if session.pid is None:
                session.exited = True
                session.exit_code = int(result.get("returncode", -1))
                if session.exit_code == 0:
                    session.exit_code = -1
                session.completion_reason = "failed_start"
                session.termination_source = "failed_start"
                session.output_buffer = result.get("output", "").strip()
        except Exception as e:
            session.exited = True
            session.exit_code = -1
            session.completion_reason = "failed_start"
            session.termination_source = "failed_start"
            session.output_buffer = f"Failed to start: {e}"

        if not session.exited:
            # 启动一个轮询线程，定期读取日志文件
            reader = threading.Thread(
                target=self._env_poller_loop,
                args=(session, env, log_path, pid_path, exit_path),
                daemon=True,
                name=f"proc-poller-{session.id}",
            )
            session._reader_thread = reader
            reader.start()

        with self._lock:
            self._prune_if_needed()
            if not session.exited:
                self._running[session.id] = session

        if not session.exited:
            self._write_checkpoint()

        return session

    # ----- 读取 / 轮询线程 -----

    def _reader_loop(self, session: ProcessSession):
        """后台线程：读取本地 Popen 进程的 stdout。"""
        first_chunk = True
        try:
            while True:
                chunk = session.process.stdout.read(4096)
                if not chunk:
                    break
                if first_chunk:
                    chunk = self._clean_shell_noise(chunk)
                    first_chunk = False
                with session._lock:
                    session.output_buffer += chunk
                    if len(session.output_buffer) > session.max_output_chars:
                        session.output_buffer = session.output_buffer[-session.max_output_chars:]
                self._check_watch_patterns(session, chunk)
        except Exception as e:
            logger.debug("Process stdout reader ended: %s", e)
        finally:
            # 总是回收子进程，避免产生僵尸进程。
            try:
                session.process.wait(timeout=5)
            except Exception as e:
                logger.debug("Process wait timed out or failed: %s", e)
            session.exited = True
            if session.completion_reason != "killed":
                session.exit_code = session.process.returncode
                session.completion_reason = "exited"
            self._move_to_finished(session)

    def _env_poller_loop(
        self, session: ProcessSession, env: Any, log_path: str, pid_path: str, exit_path: str
    ):
        """后台线程：为非本地后端轮询沙箱日志文件。"""
        quoted_log_path = shlex.quote(log_path)
        quoted_pid_path = shlex.quote(pid_path)
        quoted_exit_path = shlex.quote(exit_path)
        prev_output_len = 0  # 记录上次的输出长度，用于 watch 模式扫描增量
        while not session.exited:
            time.sleep(2)  # 每 2 秒轮询一次
            try:
                # 从日志文件读取新输出
                result = env.execute(f"cat {quoted_log_path} 2>/dev/null", timeout=10)
                new_output = result.get("output", "")
                if new_output:
                    # 计算 watch 模式扫描所需的增量
                    delta = new_output[prev_output_len:] if len(new_output) > prev_output_len else ""
                    prev_output_len = len(new_output)
                    with session._lock:
                        session.output_buffer = new_output
                        if len(session.output_buffer) > session.max_output_chars:
                            session.output_buffer = session.output_buffer[-session.max_output_chars:]
                    if delta:
                        self._check_watch_patterns(session, delta)

                # 检查进程是否仍在运行
                check = env.execute(
                    f"kill -0 \"$(cat {quoted_pid_path} 2>/dev/null)\" 2>/dev/null; echo $?",
                    timeout=5,
                )
                check_output = check.get("output", "").strip()
                if check_output and check_output.splitlines()[-1].strip() != "0":
                    # 进程已退出 —— 读取由包装 shell 捕获的退出码。
                    exit_result = env.execute(
                        f"cat {quoted_exit_path} 2>/dev/null",
                        timeout=5,
                    )
                    exit_str = exit_result.get("output", "").strip()
                    try:
                        session.exit_code = int(exit_str.splitlines()[-1].strip())
                    except (ValueError, IndexError):
                        session.exit_code = -1
                    session.exited = True
                    if session.completion_reason != "killed":
                        session.completion_reason = "exited"
                    self._move_to_finished(session)
                    return

            except Exception:
                # 环境可能已消失（沙箱被回收等）
                session.exited = True
                session.exit_code = -1
                session.completion_reason = "lost"
                session.termination_source = "backend_lost"
                self._move_to_finished(session)
                return

    def _pty_reader_loop(self, session: ProcessSession):
        """后台线程：读取 PTY 进程的输出。"""
        pty = session._pty
        try:
            while pty.isalive():
                try:
                    chunk = pty.read(4096)
                    if chunk:
                        # ptyprocess 返回的是 bytes
                        text = chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="replace")
                        with session._lock:
                            session.output_buffer += text
                            if len(session.output_buffer) > session.max_output_chars:
                                session.output_buffer = session.output_buffer[-session.max_output_chars:]
                        self._check_watch_patterns(session, text)
                except EOFError:
                    break
                except Exception:
                    break
        except Exception as e:
            logger.debug("PTY stdout reader ended: %s", e)

        # 进程已退出
        try:
            pty.wait()
        except Exception as e:
            logger.debug("PTY wait timed out or failed: %s", e)
        session.exited = True
        if session.completion_reason != "killed":
            session.exit_code = pty.exitstatus if hasattr(pty, 'exitstatus') else -1
            session.completion_reason = "exited"
        self._move_to_finished(session)

    def _move_to_finished(self, session: ProcessSession):
        """把一个会话从运行中移到已完成。

        幂等：若会话已被移动过（例如 kill_process 与读取线程发生竞争），
        则第二次调用是 no-op —— 不会重复入队完成通知。
        """
        with self._lock:
            was_running = self._running.pop(session.id, None) is not None
            self._finished[session.id] = session
        session._completion_event.set()
        self._write_checkpoint()

        # 仅在第一次移动时入队完成通知。若没有这个守卫，kill_process()
        # 和读取线程可能都调用 _move_to_finished()，从而产生重复的
        # [IMPORTANT: ...] 消息。
        if was_running and session.notify_on_complete:
            from tools.ansi_strip import strip_ansi
            output_tail = strip_ansi(session.output_buffer[-2000:]) if session.output_buffer else ""
            self.completion_queue.put({
                "type": "completion",
                "session_id": session.id,
                "session_key": session.session_key,
                "command": session.command,
                "exit_code": session.exit_code,
                "completion_reason": session.completion_reason,
                "termination_source": session.termination_source,
                "output": output_tail,
            })

    # ----- 查询方法 -----

    def is_completion_consumed(self, session_id: str) -> bool:
        """检查完成通知是否已通过 wait/log 被消费。"""
        return session_id in self._completion_consumed

    def is_session_waiting(self, session_id: str) -> bool:
        """判断挂起在该会话上的目标循环是否仍应保持挂起。

        供目标循环等待屏障（``hermes_cli.goals``）使用，以支持等待进程自身
        的触发，而不仅仅是等待其退出。一个会话“仍在等待”的条件是：
          - 它仍在运行，并且
          - 若配置了 ``watch_patterns``，则尚无任何模式匹配过（这样一个
            运行中途触发、且可能永不退出的长期监视器，会在其模式命中时
            立即解除阻塞，而不是等到退出）。

        当会话已退出、其 watch 模式已触发、或会话未知时返回 False（不等待）
        —— 这样过期或已触发的屏障永远不会卡住循环。
        """
        if not session_id:
            return False
        with self._lock:
            session = self._running.get(session_id) or self._finished.get(session_id)
        if session is None:
            return False
        # 刷新分离/远程状态，使 .exited 保持最新。
        try:
            self._refresh_detached_session(session)
        except Exception:
            pass
        if session.exited:
            return False
        # watch 模式进程：触发条件是模式匹配，而不是退出。
        # 一旦投递过任何匹配，即便进程仍在运行（服务器/守护进程/监视器
        # 场景），等待也算满足。
        if session.watch_patterns and not session._watch_disabled:
            if session._watch_hits > 0:
                return False
        return True

    def _drain_should_skip(self, session_id: str) -> bool:
        """判断 CLI 排出是否应跳过某会话的完成事件。

        跳过的情形：agent 已真正消费了输出（wait/log →
        ``_completion_consumed``），或通过 poll() 内联观察到退出
        （``_poll_observed``）。两种情况下 CLI agent 本回合都已拿到结果，
        因此再注入一条 [SYSTEM: ...] 完成事件会是重复（#8228）。
        网关/tui 监视器不使用本方法 —— 它们只检查
        ``is_completion_consumed``，以保证只读的 poll 永远不会抑制其自主
        投递回合（#10156）。
        """
        return session_id in self._completion_consumed or session_id in self._poll_observed

    def drain_notifications(self) -> "list[tuple[dict, str]]":
        """弹出所有待处理通知事件并返回格式化后的配对。

        返回 (raw_event, formatted_text) 元组列表。
        跳过 agent 已通过 wait/log 消费、或通过 poll() 内联观察到的完成
        事件（见 ``_drain_should_skip``）。
        """
        results = []
        while not self.completion_queue.empty():
            try:
                evt = self.completion_queue.get_nowait()
            except Exception:
                break
            _evt_sid = evt.get("session_id", "")
            if evt.get("type") == "completion" and self._drain_should_skip(_evt_sid):
                continue
            text = format_process_notification(evt)
            if text:
                results.append((evt, text))
        return results

    def get(self, session_id: str) -> Optional[ProcessSession]:
        """根据 ID 获取会话（运行中或已完成）。"""
        with self._lock:
            session = self._running.get(session_id) or self._finished.get(session_id)
        return self._refresh_detached_session(session)

    def _reconcile_local_exit(self, session: "ProcessSession") -> None:
        """根据真实子进程状态核对 session.exited。

        读取线程（`_reader_loop`）仅在其 `finally` 块（即 `stdout.read()`
        返回 EOF 时运行）中才将 `session.exited` 置为 True。如果直接的
        `Popen` 子进程已退出，但某个子孙进程（例如 `hermes update` 重启
        网关时派生的守护进程）仍然占着 stdout 管道不放手，读取线程会
        永远阻塞，poll() 也会无限地返回“running”（issue #17327 —— 在
        Feishu 上 7 分钟内轮询了 74 次）。

        本辅助函数用于关闭该窗口：当 `session.exited` 仍为 False，但
        直接子进程的 `Popen.poll()` 报告了退出码时，以非阻塞方式尽量
        读取可读字节，并将 `session.exited` 翻转。被孤立的读取线程仍卡
        在其阻塞式 `read()` 上，但它是守护线程，会随进程一起被回收。

        对于没有本地 `Popen` 的会话（env/PTY）、已退出的会话、以及从
        分离状态恢复的会话，本方法是无副作用的 no-op。
        """
        if session is None or session.exited:
            return
        proc = getattr(session, "process", None)
        if proc is None:
            return
        try:
            rc = proc.poll()
        except Exception:
            return
        if rc is None:
            return  # 直接子进程仍在运行 —— 读取线程阻塞是合理的。

        # 直接子进程已退出。尝试排出读取线程尚未消费的任何字节。这是
        # 尽力而为：如果管道被某个子孙进程占着不放手，非阻塞读取只会
        # 返回立即可用的内容，然后我们就停止。
        drained = ""
        stdout = getattr(proc, "stdout", None)
        if stdout is not None and not _IS_WINDOWS:
            try:
                import fcntl
                fd = stdout.fileno()
                flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
                try:
                    chunk = stdout.read()
                    if chunk:
                        drained = chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="replace")
                except (BlockingIOError, OSError, ValueError):
                    pass
                finally:
                    try:
                        fcntl.fcntl(fd, fcntl.F_SETFL, flags)
                    except Exception:
                        pass
            except Exception as e:
                logger.debug("Non-blocking drain failed for %s: %s", session.id, e)

        with session._lock:
            if drained:
                session.output_buffer += drained
                if len(session.output_buffer) > session.max_output_chars:
                    session.output_buffer = session.output_buffer[-session.max_output_chars:]
            session.exited = True
            if session.completion_reason != "killed":
                session.exit_code = rc
                session.completion_reason = "exited"
        logger.info(
            "Reconciled session %s: direct child exited with code %s but reader "
            "was still blocked (orphaned pipe). Flipped to exited.",
            session.id, rc,
        )
        self._move_to_finished(session)

    def poll(self, session_id: str) -> dict:
        """检查后台进程的状态并获取新输出。"""
        from tools.ansi_strip import strip_ansi

        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}

        # 在读取 session.exited 之前先与真实子进程状态核对。
        # 防范孤儿管道导致的读取线程挂起（issue #17327）。
        self._reconcile_local_exit(session)

        with session._lock:
            output_preview = strip_ansi(session.output_buffer[-1000:]) if session.output_buffer else ""

        result = {
            "session_id": session.id,
            "command": session.command,
            "status": "exited" if session.exited else "running",
            "pid": session.pid,
            "uptime_seconds": int(time.time() - session.started_at),
            "output_preview": output_preview,
        }
        if session.exited:
            result["exit_code"] = session.exit_code
            result["completion_reason"] = session.completion_reason
            result["termination_source"] = session.termination_source
            # 注意：poll() 是只读状态查询，刻意不标记会话为
            # _completion_consumed。wait()/read_log() 才代表真正的输出
            # 消费，并会进行标记。若在这里标记为已消费，一次状态检查就会
            # 静默抑制 notify_on_complete 监视器的自主投递回合（#10156）。
            #
            # 我们确实会把它记录到 _poll_observed 中，这样 CLI 的内联排出
            # 仍能去重（agent 已在本回合的 poll 结果里看到了退出），同时又
            # 不影响网关/tui 监视器 —— 它们只查询 _completion_consumed。
            self._poll_observed.add(session_id)
        if session.detached:
            result["detached"] = True
            result["note"] = "Process recovered after restart -- output history unavailable"
        return result

    def read_log(self, session_id: str, offset: int = 0, limit: int = 200) -> dict:
        """读取完整输出日志，可选按行分页。"""
        from tools.ansi_strip import strip_ansi

        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}

        with session._lock:
            full_output = strip_ansi(session.output_buffer)

        lines = full_output.splitlines()
        total_lines = len(lines)

        # 默认：最后 N 行
        if offset == 0 and limit > 0:
            selected = lines[-limit:]
        else:
            selected = lines[offset:offset + limit]

        result = {
            "session_id": session.id,
            "status": "exited" if session.exited else "running",
            "output": "\n".join(selected),
            "total_lines": total_lines,
            "showing": f"{len(selected)} lines",
        }
        if session.exited:
            self._completion_consumed.add(session_id)
        return result

    def wait(self, session_id: str, timeout: int = None) -> dict:
        """
        阻塞直到进程退出、超时或被中断。

        参数：
            session_id: 要等待的进程。
            timeout: 最长阻塞秒数。回退到 TERMINAL_TIMEOUT 配置。

        返回：
            dict，包含状态（"exited"、"timeout"、"interrupted"、"not_found"）
            以及输出快照。
        """
        from tools.ansi_strip import strip_ansi
        from tools.interrupt import is_interrupted as _is_interrupted

        try:
            default_timeout = int(os.getenv("TERMINAL_TIMEOUT", "180"))
        except (ValueError, TypeError):
            default_timeout = 180
        max_timeout = default_timeout
        requested_timeout = timeout
        timeout_note = None

        if requested_timeout and requested_timeout > max_timeout:
            effective_timeout = max_timeout
            timeout_note = (
                f"Requested wait of {requested_timeout}s was clamped "
                f"to configured limit of {max_timeout}s"
            )
        else:
            effective_timeout = requested_timeout or max_timeout

        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}

        deadline = time.monotonic() + effective_timeout

        while time.monotonic() < deadline:
            session = self._refresh_detached_session(session)
            if session is None:
                return {"status": "not_found", "error": f"No process with ID {session_id}"}
            # 与真实子进程状态核对 —— 防范读取线程被阻塞但直接子进程已退出
            # 的孤儿管道挂起（issue #17327）。
            self._reconcile_local_exit(session)
            if session.exited:
                self._completion_consumed.add(session_id)
                result = {
                    "status": "exited",
                    "exit_code": session.exit_code,
                    "completion_reason": session.completion_reason,
                    "termination_source": session.termination_source,
                    "output": strip_ansi(session.output_buffer[-2000:]),
                }
                if timeout_note:
                    result["timeout_note"] = timeout_note
                return result

            if _is_interrupted():
                result = {
                    "status": "interrupted",
                    "output": strip_ansi(session.output_buffer[-1000:]),
                    "note": "User sent a new message -- wait interrupted",
                }
                if timeout_note:
                    result["timeout_note"] = timeout_note
                return result

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            session._completion_event.wait(timeout=min(1.0, remaining))

        result = {
            "status": "timeout",
            "output": strip_ansi(session.output_buffer[-1000:]),
        }
        if timeout_note:
            result["timeout_note"] = timeout_note
        else:
            result["timeout_note"] = f"Waited {effective_timeout}s, process still running"
        return result

    def kill_process(self, session_id: str, *, source: str = "process.kill") -> dict:
        """杀死一个后台进程。"""
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}

        if session.exited:
            return {
                "status": "already_exited",
                "exit_code": session.exit_code,
            }

        # 通过 PTY、Popen（本地）或 env execute（非本地）来杀死
        try:
            if session._pty:
                # PTY 进程 —— 通过 ptyprocess 终止
                try:
                    session._pty.terminate(force=True)
                except Exception:
                    if session.pid:
                        os.kill(session.pid, signal.SIGTERM)
            elif session.process:
                # 本地进程 —— 杀死整棵进程树
                try:
                    if _IS_WINDOWS:
                        session.process.terminate()
                    else:
                        import psutil
                        try:
                            parent = psutil.Process(session.process.pid)
                            for child in parent.children(recursive=True):
                                try:
                                    child.terminate()
                                except psutil.NoSuchProcess:
                                    pass
                            parent.terminate()
                        except psutil.NoSuchProcess:
                            pass
                except (ProcessLookupError, PermissionError):
                    session.process.kill()
            elif session.env_ref and session.pid:
                # 非本地 —— 在沙箱内部杀死
                session.env_ref.execute(f"kill {session.pid} 2>/dev/null", timeout=5)
            elif session.detached and session.pid_scope == "host" and session.pid:
                # 做身份校验，而不是单纯存活检查：如果 PID 已消失或被复用到
                # 不相关的进程上，则把我们的进程视为已退出，永远不对陌生进程
                # 做 tree-kill。
                if not self._host_pid_is_ours(session.pid, session.host_start_time):
                    with session._lock:
                        session.exited = True
                        session.exit_code = None
                    self._move_to_finished(session)
                    return {
                        "status": "already_exited",
                        "exit_code": session.exit_code,
                    }
                self._terminate_host_pid(session.pid, session.host_start_time)
            else:
                return {
                    "status": "error",
                    "error": (
                        "Recovered process cannot be killed after restart because "
                        "its original runtime handle is no longer available"
                    ),
                }
            session.exited = True
            session.exit_code = -15  # SIGTERM
            session.completion_reason = "killed"
            session.termination_source = source
            self._move_to_finished(session)
            self._write_checkpoint()
            return {
                "status": "killed",
                "session_id": session.id,
                "completion_reason": session.completion_reason,
                "termination_source": session.termination_source,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def write_stdin(self, session_id: str, data: str) -> dict:
        """向运行中进程的 stdin 发送原始数据（不追加换行）。"""
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        if session.exited:
            return {"status": "already_exited", "error": "Process has already finished"}

        # PTY 模式 —— 通过 pty 句柄写入。
        if hasattr(session, '_pty') and session._pty:
            try:
                # pywinpty 在 Windows 上期望 str；ptyprocess 在 POSIX 上期望 bytes。
                if _IS_WINDOWS:
                    pty_data = data.decode("utf-8") if isinstance(data, bytes) else str(data)
                else:
                    pty_data = data.encode("utf-8") if isinstance(data, str) else data
                session._pty.write(pty_data)
                return {"status": "ok", "bytes_written": len(data)}
            except Exception as e:
                return {"status": "error", "error": str(e)}

        # Popen 模式 —— 通过 stdin 管道写入
        if not session.process or not session.process.stdin:
            return {"status": "error", "error": "Process stdin not available (non-local backend or stdin closed)"}
        try:
            session.process.stdin.write(data)
            session.process.stdin.flush()
            return {"status": "ok", "bytes_written": len(data)}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def submit_stdin(self, session_id: str, data: str = "") -> dict:
        """向运行中进程的 stdin 发送数据 + 换行（相当于按回车）。"""
        return self.write_stdin(session_id, data + "\n")

    def close_stdin(self, session_id: str) -> dict:
        """关闭运行中进程的 stdin / 发送 EOF，但不杀死进程。"""
        session = self.get(session_id)
        if session is None:
            return {"status": "not_found", "error": f"No process with ID {session_id}"}
        if session.exited:
            return {"status": "already_exited", "error": "Process has already finished"}

        if hasattr(session, '_pty') and session._pty:
            try:
                session._pty.sendeof()
                return {"status": "ok", "message": "EOF sent"}
            except Exception as e:
                return {"status": "error", "error": str(e)}

        if not session.process or not session.process.stdin:
            return {"status": "error", "error": "Process stdin not available (non-local backend or stdin closed)"}
        try:
            session.process.stdin.close()
            return {"status": "ok", "message": "stdin closed"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def count_running(self) -> int:
        """返回当前正在运行的后台进程数量。

        对运行中字典的一次廉价 O(1) 读取，适合在每次渲染时刻轮询状态栏。
        CPython 字典的 ``len()`` 是原子操作；调用方无需持有 ``self._lock``。
        仅反映 ``_running``：会话在其子进程退出时被移动到 ``_finished``。
        """
        try:
            return len(self._running)
        except Exception:
            return 0

    def list_sessions(self, task_id: str = None) -> list:
        """列出所有运行中以及近期完成的进程。"""
        with self._lock:
            all_sessions = list(self._running.values()) + list(self._finished.values())

        all_sessions = [self._refresh_detached_session(s) for s in all_sessions]

        if task_id:
            all_sessions = [s for s in all_sessions if s.task_id == task_id]

        result = []
        for s in all_sessions:
            entry = {
                "session_id": s.id,
                "command": s.command[:200],
                "cwd": s.cwd,
                "pid": s.pid,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(s.started_at)),
                "uptime_seconds": int(time.time() - s.started_at),
                "status": "exited" if s.exited else "running",
                "output_preview": s.output_buffer[-200:] if s.output_buffer else "",
            }
            # 触发器元数据，以便目标循环判定器可以决定等待该进程自身的
            # 信号（watch 模式匹配或完成），而不仅仅是等待其退出。带
            # watch_patterns 的监视器可能永不退出。
            if s.watch_patterns and not s._watch_disabled:
                entry["watch_patterns"] = list(s.watch_patterns)
                entry["watch_hit"] = s._watch_hits > 0
            if s.notify_on_complete:
                entry["notify_on_complete"] = True
            if s.exited:
                entry["exit_code"] = s.exit_code
            if s.detached:
                entry["detached"] = True
            result.append(entry)
        return result

    # ----- 会话/任务查询（用于网关集成） -----

    def has_active_processes(self, task_id: str) -> bool:
        """检查某个 task_id 是否有活跃（运行中）的进程。"""
        with self._lock:
            sessions = list(self._running.values())

        for session in sessions:
            self._refresh_detached_session(session)

        with self._lock:
            return any(
                s.task_id == task_id and not s.exited
                for s in self._running.values()
            )

    def has_active_for_session(self, session_key: str) -> bool:
        """检查某个网关会话键是否有活跃进程。"""
        with self._lock:
            sessions = list(self._running.values())

        for session in sessions:
            self._refresh_detached_session(session)

        with self._lock:
            return any(
                s.session_key == session_key and not s.exited
                for s in self._running.values()
            )

    def has_any_active(self) -> bool:
        """是否有任意后台进程仍在运行（跨所有会话）。

        供 scale-to-zero 空闲检测使用（gateway/scale_to_zero）：存在活跃
        后台进程（terminal background=true）的网关不算空闲，绝不能被挂起，
        否则进程会丢失。会先刷新分离的会话，使已完成但未被回收的进程被
        判定为非活跃。
        """
        with self._lock:
            sessions = list(self._running.values())

        for session in sessions:
            self._refresh_detached_session(session)

        with self._lock:
            return any(not s.exited for s in self._running.values())

    def kill_all(self, task_id: str = None) -> int:
        """杀死所有运行中的进程，可按 task_id 过滤。返回被杀死的数量。"""
        with self._lock:
            targets = [
                s for s in self._running.values()
                if (task_id is None or s.task_id == task_id) and not s.exited
            ]

        killed = 0
        for session in targets:
            result = self.kill_process(session.id, source="kill_all")
            if result.get("status") in {"killed", "already_exited"}:
                killed += 1
        return killed

    # ----- 清理 / 淘汰 -----

    def _prune_if_needed(self):
        """超过 MAX_PROCESSES 时移除最旧的已完成会话。必须持有 _lock。"""
        # 首先淘汰已过期的已完成会话
        now = time.time()
        expired = [
            sid for sid, s in self._finished.items()
            if (now - s.started_at) > FINISHED_TTL_SECONDS
        ]
        for sid in expired:
            del self._finished[sid]
            self._completion_consumed.discard(sid)
            self._poll_observed.discard(sid)

        # 若仍超限，则移除最旧的已完成会话
        total = len(self._running) + len(self._finished)
        if total >= MAX_PROCESSES and self._finished:
            oldest_id = min(self._finished, key=lambda sid: self._finished[sid].started_at)
            del self._finished[oldest_id]
            self._completion_consumed.discard(oldest_id)
            self._poll_observed.discard(oldest_id)

        # 丢弃那些对应会话已完全不再被跟踪的 _completion_consumed /
        # _poll_observed 条目 —— 作为双重保险，防止注册表查询路径上未能
        # 触及字典淘汰逻辑而导致的模块生命周期内无限增长。
        tracked = self._running.keys() | self._finished.keys()
        stale = self._completion_consumed - tracked
        if stale:
            self._completion_consumed -= stale
        stale_polls = self._poll_observed - tracked
        if stale_polls:
            self._poll_observed -= stale_polls

    # ----- Checkpoint（崩溃恢复） -----

    def _write_checkpoint(self):
        """以原子方式将运行中进程的元数据写入 checkpoint 文件。"""
        try:
            with self._lock:
                entries = []
                for s in self._running.values():
                    if not s.exited:
                        # 惰性地为宿主 PID 补填内核启动时间，这样重启后的恢复
                        # 即便对于在该字段引入之前启动的会话，也能检测 PID
                        # 复用。
                        if s.host_start_time is None and s.pid_scope == "host" and s.pid:
                            s.host_start_time = self._safe_host_start_time(s.pid)
                        entries.append({
                            "session_id": s.id,
                            "command": s.command,
                            "pid": s.pid,
                            "pid_scope": s.pid_scope,
                            "host_start_time": s.host_start_time,
                            "cwd": s.cwd,
                            "started_at": s.started_at,
                            "task_id": s.task_id,
                            "session_key": s.session_key,
                            "watcher_platform": s.watcher_platform,
                            "watcher_chat_id": s.watcher_chat_id,
                            "watcher_user_id": s.watcher_user_id,
                            "watcher_user_name": s.watcher_user_name,
                            "watcher_thread_id": s.watcher_thread_id,
                            "watcher_message_id": s.watcher_message_id,
                            "watcher_interval": s.watcher_interval,
                            "notify_on_complete": s.notify_on_complete,
                            "watch_patterns": s.watch_patterns,
                        })
            
            # 原子写入，避免崩溃时文件损坏
            from utils import atomic_json_write
            atomic_json_write(CHECKPOINT_PATH, entries)
        except Exception as e:
            logger.debug("Failed to write checkpoint file: %s", e, exc_info=True)

    def recover_from_checkpoint(self) -> int:
        """
        在网关启动时，从 checkpoint 文件探测各 PID。

        返回以分离状态恢复的进程数量。
        """
        if not CHECKPOINT_PATH.exists():
            return 0

        try:
            entries = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        except Exception:
            return 0

        recovered = 0
        for entry in entries:
            pid = entry.get("pid")
            if not pid:
                continue

            pid_scope = entry.get("pid_scope", "host")
            if pid_scope != "host":
                # 沙箱承载的进程在 checkpoint 中只保留沙箱内 PID，一旦原始
                # 环境句柄消失，这些 PID 对重启后的宿主进程来说就不再有意义。
                logger.info(
                    "Skipping recovery for non-host process: %s (pid=%s, scope=%s)",
                    entry.get("command", "unknown")[:60],
                    pid,
                    pid_scope,
                )
                continue

            # 该 PID 必须存活且仍是我们启动的同一个进程。单纯的存活检查
            # 不安全：经过一次重启（尤其是重启后或长时间运行后），内核可能
            # 已把这个编号复用到了一个不相关的进程上 —— 接纳它会让后续的
            # kill 或监视器对陌生进程（例如浏览器）做 tree-kill。重新校验
            # checkpoint 中记录的内核启动时间。
            recorded_start = entry.get("host_start_time")
            if not self._host_pid_is_ours(pid, recorded_start):
                if self._is_host_pid_alive(pid):
                    logger.info(
                        "Not recovering session %s: pid %d is alive but its "
                        "start time no longer matches — PID was recycled onto "
                        "an unrelated process; refusing to adopt it.",
                        entry.get("session_id", "?"), pid,
                    )
                continue

            session = ProcessSession(
                id=entry["session_id"],
                command=entry.get("command", "unknown"),
                task_id=entry.get("task_id", ""),
                session_key=entry.get("session_key", ""),
                pid=pid,
                host_start_time=recorded_start,
                pid_scope=pid_scope,
                cwd=entry.get("cwd"),
                started_at=entry.get("started_at", time.time()),
                detached=True,  # 无法读取输出，但可以报告状态 + 杀死
                watcher_platform=entry.get("watcher_platform", ""),
                watcher_chat_id=entry.get("watcher_chat_id", ""),
                watcher_user_id=entry.get("watcher_user_id", ""),
                watcher_user_name=entry.get("watcher_user_name", ""),
                watcher_thread_id=entry.get("watcher_thread_id", ""),
                watcher_message_id=entry.get("watcher_message_id", ""),
                watcher_interval=entry.get("watcher_interval", 0),
                notify_on_complete=entry.get("notify_on_complete", False),
                watch_patterns=entry.get("watch_patterns", []),
            )
            with self._lock:
                self._running[session.id] = session
            recovered += 1
            logger.info("Recovered detached process: %s (pid=%d)", session.command[:60], pid)

            # 重新入队监视器，以便网关恢复通知
            if session.watcher_interval > 0:
                self.pending_watchers.append({
                    "session_id": session.id,
                    "check_interval": session.watcher_interval,
                    "session_key": session.session_key,
                    "platform": session.watcher_platform,
                    "chat_id": session.watcher_chat_id,
                    "user_id": session.watcher_user_id,
                    "user_name": session.watcher_user_name,
                    "thread_id": session.watcher_thread_id,
                    "message_id": session.watcher_message_id,
                    "notify_on_complete": session.notify_on_complete,
                })

        self._write_checkpoint()

        return recovered


# 模块级单例
process_registry = ProcessRegistry()


def _format_age(seconds: float) -> str:
    """人类友好的耗时字符串（'18m'、'2h3m'、'45s'）。"""
    try:
        s = int(max(0, seconds))
    except (TypeError, ValueError):
        return "?"
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m" if s == 0 else f"{m}m{s}s"
    h, m = divmod(m, 60)
    return f"{h}h" if m == 0 else f"{h}h{m}m"


def _format_async_delegation(evt: dict) -> str:
    """把一次异步委派的完成事件格式化为一段自包含的重新注入文本。

    承载完整的原始任务来源（goal、父级提供的上下文、工具集、角色、模型），
    以及派发时间、状态和完整的结果摘要。当这段文本重新进入对话时，agent
    可能正深陷于无关的上下文中，已经不记得为何会存在这个子 agent，因此该
    文本块被写成完全自包含 —— 足以直接使用结果，或在情况已变化时重新
    派发。
    """
    import time as _time

    deleg_id = evt.get("delegation_id", "unknown")
    goal = evt.get("goal", "") or ""
    context = evt.get("context")
    toolsets = evt.get("toolsets")
    role = evt.get("role") or "leaf"
    model = evt.get("model") or "?"
    status = evt.get("status") or "completed"
    summary = evt.get("summary")
    error = evt.get("error")
    api_calls = evt.get("api_calls", 0)
    duration = evt.get("duration_seconds", "?")
    dispatched_at = evt.get("dispatched_at")
    completed_at = evt.get("completed_at") or _time.time()

    # ----- 批量（fan-out）完成：合并的多任务文本块 -----
    # 一次完整的 delegate_task fan-out 作为单个后台单元一起完成，并携带一个
    # 按任务划分的 `results` 列表。把每个子 agent 的摘要渲染到同一个文本块里，
    # 让模型一次性看到合并后的结果。
    batch_results = evt.get("results")
    if evt.get("is_batch") or isinstance(batch_results, list):
        results = batch_results or []
        goals = evt.get("goals") or []
        n = len(results) if results else len(goals)
        total_dur = evt.get("total_duration_seconds", duration)
        lines = [
            f"[ASYNC DELEGATION BATCH COMPLETE — {deleg_id}]",
            f"A background fan-out of {n} subagent(s) you dispatched earlier "
            "has finished. All ran in parallel and waited on each other; their "
            "consolidated results are below. You may have moved on since "
            "dispatching — act on these or re-dispatch if things have changed.",
            "",
        ]
        if isinstance(dispatched_at, (int, float)):
            ts = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(dispatched_at))
            age = f" ({_format_age(completed_at - dispatched_at)} ago)"
            lines.append(f"Dispatched: {ts}{age}")
        if context:
            lines.append(f"Context you provided: {context}")
        if toolsets:
            lines.append(f"Toolsets: {', '.join(toolsets)}")
        lines.append(f"Role: {role}   Model: {model}   Total duration: {total_dur}s")
        if error and not results:
            lines.append("--- ERROR ---")
            lines.append(f"The batch did not complete successfully: {error}")
            return "\n".join(lines)
        for r in sorted(results, key=lambda x: x.get("task_index", 0)):
            idx = r.get("task_index", 0)
            r_status = r.get("status", "?")
            r_summary = r.get("summary")
            r_error = r.get("error")
            r_goal = goals[idx] if idx < len(goals) else r.get("goal", "")
            icon = "✓" if r_status in ("completed", "success") else "✗"
            lines.append("")
            header = f"--- {icon} TASK {idx + 1}/{n}"
            if r_goal:
                header += f": {r_goal}"
            header += f"  (status={r_status}"
            if r.get("api_calls"):
                header += f", api_calls={r['api_calls']}"
            if r.get("duration_seconds") is not None:
                header += f", {r['duration_seconds']}s"
            header += ") ---"
            lines.append(header)
            if r_status in ("completed", "success") and r_summary:
                lines.append(r_summary)
            elif r_summary:
                if r_error:
                    lines.append(f"({r_status}: {r_error})")
                lines.append("Partial output:")
                lines.append(r_summary)
            else:
                lines.append(
                    f"(no summary — status={r_status}"
                    + (f": {r_error}" if r_error else "")
                    + ")"
                )
        return "\n".join(lines)

    age = ""
    if isinstance(dispatched_at, (int, float)):
        age = f" ({_format_age(completed_at - dispatched_at)} ago)"

    lines = [
        f"[ASYNC DELEGATION COMPLETE — {deleg_id}]",
        "A background subagent you dispatched earlier has finished. You may "
        "have moved on since dispatching it; the full task source is below so "
        "you can act on the result or re-dispatch if things have changed.",
        "",
    ]
    if isinstance(dispatched_at, (int, float)):
        ts = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(dispatched_at))
        lines.append(f"Dispatched: {ts}{age}")
    lines.append(f"Original goal: {goal}")
    if context:
        lines.append(f"Context you provided: {context}")
    if toolsets:
        lines.append(f"Toolsets: {', '.join(toolsets)}")
    lines.append(f"Role: {role}   Model: {model}")
    lines.append(f"Status: {status}   API calls: {api_calls}   Duration: {duration}s")
    lines.append("--- RESULT ---")
    if status in ("completed", "success") and summary:
        lines.append(summary)
    elif status == "interrupted":
        lines.append(
            "The subagent was interrupted before completing"
            + (f": {error}" if error else ".")
        )
        if summary:
            lines.append("Partial output:")
            lines.append(summary)
    else:
        # 错误 / 超时 / 失败
        lines.append(
            f"The subagent did not complete successfully (status={status})."
            + (f"\n{error}" if error else "")
        )
        if summary:
            lines.append("Partial output:")
            lines.append(summary)
    return "\n".join(lines)


def format_process_notification(evt: dict) -> "str | None":
    """把一个进程通知事件格式化为 [IMPORTANT: ...] 消息。

    处理来自统一 completion_queue 的完成事件（notify_on_complete）、
    watch 模式匹配以及 watch 被禁用事件。
    """
    evt_type = evt.get("type", "completion")
    _sid = evt.get("session_id", "unknown")
    _cmd = evt.get("command", "unknown")

    if evt_type == "watch_disabled":
        return f"[IMPORTANT: {evt.get('message', '')}]"

    if evt_type == "watch_match":
        _pat = evt.get("pattern", "?")
        _out = evt.get("output", "")
        _sup = evt.get("suppressed", 0)
        text = (
            f"[IMPORTANT: Background process {_sid} matched "
            f"watch pattern \"{_pat}\".\n"
            f"Command: {_cmd}\n"
            f"Matched output:\n{_out}"
        )
        if _sup:
            text += f"\n({_sup} earlier matches were suppressed by rate limit)"
        text += "]"
        return text

    if evt_type == "async_delegation":
        return _format_async_delegation(evt)

    _exit = evt.get("exit_code", "?")
    _out = evt.get("output", "")
    _reason = evt.get("completion_reason") or "exited"
    _source = evt.get("termination_source") or ""
    _signal = ""
    if _exit in {-15, 143, "-15", "143"}:
        _signal = ", SIGTERM"
    if _reason == "killed":
        _status = f"terminated by {_source or 'Hermes'}"
    elif _reason == "lost":
        _status = "marked lost because the process backend disappeared"
    elif _reason == "failed_start":
        _status = "failed to start"
    elif _exit == 0:
        _status = "completed normally"
    else:
        _status = "exited"
    return (
        f"[IMPORTANT: Background process {_sid} {_status} "
        f"(exit code {_exit}{_signal}).\n"
        f"Command: {_cmd}\n"
        f"Output:\n{_out}]"
    )


# ---------------------------------------------------------------------------
# 注册表 —— “process”工具的 schema + 处理器
# ---------------------------------------------------------------------------
from tools.registry import registry, tool_error

PROCESS_SCHEMA = {
    "name": "process",
    "description": (
        "Manage background processes started with terminal(background=true). "
        "Actions: 'list' (show all), 'poll' (check status + new output), "
        "'log' (full output with pagination), 'wait' (block until done or timeout), "
        "'kill' (terminate), 'write' (send raw stdin data without newline), "
        "'submit' (send data + Enter, for answering prompts), 'close' (close stdin/send EOF)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "poll", "log", "wait", "kill", "write", "submit", "close"],
                "description": "Action to perform on background processes"
            },
            "session_id": {
                "type": "string",
                "description": "Process session ID (from terminal background output). Required for all actions except 'list'."
            },
            "data": {
                "type": "string",
                "description": "Text to send to process stdin (for 'write' and 'submit' actions)"
            },
            "timeout": {
                "type": "integer",
                "description": "Max seconds to block for 'wait' action. Returns partial output on timeout.",
                "minimum": 1
            },
            "offset": {
                "type": "integer",
                "description": "Line offset for 'log' action (default: last 200 lines)"
            },
            "limit": {
                "type": "integer",
                "description": "Max lines to return for 'log' action",
                "minimum": 1
            }
        },
        "required": ["action"]
    }
}


def _handle_process(args, **kw):
    task_id = kw.get("task_id")
    action = args.get("action", "")
    # 强制转为字符串 —— 某些模型会把 session_id 当作整数发送
    session_id = str(args.get("session_id", "")) if args.get("session_id") is not None else ""

    if action == "list":
        return json.dumps({"processes": process_registry.list_sessions(task_id=task_id)}, ensure_ascii=False)
    elif action in {"poll", "log", "wait", "kill", "write", "submit", "close"}:
        if not session_id:
            return tool_error(f"session_id is required for {action}")
        if action == "poll":
            return json.dumps(process_registry.poll(session_id), ensure_ascii=False)
        elif action == "log":
            return json.dumps(process_registry.read_log(
                session_id, offset=args.get("offset", 0), limit=args.get("limit", 200)), ensure_ascii=False)
        elif action == "wait":
            return json.dumps(process_registry.wait(session_id, timeout=args.get("timeout")), ensure_ascii=False)
        elif action == "kill":
            return json.dumps(process_registry.kill_process(session_id), ensure_ascii=False)
        elif action == "write":
            return json.dumps(process_registry.write_stdin(session_id, str(args.get("data", ""))), ensure_ascii=False)
        elif action == "submit":
            return json.dumps(process_registry.submit_stdin(session_id, str(args.get("data", ""))), ensure_ascii=False)
        elif action == "close":
            return json.dumps(process_registry.close_stdin(session_id), ensure_ascii=False)
    return tool_error(f"Unknown process action: {action}. Use: list, poll, log, wait, kill, write, submit, close")


registry.register(
    name="process",
    toolset="terminal",
    schema=PROCESS_SCHEMA,
    handler=_handle_process,
    emoji="⚙️",
)
