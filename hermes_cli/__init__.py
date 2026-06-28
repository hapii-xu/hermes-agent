"""
Hermes CLI - Hermes Agent 的统一命令行界面。

提供以下子命令：
- hermes chat          - 交互式聊天（与 ./hermes 相同）
- hermes gateway       - 在前台运行 gateway
- hermes gateway start - 启动 gateway 服务
- hermes gateway stop  - 停止 gateway 服务
- hermes setup         - 交互式设置向导
- hermes status        - 显示所有组件状态
- hermes cron          - 管理定时任务
"""

import os
import sys

__version__ = "0.17.0"
__release_date__ = "2026.6.19"


def _ensure_utf8():
    """强制 stdout/stderr 使用 UTF-8，以防止 UnicodeEncodeError 崩溃。

    某些环境会为标准流选择旧版非 UTF-8 编码：

    - Windows 服务和终端默认使用 cp1252。
    - 使用 latin-1 / C / POSIX locale 的 Linux 主机（常见于精简版 Debian
      和树莓派）会选择 latin-1 或 ASCII。

    CLI 在设置向导、doctor 和状态横幅中会输出制表符字符（┌│├└─）和 ⚕ 符号。
    在非 UTF-8 编码下输出这些字符会引发未处理的 UnicodeEncodeError，导致
    命令在启动前就崩溃——例如在全新树莓派上运行 `hermes setup`。

    此函数在导入时运行，可保护所有 CLI 子命令在任意平台上的运行。
    当 stdout/stderr 编码不是 UTF-8 时，优先使用 TextIOWrapper.reconfigure()
    原地修复现有流对象（缓存的 `sys.stdout` 引用仍然有效），若不支持则
    回退到以 closefd=False 重新打开文件描述符（CPython 推荐的安全方式）。

    若流已是 UTF-8 则为空操作：健康的 UTF-8 系统不会修改流也不会改变环境变量。

    注意：此函数是最早运行的、与平台无关的守卫。
    hermes_cli/stdio.py::configure_windows_stdio() 稍后从入口点运行，
    附加 Windows 专属功能（控制台代码页切换、EDITOR 默认值、PATH 扩展）；
    由于此处已修复流，其流重配置是无害的幂等空操作。
    """
    repaired = False

    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
            if encoding == "utf8":
                continue

            # 优先方式：原地重新配置现有的 TextIOWrapper。
            # 这样可以保持对象标识，使已持有旧 sys.stdout 引用的代码也能受益。
            reconfigure = getattr(stream, "reconfigure", None)
            if callable(reconfigure):
                reconfigure(encoding="utf-8", errors="replace")
                repaired = True
                continue

            # 回退方式：以 UTF-8 重新打开底层文件描述符。用于不支持
            # reconfigure() 的流（如某些被包装或替换的流）。
            # closefd=False 保持原始 fd 不关闭。
            new_stream = open(
                stream.fileno(), "w", encoding="utf-8",
                errors="replace", buffering=1, closefd=False,
            )
            setattr(sys, stream_name, new_stream)
            repaired = True
        except (AttributeError, OSError, ValueError):
            pass

    # 仅在确实检测到非 UTF-8 locale 时才引导子进程使用 UTF-8。
    # 在健康的 UTF-8 主机上，子进程已从 locale 继承 UTF-8，
    # 因此保持环境不变（最小化影响）。
    if repaired:
        os.environ.setdefault("PYTHONUTF8", "1")
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")


_ensure_utf8()
