# TouchDesigner MCP —— 坑与经验教训

来自真实 TD 会话的宝贵知识。在构建任何东西之前阅读本文。

## 参数名

### 1. 绝不硬编码参数名 —— 总是先发现

参数名在不同 TD 版本之间会变化。在某个构建中可用的在另一个中可能不可用。始终使用 td_get_par_info 从 TD 发现实际名称。

agent 的 LLM 训练数据包含错误的参数名。不要相信它们。

已知的历史差异（可能进一步变化 —— 总是验证）：
| 文档/训练数据说的 | 某些版本中的实际值 | 说明 |
|---------------|---------------|-------|
| `dat` | `pixeldat` | GLSL TOP 像素着色器 DAT |
| `colora` | `alpha` | Constant TOP alpha |
| `sizex` / `sizey` | `size` | Blur TOP（单个值） |
| `fontr/g/b/a` | `fontcolorr/g/b/a` | Text TOP 字体颜色（r/g/b） |
| `fontcolora` | `fontalpha` | Text TOP 字体 alpha（不是 `fontcolora`） |
| `bgcolora` | `bgalpha` | Text TOP 背景 alpha |
| `value1name` | `vec0name` | GLSL TOP uniform 名 |

### 2. twozero td_execute_python 响应格式

通过 twozero MCP 调用 `td_execute_python` 时，成功响应返回 `(ok)` 后跟 FPS/错误摘要（例如 `[fps 60.0/60] [0 err/0 warn]`），而不是原始 Python `result` 字典。如果你以编程方式解析响应，检查 `(ok)` 前缀 —— 不要对脚本中的 Python 变量名做模式匹配。使用 `td_get_operator_info` 或单独的检查调用来回读值。

### 3. 使用 td_set_operator_pars 时，参数名必须完全匹配

使用 td_get_par_info 来发现它们。MCP 工具会验证参数名并返回清晰的错误解释哪里出了问题，不像原始 Python 会因 tdAttributeError 崩溃整个脚本并停止执行。设置前总是先发现。

### 4. 使用 `safe_par()` 模式以实现跨版本兼容

```python
def safe_par(node, name, value):
    p = getattr(node.par, name, None)
    if p is not None:
        p.val = value
        return True
    return False
```

### 5. `td.tdAttributeError` 会崩溃整个脚本 —— 使用防御性访问

如果你执行 `node.par.nonexistent = value`，TD 会抛出 `tdAttributeError` 并停止整个脚本。预防胜于捕获：
- 使用 `op()` 而非 `opex()` —— `op()` 失败时返回 None，`opex()` 会抛出异常
- 访问任何参数前使用 `hasattr(node.par, 'name')`
- 使用带默认值的 `getattr(node.par, 'name', None)`
- 使用坑 #3 中的 `safe_par()` 模式

```python
# 错误 —— 如果参数不存在会崩溃：
node.par.nonexistent = value

# 正确 —— 防御性访问：
if hasattr(node.par, 'nonexistent'):
    node.par.nonexistent = value
```

### 6. `outputresolution` 是字符串菜单，不是整数

```
menuNames: ['useinput','eighth','quarter','half','2x','4x','8x','fit','limit','custom','parpanel']
```
始终使用字符串形式。设置 `outputresolution = 9` 可能会静默失败。
```python
node.par.outputresolution = 'custom'  # 正确
node.par.resolutionw = 1280; node.par.resolutionh = 720
```
发现有效值：`list(node.par.outputresolution.menuNames)`

## GLSL 着色器

### 7. GLSL TOP 中不存在 `uTDCurrentTime`

GLSL TOP 没有内置的时间 uniform。GLSL MAT 有 `uTDGeneral.seconds` 但在 GLSL TOP 上下文中不可用。

**首选 —— GLSL TOP Vectors/Values 页：**
```python
gl.par.value0name = 'uTime'
gl.par.value0.expr = "absTime.seconds"
# 在 GLSL 中：uniform float uTime;
```

**备选 —— Constant TOP 纹理（用于复杂时间数据）：**

关键：将 format 设置为 `rgba32float` —— 默认 8 位会被钳制到 0-1：
```python
t = root.create(constantTOP, 'time_driver')
t.par.format = 'rgba32float'
t.par.outputresolution = 'custom'
t.par.resolutionw = 1; t.par.resolutionh = 1
t.par.colorr.expr = "absTime.seconds % 1000.0"
t.outputConnectors[0].connect(glsl.inputConnectors[0])
```

