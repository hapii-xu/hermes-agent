# Mobject 参考

屏幕上一切可见的东西都是 Mobject。它们有位置、颜色、不透明度，并且可以被动画化。

## 文本

```python
title = Text("Hello World", font_size=48, color=BLUE)
eq = MathTex(r"E = mc^2", font_size=40)

# 多段（用于选择性着色）
eq = MathTex(r"a^2", r"+", r"b^2", r"=", r"c^2")
eq[0].set_color(RED)
eq[4].set_color(BLUE)

# 文字与数学混排
t = Tex(r"The area is $\pi r^2$", font_size=36)

# 带样式的标记文本
t = MarkupText('<span foreground="#58C4DD">Blue</span> text', font_size=30)
```

**对于任何含反斜杠的字符串，都要用原始字符串（`r""`）。**

## 形状

```python
circle = Circle(radius=1, color=BLUE, fill_opacity=0.5)
square = Square(side_length=2, color=RED)
rect = Rectangle(width=4, height=2, color=GREEN)
dot = Dot(point=ORIGIN, radius=0.08, color=YELLOW)
line = Line(LEFT * 2, RIGHT * 2, color=WHITE)
arrow = Arrow(LEFT, RIGHT, color=ORANGE)
rrect = RoundedRectangle(corner_radius=0.3, width=4, height=2)
brace = Brace(rect, DOWN, color=YELLOW)
```

## 多边形与弧

```python
# 由顶点构成的任意多边形
poly = Polygon(LEFT, UP * 2, RIGHT, color=GREEN, fill_opacity=0.3)

# 正 n 边形
hexagon = RegularPolygon(n=6, color=TEAL, fill_opacity=0.4)

# 三角形（RegularPolygon(n=3) 的简写）
tri = Triangle(color=YELLOW, fill_opacity=0.5)

# 弧（圆的一部分）
arc = Arc(radius=2, start_angle=0, angle=PI / 2, color=BLUE)

# 两点之间的弧
arc_between = ArcBetweenPoints(LEFT * 2, RIGHT * 2, angle=TAU / 4, color=RED)

# 曲线箭头（带尖端的弧）
curved_arrow = CurvedArrow(LEFT * 2, RIGHT * 2, color=ORANGE)
```

## 扇形与圆环

```python
# 扇形（饼状切片）
sector = Sector(outer_radius=2, start_angle=0, angle=PI / 3, fill_opacity=0.7, color=BLUE)

# 圆环（环）
ring = Annulus(inner_radius=1, outer_radius=2, fill_opacity=0.5, color=GREEN)

# 环形扇区（部分环）
partial_ring = AnnularSector(
    inner_radius=1, outer_radius=2,
    angle=PI / 2, start_angle=0,
    fill_opacity=0.7, color=TEAL
)

# 切割（在形状上挖洞）
background = Square(side_length=4, fill_opacity=1, color=BLUE)
hole = Circle(radius=0.5)
cutout = Cutout(background, hole, fill_opacity=1, color=BLUE)
```

适用场景：饼图、环形进度条、带弧的韦恩图、几何证明。

## 定位

```python
mob.move_to(ORIGIN)                        # 居中
mob.move_to(UP * 2 + RIGHT)               # 相对位置
label.next_to(circle, DOWN, buff=0.3)     # 紧贴另一个对象
title.to_edge(UP, buff=0.5)               # 屏幕边缘（buff 必须 >= 0.5！）
mob.to_corner(UL, buff=0.5)               # 角落
```

## VGroup 与 Group

**VGroup** 用于形状集合（仅限 VMobject —— Circle、Square、Arrow、Line、MathTex）：
```python
shapes = VGroup(circle, square, arrow)
shapes.arrange(DOWN, buff=0.5)
shapes.set_color(BLUE)
```

**Group** 用于混合集合（Text + 形状，或任意 Mobject 类型）：
```python
# Text 对象是 Mobject，不是 VMobject —— 混合时用 Group
labeled_shape = Group(circle, Text("Label").next_to(circle, DOWN))
labeled_shape.move_to(ORIGIN)

# FadeOut 屏幕上所有东西（可能含混合类型）
self.play(FadeOut(Group(*self.mobjects)))
```

