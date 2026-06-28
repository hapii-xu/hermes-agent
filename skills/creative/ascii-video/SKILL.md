---
name: ascii-video
description: "ASCII 视频：将视频/音频转换为彩色 ASCII MP4/GIF。"
platforms: [linux, macos, windows]
---

# ASCII 视频制作流水线

## 何时使用

当用户请求以下内容时使用：ASCII 视频、文本艺术视频、终端风格视频、字符艺术动画、复古文本可视化、ASCII 音频可视化器、把视频转换为 ASCII 艺术、Matrix 风格效果，或任何动画 ASCII 输出。

## 包含什么

ASCII 艺术视频的制作流水线 —— 任何格式。把视频/音频/图像/生成式输入转换为彩色 ASCII 字符视频输出（MP4、GIF、图像序列）。涵盖：视频转 ASCII、音频反应音乐可视化器、生成式 ASCII 艺术动画、混合视频+音频反应、文本/歌词叠加、实时终端渲染。

## 创意标准

这是视觉艺术。ASCII 字符是媒介；电影是标准。

**在写一行代码之前**，先阐明创意概念。情绪是什么？它讲了什么视觉故事？是什么让这个项目不同于其他任何 ASCII 视频？用户的提示词只是起点 —— 用创作野心去诠释它，而非字面转录。

**首次渲染的卓越是不可妥协的。** 输出必须在不需要多轮修订的情况下视觉惊艳。如果某样东西看起来平庸、扁平，或像"AI 生成的 ASCII 艺术"，那就是错的 —— 在交付前重新思考创意概念。

**超越参考词汇。** 参考资料中的效果目录、着色器预设和调色板库是一个起始词汇。对每个项目，组合、修改并发明新模式。目录是一组颜料 —— 你来画这幅画。

**主动创作。** 当项目需要时扩展 skill 的词汇。如果参考资料中没有视觉所需的东西，就构建它。至少包含一个用户没要求但会欣赏的视觉瞬间 —— 一个转场、一个效果、一个提升整件作品的颜色选择。

**统一美学高于技术正确。** 视频中的所有场景必须感觉被一种统一的视觉语言连接 —— 共享的色温、相关的字符调色板、一致的运动词汇。一个技术上正确但每个场景用随机不同效果的视频是美学上的失败。

**密集、分层、深思熟虑。** 每一帧都应值得细看。绝不用扁平的纯黑背景。始终用多网格合成。始终有每场景的变化。始终有有意的颜色。

## 模式

| 模式 | 输入 | 输出 | 参考 |
|------|-------|--------|-----------|
| **视频转 ASCII** | 视频文件 | 源影像的 ASCII 再现 | `references/inputs.md` § Video Sampling |
| **音频反应** | 音频文件 | 由音频特征驱动的生成式视觉 | `references/inputs.md` § Audio Analysis |
| **生成式** | 无（或种子参数） | 程序化 ASCII 动画 | `references/effects.md` |
| **混合** | 视频 + 音频 | 带音频反应叠加的 ASCII 视频 | 两个输入参考 |
| **歌词/文本** | 音频 + 文本/SRT | 带视觉效果的时间文本 | `references/inputs.md` § Text/Lyrics |
| **TTS 旁白** | 文本引语 + TTS API | 带打字文本的旁白证言/引语视频 | `references/inputs.md` § TTS Integration |

## 技术栈

每个项目一个自包含的 Python 脚本。无需 GPU。

| 层 | 工具 | 用途 |
|-------|------|---------|
| 核心 | Python 3.10+、NumPy | 数学、数组运算、向量化效果 |
| 信号 | SciPy | FFT、峰值检测（音频模式） |
| 成像 | Pillow（PIL） | 字体栅格化、帧解码、图像 I/O |
| 视频 I/O | ffmpeg（CLI） | 解码输入、编码输出、混流音频 |
| 并行 | concurrent.futures | N 个 worker 做批量/片段渲染 |
| TTS | ElevenLabs API（可选） | 生成旁白片段 |
| 可选 | OpenCV | 视频帧采样、边缘检测 |

