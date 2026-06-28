---
name: manim-video
description: "Manim CE 动画：3Blue1Brown 风格的数学/算法视频。"
version: 1.0.0
platforms: [linux, macos, windows]
---

# Manim 视频制作流水线

## 何时使用

当用户需要以下内容时使用：动画讲解、数学动画、概念可视化、算法演示、技术讲解、3Blue1Brown 风格视频，或任何包含几何/数学内容的程序化动画。使用 Manim Community Edition 创建 3Blue1Brown 风格的讲解视频、算法可视化、公式推导、架构图和数据故事。

## 创作标准

这是教育电影。每一帧都在传授知识。每一个动画都在揭示结构。

**在写下一行代码之前**，先理清叙事弧线。这段动画纠正了什么误解？"顿悟时刻"在哪里？什么样的视觉故事能带领观众从困惑走向理解？用户的提示只是起点——请以教学上的抱负来诠释它。

**几何先于代数。** 先展示形状，再展示公式。视觉记忆的编码速度快于符号记忆。当观众在看到公式之前先看到几何模式，这个公式就显得顺理成章了。

**首版即精品，不可妥协。** 输出必须清晰可读、美学统一，无需多轮修改。如果某处看起来杂乱、节奏不当，或者像"AI 生成的幻灯片"，那就是错的。

**透明度分层引导注意力。** 永远不要让所有元素都以最高亮度显示。主要元素为 1.0，上下文元素为 0.4，结构元素（坐标轴、网格）为 0.15。大脑会按层级处理视觉显著性。

**留出呼吸空间。** 每个动画之后都需要 `self.wait()`。观众需要时间来消化刚刚出现的内容。绝不要匆忙地从一个动画跳到下一个。在关键揭示后停留 2 秒永远不会浪费。

**统一的视觉语言。** 所有场景共享一套配色方案、一致的字号体系、相匹配的动画速度。一个技术上正确但每个场景都用随机不同颜色的视频，在美学上是失败的。

## 前置条件

运行 `scripts/setup.sh` 以验证所有依赖。需要：Python 3.10+、Manim Community Edition v0.20+（`pip install manim`）、LaTeX（Linux 上用 `texlive-full`，macOS 上用 `mactex`）以及 ffmpeg。参考文档基于 Manim CE v0.20.1 测试。

## 模式

| 模式 | 输入 | 输出 | 参考 |
|------|-------|--------|-----------|
| **概念讲解** | 主题/概念 | 带几何直觉的动画讲解 | `references/scene-planning.md` |
| **公式推导** | 数学表达式 | 逐步动画演示的证明 | `references/equations.md` |
| **算法可视化** | 算法描述 | 带数据结构的逐步执行过程 | `references/graphs-and-data.md` |
| **数据故事** | 数据/指标 | 动画图表、对比、计数器 | `references/graphs-and-data.md` |
| **架构图** | 系统描述 | 组件逐步出现并建立连接 | `references/mobjects.md` |
| **论文讲解** | 研究论文 | 关键发现与方法被做成动画 | `references/scene-planning.md` |
| **3D 可视化** | 3D 概念 | 旋转的曲面、参数曲线、空间几何 | `references/camera-and-3d.md` |

## 技术栈

每个项目一个 Python 脚本。无需浏览器、无需 Node.js、无需 GPU。

| 层 | 工具 | 用途 |
|-------|------|---------|
| 核心 | Manim Community Edition | 场景渲染、动画引擎 |
| 数学 | LaTeX（texlive/MiKTeX） | 通过 `MathTex` 渲染公式 |
| 视频输入输出 | ffmpeg | 场景拼接、格式转换、音频混流 |
| TTS | ElevenLabs / Qwen3-TTS（可选） | 旁白配音 |

## 流水线

```
PLAN --> CODE --> RENDER --> STITCH --> AUDIO (可选) --> REVIEW
```

1. **PLAN（规划）** — 编写 `plan.md`，包含叙事弧线、场景列表、视觉元素、配色方案、旁白脚本
2. **CODE（编码）** — 编写 `script.py`，每个场景一个类，每个都可独立渲染
3. **RENDER（渲染）** — `manim -ql script.py Scene1 Scene2 ...` 出草稿，`-qh` 出成品
4. **STITCH（拼接）** — 用 ffmpeg 把场景片段拼接成 `final.mp4`
5. **AUDIO（音频，可选）** — 通过 ffmpeg 添加旁白和/或背景音乐。参见 `references/rendering.md`
6. **REVIEW（审查）** — 渲染预览静帧，对照计划核对，再做调整

