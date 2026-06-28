"""按平台划分的 slash command 访问控制。

本模块位于现有的按平台 allowlist（``allow_from``）旁边，并新增了第二个维度：
在那些*被允许与 gateway 通信的用户*中，哪些人可以运行*哪些 slash command*。

每个平台 scope（DM 与 group，分别对应 ``allow_from`` 与
``group_allow_from``）各有两个列表：

  - ``allow_admin_from``      — 拥有所有已注册 slash command（内置 + 插件注册）
                                使用权的用户 ID。
  - ``user_allowed_commands`` — 非管理员用户可以运行的 slash command 名称。
                                为空 / 未设置 → 非管理员没有任何 slash command。

向后兼容性：

  如果某个 scope 未设置 ``allow_admin_from``，则该 scope 上的 slash command
  门控将被完全关闭。所有被允许的用户都可以运行所有 slash command，与之前
  完全一致。这意味着现有安装不受影响，直到运维人员选择启用（即至少列出
  一个 admin）。

该门控在 ``gateway/run.py`` 中的 slash command 派发处生效，因此通过实时
注册表同时覆盖内置命令和插件注册命令。对 slash command 进行门控不会影响
普通聊天——非管理员用户仍可正常与 agent 通信，只是无法触发
``user_allowed_commands`` 之外的命令。

作为 PR #4443 权限分级方案的精简版 salvage（由 @ReqX 共同署名）。该 PR 中的
完整分级系统、审计日志、用量追踪、限流和工具过滤未在此处包含——这里只保留
slash command 的访问划分。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, FrozenSet, Iterable, Optional, Tuple


# 这些 slash command 必须对任何被允许的用户保持可达，即使启用了 slash
# 门控且该用户没有任何已列出的命令。如果没有这项豁免，非管理员用户将无法
# 了解自己能或不能做什么（``/help``、``/whoami``），也无法查看 agent 当前
# 处于什么状态（``/status``）。这些镜像了我们会交给访客的最小只读命令集合。
# 运维人员仍可通过编写自己的 ``user_allowed_commands`` 进一步收窄（本集合仅
# 是隐式回退下限——``user_allowed_commands`` 中的内容只会做加法，永远不会
# 反向限制）。
_ALWAYS_ALLOWED_FOR_USERS: FrozenSet[str] = frozenset({
    "help",
    "whoami",
})


@dataclass(frozen=True)
class SlashAccessPolicy:
    """针对单个 (platform, scope) 对解析得到的访问策略。

    ``scope`` 为 ``"dm"`` 表示直接消息，``"group"`` 表示群组、频道、
    线程以及任何其他多用户上下文。SessionSource.chat_type → scope 的映射
    在 ``policy_for_source`` 中完成。
    """

    enabled: bool                      # 该 scope 是否启用了门控？
    admin_user_ids: FrozenSet[str]
    user_allowed_commands: FrozenSet[str]

    def is_admin(self, user_id: Optional[str]) -> bool:
        if not self.enabled:
            # 门控已关闭 → 将每个被允许的用户都视为 admin，这样下游代码可以
            # 统一地继续使用 ``is_admin`` / ``can_run``。
            return True
        if not user_id:
            return False
        return str(user_id) in self.admin_user_ids

    def can_run(self, user_id: Optional[str], canonical_cmd: str) -> bool:
        if not self.enabled:
            return True
        if self.is_admin(user_id):
            return True
        if not canonical_cmd:
            return False
        if canonical_cmd in _ALWAYS_ALLOWED_FOR_USERS:
            return True
        return canonical_cmd in self.user_allowed_commands


_DM_CHAT_TYPES = frozenset({"dm", "direct", "private", ""})


def _coerce_id_list(raw: Any) -> FrozenSet[str]:
    """将 YAML 加载得到的 admin/user 列表归一化为字符串 frozenset。

    接受 ``None``、list、tuple 或以逗号分隔的字符串。每项会被转为字符串
    并去除首尾空白；空白项会被丢弃。
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, (list, tuple, set, frozenset)):
        items: Iterable[Any] = raw
    elif isinstance(raw, str):
        items = (s for s in raw.split(",") if s.strip())
    else:
        # 单个标量（int 型用户 id 等）
        items = (raw,)
    out: list[str] = []
    for it in items:
        s = str(it).strip()
        if s:
            out.append(s)
    return frozenset(out)


