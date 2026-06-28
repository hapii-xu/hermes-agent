---
name: sketch
description: "一次性 HTML 模型：2-3 个设计变体用于对比。"
version: 1.0.0
author: Hermes Agent（改编自 gsd-build/get-shit-done）
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [sketch, mockup, design, ui, prototype, html, variants, exploration, wireframe, comparison]
    related_skills: [spike, claude-design, popular-web-designs, excalidraw]
---

# Sketch

当用户想要在 **commit 到某个方向之前先看到设计方向**时使用此 skill——以一次性 HTML 模型的形式探索一个 UI/UX 想法。目的是生成 2-3 个可交互的变体，让用户可以并排比较视觉方向，而不是产出可发布的代码。

当用户说类似下面这些话时加载此 skill：「sketch this screen」、「show me what X could look like」、「compare layout A vs B」、「give me 2-3 takes on this UI」、「let me see some variants」、「mockup this before I build」。

## 何时不要使用

- 用户想要生产级组件——使用 `claude-design` 或正确地构建它
- 用户想要精致的一次性 HTML 产物（落地页、幻灯片）——`claude-design`
- 用户想要图表——`excalidraw`、`architecture-diagram`
- 设计已经锁定——直接构建即可

## 如果用户安装了完整的 GSD 系统

如果 `gsd-sketch` 作为同级 skill 出现（通过 `npx get-shit-done-cc --hermes` 安装），优先使用 **`gsd-sketch`** 以获得完整工作流：持久化的 `.planning/sketches/` 配 MANIFEST、frontier 模式分析、跨过往 sketch 的一致性审计，以及与 GSD 其余部分的集成。本 skill 是轻量级的独立版本——没有状态机制的一次性 sketch。

## 核心方法

```
intake  →  variants  →  head-to-head  →  pick winner (or iterate)
```

### 1. Intake（如果用户已经给了足够信息则跳过）

在生成变体之前，一次获取一项信息——不要一次问三个问题：

1. **感觉。**「这应该是什么感觉？形容词、情绪、一种氛围。」——*「平静、编辑风，像 Linear」*比*「极简」*告诉你更多。
2. **参考。**「哪些应用、网站或产品捕捉了你想象中的感觉？」——实际的参考胜过抽象的描述。
3. **核心操作。**「用户在这个屏幕上做的最重要的一件事是什么？」——所有变体都应该服务好这件事；如果不能，那它们只是装饰。

在问下一个问题前简要复述每个答案。如果用户已经预先给了全部三个，直接跳到变体阶段。

### 2. Variants（2-3 个，绝不要 1 个，很少 4+）

一次性产出 **2-3 个变体**。每个变体是一个完整的、独立的 HTML 文件。不要描述变体——去构建它们。目的是比较。

每个变体应该采取**不同的设计立场**，而不是不同的像素值。三个好的变体轴：

- **密度：** 紧凑 / 通透 / 超密集（选两个对比鲜明的极端）
- **强调：** 内容优先 / 操作优先 / 工具优先
- **美学：** 编辑风 / 实用主义 / 趣味性
- **布局：** 单列 / 侧边栏 / 分屏
- **基底：** 卡片式 / 裸内容 / 文档式

选一个轴并从中拉开差距。两个只在强调色上不同的变体是浪费——用户分辨不出。

**变体命名：** 描述立场，不要描述数字。

```
sketches/
├── 001-calm-editorial/
│   ├── index.html
│   └── README.md
├── 001-utilitarian-dense/
│   ├── index.html
│   └── README.md
└── 001-playful-split/
    ├── index.html
    └── README.md
```

### 3. 让它们成为真正的 HTML

每个变体是一个**单一的自包含 HTML 文件**：

- 内联 `<style>`——无构建步骤、无外部 CSS
- 系统字体或通过 `<link>` 引入一个 Google Font
- 通过 CDN 引入 Tailwind（`<script src="https://cdn.tailwindcss.com"></script>`）可以
- 逼真的假内容——真实的句子、真实的名字，而非「Lorem ipsum」
- **可交互**：链接可点击、悬停真实、至少有一个状态转换（打开/关闭、筛选、切换）。一张冻结的静态图比一个粗糙的动画版本是更差的 spike。

在浏览器中打开它。如果看起来坏了，在给用户看之前修好它。

**用视觉方式验证变体——使用 Hermes 的浏览器工具。** 不要只是写 HTML 然后祈祷它渲染出来；加载每个变体并查看它：

```
browser_navigate(url="file:///absolute/path/to/sketches/001-calm-editorial/index.html")
browser_vision(question="Does this layout look clean and readable? Any visible bugs (overlapping text, unstyled elements, broken images)?")
```

