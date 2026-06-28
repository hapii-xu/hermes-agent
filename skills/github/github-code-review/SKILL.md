---
name: github-code-review
description: "审查 PR：diff、通过 gh 或 REST 提交行内评论。"
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Code-Review, Pull-Requests, Git, Quality]
    related_skills: [github-auth, github-pr-workflow]
---

# GitHub 代码审查

在推送前审查本地更改，或在 GitHub 上审查开放的 PR。本 skill 大部分使用纯 `git` —— `gh`/`curl` 的区分仅对 PR 级别的交互有影响。

## 前提条件

- 已通过 GitHub 身份验证（见 `github-auth` skill）
- 在 git 仓库内

### 设置（用于 PR 交互）

```bash
if command -v gh &>/dev/null && gh auth status &>/dev/null; then
  AUTH="gh"
else
  AUTH="git"
  if [ -z "$GITHUB_TOKEN" ]; then
    if _hermes_env="${HERMES_HOME:-$HOME/.hermes}/.env"; [ -f "$_hermes_env" ] && grep -q "^GITHUB_TOKEN=" "$_hermes_env"; then
      GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" "$_hermes_env" | head -1 | cut -d= -f2 | tr -d '\n\r')
    elif grep -q "github.com" ~/.git-credentials 2>/dev/null; then
      GITHUB_TOKEN=$(grep "github.com" ~/.git-credentials 2>/dev/null | head -1 | sed 's|https://[^:]*:\([^@]*\)@.*|\1|')
    fi
  fi
fi

REMOTE_URL=$(git remote get-url origin)
OWNER_REPO=$(echo "$REMOTE_URL" | sed -E 's|.*github\.com[:/]||; s|\.git$||')
OWNER=$(echo "$OWNER_REPO" | cut -d/ -f1)
REPO=$(echo "$OWNER_REPO" | cut -d/ -f2)
```

---

## 1. 审查本地更改（推送前）

这是纯 `git` —— 到处可用，不需要 API。

### 获取 diff

```bash
# 已暂存的更改（将会被提交的内容）
git diff --staged

# 相对 main 的所有更改（PR 会包含的内容）
git diff main...HEAD

# 仅文件名
git diff main...HEAD --name-only

# 统计摘要（每个文件的增/删行数）
git diff main...HEAD --stat
```

### 审查策略

1. **先看全局：**

```bash
git diff main...HEAD --stat
git log main..HEAD --oneline
```

2. **逐文件审查** —— 对变更文件使用 `read_file` 获取完整上下文，并用 diff 查看改了什么：

```bash
git diff main...HEAD -- src/auth/login.py
```

3. **检查常见问题：**

```bash
# 遗留的调试语句、TODO、console.log
git diff main...HEAD | grep -n "print(\|console\.log\|TODO\|FIXME\|HACK\|XXX\|debugger"

# 意外暂存的大文件
git diff main...HEAD --stat | sort -t'|' -k2 -rn | head -10

# 密钥或凭证模式
git diff main...HEAD | grep -in "password\|secret\|api_key\|token.*=\|private_key"

# 合并冲突标记
git diff main...HEAD | grep -n "<<<<<<\|>>>>>>\|======="
```

4. **向用户呈现结构化反馈。**

### 审查输出格式

审查本地更改时，按以下结构呈现发现：

```
## Code Review Summary

### Critical
- **src/auth.py:45** — SQL 注入：用户输入直接传入查询。
  建议：使用参数化查询。

### Warnings
- **src/models/user.py:23** — 密码以明文存储。使用 bcrypt 或 argon2。
- **src/api/routes.py:112** — 登录端点没有速率限制。

### Suggestions
- **src/utils/helpers.py:8** — 与 `src/core/utils.py:34` 的逻辑重复。合并。
- **tests/test_auth.py** — 缺少边缘用例：过期 token 测试。

### Looks Good
- 中间件层关注点分离清晰
- 正常路径的测试覆盖良好
```

---

## 2. 审查 GitHub 上的 Pull Request

### 查看 PR 详情

**使用 gh：**

```bash
gh pr view 123
gh pr diff 123
gh pr diff 123 --name-only
```

**使用 git + curl：**

```bash
PR_NUMBER=123

# 获取 PR 详情
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER \
  | python3 -c "
import sys, json
pr = json.load(sys.stdin)
print(f\"Title: {pr['title']}\")
print(f\"Author: {pr['user']['login']}\")
print(f\"Branch: {pr['head']['ref']} -> {pr['base']['ref']}\")
print(f\"State: {pr['state']}\")
print(f\"Body:\n{pr['body']}\")"

# 列出变更文件
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER/files \
  | python3 -c "
import sys, json
for f in json.load(sys.stdin):
    print(f\"{f['status']:10} +{f['additions']:-4} -{f['deletions']:-4}  {f['filename']}\")"
```

