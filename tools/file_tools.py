#!/usr/bin/env python3
"""文件工具模块 —— LLM agent 的文件操作工具。"""

import errno
import json
import logging
import os
import threading
from pathlib import Path

from agent.file_safety import get_read_block_error
from tools.binary_extensions import has_binary_extension
from tools.file_operations import (
    ShellFileOperations,
    normalize_read_pagination,
    normalize_search_pagination,
)
from tools import file_state
from agent.redact import redact_sensitive_text

logger = logging.getLogger(__name__)


_EXPECTED_WRITE_ERRNOS = {errno.EACCES, errno.EPERM, errno.EROFS}


def _expand_tilde(path: str) -> str:
    """在可用时使用当前 profile 的 home 来展开 ``~``。

    进程内文件工具共享 gateway 进程的 HOME，它可能与交互式 CLI 会话所用的
    profile 专属 HOME 不同。这里镜像 ``hermes_constants.get_subprocess_home()``
    的行为，使 ``~`` 无论在交互式运行还是在 gateway 驱动的 cron 任务
    （#48552）中都能一致解析。
    """
    if not path or "~" not in path:
        return path
    try:
        from hermes_constants import get_subprocess_home

        home = get_subprocess_home()
    except Exception:
        home = None
    if home and (path == "~" or path.startswith("~/")):
        return home if path == "~" else os.path.join(home, path[2:])
    return os.path.expanduser(path)


# ---------------------------------------------------------------------------
# 读取大小守卫：限制返回给模型的字符数。
# 我们与具体模型无关，无法统计 token；字符数是一个安全的代理指标。
# 100K 字符 ≈ 25–35K token（跨常见分词器）。单次读取超过这个大小的
# 文件是上下文窗口的隐患——模型应使用 offset+limit 读取相关区段。
#
# 可通过 config.yaml 配置：file_read_max_chars: 200000
# ---------------------------------------------------------------------------
_DEFAULT_MAX_READ_CHARS = 100_000
_max_read_chars_cached: int | None = None


def _get_max_read_chars() -> int:
    """返回配置的单次文件读取最大字符数。

    首次调用时从 config.yaml 读取 ``file_read_max_chars``，并在进程
    生命周期内缓存结果。若配置缺失或非法，则回退到内置默认值。
    """
    global _max_read_chars_cached
    if _max_read_chars_cached is not None:
        return _max_read_chars_cached
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        val = cfg.get("file_read_max_chars")
        if isinstance(val, (int, float)) and val > 0:
            _max_read_chars_cached = int(val)
            return _max_read_chars_cached
    except Exception:
        pass
    _max_read_chars_cached = _DEFAULT_MAX_READ_CHARS
    return _max_read_chars_cached

# 如果文件总大小超过此值且调用者未指定窄范围（limit <= 200），
# 我们就附带一条提示，鼓励定向读取。
_LARGE_FILE_HINT_BYTES = 512_000  # 512 KB

# ---------------------------------------------------------------------------
# 设备路径黑名单 —— 读取这些路径会挂起进程（无限输出或阻塞在输入上）。
# 仅按路径检查（不做 I/O）。
# ---------------------------------------------------------------------------
_BLOCKED_DEVICE_PATHS = frozenset({
    # 无限输出 —— 永远到不了 EOF
    "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",
    # 阻塞等待输入
    "/dev/stdin", "/dev/tty", "/dev/console",
    # 读取没有意义
    "/dev/stdout", "/dev/stderr",
    # fd 别名
    "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",
})


def _resolve_path(filepath: str, task_id: str = "default") -> Path:
    """把路径相对于 TERMINAL_CWD（worktree 基目录）解析，
    而不是相对于主仓库根目录。
    """
    return _resolve_path_for_task(filepath, task_id)


# 表示 "未配置" 的 ``TERMINAL_CWD`` 哨兵值，而不是一个字面的、可据此解析的
# 目录。陈旧的配置 / .env 常常把字面 "." 留在这里；"auto"/"cwd" 是安装向导
# 的占位符。把其中任何一个当作真实的相对基，会静默地把编辑锚定到 agent
# 进程的 cwd（例如 worktree 会话激活时的主仓库），把写入导向错误的检出。
# gateway 在导入时（gateway/run.py）会对同一集合做清理；文件/终端工具层
# 也必须照做，以便 CLI 会话获得同等保护。参见
# references/worktree-cwd-discipline.md。
_TERMINAL_CWD_SENTINELS = frozenset({"", ".", "./", "auto", "cwd"})


def _sentinel_free_abs_cwd(raw: str | None) -> str | None:
    """把 cwd 候选值归一化为一个绝对、无哨兵的锚点。

    只有当 *raw* 非空、不是哨兵值（见 ``_TERMINAL_CWD_SENTINELS``）且为
    绝对路径时，才返回展开后的路径。在不知道相对哪个 cwd 的前提下，相对
    锚点毫无意义——正是这种歧义会导致 worktree 编辑被错误路由——因此
    相对/哨兵/空值都返回 ``None``。
    """
    raw = str(raw or "").strip()
    if raw.lower() in _TERMINAL_CWD_SENTINELS:
        return None
    expanded = _expand_tilde(raw)
    if not os.path.isabs(expanded):
        return None
    return expanded


def _configured_terminal_cwd() -> str | None:
    """仅当 ``$TERMINAL_CWD`` 命名一个真实的目录锚点时才返回它。

    哨兵值（见 ``_TERMINAL_CWD_SENTINELS``）和相对路径被拒绝——在不知道
    相对哪个 cwd 的前提下，相对锚点毫无意义，正是这种歧义会导致 worktree
    编辑被错误路由。只有绝对且无哨兵的值才会被采纳。
    """
    return _sentinel_free_abs_cwd(os.environ.get("TERMINAL_CWD"))


def _registered_task_cwd_override(task_id: str = "default") -> str | None:
    """在可用时，为原始 task id 返回已注册的 cwd 覆盖值。

    ``terminal_tool`` 有意把仅含 CWD 的任务覆盖合并到共享的 ``"default"``
    环境，使 TUI/dashboard/ACP 会话不会仅因工作区不同就各自拉起隔离的
    沙箱。cwd 值本身仍按原始会话/任务 id 作为键，因此文件工具必须先读取
    这个原始覆盖值，再回退到合并后的容器键。
    """
    try:
        from tools.terminal_tool import resolve_task_overrides

        overrides = resolve_task_overrides(task_id)
    except Exception:
        return None

    return _sentinel_free_abs_cwd(overrides.get("cwd"))


def _get_live_tracking_cwd(task_id: str = "default") -> str | None:
    """在可用时，返回该任务的实时终端 cwd 以便记账。"""
    try:
        from tools.terminal_tool import _resolve_container_task_id
        container_key = _resolve_container_task_id(task_id)
    except Exception:
        container_key = task_id

    with _file_ops_lock:
        cached = _file_ops_cache.get(container_key) or _file_ops_cache.get(task_id)
    if cached is not None:
        live_cwd = getattr(getattr(cached, "env", None), "cwd", None) or getattr(
            cached, "cwd", None
        )
        if live_cwd:
            return live_cwd

    try:
        from tools.terminal_tool import _active_environments, _env_lock

        with _env_lock:
            env = _active_environments.get(container_key) or _active_environments.get(task_id)
            live_cwd = getattr(env, "cwd", None) if env is not None else None
        if live_cwd:
            return live_cwd
    except Exception:
        pass

    return None


def _authoritative_workspace_root(task_id: str = "default") -> str | None:
    """尽力而为地返回用于偏差检查的绝对工作区根目录。

    优先使用实时终端 cwd（agent 实际工作的目录）。当还没有任何终端命令
    运行——即实时注册表为空——时，回退到已注册的任务/会话 cwd 覆盖
    （TUI/Desktop/ACP 会话在任何工具运行前就会注册一个按原始键索引的
    cwd），再回退到无哨兵的绝对 ``$TERMINAL_CWD``。正是这一点让一个
    worktree 或 Desktop 会话从第一次 ``write_file``/``patch`` 开始、在
    任何 ``cd`` 填充实时 cwd 之前，就能针对自己的工作区发出警告（并解析进去）。

    仅当确实不存在可靠的锚点时才返回 ``None``，此时调用方回退到进程 cwd。
    """
    live = _get_live_tracking_cwd(task_id)
    if live:
        return live
    registered = _registered_task_cwd_override(task_id)
    if registered:
        return registered
    return _configured_terminal_cwd()


