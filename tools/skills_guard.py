#!/usr/bin/env python3
"""
Skills Guard — 外部来源 skill 的安全扫描器。

每个从 registry 下载的 skill 在安装前都会经过此扫描器。它使用基于
正则的静态分析来检测已知的恶意模式（数据外泄、提示词注入、破坏性
命令、持久化等），并使用一个感知信任级别的安装策略，根据扫描
结论和来源的信任级别决定是否允许安装某个 skill。

信任级别：
  - builtin：随 Hermes 发行。从不扫描，始终信任。
  - trusted：仅 openai/skills 和 anthropics/skills。允许 caution 结论。
  - community：其他所有来源。任何发现 = 阻止，除非 --force。

用法：
    from tools.skills_guard import scan_skill, should_allow_install, format_scan_report

    result = scan_skill(Path("skills/.hub/quarantine/some-skill"), source="community")
    allowed, reason = should_allow_install(result)
    if not allowed:
        print(format_scan_report(result))
"""

import re
import fnmatch
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple




# ---------------------------------------------------------------------------
# 硬编码的信任配置
# ---------------------------------------------------------------------------

TRUSTED_REPOS = {
    "openai/skills",
    "anthropics/skills",
    "huggingface/skills",
    # NVIDIA 验证的 skill：每个条目都附带签名 `skill.oms.sig`
    # 和治理文件 `skill-card.md`（同步流水线会丢弃任何
    # 缺少签名或卡片的条目）。目录详情：
    # https://github.com/NVIDIA/skills
    "NVIDIA/skills",
}

INSTALL_POLICY = {
    #                  safe      caution    dangerous
    "builtin":       ("allow",  "allow",   "allow"),
    "trusted":       ("allow",  "allow",   "block"),
    "community":     ("allow",  "block",   "block"),
    # Agent 创建的：对 dangerous 表面返回 "ask" 作为给 agent 的错误，
    # agent 可以去掉被标记内容后重试。此门控仅在启用
    # skills.guard_agent_created 时运行（默认关闭）——见
    # tools/skill_manager_tool.py::_guard_agent_created_enabled。
    "agent-created": ("allow",  "allow",   "ask"),
}

VERDICT_INDEX = {"safe": 0, "caution": 1, "dangerous": 2}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    pattern_id: str
    severity: str       # "critical" | "high" | "medium" | "low"
    category: str       # "exfiltration" | "injection" | "destructive" | "persistence" | "network" | "obfuscation"
    file: str
    line: int
    match: str
    description: str


@dataclass
class ScanResult:
    skill_name: str
    source: str
    trust_level: str    # "builtin" | "trusted" | "community"
    verdict: str        # "safe" | "caution" | "dangerous"
    findings: List[Finding] = field(default_factory=list)
    scanned_at: str = ""
    summary: str = ""


# ---------------------------------------------------------------------------
# 威胁模式 — (regex, pattern_id, severity, category, description)
# ---------------------------------------------------------------------------

