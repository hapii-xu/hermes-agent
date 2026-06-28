---
name: github-auth
description: "GitHub 认证配置：HTTPS 令牌、SSH 密钥、gh CLI 登录。"
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Authentication, Git, gh-cli, SSH, Setup]
    related_skills: [github-pr-workflow, github-code-review, github-issues, github-repo-management]
---

# GitHub 认证配置

本技能用于配置认证，使智能体能与 GitHub 仓库、PR、issue 和 CI 协作。它涵盖两条路径：

- **`git`（始终可用）** —— 使用 HTTPS 个人访问令牌或 SSH 密钥
- **`gh` CLI（若已安装）** —— 提供更丰富的 GitHub API 访问，且认证流程更简单

## 检测流程

当用户要求你与 GitHub 协作时，先运行此检查：

```bash
# 检查可用工具
git --version
gh --version 2>/dev/null || echo "gh not installed"

# 检查是否已认证
gh auth status 2>/dev/null || echo "gh not authenticated"
git config --global credential.helper 2>/dev/null || echo "no git credential helper"
```

**决策树：**
1. 如果 `gh auth status` 显示已认证 → 一切就绪，所有操作都用 `gh`
2. 如果已安装 `gh` 但未认证 → 使用下方的 "gh auth" 方法
3. 如果未安装 `gh` → 使用下方的 "仅 git" 方法（无需 sudo）

---

## 方法 1：仅使用 git 认证（无 gh、无 sudo）

此方法在任何装有 `git` 的机器上都可用，无需 root 权限。

### 选项 A：使用个人访问令牌的 HTTPS（推荐）

这是最通用的方法 —— 处处可用，无需 SSH 配置。

**第 1 步：创建个人访问令牌**

让用户前往：**https://github.com/settings/tokens**

- 点击 "Generate new token (classic)"
- 给它起个名字，例如 "hermes-agent"
- 选择权限范围（scopes）：
  - `repo`（完整的仓库访问 —— 读、写、push、PR）
  - `workflow`（触发并管理 GitHub Actions）
  - `read:org`（如果涉及组织仓库）
- 设置过期时间（90 天是一个不错的默认值）
- 复制令牌 —— 它不会再显示第二次

**第 2 步：配置 git 存储令牌**

```bash
# 配置 credential helper 以缓存凭据
# "store" 会以明文保存到 ~/.git-credentials（简单且持久）
git config --global credential.helper store

# 现在执行一次会触发认证的测试操作 —— git 会提示输入凭据
# 用户名：<their-github-username>
# 密码：<粘贴个人访问令牌，而不是他们的 GitHub 登录密码>
git ls-remote https://github.com/<their-username>/<any-repo>.git
```

输入一次凭据后，它们会被保存并在未来所有操作中复用。

**替代方案：cache helper（凭据在内存中过期）**

```bash
# 在内存中缓存 8 小时（28800 秒），而不写入磁盘
git config --global credential.helper 'cache --timeout=28800'
```

**替代方案：将令牌直接写入 remote URL（按仓库设置）**

```bash
# 将令牌嵌入 remote URL（彻底避免凭据提示）
git remote set-url origin https://<username>:<token>@github.com/<owner>/<repo>.git
```

**第 3 步：配置 git 身份**

```bash
# 提交所必需 —— 设置姓名和邮箱
git config --global user.name "Their Name"
git config --global user.email "their-email@example.com"
```

**第 4 步：验证**

```bash
# 测试 push 权限（现在应该无需任何提示即可成功）
git ls-remote https://github.com/<their-username>/<any-repo>.git

# 验证身份
git config --global user.name
git config --global user.email
```

### 选项 B：SSH 密钥认证

适合偏好 SSH 或已有密钥的用户。

**第 1 步：检查现有 SSH 密钥**

```bash
ls -la ~/.ssh/id_*.pub 2>/dev/null || echo "No SSH keys found"
```

**第 2 步：如有需要则生成密钥**

```bash
# 生成 ed25519 密钥（现代、安全、快速）
ssh-keygen -t ed25519 -C "their-email@example.com" -f ~/.ssh/id_ed25519 -N ""

# 显示公钥，以便他们添加到 GitHub
cat ~/.ssh/id_ed25519.pub
```

