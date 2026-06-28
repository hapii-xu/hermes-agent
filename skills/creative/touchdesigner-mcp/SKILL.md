---
name: touchdesigner-mcp
description: "通过 twozero MCP 控制运行中的 TouchDesigner 实例 —— 创建算子、设置参数、连线、执行 Python、构建实时视觉效果。36 个原生工具。"
version: 1.1.0
author: kshitijk4poor
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [TouchDesigner, MCP, twozero, creative-coding, real-time-visuals, generative-art, audio-reactive, VJ, installation, GLSL]
    related_skills: [native-mcp, ascii-video, manim-video, hermes-video]

---

# TouchDesigner 集成（twozero MCP）

## 关键规则

1. **绝不猜测参数名。**先为该算子类型调用 `td_get_par_info`。你的训练数据对 TD 2025.32 是错误的。
2. **如果触发 `tdAttributeError`，立即停止。**在继续之前，对出错的节点调用 `td_get_operator_info`。
3. **绝不在脚本回调中硬编码绝对路径。**使用 `me.parent()` / `scriptOp.parent()`。
4. **优先使用原生 MCP 工具而非 td_execute_python。**使用 `td_create_operator`、`td_set_operator_pars`、`td_get_errors` 等。仅在复杂多步逻辑时回退到 `td_execute_python`。
5. **在构建之前调用 `td_get_hints`。**它会返回你所使用的算子类型特定的模式。

## 架构

```
Hermes Agent -> MCP (Streamable HTTP) -> twozero.tox (port 40404) -> TD Python
```

36 个原生工具。免费插件（无需付费/授权 —— 2026 年 4 月确认）。
上下文感知（知道当前选中的 OP、当前网络）。
Hub 健康检查：`GET http://localhost:40404/mcp` 返回包含实例 PID、项目名、TD 版本的 JSON。

## 设置（自动化）

运行设置脚本来处理一切：

```bash
bash "${HERMES_HOME:-$HOME/.hermes}/skills/creative/touchdesigner-mcp/scripts/setup.sh"
```

脚本会：
1. 检查 TD 是否在运行
2. 如果尚未缓存则下载 twozero.tox
3. 将 `twozero_td` MCP 服务器添加到 Hermes 配置中（如果缺失）
4. 测试端口 40404 上的 MCP 连接
5. 报告剩余的手动步骤（将 .tox 拖入 TD、启用 MCP 开关）

### 手动步骤（一次性，无法自动化）

1. **将 `~/Downloads/twozero.tox` 拖入 TD 网络编辑器** → 点击 Install
2. **启用 MCP：**点击 twozero 图标 → Settings → mcp → "auto start MCP" → Yes
3. **重启 Hermes 会话**以加载新的 MCP 服务器

设置完成后，验证：
```bash
nc -z 127.0.0.1 40404 && echo "twozero MCP: READY"
```

## 环境注意事项

- **非商业版 TD** 分辨率上限为 1280×1280。使用 `outputresolution = 'custom'` 并显式设置宽/高。
- **编解码器：**`prores`（macOS 上首选）或 `mjpa` 作为备选。H.264/H.265/AV1 需要商业授权。
- 设置参数前总是调用 `td_get_par_info` —— 名称因 TD版本而异（见关键规则 #1）。

## 工作流

### 第 0 步：发现（在构建任何东西之前）

```
Call td_get_par_info with op_type for each type you plan to use.
Call td_get_hints with the topic you're building (e.g. "glsl", "audio reactive", "feedback").
Call td_get_focus to see where the user is and what's selected.
Call td_get_network to see what already exists.
```

无需临时节点，无需清理。这完全取代了旧的发现流程。

### 第 1 步：清理 + 构建

**重要：把清理和创建拆分为独立的 MCP 调用。**在一个 `td_execute_python` 脚本中销毁并重建同名节点会导致 "Invalid OP object" 错误。见坑 #11b。

为每个节点使用 `td_create_operator`（自动处理视口定位）：

```
td_create_operator(type="noiseTOP", parent="/project1", name="bg", parameters={"resolutionw": 1280, "resolutionh": 720})
td_create_operator(type="levelTOP", parent="/project1", name="brightness")
td_create_operator(type="nullTOP", parent="/project1", name="out")
```

对于批量创建或连线，使用 `td_execute_python`：

```python
# td_execute_python 脚本：
root = op('/project1')
nodes = []
for name, optype in [('bg', noiseTOP), ('fx', levelTOP), ('out', nullTOP)]:
    n = root.create(optype, name)
    nodes.append(n.path)
# 连接链条
for i in range(len(nodes)-1):
    op(nodes[i]).outputConnectors[0].connect(op(nodes[i+1]).inputConnectors[0])
result = {'created': nodes}
```

