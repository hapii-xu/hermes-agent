# Himalaya 配置参考

配置文件位置：`~/.config/himalaya/config.toml`

## 最小 IMAP + SMTP 配置

```toml
[accounts.default]
email = "user@example.com"
display-name = "Your Name"
default = true

# 用于读取邮件的 IMAP 后端
backend.type = "imap"
backend.host = "imap.example.com"
backend.port = 993
backend.encryption.type = "tls"
backend.login = "user@example.com"
backend.auth.type = "password"
backend.auth.raw = "your-password"

# 用于发送邮件的 SMTP 后端
message.send.backend.type = "smtp"
message.send.backend.host = "smtp.example.com"
message.send.backend.port = 587
message.send.backend.encryption.type = "start-tls"
message.send.backend.login = "user@example.com"
message.send.backend.auth.type = "password"
message.send.backend.auth.raw = "your-password"

# 文件夹别名——当服务器文件夹名与 himalaya 的规范名不同时必填。
# 参见下文的「文件夹别名」。
folder.aliases.inbox = "INBOX"
folder.aliases.sent = "Sent"
folder.aliases.drafts = "Drafts"
folder.aliases.trash = "Trash"
```

## 密码选项

### 明文密码（仅用于测试，不推荐）

```toml
backend.auth.raw = "your-password"
```

### 通过命令获取密码（推荐）

```toml
backend.auth.cmd = "pass show email/imap"
# backend.auth.cmd = "security find-generic-password -a user@example.com -s imap -w"
```

### 系统 keyring（需要 keyring 特性）

```toml
backend.auth.keyring = "imap-example"
```

然后运行 `himalaya account configure <account>` 来存储密码。

## Gmail 配置

```toml
[accounts.gmail]
email = "you@gmail.com"
display-name = "Your Name"
default = true

backend.type = "imap"
backend.host = "imap.gmail.com"
backend.port = 993
backend.encryption.type = "tls"
backend.login = "you@gmail.com"
backend.auth.type = "password"
backend.auth.cmd = "pass show google/app-password"

message.send.backend.type = "smtp"
message.send.backend.host = "smtp.gmail.com"
message.send.backend.port = 587
message.send.backend.encryption.type = "start-tls"
message.send.backend.login = "you@gmail.com"
message.send.backend.auth.type = "password"
message.send.backend.auth.cmd = "pass show google/app-password"

# Gmail 文件夹映射。缺少这些配置，「保存到已发送」会在
# SMTP 投递成功之后失败（Gmail 的已发送文件夹是
# `[Gmail]/Sent Mail`，而不是 `Sent`），`himalaya message send`
# 以非零状态退出。任何在该错误上重试的调用方都会重新执行
# SMTP——给收件人发出重复邮件。Gmail 必须始终包含此配置块。
folder.aliases.inbox = "INBOX"
folder.aliases.sent = "[Gmail]/Sent Mail"
folder.aliases.drafts = "[Gmail]/Drafts"
folder.aliases.trash = "[Gmail]/Trash"
```

**注意：** 如果 Gmail 启用了 2FA，则需要应用专用密码（App Password）。

## iCloud 配置

```toml
[accounts.icloud]
email = "you@icloud.com"
display-name = "Your Name"

backend.type = "imap"
backend.host = "imap.mail.me.com"
backend.port = 993
backend.encryption.type = "tls"
backend.login = "you@icloud.com"
backend.auth.type = "password"
backend.auth.cmd = "pass show icloud/app-password"

message.send.backend.type = "smtp"
message.send.backend.host = "smtp.mail.me.com"
message.send.backend.port = 587
message.send.backend.encryption.type = "start-tls"
message.send.backend.login = "you@icloud.com"
message.send.backend.auth.type = "password"
message.send.backend.auth.cmd = "pass show icloud/app-password"
```

**注意：** 请在 appleid.apple.com 生成应用专用密码。

## 文件夹别名

把 himalaya 的规范文件夹名（`inbox`、`sent`、`drafts`、
`trash`）映射为服务器实际使用的名称。请使用 v1.2.0 的
`folder.aliases.X` 语法（复数、点分键，直接写在
`[accounts.NAME]` 下）：

```toml
[accounts.default]
# ... 其他账户配置 ...

folder.aliases.inbox = "INBOX"
folder.aliases.sent = "Sent"
folder.aliases.drafts = "Drafts"
folder.aliases.trash = "Trash"
```

在 v1.2.0 中，等价的 TOML 子段写法也可用：

```toml
[accounts.default.folder.aliases]
inbox = "INBOX"
sent = "Sent"
drafts = "Drafts"
trash = "Trash"
```

> **不要使用单数 `alias` 写法。** v1.2.0 之前的文档展示的是
> `[accounts.NAME.folder.alias]`（单数）。v1.2.0 会静默
> 忽略该子段——TOML 解析不报错，但别名解析器
> 根本读不到它。于是每次查找都会回落到规范名。
> 在 Gmail 上（其 `sent` 实际是 `[Gmail]/Sent Mail`），
> 这意味着「保存到已发送」会在 SMTP 投递成功*之后*
> 失败，`himalaya message send` 以非零状态退出。任何
> 在该错误码上重试的调用方（agent、脚本、用户）都会
> 重新执行发送——包括 SMTP——给收件人发出重复邮件。
> 请始终使用 `folder.aliases.X`（复数）。

## 多账户

```toml
[accounts.personal]
email = "personal@example.com"
default = true
# ... 后端配置 ...

[accounts.work]
email = "work@company.com"
# ... 后端配置 ...
```

用 `--account` 切换账户：

```bash
himalaya --account work envelope list
```

## Notmuch 后端（本地邮件）

```toml
[accounts.local]
email = "user@example.com"

backend.type = "notmuch"
backend.db-path = "~/.mail/.notmuch"
```

## OAuth2 认证（用于支持它的服务商）

```toml
backend.auth.type = "oauth2"
backend.auth.client-id = "your-client-id"
backend.auth.client-secret.cmd = "pass show oauth/client-secret"
backend.auth.access-token.cmd = "pass show oauth/access-token"
backend.auth.refresh-token.cmd = "pass show oauth/refresh-token"
backend.auth.auth-url = "https://provider.com/oauth/authorize"
backend.auth.token-url = "https://provider.com/oauth/token"
```

## 其他选项

### 签名

```toml
[accounts.default]
signature = "Best regards,\nYour Name"
signature-delim = "-- \n"
```

### 下载目录

```toml
[accounts.default]
downloads-dir = "~/Downloads/himalaya"
```

### 用于撰写的编辑器

通过环境变量设置：

```bash
export EDITOR="vim"
```