def _resolve_base_dir(task_id: str = "default") -> Path:
    """返回用于解析相对路径的【绝对】基目录。

    解析顺序：
      1. 该任务的实时终端 cwd（agent 实际工作的目录——例如一个 git
         worktree）。已知时为权威值。
      2. 已注册的任务/会话 cwd 覆盖（TUI/Desktop/ACP 会话在任何终端命令
         运行前就会注册一个按原始键索引的工作区 cwd）。
      3. 无哨兵、绝对的 ``$TERMINAL_CWD``（由 ``cli.py``/``main.py`` 为
         ``-w`` 会话设置的 worktree 路径）。即使任何终端命令都尚未填充
         实时 cwd 注册表时也会被使用。
      4. 进程 cwd。

    返回的基目录【始终】是绝对路径。这是防止 worktree-cwd 偏差 bug 的核心
    不变式：相对或哨兵式的 ``TERMINAL_CWD``（常常是陈旧配置里字面的 ``"."``）
    作为解析锚点毫无意义——交给 ``Path.resolve()`` 的话，它会静默地相对
    agent 进程 cwd 当前的值来解析（例如终端在 worktree 中时是主仓库），
    把编辑导向错误的检出。因此我们干脆拒绝哨兵/相对的 ``TERMINAL_CWD``
    值（而不是把它们锚定到进程 cwd），并以确定性的方式仅在最后才回退到
    进程 cwd。
    """
    root = _authoritative_workspace_root(task_id)
    if root:
        base = Path(_expand_tilde(root))
    else:
        base = Path(os.getcwd())
    if not base.is_absolute():
        # 最后手段的锚定：实时 cwd 本应已是绝对的，但如果某个终端后端
        # 报告了相对 cwd，就在这里把它一次性锚定到进程 cwd，使结果不再
        # 依赖于 resolve() 时的 cwd。
        base = Path(os.getcwd()) / base
    return base.resolve()


def _resolve_path_for_task(filepath: str, task_id: str = "default") -> Path:
    """把 *filepath* 相对该任务的绝对基目录进行解析。

    基目录如何选取见 :func:`_resolve_base_dir`。绝对路径的输入会被解析但
    不锚定（resolved-but-unanchored）地返回。
    """
    p = Path(_expand_tilde(filepath))
    if p.is_absolute():
        return p.resolve()
    return (_resolve_base_dir(task_id) / p).resolve()


def _path_resolution_warning(filepath: str, resolved: Path, task_id: str = "default") -> str | None:
    """当相对路径解析到任务工作区根目录【之外】时给出警告。

    在问题即将产生的那一刻暴露 worktree-cwd 偏差：如果 agent 传入一个
    相对路径，但它解析到一个非工作区根目录的目录下（即编辑即将落到一个
    与 agent 正在工作的检出不同的检出中），返回一条点名绝对目标的消息。
    当路径为绝对路径、基目录未知、或解析后的路径正确地位于工作区根目录
    下时，返回 ``None``。

    工作区根目录在已知时是实时终端 cwd，否则是已注册的任务/会话 cwd
    覆盖，再否则是无哨兵的绝对 ``$TERMINAL_CWD``——因此一个终端注册表
    仍为空（尚未运行 ``cd``）的 worktree 或 Desktop 会话，会在第一次写入
    时就收到警告。
    """
    try:
        if Path(_expand_tilde(filepath)).is_absolute():
            return None
        workspace_root = _authoritative_workspace_root(task_id)
        if not workspace_root:
            return None  # 没有可比较的权威工作区根目录。
        root = Path(_expand_tilde(workspace_root)).resolve()
        # `resolved` 是否在 `root` 之内？
        try:
            resolved.relative_to(root)
            return None  # 在工作区内——符合预期。
        except ValueError:
            return (
                f"Relative path {filepath!r} resolved to {str(resolved)!r}, which is "
                f"OUTSIDE the active workspace ({str(root)!r}). The edit will land in "
                f"a different directory than the terminal's cwd. If this is not "
                f"intended (e.g. a git-worktree session writing into the main "
                f"checkout), pass an absolute path under the workspace instead."
            )
    except Exception:
        return None


def _is_blocked_device_path(path: str) -> bool:
    """对于会让读取挂起的具体设备/fd 路径返回 True。"""
    normalized = os.path.normpath(_expand_tilde(path))
    if normalized in _BLOCKED_DEVICE_PATHS:
        return True
    # /proc/self/fd/0-2 和 /proc/<pid>/fd/0-2 是 stdio 在 Linux 上的别名
    if normalized.startswith("/proc/") and normalized.endswith(
        ("/fd/0", "/fd/1", "/fd/2")
    ):
        return True
    # /proc/*/environ、/proc/*/cmdline、/proc/*/maps 会泄露宿主进程的
    # 密钥、命令行参数和内存布局（issue #4427）
    if normalized.startswith("/proc/") and normalized.endswith(
        ("/environ", "/cmdline", "/maps")
    ):
        return True
    return False


def _is_blocked_device(filepath: str, base_dir: str | Path | None = None) -> bool:
    """如果该路径会挂起进程（无限输出或阻塞输入）则返回 True。

    先检查字面路径，以便 /dev/stdin 这样的别名在解析为终端专属路径之前
    被拦截。然后检查每一个符号链接跳转，最后再检查最终解析路径，使指向
    设备的别名无法绕过守卫。
    """
    expanded = _expand_tilde(filepath)
    if base_dir is not None and not os.path.isabs(expanded):
        expanded = os.path.join(os.fspath(base_dir), expanded)
    normalized = os.path.normpath(expanded)
    if _is_blocked_device_path(normalized):
        return True

    seen: set[str] = set()
    current = normalized
    for _ in range(20):
        try:
            target = os.readlink(current)
        except OSError:
            break
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(current), target)
        target = os.path.normpath(target)
        if _is_blocked_device_path(target):
            return True
        if target in seen:
            break
        seen.add(target)
        current = target

    try:
        resolved = os.path.normpath(os.path.realpath(normalized))
    except (OSError, ValueError):
        return False
    if _is_blocked_device_path(resolved):
        return True
    return False


# 文件工具在不经过终端工具审批系统的情况下应拒绝写入的路径。
# 这些匹配 os.path.realpath 之后的路径前缀。
_SENSITIVE_PATH_PREFIXES = (
    "/etc/", "/boot/", "/usr/lib/systemd/",
    "/private/etc/", "/private/var/",
)
_SENSITIVE_EXACT_PATHS = {"/var/run/docker.sock", "/run/docker.sock"}

_hermes_config_resolved: str | None = None
_hermes_config_resolved_loaded = False


def _get_hermes_config_resolved() -> str | None:
    """返回 Hermes 配置文件解析后的绝对路径（已缓存）。"""
    global _hermes_config_resolved, _hermes_config_resolved_loaded
    if _hermes_config_resolved_loaded:
        return _hermes_config_resolved
    _hermes_config_resolved_loaded = True
    try:
        from hermes_cli.config import get_config_path
        _hermes_config_resolved = str(get_config_path().resolve())
    except Exception:
        try:
            _hermes_config_resolved = str(Path(_expand_tilde("~/.hermes/config.yaml")).resolve())
        except Exception:
            _hermes_config_resolved = None
    return _hermes_config_resolved


def _check_sensitive_path(filepath: str, task_id: str = "default") -> str | None:
    """如果路径指向敏感的系统位置，返回错误消息。"""
    try:
        resolved = str(_resolve_path_for_task(filepath, task_id))
    except (OSError, ValueError):
        resolved = filepath
    normalized = os.path.normpath(_expand_tilde(filepath))
    _err = (
        f"Refusing to write to sensitive system path: {filepath}\n"
        "Use the terminal tool with sudo if you need to modify system files."
    )
    for prefix in _SENSITIVE_PATH_PREFIXES:
        if resolved.startswith(prefix) or normalized.startswith(prefix):
            return _err
    if resolved in _SENSITIVE_EXACT_PATHS or normalized in _SENSITIVE_EXACT_PATHS:
        return _err
    # 阻止 agent 直接修改 Hermes 配置文件。approvals.mode 等安全设置存放在
    # 这里；一个被恶意注入提示词的 agent 可能通过写这个文件来静默地关闭
    # 执行审批。
    hermes_config = _get_hermes_config_resolved()
    if hermes_config and (resolved == hermes_config or normalized == hermes_config):
        return (
            f"Refusing to write to Hermes config file: {filepath}\n"
            "Agent cannot modify security-sensitive configuration. "
            "Edit ~/.hermes/config.yaml directly or use 'hermes config' instead."
        )
    return None


def _get_container_mirror_prefix_for_task(task_id: str = "default") -> str | None:
    """返回 Docker 文件工具在容器一侧的 Hermes 镜像前缀。"""
    try:
        from tools.terminal_tool import (
            _active_environments,
            _env_lock,
            _get_env_config,
            _resolve_container_task_id,
        )

        container_key = _resolve_container_task_id(task_id)
    except Exception:
        return None

    try:
        with _env_lock:
            env = _active_environments.get(container_key) or _active_environments.get(task_id)

        if env is not None:
            if env.__class__.__name__ == "DockerEnvironment" and bool(
                getattr(env, "_persistent", False)
            ):
                return "/root/.hermes"
            return None

        config = _get_env_config()
    except Exception:
        return None

    if config.get("env_type") == "docker" and config.get("container_persistent", True):
        return "/root/.hermes"
    return None


