---
name: github-pr-workflow
description: "GitHub PR 生命周期：建分支、提交、开启 PR、CI、合并。"
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Pull-Requests, CI/CD, Git, Automation, Merge]
    related_skills: [github-auth, github-code-review]
---

# GitHub Pull Request 工作流

管理 PR 生命周期的完整指南。每一节先给出 `gh` 的做法，再给出没有 `gh` 的机器上用的 `git` + `curl` 兜底方案。

## 前置条件

- 已通过 GitHub 鉴权（见 `github-auth` skill）
- 处于一个带有 GitHub 远程仓库的 git 仓库中

### 快速鉴权检测

```bash
# 决定整个工作流用哪种方式
if command -v gh &>/dev/null && gh auth status &>/dev/null; then
  AUTH="gh"
else
  AUTH="git"
  # 确保有用于 API 调用的 token
  if [ -z "$GITHUB_TOKEN" ]; then
    if _hermes_env="${HERMES_HOME:-$HOME/.hermes}/.env"; [ -f "$_hermes_env" ] && grep -q "^GITHUB_TOKEN=" "$_hermes_env"; then
      GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" "$_hermes_env" | head -1 | cut -d= -f2 | tr -d '\n\r')
    elif grep -q "github.com" ~/.git-credentials 2>/dev/null; then
      GITHUB_TOKEN=$(grep "github.com" ~/.git-credentials 2>/dev/null | head -1 | sed 's|https://[^:]*:\([^@]*\)@.*|\1|')
    fi
  fi
fi
echo "Using: $AUTH"
```

### 从 git remote 中提取 Owner/Repo

很多 `curl` 命令需要 `owner/repo`。从 git remote 中提取：

```bash
# 同时适用于 HTTPS 和 SSH 远程 URL
REMOTE_URL=$(git remote get-url origin)
OWNER_REPO=$(echo "$REMOTE_URL" | sed -E 's|.*github\.com[:/]||; s|\.git$||')
OWNER=$(echo "$OWNER_REPO" | cut -d/ -f1)
REPO=$(echo "$OWNER_REPO" | cut -d/ -f2)
echo "Owner: $OWNER, Repo: $REPO"
```

---

## 1. 创建分支

这部分纯粹是 `git` 操作 —— 两种方式都一样：

```bash
# 确保是最新的
git fetch origin
git checkout main && git pull origin main

# 创建并切换到新分支
git checkout -b feat/add-user-authentication
```

分支命名约定：
- `feat/description` —— 新功能
- `fix/description` —— bug 修复
- `refactor/description` —— 代码重构
- `docs/description` —— 文档
- `ci/description` —— CI/CD 改动

## 2. 提交

用 agent 的文件工具（`write_file`、`patch`）做修改，然后提交：

```bash
# 暂存指定文件
git add src/auth.py src/models/user.py tests/test_auth.py

# 用约定式提交信息提交
git commit -m "feat: add JWT-based user authentication

- Add login/register endpoints
- Add User model with password hashing
- Add auth middleware for protected routes
- Add unit tests for auth flow"
```

提交信息格式（约定式提交 / Conventional Commits）：
```
type(scope): short description

Longer explanation if needed. Wrap at 72 characters.
```

类型：`feat`, `fix`, `refactor`, `docs`, `test`, `ci`, `chore`, `perf`

## 3. 推送并创建 PR

### 推送分支（两种方式都一样）

```bash
git push -u origin HEAD
```

### 创建 PR

**用 gh：**

```bash
gh pr create \
  --title "feat: add JWT-based user authentication" \
  --body "## Summary
- Adds login and register API endpoints
- JWT token generation and validation

## Test Plan
- [ ] Unit tests pass

Closes #42"
```

选项：`--draft`, `--reviewer user1,user2`, `--label "enhancement"`, `--base develop`

**用 git + curl：**

```bash
BRANCH=$(git branch --show-current)

curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  https://api.github.com/repos/$OWNER/$REPO/pulls \
  -d "{
    \"title\": \"feat: add JWT-based user authentication\",
    \"body\": \"## Summary\nAdds login and register API endpoints.\n\nCloses #42\",
    \"head\": \"$BRANCH\",
    \"base\": \"main\"
  }"
```

响应 JSON 里包含 PR 的 `number` —— 保存下来供后续命令使用。

要创建为草稿，在 JSON body 里加上 `"draft": true`。

## 4. 监控 CI 状态

### 检查 CI 状态

**用 gh：**

```bash
# 一次性检查
gh pr checks

# 持续观察直到所有检查结束（每 10 秒轮询一次）
gh pr checks --watch
```

**用 git + curl：**

```bash
# 获取当前分支最新提交的 SHA
SHA=$(git rev-parse HEAD)

# 查询合并状态
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/commits/$SHA/status \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f\"Overall: {data['state']}\")
for s in data.get('statuses', []):
    print(f\"  {s['context']}: {s['state']} - {s.get('description', '')}\")"

# 同时检查 GitHub Actions 的 check runs（独立接口）
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/commits/$SHA/check-runs \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
for cr in data.get('check_runs', []):
    print(f\"  {cr['name']}: {cr['status']} / {cr['conclusion'] or 'pending'}\")"
```

### 轮询直到完成（git + curl）

