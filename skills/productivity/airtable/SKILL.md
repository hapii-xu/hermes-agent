---
name: airtable
description: 通过 curl 使用 Airtable REST API。记录的增删改查、筛选、upsert。
version: 1.1.0
author: community
license: MIT
platforms: [linux, macos, windows]
prerequisites:
  env_vars: [AIRTABLE_API_KEY]
  commands: [curl]
metadata:
  hermes:
    tags: [Airtable, Productivity, Database, API]
    homepage: https://airtable.com/developers/web/api/introduction
---

# Airtable — Base、Table 与 Record

通过 `curl` 直接使用 Airtable 的 REST API（借助 `terminal` 工具）。无需 MCP 服务器，无需 OAuth 流程，也无需 Python SDK——只用 `curl` 和一个个人访问令牌即可。

## 前置条件

1. 在 https://airtable.com/create/tokens 创建一个**个人访问令牌（Personal Access Token，PAT）**（令牌以 `pat...` 开头）。
2. 授予以下权限（最低要求）：
   - `data.records:read` — 读取行
   - `data.records:write` — 创建 / 更新 / 删除行
   - `schema.bases:read` — 列出 base 和 table
3. **重要：** 在同一个令牌界面中，把你想要访问的每个 base 加入该令牌的 **Access** 列表。PAT 是按 base 限定作用域的——一个有效令牌用在错误的 base 上会返回 `403`。
4. 把令牌存入 `${HERMES_HOME:-~/.hermes}/.env`（或通过 `hermes setup`）：
   ```
   AIRTABLE_API_KEY=pat_your_token_here
   ```

> 注意：旧版 `key...` API key 已于 2024 年 2 月被弃用。现在只有 PAT 和 OAuth token 能用。

## API 基础

- **Endpoint：** `https://api.airtable.com/v0`
- **认证头：** `Authorization: Bearer $AIRTABLE_API_KEY`
- **所有请求**都使用 JSON（任何 POST/PATCH/PUT body 都要带 `Content-Type: application/json`）。
- **对象 ID：** base 为 `app...`，table 为 `tbl...`，record 为 `rec...`，field 为 `fld...`。ID 永远不会变；名字可能会变。自动化中优先使用 ID。
- **速率限制：** 每个 base 每秒 5 次请求。`429` → 退避重试。在单个 base 上突发会被限流。

基础 curl 模式：
```bash
curl -s "https://api.airtable.com/v0/$BASE_ID/$TABLE?maxRecords=5" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```

`-s` 会抑制 curl 的进度条——每次调用都保持设置，这样工具输出对 Hermes 来说是干净的。通过 `python3 -m json.tool`（始终可用）或 `jq`（如果已安装）管道处理，可以得到可读的 JSON。

## 字段类型（请求体形状）

| 字段类型 | 写入形状 |
|---|---|
| 单行文本 | `"Name": "hello"` |
| 长文本 | `"Notes": "multi\nline"` |
| 数字 | `"Score": 42` |
| 复选框 | `"Done": true` |
| 单选 | `"Status": "Todo"`（除非带 `typecast: true`，否则选项名必须已存在） |
| 多选 | `"Tags": ["urgent", "bug"]` |
| 日期 | `"Due": "2026-04-01"` |
| DateTime（UTC） | `"At": "2026-04-01T14:30:00.000Z"` |
| URL / Email / Phone | `"Link": "https://…"` |
| 附件 | `"Files": [{"url": "https://…"}]`（Airtable 会抓取并重新托管） |
| 关联记录 | `"Owner": ["recXXXXXXXXXXXXXX"]`（record ID 数组） |
| 用户 | `"AssignedTo": {"id": "usrXXXXXXXXXXXXXX"}` |

在创建/更新 body 的顶层传 `"typecast": true`，可让 Airtable 自动强制转换值（例如即时创建新的单选选项、把 `"42"` → `42`）。

## 常见查询

### 列出令牌可见的 base
```bash
curl -s "https://api.airtable.com/v0/meta/bases" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```

### 列出某个 base 的 table + schema
```bash
curl -s "https://api.airtable.com/v0/meta/bases/$BASE_ID/tables" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```
在做任何修改**之前**使用它——确认确切的字段名和 ID，查看单选字段的 `options.choices`，并显示主字段名。