def _check_cross_profile_path(filepath: str, task_id: str = "default") -> str | None:
    """当 ``filepath`` 落在另一个 Hermes profile 的专属区域、主机一侧的
    权威 profile 状态的沙箱镜像、或 Docker 容器中 Hermes 状态的沙箱镜像
    时，返回一个软守卫警告。

    三个检测器按顺序运行：

    * cross-profile —— 命中另一个 profile 的 ``skills/plugins/cron/memories``
      目录的写入。
    * sandbox-mirror（#32049）—— 命中由非本地终端后端（Docker、Daytona
      等）创建的 ``…/sandboxes/<backend>/<task>/home/.hermes/…`` 镜像的
      写入；宿主 Hermes 进程从不读取该镜像，权威文件原封不动。
    * container-mirror（#32049 后续）—— 来自 Docker 容器内部、其绑定挂载
      的 home 剥离了 ``sandboxes/`` 前缀的写入，因此 agent 看到的是一条
      普通的 ``/root/.hermes/…`` 路径。

    当写入在作用域内或位于 Hermes 作用域之外时返回 ``None``。
    所有检测器都是软守卫——agent 可以在明确的用户指示后，向其写入工具
    传入 ``cross_profile=True`` 来覆盖任一检测器。这是纵深防御，【不是】
    安全边界——终端工具以同一操作系统用户身份运行，可以直接写入这些路径
    中的任何一个。检测规则见 ``agent/file_safety.classify_cross_profile_target``、
    ``classify_sandbox_mirror_target`` 和 ``classify_container_mirror_target``。
    """
    try:
        from agent.file_safety import (
            get_container_mirror_warning,
            get_cross_profile_warning,
            get_sandbox_mirror_warning,
        )
    except Exception:
        # 导入失败时放行——现有的敏感路径守卫加上 write_denied 清单仍然生效。
        return None

    # 经由任务的 cwd 解析，使一个在 cd 进 ``~/.hermes/profiles/other/`` 的
    # 会话中的相对路径 ``skills/foo/SKILL.md`` 能针对正确的基进行分类。
    try:
        resolved = str(_resolve_path_for_task(filepath, task_id))
    except (OSError, ValueError):
        resolved = filepath

    warning = get_cross_profile_warning(resolved)
    if warning is not None:
        return warning

    warning = get_sandbox_mirror_warning(resolved)
    if warning is not None:
        return warning

    return get_container_mirror_warning(
        resolved,
        mirror_prefix=_get_container_mirror_prefix_for_task(task_id),
    )


def _is_expected_write_exception(exc: Exception) -> bool:
    """对于不应进入错误日志的预期性写入拒绝返回 True。"""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError) and exc.errno in _EXPECTED_WRITE_ERRNOS:
        return True
    return False


_file_ops_lock = threading.Lock()
_file_ops_cache: dict = {}

# 按任务追踪已读取的文件，以检测重复读取循环并去重读取。
# 每个 task_id 下我们存储：
#   "last_key":     最近一次 read/search 调用的键（或 None）
#   "consecutive":  该完全相同的调用连续重复了多少次
#   "read_history": (path, offset, limit) 元组的集合，供 get_read_files_summary 使用
#   "dedup":        (resolved_path, offset, limit) → mtime 浮点数 的字典
#                   用于跳过对未更改文件的重复读取。在上下文压缩时重置
#                   （原始内容被摘要掉了，模型需要完整内容）。
#   "read_timestamps": resolved_path → 修改时间浮点数 的字典
#                      记录该任务最后一次读取（或写入）该文件的时刻。
#                      write_file 和 patch 用它检测 agent 读取与写入之间
#                      的外部更改。在成功写入后更新，使同一任务的连续编辑
#                      不会触发误报警告。
_read_tracker_lock = threading.Lock()
_read_tracker: dict = {}

# 按 (task_id, resolved_path) 追踪连续的 patch 失败。用于在模型反复
# patch 同一文件失败时升级提示（常见原因：对文件内容的陈旧视图、
# old_string 不唯一，或文件在 agent 读取与 patch 尝试之间被外部修改）。
# 在对该路径成功 patch 后重置。
_patch_failure_lock = threading.Lock()
_patch_failure_tracker: dict = {}  # {task_id: {resolved_path: count}}


def _record_patch_failure(task_id: str, resolved_path: str) -> int:
    """自增并返回该路径的连续失败计数。"""
    with _patch_failure_lock:
        task_failures = _patch_failure_tracker.setdefault(task_id, {})
        # 为每个任务的字典设上限，避免在 agent 对许多不同文件失败的
        # 长会话中无限增长。每个任务 64 个不同的失败文件已经很宽裕；
        # 更早的条目会被驱逐。
        if len(task_failures) >= 64 and resolved_path not in task_failures:
            try:
                first_key = next(iter(task_failures))
                del task_failures[first_key]
            except StopIteration:
                pass
        task_failures[resolved_path] = task_failures.get(resolved_path, 0) + 1
        return task_failures[resolved_path]


def _reset_patch_failures(task_id: str, resolved_paths: list) -> None:
    """清除给定路径的连续失败计数。"""
    if not resolved_paths:
        return
    with _patch_failure_lock:
        task_failures = _patch_failure_tracker.get(task_id)
        if not task_failures:
            return
        for rp in resolved_paths:
            task_failures.pop(rp, None)

# 每个 _read_tracker[task_id] 内部容器的每任务上限。
# 一个 CLI 会话在其生命周期内使用一个稳定的 task_id；没有这些上限的话，
# 一个 1 万次读取的会话会累积约 1.5MB 永远不会再被引用的字典/集合状态
# （只有最近的读取对去重、循环检测和外部编辑警告有意义）。硬性上限把
# 这种累积控制在几百 KB 以内，与会话长度无关。
_READ_HISTORY_CAP = 500       # 集合；仅供 get_read_files_summary 使用
_DEDUP_CAP = 1000             # 字典；跳过相同重复读取的守卫
_READ_TIMESTAMPS_CAP = 1000   # 字典；用于 write/patch 的外部编辑检测
_READ_DEDUP_STATUS_MESSAGE = (
    "File unchanged since last read. The content from "
    "the earlier read_file result in this conversation is "
    "still current — refer to that instead of re-reading."
)


def _cap_read_tracker_data(task_data: dict) -> None:
    """对每任务的读取追踪子容器强制施加大小上限。

    必须在持有 ``_read_tracker_lock`` 时调用。驱逐策略：

      * ``read_history``（集合）：溢出时弹出任意条目。这没问题，因为该
        集合只用于诊断摘要；丢失旧条目只会裁剪摘要的尾部。
      * ``dedup`` / ``read_timestamps``（字典）：按插入顺序弹出最旧条目
        （Python 3.7+ 字典）。被驱逐的条目会在未来重复读取时失去去重
        跳过（文件会被重新发送一次）和外部编辑 mtime 比较（write/patch
        回退到非 mtime 检查）。两者都是优雅降级，不是 bug。
    """
    rh = task_data.get("read_history")
    if rh is not None and len(rh) > _READ_HISTORY_CAP:
        excess = len(rh) - _READ_HISTORY_CAP
        for _ in range(excess):
            try:
                rh.pop()
            except KeyError:
                break

    dedup = task_data.get("dedup")
    if dedup is not None and len(dedup) > _DEDUP_CAP:
        excess = len(dedup) - _DEDUP_CAP
        for _ in range(excess):
            try:
                dedup.pop(next(iter(dedup)))
            except (StopIteration, KeyError):
                break

    dedup_hits = task_data.get("dedup_hits")
    if dedup_hits is not None and len(dedup_hits) > _DEDUP_CAP:
        excess = len(dedup_hits) - _DEDUP_CAP
        for _ in range(excess):
            try:
                dedup_hits.pop(next(iter(dedup_hits)))
            except (StopIteration, KeyError):
                break

    ts = task_data.get("read_timestamps")
    if ts is not None and len(ts) > _READ_TIMESTAMPS_CAP:
        excess = len(ts) - _READ_TIMESTAMPS_CAP
        for _ in range(excess):
            try:
                ts.pop(next(iter(ts)))
            except (StopIteration, KeyError):
                break


def _is_internal_file_status_text(content: str) -> bool:
    """当内容看起来像内部文件工具状态而非真实文件字节时返回 True。

    read_file 的去重状态消息绝不能被当作文件内容持久化。最明显的形态是
    模型逐字回显该消息，但实际中它还会在调用 write_file 前用少量框架
    文本（开头的 "Note:"、末尾的换行 + 短注释等）包裹它。我们把任何
    正文以该状态消息为主的较短写入视为同一类内容损坏。

    启发式判断：
      * 严格相等（strip 之后）——逐字形态。
      * 或者 strip 之后的内容包含完整状态消息【并且】足够短以至于状态消息
        占主导（<= 消息长度的 2 倍）。简短的、以状态为主的写入不可能合理地
        是真实文件——恰巧引用这条内部消息的合法文档/笔记总是会比这长得多。
    """
    if not isinstance(content, str):
        return False
    stripped = content.strip()
    if not stripped:
        return False
    if stripped == _READ_DEDUP_STATUS_MESSAGE:
        return True
    if _READ_DEDUP_STATUS_MESSAGE in stripped and \
            len(stripped) <= 2 * len(_READ_DEDUP_STATUS_MESSAGE):
        return True
    return False


