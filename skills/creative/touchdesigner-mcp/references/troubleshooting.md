# TouchDesigner 故障排除（twozero MCP）

> 完整的经验教训清单见 `references/pitfalls.md`。

## 1. 连接问题

### 端口 40404 无响应

按顺序检查：

1. TouchDesigner 是否在运行？
   ```bash
   pgrep TouchDesigner
   ```

1b. 快速 hub 健康检查（无需 JSON-RPC）：
   对 MCP URL 发一个普通 GET 会返回实例信息：
   ```
   curl -s http://localhost:40404/mcp
   ```
   返回：`{"hub": true, "pid": ..., "instances": {"127.0.0.1_PID": {"project": "...", "tdVersion": "...", ...}}}`
   若返回 JSON 但 `instances` 为空，说明 TD 在运行但 twozero 尚未注册。

2. twozero 是否已安装到 TD？
   打开 TD 的 Palette 浏览器 > 应列出 twozero。若没有，安装它。

3. twozero 设置里是否启用了 MCP？
   在 TD 中打开 twozero 偏好设置，确认 MCP 服务器开关为 ON。

4. 直接测试端口：
   ```bash
   nc -z 127.0.0.1 40404
   ```

5. 测试 MCP 端点：
   ```bash
   curl -s http://localhost:40404/mcp
   ```
   应返回带 hub 信息的 JSON。若返回，说明服务器在运行。

### Hub 响应但没有 TD 实例

twozero MCP hub 在运行，但 TD 尚未注册。原因：
- TD 工程尚未加载（仍在启动画面）
- 当前工程里未初始化 twozero COMP
- twozero 版本不匹配

修复：打开/重新加载一个含 twozero COMP 的 TD 工程。用 td_list_instances
检查哪些 TD 实例已注册。

### 多实例设置

twozero 会为多个 TD 实例自动分配端口：
- 第一个实例：40404
- 第二个实例：40405
- 第三个实例：40406
- 以此类推

用 `td_list_instances` 发现已运行实例及其端口。

## 2. MCP 工具错误

### td_execute_python 返回错误

td_execute_python 的错误信息常包含 Python 回溯。
若不清楚，用 `td_read_textport` 查看完整 TD 控制台输出 ——
Python 异常总是会打印到那里。

常见原因：
- 脚本语法错误
- 引用了不存在的节点（op() 返回 None，然后你对 None 调 .par）
- 用错参数名（见 pitfalls.md）

### td_set_operator_pars 失败

参数名不匹配是头号原因。该工具会校验参数名并
返回清晰错误，但你必须使用确切名称。

修复：始终先调用 `td_get_par_info` 发现真实参数名：
```
td_get_par_info(op_type='glslTOP')
td_get_par_info(op_type='noiseTOP')
```

### td_create_operator 类型名错误

算子类型名使用 camelCase 加家族后缀：
- 正确：noiseTOP、glslTOP、levelTOP、compositeTOP、audiospectrumCHOP
- 错误：NoiseTOP、noise_top、NOISE TOP、Noise

### td_get_operator_info 用于深入检查

若对算子的任何方面（参数、输入、输出、状态）不确定：
```
td_get_operator_info(path='/project1/noise1', detail='full')
```

## 3. 参数发现

关键：始终用 td_get_par_info 发现参数名。

智能体的 LLM 训练数据中包含错误的 TouchDesigner 参数名。
不要信任它们。已知错误名包括 dat vs pixeldat、colora vs alpha、
sizex vs size 等等。完整清单见 pitfalls.md。

工作流：
1. td_get_par_info(op_type='glslTOP') —— 获取某类型的所有参数
2. td_get_operator_info(path='/project1/mynode', detail='full') —— 获取某具体实例的参数
3. 只使用这些工具返回的名称

## 4. 性能

### 诊断性能缓慢

用 `td_get_perf` 查看哪些算子慢。关注 cook 时间 ——
任何超过每帧 1ms 的都值得排查。

常见原因：
- 分辨率过高（尤其非商业版）
- 复杂的 GLSL 着色器
- 过多的 TOP→CHOP 或 CHOP→TOP 传输（GPU-CPU 内存拷贝）
- 无衰减的反馈循环（数值累积、内存增长）

