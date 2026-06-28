"""Pet 生成 — 从基础草稿到孵化的流水线。

供 gateway RPC、CLI ``hermes pets generate`` 命令和测试使用的公开接口：

- :func:`generate_base_drafts` / :func:`hatch_pet` — 两步流程。
- :class:`HatchResult`、:class:`GenerationError`。
- :mod:`atlas` — 确定性的帧提取与 atlas 合成/校验。

图像生成委托给当前激活的、支持参考图的
:class:`~agent.image_gen_provider.ImageGenProvider`（OpenAI gpt-image-2 或 Krea）；
atlas 组装完全确定性，因此无需任何 API 调用即可测试。
"""

from __future__ import annotations

from agent.pet.generate.imagegen import GenerationError
from agent.pet.generate.orchestrate import (
    HatchResult,
    generate_base_drafts,
    hatch_pet,
)

__all__ = [
    "GenerationError",
    "HatchResult",
    "generate_base_drafts",
    "hatch_pet",
]