THREAT_PATTERNS = [
    # ── 外泄：泄露密钥的 shell 命令 ──
    (r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)',
     "env_exfil_curl", "critical", "exfiltration",
     "curl command interpolating secret environment variable"),
    (r'wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)',
     "env_exfil_wget", "critical", "exfiltration",
     "wget command interpolating secret environment variable"),
    (r'fetch\s*\([^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|API)',
     "env_exfil_fetch", "critical", "exfiltration",
     "fetch() call interpolating secret environment variable"),
    (r'httpx?\.(get|post|put|patch)\s*\([^\n]*(KEY|TOKEN|SECRET|PASSWORD)',
     "env_exfil_httpx", "critical", "exfiltration",
     "HTTP library call with secret variable"),
    (r'requests\.(get|post|put|patch)\s*\([^\n]*(KEY|TOKEN|SECRET|PASSWORD)',
     "env_exfil_requests", "critical", "exfiltration",
     "requests library call with secret variable"),

    # ── 外泄：读取凭据存储 ──
    (r'base64[^\n]*env',
     "encoded_exfil", "high", "exfiltration",
     "base64 encoding combined with environment access"),
    (r'\$HOME/\.ssh|\~/\.ssh',
     "ssh_dir_access", "high", "exfiltration",
     "references user SSH directory"),
    (r'\$HOME/\.aws|\~/\.aws',
     "aws_dir_access", "high", "exfiltration",
     "references user AWS credentials directory"),
    (r'\$HOME/\.gnupg|\~/\.gnupg',
     "gpg_dir_access", "high", "exfiltration",
     "references user GPG keyring"),
    (r'\$HOME/\.kube|\~/\.kube',
     "kube_dir_access", "high", "exfiltration",
     "references Kubernetes config directory"),
    (r'\$HOME/\.docker|\~/\.docker',
     "docker_dir_access", "high", "exfiltration",
     "references Docker config (may contain registry creds)"),
    (r'\$HOME/\.hermes/\.env|\~/\.hermes/\.env',
     "hermes_env_access", "critical", "exfiltration",
     "directly references Hermes secrets file"),
    # 匹配 `cat <secrets-file>`（读取凭据），但不匹配 `cat > <file>`
    # 或 `cat >> <file>`，后者是写入文件的输出重定向
    # （例如一份安装文档让用户通过 heredoc 把自己的密钥写进
    # 自己本地的 `.env`）。写入自己的配置与外泄密钥正好
    # 相反。
    (r'cat\s+(?!>)[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)',
     "read_secrets_file", "critical", "exfiltration",
     "reads known secrets file"),

    # ── 外泄：编程式 env 访问 ──
    (r'printenv|env\s*\|',
     "dump_all_env", "high", "exfiltration",
     "dumps all environment variables"),
    # 裸 `os.environ` 访问（字典转储/迭代）是可疑的，但常见的
    # `os.environ.get("SOME_CONFIG")` 形式只是读取配置，与外泄正好
    # 相反（它读取一个本地变量，不发送任何东西）。该前瞻仅在
    # <name> 不是密钥形状的标识符时豁免 `os.environ.get("<name>")` ——
    # `os.environ.get("OPENAI_API_KEY")` 仍会通过下方专门的密钥模式触发。
    (r'os\.environ\b(?!\s*\.get\s*\(\s*["\'](?![^"\']*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)))',
     "python_os_environ", "high", "exfiltration",
     "accesses os.environ (potential env dump)"),
    (r'os\.environ\s*\.get\s*\(\s*["\'][^"\']*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)',
     "python_environ_get_secret", "critical", "exfiltration",
     "reads secret via os.environ.get()"),
    (r'os\.getenv\s*\(\s*[^\)]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)',
     "python_getenv_secret", "critical", "exfiltration",
     "reads secret via os.getenv()"),
    (r'process\.env\[',
     "node_process_env", "high", "exfiltration",
     "accesses process.env (Node.js environment)"),
    (r'ENV\[.*(?:KEY|TOKEN|SECRET|PASSWORD)',
     "ruby_env_secret", "critical", "exfiltration",
     "reads secret via Ruby ENV[]"),

    # ── 外泄：DNS 与中转 ──
    (r'\b(dig|nslookup|host)\s+[^\n]*\$',
     "dns_exfil", "critical", "exfiltration",
     "DNS lookup with variable interpolation (possible DNS exfiltration)"),
    (r'>\s*/tmp/[^\s]*\s*&&\s*(curl|wget|nc|python)',
     "tmp_staging", "critical", "exfiltration",
     "writes to /tmp then exfiltrates"),

    # ── 外泄：基于 markdown/链接 ──
    (r'!\[.*\]\(https?://[^\)]*\$\{?',
     "md_image_exfil", "high", "exfiltration",
     "markdown image URL with variable interpolation (image-based exfil)"),
    (r'\[.*\]\(https?://[^\)]*\$\{?',
     "md_link_exfil", "high", "exfiltration",
     "markdown link with variable interpolation"),

    # ── 提示词注入 ──
    (r'ignore\s+(?:\w+\s+)*(previous|all|above|prior)\s+instructions',
     "prompt_injection_ignore", "critical", "injection",
     "prompt injection: ignore previous instructions"),
    (r'you\s+are\s+(?:\w+\s+)*now\s+',
     "role_hijack", "high", "injection",
     "attempts to override the agent's role"),
    (r'do\s+not\s+(?:\w+\s+)*tell\s+(?:\w+\s+)*the\s+user',
     "deception_hide", "critical", "injection",
     "instructs agent to hide information from user"),
    (r'system\s+(?:\w+\s+)*prompt\s+(?:\w+\s+)*override',
     "sys_prompt_override", "critical", "injection",
     "attempts to override the system prompt"),
    (r'pretend\s+(?:\w+\s+)*(you\s+are|to\s+be)\s+',
     "role_pretend", "high", "injection",
     "attempts to make the agent assume a different identity"),
    (r'disregard\s+(?:\w+\s+)*(your|all|any)\s+(?:\w+\s+)*(instructions|rules|guidelines)',
     "disregard_rules", "critical", "injection",
     "instructs agent to disregard its rules"),
    (r'output\s+(?:\w+\s+)*(system|initial)\s+prompt',
     "leak_system_prompt", "high", "injection",
     "attempts to extract the system prompt"),
    (r'(when|if)\s+no\s*one\s+is\s+(watching|looking)',
     "conditional_deception", "high", "injection",
     "conditional instruction to behave differently when unobserved"),
    (r'act\s+as\s+(if|though)\s+(?:\w+\s+)*you\s+(?:\w+\s+)*(have\s+no|don\'t\s+have)\s+(?:\w+\s+)*(restrictions|limits|rules)',
     "bypass_restrictions", "critical", "injection",
     "instructs agent to act without restrictions"),
    (r'translate\s+.*\s+into\s+.*\s+and\s+(execute|run|eval)',
     "translate_execute", "critical", "injection",
     "translate-then-execute evasion technique"),
    (r'<!--[^>]*(?:ignore|override|system|secret|hidden)[^>]*-->',
     "html_comment_injection", "high", "injection",
     "hidden instructions in HTML comments"),
    (r'<\s*div\s+style\s*=\s*["\'][\s\S]*?display\s*:\s*none',
     "hidden_div", "high", "injection",
     "hidden HTML div (invisible instructions)"),

    # ── 破坏性操作 ──
    (r'rm\s+-rf\s+/',
     "destructive_root_rm", "critical", "destructive",
     "recursive delete from root"),
    (r'rm\s+(-[^\s]*)?r.*\$HOME|\brmdir\s+.*\$HOME',
     "destructive_home_rm", "critical", "destructive",
     "recursive delete targeting home directory"),
    (r'chmod\s+777',
     "insecure_perms", "medium", "destructive",
     "sets world-writable permissions"),
    (r'>\s*/etc/',
     "system_overwrite", "critical", "destructive",
     "overwrites system configuration file"),
    (r'\bmkfs\b',
     "format_filesystem", "critical", "destructive",
     "formats a filesystem"),
    (r'\bdd\s+.*if=.*of=/dev/',
     "disk_overwrite", "critical", "destructive",
     "raw disk write operation"),
    (r'shutil\.rmtree\s*\(\s*[\"\'/]',
     "python_rmtree", "high", "destructive",
     "Python rmtree on absolute or root-relative path"),
    (r'truncate\s+-s\s*0\s+/',
     "truncate_system", "critical", "destructive",
     "truncates system file to zero bytes"),

    # ── 持久化 ──
    (r'\bcrontab\b',
     "persistence_cron", "medium", "persistence",
     "modifies cron jobs"),
    (r'\.(bashrc|zshrc|profile|bash_profile|bash_login|zprofile|zlogin)\b',
     "shell_rc_mod", "medium", "persistence",
     "references shell startup file"),
    (r'authorized_keys',
     "ssh_backdoor", "critical", "persistence",
     "modifies SSH authorized keys"),
    (r'ssh-keygen',
     "ssh_keygen", "medium", "persistence",
     "generates SSH keys"),
    (r'systemd.*\.service|systemctl\s+(enable|start)',
     "systemd_service", "medium", "persistence",
     "references or enables systemd service"),
    (r'/etc/init\.d/',
     "init_script", "medium", "persistence",
     "references init.d startup script"),
    (r'launchctl\s+load|LaunchAgents|LaunchDaemons',
     "macos_launchd", "medium", "persistence",
     "macOS launch agent/daemon persistence"),
    (r'/etc/sudoers|visudo',
     "sudoers_mod", "critical", "persistence",
     "modifies sudoers (privilege escalation)"),
    (r'git\s+config\s+--global\s+',
     "git_config_global", "medium", "persistence",
     "modifies global git configuration"),

    # ── 网络：反向 shell 与隧道 ──
    (r'\bnc\s+-[lp]|ncat\s+-[lp]|\bsocat\b',
     "reverse_shell", "critical", "network",
     "potential reverse shell listener"),
    (r'\bngrok\b|\blocaltunnel\b|\bserveo\b|\bcloudflared\b',
     "tunnel_service", "high", "network",
     "uses tunneling service for external access"),
    (r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{2,5}',
     "hardcoded_ip_port", "medium", "network",
     "hardcoded IP address with port"),
    (r'0\.0\.0\.0:\d+|INADDR_ANY',
     "bind_all_interfaces", "high", "network",
     "binds to all network interfaces"),
    (r'/bin/(ba)?sh\s+-i\s+.*>/dev/tcp/',
     "bash_reverse_shell", "critical", "network",
     "bash interactive reverse shell via /dev/tcp"),
    (r'python[23]?\s+-c\s+["\']import\s+socket',
     "python_socket_oneliner", "critical", "network",
     "Python one-liner socket connection (likely reverse shell)"),
    (r'socket\.connect\s*\(\s*\(',
     "python_socket_connect", "high", "network",
     "Python socket connect to arbitrary host"),
    (r'webhook\.site|requestbin\.com|pipedream\.net|hookbin\.com',
     "exfil_service", "high", "network",
     "references known data exfiltration/webhook testing service"),
    (r'pastebin\.com|hastebin\.com|ghostbin\.',
     "paste_service", "medium", "network",
     "references paste service (possible data staging)"),

    # ── 混淆：编码与 eval ──
    (r'base64\s+(-d|--decode)\s*\|',
     "base64_decode_pipe", "high", "obfuscation",
     "base64 decodes and pipes to execution"),
    (r'\\x[0-9a-fA-F]{2}.*\\x[0-9a-fA-F]{2}.*\\x[0-9a-fA-F]{2}',
     "hex_encoded_string", "medium", "obfuscation",
     "hex-encoded string (possible obfuscation)"),
    (r'\beval\s*\(\s*["\']',
     "eval_string", "high", "obfuscation",
     "eval() with string argument"),
    (r'\bexec\s*\(\s*["\']',
     "exec_string", "high", "obfuscation",
     "exec() with string argument"),
    (r'echo\s+[^\n]*\|\s*(bash|sh|python|perl|ruby|node)',
     "echo_pipe_exec", "critical", "obfuscation",
     "echo piped to interpreter for execution"),
    (r'compile\s*\(\s*[^\)]+,\s*["\'].*["\']\s*,\s*["\']exec["\']\s*\)',
     "python_compile_exec", "high", "obfuscation",
     "Python compile() with exec mode"),
    (r'getattr\s*\(\s*__builtins__',
     "python_getattr_builtins", "high", "obfuscation",
     "dynamic access to Python builtins (evasion technique)"),
    (r'__import__\s*\(\s*["\']os["\']\s*\)',
     "python_import_os", "high", "obfuscation",
     "dynamic import of os module"),
    (r'codecs\.decode\s*\(\s*["\']',
     "python_codecs_decode", "medium", "obfuscation",
     "codecs.decode (possible ROT13 or encoding obfuscation)"),
    (r'String\.fromCharCode|charCodeAt',
     "js_char_code", "medium", "obfuscation",
     "JavaScript character code construction (possible obfuscation)"),
    (r'atob\s*\(|btoa\s*\(',
     "js_base64", "medium", "obfuscation",
     "JavaScript base64 encode/decode"),
    (r'\[::-1\]',
     "string_reversal", "low", "obfuscation",
     "string reversal (possible obfuscated payload)"),
    (r'chr\s*\(\s*\d+\s*\)\s*\+\s*chr\s*\(\s*\d+',
     "chr_building", "high", "obfuscation",
     "building string from chr() calls (obfuscation)"),
    (r'\\u[0-9a-fA-F]{4}.*\\u[0-9a-fA-F]{4}.*\\u[0-9a-fA-F]{4}',
     "unicode_escape_chain", "medium", "obfuscation",
     "chain of unicode escapes (possible obfuscation)"),

    # ── 脚本中的进程执行 ──
    (r'subprocess\.(run|call|Popen|check_output)\s*\(',
     "python_subprocess", "medium", "execution",
     "Python subprocess execution"),
    (r'os\.system\s*\(',
     "python_os_system", "high", "execution",
     "os.system() — unguarded shell execution"),
    (r'os\.popen\s*\(',
     "python_os_popen", "high", "execution",
     "os.popen() — shell pipe execution"),
    (r'child_process\.(exec|spawn|fork)\s*\(',
     "node_child_process", "high", "execution",
     "Node.js child_process execution"),
    (r'Runtime\.getRuntime\(\)\.exec\(',
     "java_runtime_exec", "high", "execution",
     "Java Runtime.exec() — shell execution"),
    (r'`[^`]*\$\([^)]+\)[^`]*`',
     "backtick_subshell", "medium", "execution",
     "backtick string with command substitution"),

    # ── 路径穿越 ──
    (r'\.\./\.\./\.\.',
     "path_traversal_deep", "high", "traversal",
     "deep relative path traversal (3+ levels up)"),
    (r'\.\./\.\.',
     "path_traversal", "medium", "traversal",
     "relative path traversal (2+ levels up)"),
    (r'/etc/passwd|/etc/shadow',
     "system_passwd_access", "critical", "traversal",
     "references system password files"),
    (r'/proc/self|/proc/\d+/',
     "proc_access", "high", "traversal",
     "references /proc filesystem (process introspection)"),
    (r'/dev/shm/',
     "dev_shm", "medium", "traversal",
     "references shared memory (common staging area)"),

    # ── 加密货币挖矿 ──
    (r'xmrig|stratum\+tcp|monero|coinhive|cryptonight',
     "crypto_mining", "critical", "mining",
     "cryptocurrency mining reference"),
    (r'hashrate|nonce.*difficulty',
     "mining_indicators", "medium", "mining",
     "possible cryptocurrency mining indicators"),

    # ── 供应链：curl/wget 管道到 shell ──
    (r'curl\s+[^\n]*\|\s*(ba)?sh',
     "curl_pipe_shell", "critical", "supply_chain",
     "curl piped to shell (download-and-execute)"),
    (r'wget\s+[^\n]*-O\s*-\s*\|\s*(ba)?sh',
     "wget_pipe_shell", "critical", "supply_chain",
     "wget piped to shell (download-and-execute)"),
    (r'curl\s+[^\n]*\|\s*python',
     "curl_pipe_python", "critical", "supply_chain",
     "curl piped to Python interpreter"),

    # ── 供应链：未固定/延迟的依赖 ──
    (r'#\s*///\s*script.*dependencies',
     "pep723_inline_deps", "medium", "supply_chain",
     "PEP 723 inline script metadata with dependencies (verify pinning)"),
    (r'pip\s+install\s+(?!-r\s)(?!.*==)',
     "unpinned_pip_install", "medium", "supply_chain",
     "pip install without version pinning"),
    (r'npm\s+install\s+(?!.*@\d)',
     "unpinned_npm_install", "medium", "supply_chain",
     "npm install without version pinning"),
    (r'uv\s+run\s+',
     "uv_run", "medium", "supply_chain",
     "uv run (may auto-install unpinned dependencies)"),

    # ── 供应链：远程资源获取 ──
    (r'(curl|wget|httpx?\.get|requests\.get|fetch)\s*[\(]?\s*["\']https?://',
     "remote_fetch", "medium", "supply_chain",
     "fetches remote resource at runtime"),
    (r'git\s+clone\s+',
     "git_clone", "medium", "supply_chain",
     "clones a git repository at runtime"),
    (r'docker\s+pull\s+',
     "docker_pull", "medium", "supply_chain",
     "pulls a Docker image at runtime"),

    # ── 提权 ──
    # `allowed-tools:` 是 agent-skill 规范要求的 SKILL.md frontmatter ——
    # 每个合规 skill 都会声明它，因此它本身不可能是威胁
    # 信号。保留为信息性（low）发现以便审计；
    # 它不再驱动结论。
    (r'^allowed-tools\s*:',
     "allowed_tools_field", "low", "privilege_escalation",
     "skill declares allowed-tools (standard frontmatter; informational)"),
    (r'\bsudo\b',
     "sudo_usage", "high", "privilege_escalation",
     "uses sudo (privilege escalation)"),
    (r'setuid|setgid|cap_setuid',
     "setuid_setgid", "critical", "privilege_escalation",
     "setuid/setgid (privilege escalation mechanism)"),
    (r'NOPASSWD',
     "nopasswd_sudo", "critical", "privilege_escalation",
     "NOPASSWD sudoers entry (passwordless privilege escalation)"),
    (r'chmod\s+[u+]?s',
     "suid_bit", "critical", "privilege_escalation",
     "sets SUID/SGID bit on a file"),

    # ── Agent 配置持久化 ──
    (r'AGENTS\.md|CLAUDE\.md|\.cursorrules|\.clinerules',
     "agent_config_mod", "critical", "persistence",
     "references agent config files (could persist malicious instructions across sessions)"),
    (r'\.hermes/config\.yaml|\.hermes/SOUL\.md',
     "hermes_config_mod", "critical", "persistence",
     "references Hermes configuration files directly"),
    (r'\.claude/settings|\.codex/config',
     "other_agent_config", "high", "persistence",
     "references other agent configuration files"),

    # ── 硬编码密钥（嵌入 skill 本身的凭据）──
    (r'(?:api[_-]?key|token|secret|password)\s*[=:]\s*["\'][A-Za-z0-9+/=_-]{20,}',
     "hardcoded_secret", "critical", "credential_exposure",
     "possible hardcoded API key, token, or secret"),
    (r'-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----',
     "embedded_private_key", "critical", "credential_exposure",
     "embedded private key"),
    (r'ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{80,}',
     "github_token_leaked", "critical", "credential_exposure",
     "GitHub personal access token in skill content"),
    (r'sk-[A-Za-z0-9]{20,}',
     "openai_key_leaked", "critical", "credential_exposure",
     "possible OpenAI API key in skill content"),
    (r'sk-ant-[A-Za-z0-9_-]{90,}',
     "anthropic_key_leaked", "critical", "credential_exposure",
     "possible Anthropic API key in skill content"),
    (r'AKIA[0-9A-Z]{16}',
     "aws_access_key_leaked", "critical", "credential_exposure",
     "AWS access key ID in skill content"),

    # ── 额外的提示词注入：越狱模式 ──
    (r'\bDAN\s+mode\b|Do\s+Anything\s+Now',
     "jailbreak_dan", "critical", "injection",
     "DAN (Do Anything Now) jailbreak attempt"),
    (r'\bdeveloper\s+mode\b.*\benabled?\b',
     "jailbreak_dev_mode", "critical", "injection",
     "developer mode jailbreak attempt"),
    (r'hypothetical\s+scenario.*(?:ignore|bypass|override)',
     "hypothetical_bypass", "high", "injection",
     "hypothetical scenario used to bypass restrictions"),
    (r'for\s+educational\s+purposes?\s+only',
     "educational_pretext", "medium", "injection",
     "educational pretext often used to justify harmful content"),
    (r'(respond|answer|reply)\s+without\s+(?:\w+\s+)*(restrictions|limitations|filters|safety)',
     "remove_filters", "critical", "injection",
     "instructs agent to respond without safety filters"),
    (r'you\s+have\s+been\s+(?:\w+\s+)*(updated|upgraded|patched)\s+to',
     "fake_update", "high", "injection",
     "fake update/patch announcement (social engineering)"),
    (r'new\s+(?:\w+\s+)*policy|updated\s+(?:\w+\s+)*guidelines|revised\s+(?:\w+\s+)*instructions',
     "fake_policy", "medium", "injection",
     "claims new policy/guidelines (may be social engineering)"),

    # ── 上下文窗口外泄 ──
    (r'(include|output|print|send|share)\s+(?:\w+\s+)*(conversation|chat\s+history|previous\s+messages|context)',
     "context_exfil", "high", "exfiltration",
     "instructs agent to output/share conversation history"),
    (r'(send|post|upload|transmit)\s+.*\s+(to|at)\s+https?://',
     "send_to_url", "high", "exfiltration",
     "instructs agent to send data to a URL"),
]

