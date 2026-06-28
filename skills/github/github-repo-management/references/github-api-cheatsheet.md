# GitHub REST API 速查表

Base URL：`https://api.github.com`

所有请求都需要：`-H "Authorization: token $GITHUB_TOKEN"`

使用 `gh-env.sh` 助手脚本可自动设置 `$GITHUB_TOKEN`、`$GH_OWNER`、`$GH_REPO`：
```bash
source "${HERMES_HOME:-$HOME/.hermes}/skills/github/github-auth/scripts/gh-env.sh"
```

## 仓库

| 操作 | 方法 | 端点 |
|--------|--------|----------|
| 获取仓库信息 | GET | `/repos/{owner}/{repo}` |
| 创建仓库（用户） | POST | `/user/repos` |
| 创建仓库（组织） | POST | `/orgs/{org}/repos` |
| 更新仓库 | PATCH | `/repos/{owner}/{repo}` |
| 删除仓库 | DELETE | `/repos/{owner}/{repo}` |
| 列出你的仓库 | GET | `/user/repos?per_page=30&sort=updated` |
| 列出组织仓库 | GET | `/orgs/{org}/repos` |
| Fork 仓库 | POST | `/repos/{owner}/{repo}/forks` |
| 从模板创建 | POST | `/repos/{owner}/{template}/generate` |
| 获取主题 | GET | `/repos/{owner}/{repo}/topics` |
| 设置主题 | PUT | `/repos/{owner}/{repo}/topics` |

## Pull Request

| 操作 | 方法 | 端点 |
|--------|--------|----------|
| 列出 PR | GET | `/repos/{owner}/{repo}/pulls?state=open` |
| 创建 PR | POST | `/repos/{owner}/{repo}/pulls` |
| 获取 PR | GET | `/repos/{owner}/{repo}/pulls/{number}` |
| 更新 PR | PATCH | `/repos/{owner}/{repo}/pulls/{number}` |
| 列出 PR 文件 | GET | `/repos/{owner}/{repo}/pulls/{number}/files` |
| 合并 PR | PUT | `/repos/{owner}/{repo}/pulls/{number}/merge` |
| 请求审查者 | POST | `/repos/{owner}/{repo}/pulls/{number}/requested_reviewers` |
| 创建审查 | POST | `/repos/{owner}/{repo}/pulls/{number}/reviews` |
| 行内评论 | POST | `/repos/{owner}/{repo}/pulls/{number}/comments` |

### PR 合并请求体

```json
{"merge_method": "squash", "commit_title": "feat: description (#N)"}
```

合并方式：`"merge"`、`"squash"`、`"rebase"`

### PR 审查事件

`"APPROVE"`、`"REQUEST_CHANGES"`、`"COMMENT"`

## Issue

| 操作 | 方法 | 端点 |
|--------|--------|----------|
| 列出 issue | GET | `/repos/{owner}/{repo}/issues?state=open` |
| 创建 issue | POST | `/repos/{owner}/{repo}/issues` |
| 获取 issue | GET | `/repos/{owner}/{repo}/issues/{number}` |
| 更新 issue | PATCH | `/repos/{owner}/{repo}/issues/{number}` |
| 添加评论 | POST | `/repos/{owner}/{repo}/issues/{number}/comments` |
| 添加标签 | POST | `/repos/{owner}/{repo}/issues/{number}/labels` |
| 移除标签 | DELETE | `/repos/{owner}/{repo}/issues/{number}/labels/{name}` |
| 添加指派人 | POST | `/repos/{owner}/{repo}/issues/{number}/assignees` |
| 列出标签 | GET | `/repos/{owner}/{repo}/labels` |
| 搜索 issue | GET | `/search/issues?q={query}+repo:{owner}/{repo}` |

注意：Issues API 也会返回 PR。解析时用 `"pull_request" not in item` 进行过滤。

## CI / GitHub Actions

