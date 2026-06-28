# 论文讲解工作流

如何把一篇研究论文变成动画讲解视频。

## 为什么要为论文做动画？

研究论文为精确性和完整性而优化。视频为理解和记忆而优化。这里的翻译"不是"把论文配上图朗读出来 —— 而是"提炼出核心洞见，通过视觉叙事让它变得显而易见"。

论文的任务只有一个：证明这个主张为真。视频的任务则不同：让观众理解这个主张"为什么"为真，以及它"为什么"重要。

## 谁在看？

在做任何事之前，先确定受众：

| 受众 | 前置知识 | 节奏 | 深度 |
|----------|--------------|--------|-------|
| 普通大众 | 无 | 慢，多用类比 | 只讲直觉，跳过证明 |
| 本科生 | 基础数学/计算机 | 中等，带些形式化 | 关键公式，跳过推导 |
| 研究生/研究者 | 领域知识 | 更快，更多符号 | 完整公式，勾勒证明 |

这决定了一切：用词、节奏、要动画化哪些部分、展示多少数学。

## 5 分钟模板

大多数论文讲解都符合这个结构（更长的视频按比例放大时间）：

| 部分 | 时长 | 目的 |
|---------|----------|---------|
| **钩子** | 0:00-0:30 | 令人惊讶的结果或挑衅性的问题 |
| **问题** | 0:30-1:30 | 这篇论文之前哪里坏了/缺了什么 |
| **关键洞见** | 1:30-3:00 | 核心想法，用视觉讲清楚 |
| **它如何运作** | 3:00-4:00 | 方法/算法，简化版 |
| **证据** | 4:00-4:30 | 证明它有效的关键结果 |
| **影响** | 4:30-5:00 | 为什么重要，它带来了什么 |

### 该跳过什么

- 相关工作综述 → 一句话："以往的方法做了 X，它有 Y 问题"
- 实现细节 → 除非这正是贡献所在，否则跳过
- 消融实验 → 至多展示一张图
- 证明 → 展示关键步骤，不要完整证明
- 超参数调优 → 完全跳过

### 该展开什么

- 核心洞见 → 它获得最多的屏幕时间
- 几何/视觉直觉 → 如果论文有数学，展示它的"含义"
- 之前/之后对比 → 最有说服力的证据

## 写代码前的工作流

### 关卡 1：旁白脚本

在写任何代码之前先写完整旁白。每句话都映射到一个视觉节拍。如果你写不出旁白，说明你对论文的理解还不够把它动画化。

```markdown
## 钩子（30秒）
"What if I told you that a model with 7 billion parameters can outperform
one with 70 billion — if you train it on the right data?"

## 问题（60秒）
"The standard approach is to scale up. More parameters, more compute.
[视觉：展示模型规模指数级增长的柱状图]
But Chinchilla showed us that most models are undertrained..."
```

### 关卡 2：场景列表

写完旁白后，把它拆成场景。每个场景是一个 Manim 类。

```markdown
Scene 1: 钩子 —— 用动画计数器展示惊人的数字
Scene 2: 问题 —— 模型规模柱状图不断增长
Scene 3: 关键洞见 —— 训练数据与参数对比，动画化 2D 绘图
Scene 4: 方法 —— 从左到右搭建的流水线图
Scene 5: 结果 —— 用动画柱状图做之前/之后对比
Scene 6: 收尾 —— 影响的文字
```

### 关卡 3：样式常量

在给场景写代码之前，先定义视觉语言：

```python
# style.py —— 在每个场景文件中导入
BG = "#0D1117"
PRIMARY = "#58C4DD"
SECONDARY = "#83C167"
ACCENT = "#FFFF00"
HIGHLIGHT = "#FF6B6B"
MONO = "Menlo"

# 本论文中颜色的含义
MODEL_COLOR = PRIMARY      # "模型"
DATA_COLOR = SECONDARY     # "训练数据"
BASELINE_COLOR = HIGHLIGHT # "以往的方法"
RESULT_COLOR = ACCENT      # "我们的结果"
```

## 第一性原理讲解公式

当论文中有一个关键公式时，不要只是把它摆出来 —— 要从直觉出发构建它：

### "你会怎么做？"模式

1. 用平实的语言提出问题
2. 问最简单的解法会是什么
3. 展示它为什么行不通（把失败动画化）
4. 引入论文的解法作为修复
5. "然后"再展示公式 —— 此时它显得顺理成章

```python
# 场景：为什么需要 attention（用于一篇 Transformer 论文）
# 第 1 步："我们如何让每个词都能看到其他所有词？"
# 第 2 步：展示朴素做法（全连接 = 一切都是 O(n²)）
# 第 3 步：展示它失效（信息过载，没有选择性）
# 第 4 步："如果每个词都能'选择'关注哪些词呢？"
# 第 5 步：展示 attention 公式 —— Q、K、V 此刻才有意义
```

### 公式揭示策略

