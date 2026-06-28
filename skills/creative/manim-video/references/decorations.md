# 装饰与视觉打磨

装饰是用来标注、高亮或框住其他 mobject 的 mobject。它们把一个技术上正确的动画变成视觉上打磨过的动画。

## SurroundingRectangle

在任意 mobject 周围画一个矩形。高亮的首选：

```python
highlight = SurroundingRectangle(
    equation[2],            # 要高亮的项
    color=YELLOW,
    buff=0.15,              # 内容与边框之间的留白
    corner_radius=0.1,      # 圆角
    stroke_width=2
)
self.play(Create(highlight))
self.wait(1)
self.play(FadeOut(highlight))
```

### 围住公式的一部分

```python
eq = MathTex(r"E", r"=", r"m", r"c^2")
box = SurroundingRectangle(eq[2:], color=YELLOW, buff=0.1)  # 高亮 "mc²"
label = Text("mass-energy", font_size=18, font="Menlo", color=YELLOW)
label.next_to(box, DOWN, buff=0.2)
self.play(Create(box), FadeIn(label))
```

## BackgroundRectangle

在文字后面加半透明背景，以便在复杂场景中保持可读性：

```python
bg = BackgroundRectangle(equation, fill_opacity=0.7, buff=0.2, color=BLACK)
self.play(FadeIn(bg), Write(equation))

# 或者用 set_stroke 在文字本身上制造"衬底"效果：
label.set_stroke(BLACK, width=5, background=True)
```

对于叠在图/示意图上的文字标签，`set_stroke(background=True)` 的方式更干净。

## Brace 和 BraceLabel

用来标注图或公式各部分的花括号：

```python
brace = Brace(equation[2:4], DOWN, color=YELLOW)
brace_label = brace.get_text("these terms", font_size=20)
self.play(GrowFromCenter(brace), FadeIn(brace_label))

# 在两个指定点之间
brace = BraceBetweenPoints(point_a, point_b, direction=UP)
```

### Brace 的位置

```python
# 在一组下方
Brace(group, DOWN)
# 在一组上方
Brace(group, UP)
# 在一组左侧
Brace(group, LEFT)
# 在一组右侧
Brace(group, RIGHT)
```

## 用于标注的箭头

### 指向 mobject 的直线箭头

```python
arrow = Arrow(
    start=label.get_bottom(),
    end=target.get_top(),
    color=YELLOW,
    stroke_width=2,
    buff=0.1,                    # 箭头尖端与目标之间的间隙
    max_tip_length_to_length_ratio=0.15  # 小箭头
)
self.play(GrowArrow(arrow), FadeIn(label))
```

### 曲线箭头

```python
arrow = CurvedArrow(
    start_point=source.get_right(),
    end_point=target.get_left(),
    angle=PI/4,                  # 弧度
    color=PRIMARY
)
```

### 用箭头标注

```python
# LabeledArrow：带内置文字标签的箭头
arr = LabeledArrow(
    Text("gradient", font_size=16, font="Menlo"),
    start=point_a, end=point_b, color=RED
)
```

## DashedLine 和 DashedVMobject

```python
# 虚线（用于渐近线、辅助线、隐含连接）
asymptote = DashedLine(
    axes.c2p(2, -3), axes.c2p(2, 3),
    color=YELLOW, dash_length=0.15
)

# 把任意 VMobject 变成虚线
dashed_circle = DashedVMobject(Circle(radius=2, color=BLUE), num_dashes=30)
```

## Angle 和 RightAngle 角标

```python
line1 = Line(ORIGIN, RIGHT * 2)
line2 = Line(ORIGIN, UP * 2 + RIGHT)

# 两条线之间的角弧
angle = Angle(line1, line2, radius=0.5, color=YELLOW)
angle_value = angle.get_value()  # 弧度

# 直角标记（那个小方块）
right_angle = RightAngle(line1, Line(ORIGIN, UP * 2), length=0.3, color=WHITE)
```

## Cross（删除线）

把某物标记为错误或已废弃：

```python
cross = Cross(old_equation, color=RED, stroke_width=4)
self.play(Create(cross))
# 然后展示正确的版本
```

## Underline

```python
underline = Underline(important_text, color=ACCENT, stroke_width=3)
self.play(Create(underline))
```

## 颜色高亮工作流

### 方式 1：创建时用 t2c

```python
text = Text("The gradient is negative here", t2c={"gradient": BLUE, "negative": RED})
```

### 方式 2：创建后用 set_color_by_tex

```python
eq = MathTex(r"\nabla L = -\frac{\partial L}{\partial w}")
eq.set_color_by_tex(r"\nabla", BLUE)
eq.set_color_by_tex(r"\partial", RED)
```

### 方式 3：按下标访问子 mobject

```python
eq = MathTex(r"a", r"+", r"b", r"=", r"c")
eq[0].set_color(RED)    # "a"
eq[2].set_color(BLUE)   # "b"
eq[4].set_color(GREEN)  # "c"
```

## 组合标注

叠加多个标注以加强调：

```python
# 高亮一项，加上花括号和箭头 —— 依次进行
box = SurroundingRectangle(eq[2], color=YELLOW, buff=0.1)
brace = Brace(eq[2], DOWN, color=YELLOW)
label = brace.get_text("learning rate", font_size=18)

self.play(Create(box))
self.wait(0.5)
self.play(FadeOut(box), GrowFromCenter(brace), FadeIn(label))
self.wait(1.5)
self.play(FadeOut(brace), FadeOut(label))
```

### 标注的生命周期

标注应当遵循一种节奏：
1. **出现** —— 吸引注意力（Create、GrowFromCenter）
2. **停留** —— 观众阅读并理解（self.wait）
3. **消失** —— 为下一个内容清空舞台（FadeOut）

永远不要让标注无限期留在屏幕上 —— 一旦它的使命完成，它就成了视觉噪声。
