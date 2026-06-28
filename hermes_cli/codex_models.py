"""从 API、本地缓存和配置中发现 Codex 模型。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Optional

import os

logger = logging.getLogger(__name__)

DEFAULT_CODEX_MODELS: List[str] = [
    "gpt-5.5",
    "gpt-5.4-mini",
    "gpt-5.4",
    "gpt-5.3-codex",
    # gpt-5.3-codex-spark 处于研究预览阶段，仅通过
    # Codex CLI / OAuth 后端（chatgpt.com/backend-api/codex/models）
    # 对 ChatGPT Pro 订阅用户开放。它不在公共 OpenAI
    # API 中提供，因此故意不包含在 hermes_cli/models.py 的
    # "openai" 提供者目录中——只有 openai-codex (OAuth) 提供者
    # 会展示它。Codex 后端对此 slug 报告 ``supported_in_api: false``；
    # 该标志描述的是 API 可用性，而非 Codex 后端
    # 可用性，因此下方的获取/缓存代码路径故意不
    # 根据它进行过滤。PR #12994 假设它不受支持而移除了此条目——
    # 那是错误的；在此恢复。将其保留在
    # 精选后备列表中，这样当实时发现不可用时（离线首次运行、
    # 临时 API 故障），Pro 用户仍然能在 `/model` 中看到 Spark。
    "gpt-5.3-codex-spark",
    # 注意：gpt-5.2-codex / gpt-5.1-codex-max / gpt-5.1-codex-mini 此前
    # 曾列在此处，但 chatgpt.com Codex 后端对所有我们测试过的
    # ChatGPT Pro 账户均返回 HTTP 400 "The '<model>' model is not supported
    # when using Codex with a ChatGPT account."（2026-05-27 实时验证）。
    # 将它们保留在后备列表中会在实时发现不可用时将无效的 slug
    # 泄漏到 /model（临时 API 故障、刷新前的首次运行），
    # 并在选择时触发 HTTP 400 崩溃。Codex CLI 公共目录仍然引用
    # 这些 slug，这就是它们之前存活的原因——但那些条目
    # 描述的是公共 OpenAI API，而非 Hermes 使用的 OAuth 支持的 Codex 后端。
    # 在此移除。如果 OpenAI 在 Codex 后端重新启用它们，
    # 实时发现会通过 _fetch_models_from_api 自动获取它们。
]

_FORWARD_COMPAT_TEMPLATE_MODELS: List[tuple[str, tuple[str, ...]]] = [
    ("gpt-5.5", ("gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex")),
    ("gpt-5.4-mini", ("gpt-5.3-codex",)),
    ("gpt-5.4", ("gpt-5.3-codex",)),
    # 只要存在任何兼容的 Codex 模板就展示 Spark，这样
    # 使用旧模型列表访问实时端点的账户仍然能在
    # 选择器中看到 Spark。后端根据 ChatGPT Pro
    # 权限控制实际可用性；Hermes 不做此限制。
    ("gpt-5.3-codex-spark", ("gpt-5.3-codex",)),
]


def _add_forward_compat_models(model_ids: List[str]) -> List[str]:
    """添加 Clawdbot 风格的合成向前兼容 Codex 模型。

    如果较新的 Codex slug 未被实时发现返回，则在存在
    较旧的兼容模板模型时展示它。这镜像了 Clawdbot 的
    合成目录 / 向前兼容行为，适用于 GPT-5 Codex 变体。
    """
    ordered: List[str] = []
    seen: set[str] = set()
    for model_id in model_ids:
        if model_id not in seen:
            ordered.append(model_id)
            seen.add(model_id)

    for synthetic_model, template_models in _FORWARD_COMPAT_TEMPLATE_MODELS:
        if synthetic_model in seen:
            continue
        if any(template in seen for template in template_models):
            ordered.append(synthetic_model)
            seen.add(synthetic_model)

    return ordered


def _fetch_models_from_api(access_token: str) -> List[str]:
    """从 Codex API 获取可用模型。返回按优先级排序的可见模型列表。"""
    try:
        import httpx
        resp = httpx.get(
            "https://chatgpt.com/backend-api/codex/models?client_version=1.0.0",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        entries = data.get("models", []) if isinstance(data, dict) else []
    except Exception as exc:
        logger.debug("Failed to fetch Codex models from API: %s", exc)
        return []

    sortable = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        slug = slug.strip()
        # Codex CLI 的目录使用 ``supported_in_api`` 表示公共 OpenAI
        # API 的可用性，而非此提供者使用的 OAuth 支持的 Codex 后端。
        # 一些有效的 Codex CLI 模型（例如 gpt-5.3-codex-spark）
        # 在此标记为 false，但仍可被 Codex 路由接受。
        visibility = item.get("visibility", "")
        if isinstance(visibility, str) and visibility.strip().lower() in {"hide", "hidden"}:
            continue
        priority = item.get("priority")
        rank = int(priority) if isinstance(priority, (int, float)) else 10_000
        sortable.append((rank, slug))

    sortable.sort(key=lambda x: (x[0], x[1]))
    return _add_forward_compat_models([slug for _, slug in sortable])


def _read_default_model(codex_home: Path) -> Optional[str]:
    config_path = codex_home / "config.toml"
    if not config_path.exists():
        return None
    try:
        import tomllib
    except Exception:
        return None
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    model = payload.get("model") if isinstance(payload, dict) else None
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None


def _read_cache_models(codex_home: Path) -> List[str]:
    cache_path = codex_home / "models_cache.json"
    if not cache_path.exists():
        return []
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    entries = raw.get("models") if isinstance(raw, dict) else None
    sortable = []
    if isinstance(entries, list):
        for item in entries:
            if not isinstance(item, dict):
                continue
            slug = item.get("slug")
            if not isinstance(slug, str) or not slug.strip():
                continue
            slug = slug.strip()
            # 不要在此根据 ``supported_in_api`` 过滤。它描述的是
            # 公共 OpenAI API，而 Hermes openai-codex 与 Codex CLI 一样
            # 访问同一个 OAuth 支持的 Codex 后端。
            visibility = item.get("visibility")
            if isinstance(visibility, str) and visibility.strip().lower() in {"hide", "hidden"}:
                continue
            priority = item.get("priority")
            rank = int(priority) if isinstance(priority, (int, float)) else 10_000
            sortable.append((rank, slug))

    sortable.sort(key=lambda item: (item[0], item[1]))
    deduped: List[str] = []
    for _, slug in sortable:
        if slug not in deduped:
            deduped.append(slug)
    return deduped


def get_codex_model_ids(access_token: Optional[str] = None) -> List[str]:
    """返回可用的 Codex 模型 ID，优先尝试 API，然后尝试本地源。

    解析顺序：API（如果提供了 token 则实时查询）> config.toml 默认值 >
    本地缓存 > 硬编码默认值。
    """
    codex_home_str = os.getenv("CODEX_HOME", "").strip() or str(Path.home() / ".codex")
    codex_home = Path(codex_home_str).expanduser()
    ordered: List[str] = []

    # 如果有 token 则尝试实时 API
    if access_token:
        api_models = _fetch_models_from_api(access_token)
        if api_models:
            return _add_forward_compat_models(api_models)

    # 回退到本地源
    default_model = _read_default_model(codex_home)
    if default_model:
        ordered.append(default_model)

    for model_id in _read_cache_models(codex_home):
        if model_id not in ordered:
            ordered.append(model_id)

    for model_id in DEFAULT_CODEX_MODELS:
        if model_id not in ordered:
            ordered.append(model_id)

    return _add_forward_compat_models(ordered)