### 本地检出 PR 以进行完整审查

这用纯 `git` 即可 —— 不需要 `gh`：

```bash
# 拉取 PR 分支并检出
git fetch origin pull/123/head:pr-123
git checkout pr-123

# 现在可以使用 read_file、search_files、运行测试等

# 查看相对基分支的 diff
git diff main...pr-123
```

**使用 gh（快捷方式）：**

```bash
gh pr checkout 123
```

### 在 PR 上留下评论

**通用 PR 评论 —— 使用 gh：**

```bash
gh pr comment 123 --body "Overall looks good, a few suggestions below."
```

**通用 PR 评论 —— 使用 curl：**

```bash
curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/issues/$PR_NUMBER/comments \
  -d '{"body": "Overall looks good, a few suggestions below."}'
```

### 留下行内审查评论

**单条行内评论 —— 使用 gh（通过 API）：**

```bash
HEAD_SHA=$(gh pr view 123 --json headRefOid --jq '.headRefOid')

gh api repos/$OWNER/$REPO/pulls/123/comments \
  --method POST \
  -f body="This could be simplified with a list comprehension." \
  -f path="src/auth/login.py" \
  -f commit_id="$HEAD_SHA" \
  -f line=45 \
  -f side="RIGHT"
```

**单条行内评论 —— 使用 curl：**

```bash
# 获取 head commit SHA
HEAD_SHA=$(curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['head']['sha'])")

curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER/comments \
  -d "{
    \"body\": \"This could be simplified with a list comprehension.\",
    \"path\": \"src/auth/login.py\",
    \"commit_id\": \"$HEAD_SHA\",
    \"line\": 45,
    \"side\": \"RIGHT\"
  }"
```

### 提交正式审查（批准 / 请求更改）

**使用 gh：**

```bash
gh pr review 123 --approve --body "LGTM!"
gh pr review 123 --request-changes --body "See inline comments."
gh pr review 123 --comment --body "Some suggestions, nothing blocking."
```

**使用 curl —— 多评论审查原子化提交：**

```bash
HEAD_SHA=$(curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['head']['sha'])")

curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER/reviews \
  -d "{
    \"commit_id\": \"$HEAD_SHA\",
    \"event\": \"COMMENT\",
    \"body\": \"Code review from Hermes Agent\",
    \"comments\": [
      {\"path\": \"src/auth.py\", \"line\": 45, \"body\": \"Use parameterized queries to prevent SQL injection.\"},
      {\"path\": \"src/models/user.py\", \"line\": 23, \"body\": \"Hash passwords with bcrypt before storing.\"},
      {\"path\": \"tests/test_auth.py\", \"line\": 1, \"body\": \"Add test for expired token edge case.\"}
    ]
  }"
```

事件值：`"APPROVE"`、`"REQUEST_CHANGES"`、`"COMMENT"`

`line` 字段指的是文件*新*版本中的行号。对于删除的行，使用 `"side": "LEFT"`。

---

## 3. 审查清单

执行代码审查（本地或 PR）时，系统地检查：

### 正确性
- 代码是否做到它声称的？
- 边缘用例是否处理（空输入、null、大数据、并发访问）？
- 错误路径是否优雅处理？

### 安全性
- 没有硬编码的密钥、凭证或 API key
- 面向用户的输入有验证
- 没有 SQL 注入、XSS 或路径遍历
- 需要之处有认证/授权检查

### 代码质量
- 清晰的命名（变量、函数、类）
- 没有不必要的复杂度或过早抽象
- DRY —— 没有应被抽取的重复逻辑
- 函数聚焦（单一职责）

### 测试
- 新代码路径有测试？
- 覆盖正常路径和错误用例？
- 测试可读且可维护？

### 性能
- 没有 N+1 查询或不必要的循环
- 在有益处的地方适当缓存
- 异步代码路径中没有阻塞操作

### 文档
- 公共 API 有文档
- 非显而易见的逻辑有注释解释"为什么"
- 行为变化时更新了 README

---

## 4. 推送前审查工作流

当用户要求你"审查代码"或"推送前检查"时：

1. `git diff main...HEAD --stat` —— 查看变更范围
2. `git diff main...HEAD` —— 阅读完整 diff
3. 对每个变更文件，如果需要更多上下文，使用 `read_file`
4. 应用上面的清单
5. 以结构化格式（Critical / Warnings / Suggestions / Looks Good）呈现发现
6. 如果发现严重问题，在用户推送前主动提出修复

---

## 5. PR 审查工作流（端到端）

当用户要求你"审查 PR #N"、"看下这个 PR"，或给你一个 PR URL 时，遵循此配方：

### 第 1 步：设置环境

