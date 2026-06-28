# 3D 场景参考

灯光阵列、阴影、IBL/立方体贴图、多相机和 PBR 材质。关于线框渲染和 Feedback TOP，请参见 `operator-tips.md`。关于几何体实例化，请参见 `geometry-comp.md`。关于着色器代码，请参见 `glsl.md`。

---

## 3D 场景的结构

```
[Geometry COMP]    ← 包含 SOPs（各种形状）
[Material]         ← Phong/PBR/GLSL/Constant MAT
[Light COMPs]      ← point/directional/spot/area/environment
[Camera COMP]      ← 视图位置、FOV
        │
        ▼
   [Render TOP]    ← 将几何体 + 灯光 + 相机合成一张 2D 图像
        │
        ▼
   [post-FX chain] ← bloomTOP、glsl 着色器等
        │
        ▼
   [windowCOMP]    ← 实际显示
```

Render TOP 是核心。它接收一个显式的 `geometry` 路径、一个显式的 `camera` 路径，以及通过灯光表或一个 envlight 引用传入的灯光。

---

## 最小场景

```python
# 几何体
geo = root.create(geometryCOMP, 'scene_geo')
sphere = geo.create(sphereSOP, 'shape')
sphere.par.rad = 1.0; sphere.par.rows = 64; sphere.par.cols = 64

# 材质 —— 先用 PBR
mat = root.create(pbrMAT, 'mat')
mat.par.basecolorr = 0.7; mat.par.basecolorg = 0.7; mat.par.basecolorb = 0.7
mat.par.metallic = 0.0
mat.par.roughness = 0.4

geo.par.material = mat.path

# 相机
cam = root.create(cameraCOMP, 'cam1')
cam.par.tx = 0; cam.par.ty = 0; cam.par.tz = 4
cam.par.fov = 45
cam.par.near = 0.1; cam.par.far = 100

# 主光（key light）
key = root.create(lightCOMP, 'key_light')
key.par.lighttype = 'point'
key.par.tx = 3; key.par.ty = 3; key.par.tz = 3
key.par.dimmer = 1.5

# 渲染
render = root.create(renderTOP, 'render1')
render.par.outputresolution = 'custom'
render.par.resolutionw = 1920; render.par.resolutionh = 1080
render.par.camera = cam.path
render.par.geometry = geo.path
render.par.lights = key.path                 # 单个灯光路径；多灯光见下文
render.par.bgcolorr = 0; render.par.bgcolorg = 0; render.par.bgcolorb = 0
```

若要使用多个灯光，请将 `par.lights` 留空 —— Render TOP 会默认扫描整个网络中的所有 `lightCOMP` 和 `envlightCOMP` 算子。若要限定到特定灯光，可设置 `par.lights = '/project1/key_light /project1/fill_light'`（用空格分隔的路径）。

---

## 灯光类型

| 类型 | 说明 | 常用参数 |
|---|---|---|
| `point` | 全向光，随距离衰减 | `dimmer`、`coneangle`（不适用）、`attenuation` |
| `directional` | 平行光，无限远（太阳） | `dimmer`，仅灯光旋转角度起作用 |
| `spot` | 锥形光，随距离和角度衰减 | `coneangle`、`conedelta`、`dimmer` |
| `cone` | 类似 spot，但边缘更硬 | 同上 |
| `area` | 矩形软光源 | `sizex`、`sizey` |

对所有类型通用：`colorr`、`colorg`、`colorb`、`tx/ty/tz`、`rx/ry/rz`、`dimmer`。

### 三点布光（影棚设置）

```python
# 主光（key）—— 主光源，约 45° 前侧
key = root.create(lightCOMP, 'key')
key.par.lighttype = 'point'
key.par.tx = 4; key.par.ty = 3; key.par.tz = 4
key.par.dimmer = 1.5
key.par.colorr = 1.0; key.par.colorg = 0.95; key.par.colorb = 0.85

# 补光（fill）—— 更柔，位于对侧
fill = root.create(lightCOMP, 'fill')
fill.par.lighttype = 'area'
fill.par.tx = -4; fill.par.ty = 2; fill.par.tz = 3
fill.par.dimmer = 0.5
fill.par.colorr = 0.7; fill.par.colorg = 0.8; fill.par.colorb = 1.0
fill.par.sizex = 4; fill.par.sizey = 4

# 轮廓光（rim/back）—— 从背后勾勒
rim = root.create(lightCOMP, 'rim')
rim.par.lighttype = 'spot'
rim.par.tx = 0; rim.par.ty = 4; rim.par.tz = -4
rim.par.coneangle = 30
rim.par.dimmer = 1.0

# 可选：环境补光，防止阴影完全纯黑
amb = root.create(ambientlightCOMP, 'ambient')
amb.par.dimmer = 0.15
```