### 8. GLSL 编译错误在 API 中是静默的

GLSL TOP 在 UI 中显示黄色警告三角形，但 `node.errors()` 可能返回空字符串。也检查 `node.warnings()`，并创建一个指向 GLSL TOP 的 Info DAT 来读取实际的编译器输出。

### 9. TD GLSL 使用 `vUV.st` 而非 `gl_FragCoord` —— 并且在 macOS 上需要 `TDOutputSwizzle()`

标准 GLSL 模式不起作用。TD 提供：
- `vUV.st` —— UV 坐标（0-1）
- `uTDOutputInfo.res.zw` —— 分辨率
- `sTD2DInputs[0]` —— 输入纹理
- `layout(location = 0) out vec4 fragColor` —— 输出

在 macOS 上关键：始终用 `TDOutputSwizzle()` 包裹输出：
```glsl
fragColor = TDOutputSwizzle(color);
```
TD 使用 GLSL 4.60（Vulkan 后端）。GLSL 3.30 及更早版本已移除。

### 10. 大型 GLSL 着色器 —— 写入临时文件

带特殊字符的 GLSL 代码可能损坏 JSON 载荷。将着色器写入临时文件并在 TD 中加载：
```python
# Agent 端：通过 write_file 将着色器写入 /tmp/shader.glsl
# TD 端：
sd = root.create(textDAT, 'shader_code')
with open('/tmp/shader.glsl', 'r') as f:
    sd.text = f.read()
```

## 节点管理

### 11. 迭代 `root.children` 时销毁节点会导致 `tdError`

子节点被销毁时迭代器失效。总是先做快照：
```python
kids = list(root.children)  # 快照
for child in kids:
    if child.valid:  # 检查 —— 早期销毁可能级联
        child.destroy()
```

### 11b. 把清理和创建拆分为独立的 td_execute_python 调用

在同一个脚本中用刚销毁的名字创建节点会导致 "Invalid OP object" 错误 —— 即使有 `list()` 快照。TD 的内部引用在一个执行上下文内可能变陈旧。

**错误（单次调用）：**
```python
# td_execute_python：
for c in list(root.children):
    if c.valid and c.name.startswith('my_'):
        c.destroy()
# ... 然后在同一脚本中创建 my_audio、my_shader 等 → 崩溃
```

**正确（两次独立调用）：**
```python
# 调用 1：td_execute_python —— 仅清理
for c in list(root.children):
    if c.valid and c.name.startswith('my_'):
        c.destroy()

# 调用 2：td_execute_python —— 构建（单独的 MCP 调用）
audio = root.create(audiofileinCHOP, 'my_audio')
# ... 其余构建
```

### 12. Feedback TOP：使用 `top` 参数，而非直接输入连线

feedbackTOP 的 `top` 参数引用要延迟哪个 TOP。不要同时把那个 TOP 直接连到 feedback 的输入 —— 这会创建真正的 cook 依赖循环。

正确设置：
```python
fb = root.create(feedbackTOP, 'fb_delay')
fb.par.top = comp.path          # 仅引用 —— 不连线到 fb 输入
fb.outputConnectors[0].connect(xf)  # fb 输出 -> transform -> fade -> comp
```

transform/fade 链上的 "Cook dependency loop detected" 警告是预期的。

### 13. GLSL TOP 自动创建伴随节点

创建 `glslTOP` 也会创建 `name_pixel`（Text DAT）、`name_info`（Info DAT）和 `name_compute`（Text DAT）。这些在网络中可见。不要被"多余"的节点惊到。

### 14. 默认项目根是 `/project1`

新 TD 文件以 `/project1` 作为主容器开始。系统节点位于 `/`、`/ui`、`/sys`、`/local`、`/perform`。不要在 `/project1` 之外创建用户节点。

### 15. 非商业授权将分辨率限制在 1280x1280

设置 `resolutionw=1920` 会静默钳制到 1280。创建后总是检查有效分辨率：
```python
n.cook(force=True)
actual = str(n.width) + 'x' + str(n.height)
```

## 录制与编解码器

### 16. MovieFileOut TOP：H.264/H.265/AV1 需要商业授权

在非商业版 TD 中，这些编解码器会产生错误。推荐的替代方案：
- `prores` —— Apple ProRes，**macOS 上最佳**，硬件加速，不受授权限制。1280x720 时约 55MB/s 但质量无损。**在 macOS 上将此作为默认。**
- `cineform` —— GoPro Cineform，支持 alpha
- `hap` —— GPU 加速回放，文件较大
- `notchlc` —— GPU 加速，质量好
- `mjpa` —— Motion JPEG，旧式备选（有损，仅在 ProRes 不可用时使用）

