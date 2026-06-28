---
name: baoyu-infographic
description: "信息图：21 种布局 × 21 种风格（信息图、可视化）。"
version: 1.56.1
author: 宝玉 (JimLiu)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [infographic, visual-summary, creative, image-generation]
    homepage: https://github.com/JimLiu/baoyu-skills#baoyu-infographic
---

# 信息图生成器

改编自 [baoyu-infographic](https://github.com/JimLiu/baoyu-skills)，适配 Hermes Agent 的工具生态。

两个维度：**布局**（信息结构）× **风格**（视觉美学）。任意布局与任意风格可自由组合。

## 适用场景

当用户要求创建信息图、视觉摘要、信息图表，或使用"信息图"、"可视化"、"高密度信息大图"等词汇时触发本技能。用户提供内容（文本、文件路径、URL 或主题），并可选择性地指定布局、风格、宽高比或语言。

## 选项

| 选项 | 取值 |
|--------|--------|
| 布局 | 21 个选项（见布局画廊），默认：bento-grid |
| 风格 | 21 个选项（见风格画廊），默认：craft-handmade |
| 宽高比 | 命名预设：landscape (16:9)、portrait (9:16)、square (1:1)。自定义：任意 W:H 比例（如 3:4、4:3、2.35:1） |
| 语言 | en、zh、ja 等 |

## 布局画廊

| 布局 | 最适用于 |
|--------|----------|
| `linear-progression` | 时间线、流程、教程 |
| `binary-comparison` | A 对 B、前后对比、优缺点 |
| `comparison-matrix` | 多因素对比 |
| `hierarchical-layers` | 金字塔、优先级层级 |
| `tree-branching` | 分类、类目体系 |
| `hub-spoke` | 中心概念及关联条目 |
| `structural-breakdown` | 分解图、剖面图 |
| `bento-grid` | 多主题、概览（默认） |
| `iceberg` | 表面与隐藏层面 |
| `bridge` | 问题—解决方案 |
| `funnel` | 转化、筛选 |
| `isometric-map` | 空间关系 |
| `dashboard` | 指标、KPI |
| `periodic-table` | 分类集合 |
| `comic-strip` | 叙事、序列 |
| `story-mountain` | 情节结构、张力曲线 |
| `jigsaw` | 相互关联的部分 |
| `venn-diagram` | 重叠概念 |
| `winding-roadmap` | 旅程、里程碑 |
| `circular-flow` | 循环、周期性流程 |
| `dense-modules` | 高密度模块、数据丰富的指南 |

完整定义：`references/layouts/<layout>.md`

## 风格画廊

| 风格 | 描述 |
|-------|-------------|
| `craft-handmade` | 手绘、纸艺（默认） |
| `claymation` | 3D 黏土人物、定格动画 |
| `kawaii` | 日系可爱、粉彩 |
| `storybook-watercolor` | 柔和水彩、奇幻 |
| `chalkboard` | 黑板粉笔字 |
| `cyberpunk-neon` | 霓虹光效、未来感 |
| `bold-graphic` | 漫画风、半调网点 |
| `aged-academia` | 复古科学、棕褐色调 |
| `corporate-memphis` | 扁平矢量、明快色彩 |
| `technical-schematic` | 蓝图、工程制图 |
| `origami` | 折纸、几何形态 |
| `pixel-art` | 复古 8 位像素 |
| `ui-wireframe` | 灰度界面线框 |
| `subway-map` | 地铁线路图 |
| `ikea-manual` | 极简线条画 |
| `knolling` | 整齐排列的平铺陈列 |
| `lego-brick` | 玩具积木拼装 |
| `pop-laboratory` | 蓝图网格、坐标标记、实验室精度 |
| `morandi-journal` | 手绘涂鸦、温暖莫兰迪色调 |
| `retro-pop-grid` | 1970 年代复古波普、瑞士网格、粗描边 |
| `hand-drawn-edu` | 马卡龙粉彩、手绘抖动感、火柴人 |

完整定义：`references/styles/<style>.md`

## 推荐组合

| 内容类型 | 布局 + 风格 |
|--------------|----------------|
| 时间线/历史 | `linear-progression` + `craft-handmade` |
| 分步教程 | `linear-progression` + `ikea-manual` |
| A 对 B | `binary-comparison` + `corporate-memphis` |
| 层级关系 | `hierarchical-layers` + `craft-handmade` |
| 重叠关系 | `venn-diagram` + `craft-handmade` |
| 转化漏斗 | `funnel` + `corporate-memphis` |
| 循环周期 | `circular-flow` + `craft-handmade` |
| 技术类 | `structural-breakdown` + `technical-schematic` |
| 指标数据 | `dashboard` + `corporate-memphis` |
| 教育类 | `bento-grid` + `chalkboard` |
| 旅程 | `winding-roadmap` + `storybook-watercolor` |
| 分类 | `periodic-table` + `bold-graphic` |
| 产品指南 | `dense-modules` + `morandi-journal` |
| 技术指南 | `dense-modules` + `pop-laboratory` |
| 潮流指南 | `dense-modules` + `retro-pop-grid` |
| 教学图解 | `hub-spoke` + `hand-drawn-edu` |
| 流程教程 | `linear-progression` + `hand-drawn-edu` |

默认：`bento-grid` + `craft-handmade`

## 关键词快捷方式

当用户输入包含以下关键词时，**自动选择**对应的布局，并在第 3 步中将关联风格作为首选推荐。匹配到关键词时跳过基于内容的布局推断。

若快捷方式带有**提示词备注**，则在生成的提示词（第 5 步）中将其作为额外的风格指令追加。

| 用户关键词 | 布局 | 推荐风格 | 默认宽高比 | 提示词备注 |
|--------------|--------|--------------------|----------------|--------------|
| 高密度信息大图 / high-density-info | `dense-modules` | `morandi-journal`、`pop-laboratory`、`retro-pop-grid` | portrait | — |
| 信息图 / infographic | `bento-grid` | `craft-handmade` | landscape | 极简：干净的画布、充足的留白、不要复杂的背景纹理。仅使用简单的卡通元素和图标。 |

## 输出结构

```
infographic/{topic-slug}/
├── source-{slug}.{ext}
├── analysis.md
├── structured-content.md
├── prompts/infographic.md
└── infographic.png
```

Slug：取主题的 2-4 个词，转为 kebab-case。冲突时：追加 `-YYYYMMDD-HHMMSS`。

## 核心原则

- 忠实保留源数据 — 不做摘要或改写（但在写入输出前**剥离任何凭据、API 密钥、令牌或机密信息**）
- 在组织内容之前先明确学习目标
- 以视觉传达为目标进行结构化（标题、标签、视觉元素）

## 工作流

### 第 1 步：分析内容

**加载参考文件**：读取本技能的 `references/analysis-framework.md`。

1. 保存源内容（文件路径或粘贴内容 → 使用 `write_file` 写入 `source.md`）
   - **备份规则**：若 `source.md` 已存在，重命名为 `source-backup-YYYYMMDD-HHMMSS.md`
2. 分析：主题、数据类型、复杂度、语气、受众
3. 检测源语言和用户语言
4. 从用户输入中提取设计指令
5. 将分析结果保存到 `analysis.md`
   - **备份规则**：若 `analysis.md` 已存在，重命名为 `analysis-backup-YYYYMMDD-HHMMSS.md`

详细格式见 `references/analysis-framework.md`。

### 第 2 步：生成结构化内容 → `structured-content.md`

将内容转化为信息图结构：
1. 标题与学习目标
2. 各部分包含：核心概念、内容（原文照录）、视觉元素、文字标签
3. 数据点（所有统计数字/引述逐字复制）
4. 来自用户的设计指令

**规则**：仅使用 Markdown。不新增信息。忠实保留数据。从输出中剥离任何凭据或机密。

详细格式见 `references/structured-content-template.md`。

### 第 3 步：推荐组合

**3.1 先检查关键词快捷方式**：若用户输入匹配**关键词快捷方式**表中的某个关键词，自动选择对应布局并将关联风格作为首选推荐。跳过基于内容的布局推断。

**3.2 否则**，根据以下因素推荐 3-5 个布局×风格组合：
- 数据结构 → 匹配布局
- 内容语气 → 匹配风格
- 受众期望
- 用户设计指令

### 第 4 步：确认选项

使用 `clarify` 工具与用户确认选项。由于 `clarify` 一次只处理一个问题，先问最重要的问题：

**问题 1 — 组合**：给出 3 个以上的布局×风格组合及理由。请用户选择一个。

**问题 2 — 宽高比**：询问宽高比偏好（landscape/portrait/square 或自定义 W:H）。

**问题 3 — 语言**（仅当源语言 ≠ 用户语言时）：询问文字内容应使用哪种语言。

### 第 5 步：生成提示词 → `prompts/infographic.md`

**备份规则**：若 `prompts/infographic.md` 已存在，重命名为 `prompts/infographic-backup-YYYYMMDD-HHMMSS.md`

**加载参考文件**：从 `references/layouts/<layout>.md` 读取所选布局，从 `references/styles/<style>.md` 读取所选风格。

组合以下内容：
1. 来自 `references/layouts/<layout>.md` 的布局定义
2. 来自 `references/styles/<style>.md` 的风格定义
3. 来自 `references/base-prompt.md` 的基础模板
4. 第 2 步的结构化内容
5. 所有文字使用已确认的语言

**宽高比解析**（用于 `{{ASPECT_RATIO}}`）：
- 命名预设 → 比例字符串：landscape→`16:9`、portrait→`9:16`、square→`1:1`
- 自定义 W:H 比例 → 原样使用（如 `3:4`、`4:3`、`2.35:1`）

使用 `write_file` 将组装好的提示词保存到 `prompts/infographic.md`。

### 第 6 步：生成图像

使用 `image_generate` 工具，传入第 5 步组装好的提示词。

- 将宽高比映射到 image_generate 的格式：`16:9` → `landscape`、`9:16` → `portrait`、`1:1` → `square`
- 自定义比例选最接近的命名宽高比
- 失败时自动重试一次
- 将生成的图像 URL/路径保存到输出目录

### 第 7 步：输出摘要

报告：主题、布局、风格、宽高比、语言、输出路径、创建的文件。

## 参考文件

- `references/analysis-framework.md` — 分析方法论
- `references/structured-content-template.md` — 内容格式
- `references/base-prompt.md` — 提示词模板
- `references/layouts/<layout>.md` — 21 个布局定义
- `references/styles/<style>.md` — 21 个风格定义

## 易错点

1. **数据完整性至上** — 绝不摘要、改写或篡改源统计数据。"增长 73%" 必须保持 "增长 73%"，不能变成 "显著增长"。
2. **剥离机密** — 在将源内容写入任何输出文件之前，始终扫描其中的 API 密钥、令牌或凭据。
3. **每节传达一个信息** — 信息图的每个部分应传达一个清晰的概念。塞入过多内容会降低可读性。
4. **风格一致性** — 参考文件中的风格定义必须一致地应用于整张信息图。不要混用风格。
5. **image_generate 的宽高比** — 该工具仅支持 `landscape`、`portrait` 和 `square`。`3:4` 等自定义比例应映射到最接近的选项（此例为 portrait）。