def _looks_like_read_file_line_numbered_content(content: str) -> bool:
    """当内容以 read_file 的 ``LINE_NUM|CONTENT`` 展示格式为主时返回 True。

    ``read_file`` 有意向模型返回带行号的文本。如果该展示格式被回显进
    ``write_file``，配置/源文件会被静默地用 `` 1|`` 这样的前缀损坏。我们
    拒绝那些非空行大多是连续的 read_file 风格带编号行的写入，同时允许
    稀疏的字面管道内容，例如单独一行 ``1|value``。
    """
    if not isinstance(content, str):
        return False

    lines = [line for line in content.splitlines() if line.strip()]
    if len(lines) < 2:
        return False

    numbered: list[int] = []
    for line in lines:
        stripped = line.lstrip()
        prefix, sep, _rest = stripped.partition("|")
        if sep and prefix.isdigit():
            numbered.append(int(prefix))

    if len(numbered) < 2:
        return False
    if len(numbered) / len(lines) < 0.6:
        return False

    consecutive_pairs = sum(
        1 for prev, current in zip(numbered, numbered[1:])
        if current == prev + 1
    )
    return consecutive_pairs >= len(numbered) - 1


def _is_internal_file_tool_content(content: str) -> bool:
    """当内容是文件工具展示文本而非预期的文件字节时返回 True。"""
    return (
        _is_internal_file_status_text(content)
        or _looks_like_read_file_line_numbered_content(content)
    )


def _get_file_ops(task_id: str = "default") -> ShellFileOperations:
    """获取或创建终端环境对应的 ShellFileOperations。

    遵循 TERMINAL_ENV 设置——如果该 task_id 尚未有环境，就使用配置的
    后端（local、docker、modal 等）创建一个，而不是总默认用 local。

    线程安全：使用与 terminal_tool 相同的每任务创建锁，防止并发工具调用
    重复创建沙箱。

    说明：子 agent 的 task_id 会通过 ``_resolve_container_task_id`` 合并到
    "default"，使 delegate_task 的子任务共享父任务的容器及其缓存的
    file_ops。带有已注册环境覆盖的 RL/benchmark task_id 保留各自的隔离。
    """
    from tools.terminal_tool import (
        _active_environments, _env_lock, _create_environment,
        _get_env_config, _last_activity, _start_cleanup_thread,
        _creation_locks,
        _creation_locks_lock,
        _resolve_container_task_id,
    )
    import time

    raw_task_id = task_id or "default"
    task_id = _resolve_container_task_id(raw_task_id)

    # 快速路径：检查缓存——但同时校验底层环境是否仍存活
    # （它可能已被清理线程杀死）。
    with _file_ops_lock:
        cached = _file_ops_cache.get(task_id)
    if cached is not None:
        with _env_lock:
            if task_id in _active_environments:
                _last_activity[task_id] = time.time()
                return cached
            else:
                # 环境已被清理——使陈旧的缓存条目失效
                with _file_ops_lock:
                    _file_ops_cache.pop(task_id, None)

    # 在构建 file_ops 之前需要确保环境存在。
    # 获取每任务锁，使只有一个线程创建沙箱。
    with _creation_locks_lock:
        if task_id not in _creation_locks:
            _creation_locks[task_id] = threading.Lock()
        task_lock = _creation_locks[task_id]

    with task_lock:
        # 二次检查：在我们等待时另一个线程可能已经创建了它
        with _env_lock:
            if task_id in _active_environments:
                _last_activity[task_id] = time.time()
                terminal_env = _active_environments[task_id]
            else:
                terminal_env = None

        if terminal_env is None:
            from tools.terminal_tool import resolve_task_overrides

            config = _get_env_config()
            env_type = config["env_type"]
            overrides = resolve_task_overrides(raw_task_id)

            if env_type == "docker":
                image = overrides.get("docker_image") or config["docker_image"]
            elif env_type == "singularity":
                image = overrides.get("singularity_image") or config["singularity_image"]
            elif env_type == "modal":
                image = overrides.get("modal_image") or config["modal_image"]
            elif env_type == "daytona":
                image = overrides.get("daytona_image") or config["daytona_image"]
            else:
                image = ""

            cwd = overrides.get("cwd") or config["cwd"]
            logger.info("Creating new %s environment for task %s...", env_type, task_id[:8])

            container_config = None
            if env_type in {"docker", "singularity", "modal", "daytona"}:
                container_config = {
                    "container_cpu": config.get("container_cpu", 1),
                    "container_memory": config.get("container_memory", 5120),
                    "container_disk": config.get("container_disk", 51200),
                    "container_persistent": config.get("container_persistent", True),
                    "docker_volumes": config.get("docker_volumes", []),
                    "docker_mount_cwd_to_workspace": config.get("docker_mount_cwd_to_workspace", False),
                    "docker_forward_env": config.get("docker_forward_env", []),
                    "docker_run_as_host_user": config.get("docker_run_as_host_user", False),
                }

            ssh_config = None
            if env_type == "ssh":
                ssh_config = {
                    "host": config.get("ssh_host", ""),
                    "user": config.get("ssh_user", ""),
                    "port": config.get("ssh_port", 22),
                    "key": config.get("ssh_key", ""),
                    "persistent": config.get("ssh_persistent", False),
                }

            local_config = None
            if env_type == "local":
                local_config = {
                    "persistent": config.get("local_persistent", False),
                }

            terminal_env = _create_environment(
                env_type=env_type,
                image=image,
                cwd=cwd,
                timeout=config["timeout"],
                ssh_config=ssh_config,
                container_config=container_config,
                local_config=local_config,
                task_id=task_id,
                host_cwd=config.get("host_cwd"),
            )

            with _env_lock:
                _active_environments[task_id] = terminal_env
                _last_activity[task_id] = time.time()

            _start_cleanup_thread()
            logger.info("%s environment ready for task %s", env_type, task_id[:8])

    # 从（保证存活的）环境构建 file_ops 并缓存
    file_ops = ShellFileOperations(terminal_env)
    with _file_ops_lock:
        _file_ops_cache[task_id] = file_ops
    return file_ops


def clear_file_ops_cache(task_id: str = None):
    """清空文件操作缓存。"""
    with _file_ops_lock:
        if task_id:
            _file_ops_cache.pop(task_id, None)
        else:
            _file_ops_cache.clear()


