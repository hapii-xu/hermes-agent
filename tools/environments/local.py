"""本地执行环境 —— 每次调用重新派生进程，并保留会话快照。"""

import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from tools.environments.base import BaseEnvironment, _pipe_stdin
from hermes_cli._subprocess_compat import windows_hide_flags

_IS_WINDOWS = platform.system() == "Windows"

logger = logging.getLogger(__name__)


def _msys_to_windows_path(cwd: str) -> str:
    """把 Git Bash / MSYS 风格的 POSIX 路径（``/c/Users/x``）转换成原生
    Windows 形式（``C:\\Users\\x``），以便 ``os.path.isdir`` 和
    ``subprocess.Popen(..., cwd=...)`` 能够找到它。

    在非 Windows 主机上、或路径不是 MSYS 形式时为无操作。
    当无需转换时原样返回输入。该操作是幂等的 —— 对一个已是 Windows 形式
    的路径调用本函数会原样返回。
    """
    if not _IS_WINDOWS or not cwd:
        return cwd
    # 匹配开头的 "/<单字母>/" 或恰好 "/<字母>"（裸盘符根）。
    m = re.match(r'^/([a-zA-Z])(/.*)?$', cwd)
    if not m:
        return cwd
    drive = m.group(1).upper()
    tail = (m.group(2) or "").replace('/', '\\')
    return f"{drive}:{tail or chr(92)}"  # chr(92) = 反斜杠，避免原始字符串转义


def _resolve_safe_cwd(cwd: str) -> str:
    """如果 ``cwd`` 作为目录存在则返回它，否则返回最近存在的祖先目录。
    只有当向上回溯路径也找不到任何存在的目录时，才回退到
    ``tempfile.gettempdir()``（在健康的文件系统上实际上永远不会发生，但
    这是一个廉价的双重保险）。

    在 Windows 上，还会在 isdir 检查之前把 Git Bash / MSYS 风格的 POSIX
    路径（``/c/Users/x``）归一化为原生 Windows 形式，以免 bash 返回的一个
    完全合法的 ``pwd -P`` 结果被当作“缺失”而拒绝（见
    ``_msys_to_windows_path``）。

    供 ``_run_bash`` 在配置的 cwd 已不存在时做恢复 —— 最常见的情况是上一个
    工具调用删除了它自己的工作目录（issue #17558）。没有这个守卫，
    ``subprocess.Popen(..., cwd=...)`` 会在 bash 启动之前抛出
    ``FileNotFoundError``，卡死后续每一次终端调用，直到网关重启。
    """
    cwd = _msys_to_windows_path(cwd) if _IS_WINDOWS else cwd
    if cwd and os.path.isdir(cwd):
        return cwd
    parent = os.path.dirname(cwd) if cwd else ""
    while parent:
        if os.path.isdir(parent):
            return parent
        next_parent = os.path.dirname(parent)
        if next_parent == parent:
            # 已到达文件系统根目录且它也不存在 —— 确实除了临时目录之外没有
            # 别的回退选择了。
            break
        parent = next_parent
    return tempfile.gettempdir()


# 不应泄漏到终端子进程中的 Hermes 内部环境变量。
_HERMES_PROVIDER_ENV_FORCE_PREFIX = "_HERMES_FORCE_"

# Hermes 托管的 AWS *推理*凭证，用于 ``auth_type="aws_sdk"`` 的提供方
# （Bedrock）。作用域被故意收窄：这里只列出 Bedrock 专用的 bearer token，
# 它是一个 Hermes 推理密钥，与 ``OPENAI_API_KEY`` 完全类似 —— 没有人拿它去
# 驱动 ``aws``/``terraform``/``boto3`` 工具链，所以从 terminal/execute_code
# 子进程里剥离它不会损失任何用户能力。
#
# 通用的 AWS 凭证链（AWS_ACCESS_KEY_ID、AWS_SECRET_ACCESS_KEY、
# AWS_SESSION_TOKEN、AWS_PROFILE 以及配置/角色指针）被有意保留为可继承。
# 依据 SECURITY.md §3.2，本地终端是用户的可信运维 shell；agent 拥有与用户
# 自己 shell 相同的通用 AWS 访问权限是预期姿态，而非泄漏。把这些变量硬拉进
# 黑名单会（a）让每个在 agent 终端里运行 aws/terraform/cdk/boto3 的用户都
# 出现回归 —— 不止 Bedrock 用户，因为注册表是无条件遍历的 —— 而且（b）
# 无法恢复，因为 env_passthrough.py 拒绝重新放行这个黑名单里的任何内容
# （GHSA-rhgp-j443-p4rf）。参见 issue #32314 的讨论。
_AWS_SDK_CREDENTIAL_ENV_VARS = frozenset({
    "AWS_BEARER_TOKEN_BEDROCK",
})


