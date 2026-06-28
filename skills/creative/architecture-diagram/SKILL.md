---
name: architecture-diagram
description: "生成深色主题的 SVG 架构图/云/基础设施图，输出为 HTML。"
version: 1.0.0
author: Cocoon AI (hello@cocoon-ai.com), ported by Hermes Agent
license: MIT
dependencies: []
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [architecture, diagrams, SVG, HTML, visualization, infrastructure, cloud]
    related_skills: [concept-diagrams, excalidraw]
---

# 架构图技能（Architecture Diagram Skill）

生成专业、深色主题的技术架构图，输出为带内联 SVG 图形的独立 HTML 文件。无需外部工具、无需 API 密钥、无需渲染库 —— 只需写出 HTML 文件并在浏览器中打开即可。

## 适用范围

**最适合：**
- 软件系统架构（前端 / 后端 / 数据库分层）
- 云基础设施（VPC、区域、子网、托管服务）
- 微服务 / 服务网格拓扑
- 数据库 + API 地图、部署图
- 任何符合深色、网格背景美学的技术基础设施主题

**应优先考虑其他技能的场景：**
- 物理、化学、数学、生物或其他科学主题
- 实体物件（车辆、硬件、解剖结构、剖面图）
- 平面图、叙事旅程、教科书式的教学/可视化
- 手绘白板草图（考虑 `excalidraw`）
- 动画讲解（考虑动画类技能）

如果有更专精于该主题的技能，请优先使用它。如果都不合适，本技能也可作为通用的 SVG 图表兜底方案 —— 只是输出会带有下文描述的深色技术美学风格。

基于 [Cocoon AI 的 architecture-diagram-generator](https://github.com/Cocoon-AI/architecture-diagram-generator)（MIT）。

## 工作流

1. 用户描述其系统架构（组件、连接、所用技术）
2. 按下面的设计系统生成 HTML 文件
3. 用 `write_file` 保存为 `.html` 文件（例如 `~/architecture-diagram.html`）
4. 用户在任意浏览器中打开 —— 离线可用，无依赖

### 输出位置

将图表保存到用户指定的路径，或默认使用当前工作目录：
```
./[project-name]-architecture.html
```

### 预览

保存后，建议用户打开它：
```bash
# macOS
open ./my-architecture.html
# Linux
xdg-open ./my-architecture.html
```

## 设计系统与视觉语言

### 调色板（语义映射）

使用特定的 `rgba` 填充和十六进制描边来对组件分类：

| 组件类型 | 填充（rgba） | 描边（Hex） |
| :--- | :--- | :--- |
| **前端（Frontend）** | `rgba(8, 51, 68, 0.4)` | `#22d3ee`（cyan-400） |
| **后端（Backend）** | `rgba(6, 78, 59, 0.4)` | `#34d399`（emerald-400） |
| **数据库（Database）** | `rgba(76, 29, 149, 0.4)` | `#a78bfa`（violet-400） |
| **AWS/云（Cloud）** | `rgba(120, 53, 15, 0.3)` | `#fbbf24`（amber-400） |
| **安全（Security）** | `rgba(136, 19, 55, 0.4)` | `#fb7185`（rose-400） |
| **消息总线（Message Bus）** | `rgba(251, 146, 60, 0.3)` | `#fb923c`（orange-400） |
| **外部（External）** | `rgba(30, 41, 59, 0.5)` | `#94a3b8`（slate-400） |

### 字体与背景
- **字体：** JetBrains Mono（等宽字体），从 Google Fonts 加载
- **字号：** 12px（名称）、9px（子标签）、8px（注释）、7px（极小标签）
- **背景：** Slate-950（`#020617`），带细微的 40px 网格图案

```svg
<!-- 背景网格图案 -->
<pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
  <path d="M 40 0 L 0 0 0 40" fill="none" stroke="#1e293b" stroke-width="0.5"/>
</pattern>
```

## 技术实现细节

### 组件渲染
组件是圆角矩形（`rx="6"`），描边为 1.5px。为避免箭头透过半透明填充显现，使用**双矩形遮罩技巧**：
1. 先画一个不透明的背景矩形（`#0f172a`）
2. 再在其上画半透明的样式化矩形

### 连接线规则
- **Z 序：** 在 SVG 中**尽早**绘制箭头（在网格之后），使其渲染在组件框背后
- **箭头：** 通过 SVG marker 定义
- **安全流（Security Flows）：** 使用玫瑰色（`#fb7185`）虚线
- **边界（Boundaries）：**
  - *安全组：* 虚线（`4,4`），玫瑰色
  - *区域：* 大虚线（`8,4`），琥珀色，`rx="12"`

### 间距与布局逻辑
- **标准高度：** 60px（服务）；80-120px（大型组件）
- **垂直间距：** 组件之间至少 40px
- **消息总线：** 必须放置在服务*之间的空隙中*，不能与之重叠
- **图例位置：** **关键。** 必须放置在所有边界框之外。计算所有边界的最低 Y 坐标，并将图例放在其下方至少 20px 处。

## 文档结构

生成的 HTML 文件遵循四段式布局：
1. **页头：** 带脉冲圆点指示器的标题和副标题
2. **主 SVG：** 包含在圆角边框卡片中的图表
3. **摘要卡片：** 图表下方由三张卡片组成的网格，用于呈现高层信息
4. **页脚：** 极简的元信息

### 信息卡片模式
```html
<div class="card">
  <div class="card-header">
    <div class="card-dot cyan"></div>
    <h3>Title</h3>
  </div>
  <ul>
    <li>• Item one</li>
    <li>• Item two</li>
  </ul>
</div>
```

## 输出要求
- **单文件：** 一个自包含的 `.html` 文件
- **无外部依赖：** 所有 CSS 和 SVG 必须内联（Google Fonts 除外）
- **无 JavaScript：** 任何动画（如脉冲圆点）均使用纯 CSS
- **兼容性：** 必须能在任何现代浏览器中正确渲染

## 模板参考

加载完整的 HTML 模板以获取精确的结构、CSS 和 SVG 组件示例：

```
skill_view(name="architecture-diagram", file_path="templates/template.html")
```

该模板包含每种组件类型（前端、后端、数据库、云、安全）的工作示例，各种箭头样式（标准、虚线、曲线）、安全组、区域边界以及图例 —— 在生成图表时请将其作为结构参考。