### 非商业版授权限制

- 分辨率上限：1280x1280。设置 resolutionw=1920 会被静默钳到 1280。
- H.264/H.265/AV1 编码需要 Commercial 授权。请改用 ProRes 或 Hap。
- 输出不得用于商业用途。

创建后始终检查实际有效分辨率：
```python
n.cook(force=True)
actual = str(n.width) + 'x' + str(n.height)
```

## 5. Hermes 配置

### 配置位置

`$HERMES_HOME/config.yaml`（`HERMES_HOME` 未设置时默认为 `~/.hermes/config.yaml`）

### MCP 条目格式

twozero TD 条目应类似：
```yaml
mcpServers:
  twozero_td:
    url: http://localhost:40404/mcp
```

### 配置改动后

重启 Hermes 会话以使改动生效。MCP 连接在
会话启动时建立。

### 验证 MCP 工具可用

重启后，会话日志应显示 twozero MCP 工具已注册。
若工具显示已注册但无法调用，检查：
- twozero MCP hub 仍在运行（上面的 curl 测试）
- TD 仍在运行且加载了工程
- 没有防火墙阻挡 localhost:40404

## 6. 节点创建问题

### “Node type not found”错误

类型字符串错误。使用 camelCase 加家族后缀：
- 错误：NoiseTop、noise_top、NOISE TOP
- 正确：noiseTOP

### 节点已创建但不可见

检查 parentPath —— 使用如 /project1 的绝对路径。默认工程
根为 /project1。系统节点位于 /、/ui、/sys、/local、/perform。
不要在 /project1 之外创建用户节点。

### 无法在非 COMP 内创建节点

只有 COMP 算子（Container、Base、Geometry 等）能包含子级。
不能在 TOP、CHOP、SOP、DAT 或 MAT 内创建节点。

## 7. 接线问题

### 跨家族接线

TOP 连 TOP、CHOP 连 CHOP、SOP 连 SOP、DAT 连 DAT。
用转换算子桥接：choptoTOP、topToCHOP、soptoDAT 等。

注意：choptoTOP 没有输入连接器。改用 par.chop 引用：
```python
spec_tex.par.chop = resample_node  # 正确
# 不要：resample.outputConnectors[0].connect(spec_tex.inputConnectors[0])
```

### 反馈循环

切勿直接创建 A -> B -> A。用 Feedback TOP：
```python
fb = root.create(feedbackTOP, 'fb')
fb.par.top = comp.path          # 仅引用，不要往 fb 输入接线
fb.outputConnectors[0].connect(next_node)
```
链上出现“Cook dependency loop detected”警告是预期且正确的。

## 8. GLSL 问题

### 着色器编译错误是静默的

GLSL TOP 在 UI 中显示黄色警告，但 node.errors() 可能返回空。
同时检查 node.warnings()。创建一个指向该 GLSL TOP 的 Info DAT 以
获取完整编译器输出。

### TD GLSL 特性

- 使用 GLSL 4.60（Vulkan 后端）。GLSL 3.30 及更早版本已移除。
- UV 坐标：vUV.st（不是 gl_FragCoord）
- 输入纹理：sTD2DInputs[0]
- 输出：layout(location = 0) out vec4 fragColor
- macOS 关键：始终用 TDOutputSwizzle(color) 包裹输出
- 没有内置 time uniform。通过 GLSL TOP Values 页或 Constant TOP 传入时间。

## 9. 录制问题

### H.264/H.265/AV1 需要 Commercial 授权

在 macOS 上使用 Apple ProRes（硬件加速、不受授权限制）：
```python
rec.par.videocodec = 'prores'  # macOS 首选 —— 无损、非商业版可用
# rec.par.videocodec = 'mjpa'  # 备选 —— 有损、到处可用
```

### MovieFileOut 没有 .record() 方法

使用切换参数：
```python
rec.par.record = True   # 开始
rec.par.record = False  # 停止
```

### 所有导出帧完全相同

快速连续调用 TOP.save() 会捕获同一帧。实时录制请用 MovieFileOut。
要帧精确输出，设置 project.realTime = False。
