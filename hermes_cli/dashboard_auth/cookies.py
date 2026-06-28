"""Dashboard auth 的 Cookie 辅助函数。

涉及三个 cookie：
  - hermes_session_at:   OAuth access token
                         （HttpOnly，生命周期 = token TTL，约 15 分钟）
  - hermes_session_rt:   OAuth refresh token
                         （HttpOnly，生命周期 = 24 小时，轮换 + 重用检测）
                         Nous Portal 为 dashboard auth-code 授权发放轮换的
                         refresh token（Portal NAS #293 / hermes #37247）。
                         ``set_session_cookies`` 在提供者返回非空
                         ``refresh_token`` 时写入此 cookie；中间件使用它
                         在 AT 过期时透明地轮换获取新的 access token。
                         省略 refresh token（空字符串）的提供者会优雅地
                         降级为仅 access token 的 session——RT cookie
                          simply 不会被写入。
  - hermes_session_pkce: 短期 PKCE 状态 + CSRF nonce + 提供者提示
                         （HttpOnly，生命周期 = 10 分钟）

三者均为 ``SameSite=Lax``（浏览器会在跨站 GET 顶级导航时发送，
IDP 重定向回 ``/auth/callback`` 时需要此行为），并位于前缀的 Path 下。
``Secure`` 仅在 dashboard 通过 HTTPS 访问时设置——通过请求 URL 的
scheme 检测，当 uvicorn 配置了 ``proxy_headers=True`` 时会 honour
Fly 的 TLS 终结器上游的 ``X-Forwarded-Proto``。回环开发流量始终为
HTTP，因此设置 ``Secure`` 会导致 cookie 在浏览器中被锁定。

Cookie 前缀选择（浏览器加固，参见
https://datatracker.ietf.org/doc/html/draft-west-cookie-prefixes）：

  * 回环 HTTP — 裸名称。``__Host-`` / ``__Secure-`` 需要 ``Secure``，
    与 HTTP 不兼容。
  * 门控 HTTPS，直接部署（Path=/）— ``__Host-`` 前缀。将 cookie
    绑定到确切源（无 Domain 属性）——最强的规范保证。
  * 门控 HTTPS，反向代理前缀（Path=/hermes）— ``__Secure-`` 前缀。
    ``__Host-`` 在 Path != "/" 时不允许使用；``__Secure-`` 保留了
    Secure 要求的加固而没有 Path 约束，显式的 ``Path=/hermes``
    覆盖了同源应用隔离。

设置器和读取器都查询活动前缀，因为 cookie *名称* 会变化——
如果设置器写入了 ``__Secure-hermes_session_at`` 而读取器查找裸名称，
则永远找不到值。

Refresh token 处理：
   ``set_session_cookies`` 接受 ``refresh_token=""``（提供者省略了它）
   并静默跳过写入 RT cookie，因此无 refresh token 的提供者会降级为
   仅 access token 的 session。``clear_session_cookies`` 在注销 / session
   过期时始终为 RT cookie 发出 Max-Age=0 删除，以便清除早期部署留下的
   过期 cookie。透明轮换流程（"过期 AT + 有效 RT → 服务端轮换，
   否则 401 → /login"）位于 ``middleware._attempt_refresh``。
"""
from __future__ import annotations

from typing import Optional, Tuple

from fastapi import Request
from fastapi.responses import Response

# 裸 cookie 名称——请求作用域的 ``_resolved_name`` 辅助函数
# 根据请求的 HTTPS + 前缀组合决定是否添加 ``__Host-`` / ``__Secure-`` 前缀。
SESSION_AT_COOKIE = "hermes_session_at"
SESSION_RT_COOKIE = "hermes_session_rt"
PKCE_COOKIE = "hermes_session_pkce"

# 可能需要回读的名称变体。排序使最严格的前缀在迭代时优先
# （实践中不应同时存在多个变体——单个请求只发出一个变体）。
_NAME_VARIANTS = ("__Host-", "__Secure-", "")

# RT cookie Max-Age。设为 30 天作为 cookie 浏览器生命周期的宽松上限；
# Portal 实际的 refresh token TTL（24 小时，轮换）才是权威依据——
# 一旦 RT 本身过期/轮换掉，刷新尝试会返回 400 → RefreshExpiredError →
# 干净重新登录，无论 cookie 存留多久。（此处不收紧到 24 小时，
# 以避免将 cookie 生命周期耦合到可独立更改的服务端 TTL；
# 如果过期 cookie 的刷新抖动成为问题再重新评估。）
_RT_MAX_AGE = 30 * 24 * 60 * 60
_PKCE_MAX_AGE = 10 * 60


def _resolved_name(bare: str, *, use_https: bool, prefix: str) -> str:
    """为活动请求形态选择 cookie 前缀变体。

    前缀选择规则见模块文档字符串。设置器与读取器之间的不匹配会
    静默破坏 session，因此此函数是命名的唯一真实来源。
    """
    if not use_https:
        return bare
    if prefix:
        # Path != "/" 禁止使用 __Host-；回退到 __Secure-。
        return f"__Secure-{bare}"
    return f"__Host-{bare}"


