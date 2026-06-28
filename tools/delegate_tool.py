#!/usr/bin/env python3
"""
委托工具（Delegate Tool）—— 子代理架构

派生（spawn）子 AIAgent 实例，它们拥有隔离的上下文、受限的工具集，以及
各自的终端会话。支持单任务模式和批量（并行）模式。父代理会阻塞，直到所有
子代理执行完毕。

每个子代理拥有：
  - 全新的对话（不含父代理的历史记录）
  - 自己的 task_id（独立的终端会话、文件操作缓存）
  - 受限的工具集（可配置，始终会剥离被屏蔽的工具）
  - 由委派目标 + 上下文构建的聚焦型系统提示词

父代理的上下文只能看到委派调用本身和汇总结果，永远看不到子代理的中间工具
调用或推理过程。
"""

import enum
import json
import logging

logger = logging.getLogger(__name__)
import os
import threading
import time
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
)
from typing import Any, Dict, List, Optional

from toolsets import TOOLSETS

# 运行时 provider 系统使用的哨兵值，用于标识非原生已知 provider
#（具名自定义 provider、第三方聚合器等）。
# 必须与 hermes_cli.runtime_provider.RUNTIME_PROVIDER_TYPE_CUSTOM 保持一致。
_RUNTIME_PROVIDER_CUSTOM = "custom"
from tools import file_state
from tools.terminal_tool import set_approval_callback as _set_subagent_approval_cb
from utils import base_url_hostname, is_truthy_value


# 子代理永远不应拥有访问权限的工具
DELEGATE_BLOCKED_TOOLS = frozenset(
    [
        "delegate_task",  # 禁止递归委派
        "clarify",  # 禁止与用户交互
        "memory",  # 禁止写入共享的 MEMORY.md
        "send_message",  # 禁止跨平台副作用
        "execute_code",  # 子代理应逐步推理，而不是编写脚本
        "cronjob",  # 禁止以父代理的名义调度更多工作
    ]
)


# ---------------------------------------------------------------------------
# 子代理审批回调
# ---------------------------------------------------------------------------
# 子代理运行在 ThreadPoolExecutor 的工作线程里。CLI 的交互式审批回调保存在
# tools/terminal_tool.py 的 threading.local() 中，因此工作线程不会继承它。
# 没有回调时，prompt_dangerous_approval() 会回退到从工作线程中调用 input()，
# 这会与持有 stdin 的父代理 prompt_toolkit TUI 发生死锁。
#
# 修复方案：通过 ThreadPoolExecutor(initializer=_set_subagent_approval_cb,
# initargs=(cb,)) 向每个子代理工作线程安装一个非交互式回调。
# 回调由 `delegation.subagent_auto_approve` 配置项决定：
#   false（默认）→ _subagent_auto_deny（安全；与叶子工具黑名单一致）
#   true         → _subagent_auto_approve（cron/批量场景的可选 YOLO 模式）
# 两者都会发出 logger.warning 以便审计；网关会话不受影响，因为它们通过
# tools/approval.py 的按会话队列来解析审批，而不是走这些 TLS 回调。
def _subagent_auto_deny(command: str, description: str, **kwargs) -> str:
    """在子代理线程中自动拒绝危险命令（安全的默认行为）。

    返回 'deny'，让子代理看到一个可恢复的拒绝响应，并且绝不调用
    input()（否则会导致父代理 TUI 死锁）。
    """
    logger.warning(
        "Subagent auto-denied dangerous command: %s (%s). "
        "Set delegation.subagent_auto_approve: true to allow.",
        command, description,
    )
    return "deny"


def _subagent_auto_approve(command: str, description: str, **kwargs) -> str:
    """在子代理线程中自动批准危险命令（可选的 YOLO 模式）。

    仅在 delegation.subagent_auto_approve=true 时安装。返回 'once'，
    让子代理继续执行而不会阻塞父代理 UI。
    """
    logger.warning(
        "Subagent auto-approved dangerous command: %s (%s)",
        command, description,
    )
    return "once"


def _get_subagent_approval_callback():
    """返回要安装到子代理工作线程中的回调。

    配置键：delegation.subagent_auto_approve（布尔值，默认 False）。
    通过与 delegate_task 其余部分相同的 _load_config() 路径读取，因此
    优先级为 config.yaml >（此开关无环境变量覆盖）> 默认值。
    """
    cfg = _load_config()
    val = cfg.get("subagent_auto_approve", False)
    if is_truthy_value(val):
        return _subagent_auto_approve
    return _subagent_auto_deny

# 构建一段描述片段，列出子代理可用的工具集。
# 排除全部工具都被屏蔽的工具集、组合/平台工具集（以 hermes- 为前缀），
# 以及场景工具集。
#
# 注意：「delegation」也在排除集合中，这样面向子代理的能力提示字符串
#（_TOOLSET_LIST_STR）就不会把它宣传为可显式请求的工具集——嵌套委派
# 的正确机制是 role='orchestrator'，它会在 _build_child_agent 中无视此
# 排除集合重新加回「delegation」。
_EXCLUDED_TOOLSET_NAMES = frozenset({"debugging", "safe", "delegation", "moa", "rl"})
_SUBAGENT_TOOLSETS = sorted(
    name
    for name, defn in TOOLSETS.items()
    if name not in _EXCLUDED_TOOLSET_NAMES
    and not name.startswith("hermes-")
    and not all(t in DELEGATE_BLOCKED_TOOLS for t in defn.get("tools", []))
)
_TOOLSET_LIST_STR = ", ".join(f"'{n}'" for n in _SUBAGENT_TOOLSETS)

_DEFAULT_MAX_CONCURRENT_CHILDREN = 3
# 一次性防护：高并发成本告警每个进程最多发出一次。_get_max_concurrent_children()
# 会在每次 get_definitions() 重建 schema 时运行（通过 _build_top_level_description
# / _build_tasks_param_description），因此若没有这个标志，配置 max_concurrent_children>10
# 时会在每个回合 / 代理派生时都刷屏日志，即使 delegate_task 从未被调用。
_HIGH_CONCURRENCY_WARNED = False
MAX_DEPTH = 1  # 默认扁平：父代理 (0) -> 子代理 (1)；除非提高 max_spawn_depth，否则孙代理会被拒绝。
# _get_max_spawn_depth 会参考的可配置深度上限；MAX_DEPTH 仍是默认回退值，
# 并且仍是测试代码导入的符号。
_MIN_SPAWN_DEPTH = 1
# 派生深度没有上限——与 max_concurrent_children 一样，深度下限为 1 且无上限。
# 更深的树会让 API 成本成倍增加，因此默认保持扁平（MAX_DEPTH = 1）；
# 提高这个配置项是显式的可选启用行为。


# ---------------------------------------------------------------------------
# 运行时状态：暂停标志 + 活跃子代理注册表
#
# 供 TUI 可观测层（overlay/控制面板）以及网关 RPC `delegation.pause`、
# `delegation.status`、`subagent.interrupt` 消费。保持为模块级，这样它们
# 就能跨越进程中的每次 delegate_task 调用，包括嵌套的
# 编排者（orchestrator） -> 工作者（worker）链路。
# ---------------------------------------------------------------------------

_spawn_pause_lock = threading.Lock()
_spawn_paused: bool = False

_active_subagents_lock = threading.Lock()
# subagent_id -> 跟踪活跃子代理的可变记录。仅在本次运行的生命周期内存在；
# _run_single_child 是其所有者。
_active_subagents: Dict[str, Dict[str, Any]] = {}


def set_spawn_paused(paused: bool) -> bool:
    """全局阻塞/解除阻塞新的 delegate_task 派生。

    已在运行的子代理继续执行；只有对 delegate_task 的【新】调用会快速失败
    并返回 "spawning paused" 错误，直到解除阻塞。返回新的状态。
    """
    global _spawn_paused
    with _spawn_pause_lock:
        _spawn_paused = bool(paused)
        return _spawn_paused


def is_spawn_paused() -> bool:
    with _spawn_pause_lock:
        return _spawn_paused


def _register_subagent(record: Dict[str, Any]) -> None:
    sid = record.get("subagent_id")
    if not sid:
        return
    with _active_subagents_lock:
        _active_subagents[sid] = record


def _unregister_subagent(subagent_id: str) -> None:
    with _active_subagents_lock:
        _active_subagents.pop(subagent_id, None)


def interrupt_subagent(subagent_id: str) -> bool:
    """请求某个正在运行的子代理在其下一次迭代边界处停止。

    不会硬终止工作线程（Python 做不到）；而是设置子代理的中断标志，该标志
    会传播到执行中的工具，并通过 AIAgent.interrupt() 递归传播到孙代理。
    如果找到匹配的子代理则返回 True。
    """
    with _active_subagents_lock:
        record = _active_subagents.get(subagent_id)
    if not record:
        return False
    agent = record.get("agent")
    if agent is None:
        return False
    try:
        agent.interrupt(f"Interrupted via TUI ({subagent_id})")
    except Exception as exc:
        logger.debug("interrupt_subagent(%s) failed: %s", subagent_id, exc)
        return False
    return True


def list_active_subagents() -> List[Dict[str, Any]]:
    """当前正在运行的子代理树的快照。

    每条记录包含：{subagent_id, parent_id, depth, goal, model, started_at,
    tool_count, status}。可从任意线程安全调用——返回的是一份副本。
    """
    with _active_subagents_lock:
        return [
            {k: v for k, v in r.items() if k != "agent"}
            for r in _active_subagents.values()
        ]


def _extract_output_tail(
    result: Dict[str, Any],
    *,
    max_entries: int = 12,
    max_chars: int = 8000,
) -> List[Dict[str, Any]]:
    """从子代理的对话中提取最后 N 条工具调用结果。

    为 overlay 的「输出」区域提供数据——这是 cc-swarm-parity 特性。
    我们复用 trajectory saver 所遍历的同一个 messages 列表，只取尾部以
    保持事件负载较小。每个条目的结构为 ``{tool, preview, is_error}``。
    """
    messages = result.get("messages") if isinstance(result, dict) else None
    if not isinstance(messages, list):
        return []

    # 反向遍历以构建尾部；拿到足够数量后停止。
    tail: List[Dict[str, Any]] = []
    pending_call_by_id: Dict[str, str] = {}

    # 第一趟（正向）：构建 tool_call_id -> tool_name 的映射
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                tc_id = tc.get("id")
                fn = tc.get("function") or {}
                if tc_id:
                    pending_call_by_id[tc_id] = str(fn.get("name") or "tool")

    # 第二趟（反向）：挑选工具结果，最新的优先
    for msg in reversed(messages):
        if len(tail) >= max_entries:
            break
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        # 将 content-block 列表/字典拍平为文本，这样 overlay 能展示真实
        # 输出（而不是 "[{'type': 'text'...}]" 这样的团块），错误检测也
        # 能看到埋在 content block 里的标记。如果在这里用简单的 str()，
        # 会把被 block 包裹的 "Error: ..." 结果误判为 is_error=False。
        content = _stringify_tool_content(msg.get("content") or "")
        is_error = _looks_like_error_output(content)
        tool_name = pending_call_by_id.get(msg.get("tool_call_id") or "", "tool")
        # 保留换行结构，这样 overlay 的换行滚动区域能展示真实输出，
        # 而不是空白被折叠后的团块。我们仍然限制负载大小以保持事件有界。
        preview = content[:max_chars]
        tail.append({"tool": tool_name, "preview": preview, "is_error": is_error})

    tail.reverse()  # 恢复为按时间顺序展示
    return tail


