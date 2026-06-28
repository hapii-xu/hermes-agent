#!/usr/bin/env python3
"""
文件操作的模糊匹配模块

实现了一条多策略匹配链，用于稳健地查找和替换文本，
能够适应 LLM 生成的代码中常见的空白、缩进与转义差异。

9 策略链（灵感来自 OpenCode），按顺序依次尝试：
1. 精确匹配（exact） - 直接字符串比较
2. 行修剪（line-trimmed） - 去除每行首尾空白
3. 空白归一化（whitespace normalized） - 将多个空格/制表符折叠为单个空格
4. 缩进灵活（indentation flexible） - 完全忽略缩进差异
5. 转义归一化（escape normalized） - 将 \\n 字面量转换为真实换行
6. 边界修剪（trimmed boundary） - 仅修剪首尾行的空白
7. 块锚定（block anchor） - 匹配首尾行，用相似度判定中间部分
8. 上下文感知（context-aware） - 50% 行相似度阈值

多出现位置匹配通过 replace_all 标志处理。

用法：
    from tools.fuzzy_match import fuzzy_find_and_replace

    new_content, match_count, strategy, error = fuzzy_find_and_replace(
        content="def foo():\\n    pass",
        old_string="def foo():",
        new_string="def bar():",
        replace_all=False
    )
"""

import re
from typing import Tuple, Optional, List, Callable
from difflib import SequenceMatcher

UNICODE_MAP = {
    "“": '"', "”": '"',  # 智能双引号
    "‘": "'", "’": "'",  # 智能单引号
    "—": "--", "–": "-", # 破折号（em/en dash）
    "…": "...", " ": " ", # 省略号与不间断空格
}

def _unicode_normalize(text: str) -> str:
    """将 Unicode 字符归一化为它们对应的标准 ASCII 等价物。"""
    for char, repl in UNICODE_MAP.items():
        text = text.replace(char, repl)
    return text


