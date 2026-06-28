# 更新器与值追踪器

## 更新器解决的问题

普通动画是离散的：`self.play()` 从状态 A 走到状态 B。但如果你需要持续的关系呢 —— 一个始终悬在移动点上方的标签，或一条始终连接两点的线？

没有更新器，你就得在每次 `self.play()` 之前手动重新定位每个依赖对象。五次移动点的动画意味着五次对标签的手动重新定位。漏掉一次它就会卡在错误的位置。

更新器让你只需"声明一次"这种关系。Manim 会在"每一帧"（根据质量为 15-60 fps）调用更新器函数来强制维持这种关系，不管此刻还在发生什么。

## ValueTracker：一个隐形的方向盘

ValueTracker 是一个持有单个浮点数的隐形 Mobject。它从不出现在屏幕上。它存在的意义是让你可以"动画化"它，而其他对象则"响应"它的值。

把它想成一个滑块：把滑块从 0 拖到 5，每个连到它的对象都会实时响应。

```python
tracker = ValueTracker(0)        # 隐形，存储 0.0
tracker.get_value()              # 读：0.0
tracker.set_value(5)             # 写：瞬间跳到 5.0
tracker.animate.set_value(5)     # 动画化：平滑插值到 5.0
```

### 三步模式

每个 ValueTracker 的用法都遵循这个：

1. **创建追踪器**（那个隐形滑块）
2. **创建通过更新器"读取"追踪器"的可视对象**
3. **动画化追踪器** —— 所有依赖者自动更新

```python
# 第 1 步：创建追踪器
x_tracker = ValueTracker(1)

# 第 2 步：创建依赖对象
dot = always_redraw(lambda: Dot(axes.c2p(x_tracker.get_value(), 0), color=YELLOW))
v_line = always_redraw(lambda: axes.get_vertical_line(
    axes.c2p(x_tracker.get_value(), func(x_tracker.get_value())), color=BLUE
))
label = always_redraw(lambda: DecimalNumber(x_tracker.get_value(), font_size=24)
    .next_to(dot, UP))

self.add(dot, v_line, label)

# 第 3 步：动画化追踪器 —— 一切随之而动
self.play(x_tracker.animate.set_value(5), run_time=3)
```

## 更新器的种类

### Lambda 更新器（最常见）

每帧运行一个函数，把 mobject 本身作为参数传入：

```python
# 标签始终保持在点上方
label.add_updater(lambda m: m.next_to(dot, UP, buff=0.2))

# 线始终连接两点
line.add_updater(lambda m: m.put_start_and_end_on(
    point_a.get_center(), point_b.get_center()
))
```

### 基于时间的更新器（带 dt）

第二个参数 `dt` 是自上一帧以来的时间（60fps 下约 0.017s）：

```python
# 持续旋转
square.add_updater(lambda m, dt: m.rotate(0.5 * dt))

# 持续向右漂移
dot.add_updater(lambda m, dt: m.shift(RIGHT * 0.3 * dt))

# 振荡
dot.add_updater(lambda m, dt: m.move_to(
    axes.c2p(m.get_center()[0], np.sin(self.time))
))
```

物理模拟、持续运动、随时间变化的效果用 `dt` 更新器。

### always_redraw：每帧完整重建

每帧从零创建一个新的 mobject。比 `add_updater` 开销更大，但能处理 mobject 的结构本身变化（而不仅仅是位置/颜色）的情况：

```python
# 跟随一个正在缩放的正方形的花括号
brace = always_redraw(Brace, square, UP)

# 随函数变化而更新的曲线下面积
area = always_redraw(lambda: axes.get_area(
    graph, x_range=[0, x_tracker.get_value()], color=BLUE, opacity=0.3
))

# 重建自身文字的标签
counter = always_redraw(lambda: Text(
    f"n = {int(x_tracker.get_value())}", font_size=24, font="Menlo"
).to_corner(UR))
```

**何时用哪个：**
- `add_updater` —— 位置、颜色、不透明度变化（开销小，首选）
- `always_redraw` —— 当形状/结构本身变化时（开销大，少用）

## DecimalNumber：显示实时值

```python
# 跟踪 ValueTracker 的计数器
tracker = ValueTracker(0)
number = DecimalNumber(0, font_size=48, num_decimal_places=1, color=PRIMARY)
number.add_updater(lambda m: m.set_value(tracker.get_value()))
number.add_updater(lambda m: m.next_to(dot, RIGHT, buff=0.3))

self.add(number)
self.play(tracker.animate.set_value(100), run_time=3)
```

### Variable：带标签的版本

```python
var = Variable(0, Text("x", font_size=24, font="Menlo"), num_decimal_places=2)
self.add(var)
self.play(var.tracker.animate.set_value(PI), run_time=2)
# 显示：x = 3.14
```

