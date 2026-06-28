"""结构化流式事件 —— agent→gateway 的投递契约。

历史上，agent 通过一组弱类型回调来驱动 gateway 投递（``stream_delta_callback(text)``、
``tool_progress_callback(event_type, tool_name, preview, args)``、
``interim_assistant_callback(text)``……），每个 gateway 回调都*同时*决定渲染什么
以及如何发送。正是这种耦合导致 tool-progress 气泡与流式 draft 在 Telegram 上互相
竞争，也导致工具调用格式化停留在 agent 侧 —— 尽管只有 gateway 才知道某个平台能
渲染什么。

本模块定义了一组小而类型化的事件词汇，只描述*发生了什么*，而不规定*如何投递*。
gateway 的 stream consumer（``GatewayStreamConsumer``）是唯一的 sink；平台 adapter
决定如何渲染每个事件（Telegram 可以把一个 MarkdownV2 ```bash``` 块作为原生 draft
流式发送；iMessage 没有富格式，可能折叠或丢弃 tool chrome）。关注点分离：聪明的
agent 发出结构化数据，聪明的 gateway 决定投递方式。

这些都是刻意为之的纯 frozen dataclass —— 没有行为，没有平台知识，没有 I/O。它们
在 agent 的工作线程上构造代价很低，并且可以安全地跨线程/异步边界传递到 consumer
队列。

设计约束（见 hermes-agent-dev skill —— message-flow + cache invariants）：
  * 事件描述的是 *transport*，绝不是 *context*。这里没有任何内容会被持久化到对话
    历史；gateway 选择"吃掉"的内容（例如某个无法渲染 tool chrome 的平台上的 tool
    chrome）绝不能与 agent 消息历史中存储的字节产生分歧。历史由 agent 拥有；这些
    事件只是表示层的流。
  * 构造上向后兼容。gateway 在边界处把现有回调适配为这些事件；未选择事件原生渲染的
    adapter 通过基类默认实现获得完全相同的行为。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Union


# ── 消息（assistant 文本）事件 ───────────────────────────────────────────────

@dataclass(frozen=True)
class MessageChunk:
    """一段流式 assistant 文本的增量。

    ``text`` 是从模型到达的增量内容。consumer 会累积这些 chunk 并渐进式地渲染
    （Telegram DM 用原生 draft，其他平台就地编辑）。推理/think-block 内容已在
    上游过滤，永远不会作为 MessageChunk 到达。
    """
    text: str


@dataclass(frozen=True)
class MessageStop:
    """当前的 assistant 消息分段已完成。

    当一段连续的 assistant 文本结束时触发 —— 可能是整个响应结束，也可能是工具边界
    打断了文本，因此下一段应在所有 tool chrome *下方* 渲染为一条新消息。

    ``final`` 仅在整个 turn 的最后一次终止时为 True；中间的终止（文本 → 工具调用
    → 更多文本）携带 ``final=False``，以便 consumer 完成当前气泡并准备新分段，而
    不把 turn 当作已结束。
    """
    final: bool = False


@dataclass(frozen=True)
class Commentary:
    """在工具迭代之间发出的一条完整的过渡性 assistant 消息。

    例如：模型在发出工具调用前说"我先检查一下仓库。"。与 MessageChunk 不同，这是
    已完成的文本（不是增量）；consumer 把它渲染为单独的消息，使其读起来是一个独立
    的节拍。
    """
    text: str


# ── 工具调用事件 ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolCallChunk:
    """一个工具调用已开始（或其进行中状态发生了变化）。

    携带该调用的原始事实 —— 名称、简短的参数 ``preview``，以及完整的 ``args``
    字典 —— 并让 *gateway* 决定呈现方式（emoji、截断、verbose 还是 compact，或在
    不显示 tool chrome 的平台上直接吃掉它）。此前 agent 的 gateway 回调把 emoji +
    preview 的格式化硬编码在里面；该决定现在归属 adapter。
    """
    tool_name: str
    preview: Optional[str] = None
    args: Optional[Dict[str, Any]] = None
    # 单调递增的 per-turn 序号，使 consumer 能把完成事件与起始事件关联，并让
    # "new" 模式去重（仅在工具变化时上报）生效，而无需 consumer 自行跟踪调用顺序。
    index: int = 0


@dataclass(frozen=True)
class ToolCallFinished:
    """一个工具调用已完成。

    ``duration`` 是墙上时钟秒数。``ok`` 反映该工具是否无异常地返回。gateway 用它来
    清除/收尾进度气泡，并驱动一次性的上手提示（例如在一次长时间工具运行后建议
    /verbose）。没有工具 *输出* 在此传播 —— 输出是 agent 的关注点，会持久化到历史，
    而不作为呈现层流式发送。
    """
    tool_name: str
    duration: float = 0.0
    ok: bool = True
    index: int = 0


# ── gateway 控制 / 生命周期事件 ──────────────────────────────────────────────

@dataclass(frozen=True)
class LongToolHint:
    """当工具运行时间超过阈值时的一次性上手提示。

    gateway 会根据平台能力（/verbose 命令必须可用）以及用户是否已见过该提示来门控。
    将其建模为事件，使 *gateway* 而非 agent 拥有"我是否应在此处展示？"的决定权。
    """
    tool_name: str = ""
    duration: float = 0.0


@dataclass(frozen=True)
class GatewayNotice:
    """由 gateway 发起的控制消息（重启、上线、长运行通知）。

    ``kind`` 是 adapter 可用于 switch 的稳定字符串（``"restart"`` / ``"online"`` /
    ``"long_run"`` / ……）。``text`` 是基类在 adapter 没有平台特定处理时渲染的
    人类可读默认值。
    """
    kind: str
    text: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


# consumer 的 dispatcher 接受的所有事件的联合类型。刻意写全（而不是用一个标记基类），
# 这样在穷举 match 中漏写某个 ``case`` 会是明显的类型错误，而不是静默 fall-through。
StreamEvent = Union[
    MessageChunk,
    MessageStop,
    Commentary,
    ToolCallChunk,
    ToolCallFinished,
    LongToolHint,
    GatewayNotice,
]


__all__ = [
    "MessageChunk",
    "MessageStop",
    "Commentary",
    "ToolCallChunk",
    "ToolCallFinished",
    "LongToolHint",
    "GatewayNotice",
    "StreamEvent",
]