## 流水线架构

每种模式都遵循相同的 6 阶段流水线：

```
INPUT → ANALYZE → SCENE_FN → TONEMAP → SHADE → ENCODE
```

1. **INPUT** —— 加载/解码源素材（视频帧、音频采样、图像，或无）
2. **ANALYZE** —— 提取每帧特征（音频频段、视频亮度/边缘、运动向量）
3. **SCENE_FN** —— 场景函数渲染到像素画布（`uint8 H,W,3`）。通过 `_render_vf()` + 像素混合模式组合多个字符网格。见 `references/composition.md`
4. **TONEMAP** —— 基于百分位的自适应亮度归一化。见 `references/composition.md` § Adaptive Tonemap
5. **SHADE** —— 通过 `ShaderChain` + `FeedbackBuffer` 做后处理。见 `references/shaders.md`
6. **ENCODE** —— 将原始 RGB 帧管道传给 ffmpeg 做 H.264/GIF 编码

## 创意指导

### 美学维度

| 维度 | 选项 | 参考 |
|-----------|---------|-----------|
| **字符调色板** | 密度斜坡、块元素、符号、文字（片假名、希腊、符文、盲文）、项目专属 | `architecture.md` § Palettes |
| **颜色策略** | HSV、OKLAB/OKLCH、离散 RGB 调色板、自动生成的和谐色、单色、温度 | `architecture.md` § Color System |
| **背景纹理** | 正弦场、fBM 噪声、域扭曲、voronoi、反应-扩散、元胞自动机、视频 | `effects.md` |
| **主要效果** | 环形、螺旋、隧道、漩涡、波浪、干涉、极光、火焰、SDF、奇异吸引子 | `effects.md` |
| **粒子** | 火花、雪、雨、泡泡、符文、轨道、群集 boid、流场跟随者、尾迹 | `effects.md` § Particles |
| **着色器氛围** | 复古 CRT、干净现代、故障艺术、电影感、梦幻、工业、迷幻 | `shaders.md` |
| **网格密度** | xs(8px) 到 xxl(40px)，每层混合 | `architecture.md` § Grid System |
| **坐标空间** | 笛卡尔、极坐标、平铺、旋转、鱼眼、莫比乌斯、域扭曲 | `effects.md` § Transforms |
| **反馈** | 缩放隧道、彩虹尾迹、幽灵回声、旋转曼陀罗、颜色演化 | `composition.md` § Feedback |
| **遮罩** | 圆、环、渐变、文本模版、动画光圈/擦除/溶解 | `composition.md` § Masking |
| **转场** | 交叉淡入、擦除、溶解、故障切换、光圈、基于遮罩的揭示 | `shaders.md` § Transitions |

### 每小节变化

绝不在整个视频中使用相同配置。对每个小节/场景：
- **不同的背景效果**（或组合 2-3 个）
- **不同的字符调色板**（匹配情绪）
- **不同的颜色策略**（或至少不同的色相）
- **变化着色器强度**（峰值时更多泛光，安静时更多颗粒）
- **不同的粒子类型**（如果粒子活跃）

### 项目专属发明

对每个项目，至少发明以下之一：
- 匹配主题的自定义字符调色板
- 自定义背景效果（组合/修改现有构件）
- 自定义颜色调色板（匹配品牌/情绪的离散 RGB 集）
- 自定义粒子字符集
- 新颖的场景转场或视觉瞬间

不要只从目录里挑。目录是词汇 —— 你来写这首诗。

## 工作流

### 第 1 步：创意愿景

在任何代码之前，阐明创意概念：