def _build_provider_env_blocklist() -> frozenset:
    """从 provider、tool 和 gateway 配置中派生出黑名单。"""
    blocked: set[str] = set()

    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
        for pconfig in PROVIDER_REGISTRY.values():
            blocked.update(pconfig.api_key_env_vars)
            if pconfig.auth_type == "aws_sdk":
                blocked.update(_AWS_SDK_CREDENTIAL_ENV_VARS)
            if pconfig.base_url_env_var:
                blocked.add(pconfig.base_url_env_var)
    except ImportError:
        pass

    try:
        from hermes_cli.config import OPTIONAL_ENV_VARS
        for name, metadata in OPTIONAL_ENV_VARS.items():
            category = metadata.get("category")
            if category in {"tool", "messaging"}:
                blocked.add(name)
            elif category == "setting" and metadata.get("password"):
                blocked.add(name)
    except ImportError:
        pass

    blocked.update({
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_ORG_ID",
        "OPENAI_ORGANIZATION",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "LLM_MODEL",
        "GOOGLE_API_KEY",
        "DEEPSEEK_API_KEY",
        "MISTRAL_API_KEY",
        "GROQ_API_KEY",
        "TOGETHER_API_KEY",
        "PERPLEXITY_API_KEY",
        "COHERE_API_KEY",
        "FIREWORKS_API_KEY",
        "XAI_API_KEY",
        "HELICONE_API_KEY",
        "PARALLEL_API_KEY",
        "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL",
        "TELEGRAM_HOME_CHANNEL",
        "TELEGRAM_HOME_CHANNEL_NAME",
        "DISCORD_HOME_CHANNEL",
        "DISCORD_HOME_CHANNEL_NAME",
        "DISCORD_REQUIRE_MENTION",
        "DISCORD_FREE_RESPONSE_CHANNELS",
        "DISCORD_AUTO_THREAD",
        "SLACK_HOME_CHANNEL",
        "SLACK_HOME_CHANNEL_NAME",
        "SLACK_ALLOWED_USERS",
        "WHATSAPP_ENABLED",
        "WHATSAPP_MODE",
        "WHATSAPP_ALLOWED_USERS",
        "SIGNAL_HTTP_URL",
        "SIGNAL_ACCOUNT",
        "SIGNAL_ALLOWED_USERS",
        "SIGNAL_GROUP_ALLOWED_USERS",
        "SIGNAL_HOME_CHANNEL",
        "SIGNAL_HOME_CHANNEL_NAME",
        "SIGNAL_IGNORE_STORIES",
        "HASS_TOKEN",
        "HASS_URL",
        "EMAIL_ADDRESS",
        "EMAIL_PASSWORD",
        "EMAIL_IMAP_HOST",
        "EMAIL_SMTP_HOST",
        "EMAIL_HOME_ADDRESS",
        "EMAIL_HOME_ADDRESS_NAME",
        "HERMES_DASHBOARD_SESSION_TOKEN",
        "GATEWAY_ALLOWED_USERS",
        "GH_TOKEN",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_INSTALLATION_ID",
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "DAYTONA_API_KEY",
    })
    return frozenset(blocked)


_HERMES_PROVIDER_ENV_BLOCKLIST = _build_provider_env_blocklist()


def _inject_context_hermes_home(env: dict) -> None:
    """把上下文本地的 Hermes home 覆盖值桥接到子进程环境中。"""
    try:
        from hermes_constants import get_hermes_home_override

        value = get_hermes_home_override()
        if value:
            env["HERMES_HOME"] = value
    except Exception:
        pass


