"""Microsoft Foundry provider 配置文件。

Azure Foundry 提供 OpenAI 兼容的端点；由于端点是按资源划分的，
用户需要在设置时提供自己的 base URL。
"""

from providers import register_provider
from providers.base import ProviderProfile

azure_foundry = ProviderProfile(
    name="azure-foundry",
    aliases=("azure", "azure-ai-foundry", "azure-ai"),
    display_name="Azure Foundry",
    description="Microsoft Foundry - OpenAI-compatible endpoint (user-supplied base URL)",
    signup_url="https://ai.azure.com/",
    env_vars=("AZURE_FOUNDRY_API_KEY", "AZURE_FOUNDRY_BASE_URL"),
    base_url="",  # 按资源划分；用户在设置时提供
    auth_type="api_key",
)

register_provider(azure_foundry)
