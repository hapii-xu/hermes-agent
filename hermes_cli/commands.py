"""Hermes CLI 的斜杠命令定义和自动补全。

所有斜杠命令的中央注册表。每个消费者 —— CLI 帮助、gateway
分发、Telegram BotCommands、Slack 子命令映射、自动补全 ——
都从 ``COMMAND_REGISTRY`` 获取数据。

要添加命令：向 ``COMMAND_REGISTRY`` 添加一个 ``CommandDef`` 条目。
要添加别名：在现有 ``CommandDef`` 上设置 ``aliases=("short",)``。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from utils import is_truthy_value

logger = logging.getLogger(__name__)

# prompt_toolkit 是一个可选的 CLI 依赖 —— 仅在
# SlashCommandCompleter 和 SlashCommandAutoSuggest 中使用。
# 缺少它的 gateway 和测试环境仍需能够导入此模块
# 以使用 resolve_command、gateway_help_lines 和 COMMAND_REGISTRY。
try:
    from prompt_toolkit.auto_suggest import AutoSuggest, Suggestion
    from prompt_toolkit.completion import Completer, Completion
except ImportError:  # pragma: no cover
    AutoSuggest = object  # type: ignore[assignment,misc]
    Completer = object    # type: ignore[assignment,misc]
    Suggestion = None     # type: ignore[assignment]
    Completion = None     # type: ignore[assignment]


# ---------------------------------------------------------------------------
# CommandDef 数据类
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandDef:
    """单个斜杠命令的定义。"""

    name: str                          # 不含斜杠的规范名称："background"
    description: str                   # 人类可读的描述
    category: str                      # "Session"、"Configuration" 等
    aliases: tuple[str, ...] = ()      # 替代名称：("bg",)
    args_hint: str = ""                # 参数占位符："<prompt>"、"[name]"
    subcommands: tuple[str, ...] = ()  # 可 tab 补全的子命令
    cli_only: bool = False             # 仅在 CLI 中可用
    gateway_only: bool = False         # 仅在 gateway/消息平台中可用
    gateway_config_gate: str | None = None  # 配置点路径；为真时，覆盖 cli_only 以允许 gateway 使用


# ---------------------------------------------------------------------------
# 中央注册表 -- 单一真实来源
# ---------------------------------------------------------------------------

COMMAND_REGISTRY: list[CommandDef] = [
    # 会话
    CommandDef("start", "Acknowledge platform start pings without a reply", "Session",
               gateway_only=True),
    CommandDef("new", "Start a new session (fresh session ID + history)", "Session",
               aliases=("reset",), args_hint="[name]"),
    CommandDef("topic", "Enable or inspect Telegram DM topic sessions", "Session",
               gateway_only=True, args_hint="[off|help|session-id]"),
    CommandDef("clear", "Clear screen and start a new session", "Session",
               cli_only=True),
    CommandDef("redraw", "Force a full UI repaint (recovers from terminal drift)", "Session",
               cli_only=True),
    CommandDef("history", "Show conversation history", "Session",
               cli_only=True),
    CommandDef("save", "Save the current conversation", "Session",
               cli_only=True),
    CommandDef("retry", "Retry the last message (resend to agent)", "Session"),
    CommandDef("prompt", "Compose your next prompt in $EDITOR (markdown), then send it", "Session",
               cli_only=True, args_hint="[initial text]", aliases=("compose",)),
    CommandDef("undo", "Back up N user turns and re-prompt (default 1)", "Session",
               args_hint="[N]"),
    CommandDef("title", "Set a title for the current session", "Session",
               args_hint="[name]"),
    CommandDef("handoff", "Hand off this session to a messaging platform (Telegram, Discord, etc.)", "Session",
               args_hint="<platform>", cli_only=True),
    CommandDef("branch", "Branch the current session (explore a different path)", "Session",
               aliases=("fork",), args_hint="[name]"),
    CommandDef("compress", "Compress conversation context (add 'here [N]' to keep recent N turns)", "Session",
               args_hint="[here [N] | focus topic]"),
    CommandDef("rollback", "List or restore filesystem checkpoints", "Session",
               args_hint="[number]"),
    CommandDef("snapshot", "Create or restore state snapshots of Hermes config/state", "Session",
               cli_only=True, aliases=("snap",), args_hint="[create|restore <id>|prune]"),
    CommandDef("stop", "Kill all running background processes", "Session"),
    CommandDef("approve", "Approve a pending dangerous command", "Session",
               gateway_only=True, args_hint="[session|always]"),
    CommandDef("deny", "Deny a pending dangerous command", "Session",
               gateway_only=True),
    CommandDef("background", "Run a prompt in the background", "Session",
               aliases=("bg", "btw"), args_hint="<prompt>"),
    CommandDef("agents", "Show active agents and running tasks", "Session",
               aliases=("tasks",)),
    CommandDef("queue", "Queue a prompt for the next turn (doesn't interrupt)", "Session",
               aliases=("q",), args_hint="<prompt>"),
    CommandDef("steer", "Inject a message after the next tool call without interrupting", "Session",
               args_hint="<prompt>"),
    CommandDef("goal", "Set a standing goal Hermes works on across turns until achieved", "Session",
               args_hint="[text | draft <text> | show | pause | resume | clear | status | wait <pid> | unwait]"),
    CommandDef("subgoal", "Add or manage extra criteria on the active goal", "Session",
               args_hint="[text | remove N | clear]"),
    CommandDef("status", "Show session, model, token, and context info", "Session"),
    CommandDef("whoami", "Show your slash command access (admin / user)", "Info"),
    CommandDef("profile", "Show active profile name and home directory", "Info"),
    CommandDef("sethome", "Set this chat as the home channel", "Session",
               gateway_only=True, aliases=("set-home",)),
    CommandDef("resume", "Resume a previously-named session", "Session",
               args_hint="[name]"),

    # 配置
    CommandDef("sessions", "Browse and resume previous sessions", "Session"),

    # 配置
    CommandDef("config", "Show current configuration", "Configuration",
               cli_only=True),
    CommandDef("model", "Switch model (persists by default)", "Configuration",
               args_hint="[model] [--provider name] [--global|--session] [--refresh]"),
    CommandDef("codex-runtime", "Toggle codex app-server runtime for OpenAI/Codex models",
               "Configuration", aliases=("codex_runtime",),
               args_hint="[auto|codex_app_server]"),

    CommandDef("personality", "Set a predefined personality", "Configuration",
               args_hint="[name]"),
    CommandDef("statusbar", "Toggle the context/model status bar", "Configuration",
               cli_only=True, aliases=("sb",)),
    CommandDef("timestamps", "Toggle [HH:MM] timestamps on messages and /history", "Configuration",
               cli_only=True, args_hint="[on|off|status]",
               subcommands=("on", "off", "status"), aliases=("ts",)),
    CommandDef("verbose", "Cycle tool progress display: off -> new -> all -> verbose",
               "Configuration", cli_only=True,
               gateway_config_gate="display.tool_progress_command"),
    CommandDef("footer", "Toggle gateway runtime-metadata footer on final replies",
               "Configuration", args_hint="[on|off|status]",
               subcommands=("on", "off", "status")),
    CommandDef("yolo", "Toggle YOLO mode (skip all dangerous command approvals)",
               "Configuration"),
    CommandDef("reasoning", "Manage reasoning effort and display", "Configuration",
               args_hint="[level|show|hide|full|clamp]",
               subcommands=("none", "minimal", "low", "medium", "high", "xhigh", "show", "hide", "on", "off", "full", "clamp")),
    CommandDef("fast", "Toggle fast mode — OpenAI Priority Processing / Anthropic Fast Mode (Normal/Fast)", "Configuration",
               args_hint="[normal|fast|status]",
               subcommands=("normal", "fast", "status", "on", "off")),
    CommandDef("skin", "Show or change the display skin/theme", "Configuration",
               cli_only=True, args_hint="[name]"),
    CommandDef("indicator", "Pick the TUI busy-indicator style", "Configuration",
               cli_only=True, args_hint="[kaomoji|emoji|unicode|ascii]",
               subcommands=("kaomoji", "emoji", "unicode", "ascii")),
    CommandDef("voice", "Toggle voice mode", "Configuration",
               args_hint="[on|off|tts|status]", subcommands=("on", "off", "tts", "status")),
    CommandDef("busy", "Control what Enter does while Hermes is working", "Configuration",
               cli_only=True, args_hint="[queue|steer|interrupt|status]",
               subcommands=("queue", "steer", "interrupt", "status")),

    # 工具与技能
    CommandDef("tools", "Manage tools: /tools [list|disable|enable] [name...]", "Tools & Skills",
               args_hint="[list|disable|enable] [name...]", cli_only=True),
    CommandDef("toolsets", "List available toolsets", "Tools & Skills",
               cli_only=True),
    CommandDef("skills", "Search, install, inspect, or manage skills",
               "Tools & Skills", cli_only=True,
               gateway_config_gate="skills.write_approval",
               subcommands=("search", "browse", "inspect", "install", "audit",
                            "pending", "approve", "reject", "diff", "approval")),
    CommandDef("memory", "Review pending memory writes / toggle the approval gate",
               "Tools & Skills",
               args_hint="[pending|approve|reject|approval] [id|on|off]",
               subcommands=("pending", "approve", "reject", "approval")),
    CommandDef("bundles", "List skill bundles (aliases /<name> for multiple skills)",
               "Tools & Skills"),
    CommandDef("pet", "Toggle or adopt a petdex mascot (/pet, /pet list, /pet <slug>)", "Tools & Skills",
               cli_only=True, args_hint="[toggle|list|scale <n>|<slug>]", subcommands=("toggle", "list", "scale", "off")),
    CommandDef("hatch", "Generate a new petdex pet from a description",
               "Tools & Skills", cli_only=True, aliases=("generate-pet",), args_hint="[description]"),
    CommandDef("learn", "Learn a reusable skill from anything you describe (dirs, URLs, this chat, notes)",
               "Tools & Skills", args_hint="<what to learn from>"),
    CommandDef("cron", "Manage scheduled tasks", "Tools & Skills",
               cli_only=True, args_hint="[subcommand]",
               subcommands=("list", "add", "create", "edit", "pause", "resume", "run", "remove")),
    CommandDef("suggestions", "Review suggested automations (accept/dismiss)",
               "Tools & Skills", aliases=("suggest",), args_hint="[accept|dismiss N | catalog]",
               subcommands=("accept", "dismiss", "catalog", "clear")),
    CommandDef("blueprint", "Set up an automation from a blueprint template",
               "Tools & Skills", aliases=("bp",), args_hint="[name] [slot=value ...]"),
    CommandDef("curator", "Background skill maintenance (status, run, pin, archive, list-archived)",
               "Tools & Skills", args_hint="[subcommand]",
               subcommands=("status", "run", "pause", "resume", "pin", "unpin", "restore", "list-archived")),
    CommandDef("kanban", "Multi-profile collaboration board (tasks, links, comments)",
               "Tools & Skills", args_hint="[subcommand]",
               subcommands=("init", "boards", "create", "list", "ls", "show", "assign",
                            "reclaim", "reassign", "diagnostics", "diag", "link", "unlink",
                            "claim", "comment", "complete", "edit", "block", "unblock",
                            "archive", "tail", "dispatch", "stats", "notify-subscribe",
                            "notify-list", "notify-unsubscribe", "log", "runs",
                            "heartbeat", "assignees", "context", "specify", "gc")),
    CommandDef("reload", "Reload .env variables into the running session", "Tools & Skills",
               cli_only=True),
    CommandDef("reload-mcp", "Reload MCP servers from config", "Tools & Skills",
               aliases=("reload_mcp",)),
    CommandDef("reload-skills", "Re-scan ~/.hermes/skills/ for newly installed or removed skills",
               "Tools & Skills", aliases=("reload_skills",)),
    CommandDef("browser", "Connect browser tools to your live Chromium-family browser via CDP", "Tools & Skills",
               cli_only=True, args_hint="[connect|disconnect|status]",
               subcommands=("connect", "disconnect", "status")),
    CommandDef("plugins", "List installed plugins and their status",
               "Tools & Skills", cli_only=True),

    # 信息
    CommandDef("commands", "Browse all commands and skills (paginated)", "Info",
               gateway_only=True, args_hint="[page]"),
    CommandDef("help", "Show available commands", "Info"),
    CommandDef("restart", "Gracefully restart the gateway after draining active runs", "Session",
               gateway_only=True),
    CommandDef("usage", "Show token usage and rate limits for the current session", "Info"),
    CommandDef("credits", "Show Nous credit balance and top up", "Info"),
    CommandDef("billing", "Manage Nous terminal billing — buy credits, auto-reload, limits", "Info",
               cli_only=True),
    CommandDef("insights", "Show usage insights and analytics", "Info",
               args_hint="[days]"),
    CommandDef("platforms", "Show gateway/messaging platform status", "Info",
               cli_only=True, aliases=("gateway",)),
    CommandDef("platform", "Pause, resume, or list a failing gateway platform", "Info",
               gateway_only=True, args_hint="<pause|resume|list> [name]"),
    CommandDef("copy", "Copy the last assistant response to clipboard", "Info",
               cli_only=True, args_hint="[number]"),
    CommandDef("paste", "Attach clipboard image from your clipboard", "Info",
               cli_only=True),
    CommandDef("image", "Attach a local image file for your next prompt", "Info",
               cli_only=True, args_hint="<path>"),
    CommandDef("update", "Update Hermes Agent to the latest version", "Info"),
    CommandDef("version", "Show Hermes Agent version", "Info", aliases=("v",)),
    CommandDef("debug", "Upload debug report (system info + logs) and get shareable links", "Info"),

    # 退出
    CommandDef("quit", "Exit the CLI (use --delete to also remove session history)", "Exit",
               cli_only=True, aliases=("exit",), args_hint="[--delete]"),
]


# ---------------------------------------------------------------------------
# 派生查找表 -- 在导入时重建一次，由 rebuild_lookups() 刷新
# ---------------------------------------------------------------------------

def _build_command_lookup() -> dict[str, CommandDef]:
    """将每个名称和别名映射到其 CommandDef。"""
    lookup: dict[str, CommandDef] = {}
    for cmd in COMMAND_REGISTRY:
        lookup[cmd.name] = cmd
        for alias in cmd.aliases:
            lookup[alias] = cmd
    return lookup


_COMMAND_LOOKUP: dict[str, CommandDef] = _build_command_lookup()


def resolve_command(name: str) -> CommandDef | None:
    """将命令名称或别名解析为其 CommandDef。

    接受带或不带前导斜杠的名称。
    """
    return _COMMAND_LOOKUP.get(name.lower().lstrip("/"))


def _build_description(cmd: CommandDef) -> str:
    """构建包含使用提示的 CLI 面向描述字符串。"""
    if cmd.args_hint:
        return f"{cmd.description} (usage: /{cmd.name} {cmd.args_hint})"
    return cmd.description


# 向后兼容的扁平字典："/command" -> 描述
COMMANDS: dict[str, str] = {}
for _cmd in COMMAND_REGISTRY:
    if not _cmd.gateway_only:
        COMMANDS[f"/{_cmd.name}"] = _build_description(_cmd)
        for _alias in _cmd.aliases:
            COMMANDS[f"/{_alias}"] = f"{_cmd.description} (alias for /{_cmd.name})"

# 向后兼容的分类字典
COMMANDS_BY_CATEGORY: dict[str, dict[str, str]] = {}
for _cmd in COMMAND_REGISTRY:
    if not _cmd.gateway_only:
        _cat = COMMANDS_BY_CATEGORY.setdefault(_cmd.category, {})
        _cat[f"/{_cmd.name}"] = COMMANDS[f"/{_cmd.name}"]
        for _alias in _cmd.aliases:
            _cat[f"/{_alias}"] = COMMANDS[f"/{_alias}"]


# 子命令查找表："/cmd" -> ["sub1", "sub2", ...]
SUBCOMMANDS: dict[str, list[str]] = {}
for _cmd in COMMAND_REGISTRY:
    if _cmd.subcommands:
        SUBCOMMANDS[f"/{_cmd.name}"] = list(_cmd.subcommands)

# 同时提取 args_hint 中以管道分隔模式暗示的子命令
# 例如 args_hint="[on|off|tts|status]" 用于没有显式子命令的命令。
# 注意：如果命令已有显式子命令，则跳过此回退。
# 使用 CommandDef 上的 `subcommands` 字段来定义有意可 tab 补全的参数。
_PIPE_SUBS_RE = re.compile(r"[a-z]+(?:\|[a-z]+)+")
for _cmd in COMMAND_REGISTRY:
    key = f"/{_cmd.name}"
    if key in SUBCOMMANDS or not _cmd.args_hint:
        continue
    m = _PIPE_SUBS_RE.search(_cmd.args_hint)
    if m:
        SUBCOMMANDS[key] = m.group(0).split("|")


# ---------------------------------------------------------------------------
# Gateway 辅助函数
# ---------------------------------------------------------------------------

# gateway 识别的所有命令名称 + 别名的集合。
# 包含受 config 门控的命令，以便 gateway 可以分发它们
# （处理器在运行时检查 config 门控）。
GATEWAY_KNOWN_COMMANDS: frozenset[str] = frozenset(
    name
    for cmd in COMMAND_REGISTRY
    if not cmd.cli_only or cmd.gateway_config_gate
    for name in (cmd.name, *cmd.aliases)
)


def is_gateway_known_command(name: str | None) -> bool:
    """如果 ``name`` 解析为可 gateway 分发的斜杠命令，返回 True。

    这涵盖内置命令（从 ``COMMAND_REGISTRY`` 派生的
    ``GATEWAY_KNOWN_COMMANDS``）和插件注册的命令，后者采用
    延迟查找，因此导入此模块不会强制触发插件
    发现。Gateway 代码使用此函数来决定是否发出
    ``command:<name>`` 钩子 —— 插件命令获得与内置命令
    相同的生命周期事件。
    """
    if not name:
        return False
    if name in GATEWAY_KNOWN_COMMANDS:
        return True
    for plugin_name, _description, _args_hint in _iter_plugin_command_entries():
        if plugin_name == name:
            return True
    return False


# 在 gateway/run.py 中具有显式 Level-2 运行中 agent 处理器的命令。
# 此处列出用于内省/测试；语义上是
# "所有可解析命令" 的子集 —— 这才是真正的绕过集合（参见
# 下方的 should_bypass_active_session）。
ACTIVE_SESSION_BYPASS_COMMANDS: frozenset[str] = frozenset(
    {
        "agents",
        "approve",
        "background",
        "commands",
        "deny",
        "help",
        "new",
        "profile",
        "queue",
        "restart",
        "status",
        "steer",
        "stop",
        "update",
        "version",
    }
)


def should_bypass_active_session(command_name: str | None) -> bool:
    """对任何可解析的斜杠命令返回 True。

    原因：每个 gateway 注册的斜杠命令要么在 gateway/run.py 中
    有特定的 Level-2 处理器（/stop、/new、/model、
    /approve 等），要么到达返回 "忙碌 — 请先等待或 /stop"
    响应的运行中 agent 兜底处理器。在两种路径中，命令
    都被分发，而非排队。

    对于已识别的斜杠命令，排队总是错误的，因为
    gateway.run 中的安全网会丢弃任何到达待处理队列的命令
    文本 —— 这意味着运行中的 /model（或 /reasoning、
    /voice、/insights、/title、/resume、/retry、/undo、/compress、
    /usage、/reload-mcp、/sethome、/reset）会静默
    中断 agent 并被丢弃，产生零字符
    响应。参见 issue #5057 / PRs #6252、#10370、#4665。

    ACTIVE_SESSION_BYPASS_COMMANDS 保留具有显式 Level-2
    处理器的命令子集；其余命令落入兜底处理器。
    """
    return resolve_command(command_name) is not None if command_name else False


def _resolve_config_gates() -> set[str]:
    """返回 ``gateway_config_gate`` 为真的命令的规范名称集合。

    读取 ``config.yaml`` 并为每个受 config 门控的命令遍历
    点分隔的键路径。出错时返回空集，以便调用者
    优雅降级。
    """
    gated = [c for c in COMMAND_REGISTRY if c.gateway_config_gate]
    if not gated:
        return set()
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
    except Exception:
        return set()
    result: set[str] = set()
    for cmd in gated:
        val: Any = cfg
        for key in cmd.gateway_config_gate.split("."):
            if isinstance(val, dict):
                val = val.get(key)
            else:
                val = None
                break
        if is_truthy_value(val, default=False):
            result.add(cmd.name)
    return result


def _is_gateway_available(cmd: CommandDef, config_overrides: set[str] | None = None) -> bool:
    """检查 *cmd* 是否应出现在 gateway 界面（帮助、菜单、映射）中。

    当 ``cli_only`` 为 False 时无条件可用。当 ``cli_only``
    为 True 但设置了 ``gateway_config_gate`` 时，仅当 config 值为真
    时命令才可用。传入 *config_overrides*（来自
    ``_resolve_config_gates()``）以避免为每个命令重新读取 config。
    """
    if not cmd.cli_only:
        return True
    if cmd.gateway_config_gate:
        overrides = config_overrides if config_overrides is not None else _resolve_config_gates()
        return cmd.name in overrides
    return False


def _requires_argument(args_hint: str) -> bool:
    """当选择命令但没有文本时会不完整时返回 True。"""
    return args_hint.strip().startswith("<")


def gateway_help_lines() -> list[str]:
    """从注册表生成 gateway 帮助文本行。"""
    overrides = _resolve_config_gates()
    lines: list[str] = []
    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        args = f" {cmd.args_hint}" if cmd.args_hint else ""
        alias_parts: list[str] = []
        for a in cmd.aliases:
            # 跳过内部别名，如 reload_mcp（下划线变体）
            if a.replace("-", "_") == cmd.name.replace("-", "_") and a != cmd.name:
                continue
            alias_parts.append(f"`/{a}`")
        alias_note = f" (alias: {', '.join(alias_parts)})" if alias_parts else ""
        lines.append(f"`/{cmd.name}{args}` -- {cmd.description}{alias_note}")
    return lines


def _iter_plugin_command_entries() -> list[tuple[str, str, str]]:
    """为所有插件斜杠命令生成 (name, description, args_hint) 元组。

    插件命令通过
    :func:`hermes_cli.plugins.PluginContext.register_command` 注册。它们的行为
    类似于 ``CommandDef`` 条目，用于 gateway 界面展示：它们出现在
    Telegram 命令菜单、Slack 的 ``/hermes`` 子命令映射中，以及
    （通过 :func:`plugins.platforms.discord.adapter._register_slash_commands`）
    Discord 的原生斜杠命令选择器中。

    查找是延迟的，因此导入此模块不会强制触发插件发现
    （这可能触发文件系统扫描和环境依赖的
    行为）。
    """
    try:
        from hermes_cli.plugins import get_plugin_commands
    except Exception:
        return []
    try:
        commands = get_plugin_commands() or {}
    except Exception:
        return []
    entries: list[tuple[str, str, str]] = []
    for name, meta in commands.items():
        if not isinstance(name, str) or not isinstance(meta, dict):
            continue
        description = str(meta.get("description") or f"Run /{name}")
        args_hint = str(meta.get("args_hint") or "").strip()
        entries.append((name, description, args_hint))
    return entries


def telegram_bot_commands() -> list[tuple[str, str]]:
    """返回用于 Telegram setMyCommands 的 (command_name, description) 对。

    Telegram 命令名称不能包含连字符，因此用下划线替换。
    别名被跳过 —— Telegram 每个规范命令只显示一个菜单条目。

    需要参数的内置命令（例如 /queue、/steer、/background）
    **包含在内**，因为它们的处理器在没有 payload 时返回用法文本，
    使其可通过自动补全发现。

    需要参数的插件注册斜杠命令**被排除**，
    因为插件可能不提供无参数用法回退。
    """
    overrides = _resolve_config_gates()
    result: list[tuple[str, str]] = []
    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        # 内置的带参数命令被包含 —— 它们的处理器在
        # 无参数调用时显示用法文本，将它们从菜单中隐藏
        # 会损害可发现性（issue #24312）。
        tg_name = _sanitize_telegram_name(cmd.name)
        if tg_name:
            result.append((tg_name, cmd.description))
    for name, description, args_hint in _iter_plugin_command_entries():
        if _requires_argument(args_hint):
            continue
        tg_name = _sanitize_telegram_name(name)
        if tg_name:
            result.append((tg_name, description))
    return result


# Telegram 最多允许 100 个 BotCommands。Hermes 提供约 50 个内置命令；
# 默认 60 个槽位保持所有内置命令加常用技能命令在
# `/` 菜单中可见，同时 comfortably 低于 Telegram 约 4KB 的 payload 限制。
# 用户可以通过 platforms.telegram.extra.command_menu.max_commands 调整。
_DEFAULT_TELEGRAM_MENU_MAX_COMMANDS = 60
_TELEGRAM_BOT_API_MAX_COMMANDS = 100
_TELEGRAM_PRIORITY_MODES = {"prepend", "append", "replace"}

_TELEGRAM_MENU_PRIORITY = (
    # 最常输入的日常命令优先。
    "help",
    "new",
    "stop",
    "status",
    "resume",
    "sessions",
    "model",
    # 维护/诊断 —— 促使此优先级列表的命令。
    "debug",
    "restart",
    "update",
    "verbose",
    "commands",
    # 回合中会话控制。
    "approve",
    "deny",
    "queue",
    "steer",
    "background",
    # 较低优先级但仍有用的操作内置命令。
    "reasoning",
    "usage",
    "platforms",
    "platform",
    "profile",
    "whoami",
)
"""应保持在 Telegram 有限菜单中可见的内置命令。