def _sanitize_subprocess_env(base_env: dict | None, extra_env: dict | None = None) -> dict:
    """从子进程环境中过滤掉 Hermes 托管的密钥。"""
    try:
        from tools.env_passthrough import is_env_passthrough as _is_passthrough
    except Exception:
        _is_passthrough = lambda _: False  # noqa: E731

    sanitized: dict[str, str] = {}

    for key, value in (base_env or {}).items():
        if key.startswith(_HERMES_PROVIDER_ENV_FORCE_PREFIX):
            continue
        if key not in _HERMES_PROVIDER_ENV_BLOCKLIST or _is_passthrough(key):
            sanitized[key] = value

    for key, value in (extra_env or {}).items():
        if key.startswith(_HERMES_PROVIDER_ENV_FORCE_PREFIX):
            real_key = key[len(_HERMES_PROVIDER_ENV_FORCE_PREFIX):]
            sanitized[real_key] = value
        elif key not in _HERMES_PROVIDER_ENV_BLOCKLIST or _is_passthrough(key):
            sanitized[key] = value

    _inject_context_hermes_home(sanitized)

    from hermes_constants import apply_subprocess_home_env
    apply_subprocess_home_env(sanitized)

    return sanitized


def _find_bash() -> str:
    """为命令执行查找 bash。"""
    if not _IS_WINDOWS:
        return (
            shutil.which("bash")
            or ("/usr/bin/bash" if os.path.isfile("/usr/bin/bash") else None)
            or ("/bin/bash" if os.path.isfile("/bin/bash") else None)
            or os.environ.get("SHELL")
            or "/bin/sh"
        )

    custom = os.environ.get("HERMES_GIT_BASH_PATH")
    if custom and os.path.isfile(custom):
        return custom

    # 优先使用我们自带的便携版 Git —— 这样一个损坏或被部分卸载的系统 Git
    # 就无法劫持 bash 查找。当用户原本没有可用的系统 Git 时，
    # install.ps1 安装器总会把便携版 Git 放在这里。
    #
    # 布局（两种都检查，以便 MinGit 和 PortableGit 之间的升级能透明工作）：
    #   PortableGit: %LOCALAPPDATA%\hermes\git\bin\bash.exe   （主选）
    #   MinGit:      %LOCALAPPDATA%\hermes\git\usr\bin\bash.exe （遗留/32 位回退）
    _local_appdata = os.environ.get("LOCALAPPDATA", "")
    _hermes_portable_git = os.path.join(_local_appdata, "hermes", "git") if _local_appdata else ""
    if _hermes_portable_git:
        for candidate in (
            os.path.join(_hermes_portable_git, "bin", "bash.exe"),        # PortableGit（主选）
            os.path.join(_hermes_portable_git, "usr", "bin", "bash.exe"), # MinGit 回退
        ):
            if os.path.isfile(candidate):
                return candidate

    found = shutil.which("bash")
    if found:
        return found

    for candidate in (
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "bin", "bash.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Git", "bin", "bash.exe"),
        os.path.join(_local_appdata, "Programs", "Git", "bin", "bash.exe"),
    ):
        if candidate and os.path.isfile(candidate):
            return candidate

    raise RuntimeError(
        "Git Bash not found. Hermes Agent requires Git for Windows on Windows.\n"
        "Install it from: https://git-scm.com/download/win\n"
        "Or set HERMES_GIT_BASH_PATH to your bash.exe location."
    )


# 向后兼容 —— process_registry.py 导入这个名字
_find_shell = _find_bash


# 用于 PATH 极简的环境的标准 PATH 条目。
_SANE_PATH = (
    "/opt/homebrew/bin:/opt/homebrew/sbin:"
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)

# 缓存包含 ``hermes`` 控制台脚本的目录。
# ``_SENTINEL`` 用来区分“尚未解析”和已解析为 ``None``。
_SENTINEL = object()
_HERMES_BIN_DIR: "str | None | object" = _SENTINEL