对于图像序列：`rec.par.type = 'imagesequence'`，`rec.par.imagefiletype = 'png'`

### 17. MovieFileOut 的 `.record()` 方法可能不存在

改用切换参数：
```python
rec.par.record = True   # 开始录制
rec.par.record = False  # 停止录制
```

在同一脚本中设置文件路径并开始录制时，使用 delayFrames：
```python
rec.par.file = '/tmp/new_output.mov'
run("op('/project1/recorder').par.record = True", delayFrames=2)
```

### 18. TOP.save() 快速调用时捕获相同帧

实时录制请使用 MovieFileOut。设置 `project.realTime = False` 以获得帧精确输出。

### 19. AudioFileIn CHOP：提示和录制顺序很重要

录制序列必须按确切顺序完成，否则录制会是空的、音频会从文件中间开始，或文件不会被写入。

**经过验证的录制序列：**

```python
# 第 1 步：停止任何现有录制
rec.par.record = False

# 第 2 步：将音频重置到开头
audio.par.play = False
audio.par.cue = True
audio.par.cuepoint = 0      # 可能还需要 cuepointunit=0
# 验证：audio.par.cue.eval() 应为 True

# 第 3 步：设置输出文件路径
rec.par.file = '/tmp/output.mov'

# 第 4 步：释放 cue + 开始播放 + 开始录制（带帧延迟）
audio.par.cue = False
audio.par.play = True
audio.par.playmode = 2      # 顺序 —— 播放一次
run("op('/project1/recorder').par.record = True", delayFrames=3)
```

**每一步为何重要：**
- 先 `rec.par.record = False` —— 如果之前的录制还在活动，设置 `par.file` 可能静默失败
- `audio.par.cue = True` + `cuepoint = 0` —— 保证音频从头开始，否则频谱可能在最初几秒是静音的
- 录制开始上的 `delayFrames=3` —— 在同一脚本中设置 `par.file` 和 `par.record = True` 可能竞争；文件路径需要一帧来注册后才能开始录制
- `playmode = 2`（顺序）—— 播放文件一次。如果你想 TD 时间线控制位置，使用 `playmode = 0`（锁定到时间线）

## TD Python API 模式

### 20. COMP 扩展设置：ext0object 格式至关重要

`ext0object` 期望一个 CONSTANT 字符串（不是表达式模式）：
```python
comp.par.ext0object = "op('./myExtensionDat').module.MyClassName(me)"
```
绝不要只设置为 DAT 名。绝不要使用 ParMode.EXPRESSION。始终确保 DAT 的 `par.language='python'`。

### 21. td.Panel 不可下标访问 —— 使用属性访问

```python
comp.panel.select      # 正确（属性访问，返回 float）
comp.panel['select']   # 错误 —— 'td.Panel' 对象不可下标访问
```

### 22. 始终在脚本回调中使用相对路径

在 scriptTOP/CHOP/SOP/DAT 回调中，使用相对于 `scriptOp` 或 `me` 的路径：
```python
root = scriptOp.parent().parent()
dat = root.op('pixel_data')
```
绝不要硬编码像 `op('/project1/myComp/child')` 这样的绝对路径 —— 当容器被重命名或复制时会失效。

### 23. keyboardinCHOP 通道名带 'k' 前缀

通道名是 `kup`、`kdown`、`kleft`、`kright`、`ka`、`kb` 等 —— 不是 `up`、`down`、`a`、`b`。总是用以下方式验证：
```python
channels = [c.name for c in op('/project1/keyboard1').chans()]
```

### 24. expressCHOP 仅 cook 上下文属性 —— 误报错误

`me.inputVal`、`me.chanIndex`、`me.sampleIndex` 仅在 cook 上下文中工作。从外部调用 `par.expr0expr.eval()` 总是抛出错误 —— 这不是真正的算子错误。在错误扫描中忽略这些。

### 25. td.Vertex 属性 —— 使用索引访问而非命名属性

在 TD 2025.32 中，`td.Vertex` 对象没有 `.x`、`.y`、`.z` 属性：
```python
# 错误 —— 崩溃：
vertex.x, vertex.y, vertex.z

# 正确 —— 基于索引：
vertex.point.P[0], vertex.point.P[1], vertex.point.P[2]
# 或对于 SOP 点位置：
pt = sop.points()[i]
pos = pt.P    # 使用 P[0], P[1], P[2]
```

