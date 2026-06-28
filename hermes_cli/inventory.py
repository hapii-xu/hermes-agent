"""Provider/model 库存上下文 —— dashboard ``/api/model/options``、TUI
``model.options``/``model.save_key`` JSON-RPC 处理器以及交互式选择器的共享底层模块。

在此模块之前，三个调用站点各自重复了以下逻辑：

1. 17 行的 config 切片，从 ``load_config()`` 中提取 ``model.{default,name,provider,base_url}``、
   ``providers:`` 和 ``custom_providers:``；
2. 用生成的 kwargs 调用 ``list_authenticated_providers``；
3. （仅 TUI）一个 45 行的后处理步骤，将已认证行与未配置的 ``CANONICAL_PROVIDERS`` 行合并，
   并为选择器 UI 输出 ``authenticated``/``auth_type``/``key_env``/``warning`` 提示。

将这三个步骤整合到一个入口点中，消除了重复代码隐藏的两个 bug：

- Dashboard 直接读取 ``cfg.get("custom_providers")``，遗漏了 v12+ 的带键
  ``providers:`` 格式（TUI 通过 ``get_compatible_custom_providers`` 正确处理了该格式）。
- TUI 的 canonical 合并以 ``is_user_defined`` 作为排序依据。而
  ``list_authenticated_providers`` 的第 3 节会对出现在 ``providers:`` 配置字典中的
  canonical slug 也设置 ``is_user_defined=True``，这会将它们悄悄降到选择器末尾。
  ``_reorder_canonical`` 改为依据 slug 成员身份排序。

底层事实（2026 年 5 月验证）：
- ``list_authenticated_providers`` 已经从精选目录中填充每行的 ``models``
  （与选择器使用相同数据源）。不要对每行调用 ``provider_model_ids()`` 来"刷新"
  —— 那会绕过精选机制并拉入非 agent 模型（Nous /models 返回约 400 个 ID，
  包括 TTS、embeddings、rerankers、image/video 生成器）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional


# ─── 公共类型 ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConfigContext:
    """每个库存调用方所需的 model + provider 配置快照。
    通过 ``load_picker_context()`` 构建一次；TUI 在传递前通过
    ``with_overrides()`` 叠加实时 agent 状态。
    """

    current_provider: str
    current_model: str
    current_base_url: str
    user_providers: dict
    custom_providers: list

    def with_overrides(
        self,
        *,
        current_provider: Optional[str] = None,
        current_model: Optional[str] = None,
        current_base_url: Optional[str] = None,
    ) -> "ConfigContext":
        """返回一个副本，应用真值覆盖。

        仅检查真值，因为 TUI 读取的 agent 属性在 agent 生成前可能是
        空字符串 —— 空值不能覆盖磁盘配置中的值。
        """
        kw: dict = {}
        if current_provider:
            kw["current_provider"] = current_provider
        if current_model:
            kw["current_model"] = current_model
        if current_base_url:
            kw["current_base_url"] = current_base_url
        return replace(self, **kw) if kw else self


def load_picker_context() -> ConfigContext:
    """加载每个消费者所需的磁盘配置快照。

    替代了 ``web_server.py`` 和 ``tui_gateway/server.py``（×2 处）
    以前内联的 17 行 config 切片。
    """
    from hermes_cli.config import get_compatible_custom_providers, load_config

    cfg = load_config()
    model_cfg = cfg.get("model", {})
    if isinstance(model_cfg, dict):
        current_model = model_cfg.get("default", model_cfg.get("name", "")) or ""
        current_provider = model_cfg.get("provider", "") or ""
        current_base_url = model_cfg.get("base_url", "") or ""
    else:
        # config.model 在旧配置中可能是纯字符串。
        current_model = str(model_cfg) if model_cfg else ""
        current_provider = ""
        current_base_url = ""
    raw = cfg.get("providers")
    return ConfigContext(
        current_provider=current_provider,
        current_model=current_model,
        current_base_url=current_base_url,
        user_providers=raw if isinstance(raw, dict) else {},
        custom_providers=get_compatible_custom_providers(cfg),
    )


# ─── 公共：payload 构建器 ────────────────────────────────────────────


def build_models_payload(
    ctx: ConfigContext,
    *,
    include_unconfigured: bool = False,
    picker_hints: bool = False,
    canonical_order: bool = False,
    pricing: bool = False,
    capabilities: bool = False,
    force_fresh_nous_tier: bool = False,
    refresh: bool = False,
    max_models: int | None = None,
) -> dict:
    """构建 ``{providers, model, provider}`` 结构，每个消费者
    从单次底层调用中都需要此形状。

    标志位：
    - ``include_unconfigured``：追加 ``list_authenticated_providers``
      未输出的 ``CANONICAL_PROVIDERS`` 行（TUI 用此标志在选择器中
      显示完整的 provider 集合）。
    - ``picker_hints``：为每行添加 ``authenticated``/``auth_type``/
      ``key_env``/``warning``（TUI ``ModelPickerDialog`` 形状）。
    - ``canonical_order``：将 canonical slug 行重排为
      ``CANONICAL_PROVIDERS`` 声明顺序；真正的自定义行放到最后
      （TUI 显示顺序）。
    - ``pricing``：为每行补充格式化的每模型定价，对 Nous 还添加
      ``free_tier``/``unavailable_models``，以便 GUI 选择器显示
      $/Mtok 列并在免费账户下限制付费模型 —— 与 ``hermes model``
      CLI 选择器一致。会增加网络调用（pricing 获取 + Nous 层级检查）；
      仅在交互式选择器中设置。
    - ``capabilities``：为每行添加 ``capabilities`` 映射
      ``{model: {fast, reasoning}}``，以便选择器根据每个模型实际支持
      的功能来限制 model-options 控件（fast 切换 / reasoning），
      而不是提供后端会拒绝的选项。
    - ``force_fresh_nous_tier``：在选择 Portal 推荐的 Nous 模型并
      应用层级限制时，绕过短期 Nous 免费层级缓存。UI 选择器打开时
      保持为 false；显式 auth/model 流程在需要新购额度立即生效时
      可以选择启用。
    - ``refresh``：清除每个 provider 的 model-id 磁盘缓存，使每行
      重新获取其实时目录。仅在显式用户触发的"刷新模型"操作中设置；
      普通选择器打开保持为 false 以在 1 小时缓存上保持响应速度。
    """
    from hermes_cli.model_switch import list_authenticated_providers

    rows = list_authenticated_providers(
        current_provider=ctx.current_provider,
        current_base_url=ctx.current_base_url,
        current_model=ctx.current_model,
        user_providers=ctx.user_providers,
        custom_providers=ctx.custom_providers,
        force_fresh_nous_tier=force_fresh_nous_tier,
        max_models=max_models,
        refresh=refresh,
    )

    # --- 去重：从聚合器中移除与用户定义 provider 重叠的模型。
    # 当本地代理（例如 litellm-proxy）提供的模型名称也出现在聚合器的
    # 精选目录中时，选择器会在两个 provider 下都显示该模型。
    # 从聚合器行选择它会将 model.provider 设置为聚合器
    # （例如 openrouter）而非用户的代理 —— 悄悄破坏了调用。
    # 在 payload 层级过滤可保持聚合器行的真实性：它们只显示用户
    # 无法从更具体的 provider 获取的模型。（#45954）
    try:
        from hermes_cli.providers import is_routing_aggregator as _is_routing_aggregator
    except Exception:
        _is_routing_aggregator = None  # type: ignore[assignment]

    if _is_routing_aggregator is not None:
        user_models: set[str] = set()
        for row in rows:
            if row.get("is_user_defined"):
                user_models.update(m.lower() for m in (row.get("models") or []))
        if user_models:
            for row in rows:
                # 用户自己配置的 provider 永远不是其自身的"聚合器
                # 重复"：user_models 就是从这些行构建的，而
                # is_routing_aggregator() 对每个 custom:* slug 都返回 True。
                # 没有此保护，去重会清空用户定义的自定义 provider 的
                # 整个模型列表（全部都在 user_models 中），使其选择器行为空。
                if row.get("is_user_defined"):
                    continue
                slug = row.get("slug", "")
                # 仅从真正的路由聚合器（OpenRouter、custom:* 代理）中移除重叠项。
                # 扁平命名空间的转售商（opencode-go / opencode-zen）将每个列出的模型
                # 作为第一方模型提供，因此它们的行必须保留与用户代理同名的模型 ——
                # 否则订阅 provider 自己的目录（minimax-m3、glm-5、deepseek-v4-flash 等）
                # 会在选择器中被悄悄清空。（#47077）
                if not _is_routing_aggregator(slug):
                    continue
                original = row.get("models") or []
                filtered = [m for m in original if m.lower() not in user_models]
                if len(filtered) < len(original):
                    row["models"] = filtered
                    row["total_models"] = len(filtered)

    if include_unconfigured:
        rows = list(rows) + _append_unconfigured_rows(rows, ctx)
    if picker_hints:
        _apply_picker_hints(rows)
    if canonical_order:
        rows = _reorder_canonical(rows)
    if pricing:
        _apply_pricing(rows, force_fresh_nous_tier=force_fresh_nous_tier)
    if capabilities:
        _apply_capabilities(rows)

    return {
        "providers": rows,
        "model": ctx.current_model,
        "provider": ctx.current_provider,
    }


def _apply_capabilities(rows: list[dict]) -> None:
    """为每个 provider 行附加 ``{model: {fast, reasoning}}`` 映射。

    `fast` 与 ``model_supports_fast_mode`` 一致（运行时执行的相同检查）。
    `reasoning` 来自 models.dev 目录（如果已知），否则默认为 True ——
    effort 旋钮被广泛接受且对忽略它的模型无影响，而对有能力但未收录的模型
    隐藏它则是更严重的失败。
    """
    from hermes_cli.models import model_supports_fast_mode

    try:
        from agent.models_dev import get_model_capabilities
    except Exception:
        get_model_capabilities = None  # type: ignore[assignment]

    for row in rows:
        slug = row.get("slug") or ""
        caps: dict[str, dict[str, bool]] = {}

        for model in row.get("models") or []:
            reasoning = True
            if get_model_capabilities is not None and slug:
                try:
                    meta = get_model_capabilities(slug, model)
                    if meta is not None:
                        reasoning = bool(meta.supports_reasoning)
                except Exception:
                    reasoning = True

            caps[model] = {
                "fast": bool(model_supports_fast_mode(model)),
                "reasoning": reasoning,
            }

        row["capabilities"] = caps


# ─── 内部：行后处理 ──────────────────────────────────────────────────


def _append_unconfigured_rows(rows: list[dict], ctx: ConfigContext) -> list[dict]:
    """为 ``rows`` 中缺失的 canonical provider 构建骨架行。"""
    from hermes_cli.models import CANONICAL_PROVIDERS, _PROVIDER_LABELS

    seen = {r["slug"].lower() for r in rows}
    cur = (ctx.current_provider or "").lower()
    extras: list[dict] = []
    for entry in CANONICAL_PROVIDERS:
        if entry.slug.lower() in seen:
            continue
        extras.append(
            {
                "slug": entry.slug,
                "name": _PROVIDER_LABELS.get(entry.slug, entry.label),
                "is_current": entry.slug.lower() == cur,
                "is_user_defined": False,
                "models": [],
                "total_models": 0,
                "source": "canonical",
            }
        )
    return extras


def _apply_picker_hints(rows: list[dict]) -> None:
    """为每行添加 ``authenticated``/``auth_type``/``key_env``/``warning``。

    就地修改 ``rows``。来自 ``list_authenticated_providers`` 的行
    已标记 ``authenticated=True``；来自 ``_append_unconfigured_rows``
    的未配置骨架行则获取选择器的设置提示形状。
    """
    from hermes_cli.auth import PROVIDER_REGISTRY

    for row in rows:
        if "authenticated" in row:
            continue
        # 区分已认证行（由 list_authenticated_providers 返回）
        # 和骨架行（来自 _append_unconfigured_rows）。骨架行具有
        # 空 `models` 且 source="canonical"；已认证行具有填充的
        # `models` 或非 canonical source。
        is_skeleton = row.get("source") == "canonical" and not row.get("models")
        row["authenticated"] = not is_skeleton
        if not is_skeleton or row.get("is_user_defined"):
            continue
        cfg = PROVIDER_REGISTRY.get(row["slug"])
        auth_type = cfg.auth_type if cfg else "api_key"
        key_env = (
            cfg.api_key_env_vars[0]
            if (cfg and cfg.api_key_env_vars)
            else ""
        )
        row["auth_type"] = auth_type
        row["key_env"] = key_env
        row["warning"] = (
            f"paste {key_env} to activate"
            if auth_type == "api_key" and key_env
            else f"run `hermes model` to configure ({auth_type})"
        )


def _reorder_canonical(rows: list[dict]) -> list[dict]:
    """Canonical slug 按 ``CANONICAL_PROVIDERS`` 声明顺序排列；
    真正的自定义行放到最后。

    依据 slug 成员身份排序，而非 ``is_user_defined`` ——
    ``list_authenticated_providers`` 的第 3 节会对来自 ``providers:``
    配置字典的行也设置 ``is_user_defined=True``，即使 slug 是 canonical 的。
    如果依据该标志排序，会悄悄将通过新的带键模式配置的 canonical provider
    降到末尾。
    """
    from hermes_cli.models import CANONICAL_PROVIDERS

    order = {e.slug: i for i, e in enumerate(CANONICAL_PROVIDERS)}
    canon = sorted(
        (r for r in rows if r["slug"] in order),
        key=lambda r: order[r["slug"]],
    )
    extras = [r for r in rows if r["slug"] not in order]
    return canon + extras


def _apply_pricing(
    rows: list[dict],
    *,
    force_fresh_nous_tier: bool = False,
) -> None:
    """为每个 provider 行补充每模型定价 + Nous 层级限制。

    就地修改 ``rows``。对于每个支持实时定价的 provider
    （openrouter / nous / novita），添加::

        row["pricing"] = {model_id: {"input": "$3.00", "output": "$15.00",
                                     "cache": "$0.30" | None, "free": bool}}

    对于 Nous 还额外添加::

        row["free_tier"] = bool            # 当前账户是否为免费层级
        row["unavailable_models"] = [...]  # 免费用户无法选择的付费模型

    价格通过 ``_format_price_per_mtok`` 预格式化，GUI 直接渲染字符串 ——
    与 CLI 选择器格式完全相同。所有失败都被静默处理（尽力而为）：
    行只是没有 ``pricing`` 键。
    """
    from hermes_cli.models import (
        _format_price_per_mtok,
        check_nous_free_tier,
        get_pricing_for_provider,
        partition_nous_models_by_tier,
    )

    # 一次性解析 Nous 免费层级（在 models.py 中按 TTL 窗口缓存）。
    nous_free_tier: Optional[bool] = None

    for row in rows:
        slug = str(row.get("slug", "")).lower()
        models = row.get("models") or []
        if not models:
            continue
        try:
            raw_pricing = get_pricing_for_provider(slug) or {}
        except Exception:
            raw_pricing = {}
        if not raw_pricing:
            continue

        formatted: dict[str, dict] = {}
        for mid in models:
            p = raw_pricing.get(mid)
            if not p:
                continue
            inp_raw = p.get("prompt", "")
            out_raw = p.get("completion", "")
            cache_raw = p.get("input_cache_read", "")
            inp = _format_price_per_mtok(inp_raw) if inp_raw != "" else ""
            out = _format_price_per_mtok(out_raw) if out_raw != "" else ""
            cache = _format_price_per_mtok(cache_raw) if cache_raw else None
            # 当 input 和 output 都免费时，模型为"免费"。
            is_free = inp == "free" and (out == "free" or out == "")
            formatted[mid] = {
                "input": inp,
                "output": out,
                "cache": cache,
                "free": is_free,
            }

        if formatted:
            row["pricing"] = formatted

        if slug == "nous":
            try:
                if nous_free_tier is None:
                    nous_free_tier = check_nous_free_tier(
                        force_fresh=force_fresh_nous_tier
                    )
                row["free_tier"] = bool(nous_free_tier)
                if nous_free_tier:
                    _selectable, unavailable = partition_nous_models_by_tier(
                        list(models), raw_pricing, free_tier=True
                    )
                    row["unavailable_models"] = unavailable
                else:
                    row["unavailable_models"] = []
            except Exception:
                # Tier detection failed — fail open (no gating) so the user
                # is never blocked from picking a model.
                row["free_tier"] = False
                row["unavailable_models"] = []
