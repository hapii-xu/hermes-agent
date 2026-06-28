# 设计系统：Intercom


> **Hermes Agent — 实现说明**
>
> 原站点使用专有字体。对于自包含的 HTML 输出，请使用以下 CDN 替代字体：
> - **主字体：** `Inter` | **等宽字体：** `system monospace stack`
> - **字体栈（CSS）：** `font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;`
> - **等宽字体栈（CSS）：** `font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;`
> ```html
> <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
> ```
> 使用 `write_file` 创建 HTML，通过 `generative-widgets` 技能（cloudflared 隧道）提供服务。
> 生成后用 `browser_vision` 验证视觉准确性。

## 1. 视觉主题与氛围

Intercom 的网站是一个温暖、自信的客户服务平台，通过干净、编辑式的设计语言传达出「AI 优先的帮助台」。页面运行在温暖的灰白画布（`#faf9f6`）上，搭配近黑（`#111111`）文字，营造出一种亲切的、杂志式的阅读体验。标志性的 Fin 橙色（`#ff5600`）—— 以 Intercom 的 AI agent 命名 —— 作为温暖中性色调中唯一的鲜艳点缀。

字体使用 Saans —— 一款定制的几何无衬线字体，带有激进的负字距（80px 时 -2.4px，24px 时 -0.48px），并在所有标题字号上保持一致的 1.00 行高。这打造出超压缩的、广告牌式的标题，给人一种工程化、精准的感觉。Serrif 提供编辑时刻的衬线伴侣，SaansMono 处理代码和大写技术标签。MediumLL 和 LLMedium 出现在特定 UI 语境中，构成了一个丰富的五字体生态。

Intercom 的独特之处在于其极其锋利的几何感 —— 按钮使用 4px 边框圆角，打造出近乎矩形的交互元素，给人一种工业感和精准感，与温暖的表面色形成对比。按钮悬停状态使用 `scale(1.1)` 放大，营造出一种物理上的「生长」交互。边框系统使用温暖的燕麦色调（`#dedbd6`），以及基于 oklab 的不透明度值，以实现精细的颜色管理。

**关键特征：**
- 温暖的灰白画布（`#faf9f6`）搭配燕麦色调边框（`#dedbd6`）
- Saans 字体带极负字距（80px 时 -2.4px）和 1.00 行高
- Fin 橙色（`#ff5600`）作为唯一的品牌点缀色
- 锋利的 4px 边框圆角 —— 近乎矩形的按钮和元素
- Scale(1.1) 悬停配合 scale(0.85) 激活 —— 物理按钮交互
- SaansMono 大写标签，宽字距（0.6px–1.2px）
- 丰富的多色报告配色（蓝、绿、红、粉、青柠、橙）
- oklab 颜色值用于精细的不透明度管理

## 2. 配色方案与角色

### 主色
- **Off Black**（`#111111`）：`--color-off-black`，主要文字、按钮背景
- **Pure White**（`#ffffff`）：`--wsc-color-content-primary`，主要表面
- **Warm Cream**（`#faf9f6`）：按钮背景、卡片表面
- **Fin Orange**（`#ff5600`）：`--color-fin`，主要品牌点缀色
- **Report Orange**（`#fe4c02`）：`--color-report-orange`，数据可视化

### 报告配色
- **Report Blue**（`#65b5ff`）：`--color-report-blue`
- **Report Green**（`#0bdf50`）：`--color-report-green`
- **Report Red**（`#c41c1c`）：`--color-report-red`
- **Report Pink**（`#ff2067`）：`--color-report-pink`
- **Report Lime**（`#b3e01c`）：`--color-report-lime-300`
- **Green**（`#00da00`）：`--color-green`
- **Deep Blue**（`#0007cb`）：深蓝点缀

### 中性色阶（暖调）
- **Black 80**（`#313130`）：`--wsc-color-black-80`，深中性色
- **Black 60**（`#626260`）：`--wsc-color-black-60`，中等中性色
- **Black 50**（`#7b7b78`）：`--wsc-color-black-50`，柔和文字
- **Content Tertiary**（`#9c9fa5`）：`--wsc-color-content-tertiary`
- **Oat Border**（`#dedbd6`）：温暖的边框色
- **Warm Sand**（`#d3cec6`）：浅暖中性色

## 3. 字体规则

