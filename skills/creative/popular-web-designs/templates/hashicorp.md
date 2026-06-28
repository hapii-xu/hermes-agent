# 设计系统：HashiCorp


> **Hermes Agent —— 实现说明**
>
> 原站点使用专有字体。对于自包含的 HTML 输出，请使用以下 CDN 替代字体：
> - **主字体：** `Inter` | **等宽字体：** `JetBrains Mono`
> - **字体栈（CSS）：** `font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;`
> - **等宽字体栈（CSS）：** `font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;`
> ```html
> <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
> ```
> 使用 `write_file` 创建 HTML，并通过 `generative-widgets` skill（cloudflared 隧道）提供服务。
> 生成后用 `browser_vision` 验证视觉准确性。

## 1. 视觉主题与氛围

HashiCorp 的网站让企业级基础设施变得可触可感——这套设计系统必须既能传达云基础设施管理的复杂性，又要保持平易近人。其视觉语言分为两种模式：信息区块使用干净的白色浅色模式，而英雄区块和产品展示区则使用戏剧化的深色模式（`#15181e`、`#0d0e12`），形成一种日/夜二元对立，恰好映射了开发者「在光明中构建，在黑暗中部署」的工作流程。

字体由一款定制品牌字体（HashiCorp Sans，以 `__hashicorpSans_96f0ca` 加载）锚定，它承载着相当可观的字重——字面意义上。标题使用 600–700 的字重，搭配紧凑的行高（1.17–1.19），形成密集、权威的文字块，传达出企业的自信。英雄标题为 82px、字重 600、启用 OpenType `"kern"`，这并非装饰——这是基础设施级别的排版。

让 HashiCorp 与众不同的是其多产品色彩系统。产品组合中的每个产品都有自己的品牌色——Terraform 紫（`#7b42bc`）、Vault 黄（`#ffcf25`）、Waypoint 青（`#14c6cb`）、Vagrant 蓝（`#1868f2`）——这些颜色通过 CSS 自定义属性系统（`--mds-color-*`）作为强调色 token 贯穿整个设计。这在设计系统之中又构建了一个设计系统：母品牌是黑白配蓝色强调，而每个子产品则注入了各自的色彩身份。

组件系统使用 `mds`（Markdown Design System）前缀，表明这是一种系统化的、token 驱动的方式，其中颜色、间距和状态都通过 CSS 变量来管理。阴影极为克制——双层微阴影使用 `rgba(97, 104, 117, 0.05)`，几乎不可见，但恰好提供足够的深度来区分可交互表面与背景。

**关键特征：**
- 双模式：干净的白色区块 + 戏剧化的深色（`#15181e`）英雄/产品区域
- 定制 HashiCorp Sans 字体，字重 600–700，启用 `"kern"` 特性
- 通过 `--mds-color-*` CSS 自定义属性实现的多产品色彩系统
- 产品品牌色：Terraform 紫、Vault 黄、Waypoint 青、Vagrant 蓝
- 大写字母加字距的说明文字（13px，字重 600，字间距 1.3px）
- 微阴影：0.05 不透明度的双层阴影——通过低语而非呐喊来营造深度
- Token 驱动的 `mds` 组件系统，使用语义化变量名
- 紧凑的圆角：2px–8px，没有药丸形或圆形
- 次要文本使用 system-ui 回退字体栈

## 2. 调色板与角色

### 品牌主色
- **黑色**（`#000000`）：主品牌色，浅色表面上的文本，`--mds-color-hcp-brand`
- **深炭灰**（`#15181e`）：深色模式背景，英雄区块
- **近黑**（`#0d0e12`）：最深的深色模式表面，深色背景上的表单输入框

### 中性色阶
- **浅灰**（`#f1f2f3`）：浅色背景，细微表面
- **中灰**（`#d5d7db`）：边框，深色背景上的按钮文字
- **冷灰**（`#b2b6bd`）：边框强调（0.1–0.4 不透明度）
- **深灰**（`#656a76`）：辅助文字，次要标签，`--mds-form-helper-text-color`
- **炭灰**（`#3b3d45`）：浅色上的次要文字，按钮边框
- **近白**（`#efeff1`）：深色表面上的主要文本

### 产品品牌色
- **Terraform 紫**（`#7b42bc`）：`--mds-color-terraform-button-background`
- **Vault 黄**（`#ffcf25`）：`--mds-color-vault-button-background`
- **Waypoint 青**（`#14c6cb`）：`--mds-color-waypoint-button-background-focus`
- **Waypoint 青 悬停**（`#12b6bb`）：`--mds-color-waypoint-button-background-hover`
- **Vagrant 蓝**（`#1868f2`）：`--mds-color-vagrant-brand`
- **紫色强调**（`#911ced`）：`--mds-color-palette-purple-300`
- **已访问紫**（`#a737ff`）：`--mds-color-foreground-action-visited`

