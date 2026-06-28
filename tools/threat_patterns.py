"""用于上下文窗口安全扫描的共享威胁模式库。

本模块是提示注入（prompt injection）/ promptware / 数据外泄模式的
单一事实来源，供上下文组装扫描器（``agent/prompt_builder.py``、
``tools/memory_tool.py``）以及 ``agent/tool_dispatch_helpers.py`` 中
的工具结果分隔符系统共同使用。

模式设计理念
------------------
模式按攻击类别（ATTACK CLASS）组织，而不是按源文件组织。每个模式是一个
``(regex, pattern_id, scope)`` 三元组，其中 ``scope`` 决定哪些扫描器
使用它：

- ``"all"``  —— 到处应用（经典提示注入、数据外泄）
- ``"context"`` —— 应用于上下文文件 + 记忆 + 工具结果
  （promptware / C2 / 行为劫持；更宽泛的检测）
- ``"strict"`` —— 仅应用于记忆写入 + skill 安装
  （对用户精选的内容可接受更激进的检查，但对工具结果而言太嘈杂）

之所以这样划分，是因为工具结果中包含网页、GitHub issue 和 MCP 响应
——这些内容并非用户编写——我们希望在那里做宽泛检测，但拦截只保留
在用户能介入的路径上（记忆写入、skill 安装）。

模式锚定
-----------------
新模式锚定在 **C2 特定词汇或明确的攻击行为** 上，而不是锚定在命令式
的英语上。像 "you are obligated to" 或 "you must" 这样的短语在合法的
指令编写（见 AGENTS.md、CLAUDE.md 等）中太常见，不应被标记。对于
边界情况的设计理由见各模式注释。

多词绕过
-----------------
模式在关键 token 之间使用 ``(?:\\w+\\s+)*``，以防止攻击者插入填充词
（例如用 "ignore all prior instructions" 代替 "ignore all
instructions"）。这与提交 4ea29978 中对 ``skills_guard.py`` 所做的
修复相对应。
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

# 每个条目：(regex, pattern_id, scope)
# scope ∈ {"all", "context", "strict"}
_PATTERNS: List[Tuple[str, str, str]] = [
    # ── 经典提示注入（到处应用）────────────────────────────────
    (r'ignore\s+(?:\w+\s+)*(previous|all|above|prior)\s+(?:\w+\s+)*instructions', "prompt_injection", "all"),
    (r'system\s+prompt\s+override', "sys_prompt_override", "all"),
    (r'disregard\s+(?:\w+\s+)*(your|all|any)\s+(?:\w+\s+)*(instructions|rules|guidelines)', "disregard_rules", "all"),
    (r'act\s+as\s+(if|though)\s+(?:\w+\s+)*you\s+(?:\w+\s+)*(have\s+no|don\'t\s+have)\s+(?:\w+\s+)*(restrictions|limits|rules)', "bypass_restrictions", "all"),
    (r'<!--[^>]*(?:ignore|override|system|secret|hidden)[^>]*-->', "html_comment_injection", "all"),
    (r'<\s*div\s+style\s*=\s*["\'][\s\S]*?display\s*:\s*none', "hidden_div", "all"),
    (r'translate\s+.*\s+into\s+.*\s+and\s+(execute|run|eval)', "translate_execute", "all"),
    (r'do\s+not\s+(?:\w+\s+)*tell\s+(?:\w+\s+)*the\s+user', "deception_hide", "all"),

    # ── 角色扮演 / 身份劫持（context + strict；在抓取的网页内容和
    #    被投毒的上下文文件中是常见攻击面）──────────────────────
    (r'you\s+are\s+(?:\w+\s+)*now\s+(?:a|an|the)\s+', "role_hijack", "context"),
    (r'pretend\s+(?:\w+\s+)*(you\s+are|to\s+be)\s+', "role_pretend", "context"),
    (r'output\s+(?:\w+\s+)*(system|initial)\s+prompt', "leak_system_prompt", "context"),
    (r'(respond|answer|reply)\s+without\s+(?:\w+\s+)*(restrictions|limitations|filters|safety)', "remove_filters", "context"),
    (r'you\s+have\s+been\s+(?:\w+\s+)*(updated|upgraded|patched)\s+to', "fake_update", "context"),
    # "name yourself X" 是 Brainworm 特有的特征——通过规格而非越狱
    # 实现身份覆盖。锚定在动词对上，使其不会匹配到
    # "name your variables" 等。
    (r'\bname\s+yourself\s+\w+', "identity_override", "context"),

    # ── C2 / Brainworm 风格的 promptware（context 作用域）─────────────
    # 这些锚定在 C2 特定词汇上。"register as a node" 会出现在合法的
    # 分布式系统文档中，但与其他模式组合起来信号就很强；我们发出
    # 警告而非拦截，这样在网页中阅读 Brainworm 文章的安全研究员
    # 不会因此中断其会话。
    (r'register\s+(as\s+)?a?\s*node', "c2_node_registration", "context"),
    (r'(heartbeat|beacon|check[\s\-]?in)\s+(to|with)\s+', "c2_heartbeat", "context"),
    (r'pull\s+(down\s+)?(?:new\s+)?task(?:ing|s)?\b', "c2_task_pull", "context"),
    (r'connect\s+to\s+the\s+network\b', "c2_network_connect", "context"),
    # 以动词锚定的 "you must register/connect/report/beacon"——这些
    # 动词是 C2 特定的，因此避免了更宽泛的 "you must X" 误报。
    (r'you\s+must\s+(?:\w+\s+){0,3}(register|connect|report|beacon)\b', "forced_action", "context"),
    # 反取证指令（"never write to disk"、"one-liners only"）
    # ——在合法内容中极其罕见；近乎零误报。
    (r'only\s+use\s+one[\s\-]?liners?\b', "anti_forensic_oneliner", "context"),
    (r'never\s+(?:\w+\s+)*(?:create|write)\s+(?:\w+\s+)*(?:script|file)\s+(?:\w+\s+)*disk', "anti_forensic_disk", "context"),
    # 针对已知 agent 运行时的环境变量取消设置——
    # 这是纯攻击行为（Brainworm 子会话绕过）。
    (r'unset\s+\w*(?:CLAUDE|CODEX|HERMES|AGENT|OPENAI|ANTHROPIC)\w*', "env_var_unset_agent", "context"),

    # ── 已知的 C2 / 红队框架名称（在安全研究之外近乎零误报；
    #    默认仅警告）────────────────────────────────────────────
    (r'\b(?:praxis|cobalt\s*strike|sliver|havoc|mythic|metasploit|brainworm)\b', "known_c2_framework", "context"),
    (r'\bc2\s+(?:server|channel|infrastructure|beacon)\b', "c2_explicit", "context"),
    (r'\bcommand\s+and\s+control\b', "c2_explicit_long", "context"),

    # ── 通过 curl/wget/cat 携带密钥外泄（到处应用）──────────────
    (r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', "exfil_curl", "all"),
    (r'wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', "exfil_wget", "all"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)', "read_secrets", "all"),
    (r'(send|post|upload|transmit)\s+.*\s+(to|at)\s+https?://', "send_to_url", "strict"),
    (r'(include|output|print|share)\s+(?:\w+\s+)*(conversation|chat\s+history|previous\s+messages|full\s+context|entire\s+context)', "context_exfil", "strict"),

    # ── 持久化 / SSH 后门（strict 作用域——记忆 + skills）──────
    (r'authorized_keys', "ssh_backdoor", "strict"),
    (r'\$HOME/\.ssh|\~/\.ssh', "ssh_access", "strict"),
    (r'\$HOME/\.hermes/\.env|\~/\.hermes/\.env', "hermes_env", "strict"),
    (r'(update|modify|edit|write|change|append|add\s+to)\s+.*(?:AGENTS\.md|CLAUDE\.md|\.cursorrules|\.clinerules)', "agent_config_mod", "strict"),
    (r'(update|modify|edit|write|change|append|add\s+to)\s+.*\.hermes/(config\.yaml|SOUL\.md)', "hermes_config_mod", "strict"),

    # ── 硬编码密钥 ────────────────────────────────────────────────
    (r'(?:api[_-]?key|token|secret|password)\s*[=:]\s*["\'][A-Za-z0-9+/=_-]{20,}', "hardcoded_secret", "strict"),
]

# 注入攻击中使用的不可见 / 双向 Unicode 字符。
# 与 skills_guard.py 的 INVISIBLE_CHARS 对齐——方向隔离符
# （U+2066-U+2069）和不可见数学运算符（U+2062-U+2064）都是真实的
# 攻击工具。
INVISIBLE_CHARS = frozenset({
    '\u200b',  # 零宽空格
    '\u200c',  # 零宽非连接符
    '\u200d',  # 零宽连接符
    '\u2060',  # 字词连接符
    '\u2062',  # 不可见乘号
    '\u2063',  # 不可见分隔符
    '\u2064',  # 不可见加号
    '\ufeff',  # 零宽不换行空格（BOM）
    '\u202a',  # 从左到右嵌入
    '\u202b',  # 从右到左嵌入
    '\u202c',  # 弹出方向格式化
    '\u202d',  # 从左到右覆盖
    '\u202e',  # 从右到左覆盖
    '\u2066',  # 从左到右隔离
    '\u2067',  # 从右到左隔离
    '\u2068',  # 第一个强方向隔离
    '\u2069',  # 弹出方向隔离
})


# 按作用域索引的已编译模式集合。在导入时编译一次；
# scan_for_threats() 会查表使用。
_COMPILED: dict[str, List[Tuple[re.Pattern, str]]] = {}


def _compile() -> None:
    """为每个作用域（all / context / strict）编译模式集合。

    scope="all" 的模式会进入每个集合。scope="context" 的模式会进入
    context + strict（context 意味着 strict 扫描器也需要它）。
    scope="strict" 的模式只进入 strict。
    """
    global _COMPILED
    if _COMPILED:
        return

    all_patterns: List[Tuple[re.Pattern, str]] = []
    context_patterns: List[Tuple[re.Pattern, str]] = []
    strict_patterns: List[Tuple[re.Pattern, str]] = []

    for pattern, pid, scope in _PATTERNS:
        compiled = re.compile(pattern, re.IGNORECASE)
        entry = (compiled, pid)
        if scope == "all":
            all_patterns.append(entry)
            context_patterns.append(entry)
            strict_patterns.append(entry)
        elif scope == "context":
            context_patterns.append(entry)
            strict_patterns.append(entry)
        elif scope == "strict":
            strict_patterns.append(entry)
        else:
            raise ValueError(f"threat_patterns: unknown scope {scope!r} for pattern {pid!r}")

    _COMPILED = {
        "all": all_patterns,
        "context": context_patterns,
        "strict": strict_patterns,
    }


_compile()


def scan_for_threats(content: str, scope: str = "context") -> List[str]:
    """返回 ``content`` 中在指定作用域下匹配到的模式 ID 列表。

    ``scope`` 选择要应用哪个模式集合：

    - ``"all"``（窄）：仅经典注入 + 外泄——误报最少，适用于任何文本。
    - ``"context"``（默认）：追加 promptware / C2 / 角色扮演模式——
      适用于上下文文件、记忆条目和工具结果。
    - ``"strict"``（宽）：追加持久化 / SSH 后门 / 外泄-URL 模式——
      适合用户介入的写入（记忆工具、skills 安装），这类场景下的
      误报可交互解决。

    还会检查不可见 Unicode 字符（以 ``"invisible_unicode_U+XXXX"``
    形式返回，以便调用方能在日志行中展示违规的码点）。
    """
    if not content:
        return []

    findings: List[str] = []

    # 不可见 Unicode——对内容字符集合做一次遍历，而不是 17 次
    # ``in`` 查找。
    char_set = set(content)
    invisible_hits = char_set & INVISIBLE_CHARS
    for ch in invisible_hits:
        findings.append(f"invisible_unicode_U+{ord(ch):04X}")

    # 威胁模式
    patterns = _COMPILED.get(scope)
    if patterns is None:
        raise ValueError(f"scan_for_threats: unknown scope {scope!r}")
    for compiled, pid in patterns:
        if compiled.search(content):
            findings.append(pid)

    return findings


def first_threat_message(content: str, scope: str = "strict") -> Optional[str]:
    """返回首个命中威胁的可读错误字符串，没有则返回 None。

    这是一个便捷封装，供在首次命中时即拦截的路径使用
    （记忆工具写入、skills 安装），这类调用方只需要一个是/否判断
    加一条消息。
    """
    findings = scan_for_threats(content, scope=scope)
    if not findings:
        return None
    pid = findings[0]
    if pid.startswith("invisible_unicode_"):
        codepoint = pid.replace("invisible_unicode_", "")
        return f"Blocked: content contains invisible unicode character {codepoint} (possible injection)."
    return (
        f"Blocked: content matches threat pattern '{pid}'. "
        f"Content is injected into the system prompt and must not contain "
        f"injection or exfiltration payloads."
    )


__all__ = [
    "INVISIBLE_CHARS",
    "scan_for_threats",
    "first_threat_message",
]
