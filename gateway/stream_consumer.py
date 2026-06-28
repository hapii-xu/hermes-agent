"""gateway 流式 consumer —— 在同步 agent 回调与异步平台投递之间搭桥。

agent 在其工作线程中同步触发 stream_delta_callback(text)。
GatewayStreamConsumer：
  1. 通过 on_delta() 接收增量（线程安全、同步）
  2. 通过 queue.Queue 把它们入队到一个 asyncio 任务
  3. 异步 run() 任务负责缓冲、限速，并渐进式地编辑目标平台上的单条消息

设计：使用 edit 传输（先发送初始消息，再 editMessageText）。
这在 Telegram、Discord 和 Slack 之间普遍受支持。

致谢：jobless0x（#774、#1312）、OutThisLife（#798）、clicksingh（#697）。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import queue
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from gateway.platforms.base import BasePlatformAdapter as _BasePlatformAdapter
from gateway.platforms.base import _custom_unit_to_cp
from gateway.platforms.base import MEDIA_TAG_CLEANUP_RE
from gateway.config import (
    DEFAULT_STREAMING_EDIT_INTERVAL as _DEFAULT_STREAMING_EDIT_INTERVAL,
    DEFAULT_STREAMING_BUFFER_THRESHOLD as _DEFAULT_STREAMING_BUFFER_THRESHOLD,
    DEFAULT_STREAMING_CURSOR as _DEFAULT_STREAMING_CURSOR,
)

logger = logging.getLogger("gateway.stream_consumer")

# 标记流已完成的哨兵
_DONE = object()

# 标记工具边界的哨兵 —— 完成当前消息并开始一条新消息，使后续文本出现在 tool
# progress 消息的下方。
_NEW_SEGMENT = object()

# 队列标记：在 API/工具迭代之间发出的一条已完成 assistant commentary 消息
#（例如："我先检查一下仓库。"）。
_COMMENTARY = object()


@dataclass
class StreamConsumerConfig:
    """单个 stream consumer 实例的运行时配置。"""
    edit_interval: float = _DEFAULT_STREAMING_EDIT_INTERVAL
    buffer_threshold: int = _DEFAULT_STREAMING_BUFFER_THRESHOLD
    cursor: str = _DEFAULT_STREAMING_CURSOR
    buffer_only: bool = False
    # 当 >0 时，如果原始 preview 已经可见至少这么多元秒，则流式响应的最终 edit 会作为
    # 一条全新消息投递。这使平台的可见时间戳反映完成时间而非首个 token 的时间，适用于
    # 长时间运行的响应（例如流式很慢的推理模型）。移植自 openclaw/openclaw#72038。
    # 默认 0 = 总是就地编辑（旧行为）。gateway 会按平台选择性地启用此项。
    fresh_final_after_seconds: float = 0.0
    # 流式传输方式选择：
    #   "auto"  — 当 adapter + chat 支持时优先使用原生 draft 流式（例如 Telegram
    #             sendMessageDraft）；否则回退到 edit。
    #   "draft" — 显式请求原生 draft 流式；不支持时回退到 edit。
    #   "edit"  — 渐进式 editMessageText（旧/默认行为）。
    #   "off"   — 由 gateway 在 consumer 构建之前就处理掉。
    transport: str = "edit"
    # 给 consumer 的来源 chat 类型提示（例如 "dm"、"group"、"supergroup"、"forum"）。
    # 用于门控原生 draft 流式，该功能与平台相关（Telegram 的 draft 仅限 DM）。
    chat_type: str = ""


class GatewayStreamConsumer:
    """异步 consumer，用流式 token 渐进式地编辑平台消息。

    用法::

        consumer = GatewayStreamConsumer(adapter, chat_id, config, metadata=metadata)
        # 把 consumer.on_delta 作为 stream_delta_callback 传给 AIAgent
        agent = AIAgent(..., stream_delta_callback=consumer.on_delta)
        # 把 consumer 作为 asyncio 任务启动
        task = asyncio.create_task(consumer.run())
        # ... 在线程池中运行 agent ...
        consumer.finish()  # 发出完成信号
        await task         # 等待最终 edit
    """

    # 连续发生这么多次 flood-control 失败后，在本次流的剩余部分永久禁用渐进式编辑。
    _MAX_FLOOD_STRIKES = 3

    # 模型在内容中内联输出的推理/思考标签。
    # 必须与 cli.py 的 _OPEN_TAGS/_CLOSE_TAGS 以及 run_agent.py 的
    # _strip_think_blocks() 标签变体保持同步。
    _OPEN_THINK_TAGS = (
        "<REASONING_SCRATCHPAD>", "<think>", "<reasoning>",
        "<THINKING>", "<thinking>", "<thought>",
    )
    _CLOSE_THINK_TAGS = (
        "</REASONING_SCRATCHPAD>", "</think>", "</reasoning>",
        "</THINKING>", "</thinking>", "</thought>",
    )

    # 用于原生流式 draft id 的类级单调计数器。当同一个 draft_id 在同一个 chat 中
    # 连续调用中被复用时，Telegram 会为该 draft 播放动画，因此每次响应都需要一个新的
    # 非零 id。
    _draft_id_counter: int = 0

    def __init__(
        self,
        adapter: Any,
        chat_id: str,
        config: Optional[StreamConsumerConfig] = None,
        metadata: Optional[dict] = None,
        on_new_message: Optional[callable] = None,
        on_before_finalize: Optional[Callable[[], Any]] = None,
        initial_reply_to_id: Optional[str] = None,
    ):
        self.adapter = adapter
        self.chat_id = chat_id
        self.cfg = config or StreamConsumerConfig()
        self.metadata = metadata
        # 当平台上一条新的内容气泡被创建时触发（新消息首次发送、commentary、溢出
        # 分块，或回退续接消息）。gateway 用它来线性化 tool-progress 气泡：当内容
        # 在一批工具调用之后恢复时，下一个 tool.started 应当在内容*下方*打开一个
        # 新的 progress 气泡，而不是编辑上方的旧气泡。
        # 不带参数调用。异常会被吞掉。
        self._on_new_message = on_new_message
        # 当流进入其收尾路径时触发一次。gateway 调用方在缓慢的最终富文本 edit
        #（Telegram MarkdownV2 finalize 等）之前用它暂停 typing 刷新。
        self._on_before_finalize = on_before_finalize
        self._initial_reply_to_id = initial_reply_to_id
        self._queue: queue.Queue = queue.Queue()
        self._accumulated = ""
        self._message_id: Optional[str] = None
        # 首次成功 first-send 赋值 ``_message_id`` 时的墙上时钟时间戳
        #（time.monotonic）。供 fresh-final 逻辑检测长生命周期的 preview —— 这些
        # preview 的 edit 时间戳在完成时会过期。移植自 openclaw/openclaw#72038。
        self._message_created_ts: Optional[float] = None
        # 本次响应期间 consumer 在屏幕上显示的每一个真实 preview 消息 id（首次发送
        # + 任何来自超大 edit/send 的续接消息）。fresh-final 路径在把完整答案作为单条
        #（富文本）消息重新投递时会删除所有这些 id，这样流式过程中因超出平台 edit 上限
        # 而被拆分的回复就不会在最终消息上方留下过期片段。
        self._preview_message_ids: "set[str]" = set()
        self._already_sent = False
        self._edit_supported = True  # 当渐进式 edit 不再可用时禁用
        self._last_edit_time = 0.0
        self._last_sent_text = ""   # 记录上次发送的文本以跳过冗余 edit
        # 当最近一次 _send_or_edit 拆分并跨续接消息投递时（adapter 采纳了新的消息 id）为 True。
        self._last_edit_overflowed = False
        self._fallback_final_send = False
        self._fallback_prefix = ""
        # 当 fallback 只是在 Telegram 部分溢出投递后补发缺失的尾部时为 True。此时已可见
        # 的前缀是有意保留的内容，而不是要删除的过期 preview。
        self._fallback_preserve_partial_messages = False
        self._flood_strikes = 0         # 连续的 flood-control edit 失败次数
        self._current_edit_interval = self.cfg.edit_interval  # 自适应退避
        self._final_response_sent = False
        # 当最终响应内容已通过流式发送给用户时设置，即使随后的最终 edit（移除光标等）
        # 失败也是如此。
        self._final_content_delivered = False
        # 缓存 adapter 的生命周期能力：只有需要显式 finalize 调用的平台（例如 DingTalk
        # AI Cards）才会迫使我们做一次冗余的最终 edit。其他平台保持快速路径。
        # 使用 ``is True``（而非 ``bool(...)``）以便测试中的 MagicMock 属性访问
        # 不会错误地启用此路径。
        self._adapter_requires_finalize: bool = (
            getattr(adapter, "REQUIRES_EDIT_FINALIZE", False) is True
        )

        # think-block 过滤状态（对应 CLI 的 _stream_delta 标签抑制逻辑）
        self._in_think_block = False
        self._think_buffer = ""

        # 原生 draft 流式状态。在 run() 开始时根据 cfg.transport、cfg.chat_type 和
        # adapter 的 supports_draft_streaming() 探测结果解析。当为 True 时，consumer
        # 通过 adapter.send_draft 发送动画 draft 帧，而不是通过 adapter.edit_message
        # 做渐进式 edit。最终答案仍通过常规的 first-send 路径投递，这样用户聊天历史
        # 里会有一条真实消息（draft 没有 message_id）。
        self._use_draft_streaming = False
        self._draft_id: Optional[int] = None
        # 本 consumer 的累计 draft 帧失败计数。首次失败后我们会在本次响应的剩余部分
        # 永久禁用 draft，并通过 edit 路径优雅降级。
        self._draft_failures = 0
        self._before_finalize_notified = False

    def _metadata_for_send(
        self,
        *,
        final: bool = False,
        expect_edits: bool = False,
    ) -> dict | None:
        """返回流式创建消息的 per-send metadata。

        Mattermost 在决定一个损坏的 thread 根是否可以扁平回退时，会把值得通知的
        send 视为用户可见的最终内容。preview 和 progress 的 send 保留其原始 metadata
        并保持 thread 严格性。

        ``expect_edits`` 保留了上游 Telegram 的流式契约：后续可能被 edit 的 preview
        消息必须留在可编辑的旧 send 路径上，而全新的/回退的最终 send 仍可使用更丰富
        的最终消息投递方式。
        """
        meta = dict(self.metadata) if self.metadata else {}
        if expect_edits:
            meta["expect_edits"] = True
        if final:
            meta["notify"] = True
        return meta or None

    @property
    def already_sent(self) -> bool:
        """本次运行期间是否至少发送或编辑过一条消息。"""
        return self._already_sent

    @property
    def final_response_sent(self) -> bool:
        """stream consumer 是否已投递最终的 assistant 回复。"""
        return self._final_response_sent

    @property
    def message_id(self) -> str | None:
        """最后发送或编辑的消息的 Discord/chat 消息 ID。"""
        return self._message_id

    @property
    def final_content_delivered(self) -> bool:
        """最终响应内容是否已到达用户，即使随后的装饰性 edit（移除光标）失败。"""
        return self._final_content_delivered

    async def _notify_before_finalize(self) -> None:
        """仅运行一次 pre-finalize 钩子，吞掉钩子抛出的错误。"""
        if self._before_finalize_notified:
            return
        self._before_finalize_notified = True
        if self._on_before_finalize is None:
            return
        try:
            result = self._on_before_finalize()
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass

    async def _edit_message(
        self,
        *,
        message_id: str,
        content: str,
        finalize: bool = False,
    ):
        """通过 adapter 执行 edit，在支持时传入路由 metadata。"""
        kwargs = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "content": content,
        }
        # 保留 stream-consumer 长期以来的契约：具体 adapter 必须接受 finalize=
        #（即使为 False，由测试守护）。
        kwargs["finalize"] = finalize

        if self.metadata:
            try:
                params = inspect.signature(self.adapter.edit_message).parameters
                if "metadata" in params or any(
                    param.kind is inspect.Parameter.VAR_KEYWORD
                    for param in params.values()
                ):
                    kwargs["metadata"] = self.metadata
            except (TypeError, ValueError):
                pass
        return await self.adapter.edit_message(**kwargs)

    def on_segment_break(self) -> None:
        """完成当前流式分段并开始一条新消息。"""
        self._queue.put(_NEW_SEGMENT)

    def on_commentary(self, text: str) -> None:
        """把一条已完成的过渡性 assistant commentary 消息入队。"""
        if text:
            self._queue.put((_COMMENTARY, text))

    def _notify_new_message(self) -> None:
        """触发 on_new_message 回调，吞掉任何错误。"""
        cb = self._on_new_message
        if cb is None:
            return
        try:
            cb()
        except Exception:
            logger.debug("on_new_message callback error", exc_info=True)

    def _reset_segment_state(self, *, preserve_no_edit: bool = False) -> None:
        if preserve_no_edit and self._message_id == "__no_edit__":
            return
        self._message_id = None
        self._message_created_ts = None
        self._accumulated = ""
        self._last_sent_text = ""
        self._fallback_final_send = False
        self._fallback_prefix = ""
        self._fallback_preserve_partial_messages = False
        # #29346：工具/分段边界意味着我们此前投递的是过渡性前言，而不是最终答案 ——
        # 清除这些 flag，以防某个过早的 setter 欺骗 gateway。安全：got_done 在任何 reset
        # 之前返回，而 run.py 仅在 consumer 任务退出后才读取这些值。
        self._final_response_sent = False
        self._final_content_delivered = False
        # 原生 draft 流式：递增 draft_id，使下一段文本作为全新的 preview 在
        # tool-progress 气泡下方播放动画，而不是覆盖上一段已完成的 draft。这正是我们
        # 避免 openclaw 在其 issue #32535 中记录的"工具调用之间的文本泄漏"失败模式
        # 的方式 —— 每个文本块通过 finalize 成为一条可见消息，然后下一段再以一个新
        # draft 播放动画。
        if self._use_draft_streaming:
            type(self)._draft_id_counter += 1
            self._draft_id = type(self)._draft_id_counter

    def on_delta(self, text: str) -> None:
        """线程安全的回调 —— 从 agent 的工作线程中调用。

        当 *text* 为 ``None`` 时，表示一个工具边界：当前消息会被完成，后续文本将
        作为一条新消息发送，使其出现在 gateway 在其间发送的任何 tool-progress 消息
        下方。
        """
        if text:
            self._queue.put(text)
        elif text is None:
            self.on_segment_break()

    def finish(self) -> None:
        """发出流已完成的信号。"""
        self._queue.put(_DONE)

    # ── think-block 过滤 ─────────────────────────────────────────────
    # 像 MiniMax 这样的模型会在其内容中内联输出 <think>...</think> 块。CLI 的
    # _stream_delta 通过一个状态机来抑制它们；我们在这里做同样的事，使 gateway
    # 用户永远不会看到原始推理标签。agent 也会从最终响应中剥离它们
    #（run_agent.py 的 _strip_think_blocks），但 stream consumer 在剥离发生之前
    # 就发送了中间 edit。

    def _filter_and_accumulate(self, text: str) -> None:
        """把一个文本增量加入已累积的缓冲区，抑制 think block。

        使用一个状态机来跟踪我们是否处于一个推理/思考块内部。此类块内部的文本会被
        静默丢弃。缓冲区边界处的不完整标签会暂存在 ``_think_buffer`` 中，直到足够的
        字符到达可以做出判断。
        """
        buf = self._think_buffer + text
        self._think_buffer = ""

        while buf:
            if self._in_think_block:
                # 寻找最早的闭合标签
                best_idx = -1
                best_len = 0
                for tag in self._CLOSE_THINK_TAGS:
                    idx = buf.find(tag)
                    if idx != -1 and (best_idx == -1 or idx < best_idx):
                        best_idx = idx
                        best_len = len(tag)

                if best_len:
                    # 找到闭合标签 —— 丢弃块，处理剩余部分
                    self._in_think_block = False
                    buf = buf[best_idx + best_len:]
                else:
                    # 尚无闭合标签 —— 暂存可能是不完整闭合标签前缀的尾部，丢弃其余部分。
                    max_tag = max(len(t) for t in self._CLOSE_THINK_TAGS)
                    self._think_buffer = buf[-max_tag:] if len(buf) > max_tag else buf
                    return
            else:
                # 在块边界处（文本开头 / 前面是换行 + 可选空白）寻找最早的开始标签。
                # 这可以避免模型在散文中*提及*标签时（例如 "the <think> tag is used for…"）
                # 出现误判。
                best_idx = -1
                best_len = 0
                for tag in self._OPEN_THINK_TAGS:
                    search_start = 0
                    while True:
                        idx = buf.find(tag, search_start)
                        if idx == -1:
                            break
                        # 块边界检查（对应 cli.py 的逻辑）
                        if idx == 0:
                            is_boundary = (
                                not self._accumulated
                                or self._accumulated.endswith("\n")
                            )
                        else:
                            preceding = buf[:idx]
                            last_nl = preceding.rfind("\n")
                            if last_nl == -1:
                                is_boundary = (
                                    (not self._accumulated
                                     or self._accumulated.endswith("\n"))
                                    and preceding.strip() == ""
                                )
                            else:
                                is_boundary = preceding[last_nl + 1:].strip() == ""

                        if is_boundary and (best_idx == -1 or idx < best_idx):
                            best_idx = idx
                            best_len = len(tag)
                            break  # 该标签的第一次边界命中就足够了
                        search_start = idx + 1

                if best_len:
                    # 输出标签之前的文本，进入 think block
                    self._accumulated += buf[:best_idx]
                    self._in_think_block = True
                    buf = buf[best_idx + best_len:]
                else:
                    # 没有开始标签 —— 检查尾部是否有不完整的标签
                    held_back = 0
                    for tag in self._OPEN_THINK_TAGS:
                        for i in range(1, len(tag)):
                            if buf.endswith(tag[:i]) and i > held_back:
                                held_back = i
                    if held_back:
                        self._accumulated += buf[:-held_back]
                        self._think_buffer = buf[-held_back:]
                    else:
                        self._accumulated += buf
                    return

    def _flush_think_buffer(self) -> None:
        """把暂存的不完整标签缓冲区刷入已累积文本。

        在流结束（got_done）时调用，使此前为等待可能的开始标签而暂存的不完整文本
        不会丢失。
        """
        if self._think_buffer and not self._in_think_block:
            self._accumulated += self._think_buffer
            self._think_buffer = ""

    async def run(self) -> None:
        """排空队列并编辑平台消息的异步任务。"""
        # 平台消息长度上限 —— 给光标 + 格式化留出空间。
        # 使用 adapter 的长度函数（例如 Telegram 的 utf16_len），使溢出检测与平台
        # 实际强制执行的规则一致。用 isinstance(BasePlatformAdapter) 做门控，使测试
        # 用的 MagicMock（其自动属性返回 mock 对象而非可调用对象）回退到 len。
        _len_fn: "Callable[[str], int]" = (
            self.adapter.message_len_fn
            if isinstance(self.adapter, _BasePlatformAdapter)
            else len
        )
        # 具备富文本能力的 adapter（Telegram 富文本消息）会把这个值抬高到旧的每条
        # 消息上限之上，使一条能放进一次富文本 send/draft 的回复在流式过程中不会在
        # 4096 处被拆分。参见 _raw_message_limit。
        _raw_limit = self._raw_message_limit()
        _safe_limit = max(500, _raw_limit - _len_fn(self.cfg.cursor) - 100)

        # 每次 run 解析一次原生 draft 流式。启用后，consumer 把流中段的帧通过
        # adapter.send_draft 路由，并保持 _message_id=None，使现有的 got_done 路径
        # 以一次常规 sendMessage 投递最终答案（draft 没有可 edit 的 message_id）。
        self._use_draft_streaming = self._resolve_draft_streaming()
        if self._use_draft_streaming:
            type(self)._draft_id_counter += 1
            self._draft_id = type(self)._draft_id_counter
            logger.debug(
                "Stream consumer using native-draft transport (chat=%s draft_id=%s)",
                self.chat_id, self._draft_id,
            )

        try:
            while True:
                # 从队列中排空所有可用项
                got_done = False
                got_segment_break = False
                commentary_text = None
                while True:
                    try:
                        item = self._queue.get_nowait()
                        if item is _DONE:
                            got_done = True
                            break
                        if item is _NEW_SEGMENT:
                            got_segment_break = True
                            break
                        if isinstance(item, tuple) and len(item) == 2 and item[0] is _COMMENTARY:
                            commentary_text = item[1]
                            break
                        self._filter_and_accumulate(item)
                    except queue.Empty:
                        break

                # 在流结束时刷出暂存的不完整标签缓冲区，使此前等待可能开始标签
                # 的尾部文本不会丢失。
                if got_done:
                    self._flush_think_buffer()

                # 决定是否刷新一次 edit
                now = time.monotonic()
                elapsed = now - self._last_edit_time
                should_edit = (
                    got_done
                    or got_segment_break
                    or commentary_text is not None
                )
                if not self.cfg.buffer_only:
                    should_edit = should_edit or (
                        (elapsed >= self._current_edit_interval
                            and self._accumulated)
                        # buffer_threshold 刻意按 codepoint 计算：它是一个去抖
                        # 启发式（"大约每 N 个可见字符发送一次更新"），而不是平台
                        # 上限检查。_len_fn 留给溢出检测使用。
                        or len(self._accumulated) >= self.cfg.buffer_threshold
                    )

                current_update_visible = False
                if should_edit and self._accumulated:
                    # 拆分溢出：如果累积文本超出平台上限，把它拆成大小合适的块。
                    if (
                        _len_fn(self._accumulated) > _safe_limit
                        and self._message_id is None
                    ):
                        # 没有可 edit 的现有消息（首条消息或分段中断之后）。使用
                        # truncate_message —— 即非流式路径使用的同一个辅助函数 —— 按
                        # 正确的单词/代码围栏边界拆分，并加上类似 "(1/2)" 的分块
                        # 标记。
                        chunks = self.adapter.truncate_message(
                            self._accumulated, _safe_limit, len_fn=_len_fn,
                        )
                        chunks_delivered = False
                        reply_to = self._message_id or self._initial_reply_to_id
                        for chunk in chunks:
                            new_id = await self._send_new_chunk(
                                chunk,
                                reply_to,
                                final=got_done,
                            )
                            if new_id is not None and new_id != reply_to:
                                chunks_delivered = True
                        self._accumulated = ""
                        self._last_sent_text = ""
                        self._last_edit_time = time.monotonic()
                        if got_done:
                            # 只有当这些块确实落地时才声明最终投递成功。
                            # ``_already_sent`` 可能因之前的 tool-progress edit 或
                            # fallback 模式提升（#10748）而为 True —— 这并不意味着最终
                            # 答案已到达用户。
                            self._final_response_sent = chunks_delivered
                            if chunks_delivered:
                                self._final_content_delivered = True
                            return
                        if got_segment_break:
                            self._message_id = None
                            self._fallback_final_send = False
                            self._fallback_prefix = ""
                        continue

                    # 已有消息：用第一块 edit 它，然后为新溢出余下部分开一条新消息。
                    while (
                        _len_fn(self._accumulated) > _safe_limit
                        and self._message_id is not None
                        and self._edit_supported
                    ):
                        _cp_budget = _custom_unit_to_cp(
                            self._accumulated, _safe_limit, _len_fn,
                        )
                        split_at = self._accumulated.rfind("\n", 0, _cp_budget)
                        if split_at < _safe_limit // 2:
                            split_at = _safe_limit
                        chunk = self._accumulated[:split_at]
                        # finalize=True 以便 adapter 应用平台相关的富文本标记
                        #（例如 Telegram MarkdownV2）。这个封存的块永不会再被 edit ——
                        # _message_id 在下面立即被重置为 None —— 因此它现在必须完成其
                        # 最终格式化，否则早期拆分消息会渲染成原始 markdown，而只有最后
                        # 一块能正确渲染。
                        # is_turn_final=False：这是多条拆分消息中的第一条，而不是
                        # turn 最终答案，所以 fresh-final 路径（可选的
                        # fresh_final_after_seconds）不得把 turn 标记为已投递（#29346
                        # 语义）。
                        ok = await self._send_or_edit(
                            chunk, finalize=True, is_turn_final=False,
                        )
                        if self._fallback_final_send or not ok:
                            # 在尝试拆分超大消息时 edit 失败（或因 flood control 退避）。
                            # 保留完整的累积文本，使 fallback 最终发送路径能投递剩余的
                            # 续接内容而不丢内容。
                            break
                        self._accumulated = self._accumulated[split_at:].lstrip("\n")
                        self._message_id = None
                        self._last_sent_text = ""

                    display_text = self._accumulated
                    if not got_done and not got_segment_break and commentary_text is None:
                        display_text += self.cfg.cursor

                    # 分段中断：完成当前消息，使需要显式收尾的平台（例如 DingTalk AI
                    # Cards）在下一段（tool progress、下一个块）在其下方创建新消息时，
                    # 不会让上一段停留在加载状态。got_done 有自己的收尾路径，因此此处
                    # 不为它做 finalize。
                    current_update_visible = await self._send_or_edit(
                        display_text,
                        finalize=(got_done or got_segment_break),
                        # 分段中断的 finalize 关闭的是一个前言，而不是 turn 最终答案
                        # —— 只有 got_done 才标记为已投递（#29346）。
                        is_turn_final=got_done,
                    )
                    self._last_edit_time = time.monotonic()

                if got_done:
                    if self._accumulated or self._message_id is not None or self._already_sent:
                        await self._notify_before_finalize()
                    # 不带光标的最终 edit。如果流式过程中渐进式 edit 失败，则在此发送一条
                    # 续接/回退消息，而不是让基类 gateway 路径再次发送完整响应。
                    if self._accumulated:
                        if self._fallback_final_send:
                            await self._send_fallback_final(self._accumulated)
                        elif self._final_response_sent:
                            # 上面的某次 finalize=True tick 已经通过 adapter 的
                            # fresh-final 路径投递了最终答案（_try_fresh_final 发送了一条
                            # 新的富文本消息并删除了 preview）。在此再跑一次 finalize
                            # edit 会重复消息/重复删除，因此只记录投递并停止。
                            self._final_content_delivered = True
                        elif (
                            current_update_visible
                            and (
                                not self._adapter_requires_finalize
                                or self._last_edit_overflowed
                            )
                        ):
                            # 上面的流中 edit 已经投递了最终累积内容。对于不需要显式
                            # finalize 信号的 adapter，以及当该 edit 跨续接消息拆分投递时
                            # 的任何 adapter，跳过冗余的最终 edit：拆分 edit 本身已携带
                            # finalize=True，用完整文本再次 finalize 会再次溢出拆分进被
                            # 采纳的续接消息，导致屏幕上出现重复分块。
                            self._final_response_sent = True
                            self._final_content_delivered = True
                        elif self._message_id:
                            # 或者流中 edit 没运行（本次 tick 没有可见更新），或者 adapter
                            # 需要显式 finalize=True 来关闭流。
                            self._final_response_sent = await self._send_or_edit(
                                self._accumulated, finalize=True,
                            )
                            if self._final_response_sent:
                                self._final_content_delivered = True
                            elif self._fallback_final_send:
                                # 最终 edit 尝试本身可能就是耗尽 flood-control 次数并把
                                # consumer 提升为 fallback 模式的那一次。不要在仍待处理的
                                # 完整响应 fallback 状态下返回给 gateway；在此只发送未发送
                                # 的尾部，使常规 gateway 发送路径不会重复可见前缀。
                                await self._send_fallback_final(self._accumulated)
                        elif not self._already_sent:
                            self._final_response_sent = await self._send_or_edit(self._accumulated)
                            if self._final_response_sent:
                                self._final_content_delivered = True
                    return

                if commentary_text is not None:
                    self._reset_segment_state()
                    await self._send_commentary(commentary_text)
                    self._last_edit_time = time.monotonic()
                    self._reset_segment_state()

                # 工具边界：重置消息状态，使下一个文本块在任何 tool-progress 消息下方
                # 创建一条新消息。
                #
                # 例外：当 _message_id 为 "__no_edit__" 时，平台从不返回真实的消息 ID
                #（例如 Signal、使用 github_comment 投递的 webhook）。把它重置为 None 会在
                # 每个工具边界重新进入"首次发送"路径，并为每个工具调用发送一条平台消息
                # —— 这正是导致一个 PR 下出现 155 条评论的原因。相反，保留该哨兵，使完整
                # 续接内容通过 _send_fallback_final 投递一次。
                #（当 edit 因 flood control 在流中失败时，id 是 "msg_1" 这样的真实
                # 字符串，而非 "__no_edit__"，因此该情况仍会重置并按预期创建新分段。）
                if got_segment_break:
                    # 如果分段中断的 edit 未能投递累积内容（尚未提升为 fallback 模式的
                    # flood control，或 fallback 模式本身），_accumulated 仍持有用户从未
                    # 看到的边界前文本。在下面的 reset 抹除 _accumulated 之前，把该尾部作
                    # 为续接消息刷出 —— 否则工具边界之前生成的文本会被静默丢弃
                    #（issue #8124）。
                    if (
                        self._accumulated
                        and not current_update_visible
                        and self._message_id
                        and self._message_id != "__no_edit__"
                    ):
                        await self._flush_segment_tail_on_edit_failure()
                    self._reset_segment_state(preserve_no_edit=True)

                await asyncio.sleep(0.05)  # 小让步，避免忙循环

        except asyncio.CancelledError:
            # 取消时尽力做最终 edit。finalize=True 使 REQUIRES_EDIT_FINALIZE 平台
            #（Telegram）应用最终格式化 —— 此处一次普通 edit 会让整条回复以原始流式
            # preview 形式渲染，而下面的成功 flag 又会抑制 gateway 的格式化重发。
            # is_turn_final=False 阻止 _try_fresh_final 自行设置
            # _final_response_sent；本 handler 拥有这些 flag。
            _best_effort_ok = False
            if self._accumulated and self._message_id:
                try:
                    _best_effort_ok = bool(
                        await self._send_or_edit(
                            self._accumulated, finalize=True, is_turn_final=False,
                        )
                    )
                except Exception:
                    pass
            # 只有当上面的尽力发送确实成功了，或者在被取消之前最终响应已被确认时，才
            # 确认最终投递。此前这会把任何部分发送（already_sent=True）提升为
            # final_response_sent —— 这会抑制 gateway 的 fallback 发送，即使只投递了
            # 中间文本（例如 "让我搜索一下…"）而非真正的答案。
            if _best_effort_ok and not self._final_response_sent:
                self._final_response_sent = True
                self._final_content_delivered = True
        except Exception as e:
            logger.error("Stream consumer error: %s", e)

    # 在显示前剥离 MEDIA:<path> 标签。使用 gateway/platforms/base.py 中共享的带锚点
    # MEDIA_TAG_CLEANUP_RE —— 只移除路径以可投递扩展名结尾的标签，使未知扩展名的
    # 路径保持可见，而不是被静默丢弃（issue #34517）。
    # 流式与非流式路径共用同一个 regex，因此无论由哪条路径投递文本，标签的处理都一致。
    _MEDIA_RE = MEDIA_TAG_CLEANUP_RE

    @staticmethod
    def _clean_for_display(text: str) -> str:
        """在显示前从文本中剥离 MEDIA: 指令和内部标记。

        流式路径投递的原始文本块可能包含 ``MEDIA:<path>`` 标签和
        ``[[audio_as_voice]]`` 指令，它们是供平台 adapter 后处理的。实际的媒体文件
        会在流结束后通过 ``_deliver_media_from_response()`` 单独投递 —— 我们只需
        对用户隐藏这些原始指令。
        """
        if "MEDIA:" not in text and "[[audio_as_voice]]" not in text:
            return text
        cleaned = text.replace("[[audio_as_voice]]", "")
        cleaned = GatewayStreamConsumer._MEDIA_RE.sub("", cleaned)
        # 折叠因移除标签而留下的过多空行
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        # 去除尾部空白/换行，但保留开头内容
        return cleaned.rstrip()

    async def _send_new_chunk(
        self,
        text: str,
        reply_to_id: Optional[str],
        *,
        final: bool = False,
    ) -> Optional[str]:
        """发送一个新消息块，可选地 thread 到上一条消息。

        返回 message_id，以便调用方对后续块做 thread。
        """
        text = self._clean_for_display(text)
        if not text.strip():
            return reply_to_id
        try:
            result = await self.adapter.send(
                chat_id=self.chat_id,
                content=text,
                reply_to=reply_to_id,
                metadata=self._metadata_for_send(final=final, expect_edits=True),
            )
            if result.success and result.message_id:
                self._message_id = str(result.message_id)
                self._track_preview_ids_from_result(result)
                self._already_sent = True
                self._last_sent_text = text
                # 全新内容气泡 —— 关闭上方任何过期 tool 气泡，使下一个工具在下方
                # 开启新气泡。
                self._notify_new_message()
                return str(result.message_id)
            else:
                self._edit_supported = False
                return reply_to_id
        except Exception as e:
            logger.error("Stream send chunk error: %s", e)
            return reply_to_id

    def _visible_prefix(self) -> str:
        """返回流式消息中已显示的可见文本。"""
        prefix = self._last_sent_text or ""
        if self.cfg.cursor and prefix.endswith(self.cfg.cursor):
            prefix = prefix[:-len(self.cfg.cursor)]
        return self._clean_for_display(prefix)

    def _continuation_text(self, final_text: str) -> str:
        """返回 final_text 中用户尚未看到的那些部分。"""
        prefix = self._fallback_prefix or self._visible_prefix()
        if prefix and final_text.startswith(prefix):
            return final_text[len(prefix):].lstrip()
        return final_text

    @staticmethod
    def _split_text_chunks(
        text: str, limit: int,
        len_fn: "Callable[[str], int]" = len,
    ) -> list[str]:
        """把文本拆分成大小合理的块，供 fallback 发送使用。"""
        if len_fn(text) <= limit:
            return [text]
        chunks: list[str] = []
        remaining = text
        while len_fn(remaining) > limit:
            _cp_budget = _custom_unit_to_cp(remaining, limit, len_fn)
            split_at = remaining.rfind("\n", 0, _cp_budget)
            if split_at < limit // 2:
                split_at = limit
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip("\n")
        if remaining:
            chunks.append(remaining)
        return chunks

    async def _send_fallback_final(self, text: str) -> None:
        """在流式 edit 失效后发送最终续接内容。

        对每个块在 flood-control 失败时以较短延迟重试一次。
        """
        final_text = self._clean_for_display(text)
        continuation = self._continuation_text(final_text)
        self._fallback_final_send = False
        if not continuation.strip():
            # 没有新内容可发 —— 可见的部分已与最终文本匹配。
            # 但是：如果 final_text 本身有有意义的内容（例如长工具调用后的超时
            # 消息），基于前缀的续接计算可能错误地判定"已显示"，因为流式前缀
            # 来自*上一个*分段（工具边界之前）。此时，原样发送完整 final_text
            #（#10807）。
            if final_text.strip() and final_text != self._visible_prefix():
                continuation = final_text
            else:
                # 针对 #7183 的纵深防御：最后一次 edit 可能仍显示光标字符，因为
                # fallback 模式是在一次 edit 失败留下卡住光标后进入的。尝试一次最终
                # edit 把它剥掉，使消息不会冻结在可见的 ▉ 上。尽力而为 —— 如果此 edit
                # 也失败（flood control 仍激活），进入 fallback 时已调用过
                # _try_strip_cursor，自适应退避重试也已轮过。
                if (
                    self._message_id
                    and self._last_sent_text
                    and self.cfg.cursor
                    and self._last_sent_text.endswith(self.cfg.cursor)
                ):
                    clean_text = self._last_sent_text[:-len(self.cfg.cursor)]
                    try:
                        result = await self._edit_message(
                            message_id=self._message_id,
                            content=clean_text,
                        )
                        if result.success:
                            self._last_sent_text = clean_text
                    except Exception:
                        pass
                self._already_sent = True
                self._final_response_sent = True
                self._final_content_delivered = True
                return

        raw_limit = getattr(self.adapter, "MAX_MESSAGE_LENGTH", 4096)
        _len_fn: "Callable[[str], int]" = (
            self.adapter.message_len_fn
            if isinstance(self.adapter, _BasePlatformAdapter)
            else len
        )
        safe_limit = max(500, raw_limit - 100)
        chunks = self._split_text_chunks(continuation, safe_limit, len_fn=_len_fn)

        stale_message_id = self._message_id  # 待清理的部分消息
        last_message_id: Optional[str] = None
        last_successful_chunk = ""
        sent_any_chunk = False
        for chunk in chunks:
            # 尝试发送，在 flood-control 错误时重试一次。
            result = None
            for attempt in range(2):
                result = await self.adapter.send(
                    chat_id=self.chat_id,
                    content=chunk,
                    metadata=self._metadata_for_send(final=True),
                )
                if result.success:
                    break
                if attempt == 0 and self._is_flood_error(result):
                    logger.debug(
                        "Flood control on fallback send, retrying in 3s"
                    )
                    await asyncio.sleep(3.0)
                else:
                    break  # 非 flood 错误，或第二次尝试失败

            if not result or not result.success:
                if sent_any_chunk:
                    # 部分续接文本已到达用户，但不是完整响应。不要设置
                    # _final_response_sent —— 基类 gateway 的最终发送路径仍应投递完整
                    # 响应，让用户拿到完整答案。仅抑制 _already_sent 以避免对同一部分
                    # 内容重复发送。
                    self._already_sent = True
                    self._message_id = last_message_id
                    self._last_sent_text = last_successful_chunk
                    self._fallback_prefix = ""
                    return
                # 没有任何 fallback 块到达用户 —— 允许常规 gateway 最终发送路径再试一次。
                self._already_sent = False
                self._message_id = None
                self._last_sent_text = ""
                self._fallback_prefix = ""
                return
            sent_any_chunk = True
            last_successful_chunk = chunk
            last_message_id = result.message_id or last_message_id
            # 每个 fallback 块都是一条全新的平台消息 —— 发通知，使任何过期的
            # tool-progress 气泡被关闭。
            self._notify_new_message()

        # 移除冻结的部分消息，使用户只看到完整的 fallback 响应。仅当 fallback
        # 重新发送的是完整最终文本（continuation == final_text）时才安全。当上面基于
        # 前缀的去重只发送了缺失的尾部时，部分消息就是答案的头部 —— 删除它会让用户
        # 只看到响应的最后部分（即"Gemini 只发送了后半段"症状）。尽力而为 —— 如果平台
        # 没有实现 ``delete_message``、删除失败（flood control 仍激活、bot 缺少权限、
        # 消息太旧无法删除），部分消息会保留，但至少完整答案已投递。
        if (
            stale_message_id
            and stale_message_id != last_message_id
            and not self._fallback_preserve_partial_messages
            and continuation == final_text
        ):
            delete_fn = getattr(self.adapter, "delete_message", None)
            if delete_fn is not None:
                try:
                    await delete_fn(self.chat_id, stale_message_id)
                except Exception as e:
                    logger.debug(
                        "Fallback partial cleanup failed (%s): %s",
                        stale_message_id, e,
                    )

        self._message_id = last_message_id
        self._already_sent = True
        self._final_response_sent = True
        self._final_content_delivered = True
        self._last_sent_text = chunks[-1]
        self._fallback_prefix = ""
        self._fallback_preserve_partial_messages = False

    def _is_flood_error(self, result) -> bool:
        """检查一次 SendResult 失败是否由 flood control / rate limiting 导致。"""
        err = getattr(result, "error", "") or ""
        err_lower = err.lower()
        return "flood" in err_lower or "retry after" in err_lower or "rate" in err_lower

    def _resolve_draft_streaming(self) -> bool:
        """决定本次 run 是否应使用原生 draft 流式。

        遵循 ``cfg.transport``：
          * ``"edit"``  → 从不使用 draft（旧的渐进式 edit 路径）。
          * ``"draft"`` → 要求 draft 支持；adapter 拒绝时优雅回退到 edit。降级以
            debug 级别记录。
          * ``"auto"``  → 当 adapter 对此 chat 类型支持 draft 时使用；否则 edit。

        adapter 资格通过 :meth:`BasePlatformAdapter.supports_draft_streaming` 检查，
        该方法会考虑 chat 类型（例如 Telegram 的 draft 仅限 DM）和平台版本门控
        （例如 python-telegram-bot 22.6+）。
        """
        transport = (self.cfg.transport or "edit").lower()
        if transport == "edit":
            return False
        # "off" 由 gateway 在上游过滤；此处防御性地按 edit 处理。
        if transport == "off":
            return False
        # 测试 adapter 是 MagicMock，不是 BasePlatformAdapter 的子类；默认按 edit
        # 处理，以保留现有测试行为。
        if not isinstance(self.adapter, _BasePlatformAdapter):
            return False
        try:
            supported = self.adapter.supports_draft_streaming(
                chat_type=self.cfg.chat_type or None,
                metadata=self.metadata,
            )
        except Exception:
            logger.debug("supports_draft_streaming probe raised", exc_info=True)
            supported = False
        if not supported:
            if transport == "draft":
                logger.debug(
                    "Draft streaming requested but unsupported (chat=%s, type=%r) — "
                    "falling back to edit",
                    self.chat_id, self.cfg.chat_type,
                )
            return False
        return True

    async def _send_draft_frame(self, text: str) -> bool:
        """为当前累积文本发出一帧动画 draft。

        成功落地时返回 True。任何失败都会在本次 run 的剩余部分永久禁用 draft，
        使后续帧走基于 edit 的路径（可随 flood-control 退避等自适应）。draft 没有
        message_id，当响应通过一次常规 sendMessage 收尾时，客户端会自然清除它。
        """
        if self._draft_id is None:
            # 防御性处理：不应发生 —— _use_draft_streaming 门控与 _draft_id 在
            # run() 中是配套设置的。为安全起见禁用。
            self._use_draft_streaming = False
            return False
        try:
            result = await self.adapter.send_draft(
                chat_id=self.chat_id,
                draft_id=self._draft_id,
                content=text,
                metadata=self.metadata,
            )
        except Exception as e:
            logger.debug(
                "send_draft raised, disabling draft transport for this run: %s", e,
            )
            self._draft_failures += 1
            self._use_draft_streaming = False
            return False
        if not getattr(result, "success", False):
            logger.debug(
                "send_draft returned success=False, disabling draft transport: %s",
                getattr(result, "error", "unknown"),
            )
            self._draft_failures += 1
            self._use_draft_streaming = False
            return False
        # 帧已投递。记录文本，以便与基于 edit 的 no-op 跳过保持一致。
        self._last_sent_text = text
        return True

    async def _flush_segment_tail_on_edit_failure(self) -> None:
        """在分段中断 reset 之前投递尚未发送的尾部内容。

        当一次 edit 失败（flood control、传输错误）且下一次重试之前到达了工具
        边界时，``_accumulated`` 持有已生成但从未展示给用户的文本。若不在此刷出，
        分段 reset 会丢弃该尾部，并在部分消息中留下冻结的光标。

        把位于上次成功投递前缀之后的尾部作为新消息发送，并尽力从上一条部分消息中
        剥除卡住的光标。
        """
        if not self._fallback_final_send:
            await self._try_strip_cursor()
        visible = self._fallback_prefix or self._visible_prefix()
        tail = self._accumulated
        if visible and tail.startswith(visible):
            tail = tail[len(visible):].lstrip()
        tail = self._clean_for_display(tail)
        if not tail.strip():
            return
        try:
            result = await self.adapter.send(
                chat_id=self.chat_id,
                content=tail,
                metadata=self.metadata,
            )
            if result.success:
                self._already_sent = True
        except Exception as e:
            logger.error("Segment-break tail flush error: %s", e)

    async def _try_strip_cursor(self) -> None:
        """尽力做一次 edit，从最后一条可见消息中移除光标。

        在进入 fallback 模式时调用，使用户不会在部分消息中看到卡住的光标（▉）。
        """
        if not self._message_id or self._message_id == "__no_edit__":
            return
        prefix = self._visible_prefix()
        if not prefix or not prefix.strip():
            return
        try:
            await self._edit_message(
                message_id=self._message_id,
                content=prefix,
            )
            self._last_sent_text = prefix
        except Exception:
            pass  # 尽力而为 —— 不要让此操作阻塞 fallback 路径

    async def _send_commentary(self, text: str) -> bool:
        """发送一条已完成的过渡性 assistant commentary 消息。"""
        text = self._clean_for_display(text)
        if not text.strip():
            return False
        try:
            result = await self.adapter.send(
                chat_id=self.chat_id,
                content=text,
                metadata=self.metadata,
            )
            # 注意：此处不要设置 _already_sent = True。
            # commentary 消息是过渡性状态更新（例如 "正在使用 browser 工具..."），不是
            # 最终响应。设置 already_sent 会在有多次工具调用时错误地抑制最终响应。参见：
            # https://github.com/NousResearch/hermes-agent/issues/10454
            if result.success:
                # commentary 算作全新内容 —— 关闭其上方任何过期 tool 气泡，使下一个
                # 工具在下方开启新气泡。
                self._notify_new_message()
            return result.success
        except Exception as e:
            logger.error("Commentary send error: %s", e)
            return False

    def _should_send_fresh_final(self) -> bool:
        """当一个长生命周期的 preview 应被替换为一条全新最终消息而非 edit 时返回
        True。

        条件：
        - 已启用 fresh-final（``fresh_final_after_seconds > 0``）。
        - 持有一个真实 preview 消息 id（不是 ``__no_edit__`` 哨兵，也不是 ``None``）。
        - preview 已可见至少配置的阈值时长。

        移植自 openclaw/openclaw#72038。
        """
        threshold = getattr(self.cfg, "fresh_final_after_seconds", 0.0) or 0.0
        if threshold <= 0:
            return False
        if not self._message_id or self._message_id == "__no_edit__":
            return False
        if self._message_created_ts is None:
            return False
        age = time.monotonic() - self._message_created_ts
        return age >= threshold

    def _raw_message_limit(self) -> int:
        """每条消息的长度预算（以 adapter 的 ``message_len_fn`` 单位计），consumer
        会在此处拆分一条溢出的回复。

        具备更丰富 send/draft 路径的 adapter（例如 Telegram 富文本消息）可以通过
        ``streaming_overflow_limit`` 把该值抬到 ``MAX_MESSAGE_LENGTH`` 之上，使一条
        能放进一条富文本消息的回复不会在旧的 edit 上限处被拆分。其他情况回退到
        ``MAX_MESSAGE_LENGTH``（默认 4096）。
        """
        base = getattr(self.adapter, "MAX_MESSAGE_LENGTH", 4096)
        # isinstance 门控：MagicMock adapter 对任意属性访问返回 mock 对象（truthy，
        # 不是 int）—— 让它们保持在基础上限。
        if isinstance(self.adapter, _BasePlatformAdapter):
            try:
                cap = self.adapter.streaming_overflow_limit()
            except Exception as e:
                logger.debug("streaming_overflow_limit check failed: %s", e)
                cap = None
            if isinstance(cap, int) and cap > base:
                return cap
        return base

    def _track_preview_id(self, message_id: Optional[str]) -> None:
        """记录一个真实 preview 消息 id，供 fresh-final 清理使用。"""
        if message_id and message_id != "__no_edit__":
            self._preview_message_ids.add(str(message_id))

    def _track_preview_ids_from_result(self, result: Any) -> None:
        """记录一次 send/edit 结果暴露的每一个消息 id：主 id，以及任何来自超大
        拆分的续接 id（``continuation_message_ids`` 或
        ``raw_response['message_ids']``）。"""
        self._track_preview_id(getattr(result, "message_id", None))
        for mid in (getattr(result, "continuation_message_ids", None) or ()):
            self._track_preview_id(mid)
        raw = getattr(result, "raw_response", None) or {}
        if isinstance(raw, dict):
            for mid in (raw.get("message_ids") or ()):
                self._track_preview_id(mid)

    def _adapter_prefers_fresh_final(self, text: str) -> bool:
        """当 adapter 更倾向于通过发送一条新消息并删除 preview 来收尾一条流式回复，
        而不是就地编辑 preview 时返回 True —— 例如 Telegram，其
        ``sendRichMessage`` 发送路径目前渲染的 markdown 比 Hermes 的 MarkdownV2
        edit 路径更丰富。

        当没有真实 preview 可替换（没有消息 id，或 ``__no_edit__`` 哨兵）、adapter
        没有暴露该钩子、或发生任何错误时返回 False（consumer 随后保持就地编辑路径）。
        """
        if not self._message_id or self._message_id == "__no_edit__":
            return False
        fn = getattr(self.adapter, "prefers_fresh_final_streaming", None)
        if fn is None:
            return False
        try:
            try:
                result = fn(text, metadata=self.metadata)
            except TypeError:
                # adapter / 测试替身的钩子不接受 metadata 关键字 —— 回退到仅位置参数
                # 的形式。
                result = fn(text)
        except Exception as e:
            logger.debug("prefers_fresh_final_streaming check failed: %s", e)
            return False
        # ``is True``（而非 ``bool(...)``）以便测试中 MagicMock adapter 的自动子方法
        # —— 默认 truthy —— 不会错误地启用 fresh-final 路径。对应 __init__ 中的
        # REQUIRES_EDIT_FINALIZE 门控。
        return result is True

    async def _try_fresh_final(self, text: str, *, is_turn_final: bool = True) -> bool:
        """把 ``text`` 作为一条全新消息发送（尽力删除旧 preview），使平台的可见
        时间戳反映完成时间。投递成功返回 True，任何失败返回 False，以便调用方回退
        到常规 edit 路径。

        ``is_turn_final`` 为 False 时表示在工具边界处收尾的是过渡分段（前言），
        而非 turn 最终答案；此时不设置最终投递 flag，使 gateway 仍从下一次 API 调用
        投递真实答案（#29346）。

        移植自 openclaw/openclaw#72038。
        """
        # 用户在本次响应中看到的每一条 preview 消息：当前这条加上流式过程中记录
        # 的任何续接片段（一条超出平台 edit 上限被拆分的回复）。它们全部被下面
        # 那条新消息所取代。
        stale_ids = set(self._preview_message_ids)
        if self._message_id and self._message_id != "__no_edit__":
            stale_ids.add(self._message_id)
        try:
            result = await self.adapter.send(
                chat_id=self.chat_id,
                content=text,
                metadata=self._metadata_for_send(final=True),
            )
        except Exception as e:
            logger.debug("Fresh-final send failed, falling back to edit: %s", e)
            return False
        if not getattr(result, "success", False):
            return False
        # 把新消息 id 采纳为当前消息，使后续调用方（例如溢出拆分循环、finalize 重试）
        # 看到一致的状态。
        new_message_id = getattr(result, "message_id", None)
        # 成功的全新发送 —— 尽量删除过期 preview，使用户不会在下方看到旧的、edit 卡住
        # 的消息。清理是尽力而为；未实现 ``delete_message`` 的平台会保留 preview
        #（仍是可接受的结果 —— 可见的最终时间戳才是关键）。绝不删除我们刚发出的消息。
        delete_fn = getattr(self.adapter, "delete_message", None)
        if delete_fn is not None:
            for stale_id in stale_ids:
                if not stale_id or stale_id == "__no_edit__" or stale_id == new_message_id:
                    continue
                try:
                    await delete_fn(self.chat_id, stale_id)
                except Exception as e:
                    logger.debug(
                        "Fresh-final preview cleanup failed (%s): %s",
                        stale_id, e,
                    )
        self._preview_message_ids = set()
        if new_message_id:
            self._message_id = new_message_id
            self._message_created_ts = time.monotonic()
        else:
            # 发送成功但平台未返回 id —— 把投递视为 final-only 并回退到
            # "__no_edit__"，以免我们尝试去 edit 一个无法寻址的消息。
            self._message_id = "__no_edit__"
            self._message_created_ts = None
        self._already_sent = True
        self._last_sent_text = text
        if is_turn_final:
            self._final_response_sent = True
        return True

    async def _send_or_edit(
        self, text: str, *, finalize: bool = False, is_turn_final: bool = True,
    ) -> bool:
        """发送或编辑流式消息。

        文本成功投递（发送或编辑）时返回 True，否则返回 False。溢出拆分循环之类的
        调用方用它来决定是否推进过已投递的块。

        ``finalize`` 为 True 时表示这是流式序列中的最后一次 edit。
        """
        # 剥离 MEDIA: 指令，使其不作为可见文本出现。
        # 媒体文件在流结束后作为原生附件投递（通过 gateway/run.py 的
        # _deliver_media_from_response）。
        text = self._clean_for_display(text)
        # 一个光秃秃的流式光标不是有意义的用户可见内容，在某些客户端上可能渲染成
        # 杂乱的豆腐块/白块消息。
        visible_without_cursor = text
        if self.cfg.cursor:
            visible_without_cursor = visible_without_cursor.replace(self.cfg.cursor, "")
        _visible_stripped = visible_without_cursor.strip()
        if not _visible_stripped:
            return True  # 仅含光标 / 仅空白的更新
        if not text.strip():
            return True  # 没有可发送内容即为"成功"
        # 守卫：当唯一可见内容是流式光标旁的少数几个字符时，不要创建一条全新的独立
        # 消息。在快速工具调用期间，模型常在切换到工具调用之前输出 1-2 个 token；
        # 由此产生的 "X ▉" 消息，如果后续 edit（在分段中断时剥离光标）被平台限速，
        # 就会让光标永久可见。这在 Telegram、Matrix 以及其他把 ▉ 块字符渲染成可见
        # 白块（"豆腐"）的客户端上都被报告过。
        # 已有消息（edit）不受影响 —— 仅对首次发送做门控。
        _MIN_NEW_MSG_CHARS = 4
        if (self._message_id is None
                and self.cfg.cursor
                and self.cfg.cursor in text
                and len(_visible_stripped) < _MIN_NEW_MSG_CHARS):
            return True  # 对独立消息而言太短 —— 继续累积

        # 原生 draft 流式：把流中段的帧通过 send_draft 路由。最终答案通过下面的常规
        # sendMessage 路径投递 —— draft 没有 message_id，无法就地 finalize；常规
        # sendMessage 会自然清除客户端上的 draft，并在用户历史中留下一条真实消息。
        # 跳过条件：
        #   * finalize=True（这是最终答案；需要是一条真实消息）
        #   * 已建立 edit 路径（message_id 已设置，例如在工具边界分段中断之后，此前
        #     的文本已作为真实 sendMessage 收尾，而下一段文本继续编辑那条 —— 此分段
        #     留在基于 edit 的路径上是正确的）。
        if (
            self._use_draft_streaming
            and not finalize
            and self._message_id is None
        ):
            # No-op 跳过：与上次发送的帧相同。
            if text == self._last_sent_text:
                return True
            ok = await self._send_draft_frame(text)
            if ok:
                # draft 标记"我们已在屏幕上放了东西"，但不设置 _already_sent ——
                # 该 flag 门控 gateway 的 fallback 最终发送路径，我们仍需要它触发，
                # 以便用户得到一条真实消息（draft 没有 message_id）。
                return True
            # 失败已为本 run 禁用 draft；落到下面常规 edit/send 路径。
        self._last_edit_overflowed = False
        try:
            if self._message_id is not None:
                if self._edit_supported:
                    # 若文本与上次发送的相同则跳过。
                    # 例外：需要显式 finalize 调用的 adapter（REQUIRES_EDIT_FINALIZE）
                    # 即使内容未变也必须收到 finalize=True 的 edit，使其流式 UI 能从
                    # 进行中状态转换出来。其他 adapter 直接短路。
                    if text == self._last_sent_text and not (
                        finalize and self._adapter_requires_finalize
                    ):
                        return True
                    # 针对长生命周期 preview 的 fresh-final：当收尾流式序列的最后
                    # 一次 edit 时，如果原始 preview 已可见至少
                    # ``fresh_final_after_seconds``，则把完整回复作为一条新消息发送，
                    # 使平台的可见时间戳反映完成时间而非 preview 创建时间。旧 preview
                    # 的清理是尽力而为。移植自 openclaw/openclaw#72038。由 config 门控，
                    # 使旧的就地编辑路径保持为默认。
                    #
                    # adapter 也可以无视时间阈值，通过 prefers_fresh_final_streaming
                    # 主动选择（例如 Telegram，其 send 路径渲染的 markdown 比其 edit
                    # 路径更丰富）：通过 edit 收尾会明显降级一个富文本 preview，因此改
                    # 为作为新消息重新投递 + 删除 preview。
                    #
                    # 当 adapter 暴露 prefers_fresh_final_streaming 并显式返回 False 时，
                    # 基于时间的阈值不得覆盖该决定。在 Telegram 上，fresh-final 路径
                    # 发送一条 Rich Message（sendRichMessage），与流式过程中已可见的
                    # 旧 MarkdownV2 preview 重叠 —— 因为旧消息只是尽力删除，两条都会留
                    # 在屏幕上。没有该钩子的 adapter 仍走基于时间的 fresh-final。
                    #（#47048）
                    # 检查 *类* 是否有该钩子，以免 MagicMock adapter（访问时自动创建
                    # 属性）被误判为拥有它。同时检查实例 __dict__，以覆盖显式赋值该
                    # 属性的测试替身（例如 adapter.prefers_fresh_final_streaming =
                    # MagicMock(return_value=False)）。
                    _has_prefers_hook = (
                        hasattr(type(self.adapter),
                                "prefers_fresh_final_streaming")
                        or "prefers_fresh_final_streaming"
                            in getattr(self.adapter, "__dict__", {})
                    )
                    _prefers_fresh = self._adapter_prefers_fresh_final(text)
                    if (
                        finalize
                        and (
                            _prefers_fresh
                            or (
                                not _has_prefers_hook
                                and self._should_send_fresh_final()
                            )
                        )
                        and await self._try_fresh_final(
                            text, is_turn_final=is_turn_final,
                        )
                    ):
                        return True
                    # 编辑现有消息
                    result = await self._edit_message(
                        message_id=self._message_id,
                        content=text,
                        finalize=finalize,
                    )
                    if result.success:
                        self._already_sent = True
                        # 记录一次超大 edit 拆出的任何续接片段，以便 fresh-final 能
                        # 把它们全部清理掉。
                        self._track_preview_ids_from_result(result)
                        # adapter 可能把一次超大 edit 拆分并投递到原消息 + N 条续接
                        # 消息上。此时 ``message_id`` 是最后一条可见续接，而
                        # ``_last_sent_text`` 不再反映屏幕内容（新消息只持有最后一块
                        # 的文本），因此后续 edit 必须针对新 id，并且"相同则跳过"的
                        # 比较必须重置。触发 on_new_message，使 tool-progress 气泡
                        # 线性排列在新续接下方，而非原消息下方。
                        # ``getattr`` 带默认值，以保留对该字段出现之前测试中
                        # SimpleNamespace mock 的向后兼容。
                        _continuation_ids = getattr(result, "continuation_message_ids", ()) or ()
                        if (
                            _continuation_ids
                            and result.message_id
                            and result.message_id != self._message_id
                        ):
                            self._last_edit_overflowed = True
                            self._message_id = str(result.message_id)
                            self._message_created_ts = time.monotonic()
                            self._last_sent_text = ""
                            self._notify_new_message()
                        else:
                            self._last_sent_text = text
                        # 成功的 edit —— 重置 flood strike 计数器
                        self._flood_strikes = 0
                        return True
                    else:
                        if (
                            finalize
                            and is_turn_final
                            and self.cfg.cursor
                            and self._last_sent_text.endswith(self.cfg.cursor)
                            and self._visible_prefix() == text
                        ):
                            # 最终的清理 edit 失败，但完整答案已从上一帧流式内容
                            # 可见（通常只残留卡住的光标）。标记内容已投递，使
                            # gateway 抑制其常规的完整最终发送；否则当
                            # Telegram/Discord 对此装饰性最终 edit 限速时，用户会
                            # 两次看到同一条长答案（#36965、#25349）。
                            self._final_content_delivered = True
                        raw_response = getattr(result, "raw_response", None)
                        if isinstance(raw_response, dict) and raw_response.get("partial_overflow"):
                            # Telegram 编辑/发送了一个或多个溢出块，但不是完整响应。
                            # 保留可见前缀，使 got_done fallback 发送缺失的尾部，而不是
                            # 把一条被截断的 topic 回复标记为最终投递。
                            self._message_id = str(
                                raw_response.get("last_message_id")
                                or result.message_id
                                or self._message_id
                            )
                            delivered_prefix = raw_response.get("delivered_prefix")
                            if isinstance(delivered_prefix, str) and delivered_prefix:
                                self._last_sent_text = delivered_prefix
                                self._fallback_prefix = delivered_prefix
                                self._fallback_preserve_partial_messages = text.startswith(
                                    delivered_prefix
                                )
                            else:
                                self._fallback_prefix = self._visible_prefix()
                                self._fallback_preserve_partial_messages = False
                            self._fallback_final_send = True
                            self._edit_supported = False
                            self._already_sent = True
                            if getattr(result, "continuation_message_ids", ()):
                                self._notify_new_message()
                            return False

                        # edit 失败。如果看起来像 flood control / rate limiting，则
                        # 使用自适应退避：把 edit 间隔翻倍并在下一轮重试。只有在连续
                        # 失败 _MAX_FLOOD_STRIKES 次之后才永久禁用 edit。
                        if self._is_flood_error(result):
                            self._flood_strikes += 1
                            self._current_edit_interval = min(
                                self._current_edit_interval * 2, 10.0,
                            )
                            logger.debug(
                                "Flood control on edit (strike %d/%d), "
                                "backoff interval → %.1fs",
                                self._flood_strikes,
                                self._MAX_FLOOD_STRIKES,
                                self._current_edit_interval,
                            )
                            if self._flood_strikes < self._MAX_FLOOD_STRIKES:
                                # 先不要禁用 edit —— 只是放慢。
                                # 更新 _last_edit_time，使下一次 edit 遵守新间隔。
                                self._last_edit_time = time.monotonic()
                                return False

                        # 非 flood 错误，或 flood 次数耗尽：进入 fallback 模式 ——
                        # 一旦最终响应可用，只发送缺失的尾部。
                        logger.debug(
                            "Edit failed (strikes=%d), entering fallback mode",
                            self._flood_strikes,
                        )
                        self._fallback_prefix = self._visible_prefix()
                        self._fallback_final_send = True
                        self._edit_supported = False
                        self._already_sent = True
                        # 尽力而为：从最后一条可见消息中剥离光标，使用户不会看到
                        # 卡住的 ▉。
                        await self._try_strip_cursor()
                        return False
                else:
                    # 不支持编辑 —— 跳过中间更新。
                    # 最终响应会由 fallback 路径发送。
                    return False
            else:
                # 首条消息 —— 发送新消息，thread 到原始用户消息，使其落在正确的
                # topic/thread 中。
                result = await self.adapter.send(
                    chat_id=self.chat_id,
                    content=text,
                    reply_to=self._initial_reply_to_id,
                    metadata=self._metadata_for_send(
                        final=finalize,
                        expect_edits=True,
                    ),
                )
                if result.success:
                    if result.message_id:
                        self._message_id = result.message_id
                        # 记录 preview 首次对用户可见的时间，以便 fresh-final 逻辑
                        # 在长时运行响应上检测过期的 preview 时间戳。
                        self._message_created_ts = time.monotonic()
                        # 记录这条（以及任何来自超大首次发送的续接片段），供
                        # fresh-final 清理。
                        self._track_preview_ids_from_result(result)
                    else:
                        self._edit_supported = False
                    self._already_sent = True
                    self._last_sent_text = text
                    if not result.message_id:
                        self._fallback_prefix = self._visible_prefix()
                        self._fallback_final_send = True
                        # 该哨兵防止在平台接受消息但不返回可编辑消息 id 时，每个
                        # delta/工具边界都重新进入首次发送路径。
                        self._message_id = "__no_edit__"
                    # 通知 gateway 创建了全新的内容气泡，使上方任何累积的
                    # tool-progress 气泡被关闭 —— 下一个工具在下方的新气泡中触发，
                    # 保持时间顺序。
                    self._notify_new_message()
                    return True
                else:
                    # 初始发送失败 —— 为本次会话禁用流式
                    self._edit_supported = False
                    return False
        except Exception as e:
            logger.error("Stream send/edit error: %s", e)
            return False
