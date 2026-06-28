# 设计系统：Wise


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

Wise 的网站是一个大胆、自信的金融科技平台，通过巨型字体和独特的青柠绿点缀，传达出「无国界的金钱」。设计运行在温暖的灰白画布上，搭配近黑文字（`#0e0f0c`）和标志性的 Wise 绿（`#9fe870`）—— 一种清新、青柠般明亮的颜色，感觉鲜活而乐观，与传统银行的企业蓝截然不同。

字体使用 Wise Sans —— 一种专有字体，在展示标题上使用极致的字重 900（黑色），并搭配极为紧凑的 0.85 行高和 OpenType `"calt"`（上下文替换）。在 126px 时，文字密集得像一张抗议标语 —— 大胆、急切、无法忽视。Inter 作为正文字体，默认用字重 600 强调，创造出一贯自信的嗓音。

Wise 的独特之处在于其绿-白-黑的材质配色。青柠绿（`#9fe870`）出现在按钮上，搭配深绿文字（`#163300`），打造出受自然启发的 CTA，感觉清新。悬停状态使用 `scale(1.05)` 放大而非颜色变化 —— 按钮在交互时物理生长。圆角系统按钮使用 9999px（药丸形）、卡片使用 30px–40px，阴影系统极简 —— 仅有 `rgba(14,15,12,0.12) 0px 0px 0px 1px` 环形阴影。

**关键特征：**
- Wise Sans 字重 900，0.85 行高 —— 广告牌级别的粗体标题
- 青柠绿（`#9fe870`）点缀，配深绿文字（`#163300`）—— 受自然启发的金融科技
- Inter 正文默认字重 600 —— 自信，不轻巧
- 近黑（`#0e0f0c`）主色，带温暖绿色底色
- Scale(1.05) 悬停动画 —— 按钮物理生长
- 所有文字启用 OpenType `"calt"`
- 药丸按钮（9999px）和大圆角卡片（30px–40px）
- 语义配色系统，带全面的状态管理

## 2. 配色方案与角色

### 主品牌色
- **Near Black**（`#0e0f0c`）：主要文字、深色区块背景
- **Wise Green**（`#9fe870`）：主要 CTA 按钮、品牌点缀色
- **Dark Green**（`#163300`）：绿色上的按钮文字、深绿点缀
- **Light Mint**（`#e2f6d5`）：柔和绿色表面、徽章背景
- **Pastel Green**（`#cdffad`）：`--color-interactive-contrast-hover`，悬停点缀

### 语义色
- **Positive Green**（`#054d28`）：`--color-sentiment-positive-primary`，成功
- **Danger Red**（`#d03238`）：`--color-interactive-negative-hover`，错误/破坏性
- **Warning Yellow**（`#ffd11a`）：`--color-sentiment-warning-hover`，警告
- **Background Cyan**（`rgba(56,200,255,0.10)`）：`--color-background-accent`，信息色调
- **Bright Orange**（`#ffc091`）：`--color-bright-orange`，温暖点缀

### 中性色
- **Warm Dark**（`#454745`）：次要文字、边框
- **Gray**（`#868685`）：柔和文字、第三级
- **Light Surface**（`#e8ebe6`）：带绿色调的细微浅色表面

## 3. 字体规则

### 字体家族
- **展示字体**：`Wise Sans`，回退：`Inter` —— 所有文字启用 OpenType `"calt"`
- **正文 / UI**：`Inter`，回退：`Helvetica, Arial`

### 层级

| 角色 | 字体 | 字号 | 字重 | 行高 | 字距 | 备注 |
|------|------|------|--------|-------------|----------------|-------|
| Display Mega | Wise Sans | 126px (7.88rem) | 900 | 0.85（超紧凑） | normal | `"calt"` |
| Display Hero | Wise Sans | 96px (6.00rem) | 900 | 0.85 | normal | `"calt"` |
| Section Heading | Wise Sans | 64px (4.00rem) | 900 | 0.85 | normal | `"calt"` |
| Sub-heading | Wise Sans | 40px (2.50rem) | 900 | 0.85 | normal | `"calt"` |
| Alt Heading | Inter | 78px (4.88rem) | 600 | 1.10（紧凑） | -2.34px | `"calt"` |
| Card Title | Inter | 26px (1.62rem) | 600 | 1.23（紧凑） | -0.39px | `"calt"` |
| Feature Title | Inter | 22px (1.38rem) | 600 | 1.25（紧凑） | -0.396px | `"calt"` |
| Body | Inter | 18px (1.13rem) | 400 | 1.44 | 0.18px | `"calt"` |
| Body Semibold | Inter | 18px (1.13rem) | 600 | 1.44 | -0.108px | `"calt"` |
| Button | Inter | 18px–22px | 600 | 1.00–1.44 | -0.108px | `"calt"` |
| Caption | Inter | 14px (0.88rem) | 400–600 | 1.50–1.86 | -0.084px to -0.108px | `"calt"` |
| Small | Inter | 12px (0.75rem) | 400–600 | 1.00–2.17 | -0.084px to -0.108px | `"calt"` |