def fuzzy_find_and_replace(content: str, old_string: str, new_string: str,
                            replace_all: bool = False) -> Tuple[str, int, Optional[str], Optional[str]]:
    """
    使用一条逐渐放宽的模糊匹配策略链来查找并替换文本。

    参数：
        content: 要在其中查找的文件内容
        old_string: 要查找的文本
        new_string: 替换后的文本
        replace_all: 若为 True，替换所有出现位置；若为 False，要求匹配唯一

    返回：
        元组 (new_content, match_count, strategy_name, error_message)
        - 成功时：(modified_content, number_of_replacements, strategy_used, None)
        - 失败时：(original_content, 0, None, error_description)
    """
    if not old_string:
        return content, 0, None, "old_string cannot be empty"

    if old_string == new_string:
        return content, 0, None, "old_string and new_string are identical"

    # 按顺序尝试每种匹配策略
    strategies: List[Tuple[str, Callable]] = [
        ("exact", _strategy_exact),
        ("line_trimmed", _strategy_line_trimmed),
        ("whitespace_normalized", _strategy_whitespace_normalized),
        ("indentation_flexible", _strategy_indentation_flexible),
        ("escape_normalized", _strategy_escape_normalized),
        ("trimmed_boundary", _strategy_trimmed_boundary),
        ("unicode_normalized", _strategy_unicode_normalized),
        ("block_anchor", _strategy_block_anchor),
        ("context_aware", _strategy_context_aware),
    ]

    for strategy_name, strategy_fn in strategies:
        matches = strategy_fn(content, old_string)

        if matches:
            # 用该策略找到了匹配
            if len(matches) > 1 and not replace_all:
                return content, 0, None, (
                    f"Found {len(matches)} matches for old_string. "
                    f"Provide more context to make it unique, or use replace_all=True."
                )

            # 转义漂移守卫：当命中的策略不是 `exact` 时，说明我们是通过某种
            # 归一化形式匹配上的。如果 new_string 包含 shell/JSON 风格的转义
            # 序列（\' 或 \"），这些序列会被原样写入文件，但文件中被匹配的
            # 区域里并没有这样的序列，这几乎可以肯定是工具调用序列化漂移 ——
            # 模型输入了一个撇号/引号，传输层加了一个多余的反斜杠。原样写入
            # new_string 会损坏文件。用一个有用的错误拦截，让模型重新读取并
            # 重试，而不是让调用方默默持久化垃圾内容（或反之）。
            if strategy_name != "exact":
                drift_err = _detect_escape_drift(content, matches, old_string, new_string)
                if drift_err:
                    return content, 0, None, drift_err

            # 执行替换。当命中的策略不是 `exact` 时，文件的缩进可能和 LLM
            # 在 old_string/new_string 里发送的不一致 —— 例如 LLM 用了 2 空格
            # 缩进但文件是 4 空格。按缩进差值平移 new_string，使替换结果匹配
            # 文件实际的缩进风格。
            # LLM 经常把 JSON 工具调用参数里的制表符/回车序列化为两字符
            # 序列 ``\t`` 和 ``\r``（反斜杠 + 字母），而不是真实的控制字节。
            # 如果我们原样写入 new_string，文件里会留下字面反斜杠序列，而周围
            # 代码用的是真实制表符。
            #
            # 策略：仅当文件被匹配区域*实际包含*对应的真实控制字符时才反转义。
            # 这镜像了 ``_detect_escape_drift`` 里基于区域的启发式，并保持对
            # 两字符字面串 ``"\t"`` 的合法写入（例如修补包含制表符字符串字面量
            # 的 Python 源码）不变 —— 那些文件在被匹配区域里是反斜杠+t，
            # 而非真实制表符，所以我们不对 new_string 做任何改动。
            #
            # ``\n`` 被有意排除：换行符能正确通过 JSON 序列化，而改写反斜杠-n
            # 破坏源码常量中转义序列的情况，会远远多于它带来的帮助。
            effective_new = _maybe_unescape_new_string(
                new_string, content, matches,
            )
            new_content = _apply_replacements(
                content, matches, effective_new,
                old_string=old_string if strategy_name != "exact" else None,
            )
            return new_content, len(matches), strategy_name, None

    # 没有任何策略找到匹配
    return content, 0, None, "Could not find a match for old_string in the file"


def _detect_escape_drift(content: str, matches: List[Tuple[int, int]],
                         old_string: str, new_string: str) -> Optional[str]:
    """检测 new_string 中的工具调用转义漂移伪影。

    查找同时出现在 old_string 和 new_string 中（即模型把它们当作要保留的
    “上下文”复制粘贴过来）、但文件被匹配区域里不存在的 ``\\'`` 或 ``\\"``
    序列。这种模式表明传输层在撇号或引号周围插入了多余的 shell 风格转义 ——
    原样写入 new_string 会把 ``\\'`` 字面地插入源码。

    检测到漂移时返回错误字符串，否则返回 None。
    """
    # 廉价预检：除非 new_string 确实包含可疑的转义序列，否则直接退出。
    # 这让守卫在所有常见且正确的情况下几乎零开销。
    if "\\'" not in new_string and '\\"' not in new_string:
        return None

    # 聚合文件中被匹配的区域 —— new_string 将要替换的就是这些内容。如果可疑
    # 转义已经存在其中，说明模型确实在保留它们（对某些语言/转义字符串是合法
    # 的）；接受这个补丁。
    matched_regions = "".join(content[start:end] for start, end in matches)

    for suspect in ("\\'", '\\"'):
        if suspect in new_string and suspect in old_string and suspect not in matched_regions:
            plain = suspect[1]  # "'" 或 '"'
            return (
                f"Escape-drift detected: old_string and new_string contain "
                f"the literal sequence {suspect!r} but the matched region of "
                f"the file does not. This is almost always a tool-call "
                f"serialization artifact where an apostrophe or quote got "
                f"prefixed with a spurious backslash. Re-read the file with "
                f"read_file and pass old_string/new_string without "
                f"backslash-escaping {plain!r} characters."
            )
    return None


