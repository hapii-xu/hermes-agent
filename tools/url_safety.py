"""URL 安全检查 —— 拦截对私有/内部网络地址的请求。

防止 SSRF（服务端请求伪造），即恶意的 prompt 或技能诱导 agent 去抓取内部资源，
例如云元数据端点（169.254.169.254）、localhost 服务或私有网络主机。

本检查可以通过 config.yaml 中的 ``security.allow_private_urls: true`` 全局关闭，
适用于 DNS 把外部域名解析到私有/基准区间 IP 的环境（OpenWrt 路由器、企业代理、
使用 198.18.0.0/15 或 100.64.0.0/10 的 VPN）。即使在关闭状态下，云元数据主机名
（metadata.google.internal、169.254.169.254）也 **始终** 被拦截 —— 这些绝不是
合法的 agent 目标。

局限性（已记录，无法在预检层面修复）：
  - DNS 重绑定（TOCTOU）：攻击者控制的 DNS 服务器用 TTL=0 可以在检查时返回公网 IP，
    而在实际建立连接时返回私有 IP。修复需要连接级校验（例如 Python 的 Champion
    库，或 Stripe 的 Smokescreen 之类的出口代理）。
  - 基于重定向的绕过已通过 httpx 事件钩子缓解，这些钩子会在 vision_tools、
    网关平台适配器和媒体缓存辅助工具中对每个重定向目标重新校验。Web 工具使用第三方
    SDK（Firecrawl/Tavily），重定向处理在它们的服务器上完成。
"""

import ipaddress
import logging
import os
import socket
import asyncio
from urllib.parse import quote, urlparse, urlsplit, urlunsplit

from utils import is_truthy_value

logger = logging.getLogger(__name__)


def normalize_url_for_request(url: str) -> str:
    """为 Hermes 自有的 URL 工具返回一个 ASCII 安全的 HTTP URL。

    浏览器和 HTTP 客户端期望的是 URI，但用户和模型经常提供 IRI，例如
    ``https://wttr.in/Köln``。本函数保留 URL 语法和已有的百分号转义，同时把
    非 ASCII 的主机/路径/查询/片段文本做编码。该函数刻意只用于 URL 工具的输入；
    任意的 shell 命令绝不能被改写。
    """
    if not isinstance(url, str):
        return url

    raw = url.strip()
    if not raw:
        return raw

    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw

    if parsed.scheme.lower() not in {"http", "https"}:
        return raw

    netloc = parsed.netloc
    hostname = parsed.hostname
    if hostname:
        try:
            ascii_host = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            ascii_host = hostname
        if ascii_host != hostname:
            netloc = netloc.replace(hostname, ascii_host, 1)

    path = quote(parsed.path, safe="/%:@!$&'()*+,;=")
    query = quote(parsed.query, safe="/%:@!$&'()*+,;=?")
    fragment = quote(parsed.fragment, safe="/%:@!$&'()*+,;=?")

    return urlunsplit((parsed.scheme, netloc, path, query, fragment))

# 无论 IP 解析结果如何或任何配置开关如何，都应始终被拦截的主机名。
# 这些是云元数据端点，攻击者可能利用它们窃取实例凭证。
_BLOCKED_HOSTNAMES = frozenset({
    "metadata.google.internal",
    "metadata.goog",
})

