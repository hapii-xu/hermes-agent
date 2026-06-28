"""Microsoft Foundry 的 Microsoft Entra ID 适配器。

使用 `azure-identity` SDK 的 `DefaultAzureCredential` 链为 Microsoft Foundry 部署
提供无密钥身份验证（环境服务主体 → 工作负载身份 → 托管身份 → VS Code →
Azure CLI → azd → PowerShell → broker）。

架构镜像 `agent/bedrock_adapter.py`：

* 延迟导入。仅当选择 ``model.auth_mode = entra_id`` 时才加载 `azure-identity`。
  坚持使用 `AZURE_FOUNDRY_API_KEY` 的用户永远不会支付导入成本。
* SDK 可调用合约。公共入口点 ``build_token_provider`` 返回由
  ``get_bearer_token_provider`` 生成的零参数可调用对象 — 这正是 Microsoft
  记录的示例插件到 ``OpenAI(api_key=token_provider, base_url=...)`` 的值。
  OpenAI SDK 在每次请求前调用它，因此令牌刷新是透明的。
* 三个显式的消费端辅助函数（显示/缓存/http-bearer），而不是一个通用的
  "具体化"函数 — 按目的拆分可以防止在日志路径中意外铸造令牌或将令牌泄漏到缓存键/仪表板 JSON 中。
* 没有持久的 JWT。``azure-identity`` 在进程内缓存（并且在可用时）在操作系统密钥链
  或 ``~/.IdentityService`` 中。Hermes 不会在 ``auth.json`` 中重复该存储。

参考：https://learn.microsoft.com/azure/ai-foundry/foundry-models/how-to/configure-entra-id

要求：``azure-identity``（可选依赖 — 仅在 ``model.auth_mode = entra_id`` 时需要）。
"""

from __future__ import annotations

import functools
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Microsoft 记录的 Foundry 推理身份验证范围。新的 Foundry 门户和旧的 Azure OpenAI
# 托管身份文档都使用此范围用于所有 Foundry 端点形状（*.openai.azure.com、
# *.services.ai.azure.com、*.ai.azure.com）。较旧的控制平面范围
# ``https://cognitiveservices.azure.com/.default`` 用于 ARM 资源管理，
# 并被较新的资源拒绝用于推理 — 具有该要求的用户通过 config.yaml 中的
# ``model.entra.scope`` 覆盖。
SCOPE_AI_AZURE_DEFAULT = "https://ai.azure.com/.default"

# ---------------------------------------------------------------------------
# 延迟 SDK 导入 — 仅在实际使用 Entra 路径时加载。
# ---------------------------------------------------------------------------

_AZURE_IDENTITY_FEATURE = "provider.azure_identity"


def has_azure_identity_installed() -> bool:
    """如果现在可以导入 `azure-identity`，则返回 True。

    廉价检查 — 不会遍历凭据链。
    """
    try:
        import azure.identity  # noqa: F401
        return True
    except Exception:
        return False


def _require_azure_identity():
    """导入 ``azure.identity``，如果允许则延迟安装。

    当包缺失且禁用延迟安装时，抛出 ``ImportError`` 并附带清晰的可行消息。
    """
    try:
        import azure.identity as _ai
        return _ai
    except ImportError:
        try:
            from tools.lazy_deps import ensure, FeatureUnavailable
        except ImportError as exc:
            raise ImportError(
                "需要 'azure-identity' 包用于 Azure AI "
                "Foundry Entra ID 身份验证。使用以下命令安装："
                "pip install azure-identity"
            ) from exc

        try:
            ensure(_AZURE_IDENTITY_FEATURE, prompt=False)
        except FeatureUnavailable as exc:
            raise ImportError(
                "需要 'azure-identity' 包用于 Azure AI "
                "Foundry Entra ID 身份验证。 " + str(exc)
            ) from exc

        # 延迟安装后重试导入。
        import azure.identity as _ai  # noqa: WPS440
        return _ai


def reset_credential_cache() -> None:
    """清除缓存的 ``DefaultAzureCredential``。由测试和配置文件切换使用。

    防御性测试那些使用普通（非 lru 缓存）函数对 ``build_credential`` 进行
    ``monkeypatch.setattr`` 的测试 — 在 pytest 恢复补丁之前，这些测试不会公开
    ``cache_clear()``。
    """
    cache_clear = getattr(build_credential, "cache_clear", None)
    if callable(cache_clear):
        cache_clear()


