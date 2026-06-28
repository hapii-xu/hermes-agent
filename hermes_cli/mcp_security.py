"""用户配置的 MCP server 条目安全检查。

MCP stdio 传输方式有意支持任意本地命令，以便用户可以运行自定义服务器。
本模块不尝试对该能力进行沙箱隔离。它仅阻止实际中发现的两种高信号滥用模式：

1. #45620 中的数据窃取模式：一个 shell 解释器的内联脚本调用了网络出站工具。
2. 2026 年 6 月 ``hermes-0day`` 攻击活动中的持久化模式：一个 shell 解释器的
   内联脚本向操作系统持久化表面写入数据（``~/.ssh/authorized_keys``、
   ``/etc/ssh``、``/etc/pam.d``、``sudoers``、crontab、shell rc 文件）。
   该活动植入了 ``command: bash`` 的 MCP 条目，其 payload 将攻击者的 SSH
   密钥追加到 ``authorized_keys``；Hermes 在每个 cron 周期/启动时重新执行它们，
   反复安装后门。

3. 针对该活动的硬编码入侵指标（IOC）黑名单——攻击者的 ``hermes-0day`` SSH
   公钥和来源 IP。任何 command/args/env 中包含 IOC 的条目都将被直接拒绝，
   无论其形态如何，因此预植的 ``config.yaml`` 无法生成此类条目。

这些检查在保存时（``_save_mcp_server``——仪表板 API + CLI）和启动时
（``tools.mcp_tool._filter_suspicious_mcp_servers``——发现/cron/启动）
都会运行，因此手动编辑或预植的条目在执行前也会被捕获。
"""
from __future__ import annotations

import os
import re
import shlex
from typing import Any

_SHELL_INTERPRETERS = frozenset({
    "bash",
    "sh",
    "zsh",
    "dash",
    "fish",
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
})

_EGRESS_PATTERN = re.compile(
    r"(?<![\w.-])(?:curl|wget|nc|ncat|socat)(?![\w.-])"
    r"|/dev/tcp/"
    r"|\bInvoke-WebRequest\b"
    r"|\bInvoke-RestMethod\b"
    r"|\bSystem\.Net\.WebClient\b",
    re.IGNORECASE,
)

_EXFIL_HINT_PATTERN = re.compile(
    r"\.env\b|--data-binary|--data-raw|\b-X\s+POST\b|\bPOST\b|<\s*[^\s]+",
    re.IGNORECASE,
)

# MCP server 没有正当理由写入的操作系统持久化表面。
# 触及其中任何一个的 shell payload 都属于 2026 年 6 月 hermes-0day 的模式
# （SSH 密钥/PAM/sudoers/cron 持久化）。在内联脚本中任意位置匹配。
_PERSISTENCE_PATTERN = re.compile(
    r"authorized_keys"               # SSH 密钥持久化（该活动的 payload）
    r"|\.ssh/"                       # ~/.ssh 下的任何写入
    r"|/etc/ssh\b"                   # sshd_config / AuthorizedKeysCommand 后门
    r"|/etc/pam\.d\b|pam_[\w-]+\.so" # PAM 凭据记录器
    r"|/etc/sudoers"                 # sudoers 提权
    r"|/etc/cron|crontab\b"          # cron 持久化
    r"|/etc/rc\.local|/etc/systemd"  # init / systemd 单元持久化
    r"|\.bashrc\b|\.bash_profile\b|\.profile\b|\.zshrc\b",  # shell rc 后门
    re.IGNORECASE,
)

# ── 入侵指标：2026 年 6 月 hermes-0day 攻击活动 ─────────────────────────
# 硬编码，以便预植的 config.yaml（通过任何途径写入）在保存和启动时都会被拒绝。
# 这些是在多个被攻陷的公共实例（r/hermesagent、854.media）上观察到的确切攻击者产物。
_IOC_SUBSTRINGS = (
    # 攻击者 SSH 公钥（"hermes-0day" 持久化密钥）。
    "AAAAC3NzaC1lZDI1NTE5AAAAICBoh1oDC4DnsO1m5mJ4yfEKrQebaFh",
    "hermes-0day",
    # 攻击者来源 IP（中国电信甘肃），曾使用该密钥进行认证。
    "60.165.167.",
    "118.182.244.156",
    "61.178.123.196",
)


def _command_basename(command: Any) -> str:
    text = str(command or "").strip()
    if not text:
        return ""
    try:
        parts = shlex.split(text, posix=(os.name != "nt"))
    except ValueError:
        parts = text.split()
    first = parts[0] if parts else text
    return os.path.basename(first).lower()


def _inline_script(args: Any) -> str:
    if args is None:
        return ""
    if isinstance(args, (list, tuple)):
        return " ".join(str(item) for item in args)
    return str(args)


def _entry_text(entry: dict[str, Any]) -> str:
    """将 command + args + env 值展平为一个字符串，用于 IOC 扫描。"""
    parts: list[str] = [str(entry.get("command") or "")]
    parts.append(_inline_script(entry.get("args")))
    env = entry.get("env")
    if isinstance(env, dict):
        parts.extend(str(v) for v in env.values())
    return " ".join(parts)


def validate_mcp_server_entry(name: str, entry: dict[str, Any]) -> list[str]:
    """返回 MCP server 条目的安全警告。

    返回空列表表示该条目不可疑。这有意不做白名单：合法的本地 MCP 仍然可以
    使用自定义命令、Python 脚本、npx、uvx 等。我们仅阻止三种窄范围模式：

    * command/args/env 中存在已知 hermes-0day IOC（硬编码黑名单）；
    * shell 解释器的内联脚本调用了网络出站工具（#45620）；
    * shell 解释器的内联脚本向操作系统持久化表面写入数据
      （2026 年 6 月 hermes-0day SSH/PAM/sudoers/cron 模式）。
    """
    if not isinstance(entry, dict):
        return []

    issues: list[str] = []

    # 1. 硬编码 IOC 黑名单——无论命令形态如何都适用。
    flat = _entry_text(entry)
    for ioc in _IOC_SUBSTRINGS:
        if ioc in flat:
            issues.append(
                f"MCP server '{name}' contains a known hermes-0day "
                f"indicator-of-compromise ('{ioc}')"
            )
            # 一个 IOC 就足以拒绝；不要泄露完整的匹配列表。
            return issues

    command = entry.get("command")
    basename = _command_basename(command)
    if basename not in _SHELL_INTERPRETERS:
        return issues

    script = _inline_script(entry.get("args"))
    if not script:
        return issues

    # 2. 网络数据窃取模式。
    if _EGRESS_PATTERN.search(script):
        issue = (
            f"MCP server '{name}' uses shell interpreter '{command}' with "
            f"network egress in args"
        )
        if _EXFIL_HINT_PATTERN.search(script):
            issue += " and exfiltration-shaped arguments"
        issues.append(issue)

    # 3. 操作系统持久化模式（SSH 密钥 / PAM / sudoers / cron / rc 文件）。
    if _PERSISTENCE_PATTERN.search(script):
        issues.append(
            f"MCP server '{name}' uses shell interpreter '{command}' to write "
            f"to an OS persistence surface (SSH keys / PAM / sudoers / cron / "
            f"shell rc) — this is the hermes-0day backdoor shape, not a real "
            f"MCP server"
        )

    return issues


def is_mcp_server_entry_suspicious(name: str, entry: dict[str, Any]) -> bool:
    return bool(validate_mcp_server_entry(name, entry))
