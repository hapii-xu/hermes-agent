"""Dashboard auth 提供者的抽象基类 + 数据类 + 异常。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Session:
    """一个已验证的身份。由 ``complete_login`` 和 ``verify_session`` 返回。

    所有字段都是必填的。没有 org 概念的提供者应将 ``org_id`` 设为空字符串。
    ``access_token`` 和 ``refresh_token`` 对 Hermes 是不透明的——由提供者特定实现。
    """

    user_id: str
    email: str
    display_name: str
    org_id: str
    provider: str
    expires_at: int  # unix 秒数；access_token 的 exp 声明
    access_token: str
    refresh_token: str


@dataclass(frozen=True)
class LoginStart:
    """OAuth 往返的第一阶段。

    ``redirect_url`` 是浏览器必须导航到的 URL（例如 Portal 的
    ``/oauth/authorize``）。``cookie_payload`` 是一个 cookie 名称→序列化值
    的字典，认证路由会在响应中 ``Set-Cookie``。用于 PKCE 状态、CSRF nonce 等。
    此处设置的 cookie 必须是 HttpOnly + Secure（HTTPS 时）+ SameSite=Lax，
    且 TTL ≤ 10 分钟（登录有效期）。
    """

    redirect_url: str
    cookie_payload: dict[str, str]


class ProviderError(Exception):
    """IDP 不可达、网络错误或其他临时性故障。

    中间件会将此转换为 HTTP 503。
    """


class InvalidCodeError(Exception):
    """OAuth 回调的 ``code`` / ``state`` 验证失败。

    中间件会将此转换为 HTTP 400。
    """


class InvalidCredentialsError(Exception):
    """用户名/密码对被密码提供者拒绝。

    由 :meth:`DashboardAuthProvider.complete_password_login` 抛出。
    ``/auth/password-login`` 路由将此转换为 HTTP 401，并带有
    故意通用的详细信息（不区分"未知用户"和"密码错误"），
    以防止该端点被用作用户名枚举。
    """


class RefreshExpiredError(Exception):
    """Refresh token 已失效。

    中间件会清除 cookie 并强制重新登录（302 → ``/login``）。
    """


class DashboardAuthProvider(ABC):
    """每个 dashboard-auth 提供者插件实现的协议。

    生命周期：
      1. ``start_login`` — 用户在登录页点击"使用 X 登录"。
         提供者返回一个重定向 URL 和任何需要存入短期 cookie 的
         PKCE/CSRF 状态。
      2. 浏览器经过 OAuth IDP 跳转后到达 /auth/callback。
      3. ``complete_login`` — 用 code + verifier 换取 Session。
      4. ``verify_session`` — 每个请求都会调用此方法以验证 cookie 中的
         access token。如果 token 过期或无效则返回 ``None``
         （中间件随后触发刷新或注销）。
      5. ``refresh_session`` — 当 access token 即将过期时调用。
         返回一个带有轮换后 token 的新 Session。
      6. ``revoke_session`` — 在 /auth/logout 时调用。尽力而为。

    失败语义：
      * ``start_login`` 在 IDP 不可达时可抛出 ``ProviderError``。
      * ``complete_login`` 在 code/state 错误时抛出 ``InvalidCodeError``；
        IDP 不可达时抛出 ``ProviderError``。
      * ``verify_session`` 在过期/未知 token 时返回 ``None``；
        IDP 不可达时抛出 ``ProviderError``。中间件对过期和不可达
        的处理不同（过期→刷新；不可达→503）。
      * ``refresh_session`` 在 refresh token 也无效时抛出
        ``RefreshExpiredError``；中间件随后强制重新登录。
        网络故障时抛出 ``ProviderError``。
      * ``revoke_session`` 尽力而为，不得抛出异常。

    子类必须设置 ``name``（小写标识符，永久稳定）
    和 ``display_name``（登录页上面向用户的标签）。

    密码（非重定向）提供者：
      使用用户名+密码而非 OAuth 重定向进行认证的提供者需设置
      ``supports_password = True`` 并实现 ``complete_password_login``。
      登录页随后会渲染一个凭据表单（POST 到 ``/auth/password-login``），
      而非"使用 X 登录"重定向按钮。登录后的所有下游流程——
      ``verify_session`` / ``refresh_session`` / ``revoke_session``、
      session cookie、WS-ticket 生成——与 OAuth 路径相同，
      因为密码 session 只是一个带有提供者生成的不透明 token 的
      :class:`Session`。OAuth 方法（``start_login`` /
      ``complete_login``）仍然是抽象的；永远不会通过重定向流程
      访问的纯密码提供者可以将它们实现为抛出
      ``NotImplementedError`` 的桩。
    """

    name: str = ""
    display_name: str = ""

    # 当为 True 时，此提供者通过用户名+密码（``complete_password_login``）
    # 而非（或除了）OAuth 重定向流程进行认证。登录页为此类提供者渲染
    # 凭据表单；``/auth/password-login`` 路由分派到
    # ``complete_password_login``。仅 OAuth 提供者保持此值为 False，
    # 且完全不受影响。
    supports_password: bool = False

    @abstractmethod
    def start_login(self, *, redirect_uri: str) -> LoginStart: ...

    @abstractmethod
    def complete_login(
        self,
        *,
        code: str,
        state: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> Session: ...

    @abstractmethod
    def verify_session(self, *, access_token: str) -> Optional[Session]: ...

    @abstractmethod
    def refresh_session(self, *, refresh_token: str) -> Session: ...

    @abstractmethod
    def revoke_session(self, *, refresh_token: str) -> None: ...

    def complete_password_login(
        self, *, username: str, password: str
    ) -> "Session":
        """验证用户名/密码对并生成一个 :class:`Session`。

        仅在 ``supports_password`` 为 True 时调用
        （``/auth/password-login`` 路由会检查此标志）。默认实现
        抛出 ``NotImplementedError``，这样忘记设置标志的纯 OAuth
        提供者会快速失败而非静默接受凭据。

        返回的 ``Session`` 带有提供者生成的不透明
        ``access_token`` / ``refresh_token``，与 OAuth 路径完全相同，
        因此所有下游 session 处理（cookie、验证、刷新、
        ws-tickets、注销）都是一致的。

        失败语义：
          * ``InvalidCredentialsError`` — 用户名/密码被拒绝。
            路由返回通用的 401（不区分用户与密码）。实现应对
            未知用户花费恒定时间（虚拟 hash 验证）以避免时序枚举。
          * ``ProviderError`` — 后端凭据存储不可达
            （LDAP/DB 宕机）；路由返回 503。
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support password login "
            "(set supports_password = True and override "
            "complete_password_login)"
        )


def assert_protocol_compliance(cls: type) -> None:
    """如果 ``cls`` 未完整实现提供者协议则抛出 ``TypeError``。

    在每个提供者插件的单元测试中调用此函数::

        def test_protocol_compliance():
            assert_protocol_compliance(MyProvider)

    成功时返回 ``None``，以便调用者可以显式断言。
    """
    required_methods = (
        "start_login",
        "complete_login",
        "verify_session",
        "refresh_session",
        "revoke_session",
    )
    required_attrs = ("name", "display_name")

    for attr in required_attrs:
        val = getattr(cls, attr, "")
        if not val:
            raise TypeError(
                f"{cls.__name__} missing or empty attribute: {attr!r}"
            )
    for method in required_methods:
        if not callable(getattr(cls, method, None)):
            raise TypeError(f"{cls.__name__} missing method: {method}")
    # 同时捕获 ABC 未被覆盖的情况。
    if getattr(cls, "__abstractmethods__", None):
        raise TypeError(
            f"{cls.__name__} has unimplemented abstract methods: "
            f"{sorted(cls.__abstractmethods__)}"
        )
