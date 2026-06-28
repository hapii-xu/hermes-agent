#!/usr/bin/env python3
"""
Todo 工具模块 - 规划与任务管理

提供一个内存中的任务列表，供 agent 用来分解复杂任务、跟踪进度，
并在长对话中保持专注。该状态保存在 AIAgent 实例上（每个会话一个），
并在上下文压缩事件后重新注入对话。

设计：
- 单一 `todo` 工具：传入 `todos` 参数执行写入，省略则执行读取
- 每次调用都返回完整的当前列表
- 不修改系统提示词，不修改工具响应
- 行为引导完全放在工具 schema 描述中
"""

import json
from typing import Dict, Any, List, Optional


# todo 条目的合法状态值
VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}

# 持久化 todo 状态的边界。todo 列表是一种规划辅助工具，模型会在每次
# 上下文压缩事件后重新读取（见 format_for_injection），因此条目内容
# 或数量若不加限制，就会抵消它所依附的压缩效果。这些上限用于防止
# 单个过大的条目（无论是由模型编写，还是从 API 服务器上调用方提供的
# 历史中回放而来）撑大重新注入块。这些上限相对真实计划是很宽裕的——
# 一个 todo 条目只是一段简短的任务描述，活跃列表通常只有寥寥几项，
# 而不是上百项。
MAX_TODO_CONTENT_CHARS = 4000
MAX_TODO_ITEMS = 256
_TRUNCATION_MARKER = "… [truncated]"