def _leading_whitespace(line: str) -> str:
    """返回一行的前导空白前缀（空格/制表符）。"""
    i = 0
    while i < len(line) and line[i] in (" ", "\t"):
        i += 1
    return line[:i]


def _first_meaningful_line(text: str) -> Optional[str]:
    """返回 ``text`` 中第一个含有非空白内容的行。

    如果不存在这样的行（文本为空或全是空白），返回 ``None``。
    """
    for line in text.split("\n"):
        if line.strip():
            return line
    return None


def _reindent_replacement(file_region: str, old_string: str, new_string: str) -> str:
    """调整 ``new_string`` 使其缩进与 ``file_region`` 匹配。

    用于非精确模糊匹配之后：LLM 发来的 old_string 和 new_string 的缩进可能
    与文件实际缩进不同（例如工具参数里用 2 空格缩进，磁盘上是 4 空格）。模糊
    策略依然能成功匹配，但原样写入 ``new_string`` 会损坏文件的缩进。

    做法：

    1. 对 ``new_string`` 的每个非空行，计算其相对于 ``old_string`` 最浅非空行
       （LLM 的基础缩进）的*相对*缩进。
    2. 把这个相对缩进锚定到文件实际的基础缩进（file_region 第一个非空行的前导
       空白）。
    3. 把每个非空行重新输出为 ``file_base + (line_indent - llm_base)``。

    空行以及缩进比 LLM 基础缩进更浅的行，直接锚定到文件的基础缩进。

    无操作情形（原样返回 ``new_string``）：
    - file_region 或 old_string 没有有意义的行
    - LLM 基础缩进等于文件基础缩进
    - new_string 为空
    """
    if not new_string:
        return new_string

    old_first = _first_meaningful_line(old_string)
    file_first = _first_meaningful_line(file_region)
    if old_first is None or file_first is None:
        return new_string

    old_indent = _leading_whitespace(old_first)
    file_indent = _leading_whitespace(file_first)

    if old_indent == file_indent:
        return new_string

    # 对 new_string 的每一行重新缩进。策略：把 LLM 的基础缩进前缀替换为文件的
    # 基础缩进前缀，保留 LLM 在其之上额外添加的任何缩进。这与 Roo Code 使用的
    # 做法相同（multi-search-replace.ts:466-500）。它在锚定到文件实际缩进风格
    # 的同时，保留了 LLM 想要的行间*相对*嵌套。
    out_lines: List[str] = []
    for line in new_string.split("\n"):
        if not line.strip():
            # 空行：保持空白原样不动。
            out_lines.append(line)
            continue
        line_indent = _leading_whitespace(line)
        if line_indent.startswith(old_indent):
            # 常见情形：该行带有 LLM 的基础缩进（可能还有额外缩进）。
            # 把基础前缀替换为文件的基础前缀。
            remainder = line[len(old_indent):]
            out_lines.append(file_indent + remainder)
        else:
            # 该行缩进比 LLM 的基础缩进更浅 —— 例如 new_string 开头的去缩进。
            # 锚定到文件的基础缩进。
            out_lines.append(file_indent + line.lstrip(" \t"))
    return "\n".join(out_lines)


