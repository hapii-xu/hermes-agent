---
name: popular-web-designs
description: 54 套真实设计系统（Stripe、Linear、Vercel）以 HTML/CSS 形式提供。
version: 1.0.0
author: Hermes Agent + Teknium（设计系统来源于 VoltAgent/awesome-design-md）
license: MIT
tags: [design, css, html, ui, web-development, design-systems, templates]
platforms: [linux, macos, windows]
triggers:
  - build a page that looks like
  - make it look like stripe
  - design like linear
  - vercel style
  - create a UI
  - web design
  - landing page
  - dashboard design
  - website styled like
---

# Popular Web Designs

54 套真实世界的设计系统，可直接用于生成 HTML/CSS。每个模板都捕获了一个站点完整的视觉语言：配色方案、字体层级、组件样式、间距体系、阴影、响应式行为，以及带有精确 CSS 值的实用 agent 提示词。

## 相关设计技能

- **`claude-design`** —— 用于设计*流程与品味*（需求简报拆解、生成方案变体、验证本地 HTML 产物、避免 AI 设计垃圾）。当用户希望以某个知名品牌风格打造一个经过精心设计的页面时，请将该技能与本技能配合使用：`claude-design` 驱动工作流程，本技能提供视觉词汇。
- **`design-md`** —— 当交付物是一份正式的 DESIGN.md token 规范文件，而非渲染产物时使用。

## 使用方法

1. 从下方目录中挑选一个设计
2. 加载它：`skill_view(name="popular-web-designs", file_path="templates/<site>.md")`
3. 在生成 HTML 时使用其中的设计 token 和组件规范
4. 配合 `generative-widgets` 技能通过 cloudflared 隧道提供访问

每个模板顶部都包含一个 **Hermes Implementation Notes**（实现说明）块，内容包括：
- CDN 字体替代方案以及可直接粘贴的 Google Fonts `<link>` 标签
- 主要字体和等宽字体的 CSS font-family 字体栈
- 提醒使用 `write_file` 创建 HTML，并使用 `browser_vision` 进行验证

## HTML 生成模式

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Page Title</title>
  <!-- 从模板的 Hermes notes 中粘贴 Google Fonts <link> -->
  <link href="https://fonts.googleapis.com/css2?family=..." rel="stylesheet">
  <style>
    /* 将模板的配色方案作为 CSS 自定义属性应用 */
    :root {
      --color-bg: #ffffff;
      --color-text: #171717;
      --color-accent: #533afd;
      /* ... 更多来自模板第 2 节的值 */
    }
    /* 从模板第 3 节应用字体规则 */
    body {
      font-family: 'Inter', system-ui, sans-serif;
      color: var(--color-text);
      background: var(--color-bg);
    }
    /* 从模板第 4 节应用组件样式 */
    /* 从模板第 5 节应用布局 */
    /* 从模板第 6 节应用阴影 */
  </style>
</head>
<body>
  <!-- 使用模板中的组件规范进行构建 -->
