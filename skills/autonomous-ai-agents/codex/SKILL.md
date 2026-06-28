---
name: codex
description: "将编码任务委派给 OpenAI Codex CLI（功能开发、PR）。"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Coding-Agent, Codex, OpenAI, Code-Review, Refactoring]
    related_skills: [claude-code, hermes-agent]
---

# Codex CLI

通过 Hermes 终端将编码任务委派给 [Codex](https://github.com/openai/codex)。Codex 是 OpenAI 的自主编码 agent CLI。

## 何时使用

- 构建新功能
- 重构
- PR 审查
- 批量修复 issue

需要 codex CLI 和一个 git 仓库。

## 前置条件

- 已安装 Codex：`npm install -g @openai/codex`
- 已配置 OpenAI 认证：`OPENAI_API_KEY`，或来自 Codex CLI 登录流程的
  OAuth 凭证
- **必须在 git 仓库内运行** —— Codex 在 git 仓库之外会拒绝运行
- 在终端调用中使用 `pty=true` —— Codex 是一个交互式终端应用

对于 Hermes 本身，`model.provider: openai-codex` 使用由 Hermes 管理的 Codex
OAuth（在执行 `hermes auth add openai-codex` 后位于
`~/.hermes/auth.json`）。对于独立的 Codex CLI，一个有效的 CLI OAuth 会话可能
位于 `~/.codex/auth.json`；不要仅凭缺少 `OPENAI_API_KEY` 就断定 Codex 认证
缺失。

## 一次性任务

```
terminal(command="codex exec 'Add dark mode toggle to settings'", workdir="~/project", pty=true)
```

用于临时实验（Codex 仍需要一个 git 仓库）：
```
terminal(command="cd $(mktemp -d) && git init && codex exec 'Build a snake game in Python'", pty=true)
```

## 后台模式（长任务）

```
# 用 PTY 在后台启动
terminal(command="codex exec --full-auto 'Refactor the auth module'", workdir="~/project", background=true, pty=true)
# 返回 session_id

# 监控进度
process(action="poll", session_id="<id>")
process(action="log", session_id="<id>")

# 当 Codex 提问时发送输入
process(action="submit", session_id="<id>", data="yes")

# 需要时终止
process(action="kill", session_id="<id>")
```

## 关键标志

| 标志 | 效果 |
|------|--------|
| `exec "prompt"` | 一次性执行，完成后退出 |
| `--full-auto` | 沙箱内运行，但自动批准工作区内的文件改动 |
| `--yolo` | 无沙箱、无审批（最快，也最危险） |
| `--sandbox danger-full-access` | 不使用 Codex 沙箱；在宿主服务上下文破坏 bubblewrap 时很有用 |

## Hermes Gateway 注意事项

当从 Hermes gateway/服务上下文（例如由 Telegram 驱动的 agent 会话）中调用 Codex CLI 时，Codex 的 `workspace-write` 沙箱可能会失败，即使同一条命令在用户的交互式 shell 中可以正常工作。典型症状是 bubblewrap/user-namespace 相关错误，例如 `setting up uid map: Permission denied`
或 `loopback: Failed RTM_NEWADDR: Operation not permitted`。

在这种上下文中，优先使用：

```
codex exec --sandbox danger-full-access "<task>"
```

改用进程边界作为安全层：明确的 `workdir`、启动前干净的 git
状态、范围狭窄的任务提示、`git diff` 审查、有针对性的测试，以及在提交大范围改动前由人工/agent 确认。

## PR 审查

克隆到临时目录以便安全地审查：

```
terminal(command="REVIEW=$(mktemp -d) && git clone https://github.com/user/repo.git $REVIEW && cd $REVIEW && gh pr checkout 42 && codex review --base origin/main", pty=true)
```

## 用 worktree 并行修复 issue

```
# 创建 worktree
terminal(command="git worktree add -b fix/issue-78 /tmp/issue-78 main", workdir="~/project")
terminal(command="git worktree add -b fix/issue-99 /tmp/issue-99 main", workdir="~/project")

# 在每个 worktree 中启动 Codex
terminal(command="codex --yolo exec 'Fix issue #78: <description>. Commit when done.'", workdir="/tmp/issue-78", background=true, pty=true)
terminal(command="codex --yolo exec 'Fix issue #99: <description>. Commit when done.'", workdir="/tmp/issue-99", background=true, pty=true)

# 监控
process(action="list")

# 完成后推送并创建 PR
terminal(command="cd /tmp/issue-78 && git push -u origin fix/issue-78")
terminal(command="gh pr create --repo user/repo --head fix/issue-78 --title 'fix: ...' --body '...'")

# 清理
terminal(command="git worktree remove /tmp/issue-78", workdir="~/project")
```

## 批量 PR 审查

```
# 拉取所有 PR 引用
terminal(command="git fetch origin '+refs/pull/*/head:refs/remotes/origin/pr/*'", workdir="~/project")

# 并行审查多个 PR
terminal(command="codex exec 'Review PR #86. git diff origin/main...origin/pr/86'", workdir="~/project", background=true, pty=true)
terminal(command="codex exec 'Review PR #87. git diff origin/main...origin/pr/87'", workdir="~/project", background=true, pty=true)

# 发布结果
terminal(command="gh pr comment 86 --body '<review>'", workdir="~/project")
```

## 规则

1. **始终使用 `pty=true`** —— Codex 是交互式终端应用，没有 PTY 会卡住
2. **必须要有 git 仓库** —— Codex 在 git 目录之外无法运行。临时实验用 `mktemp -d && git init`
3. **一次性任务用 `exec`** —— `codex exec "prompt"` 运行后会干净退出
4. **构建时用 `--full-auto`** —— 在沙箱内自动批准改动
5. **长任务用后台模式** —— 使用 `background=true` 并通过 `process` 工具监控
6. **不要干扰** —— 用 `poll`/`log` 监控，对长时间运行的任务保持耐心
7. **可以并行** —— 可同时运行多个 Codex 进程来批量处理
