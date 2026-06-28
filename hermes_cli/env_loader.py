"""在所有入口点间一致加载 Hermes .env 文件的辅助工具。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from utils import atomic_replace


# 表示凭据值的环境变量名后缀。这些是我们唯一在加载时
# 会对值进行清理的环境变量——我们不能静默修改任意用户
# 环境变量，但凭据已知需要纯 ASCII（它们会成为 HTTP header 值）。
_CREDENTIAL_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_KEY")

# 在当前进程中已警告过的键名，这样重复调用
# load_hermes_dotenv()（用户 env + 项目 env、gateway 热重载、
# 测试）不会多次重复显示相同的警告。
_WARNED_KEYS: set[str] = set()

# 环境变量名 → 来源标签（"bitwarden" 等）的映射，记录在
# load_hermes_dotenv() 期间由外部密钥源注入的凭据。
# 由 setup / `hermes model` 流程用于标记检测到的凭据，
# 使用户了解密钥的来源——当 .env 中不直接包含该密钥时
# （否则 "credentials detected ✓" 一行看起来与 .env 情况相同，
# 用户不知道 Bitwarden 已连接）。
_SECRET_SOURCES: dict[str, str] = {}

# 在当前进程中已从中拉取过外部密钥的 HERMES_HOME 路径。
# ``load_hermes_dotenv()`` 在多个热模块（cli.py、hermes_cli/main.py、
# run_agent.py、trajectory_compressor.py、gateway/run.py 等）的模块导入
# 时调用，因此如果没有此保护，Bitwarden 状态行会在每次启动时打印 3-5 次。
# Bitwarden 自身的进程内缓存防止了冗余网络调用，但打印、配置重新解析
# 和 ASCII 清理扫描每次仍会执行。
_APPLIED_HOMES: set[str] = set()


def get_secret_source(env_var: str) -> str | None:
    """返回提供 ``env_var`` 的密钥来源标签（如果有）。

    对在当前进程的 ``load_hermes_dotenv()`` 调用中从
    Bitwarden Secrets Manager 拉取的键返回 ``"bitwarden"``。
    对来自 ``.env``、shell 环境或未跟踪的键返回 ``None``。
    返回的标签仅为元数据：凭据池持久化可能会存储它来解释
    借用密钥的来源，但绝不能将其视为持久化原始值的授权。
    """
    return _SECRET_SOURCES.get(env_var)


def reset_secret_source_cache() -> None:
    """忘记哪些 HERMES_HOME 路径已应用过外部密钥。

    在进程中首次调用 ``_apply_external_secret_sources(home_path)`` 时
    会从 Bitwarden（或其他已配置的后端）拉取，将已应用的键记录到
    ``_SECRET_SOURCES`` 中，并记住 ``home_path``，以便同一进程中
    后续调用为 no-op。调用此函数强制下次重新拉取——用于测试以及
    配置变更后需要刷新的长时间运行进程。
    """
    _APPLIED_HOMES.clear()


def format_secret_source_suffix(env_var: str) -> str:
    """返回人类可读的后缀，如 ``" (from Bitwarden)"`` 或 ``""``。

    在打印检测到的凭据时使用此项，使用户可以看到来源。
    当凭据来自 ``.env`` 或 shell 时返回空字符串——
    这些是用户已经了解的隐式/"默认"情况。
    """
    source = get_secret_source(env_var)
    if not source:
        return ""
    if source == "bitwarden":
        return " (from Bitwarden)"
    # 通用回退——为额外的密钥来源（如 1Password、
    # HashiCorp Vault）做前瞻性设计，无需更新每个调用点。
    return f" (from {source})"


def _format_offending_chars(value: str, limit: int = 3) -> str:
    """返回非 ASCII 码位的紧凑 'U+XXXX ('c'), ...' 摘要。"""
    seen: list[str] = []
    for ch in value:
        if ord(ch) > 127:
            label = f"U+{ord(ch):04X}"
            if ch.isprintable():
                label += f" ({ch!r})"
            if label not in seen:
                seen.append(label)
            if len(seen) >= limit:
                break
    return ", ".join(seen)


def _sanitize_loaded_credentials() -> None:
    """从 os.environ 中的凭据环境变量中去除非 ASCII 字符。

    在 dotenv 加载后调用，使代码库的其余部分永远不会遇到
    非 ASCII 的 API 密钥。仅修改名称以已知凭据后缀结尾的
    环境变量（``_API_KEY``、``_TOKEN`` 等）。

    当字符被去除时，向 stderr 输出一行警告。静默去除会掩盖
    复制粘贴损坏（来自 PDF / 富文本编辑器的 Unicode 形似字符、
    来自网页的零宽空格），表现为 provider 端不透明的
    "invalid API key" 错误（参见 #6843）。
    """
    for key, value in list(os.environ.items()):
        if not any(key.endswith(suffix) for suffix in _CREDENTIAL_SUFFIXES):
            continue
        try:
            value.encode("ascii")
            continue
        except UnicodeEncodeError:
            pass
        cleaned = value.encode("ascii", errors="ignore").decode("ascii")
        os.environ[key] = cleaned
        if key in _WARNED_KEYS:
            continue
        _WARNED_KEYS.add(key)
        stripped = len(value) - len(cleaned)
        detail = _format_offending_chars(value) or "non-printable"
        print(
            f"  Warning: {key} contained {stripped} non-ASCII character"
            f"{'s' if stripped != 1 else ''} ({detail}) — stripped so the "
            f"key can be sent as an HTTP header.",
            file=sys.stderr,
        )
        print(
            "  This usually means the key was copy-pasted from a PDF, "
            "rich-text editor, or web page that substituted lookalike\n"
            "  Unicode glyphs for ASCII letters. If authentication fails "
            "(e.g. \"API key not valid\"), re-copy the key from the\n"
            "  provider's dashboard and run `hermes setup` (or edit the "
            ".env file in a plain-text editor).",
            file=sys.stderr,
        )


def _load_dotenv_with_fallback(path: Path, *, override: bool) -> None:
    try:
        load_dotenv(dotenv_path=path, override=override, encoding="utf-8")
    except UnicodeDecodeError:
        load_dotenv(dotenv_path=path, override=override, encoding="latin-1")
    # 从刚加载的凭据环境变量中去除非 ASCII 字符。API 密钥
    # 必须是纯 ASCII，因为它们作为 HTTP header 值发送（httpx
    # 将 header 编码为 ASCII）。非 ASCII 字符通常来自从 PDF
    # 或富文本编辑器复制粘贴密钥（用 Unicode 形似字符替代，
    # 例如 ʋ U+028B 替代 v）。
    _sanitize_loaded_credentials()


def _sanitize_env_file_if_needed(path: Path) -> None:
    """在 python-dotenv 读取之前预清理 .env 文件。

    python-dotenv 无法处理多行 KEY=VALUE 对连接在单行上
    （缺少换行符）的损坏行。这会产生混乱的值——例如
    bot token 重复 8×（参见 #8908）。

    还会去除嵌入的空字节，它们会使 ``os.environ[k] = v`` 崩溃并
    报 ``ValueError: embedded null byte``——通常由从终端或
    富文本编辑器复制粘贴 API 密钥引入。

    我们委托给 ``hermes_cli.config._sanitize_env_lines``，它
    已知所有有效的 Hermes 环境变量名，可以正确地分割
    连接的行。
    """
    if not path.exists():
        return
    try:
        from hermes_cli.config import _sanitize_env_lines
    except ImportError:
        return  # 早期引导——config 模块尚不可用

    read_kw = {"encoding": "utf-8-sig", "errors": "replace"}
    try:
        with open(path, **read_kw) as f:
            original = f.readlines()
        # 在 _sanitize_env_lines 之前去除空字节，使它们永远不会
        # 到达 python-dotenv（后者会将它们传递给 os.environ 并
        # 因 ValueError 崩溃）。
        stripped = [line.replace("\x00", "") for line in original]
        sanitized = _sanitize_env_lines(stripped)
        if sanitized != original:
            import tempfile
            fd, tmp = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp", prefix=".env_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.writelines(sanitized)
                    f.flush()
                    os.fsync(f.fileno())
                atomic_replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    except Exception:
        pass  # 尽力而为——不阻塞 gateway 启动


def load_hermes_dotenv(
    *,
    hermes_home: str | os.PathLike | None = None,
    project_env: str | os.PathLike | None = None,
) -> list[Path]:
    """加载 Hermes 环境文件，用户配置优先。

    行为：
    - ``~/.hermes/.env`` 存在时会覆盖过时的 shell 导出值。
    - 项目 ``.env`` 作为开发回退，仅在用户 env 存在时填充缺失值。
    - 如果用户 env 不存在，项目 ``.env`` 也会覆盖过时的 shell 变量。
    """
    loaded: list[Path] = []

    home_path = Path(hermes_home or os.getenv("HERMES_HOME", Path.home() / ".hermes"))
    user_env = home_path / ".env"
    project_env_path = Path(project_env) if project_env else None

    # 在 python-dotenv 解析之前修复损坏的 .env 文件（#8908）。
    if user_env.exists():
        _sanitize_env_file_if_needed(user_env)
    if project_env_path and project_env_path.exists():
        _sanitize_env_file_if_needed(project_env_path)

    if user_env.exists():
        _load_dotenv_with_fallback(user_env, override=True)
        loaded.append(user_env)

    if project_env_path and project_env_path.exists():
        _load_dotenv_with_fallback(project_env_path, override=not loaded)
        loaded.append(project_env_path)

    _apply_external_secret_sources(home_path)
    _apply_managed_env()

    return loaded


def _apply_managed_env() -> None:
    """Apply the managed-scope .env last, with override, so it beats user/shell.

    Managed scope is machine-global (independent of HERMES_HOME / profile). v1
    enforcement is "applied last with override=True" — at the end of startup load
    ``os.environ`` holds the managed value for every managed key, beating both the
    user ``.env`` and any pre-existing shell export. This deliberately inverts the
    usual env-over-config precedence for the pinned keys (see
    ``docs/design/managed-scope.md`` §4.1).

    This does NOT prevent the agent from later mutating ``os.environ`` in-process
    or ``export``-ing in a subprocess shell; that hard boundary is a documented
    v2 item (design §8.1). v1 relies on filesystem permissions only.

    Fail-open: a missing managed dir or .env is the common case and a no-op; any
    error here is swallowed so managed scope can never block startup.
    """
    try:
        from hermes_cli import managed_scope

        managed_dir = managed_scope.get_managed_dir()
    except Exception:  # noqa: BLE001 — managed 作用域绝不能阻塞启动
        return
    if managed_dir is None:
        return
    managed_env = managed_dir / ".env"
    if not managed_env.exists():
        return
    _sanitize_env_file_if_needed(managed_env)
    _load_dotenv_with_fallback(managed_env, override=True)


def _apply_external_secret_sources(home_path: Path) -> None:
    """从外部来源（当前为 Bitwarden）拉取密钥到环境变量中。

    在 dotenv 加载之后运行，以便 .env 值可见（我们使用它们来
    定位 access token），但在 Hermes 的其余部分从 ``os.environ``
    读取凭据之前运行。此处的任何失败都会被记录并吞掉——
    外部密钥来源绝不能阻塞启动。

    在单个进程内幂等：对相同 ``home_path`` 的后续调用为 no-op。
    ``load_hermes_dotenv()`` 在多个热模块（cli.py、hermes_cli/main.py、
    run_agent.py、trajectory_compressor.py 等）的导入时运行，因此
    如果没有此保护，Bitwarden 状态行会在每次 CLI 启动时打印 3-5 次。
    使用 ``reset_secret_source_cache()`` 来强制重新拉取（测试、
    未来从长时间运行进程执行的 ``hermes secrets bitwarden sync``）。
    """
    home_key = str(Path(home_path).resolve())
    if home_key in _APPLIED_HOMES:
        return
    _APPLIED_HOMES.add(home_key)

    try:
        cfg = _load_secrets_config(home_path)
    except Exception:  # noqa: BLE001 — 配置错误绝不能阻塞启动
        return

    bw_cfg = (cfg or {}).get("bitwarden") or {}
    if not bw_cfg.get("enabled"):
        return

    try:
        from agent.secret_sources.bitwarden import apply_bitwarden_secrets
    except ImportError:
        return

    result = apply_bitwarden_secrets(
        enabled=True,
        access_token_env=bw_cfg.get("access_token_env", "BWS_ACCESS_TOKEN"),
        project_id=bw_cfg.get("project_id", ""),
        override_existing=bool(bw_cfg.get("override_existing", False)),
        cache_ttl_seconds=float(bw_cfg.get("cache_ttl_seconds", 300)),
        auto_install=bool(bw_cfg.get("auto_install", True)),
        server_url=str(bw_cfg.get("server_url", "") or "").strip(),
        home_path=home_path,
    )

    if result.applied:
        # 重新运行 ASCII 清理：BSM 值是用户提供的，
        # 可能与手动编辑的 .env 存在相同的复制粘贴损坏（参见 #6843）。
        _sanitize_loaded_credentials()
        # 记住这些来源，以便 setup / `hermes model` 流程
        # 可以用 "(from Bitwarden)" 标记检测到的凭据——
        # 否则用户会看到 "credentials ✓" 而不知道值来自
        # BSM 而非 .env。
        for name in result.applied:
            _SECRET_SOURCES[name] = "bitwarden"
        print(
            f"  Bitwarden Secrets Manager: applied {len(result.applied)} "
            f"secret{'s' if len(result.applied) != 1 else ''} "
            f"({', '.join(sorted(result.applied))})",
            file=sys.stderr,
        )
    if result.error:
        print(
            f"  Bitwarden Secrets Manager: {result.error}",
            file=sys.stderr,
        )
    for warn in result.warnings:
        print(
            f"  Bitwarden Secrets Manager: {warn}",
            file=sys.stderr,
        )


def _load_secrets_config(home_path: Path) -> dict:
    """仅读取 config.yaml 中的 ``secrets:`` 部分。

    延迟导入并与主配置加载器隔离，因此格式错误的配置不会
    导致 dotenv 加载完全失败。
    """
    config_path = home_path / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        import yaml  # type: ignore
    except ImportError:
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001
        return {}
    return data.get("secrets") or {}