def read_file_tool(path: str, offset: int = 1, limit: int = 500, task_id: str = "default") -> str:
    """读取文件，支持分页和行号。"""
    try:
        offset, limit = normalize_read_pagination(offset, limit)

        # ── 设备路径守卫 ─────────────────────────────────────────
        # 阻断会挂起进程的路径（无限输出、阻塞在输入上）。纯路径检查——不做 I/O。
        device_base = None if Path(path).expanduser().is_absolute() else _resolve_base_dir(task_id)
        if _is_blocked_device(path, base_dir=device_base):
            return json.dumps({
                "error": (
                    f"Cannot read '{path}': this is a device file that would "
                    "block or produce infinite output."
                ),
            })

        _resolved = _resolve_path_for_task(path, task_id)

        # ── 结构化文档抽取 ────────────────────────────
        # 在二进制扩展名守卫之前尝试，使 .docx/.xlsx 能渲染为文本。
        # 格式错误的文档会落入普通路径/二进制守卫。
        from tools.read_extract import ExtractionError, extract_document_text, is_extractable_document

        if is_extractable_document(str(_resolved)):
            try:
                extracted_text = extract_document_text(str(_resolved))
            except ExtractionError:
                logger.debug("document extraction failed for %s", path, exc_info=True)
            else:
                file_ops = _get_file_ops(task_id)
                lines = extracted_text.splitlines()
                total_lines = len(lines)
                end_line = offset + limit - 1
                page_text = "\n".join(lines[offset - 1:end_line])
                result_dict = {
                    "content": file_ops._add_line_numbers(page_text, offset) if page_text else "",
                    "total_lines": total_lines,
                    "file_size": os.path.getsize(_resolved),
                    "truncated": total_lines > end_line,
                    "extracted_document": True,
                }
                if result_dict["truncated"]:
                    result_dict["hint"] = (
                        f"Use offset={end_line + 1} to continue reading "
                        f"(showing {offset}-{min(end_line, total_lines)} of {total_lines} lines)"
                    )
                content_len = len(result_dict["content"])
                max_chars = _get_max_read_chars()
                if content_len > max_chars:
                    return json.dumps({
                        "error": (
                            f"Read produced {content_len:,} characters which exceeds "
                            f"the safety limit ({max_chars:,} chars). "
                            "Use offset and limit to read a smaller range. "
                            f"The document has {total_lines} lines of extracted text."
                        ),
                        "path": path,
                        "total_lines": total_lines,
                        "file_size": result_dict["file_size"],
                    }, ensure_ascii=False)
                if result_dict["content"]:
                    result_dict["content"] = redact_sensitive_text(result_dict["content"], code_file=True)
                return json.dumps(result_dict, ensure_ascii=False)

        # ── 二进制文件守卫 ─────────────────────────────────────────
        # 按扩展名阻断二进制文件（不做 I/O）。
        if has_binary_extension(str(_resolved)):
            _ext = _resolved.suffix.lower()
            return json.dumps({
                "error": (
                    f"Cannot read binary file '{path}' ({_ext}). "
                    "Use vision_analyze for images, or terminal to inspect binary files."
                ),
            })

        # ── Hermes 内部路径守卫 ────────────────────────────────
        # 防止通过 catalog 或 hub 元数据文件进行提示注入，并阻断
        # HERMES_HOME 下的凭据存储。传入已解析的路径，使针对
        # TERMINAL_CWD == HERMES_HOME 的相对路径读取（例如 "auth.json"）
        # 仍会命中黑名单——get_read_block_error 自己的 resolve() 是相对
        # Python 进程 cwd 运行的，可能与这里不同。
        block_error = get_read_block_error(str(_resolved))
        if block_error:
            return json.dumps({"error": block_error})

        # ── 去重检查 ───────────────────────────────────────────────
        # 如果我们已经读取过这个完全相同的 (path, offset, limit) 且文件
        # 此后未被修改，就返回一个轻量存根而不是重发相同内容。节省上下文 token。
        resolved_str = str(_resolved)
        dedup_key = (resolved_str, offset, limit)
        with _read_tracker_lock:
            task_data = _read_tracker.setdefault(task_id, {
                "last_key": None, "consecutive": 0,
                "read_history": set(), "dedup": {},
                "dedup_hits": {}, "read_timestamps": {},
            })
            # 向后兼容：为早于 dedup_hits/read_timestamps 的既有追踪条目
            # （长寿任务或跨越了升级边界）补齐字段。
            if "dedup_hits" not in task_data:
                task_data["dedup_hits"] = {}
            if "read_timestamps" not in task_data:
                task_data["read_timestamps"] = {}
            cached_mtime = task_data.get("dedup", {}).get(dedup_key)

        if cached_mtime is not None:
            try:
                current_mtime = os.path.getmtime(resolved_str)
                if current_mtime == cached_mtime:
                    # 统计重复的存根返回，使那些忽略 "参考早前结果" 提示的
                    # 弱工具跟随者不会在无限读取循环中耗尽迭代预算。对同一键
                    # 返回 2 次存根后，我们升级为硬阻断，镜像真实读取的
                    # count>=4 路径。
                    with _read_tracker_lock:
                        hits = task_data["dedup_hits"].get(dedup_key, 0) + 1
                        task_data["dedup_hits"][dedup_key] = hits
                        _cap_read_tracker_data(task_data)

                    if hits >= 2:
                        return json.dumps({
                            "error": (
                                f"BLOCKED: You have called read_file on this "
                                f"exact region {hits + 1} times and the file "
                                "has NOT changed. STOP calling read_file for "
                                "this path — the content from your earlier "
                                "read_file result in this conversation is "
                                "still current. Proceed with your task using "
                                "the information you already have."
                            ),
                            "path": path,
                            "already_read": hits + 1,
                        }, ensure_ascii=False)

                    return json.dumps({
                        "status": "unchanged",
                        "message": _READ_DEDUP_STATUS_MESSAGE,
                        "path": path,
                        "dedup": True,
                        "content_returned": False,
                    }, ensure_ascii=False)
            except OSError:
                pass  # stat 失败——落入完整读取

        # ── 执行读取 ──────────────────────────────────────────
        file_ops = _get_file_ops(task_id)
        result = file_ops.read_file(path, offset, limit)
        result_dict = result.to_dict()

        # ── 字符数守卫 ─────────────────────────────────────
        # 我们与具体模型无关，无法统计 token；字符数是我们能用的最佳代理。
        # 如果读取产生了不合理的大量内容，就拒绝它并让模型缩小范围。
        # 注意：我们检查的是格式化后的内容（带行号前缀），而不是原始文件
        # 大小，因为那才是真正进入上下文的东西。
        # 在脱敏之前检查，避免对超大内容做昂贵的正则。
        content_len = len(result.content or "")
        file_size = result_dict.get("file_size", 0)
        max_chars = _get_max_read_chars()
        if content_len > max_chars:
            total_lines = result_dict.get("total_lines", "unknown")
            return json.dumps({
                "error": (
                    f"Read produced {content_len:,} characters which exceeds "
                    f"the safety limit ({max_chars:,} chars). "
                    "Use offset and limit to read a smaller range. "
                    f"The file has {total_lines} lines total."
                ),
                "path": path,
                "total_lines": total_lines,
                "file_size": file_size,
            }, ensure_ascii=False)

        # ── 脱敏密钥（在守卫检查之后，以跳过超大内容）──
        if result.content:
            result.content = redact_sensitive_text(result.content, code_file=True)
            result_dict["content"] = result.content

        # 大文件提示：如果文件很大且调用者未要求窄窗口，就引导其定向读取。
        if (file_size and file_size > _LARGE_FILE_HINT_BYTES
                and limit > 200
                and result_dict.get("truncated")):
            result_dict.setdefault("_hint", (
                f"This file is large ({file_size:,} bytes). "
                "Consider reading only the section you need with offset and limit "
                "to keep context usage efficient."
            ))

        # ── 追踪以检测连续循环 ──────────────────────
        read_key = ("read", path, offset, limit)
        with _read_tracker_lock:
            # 确保 "dedup" / "dedup_hits" 键存在（向后兼容来自
            # 去重守卫出现之前会话的旧追踪状态）。
            if "dedup" not in task_data:
                task_data["dedup"] = {}
            if "dedup_hits" not in task_data:
                task_data["dedup_hits"] = {}
            # 真实读取成功——此键不再处于存根循环中，因此重置其命中计数。
            # （文件已更改，或之前 stat 失败而落入了下方流程。）
            task_data["dedup_hits"].pop(dedup_key, None)
            task_data["read_history"].add((path, offset, limit))
            if task_data["last_key"] == read_key:
                task_data["consecutive"] += 1
            else:
                task_data["last_key"] = read_key
                task_data["consecutive"] = 1
            count = task_data["consecutive"]

            # 在读取时存储 mtime，用于两个目的：
            # 1. 去重：跳过对未更改文件的相同重复读取。
            # 2. 陈旧性：如果文件在 agent 最后一次读取后被更改（外部编辑、
            #    并发 agent 等），在 write/patch 时发出警告。
            try:
                _mtime_now = os.path.getmtime(resolved_str)
                task_data["dedup"][dedup_key] = _mtime_now
                task_data.setdefault("read_timestamps", {})[resolved_str] = _mtime_now
            except OSError:
                pass  # 无法 stat——跳过对该条目的追踪

            # 为每任务容器设上限，使长 CLI 会话不会累积数 MB 的字典/集合
            # 状态。见 _cap_read_tracker_data。
            _cap_read_tracker_data(task_data)

        # 跨 agent 文件状态注册表（独立于上方每任务的读取追踪器）：记录
        # 【本】agent 已读取过该路径，使 write/patch 能检测到在我们读取
        # 之后发生的兄弟子 agent 写入。当 offset>1 或读取被截断
        # （大文件内容多于 limit 所覆盖）时为部分读取。
        # 放在 _read_tracker_lock 之外，使注册表自身的锁不会嵌套在我们的锁下。
        try:
            _partial = (offset > 1) or bool(result_dict.get("truncated"))
            file_state.record_read(task_id, resolved_str, partial=_partial)
        except Exception:
            logger.debug("file_state.record_read failed", exc_info=True)

        if count >= 4:
            # 硬阻断：停止返回内容以打破循环
            return json.dumps({
                "error": (
                    f"BLOCKED: You have read this exact file region {count} times in a row. "
                    "The content has NOT changed. You already have this information. "
                    "STOP re-reading and proceed with your task."
                ),
                "path": path,
                "already_read": count,
            }, ensure_ascii=False)
        elif count >= 3:
            result_dict["_warning"] = (
                f"You have read this exact file region {count} times consecutively. "
                "The content has not changed since your last read. Use the information you already have. "
                "If you are stuck in a loop, stop reading and proceed with writing or responding."
            )

        return json.dumps(result_dict, ensure_ascii=False)
    except Exception as e:
        return tool_error(str(e))




def reset_file_dedup(task_id: str = None):
    """清空文件读取的去重缓存。

    在上下文压缩后调用——原始读取内容已被摘要掉，因此模型如果再次读取同一
    文件，就需要完整内容。没有这一步，压缩后的读取会返回一个 "文件未更改"
    的存根，指向上下文中已不存在的内容。

    传入 task_id 只清除该任务；不传则清除全部。
    """
    with _read_tracker_lock:
        if task_id:
            task_data = _read_tracker.get(task_id)
            if task_data:
                if "dedup" in task_data:
                    task_data["dedup"].clear()
                if "dedup_hits" in task_data:
                    task_data["dedup_hits"].clear()
        else:
            for task_data in _read_tracker.values():
                if "dedup" in task_data:
                    task_data["dedup"].clear()
                if "dedup_hits" in task_data:
                    task_data["dedup_hits"].clear()


