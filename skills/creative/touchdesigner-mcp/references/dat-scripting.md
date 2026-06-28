# 基于 DAT 的脚本参考

TD 的事件/回调模型 —— 在响应网络事件时运行的 Python。完整的一组“Execute DAT”及其惯用模式。

关于任意 Python 执行（非回调驱动），见 `python-api.md`。关于 MCP 的 `td_execute_python` 工具，见 `mcp-tools.md`。

---

## Execute DAT 家族

每种类型监视一类事件源，并在变化时触发 Python。

| DAT | 监视 | 用于 |
|---|---|---|
| `chopExecuteDAT` | 某个 CHOP 的通道值 | 音频触发、阈值回调、基于数值输入的状态机 |
| `datExecuteDAT` | 某个 DAT 的内容（表格单元格、文本） | 响应来自 API 的数据更新、解析 webDAT 响应 |
| `parameterExecuteDAT` | 某个参数的值或脉冲 | 响应用户改动的参数、自定义脉冲按钮 |
| `panelExecuteDAT` | 某个 panel COMP 的交互 | 按钮点击、滑块拖动、字段提交 |
| `opExecuteDAT` | 算子生命周期 | 新算子被创建、删除、改名 |
| `executeDAT` | 工程生命周期、帧事件 | 单次初始化、逐帧逻辑、保存/加载钩子 |

它们都有一个带预定义回调函数的停靠 DAT。你只需填写关心的函数体。

---

## chopExecuteDAT —— 数值触发

```python
ce = root.create(chopExecuteDAT, 'kick_handler')
ce.par.chop = '/project1/audio/out_kick'      # 源 CHOP
ce.par.offtoon = True                          # 通道从 0 升到非 0 时触发
ce.par.ontooff = False
ce.par.whileon = False
ce.par.valuechange = False
```

在停靠的回调 DAT 中：

```python
def offToOn(channel, sampleIndex, val, prev):
    """通道从 0 变为非 0。经典节拍触发。"""
    op('/project1/strobe').par.flash.pulse()
    op('/project1/scene').par.index = (op('/project1/scene').par.index + 1) % 8
    return

def onToOff(channel, sampleIndex, val, prev):
    """通道从非 0 变为 0。"""
    return

def whileOn(channel, sampleIndex, val, prev):
    """通道非 0 时每帧触发。谨慎使用。"""
    return

def valueChange(channel, sampleIndex, val, prev):
    """值发生变化的每帧都触发（连续）。开销大。"""
    return
```

`channel` 是一个 `Channel` 对象 —— 有 `.name`、`.owner`、`.vals[]`。用 `channel.name == 'chan1'` 做过滤。

**基于阈值的自定义触发：** 先把源 CHOP 接到一个 `triggerCHOP`，得到干净的 0/1 脉冲，再用 `offtoon` 监视。

---

## datExecuteDAT —— 表格/文本变化

```python
de = root.create(datExecuteDAT, 'api_response')
de.par.dat = '/project1/api/web1'              # 源 DAT
de.par.tablechange = True                      # 任意单元格变化
de.par.cellchange = False
de.par.rowchange = False
de.par.colchange = False
```

```python
def onTableChange(dat):
    """整个表格变化（包括文本 DAT 内容更新）。"""
    if dat.numRows == 0:
        return
    # 如果是 webDAT 响应，解析 JSON
    import json
    try:
        data = json.loads(dat.text)
    except json.JSONDecodeError:
        debug(f'Bad JSON: {dat.text[:100]}')
        return
    # 写入一个 CHOP
    op('/project1/api_value').par.value0 = float(data.get('count', 0))
    return

def onCellChange(dat, cells, prev):
    """特定单元格变化。"""
    for cell in cells:
        # cell.row、cell.col、cell.val
        pass
    return
```

`debug()` 会打印到 textport —— 可通过 `td_read_textport` 读取。

---

## parameterExecuteDAT —— 参数变化与脉冲

```python
pe = root.create(parameterExecuteDAT, 'comp_params')
pe.par.op = '/project1/my_component'           # 要监视参数的 COMP
pe.par.parameters = '*'                         # 或具体名称如 'Intensity Reset'
pe.par.valuechange = True
pe.par.pulse = True
```

