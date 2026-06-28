"""/codex-runtime 斜杠命令的共享逻辑。

在 ``model.openai_runtime`` 的 "auto"（即 chat_completions，Hermes 默认值）
和 "codex_app_server"（将会话转交给 codex 子进程处理）之间切换。

CLI（cli.py）和 gateway（gateway/run.py）都调用此模块，从而保持跨界面行为一致。

实际的运行时解析在 hermes_cli.runtime_provider 的
_maybe_apply_codex_app_server_runtime() 辅助函数中进行，
该函数读取持久化的配置值。本模块只负责持久化该值并报告变更。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


VALID_RUNTIMES = ("auto", "codex_app_server")


@dataclass
class CodexRuntimeStatus:
    """``/codex-runtime`` 调用的结果。调用方根据所在界面进行渲染
    （CLI 使用 Rich 面板，gateway 发送文本消息）。"""

    success: bool
    new_value: Optional[str] = None
    old_value: Optional[str] = None
    message: str = ""
    requires_new_session: bool = False
    codex_binary_ok: bool = True
    codex_version: Optional[str] = None


def parse_args(arg_string: str) -> tuple[Optional[str], list[str]]:
    """解析斜杠命令的参数字符串。返回 (value, errors)。

    无参数              → 返回当前状态（value=None）
    'auto' / 'codex_app_server' / 'on' / 'off' → 返回对应值
    其他内容            → 错误
    """
    raw = (arg_string or "").strip().lower()
    if not raw:
        return None, []
    # Accept human-friendly synonyms
    if raw in {"on", "codex", "enable"}:
        return "codex_app_server", []
    if raw in {"off", "default", "disable", "hermes"}:
        return "auto", []
    if raw in VALID_RUNTIMES:
        return raw, []
    return None, [
        f"Unknown runtime {raw!r}. Use one of: auto, codex_app_server, on, off"
    ]


def get_current_runtime(config: dict) -> str:
    """从配置字典中读取当前的 `model.openai_runtime` 值。
    对于未设置、空值或无法识别的值，返回 'auto'。"""
    if not isinstance(config, dict):
        return "auto"
    model_cfg = config.get("model") or {}
    if not isinstance(model_cfg, dict):
        return "auto"
    value = str(model_cfg.get("openai_runtime") or "").strip().lower()
    if value in VALID_RUNTIMES:
        return value
    return "auto"


def set_runtime(config: dict, new_value: str) -> str:
    """就地修改配置字典以持久化新的运行时值。
    返回旧值，供调用方报告变更时使用。"""
    if new_value not in VALID_RUNTIMES:
        raise ValueError(
            f"invalid runtime {new_value!r}; must be one of {VALID_RUNTIMES}"
        )
    old = get_current_runtime(config)
    if not isinstance(config.get("model"), dict):
        config["model"] = {}
    config["model"]["openai_runtime"] = new_value
    return old


def check_codex_binary_ok() -> tuple[bool, Optional[str]]:
    """尽力验证 codex CLI 是否已安装且版本可接受。
    返回 (ok, version_or_message)。"""
    try:
        from agent.transports.codex_app_server import check_codex_binary

        return check_codex_binary()
    except Exception as exc:  # pragma: no cover
        return False, f"codex check failed: {exc}"


def apply(
    config: dict,
    new_value: Optional[str],
    *,
    persist_callback=None,
) -> CodexRuntimeStatus:
    """CLI 和 gateway 处理器共用的顶层入口点。

    Args:
        config: 内存中的配置字典（设置 new_value 时会就地修改）
        new_value: 期望的运行时；None 表示"仅显示当前状态"
        persist_callback: 可选的可调用对象，接收修改后的配置字典
            并将其持久化到磁盘。为 None 时跳过（用于测试）。

    Returns: 描述操作结果的 CodexRuntimeStatus。
    """
    current = get_current_runtime(config)

    # 为此次 apply() 调用缓存 codex 二进制检查结果。子进程启动成本较低
    # （`codex --version` 约 50ms），但在启用路径中（只读/状态、门控、成功消息）
    # 可能会调用最多 3 次。None = 尚未检查；(bool, str) = 结果。
    _binary_check: Optional[tuple[bool, Optional[str]]] = None

    def _check_binary_cached() -> tuple[bool, Optional[str]]:
        nonlocal _binary_check
        if _binary_check is None:
            _binary_check = check_codex_binary_ok()
        return _binary_check

    # 只读调用：仅报告当前状态
    if new_value is None:
        ok, ver = _check_binary_cached()
        msg = (
            f"openai_runtime: {current}\n"
            f"codex CLI: {'OK ' + ver if ok else 'not available — ' + (ver or 'install with `npm i -g @openai/codex`')}"
        )
        return CodexRuntimeStatus(
            success=True,
            new_value=current,
            old_value=current,
            message=msg,
            codex_binary_ok=ok,
            codex_version=ver if ok else None,
        )

    # 未请求更改
    if new_value == current:
        return CodexRuntimeStatus(
            success=True,
            new_value=current,
            old_value=current,
            message=f"openai_runtime already set to {current}",
        )

    # 切换为开启状态时，在持久化前验证 codex CLI 是否已安装 —
    # 一个在第一次调用时静默失败的可选开关是最差的用户体验。
    # 在此处阻止并给出明确的安装提示。
    if new_value == "codex_app_server":
        ok, ver_or_msg = _check_binary_cached()
        if not ok:
            return CodexRuntimeStatus(
                success=False,
                new_value=None,
                old_value=current,
                message=(
                    "Cannot enable codex_app_server runtime: "
                    f"{ver_or_msg or 'codex CLI not available'}\n"
                    "Install with: npm i -g @openai/codex"
                ),
                codex_binary_ok=False,
                codex_version=None,
            )

    set_runtime(config, new_value)
    if persist_callback is not None:
        try:
            persist_callback(config)
        except Exception as exc:
            logger.exception("failed to persist openai_runtime change")
            return CodexRuntimeStatus(
                success=False,
                new_value=new_value,
                old_value=current,
                message=f"updated config in memory but persist failed: {exc}",
            )

    msg_lines = [
        f"openai_runtime: {current} → {new_value}",
    ]
    if new_value == "codex_app_server":
        ok, ver = _check_binary_cached()
        if ok:
            msg_lines.append(f"codex CLI: {ver}")
        # 自动将 Hermes 的 MCP 服务器和 Codex 已安装的精选插件迁移到
        # ~/.codex/config.toml，让生成的 codex 子进程能看到相同的工具界面，
        # 同时可通过 MCP 回调调用 Hermes 的 browser/web/delegate_task/vision/memory 工具（修复 #7）。
        # 失败不致命 — 运行时切换仍会继续。
        try:
            from hermes_cli.codex_runtime_plugin_migration import migrate
            mig_report = migrate(config)
            # 工具/MCP 服务器（不含 hermes-tools 回调，
            # 那是内部管道 — 单独显示）。
            user_servers = [
                s for s in mig_report.migrated if s != "hermes-tools"
            ]
            if user_servers:
                msg_lines.append(
                    f"Migrated {len(user_servers)} MCP server(s): "
                    f"{', '.join(user_servers)}"
                )
            # 原生 Codex 插件迁移（Linear、GitHub 等）
            if mig_report.migrated_plugins:
                msg_lines.append(
                    f"Migrated {len(mig_report.migrated_plugins)} native "
                    f"Codex plugin(s): {', '.join(mig_report.migrated_plugins)}"
                )
            elif mig_report.plugin_query_error:
                msg_lines.append(
                    f"Codex plugin discovery skipped: "
                    f"{mig_report.plugin_query_error}"
                )
            # 权限配置和 Hermes 工具回调属于始终启用的生产级内容，
            # 用户应当知晓。
            if mig_report.wrote_permissions_default:
                msg_lines.append(
                    f"Default sandbox: {mig_report.wrote_permissions_default} "
                    f"(no approval prompt on every write)"
                )
            if "hermes-tools" in mig_report.migrated:
                msg_lines.append(
                    "Hermes tool callback registered: codex can now use "
                    "web_search, web_extract, browser_*, vision_analyze, "
                    "image_generate, skill_view, skills_list, text_to_speech, "
                    "kanban_* (worker + orchestrator) via MCP."
                )
                msg_lines.append(
                    "  (delegate_task, memory, session_search, todo run "
                    "only on the default Hermes runtime — they need the "
                    "agent loop context.)"
                )
            msg_lines.append(f"  (config: {mig_report.target_path})")
            for err in mig_report.errors:
                msg_lines.append(f"⚠ MCP migration: {err}")
        except Exception as exc:
            msg_lines.append(f"⚠ MCP migration skipped: {exc}")
        msg_lines.append(
            "OpenAI/Codex turns now run through `codex app-server` "
            "(terminal/file ops/patching inside Codex; "
            "Hermes tools available via MCP callback)."
        )
        msg_lines.append(
            "Effective on next session — current cached agent keeps "
            "the prior runtime to preserve prompt cache."
        )
    else:
        msg_lines.append("OpenAI/Codex turns will use the default Hermes runtime.")
        msg_lines.append("Effective on next session.")
    return CodexRuntimeStatus(
        success=True,
        new_value=new_value,
        old_value=current,
        message="\n".join(msg_lines),
        requires_new_session=True,
    )
