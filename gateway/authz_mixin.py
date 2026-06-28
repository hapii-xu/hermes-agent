"""``GatewayRunner`` 的用户授权方法。

从 ``gateway/run.py`` 中提取，属于 god-file 分解工作的一部分
（``~/.hermes/plans/god-file-decomposition.md``，阶段 3 机械化 mixin 提取）。
此 mixin 包含入站消息授权逻辑集群：判断某个用户/聊天
是否允许与 agent 通信、每个 adapter 的私信（DM）策略，以及
未授权 DM 的处理行为。

行为中性：每个方法均原样从 ``GatewayRunner`` 中迁移。
``self.*`` 调用通过 MRO 保持不变地解析。中性依赖在
模块顶部导入；模块级 ``logger`` 在使用它的方法内部惰性导入
（``from gateway.run import logger`` 在调用时才解析，此时
``gateway.run`` 已完全加载），因此此模块在导入时不会导入 ``gateway.run``
-> 避免循环导入。惰性导入保留了原始 logger 名称
（``"gateway.run"``），因此日志记录保持不变。
"""

from __future__ import annotations

import os
from typing import Optional

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.whatsapp_identity import (
    expand_whatsapp_aliases as _expand_whatsapp_auth_aliases,
    normalize_whatsapp_identifier as _normalize_whatsapp_identifier,
)


