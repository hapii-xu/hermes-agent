"""hermes-agent ACP 适配器的 CLI 入口点。

从 ``~/.hermes/.env`` 加载环境变量，配置日志写入 stderr
（这样 stdout 保留给 ACP JSON-RPC 传输使用），并启动 ACP 代理服务器。

用法::

    python -m acp_adapter.entry
    # 或
    hermes acp
    # 或
    hermes-acp
"""

# 重要：hermes_bootstrap 必须是第一个导入 — 在 Windows 上设置 UTF-8 stdio。
# POSIX 上无操作。完整原因参见 hermes_bootstrap.py。
try:
    import hermes_bootstrap  # noqa: F401
except ModuleNotFoundError:
    # 当 hermes_bootstrap 尚未在 venv 中注册时的优雅降级 —
    # 这会在 ``hermes update`` 的部分完成时发生，即 git-reset 已
    # 拉入新代码但 ``uv pip install -e .`` 尚未完成。缺少 bootstrap
    # 意味着 Windows 上会跳过 UTF-8 stdio 设置；POSIX 不受影响。
    pass
else:
    # 阻止启动目录中的 ``utils/``/``proxy/``/``ui/`` 包遮蔽
    # Hermes 自身的模块 — ``hermes acp`` 可以从任何 cwd 启动，
    # 包括路径上存在同名包的项目。
    hermes_bootstrap.harden_import_path()

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from hermes_constants import get_hermes_home


# 客户端作为周期性活性探测发送的方法。它们不是 ACP schema 的一部分，
# 因此 acp 路由正确地向调用者返回 JSON-RPC -32601 — 但分发请求的
# supervisor task 会通过 ``logging.exception("Background task failed")``
# 抛出 RequestError，导致每个探测间隔都向 stderr 输出 traceback。
# acp-bridge 等客户端已经将 -32601 响应视为"代理存活"，因此 traceback
# 纯属噪音。我们保持协议响应不变，仅在这种特定的良性情况下消除
# stderr 噪音。
_BENIGN_PROBE_METHODS = frozenset({"ping", "health", "healthcheck"})


