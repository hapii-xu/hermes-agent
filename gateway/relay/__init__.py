"""Hermes gateway 的 relay/connector 支持包。

EXPERIMENTAL。本包实现了 "Gateway Gateway" relay 设计的 gateway 侧：一个通用
的 ``RelayAdapter``、connector 在握手时交给它的可序列化 ``CapabilityDescriptor``，
以及拨号连接 connector 的生产级 ``WebSocketRelayTransport``。在至少两个真实的
Class-1 平台（Discord + Telegram）把 schema 打磨稳定之前，公开 API（模块名、
descriptor 字段集合、transport 协议）可能不经 deprecation 周期而变更。

正式的跨仓库接口见 ``docs/relay-connector-contract.md``。

激活由配置驱动，而不是单独的 feature flag：当配置了 connector relay URL
（``GATEWAY_RELAY_URL`` 环境变量或 config.yaml 中的 ``gateway.relay_url``）时，
relay 平台才会被注册。未设置此项的部署不受影响——与 ``gateway.proxy_url`` 的形态
完全一致。
"""

from __future__ import annotations

import os
from typing import Optional


def relay_url() -> Optional[str]:
    """connector relay endpoint 的 URL，若未配置 relay 则返回 None。

    先检查 ``GATEWAY_RELAY_URL``（对 Docker 较为方便），再检查 config.yaml 中的
    ``gateway.relay_url``。非空值会激活 relay 平台；缺省则表示这是一个普通的
    直连/单租户 gateway。
    """
    url = os.environ.get("GATEWAY_RELAY_URL", "").strip()
    if url:
        return url.rstrip("/")
    try:
        from gateway.run import _load_gateway_config  # 延迟导入以避免循环依赖

        cfg = _load_gateway_config()
        url = (cfg.get("gateway") or {}).get("relay_url", "").strip()
        if url:
            return url.rstrip("/")
    except Exception:  # noqa: BLE001 - 配置缺失/解析失败绝不能导致注册崩溃
        pass
    return None


def relay_platform_identity() -> tuple[str, str]:
    """本 gateway 经由 relay 对外代表的 platform + bot id（用于握手的 hello）。

    默认为 ``("relay", "")``；可通过 ``GATEWAY_RELAY_PLATFORM`` /
    ``GATEWAY_RELAY_BOT_ID`` 覆盖，这样一个 connector 就能代理多个平台。
    """
    platform = os.environ.get("GATEWAY_RELAY_PLATFORM", "relay").strip() or "relay"
    bot_id = os.environ.get("GATEWAY_RELAY_BOT_ID", "").strip()
    return platform, bot_id


def relay_connection_auth() -> tuple[Optional[str], Optional[str]]:
    """本 gateway 用于认证 WS upgrade 的 (gateway_id, upgrade_secret)。

    二者均来自 enrollment（``hermes gateway enroll`` 会把它们写入
    ``~/.hermes/.env``）：``GATEWAY_RELAY_ID`` 标识已登记的实例，
    ``GATEWAY_RELAY_SECRET`` 是该 gateway 专用的签名密钥。若任一缺失 ->
    ``(None, None)``，则 transport 以未认证方式拨号（开发/测试，或一个不强制
    鉴权的 connector）。先检查环境变量（Docker），再检查 config.yaml 中的
    ``gateway.relay_id`` / ``gateway.relay_secret``。
    """
    gateway_id = os.environ.get("GATEWAY_RELAY_ID", "").strip()
    secret = os.environ.get("GATEWAY_RELAY_SECRET", "").strip()
    if not (gateway_id and secret):
        try:
            from gateway.run import _load_gateway_config  # 延迟导入以避免循环依赖

            cfg = (_load_gateway_config().get("gateway") or {})
            gateway_id = gateway_id or str(cfg.get("relay_id", "") or "").strip()
            secret = secret or str(cfg.get("relay_secret", "") or "").strip()
        except Exception:  # noqa: BLE001 - 配置缺失/解析失败绝不能导致注册崩溃
            pass
    return (gateway_id or None, secret or None)


