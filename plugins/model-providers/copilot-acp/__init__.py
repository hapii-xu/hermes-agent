"""GitHub Copilot ACP provider 配置文件。

copilot-acp 使用外部 ACP 子进程 — 而非标准
传输。api_mode="copilot_acp" 在 run_agent.py 中单独处理。
该配置文件为注册表迁移捕获 auth + 端点元数据。
"""

from providers import register_provider
from providers.base import ProviderProfile


class CopilotACPProfile(ProviderProfile):
    """GitHub Copilot ACP — 外部进程，没有 REST models 端点。"""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """模型列表由 ACP 子进程处理。"""
        return None


copilot_acp = CopilotACPProfile(
    name="copilot-acp",
    aliases=("github-copilot-acp", "copilot-acp-agent"),
    api_mode="chat_completions",  # ACP 子进程使用 chat_completions 路由
    env_vars=(),  # 由 ACP 子进程管理
    base_url="acp://copilot",  # ACP 内部 scheme
    auth_type="external_process",
)

register_provider(copilot_acp)