### 原则
- **字重 900 作为标识**：Wise Sans Black（900）专用于展示 —— 所有分析过的系统里最重的字重。它创造出感觉像盖章、压印、实体般的文字。
- **0.85 行高**：分析过的最紧凑的展示行高。字母垂直方向重叠，创造出密集、广告牌式的文字块。
- **"calt" 处处启用**：上下文替换在所有文字上启用 —— 无论 Wise Sans 还是 Inter。
- **字重 600 作为正文默认值**：Inter Semibold 是标准阅读字重 —— 自信，不轻巧。

## 4. 组件样式

### 按钮

**主绿色药丸**
- 背景：`#9fe870`（Wise 绿）
- 文字：`#163300`（深绿）
- 内边距：5px 16px
- 圆角：9999px
- 悬停：scale(1.05) —— 按钮物理生长
- 激活：scale(0.95) —— 按钮压缩
- 聚焦：内嵌环 + 轮廓

**次要细微药丸**
- 背景：`rgba(22, 51, 0, 0.08)`（深绿 8% 不透明度）
- 文字：`#0e0f0c`
- 内边距：8px 12px 8px 16px
- 圆角：9999px
- 相同的 scale 悬停/激活行为

### 卡片与容器
- 圆角：16px（小）、30px（中）、40px（大卡片/表格）
- 边框：`1px solid rgba(14,15,12,0.12)` 或 `1px solid #9fe870`（绿色点缀）
- 阴影：`rgba(14,15,12,0.12) 0px 0px 0px 1px`（环形阴影）

### 导航
- 带绿色调的导航悬停：`rgba(211,242,192,0.4)`
- 干净的页眉，带 Wise 字标
- 药丸 CTA 右对齐

## 5. 布局原则

### 间距系统
- 基准单位：8px
- 比例：1px, 2px, 3px, 4px, 5px, 8px, 10px, 11px, 12px, 16px, 18px, 19px, 20px, 22px, 24px

### 边框圆角比例
- 极小（2px）：链接、输入框
- 标准（10px）：组合框、输入框
- 卡片（16px）：小卡片、按钮、单选框
- 中（20px）：链接、中等卡片
- 大（30px）：功能卡片
- 区块（40px）：表格、大卡片
- 超大（1000px）：展示型元素
- 药丸（9999px）：所有按钮、图片
- 圆形（50%）：图标、徽章

## 6. 深度与抬升

| 级别 | 处理 | 用途 |
|-------|-----------|-----|
| 扁平（Level 0） | 无阴影 | 默认 |
| 环形（Level 1） | `rgba(14,15,12,0.12) 0px 0px 0px 1px` | 卡片边框 |
| 内嵌（Level 2） | `rgb(134,134,133) 0px 0px 0px 1px inset` | 输入框聚焦 |

**阴影哲学**：Wise 使用极简阴影 —— 仅环形阴影。深度来自中性画布上的粗体绿色点缀对比。

## 7. 宜与不宜

### 宜
- 展示使用 Wise Sans 字重 900 —— 极致的粗体就是品牌
- Wise Sans 展示应用 0.85 行高 —— 超紧凑是有意为之
- 主要 CTA 使用青柠绿（#9fe870）配深绿（#163300）文字
- 按钮应用 scale(1.05) 悬停和 scale(0.95) 激活
- 所有文字启用 "calt"
- 正文默认使用 Inter 字重 600

### 不宜
- 不要对 Wise Sans 使用细字重 —— 仅限 900
- 不要放宽展示的 0.85 行高 —— 密度就是标识
- 不要把 Wise 绿用作大表面背景 —— 它是给按钮和点缀用的
- 不要跳过按钮的 scale 动画
- 不要使用传统阴影 —— 仅环形阴影

## 8. 响应式行为

### 断点
| 名称 | 宽度 | 关键变化 |
|------|-------|-------------|
| Mobile | <576px | 单列 |
| Tablet | 576–992px | 2 列 |
| Desktop | 992–1440px | 完整布局 |
| Large | >1440px | 扩展 |

## 9. Agent 提示词指南

### 快速颜色参考
- 文字：Near Black（`#0e0f0c`）
- 背景：白色（`#ffffff` / 灰白）
- 点缀：Wise 绿（`#9fe870`）
- 按钮文字：深绿（`#163300`）
- 次要色：灰色（`#868685`）

### 组件示例提示词
- "创建 hero：白色背景。标题 96px Wise Sans 字重 900，行高 0.85，启用 'calt'，#0e0f0c 文字。绿色药丸 CTA（#9fe870，9999px 圆角，5px 16px 内边距，#163300 文字）。悬停：scale(1.05)。"
- "构建卡片：30px 圆角，1px solid rgba(14,15,12,0.12)。标题 22px Inter 字重 600，正文 18px 字重 400。"

### 迭代指南
1. Wise Sans 900 配 0.85 行高 —— 极致字重就是品牌
2. 青柠绿仅用于按钮 —— 绿色背景上配深绿文字
3. 所有交互元素使用 scale 动画（1.05 悬停，0.95 激活）
4. 万物启用 "calt" —— 上下文替换是强制性的
5. Inter 600 用于正文 —— 自信的阅读字重
