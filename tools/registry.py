"""所有 hermes-agent 工具的中央注册表。

每个工具文件在模块级别调用 ``registry.register()`` 来声明其 schema、
handler、toolset 归属以及可用性检查。``model_tools.py`` 查询注册表，
而不是维护自己的并行数据结构。

导入链（避免循环导入）：
    tools/registry.py  （不从 model_tools 或工具文件导入）
           ^
    tools/*.py  （在模块级别从 tools.registry 导入）
           ^
    model_tools.py  （导入 tools.registry + 所有工具模块）
           ^
    run_agent.py, cli.py, batch_runner.py 等
"""

import ast
import importlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


def _is_registry_register_call(node: ast.AST) -> bool:
    """当 *node* 是一个 ``registry.register(...)`` 调用表达式时返回 True。"""
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "register"
        and isinstance(func.value, ast.Name)
        and func.value.id == "registry"
    )


def _module_registers_tools(module_path: Path) -> bool:
    """当模块包含一个顶层的 ``registry.register(...)`` 调用时返回 True。

    只检查模块体语句，这样恰好在一个函数内部调用
    ``registry.register()`` 的辅助模块不会被误选。
    """
    try:
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module_path))
    except (OSError, SyntaxError):
        return False

    return any(_is_registry_register_call(stmt) for stmt in tree.body)


def discover_builtin_tools(tools_dir: Optional[Path] = None) -> List[str]:
    """导入内建的自注册工具模块，并返回它们的模块名。"""
    tools_path = Path(tools_dir) if tools_dir is not None else Path(__file__).resolve().parent
    module_names = [
        f"tools.{path.stem}"
        for path in sorted(tools_path.glob("*.py"))
        if path.name not in {"__init__.py", "registry.py", "mcp_tool.py"}
        and _module_registers_tools(path)
    ]

    imported: List[str] = []
    for mod_name in module_names:
        try:
            importlib.import_module(mod_name)
            imported.append(mod_name)
        except Exception as e:
            logger.warning("Could not import tool module %s: %s", mod_name, e)
    return imported


class ToolEntry:
    """单个已注册工具的元数据。"""

    __slots__ = (
        "name", "toolset", "schema", "handler", "check_fn",
        "requires_env", "is_async", "description", "emoji",
        "max_result_size_chars", "dynamic_schema_overrides",
    )

    def __init__(self, name, toolset, schema, handler, check_fn,
                 requires_env, is_async, description, emoji,
                 max_result_size_chars=None, dynamic_schema_overrides=None):
        self.name = name
        self.toolset = toolset
        self.schema = schema
        self.handler = handler
        self.check_fn = check_fn
        self.requires_env = requires_env
        self.is_async = is_async
        self.description = description
        self.emoji = emoji
        self.max_result_size_chars = max_result_size_chars
        # 可选的零参 callable，返回一个 schema 覆盖 dict，在
        # get_definitions() 时应用。用于依赖运行时配置的字段（例如
        # delegate_task 的描述必须反映用户当前的
        # delegation.max_concurrent_children / max_spawn_depth，以免模型
        # 被告知错误的限制）。该 callable 在每次 get_definitions() 调用
        # 时都会被触发；结果会浅合并到基础 schema 之上，早于
        # {"type": "function", ...} 的包装。
        self.dynamic_schema_overrides = dynamic_schema_overrides


# ---------------------------------------------------------------------------
# check_fn TTL 缓存
#
# check_fn 这类 callable（如 tools/terminal_tool.check_terminal_requirements）
# 会探测外部状态（Docker 守护进程、Modal SDK 安装、playwright 二进制可用性）。
# 对于长生命周期的 CLI 或 gateway 进程，在每次 get_definitions() 时都调用
# 它们纯属浪费 —— 外部状态的变化发生在人类时间尺度上。将结果缓存约 30 秒，
# 使通过 ``hermes tools`` 的环境变量切换或实时凭据文件变更能在一两轮对话内
# 传播，而无需任何显式失效。
# ---------------------------------------------------------------------------

_CHECK_FN_TTL_SECONDS = 30.0
_check_fn_cache: Dict[Callable, tuple[float, bool]] = {}
_check_fn_cache_lock = threading.Lock()


