"""``computer_use`` 捕获结果的视觉路由决策。

背景
----
``computer_use(action='capture', mode='som'|'vision')`` 返回一个
``_multimodal`` 封包，其中包含捕获的截图。该封包会作为工具结果回传给
**当前活动的会话模型**。当活动主模型没有视觉能力（例如纯文本模型，或仅
支持文本+代码的模型），或当活动的 provider 拒绝工具结果消息中的多模态
内容时，截图会在 provider 边界触发 404 / 400，agent 循环随之上报一个
硬性工具失败。

Issue #24015 报告了 ``cua-driver`` 后端的这一回归：在 ``config.yaml`` 中
配置 ``auxiliary.vision``（一个专用的具备视觉能力的模型）会被静默忽略
——截图仍然被路由到*主*模型，并以 HTTP 404 ``No endpoints found that
support image input`` 失败，即便配置里正坐着一个完全可用的视觉后端等待
被使用。

本模块集中处理这一小型策略决策：捕获的截图应作为多模态内容返回（由主
模型原生处理视觉），还是经由辅助视觉管线预先分析，使主模型只能看到
文本？

行为（与 ``vision_analyze`` 保持一致）
----------------------------------------
* 若用户显式配置了 ``auxiliary.vision``（``provider``、``model`` 或
  ``base_url`` 任意一个非空且不为 ``"auto"``），截图会经由辅助视觉管线
  路由。用户为专用视觉模型付费，通常就是希望用到它。
* 否则，若用户通过 ``model.supports_vision`` / provider 模型配置显式声明
  活动模型具备视觉能力，返回 ``False``。这是给自定义/本地 OpenAI 兼容
  VLM 路由的逃生口——这类路由在 models.dev 与 provider 白名单中查不到。
* 否则，若活动主模型+provider 能在工具结果消息中携带图片，且模型在
  models.dev 元数据中报告 ``supports_vision=True``，则返回 ``False``
  （走多模态路径）。
* 其余所有情况（非视觉主模型、不接受多模态工具结果的 provider、查找
  失败），都路由到辅助视觉，使主模型收到一段可操作的文本描述。

当元数据缺失或模糊时，该决策有意*偏向关闭*（即倾向于辅助路由）：把
截图返回给一个读不懂它的模型属于硬性工具失败，而经由辅助路由仅多花
一次 LLM 调用，且能产出可用的描述。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _explicit_aux_vision_override(cfg: Optional[Dict[str, Any]]) -> bool:
    """当 ``auxiliary.vision`` 携带非默认的用户覆盖时返回 True。

    与 ``agent.image_routing._explicit_aux_vision_override`` 保持一致，
    使捕获路径与用户附加图片路径对"什么算作对辅助视觉管线的显式用户请求"
    达成共识。``provider: "auto"``、空值或缺失的配置块都算作*非显式*。
    """
    if not isinstance(cfg, dict):
        return False
    aux = cfg.get("auxiliary") or {}
    if not isinstance(aux, dict):
        return False
    vision = aux.get("vision") or {}
    if not isinstance(vision, dict):
        return False

    provider = str(vision.get("provider") or "").strip().lower()
    model = str(vision.get("model") or "").strip()
    base_url = str(vision.get("base_url") or "").strip()

    if provider in ("", "auto") and not model and not base_url:
        return False
    return True


def _lookup_user_declared_supports_vision(
    provider: str,
    model: str,
    cfg: Optional[Dict[str, Any]],
) -> Optional[bool]:
    """返回当前路由由配置声明的 ``supports_vision``。"""
    try:
        from agent.image_routing import _supports_vision_override
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "computer_use vision_routing: config override lookup import failed: %s",
            exc,
        )
        return None
    try:
        return _supports_vision_override(cfg, provider, model)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "computer_use vision_routing: config override lookup failed: %s",
            exc,
        )
        return None


def _lookup_supports_vision(
    provider: str,
    model: str,
    cfg: Optional[Dict[str, Any]] = None,
) -> Optional[bool]:
    """返回 *(provider, model)* 由配置/models.dev 给出的 ``supports_vision``。"""
    if not provider or not model:
        return None
    try:
        from agent.image_routing import _lookup_supports_vision as _lookup_image_supports
    except Exception:
        _lookup_image_supports = None
    if _lookup_image_supports is not None:
        try:
            return _lookup_image_supports(provider, model, cfg)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "computer_use vision_routing: image-routing caps lookup failed "
                "for %s:%s — %s",
                provider, model, exc,
            )
            return None
    try:
        from agent.models_dev import get_model_capabilities
        caps = get_model_capabilities(provider, model)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "computer_use vision_routing: caps lookup failed for %s:%s — %s",
            provider, model, exc,
        )
        return None
    if caps is None:
        return None
    return bool(getattr(caps, "supports_vision", False))


def _provider_accepts_multimodal_tool_result(provider: str, model: str) -> Optional[bool]:
    """返回 *provider*+*model* 是否在工具结果消息中携带图片。

    复用 ``tools.vision_tools._supports_media_in_tool_results``，使捕获路由
    决策与 ``vision_analyze`` 的原生快速路径保持步调一致。导入失败时返回
    None，让调用方回退到辅助路由，而非猜测。
    """
    if not provider:
        return None
    try:
        from tools.vision_tools import _supports_media_in_tool_results
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "computer_use vision_routing: tool-result support lookup failed: %s",
            exc,
        )
        return None
    return bool(_supports_media_in_tool_results(provider, model))


def should_route_capture_to_aux_vision(
    provider: str,
    model: str,
    cfg: Optional[Dict[str, Any]],
) -> bool:
    """当捕获的截图应经由辅助视觉预先分析时返回 True。

    参数：
      provider: 当前推理 provider 的 id（例如 ``"openrouter"``、
        ``"anthropic"``、``"openai-codex"``）。规范化的小写 id。
      model:    当前主模型 slug，即发送给 provider 的形式。
      cfg:      已加载的 ``config.yaml`` 字典（或 None）。

    返回：
      当调用方应把截图交给辅助视觉管线（并对外暴露一个纯文本的工具结果）
      时为 ``True``。当调用方应保留既有的多模态封包（主模型原生处理视觉）
      时为 ``False``。
    """
    if _explicit_aux_vision_override(cfg):
        return True

    user_declared = _lookup_user_declared_supports_vision(provider, model, cfg)
    if user_declared is True:
        return False
    if user_declared is False:
        return True

    accepts_tool_image = _provider_accepts_multimodal_tool_result(provider, model)
    if accepts_tool_image is None or accepts_tool_image is False:
        return True

    supports_vision = _lookup_supports_vision(provider, model, cfg)
    if supports_vision is True:
        return False
    return True


__all__ = [
    "should_route_capture_to_aux_vision",
]
