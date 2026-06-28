"""
hermes CLI 的 dump 命令。

输出用户 Hermes 配置的紧凑纯文本摘要，
可复制粘贴到 Discord/GitHub/Telegram 以提供上下文支持。
无 ANSI 颜色，无勾选符号——只有数据。
"""

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

from hermes_cli.config import get_hermes_home, get_env_path, get_project_root, load_config
from hermes_cli.env_loader import load_hermes_dotenv
from hermes_constants import display_hermes_home
from agent.skill_utils import is_excluded_skill_path


def _get_git_commit(project_root: Path) -> str:
    """返回短 git commit 哈希，或 '(unknown)'。

    源码安装和开发镜像通过 ``git rev-parse`` 实时解析。
    发布的 Docker 镜像在构建上下文中排除了 ``.git``，因此
    该查找始终失败——我们回退到 Dockerfile 的
    ``HERMES_GIT_SHA`` 构建参数写入 ``<project_root>/.hermes_build_sha``
    的烘焙构建 SHA（参见 ``hermes_cli/build_info.py``）。
    无论来源如何，输出格式相同。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=str(project_root),
        )
        if result.returncode == 0:
            value = result.stdout.strip()
            if value:
                return value
    except Exception:
        pass

    # 回退到构建时烘焙的 SHA（在已发布的 Docker 镜像中存在，
    # 其他情况下不存在）。延迟导入以使 dump 模块在非 dump
    # 代码路径上保持轻量。
    try:
        from hermes_cli.build_info import get_build_sha
        baked = get_build_sha(short=8)
        if baked:
            return baked
    except Exception:
        pass

    return "(unknown)"


def _get_git_commit_date(project_root: Path) -> str:
    """返回 HEAD 提交的编写日期（YYYY-MM-DD），或 ''。

    在源码安装中通过 ``git log`` 实时解析。发布的 Docker
    镜像排除了 ``.git``，因此在此返回 ''——dump 行在这种情况下
    会省略日期后缀（烘焙的 SHA 仍可标识构建版本）。
    """
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%cd", "--date=short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=str(project_root),
        )
        if result.returncode == 0:
            value = result.stdout.strip()
            if value:
                return value
    except Exception:
        pass

    return ""


def _redact(value: str) -> str:
    """对除前 4 个和后 4 个字符外的所有字符进行脱敏。

    :func:`agent.redact.mask_secret` 的薄封装。对空值返回 ``""``
    （与此辅助函数的历史行为一致——``hermes dump`` 将空值格式化为
    空白，而非 ``"(not set)"``）。
    """
    from agent.redact import mask_secret
    return mask_secret(value)


def _gateway_status() -> str:
    """返回简短的 gateway 状态字符串。"""
    try:
        from hermes_cli.gateway import get_gateway_runtime_snapshot

        snapshot = get_gateway_runtime_snapshot()
        if snapshot.running:
            mode = snapshot.manager
            if snapshot.has_process_service_mismatch:
                mode = "manual"
            return f"running ({mode}, pid {snapshot.gateway_pids[0]})"
        if snapshot.service_installed and not snapshot.service_running:
            return f"stopped ({snapshot.manager})"
        return f"stopped ({snapshot.manager})"
    except Exception:
        return "unknown" if sys.platform.startswith(("linux", "darwin")) else "N/A"


def _count_skills(hermes_home: Path) -> int:
    """统计已安装的 skills 数量。"""
    skills_dir = hermes_home / "skills"
    if not skills_dir.is_dir():
        return 0
    count = 0
    for item in skills_dir.rglob("SKILL.md"):
        if is_excluded_skill_path(item):
            continue
        count += 1
    return count


def _count_mcp_servers(config: dict) -> int:
    """统计已配置的 MCP 服务器数量。"""
    mcp = config.get("mcp", {})
    servers = mcp.get("servers", {})
    return len(servers)


def _cron_summary(hermes_home: Path) -> str:
    """返回 cron 任务摘要。"""
    jobs_file = hermes_home / "cron" / "jobs.json"
    if not jobs_file.exists():
        return "0"
    try:
        with open(jobs_file, encoding="utf-8") as f:
            data = json.load(f)
        jobs = data.get("jobs", [])
        active = sum(1 for j in jobs if j.get("enabled", True))
        return f"{active} active / {len(jobs)} total"
    except Exception:
        return "(error reading)"


def _configured_platforms() -> list[str]:
    """返回已配置的消息平台名称列表。"""
    checks = {
        "telegram": "TELEGRAM_BOT_TOKEN",
        "discord": "DISCORD_BOT_TOKEN",
        "slack": "SLACK_BOT_TOKEN",
        "whatsapp": "WHATSAPP_ENABLED",
        "signal": "SIGNAL_HTTP_URL",
        "email": "EMAIL_ADDRESS",
        "sms": "TWILIO_ACCOUNT_SID",
        "matrix": "MATRIX_HOMESERVER_URL",
        "mattermost": "MATTERMOST_URL",
        "homeassistant": "HASS_TOKEN",
        "dingtalk": "DINGTALK_CLIENT_ID",
        "feishu": "FEISHU_APP_ID",
        "wecom": "WECOM_BOT_ID",
        "wecom_callback": "WECOM_CALLBACK_CORP_ID",
        "weixin": "WEIXIN_ACCOUNT_ID",
        "qqbot": "QQ_APP_ID",
    }
    return [name for name, env in checks.items() if os.getenv(env)]


def _memory_provider(config: dict) -> str:
    """返回活跃的 memory provider 名称。"""
    mem = config.get("memory", {})
    provider = mem.get("provider", "")
    return provider if provider else "built-in"


def _get_model_and_provider(config: dict) -> tuple[str, str]:
    """从配置中提取 model 和 provider。"""
    model_cfg = config.get("model", "")
    if isinstance(model_cfg, dict):
        model = model_cfg.get("default") or model_cfg.get("model") or model_cfg.get("name") or "(not set)"
        provider = model_cfg.get("provider") or "(auto)"
    elif isinstance(model_cfg, str):
        model = model_cfg or "(not set)"
        provider = "(auto)"
    else:
        model = "(not set)"
        provider = "(auto)"
    return model, provider


def _config_overrides(config: dict) -> dict[str, str]:
    """查找值得报告的非默认配置值。

    返回一个扁平字典，键为 dotpath，值为有趣的覆盖项。
    """
    from hermes_cli.config import DEFAULT_CONFIG

    overrides = {}

    # 包含有趣用户端覆盖的配置段
    interesting_paths = [
        ("agent", "max_turns"),
        ("agent", "gateway_timeout"),
        ("agent", "tool_use_enforcement"),
        ("terminal", "backend"),
        ("terminal", "docker_image"),
        ("terminal", "persistent_shell"),
        ("browser", "allow_private_urls"),
        ("compression", "enabled"),
        ("compression", "threshold"),
        ("display", "streaming"),
        ("display", "skin"),
        ("display", "show_reasoning"),
        ("privacy", "redact_pii"),
        ("tts", "provider"),
    ]

    for section, key in interesting_paths:
        default_section = DEFAULT_CONFIG.get(section, {})
        user_section = config.get(section, {})
        if not isinstance(default_section, dict) or not isinstance(user_section, dict):
            continue
        default_val = default_section.get(key)
        user_val = user_section.get(key)
        if user_val is not None and user_val != default_val:
            overrides[f"{section}.{key}"] = str(user_val)

    # Toolsets（如果与默认值不同）
    default_toolsets = DEFAULT_CONFIG.get("toolsets", [])
    user_toolsets = config.get("toolsets", [])
    if user_toolsets != default_toolsets:
        overrides["toolsets"] = str(user_toolsets)

    # 回退 provider
    fallbacks = config.get("fallback_providers", [])
    if fallbacks:
        overrides["fallback_providers"] = str(fallbacks)

    return overrides


def run_dump(args):
    """输出紧凑的、可复制粘贴的配置摘要。"""
    show_keys = getattr(args, "show_keys", False)

    # 从 .env 文件加载环境变量，使 key 检查生效
    env_path = get_env_path()
    load_hermes_dotenv(
        hermes_home=env_path.parent,
        project_env=get_project_root() / ".env",
    )

    project_root = get_project_root()
    hermes_home = get_hermes_home()

    try:
        from hermes_cli import __version__
    except ImportError:
        __version__ = "(unknown)"

    commit = _get_git_commit(project_root)
    commit_date = _get_git_commit_date(project_root)

    try:
        config = load_config()
    except Exception:
        config = {}

    model, provider = _get_model_and_provider(config)

    # Profile（配置文件）
    try:
        from hermes_cli.profiles import get_active_profile_name
        profile = get_active_profile_name() or "(default)"
    except Exception:
        profile = "(default)"

    # Terminal backend——报告实际生效的 backend，而不仅是 config.yaml。
    # config.yaml 中的 ``terminal.backend`` 会桥接到 TERMINAL_ENV 环境变量，
    # 但在 .env / shell 中直接设置的 TERMINAL_ENV 会覆盖配置，也是
    # terminal_tool 实际使用的值（tools/terminal_tool.py 读取 TERMINAL_ENV）。
    # 仅报告配置值会隐藏该覆盖，并在 agent 运行于 docker/podman 沙箱时
    # 误导用户排查方向（即使配置显示 "local"，反之亦然）。run_dump() 已
    # 加载 .env，因此 os.environ 在此反映了真实的覆盖情况。
    terminal_cfg = config.get("terminal", {})
    config_backend = terminal_cfg.get("backend", "local")
    env_backend = (os.environ.get("TERMINAL_ENV") or "").strip().lower()
    if env_backend and env_backend != str(config_backend).strip().lower():
        backend = (
            f"{env_backend}  (TERMINAL_ENV overrides config.yaml "
            f"terminal.backend={config_backend})"
        )
    else:
        backend = config_backend

    # OpenAI SDK 版本
    try:
        import openai
        openai_ver = openai.__version__
    except ImportError:
        openai_ver = "not installed"

    # 操作系统信息
    os_info = f"{platform.system()} {platform.release()} {platform.machine()}"

    lines = []
    lines.append("--- hermes dump ---")
    # 通过 commit 和该提交的日期标识构建版本，
    # 通过 git 实时解析。此处故意不显示 __release_date__
    # （包发布日期）——它看起来像挂钟时间戳，会混淆
    # 支持分类。提交日期才是真正的"截至"日期。
    ver_str = f"{__version__}"
    ver_str += f" [{commit}]"
    if commit_date:
        ver_str += f" ({commit_date})"
    lines.append(f"version:          {ver_str}")
    lines.append(f"os:               {os_info}")
    lines.append(f"python:           {sys.version.split()[0]}")
    lines.append(f"openai_sdk:       {openai_ver}")
    lines.append(f"profile:          {profile}")
    lines.append(f"hermes_home:      {display_hermes_home()}")
    lines.append(f"model:            {model}")
    lines.append(f"provider:         {provider}")
    lines.append(f"terminal:         {backend}")

    # API 密钥
    lines.append("")
    lines.append("api_keys:")
    api_keys = [
        ("OPENROUTER_API_KEY", "openrouter"),
        ("OPENAI_API_KEY", "openai"),
        ("ANTHROPIC_API_KEY", "anthropic"),
        ("ANTHROPIC_TOKEN", "anthropic_token"),
        ("NOUS_API_KEY", "nous"),
        ("GOOGLE_API_KEY", "google/gemini"),
        ("GEMINI_API_KEY", "gemini"),
        ("GLM_API_KEY", "glm/zai"),
        ("ZAI_API_KEY", "zai"),
        ("KIMI_API_KEY", "kimi"),
        ("MINIMAX_API_KEY", "minimax"),
        ("DEEPSEEK_API_KEY", "deepseek"),
        ("DASHSCOPE_API_KEY", "dashscope"),
        ("HF_TOKEN", "huggingface"),
        ("NVIDIA_API_KEY", "nvidia"),
        ("OPENCODE_ZEN_API_KEY", "opencode_zen"),
        ("OPENCODE_GO_API_KEY", "opencode_go"),
        ("KILOCODE_API_KEY", "kilocode"),
        ("FIRECRAWL_API_KEY", "firecrawl"),
        ("TAVILY_API_KEY", "tavily"),
        ("BROWSERBASE_API_KEY", "browserbase"),
        ("FAL_KEY", "fal"),
        ("ELEVENLABS_API_KEY", "elevenlabs"),
        ("GITHUB_TOKEN", "github"),
    ]

    for env_var, label in api_keys:
        val = os.getenv(env_var, "")
        if show_keys and val:
            display = _redact(val)
        else:
            display = "set" if val else "not set"
        # 通过 `hermes auth add openrouter` 添加的凭据存储在
        # 凭据池中，而非环境变量——在此处显示它，使 dump 不会
        # 误导性地显示 "not set"，而 `hermes auth list` 却显示它（#42130）。
        if not val and label == "openrouter":
            try:
                from agent.credential_pool import load_pool as _load_pool

                if _load_pool("openrouter").has_credentials():
                    display = "set (auth pool)"
            except Exception:
                pass
        lines.append(f"  {label:<20} {display}")

    # 功能摘要
    lines.append("")
    lines.append("features:")

    toolsets = config.get("toolsets", ["hermes-cli"])
    lines.append(f"  toolsets:           {', '.join(toolsets) if toolsets else '(default)'}")
    lines.append(f"  mcp_servers:        {_count_mcp_servers(config)}")
    lines.append(f"  memory_provider:    {_memory_provider(config)}")
    lines.append(f"  gateway:            {_gateway_status()}")

    platforms = _configured_platforms()
    lines.append(f"  platforms:          {', '.join(platforms) if platforms else 'none'}")
    lines.append(f"  cron_jobs:          {_cron_summary(hermes_home)}")
    lines.append(f"  skills:             {_count_skills(hermes_home)}")

    # 配置覆盖（非默认值）
    overrides = _config_overrides(config)
    if overrides:
        lines.append("")
        lines.append("config_overrides:")
        for key, val in overrides.items():
            lines.append(f"  {key}: {val}")

    lines.append("--- end dump ---")

    output = "\n".join(lines)
    print(output)
