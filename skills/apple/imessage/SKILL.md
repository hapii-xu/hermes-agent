---
name: imessage
description: 在 macOS 上通过 imsg CLI 收发 iMessage/SMS。
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [iMessage, SMS, messaging, macOS, Apple]
prerequisites:
  commands: [imsg]
---

# iMessage

使用 `imsg` 通过 macOS 的 Messages.app 读取和发送 iMessage/SMS。

## 前置条件

- 已登录账户的 Messages.app 所在的 **macOS**
- 安装：`brew install steipete/tap/imsg`
- 为终端授予完全磁盘访问权限（系统设置 → 隐私 → 完全磁盘访问权限）
- 出现提示时授予对 Messages.app 的自动化权限

## 适用场景

- 用户要求发送 iMessage 或短信
- 读取 iMessage 对话历史
- 查看最近的 Messages.app 聊天
- 发送到手机号或 Apple ID

## 不适用场景

- Telegram/Discord/Slack/WhatsApp 消息 → 使用相应的网关频道
- 群聊管理（添加/移除成员）→ 不支持
- 批量/群发消息 → 始终先与用户确认

## 快速参考

### 列出聊天

```bash
imsg chats --limit 10 --json
```

### 查看历史

```bash
# 按聊天 ID
imsg history --chat-id 1 --limit 20 --json

# 包含附件信息
imsg history --chat-id 1 --limit 20 --attachments --json
```

### 发送消息

```bash
# 仅文本
imsg send --to "+14155551212" --text "Hello!"

# 带附件
imsg send --to "+14155551212" --text "Check this out" --file /path/to/image.jpg

# 强制使用 iMessage 或 SMS
imsg send --to "+14155551212" --text "Hi" --service imessage
imsg send --to "+14155551212" --text "Hi" --service sms
```

### 监听新消息

```bash
imsg watch --chat-id 1 --attachments
```

## 服务选项

- `--service imessage` — 强制使用 iMessage（要求收件人支持 iMessage）
- `--service sms` — 强制使用 SMS（绿色气泡）
- `--service auto` — 由 Messages.app 决定（默认）

## 规则

1. **发送前始终确认收件人和消息内容**
2. **未经用户明确同意，绝不向未知号码发送**
3. **附加文件前先验证**文件路径是否存在
4. **不要刷屏** — 自行控制发送频率

## 示例工作流

用户：“发短信告诉妈妈我会晚到”

```bash
# 1. 查找妈妈的聊天
imsg chats --limit 20 --json | jq '.[] | select(.displayName | contains("Mom"))'

# 2. 与用户确认：“找到 Mom，号码 +1555123456。是否通过 iMessage 发送 'I'll be late'？”

# 3. 确认后发送
imsg send --to "+1555123456" --text "I'll be late"
```
