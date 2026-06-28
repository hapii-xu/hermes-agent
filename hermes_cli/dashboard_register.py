"""``hermes dashboard register`` — 注册自托管 dashboard OAuth 客户端。

自动化用户原本需要手动完成的操作：在浏览器中打开 Nous Portal
的 ``/local-dashboards`` 页面，点击"register"，复制生成的
``agent:{id}`` OAuth 客户端 ID，然后将其粘贴到 ``~/.hermes/.env``
中作为 ``HERMES_DASHBOARD_OAUTH_CLIENT_ID``。

此命令的执行流程：
  1. 从现有登录（``~/.hermes/auth.json``）解析一个新的 Nous Portal
     access token，必要时进行刷新。如果用户未登录，则快速失败并
     提示"运行 `hermes setup`"。
  2. 使用该 bearer token 向 ``{portal}/api/oauth/self-hosted-client``
     发送 POST 请求，创建一个由调用者所在 org 拥有的 SELF_HOSTED
     agent 客户端，并返回完整的 ``agent:{id}`` client_id。
  3. 以幂等方式将 ``HERMES_DASHBOARD_OAUTH_CLIENT_ID`` 和
     （如果缺失）``HERMES_DASHBOARD_PORTAL_URL`` 写入 ``~/.hermes/.env``。
  4. 打印注册后的提示信息，说明 OAuth 门控仅在非 loopback 绑定时生效。

Portal 端点是此功能的 NAS 部分（POST /api/oauth/self-hosted-client）。
``agent:`` 前缀由服务端添加，因此此客户端无需了解命名空间约定。
"""

from __future__ import annotations

import json
import os
import random
import sys
import urllib.error
import urllib.request
from typing import Optional


# Docker 风格的名称生成器。与 Docker 的 adjective_surname 类似，但使用
# adjective_noun 加无空格下划线连接，便于直接放入 label 字段。Portal 端
# 没有唯一性约束（行 id 是主键），因此碰撞无害，无需重试。
_NAME_ADJECTIVES = (
    "amber", "bold", "brave", "bright", "calm", "clever", "cosmic", "crisp",
    "dreamy", "eager", "electric", "fancy", "gentle", "golden", "happy",
    "hidden", "jolly", "keen", "lively", "lucid", "lunar", "mellow", "merry",
    "mighty", "nimble", "noble", "polished", "quiet", "quirky", "rapid",
    "serene", "sharp", "shiny", "silent", "snappy", "solar", "spry", "stellar",
    "sunny", "swift", "tidy", "vivid", "vibrant", "witty", "zesty",
)

_NAME_NOUNS = (
    "albatross", "antelope", "badger", "beacon", "comet", "condor", "cypress",
    "dolphin", "ember", "falcon", "ferret", "galaxy", "glacier", "harbor",
    "heron", "ibex", "jaguar", "kestrel", "lantern", "lynx", "meadow", "nebula",
    "ocelot", "orchid", "otter", "panther", "petrel", "quasar", "raven", "reef",
    "sparrow", "summit", "tundra", "vortex", "walrus", "willow", "yarrow",
    # 几个 Docker 风格的科学家姓氏。
    "kepler", "tesla", "curie", "hopper", "turing", "lovelace",
)


def _generate_dashboard_name() -> str:
    """返回一个人类可读的 ``adjective_noun`` 名称（Docker 风格）。"""
    return f"{random.choice(_NAME_ADJECTIVES)}_{random.choice(_NAME_NOUNS)}"


def _resolve_portal_base_url(override: Optional[str] = None) -> str:
    """解析注册请求的 portal base URL。

    优先级：
      1. ``override`` — 显式的 ``--portal-url`` 标志或
         ``HERMES_DASHBOARD_PORTAL_URL`` 环境变量（用于针对预览/预发布
         portal 进行测试）。注意：access token 必须在此 portal 上有效 —
         它由你登录的 portal 签发，因此只有当 token 的签发方匹配时
         override 才有效（例如你登录了同一个预发布/预览 portal）。
      2. Nous 登录中存储的 ``portal_base_url`` — 这是签发 token 的
         portal，因此是正确的默认目标。
      3. 生产环境默认值。
    """
    if isinstance(override, str) and override.strip():
        return override.rstrip("/")
    try:
        from hermes_cli.auth import DEFAULT_NOUS_PORTAL_URL, get_provider_auth_state

        state = get_provider_auth_state("nous") or {}
        base = state.get("portal_base_url")
        if isinstance(base, str) and base.strip():
            return base.rstrip("/")
        return str(DEFAULT_NOUS_PORTAL_URL).rstrip("/")
    except Exception:
        return "https://portal.nousresearch.com"


