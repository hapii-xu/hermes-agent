"""
Hermes Agent 安全公告检查器。

检测活动虚拟环境中已安装的已知受损 Python 包
（供应链攻击，例如 2026 年 5 月的 Mini Shai-Hulud 蠕虫
在 PyPI 上污染了 ``mistralai 2.4.6``），并向用户展示修复指导。

设计目标：

- **低成本。** 每个公告包只需一次 ``importlib.metadata.version()`` 调用。
  可在每次 CLI 启动时安全运行。
- **重要时高声提醒，否则静默。** 如果未安装受损包，用户不会看到任何内容。
- **可确认。** 用户阅读并处理公告后，可通过
  ``hermes doctor --ack <id>`` 关闭公告；确认信息会持久化到
  ``config.security.acked_advisories`` 并在重启后保留。
- **可扩展。** 添加新公告只需在 ``ADVISORIES`` 中添加一个条目；
  添加新的受损版本只需修改一行。下一次蠕虫爆发时无需更改代码。

检查从以下三个位置调用：

1. ``hermes doctor``（和 ``hermes doctor --ack <id>``）
2. CLI 启动横幅（一行简短提示，然后通过
   ``hermes doctor`` 查看完整指导）
3. Gateway 启动（记录到 gateway.log；首条交互消息会显示
   一行运维横幅）

此模块有意保持无外部依赖（仅使用标准库），以便在
 Hermes 其他部分导入失败的环境中也能运行。
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# 公告目录
#
# 每个公告都是面向社区的安全警告，涉及一个或多个
# 已知受损的特定包版本。添加新公告的步骤：
#
#   1. 在下面的 ``ADVISORIES`` 中追加新的 ``Advisory``
#   2. 将 ``compromised`` 设置为 ``(pkg_name, frozenset_of_versions)`` 的元组
#      — 版本字符串必须与 ``importlib.metadata.version()``
#      返回的内容匹配。使用空 frozenset 标记*任何已安装版本*
#      （罕见；仅在维护者命名空间本身受损时使用）。
#   3. 编写 2-4 行简短的 ``remediation`` 步骤，方便非专业用户复制粘贴。
#
# 不要删除旧公告。公告发布后，保留在原处，以便
# 运行包含受损包的旧版本的用户仍能收到
# 警告。如需要，通过 ``superseded_by`` 标记已取代的公告。
# =============================================================================


@dataclass(frozen=True)
class Advisory:
    """单个安全公告条目。

    属性：
        id: 用于确认的稳定标识符（例如 ``shai-hulud-2026-05``）。
            小写连字符格式，永不重复使用。
        title: 横幅中显示的单行标题。
        summary: 1-3 句描述，说明受损内容及其方式。
        url: 参考 URL（Socket 公告、GitHub 公告、PyPI 页面）。
        compromised: ``(package_name, frozenset_of_versions)`` 对的元组。
            空 frozenset 表示"此包的任何版本均被视为可疑"
            — 请谨慎使用。
        remediation: 用户应执行的有序步骤列表。第一步
            应为卸载命令；后续步骤为凭据
            审计/轮换指导。
        published: 用于排序的 ISO 日期字符串。
    """

    id: str
    title: str
    summary: str
    url: str
    compromised: tuple[tuple[str, frozenset[str]], ...]
    remediation: tuple[str, ...]
    published: str = ""
    severity: str = "high"  # low / medium / high / critical（低/中/高/严重）


ADVISORIES: tuple[Advisory, ...] = (
    Advisory(
        id="shai-hulud-2026-05",
        title="Mini Shai-Hulud worm — mistralai 2.4.6 compromised on PyPI",
        summary=(
            "PyPI quarantined the mistralai package on 2026-05-12 after a "
            "malicious 2.4.6 release. The worm steals credentials from "
            "environment variables and credential files (~/.npmrc, ~/.pypirc, "
            "~/.aws/credentials, GitHub PATs, cloud SDK tokens) and exfils "
            "them to a hardcoded webhook. If you ran any Python process that "
            "imported mistralai 2.4.6 — including hermes when configured "
            "with provider=mistral for TTS or STT — assume those credentials "
            "are exposed. PyPI has since removed 2.4.6 and the project ships "
            "clean releases again (2.4.7, 2.4.8); this advisory only fires if "
            "the compromised 2.4.6 is still installed."
        ),
        url="https://socket.dev/blog/mini-shai-hulud-worm-pypi",
        compromised=(
            ("mistralai", frozenset({"2.4.6"})),
        ),
        remediation=(
            "Run: pip uninstall -y mistralai  (or: uv pip uninstall mistralai)",
            "Rotate API keys in ~/.hermes/.env (OpenRouter, Anthropic, OpenAI, "
            "Nous, GitHub, AWS, Google, Mistral, etc.).",
            "Audit ~/.npmrc, ~/.pypirc, ~/.aws/credentials, ~/.config/gh/hosts.yml, "
            "and any other credential files for tokens that may have been read.",
            "Check GitHub for unexpected new SSH keys, deploy keys, or webhook "
            "additions on repos you have admin on.",
            "After cleanup: hermes doctor --ack shai-hulud-2026-05  to dismiss "
            "this warning.",
        ),
        published="2026-05-12",
        severity="critical",
    ),
)


# =============================================================================
# 检测
# =============================================================================


@dataclass(frozen=True)
class AdvisoryHit:
    """公告的一个包版本匹配项。"""

    advisory: Advisory
    package: str
    installed_version: str


def _installed_version(pkg_name: str) -> Optional[str]:
    """返回 ``pkg_name`` 的已安装版本，如果未安装则返回 None。

    使用 ``importlib.metadata``，这样我们就不依赖于活动虚拟环境中
    pip 的可导入性（uv 创建的虚拟环境可能缺少 pip）。
    """
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # py<3.8 — Hermes 要求 3.10+，但这是防御性检查。
        return None
    try:
        return version(pkg_name)
    except PackageNotFoundError:
        return None
    except Exception:
        # 某些元数据损坏模式会引发 ValueError 或 OSError。不要让
        # 公告检查使 CLI 启动路径崩溃。
        logger.debug("importlib.metadata.version(%s) raised", pkg_name, exc_info=True)
        return None


def detect_compromised(
    advisories: Iterable[Advisory] = ADVISORIES,
) -> list[AdvisoryHit]:
    """扫描已安装的包并返回所有公告匹配项。

    "匹配"表示公告列出的包已安装，并且版本
    在受损集合中（或者受损集合为空，表示
    *任何*版本均可疑）。
    """
    hits: list[AdvisoryHit] = []
    for advisory in advisories:
        for pkg_name, bad_versions in advisory.compromised:
            installed = _installed_version(pkg_name)
            if installed is None:
                continue
            if not bad_versions or installed in bad_versions:
                hits.append(AdvisoryHit(
                    advisory=advisory,
                    package=pkg_name,
                    installed_version=installed,
                ))
    return hits


# =============================================================================
# 确认持久化
#
# 确认信息存储在 config.yaml 的 ``security.acked_advisories`` 下，
# 作为公告 ID 的列表。该列表是唯一的状态 — 没有每台主机的数据、
# 没有时间戳、没有指纹。跨机器共享 config.yaml 的用户
# （罕见但可能）会在所有位置获得相同的关闭效果，
# 这对于全局公告是正确的行为。
# =============================================================================


def get_acked_ids() -> set[str]:
    """返回用户已关闭的公告 ID 集合。

    如果无法加载配置，则返回空集（不要因为
    配置损坏而阻塞启动 — 公告会继续触发，直到
    配置修复，这没问题）。
    """
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
    except Exception:
        logger.debug("Could not load config for advisory acks", exc_info=True)
        return set()
    sec = cfg.get("security") or {}
    raw = sec.get("acked_advisories") or []
    if not isinstance(raw, list):
        return set()
    return {str(x).strip() for x in raw if str(x).strip()}


def ack_advisory(advisory_id: str) -> bool:
    """持久化 ``advisory_id`` 的确认。成功时返回 True。

    幂等操作 — 确认已确认的 ID 是空操作。
    """
    advisory_id = advisory_id.strip()
    if not advisory_id:
        return False
    try:
        from hermes_cli.config import load_config, save_config
    except Exception:
        logger.warning("Could not import config module to persist ack")
        return False
    try:
        cfg = load_config()
        sec = cfg.setdefault("security", {})
        existing = sec.get("acked_advisories") or []
        if not isinstance(existing, list):
            existing = []
        if advisory_id not in existing:
            existing.append(advisory_id)
            sec["acked_advisories"] = existing
            save_config(cfg)
        return True
    except Exception:
        logger.exception("Failed to persist advisory ack for %s", advisory_id)
        return False


def filter_unacked(hits: list[AdvisoryHit]) -> list[AdvisoryHit]:
    """仅返回用户尚未关闭的公告的匹配项。"""
    if not hits:
        return []
    acked = get_acked_ids()
    return [h for h in hits if h.advisory.id not in acked]


# =============================================================================
# 渲染辅助函数
# =============================================================================


def _term_supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    return True


def short_banner_lines(hits: list[AdvisoryHit]) -> list[str]:
    """返回 1-3 行适合启动横幅的简短文本。

    调用方负责颜色/样式。始终明确命名最严重的匹配项，
    以便用户无需运行 doctor 即可了解问题所在。
    """
    if not hits:
        return []
    primary = hits[0]
    lines = [
        f"SECURITY ADVISORY [{primary.advisory.id}]: {primary.advisory.title}",
        f"  Detected: {primary.package}=={primary.installed_version}",
        "  Run 'hermes doctor' for remediation steps.",
    ]
    if len(hits) > 1:
        lines.insert(1, f"  ({len(hits) - 1} additional advisor"
                       f"{'ies' if len(hits) > 2 else 'y'} also active.)")
    return lines


def full_remediation_text(hit: AdvisoryHit) -> list[str]:
    """返回描述公告 + 修复步骤的多行文本块。"""
    a = hit.advisory
    lines = [
        f"=== {a.title} ===",
        f"ID:        {a.id}    Severity: {a.severity}    Published: {a.published}",
        f"Detected:  {hit.package}=={hit.installed_version}",
        f"Reference: {a.url}",
        "",
        a.summary,
        "",
        "Remediation:",
    ]
    for i, step in enumerate(a.remediation, 1):
        lines.append(f"  {i}. {step}")
    return lines


# =============================================================================
# 启动横幅门控
#
# 我们不希望在每个命令上都用横幅轰炸用户。一旦
# 他们在 24 小时内看到过横幅，我们就将该事实缓存到
# ``~/.hermes/cache/advisory_banner_seen``（每个公告 ID 一行：
# ``<id> <iso8601_timestamp>``）。
#
# 已确认的公告永远不会重新显示横幅。已缓存但未确认的公告
# 会在 24 小时后重新显示横幅，这样用户不会完全忘记。
# =============================================================================


_BANNER_CACHE_FILE = "advisory_banner_seen"
_BANNER_REPEAT_HOURS = 24


def _banner_cache_path() -> Optional[Path]:
    try:
        from hermes_constants import get_hermes_home
        cache_dir = Path(get_hermes_home()) / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / _BANNER_CACHE_FILE
    except Exception:
        return None


def _read_banner_cache() -> dict[str, float]:
    p = _banner_cache_path()
    if p is None or not p.exists():
        return {}
    out: dict[str, float] = {}
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            advisory_id, ts = parts
            try:
                out[advisory_id] = float(ts)
            except ValueError:
                continue
    except Exception:
        return {}
    return out


def _write_banner_cache(seen: dict[str, float]) -> None:
    p = _banner_cache_path()
    if p is None:
        return
    try:
        lines = [f"{aid} {ts}" for aid, ts in seen.items()]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        logger.debug("Could not write advisory banner cache", exc_info=True)


def hits_due_for_banner(
    hits: list[AdvisoryHit],
    *,
    repeat_hours: int = _BANNER_REPEAT_HOURS,
) -> list[AdvisoryHit]:
    """仅返回横幅到期（未确认、未最近显示）的匹配项。

    副作用：为即将显示的匹配项在横幅缓存中打时间戳。
    调用方应随后渲染结果。
    """
    import time

    fresh = filter_unacked(hits)
    if not fresh:
        return []
    now = time.time()
    cache = _read_banner_cache()
    cutoff = now - (repeat_hours * 3600)

    due: list[AdvisoryHit] = []
    for hit in fresh:
        last = cache.get(hit.advisory.id, 0.0)
        if last < cutoff:
            due.append(hit)
            cache[hit.advisory.id] = now
    if due:
        _write_banner_cache(cache)
    return due


# =============================================================================
# doctor / CLI / gateway 使用的公共入口点
# =============================================================================


def render_doctor_section(hits: list[AdvisoryHit]) -> tuple[bool, list[str]]:
    """为 ``hermes doctor`` 渲染安全公告部分。

    返回 ``(has_problems, lines)``。调用方负责使用
    其使用的任何颜色方案进行打印。
    """
    fresh = filter_unacked(hits)
    if not fresh:
        return False, ["No active security advisories.  ✓"]

    lines: list[str] = []
    for i, hit in enumerate(fresh):
        if i:
            lines.append("")
        lines.extend(full_remediation_text(hit))
    return True, lines


def startup_banner(hits: list[AdvisoryHit]) -> Optional[str]:
    """返回可打印的启动横幅，如果没有到期内容则返回 None。

    副作用是更新横幅缓存（因此同一匹配项在
    24 小时内的下一次调用会返回 None）。
    """
    due = hits_due_for_banner(hits)
    if not due:
        return None
    lines = short_banner_lines(due)
    if _term_supports_color():
        red = "\x1b[1;31m"
        reset = "\x1b[0m"
        return red + "\n".join(lines) + reset
    return "\n".join(lines)


def gateway_log_message(hits: list[AdvisoryHit]) -> Optional[str]:
    """为 gateway 运维人员返回单行日志消息，或者返回 None。"""
    fresh = filter_unacked(hits)
    if not fresh:
        return None
    if len(fresh) == 1:
        h = fresh[0]
        return (f"Security advisory [{h.advisory.id}] active: "
                f"{h.package}=={h.installed_version} matches {h.advisory.title}. "
                f"See {h.advisory.url}")
    return (f"{len(fresh)} security advisories active "
            f"(IDs: {', '.join(h.advisory.id for h in fresh)}). "
            f"Run `hermes doctor` on the gateway host for details.")
