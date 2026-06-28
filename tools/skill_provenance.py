"""技能写入来源溯源——用一个 ContextVar 区分 agent 沉淀式技能写入与前台用户主导的写入。

curator 只会合并/修剪那些由后台自我改进评审分叉自主创建的技能。
而用户要求前台 agent 写入的技能归属于用户，绝不能被自动整理。

本模块暴露一个 ContextVar，由 run_agent.py 在每个工具循环之前设置，
以便工具处理器（例如 skill_manage create）能够判断自己当前是否运行在
后台评审分叉之中。

该信号搭载在 AIAgent._memory_write_origin 上：对于评审分叉实例，它已被
设为 "background_review"（见 run_agent.py 中的 _spawn_background_review）；
对于普通（前台）agent，默认值为 "assistant_tool"。

用法：
    from tools.skill_provenance import (
        set_current_write_origin,
        reset_current_write_origin,
        get_current_write_origin,
    )

    token = set_current_write_origin("background_review")
    try:
        ...  # 工具在这里运行
    finally:
        reset_current_write_origin(token)

    # 在某个工具内部：
    if get_current_write_origin() == "background_review":
        mark_agent_created(skill_name)
"""

import contextvars


_write_origin: contextvars.ContextVar[str] = contextvars.ContextVar(
    "skill_write_origin",
    default="foreground",
)

# 后台评审分叉所使用的哨兵值；与 run_agent.py 中
# _spawn_background_review() 对 AIAgent._memory_write_origin 的覆盖保持一致。
BACKGROUND_REVIEW = "background_review"


def set_current_write_origin(origin: str) -> contextvars.Token[str]:
    """把当前生效的写入来源绑定到当前上下文。

    返回一个 Token，调用方必须在 finally 块中把它传给
    reset_current_write_origin。
    """
    return _write_origin.set(origin or "foreground")


def reset_current_write_origin(token: contextvars.Token[str]) -> None:
    """恢复先前的写入来源上下文。"""
    _write_origin.reset(token)


def get_current_write_origin() -> str:
    """返回当前生效的写入来源。

    默认值："foreground"——由普通（非评审）agent 从 CLI、gateway、
    cron 或子 agent 发起的任何工具调用。

    "background_review"——自我改进评审分叉；只有在此来源下创建的技能
    才应被标记为 agent 创建，交由 curator 管理。
    """
    return _write_origin.get()


def is_background_review() -> bool:
    """便捷方法：当且仅当前写入来源为后台评审分叉时返回 True。"""
    return get_current_write_origin() == BACKGROUND_REVIEW
