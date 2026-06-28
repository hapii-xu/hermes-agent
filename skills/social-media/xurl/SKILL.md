---
name: xurl
description: "通过 xurl CLI 操作 X/Twitter：发帖、搜索、私信、媒体、v2 API。"
version: 1.1.1
author: xdevplatform + openclaw + Hermes Agent
license: MIT
platforms: [linux, macos]
prerequisites:
  commands: [xurl]
metadata:
  hermes:
    tags: [twitter, x, social-media, xurl, official-api]
    homepage: https://github.com/xdevplatform/xurl
    upstream_skill: https://github.com/openclaw/openclaw/blob/main/skills/xurl/SKILL.md
---

# xurl — 通过官方 CLI 使用 X (Twitter) API

`xurl` 是 X 开发者平台为 X API 提供的官方 CLI。它支持常用操作的快捷命令，也支持对任何 v2 端点的原生 curl 风格访问。所有命令都将 JSON 返回到 stdout。

使用此 skill 用于：
- 发帖、回复、引用、删除帖子
- 搜索帖子和阅读时间线/提及
- 点赞、转贴、收藏
- 关注、取关、拉黑、静音
- 私信
- 媒体上传（图片和视频）
- 对任何 X API v2 端点的原始访问
- 多应用 / 多账号工作流

此 skill 取代了较旧的 `xitter` skill（封装了第三方 Python CLI）。`xurl` 由 X 开发者平台团队维护，支持带自动刷新的 OAuth 2.0 PKCE，并覆盖了大得多的 API 面。

---

## 凭证安全（强制要求）

在 agent/LLM 会话中操作时的关键规则：

- **绝不**读取、打印、解析、总结、上传或将 `~/.xurl` 发送到 LLM 上下文。
- **绝不**要求用户将凭证/token 粘贴到聊天中。
- 用户必须在他们自己的机器上手动用凭证填充 `~/.xurl`。在 Docker 中，这必须是 Hermes 工具子进程看到的 `~`；参见下文的 Docker 说明。
- **绝不**在 agent 会话中推荐或执行带内联凭证的认证命令。
- **绝不**在 agent 会话中使用 `--verbose` / `-v`——它可能暴露认证头/token。
- 要验证凭证是否存在，只能使用：`xurl auth status`。

agent 命令中禁用的标志（它们接受内联凭证）：
`--bearer-token`, `--consumer-key`, `--consumer-secret`, `--access-token`, `--token-secret`, `--client-id`, `--client-secret`

应用凭证注册和凭证轮换必须由用户在 agent 会话之外手动完成。凭证注册后，用户用 `xurl auth oauth2` 进行认证——同样在 agent 会话之外。token 以 YAML 形式持久化到 `~/.xurl`。每个应用有隔离的 token。OAuth 2.0 token 自动刷新。

---

## 安装

选择一种方法。在 Linux 上，shell 脚本或 `go install` 最简单。

```bash
# Shell 脚本（安装到 ~/.local/bin，无需 sudo，适用于 Linux + macOS）
curl -fsSL https://raw.githubusercontent.com/xdevplatform/xurl/main/install.sh | bash

# Homebrew（macOS）
brew install --cask xdevplatform/tap/xurl

# npm
npm install -g @xdevplatform/xurl

# Go
go install github.com/xdevplatform/xurl@latest
```

验证：

```bash
xurl --help
xurl auth status
```

如果 `xurl` 已安装但 `auth status` 显示没有应用或 token，用户需要手动完成认证——参见下一节。

---

## 一次性用户设置（用户在 agent 之外运行这些）

这些步骤必须由用户直接执行，而非由 agent 执行，因为涉及粘贴凭证。引导用户到这个区块；不要替他们执行。

1. 在 https://developer.x.com/en/portal/dashboard 创建或打开一个应用
2. 将重定向 URI 设置为 `http://localhost:8080/callback`
3. 复制应用的 Client ID 和 Client Secret
4. 在本地注册应用（用户运行）：
   ```bash
   xurl auth apps add my-app --client-id YOUR_CLIENT_ID --client-secret YOUR_CLIENT_SECRET
   ```
5. 认证（指定 `--app` 以将 token 绑定到你的应用）：
   ```bash
   xurl auth oauth2 --app my-app
   ```
   （这会打开浏览器进行 OAuth 2.0 PKCE 流程。）

   如果 X 返回 `UsernameNotFound` 错误或在 OAuth 后的 `/2/users/me` 查询上返回 403，请显式传入你的用户名（xurl v1.1.0+）：
   ```bash
   xurl auth oauth2 --app my-app YOUR_USERNAME
   ```
   这会将 token 绑定到你的用户名并跳过有问题的 `/2/users/me` 调用。