```python
# 先把公式暗淡显示（完整的终点）
eq = MathTex(r"Attention(Q,K,V) = softmax\left(\frac{QK^T}{\sqrt{d_k}}\right)V")
eq.set_opacity(0.15)
self.play(FadeIn(eq))

# 逐个高亮 Q、K、V，配上颜色和标签
for part, color, label_text in [
    (r"Q", PRIMARY, "Query: what am I looking for?"),
    (r"K", SECONDARY, "Key: what do I contain?"),
    (r"V", ACCENT, "Value: what do I output?"),
]:
    eq.set_color_by_tex(part, color)
    label = Text(label_text, font_size=18, color=color, font=MONO)
    # 定位标签，动画化它，等待，然后调暗它
```

## 搭建架构图

### 渐进搭建模式

不要一次性展示完整架构。要逐步搭建：

1. 第一个组件单独出现 → 讲解
2. 箭头长出 → "它流入……"
3. 第二个组件出现 → 讲解
4. 重复直到完成

```python
# 组件工厂
def make_box(label, color, width=2.0, height=0.8):
    box = RoundedRectangle(corner_radius=0.1, width=width, height=height,
                           color=color, fill_opacity=0.1, stroke_width=1.5)
    text = Text(label, font_size=18, font=MONO, color=color).move_to(box)
    return Group(box, text)

encoder = make_box("Encoder", PRIMARY)
decoder = make_box("Decoder", SECONDARY).next_to(encoder, RIGHT, buff=1.5)
arrow = Arrow(encoder.get_right(), decoder.get_left(), color=DIM, stroke_width=1.5)

self.play(FadeIn(encoder))
self.wait(1)  # 讲解 encoder
self.play(GrowArrow(arrow))
self.play(FadeIn(decoder))
self.wait(1)  # 讲解 decoder
```

### 数据流动画

搭好图之后，展示数据在其中流动：

```python
# 沿流水线移动的点
data_dot = Dot(color=ACCENT, radius=0.1).move_to(encoder)
self.play(FadeIn(data_dot))
self.play(MoveAlongPath(data_dot, arrow), run_time=1)
self.play(data_dot.animate.move_to(decoder), run_time=0.5)
self.play(Flash(data_dot.get_center(), color=ACCENT), run_time=0.3)
```

## 动画化结果

### 柱状图对比（最常见）

```python
# 之前/之后的柱子
before_data = [45, 52, 38, 61]
after_data = [78, 85, 72, 91]
labels = ["Task A", "Task B", "Task C", "Task D"]

before_chart = BarChart(before_data, bar_names=labels,
    y_range=[0, 100, 20], bar_colors=[HIGHLIGHT]*4).scale(0.6).shift(LEFT*3)
after_chart = BarChart(after_data, bar_names=labels,
    y_range=[0, 100, 20], bar_colors=[SECONDARY]*4).scale(0.6).shift(RIGHT*3)

before_label = Text("Baseline", font_size=20, color=HIGHLIGHT, font=MONO)
after_label = Text("Ours", font_size=20, color=SECONDARY, font=MONO)

# 先揭示基线，再揭示我们的（戏剧性对比）
self.play(Create(before_chart), FadeIn(before_label))
self.wait(1.5)
self.play(Create(after_chart), FadeIn(after_label))
self.wait(0.5)

# 高亮提升幅度
improvement = Text("+35% avg", font_size=24, color=ACCENT, font=MONO)
self.play(FadeIn(improvement))
```

### 训练曲线（用于 ML 论文）

```python
tracker = ValueTracker(0)
curve = always_redraw(lambda: axes.plot(
    lambda x: 1 - 0.8 * np.exp(-x / 3),
    x_range=[0, tracker.get_value()], color=PRIMARY
))
epoch_label = always_redraw(lambda: Text(
    f"Epoch {int(tracker.get_value())}", font_size=18, font=MONO
).to_corner(UR))

self.add(curve, epoch_label)
self.play(tracker.animate.set_value(10), run_time=5, rate_func=linear)
```

## 领域专属模式

### ML 论文
- 展示数据在模型中流动（动画化的流水线）
- 用 `ValueTracker` 做训练曲线
- 把 attention 热力图做成彩色网格
- 把嵌入空间做成 2D 散点（PCA/t-SNE 可视化）
- 把损失地形做成 3D 曲面，配一个梯度下降的点

### 物理/数学论文
- 线性代数用 `LinearTransformationScene`
- 用 `ArrowVectorField` / `StreamLines` 做向量场
- 用 `NumberPlane` + 轨迹做相空间
- 带时间参数的绘图做波动方程

### 系统/架构论文
- 渐进搭建的流水线图
- 沿箭头的数据流用 `ShowPassingFlash`
- 缩放到组件内部用 `ZoomedScene`
- 之前/之后的延迟/吞吐对比

## 常见错误

1. **想覆盖整篇论文。** 一段 5 分钟的视频能"讲好一个"核心洞见。什么都想覆盖等于什么都没讲清。
2. **把摘要当旁白念。** 学术写作是给读者看的，不是给听众听的。改写成口语化的语言。
3. **只摆符号不给含义。** 永远不要在先用视觉展示一个符号代表什么之前就把它摆出来。
4. **跳过动机。** 直接跳到"这是我们的方法"，却不展示这个问题为什么重要。问题部分才让观众在意。
5. **从头到尾节奏一模一样。** 钩子和关键洞见需要最多的视觉能量。方法部分可以快一些。证据要落地有力（展示出大数字后停一下）。
