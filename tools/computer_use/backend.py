"""computer use 的抽象后端接口。

任何实现（基于 MCP 的 cua-driver、pyautogui、noop、未来的 Linux/Windows
实现）都必须返回下文描述的形状。所有方法均为同步；如需异步，由后端实现
内部自行处理。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class UIElement:
    """当前屏幕上的一个可交互元素。"""

    index: int                       # 从 1 开始的 SOM 索引
    role: str                        # AX 角色（AXButton、AXTextField……）
    label: str = ""                  # AXTitle / AXDescription / AXValue 片段
    bounds: Tuple[int, int, int, int] = (0, 0, 0, 0)  # x, y, w, h（逻辑像素）
    app: str = ""                    # 所属的 bundle ID 或应用名
    pid: int = 0                     # 所属进程的 PID
    window_id: int = 0               # SkyLight / CG 窗口 ID
    attributes: Dict[str, Any] = field(default_factory=dict)
    # 来自 cua-driver 的、针对单个快照的不透明元素句柄
    # （trycua/cua#1961 —— NousResearch/hermes-agent#47072 的 Surface 6）。
    # 设置后，下游调用可在传 `index` 的同时带上它，用于显式的过期检测：
    # 过期的 token 会从 cua-driver 返回错误，而不是静默地重新解析到另一个
    # 元素。对于早于 #1961、不携带该字段的驱动，此处为 None。
    element_token: Optional[str] = None

    def center(self) -> Tuple[int, int]:
        x, y, w, h = self.bounds
        return x + w // 2, y + h // 2


@dataclass
class CaptureResult:
    """一次屏幕捕获调用的结果。

    根据捕获模式，png_b64 / elements 至少有一个会被填充：
      * mode="vision" → 仅 png_b64
      * mode="ax"     → 仅 elements
      * mode="som"    → 两者皆有（默认）：PNG 已经由后端画上了编号覆盖层，
                        且 `elements` 持有对应的 index → 元素映射。
    """

    mode: str
    width: int                      # 截图宽度（逻辑像素，Anthropic 缩放前）
    height: int
    png_b64: Optional[str] = None
    elements: List[UIElement] = field(default_factory=list)
    # 可选：这些元素被捕获时所针对的目标应用/窗口。
    app: str = ""
    window_title: str = ""
    # 我们发给 Anthropic 的原始字节数，用于估算 token。
    png_bytes_len: int = 0
    # 后端提供时，`png_b64` 的显式 MIME 类型
    # （自 trycua/cua#1961 起，cua-driver-rs 在每个图片部分上都会输出
    # `mimeType` —— NousResearch/hermes-agent#47072 的 Surface 7）。
    # 为 None 时，下游消费者会回退到 base64 前缀嗅探，以兼容旧驱动。
    image_mime_type: Optional[str] = None


@dataclass
class ActionResult:
    """任意动作（click / type / scroll / drag / key / wait）的结果。"""

    ok: bool
    action: str
    message: str = ""                # 人类可读的摘要
    # 可选的尾随截图 —— 当调用方要求动作后捕获，或后端总是返回截图时设置。
    capture: Optional[CaptureResult] = None
    # 用于调试 / 遥测的任意额外字段。
    meta: Dict[str, Any] = field(default_factory=dict)


class ComputerUseBackend(ABC):
    """生命周期：首次使用前调用 `start()`，关闭时调用 `stop()`。"""

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def is_available(self) -> bool:
        """当后端在当前主机上立即可用时返回 True。

        供 check_fn 门禁与安装后向导使用。
        """

    # ── 捕获 ─────────────────────────────────────────────────────
    @abstractmethod
    def capture(self, mode: str = "som", app: Optional[str] = None) -> CaptureResult: ...

    # ── 指针动作 ─────────────────────────────────────────────────
    @abstractmethod
    def click(
        self,
        *,
        element: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",           # left | right | middle
        click_count: int = 1,
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult: ...

    @abstractmethod
    def drag(
        self,
        *,
        from_element: Optional[int] = None,
        to_element: Optional[int] = None,
        from_xy: Optional[Tuple[int, int]] = None,
        to_xy: Optional[Tuple[int, int]] = None,
        button: str = "left",
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult: ...

    @abstractmethod
    def scroll(
        self,
        *,
        direction: str,                 # up | down | left | right
        amount: int = 3,                # 滚轮刻度
        element: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult: ...

    # ── 键盘 ────────────────────────────────────────────────────
    @abstractmethod
    def type_text(self, text: str) -> ActionResult: ...

    @abstractmethod
    def key(self, keys: str) -> ActionResult:
        """发送一个组合键，例如 'cmd+s'、'ctrl+alt+t'、'return'。"""

    # ── 自省 ───────────────────────────────────────────────────
    @abstractmethod
    def list_apps(self) -> List[Dict[str, Any]]:
        """返回正在运行的应用，包含 bundle ID、PID、窗口数量。"""

    @abstractmethod
    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        """把输入路由到 `app`（按名称或 bundle ID）。默认：聚焦但不前置。"""

    # ── 原生值修改 ────────────────────────────────────────────────
    @abstractmethod
    def set_value(self, value: str, element: Optional[int] = None) -> ActionResult:
        """在元素上设置一个原生值（例如 AXPopUpButton 的选择）。

        `element` 是先前某次捕获调用返回的、从 1 开始的 SOM 索引。
        """

    # ── 计时 ──────────────────────────────────────────────────────
    def wait(self, seconds: float) -> ActionResult:
        """默认实现：time.sleep。"""
        import time
        time.sleep(max(0.0, min(seconds, 30.0)))
        return ActionResult(ok=True, action="wait", message=f"waited {seconds:.2f}s")