`browser_vision` 返回页面上实际内容的 AI 描述以及截图路径——能捕获纯源码检查会遗漏的布局 bug（例如静默失败的字体导入、坍塌的 flex 容器）。修复并重新导航，直到每个变体看起来都对。

**默认的 CSS reset + 系统字体栈**，用于快速起步：

```html
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
    color: #1a1a1a;
    background: #fafafa;
    line-height: 1.5;
  }
</style>
```

### 4. 变体 README

每个变体的 `README.md` 回答：

```markdown
## Variant: {stance name}

### Design stance
关于驱动这个变体的原则的一句话。

### Key choices
- Layout: ...
- Typography: ...
- Color: ...
- Interaction: ...

### Trade-offs
- Strong at: ...
- Weak at: ...

### Best for
- 这个变体真正服务的用户类型或用例
```

### 5. Head-to-head

所有变体构建完成后，以比较的方式呈现它们。不要只是列出——**给出观点**：

```markdown
## Three takes on the home screen

| Dimension | Calm editorial | Utilitarian dense | Playful split |
|-----------|----------------|-------------------|---------------|
| Density   | Low            | High              | Medium        |
| Primary action visibility | Low | High | Medium |
| Scan-ability | High | Medium | Low |
| Feel | Calm, trusted | Sharp, tool-like | Inviting, energetic |

**My take:** Utilitarian dense for power users, calm editorial for content-forward audiences. Playful split is weakest — tries to do both and commits to neither.
```

让用户选一个赢家，或把两个合并为混合体，或要求再来一轮。

## Theming（当项目有视觉身份时）

如果用户有现有的主题（颜色、字体、token），把共享 token 放在 `sketches/themes/tokens.css`，并在每个变体中 `@import` 它。保持 token 最少：

```css
/* sketches/themes/tokens.css */
:root {
  --color-bg: #fafafa;
  --color-fg: #1a1a1a;
  --color-accent: #0066ff;
  --color-muted: #666;
  --radius: 8px;
  --font-display: "Inter", sans-serif;
  --font-body: -apple-system, BlinkMacSystemFont, sans-serif;
}
```

不要对一次性 sketch 过度 token 化——三种颜色和一种字体通常就够了。

## 交互性标准

当一个 sketch 让用户能够做到以下几点时，它的交互性就够了：

1. **点击一个主要操作**并有可见的事情发生（状态变化、模态框、toast、导航尝试）
2. **看到一个有意义的状态转换**（筛选列表、切换模式、打开/关闭面板）
3. **悬停可识别的可供性**（按钮、行、标签页）

超过这些就是过度工程化一次性的东西。少于这些就是一张截图。

## Frontier 模式（选择下一个要 sketch 什么）

如果 sketch 已经存在且用户说「what should I sketch next?」：

- **一致性缺口**——来自不同 sketch 的两个获胜变体做出了尚未组合在一起的独立选择
- **未 sketch 的屏幕**——被引用但从未探索过
- **状态覆盖**——happy path 已 sketch，但 empty / loading / error / 1000-items 没有
- **响应式缺口**——在一个视口验证过；在移动端 / 超宽屏下是否成立？
- **交互模式**——静态布局存在；过渡、拖拽、滚动行为不存在

提出 2-4 个命名的候选。让用户选择。

## 输出

- 在仓库根目录创建 `sketches/`（如果用户使用 GSD 约定则为 `.planning/sketches/`）
- 每个变体一个子目录：`NNN-stance-name/index.html` + `README.md`
- 告诉用户如何打开它们：macOS 上 `open sketches/001-calm-editorial/index.html`，Linux 上 `xdg-open`，Windows 上 `start`
- 保持变体可丢弃——一个你觉得需要保留的 sketch 应该被提升为真正的项目代码，而不是当作资产来策展

**单个变体的典型工具序列：**

```
terminal("mkdir -p sketches/001-calm-editorial")
write_file("sketches/001-calm-editorial/index.html", "<!doctype html>...")
write_file("sketches/001-calm-editorial/README.md", "## Variant: Calm editorial\n...")
browser_navigate(url="file://$(pwd)/sketches/001-calm-editorial/index.html")
browser_vision(question="How does this look? Any obvious layout issues?")
```

对每个变体重复，然后呈现比较表。

## 署名

改编自 GSD（Get Shit Done）项目的 `/gsd-sketch` 工作流——MIT © 2025 Lex Christopherson（[gsd-build/get-shit-done](https://github.com/gsd-build/get-shit-done)）。完整的 GSD 系统附带持久化 sketch 状态、主题/变体模式参考和一致性审计工作流；通过 `npx get-shit-done-cc --hermes --global` 安装。
