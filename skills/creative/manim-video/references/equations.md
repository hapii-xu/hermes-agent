# 公式与 LaTeX 参考

## 基础 LaTeX

```python
eq = MathTex(r"E = mc^2")
eq = MathTex(r"f(x) &= x^2 + 2x + 1 \\ &= (x + 1)^2")  # 多行对齐
```

**始终使用原始字符串（`r""`）。**

## 逐步推导

```python
step1 = MathTex(r"a^2 + b^2 = c^2")
step2 = MathTex(r"a^2 = c^2 - b^2")
self.play(Write(step1), run_time=1.5)
self.wait(1.5)
self.play(TransformMatchingTex(step1, step2), run_time=1.5)
```

## 选择性着色

```python
eq = MathTex(r"a^2", r"+", r"b^2", r"=", r"c^2")
eq[0].set_color(RED)
eq[4].set_color(GREEN)
```

## 逐步搭建

```python
parts = MathTex(r"f(x)", r"=", r"\sum_{n=0}^{\infty}", r"\frac{f^{(n)}(a)}{n!}", r"(x-a)^n")
self.play(Write(parts[0:2]))
self.wait(0.5)
self.play(Write(parts[2]))
self.wait(0.5)
self.play(Write(parts[3:]))
```

## 高亮

```python
highlight = SurroundingRectangle(eq[2], color=YELLOW, buff=0.1)
self.play(Create(highlight))
self.play(Indicate(eq[4], color=YELLOW))
```

## 标注

```python
brace = Brace(eq, DOWN, color=YELLOW)
label = brace.get_text("Fundamental Theorem", font_size=24)
self.play(GrowFromCenter(brace), Write(label))
```

## 常用 LaTeX

```python
MathTex(r"\frac{a}{b}")                  # 分数
MathTex(r"\alpha, \beta, \gamma")         # 希腊字母
MathTex(r"\sum_{i=1}^{n} x_i")           # 求和
MathTex(r"\int_{0}^{\infty} e^{-x} dx")  # 积分
MathTex(r"\vec{v}")                       # 向量
MathTex(r"\lim_{x \to \infty} f(x)")    # 极限
```

## 矩阵

`MathTex` 通过 `amsmath`（默认已加载）支持标准的 LaTeX 矩阵环境：

```python
# 带方括号的矩阵
MathTex(r"\begin{bmatrix} 1 & 0 \\ 0 & 1 \end{bmatrix}")

# 带圆括号的矩阵
MathTex(r"\begin{pmatrix} a & b \\ c & d \end{pmatrix}")

# 行列式（竖线）
MathTex(r"\begin{vmatrix} a & b \\ c & d \end{vmatrix}")

# 无定界符（裸矩阵）
MathTex(r"\begin{matrix} x_1 \\ x_2 \\ x_3 \end{matrix}")
```

如果矩阵需要逐元素动画化或给单个元素着色，请改用 `IntegerMatrix`、`DecimalMatrix` 或 `MobjectMatrix` 这些 mobject —— 参见 `mobjects.md`。

## 分段函数（cases）

```python
MathTex(r"""
    f(x) = \begin{cases}
        x^2    & \text{if } x \geq 0 \\
        -x^2   & \text{if } x < 0
    \end{cases}
""")
```

## 对齐环境

对于带对齐的多行推导，在 `MathTex` 中使用 `aligned`：

```python
MathTex(r"""
    \begin{aligned}
        \nabla \cdot \mathbf{E} &= \frac{\rho}{\epsilon_0} \\
        \nabla \cdot \mathbf{B} &= 0 \\
        \nabla \times \mathbf{E} &= -\frac{\partial \mathbf{B}}{\partial t} \\
        \nabla \times \mathbf{B} &= \mu_0 \mathbf{J} + \mu_0 \epsilon_0 \frac{\partial \mathbf{E}}{\partial t}
    \end{aligned}
""")
```

注意：`MathTex` 默认会把内容包在 `align*` 里。如有需要，用 `tex_environment` 覆盖：
```python
MathTex(r"...", tex_environment="gather*")
```

## 推导模式

