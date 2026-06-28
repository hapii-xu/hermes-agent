# Notion Block 类型

通过 API 创建和读取所有常见 Notion block 类型的参考。

## 创建 block

使用 `PATCH /v1/blocks/{page_id}/children` 并附带一个 `children` 数组。每个 block 遵循这个结构：

```json
{"object": "block", "type": "<type>", "<type>": { ... }}
```

### 段落

```json
{"type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": "Hello world"}}]}}
```

### 标题

```json
{"type": "heading_1", "heading_1": {"rich_text": [{"text": {"content": "Title"}}]}}
{"type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": "Section"}}]}}
{"type": "heading_3", "heading_3": {"rich_text": [{"text": {"content": "Subsection"}}]}}
```

### 无序列表

```json
{"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"text": {"content": "Item"}}]}}
```

### 有序列表

```json
{"type": "numbered_list_item", "numbered_list_item": {"rich_text": [{"text": {"content": "Step 1"}}]}}
```

### 待办 / 复选框

```json
{"type": "to_do", "to_do": {"rich_text": [{"text": {"content": "Task"}}], "checked": false}}
```

### 引用

```json
{"type": "quote", "quote": {"rich_text": [{"text": {"content": "Something wise"}}]}}
```

### 标注（Callout）

```json
{"type": "callout", "callout": {"rich_text": [{"text": {"content": "Important note"}}], "icon": {"emoji": "💡"}}}
```

### 代码

```json
{"type": "code", "code": {"rich_text": [{"text": {"content": "print('hello')"}}], "language": "python"}}
```

### 折叠块（Toggle）

```json
{"type": "toggle", "toggle": {"rich_text": [{"text": {"content": "Click to expand"}}]}}
```

### 分隔线

```json
{"type": "divider", "divider": {}}
```

### 书签

```json
{"type": "bookmark", "bookmark": {"url": "https://example.com"}}
```

### 图片（外部 URL）

```json
{"type": "image", "image": {"type": "external", "external": {"url": "https://example.com/photo.png"}}}
```

## 读取 block

从 `GET /v1/blocks/{page_id}/children` 读取 block 时，每个 block 都有一个 `type` 字段。按如下方式提取可读文本：

| 类型 | 文本位置 | 额外字段 |
|------|--------------|--------------|
| `paragraph` | `.paragraph.rich_text` | — |
| `heading_1/2/3` | `.heading_N.rich_text` | — |
| `bulleted_list_item` | `.bulleted_list_item.rich_text` | — |
| `numbered_list_item` | `.numbered_list_item.rich_text` | — |
| `to_do` | `.to_do.rich_text` | `.to_do.checked`（布尔值） |
| `toggle` | `.toggle.rich_text` | 有子 block |
| `code` | `.code.rich_text` | `.code.language` |
| `quote` | `.quote.rich_text` | — |
| `callout` | `.callout.rich_text` | `.callout.icon.emoji` |
| `divider` | — | — |
| `image` | `.image.caption` | `.image.file.url` 或 `.image.external.url` |
| `bookmark` | `.bookmark.caption` | `.bookmark.url` |
| `child_page` | — | `.child_page.title` |
| `child_database` | — | `.child_database.title` |

rich_text 数组包含带 `.plain_text` 的对象——将它们拼接起来即可得到可读输出。

---

*由 [@dogiladeveloper](https://github.com/dogiladeveloper) 贡献*
