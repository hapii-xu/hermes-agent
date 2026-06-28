"""适用于 Hermes 入口点的 Windows UTF-8 引导模块。

Python 在 Windows 上存在两个由来已久的文本编码陷阱：

1. ``sys.stdout`` / ``sys.stderr`` 绑定到控制台代码页
   （美国区域设置安装中为 ``cp1252``），因此 ``print("café")`` 会因
   ``UnicodeEncodeError: 'charmap' codec can't encode character`` 而崩溃。

2. 通过 ``subprocess`` 派生的子进程不知道要使用 UTF-8，
   除非在其环境中设置了 ``PYTHONUTF8`` 和/或 ``PYTHONIOENCODING``——
   因此任何 Python 子进程（execute_code 沙箱、委派子进程、linter 子进程等）
   都会继承相同的 cp1252 默认值，并遭遇同样的 UnicodeEncodeError。

本模块*仅*在 Windows 上修复上述两个问题——POSIX 系统不受影响。
它应当在每个 Hermes 入口点的最顶部被导入
（``hermes``、``hermes-agent``、``hermes-acp``、``python -m gateway.run``、
``batch_runner.py``、``cron/scheduler.py``），在任何可能执行文件 I/O
或向 stdout 打印的导入之前。

本模块在 Windows 上的作用：

  - 设置 ``os.environ["PYTHONUTF8"] = "1"``（PEP 540 UTF-8 模式），
    使我们派生的每个子进程对 ``open()`` 和 stdio 均使用 UTF-8。
  - 设置 ``os.environ["PYTHONIOENCODING"] = "utf-8"`` 作为双重保险——
    某些工具读取此变量而非 ``PYTHONUTF8``，或同时读取两者。
  - 使用 ``reconfigure()`` API（Python 3.7+）将当前进程的
    ``sys.stdout`` / ``sys.stderr`` 重新配置为 UTF-8。
    这样无需重新启动进程即可修复父进程中的 ``print("café")``。

本模块不做的事情：

  - 不会以 ``-X utf8`` 重新启动 Python，因此*当前*进程中的 ``open()``
    调用仍默认使用区域编码。这些调用需要在调用处显式指定
    ``encoding="utf-8"``（lint 规则 ``PLW1514`` / ``PYI058``）。
    Ruff 是执行该检查的正确工具。

本模块在 POSIX 上的作用：

  - 什么都不做。在 99% 的情况下，POSIX 系统默认已使用 UTF-8，
    我们不想干扰用户可能有意配置的 ``LANG``/``LC_*`` 行为。
    如果有人在 Linux 上遇到 C/POSIX 区域设置问题，
    可以自行导出 ``PYTHONUTF8=1``——我们不会覆盖。

幂等性：可安全多次调用。``_bootstrap_once`` 防止重复重新配置。
"""

from __future__ import annotations

import os
import sys

_IS_WINDOWS = sys.platform == "win32"
_bootstrap_applied = False


