"""本地 OpenAI 兼容代理，将请求转发至 OAuth 认证的上游服务。

让外部应用（OpenViking、Karakeep、Open WebUI 等）可以复用用户已登录的
provider 订阅，而无需在每个应用的配置中粘贴静态 API key。

代理监听 ``127.0.0.1:<port>``，接受任意 bearer（客户端的
``Authorization`` 头会被丢弃），并将用户的真实上游凭证附加到
转发请求中。凭证接近过期时会自动刷新。

一等 adapter：
  - ``nous`` — Nous Portal (https://inference-api.nousresearch.com/v1)

未来的 adapter 可以通过实现 ``UpstreamAdapter`` 来接入。
"""

from hermes_cli.proxy.adapters.base import UpstreamAdapter

__all__ = ["UpstreamAdapter"]
