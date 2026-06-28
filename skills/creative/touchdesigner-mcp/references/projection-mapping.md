# 投影映射参考

多窗口输出、曲面映射、边缘融合，以及面向装置/活动工作的投影仪校准模式。

关于 HUD 布局和屏幕上面板网格，见 `layout-compositor.md`。关于线框/测试图案生成，见 `operator-tips.md`。

---

## Window COMP —— 输出到显示器

`windowCOMP` 是 TD 把像素推送到真实显示器的方式。

```python
win = root.create(windowCOMP, 'output_window')
win.par.winop = '/project1/final_out'   # 要显示的 TOP 路径
win.par.winw = 1920
win.par.winh = 1080
win.par.winoffsetx = 0                  # 屏幕空间偏移
win.par.winoffsety = 0
win.par.borders = False                 # 无边框
win.par.alwaysontop = True
win.par.cursor = False                  # 全屏时隐藏光标
win.par.justify = 'fillaspect'          # 'fill' | 'fitaspect' | 'fillaspect' | 'native'
win.par.winopen.pulse()                 # 打开窗口
```

要指向某个具体物理显示器，设置 `par.location`：

```python
win.par.location = 'secondary'          # 'primary' | 'secondary' | 'monitor1' | 'monitor2' | ...
```

或使用与 OS 显示布局匹配的 `winoffsetx/y` 绝对坐标。

**始终脉冲化 `winopen` —— 仅设置参数不会打开窗口。**

---

## 多窗口输出

对多投影仪或多显示器设置，每个输出创建一个 `windowCOMP`，各自指向不同 TOP。

```python
for i, screen_top in enumerate(['out_left', 'out_center', 'out_right']):
    w = root.create(windowCOMP, f'win_{i}')
    w.par.winop = f'/project1/{screen_top}'
    w.par.winw = 1920; w.par.winh = 1080
    w.par.winoffsetx = i * 1920
    w.par.winoffsety = 0
    w.par.borders = False
    w.par.alwaysontop = True
    w.par.cursor = False
    w.par.winopen.pulse()
```

对超宽单输出拼接，用单个 5760×1080 的 windowCOMP，通过 GPU 的拼接/跨度模式（Nvidia Mosaic、AMD Eyefinity）横跨三台投影仪，再在 TD 内用 `cropTOP` 按屏切分内容。

---

## 四点角钉（Quad Warp）

最简单的投影映射原语 —— 把一个矩形扭曲到四边形上。

```python
# 源内容
src = op('/project1/scene_out')

# 手工方式：cornerPinTOP（TD 内置）
cp = root.create(cornerPinTOP, 'corner_pin')
cp.par.tlx = 0.05; cp.par.tly = 0.10    # 左上（归一化 0-1）
cp.par.trx = 0.95; cp.par.try = 0.08    # 右上
cp.par.brx = 0.93; cp.par.bry = 0.92    # 右下
cp.par.blx = 0.07; cp.par.bly = 0.94    # 左下
cp.inputConnectors[0].connect(src)
```

替代方案：用一个带 `gridSOP` 的 `geometryCOMP` 并在顶点 GLSL 中弯折顶点。更灵活（曲面）但设置更多。

用 `td_get_par_info(op_type='cornerPinTOP')` 核实 TD 2025.32 的参数名。

---

## 贝塞尔 / 网格扭曲（曲面）

对非平面表面（穹顶、柱体、弧形墙），使用细分网格和逐顶点位移。

### 模式：网格 + GLSL 位移

```python
# 在一个 geo 内的细分网格
geo = root.create(geometryCOMP, 'warp_geo')
grid = geo.create(gridSOP, 'warp_grid')
grid.par.rows = 32          # 越高 = 曲面越平滑
grid.par.cols = 32
grid.par.sizex = 2; grid.par.sizey = 2

# 把源贴图贴上去
mat = root.create(constMAT, 'warp_mat')      # 投影用 constMAT（无光照）
mat.par.maptop = '/project1/scene_out'        # 源 TOP

geo.par.material = mat.path

# 渲染到一个进入投影仪窗口的 TOP
cam = root.create(cameraCOMP, 'cam_proj')
cam.par.tz = 4

render = root.create(renderTOP, 'projection_out')
render.par.camera = cam.path
render.par.geometry = geo.path
render.par.outputresolution = 'custom'
render.par.resolutionw = 1920; render.par.resolutionh = 1080
```

要做逐顶点偏移，在 constMAT 上写顶点 GLSL（或用 `glslMAT`），通过 uniform 从 CHOP 读取位移值。

校准是迭代的：从 `scene_out` 渲染一张棋盘格，投影它，拍下投影照片，手工微调角点/网格点直到对齐。

---

## 边缘融合（多投影仪重叠）

