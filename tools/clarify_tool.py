#!/usr/bin/env python3
"""
Clarify 工具模块 —— 交互式澄清提问。

允许 agent 向用户呈现结构化的多选题或开放式提示。在 CLI 模式下，可用方向
键在选项间导航；在消息平台上，选项以编号列表形式呈现。

真正的用户交互逻辑位于平台层（CLI 为 cli.py，消息平台为 gateway/run.py）。
本模块只定义 schema、校验，以及一个轻量的分发器，把实际工作委托给平台
提供的回调。
"""

import json
from typing import List, Optional, Callable


# agent 最多能提供的预设选项数量。
# UI 总会在末尾追加第 5 个「其他（输入你的答案）」选项。
MAX_CHOICES = 4


def _flatten_choice(c) -> str:
    """把单个选项规整为面向用户的展示字符串。

    schema 声明选项为纯字符串，但 LLM 有时会发出字典形态的选项，例如
    ``[{"description": "..."}]``。简单地 ``str(c)`` 会把整个字典变成它的
    Python repr——``{'description': '...'}``——这会泄漏到每一个渲染该选项
    的界面（CLI 面板、Discord 按钮、Telegram 编号列表），并且原样作为用户
    的回答返回。在这里，即唯一与平台无关的入口处做归一化，能一处修复整类
    问题，而无需逐个适配器修改。

    字典解包顺序遵循 LLM 工具调用中面向用户的规范键：
    ``label`` → ``description`` → ``text`` → ``title``。``name`` 和
    ``value`` 被有意排除——它们是组件形态的字段，可能承载原始枚举值或短
    标识符，而非人类可读的标签。不包含任何规范键的字典会被丢弃（返回 ""），
    因为一个垃圾标签比没有选项更糟。
    """
    if c is None:
        return ""
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, dict):
        for key in ("label", "description", "text", "title"):
            v = c.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    if isinstance(c, (list, tuple)):
        return " ".join(_flatten_choice(x) for x in c).strip()
    return str(c).strip()


def clarify_tool(
    question: str,
    choices: Optional[List[str]] = None,
    callback: Optional[Callable] = None,
) -> str:
    """
    向用户提问，可选地附带多选项。

    参数：
        question: 要呈现的提问文本。
        choices:  最多 4 个预设答案选项。省略时提问为纯开放式。
        callback: 由平台提供的、处理实际 UI 交互的函数。签名：
                  callback(question, choices) -> str。由 agent 运行器
                  （cli.py / gateway）注入。

    返回：
        包含用户回答的 JSON 字符串。
    """
    if not question or not question.strip():
        return tool_error("Question text is required.")

    question = question.strip()

    # 校验并裁剪选项
    if choices is not None:
        if not isinstance(choices, list):
            return tool_error("choices must be a list of strings.")
        # LLM 有时会发出字典形态的选项（例如 [{"description": "..."}]），
        # 而不是纯字符串。_flatten_choice 在这里——即唯一与平台无关的入口
        # 处——把它们解包为面向用户的文本，使 CLI 面板、Discord 按钮和
        # Telegram 列表都能渲染干净的文本，且解析出的答案永远不会是原始的
        # Python 字典 repr。
        choices = [s for s in (_flatten_choice(c) for c in choices) if s]
        if len(choices) > MAX_CHOICES:
            choices = choices[:MAX_CHOICES]
        if not choices:
            choices = None  # 空列表 → 开放式

    if callback is None:
        return json.dumps(
            {"error": "Clarify tool is not available in this execution context."},
            ensure_ascii=False,
        )

    try:
        user_response = callback(question, choices)
    except Exception as exc:
        return json.dumps(
            {"error": f"Failed to get user input: {exc}"},
            ensure_ascii=False,
        )

    return json.dumps({
        "question": question,
        "choices_offered": choices,
        "user_response": str(user_response).strip(),
    }, ensure_ascii=False)


def check_clarify_requirements() -> bool:
    """Clarify 工具没有外部依赖 —— 始终可用。"""
    return True


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

CLARIFY_SCHEMA = {
    "name": "clarify",
    "description": (
        "Ask the user a question when you need clarification, feedback, or a "
        "decision before proceeding. Supports two modes:\n\n"
        "1. **Multiple choice** — provide up to 4 choices. The user picks one "
        "or types their own answer via a 5th 'Other' option.\n"
        "2. **Open-ended** — omit choices entirely. The user types a free-form "
        "response.\n\n"
        "CRITICAL: when you are offering options, put each option ONLY in the "
        "`choices` array — NEVER enumerate the options inside the `question` "
        "text. The UI renders `choices` as selectable rows; options written "
        "into the question string render as dead prose the user can't pick. "
        "Right: question='Which deployment target?', choices=['staging', "
        "'prod']. Wrong: question='Which target? 1) staging 2) prod', choices=[].\n\n"
        "Use this tool when:\n"
        "- The task is ambiguous and you need the user to choose an approach\n"
        "- You want post-task feedback ('How did that work out?')\n"
        "- You want to offer to save a skill or update memory\n"
        "- A decision has meaningful trade-offs the user should weigh in on\n\n"
        "Do NOT use this tool for simple yes/no confirmation of dangerous "
        "commands (the terminal tool handles that). Prefer making a reasonable "
        "default choice yourself when the decision is low-stakes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The question itself, and ONLY the question (e.g. 'Which "
                    "deployment target?'). Do NOT embed the answer options here "
                    "— pass them as separate elements in `choices`."
                ),
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_CHOICES,
                "description": (
                    "REQUIRED whenever you are presenting selectable options: "
                    "each distinct option is its own array element (up to 4). "
                    "The UI renders these as pickable rows and auto-appends an "
                    "'Other (type your answer)' option. Omit this parameter "
                    "entirely ONLY for a genuinely open-ended free-text question."
                ),
            },
        },
        "required": ["question"],
    },
}


# --- 注册表 ---
from tools.registry import registry, tool_error

registry.register(
    name="clarify",
    toolset="clarify",
    schema=CLARIFY_SCHEMA,
    handler=lambda args, **kw: clarify_tool(
        question=args.get("question", ""),
        choices=args.get("choices"),
        callback=kw.get("callback")),
    check_fn=check_clarify_requirements,
    emoji="❓",
)
