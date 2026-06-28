"""
Hermes Agent 的定时任务（cron job）管理工具。

暴露一个压缩的、面向动作的工具，以避免 schema/上下文膨胀。
为直接 Python 调用者和遗留测试保留了兼容性包装。
"""

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from hermes_constants import display_hermes_home

logger = logging.getLogger(__name__)

# 从 cron 模块导入（正确安装后即可用）
sys.path.insert(0, str(Path(__file__).parent.parent))

from cron.jobs import (
    AmbiguousJobReference,
    claim_job_for_fire,
    create_job,
    get_job,
    list_jobs,
    mark_job_run,
    parse_schedule,
    pause_job,
    remove_job,
    resolve_job_ref,
    resume_job,
    update_job,
)


def _notify_provider_jobs_changed_safe() -> None:
    """告知活跃的 cron 调度器 provider 任务集合已变更（对内置 provider 是
    no-op）。尽力而为——绝不让 provider 错误打断工具。"""
    try:
        from cron.scheduler import _notify_provider_jobs_changed
        _notify_provider_jobs_changed()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cron 提示词扫描
# ---------------------------------------------------------------------------
#
# 两个威胁面，两个扫描器：
#
#   1. 用户提供的 cron 提示词（小，作为指令书写）。
#      严格扫描是合适的——一个合法的 cron 提示词没理由
#      说 "cat ~/.hermes/.env" 或 "rm -rf /"。`_scan_cron_prompt()` 在
#      创建/更新时以及作为运行时纵深防御对此运行。
#
#   2. 包含已加载 skill 内容的组装提示词（大型 markdown
#      正文，常常是安全文档、事后总结、以散文形式讨论攻击
#      模式的 runbook）。在这里复用严格模式会对每次 skill *描述*
#      某个命令都误报——见 #3968 后续：`hermes-agent-dev`
#      skill 包含一篇提到 `cat ~/.hermes/.env` 的安全事后总结，
#      这触发了 `read_secrets` 并静默杀死了所有 PR-scout 任务。
#
#      Skill 正文由用户精选，并在安装时由
#      `skills_guard.py` 扫描。运行时 cron 扫描只需捕获
#      那些措辞无法在正常英文散文中存活的模式：
#      经典的提示词注入指令（"ignore previous instructions"、
#      "disregard your rules"）、欺骗指令和不可见
#      unicode。`_scan_cron_skill_assembled()` 用这个更收紧的模式集
#      对组装后的提示词运行。
#
# 两个扫描器共享不可见 unicode 检查和 GitHub Authorization
# 头豁免。

# 严格模式——仅应用于用户提示词。
_CRON_THREAT_PATTERNS = [
    (r'ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions', "prompt_injection"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass)', "read_secrets"),
    (r'authorized_keys', "ssh_backdoor"),
    (r'/etc/sudoers|visudo', "sudoers_mod"),
    (r'rm\s+-rf\s+/', "destructive_root_rm"),
]