def _check_fn_cached(fn: Callable) -> bool:
    """返回 bool(fn())，跨调用做 TTL 缓存。异常被吞掉并视为 False。"""
    now = time.monotonic()
    with _check_fn_cache_lock:
        cached = _check_fn_cache.get(fn)
        if cached is not None:
            ts, value = cached
            if now - ts < _CHECK_FN_TTL_SECONDS:
                return value
    try:
        value = bool(fn())
    except Exception:
        value = False
    with _check_fn_cache_lock:
        _check_fn_cache[fn] = (now, value)
    return value


def invalidate_check_fn_cache() -> None:
    """丢弃所有缓存的 ``check_fn`` 结果。在影响工具可用性的配置变更
    （例如 ``hermes tools enable``）之后调用。"""
    with _check_fn_cache_lock:
        _check_fn_cache.clear()


class ToolRegistry:
    """从工具文件收集工具 schema + handler 的单例注册表。"""

    def __init__(self):
        self._tools: Dict[str, ToolEntry] = {}
        self._toolset_checks: Dict[str, Callable] = {}
        self._toolset_aliases: Dict[str, str] = {}
        # MCP 动态刷新可能在其他线程读取工具元数据时变更注册表，
        # 因此将变更串行化，并让读取者使用稳定的快照。
        self._lock = threading.RLock()
        # 单调递增的 generation 计数器。每次变更（注册 / 注销 /
        # register_toolset_alias / MCP 刷新）时递增。外部调用者
        # （如 get_tool_definitions）可以针对它做记忆化：以 generation
        # 为键的缓存条目在 generation 未变化期间一直有效。
        self._generation: int = 0

    def _snapshot_state(self) -> tuple[List[ToolEntry], Dict[str, Callable]]:
        """返回注册表条目和 toolset 检查的一个一致性快照。"""
        with self._lock:
            return list(self._tools.values()), dict(self._toolset_checks)

    def _snapshot_entries(self) -> List[ToolEntry]:
        """返回已注册工具条目的稳定快照。"""
        return self._snapshot_state()[0]

    def _snapshot_toolset_checks(self) -> Dict[str, Callable]:
        """返回 toolset 可用性检查的稳定快照。"""
        return self._snapshot_state()[1]

    def _evaluate_toolset_check(self, toolset: str, check: Callable | None) -> bool:
        """运行一次 toolset 检查，将缺失或失败的检查视为不可用/可用。"""
        if not check:
            return True
        try:
            return bool(check())
        except Exception:
            logger.debug("Toolset %s check raised; marking unavailable", toolset)
            return False

    def get_entry(self, name: str) -> Optional[ToolEntry]:
        """按名称返回一个已注册的工具条目，或 None。"""
        with self._lock:
            return self._tools.get(name)

    def get_registered_toolset_names(self) -> List[str]:
        """返回注册表中存在的、排序去重后的 toolset 名称。"""
        return sorted({entry.toolset for entry in self._snapshot_entries()})

    def get_tool_names_for_toolset(self, toolset: str) -> List[str]:
        """返回注册在某个给定 toolset 下的、排序后的工具名。"""
        return sorted(
            entry.name for entry in self._snapshot_entries()
            if entry.toolset == toolset
        )

    def register_toolset_alias(self, alias: str, toolset: str) -> None:
        """为某个规范 toolset 名注册一个显式别名。"""
        with self._lock:
            existing = self._toolset_aliases.get(alias)
            if existing and existing != toolset:
                logger.warning(
                    "Toolset alias collision: '%s' (%s) overwritten by %s",
                    alias, existing, toolset,
                )
            self._toolset_aliases[alias] = toolset
            self._generation += 1

    def get_registered_toolset_aliases(self) -> Dict[str, str]:
        """返回 ``{alias: canonical_toolset}`` 映射的一个快照。"""
        with self._lock:
            return dict(self._toolset_aliases)

    def get_toolset_alias_target(self, alias: str) -> Optional[str]:
        """返回某个别名对应的规范 toolset 名，或 None。"""
        with self._lock:
            return self._toolset_aliases.get(alias)

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        check_fn: Callable = None,
        requires_env: list = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        max_result_size_chars: int | float | None = None,
        dynamic_schema_overrides: Callable = None,
        override: bool = False,
    ):
        """注册一个工具。由每个工具文件在模块导入时调用。

        ``override=True`` 是插件显式声明的意图：替换一个已有的内建工具
        实现（例如把默认的浏览器工具换成有头 Chrome 的 CDP 后端）。
        若不设置，凡是会遮蔽来自不同 toolset 的已有工具的注册都会被
        拒绝，以防意外的覆盖。
        """
        with self._lock:
            existing = self._tools.get(name)
            if existing and existing.toolset != toolset:
                # 允许 MCP 到 MCP 的覆盖（合法场景：服务器刷新，
                # 或两个工具名重叠的 MCP 服务器）。
                both_mcp = (
                    existing.toolset.startswith("mcp-")
                    and toolset.startswith("mcp-")
                )
                if both_mcp:
                    logger.debug(
                        "Tool '%s': MCP toolset '%s' overwriting MCP toolset '%s'",
                        name, toolset, existing.toolset,
                    )
                elif override:
                    # 显式的插件选择：替换已有工具。
                    # 以 INFO 级别记录，使覆盖可在 agent.log 中审计。
                    logger.info(
                        "Tool '%s': toolset '%s' overriding existing toolset '%s' "
                        "(override=True opt-in)",
                        name, toolset, existing.toolset,
                    )
                else:
                    # 拒绝遮蔽 —— 防止插件/MCP 覆盖内建工具，反之亦然。
                    logger.error(
                        "Tool registration REJECTED: '%s' (toolset '%s') would "
                        "shadow existing tool from toolset '%s'. Pass "
                        "override=True to register() if the replacement is "
                        "intentional, or deregister the existing tool first.",
                        name, toolset, existing.toolset,
                    )
                    return
            self._tools[name] = ToolEntry(
                name=name,
                toolset=toolset,
                schema=schema,
                handler=handler,
                check_fn=check_fn,
                requires_env=requires_env or [],
                is_async=is_async,
                description=description or schema.get("description", ""),
                emoji=emoji,
                max_result_size_chars=max_result_size_chars,
                dynamic_schema_overrides=dynamic_schema_overrides,
            )
            if check_fn and toolset not in self._toolset_checks:
                self._toolset_checks[toolset] = check_fn
            self._generation += 1

    def deregister(self, name: str) -> None:
        """从注册表中移除一个工具。

        若同一 toolset 下不再有其他工具，也会清理对应的 toolset 检查。
        MCP 动态工具发现在服务器发送
        ``notifications/tools/list_changed`` 时用它来做「推倒重建」。
        """
        with self._lock:
            entry = self._tools.pop(name, None)
            if entry is None:
                return
            # 若这是该 toolset 下的最后一个工具，则丢弃对应的 toolset
            # 检查和别名。
            toolset_still_exists = any(
                e.toolset == entry.toolset for e in self._tools.values()
            )
            if not toolset_still_exists:
                self._toolset_checks.pop(entry.toolset, None)
                self._toolset_aliases = {
                    alias: target
                    for alias, target in self._toolset_aliases.items()
                    if target != entry.toolset
                }
            self._generation += 1
        logger.debug("Deregistered tool: %s", name)

    # ------------------------------------------------------------------
    # Schema 检索
    # ------------------------------------------------------------------

    def get_definitions(self, tool_names: Set[str], quiet: bool = False) -> List[dict]:
        """为所请求的工具名返回 OpenAI 格式的工具 schema。

        只包含那些 ``check_fn()`` 返回 True（或没有 check_fn）的工具。
        ``check_fn()`` 结果通过 :func:`_check_fn_cached` 缓存约 30 秒，
        以摊销重复探测的开销（check_terminal_requirements 探测
        modal/docker，浏览器检查探测 playwright 等）；TTL 的选取使环境
        变量变更（``hermes tools enable foo``）仍能近实时生效，而不必
        在每次调用时强制做一次完整的缓存刷新。
        """
        result = []
        # 在 30 秒 TTL 之上的每次调用缓存 —— 处理在一次 definitions
        # 扫描内对同一 check_fn 的重复探测，而无需重复读取 TTL 时钟。
        check_results: Dict[Callable, bool] = {}
        entries_by_name = {entry.name: entry for entry in self._snapshot_entries()}
        for name in sorted(tool_names):
            entry = entries_by_name.get(name)
            if not entry:
                continue
            if entry.check_fn:
                if entry.check_fn not in check_results:
                    check_results[entry.check_fn] = _check_fn_cached(entry.check_fn)
                if not check_results[entry.check_fn]:
                    if not quiet:
                        logger.debug("Tool %s unavailable (check failed)", name)
                    continue
            # 确保 schema 始终带有一个 "name" 字段 —— 以 entry.name 作为兜底
            schema_with_name = {**entry.schema, "name": entry.name}
            # 应用运行时动态覆盖（例如 delegate_task 的描述取决于当前的
            # delegation.max_concurrent_children / max_spawn_depth）。调用方
            # 一侧（model_tools.get_tool_definitions）已将其记忆化键建立在
            # config.yaml 的 mtime + size 上，因此对 config 中 delegation.*
            # 的变更会自动使缓存失效。
            if entry.dynamic_schema_overrides is not None:
                try:
                    overrides = entry.dynamic_schema_overrides()
                    if isinstance(overrides, dict):
                        schema_with_name.update(overrides)
                except Exception as exc:
                    logger.warning(
                        "dynamic_schema_overrides for tool %s raised %s; "
                        "using static schema",
                        name, exc,
                    )
            result.append({"type": "function", "function": schema_with_name})
        return result

    # ------------------------------------------------------------------
    # 分发
    # ------------------------------------------------------------------

    def dispatch(self, name: str, args: dict, **kwargs) -> str:
        """按名称执行一个工具 handler。

        * 异步 handler 通过 ``_run_async()`` 自动桥接。
        * 所有异常都被捕获并以 ``{"error": "..."}`` 格式返回，
          以保证错误格式一致。
        """
        entry = self.get_entry(name)
        if not entry:
            return json.dumps({"error": f"Unknown tool: {name}"})
        try:
            if entry.is_async:
                from model_tools import _run_async
                return _run_async(entry.handler(args, **kwargs))
            return entry.handler(args, **kwargs)
        except Exception as e:
            logger.exception("Tool %s dispatch error: %s", name, e)
            # 经由 sanitizer 路由，使异常字符串中的成帧 token / CDATA /
            # 围栏不会作为结构性噪声到达模型。
            # 理由见 model_tools._sanitize_tool_error。
            raw = f"Tool execution failed: {type(e).__name__}: {e}"
            try:
                from model_tools import _sanitize_tool_error
                sanitized = _sanitize_tool_error(raw)
            except Exception:
                sanitized = raw  # 防御性：绝不让 sanitizer 阻断错误传播
            return json.dumps({"error": sanitized})

    # ------------------------------------------------------------------
    # 查询辅助  （取代 model_tools.py 中冗余的 dict）
    # ------------------------------------------------------------------

    def get_max_result_size(self, name: str, default: int | float | None = None) -> int | float:
        """返回按工具的最大结果大小，或 *default*（或全局默认值）。"""
        entry = self.get_entry(name)
        if entry and entry.max_result_size_chars is not None:
            return entry.max_result_size_chars
        if default is not None:
            return default
        from tools.budget_config import DEFAULT_RESULT_SIZE_CHARS
        return DEFAULT_RESULT_SIZE_CHARS

    def get_all_tool_names(self) -> List[str]:
        """返回所有已注册工具名的排序列表。"""
        return sorted(entry.name for entry in self._snapshot_entries())

    def get_schema(self, name: str) -> Optional[dict]:
        """返回某个工具的原始 schema dict，绕过 check_fn 过滤。

        适用于 token 估算和内省等可用性无关、只关心 schema 内容的场景。
        """
        entry = self.get_entry(name)
        return entry.schema if entry else None

    def get_toolset_for_tool(self, name: str) -> Optional[str]:
        """返回某个工具所属的 toolset，或 None。"""
        entry = self.get_entry(name)
        return entry.toolset if entry else None

    def get_emoji(self, name: str, default: str = "⚡") -> str:
        """返回某个工具的 emoji，未设置时返回 *default*。"""
        entry = self.get_entry(name)
        return (entry.emoji if entry and entry.emoji else default)

    def get_tool_to_toolset_map(self) -> Dict[str, str]:
        """为每个已注册工具返回 ``{tool_name: toolset_name}``。"""
        return {entry.name: entry.toolset for entry in self._snapshot_entries()}

    def is_toolset_available(self, toolset: str) -> bool:
        """检查某个 toolset 的要求是否得到满足。

        当检查函数抛出意外异常（例如网络错误、缺少导入、配置错误）时
        返回 False（而不是崩溃）。
        """
        with self._lock:
            check = self._toolset_checks.get(toolset)
        return self._evaluate_toolset_check(toolset, check)

    def check_toolset_requirements(self) -> Dict[str, bool]:
        """为每个 toolset 返回 ``{toolset: available_bool}``。"""
        entries, toolset_checks = self._snapshot_state()
        toolsets = sorted({entry.toolset for entry in entries})
        return {
            toolset: self._evaluate_toolset_check(toolset, toolset_checks.get(toolset))
            for toolset in toolsets
        }

    def get_available_toolsets(self) -> Dict[str, dict]:
        """返回供 UI 展示的 toolset 元数据。"""
        toolsets: Dict[str, dict] = {}
        entries, toolset_checks = self._snapshot_state()
        for entry in entries:
            ts = entry.toolset
            if ts not in toolsets:
                toolsets[ts] = {
                    "available": self._evaluate_toolset_check(
                        ts, toolset_checks.get(ts)
                    ),
                    "tools": [],
                    "description": "",
                    "requirements": [],
                }
            toolsets[ts]["tools"].append(entry.name)
            if entry.requires_env:
                for env in entry.requires_env:
                    if env not in toolsets[ts]["requirements"]:
                        toolsets[ts]["requirements"].append(env)
        return toolsets

    def get_toolset_requirements(self) -> Dict[str, dict]:
        """构建一个与 TOOLSET_REQUIREMENTS 兼容的 dict，用于向后兼容。"""
        result: Dict[str, dict] = {}
        entries, toolset_checks = self._snapshot_state()
        for entry in entries:
            ts = entry.toolset
            if ts not in result:
                result[ts] = {
                    "name": ts,
                    "env_vars": [],
                    "check_fn": toolset_checks.get(ts),
                    "setup_url": None,
                    "tools": [],
                }
            if entry.name not in result[ts]["tools"]:
                result[ts]["tools"].append(entry.name)
            for env in entry.requires_env:
                if env not in result[ts]["env_vars"]:
                    result[ts]["env_vars"].append(env)
        return result

    def check_tool_availability(self, quiet: bool = False):
        """像旧函数一样返回 (available_toolsets, unavailable_info)。"""
        available = []
        unavailable = []
        seen = set()
        entries, toolset_checks = self._snapshot_state()
        for entry in entries:
            ts = entry.toolset
            if ts in seen:
                continue
            seen.add(ts)
            if self._evaluate_toolset_check(ts, toolset_checks.get(ts)):
                available.append(ts)
            else:
                unavailable.append({
                    "name": ts,
                    "env_vars": entry.requires_env,
                    "tools": [e.name for e in entries if e.toolset == ts],
                })
        return available, unavailable


