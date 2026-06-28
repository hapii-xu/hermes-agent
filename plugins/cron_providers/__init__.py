"""Cron 调度器 provider 插件发现。

扫描两个目录以查找 cron 调度器 provider 插件：

1. 内置 provider：``plugins/cron_providers/<name>/``（随 hermes-agent 发布）
2. 用户安装的 provider：``$HERMES_HOME/plugins/<name>/``

每个子目录必须包含 ``__init__.py``，其中有一个实现
``CronScheduler`` ABC（``cron/scheduler_provider.py``）的类。发生名称冲突时，
内置 provider 优先。

这是 ``plugins/memory/__init__.py`` 的近乎逐字克隆 — 相同的
发现/加载机制，重定向到 ``CronScheduler``。内置
``InProcessCronScheduler`` 不在此处发现：它是核心（位于
``cron/scheduler_provider.py``），因此永远不会被意外移除。
只有非默认 provider（例如 "chronos"）位于此目录下。

同一时间只能激活一个 provider，通过 config.yaml 中的 ``cron.provider``
选择（空 = 内置）。参见 ``cron.scheduler_provider.resolve_cron_scheduler``。

用法:
    from plugins.cron_providers import discover_cron_schedulers, load_cron_scheduler

    available = discover_cron_schedulers()   # [(name, desc, available), ...]
    provider = load_cron_scheduler("chronos")  # CronScheduler 实例
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import logging
import sys
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_CRON_PLUGINS_DIR = Path(__file__).parent

# 用户安装 provider 的合成父包，避免与 sys.modules 中的内置 provider 冲突。
_USER_NAMESPACE = "_hermes_user_cron"


def _register_synthetic_package(name: str, search_locations: List[str]) -> None:
    """在 sys.modules 中注册一个空的包外壳。

    用户安装的 provider 以 ``_hermes_user_cron.<name>`` 方式导入，这是一个
    点分路径，其父包在磁盘上不存在。除非这些父包存在于
    ``sys.modules`` 中，否则插件内的任何相对导入
    （``from . import config``）都会失败，报错
    ``ModuleNotFoundError: No module named '_hermes_user_cron'`` — 这也是加载器
    已为内置 provider 注册 ``plugins`` 和 ``plugins.cron_providers`` 的原因。
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
    """返回 ``$HERMES_HOME/plugins/``，若不可用则返回 None。"""
    try:
        from hermes_constants import get_hermes_home
        d = get_hermes_home() / "plugins"
        return d if d.is_dir() else None
    except Exception:
        return None


def _is_cron_provider_dir(path: Path) -> bool:
    """启发式判断：*path* 是否像一个 cron 调度器 provider 插件？

    检查 ``__init__.py`` 源码中是否包含 ``register_cron_scheduler`` 或 ``CronScheduler``。
    低成本文本扫描 — 无需导入。
    """
    init_file = path / "__init__.py"
    if not init_file.exists():
        return False
    try:
        source = init_file.read_text(errors="replace")[:8192]
        return "register_cron_scheduler" in source or "CronScheduler" in source
    except Exception:
        return False


