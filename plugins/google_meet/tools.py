"""google_meet 插件面向 agent 的工具。

工具：
  meet_join        — 加入 Google Meet URL（在本地启动 Playwright bot
                     或通过 node=<name> 在远程节点主机上启动）
  meet_status      — 报告 bot 活跃度 + 转录进度
  meet_transcript  — 读取当前转录（可选 last-N）
  meet_leave       — 通知 bot 干净离开
  meet_say         — （v2）通过实时音频桥说话。
                     要求活跃会议已通过 mode='realtime' 加入。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from plugins.google_meet import process_manager as pm


# ---------------------------------------------------------------------------
# 运行时检查
# ---------------------------------------------------------------------------

def check_meet_requirements() -> bool:
    """当插件可以在本地实际运行时返回 True。

    检查项：
      * Python ``playwright`` 包可导入
      * 插件在支持的平台上（Linux 或 macOS）

    注意：远程节点操作（``node=<name>``）在网关侧
    仅需 ``websockets`` 依赖 — Chromium 位于节点上。
    但插件级检查保留了 v1 语义；当指定了节点时，
    各工具处理器会放宽该要求。
    """
    import platform as _p
    if _p.system().lower() not in {"linux", "darwin"}:
        return False
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# 节点客户端辅助函数
# ---------------------------------------------------------------------------

def _resolve_node_client(node: Optional[str]):
    """为 *node* 返回 (NodeClient, node_name)，或为本地运行返回 (None, None)。

    如果节点已命名但无法解析，则抛出带有可读消息的 RuntimeError，
    以便处理器可以向 agent 展示清晰的错误。
    """
    if node is None or node == "":
        return None, None
    from plugins.google_meet.node.registry import NodeRegistry
    from plugins.google_meet.node.client import NodeClient

    reg = NodeRegistry()
    entry = reg.resolve(node if node != "auto" else None)
    if entry is None:
        raise RuntimeError(
            f"no registered meet node matches {node!r} — "
            "run `hermes meet node approve <name> <url> <token>` first"
        )
    client = NodeClient(url=entry["url"], token=entry["token"])
    return client, entry.get("name")


# ---------------------------------------------------------------------------
# 模式定义
# ---------------------------------------------------------------------------

MEET_JOIN_SCHEMA: Dict[str, Any] = {
    "name": "meet_join",
    "description": (
        "Join a Google Meet call and start scraping live captions into a "
        "transcript file. Only meet.google.com URLs are accepted; no calendar "
        "scanning, no auto-dial. Spawns a headless Chromium subprocess that "
        "runs in parallel with the agent loop — returns immediately. Poll "
        "with meet_status and read captions with meet_transcript. Reminder "
        "to the agent: you should announce yourself in the meeting (there is "
        "no automatic consent announcement)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": (
                    "Full https://meet.google.com/... URL. Required."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["transcribe", "realtime"],
                "description": (
                    "transcribe (default): listen-only, scrape captions. "
                    "realtime: also enable agent speech via meet_say "
                    "(requires OpenAI Realtime key + platform audio bridge)."
                ),
            },
            "guest_name": {
                "type": "string",
                "description": (
                    "Display name to use when joining as guest. Defaults to "
                    "'Hermes Agent'."
                ),
            },
            "duration": {
                "type": "string",
                "description": (
                    "Optional max duration before auto-leave (e.g. '30m', "
                    "'2h', '90s'). Omit to stay until meet_leave is called."
                ),
            },
            "headed": {
                "type": "boolean",
                "description": (
                    "Run Chromium headed instead of headless (debug only). "
                    "Default false."
                ),
            },
            "node": {
                "type": "string",
                "description": (
                    "Name of a registered remote node to run the bot on "
                    "(useful when the gateway runs on a headless Linux box "
                    "but the user's Chrome with a signed-in Google profile "
                    "lives on their Mac). Pass 'auto' to use the single "
                    "registered node. Default: run locally. Nodes are "
                    "approved via `hermes meet node approve`."
                ),
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}

MEET_STATUS_SCHEMA: Dict[str, Any] = {
    "name": "meet_status",
    "description": (
        "Report the current Meet session state — whether the bot is alive, "
        "has joined, is sitting in the lobby, number of transcript lines "
        "captured, and last-caption timestamp."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "node": {"type": "string"},
        },
        "additionalProperties": False,
    },
}

MEET_TRANSCRIPT_SCHEMA: Dict[str, Any] = {
    "name": "meet_transcript",
    "description": (
        "Read the scraped transcript for the active Meet session. Returns "
        "full transcript unless 'last' is set, in which case returns the last "
        "N lines only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "last": {
                "type": "integer",
                "description": (
                    "Optional: return only the last N caption lines. Useful "
                    "for polling during a meeting without re-reading the "
                    "whole transcript."
                ),
                "minimum": 1,
            },
            "node": {"type": "string"},
        },
        "additionalProperties": False,
    },
}

MEET_LEAVE_SCHEMA: Dict[str, Any] = {
    "name": "meet_leave",
    "description": (
        "Leave the active Meet call cleanly, stop caption scraping, and "
        "finalize the transcript file. Safe to call when no meeting is "
        "active — returns ok=false with a reason."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "node": {"type": "string"},
        },
        "additionalProperties": False,
    },
}

MEET_SAY_SCHEMA: Dict[str, Any] = {
    "name": "meet_say",
    "description": (
        "Speak text into the active Meet call. Requires the active meeting "
        "to have been joined with mode='realtime'. The text is queued to "
        "the bot's OpenAI Realtime session; the generated audio is streamed "
        "into Chrome's fake microphone via a virtual audio device "
        "(PulseAudio null-sink on Linux, BlackHole on macOS). Returns "
        "immediately — the actual speech lags by a couple of seconds."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to speak."},
            "node": {"type": "string"},
        },
        "required": ["text"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# 处理器
# ---------------------------------------------------------------------------

def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _err(msg: str, **extra) -> str:
    return _json({"success": False, "error": msg, **extra})


def handle_meet_join(args: Dict[str, Any], **_kw) -> str:
    url = (args.get("url") or "").strip()
    if not url:
        return _err("url is required")
    mode = (args.get("mode") or "transcribe").strip().lower()
    if mode not in {"transcribe", "realtime"}:
        return _err(f"mode must be 'transcribe' or 'realtime' (got {mode!r})")

    node = args.get("node")
    try:
        client, node_name = _resolve_node_client(node)
    except RuntimeError as e:
        return _err(str(e))

    if client is not None:
        # 远程路径 — 委托给节点主机。
        try:
            res = client.start_bot(
                url=url,
                guest_name=str(args.get("guest_name") or "Hermes Agent"),
                duration=str(args.get("duration")) if args.get("duration") else None,
                headed=bool(args.get("headed", False)),
                mode=mode,
            )
            return _json({"success": bool(res.get("ok")), "node": node_name, **res})
        except Exception as e:
            return _err(f"remote node start_bot failed: {e}", node=node_name)

    # 本地路径 — 与 v1 相同，带有 v2 参数。
    if not check_meet_requirements():
        return _err(
            "google_meet plugin prerequisites missing — install with "
            "`pip install playwright && python -m playwright install "
            "chromium`. Plugin is supported on Linux and macOS only."
        )
    res = pm.start(
        url=url,
        headed=bool(args.get("headed", False)),
        guest_name=str(args.get("guest_name") or "Hermes Agent"),
        duration=str(args.get("duration")) if args.get("duration") else None,
        mode=mode,
    )
    return _json({"success": bool(res.get("ok")), **res})


def handle_meet_status(args: Dict[str, Any], **_kw) -> str:
    try:
        client, node_name = _resolve_node_client(args.get("node"))
    except RuntimeError as e:
        return _err(str(e))
    if client is not None:
        try:
            res = client.status()
            return _json({"success": bool(res.get("ok")), "node": node_name, **res})
        except Exception as e:
            return _err(f"remote node status failed: {e}", node=node_name)
    res = pm.status()
    return _json({"success": bool(res.get("ok")), **res})


def handle_meet_transcript(args: Dict[str, Any], **_kw) -> str:
    last = args.get("last")
    try:
        last_i = int(last) if last is not None else None
        if last_i is not None and last_i < 1:
            last_i = None
    except (TypeError, ValueError):
        last_i = None
    try:
        client, node_name = _resolve_node_client(args.get("node"))
    except RuntimeError as e:
        return _err(str(e))
    if client is not None:
        try:
            res = client.transcript(last=last_i)
            return _json({"success": bool(res.get("ok")), "node": node_name, **res})
        except Exception as e:
            return _err(f"remote node transcript failed: {e}", node=node_name)
    res = pm.transcript(last=last_i)
    return _json({"success": bool(res.get("ok")), **res})


def handle_meet_leave(args: Dict[str, Any], **_kw) -> str:
    try:
        client, node_name = _resolve_node_client(args.get("node"))
    except RuntimeError as e:
        return _err(str(e))
    if client is not None:
        try:
            res = client.stop()
            return _json({"success": bool(res.get("ok")), "node": node_name, **res})
        except Exception as e:
            return _err(f"remote node stop failed: {e}", node=node_name)
    res = pm.stop(reason="agent called meet_leave")
    return _json({"success": bool(res.get("ok")), **res})


def handle_meet_say(args: Dict[str, Any], **_kw) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        return _err("text is required")
    try:
        client, node_name = _resolve_node_client(args.get("node"))
    except RuntimeError as e:
        return _err(str(e))
    if client is not None:
        try:
            res = client.say(text)
            return _json({"success": bool(res.get("ok")), "node": node_name, **res})
        except Exception as e:
            return _err(f"remote node say failed: {e}", node=node_name)
    res = pm.enqueue_say(text)
    return _json({"success": bool(res.get("ok")), **res})
