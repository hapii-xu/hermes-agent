# 审查输出模板

将此作为 PR 审查摘要评论的结构。复制并填写各部分。

## 用于 PR 摘要评论

```markdown
## Code Review Summary

**Verdict: [Approved ✅ | Changes Requested 🔴 | Reviewed 💬]**（[N] 个问题，[N] 个建议）

**PR:** #[number] — [title]
**Author:** @[username]
**Files changed:** [N]（+[additions] -[deletions]）

### 🔴 Critical
<!-- 合并前必须修复的问题 -->
- **file.py:line** — [描述]。建议：[修复]。

### ⚠️ Warnings
<!-- 应该修复但并非严格阻塞的问题 -->
- **file.py:line** — [描述]。

### 💡 Suggestions
<!-- 非阻塞改进、风格偏好、未来考虑 -->
- **file.py:line** — [描述]。

### ✅ Looks Good
<!-- 指出做得好的地方 —— 正向反馈 -->
- [做得好的方面]

---
*Reviewed by Hermes Agent*
```

## 严重程度指南

| 级别 | 图标 | 何时使用 | 是否阻塞合并？ |
|-------|------|-------------|---------------|
| Critical（严重） | 🔴 | 安全漏洞、数据丢失风险、崩溃、核心功能损坏 | 是 |
| Warning（警告） | ⚠️ | 非关键路径的 bug、缺少错误处理、新代码缺少测试 | 通常阻塞 |
| Suggestion（建议） | 💡 | 风格改进、重构想法、性能提示、文档缺口 | 否 |
| Looks Good（良好） | ✅ | 清晰的模式、良好的测试覆盖、清晰的命名、明智的设计决策 | 不适用 |

## 裁决决策

- **Approved ✅（已批准）** —— 零严重/警告项。只有建议或全部清晰。
- **Changes Requested 🔴（已请求更改）** —— 存在任何严重或警告项。
- **Reviewed 💬（已审查）** —— 仅观察（草稿 PR、不确定的发现、信息性）。

## 用于行内评论

为行内评论添加严重程度图标前缀，使其易于扫读：

```
🔴 **Critical:** User input passed directly to SQL query — use parameterized queries to prevent injection.
```

```
⚠️ **Warning:** This error is silently swallowed. At minimum, log it.
```

```
💡 **Suggestion:** This could be simplified with a dict comprehension:
`{k: v for k, v in items if v is not None}`
```

```
✅ **Nice:** Good use of context manager here — ensures cleanup on exceptions.
```

## 用于本地（推送前）审查

在推送前本地审查时，使用相同的结构，但作为给用户的消息呈现而非 PR 评论。跳过 PR 元数据头部，直接从严重程度部分开始。
