"""上下文引擎插件发现。

扫描 ``plugins/context_engine/<name>/`` 目录以查找上下文引擎插件。
每个子目录必须包含 ``__init__.py``，其中有一个实现 ContextEngine ABC 的类。

上下文引擎独立于通用插件系统 — 它们存在于仓库中，无需用户安装即可使用。
同一时间只能激活一个，通过 config.yaml 中的 ``context.engine`` 选择。
默认引擎为 ``"compressor"``（内置 ContextCompressor）。

用法:
    from plugins.context_engine import discover_context_engines, load_context_engine

    available = discover_context_engines()   # [(name, desc, available), ...]
    engine = load_context_engine("lcm")      # ContextEngine 实例
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_CONTEXT_ENGINE_PLUGINS_DIR = Path(__file__).parent


def discover_context_engines() -> List[Tuple[str, str, bool]]:
    """扫描 plugins/context_engine/ 以查找可用的引擎。

    返回 (name, description, is_available) 元组列表。
    不导入引擎 — 仅从 plugin.yaml 读取元数据
    并进行轻量级可用性检查。
    """
    results = []
    if not _CONTEXT_ENGINE_PLUGINS_DIR.is_dir():
        return results

    for child in sorted(_CONTEXT_ENGINE_PLUGINS_DIR.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")):
            continue
        init_file = child / "__init__.py"
        if not init_file.exists():
            continue

        # 如果 plugin.yaml 可用则读取描述
        desc = ""
        yaml_file = child / "plugin.yaml"
        if yaml_file.exists():
            try:
                import yaml
                with open(yaml_file, encoding="utf-8-sig") as f:
                    meta = yaml.safe_load(f) or {}
                desc = meta.get("description", "")
            except Exception:
                pass

        # 快速可用性检查 — 尝试加载并调用 is_available()
        available = True
        try:
            engine = _load_engine_from_dir(child)
            if engine is None:
                available = False
            elif hasattr(engine, "is_available"):
                available = engine.is_available()
        except Exception:
            available = False

        results.append((child.name, desc, available))

    return results


def load_context_engine(name: str) -> Optional["ContextEngine"]:
    """按名称加载并返回 ContextEngine 实例。

    如果未找到引擎或加载失败，返回 None。
    """
    engine_dir = _CONTEXT_ENGINE_PLUGINS_DIR / name
    if not engine_dir.is_dir():
        logger.debug("Context engine '%s' not found in %s", name, _CONTEXT_ENGINE_PLUGINS_DIR)
        return None

    try:
        engine = _load_engine_from_dir(engine_dir)
        if engine:
            return engine
        logger.warning("Context engine '%s' loaded but no engine instance found", name)
        return None
    except Exception as e:
        logger.warning("Failed to load context engine '%s': %s", name, e)
        return None


def _load_engine_from_dir(engine_dir: Path) -> Optional["ContextEngine"]:
    """导入引擎模块并提取 ContextEngine 实例。

    模块必须具备以下之一：
    - register(ctx) 函数（插件风格）— 我们模拟一个 ctx
    - 继承自 ContextEngine 的顶层类 — 我们对其进行实例化
    """
    name = engine_dir.name
    module_name = f"plugins.context_engine.{name}"
    init_file = engine_dir / "__init__.py"

    if not init_file.exists():
        return None

    # 检查是否已加载
    if module_name in sys.modules:
        mod = sys.modules[module_name]
    else:
        # 处理插件内的相对导入
        # 首先确保父包已注册
        for parent in ("plugins", "plugins.context_engine"):
            if parent not in sys.modules:
                parent_path = Path(__file__).parent
                if parent == "plugins":
                    parent_path = parent_path.parent
                parent_init = parent_path / "__init__.py"
                if parent_init.exists():
                    spec = importlib.util.spec_from_file_location(
                        parent, str(parent_init),
                        submodule_search_locations=[str(parent_path)]
                    )
                    if spec:
                        parent_mod = importlib.util.module_from_spec(spec)
                        sys.modules[parent] = parent_mod
                        try:
                            spec.loader.exec_module(parent_mod)
                        except Exception:
                            pass

        # 现在加载引擎模块
        spec = importlib.util.spec_from_file_location(
            module_name, str(init_file),
            submodule_search_locations=[str(engine_dir)]
        )
        if not spec:
            return None

        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod

        # 注册子模块以使相对导入正常工作
        for sub_file in engine_dir.glob("*.py"):
            if sub_file.name == "__init__.py":
                continue
            sub_name = sub_file.stem
            full_sub_name = f"{module_name}.{sub_name}"
            if full_sub_name not in sys.modules:
                sub_spec = importlib.util.spec_from_file_location(
                    full_sub_name, str(sub_file)
                )
                if sub_spec:
                    sub_mod = importlib.util.module_from_spec(sub_spec)
                    sys.modules[full_sub_name] = sub_mod
                    try:
                        sub_spec.loader.exec_module(sub_mod)
                    except Exception as e:
                        logger.debug("Failed to load submodule %s: %s", full_sub_name, e)

        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            logger.debug("Failed to exec_module %s: %s", module_name, e)
            sys.modules.pop(module_name, None)
            return None

    # 首先尝试 register(ctx) 模式（插件的编写方式）
    if hasattr(mod, "register"):
        collector = _EngineCollector(engine_name=name)
        try:
            mod.register(collector)
            if collector.engine:
                return collector.engine
        except Exception as e:
            logger.debug("register() failed for %s: %s", name, e)

    # 回退：查找 ContextEngine 子类并实例化
    from agent.context_engine import ContextEngine
    for attr_name in dir(mod):
        attr = getattr(mod, attr_name, None)
        if (isinstance(attr, type) and issubclass(attr, ContextEngine)
                and attr is not ContextEngine):
            try:
                return attr()
            except Exception:
                pass

    return None


class _EngineCollector:
    """假的插件上下文，用于捕获 register_context_engine 调用。

    使用标准 ``register(ctx)`` 模式的插件上下文引擎也可能调用
    ``ctx.register_command(...)`` 来暴露斜杠命令（例如 ``/lcm``）。
    将这些命令转发到全局插件命令注册表，使其行为与普通插件注册的命令相同。
    """

    def __init__(self, engine_name: str = ""):
        self.engine = None
        self._engine_name = engine_name or "context_engine"
        self._registered_commands: list[str] = []

    def register_context_engine(self, engine):
        self.engine = engine

    def register_command(
        self,
        name: str,
        handler,
        description: str = "",
        args_hint: str = "",
    ) -> None:
        """转发到全局插件命令注册表。"""
        clean = (name or "").lower().strip().lstrip("/").replace(" ", "-")
        if not clean:
            logger.warning(
                "Context engine '%s' tried to register a command with an empty name.",
                self._engine_name,
            )
            return

        # 拒绝与内置命令的冲突。
        try:
            from hermes_cli.commands import resolve_command
            if resolve_command(clean) is not None:
                logger.warning(
                    "Context engine '%s' tried to register command '/%s' which conflicts "
                    "with a built-in command. Skipping.",
                    self._engine_name, clean,
                )
                return
        except Exception:
            pass

        try:
            from hermes_cli.plugins import get_plugin_manager
            manager = get_plugin_manager()
            if clean in manager._plugin_commands:
                # 不要覆盖普通插件的命令 — 与插件系统对插件间冲突
                # 使用的相同冲突策略。
                logger.warning(
                    "Context engine '%s' tried to register command '/%s' which "
                    "is already registered by a plugin. Skipping.",
                    self._engine_name, clean,
                )
                return
            manager._plugin_commands[clean] = {
                "handler": handler,
                "description": description or "Context engine command",
                "plugin": f"context-engine:{self._engine_name}",
                "args_hint": (args_hint or "").strip(),
            }
            self._registered_commands.append(clean)
            logger.debug(
                "Context engine '%s' registered command: /%s",
                self._engine_name, clean,
            )
        except Exception as exc:
            logger.debug(
                "Context engine '%s' could not register /%s: %s",
                self._engine_name, clean, exc,
            )

    # 对其他注册方法为空操作
    def register_tool(self, *args, **kwargs):
        pass

    def register_hook(self, *args, **kwargs):
        pass

    def register_cli_command(self, *args, **kwargs):
        pass

    def register_memory_provider(self, *args, **kwargs):
        pass
