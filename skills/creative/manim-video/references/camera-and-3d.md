# 相机与 3D 参考

## MovingCameraScene（2D 相机控制）

```python
class ZoomExample(MovingCameraScene):
    def construct(self):
        circle = Circle(radius=2, color=BLUE)
        self.play(Create(circle))
        # 放大
        self.play(self.camera.frame.animate.set(width=4).move_to(circle.get_top()), run_time=2)
        self.wait(2)
        # 缩小回去
        self.play(self.camera.frame.animate.set(width=14.222).move_to(ORIGIN), run_time=2)
```

### 相机操作

```python
self.camera.frame.animate.set(width=6)     # 放大
self.camera.frame.animate.set(width=20)    # 缩小
self.camera.frame.animate.move_to(target)  # 平移
self.camera.frame.save_state()             # 保存
self.play(Restore(self.camera.frame))      # 恢复
```

## ThreeDScene

```python
class ThreeDExample(ThreeDScene):
    def construct(self):
        self.set_camera_orientation(phi=60*DEGREES, theta=-45*DEGREES)
        axes = ThreeDAxes()
        surface = Surface(
            lambda u, v: axes.c2p(u, v, np.sin(u) * np.cos(v)),
            u_range=[-PI, PI], v_range=[-PI, PI], resolution=(30, 30)
        )
        surface.set_color_by_gradient(BLUE, GREEN, YELLOW)
        self.play(Create(axes), Create(surface))
        self.begin_ambient_camera_rotation(rate=0.2)
        self.wait(5)
        self.stop_ambient_camera_rotation()
```

### 3D 中的相机控制

```python
self.set_camera_orientation(phi=70*DEGREES, theta=-45*DEGREES)
self.move_camera(phi=45*DEGREES, theta=30*DEGREES, run_time=2)
self.begin_ambient_camera_rotation(rate=0.2)
```

### 3D mobject

```python
sphere = Sphere(radius=1).set_color(BLUE).set_opacity(0.7)
cube = Cube(side_length=2, fill_color=GREEN, fill_opacity=0.5)
arrow = Arrow3D(start=ORIGIN, end=[2, 1, 1], color=RED)
# 朝向相机的 2D 文字：
label = Text("Label", font_size=30)
self.add_fixed_in_frame_mobjects(label)
```

### 参数曲线

```python
helix = ParametricFunction(
    lambda t: [np.cos(t), np.sin(t), t / (2*PI)],
    t_range=[0, 4*PI], color=YELLOW
)
```

## 何时使用 3D
- 曲面、向量场、空间几何、3D 变换
## 何时不使用 3D
- 2D 概念、文字密集的场景、扁平数据（柱状图、时间序列）

## ZoomedScene —— 画中画放大

在保持完整视图可见的同时，放大展示某个细节：

```python
class ZoomExample(ZoomedScene):
    def __init__(self, **kwargs):
        super().__init__(
            zoom_factor=0.3,           # 放大框覆盖场景的比例
            zoomed_display_height=3,   # 画中画的尺寸
            zoomed_display_width=3,
            zoomed_camera_frame_starting_position=ORIGIN,
            **kwargs
        )

    def construct(self):
        self.camera.background_color = BG
        # ... 创建你的场景内容 ...

        # 激活放大
        self.activate_zooming()

        # 把放大框移到感兴趣的点
        self.play(self.zoomed_camera.frame.animate.move_to(detail_point))
        self.wait(2)

        # 关闭
        self.play(self.get_zoomed_display_pop_out_animation(), rate_func=lambda t: smooth(1-t))
```

适用场景：放大公式中的某一项、展示图中的精细细节、放大绘图的某个区域。

## LinearTransformationScene —— 线性代数

预置了基向量和网格的场景，用于可视化矩阵变换：

```python
class LinearTransformExample(LinearTransformationScene):
    def __init__(self, **kwargs):
        super().__init__(
            show_coordinates=True,
            show_basis_vectors=True,
            **kwargs
        )

    def construct(self):
        matrix = [[2, 1], [1, 1]]

        # 在应用变换前添加一个向量
        vector = self.get_vector([1, 2], color=YELLOW)
        self.add_vector(vector)

        # 应用变换 —— 网格、基向量以及你的向量全部一起变换
        self.apply_matrix(matrix)
        self.wait(2)
```

这会产出标志性的 3Blue1Brown《线性代数的本质》效果 —— 网格线变形、基向量伸缩、行列式通过面积变化来可视化。