# 更宽松的模式集——当 skill 被附加时应用于组装后的提示词。
# 仅包含在任何上下文中措辞都不含歧义的模式；
# 命令形状的模式被丢弃，因为它们会在安全文档/事后总结的
# 散文上误报。Skill 正文在安装时由
# `skills_guard.py` 扫描，因此运行时 cron 扫描纯粹是一个绊线，
# 用于捕获溜过安装的恶意 skill 中存活的明显注入指令。
_CRON_SKILL_ASSEMBLED_PATTERNS = [
    (r'ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions', "prompt_injection"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
]

_CRON_SECRET_VAR_RE = r'\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)\w*\}?'
_CRON_EXFIL_COMMAND_PATTERNS = [
    # 把外泄检测收紧到明显的泄露路径：把密钥直接嵌入
    # 目标 URL、通过 POST/FORM 载荷发送，或通过 Authorization 头发送到
    # 任意主机。今天唯一预期的白名单例外是与 api.github.com 通信的
    # 内置 GitHub skill 模式。
    (rf'curl\s+[^\n]*https?://[^\s"\'`]*{_CRON_SECRET_VAR_RE}', "exfil_curl_url"),
    (rf'wget\s+[^\n]*https?://[^\s"\'`]*{_CRON_SECRET_VAR_RE}', "exfil_wget_url"),
    (rf'curl\s+[^\n]*(?:--data(?:-raw|-binary|-urlencode)?|-d|--form|-F)\s+[^\n]*{_CRON_SECRET_VAR_RE}', "exfil_curl_data"),
    (rf'wget\s+[^\n]*--post-(?:data|file)=[^\n]*{_CRON_SECRET_VAR_RE}', "exfil_wget_post"),
    (rf'curl\s+[^\n]*(?:-H|--header)\s+["\']Authorization:\s*(?:Bearer|token)\s+{_CRON_SECRET_VAR_RE}["\']', "exfil_curl_auth_header"),
]

_CRON_INVISIBLE_CHARS = {
    '\u200b', '\u200c', '\u200d', '\u2060', '\ufeff',
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
}

# U+200D Zero-Width Joiner（零宽连接符）也是许多 Unicode emoji 序列中合法、
# 必需的一部分（例如 👨‍👩‍👧、🏳️‍🌈、❤️‍🩹、🧑‍💻）。
# 我们仍应在 ZWJ 隐藏在纯文本字符之间时阻止它，
# 但在它明显是 emoji 字素簇的一部分时不阻止。
_EMOJI_NEIGHBOUR_CP_RANGES = (
    (0x1F000, 0x1FFFF),
    (0x2600, 0x27BF),
    (0x2300, 0x23FF),
    (0x1F1E6, 0x1F1FF),
    (0x20E3, 0x20E3),
)
_VARIATION_SELECTOR_CP = 0xFE0F


def _is_emoji_cp(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _EMOJI_NEIGHBOUR_CP_RANGES)


def _zwj_has_emoji_neighbour(text: str, idx: int) -> bool:
    """当 text[idx] 处的 ZWJ 出现在 emoji 序列内部时返回 True。"""
    left = idx - 1
    while left >= 0 and ord(text[left]) == _VARIATION_SELECTOR_CP:
        left -= 1
    right = idx + 1
    while right < len(text) and ord(text[right]) == _VARIATION_SELECTOR_CP:
        right += 1
    return (
        left >= 0 and right < len(text)
        and _is_emoji_cp(ord(text[left]))
        and _is_emoji_cp(ord(text[right]))
    )


def _strip_legitimate_emoji_zwj(prompt: str) -> str:
    if '\u200d' not in prompt:
        return prompt
    cleaned: list[str] = []
    for idx, ch in enumerate(prompt):
        if ch == '\u200d' and _zwj_has_emoji_neighbour(prompt, idx):
            continue
        cleaned.append(ch)
    return ''.join(cleaned)


def _strip_cron_safe_constructs(prompt: str) -> str:
    """剥离 GitHub 的 `Authorization: token $GITHUB_TOKEN` 认证头
    模式，使其不会触发更宽泛的 curl 认证头外泄规则。

    允许内置 GitHub skill 回退，而不为任意 Authorization 头
    外泄打开一刀切的豁免。
    """
    github_auth_header = re.search(
        rf'curl\s+[^\n]*(?:-H|--header)\s+["\']Authorization:\s*token\s+{_CRON_SECRET_VAR_RE}["\']'
        r'\s+["\']?https://api\.github\.com(?:/|\b)',
        prompt,
        re.IGNORECASE,
    )
    if github_auth_header:
        return prompt.replace(github_auth_header.group(0), "curl https://api.github.com/user")
    return prompt


def _check_invisible_unicode(prompt: str) -> str:
    """若提示词包含不可见 unicode 注入标记则返回错误字符串
    （合法 emoji 序列内的 ZWJ 被允许）。
    """
    prompt_for_invisible_scan = _strip_legitimate_emoji_zwj(prompt)
    for char in _CRON_INVISIBLE_CHARS:
        if char in prompt_for_invisible_scan:
            return f"Blocked: prompt contains invisible unicode U+{ord(char):04X} (possible injection)."
    return ""


def _strip_invisible_unicode(prompt: str) -> tuple[str, list[str]]:
    """从 *prompt* 中剥离不可见 unicode 字符，保留位于
    合法 emoji 序列内的 ZWJ。

    返回 ``(cleaned_prompt, removed_codepoints)``，其中 ``removed_codepoints``
    是被剥离的 ``U+XXXX`` 标签排序列表（提示词本来就很干净时为空）。
    供 skill 附加的 cron 路径使用——skill 正文已在安装时由
    ``skills_guard.py`` 审查——代码示例中一个零宽空格应被净化，
    而不是变成永久杀死任务的硬性阻止。
    """
    if not prompt:
        return prompt, []
    # 保留 emoji-ZWJ：临时移除合法的连接符，扫描/剥离
    # 其余部分，然后合法连接符存活，因为我们操作的是
    # 原始字符串，只丢弃不属于 emoji 簇的字符。
    removed: set[str] = set()
    cleaned: list[str] = []
    for idx, ch in enumerate(prompt):
        if ch in _CRON_INVISIBLE_CHARS:
            if ch == '\u200d' and _zwj_has_emoji_neighbour(prompt, idx):
                cleaned.append(ch)  # 合法的 emoji 连接符——保留
                continue
            removed.add(f"U+{ord(ch):04X}")
            continue
        cleaned.append(ch)
    return ''.join(cleaned), sorted(removed)


def _scan_cron_prompt(prompt: str) -> str:
    """扫描用户提供（USER-SUPPLIED）的 cron 提示词的关键威胁。

    严格模式集——在任务创建/更新时以及作为运行时
    纵深防御（针对扫描器出现之前编写的提示词）使用。
    用户提示词小且是指令式的；其中的裸 `cat .env` 或 `rm -rf /`
    是铁证，而非散文。被阻止时返回错误字符串，否则返回空字符串。
    """
    prompt_to_scan = _strip_cron_safe_constructs(prompt)
    invisible_err = _check_invisible_unicode(prompt_to_scan)
    if invisible_err:
        return invisible_err
    for pattern, pid in _CRON_THREAT_PATTERNS:
        if re.search(pattern, prompt_to_scan, re.IGNORECASE):
            return f"Blocked: prompt matches threat pattern '{pid}'. Cron prompts must not contain injection or exfiltration payloads."
    for pattern, pid in _CRON_EXFIL_COMMAND_PATTERNS:
        if re.search(pattern, prompt_to_scan, re.IGNORECASE):
            return f"Blocked: prompt matches threat pattern '{pid}'. Cron prompts must not contain injection or exfiltration payloads."
    return ""


def _scan_cron_skill_assembled(assembled: str) -> tuple[str, str]:
    """扫描包含已加载 skill 内容的已组装（ASSEMBLED）cron 提示词。

    更宽松的模式集——仅捕获不含歧义的提示词注入
    指令。丢弃命令形状的模式（cat .env、rm -rf /、
    authorized_keys、/etc/sudoers），因为它们会在以散文
    形式*描述*攻击命令的合法 skill markdown（安全事后总结和
    runbook）上误报。

    不可见 unicode 被净化，而非阻止。Skill 正文由用户
    精选，并在安装时已由 ``skills_guard.py`` 扫描；代码示例
    中一个零宽空格（复制粘贴的 unicode 文档中常见）不应
    永久杀死任务。违规的码位被剥离并记录，返回净化后的
    提示词。硬性阻止仍保留给通过
    ``_scan_cron_prompt`` 的原始用户提示词——那条路径才是
    真正的注入面。

    返回 ``(cleaned_prompt, error)``；提示词通过（净化后）时
    ``error`` 为空。
    """
    cleaned, removed = _strip_invisible_unicode(assembled)
    if removed:
        logger.warning(
            "Cron skill-assembled prompt: stripped %d invisible-unicode "
            "char(s) (%s) from vetted skill content",
            len(removed), ", ".join(removed),
        )
    prompt_to_scan = _strip_cron_safe_constructs(cleaned)
    for pattern, pid in _CRON_SKILL_ASSEMBLED_PATTERNS:
        if re.search(pattern, prompt_to_scan, re.IGNORECASE):
            return cleaned, f"Blocked: prompt matches threat pattern '{pid}'. Cron prompts must not contain injection or exfiltration payloads."
    return cleaned, ""


def _origin_from_env() -> Optional[Dict[str, str]]:
    from gateway.session_context import get_session_env
    origin_platform = get_session_env("HERMES_SESSION_PLATFORM")
    origin_chat_id = get_session_env("HERMES_SESSION_CHAT_ID")
    if origin_platform and origin_chat_id:
        thread_id = get_session_env("HERMES_SESSION_THREAD_ID") or None
        if thread_id:
            logger.debug(
                "Cron origin captured thread_id=%s for %s:%s",
                thread_id, origin_platform, origin_chat_id,
            )
        return {
            "platform": origin_platform,
            "chat_id": origin_chat_id,
            "chat_name": get_session_env("HERMES_SESSION_CHAT_NAME") or None,
            "thread_id": thread_id,
            # 捕获这些以便一个可选的投递镜像（cron.mirror_delivery /
            # attach_to_session）能在按用户隔离的群聊中解析出确切的
            # 参与者会话——与交互式
            # send_message 对等，后者把 HERMES_SESSION_USER_ID 传给
            # gateway.mirror.mirror_to_session。对 DM/共享会话无害。
            "user_id": get_session_env("HERMES_SESSION_USER_ID") or None,
        }
    return None


def _local_delivery_notice(job: Dict[str, Any], user_deliver: Optional[str]) -> Optional[str]:
    """当一个创建的任务不会投递到任何地方时返回一条信息性通知。

    TUI/CLI 会话无法被捕获为 cron ``origin``（没有为它们设置
    ``HERMES_SESSION_PLATFORM``/``CHAT_ID``），因此一个
    ``deliver="origin"`` 请求——或省略 ``deliver`` 而默认为
    origin-or-local——会产生一个运行并把输出保存到
    ``last_output`` 但永不投递回会话的任务。这是设计使然
    （本地会话没有实时投递通道），但静默
    丢弃用户的"运行时告诉我"意图正是 #51568 报告的陷阱。
    在创建时浮现它，以便 agent 可以转达，而不是
    承诺一个永不发生的投递。

    当用户显式要求 ``local``（无意外）时返回 ``None``，
    或当任务解析到一个真实的投递目标时返回 ``None``。
    """
    # 一个显式的 local 请求正是用户想要的——无需通知。
    if (user_deliver or "").strip().lower() == "local":
        return None
    try:
        from cron.scheduler import _resolve_delivery_targets

        if _resolve_delivery_targets(job):
            return None  # 确实会投递到某处——没什么可标记的。
    except Exception:
        # 若无法评估解析，则回退到 origin 信号。
        if job.get("origin"):
            return None
    return (
        "This is a local-only cron job: its output is saved (view it with "
        "cronjob(action='list')) but will NOT be delivered back into this "
        "session — CLI/TUI sessions have no live-delivery channel. To be "
        "notified when it runs, recreate or update the job with deliver set to "
        "a gateway-connected platform, e.g. deliver='telegram' or deliver='all'."
    )


def _repeat_display(job: Dict[str, Any]) -> str:
    times = (job.get("repeat") or {}).get("times")
    completed = (job.get("repeat") or {}).get("completed", 0)
    if times is None:
        return "forever"
    if times == 1:
        return "once" if completed == 0 else "1/1"
    return f"{completed}/{times}" if completed else f"{times} times"


def _canonical_skills(skill: Optional[str] = None, skills: Optional[Any] = None) -> List[str]:
    if skills is None:
        raw_items = [skill] if skill else []
    elif isinstance(skills, str):
        raw_items = [skills]
    else:
        raw_items = list(skills)

    normalized: List[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized




def _resolve_model_override(model_obj: Optional[Dict[str, Any]]) -> tuple:
    """把一个模型覆盖对象解析为用于任务存储的 (provider, model)。

    如果省略 provider，则固定配置中的当前主 provider，以便
    用户随后通过 hermes model 更改默认值时任务不会漂移。

    返回 (provider_str_or_none, model_str_or_none)。
    """
    if not model_obj or not isinstance(model_obj, dict):
        return (None, None)
    model_name = (model_obj.get("model") or "").strip() or None
    provider_name = (model_obj.get("provider") or "").strip() or None
    # 裸 "custom" 通常是一个不完整的规格——规范形式是
    # 匹配某个 custom_providers 条目的 "custom:<name>"，而 LLM 经常
    # 供应裸类型，因为 schema 不广告
    # ":<name>" 后缀。它只在运行时无法解析时才是问题：
    # 用户可能字面上命名一个 ``providers.custom``（或 custom_providers
    # "custom"）条目，此时任务应保留 ``provider="custom"``
    # 并针对该端点运行。仅当不存在这样的条目时，我们才把
    # 裸值视为"未供应 provider"并在下方固定当前主
    # provider——否则固定到 ``model.provider``（例如 codex）
    # 会静默劫持一个本意是使用配置的自定义端点的任务。
    if provider_name == "custom":
        try:
            from hermes_cli.runtime_provider import has_named_custom_provider
            if not has_named_custom_provider("custom"):
                provider_name = None
        except Exception:
            provider_name = None
    if model_name and not provider_name:
        # 固定到当前主 provider 以便任务稳定
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            model_cfg = cfg.get("model", {})
            if isinstance(model_cfg, dict):
                provider_name = model_cfg.get("provider") or None
        except Exception:
            pass  # 尽力而为；provider 保持 None
    return (provider_name, model_name)


def _normalize_optional_job_value(value: Optional[Any], *, strip_trailing_slash: bool = False) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if strip_trailing_slash:
        text = text.rstrip("/")
    return text or None


def _normalize_deliver_param(value: Any) -> Optional[str]:
    """把用户提供的 ``deliver`` 值规范化为规范字符串形式。

    cron schema 把 ``deliver`` 记录为字符串（``"local"``、``"origin"``、
    ``"telegram"``、``"telegram:chat_id[:thread_id]"``，或逗号分隔的组合）。
    一些调用者——传数组的 MCP 客户端、把载荷构建为
    列表的脚本——会供应 ``["telegram"]``。``create_job``/``update_job`` 原样存储，
    而调度器的 ``str(deliver).split(",")`` 随后把列表序列化为
    字面量 ``"['telegram']"``，这不是一个已知的平台。在 API 边界
    处展平列表/元组，以便存储始终是字符串。对 ``None``/空返回 ``None``
    以便调用者可以将其视为"未供应"。
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        parts = [str(p).strip() for p in value if str(p).strip()]
        return ",".join(parts) if parts else None
    text = str(value).strip()
    return text or None


def _validate_cron_script_path(script: Optional[str]) -> Optional[str]:
    """在 API 边界验证一个 cron 任务脚本路径。

    脚本必须是能解析到 HERMES_HOME/scripts/ 内的相对路径。
    绝对路径和 ~ 展开被拒绝，以防止通过提示词注入
    执行任意脚本。

    若被阻止则返回错误字符串，否则返回 None（有效）。
    """
    if not script or not script.strip():
        return None  # 空/None = 清空该字段，始终 OK

    from hermes_constants import get_hermes_home

    raw = script.strip()

    # 在 API 边界拒绝绝对路径和 ~ 展开。
    # 仅允许 ~/.hermes/scripts/ 内的相对路径。
    if raw.startswith(("/", "~")) or (len(raw) >= 2 and raw[1] == ":"):
        return (
            f"Script path must be relative to ~/.hermes/scripts/. "
            f"Got absolute or home-relative path: {raw!r}. "
            f"Place scripts in ~/.hermes/scripts/ and use just the filename."
        )

    # 解析后验证包含关系
    from tools.path_security import validate_within_dir

    scripts_dir = get_hermes_home() / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    containment_error = validate_within_dir(scripts_dir / raw, scripts_dir)
    if containment_error:
        return (
            f"Script path escapes the scripts directory via traversal: {raw!r}"
        )

    return None


def _format_job(job: Dict[str, Any]) -> Dict[str, Any]:
    prompt = str(job.get("prompt") or "")
    skills = _canonical_skills(job.get("skill"), job.get("skills"))
    job_id = str(job.get("id") or "unknown")
    name = str(job.get("name") or prompt[:50] or (skills[0] if skills else "") or job_id or "cron job")
    result = {
        "job_id": job_id,
        "name": name,
        "skill": skills[0] if skills else None,
        "skills": skills,
        "prompt_preview": prompt[:100] + "..." if len(prompt) > 100 else prompt,
        "model": job.get("model"),
        "provider": job.get("provider"),
        "base_url": job.get("base_url"),
        "schedule": job.get("schedule_display") or "?",
        "repeat": _repeat_display(job),
        "deliver": job.get("deliver", "local"),
        "next_run_at": job.get("next_run_at"),
        "last_run_at": job.get("last_run_at"),
        "last_status": job.get("last_status"),
        "last_delivery_error": job.get("last_delivery_error"),
        "enabled": job.get("enabled", True),
        "state": job.get("state", "scheduled" if job.get("enabled", True) else "paused"),
        "paused_at": job.get("paused_at"),
        "paused_reason": job.get("paused_reason"),
    }
    if job.get("script"):
        result["script"] = job["script"]
    if job.get("no_agent"):
        result["no_agent"] = True
    if job.get("enabled_toolsets"):
        result["enabled_toolsets"] = job["enabled_toolsets"]
    if job.get("workdir"):
        result["workdir"] = job["workdir"]
    return result


def _execute_job_now(job: Dict[str, Any]) -> Dict[str, Any]:
    """立即执行一个 cron 任务，在调度器 tick 之外。

    先通过 ``claim_job_for_fire`` 原子地认领任务——与
    调度器/外部 provider 触发路径使用的相同 at-most-once CAS——这样
    一个并发运行的 gateway ticker 就无法也触发它（认领既
    阻止重复触发，又为周期性任务推进 ``next_run_at``）。
    如果认领失败（另一次触发正在进行），这是 no-op。

    实际触发委托给 ``run_one_job``——ticker 和外部 provider 使用的
    单一共享 execute→save→deliver→mark 主体——因此
    失败投递、``[SILENT]`` 处理和实时 adapter 投递在各路径间保持
    一致，不会漂移。

    返回 {"claimed": bool, "success": bool, "error": str|None}。
    """
    job_id = job["id"]
    try:
        from cron.scheduler import run_one_job

        # At-most-once 认领：若一个 tick/其他触发已拥有它，则不运行直接退出。
        if not claim_job_for_fire(job_id):
            return {"claimed": False, "success": False,
                    "error": "Job is already being fired by the scheduler; not run again."}

        # run_one_job 通过 mark_job_run 记录 last_run_at/last_status（它
        # 还清除触发认领），并仅在它处理了任务时返回 True。
        processed = run_one_job(job)
        refreshed = get_job(job_id) or {}
        ok = refreshed.get("last_status") == "ok"
        return {
            "claimed": True,
            "success": bool(processed and ok),
            "error": refreshed.get("last_error"),
        }

    except Exception as e:
        logger.error("Failed to execute cron job %s immediately: %s", job_id, e)
        try:
            mark_job_run(job_id, False, str(e))
        except Exception:
            pass
        return {"claimed": True, "success": False, "error": str(e)}


def cronjob(
    action: str,
    job_id: Optional[str] = None,
    prompt: Optional[str] = None,
    schedule: Optional[str] = None,
    name: Optional[str] = None,
    repeat: Optional[int] = None,
    deliver: Optional[str] = None,
    include_disabled: bool = False,
    skill: Optional[str] = None,
    skills: Optional[List[str]] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    reason: Optional[str] = None,
    script: Optional[str] = None,
    context_from: Optional[Union[str, List[str]]] = None,
    enabled_toolsets: Optional[List[str]] = None,
    workdir: Optional[str] = None,
    no_agent: Optional[bool] = None,
    attach_to_session: Optional[bool] = None,
    task_id: str = None,
) -> str:
    """统一的 cron 任务管理工具。"""
    del task_id  # 未使用但保留以兼容 handler 签名

    try:
        normalized = (action or "").strip().lower()

        if normalized == "create":
            if not schedule:
                return tool_error("schedule is required for create", success=False)
            canonical_skills = _canonical_skills(skill, skills)
            _no_agent = bool(no_agent)
            # 任务形状验证按模式不同：
            #   - no_agent=True → script 就是任务；prompt/skills 可选
            #     （且与执行无关）。
            #   - no_agent=False（默认）→ prompt/skills 至少需设置其一，
            #     与之前相同。
            if _no_agent:
                if not script:
                    return tool_error(
                        "create with no_agent=True requires a script — "
                        "the script is the job.",
                        success=False,
                    )
            elif not prompt and not canonical_skills:
                return tool_error("create requires either prompt or at least one skill", success=False)
            if prompt:
                scan_error = _scan_cron_prompt(prompt)
                if scan_error:
                    return tool_error(scan_error, success=False)

            # 存储前验证脚本路径
            if script:
                script_error = _validate_cron_script_path(script)
                if script_error:
                    return tool_error(script_error, success=False)

            # 验证 context_from 引用了已存在的任务
            if context_from:
                from cron.jobs import get_job as _get_job
                refs = [context_from] if isinstance(context_from, str) else context_from
                for ref_id in refs:
                    if not _get_job(ref_id):
                        return tool_error(
                            f"context_from job '{ref_id}' not found. "
                            "Use cronjob(action='list') to see available jobs.",
                            success=False,
                        )

            job = create_job(
                prompt=prompt or "",
                schedule=schedule,
                name=name,
                repeat=repeat,
                deliver=_normalize_deliver_param(deliver),
                origin=_origin_from_env(),
                skills=canonical_skills,
                model=_normalize_optional_job_value(model),
                provider=_normalize_optional_job_value(provider),
                base_url=_normalize_optional_job_value(base_url, strip_trailing_slash=True),
                script=_normalize_optional_job_value(script),
                context_from=context_from,
                enabled_toolsets=enabled_toolsets or None,
                workdir=_normalize_optional_job_value(workdir),
                no_agent=_no_agent,
                attach_to_session=attach_to_session,
            )
            _notify_provider_jobs_changed_safe()
            _create_message = f"Cron job '{job['name']}' created."
            _local_notice = _local_delivery_notice(job, _normalize_deliver_param(deliver))
            if _local_notice:
                _create_message = f"{_create_message} {_local_notice}"
            return json.dumps(
                {
                    "success": True,
                    "job_id": job["id"],
                    "name": job["name"],
                    "skill": job.get("skill"),
                    "skills": job.get("skills", []),
                    "schedule": job["schedule_display"],
                    "repeat": _repeat_display(job),
                    "deliver": job.get("deliver", "local"),
                    "next_run_at": job["next_run_at"],
                    "job": _format_job(job),
                    "message": _create_message,
                },
                indent=2,
            )

        if normalized == "list":
            jobs = [_format_job(job) for job in list_jobs(include_disabled=include_disabled)]
            return json.dumps({"success": True, "count": len(jobs), "jobs": jobs}, indent=2)

        if not job_id:
            return tool_error(f"job_id is required for action '{normalized}'", success=False)

        try:
            job = resolve_job_ref(job_id)
        except AmbiguousJobReference as exc:
            return json.dumps(
                {
                    "success": False,
                    "error": str(exc),
                    "matches": [
                        {
                            "id": m["id"],
                            "name": m.get("name"),
                            "schedule": m.get("schedule_display"),
                            "next_run_at": m.get("next_run_at"),
                        }
                        for m in exc.matches
                    ],
                },
                indent=2,
            )
        if not job:
            return json.dumps(
                {"success": False, "error": f"Job with ID or name '{job_id}' not found. Use cronjob(action='list') to inspect jobs."},
                indent=2,
            )
        # 解析为规范 ID（支持基于名称的查找）
        job_id = job["id"]

        if normalized == "remove":
            removed = remove_job(job_id)
            if not removed:
                return tool_error(f"Failed to remove job '{job_id}'", success=False)
            _notify_provider_jobs_changed_safe()
            return json.dumps(
                {
                    "success": True,
                    "message": f"Cron job '{job['name']}' removed.",
                    "removed_job": {
                        "id": job_id,
                        "name": job["name"],
                        "schedule": job.get("schedule_display"),
                    },
                },
                indent=2,
            )

        if normalized == "pause":
            updated = pause_job(job_id, reason=reason)
            _notify_provider_jobs_changed_safe()
            return json.dumps({"success": True, "job": _format_job(updated)}, indent=2)

        if normalized == "resume":
            updated = resume_job(job_id)
            _notify_provider_jobs_changed_safe()
            return json.dumps({"success": True, "job": _format_job(updated)}, indent=2)

        if normalized in {"run", "run_now", "trigger"}:
            # 立即执行任务，而不是仅仅把它排到下一个
            # 调度器 tick——一个手动的 `run` 应当真正运行，即便
            # 没有活跃的 gateway/ticker（#41037 的情况）。_execute_job_now 内的
            # 认领会推进 next_run_at 并阻止一个并发 tick
            # 双重触发。
            exec_result = _execute_job_now(job)
            # 重新读取，以便响应反映运行后的 last_run_at/last_status。
            result = _format_job(get_job(job_id) or {"id": job_id})
            result["executed"] = exec_result.get("claimed", False)
            result["execution_success"] = exec_result.get("success", False)
            if not exec_result.get("claimed", False):
                result["execution_skipped"] = (
                    "Already being fired by the scheduler; not run again."
                )
            elif exec_result.get("error"):
                result["execution_error"] = exec_result["error"]
            return json.dumps({"success": True, "job": result}, indent=2)

        if normalized == "update":
            updates: Dict[str, Any] = {}
            if prompt is not None:
                scan_error = _scan_cron_prompt(prompt)
                if scan_error:
                    return tool_error(scan_error, success=False)
                updates["prompt"] = prompt
            if name is not None:
                updates["name"] = name
            if deliver is not None:
                updates["deliver"] = _normalize_deliver_param(deliver)
            if skills is not None or skill is not None:
                canonical_skills = _canonical_skills(skill, skills)
                updates["skills"] = canonical_skills
                updates["skill"] = canonical_skills[0] if canonical_skills else None
            if model is not None:
                updates["model"] = _normalize_optional_job_value(model)
            if provider is not None:
                updates["provider"] = _normalize_optional_job_value(provider)
            if base_url is not None:
                updates["base_url"] = _normalize_optional_job_value(base_url, strip_trailing_slash=True)
            if script is not None:
                # 传空字符串以清空已有脚本
                if script:
                    script_error = _validate_cron_script_path(script)
                    if script_error:
                        return tool_error(script_error, success=False)
                updates["script"] = _normalize_optional_job_value(script) if script else None
            if context_from is not None:
                # 空字符串/空列表清空该字段；否则验证
                # 每个被引用任务存在后再存储。规范化为列表
                # （或 None）以匹配 create_job() 存储的形状。
                if isinstance(context_from, str):
                    refs = [context_from.strip()] if context_from.strip() else []
                else:
                    refs = [str(j).strip() for j in context_from if str(j).strip()]
                if refs:
                    from cron.jobs import get_job as _get_job
                    for ref_id in refs:
                        if not _get_job(ref_id):
                            return tool_error(
                                f"context_from job '{ref_id}' not found. "
                                "Use cronjob(action='list') to see available jobs.",
                                success=False,
                            )
                updates["context_from"] = refs or None
            if enabled_toolsets is not None:
                updates["enabled_toolsets"] = enabled_toolsets or None
            if attach_to_session is not None:
                updates["attach_to_session"] = bool(attach_to_session)
            if workdir is not None:
                # 空字符串清空该字段（恢复旧行为）；
                # 否则原样传入——update_job() 会验证/规范化。
                updates["workdir"] = _normalize_optional_job_value(workdir) or None
            if no_agent is not None:
                # 在更新时切换 no_agent 开/关。若切到 True，
                # 我们需要任务上已存在一个脚本（或是同一次
                # 更新的一部分）——否则下一个 tick 会报错。
                target_no_agent = bool(no_agent)
                if target_no_agent:
                    effective_script = updates.get("script") if "script" in updates else job.get("script")
                    if not effective_script:
                        return tool_error(
                            "Cannot set no_agent=True on a job without a script. "
                            "Set `script` in the same update, or on the job first.",
                            success=False,
                        )
                updates["no_agent"] = target_no_agent
            if repeat is not None:
                # 规范化：把 0 或负数视为 None（无限）
                normalized_repeat = None if repeat <= 0 else repeat
                repeat_state = dict(job.get("repeat") or {})
                repeat_state["times"] = normalized_repeat
                updates["repeat"] = repeat_state
            if schedule is not None:
                parsed_schedule = parse_schedule(schedule)
                updates["schedule"] = parsed_schedule
                updates["schedule_display"] = parsed_schedule.get("display", schedule)
                if job.get("state") != "paused":
                    updates["state"] = "scheduled"
                    updates["enabled"] = True
            if not updates:
                return tool_error("No updates provided.", success=False)
            updated = update_job(job_id, updates)
            _notify_provider_jobs_changed_safe()
            return json.dumps({"success": True, "job": _format_job(updated)}, indent=2)

        return tool_error(f"Unknown cron action '{action}'", success=False)

    except Exception as e:
        return tool_error(str(e), success=False)



CRONJOB_SCHEMA = {
    "name": "cronjob",
    "description": """Manage scheduled cron jobs with a single compressed tool.

Use action='create' to schedule a new job from a prompt or one or more skills.
Use action='list' to inspect jobs.
Use action='update', 'pause', 'resume', 'remove', or 'run' to manage an existing job.

To stop a job the user no longer wants: first action='list' to find the job_id, then action='remove' with that job_id. Never guess job IDs — always list first.

Jobs run in a fresh session with no current-chat context, so prompts must be self-contained.
If skills are provided on create, the future cron run loads those skills in order, then follows the prompt as the task instruction.
On update, passing skills=[] clears attached skills.

NOTE: The agent's final response is auto-delivered to the target. Put the primary
user-facing content in the final response. Cron jobs run autonomously with no user
present — they cannot ask questions or request clarification.

Important safety rule: cron-run sessions should not recursively schedule more cron jobs.""",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "One of: create, list, update, pause, resume, remove, run. When action=create, the 'schedule' and 'prompt' fields are REQUIRED."
            },
            "job_id": {
                "type": "string",
                "description": "Required for update/pause/resume/remove/run"
            },
            "prompt": {
                "type": "string",
                "description": "For create: the full self-contained prompt. If skills are also provided, this becomes the task instruction paired with those skills."
            },
            "schedule": {
                "type": "string",
                "description": "REQUIRED for action=create. For create/update: '30m', 'every 2h', '0 9 * * *', or ISO timestamp. Examples: '30m' (every 30 minutes), 'every 2h' (every 2 hours), '0 9 * * *' (daily at 9am), '2026-06-01T09:00:00' (one-shot). You MUST include this field when action=create."
            },
            "name": {
                "type": "string",
                "description": "Optional human-friendly name"
            },
            "repeat": {
                "type": "integer",
                "description": "Optional repeat count. Omit for defaults (once for one-shot, forever for recurring)."
            },
            "deliver": {
                "type": "string",
                "description": "Omit this parameter to auto-deliver back to the current chat and topic (recommended). Auto-detection preserves thread/topic context. Only set explicitly when the user asks to deliver somewhere OTHER than the current conversation. Values: 'origin' (same as omitting), 'local' (no delivery, save only), 'all' (fan out to every connected home channel), or platform:chat_id:thread_id for a specific destination. Combine with comma: 'origin,all' delivers to the origin plus every other connected channel. Examples: 'telegram:-1001234567890:17585', 'discord:#engineering', 'sms:+15551234567', 'all'. WARNING: 'platform:chat_id' without :thread_id loses topic targeting. 'all' resolves at fire time, so a job created before a channel was wired up will pick it up automatically once connected."
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional ordered list of skill names to load before executing the cron prompt. On update, pass an empty array to clear attached skills."
            },
            "model": {
                "type": "object",
                "description": "Optional per-job model override. If provider is omitted, the current main provider is pinned at creation time so the job stays stable.",
                "properties": {
                    "provider": {
                        "type": "string",
                        "description": "Provider name (e.g. 'openrouter', 'anthropic', or 'custom:<name>' for a provider defined in custom_providers config — always include the ':<name>' suffix, never pass the bare 'custom'). Omit to use and pin the current provider."
                    },
                    "model": {
                        "type": "string",
                        "description": "Model name (e.g. 'anthropic/claude-sonnet-4', 'claude-sonnet-4')"
                    }
                },
                "required": ["model"]
            },
            "script": {
                "type": "string",
                "description": f"Optional path to a script that runs each tick. In the default mode its stdout is injected into the agent's prompt as context (data-collection / change-detection pattern). With no_agent=True, the script IS the job and its stdout is delivered verbatim (classic watchdog pattern). Relative paths resolve under {display_hermes_home()}/scripts/. ``.sh``/``.bash`` extensions run via bash, everything else via Python. On update, pass empty string to clear."
            },
            "no_agent": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Default: False (LLM-driven job — the agent runs the prompt each tick). "
                    "Set True to skip the LLM entirely: the scheduler just runs ``script`` on schedule and delivers its stdout verbatim. No tokens, no agent loop, no model override honoured. "
                    "\n\n"
                    "REQUIREMENTS when True: ``script`` MUST be set (``prompt`` and ``skills`` are ignored). "
                    "\n\n"
                    "DELIVERY SEMANTICS when True: "
                    "(a) non-empty stdout is sent verbatim as the message; "
                    "(b) EMPTY stdout means SILENT — nothing is sent to the user and they won't see anything happened, so design your script to stay quiet when there's nothing to report (the watchdog pattern); "
                    "(c) non-zero exit / timeout sends an error alert so a broken watchdog can't fail silently. "
                    "\n\n"
                    "WHEN TO USE True: recurring script-only pings where the script itself produces the exact message text (memory/disk/GPU watchdogs, threshold alerts, heartbeats, CI notifications, API pollers with a fixed output shape). "
                    "WHEN TO USE False (default): anything that needs reasoning — summarize a feed, draft a daily briefing, pick interesting items, rephrase data for a human, follow conditional logic based on content."
                ),
            },
            "context_from": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional job ID or list of job IDs whose most recent completed output is "
                    "injected into the prompt as context before each run. "
                    "Use this to chain cron jobs: job A collects data, job B processes it. "
                    "Each entry must be a valid job ID (from cronjob action='list'). "
                    "Note: injects the most recent completed output — does not wait for "
                    "upstream jobs running in the same tick. "
                    "On update, pass an empty array to clear."
                ),
            },
            "enabled_toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of toolset names to restrict the job's agent to (e.g. [\"web\", \"terminal\", \"file\", \"delegation\"]). When set, only tools from these toolsets are loaded, significantly reducing input token overhead. When omitted, all default tools are loaded. Infer from the job's prompt — e.g. use \"web\" if it calls web_search, \"terminal\" if it runs scripts, \"file\" if it reads files, \"delegation\" if it calls delegate_task. On update, pass an empty array to clear."
            },
            "workdir": {
                "type": "string",
                "description": "Optional absolute path to run the job from. When set, AGENTS.md / CLAUDE.md / .cursorrules from that directory are injected into the system prompt, and the terminal/file/code_exec tools use it as their working directory — useful for running a job inside a specific project repo. Must be an absolute path that exists. When unset (default), preserves the original behaviour: no project context files, tools use the scheduler's cwd. On update, pass an empty string to clear. Jobs with workdir run sequentially (not parallel) to keep per-job directories isolated."
            },
            "attach_to_session": {
                "type": "boolean",
                "description": "When True, this job becomes CONTINUABLE: the user can reply to its delivery and the agent has the brief in context instead of asking 'what is that?'. On thread-capable platforms (Telegram topics, Discord/Slack threads) a dedicated thread is opened for the job and its replies; on DM-only platforms (WhatsApp/Signal) the brief is mirrored into the origin DM session. Use this for conversational recurring jobs the user will reply to — daily briefings, reminders that kick off follow-up work. Leave unset for fire-and-forget alerts/watchdogs. Overrides the global cron.mirror_delivery config for this one job. Only the origin chat is touched (never fan-out targets); no effect when deliver='local'."
            },
        },
        "required": ["action"]
    }
}


def check_cronjob_requirements() -> bool:
    """
    检查 cronjob 工具是否可用。

    在交互式 CLI 模式和 gateway/消息平台中可用。
    cron 系统是内部的（基于 JSON 文件、由 gateway 驱动的调度器），
    因此不需要外部 crontab 可执行文件。

    会话环境变量必须持有一个显式的真值字符串（``1``、``true``、
    ``yes``、``on``）——假值（``0``、``false``、``no``、``off``）
    会让工具保持禁用。使用共享的 ``env_var_enabled`` 辅助函数，以便
    这些标志的每个消费者对真值集合达成一致。
    """
    from utils import env_var_enabled

    return (
        env_var_enabled("HERMES_INTERACTIVE")
        or env_var_enabled("HERMES_GATEWAY_SESSION")
        or env_var_enabled("HERMES_EXEC_ASK")
    )


# --- 注册表 ---
from tools.registry import registry, tool_error

registry.register(
    name="cronjob",
    toolset="cronjob",
    schema=CRONJOB_SCHEMA,
    handler=lambda args, **kw: (lambda _mo=_resolve_model_override(args.get("model")): cronjob(
        action=args.get("action", ""),
        job_id=args.get("job_id"),
        prompt=args.get("prompt"),
        schedule=args.get("schedule"),
        name=args.get("name"),
        repeat=args.get("repeat"),
        deliver=args.get("deliver"),
        include_disabled=args.get("include_disabled", True),
        skill=args.get("skill"),
        skills=args.get("skills"),
        model=_mo[1],
        provider=_mo[0] or args.get("provider"),
        base_url=args.get("base_url"),
        reason=args.get("reason"),
        script=args.get("script"),
        context_from=args.get("context_from"),
        enabled_toolsets=args.get("enabled_toolsets"),
        workdir=args.get("workdir"),
        no_agent=args.get("no_agent"),
        task_id=kw.get("task_id"),
    ))(),
    check_fn=check_cronjob_requirements,
    emoji="⏰",
)