## 项目结构

```
project-name/
  plan.md                # 叙事弧线、场景拆解
  script.py              # 所有场景都在这一个文件里
  concat.txt             # ffmpeg 场景列表
  final.mp4              # 拼接后的输出
  media/                 # 由 Manim 自动生成
    videos/script/480p15/
```

## 创意方向

### 配色方案

| 配色 | 背景 | 主色 | 辅色 | 强调色 | 使用场景 |
|---------|-----------|---------|-----------|--------|----------|
| **经典 3B1B** | `#1C1C1C` | `#58C4DD` (BLUE) | `#83C167` (GREEN) | `#FFFF00` (YELLOW) | 通用数学/计算机科学 |
| **温暖学术** | `#2D2B55` | `#FF6B6B` | `#FFD93D` | `#6BCB77` | 平易近人 |
| **霓虹科技** | `#0A0A0A` | `#00F5FF` | `#FF00FF` | `#39FF14` | 系统、架构 |
| **单色** | `#1A1A2E` | `#EAEAEA` | `#888888` | `#FFFFFF` | 极简 |

### 动画速度

| 场景 | run_time | 之后的 self.wait() |
|---------|----------|-------------------|
| 标题/开场出现 | 1.5s | 1.0s |
| 关键公式揭示 | 2.0s | 2.0s |
| 变换/变形 | 1.5s | 1.5s |
| 辅助标签 | 0.8s | 0.5s |
| FadeOut 清理 | 0.5s | 0.3s |
| "顿悟时刻"揭示 | 2.5s | 3.0s |

### 字号体系

| 角色 | 字号 | 用途 |
|------|-----------|-------|
| 标题 | 48 | 场景标题、开场文字 |
| 标题 | 36 | 场景内的分节标题 |
| 正文 | 30 | 说明性文字 |
| 标签 | 24 | 标注、坐标轴标签 |
| 说明 | 20 | 字幕、附属说明 |

### 字体

**所有文字都使用等宽字体。** Manim 的 Pango 渲染器在所有字号下都会让比例字体产生错乱的字距。完整建议参见 `references/visual-design.md`。

```python
MONO = "Menlo"  # 在文件顶部定义一次

Text("Fourier Series", font_size=48, font=MONO, weight=BOLD)  # 标题
Text("n=1: sin(x)", font_size=20, font=MONO)                  # 标签
MathTex(r"\nabla L")                                            # 数学（用 LaTeX）
```

为保证可读性，`font_size` 最小取 18。

### 每个场景都要有变化

绝不要让所有场景用相同的配置。每个场景要做到：
- **主色不同** — 从配色方案中取不同颜色
- **布局不同** — 不要总是把所有东西居中
- **入场动画不同** — 在 Write、FadeIn、GrowFromCenter、Create 之间轮换
- **视觉重量不同** — 有些场景密集，有些场景留白

## 工作流程

### 第 1 步：规划（plan.md）

在写任何代码之前，先写 `plan.md`。完整模板参见 `references/scene-planning.md`。

### 第 2 步：编码（script.py）

每个场景一个类。每个场景都能独立渲染。

```python
from manim import *

BG = "#1C1C1C"
PRIMARY = "#58C4DD"
SECONDARY = "#83C167"
ACCENT = "#FFFF00"
MONO = "Menlo"

class Scene1_Introduction(Scene):
    def construct(self):
        self.camera.background_color = BG
        title = Text("Why Does This Work?", font_size=48, color=PRIMARY, weight=BOLD, font=MONO)
        self.add_subcaption("Why does this work?", duration=2)
        self.play(Write(title), run_time=1.5)
        self.wait(1.0)
        self.play(FadeOut(title), run_time=0.5)
```

关键模式：
- **每个动画都配字幕**：`self.add_subcaption("text", duration=N)` 或在 `self.play()` 上用 `subcaption="text"`
- **共享颜色常量**放在文件顶部，保证跨场景一致
- **每个场景都要设置** `self.camera.background_color`
- **干净收尾** — 场景结束时 FadeOut 所有 mobject：`self.play(FadeOut(Group(*self.mobjects)))`

### 第 3 步：渲染

```bash
manim -ql script.py Scene1_Introduction Scene2_CoreConcept  # 草稿
manim -qh script.py Scene1_Introduction Scene2_CoreConcept  # 成品
```

