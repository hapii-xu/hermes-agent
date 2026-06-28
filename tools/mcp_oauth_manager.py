#!/usr/bin/env python3
"""按服务器的 MCP OAuth 状态中央管理器。

进程内共享的单实例。持有各服务器的 OAuth provider 实例，并协调以下事项：

- **跨进程的 token 重载**：通过基于 mtime 的磁盘监视。当外部进程
  （例如用户 cron 任务）在磁盘上刷新了 token，下一次授权流程会直接
  读取它们，而无需进程重启。
- **401 去重**：通过 in-flight future。当 N 个并发工具调用都以同一个
  access_token 命中 401 时，只触发一次恢复尝试；其余调用等待同一个
  结果。
- **重连信号**：用于长生命周期的 MCP 会话。管理器本身不驱动重连 ——
  重连由 `mcp_tool.py` 中的 `MCPServerTask` 完成 —— 但管理器是决定
  何时有必要重连的唯一事实来源。

取代了过去散落在 `mcp_oauth.py`、`mcp_tool.py` 和
`hermes_cli/mcp_config.py` 中八个调用点的逻辑。本模块是唯一实例化
MCP SDK 的 `OAuthClientProvider` 的地方 —— 所有其他代码路径都经过
`get_manager()`。

设计参考：

- Claude Code 的 ``invalidateOAuthCacheIfDiskChanged``
  （``claude-code/src/utils/auth.ts:1320``，CC-1096 / GH#24317）。
  完全相同的外部刷新陈旧性 bug 类别。
- Codex 的 ``refresh_oauth_if_needed`` / ``persist_if_needed``
  （``codex-rs/rmcp-client/src/rmcp_client.rs:805``）。我们依赖 MCP
  SDK 的懒刷新，而不是在每次操作前主动调用 refresh，因为每次工具调用
  只做一次 ``stat()`` 比一次 ``await`` + 可能的刷新往返更廉价，而且
  SDK 的内存过期路径本身已是正确的。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 按服务器的条目
# ---------------------------------------------------------------------------


@dataclass
class _ProviderEntry:
    """管理器所追踪的、按服务器的 OAuth 状态。

    字段：
        server_url: 用于构建 provider 的 MCP 服务器 URL。跟踪它以便
            在 URL 变化时丢弃缓存的 provider。
        oauth_config: 来自 ``mcp_servers.<name>.oauth`` 的可选 dict。
        provider: 包装 MCP SDK 的、与 ``httpx.Auth`` 兼容的 provider。
            首次使用前为 None。
        last_mtime_ns: 磁盘 token 文件最近一次看到的 ``st_mtime_ns``。
            从未读取过则为零。供 :meth:`MCPOAuthManager.invalidate_if_disk_changed`
            用于检测外部刷新。
        lock: 串行化对该条目状态的并发访问。绑定到首次 await 它的
            asyncio 事件循环（即 MCP 的事件循环）。
        pending_401: 以失败的 access_token 为键的、in-flight 的
            401 处理 future，用于对惊群（thundering-herd）式 401 去重。
            镜像了 Claude Code 的 ``pending401Handlers`` 映射。
    """

    server_url: str
    oauth_config: Optional[dict]
    provider: Optional[Any] = None
    last_mtime_ns: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_401: dict[str, "asyncio.Future[bool]"] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# HermesMCPOAuthProvider —— 带 disk-watch 的 OAuthClientProvider 子类
# ---------------------------------------------------------------------------


def _make_hermes_provider_class() -> Optional[type]:
    """懒加载导入 SDK 基类并返回我们的子类。

    包裹在函数中，使本模块在 MCP SDK 的 OAuth 模块不可用时
    （例如较旧的 mcp 版本）仍能干净地导入。
    """
    try:
        from mcp.client.auth.oauth2 import OAuthClientProvider
    except ImportError:  # pragma: no cover —— CI 中要求 SDK
        return None

    class HermesMCPOAuthProvider(OAuthClientProvider):
        """带流程前磁盘 mtime 重载的 OAuthClientProvider。

        在每次 ``async_auth_flow`` 调用之前，请求管理器检查磁盘上的
        token 文件是否被外部修改过。若是，则管理器重置
        ``_initialized``，使下一次流程从存储中重新读取。

        这让外部进程的刷新（cron、另一个 CLI 实例）对正在运行的 MCP
        会话可见，而无需重启。

        参考：Claude Code 的 ``invalidateOAuthCacheIfDiskChanged``
        （``src/utils/auth.ts:1320``，CC-1096 / GH#24317）。
        """

        def __init__(self, *args: Any, server_name: str = "", **kwargs: Any):
            super().__init__(*args, **kwargs)
            self._hermes_server_name = server_name

        async def _initialize(self) -> None:
            """加载已存储的 token + 客户端信息，并预置 token_expiry_time。

            此外，当我们有已存储的 token 但没有缓存的元数据时，会
            主动获取 OAuth 授权服务器元数据（PRM + ASM），使 SDK 的
            ``_refresh_token`` 能在抢占式刷新路径上构建正确的
            token_endpoint URL。否则 SDK 会退回到
            ``{mcp_server_url}/token``（对于其 AS 位于不同源的 provider
            是错误的 —— BetterStack 的 MCP 位于
            ``https://mcp.betterstack.com``，但其 token 端点在
            ``https://betterstack.com/oauth/token``），刷新返回 404，
            我们便跌落至完整的浏览器重新授权。

            SDK 的基类 ``_initialize`` 会填充 ``current_tokens`` 但不会
            调用 ``update_token_expiry``，因此 ``token_expiry_time`` 保持
            为 ``None``，``is_token_valid()`` 对任何已加载的 token
            （不论实际有多旧）都返回 True。进程重启后，这会把过期的
            Bearer token 发给服务器；某些 provider 返回 HTTP 401（被 401
            handler 捕获），另一些返回 200 但附带应用级认证错误（对传输层
            不可见，例如 BetterStack 返回 "No teams found. Please check
            your authentication."）。

            从重载的 token 预置 ``token_expiry_time`` 即可修复此问题：
            ``is_token_valid()`` 对过期 token 正确返回 False，
            ``async_auth_flow`` 走 ``can_refresh_token()`` 分支，SDK
            在第一次真实请求前静默刷新。

            与 :class:`HermesTokenStorage` 持久化绝对 ``expires_at``
            时间戳（``mcp_oauth.py:set_tokens``）配合，使此处计算出的
            剩余 TTL 反映真实的挂钟时长。
            """
            await super()._initialize()
            tokens = self.context.current_tokens
            if tokens is not None and tokens.expires_in is not None:
                self.context.update_token_expiry(tokens)

            # 冷加载：在任何刷新尝试之前，从磁盘恢复 OAuth 服务器元数据。
            # 否则，一个重启后的进程若有缓存 token 但无内存元数据，会
            # 退回到 SDK 猜测的 ``{server_url}/token`` 路径（在大多数
            # 真实 provider 上返回 404），并需要完整的浏览器重新授权。
            storage = self.context.storage
            from tools.mcp_oauth import HermesTokenStorage
            if (
                isinstance(storage, HermesTokenStorage)
                and self.context.oauth_metadata is None
            ):
                meta = storage.load_oauth_metadata()
                if meta is not None:
                    self.context.oauth_metadata = meta
                    logger.debug(
                        "MCP OAuth '%s': restored metadata from disk "
                        "(token_endpoint=%s)",
                        self._hermes_server_name,
                        meta.token_endpoint,
                    )

            # 流程前执行 OAuth AS 发现，使 ``_refresh_token`` 在首次刷新
            # 尝试前拥有正确的 ``token_endpoint``。仅在我们冷加载时
            # 有 token 但无缓存元数据时运行 —— 即 SDK 内置的 401 分支
            # 发现还没机会运行的场景。
            if (
                tokens is not None
                and self.context.oauth_metadata is None
            ):
                try:
                    await self._prefetch_oauth_metadata()
                except Exception as exc:  # pragma: no cover —— 防御性
                    # 非致命：若发现失败，SDK 正常的 401 分支发现会在
                    # 下一次请求时运行。
                    logger.debug(
                        "MCP OAuth '%s': pre-flight metadata discovery "
                        "failed (non-fatal): %s",
                        self._hermes_server_name, exc,
                    )

        async def _prefetch_oauth_metadata(self) -> None:
            """从 well-known 端点获取 PRM + ASM，并缓存到 context 上。

            镜像 SDK 的 401 分支发现（oauth2.py 约 511-551 行），但
            在首次请求之前同步运行，而不是在 httpx 的 auth_flow 生成器
            内部。使用 SDK 自己的 URL 构建器和响应处理器，使我们能跟踪
            我们所固定的 SDK 版本所期望的行为。
            """
            import httpx  # 本地导入：httpx 是 MCP SDK 的依赖
            from mcp.client.auth.utils import (
                build_oauth_authorization_server_metadata_discovery_urls,
                build_protected_resource_metadata_discovery_urls,
                create_oauth_metadata_request,
                handle_auth_metadata_response,
                handle_protected_resource_response,
            )

            server_url = self.context.server_url
            async with httpx.AsyncClient(timeout=10.0) as client:
                # 第 1 步：PRM 发现，以获知 authorization_server URL。
                for url in build_protected_resource_metadata_discovery_urls(
                    None, server_url
                ):
                    req = create_oauth_metadata_request(url)
                    try:
                        resp = await client.send(req)
                    except httpx.HTTPError as exc:
                        logger.debug(
                            "MCP OAuth '%s': PRM discovery to %s failed: %s",
                            self._hermes_server_name, url, exc,
                        )
                        continue
                    prm = await handle_protected_resource_response(resp)
                    if prm:
                        self.context.protected_resource_metadata = prm
                        if prm.authorization_servers:
                            self.context.auth_server_url = str(
                                prm.authorization_servers[0]
                            )
                        break

                # 第 2 步：针对 auth_server_url 进行 ASM 发现
                # （或对遗留 provider 回退到 server_url）。
                for url in build_oauth_authorization_server_metadata_discovery_urls(
                    self.context.auth_server_url, server_url
                ):
                    req = create_oauth_metadata_request(url)
                    try:
                        resp = await client.send(req)
                    except httpx.HTTPError as exc:
                        logger.debug(
                            "MCP OAuth '%s': ASM discovery to %s failed: %s",
                            self._hermes_server_name, url, exc,
                        )
                        continue
                    ok, asm = await handle_auth_metadata_response(resp)
                    if not ok:
                        break
                    if asm:
                        self.context.oauth_metadata = asm
                        # 立即持久化，使随后的冷加载能完全跳过发现。
                        storage = self.context.storage
                        from tools.mcp_oauth import HermesTokenStorage
                        if isinstance(storage, HermesTokenStorage):
                            storage.save_oauth_metadata(asm)
                        logger.debug(
                            "MCP OAuth '%s': pre-flight ASM discovered "
                            "token_endpoint=%s",
                            self._hermes_server_name, asm.token_endpoint,
                        )
                        break

        def _persist_oauth_metadata_if_changed(self) -> None:
            """持久化已发现的 OAuth 元数据，供未来的进程重启使用。

            在 SDK 正常的 401 分支授权流程完成之后调用，使通过懒路径
            （非预检）发现的元数据也被保存。当没有可持久化的内容或元数据
            未变化时为空操作。
            """
            meta = self.context.oauth_metadata
            if meta is None:
                return
            storage = self.context.storage
            from tools.mcp_oauth import HermesTokenStorage
            if not isinstance(storage, HermesTokenStorage):
                return
            existing = storage.load_oauth_metadata()
            if (
                existing is None
                or str(existing.token_endpoint) != str(meta.token_endpoint)
            ):
                storage.save_oauth_metadata(meta)

        async def async_auth_flow(self, request):  # type: ignore[override]
            # 流程前钩子：请求管理器在需要时从磁盘刷新。
            # 此处任何失败都是非致命的 —— 我们只是记日志，然后用 SDK
            # 已有的状态继续。
            try:
                await get_manager().invalidate_if_disk_changed(
                    self._hermes_server_name
                )
            except Exception as exc:  # pragma: no cover —— 防御性
                logger.debug(
                    "MCP OAuth '%s': pre-flow disk-watch failed (non-fatal): %s",
                    self._hermes_server_name, exc,
                )

            # 手动桥接双向生成器协议。httpx 的 auth_flow 驱动
            # （httpx._client._send_handling_auth）调用
            # ``auth_flow.asend(response)`` 把 HTTP 响应回送给生成器。
            # 一个使用 ``async for item in inner: yield item`` 的朴素包装
            # 会丢弃这些 .asend(response) 值，并以 None 恢复内部生成器，
            # 于是 mcp/client/auth/oauth2.py 中 SDK 的
            # ``response = yield request`` 分支会看到 response=None，并在
            # ``if response.status_code == 401`` 处因 AttributeError 崩溃。
            #
            # 下方的桥接通过 inner.asend(incoming) 把每个 .asend() 值
            # 转发给内部生成器，从而保留双向契约。这是 PR #11383 引入的
            # 回归，由 tests/tools/test_mcp_oauth_bidirectional.py 捕获。
            inner = super().async_auth_flow(request)
            try:
                outgoing = await inner.__anext__()
                while True:
                    incoming = yield outgoing
                    outgoing = await inner.asend(incoming)
            except StopAsyncIteration:
                # 持久化 SDK 在 401 分支中懒发现的任何元数据，
                # 使随后的冷加载能跳过发现。
                self._persist_oauth_metadata_if_changed()
                return

    return HermesMCPOAuthProvider


# 在导入时缓存。已被测试使用，且被 :class:`MCPOAuthManager` 使用。
_HERMES_PROVIDER_CLS: Optional[type] = _make_hermes_provider_class()


# ---------------------------------------------------------------------------
# 管理器
# ---------------------------------------------------------------------------


class MCPOAuthManager:
    """按服务器的 MCP OAuth 状态的唯一事实来源。

    线程安全：``_entries`` dict 由 ``_entries_lock`` 保护，以实现
    get-or-create 语义。按条目的状态由条目自身的 ``asyncio.Lock`` 保护
    （从 MCP 事件循环线程中使用）。
    """

    def __init__(self) -> None:
        self._entries: dict[str, _ProviderEntry] = {}
        self._entries_lock = threading.Lock()

    # -- Provider 构建与缓存 ------------------------------------------------

    def get_or_build_provider(
        self,
        server_name: str,
        server_url: str,
        oauth_config: Optional[dict],
    ) -> Optional[Any]:
        """返回 ``server_name`` 的已缓存 OAuth provider，或构建一个。

        幂等：对同一名称的重复调用返回同一实例。若给定名称的
        ``server_url`` 发生变化，则丢弃缓存的条目并构建新的 provider。

        当 MCP SDK 的 OAuth 支持不可用时返回 None。
        """
        with self._entries_lock:
            entry = self._entries.get(server_name)
            if entry is not None and entry.server_url != server_url:
                logger.info(
                    "MCP OAuth '%s': URL changed from %s to %s, discarding cache",
                    server_name, entry.server_url, server_url,
                )
                entry = None

            if entry is None:
                entry = _ProviderEntry(
                    server_url=server_url,
                    oauth_config=oauth_config,
                )
                self._entries[server_name] = entry

            if entry.provider is None:
                entry.provider = self._build_provider(server_name, entry)

            return entry.provider

    def _build_provider(
        self,
        server_name: str,
        entry: _ProviderEntry,
    ) -> Optional[Any]:
        """构建底层 OAuth provider。

        直接使用从 ``tools.mcp_oauth`` 抽取出的辅助函数来构造
        :class:`HermesMCPOAuthProvider`。该子类注入了一个流程前的
        disk-watch 钩子，使外部 token 刷新（cron、其他 CLI 实例）对
        正在运行的 MCP 会话可见。

        当 MCP SDK 的 OAuth 支持不可用时返回 None。
        """
        if _HERMES_PROVIDER_CLS is None:
            logger.warning(
                "MCP OAuth '%s': SDK auth module unavailable", server_name,
            )
            return None

        # 本地导入以避免模块导入时的循环依赖。
        from tools.mcp_oauth import (
            HermesTokenStorage,
            OAuthNonInteractiveError,
            _OAUTH_AVAILABLE,
            _build_client_metadata,
            _configure_callback_port,
            _is_interactive,
            _maybe_preregister_client,
            _redirect_handler,
            _wait_for_callback,
        )

        if not _OAUTH_AVAILABLE:
            return None

        cfg = dict(entry.oauth_config or {})
        storage = HermesTokenStorage(server_name)

        if not _is_interactive() and not storage.has_cached_tokens():
            raise OAuthNonInteractiveError(
                "MCP OAuth for "
                f"'{server_name}': non-interactive environment and no "
                "cached tokens found. Run `hermes mcp login "
                f"{server_name}` interactively first to complete initial "
                "authorization."
            )

        _configure_callback_port(cfg)
        client_metadata = _build_client_metadata(cfg)
        _maybe_preregister_client(storage, cfg, client_metadata)

        return _HERMES_PROVIDER_CLS(
            server_name=server_name,
            server_url=entry.server_url,
            client_metadata=client_metadata,
            storage=storage,
            redirect_handler=_redirect_handler,
            callback_handler=_wait_for_callback,
            timeout=float(cfg.get("timeout", 300)),
        )

    def remove(self, server_name: str) -> None:
        """从缓存中驱逐 provider，并删除磁盘上的 token。

        由 ``hermes mcp remove <name>`` 调用，并（间接地）由
        ``hermes mcp login <name>`` 在强制重新认证时调用。
        """
        with self._entries_lock:
            self._entries.pop(server_name, None)

        from tools.mcp_oauth import remove_oauth_tokens
        remove_oauth_tokens(server_name)
        logger.info(
            "MCP OAuth '%s': evicted from cache and removed from disk",
            server_name,
        )

    # -- 磁盘监视 ------------------------------------------------------------

    async def invalidate_if_disk_changed(self, server_name: str) -> bool:
        """若磁盘上的 token 文件 mtime 比最近一次看到的更新，则强制
        MCP SDK provider 重载其内存状态。

        当缓存被失效（mtime 不同）时返回 True。这是外部刷新工作流的
        核心修复：一个 cron 任务把新 token 写到磁盘，正在运行的 MCP
        会话在下一次工具调用时就能获取它们而无需重启。
        """
        from tools.mcp_oauth import _get_token_dir, _safe_filename

        entry = self._entries.get(server_name)
        if entry is None or entry.provider is None:
            return False

        async with entry.lock:
            tokens_path = _get_token_dir() / f"{_safe_filename(server_name)}.json"
            try:
                mtime_ns = tokens_path.stat().st_mtime_ns
            except (FileNotFoundError, OSError):
                return False

            if mtime_ns != entry.last_mtime_ns:
                old = entry.last_mtime_ns
                entry.last_mtime_ns = mtime_ns
                # 强制 SDK 的 OAuthClientProvider 在下一次 auth flow 时
                # 从存储重载。`_initialized` 是私有 API，但在我们固定的
                # MCP SDK 版本（>=1.26.0）中是稳定的。
                if hasattr(entry.provider, "_initialized"):
                    entry.provider._initialized = False  # noqa: SLF001
                logger.info(
                    "MCP OAuth '%s': tokens file changed (mtime %d -> %d), "
                    "forcing reload",
                    server_name, old, mtime_ns,
                )
                return True
            return False

    # -- 401 handler（已去重）------------------------------------------------

    async def handle_401(
        self,
        server_name: str,
        failed_access_token: Optional[str] = None,
    ) -> bool:
        """处理来自工具调用的 401，在并发调用者之间去重。

        返回：
            True  若现在有一个（可能是新的）access token 可用 —— 调用者
                  应触发重连并重试该操作。
            False 若不存在恢复路径 —— 调用者应向模型呈现一个
                  ``needs_reauth`` 错误，使其停止臆想手动刷新尝试。

        惊群保护：若 N 个并发工具调用以同一个 ``failed_access_token``
        命中 401，只触发一次恢复尝试。其余调用等待同一个 future。
        """
        entry = self._entries.get(server_name)
        if entry is None or entry.provider is None:
            return False

        key = failed_access_token or "<unknown>"
        loop = asyncio.get_running_loop()

        async with entry.lock:
            pending = entry.pending_401.get(key)
            if pending is None:
                pending = loop.create_future()
                entry.pending_401[key] = pending

                async def _do_handle() -> None:
                    try:
                        # 第 1 步：磁盘是否变化？获取外部刷新。
                        disk_changed = await self.invalidate_if_disk_changed(
                            server_name
                        )
                        if disk_changed:
                            if not pending.done():
                                pending.set_result(True)
                            return

                        # 第 2 步：磁盘无变化 —— 若 SDK 能就地刷新，
                        # 则让调用者重试。SDK 的 httpx.Auth flow 会在
                        # 下一次请求时发起刷新。
                        provider = entry.provider
                        ctx = getattr(provider, "context", None)
                        can_refresh = False
                        if ctx is not None:
                            can_refresh_fn = getattr(ctx, "can_refresh_token", None)
                            if callable(can_refresh_fn):
                                try:
                                    can_refresh = bool(can_refresh_fn())
                                except Exception:
                                    can_refresh = False
                        if not pending.done():
                            pending.set_result(can_refresh)
                    except Exception as exc:  # pragma: no cover —— 防御性
                        logger.warning(
                            "MCP OAuth '%s': 401 handler failed: %s",
                            server_name, exc,
                        )
                        if not pending.done():
                            pending.set_result(False)
                    finally:
                        entry.pending_401.pop(key, None)

                asyncio.create_task(_do_handle())

        try:
            return await pending
        except Exception as exc:  # pragma: no cover —— 防御性
            logger.warning(
                "MCP OAuth '%s': awaiting 401 handler failed: %s",
                server_name, exc,
            )
            return False


# ---------------------------------------------------------------------------
# 模块级单例
# ---------------------------------------------------------------------------


_MANAGER: Optional[MCPOAuthManager] = None
_MANAGER_LOCK = threading.Lock()


def get_manager() -> MCPOAuthManager:
    """返回进程级的 :class:`MCPOAuthManager` 单例。"""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = MCPOAuthManager()
        return _MANAGER


def reset_manager_for_tests() -> None:
    """仅测试用：丢弃单例，使 fixture 从干净状态启动。"""
    global _MANAGER
    with _MANAGER_LOCK:
        _MANAGER = None