# skill 目录的结构限制
MAX_FILE_COUNT = 50       # skill 不应该有 50+ 个文件
MAX_TOTAL_SIZE_KB = 1024  # 总计 1MB 对一个 skill 来说可疑
MAX_SINGLE_FILE_KB = 256  # 单个文件 > 256KB 可疑

# 要扫描的文件扩展名（仅文本文件——跳过二进制）
SCANNABLE_EXTENSIONS = {
    '.md', '.txt', '.py', '.sh', '.bash', '.js', '.ts', '.rb',
    '.yaml', '.yml', '.json', '.toml', '.cfg', '.ini', '.conf',
    '.html', '.css', '.xml', '.tex', '.r', '.jl', '.pl', '.php',
}

# 不应出现在 skill 中的已知二进制扩展名
SUSPICIOUS_BINARY_EXTENSIONS = {
    '.exe', '.dll', '.so', '.dylib', '.bin', '.dat', '.com',
    '.msi', '.dmg', '.app', '.deb', '.rpm',
}

# 用于注入的零宽和不可见 unicode 字符
INVISIBLE_CHARS = {
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
    '\u202c',  # 弹出定向格式化
    '\u202d',  # 从左到右覆盖
    '\u202e',  # 从右到左覆盖
    '\u2066',  # 从左到右隔离
    '\u2067',  # 从右到左隔离
    '\u2068',  # 首个强字符隔离
    '\u2069',  # 弹出定向隔离
}


