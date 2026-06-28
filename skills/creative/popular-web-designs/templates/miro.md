# 设计系统：Miro


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

Miro 的网站是一个干净、以协作为导向的平台，通过慷慨的留白、柔和的点缀色和自信的几何字体，传达出「视觉化思考」。设计使用以白色为主的画布，搭配近黑文字（`#1c1c1e`），以及独特的柔和配色 —— 珊瑚、玫瑰、青、橙、黄、苔藓 —— 每种颜色代表不同的协作场景。

字体使用 Roobert PRO Medium 作为主要展示字体，带有 OpenType 字符变体（`"blwf", "cv03", "cv04", "cv09", "cv11"`）和负字距（56px 时 -1.68px）。Noto Sans 处理正文文字，带有自己的样式集（`"liga" 0, "ss01", "ss04", "ss05"`）。该设计使用 Framer 构建，带来流畅的动画和现代的组件模式。

**关键特征：**
- 白色画布搭配近黑（`#1c1c1e`）文字
- Roobert PRO Medium 带多种 OpenType 字符变体
- 柔和点缀配色：珊瑚、玫瑰、青、橙、黄、苔藓（浅色 + 深色配对）
- Blue 450（`#5b76fe`）作为主要交互色
- 成功绿（`#00b473`）用于正向状态
- 慷慨的圆角：8px–50px 范围
- 用 Framer 构建，带流畅的运动模式
- 环形阴影边框：`rgb(224,226,232) 0px 0px 0px 1px`

## 2. 配色方案与角色

### 主色
- **Near Black**（`#1c1c1e`）：主要文字
- **White**（`#ffffff`）：`--tw-color-white`，主要表面
- **Blue 450**（`#5b76fe`）：`--tw-color-blue-450`，主要交互色
- **Actionable Pressed**（`#2a41b6`）：`--tw-color-actionable-pressed`

### 柔和点缀（浅/深配对）
- **Coral**：浅 `#ffc6c6` / 深 `#600000`
- **Rose**：浅 `#ffd8f4` / 深（隐含）
- **Teal**：浅 `#c3faf5` / 深 `#187574`
- **Orange**：浅 `#ffe6cd`
- **Yellow**：深 `#746019`
- **Moss**：深 `#187574`
- **Pink**（`#fde0f0`）：柔粉表面
- **Red**（`#fbd4d4`）：浅红表面
- **Dark Red**（`#e3c5c5`）：柔和红

### 语义色
- **Success**（`#00b473`）：`--tw-color-success-accent`

### 中性色
- **Slate**（`#555a6a`）：次要文字
- **Input Placeholder**（`#a5a8b5`）：`--tw-color-input-placeholder`
- **Border**（`#c7cad5`）：按钮边框
- **Ring**（`rgb(224,226,232)`）：阴影充当边框

## 3. 字体规则

### 字体家族
- **展示字体**：`Roobert PRO Medium`，回退：Placeholder —— `"blwf", "cv03", "cv04", "cv09", "cv11"`
- **展示变体**：`Roobert PRO SemiBold`, `Roobert PRO SemiBold Italic`, `Roobert PRO`
- **正文字体**：`Noto Sans` —— `"liga" 0, "ss01", "ss04", "ss05"`

### 层级

| 角色 | 字体 | 字号 | 字重 | 行高 | 字距 |
|------|------|------|--------|-------------|----------------|
| Display Hero | Roobert PRO Medium | 56px | 400 | 1.15 | -1.68px |
| Section Heading | Roobert PRO Medium | 48px | 400 | 1.15 | -1.44px |
| Card Title | Roobert PRO Medium | 24px | 400 | 1.15 | -0.72px |
| Sub-heading | Noto Sans | 22px | 400 | 1.35 | -0.44px |
| Feature | Roobert PRO Medium | 18px | 600 | 1.35 | normal |
| Body | Noto Sans | 18px | 400 | 1.45 | normal |
| Body Standard | Noto Sans | 16px | 400–600 | 1.50 | -0.16px |
| Button | Roobert PRO Medium | 17.5px | 700 | 1.29 | 0.175px |
| Caption | Roobert PRO Medium | 14px | 400 | 1.71 | normal |
| Small | Roobert PRO Medium | 12px | 400 | 1.15 | -0.36px |
| Micro Uppercase | Roobert PRO | 10.5px | 400 | 0.90 | 大写 |

## 4. 组件样式

### 按钮
- 描边：透明背景，`1px solid #c7cad5`，8px 圆角，7px 12px 内边距
- 白色圆形：50% 圆角，白色背景带阴影
- 蓝色主按钮（从交互色隐含）

### 卡片：12px–24px 圆角，柔和背景
### 输入框：白色背景，`1px solid #e9eaef`，8px 圆角，16px 内边距

## 5. 布局原则
- 间距：1–24px 基准比例
- 圆角：8px（按钮）、10px–12px（卡片）、20px–24px（面板）、40px–50px（大容器）
- 环形阴影：`rgb(224,226,232) 0px 0px 0px 1px`

## 6. 深度与抬升
极简 —— 环形阴影 + 柔和表面对比

## 7. 宜与不宜
### 宜
- 功能区块使用柔和的浅/深配色对
- 应用 Roobert PRO 及其 OpenType 字符变体
- 交互元素使用 Blue 450（#5b76fe）
### 不宜
- 不要使用重阴影
- 每个区块不要混用超过 2 种柔和点缀色

## 8. 响应式行为
断点：425px, 576px, 768px, 896px, 1024px, 1200px, 1280px, 1366px, 1700px, 1920px

## 9. Agent 提示词指南
### 快速颜色参考
- 文字：Near Black（`#1c1c1e`）
- 背景：白色（`#ffffff`）
- 交互：Blue 450（`#5b76fe`）
- 成功：`#00b473`
- 边框：`#c7cad5`
### 组件示例提示词
- "创建 hero：白色背景。Roobert PRO Medium 56px，行高 1.15，字距 -1.68px。蓝色 CTA（#5b76fe）。描边次要按钮（1px solid #c7cad5，8px 圆角）。"