def _register_self_hosted_client(
    *,
    access_token: str,
    portal_base_url: str,
    name: Optional[str],
    custom_redirect_uri: Optional[str],
    existing_client_id: Optional[str] = None,
    timeout: float = 15.0,
) -> dict:
    """向 portal 的 self-hosted-client 端点发送 POST 请求并返回 JSON body。

    当提供了 ``existing_client_id``（此安装在之前运行中持久化的 client_id）
    时，会将其发送出去，以便 portal 原地更新该现有 dashboard 记录，而不是
    创建重复记录 — 这正是使 ``hermes dashboard register`` 幂等的原因。
    如果该 id 在调用者 org 中不再对应有效记录（已过期/已删除），portal
    会回退到创建新客户端，因此传递它始终是安全的。

    在幂等更新路径（重新运行且未显式指定 ``--name``）上，``name`` 可能为
    ``None``：省略它会告诉 portal 保留已存储的名称而不是覆盖。在创建路径
    上它是必需的；调用方保证在那里提供值。

    对任何非 2xx 响应或传输失败，抛出带有用户可见消息的 RuntimeError。
    """
    url = f"{portal_base_url.rstrip('/')}/api/oauth/self-hosted-client"
    body: dict[str, str] = {}
    if name:
        body["name"] = name
    if custom_redirect_uri:
        body["custom_redirect_uri"] = custom_redirect_uri
    if existing_client_id:
        body["client_id"] = existing_client_id

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        # 端点返回结构化 JSON 错误（{error, error_description}）。
        detail = ""
        try:
            err_body = json.loads(exc.read().decode())
            detail = (
                err_body.get("error_description")
                or err_body.get("error")
                or ""
            )
        except Exception:
            pass
        if exc.code == 401:
            raise RuntimeError(
                "Nous Portal rejected the access token (401). "
                "Try `hermes auth login nous` to re-authenticate."
            ) from exc
        if exc.code == 403:
            raise RuntimeError(
                detail
                or "Your account is not permitted to register a self-hosted dashboard."
            ) from exc
        raise RuntimeError(
            f"Portal returned HTTP {exc.code}"
            + (f": {detail}" if detail else "")
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not reach Nous Portal at {portal_base_url}: {exc.reason}"
        ) from exc

    if not isinstance(payload, dict) or not payload.get("client_id"):
        raise RuntimeError("Portal returned an unexpected response (no client_id).")
    return payload


def _print_post_register_hint(
    *,
    client_id: str,
    portal_base_url: str,
    custom_redirect_uri: Optional[str],
    wrote_portal_url: bool,
    public_url: str = "",
) -> None:
    """打印成功摘要和门控生效提示。"""
    from hermes_cli.config import get_env_path

    env_path = get_env_path()
    _cid = client_id
    print()
    print(f"  Wrote to {env_path}:")
    print("    HERMES_DASHBOARD_OAUTH_CLIENT_ID=" + str(_cid))
    if wrote_portal_url:
        print("    HERMES_DASHBOARD_PORTAL_URL=" + str(portal_base_url))
    if public_url:
        print("    HERMES_DASHBOARD_PUBLIC_URL=" + str(public_url))
    print()
    print(
        "  Heads up — Nous login only *engages* on a non-loopback bind. A plain\n"
        "  `hermes dashboard` (localhost) leaves the gate off and serves locally\n"
        "  without auth, which is fine for your own machine."
    )
    print()
    if custom_redirect_uri:
        # 推导用户注册的主机名，使示例与其匹配。
        try:
            from urllib.parse import urlparse

            host = urlparse(custom_redirect_uri).hostname or "your-host"
        except Exception:
            host = "your-host"
        print("  To require Nous login on your registered host, run the dashboard")
        print(f"  bound publicly (it must be reachable at https://{host}) and log in")
        print("  at its /login page.")
    else:
        print("  To require Nous login (e.g. exposing on your LAN or a public host):")
        print("    hermes dashboard --host 0.0.0.0")
        print("  …then log in at the dashboard's /login page.")
    print()
    print(
        "  If the dashboard is already running, restart it to pick up the new env."
    )
    print(
        f"  Manage or revoke this dashboard at {portal_base_url}/local-dashboards"
    )