### 语义色
- **操作蓝**（`#1060ff`）：深色背景上的主要操作链接
- **链接蓝**（`#2264d6`）：浅色背景上的主要链接
- **亮蓝**（`#2b89ff`）：激活链接，悬停强调
- **琥珀色**（`#bb5a00`）：`--mds-color-palette-amber-200`，警告状态
- **浅琥珀色**（`#fbeabf`）：`--mds-color-palette-amber-100`，警告背景
- **Vault 淡黄**（`#fff9cf`）：`--mds-color-vault-radar-gradient-faint-stop`
- **橙色**（`#a9722e`）：`--mds-color-unified-core-orange-6`
- **红色**（`#731e25`）：`--mds-color-unified-core-red-7`，错误状态
- **海军蓝**（`#101a59`）：`--mds-color-unified-core-blue-7`

### 阴影
- **微阴影**（`rgba(97, 104, 117, 0.05) 0px 1px 1px, rgba(97, 104, 117, 0.05) 0px 2px 2px`）：默认卡片/按钮高度
- **聚焦轮廓**：`3px solid var(--mds-color-focus-action-external)`——系统化的聚焦环

## 3. 排版规则

### 字体族
- **主品牌字体**：`__hashicorpSans_96f0ca`（HashiCorp Sans），带回退字体：`__hashicorpSans_Fallback_96f0ca`
- **System UI**：`system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial`

### 层级

| 角色 | 字体 | 字号 | 字重 | 行高 | 字间距 | 备注 |
|------|------|------|--------|-------------|----------------|-------|
| 展示级英雄标题 | HashiCorp Sans | 82px (5.13rem) | 600 | 1.17（紧凑） | normal | 启用 `"kern"` |
| 区块标题 | HashiCorp Sans | 52px (3.25rem) | 600 | 1.19（紧凑） | normal | 启用 `"kern"` |
| 功能标题 | HashiCorp Sans | 42px (2.63rem) | 700 | 1.19（紧凑） | -0.42px | 负字距 |
| 副标题 | HashiCorp Sans | 34px (2.13rem) | 600–700 | 1.18（紧凑） | normal | 功能区块 |
| 卡片标题 | HashiCorp Sans | 26px (1.63rem) | 700 | 1.19（紧凑） | normal | 卡片与面板标题 |
| 小标题 | HashiCorp Sans | 19px (1.19rem) | 700 | 1.21（紧凑） | normal | 紧凑型标题 |
| 正文强调 | HashiCorp Sans | 17px (1.06rem) | 600–700 | 1.18–1.35 | normal | 加粗正文 |
| 大号正文 | system-ui | 20px (1.25rem) | 400–600 | 1.50 | normal | 英雄描述 |
| 正文 | system-ui | 16px (1.00rem) | 400–500 | 1.63–1.69（宽松） | normal | 标准正文 |
| 导航链接 | system-ui | 15px (0.94rem) | 500 | 1.60（宽松） | normal | 导航项 |
| 小号正文 | system-ui | 14px (0.88rem) | 400–500 | 1.29–1.71 | normal | 次要内容 |
| 说明文字 | system-ui | 13px (0.81rem) | 400–500 | 1.23–1.69 | normal | 元数据、页脚链接 |
| 大写标签 | HashiCorp Sans | 13px (0.81rem) | 600 | 1.69（宽松） | 1.3px | `text-transform: uppercase` |

### 原则
- **品牌/系统字体分工**：HashiCorp Sans 用于标题和品牌关键文字；system-ui 用于正文、导航和功能性文字。品牌字体承载分量，system-ui 承载文字内容。
- **始终开启字距调整**：所有 HashiCorp Sans 文字都启用 OpenType `"kern"`——字距调整是不可妥协的。
- **紧凑的标题**：每个标题都使用 1.17–1.21 的行高，形成密集、堆叠的文字块，给人基础设施般的感觉——坚实、承重。
- **宽松的正文**：正文使用 1.50–1.69 的行高（相当宽裕），在密集标题下方营造出舒适的阅读节奏。
- **大写标签作为引导**：13px 大写配 1.3px 字间距充当系统化的类别/区块标记——始终使用 HashiCorp Sans 字重 600。

## 4. 组件样式

### 按钮

**深色主按钮**
- 背景：`#15181e`
- 文字：`#d5d7db`
- 内边距：9px 9px 9px 15px（非对称，左侧内边距更大）
- 圆角：5px
- 边框：`1px solid rgba(178, 182, 189, 0.4)`
- 阴影：`rgba(97, 104, 117, 0.05) 0px 1px 1px, rgba(97, 104, 117, 0.05) 0px 2px 2px`
- 聚焦：`3px solid var(--mds-color-focus-action-external)`
- 悬停：使用 `--mds-color-surface-interactive` token