## 移除更新器

```python
# 移除所有更新器
mobject.clear_updaters()

# 临时挂起（在某个会与更新器打架的动画期间）
mobject.suspend_updating()
self.play(mobject.animate.shift(RIGHT))
mobject.resume_updating()

# 移除特定更新器（如果你保存了引用）
def my_updater(m):
    m.next_to(dot, UP)
label.add_updater(my_updater)
# …… 之后 ……
label.remove_updater(my_updater)
```

## 基于动画的更新器

### UpdateFromFunc / UpdateFromAlphaFunc

这些是"动画"（传给 `self.play`），不是持久更新器：

```python
# 在动画的每一帧调用一个函数
self.play(UpdateFromFunc(mobject, lambda m: m.next_to(moving_target, UP)), run_time=3)

# 带 alpha（0 到 1）—— 适合自定义插值
self.play(UpdateFromAlphaFunc(circle, lambda m, a: m.set_fill(opacity=a)), run_time=2)
```

### turn_animation_into_updater

把一个一次性动画转换成持续更新器：

```python
from manim import turn_animation_into_updater

# 这通常只播放一次 —— 现在它永远循环
turn_animation_into_updater(Rotating(gear, rate=PI/4))
self.add(gear)
self.wait(5)  # 齿轮旋转 5 秒
```

## 实用模式

### 模式 1：点沿函数追踪

```python
tracker = ValueTracker(0)
graph = axes.plot(np.sin, x_range=[0, 2*PI], color=PRIMARY)
dot = always_redraw(lambda: Dot(
    axes.c2p(tracker.get_value(), np.sin(tracker.get_value())),
    color=YELLOW
))
tangent = always_redraw(lambda: axes.get_secant_slope_group(
    x=tracker.get_value(), graph=graph, dx=0.01,
    secant_line_color=HIGHLIGHT, secant_line_length=3
))

self.add(graph, dot, tangent)
self.play(tracker.animate.set_value(2*PI), run_time=6, rate_func=linear)
```

### 模式 2：实时曲线下面积

```python
tracker = ValueTracker(0.5)
area = always_redraw(lambda: axes.get_area(
    graph, x_range=[0, tracker.get_value()],
    color=PRIMARY, opacity=0.3
))
area_label = always_redraw(lambda: DecimalNumber(
    # 数值积分
    sum(func(x) * 0.01 for x in np.arange(0, tracker.get_value(), 0.01)),
    font_size=24
).next_to(axes, RIGHT))

self.add(area, area_label)
self.play(tracker.animate.set_value(4), run_time=5)
```

### 模式 3：连接图

```python
# 可移动的节点，边自动跟随
node_a = Dot(LEFT * 2, color=PRIMARY)
node_b = Dot(RIGHT * 2, color=SECONDARY)
edge = Line().add_updater(lambda m: m.put_start_and_end_on(
    node_a.get_center(), node_b.get_center()
))
label = Text("edge", font_size=18, font="Menlo").add_updater(
    lambda m: m.move_to(edge.get_center() + UP * 0.3)
)

self.add(node_a, node_b, edge, label)
self.play(node_a.animate.shift(UP * 2), run_time=2)
self.play(node_b.animate.shift(DOWN + RIGHT), run_time=2)
# 边和标签自动跟随
```

### 模式 4：参数探索

```python
# 探索一个参数如何改变曲线
a_tracker = ValueTracker(1)
curve = always_redraw(lambda: axes.plot(
    lambda x: a_tracker.get_value() * np.sin(x),
    x_range=[0, 2*PI], color=PRIMARY
))
param_label = always_redraw(lambda: Text(
    f"a = {a_tracker.get_value():.1f}", font_size=24, font="Menlo"
).to_corner(UR))

self.add(curve, param_label)
self.play(a_tracker.animate.set_value(3), run_time=3)
self.play(a_tracker.animate.set_value(0.5), run_time=2)
self.play(a_tracker.animate.set_value(1), run_time=1)
```

## 常见错误

1. **更新器与动画打架：** 如果一个 mobject 有一个设定其位置的更新器，而你又试图把它动画化到别处，更新器每帧都会赢。先挂起更新。

2. **简单移动用 always_redraw：** 如果你只需要重新定位，用 `add_updater`。`always_redraw` 每帧重建整个 mobject —— 用于位置追踪既昂贵又没必要。

3. **忘记加入场景：** 更新器只对已在场景中的 mobject 运行。`always_redraw` 创建了 mobject，但你仍需要 `self.add()`。

4. **更新器创建新 mobject 但不清理：** 如果你的更新器每帧创建 Text 对象，它们会堆积。用 `always_redraw`（它会处理清理）或就地更新属性。