def relay_endpoint() -> Optional[str]:
    """gateway 自己的公开 inbound URL，在 provision 时上报给 connector。

    connector 会把已签名的 inbound POST 投递到该 URL，并将其存储到该租户的
    route 行上。该值由 gateway 上报（connector 会将其限定到已验证的租户范围内，
    因此一个不诚实的 gateway 只能误导它自己的 inbound）。该值的*来源*因部署方式
    而异，但代码路径是统一的：自托管运维者设置 ``GATEWAY_RELAY_ENDPOINT``
    （与其设置 ``HERMES_DASHBOARD_PUBLIC_URL`` 的方式一致）；hosted/NAS 容器则由
    NAS 注入相同的变量（只有在这种情况下 NAS 才知道公开 URL）。若缺省 ->
    gateway 以 outbound-only 方式 provision（不写入任何 inbound route）。

    先检查环境变量（Docker），再检查 config.yaml 中的 ``gateway.relay_endpoint``。
    """
    url = os.environ.get("GATEWAY_RELAY_ENDPOINT", "").strip()
    if not url:
        try:
            from gateway.run import _load_gateway_config  # 延迟导入以避免循环依赖

            cfg = (_load_gateway_config().get("gateway") or {})
            url = str(cfg.get("relay_endpoint", "") or "").strip()
        except Exception:  # noqa: BLE001 - 配置缺失/解析失败绝不能导致启动崩溃
            url = ""
    return url.rstrip("/") or None


def relay_route_keys() -> list[str]:
    """本 gateway 所属租户拥有的判别标识（guild_ids / chat_ids / paths）。

    由 gateway 提供的配置，与 ``relay_endpoint()`` 配对使用：connector 为每个
    (routeKey -> tenant, endpoint) 写入一行 route，因此 route keys 只有在同时存在
    endpoint 时才会生效。为空 -> outbound-only 的 provision（connector 接受空集合
    并不写入任何 route 行）。

    ``GATEWAY_RELAY_ROUTE_KEYS`` 以逗号分隔；config.yaml 中的
    ``gateway.relay_route_keys`` 可以是列表或逗号分隔字符串。
    """
    raw = os.environ.get("GATEWAY_RELAY_ROUTE_KEYS", "").strip()
    if not raw:
        try:
            from gateway.run import _load_gateway_config  # 延迟导入以避免循环依赖

            cfg = (_load_gateway_config().get("gateway") or {})
            val = cfg.get("relay_route_keys", "")
            if isinstance(val, (list, tuple)):
                return [str(k).strip() for k in val if str(k).strip()]
            raw = str(val or "").strip()
        except Exception:  # noqa: BLE001
            raw = ""
    return [k.strip() for k in raw.split(",") if k.strip()]


def relay_instance_id() -> Optional[str]:
    """本 gateway 在 provision 时上报的稳定 per-instance id（Phase 6 Unit α）。

    绑定 connector 侧的 ``gatewayId -> instanceId``，以便 Phase 6 的投递落地后
    connector 能够按实例（而非租户广播）路由 inbound。对于托管 agent，该值是 NAS
    的 ``AgentInstance.id``（NAS 把 ``GATEWAY_RELAY_INSTANCE_ID`` 注入到容器环境
    变量中，紧挨着 ``GATEWAY_RELAY_URL``）；自托管运维者也可以显式设置。该值由
    gateway 上报，但被安全地限定范围：org/tenant 仍然经过 token 验证，因此一个
    不诚实的 gateway 只能绑定它自己租户的实例——与 ``relay_endpoint()`` 的姿态
    一致。若缺省 -> connector 存储 null，per-instance 路由暂时对本次连接没有绑定
    （向后兼容）。

    先检查环境变量（Docker/NAS），再检查 config.yaml 中的
    ``gateway.relay_instance_id``。
    """
    value = os.environ.get("GATEWAY_RELAY_INSTANCE_ID", "").strip()
    if not value:
        try:
            from gateway.run import _load_gateway_config  # 延迟导入以避免循环依赖

            cfg = (_load_gateway_config().get("gateway") or {})
            value = str(cfg.get("relay_instance_id", "") or "").strip()
        except Exception:  # noqa: BLE001 - 配置缺失/解析失败绝不能导致启动崩溃
            value = ""
    return value or None