class GatewayAuthorizationMixin:
    """``GatewayRunner`` 的用户/聊天授权方法。"""

    def _adapter_authorization_is_upstream(self, platform: Optional[Platform]) -> bool:
        """*platform* 对应的 adapter 是否将授权委托给可信的上游。

        对应 ``BasePlatformAdapter.authorization_is_upstream``。relay
        adapter 将此设为 True：Team Gateway connector 对 gateway 的 WS 进行认证，
        并在投递之前解析仅限 owner 的授权绑定，因此入站 relay 事件
        已经作为此实例绑定的用户完成授权。与
        ``_adapter_enforces_own_access_policy``（一个本地配置策略，gateway
        仅在其为 allowlist 时遵循）不同，这是一个上游做出的决策，
        gateway 直接遵守。当 adapter 未知或未暴露此标志时，
        默认返回 ``False``。
        """
        if not platform:
            return False
        adapters = getattr(self, "adapters", None)
        if not adapters:
            return False
        adapter = adapters.get(platform)
        if adapter is None:
            return False
        return bool(getattr(adapter, "authorization_is_upstream", False))

    def _adapter_enforces_own_access_policy(self, platform: Optional[Platform]) -> bool:
        """*platform* 对应的 adapter 是否在入口处自行进行访问控制。

        对应 ``BasePlatformAdapter.enforces_own_access_policy``。
        WeCom、微信、元宝、QQBot 和 WhatsApp 等 adapter
        在消息分发到 gateway 之前，会评估其文档中描述的
        ``dm_policy`` / ``group_policy`` / ``allow_from`` 配置。仅此标志
        并不意味着"已授权"：这些 adapter 默认为 ``open``，即转发所有
        发送者，因此 ``_is_user_authorized`` 仅在该 adapter 对当前聊天类型
        的有效策略为实际的 ``allowlist`` 限制时才信任该 adapter
        （参见该方法）。当 adapter 未知或未暴露此标志时，
        默认返回 ``False``。
        """
        if not platform:
            return False
        # 一些测试辅助工具会通过 object.__new__ 构造一个裸 GatewayRunner，
        # 且从不设置 ``adapters``；把缺失/空的 map 当作“没有 adapter”处理，
        # 而不是抛异常（参见 pitfalls.md #17）。
        adapters = getattr(self, "adapters", None)
        if not adapters:
            return False
        adapter = adapters.get(platform)
        if adapter is None:
            return False
        return bool(getattr(adapter, "enforces_own_access_policy", False))

    def _adapter_dm_policy(self, platform: Optional[Platform]) -> str:
        """best-effort 地读取某个 own-policy adapter 实际生效的 DM 策略。

        返回 *platform* 对应的小写 ``dm_policy``（``"open"`` / ``"allowlist"`` /
        ``"disabled"`` / ``"pairing"``），未知时返回 ``""``。优先使用活动
        adapter 已解析好的 ``_dm_policy`` —— 它已经同时融合了
        ``config.extra`` 和 ``<PLATFORM>_DM_POLICY`` 环境变量（该环境变量并不
        总是被桥接回 ``config.extra``）—— 并在没有活动 adapter 的裸 runner
        上回退到 ``config.extra``。

        由 ``_is_user_authorized`` 用于判断某个 own-policy adapter 是否真的
        把 DM 发送者限制到了一个已配置的 allowlist（可信），还是仅仅在
        ``dm_policy: open`` 下转发了所有人 / 或是用于一次配对握手（不构成
        授权）。“到达了 gateway”这一事实只有在 ``allowlist`` 情况下才携带
        授权信号。
        """
        if not platform:
            return ""
        adapters = getattr(self, "adapters", None) or {}
        adapter = adapters.get(platform)
        policy = getattr(adapter, "_dm_policy", None) if adapter is not None else None
        if policy is None:
            config = getattr(self, "config", None)
            platform_cfg = (
                config.platforms.get(platform)
                if config is not None and hasattr(config, "platforms")
                else None
            )
            extra = getattr(platform_cfg, "extra", None) if platform_cfg else None
            if isinstance(extra, dict):
                policy = extra.get("dm_policy")
        return str(policy or "").strip().lower()

    def _adapter_group_policy(self, platform: Optional[Platform]) -> str:
        """best-effort 地读取某个 own-policy adapter 实际生效的群组策略。

        对应 ``_adapter_dm_policy``，但面向 group / forum / channel 流量：
        返回 *platform* 对应的小写 ``group_policy``（``"open"`` /
        ``"allowlist"`` / ``"disabled"``），未知时返回 ``""``。优先使用活动
        adapter 已解析好的 ``_group_policy``，并在没有活动 adapter 的裸
        runner 上回退到 ``config.extra``。

        由 ``_is_user_authorized`` 用于判断某个 own-policy adapter 是否把群组
        发送者限制到了一个已配置的 allowlist（可信），还是在
        ``group_policy: open`` 下转发了整个频道（不构成授权）。
        """
        if not platform:
            return ""
        adapters = getattr(self, "adapters", None) or {}
        adapter = adapters.get(platform)
        policy = getattr(adapter, "_group_policy", None) if adapter is not None else None
        if policy is None:
            config = getattr(self, "config", None)
            platform_cfg = (
                config.platforms.get(platform)
                if config is not None and hasattr(config, "platforms")
                else None
            )
            extra = getattr(platform_cfg, "extra", None) if platform_cfg else None
            if isinstance(extra, dict):
                policy = extra.get("group_policy")
        return str(policy or "").strip().lower()

    def _adapter_group_has_sender_allowlist(
        self,
        platform: Optional[Platform],
        chat_id: Optional[str],
    ) -> bool:
        """某条群组消息是否被一个按群组的发送者 allowlist 所门控。

        WeCom 在顶层的 ``group_policy`` 之外，还支持
        ``groups.<group_id>.allow_from``。一个群组在聊天层面可能是 open 的，
        但仍然会限制该群组内哪些发送者可以调用 Hermes。如果这样一条消息
        到达了 gateway，说明 adapter 已经检查过那个发送者 allowlist，因此
        这是一个可信的 intake 决策，而不是 fail-open 的
        ``group_policy: open`` 情况。
        """
        if not platform or not chat_id:
            return False
        adapters = getattr(self, "adapters", None) or {}
        adapter = adapters.get(platform)
        groups = getattr(adapter, "_groups", None) if adapter is not None else None
        if groups is None:
            config = getattr(self, "config", None)
            platform_cfg = (
                config.platforms.get(platform)
                if config is not None and hasattr(config, "platforms")
                else None
            )
            extra = getattr(platform_cfg, "extra", None) if platform_cfg else None
            if isinstance(extra, dict):
                groups = extra.get("groups")
        if not isinstance(groups, dict):
            return False

        chat_id_str = str(chat_id)
        group_cfg = groups.get(chat_id_str)
        if not isinstance(group_cfg, dict):
            lowered = chat_id_str.lower()
            for key, value in groups.items():
                if isinstance(key, str) and key.lower() == lowered and isinstance(value, dict):
                    group_cfg = value
                    break
        if not isinstance(group_cfg, dict):
            group_cfg = groups.get("*")
        if not isinstance(group_cfg, dict):
            return False

        sender_allow = group_cfg.get("allow_from") or group_cfg.get("allowFrom")
        if isinstance(sender_allow, str):
            return bool(sender_allow.strip())
        if isinstance(sender_allow, (list, tuple, set)):
            return any(str(item).strip() for item in sender_allow)
        return False

    def _is_user_authorized(self, source: SessionSource) -> bool:
        """
        检查某用户是否被授权使用本 bot。

        按以下顺序检查：
        1. 每个平台的 allow-all flag（例如 DISCORD_ALLOW_ALL_USERS=true）
        2. 环境变量 allowlist（TELEGRAM_ALLOWED_USERS 等）
        3. DM 配对已批准列表
        4. 全局 allow-all（GATEWAY_ALLOW_ALL_USERS=true）
        5. 默认：拒绝
        """
        from gateway.run import logger
        # Home Assistant 事件是系统生成的（状态变化），不是用户主动发起的消息。
        # HASS_TOKEN 已经认证了连接，因此 HA 事件总是已授权的。
        # Webhook 事件通过 adapter 自身的 HMAC 签名校验完成认证 —— 不适用
        # 任何用户 allowlist。
        if source.platform in {Platform.HOMEASSISTANT, Platform.WEBHOOK}:
            return True

        # Relay（以及任何其授权由可信的、已认证的上游来执行的 adapter）：
        # Team Gateway connector 用一个 per-instance 的 secret 认证本 gateway
        # 的 WS，并在投递 *之前* 解析仅限 owner 的授权绑定，因此一条入站的
        # relay 事件已经作为本实例绑定的用户完成授权（author id 是 connector
        # 观察到的那个，绝不由 gateway 断言）。不存在本地的
        # RELAY_ALLOWED_USERS 环境 allowlist 可供查询，而因其缺失就默认拒绝，
        # 正是本分支所修复的 bug。这是向可信上游的委托，而不是 fail-open：
        # 它只对确实经由已认证 relay WS 投递的事件（transport 会打上
        # ``delivered_via_upstream_relay`` 标记），或其平台 adapter 显式声明
        # ``authorization_is_upstream=True`` 的事件触发；每个直接暴露在网络上
        # 的 adapter 都让该 flag 保持 False，其事件也不打标记，因此下面的
        # env-allowlist 默认拒绝依然原样适用。
        #
        # 投递标记是 *首要* 信号：一条 relay *消息* 入站时携带的是 *底层*
        # 平台（``source.platform`` == discord/…），而不是 ``Platform.RELAY``，
        # 因为那是会话键和出口所需的 —— 因此如果以 ``source.platform`` 来
        # 决定授权就会漏掉（relay adapter 注册在 ``Platform.RELAY`` 下）并对
        # 用户默认拒绝（"Unauthorized user <id> on discord"）。adapter-flag
        # 检查之所以保留，是为了那些 ``source.platform`` 确实是
        # ``Platform.RELAY`` 的事件（例如交互透传路径）。
        # ``is True``（而非单纯的真值判断）：该标记在 SessionSource 上是一个
        # 真正的 bool，显式的身份比较会拒绝授权一个非 bool 的替身（例如一个
        # MagicMock 属性在测试中会自动具现化为真值）—— 防御意外的 fail-open。
        if source.delivered_via_upstream_relay is True or self._adapter_authorization_is_upstream(
            source.platform
        ):
            return True

        user_id = source.user_id

        # Telegram（以及类似平台）通过 TELEGRAM_GROUP_ALLOWED_CHATS /
        # QQ_GROUP_ALLOWED_USERS 按 chat ID 授权整个 group/forum/channel 聊天。
        # 该 allowlist 是聊天作用域的，因此即使在 source.user_id 为 None 时
        # 也必须生效——Telegram 会发出匿名管理员发言、sender_chat 流量，以及
        # 没有 `from_user` 的频道广播，而显式列出该聊天的运营者期望这些都能
        # 被兑现。在下方 no-user-id 守卫之前运行此检查，从而文档化行为与实际
        # 一致（website/docs/reference/environment-variables.md、
        # website/docs/user-guide/messaging/telegram.md）。
        if source.chat_type in {"group", "forum", "channel"} and source.chat_id:
            chat_allowlist_env = {
                Platform.TELEGRAM: "TELEGRAM_GROUP_ALLOWED_CHATS",
                Platform.QQBOT: "QQ_GROUP_ALLOWED_USERS",
            }.get(source.platform, "")
            if chat_allowlist_env:
                raw_chat_allowlist = os.getenv(chat_allowlist_env, "").strip()
                if raw_chat_allowlist:
                    allowed_group_ids = {
                        cid.strip()
                        for cid in raw_chat_allowlist.split(",")
                        if cid.strip()
                    }
                    if "*" in allowed_group_ids or source.chat_id in allowed_group_ids:
                        return True

        if not user_id:
            return False

        platform_env_map = {
            Platform.TELEGRAM: "TELEGRAM_ALLOWED_USERS",
            Platform.DISCORD: "DISCORD_ALLOWED_USERS",
            Platform.WHATSAPP: "WHATSAPP_ALLOWED_USERS",
            Platform.WHATSAPP_CLOUD: "WHATSAPP_CLOUD_ALLOWED_USERS",
            Platform.SLACK: "SLACK_ALLOWED_USERS",
            Platform.SIGNAL: "SIGNAL_ALLOWED_USERS",
            Platform.EMAIL: "EMAIL_ALLOWED_USERS",
            Platform.SMS: "SMS_ALLOWED_USERS",
            Platform.MATTERMOST: "MATTERMOST_ALLOWED_USERS",
            Platform.MATRIX: "MATRIX_ALLOWED_USERS",
            Platform.DINGTALK: "DINGTALK_ALLOWED_USERS",
            Platform.FEISHU: "FEISHU_ALLOWED_USERS",
            Platform.WECOM: "WECOM_ALLOWED_USERS",
            Platform.WECOM_CALLBACK: "WECOM_CALLBACK_ALLOWED_USERS",
            Platform.WEIXIN: "WEIXIN_ALLOWED_USERS",
            Platform.BLUEBUBBLES: "BLUEBUBBLES_ALLOWED_USERS",
            Platform.QQBOT: "QQ_ALLOWED_USERS",
            Platform.YUANBAO: "YUANBAO_ALLOWED_USERS",
        }
        platform_group_user_env_map = {
            Platform.TELEGRAM: "TELEGRAM_GROUP_ALLOWED_USERS",
        }
        platform_group_chat_env_map = {
            Platform.TELEGRAM: "TELEGRAM_GROUP_ALLOWED_CHATS",
            Platform.QQBOT: "QQ_GROUP_ALLOWED_USERS",
        }
        platform_allow_all_map = {
            Platform.TELEGRAM: "TELEGRAM_ALLOW_ALL_USERS",
            Platform.DISCORD: "DISCORD_ALLOW_ALL_USERS",
            Platform.WHATSAPP: "WHATSAPP_ALLOW_ALL_USERS",
            Platform.WHATSAPP_CLOUD: "WHATSAPP_CLOUD_ALLOW_ALL_USERS",
            Platform.SLACK: "SLACK_ALLOW_ALL_USERS",
            Platform.SIGNAL: "SIGNAL_ALLOW_ALL_USERS",
            Platform.EMAIL: "EMAIL_ALLOW_ALL_USERS",
            Platform.SMS: "SMS_ALLOW_ALL_USERS",
            Platform.MATTERMOST: "MATTERMOST_ALLOW_ALL_USERS",
            Platform.MATRIX: "MATRIX_ALLOW_ALL_USERS",
            Platform.DINGTALK: "DINGTALK_ALLOW_ALL_USERS",
            Platform.FEISHU: "FEISHU_ALLOW_ALL_USERS",
            Platform.WECOM: "WECOM_ALLOW_ALL_USERS",
            Platform.WECOM_CALLBACK: "WECOM_CALLBACK_ALLOW_ALL_USERS",
            Platform.WEIXIN: "WEIXIN_ALLOW_ALL_USERS",
            Platform.BLUEBUBBLES: "BLUEBUBBLES_ALLOW_ALL_USERS",
            Platform.QQBOT: "QQ_ALLOW_ALL_USERS",
            Platform.YUANBAO: "YUANBAO_ALLOW_ALL_USERS",
        }
        # 由 {PLATFORM}_ALLOW_BOTS 放行的 bot 会绕过人类 allowlist（#4466）。
        platform_allow_bots_map = {
            Platform.DISCORD: "DISCORD_ALLOW_BOTS",
            Platform.FEISHU: "FEISHU_ALLOW_BOTS",
        }

        # 插件平台：在注册表中查找授权用的环境变量名
        if source.platform not in platform_env_map:
            try:
                from gateway.platform_registry import platform_registry
                entry = platform_registry.get(source.platform.value)
                if entry:
                    if entry.allowed_users_env:
                        platform_env_map[source.platform] = entry.allowed_users_env
                    if entry.allow_all_env:
                        platform_allow_all_map[source.platform] = entry.allow_all_env
            except Exception:
                pass

        # 每个平台的 allow-all flag（例如 DISCORD_ALLOW_ALL_USERS=true）
        platform_allow_all_var = platform_allow_all_map.get(source.platform, "")
        if platform_allow_all_var and os.getenv(platform_allow_all_var, "").lower() in {"true", "1", "yes"}:
            return True

        # adapter 已校验的角色授权：Discord adapter 在分发消息之前，已经确认
        # 该用户持有 DISCORD_ALLOWED_ROLES 中的某个角色。
        # 用 ``is True`` 比较，这样真正的 bool 字段才会授权，而一个
        # MagicMock source（使用 ``object.__new__`` runner 和 mock source 的
        # 测试夹具）不会在这个门控上自动通过真值判断（参见 pitfall #13）。
        if getattr(source, "role_authorized", False) is True:
            return True

        if getattr(source, "is_bot", False):
            allow_bots_var = platform_allow_bots_map.get(source.platform)
            if allow_bots_var and os.getenv(allow_bots_var, "none").lower().strip() in {"mentions", "all"}:
                return True

        # 检查配对存储（总是检查，无论是否有 allowlist）
        platform_name = source.platform.value if source.platform else ""
        if self.pairing_store.is_approved(platform_name, user_id):
            return True

        # 检查平台特定和全局 allowlist
        platform_allowlist = os.getenv(platform_env_map.get(source.platform, ""), "").strip()
        group_user_allowlist = ""
        group_chat_allowlist = ""
        if source.chat_type in {"group", "forum"}:
            group_user_allowlist = os.getenv(platform_group_user_env_map.get(source.platform, ""), "").strip()
            group_chat_allowlist = os.getenv(platform_group_chat_env_map.get(source.platform, ""), "").strip()
        global_allowlist = os.getenv("GATEWAY_ALLOWED_USERS", "").strip()

        if not platform_allowlist and not group_user_allowlist and not group_chat_allowlist and not global_allowlist:
            # 没有配置 env allowlist。那些自身拥有配置驱动访问策略
            # （dm_policy / group_policy / allow_from / group_allow_from）的
            # adapter 会在 intake 处进行门控，因此对于这些平台，我们可以遵从
            # adapter 的决策，而不是下面仅 env 的默认拒绝——但 *仅当* 该决策
            # 是一个真正的 allowlist 限制时。
            #
            # adapter 默认把 dm_policy / group_policy 设为 "open"，这会转发
            # *每一个* 发送者。在这种情况下把“到达了 gateway”解读为授权，
            # 就会在没有任何运营者配置的 allowlist 的情况下放行整个外部网络
            # ——这正是 SECURITY.md §2.6 所禁止的 fail-open（“每个启用的、暴露
            # 在网络上的 adapter 都需要一个 allowlist ……在未配置 allowlist 时
            # fail-open 的代码路径都是代码 bug”）。"disabled" 绝不转发，
            # 而 "pairing" 只转发未配对的 DM，以便 gateway 能跑它的配对握手
            # （上面的配对存储检查已经拒绝了该发送者）。因此，只有当 adapter
            # 对 *本* 聊天类型实际生效的策略是 "allowlist" 时才信任它；对于
            # "open" / "pairing" / 其他任何情况，都落入默认拒绝，在那里
            # GATEWAY_ALLOW_ALL_USERS、每平台的 {PLATFORM}_ALLOW_ALL_USERS
            # flag（已在上方检查）以及配对流程仍然是扩大访问的显式 opt-in。
            # （#34515 的后续：信任 "open" 曾是一次 fail-open。）
            if self._adapter_enforces_own_access_policy(source.platform):
                if source.chat_type in {"group", "forum", "channel"}:
                    effective_policy = self._adapter_group_policy(source.platform)
                    if self._adapter_group_has_sender_allowlist(
                        source.platform,
                        source.chat_id,
                    ):
                        return True
                else:
                    effective_policy = self._adapter_dm_policy(source.platform)
                if effective_policy == "allowlist":
                    return True
            # 没有配置 allowlist —— 检查全局 allow-all flag
            return os.getenv("GATEWAY_ALLOW_ALL_USERS", "").lower() in {"true", "1", "yes"}

        # Telegram 可以选择按 chat ID 授权群组流量。
        # 将此与 TELEGRAM_GROUP_ALLOWED_USERS 分开，后者用于门控 group/forum
        # 消息的发送者 user ID。
        if group_chat_allowlist and source.chat_type in {"group", "forum"} and source.chat_id:
            allowed_group_ids = {
                chat_id.strip() for chat_id in group_chat_allowlist.split(",") if chat_id.strip()
            }
            if "*" in allowed_group_ids or source.chat_id in allowed_group_ids:
                return True

        # 针对向 #15027 的后向兼容垫片：在 PR #17686 之前，
        # TELEGRAM_GROUP_ALLOWED_USERS 被（错误地）当作 chat-ID allowlist 使用。
        # 以 "-" 开头的值是 Telegram chat ID，而非 user ID，因此如果用户的
        # TELEGRAM_GROUP_ALLOWED_USERS 中仍有这些值，我们会把它们当作 chat ID
        # 来兑现，并警告一次。现在正确的变量是
        # TELEGRAM_GROUP_ALLOWED_CHATS。
        if (
            source.platform == Platform.TELEGRAM
            and group_user_allowlist
            and source.chat_type in {"group", "forum"}
            and source.chat_id
        ):
            legacy_chat_ids = {
                v.strip()
                for v in group_user_allowlist.split(",")
                if v.strip().startswith("-")
            }
            if legacy_chat_ids:
                if not getattr(self, "_warned_telegram_group_users_legacy", False):
                    logger.warning(
                        "TELEGRAM_GROUP_ALLOWED_USERS contains chat-ID-shaped values "
                        "(%s). Treating them as chat IDs for backward compatibility. "
                        "Move chat IDs to TELEGRAM_GROUP_ALLOWED_CHATS — the _USERS var "
                        "is now for sender user IDs.",
                        ",".join(sorted(legacy_chat_ids)),
                    )
                    self._warned_telegram_group_users_legacy = True
                if source.chat_id in legacy_chat_ids:
                    return True

        # 检查用户是否在任一 allowlist 中。在 group/forum 聊天中，
        # TELEGRAM_GROUP_ALLOWED_USERS 是作用域 allowlist，不应隐含 DM 访问权；
        # TELEGRAM_ALLOWED_USERS 仍然是平台范围的 allowlist，出于向后兼容，
        # 它在任何地方都依然有效。
        allowed_ids = set()
        if platform_allowlist:
            allowed_ids.update(uid.strip() for uid in platform_allowlist.split(",") if uid.strip())
        if group_user_allowlist:
            allowed_ids.update(uid.strip() for uid in group_user_allowlist.split(",") if uid.strip())
        if global_allowlist:
            allowed_ids.update(uid.strip() for uid in global_allowlist.split(",") if uid.strip())

        # 任一 allowlist 中的 "*" 表示允许所有人（与 SIGNAL_GROUP_ALLOWED_USERS
        # 的先例一致）
        if "*" in allowed_ids:
            return True

        check_ids = {user_id}
        if "@" in user_id:
            check_ids.add(user_id.split("@")[0])

        # WhatsApp：从 bridge 的会话映射文件解析电话↔LID 别名
        if source.platform == Platform.WHATSAPP:
            normalized_allowed_ids = set()
            for allowed_id in allowed_ids:
                normalized_allowed_ids.update(_expand_whatsapp_auth_aliases(allowed_id))
            if normalized_allowed_ids:
                allowed_ids = normalized_allowed_ids

            check_ids.update(_expand_whatsapp_auth_aliases(user_id))
            normalized_user_id = _normalize_whatsapp_identifier(user_id)
            if normalized_user_id:
                check_ids.add(normalized_user_id)

        # SimpleX：SIMPLEX_ALLOWED_USERS 既接受数字形式的 contactId，也接受该
        # 联系人的显示名。adapter 把 user_id 设为 contactId 以在重命名后保持
        # 稳定，但 SimpleX UI 从不展示数字 id —— 运营者只能看到显示名，因此
        # 他们自然会把显示名放进环境变量里。同时匹配两者，这样无论选择了哪种
        # 形式，allowlist 都能生效。
        # 插件平台：按 value 比较，因为 Platform.SIMPLEX 不是硬编码的 enum 成员
        # （它是一个动态插件平台）。
        if (
            source.platform is not None
            and source.platform.value == "simplex"
            and source.user_name
        ):
            check_ids.add(source.user_name)

        return bool(check_ids & allowed_ids)

    def _get_unauthorized_dm_behavior(self, platform: Optional[Platform]) -> str:
        """返回某平台上未授权 DM 应当如何处理。

        解析顺序：
        1. config 中显式的每平台 ``unauthorized_dm_behavior`` —— 总是胜出。
        2. Email 默认为 ``"ignore"``，除非显式 opt-in 到配对。收件箱里可能
           含有任意未读的人类消息，因此用配对码回复并不是一个安全的平台默认
           行为。
        3. config 中显式的全局 ``unauthorized_dm_behavior`` —— 在没有设置
           每平台覆盖时，对聊天型平台胜出。
        4. 当 adapter 层的 DM 策略 opt-in 到配对或静默丢弃时，遵从它。
        5. 当配置了 allowlist（``PLATFORM_ALLOWED_USERS``、
           ``PLATFORM_GROUP_ALLOWED_USERS`` / ``PLATFORM_GROUP_ALLOWED_CHATS``，
           或 ``GATEWAY_ALLOWED_USERS``）时，默认为 ``"ignore"`` —— allowlist
           表明 owner 已经刻意限制了访问；用配对码轰炸未知联系人既嘈杂，
           又可能造成信息泄露。（#9337）
        6. 没有 allowlist 且没有显式配置 → ``"pair"``（开放 gateway 的默认值）。
        """
        config = getattr(self, "config", None)

        # 先检查是否有显式的每平台覆盖。
        if config and hasattr(config, "get_unauthorized_dm_behavior") and platform:
            platform_cfg = config.platforms.get(platform) if hasattr(config, "platforms") else None
            if platform_cfg and "unauthorized_dm_behavior" in getattr(platform_cfg, "extra", {}):
                # 运营者已为本平台显式配置了行为 —— 遵从它。
                return config.get_unauthorized_dm_behavior(platform)

        # Email 是收件箱形态，而非聊天形态：一个 agent 邮箱里可能含有不相关的
        # 未读人类邮件。在用配对码回复未知发送者之前，要求显式的每平台
        # ``unauthorized_dm_behavior: pair`` opt-in。把它放在全局回退之前，
        # 以与 GatewayConfig.get_unauthorized_dm_behavior() 保持一致。
        if platform == Platform.EMAIL:
            return "ignore"

        # 检查是否有显式的全局 config 覆盖。
        if config and hasattr(config, "unauthorized_dm_behavior"):
            if config.unauthorized_dm_behavior != "pair":  # 非默认值 → 显式覆盖
                return config.unauthorized_dm_behavior

        # 配置驱动的 dm_policy（WeCom / Weixin / Yuanbao / QQBot）。一个
        # allowlist 或 disabled DM 策略意味着运营者限制了访问，因此未授权 DM
        # 应当被静默丢弃，而不是用配对码回复。显式的 pairing 策略会重新
        # opt-in 到配对码。
        if platform and config and hasattr(config, "platforms"):
            platform_cfg = config.platforms.get(platform)
            extra = getattr(platform_cfg, "extra", None) if platform_cfg else None
            if isinstance(extra, dict):
                dm_policy = str(extra.get("dm_policy") or "").strip().lower()
                if dm_policy == "pairing":
                    return "pair"
                if dm_policy in {"allowlist", "disabled"}:
                    return "ignore"

        # 没有显式覆盖。回退到感知 allowlist 的默认行为：
        # 如果为本平台配置了任何 allowlist，则静默丢弃未授权消息，而不是发送
        # 配对码。
        if platform:
            platform_env_map = {
                Platform.TELEGRAM: "TELEGRAM_ALLOWED_USERS",
                Platform.DISCORD:  "DISCORD_ALLOWED_USERS",
                Platform.WHATSAPP: "WHATSAPP_ALLOWED_USERS",
                Platform.WHATSAPP_CLOUD: "WHATSAPP_CLOUD_ALLOWED_USERS",
                Platform.SLACK:    "SLACK_ALLOWED_USERS",
                Platform.SIGNAL:   "SIGNAL_ALLOWED_USERS",
                Platform.EMAIL:    "EMAIL_ALLOWED_USERS",
                Platform.SMS:      "SMS_ALLOWED_USERS",
                Platform.MATTERMOST: "MATTERMOST_ALLOWED_USERS",
                Platform.MATRIX:   "MATRIX_ALLOWED_USERS",
                Platform.DINGTALK: "DINGTALK_ALLOWED_USERS",
                Platform.FEISHU:   "FEISHU_ALLOWED_USERS",
                Platform.WECOM:    "WECOM_ALLOWED_USERS",
                Platform.WECOM_CALLBACK: "WECOM_CALLBACK_ALLOWED_USERS",
                Platform.WEIXIN:   "WEIXIN_ALLOWED_USERS",
                Platform.BLUEBUBBLES: "BLUEBUBBLES_ALLOWED_USERS",
                Platform.QQBOT:    "QQ_ALLOWED_USERS",
            }
            platform_group_env_map = {
                Platform.TELEGRAM: (
                    "TELEGRAM_GROUP_ALLOWED_USERS",
                    "TELEGRAM_GROUP_ALLOWED_CHATS",
                ),
                Platform.QQBOT: ("QQ_GROUP_ALLOWED_USERS",),
            }
            if os.getenv(platform_env_map.get(platform, ""), "").strip():
                return "ignore"
            for env_key in platform_group_env_map.get(platform, ()):
                if os.getenv(env_key, "").strip():
                    return "ignore"

        if os.getenv("GATEWAY_ALLOWED_USERS", "").strip():
            return "ignore"

        return "pair"