# ---------------------------------------------------------------------------
# 扫描函数
# ---------------------------------------------------------------------------

def scan_file(file_path: Path, rel_path: str = "") -> List[Finding]:
    """
    扫描单个文件的威胁模式和不可见 unicode 字符。

    参数：
        file_path: 文件的绝对路径
        rel_path: 用于显示的相对路径（默认为 file_path.name）

    返回：
        发现列表（每个模式每行去重）
    """
    if not rel_path:
        rel_path = file_path.name

    if file_path.suffix.lower() not in SCANNABLE_EXTENSIONS and file_path.name != "SKILL.md":
        return []

    try:
        content = file_path.read_text(encoding='utf-8')
    except (UnicodeDecodeError, OSError):
        return []

    findings = []
    lines = content.split('\n')
    seen = set()  # (pattern_id, line_number) 用于去重

    # 正则模式匹配
    for pattern, pid, severity, category, description in THREAT_PATTERNS:
        for i, line in enumerate(lines, start=1):
            if (pid, i) in seen:
                continue
            if re.search(pattern, line, re.IGNORECASE):
                seen.add((pid, i))
                matched_text = line.strip()
                if len(matched_text) > 120:
                    matched_text = matched_text[:117] + "..."
                findings.append(Finding(
                    pattern_id=pid,
                    severity=severity,
                    category=category,
                    file=rel_path,
                    line=i,
                    match=matched_text,
                    description=description,
                ))

    # 不可见 unicode 字符检测
    for i, line in enumerate(lines, start=1):
        for char in INVISIBLE_CHARS:
            if char in line:
                char_name = _unicode_char_name(char)
                findings.append(Finding(
                    pattern_id="invisible_unicode",
                    severity="high",
                    category="injection",
                    file=rel_path,
                    line=i,
                    match=f"U+{ord(char):04X} ({char_name})",
                    description=f"invisible unicode character {char_name} (possible text hiding/injection)",
                ))
                break  # 不可见字符每行一个发现

    return findings