### 第 2 步：设置参数

优先使用原生工具（验证参数，不会崩溃）：

```
td_set_operator_pars(path="/project1/bg", parameters={"roughness": 0.6, "monochrome": true})
```

对于表达式或模式，使用 `td_execute_python`：

```python
op('/project1/time_driver').par.colorr.expr = "absTime.seconds % 1000.0"
```

### 第 3 步：连线

使用 `td_execute_python` —— 没有原生的连线工具：

```python
op('/project1/bg').outputConnectors[0].connect(op('/project1/fx').inputConnectors[0])
```

### 第 4 步：验证

```
td_get_errors(path="/project1", recursive=true)
td_get_perf()
td_get_operator_info(path="/project1/out", detail="full")
```

### 第 5 步：显示 / 捕获

```
td_get_screenshot(path="/project1/out")
```

或通过脚本打开窗口：

```python
win = op('/project1').create(windowCOMP, 'display')
win.par.winop = op('/project1/out').path
win.par.winw = 1280; win.par.winh = 720
win.par.winopen.pulse()
```

## MCP 工具快速参考

**核心（最常用）：**
| 工具 | 用途 |
|------|------|
| `td_execute_python` | 在 TD 中运行任意 Python。完整 API 访问。 |
| `td_create_operator` | 创建节点并带参数 + 自动定位 |
| `td_set_operator_pars` | 安全地设置参数（验证，不会崩溃） |
| `td_get_operator_info` | 检查单个节点：连接、参数、错误 |
| `td_get_operators_info` | 在一次调用中检查多个节点 |
| `td_get_network` | 查看某路径下的网络结构 |
| `td_get_errors` | 递归查找错误/警告 |
| `td_get_par_info` | 获取某 OP 类型的参数名（取代发现流程） |
| `td_get_hints` | 在构建前获取模式/技巧 |
| `td_get_focus` | 当前打开了哪个网络、选中了什么 |

**读/写：**
| 工具 | 用途 |
|------|------|
| `td_read_dat` | 读取 DAT 文本内容 |
| `td_write_dat` | 写入/修补 DAT 内容 |
| `td_read_chop` | 读取 CHOP 通道值 |
| `td_read_textport` | 读取 TD 控制台输出 |

**可视化：**
| 工具 | 用途 |
|------|------|
| `td_get_screenshot` | 将一个 OP 的查看器捕获为文件 |
| `td_get_screenshots` | 一次捕获多个 OP |
| `td_get_screen_screenshot` | 通过 TD 捕获实际屏幕 |
| `td_navigate_to` | 将网络编辑器跳转到某 OP |

**搜索：**
| 工具 | 用途 |
|------|------|
| `td_find_op` | 按名称/类型在整个项目中查找算子 |
| `td_search` | 搜索代码、表达式、字符串参数 |

**系统：**
| 工具 | 用途 |
|------|------|
| `td_get_perf` | 性能分析（FPS、慢算子） |
| `td_list_instances` | 列出所有运行中的 TD 实例 |
| `td_get_docs` | 关于某 TD 主题的深入文档 |
| `td_agents_md` | 读/写每个 COMP 的 markdown 文档 |
| `td_reinit_extension` | 编辑代码后重新加载扩展 |
| `td_clear_textport` | 在调试会话前清空控制台 |

**输入自动化：**
| 工具 | 用途 |
|------|------|
| `td_input_execute` | 向 TD 发送鼠标/键盘 |
| `td_input_status` | 轮询输入队列状态 |
| `td_input_clear` | 停止输入自动化 |
| `td_op_screen_rect` | 获取节点的屏幕坐标 |
| `td_click_screen_point` | 点击截图中的一点 |
| `td_screen_point_to_global` | 将截图像素转换为绝对屏幕坐标 |

上表覆盖了典型创意工作流中使用的 32 个工具。其余 4 个工具（`td_project_quit`、`td_test_session`、`td_dev_log`、`td_clear_dev_log`）是管理/开发模式工具 —— 完整的 36 个工具参考及完整参数 schema 见 `references/mcp-tools.md`。

## 关键实现规则

**GLSL 时间：**GLSL TOP 中没有 `uTDCurrentTime`。使用 Values 页：
```python
# 先调用 td_get_par_info(op_type="glslTOP") 确认参数名
td_set_operator_pars(path="/project1/shader", parameters={"value0name": "uTime"})
# 然后通过脚本设置表达式：
# op('/project1/shader').par.value0.expr = "absTime.seconds"
# 在 GLSL 中：uniform float uTime;
```