def relay_wake_url() -> Optional[str]:
    """gateway 的 WAKE URL，在 provision 时上报（Phase 5 §5.2 wake PRIMITIVE）。

    一个唤醒目标：当本实例的一个 buffered-only（going-idle）目的地收到其第一条
    buffered 事件时，connector 会向该 URL 发起一个无负载的 GET，从而让一个已挂起
    的 gateway 唤醒、重连它的 relay WS，并排空其 delivery-leg 的积压消息。该值的
    *来源*因部署方式而异，但代码路径是统一的：托管/NAS 容器会被注入
    ``GATEWAY_RELAY_WAKE_URL``（NAS 知道 Fly autostart / dashboard 主机名）；自托管
    运维者可显式设置（或在 ``hermes gateway enroll`` 时传入 ``--wake-url``）。

    由 gateway 上报，但被安全地限定范围：org/tenant 仍然经过 token 验证，因此一个
    不诚实的 gateway 只能为其自己的实例注册唤醒目标——与
    ``relay_instance_id()`` / 已废弃的 ``relay_endpoint()`` 的姿态一致。若缺省 ->
    connector 存储 null，并简单地无法唤醒本实例（缓冲仍然有效；gateway 在下次
    重连时再排空积压）。

    先检查环境变量（Docker/NAS），再检查 config.yaml 中的 ``gateway.relay_wake_url``。
    """
    value = os.environ.get("GATEWAY_RELAY_WAKE_URL", "").strip()
    if not value:
        try:
            from gateway.run import _load_gateway_config  # 延迟导入以避免循环依赖

            cfg = (_load_gateway_config().get("gateway") or {})
            value = str(cfg.get("relay_wake_url", "") or "").strip()
        except Exception:  # noqa: BLE001 - 配置缺失/解析失败绝不能导致启动崩溃
            value = ""
    return value.rstrip("/") or None


def _provision_url(relay_dial_url: str) -> str:
    """把 ``ws(s)://…/relay`` 拨号 URL 映射为 ``http(s)://…/relay/provision`` POST URL。"""
    raw = relay_dial_url.rstrip("/")
    if raw.startswith("ws://"):
        raw = "http://" + raw[len("ws://"):]
    elif raw.startswith("wss://"):
        raw = "https://" + raw[len("wss://"):]
    if raw.endswith("/relay"):
        raw = raw[: -len("/relay")]
    return f"{raw}/relay/provision"


def _policy_url(relay_dial_url: str) -> str:
    """把 ``ws(s)://…/relay`` 拨号 URL 映射为 ``http(s)://…/relay/policy`` POST URL。

    主机部分的派生方式与 ``_provision_url`` 相同；connector 在 ``/relay/policy``
    挂载 relevance-policy 更新通道（Phase 6 Unit ζ）。
    """
    raw = relay_dial_url.rstrip("/")
    if raw.startswith("ws://"):
        raw = "http://" + raw[len("ws://"):]
    elif raw.startswith("wss://"):
        raw = "https://" + raw[len("wss://"):]
    if raw.endswith("/relay"):
        raw = raw[: -len("/relay")]
    return f"{raw}/relay/policy"


