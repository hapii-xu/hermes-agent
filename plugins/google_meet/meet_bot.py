"""无头 Google Meet bot — Playwright + 实时字幕抓取。

作为由 ``process_manager.py`` 启动的独立子进程运行。从环境变量
读取配置，将状态 + 转录内容写入
``$HERMES_HOME/workspace/meetings/<meeting-id>/`` 下的文件。主 hermes 进程
通过 ``meet_*`` 工具读取这些文件 — 除文件系统外无 IPC。

抓取策略借鉴 OpenUtter（sumansid/openutter）：我们不解析 WebRTC 音频，
而是启用 Google Meet 内置的实时字幕，并通过 MutationObserver 观察
DOM 中的字幕容器。这种方式有损且偏向英语，但：

* 确定性（无需 API key，无 STT 计费），
* 可在 Meet 的正常登录/准入流程下工作，
* 由于字幕容器具有稳定的 ARIA role，能较好地应对 Meet UI 重写。

独立运行以进行调试::

    HERMES_MEET_URL=https://meet.google.com/abc-defg-hij \\
    HERMES_MEET_OUT_DIR=/tmp/meet-debug \\
    HERMES_MEET_HEADED=1 \\
    python -m plugins.google_meet.meet_bot

非 meet.google.com URL → 以非零状态退出。任何不以
``https://meet.google.com/`` 开头的 URL 都会被拒绝（显式拒绝设计）。
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# 匹配 ``https://meet.google.com/abc-defg-hij`` 或 ``.../lookup/...`` —
# 即三段式短码或 lookup URL。其他任何内容均被拒绝。
MEET_URL_RE = re.compile(
    r"^https://meet\.google\.com/("
    r"[a-z0-9]{3,}-[a-z0-9]{3,}-[a-z0-9]{3,}"
    r"|lookup/[^/?#]+"
    r"|new"
    r")(?:[/?#].*)?$"
)


# bot 在 ``HERMES_MEET_OUT_DIR`` 中读写的文件名。
SAY_QUEUE_FILENAME = "say_queue.jsonl"
SAY_PCM_FILENAME = "speaker.pcm"


def _is_safe_meet_url(url: str) -> bool:
    """如果 *url* 是我们愿意导航到的 Google Meet URL，则返回 True。"""
    if not isinstance(url, str):
        return False
    return bool(MEET_URL_RE.match(url.strip()))


def _meeting_id_from_url(url: str) -> str:
    """从 Meet URL 中提取三段式会议代码。

    对于 ``https://meet.google.com/abc-defg-hij`` → ``abc-defg-hij``。
    对于 ``.../lookup/<id>`` 或 ``/new``，我们回退到基于时间戳的 id —
    bot 在重定向之前无法得知真实代码，而调用方无论如何都会将其传递给文件名。
    """
    m = re.search(
        r"meet\.google\.com/([a-z0-9]{3,}-[a-z0-9]{3,}-[a-z0-9]{3,})",
        url or "",
    )
    if m:
        return m.group(1)
    return f"meet-{int(time.time())}"


# ---------------------------------------------------------------------------
# 状态 + 转录文件写入器
# ---------------------------------------------------------------------------

class _BotState:
    """单进程可变状态，每次变更时刷新到 ``status.json``。"""

    def __init__(self, out_dir: Path, meeting_id: str, url: str):
        self.out_dir = out_dir
        self.meeting_id = meeting_id
        self.url = url
        self.in_call = False
        self.captioning = False
        self.captions_enabled_attempted = False
        self.lobby_waiting = False
        self.join_attempted_at: Optional[float] = None
        self.joined_at: Optional[float] = None
        self.last_caption_at: Optional[float] = None
        self.transcript_lines = 0
        self.error: Optional[str] = None
        self.exited = False
        # v2 实时字段。
        self.realtime = False
        self.realtime_ready = False
        self.realtime_device: Optional[str] = None
        self.audio_bytes_out: int = 0
        self.last_audio_out_at: Optional[float] = None
        self.last_barge_in_at: Optional[float] = None
        self.leave_reason: Optional[str] = None
        # 按顺序抓取的标题，去重。每个条目是
        # {"ts": <epoch>, "speaker": str, "text": str} 形式的字典。
        self._seen: set = set()
        out_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_path = out_dir / "transcript.txt"
        self.status_path = out_dir / "status.json"
        self._flush()

    # -------- transcript ------------------------------------------------

    def record_caption(self, speaker: str, text: str) -> None:
        """如果尚未见过此 (speaker, text) 组合，则追加一条字幕行。"""
        speaker = (speaker or "").strip() or "Unknown"
        text = (text or "").strip()
        if not text:
            return
        key = f"{speaker}|{text}"
        if key in self._seen:
            return
        self._seen.add(key)
        self.transcript_lines += 1
        self.last_caption_at = time.time()
        ts = time.strftime("%H:%M:%S", time.localtime(self.last_caption_at))
        line = f"[{ts}] {speaker}: {text}\n"
        # 近似原子追加 — 对于单写入者已足够。
        with self.transcript_path.open("a", encoding="utf-8") as f:
            f.write(line)
        self._flush()

    # -------- status file ----------------------------------------------

    def _flush(self) -> None:
        data = {
            "meetingId": self.meeting_id,
            "url": self.url,
            "inCall": self.in_call,
            "captioning": self.captioning,
            "captionsEnabledAttempted": self.captions_enabled_attempted,
            "lobbyWaiting": self.lobby_waiting,
            "joinAttemptedAt": self.join_attempted_at,
            "joinedAt": self.joined_at,
            "lastCaptionAt": self.last_caption_at,
            "transcriptLines": self.transcript_lines,
            "transcriptPath": str(self.transcript_path),
            "error": self.error,
            "exited": self.exited,
            "pid": os.getpid(),
            # v2 实时遥测。
            "realtime": self.realtime,
            "realtimeReady": self.realtime_ready,
            "realtimeDevice": self.realtime_device,
            "audioBytesOut": self.audio_bytes_out,
            "lastAudioOutAt": self.last_audio_out_at,
            "lastBargeInAt": self.last_barge_in_at,
            "leaveReason": self.leave_reason,
        }
        tmp = self.status_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.status_path)

    def set(self, **kwargs) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)
        self._flush()


# ---------------------------------------------------------------------------
# Playwright bot 入口点
# ---------------------------------------------------------------------------

# 注入到 Meet 标签页中以观察字幕的 JavaScript。通过
# MutationObserver 在字幕容器上捕获 {speaker, text} 元组，
# 并暴露 ``window.__hermesMeetDrain()`` 以提取新条目。此方法
# 借鉴了 OpenUtter 的字幕抓取方式。
_CAPTION_OBSERVER_JS = r"""
(() => {
  if (window.__hermesMeetInstalled) return;
  window.__hermesMeetInstalled = true;
  window.__hermesMeetQueue = [];

  const captionSelector = '[role="region"][aria-label*="aption" i], ' +
                          'div[jsname="YSxPC"], ' +  // legacy
                          'div[jsname="tgaKEf"]';    // current (Apr 2026)

  function pushEntry(speaker, text) {
    if (!text || !text.trim()) return;
    window.__hermesMeetQueue.push({
      ts: Date.now(),
      speaker: (speaker || '').trim(),
      text: text.trim(),
    });
  }

  function scan(root) {
    // Meet captions render as a list of rows; each row contains a speaker
    // label and a text block. Selectors vary across Meet rewrites; we try
    // a few shapes and fall back to raw text.
    const rows = root.querySelectorAll('div[jsname="dsyhDe"], div.CNusmb, div.TBMuR');
    if (rows.length) {
      rows.forEach((row) => {
        const spkEl = row.querySelector('div.KcIKyf, div.zs7s8d, span[jsname="YSxPC"]');
        const txtEl = row.querySelector('div.bh44bd, span[jsname="tgaKEf"], div.iTTPOb');
        const speaker = spkEl ? spkEl.innerText : '';
        const text = txtEl ? txtEl.innerText : row.innerText;
        pushEntry(speaker, text);
      });
      return;
    }
    // Fallback: treat the whole region's innerText as one anonymous line.
    const text = (root.innerText || '').split('\n').filter(Boolean).pop();
    pushEntry('', text);
  }

  function attach() {
    const el = document.querySelector(captionSelector);
    if (!el) return false;
    const obs = new MutationObserver(() => scan(el));
    obs.observe(el, { childList: true, subtree: true, characterData: true });
    scan(el);
    return true;
  }

  // Try now and retry on interval — the caption region only appears after
  // captions are enabled and someone speaks.
  if (!attach()) {
    const iv = setInterval(() => { if (attach()) clearInterval(iv); }, 1500);
  }

  window.__hermesMeetDrain = () => {
    const out = window.__hermesMeetQueue.slice();
    window.__hermesMeetQueue = [];
    return out;
  };
})();
"""


def _enable_captions_js() -> str:
    """返回一个小型 JS 片段，尝试点击"开启字幕"按钮。

    尽力而为 — Meet 的字幕切换可通过键盘 ``c`` 键访问。我们
    将该按键作为低成本备选方案进行分发。实际点击定位
    过于脆弱，无法依赖。
    """
    return r"""
    (() => {
      const ev = new KeyboardEvent('keydown', {
        key: 'c', code: 'KeyC', keyCode: 67, which: 67, bubbles: true,
      });
      document.body.dispatchEvent(ev);
      return true;
    })();
    """


def _start_realtime_speaker(
    *,
    rt: dict,
    out_dir: Path,
    bridge_info: dict,
    api_key: str,
    model: str,
    voice: str,
    instructions: str,
    stop_flag: dict,
    state: "_BotState",
) -> None:
    """连接 OpenAI Realtime session + speaker 线程 + PCM 泵。

    speaker 线程从 ``say_queue.jsonl`` 读取文本行，将每行
    发送到 OpenAI Realtime，并将 PCM 音频写入 ``speaker.pcm``。一个
    独立的 *泵* 线程将该 PCM 转发到 OS 音频 sink，以便
    Chrome 的假麦克风拾取。在 Linux 上，我们通过管道传输到
    null-sink 的 ``paplay``；在 macOS 上，调用方需将 BlackHole
    设备选为默认输入。
    """
    try:
        from plugins.google_meet.realtime.openai_client import (
            RealtimeSession,
            RealtimeSpeaker,
        )
    except Exception as e:
        state.set(error=f"realtime import failed: {e}")
        return

    pcm_path = out_dir / SAY_PCM_FILENAME
    queue_path = out_dir / SAY_QUEUE_FILENAME
    processed_path = out_dir / "say_processed.jsonl"
    # 重置 sink 文件，使每次会话都从干净状态开始。
    pcm_path.write_bytes(b"")
    # 确保队列文件存在，以免 speaker 轮询器在
    # 第一次迭代时出错。
    queue_path.touch()

    try:
        session = RealtimeSession(
            api_key=api_key,
            model=model,
            voice=voice,
            instructions=instructions,
            audio_sink_path=pcm_path,
            sample_rate=24000,
        )
        session.connect()
    except Exception as e:
        state.set(error=f"realtime connect failed: {e}")
        return

    rt["session"] = session

    def _stop_fn():
        return stop_flag.get("stop", False)

    rt["speaker_stop"] = lambda: stop_flag.__setitem__("stop", stop_flag.get("stop", False))

    speaker = RealtimeSpeaker(
        session=session,
        queue_path=queue_path,
        processed_path=processed_path,
    )

    def _speaker_loop():
        try:
            speaker.run_until_stopped(_stop_fn)
        except Exception as e:
            state.set(error=f"realtime speaker crashed: {e}")

    t_speaker = threading.Thread(target=_speaker_loop, name="meet-speaker", daemon=True)
    t_speaker.start()
    rt["speaker_thread"] = t_speaker

    # PCM 泵：将 speaker.pcm（24kHz s16le 单声道）馈送到
    # Chrome 假麦克风读取的 OS 音频设备。不同平台
    # 使用不同工具，但约定相同 — 块读取不断增长的
    # PCM 文件，并以近实时方式流式传输到设备。
    platform_tag = (bridge_info or {}).get("platform")
    if platform_tag == "linux":
        import subprocess as _sp

        sink = (bridge_info or {}).get("write_target") or "hermes_meet_sink"
        try:
            proc = _sp.Popen(
                [
                    "paplay",
                    "--raw",
                    "--rate=24000",
                    "--format=s16le",
                    "--channels=1",
                    f"--device={sink}",
                    str(pcm_path),
                ],
                stdin=_sp.DEVNULL,
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
            )
            rt["pcm_pump"] = proc
        except FileNotFoundError:
            state.set(error="paplay not found — install pulseaudio-utils for realtime on Linux")
    elif platform_tag == "darwin":
        # macOS：使用 ffmpeg 尾部读取 speaker.pcm 并将其写入
        # BlackHole 输出设备。用户必须在系统设置 → 声音中
        # 选择 BlackHole 作为默认输入，Chrome 才能拾取。
        # 我们首选 ffmpeg，因为它可脚本化且可以
        # 按名称指定 AVFoundation 设备；如果 ffmpeg 不存在，则回退到
        # 紧密循环中 afplay 该文件。
        import shutil as _shutil
        import subprocess as _sp

        device_name = (bridge_info or {}).get("write_target") or "BlackHole 2ch"
        if _shutil.which("ffmpeg"):
            try:
                # -re：以原生帧率读取输入。
                # -f avfoundation -i：speaker 路径作为原始 PCM。
                # -f s16le -ar 24000 -ac 1 -i <pcm>：解释该文件。
                # -f audiotoolbox -audio_device_index：写入 BlackHole。
                # 更简单的方式：通过 coreaudio 使用 "-f audiotoolbox" 输出原始数据。
                # ffmpeg 的 audiotoolbox 输出会选择当前默认
                # 输出设备，这不是我们想要的。因此我们使用
                # -f avfoundation 并将命名设备作为 OUTPUT，
                # 配合 -vn 和设备名称。
                proc = _sp.Popen(
                    [
                        "ffmpeg",
                        "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-re",
                        "-f", "s16le", "-ar", "24000", "-ac", "1",
                        "-i", str(pcm_path),
                        "-f", "audiotoolbox",
                        "-audio_device_index", _mac_audio_device_index(device_name),
                        "-",
                    ],
                    stdin=_sp.DEVNULL,
                    stdout=_sp.DEVNULL,
                    stderr=_sp.DEVNULL,
                )
                rt["pcm_pump"] = proc
            except FileNotFoundError:
                state.set(error="ffmpeg not found — install via `brew install ffmpeg` for realtime on macOS")
            except Exception as e:
                state.set(error=f"macOS pcm pump failed to start: {e}")
        else:
            state.set(error="ffmpeg not found — install via `brew install ffmpeg` for realtime on macOS")


def _mac_audio_device_index(device_name: str) -> str:
    """返回 *device_name* 的 ffmpeg ``-audio_device_index``，以字符串形式。

    探测 ``ffmpeg -f avfoundation -list_devices true -i ''``（在 stderr 上
    打印设备表），并忽略大小写匹配 *device_name*。
    如果找不到设备，默认返回 ``"0"`` — 调用方将获得错误路由的流
    但不会崩溃，且错误将是显而易见的。
    """
    import subprocess as _sp

    try:
        out = _sp.run(
            ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return "0"
    # ffmpeg 在 stderr 上打印设备表。行格式如下：
    #   [AVFoundation indev @ 0x...] [0] BlackHole 2ch
    import re as _re

    needle = device_name.strip().lower()
    for line in (out.stderr or "").splitlines():
        m = _re.search(r"\[(\d+)\]\s+(.+)$", line)
        if not m:
            continue
        if m.group(2).strip().lower() == needle:
            return m.group(1)
    return "0"


def run_bot() -> int:  # noqa: C901 — orchestration, explicit branches
    url = os.environ.get("HERMES_MEET_URL", "").strip()
    out_dir_env = os.environ.get("HERMES_MEET_OUT_DIR", "").strip()
    headed = os.environ.get("HERMES_MEET_HEADED", "").lower() in {"1", "true", "yes"}
    auth_state = os.environ.get("HERMES_MEET_AUTH_STATE", "").strip()
    guest_name = os.environ.get("HERMES_MEET_GUEST_NAME", "Hermes Agent")
    duration_s = _parse_duration(os.environ.get("HERMES_MEET_DURATION", ""))
    # v2：可选实时模式。当 HERMES_MEET_MODE=realtime 时启用。
    mode = os.environ.get("HERMES_MEET_MODE", "transcribe").strip().lower()
    realtime_model = os.environ.get("HERMES_MEET_REALTIME_MODEL", "gpt-realtime")
    realtime_voice = os.environ.get("HERMES_MEET_REALTIME_VOICE", "alloy")
    realtime_instructions = os.environ.get("HERMES_MEET_REALTIME_INSTRUCTIONS", "")
    realtime_api_key = os.environ.get("HERMES_MEET_REALTIME_KEY") or os.environ.get("OPENAI_API_KEY", "")

    if not url or not _is_safe_meet_url(url):
        sys.stderr.write(
            "google_meet bot: refusing to launch — HERMES_MEET_URL must be a "
            "meet.google.com URL. got: %r\n" % url
        )
        return 2
    if not out_dir_env:
        sys.stderr.write("google_meet bot: HERMES_MEET_OUT_DIR is required\n")
        return 2

    out_dir = Path(out_dir_env)
    meeting_id = _meeting_id_from_url(url)
    state = _BotState(out_dir=out_dir, meeting_id=meeting_id, url=url)

    # SIGTERM → 干净退出，以便父进程 ``meet_leave`` 获取最终
    # 转录内容。我们设置一个标志而非抛出异常，以便 Playwright context
    # 在下面的 finally 块中执行清理。
    stop_flag = {"stop": False}

    def _on_signal(_sig, _frame):
        stop_flag["stop"] = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # v2 实时：配置虚拟音频设备 + 启动 speaker 线程。
    # 我们将这些存储在字典中，以便 finally 块可以
    无论我们如何退出都能进行清理。如果实时设置中的任何内容失败，我们
    # 回退到带有状态标志的转录模式。
    rt = {
        "enabled": mode == "realtime",
        "bridge": None,            # AudioBridge | None
        "bridge_info": None,       # dict | None
        "session": None,           # RealtimeSession | None
        "speaker_thread": None,    # threading.Thread | None
        "speaker_stop": None,      # callable | None
    }
    if rt["enabled"]:
        if not realtime_api_key:
            state.set(error="realtime mode requested but no API key in HERMES_MEET_REALTIME_KEY/OPENAI_API_KEY — falling back to transcribe")
            rt["enabled"] = False
        else:
            try:
                from plugins.google_meet.audio_bridge import AudioBridge
                bridge = AudioBridge()
                rt["bridge_info"] = bridge.setup()
                rt["bridge"] = bridge
                state.set(realtime=True, realtime_device=rt["bridge_info"].get("device_name"))
            except Exception as e:
                state.set(error=f"audio bridge setup failed: {e} — falling back to transcribe")
                rt["enabled"] = False

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        state.set(error=f"playwright not installed: {e}", exited=True)
        sys.stderr.write(
            "google_meet bot: playwright is not installed. Run "
            "`pip install playwright && python -m playwright install chromium`\n"
        )
        if rt["bridge"]:
            rt["bridge"].teardown()
        return 3

    # Chrome 环境：如果在 Linux 上实时模式已启动，将 PULSE_SOURCE 指向
    # 虚拟 source，以便 Chrome 的假麦克风读取我们生成的音频。
    chrome_env = os.environ.copy()
    chrome_args = [
        "--use-fake-ui-for-media-stream",
        "--disable-blink-features=AutomationControlled",
    ]
    if not rt["enabled"]:
        # v1 风格假设备（静音）— 当我们不发声时
        # 我们不在乎麦克风内容。
        chrome_args.insert(1, "--use-fake-device-for-media-stream")
    elif rt["bridge_info"] and rt["bridge_info"].get("platform") == "linux":
        chrome_env["PULSE_SOURCE"] = rt["bridge_info"].get("device_name", "")

    try:
        with sync_playwright() as pw:
            # Playwright 的 launch() 不接受 env 参数；我们在启动前通过
            # 进程 env 设置 PULSE_SOURCE，以便子 Chrome 继承它。
            for k, v in chrome_env.items():
                os.environ[k] = v
            browser = pw.chromium.launch(
                headless=not headed,
                args=chrome_args,
            )
            context_args = {
                "viewport": {"width": 1280, "height": 800},
                "user_agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "permissions": ["microphone", "camera"],
            }
            if auth_state and Path(auth_state).is_file():
                context_args["storage_state"] = auth_state
            context = browser.new_context(**context_args)
            page = context.new_page()

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                state.set(error=f"navigate failed: {e}", exited=True)
                return 4

            # 访客模式：Meet 在"请求加入"前显示名称字段。
            # 当我们已认证时，我们看到的是"立即加入"。
            _try_guest_name(page, guest_name)
            _click_join(page, state)

            # 安装字幕观察器并尝试启用字幕。
            try:
                page.evaluate(_enable_captions_js())
                state.set(captions_enabled_attempted=True)
            except Exception:
                pass
            try:
                page.evaluate(_CAPTION_OBSERVER_JS)
            except Exception as e:
                state.set(error=f"caption observer install failed: {e}")

            # 注意：在准入确认之前 in_call=False（我们通过
            # 检测"离开"按钮或字幕区域来判断，表示我们
            # 已通过大厅）。
            state.set(captioning=True, join_attempted_at=time.time())

            # v2 实时：启动从插件侧 say 队列读取的 speaker 线程。
            # 该线程读取 meet_say 写入的 JSONL 行，调用 OpenAI Realtime，
            # 并将音频 PCM 流式传输到 Chrome 假麦克风所指向的虚拟 sink。
            if rt["enabled"]:
                _start_realtime_speaker(
                    rt=rt,
                    out_dir=out_dir,
                    bridge_info=rt["bridge_info"],
                    api_key=realtime_api_key,
                    model=realtime_model,
                    voice=realtime_voice,
                    instructions=realtime_instructions,
                    stop_flag=stop_flag,
                    state=state,
                )
                if rt["session"] is not None:
                    state.set(realtime_ready=True)

            # 准入 + 排空循环。运行直到 SIGTERM、时长到期，
            # 或页面检测到"您已被移除/您已离开
            # 会议"。负责：
            #   * 检测准入（"离开"按钮可见 → in_call=True）
            #   * 超时卡在大厅（默认 5 分钟）
            #   * 将抓取的字幕排空到转录文件
            #   * 当人类在 bot 正在生成音频时发言时触发实时打断
            #   * 定期将实时计数器刷新到 status.json
            deadline = (time.time() + duration_s) if duration_s else None
            lobby_deadline = time.time() + float(
                os.environ.get("HERMES_MEET_LOBBY_TIMEOUT", "300")
            )
            last_admission_check = 0.0
            while not stop_flag["stop"]:
                now = time.time()
                if deadline and now > deadline:
                    state.set(leave_reason="duration_expired")
                    break

                # 每 ~3 秒进行一次准入检测，直到准入。
                if not state.in_call and (now - last_admission_check) > 3.0:
                    last_admission_check = now
                    admitted = _detect_admission(page)
                    if admitted:
                        state.set(
                            in_call=True,
                            lobby_waiting=False,
                            joined_at=now,
                        )
                    elif now > lobby_deadline:
                        state.set(
                            error=(
                                "lobby timeout — host never admitted the bot "
                                f"within {int(lobby_deadline - state.join_attempted_at) if state.join_attempted_at else 0}s"
                            ),
                            leave_reason="lobby_timeout",
                        )
                        break
                    elif _detect_denied(page):
                        state.set(
                            error="host denied admission",
                            leave_reason="denied",
                        )
                        break

                try:
                    queued = page.evaluate("window.__hermesMeetDrain && window.__hermesMeetDrain()")
                    if isinstance(queued, list):
                        for entry in queued:
                            if not isinstance(entry, dict):
                                continue
                            speaker = str(entry.get("speaker", ""))
                            text = str(entry.get("text", ""))
                            state.record_caption(speaker=speaker, text=text)
                            # 打断：如果 bot 当前正在生成
                            # 音频 AND 一个真实人类刚刚发言，取消
                            # 正在进行的响应，以免我们盖过他们。
                            if rt["enabled"] and rt["session"] is not None:
                                if _looks_like_human_speaker(speaker, guest_name):
                                    try:
                                        cancelled = rt["session"].cancel_response()
                                        if cancelled:
                                            state.set(last_barge_in_at=now)
                                    except Exception:
                                        pass
                except Exception:
                    # Meet 已刷新或我们被踢出 — 尝试检测并
                    # 优雅退出，而非持续旋转。
                    if page.is_closed():
                        state.set(leave_reason="page_closed")
                        break

                # 将实时 session 的字节/时间戳计数器折叠到
                # 状态文件中，以便 meet_status 可以展示它们。
                if rt["session"] is not None:
                    state.set(
                        audio_bytes_out=getattr(rt["session"], "audio_bytes_out", 0),
                        last_audio_out_at=getattr(rt["session"], "last_audio_out_at", None),
                    )

                time.sleep(1.0)

            # 尝试干净离开 — 如果存在则点击"离开通话"按钮。
            try:
                page.evaluate(
                    "() => { const b = document.querySelector('button[aria-label*=\"eave call\"]');"
                    " if (b) b.click(); }"
                )
            except Exception:
                pass

            context.close()
            browser.close()
            # v2：清理 PCM 泵、speaker 线程和音频桥。
            if rt.get("pcm_pump"):
                try:
                    rt["pcm_pump"].terminate()
                    rt["pcm_pump"].wait(timeout=3)
                except Exception:
                    pass
            if rt["speaker_stop"]:
                try:
                    rt["speaker_stop"]()
                except Exception:
                    pass
            if rt["speaker_thread"] is not None:
                try:
                    rt["speaker_thread"].join(timeout=5.0)
                except Exception:
                    pass
            if rt["session"]:
                try:
                    rt["session"].close()
                except Exception:
                    pass
            if rt["bridge"]:
                try:
                    rt["bridge"].teardown()
                except Exception:
                    pass
            state.set(in_call=False, captioning=False, exited=True)
            return 0

    except Exception as e:
        state.set(error=f"unhandled: {e}", exited=True)
        return 1


def _try_guest_name(page, guest_name: str) -> None:
    """如果 Meet 正在显示访客名称输入框，则在其中输入 *guest_name*。"""
    try:
        # Meet 的访客名称输入框占位符为 "Your name"。
        locator = page.locator('input[aria-label*="name" i]').first
        if locator.count() and locator.is_visible():
            locator.fill(guest_name, timeout=2_000)
    except Exception:
        pass


def _detect_admission(page) -> bool:
    """如果我们已明确通过大厅并进入通话本身，则返回 True。

    使用 JS 端探测，因为 Meet 的 DOM 结构因客户端
    版本而异。我们检查几个高信号指标，并在
    首次命中时声明准入：

      1. 离开通话按钮存在（``aria-label`` 包含"eave call"）。
      2. 字幕区域已出现（我们安装了观察器且其已附加）。
      3. 参与者列表容器可见。

    默认保守 — 任何错误时返回 False。
    """
    probe = r"""
    (() => {
      const leave = document.querySelector('button[aria-label*="eave call" i]');
      if (leave) return true;
      if (window.__hermesMeetInstalled) {
        const caps = document.querySelector(
          '[role="region"][aria-label*="aption" i], ' +
          'div[jsname="YSxPC"], div[jsname="tgaKEf"]'
        );
        if (caps) return true;
      }
      const parts = document.querySelector('[aria-label*="articipants" i]');
      if (parts) return true;
      return false;
    })();
    """
    try:
        return bool(page.evaluate(probe))
    except Exception:
        return False


def _detect_denied(page) -> bool:
    """当 Meet 显示"您已被拒绝"/"无人准入"页面时返回 True。"""
    probe = r"""
    (() => {
      const text = document.body ? document.body.innerText || '' : '';
      # 仅限英语 — 匹配主持人拒绝或
      # 移除访客时显示的内容。
      if (/You can't join this video call/i.test(text)) return true;
      if (/You were removed from the meeting/i.test(text)) return true;
      if (/No one responded to your request to join/i.test(text)) return true;
      return false;
    })();
    """
    try:
        return bool(page.evaluate(probe))
    except Exception:
        return False


def _looks_like_human_speaker(speaker: str, bot_guest_name: str) -> bool:
    """判断字幕行的说话者是否可能是人类，而非我们的 bot 回声。

    Meet 将字幕归属于说话者的显示名称。当 Chrome
    读取我们的假麦克风时，Meet 仍将字幕归属于 *我们的* bot 名称
    （因为是 bot 在"说话"）。我们不希望这些触发
    打断。其他任何内容 — 真实参与者名称 — 则会触发。

    保守策略：未知/空白说话者（当字幕抓取
    回退到原始文本时常见）不会触发打断，因为我们无法判断
    是人类还是我们自己。
    """
    if not speaker or not speaker.strip():
        return False
    spk = speaker.strip().lower()
    if spk in {"unknown", "you", bot_guest_name.strip().lower()}:
        return False
    return True


def _click_join(page, state: _BotState) -> None:
    """如果"立即加入"或"请求加入"按钮可见，则点击。

    当我们进入"等待主持人准入"状态时标记
    ``lobby_waiting``，以便 agent 可以在状态中展示。
    """
    for label in ("Join now", "Ask to join"):
        try:
            btn = page.get_by_role("button", name=label, exact=False).first
            if btn.count() and btn.is_visible():
                btn.click(timeout=3_000)
                if label == "Ask to join":
                    state.set(lobby_waiting=True)
                break
        except Exception:
            continue


def _parse_duration(raw: str) -> Optional[float]:
    """解析 ``30m`` / ``2h`` / ``90``（秒）→ float 秒数，或 None。"""
    if not raw:
        return None
    raw = raw.strip().lower()
    try:
        if raw.endswith("h"):
            return float(raw[:-1]) * 3600
        if raw.endswith("m"):
            return float(raw[:-1]) * 60
        if raw.endswith("s"):
            return float(raw[:-1])
        return float(raw)
    except ValueError:
        return None


if __name__ == "__main__":  # pragma: no cover — subprocess entry point
    sys.exit(run_bot())