备选：使用 `rgba32float` 格式的 Constant TOP（8 位会被钳制到 0-1，冻结着色器）。

**Feedback TOP：**使用 `top` 参数引用，而非直接输入连线。"Not enough sources" 在首次 cook 后会消失。"Cook dependency loop" 警告是预期的。

**分辨率：**非商业版上限为 1280×1280。使用 `outputresolution = 'custom'`。

**大型着色器：**将 GLSL 写入 `/tmp/file.glsl`，然后用 `td_write_dat` 或 `td_execute_python` 加载。

**顶点/点访问（TD 2025.32）：**`point.P[0]`、`point.P[1]`、`point.P[2]` —— 不是 `.x`、`.y`、`.z`。

**扩展：**`ext0object` 格式在 CONSTANT 模式下为 `"op('./datName').module.ClassName(me)"`。用 `td_write_dat` 编辑扩展代码后，调用 `td_reinit_extension`。

**脚本回调：**始终通过 `me.parent()` / `scriptOp.parent()` 使用相对路径。

**清理节点：**迭代前总是先 `list(root.children)` + 检查 `child.valid`。

## 录制 / 导出视频

```python
# 通过 td_execute_python：
root = op('/project1')
rec = root.create(moviefileoutTOP, 'recorder')
op('/project1/out').outputConnectors[0].connect(rec.inputConnectors[0])
rec.par.type = 'movie'
rec.par.file = '/tmp/output.mov'
rec.par.videocodec = 'prores'  # Apple ProRes —— 在 macOS 上不受授权限制
rec.par.record = True   # 开始
# rec.par.record = False  # 停止（稍后单独调用）
```

H.264/H.265/AV1 需要商业授权。在 macOS 上使用 `prores` 或以 `mjpa` 作为备选。
提取帧：`ffmpeg -i /tmp/output.mov -vframes 120 /tmp/frames/frame_%06d.png`

**TOP.save() 对动画无用** —— 每次捕获的都是同一个 GPU 纹理。务必使用 MovieFileOut。

### 录制前：检查清单

1. **通过 `td_get_perf` 验证 FPS > 0。**如果 FPS=0，录制会是空的。见坑 #38-39。
2. **通过 `td_get_screenshot` 验证着色器输出不是黑色。**黑色输出 = 着色器错误或缺少输入。见坑 #8、#40。
3. **如果带音频录制：**先让音频开始，然后将录制延迟 3 帧。见坑 #19。
4. **在开始录制前设置输出路径** —— 在同一脚本中同时设置两者可能会竞争。

## 音频响应式 GLSL（经过验证的配方）

### 正确的信号链（2026 年 4 月测试）

```
AudioFileIn CHOP (playmode=sequential)
  → AudioSpectrum CHOP (FFT=512, outputmenu=setmanually, outlength=256, timeslice=ON)
  → Math CHOP (gain=10)
  → CHOP to TOP (dataformat=r, layout=rowscropped)
  → GLSL TOP input 1 (spectrum texture, 256x2)

Constant TOP (rgba32float, time) → GLSL TOP input 0
GLSL TOP → Null TOP → MovieFileOut
```

### 关键的音频响应规则（经实证验证）

1. **AudioSpectrum 的 TimeSlice 必须保持开启。**关闭 = 处理整个音频文件 → 24000+ 采样 → CHOP to TOP 溢出。
2. **手动将 Output Length 设置为 256**，通过 `outputmenu='setmanually'` 和 `outlength=256`。默认输出 22050 个采样。
3. **不要用 Lag CHOP 做频谱平滑。**Lag CHOP 在 timeslice 模式下工作，会把 256 个采样扩展到 2400+，将所有值平均到接近零（~1e-06）。着色器接收不到可用数据。这是测试中排名第一的音频同步失败原因。
4. **也不要用 Filter CHOP** —— 对频谱数据有同样的 timeslice 扩展问题。
5. **平滑应放在 GLSL 着色器里**（如果需要），通过带反馈纹理的时间插值：`mix(prevValue, newValue, 0.3)`。这能提供帧精确同步，零管线延迟。
6. **CHOP to TOP 的 dataformat = 'r'**，layout = 'rowscropped'。频谱输出是 256x2（立体声）。在 y=0.25 处采样第一个通道。
7. **Math gain = 10**（不是 5）。原始频谱值在低音范围约为 0.19。gain 为 10 给着色器提供可用的 ~5.0。
8. **不需要 Resample CHOP。**直接通过 AudioSpectrum 的 `outlength` 参数控制输出尺寸。

