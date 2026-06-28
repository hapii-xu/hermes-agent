# 视觉设计原则

## 12 条核心原则

1. **几何先于代数** —— 先展示形状，再展示公式。
2. **透明度分层** —— PRIMARY=1.0、CONTEXT=0.4、GRID=0.15。通过亮度引导注意力。
3. **每场景只引入一个新想法** —— 每个场景恰好引入一个概念。
4. **空间一致性** —— 同一个概念在整个过程中占据同一屏幕区域。
5. **颜色即含义** —— 把颜色赋予概念，而非 mobject。如果速度是蓝色，它就一直是蓝色。
6. **渐进式揭示** —— 先展示最简版本，再逐步增加复杂度。
7. **变换而非替换** —— 用 Transform/ReplacementTransform 展示联系。
8. **留出呼吸空间** —— 展示新内容后最少 `self.wait(1.5)`。
9. **视觉重量平衡** —— 不要把所有东西都堆在一侧。
10. **一致的运动词汇** —— 选一小套动画类型并复用。
11. **深色背景，浅色内容** —— #1C1C1C 到 #2D2B55 的背景能最大化对比度。
12. **有意的留白** —— 让画面至少留出 15% 的空白。

## 布局模板

### FULL_CENTER
一个主元素居中，标题在上，注释在下。
最适合：单个公式、单个图、标题卡。

### LEFT_RIGHT
两个元素并排，分别在 x=-3.5 和 x=3.5。
最适合：公式 + 视觉、之前/之后、对比。

### TOP_BOTTOM
主元素在 y=1.5，辅助内容在 y=-1.5。
最适合：概念 + 例子、定理 + 各种情形。

### GRID
通过 `arrange_in_grid()` 排列多个元素。
最适合：对比矩阵、多步流程。

### PROGRESSIVE
元素逐一出现，沿 DOWN 排列，aligned_edge=LEFT。
最适合：算法、证明、分步流程。

### ANNOTATED_DIAGRAM
中心图配浮动标签，用箭头连接。
最适合：架构图、带标注的插图。

## 配色方案

### 经典 3B1B
```python
BG="#1C1C1C"; PRIMARY=BLUE; SECONDARY=GREEN; ACCENT=YELLOW; HIGHLIGHT=RED
```

### 温暖学术
```python
BG="#2D2B55"; PRIMARY="#FF6B6B"; SECONDARY="#FFD93D"; ACCENT="#6BCB77"
```

### 霓虹科技
```python
BG="#0A0A0A"; PRIMARY="#00F5FF"; SECONDARY="#FF00FF"; ACCENT="#39FF14"
```

## 字体选择

**所有文字都使用等宽字体。** Manim 的 Pango 文字渲染器在所有字号和分辨率下都会让比例字体（Helvetica、Inter、SF Pro、Arial）产生错乱的字距。字符相互重叠、间距不一致。这是 Pango 的根本性限制，不是 Manim 的 bug。

等宽字体的字符宽度固定 —— 从设计上就没有字距问题。

### 推荐字体

| 用途 | 字体 | 备选 |
|----------|------|----------|
| **所有文字（默认）** | `"Menlo"` | `"Courier New"`、`"DejaVu Sans Mono"` |
| 代码、标签 | `"JetBrains Mono"`、`"SF Mono"` | `"Menlo"` |
| 数学 | 用 `MathTex`（通过 LaTeX 渲染，不走 Pango） | — |

```python
MONO = "Menlo"  # 在文件顶部定义一次

title = Text("Fourier Series", font_size=48, color=PRIMARY, weight=BOLD, font=MONO)
label = Text("n=1: (4/pi) sin(x)", font_size=20, color=BLUE, font=MONO)
note = Text("Convergence at discontinuities", font_size=18, color=DIM, font=MONO)

# 数学 —— 始终用 MathTex，不要用 Text
equation = MathTex(r"\nabla L = \frac{\partial L}{\partial w}")
```

### 比例字体何时可接受

大号标题文字（font_size >= 48）且字符串很短（1-3 个词）时，可以使用比例字体而看不到明显的字距问题。除此之外 —— 标签、描述、多词文字、小字号 —— 都用等宽字体。

### 字体可用性

- **macOS**：Menlo（预装）、SF Mono
- **Linux**：DejaVu Sans Mono（预装）、Liberation Mono
- **跨平台**：JetBrains Mono（从 jetbrains.com 安装）

`"Menlo"` 是最安全的默认 —— macOS 上预装，Linux 系统会回退到 DejaVu Sans Mono。

### 细粒度文字控制

`Text()` 不支持 `letter_spacing` 或字距参数。要精细控制，用 `MarkupText` 配 Pango 属性：

```python
# 字母间距（Pango 单位：1/1024 个点）
MarkupText('<span letter_spacing="6000">HERMES</span>', font_size=18, font="Menlo")

# 给特定词加粗
MarkupText('This is <b>important</b>', font_size=24, font="Menlo")

# 给特定词着色
MarkupText('Red <span foreground="#FF6B6B">warning</span>', font_size=24, font="Menlo")
```

### 最小字号

`font_size=18` 是任何分辨率下可读文字的最小值。低于 18 时，字符在 `-ql` 下变模糊，即便在 `-qh` 下也几乎无法阅读。

## 视觉层级检查清单

对每一帧：
1. 要看的那"一个"东西是什么？（最亮/最大的）
2. 什么是上下文？（调暗到 0.3-0.4）
3. 什么是结构性的？（调暗到 0.15）
4. 留白够吗？（> 15%）
5. 所有文字在手机尺寸下可读吗？
