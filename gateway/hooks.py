"""
事件钩子（Event Hook）系统

一个轻量级事件驱动系统，在关键生命周期点触发 handler。钩子从
~/.hermes/hooks/ 目录中发现，每个钩子目录包含：
  - HOOK.yaml  （元数据：name、description、events 列表）
  - handler.py （Python handler，包含 async def handle(event_type, context)）

事件（Events）：
  - gateway:startup     -- gateway 进程启动
  - session:start       -- 新会话创建（一个新会话的第一条消息）
  - session:end         -- 会话结束（用户执行了 /new 或 /reset）
  - session:reset       -- 会话重置完成（创建了新的会话条目）
  - agent:start         -- agent 开始处理一条消息
  - agent:step          -- tool-calling 循环中的每一个 turn
  - agent:end           -- agent 完成处理
  - command:*           -- 任意斜杠命令被执行（通配匹配）

钩子中出现的错误会被捕获并记录，但绝不会阻塞主管线。

传给 ``agent:start`` / ``agent:end`` handler 的 context dict：
  platform     -- 来源平台名（例如 "telegram"、"matrix"、"slack"）
  user_id      -- 发送者的平台用户 id
  chat_id      -- 平台聊天 id（群组/DM 标识符）
  thread_id    -- Telegram forum-topic id / thread root id（字符串；不在
                  thread / topic 中时为空）
  chat_type    -- "dm" | "group" | "forum"（未知时为空）
  session_id   -- Hermes 会话 id
  message      -- 入站消息文本（截断到 500 字符）

``agent:end`` 额外包含：
  response     -- agent 响应文本（截断到 500 字符）

向同一个 Telegram forum-topic 投递 follow-up 的 handler，当
``chat_type == "forum"`` 且 ``thread_id`` 非空时，应当带上
``message_thread_id=int(thread_id)``。
"""

import asyncio
import importlib.util
import sys
from typing import Any, Callable, Dict, List, Optional

import yaml

from hermes_cli.config import get_hermes_home


HOOKS_DIR = get_hermes_home() / "hooks"


