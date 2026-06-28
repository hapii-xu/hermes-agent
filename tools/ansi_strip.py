"""从子进程输出中剥离 ANSI 转义序列。

由 terminal_tool、code_execution_tool 和 process_registry 使用，
用于在将命令输出返回给模型之前进行清理。这可以防止 ANSI 代码
进入模型的上下文——这是模型将转义序列复制到文件写入中的根本原因。

覆盖完整的 ECMA-48 规范：CSI（包括私有模式 ``?`` 前缀、
冒号分隔的参数、中间字节）、OSC（BEL 和 ST 终止符）、
DCS/SOS/PM/APC 字符串序列、nF 多字节转义、Fp/Fe/Fs
单字节转义，以及 8 位 C1 控制字符。
"""

import re

_ANSI_ESCAPE_RE = re.compile(
    r"\x1b"
    r"(?:"
        r"\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"     # CSI 序列
        r"|\][\s\S]*?(?:\x07|\x1b\\)"                  # OSC（BEL 或 ST 终止符）
        r"|[PX^_][\s\S]*?(?:\x1b\\)"                   # DCS/SOS/PM/APC 字符串
        r"|[\x20-\x2f]+[\x30-\x7e]"                    # nF 转义序列
        r"|[\x30-\x7e]"                                 # Fp/Fe/Fs 单字节
    r")"
    r"|\x9b[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"       # 8 位 CSI
    r"|\x9d[\s\S]*?(?:\x07|\x9c)"                       # 8 位 OSC
    r"|[\x80-\x9f]",                                    # 其他 8 位 C1 控制字符
    re.DOTALL,
)

# 快速路径检查——当不存在类似转义的字节时，跳过完整的 regex 匹配。
_HAS_ESCAPE = re.compile(r"[\x1b\x80-\x9f]")


def strip_ansi(text: str) -> str:
    """从文本中移除 ANSI 转义序列。

    当不存在 ESC 或 C1 字节时，原样返回输入（快速路径）。
    可安全地对任意字符串调用——干净的文本可以直接通过，
    开销可忽略不计。
    """
    if not text or not _HAS_ESCAPE.search(text):
        return text
    return _ANSI_ESCAPE_RE.sub("", text)
