# 动画参考

## 核心概念

动画是一个 Python 对象，它随时间计算一个 mobject 的中间视觉状态。动画是传给 `self.play()` 的对象，而不是函数。

`run_time` 控制秒数（默认值：1）。对重要动画，务必显式指定它。

## 创建类动画

```python
self.play(Create(circle))          # 描出轮廓
self.play(Write(equation))         # 模拟手写（用于 Text/MathTex）
self.play(FadeIn(group))           # 不透明度 0 -> 1
self.play(GrowFromCenter(dot))     # 从中心缩放 0 -> 1
self.play(DrawBorderThenFill(sq))  # 先画轮廓，再填充
```

## 移除类动画

```python
self.play(FadeOut(mobject))         # 不透明度 1 -> 0
self.play(Uncreate(circle))        # Create 的反向
self.play(ShrinkToCenter(group))   # 缩放 1 -> 0
```

## 变换类动画

```python
# Transform —— 原地修改原始对象
self.play(Transform(circle, square))
# 之后：circle 就是 square（同一个对象，新外观）

# ReplacementTransform —— 用新对象替换旧对象
self.play(ReplacementTransform(circle, square))
# 之后：circle 被移除，square 出现在屏幕上

# TransformMatchingTex —— 智能的公式变形
eq1 = MathTex(r"a^2 + b^2")
eq2 = MathTex(r"a^2 + b^2 = c^2")
self.play(TransformMatchingTex(eq1, eq2))
```

**关键**：`Transform(A, B)` 之后，变量 `A` 引用的是屏幕上的 mobject。变量 `B` 不在屏幕上。如果你之后想操作 `B`，请用 `ReplacementTransform`。

## .animate 语法

```python
self.play(circle.animate.set_color(RED))
self.play(circle.animate.shift(RIGHT * 2).scale(0.5))  # 链式调用多个
```

## 其他创建类动画

```python
self.play(GrowFromPoint(circle, LEFT * 3))     # 从指定点缩放 0 -> 1
self.play(GrowFromEdge(rect, DOWN))             # 从某条边生长
self.play(SpinInFromNothing(square))            # 旋转着放大（默认 PI/2）
self.play(GrowArrow(arrow))                     # 箭头从起点长到尖端
```

## 移动类动画

```python
# 让 mobject 沿任意路径移动
path = Arc(radius=2, angle=PI)
self.play(MoveAlongPath(dot, path), run_time=2)

# Rotate（作为 Transform，不是 .animate —— 支持 about_point）
self.play(Rotate(square, angle=PI / 2, about_point=ORIGIN), run_time=1.5)

# Rotating（持续旋转，更新器风格 —— 适合旋转物体）
self.play(Rotating(gear, angle=TAU, run_time=4, rate_func=linear))
```

`MoveAlongPath` 接受任何 `VMobject` 作为路径 —— 可用 `Arc`、`CubicBezier`、`Line`，或自定义的 `VMobject`。位置通过 `path.point_from_proportion()` 计算。

## 强调类动画

```python
self.play(Indicate(mobject))             # 短暂黄色闪烁 + 缩放
self.play(Circumscribe(mobject))         # 在它周围画一个矩形
self.play(Flash(point))                  # 放射状闪光
self.play(Wiggle(mobject))               # 左右摇晃
```

## 速率函数

```python
self.play(FadeIn(mob), rate_func=smooth)          # 默认：缓入缓出
self.play(FadeIn(mob), rate_func=linear)           # 匀速
self.play(FadeIn(mob), rate_func=rush_into)        # 起慢终快
self.play(FadeIn(mob), rate_func=rush_from)        # 起快终慢
self.play(FadeIn(mob), rate_func=there_and_back)   # 动画后反向播放
```

## 组合

```python
# 同时播放
self.play(FadeIn(title), Create(circle), run_time=2)

# 带延迟的 AnimationGroup
self.play(AnimationGroup(*[FadeIn(i) for i in items], lag_ratio=0.2))

# LaggedStart
self.play(LaggedStart(*[Write(l) for l in lines], lag_ratio=0.3, run_time=3))

# Succession（在一次 play 调用中顺序播放）
self.play(Succession(FadeIn(title), Wait(0.5), Write(subtitle)))
```

## 更新器

```python
tracker = ValueTracker(0)
dot = Dot().add_updater(lambda m: m.move_to(axes.c2p(tracker.get_value(), 0)))
self.play(tracker.animate.set_value(5), run_time=3)
```

## 字幕

```python
# 方式 1：独立添加
self.add_subcaption("Key insight", duration=2)
self.play(Write(equation), run_time=2.0)

# 方式 2：内联
self.play(Write(equation), subcaption="Key insight", subcaption_duration=2)
```

Manim 会自动生成 `.srt` 字幕文件。为提升可访问性，请始终添加 subcaption。

## 节奏模式

```python
# 揭示后停顿
self.play(Write(key_equation), run_time=2.0)
self.wait(2.0)

# 暗化并聚焦
self.play(old_content.animate.set_opacity(0.3), FadeIn(new_content))

# 干净收尾
self.play(FadeOut(Group(*self.mobjects)), run_time=0.5)
self.wait(0.3)
```

