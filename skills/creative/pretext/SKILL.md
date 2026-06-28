---
name: pretext
description: "用于配合 @chenglou/pretext 构建创意浏览器 demo —— 无 DOM 的文本排版，适用于 ASCII 艺术、围绕障碍物的排版流、文字即几何的游戏、动态字体（kinetic typography）以及文本驱动的生成艺术。默认产出单文件 HTML demo。"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [creative-coding, typography, pretext, ascii-art, canvas, generative, text-layout, kinetic-typography]
    related_skills: [p5js, claude-design, excalidraw, architecture-diagram]
---

# Pretext 创意 Demo

## 概览

[`@chenglou/pretext`](https://github.com/chenglou/pretext) 是由 Cheng Lou（React 核心、ReasonML、Midjourney）开发的一个 15KB、零依赖的 TypeScript 库，用于**无 DOM 的多行文本测量与排版**。它只做一件事：给定 `(text, font, width)`，返回换行结果、每行宽度、每个字形素（grapheme）的位置以及总高度——全部通过 canvas 测量完成，不会触发 reflow。

这听起来像是底层管线。其实不是。因为它又快又是几何化的，所以它是一个**创意原语**：你可以让段落以 60fps 围绕一个移动的精灵重新流动，构建关卡几何由真实单词组成的游戏，让 ASCII logo 穿过正文，用精确的每个字形素起始位置把文字炸成粒子，或者在没有任何 `getBoundingClientRect` 抖动的情况下打包出紧贴边界的多行 UI。

这个技能存在的意义是让 Hermes 能用它做出**酷炫的 demo**——那种人们会发到 X 上的东西。社区 demo 集合见 `pretext.cool` 和 `chenglou.me/pretext`。

## 何时使用

当用户要求以下内容时使用：
- 一个「pretext demo」/「酷炫的 pretext 玩意」/「文字即 X」
- 文字围绕一个移动形状流动（hero 区、编辑式排版、动画长文页面）
- 使用**真实单词或正文**的 ASCII 艺术效果，而非等宽栅格
- 游玩场地 / 障碍物 / 砖块由文字组成的游戏（字母俄罗斯方块、正文打砖块）
- 带逐字物理效果的动态字体（炸开、散开、群聚、流动）
- 字体生成艺术，尤其是非拉丁文字或混排文字
- 多行「紧贴边界」UI（仍能容纳文本的最小容器宽度）
- 任何需要在渲染*之前*就知道换行位置的场景

不要用于：
- CSS 已经解决排版的静态 SVG/HTML 页面——直接用 CSS
- 富文本编辑器、通用内联格式化引擎（pretext 有意做得狭窄）
- 图像 → 文本（用 `ascii-art` / `ascii-video` 技能）
- 没有文字角色的纯 canvas 生成艺术——用 `p5js`

## 创意标准

这是在浏览器中渲染的视觉艺术。Pretext 返回数字；**你**来画东西。

- **不要交付一个「hello world」demo。** `hello-orb-flow.html` 模板是*起点*。每个交付的 demo 都必须加上有意的配色、运动、构图，以及一个用户没要求但会欣赏的视觉细节。
- **深色背景、暖色核心、考究的调色板。** 经典的琥珀色配黑（CRT / 终端）可行，冷白配炭灰（编辑式）和去饱和的粉彩（孔版印刷感）也行。选一个并坚持下去。
- **比例字体才是重点。** Pretext 整体的调性就是「不等宽」——拥抱它。用 Iowan Old Style、Inter、JetBrains Mono、Helvetica Neue 或可变字体。永远不要用默认无衬线。
- **真实的源文本/语料，不要 lorem ipsum。** 语料应当有所指。简短的宣言、诗歌、真实源代码、找到的文本、库自己的 README——永远不要 `lorem ipsum`。
- **首屏即出色。** 不要加载态、不要空白帧。demo 打开的瞬间就必须看起来可交付。

## 技术栈

每个 demo 一个自包含的 HTML 文件。没有构建步骤。

| 层 | 工具 | 用途 |
|-------|------|---------|
| 核心 | 通过 `esm.sh` CDN 的 `@chenglou/pretext` | 文本测量 + 行排版 |
| 渲染 | HTML5 Canvas 2D | 字形渲染、逐帧合成 |
| 分词 | `Intl.Segmenter`（内建） | 为 emoji / CJK / 组合标记做字形素拆分 |
| 交互 | 原生 DOM 事件 | 鼠标 / 触摸 / 滚轮——不用框架 |

```html
<script type="module">
import {
  prepare, layout,                   // 用例 1：简单求高度
  prepareWithSegments, layoutWithLines,  // 用例 2a：定宽行
  layoutNextLineRange, materializeLineRange, // 用例 2b：流式 / 变宽
  measureLineStats, walkLineRanges,  // 不分配字符串的统计
} from "https://esm.sh/@chenglou/pretext@0.0.6";
</script>
```

锁定版本。撰写本文时为 `@0.0.6`——如果 demo 行为不对，去 [npm](https://www.npmjs.com/package/@chenglou/pretext) 查最新版本。

## 两种用例

几乎所有东西都能归结为这两种形状之一。两种都要学会。

### 用例 1 —— 测量，然后用 CSS/DOM 渲染

```js
const prepared = prepare(text, "16px Inter");
const { height, lineCount } = layout(prepared, 320, 20);
```

你仍然让浏览器画文字。Pretext 只是告诉你，在给定宽度下盒子会有多高，**无需**读取 DOM。用于：
- 行里包含换行文本的虚拟列表
- 卡片高度精确的瀑布流
- 「这个标签放得下吗？」的开发期检查
- 远程文本加载时防止布局偏移

**保持 `font` 和 `letterSpacing` 与你的 CSS 完全同步。** canvas 的 `ctx.font` 格式（如 `"16px Inter"`、`"500 17px 'JetBrains Mono'"`）必须与渲染用的 CSS 一致，否则测量会漂移。

### 用例 2 —— 自己测量*并*渲染

```js
const prepared = prepareWithSegments(text, FONT);
const { lines } = layoutWithLines(prepared, 320, 26);
for (let i = 0; i < lines.length; i++) {
  ctx.fillText(lines[i].text, 0, i * 26);
}
```

这才是创意活儿所在。你掌控绘制，所以你可以：
- 渲染到 canvas、SVG、WebGL 或任何坐标系
- 替换逐字变换（旋转、抖动、缩放、不透明度）
- 把行元数据（宽度、字形素位置）当作几何来用

对于**逐行变宽**的流动（文字绕一个形状、文字在甜甜圈环带里、文字在非矩形列里）：

```js
let cursor = { segmentIndex: 0, graphemeIndex: 0 };
let y = 0;
while (true) {
  const lineWidth = widthAtY(y);  // 你的函数：在这个 y 处走廊有多宽？
  const range = layoutNextLineRange(prepared, cursor, lineWidth);
  if (!range) break;
  const line = materializeLineRange(prepared, range);
  ctx.fillText(line.text, leftEdgeAtY(y), y);
  cursor = range.end;
  y += lineHeight;
}
```

这是整个库里最重要的模式。正是它解锁了「文字围绕拖拽的精灵流动」——那个在 X 上疯传的 demo。

### 值得了解的辅助函数

- `measureLineStats(prepared, maxWidth)` → `{ lineCount, maxLineWidth }` ——最宽的行，即多行紧贴边界的宽度。
- `walkLineRanges(prepared, maxWidth, callback)` ——不分配字符串地遍历行。当你不需要字符、只对字形素做统计/物理时使用。
- `@chenglou/pretext/rich-inline` ——同一套系统，但用于混排字体 / 标签 / 提及的段落。从该子路径导入。

## Demo 配方模式

社区语料（见 `references/patterns.md`）聚集成少数几种强势模式。选一种并即兴发挥——除非被要求，否则不要发明新类别。

| 模式 | 关键 API | 示例点子 |
|---|---|---|
| **围绕障碍物回流** | `layoutNextLineRange` + 逐行宽度函数 | 围绕拖拽的光标精灵分开的编辑式段落 |
| **文字即几何的游戏** | `layoutWithLines` + 逐行碰撞矩形 | 每块砖都是一个被测量单词的打砖块 |
| **炸裂 / 粒子** | `walkLineRanges` → 每个字形素 (x,y) → 物理 | 点击即炸成字母的句子 |
| **ASCII 障碍物排版** | `layoutNextLineRange` + 测量出的逐行障碍物区间 | 位图 ASCII logo、形状变形，以及让文字按其真实几何打开的可拖拽线框物体 |
| **编辑式多栏** | 每栏 `layoutNextLineRange` + 共享游标 | 带引言的动画杂志跨页 |
| **动态字体** | `layoutWithLines` + 随时间逐行变换 | 星战爬行字幕、波浪、弹跳、故障 |
| **多行紧贴边界** | `measureLineStats` | 自动缩放到最紧容器的引言卡片 |

工作示例的单文件起点见 `templates/donut-orbit.html` 和 `templates/hello-orb-flow.html`。

## 工作流

1. 根据用户简报从上表中**选一个模式**。
2. **从一个模板起步**：
   - `templates/hello-orb-flow.html` —— 文字围绕移动的球回流（围绕障碍物回流模式）
   - `templates/donut-orbit.html` —— 进阶示例：测量过的 ASCII logo 障碍物、可拖拽的线框球/立方体、变形的形状场、可选的 DOM 文本，以及仅开发用的控件
   - 用 `write_file` 写到 `/tmp/` 或用户工作区里的一个新 `.html`。
3. **替换语料**为对简报有意义的内容。真实正文，10-100 句，不要 lorem。
4. **调优美学** —— 字体、调色板、构图、交互。这才是活儿，别跳过。
5. **本地验证**：
   ```sh
   cd <dir-with-html> && python3 -m http.server 8765
   # 然后打开 http://localhost:8765/<file>.html
   ```
6. **检查控制台** —— 如果 `prepareWithSegments` 被传入了错误的 font 字符串，pretext 会抛错；`Intl.Segmenter` 在每个现代浏览器里都可用。
7. **给用户看文件路径**，不只是给代码——他们想打开它。

## 性能注意事项

- `prepare()` / `prepareWithSegments()` 是昂贵的调用。每个 text+font 对**只**做一次。缓存句柄。
- 在 resize 时，只重跑 `layout()` / `layoutWithLines()` —— 永远不要重新 prepare。
- 对于文字不变但几何变的逐帧动画，在紧凑循环里跑 `layoutNextLineRange` 足够便宜，可以在 60fps 下每帧跑（针对正常长度的段落）。
- 当逐帧渲染 ASCII 掩码时，保留一个 cell 缓冲（`Uint8Array`/类型化数组），从这些 cell 或投影几何推导出测量过的逐行障碍物区间，合并区间，然后在画文字前把这些区间喂给 `layoutNextLineRange`。
- 让视觉动画和排版动画保持耦合。如果一个球变形为立方体，用同一个值同时补间渲染的 cell 缓冲和障碍物区间；否则 demo 看起来像是贴上去的，而不是物理地回流。
- 对于淡入淡出，优先用图层不透明度，而不是改变字形强度或障碍物缩放。把临时的 ASCII 精灵放在它们自己的 canvas 上，用 CSS/GSAP 的不透明度淡入淡出 canvas，这样几何就不会显得在缩小。
- Canvas 的 `ctx.font` 设置出奇地慢；当字体不变时，**每帧只设一次**，而不是每次 `fillText` 都设。

## 常见陷阱

1. **CSS/canvas 的 font 字符串漂移。** 测量用的是 `ctx.font = "16px Inter"`，但 CSS 写的是 `font-family: Inter, sans-serif; font-size: 16px`。如果 Inter 加载成功，这没问题。如果 Inter 404 了，CSS 会回退到 sans-serif，测量会漂移 5-20%。始终 `preload` 字体或使用 web 安全的字体族。

2. **在动画循环里重新 prepare。** 只有 `layout*` 是便宜的。每帧重跑 `prepare` 会拖垮性能。把 prepared 句柄留在模块作用域里。

3. **字形素拆分忘了 `Intl.Segmenter`。** emoji、组合标记、CJK——`"é".split("")` 会给你两个字符。在采样单个可见字形时用 `new Intl.Segmenter(undefined, { granularity: "grapheme" })`。

4. **`break: 'never'` 的标签没给 `extraWidth`。** 在 `rich-inline` 中，如果你为原子化的标签/提及用 `break: 'never'`，你必须同时给出 `extraWidth` 作为药丸标签的内边距——否则标签的边框会溢出容器。

5. **从 `unpkg` 用 TypeScript-only 入口加载 `@chenglou/pretext`。** 用 `esm.sh`——它会自动把 TS 导出编译成浏览器可用的 ESM。`unpkg` 会 404 或返回原始 TS。

6. **等宽回退悄悄抹掉了全部意义。** 用户看到等宽输出时，往往是 CSS `font-family` 漏到了 `monospace`。用 DevTools 验证实际渲染的字体。

7. **绕形状流动时跳过行 vs 调整宽度。** 如果这一行的走廊太窄放不下一行，*跳过该行*（`y += lineHeight; continue;`），而不是把一个极小的 maxWidth 传给 `layoutNextLineRange`——pretext 会返回只有一个字形素的行，看起来是坏的。

8. **交付一个冷冰冰的 demo。** 默认首屏看起来像教程级别。加上：晕影、细微的扫描线、空闲自动运动、一个精心挑选的交互响应（拖拽、悬停、滚动、点击）。没有这些，「酷炫的 pretext demo」就会变成「实习生对 README 的复刻」。

## 验证清单

- [ ] demo 是一个自包含的单 `.html` 文件——双击或用 `python3 -m http.server` 即可打开
- [ ] `@chenglou/pretext` 通过 `esm.sh` 导入并锁定版本
- [ ] 语料是真实正文，不是 lorem ipsum，且与 demo 的概念匹配
- [ ] 传给 `prepare` 的 font 字符串与 CSS 字体完全一致
- [ ] `prepare()` / `prepareWithSegments()` 只调用一次，而非每帧
- [ ] 深色背景 + 考究的调色板——不是默认的白色 canvas
- [ ] 至少有一个交互响应（拖拽 / 悬停 / 滚动 / 点击）或空闲自动运动
- [ ] 用 `python3 -m http.server` 本地测试过，确认没有控制台报错
- [ ] 在中端笔记本上达到 60fps（或有文档化的优雅降级）
- [ ] 一个用户没要求的「多走一英里」细节

## 参考：社区 Demo

为灵感 / 模式克隆这些（都是 MIT 之类许可，链接来自 [pretext.cool](https://www.pretext.cool/)）：

- **Pretext Breaker** —— 用单词做砖块的打砖块 —— `github.com/rinesh/pretext-breaker`
- **Tetris × Pretext** —— `github.com/shinichimochizuki/tetris-pretext`
- **Dragon animation** —— `github.com/qtakmalay/PreTextExperiments`
- **Somnai editorial engine** —— `github.com/somnai-dreams/pretext-demos`
- **Bad Apple!! ASCII** —— `github.com/frmlinn/bad-apple-pretext`
- **Drag-sprite reflow** —— `github.com/dokobot/pretext-demo`
- **Alarmy editorial clock** —— `github.com/SmisLee/alarmy-pretext-demo`

官方实验场：[chenglou.me/pretext](https://chenglou.me/pretext/) —— 手风琴、气泡、动态排版、editorial-engine、对齐对比、瀑布流、markdown-chat、rich-note。
