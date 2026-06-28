# Gmail 搜索语法

`query` 参数支持标准的 Gmail 搜索运算符。

## 常用运算符

| 运算符 | 示例 | 描述 |
|----------|---------|-------------|
| `is:unread` | `is:unread` | 未读邮件 |
| `is:starred` | `is:starred` | 已加星邮件 |
| `is:important` | `is:important` | 重要邮件 |
| `in:inbox` | `in:inbox` | 仅收件箱 |
| `in:sent` | `in:sent` | 已发送文件夹 |
| `in:drafts` | `in:drafts` | 草稿 |
| `in:trash` | `in:trash` | 回收站 |
| `in:anywhere` | `in:anywhere` | 所有邮件，包括垃圾邮件/回收站 |
| `from:` | `from:alice@example.com` | 发件人 |
| `to:` | `to:bob@example.com` | 收件人 |
| `cc:` | `cc:team@example.com` | 抄送收件人 |
| `subject:` | `subject:invoice` | 主题包含 |
| `label:` | `label:work` | 含标签 |
| `has:attachment` | `has:attachment` | 含附件 |
| `filename:` | `filename:pdf` | 附件文件名/类型 |
| `larger:` | `larger:5M` | 大于此大小 |
| `smaller:` | `smaller:1M` | 小于此大小 |

## 日期运算符

| 运算符 | 示例 | 描述 |
|----------|---------|-------------|
| `newer_than:` | `newer_than:7d` | 最近 N 天 (d)、月 (m)、年 (y) 内 |
| `older_than:` | `older_than:30d` | 早于 N 天/月/年 |
| `after:` | `after:2026/02/01` | 某日期之后 (YYYY/MM/DD) |
| `before:` | `before:2026/03/01` | 某日期之前 |

## 组合

| 语法 | 示例 | 描述 |
|--------|---------|-------------|
| 空格 | `from:alice subject:meeting` | 与（隐式） |
| `OR` | `from:alice OR from:bob` | 或 |
| `-` | `-from:noreply@` | 非（排除） |
| `()` | `(from:alice OR from:bob) subject:meeting` | 分组 |
| `""` | `"exact phrase"` | 精确短语匹配 |

## 常用模式

```
# 最近一天的未读邮件
is:unread newer_than:1d

# 来自特定发件人、带 PDF 附件的邮件
from:accounting@company.com has:attachment filename:pdf

# 重要的未读邮件（排除推广/社交）
is:unread -category:promotions -category:social

# 某主题相关的邮件线程
subject:"Q4 budget" newer_than:30d

# 待清理的大附件
has:attachment larger:10M older_than:90d
```