---

## 阴影

当 `par.shadowtype != 'none'` 时，spot 和 directional 灯光会投射阴影。

```python
key.par.shadowtype = 'softshadow'        # 'none' | 'hardshadow' | 'softshadow'
key.par.shadowsize = 1024                # 阴影贴图分辨率
key.par.shadowsoftness = 0.02            # 仅 softshadow 生效
```

**技巧：**
- 软阴影对 GPU 开销很大。从 `shadowsize = 1024` 起步，仅当阴影边缘在你的分辨率下出现锯齿时才提高（2048/4096）。
- 将聚光灯的 `near`/`far` 设置到刚好包含场景即可。范围越宽 = 阴影贴图精度越浪费。
- 多个投射阴影的灯光开销会叠加。实时工作中限制在 1-2 个；其余预先烘焙到材质里。

---

## 基于图像的光照（IBL）/ 环境光

要实现逼真的 PBR 材质，需要一个立方体贴图来提供反射。

```python
# 来自 HDR 的环境光
env = root.create(envlightCOMP, 'env')
env.par.envmap = '/project1/cube_in'         # 指向一个生成立方体贴图的 TOP 路径
env.par.envlightmap = ...                    # 漫反射辐照度贴图（通常与 envmap 相同）
env.par.dimmer = 1.0

# 立方体贴图来源 —— 方式 A：用内置的 cubeTOP 从 6 个面生成
cube = root.create(cubeTOP, 'cube_in')
# （指定 6 个面的 TOP）

# 方式 B：HDR 等距矩形 → 立方体贴图转换
# 用 moviefileinTOP 加载 .hdr 或 .exr，再用 projectTOP type='cubemapfromequirect'
hdr = root.create(moviefileinTOP, 'hdr_src')
hdr.par.file = '/path/to/environment.hdr'

proj = root.create(projectTOP, 'cube_proj')
proj.par.projecttype = 'cubemapfromequirect'
proj.inputConnectors[0].connect(hdr)
```

当场景中存在 `envlightCOMP` 时，PBR 材质会自动采样环境。请用 `td_get_par_info(op_type='envlightCOMP')` 核实参数名 —— 不同 TD 版本会有差异。

---

## PBR 材质设置

```python
mat = root.create(pbrMAT, 'pbr_metal')
mat.par.basecolorr = 0.95; mat.par.basecolorg = 0.65; mat.par.basecolorb = 0.4
mat.par.metallic = 1.0
mat.par.roughness = 0.25
mat.par.specularlevel = 0.5
mat.par.emitcolorr = 0; mat.par.emitcolorg = 0; mat.par.emitcolorb = 0

# 纹理贴图
mat.par.basecolormap = '/project1/textures/albedo'         # TOP 路径
mat.par.metallicroughnessmap = '/project1/textures/mr'      # G=roughness，B=metallic（glTF 约定）
mat.par.normalmap = '/project1/textures/normal'
mat.par.emitmap = '/project1/textures/emit'
mat.par.occlusionmap = '/project1/textures/ao'
```

**材质常用设定：**

| 外观 | metallic | roughness | basecolor |
|---|---|---|---|
| 拉丝钢 | 1.0 | 0.4 | (0.7, 0.7, 0.7) |
| 抛光金 | 1.0 | 0.1 | (1.0, 0.85, 0.4) |
| 塑料 | 0.0 | 0.5 | 中等饱和度 |
| 橡胶 | 0.0 | 0.9 | 偏暗 |
| 玻璃 | 0.0 | 0.05 | (1, 1, 1)，低 alpha + transmission |
| 发光体 | 0.0 | 1.0 | 偏暗，高 `emitcolor` |

对于玻璃/透射，较新的 TD 版本在 PBR 中支持 `transmission`；旧版本需要 glslMAT。

---

## 多相机设置

用于对比视图、即时回放、多屏映射等。