def scan_skill(skill_path: Path, source: str = "community") -> ScanResult:
    """
    扫描 skill 目录中所有文件的安全威胁。

    执行：
    1. 结构检查（文件数、总大小、二进制文件、符号链接）
    2. 所有文本文件的正则模式匹配
    3. 不可见 unicode 字符检测

    一个 skill 可以附带 `.skillignore`（或 `.clawhubignore`）文件，内含
    gitignore 风格的模式。匹配的路径会从结构检查和模式扫描中同时
    排除，这样不属于已安装 skill 的开发/文档产物
    （例如 `SKILL-original.md`、`docs/plans/`、`release-notes.md`）就不会
    触发发现。忽略文件本身始终被排除。模式无法取消忽略
    skill 自己的 `SKILL.md`，它始终被扫描。

    参数：
        skill_path: skill 目录的路径（必须包含 SKILL.md）
        source: 用于解析信任级别的来源标识符（例如 "openai/skills"）

    返回：
        带有结论、发现和信任元数据的 ScanResult
    """
    skill_name = skill_path.name
    trust_level = _resolve_trust_level(source)

    all_findings: List[Finding] = []

    if skill_path.is_dir():
        ignore = _load_skill_ignore(skill_path)

        # 先做结构检查（尊重忽略列表）
        all_findings.extend(_check_structure(skill_path, ignore=ignore))

        # 对每个文件做模式扫描
        for f in skill_path.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(skill_path))
                if ignore(rel):
                    continue
                all_findings.extend(scan_file(f, rel))
    elif skill_path.is_file():
        all_findings.extend(scan_file(skill_path, skill_path.name))

    verdict = _determine_verdict(all_findings)
    summary = _build_summary(skill_name, source, trust_level, verdict, all_findings)

    return ScanResult(
        skill_name=skill_name,
        source=source,
        trust_level=trust_level,
        verdict=verdict,
        findings=all_findings,
        scanned_at=datetime.now(timezone.utc).isoformat(),
        summary=summary,
    )