**白色次要按钮**
- 背景：`#ffffff`
- 文字：`#3b3d45`
- 内边距：8px 12px
- 圆角：4px
- 悬停：`--mds-color-surface-interactive` + 低阴影高度
- 聚焦：`3px solid transparent` 轮廓
- 干净、极简的外观

**产品色按钮**
- Terraform：背景 `#7b42bc`
- Vault：背景 `#ffcf25`（深色文字）
- Waypoint：背景 `#14c6cb`，悬停 `#12b6bb`
- 每个产品按钮遵循相同的结构模式，但使用各自的品牌色

### 徽章 / 药丸标签
- 背景：`#42225b`（深紫色）
- 文字：`#efeff1`
- 内边距：3px 7px
- 圆角：5px
- 边框：`1px solid rgb(180, 87, 255)`
- 字号：16px

### 输入框

**文本输入（深色模式）**
- 背景：`#0d0e12`
- 文字：`#efeff1`
- 边框：`1px solid rgb(97, 104, 117)`
- 内边距：11px
- 圆角：5px
- 聚焦：`3px solid var(--mds-color-focus-action-external)` 轮廓

**复选框**
- 背景：`#0d0e12`
- 边框：`1px solid rgb(97, 104, 117)`
- 圆角：3px

### 链接
- **浅色背景上的操作蓝**：`#2264d6`，悬停 → blue-600 变量，悬停时显示下划线
- **深色背景上的操作蓝**：`#1060ff` 或 `#2b89ff`，悬停时显示下划线
- **深色背景上的白色**：`#ffffff`，透明下划线 → 悬停时显示可见下划线
- **浅色背景上的中性色**：`#3b3d45`，透明下划线 → 悬停时显示可见下划线
- **深色背景上的浅色**：`#efeff1`，类似的悬停模式
- 所有链接都使用 `var(--wpl-blue-600)` 作为悬停色

### 卡片与容器
- 浅色模式：白色背景，微阴影高度
- 深色模式：`#15181e` 或更深的表面
- 圆角：卡片和容器为 8px
- 产品展示卡片使用渐变边框或强调灯光

### 导航
- 干净的水平导航，带超级菜单下拉
- HashiCorp 徽标左对齐
- system-ui 15px 字重 500 用于链接
- 产品类别按生命周期管理组组织
- 页头有「Get started」和「Contact us」CTA
- 英雄区块有深色模式变体

## 5. 布局原则

### 间距系统
- 基础单位：8px
- 阶梯：2px, 3px, 4px, 6px, 7px, 8px, 9px, 11px, 12px, 16px, 20px, 24px, 32px, 40px, 48px

### 网格与容器
- 最大内容宽度：约 1150px（xl 断点）
- 全宽深色英雄区块，内容居中
- 卡片网格：2–3 列布局
- 桌面端有宽裕的水平内边距

### 断点
| 名称 | 宽度 | 关键变化 |
|------|-------|-------------|
| 小型移动设备 | <375px | 紧凑单列 |
| 移动设备 | 375–480px | 标准移动布局 |
| 小型平板 | 480–600px | 轻微调整 |
| 平板 | 600–768px | 开始出现 2 列网格 |
| 小型桌面 | 768–992px | 完整导航可见 |
| 桌面 | 992–1120px | 标准布局 |
| 大型桌面 | 1120–1440px | 最大宽度内容 |
| 超宽屏 | >1440px | 居中，宽裕边距 |

### 留白哲学
- **企业级的呼吸空间**：区块之间宽裕的垂直间距（48px–80px+）传达出稳定和严谨。
- **密集标题，宽敞正文**：紧凑行高的标题位于宽松正文上方，在每个区块顶部形成视觉「重心」。
- **以深色为画布**：深色英雄区块使用额外的垂直内边距，让 3D 插图和渐变得以呼吸。

### 圆角阶梯
- 最小（2px）：链接、小型内联元素
- 细微（3px）：复选框、小型输入框
- 标准（4px）：次要按钮
- 舒适（5px）：主按钮、徽章、输入框
- 卡片（8px）：卡片、容器、图片

## 6. 深度与高度

| 层级 | 处理方式 | 用途 |
|-------|-----------|-----|
| 平坦（层级 0） | 无阴影 | 默认表面、文字块 |
| 低语（层级 1） | `rgba(97, 104, 117, 0.05) 0px 1px 1px, rgba(97, 104, 117, 0.05) 0px 2px 2px` | 卡片、按钮、可交互表面 |
| 聚焦（层级 2） | `3px solid var(--mds-color-focus-action-external)` 轮廓 | 聚焦环——颜色与上下文匹配 |

**阴影哲学**：HashiCorp 可以说是使用了现代网页设计中最克制的阴影系统。5% 不透明度的双层阴影几乎不可见——它们的存在不是为了创造视觉深度，而是为了标示可交互性。如果你能看见阴影，那就太强了。这种克制传达了企业对稳定性的价值取向——没有东西漂浮，没有东西是不确定的。

