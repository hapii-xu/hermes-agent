"""AWS Bedrock provider 配置文件。"""

from providers import register_provider
from providers.base import ProviderProfile


class BedrockProfile(ProviderProfile):
    """AWS Bedrock — 没有 REST /v1/models 端点；使用 AWS SDK。"""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Bedrock 模型列表需要 AWS SDK，而非 REST 调用。"""
        return None


bedrock = BedrockProfile(
    name="bedrock",
    aliases=("aws", "aws-bedrock", "amazon-bedrock", "amazon"),
    api_mode="bedrock_converse",
    env_vars=(),  # AWS SDK 凭证 — 非环境变量
    base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
    auth_type="aws_sdk",
)

register_provider(bedrock)