def cmd_dashboard_register(args) -> None:
    """在 Nous Portal 注册自托管 dashboard OAuth 客户端。"""
    from hermes_cli.auth import AuthError, resolve_nous_access_token
    from hermes_cli.config import get_env_value, is_managed, save_env_value

    # 托管（Docker/hosted）安装的 dashboard OAuth client_id 由编排器注入
    # （NAS 通过 buildContainerEnvVars 设置 HERMES_DASHBOARD_OAUTH_CLIENT_ID）。
    # 从此类容器内注册是错误的 — 而且 save_env_value 无论如何都会拒绝写入。
    if is_managed():
        print(
            "✗ `hermes dashboard register` is not available in a managed/hosted "
            "install.\n"
            "  The dashboard OAuth client is provisioned by the hosting platform."
        )
        sys.exit(1)

    # 1. 解析一个新的 Nous access token（临近过期时自动刷新）。
    #    如果用户未登录，则快速失败并提示 setup。
    try:
        access_token = resolve_nous_access_token()
    except AuthError as exc:
        if getattr(exc, "relogin_required", False):
            print("✗ You're not logged into Nous Portal.")
            print("  Run `hermes setup` (or `hermes auth login nous`) first, then retry.")
        else:
            print(f"✗ Could not resolve a Nous Portal access token: {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"✗ Could not resolve a Nous Portal access token: {exc}")
        sys.exit(1)

    # Portal override：显式的 --portal-url 标志优先，其次是
    # HERMES_DASHBOARD_PORTAL_URL 环境变量，最后是存储的登录 portal。
    #
    # 我们分别跟踪自定义 URL 是否被*显式提供*（标志或环境变量）与
    # 解析后的值。显式的自定义 URL 是用户想要持久化的有意选择（如果
    # .env 中已存在则原地更新）；而从存储的登录信息推断出的 portal
    # 则保留较旧的、仅在缺失时写入的保守行为，避免在常见生产场景中
    # 污染 .env 文件。
    portal_override = getattr(args, "portal_url", None) or os.environ.get(
        "HERMES_DASHBOARD_PORTAL_URL"
    )
    custom_portal_supplied = bool(
        isinstance(portal_override, str) and portal_override.strip()
    )
    portal_base_url = _resolve_portal_base_url(portal_override)

    # 幂等性：如果此安装已经注册过 dashboard，我们会在本地保留其
    # client_id（HERMES_DASHBOARD_OAUTH_CLIENT_ID）。重新发送它以便
    # portal 更新该现有记录而不是创建重复记录。没有存储的 client_id
    # -> 这是首次注册 -> 创建新的（原始行为）。这镜像了 portal 的规则：
    # 无 client id = 新 dashboard；有 client id = 要修改的记录的稳定键。
    existing_client_id = None
    try:
        existing_client_id = get_env_value("HERMES_DASHBOARD_OAUTH_CLIENT_ID")
    except Exception:
        existing_client_id = None
    if isinstance(existing_client_id, str):
        existing_client_id = existing_client_id.strip() or None
    else:
        existing_client_id = None

    explicit_name = getattr(args, "name", None)
    # 仅在首次注册时自动生成随机名称。在重新运行时（我们持有 client_id）
    # 且未显式指定 --name 时，保留 portal 已存储的名称，而不是每次都
    # 生成新的随机值 — 因此保持 `name` 未设置，让 portal 保留它。
    if explicit_name:
        name = explicit_name
    elif existing_client_id:
        name = None
    else:
        name = _generate_dashboard_name()
    custom_redirect_uri = getattr(args, "redirect_uri", None)

    # 2. 向 portal 注册。
    try:
        result = _register_self_hosted_client(
            access_token=access_token,
            portal_base_url=portal_base_url,
            name=name,
            custom_redirect_uri=custom_redirect_uri,
            existing_client_id=existing_client_id,
        )
    except RuntimeError as exc:
        print(f"✗ Registration failed: {exc}")
        sys.exit(1)

    client_id = str(result["client_id"])
    registered_name = str(result.get("name") or name or "")

    # 区分创建与更新：portal 在原地更新时会回传相同的 client_id。
    updated_existing = bool(
        existing_client_id and client_id == existing_client_id
    )
    if updated_existing:
        print(f'✓ Updated dashboard "{registered_name}"')
    else:
        print(f'✓ Registered dashboard "{registered_name}"')

    # 3. 幂等地写入环境变量。始终设置 client_id。
    try:
        save_env_value("HERMES_DASHBOARD_OAUTH_CLIENT_ID", client_id)
    except Exception as exc:
        print(f"✗ Failed to write HERMES_DASHBOARD_OAUTH_CLIENT_ID to .env: {exc}")
        print(f"  Set it manually:  HERMES_DASHBOARD_OAUTH_CLIENT_ID={client_id}")
        sys.exit(1)

    # 持久化 portal URL。两种情况：
    #   a) 用户显式提供了自定义 portal（--portal-url 标志或
    #      HERMES_DASHBOARD_PORTAL_URL 环境变量）。这是一个有意选择，
    #      我们总是持久化它以便跨会话保留 — 原地覆盖任何现有条目
    #      （save_env_value 更新匹配的键而不是追加重复项）。即使值
    #      等于生产环境默认值也是如此：是用户显式请求的。
    #   b) 未提供自定义 portal。保留较旧的保守行为：仅当尚未配置
    #      且与生产环境默认值不同时，才写入从存储的登录信息推断
    #      出的 portal，避免在常见生产场景中污染 .env 文件，也
    #      避免意外更改现有条目。
    wrote_portal_url = False
    default_portal = "https://portal.nousresearch.com"
    existing_portal = None
    try:
        existing_portal = get_env_value("HERMES_DASHBOARD_PORTAL_URL")
    except Exception:
        existing_portal = None

    if custom_portal_supplied:
        should_write_portal = existing_portal != portal_base_url
    else:
        should_write_portal = (
            not existing_portal and portal_base_url.rstrip("/") != default_portal
        )

    if should_write_portal:
        try:
            save_env_value("HERMES_DASHBOARD_PORTAL_URL", portal_base_url)
            wrote_portal_url = True
        except Exception:
            # 非致命错误：client_id 才是关键值。
            pass

    # 持久化从 OAuth redirect URI 派生的 dashboard public URL。
    #
    # --redirect-uri 是用户在 portal 注册的完整公共 HTTPS 回调地址，
    # 例如 https://hermes.example.com/auth/callback。在服务端，dashboard
    # auth 层（dashboard_auth/routes._redirect_uri）通过取
    # HERMES_DASHBOARD_PUBLIC_URL 并追加 "/auth/callback" 来重建相同的
    # 回调。因此运行时实际消费的值是 ORIGIN（scheme://host[:port]），
    # 而非完整的回调路径 — 持久化原始 redirect URI 会导致路径重复。
    # 我们从提供的 redirect URI 推导出 origin 并持久化为
    # HERMES_DASHBOARD_PUBLIC_URL，这样运维人员无需重复提供，且
    # public-URL override 能正确连接（门控生效且回调能正确往返）。
    #
    # 与 portal URL 类似，显式提供的值总是被写入（原地更新现有条目
    # 而非追加重复项），已匹配时为 no-op，且永远不会在仅 localhost
    # 安装时写入（无 --redirect-uri）。
    wrote_public_url = False
    public_url = ""
    if custom_redirect_uri:
        try:
            from urllib.parse import urlparse

            parsed = urlparse(custom_redirect_uri)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                public_url = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            public_url = ""

    if public_url:
        existing_public_url = None
        try:
            existing_public_url = get_env_value("HERMES_DASHBOARD_PUBLIC_URL")
        except Exception:
            existing_public_url = None
        if existing_public_url != public_url:
            try:
                save_env_value("HERMES_DASHBOARD_PUBLIC_URL", public_url)
                wrote_public_url = True
            except Exception:
                # 非致命错误：client_id 才是关键值。
                pass

    # 4. 提示信息。
    _print_post_register_hint(
        client_id=client_id,
        portal_base_url=portal_base_url,
        custom_redirect_uri=custom_redirect_uri,
        wrote_portal_url=wrote_portal_url,
        public_url=public_url if wrote_public_url else "",
    )
