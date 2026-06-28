"""外部密钥源集成。

密钥源是指任何能在进程启动时、_在_ ~/.hermes/.env 加载之后，提供环境变量形式凭据的来源。
默认情况下，密钥源是非破坏性的：它们只为尚未存在的环境变量设置值，因此 .env 和 shell 导出
仍然优先。

目前已包含：

  - ``bitwarden`` — Bitwarden Secrets Manager（`bws` CLI）。参见
    ``agent.secret_sources.bitwarden`` 了解集成实现，
    ``hermes_cli.secrets_cli`` 了解用户端设置向导。
"""