```python
class DerivationScene(Scene):
    def construct(self):
        self.camera.background_color = BG
        s1 = MathTex(r"ax^2 + bx + c = 0")
        self.play(Write(s1))
        self.wait(1.5)
        s2 = MathTex(r"x^2 + \frac{b}{a}x + \frac{c}{a} = 0")
        s2.next_to(s1, DOWN, buff=0.8)
        self.play(s1.animate.set_opacity(0.4), TransformMatchingTex(s1.copy(), s2))
```

## substrings_to_isolate 用于复杂公式

对于难以手动拆分成几部分的密集公式，用 `substrings_to_isolate` 告诉 Manim 把哪些子串当作独立元素来追踪：

```python
# 不隔离 —— 整个表达式是一团
lagrangian = MathTex(
    r"\mathcal{L} = \bar{\psi}(i \gamma^\mu D_\mu - m)\psi - \tfrac{1}{4}F_{\mu\nu}F^{\mu\nu}"
)

# 加隔离 —— 每个具名子串都是一个独立的子 mobject
lagrangian = MathTex(
    r"\mathcal{L} = \bar{\psi}(i \gamma^\mu D_\mu - m)\psi - \tfrac{1}{4}F_{\mu\nu}F^{\mu\nu}",
    substrings_to_isolate=[r"\psi", r"D_\mu", r"\gamma^\mu", r"F_{\mu\nu}"]
)
# 现在你可以给单独的项着色了
lagrangian.set_color_by_tex(r"\psi", BLUE)
lagrangian.set_color_by_tex(r"F_{\mu\nu}", YELLOW)
```

对于在复杂公式上做 `TransformMatchingTex` 不可或缺 —— 不加隔离，匹配在密集表达式上会失败。

## 多行复杂公式

对于含多条相关行的公式，把每行作为单独的参数传入：

```python
maxwell = MathTex(
    r"\nabla \cdot \mathbf{E} = \frac{\rho}{\epsilon_0}",
    r"\nabla \times \mathbf{B} = \mu_0\mathbf{J} + \mu_0\epsilon_0\frac{\partial \mathbf{E}}{\partial t}"
).arrange(DOWN)

# 每一行是独立的子 mobject —— 可单独动画化
self.play(Write(maxwell[0]))
self.wait(1)
self.play(Write(maxwell[1]))
```

## TransformMatchingTex 配合 key_map

在变换过程中，把源公式和目标公式之间的特定子串映射起来：

```python
eq1 = MathTex(r"A^2 + B^2 = C^2")
eq2 = MathTex(r"A^2 = C^2 - B^2")

self.play(TransformMatchingTex(
    eq1, eq2,
    key_map={"+": "-"},   # 把源中的 "+" 映射为目标中的 "-"
    path_arc=PI / 2,      # 让碎片沿弧线飞到位置
))
```

## set_color_by_tex —— 按子串着色

```python
eq = MathTex(r"E = mc^2")
eq.set_color_by_tex("E", BLUE)
eq.set_color_by_tex("m", RED)
eq.set_color_by_tex("c", GREEN)
```

## TransformMatchingTex 配合 matched_keys

当匹配的子串存在歧义时，显式指定要对齐哪些：

```python
kw = dict(font_size=72, t2c={"A": BLUE, "B": TEAL, "C": GREEN})
lines = [
    MathTex(r"A^2 + B^2 = C^2", **kw),
    MathTex(r"A^2 = C^2 - B^2", **kw),
    MathTex(r"A^2 = (C + B)(C - B)", **kw),
    MathTex(r"A = \sqrt{(C + B)(C - B)}", **kw),
]

self.play(TransformMatchingTex(
    lines[0].copy(), lines[1],
    matched_keys=["A^2", "B^2", "C^2"],  # 显式匹配这些
    key_map={"+": "-"},                    # 把 + 映射为 -
    path_arc=PI / 2,                       # 让碎片沿弧线飞到位置
))
```

如果不加 `matched_keys`，动画会匹配最长公共子串，在复杂公式上可能产生意外结果（例如 "^2 = C^2" 跨项匹配）。