def notify_other_tool_call(task_id: str = "default"):
    """重置某任务的连续读取/搜索计数器。

    由工具分发器（model_tools.py）在执行 read_file / search_files【之外】
    的工具时调用。这确保我们只对【真正连续】的重复读取发出警告或阻断——
    如果 agent 在中间做了任何其他事（write、patch、terminal 等），计数器
    就重置，下一次读取被视为全新的。
    """
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if task_data:
            task_data["last_key"] = None
            task_data["consecutive"] = 0
            # 一次插入了非读取工具调用会打断任何进行中的存根循环，
            # 因此也清除每键的去重命中计数器。
            if "dedup_hits" in task_data:
                task_data["dedup_hits"].clear()


def _invalidate_dedup_for_path(filepath: str, task_id: str) -> None:
    """移除所有解析路径匹配 *filepath* 的去重缓存条目。

    在 write_file 和 patch 之后调用，使随后对同一路径的 read_file 始终
    返回新鲜内容，而不是陈旧的 "文件未更改" 存根。去重缓存键是
    ``(resolved_path, offset, limit)`` 元组；我们必须驱逐被写入路径的
    【所有】offset/limit 组合，因为任何缓存范围现在都可能已陈旧。

    必须在【未】持有 ``_read_tracker_lock`` 时调用——内部会自行获取它。
    """
    try:
        resolved = str(_resolve_path(filepath))
    except (OSError, ValueError):
        return
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if task_data is None:
            return
        dedup = task_data.get("dedup")
        if not dedup:
            return
        # 收集要删除的键（迭代期间不能修改字典）。
        stale_keys = [k for k in dedup if k[0] == resolved]
        for k in stale_keys:
            del dedup[k]


def _update_read_timestamp(filepath: str, task_id: str) -> None:
    """在成功写入后记录文件的当前修改时间。

    在 write_file 和 patch 之后调用，使同一任务的连续编辑不会触发误报的
    陈旧性警告——每次写入都刷新存储的时间戳以匹配文件的新状态。

    同时使被写入路径的去重缓存失效，使后续读取返回新鲜内容（修复 #13144）。
    """
    # 先使去重失效（在为时间戳更新获取锁之前）。
    _invalidate_dedup_for_path(filepath, task_id)
    try:
        resolved = str(_resolve_path_for_task(filepath, task_id))
        current_mtime = os.path.getmtime(resolved)
    except (OSError, ValueError):
        return
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if task_data is not None:
            task_data.setdefault("read_timestamps", {})[resolved] = current_mtime
            _cap_read_tracker_data(task_data)


def _check_file_staleness(filepath: str, task_id: str) -> str | None:
    """检查文件自 agent 最后一次读取后是否被修改过。

    如果文件已陈旧（mtime 自该任务最后一次 read_file 调用以来已更改），
    返回警告字符串；如果文件是新鲜的或从未被读取过则返回 None。
    不会阻断——写入仍会继续。
    """
    try:
        resolved = str(_resolve_path_for_task(filepath, task_id))
    except (OSError, ValueError):
        return None
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if not task_data:
            return None
        read_mtime = task_data.get("read_timestamps", {}).get(resolved)
    if read_mtime is None:
        return None  # 文件从未被读取——没有可比较的对象
    try:
        current_mtime = os.path.getmtime(resolved)
    except OSError:
        return None  # 无法 stat——文件可能已被删除，让 write 去处理
    if current_mtime != read_mtime:
        return (
            f"Warning: {filepath} was modified since you last read it "
            "(external edit or concurrent agent). The content you read may be "
            "stale. Consider re-reading the file to verify before writing."
        )
    return None


def _mark_verification_stale(
    task_id: str,
    resolved_paths: list[str],
    session_id: str | None = None,
) -> None:
    """尽力而为地记录：成功的编辑使先前的验证变得陈旧。"""
    paths = [p for p in resolved_paths if p]
    if not paths:
        return
    try:
        from agent.coding_context import project_facts_for
        from agent.verification_evidence import mark_workspace_edited

        cwd = None
        for path in paths:
            try:
                candidate = str(Path(path).parent)
            except Exception:
                continue
            if project_facts_for(candidate):
                cwd = candidate
                break
        if cwd is None:
            cwd = _authoritative_workspace_root(task_id)
        if cwd is None:
            try:
                cwd = str(Path(paths[0]).parent)
            except Exception:
                cwd = None
        mark_workspace_edited(session_id=session_id or task_id, cwd=cwd, paths=paths)
    except Exception:
        logger.debug("verification stale marker failed", exc_info=True)


def write_file_tool(path: str, content: str, task_id: str = "default",
                    cross_profile: bool = False,
                    session_id: str | None = None) -> str:
    """把内容写入文件。

    ``cross_profile`` 退出软性的跨 Hermes profile 守卫。该守卫仅在写入
    落在另一个 profile 的 skills/plugins/cron/memories 目录时触发；其他
    情况不受影响。在明确的用户指示后传入 ``True``——与终端工具上的
    ``force`` 形态相同。
    """
    sensitive_err = _check_sensitive_path(path, task_id)
    if sensitive_err:
        return tool_error(sensitive_err)
    if not cross_profile:
        cross_warning = _check_cross_profile_path(path, task_id)
        if cross_warning:
            return tool_error(cross_warning)
    if _is_internal_file_tool_content(content):
        return tool_error(
            "Refusing to write internal read_file display text as file content. "
            "Strip read_file line-number prefixes or reconstruct the intended "
            "file contents before writing."
        )
    try:
        # 为注册表锁 + 陈旧检查解析一次。此处的失败会回退到旧路径
        # ——写入继续，下方的每任务陈旧检查仍会运行。
        try:
            _resolved = str(_resolve_path_for_task(path, task_id))
        except Exception:
            _resolved = None

        if _resolved is None:
            stale_warning = _check_file_staleness(path, task_id)
            file_ops = _get_file_ops(task_id)
            result = file_ops.write_file(path, content)
            result_dict = result.to_dict()
            if stale_warning:
                result_dict["_warning"] = stale_warning
            if not result_dict.get("error"):
                _mark_verification_stale(task_id, [path], session_id=session_id)
            _update_read_timestamp(path, task_id)
            return json.dumps(result_dict, ensure_ascii=False)

        # 按路径串行化 read→modify→write 区域，使并发子 agent 不会在同一
        # 文件上交错。不同路径仍完全并行。
        with file_state.lock_path(_resolved):
            # 跨 agent 陈旧性优先于每任务警告（当两者都触发时）
            # ——它的消息会点名兄弟子 agent。
            cross_warning = file_state.check_stale(task_id, _resolved)
            stale_warning = _check_file_staleness(path, task_id)
            # 工作区偏差警告：相对路径解析到终端 cwd 之外（worktree-cwd bug）。
            # 三者中优先级最低。
            cwd_warning = _path_resolution_warning(path, Path(_resolved), task_id)
            file_ops = _get_file_ops(task_id)
            result = file_ops.write_file(_resolved, content)
            result_dict = result.to_dict()
            effective_warning = cross_warning or stale_warning or cwd_warning
            if effective_warning:
                result_dict["_warning"] = effective_warning
            # 始终上报实际写入的【绝对】路径，使错误的 cwd 不匹配能在响应中
            # 显露出来，而不是静默地把编辑路由到错误的检出。
            result_dict["resolved_path"] = _resolved
            if not result_dict.get("error"):
                result_dict["files_modified"] = [_resolved]
                _mark_verification_stale(task_id, [_resolved], session_id=session_id)
            # 在成功写入后刷新时间戳，使本任务的连续写入不会触发误报的
            # 陈旧性警告。
            _update_read_timestamp(path, task_id)
            if not result_dict.get("error"):
                file_state.note_write(task_id, _resolved)
        return json.dumps(result_dict, ensure_ascii=False)
    except Exception as e:
        if _is_expected_write_exception(e):
            logger.debug("write_file expected denial: %s: %s", type(e).__name__, e)
        else:
            logger.error("write_file error: %s: %s", type(e).__name__, e, exc_info=True)
        return tool_error(str(e))