Telegram 实际上只显示一个小的 BotCommand 菜单。完整的 Hermes
注册表在手动输入时仍可分发，但操作命令
需要在较低优先级的内置命令之前 surviving 可见菜单上限。
"""


def _nested_mapping(root: Mapping[str, Any], *path: str) -> Mapping[str, Any]:
    node: Any = root
    for key in path:
        if not isinstance(node, Mapping):
            return {}
        node = node.get(key)
    return node if isinstance(node, Mapping) else {}


def _telegram_command_menu_config() -> dict[str, Any]:
    """返回规范化的 Telegram 命令菜单配置及安全默认值。

    规范的用户面向路径：
    ``platforms.telegram.extra.command_menu``。
    """
    try:
        from hermes_cli.config import read_raw_config
        raw_cfg = read_raw_config() or {}
    except Exception:
        raw_cfg = {}
    if not isinstance(raw_cfg, Mapping):
        raw_cfg = {}

    menu_cfg = dict(_nested_mapping(raw_cfg, "platforms", "telegram", "extra", "command_menu"))

    max_commands = menu_cfg.get("max_commands", _DEFAULT_TELEGRAM_MENU_MAX_COMMANDS)
    try:
        max_commands = int(max_commands)
    except (TypeError, ValueError):
        max_commands = _DEFAULT_TELEGRAM_MENU_MAX_COMMANDS
    max_commands = max(1, min(_TELEGRAM_BOT_API_MAX_COMMANDS, max_commands))

    priority_mode = str(menu_cfg.get("priority_mode") or "prepend").strip().lower()
    if priority_mode not in _TELEGRAM_PRIORITY_MODES:
        priority_mode = "prepend"

    raw_priority = menu_cfg.get("priority")
    if isinstance(raw_priority, list):
        priority = [str(item) for item in raw_priority if str(item).strip()]
    else:
        priority = []

    return {
        "max_commands": max_commands,
        "priority_mode": priority_mode,
        "priority": priority,
    }


def telegram_menu_max_commands() -> int:
    """返回配置的 Telegram BotCommand 菜单上限及安全边界。"""
    return int(_telegram_command_menu_config()["max_commands"])


def _dedupe_sanitized_names(raw_names: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_name in raw_names:
        name = _sanitize_telegram_name(str(raw_name))
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return tuple(result)


def _telegram_effective_priority() -> tuple[str, ...]:
    menu_cfg = _telegram_command_menu_config()
    configured = list(_dedupe_sanitized_names(menu_cfg["priority"]))
    defaults = list(_dedupe_sanitized_names(_TELEGRAM_MENU_PRIORITY))

    if menu_cfg["priority_mode"] == "replace":
        raw_priority = configured
    elif menu_cfg["priority_mode"] == "append":
        raw_priority = defaults + configured
    else:
        raw_priority = configured + defaults

    return _dedupe_sanitized_names(raw_priority)


def _prioritize_telegram_menu_commands(
    commands: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    priority = {
        name: index
        for index, name in enumerate(_telegram_effective_priority())
    }
    return [
        command
        for _index, command in sorted(
            enumerate(commands),
            key=lambda item: (
                0,
                priority[item[1][0]],
                item[0],
            )
            if item[1][0] in priority
            else (
                1,
                item[0],
            ),
        )
    ]


_CMD_NAME_LIMIT = 32
"""命令名称最大长度，Telegram 和 Discord 共享。"""

# 向后兼容别名 —— 测试和外部代码可能引用旧名称。
_TG_NAME_LIMIT = _CMD_NAME_LIMIT

# Telegram Bot API 在命令名称中只允许小写 a-z、0-9 和下划线。
# 此正则表达式在初始转换后去除所有其他字符。
_TG_INVALID_CHARS = re.compile(r"[^a-z0-9_]")
_TG_MULTI_UNDERSCORE = re.compile(r"_{2,}")


def _sanitize_telegram_name(raw: str) -> str:
    """将命令/技能/插件名称转换为有效的 Telegram 命令名称。

    Telegram 要求：1-32 个字符，小写 a-z、数字 0-9、仅下划线。
    步骤：小写 → 连字符替换为下划线 → 去除所有其他
    无效字符 → 折叠连续下划线 → 去除前导/
    尾部下划线。
    """
    name = raw.lower().replace("-", "_")
    name = _TG_INVALID_CHARS.sub("", name)
    name = _TG_MULTI_UNDERSCORE.sub("_", name)
    return name.strip("_")


def _clamp_command_names(
    entries: list[tuple[str, ...]],
    reserved: set[str],
) -> list[tuple[str, ...]]:
    """执行 32 字符命令名称限制及冲突避免。

    Telegram 和 Discord 都将斜杠命令名称限制为 32 个字符。
    超过限制的名称会被截断。如果截断产生重复
    （与 *reserved* 名称或同批次中的早期条目），名称被
    缩短为 31 个字符并附加数字 ``0``-``9`` 以区分。
    如果所有 10 个数字槽位都被占用，条目被静默丢弃。

    接受长度 >= 2 的元组。超出 ``(name, desc)`` 的额外元素
    （例如 ``cmd_key``）原样传递，因此调用者可以附加
    在重命名后仍然保留的元数据。
    """
    used: set[str] = set(reserved)
    result: list[tuple] = []
    for entry in entries:
        name, desc, *extra = entry
        if len(name) > _CMD_NAME_LIMIT:
            candidate = name[:_CMD_NAME_LIMIT]
            if candidate in used:
                prefix = name[:_CMD_NAME_LIMIT - 1]
                for digit in range(10):
                    candidate = f"{prefix}{digit}"
                    if candidate not in used:
                        break
                else:
                    # 所有 10 个数字槽位已耗尽 —— 跳过条目
                    continue
            name = candidate
        if name in used:
            continue
        used.add(name)
        result.append((name, desc, *extra))
    return result


# 向后兼容别名。
_clamp_telegram_names = _clamp_command_names


# ---------------------------------------------------------------------------
# gateway 平台共享的技能/插件集合
# ---------------------------------------------------------------------------

def _collect_gateway_skill_entries(
    platform: str,
    max_slots: int,
    reserved_names: set[str],
    desc_limit: int = 100,
    sanitize_name: "Callable[[str], str] | None" = None,
) -> tuple[list[tuple[str, str, str]], int]:
    """为 gateway 平台收集插件 + 技能条目。

    优先级顺序：
      1. 插件斜杠命令（优先于技能）
      2. 内置技能命令（填充剩余槽位，按字母顺序）

    仅技能在达到上限时被裁剪。
    Hub 安装的技能被排除。每个平台禁用的技能被排除。

    Args:
        platform: 用于每个平台技能过滤的平台标识符
            （``"telegram"``、``"discord"`` 等）。
        max_slots: 返回的最大条目数（内置/核心命令后的剩余槽位）。
        reserved_names: 已被内置命令占用的名称。作为新名称
            添加时被就地修改。
        desc_limit: 最大描述长度（Telegram 为 40，Discord 为 100）。
        sanitize_name: 在限制前应用的可选名称转换，例如
            Telegram 的 :func:`_sanitize_telegram_name`。可返回
            空字符串表示 "跳过此条目"。

    Returns:
        ``(entries, hidden_count)``，其中 *entries* 是
        ``(name, description, cmd_key)`` 三元组列表，*hidden_count* 是
        因上限被丢弃的技能条目数。``cmd_key`` 是
        来自 :func:`get_skill_commands` 的原始 ``/skill-name`` 键。
    """
    all_entries: list[tuple[str, str, str]] = []

    # --- 第 1 层：插件斜杠命令（永不被裁剪）---------------------
    plugin_pairs: list[tuple[str, str]] = []
    try:
        from hermes_cli.plugins import get_plugin_commands
        plugin_cmds = get_plugin_commands()
        for cmd_name in sorted(plugin_cmds):
            name = sanitize_name(cmd_name) if sanitize_name else cmd_name
            if not name:
                continue
            desc = plugin_cmds[cmd_name].get("description", "Plugin command")
            if len(desc) > desc_limit:
                desc = desc[:desc_limit - 3] + "..."
            plugin_pairs.append((name, desc))
    except Exception:
        pass

    plugin_pairs = _clamp_command_names(plugin_pairs, reserved_names)
    reserved_names.update(n for n, _ in plugin_pairs)
    # 插件没有 cmd_key —— 使用空字符串作为占位符
    for n, d in plugin_pairs:
        all_entries.append((n, d, ""))

    # --- 第 2 层：内置技能命令（在上限处裁剪）-----------------
    _platform_disabled: set[str] = set()
    try:
        from agent.skill_utils import get_disabled_skill_names
        _platform_disabled = get_disabled_skill_names(platform=platform)
    except Exception:
        pass

    skill_triples: list[tuple[str, str, str]] = []
    try:
        from agent.skill_commands import get_skill_commands
        from tools.skills_tool import SKILLS_DIR
        from agent.skill_utils import get_external_skills_dirs
        _skills_dir = str(SKILLS_DIR.resolve())
        _hub_dir = str((SKILLS_DIR / ".hub").resolve()).rstrip("/") + "/"
        # 构建允许目录前缀集合：本地技能目录 + 任何
        # 用户配置的 ``skills.external_dirs``。确保每个前缀以
        # ``/`` 结尾，这样 ``/my-skills`` 不会也匹配 ``/my-skills-extra``。
        # 如果不扩展，外部技能在
        # ``hermes skills list`` 和 agent 的 ``/skill-name`` 分发中可见，
        # 但在 gateway 斜杠菜单中被静默排除（#8110）。
        _allowed_prefixes = [_skills_dir.rstrip("/") + "/"]
        _allowed_prefixes.extend(
            str(d).rstrip("/") + "/" for d in get_external_skills_dirs()
        )
        skill_cmds = get_skill_commands()
        for cmd_key in sorted(skill_cmds):
            info = skill_cmds[cmd_key]
            skill_path = info.get("skill_md_path", "")
            if not skill_path:
                continue
            if not any(skill_path.startswith(prefix) for prefix in _allowed_prefixes):
                continue
            if skill_path.startswith(_hub_dir):
                continue
            skill_name = info.get("name", "")
            if skill_name in _platform_disabled:
                continue
            raw_name = cmd_key.lstrip("/")
            name = sanitize_name(raw_name) if sanitize_name else raw_name
            if not name:
                continue
            desc = info.get("description", "")
            if len(desc) > desc_limit:
                desc = desc[:desc_limit - 3] + "..."
            skill_triples.append((name, desc, cmd_key))
    except Exception:
        pass

    # 限制名称；cmd_key 作为额外 payload 传递，以便在
    # 任何限制导致重命名后仍然保留。
    skill_triples = _clamp_command_names(skill_triples, reserved_names)

    # 技能填充剩余槽位 —— 唯一被裁剪的层
    remaining = max(0, max_slots - len(all_entries))
    hidden_count = max(0, len(skill_triples) - remaining)
    for n, d, k in skill_triples[:remaining]:
        all_entries.append((n, d, k))

    return all_entries[:max_slots], hidden_count


# ---------------------------------------------------------------------------
# 平台特定的封装器
# ---------------------------------------------------------------------------

def telegram_menu_commands(max_commands: int = 100) -> tuple[list[tuple[str, str]], int]:
    """返回限制在 Bot API 上限内的 Telegram 菜单命令。

    优先级顺序（更高优先级 = 永远不会被溢出挤出）：
      1. 核心 CommandDef 命令（始终包含）
      2. 插件斜杠命令（优先于技能）
      3. 内置技能命令（填充剩余槽位，按字母顺序）

    技能是唯一在达到上限时被裁剪的层。
    用户安装的 hub 技能被排除 —— 通过 /skills 访问。
    为 ``"telegram"`` 平台禁用的技能（通过 ``hermes skills
    config``）从菜单中完全排除。

    Returns:
        (menu_commands, hidden_count)，其中 hidden_count 是
        因上限被省略的命令数。
    """
    core_commands = _prioritize_telegram_menu_commands(list(telegram_bot_commands()))
    reserved_names = {n for n, _ in core_commands}
    all_commands = list(core_commands)
    hidden_core_count = max(0, len(all_commands) - max_commands)

    remaining_slots = max(0, max_commands - len(all_commands))
    entries, hidden_count = _collect_gateway_skill_entries(
        platform="telegram",
        max_slots=remaining_slots,
        reserved_names=reserved_names,
        desc_limit=40,
        sanitize_name=_sanitize_telegram_name,
    )
    # 丢弃 cmd_key —— Telegram 只需要 (name, desc) 对。
    all_commands.extend((n, d) for n, d, _k in entries)
    return all_commands[:max_commands], hidden_count + hidden_core_count


def discord_skill_commands(
    max_slots: int,
    reserved_names: set[str],
) -> tuple[list[tuple[str, str, str]], int]:
    """返回用于 Discord 斜杠命令注册的技能条目。

    与 :func:`telegram_menu_commands` 相同的优先级和过滤逻辑
    （插件 > 技能，排除 hub，排除每个平台禁用的），但
    适配 Discord 的约束：

    - 名称中允许连字符（无 ``-`` → ``_`` 清理）
    - 描述限制为 100 个字符（Discord 的每字段最大值）

    Args:
        max_slots: 可用命令槽位（100 减去现有内置命令数）。
        reserved_names: 已注册的内置命令名称。

    Returns:
        ``(entries, hidden_count)``，其中 *entries* 是
        ``(discord_name, description, cmd_key)`` 三元组列表。``cmd_key`` 是
        斜杠处理器回调所需的原始 ``/skill-name`` 键。
    """
    return _collect_gateway_skill_entries(
        platform="discord",
        max_slots=max_slots,
        reserved_names=set(reserved_names),  # copy — don't mutate caller's set
        desc_limit=100,
    )


def discord_skill_commands_by_category(
    reserved_names: set[str],
) -> tuple[dict[str, list[tuple[str, str, str]]], list[tuple[str, str, str]], int]:
    """返回按类别组织的技能条目，用于 Discord ``/skill`` 自动补全。

    目录嵌套在扫描根目录下至少 2 级的技能
    （例如 ``creative/ascii-art/SKILL.md``）按其顶层
    类别分组。根级技能（例如 ``dogfood/SKILL.md``）作为
    *uncategorized* 返回。

    扫描根目录包括本地 ``SKILLS_DIR`` **和**任何配置的
    ``skills.external_dirs`` —— 与 #18741 中应用于
    扁平 ``discord_skill_commands()`` 收集器的扩展过滤匹配。如果没有此
    对等性，外部目录技能通过 ``hermes skills list`` 和
    agent 的 ``/skill-name`` 分发可见，但在 Discord 的
    ``/skill`` 自动补全中静默缺失。

    过滤镜像 :func:`discord_skill_commands`：排除 hub 技能，
    排除每个平台禁用的，名称限制为 32 字符，描述
    限制为 100 字符。

    旧的 25 组 × 25 子命令上限（来自旧的嵌套
    ``/skill <cat> <name>`` 布局）**不**被应用 —— 实时调用者
    （``gateway/platforms/discord.py`` 中的 ``_register_skill_group``，
    在 PR #11580 中重构）扁平化这些结果并将它们馈送到单个
    自动补全回调，该回调可扩展到数千个条目而无需任何
    每命令 payload  concerns。``hidden_count`` 在返回
    元组中保留以向后兼容，仍然报告因其他原因
    被丢弃的技能（32 字符限制冲突与保留名称）。

    Returns:
        ``(categories, uncategorized, hidden_count)``

        - *categories*：``{category_name: [(name, description, cmd_key), ...]}``
        - *uncategorized*：``[(name, description, cmd_key), ...]``
        - *hidden_count*：因名称限制冲突已注册命令名称
          而被丢弃的技能。
    """
    from pathlib import Path as _P

    _platform_disabled: set[str] = set()
    try:
        from agent.skill_utils import get_disabled_skill_names
        _platform_disabled = get_disabled_skill_names(platform="discord")
    except Exception:
        pass

    # 收集原始技能数据 --------------------------------------------------
    categories: dict[str, list[tuple[str, str, str]]] = {}
    uncategorized: list[tuple[str, str, str]] = []
    # 将限制的 32 字符名称 → 其来源映射，以便在冲突时发出
    # 可操作的警告。保留的（gateway 内置）命令
    # 名称用标记标记，以便警告区分
    # "技能与保留命令冲突" 和 "两个技能在 32 字符限制上冲突"
    # —— 后者是值得重命名的情况。
    _names_used: dict[str, str] = dict.fromkeys(reserved_names, "<reserved>")
    hidden = 0

    try:
        from agent.skill_commands import get_skill_commands
        from agent.skill_utils import get_external_skills_dirs
        from tools.skills_tool import SKILLS_DIR

        _skills_dir = SKILLS_DIR.resolve()
        _hub_dir = (SKILLS_DIR / ".hub").resolve()
        # 构建（已解析根目录，是否为本地）元组列表。每个外部目录
        # 成为其自己的类别派生扫描根 —— 位于
        # ``<external>/mlops/foo/SKILL.md`` 的技能仍被归类为 "mlops"。
        _scan_roots: list[_P] = [_skills_dir]
        try:
            for ext in get_external_skills_dirs():
                try:
                    _scan_roots.append(_P(ext).resolve())
                except Exception:
                    continue
        except Exception:
            pass
        skill_cmds = get_skill_commands()

        for cmd_key in sorted(skill_cmds):
            info = skill_cmds[cmd_key]
            skill_path = info.get("skill_md_path", "")
            if not skill_path:
                continue
            sp = _P(skill_path).resolve()
            # Hub 技能通过技能 hub 加载，不作为
            # 斜杠命令展示。
            if str(sp).startswith(str(_hub_dir)):
                continue
            # 如果技能位于任何扫描根目录下则接受；记录
            # 匹配的根目录以便正确派生类别。
            matched_root: _P | None = None
            for root in _scan_roots:
                try:
                    sp.relative_to(root)
                except ValueError:
                    continue
                matched_root = root
                break
            if matched_root is None:
                continue

            skill_name = info.get("name", "")
            if skill_name in _platform_disabled:
                continue

            raw_name = cmd_key.lstrip("/")
            # 限制为 32 字符（Discord 每命令名称限制）
            discord_name = raw_name[:32]
            if discord_name in _names_used:
                # 两个技能的前 32 个字符相同。一个获胜
                # （第一个看到的，按字母顺序因为
                # 调用者迭代 ``sorted(skill_cmds)``）；另一个
                # 从 Discord 的 /skill 自动补全中丢弃。
                #
                # 将此静默计为 ``hidden``（旧行为）
                # 意味着技能作者无法发现丢弃 ——
                # 他们的技能只是不出现在选择器中。发出
                # WARNING 命名双方，以便作者可以重命名
                # 失败技能的前置 ``name:`` 为具有
                # 不同的 32 字符前缀。
                prior = _names_used[discord_name]
                if prior == "<reserved>":
                    logger.warning(
                        "Discord /skill: %r (from %r) collides on its 32-char "
                        "clamp with a reserved gateway command name %r — the "
                        "skill will not appear in the /skill autocomplete. "
                        "Rename the skill's frontmatter ``name:`` to differ "
                        "in its first 32 chars.",
                        discord_name, cmd_key, discord_name,
                    )
                else:
                    logger.warning(
                        "Discord /skill: %r and %r both clamp to %r on "
                        "Discord's 32-char command-name limit — only %r "
                        "will appear in the /skill autocomplete. Rename "
                        "one skill's frontmatter ``name:`` to differ in "
                        "its first 32 chars.",
                        prior, cmd_key, discord_name, prior,
                    )
                hidden += 1
                continue
            _names_used[discord_name] = cmd_key

            desc = info.get("description", "")
            if len(desc) > 100:
                desc = desc[:97] + "..."

            # 从匹配的扫描根目录内的相对路径确定类别。
            # 例如 creative/ascii-art/SKILL.md → ("creative", ...)
            rel = sp.parent.relative_to(matched_root)
            parts = rel.parts
            if len(parts) >= 2:
                cat = parts[0]
                categories.setdefault(cat, []).append((discord_name, desc, cmd_key))
            else:
                uncategorized.append((discord_name, desc, cmd_key))
    except Exception:
        pass

    return categories, uncategorized, hidden


# ---------------------------------------------------------------------------
# Slack 原生斜杠命令
# ---------------------------------------------------------------------------

# Slack 斜杠命令名称约束：小写 a-z、0-9、连字符、
# 下划线。最多 32 个字符。Slack app manifest 每个 app 接受最多 50 个斜杠
# 命令。
_SLACK_MAX_SLASH_COMMANDS = 50
_SLACK_NAME_LIMIT = 32
_SLACK_INVALID_CHARS = re.compile(r"[^a-z0-9_\-]")
_SLACK_RESERVED_COMMANDS = frozenset({
    # 不能由 app 注册的内置 Slack 斜杠命令。
    # https://slack.com/help/articles/201259356-Use-built-in-slash-commands
    "me", "status", "away", "dnd", "shrug", "remind", "msg", "feed",
    "who", "collapse", "expand", "leave", "join", "open", "search",
    "topic", "mute", "pro", "shortcuts",
})

# 高价值别名，即使注册表填满也必须 surviving Slack 的 50 个斜杠上限。
# 否则，添加新的规范命令会静默限制掉
# 低优先级别名（它们在第二轮中添加），因此一个
# 长期存在的原生斜杠如 /btw 可能仅仅因为一个
# 不相关的命令登陆而消失。它们在 /hermes 之后立即声明自己的槽位，
# 在规范名称和其他别名之前。此处未列出的任何内容
# 仍然优雅降级（通过 /hermes <command> 可达）。
# 保持此列表紧凑：每个固定别名占用一个槽位，否则
# 规范命令可以获得，当规范命令被限制时
# Telegram 对等测试失败（"reset" 正是因此被取消固定 ——
# /new 保留其原生槽位，别名拼写通过 /hermes reset 保持可达）。
_SLACK_PRIORITY_ALIASES = ("btw", "bg")

# 故意不给予原生 Slack 斜杠槽位的规范命令。Slack
# 将 app 限制为 50 个斜杠命令，注册表已达到此上限；
# 而不是让限制静默丢弃排序最后的命令（并破坏
# Telegram 对等性），我们显式通过 ``/hermes <command>`` 在仅 Slack 上
# 路由一些低频命令。它们在其他每个
# 界面（CLI、TUI、Telegram、Discord）上仍然是原生的。保持此列表紧凑且有意 ——
# telegram 对等测试读取它，因此此处的条目是
# 有意的 "Slack 通过 /hermes" 决策，而不是静默限制。
#   - credits：计费/充值界面；在 Slack 上通过 /hermes credits 访问。
#   - billing：终端计费界面（购买/自动重新加载/限制）；/hermes billing。
#   - debug：日志/报告上传界面；在 Slack 上通过 /hermes debug 访问。
_SLACK_VIA_HERMES_ONLY = frozenset({"credits", "billing", "debug"})


def _sanitize_slack_name(raw: str) -> str:
    """将命令名称转换为有效的 Slack 斜杠命令名称。

    Slack 允许小写 a-z、数字、连字符和下划线。最多 32
    个字符。大写被小写化；无效字符被去除。
    """
    name = raw.lower()
    name = _SLACK_INVALID_CHARS.sub("", name)
    name = name.strip("-_")
    return name[:_SLACK_NAME_LIMIT]


def slack_native_slashes() -> list[tuple[str, str, str]]:
    """返回 Slack 的 (slash_name, description, usage_hint) 三元组。

    ``COMMAND_REGISTRY`` 中每个 gateway 可用的命令都作为
    独立的 Slack 斜杠命令展示（例如 ``/btw``、``/stop``、``/model``），
    匹配 Discord 和 Telegram 的模型，其中每个命令都是
    一等斜杠，而不是 ``/hermes <verb>`` 子命令。

    规范名称和别名都包含在内，以便用户可以输入任何
    记录的表单（例如 ``/background``、``/bg`` 和 ``/btw`` 都有效）。
    插件注册的斜杠命令也包含在内。

    清理后名称与 Slack 内置命令冲突的命令
    （例如 ``/status``、``/me``、``/join``）被静默跳过。用户
    仍可通过 ``/hermes <command>`` 访问它们。

    结果被限制在 Slack 的 50 个命令上限，并避免重复名称。
    ``/hermes`` 始终作为第一个条目保留，以便
    传统的 ``/hermes <subcommand>`` 表单对因限制而丢弃的任何命令或自由形式问题保持有效。
    """
    overrides = _resolve_config_gates()
    entries: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    # 将 /hermes 保留为 catch-all 顶级命令。
    entries.append(("hermes", "Talk to Hermes or run a subcommand", "[subcommand] [args]"))
    seen.add("hermes")

    def _add(name: str, desc: str, hint: str) -> None:
        slack_name = _sanitize_slack_name(name)
        if not slack_name or slack_name in seen:
            return
        if slack_name in _SLACK_RESERVED_COMMANDS:
            return
        if slack_name in _SLACK_VIA_HERMES_ONLY:
        # 有意的仅 Slack 通过 /hermes（参见 _SLACK_VIA_HERMES_ONLY）。
            return
        if len(entries) >= _SLACK_MAX_SLASH_COMMANDS:
            return
        # Slack 描述上限是 2000 个字符；保持简短。
        entries.append((slack_name, desc[:140], hint[:100]))
        seen.add(slack_name)

    # 优先级轮次：固定高价值别名（例如 /btw、/bg、/reset）在
    # 除 /hermes 之外的所有内容之前，以便新的规范命令永远不会静默
    # 将它们从 50 个斜杠上限中限制掉。每个别名借用其父命令的
    # 描述和提示。
    _alias_to_cmd = {
        alias: cmd
        for cmd in COMMAND_REGISTRY
        if _is_gateway_available(cmd, overrides)
        for alias in cmd.aliases
    }
    for alias in _SLACK_PRIORITY_ALIASES:
        cmd = _alias_to_cmd.get(alias)
        if cmd is not None:
            _add(alias, f"Alias for /{cmd.name} — {cmd.description}", cmd.args_hint or "")

    # 第一轮：规范名称（以便在达到上限时它们赢得槽位）。
    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        _add(cmd.name, cmd.description, cmd.args_hint or "")

    # 第二轮：别名。
    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        for alias in cmd.aliases:
            # 跳过仅因大小写/标点而与规范名称不同的别名
            # 规范化（已由 _add 去重覆盖）。
            _add(alias, f"Alias for /{cmd.name} — {cmd.description}", cmd.args_hint or "")

    # 第三轮：插件命令。
    for name, description, args_hint in _iter_plugin_command_entries():
        _add(name, description, args_hint or "")

    return entries


def slack_app_manifest(request_url: str = "https://hermes-agent.local/slack/commands") -> dict[str, Any]:
    """生成包含所有 gateway 命令作为斜杠的 Slack app manifest。

    ``request_url`` 是 Slack 的 manifest schema 为每个斜杠
    命令所需的，但在 Socket 模式（我们使用的）中 Slack 忽略它并通过
    WebSocket 路由命令事件。占位符 URL 即可。

    返回的字典仅是 ``features.slash_commands`` 部分 ——
    调用者将其组合成完整的 manifest（或合并到现有的
    中）。保持狭窄避免将我们与 manifest schema 的其余部分耦合
    （display_information、oauth_config、settings 等），用户
    在 Slack UI 中设置一次且很少更改。
    """
    slashes = []
    for name, desc, usage in slack_native_slashes():
        entry = {
            "command": f"/{name}",
            "description": desc or f"Run /{name}",
            "should_escape": False,
            "url": request_url,
        }
        if usage:
            entry["usage_hint"] = usage
        slashes.append(entry)
    return {"features": {"slash_commands": slashes}}


def slack_subcommand_map() -> dict[str, str]:
    """返回 Slack /hermes 处理器的子命令 -> /command 映射。

    映射规范名称和别名，以便 /hermes bg do stuff 与
    /hermes background do stuff 相同。

    插件注册的斜杠命令包含在内，以便 ``/hermes <plugin-cmd>``
    通过插件处理器路由。
    """
    overrides = _resolve_config_gates()
    mapping: dict[str, str] = {}
    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        mapping[cmd.name] = f"/{cmd.name}"
        for alias in cmd.aliases:
            mapping[alias] = f"/{alias}"
    for name, _description, _args_hint in _iter_plugin_command_entries():
        if name not in mapping:
            mapping[name] = f"/{name}"
    return mapping


# ---------------------------------------------------------------------------
# 自动补全
# ---------------------------------------------------------------------------


class SlashCommandCompleter(Completer):
    """内置斜杠命令、子命令和技能命令的自动补全。"""

    def __init__(
        self,
        skill_commands_provider: Callable[[], Mapping[str, dict[str, Any]]] | None = None,
        command_filter: Callable[[str], bool] | None = None,
        skill_bundles_provider: Callable[[], Mapping[str, dict[str, Any]]] | None = None,
    ) -> None:
        self._skill_commands_provider = skill_commands_provider
        self._command_filter = command_filter
        self._skill_bundles_provider = skill_bundles_provider
        # 缓存的项目文件列表，用于模糊 @ 补全
        self._file_cache: list[str] = []
        self._file_cache_time: float = 0.0
        self._file_cache_cwd: str = ""

    def _command_allowed(self, slash_command: str) -> bool:
        if self._command_filter is None:
            return True
        try:
            return bool(self._command_filter(slash_command))
        except Exception:
            return True

    def _iter_skill_commands(self) -> Mapping[str, dict[str, Any]]:
        if self._skill_commands_provider is None:
            return {}
        try:
            return self._skill_commands_provider() or {}
        except Exception:
            return {}

    def _iter_skill_bundles(self) -> Mapping[str, dict[str, Any]]:
        if self._skill_bundles_provider is None:
            return {}
        try:
            return self._skill_bundles_provider() or {}
        except Exception:
            return {}

    # 无参数运行时打开选择器的命令。
    # 这些不应在补全中接收尾随空格，因为：
    # - TUI 的提交处理器在 Enter 时应用补全（如果输入不同）
    # - 添加空格使 "/model" → "/model " 这会阻止选择器执行
    _PICKER_COMMANDS = frozenset({"model", "skin", "personality"})

    @staticmethod
    def _completion_text(cmd_name: str, word: str) -> str:
        """返回补全的替换文本。

        当用户已经精确输入了完整命令（``/help``），
        返回 ``help`` 将是一个空操作，prompt_toolkit 会抑制
        菜单。附加尾随空格使下拉菜单保持可见并
        使退格自然地重新触发它。

        然而，打开选择器的命令（model、skin、personality）不应
        获得尾随空格 —— TUI 会在 Enter 时应用补全
        并阻止选择器打开。
        """
        if cmd_name != word:
            return cmd_name
        # 不为选择器命令添加空格 —— 允许 Enter 执行它们
        if cmd_name in SlashCommandCompleter._PICKER_COMMANDS:
            return cmd_name
        return f"{cmd_name} "

    @staticmethod
    def _extract_path_word(text: str) -> str | None:
        """如果当前词看起来像文件路径，则提取它。

        返回光标下的类路径标记，或如果
        当前词看起来不像路径则返回 None。当词以
        ``./``、``../``、``~/``、``/`` 开头，或包含 ``/`` 分隔符
        （例如 ``src/main.py``）时，词被认为是类路径的。

        包含 ``://`` scheme 分隔符的标记（例如 URL 如
        ``https://example.com/x``）即使包含 ``/`` 也被排除 ——
        它们永远不是有用的本地路径补全。
        """
        if not text:
            return None
        # 向后走以查找当前 "词" 的开头。
        # 词由空格分隔，但路径几乎可以包含任何内容。
        i = len(text) - 1
        while i >= 0 and text[i] != " ":
            i -= 1
        word = text[i + 1:]
        if not word:
            return None
        # URL 包含 "/" 但不是本地路径。将它们视为路径会在
        # 输入/粘贴链接时每次按键触发 os.listdir
        # （例如 https:// URL 变为 "https:" 的 listdir）——
        # 纯粹的延迟，永远不会是有用的补全。跳过任何带有
        # scheme 分隔符的标记。
        if "://" in word:
            return None
        # 仅为类路径标记触发路径补全
        if word.startswith(("./", "../", "~/", "/")) or "/" in word:
            return word
        return None

    @staticmethod
    def _path_completions(word: str, limit: int = 30):
        """为匹配 *word* 的文件路径生成 Completion 对象。"""
        expanded = os.path.expanduser(word)
        # 拆分为目录部分和要在其中匹配的前缀
        if expanded.endswith("/"):
            search_dir = expanded
            prefix = ""
        else:
            search_dir = os.path.dirname(expanded) or "."
            prefix = os.path.basename(expanded)

        try:
            entries = os.listdir(search_dir)
        except OSError:
            return

        count = 0
        prefix_lower = prefix.lower()
        for entry in sorted(entries):
            if prefix and not entry.lower().startswith(prefix_lower):
                continue
            if count >= limit:
                break

            full_path = os.path.join(search_dir, entry)
            is_dir = os.path.isdir(full_path)

            # 构建补全文本（替换输入的词）
            if word.startswith("~"):
                display_path = "~/" + os.path.relpath(full_path, os.path.expanduser("~"))
            elif os.path.isabs(word):
                display_path = full_path
            else:
                # 保持相对路径
                display_path = os.path.relpath(full_path)

            if is_dir:
                display_path += "/"

            suffix = "/" if is_dir else ""
            meta = "dir" if is_dir else _file_size_label(full_path)

            yield Completion(
                display_path,
                start_position=-len(word),
                display=entry + suffix,
                display_meta=meta,
            )
            count += 1

    @staticmethod
    def _extract_context_word(text: str) -> str | None:
        """提取裸 ``@`` 标记用于上下文引用补全。"""
        if not text:
            return None
        # 向后走以查找当前词的开头
        i = len(text) - 1
        while i >= 0 and text[i] != " ":
            i -= 1
        word = text[i + 1:]
        if not word.startswith("@"):
            return None
        return word

    def _context_completions(self, word: str, limit: int = 30):
        """生成 Claude Code 风格的 @ 上下文补全。

        裸 ``@`` 或 ``@partial`` 显示静态引用和匹配的文件/文件夹。
        ``@file:path`` 和 ``@folder:path`` 由现有的路径补全路径处理。
        """
        lowered = word.lower()

        # 静态上下文引用
        _STATIC_REFS = (
            ("@diff", "Git working tree diff"),
            ("@staged", "Git staged diff"),
            ("@file:", "Attach a file"),
            ("@folder:", "Attach a folder"),
            ("@git:", "Git log with diffs (e.g. @git:5)"),
            ("@url:", "Fetch web content"),
        )
        for candidate, meta in _STATIC_REFS:
            if candidate.lower().startswith(lowered) and candidate.lower() != lowered:
                yield Completion(
                    candidate,
                    start_position=-len(word),
                    display=candidate,
                    display_meta=meta,
                )

        # 如果用户输入了 @file: / @folder:（或只有 @file / @folder 且
        # 尚无冒号），委托给路径补全。接受裸
        # 形式使选择器在用户输入 `@folder` 后立即展示目录，
        # 而不要求他们首先接受静态
        # `@folder:` 提示并重新触发补全。
        for prefix in ("@file:", "@folder:"):
            bare = prefix[:-1]

            if word == bare or word.startswith(prefix):
                want_dir = prefix == "@folder:"
                path_part = '' if word == bare else word[len(prefix):]
                expanded = os.path.expanduser(path_part)

                if not expanded or expanded == ".":
                    search_dir, match_prefix = ".", ""
                elif expanded.endswith("/"):
                    search_dir, match_prefix = expanded, ""
                else:
                    search_dir = os.path.dirname(expanded) or "."
                    match_prefix = os.path.basename(expanded)

                try:
                    entries = os.listdir(search_dir)
                except OSError:
                    return

                count = 0
                prefix_lower = match_prefix.lower()
                for entry in sorted(entries):
                    if match_prefix and not entry.lower().startswith(prefix_lower):
                        continue
                    full_path = os.path.join(search_dir, entry)
                    is_dir = os.path.isdir(full_path)
                    # `@folder:` 必须只展示目录；`@file:` 只展示
                    # 常规文件。没有此过滤器，`@folder:` 会列出
                    # cwd 中的每个 .env / .gitignore，
                    # 违背了显式前缀并让期望
                    # 目录选择器的用户感到困惑。
                    if want_dir != is_dir:
                        continue
                    if count >= limit:
                        break
                    display_path = os.path.relpath(full_path)
                    suffix = "/" if is_dir else ""
                    meta = "dir" if is_dir else _file_size_label(full_path)
                    completion = f"{prefix}{display_path}{suffix}"
                    yield Completion(
                        completion,
                        start_position=-len(word),
                        display=entry + suffix,
                        display_meta=meta,
                    )
                    count += 1
                return

        # 裸 @ 或 @partial —— 模糊项目范围文件搜索
        query = word[1:]  # 去除 @
        yield from self._fuzzy_file_completions(word, query, limit)

    def _get_project_files(self) -> list[str]:
        """返回缓存的项目文件列表（每 5 秒刷新）。"""
        cwd = os.getcwd()
        now = time.monotonic()
        if (
            self._file_cache
            and self._file_cache_cwd == cwd
            and now - self._file_cache_time < 5.0
        ):
            return self._file_cache

        files: list[str] = []
        # 先尝试 rg（快速，遵守 .gitignore），然后 fd，然后 find。
        for cmd in [
            ["rg", "--files", "--sortr=modified", cwd],
            ["rg", "--files", cwd],
            ["fd", "--type", "f", "--base-directory", cwd],
        ]:
            tool = cmd[0]
            if not shutil.which(tool):
                continue
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=2,
                    cwd=cwd, encoding="utf-8", errors="replace",
                )
                if proc.returncode == 0 and proc.stdout and proc.stdout.strip():
                    raw = proc.stdout.strip().split("\n")
                    # 存储相对路径
                    for p in raw[:5000]:
                        rel = os.path.relpath(p, cwd) if os.path.isabs(p) else p
                        files.append(rel)
                    break
            except (subprocess.TimeoutExpired, OSError):
                continue

        self._file_cache = files
        self._file_cache_time = now
        self._file_cache_cwd = cwd
        return files

    @staticmethod
    def _score_path(filepath: str, query: str) -> int:
        """对模糊查询评分文件路径。越高 = 匹配越好。"""
        if not query:
            return 1  # 查询为空时显示所有内容

        filename = os.path.basename(filepath)
        lower_file = filename.lower()
        lower_path = filepath.lower()
        lower_q = query.lower()

        # 精确文件名匹配
        if lower_file == lower_q:
            return 100
        # 文件名以查询开头
        if lower_file.startswith(lower_q):
            return 80
        # 文件名包含查询作为子串
        if lower_q in lower_file:
            return 60
        # 完整路径包含查询
        if lower_q in lower_path:
            return 40
        # 首字母/缩写匹配：例如 "fo" 匹配 "file_operations"
        # 检查查询字符是否按顺序出现在文件名中
        qi = 0
        for c in lower_file:
            if qi < len(lower_q) and c == lower_q[qi]:
                qi += 1
        if qi == len(lower_q):
            # 如果匹配落在单词边界上（在 _、-、/、. 之后）则加分
            boundary_hits = 0
            qi = 0
            prev = "_"  # 将开头视为边界
            for c in lower_file:
                if qi < len(lower_q) and c == lower_q[qi]:
                    if prev in "_-./":
                        boundary_hits += 1
                    qi += 1
                prev = c
            if boundary_hits >= len(lower_q) * 0.5:
                return 35
            return 25
        return 0

    def _fuzzy_file_completions(self, word: str, query: str, limit: int = 20):
        """为裸 @query 生成模糊文件补全。"""
        files = self._get_project_files()

        if not query:
            # 无查询 —— 显示最近修改的文件（已按 mtime 排序）
            for fp in files[:limit]:
                is_dir = fp.endswith("/")
                filename = os.path.basename(fp)
                kind = "folder" if is_dir else "file"
                meta = "dir" if is_dir else _file_size_label(
                    os.path.join(os.getcwd(), fp)
                )
                yield Completion(
                    f"@{kind}:{fp}",
                    start_position=-len(word),
                    display=filename,
                    display_meta=meta,
                )
            return

        # 评分并排名
        scored = []
        for fp in files:
            s = self._score_path(fp, query)
            if s > 0:
                scored.append((s, fp))
        scored.sort(key=lambda x: (-x[0], x[1]))

        for _, fp in scored[:limit]:
            is_dir = fp.endswith("/")
            filename = os.path.basename(fp)
            kind = "folder" if is_dir else "file"
            meta = "dir" if is_dir else _file_size_label(
                os.path.join(os.getcwd(), fp)
            )
            yield Completion(
                f"@{kind}:{fp}",
                start_position=-len(word),
                display=filename,
                display_meta=f"{fp}  {meta}" if meta else fp,
            )

    @staticmethod
    def _skin_completions(sub_text: str, sub_lower: str):
        """从可用 skins 生成 /skin 补全。"""
        try:
            from hermes_cli.skin_engine import list_skins
            for s in list_skins():
                name = s["name"]
                if name.startswith(sub_lower) and name != sub_lower:
                    yield Completion(
                        name,
                        start_position=-len(sub_text),
                        display=name,
                        display_meta=s.get("description", "") or s.get("source", ""),
                    )
        except Exception:
            pass

    @staticmethod
    def _tools_completions(sub_text: str, sub_lower: str):
        """生成 /tools 的补全 —— 子命令 + toolset/MCP 服务器名称。

        处理 ``/tools <tab>``（建议 ``list|disable|enable``）和
        ``/tools enable <tab>`` / ``/tools disable <tab>``（建议 toolset
        键和 MCP 服务器前缀，按当前启用状态过滤，
        使用户只看到可操作的选项）。
        """
        SUBS = ("list", "disable", "enable")
        parts = sub_text.split()
        trailing_space = sub_text.endswith(" ")

        # 子命令阶段：零个词已输入，或正在完成第一个词。
        if len(parts) == 0 or (len(parts) == 1 and not trailing_space):
            partial = sub_text if not trailing_space else ""
            for sub in SUBS:
                if sub.startswith(partial.lower()) and sub != partial.lower():
                    yield Completion(sub, start_position=-len(partial), display=sub)
            return

        subcommand = parts[0].lower()
        if subcommand not in ("enable", "disable"):
            return

        partial = "" if trailing_space else parts[-1]
        partial_lower = partial.lower()
        already = set(parts[1:] if trailing_space else parts[1:-1])

        try:
            from hermes_cli.config import load_config
            from hermes_cli.tools_config import (
                CONFIGURABLE_TOOLSETS,
                _get_platform_tools,
                _get_plugin_toolset_keys,
            )

            config = load_config()
            enabled = _get_platform_tools(config, "cli", include_default_mcp_servers=False)

            for ts_key, label, _desc in CONFIGURABLE_TOOLSETS:
                if ts_key in already or not ts_key.startswith(partial_lower):
                    continue
                is_on = ts_key in enabled
                if subcommand == "enable" and is_on:
                    continue
                if subcommand == "disable" and not is_on:
                    continue
                yield Completion(
                    ts_key,
                    start_position=-len(partial),
                    display=ts_key,
                    display_meta=label,
                )

            for ts_key in sorted(_get_plugin_toolset_keys()):
                if ts_key in already or not ts_key.startswith(partial_lower):
                    continue
                is_on = ts_key in enabled
                if subcommand == "enable" and is_on:
                    continue
                if subcommand == "disable" and not is_on:
                    continue
                yield Completion(
                    ts_key,
                    start_position=-len(partial),
                    display=ts_key,
                    display_meta="plugin toolset",
                )

            mcp_servers = config.get("mcp_servers") or {}
            if isinstance(mcp_servers, dict):
                for server in sorted(mcp_servers):
                    prefix = f"{server}:"
                    if prefix in already or not prefix.startswith(partial_lower):
                        continue
                    yield Completion(
                        prefix,
                        start_position=-len(partial),
                        display=prefix,
                        display_meta=f"MCP server '{server}'",
                    )
        except Exception:
            return

    @staticmethod
    def _handoff_completions(sub_text: str, sub_lower: str):
        """生成 /handoff 的平台补全。

        提供已连接（已启用 + 已配置）的 gateway 平台。记录的
        家频道不是列出平台所必需的 —— 它通常在
        运行时学习 —— 因此 meta 提示是否已设置。仅完成
        第一个参数（平台）；一旦选择了一个，就停止。
        """
        parts = sub_text.split()
        trailing_space = sub_text.endswith(" ")
        if len(parts) > 1 or (len(parts) == 1 and trailing_space):
            return
        partial = "" if (not parts or trailing_space) else parts[-1]
        partial_lower = partial.lower()
        try:
            from gateway.config import load_gateway_config

            gw = load_gateway_config()
            platforms = gw.get_connected_platforms()
        except Exception:
            return
        for platform in platforms:
            name = platform.value
            if not name.startswith(partial_lower):
                continue
            try:
                home = gw.get_home_channel(platform)
            except Exception:
                home = None
            meta = f"→ {home.name}" if home and getattr(home, "name", None) else "send this session here"
            yield Completion(
                name,
                start_position=-len(partial),
                display=name,
                display_meta=meta,
            )

    @staticmethod
    def _personality_completions(sub_text: str, sub_lower: str):
        """从配置的 personalities 生成 /personality 补全。"""
        try:
            # 从运行时应用 personalities 的同一来源解析 ——
            # agent.personalities 通过 CLI config（附带内置的）。
            # load_config() 的 schema 没有 agent.personalities，
            # 因此即使有可用的 personalities，补全器
            # 过去也返回空。
            from cli import load_cli_config

            personalities = (load_cli_config().get("agent") or {}).get("personalities", {}) or {}
            if "none".startswith(sub_lower) and "none" != sub_lower:
                yield Completion(
                    "none",
                    start_position=-len(sub_text),
                    display="none",
                    display_meta="clear personality overlay",
                )
            for name, prompt in personalities.items():
                if name.startswith(sub_lower) and name != sub_lower:
                    if isinstance(prompt, dict):
                        meta = prompt.get("description") or prompt.get("system_prompt", "")[:50]
                    else:
                        meta = str(prompt)[:50]
                    yield Completion(
                        name,
                        start_position=-len(sub_text),
                        display=name,
                        display_meta=meta,
                    )
        except Exception:
            pass

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            # 尝试 @ 上下文补全（Claude Code 风格）
            ctx_word = self._extract_context_word(text)
            if ctx_word is not None:
                yield from self._context_completions(ctx_word)
                return
            # 为非斜杠输入尝试文件路径补全
            path_word = self._extract_path_word(text)
            if path_word is not None:
                yield from self._path_completions(path_word)
            return

        # 检查是否正在完成子命令（基础命令已输入）
        parts = text.split(maxsplit=1)
        base_cmd = parts[0].lower()
        if len(parts) > 1 or (len(parts) == 1 and text.endswith(" ")):
            sub_text = parts[1] if len(parts) > 1 else ""
            sub_lower = sub_text.lower()

            # 具有运行时列表的命令的动态补全
            if " " not in sub_text:
                if base_cmd == "/skin":
                    yield from self._skin_completions(sub_text, sub_lower)
                    return
                if base_cmd == "/personality":
                    yield from self._personality_completions(sub_text, sub_lower)
                    return

            # /tools 需要多词补全（子命令 + toolset 名称）
            # 因此它自己处理两个阶段，绕过下面的单词
            # SUBCOMMANDS 分支。
            if base_cmd == "/tools":
                yield from self._tools_completions(sub_text, sub_lower)
                return

            if base_cmd == "/handoff":
                yield from self._handoff_completions(sub_text, sub_lower)
                return

            # 静态子命令补全
            if " " not in sub_text and base_cmd in SUBCOMMANDS and self._command_allowed(base_cmd):
                for sub in SUBCOMMANDS[base_cmd]:
                    if sub.startswith(sub_lower) and sub != sub_lower:
                        yield Completion(
                            sub,
                            start_position=-len(sub_text),
                            display=sub,
                        )
            return

        word = text[1:]

        for cmd, desc in COMMANDS.items():
            if not self._command_allowed(cmd):
                continue
            cmd_name = cmd[1:]
            if cmd_name.startswith(word):
                yield Completion(
                    self._completion_text(cmd_name, word),
                    start_position=-len(word),
                    display=cmd,
                    display_meta=desc,
                )

        for cmd, info in self._iter_skill_bundles().items():
            cmd_name = cmd[1:]
            if cmd_name.startswith(word):
                description = str(info.get("description", "Skill bundle"))
                short_desc = description[:50] + ("..." if len(description) > 50 else "")
                skill_count = len(info.get("skills", []))
                yield Completion(
                    self._completion_text(cmd_name, word),
                    start_position=-len(word),
                    display=cmd,
                    display_meta=f"▣ {short_desc} ({skill_count} skills)",
                )

        for cmd, info in self._iter_skill_commands().items():
            cmd_name = cmd[1:]
            if cmd_name.startswith(word):
                description = str(info.get("description", "Skill command"))
                short_desc = description[:50] + ("..." if len(description) > 50 else "")
                yield Completion(
                    self._completion_text(cmd_name, word),
                    start_position=-len(word),
                    display=cmd,
                    display_meta=f"⚡ {short_desc}",
                )

        # 插件注册的斜杠命令
        try:
            from hermes_cli.plugins import get_plugin_commands
            for cmd_name, cmd_info in get_plugin_commands().items():
                if cmd_name.startswith(word):
                    desc = str(cmd_info.get("description", "Plugin command"))
                    short_desc = desc[:50] + ("..." if len(desc) > 50 else "")
                    yield Completion(
                        self._completion_text(cmd_name, word),
                        start_position=-len(word),
                        display=f"/{cmd_name}",
                        display_meta=f"🔌 {short_desc}",
                    )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 斜杠命令的内联自动建议（ghost 文本）
# ---------------------------------------------------------------------------

class SlashCommandAutoSuggest(AutoSuggest):
    """斜杠命令及其子命令的内联 ghost 文本建议。

    在您输入时以暗淡文本显示命令或子命令的其余部分。
    对非斜杠输入回退到基于历史的建议。
    """

    def __init__(
        self,
        history_suggest: AutoSuggest | None = None,
        completer: SlashCommandCompleter | None = None,
    ) -> None:
        self._history = history_suggest
        self._completer = completer  # 重用其模型缓存

    def get_suggestion(self, buffer, document):
        text = document.text_before_cursor

        # 仅为斜杠命令建议
        if not text.startswith("/"):
            # 回退到常规文本的历史
            if self._history:
                return self._history.get_suggestion(buffer, document)
            return None

        parts = text.split(maxsplit=1)
        base_cmd = parts[0].lower()

        if len(parts) == 1 and not text.endswith(" "):
            # 仍在输入命令名称：/upd → 建议 "ate"
            word = text[1:].lower()
            for cmd in COMMANDS:
                if self._completer is not None and not self._completer._command_allowed(cmd):
                    continue
                cmd_name = cmd[1:]  # 去除前导 /
                if cmd_name.startswith(word) and cmd_name != word:
                    return Suggestion(cmd_name[len(word):])
            return None

        # 命令已完成 —— 建议子命令
        sub_text = parts[1] if len(parts) > 1 else ""
        sub_lower = sub_text.lower()

        # 静态子命令
        if self._completer is not None and not self._completer._command_allowed(base_cmd):
            return None
        if base_cmd in SUBCOMMANDS and SUBCOMMANDS[base_cmd]:
            if " " not in sub_text:
                for sub in SUBCOMMANDS[base_cmd]:
                    if sub.startswith(sub_lower) and sub != sub_lower:
                        return Suggestion(sub[len(sub_text):])

        # 回退到历史
        if self._history:
            return self._history.get_suggestion(buffer, document)
        return None


def _file_size_label(path: str) -> str:
    """返回紧凑的人类可读文件大小，出错时返回 ''。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f}K"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f}M"
    return f"{size / (1024 * 1024 * 1024):.1f}G"
