"""Memory 提供者插件发现。

扫描两个目录以查找 memory 提供者插件：

1. 内置提供者：``plugins/memory/<name>/``（随 hermes-agent 一起发布）
2. 用户安装的提供者：``$HERMES_HOME/plugins/<name>/``

每个子目录必须包含 ``__init__.py``，其中实现了一个继承
MemoryProvider 抽象类的类。发生名称冲突时，内置提供者优先。

同一时间只能有一个提供者处于激活状态，通过 config.yaml 中的
``memory.provider`` 来选择。

用法：
    from plugins.memory import discover_memory_providers, load_memory_provider

    available = discover_memory_providers()   # [(name, desc, available), ...]
    provider = load_memory_provider("mnemosyne")  # MemoryProvider 实例
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import logging
import sys
from pathlib import Path
from typing import List, Optional, Tuple
from hermes_cli.config import cfg_get

logger = logging.getLogger(__name__)

_MEMORY_PLUGINS_DIR = Path(__file__).parent

# 用户安装提供者的合成父包，避免与 sys.modules 中的内置提供者冲突。
_USER_NAMESPACE = "_hermes_user_memory"


def _register_synthetic_package(name: str, search_locations: List[str]) -> None:
    """在 sys.modules 中注册一个空的包壳。

    用户安装的提供者以 ``_hermes_user_memory.<name>`` 的形式导入，
    这是一个点分名称，其父级在磁盘上并不存在。除非这些父级
    存在于 ``sys.modules`` 中，否则插件内的任何相对导入
    （``from . import config``）都会失败，报
    ``ModuleNotFoundError: No module named '_hermes_user_memory'`` —
    这与 loader 已经为内置提供者注册 ``plugins`` 和
    ``plugins.memory`` 的原因相同。
    """
    if name in sys.modules:
        return
    spec = importlib.machinery.ModuleSpec(name, None, is_package=True)
    spec.submodule_search_locations = search_locations
    sys.modules[name] = importlib.util.module_from_spec(spec)


# ---------------------------------------------------------------------------
# 目录辅助函数
# ---------------------------------------------------------------------------

def _get_user_plugins_dir() -> Optional[Path]:
    """返回 ``$HERMES_HOME/plugins/``，如果不可用则返回 None。"""
    try:
        from hermes_constants import get_hermes_home
        d = get_hermes_home() / "plugins"
        return d if d.is_dir() else None
    except Exception:
        return None


def _is_memory_provider_dir(path: Path) -> bool:
    """启发式检查：*path* 是否看起来像一个 memory 提供者插件？

    检查 ``__init__.py`` 源码中是否包含 ``register_memory_provider``
    或 ``MemoryProvider``。廉价的文本扫描 — 无需导入。
    """
    init_file = path / "__init__.py"
    if not init_file.exists():
        return False
    try:
        source = init_file.read_text(errors="replace")[:8192]
        return "register_memory_provider" in source or "MemoryProvider" in source
    except Exception:
        return False


def _iter_provider_dirs() -> List[Tuple[str, Path]]:
    """为所有发现的提供者目录生成 ``(name, path)``。

    先扫描内置提供者，再扫描用户安装的提供者。名称冲突时
    内置提供者优先（通过 ``seen`` 集合，先见者优先）。
    """
    seen: set = set()
    dirs: List[Tuple[str, Path]] = []

    # 1. 内置提供者 (plugins/memory/<name>/)
    if _MEMORY_PLUGINS_DIR.is_dir():
        for child in sorted(_MEMORY_PLUGINS_DIR.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            if not (child / "__init__.py").exists():
                continue
            seen.add(child.name)
            dirs.append((child.name, child))

    # 2. 用户安装的提供者 ($HERMES_HOME/plugins/<name>/)
    user_dir = _get_user_plugins_dir()
    if user_dir:
        for child in sorted(user_dir.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            if child.name in seen:
                continue  # 内置提供者优先
            if not _is_memory_provider_dir(child):
                continue  # 跳过非 memory 插件
            dirs.append((child.name, child))

    return dirs


def find_provider_dir(name: str) -> Optional[Path]:
    """将提供者名称解析为其目录。

    先检查内置提供者，再检查用户安装的提供者。
    """
    # 内置提供者
    bundled = _MEMORY_PLUGINS_DIR / name
    if bundled.is_dir() and (bundled / "__init__.py").exists():
        return bundled
    # 用户安装的提供者
    user_dir = _get_user_plugins_dir()
    if user_dir:
        user = user_dir / name
        if user.is_dir() and _is_memory_provider_dir(user):
            return user
    return None


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------

def discover_memory_providers() -> List[Tuple[str, str, bool]]:
    """扫描内置和用户安装目录中的可用提供者。

    返回 (name, description, is_available) 元组列表。
    名称冲突时内置提供者优先。
    """
    results = []

    for name, child in _iter_provider_dirs():
        # 如果可用，从 plugin.yaml 中读取描述
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
            provider = _load_provider_from_dir(child)
            if provider:
                available = provider.is_available()
            else:
                available = False
        except Exception:
            available = False

        results.append((name, desc, available))

    return results


def load_memory_provider(name: str) -> Optional["MemoryProvider"]:
    """按名称加载并返回一个 MemoryProvider 实例。

    同时检查内置（``plugins/memory/<name>/``）和用户安装
    （``$HERMES_HOME/plugins/<name>/``）目录。名称冲突时
    内置提供者优先。

    如果提供者未找到或加载失败，返回 None。
    """
    provider_dir = find_provider_dir(name)
    if not provider_dir:
        logger.debug("Memory provider '%s' not found in bundled or user plugins", name)
        return None

    try:
        provider = _load_provider_from_dir(provider_dir)
        if provider:
            return provider
        logger.warning("Memory provider '%s' loaded but no provider instance found", name)
        return None
    except Exception as e:
        logger.warning("Failed to load memory provider '%s': %s", name, e)
        return None


def _load_provider_from_dir(provider_dir: Path) -> Optional["MemoryProvider"]:
    """导入提供者模块并提取 MemoryProvider 实例。

    模块必须具备以下之一：
    - 一个 register(ctx) 函数（插件风格） — 我们会模拟一个 ctx
    - 一个继承 MemoryProvider 的顶级类 — 我们会实例化它
    """
    name = provider_dir.name
    # Use a separate namespace for user-installed plugins so they don't
    # collide with bundled providers in sys.modules.
    _is_bundled = _MEMORY_PLUGINS_DIR in provider_dir.parents or provider_dir.parent == _MEMORY_PLUGINS_DIR
    module_name = f"plugins.memory.{name}" if _is_bundled else f"{_USER_NAMESPACE}.{name}"
    init_file = provider_dir / "__init__.py"

    if not init_file.exists():
        return None

    # Check if already loaded.  A synthetic package shell registered by
    # discover_plugin_cli_commands() for relative-import support has no
    # __file__; only reuse modules that were actually loaded from disk.
    cached = sys.modules.get(module_name)
    if cached is not None and getattr(cached, "__file__", None):
        mod = cached
    else:
        # Handle relative imports within the plugin
        # First ensure the parent packages are registered
        for parent in ("plugins", "plugins.memory"):
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

        # User-installed plugins need their synthetic parent registered the
        # same way, or relative imports inside the plugin cannot resolve.
        if not _is_bundled:
            _register_synthetic_package(_USER_NAMESPACE, [])

        # Now load the provider module
        spec = importlib.util.spec_from_file_location(
            module_name, str(init_file),
            submodule_search_locations=[str(provider_dir)]
        )
        if not spec:
            return None

        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod

        # Register submodules so relative imports work
        # e.g., "from .store import MemoryStore" in holographic plugin
        for sub_file in provider_dir.glob("*.py"):
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

    # Try register(ctx) pattern first (how our plugins are written)
    if hasattr(mod, "register"):
        collector = _ProviderCollector()
        try:
            mod.register(collector)
            if collector.provider:
                return collector.provider
        except Exception as e:
            logger.debug("register() failed for %s: %s", name, e)

    # Fallback: find a MemoryProvider subclass and instantiate it
    from agent.memory_provider import MemoryProvider
    for attr_name in dir(mod):
        attr = getattr(mod, attr_name, None)
        if (isinstance(attr, type) and issubclass(attr, MemoryProvider)
                and attr is not MemoryProvider):
            try:
                return attr()
            except Exception:
                pass

    return None


class _ProviderCollector:
    """Fake plugin context that captures register_memory_provider calls."""

    def __init__(self):
        self.provider = None

    def register_memory_provider(self, provider):
        self.provider = provider

    # No-op for other registration methods
    def register_tool(self, *args, **kwargs):
        pass

    def register_hook(self, *args, **kwargs):
        pass

    def register_cli_command(self, *args, **kwargs):
        pass  # CLI registration happens via discover_plugin_cli_commands()


def _get_active_memory_provider() -> Optional[str]:
    """Read the active memory provider name from config.yaml.

    Returns the provider name (e.g. ``"honcho"``) or None if no
    external provider is configured.  Lightweight — only reads config,
    no plugin loading.
    """
    try:
        from hermes_cli.config import load_config
        config = load_config()
        return cfg_get(config, "memory", "provider") or None
    except Exception:
        return None


def discover_plugin_cli_commands() -> List[dict]:
    """Return CLI commands for the **active** memory plugin only.

    Only one memory provider can be active at a time (set via
    ``memory.provider`` in config.yaml).  This function reads that
    value and only loads CLI registration for the matching plugin.
    If no provider is active, no commands are registered.

    Looks for a ``register_cli(subparser)`` function in the active
    plugin's ``cli.py``.  Returns a list of at most one dict with
    keys: ``name``, ``help``, ``description``, ``setup_fn``,
    ``handler_fn``.

    This is a lightweight scan — it only imports ``cli.py``, not the
    full plugin module.  Safe to call during argparse setup before
    any provider is loaded.
    """
    results: List[dict] = []
    if not _MEMORY_PLUGINS_DIR.is_dir():
        return results

    active_provider = _get_active_memory_provider()
    if not active_provider:
        return results

    # Only look at the active provider's directory
    plugin_dir = find_provider_dir(active_provider)
    if not plugin_dir:
        return results

    cli_file = plugin_dir / "cli.py"
    if not cli_file.exists():
        return results

    _is_bundled = _MEMORY_PLUGINS_DIR in plugin_dir.parents or plugin_dir.parent == _MEMORY_PLUGINS_DIR
    module_name = f"plugins.memory.{active_provider}.cli" if _is_bundled else f"{_USER_NAMESPACE}.{active_provider}.cli"
    try:
        # Import the CLI module (lightweight — no SDK needed)
        if module_name in sys.modules:
            cli_mod = sys.modules[module_name]
        else:
            if not _is_bundled:
                # cli.py imports as _hermes_user_memory.<name>.cli, usually
                # before the provider itself is loaded.  Register its parent
                # packages so relative imports inside cli.py
                # ("from . import config") resolve without executing the
                # plugin's __init__.py.  The package shell has no __file__,
                # so _load_provider_from_dir() will still load the real
                # module later instead of reusing the shell.
                _register_synthetic_package(_USER_NAMESPACE, [])
                _register_synthetic_package(
                    f"{_USER_NAMESPACE}.{active_provider}", [str(plugin_dir)]
                )
            spec = importlib.util.spec_from_file_location(
                module_name, str(cli_file)
            )
            if not spec or not spec.loader:
                return results
            cli_mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = cli_mod
            spec.loader.exec_module(cli_mod)

        register_cli = getattr(cli_mod, "register_cli", None)
        if not callable(register_cli):
            return results

        # Read metadata from plugin.yaml if available
        help_text = f"Manage {active_provider} memory plugin"
        description = ""
        yaml_file = plugin_dir / "plugin.yaml"
        if yaml_file.exists():
            try:
                import yaml
                with open(yaml_file, encoding="utf-8-sig") as f:
                    meta = yaml.safe_load(f) or {}
                desc = meta.get("description", "")
                if desc:
                    help_text = desc
                    description = desc
            except Exception:
                pass

        handler_fn = getattr(cli_mod, f"{active_provider}_command", None) or \
                     getattr(cli_mod, "honcho_command", None)

        results.append({
            "name": active_provider,
            "help": help_text,
            "description": description,
            "setup_fn": register_cli,
            "handler_fn": handler_fn,
            "plugin": active_provider,
        })
    except Exception as e:
        logger.debug("Failed to scan CLI for memory plugin '%s': %s", active_provider, e)

    return results
