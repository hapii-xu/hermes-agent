---
name: findmy
description: "通过 macOS 上的 FindMy.app 追踪 Apple 设备/AirTag。"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [FindMy, AirTag, location, tracking, macOS, Apple]
---

# Find My (Apple)

通过 macOS 上的 FindMy.app 追踪 Apple 设备和 AirTag。由于 Apple 没有为 Find My 提供 CLI，本 skill 使用 AppleScript 打开应用，并通过屏幕截图读取设备位置。

## 前置条件

- **macOS**，已安装 Find My 应用并登录 iCloud
- 设备/AirTag 已在 Find My 中注册
- 终端已获得"屏幕录制"权限（系统设置 → 隐私与安全 → 屏幕录制）
- **可选但推荐**：安装 `peekaboo` 以获得更好的 UI 自动化体验：
  `brew install steipete/tap/peekaboo`

## 适用场景

- 用户询问"我的[设备/猫/钥匙/包]在哪？"
- 追踪 AirTag 位置
- 查看设备位置（iPhone、iPad、Mac、AirPods）
- 长时间监控宠物或物品的移动轨迹（AirTag 巡逻路线）

## 方法 1：AppleScript + 截图（基础）

### 打开 FindMy 并导航

```bash
# 打开 Find My 应用
osascript -e 'tell application "FindMy" to activate'

# 等待加载
sleep 3

# 对 Find My 窗口截图
screencapture -w -o /tmp/findmy.png
```

然后用 `vision_analyze` 读取截图：
```
vision_analyze(image_url="/tmp/findmy.png", question="What devices/items are shown and what are their locations?")
```

### 在标签页之间切换

```bash
# 切换到 Devices 标签页
osascript -e '
tell application "System Events"
    tell process "FindMy"
        click button "Devices" of toolbar 1 of window 1
    end tell
end tell'

# 切换到 Items 标签页（AirTag）
osascript -e '
tell application "System Events"
    tell process "FindMy"
        click button "Items" of toolbar 1 of window 1
    end tell
end tell'
```

## 方法 2：Peekaboo UI 自动化（推荐）

如果已安装 `peekaboo`，可使用它获得更可靠的 UI 交互：

```bash
# 打开 Find My
osascript -e 'tell application "FindMy" to activate'
sleep 3

# 截取并标注 UI
peekaboo see --app "FindMy" --annotate --path /tmp/findmy-ui.png

# 按元素 ID 点击特定设备/物品
peekaboo click --on B3 --app "FindMy"

# 截取详情视图
peekaboo image --app "FindMy" --path /tmp/findmy-detail.png
```

然后用视觉分析：
```
vision_analyze(image_url="/tmp/findmy-detail.png", question="What is the location shown for this device/item? Include address and coordinates if visible.")
```

## 工作流：随时间追踪 AirTag 位置

用于监控某个 AirTag（例如追踪猫的巡逻路线）：

```bash
# 1. 打开 FindMy 到 Items 标签页
osascript -e 'tell application "FindMy" to activate'
sleep 3

# 2. 点击该 AirTag 物品（停留在页面上 —— AirTag 仅在页面打开时更新位置）

# 3. 周期性截取位置
while true; do
    screencapture -w -o /tmp/findmy-$(date +%H%M%S).png
    sleep 300  # 每 5 分钟一次
done
```

用视觉分析每张截图以提取坐标，然后汇总成一条路线。

## 限制

- FindMy **没有 CLI 或 API** —— 必须使用 UI 自动化
- AirTag 仅在 Find My 页面处于活动显示状态时才会更新位置
- 位置精度取决于 Find My 网络中附近的 Apple 设备
- 截图需要"屏幕录制"权限
- AppleScript UI 自动化可能随 macOS 版本变化而失效

## 规则

1. 追踪 AirTag 时让 Find My 应用保持在前台（最小化后更新会停止）
2. 使用 `vision_analyze` 读取截图内容 —— 不要尝试自己解析像素
3. 进行持续追踪时，使用 cron 定时任务周期性截取并记录位置
4. 尊重隐私 —— 仅追踪用户拥有的设备/物品
