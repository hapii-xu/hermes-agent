"""``hermes gateway enroll`` — 使用 relay connector 注册自托管 gateway。

connector⇄gateway 通道经过身份验证（gateway 可能由客户管理并暴露在公网上）。
此命令是 connector 仓库 ``docs/connector-gateway-auth-design.md`` 中
零接触注册流程的 gateway 端：

  1. 从现有登录（``~/.hermes/auth.json``）中解析一个新鲜的 Nous Portal 访问
     token — 与 ``hermes dashboard register`` 使用的路径相同
     （``resolve_nous_access_token``）。这证明了*调用者拥有哪个 Nous org
     （租户）*；connector 通过 ``GET /api/oauth/account`` 从中获取权威租户
     （绝不依赖 gateway 断言的任何内容）。
  2. 将该 token 放在 ``Authorization`` 头中，通过 TLS 向 connector 的
     ``/relay/enroll`` POST ``{enrollmentToken, gatewayId}``。
  3. connector 验证注册 token（签名 + 一次性使用 + 租户匹配），铸造一个
     每 gateway 密钥，获取或创建每租户 delivery key，并一次性返回两者。
  4. 将 ``GATEWAY_RELAY_ID`` / ``GATEWAY_RELAY_SECRET`` /
     ``GATEWAY_RELAY_DELIVERY_KEY``（以及 ``GATEWAY_RELAY_URL``（如果提供））
     持久化到 ``~/.hermes/.env``。每 gateway 密钥用于认证 WS 升级；
     每租户 delivery key 用于验证签名的入站投递。

托管/主机安装不会自行注册：编排器（NAS）直接铸造密钥并将其注入容器环境
变量，因此此命令在 ``is_managed()`` 下拒绝运行（与 ``dashboard register``
一致）。

实验性：relay 认证方案可能会在没有弃用周期的情况下发生变化，直到
≥2 个 Class-1 平台验证该合约。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from typing import Optional


def _default_gateway_id() -> str:
    """一个相对稳定的默认 gateway 实例 id：``<hostname>-<pid-free slug>``。

    gatewayId 用于标识此已注册的实例，以实现 kill-switch 粒度控制
    （connector 通过它索引密钥验证列表）。默认使用主机名以便人工识别；
    可通过 ``--gateway-id`` 覆盖。
    """
    host = ""
    try:
        host = socket.gethostname().strip()
    except Exception:
        host = ""
    return f"gw-{host or 'hermes'}"


def _resolve_connector_url(override: Optional[str]) -> Optional[str]:
    """解析用于注册的 connector 基础 URL（无尾部斜杠）。

    优先级：显式 ``--connector-url`` 参数 > ``GATEWAY_RELAY_URL`` 环境变量 >
    config.yaml 中的 ``gateway.relay_url``。relay URL 是 ``ws(s)://`` 拨号
    目标；注册是向同一主机的 ``http(s)://`` POST，因此我们映射 scheme。
    当没有配置任何内容时返回 None（用户必须提供一个）。
    """
    raw = (override or os.environ.get("GATEWAY_RELAY_URL", "")).strip()
    if not raw:
        try:
            from gateway.run import _load_gateway_config  # late import to avoid cycle

            cfg = (_load_gateway_config().get("gateway") or {})
            raw = str(cfg.get("relay_url", "") or "").strip()
        except Exception:
            raw = ""
    if not raw:
        return None
    raw = raw.rstrip("/")
    # relay 拨号 URL 为 ws(s)://…/relay；注册向 http(s)://…/relay/enroll 发送 POST。
    if raw.startswith("ws://"):
        raw = "http://" + raw[len("ws://"):]
    elif raw.startswith("wss://"):
        raw = "https://" + raw[len("wss://"):]
    # 如果用户粘贴了拨号 URL，去掉尾部的 /relay 路径段。
    if raw.endswith("/relay"):
        raw = raw[: -len("/relay")]
    return raw


def _post_enroll(
    *,
    connector_base_url: str,
    access_token: str,
    enrollment_token: str,
    gateway_id: str,
    timeout: float = 15.0,
) -> dict:
    """向 connector 的 ``/relay/enroll`` 发送 POST 并返回 JSON 响应体。

    在任何非 2xx / 传输失败时抛出 RuntimeError 并附带面向用户的消息。
    connector 成功时返回 ``{secret, deliveryKey, tenant, gatewayId}``，
    400/401/403 时返回 ``{error}``。
    """
    url = f"{connector_base_url.rstrip('/')}/relay/enroll"
    data = json.dumps({"enrollmentToken": enrollment_token, "gatewayId": gateway_id}).encode("utf-8")
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
        detail = ""
        try:
            detail = (json.loads(exc.read().decode()) or {}).get("error", "")
        except Exception:
            pass
        if exc.code == 401:
            raise RuntimeError(
                "Connector rejected the caller identity (401). Your Nous Portal "
                "token could not be verified — try `hermes auth login nous` and retry."
            ) from exc
        if exc.code == 403:
            raise RuntimeError(
                detail
                or "Enrollment token invalid, expired, already used, or tenant mismatch (403)."
            ) from exc
        raise RuntimeError(
            f"Connector returned HTTP {exc.code}" + (f": {detail}" if detail else "")
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not reach the connector at {connector_base_url}: {exc.reason}"
        ) from exc

    if not isinstance(payload, dict) or not payload.get("secret"):
        raise RuntimeError("Connector returned an unexpected response (no secret).")
    return payload


def cmd_gateway_enroll(args) -> None:
    """将此 gateway 注册到 relay connector；将认证凭据持久化到 .env。"""
    from hermes_cli.auth import AuthError, resolve_nous_access_token
    from hermes_cli.config import is_managed, save_env_value

    # 托管安装的 GATEWAY_RELAY_* 由编排器注入（NAS 根据设计中的托管模式
    # 直接铸造密钥）。在此类容器内自行注册是错误的 — 而且 save_env_value
    # 无论如何也拒绝写入。
    if is_managed():
        print(
            "✗ `hermes gateway enroll` is not available in a managed/hosted install.\n"
            "  The relay gateway secret is provisioned by the hosting platform."
        )
        sys.exit(1)

    enrollment_token = (getattr(args, "token", None) or os.environ.get("GATEWAY_RELAY_ENROLL_TOKEN", "")).strip()
    if not enrollment_token:
        print(
            "✗ No enrollment token. Pass --token <token> (or set "
            "GATEWAY_RELAY_ENROLL_TOKEN).\n"
            "  The connector mints this single-use token when your tenant's route "
            "is provisioned; it is delivered with your gateway config."
        )
        sys.exit(1)

    connector_base_url = _resolve_connector_url(getattr(args, "connector_url", None))
    if not connector_base_url:
        print(
            "✗ No connector URL. Pass --connector-url <url> (or set GATEWAY_RELAY_URL "
            "/ gateway.relay_url in config.yaml)."
        )
        sys.exit(1)

    gateway_id = (getattr(args, "gateway_id", None) or _default_gateway_id()).strip()

    # 1. 解析一个新鲜的 Nous 访问 token（证明租户身份的凭据）。
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

    # 2-3. 在 connector 处兑换注册 token。
    try:
        result = _post_enroll(
            connector_base_url=connector_base_url,
            access_token=access_token,
            enrollment_token=enrollment_token,
            gateway_id=gateway_id,
        )
    except RuntimeError as exc:
        print(f"✗ Enrollment failed: {exc}")
        sys.exit(1)

    secret = str(result.get("secret") or "")
    delivery_key = str(result.get("deliveryKey") or "")
    tenant = str(result.get("tenant") or "")
    resolved_gateway_id = str(result.get("gatewayId") or gateway_id)

    # 4. 幂等地持久化凭据。secret + delivery key 是敏感信息；
    #    save_env_value 将它们写入 ~/.hermes/.env（0600 权限目录），且绝不记录日志。
    to_write = {
        "GATEWAY_RELAY_ID": resolved_gateway_id,
        "GATEWAY_RELAY_SECRET": secret,
        "GATEWAY_RELAY_DELIVERY_KEY": delivery_key,
    }
    # 如果显式提供了 connector URL（作为 ws(s):// 拨号目标），也持久化它，
    # 以便运行时可以在不重新指定的情况下进行拨号。
    explicit_url = (getattr(args, "connector_url", None) or "").strip()
    if explicit_url:
        to_write["GATEWAY_RELAY_URL"] = explicit_url.rstrip("/")

    # Phase 5 §5.2：持久化 wake URL，以便 self_provision_relay 将其转发给
    # connector（当缓冲的工作在 gateway 空闲时到达时，connector 通过它唤醒
    # 此 gateway）。可选 — 省略则 connector 无法唤醒它，但 gateway 仍会在
    # 下次重连时排空队列。
    explicit_wake_url = (getattr(args, "wake_url", None) or "").strip()
    if explicit_wake_url:
        to_write["GATEWAY_RELAY_WAKE_URL"] = explicit_wake_url.rstrip("/")

    for key, value in to_write.items():
        if not value:
            continue
        try:
            save_env_value(key, value)
        except Exception as exc:
            print(f"✗ Failed to write {key} to .env: {exc}")
            sys.exit(1)

    from hermes_cli.config import get_env_path

    print(f'✓ Enrolled gateway "{resolved_gateway_id}"' + (f" for tenant {tenant}" if tenant else ""))
    print()
    print(f"  Wrote to {get_env_path()}:")
    print(f"    GATEWAY_RELAY_ID={resolved_gateway_id}")
    print("    GATEWAY_RELAY_SECRET=<hidden>")
    print("    GATEWAY_RELAY_DELIVERY_KEY=<hidden>")
    if explicit_url:
        print(f"    GATEWAY_RELAY_URL={explicit_url.rstrip('/')}")
    if explicit_wake_url:
        print(f"    GATEWAY_RELAY_WAKE_URL={explicit_wake_url.rstrip('/')}")
    print()
    print(
        "  The gateway now authenticates its relay WS upgrade with the per-gateway\n"
        "  secret and verifies signed inbound deliveries with the tenant delivery\n"
        "  key. Restart the gateway to pick up the new env."
    )
