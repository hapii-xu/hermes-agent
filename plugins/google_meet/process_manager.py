"""google_meet bot 的子进程生命周期管理器。

同一时间仅允许一个活跃会议。将运行中的 pid + out_dir 存储在
会话范围的状态文件 ``$HERMES_HOME/workspace/meetings/.active.json`` 中，
以便跨 turn 的工具调用可以找到 bot，且 ``on_session_end`` 可以
清理它。

bot 作为分离的子进程运行 — 我们不持有打开的文件描述符，
因此父 agent 循环不会因此阻塞。我们仅通过文件进行通信。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home

# 文件 + 目录布局（在 $HERMES_HOME 下）：
#
#   workspace/meetings/
#       .active.json                # 指向当前会话 bot 的指针
#       <meeting-id>/
#           status.json             # 活跃 bot 状态（由 bot 每个 tick 写入）
#           transcript.txt          # 抓取的字幕
#
# .active.json 包含：
#   {"pid": 12345, "meeting_id": "abc-defg-hij", "out_dir": "...",
#    "url": "https://meet.google.com/...", "started_at": 1714159200.0,
#    "session_id": "optional"}


def _root() -> Path:
    return Path(get_hermes_home()) / "workspace" / "meetings"


def _active_file() -> Path:
    return _root() / ".active.json"


def _read_active() -> Optional[Dict[str, Any]]:
    p = _active_file()
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_active(data: Dict[str, Any]) -> None:
    p = _active_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def _clear_active() -> None:
    try:
        _active_file().unlink()
    except FileNotFoundError:
        pass


def _pid_alive(pid: int) -> bool:
    # ``os.kill(pid, 0)`` 在 Windows 上并非空操作（bpo-14484）—
    # 它通过 GenerateConsoleCtrlEvent 路由，可能会杀死目标进程。
    # 使用跨平台的存在性检查。
    from gateway.status import _pid_exists
    return _pid_exists(pid)


# ---------------------------------------------------------------------------
# 公共 API — 供工具处理器 + CLI 使用
# ---------------------------------------------------------------------------

def start(
    url: str,
    *,
    out_dir: Optional[Path] = None,
    headed: bool = False,
    auth_state: Optional[str] = None,
    guest_name: str = "Hermes Agent",
    duration: Optional[str] = None,
    session_id: Optional[str] = None,
    mode: str = "transcribe",
    realtime_model: Optional[str] = None,
    realtime_voice: Optional[str] = None,
    realtime_instructions: Optional[str] = None,
    realtime_api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """为 *url* 启动 meet_bot 子进程。

    如果此 hermes 安装已有 bot 在运行，则先离开 —
    我们强制实施单活跃会议语义。

    返回描述已启动 bot 的字典。
    """
    from plugins.google_meet.meet_bot import _is_safe_meet_url, _meeting_id_from_url

    if not _is_safe_meet_url(url):
        return {
            "ok": False,
            "error": (
                "refusing: only https://meet.google.com/ URLs are allowed. "
                "got: " + repr(url)
            ),
        }

    existing = _read_active()
    if existing and _pid_alive(int(existing.get("pid", 0))):
        stop(reason="replaced by new meet_join")

    meeting_id = _meeting_id_from_url(url)
    out = out_dir or (_root() / meeting_id)
    out.mkdir(parents=True, exist_ok=True)

    # 清除此会议 id 之前运行留下的任何陈旧转录/状态文件，
    # 以免轮询混淆。
    for name in ("transcript.txt", "status.json"):
        f = out / name
        if f.exists():
            try:
                f.unlink()
            except OSError:
                pass

    env = os.environ.copy()
    env["HERMES_MEET_URL"] = url
    env["HERMES_MEET_OUT_DIR"] = str(out)
    env["HERMES_MEET_GUEST_NAME"] = guest_name
    if headed:
        env["HERMES_MEET_HEADED"] = "1"
    if auth_state:
        env["HERMES_MEET_AUTH_STATE"] = auth_state
    if duration:
        env["HERMES_MEET_DURATION"] = duration
    # v2：实时模式 + 透传参数。如果未设置 HERMES_MEET_MODE，
    # bot 默认使用转录模式，与 v1 行为一致。
    if mode:
        env["HERMES_MEET_MODE"] = mode
    if realtime_model:
        env["HERMES_MEET_REALTIME_MODEL"] = realtime_model
    if realtime_voice:
        env["HERMES_MEET_REALTIME_VOICE"] = realtime_voice
    if realtime_instructions:
        env["HERMES_MEET_REALTIME_INSTRUCTIONS"] = realtime_instructions
    if realtime_api_key:
        env["HERMES_MEET_REALTIME_KEY"] = realtime_api_key

    log_path = out / "bot.log"
    # 分离：stdin=devnull，stdout/stderr → 日志文件，新 session 使父进程
    # 信号不会传播。
    log_fh = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "plugins.google_meet.meet_bot"],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        # 子进程现在拥有日志 fd；我们可以关闭我们的。
        log_fh.close()

    record = {
        "pid": proc.pid,
        "meeting_id": meeting_id,
        "out_dir": str(out),
        "url": url,
        "started_at": time.time(),
        "session_id": session_id,
        "log_path": str(log_path),
        "mode": mode,
    }
    _write_active(record)
    return {"ok": True, **record}


def status() -> Dict[str, Any]:
    """返回当前会议状态，或 ``{"ok": False, "reason": ...}``。"""
    active = _read_active()
    if not active:
        return {"ok": False, "reason": "no active meeting"}

    pid = int(active.get("pid", 0))
    alive = _pid_alive(pid) if pid else False

    status_path = Path(active.get("out_dir", "")) / "status.json"
    bot_status: Dict[str, Any] = {}
    if status_path.is_file():
        try:
            bot_status = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    return {
        "ok": True,
        "alive": alive,
        "pid": pid,
        "meetingId": active.get("meeting_id"),
        "url": active.get("url"),
        "startedAt": active.get("started_at"),
        "outDir": active.get("out_dir"),
        **bot_status,
    }


def transcript(last: Optional[int] = None) -> Dict[str, Any]:
    """读取当前转录文件。如果不存在则返回 ok=False。"""
    active = _read_active()
    if not active:
        return {"ok": False, "reason": "no active meeting"}

    tp = Path(active.get("out_dir", "")) / "transcript.txt"
    if not tp.is_file():
        return {
            "ok": True,
            "meetingId": active.get("meeting_id"),
            "lines": [],
            "total": 0,
            "path": str(tp),
        }
    text = tp.read_text(encoding="utf-8", errors="replace")
    all_lines = [ln for ln in text.splitlines() if ln.strip()]
    lines = all_lines[-last:] if last else all_lines
    return {
        "ok": True,
        "meetingId": active.get("meeting_id"),
        "lines": lines,
        "total": len(all_lines),
        "path": str(tp),
    }


def enqueue_say(text: str) -> Dict[str, Any]:
    """将 ``say`` 请求追加到活跃 bot 的 JSONL 队列。

    当没有活跃会议或活跃 bot 处于仅转录模式时，
    返回 ``{"ok": False, "reason": ...}``。否则向 bot 的实时 speaker 线程
    将消费的 ``<out_dir>/say_queue.jsonl`` 写入一行。
    """
    import uuid

    text = (text or "").strip()
    if not text:
        return {"ok": False, "reason": "text is required"}

    active = _read_active()
    if not active:
        return {"ok": False, "reason": "no active meeting"}
    if active.get("mode") != "realtime":
        return {
            "ok": False,
            "reason": (
                "active meeting is in transcribe mode — pass mode='realtime' "
                "to meet_join to enable agent speech"
            ),
        }

    out_dir = Path(active.get("out_dir", ""))
    if not out_dir.is_dir():
        return {"ok": False, "reason": f"out_dir missing: {out_dir}"}

    queue_path = out_dir / "say_queue.jsonl"
    entry = {"id": uuid.uuid4().hex[:12], "text": text}
    with queue_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return {
        "ok": True,
        "meetingId": active.get("meeting_id"),
        "enqueued_id": entry["id"],
        "queue_path": str(queue_path),
    }


def stop(*, reason: str = "requested") -> Dict[str, Any]:
    """通知活跃 bot 干净离开，然后清除活跃指针。

    发送 SIGTERM 并等待最多 10 秒让 bot 退出。如果 bot 无响应，
    则回退到 SIGKILL。
    """
    active = _read_active()
    if not active:
        return {"ok": False, "reason": "no active meeting"}

    pid = int(active.get("pid", 0))
    out_dir = active.get("out_dir")
    transcript_path = Path(out_dir) / "transcript.txt" if out_dir else None

    if pid and _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        for _ in range(20):
            if not _pid_alive(pid):
                break
            time.sleep(0.5)
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)  # windows-footgun: ok — POSIX-only plugin (google_meet registers no-op on Windows; see __init__.py)
            except ProcessLookupError:
                pass

    _clear_active()
    return {
        "ok": True,
        "reason": reason,
        "meetingId": active.get("meeting_id"),
        "transcriptPath": str(transcript_path) if transcript_path else None,
    }