# 模块级单例
registry = ToolRegistry()


# ---------------------------------------------------------------------------
# 工具响应序列化的辅助函数
# ---------------------------------------------------------------------------
# 每个工具 handler 必须返回一个 JSON 字符串。这些辅助函数消除了在各工具
# 文件中出现数百次的样板 ``json.dumps({"error": msg}, ensure_ascii=False)``。
#
# 用法：
#   from tools.registry import registry, tool_error, tool_result
#
#   return tool_error("something went wrong")
#   return tool_error("not found", code=404)
#   return tool_result(success=True, data=payload)
#   return tool_result(items)            # 直接传一个 dict


def tool_error(message, **extra) -> str:
    """为工具 handler 返回一个 JSON 错误字符串。

    >>> tool_error("file not found")
    '{"error": "file not found"}'
    >>> tool_error("bad input", success=False)
    '{"error": "bad input", "success": false}'
    """
    result = {"error": str(message)}
    if extra:
        result.update(extra)
    return json.dumps(result, ensure_ascii=False)


def tool_result(data=None, **kwargs) -> str:
    """为工具 handler 返回一个 JSON 结果字符串。

    接受一个 dict 位置参数*或*关键字参数（二者不可同时使用）：

    >>> tool_result(success=True, count=42)
    '{"success": true, "count": 42}'
    >>> tool_result({"key": "value"})
    '{"key": "value"}'
    """
    if data is not None:
        return json.dumps(data, ensure_ascii=False)
    return json.dumps(kwargs, ensure_ascii=False)