def relay_relevance_policy() -> Optional[dict]:
    """把本 gateway 的 RELEVANCE 配置投影到 connector 的通用词汇中。

    connector 的 relevance gate（Phase 6 Unit ζ）基于一套平台无关的策略来推理——
    ``requireAddress`` / ``freeResponseScopes`` / ``allowOtherBots``——而不是基于
    Discord/Telegram 的措辞。本函数是该契约的 gateway 侧：它读取 agent 现有的
    relevance 配置项，并输出 connector 按 instance 存储的通用结构。

    映射关系（connector 词汇 ← gateway 现有配置）：
      - ``requireAddress``     ← 平台的 ``require_mention``（agent 仅对 @提及它 /
        回复它的非 owner 消息作出响应）。
      - ``freeResponseScopes`` ← 平台的 ``free_response_channels``（免除
        ``require_mention`` 的 channel/scope id 集合——与 connector 的 δ scope
        授权 + ε 下限使用的是同一套 scope 词汇）。
      - ``allowOtherBots``     ← ``{PLATFORM}_ALLOW_BOTS`` 取值 {"mentions","all"}
        （是否放行 bot 发出的消息；默认关闭）。

    从 relay 平台的配置块（connector 所代理的平台，例如 ``discord:``）读取，回退到
    桥接的顶层 key，再到 ``{PLATFORM}_*`` 环境变量。返回通用 dict；当未配置 relay
    或该平台不暴露任何 relevance 配置项时返回 None（⇒ connector 的静默默认值已经
    匹配，因此没有需要声明的内容）。
    """
    platform, _bot_id = relay_platform_identity()
    if not platform or platform == "relay":
        # 没有解析出具体的被代理平台 ⇒ 没有平台特定的内容需要投影。
        return None

    # 解析平台的配置块 + 桥接的顶层 key。
    require_mention = None
    free_response: list[str] = []
    try:
        from gateway.run import _load_gateway_config  # 延迟导入以避免循环依赖

        cfg = _load_gateway_config() or {}
        plat_cfg = cfg.get(platform)
        if not isinstance(plat_cfg, dict):
            plat_cfg = ((cfg.get("gateway") or {}).get("platforms") or {}).get(platform)
        if not isinstance(plat_cfg, dict):
            plat_cfg = (cfg.get("platforms") or {}).get(platform)
        plat_cfg = plat_cfg if isinstance(plat_cfg, dict) else {}

        if "require_mention" in plat_cfg:
            require_mention = plat_cfg.get("require_mention")
        elif cfg.get("require_mention") is not None:
            require_mention = cfg.get("require_mention")

        frc = plat_cfg.get("free_response_channels")
        if frc is None:
            frc = cfg.get("free_response_channels")
        if isinstance(frc, (list, tuple)):
            free_response = [str(c).strip() for c in frc if str(c).strip()]
        elif isinstance(frc, str) and frc.strip():
            free_response = [c.strip() for c in frc.split(",") if c.strip()]
    except Exception:  # noqa: BLE001 - 配置缺失/解析失败绝不能导致启动崩溃
        pass

    # allow_other_bots ← {PLATFORM}_ALLOW_BOTS 取值 {"mentions","all"}（与 gateway
    # 自身 authz_mixin 的 DISCORD_ALLOW_BOTS 旁路是同一个门控）。
    allow_bots_env = os.environ.get(f"{platform.upper()}_ALLOW_BOTS", "").lower().strip()
    allow_other_bots = allow_bots_env in {"mentions", "all"}

    require_address = bool(require_mention) if require_mention is not None else False

    # 没有任何非默认值需要声明 ⇒ 让 connector 保留其静默默认值
    #（与 connector 侧“无对应行”的语义一致）。
    if not require_address and not free_response and not allow_other_bots:
        return None

    return {
        "platform": platform,
        "requireAddress": require_address,
        "freeResponseScopes": free_response,
        "allowOtherBots": allow_other_bots,
    }