### 列出记录（前 10 条）
```bash
curl -s "https://api.airtable.com/v0/$BASE_ID/$TABLE?maxRecords=10" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```

### 获取单条记录
```bash
curl -s "https://api.airtable.com/v0/$BASE_ID/$TABLE/$RECORD_ID" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```

### 筛选记录（filterByFormula）
Airtable 公式必须做 URL 编码。交给 Python 标准库来做——绝不要手动编码：
```bash
FORMULA="{Status}='Todo'"
ENC=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$FORMULA")
curl -s "https://api.airtable.com/v0/$BASE_ID/$TABLE?filterByFormula=$ENC&maxRecords=20" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```

常用公式模式：
- 精确匹配：`{Email}='user@example.com'`
- 包含：`FIND('bug', LOWER({Title}))`
- 多条件：`AND({Status}='Todo', {Priority}='High')`
- 或：`OR({Owner}='alice', {Owner}='bob')`
- 非空：`NOT({Assignee}='')`
- 日期比较：`IS_AFTER({Due}, TODAY())`

### 排序 + 选择特定字段
```bash
curl -s "https://api.airtable.com/v0/$BASE_ID/$TABLE?sort%5B0%5D%5Bfield%5D=Priority&sort%5B0%5D%5Bdirection%5D=asc&fields%5B%5D=Name&fields%5B%5D=Status" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```
查询参数中的方括号**必须**做 URL 编码（`%5B` / `%5D`）。

### 使用命名视图
```bash
curl -s "https://api.airtable.com/v0/$BASE_ID/$TABLE?view=Grid%20view&maxRecords=50" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```
视图会在服务端应用其保存的筛选 + 排序。

## 常见变更操作

### 创建一条记录
```bash
curl -s -X POST "https://api.airtable.com/v0/$BASE_ID/$TABLE" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"fields":{"Name":"New task","Status":"Todo","Priority":"High"}}' | python3 -m json.tool
```

### 一次调用创建最多 10 条记录
```bash
curl -s -X POST "https://api.airtable.com/v0/$BASE_ID/$TABLE" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "typecast": true,
    "records": [
      {"fields": {"Name": "Task A", "Status": "Todo"}},
      {"fields": {"Name": "Task B", "Status": "In progress"}}
    ]
  }' | python3 -m json.tool
```
批量端点上限是**每次请求 10 条记录**。更大批量的插入，请按 10 条一组循环并短暂 sleep，以遵守 5 req/sec/base 的预算。

### 更新一条记录（PATCH — 合并，保留未变更字段）
```bash
curl -s -X PATCH "https://api.airtable.com/v0/$BASE_ID/$TABLE/$RECORD_ID" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"fields":{"Status":"Done"}}' | python3 -m json.tool
```

### 按合并字段 upsert（无需 ID）
```bash
curl -s -X PATCH "https://api.airtable.com/v0/$BASE_ID/$TABLE" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "performUpsert": {"fieldsToMergeOn": ["Email"]},
    "records": [
      {"fields": {"Email": "user@example.com", "Status": "Active"}}
    ]
  }' | python3 -m json.tool
```
`performUpsert` 会为合并字段值为新值的记录执行创建，为合并字段值已存在的记录执行 patch。非常适合幂等的同步。

### 删除一条记录
```bash
curl -s -X DELETE "https://api.airtable.com/v0/$BASE_ID/$TABLE/$RECORD_ID" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```

### 一次调用删除最多 10 条记录
```bash
curl -s -X DELETE "https://api.airtable.com/v0/$BASE_ID/$TABLE?records%5B%5D=rec1&records%5B%5D=rec2" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" | python3 -m json.tool
```

## 分页

列表端点每页最多返回 **100 条记录**。如果响应里包含 `"offset": "..."`，就在下次调用时把它传回去。循环直到该字段不存在：

