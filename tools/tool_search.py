"""Hermes Agent 的渐进式工具披露（「tool search」）。

启用后，MCP 和非核心插件工具会在模型可见的 tools 数组中被替换为三个
桥接工具 —— ``tool_search``、``tool_describe``、``tool_call`` —— 并按需
呈现。核心 Hermes 工具永远不会被延迟。

本模块围绕以下设计约束构建（完整理由见 ``openclaw-tool-search-report``）：

* ``toolsets._HERMES_CORE_TOOLS`` 中定义的核心工具*绝不*被延迟。
  always-load 就是 always-load，没有例外。
* 阈值门控在每次装配时都运行：当可延迟工具占用的 token 不足模型上下文
  窗口的 ``threshold_pct``（默认 10%）时，工具搜索为空操作（no-op），
  tools 数组原样透传。
* 目录（catalog）在多轮对话和多次 tools 数组装配之间是无状态的。
  每次都从当前的 tool-defs 列表重建。这是 OpenClaw 的 cron 回归
  （openclaw/openclaw#84141）带来的教训：一个以会话为键、且与实时
  工具注册表不同步的目录会导致静默的工具丢失。
* 桥接工具完全像直接调用一样经由 ``model_tools.handle_function_call``
  路由，因此护栏、插件 pre/post 钩子、审批流程以及工具结果截断
  全部以相同方式触发。
* 展示与轨迹解包（unwrap）在本模块内实现，使用户（CLI 活动流、
  gateway、保存的轨迹）始终看到的是底层工具，而非桥接工具。
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("tools.tool_search")


# 桥接工具名。这些名称是保留的，不得与用户/插件/MCP 工具冲突 ——
# 注册任何使用这些名称的工具都会被注册表已有的 override 保护逻辑拒绝。
TOOL_SEARCH_NAME = "tool_search"
TOOL_DESCRIBE_NAME = "tool_describe"
TOOL_CALL_NAME = "tool_call"

BRIDGE_TOOL_NAMES = frozenset({TOOL_SEARCH_NAME, TOOL_DESCRIBE_NAME, TOOL_CALL_NAME})

# 在没有真实分词器的情况下用字符数估算 token 时，这是一条在各 provider
# 之间稳定的经验法则。大约每 4 个字符对应 1 个 token（针对英文 + JSON）。
# 低估会导致漏报（本该激活工具搜索却没激活）；高估会导致误报（不该激活
# 时却激活了）。4.0 略微偏向低估，这是更安全的默认值。
CHARS_PER_TOKEN = 4.0


# ---------------------------------------------------------------------------
# 配置管道
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSearchConfig:
    """针对单次装配解析并校验后的工具搜索配置。"""

    enabled: str  # "auto" | "on" | "off"
    threshold_pct: float  # 0..100 —— 仅在 enabled == "auto" 时使用
    search_default_limit: int
    max_search_limit: int

    @classmethod
    def from_raw(cls, raw: Any) -> "ToolSearchConfig":
        """从原始 dict / bool / None 构建一份配置。

        接受遗留的 bool 形式（``tools.tool_search: true``）和 dict 形式
        （``tools.tool_search: {enabled: auto, ...}``）。校验并钳制每个
        数值字段；未知值会退回到安全默认值而非抛异常，因此用户配置里的
        一个拼写错误不会弄崩 agent。
        """
        if raw is True:
            return cls(enabled="auto", threshold_pct=10.0,
                       search_default_limit=5, max_search_limit=20)
        if raw is False:
            return cls(enabled="off", threshold_pct=10.0,
                       search_default_limit=5, max_search_limit=20)
        if not isinstance(raw, dict):
            return cls(enabled="auto", threshold_pct=10.0,
                       search_default_limit=5, max_search_limit=20)

        enabled_raw = str(raw.get("enabled", "auto")).strip().lower()
        if enabled_raw in ("true", "1", "yes"):
            enabled = "on"
        elif enabled_raw in ("false", "0", "no"):
            enabled = "off"
        elif enabled_raw in ("auto", "on", "off"):
            enabled = enabled_raw
        else:
            enabled = "auto"

        threshold_pct = _safe_float(raw.get("threshold_pct"), 10.0)
        threshold_pct = max(0.0, min(100.0, threshold_pct))

        max_search_limit = max(1, min(50, _safe_int(raw.get("max_search_limit"), 20)))
        search_default_limit = max(1, min(max_search_limit,
                                          _safe_int(raw.get("search_default_limit"), 5)))

        return cls(
            enabled=enabled,
            threshold_pct=threshold_pct,
            search_default_limit=search_default_limit,
            max_search_limit=max_search_limit,
        )


def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _safe_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def load_config() -> ToolSearchConfig:
    """从用户配置文件加载工具搜索配置。"""
    try:
        from hermes_cli.config import load_config as _load
        cfg = _load() or {}
        tools_cfg = cfg.get("tools") if isinstance(cfg.get("tools"), dict) else {}
        if not isinstance(tools_cfg, dict):
            tools_cfg = {}
        return ToolSearchConfig.from_raw(tools_cfg.get("tool_search"))
    except Exception as e:
        logger.debug("Failed to load tool-search config: %s", e)
        return ToolSearchConfig.from_raw(None)


# ---------------------------------------------------------------------------
# 工具分类
# ---------------------------------------------------------------------------


def _core_tool_names() -> frozenset[str]:
    """返回绝不延迟的工具名称集合。

    懒加载导入，因为 ``toolsets`` 会从 ``tools.registry`` 导入，
    我们不希望形成硬性的循环依赖。
    """
    try:
        from toolsets import _HERMES_CORE_TOOLS
        return frozenset(_HERMES_CORE_TOOLS)
    except Exception:
        return frozenset()


def is_deferrable_tool_name(name: str) -> bool:
    """当名为该值的工具有资格被延迟时返回 True。

    一个工具是可延迟的，当且仅当它注册于一个 MCP toolset 前缀下，
    或者它不在 ``_HERMES_CORE_TOOLS`` 中。核心工具即使其 toolset
    在技术上是由插件提供的也绝不延迟（这能防止意外的覆盖）。
    """
    if name in BRIDGE_TOOL_NAMES:
        return False
    if name in _core_tool_names():
        return False
    # 检查注册表中的 toolset 是否带有 MCP 前缀。
    try:
        from tools.registry import registry
        entry = registry.get_entry(name)
        if entry is None:
            return False
        if entry.toolset.startswith("mcp-"):
            return True
        # 非 MCP、非核心 → 插件工具，有资格。
        return True
    except Exception:
        return False


def classify_tools(tool_defs: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """将一个 tool-defs 列表拆分为 (visible, deferrable)。

    ``visible`` 保留所有必须留在面向模型的数组中的工具：每个核心工具，
    外加任何我们无法分类的工具。``deferrable`` 是目录条目的候选集合。
    """
    visible: List[Dict[str, Any]] = []
    deferrable: List[Dict[str, Any]] = []
    for td in tool_defs:
        fn = td.get("function") or {}
        name = fn.get("name", "")
        if name in BRIDGE_TOOL_NAMES:
            # 不应发生 —— 桥接工具是在分类之后才加入的 ——
            # 但仍做防御性处理。
            continue
        if is_deferrable_tool_name(name):
            deferrable.append(td)
        else:
            visible.append(td)
    return visible, deferrable


# ---------------------------------------------------------------------------
# Token 估算与阈值门控
# ---------------------------------------------------------------------------


def estimate_tokens_from_schemas(tool_defs: Iterable[Dict[str, Any]]) -> int:
    """用「字符数 / 4」规则估算一个 tool-defs 列表的 token 开销。

    廉价且在各 provider 间稳定。这个数值不需要精确 —— 它只决定
    激活/跳过的判定；典型 200K 上下文加 10% 阈值意味着判定在大约
    20K token 的 schema 处翻转。数量级级别的精度就够了。
    """
    total_chars = 0
    for td in tool_defs:
        try:
            total_chars += len(json.dumps(td, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError):
            total_chars += len(str(td))
    return int(math.ceil(total_chars / CHARS_PER_TOKEN))


def should_activate(
    config: ToolSearchConfig,
    deferrable_tokens: int,
    context_length: Optional[int],
) -> bool:
    """决定当前装配是否应激活工具搜索。

    ``"off"`` 无条件跳过。``"on"`` 无条件激活（前提是至少有一个可延迟
    工具 —— 替换一个空操作没有意义）。``"auto"`` 在可延迟 schema 会
    占用 ``threshold_pct`` 比例的上下文或更多时激活。
    """
    if config.enabled == "off":
        return False
    if deferrable_tokens <= 0:
        return False
    if config.enabled == "on":
        return True
    # auto
    if not context_length or context_length <= 0:
        # 在不知道上下文大小的情况下，退回到固定的 20K token 截断值
        # —— 这是 Anthropic 和 OpenAI 都观察到质量下降的悬崖。
        return deferrable_tokens >= 20_000
    threshold_tokens = int(context_length * (config.threshold_pct / 100.0))
    return deferrable_tokens >= threshold_tokens


# ---------------------------------------------------------------------------
# 目录 + BM25 检索
# ---------------------------------------------------------------------------


@dataclass
class CatalogEntry:
    """一个可延迟的工具，以桥接工具可搜索、可服务的形式呈现。"""

    name: str
    description: str
    schema: Dict[str, Any]  # 完整的 {"type":"function", "function": {...}} 条目。
    source: str  # "mcp" | "plugin" | "other"
    source_name: str  # toolset 名称，例如 "mcp-github" 或 "kanban"

    # 供 BM25 使用的预分词字段。
    _tokens: List[str] = field(default_factory=list)


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _tokenize(text: str) -> List[str]:
    if not text:
        return []
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _entry_search_text(td: Dict[str, Any]) -> str:
    """为一个可延迟工具构建搜索文本块。

    包含工具名（将下划线拆分成单词，以便 BM25 能匹配查询词）、描述，
    以及顶层参数的名称。刻意排除了 schema 主体 —— 对其建索引只会增加
    噪声而不会改善我们测量到的召回率。
    """
    fn = td.get("function") or {}
    name = fn.get("name", "")
    desc = fn.get("description", "") or ""
    params = ((fn.get("parameters") or {}).get("properties") or {})
    param_names = " ".join(params.keys())
    # 将 snake_case 和带点号的名字拆分成单词，供 BM25 使用。
    name_words = name.replace("_", " ").replace(".", " ").replace("-", " ").replace(":", " ")
    return f"{name_words} {desc} {param_names}"


def _classify_source(name: str) -> Tuple[str, str]:
    """返回某个已注册工具名的 (source_kind, source_name)。"""
    try:
        from tools.registry import registry
        entry = registry.get_entry(name)
        if entry is None:
            return ("other", "")
        if entry.toolset.startswith("mcp-"):
            return ("mcp", entry.toolset)
        return ("plugin", entry.toolset)
    except Exception:
        return ("other", "")


def build_catalog(tool_defs: List[Dict[str, Any]]) -> List[CatalogEntry]:
    """从一个 tool-defs 列表构建可延迟工具的目录。

    调用方应只传入可延迟的子集（``classify_tools`` 将其作为第二个
    元素返回）。
    """
    catalog: List[CatalogEntry] = []
    for td in tool_defs:
        fn = td.get("function") or {}
        name = fn.get("name", "")
        if not name:
            continue
        desc = fn.get("description", "") or ""
        source, source_name = _classify_source(name)
        entry = CatalogEntry(
            name=name,
            description=desc,
            schema=td,
            source=source,
            source_name=source_name,
            _tokens=_tokenize(_entry_search_text(td)),
        )
        catalog.append(entry)
    return catalog


def _bm25_score(query_tokens: List[str], doc_tokens: List[str],
                doc_lengths: List[int], avg_dl: float,
                doc_freq: Dict[str, int], n_docs: int,
                k1: float = 1.5, b: float = 0.75) -> float:
    """针对一个查询对一个文档的标准 BM25 得分。

    内联了一个小型实现，而不是引入新依赖。性能没问题 —— 目录规模以
    N（工具数）为上界，通常 < 500，而且我们对内存中的 token 列表打分。
    """
    if not doc_tokens:
        return 0.0
    score = 0.0
    dl = len(doc_tokens)
    # 预统计文档中的 token。
    doc_tf: Dict[str, int] = {}
    for t in doc_tokens:
        doc_tf[t] = doc_tf.get(t, 0) + 1
    for q in query_tokens:
        df = doc_freq.get(q, 0)
        if df == 0:
            continue
        idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
        tf = doc_tf.get(q, 0)
        if tf == 0:
            continue
        norm = tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / max(avg_dl, 1.0)))
        score += idf * norm
    return score


def search_catalog(catalog: List[CatalogEntry], query: str, limit: int = 5) -> List[CatalogEntry]:
    """按 BM25 返回 ``query`` 的前 ``limit`` 个目录条目。

    当 BM25 没有任何高于零的结果时，退回到稳定的名称子串匹配。这确保了
    像对 ``"github"`` 的查询（目录中每个工具都叫 ``github_*``）仍能返回
    结果 —— 当查询和文档只共享一个出现在每个文档中的 token（IDF 为零）
    时，BM25 的表现会变差。
    """
    if not catalog or limit <= 0:
        return []
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    # 预计算文档统计量。
    doc_lengths = [len(e._tokens) for e in catalog]
    avg_dl = sum(doc_lengths) / max(len(doc_lengths), 1)
    doc_freq: Dict[str, int] = {}
    for e in catalog:
        seen = set(e._tokens)
        for t in seen:
            doc_freq[t] = doc_freq.get(t, 0) + 1
    n_docs = len(catalog)

    scored: List[Tuple[float, CatalogEntry]] = []
    for entry in catalog:
        s = _bm25_score(query_tokens, entry._tokens, doc_lengths, avg_dl,
                        doc_freq, n_docs)
        if s > 0:
            scored.append((s, entry))

    if not scored:
        # 针对原始工具名的子串兜底。
        ql = query.lower()
        for entry in catalog:
            if ql in entry.name.lower():
                scored.append((0.1, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:limit]]


# ---------------------------------------------------------------------------
# 桥接工具 schema
# ---------------------------------------------------------------------------


def bridge_tool_schemas(deferred_count: int) -> List[Dict[str, Any]]:
    """构建用于替换被延迟工具的桥接工具 schema。

    schema 刻意保持简短 —— 这里每多一个字节，都是用户在每一轮都要付出的
    代价。描述经过调校，确保对模型应遵循的调用顺序表述明确无歧义。
    """
    desc_search = (
        f"Search {deferred_count} additional tools that are loaded on demand. "
        "Returns up to ``limit`` matches with name and description. Follow "
        f"with `{TOOL_DESCRIBE_NAME}` to load a tool's full parameter schema, "
        f"then `{TOOL_CALL_NAME}` to invoke it. Tools listed at the top of this "
        "system prompt are already available and do not need to be searched."
    )
    desc_describe = (
        f"Load the full JSON schema for one tool returned by `{TOOL_SEARCH_NAME}`. "
        f"Required before `{TOOL_CALL_NAME}` if the tool's parameters are unknown."
    )
    desc_call = (
        "Invoke a deferred tool by name with the given arguments. Argument shape "
        f"matches the tool's schema (see `{TOOL_DESCRIBE_NAME}`). Policy, hooks, "
        "and approvals run exactly as for any directly-listed tool."
    )

    return [
        {
            "type": "function",
            "function": {
                "name": TOOL_SEARCH_NAME,
                "description": desc_search,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Keywords describing the capability you need (e.g. 'create github issue').",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of results to return. Default 5.",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": TOOL_DESCRIBE_NAME,
                "description": desc_describe,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Exact tool name (as returned by tool_search).",
                        },
                    },
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": TOOL_CALL_NAME,
                "description": desc_call,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Exact tool name to invoke.",
                        },
                        "arguments": {
                            "type": "object",
                            "description": "Arguments for the tool, matching its schema.",
                        },
                    },
                    "required": ["name", "arguments"],
                },
            },
        },
    ]


# ---------------------------------------------------------------------------
# 公共入口：装配 tool-defs（可选地带工具搜索）
# ---------------------------------------------------------------------------


@dataclass
class AssemblyResult:
    """一次装配的结果。对测试和可观测性有用。"""

    tool_defs: List[Dict[str, Any]]
    activated: bool
    deferred_count: int = 0
    deferred_tokens: int = 0
    threshold_tokens: int = 0


def assemble_tool_defs(
    tool_defs: List[Dict[str, Any]],
    *,
    context_length: Optional[int] = None,
    config: Optional[ToolSearchConfig] = None,
) -> AssemblyResult:
    """返回模型实际应看到的 tool-defs 列表。

    当工具搜索未激活时（关闭、没有可延迟工具、或低于阈值），本函数是
    直通。激活时，MCP 和插件工具会从可见列表中剥离，并被替换为三个
    桥接工具。无论配置如何，核心工具*绝不*延迟。

    幂等：当输入中已包含桥接工具时调用本函数是空操作（它们会被分类为
    非核心/非可延迟，但其名称是保留的，因此会从可延迟集合中被过滤掉）。
    """
    if config is None:
        config = load_config()

    # 防御性处理：剥离可能已在列表中的桥接工具
    # （例如有人调用了两次 assemble）。
    incoming = [td for td in tool_defs
                if (td.get("function") or {}).get("name") not in BRIDGE_TOOL_NAMES]

    visible, deferrable = classify_tools(incoming)
    if not deferrable:
        return AssemblyResult(tool_defs=incoming, activated=False)

    deferrable_tokens = estimate_tokens_from_schemas(deferrable)
    if not should_activate(config, deferrable_tokens, context_length):
        return AssemblyResult(
            tool_defs=incoming,
            activated=False,
            deferred_count=len(deferrable),
            deferred_tokens=deferrable_tokens,
            threshold_tokens=int((context_length or 0) * (config.threshold_pct / 100.0)),
        )

    bridge = bridge_tool_schemas(len(deferrable))
    result = visible + bridge
    threshold_tokens = int((context_length or 0) * (config.threshold_pct / 100.0))

    logger.info(
        "tool_search activated: %d core/visible tools kept, %d deferred (~%d tokens, threshold ~%d)",
        len(visible), len(deferrable), deferrable_tokens, threshold_tokens,
    )

    return AssemblyResult(
        tool_defs=result,
        activated=True,
        deferred_count=len(deferrable),
        deferred_tokens=deferrable_tokens,
        threshold_tokens=threshold_tokens,
    )


# ---------------------------------------------------------------------------
# 桥接工具分发
# ---------------------------------------------------------------------------


def is_bridge_tool(name: str) -> bool:
    return name in BRIDGE_TOOL_NAMES


def _format_search_hit(entry: CatalogEntry) -> Dict[str, Any]:
    return {
        "name": entry.name,
        "source": entry.source,
        "source_name": entry.source_name,
        # 限制描述长度，避免一个啰嗦的 MCP 服务器把结果撑爆。
        "description": (entry.description or "")[:400],
    }


def dispatch_tool_search(args: Dict[str, Any],
                         *,
                         current_tool_defs: List[Dict[str, Any]],
                         config: Optional[ToolSearchConfig] = None) -> str:
    """执行 ``tool_search`` 桥接工具。返回一个 JSON 字符串。"""
    if config is None:
        config = load_config()
    query = str(args.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "query is required"}, ensure_ascii=False)

    raw_limit = args.get("limit")
    if raw_limit is None:
        limit = config.search_default_limit
    else:
        limit = max(1, min(config.max_search_limit, _safe_int(raw_limit, config.search_default_limit)))

    _, deferrable = classify_tools(current_tool_defs)
    catalog = build_catalog(deferrable)
    hits = search_catalog(catalog, query, limit=limit)
    return json.dumps({
        "query": query,
        "total_available": len(catalog),
        "matches": [_format_search_hit(h) for h in hits],
    }, ensure_ascii=False)


def dispatch_tool_describe(args: Dict[str, Any],
                           *,
                           current_tool_defs: List[Dict[str, Any]]) -> str:
    """执行 ``tool_describe`` 桥接工具。返回一个 JSON 字符串。"""
    name = str(args.get("name") or "").strip()
    if not name:
        return json.dumps({"error": "name is required"}, ensure_ascii=False)
    if not is_deferrable_tool_name(name):
        return json.dumps({
            "error": (
                f"'{name}' is not a deferrable tool. If you see it in the tools list "
                "already, call it directly; otherwise check the spelling against tool_search."
            ),
        }, ensure_ascii=False)
    _, deferrable = classify_tools(current_tool_defs)
    for td in deferrable:
        fn = td.get("function") or {}
        if fn.get("name") == name:
            return json.dumps({
                "name": name,
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            }, ensure_ascii=False)
    return json.dumps({
        "error": f"'{name}' is not currently available. Re-run tool_search to refresh.",
    }, ensure_ascii=False)


def scoped_deferrable_names(tool_defs: List[Dict[str, Any]]) -> frozenset[str]:
    """返回 ``tool_defs`` 中存在的可延迟工具名集合。

    ``tool_defs`` 应为当前会话 toolset 范围下的*装配前*工具列表（即
    ``get_tool_definitions(skip_tool_search_assembly=True)`` 针对该会话
    已启用/禁用的 toolset 所返回的内容）。得到的集合就是该会话可通过
    ``tool_call`` 合法触达的工具全集。被 ``model_tools`` 桥接分发和
    ``tool_executor`` 解包同时用作范围门控，使一个受限 toolset 的会话
    永远无法通过桥接触达范围外的工具。
    """
    names: set[str] = set()
    for td in tool_defs:
        name = (td.get("function") or {}).get("name", "")
        if name and is_deferrable_tool_name(name):
            names.add(name)
    return frozenset(names)


def resolve_underlying_call(args: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    """将一次 ``tool_call`` 调用解析为 (underlying_name, args, error_msg)。

    被以下使用：
    * ``model_tools.handle_function_call`` 中的分发器、
    * 展示层（使活动流显示底层工具）、
    * 轨迹记录器。

    解析出错时返回 ``(None, {}, error_message)``。
    """
    name = str(args.get("name") or "").strip()
    if not name:
        return None, {}, "tool_call requires a 'name' argument"
    if name in BRIDGE_TOOL_NAMES:
        return None, {}, f"tool_call cannot invoke '{name}' (it is itself a bridge tool)"
    raw_args = args.get("arguments")
    if raw_args is None:
        raw_args = {}
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except json.JSONDecodeError as e:
            return None, {}, f"tool_call 'arguments' is not valid JSON: {e}"
    if not isinstance(raw_args, dict):
        return None, {}, "tool_call 'arguments' must be an object"
    if not is_deferrable_tool_name(name):
        return None, {}, (
            f"'{name}' is not a deferrable tool. If it appears in the model-facing tools "
            "list already, call it directly instead of via tool_call."
        )
    return name, raw_args, None


__all__ = [
    "TOOL_SEARCH_NAME",
    "TOOL_DESCRIBE_NAME",
    "TOOL_CALL_NAME",
    "BRIDGE_TOOL_NAMES",
    "ToolSearchConfig",
    "CatalogEntry",
    "AssemblyResult",
    "load_config",
    "is_deferrable_tool_name",
    "classify_tools",
    "estimate_tokens_from_schemas",
    "should_activate",
    "build_catalog",
    "search_catalog",
    "bridge_tool_schemas",
    "assemble_tool_defs",
    "is_bridge_tool",
    "dispatch_tool_search",
    "dispatch_tool_describe",
    "resolve_underlying_call",
    "scoped_deferrable_names",
]