def _resolve_hermes_bin_dir() -> str | None:
    """返回存放 ``hermes`` 控制台脚本的目录，或 None。

    终端工具运行在一个全新派生的子 shell 中，其 PATH 是 agent 进程的 PATH
    加上一组静态系统目录（``_SANE_PATH``）。当网关由不读取用户 shell rc 的
    东西启动时 —— systemd、服务管理器、桌面启动器、cron —— hermes 的安装目录
    （``~/.local/bin``、venv 的 ``bin``/``Scripts``、pipx、nix）不在该 PATH
    里，于是通过终端工具 shell 调用裸 ``hermes`` 的插件会命中
    ``command not found``（exit 127），尽管 ``hermes`` 在用户自己的交互式
    终端里工作正常。

    我们解析一次安装目录（它在单个进程内永不变），并在缺失时把它前置到子
    shell 的 PATH，这样无论网关如何启动，裸 ``hermes`` 都能被解析到。

    解析顺序（廉价，无重型导入）：
      1. ``shutil.which("hermes")`` —— 正常 PATH 安装的 shim。
      2. 当 ``sys.argv[0]`` 是指向真实 ``hermes`` 可执行文件的绝对路径时，
         取其所在目录（覆盖 nix-store / venv 包装器）。
      3. ``sys.executable`` 所在目录 —— 正在运行的解释器所属 venv 的
         ``bin``/``Scripts`` 正是控制台脚本所在之处。
    """
    global _HERMES_BIN_DIR
    if _HERMES_BIN_DIR is not _SENTINEL:
        return _HERMES_BIN_DIR  # type: ignore[return-value]

    candidate: str | None = None

    which = shutil.which("hermes")
    if which:
        candidate = os.path.dirname(which)

    if candidate is None:
        argv0 = sys.argv[0] if sys.argv else ""
        base = os.path.basename(argv0).lower()
        if (
            os.path.isabs(argv0)
            and (base == "hermes" or base.startswith("hermes."))
            and os.path.isfile(argv0)
        ):
            candidate = os.path.dirname(argv0)

    if candidate is None:
        exe_dir = os.path.dirname(sys.executable) if sys.executable else ""
        if exe_dir:
            shim = "hermes.exe" if _IS_WINDOWS else "hermes"
            if os.path.isfile(os.path.join(exe_dir, shim)):
                candidate = exe_dir

    if candidate and not os.path.isdir(candidate):
        candidate = None

    _HERMES_BIN_DIR = candidate
    return candidate


def _prepend_hermes_bin_dir(existing_path: str) -> str:
    """如果缺失，则把 hermes 安装目录前置到 ``existing_path``。

    跨平台（使用 ``os.pathsep``）。首次出现优先，所以已包含该目录的 PATH
    会原样返回。当无法解析安装目录时原样返回输入。
    """
    bin_dir = _resolve_hermes_bin_dir()
    if not bin_dir:
        return existing_path
    sep = os.pathsep
    entries = [e for e in existing_path.split(sep) if e] if existing_path else []
    if bin_dir in entries:
        return existing_path
    return sep.join([bin_dir, *entries])


def _append_missing_sane_path_entries(existing_path: str) -> str:
    """返回一个归一化后的 POSIX PATH，并把缺失的合理条目追加到末尾。

    在 POSIX 上，调用方提供的 PATH 会被重写（而不只是追加）：空条目和重复
    条目被丢弃，保留首次出现顺序，然后每个缺失的 ``_SANE_PATH`` 条目在末尾
    各追加一次，使已有条目保持原有优先级。

    除了“添加 Homebrew 目录”这一基本修复之外，还有两处刻意的归一化：

    - **空条目被剥离。** 开头/结尾/连续的 ``:`` 表示一个空 PATH 元素，POSIX
      shell 会把它解释为当前工作目录 —— 在默认终端环境里是个小小的坑。我们
      直接丢弃它们，而不是带过去。
    - **重复被合并**（首次出现优先），所以已经包含重复的调用方 PATH 不会
      被原样传播。

    对于格式良好的 PATH（无空条目、无重复），开头部分与输入逐字节相同，顺序
    也被保留；只有缺失的合理条目被追加。在 Windows 上这是一个无操作的透传
    （分隔符是 ``;``，原生 PATH 不能被改动）。
    """
    if _IS_WINDOWS:
        return existing_path

    sane_entries = [entry for entry in _SANE_PATH.split(":") if entry]
    if not existing_path:
        return ":".join(sane_entries)

    # 在合并合理回退条目之前，对调用方 PATH 去重（首次出现优先）并丢弃空条目。
    seen: set[str] = set()
    ordered_entries: list[str] = []
    for entry in existing_path.split(":"):
        if not entry or entry in seen:
            continue
        seen.add(entry)
        ordered_entries.append(entry)

    # _SANE_PATH 是静态、无重复的常量，所以对调用方条目做成员检查就够了
    # —— 这里无需再跟踪 `seen`。
    for entry in sane_entries:
        if entry not in seen:
            ordered_entries.append(entry)

    return ":".join(ordered_entries)


