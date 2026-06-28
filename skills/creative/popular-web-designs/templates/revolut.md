# 设计系统：Revolut


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

Revolut 的网站是金融科技自信的像素化凝聚 —— 一套通过巨型字体、慷慨留白和克制的中性配色，传达「你的钱掌握在可靠之手」的设计系统。视觉语言构建在 Aeonik Pro 之上，这是一款几何无衬线字体，在 136px 字号、字重 500 和激进负字距（-2.72px）下打造出广告牌级别的标题。这不是含蓄的品牌塑造；这是体育场规模的金融科技。

配色系统建立在全面的 `--rui-*`（Revolut UI）token 架构之上，为每个状态使用语义化命名：danger（`#e23b4a`）、warning（`#ec7e00`）、teal（`#00a87e`）、blue（`#494fdf`）、deep-pink（`#e61e49`）等等。但营销页面本身却异常克制 —— 近黑（`#191c1f`）和纯白（`#ffffff`）占主导，丰富多彩的语义 token 留给产品界面，而非营销页。

Revolut 的独特之处在于其「万物皆药丸」的按钮系统。每个按钮都使用 9999px 圆角 —— 主深色（`#191c1f`）、次浅色（`#f4f4f4`）、描边（`透明 + 2px solid`）、深色上的幽灵按钮（`rgba(244,244,244,0.1) + 2px solid`）。内边距慷慨（14px 32px–34px），创造出大型、自信的触控目标。结合各种字重的 Inter 正文和正字距（0.16px–0.24px），结果是一个既高端又亲民的设计 —— 现代时代的银行。

**关键特征：**
- Aeonik Pro 展示字体在 136px 字重 500 —— 广告牌级别的金融科技标题
- 近黑（`#191c1f`）+ 白色二元搭配，全面的 `--rui-*` 语义 token
- 全局药丸按钮（9999px 圆角），慷慨内边距（14px 32px）
- Inter 正文配正字距（0.16px–0.24px）
- 丰富的语义配色系统：蓝、青、粉、黄、绿、棕、danger、warning
- 检测到零阴影 —— 深度仅通过颜色对比营造
- 紧凑的展示行高（1.00）搭配放松的正文（1.50–1.56）

## 2. 配色方案与角色

### 主色
- **Revolut Dark**（`#191c1f`）：主要深色表面、按钮背景、近黑文字
- **Pure White**（`#ffffff`）：`--rui-color-action-label`，主要浅色表面
- **Light Surface**（`#f4f4f4`）：次要按钮背景、细微表面

### 品牌 / 交互色
- **Revolut Blue**（`#494fdf`）：`--rui-color-blue`，主要品牌蓝
- **Action Blue**（`#4f55f1`）：`--rui-color-action-photo-header-text`，标题点缀
- **Blue Text**（`#376cd5`）：`--website-color-blue-text`，链接蓝

### 语义色
- **Danger Red**（`#e23b4a`）：`--rui-color-danger`，错误/破坏性
- **Deep Pink**（`#e61e49`）：`--rui-color-deep-pink`，关键点缀
- **Warning Orange**（`#ec7e00`）：`--rui-color-warning`，警告状态
- **Yellow**（`#b09000`）：`--rui-color-yellow`，注意
- **Teal**（`#00a87e`）：`--rui-color-teal`，成功/正向
- **Light Green**（`#428619`）：`--rui-color-light-green`，次要成功
- **Green Text**（`#006400`）：`--website-color-green-text`，绿色文字
- **Light Blue**（`#007bc2`）：`--rui-color-light-blue`，信息性
- **Brown**（`#936d62`）：`--rui-color-brown`，温暖中性点缀
- **Red Text**（`#8b0000`）：`--website-color-red-text`，深红文字

### 中性色阶
- **Mid Slate**（`#505a63`）：次要文字
- **Cool Gray**（`#8d969e`）：柔和文字、第三级
- **Gray Tone**（`#c9c9cd`）：`--rui-color-grey-tone-20`，边框/分割线

## 3. 字体规则

### 字体家族
- **展示字体**：`Aeonik Pro` —— 几何无衬线，未检测到回退
- **正文 / UI**：`Inter` —— 标准系统无衬线
- **回退**：特定按钮语境使用 `Arial`

### 层级

| 角色 | 字体 | 字号 | 字重 | 行高 | 字距 | 备注 |
|------|------|------|--------|-------------|----------------|-------|
| Display Mega | Aeonik Pro | 136px (8.50rem) | 500 | 1.00（紧凑） | -2.72px | 体育场规模 hero |
| Display Hero | Aeonik Pro | 80px (5.00rem) | 500 | 1.00（紧凑） | -0.8px | 主要 hero |
| Section Heading | Aeonik Pro | 48px (3.00rem) | 500 | 1.21（紧凑） | -0.48px | 功能区块 |
| Sub-heading | Aeonik Pro | 40px (2.50rem) | 500 | 1.20（紧凑） | -0.4px | 子区块 |
| Card Title | Aeonik Pro | 32px (2.00rem) | 500 | 1.19（紧凑） | -0.32px | 卡片标题 |
| Feature Title | Aeonik Pro | 24px (1.50rem) | 400 | 1.33 | normal | 轻量标题 |
| Nav / UI | Aeonik Pro | 20px (1.25rem) | 500 | 1.40 | normal | 导航、按钮 |
| Body Large | Inter | 18px (1.13rem) | 400 | 1.56 | -0.09px | 引言段落 |
| Body | Inter | 16px (1.00rem) | 400 | 1.50 | 0.24px | 标准阅读 |
| Body Semibold | Inter | 16px (1.00rem) | 600 | 1.50 | 0.16px | 强调正文 |
| Body Bold Link | Inter | 16px (1.00rem) | 700 | 1.50 | 0.24px | 粗体链接 |

