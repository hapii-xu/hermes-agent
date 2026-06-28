---
name: himalaya
description: "Himalaya CLI：从终端使用 IMAP/SMTP 收发邮件。"
version: 1.1.0
author: community
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Email, IMAP, SMTP, CLI, Communication]
    homepage: https://github.com/pimalaya/himalaya
prerequisites:
  commands: [himalaya]
---

# Himalaya 邮件 CLI

Himalaya 是一个命令行邮件客户端，让你能够通过 IMAP、SMTP、Notmuch 或 Sendmail 后端从终端管理邮件。

本技能与 Hermes Email 网关适配器相互独立。网关适配器用于让人们给 agent
发邮件，并使用 Hermes 内置的 IMAP/SMTP 适配器；而本技能则让
agent 通过终端工具操作邮箱，并需要外部的 `himalaya` CLI。

## 参考资料

- `references/configuration.md`（配置文件设置 + IMAP/SMTP 认证）
- `references/message-composition.md`（用于撰写邮件的 MML 语法）

## 前置条件

1. 已安装 Himalaya CLI（用 `himalaya --version` 验证）
2. 在 `~/.config/himalaya/config.toml` 存在配置文件
3. 已配置 IMAP/SMTP 凭据（密码需安全存储）

### 安装

```bash
# 预编译二进制（Linux/macOS —— 推荐）
curl -sSL https://raw.githubusercontent.com/pimalaya/himalaya/master/install.sh | PREFIX=~/.local sh

# macOS 通过 Homebrew
brew install himalaya

# 或通过 cargo（任何装有 Rust 的平台）
cargo install himalaya --locked
```

## 配置设置

运行交互式向导来设置账户：

```bash
himalaya account configure
```

或手动创建 `~/.config/himalaya/config.toml`：

```toml
[accounts.personal]
email = "you@example.com"
display-name = "Your Name"
default = true

backend.type = "imap"
backend.host = "imap.example.com"
backend.port = 993
backend.encryption.type = "tls"
backend.login = "you@example.com"
backend.auth.type = "password"
backend.auth.cmd = "pass show email/imap"  # 或使用 keyring

message.send.backend.type = "smtp"
message.send.backend.host = "smtp.example.com"
message.send.backend.port = 587
message.send.backend.encryption.type = "start-tls"
message.send.backend.login = "you@example.com"
message.send.backend.auth.type = "password"
message.send.backend.auth.cmd = "pass show email/smtp"

# 文件夹别名（himalaya v1.2.0+ 语法）。当服务器的文件夹名
# 与 himalaya 的规范名（inbox/sent/drafts/trash）不一致时必填。
# Gmail 是最常见的情况——参见 `references/configuration.md`
# 中关于 `[Gmail]/Sent Mail` 映射的部分。
folder.aliases.inbox = "INBOX"
folder.aliases.sent = "Sent"
folder.aliases.drafts = "Drafts"
folder.aliases.trash = "Trash"
```

> **关于别名语法的提醒。** v1.2.0 之前的文档使用的是
> `[accounts.NAME.folder.alias]` 子段（单数 `alias`）。
> v1.2.0 会静默忽略这种写法——TOML 能正常解析，但
> 别名解析器读不到它，于是每次查找都会回落到
> 规范名。在 Gmail 上，这意味着「保存到已发送」会在
> SMTP 投递成功*之后*失败，`himalaya message send` 以非零状态退出。
> 任何在该退出码上重试的调用方（agent、脚本、用户）
> 都会重新执行整个发送流程——包括 SMTP——从而给收件人
> 发出重复邮件。请始终使用 `folder.aliases.X`（复数、
> 点分键，直接写在 `[accounts.NAME]` 下）。

## Hermes 集成说明