def _maybe_unescape_new_string(new_string: str,
                               content: str,
                               matches: List[Tuple[int, int]]) -> str:
    """有条件地反转义 new_string 中的 ``\\t``/``\\r``。

    LLM 经常在 JSON 工具调用参数里发送两字符序列 ``\\t``（反斜杠 + t）和
    ``\\r``（反斜杠 + r），而本意是真实的制表符或回车字节。原样写入字符串
    会用字面反斜杠+字母对损坏制表符缩进的文件。

    反转义仅在*文件被匹配区域*实际包含对应控制字符时才按序列应用 —— 即只有
    当我们要替换的文件区域里含有真实制表符字节时，才把 ``\\t`` 转换为制表符。
    合法包含两字符字面串 ``"\\t"`` 的文件（例如定义了 ``sep = "\\t"`` 的
    Python 源码行）在被匹配区域里是反斜杠+t 而非制表符，所以我们不对
    new_string 做任何改动。

    ``\\n`` 被有意排除：换行符能正确通过 JSON 序列化，而改写反斜杠-n 破坏
    字符串字面量中转义序列的情况，会远远多于它带来的帮助。
    """
    # 廉价预检 —— 除非 new_string 确实包含某个可疑序列，否则直接退出。
    # 让常见情形保持零开销。
    if "\\t" not in new_string and "\\r" not in new_string:
        return new_string

    matched_regions = "".join(content[start:end] for start, end in matches)
    out = new_string
    if "\\t" in out and "\t" in matched_regions:
        out = out.replace("\\t", "\t")
    if "\\r" in out and "\r" in matched_regions:
        out = out.replace("\\r", "\r")
    return out


def _apply_replacements(content: str, matches: List[Tuple[int, int]],
                        new_string: str, old_string: Optional[str] = None) -> str:
    """
    在给定位置应用替换。

    参数：
        content: 原始内容
        matches: 要替换的 (start, end) 位置列表
        new_string: 替换文本
        old_string: 非 None 时表示匹配来自非精确的模糊策略；在替换前会把
            ``new_string`` 重新缩进以匹配文件的实际缩进。

    返回：
        应用替换后的内容
    """
    # 按位置降序排序匹配，从末尾向开头替换
    # 这样可以保持更早匹配的位置不变
    sorted_matches = sorted(matches, key=lambda x: x[0], reverse=True)

    result = content
    for start, end in sorted_matches:
        if old_string is not None:
            file_region = content[start:end]
            adjusted = _reindent_replacement(file_region, old_string, new_string)
        else:
            adjusted = new_string
        result = result[:start] + adjusted + result[end:]

    return result


# =============================================================================
# 匹配策略
# =============================================================================

def _strategy_exact(content: str, pattern: str) -> List[Tuple[int, int]]:
    """策略 1：精确字符串匹配。"""
    matches = []
    start = 0
    while True:
        pos = content.find(pattern, start)
        if pos == -1:
            break
        matches.append((pos, pos + len(pattern)))
        start = pos + 1
    return matches


def _strategy_line_trimmed(content: str, pattern: str) -> List[Tuple[int, int]]:
    """
    策略 2：逐行修剪空白后匹配。

    匹配前去除每行的首尾空白。
    """
    # 通过修剪每行来归一化 pattern 和 content
    pattern_lines = [line.strip() for line in pattern.split('\n')]
    pattern_normalized = '\n'.join(pattern_lines)

    content_lines = content.split('\n')
    content_normalized_lines = [line.strip() for line in content_lines]

    # 建立从归一化位置回到原始位置的映射
    return _find_normalized_matches(
        content, content_lines, content_normalized_lines,
        pattern, pattern_normalized
    )


def _strategy_whitespace_normalized(content: str, pattern: str) -> List[Tuple[int, int]]:
    """
    策略 3：将多个空白折叠为单个空格。
    """
    def normalize(s):
        # 将多个空格/制表符折叠为单个空格，保留换行
        return re.sub(r'[ \t]+', ' ', s)

    pattern_normalized = normalize(pattern)
    content_normalized = normalize(content)

    # 在归一化后的内容里查找，再映射回原始内容
    matches_in_normalized = _strategy_exact(content_normalized, pattern_normalized)

    if not matches_in_normalized:
        return []

    # 将位置映射回原始内容
    return _map_normalized_positions(content, content_normalized, matches_in_normalized)


def _strategy_indentation_flexible(content: str, pattern: str) -> List[Tuple[int, int]]:
    """
    策略 4：完全忽略缩进差异。

    匹配前去除所有行的前导空白。
    """
    content_lines = content.split('\n')
    content_stripped_lines = [line.lstrip() for line in content_lines]
    pattern_lines = [line.lstrip() for line in pattern.split('\n')]

    return _find_normalized_matches(
        content, content_lines, content_stripped_lines,
        pattern, '\n'.join(pattern_lines)
    )


