"""`computer_use` 工具的入口。

通用的（任意模型皆可）桌面控制，覆盖 macOS、Windows 和 Linux，通过
cua-driver 的后台 computer-use 原语实现。替代了 #4562 中 Anthropic
原生的 `computer_20251124` 方案——这里的 schema 是标准的 OpenAI
函数调用格式，因此任何一个支持工具调用的模型都能驱动它。

Linux 是最近加入的运行时（X11 + Wayland，通过 cua-driver-rs 的
AT-SPI 树路径）；它在这里与 macOS、Windows 一并启用。当某台主机的
显示服务器或无障碍栈不可达时，cua-driver 的 `health_report`
（由 `hermes computer-use doctor` 暴露）会报告具体哪一项检查被阻塞，
而不是让整个工具集静默失败。

返回约定
-------
对于纯文本结果（wait、key、list_apps、focus_app、失败等）：
  JSON 字符串。

对于带 `capture_after=True` 的截图/动作：
  一个字典，包装为 OpenAI 风格的多分片工具消息内容：

      {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": "<人类可读摘要 + SOM 索引>"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,<b64>"}},
        ],
        "text_summary": "<用于回退字符串内容的文本>",
      }

  run_agent.py 的工具消息构建器会检查 `_multimodal`，并为兼容 OpenAI 的
  provider 发出列表形式的 `content`。Anthropic 适配器会把 base64 图片
  拼接进一个 `tool_result` 块（见 `agent/anthropic_adapter.py`）。每一个
  支持多分片工具内容的 provider 都能拿到图片；仅支持文本的 provider 只
  会看到摘要。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import struct
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

from tools.computer_use.backend import (
    ActionResult,
    CaptureResult,
    ComputerUseBackend,
    UIElement,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 审批与安全
# ---------------------------------------------------------------------------

_approval_callback = None


def set_approval_callback(cb) -> None:
    """注册一个用于 computer_use 审批提示的回调（CLI 使用）。

    与 terminal_tool._approval_callback 的模式一致。回调接收
    (action, args, summary)，并返回以下之一：
      "approve_once" | "approve_session" | "always_approve" | "deny"。
    """
    global _approval_callback
    _approval_callback = cb


# 只读、不修改状态的动作。始终允许。
_SAFE_ACTIONS = frozenset({"capture", "wait", "list_apps"})

# 会修改用户可见状态的动作。需要经过审批。
_DESTRUCTIVE_ACTIONS = frozenset({
    "click", "double_click", "right_click", "middle_click",
    "drag", "scroll", "type", "key", "set_value", "focus_app",
})

# 被硬性屏蔽的按键组合。镜像自 #4562 —— 无论审批级别如何，这些组合都是
# 破坏性的（例如注销会杀掉 Hermes 运行所在的会话）。
_BLOCKED_KEY_COMBOS = {
    frozenset({"cmd", "shift", "backspace"}),   # 清空废纸篓
    frozenset({"cmd", "option", "backspace"}),   # 强制删除
    frozenset({"cmd", "ctrl", "q"}),             # 锁屏
    frozenset({"cmd", "shift", "q"}),            # 注销
    frozenset({"cmd", "option", "shift", "q"}),  # 强制注销
    # Windows 安全/会话快捷键。Windows 驱动接受 Win 键组合，而 Alt 在下面
    # 会被规范化为 option，因此要在任何后端看到之前先屏蔽这些破坏性变体。
    frozenset({"win", "l"}),
    frozenset({"ctrl", "option", "delete"}),
    frozenset({"ctrl", "option", "del"}),
    frozenset({"option", "f4"}),
}

_KEY_ALIASES = {
    "command": "cmd", "control": "ctrl", "alt": "option", "⌘": "cmd", "⌥": "option",
    "windows": "win", "super": "win", "meta": "win",
}


def _canon_key_combo(keys: str) -> frozenset:
    parts = [p.strip().lower() for p in re.split(r"\s*\+\s*", keys) if p.strip()]
    parts = [_KEY_ALIASES.get(p, p) for p in parts]
    return frozenset(parts)


# `type` 动作的危险文本模式。与 #4562 中的列表相同。
_BLOCKED_TYPE_PATTERNS = [
    re.compile(r"curl\s+[^|]*\|\s*bash", re.IGNORECASE),
    re.compile(r"curl\s+[^|]*\|\s*sh", re.IGNORECASE),
    re.compile(r"wget\s+[^|]*\|\s*bash", re.IGNORECASE),
    re.compile(r"\bsudo\s+rm\s+-[rf]", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\s+/\s*$", re.IGNORECASE),
    re.compile(r":\s*\(\)\s*\{\s*:\|:\s*&\s*\}", re.IGNORECASE),  # fork 炸弹
]


def _is_blocked_type(text: str) -> Optional[str]:
    for pat in _BLOCKED_TYPE_PATTERNS:
        if pat.search(text):
            return pat.pattern
    return None


# ---------------------------------------------------------------------------
# 后端选择 —— 可通过环境变量替换，便于测试
# ---------------------------------------------------------------------------

# 进程级缓存的后端；在首次调用时懒加载。
_backend_lock = threading.Lock()
_backend: Optional[ComputerUseBackend] = None
# 会话级审批状态。
_session_auto_approve = False
_always_allow: set = set()  # 用户为本会话解锁的动作名


def _get_backend() -> ComputerUseBackend:
    global _backend
    with _backend_lock:
        if _backend is None:
            backend_name = os.environ.get("HERMES_COMPUTER_USE_BACKEND", "cua").lower()
            if backend_name in {"cua", "cua-driver", ""}:
                from tools.computer_use.cua_backend import CuaDriverBackend
                _backend = CuaDriverBackend()
            elif backend_name == "noop":  # pragma: no cover
                _backend = _NoopBackend()
            else:
                raise RuntimeError(f"Unknown HERMES_COMPUTER_USE_BACKEND={backend_name!r}")
            try:
                _backend.start()
            except Exception:
                # 不要缓存一个 start() 失败的后端（例如懒安装依赖被拒绝/失败）。
                # 下一次调用会干净地重试，而不是返回一个半初始化的后端。
                _backend = None
                raise
        return _backend


def reset_backend_for_tests() -> None:  # pragma: no cover
    """测试辅助 —— 拆除缓存的后端。"""
    global _backend, _session_auto_approve, _always_allow
    with _backend_lock:
        if _backend is not None:
            try:
                _backend.stop()
            except Exception:
                pass
        _backend = None
    _session_auto_approve = False
    _always_allow = set()


class _NoopBackend(ComputerUseBackend):  # pragma: no cover
    """测试/CI 桩。记录调用；返回平凡结果。"""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self._started = False

    def start(self) -> None: self._started = True
    def stop(self) -> None: self._started = False
    def is_available(self) -> bool: return True

    def capture(self, mode: str = "som", app: Optional[str] = None) -> CaptureResult:
        self.calls.append(("capture", {"mode": mode, "app": app}))
        return CaptureResult(mode=mode, width=1024, height=768, png_b64=None,
                             elements=[], app=app or "", window_title="")

    def click(self, **kw) -> ActionResult:
        self.calls.append(("click", kw))
        return ActionResult(ok=True, action="click")

    def drag(self, **kw) -> ActionResult:
        self.calls.append(("drag", kw))
        return ActionResult(ok=True, action="drag")

    def scroll(self, **kw) -> ActionResult:
        self.calls.append(("scroll", kw))
        return ActionResult(ok=True, action="scroll")

    def type_text(self, text: str) -> ActionResult:
        self.calls.append(("type", {"text": text}))
        return ActionResult(ok=True, action="type")

    def key(self, keys: str) -> ActionResult:
        self.calls.append(("key", {"keys": keys}))
        return ActionResult(ok=True, action="key")

    def list_apps(self) -> List[Dict[str, Any]]:
        self.calls.append(("list_apps", {}))
        return []

    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        self.calls.append(("focus_app", {"app": app, "raise": raise_window}))
        return ActionResult(ok=True, action="focus_app")

    def set_value(self, value: str, element: Optional[int] = None) -> ActionResult:
        self.calls.append(("set_value", {"value": value, "element": element}))
        return ActionResult(ok=True, action="set_value")


# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------

def handle_computer_use(args: Dict[str, Any], **kwargs) -> Any:
    """主入口 —— 由 tools.registry 分发。

    返回一个 JSON 字符串（纯文本）或一个带 `_multimodal` 标记的字典
    （图片 + 摘要），后者由 run_agent.py 包装进工具消息。
    """
    action = (args.get("action") or "").strip().lower()
    if not action:
        return json.dumps({"error": "missing `action`"})

    # 安全：在审批提示之前先校验动作。
    if action == "type":
        text = args.get("text", "")
        pat = _is_blocked_type(text)
        if pat:
            return json.dumps({
                "error": f"blocked pattern in type text: {pat!r}",
                "hint": "Dangerous shell patterns cannot be typed via computer_use.",
            })

    if action == "key":
        keys = args.get("keys", "")
        combo = _canon_key_combo(keys)
        for blocked in _BLOCKED_KEY_COMBOS:
            if blocked.issubset(combo) and len(blocked) <= len(combo):
                return json.dumps({
                    "error": f"blocked key combo: {sorted(blocked)}",
                    "hint": "Destructive system shortcuts are hard-blocked.",
                })

    # 审批闸门（仅针对破坏性动作）。
    if action in _DESTRUCTIVE_ACTIONS:
        err = _request_approval(action, args)
        if err is not None:
            return err

    # 分发到后端。
    try:
        backend = _get_backend()
    except Exception as e:
        return json.dumps({
            "error": f"computer_use backend unavailable: {e}",
            "hint": "If the cua-driver binary is missing, run `hermes computer-use install`. "
                    "If a Python dependency is missing, the error above shows the exact install command.",
        })

    try:
        return _dispatch(backend, action, args)
    except Exception as e:
        logger.exception("computer_use %s failed", action)
        return json.dumps({"error": f"{action} failed: {e}"})


def _request_approval(action: str, args: Dict[str, Any]) -> Optional[str]:
    """已批准则返回 None，被拒绝则返回一个 JSON 错误字符串。"""
    global _session_auto_approve, _always_allow
    if _session_auto_approve:
        return None
    if action in _always_allow:
        return None
    cb = _approval_callback
    if cb is None:
        # 未接入 CLI 审批 —— 默认允许。网关审批由外层一层的常规工具审批
        # 基础设施处理。
        return None
    summary = _summarize_action(action, args)
    try:
        verdict = cb(action, args, summary)
    except Exception as e:
        logger.warning("approval callback failed: %s", e)
        verdict = "deny"
    if verdict == "approve_once":
        return None
    if verdict == "approve_session" or verdict == "always_approve":
        _always_allow.add(action)
        if verdict == "always_approve":
            _session_auto_approve = True
        return None
    return json.dumps({"error": "denied by user", "action": action})


def _summarize_action(action: str, args: Dict[str, Any]) -> str:
    if action in {"click", "double_click", "right_click", "middle_click"}:
        if args.get("element") is not None:
            return f"{action} element #{args['element']}"
        coord = args.get("coordinate")
        if coord:
            return f"{action} at {tuple(coord)}"
        return action
    if action == "drag":
        src = args.get("from_element") or args.get("from_coordinate")
        dst = args.get("to_element") or args.get("to_coordinate")
        return f"drag {src} → {dst}"
    if action == "scroll":
        return f"scroll {args.get('direction', '?')} x{args.get('amount', 3)}"
    if action == "type":
        text = args.get("text", "")
        return f"type {text[:60]!r}" + ("..." if len(text) > 60 else "")
    if action == "key":
        return f"key {args.get('keys', '')!r}"
    if action == "focus_app":
        return f"focus {args.get('app', '')!r}" + (" (raise)" if args.get("raise_window") else "")
    return action


def _dispatch(backend: ComputerUseBackend, action: str, args: Dict[str, Any]) -> Any:
    capture_after = bool(args.get("capture_after"))

    if action == "capture":
        mode = str(args.get("mode", "som"))
        if mode not in {"som", "vision", "ax"}:
            return json.dumps({"error": f"bad mode {mode!r}; use som|vision|ax"})
        cap = backend.capture(mode=mode, app=args.get("app"))
        return _capture_response(cap, max_elements=_coerce_max_elements(args.get("max_elements")))

    if action == "wait":
        seconds = float(args.get("seconds", 1.0))
        res = backend.wait(seconds)
        return _text_response(res)

    if action == "list_apps":
        apps = backend.list_apps()
        return json.dumps({"apps": apps, "count": len(apps)})

    if action == "focus_app":
        app = args.get("app")
        if not app:
            return json.dumps({"error": "focus_app requires `app`"})
        res = backend.focus_app(app, raise_window=bool(args.get("raise_window")))
        return _maybe_follow_capture(backend, res, capture_after)

    if action in {"click", "double_click", "right_click", "middle_click"}:
        button = args.get("button")
        click_count = 1
        if action == "double_click":
            click_count = 2
        elif action == "right_click":
            button = "right"
        elif action == "middle_click":
            button = "middle"
        else:
            button = button or "left"
        element = args.get("element")
        coord = args.get("coordinate") or (None, None)
        x, y = (coord[0], coord[1]) if coord and coord[0] is not None else (None, None)
        res = backend.click(
            element=element if element is not None else None,
            x=x, y=y, button=button or "left", click_count=click_count,
            modifiers=args.get("modifiers"),
        )
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "drag":
        has_elements = args.get("from_element") is not None and args.get("to_element") is not None
        has_coords = args.get("from_coordinate") and args.get("to_coordinate")
        if not has_elements and not has_coords:
            return json.dumps({
                "error": "drag requires from_coordinate/to_coordinate or from_element/to_element",
            })
        res = backend.drag(
            from_element=args.get("from_element"),
            to_element=args.get("to_element"),
            from_xy=tuple(args["from_coordinate"]) if args.get("from_coordinate") else None,
            to_xy=tuple(args["to_coordinate"]) if args.get("to_coordinate") else None,
            button=args.get("button", "left"),
            modifiers=args.get("modifiers"),
        )
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "scroll":
        coord = args.get("coordinate") or (None, None)
        res = backend.scroll(
            direction=args.get("direction", "down"),
            amount=int(args.get("amount", 3)),
            element=args.get("element"),
            x=coord[0] if coord and coord[0] is not None else None,
            y=coord[1] if coord and coord[1] is not None else None,
            modifiers=args.get("modifiers"),
        )
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "type":
        res = backend.type_text(args.get("text", ""))
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "key":
        res = backend.key(args.get("keys", ""))
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "set_value":
        value = args.get("value")
        if value is None:
            return json.dumps({"error": "set_value requires `value`"})
        res = backend.set_value(value=str(value), element=args.get("element"))
        return _maybe_follow_capture(backend, res, capture_after)

    return json.dumps({"error": f"unknown action {action!r}"})


# ---------------------------------------------------------------------------
# 响应整形
# ---------------------------------------------------------------------------

def _text_response(res: ActionResult) -> str:
    payload: Dict[str, Any] = {"ok": res.ok, "action": res.action}
    if res.message:
        payload["message"] = res.message
    if res.meta:
        payload["meta"] = res.meta
    return json.dumps(payload)


# capture 返回的 AX `elements` 数组的默认上限。密集的 UI（Electron 应用、
# Obsidian、JetBrains IDE）可能发布 500+ 个 AX 节点，单次截图就能耗尽
# 会话上下文。面向模型的 `max_elements` 参数允许调用方在需要完整树时调高。
_DEFAULT_MAX_ELEMENTS = 100
# 调用方提供的 `max_elements` 的硬性上限。没有这个限制的话，传入一个非常
# 大整数的工具调用会静默禁用这个保护，重新引入原本的无界行为。
_MAX_ALLOWED_MAX_ELEMENTS = 1000
_MIN_PROVIDER_IMAGE_DIMENSION = 8


def _image_dimensions_from_b64(image_b64: str) -> Optional[Tuple[int, int]]:
    """返回常见内联截图格式的 (width, height)。

    一些 provider 会在模型看到工具结果之前就拒绝小于 8x8 的图片。在这里
    检查编码后的字节，可以让 computer_use 回退到它的 AX/SOM 文本载荷，
    而不是发送一个无法使用的占位图。
    """
    if not image_b64:
        return None
    try:
        raw = base64.b64decode(image_b64, validate=False)
    except Exception:
        return None

    # PNG：签名 + IHDR 宽/高。
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
        try:
            width, height = struct.unpack(">II", raw[16:24])
            return int(width), int(height)
        except Exception:
            return None

    # JPEG：扫描携带尺寸信息的 SOF 标记。
    if raw.startswith(b"\xff\xd8") and len(raw) > 4:
        i = 2
        while i + 9 < len(raw):
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            i += 2
            while marker == 0xFF and i < len(raw):
                marker = raw[i]
                i += 1
            if marker in {0xD8, 0xD9}:
                continue
            if marker == 0xDA:
                break
            if i + 2 > len(raw):
                break
            segment_len = int.from_bytes(raw[i:i + 2], "big")
            if segment_len < 2 or i + segment_len > len(raw):
                break
            if marker in {
                0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
            } and segment_len >= 7:
                height = int.from_bytes(raw[i + 3:i + 5], "big")
                width = int.from_bytes(raw[i + 5:i + 7], "big")
                return int(width), int(height)
            i += segment_len
    return None


def _coerce_max_elements(value: Any) -> int:
    """校验调用方提供的 ``max_elements``。

    对于缺失 / 非整数 / 小于 1 的输入，回退到 :data:`_DEFAULT_MAX_ELEMENTS`，
    这样上限永远不会因为一个格式错误的工具调用参数而被静默禁用。将过大
    的值钳制到 :data:`_MAX_ALLOWED_MAX_ELEMENTS`，使调用方无法通过传入
    极大整数来绕过保护。
    """
    if value is None:
        return _DEFAULT_MAX_ELEMENTS
    try:
        n = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ELEMENTS
    if n < 1:
        return _DEFAULT_MAX_ELEMENTS
    if n > _MAX_ALLOWED_MAX_ELEMENTS:
        return _MAX_ALLOWED_MAX_ELEMENTS
    return n


def _capture_response(cap: CaptureResult, max_elements: int = _DEFAULT_MAX_ELEMENTS) -> Any:
    total_elements = len(cap.elements)
    visible_elements = cap.elements[:max_elements]
    truncated_elements = max(0, total_elements - len(visible_elements))
    image_dimensions = _image_dimensions_from_b64(cap.png_b64 or "") if cap.png_b64 else None
    response_width = image_dimensions[0] if image_dimensions else cap.width
    response_height = image_dimensions[1] if image_dimensions else cap.height
    image_too_small = bool(
        image_dimensions
        and (
            image_dimensions[0] < _MIN_PROVIDER_IMAGE_DIMENSION
            or image_dimensions[1] < _MIN_PROVIDER_IMAGE_DIMENSION
        )
    )

    # 只对响应中实际呈现的内容建立索引——否则人类可读摘要会引用模型在
    # JSON `elements` 数组里找不到的元素索引（例如 max_elements=10 vs
    # 默认 40 行的索引窗口）。
    element_index = _format_elements(visible_elements)
    summary_lines = [
        f"capture mode={cap.mode} {response_width}x{response_height}"
        + (f" app={cap.app}" if cap.app else "")
        + (f" window={cap.window_title!r}" if cap.window_title else ""),
        f"{total_elements} interactable element(s):",
    ]
    if element_index:
        summary_lines.extend(element_index)
    # 多模态路径和 AX 路径都会引用 `summary`，因此预先构建一次，这样辅助
    # 视觉路由分支（它在两条路径被选定之前触发）就有一个有效值可以交给
    # _route_capture_through_aux_vision。AX 路径会在下方把"已截断为 M 个
    # 中的 N 个"提示追加到 summary_lines 并重建；多模态路径则保持此版本
    # 不变。
    if image_too_small:
        summary_lines.append(
            f"  (screenshot omitted: {image_dimensions[0]}x{image_dimensions[1]} "
            f"is below the {_MIN_PROVIDER_IMAGE_DIMENSION}x{_MIN_PROVIDER_IMAGE_DIMENSION} "
            "provider minimum)"
        )
    summary = "\n".join(summary_lines)

    if cap.png_b64 and cap.mode != "ax" and not image_too_small:
        # 决定是把截图交给 auxiliary.vision 管线（纯文本结果），还是保留
        # 多模态封装（主模型原生处理视觉）。Issue #24015：以前多模态封装
        # 被无条件返回，因此非视觉主模型即使在显式配置了 auxiliary.vision
        # 来处理此情况时，也会在 provider 边界触发 HTTP 404 / 400。
        if _should_route_through_aux_vision():
            routed = _route_capture_through_aux_vision(cap, summary)
            if routed is not None:
                return routed
            # 辅助路由被请求但失败了（视觉节点宕机、辅助调用抛异常、分析
            # 为空等）。请求路由意味着主模型可能无法消费图片；此时落入
            # 多模态封装可能会因 provider 错误而破坏截图。改为降级到
            # AX/SOM 文本载荷，这样在视觉不可用时元素索引仍然可用。
            summary_lines.append(
                "  (vision unavailable: the auxiliary vision model could not "
                "be reached; screenshot omitted. Element-index actions still "
                "work — drive via the element list above.)"
            )
            if truncated_elements:
                summary_lines.append(
                    f"  (response truncated to {len(visible_elements)} of "
                    f"{total_elements} elements; raise max_elements or pass "
                    "app= to narrow)"
                )
            payload = {
                "mode": cap.mode,
                "width": response_width,
                "height": response_height,
                "app": cap.app,
                "window_title": cap.window_title,
                "elements": [_element_to_dict(e) for e in visible_elements],
                "total_elements": total_elements,
                "summary": "\n".join(summary_lines),
                "vision_unavailable": True,
            }
            if truncated_elements:
                payload["truncated_elements"] = truncated_elements
            return json.dumps(payload)

        # 优先使用 cua-driver 为其图片分片附带的显式 MIME 类型
        #（NousResearch/hermes-agent#47072 的 Surface 7 —— trycua/cua#1961
        # 让 `mimeType` 成为每个 MCP image-part 响应的一部分）。对于不带
        # 该字段的旧 cua-driver 构建，回退到 base64 前缀嗅探。JPEG base64
        # 以 /9j/ 开头；PNG 以 iVBOR 开头。
        _mime = cap.image_mime_type
        if not _mime:
            _b64_prefix = cap.png_b64[:8]
            _mime = "image/jpeg" if _b64_prefix.startswith("/9j/") else "image/png"
        # 多模态响应携带的是截图，而不是 AX elements 数组，因此"响应已
        # 截断为 M 个中的 N 个元素"的提示在这里是不准确的——在此分支跳过。
        return {
            "_multimodal": True,
            "content": [
                {"type": "text", "text": summary},
                {"type": "image_url",
                 "image_url": {"url": f"data:{_mime};base64,{cap.png_b64}"}},
            ],
            "text_summary": summary,
            "meta": {"mode": cap.mode, "width": response_width, "height": response_height,
                     "elements": total_elements, "png_bytes": cap.png_bytes_len},
        }
    # 仅 AX（或图片缺失时的回退）：文本路径实际上携带 `elements` 数组，
    # 因此截断提示适用于此处。
    if truncated_elements:
        summary_lines.append(
            f"  (response truncated to {len(visible_elements)} of {total_elements} elements; "
            f"raise max_elements or pass app= to narrow)"
        )
    summary = "\n".join(summary_lines)
    payload: Dict[str, Any] = {
        "mode": cap.mode,
        "width": response_width,
        "height": response_height,
        "app": cap.app,
        "window_title": cap.window_title,
        "elements": [_element_to_dict(e) for e in visible_elements],
        "total_elements": total_elements,
        "summary": summary,
    }
    if truncated_elements:
        payload["truncated_elements"] = truncated_elements
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# 截图的 auxiliary.vision 路由（#24015）
# ---------------------------------------------------------------------------

# 交给辅助视觉模型的图片最长边。全分辨率桌面截图 tokenize 严重，可能
# 溢出小型本地模型的上下文窗口；约 1456px 既能保持 SOM 徽标可读，又能
# 降低每次截图的视觉延迟。
_MAX_VISION_DIM = 1456


def _shrink_capture_for_vision(raw: bytes, ext: str,
                               max_dim: int = _MAX_VISION_DIM) -> bytes:
    """对编码后的图片字节做缩放，使最长边 <= max_dim。

    当图片已经符合尺寸，或 Pillow 不可用/失败时，原样返回原始字节——
    不会比缩放前的行为更差。
    """
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(raw))
        if max(img.size) <= max_dim:
            return raw
        img.thumbnail((max_dim, max_dim))
        out = BytesIO()
        img.save(out, format="JPEG" if ext == ".jpg" else "PNG")
        return out.getvalue()
    except Exception as exc:
        logger.debug("computer_use: vision downscale skipped: %s", exc)
        return raw

def _should_route_through_aux_vision() -> bool:
    """当 ``_capture_response`` 应把 PNG 交给辅助视觉时返回 True。

    读取当前活跃的主 provider/model 和已加载的配置，并询问路由辅助函数。
    任何失败（配置导入、运行时覆盖缺失等）都返回 False，从而继续返回
    现有的多模态封装——在路由决策上采取失败放行（fail open），这样一
    个损坏的配置永远不会为具备视觉能力的主模型静默丢弃截图。
    """
    try:
        from agent.auxiliary_client import _read_main_model, _read_main_provider
        from hermes_cli.config import load_config
        from tools.computer_use.vision_routing import (
            should_route_capture_to_aux_vision,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("computer_use: aux-vision routing import failed: %s", exc)
        return False
    try:
        provider = _read_main_provider()
        model = _read_main_model()
        cfg = load_config()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("computer_use: aux-vision routing config read failed: %s", exc)
        return False
    try:
        return bool(should_route_capture_to_aux_vision(provider, model, cfg))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("computer_use: aux-vision routing decision failed: %s", exc)
        return False


def _route_capture_through_aux_vision(
    cap: CaptureResult,
    summary: str,
) -> Optional[str]:
    """通过 ``vision_analyze`` 预分析截取的 PNG 并返回一个文本结果。

    截取的 base64 PNG 会被物化到 ``$HERMES_HOME/cache/vision/``，并以一个
    通用的描述提示交给 ``vision_analyze_tool``。得到的文本描述会被合并进
    现有的 AX/SOM 摘要，使主模型收到一个单一文本载荷，其中既提到每一个
    可交互元素，又包含截图外观的描述。

    返回：
      成功时返回一个 JSON 编码的文本响应。
      失败时返回 ``None``（调用方回退到多模态封装）。
    """
    if not cap.png_b64:
        return None
    try:
        import base64 as _base64
        import os as _os
        import uuid as _uuid

        from hermes_constants import get_hermes_dir
        from model_tools import _run_async
        from tools.vision_tools import vision_analyze_tool
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("computer_use: aux-vision import failed: %s", exc)
        return None

    temp_image_path = None
    try:
        try:
            raw = _base64.b64decode(cap.png_b64, validate=False)
        except Exception as exc:
            logger.debug("computer_use: failed to decode capture base64: %s", exc)
            return None

        # 选择一个与磁盘字节匹配的扩展名，以便 vision_analyze 的 MIME
        # 嗅探返回正确的 content-type。
        # Surface 7：优先使用 cua-driver 提供的显式 MIME 类型。
        _mime_for_ext = cap.image_mime_type or ""
        if _mime_for_ext == "image/jpeg" or (not _mime_for_ext and cap.png_b64[:8].startswith("/9j/")):
            ext = ".jpg"
        else:
            ext = ".png"
        cache_dir = get_hermes_dir("cache/vision", "temp_vision_images")
        cache_dir.mkdir(parents=True, exist_ok=True)
        temp_image_path = cache_dir / f"computer_use_{_uuid.uuid4().hex}{ext}"
        raw = _shrink_capture_for_vision(raw, ext)
        temp_image_path.write_bytes(raw)

        prompt = (
            "Describe what is visible in this desktop application screenshot in "
            "concise but specific terms. Mention the app name and window "
            "title if visible, the overall layout, any labelled buttons, "
            "menus or text fields, and any prominent text content the user "
            "would need to know about. Do not invent details that are not "
            "actually visible.\n\n"
            f"AX/SOM index for cross-reference:\n{summary}"
        )

        result_json = _run_async(
            vision_analyze_tool(str(temp_image_path), prompt)
        )
    except Exception as exc:
        logger.warning(
            "computer_use: auxiliary.vision pre-analysis failed (%s); "
            "returning to caller without aux analysis",
            exc,
        )
        return None
    finally:
        if temp_image_path is not None:
            try:
                _os.unlink(str(temp_image_path))
            except Exception:
                pass

    analysis_text = ""
    if isinstance(result_json, str):
        try:
            parsed = json.loads(result_json)
            if isinstance(parsed, dict):
                analysis_text = str(parsed.get("analysis") or "").strip()
        except (TypeError, json.JSONDecodeError):
            analysis_text = result_json.strip()

    if not analysis_text:
        return None

    return json.dumps({
        "mode": cap.mode,
        "width": cap.width,
        "height": cap.height,
        "app": cap.app,
        "window_title": cap.window_title,
        "elements": [_element_to_dict(e) for e in cap.elements],
        "summary": summary,
        "vision_analysis": analysis_text,
        "vision_analysis_routed_via": "auxiliary.vision",
    })


def _maybe_follow_capture(
    backend: ComputerUseBackend, res: ActionResult, do_capture: bool,
) -> Any:
    if not do_capture:
        return _text_response(res)
    # 当动作本身失败时跳过后续截图：在失败后展示一张看起来正常的截图
    # 会误导模型以为动作成功了。改为返回错误文本。
    if not res.ok:
        return _text_response(res)
    try:
        # 保留前一次 capture/focus_app 建立的应用上下文，使 capture_after=True
        # 重新截取的是同一个应用，而不是最前窗口（如果动作导致了焦点切换，
        # 最前窗口可能已经变了）。
        last_app = getattr(backend, "_last_app", None)
        cap = backend.capture(mode="som", app=last_app)
    except Exception as e:
        logger.warning("follow-up capture failed: %s", e)
        return _text_response(res)
    # 将动作摘要与截图合并。
    resp = _capture_response(cap)
    if isinstance(resp, dict) and resp.get("_multimodal"):
        prefix = f"[{res.action}] ok={res.ok}" + (f" — {res.message}" if res.message else "")
        resp["content"][0]["text"] = prefix + "\n\n" + resp["content"][0]["text"]
        resp["text_summary"] = prefix + "\n\n" + resp["text_summary"]
        return resp
    # 回退：动作 + 文本截图合并。
    try:
        data = json.loads(resp)
    except (TypeError, json.JSONDecodeError):
        data = {"capture": resp}
    data["action"] = res.action
    data["ok"] = res.ok
    if res.message:
        data["message"] = res.message
    return json.dumps(data)


def _format_elements(elements: List[UIElement], max_lines: int = 40) -> List[str]:
    out: List[str] = []
    for e in elements[:max_lines]:
        label = e.label.replace("\n", " ")[:60]
        out.append(f"  #{e.index} {e.role} {label!r} @ {e.bounds}"
                   + (f" [{e.app}]" if e.app else ""))
    if len(elements) > max_lines:
        out.append(f"  ... +{len(elements) - max_lines} more (call capture with app= to narrow)")
    return out


def _element_to_dict(e: UIElement) -> Dict[str, Any]:
    return {
        "index": e.index,
        "role": e.role,
        "label": e.label,
        "bounds": list(e.bounds),
        "app": e.app,
    }


# ---------------------------------------------------------------------------
# 可用性检查（供工具注册表的 check_fn 使用）
# ---------------------------------------------------------------------------

def check_computer_use_requirements() -> bool:
    """当且仅当 computer_use 可以在当前主机上运行时返回 True。

    条件：macOS、Windows 或 Linux + 已安装 cua-driver 二进制（或通过
    环境变量覆盖）。cua-driver 在三者上都能运行；Linux 路径目前是
    有头/X11（Wayland 通过 XWayland），纯 Wayland 的进展在上游跟进中。
    如果 Linux 用户的会话不完整（例如未设置 DISPLAY），他们会通过
    `hermes computer-use doctor` 看到具体的阻塞检查。
    """
    if sys.platform not in ("darwin", "win32", "linux"):
        return False
    from tools.computer_use.cua_backend import cua_driver_binary_available
    return cua_driver_binary_available()


def get_computer_use_schema() -> Dict[str, Any]:
    from tools.computer_use.schema import COMPUTER_USE_SCHEMA
    return COMPUTER_USE_SCHEMA
