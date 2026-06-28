---
name: openhue
description: "通过 OpenHue CLI 控制 Philips Hue 灯光、场景和房间。"
version: 1.0.0
author: community
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Smart-Home, Hue, Lights, IoT, Automation]
    homepage: https://www.openhue.io/cli
prerequisites:
  commands: [openhue]
---

# OpenHue CLI

通过 Hue Bridge 从终端控制 Philips Hue 的灯光和场景。

## 前置条件

```bash
# Linux（预编译二进制）
curl -sL https://github.com/openhue/openhue-cli/releases/latest/download/openhue-linux-amd64 -o ~/.local/bin/openhue && chmod +x ~/.local/bin/openhue

# macOS
brew install openhue/cli/openhue-cli
```

首次运行需要按下 Hue Bridge 上的按钮进行配对。Bridge 必须与本机处于同一局域网。

## 适用场景

- “打开/关闭灯”
- “把客厅的灯调暗”
- “设置一个场景”或“影院模式”
- 控制特定的 Hue 房间、区域或单个灯泡
- 调整亮度、颜色或色温

## 常用命令

### 列出资源

```bash
openhue get light       # 列出所有灯光
openhue get room        # 列出所有房间
openhue get scene       # 列出所有场景
```

### 控制灯光

```bash
# 打开/关闭
openhue set light "Bedroom Lamp" --on
openhue set light "Bedroom Lamp" --off

# 亮度（0-100）
openhue set light "Bedroom Lamp" --on --brightness 50

# 色温（暖到冷：153-500 mirek）
openhue set light "Bedroom Lamp" --on --temperature 300

# 颜色（按名称或十六进制）
openhue set light "Bedroom Lamp" --on --color red
openhue set light "Bedroom Lamp" --on --rgb "#FF5500"
```

### 控制房间

```bash
# 关闭整个房间
openhue set room "Bedroom" --off

# 设置房间亮度
openhue set room "Bedroom" --on --brightness 30
```

### 场景

```bash
openhue set scene "Relax" --room "Bedroom"
openhue set scene "Concentrate" --room "Office"
```

## 快速预设

```bash
# 睡前（暗暖光）
openhue set room "Bedroom" --on --brightness 20 --temperature 450

# 工作模式（亮冷光）
openhue set room "Office" --on --brightness 100 --temperature 250

# 影院模式（暗光）
openhue set room "Living Room" --on --brightness 10

# 全部关闭
openhue set room "Bedroom" --off
openhue set room "Office" --off
openhue set room "Living Room" --off
```

## 注意事项

- Bridge 必须与运行 Hermes 的机器处于同一局域网
- 首次运行需要在 Hue Bridge 上物理按下按钮以完成授权
- 颜色仅在支持彩色的灯泡上生效（不支持纯白型号）
- 灯光和房间的名称区分大小写 —— 可用 `openhue get light` 查看确切名称
- 配合 cron 任务可很好地实现定时灯光（例如睡前调暗、起床时调亮）