def _post_provision(
    *,
    provision_url: str,
    access_token: str,
    gateway_id: str,
    platform: str,
    bot_id: str,
    gateway_endpoint: Optional[str],
    route_keys: list[str],
    instance_id: Optional[str] = None,
    wake_url: Optional[str] = None,
    timeout: float = 15.0,
) -> dict:
    """POST 到 connector 的 ``/relay/provision`` 并返回 JSON body。

    connector 会向 NAS 校验 ``access_token``，推导出权威的 tenant，签发 per-gateway
    secret + per-tenant delivery key，upsert 该租户的 route 行，并返回
    ``{secret, deliveryKey, tenant, gatewayId, routeKeys}``。任何非 2xx / transport
    失败都会抛出带有面向用户消息的 RuntimeError。
    """
    import json
    import urllib.error
    import urllib.request

    body: dict = {
        "gatewayId": gateway_id,
        "platform": platform,
        "botId": bot_id,
        "gatewayEndpoint": gateway_endpoint or "",
        "routeKeys": route_keys,
    }
    # 只有当我们确有 instanceId 时才发送——省略它可让 connector 存储 null
    #（向后兼容），而不是绑定一个空字符串。
    if instance_id:
        body["instanceId"] = instance_id
    # wake URL 同理（Phase 5 §5.2）：缺省时省略，以便 connector 存储 null 并简单地
    # 无法唤醒本实例（缓冲仍然有效）。
    if wake_url:
        body["wakeUrl"] = wake_url
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        provision_url,
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
        raise RuntimeError(
            f"connector returned HTTP {exc.code}" + (f": {detail}" if detail else "")
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not reach connector: {exc.reason}") from exc

    if not isinstance(payload, dict) or not payload.get("secret"):
        raise RuntimeError("connector returned an unexpected response (no secret)")
    return payload


def self_provision_relay() -> bool:
    """启动时的 relay 自助 provision：在进程内签发 relay 凭证，无需人工、不落盘。

    在以下条件同时满足时触发：已配置 relay（``relay_url()`` 已设置）且尚不存在
    per-gateway secret，并且 agent 能解析出自己的 Nous access token。此时 runtime
    会解析 agent 自己的 Nous access token（与 enroll CLI / dashboard 注册所用的
    ``resolve_nous_access_token()`` 相同），POST 到 ``/relay/provision`` 上报自己的
    endpoint + route keys，并将 ``GATEWAY_RELAY_ID`` / ``GATEWAY_RELAY_SECRET`` /
    ``GATEWAY_RELAY_DELIVERY_KEY`` 写入 ``os.environ``，以便随后的
    ``register_relay_adapter()`` 能读取到它们。这些凭证仅存在于进程内存中——绝不
    写入 ``~/.hermes/.env``。

    触发条件刻意不是 ``is_managed()``：它表示“由包管理器/NixOS 管理”，在 NAS 托管
    的 Fly agent 上为 False（这类 agent 既不设置 ``HERMES_MANAGED`` 也不设置
    ``.managed`` 标记），因此以它为门控会挡住本机制正是为之服务的那个托管场景。
    真正的信号是“你把我指向了一个 connector 且没有固定 secret”——这既与 NAS 无关，
    又能自我保护：

      - NAS 托管的 agent：具备 ``GATEWAY_RELAY_URL``、无固定 secret、且有一个已
        引导的 NAS token -> 自助 provision。
      - 运行过 ``hermes gateway enroll`` 的自托管运维者：具备已固定的
        ``GATEWAY_RELAY_SECRET`` -> 跳过（即下方的 secret 存在守卫）。
      - 配置了 relay URL 但没有 NAS 身份的自托管机器：
        ``resolve_nous_access_token()`` 失败 -> 优雅地无操作。

    无状态：进程环境变量中的凭证在重启后不复存在，因此托管容器每次启动都会重新
    provision；connector 的轮转窗口会覆盖仍连接着的前一个实例。显式固定的
    ``GATEWAY_RELAY_SECRET``（环境变量或 config）会被尊重——自助 provision 会跳过，
    以免覆盖运维者固定的值。

    若完成 provision 则返回 True，否则返回 False。绝不抛异常：provision 失败只会
    记录日志并返回 False，使 gateway 仍能启动（``register_relay_adapter`` 会简单
    地以未认证方式拨号 / 被拒绝，而不是整个 gateway 崩溃）。
    """
    import logging

    logger = logging.getLogger("gateway.relay")

    dial_url = relay_url()
    if not dial_url:
        return False

    # 尊重已存在（固定/已注入）的 secret——不要覆盖它。这也是让一个自托管、已登记
    # 的 gateway 跳过自助 provision 的原因。
    existing_id, existing_secret = relay_connection_auth()
    if existing_id and existing_secret:
        logger.info("relay self-provision skipped: GATEWAY_RELAY_SECRET already set")
        return False

    try:
        from hermes_cli.auth import resolve_nous_access_token

        access_token = resolve_nous_access_token()
    except Exception as exc:  # noqa: BLE001 - 启动必须能扛住 token 失败
        # 无法解析出 NAS 身份（例如一台尚未登记的自托管机器）-> 没有可用于 provision
        # 的凭证；安静地跳过，让 gateway 继续启动。
        logger.warning("relay self-provision skipped: could not resolve Nous token (%s)", exc)
        return False

    platform, bot_id = relay_platform_identity()
    # gatewayId 的默认值与 enroll CLI 基于主机名的 slug 保持一致。
    import socket

    try:
        host = socket.gethostname().strip()
    except Exception:  # noqa: BLE001
        host = ""
    gateway_id = os.environ.get("GATEWAY_RELAY_ID", "").strip() or f"gw-{host or 'hermes'}"
    endpoint = relay_endpoint()
    route_keys = relay_route_keys()
    instance_id = relay_instance_id()
    wake_url = relay_wake_url()

    try:
        result = _post_provision(
            provision_url=_provision_url(dial_url),
            access_token=access_token,
            gateway_id=gateway_id,
            platform=platform,
            bot_id=bot_id,
            gateway_endpoint=endpoint,
            route_keys=route_keys,
            instance_id=instance_id,
            wake_url=wake_url,
        )
    except RuntimeError as exc:
        logger.warning("relay self-provision failed (%s); gateway will boot without relay auth", exc)
        return False

    # 把凭证设置进进程内，以便 register_relay_adapter() 从 os.environ 中读取它们
    #（per-gateway secret 用于认证 outbound WS upgrade）。delivery key 仍由 connector
    # 签发并出于前向兼容而持久化，但 inbound 现在经由 WS（没有 HTTP receiver），
    # 因此这里并不消费它。绝不记录日志。
    os.environ["GATEWAY_RELAY_ID"] = str(result.get("gatewayId") or gateway_id)
    os.environ["GATEWAY_RELAY_SECRET"] = str(result.get("secret") or "")
    os.environ["GATEWAY_RELAY_DELIVERY_KEY"] = str(result.get("deliveryKey") or "")
    tenant = str(result.get("tenant") or "")
    logger.info(
        "relay self-provisioned (gateway_id=%s tenant=%s routes=%d inbound=%s instance=%s wake=%s)",
        os.environ["GATEWAY_RELAY_ID"],
        tenant or "?",
        len(route_keys),
        "yes" if endpoint else "outbound-only",
        instance_id or "unbound",
        "yes" if wake_url else "none",
    )
    return True


def _post_policy(*, policy_url: str, token: str, policy: dict, timeout: float = 15.0) -> int:
    """把 relevance policy POST 到 connector 的 ``/relay/policy``；返回 HTTP 状态码。

    使用 gateway 自己的 per-gateway upgrade token 认证（与 WS upgrade 的 bearer
    形态相同——``make_upgrade_token``），因此 connector 从其存储的 secret 记录中解析
    ``{tenant, instanceId}``，而不是从 body 中解析。transport 失败时抛出
    RuntimeError（调用方把任何失败都视为非致命——relevance 是一项优化，而非启动
    依赖）。
    """
    import json
    import urllib.error
    import urllib.request

    data = json.dumps(policy).encode("utf-8")
    req = urllib.request.Request(
        policy_url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not reach connector: {exc.reason}") from exc


def send_relay_policy() -> bool:
    """向 connector 声明本 gateway 的 relevance policy（Phase 6 Unit ζ）。

    在启动时、per-gateway secret 解析完毕（自助 provision 或已固定）之后运行，把
    agent 的 relevance 配置投影到通用词汇（``relay_relevance_policy``），并用
    gateway 自己的 upgrade token 把它 POST 到 ``/relay/policy``。connector 按
    instance 存储它，relevance gate 在投递时据此执行——因此 agent 直接施加的那同一
    套 mention-gating / free-response / allow-bots 行为也约束 relay 投递，被排除的
    流量永远不会唤醒一个已 scale-to-zero 的 agent。

    自愈：agent 是事实来源，并在每次启动时重新声明（镜像 provision 时对
    ``routeKeys`` 的 upsert）。幂等——一次全量替换。

    绝不抛异常，也绝不阻塞启动：relevance 是叠加在 δ/ε 授权门控（已经保护了隔离性）
    之上的一项优化，因此声明失败仅意味着 connector 保留先前/静默的策略。当且仅当
    connector 接受该策略（HTTP 200）时返回 True。
    """
    import logging

    logger = logging.getLogger("gateway.relay")

    dial_url = relay_url()
    if not dial_url:
        return False

    gateway_id, secret = relay_connection_auth()
    if not gateway_id or not secret:
        # 没有解析出 per-gateway secret（未登记 / provision 失败）⇒ 无法为该 policy
        # POST 鉴权；安静地跳过（WS upgrade 同样会是未认证的，因此也没有可附着
        # policy 的实例）。
        return False

    policy = relay_relevance_policy()
    if policy is None:
        # 没有任何非默认值需要声明 ⇒ connector 的静默默认值已经匹配；不要写一条
        # 冗余的行。
        logger.info("relay policy: no non-default relevance config to declare; using connector default")
        return False

    try:
        from gateway.relay.auth import make_upgrade_token

        token = make_upgrade_token(gateway_id, secret)
        status = _post_policy(policy_url=_policy_url(dial_url), token=token, policy=policy)
    except Exception as exc:  # noqa: BLE001 - 启动必须能扛住 policy 声明失败
        logger.warning("relay policy declaration failed (%s); connector keeps prior/default policy", exc)
        return False

    if status == 200:
        logger.info(
            "relay policy declared (platform=%s require_address=%s free_scopes=%d allow_bots=%s)",
            policy.get("platform"),
            policy.get("requireAddress"),
            len(policy.get("freeResponseScopes") or []),
            policy.get("allowOtherBots"),
        )
        return True
    logger.warning("relay policy declaration returned HTTP %s; connector keeps prior/default policy", status)
    return False


def register_relay_adapter(force: bool = False, url: Optional[str] = None) -> bool:
    """通过 platform registry 注册通用的 ``relay`` 平台。

    当配置了 relay URL 时（或测试用 ``force=True``，此时构建一个无 transport 的
    adapter——即单元测试的姿态）进行注册。若完成了注册则返回 True。增量式：使用与
    插件 adapter 相同的 registry 路径，因此无需修改核心派发逻辑。

    当存在 URL 时，factory 会构建一个活跃的 ``WebSocketRelayTransport``；
    ``RelayAdapter`` 在 ``connect()`` 时通过 ``transport.handshake()`` 协商出真正的
    ``CapabilityDescriptor``。
    """
    resolved_url = url if url is not None else relay_url()
    if not (force or resolved_url):
        return False

    from gateway.platform_registry import PlatformEntry, platform_registry
    from gateway.relay.adapter import RelayAdapter
    from gateway.relay.descriptor import CONTRACT_VERSION, CapabilityDescriptor

    platform, bot_id = relay_platform_identity()

    def _factory(config):
        # 占位 descriptor；当存在 transport 时，会在 connect 时被协商出的真实
        # descriptor 替换。若无 URL（force/测试），则 adapter 没有 transport 并保留
        # 该占位符。
        placeholder = CapabilityDescriptor(
            contract_version=CONTRACT_VERSION,
            platform=platform,
            label="Relay",
            max_message_length=4096,
            supports_draft_streaming=False,
            supports_edit=True,
            supports_threads=False,
            markdown_dialect="plain",
            len_unit="chars",
        )
        transport = None
        if resolved_url:
            from gateway.relay.ws_transport import WebSocketRelayTransport

            gateway_id, upgrade_secret = relay_connection_auth()
            transport = WebSocketRelayTransport(
                resolved_url,
                platform,
                bot_id,
                gateway_id=gateway_id,
                upgrade_secret=upgrade_secret,
                # Phase 5 §5.3：在意外 socket 关闭后重新拨号 + 重新握手，以便一个
                # 进入 idle/suspended 的 gateway 重新建立其 relay socket——这会在
                # 新的握手时触发 connector 的 buffered-flip 排空（即 delivery-leg 的
                # onResume）。
                reconnect=True,
            )
        return RelayAdapter(config, placeholder, transport=transport)

    platform_registry.register(
        PlatformEntry(
            name="relay",
            label="Relay",
            adapter_factory=_factory,
            check_fn=lambda: True,
            source="builtin",
            emoji="\U0001f50c",
        )
    )
    return True