| 操作 | 方法 | 端点 |
|--------|--------|----------|
| 列出工作流 | GET | `/repos/{owner}/{repo}/actions/workflows` |
| 列出运行 | GET | `/repos/{owner}/{repo}/actions/runs?per_page=10` |
| 列出运行（按分支） | GET | `/repos/{owner}/{repo}/actions/runs?branch={branch}` |
| 获取运行 | GET | `/repos/{owner}/{repo}/actions/runs/{run_id}` |
| 下载日志 | GET | `/repos/{owner}/{repo}/actions/runs/{run_id}/logs` |
| 重新运行 | POST | `/repos/{owner}/{repo}/actions/runs/{run_id}/rerun` |
| 重跑失败项 | POST | `/repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs` |
| 触发 dispatch | POST | `/repos/{owner}/{repo}/actions/workflows/{id}/dispatches` |
| 提交状态 | GET | `/repos/{owner}/{repo}/commits/{sha}/status` |
| Check run | GET | `/repos/{owner}/{repo}/commits/{sha}/check-runs` |

## Release

| 操作 | 方法 | 端点 |
|--------|--------|----------|
| 列出 release | GET | `/repos/{owner}/{repo}/releases` |
| 创建 release | POST | `/repos/{owner}/{repo}/releases` |
| 获取 release | GET | `/repos/{owner}/{repo}/releases/{id}` |
| 删除 release | DELETE | `/repos/{owner}/{repo}/releases/{id}` |
| 上传资产 | POST | `https://uploads.github.com/repos/{owner}/{repo}/releases/{id}/assets?name={filename}` |

## 密钥

| 操作 | 方法 | 端点 |
|--------|--------|----------|
| 列出密钥 | GET | `/repos/{owner}/{repo}/actions/secrets` |
| 获取公钥 | GET | `/repos/{owner}/{repo}/actions/secrets/public-key` |
| 设置密钥 | PUT | `/repos/{owner}/{repo}/actions/secrets/{name}` |
| 删除密钥 | DELETE | `/repos/{owner}/{repo}/actions/secrets/{name}` |

## 分支保护

| 操作 | 方法 | 端点 |
|--------|--------|----------|
| 获取保护规则 | GET | `/repos/{owner}/{repo}/branches/{branch}/protection` |
| 设置保护规则 | PUT | `/repos/{owner}/{repo}/branches/{branch}/protection` |
| 删除保护规则 | DELETE | `/repos/{owner}/{repo}/branches/{branch}/protection` |

## 用户 / 认证

| 操作 | 方法 | 端点 |
|--------|--------|----------|
| 获取当前用户 | GET | `/user` |
| 列出用户仓库 | GET | `/user/repos` |
| 列出用户 gist | GET | `/gists` |
| 创建 gist | POST | `/gists` |
| 搜索仓库 | GET | `/search/repositories?q={query}` |

## 分页

大多数列表端点支持：
- `?per_page=100`（最大 100）
- `?page=2` 获取下一页
- 检查 `Link` 头中的 `rel="next"` URL

## 速率限制

- 已认证：5,000 次请求/小时
- 查看剩余额度：`curl -s -H "Authorization: token $GITHUB_TOKEN" https://api.github.com/rate_limit`

## 常用 curl 模式

```bash
# GET
curl -s -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$GH_OWNER/$GH_REPO

# 带 JSON 请求体的 POST
curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$GH_OWNER/$GH_REPO/issues \
  -d '{"title": "...", "body": "..."}'

# PATCH（更新）
curl -s -X PATCH \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$GH_OWNER/$GH_REPO/issues/42 \
  -d '{"state": "closed"}'

# DELETE
curl -s -X DELETE \
  -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/repos/$GH_OWNER/$GH_REPO/issues/42/labels/bug

# 用 python3 解析 JSON 响应
curl -s ... | python3 -c "import sys,json; data=json.load(sys.stdin); print(data['field'])"
```