当两台投影仪重叠时，重叠区域亮度是两倍。通过在重叠区把每台投影仪的边缘 alpha 斜降到 0 来融合。

### GLSL 边缘融合着色器

针对每台投影仪的输出通道，把内侧边缘淡到黑色：

```glsl
// edge_blend_pixel.glsl
out vec4 fragColor;
uniform float uBlendLeft;     // 左边缘重叠宽度（0-0.5，0=不融合）
uniform float uBlendRight;
uniform float uGamma;          // 典型值 2.2 —— 感知斜坡

void main() {
    vec2 uv = vUV.st;
    vec4 col = texture(sTD2DInputs[0], uv);

    float aL = (uBlendLeft  > 0.0) ? smoothstep(0.0, uBlendLeft, uv.x) : 1.0;
    float aR = (uBlendRight > 0.0) ? smoothstep(0.0, uBlendRight, 1.0 - uv.x) : 1.0;
    float a = pow(aL * aR, uGamma);

    fragColor = TDOutputSwizzle(vec4(col.rgb * a, 1.0));
}
```

把此着色器应用到每台触碰重叠的投影仪的输出。调 `uBlendLeft` / `uBlendRight` 以匹配你的物理重叠。

对上下融合或柱面设置，扩展着色器加入 `uBlendTop` / `uBlendBottom`。

---

## 校准图案

用于对齐投影仪的有用测试图案。构建一个在这些图案间切换的 `switchTOP`，设置期间路由到所有投影仪窗口。

```python
# 纯白 —— 用于亮度/均匀性检查
white = root.create(constantTOP, 'cal_white')
white.par.colorr = 1.0; white.par.colorg = 1.0; white.par.colorb = 1.0

# 居中十字准星 —— 用于梯形对齐
gridcross = root.create(textTOP, 'cal_cross')
gridcross.par.text = '+'
gridcross.par.fontsizex = 200

# 细网格 —— 用于扭曲/网格对齐（用 rampTOP + math + threshold，或通过 GLSL 构建）
# 彩条用于投影仪颜色校准
bars = root.create(rampTOP, 'cal_bars')
bars.par.type = 'horizontal'
```

或如果你的 TD 版本自带，用捆绑的 `testpatternTOP`。

---

## 投影审计工作流

调试多屏设置时：

1. 为每个输出渲染一种独特颜色和标签（`textTOP` 写“LEFT”、“CENTER”、“RIGHT”）。
2. 检查每个窗口是否取自正确路径：`td_get_operator_info(path='/project1/win_0')`。
3. 核实显示器分配：走到每台投影仪前肉眼确认。
4. 检查分辨率：投影仪原生分辨率 vs. TD 输出分辨率 —— 不匹配会产生缩放伪影。
5. cook 标志：`td_get_perf` —— 若某窗口的源 TOP 未 cook，投影仪会显示冻结的上一帧。

---

## 陷阱

1. **窗口打不开** —— 你忘了 `winopen.pulse()`。仅设置参数不会打开它。
2. **显示器错误** —— `par.location='secondary'` 取决于 OS 显示顺序。把 `winoffsetx/y` 设为绝对坐标是更可靠的覆盖方式。
3. **光标可见** —— 在打开前设置 `par.cursor = False`，或关闭后重开。
4. **投影全黑** —— 通常是 cook 问题。用 `td_get_perf` 核实 `final_out` TOP 正在 cook。从 `/` 递归检查 `td_get_errors`。
5. **撕裂 / vsync** —— `windowCOMP` 遵循 `par.vsync`。投影时始终设 `vsync='vsync'`（默认）。撕裂意味着 GPU 超预算 —— 降低渲染分辨率。
6. **宽高比不匹配** —— 投影仪原生常为 1920×1200（16:10）而非 1080。用 `justify='fitaspect'` 或按投影仪原生分辨率渲染。
7. **非商业授权** —— 总分辨率上限 1280×1280。真正的装置工作需要 Commercial。Pro 授权支持 4K+。
8. **macOS 多显示器** —— `windowCOMP` 遵循 macOS Spaces。演出前禁用 Spaces，或在系统设置中把 TD 固定到某个显示器。

---

## 快速配方

| 目标 | 做法 |
|---|---|
| 单全屏输出 | 一个 `windowCOMP`，`justify='fillaspect'`，`winopen.pulse()` |
| 3 投影仪宽幅拼接 | 3 个 `windowCOMP` + 从一个宽源按输出 `cropTOP` |
| 单个四边曲面 | `cornerPinTOP` → `windowCOMP` |
| 曲面/穹顶 | 细分 gridSOP + 顶点 GLSL → `renderTOP` → `windowCOMP` |
| 边缘融合重叠 | 每台投影仪一个 GLSL 淡化着色器 → `windowCOMP` |
| 校准模式 | 在场景与测试图案间 `switchTOP`，热键触发 |
