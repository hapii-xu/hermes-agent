---
name: notion
description: "Notion API + ntn CLI：页面、数据库、Markdown、Workers。"
version: 2.0.0
author: community
license: MIT
platforms: [linux, macos, windows]
prerequisites:
  env_vars: [NOTION_API_KEY]
metadata:
  hermes:
    tags: [Notion, Productivity, Notes, Database, API, CLI, Workers]
    homepage: https://developers.notion.com
---

# Notion

有两种方式与 Notion 交互。同一个集成 token 对两种方式都适用——根据可用情况选择。

◆ **`ntn` CLI** —— Notion 的官方 CLI。语法更简短、支持单行文件上传，是使用 Workers 的必备工具。截至 2026 年 5 月仅支持 macOS + Linux（Windows 支持"即将推出"）。**已安装时为默认选择。**
◆ **HTTP + curl** —— 到处都能用，包括 Windows。当 `ntn` 未安装时的**默认回退方案**。

## 设置

### 1. 获取一个集成 token（两条路径都需要）

1. 在 https://notion.so/my-integrations 创建一个集成
2. 复制 API 密钥（以 `ntn_` 或 `secret_` 开头）
3. 存入 `${HERMES_HOME:-~/.hermes}/.env`：
   ```
   NOTION_API_KEY=ntn_your_key_here
   ```
4. 在 Notion 中**将目标页面/数据库分享给该集成**：页面菜单 `...` → `Connect to` → 你的集成名称。否则，即使该页面存在，API 也会对该页面返回 404。

### 2. 安装 `ntn`（macOS / Linux 上的推荐路径）

```bash
# 推荐
curl -fsSL https://ntn.dev | bash

# 或通过 npm（需要 Node 22+，npm 10+）
npm install --global ntn

ntn --version    # 验证
```

**跳过 `ntn login`——改用集成 token。** 这样可以无头运行，无需浏览器：
```bash
export NOTION_API_TOKEN=$NOTION_API_KEY      # ntn 读取 NOTION_API_TOKEN
export NOTION_KEYRING=0                       # 不要尝试使用 OS 钥匙串
```

把这些 export 加入你的 shell 配置文件（或加入 `${HERMES_HOME:-~/.hermes}/.env`），这样每个会话都会继承它们。

### 3. 运行时选择路径

```bash
if command -v ntn >/dev/null 2>&1; then
  # 使用 ntn
else
  # 回退到 curl
fi
```

Windows 用户：在原生 `ntn` 发布前完全跳过第 2 步——路径 B 工作良好。如果你现在就想要 CLI 的体验，可以在 WSL2 中安装 `ntn`。

## API 基础

所有 HTTP 请求都需要 `Notion-Version: 2025-09-03`。`ntn` 会替你处理这一点。在这个版本中，用户口中的"databases"在 API 里被称为**data sources**（数据源）。

## 路径 A —— `ntn` CLI（推荐，macOS / Linux）

### 原始 API 调用（curl 的简写形式）
```bash
ntn api v1/users                                  # GET
ntn api v1/pages parent[page_id]=abc123 \         # 带内联请求体的 POST
  properties[title][0][text][content]="Notes"
ntn api v1/pages/abc123 -X PATCH archived:=true   # PATCH；:= 表示非字符串（布尔/数字/null）
```

语法说明：
- `key=value` —— 字符串字段
- `key[nested]=value` —— 嵌套对象字段
- `key:=value` —— 带类型赋值（布尔值、数字、null、数组）

### 搜索
```bash
ntn api v1/search query="page title"
```

### 读取页面元数据
```bash
ntn api v1/pages/{page_id}
```

### 以 Markdown 读取页面（对 agent 友好）
```bash
ntn api v1/pages/{page_id}/markdown
```

### 以 block 形式读取页面内容
```bash
ntn api v1/blocks/{page_id}/children
```

