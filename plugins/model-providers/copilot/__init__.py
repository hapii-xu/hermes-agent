"""Copilot / GitHub Models provider 配置文件。

Copilot 使用按模型的 api_mode 路由：
  - GPT-5+ / Codex 模型 → codex_responses
  - Claude 模型 → anthropic_messages
  - 其他所有模型 → chat_completions（此配置文件覆盖该子集）

chat_completions 子集的关键特点：
  - 编辑器归属 headers（通过 copilot_default_headers()）
  - GitHub Models 推理 extra_body（model-catalog 门控）
"""

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class CopilotProfile(ProviderProfile):
    """GitHub Copilot / GitHub Models — 编辑器 headers + 推理。"""

    def build_api_kwargs_extras(
        self,
        *,
        model: str | None = None,
        reasoning_config: dict | None = None,
        supports_reasoning: bool = False,
        **ctx,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        if supports_reasoning and model:
            try:
                from hermes_cli.models import github_model_reasoning_efforts

                supported_efforts = github_model_reasoning_efforts(model)
                if supported_efforts and reasoning_config:
                    effort = reasoning_config.get("effort", "medium")
                    # 将非标准的 effort 级别规范化为最接近的支持值
                    if effort == "xhigh":
                        effort = "high"
                    if effort in supported_efforts:
                        extra_body["reasoning"] = {"effort": effort}
                elif supported_efforts:
                    extra_body["reasoning"] = {"effort": "medium"}
            except Exception:
                pass
        return extra_body, {}


copilot = CopilotProfile(
    name="copilot",
    aliases=("github-copilot", "github-models", "github-model", "github"),
    env_vars=("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"),
    base_url="https://api.githubcopilot.com",
    auth_type="copilot",
)

register_provider(copilot)