def _strategy_escape_normalized(content: str, pattern: str) -> List[Tuple[int, int]]:
    """
    策略 5：将转义序列转换为真实字符。

    处理 \\n -> 换行、\\t -> 制表符等。
    """
    def unescape(s):
        # 转换常见的转义序列
        return s.replace('\\n', '\n').replace('\\t', '\t').replace('\\r', '\r')

    pattern_unescaped = unescape(pattern)

    if pattern_unescaped == pattern:
        # 没有转义可转换，跳过此策略
        return []

    return _strategy_exact(content, pattern_unescaped)


def _strategy_trimmed_boundary(content: str, pattern: str) -> List[Tuple[int, int]]:
    """
    策略 6：仅修剪首尾行的空白。

    当 pattern 边界处存在空白差异时很有用。
    """
    pattern_lines = pattern.split('\n')
    if not pattern_lines:
        return []

    # 仅修剪首行和尾行
    pattern_lines[0] = pattern_lines[0].strip()
    if len(pattern_lines) > 1:
        pattern_lines[-1] = pattern_lines[-1].strip()

    modified_pattern = '\n'.join(pattern_lines)

    content_lines = content.split('\n')

    # 在 content 中搜索匹配的块
    matches = []
    pattern_line_count = len(pattern_lines)

    for i in range(len(content_lines) - pattern_line_count + 1):
        block_lines = content_lines[i:i + pattern_line_count]

        # 修剪该块的首尾行
        check_lines = block_lines.copy()
        check_lines[0] = check_lines[0].strip()
        if len(check_lines) > 1:
            check_lines[-1] = check_lines[-1].strip()

        if '\n'.join(check_lines) == modified_pattern:
            # 找到匹配 - 计算原始位置
            start_pos, end_pos = _calculate_line_positions(
                content_lines, i, i + pattern_line_count, len(content)
            )
            matches.append((start_pos, end_pos))

    return matches


def _build_orig_to_norm_map(original: str) -> List[int]:
    """构建一个列表，把每个原始字符索引映射到它对应的归一化索引。

    因为 UNICODE_MAP 的替换可能展开字符（例如 em-dash → '--'，省略号 → '...'），
    归一化后的字符串可能比原始字符串更长。这个映射让我们能把归一化字符串里
    的位置转换回原始字符串中对应的位置。

    返回长度为 ``len(original) + 1`` 的列表；第 ``i`` 项是字符 ``i`` 所映射
    到的归一化索引。
    """
    result: List[int] = []
    norm_pos = 0
    for char in original:
        result.append(norm_pos)
        repl = UNICODE_MAP.get(char)
        norm_pos += len(repl) if repl is not None else 1
    result.append(norm_pos)  # 哨兵：最后一个字符之后的位置
    return result


def _map_positions_norm_to_orig(
    orig_to_norm: List[int],
    norm_matches: List[Tuple[int, int]],
) -> List[Tuple[int, int]]:
    """把归一化字符串中的 (start, end) 位置转换为原始位置。"""
    # 反转映射：norm_pos -> 拥有该 norm_pos 的第一个原始位置
    norm_to_orig_start: dict[int, int] = {}
    for orig_pos, norm_pos in enumerate(orig_to_norm[:-1]):
        if norm_pos not in norm_to_orig_start:
            norm_to_orig_start[norm_pos] = orig_pos

    results: List[Tuple[int, int]] = []
    orig_len = len(orig_to_norm) - 1  # 原始字符数

    for norm_start, norm_end in norm_matches:
        if norm_start not in norm_to_orig_start:
            continue
        orig_start = norm_to_orig_start[norm_start]

        # 向前走，直到 orig_to_norm[orig_end] >= norm_end
        orig_end = orig_start
        while orig_end < orig_len and orig_to_norm[orig_end] < norm_end:
            orig_end += 1

        results.append((orig_start, orig_end))

    return results


