---
name: excalidraw
description: "手绘风格的 Excalidraw JSON 图表（架构图、流程图、时序图）。"
version: 1.0.0
author: Hermes Agent
license: MIT
dependencies: []
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Excalidraw, Diagrams, Flowcharts, Architecture, Visualization, JSON]
    related_skills: []

---

# Excalidraw 图表技能

通过编写标准 Excalidraw 元素 JSON 来创建图表，并保存为 `.excalidraw` 文件。这些文件可以拖放到 [excalidraw.com](https://excalidraw.com) 上查看和编辑。无需账号、无需 API 密钥、无需渲染库——只需要 JSON。

## 何时使用

为架构图、流程图、时序图、概念图等生成 `.excalidraw` 文件。文件可在 excalidraw.com 打开，或上传以获取可分享的链接。

## 工作流程

1. **加载本技能**（你已经完成了）
2. **编写元素 JSON**——一个由 Excalidraw 元素对象组成的数组
3. **保存文件**，使用 `write_file` 创建一个 `.excalidraw` 文件
4. **（可选）上传**，通过 `terminal` 运行 `scripts/upload.py` 获取可分享的链接

### 保存图表

将你的元素数组包裹在标准的 `.excalidraw` 信封中，并使用 `write_file` 保存：

```json
{
  "type": "excalidraw",
  "version": 2,
  "source": "hermes-agent",
  "elements": [ ...你的元素数组放这里... ],
  "appState": {
    "viewBackgroundColor": "#ffffff"
  }
}
```

可保存到任意路径，例如 `~/diagrams/my_diagram.excalidraw`。

### 上传以获取可分享的链接

通过 terminal 运行上传脚本（位于本技能的 `scripts/` 目录中）：

```bash
python skills/diagramming/excalidraw/scripts/upload.py ~/diagrams/my_diagram.excalidraw
```

这会将其上传到 excalidraw.com（无需账号）并打印一个可分享的 URL。需要 `cryptography` pip 包（`pip install cryptography`）。

---

## 元素格式参考

### 必填字段（所有元素）
`type`、`id`（唯一字符串）、`x`、`y`、`width`、`height`

### 默认值（可省略——会自动应用）
- `strokeColor`: `"#1e1e1e"`
- `backgroundColor`: `"transparent"`
- `fillStyle`: `"solid"`
- `strokeWidth`: `2`
- `roughness`: `1`（手绘外观）
- `opacity`: `100`

画布背景为白色。

### 元素类型

**矩形**：
```json
{ "type": "rectangle", "id": "r1", "x": 100, "y": 100, "width": 200, "height": 100 }
```
- `roundness: { "type": 3 }` 用于圆角
- `backgroundColor: "#a5d8ff"`、`fillStyle: "solid"` 用于填充

**椭圆**：
```json
{ "type": "ellipse", "id": "e1", "x": 100, "y": 100, "width": 150, "height": 150 }
```

**菱形**：
```json
{ "type": "diamond", "id": "d1", "x": 100, "y": 100, "width": 150, "height": 150 }
```

**带标签的形状（容器绑定）**——创建一个绑定到该形状的文本元素：

> **警告：** 不要在形状上使用 `"label": { "text": "..." }`。这**不是**有效的
> Excalidraw 属性，会被静默忽略，从而产生空白形状。你**必须**
> 使用下面的容器绑定方法。

形状需要 `boundElements` 列出该文本，而文本需要 `containerId` 指回该形状：
```json
{ "type": "rectangle", "id": "r1", "x": 100, "y": 100, "width": 200, "height": 80,
  "roundness": { "type": 3 }, "backgroundColor": "#a5d8ff", "fillStyle": "solid",
  "boundElements": [{ "id": "t_r1", "type": "text" }] },
{ "type": "text", "id": "t_r1", "x": 105, "y": 110, "width": 190, "height": 25,
  "text": "Hello", "fontSize": 20, "fontFamily": 1, "strokeColor": "#1e1e1e",
  "textAlign": "center", "verticalAlign": "middle",
  "containerId": "r1", "originalText": "Hello", "autoResize": true }
```
- 适用于矩形、椭圆、菱形
- 当设置了 `containerId` 时，Excalidraw 会自动将文本居中
- 文本的 `x`/`y`/`width`/`height` 是近似值——Excalidraw 在加载时会重新计算
- `originalText` 应与 `text` 一致
- 始终包含 `fontFamily: 1`（Virgil/手绘字体）

**带标签的箭头**——同样的容器绑定方法：
```json
{ "type": "arrow", "id": "a1", "x": 300, "y": 150, "width": 200, "height": 0,
  "points": [[0,0],[200,0]], "endArrowhead": "arrow",
  "boundElements": [{ "id": "t_a1", "type": "text" }] },
{ "type": "text", "id": "t_a1", "x": 370, "y": 130, "width": 60, "height": 20,
  "text": "connects", "fontSize": 16, "fontFamily": 1, "strokeColor": "#1e1e1e",
  "textAlign": "center", "verticalAlign": "middle",
  "containerId": "a1", "originalText": "connects", "autoResize": true }
```

**独立文本**（仅用于标题和注释——无容器）：
```json
{ "type": "text", "id": "t1", "x": 150, "y": 138, "text": "Hello", "fontSize": 20,
  "fontFamily": 1, "strokeColor": "#1e1e1e", "originalText": "Hello", "autoResize": true }
```
- `x` 是左边缘。要在位置 `cx` 处居中：`x = cx - (text.length * fontSize * 0.5) / 2`
- 不要依赖 `textAlign` 或 `width` 来定位

**箭头**：
```json
{ "type": "arrow", "id": "a1", "x": 300, "y": 150, "width": 200, "height": 0,
  "points": [[0,0],[200,0]], "endArrowhead": "arrow" }
```
- `points`: `[dx, dy]` 相对于元素 `x`、`y` 的偏移量
- `endArrowhead`: `null` | `"arrow"` | `"bar"` | `"dot"` | `"triangle"`
- `strokeStyle`: `"solid"`（默认）| `"dashed"` | `"dotted"`

### 箭头绑定（将箭头连接到形状）

```json
{
  "type": "arrow", "id": "a1", "x": 300, "y": 150, "width": 150, "height": 0,
  "points": [[0,0],[150,0]], "endArrowhead": "arrow",
  "startBinding": { "elementId": "r1", "fixedPoint": [1, 0.5] },
  "endBinding": { "elementId": "r2", "fixedPoint": [0, 0.5] }
}
```

`fixedPoint` 坐标：`上=[0.5,0]`、`下=[0.5,1]`、`左=[0,0.5]`、`右=[1,0.5]`

### 绘制顺序（z 轴顺序）
- 数组顺序 = z 轴顺序（第一个 = 最底层，最后一个 = 最顶层）
- 按顺序依次输出：背景区域 → 形状 → 其绑定文本 → 其箭头 → 下一个形状
- 错误做法：先所有矩形，再所有文本，再所有箭头
- 正确做法：背景区域 → 形状1 → 形状1的文本 → 箭头1 → 箭头标签文本 → 形状2 → 形状2的文本 → ...
- 始终将绑定文本元素紧随其容器形状之后放置

### 尺寸指南

**字号：**
- 正文字体、标签、描述的最小 `fontSize`：**16**
- 标题和页眉的最小 `fontSize`：**20**
- 仅次要注释的最小 `fontSize`：**14**（谨慎使用）
- 永远不要使用小于 14 的 `fontSize`

**元素尺寸：**
- 带标签的矩形/椭圆最小形状尺寸：120x60
- 元素之间至少留出 20-30px 的间距
- 宁可使用更少、更大的元素，也不要使用许多微小的元素

### 颜色调色板

完整颜色表请参见 `references/colors.md`。快速参考：

| 用途 | 填充颜色 | Hex |
|-----|-----------|-----|
| 主要 / 输入 | 浅蓝 | `#a5d8ff` |
| 成功 / 输出 | 浅绿 | `#b2f2bb` |
| 警告 / 外部 | 浅橙 | `#ffd8a8` |
| 处理 / 特殊 | 浅紫 | `#d0bfff` |
| 错误 / 严重 | 浅红 | `#ffc9c9` |
| 备注 / 决策 | 浅黄 | `#fff3bf` |
| 存储 / 数据 | 浅青 | `#c3fae8` |

### 提示
- 在整张图中保持颜色调色板的一致使用
- **文字对比度至关重要**——切勿在白色背景上使用浅灰色。白色背景上的最小文字颜色：`#757575`
- 不要在文本中使用 emoji——它们无法在 Excalidraw 的字体中渲染
- 暗色模式图表请参见 `references/dark-mode.md`
- 更多示例请参见 `references/examples.md`


