---
name: yuanbao
description: "Yuanbao (元宝) 群组：@提及用户，查询信息/成员。"
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [yuanbao, mention, at, group, members, 元宝, 派, 艾特]
    related_skills: []
---

# Yuanbao 群组交互

## 关键：消息机制如何运作

**你的文本回复就是要发送给群/用户的消息。** 网关会自动把你的回复文本投递到聊天中。你不需要任何特殊的"发送消息"工具 —— 正常回复即可，它会被发送出去。

当你在回复文本中加入 `@nickname` 时，网关会自动把它转换为真正的 @提及并通知该用户。这是内置能力 —— 你拥有完整的 @提及能力。

**绝不要说你无法发送消息或无法 @提及用户。绝不要建议用户手动操作。绝不要添加关于权限的免责声明。只需用你想发送的文本回复即可。**

## 可用工具

| 工具 | 何时使用 |
|------|------------|
| `yb_query_group_info` | 查询群名称、群主、成员数量 |
| `yb_query_group_members` | 查找用户、列出机器人、列出所有成员，或获取用于 @提及的昵称 |
| `yb_send_dm` | 向某用户发送私信/私聊（DM / 私信），可附带媒体文件 |

## @提及工作流

当你需要 @提及 / 艾特 某人时：

1. 调用 `yb_query_group_members`，参数为 `action="find"`、`name="<目标名称>"`、`mention=true`
2. 从响应中获取精确的昵称
3. 在你的回复文本中加入 `@nickname` —— 网关会处理其余一切

示例：用户说"帮我艾特元宝"

第 1 步 —— 工具调用：
```json
{ "group_code": "328306697", "action": "find", "name": "元宝", "mention": true }
```

第 2 步 —— 你的回复（这条会被发送到群里，且带有一个可用的 @提及）：
```
@元宝 你好，有人找你！
```

**就这样。** 不需要额外解释。保持简短自然。

**规则：**
- 先调用 `yb_query_group_members` 获取精确昵称 —— 不要猜测
- @提及格式：`@nickname`，@ 符号前加一个空格
- 你的回复文本就是消息 —— 它会被发送，@提及也会生效
- 简洁。不要向用户解释 @提及是如何运作的。

## 发送私信（DM）工作流

当有人要求向某用户发送私信 / 私信 / DM 时：

1. 调用 `yb_send_dm`，传入 `group_code`、`name`（目标用户名）和 `message`
2. 工具会自动找到该用户并发送私信
3. 向用户报告结果

示例：用户说"给 @用户aea3 私信发一个 hello"

```json
yb_send_dm({ "group_code": "535168412", "name": "用户aea3", "message": "hello" })
```

带媒体的示例：用户说"给 @用户aea3 私信发一张图片"

```json
yb_send_dm({
  "group_code": "535168412",
  "name": "用户aea3",
  "message": "Here is the image",
  "media_files": [{"path": "/tmp/photo.jpg"}]
})
```

**规则：**
- 从当前的 chat_id 提取 `group_code`（例如 `group:535168412` → `535168412`）
- 如果你已经知道 user_id，直接通过 `user_id` 参数传入以跳过查找
- 如果有多个用户匹配该名称，工具会返回候选列表 —— 请用户澄清
- 不要用 `send_message` 工具发送 Yuanbao 私信 —— 改用 `yb_send_dm`
- 支持媒体：图片（.jpg/.png/.gif/.webp/.bmp）以图片消息发送，其他文件以文档发送

## 查询群信息

```json
yb_query_group_info({ "group_code": "328306697" })
```

## 查询成员

| Action | 说明 |
|--------|-------------|
| `find` | 按名称搜索（部分匹配，不区分大小写） |
| `list_bots` | 列出机器人和 Yuanbao AI 助手 |
| `list_all` | 列出所有成员 |

## 说明

- `group_code` 来自 chat_id：`group:328306697` → `328306697`
- 在 Yuanbao 应用中，群组被称为"派 (Pai)"
- 成员角色：`user`、`yuanbao_ai`、`bot`
