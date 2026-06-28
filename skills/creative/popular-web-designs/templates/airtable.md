# 设计系统：Airtable


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

Airtable 的网站是一个干净、企业友好的平台，通过白色画布、深海军蓝文字（`#181d26`）和 Airtable 蓝（`#1b61c9`）作为主要交互点缀色，传达出「精致的简约」。Haas 字体家族（display + text 变体）创造了一套瑞士精度般的字体系统，正文通篇使用正字距。

**关键特征：**
- 白色画布搭配深海军蓝文字（`#181d26`）
- Airtable 蓝（`#1b61c9`）作为主要 CTA 和链接颜色
- Haas + Haas Groot Disp 双字体系统
- 正文字使用正字距（0.08px–0.28px）
- 12px 圆角按钮，16px–32px 用于卡片
- 多层蓝色调阴影：`rgba(45,127,249,0.28) 0px 1px 3px`
- 语义化主题 token：`--theme_*` CSS 变量命名

## 2. 配色方案与角色

### 主色
- **Deep Navy**（`#181d26`）：主要文字
- **Airtable Blue**（`#1b61c9`）：CTA 按钮、链接
- **White**（`#ffffff`）：主要表面
- **Spotlight**（`rgba(249,252,255,0.97)`）：`--theme_button-text-spotlight`

### 语义色
- **Success Green**（`#006400`）：`--theme_success-text`
- **Weak Text**（`rgba(4,14,32,0.69)`）：`--theme_text-weak`
- **Secondary Active**（`rgba(7,12,20,0.82)`）：`--theme_button-text-secondary-active`

### 中性色
- **Dark Gray**（`#333333`）：次要文字
- **Mid Blue**（`#254fad`）：链接/蓝色变体点缀
- **Border**（`#e0e2e6`）：卡片边框
- **Light Surface**（`#f8fafc`）：细微表面

### 阴影
- **蓝色调**（`rgba(0,0,0,0.32) 0px 0px 1px, rgba(0,0,0,0.08) 0px 0px 2px, rgba(45,127,249,0.28) 0px 1px 3px, rgba(0,0,0,0.06) 0px 0px 0px 0.5px inset`）
- **柔和**（`rgba(15,48,106,0.05) 0px 0px 20px`）

## 3. 字体规则

### 字体家族
- **主字体**：`Haas`，回退：`-apple-system, system-ui, Segoe UI, Roboto`
- **展示字体**：`Haas Groot Disp`，回退：`Haas`

### 层级

| 角色 | 字体 | 字号 | 字重 | 行高 | 字距 |
|------|------|------|--------|-------------|----------------|
| Display Hero | Haas | 48px | 400 | 1.15 | normal |
| Display Bold | Haas Groot Disp | 48px | 900 | 1.50 | normal |
| Section Heading | Haas | 40px | 400 | 1.25 | normal |
| Sub-heading | Haas | 32px | 400–500 | 1.15–1.25 | normal |
| Card Title | Haas | 24px | 400 | 1.20–1.30 | 0.12px |
| Feature | Haas | 20px | 400 | 1.25–1.50 | 0.1px |
| Body | Haas | 18px | 400 | 1.35 | 0.18px |
| Body Medium | Haas | 16px | 500 | 1.30 | 0.08–0.16px |
| Button | Haas | 16px | 500 | 1.25–1.30 | 0.08px |
| Caption | Haas | 14px | 400–500 | 1.25–1.35 | 0.07–0.28px |

## 4. 组件样式

### 按钮
- **主蓝色**：`#1b61c9`，白色文字，16px 24px 内边距，12px 圆角
- **白色**：白色背景，`#181d26` 文字，12px 圆角，1px 白色边框
- **Cookie 同意条**：`#1b61c9` 背景，2px 圆角（尖锐）

### 卡片：`1px solid #e0e2e6`，16px–24px 圆角
### 输入框：标准 Haas 样式

## 5. 布局
- 间距：1–48px（8px 基准）
- 圆角：2px（小）、12px（按钮）、16px（卡片）、24px（区块）、32px（大）、50%（圆形）

## 6. 深度
- 蓝色调多层阴影系统
- 柔和环境光：`rgba(15,48,106,0.05) 0px 0px 20px`

## 7. 宜与不宜
### 宜：CTA 使用 Airtable 蓝，Haas 配正字距，12px 圆角按钮
### 不宜：跳过正字距，使用过重的阴影

## 8. 响应式行为
断点：425–1664px（23 个断点）

## 9. Agent 提示词指南
- 文字：Deep Navy（`#181d26`）
- CTA：Airtable 蓝（`#1b61c9`）
- 背景：白色（`#ffffff`）
- 边框：`#e0e2e6`