## 音频

### 26. Audio Spectrum CHOP 输出较弱 —— 提升它

原始输出非常小（0.001-0.05）。使用内置提升：`spectrum.par.highfrequencyboost = 3.0`

如果仍然较弱，在 Range 模式下添加 Math CHOP：`fromrangehi=0.05, torangehi=1.0`

### 27. AudioSpectrum CHOP：timeslice 和采样数是头号陷阱

44100Hz 下 `timeslice=False` 的 AudioSpectrum 输出整个音频文件作为采样（~24000+）。CHOP-to-TOP 然后超过纹理分辨率上限并警告/失败。

**修复：**保持 `timeslice = True`（默认）以实现逐帧实时 FFT。设置 `fftsize` 控制 bin 数（它是 STRING 枚举：`'256'` 不是 `256`）。

如果 CHOP-to-TOP 仍然采样过多，在 choptoTOP 上设置 `layout = 'rowscropped'`。

```python
spectrum.par.fftsize = '256'      # STRING，不是 int —— 枚举值
spectrum.par.timeslice = True     # 实时音频响应必须为 True
spectex.par.layout = 'rowscropped'  # 处理过大的 CHOP 输入
```

**resampleCHOP 没有 `numsamples` 参数。**它使用 `rate`、`start`、`end`、`method`。不要猜 —— 总是先 `td_get_par_info('resampleCHOP')`。

### 28. CHOP To TOP 没有输入连接器 —— 使用 par.chop 引用

```python
spec_tex = root.create(choptoTOP, 'spectrum_tex')
spec_tex.par.chop = resample  # 正确：参数引用
# 不是：resample.outputConnectors[0].connect(spec_tex.inputConnectors[0])  # 错误
```

## 工作流

### 29. 构建后总是验证 —— 错误是静默的

节点错误和断开的连接不产生输出。总是检查：
```python
for c in list(root.children):
    e = c.errors()
    w = c.warnings()
    if e: print(c.name, 'ERR:', e)
    if w: print(c.name, 'WARN:', w)
```

### 30. Window COMP 显示目标参数是 `winop`

```python
win = root.create(windowCOMP, 'display')
win.par.winop = '/project1/logo_out'
win.par.winw = 1280; win.par.winh = 720
win.par.winopen.pulse()
```

### 31. `sample()` 在快速调用时返回冻结像素

`out.sample(x, y)` 从单次 cook 快照返回像素。以 2 秒以上延迟比较采样，或对显示窗口使用 screencapture。

### 32. 音频响应式 GLSL：TD 端管线

对于音频同步视觉：AudioFileIn → AudioSpectrum(timeslice=True, fftsize='256') → Math(gain=5) → choptoTOP(par.chop=math, layout='rowscropped') → GLSL input。着色器在不同 x 位置采样 `sTD2DInputs[1]` 获取低音/中音/高音。用 MovieFileOut 录制 TD 输出。

**关键陷阱：**AudioFileIn 必须先提示（`par.cue=True` → `par.cuepulse.pulse()`）然后取消提示（`par.cue=False`，`par.play=True`）才能开始录制。否则频谱在最初几秒是静音的。

### 33. twozero MCP：优先使用原生工具

**始终优先使用原生 MCP 工具而非 td_execute_python：**
- `td_create_operator` 而非 `root.create()` 脚本（处理视口定位）
- `td_set_operator_pars` 而非 `node.par.X = Y` 脚本（验证参数名）
- `td_get_par_info` 而非临时节点发现流程（即时，无需清理）
- `td_get_errors` 而非手动 `c.errors()` 循环
- `td_get_focus` 用于上下文感知（旧方法中没有等价物）

仅在多步逻辑（连线链、条件构建、循环）时回退到 `td_execute_python`。

### 34. twozero td_execute_python 响应包装

twozero 用状态信息包装 `td_execute_python` 响应：`(ok)\n\n[fps 60.0/60] [0 err/0 warn]`。你的 Python `result` 变量值可能不会逐字出现在响应文本中。如果你需要以编程方式检查结果，在脚本中使用 `print()` 语句 —— 它们会出现在响应中。不要依赖对 `result` 字典的字符串匹配。

### 35. 音频响应链：不要用 Lag CHOP 或 Filter CHOP 做频谱平滑