def _stringify_tool_content(content: Any) -> str:
    """返回工具结果的稳定文本表示。

    大多数 provider 把工具结果存为字符串，但一些 OpenAI 兼容的路径会返回
    content-block 列表。委派可观测性在汇总子代理运行时绝不能仅因为传输层
    使用了 block 而崩溃。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                else:
                    parts.append(json.dumps(item, ensure_ascii=False, default=str))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False, default=str)
    return str(content)


def _looks_like_error_output(content: Any) -> bool:
    """用于工具结果预览的保守型 stderr/错误检测器。

    旧的启发式做法会把任何包含子串 "error" 的预览都标记为错误，这会把完全
    正常的终端/json 输出也标红。现在我们只在有更强证据时才把输出标记为错误：
      - 包含 ``error`` 键的结构化 JSON
      - ``status`` 为 error/failed 的结构化 JSON
      - 首行以经典的错误标记开头
    """
    content = _stringify_tool_content(content)
    if not content:
        return False

    head = content.lstrip()
    if head.startswith("{") or head.startswith("["):
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                if parsed.get("error"):
                    return True
                status = str(parsed.get("status") or "").strip().lower()
                if status in {"error", "failed", "failure", "timeout"}:
                    return True
        except Exception:
            pass

    first = content.splitlines()[0].strip().lower() if content.splitlines() else ""
    return (
        first.startswith("error:")
        or first.startswith("failed:")
        or first.startswith("traceback ")
        or first.startswith("exception:")
    )


def _normalize_role(r: Optional[str]) -> str:
    """把调用方传入的 role 归一化为 'leaf' 或 'orchestrator'。

    None/空值 -> 'leaf'。未知字符串会被强制转为 'leaf' 并记录一条
    warning 日志（与 _get_orchestrator_enabled 的静默降级模式一致）。
    _build_child_agent 还会针对深度/总开关边界添加第二层降级。
    """
    if r is None or not r:
        return "leaf"
    r_norm = str(r).strip().lower()
    if r_norm in {"leaf", "orchestrator"}:
        return r_norm
    logger.warning("Unknown delegate_task role=%r, coercing to 'leaf'", r)
    return "leaf"


def _get_max_concurrent_children() -> int:
    """从配置中读取 delegation.max_concurrent_children，回退顺序为
    DELEGATION_MAX_CONCURRENT_CHILDREN 环境变量，再到默认值 (3)。

    用户可以随意调高此值；仅强制执行下限 (1)。

    使用与 ``delegate_task`` 其余部分相同的 ``_load_config()`` 路径，保持
    配置优先级一致（config.yaml > 环境变量 > 默认值）。
    """
    cfg = _load_config()
    val = cfg.get("max_concurrent_children")
    if val is not None:
        try:
            result = max(1, int(val))
            if result > 10:
                global _HIGH_CONCURRENCY_WARNED
                if not _HIGH_CONCURRENCY_WARNED:
                    _HIGH_CONCURRENCY_WARNED = True
                    logger.warning(
                        "delegation.max_concurrent_children=%d: each child consumes API tokens "
                        "independently. High values multiply cost linearly.",
                        result,
                    )
            return result
        except (TypeError, ValueError):
            logger.warning(
                "delegation.max_concurrent_children=%r is not a valid integer; "
                "using default %d",
                val,
                _DEFAULT_MAX_CONCURRENT_CHILDREN,
            )
            return _DEFAULT_MAX_CONCURRENT_CHILDREN
    env_val = os.getenv("DELEGATION_MAX_CONCURRENT_CHILDREN")
    if env_val:
        try:
            return max(1, int(env_val))
        except (TypeError, ValueError):
            return _DEFAULT_MAX_CONCURRENT_CHILDREN
    return _DEFAULT_MAX_CONCURRENT_CHILDREN


_DEFAULT_MAX_ASYNC_CHILDREN = 3


def _get_max_async_children() -> int:
    """从配置中读取 delegation.max_async_children（下限 1，无上限）。

    限制同一时刻可以运行多少个后台（``background=true``）子代理。
    当达到容量上限时，新的异步派发会被【拒绝】（而不是排队），这样失控的
    模型就无法堆积无界的后台工作。与 max_concurrent_children 分开，后者
    限制的是单次同步批量任务的规模。
    """
    cfg = _load_config()
    val = cfg.get("max_async_children")
    if val is not None:
        try:
            return max(1, int(val))
        except (TypeError, ValueError):
            logger.warning(
                "delegation.max_async_children=%r is not a valid integer; "
                "using default %d",
                val, _DEFAULT_MAX_ASYNC_CHILDREN,
            )
            return _DEFAULT_MAX_ASYNC_CHILDREN
    env_val = os.getenv("DELEGATION_MAX_ASYNC_CHILDREN")
    if env_val:
        try:
            return max(1, int(env_val))
        except (TypeError, ValueError):
            return _DEFAULT_MAX_ASYNC_CHILDREN
    return _DEFAULT_MAX_ASYNC_CHILDREN


def _get_child_timeout() -> Optional[float]:
    """从配置中读取 delegation.child_timeout_seconds。

    返回单个子代理在被切断之前允许运行的秒数；若不应用挂钟时间上限，
    则返回 ``None``。

    默认值：``None``（无超时）。做合法重型工作的子代理（深度代码审查、
    大规模研究扇出、慢速推理模型）过去常被旧的统一上限在任务中途杀掉，
    即便它们正在稳步推进。失败应当源自子代理实际在做的事情——API 错误、
    工具错误、迭代预算——而不是源自一个通用的委派级秒表。卡死子代理的
    保护由心跳陈旧度监视器单独处理，它会停止刷新父代理活动，从而让网关
    不活跃超时能够触发。

    将 ``delegation.child_timeout_seconds`` 设为正数即可重新启用硬性上限
    （下限 30 秒）；``0`` 或负值表示禁用。
    """
    cfg = _load_config()
    val = cfg.get("child_timeout_seconds")
    if val is not None:
        try:
            parsed = float(val)
        except (TypeError, ValueError):
            logger.warning(
                "delegation.child_timeout_seconds=%r is not a valid number; "
                "using default (no timeout)",
                val,
            )
        else:
            return None if parsed <= 0 else max(30.0, parsed)
    env_val = os.getenv("DELEGATION_CHILD_TIMEOUT_SECONDS")
    if env_val:
        try:
            parsed = float(env_val)
        except (TypeError, ValueError):
            pass
        else:
            return None if parsed <= 0 else max(30.0, parsed)
    return DEFAULT_CHILD_TIMEOUT


def _get_max_spawn_depth() -> int:
    """从配置中读取 delegation.max_spawn_depth，下限为 1（无上限）。

    depth 0 = 父代理。max_spawn_depth = N 表示深度 0..N-1 的代理可以派生；
    深度 N 是叶子层底。默认值 1 是扁平的：父代理派生子代理（深度 1），
    深度 1 的子代理不能再派生（被此守卫阻止；并且对于叶子子代理，还会被
    _strip_blocked_tools 中的 delegation 工具集剥离所阻止）。

    提高到 2+ 可解锁嵌套编排。当 max_spawn_depth >= 2 时，role="orchestrator"
    会移除派生子代理时的工具集剥离，使其能够派生自己的工作者。与
    max_concurrent_children 一样，没有上限——但每多一层都会让 API 成本成倍
    增加，因此请谨慎调整。
    """
    cfg = _load_config()
    val = cfg.get("max_spawn_depth")
    if val is None:
        return MAX_DEPTH
    try:
        ival = int(val)
    except (TypeError, ValueError):
        logger.warning(
            "delegation.max_spawn_depth=%r is not a valid integer; " "using default %d",
            val,
            MAX_DEPTH,
        )
        return MAX_DEPTH
    floored = max(_MIN_SPAWN_DEPTH, ival)
    if floored != ival:
        logger.warning(
            "delegation.max_spawn_depth=%d below floor %d; using %d",
            ival,
            _MIN_SPAWN_DEPTH,
            floored,
        )
    return floored


def _get_orchestrator_enabled() -> bool:
    """orchestrator 角色的全局总开关。

    为 False 时，role="orchestrator" 会在 _build_child_agent 中被静默强制
    改为 "leaf"，并且 delegation 工具集仍像以前一样被剥离。这让运维人员
    可以无需回退代码即可禁用此特性。
    """
    cfg = _load_config()
    val = cfg.get("orchestrator_enabled", True)
    if isinstance(val, bool):
        return val
    # 接受来自未自动类型转换的 YAML 的 "true"/"false" 字符串。
    if isinstance(val, str):
        return val.strip().lower() in {"true", "1", "yes", "on"}
    return True


def _get_inherit_mcp_toolsets() -> bool:
    """收窄后的子代理工具集是否应保留父代理的 MCP 工具集。"""
    cfg = _load_config()
    return is_truthy_value(cfg.get("inherit_mcp_toolsets"), default=True)


def _is_mcp_toolset_name(name: str) -> bool:
    """对于规范的 MCP 工具集及其已注册的别名返回 True。"""
    if not name:
        return False
    if str(name).startswith("mcp-"):
        return True
    try:
        from tools.registry import registry

        target = registry.get_toolset_alias_target(str(name))
    except Exception:
        target = None
    return bool(target and str(target).startswith("mcp-"))


def _expand_parent_toolsets(parent_toolsets: set) -> set:
    """展开组合工具集，使各个独立的工具集名称能被识别。

    当父代理使用组合工具集（例如 ``hermes-cli``，它打包了所有核心工具）时，
    子代理可能请求独立的工具集，例如 ``web`` 或 ``terminal``。简单的基于
    名称的交集会把它们拒绝，因为 ``"web" != "hermes-cli"``。

    此辅助函数从父代理的每个工具集中收集工具名称，然后把所有工具属于父代理
    可用工具【子集】的独立工具集名称也加进来。原始的父代理工具集名称会被保留。
    """
    parent_tool_names: set = set()
    for ts_name in parent_toolsets:
        ts_def = TOOLSETS.get(ts_name)
        if ts_def:
            parent_tool_names.update(ts_def.get("tools", []))

    if not parent_tool_names:
        return set(parent_toolsets)

    expanded = set(parent_toolsets)
    for ts_name, ts_def in TOOLSETS.items():
        if ts_name in expanded:
            continue
        ts_tools = ts_def.get("tools", [])
        if ts_tools and set(ts_tools).issubset(parent_tool_names):
            expanded.add(ts_name)
    return expanded


def _preserve_parent_mcp_toolsets(
    child_toolsets: List[str], parent_toolsets: set[str]
) -> List[str]:
    """把收窄后的子代理所缺失的父代理 MCP 工具集追加进去。"""
    preserved = list(child_toolsets)
    for toolset_name in sorted(parent_toolsets):
        if _is_mcp_toolset_name(toolset_name) and toolset_name not in preserved:
            preserved.append(toolset_name)
    return preserved


DEFAULT_MAX_ITERATIONS = 50
# 子代理没有默认的挂钟时间上限：合法的重型子代理工作（深度审查、研究扇出、
# 慢速推理模型）过去会被在任务中途杀掉。错误应当源自子代理实际在做的事情；
# 卡死子代理的检测放在下面的心跳陈旧度监视器里。用户可以通过
# delegation.child_timeout_seconds 重新启用上限。
DEFAULT_CHILD_TIMEOUT: Optional[float] = None
_HEARTBEAT_INTERVAL = 30  # 委派期间父代理活动心跳之间的间隔秒数
# 心跳陈旧度阈值。一个没有 API 调用进展的子代理，状态要么是：
#   - 回合之间空闲（无 current_tool）——很可能卡在某个慢速 API 调用上
#   - 正在执行工具（current_tool 已设置）——很可能在运行一个合法的耗时
#     操作（终端命令、网络抓取、大文件读取）
# 空闲阈值保持紧凑，这样真正卡死的子代理不会掩盖网关超时。工具内阈值
# 要高得多，让合法的长时间运行工具有时间完成；delegation.child_timeout_seconds
#（默认关闭）仍然是想要硬性上限的用户的可选项。
_HEARTBEAT_STALE_CYCLES_IDLE = 15  # 15 * 30s = 450s 回合间空闲 → 陈旧
_HEARTBEAT_STALE_CYCLES_IN_TOOL = 40  # 40 * 30s = 1200s 卡在同一工具 → 陈旧
DEFAULT_TOOLSETS = ["terminal", "file", "web"]


# ---------------------------------------------------------------------------
# 委派进度事件类型
# ---------------------------------------------------------------------------


class DelegateEvent(str, enum.Enum):
    """委派进度期间发出的正式事件类型。

    _build_child_progress_callback 通过 ``_LEGACY_EVENT_MAP`` 把传入的旧版
    字符串（``tool.started``、``_thinking`` 等）归一化为这些枚举值。在弃用
    期内，外部消费者（网关 SSE、ACP 适配器、CLI）仍然接收旧版字符串。

    TASK_SPAWNED / TASK_COMPLETED / TASK_FAILED 保留给未来的编排者生命周期
    事件使用，当前并不会发出。
    """

    TASK_SPAWNED = "delegate.task_spawned"
    TASK_PROGRESS = "delegate.task_progress"
    TASK_COMPLETED = "delegate.task_completed"
    TASK_FAILED = "delegate.task_failed"
    TASK_THINKING = "delegate.task_thinking"
    TASK_TOOL_STARTED = "delegate.tool_started"
    TASK_TOOL_COMPLETED = "delegate.tool_completed"


# 旧版事件字符串 → DelegateEvent 的映射。
# 子代理传入的事件使用旧名称；回调会将其归一化。
_LEGACY_EVENT_MAP: Dict[str, DelegateEvent] = {
    "_thinking": DelegateEvent.TASK_THINKING,
    "reasoning.available": DelegateEvent.TASK_THINKING,
    "tool.started": DelegateEvent.TASK_TOOL_STARTED,
    "tool.completed": DelegateEvent.TASK_TOOL_COMPLETED,
    "subagent_progress": DelegateEvent.TASK_PROGRESS,
}


def check_delegate_requirements() -> bool:
    """委派没有外部依赖要求——始终可用。"""
    return True


def _build_child_system_prompt(
    goal: str,
    context: Optional[str] = None,
    *,
    workspace_path: Optional[str] = None,
    role: str = "leaf",
    max_spawn_depth: int = 2,
    child_depth: int = 1,
) -> str:
    """为子代理构建聚焦型的系统提示词。

    当 role='orchestrator' 时，追加一个委派能力说明块，其设计参照了
    OpenClaw 的 buildSubagentSystemPrompt（canSpawn 分支，见
    inspiration/openclaw/src/agents/subagent-system-prompt.ts:63-95）。
    深度说明是字面上的真实情况（基于传入的配置），这样 LLM 就不会
    臆造出不存在的嵌套能力。
    """
    parts = [
        "You are a focused subagent working on a specific delegated task.",
        "",
        f"YOUR TASK:\n{goal}",
    ]
    if context and context.strip():
        parts.append(f"\nCONTEXT:\n{context}")
    if workspace_path and str(workspace_path).strip():
        parts.append(
            "\nWORKSPACE PATH:\n"
            f"{workspace_path}\n"
            "Use this exact path for local repository/workdir operations unless the task explicitly says otherwise."
        )
    parts.append(
        "\nComplete this task using the tools available to you. "
        "When finished, provide a clear, concise summary of:\n"
        "- What you did\n"
        "- What you found or accomplished\n"
        "- Any files you created or modified\n"
        "- Any issues encountered\n\n"
        "Important workspace rule: Never assume a repository lives at /workspace/... or any other container-style path unless the task/context explicitly gives that path. "
        "If no exact local path is provided, discover it first before issuing git/workdir-specific commands.\n\n"
        "Be thorough but concise -- your response is returned to the "
        "parent agent as a summary."
    )
    if role == "orchestrator":
        child_note = (
            "Your own children MUST be leaves (cannot delegate further) "
            "because they would be at the depth floor — you cannot pass "
            "role='orchestrator' to your own delegate_task calls."
            if child_depth + 1 >= max_spawn_depth
            else "Your own children can themselves be orchestrators or leaves, "
            "depending on the `role` you pass to delegate_task. Default is "
            "'leaf'; pass role='orchestrator' explicitly when a child "
            "needs to further decompose its work."
        )
        parts.append(
            "\n## Subagent Spawning (Orchestrator Role)\n"
            "You have access to the `delegate_task` tool and CAN spawn "
            "your own subagents to parallelize independent work.\n\n"
            "WHEN to delegate:\n"
            "- The goal decomposes into 2+ independent subtasks that can "
            "run in parallel (e.g. research A and B simultaneously).\n"
            "- A subtask is reasoning-heavy and would flood your context "
            "with intermediate data.\n\n"
            "WHEN NOT to delegate:\n"
            "- Single-step mechanical work — do it directly.\n"
            "- Trivial tasks you can execute in one or two tool calls.\n"
            "- Re-delegating your entire assigned goal to one worker "
            "(that's just pass-through with no value added).\n\n"
            "Coordinate your workers' results and synthesize them before "
            "reporting back to your parent. You are responsible for the "
            "final summary, not your workers.\n\n"
            f"NOTE: You are at depth {child_depth}. The delegation tree "
            f"is capped at max_spawn_depth={max_spawn_depth}. {child_note}"
        )
    return "\n".join(parts)


def _resolve_workspace_hint(parent_agent) -> Optional[str]:
    """尽力为子代理提示词提供本地工作区路径。

    仅当我们有一个具体的绝对目录时才注入路径。这样既避免教给子代理一个
    虚假的容器路径，又能帮助它们避免在本地仓库任务中胡乱猜测
    `/workspace/...`。
    """
    candidates = [
        os.getenv("TERMINAL_CWD"),
        getattr(
            getattr(parent_agent, "_subdirectory_hints", None), "working_dir", None
        ),
        getattr(parent_agent, "terminal_cwd", None),
        getattr(parent_agent, "cwd", None),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            text = os.path.abspath(os.path.expanduser(str(candidate)))
        except Exception:
            continue
        if os.path.isabs(text) and os.path.isdir(text):
            return text
    return None


def _strip_blocked_tools(toolsets: List[str]) -> List[str]:
    """移除仅包含被屏蔽工具的工具集。

    剥离集合派生自 DELEGATE_BLOCKED_TOOLS，再加上没有一一对应工具的显式
    组合/场景工具集（delegation、code_execution）。这样可以让黑名单与剥离
    集合保持一致，使新增的被屏蔽工具不会以工具集名称的形式悄悄泄露。
    """
    # 永远不应传递给子代理的组合工具集，即使它们各自的工具并不全都在
    # DELEGATE_BLOCKED_TOOLS 中。
    _COMPOSITE_BLOCKED_TOOLSETS = frozenset({"delegation", "code_execution"})
    blocked_toolset_names = {
        name
        for name, defn in TOOLSETS.items()
        if name in _COMPOSITE_BLOCKED_TOOLSETS
        or all(t in DELEGATE_BLOCKED_TOOLS for t in defn.get("tools", []))
    }
    return [t for t in toolsets if t not in blocked_toolset_names]


def _build_child_progress_callback(
    task_index: int,
    goal: str,
    parent_agent,
    task_count: int = 1,
    *,
    subagent_id: Optional[str] = None,
    parent_id: Optional[str] = None,
    depth: Optional[int] = None,
    model: Optional[str] = None,
    toolsets: Optional[List[str]] = None,
    session_ref: Optional[Dict[str, Any]] = None,
) -> Optional[callable]:
    """构建一个回调，把子代理的工具调用转发到父代理的显示层。

    两条显示路径：
      CLI：    在父代理的委派 spinner 之上打印树状视图行
      网关：   批量收集工具名并转发给父代理的进度回调

    身份关键字参数（``subagent_id``、``parent_id``、``depth``、``model``、
    ``toolsets``）会被穿透到每个转发的事件中，这样 TUI 就能重建实时的派生
    树，并按 ``subagent_id`` 把按分支的控制（终止、暂停）路由回来。这些参数
    全部是可选的，以保持向后兼容——忽略它们的旧调用方仍然会在 TUI 上得到
    一个扁平列表。

    如果没有任何可用的显示机制则返回 None，此时子代理将在没有进度回调的
    情况下运行（与当前行为完全一致）。
    """
    spinner = getattr(parent_agent, "_delegate_spinner", None)
    parent_cb = getattr(parent_agent, "tool_progress_callback", None)

    if not spinner and not parent_cb:
        return None  # 无显示 → 无回调 → 行为零变化

    # 仅在批量模式（多个任务）下显示从 1 开始的下标前缀
    prefix = f"[{task_index + 1}] " if task_count > 1 else ""
    goal_label = (goal or "").strip()

    # 网关：批量收集工具名，定期刷出
    _BATCH_SIZE = 5
    _batch: List[str] = []
    _tool_count = [0]  # 按子代理累计的计数器（用 list 以便闭包内修改）

    def _identity_kwargs() -> Dict[str, Any]:
        kw: Dict[str, Any] = {
            "task_index": task_index,
            "task_count": task_count,
            "goal": goal_label,
        }
        if subagent_id is not None:
            kw["subagent_id"] = subagent_id
        if parent_id is not None:
            kw["parent_id"] = parent_id
        if depth is not None:
            kw["depth"] = depth
        if model is not None:
            kw["model"] = model
        if toolsets is not None:
            kw["toolsets"] = list(toolsets)
        # 子代理自身的 session id——等子代理存在后再填入共享引用（回调是先
        # 构建的），这样每个转发的事件都能让 UI 直接打开/查看子代理的会话。
        if session_ref and session_ref.get("session_id"):
            kw["child_session_id"] = str(session_ref["session_id"])
        kw["tool_count"] = _tool_count[0]
        return kw

    def _relay(
        event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs
    ):
        if not parent_cb:
            return
        payload = _identity_kwargs()
        payload.update(kwargs)  # 调用方的覆盖项（如 status、duration_seconds）
        try:
            parent_cb(event_type, tool_name, preview, args, **payload)
        except Exception as e:
            logger.debug("Parent callback failed: %s", e)

    def _callback(
        event_type, tool_name: str = None, preview: str = None, args=None, **kwargs
    ):
        # 由编排者自身发出的生命周期事件——在枚举归一化之前处理，因为它们
        # 不属于 DelegateEvent。
        if event_type == "subagent.start":
            if spinner and goal_label:
                short = (
                    (goal_label[:55] + "...") if len(goal_label) > 55 else goal_label
                )
                try:
                    spinner.print_above(f" {prefix}├─ 🔀 {short}")
                except Exception as e:
                    logger.debug("Spinner print_above failed: %s", e)
            _relay("subagent.start", preview=preview or goal_label or "", **kwargs)
            return

        if event_type == "subagent.complete":
            _relay("subagent.complete", preview=preview, **kwargs)
            return

        if event_type == "subagent.text":
            # 子代理流式输出的助手回复文本。原样转发，这样网关监视窗口就能
            # 在子代理「说话」时同步镜像。不回显到 spinner——CLI 通过树状
            # 视图展示子代理，而 CLI/TUI 的进度处理器会忽略非工具事件类型，
            # 因此在这里是无效的；只有网关监视窗口会消费它。
            _relay("subagent.text", preview=preview)
            return

        # 将旧版字符串、新式 "delegate.*" 字符串以及 DelegateEvent 枚举值
        # 全部归一化为单个 DelegateEvent。原始实现只接受五种旧版字符串；
        # 枚举类型的调用方会被静默丢弃。
        if isinstance(event_type, DelegateEvent):
            event = event_type
        else:
            event = _LEGACY_EVENT_MAP.get(event_type)
            if event is None:
                try:
                    event = DelegateEvent(event_type)
                except (ValueError, TypeError):
                    return  # 未知事件——忽略

        if event == DelegateEvent.TASK_THINKING:
            text = preview or tool_name or ""
            if spinner:
                short = (text[:55] + "...") if len(text) > 55 else text
                try:
                    spinner.print_above(f' {prefix}├─ 💭 "{short}"')
                except Exception as e:
                    logger.debug("Spinner print_above failed: %s", e)
            _relay("subagent.thinking", preview=text)
            return

        if event == DelegateEvent.TASK_TOOL_COMPLETED:
            return

        if event == DelegateEvent.TASK_PROGRESS:
            # 从嵌套编排者的孙代理转发过来的预批量进度摘要（上游以
            # parent_cb("subagent_progress", summary_string) 的形式发出，
            # 摘要落在 tool_name 这个位置参数槽里）。当作直通处理：用独立的
            # 方式渲染（不要走 tool-start 的 emoji 查找，否则会把摘要字符串
            # 误当作工具名），并且向上转发时不再重新批量。
            summary_text = tool_name or preview or ""
            if spinner and summary_text:
                try:
                    spinner.print_above(f" {prefix}├─ 🔀 {summary_text}")
                except Exception as e:
                    logger.debug("Spinner print_above failed: %s", e)
            if parent_cb:
                try:
                    parent_cb("subagent_progress", f"{prefix}{summary_text}")
                except Exception as e:
                    logger.debug("Parent callback relay failed: %s", e)
            return

        # TASK_TOOL_STARTED —— 显示并批量收集以便转发给父代理
        _tool_count[0] += 1
        if subagent_id is not None:
            with _active_subagents_lock:
                rec = _active_subagents.get(subagent_id)
                if rec is not None:
                    rec["tool_count"] = _tool_count[0]
                    rec["last_tool"] = tool_name or ""
        if spinner:
            short = (
                (preview[:35] + "...")
                if preview and len(preview) > 35
                else (preview or "")
            )
            from agent.display import get_tool_emoji

            emoji = get_tool_emoji(tool_name or "")
            line = f" {prefix}├─ {emoji} {tool_name}"
            if short:
                line += f'  "{short}"'
            try:
                spinner.print_above(line)
            except Exception as e:
                logger.debug("Spinner print_above failed: %s", e)

        if parent_cb:
            _relay("subagent.tool", tool_name, preview, args)
            _batch.append(tool_name or "")
            if len(_batch) >= _BATCH_SIZE:
                summary = ", ".join(_batch)
                _relay("subagent.progress", preview=f"🔀 {prefix}{summary}")
                _batch.clear()

    def _flush():
        """完成时把剩余的批量工具名刷出到网关。"""
        if parent_cb and _batch:
            summary = ", ".join(_batch)
            _relay("subagent.progress", preview=f"🔀 {prefix}{summary}")
            _batch.clear()

    _callback._flush = _flush
    return _callback


def _build_child_agent(
    task_index: int,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    model: Optional[str],
    max_iterations: int,
    task_count: int,
    parent_agent,
    # 来自委派配置的凭据覆盖项（provider:model 解析）
    override_provider: Optional[str] = None,
    override_base_url: Optional[str] = None,
    override_api_key: Optional[str] = None,
    override_api_mode: Optional[str] = None,
    # ACP 传输覆盖项——让非 ACP 父代理也能派生 ACP 子代理
    override_acp_command: Optional[str] = None,
    override_acp_args: Optional[List[str]] = None,
    # 控制子代理能否进一步委派的按调用角色。
    # 'leaf'（默认）不能再委派；'orchestrator' 在下方应用的深度/总开关边界内
    # 保留 delegation 工具集。
    role: str = "leaf",
):
    """
    在主线程上构建子 AIAgent（线程安全的构造过程）。
    返回构造好的子代理，但不运行它。

    当设置了 override_* 参数（来自委派配置）时，子代理会使用这些凭据，
    而不是从父代理继承。这使得把子代理路由到不同的 provider:model 组合
    成为可能（例如父代理运行在 Nous Portal 上时，把子代理路由到 OpenRouter
    上更便宜/更快的模型）。
    """
    from run_agent import AIAgent
    import uuid as _uuid

    # ── 角色解析 ───────────────────────────────────────────────────────
    # 仅当总开关和子代理深度都允许时，才尊重调用方传入的角色。这是 role
    # 降级为 'leaf' 的唯一入口——让规则可预测。调用方传入的是已归一化的
    # 角色（_normalize_role 已在 delegate_task 中运行过），因此这里只需
    # 处理 'leaf' 或 'orchestrator'。
    child_depth = getattr(parent_agent, "_delegate_depth", 0) + 1
    max_spawn = _get_max_spawn_depth()
    orchestrator_ok = _get_orchestrator_enabled() and child_depth < max_spawn
    effective_role = role if (role == "orchestrator" and orchestrator_ok) else "leaf"

    # ── 子代理身份（在各事件间稳定，对 TUI 从 0 开始计数）─────────────
    # subagent_id 在这里生成，这样进度回调、spawn_requested 事件以及
    # _active_subagents 注册表就能共用同一个键。parent_id 在【当前】父代理
    # 自身也是子代理时（嵌套的编排者 -> 工作者链路）才不为 None。
    subagent_id = f"sa-{task_index}-{_uuid.uuid4().hex[:8]}"
    parent_subagent_id = getattr(parent_agent, "_subagent_id", None)
    tui_depth = max(0, child_depth - 1)  # 0 = UI 中的第一层子代理

    delegation_cfg = _load_config()

    # 当未显式给出工具集时，从父代理已启用的工具集继承，这样被禁用的工具
    #（例如 web）就不会泄露给子代理。
    # 注意：enabled_toolsets=None 表示「所有工具都已启用」（默认值），因此
    # 我们必须从父代理已加载的工具名称推导出有效工具集。
    parent_enabled = getattr(parent_agent, "enabled_toolsets", None)
    if parent_enabled is not None:
        parent_toolsets = set(parent_enabled)
    elif parent_agent and hasattr(parent_agent, "valid_tool_names"):
        # enabled_toolsets 为 None（所有工具）——从已加载的工具名称推导
        import model_tools

        parent_toolsets = {
            ts
            for name in parent_agent.valid_tool_names
            if (ts := model_tools.get_toolset_for_tool(name)) is not None
        }
    else:
        parent_toolsets = set(DEFAULT_TOOLSETS)

    if toolsets:
        # 与父代理取交集——子代理绝不能获得父代理所没有的工具。
        # 展开组合工具集（例如 hermes-cli），这样在取交集时独立的工具集名称
        #（例如 web、terminal）才能被识别。
        expanded_parent = _expand_parent_toolsets(parent_toolsets)
        child_toolsets = [t for t in toolsets if t in expanded_parent]
        if _get_inherit_mcp_toolsets():
            child_toolsets = _preserve_parent_mcp_toolsets(
                child_toolsets, parent_toolsets
            )
        child_toolsets = _strip_blocked_tools(child_toolsets)
    elif parent_agent and parent_enabled is not None:
        child_toolsets = _strip_blocked_tools(parent_enabled)
    elif parent_toolsets:
        child_toolsets = _strip_blocked_tools(sorted(parent_toolsets))
    else:
        child_toolsets = _strip_blocked_tools(DEFAULT_TOOLSETS)

    # 编排者保留被 _strip_blocked_tools 移除的 'delegation' 工具集。这里的
    # 重新添加不依赖父工具集成员关系，因为编排者能力由角色授予，而非继承——
    # 设计理由见 test_intersection_preserves_delegation_bound 测试。
    if effective_role == "orchestrator" and "delegation" not in child_toolsets:
        child_toolsets.append("delegation")

    workspace_hint = _resolve_workspace_hint(parent_agent)
    child_prompt = _build_child_system_prompt(
        goal,
        context,
        workspace_path=workspace_hint,
        role=effective_role,
        max_spawn_depth=max_spawn,
        child_depth=child_depth,
    )
    # 提取父代理的 API key，使子代理继承鉴权（例如 Nous Portal）。
    parent_api_key = getattr(parent_agent, "api_key", None)
    if (not parent_api_key) and hasattr(parent_agent, "_client_kwargs"):
        parent_api_key = parent_agent._client_kwargs.get("api_key")

    # 尽早解析子代理的有效模型，这样它就能搭载到每个事件上。
    effective_model_for_cb = model or getattr(parent_agent, "model", None)

    # 构建进度回调，把工具调用转发给父代理显示层。
    # 身份关键字参数把 subagent_id 穿透到每个发出的事件里，这样 TUI 就能
    # 重建派生树并把按分支的控制路由回来。
    child_session_ref: Dict[str, Any] = {}
    child_progress_cb = _build_child_progress_callback(
        task_index,
        goal,
        parent_agent,
        task_count,
        subagent_id=subagent_id,
        parent_id=parent_subagent_id,
        depth=tui_depth,
        model=effective_model_for_cb,
        toolsets=child_toolsets,
        session_ref=child_session_ref,
    )

    # 每个子代理都拥有自己的迭代预算，上限为 max_iterations（可通过
    # delegation.max_iterations 配置，默认 50）。这意味着父代理 + 子代理
    # 的总迭代次数可能超过父代理的 max_iterations。用户可在 config.yaml 中
    # 控制每个子代理的上限。

    child_thinking_cb = None
    if child_progress_cb:

        def _child_thinking(text: str) -> None:
            if not text:
                return
            try:
                child_progress_cb("_thinking", text)
            except Exception as e:
                logger.debug("Child thinking callback relay failed: %s", e)

        child_thinking_cb = _child_thinking

    # 解析有效凭据：配置覆盖项 > 父代理继承
    effective_model = model or parent_agent.model
    effective_provider = override_provider or getattr(parent_agent, "provider", None)
    effective_base_url = override_base_url or parent_agent.base_url
    effective_api_key = override_api_key or parent_api_key
    # Bug #20558 / PR #20563：当子代理使用的 provider 与父代理不同时，绝不能
    # 继承 api_mode——每个 provider 都有各自的 API 接口形态（例如 MiniMax 用
    # anthropic_messages，DeepSeek 用 chat_completions）。继承父代理的 mode
    # 会在子代理路由到错误端点时导致 404 错误。当 provider 不同时，从目标
    # provider 重新推导 mode。
    _parent_provider = getattr(parent_agent, "provider", None) or ""
    if override_api_mode is not None:
        effective_api_mode = override_api_mode
    elif effective_provider != _parent_provider:
        effective_api_mode = None  # 强制从 provider 的默认值重新推导
    else:
        effective_api_mode = getattr(parent_agent, "api_mode", None)
    effective_acp_command = override_acp_command or getattr(
        parent_agent, "acp_command", None
    )
    effective_acp_args = list(
        override_acp_args
        if override_acp_args is not None
        else (getattr(parent_agent, "acp_args", []) or [])
    )

    # 当设置了 override_provider（例如 delegation.provider: minimax-cn）时，
    # 子代理必须使用直接的 API 调用——而不是父代理的 ACP 传输。无条件继承
    # acp_command 会导致 run_agent.py 初始化 CopilotACPClient，完全绕过覆盖
    # 凭据（issue #16816）。
    if override_provider and not override_acp_command:
        effective_acp_command = None
        effective_acp_args = []

    if override_acp_command:
        # 如果显式强制使用 ACP 传输覆盖项，provider 必须是 copilot-acp，
        # 这样 run_agent.py 才会初始化 CopilotACPClient。
        effective_provider = "copilot-acp"
        effective_api_mode = "chat_completions"

    # 解析推理配置：委派覆盖项 > 父代理继承
    parent_reasoning = getattr(parent_agent, "reasoning_config", None)
    child_reasoning = parent_reasoning
    try:
        delegation_effort = str(delegation_cfg.get("reasoning_effort") or "").strip()
        if delegation_effort:
            from hermes_constants import parse_reasoning_effort

            parsed = parse_reasoning_effort(delegation_effort)
            if parsed is not None:
                child_reasoning = parsed
            else:
                logger.warning(
                    "Unknown delegation.reasoning_effort '%s', inheriting parent level",
                    delegation_effort,
                )
    except Exception as exc:
        logger.debug("Could not load delegation reasoning_effort: %s", exc)

    # 继承父代理的 fallback provider 链，使子代理能够像顶层代理一样从
    # 限流和凭据耗尽中恢复。_fallback_chain 是一个列表，被 AIAgent 的
    # fallback_model 参数所接受（该参数同时支持列表和字典两种形式）。
    parent_fallback = getattr(parent_agent, "_fallback_chain", None) or None

    # 默认继承父代理的 OpenRouter provider 偏好过滤器（这样路由到同一
    # provider 的子代理就会遵循相同的路由约束）。但是：当设置了
    # `delegation.provider` 时，用户是明确要求子代理运行在另一个 provider
    # 上，此时父代理级别的 OpenRouter 过滤器（例如 `only=["Anthropic"]`）
    # 会把子代理静默地强制拉回父代理的 provider。在这种情况下清除过滤器，
    # 以尊重被委派的 provider。
    child_providers_allowed = getattr(parent_agent, "providers_allowed", None)
    child_providers_ignored = getattr(parent_agent, "providers_ignored", None)
    child_providers_order = getattr(parent_agent, "providers_order", None)
    child_provider_sort = getattr(parent_agent, "provider_sort", None)
    child_openrouter_min_coding_score = getattr(parent_agent, "openrouter_min_coding_score", None)
    if override_provider:
        child_providers_allowed = None
        child_providers_ignored = None
        child_providers_order = None
        child_provider_sort = None
        # 注意：openrouter_min_coding_score 受模型门控（仅在
        # openrouter/pareto-code 上发出），因此即使 provider 被覆盖我们仍然
        # 保持继承——它在任何其他模型上都是空操作。

    child = AIAgent(
        base_url=effective_base_url,
        api_key=effective_api_key,
        model=effective_model,
        provider=effective_provider,
        api_mode=effective_api_mode,
        acp_command=effective_acp_command,
        acp_args=effective_acp_args,
        max_iterations=max_iterations,
        max_tokens=getattr(parent_agent, "max_tokens", None),
        reasoning_config=child_reasoning,
        prefill_messages=getattr(parent_agent, "prefill_messages", None),
        fallback_model=parent_fallback,
        enabled_toolsets=child_toolsets,
        quiet_mode=True,
        ephemeral_system_prompt=child_prompt,
        log_prefix=f"[subagent-{task_index}]",
        platform="subagent",
        skip_context_files=True,
        skip_memory=True,
        clarify_callback=None,
        thinking_callback=child_thinking_cb,
        session_db=getattr(parent_agent, "_session_db", None),
        parent_session_id=getattr(parent_agent, "session_id", None),
        providers_allowed=child_providers_allowed,
        providers_ignored=child_providers_ignored,
        providers_order=child_providers_order,
        provider_sort=child_provider_sort,
        openrouter_min_coding_score=child_openrouter_min_coding_score,
        tool_progress_callback=child_progress_cb,
        iteration_budget=None,  # fresh budget per subagent
    )
    child._print_fn = getattr(parent_agent, "_print_fn", None)
    # 此时子代理已存在，它的 session id 可以搭载到每个转发事件上
    #（包括下面的 spawn_requested——第一次发出发生在此之后）。
    child_session_ref["session_id"] = getattr(child, "session_id", "") or ""
    # 设置委派深度，使子代理不能再派生孙代理
    child._delegate_depth = child_depth
    # 暂存降级后的角色，用于内省（当总开关或深度限制了调用方请求的角色时
    # 为 leaf）。
    child._delegate_role = effective_role
    # 暂存子代理身份，用于嵌套委派的事件传播，以及供 _run_single_child /
    # interrupt_subagent 按 id 查找。
    child._subagent_id = subagent_id
    child._parent_subagent_id = parent_subagent_id
    child._subagent_goal = goal
    child._parent_turn_id = getattr(parent_agent, "_current_turn_id", "") or ""
    # 稳定的侧栏标记：委派子代理会话必须排除在会话选择器之外，即使父代理
    # 被删除导致它们成为孤儿（parent_session_id → NULL）。这镜像了
    # /branch 的 ``_branched_from`` 模式——见 ``list_sessions_rich`` 的
    # 子代理排除条款。
    parent_sid = getattr(parent_agent, "session_id", None)
    if parent_sid and getattr(child, "_session_init_model_config", None) is not None:
        child._session_init_model_config["_delegate_from"] = parent_sid

    # 尽可能让子代理共享凭据池，使子代理在限流时能够轮换凭据，而不是被
    # 固定在单个 key 上。
    child_pool = _resolve_child_credential_pool(
        effective_provider, parent_agent, effective_base_url
    )
    if child_pool is not None:
        child._credential_pool = child_pool

    # 注册子代理以便中断传播
    if hasattr(parent_agent, "_active_children"):
        lock = getattr(parent_agent, "_active_children_lock", None)
        if lock:
            with lock:
                parent_agent._active_children.append(child)
        else:
            parent_agent._active_children.append(child)

    # 立即宣告派生——当 max_concurrent_children 饱和时，子代理可能在队列
    # 中等待数秒，因此 TUI 希望在运行开始前就有树中的一个节点。
    if child_progress_cb:
        try:
            child_progress_cb("subagent.spawn_requested", preview=goal)
        except Exception as exc:
            logger.debug("spawn_requested relay failed: %s", exc)

    try:
        from hermes_cli.plugins import invoke_hook as _invoke_hook
        _invoke_hook(
            "subagent_start",
            parent_session_id=getattr(parent_agent, "session_id", None),
            parent_turn_id=getattr(parent_agent, "_current_turn_id", "") or "",
            parent_subagent_id=parent_subagent_id,
            child_session_id=getattr(child, "session_id", None),
            child_subagent_id=subagent_id,
            child_role=effective_role,
            child_goal=goal,
        )
    except Exception:
        logger.debug("subagent_start hook invocation failed", exc_info=True)

    return child


def _dump_subagent_timeout_diagnostic(
    *,
    child: Any,
    task_index: int,
    timeout_seconds: float,
    duration_seconds: float,
    worker_thread: Optional[threading.Thread],
    goal: str,
) -> Optional[str]:
    """为在发起任何 API 调用之前就超时的子代理写入结构化的诊断转储。

    见 issue #14726：用户遇到「subagent timed out after 300s with no
    response」（子代理 300 秒后超时且无响应），且没有任何 API 调用，
    也无从查看发生了什么。此辅助函数会在 ``~/.hermes/logs/subagent-<sid>-<ts>.log``
    下写入一份专用日志，记录子代理的配置、系统提示词 / 工具 schema 大小、
    活动追踪器快照，以及超时时工作线程的 Python 调用栈。

    成功时返回诊断文件的绝对路径，失败时返回 None。
    """
    try:
        from hermes_constants import get_hermes_home
        import datetime as _dt
        import sys as _sys
        import traceback as _traceback

        hermes_home = get_hermes_home()
        logs_dir = hermes_home / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None

        subagent_id = getattr(child, "_subagent_id", None) or f"idx{task_index}"
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        dump_path = logs_dir / f"subagent-timeout-{subagent_id}-{ts}.log"

        lines: List[str] = []
        def _w(line: str = "") -> None:
            lines.append(line)

        _w(f"# Subagent timeout diagnostic — issue #14726")
        _w(f"# Generated: {_dt.datetime.now().isoformat()}")
        _w("")
        _w("## Timeout")
        _w(f"  task_index:        {task_index}")
        _w(f"  subagent_id:       {subagent_id}")
        _w(f"  configured_timeout: {timeout_seconds}s")
        _w(f"  actual_duration:   {duration_seconds:.2f}s")
        _w("")

        _w("## Goal")
        _goal_preview = (goal or "").strip()
        if len(_goal_preview) > 1000:
            _goal_preview = _goal_preview[:1000] + " ...[truncated]"
        _w(_goal_preview or "(empty)")
        _w("")

        _w("## Child config")
        for attr in (
            "model", "provider", "api_mode", "base_url", "max_iterations",
            "quiet_mode", "skip_memory", "skip_context_files", "platform",
            "_delegate_role", "_delegate_depth",
        ):
            try:
                val = getattr(child, attr, None)
                # 防御性地对 api_key 形态的值进行脱敏
                if isinstance(val, str) and attr == "base_url":
                    pass
                _w(f"  {attr}: {val!r}")
            except Exception:
                _w(f"  {attr}: <unreadable>")
        _w("")

        _w("## Toolsets")
        enabled = getattr(child, "enabled_toolsets", None)
        _w(f"  enabled_toolsets:  {enabled!r}")
        tool_names = getattr(child, "valid_tool_names", None)
        if tool_names:
            _w(f"  loaded tool count: {len(tool_names)}")
            try:
                _w(f"  loaded tools:      {sorted(tool_names)}")
            except Exception:
                pass
        _w("")

        _w("## Prompt / schema sizes")
        try:
            sys_prompt = getattr(child, "ephemeral_system_prompt", None) \
                or getattr(child, "system_prompt", None) \
                or ""
            _w(f"  system_prompt_bytes: {len(sys_prompt.encode('utf-8')) if isinstance(sys_prompt, str) else 'n/a'}")
            _w(f"  system_prompt_chars: {len(sys_prompt) if isinstance(sys_prompt, str) else 'n/a'}")
        except Exception as exc:
            _w(f"  system_prompt: <error: {exc}>")
        try:
            tools_schema = getattr(child, "tools", None)
            if tools_schema is not None:
                _schema_json = json.dumps(tools_schema, default=str)
                _w(f"  tool_schema_count: {len(tools_schema)}")
                _w(f"  tool_schema_bytes: {len(_schema_json.encode('utf-8'))}")
        except Exception as exc:
            _w(f"  tool_schema: <error: {exc}>")
        _w("")

        _w("## Activity summary")
        try:
            summary = child.get_activity_summary()
            for k, v in summary.items():
                _w(f"  {k}: {v!r}")
        except Exception as exc:
            _w(f"  <get_activity_summary failed: {exc}>")
        _w("")

        _w("## Worker thread stack at timeout")
        if worker_thread is not None and worker_thread.is_alive():
            frames = _sys._current_frames()
            worker_frame = frames.get(worker_thread.ident)
            if worker_frame is not None:
                stack = _traceback.format_stack(worker_frame)
                for frame_line in stack:
                    for sub in frame_line.rstrip().split("\n"):
                        _w(f"  {sub}")
            else:
                _w("  <worker frame not available>")
        elif worker_thread is None:
            _w("  <no worker thread handle>")
        else:
            _w("  <worker thread already exited>")
        _w("")

        _w("## Notes")
        _w("  This file is written ONLY when a subagent times out with 0 API calls.")
        _w("  0-API-call timeouts mean the child never reached its first LLM request.")
        _w("  Common causes: oversized prompt rejected by provider, transport hang,")
        _w("  credential resolution stuck. See issue #14726 for context.")

        dump_path.write_text("\n".join(lines), encoding="utf-8")
        return str(dump_path)
    except Exception as exc:
        logger.warning("Subagent timeout diagnostic dump failed: %s", exc)
        return None


def _run_single_child(
    task_index: int,
    goal: str,
    child=None,
    parent_agent=None,
    **_kwargs,
) -> Dict[str, Any]:
    """
    运行一个预构建好的子代理。从线程内部调用。
    返回一个结构化的结果字典。
    """
    child_start = time.monotonic()

    # 从子代理获取进度回调
    child_progress_cb = getattr(child, "tool_progress_callback", None)

    # 使用子代理构造前保存的值来恢复父代理工具名称。这才是正确的父代理
    # 工具集，而不是子代理的。
    import model_tools

    _saved_tool_names = getattr(
        child, "_delegate_saved_tool_names", list(model_tools._last_resolved_tool_names)
    )

    child_pool = getattr(child, "_credential_pool", None)
    leased_cred_id = None
    if child_pool is not None:
        leased_cred_id = child_pool.acquire_lease()
        if leased_cred_id is not None:
            try:
                leased_entry = child_pool.current()
                if leased_entry is not None and hasattr(child, "_swap_credential"):
                    child._swap_credential(leased_entry)
            except Exception as exc:
                logger.debug("Failed to bind child to leased credential: %s", exc)

    # 心跳：周期性地把子代理的活动传播给父代理，这样网关的不活跃超时就不
    # 会在子代理工作时触发。没有这个机制，父代理的 _last_activity_ts 会在
    # delegate_task 启动时冻结，网关最终会因为「无活动」而杀掉代理。
    _heartbeat_stop = threading.Event()
    # 陈旧检测：跨心跳周期跟踪子代理的 (tool, iteration) 对。如果两者都
    # 没有进展，就把该周期计为陈旧。空闲与工具内使用不同阈值
    #（见 _HEARTBEAT_STALE_CYCLES_*）。
    _last_seen_iter = [0]
    _last_seen_tool = [None]  # type: list
    _stale_count = [0]

    def _heartbeat_loop():
        while not _heartbeat_stop.wait(_HEARTBEAT_INTERVAL):
            if parent_agent is None:
                continue
            touch = getattr(parent_agent, "_touch_activity", None)
            if not touch:
                continue
            # 从子代理自身的活动追踪器中拉取详情
            desc = f"delegate_task: subagent {task_index} working"
            try:
                child_summary = child.get_activity_summary()
                child_tool = child_summary.get("current_tool")
                child_iter = child_summary.get("api_call_count", 0)
                child_max = child_summary.get("max_iterations", 0)

                # 陈旧检测：统计迭代次数和 current_tool 都没有进展的周期数。
                # 一个正在运行合法耗时工具（终端命令、网络抓取）的子代理会
                # 保持 current_tool 已设置，但不会推进 api_call_count——我们
                # 不希望这种情况在空闲阈值下被判定为陈旧。
                iter_advanced = child_iter > _last_seen_iter[0]
                tool_changed = child_tool != _last_seen_tool[0]
                if iter_advanced or tool_changed:
                    _last_seen_iter[0] = child_iter
                    _last_seen_tool[0] = child_tool
                    _stale_count[0] = 0
                else:
                    _stale_count[0] += 1

                # 根据子代理当前是否正在执行工具调用来选择阈值。工具内阈值
                # 足够高，能覆盖合法的慢速工具；空闲阈值保持紧凑，这样网关
                # 超时仍能在真正卡死的子代理上触发。
                stale_limit = (
                    _HEARTBEAT_STALE_CYCLES_IN_TOOL
                    if child_tool
                    else _HEARTBEAT_STALE_CYCLES_IDLE
                )
                if _stale_count[0] >= stale_limit:
                    logger.warning(
                        "Subagent %d appears stale (no progress for %d "
                        "heartbeat cycles, tool=%s) — stopping heartbeat",
                        task_index,
                        _stale_count[0],
                        child_tool or "<none>",
                    )
                    break  # 停止触碰父代理，让网关超时触发

                if child_tool:
                    desc = (
                        f"delegate_task: subagent running {child_tool} "
                        f"(iteration {child_iter}/{child_max})"
                    )
                else:
                    child_desc = child_summary.get("last_activity_desc", "")
                    if child_desc:
                        desc = (
                            f"delegate_task: subagent {child_desc} "
                            f"(iteration {child_iter}/{child_max})"
                        )
            except Exception:
                pass
            try:
                touch(desc)
            except Exception:
                pass

    _heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)

    # 把活跃代理注册到模块级注册表中，这样 TUI 就能按 subagent_id 定位它
    #（终止、暂停、状态查询）。在 finally 块中取消注册，即使子代理抛出异常
    # 也会执行。传入 MagicMock 测试替身时不会有稳定的 id；此时跳过注册。
    _raw_sid = getattr(child, "_subagent_id", None)
    _subagent_id = _raw_sid if isinstance(_raw_sid, str) else None
    if _subagent_id:
        _raw_depth = getattr(child, "_delegate_depth", 1)
        _tui_depth = max(0, _raw_depth - 1) if isinstance(_raw_depth, int) else 0
        _parent_sid = getattr(child, "_parent_subagent_id", None)
        _register_subagent(
            {
                "subagent_id": _subagent_id,
                "parent_id": _parent_sid if isinstance(_parent_sid, str) else None,
                "depth": _tui_depth,
                "goal": goal,
                "model": (
                    getattr(child, "model", None)
                    if isinstance(getattr(child, "model", None), str)
                    else None
                ),
                "started_at": time.time(),
                "status": "running",
                "tool_count": 0,
                "agent": child,
            }
        )

    try:
        _heartbeat_thread.start()
        if child_progress_cb:
            try:
                child_progress_cb("subagent.start", preview=goal)
            except Exception as e:
                logger.debug("Progress callback start failed: %s", e)

        # 文件状态协调：复用稳定的 subagent_id 作为子代理的 task_id，这样
        # file_state 写入、活跃子代理注册表以及 TUI 事件就能共用同一个键。
        # 仅当预构建的 id 不知何故缺失时，才回退到新生成的 uuid。
        import uuid as _uuid

        child_task_id = _subagent_id or f"subagent-{task_index}-{_uuid.uuid4().hex[:8]}"
        parent_task_id = getattr(parent_agent, "_current_task_id", None)
        wall_start = time.time()
        parent_reads_snapshot = (
            list(file_state.known_reads(parent_task_id)) if parent_task_id else []
        )

        # 运行子代理，可选地附带硬性超时（默认关闭——
        # result(timeout=None) 会阻塞到子代理完成）。卡死子代理的保护改由
        # 心跳陈旧度监视器提供。
        child_timeout = _get_child_timeout()
        _timeout_executor = ThreadPoolExecutor(
            max_workers=1,
            # 在工作线程中安装非交互式审批回调，使子代理的危险命令提示不会
            # 回退到 input() 从而导致父代理的 prompt_toolkit TUI 死锁。
            # 回调（拒绝还是批准）由 delegation.subagent_auto_approve 控制。
            initializer=_set_subagent_approval_cb,
            initargs=(_get_subagent_approval_callback(),),
        )
        # 捕获工作线程，这样超时诊断就能转储它的 Python 调用栈
        #（见 #14726——0 次 API 调用的挂起如果没有它会完全不透明）。
        _worker_thread_holder: Dict[str, Optional[threading.Thread]] = {"t": None}

        def _relay_child_text(delta: str) -> None:
            # 把子代理流式输出的回复文本向上转发到进度中继，这样网关监视
            # 窗口就能实时镜像它（subagent.text → message.delta）。
            # 在 CLI/TUI 下无效：它们的进度处理器会忽略非工具事件。
            if not delta or not child_progress_cb:
                return
            try:
                child_progress_cb("subagent.text", preview=delta)
            except Exception as e:
                logger.debug("Child text relay failed: %s", e)

        def _run_with_thread_capture():
            _worker_thread_holder["t"] = threading.current_thread()
            return child.run_conversation(
                user_message=goal,
                task_id=child_task_id,
                stream_callback=_relay_child_text,
            )

        _child_future = _timeout_executor.submit(_run_with_thread_capture)
        try:
            result = _child_future.result(timeout=child_timeout)
        except Exception as _timeout_exc:
            # 通知子代理停止，以便它的线程能干净地退出。
            try:
                if hasattr(child, "interrupt"):
                    child.interrupt()
                elif hasattr(child, "_interrupt_requested"):
                    child._interrupt_requested = True
            except Exception:
                pass

            is_timeout = isinstance(_timeout_exc, (FuturesTimeoutError, TimeoutError))
            duration = round(time.monotonic() - child_start, 2)
            logger.warning(
                "Subagent %d %s after %.1fs",
                task_index,
                "timed out" if is_timeout else f"raised {type(_timeout_exc).__name__}",
                duration,
            )

            # 当子代理在进行任何 API 调用【之前】超时时，转储一份诊断信息，
            # 帮助用户（和我们）看清子代理当时在做什么。
            # 见 #14726——没有它，0 次 API 调用的挂起就是一个黑盒。
            diagnostic_path: Optional[str] = None
            child_api_calls = 0
            try:
                _summary = child.get_activity_summary()
                child_api_calls = int(_summary.get("api_call_count", 0) or 0)
            except Exception:
                pass
            if is_timeout and child_api_calls == 0:
                diagnostic_path = _dump_subagent_timeout_diagnostic(
                    child=child,
                    task_index=task_index,
                    # is_timeout 意味着配置了上限（result(timeout=None) 永远
                    # 不会抛出 FuturesTimeoutError）；此处为类型检查器做防护。
                    timeout_seconds=float(child_timeout or 0.0),
                    duration_seconds=float(duration),
                    worker_thread=_worker_thread_holder.get("t"),
                    goal=goal,
                )
                if diagnostic_path:
                    logger.warning(
                        "Subagent %d 0-API-call timeout — diagnostic written to %s",
                        task_index,
                        diagnostic_path,
                    )

            if child_progress_cb:
                try:
                    child_progress_cb(
                        "subagent.complete",
                        preview=(
                            f"Timed out after {duration}s"
                            if is_timeout
                            else str(_timeout_exc)
                        ),
                        status="timeout" if is_timeout else "error",
                        duration_seconds=duration,
                        summary="",
                    )
                except Exception:
                    pass

            if is_timeout:
                if child_api_calls == 0:
                    _err = (
                        f"Subagent timed out after {child_timeout}s without "
                        f"making any API call — the child never reached its "
                        f"first LLM request (prompt construction, credential "
                        f"resolution, or transport may be stuck)."
                    )
                    if diagnostic_path:
                        _err += f" Diagnostic: {diagnostic_path}"
                else:
                    _err = (
                        f"Subagent timed out after {child_timeout}s with "
                        f"{child_api_calls} API call(s) completed — likely "
                        f"stuck on a slow API call or unresponsive network request."
                    )
            else:
                _err = str(_timeout_exc)

            return {
                "task_index": task_index,
                "status": "timeout" if is_timeout else "error",
                "summary": None,
                "error": _err,
                "exit_reason": "timeout" if is_timeout else "error",
                "api_calls": child_api_calls,
                "duration_seconds": duration,
                "_child_role": getattr(child, "_delegate_role", None),
                "diagnostic_path": diagnostic_path,
            }
        finally:
            # 不等待地关闭 executor——如果子代理线程卡在阻塞 I/O 上，
            # wait=True 会永远挂起。
            _timeout_executor.shutdown(wait=False)

        # 把剩余的批量进度刷出到网关
        if child_progress_cb and hasattr(child_progress_cb, "_flush"):
            try:
                child_progress_cb._flush()
            except Exception as e:
                logger.debug("Progress callback flush failed: %s", e)

        duration = round(time.monotonic() - child_start, 2)

        summary = result.get("final_response") or ""
        completed = result.get("completed", False)
        interrupted = result.get("interrupted", False)
        api_calls = result.get("api_calls", 0)

        if interrupted:
            status = "interrupted"
        elif summary:
            # 有 summary 说明子代理产出了可用输出。
            # exit_reason（"completed" 还是 "max_iterations"）已经告诉父代理
            # 任务是【如何】结束的。
            status = "completed"
        else:
            status = "failed"

        # 从对话消息（已在内存中）构建工具调用轨迹。
        # 使用 tool_call_id 来正确地把并行工具调用与结果配对。
        tool_trace: list[Dict[str, Any]] = []
        trace_by_id: Dict[str, Dict[str, Any]] = {}
        messages = result.get("messages") or []
        if isinstance(messages, list):
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") == "assistant":
                    for tc in msg.get("tool_calls") or []:
                        fn = tc.get("function", {})
                        entry_t = {
                            "tool": fn.get("name", "unknown"),
                            "args_bytes": len(fn.get("arguments", "")),
                        }
                        tool_trace.append(entry_t)
                        tc_id = tc.get("id")
                        if tc_id:
                            trace_by_id[tc_id] = entry_t
                elif msg.get("role") == "tool":
                    content = _stringify_tool_content(msg.get("content", ""))
                    is_error = _looks_like_error_output(content)
                    result_meta = {
                        "result_bytes": len(content),
                        "status": "error" if is_error else "ok",
                    }
                    # 对于并行调用，按 tool_call_id 匹配
                    tc_id = msg.get("tool_call_id")
                    target = trace_by_id.get(tc_id) if tc_id else None
                    if target is not None:
                        target.update(result_meta)
                    elif tool_trace:
                        # 没有 tool_call_id 的消息的回退方案
                        tool_trace[-1].update(result_meta)

        # 判断退出原因
        if interrupted:
            exit_reason = "interrupted"
        elif completed:
            exit_reason = "completed"
        else:
            exit_reason = "max_iterations"

        # 提取 token 计数（对 mock 对象安全）
        _input_tokens = getattr(child, "session_prompt_tokens", 0)
        _output_tokens = getattr(child, "session_completion_tokens", 0)
        _model = getattr(child, "model", None)

        entry: Dict[str, Any] = {
            "task_index": task_index,
            "status": status,
            "summary": summary,
            "api_calls": api_calls,
            "duration_seconds": duration,
            "model": _model if isinstance(_model, str) else None,
            "exit_reason": exit_reason,
            "tokens": {
                "input": (
                    _input_tokens if isinstance(_input_tokens, (int, float)) else 0
                ),
                "output": (
                    _output_tokens if isinstance(_output_tokens, (int, float)) else 0
                ),
            },
            "tool_trace": tool_trace,
            # 在 finally 块调用 child.close() 之前捕获，这样父代理线程就能
            # 用正确的角色触发 subagent_stop。
            # 在字典被序列化回模型之前会被剥离。
            "_child_role": getattr(child, "_delegate_role", None),
            # 在 child.close() 之前捕获，这样父代理聚合器就能把子代理的总
            # 花费并入父代理的会话成本。移植自 Kilo-Org/kilocode#9448——
            # 此前页脚只反映父代理直接的 API 调用，少算了子代理密集型运行。
            # 在字典被序列化回模型之前会被剥离。
            "_child_cost_usd": (
                float(getattr(child, "session_estimated_cost_usd", 0.0) or 0.0)
                if isinstance(
                    getattr(child, "session_estimated_cost_usd", 0.0),
                    (int, float),
                )
                else 0.0
            ),
        }
        if status == "failed":
            entry["error"] = result.get("error", "Subagent did not produce a response.")

        # 跨代理的文件状态提醒。如果此子代理写入了父代理已经读取过的文件，
        # 就把它呈现出来，让父代理知道在编辑之前要重新读取——这正是该注册表
        # 被创建的动机场景。我们检查的是【任何】非父代理 task_id 的写入
        #（不仅仅是这个子代理的），这样也能覆盖嵌套编排者→工作者链路带来的
        # 传递性写入。
        try:
            if parent_task_id and parent_reads_snapshot:
                sibling_writes = file_state.writes_since(
                    parent_task_id, wall_start, parent_reads_snapshot
                )
                if sibling_writes:
                    mod_paths = sorted(
                        {p for paths in sibling_writes.values() for p in paths}
                    )
                    if mod_paths:
                        reminder = (
                            "\n\n[NOTE: subagent modified files the parent "
                            "previously read — re-read before editing: "
                            + ", ".join(mod_paths[:8])
                            + (
                                f" (+{len(mod_paths) - 8} more)"
                                if len(mod_paths) > 8
                                else ""
                            )
                            + "]"
                        )
                        if entry.get("summary"):
                            entry["summary"] = entry["summary"] + reminder
                        else:
                            entry["stale_paths"] = mod_paths
        except Exception:
            logger.debug("file_state sibling-write check failed", exc_info=True)

        # 按分支的可观测性负载：token、成本、触碰的文件，以及工具调用结果的
        # 尾部。喂给 TUI 的 overlay 详情面板 + 折叠汇总（特性 1、2、4）。
        # 所有字段都是可选的——缺失的数据会在客户端优雅降级。
        _cost_usd = getattr(child, "session_estimated_cost_usd", None)
        _reasoning_tokens = getattr(child, "session_reasoning_tokens", 0)
        try:
            _files_read = list(file_state.known_reads(child_task_id))[:40]
        except Exception:
            _files_read = []
        try:
            _files_written_map = file_state.writes_since(
                "", wall_start, []
            )  # 自 wall_start 以来的所有写入
        except Exception:
            _files_written_map = {}
        _files_written = sorted(
            {
                p
                for tid, paths in _files_written_map.items()
                if tid == child_task_id
                for p in paths
            }
        )[:40]

        _output_tail = _extract_output_tail(result, max_entries=8, max_chars=600)

        complete_kwargs: Dict[str, Any] = {
            "preview": summary[:160] if summary else entry.get("error", ""),
            "status": status,
            "duration_seconds": duration,
            "summary": summary[:500] if summary else entry.get("error", ""),
            "input_tokens": (
                int(_input_tokens) if isinstance(_input_tokens, (int, float)) else 0
            ),
            "output_tokens": (
                int(_output_tokens) if isinstance(_output_tokens, (int, float)) else 0
            ),
            "reasoning_tokens": (
                int(_reasoning_tokens)
                if isinstance(_reasoning_tokens, (int, float))
                else 0
            ),
            "api_calls": int(api_calls) if isinstance(api_calls, (int, float)) else 0,
            "files_read": _files_read,
            "files_written": _files_written,
            "output_tail": _output_tail,
        }
        if _cost_usd is not None:
            try:
                complete_kwargs["cost_usd"] = float(_cost_usd)
            except (TypeError, ValueError):
                pass

        if child_progress_cb:
            try:
                child_progress_cb("subagent.complete", **complete_kwargs)
            except Exception as e:
                logger.debug("Progress callback completion failed: %s", e)

        return entry

    except Exception as exc:
        duration = round(time.monotonic() - child_start, 2)
        logging.exception(f"[subagent-{task_index}] failed")
        if child_progress_cb:
            try:
                child_progress_cb(
                    "subagent.complete",
                    preview=str(exc),
                    status="failed",
                    duration_seconds=duration,
                    summary=str(exc),
                )
            except Exception as e:
                logger.debug("Progress callback failure relay failed: %s", e)
        return {
            "task_index": task_index,
            "status": "error",
            "summary": None,
            "error": str(exc),
            "api_calls": 0,
            "duration_seconds": duration,
            "_child_role": getattr(child, "_delegate_role", None),
        }

    finally:
        # 停止心跳线程，使其不会在子代理完成（或失败）后继续触碰父代理活动。
        # 对 join 做防护：.start() 现在位于 try 块内，如果它抛出异常（OS 线程
        # 耗尽），线程从未启动，而 Thread.join() 会抛出 RuntimeError。
        # ident 在 start() 成功之前是 None。
        _heartbeat_stop.set()
        if _heartbeat_thread.ident is not None:
            _heartbeat_thread.join(timeout=5)

        # 丢弃面向 TUI 的注册表条目。即使子代理从未注册过（例如测试替身上
        # 缺少 ID）也可以安全调用。
        if _subagent_id:
            _unregister_subagent(_subagent_id)

        if child_pool is not None and leased_cred_id is not None:
            try:
                child_pool.release_lease(leased_cred_id)
            except Exception as exc:
                logger.debug("Failed to release credential lease: %s", exc)

        # 恢复父代理的工具名称，使进程级全局变量对后续的 execute_code 调用
        # 或其他消费者保持正确。
        import model_tools

        saved_tool_names = getattr(child, "_delegate_saved_tool_names", None)
        if isinstance(saved_tool_names, list):
            model_tools._last_resolved_tool_names = list(saved_tool_names)

        # 从活跃跟踪中移除子代理

        # 取消子代理的中断传播注册
        if hasattr(parent_agent, "_active_children"):
            try:
                lock = getattr(parent_agent, "_active_children_lock", None)
                if lock:
                    with lock:
                        parent_agent._active_children.remove(child)
                else:
                    parent_agent._active_children.remove(child)
            except (ValueError, UnboundLocalError) as e:
                logger.debug("Could not remove child from active_children: %s", e)

        # 关闭工具资源（终端沙箱、浏览器守护进程、后台进程、httpx 客户端），
        # 使子代理子进程不会比委派活得更久。
        try:
            if hasattr(child, "close"):
                child.close()
        except Exception:
            logger.debug("Failed to close child agent after delegation")


def _recover_tasks_from_json_string(
    tasks: Any,
) -> tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    if not isinstance(tasks, str):
        return None, None
    raw = tasks.strip()
    if not raw:
        return None, "Provide either 'goal' (single task) or 'tasks' (batch)."
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, (
            "tasks must be a JSON array of task objects; received a string "
            f"that could not be parsed as JSON ({exc.msg})."
        )
    if not isinstance(parsed, list):
        return None, (
            f"tasks must be a JSON array of task objects; parsed "
            f"{type(parsed).__name__} instead."
        )
    return parsed, None


def delegate_task(
    goal: Optional[str] = None,
    context: Optional[str] = None,
    toolsets: Optional[List[str]] = None,
    tasks: Optional[List[Dict[str, Any]]] = None,
    max_iterations: Optional[int] = None,
    acp_command: Optional[str] = None,
    acp_args: Optional[List[str]] = None,
    role: Optional[str] = None,
    background: Optional[bool] = None,
    parent_agent=None,
) -> str:
    """
    派生一个或多个子代理来处理委派的任务。

    支持两种模式：
      - 单任务：提供 goal（可选 context、toolsets、role）
      - 批量：  提供 tasks 数组 [{goal, context, toolsets, role}, ...]

    'role' 参数控制子代理能否进一步委派：'leaf'（默认）不能；
    'orchestrator' 保留 delegation 工具集，可以派生自己的工作者，
    但受 delegation.max_spawn_depth 约束。按任务的 role 优先于顶层 role。

    返回包含 results 数组的 JSON，每个任务对应一条记录。
    """
    if parent_agent is None:
        return tool_error("delegate_task requires a parent agent context.")

    # 运维人员控制的总开关——让 TUI 在检测到失控的派生树时冻结新的扇出，
    # 而不打断已在运行的子代理。通过配套的 `delegation.pause` RPC 清除。
    if is_spawn_paused():
        return tool_error(
            "Delegation spawning is paused. Clear the pause via the TUI "
            "(`p` in /agents) or the `delegation.pause` RPC before retrying."
        )

    # 顶层角色只归一化一次；按任务的覆盖项会再次归一化。
    top_role = _normalize_role(role)

    # 后台（异步）委派现在同时适用于单任务和批量任务。批量任务会简单地变成
    # N 个独立的异步派发：每个子代理运行在守护执行器上，并通过完成队列自行
    # 重新进入对话，各自携带自己的句柄。这里没有合并的「等待全部完成」——
    # 扇出恰好就是 N 个后台子代理。
    background = is_truthy_value(background, default=False) if background is not None else False

    # 深度限制——可通过 delegation.max_spawn_depth 配置，默认值 2 与原始的
    # MAX_DEPTH 常量保持一致。
    depth = getattr(parent_agent, "_delegate_depth", 0)
    max_spawn = _get_max_spawn_depth()
    if depth >= max_spawn:
        return json.dumps(
            {
                "error": (
                    f"Delegation depth limit reached (depth={depth}, "
                    f"max_spawn_depth={max_spawn}). Raise "
                    f"delegation.max_spawn_depth in config.yaml if deeper "
                    f"nesting is required (no hard ceiling, but each level "
                    f"multiplies API cost)."
                )
            }
        )

    # 加载配置
    cfg = _load_config()
    default_max_iter = cfg.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    # 模型提供的 max_iterations 会被忽略——配置值才是权威的，这样用户能得到
    # 可预测的预算。该关键字参数保留给内部调用方和测试使用；此处如果出现
    # 模型发出的值，只会缩减预算并在运行中途给用户带来意外。如果某个值从
    # 缓存的工具 schema 或陈旧的 provider 中漏过来，就记录日志并丢弃。
    if max_iterations is not None and max_iterations != default_max_iter:
        logger.debug(
            "delegate_task: ignoring caller-supplied max_iterations=%s; "
            "using delegation.max_iterations=%s from config",
            max_iterations, default_max_iter,
        )
    effective_max_iter = default_max_iter

    # 解析委派凭据（provider:model 对）。
    # 当配置了 delegation.provider 时，这里会通过 CLI/网关启动所使用的同一个
    # 运行时 provider 系统来解析完整的凭据包（base_url、api_key、api_mode）。
    # 未配置时返回 None 值，使子代理从父代理继承。
    try:
        creds = _resolve_delegation_credentials(cfg, parent_agent)
    except ValueError as exc:
        return tool_error(str(exc))

    # 归一化为任务列表
    max_children = _get_max_concurrent_children()
    recovered_tasks, tasks_error = _recover_tasks_from_json_string(tasks)
    if tasks_error:
        return tool_error(tasks_error)
    if recovered_tasks is not None:
        tasks = recovered_tasks

    if tasks and isinstance(tasks, list):
        if len(tasks) > max_children:
            return tool_error(
                f"Too many tasks: {len(tasks)} provided, but "
                f"max_concurrent_children is {max_children}. "
                f"Either reduce the task count, split into multiple "
                f"delegate_task calls, or increase "
                f"delegation.max_concurrent_children in config.yaml."
            )
        task_list = tasks
    elif goal and isinstance(goal, str) and goal.strip():
        task_list = [
            {"goal": goal, "context": context, "toolsets": toolsets, "role": top_role}
        ]
    else:
        return tool_error("Provide either 'goal' (single task) or 'tasks' (batch).")

    if not task_list:
        return tool_error("No tasks provided.")

    # 校验每个任务都带有 goal
    for i, task in enumerate(task_list):
        if not isinstance(task, dict):
            return tool_error(
                f"Task {i} must be an object, got {type(task).__name__}."
            )
        if not task.get("goal", "").strip():
            return tool_error(f"Task {i} is missing a 'goal'.")

    overall_start = time.monotonic()
    results = []

    n_tasks = len(task_list)
    # 记录 goal 标签用于进度展示（为可读性而截断）
    task_labels = [t["goal"][:40] for t in task_list]

    # 在任何子代理构造改动全局变量之前，保存父代理工具名称。
    # _build_child_agent() 会调用 AIAgent()，后者会调用 get_tool_definitions()，
    # 它会用子代理的工具集覆盖 model_tools._last_resolved_tool_names。
    import model_tools as _model_tools

    _parent_tool_names = list(_model_tools._last_resolved_tool_names)

    # 在主线程上构建所有子代理（线程安全的构造过程）
    # 用 try/finally 包裹，这样即使某个子代理构建抛出异常，全局变量也总是
    # 会被恢复（否则 _last_resolved_tool_names 会一直处于损坏状态）。
    children = []
    try:
        for i, t in enumerate(task_list):
            task_acp_args = t.get("acp_args") if "acp_args" in t else None
            # 按任务的 role 优先于顶层；再次归一化，使未知的按任务取值统一
            # 地记录警告并降级为 leaf。
            effective_role = _normalize_role(t.get("role") or top_role)
            child = _build_child_agent(
                task_index=i,
                goal=t["goal"],
                context=t.get("context"),
                toolsets=t.get("toolsets") or toolsets,
                model=creds["model"],
                max_iterations=effective_max_iter,
                task_count=n_tasks,
                parent_agent=parent_agent,
                override_provider=creds["provider"],
                override_base_url=creds["base_url"],
                override_api_key=creds["api_key"],
                override_api_mode=creds["api_mode"],
                override_acp_command=t.get("acp_command")
                or acp_command
                or creds.get("command"),
                override_acp_args=(
                    task_acp_args
                    if task_acp_args is not None
                    else (acp_args if acp_args is not None else creds.get("args"))
                ),
                role=effective_role,
            )
            # 用正确的父代理工具名称覆盖（在子代理构造改动全局之前）
            child._delegate_saved_tool_names = _parent_tool_names
            children.append((i, t, child))
    finally:
        # 权威恢复：所有子代理构建完成后，把全局变量重置为父代理的工具名称
        _model_tools._last_resolved_tool_names = _parent_tool_names

    def _execute_and_aggregate() -> dict:
        """运行所有构建好的子代理（1 个或 N 个），等待它们汇合，聚合结果，
        触发 subagent_stop 钩子 + 成本汇总，并返回合并后的结果字典。同时
        供同步路径和后台运行器使用。在后台场景下，整个函数运行在守护执行器
        上，因此父代理回合不会被阻塞——但批量任务仍然会在这里【等待自身
        汇合】（所有子代理必须完成）后，才产出一个合并的结果块。这就是契约：
        扇出在后台运行，彼此相互等待，最后一起返回。
        """
        if n_tasks == 1:
            # 单任务——直接运行（无线程池开销）
            _i, _t, child = children[0]
            result = _run_single_child(_i, _t["goal"], child, parent_agent)
            results.append(result)
        else:
            # 批量任务——并行运行，每个任务一行进度
            completed_count = 0
            spinner_ref = getattr(parent_agent, "_delegate_spinner", None)

            with ThreadPoolExecutor(max_workers=max_children) as executor:
                futures = {}
                for i, t, child in children:
                    future = executor.submit(
                        _run_single_child,
                        task_index=i,
                        goal=t["goal"],
                        child=child,
                        parent_agent=parent_agent,
                    )
                    futures[future] = i

                # 带中断检查地轮询 future。as_completed() 会阻塞到【所有】
                # future 完成——如果某个子代理卡住，即使中断传播之后父代理
                # 也会永远阻塞。改为使用带短超时的 wait()，这样当父代理被
                # 中断时我们就能退出。
                # 把 task_index 映射到子代理，这样为仍在挂起的 future 伪造的
                # 条目就能携带正确的 _delegate_role。
                _child_by_index = {i: child for (i, _, child) in children}

                pending = set(futures.keys())
                while pending:
                    if getattr(parent_agent, "_interrupt_requested", False) is True:
                        # 父代理被中断——收集已完成的部分，放弃其余的。子代理
                        # 已经收到中断信号；我们只是不能永远等下去。
                        for f in pending:
                            idx = futures[f]
                            if f.done():
                                try:
                                    entry = f.result()
                                except Exception as exc:
                                    entry = {
                                        "task_index": idx,
                                        "status": "error",
                                        "summary": None,
                                        "error": str(exc),
                                        "api_calls": 0,
                                        "duration_seconds": 0,
                                        "_child_role": getattr(
                                            _child_by_index.get(idx), "_delegate_role", None
                                        ),
                                    }
                            else:
                                entry = {
                                    "task_index": idx,
                                    "status": "interrupted",
                                    "summary": None,
                                    "error": "Parent agent interrupted — child did not finish in time",
                                    "api_calls": 0,
                                    "duration_seconds": 0,
                                    "_child_role": getattr(
                                        _child_by_index.get(idx), "_delegate_role", None
                                    ),
                                }
                            results.append(entry)
                            completed_count += 1
                        break

                    from concurrent.futures import wait as _cf_wait, FIRST_COMPLETED

                    done, pending = _cf_wait(
                        pending, timeout=0.5, return_when=FIRST_COMPLETED
                    )
                    for future in done:
                        try:
                            entry = future.result()
                        except Exception as exc:
                            idx = futures[future]
                            entry = {
                                "task_index": idx,
                                "status": "error",
                                "summary": None,
                                "error": str(exc),
                                "api_calls": 0,
                                "duration_seconds": 0,
                                "_child_role": getattr(
                                    _child_by_index.get(idx), "_delegate_role", None
                                ),
                            }
                        results.append(entry)
                        completed_count += 1

                        # 在 spinner 之上打印每个任务的完成行
                        idx = entry["task_index"]
                        label = (
                            task_labels[idx] if idx < len(task_labels) else f"Task {idx}"
                        )
                        dur = entry.get("duration_seconds", 0)
                        status = entry.get("status", "?")
                        icon = "✓" if status == "completed" else "✗"
                        remaining = n_tasks - completed_count
                        completion_line = f"{icon} [{idx+1}/{n_tasks}] {label}  ({dur}s)"
                        if spinner_ref:
                            try:
                                spinner_ref.print_above(completion_line)
                            except Exception:
                                print(f"  {completion_line}")
                        else:
                            print(f"  {completion_line}")

                        # 更新 spinner 文本以显示剩余数量
                        if spinner_ref and remaining > 0:
                            try:
                                spinner_ref.update_text(
                                    f"🔀 {remaining} task{'s' if remaining != 1 else ''} remaining"
                                )
                            except Exception as e:
                                logger.debug("Spinner update_text failed: %s", e)

            # 按 task_index 排序，使结果与输入顺序一致
            results.sort(key=lambda r: r["task_index"])

        # 把委派结果通知给父代理的 memory provider
        if (
            parent_agent
            and hasattr(parent_agent, "_memory_manager")
            and parent_agent._memory_manager
        ):
            for entry in results:
                try:
                    _task_goal = (
                        task_list[entry["task_index"]]["goal"]
                        if entry["task_index"] < len(task_list)
                        else ""
                    )
                    parent_agent._memory_manager.on_delegation(
                        task=_task_goal,
                        result=entry.get("summary", "") or "",
                        child_session_id=(
                            getattr(children[entry["task_index"]][2], "session_id", "")
                            if entry["task_index"] < len(children)
                            else ""
                        ),
                    )
                except Exception:
                    pass

        # 每个子代理触发一次 subagent_stop 钩子，在父代理线程上串行执行。
        # 这样可以把 Python 插件和 shell 钩子回调排除在运行子代理的工作线程
        # 之外，钩子作者就不必考虑并发调用的问题。role 已在 _run_single_child
        #（或上面伪造条目的分支）中、子代理被关闭之前捕获到了 entry 字典里。
        _parent_session_id = getattr(parent_agent, "session_id", None)
        try:
            from hermes_cli.plugins import invoke_hook as _invoke_hook
        except Exception:
            _invoke_hook = None
        # 在这里聚合子代理花费，使父代理的页脚/UI 能反映子代理密集型回合的
        # 真实成本。移植自 Kilo-Org/kilocode#9448。每个子代理的成本已在
        # _run_single_child 中、其 AIAgent 被关闭之前捕获；我们在这里和
        # subagent_stop 钩子循环一起一次性并入父代理，这样就不必遍历 `results`
        # 两次。
        _children_cost_total = 0.0
        for entry in results:
            child_role = entry.pop("_child_role", None)
            child_cost = entry.pop("_child_cost_usd", 0.0)
            try:
                if child_cost:
                    _children_cost_total += float(child_cost)
            except (TypeError, ValueError):
                pass
            if _invoke_hook is None:
                continue
            try:
                _child_index = entry.get("task_index", -1)
                _child_agent = (
                    children[_child_index][2]
                    if isinstance(_child_index, int) and 0 <= _child_index < len(children)
                    else None
                )
                _invoke_hook(
                    "subagent_stop",
                    parent_session_id=_parent_session_id,
                    parent_turn_id=getattr(parent_agent, "_current_turn_id", "") or "",
                    child_session_id=getattr(_child_agent, "session_id", None),
                    child_role=child_role,
                    child_summary=entry.get("summary"),
                    child_status=entry.get("status"),
                    duration_ms=int((entry.get("duration_seconds") or 0) * 1000),
                )
            except Exception:
                logger.debug("subagent_stop hook invocation failed", exc_info=True)

        # 把聚合后的子代理成本并入父代理的会话总计。这是累加的——每次
        # delegate_task 调用贡献各自的子代理——因此嵌套的编排者→工作者树会
        # 自然地向上汇总：每一层自己的 delegate_task() 并入它的直接子代理，
        # 而当编排者自身完成时，它的父代理再把编排者现在已经膨胀的总计叠加
        # 上去。如果父代理缺少计数器（较旧的测试夹具等），则静默降级。
        if _children_cost_total > 0.0:
            try:
                current = float(getattr(parent_agent, "session_estimated_cost_usd", 0.0) or 0.0)
                parent_agent.session_estimated_cost_usd = current + _children_cost_total
                # 升级 cost_source，这样当父代理自身尚未计费任何调用时
                #（罕见但可能，当父代理本回合唯一的动作就是 delegate_task 时），
                # UI 就不会把一个部分真实的总计标记为「none」。
                if getattr(parent_agent, "session_cost_source", "none") in {None, "", "none"}:
                    parent_agent.session_cost_source = "subagent"
                if getattr(parent_agent, "session_cost_status", "unknown") in {None, "", "unknown"}:
                    parent_agent.session_cost_status = "estimated"
            except Exception:
                logger.debug("Subagent cost rollup failed", exc_info=True)

        total_duration = round(time.monotonic() - overall_start, 2)

        return {
            "results": results,
            "total_duration_seconds": total_duration,
        }

    # ----- 后台派发：把整个批量任务作为一个异步单元运行 -----
    # 当 background 为 true 时，整个扇出通过一次异步委派运行在守护执行器上。
    # _execute_and_aggregate() 会等待每个子代理汇合，并产出一个合并的结果块，
    # 该块在【所有】子代理完成时作为一条消息重新进入对话。期间聊天不会被
    # 阻塞。这就是契约：派发 N 个子代理，继续聊天，最后一起拿到合并的摘要。
    if background:
        from tools.async_delegation import dispatch_async_delegation_batch
        from tools.approval import get_current_session_key

        # 无状态的请求/响应会话（API server / WebUI 路径）无法在回合结束后把
        # 分离的子代理结果路由回代理——没有持久通道，并且适配器的 send() 是
        # 空操作，因此后台派发会静默地永远不重新进入对话（issue #10760）。
        # 回退到【同步】执行：工作仍然会运行，其结果会在本次响应中返回，
        # 这严格优于一个永远不兑现的句柄。镜像下方池满时的内联回退逻辑。
        try:
            from gateway.session_context import async_delivery_supported
            _async_ok = async_delivery_supported()
        except Exception:
            _async_ok = True
        if not _async_ok:
            logger.info(
                "delegate_task: async delivery unsupported on this session "
                "(stateless HTTP API); running the batch synchronously instead."
            )
            _sync_result = _execute_and_aggregate()
            if isinstance(_sync_result, dict):
                _sync_result["note"] = (
                    "background=true is not available on this endpoint (stateless "
                    "HTTP API — no channel to deliver a detached subagent result "
                    "after the turn ends), so the subagent(s) ran SYNCHRONOUSLY and "
                    "the result is included above."
                )
            return json.dumps(_sync_result, ensure_ascii=False)

        _session_key = get_current_session_key(default="")
        _child_agents = [c for (_, _, c) in children]

        # 把每个子代理从父代理的中断传播列表中分离——批量的生命周期现在
        # 由异步注册表拥有，而不是父代理回合。_build_child_agent 之前把它们
        # 挂载上去（这对同步运行是正确的）。
        if hasattr(parent_agent, "_active_children"):
            _ac_lock = getattr(parent_agent, "_active_children_lock", None)
            for _c in _child_agents:
                try:
                    if _ac_lock:
                        with _ac_lock:
                            parent_agent._active_children.remove(_c)
                    else:
                        parent_agent._active_children.remove(_c)
                except ValueError:
                    pass

        def _batch_runner():
            return _execute_and_aggregate()

        def _batch_interrupt():
            for _c in _child_agents:
                try:
                    if hasattr(_c, "interrupt"):
                        _c.interrupt("Async delegation cancelled")
                    elif hasattr(_c, "_interrupt_requested"):
                        _c._interrupt_requested = True
                except Exception:
                    pass

        _goals = [t["goal"] for t in task_list]
        dispatch = dispatch_async_delegation_batch(
            goals=_goals,
            context=context,
            toolsets=toolsets,
            role=top_role,
            model=creds["model"],
            session_key=_session_key,
            runner=_batch_runner,
            interrupt_fn=_batch_interrupt,
            max_async_children=_get_max_async_children(),
        )

        if dispatch.get("status") == "dispatched":
            n = len(_goals)
            note = (
                "Subagent is running in the background. You and the user can "
                "keep working; its full result re-enters the conversation as a "
                "new message when it finishes. Do not wait or poll — just "
                "continue."
                if n == 1 else
                f"{n} subagents are running in parallel in the background. You "
                f"and the user can keep working; they wait on each other and "
                f"their consolidated results re-enter the conversation as a "
                f"single message once ALL of them finish. Do not wait or poll "
                f"— just continue."
            )
            payload = {
                "status": "dispatched",
                "mode": "background",
                "count": n,
                "delegation_id": dispatch["delegation_id"],
                "goals": _goals,
                "note": note,
            }
            return json.dumps(payload, ensure_ascii=False)

        # 池已满 / 调度失败——子代理仍然挂载着（我们上面只在父代理列表上
        # 做分离，但异步单元从未被接受，因此不需要重新挂载：我们直接内联运行）。
        logger.info(
            "delegate_task: async pool at capacity (%s); running the whole "
            "batch synchronously instead.",
            dispatch.get("error", "rejected"),
        )
        return json.dumps(_execute_and_aggregate(), ensure_ascii=False)

    # ----- 同步路径 -----
    return json.dumps(_execute_and_aggregate(), ensure_ascii=False)


def _resolve_child_credential_pool(
    effective_provider: Optional[str],
    parent_agent,
    effective_base_url: Optional[str] = None,
):
    """为子代理解析凭据池。

    规则：
    1. 与父代理同一个 provider -> 共享父代理的池，使冷却状态和轮换保持同步。
    2. 不同 provider -> 尝试加载该 provider 自己的池。
    3. 没有可用池 -> 返回 None，让子代理保持继承来的固定凭据行为。

    自定义端点是特殊情况：每个直接的 ``delegation.base_url`` 运行时都会坍缩
    为 ``provider="custom"``，因此单纯的 provider 相等性会把两个【不同的】
    自定义端点视为可互换，让子代理继承父代理的池。从该池租约会用父代理的
    端点覆盖子代理被委派的 ``base_url``（issue #7833）。因此我们按端点身份
    （从 base_url 派生出的 ``custom:<name>`` 池键）来解析自定义运行时，并且
    只有当两者都解析到【同一个】自定义端点时才共享父代理的池。
    """
    if not effective_provider:
        return getattr(parent_agent, "_credential_pool", None)

    parent_provider = getattr(parent_agent, "provider", None) or ""
    parent_pool = getattr(parent_agent, "_credential_pool", None)

    # 自定义端点：按端点身份区分，而不是按单纯的「custom」provider 字符串。
    # 两个自定义运行时只有在解析到同一个 custom:<name> 池键时才可互换。
    if effective_provider == "custom":
        try:
            from agent.credential_pool import get_custom_provider_pool_key, load_pool

            child_key = get_custom_provider_pool_key(effective_base_url)
            if child_key is None:
                # 未注册的端点（原始 delegation.base_url，没有匹配的
                # custom_providers 条目）-> 不存在共享池。保留子代理固定的
                # 被委派凭据，而不是冒险继承父代理的自定义端点。
                return None

            # 仅当是同一个自定义端点时才复用父代理的池。
            parent_key = get_custom_provider_pool_key(
                getattr(parent_agent, "base_url", None)
            )
            if (
                parent_pool is not None
                and parent_provider == "custom"
                and parent_key is not None
                and parent_key == child_key
            ):
                return parent_pool

            pool = load_pool(child_key)
            if pool is not None and pool.has_credentials():
                return pool
        except Exception as exc:
            logger.debug(
                "Could not resolve custom credential pool for child endpoint '%s': %s",
                effective_base_url,
                exc,
            )
        return None

    if parent_pool is not None and effective_provider == parent_provider:
        return parent_pool

    try:
        from agent.credential_pool import load_pool

        pool = load_pool(effective_provider)
        if pool is not None and pool.has_credentials():
            return pool
    except Exception as exc:
        logger.debug(
            "Could not load credential pool for child provider '%s': %s",
            effective_provider,
            exc,
        )
    return None


def _resolve_delegation_credentials(cfg: dict, parent_agent) -> dict:
    """为子代理委派解析凭据。

    如果配置了 ``delegation.base_url``，子代理会使用那个直接的
    OpenAI 兼容端点。``delegation.api_key`` 会覆盖 key；省略时
    ``api_key`` 返回为 ``None``，这样 ``_build_child_agent`` 会继承父代理
    的 key（``effective_api_key = override_api_key or parent_api_key``）。
    这使得把 key 存在 ``OPENAI_API_KEY`` 之外的 provider（例如
    ``MINIMAX_API_KEY``、``DASHSCOPE_API_KEY``）无需重复的配置条目即可工作。

    否则，如果配置了 ``delegation.provider``，完整的凭据包
    （base_url、api_key、api_mode、provider）会通过运行时 provider 系统解析
    ——与 CLI/网关启动所使用的路径相同。这让子代理可以运行在完全不同的
    provider:model 对上。

    如果 base_url 和 provider 都未配置，返回 None 值，使子代理从父代理
    继承一切。

    凭据失败时抛出带有用户友好信息的 ValueError。
    """
    configured_model = str(cfg.get("model") or "").strip() or None
    configured_provider = str(cfg.get("provider") or "").strip() or None
    configured_base_url = str(cfg.get("base_url") or "").strip() or None
    configured_api_key = str(cfg.get("api_key") or "").strip() or None
    configured_api_mode = str(cfg.get("api_mode") or "").strip().lower() or None

    if configured_base_url:
        # 当 delegation.api_key 未设置时返回 None，这样 _build_child_agent 会
        # 通过凭据继承路径回退到父代理的 API key
        #（effective_api_key = override_api_key or parent_api_key）。这让把 key
        # 存在非 OPENAI_API_KEY 环境变量中的 provider（例如 MINIMAX_API_KEY、
        # DASHSCOPE_API_KEY）无需调用方在 delegation.api_key 下重复配置 key 即可工作。
        api_key = configured_api_key  # None → 在 _build_child_agent 中从父代理继承

        # 使用共享的、基于 URL 的 api_mode 探测器（与主代理运行时解析器使用的
        # 路径相同），这样带 /anthropic 后缀的 Anthropic 兼容直连端点——Azure AI
        # Foundry、MiniMax、Zhipu GLM、LiteLLM 代理——能自动选择正确的传输。
        # 没有这个，子代理会默认使用 chat_completions，并在只说 Anthropic
        # Messages 协议的端点上命中 404。修复 #10213。
        from hermes_cli.runtime_provider import _detect_api_mode_for_url

        base_lower = configured_base_url.lower()
        provider = "custom"
        api_mode = _detect_api_mode_for_url(configured_base_url) or "chat_completions"
        if (
            base_url_hostname(configured_base_url) == "chatgpt.com"
            and "/backend-api/codex" in base_lower
        ):
            provider = "openai-codex"
            api_mode = "codex_responses"
        elif base_url_hostname(configured_base_url) == "api.anthropic.com":
            provider = "anthropic"
            api_mode = "anthropic_messages"
        elif "api.kimi.com/coding" in base_lower:
            provider = "custom"
            api_mode = "anthropic_messages"

        # 配置中显式的 delegation.api_mode 总是优先。让用户可以为 URL 启发式
        # 无法探测的非标准端点强制指定传输方式。
        if configured_api_mode in {"chat_completions", "codex_responses", "anthropic_messages"}:
            api_mode = configured_api_mode

        return {
            "model": configured_model,
            "provider": provider,
            "base_url": configured_base_url,
            "api_key": api_key,
            "api_mode": api_mode,
        }

    if not configured_provider:
        # 没有 provider 覆盖——子代理从父代理继承一切
        return {
            "model": configured_model,
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
        }

    # 已配置 provider——解析完整凭据
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested=configured_provider, target_model=configured_model)
    except Exception as exc:
        raise ValueError(
            f"Cannot resolve delegation provider '{configured_provider}': {exc}. "
            f"Check that the provider is configured (API key set, valid provider name), "
            f"or set delegation.base_url/delegation.api_key for a direct endpoint. "
            f"Available providers: openrouter, nous, zai, kimi-coding, minimax."
        ) from exc

    api_key = runtime.get("api_key", "")
    if not api_key:
        raise ValueError(
            f"Delegation provider '{configured_provider}' resolved but has no API key. "
            f"Set the appropriate environment variable or run 'hermes auth'."
        )

    return {
        "model": configured_model or runtime.get("model") or None,
        "provider": configured_provider if runtime.get("provider") == _RUNTIME_PROVIDER_CUSTOM else runtime.get("provider"),
        "base_url": runtime.get("base_url"),
        "api_key": api_key,
        "api_mode": runtime.get("api_mode"),
        "command": runtime.get("command"),
        "args": list(runtime.get("args") or []),
    }


def _load_config() -> dict:
    """从 CLI_CONFIG 或持久化配置中加载委派配置。

    先检查运行时配置（cli.py 的 CLI_CONFIG），然后回退到持久化配置
    （hermes_cli/config.py 的 load_config()），这样无论从哪个入口点
    （CLI、网关、cron）进入，都能读取到 ``delegation.model`` /
    ``delegation.provider``。
    """
    try:
        from cli import CLI_CONFIG

        cfg = CLI_CONFIG.get("delegation") or {}
        if cfg:
            return cfg
    except Exception:
        pass
    try:
        from hermes_cli.config import load_config

        full = load_config()
        return full.get("delegation") or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# OpenAI 函数调用 Schema
# ---------------------------------------------------------------------------


def _build_top_level_description() -> str:
    """用当前的运行时上限组装 delegate_task 工具描述。

    模型需要知道它实际的上限（而不是框架默认值），否则即使用户已经调高了
    delegation.max_concurrent_children / max_spawn_depth，它也会自我设限在
    「默认 3」/「默认 2」。在模块导入时（用于初始化 DELEGATE_TASK_SCHEMA）
    以及每次 get_definitions() 调用时（通过 dynamic_schema_overrides）都会调用。
    """
    try:
        max_children = _get_max_concurrent_children()
    except Exception:
        max_children = _DEFAULT_MAX_CONCURRENT_CHILDREN
    try:
        max_depth = _get_max_spawn_depth()
    except Exception:
        max_depth = MAX_DEPTH
    try:
        orchestrator_on = _get_orchestrator_enabled()
    except Exception:
        orchestrator_on = True

    if max_depth >= 2 and orchestrator_on:
        nesting_clause = (
            f"Nested delegation IS enabled for this user "
            f"(max_spawn_depth={max_depth}): pass role='orchestrator' on a "
            f"child to let it spawn its own workers, up to {max_depth - 1} "
            f"additional level(s) deep."
        )
    elif max_depth >= 2 and not orchestrator_on:
        nesting_clause = (
            f"Nested delegation is DISABLED on this install "
            f"(delegation.orchestrator_enabled=false), even though "
            f"max_spawn_depth={max_depth}. role='orchestrator' is silently "
            f"forced to 'leaf'."
        )
    else:
        nesting_clause = (
            f"Nested delegation is OFF for this user "
            f"(max_spawn_depth={max_depth}): every child is a leaf and "
            f"cannot delegate further. Raise delegation.max_spawn_depth in "
            f"config.yaml to enable nesting."
        )

    return (
        "Spawn one or more subagents to work on tasks in isolated contexts. "
        "Each subagent gets its own conversation, terminal session, and toolset. "
        "Only the final summary is returned -- intermediate tool results "
        "never enter your context window.\n\n"
        "TWO MODES (one of 'goal' or 'tasks' is required):\n"
        "1. Single task: provide 'goal' (+ optional context, toolsets).\n"
        f"2. Batch (parallel): provide 'tasks' array with up to {max_children} "
        f"items concurrently for this user (configured via "
        f"delegation.max_concurrent_children in config.yaml). {nesting_clause}\n\n"
        "BOTH MODES RUN IN THE BACKGROUND. delegate_task returns immediately — "
        "you and the user keep working, and each subagent's full result "
        "re-enters the conversation as its own new message when it finishes. A "
        "batch is just N independent background subagents (N handles, each "
        "completes on its own). Do NOT wait or poll; just continue with other "
        "work after dispatching.\n\n"
        "WHEN TO USE delegate_task:\n"
        "- Reasoning-heavy subtasks (debugging, code review, research synthesis)\n"
        "- Tasks that would flood your context with intermediate data\n"
        "- Parallel independent workstreams (research A and B simultaneously)\n\n"
        "WHEN NOT TO USE (use these instead):\n"
        "- Mechanical multi-step work with no reasoning needed -> use execute_code\n"
        "- Single tool call -> just call the tool directly\n"
        "- Tasks needing user interaction -> subagents cannot use clarify\n"
        "- Durable long-running work that must outlive the current turn -> "
        "use cronjob (action='create') or terminal(background=True, "
        "notify_on_complete=True) instead. Background delegations are NOT "
        "durable: if the parent session is closed (/new) or the process exits "
        "before a subagent finishes, that subagent's work is discarded, and "
        "/stop cancels every running background subagent.\n\n"
        "IMPORTANT:\n"
        "- Subagents have NO memory of your conversation. Pass all relevant "
        "info (file paths, error messages, constraints) via the 'context' field.\n"
        "- If the user is writing in a non-English language, or asked for "
        "output in a specific language / tone / style, say so in 'context' "
        "(e.g. \"respond in Chinese\", \"return output in Japanese\"). "
        "Otherwise subagents default to English and their summaries will "
        "contaminate your final reply with the wrong language.\n"
        "- Subagent summaries are SELF-REPORTS, not verified facts. A subagent "
        "that claims \"uploaded successfully\" or \"file written\" may be wrong. "
        "For operations with external side-effects (HTTP POST/PUT, remote "
        "writes, file creation at shared paths, publishing), require the "
        "subagent to return a verifiable handle (URL, ID, absolute path, HTTP "
        "status) and verify it yourself — fetch the URL, stat the file, read "
        "back the content — before telling the user the operation succeeded.\n"
        "- Leaf subagents (role='leaf', the default) CANNOT call: "
        "delegate_task, clarify, memory, send_message, execute_code.\n"
        "- Orchestrator subagents (role='orchestrator') retain "
        "delegate_task so they can spawn their own workers, but still "
        "cannot use clarify, memory, send_message, or execute_code. "
        f"Orchestrators are bounded by max_spawn_depth={max_depth} for this "
        f"user and can be disabled globally via "
        "delegation.orchestrator_enabled=false.\n"
        "- Subagent model is NOT selectable per call: children inherit the parent model (plus its fallback chain) unless you pin all subagents to a model via delegation.provider / delegation.model in config.yaml.\n"
        "- Each subagent gets its own terminal session (separate working directory and state).\n"
        "- Results are always returned as an array, one entry per task."
    )


def _build_tasks_param_description() -> str:
    """用当前的并发上限组装 'tasks' 参数描述。"""
    try:
        max_children = _get_max_concurrent_children()
    except Exception:
        max_children = _DEFAULT_MAX_CONCURRENT_CHILDREN
    return (
        f"Batch mode: tasks to run in parallel (up to {max_children} for this "
        f"user, set via delegation.max_concurrent_children). Each gets "
        "its own subagent with isolated context and terminal session. "
        "When provided, top-level goal/context/toolsets are ignored."
    )


def _build_role_param_description() -> str:
    """用当前的派生深度上限组装 'role' 参数描述。"""
    try:
        max_depth = _get_max_spawn_depth()
    except Exception:
        max_depth = MAX_DEPTH
    try:
        orchestrator_on = _get_orchestrator_enabled()
    except Exception:
        orchestrator_on = True

    if max_depth >= 2 and orchestrator_on:
        nesting_note = (
            f"Nesting IS enabled for this user (max_spawn_depth={max_depth}): "
            f"orchestrator children can themselves delegate up to {max_depth - 1} "
            "more level(s) deep."
        )
    elif max_depth >= 2 and not orchestrator_on:
        nesting_note = (
            "Nesting is currently disabled "
            "(delegation.orchestrator_enabled=false); 'orchestrator' is "
            "silently forced to 'leaf'."
        )
    else:
        nesting_note = (
            f"Nesting is OFF for this user (max_spawn_depth={max_depth}); "
            "'orchestrator' is silently forced to 'leaf'. Raise "
            "delegation.max_spawn_depth in config.yaml to enable."
        )

    return (
        "Role of the child agent. 'leaf' (default) = focused "
        "worker, cannot delegate further. 'orchestrator' = can "
        f"use delegate_task to spawn its own workers. {nesting_note}"
    )


def _build_dynamic_schema_overrides() -> dict:
    """返回反映当前配置的按调用 schema 覆盖项。

    插入到 ToolEntry.dynamic_schema_overrides 中，这样每次 get_definitions()
    都会把描述字段改写为用户的实际上限。
    """
    overrides_params = {
        **DELEGATE_TASK_SCHEMA["parameters"],
    }
    # 深拷贝 properties，避免改动静态 schema 字典。
    overrides_params["properties"] = {
        k: dict(v) for k, v in DELEGATE_TASK_SCHEMA["parameters"]["properties"].items()
    }
    overrides_params["properties"]["tasks"]["description"] = _build_tasks_param_description()
    overrides_params["properties"]["role"]["description"] = _build_role_param_description()
    return {
        "description": _build_top_level_description(),
        "parameters": overrides_params,
    }


DELEGATE_TASK_SCHEMA = {
    "name": "delegate_task",
    # 注意：description / tasks.description / role.description 都是占位值。
    # 真正的文本由 _build_dynamic_schema_overrides() 在每次 get_definitions()
    # 调用时生成（通过下方的 dynamic_schema_overrides 注册），这样模型看到的
    # 就是用户实际的 delegation.max_concurrent_children / max_spawn_depth，
    # 而不是框架默认值。惰性构建（而不是在模块导入时构建）也避免了强制
    # cli.CLI_CONFIG 在测试 conftest 重定向 HERMES_HOME 之前就加载。
    "description": (
        "Spawn one or more subagents in isolated contexts. "
        "Description is rebuilt at every get_definitions() call to reflect "
        "the user's current delegation limits."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "What the subagent should accomplish. Be specific and "
                    "self-contained -- the subagent knows nothing about your "
                    "conversation history."
                ),
            },
            "context": {
                "type": "string",
                "description": (
                    "Background information the subagent needs: file paths, "
                    "error messages, project structure, constraints. The more "
                    "specific you are, the better the subagent performs."
                ),
            },
            "toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Toolsets to enable for this subagent. "
                    "Default: inherits your enabled toolsets. "
                    f"Available toolsets: {_TOOLSET_LIST_STR}. "
                    "Common patterns: ['terminal', 'file'] for code work, "
                    "['web'] for research, ['browser'] for web interaction, "
                    "['terminal', 'file', 'web'] for full-stack tasks."
                ),
            },
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string", "description": "Task goal"},
                        "context": {
                            "type": "string",
                            "description": "Task-specific context",
                        },
                        "toolsets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": f"Toolsets for this specific task. Available: {_TOOLSET_LIST_STR}. Use 'web' for network access, 'terminal' for shell, 'browser' for web interaction.",
                        },
                        "acp_command": {
                            "type": "string",
                            "description": (
                                "Per-task ACP command override (e.g. 'copilot'). "
                                "Overrides the top-level acp_command for this task only. "
                                "Do NOT set unless the user explicitly told you an ACP CLI is installed."
                            ),
                        },
                        "acp_args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Per-task ACP args override. Leave empty unless acp_command is set.",
                        },
                        "role": {
                            "type": "string",
                            "enum": ["leaf", "orchestrator"],
                            "description": "Per-task role override. See top-level 'role' for semantics.",
                        },
                    },
                    "required": ["goal"],
                },
                # 没有 maxItems——运行时上限可通过 delegation.max_concurrent_children
                #（默认 3）配置，并在 delegate_task() 中以明确的错误强制执行。
                "description": "(rebuilt at get_definitions() time)",
            },
            "role": {
                "type": "string",
                "enum": ["leaf", "orchestrator"],
                "description": "(rebuilt at get_definitions() time)",
            },
            "background": {
                "type": "boolean",
                "description": (
                    "DEPRECATED / IGNORED. Single-task delegations always run "
                    "in the background automatically — you do not need to (and "
                    "cannot) opt in or out. The result re-enters the "
                    "conversation as a new message when the subagent finishes; "
                    "just continue working in the meantime. Setting this has no "
                    "effect; the parameter remains only for backward "
                    "compatibility."
                ),
            },
            "acp_command": {
                "type": "string",
                "description": (
                    "Override ACP command for child agents (e.g. 'copilot'). "
                    "When set, children use ACP subprocess transport instead of inheriting "
                    "the parent's transport. Requires an ACP-compatible CLI "
                    "(currently GitHub Copilot CLI via 'copilot --acp --stdio'). "
                    "See agent/copilot_acp_client.py for the implementation. "
                    "IMPORTANT: Do NOT set this unless the user has explicitly told you "
                    "a specific ACP-compatible CLI is installed and configured. "
                    "Leave empty to use the parent's default transport (Hermes subagents)."
                ),
            },
            "acp_args": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Arguments for the ACP command (default: ['--acp', '--stdio']). "
                    "Only used when acp_command is set. "
                    "Leave empty unless acp_command is explicitly provided."
                ),
            },
        },
        "required": [],
    },
}


# --- 注册表 ---
from tools.registry import registry, tool_error


def _model_background_value(args: dict, parent_agent=None) -> bool:
    """面向模型的派发路径（注册表回退）所用的后台标志。

    来自顶层代理的委派总是在后台运行——模型不做选择。这适用于单任务和
    扇出批量（每个任务成为各自独立的后台子代理）。唯一的例外是来自
    编排者子代理（depth > 0）的委派，它需要在其自身回合内拿到工作者的结果。
    实际路径是 ``run_agent._dispatch_delegate_task``；此 lambda 在拦截被
    绕过的罕见情况下与之镜像。直接用 Python 调用 ``delegate_task`` 的调用方
    保留历史上的同步默认行为。
    """
    is_subagent = getattr(parent_agent, "_delegate_depth", 0) > 0
    return not is_subagent


registry.register(
    name="delegate_task",
    toolset="delegation",
    schema=DELEGATE_TASK_SCHEMA,
    handler=lambda args, **kw: delegate_task(
        goal=args.get("goal"),
        context=args.get("context"),
        toolsets=args.get("toolsets"),
        tasks=args.get("tasks"),
        max_iterations=args.get("max_iterations"),
        acp_command=args.get("acp_command"),
        acp_args=args.get("acp_args"),
        role=args.get("role"),
        background=_model_background_value(args, kw.get("parent_agent")),
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=check_delegate_requirements,
    emoji="🔀",
    dynamic_schema_overrides=_build_dynamic_schema_overrides,
)
