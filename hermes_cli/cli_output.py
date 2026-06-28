"""Hermes CLI 模块的共享 CLI 输出辅助函数。

提取了之前在 setup.py、tools_config.py、mcp_config.py 和 memory_setup.py
中重复的 ``print_info/success/warning/error`` 和 ``prompt()`` 函数。
"""

from hermes_cli.colors import Colors, color
from hermes_cli.secret_prompt import masked_secret_prompt


# ─── 打印辅助函数 ────────────────────────────────────────────────────────────


def print_info(text: str) -> None:
    """打印一条暗淡的信息消息。"""
    print(color(f"  {text}", Colors.DIM))


def print_success(text: str) -> None:
    """打印一条带 ✓ 前缀的绿色成功消息。"""
    print(color(f"✓ {text}", Colors.GREEN))


def print_warning(text: str) -> None:
    """打印一条带 ⚠ 前缀的黄色警告消息。"""
    print(color(f"⚠ {text}", Colors.YELLOW))


def print_error(text: str) -> None:
    """打印一条带 ✗ 前缀的红色错误消息。"""
    print(color(f"✗ {text}", Colors.RED))


def print_header(text: str) -> None:
    """打印一条粗体黄色标题。"""
    print(color(f"\n  {text}", Colors.YELLOW))


# ─── 输入提示 ────────────────────────────────────────────────────────────


def prompt(
    question: str,
    default: str | None = None,
    password: bool = False,
) -> str:
    """提示用户输入，支持可选默认值和密码遮掩。

    替代了 setup.py、tools_config.py、mcp_config.py 和 memory_setup.py
    中四个独立的 ``_prompt()`` / ``prompt()`` 实现。

    返回用户的输入（去除首尾空白），如果用户按回车则返回 *default*。
    在 Ctrl-C 或 EOF 时返回空字符串。
    """
    suffix = f" [{default}]" if default else ""
    display = color(f"  {question}{suffix}: ", Colors.YELLOW)

    try:
        if password:
            value = masked_secret_prompt(display)
        else:
            value = input(display)
        value = value.strip()
        return value if value else (default or "")
    except (KeyboardInterrupt, EOFError):
        print()
        return ""


def prompt_yes_no(question: str, default: bool = True) -> bool:
    """提示用户回答是/否。返回布尔值。"""
    hint = "Y/n" if default else "y/N"
    answer = prompt(f"{question} ({hint})")
    if not answer:
        return default
    return answer.lower().startswith("y")
