"""环境变量透传注册表。

在其 frontmatter 中声明了 ``required_environment_variables`` 的 skills
需要这些变量在沙箱执行环境（execute_code、terminal）中可用。
默认情况下，两个沙箱出于安全考虑都会从子进程环境中剥离密钥。
本模块提供一个会话级（session-scoped）白名单，使 skill 声明的变量
（以及用户配置的覆盖项）能够透传。

两个来源向白名单提供条目：

1. **Skill 声明** —— 当一个 skill 通过 ``skill_view`` 加载时，其
   ``required_environment_variables`` 会在此自动注册。
2. **用户配置** —— config.yaml 中的 ``terminal.env_passthrough`` 让用户
   可以为非 skill 的用例显式加入白名单。

``code_execution_tool.py`` 和 ``tools/environments/local.py`` 都会在
剥离某个变量之前查询 :func:`is_env_passthrough`。
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Iterable
from hermes_cli.config import cfg_get

logger = logging.getLogger(__name__)

# 应透传给沙箱的环境变量名组成的会话级集合。
# 由 ContextVar 支撑，以防止在 gateway 流水线中出现跨会话数据泄漏。
_allowed_env_vars_var: ContextVar[set[str]] = ContextVar("_allowed_env_vars")


def _get_allowed() -> set[str]:
    """获取或创建当前上下文/会话对应的允许的环境变量集合。"""
    try:
        return _allowed_env_vars_var.get()
    except LookupError:
        val: set[str] = set()
        _allowed_env_vars_var.set(val)
        return val


# 基于配置的白名单缓存（每个进程加载一次）。
_config_passthrough: frozenset[str] | None = None


def _is_hermes_provider_credential(name: str) -> bool:
    """当 ``name`` 是 Hermes 托管的 provider 凭据（API key、
    token 等），依据 ``_HERMES_PROVIDER_ENV_BLOCKLIST`` 返回 True。

    Skill 声明的 ``required_environment_variables`` frontmatter 绝不能
    覆盖此列表 —— 那正是 GHSA-rhgp-j443-p4rf 中的绕过手段：恶意 skill
    将 ``ANTHROPIC_TOKEN`` / ``OPENAI_API_KEY`` 注册为透传，
    并在 ``execute_code`` 子进程中获取到该凭据，
    瓦解了沙箱的剥离保证。

    非 Hermes 的 API key（TENOR_API_KEY、NOTION_TOKEN 等）不在
    该黑名单中，仍可合法注册 —— 包装第三方 API 的 skills 照常工作。
    """
    try:
        from tools.environments.local import _HERMES_PROVIDER_ENV_BLOCKLIST
    except Exception:
        return False
    return name in _HERMES_PROVIDER_ENV_BLOCKLIST


def register_env_passthrough(var_names: Iterable[str]) -> None:
    """将环境变量名注册为在沙箱环境中允许使用。

    通常在某个 skill 声明 ``required_environment_variables`` 时调用。

    属于 Hermes 托管 provider 凭据的变量（来自
    ``_HERMES_PROVIDER_ENV_BLOCKLIST``）在此被拒绝，以依据
    GHSA-rhgp-j443-p4rf 保留 ``execute_code`` 沙箱的凭据剥离保证。
    需要与 Hermes 托管 provider 通信的 skill 应通过 agent 的主进程工具
    （web_search、web_extract 等）进行，这样凭据能安全地保留在主进程中。

    非 Hermes 的第三方 API key（TENOR_API_KEY、NOTION_TOKEN 等）
    照常透传 —— 它们本就不在沙箱剥离列表中。
    """
    for name in var_names:
        name = name.strip()
        if not name:
            continue
        if _is_hermes_provider_credential(name):
            logger.warning(
                "env passthrough: refusing to register Hermes provider "
                "credential %r (blocked by _HERMES_PROVIDER_ENV_BLOCKLIST). "
                "Skills must not override the execute_code sandbox's "
                "credential scrubbing; see GHSA-rhgp-j443-p4rf.",
                name,
            )
            continue
        _get_allowed().add(name)
        logger.debug("env passthrough: registered %s", name)


def _load_config_passthrough() -> frozenset[str]:
    """从 config.yaml 加载 ``tools.env_passthrough``（带缓存）。"""
    global _config_passthrough
    if _config_passthrough is not None:
        return _config_passthrough

    result: set[str] = set()
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        passthrough = cfg_get(cfg, "terminal", "env_passthrough")
        if isinstance(passthrough, list):
            for item in passthrough:
                if not isinstance(item, str) or not item.strip():
                    continue
                name = item.strip()
                # 与 register_env_passthrough 中的 skill 路径过滤保持一致：
                # Hermes 托管的 provider 凭据绝不能透传给
                # execute_code / terminal 子进程，无论请求来自 skill 还是 config.yaml。
                # 参见 GHSA-rhgp-j443-p4rf。
                if _is_hermes_provider_credential(name):
                    logger.warning(
                        "env passthrough: refusing to register Hermes "
                        "provider credential %r from config.yaml (blocked "
                        "by _HERMES_PROVIDER_ENV_BLOCKLIST). Operator "
                        "configuration must not override the execute_code "
                        "sandbox's credential scrubbing; see "
                        "GHSA-rhgp-j443-p4rf.",
                        name,
                    )
                    continue
                result.add(name)
    except Exception as e:
        logger.debug("Could not read tools.env_passthrough from config: %s", e)

    _config_passthrough = frozenset(result)
    return _config_passthrough


def is_env_passthrough(var_name: str) -> bool:
    """检查 *var_name* 是否被允许透传给沙箱。

    如果该变量由 skill 注册或列在用户的 ``tools.env_passthrough`` 配置中，
    则返回 ``True``。
    """
    if var_name in _get_allowed():
        return True
    return var_name in _load_config_passthrough()


def get_all_passthrough() -> frozenset[str]:
    """返回 skill 注册的和基于配置的透传变量的并集。"""
    return frozenset(_get_allowed()) | _load_config_passthrough()


def clear_env_passthrough() -> None:
    """重置 skill 级别的白名单（例如在会话重置时）。"""
    _get_allowed().clear()