class _BenignProbeMethodFilter(logging.Filter):
    """抑制由未知活性探测方法（如 ``ping``）引起的 ACP 'Background task failed'
    traceback，同时保留所有其他后台任务错误 — 包括非探测方法的 method_not_found —
    在 stderr 中可见。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.getMessage() != "Background task failed":
            return True
        exc_info = record.exc_info
        if not exc_info:
            return True
        exc = exc_info[1]
        # 惰性导入，以便在可选依赖 ``agent-client-protocol`` 未安装时
        # 本模块仍可正常导入。
        try:
            from acp.exceptions import RequestError
        except ImportError:
            return True
        if not isinstance(exc, RequestError):
            return True
        if getattr(exc, "code", None) != -32601:
            return True
        data = getattr(exc, "data", None)
        method = data.get("method") if isinstance(data, dict) else None
        return method not in _BENIGN_PROBE_METHODS


def _setup_logging() -> None:
    """将所有日志路由到 stderr，使 stdout 保持干净用于 ACP stdio。"""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.addFilter(_BenignProbeMethodFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    # 降低嘈杂日志库的级别
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


def _load_env() -> None:
    """从 HERMES_HOME（默认 ``~/.hermes``）加载 .env。"""
    from hermes_cli.env_loader import load_hermes_dotenv

    hermes_home = get_hermes_home()
    loaded = load_hermes_dotenv(hermes_home=hermes_home)
    if loaded:
        for env_file in loaded:
            logging.getLogger(__name__).info("Loaded env from %s", env_file)
    else:
        logging.getLogger(__name__).info(
            "No .env found at %s, using system env", hermes_home / ".env"
        )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hermes-acp",
        description="Run Hermes Agent as an ACP stdio server.",
    )
    parser.add_argument("--version", action="store_true", help="Print Hermes version and exit")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify ACP dependencies and adapter imports, then exit",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Run interactive Hermes provider/model setup for ACP terminal auth",
    )
    parser.add_argument(
        "--setup-browser",
        action="store_true",
        help="Install agent-browser + Playwright Chromium into ~/.hermes/node/ "
             "for browser tool support. Idempotent.",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        dest="assume_yes",
        help="Accept all prompts (currently used by --setup-browser to skip the "
             "~400 MB Chromium download confirmation).",
    )
    return parser.parse_args(argv)


def _print_version() -> None:
    from hermes_cli import __version__ as hermes_version

    print(hermes_version)


def _run_check() -> None:
    import acp  # noqa: F401
    from acp_adapter.server import HermesACPAgent  # noqa: F401

    print("Hermes ACP check OK")


def _run_setup() -> None:
    from hermes_cli.main import main as hermes_main

    old_argv = sys.argv[:]
    try:
        sys.argv = [old_argv[0] if old_argv else "hermes", "model"]
        hermes_main()
    finally:
        sys.argv = old_argv

    # 提供浏览器工具安装作为后续步骤。终端认证方式是注册中心安装
    # 支持的首次运行体验，因此此时询问是自然的时机。如果 stdin 不是
    # TTY 则静默跳过（无论如何也无法收集回答）。
    if not sys.stdin.isatty():
        return
    try:
        reply = input(
            "\nInstall browser tools? Downloads agent-browser (npm) and "
            "optionally Playwright Chromium (~400 MB). [y/N] "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if reply in {"y", "yes"}:
        _run_setup_browser(assume_yes=False)


def _run_setup_browser(assume_yes: bool = False) -> int:
    """引导安装 agent-browser + Chromium。

    通过 dep_ensure -> install.{sh,ps1} --ensure 路由，与
    ``hermes postinstall`` 和运行时惰性安装器共享代码。

    成功返回 0，失败返回 1。
    """
    from hermes_cli.dep_ensure import ensure_dependency

    try:
        node_ok = ensure_dependency("node", interactive=not assume_yes)
        if not node_ok:
            print("Node.js installation failed — cannot proceed with browser tools.",
                  file=sys.stderr)
            return 1

        browser_ok = ensure_dependency("browser", interactive=not assume_yes)
        if not browser_ok:
            print("Browser tools installation failed.", file=sys.stderr)
            return 1

        return 0
    except OSError as exc:
        print(f"Browser bootstrap failed: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> None:
    """入口点：加载环境、配置日志、运行 ACP 代理。"""
    args = _parse_args(argv)
    if args.version:
        _print_version()
        return
    if args.check:
        _run_check()
        return
    if args.setup:
        _run_setup()
        return
    if args.setup_browser:
        rc = _run_setup_browser(assume_yes=args.assume_yes)
        if rc != 0:
            sys.exit(rc)
        return

    _setup_logging()
    _load_env()

    logger = logging.getLogger(__name__)
    logger.info("Starting hermes-agent ACP adapter")

    # 确保项目根目录在 sys.path 中，使 ``from run_agent import AIAgent`` 可用
    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    import acp
    from .server import HermesACPAgent

    # 从 config.yaml 发现 MCP 工具 — 在 asyncio.run() 之前运行，
    # 这样使用阻塞等待是安全的。（ACP 也会在事件循环内通过
    # asyncio.to_thread 动态注册每个会话的 MCP 服务器；该路径不受
    # 影响。）从 model_tools.py 模块级移到此处，以避免在惰性导入时
    # 冻结 gateway 的事件循环 (#16856)。
    try:
        from tools.mcp_tool import discover_mcp_tools
        discover_mcp_tools()
    except Exception:
        logger.debug("MCP tool discovery failed at ACP startup", exc_info=True)

    agent = HermesACPAgent()
    try:
        asyncio.run(acp.run_agent(agent, use_unstable_protocol=True))
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt)")
    except Exception:
        logger.exception("ACP agent crashed")
        sys.exit(1)


if __name__ == "__main__":
    main()