def apply_windows_utf8_bootstrap() -> bool:
    """如果当前运行于 Windows，则应用 Windows UTF-8 引导。

    若引导已应用（即当前为 Windows 且尚未执行过），返回 True，否则返回 False。
    返回值仅供参考——调用方通常不需要它，但测试可能希望断言该路径已被执行。

    幂等性：首次调用后的后续调用均为空操作。
    """
    global _bootstrap_applied

    if not _IS_WINDOWS:
        return False
    if _bootstrap_applied:
        return False

    # 1. 子进程继承这些环境变量并以 UTF-8 模式运行。
    #    使用 setdefault() 而非直接覆盖，以便用户可在环境中设置
    #    PYTHONUTF8=0（或 PYTHONIOENCODING=其他值）来显式退出。
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    # 2. 将当前进程的 stdio 重新配置为 UTF-8。
    #    这是必要的，因为 os.environ 的更改不会追溯性地重新绑定 sys.stdout
    #    ——后者在解释器启动时已根据控制台代码页完成绑定。
    #    ``reconfigure`` 是 Python 3.7 起 TextIOWrapper 提供的方法。
    #
    #    errors="replace" 意味着如果我们从 stdin *读取*到非 UTF-8 内容
    #    （不常见，但通过旧工具管道输入时可能发生），
    #    会得到 U+FFFD 替换字符而非崩溃。输出为纯 UTF-8。
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # 不是 TextIOWrapper（可能在测试中被重定向为 BytesIO，
            # 或在某些嵌入场景中为非标准流）。
            # 静默跳过——环境变量修复对子进程仍然有效，那才是更大的收益。
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # Already closed, or someone replaced it with something
            # non-reconfigurable.  Non-fatal.
            pass

    # stdin is reconfigured separately with errors="replace" too — input
    # from a legacy pipe shouldn't crash the process.
    stdin = getattr(sys, "stdin", None)
    if stdin is not None:
        reconfigure = getattr(stdin, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    _bootstrap_applied = True
    return True


def harden_import_path(src_root: str | None = None) -> None:
    """Stop a package in the current directory from shadowing Hermes modules.

    Hermes ships top-level modules with common names (``utils``, ``proxy``,
    ``ui``).  Python always seeds ``sys.path`` with the current directory, so
    launching an entry point from a project that has its own ``utils/`` package
    makes ``from utils import ...`` resolve to the *user's* package and crash
    with an ImportError before the gateway can even start.

    The current directory reaches ``sys.path`` two ways, and a complete guard
    has to handle both:

      - As the empty string ``""`` (or ``"."``) that Python inserts at
        ``sys.path[0]`` for ``-m`` / script launches.
      - As its own *absolute* path, when a venv activation or a project that
        adds itself to ``PYTHONPATH`` puts the directory there explicitly.

    We drop the relative forms outright, then force the real Hermes source root
    to the front — relocating it ahead of any absolute cwd entry rather than
    only inserting when absent, so an absolute cwd path can't keep winning.

    ``src_root`` defaults to the directory this module lives in, which is the
    repository root for every shipped entry point, so the guard is
    self-sufficient and does not depend on the spawner exporting an env var.
    """
    root = src_root or os.environ.get("HERMES_PYTHON_SRC_ROOT") or os.path.dirname(
        os.path.abspath(__file__)
    )

    sys.path[:] = [p for p in sys.path if p not in ("", ".")]

    root_abs = os.path.abspath(root)
    sys.path[:] = [p for p in sys.path if os.path.abspath(p) != root_abs]
    sys.path.insert(0, root)


def activate_durable_lazy_target() -> None:
    """Put the durable lazy-install dir on ``sys.path`` if one is configured.

    On immutable Docker images the agent venv is sealed and lazy installs
    are redirected to a writable dir on the data volume
    (``HERMES_LAZY_INSTALL_TARGET``, e.g. ``/opt/data/lazy-packages``).
    Packages installed there on a previous run must be importable on this
    run, so we activate the dir here — at the very first import, before any
    backend module imports its SDK.

    The activation appends to the END of ``sys.path`` so the core venv
    always wins name collisions (see ``tools.lazy_deps`` for the full
    security rationale). Never raises; a missing/empty target is a no-op.
    """
    if not os.environ.get("HERMES_LAZY_INSTALL_TARGET", "").strip():
        return
    try:
        from tools import lazy_deps
        lazy_deps.activate_durable_lazy_target()
    except Exception:
        # Bootstrap must never crash an entry point. If activation fails the
        # backend simply reports itself unavailable, exactly as before.
        pass


# Apply on import — entry points just need ``import hermes_bootstrap``
# (or ``from hermes_bootstrap import apply_windows_utf8_bootstrap``) at
# the very top of their module, before importing anything else.  The
# import side effect does the right thing.
apply_windows_utf8_bootstrap()

# Activate the durable lazy-install target (immutable Docker images) so
# packages installed into the data volume on a previous run are importable
# this run, before any backend module imports its SDK. No-op when unset.
activate_durable_lazy_target()