Derivative 文档和教程建议使用 Lag CHOP（lag1=0.2, lag2=0.5）在传递给着色器之前平滑原始 FFT 输出。**这与 AudioSpectrum → CHOP to TOP → GLSL 不兼容。**

发生的事：Lag CHOP 在 timeslice 模式下工作。256 采样的频谱输入被扩展到 1600-2400 采样。Lag 平均把所有值驱动到接近零（~1e-06）。CHOP to TOP 产生 2400x2 纹理而非 256x2。着色器实际接收不到任何音频数据。

**正确的链是：Spectrum(outlength=256) → Math(gain=10) → CHOPtoTOP → GLSL。**完全不要 CHOP 平滑。如果需要平滑，在 GLSL 着色器中通过带反馈纹理的时间插值完成。

音频播放时验证的值：
- 不用 Lag CHOP：低音 bin = 5.0-5.4，中音 bin = 1.0-1.7（强，可用）
- 用 Lag CHOP：所有 bin = 0.000001-0.00004（死寂，零音频响应）

### 36. AudioSpectrum 输出长度：手动设置以避免 CHOP to TOP 溢出

可视化模式下 FFT 8192 的 AudioSpectrum 默认输出 22,050 个采样（每 Hz 一个，0–22050）。CHOP to TOP 无法处理 —— 你会得到 "Number of samples exceeded texture resolution max"。

修复：`spectrum.par.outputmenu = 'setmanually'` 和 `spectrum.par.outlength = 256`。这给出 256 个频率 bin —— 对可视化 FFT 足够。

不要设置 `timeslice = False` 作为变通 —— 那会一次性处理整个音频文件并产生更多采样。

### 37. 来自 CHOP to TOP 的 GLSL 频谱纹理是 256x2 不是 256x1

AudioSpectrum 输出 2 个通道（立体声：chan1、chan2）。带 `dataformat='r'` 的 CHOP to TOP 创建一个 256x2 纹理 —— 每个通道一行。在 `y=0.25`（第一行中心）采样第一个通道，不是 `y=0.5`（行之间的边界）：

```glsl
float bass = texture(sTD2DInputs[1], vec2(0.05, 0.25)).r;  // 正确
float bass = texture(sTD2DInputs[1], vec2(0.05, 0.5)).r;   // 错误 —— 在行之间采样
```

### 38. FPS=0 不意味着算子没在 cook —— 检查播放状态

TD 可以在 `td_get_perf` 中显示 `fps:0` 而算子仍在 cook 且 `TOP.save()` 仍产生有效截图。两个最常见的原因：

**a) 项目暂停（播放条停止）。**TD 的播放条可以用空格键切换。`/` 处的 `root` 没有 `.playbar` 属性（它在 perform COMP 上）。最简单的修复是通过 `td_input_execute` 发送空格键，尽管此工具有时会出错。作为变通，`TOP.save()` 无论播放状态如何总是有效 —— 在花时间调试 FPS 之前用它验证渲染实际在进行。

**b) 音频设备 CHOP 阻塞主线程（最常见）。**`active=True` 的 `audiodeviceoutCHOP` 可能消耗 300-400ms/s（帧预算的 2000%+），使 cook 循环停滞在 FPS=0。**`volume=0` 不够** —— 音频驱动仍然阻塞。修复：`par.active = False`。这完全停止 CHOP 与音频驱动交互。如果需要音频监听，仅在短播放检查期间启用，然后在录制前禁用。

2026 年 4 月验证：禁用 `audiodeviceoutCHOP`（`active=False`）立即将 FPS 从 0 恢复到 60，从 2348% 预算使用恢复到 0.1%。

FPS=0 时的诊断序列：
1. `td_get_perf` —— 检查是否有任何算子有极端的 cpu/s（audiodeviceoutCHOP 是通常嫌疑）
2. 如果 audiodeviceoutCHOP 显示 >100ms/s：立即设置 `par.active = False`
3. 对输出 `TOP.save()` —— 如果它产生有效图像，管线工作正常，只是不是实时速率
4. 检查其他阻塞 CHOP（audiodevin 等）
5. 切换播放状态（空格键，或检查 absTime.seconds 是否在推进）

### 39. FPS=0 时录制产生空或接近空的文件

这是"我录制了 30 秒但只得到 2 帧视频"的头号原因。如果 TD 的 cook 循环停滞（FPS=0 或非常低），MovieFileOut 没有东西可录制。与无论播放状态如何都捕获最后 cook 帧的 `TOP.save()` 不同，MovieFileOut 只写入实际 cook 的帧。

