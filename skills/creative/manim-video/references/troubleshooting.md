# 故障排查

## LaTeX 错误

**漏写原始字符串**（头号错误）：
```python
# 错误：MathTex("\\frac{1}{2}")  -- \\f 是换页符
# 正确：MathTex(r"\frac{1}{2}")
```

**花括号不配对**：`MathTex(r"\frac{1}{2")` —— 缺少右花括号。

**未安装 LaTeX**：`which pdflatex` —— 安装 texlive-full 或 mactex。

**缺少宏包**：加入导言区：
```python
tex_template = TexTemplate()
tex_template.add_to_preamble(r"\usepackage{mathrsfs}")
MathTex(r"\mathscr{L}", tex_template=tex_template)
```

## VGroup TypeError

**错误：** `TypeError: Only values of type VMobject can be added as submobjects of VGroup`

**原因：** `Text()` 对象是 `Mobject`，不是 `VMobject`。在 Manim CE v0.20+ 上，把 `Text` 与形状混在 `VGroup` 里会失败。

```python
# 错误：Text 不是 VMobject
group = VGroup(circle, Text("Label"))

# 正确：混合类型用 Group
group = Group(circle, Text("Label"))

# 正确：全是形状时 VGroup 没问题
shapes = VGroup(circle, square, arrow)

# 正确：MathTex 是 VMobject —— VGroup 可用
equations = VGroup(MathTex(r"a"), MathTex(r"b"))
```

**规则：** 如果组里包含任何 `Text()`，用 `Group`。如果全是形状或全是 `MathTex`，`VGroup` 没问题。

**FadeOut 所有东西：** 始终用 `Group(*self.mobjects)`，不要用 `VGroup(*self.mobjects)`：
```python
self.play(FadeOut(Group(*self.mobjects)))  # 对混合类型安全
```

## Group 的 save_state() / restore() 不支持

**错误：** `NotImplementedError: Please override in a child class.`

**原因：** `Group.save_state()` 和 `Group.restore()` 在 Manim CE v0.20+ 中没有实现。只有 `VGroup` 和单独的 `Mobject` 子类支持保存/恢复。

```python
# 错误：Group 不支持 save_state
group = Group(circle, Text("label"))
group.save_state()  # NotImplementedError！

# 正确：改用带 shift/scale 的 FadeIn，而不用 save_state/restore
self.play(FadeIn(group, shift=UP * 0.3, scale=0.8))

# 正确：或者在单独的 VMobject 上保存/恢复
circle.save_state()
self.play(circle.animate.shift(RIGHT))
self.play(Restore(circle))
```

## letter_spacing 不是有效参数

**错误：** `TypeError: Mobject.__init__() got an unexpected keyword argument 'letter_spacing'`

**原因：** `Text()` 不接受 `letter_spacing`。Manim 用 Pango 渲染文字，不在 `Text()` 上暴露字距控制。

```python
# 错误
Text("HERMES", letter_spacing=6)

# 正确：用 MarkupText 配 Pango 属性来控制间距
MarkupText('<span letter_spacing="6000">HERMES</span>', font_size=18)
# 注意：Pango 的 letter_spacing 单位是 1/1024 个点
```

## 动画错误

**看不见的动画** —— mobject 从未添加：
```python
# 错误：circle = Circle(); self.play(circle.animate.set_color(RED))
# 正确：self.play(Create(circle)); self.play(circle.animate.set_color(RED))
```

**Transform 困惑** —— Transform(A, B) 之后，A 在屏幕上，B 不在。如果你想要 B，用 ReplacementTransform。

**重复动画** —— 同一个 mobject 在一次 play() 里出现两次：
```python
# 错误：self.play(c.animate.shift(RIGHT), c.animate.set_color(RED))
# 正确：self.play(c.animate.shift(RIGHT).set_color(RED))
```

**更新器与动画打架**：
```python
mob.suspend_updating()
self.play(mob.animate.shift(RIGHT))
mob.resume_updating()
```

## 渲染问题

**输出模糊**：用了 -ql（480p）。出成品时改用 -qm/-qh。

**渲染慢**：开发时用 -ql。降低 Surface 分辨率。缩短 self.wait()。

**输出陈旧**：`manim -ql --disable_caching script.py Scene`

**ffmpeg 拼接失败**：所有片段的分辨率/FPS/编码必须一致。

## 常见误区

**文字在边缘被裁切**：`.to_edge()` 的 `buff >= 0.5`

**文字重叠**：用 `ReplacementTransform(old, new)`，不要在上面 `Write(new)`。

**太拥挤**：最多 5-6 个可见元素。拆成多个场景或用透明度分层。

**没有呼吸空间**：揭示之后最少 `self.wait(1.5)`，关键时刻 `self.wait(2.0)`。

**漏设背景色**：每个场景都设 `self.camera.background_color = BG`。

## 调试策略

1. 渲染一张静帧：`manim -ql -s script.py Scene` —— 瞬间检查布局
2. 隔离出问题的场景 —— 只渲染那一个
3. 用 `self.add()` 替换 `self.play()` 以瞬间看到最终状态
4. 打印位置：`print(mob.get_center())`
5. 清缓存：删除 `media/` 目录
