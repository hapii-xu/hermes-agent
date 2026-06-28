"""针对 MCP 扩展包的 OSV 恶意软件检查。

在通过 npx/uvx 启动 MCP 服务器之前，查询 OSV（开源漏洞，Open Source
Vulnerabilities）API 以检查该包是否有任何已知的恶意软件公告
（MAL-* ID）。常规的 CVE 一律忽略 —— 仅拦截已确认的恶意软件。

该 API 免费公开，由 Google 维护。典型延迟约 300ms。
故障放行（fail-open）：网络错误时允许该包继续运行。

灵感来自 Block/goose 的扩展恶意软件检查。
"""

import json
import logging
import os
import re
import urllib.request
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_OSV_ENDPOINT = os.getenv("OSV_ENDPOINT", "https://api.osv.dev/v1/query")
_TIMEOUT = 10  # 秒


def check_package_for_malware(
    command: str, args: list
) -> Optional[str]:
    """检查 MCP 服务器包是否有已知的恶意软件公告。

    检查 *command*（例如 ``npx``、``uvx``）和 *args* 以推断
    包名和所属生态系统。查询 OSV API 获取 MAL-* 公告。

    返回：
        如果发现恶意软件则返回错误消息字符串，否则返回 None（干净/未知）。
        在网络错误或无法识别的命令时返回 None（放行）。
    """
    ecosystem = _infer_ecosystem(command)
    if not ecosystem:
        return None  # 非 npx/uvx —— 跳过

    package, version = _parse_package_from_args(args, ecosystem)
    if not package:
        return None

    try:
        malware = _query_osv(package, ecosystem, version)
    except Exception as exc:
        # 故障放行：网络错误、超时、解析失败 → 放行
        logger.debug("OSV check failed for %s/%s (allowing): %s", ecosystem, package, exc)
        return None

    if malware:
        ids = ", ".join(m["id"] for m in malware[:3])
        summaries = "; ".join(
            m.get("summary", m["id"])[:100] for m in malware[:3]
        )
        return (
            f"BLOCKED: Package '{package}' ({ecosystem}) has known malware "
            f"advisories: {ids}. Details: {summaries}"
        )
    return None


def _infer_ecosystem(command: str) -> Optional[str]:
    """从命令名推断包的所属生态系统。"""
    base = os.path.basename(command).lower()
    if base in {"npx", "npx.cmd"}:
        return "npm"
    if base in {"uvx", "uvx.cmd", "pipx"}:
        return "PyPI"
    return None


def _parse_package_from_args(
    args: list, ecosystem: str
) -> Tuple[Optional[str], Optional[str]]:
    """从命令参数中提取包名和可选版本。

    如果无法解析则返回 (package_name, version) 或 (None, None)。
    """
    if not args:
        return None, None

    # 跳过各种 flag 以找到包 token。
    # 兼容 npx 的显式安装目标：--package=NAME / --package NAME 以及
    # -p NAME 短形式，它们指定的包与实际执行的二进制不同。
    # 不这样做的话，第一个裸位置参数（通常是命令名）
    # 会被误当成包。
    package_token = None
    take_next = False
    for arg in args:
        if not isinstance(arg, str):
            continue
        if take_next:
            package_token = arg
            break
        if arg in ("--package", "-p"):
            take_next = True
            continue
        if arg.startswith("--package="):
            package_token = arg[len("--package="):]
            break
        if arg.startswith("-"):
            continue
        package_token = arg
        break

    if not package_token:
        return None, None

    if ecosystem == "npm":
        return _parse_npm_package(package_token)
    elif ecosystem == "PyPI":
        return _parse_pypi_package(package_token)
    return package_token, None


def _parse_npm_package(token: str) -> Tuple[Optional[str], Optional[str]]:
    """解析 npm 包：@scope/name@version 或 name@version。"""
    if token.startswith("@"):
        # 带作用域：@scope/name@version
        match = re.match(r"^(@[^/]+/[^@]+)(?:@(.+))?$", token)
        if match:
            return match.group(1), match.group(2)
        return token, None
    # 不带作用域：name@version
    if "@" in token:
        parts = token.rsplit("@", 1)
        name = parts[0]
        version = parts[1] if len(parts) > 1 and parts[1] != "latest" else None
        return name, version
    return token, None


def _parse_pypi_package(token: str) -> Tuple[Optional[str], Optional[str]]:
    """解析 PyPI 包：name==version 或 name[extras]==version。"""
    # 去除 extras：name[extra1,extra2]==version
    match = re.match(r"^([a-zA-Z0-9._-]+)(?:\[[^\]]*\])?(?:==(.+))?$", token)
    if match:
        return match.group(1), match.group(2)
    return token, None


def _query_osv(
    package: str, ecosystem: str, version: Optional[str] = None
) -> list:
    """查询 OSV API 获取 MAL-* 公告。返回恶意软件漏洞列表。"""
    payload = {"package": {"name": package, "ecosystem": ecosystem}}
    if version:
        payload["version"] = version

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _OSV_ENDPOINT,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "hermes-agent-osv-check/1.0",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        result = json.loads(resp.read())

    vulns = result.get("vulns", [])
    # 仅恶意软件公告 —— 忽略常规 CVE
    return [v for v in vulns if v.get("id", "").startswith("MAL-")]
