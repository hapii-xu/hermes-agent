# 音频响应参考

用音频驱动视觉的模式 —— 频谱分析、节拍检测、包络跟随。

## 音频输入

```python
# 来自音频接口的实时输入
audio_in = root.create(audiodeviceinCHOP, 'audio_in')
audio_in.par.rate = 44100

# 或：来自音频文件（用于测试）
audio_file = root.create(audiofileinCHOP, 'audio_in')
audio_file.par.file = '/path/to/track.wav'
audio_file.par.play = True
audio_file.par.repeat = 'on'       # 不是 par.loop
audio_file.par.playmode = 'locked'
```

---

## 音频频段提取（已在 TD 2025.32460 上验证）

使用 `audiofilterCHOP` 进行频段分离（不要用按通道索引的 `selectCHOP`）：

```python
# 音频输入
af = root.create(audiofileinCHOP, 'audio_in')
af.par.file = path
af.par.play = True
af.par.repeat = 'on'
af.par.playmode = 'locked'

# 低频段：250Hz 处低通
flt_low = root.create(audiofilterCHOP, 'flt_low')
flt_low.par.filter = 'lowpass'
flt_low.par.cutofffrequency = 250
flt_low.par.rolloff = 2
flt_low.inputConnectors[0].connect(af)

# 中频段：250Hz 高通 → 4000Hz 低通
flt_mid_hp = root.create(audiofilterCHOP, 'flt_mid_hp')
flt_mid_hp.par.filter = 'highpass'
flt_mid_hp.par.cutofffrequency = 250
flt_mid_hp.par.rolloff = 2
flt_mid_hp.inputConnectors[0].connect(af)

flt_mid_lp = root.create(audiofilterCHOP, 'flt_mid_lp')
flt_mid_lp.par.filter = 'lowpass'
flt_mid_lp.par.cutofffrequency = 4000
flt_mid_lp.par.rolloff = 2
flt_mid_lp.inputConnectors[0].connect(flt_mid_hp)

# 高频段：4000Hz 处高通
flt_high = root.create(audiofilterCHOP, 'flt_high')
flt_high.par.filter = 'highpass'
flt_high.par.cutofffrequency = 4000
flt_high.par.rolloff = 2
flt_high.inputConnectors[0].connect(af)

# 每个频段：RMS → lag → gain → clamp
for name, filt in [('low', flt_low), ('mid', flt_mid_lp), ('high', flt_high)]:
    rms = root.create(analyzeCHOP, f'rms_{name}')
    rms.par.function = 'rmspower'  # 不是 'rms'
    rms.inputConnectors[0].connect(filt)

    lag = root.create(lagCHOP, f'lag_{name}')
    lag.par.lag1 = 0.05   # 起音（不是 par.lagin）
    lag.par.lag2 = 0.25   # 释放（不是 par.lagout）
    lag.inputConnectors[0].connect(rms)

    math = root.create(mathCHOP, f'scale_{name}')
    math.par.gain = 8.0
    math.inputConnectors[0].connect(lag)

    # mathCHOP 没有 par.clamp —— 改用 limitCHOP
    lim = root.create(limitCHOP, f'clamp_{name}')
    lim.par.type = 'clamp'
    lim.par.min = 0.0
    lim.par.max = 1.0
    lim.inputConnectors[0].connect(math)

    null = root.create(nullCHOP, f'out_{name}')
    null.inputConnectors[0].connect(lim)
    null.viewer = True
```

**TD 2025 的关键更正：**
- `analyzeCHOP.par.function = 'rmspower'`，不是 `'rms'`
- `lagCHOP.par.lag1` / `par.lag2`，不是 `par.lagin` / `par.lagout`
- `mathCHOP` 没有 `par.clamp` —— 用单独的 `limitCHOP`

---

## 节拍 / 起音检测

### 底鼓检测（slope → trigger）

```python
slope = root.create(slopeCHOP, 'kick_slope')
slope.inputConnectors[0].connect(op('out_low'))

trig = root.create(triggerCHOP, 'kick_trig')
trig.par.threshold = 0.12
trig.par.attack = 0.005    # 不是 par.attacktime
trig.par.decay = 0.15       # 不是 par.decaytime
trig.par.triggeron = 'increase'
trig.inputConnectors[0].connect(slope)

kick_out = root.create(nullCHOP, 'out_kick')
kick_out.inputConnectors[0].connect(trig)
```

---

## 将音频传给 GLSL

```python
glsl.par.vec0name = 'uLow'
glsl.par.vec0valuex.expr = "op('out_low')['chan1']"
glsl.par.vec0valuex.mode = ParMode.EXPRESSION

glsl.par.vec1name = 'uKick'
glsl.par.vec1valuex.expr = "op('out_kick')['chan1']"
glsl.par.vec1valuex.mode = ParMode.EXPRESSION
```

```glsl
uniform float uLow;
uniform float uKick;
float scale = 1.0 + uKick * 0.4 + uLow * 0.2;
```

---

## 标准音频总线模式

推荐结构：

```
audiodeviceinCHOP (audio_in)
        ↓
  [null_audio_in]
        ├──→ audiofilterCHOP (250Hz 低通) → analyzeCHOP → lagCHOP → mathCHOP → limitCHOP → null
        ├──→ audiofilterCHOP (250-4k 带通) → analyzeCHOP → lagCHOP → mathCHOP → limitCHOP → null
        ├──→ audiofilterCHOP (4k 高通) → analyzeCHOP → lagCHOP → mathCHOP → limitCHOP → null
        │
        └──→ slopeCHOP → triggerCHOP (beat_trigger)
```

把整条总线放在一个 `baseCOMP`（例如 `audio_bus`）内，视觉网络通过路径引用它。

---

## MIDI 输入

```python
midi_in = root.create(midiinCHOP, 'midi_in')
midi_in.par.device = 0  # 用 midiinDAT 查看设备索引
# 输出通道按 MIDI 音符/CC 命名：'ch1n60'、'ch1c74' 等

# 将 CC 映射到参数
op('bloom1').par.threshold.mode = ParMode.EXPRESSION
op('bloom1').par.threshold.expr = "op('midi_in')['ch1c74'][0]"
```

---

## 关键：切勿用 Lag CHOP 对频谱做平滑

Lag CHOP 在 timeslice 模式下会把 256 采样的频谱扩展到 1600-2400 个采样，所有值被平均到接近零（约 1e-06）。着色器收不到可用数据。请直接用 `mathCHOP(gain=8)`，或在 GLSL 中通过反馈纹理做时间插值实现平滑。

已验证：
- 不加 Lag CHOP 时：低频频段 = 5.0-5.4（强、可用）
- 加 Lag CHOP 时：所有频段 = 0.000001（失效）