```bash
OFFSET=""
while :; do
  URL="https://api.airtable.com/v0/$BASE_ID/$TABLE?pageSize=100"
  [ -n "$OFFSET" ] && URL="$URL&offset=$OFFSET"
  RESP=$(curl -s "$URL" -H "Authorization: Bearer $AIRTABLE_API_KEY")
  echo "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); [print(r["id"], r["fields"].get("Name","")) for r in d["records"]]'
  OFFSET=$(echo "$RESP" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("offset",""))')
  [ -z "$OFFSET" ] && break
done
```

## 典型的 Hermes 工作流

1. **确认认证。** `curl -s -o /dev/null -w "%{http_code}\n" https://api.airtable.com/v0/meta/bases -H "Authorization: Bearer $AIRTABLE_API_KEY"` —— 期望返回 `200`。
2. **找到 base。** 列出 base（上一步）或直接向用户索取 `app...` ID（当令牌缺少 `schema.bases:read` 时）。
3. **检查 schema。** `GET /v0/meta/bases/$BASE_ID/tables` —— 在做任何修改之前，把确切的字段名和主字段名缓存在会话本地。
4. **先读后写。** 对于「在 Y 条件下更新 X」，先用 `filterByFormula` 解析出 `rec...` ID，再 `PATCH /v0/$BASE_ID/$TABLE/$RECORD_ID`。绝不要猜测 record ID。
5. **批量写入。** 把相关的创建合并成一次最多 10 条记录的 POST，以保持在 5 req/sec 预算内。
6. **破坏性操作。** 删除无法通过 API 撤销。如果用户说「删除所有 X」，先回显筛选条件 + 记录数量并确认，再执行。

## 易错点

- **`filterByFormula` 必须做 URL 编码。** 含空格或非 ASCII 的字段名也需要编码（`{My Field}` → `%7BMy%20Field%7D`）。使用 Python 标准库（上面的模式）——绝不要手动转义。
- **空字段会从响应中省略。** 缺少 `"Assignee"` 键并不意味着该字段不存在——而是该记录的值为空。在得出字段缺失结论之前，先检查 schema（第 3 步）。
- **PATCH vs PUT。** `PATCH` 把提供的字段合并进记录。`PUT` 会完全替换记录，并清空任何未包含的字段。默认使用 `PATCH`。
- **单选选项必须已存在。** 当 `Shipping` 不在该字段选项列表中时，写 `"Status": "Shipping"` 会以 `INVALID_MULTIPLE_CHOICE_OPTIONS` 报错，除非你传 `"typecast": true`（会自动创建该选项）。
- **按 base 限定令牌作用域。** 一个 base 返回 `403` 而另一个正常，意味着该令牌的 Access 列表不包含那个 base——而不是作用域或认证问题。请让用户到 https://airtable.com/create/tokens 授权。
- **速率限制按 base 而非按 token。** 在 `baseA` 上 5 req/sec、同时在 `baseB` 上 5 req/sec 是可以的；仅在 `baseA` 上 6 req/sec 就会被限流。监视 `429` 上的 `Retry-After` 头。

## 给 Hermes 的重要提示

- **始终通过 `terminal` 工具使用 `curl`。** 不要用 `web_extract`（它无法发送认证头）或 `browser_navigate`（需要 UI 认证且速度慢）。
- **在本 skill 加载时，`AIRTABLE_API_KEY` 会从 `${HERMES_HOME:-~/.hermes}/.env` 自动流入子进程** —— 无需在每次 `curl` 调用前重新 export。
- **小心转义公式中的花括号。** 在 heredoc body 中，`{Status}` 是字面量。在 shell 参数中，`{Status}` 在 `{...}` 花括号展开上下文之外是安全的——但动态字符串要先经 `python3 urllib.parse.quote` 处理再拼接到 URL。
- **用 `python3 -m json.tool` 美化输出**（始终可用），而不是 `jq`（可选）。只有需要过滤/投影时才用 `jq`。
- **分页是按页而非全局。** Airtable 的 100 条记录上限是硬性限制；没有办法绕过。用 `offset` 循环直到该字段不存在。
- **在非 2xx 响应上读取 `errors` 数组** —— Airtable 会返回结构化的错误码，如 `AUTHENTICATION_REQUIRED`、`INVALID_PERMISSIONS`、`MODEL_ID_NOT_FOUND`、`INVALID_MULTIPLE_CHOICE_OPTIONS`，能精确告诉你哪里出错了。