### 原则
- **字重 500 作为展示默认值**：Aeonik Pro 所有标题都使用 medium（500）—— 不加粗。这通过字号和字距而非字重来建立权威感。
- **广告牌字距**：136px 时 -2.72px 极其压缩 —— 文字设计为一眼可读，如同机场标识。
- **正文正字距**：Inter 使用 +0.16px 到 +0.24px，创造出通透、间隔良好的阅读文字，与压缩的标题形成对比。

## 4. 组件样式

### 按钮

**主深色药丸**
- 背景：`#191c1f`
- 文字：`#ffffff`
- 内边距：14px 32px
- 圆角：9999px（全药丸）
- 悬停：不透明度 0.85
- 聚焦：`0 0 0 0.125rem` 环

**次要浅色药丸**
- 背景：`#f4f4f4`
- 文字：`#000000`
- 内边距：14px 34px
- 圆角：9999px
- 悬停：不透明度 0.85

**描边药丸**
- 背景：透明
- 文字：`#191c1f`
- 边框：`2px solid #191c1f`
- 内边距：14px 32px
- 圆角：9999px

**深色上的幽灵**
- 背景：`rgba(244, 244, 244, 0.1)`
- 文字：`#f4f4f4`
- 边框：`2px solid #f4f4f4`
- 内边距：14px 32px
- 圆角：9999px

### 卡片与容器
- 圆角：12px（小）、20px（卡片）
- 无阴影 —— 扁平表面靠颜色对比
- 深色与浅色区块交替

### 导航
- Aeonik Pro 20px 字重 500
- 干净的页眉，12px 圆角的汉堡菜单切换
- 药丸 CTA 右对齐

## 5. 布局原则

### 间距系统
- 基准单位：8px
- 比例：4px, 6px, 8px, 14px, 16px, 20px, 24px, 32px, 40px, 48px, 80px, 88px, 120px
- 大区块间距：80px–120px

### 边框圆角比例
- 标准（12px）：导航、小按钮
- 卡片（20px）：功能卡片
- 药丸（9999px）：所有按钮

## 6. 深度与抬升

| 级别 | 处理 | 用途 |
|-------|-----------|-----|
| 扁平（Level 0） | 无阴影 | 万物 —— Revolut 使用零阴影 |
| 聚焦 | `0 0 0 0.125rem` 环 | 无障碍聚焦 |

**阴影哲学**：Revolut 使用零阴影。深度完全来自深/浅区块对比以及元素之间慷慨的留白。

## 7. 宜与不宜

### 宜
- 所有展示标题使用 Aeonik Pro 字重 500
- 所有按钮应用 9999px 圆角 —— 药丸形是通用的
- 使用慷慨的按钮内边距（14px 32px）
- 营销表面保持近黑 + 白色配色
- Inter 正文文字应用正字距

### 不宜
- 不要使用阴影 —— Revolut 设计上就是扁平的
- Aeonik Pro 标题不要加粗（700）—— 500 才是字重
- 不要使用小按钮 —— 慷慨的内边距是有意为之
- 不要将语义色应用到营销表面 —— 它们是给产品用的

## 8. 响应式行为

### 断点
| 名称 | 宽度 | 关键变化 |
|------|-------|-------------|
| Mobile Small | <400px | 紧凑，单列 |
| Mobile | 400–720px | 标准移动端 |
| Tablet | 720–1024px | 2 列布局 |
| Desktop | 1024–1280px | 标准桌面 |
| Large | 1280–1920px | 完整布局 |

## 9. Agent 提示词指南

### 快速颜色参考
- 深色：Revolut Dark（`#191c1f`）
- 浅色：白色（`#ffffff`）
- 表面：浅色（`#f4f4f4`）
- 蓝色：Revolut Blue（`#494fdf`）
- 危险：红色（`#e23b4a`）
- 成功：青色（`#00a87e`）

### 组件示例提示词
- "创建 hero：白色背景。标题 136px Aeonik Pro 字重 500，行高 1.00，字距 -2.72px，#191c1f 文字。深色药丸 CTA（#191c1f，9999px，14px 32px）。描边药丸次要按钮（透明，2px solid #191c1f）。"
- "构建药丸按钮：#191c1f 背景，白色文字，9999px 圆角，14px 32px 内边距，20px Aeonik Pro 字重 500。悬停：不透明度 0.85。"

### 迭代指南
1. 标题使用 Aeonik Pro 500 —— 绝不加粗
2. 所有按钮都是药丸形（9999px），带慷慨内边距
3. 零阴影 —— 扁平就是 Revolut 的标识
4. 营销用近黑 + 白色，产品用语义色
