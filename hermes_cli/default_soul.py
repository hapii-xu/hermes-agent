"""首次运行时播种到 HERMES_HOME 的默认 SOUL.md 模板。"""

DEFAULT_SOUL_MD = (
    "You are Hermes Agent, an intelligent AI assistant created by Nous Research. "
    "You are helpful, knowledgeable, and direct. You assist users with a wide "
    "range of tasks including answering questions, writing and editing code, "
    "analyzing information, creative work, and executing actions via your tools. "
    "You communicate clearly, admit uncertainty when appropriate, and prioritize "
    "being genuinely useful over being verbose unless otherwise directed below. "
    "Be targeted and efficient in your exploration and investigations."
)

# 旧版安装器（install.sh / install.ps1 / docker/SOUL.md）在改用
# DEFAULT_SOUL_MD 之前播种的旧版 SOUL.md 样板。这些模板不包含人设文本——
# 它们纯粹是注释脚手架，因此内容与其中之一匹配的 SOUL.md 显然从未被用户
# 自定义过，可以安全地就地升级为 DEFAULT_SOUL_MD。
#
# 基于归一化内容进行比较（去除首尾空白、统一换行符），以避免 Windows
# 安装器产生的尾部换行或 CRLF 破坏比较。切勿在此添加用户可能故意写入的
# 内容——整个安全保证就在于这些字符串不携带任何用户意图。
_LEGACY_TEMPLATE_SOULS = (
    (
        "# Hermes Agent Persona\n"
        "\n"
        "<!--\n"
        "This file defines the agent's personality and tone.\n"
        "The agent will embody whatever you write here.\n"
        "Edit this to customize how Hermes communicates with you.\n"
        "\n"
        "Examples:\n"
        '  - "You are a warm, playful assistant who uses kaomoji occasionally."\n'
        '  - "You are a concise technical expert. No fluff, just facts."\n'
        '  - "You speak like a friendly coworker who happens to know everything."\n'
        "\n"
        "This file is loaded fresh each message -- no restart needed.\n"
        "Delete the contents (or this file) to use the default personality.\n"
        "-->"
    ),
    # docker/SOUL.md 和 install.sh 的 heredoc 仅在某些历史版本中因
    # "Examples" 块 / 尾部换行而不同；裸脚手架（无 Examples 块）也曾短暂发布。
    (
        "# Hermes Agent Persona\n"
        "\n"
        "<!--\n"
        "This file defines the agent's personality and tone.\n"
        "The agent will embody whatever you write here.\n"
        "Edit this to customize how Hermes communicates with you.\n"
        "\n"
        "This file is loaded fresh each message -- no restart needed.\n"
        "Delete the contents (or this file) to use the default personality.\n"
        "-->"
    ),
)


def _normalize_soul(text: str) -> str:
    """归一化 SOUL.md 内容，用于旧模板比较。"""
    # 统一换行符（Windows 安装器写入的是无 CRLF，但做防御性处理），
    # 去除前导 UTF-8 BOM，并裁剪周围空白。
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").strip()


def is_legacy_template_soul(text: str) -> bool:
    """如果 ``text`` 是旧的空白模板 SOUL.md（无用户人设），则返回 True。

    旧版安装器播种的是仅含注释的脚手架，而非 DEFAULT_SOUL_MD，
    这会遮蔽运行时默认值并导致用户没有角色设定。与已知脚手架之一
    匹配的文件不携带任何用户意图，可以安全地就地升级。任何偏差
    （用户输入了角色设定，甚至是注释外的一个字符）都会使此函数返回 False。
    """
    normalized = _normalize_soul(text)
    return any(normalized == _normalize_soul(t) for t in _LEGACY_TEMPLATE_SOULS)