**规则：如果你的组里包含任何 `Text()` 对象，用 `Group`，不要用 `VGroup`。** 在 Manim CE v0.20+ 上 VGroup 会抛出 TypeError。MathTex 和 Tex 是 VMobject，可以与 VGroup 配合使用。

两者都支持 `arrange()`、`arrange_in_grid()`、`set_opacity()`、`shift()`、`scale()`、`move_to()`。

## 样式

```python
mob.set_color(BLUE)
mob.set_fill(RED, opacity=0.5)
mob.set_stroke(WHITE, width=2)
mob.set_opacity(0.4)
mob.set_z_index(1)                         # 分层
```

## 专用 mobject

```python
nl = NumberLine(x_range=[-3, 3, 1], length=8, include_numbers=True)
table = Table([["A", "B"], ["C", "D"]], row_labels=[Text("R1"), Text("R2")])
code = Code("example.py", tab_width=4, font_size=20, language="python")
highlight = SurroundingRectangle(target, color=YELLOW, buff=0.2)
bg = BackgroundRectangle(equation, fill_opacity=0.7, buff=0.2)
```

## 自定义 mobject

```python
class NetworkNode(Group):
    def __init__(self, label_text, color=BLUE, **kwargs):
        super().__init__(**kwargs)
        self.circle = Circle(radius=0.4, color=color, fill_opacity=0.3)
        self.label = Text(label_text, font_size=20).move_to(self.circle)
        self.add(self.circle, self.label)
```

## 矩阵 mobject

把矩阵显示为数字网格或 mobject 网格：

```python
# 整数矩阵
m = IntegerMatrix([[1, 2], [3, 4]])

# 小数矩阵（控制小数位数）
m = DecimalMatrix([[1.5, 2.7], [3.1, 4.9]], element_to_mobject_config={"num_decimal_places": 2})

# mobject 矩阵（每个单元放任意 mobject）
m = MobjectMatrix([
    [MathTex(r"\pi"), MathTex(r"e")],
    [MathTex(r"\phi"), MathTex(r"\tau")]
])

# 括号类型："(" "[" "|" 或 "\\{"
m = IntegerMatrix([[1, 0], [0, 1]], left_bracket="[", right_bracket="]")
```

适用场景：线性代数、变换矩阵、方程组系数展示。

## 常量

方向：`UP, DOWN, LEFT, RIGHT, ORIGIN, UL, UR, DL, DR`
颜色：`RED, BLUE, GREEN, YELLOW, WHITE, GRAY, ORANGE, PINK, PURPLE, TEAL, GOLD`
画框：`config.frame_width = 14.222, config.frame_height = 8.0`

## SVGMobject —— 导入 SVG 文件

```python
logo = SVGMobject("path/to/logo.svg")
logo.set_color(WHITE).scale(0.5).to_corner(UR)
self.play(FadeIn(logo))

# SVG 的子 mobject 可以单独动画化
for part in logo.submobjects:
    self.play(part.animate.set_color(random_color()))
```

## ImageMobject —— 显示图片

```python
img = ImageMobject("screenshot.png")
img.set_height(3).to_edge(RIGHT)
self.play(FadeIn(img))
```

注意：图片不能用 `.animate` 动画化（它们是栅格图，不是矢量图）。只能用 `FadeIn`/`FadeOut` 以及 `shift`/`scale`。

## Variable —— 自动更新的显示

```python
var = Variable(0, Text("x"), num_decimal_places=2)
var.move_to(ORIGIN)
self.add(var)

# 动画化它的值
self.play(var.tracker.animate.set_value(5), run_time=2)
# 显示自动更新为："x = 5.00"
```

对于简单的带标签数值显示，这比手动 `DecimalNumber` + `add_updater` 更干净。

## BulletedList