def should_allow_install(result: ScanResult, force: bool = False) -> Tuple[bool, str]:
    """
    根据扫描结果和信任级别决定是否应安装某个 skill。

    参数：
        result: 来自 scan_skill() 的扫描结果
        force: 若为 True，覆盖此次扫描结果中被阻止的策略决定

    返回：
        (allowed, reason) 元组
    """
    policy = INSTALL_POLICY.get(result.trust_level, INSTALL_POLICY["community"])
    vi = VERDICT_INDEX.get(result.verdict, 2)
    decision = policy[vi]

    if decision == "allow":
        return True, f"Allowed ({result.trust_level} source, {result.verdict} verdict)"

    if force and not (result.verdict == "dangerous" and result.trust_level in ("community", "trusted")):
        return True, (
            f"Force-installed despite {result.verdict} verdict "
            f"({len(result.findings)} findings)"
        )

    if decision == "ask":
        # 返回 None 表示"需要用户确认"
        return None, (
            f"Requires confirmation ({result.trust_level} source + {result.verdict} verdict, "
            f"{len(result.findings)} findings)"
        )

    # dangerous 结论不能被 --force 覆盖（community/trusted）；
    # 其他阻止可以。
    if result.verdict == "dangerous" and result.trust_level in ("community", "trusted"):
        return False, (
            f"Blocked ({result.trust_level} source + dangerous verdict, "
            f"{len(result.findings)} findings). --force does not override a dangerous verdict."
        )
    return False, (
        f"Blocked ({result.trust_level} source + {result.verdict} verdict, "
        f"{len(result.findings)} findings). Use --force to override."
    )


def format_scan_report(result: ScanResult) -> str:
    """
    将扫描结果格式化为人类可读的报告字符串。

    返回适合 CLI 或聊天展示的紧凑多行报告。
    """
    lines = []

    verdict_display = result.verdict.upper()
    lines.append(f"Scan: {result.skill_name} ({result.source}/{result.trust_level})  Verdict: {verdict_display}")

    if result.findings:
        # 分组并排序：critical 优先，然后 high、medium、low
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        sorted_findings = sorted(result.findings, key=lambda f: severity_order.get(f.severity, 4))

        for f in sorted_findings:
            sev = f.severity.upper().ljust(8)
            cat = f.category.ljust(14)
            loc = f"{f.file}:{f.line}".ljust(30)
            lines.append(f"  {sev} {cat} {loc} \"{f.match[:60]}\"")

        lines.append("")

    allowed, reason = should_allow_install(result)
    if allowed is True:
        status = "ALLOWED"
    elif allowed is None:
        status = "NEEDS CONFIRMATION"
    else:
        status = "BLOCKED"
    lines.append(f"Decision: {status} — {reason}")

    return "\n".join(lines)