```bash
# 简单的轮询循环 —— 每 30 秒检查一次，最多 10 分钟
SHA=$(git rev-parse HEAD)
for i in $(seq 1 20); do
  STATUS=$(curl -s \
    -H "Authorization: token $GITHUB_TOKEN" \
    https://api.github.com/repos/$OWNER/$REPO/commits/$SHA/status \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['state'])")
  echo "Check $i: $STATUS"
  if [ "$STATUS" = "success" ] || [ "$STATUS" = "failure" ] || [ "$STATUS" = "error" ]; then
    break
  fi
  sleep 30
done
```

## 5. 自动修复 CI 失败

当 CI 失败时，诊断并修复。这个循环对两种鉴权方式都适用。

### 第 1 步：获取失败详情

**用 gh：**

```bash
# 列出本分支最近的工作流运行
gh run list --branch $(git branch --show-current) --limit 5

# 查看失败日志
gh run view <RUN_ID> --log-failed
```

**用 git + curl：**

```bash
BRANCH=$(git branch --show-current)

# 列出本分支的工作流运行
curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  "https://api.github.com/repos/$OWNER/$REPO/actions/runs?branch=$BRANCH&per_page=5" \
  | python3 -c "
import sys, json
runs = json.load(sys.stdin)['workflow_runs']
for r in runs:
    print(f\"Run {r['id']}: {r['name']} - {r['conclusion'] or r['status']}\")"

# 获取失败作业的日志（下载为 zip，解压，读取）
RUN_ID=<run_id>
curl -s -L \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/actions/runs/$RUN_ID/logs \
  -o /tmp/ci-logs.zip
cd /tmp && unzip -o ci-logs.zip -d ci-logs && cat ci-logs/*.txt
```

### 第 2 步：修复并推送

定位到问题后，用文件工具（`patch`、`write_file`）修复：

```bash
git add <fixed_files>
git commit -m "fix: resolve CI failure in <check_name>"
git push
```

### 第 3 步：验证

用第 4 节中的命令重新检查 CI 状态。

### 自动修复循环模式

当被要求自动修复 CI 时，遵循这个循环：

1. 检查 CI 状态 → 找出失败
2. 读失败日志 → 理解错误
3. 用 `read_file` + `patch`/`write_file` → 修复代码
4. `git add . && git commit -m "fix: ..." && git push`
5. 等待 CI → 重新检查状态
6. 如果仍然失败则重复（最多 3 次尝试，然后询问用户）

## 6. 合并

**用 gh：**

```bash
# 压缩合并 + 删除分支（对功能分支最干净）
gh pr merge --squash --delete-branch

# 启用自动合并（所有检查通过后即合并）
gh pr merge --auto --squash --delete-branch
```

**用 git + curl：**

```bash
PR_NUMBER=<number>

# 通过 API 合并 PR（压缩合并）
curl -s -X PUT \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER/merge \
  -d "{
    \"merge_method\": \"squash\",
    \"commit_title\": \"feat: add user authentication (#$PR_NUMBER)\"
  }"

# 合并后删除远程分支
BRANCH=$(git branch --show-current)
git push origin --delete $BRANCH

# 本地切回 main
git checkout main && git pull origin main
git branch -d $BRANCH
```

合并方式：`"merge"`（合并提交）、`"squash"`、`"rebase"`

### 启用自动合并（curl）

```bash
# 自动合并需要在仓库设置里先启用。
# 这里用 GraphQL API，因为 REST 不支持自动合并。
PR_NODE_ID=$(curl -s \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$OWNER/$REPO/pulls/$PR_NUMBER \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['node_id'])")

curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/graphql \
  -d "{\"query\": \"mutation { enablePullRequestAutoMerge(input: {pullRequestId: \\\"$PR_NODE_ID\\\", mergeMethod: SQUASH}) { clientMutationId } }\"}"
```

## 7. 完整工作流示例

```bash
# 1. 从干净的 main 开始
git checkout main && git pull origin main

# 2. 建分支
git checkout -b fix/login-redirect-bug

# 3. （agent 用文件工具做代码改动）

# 4. 提交
git add src/auth/login.py tests/test_login.py
git commit -m "fix: correct redirect URL after login

Preserves the ?next= parameter instead of always redirecting to /dashboard."

# 5. 推送
git push -u origin HEAD

# 6. 创建 PR（按可用工具选用 gh 或 curl）
# ... （见第 3 节）

# 7. 监控 CI（见第 4 节）

# 8. 变绿后合并（见第 6 节）
```

## 常用 PR 命令参考

| 操作 | gh | git + curl |
|--------|-----|-----------|
| 列出我的 PR | `gh pr list --author @me` | `curl -s -H "Authorization: token $GITHUB_TOKEN" "https://api.github.com/repos/$OWNER/$REPO/pulls?state=open"` |
| 查看 PR diff | `gh pr diff` | `git diff main...HEAD`（本地）或 `curl -H "Accept: application/vnd.github.diff" ...` |
| 添加评论 | `gh pr comment N --body "..."` | `curl -X POST .../issues/N/comments -d '{"body":"..."}'` |
| 请求审查 | `gh pr edit N --add-reviewer user` | `curl -X POST .../pulls/N/requested_reviewers -d '{"reviewers":["user"]}'` |
| 关闭 PR | `gh pr close N` | `curl -X PATCH .../pulls/N -d '{"state":"closed"}'` |
| 检出别人的 PR | `gh pr checkout N` | `git fetch origin pull/N/head:pr-N && git checkout pr-N` |
