"""Modal 传输在 Hermes 侧共享的执行流程。

本模块有意只到 Hermes 边界为止：
- 命令准备
- cwd/timeout 规范化
- stdin/sudo 的 shell 包装
- 统一的结果形状
- 中断/取消轮询

直连 Modal 与托管 Modal 各自在各自模块中保留独立的传输逻辑、持久化与
信任边界决策。
"""

from __future__ import annotations

import shlex
import time
import uuid
from abc import abstractmethod
from dataclasses import dataclass
from typing import Any

from tools.environments.base import BaseEnvironment
from tools.interrupt import is_interrupted


@dataclass(frozen=True)
class PreparedModalExec:
    """传给传输相关执行 runner 的规范化命令数据。"""

    command: str
    cwd: str
    timeout: int
    stdin_data: str | None = None


@dataclass(frozen=True)
class ModalExecStart:
    """启动一次 exec 后的传输响应。"""

    handle: Any | None = None
    immediate_result: dict | None = None


def wrap_modal_stdin_heredoc(command: str, stdin_data: str) -> str:
    """为不支持 stdin 管道的传输，把 stdin 作为 shell heredoc 追加到命令后。"""
    marker = f"HERMES_EOF_{uuid.uuid4().hex[:8]}"
    while marker in stdin_data:
        marker = f"HERMES_EOF_{uuid.uuid4().hex[:8]}"
    return f"{command} << '{marker}'\n{stdin_data}\n{marker}"


def wrap_modal_sudo_pipe(command: str, sudo_stdin: str) -> str:
    """为不支持直接 stdin 管道的传输，通过 shell 管道喂入 sudo。"""
    return f"printf '%s\\n' {shlex.quote(sudo_stdin.rstrip())} | {command}"


class BaseModalExecutionEnvironment(BaseEnvironment):
    """*托管* Modal 传输（网关拥有的沙箱）的执行流程。

    这里有意覆盖 :meth:`BaseEnvironment.execute`，因为工具网关在服务端
    处理命令准备、CWD 跟踪与环境快照管理。基类的 ``_wrap_command`` /
    ``_wait_for_process`` / 快照机制在此不适用——网关承担这些职责。具体
    子类见 ``ManagedModalEnvironment``。
    """

    _stdin_mode = "payload"
    _poll_interval_seconds = 0.25
    _client_timeout_grace_seconds: float | None = None
    _interrupt_output = "[Command interrupted]"
    _unexpected_error_prefix = "Modal execution error"

    def execute(
        self,
        command: str,
        cwd: str = "",
        *,
        timeout: int | None = None,
        stdin_data: str | None = None,
        rewrite_compound_background: bool = True,
    ) -> dict:
        # 托管/远程 modal 传输通过显式传输执行命令，不依赖 shell 后台重写器。
        # 保留该参数仅为兼容 BaseEnvironment 的调用方。
        _ = rewrite_compound_background
        self._before_execute()
        prepared = self._prepare_modal_exec(
            command,
            cwd=cwd,
            timeout=timeout,
            stdin_data=stdin_data,
        )

        try:
            start = self._start_modal_exec(prepared)
        except Exception as exc:
            return self._error_result(f"{self._unexpected_error_prefix}: {exc}")

        if start.immediate_result is not None:
            return start.immediate_result

        if start.handle is None:
            return self._error_result(
                f"{self._unexpected_error_prefix}: transport did not return an exec handle"
            )

        deadline = None
        if self._client_timeout_grace_seconds is not None:
            deadline = time.monotonic() + prepared.timeout + self._client_timeout_grace_seconds

        _now = time.monotonic()
        _activity_state = {
            "last_touch": _now,
            "start": _now,
        }

        while True:
            if is_interrupted():
                try:
                    self._cancel_modal_exec(start.handle)
                except Exception:
                    pass
                return self._result(self._interrupt_output, 130)

            try:
                result = self._poll_modal_exec(start.handle)
            except Exception as exc:
                return self._error_result(f"{self._unexpected_error_prefix}: {exc}")

            if result is not None:
                return result

            if deadline is not None and time.monotonic() >= deadline:
                try:
                    self._cancel_modal_exec(start.handle)
                except Exception:
                    pass
                return self._timeout_result_for_modal(prepared.timeout)

            # 周期性活动心跳，让网关知道我们还活着
            try:
                from tools.environments.base import touch_activity_if_due
                touch_activity_if_due(_activity_state, "modal command running")
            except Exception:
                pass

            time.sleep(self._poll_interval_seconds)

    def _before_execute(self) -> None:
        """供需要在执行前做同步或校验的后端使用的钩子。"""
        pass

    def _prepare_modal_exec(
        self,
        command: str,
        *,
        cwd: str = "",
        timeout: int | None = None,
        stdin_data: str | None = None,
    ) -> PreparedModalExec:
        effective_cwd = cwd or self.cwd
        effective_timeout = timeout or self.timeout

        exec_command = command
        exec_stdin = stdin_data if self._stdin_mode == "payload" else None
        if stdin_data is not None and self._stdin_mode == "heredoc":
            exec_command = wrap_modal_stdin_heredoc(exec_command, stdin_data)

        exec_command, sudo_stdin = self._prepare_command(exec_command)
        if sudo_stdin is not None:
            exec_command = wrap_modal_sudo_pipe(exec_command, sudo_stdin)

        return PreparedModalExec(
            command=exec_command,
            cwd=effective_cwd,
            timeout=effective_timeout,
            stdin_data=exec_stdin,
        )

    def _result(self, output: str, returncode: int) -> dict:
        return {
            "output": output,
            "returncode": returncode,
        }

    def _error_result(self, output: str) -> dict:
        return self._result(output, 1)

    def _timeout_result_for_modal(self, timeout: int) -> dict:
        return self._result(f"Command timed out after {timeout}s", 124)

    @abstractmethod
    def _start_modal_exec(self, prepared: PreparedModalExec) -> ModalExecStart:
        """开始一次传输相关的 exec。"""

    @abstractmethod
    def _poll_modal_exec(self, handle: Any) -> dict | None:
        """完成时返回最终结果字典，否则返回 ``None``。"""

    @abstractmethod
    def _cancel_modal_exec(self, handle: Any) -> None:
        """取消或终止活动的传输 exec。"""