```bash
source "${HERMES_HOME:-$HOME/.hermes}/skills/github/github-auth/scripts/gh-env.sh"
# 或运行本 skill 顶部的内联设置块
```

### 第 2 步：收集 PR 上下文

获取 PR 元数据、描述和变更文件列表，在深入代码之前了解范围。

**使用 gh：**
```bash
gh pr view 123
gh pr diff 123 --name-only
gh pr checks 123
```

**使用 curl：**
```bash
PR_NUMBER=123

# PR 详情（标题、作者、描述、分支）
curl -s -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$GH_OWNER/$GH_REPO/pulls/$PR_NUMBER

# 带行数的变更文件
curl -s -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$GH_OWNER/$GH_REPO/pulls/$PR_NUMBER/files
```

### 第 3 步：本地检出 PR

这让你能完整访问 `read_file`、`search_files`，以及运行测试的能力。

```bash
git fetch origin pull/$PR_NUMBER/head:pr-$PR_NUMBER
git checkout pr-$PR_NUMBER
```

### 第 4 步：阅读 diff 并理解变更

```bash
# 相对基分支的完整 diff
git diff main...HEAD

# 或对大型 PR 逐文件查看
git diff main...HEAD --name-only
# 然后对每个文件：
git diff main...HEAD -- path/to/file.py
```

对每个变更文件，使用 `read_file` 查看变更周围的完整上下文 —— 单凭 diff 可能会遗漏只有结合周围代码才能发现的问题。

### 第 5 步：在本地运行自动检查（如适用）

```bash
# 如果有测试套件则运行测试
python -m pytest 2>&1 | tail -20
# 或：npm test, cargo test, go test ./..., 等

# 如果配置了 linter 则运行
ruff check . 2>&1 | head -30
# 或：eslint, clippy, 等
```

### 第 6 步：应用审查清单（第 3 节）

逐个类别过：正确性、安全性、代码质量、测试、性能、文档。

### 第 7 步：把审查发布到 GitHub

收集你的发现并作为带行内评论的正式审查提交。

**使用 gh：**
```bash
# 如果没有问题 —— 批准
gh pr review $PR_NUMBER --approve --body "Reviewed by Hermes Agent. Code looks clean — good test coverage, no security concerns."

# 如果发现问题 —— 请求更改并附行内评论
gh pr review $PR_NUMBER --request-changes --body "Found a few issues — see inline comments."
```

**使用 curl —— 带多条行内评论的原子审查：**
```bash
HEAD_SHA=$(curl -s -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$GH_OWNER/$GH_REPO/pulls/$PR_NUMBER \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['head']['sha'])")

# 构建审查 JSON —— event 为 APPROVE、REQUEST_CHANGES 或 COMMENT
curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$GH_OWNER/$GH_REPO/pulls/$PR_NUMBER/reviews \
  -d "{
    \"commit_id\": \"$HEAD_SHA\",
    \"event\": \"REQUEST_CHANGES\",
    \"body\": \"## Hermes Agent Review\n\nFound 2 issues, 1 suggestion. See inline comments.\",
    \"comments\": [
      {\"path\": \"src/auth.py\", \"line\": 45, \"body\": \"🔴 **Critical:** User input passed directly to SQL query — use parameterized queries.\"},
      {\"path\": \"src/models.py\", \"line\": 23, \"body\": \"⚠️ **Warning:** Password stored without hashing.\"},
      {\"path\": \"src/utils.py\", \"line\": 8, \"body\": \"💡 **Suggestion:** This duplicates logic in core/utils.py:34.\"}
    ]
  }"
```

### 第 8 步：同时发布摘要评论

除了行内评论，留下顶层摘要以便 PR 作者一眼就能看到全貌。使用 `references/review-output-template.md` 中的审查输出格式。

**使用 gh：**
```bash
gh pr comment $PR_NUMBER --body "$(cat <<'EOF'
## Code Review Summary

**Verdict: Changes Requested** (2 issues, 1 suggestion)

### 🔴 Critical
- **src/auth.py:45** — SQL injection vulnerability

### ⚠️ Warnings
- **src/models.py:23** — Plaintext password storage

### 💡 Suggestions
- **src/utils.py:8** — Duplicated logic, consider consolidating

### ✅ Looks Good
- Clean API design
- Good error handling in the middleware layer

---
*Reviewed by Hermes Agent*
EOF
)"
```

### 第 9 步：清理

```bash
git checkout main
git branch -D pr-$PR_NUMBER
```

### 决策：批准 vs 请求更改 vs 评论

- **批准** —— 没有严重或警告级别的问题，只有次要建议或全部清晰
- **请求更改** —— 有任何应在合并前修复的严重或警告级别问题
- **评论** —— 观察和建议，但没有阻塞项（当你不确定或 PR 是草稿时使用）