**开始录制前总是验证 FPS：**
```python
# 先通过 td_get_perf 检查
# 如果 FPS < 30，不要开始录制 —— 先修复性能问题
# 如果 FPS=0，播放条可能暂停 —— 见坑 #37
```

录制空视频的常见原因：
- 播放条暂停（FPS=0）—— 见坑 #37
- 音频设备 CHOP 阻塞主线程 —— 见坑 #37b
- 在音频提示之前开始录制 —— 音频静音，GLSL 输出黑色，MovieFileOut 录制看起来空的黑色帧
- `par.file` 与 `par.record = True` 在同一脚本中设置 —— 见坑 #18

### 40. GLSL 着色器产生黑色输出 —— 在提交长时间渲染之前测试

新的 GLSL 着色器可能静默失败（见坑 #7）。在录制长镜头之前，总是：

1. **先写一个最小测试着色器**，只输出纯色或直通：
```glsl
void main() {
    vec2 uv = vUV.st;
    fragColor = TDOutputSwizzle(vec4(uv, 0.0, 1.0));
}
```

2. **通过 `td_get_screenshot` 验证测试正确渲染** GLSL TOP 的输出。

3. **换入真正的着色器**并立即再次截图。如果是黑色，着色器有编译错误或逻辑问题。

4. **然后才开始录制。**90 秒的 ProRes 录制约 5GB。录制黑帧浪费磁盘和时间。

黑色 GLSL 输出的常见原因：
- macOS 上缺少 `TDOutputSwizzle()`（坑 #8）
- 时间 uniform 未连接 —— 着色器使用默认 0.0，分形停留在原点
- 频谱纹理未连接 —— 音频值全为 0.0，把一切驱动到黑色
- 应为浮点除法却用了整数除法（`1/2 = 0` 不是 `0.5`）
- `absTime.seconds % 1000.0` 回绕超过 1000，模运算产生意外值

### 41. td_write_dat 使用 `text` 参数，不是 `content`

MCP 工具 `td_write_dat` 期望 `text` 参数做全量替换。传递 `content` 返回错误：`"Provide either 'text' for full replace, or 'old_text'+'new_text' for patching"`。

如果 `td_write_dat` 失败，回退到 `td_execute_python`：
```python
op("/project1/shader_code").text = shader_string
```

### 42. td_execute_python 确实返回 print() 输出 —— 用它调试

`td_execute_python` 脚本中的 `print()` 语句出现在 MCP 响应文本中。这是从脚本回读值的正确方式。响应格式是：先打印输出，然后单独一行 `[fps X.X/X] [N err/N warn]`。

然而，`result` 变量（如果你设置了）不会逐字出现 —— 对任何需要回读的内容使用 `print()`：
```python
# 正确 —— 出现在响应中：
print('value:', some_value)

# 错误 —— 不一定在响应中：
result = some_value
```

对于结构化数据，使用专用检查工具（`td_get_operator_info`、`td_read_chop`），它们返回干净的 JSON。

### 43. td_get_operator_info JSON 末尾追加了 `[fps X.X/X]` —— 破坏 json.loads()

`td_get_operator_info` 的响应文本在 JSON 对象后追加了 `[fps 60.0/60]`。这导致 `json.loads()` 因 "Extra data" 错误失败。解析前去掉它：
```python
clean = response_text.rsplit('[fps', 1)[0]
data = json.loads(clean)
```

### 44. td_get_screenshot 不可靠 —— 返回 `{"status": "pending"}` 可能永远不交付

截图不会立即完成。该工具返回 `{"status": "pending", "requestId": "..."}` 而实际文件可能稍后出现 —— 或者永远不出现。在测试中（2026 年 4 月），截图无限期保持 "pending"，没有文件写入磁盘，即使着色器以 8-30fps cook。

**不要依赖 `td_get_screenshot` 做帧捕获。**对于可靠的帧捕获，使用 MovieFileOut 录制 + ffmpeg 帧提取：
```bash
# 先在 TD 中录制，然后提取帧：
ffmpeg -y -i /tmp/td_output.mov -t 25 -vf 'fps=24' /tmp/td_frames/frame_%06d.png
```

如果你需要快速视觉检查，`td_get_screenshot` 值得一试（有时有效），但始终要有录制作为备选。没有回调或完成通知 —— 如果文件 5-10 秒后不出现，就不会来了。

### 45. 重型着色器 cook 低于录制 FPS —— 输出中有许多重复帧