### 从 Markdown 创建页面
```bash
ntn api v1/pages \
  parent[page_id]=xxx \
  properties[title][0][text][content]="Notes from meeting" \
  markdown="# Agenda

- Q3 roadmap
- Hiring"
```

### 用 Markdown 给页面打补丁
```bash
ntn api v1/pages/{page_id}/markdown -X PATCH \
  markdown="## Update

Shipped the prototype."
```

### 查询数据库（data source）
```bash
ntn api v1/data_sources/{data_source_id}/query -X POST \
  filter[property]=Status filter[select][equals]=Active
```

对于带 `sorts`、多个 filter 子句或复合逻辑的复杂查询，可通过管道传入 JSON：
```bash
echo '{"filter": {"property": "Status", "select": {"equals": "Active"}}, "sorts": [{"property": "Date", "direction": "descending"}]}' | \
  ntn api v1/data_sources/{data_source_id}/query -X POST --json -
```

### 文件上传（单行命令——CLI 的最大优势）
```bash
ntn files create < photo.png
ntn files create --external-url https://example.com/photo.png
ntn files list
```

相比之下，HTTP 流程需要 3 步（创建上传 → PUT 字节 → 引用）。

### 实用的环境变量
| 变量 | 作用 |
|---|---|
| `NOTION_API_TOKEN` | 认证 token（覆盖钥匙串）——将其设为你的集成 token |
| `NOTION_KEYRING=0` | 使用基于文件的凭据 `~/.config/notion/auth.json`，而非 OS 钥匙串 |
| `NOTION_WORKSPACE_ID` | 跳过工作区选择提示 |

## 路径 B —— HTTP + curl（跨平台，Windows 上的默认方案）

所有请求都遵循这个模式：

```bash
curl -s -X GET "https://api.notion.com/v1/..." \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json"
```

在 Windows 上，Windows 10+ 自带的 `curl` 可直接使用。PowerShell 用户也可以使用 `Invoke-RestMethod`。

### 搜索
```bash
curl -s -X POST "https://api.notion.com/v1/search" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{"query": "page title"}'
```

### 读取页面元数据
```bash
curl -s "https://api.notion.com/v1/pages/{page_id}" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03"
```

### 以 Markdown 读取页面（对 agent 友好）

比 block JSON 更容易喂给模型。

```bash
curl -s "https://api.notion.com/v1/pages/{page_id}/markdown" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03"
```

### 以 block 形式读取页面内容（当你需要结构时）
```bash
curl -s "https://api.notion.com/v1/blocks/{page_id}/children" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03"
```

### 从 Markdown 创建页面

`POST /v1/pages` 接受一个 `markdown` 请求体参数。

```bash
curl -s -X POST "https://api.notion.com/v1/pages" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{
    "parent": {"page_id": "xxx"},
    "properties": {"title": [{"text": {"content": "Notes from meeting"}}]},
    "markdown": "# Agenda\n\n- Q3 roadmap\n- Hiring\n\n## Decisions\n- Ship MVP Friday"
  }'
```

### 用 Markdown 给页面打补丁
```bash
curl -s -X PATCH "https://api.notion.com/v1/pages/{page_id}/markdown" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{"markdown": "## Update\n\nShipped the prototype."}'
```

### 在数据库中创建页面（带类型的属性）
```bash
curl -s -X POST "https://api.notion.com/v1/pages" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{
    "parent": {"database_id": "xxx"},
    "properties": {
      "Name": {"title": [{"text": {"content": "New Item"}}]},
      "Status": {"select": {"name": "Todo"}}
    }
  }'
```

### 查询数据库（data source）
```bash
curl -s -X POST "https://api.notion.com/v1/data_sources/{data_source_id}/query" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{
    "filter": {"property": "Status", "select": {"equals": "Active"}},
    "sorts": [{"property": "Date", "direction": "descending"}]
  }'
```