6. 将应用设为默认，以便所有命令都使用它：
   ```bash
   xurl auth default my-app
   ```
7. 验证：
   ```bash
   xurl auth status
   xurl whoami
   ```

此后，agent 可以使用下面的任何命令而无需进一步设置。OAuth 2.0 token 自动刷新。

> **常见陷阱：** 如果你从 `xurl auth oauth2` 中省略了 `--app my-app`，OAuth token 会保存到内置的 `default` 应用配置——它没有 client-id 或 client-secret。即使 OAuth 流程看起来成功，命令也会因认证错误而失败。如果遇到这种情况，重新运行 `xurl auth oauth2 --app my-app` 和 `xurl auth default my-app`。

> **Docker HOME 陷阱：** 在官方 Hermes Docker 布局中，`/opt/data` 是 `HERMES_HOME`，但 Hermes 工具子进程使用 `/opt/data/home` 作为 `HOME`。这意味着对于 Hermes 运行的 `xurl` 命令，`~/.xurl` 解析为 `/opt/data/home/.xurl`，而非 `/opt/data/.xurl`。用相同的 HOME 运行用户设置：
> ```bash
> HOME=/opt/data/home xurl auth apps add my-app --client-id YOUR_CLIENT_ID --client-secret YOUR_CLIENT_SECRET
> HOME=/opt/data/home xurl auth oauth2 --app my-app YOUR_USERNAME
> HOME=/opt/data/home xurl auth default my-app YOUR_USERNAME
> HOME=/opt/data/home xurl auth status
> ```
> 如果 `HOME=/opt/data xurl auth status` 成功但 `HOME=/opt/data/home xurl auth status` 显示没有应用或 token，Hermes 工具调用将看不到凭证。

---

## 快速参考

| 操作 | 命令 |
| --- | --- |
| 发帖 | `xurl post "Hello world!"` |
| 回复 | `xurl reply POST_ID "Nice post!"` |
| 引用 | `xurl quote POST_ID "My take"` |
| 删除帖子 | `xurl delete POST_ID` |
| 阅读帖子 | `xurl read POST_ID` |
| 搜索帖子 | `xurl search "QUERY" -n 10` |
| 我是谁 | `xurl whoami` |
| 查找用户 | `xurl user @handle` |
| 主页时间线 | `xurl timeline -n 20` |
| 提及 | `xurl mentions -n 10` |
| 点赞 / 取消 | `xurl like POST_ID` / `xurl unlike POST_ID` |
| 转贴 / 撤销 | `xurl repost POST_ID` / `xurl unrepost POST_ID` |
| 收藏 / 移除 | `xurl bookmark POST_ID` / `xurl unbookmark POST_ID` |
| 列出收藏 / 点赞 | `xurl bookmarks -n 10` / `xurl likes -n 10` |
| 关注 / 取关 | `xurl follow @handle` / `xurl unfollow @handle` |
| 正在关注 / 粉丝 | `xurl following -n 20` / `xurl followers -n 20` |
| 拉黑 / 取消 | `xurl block @handle` / `xurl unblock @handle` |
| 静音 / 取消 | `xurl mute @handle` / `xurl unmute @handle` |
| 发送私信 | `xurl dm @handle "message"` |
| 列出私信 | `xurl dms -n 10` |
| 上传媒体 | `xurl media upload path/to/file.mp4` |
| 媒体状态 | `xurl media status MEDIA_ID` |
| 列出应用 | `xurl auth apps list` |
| 移除应用 | `xurl auth apps remove NAME` |
| 设置默认应用 | `xurl auth default APP_NAME [USERNAME]` |
| 按请求指定应用 | `xurl --app NAME /2/users/me` |
| 认证状态 | `xurl auth status` |

注意：
- `POST_ID` 也接受完整 URL（例如 `https://x.com/user/status/1234567890`）——xurl 会提取 ID。
- 用户名带或不带前导 `@` 均可。

---

## 命令详情

### 发帖

```bash
xurl post "Hello world!"
xurl post "Check this out" --media-id MEDIA_ID
xurl post "Thread pics" --media-id 111 --media-id 222

xurl reply 1234567890 "Great point!"
xurl reply https://x.com/user/status/1234567890 "Agreed!"
xurl reply 1234567890 "Look at this" --media-id MEDIA_ID

xurl quote 1234567890 "Adding my thoughts"
xurl delete 1234567890
```

### 阅读与搜索

