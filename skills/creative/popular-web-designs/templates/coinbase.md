# 设计系统：Coinbase


> **Hermes Agent — 实现说明**
>
> 原站点使用专有字体。对于自包含的 HTML 输出，请使用以下 CDN 替代字体：
> - **主字体：** `DM Sans` | **等宽字体：** `system monospace stack`
> - **字体栈（CSS）：** `font-family: 'DM Sans', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;`
> - **等宽字体栈（CSS）：** `font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;`
> ```html
> <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,100..1000;1,9..40,100..1000&display=swap" rel="stylesheet">
> ```
> 使用 `write_file` 创建 HTML，通过 `generative-widgets` 技能（cloudflared 隧道）提供服务。
> 生成后用 `browser_vision` 验证视觉准确性。

## 1. 视觉主题与氛围

Coinbase 的网站是一个干净、可信赖的加密平台，通过蓝白二元配色传达出金融可靠性。设计使用 Coinbase 蓝（`#0052ff`）—— 一种深沉、饱和的蓝色 —— 作为白色和近黑表面上唯一的品牌点缀色。专有字体家族包括用于英雄标题的 CoinbaseDisplay、用于 UI 文字的 CoinbaseSans、用于正文阅读的 CoinbaseText，以及用于图标体系的 CoinbaseIcons —— 一套完整的四字体系统。

按钮系统使用独特的 56px 圆角，打造药丸形 CTA，悬停时过渡到更浅的蓝色（`#578bfa`）。设计在白色内容区块和深色（`#0a0b0d`、`#282b31`）功能区之间交替，营造出专业、金融级的界面。

**关键特征：**
- Coinbase 蓝（`#0052ff`）作为唯一的品牌点缀色
- 四字体专有家族：Display、Sans、Text、Icons
- 56px 圆角药丸按钮，带蓝色悬停过渡
- 近黑（`#0a0b0d`）深色区块 + 白色浅色区块
- 展示标题使用 1.00 行高 —— 超紧凑
- 冷灰色次要表面（`#eef0f3`）带蓝色调
- 部分按钮标签使用 `text-transform: lowercase` —— 不常见

## 2. 配色方案与角色

### 主色
- **Coinbase Blue**（`#0052ff`）：主要品牌色、链接、CTA 边框
- **Pure White**（`#ffffff`）：主要浅色表面
- **Near Black**（`#0a0b0d`）：文字、深色区块背景
- **Cool Gray Surface**（`#eef0f3`）：次要按钮背景

### 交互色
- **Hover Blue**（`#578bfa`）：按钮悬停背景
- **Link Blue**（`#0667d0`）：次要链接颜色
- **Muted Blue**（`#5b616e`）：20% 不透明度的边框色

### 表面色
- **Dark Card**（`#282b31`）：深色按钮/卡片背景
- **Light Surface**（`rgba(247,247,247,0.88)`）：细微表面

## 3. 字体规则

### 字体家族
- **Display**：`CoinbaseDisplay` —— 英雄标题
- **UI / Sans**：`CoinbaseSans` —— 按钮、标题、导航
- **Body**：`CoinbaseText` —— 阅读文字
- **Icons**：`CoinbaseIcons` —— 图标字体

### 层级

| 角色 | 字体 | 字号 | 字重 | 行高 | 备注 |
|------|------|------|--------|-------------|-------|
| Display Hero | CoinbaseDisplay | 80px | 400 | 1.00（紧凑） | 最大冲击力 |
| Display Secondary | CoinbaseDisplay | 64px | 400 | 1.00 | 次级英雄 |
| Display Third | CoinbaseDisplay | 52px | 400 | 1.00 | 第三层级 |
| Section Heading | CoinbaseSans | 36px | 400 | 1.11（紧凑） | 功能区块 |
| Card Title | CoinbaseSans | 32px | 400 | 1.13 | 卡片标题 |
| Feature Title | CoinbaseSans | 18px | 600 | 1.33 | 功能强调 |
| Body Bold | CoinbaseSans | 16px | 700 | 1.50 | 强壮正文 |
| Body Semibold | CoinbaseSans | 16px | 600 | 1.25 | 按钮、导航 |
| Body | CoinbaseText | 18px | 400 | 1.56 | 标准阅读 |
| Body Small | CoinbaseText | 16px | 400 | 1.50 | 次要阅读 |
| Button | CoinbaseSans | 16px | 600 | 1.20 | +0.16px 字距 |
| Caption | CoinbaseSans | 14px | 600–700 | 1.50 | 元数据 |
| Small | CoinbaseSans | 13px | 600 | 1.23 | 标签 |

## 4. 组件样式

### 按钮

**主药丸（56px 圆角）**
- 背景：`#eef0f3` 或 `#282b31`
- 圆角：56px
- 边框：`1px solid` 与背景同色
- 悬停：`#578bfa`（浅蓝）
- 聚焦：`2px solid black` 轮廓

**全药丸（100000px 圆角）**
- 用于最大化的药丸形状

**蓝色描边**
- 边框：`1px solid #0052ff`
- 背景：透明

### 卡片与容器
- 圆角：8px–40px 范围
- 边框：`1px solid rgba(91,97,110,0.2)`

## 5. 布局原则

### 间距系统
- 基准：8px
- 比例：1px, 3px, 4px, 5px, 6px, 8px, 10px, 12px, 15px, 16px, 20px, 24px, 25px, 32px, 48px

### 圆角比例
- 小（4px–8px）：文章链接、小卡片
- 标准（12px–16px）：卡片、菜单
- 大（24px–32px）：功能容器
- 超大（40px）：大型按钮/容器
- 药丸（56px）：主 CTA
- 全圆（100000px）：最大化药丸

## 6. 深度与抬升

极简阴影系统 —— 深度来自深/浅区块之间的颜色对比。

## 7. 宜与不宜

### 宜
- 主要交互元素使用 Coinbase 蓝（#0052ff）
- 所有 CTA 按钮应用 56px 圆角
- 仅在英雄标题上使用 CoinbaseDisplay
- 深色（#0a0b0d）与白色区块交替

### 不宜
- 不要将蓝色用作装饰 —— 它仅承担功能性
- CTA 不要使用尖角 —— 至少 56px 圆角

## 8. 响应式行为

断点：400px, 576px, 640px, 768px, 896px, 1280px, 1440px, 1600px

## 9. Agent 提示词指南

### 快速颜色参考
- 品牌：Coinbase 蓝（`#0052ff`）
- 背景：白色（`#ffffff`）
- 深色表面：`#0a0b0d`
- 次要表面：`#eef0f3`
- 悬停：`#578bfa`
- 文字：`#0a0b0d`

### 组件示例提示词
- "创建 hero：白色背景。CoinbaseDisplay 80px，行高 1.00。药丸 CTA（#eef0f3，56px 圆角）。悬停：#578bfa。"
- "构建深色区块：#0a0b0d 背景。CoinbaseDisplay 64px 白色文字。蓝色点缀链接（#0052ff）。"