### 字体家族
- **主字体**：`Saans`，回退：`Saans Fallback, ui-sans-serif, system-ui`
- **衬线字体**：`Serrif`，回退：`Serrif Fallback, ui-serif, Georgia`
- **等宽字体**：`SaansMono`，回退：`SaansMono Fallback, ui-monospace`
- **UI**：`MediumLL` / `LLMedium`，回退：`system-ui, -apple-system`

### 层级

| 角色 | 字体 | 字号 | 字重 | 行高 | 字距 |
|------|------|------|--------|-------------|----------------|
| Display Hero | Saans | 80px | 400 | 1.00（紧凑） | -2.4px |
| Section Heading | Saans | 54px | 400 | 1.00 | -1.6px |
| Sub-heading | Saans | 40px | 400 | 1.00 | -1.2px |
| Card Title | Saans | 32px | 400 | 1.00 | -0.96px |
| Feature Title | Saans | 24px | 400 | 1.00 | -0.48px |
| Body Emphasis | Saans | 20px | 400 | 0.95 | -0.2px |
| Nav / UI | Saans | 18px | 400 | 1.00 | normal |
| Body | Saans | 16px | 400 | 1.50 | normal |
| Body Light | Saans | 14px | 300 | 1.40 | normal |
| Button | Saans | 16px / 14px | 400 | 1.50 / 1.43 | normal |
| Button Bold | LLMedium | 16px | 700 | 1.20 | 0.16px |
| Serif Body | Serrif | 16px | 300 | 1.40 | -0.16px |
| Mono Label | SaansMono | 12px | 400–500 | 1.00–1.30 | 0.6px–1.2px 大写 |

## 4. 组件样式

### 按钮

**主深色**
- 背景：`#111111`
- 文字：`#ffffff`
- 内边距：0px 14px
- 圆角：4px
- 悬停：白色背景、深色文字、scale(1.1)
- 激活：绿色背景（`#2c6415`）、scale(0.85)

**描边**
- 背景：透明
- 文字：`#111111`
- 边框：`1px solid #111111`
- 圆角：4px
- 相同的 scale 悬停/激活行为

**温暖卡片按钮**
- 背景：`#faf9f6`
- 文字：`#111111`
- 内边距：16px
- 边框：`1px solid oklab(... / 0.1)`

### 卡片与容器
- 背景：`#faf9f6`（温暖奶油色）
- 边框：`1px solid #dedbd6`（温暖燕麦色）
- 圆角：8px
- 无可见阴影

### 导航
- 链接使用 Saans 16px
- 白色上的近黑文字
- 小的 4px–6px 圆角按钮
- AI 功能使用橙色 Fin 点缀

## 5. 布局原则

### 间距：8px, 10px, 12px, 14px, 16px, 20px, 24px, 32px, 40px, 48px, 60px, 64px, 80px, 96px
### 边框圆角：4px（按钮）、6px（导航项）、8px（卡片、容器）

## 6. 深度与抬升
极简阴影。深度通过温暖的边框色和表面色调来营造。

## 7. 宜与不宜

### 宜
- 所有标题使用 Saans 配 1.00 行高和负字距
- 按钮应用 4px 圆角 —— 锋利的几何感就是品牌识别
- 仅在 AI/品牌点缀时使用 Fin 橙色（#ff5600）
- 按钮应用 scale(1.1) 悬停
- 使用温暖的中性色（#faf9f6, #dedbd6）

### 不宜
- 按钮圆角不要超过 4px
- 不要将 Fin 橙色用作装饰
- 不要使用冷灰色边框 —— 始终用温暖的燕麦色调
- 不要跳过标题的负字距

## 8. 响应式行为
断点：425px, 530px, 600px, 640px, 768px, 896px

## 9. Agent 提示词指南

### 快速颜色参考
- 文字：Off Black（`#111111`）
- 背景：Warm Cream（`#faf9f6`）
- 点缀：Fin 橙色（`#ff5600`）
- 边框：Oat（`#dedbd6`）
- 柔和：`#7b7b78`

### 组件示例提示词
- "创建 hero：温暖奶油色（#faf9f6）背景。Saans 80px 字重 400，行高 1.00，字距 -2.4px，#111111。深色按钮（#111111，4px 圆角）。悬停：scale(1.1)，白色背景。"
