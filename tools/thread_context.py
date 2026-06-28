#!/usr/bin/env python3
"""把 agent 轮次的上下文传播到派发 Hermes 工具的工作线程中。

一个裸的 ``threading.Thread`` / ``ThreadPoolExecutor`` 工作线程启动时
带有一个空的 ``contextvars.Context``，也没有线程本地的 approval/sudo 回调。
因此，在这种线程内部派发工具会悄悄丢失：

  * approval 的 *会话/平台* ContextVars（``tools.approval`` /
    ``gateway.session_context``）—— 于是 gateway 会话落入
    ``check_dangerous_command`` 的非交互式自动批准分支，
    危险命令不经提示就运行（#33057、#30882）；
  * 线程本地的 CLI approval/sudo 回调（``tools.terminal_tool``）——
    于是 ``prompt_dangerous_approval`` 无法触达用户
    （GHSA-qg5c-hvr5-hjgr、#15216）。

本辅助函数把"捕获/安装/清理"这一生命周期抽取出来，让若干把工具派发
分摊到工作线程的地方（``agent.tool_executor`` 和
``execute_code`` RPC 线程）共用一套经过审计的实现，而不是各自分叉。

用法 —— 在**父线程**上调用 :func:`propagate_context_to_thread`
（它在调用时对父线程的 ContextVars 和回调做快照），并把返回的可调用对象
作为工作线程的 target::

    t = threading.Thread(target=propagate_context_to_thread(loop_fn), args=(...))
    # 或
    executor.submit(propagate_context_to_thread(worker_fn), *args)

Approval/sudo 回调在工作线程的整个生命周期内都被安装，并且**总是在退出时
清理**，所以一个被复用的线程绝不会持有一个已销毁 CLI 实例的过期引用。
"""

from __future__ import annotations

import contextvars
import logging
from typing import Callable

logger = logging.getLogger(__name__)


def _callback_api():
    """解析 terminal_tool 的回调 getter/setter。

    延迟导入：``tools.terminal_tool`` 在模块加载时就会导入 ``tools.approval``，
    所以这里在顶层导入会为位于 ``tools.approval`` 中的调用方
    带来导入循环的风险。
    """
    from tools.terminal_tool import (
        _get_approval_callback,
        _get_sudo_password_callback,
        set_approval_callback,
        set_sudo_password_callback,
    )
    return (
        _get_approval_callback,
        _get_sudo_password_callback,
        set_approval_callback,
        set_sudo_password_callback,
    )


def propagate_context_to_thread(target: Callable) -> Callable:
    """包装 *target*，使其在工作线程上执行时能传播*当前*线程的
    ContextVars 和 approval/sudo 回调。

    在父线程上调用本函数；把返回的可调用对象作为
    线程/执行器的 target。返回的可调用对象会把它的位置参数和
    关键字参数转发给 *target*，并返回其结果。

    故障关闭（fail-closed）：如果回调安装抛出异常，回调会保持未设置
    （``None``）状态。这是安全的结果 —— 当交互式上下文中没有注册回调时，
    ``prompt_dangerous_approval`` 会拒绝危险命令，而当 gateway
    approval 队列缺少 notify 回调时会阻塞。
    """
    ctx = contextvars.copy_context()
    parent_approval_cb = parent_sudo_cb = None
    setters = None
    try:
        get_approval, get_sudo, set_approval, set_sudo = _callback_api()
        parent_approval_cb = get_approval()
        parent_sudo_cb = get_sudo()
        setters = (set_approval, set_sudo)
    except Exception:
        logger.debug("Could not capture parent approval/sudo callbacks", exc_info=True)

    def _runner(*args, **kwargs):
        def _inner():
            if setters is not None:
                set_approval, set_sudo = setters
                try:
                    if parent_approval_cb is not None:
                        set_approval(parent_approval_cb)
                    if parent_sudo_cb is not None:
                        set_sudo(parent_sudo_cb)
                except Exception:
                    logger.debug(
                        "Failed to install propagated approval/sudo callbacks; "
                        "dangerous-command approval will fail closed",
                        exc_info=True,
                    )
            try:
                return target(*args, **kwargs)
            finally:
                if setters is not None:
                    set_approval, set_sudo = setters
                    try:
                        set_approval(None)
                        set_sudo(None)
                    except Exception:
                        logger.debug(
                            "Failed to clear propagated approval/sudo callbacks",
                            exc_info=True,
                        )

        return ctx.run(_inner)

    return _runner