### 第 4 步：拼接

```bash
cat > concat.txt << 'EOF'
file 'media/videos/script/480p15/Scene1_Introduction.mp4'
file 'media/videos/script/480p15/Scene2_CoreConcept.mp4'
EOF
ffmpeg -y -f concat -safe 0 -i concat.txt -c copy final.mp4
```

### 第 5 步：审查

```bash
manim -ql --format=png -s script.py Scene2_CoreConcept  # 预览静帧
```

## 关键实现注意事项

### LaTeX 必须用原始字符串
```python
# 错误：MathTex("\frac{1}{2}")
# 正确：
MathTex(r"\frac{1}{2}")
```

### 边缘文字 buff >= 0.5
```python
label.to_edge(DOWN, buff=0.5)  # 永远不要小于 0.5
```

### 替换文字前先 FadeOut
```python
self.play(ReplacementTransform(note1, note2))  # 而不是在 note1 上面 Write(note2)
```

### 不要动画未添加的 mobject
```python
self.play(Create(circle))  # 必须先添加
self.play(circle.animate.set_color(RED))  # 然后再动画
```

## 性能目标

| 质量 | 分辨率 | FPS | 速度 |
|---------|-----------|-----|-------|
| `-ql`（草稿） | 854x480 | 15 | 5-15s/场景 |
| `-qm`（中等） | 1280x720 | 30 | 15-60s/场景 |
| `-qh`（成品） | 1920x1080 | 60 | 30-120s/场景 |

迭代时始终用 `-ql`。只在出最终成品时才用 `-qh`。

## 参考资料

| 文件 | 内容 |
|------|----------|
| `references/animations.md` | 核心动画、速率函数、组合、`.animate` 语法、节奏模式 |
| `references/mobjects.md` | 文本、形状、VGroup/Group、定位、样式、自定义 mobject |
| `references/visual-design.md` | 12 条设计原则、透明度分层、布局模板、配色方案 |
| `references/equations.md` | Manim 中的 LaTeX、TransformMatchingTex、推导模式 |
| `references/graphs-and-data.md` | 坐标轴、绘图、BarChart、动画化数据、算法可视化 |
| `references/camera-and-3d.md` | MovingCameraScene、ThreeDScene、3D 曲面、相机控制 |
| `references/scene-planning.md` | 叙事弧线、布局模板、场景过渡、规划模板 |
| `references/rendering.md` | CLI 参考、质量预设、ffmpeg、配音流程、GIF 导出 |
| `references/troubleshooting.md` | LaTeX 错误、动画错误、常见误区、调试 |
| `references/animation-design-thinking.md` | 何时动画 vs 何时静态、拆解、节奏、旁白同步 |
| `references/updaters-and-trackers.md` | ValueTracker、add_updater、always_redraw、基于时间的更新器、模式 |
| `references/paper-explainer.md` | 把研究论文转化为动画 — 流程、模板、领域模式 |
| `references/decorations.md` | SurroundingRectangle、Brace、箭头、DashedLine、Angle、标注的生命周期 |
| `references/production-quality.md` | 写代码前、渲染前、渲染后的检查清单，空间布局、配色、节奏 |

---

## 创意发散（仅在用户要求实验性/创意性/独特输出时使用）

如果用户要求创意、实验性或非常规的讲解方式，先选定一个策略并在设计动画之前把它想透。

- **SCAMPER** — 当用户想要对标准讲解换个新花样时
- **假设反转** — 当用户想挑战某个主题通常的讲授方式时

### SCAMPER 变换
拿一个标准的数学/技术可视化，对它做变换：
- **替换（Substitute）**：换掉标准的视觉隐喻（数轴 → 蜿蜒小径，矩阵 → 城市网格）
- **合并（Combine）**：融合两种讲解方式（代数 + 几何同时呈现）
- **反转（Reverse）**：反向推导 — 从结果出发，回溯到公理
- **修改（Modify）**：夸张化某个参数以显示它为何重要（10 倍学习率、1000 倍样本量）
- **消除（Eliminate）**：去掉所有符号 — 纯粹用动画和空间关系来讲解

### 假设反转
1. 列出这个主题在可视化时有哪些"标准做法"（从左到右、二维、离散步骤、形式化符号）
2. 挑出最根本的一个假设
3. 把它反转（从右到左推导、二维概念的 3D 嵌入、用连续变形代替分步、零符号）
4. 探索这种反转揭示出了标准做法所隐藏的什么