# ---------------------------------------------------------------------------
# 令牌提供程序构造
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntraIdentityConfig:
    """可序列化的 Entra ID 配置。

    捕获我们在 Azure SDK 环境配置之外需要的 Hermes 管理的 Entra 旋钮。
    其他所有内容（租户 ID、服务主体密钥、联合令牌文件、主权云机构等）
    通过 azure-identity 的标准 ``AZURE_*`` 环境变量流动 — 参见
    ``hermes_cli/runtime_provider.py:1310-1377`` 中的 Bedrock 模式以了解类似的
    "让 SDK 读取环境" 方法。

    ``scope`` 是 Microsoft 记录的 Foundry 推理受众。几乎每个人都使用默认值；
    主权云/非标准租户可以通过 ``model.entra.scope`` 覆盖。
    身份选择（用户分配的托管身份、工作负载身份、服务主体、租户、机构）
    保留在标准 Azure SDK 环境变量中，如 ``AZURE_CLIENT_ID``。

    ``exclude_interactive_browser`` 保留为内部构造旋钮，以便探测默认保持非交互式。
    安装向导不会写入此字段。

    数据类是冻结的，因此它可以作为 ``functools.lru_cache`` 键的哈希值，
    并可跨多进程边界序列化（工作进程在自己的进程中重建凭据）。
    """

    scope: str = SCOPE_AI_AZURE_DEFAULT
    exclude_interactive_browser: bool = True

    def __post_init__(self) -> None:
        scope = str(self.scope or "").strip() or SCOPE_AI_AZURE_DEFAULT
        object.__setattr__(self, "scope", scope)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.scope,
            "exclude_interactive_browser": self.exclude_interactive_browser,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]],
                  *, default_scope: Optional[str] = None) -> "EntraIdentityConfig":
        data = data or {}
        scope = str(data.get("scope") or "").strip() or default_scope or SCOPE_AI_AZURE_DEFAULT
        exclude_browser = bool(data.get("exclude_interactive_browser", True))
        return cls(
            scope=scope,
            exclude_interactive_browser=exclude_browser,
        )


def _build_default_credential(config: EntraIdentityConfig) -> Any:
    """为 ``config`` 构造 ``DefaultAzureCredential``。

    仅将 Hermes 选择的旋钮作为 kwargs 传递。其他所有内容（租户、服务主体密钥、
    联合令牌文件、主权云机构等）由 ``azure-identity`` 从标准 ``AZURE_*``
    环境变量读取 — 参见 Microsoft 记录的凭据解析链。
    用户在 ``~/.hermes/.env`` 或部署环境中配置这些。
    """
    ai = _require_azure_identity()
    kwargs: Dict[str, Any] = {}
    # SDK 默认为 True（排除浏览器）；仅当用户明确选择加入交互式浏览器身份验证时才传递。
    if not config.exclude_interactive_browser:
        kwargs["exclude_interactive_browser_credential"] = False
    return ai.DefaultAzureCredential(**kwargs)


@functools.lru_cache(maxsize=1)
def build_credential(config: EntraIdentityConfig) -> Any:
    """返回 ``config`` 的缓存 ``DefaultAzureCredential``。

    Hermes 进程一次只使用一个 Entra 配置（config.yaml 中的 ``model.entra.*``
    块驱动会话中的每个辅助任务、子代理和凭据探测）。
    ``maxsize=1`` 是有意的：它反映了实际的使用模式，并使缓存保持微小的状态。

    ``EntraIdentityConfig`` 是一个冻结的数据类，因此它是可哈希的，可以安全地
    作为 LRU 缓存键。``functools.lru_cache`` 在 CPython 中是线程安全的。

    如果传递了两个不同的配置（测试会这样做；生产环境很少这样做），LRU 驱逐
    会正确处理它 — 每次调用仍返回与其配置匹配的凭据；一次只缓存一个。
    使用 :func:`reset_credential_cache` 清除（例如在测试中）。
    """
    return _build_default_credential(config)


