# 动画参考

基于时间的运动模式 —— 关键帧、LFO、计时器、缓动、表达式驱动的动画。

在设置参数前始终调用 `td_get_par_info` 查询该算子类型。下方参数名对应 TD 2025.32，但若报错请核实。

---

## 时间源

TD 有三种时间引用 —— 请选择合适的。

| 表达式 | 行为 | 用于 |
|---|---|---|
| `absTime.seconds` | 自 TD 启动以来的挂钟秒数。永不重置。 | 连续运动、GLSL `uTime`、无限循环 |
| `absTime.frame` | 挂钟帧计数。 | 帧精确触发 |
| `me.time.frame` | 本地组件帧计数（播放/停止时重置）。 | 单个 COMP 的动画时间线 |
| `me.time.seconds` | 本地组件秒数。 | 同上，以秒为单位 |

**原则：** 着色器和连续运动用 `absTime.seconds`；COMP 内的触发/循环动画用 `me.time.*`。

---

## LFO CHOP —— 循环运动

最简单的周期驱动器。快速、GPU 友好、易于表达式使用。

```python
lfo = root.create(lfoCHOP, 'rot_driver')
lfo.par.type = 'sin'        # 'sin' | 'cos' | 'ramp' | 'square' | 'triangle' | 'pulse'
lfo.par.frequency = 0.25    # 每秒循环次数
lfo.par.amplitude = 1.0
lfo.par.offset = 0.0
lfo.par.phase = 0.0         # 0-1，便于并行偏移多个 LFO
```

**通过 export 驱动参数：**

```python
op('/project1/geo1').par.rx.mode = ParMode.EXPRESSION
op('/project1/geo1').par.rx.expr = "op('rot_driver')['chan1'] * 360"
```

**多个同步 LFO（X/Y/Z 旋转带相位偏移）：**
创建一个含三个通道的 LFO 并逐个偏移相位，或用三个 LFO 并错开它们的 `phase` 参数（0.0、0.33、0.66）。

---

## Timer CHOP —— 触发式序列

用于单次播放动画、节拍锁定序列或基于阶段的逻辑。

```python
timer = root.create(timerCHOP, 'fade_timer')
timer.par.length = 4.0       # 循环长度（秒）
timer.par.cycle = False      # 单次运行 vs 循环
timer.par.outputseconds = True
```

输出通道：`timer_fraction`（在循环内 0→1）、`running`、`done`、`cycles`。

**启动计时器：**
```python
timer.par.start.pulse()
```

**驱动淡入淡出：**
```python
op('/project1/level1').par.opacity.mode = ParMode.EXPRESSION
op('/project1/level1').par.opacity.expr = "op('fade_timer')['timer_fraction']"
```

**对计时器分值应用缓动** —— 直接在表达式里处理：

```python
# 平滑阶梯（smoothstep）：缓入缓出
expr = "smoothstep(0, 1, op('fade_timer')['timer_fraction'])"
# 三次缓出（cubic ease-out）：1 - (1-t)^3
expr = "1 - pow(1 - op('fade_timer')['timer_fraction'], 3)"
```

---

## Pattern CHOP —— 自定义曲线

用于任意波形（锯齿斜坡、缓动曲线、自定义包络）。

```python
pat = root.create(patternCHOP, 'envelope')
pat.par.type = 'gaussian'    # 'gaussian' | 'ramp' | 'square' | 'sin' | 等
pat.par.length = 60          # 采样数
pat.par.cyclelength = 1.0    # 在 TD 帧率下的秒数
```

配合 `lookupCHOP`，可将一个 0-1 驱动量重新映射到自定义曲线上。

---

## Animation COMP —— 基于关键帧

用于多关键帧的动态图形。每个 animationCOMP 持有可在动画编辑器中编辑的关键帧通道。

```python
anim = root.create(animationCOMP, 'intro_anim')
# 默认含 chan1..chanN 通道；通过以下方式访问：
# op('intro_anim').par.length、.par.play、.par.cue 等

# 用通道驱动参数
op('/project1/text1').par.tx.mode = ParMode.EXPRESSION
op('/project1/text1').par.tx.expr = "op('intro_anim/out1')['chan1']"
```

**关键帧通常在 UI（动画编辑器）中编辑**，但也可以通过内部 `keyframes` 表设置。要以编程方式创建关键帧，使用 `td_execute_python`：

