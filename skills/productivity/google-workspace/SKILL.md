---
name: google-workspace
description: "通过 gws CLI 或 Python 使用 Gmail、Calendar、Drive、Docs、Sheets。"
version: 1.1.0
author: Nous Research
license: MIT
platforms: [linux, macos, windows]
required_credential_files:
  - path: google_token.json
    description: Google OAuth2 令牌（由设置脚本创建）
  - path: google_client_secret.json
    description: Google OAuth2 客户端凭据（从 Google Cloud Console 下载）
metadata:
  hermes:
    tags: [Google, Gmail, Calendar, Drive, Sheets, Docs, Contacts, Email, OAuth]
    homepage: https://github.com/NousResearch/hermes-agent
    related_skills: [himalaya]
---

# Google Workspace

Gmail、Calendar、Drive、Contacts、Sheets 与 Docs——通过 Hermes 管理的 OAuth 与一层轻量 CLI 包装。当 `gws` 已安装时，该 skill 用作更广泛 Google Workspace 覆盖的执行后端；否则回退到内置的 Python 客户端实现。

## 参考资料

- `references/gmail-search-syntax.md` — Gmail 搜索运算符（is:unread、from:、newer_than: 等）

## 脚本

- `scripts/setup.py` — OAuth2 设置（运行一次以授权）
- `scripts/google_api.py` — 兼容性包装 CLI。它在可用时优先用 `gws` 执行操作，同时保留 Hermes 既有的 JSON 输出契约。

## 首次设置

设置完全非交互式——你逐步驱动它，使其在 CLI、Telegram、Discord 或任何平台上都能工作。

先定义一个简写：

```bash
GSETUP="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/setup.py"
```

### 步骤 0：检查是否已设置

```bash
$GSETUP --check
```

如果打印 `AUTHENTICATED`，跳到"用法"——设置已完成。

### 步骤 1：分流——询问用户需要什么

开始 OAuth 设置前，向用户提两个问题：

**问题 1："你需要哪些 Google 服务？仅邮件，还是也包含
Calendar/Drive/Sheets/Docs？"**

- **仅邮件** → 他们根本不需要这个 skill。改用 `himalaya`
  skill——它配合 Gmail 应用专用密码（设置 → 安全 → 应用
  密码）工作，2 分钟即可设置完毕。无需 Google Cloud 项目。
  加载 himalaya skill 并遵循其设置指引。

- **邮件 + Calendar** → 继续使用本 skill，但在授权时使用
  `--services email,calendar`，这样同意屏幕只会请求他们
  实际需要的范围。

- **仅 Calendar/Drive/Sheets/Docs** → 继续使用本 skill，并使用更窄的
  `--services` 集合，如 `calendar,drive,sheets,docs`。

- **完整 Workspace 访问** → 继续使用本 skill 并使用默认的
  `all` 服务集。

**问题 2："你的 Google 账户是否使用了高级保护（登录需要硬件
安全密钥）？如果不确定，大概率没有——这是你需要显式
加入的东西。"**

- **否 / 不确定** → 正常设置。继续下方步骤。
- **是** → 他们的 Workspace 管理员必须先把 OAuth 客户端 ID 加入组织的
  允许应用列表，步骤 4 才能工作。预先告知他们。

### 步骤 2：创建 OAuth 凭据（一次性，约 5 分钟）

告诉用户：

> 你需要一个 Google Cloud OAuth 客户端。这是一次性设置：
>
> 1. 创建或选择一个项目：
>    https://console.cloud.google.com/projectselector2/home/dashboard
> 2. 从 API 库启用所需 API：
>    https://console.cloud.google.com/apis/library
>    启用：Gmail API、Google Calendar API、Google Drive API、
>    Google Sheets API、Google Docs API、People API
> 3. 在此处创建 OAuth 客户端：
>    https://console.cloud.google.com/apis/credentials
>    凭据 → 创建凭据 → OAuth 2.0 Client ID
> 4. 应用类型："Desktop app" → 创建
> 5. 如果应用仍处于测试中，在此处把用户的 Google 账户加为测试用户：
>    https://console.cloud.google.com/auth/audience
>    受众 → 测试用户 → 添加用户
> 6. 下载 JSON 文件并告诉我文件路径
>
> Hermes CLI 重要提示：如果文件路径以 `/` 开头，不要在 CLI 中
> 单独把裸路径作为一条消息发送，因为它会被误认为斜杠命令。改用
> 一整句话发送，例如：
> `The JSON file path is: /home/user/Downloads/client_secret_....json`

一旦他们提供路径：

```bash
$GSETUP --client-secret /path/to/client_secret.json
```

如果他们粘贴的是原始客户端 ID / 客户端密钥值而非文件路径，
你亲自为他们写一个合法的 Desktop OAuth JSON 文件，保存到某个明确位置
（例如 `~/Downloads/hermes-google-client-secret.json`），然后对该文件
运行 `--client-secret`。

### 步骤 3：获取授权 URL

使用步骤 1 中选择的服务集。示例：