def build_token_provider(scope: Optional[str] = None,
                         *,
                         config: Optional[EntraIdentityConfig] = None,
                         base_url: Optional[str] = None,
                         exclude_interactive_browser: bool = True,
                         ) -> Callable[[], str]:
    """返回一个铸造新鲜 Entra 承载 JWT 的零参数可调用对象。

    返回的可调用对象正是 Microsoft 记录的 Foundry 示例所期望的::

        from openai import OpenAI
        client = OpenAI(
            base_url="https://my-resource.openai.azure.com/openai/v1/",
            api_key=build_token_provider(),
        )

    范围解析顺序：
      1. 提供配置对象时的 ``config.scope``
      2. 显式 ``scope`` kwarg
      3. ``SCOPE_AI_AZURE_DEFAULT``（Microsoft 记录的 Foundry 范围）

    ``base_url`` 目前未使用，保留用于向后兼容。
    租户/服务主体/主权云配置通过 ``azure-identity`` 的标准 ``AZURE_*``
    环境变量流动 — 参见 :func:`_build_default_credential` 了解基本原理。

    不可跨进程边界序列化。对于多进程工作进程，序列化 ``EntraIdentityConfig``
    并在工作进程内重建提供程序。
    """
    ai = _require_azure_identity()
    if config is None:
        config = EntraIdentityConfig(
            scope=scope or SCOPE_AI_AZURE_DEFAULT,
            exclude_interactive_browser=exclude_interactive_browser,
        )
    credential = build_credential(config)
    return ai.get_bearer_token_provider(credential, config.scope)


# ---------------------------------------------------------------------------
# 凭据探测
# ---------------------------------------------------------------------------


def has_azure_identity_credentials(scope: Optional[str] = None,
                                   *,
                                   config: Optional[EntraIdentityConfig] = None,
                                   timeout_seconds: float = 10.0,
                                   allow_install: bool = True,
                                   **overrides: Any) -> bool:
    """最佳努力探测：`DefaultAzureCredential` 现在可以铸造令牌吗？

    在基于线程的超时下运行 ``credential.get_token(scope)``，以便慢速令牌服务
    不会挂起调用者。在任何错误时返回 False — 永不抛出。
    用于 ``hermes doctor`` / ``hermes auth status`` / 向导预飞。

    ``allow_install``：当为 True（默认）且 ``azure-identity`` 不可导入时，
    适配器在探测前触​​发标准的延迟安装路径（受 ``security.allow_lazy_installs`` 约束）。
    设置为 False 以使其成为严格的"已安装？"检查 — 用于我们不希望运行 pip 的热路径，
    如 CLI 启动。

    不被 ``is_provider_configured()`` 使用 — 该路径仅是结构性的（无令牌铸造），
    因此 CLI 启动不会支付此延迟。
    """
    if not has_azure_identity_installed():
        if not allow_install:
            return False
        try:
            _require_azure_identity()
        except ImportError as exc:
            logger.debug("azure-identity 延迟安装不可用：%s", exc)
            return False
    if config is None:
        effective_scope = (scope or "").strip() or SCOPE_AI_AZURE_DEFAULT
        config = EntraIdentityConfig(scope=effective_scope, **overrides)

    result = {"ok": False}

    def _probe() -> None:
        try:
            credential = build_credential(config)
            tok = credential.get_token(config.scope)
            result["ok"] = bool(getattr(tok, "token", None))
        except Exception as exc:
            logger.debug("Entra 凭据探测失败：%s", exc)
            result["ok"] = False

    thread = threading.Thread(target=_probe, daemon=True)
    thread.start()
    thread.join(timeout=max(0.01, timeout_seconds))
    if thread.is_alive():
        logger.debug("Entra 令牌服务探测在 %ss 后超时", timeout_seconds)
        return False
    return bool(result.get("ok"))