class TodoStore:
    """
    内存中的 todo 列表。每个 AIAgent 一个实例（每个会话一个）。

    条目有序——列表位置即优先级。每个条目包含：
      - id：唯一的字符串标识符（由 agent 选定）
      - content：任务描述
      - status：pending | in_progress | completed | cancelled
    """

    def __init__(self):
        self._items: List[Dict[str, str]] = []

    def write(self, todos: List[Dict[str, Any]], merge: bool = False) -> List[Dict[str, str]]:
        """
        写入 todos。返回写入后的完整当前列表。

        参数：
            todos：{id, content, status} 字典列表
            merge：若为 False，替换整个列表。若为 True，按 id
                   更新已有条目并追加新条目。
        """
        if not merge:
            # 替换模式：整体使用新列表
            self._items = [self._validate(t) for t in self._dedupe_by_id(todos)]
        else:
            # 合并模式：按 id 更新已有条目，追加新条目
            existing = {item["id"]: item for item in self._items}
            for t in self._dedupe_by_id(todos):
                item_id = str(t.get("id", "")).strip()
                if not item_id:
                    continue  # 没有 id 无法合并

                if item_id in existing:
                    # 只更新 LLM 实际提供的字段
                    if "content" in t and t["content"]:
                        existing[item_id]["content"] = self._cap_content(str(t["content"]).strip())
                    if "status" in t and t["status"]:
                        status = str(t["status"]).strip().lower()
                        if status in VALID_STATUSES:
                            existing[item_id]["status"] = status
                else:
                    # 新条目——完整校验并追加到末尾
                    validated = self._validate(t)
                    existing[validated["id"]] = validated
                    self._items.append(validated)
            # 重建 _items，保留已有条目的顺序
            seen = set()
            rebuilt = []
            for item in self._items:
                current = existing.get(item["id"], item)
                if current["id"] not in seen:
                    rebuilt.append(current)
                    seen.add(current["id"])
            self._items = rebuilt
        # 限制条目总数，防止回放/过大的列表无限撑大重新注入块。
        # 保留优先级最高的开头部分（列表顺序即优先级）。
        if len(self._items) > MAX_TODO_ITEMS:
            self._items = self._items[:MAX_TODO_ITEMS]
        return self.read()

    def read(self) -> List[Dict[str, str]]:
        """返回当前列表的副本。"""
        return [item.copy() for item in self._items]

    def has_items(self) -> bool:
        """检查列表中是否有任何条目。"""
        return bool(self._items)

    def format_for_injection(self) -> Optional[str]:
        """
        渲染 todo 列表，用于压缩后注入。

        返回一段人类可读的字符串，追加到压缩后的消息历史之后；
        若列表为空则返回 None。
        """
        if not self._items:
            return None

        # 紧凑显示用的状态标记
        markers = {
            "completed": "[x]",
            "in_progress": "[>]",
            "pending": "[ ]",
            "cancelled": "[~]",
        }

        # 只注入 pending/in_progress 条目——completed/cancelled 条目会
        # 导致模型在压缩后重做已完成的工作。
        active_items = [
            item for item in self._items
            if item["status"] in {"pending", "in_progress"}
        ]
        if not active_items:
            return None

        lines = ["[Your active task list was preserved across context compression]"]
        for item in active_items:
            marker = markers.get(item["status"], "[?]")
            lines.append(f"- {marker} {item['id']}. {item['content']} ({item['status']})")

        return "\n".join(lines)

    @staticmethod
    def _cap_content(content: str) -> str:
        """将过大的 todo 内容截断到 MAX_TODO_CONTENT_CHARS。

        否则单个巨大的条目会无限制地撑大压缩后的重新注入块
        （format_for_injection）。保留开头部分——即任务描述中可操作
        的部分——再加一个截断标记。
        """
        if len(content) > MAX_TODO_CONTENT_CHARS:
            keep = MAX_TODO_CONTENT_CHARS - len(_TRUNCATION_MARKER)
            return content[:keep] + _TRUNCATION_MARKER
        return content

    @staticmethod
    def _validate(item: Dict[str, Any]) -> Dict[str, str]:
        """
        校验并规范化一个 todo 条目。

        确保必需字段存在且状态合法。
        返回一个只包含 {id, content, status} 的干净字典。
        """
        item_id = str(item.get("id", "")).strip()
        if not item_id:
            item_id = "?"

        content = str(item.get("content", "")).strip()
        if not content:
            content = "(no description)"
        else:
            content = TodoStore._cap_content(content)

        status = str(item.get("status", "pending")).strip().lower()
        if status not in VALID_STATUSES:
            status = "pending"

        return {"id": item_id, "content": content, "status": status}

    @staticmethod
    def _dedupe_by_id(todos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """折叠重复的 id，保留最后一次出现且位置不变。"""
        last_index: Dict[str, int] = {}
        for i, item in enumerate(todos):
            item_id = str(item.get("id", "")).strip() or "?"
            last_index[item_id] = i
        return [todos[i] for i in sorted(last_index.values())]


def todo_tool(
    todos: Optional[List[Dict[str, Any]]] = None,
    merge: bool = False,
    store: Optional[TodoStore] = None,
) -> str:
    """
    todo 工具的单一入口。根据参数决定读取还是写入。

    参数：
        todos：若提供，则写入这些条目。若为 None，则读取当前列表。
        merge：若为 True，按 id 更新。若为 False（默认），替换整个列表。
        store：来自 AIAgent 的 TodoStore 实例。

    返回：
        包含完整当前列表和摘要元数据的 JSON 字符串。
    """
    if store is None:
        return tool_error("TodoStore not initialized")

    if todos is not None:
        items = store.write(todos, merge)
    else:
        items = store.read()

    # 构建摘要计数
    pending = sum(1 for i in items if i["status"] == "pending")
    in_progress = sum(1 for i in items if i["status"] == "in_progress")
    completed = sum(1 for i in items if i["status"] == "completed")
    cancelled = sum(1 for i in items if i["status"] == "cancelled")

    return json.dumps({
        "todos": items,
        "summary": {
            "total": len(items),
            "pending": pending,
            "in_progress": in_progress,
            "completed": completed,
            "cancelled": cancelled,
        },
    }, ensure_ascii=False)


def check_todo_requirements() -> bool:
    """todo 工具没有外部依赖要求——始终可用。"""
    return True


# =============================================================================
# OpenAI 函数调用 Schema
# =============================================================================
# 行为引导被固化在 description 中，使其成为静态工具 schema 的一部分
# （会被缓存，且不会在对话中途改变）。

TODO_SCHEMA = {
    "name": "todo",
    "description": (
        "Manage your task list for the current session. Use for complex tasks "
        "with 3+ steps or when the user provides multiple tasks. "
        "Call with no parameters to read the current list.\n\n"
        "Writing:\n"
        "- Provide 'todos' array to create/update items\n"
        "- merge=false (default): replace the entire list with a fresh plan\n"
        "- merge=true: update existing items by id, add any new ones\n\n"
        "Each item: {id: string, content: string, "
        "status: pending|in_progress|completed|cancelled}\n"
        "List order is priority. Only ONE item in_progress at a time.\n"
        "Mark items completed immediately when done. If something fails, "
        "cancel it and add a revised item.\n\n"
        "Always returns the full current list."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "Task items to write. Omit to read current list.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Unique item identifier"
                        },
                        "content": {
                            "type": "string",
                            "description": "Task description"
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "cancelled"],
                            "description": "Current status"
                        }
                    },
                    "required": ["id", "content", "status"]
                }
            },
            "merge": {
                "type": "boolean",
                "description": (
                    "true: update existing items by id, add new ones. "
                    "false (default): replace the entire list."
                ),
                "default": False
            }
        },
        "required": []
    }
}


# --- 注册 ---
from tools.registry import registry, tool_error

registry.register(
    name="todo",
    toolset="todo",
    schema=TODO_SCHEMA,
    handler=lambda args, **kw: todo_tool(
        todos=args.get("todos"), merge=args.get("merge", False), store=kw.get("store")),
    check_fn=check_todo_requirements,
    emoji="📋",
)