def _strategy_unicode_normalized(content: str, pattern: str) -> List[Tuple[int, int]]:
    """策略 7：Unicode 归一化。

    把智能引号、破折号、省略号和不间断空格在 *content* 和 *pattern* 两侧都
    归一化为对应的 ASCII 等价物，然后在归一化副本上运行精确匹配和行修剪匹配。

    位置通过 ``_build_orig_to_norm_map`` 映射回*原始*字符串 —— 这是必要的，
    因为某些 UNICODE_MAP 替换会把单个字符展开成多个 ASCII 字符，使朴素的
    位置复制变得不正确。
    """
    # 两侧都归一化。content 或 pattern（或两者）都可能携带 unicode 变体 ——
    # 例如 content 有一个本应匹配 LLM ASCII '--' 的破折号，或反过来。
    # 只有当两者都没变化时才跳过。
    norm_pattern = _unicode_normalize(pattern)
    norm_content = _unicode_normalize(content)
    if norm_content == content and norm_pattern == pattern:
        return []

    norm_matches = _strategy_exact(norm_content, norm_pattern)
    if not norm_matches:
        norm_matches = _strategy_line_trimmed(norm_content, norm_pattern)

    if not norm_matches:
        return []

    orig_to_norm = _build_orig_to_norm_map(content)
    return _map_positions_norm_to_orig(orig_to_norm, norm_matches)


def _strategy_block_anchor(content: str, pattern: str) -> List[Tuple[int, int]]:
    """
    策略 8：以首尾行作为锚点进行匹配。
    采用更宽松的阈值和 unicode 归一化做了调整。
    """
    # 比较时归一化两个字符串，但保留原始内容用于偏移量计算
    norm_pattern = _unicode_normalize(pattern)
    norm_content = _unicode_normalize(content)

    pattern_lines = norm_pattern.split('\n')
    if len(pattern_lines) < 2:
        return []

    first_line = pattern_lines[0].strip()
    last_line = pattern_lines[-1].strip()

    # 匹配逻辑使用归一化后的行
    norm_content_lines = norm_content.split('\n')
    # 但计算起始/结束位置时使用原始行，防止索引偏移
    orig_content_lines = content.split('\n')

    pattern_line_count = len(pattern_lines)

    potential_matches = []
    for i in range(len(norm_content_lines) - pattern_line_count + 1):
        if (norm_content_lines[i].strip() == first_line and
            norm_content_lines[i + pattern_line_count - 1].strip() == last_line):
            potential_matches.append(i)

    matches = []
    candidate_count = len(potential_matches)

    # 阈值逻辑：唯一匹配用 0.50，多个候选时用 0.70。
    # 之前的值（0.10 / 0.30）危险地宽松 —— 10% 的中间段相似度可能匹配到
    # 完全无关的代码块。
    threshold = 0.50 if candidate_count == 1 else 0.70

    for i in potential_matches:
        if pattern_line_count <= 2:
            similarity = 1.0
        else:
            # 比较归一化后的中间段
            content_middle = '\n'.join(norm_content_lines[i+1:i+pattern_line_count-1])
            pattern_middle = '\n'.join(pattern_lines[1:-1])
            similarity = SequenceMatcher(None, content_middle, pattern_middle).ratio()

        if similarity >= threshold:
            # 使用原始行计算位置，确保文件中的字符偏移正确
            start_pos, end_pos = _calculate_line_positions(
                orig_content_lines, i, i + pattern_line_count, len(content)
            )
            matches.append((start_pos, end_pos))

    return matches