def _cookie_path(prefix: str) -> str:
    """活动部署形态的 cookie ``Path`` 属性。

    在 ``X-Forwarded-Prefix: /hermes`` 下需要 ``Path=/hermes``，以便：
      a) 浏览器在前缀下的请求上发送 cookie
         （如果请求路径不以 Path 开头，浏览器会省略 cookie）；
      b) cookie 不会泄露到同源的其他应用
         （``mission-control.tilos.com/billing/...``）。

    直接部署（无代理前缀）使用 ``Path=/``。
    """
    return prefix if prefix else "/"


def _common_attrs(*, use_https: bool, prefix: str) -> dict:
    attrs: dict = {
        "httponly": True,
        "samesite": "lax",
        "path": _cookie_path(prefix),
    }
    if use_https:
        attrs["secure"] = True
    return attrs


def set_session_cookies(
    response: Response,
    *,
    access_token: str,
    refresh_token: str,
    access_token_expires_in: int,
    use_https: bool,
    prefix: str = "",
) -> None:
    """在响应上设置 session cookie。

    ``access_token_expires_in`` 以秒为单位。使用提供者报告的
    access token TTL。

    ``refresh_token`` 非空时写入 RT cookie。Nous Portal 发放
    24 小时轮换的 refresh token（hermes #37247）；省略它的提供者
    返回 ``Session.refresh_token == ""``，我们 simply 不持久化
    RT cookie——session 随后表现为仅 access token，直到 AT 过期。
    两种情况之间没有其他分支变化。

    ``prefix`` 是规范化的 X-Forwarded-Prefix 值（例如 ``/hermes``）
    或 ``""`` 表示直接部署。它同时影响 cookie 名称
    （``__Host-`` vs ``__Secure-`` vs 裸名）和 ``Path`` 属性。
    """
    response.set_cookie(
        _resolved_name(SESSION_AT_COOKIE, use_https=use_https, prefix=prefix),
        access_token,
        max_age=access_token_expires_in,
        **_common_attrs(use_https=use_https, prefix=prefix),
    )
    # 契约 v1：空 refresh token 表示"不持久化 RT cookie"。
    # 保留一个空值的 cookie 充其量是死状态，最坏情况是攻击面。
    if refresh_token:
        response.set_cookie(
            _resolved_name(SESSION_RT_COOKIE, use_https=use_https, prefix=prefix),
            refresh_token,
            max_age=_RT_MAX_AGE,
            **_common_attrs(use_https=use_https, prefix=prefix),
        )


def clear_session_cookies(response: Response, *, prefix: str = "") -> None:
    """为两个 session cookie 发出 Max-Age=0 删除。

    要可靠地删除 cookie，删除的 ``Path`` 必须匹配设置时的路径，
    且 cookie 名称必须匹配设置器使用的变体。我们不知道最初设置的是
    哪个变体（cookie 前缀取决于设置它的请求），因此我们为活动路径下
    所有可能的变体发出删除。
    """
    path = _cookie_path(prefix)
    for variant in _NAME_VARIANTS:
        response.set_cookie(
            f"{variant}{SESSION_AT_COOKIE}", "", max_age=0,
            path=path, httponly=True, samesite="lax",
        )
        response.set_cookie(
            f"{variant}{SESSION_RT_COOKIE}", "", max_age=0,
            path=path, httponly=True, samesite="lax",
        )


def set_pkce_cookie(
    response: Response, *, payload: str, use_https: bool, prefix: str = "",
) -> None:
    response.set_cookie(
        _resolved_name(PKCE_COOKIE, use_https=use_https, prefix=prefix),
        payload,
        max_age=_PKCE_MAX_AGE,
        **_common_attrs(use_https=use_https, prefix=prefix),
    )


def clear_pkce_cookie(response: Response, *, prefix: str = "") -> None:
    path = _cookie_path(prefix)
    for variant in _NAME_VARIANTS:
        response.set_cookie(
            f"{variant}{PKCE_COOKIE}", "", max_age=0,
            path=path, httponly=True, samesite="lax",
        )


def _read_with_fallback(
    request: Request, bare_name: str,
) -> Optional[str]:
    """通过按顺序检查每个前缀变体来读取 cookie。

    设置器根据活动请求形态选择一个变体；读取器不知道哪个变体
    生效了（读取 cookie 的请求在极端情况下可能与设置它的请求
    形态不同）。尝试全部三个变体保证能找到它。
    """
    for variant in _NAME_VARIANTS:
        value = request.cookies.get(f"{variant}{bare_name}")
        if value is not None:
            return value
    return None


def read_session_cookies(request: Request) -> Tuple[Optional[str], Optional[str]]:
    """返回 (access_token, refresh_token)，两者均可能为 None。"""
    at = _read_with_fallback(request, SESSION_AT_COOKIE)
    rt = _read_with_fallback(request, SESSION_RT_COOKIE)
    return at, rt


def read_pkce_cookie(request: Request) -> Optional[str]:
    return _read_with_fallback(request, PKCE_COOKIE)


def detect_https(request: Request) -> bool:
    """决定是否设置 ``Secure`` cookie 标志。

    读取 ``request.url.scheme``——在 uvicorn 的 ``proxy_headers=True``
    下（当门控激活时 start_server 会启用），这会 honour Fly TLS 终结器
    上游的 ``X-Forwarded-Proto``。回环流量始终为 HTTP，因此此处返回 False。
    """
    return request.url.scheme == "https"
