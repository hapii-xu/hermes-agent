---
name: design-md
description: 创作/验证/导出 Google 的 DESIGN.md 令牌规范文件。
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [design, design-system, tokens, ui, accessibility, wcag, tailwind, dtcg, google]
    related_skills: [popular-web-designs, claude-design, excalidraw, architecture-diagram]
---

# DESIGN.md Skill

DESIGN.md 是 Google 的开放规范（Apache-2.0，`google-labs-code/design.md`），用于向编码智能体描述视觉身份。单个文件结合了：

- **YAML front matter** —— 机器可读的设计令牌（规范性值）
- **Markdown 正文** —— 人类可读的设计理由，按规范章节组织

令牌给出精确值。正文告诉智能体这些值*为什么*存在以及如何应用。CLI（`npx @google/design.md`）会校验结构和 WCAG 对比度、对比版本以发现回归，并导出为 Tailwind 或 W3C DTCG JSON。

## 何时使用此 skill

- 用户要求一个 DESIGN.md 文件、设计令牌或设计系统规范
- 用户希望跨多个项目或工具保持一致的 UI/品牌
- 用户粘贴一个现有的 DESIGN.md 并要求校验、对比、导出或扩展它
- 用户要求将样式指南移植为智能体可消费的格式
- 用户希望对其调色板进行对比度 / WCAG 可访问性验证

若仅是为了视觉灵感或布局示例，请改用 `popular-web-designs`。若要从零设计一次性 HTML 产物（原型、幻灯片、落地页、组件实验室）的*流程与品味*，请使用 `claude-design`。本 skill 面向的是*正式规范文件*本身。

## 文件结构剖析

```md
---
version: alpha
name: Heritage
description: Architectural minimalism meets journalistic gravitas.
colors:
  primary: "#1A1C1E"
  secondary: "#6C7278"
  tertiary: "#B8422E"
  neutral: "#F7F5F2"
typography:
  h1:
    fontFamily: Public Sans
    fontSize: 3rem
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: "-0.02em"
  body-md:
    fontFamily: Public Sans
    fontSize: 1rem
rounded:
  sm: 4px
  md: 8px
  lg: 16px
spacing:
  sm: 8px
  md: 16px
  lg: 24px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "#FFFFFF"
    rounded: "{rounded.sm}"
    padding: 12px
  button-primary-hover:
    backgroundColor: "{colors.primary}"
---

## Overview

Architectural Minimalism meets Journalistic Gravitas...

## Colors

- **Primary (#1A1C1E):** Deep ink for headlines and core text.
- **Tertiary (#B8422E):** "Boston Clay" — the sole driver for interaction.

## Typography

Public Sans for everything except small all-caps labels...

## Components

`button-primary` is the only high-emphasis action on a page...
```

## 令牌类型

| 类型 | 格式 | 示例 |
|------|--------|---------|
| 颜色 | `#` + 十六进制 (sRGB) | `"#1A1C1E"` |
| 尺寸 | 数字 + 单位 (`px`, `em`, `rem`) | `48px`, `-0.02em` |
| 令牌引用 | `{path.to.token}` | `{colors.primary}` |
| 排版 | 含 `fontFamily`, `fontSize`, `fontWeight`, `lineHeight`, `letterSpacing`, `fontFeature`, `fontVariation` 的对象 | 见上文 |

组件属性白名单：`backgroundColor`, `textColor`, `typography`, `rounded`, `padding`, `size`, `height`, `width`。变体（hover、active、pressed）是**独立的组件条目**，使用关联的键名（`button-primary-hover`），而非嵌套。

## 规范章节顺序

章节是可选的，但存在的章节必须按此顺序出现。重复的标题会导致文件被拒绝。

1. Overview（别名：Brand & Style）
2. Colors
3. Typography
4. Layout（别名：Layout & Spacing）
5. Elevation & Depth（别名：Elevation）
6. Shapes
7. Components
8. Do's and Don'ts

未知章节会被保留，而非报错。未知令牌名在值类型有效时会被接受。未知组件属性会产生警告。

## 工作流：创作新的 DESIGN.md

1. **询问用户**（或推断）品牌基调、强调色和排版方向。若他们提供了网站、图片或氛围，将其转换为上文的令牌结构。
2. **用 `write_file` 在他们的项目根目录写入 `DESIGN.md`**。始终包含 `name:` 和 `colors:`；其他章节可选但推荐。
3. **在 `components:` 章节使用令牌引用**（`{colors.primary}`），而非重新输入十六进制值。保持调色板单一来源。
4. **校验它**（见下文）。在返回前修复任何损坏的引用或 WCAG 失败。
5. **若用户有现有项目**，也在文件旁写入 Tailwind 或 DTCG 导出（`tailwind.theme.json`、`tokens.json`）。

## 工作流：校验 / 对比 / 导出

CLI 为 `@google/design.md`（Node）。使用 `npx` —— 无需全局安装。

```bash
# 校验结构 + 令牌引用 + WCAG 对比度
npx -y @google/design.md lint DESIGN.md

# 对比两个版本，回归时失败 (exit 1 = 回归)
npx -y @google/design.md diff DESIGN.md DESIGN-v2.md

# 导出为 Tailwind 主题 JSON
npx -y @google/design.md export --format tailwind DESIGN.md > tailwind.theme.json

# 导出为 W3C DTCG (Design Tokens Format Module) JSON
npx -y @google/design.md export --format dtcg DESIGN.md > tokens.json

# 打印规范本身 —— 注入到智能体提示词时很有用
npx -y @google/design.md spec --rules-only --format json
```

所有命令都接受 `-` 表示 stdin。`lint` 在出错时返回 exit 1。若需要结构化报告发现，使用 `--format json` 标志并解析输出。

### 校验规则参考（7 条规则分别捕获什么）

- `broken-ref` (error) —— `{colors.missing}` 指向不存在的令牌
- `duplicate-section` (error) —— 同一个 `## Heading` 出现两次
- `invalid-color`, `invalid-dimension`, `invalid-typography` (error)
- `wcag-contrast` (warning/info) —— 组件 `textColor` 对 `backgroundColor` 按 WCAG AA (4.5:1) 和 AAA (7:1) 的对比率
- `unknown-component-property` (warning) —— 超出上文白名单

当用户关心可访问性时，在总结中明确指出 —— WCAG 发现是使用 CLI 最关键的理由。

## 陷阱

- **不要嵌套组件变体。** `button-primary.hover` 是错的；`button-primary-hover` 作为同级键才对。
- **十六进制颜色必须是带引号的字符串。** 否则 YAML 会在 `#` 处出错，或奇怪地截断 `#1A1C1E` 这样的值。
- **负尺寸也需要引号。** `letterSpacing: -0.02em` 会被解析为 YAML 流；应写 `letterSpacing: "-0.02em"`。
- **章节顺序是强制的。** 若用户给你的正文是随机顺序，在保存前重新排序以匹配规范列表。
- **`version: alpha` 是当前规范版本**（截至 2026 年 4 月）。该规范标记为 alpha —— 注意破坏性变更。
- **令牌引用按点分路径解析。** `{colors.primary}` 有效；`{primary}` 无效。

## 规范权威来源

- 仓库：https://github.com/google-labs-code/design.md (Apache-2.0)
- CLI：npm 上的 `@google/design.md`
- 生成的 DESIGN.md 文件许可：由用户项目决定；规范本身为 Apache-2.0。
