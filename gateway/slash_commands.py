"""GatewayRunner 的 gateway slash command 处理器。

从 ``gateway/run.py`` 中抽取（god-file 拆分第 3b 阶段）。这些是 gateway 在
``_handle_message`` 中派发的会话内 slash command（/model、/reset、/usage、
/compress 等），共 42 个（约 3200 行）；将它们上提为一个由 ``GatewayRunner``
继承的 mixin，使得每个 ``self._handle_*_command`` 派发和测试引用都能通过
MRO 继续工作，同时把大块代码从 run.py 中移出。

处理器所需的模块级 run.py 辅助函数（``_hermes_home``、
``_load_gateway_config``、``_resolve_gateway_model`` 等）都在处理器函数体内
延迟导入——延迟执行的 ``from gateway.run import ...`` 会在调用时解析（此时
run.py 已完全加载），从而避免 import 循环。
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import inspect
import logging
import os
import re
import shlex
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

from agent.account_usage import fetch_account_usage, render_account_usage_lines
from agent.i18n import t
from gateway.config import HomeChannel, Platform, PlatformConfig
from gateway.platforms.base import EphemeralReply, MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key
from hermes_cli.config import cfg_get, clear_model_endpoint_credentials
from utils import (
    atomic_json_write,
    atomic_yaml_write,
    base_url_host_matches,
    is_truthy_value,
)

logger = logging.getLogger("gateway.run")

# /new 或 /reset 期间脱离 event loop 执行的 agent 资源清理的时长上限
# （见 _handle_reset_command）。卡死的 teardown 不能阻塞 event loop；超过该
# 时长后 reset 照常继续，清理工作交由其工作线程自行完成（或泄漏）。（#35994）
_RESET_CLEANUP_TIMEOUT_S = 30.0


def _model_switch_skew_guard() -> Optional[str]:
    """当 gateway 正在运行过期代码时拒绝切换模型。

    长生命周期的 gateway 会把自启动以来加载的模块保留在内存中。如果在此期间
    checkout 发生了变化（例如手动 ``git pull``），切换模型时可能会在新代码路径
    上首次触发延迟导入，并因过期的缓存依赖而崩溃——出现晦涩的
    ``cannot import name 'env_float' from 'utils'``。这里检测这种漂移并提示
    用户改为重启。

    有意限定在模型切换这个已知且风险最高的触发点上。过期的进程上任何首次
    延迟导入在技术上都有暴露风险；我们并不保护每一个 import 位置，仅此一处。
    """
    from gateway.code_skew import detect_code_skew

    skew = detect_code_skew()
    if not skew:
        return None
    boot_rev, disk_rev = skew
    return t(
        "gateway.model.error_prefix",
        error=(
            f"This gateway is running code from {boot_rev} but the checkout on "
            f"disk is now {disk_rev}. Switching models would risk a stale-module "
            f"crash — restart the gateway to load the new code: hermes gateway restart"
        ),
    )


class GatewaySlashCommandsMixin:
    """GatewayRunner 的会话内 slash command 处理器。"""

    def _typed_command_prefix_for(self, platform) -> str:
        """返回用户总是可以键入以触达 Hermes 命令的前缀。

        读取 adapter 的 ``typed_command_prefix`` 能力标志（默认为 "/"）。
        Slack 和 Matrix 返回 "!"，因为在 Slack 线程中键入 "/" 命令会被拦截 /
        被 Matrix 客户端保留；它们的 adapter 在接收时会把 "!command" 改写为
        "/command"。为这些平台构造的说明文字必须展示实际可用的前缀。
        """
        adapter = self.adapters.get(platform) if getattr(self, "adapters", None) else None
        return getattr(adapter, "typed_command_prefix", "/") if adapter is not None else "/"

    async def _handle_reset_command(self, event: MessageEvent) -> Union[str, EphemeralReply]:
        """处理 /new 或 /reset 命令。"""
        source = event.source

        # 获取现有 session key
        session_key = self._session_key_for_source(source)
        self._invalidate_session_run_generation(session_key, reason="session_reset")
        # 既然 generation 已经递增，就驱逐正在运行的 agent 槽位。在途 run 自身的
        # 受保护释放（run_generation=old）会返回 False 并留下其已死掉的 agent；
        # 在这里清空可以避免该槽位变成僵尸槽位，从而悄悄丢弃后续所有消息
        # （#28686）。该操作幂等，因此 run 的 finally 再次调用它是无害的。
        self._release_running_agent_state(session_key)

        # 快照旧条目，以便 on_session_finalize 在 reset_session() 轮换它之前
        # 可以报告即将到期的 session id。
        old_entry = self.session_store._entries.get(session_key)

        # 在从缓存驱逐之前，关闭旧 agent 上的工具资源（terminal 沙盒、浏览器
        # 守护进程、后台进程）。用 getattr 做防御性访问，因为测试夹具可能跳过
        # __init__。
        #
        # _cleanup_agent_resources 是同步的，可能长时间阻塞（agent.close() 会做
        # 子进程 teardown；shutdown_memory_provider() 可能产生网络 IO）。当
        # Telegram/Discord/Slack 的确认按钮点击解析 slash-confirm 时（见
        # _request_slash_confirm），本处理器直接在 event loop 上运行，因此内联
        # 调用会卡住整个 loop，使 bot 直到重启前都不再响应（#35994）。将其卸载
        # 到工作线程（通过保留 contextvar 的 executor 辅助函数），并设置有界
        # 超时，从而 loop 永远不会被阻塞。
        _cache_lock = getattr(self, "_agent_cache_lock", None)
        if _cache_lock is not None:
            with _cache_lock:
                _cached = self._agent_cache.get(session_key)
                _old_agent = _cached[0] if isinstance(_cached, tuple) else _cached if _cached else None
            if _old_agent is not None:
                try:
                    await asyncio.wait_for(
                        self._run_in_executor_with_context(
                            self._cleanup_agent_resources, _old_agent
                        ),
                        timeout=_RESET_CLEANUP_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    # wait_for 会取消 await，但工作线程无法被取消——卡死的
                    # teardown 会在 gateway 的整个生命周期内继续运行（或泄漏）。
                    # reset 无论如何都会继续进行。
                    logger.warning(
                        "Agent resource cleanup for session %s exceeded %ss during "
                        "/new reset; proceeding with reset (the worker thread is left "
                        "to finish on its own). (#35994)",
                        session_key, _RESET_CLEANUP_TIMEOUT_S,
                    )
                except Exception as cleanup_exc:
                    logger.warning(
                        "Agent resource cleanup for session %s failed during /new "
                        "reset: %s (#35994)",
                        session_key, cleanup_exc,
                    )
        self._evict_cached_agent(session_key)

        # 丢弃该 session 的任何 /queue 溢出——/new 是一次会话边界操作，
        # 上一段会话中排队的后续消息绝不能渗透到新的会话中。
        _qe = getattr(self, "_queued_events", None)
        if _qe is not None:
            _qe.pop(session_key, None)

        try:
            from tools.env_passthrough import clear_env_passthrough
            clear_env_passthrough()
        except Exception:
            pass

        try:
            from tools.credential_files import clear_credential_files
            clear_credential_files()
        except Exception:
            pass

        # 重置 session
        new_entry = self.session_store.reset_session(session_key)

        # 清除任何 session 级别的 model/reasoning 覆盖，使下一个 agent 使用
        # 配置的默认值，而非上一段会话中的切换结果。
        self._session_model_overrides.pop(session_key, None)
        self._set_session_reasoning_override(session_key, None)
        if hasattr(self, "_pending_model_notes"):
            self._pending_model_notes.pop(session_key, None)

        # 清除 session 级别的危险命令审批与 /yolo 状态。/new 是一次会话边界
        # 操作——上一段会话中的审批状态不应在 reset 后继续存在。
        self._clear_session_boundary_security_state(session_key)

        _old_sid = old_entry.session_id if old_entry else None

        # 触发插件 on_session_finalize 钩子（会话边界）
        try:
            from hermes_cli.plugins import invoke_hook as _invoke_hook
            _invoke_hook(
                "on_session_finalize",
                session_id=_old_sid,
                platform=source.platform.value if source.platform else "",
                reason="new_session",
                old_session_id=_old_sid,
                new_session_id=new_entry.session_id if new_entry else None,
            )
        except Exception:
            pass

        # 发出 session:end 钩子（session 正在结束）
        await self.hooks.emit("session:end", {
            "platform": source.platform.value if source.platform else "",
            "user_id": source.user_id,
            "session_key": session_key,
        })

        # 发出 session:reset 钩子
        await self.hooks.emit("session:reset", {
            "platform": source.platform.value if source.platform else "",
            "user_id": source.user_id,
            "session_key": session_key,
        })

        # 解析要向用户展示的 session 配置信息
        try:
            session_info = self._format_session_info()
        except Exception:
            session_info = ""

        if new_entry:
            header = self._telegram_topic_new_header(source) or t("gateway.reset.header_default")
        else:
            # 不存在现有 session，直接创建一个
            new_entry = self.session_store.get_or_create_session(source, force_new=True)
            header = self._telegram_topic_new_header(source) or t("gateway.reset.header_new")

        # 如果通过 /new <title> 提供了标题，则设置 session 标题
        _title_arg = event.get_command_args().strip()
        _title_note = ""
        if _title_arg and self._session_db and new_entry:
            from hermes_state import SessionDB
            try:
                sanitized = SessionDB.sanitize_title(_title_arg)
            except ValueError as e:
                sanitized = None
                _title_note = t("gateway.reset.title_rejected", error=str(e))
            if sanitized:
                try:
                    self._session_db.set_session_title(new_entry.session_id, sanitized)
                    header = t("gateway.reset.header_titled", title=sanitized)
                except ValueError as e:
                    _title_note = t("gateway.reset.title_error_untitled", error=str(e))
                except Exception:
                    pass
            elif not _title_note:
                # sanitize_title 返回为空（仅空白 / 不可打印字符）
                _title_note = t("gateway.reset.title_empty_untitled")
        header = header + _title_note

        # 当 /new 在 Telegram DM topic lane 中执行时，改写
        # (chat_id, thread_id) → session_id 绑定，使下一条消息使用新创建的
        # session。否则该绑定仍指向旧 session，_handle_message_with_agent
        # 顶部的绑定查询会立即切回去。
        if self._is_telegram_topic_lane(source) and new_entry is not None:
            try:
                self._record_telegram_topic_binding(source, new_entry)
            except Exception:
                logger.debug("Failed to rebind Telegram topic after /new", exc_info=True)

        # 触发插件 on_session_reset 钩子（保证新 session 已存在）
        try:
            from hermes_cli.plugins import invoke_hook as _invoke_hook
            _new_sid = new_entry.session_id if new_entry else None
            _invoke_hook(
                "on_session_reset",
                session_id=_new_sid,
                platform=source.platform.value if source.platform else "",
                reason="new_session",
                old_session_id=_old_sid,
                new_session_id=_new_sid,
            )
        except Exception:
            pass

        # 在 reset 消息末尾追加一条随机提示
        try:
            from hermes_cli.tips import get_random_tip
            _tip_line = t("gateway.reset.tip", tip=get_random_tip())
        except Exception:
            _tip_line = ""

        if session_info:
            return EphemeralReply(f"{header}\n\n{session_info}{_tip_line}")
        return EphemeralReply(f"{header}{_tip_line}")

    async def _handle_profile_command(self, event: MessageEvent) -> str:
        """处理 /profile —— 显示当前 profile 名称与主目录。"""
        from hermes_constants import display_hermes_home
        from hermes_cli.profiles import get_active_profile_name

        display = display_hermes_home()
        profile_name = get_active_profile_name()

        lines = [
            t("gateway.profile.header", profile=profile_name),
            t("gateway.profile.home", home=display),
        ]

        return "\n".join(lines)

    async def _handle_whoami_command(self, event: MessageEvent) -> str:
        """处理 /whoami —— 显示用户在当前 scope 上的 slash command 访问权限。

        始终可用（它位于 slash_access 的 always-allowed 下限中）。
        报告：platform、scope（DM 还是 group）、用户级别
        （admin / user / unrestricted），以及用户在该 scope 上实际可以运行的
        slash command。
        """
        from gateway.slash_access import policy_for_source as _policy_for_source

        source = event.source
        policy = _policy_for_source(self.config, source)
        platform = source.platform.value if source and source.platform else "?"
        chat_type = (source.chat_type if source else "") or "dm"
        scope = "DM" if chat_type.lower() in {"dm", "direct", "private", ""} else "group/channel"
        user_id = (source.user_id if source else None) or "?"

        if not policy.enabled:
            return (
                f"**You** — {platform} ({scope})\n"
                f"User ID: `{user_id}`\n"
                f"Tier: unrestricted (no admin list configured for this scope)\n"
                f"Slash commands: all available"
            )

        if policy.is_admin(user_id):
            return (
                f"**You** — {platform} ({scope})\n"
                f"User ID: `{user_id}`\n"
                f"Tier: **admin**\n"
                f"Slash commands: all available"
            )

        # 非管理员用户。展示实际可达的命令。
        floor = ["help", "whoami"]  # 对应 slash_access._ALWAYS_ALLOWED_FOR_USERS
        configured = sorted(policy.user_allowed_commands)
        # 合并并去重，保持顺序：先下限，再运维添加项。
        seen: set[str] = set()
        runnable: list[str] = []
        for c in floor + configured:
            if c not in seen:
                seen.add(c)
                runnable.append(c)
        runnable_str = ", ".join(f"/{c}" for c in runnable) if runnable else "(none)"
        return (
            f"**You** — {platform} ({scope})\n"
            f"User ID: `{user_id}`\n"
            f"Tier: user\n"
            f"Slash commands you can run: {runnable_str}"
        )

    async def _handle_kanban_command(self, event: MessageEvent) -> str:
        """处理 /kanban —— 委托给共享的 kanban CLI。

        将可能阻塞的 DB 操作放到线程池中执行，以便 gateway event loop 保持
        响应。agent 运行期间允许执行读操作（list、show、context、tail）；写操作
        同样允许，因为看板与 profile 无关，且不会触及运行中 agent 的状态。

        对于 ``/kanban create`` 调用，还会自动将发起该操作的 gateway source
        （platform + chat + thread）订阅到新任务的终止事件，使得 worker 完成 /
        阻塞 / 自动阻塞 / 崩溃时用户能收到反馈，而无需主动轮询。
        """
        import asyncio
        import re
        import shlex
        from hermes_cli.kanban import run_slash

        text = (event.text or "").strip()
        # 去除开头的 "/kanban"（带或不带斜杠），保留参数部分。
        if text.startswith("/"):
            text = text.lstrip("/")
        if text.startswith("kanban"):
            text = text[len("kanban"):].lstrip()

        tokens = shlex.split(text) if text else []
        requested_board = None
        action = None
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok == "--board":
                if i + 1 >= len(tokens):
                    break
                requested_board = tokens[i + 1]
                i += 2
                continue
            if tok.startswith("--board="):
                requested_board = tok.split("=", 1)[1]
                i += 1
                continue
            action = tok
            break

        is_create = action == "create"

        try:
            output = await asyncio.to_thread(run_slash, text)
        except Exception as exc:  # pragma: no cover - defensive
            return t("gateway.kanban.error_prefix", error=exc)

        # create 时自动订阅。从 CLI 的标准成功输出行
        # （"Created t_abcd  (ready, assignee=...)"）解析出 task id。如果用户
        # 传入了 --json，则不订阅——他们显然是在写脚本，可以显式调用
        # /kanban notify-subscribe。
        if is_create and output:
            m = re.search(r"Created\s+(t_[0-9a-f]+)\b", output)
            if m:
                task_id = m.group(1)
                try:
                    source = event.source
                    platform = getattr(source, "platform", None)
                    platform_str = (
                        platform.value if hasattr(platform, "value") else str(platform or "")
                    ).lower()
                    chat_id = str(getattr(source, "chat_id", "") or "")
                    thread_id = str(getattr(source, "thread_id", "") or "")
                    user_id = str(getattr(source, "user_id", "") or "") or None
                    if platform_str and chat_id:
                        def _sub():
                            from hermes_cli import kanban_db as _kb
                            conn = _kb.connect(board=requested_board)
                            try:
                                _kb.add_notify_sub(
                                    conn, task_id=task_id,
                                    platform=platform_str, chat_id=chat_id,
                                    thread_id=thread_id or None,
                                    user_id=user_id,
                                    notifier_profile=getattr(self, "_kanban_notifier_profile", None) or self._active_profile_name(),
                                )
                            finally:
                                conn.close()
                        await asyncio.to_thread(_sub)
                        output = (
                            output.rstrip()
                            + "\n"
                            + t("gateway.kanban.subscribed_suffix", task_id=task_id)
                        )
                except Exception as exc:
                    logger.warning("kanban create auto-subscribe failed: %s", exc)

        # gateway 消息有实际的长度上限；对长列表进行截断以保持合理体验。
        if len(output) > 3800:
            output = output[:3800] + "\n" + t("gateway.kanban.truncated_suffix")
        return output or t("gateway.kanban.no_output")

    async def _handle_status_command(self, event: MessageEvent) -> str:
        """处理 /status 命令。"""
        from gateway.run import _AGENT_PENDING_SENTINEL, _load_gateway_config, _resolve_gateway_model

        source = event.source
        session_entry = self.session_store.get_or_create_session(source)

        connected_platforms = [p.value for p in self.adapters.keys()]

        # 检查是否有活动的 agent。保持 sentinel 的特殊语义：一个正在启动 / 等待
        # 中的 run 不应被视为可用于模型/上下文展示的完整 agent，但它仍然占用
        # session 槽位。
        session_key = session_entry.session_key
        agent = self._running_agents.get(session_key)
        is_running = agent is not None and agent is not _AGENT_PENDING_SENTINEL

        # 统计等待中的 /queue 后续消息（槽位 + 溢出部分）。
        adapter = self.adapters.get(source.platform) if source else None
        queue_depth = self._queue_depth(session_key, adapter=adapter)

        def _clean_str(value: Any) -> str:
            return value.strip() if isinstance(value, str) and value.strip() else ""

        def _int_value(value: Any) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        title = None
        session_row: dict[str, Any] = {}
        # 从 SQLite session DB 拉取 token 总数，而不是从内存中的 SessionStore。
        # agent 每轮的 token 增量会持久化到 sessions_db（run_agent.py），
        # 而不是 SessionEntry，因此 session_entry.total_tokens 始终为 0。
        # SessionDB 是唯一可信来源；在这里读取可以保持 /status 准确，避免把
        # token 写入两份存储。
        db_total_tokens = 0
        if self._session_db:
            try:
                title = self._session_db.get_session_title(session_entry.session_id)
            except Exception:
                title = None
            try:
                row = self._session_db.get_session(session_entry.session_id)
                if isinstance(row, dict):
                    session_row = row
                    db_total_tokens = (
                        _int_value(row.get("input_tokens"))
                        + _int_value(row.get("output_tokens"))
                        + _int_value(row.get("cache_read_tokens"))
                        + _int_value(row.get("cache_write_tokens"))
                        + _int_value(row.get("reasoning_tokens"))
                    )
            except Exception:
                db_total_tokens = 0

        # 为 cockpit 风格的状态展示解析 model/context。优先使用活动或缓存的
        # agent，因为它承载了实际运行时的路由和 context compressor。回退到
        # 持久化的 SessionDB 元数据加上 SessionStore 的 last_prompt_tokens，
        # 使得 /status 在两轮之间仍然有用，且不会产生计费/账户调用。
        status_agent = agent if is_running else None
        if status_agent is None:
            cache_lock = getattr(self, "_agent_cache_lock", None)
            cache = getattr(self, "_agent_cache", None)
            if cache_lock is not None and cache is not None:
                try:
                    with cache_lock:
                        cached = cache.get(session_key)
                    if cached:
                        status_agent = cached[0]
                except Exception:
                    status_agent = None

        model_name = ""
        provider_name = ""
        base_url = ""
        context_used = 0
        context_total = 0
        if status_agent is not None and status_agent is not _AGENT_PENDING_SENTINEL:
            model_name = _clean_str(getattr(status_agent, "model", ""))
            provider_name = _clean_str(getattr(status_agent, "provider", ""))
            base_url = _clean_str(getattr(status_agent, "base_url", ""))
            ctx = getattr(status_agent, "context_compressor", None)
            if ctx is not None:
                context_used = _int_value(getattr(ctx, "last_prompt_tokens", 0))
                context_total = _int_value(getattr(ctx, "context_length", 0))

        model_name = model_name or _clean_str(session_row.get("model"))
        provider_name = provider_name or _clean_str(session_row.get("billing_provider"))
        base_url = base_url or _clean_str(session_row.get("billing_base_url"))
        context_used = context_used or _int_value(getattr(session_entry, "last_prompt_tokens", 0))

        user_config: dict[str, Any] = {}
        if not model_name or not provider_name or not context_total:
            try:
                user_config = _load_gateway_config()
            except Exception:
                user_config = {}
        if not model_name:
            model_name = _resolve_gateway_model(user_config)
        if not provider_name:
            model_cfg = user_config.get("model", {}) if isinstance(user_config, dict) else {}
            if isinstance(model_cfg, dict):
                provider_name = _clean_str(model_cfg.get("provider"))
        if not context_total:
            model_cfg = user_config.get("model", {}) if isinstance(user_config, dict) else {}
            configured_context = model_cfg.get("context_length") if isinstance(model_cfg, dict) else None
            if isinstance(configured_context, int) and configured_context > 0:
                context_total = configured_context

        model_line = ""
        if model_name:
            if provider_name:
                model_line = t("gateway.status.model_provider", model=model_name, provider=provider_name)
            else:
                model_line = t("gateway.status.model", model=model_name)

        context_line = ""
        if context_total:
            pct = min(100, round((context_used / context_total) * 100)) if context_total else 0
            context_line = t(
                "gateway.status.context",
                used=f"{context_used:,}",
                total=f"{context_total:,}",
                pct=f"{pct}",
            )
        elif context_used:
            context_line = t("gateway.status.context_used", used=f"{context_used:,}")

        lines = [
            t("gateway.status.header"),
            "",
            t("gateway.status.session_id", session_id=session_entry.session_id),
        ]
        if title:
            lines.append(t("gateway.status.title", title=title))
        lines.extend([
            t("gateway.status.created", timestamp=session_entry.created_at.strftime('%Y-%m-%d %H:%M')),
            t("gateway.status.last_activity", timestamp=session_entry.updated_at.strftime('%Y-%m-%d %H:%M')),
        ])
        if model_line:
            lines.append(model_line)
        if context_line:
            lines.append(context_line)
        lines.extend([
            t("gateway.status.tokens", tokens=f"{db_total_tokens:,}"),
            t("gateway.status.agent_running", state=t("gateway.status.state_yes") if is_running else t("gateway.status.state_no")),
        ])
        if queue_depth:
            lines.append(t("gateway.status.queued", count=queue_depth))
        if source.platform == Platform.MATRIX:
            adapter = self.adapters.get(Platform.MATRIX)
            scope = getattr(adapter, "_matrix_session_scope", os.getenv("MATRIX_SESSION_SCOPE", "auto"))
            thread = source.thread_id or "none"
            lines.extend([
                "",
                t("gateway.status.matrix_scope_header"),
                t("gateway.status.matrix_scope_room", room=source.chat_name or source.chat_id),
                t("gateway.status.matrix_scope_room_id", room_id=source.chat_id),
                t("gateway.status.matrix_scope_thread", thread_id=thread),
                t("gateway.status.matrix_scope_mode", scope=scope),
                t(
                    "gateway.status.matrix_scope_key",
                    session_key=self._redact_matrix_session_key(session_key),
                ),
            ])
        lines.extend([
            "",
            t("gateway.status.platforms", platforms=', '.join(connected_platforms)),
        ])

        return "\n".join(lines)

    @staticmethod
    def _redact_matrix_session_key(session_key: str) -> str:
        """返回一个稳定的 Matrix session-key 指纹，用于共享房间状态展示。"""
        text = str(session_key or "")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        return f"sha256:{digest}"

    def _gateway_session_origin_for_id(self, session_id: str) -> Optional[SessionSource]:
        """尽最大努力查找 gateway session ID 对应的 origin。"""
        lookup = getattr(type(self.session_store), "lookup_by_session_id", None)
        if callable(lookup):
            entry = lookup(self.session_store, session_id)
            return getattr(entry, "origin", None) if entry is not None else None

        # 测试替身和较旧的 store 可能不提供该公共查找辅助。如果无法解析出
        # origin，则让 Matrix resume 守卫以 fail-closed 方式处理。
        entries = getattr(self.session_store, "_entries", {}) or {}
        for entry in entries.values():
            if getattr(entry, "session_id", None) == session_id:
                return getattr(entry, "origin", None)
        return None

    @staticmethod
    def _same_matrix_room(current: SessionSource, origin: Optional[SessionSource]) -> bool:
        return (
            origin is not None
            and origin.platform == Platform.MATRIX
            and current.platform == Platform.MATRIX
            and origin.chat_id == current.chat_id
        )

    async def _handle_agents_command(self, event: MessageEvent) -> str:
        """处理 /agents 命令 —— 列出活动的 agent 和运行中的任务。"""
        from gateway.run import _AGENT_PENDING_SENTINEL
        from tools.process_registry import format_uptime_short, process_registry

        now = time.time()
        current_session_key = self._session_key_for_source(event.source)

        running_agents: dict = getattr(self, "_running_agents", {}) or {}
        running_started: dict = getattr(self, "_running_agents_ts", {}) or {}

        agent_rows: list[dict] = []
        for session_key, agent in running_agents.items():
            started = float(running_started.get(session_key, now))
            elapsed = max(0, int(now - started))
            is_pending = agent is _AGENT_PENDING_SENTINEL
            agent_rows.append(
                {
                    "session_key": session_key,
                    "elapsed": elapsed,
                    "state": t("gateway.agents.state_starting") if is_pending else t("gateway.agents.state_running"),
                    "session_id": "" if is_pending else str(getattr(agent, "session_id", "") or ""),
                    "model": "" if is_pending else str(getattr(agent, "model", "") or ""),
                }
            )

        agent_rows.sort(key=lambda row: row["elapsed"], reverse=True)

        running_processes: list[dict] = []
        try:
            running_processes = [
                p for p in process_registry.list_sessions()
                if p.get("status") == "running"
            ]
        except Exception:
            running_processes = []

        background_tasks = [
            t for t in (getattr(self, "_background_tasks", set()) or set())
            if hasattr(t, "done") and not t.done()
        ]

        lines = [
            t("gateway.agents.header"),
            "",
            t("gateway.agents.active_agents", count=len(agent_rows)),
        ]

        if agent_rows:
            for idx, row in enumerate(agent_rows[:12], 1):
                current = t("gateway.agents.this_chat") if row["session_key"] == current_session_key else ""
                sid = f" · `{row['session_id']}`" if row["session_id"] else ""
                model = f" · `{row['model']}`" if row["model"] else ""
                lines.append(
                    f"{idx}. `{row['session_key']}` · {row['state']} · "
                    f"{format_uptime_short(row['elapsed'])}{sid}{model}{current}"
                )
            if len(agent_rows) > 12:
                lines.append(t("gateway.agents.more", count=len(agent_rows) - 12))

        lines.extend(
            [
                "",
                t("gateway.agents.running_processes", count=len(running_processes)),
            ]
        )
        if running_processes:
            for proc in running_processes[:12]:
                cmd = " ".join(str(proc.get("command", "")).split())
                if len(cmd) > 90:
                    cmd = cmd[:87] + "..."
                lines.append(
                    f"- `{proc.get('session_id', '?')}` · "
                    f"{format_uptime_short(int(proc.get('uptime_seconds', 0)))} · `{cmd}`"
                )
            if len(running_processes) > 12:
                lines.append(t("gateway.agents.more", count=len(running_processes) - 12))

        lines.extend(
            [
                "",
                t("gateway.agents.async_jobs", count=len(background_tasks)),
            ]
        )

        if not agent_rows and not running_processes and not background_tasks:
            lines.append("")
            lines.append(t("gateway.agents.none"))

        return "\n".join(lines)

    async def _handle_stop_command(self, event: MessageEvent) -> Union[str, EphemeralReply]:
        """处理 /stop 命令 —— 中断正在运行的 agent。

        当 agent 真正卡死时（被阻塞的线程从不检查 _interrupt_requested），
        _handle_message() 中的早期拦截会在进入本方法之前处理 /stop。本处理器
        仅通过常规命令派发（无运行中的 agent）或作为兜底触发。在所有情况下都
        强制清理 session 锁以确保安全。

        session 会被保留，以便用户可以继续对话。
        """
        from gateway.run import _AGENT_PENDING_SENTINEL, _INTERRUPT_REASON_STOP
        source = event.source
        session_entry = self.session_store.get_or_create_session(source)
        session_key = session_entry.session_key

        agent = self._running_agents.get(session_key)
        if agent is _AGENT_PENDING_SENTINEL:
            # 强制清掉 sentinel，使 session 解锁。
            await self._interrupt_and_clear_session(
                session_key,
                source,
                interrupt_reason=_INTERRUPT_REASON_STOP,
                invalidation_reason="stop_command_pending",
            )
            logger.info("STOP (pending) for session %s — sentinel cleared", session_key)
            return EphemeralReply(t("gateway.stop.stopped_pending"))
        if agent:
            # 强制清理 session 锁，使真正卡死的 agent 不会永久持有锁。
            await self._interrupt_and_clear_session(
                session_key,
                source,
                interrupt_reason=_INTERRUPT_REASON_STOP,
                invalidation_reason="stop_command_handler",
            )
            return EphemeralReply(t("gateway.stop.stopped"))

        # 调用者自己的 session key 下没有运行中的 run。在按用户划分线程的模式
        # （thread_sessions_per_user=True）中，即使是在同一个共享 thread 内，
        # 每个参与者也是隔离的，因此另一个用户启动的 run 会落在不同的 key 下。
        # 已授权的用户应仍能 /stop 它（#bernard-thread-stop）。回退为中断任何与
        # 当前 thread 共享、且通过授权校验的运行中 agent。
        sibling_keys = self._sibling_thread_run_keys(source, session_key)
        if sibling_keys and self._is_user_authorized(source):
            for sibling_key in sibling_keys:
                await self._interrupt_and_clear_session(
                    sibling_key,
                    source,
                    interrupt_reason=_INTERRUPT_REASON_STOP,
                    invalidation_reason="stop_command_thread_sibling",
                )
            logger.info(
                "STOP (thread sibling) by %s — interrupted %d run(s) in thread: %s",
                session_key,
                len(sibling_keys),
                ", ".join(sibling_keys),
            )
            return EphemeralReply(t("gateway.stop.stopped"))

        return t("gateway.stop.no_active")

    async def _handle_platform_command(self, event: MessageEvent) -> str:
        """处理 ``/platform list|pause|resume [name]`` —— 展示并手动控制失败 /
        已暂停的 gateway adapter。

        Examples:
            ``/platform list``           —— 显示已连接 + 失败/已暂停的平台
            ``/platform pause whatsapp`` —— 停止重连监视器反复冲击 whatsapp
            ``/platform resume whatsapp`` —— 将已暂停的平台重新加入重试队列
        """
        text = (getattr(event, "content", "") or "").strip()
        # 如果存在开头的 "/platform"（或 "/PLATFORM"）token，则去除它
        parts = text.split(maxsplit=2)
        if parts and parts[0].lower().lstrip("/").startswith("platform"):
            parts = parts[1:]
        action = (parts[0] if parts else "list").lower()
        target = parts[1].lower() if len(parts) > 1 else ""

        # 解析平台名称（大小写不敏感，按 value 匹配）
        def _resolve_platform(name: str):
            if not name:
                return None
            for p in Platform.__members__.values():
                if p.value.lower() == name:
                    return p
            return None

        if action == "list":
            lines = ["**Gateway platforms**"]
            connected = sorted(p.value for p in self.adapters.keys())
            if connected:
                lines.append("Connected: " + ", ".join(connected))
            else:
                lines.append("Connected: (none)")
            failed = getattr(self, "_failed_platforms", {}) or {}
            if failed:
                for p, info in failed.items():
                    if info.get("paused"):
                        reason = info.get("pause_reason") or "paused"
                        lines.append(
                            f"  · {p.value} — PAUSED ({reason}). "
                            f"Resume with `/platform resume {p.value}`."
                        )
                    else:
                        attempts = info.get("attempts", 0)
                        lines.append(
                            f"  · {p.value} — retrying (attempt {attempts})"
                        )
            else:
                lines.append("Failed/paused: (none)")
            return "\n".join(lines)

        if action in {"pause", "resume"}:
            if not target:
                return f"Usage: /platform {action} <name>"
            platform = _resolve_platform(target)
            if platform is None:
                return f"Unknown platform: {target}"
            failed = getattr(self, "_failed_platforms", {}) or {}
            if action == "pause":
                if platform not in failed:
                    return (
                        f"{platform.value} is not in the retry queue "
                        f"(it's either connected or not enabled)."
                    )
                if failed[platform].get("paused"):
                    return f"{platform.value} is already paused."
                self._pause_failed_platform(platform, reason="paused via /platform pause")
                return (
                    f"✓ {platform.value} paused. "
                    f"Resume with `/platform resume {platform.value}` or "
                    f"`hermes gateway restart` to reset."
                )
            # action == "resume"
            if platform not in failed:
                return (
                    f"{platform.value} is not in the retry queue — "
                    f"nothing to resume."
                )
            if not failed[platform].get("paused"):
                return (
                    f"{platform.value} is already retrying — "
                    f"no resume needed."
                )
            self._resume_paused_platform(platform)
            return f"✓ {platform.value} resumed — retrying on next watcher tick."

        return (
            "Usage: /platform <list|pause|resume> [name]\n"
            "  /platform list — show platform status\n"
            "  /platform pause <name> — stop retrying a failing platform\n"
            "  /platform resume <name> — re-queue a paused platform"
        )

    async def _handle_restart_command(self, event: MessageEvent) -> Union[str, EphemeralReply]:
        """处理 /restart 命令 —— 排空活动任务，然后重启 gateway。"""
        from gateway.run import _hermes_home
        # 防御性幂等检查：如果上一个 gateway 进程已经记录过同一条 /restart
        # （相同 platform + update_id），而新进程又再次看到它，则这是一次重投递，
        # 原因是 PTB 优雅关闭时 `get_updates` 的 ACK 在退出途中失败（"Error
        # while calling `get_updates` one more time to mark all fetched
        # updates. Suppressing error to ensure graceful shutdown. When
        # polling for updates is restarted, updates may be received twice."
        # 见 gateway.log）。忽略这条陈旧的重投递可以避免自我持续的 restart
        # 循环——否则每个新 gateway 都会重新处理同一条 /restart 命令并立刻
        # 再次重启。
        if self._is_stale_restart_redelivery(event):
            logger.info(
                "Ignoring redelivered /restart (platform=%s, update_id=%s) — "
                "already processed by a previous gateway instance.",
                event.source.platform.value if event.source and event.source.platform else "?",
                event.platform_update_id,
            )
            return ""

        if self._restart_requested or self._draining:
            count = self._running_agent_count()
            if count:
                return t("gateway.draining", count=count)
            return EphemeralReply(t("gateway.restart.in_progress"))

        # 保存请求者的路由信息，以便新 gateway 进程上线后可以通知他们。
        try:
            notify_data = {
                "platform": event.source.platform.value if event.source.platform else None,
                "chat_id": event.source.chat_id,
                "chat_type": event.source.chat_type,
            }
            if event.source.thread_id:
                notify_data["thread_id"] = event.source.thread_id
            if event.message_id:
                notify_data["message_id"] = event.message_id
            if event.source is not None:
                try:
                    self._restart_command_source = dataclasses.replace(
                        event.source,
                        message_id=str(event.message_id)
                        if event.message_id is not None
                        else event.source.message_id,
                    )
                except Exception:
                    self._restart_command_source = event.source
            atomic_json_write(
                _hermes_home / ".restart_notify.json",
                notify_data,
                indent=None,
            )
        except Exception as e:
            logger.debug("Failed to write restart notify file: %s", e)

        # 将触发本次重启的 platform + update_id 记录到专用的去重标记文件中。
        # 与 .restart_notify.json 不同（新 gateway 发出 "gateway restarted"
        # 通知后该文件就会被删除），这个标记会持久保留，使得新 gateway 仍能
        # 检测到来自 Telegram 的延迟 /restart 重投递。每次 /restart 都会覆盖它。
        try:
            dedup_data = {
                "platform": event.source.platform.value if event.source.platform else None,
                "requested_at": time.time(),
            }
            if event.platform_update_id is not None:
                dedup_data["update_id"] = event.platform_update_id
            atomic_json_write(
                _hermes_home / ".restart_last_processed.json",
                dedup_data,
                indent=None,
            )
        except Exception as e:
            logger.debug("Failed to write restart dedup marker: %s", e)

        active_agents = self._running_agent_count()
        # 当运行在 service manager（systemd/launchd）下，或运行在 Docker/Podman
        # 容器内时，使用 service 重启路径：以退出码 75 退出，由 service manager /
        # 容器重启策略来重启我们。detached 子进程方式（setsid + bash）在 systemd
        # 下不起作用（KillMode=mixed 会杀掉整个 cgroup），在 Docker 下也不行
        # （gateway 死亡时 tini 退出，连带着 detached 辅助进程一起被带走）。
        # systemd 会设置 INVOCATION_ID；launchd 会把 XPC_SERVICE_NAME 设为 job
        # 标签。如果没有 launchd 检查，macOS 上的 /restart 会走 detached 路径并以
        # 0 退出，而 KeepAlive.SuccessfulExit=false 会把这视为主动停止——于是
        # gateway 会一直处于死亡状态，直到下一次登录。交互式 macOS shell 会继承
        # XPC_SERVICE_NAME=0，因此 "0" 必须被视为“不在 launchd 下”。
        _under_service = bool(os.environ.get("INVOCATION_ID")) or os.environ.get(
            "XPC_SERVICE_NAME", "0"
        ) not in ("", "0")
        _in_container = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
        if _under_service or _in_container:
            self.request_restart(detached=False, via_service=True)
        else:
            self.request_restart(detached=True, via_service=False)
        if active_agents:
            return t("gateway.draining", count=active_agents)
        return EphemeralReply(t("gateway.restart.restarting"))

    async def _handle_version_command(self, event: MessageEvent) -> str:
        """处理 /version —— 显示正在运行的 Hermes Agent 版本。"""
        from hermes_cli.banner import format_banner_version_label

        return format_banner_version_label()

    async def _handle_help_command(self, event: MessageEvent) -> str:
        """处理 /help 命令 —— 列出可用命令。"""
        from gateway.run import _telegramize_command_mentions
        from hermes_cli.commands import gateway_help_lines
        lines = [
            t("gateway.help.header"),
            *gateway_help_lines(),
        ]
        try:
            from agent.skill_commands import get_skill_commands
            skill_cmds = get_skill_commands()
            if skill_cmds:
                lines.append(t("gateway.help.skill_header", count=len(skill_cmds)))
                # 先展示前 10 个，其余指向 /commands
                sorted_cmds = sorted(skill_cmds)
                for cmd in sorted_cmds[:10]:
                    lines.append(f"`{cmd}` — {skill_cmds[cmd]['description']}")
                if len(sorted_cmds) > 10:
                    lines.append(t("gateway.help.more_use_commands", count=len(sorted_cmds) - 10))
        except Exception:
            pass
        return _telegramize_command_mentions(
            "\n".join(lines),
            getattr(getattr(event, "source", None), "platform", None),
        )

    async def _handle_commands_command(self, event: MessageEvent) -> str:
        from gateway.run import _telegramize_command_mentions
        from hermes_cli.commands import gateway_help_lines

        raw_args = event.get_command_args().strip()
        if raw_args:
            try:
                requested_page = int(raw_args)
            except ValueError:
                return t("gateway.commands.usage")
        else:
            requested_page = 1

        # 构建合并的条目列表：内置命令 + skill 命令
        entries = list(gateway_help_lines())
        try:
            from agent.skill_commands import get_skill_commands
            skill_cmds = get_skill_commands()
            if skill_cmds:
                entries.append("")
                entries.append(t("gateway.commands.skill_header"))
                for cmd in sorted(skill_cmds):
                    desc = skill_cmds[cmd].get("description", "").strip() or t("gateway.commands.default_desc")
                    entries.append(f"`{cmd}` — {desc}")
        except Exception:
            pass

        if not entries:
            return t("gateway.commands.none")

        from gateway.config import Platform
        page_size = 15 if event.source.platform == Platform.TELEGRAM else 20
        total_pages = max(1, (len(entries) + page_size - 1) // page_size)
        page = max(1, min(requested_page, total_pages))
        start = (page - 1) * page_size
        page_entries = entries[start:start + page_size]

        lines = [
            t("gateway.commands.header", total=len(entries), page=page, total_pages=total_pages),
            "",
            *page_entries,
        ]
        if total_pages > 1:
            nav_parts = []
            if page > 1:
                nav_parts.append(t("gateway.commands.nav_prev", page=page - 1))
            if page < total_pages:
                nav_parts.append(t("gateway.commands.nav_next", page=page + 1))
            lines.extend(["", " | ".join(nav_parts)])
        if page != requested_page:
            lines.append(t("gateway.commands.out_of_range", requested=requested_page, page=page))
        return _telegramize_command_mentions(
            "\n".join(lines),
            getattr(getattr(event, "source", None), "platform", None),
        )

    async def _handle_model_command(self, event: MessageEvent) -> Optional[str]:
        """处理 /model 命令 —— 切换模型。

        支持：
          /model                              —— 交互式选择器（Telegram/Discord）或文本列表
          /model <name>                       —— 切换模型（默认持久化）
          /model <name> --session             —— 仅切换本次 session
          /model <name> --global              —— 切换并持久化（显式）
          /model <name> --provider <provider> —— 同时切换 provider + model
          /model --provider <provider>        —— 切换到 provider，自动检测 model
        """
        from gateway.run import _hermes_home, _load_gateway_config
        import yaml
        from hermes_cli.model_switch import (
            switch_model as _switch_model, parse_model_flags,
            resolve_persist_behavior,
            list_authenticated_providers,
            list_picker_providers,
        )
        from hermes_cli.providers import get_label

        raw_args = event.get_command_args().strip()

        # 解析 --provider、--global、--session 和 --refresh 标志
        (
            model_input,
            explicit_provider,
            is_global_flag,
            force_refresh,
            is_session,
        ) = parse_model_flags(raw_args)
        persist_global = resolve_persist_behavior(is_global_flag, is_session)

        # --refresh：使磁盘缓存失效，让选择器展示实时数据。
        if force_refresh:
            try:
                from hermes_cli.models import clear_provider_models_cache
                clear_provider_models_cache()
            except Exception:
                pass

        # 从 config 中读取当前的 model/provider
        current_model = ""
        current_provider = "openrouter"
        current_base_url = ""
        current_api_key = ""
        user_provs = None
        custom_provs = None
        config_path = _hermes_home / "config.yaml"
        try:
            cfg = _load_gateway_config()
            if cfg:
                model_cfg = cfg.get("model", {})
                if isinstance(model_cfg, dict):
                    current_model = model_cfg.get("default", "")
                    current_provider = model_cfg.get("provider", current_provider)
                    current_base_url = model_cfg.get("base_url", "")
                user_provs = cfg.get("providers")
                try:
                    from hermes_cli.config import get_compatible_custom_providers
                    custom_provs = get_compatible_custom_providers(cfg)
                except Exception:
                    custom_provs = cfg.get("custom_providers")
        except Exception:
            pass

        # 检查 session override
        source = event.source
        # 像普通消息回合一样对 source 进行归一化（Telegram DM topic 恢复），
        # 然后再派生 override key，使 override 存储到下一条消息回合所读取的
        # key 下（#30479）。
        source = self._normalize_source_for_session_key(source)
        session_key = self._session_key_for_source(source)
        override = self._session_model_overrides.get(session_key, {})
        if override:
            current_model = override.get("model", current_model)
            current_provider = override.get("provider", current_provider)
            current_base_url = override.get("base_url", current_base_url)
            current_api_key = override.get("api_key", current_api_key)

        # 无参数：展示交互式选择器（Telegram/Discord）或文本列表
        if not model_input and not explicit_provider:
            # 如果平台支持，尝试交互式选择器
            adapter = self.adapters.get(source.platform)
            has_picker = (
                adapter is not None
                and getattr(type(adapter), "send_model_picker", None) is not None
            )

            if has_picker:
                try:
                    # 把可能阻塞的 provider 列表查询（在缓存过期时会回落到同步的
                    # urllib HTTP 请求）从 event loop 卸载出去，避免 gateway 卡死。
                    # 见 #41289。
                    providers = await asyncio.to_thread(
                        list_picker_providers,
                        current_provider=current_provider,
                        current_base_url=current_base_url,
                        current_model=current_model,
                        user_providers=user_provs,
                        custom_providers=custom_provs,
                        max_models=50,
                    )
                except Exception:
                    providers = []

                if providers:
                    # 为用户选择模型时构建一个回调闭包。捕获 self 以及切换逻辑
                    # 所需的局部变量。
                    _self = self
                    _session_key = session_key
                    _cur_model = current_model
                    _cur_provider = current_provider
                    _cur_base_url = current_base_url
                    _cur_api_key = current_api_key

                    async def _on_model_selected(
                        _chat_id: str, model_id: str, provider_slug: str
                    ) -> str:
                        """执行模型切换并返回确认文本。"""
                        skew_error = _model_switch_skew_guard()
                        if skew_error:
                            return skew_error
                        result = _switch_model(
                            raw_input=model_id,
                            current_provider=_cur_provider,
                            current_model=_cur_model,
                            current_base_url=_cur_base_url,
                            current_api_key=_cur_api_key,
                            is_global=persist_global,
                            explicit_provider=provider_slug,
                            user_providers=user_provs,
                            custom_providers=custom_provs,
                        )
                        if not result.success:
                            return t("gateway.model.error_prefix", error=result.error_message)

                        try:
                            from hermes_cli.context_switch_guard import (
                                enrich_model_switch_warnings_for_gateway,
                            )

                            enrich_model_switch_warnings_for_gateway(
                                result,
                                _self,
                                session_key=_session_key,
                                source=event.source,
                                custom_providers=custom_provs,
                                load_gateway_config=_load_gateway_config,
                            )
                        except Exception as exc:
                            logger.debug("preflight-compression switch warning failed: %s", exc)

                        # 就地更新缓存的 agent
                        cached_entry = None
                        _cache_lock = getattr(_self, "_agent_cache_lock", None)
                        _cache = getattr(_self, "_agent_cache", None)
                        if _cache_lock and _cache is not None:
                            with _cache_lock:
                                cached_entry = _cache.get(_session_key)
                        if cached_entry and cached_entry[0] is not None:
                            try:
                                cached_entry[0].switch_model(
                                    new_model=result.new_model,
                                    new_provider=result.target_provider,
                                    api_key=result.api_key,
                                    base_url=result.base_url,
                                    api_mode=result.api_mode,
                                )
                            except Exception as exc:
                                # 就地切换把 agent 回滚到了旧的可用 model/client，
                                # 然后重新抛出异常。中止后续提交：不要把失败的 model
                                # 持久化到 DB，不要设置指向坏 model 的 session
                                # override，也不要驱逐可用的缓存 agent。否则下一条
                                # 消息会根据坏的 override 重新构建一个死掉的 agent，
                                # 对话就丢失了（#50163）。失败的切换必须是 no-op。
                                logger.warning(
                                    "Picker model switch failed for cached agent: %s", exc
                                )
                                return t(
                                    "gateway.model.error_prefix",
                                    error=(
                                        f"Model switch to {result.new_model} failed ({exc}); "
                                        f"staying on {_cur_model}."
                                    ),
                                )

                        # 把新模型持久化到 session DB，以便 dashboard 展示更新后的
                        # 模型（#34850）。
                        _sess_db = getattr(_self, "_session_db", None)
                        if _sess_db is not None:
                            try:
                                _sess_entry = _self.session_store.get_or_create_session(
                                    event.source
                                )
                                _sess_db.update_session_model(
                                    _sess_entry.session_id, result.new_model
                                )
                            except Exception as exc:
                                logger.debug(
                                    "Failed to persist model switch to DB: %s", exc
                                )

                        # 存储模型说明 + session override
                        if not hasattr(_self, "_pending_model_notes"):
                            _self._pending_model_notes = {}
                        _self._pending_model_notes[_session_key] = (
                            f"[Note: model was just switched from {_cur_model} to {result.new_model} "
                            f"via {result.provider_label or result.target_provider}. "
                            f"Adjust your self-identification accordingly.]"
                        )
                        _self._session_model_overrides[_session_key] = {
                            "model": result.new_model,
                            "provider": result.target_provider,
                            "api_key": result.api_key,
                            "base_url": result.base_url,
                            "api_mode": result.api_mode,
                        }

                        # 驱逐缓存 agent，使下一轮根据 override 创建新 agent，
                        # 而不是依赖陈旧的缓存签名来触发重建。
                        _self._evict_cached_agent(_session_key)

                        # 除非 --session 选择退出，否则持久化到 config（默认行为），
                        # 与上面文本 /model 命令路径保持一致，使选中的模型像键入的
                        # 模型一样跨 session 保留（#49066）。
                        if persist_global:
                            try:
                                if config_path.exists():
                                    with open(config_path, encoding="utf-8") as f:
                                        _persist_cfg = yaml.safe_load(f) or {}
                                else:
                                    _persist_cfg = {}
                                _raw_model = _persist_cfg.get("model")
                                if isinstance(_raw_model, dict):
                                    _persist_model_cfg = _raw_model
                                elif isinstance(_raw_model, str) and _raw_model.strip():
                                    _persist_model_cfg = {"default": _raw_model.strip()}
                                    _persist_cfg["model"] = _persist_model_cfg
                                else:
                                    _persist_model_cfg = {}
                                    _persist_cfg["model"] = _persist_model_cfg
                                _persist_model_cfg["default"] = result.new_model
                                _persist_model_cfg["provider"] = result.target_provider
                                if result.base_url:
                                    _persist_model_cfg["base_url"] = result.base_url
                                if str(result.target_provider or "").strip().lower() != "custom":
                                    clear_model_endpoint_credentials(_persist_model_cfg)
                                from hermes_cli.config import save_config
                                save_config(_persist_cfg)
                            except Exception as e:
                                logger.warning("Failed to persist model switch: %s", e)

                        # 构建确认文本
                        plabel = result.provider_label or result.target_provider
                        lines = [t("gateway.model.switched", model=result.new_model)]
                        lines.append(t("gateway.model.provider_label", provider=plabel))
                        mi = result.model_info
                        from hermes_cli.model_switch import resolve_display_context_length
                        _sw_config_ctx = None
                        try:
                            _sw_cfg = _load_gateway_config()
                            _sw_model_cfg = _sw_cfg.get("model", {})
                            if isinstance(_sw_model_cfg, dict):
                                _sw_raw = _sw_model_cfg.get("context_length")
                                if _sw_raw is not None:
                                    _sw_config_ctx = int(_sw_raw)
                        except Exception:
                            pass
                        ctx = resolve_display_context_length(
                            result.new_model,
                            result.target_provider,
                            base_url=result.base_url or current_base_url or "",
                            api_key=result.api_key or current_api_key or "",
                            model_info=mi,
                            custom_providers=custom_provs,
                            config_context_length=_sw_config_ctx,
                        )
                        if ctx:
                            lines.append(t("gateway.model.context_label", tokens=f"{ctx:,}"))
                        if mi:
                            if mi.max_output:
                                lines.append(t("gateway.model.max_output_label", tokens=f"{mi.max_output:,}"))
                            if mi.has_cost_data():
                                lines.append(t("gateway.model.cost_label", cost=mi.format_cost()))
                            lines.append(t("gateway.model.capabilities_label", capabilities=mi.format_capabilities()))
                        if result.warning_message:
                            lines.append(t("gateway.model.warning_prefix", warning=result.warning_message))
                        if persist_global:
                            lines.append(t("gateway.model.saved_global"))
                        else:
                            lines.append(t("gateway.model.session_only_hint"))
                        return "\n".join(lines)

                    metadata = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))
                    result = await adapter.send_model_picker(
                        chat_id=source.chat_id,
                        providers=providers,
                        current_model=current_model,
                        current_provider=current_provider,
                        session_key=session_key,
                        on_model_selected=_on_model_selected,
                        metadata=metadata,
                    )
                    if result.success:
                        return None  # 选择器已发送 —— 由 adapter 处理响应

            # 兜底：文本列表（用于不支持选择器的平台，或选择器失败时）
            provider_label = get_label(current_provider)
            lines = [t("gateway.model.current_label", model=current_model or "unknown", provider=provider_label), ""]

            try:
                # 把可能阻塞的 provider 列表查询从 event loop 卸载出去，避免
                # gateway 在缓存过期时因 HTTP 请求而卡死。见 #41289。
                providers = await asyncio.to_thread(
                    list_authenticated_providers,
                    current_provider=current_provider,
                    current_base_url=current_base_url,
                    current_model=current_model,
                    user_providers=user_provs,
                    custom_providers=custom_provs,
                    max_models=5,
                )
                for p in providers:
                    tag = t("gateway.model.current_tag") if p["is_current"] else ""
                    lines.append(f"**{p['name']}** `--provider {p['slug']}`{tag}:")
                    if p["models"]:
                        model_strs = ", ".join(f"`{m}`" for m in p["models"])
                        extra = t("gateway.model.more_models_suffix", count=p["total_models"] - len(p["models"])) if p["total_models"] > len(p["models"]) else ""
                        lines.append(f"  {model_strs}{extra}")
                    elif p.get("api_url"):
                        lines.append(f"  `{p['api_url']}`")
                    lines.append("")
            except Exception:
                pass

            lines.append(t("gateway.model.usage_switch_model"))
            lines.append(t("gateway.model.usage_switch_provider"))
            lines.append(t("gateway.model.usage_persist"))
            return "\n".join(lines)

        # 执行切换
        skew_error = _model_switch_skew_guard()
        if skew_error:
            return skew_error
        result = _switch_model(
            raw_input=model_input,
            current_provider=current_provider,
            current_model=current_model,
            current_base_url=current_base_url,
            current_api_key=current_api_key,
            is_global=persist_global,
            explicit_provider=explicit_provider,
            user_providers=user_provs,
            custom_providers=custom_provs,
        )

        if not result.success:
            return t("gateway.model.error_prefix", error=result.error_message)

        try:
            from hermes_cli.context_switch_guard import (
                enrich_model_switch_warnings_for_gateway,
            )

            enrich_model_switch_warnings_for_gateway(
                result,
                self,
                session_key=session_key,
                source=source,
                custom_providers=custom_provs,
                load_gateway_config=_load_gateway_config,
            )
        except Exception as exc:
            logger.debug("preflight-compression switch warning failed: %s", exc)

        async def _finish_switch() -> str:
            """应用解析后的切换结果（agent、session、config），并构建回复。"""
            # 如果存在缓存 agent，就就地更新它
            cached_entry = None
            _cache_lock = getattr(self, "_agent_cache_lock", None)
            _cache = getattr(self, "_agent_cache", None)
            if _cache_lock and _cache is not None:
                with _cache_lock:
                    cached_entry = _cache.get(session_key)

            if cached_entry and cached_entry[0] is not None:
                try:
                    cached_entry[0].switch_model(
                        new_model=result.new_model,
                        new_provider=result.target_provider,
                        api_key=result.api_key,
                        base_url=result.base_url,
                        api_mode=result.api_mode,
                    )
                except Exception as exc:
                    # 就地切换把 agent 回滚到了旧的可用 model/client，然后重新
                    # 抛出异常。中止提交：跳过 DB 持久化、session override、缓存
                    # 驱逐和 config 写入，使失败的切换成为 no-op，而不是让对话死掉
                    # （#50163）。如果没有这个提前 return，下一条消息会根据坏
                    # override 重建一个破损的 agent。
                    logger.warning("In-place model switch failed for cached agent: %s", exc)
                    return t(
                        "gateway.model.error_prefix",
                        error=(
                            f"Model switch to {result.new_model} failed ({exc}); "
                            f"staying on {current_model}."
                        ),
                    )

            # 把新模型持久化到 session DB，以便 dashboard 展示更新后的模型
            # （#34850）。
            _sess_db = getattr(self, "_session_db", None)
            if _sess_db is not None:
                try:
                    _sess_entry = self.session_store.get_or_create_session(source)
                    # 如果该 session 被自动重置过，消费掉该标记，使得下一条常规
                    # 消息的清理动作不会把刚才存的 model override 清掉
                    # （Closes #48031）。
                    if getattr(_sess_entry, "was_auto_reset", False):
                        _sess_entry.was_auto_reset = False
                    _sess_db.update_session_model(
                        _sess_entry.session_id, result.new_model
                    )
                except Exception as exc:
                    logger.debug(
                        "Failed to persist model switch to DB: %s", exc
                    )

            # 存储一条说明，前插到下一条用户消息之前，让模型知道这次切换
            # （避免在历史中间插入系统消息）。
            if not hasattr(self, "_pending_model_notes"):
                self._pending_model_notes = {}
            self._pending_model_notes[session_key] = (
                f"[Note: model was just switched from {current_model} to {result.new_model} "
                f"via {result.provider_label or result.target_provider}. "
                f"Adjust your self-identification accordingly.]"
            )

            # 存储 session override，使下次创建 agent 时使用新模型
            self._session_model_overrides[session_key] = {
                "model": result.new_model,
                "provider": result.target_provider,
                "api_key": result.api_key,
                "base_url": result.base_url,
                "api_mode": result.api_mode,
            }

            # 驱逐缓存 agent，使下一轮根据 override 创建新 agent，而不是依赖
            # 缓存签名不匹配来检测。
            self._evict_cached_agent(session_key)

            # 除非 --session 选择退出，否则持久化到 config（默认行为）
            if persist_global:
                try:
                    if config_path.exists():
                        with open(config_path, encoding="utf-8") as f:
                            cfg = yaml.safe_load(f) or {}
                    else:
                        cfg = {}
                    # 在修改前把标量/None 形式的 ``model:`` 强制转换为 dict ——
                    # 否则 ``cfg.setdefault("model", {})`` 会返回既有的标量，而
                    # 下一次赋值会抛出
                    # ``TypeError: 'str' object does not support item assignment``。
                    # 当 ``config.yaml`` 里写的是扁平字符串 ``model: <name>``，
                    # 而不是规范的嵌套 ``model: {default: ...}`` 时会复现此问题。
                    raw_model = cfg.get("model")
                    if isinstance(raw_model, dict):
                        model_cfg = raw_model
                    elif isinstance(raw_model, str) and raw_model.strip():
                        model_cfg = {"default": raw_model.strip()}
                        cfg["model"] = model_cfg
                    else:
                        model_cfg = {}
                        cfg["model"] = model_cfg
                    model_cfg["default"] = result.new_model
                    model_cfg["provider"] = result.target_provider
                    if result.base_url:
                        model_cfg["base_url"] = result.base_url
                    if str(result.target_provider or "").strip().lower() != "custom":
                        clear_model_endpoint_credentials(model_cfg)
                    from hermes_cli.config import save_config
                    save_config(cfg)
                except Exception as e:
                    logger.warning("Failed to persist model switch: %s", e)

            # 构建带完整元数据的确认消息
            provider_label = result.provider_label or result.target_provider
            lines = [t("gateway.model.switched", model=result.new_model)]
            lines.append(t("gateway.model.provider_label", provider=provider_label))

            # context：总是通过感知 provider 的解析链来获取，这样 Codex OAuth、
            # Copilot 以及 Nous 强制的上限会优先于原始的 models.dev 条目。
            mi = result.model_info
            from hermes_cli.model_switch import resolve_display_context_length
            _sw2_config_ctx = None
            try:
                _sw2_cfg = _load_gateway_config()
                _sw2_model_cfg = _sw2_cfg.get("model", {})
                if isinstance(_sw2_model_cfg, dict):
                    _sw2_raw = _sw2_model_cfg.get("context_length")
                    if _sw2_raw is not None:
                        _sw2_config_ctx = int(_sw2_raw)
            except Exception:
                pass
            ctx = resolve_display_context_length(
                result.new_model,
                result.target_provider,
                base_url=result.base_url or current_base_url or "",
                api_key=result.api_key or current_api_key or "",
                model_info=mi,
                custom_providers=custom_provs,
                config_context_length=_sw2_config_ctx,
            )
            if ctx:
                lines.append(t("gateway.model.context_label", tokens=f"{ctx:,}"))
            if mi:
                if mi.max_output:
                    lines.append(t("gateway.model.max_output_label", tokens=f"{mi.max_output:,}"))
                if mi.has_cost_data():
                    lines.append(t("gateway.model.cost_label", cost=mi.format_cost()))
                lines.append(t("gateway.model.capabilities_label", capabilities=mi.format_capabilities()))

            # 缓存提示
            cache_enabled = (
                (base_url_host_matches(result.base_url or "", "openrouter.ai") and "claude" in result.new_model.lower())
                or result.api_mode == "anthropic_messages"
            )
            if cache_enabled:
                lines.append(t("gateway.model.prompt_caching_enabled"))

            if result.warning_message:
                lines.append(t("gateway.model.warning_prefix", warning=result.warning_message))

            if persist_global:
                lines.append(t("gateway.model.saved_global"))
            else:
                lines.append(t("gateway.model.session_only_hint"))

            return "\n".join(lines)

        # 昂贵模型确认门控（键入 /model <name> 路径）。各种选择器（Telegram/
        # Discord 内联键盘、TUI、dashboard）已经通过各自的 UI 进行确认；这里
        # 覆盖直接文本命令，此前它会绕过该守卫。expensive_model_warning() 在
        # 缓存未命中时可能会访问 models.dev 或 /models endpoint，因此放到 event
        # loop 之外执行。
        _cost_warning = None
        try:
            from hermes_cli.model_cost_guard import expensive_model_warning

            _cost_warning = await asyncio.to_thread(
                expensive_model_warning,
                result.new_model,
                provider=result.target_provider,
                base_url=result.base_url or current_base_url or "",
                api_key=result.api_key or current_api_key or "",
                model_info=result.model_info,
            )
        except Exception:
            _cost_warning = None
        if _cost_warning is not None:
            async def _on_cost_confirm(choice: str) -> str:
                if choice == "cancel":
                    return (
                        f"🟡 Model switch cancelled. Current model unchanged "
                        f"({current_model or 'unknown'})."
                    )
                # "once" 与 "always" 都继续执行——cost guard 不提供持久化的
                # 永久退出（每次昂贵的切换都应是一次明确的决策）。
                return await _finish_switch()

            _p = self._typed_command_prefix_for(event.source.platform)
            return await self._request_slash_confirm(
                event=event,
                command="model",
                title="Expensive Model Warning",
                message=(
                    f"⚠️ **Expensive Model Warning**\n\n{_cost_warning.message}\n\n"
                    f"_Text fallback: reply `{_p}approve` to switch or `{_p}cancel` to keep "
                    "the current model._"
                ),
                handler=_on_cost_confirm,
            )

        return await _finish_switch()

    async def _handle_codex_runtime_command(self, event: MessageEvent) -> str:
        """在 gateway 中处理 /codex-runtime 命令。

        与 cli.py 中的 CLI 处理器具有相同的对外接口：
            /codex-runtime                  —— 显示当前状态
            /codex-runtime auto             —— Hermes 默认 runtime
            /codex-runtime codex_app_server —— codex 子进程 runtime
            /codex-runtime on / off         —— 同义词

        发生变更时，会驱逐该 session 对应的缓存 agent，使下一条消息创建一个
        携带新 api_mode 的全新 AIAgent（避免会话中途触发 prompt-cache 失效）。"""
        from hermes_cli import codex_runtime_switch as crs

        raw_args = event.get_command_args().strip() if event else ""
        new_value, errors = crs.parse_args(raw_args)
        if errors:
            return "❌ " + "\n❌ ".join(errors)

        # 通过与 /model 和 /yolo 相同的辅助函数加载并持久化
        try:
            from hermes_cli.config import load_config, save_config
        except Exception as exc:
            return f"❌ Could not load config: {exc}"
        cfg = load_config()

        result = crs.apply(
            cfg,
            new_value,
            persist_callback=(save_config if new_value is not None else None),
        )

        # 真正发生变更时，驱逐缓存 agent，使新的 runtime 在下一条消息立即生效，
        # 而不必等待缓存 TTL。
        if result.success and new_value is not None and result.requires_new_session:
            try:
                session_key = self._session_key_for_source(event.source)
                self._evict_cached_agent(session_key)
            except Exception:
                logger.debug("could not evict cached agent after codex-runtime change",
                             exc_info=True)

        prefix = "✓" if result.success else "✗"
        return f"{prefix} {result.message}"

    async def _handle_personality_command(self, event: MessageEvent) -> str:
        """处理 /personality 命令 —— 列出或设置一个 personality。"""
        from gateway.run import _hermes_home, _load_gateway_config
        from hermes_constants import display_hermes_home

        args = event.get_command_args().strip().lower()
        config_path = _hermes_home / 'config.yaml'

        try:
            config = _load_gateway_config()
            personalities = cfg_get(config, "agent", "personalities", default={})
        except Exception:
            config = {}
            personalities = {}

        if not personalities:
            return t("gateway.personality.none_configured", path=display_hermes_home())

        if not args:
            lines = [t("gateway.personality.header")]
            lines.append(t("gateway.personality.none_option"))
            for name, prompt in personalities.items():
                if isinstance(prompt, dict):
                    preview = prompt.get("description") or prompt.get("system_prompt", "")[:50]
                else:
                    preview = prompt[:50] + "..." if len(prompt) > 50 else prompt
                lines.append(t("gateway.personality.item", name=name, preview=preview))
            lines.append(t("gateway.personality.usage"))
            return "\n".join(lines)

        def _resolve_prompt(value):
            if isinstance(value, dict):
                parts = [value.get("system_prompt", "")]
                if value.get("tone"):
                    parts.append(f'Tone: {value["tone"]}')
                if value.get("style"):
                    parts.append(f'Style: {value["style"]}')
                return "\n".join(p for p in parts if p)
            return str(value)

        if args in {"none", "default", "neutral"}:
            try:
                if "agent" not in config or not isinstance(config.get("agent"), dict):
                    config["agent"] = {}
                config["agent"]["system_prompt"] = ""
                atomic_yaml_write(config_path, config)
            except Exception as e:
                return t("gateway.personality.save_failed", error=str(e))
            self._ephemeral_system_prompt = ""
            return t("gateway.personality.cleared")
        elif args in personalities:
            new_prompt = _resolve_prompt(personalities[args])

            # 写入 config.yaml，方式与 CLI 的 save_config_value 相同。
            try:
                if "agent" not in config or not isinstance(config.get("agent"), dict):
                    config["agent"] = {}
                config["agent"]["system_prompt"] = new_prompt
                atomic_yaml_write(config_path, config)
            except Exception as e:
                return t("gateway.personality.save_failed", error=str(e))

            # 同步更新内存值，使它在下一条消息立即生效。
            self._ephemeral_system_prompt = new_prompt

            return t("gateway.personality.set_to", name=args)

        available = "`none`, " + ", ".join(f"`{n}`" for n in personalities)
        return t("gateway.personality.unknown", name=args, available=available)

    async def _handle_retry_command(self, event: MessageEvent) -> str:
        """处理 /retry 命令 —— 重新发送上一条用户消息。"""
        source = event.source
        session_entry = self.session_store.get_or_create_session(source)
        history = self.session_store.load_transcript(session_entry.session_id)

        # 查找上一条用户消息
        last_user_msg = None
        last_user_idx = None
        for i in range(len(history) - 1, -1, -1):
            if history[i].get("role") == "user":
                last_user_msg = history[i].get("content", "")
                last_user_idx = i
                break

        if not last_user_msg:
            return t("gateway.retry.no_previous")

        # 将历史截断到上一条用户消息之前并持久化
        truncated = history[:last_user_idx]
        self.session_store.rewrite_transcript(session_entry.session_id, truncated)
        # 重置存储的 token 计数——transcript 已被截断
        session_entry.last_prompt_tokens = 0

        # 通过用旧消息构造一个伪造的文本事件来重新发送
        retry_event = MessageEvent(
            text=last_user_msg,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=event.raw_message,
            channel_prompt=event.channel_prompt,
        )
        
        # 交给正常的消息处理器处理
        return await self._handle_message(retry_event)

    async def _handle_goal_command(self, event: "MessageEvent") -> str:
        """处理 gateway 平台的 /goal 命令。

        子命令：``/goal`` / ``/goal status`` / ``/goal pause`` /
        ``/goal resume`` / ``/goal clear``。任何其他文本都会被当作新的 goal。

        设置新的 goal 会把 goal 文本作为下一回合排队，使 agent 立刻开始
        处理它——之后由回合后 continuation 钩子接管。
        """
        args = (event.get_command_args() or "").strip()
        lower = args.lower()

        mgr, session_entry = self._get_goal_manager_for_event(event)
        if mgr is None:
            return t("gateway.goal.unavailable")

        if not args or lower == "status":
            return mgr.status_line()

        # /goal show → 打印活动 goal 的完成契约
        if lower == "show":
            return f"{mgr.status_line()}\n{mgr.render_contract()}"

        if lower == "pause":
            state = mgr.pause(reason="user-paused")
            if state is None:
                return t("gateway.goal.no_goal_set")
            try:
                adapter = self.adapters.get(event.source.platform) if event.source else None
                _quick_key = self._session_key_for_source(event.source) if event.source else None
                if adapter and _quick_key:
                    self._clear_goal_pending_continuations(_quick_key, adapter)
            except Exception as exc:
                logger.debug("goal pause: pending continuation cleanup failed: %s", exc)
            return t("gateway.goal.paused", goal=state.goal)

        if lower == "resume":
            state = mgr.resume()
            if state is None:
                return t("gateway.goal.no_resume")
            return t("gateway.goal.resumed", goal=state.goal)

        if lower in {"clear", "stop", "done"}:
            had = mgr.has_goal()
            mgr.clear()
            try:
                adapter = self.adapters.get(event.source.platform) if event.source else None
                _quick_key = self._session_key_for_source(event.source) if event.source else None
                if adapter and _quick_key:
                    self._clear_goal_pending_continuations(_quick_key, adapter)
            except Exception as exc:
                logger.debug("goal clear: pending continuation cleanup failed: %s", exc)
            return t("gateway.goal_cleared") if had else t("gateway.no_active_goal")

        # /goal wait <pid> [reason] —— 将循环挂起在一个后台进程上。
        if lower == "wait" or lower.startswith("wait "):
            wait_arg = args[len("wait"):].strip()
            if not wait_arg:
                return "Usage: /goal wait <pid> [reason]"
            wtokens = wait_arg.split(None, 1)
            try:
                pid = int(wtokens[0])
            except ValueError:
                return "/goal wait: <pid> must be an integer process id."
            reason = wtokens[1].strip() if len(wtokens) > 1 else ""
            try:
                mgr.wait_on(pid, reason=reason)
            except (RuntimeError, ValueError) as exc:
                return f"/goal wait: {exc}"
            rtxt = f" ({reason})" if reason else ""
            return f"⏳ Goal parked on pid {pid}{rtxt}. Loop pauses until it exits."

        # /goal unwait —— 清除 wait 阻挡。
        if lower == "unwait":
            if mgr.stop_waiting():
                return "▶ Wait barrier cleared — goal loop resumes."
            return "No wait barrier set."

        # /goal draft <objective> → 先起草一个结构化的完成契约，再设置它。
        # 该 aux LLM 调用是同步的，放到 event loop 之外执行。
        draft_contract_obj = None
        if lower.startswith("draft"):
            objective = args[len("draft"):].strip()
            if not objective:
                return "Usage: /goal draft <objective in plain language>"
            try:
                import asyncio
                from hermes_cli.goals import draft_contract

                draft_contract_obj = await asyncio.get_running_loop().run_in_executor(
                    None, draft_contract, objective
                )
            except Exception as exc:
                logger.debug("goal draft failed: %s", exc)
                draft_contract_obj = None
            args = objective  # the goal text is the objective
            contract = draft_contract_obj
        else:
            # 内联的 `field: value` 行会被解析为完成契约；其余文字作为 goal 的
            # 主标题。纯自由文本的 goal（没有此类行）行为与之前完全一致。
            from hermes_cli.goals import parse_contract

            headline, parsed = parse_contract(args)
            args = headline or args
            contract = parsed if not parsed.is_empty() else None

        # 否则 —— 把剩余文本作为新的 goal。
        try:
            state = mgr.set(args, contract=contract)
        except ValueError as exc:
            return t("gateway.goal.invalid", error=str(exc))

        # 将 goal 文本作为第一回合立即排队，使 agent 开始取得进展。之后由
        # 回合后钩子接管。
        adapter = self.adapters.get(event.source.platform) if event.source else None
        _quick_key = self._session_key_for_source(event.source) if event.source else None
        if adapter and _quick_key:
            try:
                kickoff_event = MessageEvent(
                    text=state.goal,
                    message_type=MessageType.TEXT,
                    source=event.source,
                    message_id=event.message_id,
                    channel_prompt=event.channel_prompt,
                )
                self._enqueue_fifo(_quick_key, kickoff_event, adapter)
            except Exception as exc:
                logger.debug("goal kickoff enqueue failed: %s", exc)

        base = t("gateway.goal.set", budget=state.max_turns, goal=state.goal)
        if state.has_contract():
            return f"{base}\nCompletion contract:\n{state.contract.render_block()}"
        if lower.startswith("draft"):
            # 请求了起草，但 aux 模型无法生成契约。
            return f"{base}\n(Couldn't draft a contract — running as a free-form goal.)"
        return base

    async def _handle_subgoal_command(self, event: "MessageEvent") -> str:
        """处理 gateway 平台的 /subgoal（CLI 处理器的镜像）。

        subgoal 是在循环执行过程中追加到活动 goal 上的额外判据。它们修改的是
        在下一个回合边界读取的状态，因此在 agent 运行期间调用是安全的。
        """
        args = (event.get_command_args() or "").strip()
        mgr, _session_entry = self._get_goal_manager_for_event(event)
        if mgr is None:
            return t("gateway.goal.unavailable")
        if not mgr.has_goal():
            return "No active goal. Set one with /goal <text>."

        # 无参数 → 列出当前 subgoal。
        if not args:
            return f"{mgr.status_line()}\n{mgr.render_subgoals()}"

        tokens = args.split(None, 1)
        verb = tokens[0].lower()
        rest = tokens[1].strip() if len(tokens) > 1 else ""

        if verb == "remove":
            if not rest:
                return "Usage: /subgoal remove <n>"
            try:
                idx = int(rest.split()[0])
            except ValueError:
                return "/subgoal remove: <n> must be an integer (1-based index)."
            try:
                removed = mgr.remove_subgoal(idx)
            except (IndexError, RuntimeError) as exc:
                return f"/subgoal remove: {exc}"
            return f"✓ Removed subgoal {idx}: {removed}"

        if verb == "clear":
            try:
                prev = mgr.clear_subgoals()
            except RuntimeError as exc:
                return f"/subgoal clear: {exc}"
            if prev:
                return f"✓ Cleared {prev} subgoal{'s' if prev != 1 else ''}."
            return "No subgoals to clear."

        try:
            text = mgr.add_subgoal(args)
        except (ValueError, RuntimeError) as exc:
            return f"/subgoal: {exc}"
        idx = len(mgr.state.subgoals) if mgr.state else 0
        return f"✓ Added subgoal {idx}: {text}"

    async def _handle_undo_command(self, event: MessageEvent) -> str:
        """处理 /undo [N] —— 回退 N 个用户回合（默认 1），在磁盘上软删除被截断
        的行，并回显被回退的消息文本，便于用户复制/编辑后重新发送。

        镜像 CLI/TUI 的 /undo：被回退的行保留在 state.db 中（active=0）以便
        审计，并从再次提示和搜索中隐藏。缓存的 agent 会被驱逐，使下一条消息
        从截断后（仅 active）的 transcript 重建上下文——这是 gateway 中对 CLI
        的“就地历史手术 + 内存缓存失效”的等价做法。
        """
        source = event.source

        # 解析可选的回合数："/undo" → 1，"/undo 3" → 3。
        n = 1
        raw_args = event.get_command_args().strip()
        if raw_args:
            try:
                n = int(raw_args.split()[0])
            except (ValueError, IndexError):
                return t("gateway.undo.invalid_count", arg=raw_args.split()[0])
            if n < 1:
                n = 1

        session_entry = self.session_store.get_or_create_session(source)
        result = self.session_store.rewind_session(session_entry.session_id, n)

        if result is None:
            return t("gateway.undo.nothing")

        # 重置存储的 token 计数——transcript 已被截断。
        session_entry.last_prompt_tokens = 0
        # 驱逐缓存 agent，使下一轮从仅 active 的 transcript 重建，并让 memory
        # provider 刷新各自的 per-session 缓存。
        try:
            session_key = build_session_key(source)
            self._evict_cached_agent(session_key)
        except Exception as e:
            logger.debug("undo: cached-agent eviction skipped: %s", e)

        target_text = result["target_text"]
        preview = target_text[:200] + "..." if len(target_text) > 200 else target_text
        return t(
            "gateway.undo.removed",
            turns=result["turns_undone"],
            count=result["rewound_count"],
            preview=preview,
        )

    async def _handle_set_home_command(self, event: MessageEvent) -> str:
        """处理 /sethome 命令 —— 把当前 chat 设为该平台的 home channel。"""
        from gateway.run import _home_target_env_var, _home_thread_env_var
        source = event.source
        platform_name = source.platform.value if source.platform else "unknown"
        chat_id = source.chat_id
        chat_name = source.chat_name or chat_id

        env_key = _home_target_env_var(platform_name)
        thread_env_key = _home_thread_env_var(platform_name)
        thread_id = source.thread_id

        # 保存到 .env，以便在重启后保留
        try:
            from hermes_cli.config import save_env_value
            save_env_value(env_key, str(chat_id))
            # 让 thread/topic 路由保持显式，并在 /sethome 是在父 chat（而非
            # thread）中运行时清掉陈旧值。
            save_env_value(thread_env_key, str(thread_id or ""))
        except Exception as e:
            return t("gateway.set_home.save_failed", error=e)

        # 同时让运行中的 gateway config 保持同步。pre-restart 通知路径会在
        # 进程重新加载 env 之前读取 self.config。
        if source.platform:
            platform_config = self.config.platforms.setdefault(
                source.platform,
                PlatformConfig(enabled=True),
            )
            platform_config.home_channel = HomeChannel(
                platform=source.platform,
                chat_id=str(chat_id),
                name=chat_name,
                thread_id=str(thread_id) if thread_id else None,
            )

        return t("gateway.set_home.success", name=chat_name, chat_id=chat_id)

    async def _handle_voice_command(self, event: MessageEvent) -> str:
        """处理 /voice [on|off|tts|channel|leave|status] 命令。"""
        args = event.get_command_args().strip().lower()
        chat_id = event.source.chat_id
        platform = event.source.platform
        voice_key = self._voice_key(platform, chat_id)

        adapter = self.adapters.get(platform)

        if args in {"on", "enable"}:
            self._voice_mode[voice_key] = "voice_only"
            self._save_voice_modes()
            if adapter:
                self._set_adapter_auto_tts_enabled(adapter, chat_id, enabled=True)
            return t("gateway.voice.enabled_voice_only")
        elif args in {"off", "disable"}:
            self._voice_mode[voice_key] = "off"
            self._save_voice_modes()
            if adapter:
                self._set_adapter_auto_tts_disabled(adapter, chat_id, disabled=True)
            return t("gateway.voice.disabled_text")
        elif args == "tts":
            self._voice_mode[voice_key] = "all"
            self._save_voice_modes()
            if adapter:
                self._set_adapter_auto_tts_enabled(adapter, chat_id, enabled=True)
            return t("gateway.voice.tts_enabled")
        elif args in {"channel", "join"}:
            return await self._handle_voice_channel_join(event)
        elif args == "leave":
            return await self._handle_voice_channel_leave(event)
        elif args == "status":
            mode = self._voice_mode.get(voice_key, "off")
            labels = {
                "off": t("gateway.voice.label_off"),
                "voice_only": t("gateway.voice.label_voice_only"),
                "all": t("gateway.voice.label_all"),
            }
            # 如果已连接，则追加 voice channel 信息
            adapter = self.adapters.get(event.source.platform)
            guild_id = self._get_guild_id(event)
            if guild_id and hasattr(adapter, "get_voice_channel_info"):
                info = adapter.get_voice_channel_info(guild_id)
                if info:
                    lines = [
                        t("gateway.voice.status_mode", label=labels.get(mode, mode)),
                        t("gateway.voice.status_channel", channel=info['channel_name']),
                        t("gateway.voice.status_participants", count=info['member_count']),
                    ]
                    for m in info["members"]:
                        status = t("gateway.voice.speaking") if m.get("is_speaking") else ""
                        lines.append(t("gateway.voice.status_member", name=m['display_name'], status=status))
                    return "\n".join(lines)
            return t("gateway.voice.status_mode", label=labels.get(mode, mode))
        else:
            # 切换：off → on，on/all → off
            current = self._voice_mode.get(voice_key, "off")
            if current == "off":
                self._voice_mode[voice_key] = "voice_only"
                self._save_voice_modes()
                if adapter:
                    self._set_adapter_auto_tts_enabled(adapter, chat_id, enabled=True)
                toggle_line = t("gateway.voice.enabled_short")
            else:
                self._voice_mode[voice_key] = "off"
                self._save_voice_modes()
                if adapter:
                    self._set_adapter_auto_tts_disabled(adapter, chat_id, disabled=True)
                toggle_line = t("gateway.voice.disabled_short")
            # 单独的 /voice 仍然会切换状态，但会追加一段说明，让用户发现
            # on/off/tts/status 等子命令（在 Discord 上还包括实时的 voice-channel
            # join/leave）。切换结果先通过 {toggle} 占位符展示。
            supports_voice_channels = adapter is not None and hasattr(
                adapter, "join_voice_channel"
            )
            channels = (
                t("gateway.voice.help_channels") if supports_voice_channels else ""
            )
            return t("gateway.voice.help", toggle=toggle_line, channels=channels)

    async def _handle_rollback_command(self, event: MessageEvent) -> str:
        """处理 /rollback 命令 —— 列出或恢复文件系统检查点。"""
        from gateway.run import _hermes_home
        from tools.checkpoint_manager import CheckpointManager, format_checkpoint_list

        # 从 config.yaml 读取检查点配置
        cp_cfg = {}
        try:
            import yaml as _y
            _cfg_path = _hermes_home / "config.yaml"
            if _cfg_path.exists():
                with open(_cfg_path, encoding="utf-8") as _f:
                    _data = _y.safe_load(_f) or {}
                cp_cfg = _data.get("checkpoints", {})
                if isinstance(cp_cfg, bool):
                    cp_cfg = {"enabled": cp_cfg}
        except Exception:
            pass

        if not cp_cfg.get("enabled", False):
            return t("gateway.rollback.not_enabled")

        mgr = CheckpointManager(
            enabled=True,
            max_snapshots=cp_cfg.get("max_snapshots", 50),
            max_total_size_mb=cp_cfg.get("max_total_size_mb", 500),
            max_file_size_mb=cp_cfg.get("max_file_size_mb", 10),
        )

        cwd = os.getenv("TERMINAL_CWD", str(Path.home()))
        arg = event.get_command_args().strip()

        if not arg:
            checkpoints = mgr.list_checkpoints(cwd)
            return format_checkpoint_list(checkpoints, cwd)

        # 按序号或 hash 恢复
        checkpoints = mgr.list_checkpoints(cwd)
        if not checkpoints:
            return t("gateway.rollback.none_found", cwd=cwd)

        target_hash = None
        try:
            idx = int(arg) - 1
            if 0 <= idx < len(checkpoints):
                target_hash = checkpoints[idx]["hash"]
            else:
                return t("gateway.rollback.invalid_number", max=len(checkpoints))
        except ValueError:
            target_hash = arg

        result = mgr.restore(cwd, target_hash)
        if result["success"]:
            return t(
                "gateway.rollback.restored",
                hash=result["restored_to"],
                reason=result["reason"],
            )
        return t("gateway.rollback.restore_failed", error=result["error"])

    async def _handle_background_command(self, event: MessageEvent) -> str:
        """处理 /background <prompt> —— 在独立的后台 session 中运行一段 prompt。

        在后台线程中生成一个新的 AIAgent，并拥有自己的 session。完成后把结果
        发回同一个 chat，但不会修改当前活动 session 的对话历史。
        """
        prompt = event.get_command_args().strip()
        if not prompt:
            return t("gateway.background.usage")

        source = event.source
        task_id = f"bg_{datetime.now().strftime('%H%M%S')}_{os.urandom(3).hex()}"

        event_message_id = self._reply_anchor_for_event(event)

        # 转发图片/音频附件，使后台 agent 能看到它们。
        media_urls = list(event.media_urls) if event.media_urls else []
        media_types = list(event.media_types) if event.media_types else []

        # 触发即忘（fire-and-forget）的后台任务
        _task = asyncio.create_task(
            self._run_background_task(
                prompt,
                source,
                task_id,
                event_message_id=event_message_id,
                media_urls=media_urls,
                media_types=media_types,
            )
        )
        self._background_tasks.add(_task)
        _task.add_done_callback(self._background_tasks.discard)

        preview = prompt[:60] + ("..." if len(prompt) > 60 else "")
        return t("gateway.background.started", preview=preview, task_id=task_id)

    async def _handle_reasoning_command(self, event: MessageEvent) -> str:
        """处理 /reasoning 命令 —— 管理 reasoning 强度及展示开关。

        用法：
            /reasoning                       显示当前 effort 等级与展示状态
            /reasoning <level>               仅为本 session 设置 reasoning effort
            /reasoning <level> --global      将 reasoning effort 持久化到 config.yaml
            /reasoning reset                 清除本 session 的 reasoning override
            /reasoning show|on               在回复中展示模型 reasoning
            /reasoning hide|off              在回复中隐藏模型 reasoning
        """
        from gateway.run import _hermes_home, _platform_config_key
        import yaml

        raw_args = event.get_command_args().strip()
        args, persist_global = self._parse_reasoning_command_args(raw_args)
        config_path = _hermes_home / "config.yaml"
        # 在派生 override key 之前对 source 做归一化（Telegram DM topic 恢复），
        # 使存储与下一条消息回合读取的 key 一致——与 /model 相同的修复（#30479）。
        _reasoning_source = self._normalize_source_for_session_key(event.source)
        session_key = self._session_key_for_source(_reasoning_source)
        self._show_reasoning = self._load_show_reasoning()
        self._reasoning_config = self._resolve_session_reasoning_config(
            source=event.source,
            session_key=session_key,
        )

        def _save_config_key(key_path: str, value):
            """把以点号分隔的 key 保存到 config.yaml。"""
            try:
                user_config = {}
                if config_path.exists():
                    with open(config_path, encoding="utf-8") as f:
                        user_config = yaml.safe_load(f) or {}
                keys = key_path.split(".")
                current = user_config
                for k in keys[:-1]:
                    if k not in current or not isinstance(current[k], dict):
                        current[k] = {}
                    current = current[k]
                current[keys[-1]] = value
                atomic_yaml_write(config_path, user_config)
                return True
            except Exception as e:
                logger.error("Failed to save config key %s: %s", key_path, e)
                return False

        if not raw_args:
            # 展示当前状态
            rc = self._reasoning_config
            if rc is None:
                level = t("gateway.reasoning.level_default")
            elif rc.get("enabled") is False:
                level = t("gateway.reasoning.level_disabled")
            else:
                level = rc.get("effort", "medium")
            display_state = (
                t("gateway.reasoning.display_on")
                if self._show_reasoning
                else t("gateway.reasoning.display_off")
            )
            has_session_override = session_key in (getattr(self, "_session_reasoning_overrides", {}) or {})
            scope = (
                t("gateway.reasoning.scope_session")
                if has_session_override
                else t("gateway.reasoning.scope_global")
            )
            return t(
                "gateway.reasoning.status",
                level=level,
                scope=scope,
                display=display_state,
            )

        # 展示开关（按平台）
        platform_key = _platform_config_key(event.source.platform)
        if args in {"show", "on"}:
            self._show_reasoning = True
            _save_config_key(f"display.platforms.{platform_key}.show_reasoning", True)
            return t("gateway.reasoning.display_set_on", platform=platform_key)

        if args in {"hide", "off"}:
            self._show_reasoning = False
            _save_config_key(f"display.platforms.{platform_key}.show_reasoning", False)
            return t("gateway.reasoning.display_set_off", platform=platform_key)

        # effort 等级变更
        effort = args.strip()
        if effort == "reset":
            if persist_global:
                return t("gateway.reasoning.reset_global_unsupported")
            self._set_session_reasoning_override(session_key, None)
            self._reasoning_config = self._load_reasoning_config()
            self._evict_cached_agent(session_key)
            return t("gateway.reasoning.reset_done")
        if effort == "none":
            parsed = {"enabled": False}
        elif effort in {"minimal", "low", "medium", "high", "xhigh"}:
            parsed = {"enabled": True, "effort": effort}
        else:
            return t(
                "gateway.reasoning.unknown_arg",
                arg=effort or raw_args.lower(),
            )

        self._reasoning_config = parsed
        if persist_global:
            if _save_config_key("agent.reasoning_effort", effort):
                self._set_session_reasoning_override(session_key, None)
                self._evict_cached_agent(session_key)
                return t("gateway.reasoning.set_global", effort=effort)
            self._set_session_reasoning_override(session_key, parsed)
            self._evict_cached_agent(session_key)
            return t("gateway.reasoning.set_global_save_failed", effort=effort)

        self._set_session_reasoning_override(session_key, parsed)
        self._evict_cached_agent(session_key)
        return t("gateway.reasoning.set_session", effort=effort)

    async def _handle_memory_command(self, event: MessageEvent) -> str:
        """处理 /memory —— 审阅待写的 memory 条目，并切换审批门控。

        memory 条目足够小，可以在聊天气泡内逐条审阅，因此完整的
        pending/approve/reject/approval 流程在所有平台都可用。门控变更会持久化
        到 config.yaml，并驱逐缓存 agent，使新设置在下一条消息生效。
        """
        from gateway.run import _hermes_home
        from hermes_cli.write_approval_commands import handle_pending_subcommand
        from tools import write_approval as wa
        from tools.memory_tool import load_on_disk_store

        raw_args = event.get_command_args().strip()
        args = raw_args.split() if raw_args else []
        session_key = self._session_key_for_source(event.source)
        config_path = _hermes_home / "config.yaml"

        def _set_approval(enabled: bool):
            import yaml
            user_config = {}
            if config_path.exists():
                with open(config_path, encoding="utf-8") as f:
                    user_config = yaml.safe_load(f) or {}
            user_config.setdefault("memory", {})["write_approval"] = bool(enabled)
            atomic_yaml_write(config_path, user_config)
            # 新设置必须下一条消息生效 → 丢弃缓存 agent。
            self._evict_cached_agent(session_key)

        # 对一个全新从磁盘加载的 store 应用已批准的写入（gateway 没有长生命周期
        # 的 agent；该 store 持久化到同一个 MEMORY/USER.md）。
        # load_on_disk_store() 会尊重用户配置的字符上限。
        store = load_on_disk_store()

        out = handle_pending_subcommand(
            wa.MEMORY, args, memory_store=store, set_mode_fn=_set_approval,
        )
        if out is None:
            out = ("Unknown /memory subcommand. Use: pending, approve <id>, "
                   "reject <id>, approval <on|off>.")
        return out

    async def _handle_skills_command(self, event: MessageEvent) -> str:
        """在 gateway 上处理 /skills —— 仅用于待审 skill 写入审阅。

        完整的 skills 中心（搜索/浏览/安装）仍仅限 CLI；本处理器只覆盖写入审批
        的审阅界面（pending / approve / reject / diff / approval），使得从某个
        gateway session 中暂存的 skill 可以在同一个 session 中审阅。通过
        CommandDef 的 ``gateway_config_gate`` 由 ``skills.write_approval`` 门控；
        在门控关闭后若仍有暂存的写入，它也会给出响应（避免这些写入被遗留）。

        ``diff`` 输出会针对聊天气泡做截断——完整的 diff 位于
        ``~/.hermes/pending/skills/`` 下的 pending JSON 文件中。（注意这是写入
        审批用的 ``diff <id>``；CLI 另有一个不相关的 ``hermes skills diff <name>``
        命令，用于 diff 某个 bundled skill 与原版的差异。）
        """
        from gateway.run import _hermes_home
        from hermes_cli.write_approval_commands import handle_pending_subcommand
        from tools import write_approval as wa

        raw_args = event.get_command_args().strip()
        args = raw_args.split() if raw_args else []
        session_key = self._session_key_for_source(event.source)
        config_path = _hermes_home / "config.yaml"

        gate_on = wa.write_approval_enabled(wa.SKILLS)
        wants_toggle = bool(args) and args[0].lower() in {"approval", "mode"}
        if not gate_on and not wants_toggle and wa.pending_count(wa.SKILLS) == 0:
            return ("Skill write approval is off (skills.write_approval). "
                    "Enable it with /skills approval on, then review staged "
                    "writes here with /skills pending.")

        def _set_approval(enabled: bool):
            import yaml
            user_config = {}
            if config_path.exists():
                with open(config_path, encoding="utf-8") as f:
                    user_config = yaml.safe_load(f) or {}
            user_config.setdefault("skills", {})["write_approval"] = bool(enabled)
            atomic_yaml_write(config_path, user_config)
            # 新设置必须下一条消息生效 → 丢弃缓存 agent。
            self._evict_cached_agent(session_key)

        out = handle_pending_subcommand(
            wa.SKILLS, args, set_mode_fn=_set_approval,
        )
        if out is None:
            return ("Unknown /skills subcommand on this platform. Use: pending, "
                    "approve <id>, reject <id>, diff <id>, approval <on|off>. "
                    "(Search/install are CLI-only.)")

        # 聊天气泡装不下完整的 skill diff —— 做截断并指向真正的审阅界面。
        # （注意：`hermes skills diff <name>` 是一个*不同*的命令——它用来 diff
        # 某个 bundled skill 与其原版——所以我们指向 pending JSON 文件，而不是
        # 该命令。）
        if args and args[0].lower() == "diff" and len(out) > 3000:
            pending_id = args[1] if len(args) > 1 else "<id>"
            out = (out[:3000]
                   + "\n… (truncated — full diff in "
                     f"~/.hermes/pending/skills/{pending_id}.json)")
        return out

    async def _handle_fast_command(self, event: MessageEvent) -> str:
        """处理 /fast —— 在 gateway 聊天中镜像 CLI 的 Priority Processing 开关。"""
        from gateway.run import _hermes_home, _load_gateway_config, _resolve_gateway_model
        import yaml
        from hermes_cli.models import model_supports_fast_mode

        args = event.get_command_args().strip().lower()
        config_path = _hermes_home / "config.yaml"
        self._service_tier = self._load_service_tier()

        user_config = _load_gateway_config()
        model = _resolve_gateway_model(user_config)
        if not model_supports_fast_mode(model):
            return t("gateway.fast.not_supported")

        def _save_config_key(key_path: str, value):
            """把以点号分隔的 key 保存到 config.yaml。"""
            try:
                user_config = {}
                if config_path.exists():
                    with open(config_path, encoding="utf-8") as f:
                        user_config = yaml.safe_load(f) or {}
                keys = key_path.split(".")
                current = user_config
                for k in keys[:-1]:
                    if k not in current or not isinstance(current[k], dict):
                        current[k] = {}
                    current = current[k]
                current[keys[-1]] = value
                atomic_yaml_write(config_path, user_config)
                return True
            except Exception as e:
                logger.error("Failed to save config key %s: %s", key_path, e)
                return False

        if not args or args == "status":
            status = t("gateway.fast.status_fast") if self._service_tier == "priority" else t("gateway.fast.status_normal")
            return t("gateway.fast.status", mode=status)

        if args in {"fast", "on"}:
            self._service_tier = "priority"
            saved_value = "fast"
            label = t("gateway.fast.label_fast")
        elif args in {"normal", "off"}:
            self._service_tier = None
            saved_value = "normal"
            label = t("gateway.fast.label_normal")
        else:
            return t("gateway.fast.unknown_arg", arg=args)

        if _save_config_key("agent.service_tier", saved_value):
            return t("gateway.fast.saved", label=label)
        return t("gateway.fast.session_only", label=label)

    async def _handle_yolo_command(self, event: MessageEvent) -> Union[str, EphemeralReply]:
        """处理 /yolo —— 仅为本 session 切换危险命令审批绕过开关。"""
        from tools.approval import (
            disable_session_yolo,
            enable_session_yolo,
            is_session_yolo_enabled,
        )

        session_key = self._session_key_for_source(event.source)
        current = is_session_yolo_enabled(session_key)
        if current:
            disable_session_yolo(session_key)
            return EphemeralReply(t("gateway.yolo.disabled"))
        else:
            enable_session_yolo(session_key)
            return EphemeralReply(t("gateway.yolo.enabled"))

    async def _handle_verbose_command(self, event: MessageEvent) -> str:
        """处理 /verbose 命令 —— 循环切换 tool 进度展示模式。

        由 config.yaml 中的 ``display.tool_progress_command`` 门控（默认关闭）。
        启用后，会在 off → new → all → verbose → off 之间循环切换*当前平台*的
        tool 进度模式。该设置保存到
        ``display.platforms.<platform>.tool_progress``，使每个 channel 都可以
        独立拥有自己的详尽程度。
        """
        from gateway.run import _hermes_home, _load_gateway_config, _platform_config_key

        config_path = _hermes_home / "config.yaml"
        platform_key = _platform_config_key(event.source.platform)

        # --- 检查 config 门控 ------------------------------------------------
        try:
            user_config = _load_gateway_config()
            gate_enabled = is_truthy_value(
                cfg_get(user_config, "display", "tool_progress_command"),
                default=False,
            )
        except Exception:
            gate_enabled = False

        if not gate_enabled:
            return t("gateway.verbose.not_enabled")

        # --- 循环切换模式（按平台） ----------------------------------------
        cycle = ["off", "new", "all", "verbose"]
        descriptions = {
            "off": t("gateway.verbose.mode_off"),
            "new": t("gateway.verbose.mode_new"),
            "all": t("gateway.verbose.mode_all"),
            "verbose": t("gateway.verbose.mode_verbose"),
        }

        # 通过解析器读取该平台当前生效的模式
        from gateway.display_config import resolve_display_setting
        current = resolve_display_setting(user_config, platform_key, "tool_progress", "all")
        if current not in cycle:
            current = "all"
        idx = (cycle.index(current) + 1) % len(cycle)
        new_mode = cycle[idx]

        # 保存到 display.platforms.<platform>.tool_progress
        try:
            if "display" not in user_config or not isinstance(user_config.get("display"), dict):
                user_config["display"] = {}
            display = user_config["display"]
            if "platforms" not in display or not isinstance(display.get("platforms"), dict):
                display["platforms"] = {}
            if platform_key not in display["platforms"] or not isinstance(display["platforms"].get(platform_key), dict):
                display["platforms"][platform_key] = {}
            display["platforms"][platform_key]["tool_progress"] = new_mode
            atomic_yaml_write(config_path, user_config)
            return (
                f"{descriptions[new_mode]}\n"
                + t("gateway.verbose.saved_suffix", platform=platform_key)
            )
        except Exception as e:
            logger.warning("Failed to save tool_progress mode: %s", e)
            return f"{descriptions[new_mode]}\n" + t("gateway.verbose.save_failed", error=e)

    async def _handle_footer_command(self, event: MessageEvent) -> str:
        """处理 /footer 命令 —— 切换运行时元数据 footer 的开关。

        用法：
            /footer           → 切换开/关
            /footer on        → 全局启用
            /footer off       → 全局禁用
            /footer status    → 显示当前状态 + 字段

        footer 保存到 ``display.runtime_footer.enabled``（全局）。位于
        ``display.platforms.<platform>.runtime_footer`` 下的按平台覆盖会被
        尊重，但这里不会修改它——若要按平台控制，请直接编辑 config.yaml。
        """
        from gateway.run import _hermes_home, _load_gateway_config, _platform_config_key, _resolve_gateway_model
        from gateway.runtime_footer import resolve_footer_config

        config_path = _hermes_home / "config.yaml"
        platform_key = _platform_config_key(event.source.platform)

        # --- 解析参数 -------------------------------------------------
        arg = ""
        try:
            text = (getattr(event, "message", None) or "").strip()
            if text.startswith("/"):
                parts = text.split(None, 1)
                if len(parts) > 1:
                    arg = parts[1].strip().lower()
        except Exception:
            arg = ""

        # --- 加载 config ----------------------------------------------------
        try:
            user_config: dict = _load_gateway_config()
        except Exception as e:
            return t("gateway.config_read_failed", error=e)

        effective = resolve_footer_config(user_config, platform_key)

        if arg in {"status", "?"}:
            state = t("gateway.footer.state_on") if effective["enabled"] else t("gateway.footer.state_off")
            fields = ", ".join(effective.get("fields") or [])
            return t(
                "gateway.footer.status",
                state=state,
                fields=fields,
                platform=platform_key,
            )

        if arg in {"on", "enable", "true", "1"}:
            new_state = True
        elif arg in {"off", "disable", "false", "0"}:
            new_state = False
        elif arg == "":
            new_state = not effective["enabled"]
        else:
            return t("gateway.footer.usage")

        # --- 写入全局标志 ---------------------------------------------
        try:
            if not isinstance(user_config.get("display"), dict):
                user_config["display"] = {}
            display = user_config["display"]
            if not isinstance(display.get("runtime_footer"), dict):
                display["runtime_footer"] = {}
            display["runtime_footer"]["enabled"] = new_state
            atomic_yaml_write(config_path, user_config)
        except Exception as e:
            logger.warning("Failed to save runtime_footer.enabled: %s", e)
            return t("gateway.config_save_failed", error=e)

        state = t("gateway.footer.state_on") if new_state else t("gateway.footer.state_off")
        example = ""
        if new_state:
            # 如果可用，使用当前 agent 状态展示预览。
            from gateway.runtime_footer import format_runtime_footer
            preview = format_runtime_footer(
                model=_resolve_gateway_model(user_config) or None,
                context_tokens=0,
                context_length=None,
                fields=effective.get("fields") or ["model", "context_pct", "cwd"],
            )
            if preview:
                example = t("gateway.footer.example_line", preview=preview)
        return t("gateway.footer.saved", state=state, example=example)

    async def _handle_compress_command(self, event: MessageEvent) -> str:
        """处理 /compress 命令 —— 手动压缩对话上下文。

        接受可选的 focus 主题：``/compress <focus>`` 会引导摘要器保留与 *focus*
        相关的信息，同时对其他内容更激进地丢弃。

        也接受边界感知形式 ``/compress here [N]``：对除最近 ``N`` 次交流
        （默认 2）以外的内容进行摘要，被保留的部分原样不动。灵感来自 Claude
        Code 的 Rewind “Summarize up to here” 操作（v2.1.139，2026 年 5 月，
        https://code.claude.com/docs/en/whats-new/2026-w20）。
        """
        source = event.source
        session_entry = self.session_store.get_or_create_session(source)
        history = self.session_store.load_transcript(session_entry.session_id)

        if not history or len(history) < 4:
            return t("gateway.compress.not_enough")

        # 解析参数：要么是 focus 主题（全量压缩），要么是边界感知的
        # "here [N]" 形式（部分压缩）。
        from hermes_cli.partial_compress import (
            parse_partial_compress_args,
            rejoin_compressed_head_and_tail,
            split_history_for_partial_compress,
        )
        _raw_args = (event.get_command_args() or "").strip()
        partial, keep_last, focus_topic = parse_partial_compress_args(_raw_args)

        try:
            from run_agent import AIAgent
            from agent.manual_compression_feedback import summarize_manual_compression
            from agent.model_metadata import estimate_request_tokens_rough

            session_key = self._session_key_for_source(source)
            model, runtime_kwargs = self._resolve_session_agent_runtime(
                source=source,
                session_key=session_key,
            )
            if not runtime_kwargs.get("api_key"):
                return t("gateway.compress.no_provider")

            msgs = [
                {"role": m.get("role"), "content": m.get("content")}
                for m in history
                if m.get("role") in {"user", "assistant"} and m.get("content")
            ]

            # 边界感知切分：只对 head 摘要；最近 `keep_last` 次交流原样保留。
            # 切分会把 tail 对齐到一个 user 回合的起点，使重新拼接后的
            # transcript 保持合法的角色交替。
            tail: list = []
            head = msgs
            if partial:
                head, tail = split_history_for_partial_compress(msgs, keep_last)
                if not tail:
                    # 退化切分 —— 回退到全量压缩。
                    partial = False
                    head = msgs

            tmp_agent = AIAgent(
                **runtime_kwargs,
                model=model,
                max_iterations=4,
                quiet_mode=True,
                skip_memory=True,
                enabled_toolsets=["memory"],
                session_id=session_entry.session_id,
            )
            try:
                tmp_agent._print_fn = lambda *a, **kw: None

                # 估算时包含 system prompt + 工具 schema，使该数值反映真实的
                # 请求压力，而不是仅按 transcript 得到的低估（#6217）。必须在
                # tmp_agent 构建好之后计算，以便 _cached_system_prompt/tools 已被
                # 填充。
                _sys_prompt = getattr(tmp_agent, "_cached_system_prompt", "") or ""
                _tools = getattr(tmp_agent, "tools", None) or None
                approx_tokens = estimate_request_tokens_rough(
                    msgs, system_prompt=_sys_prompt, tools=_tools
                )

                compressor = tmp_agent.context_compressor
                if not compressor.has_content_to_compress(head):
                    return t("gateway.compress.nothing_to_do")

                loop = asyncio.get_running_loop()
                compressed, _ = await loop.run_in_executor(
                    None,
                    lambda: tmp_agent._compress_context(head, "", approx_tokens=approx_tokens, focus_topic=focus_topic, force=True)
                )

                # 在压缩后的 head 之后重新追加原样 tail，并在拼接处防范非法的
                # 角色相邻。
                if partial and tail:
                    compressed = rejoin_compressed_head_and_tail(compressed, tail)

                # _compress_context 要么执行了 rotation（legacy 模式：结束旧
                # session，创建一个 continuation id——把压缩后的消息写入新 session，
                # 使原 session 仍可搜索），要么就地压缩（compression.in_place /
                # #38763：id 不变，transcript 替换为压缩后的集合）。
                new_session_id = tmp_agent.session_id
                rotated = new_session_id != session_entry.session_id
                _in_place = bool(getattr(tmp_agent, "compression_in_place", False))
                if rotated:
                    session_entry.session_id = new_session_id
                    self.session_store._save()
                    self._sync_telegram_topic_binding(
                        source, session_entry, reason="compress-command",
                    )

                # 当 rotation 产生了新 id，或就地压缩成功时，才重写 transcript。
                # 这里要防范的是第三种情形：_compress_context 既没能 rotation，
                # 也不是就地模式（例如 legacy 模式但 _session_db 不可用 / DB 拆分
                # 抛了异常）——此时 session_id 不变是*失败*导致的，而
                # rewrite_transcript() 会删除原始消息并仅替换为压缩摘要
                # （永久数据丢失 #44794、#39704）。就地模式下 id 不变是*成功*，
                # 因此重写正好正确（并且当临时 /compress agent 自身没有
                # _session_db 时，这次写入就是持久化写入）。
                if rotated or _in_place:
                    self.session_store.rewrite_transcript(
                        new_session_id, compressed
                    )
                else:
                    logger.warning(
                        "Manual /compress: session rotation did not occur "
                        "(session_id unchanged) and in-place mode is off — "
                        "preserving original transcript instead of overwriting "
                        "it (#44794)."
                    )
                # 重置存储的 token 计数——transcript 已变化，旧值已过期
                self.session_store.update_session(
                    session_entry.session_key, last_prompt_tokens=0
                )
                new_tokens = estimate_request_tokens_rough(
                    compressed, system_prompt=_sys_prompt, tools=_tools
                )
                summary = summarize_manual_compression(
                    msgs,
                    compressed,
                    approx_tokens,
                    new_tokens,
                )
                # 检测摘要生成失败，以便即使是在手动 /compress 路径上，也能向
                # 用户给出可见警告（否则失败只会被静默记录日志）。
                # _last_compress_aborted 表示 aux LLM 未返回可用摘要，且
                # compressor 原样保留了消息（既不丢弃，也不插入占位符）。上面
                # 已传 force=True，因此会跳过任何活跃的冷却。
                _summary_aborted = bool(getattr(compressor, "_last_compress_aborted", False))
                _summary_err = getattr(compressor, "_last_summary_error", None)
                # 另外检查：用户配置的 aux 模型是否失败并改用主模型恢复？
                # 把它作为一条提示信息呈现，方便用户修复配置。
                _aux_fail_model = getattr(compressor, "_last_aux_model_failure_model", None)
                _aux_fail_err = getattr(compressor, "_last_aux_model_failure_error", None)
            finally:
                # 驱逐缓存 agent，使下一轮根据当前文件（SOUL.md、memory 等）
                # 重建 system prompt。
                self._evict_cached_agent(session_key)
                self._cleanup_agent_resources(tmp_agent)
            lines = [f"🗜️ {summary['headline']}"]
            if focus_topic:
                lines.append(t("gateway.compress.focus_line", topic=focus_topic))
            lines.append(summary["token_line"])
            if summary["note"]:
                lines.append(summary["note"])
            if _summary_aborted:
                lines.append(
                    t(
                        "gateway.compress.aborted",
                        error=(_summary_err or "unknown error"),
                    )
                )
            elif _aux_fail_model:
                lines.append(
                    t(
                        "gateway.compress.aux_failed",
                        model=_aux_fail_model,
                        error=(_aux_fail_err or "unknown error"),
                    )
                )
            return "\n".join(lines)
        except Exception as e:
            logger.warning("Manual compress failed: %s", e)
            return t("gateway.compress.failed", error=e)

    async def _handle_topic_command(self, event: MessageEvent, args: str = "") -> str:
        """为 Telegram DM 中由用户管理的 topic session 处理 /topic。"""
        source = event.source
        if source.platform != Platform.TELEGRAM or source.chat_type != "dm":
            return t("gateway.topic.not_telegram_dm")
        if not self._session_db:
            from hermes_state import format_session_db_unavailable
            return format_session_db_unavailable(prefix=t("gateway.shared.session_db_unavailable_prefix"))

        # 授权检查：/topic 会激活多 session 模式并改动 SQLite 侧表。未授权的
        # 发送者（不在 allowlist 中）必须不能执行此操作。gateway 路由在到达此处
        # 之前已经对消息做过授权，这里是纵深防御。
        auth_fn = getattr(self, "_is_user_authorized", None)
        if callable(auth_fn):
            try:
                if not auth_fn(source):
                    return t("gateway.topic.unauthorized")
            except Exception:
                logger.debug("Topic auth check failed", exc_info=True)

        args = event.get_command_args().strip()

        # /topic help —— 不离开 bot 即可查看内联用法。
        if args.lower() in {"help", "?", "-h", "--help"}:
            return self._telegram_topic_help_text()

        # /topic off —— 干净的关闭路径，用户无需手动改 DB。
        if args.lower() in {"off", "disable", "stop"}:
            return self._disable_telegram_topic_mode_for_chat(source)

        if args:
            if not source.thread_id:
                return t("gateway.topic.restore_needs_topic")
            return await self._restore_telegram_topic_session(event, args)

        capabilities = await self._get_telegram_topic_capabilities(source)
        if capabilities.get("checked"):
            if capabilities.get("has_topics_enabled") is False:
                # 对 BotFather 截图做去抖：在 threads 仍处于禁用状态时，不要
                # 每次 /topic 都重新发送。
                if self._should_send_telegram_capability_hint(source):
                    await self._send_telegram_topic_setup_image(source)
                return t("gateway.topic.topics_disabled")
            if capabilities.get("allows_users_to_create_topics") is False:
                if self._should_send_telegram_capability_hint(source):
                    await self._send_telegram_topic_setup_image(source)
                return t("gateway.topic.topics_user_disallowed")

        try:
            self._session_db.enable_telegram_topic_mode(
                chat_id=str(source.chat_id),
                user_id=str(source.user_id),
                has_topics_enabled=capabilities.get("has_topics_enabled"),
                allows_users_to_create_topics=capabilities.get("allows_users_to_create_topics"),
            )
        except Exception as exc:
            logger.exception("Failed to enable Telegram topic mode")
            return t("gateway.topic.enable_failed", error=exc)

        if not source.thread_id:
            await self._ensure_telegram_system_topic(source)

        if source.thread_id:
            try:
                binding = self._session_db.get_telegram_topic_binding(
                    chat_id=str(source.chat_id),
                    thread_id=str(source.thread_id),
                )
            except Exception:
                logger.debug("Failed to read Telegram topic binding", exc_info=True)
                binding = None
            if binding:
                session_id = str(binding.get("session_id") or "")
                title = None
                try:
                    title = self._session_db.get_session_title(session_id)
                except Exception:
                    title = None
                session_label = title or t("gateway.topic.untitled_session")
                return t(
                    "gateway.topic.bound_status",
                    label=session_label,
                    session_id=session_id,
                )
            return t("gateway.topic.thread_ready")

        return self._telegram_topic_root_status_message(source)

    async def _handle_title_command(self, event: MessageEvent) -> str:
        """处理 /title 命令 —— 设置或显示当前 session 的标题。"""
        source = event.source
        session_entry = self.session_store.get_or_create_session(source)
        session_id = session_entry.session_id

        if not self._session_db:
            from hermes_state import format_session_db_unavailable
            return format_session_db_unavailable(prefix=t("gateway.shared.session_db_unavailable_prefix"))

        # 确保 session 在 SQLite DB 中存在（如果是新 session 中的第一条命令，
        # 它可能仅存在于 session_store 中）
        existing_title = self._session_db.get_session_title(session_id)
        if existing_title is None:
            # DB 中尚不存在该 session —— 创建它
            try:
                self._session_db.create_session(
                    session_id=session_id,
                    source=source.platform.value if source.platform else "unknown",
                    user_id=source.user_id,
                )
            except Exception:
                pass  # session 可能已存在，忽略错误

        title_arg = event.get_command_args().strip()
        if title_arg:
            # 在设置之前对标题做清洗
            try:
                sanitized = self._session_db.sanitize_title(title_arg)
            except ValueError as e:
                return t("gateway.shared.warn_passthrough", error=e)
            if not sanitized:
                return t("gateway.title.empty_after_clean")
            # 设置标题
            try:
                if self._session_db.set_session_title(session_id, sanitized):
                    # 把用户选择的标题也同步到可见的 Telegram forum topic 名称。
                    # 自动生成的标题已经会重命名 topic；如果没有这段，/title 只
                    # 更新了 DB 中的标题，而 topic 仍保留自动分配的名称。在非
                    # Telegram topic lane 下以及禁用自动重命名时均为 no-op。
                    schedule_rename = getattr(
                        self, "_schedule_telegram_topic_title_rename", None
                    )
                    if callable(schedule_rename):
                        try:
                            schedule_rename(source, session_id, sanitized)
                        except Exception:
                            logger.debug(
                                "Failed to rename Telegram topic from /title",
                                exc_info=True,
                            )
                    return t("gateway.title.set_to", title=sanitized)
                else:
                    return t("gateway.title.not_found")
            except ValueError as e:
                return t("gateway.shared.warn_passthrough", error=e)
        else:
            # 显示当前标题和 session ID
            title = self._session_db.get_session_title(session_id)
            if title:
                return t("gateway.title.current_with_title", session_id=session_id, title=title)
            else:
                return t("gateway.title.current_no_title", session_id=session_id)

    async def _handle_resume_command(self, event: MessageEvent) -> str:
        """处理 /resume 命令 —— 列出或切换到某个之前的 session。"""
        if not self._session_db:
            from hermes_state import format_session_db_unavailable
            return format_session_db_unavailable(prefix=t("gateway.shared.session_db_unavailable_prefix"))

        source = event.source
        session_key = self._session_key_for_source(source)
        raw_args = event.get_command_args().strip()
        try:
            parts = shlex.split(raw_args)
        except ValueError as exc:
            return t("gateway.resume.parse_error", error=exc)
        allow_all = "--all" in parts
        allow_cross_room = "--cross-room" in parts
        name = " ".join(p for p in parts if p not in {"--all", "--cross-room"}).strip()

        # 去除用户可能按字面从用法提示中输入的外层括号/引号
        # （例如 ``/resume <abc123>``）。与 CLI 行为保持一致。
        if len(name) >= 2 and (
            (name[0] == "<" and name[-1] == ">")
            or (name[0] == "[" and name[-1] == "]")
            or (name[0] == '"' and name[-1] == '"')
            or (name[0] == "'" and name[-1] == "'")
        ):
            name = name[1:-1].strip()

        def _list_titled_sessions() -> list[dict]:
            user_source = source.platform.value if source.platform else None
            sessions = self._session_db.list_sessions_rich(source=user_source, limit=10)
            return [s for s in sessions if s.get("title")][:10]

        if not name:
            # 列出该 user/platform 最近的带标题 session
            try:
                titled = _list_titled_sessions()
                if source.platform == Platform.MATRIX and not allow_all:
                    scoped = []
                    for s in titled:
                        origin = self._gateway_session_origin_for_id(str(s.get("id") or ""))
                        if self._same_matrix_room(source, origin):
                            scoped.append(s)
                    titled = scoped
                if not titled:
                    if source.platform == Platform.MATRIX and not allow_all:
                        return t("gateway.resume.matrix_no_named_sessions")
                    return t("gateway.resume.no_named_sessions")
                lines = [t("gateway.resume.list_header")]
                for idx, s in enumerate(titled[:10], start=1):
                    title = s["title"]
                    if source.platform == Platform.MATRIX and allow_all:
                        origin = self._gateway_session_origin_for_id(str(s.get("id") or ""))
                        if origin:
                            title = f"{title} — {origin.chat_name or origin.chat_id}"
                    preview = s.get("preview", "")[:40]
                    preview_part = t("gateway.resume.list_preview_suffix", preview=preview) if preview else ""
                    lines.append(t("gateway.resume.list_item_numbered", index=idx, title=title, preview_part=preview_part))
                lines.append(t("gateway.resume.list_footer_numbered"))
                return "\n".join(lines)
            except Exception as e:
                logger.debug("Failed to list titled sessions: %s", e)
                return t("gateway.resume.list_failed", error=e)

        # 把编号选择或标题解析为 session ID。
        if name.isdigit():
            try:
                titled = _list_titled_sessions()
                if source.platform == Platform.MATRIX and not allow_all:
                    scoped = []
                    for s in titled:
                        origin = self._gateway_session_origin_for_id(str(s.get("id") or ""))
                        if self._same_matrix_room(source, origin):
                            scoped.append(s)
                    titled = scoped
            except Exception as e:
                logger.debug("Failed to list titled sessions for numeric resume: %s", e)
                return t("gateway.resume.list_failed", error=e)
            index = int(name)
            if index < 1 or index > len(titled):
                return t("gateway.resume.out_of_range", index=index)
            target = titled[index - 1]
            target_id = target.get("id")
            name = target.get("title") or name
        else:
            # 先尝试直接按 session ID 查找（这样 `/resume <session_id>` 在
            # gateway 中也能工作，而不只是 `/resume <title>`）。
            session = self._session_db.get_session(name)
            if session:
                target_id = session["id"]
            else:
                target_id = self._session_db.resolve_session_by_title(name)
        if not target_id:
            return t("gateway.resume.not_found", name=name)
        # 压缩会创建承载活动 transcript 的子 continuation。沿这条链查找，使
        # gateway 的 /resume 与 CLI 行为一致（#15000）。
        try:
            target_id = self._session_db.resolve_resume_session_id(target_id)
        except Exception as e:
            logger.debug("Failed to resolve resume continuation for %s: %s", target_id, e)

        if source.platform == Platform.MATRIX:
            target_origin = self._gateway_session_origin_for_id(target_id)
            if not self._same_matrix_room(source, target_origin) and not allow_cross_room:
                if target_origin is None:
                    return t("gateway.resume.matrix_blocked_no_origin", name=name)
                return t(
                    "gateway.resume.matrix_blocked_other_room",
                    room=target_origin.chat_name or target_origin.chat_id,
                    name=name,
                )

        # 检查是否已经处于该 session
        current_entry = self.session_store.get_or_create_session(source)
        if current_entry.session_id == target_id:
            return t("gateway.resume.already_on", name=name)

        # 清除该 session key 下任何运行中的 agent
        self._release_running_agent_state(session_key)

        # 把 session entry 切换到指向旧 session
        new_entry = self.session_store.switch_session(session_key, target_id)
        if not new_entry:
            return t("gateway.resume.switch_failed")
        self._clear_session_boundary_security_state(session_key)

        # 驱逐该 session 的任何缓存 agent，使下一条消息用正确的 session_id
        # 端到端地重建——与 /branch 和 /reset 一致。否则，缓存中的 AIAgent
        # （以及它的 memory provider，在 initialize() 时缓存了 `_session_id`）
        # 会继续写入错误 session 的记录。见 #6672。
        self._evict_cached_agent(session_key)

        # 获取标题以用于确认
        title = self._session_db.get_session_title(target_id) or name

        # 统计消息条数以提供上下文
        history = self.session_store.load_transcript(target_id)
        msg_count = len([m for m in history if m.get("role") == "user"]) if history else 0
        msg_part = f" ({msg_count} message{'s' if msg_count != 1 else ''})" if msg_count else ""

        if source.platform == Platform.MATRIX and allow_cross_room:
            return t(
                "gateway.resume.matrix_cross_room_success",
                title=title,
                room=source.chat_name or source.chat_id,
                msg_part=msg_part,
            )
        if not msg_count:
            return t("gateway.resume.resumed_no_count", title=title)
        if msg_count == 1:
            return t("gateway.resume.resumed_one", title=title, count=msg_count)
        return t("gateway.resume.resumed_many", title=title, count=msg_count)

    async def _handle_sessions_command(self, event: MessageEvent) -> str:
        """处理 /sessions —— 列出 gateway 聊天中之前的 session。"""
        if not self._session_db:
            from hermes_state import format_session_db_unavailable
            return format_session_db_unavailable(prefix=t("gateway.shared.session_db_unavailable_prefix"))

        from hermes_cli.session_listing import (
            format_gateway_session_listing,
            parse_session_listing_args,
            query_session_listing,
        )

        source = event.source
        raw_args = event.get_command_args().strip()
        try:
            include_all, include_unnamed, target = parse_session_listing_args(raw_args)
        except ValueError as exc:
            return t("gateway.resume.parse_error", error=exc)

        if target:
            resume_event = dataclasses.replace(event, text=f"/resume {target}")
            return await self._handle_resume_command(resume_event)

        current_entry = self.session_store.get_or_create_session(source)
        rows = query_session_listing(
            self._session_db,
            source=source.platform.value if source.platform else None,
            current_session_id=current_entry.session_id,
            include_all_sources=include_all,
            include_unnamed=include_unnamed,
            limit=10,
            exclude_sources=["tool"],
        )
        if source.platform == Platform.MATRIX and not include_all:
            rows = [
                row for row in rows
                if self._same_matrix_room(
                    source, self._gateway_session_origin_for_id(str(row.get("id") or ""))
                )
            ]
        return format_gateway_session_listing(
            rows,
            include_source=include_all,
            title="Sessions" if include_unnamed else "Named Sessions",
        )

    async def _handle_branch_command(self, event: MessageEvent) -> str:
        """处理 /branch [name] —— 把当前 session 分叉为一份新的独立副本。

        把对话历史复制到一个新的 session，使用户可以在不丢失原始会话的前提下
        探索另一种方案。灵感来自 Claude Code 的 /branch 命令。
        """
        import uuid as _uuid

        if not self._session_db:
            from hermes_state import format_session_db_unavailable
            return format_session_db_unavailable(prefix=t("gateway.shared.session_db_unavailable_prefix"))

        source = event.source
        session_key = self._session_key_for_source(source)

        # 加载当前 session 及其 transcript
        current_entry = self.session_store.get_or_create_session(source)
        history = self.session_store.load_transcript(current_entry.session_id)
        if not history:
            return t("gateway.branch.no_conversation")

        branch_name = event.get_command_args().strip()

        # 生成新的 session ID
        from datetime import datetime as _dt
        now = _dt.now()
        timestamp_str = now.strftime("%Y%m%d_%H%M%S")
        short_uuid = _uuid.uuid4().hex[:6]
        new_session_id = f"{timestamp_str}_{short_uuid}"

        # 决定分支标题
        if branch_name:
            branch_title = branch_name
        else:
            current_title = self._session_db.get_session_title(current_entry.session_id)
            base = current_title or "branch"
            branch_title = self._session_db.get_next_title_in_lineage(base)

        parent_session_id = current_entry.session_id

        # 创建带 parent 链接的新 session。
        # 在 model_config 中持久化一个稳定的 ``_branched_from`` 标记，使得
        # list_sessions_rich() 即使在 parent 被重新打开并以不同的 end_reason
        # 再次结束后（例如 tui_shutdown 覆盖了 'branched'），仍能在 /resume 和
        # /sessions 中保留该分支可见。
        try:
            self._session_db.create_session(
                session_id=new_session_id,
                source=source.platform.value if source.platform else "gateway",
                model=(self.config.get("model", {}) or {}).get("default") if isinstance(self.config, dict) else None,
                model_config={"_branched_from": parent_session_id},
                parent_session_id=parent_session_id,
            )
        except Exception as e:
            logger.error("Failed to create branch session: %s", e)
            return t("gateway.branch.create_failed", error=e)

        # 把对话历史复制到新 session
        for msg in history:
            try:
                self._session_db.append_message(
                    session_id=new_session_id,
                    role=msg.get("role", "user"),
                    content=msg.get("content"),
                    tool_name=msg.get("tool_name") or msg.get("name"),
                    tool_calls=msg.get("tool_calls"),
                    tool_call_id=msg.get("tool_call_id"),
                    finish_reason=msg.get("finish_reason"),
                    reasoning=msg.get("reasoning"),
                    reasoning_content=msg.get("reasoning_content"),
                    reasoning_details=msg.get("reasoning_details"),
                    codex_reasoning_items=msg.get("codex_reasoning_items"),
                    codex_message_items=msg.get("codex_message_items"),
                )
            except Exception:
                pass  # 尽力而为的复制

        # 设置标题
        try:
            self._session_db.set_session_title(new_session_id, branch_title)
        except Exception:
            pass

        # 把 session store 中的 entry 切换到新 session
        new_entry = self.session_store.switch_session(session_key, new_session_id)
        if not new_entry:
            return t("gateway.branch.switch_failed")
        self._clear_session_boundary_security_state(session_key)

        # 驱逐该 session 的任何缓存 agent
        self._evict_cached_agent(session_key)

        msg_count = len([m for m in history if m.get("role") == "user"])
        key = "gateway.branch.branched_one" if msg_count == 1 else "gateway.branch.branched_many"
        return t(key, title=branch_title, count=msg_count, parent=parent_session_id, new=new_session_id)

    async def _handle_credits_command(self, event: MessageEvent) -> str:
        """处理 /credits —— 显示 Nous 余额和充值入口。

        渲染余额区块 + 身份行 + 一个可点击的充值 URL，打开门户的账单页面并弹出
        模态框。终端不会确认、轮询或追踪支付（计费阶段 2a）——结账在浏览器中
        完成，下一次 /credits 会显示新余额。可点击 URL 就是入口：它在所有平台
        都能用（支持按钮的，或纯文本的 SMS/email 等）。在 event loop 之外获取；
        失败时 fail-open。
        """
        from agent.account_usage import build_credits_view

        try:
            view = await asyncio.to_thread(build_credits_view, markdown=True)
        except Exception:
            view = None

        if view is None or not view.logged_in:
            return t("gateway.credits.not_logged_in")

        lines: list[str] = ["💳 **Nous credits**"]
        for line in view.balance_lines:
            if line.lstrip().startswith("📈"):
                continue  # 去掉 helper 自带的标题；我们打印自己的
            lines.append(line)
        if view.identity_line:
            lines.append("")
            lines.append(view.identity_line)
        if view.topup_url:
            lines.append("")
            lines.append(f"Top up: {view.topup_url}")
            lines.append("Complete your top-up in the browser — credits will appear in /credits shortly.")
        return "\n".join(lines)

    async def _handle_usage_command(self, event: MessageEvent) -> str:
        """处理 /usage 命令 —— 显示当前 session 的 token 用量。

        同时检查 _running_agents（回合进行中）和 _agent_cache（两轮之间），
        这样无论用户何时询问，都能拿到限流、成本估算和详细的 token 明细，
        而不只是 agent 运行时才能看到。
        """
        from gateway.run import _AGENT_PENDING_SENTINEL
        source = event.source
        session_key = self._session_key_for_source(source)

        # 先尝试运行中的 agent（回合进行中），再尝试缓存 agent（两轮之间）
        agent = self._running_agents.get(session_key)
        if not agent or agent is _AGENT_PENDING_SENTINEL:
            _cache_lock = getattr(self, "_agent_cache_lock", None)
            _cache = getattr(self, "_agent_cache", None)
            if _cache_lock and _cache is not None:
                with _cache_lock:
                    cached = _cache.get(session_key)
                    if cached:
                        agent = cached[0]

        # 为 account-usage 拉取解析 provider/base_url/api_key。
        # 优先使用活动 agent；否则回退到 SessionDB 行上持久化的账单数据，使
        # `/usage` 在没有 agent 常驻的两轮之间也能返回账户信息。
        provider = getattr(agent, "provider", None) if agent and agent is not _AGENT_PENDING_SENTINEL else None
        base_url = getattr(agent, "base_url", None) if agent and agent is not _AGENT_PENDING_SENTINEL else None
        api_key = getattr(agent, "api_key", None) if agent and agent is not _AGENT_PENDING_SENTINEL else None
        if not provider and getattr(self, "_session_db", None) is not None:
            try:
                _entry_for_billing = self.session_store.get_or_create_session(source)
                persisted = self._session_db.get_session(_entry_for_billing.session_id) or {}
            except Exception:
                persisted = {}
            provider = provider or persisted.get("billing_provider")
            base_url = base_url or persisted.get("billing_base_url")

        # 在 event loop 之外拉取 account usage，避免慢速 provider API 阻塞
        # gateway。失败是非致命的——account_lines 保持为 []。
        account_lines: list[str] = []
        credits_lines: list[str] = []
        if provider:
            try:
                account_snapshot = await asyncio.to_thread(
                    fetch_account_usage,
                    provider,
                    base_url=base_url,
                    api_key=api_key,
                )
            except Exception:
                account_snapshot = None
            if account_snapshot:
                account_lines = render_account_usage_lines(account_snapshot, markdown=True)

        # ── Nous credits 数值 + 月度赠送额度 % 仪表 ─────────────
        # 通过 nous_credits_lines() 与 CLI / TUI 的 /usage 区块共享：单一的
        # 鉴权 + portal 拉取 + 渲染路径（同时也尊重开发夹具）。在 event loop 之外
        # 运行。helper 的判定条件是“已登录某个 Nous 账户”——而不是推理 provider，
        # 也没有嵌套在 `if provider:` 下——这样即便一个持有 Nous 凭证的用户在
        # 其他地方运行推理（或没有常驻 agent），仍能看到余额。不触发恢复动作：
        # 消息场景没有绑定 notice 消费者，因此 /usage 仅做展示。fail-open：绝不
        # 让 /usage 崩掉。
        try:
            from agent.account_usage import nous_credits_lines

            credits_lines = await asyncio.to_thread(nous_credits_lines, markdown=True)
        except Exception:
            credits_lines = []  # fail-open：绝不让 /usage 崩掉

        if agent and hasattr(agent, "session_total_tokens") and agent.session_api_calls > 0:
            lines = []

            # 限流信息（当 provider 响应头中可用时）
            rl_state = agent.get_rate_limit_state()
            if rl_state and rl_state.has_data:
                from agent.rate_limit_tracker import format_rate_limit_compact
                lines.append(t("gateway.usage.rate_limits", state=format_rate_limit_compact(rl_state)))
                lines.append("")

            # session token 用量 —— 与 CLI 一致的详细明细
            input_tokens = getattr(agent, "session_input_tokens", 0) or 0
            output_tokens = getattr(agent, "session_output_tokens", 0) or 0
            cache_read = getattr(agent, "session_cache_read_tokens", 0) or 0
            cache_write = getattr(agent, "session_cache_write_tokens", 0) or 0

            lines.append(t("gateway.usage.header_session"))
            lines.append(t("gateway.usage.label_model", model=agent.model))
            lines.append(t("gateway.usage.label_input_tokens", count=f"{input_tokens:,}"))
            if cache_read:
                lines.append(t("gateway.usage.label_cache_read", count=f"{cache_read:,}"))
            if cache_write:
                lines.append(t("gateway.usage.label_cache_write", count=f"{cache_write:,}"))
            lines.append(t("gateway.usage.label_output_tokens", count=f"{output_tokens:,}"))
            lines.append(t("gateway.usage.label_total", count=f"{agent.session_total_tokens:,}"))
            lines.append(t("gateway.usage.label_api_calls", count=agent.session_api_calls))

            # 成本估算
            try:
                from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
                cost_result = estimate_usage_cost(
                    agent.model,
                    CanonicalUsage(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_tokens=cache_read,
                        cache_write_tokens=cache_write,
                    ),
                    provider=getattr(agent, "provider", None),
                    base_url=getattr(agent, "base_url", None),
                )
                if cost_result.amount_usd is not None:
                    prefix = "~" if cost_result.status == "estimated" else ""
                    lines.append(t("gateway.usage.label_cost", prefix=prefix, amount=f"{float(cost_result.amount_usd):.4f}"))
                elif cost_result.status == "included":
                    lines.append(t("gateway.usage.label_cost_included"))
            except Exception:
                pass

            # 上下文窗口与压缩次数
            ctx = agent.context_compressor
            if ctx.last_prompt_tokens:
                pct = min(100, ctx.last_prompt_tokens / ctx.context_length * 100) if ctx.context_length else 0
                lines.append(t("gateway.usage.label_context", used=f"{ctx.last_prompt_tokens:,}", total=f"{ctx.context_length:,}", pct=f"{pct:.0f}"))
            if ctx.compression_count:
                lines.append(t("gateway.usage.label_compressions", count=ctx.compression_count))

            if account_lines:
                lines.append("")
                lines.extend(account_lines)
            if credits_lines:
                lines.append("")
                lines.extend(credits_lines)

            return "\n".join(lines)

        # 完全没有 agent —— 检查 session 历史给出一个粗略计数
        session_entry = self.session_store.get_or_create_session(source)
        history = self.session_store.load_transcript(session_entry.session_id)
        if history:
            from agent.model_metadata import estimate_messages_tokens_rough
            msgs = [m for m in history if m.get("role") in {"user", "assistant"} and m.get("content")]
            approx = estimate_messages_tokens_rough(msgs)
            lines = [
                t("gateway.usage.header_session_info"),
                t("gateway.usage.label_messages", count=len(msgs)),
                t("gateway.usage.label_estimated_context", count=f"{approx:,}"),
                t("gateway.usage.detailed_after_first"),
            ]
            if account_lines:
                lines.append("")
                lines.extend(account_lines)
            if credits_lines:
                lines.append("")
                lines.extend(credits_lines)
            return "\n".join(lines)
        if account_lines or credits_lines:
            # 仅账户、仅 credits，或两者兼有 —— 用空行分隔拼接。
            parts = list(account_lines)
            if credits_lines:
                if parts:
                    parts.append("")
                parts.extend(credits_lines)
            return "\n".join(parts)
        return t("gateway.usage.no_data")

    async def _handle_insights_command(self, event: MessageEvent) -> str:
        """\u5904\u7406 /insights \u547d\u4ee4 \u2014\u2014 \u663e\u793a\u7528\u91cf\u6d1e\u5bdf\u4e0e\u5206\u6790\u3002"""
        args = event.get_command_args().strip()

        # \u5f52\u4e00\u5316 Unicode \u7834\u6298\u53f7\uff08Telegram/iOS \u4f1a\u81ea\u52a8\u628a -- \u8f6c\u6210 em/en dash\uff09
        args = re.sub(r'[\u2012\u2013\u2014\u2015](days|source)', r'--\1', args)

        days = 30
        source = None

        # \u89e3\u6790\u7b80\u5355\u53c2\u6570\uff1a/insights 7  \u6216  /insights --days 7
        if args:
            parts = args.split()
            i = 0
            while i < len(parts):
                if parts[i] == "--days" and i + 1 < len(parts):
                    try:
                        days = int(parts[i + 1])
                    except ValueError:
                        return t("gateway.insights.invalid_days", value=parts[i + 1])
                    i += 2
                elif parts[i] == "--source" and i + 1 < len(parts):
                    source = parts[i + 1]
                    i += 2
                elif parts[i].isdigit():
                    days = int(parts[i])
                    i += 1
                else:
                    i += 1

        try:
            from hermes_state import SessionDB
            from agent.insights import InsightsEngine

            loop = asyncio.get_running_loop()

            def _run_insights():
                db = SessionDB()
                engine = InsightsEngine(db)
                report = engine.generate(days=days, source=source)
                result = engine.format_gateway(report)
                db.close()
                return result

            return await loop.run_in_executor(None, _run_insights)
        except Exception as e:
            logger.error("Insights command error: %s", e, exc_info=True)
            return t("gateway.insights.error", error=e)

    async def _handle_reload_mcp_command(self, event: MessageEvent) -> Optional[str]:
        """处理 /reload-mcp —— 重新连接 MCP 服务器并重建缓存 agent。

        重载 MCP 工具会使当前 session 的 provider prompt cache 失效（工具
        schema 内嵌在 system prompt 中）。下一条消息会重新发送完整的输入
        token，在长上下文或高 reasoning 模型上开销很大。

        为了把这一开销告知用户，本命令会经由 slash-confirm 原语路由：在 reload
        真正执行前，用户会看到 Approve Once / Always Approve / Cancel 的提示。
        "Always Approve" 会持久化 ``approvals.mcp_reload_confirm: false``，使后续
        任何 session 的 reload 都不再弹出该提示。

        用户也可以直接修改该 config key 来跳过确认。
        """
        source = event.source
        session_key = self._session_key_for_source(source)

        # 从磁盘实时读取门控值，使得上一次点击的 "always" 在下一次调用即生效，
        # 无需重启 gateway。
        user_config = self._read_user_config()
        approvals = user_config.get("approvals") if isinstance(user_config, dict) else None
        confirm_required = True
        if isinstance(approvals, dict):
            confirm_required = bool(approvals.get("mcp_reload_confirm", True))

        if not confirm_required:
            return await self._execute_mcp_reload(event)

        # 经由 slash-confirm 路由。该原语会发送提示并保存 resume handler；按钮 /
        # 文本响应会触发 ``_resolve_slash_confirm``，由它用所选结果调用 handler。
        async def _on_confirm(choice: str) -> Optional[str]:
            if choice == "cancel":
                return t("gateway.reload_mcp.cancelled")
            if choice == "always":
                # 持久化退出选项，然后执行 reload。
                try:
                    from cli import save_config_value
                    save_config_value("approvals.mcp_reload_confirm", False)
                    logger.info(
                        "User opted out of /reload-mcp confirmation (session=%s)",
                        session_key,
                    )
                except Exception as exc:
                    logger.warning("Failed to persist mcp_reload_confirm=false: %s", exc)
            # once / always → 执行 reload
            result = await self._execute_mcp_reload(event)
            if choice == "always":
                return f"{result}\n\n" + t("gateway.reload_mcp.always_followup")
            return result

        prompt_message = t("gateway.reload_mcp.confirm_prompt")
        return await self._request_slash_confirm(
            event=event,
            command="reload-mcp",
            title="/reload-mcp",
            message=prompt_message,
            handler=_on_confirm,
        )

    async def _handle_reload_skills_command(self, event: MessageEvent) -> str:
        """处理 /reload-skills —— 重新扫描 skills 目录，并为下一回合排队一条说明。

        skills 无需出现在 system prompt 中即可被模型使用（它们在运行时通过
        ``/skill-name``、``skills_list`` 或 ``skill_view`` 调用），因此本命令不
        会清除 prompt cache——prefix caching 保持不变。

        如果有任何 skill 被添加或移除，会在
        ``self._pending_skills_reload_notes[session_key]`` 上排队一条一次性说明。
        gateway 会把它前插到本 session 的下一条用户消息（见 ``_run_agent_turn``
        中约 L11025 处的消费者），然后清除。不会带外写入 session transcript，
        因此消息交替得以保留。
        """
        loop = asyncio.get_running_loop()
        try:
            from agent.skill_commands import reload_skills

            result = await loop.run_in_executor(None, reload_skills)
            added = result.get("added", [])      # [{"name", "description"}, ...]
            removed = result.get("removed", [])  # [{"name", "description"}, ...]
            total = result.get("total", 0)

            # 让每个已连接的 adapter 刷新启动时缓存的 skill 列表相关的平台侧
            # 状态。当前这就是 Discord 的 /skill 自动补全（每次连接注册一次）；
            # 如果没有这次调用，新增的 skill 在下拉框中始终不可见，已删除的
            # skill 在被点击时会报错。其他没有重写 refresh_skill_group 的
            # adapter（Telegram 的 BotCommand 菜单、Slack 的 subcommand 映射等）
            # 会被静默跳过——上面的进程内 reload 对它们已经足够。
            for adapter in list(self.adapters.values()):
                refresh = getattr(adapter, "refresh_skill_group", None)
                if not callable(refresh):
                    continue
                try:
                    maybe = refresh()
                    if inspect.isawaitable(maybe):
                        await maybe
                except Exception as exc:
                    logger.warning(
                        "Adapter %s refresh_skill_group raised: %s",
                        getattr(adapter, "name", adapter), exc,
                    )

            lines = [t("gateway.reload_skills.header")]
            if not added and not removed:
                lines.append(t("gateway.reload_skills.no_new"))
                lines.append(t("gateway.reload_skills.total", count=total))
                return "\n".join(lines)

            def _fmt_line(item: dict) -> str:
                nm = item.get("name", "")
                desc = item.get("description", "")
                if desc:
                    return t("gateway.reload_skills.item_with_desc", name=nm, desc=desc)
                return t("gateway.reload_skills.item_no_desc", name=nm)

            if added:
                lines.append(t("gateway.reload_skills.added_header"))
                for item in added:
                    lines.append(_fmt_line(item))
            if removed:
                lines.append(t("gateway.reload_skills.removed_header"))
                for item in removed:
                    lines.append(_fmt_line(item))
            lines.append(t("gateway.reload_skills.total", count=total))

            # 为本 session 下一回合排队一条一次性说明。格式与 system prompt 渲染
            # 已有 skills 时的方式一致（``    - name: description``），使模型以
            # 与原始 skill 目录相同的形态读取该 diff。
            sections = ["[USER INITIATED SKILLS RELOAD:"]
            if added:
                sections.append("")
                sections.append("Added Skills:")
                for item in added:
                    sections.append(_fmt_line(item))
            if removed:
                sections.append("")
                sections.append("Removed Skills:")
                for item in removed:
                    sections.append(_fmt_line(item))
            sections.append("")
            sections.append("Use skills_list to see the updated catalog.]")
            note = "\n".join(sections)

            session_key = self._session_key_for_source(event.source)
            if not hasattr(self, "_pending_skills_reload_notes"):
                self._pending_skills_reload_notes = {}
            if session_key:
                self._pending_skills_reload_notes[session_key] = note

            return "\n".join(lines)

        except Exception as e:
            logger.warning("Skills reload failed: %s", e)
            return t("gateway.reload_skills.failed", error=e)

    async def _handle_bundles_command(self, event: MessageEvent) -> str:
        """处理 /bundles —— 列出已安装的 skill bundle。

        镜像 CLI 的 ``/bundles`` 处理器。返回适合任意 gateway adapter 的单条
        文本消息；bundle 是通过调用其自身的 ``/<slug>`` 命令加载的，而不是由
        本命令加载。
        """
        try:
            from agent.skill_bundles import list_bundles, _bundles_dir
        except Exception as exc:
            logger.warning("Bundles command unavailable: %s", exc)
            return f"Bundles subsystem unavailable: {exc}"

        bundles = list_bundles()
        if not bundles:
            return (
                "No skill bundles installed.\n"
                "Create one on the host with:\n"
                "  `hermes bundles create <name> --skill <s1> --skill <s2>`\n"
                f"Directory: `{_bundles_dir()}`"
            )

        lines = [f"**Skill Bundles** ({len(bundles)} installed):", ""]
        for info in bundles:
            skill_count = len(info.get("skills", []))
            desc = info.get("description") or f"Load {skill_count} skills"
            lines.append(
                f"• `/{info['slug']}` — {desc} _({skill_count} skills)_"
            )
            for s in info.get("skills", []):
                lines.append(f"    · {s}")
        lines.append("")
        lines.append("Invoke a bundle with `/<slug>` to load all its skills.")
        return "\n".join(lines)

    async def _handle_approve_command(self, event: MessageEvent) -> Optional[str]:
        """处理 /approve 命令 —— 解除等待中的 agent 线程阻塞。

        agent 线程阻塞在 tools/approval.py 内，等待用户响应。本处理器发送事件
        信号使 agent 恢复运行，并由 terminal_tool 内联执行该命令——与 CLI 的
        同步 input() 审批流程相同。

        支持多个并发审批（并行 subagent、execute_code）。``/approve`` 解析最
        早的待审命令；``/approve all`` 一次性解析所有待审命令。

        用法：
            /approve              —— 仅批准最早的待审命令一次
            /approve all          —— 一次性批准所有待审命令
            /approve session      —— 批准最早的 + 在本 session 内记忆
            /approve all session  —— 批准全部 + 在本 session 内记忆
            /approve always       —— 批准最早的 + 永久记忆
            /approve all always   —— 批准全部 + 永久记忆
        """
        source = event.source
        session_key = self._session_key_for_source(source)

        from tools.approval import (
            resolve_gateway_approval, has_blocking_approval,
        )

        if not has_blocking_approval(session_key):
            if session_key in self._pending_approvals:
                self._pending_approvals.pop(session_key)
                return t("gateway.approval_expired")
            return t("gateway.approve.no_pending")

        # 解析参数：支持 "all"、"all session"、"all always"、"session"、"always"
        args = event.get_command_args().strip().lower().split()
        resolve_all = "all" in args
        remaining = [a for a in args if a != "all"]

        if any(a in {"always", "permanent", "permanently"} for a in remaining):
            choice = "always"
        elif any(a in {"session", "ses"} for a in remaining):
            choice = "session"
        else:
            choice = "once"

        count = resolve_gateway_approval(session_key, choice, resolve_all=resolve_all)
        if not count:
            return t("gateway.approve.no_pending")

        # 恢复打字指示 —— agent 即将继续处理。
        _adapter = self.adapters.get(source.platform)
        if _adapter:
            _adapter.resume_typing_for_chat(source.chat_id)

        logger.info("User approved %d dangerous command(s) via /approve (%s)", count, choice)
        plural = "plural" if count > 1 else "singular"
        return t(f"gateway.approve.{choice}_{plural}", count=count)

    async def _handle_deny_command(self, event: MessageEvent) -> str:
        """处理 /deny 命令 —— 拒绝待审的危险命令。

        向被阻塞的 agent 线程发送 'deny' 结果，使它们收到一个明确的 BLOCKED
        消息，与 CLI 的 deny 流程一致。

        ``/deny`` 拒绝最早的命令；``/deny all`` 拒绝全部。
        """
        source = event.source
        session_key = self._session_key_for_source(source)

        from tools.approval import (
            resolve_gateway_approval, has_blocking_approval,
        )

        if not has_blocking_approval(session_key):
            if session_key in self._pending_approvals:
                self._pending_approvals.pop(session_key)
                return t("gateway.deny.stale")
            return t("gateway.deny.no_pending")

        args = event.get_command_args().strip().lower()
        resolve_all = "all" in args

        count = resolve_gateway_approval(session_key, "deny", resolve_all=resolve_all)
        if not count:
            return t("gateway.deny.no_pending")

        # 恢复打字指示 —— agent 继续（结果为 BLOCKED）。
        _adapter = self.adapters.get(source.platform)
        if _adapter:
            _adapter.resume_typing_for_chat(source.chat_id)

        logger.info("User denied %d dangerous command(s) via /deny", count)
        if count > 1:
            return t("gateway.deny.denied_plural", count=count)
        return t("gateway.deny.denied_singular")

    async def _handle_debug_command(self, event: MessageEvent) -> str:
        """处理 /debug —— 上传调试报告（仅摘要）并返回 paste URL。

        gateway 仅上传摘要报告（系统信息 + 日志尾部），不上传完整日志文件，
        以保护对话隐私。需要上传完整日志的用户应使用 CLI 的
        ``hermes debug share``。
        """
        import asyncio
        from hermes_cli.debug import (
            _capture_dump, collect_debug_report,
            upload_to_pastebin, _schedule_auto_delete,
            _GATEWAY_PRIVACY_NOTICE, _best_effort_sweep_expired_pastes,
        )

        loop = asyncio.get_running_loop()

        # 在线程中执行阻塞 I/O（dump 捕获、日志读取、上传）。
        def _collect_and_upload():
            _best_effort_sweep_expired_pastes()
            dump_text = _capture_dump()
            report = collect_debug_report(log_lines=200, dump_text=dump_text)

            urls = {}
            try:
                urls["Report"] = upload_to_pastebin(report)
            except Exception as exc:
                return t("gateway.debug.upload_failed", error=exc)

            # 在 6 小时后调度自动删除
            _schedule_auto_delete(list(urls.values()))

            lines = [_GATEWAY_PRIVACY_NOTICE, "", t("gateway.debug.header"), ""]
            label_width = max(len(k) for k in urls)
            for label, url in urls.items():
                lines.append(f"`{label:<{label_width}}`  {url}")

            lines.append("")
            lines.append(t("gateway.debug.auto_delete"))
            lines.append(t("gateway.debug.full_logs_hint"))
            lines.append(t("gateway.debug.share_hint"))
            return "\n".join(lines)

        return await loop.run_in_executor(None, _collect_and_upload)

    async def _handle_update_command(self, event: MessageEvent) -> str:
        """处理 /update 命令 —— 将 Hermes Agent 更新到最新版本。

        在一个 detached session 中（通过 ``setsid``）启动 ``hermes update``，
        使其能在 ``hermes update`` 可能触发的 gateway 重启后继续存活。写入
        marker 文件，使得当前 gateway 进程或下一个进程都能在更新完成时通知
        用户。
        """
        from gateway.run import _hermes_home, _resolve_hermes_bin
        import json
        import shutil
        import subprocess
        from datetime import datetime
        from hermes_cli.config import is_managed, format_managed_message

        # 拦截非消息类平台（API server、webhooks、ACP）
        platform = event.source.platform
        _allowed = self._UPDATE_ALLOWED_PLATFORMS
        # 设置了 allow_update_command=True 的插件平台同样允许
        if platform not in _allowed:
            try:
                from gateway.platform_registry import platform_registry
                entry = platform_registry.get(platform.value)
                if not entry or not entry.allow_update_command:
                    return t("gateway.update.platform_not_messaging")
            except Exception:
                return t("gateway.update.platform_not_messaging")

        if is_managed():
            return f"✗ {format_managed_message('update Hermes Agent')}"

        project_root = Path(__file__).parent.parent.resolve()
        git_dir = project_root / '.git'

        if not git_dir.exists():
            return t("gateway.update.not_git_repo")

        hermes_cmd = _resolve_hermes_bin()
        if not hermes_cmd:
            return t("gateway.update.hermes_cmd_not_found")

        pending_path = _hermes_home / ".update_pending.json"
        output_path = _hermes_home / ".update_output.txt"
        exit_code_path = _hermes_home / ".update_exit_code"
        session_key = self._session_key_for_source(event.source)
        pending = {
            "platform": event.source.platform.value,
            "chat_id": event.source.chat_id,
            "chat_type": event.source.chat_type,
            "user_id": event.source.user_id,
            "session_key": session_key,
            "timestamp": datetime.now().isoformat(),
        }
        if event.source.thread_id:
            pending["thread_id"] = event.source.thread_id
        if event.message_id:
            pending["message_id"] = event.message_id
        _tmp_pending = pending_path.with_suffix(".tmp")
        _tmp_pending.write_text(json.dumps(pending))
        _tmp_pending.replace(pending_path)
        exit_code_path.unlink(missing_ok=True)

        # 以 detached 方式启动 `hermes update --gateway`，使其能在 gateway 重启
        # 后继续存活。
        # --gateway 为交互式提示（stash 恢复、config 迁移）启用基于文件的 IPC，
        # 让 gateway 能把这些提示转发给用户，而不是静默跳过。
        # 使用 setsid 来实现可移植的 session detach（在 systemd-run --user 因
        # 缺失 D-Bus session 而失败的系统服务下也能工作）。
        # PYTHONUNBUFFERED 确保输出逐行刷新，使 gateway 能近实时地把它流式发送
        # 到 messenger。
        # 以 detached 方式启动 `hermes update --gateway`，使其能在 gateway 重启
        # 后继续存活。
        # --gateway 为交互式提示（stash 恢复、config 迁移）启用基于文件的 IPC，
        # 让 gateway 能把这些提示转发给用户，而不是静默跳过。
        # 使用 setsid 来实现可移植的 session detach（在 systemd-run --user 因
        # 缺失 D-Bus session 而失败的系统服务下也能工作）。
        # PYTHONUNBUFFERED 确保输出逐行刷新，使 gateway 能近实时地把它流式发送
        # 到 messenger。
        #
        # Windows：没有 bash/setsid 链。直接通过 sys.executable 运行
        # `hermes update --gateway`；通过 Popen 文件句柄把 stdout/stderr 重定向
        # 到同一组输出文件；在后续写入中写入退出码。用一个微型 Python 看守进程
        # 会更干净，但我们已经在 gateway/run.py 的异步 update 路径里，因此最
        # 简单正确的做法是：启动一个内联的 Python helper 来运行命令并写入两份
        # 输出。
        try:
            if sys.platform == "win32":
                import textwrap
                from hermes_cli._subprocess_compat import windows_detach_popen_kwargs

                # hermes_cmd 是一个 argv 分量列表，可以直接传入
                # （无需 shell 转义）。
                helper = textwrap.dedent(
                    """
                    import os, subprocess, sys
                    output_path = sys.argv[1]
                    exit_code_path = sys.argv[2]
                    cmd = sys.argv[3:]
                    env = dict(os.environ)
                    env["PYTHONUNBUFFERED"] = "1"
                    with open(output_path, "wb") as f:
                        proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, env=env)
                        rc = proc.wait(timeout=3600)
                    with open(exit_code_path, "w") as f:
                        f.write(str(rc))
                    """
                ).strip()
                subprocess.Popen(
                    [
                        sys.executable, "-c", helper,
                        str(output_path), str(exit_code_path),
                        *hermes_cmd, "update", "--gateway",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **windows_detach_popen_kwargs(),
                )
            else:
                hermes_cmd_str = " ".join(shlex.quote(part) for part in hermes_cmd)
                update_cmd = (
                    f"PYTHONUNBUFFERED=1 {hermes_cmd_str} update --gateway"
                    f" > {shlex.quote(str(output_path))} 2>&1; "
                    # 避免 `status=$?`：`status` 在 zsh 中是一个只读的特殊参数，
                    # 而这条命令字符串会被复制/复用到 macOS/zsh 的运维 wrapper 中。
                    # 尽管这个具体的 subprocess 当前是在 bash 下运行，仍保持模板
                    # 对 zsh 安全。
                    f"rc=$?; printf '%s' \"$rc\" > {shlex.quote(str(exit_code_path))}"
                )
                setsid_bin = shutil.which("setsid")
                if setsid_bin:
                    # 首选：setsid 会创建一个新 session，完全 detached
                    subprocess.Popen(
                        [setsid_bin, "bash", "-c", update_cmd],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                else:
                    # 兜底：start_new_session=True 会在子进程中调用 os.setsid()
                    subprocess.Popen(
                        ["bash", "-c", update_cmd],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
        except Exception as e:
            pending_path.unlink(missing_ok=True)
            exit_code_path.unlink(missing_ok=True)
            return t("gateway.update.start_failed", error=e)

        self._schedule_update_notification_watch()
        return t("gateway.update.starting")
