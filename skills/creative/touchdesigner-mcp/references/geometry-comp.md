# Geometry COMP 参考

## 创建 Geometry COMP

```python
geo = root.create(geometryCOMP, 'geo1')
# 删除默认的圆环
for c in list(geo.children):
    if c.valid: c.destroy()
# 在内部构建你的形状
```

## 正确模式（形状放在 geo 内部）

```python
# 在 geo COMP 内部创建形状
box = geo.create(boxSOP, 'cube')
box.par.sizex = 1.5; box.par.sizey = 1.5; box.par.sizez = 1.5

# 对于基于 POP 的几何体（TD 099），POP 必须位于内部：
sph = geo.create(spherePOP, 'shape')
out1 = geo.create(outPOP, 'out1')
out1.inputConnectors[0].connect(sph.outputConnectors[0])
```

## 不要这样做：常见错误

```python
# 错误：不要在父层级创建几何体再连线进 COMP
box = root.create(boxPOP, 'box1')  # ← 在 geo 外部，不会渲染

# 错误：不要从 COMP 内部引用父级算子
choptopop1.par.chop = '../null1'  # ← 隐藏依赖，移动时会失效
```

## 实例化

```python
geo.par.instancing = True
geo.par.instanceop = 'sopto1'    # 指向包含实例数据的 CHOP/SOP 的相对路径
geo.par.instancetx = 'tx'
geo.par.instancety = 'ty'
geo.par.instancetz = 'tz'
```

### 按 OP 类型划分的实例属性名

| OP 类型 | 属性名 |
|---------|-----------------|
| CHOP | 通道名：`tx`、`ty`、`tz` |
| SOP/POP | `P(0)`、`P(1)`、`P(2)` 表示位置 |
| DAT | 第一行的列标题名 |
| TOP | `r`、`g`、`b`、`a` |

### 混合数据源

```python
geo.par.instanceop = 'pos_chop'       # 从 CHOP 取位置
geo.par.instancetx = 'tx'
geo.par.instancecolorop = 'color_top' # 从 TOP 取颜色
geo.par.instancecolorr = 'r'
```

## 渲染设置

```python
# 摄像机
cam = root.create(cameraCOMP, 'cam1')
cam.par.tx = 0; cam.par.ty = 0; cam.par.tz = 4

# Render TOP
render = root.create(renderTOP, 'render1')
render.par.outputresolution = 'custom'
render.par.resolutionw = 1280; render.par.resolutionh = 720
render.par.camera = cam.path
render.par.geometry = geo.path  # 接受路径字符串
```

## 用于渲染的 POP 与 SOP 对比

在 TD 099 中，`geometryCOMP` 渲染 **POP** 但不渲染 SOP。放在 geometry COMP 内部的 `boxSOP` 是不可见的——而且不会报错。

```python
# 错误 —— SOP 不会渲染（不可见，无报错）
box = geo.create(boxSOP, 'cube')       # ✗ 不可见

# 正确 —— POP 会渲染
box = geo.create(boxPOP, 'cube')       # ✓ 可见
```

| SOP | POP | 说明 |
|-----|-----|-------|
| `boxSOP` | `boxPOP` | `sizex/y/z`、`surftype` |
| `sphereSOP` | `spherePOP` | `radx/y/z`、`freq`、`type`（geodesic/grid/sharedpoles/tetrahedron） |
| `torusSOP` | `torusPOP` | TD 在新建 geo COMP 时会自动创建 |
| `circleSOP` | `circlePOP` | |
| `gridSOP` | `gridPOP` | |
| `tubeSOP` | `tubePOP` | |

新建的 geometry COMP 会自动创建：`in1`（inPOP）、`out1`（outPOP）、`torus1`（torusPOP）。构建前务必先清理。

## 在形状之间变形（switchPOP）

```python
sw = geo.create(switchPOP, 'shape_switch')
sw.par.index.expr = 'int(absTime.seconds / 3) % 4'
sw.inputConnectors[0].connect(tetra.outputConnectors[0])  # 形状 0
sw.inputConnectors[1].connect(box.outputConnectors[0])    # 形状 1
sw.inputConnectors[2].connect(octa.outputConnectors[0])   # 形状 2
sw.inputConnectors[3].connect(sphere.outputConnectors[0]) # 形状 3

out = geo.create(outPOP, 'out1')
out.inputConnectors[0].connect(sw.outputConnectors[0])
```

`spherePOP.par.type` 选项：`geodesic`、`grid`、`sharedpoles`、`tetrahedron`。对柏拉图多面体使用 `tetrahedron`。

## 杂项

- `connect()` 会替换已有连接——无需先断开
- `project.name` 返回 TOE 文件名，`project.folder` 返回所在目录
