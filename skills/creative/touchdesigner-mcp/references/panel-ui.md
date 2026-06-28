# 面板与 UI 参考

TouchDesigner 内部的交互式控制面板 —— 按钮、滑块、字段、自定义参数页面、面板回调。关于 HUD 叠加层（在视觉上渲染文字）见 `layout-compositor.md`。

用例：
- VJ 控制机架（主推子、场景按钮、FX 开关）
- 装置的操作员控制台
- 带自身参数 UI 的自包含 TOX 组件
- 显示在平板上的手机风格触摸界面

---

## UI 的两层

| 层 | 它是什么 | 用于 |
|---|---|---|
| **自定义参数** | 任意 COMP 上的参数，像内置 TD 参数一样编辑 | 可配置组件、预设、“设置”面板 |
| **Panel COMPs** | containerCOMP 内的可见控件（按钮、滑块、字段） | 交互式控制面板、实时 UI |

两者可结合：构建一个 containerCOMP，其中的面板控件读写父组件上的自定义参数。

---

## 自定义参数

给任意 COMP 添加用户可编辑的参数。参数随 COMP 持久化、驱动表达式，保存/重载后依然存在。

```python
# 给 baseCOMP 添加自定义页
comp = op('/project1/my_component')
page = comp.appendCustomPage('Controls')

# 添加带类型的参数
page.appendFloat('Intensity', label='Intensity')[0]   # 返回一个 Par
page.appendInt('Count', label='Count')[0]
page.appendToggle('Enabled', label='Enabled')[0]
page.appendMenu('Mode', menuNames=['off', 'soft', 'hard'], menuLabels=['Off', 'Soft', 'Hard'])[0]
page.appendStr('Title', label='Title')[0]
page.appendRGB('Color', label='Color')                # 返回 3 个 Par
page.appendXY('Offset', label='Offset')               # 返回 2 个 Par
page.appendPulse('Reset', label='Reset')[0]
page.appendFile('TextureFile', label='Texture')[0]
```

**从任意位置读写：**

```python
val = op('/project1/my_component').par.Intensity.eval()
op('/project1/my_component').par.Intensity = 0.7
```

**通过表达式驱动其他参数：**

```python
op('bloom1').par.threshold.mode = ParMode.EXPRESSION
op('bloom1').par.threshold.expr = "op('/project1/my_component').par.Intensity"
```

**脉冲处理（Reset 按钮）：**

用 `parameterExecuteDAT` 监视该 COMP 的脉冲参数。见 `dat-scripting.md`。

---

## Panel COMPs —— 控件

每个都是一个 COMP，在 `containerCOMP` 内渲染为可点击/可拖拽的控件。

| 类型 | 类型名 | 用途 |
|---|---|---|
| Button | `buttonCOMP` | 点击动作 —— 瞬时或切换 |
| Slider | `sliderCOMP` | 拖拽设置 0-1 值（一维或二维） |
| Field | `fieldCOMP` | 文本输入 |
| Container | `containerCOMP` | 布局 + 视觉样式，承载子级 |
| Select | `selectCOMP` | 引用并显示来自另一个 COMP 的内容 |
| List | `listCOMP` | 带行回调的可滚动列表 |

### Button

```python
btn = root.create(buttonCOMP, 'play_btn')
btn.par.w = 120; btn.par.h = 40
btn.par.buttontype = 'momentary'    # 'momentary' | 'toggleup' | 'togglepress' | 'radio'
btn.par.bgcolorr = 0.1; btn.par.bgcolorg = 0.1; btn.par.bgcolorb = 0.1
btn.par.text = 'Play'

# 读取状态
state = btn.panel.state          # 激活时为 1
```

### Slider

