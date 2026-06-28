# MIDI / OSC 参考

外部控制器的输入与输出 —— MIDI 硬件、TouchOSC 移动端 UI、跨网络的 OSC 路由。

关于音频驱动的 MIDI 模式（从频谱分析触发音轨），另见 `audio-reactive.md`。

---

## MIDI 输入 —— 硬件控制器

### 设备发现

先列出已连接的 MIDI 设备。用 `midiinDAT` 枚举：

```python
mdat = root.create(midiinDAT, 'mid_devices')
# cook 一次后从该 DAT 读取可用设备名
```

或直接通过 Python：

```python
# 在 td_execute_python 中
import td
devices = [d for d in op.MIDI.devices]   # 用 td_get_docs('midi') 核实
```

由于这在各 TD 版本间存在差异，请用 `td_get_docs(topic='midi')` 核实 API。

### MIDI In CHOP

标准模式：

```python
midi_in = root.create(midiinCHOP, 'midi_in')
midi_in.par.device = 0               # 来自发现的设备索引
midi_in.par.activechan = True
```

输出通道遵循 `chCcN` 和 `chCnN` 的命名约定：
- `ch1c74` —— 通道 1，CC 74
- `ch1n60` —— 通道 1，音符 60（中央 C）—— 值为力度 0-127

**将 CC 映射到参数：**

```python
op('/project1/bloom1').par.threshold.mode = ParMode.EXPRESSION
op('/project1/bloom1').par.threshold.expr = "op('midi_in')['ch1c74'][0] / 127.0"
```

**将音符映射为触发：**

`midiinCHOP` 中的音符在按下时输出力度，松开时为 0。用 `triggerCHOP` 把按住的音符转换为脉冲：

```python
trig = root.create(triggerCHOP, 'note_trig')
trig.par.threshold = 1
trig.par.triggeron = 'increase'
trig.inputConnectors[0].connect(op('midi_in'))
# 如需，可用 selectCHOP 过滤到单个通道
```

### MIDI Learn 模式

当你事先不知道控制器的 CC 布局时，可构建可复用的 learn 模式：

1. 放一个 `midiinCHOP` 和它后面的 `selectCHOP`。
2. 用户扭动控制器旋钮。
3. 对 midiinCHOP 用 `td_read_chop`，找出哪个通道非零 —— 那就是当前活动的 CC。
4. 把 `selectCHOP.par.channames` 设为该通道名。
5. 把映射保存到一个 `tableDAT`，跨会话持久化。

---

## MIDI 输出

```python
midi_out = root.create(midioutCHOP, 'midi_out')
midi_out.par.device = 0
midi_out.par.outputformat = 'continuous'    # 'continuous' | 'event'

# 驱动输出：从任意 0-1 源映射并发送一个 CC
src = root.create(constantCHOP, 'cc_src')
src.par.name0 = 'ch1c20'
src.par.value0 = 0.5
midi_out.inputConnectors[0].connect(src)
```

对于音符事件，使用 `event` 模式，并用 `pulseCHOP` 或 `triggerCHOP` 脉冲化其值。

---

## OSC 输入 —— 网络控制

OSC 是 MIDI 更灵活的表亲。广泛用于：
- TouchOSC / Lemur 移动控制面板
- 演出控制系统（QLab、Watchout）
- 应用间同步（通过 Max for Live 的 Ableton、Resolume 等）

### OSC In CHOP

```python
osc_in = root.create(oscinCHOP, 'osc_in')
osc_in.par.port = 7000             # 监听 UDP 7000
osc_in.par.localaddress = ''       # 空 = 所有网卡
osc_in.par.queued = False          # 立即处理 vs 排队处理
```

每个传入的 OSC 地址会成为一个通道。`/scene/1/intensity` 会变成名为 `scene_1_intensity` 的通道（TD 把斜杠替换为下划线）。

**常见坑：** TD 只在该地址收到第一条消息后才创建通道。设置期间从控制器发一条“hello”消息，或手动预先声明通道名。

### OSC In DAT（用于原始事件）

当你需要完整消息访问（多个带类型的参数、带括号/正则的地址）时，用 `oscinDAT`。

```python
osc_dat = root.create(oscinDAT, 'osc_events')
osc_dat.par.port = 7001
# 每行：时间戳、地址、类型标签、参数……
```

通过监视 `oscinDAT` 的 `datExecuteDAT` 驱动逻辑：