def _iter_provider_dirs() -> List[Tuple[str, Path]]:
    """生成所有已发现 provider 目录的 ``(name, path)``。

    先扫描内置，再扫描用户安装。发生名称冲突时内置优先
    （通过 ``seen`` 集合实现先见者赢）。
    """
    seen: set = set()
    dirs: List[Tuple[str, Path]] = []

    # 1. 内置 provider（plugins/cron_providers/<name>/）
    if _CRON_PLUGINS_DIR.is_dir():
        for child in sorted(_CRON_PLUGINS_DIR.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            if not (child / "__init__.py").exists():
                continue
            seen.add(child.name)
            dirs.append((child.name, child))

    # 2. 用户安装的 provider（$HERMES_HOME/plugins/<name>/）
    user_dir = _get_user_plugins_dir()
    if user_dir:
        for child in sorted(user_dir.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            if child.name in seen:
                continue  # 内置优先
            if not _is_cron_provider_dir(child):
                continue  # 跳过非 cron 插件
            dirs.append((child.name, child))

    return dirs


def find_provider_dir(name: str) -> Optional[Path]:
    """将 provider 名称解析为其目录路径。

    先检查内置，再检查用户安装。
    """
    # 内置
    bundled = _CRON_PLUGINS_DIR / name
    if bundled.is_dir() and (bundled / "__init__.py").exists():
        return bundled
    # 用户安装
    user_dir = _get_user_plugins_dir()
    if user_dir:
        user = user_dir / name
        if user.is_dir() and _is_cron_provider_dir(user):
            return user
    return None


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------

def discover_cron_schedulers() -> List[Tuple[str, str, bool]]:
    """扫描内置和用户安装的目录以查找可用 provider。

    返回 (name, description, is_available) 元组列表。可能为空 —
    内置 provider 是核心，不在此处发现，因此全新 checkout 且无
    内置非默认 provider 时返回 []。发生名称冲突时内置优先。
    """
    results = []

    for name, child in _iter_provider_dirs():
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
            provider = _load_provider_from_dir(child)
            if provider:
                available = provider.is_available()
            else:
                available = False
        except Exception:
            available = False

        results.append((name, desc, available))

    return results


def load_cron_scheduler(name: str) -> Optional["CronScheduler"]:  # noqa: F821
    """按名称加载并返回 CronScheduler 实例。

    同时检查内置（``plugins/cron_providers/<name>/``）和用户安装
    （``$HERMES_HOME/plugins/<name>/``）目录。发生名称冲突时内置优先。

    如果未找到 provider 或加载失败，返回 None。
    """
    provider_dir = find_provider_dir(name)
    if not provider_dir:
        logger.debug("Cron provider '%s' not found in bundled or user plugins", name)
        return None

    try:
        provider = _load_provider_from_dir(provider_dir)
        if provider:
            return provider
        logger.warning("Cron provider '%s' loaded but no provider instance found", name)
        return None
    except Exception as e:
        logger.warning("Failed to load cron provider '%s': %s", name, e)
        return None


def _load_provider_from_dir(provider_dir: Path) -> Optional["CronScheduler"]:  # noqa: F821
    """导入 provider 模块并提取 CronScheduler 实例。

    模块必须具备以下之一：
    - register(ctx) 函数（插件风格）— 我们模拟一个 ctx
    - 继承自 CronScheduler 的顶层类 — 我们对其进行实例化
    """
    name = provider_dir.name
    # 为用户安装的插件使用独立命名空间，避免与 sys.modules 中的内置 provider 冲突。
    _is_bundled = _CRON_PLUGINS_DIR in provider_dir.parents or provider_dir.parent == _CRON_PLUGINS_DIR
    module_name = f"plugins.cron_providers.{name}" if _is_bundled else f"{_USER_NAMESPACE}.{name}"
    init_file = provider_dir / "__init__.py"

    if not init_file.exists():
        return None

    # 检查是否已加载。合成包外壳没有 __file__；
    # 只复用实际从磁盘加载的模块。
    cached = sys.modules.get(module_name)
    if cached is not None and getattr(cached, "__file__", None):
        mod = cached
    else:
        # 确保父包已注册（用于相对导入）
        for parent in ("plugins", "plugins.cron_providers"):
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

        # 用户安装的插件也需要注册其合成父包，
        # 否则插件内的相对导入无法解析。
        if not _is_bundled:
            _register_synthetic_package(_USER_NAMESPACE, [])

        # 现在加载 provider 模块
        spec = importlib.util.spec_from_file_location(
            module_name, str(init_file),
            submodule_search_locations=[str(provider_dir)]
        )
        if not spec:
            return None

        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        loaded_submodules = []

        # 注册子模块以使相对导入正常工作
        # 例如，chronos 插件中的 "from ._nas_client import NasCronClient"
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
                        loaded_submodules.append((sub_name, sub_mod))
                    except Exception as e:
                        logger.debug("Failed to load submodule %s: %s", full_sub_name, e)

        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            logger.debug("Failed to exec_module %s: %s", module_name, e)
            sys.modules.pop(module_name, None)
            return None

        # 手动 importlib 加载绕过了将子模块绑定到父包的正常导入机制。
        # 恢复该结构，使后续的点分导入和 pytest monkeypatch 路径能正常解析。
        parent_name, child_name = module_name.rsplit(".", 1)
        parent_mod = sys.modules.get(parent_name)
        if parent_mod is not None:
            setattr(parent_mod, child_name, mod)
        for sub_name, sub_mod in loaded_submodules:
            setattr(mod, sub_name, sub_mod)

    # 首先尝试 register(ctx) 模式（我们的插件编写方式）
    if hasattr(mod, "register"):
        collector = _ProviderCollector()
        try:
            mod.register(collector)
            if collector.provider:
                return collector.provider
        except Exception as e:
            logger.debug("register() failed for %s: %s", name, e)

    # 回退：查找 CronScheduler 子类并实例化
    from cron.scheduler_provider import CronScheduler
    for attr_name in dir(mod):
        attr = getattr(mod, attr_name, None)
        if (isinstance(attr, type) and issubclass(attr, CronScheduler)
                and attr is not CronScheduler):
            try:
                return attr()
            except Exception:
                pass

    return None


class _ProviderCollector:
    """假的插件上下文，用于捕获 register_cron_scheduler 调用。"""

    def __init__(self):
        self.provider = None

    def register_cron_scheduler(self, provider):
        self.provider = provider

    # 对其他注册方法为空操作
    def register_tool(self, *args, **kwargs):
        pass

    def register_hook(self, *args, **kwargs):
        pass

    def register_memory_provider(self, *args, **kwargs):
        pass

    def register_cli_command(self, *args, **kwargs):
        pass