```python
sld = root.create(sliderCOMP, 'master_fader')
sld.par.w = 60; sld.par.h = 300
sld.par.style = 'vertical'        # 'vertical' | 'horizontal' | 'xy'
sld.par.value0min = 0.0
sld.par.value0max = 1.0

# 通过表达式驱动参数（始终生效，无需回调）
op('/project1/master_level').par.opacity.mode = ParMode.EXPRESSION
op('/project1/master_level').par.opacity.expr = "op('master_fader').panel.u"
```

`panel.u` 和 `panel.v` 给出 0-1 的归一化值。对二维滑块两者都被填充。

### Field（文本输入）

```python
fld = root.create(fieldCOMP, 'scene_name')
fld.par.w = 200; fld.par.h = 30
fld.par.fieldtype = 'string'      # 'string' | 'integer' | 'float'

# 读取当前文本
text = fld.panel.field            # 文本内容
```

### List

对于带可选行的可滚动列表，用停靠的 `list1_callbacks` DAT 处理行交互。通过 `list_definition` 表格 DAT 设置单元格。

---

## Container COMP —— 布局与样式

`containerCOMP` 是分组控件和编排布局的主要父级。

```python
panel = root.create(containerCOMP, 'control_panel')
panel.par.w = 400; panel.par.h = 600
panel.par.bgcolorr = 0.05
panel.par.bgcolorg = 0.05
panel.par.bgcolorb = 0.05
panel.par.bgalpha = 1.0

# 垂直堆叠布局子面板
panel.par.align = 'lefttoright'   # 'lefttoright' | 'toptobottom' | 等
```

子级根据 `par.align` 自动定位。要绝对定位，用 `par.align = 'fillresize'` 并设置每个子级的 `par.x` / `par.y`。

### 布局策略

| `par.align` | 行为 |
|---|---|
| `lefttoright` | 子级水平堆叠 |
| `toptobottom` | 子级垂直堆叠 |
| `righttoleft` / `bottomtotop` | 反向堆叠 |
| `fillresize` | 子级填充尺寸、手工定位 |
| `top` / `bottom` / `left` / `right` | 固定定位 |

复杂网格：嵌套容器 —— 一个垂直容器内含多个水平容器。

---

## 面板回调 —— 响应事件

`panelExecuteDAT` 监视面板并在用户交互时触发 Python 回调。

```python
pe = root.create(panelExecuteDAT, 'btn_handler')
pe.par.panel = '/project1/play_btn'
pe.par.click = True              # 响应点击
pe.par.value = True              # 响应值变化
```

在其停靠 DAT 中：

```python
def onOffToOn(panelValue):
    # 按下点击
    op('/project1/scene_timer').par.start.pulse()
    return

def onOnToOff(panelValue):
    # 释放点击
    return

def onValueChange(panelValue):
    # 滑块拖动、字段改动等
    new_val = panelValue.eval()
    op('/project1/master').par.opacity = new_val
    return
```

对于自定义参数页上的脉冲参数，改用 `parameterExecuteDAT`。

---

## 构建一个完整的 VJ 控制面板

端到端模式：

```python
# 1. 顶层容器
panel = root.create(containerCOMP, 'vj_control')
panel.par.w = 800; panel.par.h = 200
panel.par.align = 'lefttoright'

# 2. 主推子列
master_col = panel.create(containerCOMP, 'master')
master_col.par.w = 120; master_col.par.h = 200
master_col.par.align = 'toptobottom'

master_label = master_col.create(textTOP, 'lbl')
master_label.par.text = 'MASTER'

master_sld = master_col.create(sliderCOMP, 'fader')
master_sld.par.w = 60; master_sld.par.h = 150
master_sld.par.style = 'vertical'

# 3. 场景按钮行
scene_col = panel.create(containerCOMP, 'scenes')
scene_col.par.w = 400; scene_col.par.h = 200
scene_col.par.align = 'lefttoright'
for i in range(8):
    b = scene_col.create(buttonCOMP, f'scene_{i+1}')
    b.par.w = 50; b.par.h = 50
    b.par.text = str(i+1)
    b.par.buttontype = 'radio'      # 同时只能有一个激活

# 4. FX 切换列
fx_col = panel.create(containerCOMP, 'fx')
fx_col.par.w = 280; fx_col.par.h = 200
fx_col.par.align = 'toptobottom'
for fx in ['Bloom', 'CRT', 'Glitch', 'Strobe']:
    t = fx_col.create(buttonCOMP, fx.lower())
    t.par.w = 220; t.par.h = 35
    t.par.text = fx
    t.par.buttontype = 'toggleup'

# 5. 显示在窗口中
win = root.create(windowCOMP, 'control_win')
win.par.winop = panel.path
win.par.winw = 800; win.par.winh = 200
win.par.borders = True
win.par.winopen.pulse()
```

