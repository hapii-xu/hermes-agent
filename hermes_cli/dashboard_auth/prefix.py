"""X-Forwarded-Prefix 支持辅助函数。

Mission-control 风格的部署在路径前缀（例如
``mission-control.tilos.com/hermes/*`` -> dashboard on :9119）处反向代理
dashboard，注入 ``X-Forwarded-Prefix: /hermes`` 以便后端可以重建带前缀的
URL（Location 头、OAuth redirect_uri、cookie Path 属性、SPA 资源 URL）。

此模块也是 ``HERMES_DASHBOARD_PUBLIC_URL`` / ``dashboard.public_url`` 解析
的家——当操作员声明完整的公共 URL（scheme + host + 可选路径前缀）时，
我们直接将其用于 OAuth ``redirect_uri`` 并跳过 X-Forwarded-Prefix 重建。
这是代理头链不可靠的部署的缓解阀。

两个辅助函数的唯一真实来源都在这里，因此门控中间件、OAuth 路由、
cookie 辅助函数和 SPA 挂载在验证规则上保持一致。
"""
from __future__ import annotations

import logging
import os
import urllib.parse
from typing import Optional

_log = logging.getLogger(__name__)

# 如果存在则表明拼写错误或头注入尝试的字符。拒绝整个值
# 而不是尝试清理——操作员可以修复其配置。
_REJECT_CHARS = frozenset(('"', "'", "<", ">", " ", "\n", "\r", "\t"))

# 记住我们已经警告过哪些 (source, value) 对。
# ``resolve_public_url`` 在每个已认证请求上运行，因此未去重的警告
# 会在配置错误的部署中每个请求泛滥日志。也以原始值为键，
# 因此更改配置并重新加载会显示新的警告。
_warned_malformed_public_urls: set = set()


def _warn_if_malformed(source: str, raw: str) -> None:
    """当非空的 public-url 值被 :func:`_normalise_public_url` 拒绝时警告
    （每个不同值一次）。

    规范化为 ``""`` 的非空值几乎总是缺少 scheme
    （``hermes.example.com`` 而非 ``https://hermes.example.com``）——
    "我设置了 HERMES_DASHBOARD_PUBLIC_URL 但 OAuth 回调仍然是
    http://"的最常见原因。没有此警告，值会被静默丢弃，
    dashboard 会回退到从请求头重建重定向 URI，这在反向代理后
    可能产生错误的 scheme。显示此警告将静默的坑转为可自诊断的坑。
    """
    cleaned = raw.strip() if raw else ""
    if not cleaned:
        return  # empty/unset is a legitimate "no override" — not malformed
    key = (source, cleaned)
    if key in _warned_malformed_public_urls:
        return
    _warned_malformed_public_urls.add(key)
    _log.warning(
        "%s is set to %r but was ignored because it is not a valid "
        "absolute URL — it must include an http:// or https:// scheme "
        "(e.g. https://%s). Falling back to reconstructing the OAuth "
        "redirect URI from request headers, which may produce the wrong "
        "scheme behind a reverse proxy.",
        source,
        cleaned,
        cleaned.split("://")[-1] or "hermes.example.com",
    )


def normalise_prefix(raw: Optional[str]) -> str:
    """规范化 X-Forwarded-Prefix 头值。

    返回类似 ``"/hermes"`` 的字符串（无尾部斜杠）或 ``""``
    当未设置前缀/头格式错误时。我们故意拒绝任何包含 ``..`` 或
    不可打印字节的内容，以防止恶意代理通过前缀注入 HTML 或
    路径遍历序列。
    """
    if not raw:
        return ""
    p = raw.strip()
    if not p:
        return ""
    if not p.startswith("/"):
        p = "/" + p
    p = p.rstrip("/")
    if (
        "//" in p
        or ".." in p
        or any(c in p for c in _REJECT_CHARS)
    ):
        return ""
    if len(p) > 64:
        return ""
    return p


def prefix_from_request(request) -> str:
    """便捷包装器，从 Starlette/FastAPI Request 读取头并规范化。
    无前缀时返回 ``""``。
    """
    return normalise_prefix(request.headers.get("x-forwarded-prefix"))


# ---------------------------------------------------------------------------
# HERMES_DASHBOARD_PUBLIC_URL / dashboard.public_url
# ---------------------------------------------------------------------------


def _normalise_public_url(raw: Optional[str]) -> str:
    """规范化 ``dashboard.public_url`` 值。

    成功时返回清理后的 URL（scheme://netloc[/path]，移除尾部斜杠），
    或当值为空、格式错误或包含暗示头注入的字符时返回 ``""``。
    调用者必须将 ``""`` 视为"回退到请求重建"——永远不能视为
    "用户明确选择了无公共 URL"，因为两者从空环境变量中无法区分。
    """
    if not raw:
        return ""
    url = raw.strip()
    if not url:
        return ""
    # 在尝试解析之前拒绝控制/引号/空白字符——urlparse 足够宽松，
    # 会接受某些恶意值（例如嵌入的换行符），我们想要硬"否"
    # 而非软"可能"。
    if any(c in url for c in _REJECT_CHARS):
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    if not parsed.netloc:
        return ""
    # 移除单个尾部斜杠，以便调用者可以追加路径而不产生
    # ``//`` 双斜杠。
    return url.rstrip("/")


def _load_dashboard_section() -> dict:
    """如果存在且为字典则返回 ``config.yaml`` 的 ``dashboard`` 块；
    否则返回空字典。

    对以下情况健壮：(a) load_config() 抛出（YAML 格式错误、IO 错误、
    config.yaml 不存在），(b) ``dashboard`` 缺失或非字典。两种情况都
    落入 ``{}``，以便调用者可以依赖 ``.get(...)`` 访问。
    """
    try:
        from hermes_cli.config import load_config
    except Exception:
        return {}
    try:
        cfg = load_config()
    except Exception as exc:  # noqa: BLE001 — broad catch is intentional
        _log.debug(
            "dashboard-auth.prefix: load_config() raised %s; "
            "falling back to env-only configuration",
            exc,
        )
        return {}
    section = cfg.get("dashboard") if isinstance(cfg, dict) else None
    return section if isinstance(section, dict) else {}


def resolve_public_url() -> str:
    """解析操作员声明的 dashboard 公共 URL。

    优先级（镜像 ``dashboard.oauth.client_id``）：

      1. ``HERMES_DASHBOARD_PUBLIC_URL`` 环境变量（strip 后非空时——
         空值被视为未设置，因此已配置但未填充的 Fly secret 不会
         遮蔽有效的 config.yaml 条目）。
      2. ``config.yaml`` 中的 ``dashboard.public_url``。
      3. 空字符串——向调用者发出"无覆盖，从请求重建"信号。

    每个候选值都通过 :func:`_normalise_public_url` 运行。
    格式错误的环境变量落入 config.yaml 条目；格式错误的配置条目
    落入 ``""``。这意味着一个表面的拼写错误不会阻止另一个工作。
    """
    env_raw = os.environ.get("HERMES_DASHBOARD_PUBLIC_URL", "")
    env_clean = _normalise_public_url(env_raw)
    if env_clean:
        return env_clean
    _warn_if_malformed("HERMES_DASHBOARD_PUBLIC_URL env var", env_raw)
    cfg_raw = str(_load_dashboard_section().get("public_url", ""))
    cfg_clean = _normalise_public_url(cfg_raw)
    if not cfg_clean:
        _warn_if_malformed("dashboard.public_url in config.yaml", cfg_raw)
    return cfg_clean