</body>
</html>
```

用 `write_file` 写入文件，通过 `generative-widgets` 工作流（cloudflared 隧道）提供服务，再用 `browser_vision` 验证结果，以确认视觉准确性。

## 字体替代参考

大多数站点使用无法通过 CDN 获取的专有字体。每个模板都映射到一个能保留设计气质的 Google Fonts 替代字体。常见映射：

| 专有字体 | CDN 替代 | 特征 |
|---|---|---|
| Geist / Geist Sans | Geist（在 Google Fonts 上） | 几何感、紧凑的字距 |
| Geist Mono | Geist Mono（在 Google Fonts 上） | 干净的等宽、连字 |
| sohne-var（Stripe） | Source Sans 3 | 细字重的优雅 |
| Berkeley Mono | JetBrains Mono | 技术感等宽 |
| Airbnb Cereal VF | DM Sans | 圆润、亲切的几何感 |
| Circular（Spotify） | DM Sans | 几何感、温暖 |
| figmaSans | Inter | 干净的人文主义 |
| Pin Sans（Pinterest） | DM Sans | 亲切、圆润 |
| NVIDIA-EMEA | Inter（或 Arial 系统字体） | 工业感、干净 |
| CoinbaseDisplay/Sans | DM Sans | 几何感、可信 |
| UberMove | DM Sans | 粗壮、紧凑 |
| HashiCorp Sans | Inter | 企业感、中性 |
| waldenburgNormal（Sanity） | Space Grotesk | 几何感、略窄 |
| IBM Plex Sans/Mono | IBM Plex Sans/Mono | 可在 Google Fonts 获取 |
| Rubik（Sentry） | Rubik | 可在 Google Fonts 获取 |

当模板的 CDN 字体与原版一致时（Inter、IBM Plex、Rubik、Geist），不会有替换损失。当使用替代字体时（如用 DM Sans 替换 Circular、用 Source Sans 3 替换 sohne-var），请严格遵循模板中的字重、字号和字距值 —— 这些比具体的字体本身承载了更多的视觉识别度。

## 设计目录

### AI 与机器学习

| 模板 | 站点 | 风格 |
|---|---|---|
| `claude.md` | Anthropic Claude | 温暖的陶土色点缀、干净的编辑式布局 |
| `cohere.md` | Cohere | 鲜艳的渐变、信息密集的仪表板美学 |
| `elevenlabs.md` | ElevenLabs | 深色电影感 UI、声波美学 |
| `minimax.md` | Minimax | 大胆的深色界面搭配霓虹点缀 |
| `mistral.ai.md` | Mistral AI | 法式工程极简主义、紫色调 |
| `ollama.md` | Ollama | 终端优先、单色简约 |
| `opencode.ai.md` | OpenCode AI | 开发者向深色主题、全等宽字体 |
| `replicate.md` | Replicate | 干净的白色画布、代码为先 |
| `runwayml.md` | RunwayML | 电影感深色 UI、媒体丰富的布局 |
| `together.ai.md` | Together AI | 技术感、蓝图式设计 |
| `voltagent.md` | VoltAgent | 漆黑画布、翡翠色点缀、终端原生 |
| `x.ai.md` | xAI | 冷峻单色、未来极简主义、全等宽字体 |

### 开发者工具与平台

| 模板 | 站点 | 风格 |
|---|---|---|
| `cursor.md` | Cursor | 流畅的深色界面、渐变点缀 |
| `expo.md` | Expo | 深色主题、紧凑字距、代码为中心 |
| `linear.app.md` | Linear | 超极简暗色模式、精确、紫色点缀 |
| `lovable.md` | Lovable | 活泼的渐变、亲切的开发者美学 |
| `mintlify.md` | Mintlify | 干净、绿色点缀、阅读优化 |
| `posthog.md` | PostHog | 活泼的品牌感、开发者友好的深色 UI |
| `raycast.md` | Raycast | 流畅的深色外框、鲜艳的渐变点缀 |
| `resend.md` | Resend | 极简深色主题、等宽字体点缀 |
| `sentry.md` | Sentry | 深色仪表板、信息密集、粉紫点缀 |
| `supabase.md` | Supabase | 深色翡翠主题、代码优先的开发者工具 |
| `superhuman.md` | Superhuman | 高端深色 UI、键盘优先、紫色辉光 |
| `vercel.md` | Vercel | 黑白精确、Geist 字体系统 |
| `warp.md` | Warp | 深色 IDE 风界面、块状命令 UI |
| `zapier.md` | Zapier | 温暖的橙色、亲切的插画驱动 |

### 基础设施与云服务

| 模板 | 站点 | 风格 |
|---|---|---|
| `clickhouse.md` | ClickHouse | 黄色点缀、技术文档风格 |
| `composio.md` | Composio | 现代深色搭配多彩集成图标 |
| `hashicorp.md` | HashiCorp | 企业级干净、黑白 |
| `mongodb.md` | MongoDB | 绿色叶片品牌、开发者文档导向 |
| `sanity.md` | Sanity | 红色点缀、内容为先的编辑式布局 |
| `stripe.md` | Stripe | 标志性的紫色渐变、字重 300 的优雅 |

### 设计与生产力

| 模板 | 站点 | 风格 |
|---|---|---|
| `airtable.md` | Airtable | 色彩丰富、亲切、结构化数据美学 |
| `cal.md` | Cal.com | 干净的中性 UI、开发者向简约 |
| `clay.md` | Clay | 有机形状、柔和渐变、艺术化布局 |
| `figma.md` | Figma | 鲜艳的多色、活泼又不失专业 |
| `framer.md` | Framer | 大胆的黑与蓝、动效优先、设计驱动 |
| `intercom.md` | Intercom | 亲切的蓝色调、对话式 UI 模式 |
| `miro.md` | Miro | 明亮的黄色点缀、无限画布美学 |
| `notion.md` | Notion | 温暖的极简主义、衬线标题、柔和表面 |
| `pinterest.md` | Pinterest | 红色点缀、瀑布流网格、图片为先 |
| `webflow.md` | Webflow | 蓝色点缀、精致的营销站点美学 |

### 金融科技与加密

| 模板 | 站点 | 风格 |
|---|---|---|
| `coinbase.md` | Coinbase | 干净的蓝色识别、信任为先、机构感 |
| `kraken.md` | Kraken | 紫色点缀的深色 UI、信息密集的仪表板 |
| `revolut.md` | Revolut | 流畅的深色界面、渐变卡片、金融科技精度 |
| `wise.md` | Wise | 明亮的绿色点缀、亲切清晰 |

### 企业与消费

| 模板 | 站点 | 风格 |
|---|---|---|
| `airbnb.md` | Airbnb | 温暖的珊瑚色点缀、照片驱动、圆润 UI |
| `apple.md` | Apple | 高端留白、SF Pro、电影感图片 |
| `bmw.md` | BMW | 深色高端表面、精准工程美学 |
| `ibm.md` | IBM | Carbon 设计系统、结构化的蓝色调 |
| `nvidia.md` | NVIDIA | 绿黑能量感、技术力量美学 |
| `spacex.md` | SpaceX | 冷峻黑白、全幅图片、未来感 |
| `spotify.md` | Spotify | 深色上鲜艳的绿色、粗壮字体、专辑封面驱动 |
| `uber.md` | Uber | 大胆黑白、紧凑字体、都市能量 |

## 选择设计

将设计与内容相匹配：

- **开发者工具 / 仪表板：** Linear、Vercel、Supabase、Raycast、Sentry
- **文档 / 内容站点：** Mintlify、Notion、Sanity、MongoDB
- **营销 / 落地页：** Stripe、Framer、Apple、SpaceX
- **深色模式 UI：** Linear、Cursor、ElevenLabs、Warp、Superhuman
- **明亮 / 干净 UI：** Vercel、Stripe、Notion、Cal.com、Replicate
- **活泼 / 亲切：** PostHog、Figma、Lovable、Zapier、Miro
- **高端 / 奢华：** Apple、BMW、Stripe、Superhuman、Revolut
- **数据密集 / 仪表板：** Sentry、Kraken、Cohere、ClickHouse
- **等宽字体 / 终端美学：** Ollama、OpenCode、x.ai、VoltAgent
