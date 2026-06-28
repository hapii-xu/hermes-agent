"""从现有压缩链派生 ACP 会话来源元数据。

这是一个附加的 Hermes 扩展，通过 ACP ``_meta.hermes`` 暴露，现有 ACP
客户端会忽略它。它不携带新的持久化状态：所有内容都按需从 ``sessions`` 表
（``parent_session_id`` / ``end_reason``）派生，该表已经建模了压缩延续链。

ACP/编辑器的 ``session_id`` 保持为稳定的公开句柄。当上下文压缩轮换内部
Hermes 头时，``build_session_provenance`` 让客户端可以看到前一个/当前的
内部 ID 和谱系根，无需解析状态文本、从 token 下降猜测或读取 ``state.db``。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# 限制防御性遍历深度；这么深的压缩链属于病态情况。
_MAX_WALK = 100


def build_session_provenance(
    db: Any,
    acp_session_id: str,
    current_hermes_session_id: str,
    *,
    previous_hermes_session_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """为 ACP 会话构建 ``_meta.hermes.sessionProvenance``。

    Args:
        db: 一个 ``SessionDB``（必须暴露 ``get_session``）。
        acp_session_id: 稳定的面向 ACP/编辑器的会话句柄。
        current_hermes_session_id: 当前活跃的内部 Hermes DB 会话 ID
            （``state.agent.session_id``）。
        previous_hermes_session_id: 最近一轮之前的内部 ID（如果已知）。
            由 ``prompt()`` 提供以标记轮换。

    Returns:
        适用于 ACP ``_meta`` 下 ``{"hermes": {"sessionProvenance": <dict>}}``
        的字典，如果无法读取会话则返回 ``None``。
    """
    try:
        row = db.get_session(current_hermes_session_id)
    except Exception:
        return None
    if not row:
        return None

    parent_id = row.get("parent_session_id")
    end_reason = row.get("end_reason")

    # 向上遍历父级到谱系根并统计压缩深度。仅压缩分割的父级
    # （parent.end_reason == 'compression'）计入深度 — 委派/分支子级
    # 共享 parent_session_id 列但不是压缩边界。
    root_id = current_hermes_session_id
    compression_depth = 0
    cursor_parent = parent_id
    seen = {current_hermes_session_id}
    for _ in range(_MAX_WALK):
        if not cursor_parent or cursor_parent in seen:
            break
        seen.add(cursor_parent)
        try:
            prow = db.get_session(cursor_parent)
        except Exception:
            prow = None
        if not prow:
            break
        root_id = cursor_parent
        if prow.get("end_reason") == "compression":
            compression_depth += 1
        cursor_parent = prow.get("parent_session_id")

    # 当会话的父级以 end_reason='compression' 结束时，该会话是压缩延续。
    # 从直接父级确定这一点。
    is_continuation = False
    if parent_id:
        try:
            immediate_parent = db.get_session(parent_id)
        except Exception:
            immediate_parent = None
        if immediate_parent and immediate_parent.get("end_reason") == "compression":
            is_continuation = True

    rotated = bool(
        previous_hermes_session_id
        and previous_hermes_session_id != current_hermes_session_id
    )

    provenance: Dict[str, Any] = {
        "acpSessionId": acp_session_id,
        "currentHermesSessionId": current_hermes_session_id,
        "rootHermesSessionId": root_id,
        "parentHermesSessionId": parent_id,
        "sessionKind": "continuation" if is_continuation else "root",
        "compressionDepth": compression_depth,
    }
    if previous_hermes_session_id:
        provenance["previousHermesSessionId"] = previous_hermes_session_id
    if rotated:
        # 上一轮期间头部发生了移动。唯一能在轮次中途轮换内部 ID 的
        # 机制是压缩驱动的会话分割。
        provenance["reason"] = "compression"
        provenance["creatorKind"] = "compression"

    return provenance


def session_provenance_meta(
    db: Any,
    acp_session_id: str,
    current_hermes_session_id: str,
    *,
    previous_hermes_session_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """返回就绪的 ``_meta`` 载荷：``{"hermes": {"sessionProvenance": ...}}``。"""
    prov = build_session_provenance(
        db,
        acp_session_id,
        current_hermes_session_id,
        previous_hermes_session_id=previous_hermes_session_id,
    )
    if prov is None:
        return None
    return {"hermes": {"sessionProvenance": prov}}