### 创建数据库
```bash
curl -s -X POST "https://api.notion.com/v1/data_sources" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{
    "parent": {"page_id": "xxx"},
    "title": [{"text": {"content": "My Database"}}],
    "properties": {
      "Name": {"title": {}},
      "Status": {"select": {"options": [{"name": "Todo"}, {"name": "Done"}]}},
      "Date": {"date": {}}
    }
  }'
```

### 更新页面属性
```bash
curl -s -X PATCH "https://api.notion.com/v1/pages/{page_id}" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{"properties": {"Status": {"select": {"name": "Done"}}}}'
```

### 向页面追加 block
```bash
curl -s -X PATCH "https://api.notion.com/v1/blocks/{page_id}/children" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{
    "children": [
      {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": "Hello from Hermes!"}}]}}
    ]
  }'
```

### 文件上传（3 步流程）
```bash
# 1. 创建上传
curl -s -X POST "https://api.notion.com/v1/file_uploads" \
  -H "Authorization: Bearer $NOTION_API_KEY" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" \
  -d '{"filename": "photo.png", "content_type": "image/png"}'

# 2. 向上面返回的 upload_url PUT 字节
curl -s -X PUT "{upload_url}" --data-binary @photo.png

# 3. 在页面/block 的请求体中引用 {file_upload_id}
```

## 属性类型

数据库条目常用的属性格式：

- **Title：** `{"title": [{"text": {"content": "..."}}]}`
- **Rich text：** `{"rich_text": [{"text": {"content": "..."}}]}`
- **Select：** `{"select": {"name": "Option"}}`
- **Multi-select：** `{"multi_select": [{"name": "A"}, {"name": "B"}]}`
- **Date：** `{"date": {"start": "2026-01-15", "end": "2026-01-16"}}`
- **Checkbox：** `{"checkbox": true}`
- **Number：** `{"number": 42}`
- **URL：** `{"url": "https://..."}`
- **Email：** `{"email": "user@example.com"}`
- **Relation：** `{"relation": [{"id": "page_id"}]}`

## API 版本 2025-09-03 —— 数据库与数据源

- **数据库变成了数据源。** 查询和检索时使用 `/data_sources/` 端点。
- **每个数据库有两个 ID：** `database_id` 和 `data_source_id`。
  - 创建页面时用 `database_id`：`parent: {"database_id": "..."}`
  - 查询时用 `data_source_id`：`POST /v1/data_sources/{id}/query`
- 搜索返回的数据库以 `"object": "data_source"` 形式呈现，带 `data_source_id` 字段。

## Notion Workers（高级功能，需要 `ntn`）

Workers 是 Notion 为你托管的 TypeScript 程序。一个 worker 可以暴露以下任意组合：
- **Syncs** —— 按计划（默认 30 分钟）从外部 API 拉取数据到 Notion 数据库。
- **Tools** —— 在 Notion 的自定义 Agent 中以可调用工具的形式出现。
- **Webhooks** —— 接收来自外部服务（GitHub、Stripe 等）的 HTTP 事件并在 Notion 中执行操作。

**套餐 / 平台限制：**
- CLI 在所有套餐上都能用。**部署 Workers 需要 Business 或 Enterprise 套餐。**
- 截至 2026 年 5 月，`ntn` 仅支持 macOS/Linux。Windows 用户需要 WSL2 或等待原生支持。
- 2026 年 8 月 11 日前免费；之后按 Notion 额度计量计费。

### 最小 Worker

```bash
ntn workers new my-worker      # 脚手架
cd my-worker
# 编辑 src/index.ts
ntn workers deploy --name my-worker
```

`src/index.ts`：
```typescript
import { Worker } from "@notionhq/workers";

const worker = new Worker();
export default worker;

worker.tool("greet", {
  title: "Greet a User",
  description: "Returns a friendly greeting",
  inputSchema: { type: "object", properties: { name: { type: "string" } }, required: ["name"] },
  execute: async ({ name }) => `Hello, ${name}!`,
});
```

