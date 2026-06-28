---
name: apple-reminders
description: "通过 remindctl 管理 Apple Reminders：添加、列出、完成。"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [Reminders, tasks, todo, macOS, Apple]
prerequisites:
  commands: [remindctl]
---

# Apple Reminders

使用 `remindctl` 直接在终端中管理 Apple Reminders。任务会通过 iCloud 在所有 Apple 设备间同步。

## 前置条件

- 安装了 Reminders.app 的 **macOS**
- 安装：`brew install steipete/tap/remindctl`
- 出现提示时授予 Reminders 权限
- 检查：`remindctl status` / 请求授权：`remindctl authorize`

## 适用场景

- 用户提到“reminder”或“Reminders app”
- 创建带截止日期、可同步到 iOS 的个人待办事项
- 管理 Apple Reminders 列表
- 用户希望任务出现在其 iPhone/iPad 上

## 不适用场景

- 安排 agent 提醒 → 改用 cronjob 工具
- 日历事件 → 使用 Apple Calendar 或 Google Calendar
- 项目任务管理 → 使用 GitHub Issues、Notion 等
- 如果用户说“提醒我”，但实际意思是 agent 提醒 → 先澄清

## 快速参考

### 查看提醒

```bash
remindctl                    # 今日提醒
remindctl today              # 今天
remindctl tomorrow           # 明天
remindctl week               # 本周
remindctl overdue            # 已逾期
remindctl all                # 全部
remindctl 2026-01-04         # 指定日期
```

### 管理列表

```bash
remindctl list               # 列出所有列表
remindctl list Work          # 显示指定列表
remindctl list Projects --create    # 创建列表
remindctl list Work --delete        # 删除列表
```

### 创建提醒

```bash
remindctl add "Buy milk"
remindctl add --title "Call mom" --list Personal --due tomorrow
remindctl add --title "Meeting prep" --due "2026-02-15 09:00"
```

### 截止时间 vs 闹钟 / 提前提醒

`--due` 和 `--alarm` 是不同的字段：

- `--due` 设置提醒的截止日期/时间。
- `--alarm` 设置 EventKit 闹钟/通知触发器。带具体时间的截止提醒可能默认在截止时间触发闹钟，但当用户要求更早提醒时，请显式传入 `--alarm`。

对于一个下午 2:00 到期、希望提前 30 分钟收到通知的提醒：

```bash
remindctl add --title "Hairdresser" --due "2026-05-15 14:00" --alarm "2026-05-15 13:30"
```

编辑已有的提醒：

```bash
remindctl edit 87354 --due "2026-05-15 14:00" --alarm "2026-05-15 13:30"
```

Reminders 的界面可能会按闹钟时间显示或对该条目进行分组，因为通知是在那个时间触发的。请用 JSON 验证，而不要假设截止时间已被移动：

```bash
remindctl today --json
```

预期的字段含义：

- `dueDate`：实际的截止时间
- `alarmDate`：通知 / 提前提醒时间

Apple 公开的 `EKReminder` 文档只列出了提醒专有的属性。闹钟支持来自于 remindctl 的 `--alarm` 标志所暴露的、继承自 `EKCalendarItem` 的行为。

### 完成 / 删除

```bash
remindctl complete 1 2 3          # 按 ID 完成
remindctl delete 4A83 --force     # 按 ID 删除
```

### 输出格式

```bash
remindctl today --json       # 用于脚本处理的 JSON
remindctl today --plain      # TSV 格式
remindctl today --quiet      # 仅显示计数
```

## 日期格式

可被 `--due` 和日期过滤器接受：
- `today`、`tomorrow`、`yesterday`
- `YYYY-MM-DD`
- `YYYY-MM-DD HH:mm`
- ISO 8601（`2026-01-04T12:34:56Z`）

## 规则

1. 当用户说“提醒我”时，先澄清：是 Apple Reminders（同步到手机）还是 agent 的 cronjob 提醒
2. 创建前始终确认提醒内容和截止日期
3. 使用 `--json` 进行程序化解析