# 无论 allow_private_urls 开关如何，都应始终被拦截的 IP 和网段。
# 这些是云元数据 / 凭证端点 —— SSRF 的头号目标 —— 以及它们所在的链路本地网段。
#
# 这里也包含了 IPv4 映射的 IPv6 变体，因为 DNS 解析器可能为纯 IPv4 主机返回
# ``::ffff:x.x.x.x``，而 Python 的 ipaddress 模块把它们当作与纯 IPv4 地址不同的
# 对象（它们不会匹配 ``ip in frozenset`` 或 ``ip in network``）。
_ALWAYS_BLOCKED_IPS = frozenset({
    ipaddress.ip_address("169.254.169.254"),  # AWS/GCP/Azure/DO/Oracle metadata
    ipaddress.ip_address("169.254.170.2"),     # AWS ECS task metadata (task IAM creds)
    ipaddress.ip_address("169.254.169.253"),   # Azure IMDS wire server
    ipaddress.ip_address("fd00:ec2::254"),     # AWS metadata (IPv6)
    ipaddress.ip_address("100.100.100.200"),   # Alibaba Cloud metadata
    # IPv4 映射的 IPv6 变体 —— 同样的端点，可通过 ::ffff:x.x.x.x 访问
    ipaddress.ip_address("::ffff:169.254.169.254"),
    ipaddress.ip_address("::ffff:169.254.170.2"),
    ipaddress.ip_address("::ffff:169.254.169.253"),
    ipaddress.ip_address("::ffff:100.100.100.200"),
})
_ALWAYS_BLOCKED_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),    # 整个链路本地网段（没有合法的 agent 目标）
    ipaddress.ip_network("::ffff:169.254.0.0/112"), # IPv4 映射的链路本地网段
)

# 允许解析到私有/基准区间 IP 的确切 HTTPS 主机名。
# 这个白名单刻意收得很窄：QQ 的媒体下载在本地代理/基准基础设施后面可能合法地解析到
# 198.18.0.0/15。
_TRUSTED_PRIVATE_IP_HOSTS = frozenset({
    "multimedia.nt.qq.com.cn",
})

# 100.64.0.0/10（CGNAT / 共享地址空间，RFC 6598）不被
# ipaddress.is_private 覆盖 —— 它对 is_private 和 is_global 都返回 False。
# 必须显式拦截。被运营商级 NAT、Tailscale/WireGuard VPN 以及某些云内部网络使用。
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")

# ---------------------------------------------------------------------------
# 全局开关：是否允许私有/内部 IP 解析
# ---------------------------------------------------------------------------
# 首次读取后缓存，避免每次 URL 检查都去碰文件系统。
_allow_private_resolved = False
_cached_allow_private: bool = False


def _global_allow_private_urls() -> bool:
    """当用户选择退出私有 IP 拦截时返回 True。

    按以下优先级顺序检查：
    1. ``HERMES_ALLOW_PRIVATE_URLS`` 环境变量（``true``/``1``/``yes``）
    2. config.yaml 中的 ``security.allow_private_urls``
    3. config.yaml 中的 ``browser.allow_private_urls``（遗留 / 向后兼容）

    结果在进程生命周期内被缓存。
    """
    global _allow_private_resolved, _cached_allow_private
    if _allow_private_resolved:
        return _cached_allow_private

    _allow_private_resolved = True
    _cached_allow_private = False  # 安全的默认值

    # 1. 环境变量覆盖（优先级最高）
    env_val = os.getenv("HERMES_ALLOW_PRIVATE_URLS", "").strip().lower()
    if env_val in {"true", "1", "yes"}:
        _cached_allow_private = True
        return _cached_allow_private
    if env_val in {"false", "0", "no"}:
        # 显式 false —— 不再向下走到 config
        return _cached_allow_private

    # 2. 配置文件
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config()
        # security.allow_private_urls（首选）
        sec = cfg.get("security", {})
        if isinstance(sec, dict) and is_truthy_value(
            sec.get("allow_private_urls"), default=False
        ):
            _cached_allow_private = True
            return _cached_allow_private
        # browser.allow_private_urls（遗留兜底）
        browser = cfg.get("browser", {})
        if isinstance(browser, dict) and is_truthy_value(
            browser.get("allow_private_urls"), default=False
        ):
            _cached_allow_private = True
            return _cached_allow_private
    except Exception:
        # 配置不可用（例如测试、早期导入阶段）—— 保持默认值
        pass

    return _cached_allow_private