```python
def onTableChange(dat):
    last = dat[dat.numRows - 1, 'message']
    parsed = last.val.split()
    addr = parsed[0]
    args = parsed[1:]
    if addr == '/scene/trigger':
        op('/project1/scene_switcher').par.index = int(args[0])
    return
```

---

## OSC 输出 —— 发送到外部应用

```python
osc_out = root.create(oscoutCHOP, 'osc_out')
osc_out.par.netaddress = '127.0.0.1'    # 目标 IP
osc_out.par.port = 9000

# 通道名变为 OSC 地址
src = root.create(constantCHOP, 'send')
src.par.name0 = 'scene/intensity'        # → /scene/intensity
src.par.value0 = 0.7
osc_out.inputConnectors[0].connect(src)
```

**通道到地址的映射：** TD 会自动前置 `/`。在通道名中用 `/` 可实现嵌套。

对于一次性字符串/带类型消息，用 `oscoutDAT` 并调用 `.sendOSC(address, args)`：

```python
op('osc_out_dat').sendOSC('/scene/trigger', [1, 'fade'])
```

---

## TouchOSC / 移动端 UI 模式

从手机/平板做现场 VJ 控制的常见设置：

1. **配置 TouchOSC 布局** —— 为每个控件分配一个 OSC 地址，如 `/vj/master`、`/vj/scene/1` 等。
2. **找到你机器的局域网 IP** —— TouchOSC 需要指向它。
3. **TD 监听** `oscinCHOP.par.port = 8000`（或其他端口）。
4. **通过表达式把通道映射到参数**：

```python
op('/project1/master_level').par.opacity.mode = ParMode.EXPRESSION
op('/project1/master_level').par.opacity.expr = "op('osc_in')['vj_master']"
```

5. **通过 `oscoutCHOP` 向控制器回送反馈** —— 便于在多设备间同步状态。

---

## 网络 / 多机

OSC 在局域网上开箱即用。用于多 TD 实例同步（例如投影集群）：

- 一台 TD 作 **主控**，通过 OSC 广播 `/sync/...`
- 工作机 TD 运行 `oscinCHOP` 监听同一端口
- 在主控的 `oscoutCHOP.par.netaddress` 上使用 UDP **广播地址**（如 `192.168.1.255`）以触达所有对端

为在广域网上更可靠，改用 `webserverDAT` 或 `websocketDAT` 配合外部中继 —— UDP 丢包是不可见的。

---

## 陷阱

1. **MIDI 设备索引** —— 设备 `0` 是 TD 最先枚举到的设备。重新排序可能使其偏移。尽可能按名称锁定。
2. **OSC 通道名** —— 在第一条消息落地前 TD 不会创建通道。新通道在首次到达时会使已 cook 的依赖失效，造成一帧卡顿。
3. **OSC queued 模式** —— `par.queued = True` 把处理推迟到每帧一次性批处理。延迟更低，但同一帧内到达的多条消息会塌缩到最后一个值。触发场景关闭、连续旋钮场景开启。
4. **MIDI clock 与 transport** —— `midiinCHOP` 在可用时会报告 clock。用 `midisyncCHOP`（若你的 TD 版本提供）或从 clock 脉冲计算 BPM（每个四分音符 24 个脉冲）。
5. **延迟** —— 有线 MIDI 约 1-3ms。WiFi OSC 为 10-30ms 且有抖动。紧凑的节拍锁定工作请用有线。
6. **端口冲突** —— 在大多数 OS 上一个 UDP 端口只能被一个进程绑定。若 `oscinCHOP` 收不到流量，检查是否有别的应用（Max、Ableton 等）已在监听该端口。

---

## 快速配方

| 目标 | 算子链 |
|---|---|
| 旋钮 → bloom 强度 | `midiinCHOP` → 在 `bloom.par.threshold` 上加表达式 |
| 音符 → 场景切换 | `midiinCHOP` → `triggerCHOP` → `selectCHOP` → 驱动 `switchTOP.par.index` |
| 手机滑块 → 主推子 | TouchOSC `/master` → `oscinCHOP` → 在输出 `level.par.opacity` 上加表达式 |
| TD → Resolume 场景触发 | `oscoutCHOP` 通道 `composition/layers/1/clips/1/connect` → Resolume 监听 7000 |
| 多投影仪同步 | 主控 TD `oscoutCHOP` 广播 → 工作机 `oscinCHOP` |