```bash
xurl read 1234567890
xurl read https://x.com/user/status/1234567890

xurl search "golang"
xurl search "from:elonmusk" -n 20
xurl search "#buildinpublic lang:en" -n 15
```

对于 X 文章，使用原始 API 模式而非 `read` 快捷方式。`xurl read`
期望的是帖子 ID 或帖子 URL；不要把 `read` 放在 `/2/tweets/...`
端点之前。请求 `article` tweet 字段并从 JSON 响应中提取
`data.article.plain_text`：

```bash
xurl --app APP_NAME '/2/tweets/2057909493250539891?expansions=author_id,attachments.media_keys,referenced_tweets.id&tweet.fields=created_at,lang,public_metrics,context_annotations,entities,possibly_sensitive,conversation_id,in_reply_to_user_id,referenced_tweets,article'
```

### 用户、时间线、提及

```bash
xurl whoami
xurl user elonmusk
xurl user @XDevelopers

xurl timeline -n 25
xurl mentions -n 20
```

### 互动

```bash
xurl like 1234567890
xurl unlike 1234567890

xurl repost 1234567890
xurl unrepost 1234567890

xurl bookmark 1234567890
xurl unbookmark 1234567890

xurl bookmarks -n 20
xurl likes -n 20
```

### 社交关系图

```bash
xurl follow @XDevelopers
xurl unfollow @XDevelopers

xurl following -n 50
xurl followers -n 50

# 其他用户的关系图
xurl following --of elonmusk -n 20
xurl followers --of elonmusk -n 20

xurl block @spammer
xurl unblock @spammer
xurl mute @annoying
xurl unmute @annoying
```

### 私信

```bash
xurl dm @someuser "Hey, saw your post!"
xurl dms -n 25
```

### 媒体上传

```bash
# 自动检测类型
xurl media upload photo.jpg
xurl media upload video.mp4

# 显式类型/类别
xurl media upload --media-type image/jpeg --category tweet_image photo.jpg

# 视频需要服务端处理——检查状态（或轮询）
xurl media status MEDIA_ID
xurl media status --wait MEDIA_ID

# 完整工作流
xurl media upload meme.png                  # 返回 media id
xurl post "lol" --media-id MEDIA_ID
```

---

## 原始 API 访问

快捷方式覆盖常见操作。对于其他任何操作，对任何 X API v2 端点使用原始 curl 风格模式：

```bash
# GET
xurl /2/users/me

# 带 JSON body 的 POST
xurl -X POST /2/tweets -d '{"text":"Hello world!"}'

# DELETE / PUT / PATCH
xurl -X DELETE /2/tweets/1234567890

# 自定义头
xurl -H "Content-Type: application/json" /2/some/endpoint

# 强制流式
xurl -s /2/tweets/search/stream

# 完整 URL 也可
xurl https://api.x.com/2/users/me
```

---

## 全局标志

| 标志 | 简写 | 描述 |
| --- | --- | --- |
| `--app` | | 使用特定已注册应用（覆盖默认） |
| `--auth` | | 强制认证类型：`oauth1`、`oauth2` 或 `app` |
| `--username` | `-u` | 使用哪个 OAuth2 账号（如果存在多个） |
| `--verbose` | `-v` | **在 agent 会话中禁用**——会泄露认证头 |
| `--trace` | `-t` | 添加 `X-B3-Flags: 1` 追踪头 |

---

## 流式传输

流式端点会被自动检测。已知端点包括：

- `/2/tweets/search/stream`
- `/2/tweets/sample/stream`
- `/2/tweets/sample10/stream`

用 `-s` 强制任何端点的流式传输。

---

## 输出格式

所有命令都将 JSON 返回到 stdout。结构映射 X API v2：

```json
{ "data": { "id": "1234567890", "text": "Hello world!" } }
```

错误也是 JSON：

```json
{ "errors": [ { "message": "Not authorized", "code": 403 } ] }
```

---

## 常见工作流

### 带图片发帖
```bash
xurl media upload photo.jpg
xurl post "Check out this photo!" --media-id MEDIA_ID
```

### 回复一个对话
```bash
xurl read https://x.com/user/status/1234567890
xurl reply 1234567890 "Here are my thoughts..."
```

### 搜索并互动
```bash
xurl search "topic of interest" -n 10
xurl like POST_ID_FROM_RESULTS
xurl reply POST_ID_FROM_RESULTS "Great point!"
```

### 检查你的活动
```bash
xurl whoami
xurl mentions -n 20
xurl timeline -n 20
```

### 多应用（凭证已手动预配置）
```bash
xurl auth default prod alice               # prod 应用，alice 用户
xurl --app staging /2/users/me             # 针对 staging 的一次性操作
```

