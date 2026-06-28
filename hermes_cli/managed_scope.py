"""托管作用域 — IT 推送的、用户不可变的配置和环境变量层。

一个系统级目录(默认 ``/etc/hermes``,root 所有且用户不可写)提供 ``config.yaml`` 和 ``.env`` 值,
这些值在逐叶子键的基础上胜过用户的 ``~/.hermes/config.yaml`` 和 ``~/.hermes/.env``。

这与 ``hermes_cli.config.is_managed()`` / ``HERMES_MANAGED`` 不同,
后者是一个粗粒度的包管理器写锁(声明式发行版/formula
安装)。该锁阻止所有变更;此层注入特定的不可变值。两者独立且可共存。

v1 仅通过文件系统权限强制执行 — 参见
``docs/design/managed-scope.md`` §7。v1 以 Linux/POSIX 为先;``get_managed_dir()``
是以后添加 macOS/Windows 原生位置的唯一接口。

归属:不要在此文件中引用任何第三方产品名称。
"""
from __future__ import annotations

import copy
import logging
import os
import threading
from pathlib import Path
from typing import Dict, Optional

import yaml

logger = logging.getLogger(__name__)

# POSIX 默认值。其他平台的位置是有意的 v2 项;添加时,
# 它们只应出现在 get_managed_dir() 内部。
_DEFAULT_MANAGED_DIR = Path("/etc/hermes")

_CACHE_LOCK = threading.Lock()
# path_key -> (mtime_ns, size, parsed)
# 路径键 -> (修改时间纳秒, 大小, 解析后的值)
_CONFIG_CACHE: Dict[str, tuple] = {}
_ENV_CACHE: Dict[str, tuple] = {}


def _under_pytest() -> bool:
    """在测试套件内运行时返回 True。

    用于在测试期间忽略系统默认的 ``/etc/hermes``,这样开发者/CI 机器上的真实
    托管作用域不会将策略泄漏到测试套件中。测试
    通过显式设置 ``HERMES_MANAGED_DIR`` 来练习托管作用域,这
    仍然被遵守(下面的覆盖路径在此守卫生效之前运行)。
    """
    return "PYTEST_CURRENT_TEST" in os.environ


def get_managed_dir() -> Optional[Path]:
    """解析托管作用域目录,或在不存在作用域时返回 None。

    解析顺序(优先级从高到低):
      1. ``$HERMES_MANAGED_DIR`` — 部署/引导路径覆盖(仅限 IT;
         从不持久化到任何 .env)。仅在设置为非空值
         且目录存在时被遵守。
      2. ``/etc/hermes`` — POSIX 默认值,存在时。在 pytest 下被忽略,
         这样真实的系统托管作用域不会泄漏到测试套件中。

    任一层级的不存在目录都解析为 None(无托管作用域),
    这是常见情况,必须廉价且无副作用。
    """
    override = os.environ.get("HERMES_MANAGED_DIR", "").strip()
    if override:
        p = Path(override)
        return p if p.is_dir() else None
    if _under_pytest():
        return None
    return _DEFAULT_MANAGED_DIR if _DEFAULT_MANAGED_DIR.is_dir() else None


def invalidate_managed_cache() -> None:
    """清除缓存的托管配置/环境变量。用于测试和编辑后重新加载。"""
    with _CACHE_LOCK:
        _CONFIG_CACHE.clear()
        _ENV_CACHE.clear()


def _cached_read(path: Path, cache: Dict[str, tuple], parse):
    """共享的 (mtime_ns, size) 键控读取。返回解析值的深拷贝。

    当文件不存在或解析失败时返回 ``None``(故障开放)。
    解析失败会被大声记录 — 管理员需要知道他们的策略未被
    应用 — 但从不抛出,这样格式错误的托管文件不会阻断
    启动。
    """
    try:
        st = path.stat()
    except OSError:
        return None  # 不存在
    key = (st.st_mtime_ns, st.st_size)
    path_key = str(path)
    with _CACHE_LOCK:
        hit = cache.get(path_key)
        if hit is not None and hit[:2] == key:
            return copy.deepcopy(hit[2])
    try:
        with open(path, encoding="utf-8") as f:
            parsed = parse(f)
    except Exception as exc:  # noqa: BLE001 — 故障开放,但要大声
        logger.warning(
            "managed scope: failed to parse %s: %s — IGNORING this managed file. "
            "Admin policy from this file is NOT being applied. Fix and restart.",
            path,
            exc,
        )
        return None
    with _CACHE_LOCK:
        cache[path_key] = (key[0], key[1], copy.deepcopy(parsed))
    return parsed


