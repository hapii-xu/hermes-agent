"""gateway 响应过滤辅助函数。

这些辅助函数工作在 gateway 边界：它们决定一个已完成的 agent turn 是否应该
被投递到聊天，而不是决定什么内容应该被持久化到对话历史中。
"""

from __future__ import annotations

from typing import Any

# 模型发射的、表示刻意沉默的规范控制 token。
SILENT_REPLY_TOKEN = "NO_REPLY"

# 精确的整条响应标记，表示“agent 刻意选择不回复”。请保持本列表精简且明确；
# 任意空输出仍走 error/empty-response 路径，而不是沉默。
LIVE_GATEWAY_SILENT_MARKERS = frozenset({
    "[SILENT]",
    "SILENT",
    "NO_REPLY",
    "NO REPLY",
})


def _canonical_silence_candidate(text: str) -> str:
    return " ".join(text.strip().upper().split())


def is_intentional_silence_response(response: Any) -> bool:
    """仅当 ``response`` 恰好是一个沉默标记时返回 True。

    那些只是顺带提到 ``NO_REPLY`` 或 ``[SILENT]`` 的实质性正文必须照常投递。
    空白响应同样不算沉默；空白输出由 empty-response 失败路径处理。
    """
    if not isinstance(response, str):
        return False
    stripped = response.strip()
    if not stripped:
        return False
    if len(stripped) > 64:
        return False
    return _canonical_silence_candidate(stripped) in LIVE_GATEWAY_SILENT_MARKERS


def is_intentional_silence_agent_result(agent_result: dict | None, response: Any) -> bool:
    """沉默标记仅对成功的 agent turn 抑制投递。"""
    if not isinstance(agent_result, dict):
        return False
    if agent_result.get("failed"):
        return False
    return is_intentional_silence_response(response)
