# 设计系统：Webflow


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

Webflow 的网站是一个视觉丰富、以工具为导向的平台，通过干净的白色表面、标志性的 Webflow 蓝（`#146ef5`）以及丰富的次要配色（紫、粉、绿、橙、黄、红），传达出「无代码设计」。定制的 WF Visual Sans Variable 字体打造出一套自信、精准的字体系统，展示用字重 600，正文用字重 500。

**关键特征：**
- 白色画布搭配近黑（`#080808`）文字
- Webflow 蓝（`#146ef5`）作为主要品牌色 + 交互色
- WF Visual Sans Variable —— 定制可变字体，字重 500–600
- 丰富的次要配色：紫 `#7a3dff`、粉 `#ed52cb`、绿 `#00d722`、橙 `#ff6b00`、黄 `#ffae13`、红 `#ee1d36`
- 保守的 4px–8px 边框圆角 —— 锋利，不圆润
- 多层阴影堆栈（5 层级联阴影）
- 大写标签：10px–15px，字重 500–600，宽字距（0.6px–1.5px）
- 按钮悬停使用 translate(6px) 动画

## 2. 配色方案与角色

### 主色
- **Near Black**（`#080808`）：主要文字
- **Webflow Blue**（`#146ef5`）：`--_color---primary--webflow-blue`，主要 CTA 和链接
- **Blue 400**（`#3b89ff`）：`--_color---primary--blue-400`，较浅的交互蓝
- **Blue 300**（`#006acc`）：`--_color---blue-300`，较深的蓝色变体
- **Button Hover Blue**（`#0055d4`）：`--mkto-embed-color-button-hover`

### 次要点缀色
- **Purple**（`#7a3dff`）：`--_color---secondary--purple`
- **Pink**（`#ed52cb`）：`--_color---secondary--pink`
- **Green**（`#00d722`）：`--_color---secondary--green`
- **Orange**（`#ff6b00`）：`--_color---secondary--orange`
- **Yellow**（`#ffae13`）：`--_color---secondary--yellow`
- **Red**（`#ee1d36`）：`--_color---secondary--red`

### 中性色
- **Gray 800**（`#222222`）：深色次要文字
- **Gray 700**（`#363636`）：中等文字
- **Gray 300**（`#ababab`）：柔和文字、占位符
- **Mid Gray**（`#5a5a5a`）：链接文字
- **Border Gray**（`#d8d8d8`）：边框、分割线
- **Border Hover**（`#898989`）：悬停边框

### 阴影
- **5 层级联**：`rgba(0,0,0,0) 0px 84px 24px, rgba(0,0,0,0.01) 0px 54px 22px, rgba(0,0,0,0.04) 0px 30px 18px, rgba(0,0,0,0.08) 0px 13px 13px, rgba(0,0,0,0.09) 0px 3px 7px`

## 3. 字体规则

### 字体：`WF Visual Sans Variable`，回退：`Arial`

| 角色 | 字号 | 字重 | 行高 | 字距 | 备注 |
|------|------|--------|-------------|----------------|-------|
| Display Hero | 80px | 600 | 1.04 | -0.8px | |
| Section Heading | 56px | 600 | 1.04 | normal | |
| Sub-heading | 32px | 500 | 1.30 | normal | |
| Feature Title | 24px | 500–600 | 1.30 | normal | |
| Body | 20px | 400–500 | 1.40–1.50 | normal | |
| Body Standard | 16px | 400–500 | 1.60 | -0.16px | |
| Button | 16px | 500 | 1.60 | -0.16px | |
| Uppercase Label | 15px | 500 | 1.30 | 1.5px | 大写 |
| Caption | 14px | 400–500 | 1.40–1.60 | normal | |
| Badge Uppercase | 12.8px | 550 | 1.20 | normal | 大写 |
| Micro Uppercase | 10px | 500–600 | 1.30 | 1px | 大写 |
| 代码：Inconsolata（配套等宽字体）

## 4. 组件样式

### 按钮
- 透明：文字 `#080808`，悬停 translate(6px)
- 白色圆形：50% 圆角，白色背景
- 蓝色徽章：`#146ef5` 背景，4px 圆角，字重 550

### 卡片：`1px solid #d8d8d8`，4px–8px 圆角
### 徽章：10% 不透明度的蓝色调背景，4px 圆角

## 5. 布局
- 间距：分数比例（1px, 2.4px, 3.2px, 4px, 5.6px, 6px, 7.2px, 8px, 9.6px, 12px, 16px, 24px）
- 圆角：2px, 4px, 8px, 50% —— 保守、锋利
- 断点：479px, 768px, 992px

## 6. 深度：5 层级联阴影系统

## 7. 宜与不宜
- 宜：WF Visual Sans Variable 使用 500–600 字重。CTA 使用蓝色（#146ef5）。4px 圆角。悬停 translate(6px)。
- 不宜：功能元素圆角不要超过 8px。不要在主要 CTA 上使用次要颜色。

## 8. 响应式：479px, 768px, 992px

## 9. Agent 提示词指南
- 文字：Near Black（`#080808`）
- CTA：Webflow 蓝（`#146ef5`）
- 背景：白色（`#ffffff`）
- 边框：`#d8d8d8`
- 次要色：紫 `#7a3dff`、粉 `#ed52cb`、绿 `#00d722`