def _reset_allow_private_cache() -> None:
    """重置缓存的开关 —— 仅供测试使用。"""
    global _allow_private_resolved, _cached_allow_private
    _allow_private_resolved = False
    _cached_allow_private = False


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """若该 IP 应当为 SSRF 防护而被拦截，则返回 True。"""
    # IPv4 映射的 IPv6 地址（``::ffff:x.x.x.x``）应按其内嵌的 IPv4 地址来检查，
    # 而不是当作 IPv6
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        embedded_ip = ip.ipv4_mapped
        return (embedded_ip.is_private or embedded_ip.is_loopback or
                embedded_ip.is_link_local or embedded_ip.is_reserved or
                embedded_ip.is_multicast or embedded_ip.is_unspecified or
                embedded_ip in _CGNAT_NETWORK)

    # 标准的 IPv4/IPv6 地址检查
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return True
    if ip.is_multicast or ip.is_unspecified:
        return True
    # CGNAT 网段不被 is_private 覆盖
    if ip in _CGNAT_NETWORK:
        return True
    return False


def is_always_blocked_url(url: str) -> bool:
    """当 URL 指向一个始终被拦截的端点时返回 True。

    这是安全底线 —— 云元数据 IP / 主机名
    （169.254.169.254、metadata.google.internal、ECS 任务元数据等），无论后端、
    路由还是 ``allow_private_urls`` 开关如何，都没有合法的 agent 用途。供那些出于
    自身原因绕过完整 ``is_safe_url`` 检查的调用方使用（例如混合云浏览器把私有 URL
    路由到本地 Chromium 边车），它们在放行请求之前仍需要强制执行这条不可协商的底线。

    在以下情况返回 True（= 被拦截）：
      - 主机名在 ``_BLOCKED_HOSTNAMES`` 中
      - IP / 网段在 ``_ALWAYS_BLOCKED_IPS`` / ``_ALWAYS_BLOCKED_NETWORKS`` 中
      - URL 的主机名解析到上述任一项

    在以下情况返回 False（= 不在始终拦截的底线中）：
      - 良性的公网 / 私网 / 回环 URL（无论普通 SSRF 检查是否会拦截它们）
      - 非哨兵主机名的 DNS 解析失败（这是别人的问题 —— 如果适用，
        调用方普通的失败即关闭路径会捕获它们）
      - 解析错误（由调用方决定失败即放行还是失败即关闭）

    刻意比 ``is_safe_url`` 更窄：只拦截哨兵集合，不拦截普通的私有地址。
    想要完整 SSRF 检查的调用方仍应使用 ``is_safe_url``。
    """
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").strip().lower().rstrip(".")
        if not hostname:
            return False

        # 主机名拦截检查无论 DNS 解析结果如何都会触发
        if hostname in _BLOCKED_HOSTNAMES:
            logger.warning(
                "Blocked request to internal hostname (always-blocked floor): %s",
                hostname,
            )
            return True

        # 字面量 IP → 直接对照始终拦截集合检查
        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            ip = None

        if ip is not None:
            if ip in _ALWAYS_BLOCKED_IPS or any(
                ip in net for net in _ALWAYS_BLOCKED_NETWORKS
            ):
                logger.warning(
                    "Blocked request to cloud metadata address "
                    "(always-blocked floor): %s",
                    hostname,
                )
                return True
            return False

        # 主机名 → 解析并检查每一个结果。DNS 失败并不算始终拦截
        # （由调用方的普通路径处理）。
        try:
            addr_info = socket.getaddrinfo(
                hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
        except socket.gaierror:
            return False

        for _family, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            if '%' in ip_str:
                ip_str = ip_str.split('%')[0]
            try:
                resolved = ipaddress.ip_address(ip_str)
            except ValueError:
                logger.warning("Unparseable IP address %r for hostname %s — skipping address", sockaddr[0], hostname)
                continue
            if resolved in _ALWAYS_BLOCKED_IPS or any(
                resolved in net for net in _ALWAYS_BLOCKED_NETWORKS
            ):
                logger.warning(
                    "Blocked request to cloud metadata address "
                    "(always-blocked floor): %s -> %s",
                    hostname,
                    ip_str,
                )
                return True

        return False

    except Exception as exc:
        # 解析失败或意外错误 —— 不要声称该 URL 属于始终拦截。
        # 由调用方决定如何处理畸形 URL。
        logger.debug("is_always_blocked_url error for %s: %s", url, exc)
        return False


def _allows_private_ip_resolution(hostname: str, scheme: str) -> bool:
    """当受信任的 HTTPS 主机名可以绕过 IP 类别拦截时返回 True。"""
    return scheme == "https" and hostname in _TRUSTED_PRIVATE_IP_HOSTS


def is_safe_url(url: str) -> bool:
    """当 URL 目标不是私有/内部地址时返回 True。

    把主机名解析为 IP 并对照私网范围检查。失败即关闭：DNS 错误和意外异常都会拦截
    请求。

    当启用了 ``security.allow_private_urls``（或环境变量
    ``HERMES_ALLOW_PRIVATE_URLS=true``）时，会跳过私有 IP 拦截。云元数据端点
    （169.254.169.254、metadata.google.internal）无论开关如何都始终被拦截 ——
    它们绝不是合法的 agent 目标。
    """
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").strip().lower().rstrip(".")
        scheme = (parsed.scheme or "").strip().lower()
        if scheme not in {"http", "https"}:
            logger.warning("Blocked request — unsupported URL scheme: %s", scheme or "<empty>")
            return False
        if not hostname:
            return False

        # 拦截已知的内部主机名 —— 始终如此，即使开关已打开
        if hostname in _BLOCKED_HOSTNAMES:
            logger.warning("Blocked request to internal hostname: %s", hostname)
            return False

        # 在拦截了元数据主机名之后再检查全局开关
        allow_all_private = _global_allow_private_urls()

        allow_private_ip = _allows_private_ip_resolution(hostname, scheme)

        # 尝试解析并检查 IP
        try:
            addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            # DNS 解析失败 —— 失败即关闭。如果 DNS 解析不了它，HTTP 客户端也会失败，
            # 所以拦截并不会损失什么。
            logger.warning("Blocked request — DNS resolution failed for: %s", hostname)
            return False

        for family, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            if '%' in ip_str:
                ip_str = ip_str.split('%')[0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                # 去掉 scope ID 之后仍无法解析 —— 失败即关闭
                logger.warning("Blocked request — unparseable IP address %r for hostname %s", sockaddr[0], hostname)
                return False

            # 始终拦截云元数据 IP 和链路本地地址，即使开关已打开
            if ip in _ALWAYS_BLOCKED_IPS or any(ip in net for net in _ALWAYS_BLOCKED_NETWORKS):
                logger.warning(
                    "Blocked request to cloud metadata address: %s -> %s",
                    hostname, ip_str,
                )
                return False

            if not allow_all_private and not allow_private_ip and _is_blocked_ip(ip):
                logger.warning(
                    "Blocked request to private/internal address: %s -> %s",
                    hostname, ip_str,
                )
                return False

        if allow_all_private:
            logger.debug(
                "Allowing private/internal resolution (security.allow_private_urls=true): %s",
                hostname,
            )
        elif allow_private_ip:
            logger.debug(
                "Allowing trusted hostname despite private/internal resolution: %s",
                hostname,
            )

        return True

    except Exception as exc:
        # 遇到意外错误时失败即关闭 —— 不要让解析的边界情况变成 SSRF 绕过向量
        logger.warning("Blocked request — URL safety check error for %s: %s", url, exc)
        return False


async def async_is_safe_url(url: str) -> bool:
    """规则与 :func:`is_safe_url` 相同，但把 DNS 工作放到事件循环之外执行。

    ``socket.getaddrinfo`` 可能阻塞；请在 async 代码路径（网关、
    ``web_extract_tool``、视觉下载钩子）中调用本函数，而不是 ``is_safe_url``。
    """
    return await asyncio.to_thread(is_safe_url, url)