```python
# 获取 animationCOMP 内部的通道 CHOP
ch = op('/project1/intro_anim/chans')
# 插入关键帧（高级 API —— 用 td_get_par_info(op_type='animationCOMP') 核实）
ch.appendKey('chan1', frame=0, value=0.0, expression=None)
ch.appendKey('chan1', frame=120, value=1.0)
```

对大多数用例，用 LFO/Timer/Pattern CHOP 驱动参数更简单且可脚本化。

---

## 表达式中的缓动

TD 的表达式求值器支持 Python 数学。常见缓动形式：

```python
# 线性
"t"

# 平滑阶梯（经典缓入缓出）
"smoothstep(0, 1, t)"

# 三次缓出
"1 - pow(1 - t, 3)"

# 三次缓入
"pow(t, 3)"

# 三次缓入缓出
"3*t*t - 2*t*t*t"

# 弹跳（手工简化版）
"abs(sin(t * 6.28 * 3) * (1 - t))"
```

其中 `t` 是 `op('fade_timer')['timer_fraction']` 或任意 0-1 驱动量。

---

## Filter CHOP —— 平滑既有通道

在用抖动值（如音频分析、传感器数据）驱动视觉之前做平滑处理。

```python
filt = root.create(filterCHOP, 'smooth')
filt.par.filter = 'gaussian'   # 或 'lowpass'
filt.par.width = 0.5            # 平滑窗口（秒）
filt.inputConnectors[0].connect(op('raw_signal'))
```

**警告：** 切勿在 timeslice 模式下对 AudioSpectrum 输出使用 Filter CHOP —— 它会扩展采样数并把各频段平均到接近零。参见 `audio-reactive.md`。

---

## Lag CHOP —— 非对称的起音/释放

为上升与下降设置不同速度。是可视化音频包络的标准做法。

```python
lag = root.create(lagCHOP, 'env_smooth')
lag.par.lag1 = 0.02   # 起音（上升时间，秒）
lag.par.lag2 = 0.30   # 释放（下降时间，秒）
lag.inputConnectors[0].connect(op('raw_envelope'))
```

快起音、慢释放 = 经典 VU 表的观感。

---

## 通过 Script DAT 实现逐帧驱动

对于不适合用表达式表达的复杂逐帧逻辑，可使用 `executeDAT`（`onFrameStart` 回调）或 `chopExecuteDAT`。

```python
# 在 executeDAT（frameStart）中：
def onFrameStart(frame):
    t = absTime.seconds
    op('/project1/circle').par.tx = math.sin(t * 2.0) * 3.0
    op('/project1/circle').par.ty = math.cos(t * 2.0) * 3.0
    return
```

繁重的逻辑仍应放在 CHOP 中（CPU 开销低、确定性高）。脚本应留给一次性任务或非实时分支。

---

## 陷阱

1. **帧率依赖** —— `me.time.frame` 以 TD 工程帧为单位（默认 60）。若工程帧率改变，运动速度也会变。使用 `seconds` 可获得与速率无关的计时。
2. **运算开销** —— 每个驱动参数的 CHOP 每帧都会 cook。请合并驱动源（一个大 mathCHOP 胜过许多小 mathCHOP）。
3. **表达式模式** —— 参数默认为 `CONSTANT`。除非 `par.X.mode = ParMode.EXPRESSION`，否则 `par.X.expr = ...` 会被忽略。
4. **动画编辑器编辑** —— 通过 UI 设置的关键帧存在 animationCOMP 内部关键帧表中，保存/重开都会保留。通过 `appendKey()` 以编程方式生成关键帧也可行，但请先用 `td_get_docs(topic='animation')` 核实 API。
5. **循环动画** —— 要无缝循环，`length` 必须等于 `cyclelength`，且首尾值必须一致，否则会有可见跳变。

---

## 快速配方

| 目标 | 最简路径 |
|---|---|
| 连续旋转 | LFO CHOP `type='ramp'`，表达式 → `geo.par.rx` |
| 2 秒淡入 | Timer CHOP `length=2`，smoothstep 表达式 → `level.par.opacity` |
| 每个节拍脉冲 | 音频驱动的 `triggerCHOP` → 通过表达式驱动缩放 |
| 3D 利萨茹轨道 | 两个不同频率的 LFO，驱动 `tx`/`ty`/`tz` |
| 随机抖动 | 低频 `noiseCHOP` 叠加到位置上 |
| 定时场景切换 | Timer CHOP → switchTOP/CHOP 的 `index` |