```bash
$GSETUP --auth-url --services email,calendar --format json
$GSETUP --auth-url --services calendar,drive,sheets,docs --format json
$GSETUP --auth-url --services all --format json
```

这会返回带 `auth_url` 字段的 JSON，并把确切的 URL 保存到
`~/.hermes/google_oauth_last_url.txt`。

此步骤的 Agent 规则：
- 提取 `auth_url` 字段并把该确切 URL 作为单行发给用户。
- 告诉用户浏览器在批准后很可能会在 `http://localhost:1` 上失败，这是预期的。
- 告诉他们从浏览器地址栏复制完整的重定向 URL。
- 如果用户收到 `Error 403: access_denied`，直接把他们送到 `https://console.cloud.google.com/auth/audience` 把自己加为测试用户。

### 步骤 4：交换 code

用户会回贴一个类似 `http://localhost:1/?code=4/0A...&scope=...`
的 URL，或仅 code 字符串。两者都可以。`--auth-url` 步骤会在本地
保存一个临时的待处理 OAuth 会话，这样 `--auth-code` 以后可以完成
PKCE 交换，即使在无头系统上：

```bash
$GSETUP --auth-code "THE_URL_OR_CODE_THE_USER_PASTED" --format json
```

如果 `--auth-code` 因 code 过期、已被使用或来自较旧的浏览器标签页
而失败，它现在会返回一个新的 `fresh_auth_url`。此时，
立即把新 URL 发给用户，让他们只用最新的浏览器重定向重试。

### 步骤 5：验证

```bash
$GSETUP --check
```

应打印 `AUTHENTICATED`。设置完成——从现在起令牌自动刷新。

### 注意事项

- 令牌存储于 `~/.hermes/google_token.json` 并自动刷新。
- 待处理的 OAuth 会话状态/验证器临时存储于 `~/.hermes/google_oauth_pending.json`，直到交换完成。
- 如果 `gws` 已安装，`google_api.py` 会把它指向同一个 `~/.hermes/google_token.json` 凭据文件。用户无需运行单独的 `gws auth login` 流程。
- 撤销：`$GSETUP --revoke`

## 用法

所有命令都通过 API 脚本。把 `GAPI` 设为简写：

```bash
GAPI="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/google_api.py"
```

### Gmail

```bash
# 搜索（返回 JSON 数组，含 id、from、subject、date、snippet）
$GAPI gmail search "is:unread" --max 10
$GAPI gmail search "from:boss@company.com newer_than:1d"
$GAPI gmail search "has:attachment filename:pdf newer_than:7d"

# 读取完整邮件（返回带正文的 JSON）
$GAPI gmail get MESSAGE_ID

# 发送
$GAPI gmail send --to user@example.com --subject "Hello" --body "Message text"
$GAPI gmail send --to user@example.com --subject "Report" --body "<h1>Q4</h1><p>Details...</p>" --html
$GAPI gmail send --to user@example.com --subject "Hello" --from '"Research Agent" <user@example.com>' --body "Message text"

# 回复（自动线程化并设置 In-Reply-To）
$GAPI gmail reply MESSAGE_ID --body "Thanks, that works for me."
$GAPI gmail reply MESSAGE_ID --from '"Support Bot" <user@example.com>' --body "Thanks"

# 标签
$GAPI gmail labels
$GAPI gmail modify MESSAGE_ID --add-labels LABEL_ID
$GAPI gmail modify MESSAGE_ID --remove-labels UNREAD
```

### Calendar

```bash
# 列出事件（默认为接下来 7 天）
$GAPI calendar list
$GAPI calendar list --start 2026-03-01T00:00:00Z --end 2026-03-07T23:59:59Z

# 创建事件（必须使用带时区的 ISO 8601）
$GAPI calendar create --summary "Team Standup" --start 2026-03-01T10:00:00-06:00 --end 2026-03-01T10:30:00-06:00
$GAPI calendar create --summary "Lunch" --start 2026-03-01T12:00:00Z --end 2026-03-01T13:00:00Z --location "Cafe"
$GAPI calendar create --summary "Review" --start 2026-03-01T14:00:00Z --end 2026-03-01T15:00:00Z --attendees "alice@co.com,bob@co.com"

# 删除事件
$GAPI calendar delete EVENT_ID
```

### Drive

```bash
# 搜索现有文件
$GAPI drive search "quarterly report" --max 10
$GAPI drive search "mimeType='application/pdf'" --raw-query --max 5

# 获取单个文件的元数据
$GAPI drive get FILE_ID

# 上传本地文件（自动检测 MIME 类型）
$GAPI drive upload /path/to/report.pdf
$GAPI drive upload /path/to/image.png --name "Logo.png" --parent FOLDER_ID

# 下载（二进制文件原样下载；Google 原生文件导出为
# 合理的默认格式——Docs→pdf、Sheets→csv、Slides→pdf、Drawings→png）
$GAPI drive download FILE_ID
$GAPI drive download DOC_ID --output ~/doc.pdf
$GAPI drive download DOC_ID --export-mime text/plain --output ~/doc.txt

# 创建文件夹
$GAPI drive create-folder "Reports"
$GAPI drive create-folder "Q4" --parent FOLDER_ID

# 共享
$GAPI drive share FILE_ID --email alice@example.com --role reader
$GAPI drive share FILE_ID --email alice@example.com --role writer --notify
$GAPI drive share FILE_ID --type anyone --role reader        # 拥有链接的任何人
$GAPI drive share FILE_ID --type domain --domain example.com --role reader

# 删除——默认送回收站（可恢复）。使用 --permanent 跳过回收站。
$GAPI drive delete FILE_ID
$GAPI drive delete FILE_ID --permanent
```