让用户将公钥添加到：**https://github.com/settings/keys**
- 点击 "New SSH key"
- 粘贴公钥内容
- 给它起个标题，例如 "hermes-agent-<machine-name>"

**第 3 步：测试连接**

```bash
ssh -T git@github.com
# 预期输出："Hi <username>! You've successfully authenticated..."
```

**第 4 步：配置 git 对 GitHub 使用 SSH**

```bash
# 自动把 HTTPS 的 GitHub URL 改写为 SSH
git config --global url."git@github.com:".insteadOf "https://github.com/"
```

**第 5 步：配置 git 身份**

```bash
git config --global user.name "Their Name"
git config --global user.email "their-email@example.com"
```

---

## 方法 2：gh CLI 认证

如果已安装 `gh`，它能在一步之内同时处理 API 访问和 git 凭据。

### 交互式浏览器登录（桌面环境）

```bash
gh auth login
# 选择：GitHub.com
# 选择：HTTPS
# 通过浏览器认证
```

### 基于令牌的登录（无界面 / SSH 服务器）

```bash
echo "<THEIR_TOKEN>" | gh auth login --with-token

# 通过 gh 配置 git 凭据
gh auth setup-git
```

### 验证

```bash
gh auth status
```

---

## 在没有 gh 的情况下使用 GitHub API

当 `gh` 不可用时，你仍可使用 `curl` 加个人访问令牌访问完整的 GitHub API。其他 GitHub 技能的兜底实现就是这样做的。

### 为 API 调用设置令牌

```bash
# 选项 1：导出为环境变量（推荐 —— 避免出现在命令行中）
export GITHUB_TOKEN="<token>"

# 然后在 curl 调用中使用：
curl -s -H "Authorization: token $GITHUB_TOKEN" \
  https://api.github.com/user
```

### 从 git 凭据中提取令牌

如果已通过 credential.helper store 配置过 git 凭据，则可提取令牌：

```bash
# 从 git credential store 中读取
grep "github.com" ~/.git-credentials 2>/dev/null | head -1 | sed 's|https://[^:]*:\([^@]*\)@.*|\1|'
```

### 辅助函数：检测认证方式

在任何 GitHub 工作流的开头使用此模式：

```bash
# 先尝试 gh，再回退到 git + curl
if command -v gh &>/dev/null && gh auth status &>/dev/null; then
  echo "AUTH_METHOD=gh"
elif [ -n "$GITHUB_TOKEN" ]; then
  echo "AUTH_METHOD=curl"
elif _hermes_env="${HERMES_HOME:-$HOME/.hermes}/.env"; [ -f "$_hermes_env" ] && grep -q "^GITHUB_TOKEN=" "$_hermes_env"; then
  export GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" "$_hermes_env" | head -1 | cut -d= -f2 | tr -d '\n\r')
  echo "AUTH_METHOD=curl"
elif grep -q "github.com" ~/.git-credentials 2>/dev/null; then
  export GITHUB_TOKEN=$(grep "github.com" ~/.git-credentials | head -1 | sed 's|https://[^:]*:\([^@]*\)@.*|\1|')
  echo "AUTH_METHOD=curl"
else
  echo "AUTH_METHOD=none"
  echo "Need to set up authentication first"
fi
```

---

## 故障排查

| 问题 | 解决方案 |
|---------|----------|
| `git push` 要求输入密码 | GitHub 已禁用密码认证。请用个人访问令牌作为密码，或改用 SSH |
| `remote: Permission to X denied` | 令牌可能缺少 `repo` 权限范围 —— 用正确的范围重新生成 |
| `fatal: Authentication failed` | 缓存的凭据可能已失效 —— 运行 `git credential reject` 后重新认证 |
| `ssh: connect to host github.com port 22: Connection refused` | 尝试通过 HTTPS 端口走 SSH：在 `~/.ssh/config` 中为 `Host github.com` 添加 `Port 443` 和 `Hostname ssh.github.com` |
| 凭据不持久 | 检查 `git config --global credential.helper` —— 必须为 `store` 或 `cache` |
| 多个 GitHub 账号 | 在 `~/.ssh/config` 中为不同的 host 别名使用不同的 SSH 密钥，或使用按仓库区分的凭据 URL |
| `gh: command not found` 且无 sudo | 使用上方的仅 git 方法 1 —— 无需安装 |
