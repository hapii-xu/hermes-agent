---
name: opencode
description: "将编码任务委托给 OpenCode CLI（功能开发、PR 审查）。"
version: 1.2.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Coding-Agent, OpenCode, Autonomous, Refactoring, Code-Review]
    related_skills: [claude-code, codex, hermes-agent]
---

# OpenCode CLI

将 [OpenCode](https://opencode.ai) 作为自主编码工作器使用，由 Hermes 终端/进程工具编排。OpenCode 是一个与提供商无关的开源 AI 编码智能体，带有 TUI 和 CLI。

## 何时使用

- 用户明确要求使用 OpenCode
- 你想要一个外部编码智能体来实现/重构/审查代码
- 你需要长时间运行的编码会话并定期检查进度
- 你希望在相互隔离的工作目录（workdir）/工作树（worktree）中并行执行任务

## 前置条件

- 已安装 OpenCode：`npm i -g opencode-ai@latest` 或 `brew install anomalyco/tap/opencode`
- 已配置认证：`opencode auth login`，或设置提供商环境变量（OPENROUTER_API_KEY 等）
- 验证：`opencode auth list` 应至少显示一个提供商
- 用于代码任务的 Git 仓库（推荐）
- 交互式 TUI 会话需设置 `pty=true`

## 二进制文件解析（重要）

不同 Shell 环境可能解析到不同的 OpenCode 二进制文件。如果你的终端与 Hermes 之间行为不一致，请检查：

```
terminal(command="which -a opencode")
terminal(command="opencode --version")
```

如有需要，显式指定二进制路径：

```
terminal(command="$HOME/.opencode/bin/opencode run '...'", workdir="~/project", pty=true)
```

## 一次性任务（One-Shot Tasks）

对于有边界、非交互式的任务，使用 `opencode run`：

```
terminal(command="opencode run 'Add retry logic to API calls and update tests'", workdir="~/project")
```

用 `-f` 附加上下文文件：

```
terminal(command="opencode run 'Review this config for security issues' -f config.yaml -f .env.example", workdir="~/project")
```

用 `--thinking` 显示模型思考过程：

```
terminal(command="opencode run 'Debug why tests fail in CI' --thinking", workdir="~/project")
```

强制使用指定模型：

```
terminal(command="opencode run 'Refactor auth module' --model openrouter/anthropic/claude-sonnet-4", workdir="~/project")
```

## 交互式会话（后台）

对于需要多次交互的迭代式工作，可在后台启动 TUI：

```
terminal(command="opencode", workdir="~/project", background=true, pty=true)
# 返回 session_id

# 发送提示词
process(action="submit", session_id="<id>", data="Implement OAuth refresh flow and add tests")

# 监控进度
process(action="poll", session_id="<id>")
process(action="log", session_id="<id>")

# 发送后续输入
process(action="submit", session_id="<id>", data="Now add error handling for token expiry")

# 干净退出 —— Ctrl+C
process(action="write", session_id="<id>", data="\x03")
# 或者直接结束进程
process(action="kill", session_id="<id>")
```

**重要：** 不要使用 `/exit` —— 它不是有效的 OpenCode 命令，反而会打开一个智能体选择对话框。请使用 Ctrl+C（`\x03`）或 `process(action="kill")` 来退出。

### TUI 快捷键

| 按键 | 动作 |
|-----|--------|
| `Enter` | 提交消息（如有需要按两次） |
| `Tab` | 在智能体之间切换（build/plan） |
| `Ctrl+P` | 打开命令面板 |
| `Ctrl+X L` | 切换会话 |
| `Ctrl+X M` | 切换模型 |
| `Ctrl+X N` | 新建会话 |
| `Ctrl+X E` | 打开编辑器 |
| `Ctrl+C` | 退出 OpenCode |

### 恢复会话

退出后，OpenCode 会打印一个会话 ID。可按以下方式恢复：

```
terminal(command="opencode -c", workdir="~/project", background=true, pty=true)  # 继续上一个会话
terminal(command="opencode -s ses_abc123", workdir="~/project", background=true, pty=true)  # 指定会话
```

## 常用参数

| 参数 | 用途 |
|------|-----|
| `run 'prompt'` | 一次性执行后退出 |
| `--continue` / `-c` | 继续上一个 OpenCode 会话 |
| `--session <id>` / `-s` | 继续指定会话 |
| `--agent <name>` | 选择 OpenCode 智能体（build 或 plan） |
| `--model provider/model` | 强制使用指定模型 |
| `--format json` | 机器可读的输出/事件 |
| `--file <path>` / `-f` | 为消息附加文件 |
| `--thinking` | 显示模型思考块 |
| `--variant <level>` | 推理强度（high、max、minimal） |
| `--title <name>` | 为会话命名 |
| `--attach <url>` | 连接到一个正在运行的 opencode 服务 |

## 流程

1. 验证工具就绪：
   - `terminal(command="opencode --version")`
   - `terminal(command="opencode auth list")`
2. 对于有边界的任务，使用 `opencode run '...'`（无需 pty）。
3. 对于迭代式任务，使用 `background=true, pty=true` 启动 `opencode`。
4. 使用 `process(action="poll"|"log")` 监控长任务。
5. 如果 OpenCode 请求输入，通过 `process(action="submit", ...)` 响应。
6. 使用 `process(action="write", data="\x03")` 或 `process(action="kill")` 退出。
7. 向用户总结文件改动、测试结果及后续步骤。

## PR 审查工作流

OpenCode 内置了 PR 命令：

```
terminal(command="opencode pr 42", workdir="~/project", pty=true)
```

或在一个临时克隆中审查以实现隔离：

```
terminal(command="REVIEW=$(mktemp -d) && git clone https://github.com/user/repo.git $REVIEW && cd $REVIEW && opencode run 'Review this PR vs main. Report bugs, security risks, test gaps, and style issues.' -f $(git diff origin/main --name-only | head -20 | tr '\n' ' ')", pty=true)
```

## 并行工作模式

使用相互独立的工作目录/工作树以避免冲突：

```
terminal(command="opencode run 'Fix issue #101 and commit'", workdir="/tmp/issue-101", background=true, pty=true)
terminal(command="opencode run 'Add parser regression tests and commit'", workdir="/tmp/issue-102", background=true, pty=true)
process(action="list")
```

## 会话与成本管理

列出过往会话：

```
terminal(command="opencode session list")
```

查看 token 用量与成本：

```
terminal(command="opencode stats")
terminal(command="opencode stats --days 7 --models anthropic/claude-sonnet-4")
```

## 常见陷阱

- 交互式 `opencode`（TUI）会话需要 `pty=true`。`opencode run` 命令则不需要 pty。
- `/exit` 不是有效命令 —— 它会打开智能体选择器。请使用 Ctrl+C 退出 TUI。
- PATH 不匹配可能选错 OpenCode 二进制文件/模型配置。
- 如果 OpenCode 看似卡住，先检查日志再结束进程：
  - `process(action="log", session_id="<id>")`
- 避免在并行的多个 OpenCode 会话之间共用同一个工作目录。
- 在 TUI 中可能需要按两次 Enter 才能提交（第一次定稿文本，第二次发送）。

## 验证

冒烟测试：

```
terminal(command="opencode run 'Respond with exactly: OPENCODE_SMOKE_OK'")
```

成功标准：
- 输出包含 `OPENCODE_SMOKE_OK`
- 命令退出时没有提供商/模型错误
- 对于代码任务：预期文件已改动且测试通过

## 规则

1. 一次性自动化优先用 `opencode run` —— 更简单，且不需要 pty。
2. 仅当需要迭代时才使用交互式后台模式。
3. 始终将 OpenCode 会话限定在单个仓库/工作目录内。
4. 对于长任务，根据 `process` 日志提供进度更新。
5. 报告具体结果（改动文件、测试、剩余风险）。
6. 用 Ctrl+C 或 kill 退出交互式会话，绝不用 `/exit`。