class HookRegistry:
    """
    发现、加载并触发事件钩子。

    用法（Usage）：
        registry = HookRegistry()
        registry.discover_and_load()
        await registry.emit("agent:start", {"platform": "telegram", ...})
    """

    def __init__(self):
        # event_type -> [handler_fn, ...]
        self._handlers: Dict[str, List[Callable]] = {}
        self._loaded_hooks: List[dict] = []  # 用于列表展示的元数据

    @property
    def loaded_hooks(self) -> List[dict]:
        """返回所有已加载钩子的元数据。"""
        return list(self._loaded_hooks)

    def _register_builtin_hooks(self) -> None:
        """注册总是处于激活状态的内置钩子。

        目前为空——没有随产品发布的内置钩子。这里保留为扩展点，以便将来
        的常驻 gateway 钩子能直接插入，而无需重新改写 discover_and_load()。
        """
        return

    def discover_and_load(self) -> None:
        """
        扫描钩子目录中的各钩子目录，并加载它们的 handler。

        同时也会注册总是处于激活状态的内置钩子。

        每个钩子目录必须包含：
          - HOOK.yaml，至少包含 'name' 和 'events' 键
          - handler.py，包含一个顶层 'handle' 函数（同步或异步）
        """
        self._register_builtin_hooks()

        if not HOOKS_DIR.exists():
            return

        for hook_dir in sorted(HOOKS_DIR.iterdir()):
            if not hook_dir.is_dir():
                continue

            manifest_path = hook_dir / "HOOK.yaml"
            handler_path = hook_dir / "handler.py"

            if not manifest_path.exists() or not handler_path.exists():
                continue

            try:
                manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
                if not manifest or not isinstance(manifest, dict):
                    print(f"[hooks] Skipping {hook_dir.name}: invalid HOOK.yaml", flush=True)
                    continue

                hook_name = manifest.get("name", hook_dir.name)
                events = manifest.get("events", [])
                if not events:
                    print(f"[hooks] Skipping {hook_name}: no events declared", flush=True)
                    continue

                # 动态加载 handler 模块。
                # 在 exec_module 之前注册到 sys.modules 中，这样 Pydantic /
                # dataclasses / typing 内省就能解析前向引用（这些引用由
                # handler 中的 `from __future__ import annotations` 触发）。
                # 否则，一个为 webhook/event payload 声明了 Pydantic
                # BaseModel 的 handler 在首次分发时会以
                # "TypeAdapter ... is not fully defined" 失败。
                module_name = f"hermes_hook_{hook_name}"
                spec = importlib.util.spec_from_file_location(
                    module_name, handler_path
                )
                if spec is None or spec.loader is None:
                    print(f"[hooks] Skipping {hook_name}: could not load handler.py", flush=True)
                    continue

                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                try:
                    spec.loader.exec_module(module)
                except Exception:
                    sys.modules.pop(module_name, None)
                    raise

                handle_fn = getattr(module, "handle", None)
                if handle_fn is None:
                    print(f"[hooks] Skipping {hook_name}: no 'handle' function found", flush=True)
                    continue

                # 为每个声明的事件注册 handler
                for event in events:
                    self._handlers.setdefault(event, []).append(handle_fn)

                self._loaded_hooks.append({
                    "name": hook_name,
                    "description": manifest.get("description", ""),
                    "events": events,
                    "path": str(hook_dir),
                })

                print(f"[hooks] Loaded hook '{hook_name}' for events: {events}", flush=True)

            except Exception as e:
                print(f"[hooks] Error loading hook {hook_dir.name}: {e}", flush=True)

    def _resolve_handlers(self, event_type: str) -> List[Callable]:
        """返回应当为 ``event_type`` 触发的所有 handler。

        精确匹配先触发，随后是通配匹配（例如 ``command:*`` 会匹配
        ``command:reset``）。
        """
        handlers = list(self._handlers.get(event_type, []))
        if ":" in event_type:
            base = event_type.split(":")[0]
            wildcard_key = f"{base}:*"
            handlers.extend(self._handlers.get(wildcard_key, []))
        return handlers

    async def emit(self, event_type: str, context: Optional[Dict[str, Any]] = None) -> None:
        """
        触发为某个事件注册的所有 handler，丢弃返回值。

        支持通配匹配：注册到 "command:*" 的 handler 会对任意
        "command:..." 事件触发。而注册到像 "agent" 这种基础类型的 handler
        不会对 "agent:start" 触发——只有精确匹配和显式通配才会触发。

        参数（Args）：
            event_type: 事件标识符（例如 "agent:start"）。
            context:    可选的 dict，携带事件特定数据。
        """
        if context is None:
            context = {}

        for fn in self._resolve_handlers(event_type):
            try:
                result = fn(event_type, context)
                # 同时支持同步和异步 handler
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                print(f"[hooks] Error in handler for '{event_type}': {e}", flush=True)

    async def emit_collect(
        self,
        event_type: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> List[Any]:
        """触发 handler，并按顺序返回其中非 None 的返回值。

        类似 :meth:`emit`，但会捕获每个 handler 的返回值。用于决策式钩子
        （例如 ``command:<name>`` 策略，希望在常规分发之前对命令做
        允许/拒绝/改写）。

        单个 handler 抛出的异常会被记录，但不会中断其余 handler 的执行。
        """
        if context is None:
            context = {}

        results: List[Any] = []
        for fn in self._resolve_handlers(event_type):
            try:
                result = fn(event_type, context)
                if asyncio.iscoroutine(result):
                    result = await result
                if result is not None:
                    results.append(result)
            except Exception as e:
                print(f"[hooks] Error in handler for '{event_type}': {e}", flush=True)
        return results