### Webhook 能力

```typescript
worker.webhook("onGithubPush", {
  title: "GitHub Push Handler",
  execute: async (events, { notion }) => {
    for (const event of events) {
      // event.body、event.rawBody（用于签名验证）、event.headers
      console.log("got delivery", event.deliveryId);
    }
  },
});
```

部署后：`ntn workers webhooks list` 会显示 Notion 生成的 URL。请把该 URL 当作机密处理——除非你添加签名验证，否则任何拿到它的人都可以 POST 事件。

### Worker 生命周期命令

```bash
ntn workers deploy
ntn workers list
ntn workers exec <capability-key> -d '{"name": "world"}'
ntn workers sync trigger <key>            # 立即运行一次 sync
ntn workers sync pause <key>
ntn workers env set GITHUB_WEBHOOK_SECRET=...
ntn workers runs list                     # 最近的调用
ntn workers runs logs <run-id>
ntn workers webhooks list
```

当被要求构建 Worker 时，用 `ntn workers new` 搭建脚手架，在 `src/index.ts` 中编写代码，用 `ntn workers env set` 设置所需的密钥，然后部署。Notion 的文档 https://developers.notion.com/workers 涵盖了完整的 API 能力。

## Notion 风味的 Markdown（由 `/markdown` 端点使用）

标准 CommonMark 加上用于 Notion 专属 block 的类 XML 标签。缩进请使用 **制表符（tab）**。

**CommonMark 之外的 block：**
```
<callout icon="🎯" color="blue_bg">
	Ship the MVP by **Friday**.
</callout>

<details color="gray">
<summary>Toggle title</summary>
	子内容缩进一个制表符
</details>

<columns>
	<column>Left side</column>
	<column>Right side</column>
</columns>

<table_of_contents color="gray"/>
```

**行内：**
- 提及：`<mention-user url="..."/>`、`<mention-page url="...">Title</mention-page>`、`<mention-date start="2026-05-15"/>`
- 下划线：`<span underline="true">text</span>`
- 颜色：`<span color="blue">text</span>` 或在首行的 block 级 `{color="blue"}`
- 数学：行内 `$x^2$`，块级 `$$ ... $$`
- 引用：`[^https://example.com]`

**颜色：** `gray brown orange yellow green blue purple pink red`，以及用于背景的 `*_bg` 变体。

标题 5/6 会折叠为 H4。多行 `>` 会渲染成独立的引用块——要在单个 `>` 内实现多行引用，请使用 `<br>`。

## 选择正确的路径

| 任务 | mac / Linux | Windows |
|---|---|---|
| 读/写页面、搜索、查询数据库 | `ntn api ...` | curl |
| 读取页面供 agent 总结 | `ntn api v1/pages/{id}/markdown` | curl `/markdown` 端点 |
| 上传文件 | `ntn files create < file` | 3 步 HTTP 流程 |
| 一次性 API 探索 | `ntn api ...` | curl |
| 构建由 Notion 托管的 sync / webhook / agent tool | `ntn workers ...` | WSL2 + `ntn workers ...` |

## 注意事项

- 页面/数据库 ID 是 UUID（带或不带连字符——两者都被接受）。
- 速率限制：平均约 3 次请求/秒。CLI 不会绕过此限制。
- API 无法设置数据库的**视图**过滤器——那只能在 UI 中设置。
- 创建数据源时使用 `"is_inline": true` 可将其内嵌到页面中。
- 始终给 curl 加上 `-s` 以抑制进度条（agent 输出更干净）。
- 读取时通过 `jq` 处理 JSON：`... | jq '.results[0].properties'`。
- Notion 现在也提供了一个 MCP 服务器（`Notion MCP`，在数据库操作上比上一版节省约 91% 的 token）——如果你想在一个会话中获得流式 Notion 访问能力，可通过 Hermes 的 MCP 支持来接入；但对于大多数一次性任务，上面的路径已经足够。