def patch_tool(mode: str = "replace", path: str = None, old_string: str = None,
               new_string: str = None, replace_all: bool = False, patch: str = None,
               task_id: str = "default", cross_profile: bool = False,
               session_id: str | None = None) -> str:
    """使用 replace 模式或 V4A patch 格式来修补文件。

    ``cross_profile`` 对于落在另一个 profile 的 skills/plugins/cron/memories
    目录下的目标，退出软性的跨 Hermes profile 守卫。形态与 ``write_file`` 的
    标志相同。
    """
    # 对 replace（显式路径）和 V4A patch（抽取路径）都检查敏感路径
    _paths_to_check = []
    if path:
        _paths_to_check.append(path)
    if mode == "patch" and patch:
        import re as _re
        from tools.path_security import has_traversal_component
        for _m in _re.finditer(r'^\*\*\*\s+(?:Update|Add|Delete)\s+File:\s*(.+)$', patch, _re.MULTILINE):
            v4a_path = _m.group(1).strip()
            # V4A 路径头来自 patch 内容，而非显式的 ``path=`` 参数
            # ——因此它们更易受攻击者影响（技能内容、网页抽取、提示注入）。
            # 拒绝 V4A 头中的 ``..`` 穿越：从单个 cwd 发出的合法多文件 patch
            # 总能用绝对路径或不带 ``..`` 的、相对 agent cwd 的路径。显式
            # ``path=`` 参数保持不变，因为 agent 会合法地使用相对 ``..`` 路径
            # （例如从 worktree 中 ``patch path="../other_module/x.py"``）。
            if has_traversal_component(v4a_path):
                return tool_error(
                    f"V4A patch header contains '..' traversal: {v4a_path!r}. "
                    "Use the agent's cwd-relative path (no '..') or an absolute "
                    "path in '*** Update File:' / '*** Add File:' / '*** Delete File:' headers."
                )
            _paths_to_check.append(v4a_path)
    for _p in _paths_to_check:
        sensitive_err = _check_sensitive_path(_p, task_id)
        if sensitive_err:
            return tool_error(sensitive_err)
        if not cross_profile:
            cross_warning = _check_cross_profile_path(_p, task_id)
            if cross_warning:
                return tool_error(cross_warning)
    try:
        # 解析路径用于加锁。排序 + 去重，使并发调用者以相同顺序加锁
        # ——防止在重叠的多文件 V4A patch 上死锁。
        _resolved_paths: list[str] = []
        _seen: set[str] = set()
        for _p in _paths_to_check:
            try:
                _r = str(_resolve_path_for_task(_p, task_id))
            except Exception:
                _r = None
            if _r and _r not in _seen:
                _resolved_paths.append(_r)
                _seen.add(_r)
        _resolved_paths.sort()

        # 通过 ExitStack 按排序顺序获取每路径锁。单路径时退化为一把锁；
        # 空列表（无法解析）时是空操作，执行不变地落入下方流程。
        from contextlib import ExitStack
        with ExitStack() as _locks:
            for _r in _resolved_paths:
                _locks.enter_context(file_state.lock_path(_r))

            # 收集警告——先看跨 agent 注册表（会点名兄弟），再以每任务
            # 追踪器作为回退。
            stale_warnings: list[str] = []
            _path_to_resolved: dict[str, str] = {}
            for _p in _paths_to_check:
                try:
                    _r = str(_resolve_path_for_task(_p, task_id))
                except Exception:
                    _r = None
                _path_to_resolved[_p] = _r
                _cross = file_state.check_stale(task_id, _r) if _r else None
                _sw = _cross or _check_file_staleness(_p, task_id)
                if not _sw and _r:
                    # 工作区偏差警告（worktree-cwd bug）：相对路径解析到
                    # 终端 cwd 之外。
                    _sw = _path_resolution_warning(_p, Path(_r), task_id)
                if _sw:
                    stale_warnings.append(_sw)

            file_ops = _get_file_ops(task_id)

            if mode == "replace":
                if not path:
                    return tool_error("path required")
                if old_string is None or new_string is None:
                    return tool_error("old_string and new_string required")
                # 把解析后的【绝对】路径传给 shell 层，使其操作工具层解析出的
                # 那个确切文件——shell 自身的 cwd 可能不同（worktree-cwd bug），
                # 相对路径会让两层对正在编辑哪个文件产生分歧。
                _replace_target = _path_to_resolved.get(path) or path
                result = file_ops.patch_replace(_replace_target, old_string, new_string, replace_all)
            elif mode == "patch":
                if not patch:
                    return tool_error("patch content required")
                result = file_ops.patch_v4a(patch)
            else:
                return tool_error(f"Unknown mode: {mode}")

            result_dict = result.to_dict()
            if stale_warnings:
                result_dict["_warning"] = stale_warnings[0] if len(stale_warnings) == 1 else " | ".join(stale_warnings)
            # 上报实际修补的【绝对】路径，使错误的 cwd 不匹配（例如 worktree
            # 会话编辑主检出）能在响应中显露，而不是静默地落到别处。
            _resolved_modified = [
                _path_to_resolved.get(_p) or _p for _p in _paths_to_check
            ]
            # 为所有成功修补的路径刷新存储的时间戳，使本任务的连续编辑
            # 不会触发误报警告。
            if not result_dict.get("error"):
                result_dict["files_modified"] = _resolved_modified
                if len(_resolved_modified) == 1:
                    result_dict["resolved_path"] = _resolved_modified[0]
                _mark_verification_stale(task_id, _resolved_modified, session_id=session_id)
                for _p in _paths_to_check:
                    _update_read_timestamp(_p, task_id)
                    _r = _path_to_resolved.get(_p)
                    if _r:
                        file_state.note_write(task_id, _r)
                # 成功的 patch：清除所触及路径之前的任何连续失败计数，
                # 使将来在同一路径上的失败从新的升级周期开始。
                _reset_patch_failures(task_id, [
                    _r for _r in (_path_to_resolved.get(_p) for _p in _paths_to_check) if _r
                ])
        # 当 old_string 未找到时的提示——节省 agent 用陈旧内容重试而非
        # 重新读取文件的迭代。当 patch_replace 已经附带了一段丰富的
        # "Did you mean?" 片段（严格比通用提示更有用）时，此提示被抑制。
        if result_dict.get("error") and "Could not find" in str(result_dict["error"]):
            # 为 replace 模式追踪每文件连续失败。``path`` 参数只存在于
            # replace 模式；对 V4A patch 我们需要遍历其头，但实际上 V4A 失败
            # 罕见得多，现有的 _hint 已足够覆盖。
            failure_count = 0
            if mode == "replace" and path:
                resolved = _path_to_resolved.get(path) or path
                failure_count = _record_patch_failure(task_id, resolved)

            if failure_count >= 3:
                # 在同一路径上多次连续失败后的升级提示。最常见的原因是对文件
                # 的陈旧视图——模型用同一个 old_string 重试，而内容此后已变化。
                # 显露失败计数，使模型意识到自己陷入了循环，并通过重新读取或
                # 回退到 write_file 来跳出。
                result_dict["_hint"] = (
                    f"This is failure #{failure_count} patching {path!r}. "
                    "Stop retrying with variations of the same old_string. "
                    "Either: (1) re-read the file fresh to verify current "
                    "content, (2) use a longer / more unique old_string with "
                    "surrounding context lines, or (3) use write_file to "
                    "replace the entire file if the targeted region is hard "
                    "to anchor."
                )
            elif "Did you mean one of these sections?" not in str(result_dict["error"]):
                result_dict["_hint"] = (
                    "old_string not found. Use read_file to verify the current "
                    "content, or search_files to locate the text."
                )
        return json.dumps(result_dict, ensure_ascii=False)
    except Exception as e:
        return tool_error(str(e))


def search_tool(pattern: str, target: str = "content", path: str = ".",
                file_glob: str = None, limit: int = 50, offset: int = 0,
                output_mode: str = "content", context: int = 0,
                task_id: str = "default") -> str:
    """搜索内容或文件。"""
    try:
        offset, limit = normalize_search_pagination(offset, limit)

        # 追踪搜索以检测【连续】的重复搜索循环。
        # 包含分页参数，使用户能翻阅截断的结果而不会触发重复搜索守卫。
        search_key = (
            "search",
            pattern,
            target,
            str(path),
            file_glob or "",
            limit,
            offset,
        )
        with _read_tracker_lock:
            task_data = _read_tracker.setdefault(task_id, {
                "last_key": None, "consecutive": 0, "read_history": set(),
            })
            if task_data["last_key"] == search_key:
                task_data["consecutive"] += 1
            else:
                task_data["last_key"] = search_key
                task_data["consecutive"] = 1
            count = task_data["consecutive"]

        if count >= 4:
            return json.dumps({
                "error": (
                    f"BLOCKED: You have run this exact search {count} times in a row. "
                    "The results have NOT changed. You already have this information. "
                    "STOP re-searching and proceed with your task."
                ),
                "pattern": pattern,
                "already_searched": count,
            }, ensure_ascii=False)

        file_ops = _get_file_ops(task_id)
        result = file_ops.search(
            pattern=pattern, path=path, target=target, file_glob=file_glob,
            limit=limit, offset=offset, output_mode=output_mode, context=context
        )
        if hasattr(result, 'matches'):
            for m in result.matches:
                if hasattr(m, 'content') and m.content:
                    m.content = redact_sensitive_text(m.content, code_file=True)
        result_dict = result.to_dict(densify=True)

        if count >= 3:
            result_dict["_warning"] = (
                f"You have run this exact search {count} times consecutively. "
                "The results have not changed. Use the information you already have."
            )

        result_json = json.dumps(result_dict, ensure_ascii=False)
        # 结果被截断时的提示——显式给出下一个 offset 比指望模型从
        # total_count 与匹配数中推断更清晰。
        if result_dict.get("truncated"):
            next_offset = offset + limit
            result_json += f"\n\n[Hint: Results truncated. Use offset={next_offset} to see more, or narrow with a more specific pattern or file_glob.]"
        return result_json
    except Exception as e:
        return tool_error(str(e))




