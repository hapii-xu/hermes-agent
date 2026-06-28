---
version: alpha
name: MyBrand
description: 用一句话描述该视觉身份。
colors:
  primary: "#0F172A"
  secondary: "#64748B"
  tertiary: "#2563EB"
  neutral: "#F8FAFC"
  on-primary: "#FFFFFF"
  on-tertiary: "#FFFFFF"
typography:
  h1:
    fontFamily: Inter
    fontSize: 3rem
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: "-0.02em"
  h2:
    fontFamily: Inter
    fontSize: 2rem
    fontWeight: 600
    lineHeight: 1.2
  body-md:
    fontFamily: Inter
    fontSize: 1rem
    lineHeight: 1.5
  label-caps:
    fontFamily: Inter
    fontSize: 0.75rem
    fontWeight: 600
    letterSpacing: "0.08em"
rounded:
  sm: 4px
  md: 8px
  lg: 16px
  full: 9999px
spacing:
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 48px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-tertiary}"
    rounded: "{rounded.sm}"
    padding: 12px
  button-primary-hover:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
  card:
    backgroundColor: "{colors.neutral}"
    textColor: "{colors.primary}"
    rounded: "{rounded.md}"
    padding: 24px
---

## Overview

用一到两段话描述品牌的声音和感觉。它唤起什么情绪？用户在第一印象时应该有什么样的情感反应？

## Colors

- **Primary ({colors.primary})：** 核心文本、标题、高强调表面。
- **Secondary ({colors.secondary})：** 辅助文本、边框、元数据。
- **Tertiary ({colors.tertiary})：** 交互驱动色 —— 按钮、链接、选中状态。谨慎使用以保持其信号价值。
- **Neutral ({colors.neutral})：** 页面背景和表面填充。

## Typography

全部使用 Inter。层次由字重和字号承载，而非字体系列。展示级字号使用紧凑字距；正文使用默认字距。

## Layout

间距比例基于 4px 基线。组件内部间隙用 `md` (16px)，组件之间间隙用 `lg` (24px)，分节断点用 `xl` (48px)。

## Shapes

圆角适度 —— 交互元素用 `sm`，卡片用 `md`。`full` 保留给头像和药丸形徽章。

## Components

- `button-primary` 是每个屏幕上唯一的高强调操作。
- `card` 是分组内容的默认表面。默认无阴影。

## Do's and Don'ts

- **要做** 在组件定义中使用令牌引用（`{colors.primary}`），而非字面十六进制值。
- **不要做** 引入调色板之外的颜色 —— 先扩展调色板。
- **不要做** 嵌套组件变体。`button-primary-hover` 是同级，而非子级。
