---
name: teams-meeting-pipeline
description: "通过 Hermes CLI 操作 Teams 会议摘要流水线 —— 总结会议、查看流水线状态、重放任务、管理 Microsoft Graph 订阅。"
version: 1.1.0
author: Hermes Agent + Teknium
license: MIT
prerequisites:
  env_vars: [MSGRAPH_TENANT_ID, MSGRAPH_CLIENT_ID, MSGRAPH_CLIENT_SECRET]
  commands: [hermes]
metadata:
  hermes:
    tags: [Teams, Microsoft Graph, Meetings, Productivity, Operations]
    related_docs:
      - /docs/guides/microsoft-graph-app-registration
      - /docs/user-guide/messaging/teams-meetings
      - /docs/guides/operate-teams-meeting-pipeline
---

# Teams 会议流水线

当用户询问关于 Microsoft Teams 会议摘要、转录、录音、待办事项、Graph 订阅，或任何关于 Teams 会议流水线的运维问题时，都应使用本技能。适用于任何语言 —— 下方的触发条件仅为示例，并非详尽列表。

所有面向运维的操作都是通过终端工具执行的 `hermes teams-pipeline` 子命令。该流水线不引入新的模型工具 —— CLI 就是操作界面。

## 何时使用本技能

用户想要：
- 总结一场 Teams 会议 / 提取待办事项 / 获取会议笔记
- 检查流水线状态、查看已存储的会议任务，或查看最近的会议
- 重放 / 重新运行某个失败或需要重新生成摘要的已存储任务
- 在修改环境变量或配置后验证 Microsoft Graph 配置
- 排查"会议摘要一直没到"或"没有新会议进入流水线"
- 管理 Graph webhook 订阅（创建、续期、删除、查看）
- 设置自动订阅续期（见下方陷阱）

多语言触发示例（非详尽）：
- 英文："summarize the Teams meeting"、"pipeline status"、"replay job X"
- 土耳其语："Teams meeting özetle"、"action item çıkar"、"toplantı notu"、"pipeline durumu"、"replay job"

## 前置条件

在使用该流水线之前，请验证以下变量已在 `${HERMES_HOME:-~/.hermes}/.env` 中设置：

```bash
MSGRAPH_TENANT_ID=...
MSGRAPH_CLIENT_ID=...
MSGRAPH_CLIENT_SECRET=...
```

如果其中有任何一项缺失，请引导用户访问 `/docs/guides/microsoft-graph-app-registration` 的 Azure 应用注册指南 —— 他们需要一个已由管理员同意 Graph 应用权限的 Azure AD 应用注册，流水线才能工作。

## 命令参考

### 状态与检查（从这里开始）

```bash
hermes teams-pipeline validate              # 配置快照 —— 任何改动后都先运行它
hermes teams-pipeline token-health          # Graph 令牌状态
hermes teams-pipeline token-health --force-refresh   # 强制重新获取令牌
hermes teams-pipeline list                  # 最近的会议任务
hermes teams-pipeline list --status failed  # 仅失败的作业
hermes teams-pipeline show <job-id>         # 某个作业的完整详情
hermes teams-pipeline subscriptions         # 当前的 Graph webhook 订阅
```

### 重跑 / 调试

```bash
hermes teams-pipeline run <job-id>          # 重放已存储的作业（重新总结、重新送达）
hermes teams-pipeline fetch --meeting-id <id>   # 试运行：解析会议 + 转录但不持久化
hermes teams-pipeline fetch --join-web-url "<url>"   # 按 join URL 试运行
```

### 订阅管理

```bash
hermes teams-pipeline subscribe \
  --resource communications/onlineMeetings/getAllTranscripts \
  --notification-url https://<your-public-host>/msgraph/webhook \
  --client-state "$MSGRAPH_WEBHOOK_CLIENT_STATE"

hermes teams-pipeline renew-subscription <sub-id> --expiration <iso-8601>
hermes teams-pipeline delete-subscription <sub-id>
hermes teams-pipeline maintain-subscriptions            # 续期临近到期的订阅
hermes teams-pipeline maintain-subscriptions --dry-run  # 仅展示会被续期的内容
```

## 针对常见诉求的决策树

- 用户问"为什么今天这场会我没收到摘要？" → 从 `list --status failed` 开始，然后对相关行执行 `show <job-id>`。如果该作业根本不存在，检查 `subscriptions` —— webhook 可能已过期（见下方陷阱）。
- 用户问"配置是否正常？" → 依次执行 `validate`、`token-health`、`subscriptions`。如果三项都通过，请求一场测试会议并检查 `list` 中是否出现新行。
- 用户问"重新生成会议 X 的摘要" → 用 `list` 找到作业 ID，用 `run <job-id>` 重放。如果再次失败，用 `show <job-id>` 查看错误，并用 `fetch --meeting-id` 试运行产物解析。
- 用户问"把会议 X 加入流水线" → 通常不需要 —— 流水线是订阅驱动的，而非按会议驱动。如果他们想总结某场特定的历史会议，可用 `fetch` 拉取转录，并在作业创建后用 `run`。

## 关键陷阱：Graph 订阅在 72 小时后过期

Microsoft Graph 将 webhook 订阅的上限设为 72 小时，且**不会自动续期**。如果没有调度 `maintain-subscriptions`，会议通知会在任何手动创建订阅的 3 天后悄然停止到达。

当用户反馈"流水线昨天还正常，但今天什么都没到"时：
1. 运行 `hermes teams-pipeline subscriptions` —— 如果为空，或所有条目的 `expirationDateTime` 都已过去，那就是原因。
2. 按上文所示用 `subscribe` 重新创建。
3. **立即设置自动续期**，通过 `hermes cron add`、systemd timer 或普通 crontab。运维手册 `/docs/guides/operate-teams-meeting-pipeline#automating-subscription-renewal-required-for-production` 提供了全部三种方案。12 小时间隔是安全的（相对 72 小时上限有 6 倍余量）。

## 其他陷阱

- **转录尚未生成。** Teams 在会议结束后需要一些时间才会生成转录产物。对刚刚结束的会议执行 `fetch --meeting-id` 可能返回空。等待 2-5 分钟后重试，或让 Graph webhook 自然驱动入库。
- **送达模式不匹配。** 如果摘要已生成（`list` 显示成功）但 Teams 中什么也没收到，检查 `platforms.teams.extra.delivery_mode` 以及匹配的目标配置（`incoming_webhook_url` 或 `chat_id` 或 `team_id`+`channel_id`）。写入器会从 config.yaml 或 `TEAMS_*` 环境变量读取这些值。
- **Graph 应用权限。** 令牌可以顺利获取（`token-health` 通过），但当权限被添加后未重新授予管理员同意时，Graph API 调用会返回 401/403。请让用户回到 Azure 门户中的应用注册，再次点击"Grant admin consent"。

## 相关文档

当用户需要超出本技能覆盖范围的深度时，可引导至：
- Azure 应用注册 walkthrough：`/docs/guides/microsoft-graph-app-registration`
- 完整的流水线配置：`/docs/user-guide/messaging/teams-meetings`
- 运维手册（续期自动化、故障排查、上线检查清单）：`/docs/guides/operate-teams-meeting-pipeline`
- Webhook 监听器配置：`/docs/user-guide/messaging/msgraph-webhook`