def _coerce_command_list(raw: Any) -> FrozenSet[str]:
    """将 slash command allowlist 归一化。

    去除开头斜杠，使得 YAML 既可以写 ``["help", "status"]``，也可以写
    ``["/help", "/status"]``。小写归一化与 ``resolve_command()`` 存储名称的
    方式保持一致。
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, (list, tuple, set, frozenset)):
        items: Iterable[Any] = raw
    elif isinstance(raw, str):
        items = (s for s in raw.split(",") if s.strip())
    else:
        items = (raw,)
    out: list[str] = []
    for it in items:
        s = str(it).strip().lstrip("/").lower()
        if s:
            out.append(s)
    return frozenset(out)


def _scope_for_chat_type(chat_type: Optional[str]) -> str:
    if chat_type and chat_type.lower() in _DM_CHAT_TYPES:
        return "dm"
    return "group"


def _platform_extra(platform_config: Any) -> dict:
    """从类 PlatformConfig 对象中返回 ``extra`` 字典。

    防御性地处理 None 与非 PlatformConfig 形状，使调用方代码保持简洁。
    """
    if platform_config is None:
        return {}
    extra = getattr(platform_config, "extra", None)
    if isinstance(extra, dict):
        return extra
    if isinstance(platform_config, dict):
        # 某些测试夹具会直接传入 dict。
        return platform_config
    return {}


def _keys_for_scope(scope: str) -> Tuple[str, str]:
    """返回某个 scope 对应的 (admin_key, user_cmd_key) 键名。"""
    if scope == "group":
        return ("group_allow_admin_from", "group_user_allowed_commands")
    return ("allow_admin_from", "user_allowed_commands")


def policy_from_extra(extra: dict, scope: str) -> SlashAccessPolicy:
    """根据某个平台的 ``extra`` 字典为单个 scope 构建策略。

    当 DM scope 未指定自己的 ``user_allowed_commands`` 时，DM scope 仅在
    ``user_allowed_commands`` 上回退到 group scope 的键。这样可以让常见情形
    （运维希望 DM 与 group 使用相同的命令集合）保持简洁，而不必重复填写。
    Admin 列表不跨 scope：DM 中的 admin 并不会隐式成为 group 中的 admin。
    """
    admin_key, cmd_key = _keys_for_scope(scope)
    admin_ids = _coerce_id_list(extra.get(admin_key))
    cmds = _coerce_command_list(extra.get(cmd_key))

    if scope == "dm" and not cmds:
        # DM 未指定 → 让 group 的 user_allowed_commands 回退生效，
        # 这样在两者相同时，运维只需填写一次。
        cmds = _coerce_command_list(extra.get("group_user_allowed_commands"))

    enabled = bool(admin_ids)
    return SlashAccessPolicy(
        enabled=enabled,
        admin_user_ids=admin_ids,
        user_allowed_commands=cmds,
    )


def policy_for_source(gateway_config: Any, source: Any) -> SlashAccessPolicy:
    """为某个 SessionSource 解析访问策略。

    在以下情况返回“已关闭”策略（门控关闭，允许全部）：
      - gateway_config 为 None
      - 该平台没有 PlatformConfig
      - 该平台的 PlatformConfig 在该 scope 上未设置 admin 列表

    调用方应将返回的策略视为仅对 slash command 门控具有权威性。它并不会
    对普通聊天消息进行门控。
    """
    if gateway_config is None or source is None:
        return SlashAccessPolicy(
            enabled=False,
            admin_user_ids=frozenset(),
            user_allowed_commands=frozenset(),
        )
    platforms = getattr(gateway_config, "platforms", None)
    platform_config = None
    if platforms is not None:
        try:
            platform_config = platforms.get(source.platform)
        except Exception:
            platform_config = None
    extra = _platform_extra(platform_config)
    scope = _scope_for_chat_type(getattr(source, "chat_type", None))
    return policy_from_extra(extra, scope)


__all__ = [
    "SlashAccessPolicy",
    "policy_from_extra",
    "policy_for_source",
]