```python
bullets = BulletedList(
    "First key point",
    "Second important fact",
    "Third conclusion",
    font_size=28
)
bullets.to_edge(LEFT, buff=1.0)
self.play(Write(bullets))

# 高亮单条
self.play(bullets[1].animate.set_color(YELLOW))
```

## DashedLine 与角标

```python
# 虚线（渐近线、辅助线）
dashed = DashedLine(LEFT * 3, RIGHT * 3, color=SUBTLE, dash_length=0.15)

# 两条线之间的角标
line1 = Line(ORIGIN, RIGHT * 2)
line2 = Line(ORIGIN, UP * 2 + RIGHT)
angle = Angle(line1, line2, radius=0.5, color=YELLOW)
angle_label = angle.get_value()  # 返回角的弧度值

# 直角标记
right_angle = RightAngle(line1, Line(ORIGIN, UP * 2), length=0.3, color=WHITE)
```

## 布尔运算（CSG）

对 2D 形状进行合并、相减或相交：

```python
circle = Circle(radius=1.5, color=BLUE, fill_opacity=0.5).shift(LEFT * 0.5)
square = Square(side_length=2, color=RED, fill_opacity=0.5).shift(RIGHT * 0.5)

# 并、交、差、对称差
union = Union(circle, square, color=GREEN, fill_opacity=0.5)
intersect = Intersection(circle, square, color=YELLOW, fill_opacity=0.5)
diff = Difference(circle, square, color=PURPLE, fill_opacity=0.5)
exclude = Exclusion(circle, square, color=ORANGE, fill_opacity=0.5)
```

适用场景：韦恩图、集合论、几何证明、面积计算。

## LabeledArrow / LabeledLine

```python
# 带内置标签的箭头（自动定位）
arr = LabeledArrow(Text("force", font_size=18), start=LEFT, end=RIGHT, color=RED)

# 带标签的线
line = LabeledLine(Text("d = 5m", font_size=18), start=LEFT * 2, end=RIGHT * 2)
```

自动处理标签定位 —— 比手动 `Arrow` + `Text().next_to()` 更干净。

## 按子串设置文字颜色/字体/样式（t2c、t2f、t2s、t2w）

```python
# 给特定词着色（t2c = text-to-color）
text = Text(
    "Gradient descent minimizes the loss function",
    t2c={"Gradient descent": BLUE, "loss function": RED}
)

# 每个词用不同字体（t2f = text-to-font）
text = Text(
    "Use Menlo for code and Inter for prose",
    t2f={"Menlo": "Menlo", "Inter": "Inter"}
)

# 每个词用不同斜体（t2s = text-to-slant）
text = Text("Normal and italic text", t2s={"italic": ITALIC})

# 每个词用不同字重（t2w = text-to-weight）
text = Text("Normal and bold text", t2w={"bold": BOLD})
```

这比创建多个 Text 对象再分组要干净得多。

## 用于在背景上保持可读性的描边（Backstroke）

当文字与其他内容（图、示意图、图片）重叠时，在它后面加深色描边：

```python
# CE 语法：
label.set_stroke(BLACK, width=5, background=True)

# 应用到一个组
for mob in labels:
    mob.set_stroke(BLACK, width=4, background=True)
```

这就是 3Blue1Brown 在复杂背景上保持文字可读、而又不使用 BackgroundRectangle 的做法。

## 复函数变换

把复函数应用到整个 mobject 上 —— 会变换整个平面：

```python
c_grid = ComplexPlane()
moving_grid = c_grid.copy()
moving_grid.prepare_for_nonlinear_transform()  # 增加更多采样点以获得平滑变形

self.play(
    moving_grid.animate.apply_complex_function(lambda z: z**2),
    run_time=5,
)

# 也适用于 R3->R3 函数：
self.play(grid.animate.apply_function(
    lambda p: [p[0] + 0.5 * math.sin(p[1]), p[1] + 0.5 * math.sin(p[0]), p[2]]
), run_time=5)
```

**关键：** 在应用非线性函数之前要调用 `prepare_for_nonlinear_transform()` —— 不加它，网格的采样点太少，变形看起来会很锯齿。