- **情绪/氛围**：观众应该感受到什么？充满活力、冥想、混乱、优雅、不祥？
- **视觉故事**：在持续时间内发生了什么？制造紧张？转变？消散？
- **色彩世界**：暖/冷？单色？霓虹？大地色？主色相是什么？
- **字符纹理**：密集数据？稀疏星点？有机点？几何块？
- **是什么让这个不同**：让这个项目独特的那一样东西是什么？
- **情绪弧线**：场景如何推进？以能量开场，推向高潮，再收束？

把用户的提示词映射到美学选择。"舒缓 lo-fi 可视化器"所需的方方面面都与"故障赛博朋克数据流"不同。

### 第 2 步：技术设计

- **模式** —— 上述 6 种模式中的哪一种
- **分辨率** —— 横屏 1920x1080（默认）、竖屏 1080x1920、正方形 1080x1080 @ 24fps
- **硬件检测** —— 自动检测核心/内存，设置质量档位。见 `references/optimization.md`
- **小节** —— 把时间戳映射到场景函数，每个有自己的效果/调色板/颜色/着色器配置
- **输出格式** —— MP4（默认）、GIF（640x360 @ 15fps）、PNG 序列

### 第 3 步：构建脚本

单个 Python 文件。组件（带参考）：

1. **硬件检测 + 质量档位** —— `references/optimization.md`
2. **输入加载器** —— 取决于模式；`references/inputs.md`
3. **特征分析器** —— 音频 FFT、视频亮度，或合成的
4. **网格 + 渲染器** —— 带位图缓存的多密度网格；`references/architecture.md`
5. **字符调色板** —— 每项目多个；`references/architecture.md` § Palettes
6. **颜色系统** —— HSV + 离散 RGB + 和谐生成；`references/architecture.md` § Color
7. **场景函数** —— 每个返回 `canvas (uint8 H,W,3)`；`references/scenes.md`
8. **色调映射** —— 自适应亮度归一化；`references/composition.md`
9. **着色器流水线** —— `ShaderChain` + `FeedbackBuffer`；`references/shaders.md`
10. **场景表 + 分发器** —— 时间 → 场景函数 + 配置；`references/scenes.md`
11. **并行编码器** —— 带 ffmpeg 管道的 N-worker 片段渲染
12. **Main** —— 编排完整流水线

### 第 4 步：质量验证

- **先测试帧**：在完整渲染前渲染关键时间戳的单帧
- **亮度检查**：所有 ASCII 内容 `canvas.mean() > 8`。若偏暗，降低伽马
- **视觉连贯**：所有场景是否感觉属于同一视频？
- **创意愿景检查**：输出是否匹配第 1 步的概念？如果看起来平庸，回到上一步

## 关键实现说明

### 亮度 —— 用 `tonemap()`，而非线性乘法

这是头号视觉问题。黑底 ASCII 本质上偏暗。**绝不用 `canvas * N` 乘法** —— 它们裁剪高光。用自适应 tonemap：

```python
def tonemap(canvas, gamma=0.75):
    f = canvas.astype(np.float32)
    lo, hi = np.percentile(f[::4, ::4], [1, 99.5])
    if hi - lo < 10: hi = lo + 10
    f = np.clip((f - lo) / (hi - lo), 0, 1) ** gamma
    return (f * 255).astype(np.uint8)
```

流水线：`scene_fn() → tonemap() → FeedbackBuffer → ShaderChain → ffmpeg`

每场景伽马：默认 0.75，日晒 0.55，色调分离 0.50，明亮场景 0.85。暗层用 `screen` 混合（而非 `overlay`）。

### 字体单元高度

macOS Pillow：`textbbox()` 返回错误高度。用 `font.getmetrics()`：`cell_height = ascent + descent`。见 `references/troubleshooting.md`。

### ffmpeg 管道死锁

永远不要在长时间运行的 ffmpeg 上用 `stderr=subprocess.PIPE` —— 缓冲在 64KB 时填满并死锁。重定向到文件。见 `references/troubleshooting.md`。

### 字体兼容性