def load_managed_config() -> dict:
    """解析的托管 config.yaml,或在不存在/格式错误时返回 {}(故障开放)。"""
    managed_dir = get_managed_dir()
    if managed_dir is None:
        return {}
    parsed = _cached_read(
        managed_dir / "config.yaml",
        _CONFIG_CACHE,
        lambda f: yaml.safe_load(f) or {},
    )
    return parsed if isinstance(parsed, dict) else {}


def load_managed_env() -> Dict[str, str]:
    """解析的托管 .env (KEY=VALUE),或在不存在时返回 {}(故障开放)。"""
    managed_dir = get_managed_dir()
    if managed_dir is None:
        return {}
    parsed = _cached_read(managed_dir / ".env", _ENV_CACHE, _parse_env)
    return parsed if isinstance(parsed, dict) else {}


def apply_managed_overlay(config: dict) -> dict:
    """在已构建的字典之上覆盖管理员固定的配置值。

    任何构建自己字典的配置加载器(而不是通过 hermes_cli.config.load_config)
    遵守托管作用的唯一共享方式。完全镜像 hermes_cli.config._load_config_impl
    的托管合并:

      * 仅针对 PROCESS 环境变量展开托管配置的 ``${VAR}`` 引用
        (永不是用户配置定义的引用),这样用户无法通过他们控制的 ${VAR}
        遮蔽托管字面量;
      * 规范化托管配置的根 ``model`` 键(裸 ``model: x/y``
        字符串被提升为 ``model.default``),这样它不会破坏调用者期望的字典
        形状;
      * 叶子级深度合并,托管在上层,这样托管逐叶子胜出,而
        兄弟键保持用户控制。

    故障开放:如果没有托管作用域或出现任何错误,则不变地返回 ``config`` —
    托管作用域绝不能破坏调用者的启动。变更并
    返回 ``config``(调用者传入他们拥有的字典)。
    """
    try:
        managed = load_managed_config()
        if not managed:
            return config
        # 延迟导入以避免导入循环(config 导入 managed_scope)。
        from hermes_cli.config import _deep_merge, _expand_env_vars, _normalize_root_model_keys

        managed_expanded = _normalize_root_model_keys(_expand_env_vars(managed))
        # 托管文件中的裸 ``model: x/y`` 字符串必须作为
        # ``model.default`` 合并 — 否则 _deep_merge 会将调用者的
        # ``model`` 字典替换为字符串并破坏每个 ``cfg["model"]["..."]``
        # 读取。_normalize_root_model_keys 仅在有根 provider/base_url 键
        # 需要迁移时才提升字符串,因此在这里处理裸情况
        # (匹配 cli.py 自己的字符串模型处理)。
        if isinstance(managed_expanded.get("model"), str):
            managed_expanded = dict(managed_expanded)
            managed_expanded["model"] = {"default": managed_expanded["model"]}
        return _deep_merge(config, managed_expanded)
    except Exception:  # noqa: BLE001 — 覆盖绝不能破坏调用者
        logger.warning("managed scope: failed to apply config overlay", exc_info=True)
        return config


def _parse_env(f) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("\"'")
    return out


def _flatten_keys(d: dict, prefix: str = "") -> set:
    keys: set = set()
    for k, v in d.items():
        dotted = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            keys |= _flatten_keys(v, dotted)
        else:
            keys.add(dotted)
    return keys


def managed_config_keys() -> set:
    """托管配置固定的点分叶子键(例如 {'model.default'})。"""
    return _flatten_keys(load_managed_config())


def is_key_managed(dotted_key: str) -> bool:
    """如果精确的点分配置键被托管层固定则返回 True。"""
    return dotted_key in managed_config_keys()


def is_env_managed(name: str) -> bool:
    """如果环境变量名被托管 .env 层固定则返回 True。"""
    return name in load_managed_env()
