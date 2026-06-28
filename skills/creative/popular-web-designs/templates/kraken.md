# 设计系统：Kraken


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

Kraken 的网站是一个干净、可信赖的加密交易所，使用紫色作为其统领性的品牌色。设计运行在白色背景上，Kraken 紫（`#7132f5`、`#5741d8`、`#5b1ecf`）创造出独特、专业的加密身份。专有的 Kraken-Brand 字体处理展示标题，使用粗体（700）字重和负字距，而 Kraken-Product（以 IBM Plex Sans 作为回退）则作为 UI 的主力字体。

**关键特征：**
- Kraken 紫（`#7132f5`）作为主品牌色，带更深的变体（`#5741d8`、`#5b1ecf`）
- Kraken-Brand（展示）+ Kraken-Product（UI）双字体系统
- 近黑（`#101114`）文字搭配冷蓝灰色中性色阶
- 12px 圆角按钮（圆润但非药丸形）
- 细微阴影（`rgba(0,0,0,0.03) 0px 4px 24px`）—— 耳语级别
- 绿色点缀（`#149e61`）用于正向/成功状态

## 2. 配色方案与角色

### 主色
- **Kraken Purple**（`#7132f5`）：主要 CTA、品牌点缀、链接
- **Purple Dark**（`#5741d8`）：按钮边框、描边变体
- **Purple Deep**（`#5b1ecf`）：最深的紫色
- **Purple Subtle**（`rgba(133,91,251,0.16)`）：16% 不透明度的紫色 —— 细微按钮背景
- **Near Black**（`#101114`）：主要文字

### 中性色
- **Cool Gray**（`#686b82`）：主要中性色，24% 不透明度作为边框
- **Silver Blue**（`#9497a9`）：次要文字、柔和元素
- **White**（`#ffffff`）：主要表面
- **Border Gray**（`#dedee5`）：分割线边框

### 语义色
- **Green**（`#149e61`）：成功/正向，16% 不透明度用于徽章
- **Green Dark**（`#026b3f`）：徽章文字

## 3. 字体规则

### 字体家族
- **展示字体**：`Kraken-Brand`，回退：`IBM Plex Sans, Helvetica, Arial`
- **UI / 正文字体**：`Kraken-Product`，回退：`Helvetica Neue, Helvetica, Arial`

### 层级

| 角色 | 字体 | 字号 | 字重 | 行高 | 字距 |
|------|------|------|--------|-------------|----------------|
| Display Hero | Kraken-Brand | 48px | 700 | 1.17 | -1px |
| Section Heading | Kraken-Brand | 36px | 700 | 1.22 | -0.5px |
| Sub-heading | Kraken-Brand | 28px | 700 | 1.29 | -0.5px |
| Feature Title | Kraken-Product | 22px | 600 | 1.20 | normal |
| Body | Kraken-Product | 16px | 400 | 1.38 | normal |
| Body Medium | Kraken-Product | 16px | 500 | 1.38 | normal |
| Button | Kraken-Product | 16px | 500–600 | 1.38 | normal |
| Caption | Kraken-Product | 14px | 400–700 | 1.43–1.71 | normal |
| Small | Kraken-Product | 12px | 400–500 | 1.33 | normal |
| Micro | Kraken-Product | 7px | 500 | 1.00 | 大写 |

## 4. 组件样式

### 按钮

**主紫色**
- 背景：`#7132f5`
- 文字：`#ffffff`
- 内边距：13px 16px
- 圆角：12px

**紫色描边**
- 背景：`#ffffff`
- 文字：`#5741d8`
- 边框：`1px solid #5741d8`
- 圆角：12px

**紫色细微**
- 背景：`rgba(133,91,251,0.16)`
- 文字：`#7132f5`
- 内边距：8px
- 圆角：12px

**白色按钮**
- 背景：`#ffffff`
- 文字：`#101114`
- 圆角：10px
- 阴影：`rgba(0,0,0,0.03) 0px 4px 24px`

**次要灰色**
- 背景：`rgba(148,151,169,0.08)`
- 文字：`#101114`
- 圆角：12px

### 徽章
- 成功：`rgba(20,158,97,0.16)` 背景，`#026b3f` 文字，6px 圆角
- 中性：`rgba(104,107,130,0.12)` 背景，`#484b5e` 文字，8px 圆角

## 5. 布局原则

### 间距：1px, 2px, 3px, 4px, 5px, 6px, 8px, 10px, 12px, 13px, 15px, 16px, 20px, 24px, 25px
### 边框圆角：3px, 6px, 8px, 10px, 12px, 16px, 9999px, 50%

## 6. 深度与抬升
- 细微：`rgba(0,0,0,0.03) 0px 4px 24px`
- 微型：`rgba(16,24,40,0.04) 0px 1px 4px`

## 7. 宜与不宜

### 宜
- CTA 和链接使用 Kraken 紫（#7132f5）
- 所有按钮应用 12px 圆角
- 标题用 Kraken-Brand，正文用 Kraken-Product

### 不宜
- 不要使用药丸按钮 —— 12px 是按钮的最大圆角
- 不要使用既定色阶之外的紫色

## 8. 响应式行为
断点：375px, 425px, 640px, 768px, 1024px, 1280px, 1536px

## 9. Agent 提示词指南

### 快速颜色参考
- 品牌：Kraken 紫（`#7132f5`）
- 深色变体：`#5741d8`
- 文字：Near Black（`#101114`）
- 次要文字：`#9497a9`
- 背景：白色（`#ffffff`）

### 组件示例提示词
- "创建 hero：白色背景。Kraken-Brand 48px 字重 700，字距 -1px。紫色 CTA（#7132f5，12px 圆角，13px 16px 内边距）。"
