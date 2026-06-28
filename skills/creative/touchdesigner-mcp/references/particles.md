# 粒子参考

TouchDesigner 中的粒子系统 —— 现代 POPs（粒子算子）和传统的 particleSOP 路径。

关于静态几何体实例化（无每个实例的生命周期/速度），见 `geometry-comp.md`。关于 GLSL 驱动的反馈模拟（无粒子抽象），见 `operator-tips.md`（Feedback TOP 部分）。

在设置参数前始终调用 `td_get_par_info` 查询该算子类型。下方参数名对应 TD 2025.32 —— 依赖前请核实。

---

## 两条路径：POPs vs. SOPs

| | **POP 家族**（现代） | **particleSOP**（传统） |
|---|---|---|
| GPU？ | 是（compute） | 否（CPU） |
| 粒子数量 | 轻松 10 万+ | 约 5 千开始变慢 |
| API 风格 | Source / Force / Solver / Render 链 | 单个带大量参数的算子 |
| 用于 | 新项目、任何密集场景 | 快速演示、低数量、TD < 2023 |

**默认用 POPs。** 仅当所需的某个算子没有 POP 版本时，才回退到 particleSOP。

---

## POP 流水线概览

一个 POP 系统是 `geometryCOMP` 内的一条算子链：

```
popSourceTOP / popSourceSOP   ← 生成新粒子
        ↓
popForceTOP（重力、风力等）
        ↓
popForceTOP（吸引子、涡旋……）
        ↓
popDeleteTOP（生命周期、边界）
        ↓
popSolverTOP                  ← 积分速度、更新位置
        ↓
[通过 geometryCOMP / glslMAT 实例化渲染]
```

POP 缓冲区携带标准通道：`P`（位置）、`v`（速度）、`life`、`id`、`Cd`（颜色），以及你添加的任意自定义通道。

---

## 最小 POP 设置

```python
# 创建一个 geometry COMP 来承载 POP 网络
geo = root.create(geometryCOMP, 'particles_geo')

# 1. 源 —— 从一个点发射粒子
src = geo.create(popSourceTOP, 'src')
src.par.birthrate = 500          # 每秒
src.par.life = 4.0                # 秒

# 2. 重力
grav = geo.create(popForceTOP, 'gravity')
grav.par.forcetype = 'gravity'
grav.par.fy = -9.8

# 3. 生命周期清理
delp = geo.create(popDeleteTOP, 'cull')
delp.par.condition = 'lifeleq'    # 当 life <= 0 时删除
delp.par.value = 0

# 4. 求解器
solv = geo.create(popSolverTOP, 'solver')
solv.par.timestep = 'frame'

# 连线：source → force → delete → solver
src.outputConnectors[0].connect(grav.inputConnectors[0])
grav.outputConnectors[0].connect(delp.inputConnectors[0])
delp.outputConnectors[0].connect(solv.inputConnectors[0])
```

`popSolverTOP` 的输出就是实时粒子缓冲区。通过在小型 SOP（球体、点）上用 `glslMAT` 实例化来渲染，把每个粒子当作一个“形状”。

---

## 常见力

| 力类型 | 效果 | 常用参数 |
|---|---|---|
| `gravity` | 恒定方向拉力 | `fx`、`fy`、`fz` |
| `wind` | 恒定速度叠加 | `wx`、`wy`、`wz` |
| `drag` | 随时间阻尼速度 | `dragstrength` |
| `noise` | 卷曲噪声湍流 | `noiseamp`、`noisefreq`、`noiseseed` |
| `attractor` | 朝某点拉拽 | `position`、`strength`、`falloff` |
| `vortex` | 绕轴旋转 | `axis`、`strength` |
| `point`（自定义） | GLSL 求值的任意力 | 通过 `popforceadvancedTOP` |

多个 `popForceTOP` 串联叠加 —— 每个加性地修改速度。

---

## 生命周期模式

### 连续发射（如烟柱）

```python
src.par.birthrate = 800
src.par.life = 6.0       # 通过 'lifevariance' 加方差
src.par.lifevariance = 1.5
```

### 爆发式发射（如爆炸）

```python
src.par.birthrate = 0    # 不连续发射
src.par.burst.pulse()    # 按需一次性爆发（核实参数名）
src.par.burstcount = 5000
src.par.life = 1.5
```

### 节拍触发的爆发

把一个 `triggerCHOP`（来自音频或 MIDI）接到脉冲化爆发：

```python
op('/project1/audio_kick_trigger').outputConnectors[0].connect(...)
# 然后通过 chopExecuteDAT，每次底鼓：
def offToOn(channel, sampleIndex, val, prev):
    op('/project1/particles_geo/src').par.burst.pulse()
    return
```

---

## 渲染粒子

### 点精灵（最简单）

```python
# 在 geometryCOMP 内，直接渲染求解器输出
# geo 的第一个 SOP 子级成为几何体
# 但对 POPs，我们通常在小型“形状”上用 glslMAT 渲染

# 每个粒子的简易广告牌球体：
shape = geo.create(sphereSOP, 'shape')
shape.par.rad = 0.05
shape.par.rows = 6; shape.par.cols = 6   # 低面数以保持快速

# 用 POP 缓冲区做实例化的材质
mat = root.create(glslMAT, 'particle_mat')
# 配置 mat.par.instancingTOP = 求解器输出（核实参数名）
```