def describe_active_credential(config: Optional[EntraIdentityConfig] = None,
                               *,
                               scope: Optional[str] = None,
                               timeout_seconds: float = 10.0,
                               allow_install: bool = True,
                               **overrides: Any) -> Dict[str, Any]:
    """返回关于活动凭据链的诊断信息。

    最佳努力：运行 ``get_token()`` 并检查返回的内容。
    为 ``hermes doctor`` 和向导预飞设计 — 永不抛出，失败时返回
    ``{"ok": False, "error": ...}``。

    ``allow_install``：当为 True（默认）且 ``azure-identity`` 不可导入时，
    适配器在探测前触​​发标准的延迟安装路径。安装失败时作为诊断错误公开。
    为不应触发 pip 的热 CLI 路径设置 False。

    ``azure-identity`` 不将获胜的内部凭据公开为公共字段，因此我们报告粗略的图片
    （存在环境变量、令牌到期、从声明派生的租户），而不是凭据类名称。
    想要精确类名的用户可以运行 ``AZURE_LOG_LEVEL=DEBUG``。
    """
    info: Dict[str, Any] = {"ok": False}
    if not has_azure_identity_installed():
        if not allow_install:
            info["error"] = "未安装 azure-identity"
            info["hint"] = (
                "pip install azure-identity（或依赖首次使用时的延迟安装）"
            )
            return info
        try:
            _require_azure_identity()
        except ImportError as exc:
            info["error"] = str(exc) or "未安装 azure-identity"
            info["hint"] = (
                "手动 pip install azure-identity，或启用延迟安装"
                "（config.yaml 中的 security.allow_lazy_installs: true）。"
            )
            return info

    if config is None:
        effective_scope = (scope or "").strip() or SCOPE_AI_AZURE_DEFAULT
        config = EntraIdentityConfig(scope=effective_scope, **overrides)

    info["scope"] = config.scope
    # 租户/机构/服务主体配置通过标准 ``AZURE_*`` 环境变量流动；在下面公开它们。
    if os.environ.get("AZURE_TENANT_ID", "").strip():
        info["tenant_id_env"] = os.environ["AZURE_TENANT_ID"].strip()

    # 在尚未铸造的情况下公开存在哪些环境变量源。
    env_sources = []
    if os.environ.get("AZURE_FEDERATED_TOKEN_FILE", "").strip():
        env_sources.append("WorkloadIdentityCredential (AZURE_FEDERATED_TOKEN_FILE)")
    if (os.environ.get("AZURE_CLIENT_ID", "").strip()
            and os.environ.get("AZURE_CLIENT_SECRET", "").strip()
            and os.environ.get("AZURE_TENANT_ID", "").strip()):
        env_sources.append("EnvironmentCredential（客户端密钥）")
    if os.environ.get("IDENTITY_ENDPOINT", "").strip() or os.environ.get("MSI_ENDPOINT", "").strip():
        env_sources.append("ManagedIdentityCredential (IDENTITY_ENDPOINT)")
    info["env_sources"] = env_sources

    # 现在尝试铸造。
    result: Dict[str, Any] = {}

    def _probe() -> None:
        try:
            credential = build_credential(config)
            tok = credential.get_token(config.scope)
            result["token"] = tok
        except Exception as exc:
            result["error"] = str(exc)

    thread = threading.Thread(target=_probe, daemon=True)
    thread.start()
    thread.join(timeout=max(0.01, timeout_seconds))
    if thread.is_alive():
        info["error"] = f"令牌探测在 {timeout_seconds:.0f}s 后超时"
        info["hint"] = (
            "当令牌服务不可达或 az 登录状态过时时，DefaultAzureCredential 可能会变慢。"
            "尝试 `az login` 或设置 AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_CLIENT_SECRET。"
        )
        return info

    if "error" in result:
        info["error"] = result["error"]
        return info

    token = result.get("token")
    if token is None:
        info["error"] = "凭据链已耗尽"
        return info

    info["ok"] = True
    info["expires_on"] = getattr(token, "expires_on", None)
    return info


# ---------------------------------------------------------------------------
# 消费端辅助函数 — 按目的拆分以防止在日志/缓存键/仪表板路径中意外铸造令牌。
# ---------------------------------------------------------------------------


def is_token_provider(value: Any) -> bool:
    """当 ``value`` 是可调用的 Entra 令牌提供程序时返回 True。

    在消费者必须决定字符串 API 密钥语义和承载可调用语义之间的接缝处使用。
    """
    return callable(value) and not isinstance(value, str)


def materialize_bearer_for_http(value: Any) -> str:
    """返回用于手动 HTTP 请求的新鲜承载 JWT。

    仅在必须在 OpenAI SDK 之外构造 ``Authorization`` 标头的站点调用此函数
   （例如 ``hermes_cli/azure_detect.py``）。恰好调用可调用对象一次并返回结果令牌。

    **Anthropic SDK 集成：** Anthropic Python SDK 不接受 ``Callable[[], str]``
    用于 ``auth_token``。相反，:func:`build_bearer_http_client` 返回一个
    ``httpx.Client``，其请求事件挂钩调用此函数并在每次请求时重写
    ``Authorization`` 标头 — 该客户端通过 ``http_client=...`` 传递给 Anthropic SDK。
    参见 :func:`agent.anthropic_adapter.build_anthropic_client` 了解消费者。

    如果 ``value`` 不是可调用的令牌提供程序或非空字符串，则抛出 ``ValueError``。
    """
    if is_token_provider(value):
        token = value()
        if not isinstance(token, str) or not token:
            raise ValueError("令牌提供程序返回了空值")
        return token
    if isinstance(value, str) and value:
        return value
    raise ValueError("没有可用的 api_key / 令牌提供程序")


