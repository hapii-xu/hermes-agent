"""检测 gateway 在执行热 ``git pull`` 之后是否在运行过期代码。

gateway 是一个长期存活的单一进程；它的 ``sys.modules`` 在启动时就被冻结。
如果检出目录在其运行期间被更新（手动 ``git pull``，或 ``hermes update`` 的
优雅重启触发前的窗口期），某个新代码路径上首次发生的惰性导入可能会把刚拉取的
消费者模块解析到一个过期的缓存依赖上 -> ImportError（确切失败场景见
``tests/test_stale_utils_module_import.py``）。

我们在 gateway 启动时对检出 revision 做快照，并按需对比，这样高风险调用方
（例如 ``/model`` 切换）就可以用一条清晰的“请重启 gateway”消息拒绝执行，
而不是因为晦涩的导入错误而崩溃。

如果无法读取 revision（非 git 安装、IO 错误），启动快照会保持为 ``None``，
代码漂移检测也不会生效——它绝不会产生误报。
"""

from __future__ import annotations

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_boot_fingerprint: str | None = None


def _fingerprint() -> str | None:
    """当前的检出指纹，复用 CLI 的 git-rev 读取器。

    ``hermes_cli.main`` 在 gateway 进程中总是已经被导入（它就是入口点），
    因此这个导入是零成本的，也避免重复实现支持 worktree 的 ref 解析逻辑。
    """
    try:
        from hermes_cli.main import _read_git_revision_fingerprint

        return _read_git_revision_fingerprint(_PROJECT_ROOT)
    except Exception:
        return None


def record_boot_fingerprint() -> None:
    """在 gateway 启动时对检出 revision 做快照（幂等）。"""
    global _boot_fingerprint
    if _boot_fingerprint is None:
        _boot_fingerprint = _fingerprint()


def _short(fingerprint: str) -> str:
    """把 ``git:<ref>:<sha>`` 指纹渲染成一个紧凑的标签。"""
    sha = fingerprint.rsplit(":", 1)[-1]
    if sha and sha != "unresolved" and len(sha) > 10:
        return sha[:10]
    return sha or fingerprint


def detect_code_skew() -> tuple[str, str] | None:
    """如果检出相对启动时发生了漂移，返回 ``(boot_rev, disk_rev)`` 短标签，
    否则返回 ``None``。"""
    if _boot_fingerprint is None:
        return None
    current = _fingerprint()
    if current is None or current == _boot_fingerprint:
        return None
    return _short(_boot_fingerprint), _short(current)