def _path_env_key(run_env: dict) -> str | None:
    """返回要更新的 PATH 环境变量键，且不改变 Windows 大小写。

    注意：这是刻意的*第二道* Windows 守卫，与 ``_append_missing_sane_path_entries``
    中的早返回不同。它的职责是挑选大小写正确的键（``Path`` vs ``PATH``），
    以便补全时写回到调用方已使用的键；而辅助函数里的守卫只是让该辅助函数
    可以安全地独立调用（例如在 Windows 单元测试里）。两者都是刻意的。
    """
    if not _IS_WINDOWS:
        return "PATH"
    for key in run_env:
        if key.upper() == "PATH":
            return key
    return None


def _make_run_env(env: dict) -> dict:
    """构建一个具备合理 PATH 并剥离了 provider 变量的运行环境。"""
    try:
        from tools.env_passthrough import is_env_passthrough as _is_passthrough
    except Exception:
        _is_passthrough = lambda _: False  # noqa: E731

    merged = dict(os.environ | env)
    run_env = {}
    for k, v in merged.items():
        if k.startswith(_HERMES_PROVIDER_ENV_FORCE_PREFIX):
            real_key = k[len(_HERMES_PROVIDER_ENV_FORCE_PREFIX):]
            run_env[real_key] = v
        elif k not in _HERMES_PROVIDER_ENV_BLOCKLIST or _is_passthrough(k):
            run_env[k] = v
    path_key = _path_env_key(run_env)
    if path_key is not None:
        new_path = _append_missing_sane_path_entries(run_env.get(path_key, ""))
        # 确保 hermes 安装目录可达，这样即使网关启动时 PATH 里没有它
        # （systemd、服务管理器、cron 等），插件仍能通过终端工具 shell 调用
        # 裸 ``hermes``。
        run_env[path_key] = _prepend_hermes_bin_dir(new_path)

    _inject_context_hermes_home(run_env)

    from hermes_constants import apply_subprocess_home_env
    apply_subprocess_home_env(run_env)

    # 把基于 ContextVar 的会话变量注入到子进程环境中。
    # ContextVar 不会传播到子进程，所以在这里桥接它们。
    try:
        from gateway.session_context import _UNSET, _VAR_MAP
        for var_name, var in _VAR_MAP.items():
            value = var.get()
            if value is not _UNSET and value:
                run_env[var_name] = value
    except Exception:
        pass

    return run_env