确切的实例化设置随 TD 版本不同 —— 调用 `td_get_hints(topic='popInstancing')`（或 `popRender` / `instancing` —— 多试几个）。

### 通过 glslcopyPOP 实现 GPU 精灵

对于密集的烟/火类效果，用 `glslcopyPOP` 从 compute 着色器写入每粒子的颜色/尺寸，再在 `renderTOP` 中以相加混合渲染为点精灵。

---

## 碰撞

```python
# 对一个 SOP 做碰撞检测
coll = geo.create(popCollideTOP, 'ground_coll')
coll.par.collidewithsop = '/project1/ground_geo'  # 碰撞 SOP 的路径
coll.par.bounce = 0.3
coll.par.friction = 0.1
# 插在 force 与 solver 之间
```

若仅需平面/盒子碰撞，用 `popPlaneCollideTOP`（更省）。

---

## 自定义每粒子数据

通过 `popAttribCreateTOP`（或用 `glslcopyPOP` 写入）添加自定义通道：

```python
# 添加一个每粒子随机初始化的 "phase" 属性，供渲染着色器使用
attr = geo.create(popAttribCreateTOP, 'add_phase')
attr.par.attribname = 'phase'
attr.par.value0 = 'rand(@id)'   # TD 的 POP 属性语言表达式
```

然后在渲染着色器中 `texture(sTDPOPInputs[0].phase, ...)`（或你的 TD 版本所用的采样器约定 —— 用 `td_get_docs(topic='pops')` 核实）。

---

## 传统 particleSOP（谨慎使用）

用于快速演示或低数量系统：

```python
# 在一个 geo 内
psrc = geo.create(addSOP, 'point_src')      # 源：单个点
psrc.par.points = '0 0 0'

part = geo.create(particleSOP, 'particles')
part.par.life = 3.0
part.par.birthrate = 100
part.par.gravityy = -9.8
part.par.windx = 0.5
part.inputConnectors[0].connect(psrc)
```

基于 CPU。超过约 5000 个活动粒子就会出现掉帧。

---

## 陷阱

1. **粒子不显示** —— 通常是渲染侧问题。对求解器输出用 `td_get_screenshot` 检查（较新的 TD 会把缓冲区渲染为类 TOP 视图）。再检查 `geometryCOMP` 的渲染路径。
2. **爆发不触发** —— 核实 `burst` 参数是脉冲而非切换。脉冲必须用 `.pulse()`，而不是 `= True`。
3. **粒子在首帧瞬移** —— 速度未初始化。设置 `popSourceTOP.par.initialvelocityX/Y/Z`，或显式置零。
4. **重力感觉不对** —— TD 的“1 个单位”取决于你的场景尺度。从 `fy = -1.0` 起步再放大，而不是直接用现实世界的 9.8。
5. **高出生率 = 卡顿** —— birthrate 是每秒，不是每帧。在 60fps 下，`birthrate = 6000` 相当于 100/帧，没问题；`birthrate = 600000` 会拖垮。
6. **POP 求解器顺序很重要** —— 力按链中顺序施加。把重力放在 drag 之后会同时阻尼重力本身；通常不是你想要的。
7. **实例化参数名各异** —— `mat.par.instancingTOP` vs. `mat.par.instanceop` vs. `mat.par.instances` 在不同 TD 版本中不同。始终用 `td_get_par_info(op_type='glslMAT')` 检查。
8. **cook 依赖循环** —— POP 求解器会创建隐式时间循环。出现“cook dependency loop”警告对 POPs 是预期且无害的。
9. **CHOP 驱动的力值** —— 当力参数被表达式绑定到 CHOP（如音频响应的重力）时，确保该 CHOP 在求解器之前 cook。否则力会滞后一帧。

---

## 性能目标

| 粒子数量 | 设置 | 60fps 帧预算 |
|---|---|---|
| < 1k | particleSOP 即可 | 微不足道 |
| 1k - 10k | POPs、简单力 | 约 2-5ms |
| 10k - 10 万 | POPs、仅 GPU 力 | 约 5-15ms |
| 10 万+ | `glslcopyPOP`、自定义 compute | 约 10-25ms |
| 100 万+ | 自定义 GPU 缓冲区、不用 POP 框架 | 取决于着色器 |

用 `td_get_perf` 找出 POP 链中的瓶颈算子。

---

## 快速配方

| 目标 | 流水线 |
|---|---|
| 烟柱 | `popSourceTOP`（点）→ 重力 + 风 + 噪声 → `popDeleteTOP`（生命）→ 求解器 → glslMAT 实例化 |
| 节拍触发爆发 | `triggerCHOP`（音频）→ chopExecuteDAT 脉冲化 `popSourceTOP.par.burst` |
| 烟花弹 | 在某点爆发 → drag + 重力 → 到达生命周期阈值时二次爆发 |
| 雪/雨 | 跨 XZ 平面（高 y）连续发射、重力 + 微风、无限生命由盒子删除 |
| 火花 | 爆发、极短生命（0.3s）、明亮相加渲染、用反馈做运动模糊 |
| 音频粒子 | 出生率由音频包络驱动、颜色由频段驱动 |
