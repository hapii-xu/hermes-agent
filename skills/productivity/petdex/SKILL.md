---
name: petdex
description: 为 Hermes 安装并选择动画 petdex 吉祥物。
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [petdex, mascot, display, cli, tui, desktop]
    category: productivity
    homepage: https://petdex.dev
---

# Petdex Skill

浏览、安装并从公开的
[petdex](https://github.com/crafter-station/petdex) 画廊中选择动画“宠物”
吉祥物。安装好的宠物会跨 Hermes CLI、TUI 和桌面应用，
对 agent 的活动做出反应（空闲、运行工具、审查中、出错、完成）。
本 skill 驱动 `hermes pets` CLI 和 `display.pet` 配置 —— 它并不生成
精灵图（sprite）。

## 适用场景

- 用户想要一个桌面/终端吉祥物，或询问有关“pets”/petdex 的问题。
- 用户想更换、预览或禁用当前激活的宠物。
- 排查宠物为什么不显示（终端图形支持、配置等）。

## 前置条件

- 需要访问 `petdex.dev` 以获取画廊/清单（只读，无需认证）。
- 需要 Pillow（Hermes 的核心依赖）来解码精灵图 —— 已经预装。
- 如需高保真终端渲染：支持图形的终端（kitty、
  Ghostty、WezTerm、iTerm2 或 sixel）。否则会自动使用
  truecolor Unicode 半块字符作为回退方案。

## 如何运行

使用 `terminal` 工具运行 `hermes pets <subcommand>`。

## 快速参考

| 目标 | 命令 |
| --- | --- |
| 浏览画廊 | `hermes pets list`（可加子串筛选：`hermes pets list cat`） |
| 列出已安装的宠物 | `hermes pets list --installed` |
| 安装一个宠物 | `hermes pets install <slug>`（加 `--select` 可同时激活） |
| 设置当前宠物 | `hermes pets select <slug>`（省略 slug 弹出选择器） |
| 在所有界面统一缩放宠物 | `hermes pets scale <factor>`（如 `0.5`，范围限制 0.1–3.0） |
| 在终端预览/动画 | `hermes pets show [slug] [--cycle] [--state run]` |
| 禁用宠物 | `hermes pets off` |
| 移除一个宠物 | `hermes pets remove <slug>` |
| 诊断配置 | `hermes pets doctor` |

## 操作流程

1. 查找宠物：`hermes pets list <query>`，记下它的 `slug`。
2. 安装 + 激活：`hermes pets install <slug> --select`。
3. 预览：`hermes pets show`（Ctrl+C 停止）。
4. 确认配置：`hermes pets doctor` —— 会显示已解析的宠物、配置的
   渲染模式、检测到的终端图形协议以及实际生效的模式。

宠物会安装到 `<HERMES_HOME>/pets/<slug>/`（按 profile 区分）。选中一个宠物
会将 `display.pet.slug` + `display.pet.enabled` 写入 `config.yaml`。

## 配置

在 `config.yaml` 的 `display.pet` 下：

- `enabled`（布尔）— 总开关。
- `slug`（字符串）— 当前激活的宠物；留空 = 第一个已安装的宠物。
- `render_mode` — `auto`（自动检测）| `kitty` | `iterm` | `sixel` | `unicode` | `off`。
- `scale`（浮点数）— 原生 192×208 帧在屏幕上的尺寸（默认 0.33，
  范围限制 0.1–3.0）。一个旋钮即可缩放所有界面；可通过
  `hermes pets scale <factor>`、`/pet scale` slash 命令或桌面端的
  Appearance 滑块来设置。
- `unicode_cols`（整数）— Unicode 回退模式下显示的列宽。

## 常见陷阱

- 只有当某个宠物被安装并且被选中（`enabled: true`）时才会显示。
- 在管道/重定向（无 TTY）中，终端渲染会按设计被禁用。
- petdex 的 npm CLI 安装到 `~/.codex/pets`；Hermes 使用自己按
  profile 区分的 `<HERMES_HOME>/pets/` —— 请通过 `hermes pets` 安装。

## 验证

- 当某个宠物已安装、被选中、已启用且 Pillow 可导入时，
  `hermes pets doctor` 会报告 `✓ ready`。