## 7. 宜与忌

### 宜
- 标题和品牌文字使用 HashiCorp Sans，正文和 UI 文字使用 system-ui
- 在所有 HashiCorp Sans 文字上启用 `"kern"`
- 仅将产品品牌色用于对应的产品（Terraform = 紫，Vault = 黄，等等）
- 区块标记使用 13px 字重 600、字间距 1.3px 的大写标签
- 将阴影保持在「低语」级别（0.05 不透明度双层）
- 使用 `--mds-color-*` token 系统以保持色彩应用的一致性
- 维持紧凑标题 / 宽松正文的节奏（1.17–1.21 对比 1.50–1.69 行高）
- 为可访问性使用 `3px solid` 聚焦轮廓

### 忌
- 不要在产品上下文之外使用产品品牌色（不要在 Vault 内容上用 Terraform 紫）
- 不要将阴影不透明度提高到 0.1 以上——低语级别是有意为之的
- 不要使用药丸形按钮（>8px 圆角）——尖锐、极简的圆角是结构性的
- 不要在标题上省略 `"kern"` 特性——这款字体需要它
- 不要将 HashiCorp Sans 用于小号正文——它是为 17px+ 标题用途设计的
- 不要在同一组件中混合产品色——每个产品只有一种颜色
- 不要使用纯黑（`#000000`）作为深色背景——请使用 `#15181e` 或 `#0d0e12`
- 不要忘记非对称按钮内边距——9px 9px 9px 15px 是有意为之的

## 8. 响应式行为

### 断点
| 名称 | 宽度 | 关键变化 |
|------|-------|-------------|
| 移动设备 | <768px | 单列、汉堡菜单、堆叠 CTA |
| 平板 | 768–992px | 2 列网格、导航开始展开 |
| 桌面 | 992–1150px | 完整布局、超级菜单导航 |
| 大屏 | >1150px | 最大宽度居中、宽裕边距 |

### 折叠策略
- 英雄区：82px → 52px → 42px 标题字号
- 导航：超级菜单 → 汉堡菜单
- 产品卡片：3 列 → 2 列 → 堆叠
- 深色区块保持全宽，但压缩内边距
- 按钮：内联 → 移动端堆叠为全宽

## 9. Agent 提示指南

### 快速颜色参考
- 浅色背景：`#ffffff`、`#f1f2f3`
- 深色背景：`#15181e`、`#0d0e12`
- 浅色文字：`#000000`、`#3b3d45`
- 深色文字：`#efeff1`、`#d5d7db`
- 链接：`#2264d6`（浅色）、`#1060ff`（深色）、`#2b89ff`（激活）
- 辅助文字：`#656a76`
- 边框：`rgba(178, 182, 189, 0.4)`、`rgb(97, 104, 117)`
- 聚焦：`3px solid` 与产品上下文匹配的颜色

### 示例组件提示
- 「在深色背景（#15181e）上创建一个英雄区块。标题为 82px HashiCorp Sans 字重 600、行高 1.17、启用 kern、白色文字。副文本为 20px system-ui 字重 400、行高 1.50、#d5d7db 文字。两个按钮：深色主按钮（#15181e、5px 圆角、9px 15px 内边距）和白色次要按钮（#ffffff、4px 圆角、8px 12px 内边距）。」
- 「设计一个产品卡片：白色背景、8px 圆角、rgba(97,104,117,0.05) 双层阴影。标题为 26px HashiCorp Sans 字重 700，正文为 16px system-ui 字重 400 行高 1.63。」
- 「构建一个大写区块标签：13px HashiCorp Sans 字重 600、行高 1.69、字间距 1.3px、text-transform uppercase、#656a76 颜色。」
- 「创建一个特定产品的 CTA 按钮：Terraform → #7b42bc 背景，Vault → #ffcf25 配深色文字，Waypoint → #14c6cb。所有按钮：5px 圆角、500 字重文字、16px system-ui。」
- 「设计一个深色表单：#0d0e12 输入框背景、#efeff1 文字、1px solid rgb(97,104,117) 边框、5px 圆角、11px 内边距。聚焦：3px solid accent-color 轮廓。」

### 迭代指南
1. 始终从模式决策开始：信息区块用浅色（白色），英雄/产品区块用深色（#15181e）
2. 仅标题（17px+）使用 HashiCorp Sans，其余全部使用 system-ui
3. 阴影处于低语级别（0.05 不透明度）——如果可见，就减弱
4. 产品色是神圣的——每个产品恰好拥有一种颜色
5. 聚焦环始终是 3px solid，颜色与产品上下文匹配
6. 大写标签是系统化的引导模式——13px、600、1.3px 字距
