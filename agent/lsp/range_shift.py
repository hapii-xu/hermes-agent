"""用于跨编辑 LSP 增量过滤的差异感知行移映射。

当编辑在文件中间删除或插入行时，编辑点以下的所有诊断信息
都会移到新行号。LSPService 的增量过滤器从编辑后诊断信息中
减去基于 ``(severity, code, source, message, range)`` 键的编辑前基线——
如果没有调整，这些移位但内容相同的诊断看起来是全新的，
agent 会被噪音淹没。

此处使用的修复方案与 git blame 和 unified diff 使用的技巧相同：
构建从编辑前行号到编辑后行号的分段线性映射，然后在进行集合差运算前
将该映射应用于基线诊断信息。编辑前行号位于被删除区域的诊断返回
``None`` 并从基线中删除（它们确实已不再适用）。

与完全从键中删除 range（之前的修复方案）相比的权衡：
保留了"相同错误在不同行出现的新实例"信号——如果模型在不同位置
引入了相同错误类的第二个实例，该实例会作为新诊断显示，
而不会被仅基于内容的去重所吞没。

映射从 ``difflib.SequenceMatcher.get_opcodes()`` 派生，
作为单一可调用对象暴露，使调用者无需自行处理差异区域。
"""
from __future__ import annotations

import difflib
from typing import Any, Callable, Dict, List, Optional


def build_line_shift(pre_text: str, post_text: str) -> Callable[[int], Optional[int]]:
    """构建一个将编辑前行号映射到编辑后行号的函数。

    行号从 0 开始，与 LSP 线路格式一致
    （``range.start.line`` 从 0 开始）。

    返回的可调用对象接受编辑前从 0 开始的行号，
    返回对应的编辑后从 0 开始的行号，
    若该行被编辑删除（不存在编辑后对应行）则返回 ``None``。

    代价：预先执行一次 ``SequenceMatcher.get_opcodes()``；
    返回的闭包每次调用为 O(log n)（对 opcode 区域进行二分搜索）。
    对于每次写入/patch 调用一次、对每条基线诊断应用而言足够廉价。
    """
    pre_lines = pre_text.splitlines() if pre_text else []
    post_lines = post_text.splitlines() if post_text else []

    # 简单情形：内容相同或无内容 —— 恒等映射。
    if pre_lines == post_lines:
        return lambda line: line

    # SequenceMatcher.get_opcodes() 返回
    # (tag, i1, i2, j1, j2) 列表，tag 为 'equal'、'replace'、'delete'
    # 或 'insert'。i1:i2 是 pre 中的范围，j1:j2 是 post 中的范围。
    # 构建 (i1, i2, j1, j2, tag) 元组列表，按 i 进行二分搜索查找。
    sm = difflib.SequenceMatcher(a=pre_lines, b=post_lines, autojunk=False)
    opcodes = sm.get_opcodes()

    def shift(line: int) -> Optional[int]:
        # 找到满足 i1 <= line < i2 的 opcode 区域。
        # 线性扫描即可 —— 典型的 opcode 数量很少（典型 patch 工具编辑只有个位数）。
        for tag, i1, i2, j1, j2 in opcodes:
            if i1 <= line < i2:
                if tag == "equal":
                    # 编辑前行 N → 编辑后行 (N - i1 + j1)。
                    return line - i1 + j1
                if tag == "delete":
                    # 编辑前行处于已删除区域 —— 不存在编辑后对应行。
                    return None
                if tag == "replace":
                    # replace == delete + insert；编辑前行在任何有意义的意义上
                    # 都没有编辑后对应行。丢弃。
                    return None
                # 'insert' 的 i1 == i2，因此 line < i2 不会被命中。
            if line < i1:
                # 已超过相关区域 —— 在前面的迭代中已处理。
                break
        # 已超过最后一个 opcode 区域（line >= len(pre_lines)）。
        # 锚定到 post 末尾。
        return max(0, len(post_lines) - 1) if post_lines else None

    return shift


def shift_diagnostic_range(diag: Dict[str, Any],
                           shift: Callable[[int], Optional[int]]) -> Optional[Dict[str, Any]]:
    """Return a copy of ``diag`` with its line range remapped through ``shift``.

    Returns ``None`` if the diagnostic's start line maps to ``None``
    (the line was deleted by the edit) — caller drops it from the
    baseline since the diagnostic no longer applies.

    Both ``start.line`` and ``end.line`` are remapped independently;
    when only the end maps to ``None`` (rare, multi-line diagnostic
    straddling the edit boundary) we collapse to a single-line range
    at the shifted start to keep the diagnostic in the baseline.

    The original ``diag`` is not mutated.
    """
    rng = diag.get("range") or {}
    start = rng.get("start") or {}
    end = rng.get("end") or {}

    pre_start_line = int(start.get("line", 0))
    pre_end_line = int(end.get("line", pre_start_line))

    new_start_line = shift(pre_start_line)
    if new_start_line is None:
        return None

    new_end_line = shift(pre_end_line)
    if new_end_line is None:
        # Diagnostic straddled the deletion — collapse to start.
        new_end_line = new_start_line

    shifted = dict(diag)
    shifted["range"] = {
        "start": {
            "line": new_start_line,
            "character": int(start.get("character", 0)),
        },
        "end": {
            "line": new_end_line,
            "character": int(end.get("character", 0)),
        },
    }
    return shifted


def shift_baseline(baseline: List[Dict[str, Any]],
                   shift: Callable[[int], Optional[int]]) -> List[Dict[str, Any]]:
    """Apply ``shift`` to every diagnostic in ``baseline``, dropping deleted entries."""
    out: List[Dict[str, Any]] = []
    for d in baseline:
        if not isinstance(d, dict):
            continue
        shifted = shift_diagnostic_range(d, shift)
        if shifted is not None:
            out.append(shifted)
    return out


__all__ = ["build_line_shift", "shift_diagnostic_range", "shift_baseline"]