def content_hash(skill_path: Path) -> str:
    """计算 skill 目录中所有文件的 SHA-256 哈希，用于完整性跟踪。

    文件路径（相对于 ``skill_path``）会与文件内容一起混入哈希，
    这样交换 skill 中两个文件的内容就会改变哈希。这必须与
    ``tools.skills_hub.bundle_content_hash`` 保持对称——两个函数需要
    对同一个 skill 产生相同的摘要（一个操作磁盘，
    一个操作内存中的 bundle），因此对哈希形状的任何更改都必须
    同时落地到两处。
    """
    h = hashlib.sha256()
    if skill_path.is_dir():
        for f in sorted(skill_path.rglob("*")):
            if f.is_file():
                try:
                    rel = f.relative_to(skill_path).as_posix()
                    h.update(rel.encode("utf-8"))
                    h.update(b"\x00")
                    h.update(f.read_bytes())
                except OSError:
                    continue
    elif skill_path.is_file():
        h.update(skill_path.read_bytes())
    return f"sha256:{h.hexdigest()[:16]}"


# ---------------------------------------------------------------------------
# 结构检查
# ---------------------------------------------------------------------------

def _check_structure(skill_dir: Path, ignore=None) -> List[Finding]:
    """
    检查 skill 目录的结构异常：
    - 文件过多
    - 总大小可疑地大
    - 不应出现在 skill 中的二进制/可执行文件
    - 指向 skill 目录之外的符号链接
    - 单个文件过大

    参数：
        skill_dir: skill 目录的路径。
        ignore: 可选的可调用对象，接收一个相对 posix 路径并返回
            True 表示该路径应被排除（例如来自 `.skillignore`）。
            被忽略的文件不计入文件数、总大小
            或任何结构发现。
    """
    if ignore is None:
        ignore = lambda _rel: False  # noqa: E731

    findings = []
    file_count = 0
    total_size = 0

    for f in skill_dir.rglob("*"):
        if not f.is_file() and not f.is_symlink():
            continue

        rel = str(f.relative_to(skill_dir))
        if ignore(rel):
            continue
        file_count += 1

        # 符号链接检查——必须解析到 skill 目录内
        if f.is_symlink():
            try:
                resolved = f.resolve()
                if not resolved.is_relative_to(skill_dir.resolve()):
                    findings.append(Finding(
                        pattern_id="symlink_escape",
                        severity="critical",
                        category="traversal",
                        file=rel,
                        line=0,
                        match=f"symlink -> {resolved}",
                        description="symlink points outside the skill directory",
                    ))
            except OSError:
                findings.append(Finding(
                    pattern_id="broken_symlink",
                    severity="medium",
                    category="traversal",
                    file=rel,
                    line=0,
                    match="broken symlink",
                    description="broken or circular symlink",
                ))
            continue

        # 大小跟踪
        try:
            size = f.stat().st_size
            total_size += size
        except OSError:
            continue

        # 单个文件过大
        if size > MAX_SINGLE_FILE_KB * 1024:
            findings.append(Finding(
                pattern_id="oversized_file",
                severity="medium",
                category="structural",
                file=rel,
                line=0,
                match=f"{size // 1024}KB",
                description=f"file is {size // 1024}KB (limit: {MAX_SINGLE_FILE_KB}KB)",
            ))

        # 二进制/可执行文件
        ext = f.suffix.lower()
        if ext in SUSPICIOUS_BINARY_EXTENSIONS:
            findings.append(Finding(
                pattern_id="binary_file",
                severity="critical",
                category="structural",
                file=rel,
                line=0,
                match=f"binary: {ext}",
                description=f"binary/executable file ({ext}) should not be in a skill",
            ))

        # 非脚本文件上的可执行权限
        if ext not in {'.sh', '.bash', '.py', '.rb', '.pl'} and f.stat().st_mode & 0o111:
            findings.append(Finding(
                pattern_id="unexpected_executable",
                severity="medium",
                category="structural",
                file=rel,
                line=0,
                match="executable bit set",
                description="file has executable permission but is not a recognized script type",
            ))

    # 文件数限制
    if file_count > MAX_FILE_COUNT:
        findings.append(Finding(
            pattern_id="too_many_files",
            severity="medium",
            category="structural",
            file="(directory)",
            line=0,
            match=f"{file_count} files",
            description=f"skill has {file_count} files (limit: {MAX_FILE_COUNT})",
        ))

    # 总大小限制
    if total_size > MAX_TOTAL_SIZE_KB * 1024:
        findings.append(Finding(
            pattern_id="oversized_skill",
            severity="high",
            category="structural",
            file="(directory)",
            line=0,
            match=f"{total_size // 1024}KB total",
            description=f"skill is {total_size // 1024}KB total (limit: {MAX_TOTAL_SIZE_KB}KB)",
        ))

    return findings


