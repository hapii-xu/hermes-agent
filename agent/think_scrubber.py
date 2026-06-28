"""流式 assistant 文本中推理/思考块的有状态清洗器。

``run_agent._strip_think_blocks`` 基于正则表达式，对完整字符串处理正确，
但在 ``_fire_stream_delta`` 中*逐 delta* 运行时，
它会破坏下游消费者（CLI ``_stream_delta``、gateway
``GatewayStreamConsumer._filter_and_accumulate``）所依赖的状态。

具体来说，当 MiniMax-M2.7 流式输出：

    delta1 = "<think>"
    delta2 = "Let me check their config"
    delta3 = "</think>"

逐 delta 的正则表达式会完全删除 delta1（情形 2：边界处未终止的开标签
匹配 ``^<think>...``），因此下游状态机永远看不到开标签，
将 delta2 视为普通内容，从而将推理内容泄漏给用户。
不运行自有状态机的消费者（ACP、api_server、TTS）根本没有任何防御
—— 它们只是输出上游正则表达式处理后剩余的内容。

本模块在上游层集中管理标签抑制状态机，
使每个 stream_delta_callback 接收到的文本都已移除推理块。
delta 边界处的不完整标签会被暂存，等待下一个 delta 解析，
流结束时刷新会暴露任何最终发现不是真实标签前缀的暂存内容。

用法::

    scrubber = StreamingThinkScrubber()
    for delta in stream:
        visible = scrubber.feed(delta)
        if visible:
            emit(visible)
    tail = scrubber.flush()  # 流结束时
    if tail:
        emit(tail)

清洗器对每个 agent 实例是可重入的。在每个新 turn 开始时调用 ``reset()``，
防止被中断的上一个流中卡住的块污染下一个 turn 的输出。

处理的标签变体（不区分大小写）：
  ``<think>``、``<thinking>``、``<reasoning>``、``<thought>``、
  ``<REASONING_SCRATCHPAD>``。

开标签的块边界规则：开标签仅在出现在流的开头、换行符之后（可选地跟随空白），
或当前行只有空白字符时，才被视为推理块的开启标签。
这可以防止在正文中*提及*标签名称（例如 ``"use <think> tags here"``）
被错误地抑制。封闭对（``<think>X</think>``）无论边界如何始终被抑制；
封闭对是有意的、有边界的构造。
"""

from __future__ import annotations

from typing import Tuple

__all__ = ["StreamingThinkScrubber"]