```python
def onValueChange(par, prev):
    """par 是一个 Par 对象。par.name、par.eval()、par.owner。"""
    if par.name == 'Intensity':
        op('/project1/bloom').par.threshold = par.eval()
    return

def onPulse(par):
    """脉冲参数被触发。"""
    if par.name == 'Reset':
        op('/project1/scene').par.index = 0
        op('/project1/audio_player').par.cuepoint = 0
        op('/project1/audio_player').par.cuepulse.pulse()
    return

def onExpressionChange(par, val, prev):
    """用户改动了某参数的表达式。"""
    return

def onExportChange(par, val, prev):
    """export 源发生变化。"""
    return

def onModeChange(par, val, prev):
    """参数模式变化（CONSTANT / EXPRESSION / EXPORT 等）。"""
    return
```

---

## panelExecuteDAT —— UI 事件

用于交互式控制面板。完整的 panel COMP 上下文见 `panel-ui.md`。

```python
pe = root.create(panelExecuteDAT, 'btn_handler')
pe.par.panel = '/project1/play_btn'
pe.par.click = True              # 鼠标点击事件
pe.par.value = True              # 状态变化（切换）
pe.par.lockedchange = False
```

```python
def onOffToOn(panelValue):
    """panel 值升到 1（按钮按下、滑块越过阈值）。"""
    op('/project1/scene_timer').par.start.pulse()
    return

def onOnToOff(panelValue):
    """panel 值降到 0。"""
    return

def onValueChange(panelValue):
    """连续：值变化的每帧都触发。"""
    val = panelValue.eval()
    op('/project1/master').par.opacity = val
    return

def onClick(panelValue):
    """离散点击事件，每次点击触发一次。"""
    return
```

`panelValue` 是 panel COMP 上的一个 `Par` 对象。

---

## opExecuteDAT —— 算子生命周期

监视父 COMP 内算子的创建/删除/改名。

```python
oe = root.create(opExecuteDAT, 'lifecycle')
oe.par.op = '/project1'
oe.par.create = True
oe.par.destroy = True
oe.par.namechange = True
oe.par.flagchange = False
```

```python
def onCreate(opCreated):
    """创建了新算子。便于自动应用约定。"""
    if opCreated.OPType == 'glslTOP':
        # 总是用一个 null 包一层
        n = opCreated.parent().create(nullTOP, opCreated.name + '_out')
        n.inputConnectors[0].connect(opCreated)
    return

def onDestroy(opDestroyed):
    """算子被删除。opDestroyed.path 在一帧内仍然有效。"""
    return

def onNameChange(opChanged):
    """算子被改名。"""
    return
```

适合开发期的脚手架（自动创建下游 nullTOP、自动命名约定）。生产项目中应禁用，以避免意外副作用。

---

## executeDAT —— 工程生命周期与逐帧

万能兜底。提供工程启动、保存、加载、帧起始、帧结束的钩子。

```python
exec_dat = root.create(executeDAT, 'lifecycle')
exec_dat.par.start = True
exec_dat.par.create = True
exec_dat.par.framestart = True
exec_dat.par.frameend = False
```

```python
def onStart():
    """工程刚开始 cook。运行一次。"""
    op('/project1/scene').par.index = 0
    debug('Project started')
    return

def onCreate():
    """组件刚被创建（仅对组件 executeDAT 触发，工程根不会）。"""
    return

def onFrameStart(frame):
    """每帧、网络 cook 之前。此处放重逻辑会造成瓶颈。"""
    return

def onFrameEnd(frame):
    """每帧、网络 cook 之后。用于捕获、录制、网络后处理。"""
    return

def onPlayStateChange(playing):
    """工程播放/暂停切换。"""
    return

def onProjectPreSave():
    """保存 .toe 文件之前。"""
    return

def onProjectPostSave():
    return
```

在 `onFrameStart` 里放繁重的逐帧逻辑是 TD 项目中最常见的性能退化之一。逐帧计算请用 CHOP，脚本用于处理事件。

---

## 模式：节拍触发动画序列