## 响应式 mobject：always_redraw()

每帧从零重建一个 mobject —— 当它的几何依赖其他被动画化的对象时不可或缺：

```python
# 跟随一个正在缩放的正方形的花括号
brace = always_redraw(Brace, square, UP)
self.add(brace)
self.play(square.animate.scale(2))  # 花括号自动调整

# 跟踪一个移动点的水平线
h_line = always_redraw(lambda: axes.get_h_line(dot.get_left()))

# 始终紧贴另一个 mobject 的标签
label = always_redraw(lambda: Text("here", font_size=20).next_to(dot, UP, buff=0.2))
```

注意：`always_redraw` 每帧重建 mobject。对于简单的属性跟踪，请改用 `add_updater`（开销更小）：
```python
label.add_updater(lambda m: m.next_to(dot, UP))
```

## TracedPath —— 轨迹追踪

绘制一个点所经过的路径：

```python
dot = Dot(color=YELLOW)
path = TracedPath(dot.get_center, stroke_color=YELLOW, stroke_width=2)
self.add(dot, path)
self.play(dot.animate.shift(RIGHT * 3 + UP * 2), run_time=2)
# path 显示出点留下的轨迹

# 渐隐的轨迹（随时间消散）：
path = TracedPath(dot.get_center, dissipating_time=0.5, stroke_opacity=[0, 1])
```

适用场景：梯度下降路径、行星轨道、函数追踪、粒子轨迹。

## FadeTransform —— 更平滑的交叉淡入淡出

`Transform` 通过难看的中间扭曲来变形形状。`FadeTransform` 带位置匹配地交叉淡入淡出 —— 当源对象和目标对象外观差异较大时使用它：

```python
# 难看：Transform 把圆扭曲成一团再变成正方形
self.play(Transform(circle, square))

# 平滑：FadeTransform 干净地交叉淡入淡出
self.play(FadeTransform(circle, square))

# FadeTransformPieces：按子 mobject 进行 FadeTransform
self.play(FadeTransformPieces(group1, group2))

# TransformFromCopy：动画化一个副本，同时保持原件可见
self.play(TransformFromCopy(source, target))
# source 留在屏幕上，一个副本变形为 target
```

**建议：** 对外观差异大的形状，默认用 `FadeTransform`。只有相似形状（圆→椭圆、公式→公式）才用 `Transform`/`ReplacementTransform`。

## ApplyMatrix —— 线性变换可视化

在 mobject 上动画化一个矩阵变换：

```python
# 对网格应用一个 2x2 矩阵
matrix = [[2, 1], [1, 1]]
self.play(ApplyMatrix(matrix, number_plane), run_time=2)

# 也适用于单个 mobject
self.play(ApplyMatrix([[0, -1], [1, 0]], square))  # 旋转 90 度
```

可与 `LinearTransformationScene` 配合 —— 参见 `camera-and-3d.md`。

## squish_rate_func —— 时间窗口内的错峰

把任何速率函数压缩到一个动画内的某个时间窗口中。从而无需 `LaggedStart` 即可实现重叠错峰：

```python
self.play(
    FadeIn(a, rate_func=squish_rate_func(smooth, 0, 0.5)),    # 0% 到 50%
    FadeIn(b, rate_func=squish_rate_func(smooth, 0.25, 0.75)), # 25% 到 75%
    FadeIn(c, rate_func=squish_rate_func(smooth, 0.5, 1.0)),  # 50% 到 100%
    run_time=2
)
```

当你需要精确控制重叠时，比 `LaggedStart` 更精准。

## 其他速率函数

```python
from manim import (
    smooth, linear, rush_into, rush_from,
    there_and_back, there_and_back_with_pause,
    running_start, double_smooth, wiggle,
    lingering, exponential_decay, not_quite_there,
    squish_rate_func
)

# running_start：先向后拉再向前（预期动作）
self.play(FadeIn(mob, rate_func=running_start))

# there_and_back_with_pause：到那里，停住，再回来
self.play(mob.animate.shift(UP), rate_func=there_and_back_with_pause)

# not_quite_there：停在整段动画的某个比例处
self.play(FadeIn(mob, rate_func=not_quite_there(0.7)))
```

## ShowIncreasingSubsets / ShowSubmobjectsOneByOne

逐个揭示组的成员 —— 非常适合算法可视化：

```python
# 逐个揭示数组元素
array = Group(*[Square() for _ in range(8)]).arrange(RIGHT)
self.play(ShowIncreasingSubsets(array), run_time=3)

# 错峰出现子 mobject
self.play(ShowSubmobjectsOneByOne(code_lines), run_time=4)
```

## ShowPassingFlash

一束闪光沿路径划过：

```python
# 沿曲线划过的闪光
self.play(ShowPassingFlash(curve.copy().set_color(YELLOW), time_width=0.3))

# 非常适合：数据流、电信号、网络流量
```