光线步进 GLSL 着色器可能仅以 8-15fps cook，即使 MovieFileOut 以 60fps 录制。录制仍然有效（TD 每次写入最后 cook 的帧），但生成的文件有许多重复帧。为后处理提取帧时，使用较低的 fps 滤镜避免冗余帧：
```bash
# 从 8fps 着色器的 60fps 录制中以 24fps 提取：
ffmpeg -y -i /tmp/td_output.mov -t 25 -vf 'fps=24' /tmp/td_frames/frame_%06d.png
```
在提交长时间录制之前用 `td_get_perf` 检查实际 cook FPS。如果 FPS < 15，无论录制编解码器如何，输出都是幻灯片。

### 46. 录制时长是手动的 —— 音频结束时不会自动停止

MovieFileOut 录制直到设置 `par.record = False`。如果音频在你停止录制前结束，文件会以重复帧不断增长。音频时长结束后总是及时停止录制。为精确：在 agent 端设置匹配音频长度的定时器，然后发送 `par.record = False`。用 ffmpeg 修剪多余部分作为安全网：
```bash
ffmpeg -i raw.mov -t 25 -c copy trimmed.mov
```

### 47. AudioFileIn 的 par.index 在顺序模式下保持为 0 —— 不是可靠的进度指示器

当 `audiofileinCHOP` 处于 `playmode=2`（顺序）时，即使音频确实在活动播放、频谱确实在接收数据，`par.index.eval()` 也返回 0.0。不要在顺序模式下使用 `par.index` 检查播放进度。

**如何验证音频确实在播放：**
- 通过 `td_read_chop` 读取频谱 CHOP 值 —— 如果值非零且在相隔 1-2 秒的读取之间变化，音频在流动
- 读取音频 CHOP 本身：非零波形采样确认文件已加载并在播放
- `par.play.eval()` 返回 True 是必要但不充分 —— 如果 cue 卡住，它可以为 True 而没有音频流动

### 48. GLSL 着色器白化 —— 在着色器中钳制音频频谱值

原始频谱值乘以 Math CHOP gain 可能产生非常大的数（5-20+），吹爆着色器的光照，产生扁平的白色/灰色。着色器必须钳制音频输入：

```glsl
float bass = texture(sTD2DInputs[1], vec2(0.05, 0.25)).r;
bass = clamp(bass, 0.0, 3.0);   // 防止白化
mids = clamp(mids, 0.0, 3.0);
hi = clamp(hi, 0.0, 3.0);
```

在 gain=10 于安静段落产生 ~0.13（太暗）但 gain=50 产生 ~9.4（完全白化）时发现。修复：保持 gain=10，在 AudioSpectrum 上用 `highfreqboost=3.0`，在着色器中钳制。

### 49. 非商业版 TD 以 1280x1280（正方形）录制 —— 总是在后期裁剪

即使在 GLSL TOP 上设置 `resolutionw=1280, resolutionh=720`，非商业版 TD 也可能向 MovieFileOut 输出 1280x1280。总是用 ffprobe 检查尺寸并在提取期间裁剪：

```bash
# 从 1280x1280 中心裁剪到 1280:720：
ffmpeg -y -i /tmp/td_output.mov -t 25 -r 24 -vf "crop=1280:720:0:280" /tmp/frames/frame_%06d.png
```

1280x1280 的大型 ProRes 文件（1-2GB）以约 3fps 解码，因此 25 秒素材提取需要约 3 分钟。

## 高级模式（坑 51+）

### 51. 连接语法：使用 `outputConnectors`/`inputConnectors`，不是 `outputs`/`inputs`

```python
# 正确
src.outputConnectors[0].connect(dst.inputConnectors[0])
# 错误 —— 抛出 IndexError 或 AttributeError
src.outputs[0].connect(dst.inputs[0])
```

对于 feedback TOP，两者都需要：
```python
fb.par.top = target.path
target.outputConnectors[0].connect(fb.inputConnectors[0])
```

### 52. moviefileoutTOP 的 `par.input` 在 TD 2025.32460 中无法通过 Python 解析

以编程方式设置 `moviefileoutTOP.par.input` 不起作用。所有形式都静默失败并显示 "Not enough sources specified."

**变通 —— 帧捕获 + ffmpeg：**
```python
out = op('/project1/out')
for i in range(300):
    delay = i * 5
    run(f"op('/project1/out').save('/tmp/frames/f_{i:04d}.png')", delayFrames=delay)
# 然后：ffmpeg -y -framerate 30 -i /tmp/frames/f_%04d.png -c:v prores -pix_fmt yuv420p /tmp/output.mov
```