def _strategy_context_aware(content: str, pattern: str) -> List[Tuple[int, int]]:
    """
    策略 9：逐行相似度，阈值为 50%。

    找出至少 50% 的行具有高相似度的代码块。
    """
    pattern_lines = pattern.split('\n')
    content_lines = content.split('\n')

    if not pattern_lines:
        return []

    matches = []
    pattern_line_count = len(pattern_lines)

    for i in range(len(content_lines) - pattern_line_count + 1):
        block_lines = content_lines[i:i + pattern_line_count]

        # 计算逐行相似度
        high_similarity_count = 0
        for p_line, c_line in zip(pattern_lines, block_lines):
            sim = SequenceMatcher(None, p_line.strip(), c_line.strip()).ratio()
            if sim >= 0.80:
                high_similarity_count += 1

        # 需要至少 50% 的行具有高相似度
        if high_similarity_count >= len(pattern_lines) * 0.5:
            start_pos, end_pos = _calculate_line_positions(
                content_lines, i, i + pattern_line_count, len(content)
            )
            matches.append((start_pos, end_pos))

    return matches


# =============================================================================
# 辅助函数
# =============================================================================

def _calculate_line_positions(content_lines: List[str], start_line: int,
                              end_line: int, content_length: int) -> Tuple[int, int]:
    """根据行索引计算起始和结束的字符位置。

    参数：
        content_lines: 行列表（不含换行符）
        start_line: 起始行索引（从 0 开始）
        end_line: 结束行索引（不包含，从 0 开始）
        content_length: 原始内容字符串的总长度

    返回：
        原始内容中的 (start_pos, end_pos) 元组
    """
    start_pos = sum(len(line) + 1 for line in content_lines[:start_line])
    end_pos = sum(len(line) + 1 for line in content_lines[:end_line]) - 1
    end_pos = min(content_length, end_pos)
    return start_pos, end_pos


def _find_normalized_matches(content: str, content_lines: List[str],
                              content_normalized_lines: List[str],
                              pattern: str, pattern_normalized: str) -> List[Tuple[int, int]]:
    """
    在归一化后的内容里查找匹配，并映射回原始位置。

    参数：
        content: 原始内容字符串
        content_lines: 按行切分的原始内容
        content_normalized_lines: 归一化后的内容行
        pattern: 原始 pattern
        pattern_normalized: 归一化后的 pattern

    返回：
        原始内容中的 (start, end) 位置列表
    """
    pattern_norm_lines = pattern_normalized.split('\n')
    num_pattern_lines = len(pattern_norm_lines)

    matches = []

    for i in range(len(content_normalized_lines) - num_pattern_lines + 1):
        # 检查该块是否匹配
        block = '\n'.join(content_normalized_lines[i:i + num_pattern_lines])

        if block == pattern_normalized:
            # 找到匹配 - 计算原始位置
            start_pos, end_pos = _calculate_line_positions(
                content_lines, i, i + num_pattern_lines, len(content)
            )
            matches.append((start_pos, end_pos))

    return matches