class StreamingThinkScrubber:
    """流式推理/思考块的有状态清洗器。

    状态机：
      - ``_in_block``：在已开启的块内等待关闭标签时为 True。
        块内所有文本都被丢弃。
      - ``_buf``：暂存的不完整标签尾部。在下次 ``feed()`` 调用或
        ``flush()`` 时输出/丢弃。
      - ``_last_emitted_ended_newline``：若最近一次向消费者输出的内容
        以 ``\\n`` 结尾，或尚未输出任何内容（流开始算作边界），则为 True。
        用于判断缓冲区位置 0 处的开标签是否在块边界处。
    """

    _OPEN_TAG_NAMES: Tuple[str, ...] = (
        "think",
        "thinking",
        "reasoning",
        "thought",
        "REASONING_SCRATCHPAD",
    )

    # 将标签字符串实例化为字面量，使热路径执行字符串操作，
    # 而非每次 feed() 时编译正则表达式。
    _OPEN_TAGS: Tuple[str, ...] = tuple(f"<{name}>" for name in _OPEN_TAG_NAMES)
    _CLOSE_TAGS: Tuple[str, ...] = tuple(f"</{name}>" for name in _OPEN_TAG_NAMES)

    # 预先计算最长标签（用于不完整标签暂存的边界）。
    _MAX_TAG_LEN: int = max(len(tag) for tag in _OPEN_TAGS + _CLOSE_TAGS)

    def __init__(self) -> None:
        self._in_block: bool = False
        self._buf: str = ""
        self._last_emitted_ended_newline: bool = True

    def reset(self) -> None:
        """重置所有状态。在每个新 turn 开始时调用。"""
        self._in_block = False
        self._buf = ""
        self._last_emitted_ended_newline = True

    def feed(self, text: str) -> str:
        """输入一个 delta，返回清洗后的可见部分。

        当整个 delta 是推理内容，或因边界处不完整标签待解析而被暂存时，
        可能返回空字符串。
        """
        if not text:
            return ""
        buf = self._buf + text
        self._buf = ""
        out: list[str] = []

        while buf:
            if self._in_block:
                # 寻找最早出现的关闭标签。
                close_idx, close_len = self._find_first_tag(
                    buf, self._CLOSE_TAGS,
                )
                if close_idx == -1:
                    # 尚未找到关闭标签 —— 暂存可能的不完整关闭标签前缀，丢弃其余内容。
                    held = self._max_partial_suffix(buf, self._CLOSE_TAGS)
                    self._buf = buf[-held:] if held else ""
                    return "".join(out)
                # 找到关闭标签：丢弃块内容和标签，继续。
                buf = buf[close_idx + close_len:]
                self._in_block = False
            else:
                # 优先级 1 —— buf 中任意位置的封闭 <tag>X</tag> 对。
                # 封闭对始终是有意的、有边界的构造（即使正文中间包含
                # 开/闭对，几乎可以肯定是模型内联泄漏推理内容），因此无需边界门控。
                pair = self._find_earliest_closed_pair(buf)
                # 优先级 2 —— 在块边界处的未终止开标签。
                # 边界门控，以防提及 '<think>' 的正文被过度删除。
                open_idx, open_len = self._find_open_at_boundary(
                    buf, out,
                )

                # 选取缓冲区中最早出现的匹配项。
                if pair is not None and (
                    open_idx == -1 or pair[0] <= open_idx
                ):
                    start_idx, end_idx = pair
                    preceding = buf[:start_idx]
                    if preceding:
                        preceding = self._strip_orphan_close_tags(preceding)
                        if preceding:
                            out.append(preceding)
                            self._last_emitted_ended_newline = (
                                preceding.endswith("\n")
                            )
                    buf = buf[end_idx:]
                    continue

                if open_idx != -1:
                    # 边界处的未终止开标签 —— 输出前导内容，进入块状态，继续处理剩余部分。
                    preceding = buf[:open_idx]
                    if preceding:
                        preceding = self._strip_orphan_close_tags(preceding)
                        if preceding:
                            out.append(preceding)
                            self._last_emitted_ended_newline = (
                                preceding.endswith("\n")
                            )
                    self._in_block = True
                    buf = buf[open_idx + open_len:]
                    continue

                # buf 中没有可解析的标签结构。暂存尾部可能的不完整标签前缀，
                # 以防跨 delta 分割的标签被遗漏，然后输出其余内容。
                held = self._max_partial_suffix(buf, self._OPEN_TAGS)
                held_close = self._max_partial_suffix(
                    buf, self._CLOSE_TAGS,
                )
                held = max(held, held_close)
                if held:
                    emit_text = buf[:-held]
                    self._buf = buf[-held:]
                else:
                    emit_text = buf
                    self._buf = ""
                if emit_text:
                    emit_text = self._strip_orphan_close_tags(emit_text)
                    if emit_text:
                        out.append(emit_text)
                        self._last_emitted_ended_newline = (
                            emit_text.endswith("\n")
                        )
                return "".join(out)

        return "".join(out)

    def flush(self) -> str:
        """流结束时的刷新。

        若仍处于未终止的块内，暂存内容被丢弃 ——
        泄漏部分推理内容比截断回答更糟糕。
        否则，暂存的不完整标签尾部以原样输出（最终发现它不是真实的标签前缀）。
        """
        if self._in_block:
            self._buf = ""
            self._in_block = False
            return ""
        tail = self._buf
        self._buf = ""
        if not tail:
            return ""
        tail = self._strip_orphan_close_tags(tail)
        if tail:
            self._last_emitted_ended_newline = tail.endswith("\n")
        return tail

    # ── 内部辅助方法 ───────────────────────────────────────────────

    @staticmethod
    def _find_first_tag(
        buf: str, tags: Tuple[str, ...],
    ) -> Tuple[int, int]:
        """返回 *tags* 中最早出现的 (index, tag_length)，若无则返回 (-1, 0)。

        大小写不敏感匹配。
        """
        buf_lower = buf.lower()
        best_idx = -1
        best_len = 0
        for tag in tags:
            idx = buf_lower.find(tag.lower())
            if idx != -1 and (best_idx == -1 or idx < best_idx):
                best_idx = idx
                best_len = len(tag)
        return best_idx, best_len

    def _find_earliest_closed_pair(self, buf: str):
        """返回最早封闭对的 (start_idx, end_idx)，若无则返回 None。

        封闭对为任意变体的 ``<tag>...</tag>``。匹配大小写不敏感且非贪婪
        （开标签之后最近的关闭标签获胜），与 ``_strip_think_blocks``
        情形 1 的 ``<tag>.*?</tag>`` 正则语义一致。
        当两种标签变体都能匹配时，开标签出现更早的优先。
        """
        buf_lower = buf.lower()
        best: "tuple[int, int] | None" = None
        for open_tag, close_tag in zip(self._OPEN_TAGS, self._CLOSE_TAGS):
            open_lower = open_tag.lower()
            close_lower = close_tag.lower()
            open_idx = buf_lower.find(open_lower)
            if open_idx == -1:
                continue
            close_idx = buf_lower.find(
                close_lower, open_idx + len(open_lower),
            )
            if close_idx == -1:
                continue
            end_idx = close_idx + len(close_lower)
            if best is None or open_idx < best[0]:
                best = (open_idx, end_idx)
        return best

    def _find_open_at_boundary(
        self, buf: str, already_emitted: list[str],
    ) -> Tuple[int, int]:
        """返回最早的块边界开标签 (idx, len)。

        若不存在合法边界的开标签，则返回 (-1, 0)。
        """
        buf_lower = buf.lower()
        best_idx = -1
        best_len = 0
        for tag in self._OPEN_TAGS:
            tag_lower = tag.lower()
            search_start = 0
            while True:
                idx = buf_lower.find(tag_lower, search_start)
                if idx == -1:
                    break
                if self._is_block_boundary(buf, idx, already_emitted):
                    if best_idx == -1 or idx < best_idx:
                        best_idx = idx
                        best_len = len(tag)
                    break  # first boundary hit for this tag is enough
                search_start = idx + 1
        return best_idx, best_len

    def _is_block_boundary(
        self, buf: str, idx: int, already_emitted: list[str],
    ) -> bool:
        """当且仅当 *buf* 中位置 *idx* 是块边界时返回 True。

        块边界包括：
          - buf 位置 0，且最近一次输出以换行符结尾（或尚未输出任何内容）
          - 任意位置，其当前行的前导文本（自 buf 中最后一个换行符起）
            仅含空白字符，且若前导 buf 部分没有换行符，
            则最近一次先前输出以换行符结尾
        """
        if idx == 0:
            # 检查本次 feed() 调用中最后输出的块是否以换行符结尾，
            # 否则回退到跨 feed 标志。
            if already_emitted:
                return already_emitted[-1].endswith("\n")
            return self._last_emitted_ended_newline
        preceding = buf[:idx]
        last_nl = preceding.rfind("\n")
        if last_nl == -1:
            # 标签前的 buf 中没有换行符 —— 仅当先前输出以换行符结尾
            # 且此后所有内容均为空白时才算边界。
            if already_emitted:
                prior_newline = already_emitted[-1].endswith("\n")
            else:
                prior_newline = self._last_emitted_ended_newline
            return prior_newline and preceding.strip() == ""
        # 存在换行符 —— 换行符与标签之间的文本必须仅含空白字符。
        return preceding[last_nl + 1:].strip() == ""

    @classmethod
    def _max_partial_suffix(
        cls, buf: str, tags: Tuple[str, ...],
    ) -> int:
        """返回 buf 尾部是任意标签前缀的最长后缀长度。

        仅统计严格短于标签本身的前缀
        （全长后缀就是标签本身，作为匹配处理，而非暂存的不完整部分）。
        大小写不敏感。
        """
        if not buf:
            return 0
        buf_lower = buf.lower()
        max_check = min(len(buf_lower), cls._MAX_TAG_LEN - 1)
        for i in range(max_check, 0, -1):
            suffix = buf_lower[-i:]
            for tag in tags:
                tag_lower = tag.lower()
                if len(tag_lower) > i and tag_lower.startswith(suffix):
                    return i
        return 0

    @classmethod
    def _strip_orphan_close_tags(cls, text: str) -> str:
        """从 *text* 中移除所有关闭标签（孤立关闭标签处理）。

        孤立关闭标签在当前清洗器状态下没有匹配的开标签；
        它始终是噪声，与其后的任意空白一起被删除，以使周围的正文自然流畅。
        """
        if "</" not in text:
            return text
        text_lower = text.lower()
        out: list[str] = []
        i = 0
        while i < len(text):
            matched = False
            if text_lower[i:i + 2] == "</":
                for tag in cls._CLOSE_TAGS:
                    tag_lower = tag.lower()
                    tag_len = len(tag_lower)
                    if text_lower[i:i + tag_len] == tag_lower:
                        # 跳过标签及其后的任意空白，
                        # 与 _strip_think_blocks 情形 3 保持一致。
                        j = i + tag_len
                        while j < len(text) and text[j] in " \t\n\r":
                            j += 1
                        i = j
                        matched = True
                        break
            if not matched:
                out.append(text[i])
                i += 1
        return "".join(out)