---

## 错误处理

- 任何错误时返回非零退出码。
- API 错误仍以 JSON 打印到 stdout，因此你可以解析它们。
- 认证错误 → 让用户在 agent 会话之外重新运行 `xurl auth oauth2`。
- 需要调用者用户 ID 的命令（like、repost、bookmark、follow 等）会通过 `/2/users/me` 自动获取。那里的认证失败会以认证错误形式呈现。

---

## Agent 工作流

1. 验证先决条件：`xurl --help` 和 `xurl auth status`。
2. **检查默认应用是否有凭证。** 解析 `auth status` 输出。默认应用用 `▸` 标记。如果默认应用显示 `oauth2: (none)` 但另一个应用有有效的 oauth2 用户，告诉用户运行 `xurl auth default <that-app>` 来修复。这是最常见的设置错误——用户添加了带自定义名称的应用却从未将其设为默认，所以 xurl 一直尝试空的 `default` 配置。
3. 如果认证完全缺失，停止并引导用户到"一次性用户设置"区块——不要自己尝试注册应用或传递凭证。
4. 从廉价的读取操作开始（`xurl whoami`、`xurl user @handle`、`xurl search ... -n 3`）以确认可达性。
5. 在任何写操作（发帖、回复、点赞、转贴、私信、关注、拉黑、删除）之前确认目标帖子/用户和用户意图。
6. 直接使用 JSON 输出——每个响应已经是结构化的。
7. 绝不将 `~/.xurl` 内容粘贴回对话。

---

## 故障排除

| 症状 | 原因 | 修复 |
| --- | --- | --- |
| OAuth 流程成功后出现认证错误 | Token 保存到 `default` 应用（无 client-id/secret）而非你的命名应用 | `xurl auth oauth2 --app my-app` 然后 `xurl auth default my-app` |
| OAuth 期间出现 `unauthorized_client` | X 仪表盘中应用类型设为"Native App" | 在 User Authentication Settings 中改为"Web app, automated app or bot" |
| OAuth 后立即在 `/2/users/me` 上出现 `UsernameNotFound` 或 403 | X 没有可靠地从 `/2/users/me` 返回用户名 | 重新运行 `xurl auth oauth2 --app my-app YOUR_USERNAME`（xurl v1.1.0+）以显式传入用户名 |
| 每个请求都返回 401 | Token 过期或默认应用错误 | 检查 `xurl auth status`——验证 `▸` 指向一个有 oauth2 token 的应用 |
| `client-forbidden` / `client-not-enrolled` | X 平台注册问题 | 仪表盘 → Apps → Manage → 移至"Pay-per-use"套餐 → Production 环境 |
| `CreditsDepleted` | X API 余额为 $0 | 在 Developer Console → Billing 购买额度（最低 $5） |
| 图片上传时出现 `media processing failed` | 默认类别是 `amplify_video` | 添加 `--category tweet_image --media-type image/png` |
| X 仪表盘中有两个"Client Secret"值 | UI bug——第一个其实是 Client ID | 在"Keys and tokens"页面确认；ID 以 `MTpjaQ` 结尾 |

---

## 注意事项

- **速率限制：** X 对每个端点强制速率限制。429 表示等待并重试。写端点（post、reply、like、repost）的限制比读端点更紧。
- **范围：** OAuth 2.0 token 使用宽泛范围。对特定操作的 403 通常意味着 token 缺少某个范围——让用户重新运行 `xurl auth oauth2`。
- **Token 刷新：** OAuth 2.0 token 自动刷新。无需操作。
- **多应用：** 每个应用有隔离的凭证/token。用 `xurl auth default` 或 `--app` 切换。
- **每个应用多个账号：** 用 `-u / --username` 选择，或用 `xurl auth default APP USER` 设置默认。
- **Token 存储：** `~/.xurl` 是 YAML。在 Docker 中，使用 Hermes 子进程 HOME（官方镜像中为 `/opt/data/home`），以便 token 落在 `/opt/data/home/.xurl` 下。绝不读取或发送此文件到 LLM 上下文。
- **费用：** X API 访问对于有意义的用量通常是付费的。许多失败是套餐/权限问题，而非代码问题。

---

## 致谢

- 上游 CLI：https://github.com/xdevplatform/xurl（X 开发者平台团队，Chris Park 等）
- 上游 agent skill：https://github.com/openclaw/openclaw/blob/main/skills/xurl/SKILL.md
- Hermes 适配：按 Hermes skill 约定重新格式化；安全护栏逐字保留。