- **读取、列出、搜索、移动、删除** 全部可直接通过终端工具完成
- **撰写/回复/转发** —— 推荐使用管道输入（`cat << EOF | himalaya template send`）以获得可靠性。交互式 `$EDITOR` 模式可在 `pty=true` + 后台 + 进程工具下工作，但需要了解编辑器及其命令
- 使用 `--output json` 获取更易于程序化解析的结构化输出
- `himalaya account configure` 向导需要交互式输入——请使用 PTY 模式：`terminal(command="himalaya account configure", pty=true)`

## 常见操作

### 列出文件夹

```bash
himalaya folder list
```

### 列出邮件

列出 INBOX（默认）中的邮件：

```bash
himalaya envelope list
```

列出特定文件夹中的邮件：

```bash
himalaya envelope list --folder "Sent"
```

分页列出：

```bash
himalaya envelope list --page 1 --page-size 20
```

### 搜索邮件

```bash
himalaya envelope list from john@example.com subject meeting
```

### 读取邮件

按 ID 读取邮件（显示纯文本）：

```bash
himalaya message read 42
```

导出原始 MIME：

```bash
himalaya message export 42 --full
```

### 回复邮件

要从 Hermes 非交互地回复，先读取原邮件，撰写回复，再通过管道发送：

```bash
# 获取回复模板，编辑后发送
himalaya template reply 42 | sed 's/^$/\nYour reply text here\n/' | himalaya template send
```

或手动构建回复：

```bash
cat << 'EOF' | himalaya template send
From: you@example.com
To: sender@example.com
Subject: Re: Original Subject
In-Reply-To: <original-message-id>

Your reply here.
EOF
```

回复全部（交互式——需要 $EDITOR，建议改用上面的 template 方式）：

```bash
himalaya message reply 42 --all
```

### 转发邮件

```bash
# 获取转发模板并通过管道带上修改发送
himalaya template forward 42 | sed 's/^To:.*/To: newrecipient@example.com/' | himalaya template send
```

### 写新邮件

**非交互式（在 Hermes 中使用此方式）** —— 通过 stdin 管道传入邮件：

```bash
cat << 'EOF' | himalaya template send
From: you@example.com
To: recipient@example.com
Subject: Test Message

Hello from Himalaya!
EOF
```

或使用 headers 标志：

```bash
himalaya message write -H "To:recipient@example.com" -H "Subject:Test" "Message body here"
```

注意：不带管道输入的 `himalaya message write` 会打开 `$EDITOR`。这在 `pty=true` + 后台模式下可用，但管道方式更简单也更可靠。

### 移动/复制邮件

移动到文件夹（目标文件夹在前，然后是邮件 ID）：

```bash
himalaya message move "Archive" 42
```

复制到文件夹（目标文件夹在前，然后是邮件 ID）：

```bash
himalaya message copy "Important" 42
```

### 删除邮件

```bash
himalaya message delete 42
```

### 管理标志

添加标志：

```bash
himalaya flag add 42 --flag seen
```

移除标志：

```bash
himalaya flag remove 42 --flag seen
```

## 多账户

列出账户：

```bash
himalaya account list
```

使用特定账户：

```bash
himalaya --account work envelope list
```

## 附件

从邮件中保存附件：

```bash
himalaya attachment download 42
```

保存到指定目录：

```bash
himalaya attachment download 42 --downloads-dir ~/Downloads
```

## 输出格式

大多数命令支持 `--output` 用于结构化输出：

```bash
himalaya envelope list --output json
himalaya envelope list --output plain
```

## 调试

启用调试日志：

```bash
RUST_LOG=debug himalaya envelope list
```

带回溯的完整 trace：

```bash
RUST_LOG=trace RUST_BACKTRACE=1 himalaya envelope list
```

## 提示

- 使用 `himalaya --help` 或 `himalaya <command> --help` 查看详细用法。
- 邮件 ID 是相对于当前文件夹的；切换文件夹后请重新列出。
- 如需撰写带附件的富文本邮件，请使用 MML 语法（见 `references/message-composition.md`）。
- 使用 `pass`、系统 keyring 或任何能输出密码的命令来安全地存储密码。