def _unicode_char_name(char: str) -> str:
    """获取不可见 unicode 字符的可读名称。"""
    names = {
        '\u200b': "zero-width space",
        '\u200c': "zero-width non-joiner",
        '\u200d': "zero-width joiner",
        '\u2060': "word joiner",
        '\u2062': "invisible times",
        '\u2063': "invisible separator",
        '\u2064': "invisible plus",
        '\ufeff': "BOM/zero-width no-break space",
        '\u202a': "LTR embedding",
        '\u202b': "RTL embedding",
        '\u202c': "pop directional",
        '\u202d': "LTR override",
        '\u202e': "RTL override",
        '\u2066': "LTR isolate",
        '\u2067': "RTL isolate",
        '\u2068': "first strong isolate",
        '\u2069': "pop directional isolate",
    }
    return names.get(char, f"U+{ord(char):04X}")



# ---------------------------------------------------------------------------
# 内部辅助函数
# ---------------------------------------------------------------------------

# skill 可以附带的忽略文件名，用于从扫描中排除开发/文档产物。
# `.skillignore` 是 Hermes 原生名称；`.clawhubignore` 为通过
# ClawHub 发布的 skill 提供兼容性支持。
_SKILL_IGNORE_FILENAMES = (".skillignore", ".clawhubignore")

# 无论忽略模式如何都永不扫描的路径，以及永不能通过
# 忽略文件取消扫描的 SKILL.md。
_ALWAYS_IGNORED_NAMES = set(_SKILL_IGNORE_FILENAMES)
_NEVER_IGNORABLE = {"SKILL.md"}


def _load_skill_ignore(skill_dir: Path):
    """从 skill 的 `.skillignore` / `.clawhubignore` 构建一个匹配器。

    返回一个可调用对象 ``ignore(rel_posix_path) -> bool``。该匹配器
    支持 gitignore 风格的基础语法：空行和 ``#`` 注释被跳过，
    尾部的 ``/`` 标记一个目录（匹配该目录及其下所有内容），
    ``*``/``?`` 通配符通过对完整相对路径和每个路径段进行 fnmatch
    来生效。开头的 ``/`` 把模式锚定到 skill 根。忽略文件本身
    始终被排除；``SKILL.md`` 永不能被排除。
    """
    patterns: List[str] = []
    for name in _SKILL_IGNORE_FILENAMES:
        ig = skill_dir / name
        try:
            if ig.is_file():
                for raw in ig.read_text(encoding="utf-8").splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    patterns.append(line)
        except (UnicodeDecodeError, OSError):
            continue

    def ignore(rel: str) -> bool:
        rel_posix = Path(rel).as_posix()
        base = rel_posix.split("/")[-1]

        if base in _NEVER_IGNORABLE:
            return False
        if base in _ALWAYS_IGNORED_NAMES:
            return True

        for pat in patterns:
            anchored = pat.startswith("/")
            p = pat.lstrip("/")
            is_dir = p.endswith("/")
            p = p.rstrip("/")
            if not p:
                continue

            if is_dir:
                # 目录模式：匹配目录本身或其下的任何内容。
                if rel_posix == p or rel_posix.startswith(p + "/"):
                    return True
                if not anchored and ("/" + rel_posix + "/").find("/" + p + "/") != -1:
                    return True
                continue

            # 文件/通配符模式。
            if fnmatch.fnmatch(rel_posix, p):
                return True
            if not anchored:
                # 未锚定：也匹配基名和任何路径段。
                if fnmatch.fnmatch(base, p):
                    return True
                if "/" not in p and any(
                    fnmatch.fnmatch(seg, p) for seg in rel_posix.split("/")
                ):
                    return True
                # 匹配前缀目录组件（例如 `docs` 会忽略
                # `docs/plans/x.md`）。
                if rel_posix.startswith(p + "/"):
                    return True
        return False

    return ignore


def _resolve_trust_level(source: str) -> str:
    """将来源标识符映射到信任级别。"""
    prefix_aliases = (
        "skills-sh/",
        "skills.sh/",
        "skils-sh/",
        "skils.sh/",
    )
    normalized_source = source
    for prefix in prefix_aliases:
        if normalized_source.startswith(prefix):
            normalized_source = normalized_source[len(prefix):]
            break

    # Agent 创建的 skill 拥有自己的宽松信任级别
    if normalized_source == "agent-created":
        return "agent-created"
    # 官方可选 skill 必须通过来源溯源识别，而不是通过
    # 用户控制的 GitHub 标识符如 "official/<repo>"。
    if normalized_source == "official":
        return "builtin"
    # 检查来源是否精确匹配某个受信任仓库，或该仓库内的某个 skill 路径。
    # 不要信任 merely 共享前缀的兄弟仓库。
    for trusted in TRUSTED_REPOS:
        if normalized_source == trusted or normalized_source.startswith(f"{trusted}/"):
            return "trusted"
    return "community"


def _determine_verdict(findings: List[Finding]) -> str:
    """根据发现列表确定整体结论。"""
    if not findings:
        return "safe"

    has_critical = any(f.severity == "critical" for f in findings)
    has_high = any(f.severity == "high" for f in findings)

    if has_critical:
        return "dangerous"
    if has_high:
        return "caution"
    # 仅有 medium/low 发现是信息性的，不阻止
    return "safe"


def _build_summary(name: str, source: str, trust: str, verdict: str, findings: List[Finding]) -> str:
    """构建扫描结果的单行摘要。"""
    if not findings:
        return f"{name}: clean scan, no threats detected"

    categories = {f.category for f in findings}
    return f"{name}: {verdict} — {len(findings)} finding(s) in {', '.join(sorted(categories))}"