def _read_terminal_shell_init_config() -> tuple[list[str], bool]:
    """从 config.yaml 返回 (shell_init_files, auto_source_bashrc)。

    尽力而为 —— 任何失败时都返回合理的默认值，这样终端执行绝不会因为配置
    文件不可读而中断。
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        terminal_cfg = cfg.get("terminal") or {}
        files = terminal_cfg.get("shell_init_files") or []
        if not isinstance(files, list):
            files = []
        auto_bashrc = bool(terminal_cfg.get("auto_source_bashrc", True))
        return [str(f) for f in files if f], auto_bashrc
    except Exception:
        return [], True


def _resolve_shell_init_files() -> list[str]:
    """解析在登录 shell 快照之前要 source 的文件列表。

    展开 ``~`` 和 ``${VAR}`` 引用，并丢弃磁盘上不存在的文件，这样缺失的
    ``~/.bashrc`` 永远不会破坏快照。``auto_source_bashrc`` 路径只在用户未提供
    显式列表时才运行 —— 一旦他们提供了，Hermes 就信任他们。
    """
    explicit, auto_bashrc = _read_terminal_shell_init_config()

    candidates: list[str] = []
    if explicit:
        candidates.extend(explicit)
    elif auto_bashrc and not _IS_WINDOWS:
        # 构建一个类似登录 shell 的 source 列表，让那些自安装到用户 shell rc
        # 里的工具（n / nvm / asdf / pyenv）出现在捕获快照的 PATH 上。
        #
        # ~/.profile 和 ~/.bash_profile 最先运行，因为它们没有交互守卫 ——
        # 像 ``n`` 和 ``nvm`` 这样的安装器在大多数发行版上会把 PATH 导出追加
        # 到这里，而非交互式的 ``. ~/.profile`` 能捕获到它。
        #
        # ~/.bashrc 最后运行。在 Debian/Ubuntu 上，默认 bashrc 以
        # ``case $- in *i*) ;; *) return;; esac`` 开头，非交互式 source 时会
        # 提前退出，这正是只 source bashrc 会漏掉放在该守卫之下的 nvm/n
        # PATH 添加的原因。我们仍然包含它，以便把 PATH 逻辑写在 bashrc 里
        # （并且去掉了守卫，或本来就没有守卫）的用户继续可用。
        candidates.extend(["~/.profile", "~/.bash_profile", "~/.bashrc"])

    resolved: list[str] = []
    for raw in candidates:
        try:
            path = os.path.expandvars(os.path.expanduser(raw))
        except Exception:
            continue
        if path and os.path.isfile(path):
            resolved.append(path)
    return resolved


def _prepend_shell_init(cmd_string: str, files: list[str]) -> str:
    """把 ``source <file>`` 行（带守卫且静默）前置到一段 bash 脚本。

    每个文件都被包了一层，这样一个失败的 rc 文件不会终止整个引导过程：
    ``set +e`` 在出错时继续，``2>/dev/null`` 隐藏嘈杂提示，``|| true`` 中和
    退出状态。
    """
    if not files:
        return cmd_string

    prelude_parts = ["set +e"]
    for path in files:
        # 这里没有 import 时 shlex.quote 不可用；文件列表来自
        # os.path.expanduser 的输出，所以是具体的绝对路径。仍然防御性地转义
        # 单引号。
        safe = path.replace("'", "'\\''")
        prelude_parts.append(f"[ -r '{safe}' ] && . '{safe}' 2>/dev/null || true")
    prelude = "\n".join(prelude_parts) + "\n"
    return prelude + cmd_string


class LocalEnvironment(BaseEnvironment):
    """直接在宿主机上运行命令。

    每次调用重新派生：每次 execute() 都派生一个全新的 bash 进程。
    会话快照在调用之间保留环境变量。
    CWD 通过每次命令后的基于文件的读取来持久化。
    """

    def __init__(self, cwd: str = "", timeout: int = 60, env: dict = None):
        if cwd:
            cwd = os.path.expanduser(cwd)
        super().__init__(cwd=cwd or os.getcwd(), timeout=timeout, env=env)
        self.init_session()

    def get_temp_dir(self) -> str:
        """为本地执行返回一个对 shell 安全、可写的临时目录。

        Termux 默认不提供 /tmp，但暴露一个 POSIX TMPDIR。优先使用可用的
        POSIX 风格环境变量，在常规 Unix 系统上继续使用 /tmp，并仅当
        tempfile.gettempdir() 也解析为 POSIX 路径时才回退到它。

        先检查为此后端配置的环境，这样调用方可以显式覆盖临时根目录（例如
        通过 terminal.env 或自定义 TMPDIR），然后再回退到宿主进程环境。

        **Windows：** 硬编码 ``/tmp`` 有两方面错误 —— 原生 Python 无法打开
        该路径，而且 Windows 默认临时目录（``%TEMP%``）常常包含空格
        （``C:\\Users\\Some Name\\AppData\\Local\\Temp``），会破坏未加引号的
        bash 插值。改用 ``HERMES_HOME`` 下的专用缓存目录 —— 单词路径、保证
        存在、同一字符串在 Git Bash 和原生 Python 中都能解析。
        """
        if _IS_WINDOWS:
            # 在 HERMES_HOME 下派生一个对 Windows 安全的临时目录。使用
            # 正斜杠使同一字符串在 bash 命令插值和 Python ``open()`` 中都
            # 不变可用 —— Windows 文件系统路径接受正斜杠，并且路径由我们
            # 控制，可以保证不含空格。
            try:
                from hermes_constants import get_hermes_home
                cache_dir = get_hermes_home() / "cache" / "terminal"
            except Exception:
                cache_dir = Path(tempfile.gettempdir()) / "hermes_terminal"
            cache_dir.mkdir(parents=True, exist_ok=True)
            # 强制使用正斜杠，使同一字符串在两种上下文中都可用。
            return str(cache_dir).replace("\\", "/")

        for env_var in ("TMPDIR", "TMP", "TEMP"):
            candidate = self.env.get(env_var) or os.environ.get(env_var)
            if candidate and candidate.startswith("/"):
                return candidate.rstrip("/") or "/"

        if os.path.isdir("/tmp") and os.access("/tmp", os.W_OK | os.X_OK):
            return "/tmp"

        candidate = tempfile.gettempdir()
        if candidate.startswith("/"):
            return candidate.rstrip("/") or "/"

        return "/tmp"

    def _run_bash(self, cmd_string: str, *, login: bool = False,
                  timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        bash = _find_bash()
        # 对于登录 shell 调用（被 init_session 用来构建环境快照），前置
        # 用户 bashrc / 自定义初始化文件的 source，使那些在 bash_profile
        # 之外注册的工具（nvm、asdf、pyenv……）最终出现在捕获快照的 PATH 上。
        # 非登录调用已经在 source 快照，不需要这一步。
        if login:
            init_files = _resolve_shell_init_files()
            if init_files:
                cmd_string = _prepend_shell_init(cmd_string, init_files)
        args = [bash, "-l", "-c", cmd_string] if login else [bash, "-c", cmd_string]
        run_env = _make_run_env(self.env)

        # 当 cwd 被从我们脚下删除时做恢复 —— 通常是因为上一个工具调用对自己
        # 的工作目录跑了 ``rm -rf``（issue #17558）。否则 Popen 会在 bash 启动
        # 之前因 cwd 抛出 FileNotFoundError，卡死后续每次调用，直到网关重启。
        #
        # 在 Windows 上，``_resolve_safe_cwd`` 还会把 Git Bash 风格的 POSIX
        # 路径（``/c/Users/...``）归一化为原生形式，以免 bash 返回的一个完全
        # 合法的 ``pwd -P`` 结果被误判为“缺失”，并在每条命令上都报警。
        safe_cwd = _resolve_safe_cwd(self.cwd)
        if safe_cwd != self.cwd:
            # 仅 MSYS → Windows 的翻译不应表现为一条警告（它是良性归一化，
            # 不是恢复）。只有当目录在磁盘上确实不存在时才报警。
            normalized = _msys_to_windows_path(self.cwd) if _IS_WINDOWS else self.cwd
            if safe_cwd != normalized:
                logger.warning(
                    "LocalEnvironment cwd %r is missing on disk; "
                    "falling back to %r so terminal commands keep working.",
                    self.cwd,
                    safe_cwd,
                )
            self.cwd = safe_cwd

        _popen_cwd = self.cwd

        _popen_kwargs = {"creationflags": windows_hide_flags()} if _IS_WINDOWS else {}

        proc = subprocess.Popen(
            args,
            text=True,
            env=run_env,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            preexec_fn=None if _IS_WINDOWS else os.setsid,
            cwd=_popen_cwd,
            **_popen_kwargs,
        )
        if not _IS_WINDOWS:
            try:
                proc._hermes_pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                pass

        if stdin_data is not None:
            _pipe_stdin(proc, stdin_data)

        return proc

    def _kill_process(self, proc):
        """杀死整个进程组（所有子进程）。"""

        def _group_alive(pgid: int) -> bool:
            try:
                # 仅 POSIX：_IS_WINDOWS 在使用本辅助函数之前已被处理。
                os.killpg(pgid, 0)  # windows-footgun: ok —— POSIX 进程组存活探测
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                # 该组存在，即使本进程无法向它发信号。
                return True

        def _wait_for_group_exit(pgid: int, timeout: float) -> bool:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                # 及时回收包装进程。一个已死但未回收的组长进程仍会让
                # killpg(pgid, 0) 报告该组存活。
                try:
                    proc.poll()
                except Exception:
                    pass
                if not _group_alive(pgid):
                    return True
                time.sleep(0.05)
            try:
                proc.poll()
            except Exception:
                pass
            return not _group_alive(pgid)

        try:
            if _IS_WINDOWS:
                proc.terminate()
            else:
                try:
                    pgid = os.getpgid(proc.pid)
                except ProcessLookupError:
                    pgid = getattr(proc, "_hermes_pgid", None)
                    if pgid is None:
                        raise

                try:
                    os.killpg(pgid, signal.SIGTERM)  # windows-footgun: ok —— POSIX 进程组 SIGTERM（上面已由 _IS_WINDOWS 守卫）
                except ProcessLookupError:
                    return

                # 等待进程组，而不只是 shell 包装进程。在负载下包装进程可能在
                # 孙进程之前退出；在那时返回会留下孤立的进程组成员。
                if _wait_for_group_exit(pgid, 1.0):
                    return

                try:
                    # 仅 POSIX：_IS_WINDOWS 已由外层分支处理。
                    os.killpg(pgid, signal.SIGKILL)  # windows-footgun: ok —— POSIX 进程组 SIGKILL
                except ProcessLookupError:
                    return
                _wait_for_group_exit(pgid, 2.0)
                try:
                    proc.wait(timeout=0.2)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except Exception:
                pass

    def _update_cwd(self, result: dict):
        """从临时文件读取 CWD（仅本地，无需往返）。

        当路径不再是存在的目录时跳过赋值 —— 对已删除 cwd 跑 ``pwd -P``
        可能在标记文件里留下过期值，传播它会让下一次 ``Popen`` 再次卡死。
        如有需要，``_run_bash`` 的恢复路径会解析出一个安全回退。

        在 Windows 上，Git Bash 的 ``pwd -P`` 写入的值是 MSYS 形式
        （``/c/Users/x``）。在用 ``os.path.isdir`` 校验并存入 ``self.cwd``
        之前先把它翻译成原生 Windows 形式；否则 isdir 检查会拒绝每一个合法
        结果，而 ``_run_bash`` 之后会在每条命令上打印一条误导性的“cwd 缺失”
        警告。
        """
        try:
            with open(self._cwd_file, encoding="utf-8") as f:
                cwd_path = f.read().strip()
            if _IS_WINDOWS:
                cwd_path = _msys_to_windows_path(cwd_path)
            if cwd_path and os.path.isdir(cwd_path):
                self.cwd = cwd_path
        except (OSError, FileNotFoundError):
            pass

        # 仍然从输出里剥离标记，使其不可见
        self._extract_cwd_from_output(result)

    def _extract_cwd_from_output(self, result: dict):
        """语义与基类相同，但在 Windows 上 Git Bash 内 ``pwd -P`` 输出的
        值是 MSYS 形式（``/c/Users/x``）。在赋值给 ``self.cwd`` 之前先归一化
        为原生 Windows 形式并校验目录存在 —— 否则 ``_run_bash`` 的安全 cwd
        恢复会在后续每条命令上都报警。

        始终把从 ``result["output"]`` 中剥离标记文本的工作交给基类，使输出
        格式保持一致。
        """
        # 快照已存在的 cwd，交给基类做解析 + 标记剥离，然后校验/归一化它
        # 赋予的任何值。
        prev_cwd = self.cwd
        super()._extract_cwd_from_output(result)
        if self.cwd != prev_cwd:
            normalized = _msys_to_windows_path(self.cwd) if _IS_WINDOWS else self.cwd
            if normalized and os.path.isdir(normalized):
                self.cwd = normalized
            else:
                # 过期 / 不存在的路径 —— 保留之前的 cwd；如有需要
                # _run_bash 会在下次调用时解析出一个安全回退。
                self.cwd = prev_cwd

    def cleanup(self):
        """清理临时文件。"""
        for f in (self._snapshot_path, self._cwd_file):
            try:
                os.unlink(f)
            except OSError:
                pass