def build_bearer_http_client(token_provider: Callable[[], str], **httpx_kwargs: Any) -> Any:
    """返回一个 ``httpx.Client``，它为每个出站请求铸造新鲜的 Entra 承载 JWT。

    Anthropic SDK（撰写时 ≤ 0.86.0）将 ``api_key`` / ``auth_token`` 存储为静态字符串，
    并在构造时计算 ``Authorization`` 标头。为了获得每请求令牌刷新
    （Microsoft 推荐的用于可调用承载提供程序的 Foundry 模式），
    我们在自定义客户端上安装 httpx ``request`` 事件挂钩，
    并通过 ``http_client=...`` 将该客户端传递给 SDK。挂钩：

      1. 调用 :func:`materialize_bearer_for_http` 铸造新鲜 JWT（azure-identity 在
         内部缓存 — 当缓存的令牌仍然有效时，这很便宜）。
      2. 删除 SDK 可能已添加的任何预设 ``Authorization`` / ``api-key`` /
         ``x-api-key`` 标头（避免冲突的身份验证值）。
      3. 设置 ``Authorization: Bearer <fresh-jwt>``。

    ``token_provider`` 必须是返回字符串的零参数可调用对象 — 通常是
    :func:`build_token_provider` 的结果。

    ``httpx_kwargs`` 原样转发到 ``httpx.Client(...)``，以便调用者可以附加
    ``timeout``、``transport``、``proxy`` 等。

    如果未安装 ``httpx``，则抛出 ``ImportError``（它是 ``openai`` 和 ``anthropic``
    SDK 的传递依赖，因此在实践中，当达到此辅助函数时总是可用的）。
    """
    if not is_token_provider(token_provider):
        raise ValueError(
            "build_bearer_http_client 需要零参数可调用令牌提供程序"
        )

    try:
        import httpx
    except ImportError as exc:  # pragma: no cover — httpx 与 openai/anthropic 一起提供
        raise ImportError(
            "Microsoft Foundry Anthropic 风格端点的 Entra ID 承载身份验证需要 httpx。"
            "它通常是 openai/anthropic SDK 的传递依赖。"
        ) from exc

    def _inject_bearer(request: "httpx.Request") -> None:
        try:
            token = materialize_bearer_for_http(token_provider)
        except ValueError as exc:
            # 令牌提供程序失败（链已耗尽、令牌服务不可达、az 登录过期等）。
            # 删除 SDK 可能已设置的任何身份验证标头 — 包括我们自己的占位符哨兵
            # ``entra-id-bearer-via-http-hook``（来自
            # ``_build_anthropic_client_with_bearer_hook``）— 以便出站请求使用
            # 无授权命中 Azure，而不是使用占位符。Azure 返回干净的 401"缺少授权"，
            # 比针对哨兵字符串的 401 更容易诊断，并且哨兵永远不会出现在上游访问日志中。
            #
            # 以 WARNING（而不是 DEBUG）记录，以便在默认日志级别可见错误配置。
            logger.warning(
                "承载挂钩：Entra ID 令牌提供程序返回空值（%s）"
                "— 正在删除 Authorization 标头。Azure 将响应 401。"
                "运行 `hermes doctor` 或 `az login` 以恢复。",
                exc,
            )
            for header_name in ("Authorization", "authorization", "Api-Key", "api-key", "X-Api-Key", "x-api-key"):
                request.headers.pop(header_name, None)
            return
        for header_name in ("Authorization", "authorization", "Api-Key", "api-key", "X-Api-Key", "x-api-key"):
            request.headers.pop(header_name, None)
        request.headers["Authorization"] = f"Bearer {token}"

    return httpx.Client(
        event_hooks={"request": [_inject_bearer]},
        **httpx_kwargs,
    )


__all__ = [
    "EntraIdentityConfig",
    "SCOPE_AI_AZURE_DEFAULT",
    "build_bearer_http_client",
    "build_credential",
    "build_token_provider",
    "describe_active_credential",
    "has_azure_identity_credentials",
    "has_azure_identity_installed",
    "is_token_provider",
    "materialize_bearer_for_http",
    "reset_credential_cache",
]
