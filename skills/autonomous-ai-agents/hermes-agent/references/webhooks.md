# Webhook 订阅

创建动态 webhook 订阅，让外部服务（GitHub、GitLab、Stripe、CI/CD、IoT 传感器、监控工具）可以通过向某个 URL POST 事件来触发 Hermes agent 运行。

## 设置（必须先做）

在创建订阅前必须先启用 webhook 平台。用以下命令检查：
```bash
hermes webhook list
```

如果提示 "Webhook platform is not enabled"，按以下方式设置：

### 选项 1：设置向导
```bash
hermes gateway setup
```
按提示启用 webhook、设置端口并设置一个全局 HMAC 密钥。

### 选项 2：手动配置
添加到 `~/.hermes/config.yaml`：
```yaml
platforms:
  webhook:
    enabled: true
    extra:
      host: "0.0.0.0"
      port: 8644
      secret: "generate-a-strong-secret-here"
```

### 选项 3：环境变量
添加到 `${HERMES_HOME:-~/.hermes}/.env`：
```bash
WEBHOOK_ENABLED=true
WEBHOOK_PORT=8644
WEBHOOK_SECRET=generate-a-strong-secret-here
```

配置后，启动（或重启）网关：
```bash
hermes gateway run
# 或，若使用 systemd：
systemctl --user restart hermes-gateway
```

验证它在运行：
```bash
curl http://localhost:8644/health
```

## 命令

所有管理都通过 `hermes webhook` CLI 命令进行：

### 创建订阅
```bash
hermes webhook subscribe <name> \
  --prompt "Prompt template with {payload.fields}" \
  --events "event1,event2" \
  --description "What this does" \
  --skills "skill1,skill2" \
  --deliver telegram \
  --deliver-chat-id "12345" \
  --secret "optional-custom-secret"
```

返回 webhook URL 和 HMAC 密钥。用户配置其服务向该 URL 发送 POST。

### 列出订阅
```bash
hermes webhook list
```

### 移除订阅
```bash
hermes webhook remove <name>
```

### 测试订阅
```bash
hermes webhook test <name>
hermes webhook test <name> --payload '{"key": "value"}'
```

## 提示词模板

提示词支持 `{dot.notation}` 来访问嵌套的 payload 字段：

- `{issue.title}` —— GitHub issue 标题
- `{pull_request.user.login}` —— PR 作者
- `{data.object.amount}` —— Stripe 支付金额
- `{sensor.temperature}` —— IoT 传感器读数

若未指定提示词，完整的 JSON payload 会被转储进 agent 提示词。

## 常见模式

### GitHub：新 issue
```bash
hermes webhook subscribe github-issues \
  --events "issues" \
  --prompt "New GitHub issue #{issue.number}: {issue.title}\n\nAction: {action}\nAuthor: {issue.user.login}\nBody:\n{issue.body}\n\nPlease triage this issue." \
  --deliver telegram \
  --deliver-chat-id "-100123456789"
```

然后在 GitHub 仓库 Settings → Webhooks → Add webhook：
- Payload URL：返回的 webhook_url
- Content type：application/json
- Secret：返回的 secret
- Events："Issues"

### GitHub：PR 评审
```bash
hermes webhook subscribe github-prs \
  --events "pull_request" \
  --prompt "PR #{pull_request.number} {action}: {pull_request.title}\nBy: {pull_request.user.login}\nBranch: {pull_request.head.ref}\n\n{pull_request.body}" \
  --skills "github-code-review" \
  --deliver github_comment
```

### Stripe：支付事件
```bash
hermes webhook subscribe stripe-payments \
  --events "payment_intent.succeeded,payment_intent.payment_failed" \
  --prompt "Payment {data.object.status}: {data.object.amount} cents from {data.object.receipt_email}" \
  --deliver telegram \
  --deliver-chat-id "-100123456789"
```

### CI/CD：构建通知
```bash
hermes webhook subscribe ci-builds \
  --events "pipeline" \
  --prompt "Build {object_attributes.status} on {project.name} branch {object_attributes.ref}\nCommit: {commit.message}" \
  --deliver discord \
  --deliver-chat-id "1234567890"
```

### 通用监控告警
```bash
hermes webhook subscribe alerts \
  --prompt "Alert: {alert.name}\nSeverity: {alert.severity}\nMessage: {alert.message}\n\nPlease investigate and suggest remediation." \
  --deliver origin
```

### 直接投递（无 agent，零 LLM 成本）

对于只想把通知推送到用户聊天的场景 —— 没有推理、没有 agent 循环 —— 加 `--deliver-only`。渲染后的 `--prompt` 模板成为字面消息体，直接分发给目标适配器。

适用于：
- 外部服务推送通知（Supabase/Firebase webhook → Telegram）
- 应原样转发的监控告警
- agent 之间的 ping（一个 agent 告诉另一个 agent 的用户某事）
- 任何 LLM 往返纯属浪费的 webhook

```bash
hermes webhook subscribe antenna-matches \
  --deliver telegram \
  --deliver-chat-id "123456789" \
  --deliver-only \
  --prompt "🎉 New match: {match.user_name} matched with you!" \
  --description "Antenna match notifications"
```

投递成功时 POST 返回 `200 OK`，目标失败时返回 `502` —— 以便上游服务智能重试。HMAC 认证、速率限制和幂等性仍然适用。

要求 `--deliver` 是一个真实目标（telegram、discord、slack、github_comment 等） —— `--deliver log` 会被拒绝，因为仅日志的直接投递没有意义。

## 安全

- 每个订阅获得一个自动生成的 HMAC-SHA256 密钥（或用 `--secret` 提供你自己的）
- webhook 适配器在每个传入 POST 上验证签名
- config.yaml 中的静态路由不能被动态订阅覆盖
- 订阅持久化到 `~/.hermes/webhook_subscriptions.json`

## 工作原理

1. `hermes webhook subscribe` 写入 `~/.hermes/webhook_subscriptions.json`
2. webhook 适配器在每个传入请求时热重载该文件（基于 mtime 门控，开销可忽略）
3. 当匹配某路由的 POST 到达时，适配器格式化提示词并触发一次 agent 运行
4. agent 的响应被投递到配置的目标（Telegram、Discord、GitHub 评论等）

## 故障排查

如果 webhook 不工作：

1. **网关在运行吗？** 用 `systemctl --user status hermes-gateway` 或 `ps aux | grep gateway` 检查
2. **webhook 服务器在监听吗？** `curl http://localhost:8644/health` 应返回 `{"status": "ok"}`
3. **检查网关日志：** `grep webhook ~/.hermes/logs/gateway.log | tail -20`
4. **签名不匹配？** 验证你服务中的密钥与 `hermes webhook list` 给出的一致。GitHub 发送 `X-Hub-Signature-256`，GitLab 发送 `X-Gitlab-Token`。
5. **防火墙/NAT？** webhook URL 必须能从服务端可达。对于本地开发，用隧道（ngrok、cloudflared）。
6. **事件类型错误？** 检查 `--events` 过滤器是否匹配服务发送的内容。用 `hermes webhook test <name>` 验证路由是否工作。