def _map_normalized_positions(original: str, normalized: str,
                               normalized_matches: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """
    把归一化字符串中的位置映射回原始字符串。

    这是一个尽力而为的映射，适用于空白归一化的情形。
    """
    if not normalized_matches:
        return []

    # 构建从归一化到原始的字符映射
    orig_to_norm = []  # orig_to_norm[i] = 在归一化字符串中的位置

    orig_idx = 0
    norm_idx = 0

    while orig_idx < len(original) and norm_idx < len(normalized):
        if original[orig_idx] == normalized[norm_idx]:
            orig_to_norm.append(norm_idx)
            orig_idx += 1
            norm_idx += 1
        elif original[orig_idx] in ' \t' and normalized[norm_idx] == ' ':
            # 原始里有空格/制表符，归一化后被折叠成单个空格
            orig_to_norm.append(norm_idx)
            orig_idx += 1
            # 暂不前进 norm_idx —— 等所有空白都被消耗完
            if orig_idx < len(original) and original[orig_idx] not in ' \t':
                norm_idx += 1
        elif original[orig_idx] in ' \t':
            # 原始里有多余的空白
            orig_to_norm.append(norm_idx)
            orig_idx += 1
        else:
            # 不匹配 —— 在我们的归一化下不应发生
            orig_to_norm.append(norm_idx)
            orig_idx += 1

    # 填充剩余部分
    while orig_idx < len(original):
        orig_to_norm.append(len(normalized))
        orig_idx += 1

    # 反向映射：为每个归一化位置找到对应的原始范围
    norm_to_orig_start = {}
    norm_to_orig_end = {}

    for orig_pos, norm_pos in enumerate(orig_to_norm):
        if norm_pos not in norm_to_orig_start:
            norm_to_orig_start[norm_pos] = orig_pos
        norm_to_orig_end[norm_pos] = orig_pos

    # 映射匹配
    original_matches = []
    for norm_start, norm_end in normalized_matches:
        # 找原始起始位置
        if norm_start in norm_to_orig_start:
            orig_start = norm_to_orig_start[norm_start]
        else:
            # 找最近的
            orig_start = min(i for i, n in enumerate(orig_to_norm) if n >= norm_start)

        # 找原始结束位置
        if norm_end - 1 in norm_to_orig_end:
            orig_end = norm_to_orig_end[norm_end - 1] + 1
        else:
            orig_end = orig_start + (norm_end - norm_start)

        # 扩展以包含被归一化掉的后随空白
        while orig_end < len(original) and original[orig_end] in ' \t':
            orig_end += 1

        original_matches.append((orig_start, min(orig_end, len(original))))

    return original_matches


def find_closest_lines(old_string: str, content: str, context_lines: int = 2, max_results: int = 3) -> str:
    """在 content 中找出与 old_string 最相似的行，用于“你是不是想找？”反馈。

    返回一个格式化字符串，展示最匹配的行及其上下文；如果没找到有用的匹配则
    返回空字符串。
    """
    if not old_string or not content:
        return ""

    old_lines = old_string.splitlines()
    content_lines = content.splitlines()

    if not old_lines or not content_lines:
        return ""

    # 用 old_string 的第一行作为搜索锚点
    anchor = old_lines[0].strip()
    if not anchor:
        # 首行为空则尝试第二行
        candidates = [l.strip() for l in old_lines if l.strip()]
        if not candidates:
            return ""
        anchor = candidates[0]

    # 按与锚点的相似度给 content 中的每一行打分
    scored = []
    for i, line in enumerate(content_lines):
        stripped = line.strip()
        if not stripped:
            continue
        ratio = SequenceMatcher(None, anchor, stripped).ratio()
        if ratio > 0.3:
            scored.append((ratio, i))

    if not scored:
        return ""

    # 取最靠前的若干个匹配
    scored.sort(key=lambda x: -x[0])
    top = scored[:max_results]

    parts = []
    seen_ranges = set()
    for _, line_idx in top:
        start = max(0, line_idx - context_lines)
        end = min(len(content_lines), line_idx + len(old_lines) + context_lines)
        key = (start, end)
        if key in seen_ranges:
            continue
        seen_ranges.add(key)
        snippet = "\n".join(
            f"{start + j + 1:4d}| {content_lines[start + j]}"
            for j in range(end - start)
        )
        parts.append(snippet)

    if not parts:
        return ""

    return "\n---\n".join(parts)


def format_no_match_hint(error: Optional[str], match_count: int,
                         old_string: str, content: str) -> str:
    """为单纯的“未匹配”错误返回一段 '\\n\\n你是不是想找...' 片段。

    加了门控，使该提示仅对真正的“old_string 未找到”失败触发。歧义匹配
    （“Found N matches”）、转义漂移和相同字符串错误都满足 ``match_count == 0``，
    但“你是不是想找？”片段会误导 —— 它们失败的原因与之无关。

    当没有有用的内容可附加时返回空字符串。
    """
    if match_count != 0:
        return ""
    if not error or not error.startswith("Could not find"):
        return ""
    hint = find_closest_lines(old_string, content)
    if not hint:
        return ""
    return "\n\nDid you mean one of these sections?\n" + hint
