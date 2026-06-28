# 算子技巧

## 线框渲染模式

可在黑色背景上渲染线框几何体的可复用配置：

```python
# 1. 材质
mat = root.create(wireframeMAT, 'wire_mat')
mat.par.colorr = 1.0; mat.par.colorg = 0.0; mat.par.colorb = 0.0
mat.par.linewidth = 3

# 2. Geometry COMP
geo = root.create(geometryCOMP, 'my_geo')
geo.par.rx.expr = 'absTime.seconds * 30'
geo.par.ry.expr = 'absTime.seconds * 45'
geo.par.material = mat.path  # 注意：是 'material' 不是 'mat'

# 3. geo 内部的形状
box = geo.create(boxSOP, 'cube')
box.par.sizex = 1.5; box.par.sizey = 1.5; box.par.sizez = 1.5

# 4. 摄像机
cam = root.create(cameraCOMP, 'cam1')
cam.par.tx = 0; cam.par.ty = 0; cam.par.tz = 4; cam.par.fov = 45

# 5. Render TOP
render = root.create(renderTOP, 'render1')
render.par.outputresolution = 'custom'
render.par.resolutionw = 1280; render.par.resolutionh = 720
render.par.bgcolorr = 0; render.par.bgcolorg = 0; render.par.bgcolorb = 0
render.par.camera = cam.path
render.par.geometry = geo.path

# 6. 输出 null
out = root.create(nullTOP, 'out1')
out.inputConnectors[0].connect(render.outputConnectors[0])
```

**关键规则：**
- 类名：`wireframeMAT` 而不是 `wireframeMat`（后缀全大写）
- 几何体 SOP/POP 要放在 geo comp 内部
- 材质：`geo.par.material` 而不是 `geo.par.mat`
- 渲染几何体：`render.par.geometry = geo.path`（字符串路径）
- `wireframeMAT.par.wireframemode = 'topology'` 可得到干净线框（对比 `'tesselated'` 显示三角形边）
- 替代方案：使用 `renderTOP.par.overridemat` 代替逐 geo 设置材质

## Feedback TOP

### 基本结构

```
input（初始状态）──┐
                  ├──→ feedback_top ──→ 处理 ──→ null_out
                  │                                  ↑
                  └── par.top = 'null_out' ─────────┘
```

### 配置模式

```python
# 1. 处理链
glsl = root.create(glslTOP, 'sim')
null_out = root.create(nullTOP, 'null_out')
glsl.outputConnectors[0].connect(null_out.inputConnectors[0])

# 2. 引用 null_out 的 feedback
feedback = root.create(feedbackTOP, 'feedback')
feedback.par.top = 'null_out'

# 3. 黑色初始状态
const_init = root.create(constantTOP, 'const_init')
const_init.par.colorr = 0; const_init.par.colorg = 0; const_init.par.colorb = 0

# 4. 连线：初始 → feedback，feedback → 处理
feedback.inputConnectors[0].connect(const_init)
glsl.inputConnectors[0].connect(feedback)

# 5. 重置以应用初始状态
feedback.par.resetpulse.pulse()
```

### 常见错误

| 错误 | 原因 | 解决方案 |
|-------|-------|----------|
| "Not enough sources specified" | 未连接输入 | 连接初始状态 TOP |
| 出现意外的初始图案 | 初始状态错误 | 使用 Constant TOP（黑色） |

### 提示

1. 模拟使用 float 格式：`glsl.par.format = 'rgba32float'`
2. 配置完成后重置：`feedback.par.resetpulse.pulse()`
3. 分辨率要匹配——feedback、处理和初始状态三者必须一致
4. 软边界可避免边缘伪影：
   ```glsl
   float edge = 3.0 * texel.x;
   float bx = smoothstep(0.0, edge, uv.x) * smoothstep(0.0, edge, 1.0 - uv.x);
   float by = smoothstep(0.0, edge, uv.y) * smoothstep(0.0, edge, 1.0 - uv.y);
   value *= bx * by;
   ```

### 使用场景
- **波浪模拟** — R=高度，G=速度，黑色初始状态
- **元胞自动机** — 白色=存活，黑色=死亡，随机噪声初始状态
- **拖尾 / 运动模糊** — 将当前帧与 feedback 混合，黑色初始状态