```python
# 源：一个底鼓触发 CHOP
# 目标：每次底鼓运行一个 1.5 秒的缩放脉冲 + 颜色闪烁

# 初始化（创建一次）
animator = root.create(timerCHOP, 'pulse_anim')
animator.par.length = 1.5
animator.par.cycle = False

# 视觉目标上的参数表达式：
op('logo').par.sx.expr = "1.0 + (1 - op('pulse_anim')['timer_fraction']) * 0.3"
op('logo').par.sx.mode = ParMode.EXPRESSION
op('logo').par.sy.expr = "1.0 + (1 - op('pulse_anim')['timer_fraction']) * 0.3"
op('logo').par.sy.mode = ParMode.EXPRESSION

# 在监视底鼓 CHOP 的 chopExecuteDAT 中：
def offToOn(channel, sampleIndex, val, prev):
    op('pulse_anim').par.start.pulse()
    return
```

---

## 模式：用 API 数据实时编辑 CHOP

```python
# webDAT 每 5 秒轮询一次 API
# datExecuteDAT 解析响应并写入 constantCHOP

def onTableChange(dat):
    import json
    try:
        data = json.loads(dat.text)
    except:
        return
    target = op('/project1/external_state')
    target.par.name0 = 'temperature'
    target.par.value0 = float(data['temp_c'])
    target.par.name1 = 'humidity'
    target.par.value1 = float(data['humidity'])
    return
```

视觉只需引用 `op('external_state')['temperature']` —— 即可实时更新。

---

## 模式：自清理网络

```python
# 一个 opExecuteDAT 监视孤立的辅助算子，在其父算子消失后删除它们

def onDestroy(opDestroyed):
    parent_name = opDestroyed.name
    helper = op(f'/project1/{parent_name}_helper')
    if helper:
        helper.destroy()
    return
```

---

## 陷阱

1. **回调静默崩溃** —— 异常会打印到 textport 但不会显示在 UI 中。调试前务必先 `td_clear_textport`，之后用 `td_read_textport` 读取。
2. **`debug()` 与 `print()`** —— 两者都写入 textport，但 `debug()` 会附带调用 DAT 的文件/行号。脚本中优先用 `debug()`。
3. **`val` 是新值，`prev` 是旧值** —— 容易写反。始终是：`def offToOn(channel, sampleIndex, val, prev)`。混淆时查阅 TD 文档确认参数顺序。
4. **`whileOn` 与 `valueChange` 是逐帧的** —— 开销大。除非确有必要否则避免。改用表达式驱动。
5. **回调在 cook 暂停状态不运行** —— 如果父 COMP 设置了 `allowCooking=False`，回调会冻结。可当作“禁用我”的开关使用。
6. **`par` 与 `panelValue`** —— parameterExecuteDAT 给的是 `par`（一个 Par 对象），panelExecuteDAT 给的是 `panelValue`（也是 Par 类对象）。两者都有 `.name` 和 `.eval()`，但上下文不同。
7. **`opExecuteDAT` 会对自己触发** —— 当你创建一个 opExecuteDAT 时，若 `par.create=True` 且父级匹配，它可能对自己触发 `onCreate`。用 `if opCreated == me: return` 过滤。
8. **重载行为** —— 重新加载扩展（`td_reinit_extension`）时，所有回调 DAT 会重置内部状态。模块级变量会丢失。请把状态存在 tableDAT 或停靠 DAT 中，而不是模块全局变量里。
9. **cook 依赖** —— 如果回调写入的算子位于回调源的上游，会形成 cook 循环。TD 会告警但未必拦截。保持数据流单向。
10. **active 标志** —— 每个 Execute DAT 都有 `par.active`。为 False 时静默。便于在不删接线的情况下切换以进行测试。

---

## 快速配方

| 目标 | 设置 |
|---|---|
| 节拍触发 | `chopExecuteDAT.par.offtoon=True` 监视一个 `triggerCHOP` |
| API 响应处理 | `datExecuteDAT.par.tablechange=True` 监视一个 `webDAT` |
| 自定义按钮 → 动作 | `parameterExecuteDAT.par.pulse=True` 监视一个自定义脉冲参数 |
| 滑块 → 连续参数 | `panelExecuteDAT.par.value=True` 监视一个 `sliderCOMP` |
| 单次初始化 | `executeDAT.par.start=True`，逻辑写在 `onStart()` 中 |
| 逐帧指标 | `executeDAT.par.frameend=True`，把值记录到 CHOP |
| 自动为新算子命名 | `opExecuteDAT.par.create=True`，强制命名约定 |