然后通过表达式或 panelExecuteDAT 把面板值接到算子上。

---

## 显示面板 —— 窗口或内嵌

| 方式 | 何时使用 |
|---|---|
| 指向面板的 `windowCOMP` | 独立控制面板、独立显示器 |
| 通过 `renderTOP` 渲染 containerCOMP | 在视觉上合成 UI（HUD 风格） |
| 直接在网络编辑器面板里用 `panelCOMP` | 仅设计者/开发者预览 —— 面板完全可交互 |

对触摸屏平板，在第二显示器上用 `windowCOMP`，并将其路由到平板的 HDMI 输入。

---

## 陷阱

1. **面板不响应点击** —— 可能是 `par.disabled = True` 或父容器有 `par.disableinputs = True`。检查面板层级。
2. **滑块值不更新** —— `panel.u/v` 读取的是视觉位置。若直接设置 `par.value0`，视觉会滞后。把 `par.value0` 作为真值来源，让滑块跟随它。
3. **自定义参数不显示** —— 必须先 `appendCustomPage`，再追加参数。没有参数的页面不会显示。
4. **自定义参数在重载后消失** —— 运行时通过 Python 添加的参数只有在 COMP 之后被保存才会保留。用 `tox` 保存（`comp.save('mycomp.tox')`）或通过 `td_execute_python` 提交后保存工程。
5. **事件回调触发两次** —— 单次按钮按下可能同时触发 `onOffToOn` 和 `onValueChange`。只选一个处理动作，避免双重触发。
6. **脉冲参数需要 `.pulse()`** —— 对脉冲参数设置 `par.X = True` 没有任何效果。始终用 `.pulse()`。
7. **字段文本在 Tab/Enter 前不提交** —— 字段在输入过程中不触发回调。用 `par.committemode = 'all'` 可在每次按键时触发（开销大）。
8. **`par.text` 与面板内容** —— `buttonCOMP.par.text` 是按钮上的标签。按钮的状态是 `panel.state`（0/1）。不要混淆。
9. **macOS 触摸输入** —— 通过直接触摸面板的多点触摸可用，但 TD 的手势处理较初级。对复杂多点触摸（捏合/旋转），改用平板上的 TouchOSC。
10. **布局不更新** —— 改 `par.align` 需要容器重新 cook。触动一个子级或脉冲化容器以触发。

---

## 快速配方

| 目标 | 设置 |
|---|---|
| 主推子 | `sliderCOMP`（垂直）→ 在 `level.par.opacity` 上加表达式 |
| 场景选择器 | 8 个 `buttonCOMP`（radio）→ 对其状态做 `selectCHOP` → 驱动 `switchTOP.par.index` |
| FX 开关 | `buttonCOMP`（toggleup）→ 在某个 FX 算子的 `bypass` 上加表达式 |
| 数值输入 | `fieldCOMP`（float）→ 在目标参数上加表达式 |
| 组件设置 | 组件 COMP 上的自定义参数，内部的面板控件驱动它们 |
| 触摸平板 UI | 带控件的 `containerCOMP` → `windowCOMP` 到第二显示器 |
| 状态显示 | 渲染到面板中的 `textTOP`，通过 `selectCOMP` |