并非所有 Unicode 字符都能在所有字体中渲染。在初始化时验证调色板 —— 渲染每个字符，检查是否有空白输出。见 `references/troubleshooting.md`。

### 每片段架构

对于分段视频（引语、场景、章节），将每段渲染为独立片段文件，以便并行渲染和选择性重渲染。见 `references/scenes.md`。

## 性能目标

| 组件 | 预算 |
|-----------|--------|
| 特征提取 | 1-5ms |
| 效果函数 | 2-15ms |
| 字符渲染 | 80-150ms（瓶颈） |
| 着色器流水线 | 5-25ms |
| **总计** | ~100-200ms/帧 |

## 参考资料

| 文件 | 内容 |
|------|----------|
| `references/architecture.md` | 网格系统、分辨率预设、字体选择、字符调色板（20+）、颜色系统（HSV + OKLAB + 离散 RGB + 和谐生成）、`_render_vf()` helper、GridLayer 类 |
| `references/composition.md` | 像素混合模式（20 种）、`blend_canvas()`、多网格合成、自适应 `tonemap()`、`FeedbackBuffer`、`PixelBlendStack`、遮罩/模版系统 |
| `references/effects.md` | 效果构件：亮度场生成器、色相场、噪声/fBM/域扭曲、voronoi、反应-扩散、元胞自动机、SDF、奇异吸引子、粒子系统、坐标变换、时间连贯性 |
| `references/shaders.md` | `ShaderChain`、`_apply_shader_step()` 分发、38 着色器目录、音频反应缩放、转场、染色预设、输出格式编码、终端渲染 |
| `references/scenes.md` | 场景协议、`Renderer` 类、`SCENES` 表、`render_clip()`、节拍同步剪辑、并行渲染、设计模式（分层层级、方向性弧、视觉隐喻、构图技巧）、各复杂度级别的完整场景示例、场景设计清单 |
| `references/inputs.md` | 音频分析（FFT、频段、节拍）、视频采样、图像转换、文本/歌词、TTS 集成（ElevenLabs、语音分配、音频混合） |
| `references/optimization.md` | 硬件检测、质量档位、向量化模式、并行渲染、内存管理、性能预算 |
| `references/troubleshooting.md` | NumPy 广播陷阱、混合模式坑、多进程/pickle、亮度诊断、ffmpeg 问题、字体问题、常见错误 |

---

## 创意发散（仅在用户请求实验性/创意/独特输出时使用）

如果用户要求创意、实验性、惊喜或非常规输出，选择最契合的策略，并在生成代码前推演其步骤。

- **强制连接** —— 当用户想要跨领域灵感（"让它看起来有机"、"工业美学"）
- **概念融合** —— 当用户点名两样东西来组合（"海洋遇见音乐"、"太空 + 书法"）
- **斜向策略** —— 当用户最大程度开放（"给我惊喜"、"我从没见过的东西"）

### 强制连接
1. 选一个与视觉目标无关的领域（天气系统、微生物学、建筑、流体力学、纺织编织）
2. 列出其核心视觉/结构元素（侵蚀 → 渐进揭示；有丝分裂 → 分裂复制；编织 → 互锁图案）
3. 把这些元素映射到 ASCII 字符和动画模式
4. 综合 —— "侵蚀"或"结晶化"在字符网格中是什么样子？

### 概念融合
1. 命名两个不同的视觉/概念空间（例如海浪 + 乐谱）
2. 映射对应关系（波峰 = 高音、波谷 = 休止、泡沫 = 断奏）
3. 选择性融合 —— 保留最有趣的映射，丢弃牵强的
4. 发展只存在于融合中的涌现属性

### 斜向策略
1. 抽一张："Honor thy error as a hidden intention" / "Use an old idea" / "What would your closest friend do?" / "Emphasize the flaws" / "Turn it upside down" / "Only a part, not the whole" / "Reverse"
2. 针对当前的 ASCII 动画挑战诠释该指令
3. 在写代码前把横向洞察应用于视觉设计