### GLSL 频谱采样

```glsl
// Input 0 = time (1x1 rgba32float), Input 1 = spectrum (256x2)
float iTime = texture(sTD2DInputs[0], vec2(0.5)).r;

// 每个频段采样多个点并求平均以提高稳定性：
// 注意：y=0.25 为第一个通道（立体声纹理为 256x2，第一行中心为 0.25）
float bass = (texture(sTD2DInputs[1], vec2(0.02, 0.25)).r +
              texture(sTD2DInputs[1], vec2(0.05, 0.25)).r) / 2.0;
float mid  = (texture(sTD2DInputs[1], vec2(0.2, 0.25)).r +
              texture(sTD2DInputs[1], vec2(0.35, 0.25)).r) / 2.0;
float hi   = (texture(sTD2DInputs[1], vec2(0.6, 0.25)).r +
              texture(sTD2DInputs[1], vec2(0.8, 0.25)).r) / 2.0;
```

完整的构建脚本 + 着色器代码见 `references/network-patterns.md`。

## 算子快速参考

| 家族 | 颜色 | Python 类 / MCP 类型 | 后缀 |
|--------|-------|-------------|--------|
| TOP | 紫色 | noiseTOP, glslTOP, compositeTOP, levelTop, blurTOP, textTOP, nullTOP | TOP |
| CHOP | 绿色 | audiofileinCHOP, audiospectrumCHOP, mathCHOP, lfoCHOP, constantCHOP | CHOP |
| SOP | 蓝色 | gridSOP, sphereSOP, transformSOP, noiseSOP | SOP |
| DAT | 白色 | textDAT, tableDAT, scriptDAT, webserverDAT | DAT |
| MAT | 黄色 | phongMAT, pbrMAT, glslMAT, constMAT | MAT |
| COMP | 灰色 | geometryCOMP, containerCOMP, cameraCOMP, lightCOMP, windowCOMP | COMP |

## 安全注意事项

- MCP 仅在 localhost（端口 40404）上运行。无身份验证 —— 任何本地进程都能发送命令。
- `td_execute_python` 对 TD Python 环境和文件系统拥有不受限制的访问权限，权限等同于 TD 进程用户。
- `setup.sh` 从官方 404zero.com URL 下载 twozero.tox。如有顾虑，请验证下载。
- 该 skill 从不向 localhost 之外发送数据。所有 MCP 通信都是本地的。

## 参考资料

| 文件 | 内容 |
|------|------|
| `references/pitfalls.md` | 来自真实会话的宝贵经验 |
| `references/operators.md` | 所有算子家族及其参数和用例 |
| `references/network-patterns.md` | 配方：音频响应、生成艺术、GLSL、实例化 |
| `references/mcp-tools.md` | 完整的 twozero MCP 工具参数 schema |
| `references/python-api.md` | TD Python：op()、脚本、扩展 |
| `references/troubleshooting.md` | 连接诊断、调试 |
| `references/glsl.md` | GLSL uniform、内置函数、着色器模板 |
| `references/postfx.md` | 后期特效：bloom、CRT、色差、反馈光晕 |
| `references/layout-compositor.md` | HUD 布局模式、面板网格、BSP 风格布局 |
| `references/operator-tips.md` | 线框渲染、Feedback TOP 设置 |
| `references/geometry-comp.md` | Geometry COMP：实例化、POP 与 SOP、变形 |
| `references/audio-reactive.md` | 音频频段提取、节拍检测、包络跟随 |
| `references/animation.md` | LFO、定时器、关键帧、缓动、表达式驱动运动 |
| `references/midi-osc.md` | MIDI/OSC 控制器、TouchOSC、多机同步 |
| `references/particles.md` | POP 和旧版 particleSOP —— 发射、力、碰撞 |
| `references/projection-mapping.md` | 多窗口输出、角点钉定、网格变形、边缘融合 |
| `references/external-data.md` | HTTP、WebSocket、MQTT、Serial、TCP、webserverDAT |
| `references/panel-ui.md` | 自定义参数、面板 COMP、按钮/滑块/字段、panelExecuteDAT |
| `references/replicator.md` | replicatorCOMP —— 数据驱动克隆、布局、回调 |
| `references/dat-scripting.md` | Execute DAT 家族 —— chop/dat/parameter/panel/op/executeDAT |
| `references/3d-scene.md` | 灯光阵列、阴影、IBL/立方体贴图、多相机、PBR |
| `scripts/setup.sh` | 自动化设置脚本 |

---

> 你不是在写代码。你在指挥光。