### Contacts

```bash
$GAPI contacts list --max 20
```

### Sheets

```bash
# 创建新电子表格
$GAPI sheets create --title "Q4 Budget"
$GAPI sheets create --title "Inventory" --sheet-name "Stock"

# 读取
$GAPI sheets get SHEET_ID "Sheet1!A1:D10"

# 写入
$GAPI sheets update SHEET_ID "Sheet1!A1:B2" --values '[["Name","Score"],["Alice","95"]]'

# 追加行
$GAPI sheets append SHEET_ID "Sheet1!A:C" --values '[["new","row","data"]]'
```

### Docs

```bash
# 读取
$GAPI docs get DOC_ID

# 创建新 Doc（可选地用正文文本播种）
$GAPI docs create --title "Meeting Notes"
$GAPI docs create --title "Draft" --body "First paragraph..."

# 向已有 Doc 末尾追加文本
$GAPI docs append DOC_ID --text "Additional content to append"
```

## 输出格式

所有命令返回 JSON。用 `jq` 解析或直接读取。关键字段：

- **Gmail search**：`[{id, threadId, from, to, subject, date, snippet, labels}]`
- **Gmail get**：`{id, threadId, from, to, subject, date, labels, body}`
- **Gmail send/reply**：`{status: "sent", id, threadId}`
- **Calendar list**：`[{id, summary, start, end, location, description, htmlLink}]`
- **Calendar create**：`{status: "created", id, summary, htmlLink}`
- **Drive search**：`[{id, name, mimeType, modifiedTime, webViewLink}]`
- **Drive get**：`{id, name, mimeType, modifiedTime, size, webViewLink, parents, owners}`
- **Drive upload**：`{status: "uploaded", id, name, mimeType, webViewLink}`
- **Drive download**：`{status: "downloaded", id, name, path, mimeType}`
- **Drive create-folder**：`{status: "created", id, name, webViewLink}`
- **Drive share**：`{status: "shared", permissionId, fileId, role, type}`
- **Drive delete**：`{status: "trashed" | "deleted", fileId, permanent}`
- **Contacts list**：`[{name, emails: [...], phones: [...]}]`
- **Sheets get**：`[[cell, cell, ...], ...]`
- **Sheets create**：`{status: "created", spreadsheetId, title, spreadsheetUrl}`
- **Docs create**：`{status: "created", documentId, title, url}`
- **Docs append**：`{status: "appended", documentId, inserted_at, characters}`

## 规则

1. **未经用户确认，绝不发送邮件、创建/删除日历事件、删除 Drive 文件、共享文件或修改 Docs/Sheets。** 展示将要执行的操作（收件人、文件 ID、内容、共享角色）并请求批准。对于 `drive delete`，优先使用默认的回收站（可恢复）而非 `--permanent`。
2. **首次使用前检查授权**——运行 `setup.py --check`。若失败，引导用户完成设置。
3. **复杂查询使用 Gmail 搜索语法参考**——用 `skill_view("google-workspace", file_path="references/gmail-search-syntax.md")` 加载。
4. **Calendar 时间必须包含时区**——始终使用带偏移的 ISO 8601（如 `2026-03-01T10:00:00-06:00`）或 UTC（`Z`）。
5. **尊重速率限制**——避免快速的连续 API 调用。尽可能批量读取。

## 故障排查

| 问题 | 修复 |
|---------|-----|
| `NOT_AUTHENTICATED` | 运行上方设置步骤 2-5 |
| `REFRESH_FAILED` | 令牌被撤销或过期——重做步骤 3-5 |
| `HttpError 403: Insufficient Permission` | 缺少 API 范围——`$GSETUP --revoke` 后重做步骤 3-5 |
| `AUTHENTICATED (partial)` 或 "Token missing scopes" | 新的写入能力（Drive 写入/删除、Docs 创建/编辑）需要重新授权。`$GSETUP --revoke` 后重做步骤 3-5 以授予升级的范围。 |
| `HttpError 403: Access Not Configured` | API 未启用——用户需在 Google Cloud Console 中启用 |
| `ModuleNotFoundError` | 运行 `$GSETUP --install-deps` |
| 高级保护阻止授权 | Workspace 管理员必须把 OAuth 客户端 ID 加入允许列表 |

## 撤销访问

```bash
$GSETUP --revoke
```