### 53. 批量帧捕获 —— 使用 `me.fetch`/`me.store` 跨调用保持状态

```python
start = me.fetch('cap_frame', 0)
for i in range(60):
    frame = start + i
    op('/project1/out').save(f'/tmp/frames/frame_{str(frame).zfill(4)}.png')
me.store('cap_frame', start + 60)
```
调用 5 次得到 300 帧。每次从上次离开的地方继续。

### 54. TD 2025 中 GLSL TOP 像素着色器要求

```glsl
// 必需 —— 声明输出
layout(location = 0) out vec4 fragColor;

void main() {
    vec3 col = vec3(1.0, 0.0, 0.0);
    fragColor = TDOutputSwizzle(vec4(col, 1.0));
}
```
**可用的内置 uniform：**`uTDOutputInfo.res`（vec4）、`uTDTimeInfo.seconds`、`sTD2DInputs[N]`。
**自动创建的 DAT：**`name_pixel`、`name_vertex`、`name_compute` textDAT，带示例代码。

### 55. TOP.save() 不推进时间 —— 紧凑循环中帧相同

`.save()` 捕获当前 cook 帧而不推进 TD 时间线：
```python
# 错误 —— 所有帧相同
for i in range(300):
    op('/project1/out').save(f'frames/f_{i:04d}.png')

# 正确 —— 使用带 delayFrames 的 run()
for i in range(300):
    delay = i * 5
    run(f"op('/project1/out').save('frames/f_{i:04d}.png')", delayFrames=delay)
```
**绝不要在 TD 中使用 `time.sleep()`** —— 它阻塞主线程并冻结 UI。

### 56. 反馈循环掩盖输入变化 —— 捕获期间强制切换

feedback TOP 不透明度 0.7+ 时，缓冲区主导输出。切换输入产生几乎相同的帧。

**修复 —— 每次捕获强制切换索引：**
```python
for i in range(300):
    idx = (i // 8) % num_inputs
    delay = i * 5
    run(f"op('/project1/vswitch').par.index={idx}; op('/project1/out').save('f_{i:04d}.png')", delayFrames=delay)
```

### 57. 大型 td_execute_python 脚本失败 —— 拆分为增量调用

一个脚本中 10+ 个算子创建会导致时序问题。拆分为每次 2-4 个算子的 2-4 次调用。在一次调用内，`create()` 立即处理工作。跨调用时，如果前一次调用尚未提交，`op('name')` 可能返回 `None`。

### 58. project.load() 之后 MCP 实例重连

`project.load(path)` 更改 PID。加载后，调用 `td_list_instances()` 并使用新的 `target_instance`。对于 TOX 文件：作为子 comp 导入（不会断开连接）。

### 59. TOX 逆向工程工作流

```python
comp = root.loadTox(r'/path/to/file.tox')
comp.name = '_study_comp'
for child in comp.children:
    print(f'{child.name} ({child.OPType})')
# 使用 td_get_operators_info、td_read_dat、检查自定义参数
```

### 60. sliderCOMP 命名 —— TD 追加后缀

TD 自动重命名：`slider_brightness` → `slider_brightness1`。创建后总是检查名称。

### 61. create() 需要完整的算子类型后缀

```python
# 正确
proj.create('audiofileinCHOP', 'audio_in')
proj.create('glslTOP', 'render')

# 错误 —— 抛出 "Unknown operator type"
proj.create('audiofilein', 'audio_in')
proj.create('glsl', 'render')
```

### 62. 重设 COMP 父级 —— 使用 copyOPs，而非 connect()

用 `inputCOMPConnectors[0].connect()` 移动 COMP 会失败。使用复制 + 销毁：
```python
copied = target.copyOPs([source])  # 保留内部连线
source.destroy()
# 移动后手动重新连线外部连接
```

### 63. 滑块连线 —— 带 op() 表达式的 expressionCHOP 会使 TD 崩溃

```python
# 使 TD 崩溃 —— 不要这样做
echop = root.create(expressionCHOP, 'slider_ctrl')
echop.par.chan0expr = 'op("/project1/controls/slider_brightness1").par.value0'

# 可用 —— parameterCHOP 作为桥接
pchop = root.create(parameterCHOP, 'slider_vals')
pchop.par.ops = '/project1/controls'
pchop.par.parameters = 'value0'
pchop.par.custom = True
pchop.par.builtin = False
```