# ---------------------------------------------------------------------------
# Schema + 注册表
# ---------------------------------------------------------------------------
from tools.registry import registry, tool_error


def _check_file_reqs():
    """惰性包装，避免与 tools/__init__.py 产生循环导入。"""
    from tools import check_file_requirements
    return check_file_requirements()

READ_FILE_SCHEMA = {
    "name": "read_file",
    "description": "Read a text file with line numbers and pagination. Use this instead of cat/head/tail in terminal. Output format: 'LINE_NUM|CONTENT'. Suggests similar filenames if not found. Use offset and limit for large files. Reads exceeding ~100K characters are rejected; use offset and limit to read specific sections of large files. Jupyter notebooks (.ipynb), Word documents (.docx), and Excel workbooks (.xlsx) are auto-extracted to readable text. NOTE: Cannot read images or other binary files — use vision_analyze for images.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read (absolute, relative, or ~/path)"},
            "offset": {"type": "integer", "description": "Line number to start reading from (1-indexed, default: 1)", "default": 1, "minimum": 1},
            "limit": {"type": "integer", "description": "Maximum number of lines to read (default: 500, max: 2000)", "default": 500, "maximum": 2000}
        },
        "required": ["path"]
    }
}

WRITE_FILE_SCHEMA = {
    "name": "write_file",
    "description": "Write content to a file, completely replacing existing content. Use this instead of echo/cat heredoc in terminal. Creates parent directories automatically. OVERWRITES the entire file — use 'patch' for targeted edits. Auto-runs syntax checks on .py/.json/.yaml/.toml and other linted languages; only NEW errors introduced by this write are surfaced (pre-existing errors are filtered out).",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to write (will be created if it doesn't exist, overwritten if it does)"},
            "content": {"type": "string", "description": "Complete content to write to the file"},
            "cross_profile": {
                "type": "boolean",
                "description": "Opt out of the cross-profile soft guard. Defaults to false. Set true ONLY after explicit user direction to edit another Hermes profile's skills/plugins/cron/memories — by default these writes are blocked with a warning because they affect a different profile than the one this session is running under.",
                "default": False,
            },
        },
        "required": ["path", "content"]
    }
}

PATCH_SCHEMA = {
    "name": "patch",
    "description": (
        "Targeted find-and-replace edits in files. Use this instead of sed/awk in terminal. "
        "Uses fuzzy matching (9 strategies) so minor whitespace/indentation differences won't break it. "
        "Returns a unified diff. Auto-runs syntax checks after editing.\n\n"
        "REPLACE MODE (mode='replace', default): find a unique string and replace it. "
        "REQUIRED PARAMETERS: mode, path, old_string, new_string.\n"
        "PATCH MODE (mode='patch'): apply V4A multi-file patches for bulk changes. "
        "REQUIRED PARAMETERS: mode, patch."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["replace", "patch"],
                "description": "Edit mode. 'replace' (default): requires path + old_string + new_string. 'patch': requires patch content only.",
                "default": "replace",
            },
            "path": {
                "type": "string",
                "description": "REQUIRED when mode='replace'. File path to edit.",
            },
            "old_string": {
                "type": "string",
                "description": "REQUIRED when mode='replace'. Exact text to find and replace. Must be unique in the file unless replace_all=true. Include surrounding context lines to ensure uniqueness.",
            },
            "new_string": {
                "type": "string",
                "description": "REQUIRED when mode='replace'. Replacement text. Pass empty string '' to delete the matched text.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences instead of requiring a unique match (default: false)",
                "default": False,
            },
            "patch": {
                "type": "string",
                "description": "REQUIRED when mode='patch'. V4A format patch content. Format:\n*** Begin Patch\n*** Update File: path/to/file\n@@ context hint @@\n context line\n-removed line\n+added line\n*** End Patch",
            },
            "cross_profile": {
                "type": "boolean",
                "description": "Opt out of the cross-profile soft guard. Defaults to false. Set true ONLY after explicit user direction to edit another Hermes profile's skills/plugins/cron/memories.",
                "default": False,
            },
        },
        "required": ["mode"],
    },
}

SEARCH_FILES_SCHEMA = {
    "name": "search_files",
    "description": "Search file contents or find files by name. Use this instead of grep/rg/find/ls in terminal. Ripgrep-backed, faster than shell equivalents.\n\nContent search (target='content'): Regex search inside files. Output modes: full matches with line numbers, file paths only, or match counts.\n\nFile search (target='files'): Find files by glob pattern (e.g., '*.py', '*config*'). Also use this instead of ls — results sorted by modification time.",
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern for content search, or glob pattern (e.g., '*.py') for file search"},
            "target": {"type": "string", "enum": ["content", "files"], "description": "'content' searches inside file contents, 'files' searches for files by name", "default": "content"},
            "path": {"type": "string", "description": "Directory or file to search in (default: current working directory)", "default": "."},
            "file_glob": {"type": "string", "description": "Filter files by pattern in grep mode (e.g., '*.py' to only search Python files)"},
            "limit": {"type": "integer", "description": "Maximum number of results to return (default: 50)", "default": 50},
            "offset": {"type": "integer", "description": "Skip first N results for pagination (default: 0)", "default": 0},
            "output_mode": {"type": "string", "enum": ["content", "files_only", "count"], "description": "Output format for grep mode: 'content' shows matching lines with line numbers, 'files_only' lists file paths, 'count' shows match counts per file", "default": "content"},
            "context": {"type": "integer", "description": "Number of context lines before and after each match (grep mode only)", "default": 0}
        },
        "required": ["pattern"]
    }
}


def _handle_read_file(args, **kw):
    tid = kw.get("task_id") or "default"
    return read_file_tool(path=args.get("path", ""), offset=args.get("offset", 1), limit=args.get("limit", 500), task_id=tid)


def _handle_write_file(args, **kw):
    tid = kw.get("task_id") or "default"
    if not args.get("path") or not isinstance(args.get("path"), str):
        return tool_error(
            "write_file: missing required field 'path'. Re-emit the tool call with "
            "both 'path' and 'content' set."
        )
    if "content" not in args:
        return tool_error(
            "write_file: missing required field 'content'. The tool call included a "
            "path but no content argument — this is almost always a dropped-arg bug "
            "under context pressure. Re-emit the tool call with the full content "
            "payload, or use execute_code with hermes_tools.write_file() for very "
            "large files."
        )
    if not isinstance(args["content"], str):
        return tool_error(
            f"write_file: 'content' must be a string, got "
            f"{type(args['content']).__name__}."
        )
    return write_file_tool(
        path=args["path"], content=args["content"], task_id=tid,
        cross_profile=bool(args.get("cross_profile", False)),
        session_id=kw.get("session_id"),
    )


def _handle_patch(args, **kw):
    tid = kw.get("task_id") or "default"
    return patch_tool(
        mode=args.get("mode", "replace"), path=args.get("path"),
        old_string=args.get("old_string"), new_string=args.get("new_string"),
        replace_all=args.get("replace_all", False), patch=args.get("patch"), task_id=tid,
        cross_profile=bool(args.get("cross_profile", False)),
        session_id=kw.get("session_id"),
    )


def _handle_search_files(args, **kw):
    tid = kw.get("task_id") or "default"
    target_map = {"grep": "content", "find": "files"}
    raw_target = args.get("target", "content")
    target = target_map.get(raw_target, raw_target)
    return search_tool(
        pattern=args.get("pattern", ""), target=target, path=args.get("path", "."),
        file_glob=args.get("file_glob"), limit=args.get("limit", 50), offset=args.get("offset", 0),
        output_mode=args.get("output_mode", "content"), context=args.get("context", 0), task_id=tid)


registry.register(name="read_file", toolset="file", schema=READ_FILE_SCHEMA, handler=_handle_read_file, check_fn=_check_file_reqs, emoji="📖", max_result_size_chars=100_000)
registry.register(name="write_file", toolset="file", schema=WRITE_FILE_SCHEMA, handler=_handle_write_file, check_fn=_check_file_reqs, emoji="✍️", max_result_size_chars=100_000)
registry.register(name="patch", toolset="file", schema=PATCH_SCHEMA, handler=_handle_patch, check_fn=_check_file_reqs, emoji="🔧", max_result_size_chars=100_000)
registry.register(name="search_files", toolset="file", schema=SEARCH_FILES_SCHEMA, handler=_handle_search_files, check_fn=_check_file_reqs, emoji="🔎", max_result_size_chars=100_000)