```python
# 相机 A —— 主场景
cam_a = root.create(cameraCOMP, 'cam_main')
cam_a.par.tz = 5

# 相机 B —— 环绕俯视
cam_b = root.create(cameraCOMP, 'cam_top')
cam_b.par.ty = 6; cam_b.par.rx = -90

# 通过各自的 Render TOP 分别渲染
render_a = root.create(renderTOP, 'render_main')
render_a.par.camera = cam_a.path
render_a.par.geometry = geo.path

render_b = root.create(renderTOP, 'render_top')
render_b.par.camera = cam_b.path
render_b.par.geometry = geo.path
```

用 `multiplyTOP`/`compositeTOP` 合成两者实现画中画，或分别路由到独立的 `windowCOMP` 用于多显示输出。

### 相机动画

通过表达式（环绕）、animationCOMP（路径点）或 LFO（振荡）驱动相机参数：

```python
# 环绕相机
cam_a.par.tx.mode = ParMode.EXPRESSION
cam_a.par.tx.expr = "cos(absTime.seconds * 0.3) * 6"
cam_a.par.tz.mode = ParMode.EXPRESSION
cam_a.par.tz.expr = "sin(absTime.seconds * 0.3) * 6"
cam_a.par.lookat = '/project1/scene_geo'        # 自动瞄准目标
```

`par.lookat` 是最简单的“始终看向目标”机制。

### 景深（DOF）

当 `par.dof = 'on'` 时，PBR + Render TOP 支持景深。

```python
render.par.dof = 'on'
render.par.focusdistance = 5.0
render.par.aperture = 0.05         # 模糊强度
render.par.bokehshape = 'hexagon'
```

DOF 对 GPU 开销很大。为提升性能可降低分辨率渲染后再放大。

---

## 常见陷阱

1. **Render TOP 显示为黑色** —— 最常见原因：没有灯光。即使使用 PBR，也至少需要一个 `lightCOMP` 或 `envlightCOMP`。可以加一个低 dimmer 的 `ambientlightCOMP` 作为兜底。
2. **材质不显示** —— `geo.par.material` 必须是字符串路径，而不是材质算子本身。用 `mat.path`，而不是 `mat`。
3. **灯光被忽略** —— 默认 Render TOP 会拾取网络中所有 `lightCOMP`。如果留下了其他场景的灯光，它们会泄漏进来。请显式设置 `par.lights`。
4. **PBR 看起来很平** —— 没有 `envlightCOMP` 提供反射时，PBR 材质看起来像 Phong。即使没有 HDR 也要加一个（可使用 `constantTOP` 立方体贴图作为兜底）。
5. **阴影痤疮/条纹** —— 稍微调高 `par.shadowbias`，按每个灯光单独调。
6. **相机在几何体内部** —— 如果 `cam.par.tz` 位于球体内部，会看到内表面（或背面剔除时什么都看不到）。把相机移到更外面。
7. **光照范围太小** —— 点光源有隐式衰减。距离较远的几何体几乎接收不到光照。增大 `par.dimmer` 或把灯光移近。
8. **多相机冲突** —— 一个 render TOP 只能有一个相机。不要尝试共享，应使用多个 render TOP。
9. **坐标系手性错误** —— TD 是右手坐标系、Y 轴朝上。从 Z 轴朝上的应用（Blender、Maya 的 Z-up 模式）导入的资源，需要在 geo COMP 上加 90° X 轴旋转。
10. **运算开销** —— 在现代 GPU 上，PBR + IBL + 阴影 + DOF 在 1080p60 下没问题，但 4K + 4 个灯光 + 软阴影 + DOF 会让性能崩盘。用 `td_get_perf` 做性能分析，并在加更多内容前先降级设置。

---

## 快速配方

| 目标 | 配方 |
|---|---|
| 影棚人像 | 三点布光（key + fill + rim）+ 环境光 + PBR 材质 + DOF |
| 户外日光 | 一个 directional `lightCOMP`（太阳）+ envlight（天空 HDR）+ 软阴影 |
| 戏剧性/黑色电影风 | 单个侧上方聚光灯、硬阴影、深环境光 = 0.05 |
| 抽象/梦幻 | 多个低 dimmer 面光、无阴影、`bloomTOP` 后期 |
| 产品渲染 | 三点布光 + IBL + 中性 PBR + `bgcolorr=g=b=1`（纯白无缝） |
| 游戏风格 | Phong MAT + 1-2 个灯光 + 无 IBL + 平环境光（廉价、风格化） |
| 线框 + 实体 | 两个 render TOP（一个用 wireframeMAT，一个用 PBR），通过 `addTOP` 合成 |
| 环绕相机 | `par.lookat` + 用 sin/cos 在 tx/tz 上加表达式 |
