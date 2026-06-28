"""Provider 模块注册表。

Provider profile 可以存放在两个位置：

1. 内置插件：``plugins/model-providers/<name>/``（随 hermes-agent 一起发布）
2. 用户插件：``$HERMES_HOME/plugins/model-providers/<name>/``

每个插件目录包含：
  - ``__init__.py`` — 导入时调用 ``register_provider(profile)``
  - ``plugin.yaml`` — 清单文件（name、kind: model-provider、version、description）

发现过程是延迟执行的：首次调用 ``get_provider_profile()`` 或
``list_providers()`` 时会扫描上述两个位置并导入所有插件。当名称冲突时，
用户插件覆盖内置插件（后来者胜出），因此第三方无需修改仓库代码，
即可猴子补丁或替换任何内置 profile。

为了向后兼容，``providers/*.py`` 文件（``base.py`` 和 ``__init__.py``
除外）仍然通过 ``pkgutil.iter_modules`` 被发现。这使得外部用户可以在
可编辑安装中直接放入单文件 profile，而无需插件目录结构。新 profile
应优先使用插件布局。

用法::

    from providers import get_provider_profile
    profile = get_provider_profile("nvidia")   # ProviderProfile 或 None
    profile = get_provider_profile("kimi")     # 检查名称 + 别名
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path

from providers.base import OMIT_TEMPERATURE, ProviderProfile  # noqa: F401

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, ProviderProfile] = {}
_ALIASES: dict[str, str] = {}
_discovered = False

# 仓库根目录下的 ``plugins/model-providers/`` — 在发现阶段填充。
_BUNDLED_PLUGINS_DIR = (
    Path(__file__).resolve().parent.parent / "plugins" / "model-providers"
)


def register_provider(profile: ProviderProfile) -> None:
    """通过名称和别名注册 provider profile。

    相同名称的后续注册会替换之前的注册 — 因此 ``$HERMES_HOME/plugins/model-providers/``
    下的用户插件可以覆盖内置 profile，而无需修改仓库代码。
    """
    _REGISTRY[profile.name] = profile
    for alias in profile.aliases:
        _ALIASES[alias] = profile.name


def get_provider_profile(name: str) -> ProviderProfile | None:
    """通过名称或别名查找 provider profile。

    如果 provider 没有 profile 则返回 None（回退到通用配置）。
    """
    if not _discovered:
        _discover_providers()
    canonical = _ALIASES.get(name, name)
    return _REGISTRY.get(canonical)


def list_providers() -> list[ProviderProfile]:
    """返回所有已注册的 provider profile（每个规范名称一个）。"""
    if not _discovered:
        _discover_providers()
    # 去重：_REGISTRY 包含规范名称；_ALIASES 指向相同对象
    seen: set[int] = set()
    result: list[ProviderProfile] = []
    for profile in _REGISTRY.values():
        pid = id(profile)
        if pid not in seen:
            seen.add(pid)
            result.append(profile)
    return result


def _user_plugins_dir() -> Path | None:
    """如果 ``$HERMES_HOME/plugins/model-providers/`` 存在则返回该路径。"""
    try:
        from hermes_constants import get_hermes_home

        d = get_hermes_home() / "plugins" / "model-providers"
        return d if d.is_dir() else None
    except Exception:
        return None


def _import_plugin_dir(plugin_dir: Path, source: str) -> None:
    """导入单个插件目录以使其自注册。

    ``source`` 为 "bundled" 或 "user"，仅用于日志消息。
    """
    init_file = plugin_dir / "__init__.py"
    if not init_file.exists():
        return

    # 为内置插件提供稳定的导入路径（``plugins.model_providers.<name>``），
    # 以便插件内的相对导入能够正常工作。用户插件通过
    # ``importlib.util.spec_from_file_location`` 加载，使用唯一的模块名，
    # 这样多个 HERMES_HOME profile 不会互相别名冲突。
    safe_name = plugin_dir.name.replace("-", "_")
    if source == "bundled":
        module_name = f"plugins.model_providers.{safe_name}"
    else:
        module_name = f"_hermes_user_provider_{safe_name}"

    if module_name in sys.modules:
        return  # 已导入

    try:
        spec = importlib.util.spec_from_file_location(
            module_name, init_file, submodule_search_locations=[str(plugin_dir)]
        )
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except Exception as exc:
        logger.warning(
            "Failed to load %s provider plugin %s: %s", source, plugin_dir.name, exc
        )
        sys.modules.pop(module_name, None)


def _discover_providers() -> None:
    """通过导入每个 provider 插件来填充注册表。

    顺序：
      1. 内置插件位于 ``<repo>/plugins/model-providers/<name>/``
      2. 用户插件位于 ``$HERMES_HOME/plugins/model-providers/<name>/``
      3. 旧版单文件模块位于 ``providers/<name>.py``（向后兼容）

    每个步骤导入其插件，插件在模块级别调用 ``register_provider()``。
    名称冲突时后面的步骤胜出。
    """
    global _discovered
    if _discovered:
        return
    _discovered = True

    # 1. 内置插件 — 随 hermes-agent 一起发布。
    if _BUNDLED_PLUGINS_DIR.is_dir():
        for child in sorted(_BUNDLED_PLUGINS_DIR.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            _import_plugin_dir(child, "bundled")

    # 2. 用户插件 — 位于 $HERMES_HOME/plugins/model-providers/<name>/。
    #    这些插件可以覆盖任何同名的内置 profile（在 register_provider() 中
    #    后来者胜出）。
    user_dir = _user_plugins_dir()
    if user_dir is not None:
        for child in sorted(user_dir.iterdir()):
            if not child.is_dir() or child.name.startswith(("_", ".")):
                continue
            _import_plugin_dir(child, "user")

    # 3. 旧版单文件 profile 位于 providers/<name>.py。保留用于
    #    向后兼容 — 如果有人在可编辑安装中放入 ``providers/foo.py``，
    #    无需插件布局也能正常工作。
    try:
        import pkgutil

        import providers as _pkg

        for _importer, modname, _ispkg in pkgutil.iter_modules(_pkg.__path__):
            if modname.startswith("_") or modname == "base":
                continue
            try:
                importlib.import_module(f"providers.{modname}")
            except ImportError as exc:
                logger.warning(
                    "Failed to import legacy provider module %s: %s", modname, exc
                )
    except Exception:
        pass
