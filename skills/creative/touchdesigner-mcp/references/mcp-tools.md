# twozero MCP 工具参考

来自 twozero MCP v2.774+（2026 年 4 月）的 36 个工具。
所有工具都接受一个可选的 `target_instance` 参数，用于多 TD 实例场景。

## 执行与脚本

### td_execute_python

在 TouchDesigner 内部执行 Python 代码并返回结果。拥有完整的 TD Python API 访问权限（op、project、app 等）。Print 语句和最后一个表达式的值会被捕获。最适合：连线（inputConnectors）、设置表达式（par.X.expr/mode）、查询参数名，以及批量创建脚本（5 个及以上算子）。创建 1-4 个算子时，请改用 td_create_operator。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `code` | string | yes | 要在 TouchDesigner 中执行的 Python 代码 |

## 网络与结构

### td_get_network

获取 TouchDesigner (TD) 中给定路径下的算子网络结构。返回紧凑列表：name OPType flags。第一行是所查询 op 的完整路径。标志：ch:N=子节点数，!cook=allowCooking 关闭，bypass，private=isPrivate，blocked:reason，"comment text"。depth=0（默认）= 仅当前层级。depth=1 = 一层子节点（带缩进）。要深入探索，请再次对特定 COMP 路径调用。系统算子（/ui、/sys）默认隐藏。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `path` | string | no | 要检查的网络路径，例如 '/' 或 '/project1' |
| `depth` | integer | no | 递归多少层。0=仅当前层（推荐），1=包含 COMP 的直接子节点 |
| `includeSystem` | boolean | no | 包含系统算子（/ui、/sys）。默认 false。 |
| `nodeXY` | boolean | no | 包含 nodeX、nodeY 坐标。默认 false。 |

### td_create_operator

在 TouchDesigner (TD) 中创建一个新算子（节点）。创建算子的首选方式 —— 自动处理视口定位、viewer 标志和停靠的 op。对于批量创建（5 个及以上 op），可改用 td_execute_python 加脚本，但请先调用 td_get_hints('construction') 以获取正确的参数名和布局规则。支持所有 TD 算子类型：TOP、CHOP、SOP、DAT、COMP、MAT。如果省略 parent，则创建在用户视口位置当前打开的网络中。构建容器时：先创建 baseCOMP（无 parent），然后用 parent=compPath 创建子节点。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `type` | string | yes | 算子类型，例如 'textDAT'、'constantCHOP'、'noiseTOP'、'transformTOP'、'baseCOMP' |
| `parent` | string | no | 父算子的路径。如果省略，使用 TD 中当前打开的网络。 |
| `name` | string | no | 新算子的名称（可选，省略时 TD 自动命名） |
| `parameters` | object | no | 在创建的算子上设置的键值对参数 |

### td_find_op

按名称和/或类型在整个项目中查找算子。返回 TSV：path、OPType、flags。标志：bypass、!cook、private、blocked:reason。使用 td_search 在代码/表达式内部搜索；使用 td_find_op 查找算子本身。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `name` | string | no | 在算子名中匹配的子串（不区分大小写）。例如 'noise' 会找到 noise1、noise2、myNoise。 |
| `type` | string | no | 在 OPType 中匹配的子串（不区分大小写）。例如 'noiseTOP'、'baseCOMP'、'CHOP'。用精确类型提高精度，用部分匹配扩大范围。 |
| `root` | string | no | 搜索的根算子路径。默认 '/project1'。 |
| `max_results` | number | no | 返回的最大结果数。默认 50。 |
| `max_depth` | number | no | 从 root 开始的最大递归深度。默认无限制。 |
| `detail` | `basic` / `summary` | no | 结果详细程度。'basic' = 名称/路径/类型（快）。'summary' = + 连接、非默认参数、表达式。默认 'basic'。 |

### td_search

搜索 TD 项目中所有代码（DAT 脚本）、参数表达式和字符串参数值。返回 TSV：path、kind（code/expression/parameter/ref）、line、text。context>0 时返回 JSON。单词以 OR 方式匹配。用引号做精确短语：'GetLogin "op(\'login\')"'。使用 count_only=true 可在不获取完整结果的情况下快速检查是否被引用。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `query` | string | yes | 搜索查询。多个单词 = OR（任意匹配）。用引号包裹做精确短语。示例：'GetLogin getLogin' 会找到任一。 |
| `root` | string | no | 搜索的根算子路径。默认 '/project1'。 |
| `scope` | `all` / `code` / `editable` / `expressions` / `parameters` | no | 搜索范围。'code' = 仅 DAT 脚本（快，约 0.05s）。'editable' = 仅可编辑代码（跳过继承/引用的 DAT）。'expressions' = 仅参数表达式。'parameters' = 仅字符串参数值。'all' = 全部（慢，约 1.5s，因需扫描参数）。默认 'all'。 |
| `case_sensitive` | boolean | no | 区分大小写匹配。默认 false。 |
| `max_results` | number | no | 返回的最大结果数。默认 50。 |
| `context` | number | no | 每个代码匹配前后显示的行数。可省去 td_read_dat 调用。默认 0。 |
| `count_only` | boolean | no | 仅返回匹配数，不返回结果。快速存在性检查。 |
| `max_depth` | number | no | 从 root 开始的最大递归深度。默认无限制。 |

### td_navigate_to

将 TouchDesigner 网络编辑器视口导航到特定算子。打开该算子的父网络并将视图居中于其上。用它向用户展示问题所在，或在修改算子前导航到它。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `path` | string | yes | 要导航到的算子路径，例如 '/project1/noise1' |

## 算子检查

### td_get_operator_info

获取 TouchDesigner (TD) 中特定算子（节点）的信息。detail='summary'：连接、非默认参数、表达式、CHOP 通道（紧凑）。detail='full'：以上全部加上每个参数的 值/默认值/标签。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `path` | string | yes | 算子的完整路径，例如 '/project1/noise1' |
| `detail` | `summary` / `full` | no | 详细程度。'summary' = 连接、表达式、非默认参数、自定义参数（pulse 标记）、CHOP 通道。'full' = summary + 全部参数。默认 'full'。 |

### td_get_operators_info

在一次调用中获取多个算子的信息。返回算子信息对象数组。可替代多次调用 td_get_operator_info。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `paths` | array | yes | 完整算子路径数组，例如 ['/project1/null1', '/project1/null2'] |
| `detail` | `summary` / `full` | no | 详细程度。默认 'summary'。 |

### td_get_par_info

获取 TouchDesigner 算子类型的参数名和详情。不指定 pars：返回所有参数的紧凑列表，含名称、类型和菜单选项。指定 pars：返回特定参数的完整详情（帮助文本、菜单值、样式）。当你需要在设置之前知道确切的参数名时使用。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `op_type` | string | yes | TD 算子类型名，例如 'noiseTOP'、'blurTOP'、'lfoCHOP'、'compositeTOP' |
| `pars` | array | no | 要获取完整详情的可选特定参数名列表 |

## 参数设置

### td_set_operator_pars

在 TouchDesigner (TD) 中设置算子的参数和标志。对于简单的参数更改，比 td_execute_python 更安全。可以设置值、切换 bypass/viewer，而无需编写 Python 代码。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `path` | string | yes | 算子的路径 |
| `parameters` | object | no | 要设置的参数键值对 |
| `bypass` | boolean | no | 设置算子的 bypass 状态（COMP 上不可用） |
| `viewer` | boolean | no | 设置算子的 viewer 状态 |
| `allowCooking` | boolean | no | 在 COMP 上设置 cooking 标志。为 False 时，内部网络停止 cooking（0 CPU）。仅限 COMP。 |

## 数据读/写

### td_read_dat

读取 TouchDesigner (TD) 中 DAT 算子的文本内容。返回带行号的内容。用于读取脚本、扩展、GLSL 着色器、表格数据。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `path` | string | yes | DAT 算子的路径 |
| `start_line` | integer | no | 起始行（从 1 开始）。省略则从头读取。 |
| `end_line` | integer | no | 结束行（含）。省略则读到末尾。 |

### td_write_dat

写入或修补 TouchDesigner (TD) 中 DAT 算子的文本内容。可做全量替换或 StrReplace 风格的修补（old_text -> new_text）。用于编辑脚本、扩展、着色器。不会自动重新初始化扩展。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `path` | string | yes | DAT 算子的路径 |
| `text` | string | no | 全量替换文本。使用此参数或 old_text+new_text，不可同时使用。 |
| `old_text` | string | no | 要查找并替换的文本（在 DAT 中必须唯一） |
| `new_text` | string | no | 替换文本 |
| `replace_all` | boolean | no | 如果为 true，替换 old_text 的所有出现（默认：false，要求唯一匹配） |

### td_read_chop

读取 CHOP 通道采样数据。以数组形式返回通道值。当你需要实际采样值（动画曲线、查找表、波形），而不仅仅是 td_get_operator_info 的摘要时使用。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `path` | string | yes | CHOP 算子的路径 |
| `channels` | array | no | 要读取的通道名。省略则读取所有通道。 |
| `start` | integer | no | 起始采样索引（从 0 开始）。省略则从头读取。 |
| `end` | integer | no | 结束采样索引（含）。省略则读到末尾。 |

### td_read_textport

读取 TouchDesigner (TD) 日志/textport（控制台输出）的最后 N 行。用它查看 TD 的错误、警告和 print 输出。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `lines` | integer | no | 要返回的最近行数 |

### td_clear_textport

清空 MCP textport 日志缓冲区。在开始调试会话或编辑-运行-检查循环之前使用，以保持 td_read_textport 输出聚焦且最小。

无参数（除可选的 `target_instance` 外）。

## 可视化捕获

### td_get_screenshot

获取 TouchDesigner (TD) 中某算子 viewer 的截图。将图像保存到文件并返回文件路径。使用你的文件读取工具查看图像。显示算子在 viewer 中的样子（TOP 输出、CHOP 波形图、SOP 几何体、DAT 表格、参数 UI 等）。用它可视化检查任何算子，或通过 TD 为你的项目生成图像。两步异步用法：第 1 步 —— 用 'path' 调用以启动：返回 {'status': 'pending', 'requestId': '...'}。第 2 步 —— 用 'request_id' 调用以获取：返回 {'file': '/tmp/.../opname_id.jpg'}。然后读取文件查看图像。如果第 2 步仍返回 pending，执行一个其他工具调用后再重试。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `path` | string | no | 要截图的完整算子路径，例如 '/project1/noise1'。第 1 步必需。 |
| `request_id` | string | no | 从第 1 步获取的请求 ID，用于检索完成的截图。 |
| `max_size` | integer | no | 较长边的最大像素尺寸（默认 512）。用 0 表示原始算子分辨率（适合像素精确的 UI 工作）。用更高值（例如 1024）获取更多细节。 |
| `output_path` | string | no | 图像应保存到的可选绝对路径（例如 '/Users/me/project/render.png'）。如果省略，保存到 /tmp/pisang_mcp/screenshots/。请使用绝对路径 —— TD 的工作目录可能与 agent 的不同。 |
| `as_top` | boolean | no | 如果为 true，直接将算子捕获为 TOP（绕过 viewer 渲染器），保留 alpha/透明度。仅对 TOP 算子有效 —— 如果目标不是 TOP，自动回退到 viewer。当你需要带 alpha 的干净 PNG 时使用，例如保存生成的图像供其他项目使用。 |
| `format` | `auto` / `jpg` / `png` | no | 图像格式。'auto'（默认）：viewer 模式用 JPEG，as_top=true 用 PNG。'jpg'：始终 JPEG（更小）。'png'：始终 PNG（无损）。 |

### td_get_screenshots

一次性批量获取多个算子的截图。将图像保存到文件并返回文件路径。使用你的文件读取工具查看图像。两步异步用法：第 1 步 —— 用 'paths' 数组调用以启动：返回 {'status': 'pending', 'batchId': '...', 'total': N}。第 2 步 —— 用 'batch_id' 调用以获取：返回 {'files': [{op, file}, ...]}。然后读取文件查看图像。如果仍在处理中，返回 {'status': 'pending', 'ready': K, 'total': N}。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `paths` | array | no | 要截图的完整算子路径列表。第 1 步必需。 |
| `batch_id` | string | no | 从第 1 步获取的批次 ID，用于检索完成的截图。 |
| `max_size` | integer | no | 较长边的最大像素尺寸（默认 512）。用 0 表示原始分辨率。 |
| `as_top` | boolean | no | 如果为 true，直接捕获 TOP 算子（保留 alpha）。非 TOP 算子回退到 viewer。 |
| `output_dir` | string | no | 目录的可选绝对路径。每张截图在其中保存为 <opname>.jpg 或 .png 并保留在磁盘上。 |
| `format` | `auto` / `jpg` / `png` | no | 图像格式。'auto'（默认）：viewer 模式用 JPEG，as_top=true 用 PNG。'jpg'：始终 JPEG（更小）。'png'：始终 PNG（无损）。 |

### td_get_screen_screenshot

通过 TD 的 screenGrabTOP 捕获实际屏幕的截图。将图像保存到文件并返回文件路径。使用你的文件读取工具查看图像。与 td_get_screenshot（算子 viewer）不同，这显示用户在显示器上实际看到的 —— TD 窗口、UI 面板、一切。用于模拟鼠标/键盘输入时验证屏幕上发生了什么。工作流：td_get_screen_screenshot → 读取文件 → td_input_execute → 等待空闲 → 再次 td_get_screen_screenshot。两步异步：第 1 步 —— 不带 request_id 调用：返回 {'status':'pending','requestId':'...'}。第 2 步 —— 带 request_id 调用：返回 {'file': '/tmp/.../screen_id.jpg', 'info': '...metadata...'}。然后读取文件查看图像。requestId 之后也可用于 td_screen_point_to_global 做后续坐标查找。crop_x/y/w/h 以实际屏幕像素为单位（不是图像像素）。超出屏幕边界的裁剪会自动钳制。智能默认值：省略时 max_size 自动 —— 全屏用 1920（良好概览），裁剪时用 max(crop_w,crop_h)（保证 1:1 比例）。1:1 比例时：screen_coord = crop_origin + image_pixel。否则使用元数据中的公式。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `request_id` | string | no | 从第 1 步获取的请求 ID，用于检索完成的截图。 |
| `max_size` | integer | no | 较长边的最大像素尺寸。省略时自动：全屏 1920，裁剪时 max(crop_w,crop_h)（1:1）。显式设置以覆盖。 |
| `crop_x` | integer | no | 左边缘的屏幕像素坐标。 |
| `crop_y` | integer | no | 顶边缘的屏幕像素坐标（y=0 在屏幕顶部）。 |
| `crop_w` | integer | no | 宽度（像素）。 |
| `crop_h` | integer | no | 高度（像素）。 |
| `display` | integer | no | 屏幕索引（默认 0 = 主显示器）。 |

## 上下文与焦点

### td_get_focus

获取 TouchDesigner (TD) 中当前的用户焦点：哪个网络是打开的、选中的算子、当前算子以及 rollover（鼠标光标下方是什么）。重要：当用户说 'this operator' 或 'вот этот' 时，他们指的是选中/当前算子，而不是 rollover。Rollover 只是附带的鼠标位置，应按意图忽略。传 screenshots=true 以立即为所有选中算子启动截图批次 —— 响应包含带 batchId 的 'screenshots' 字段；用 td_get_screenshots(batch_id=...) 检索。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `screenshots` | boolean | no | 如果为 true，为所有选中算子启动截图批次。用 td_get_screenshots(batch_id=...) 检索。 |
| `max_size` | integer | no | screenshots=true 时的最大截图尺寸（默认 512）。 |
| `as_top` | boolean | no | screenshots=true 时传递给截图批次。 |

### td_get_errors

查找 TouchDesigner (TD) 算子中的错误和警告。检查算子错误、警告以及损坏的参数表达式（缺失通道、错误引用等）。还包含日志中最近的脚本错误（traceback），经分组和去重 —— 例如 1000 个相同的 mouse-move 错误显示为 ×1000 一条。如果给出 path，检查该算子及其子节点。如果没有 path，检查当前打开的网络。用 '/' 检查整个项目。当用户说有什么坏了、有错误、红色节点、горит ошибка 等时使用。提示：重现错误前调用 td_clear_textport 以保持日志聚焦。提示：当用户说 'тупит/лагает' 时与 td_get_perf 结合，同时检查错误和性能。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `path` | string | no | 要检查的路径。如果省略，检查当前网络。用 '/' 扫描整个项目。 |
| `recursive` | boolean | no | 递归检查子节点（默认 true） |
| `include_log` | boolean | no | 包含日志中最近的脚本错误，按唯一签名分组（默认 true）。重现错误前使用 td_clear_textport 以保持结果聚焦。 |

### td_get_perf

从 TouchDesigner (TD) 获取性能数据。返回 TSV：带 fps/预算/内存摘要的表头，然后是按 cook 时间排序的最慢算子。列：path、OPType、cpu/cook(ms)、gpu/cook(ms)、cpu/s、gpu/s、rate、flags。当用户报告卡顿、低 FPS、慢、тупит、тормозит 时使用。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `path` | string | no | 要分析的路径。如果省略，分析当前网络。用 '/' 检查整个项目。 |
| `top` | integer | no | 返回的最慢算子数量 |

## 文档

### td_get_docs

获取关于 TouchDesigner 主题的综合文档。与 td_get_hints（紧凑提示）不同，这返回深入的参考资料。不带参数调用以查看可用主题及描述。带主题名调用以获取完整文档。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `topic` | string | no | 要获取文档的主题。省略以列出可用主题。 |

### td_get_hints

获取某主题的 TouchDesigner 提示和常见模式。在创建算子或编写 TD Python 代码之前调用此工具，以学习正确的参数名、表达式和惯用方法。可用主题：animation、noise、connections、parameters、scripting、construction、ui_analysis、panel_layout、screenshots、input_simulation、undo。重要：在构建多算子设置之前，始终用 topic='construction' 调用以获取正确的 TOP/CHOP 参数名、compositeTOP 输入顺序和布局指南。重要：在使用 td_input_execute 之前，始终用 topic='input_simulation' 调用以学习焦点恢复、坐标系和测试工作流。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `topic` | string | yes | 要获取提示的主题。可用：'animation'、'noise'、'connections'、'parameters'、'scripting'、'construction'、'ui_analysis'、'panel_layout'、'screenshots'、'input_simulation'、'undo'、'networking'、'all' |

### td_agents_md

读取、写入或更新 COMP 容器内的 agents_md 文档。agents_md 是一个描述容器用途、结构和约定的 Markdown textDAT。action='read'：返回内容 + 过期检查（比较文档记录的子节点与实时状态）。action='update'：从实时状态刷新自动生成部分（子节点列表、连接），保留人工编写的部分。action='write'：设置完整内容，如果缺失则创建 DAT。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `path` | string | yes | COMP 容器的路径 |
| `action` | `read` / `update` / `write` | yes | read=获取内容+过期检查，update=刷新自动部分，write=设置内容 |
| `content` | string | no | Markdown 内容（仅用于 action='write'） |

## 输入自动化

### td_input_execute

向 TouchDesigner 发送一系列鼠标/键盘命令。命令按顺序执行，带平滑贝塞尔移动。立即返回 —— 轮询 td_input_status() 直到 status='idle' 再继续。命令类型：'focus' —— 将 TD 带到前台。'move' —— 平滑鼠标移动：{type,x,y,duration,easing}。'click' —— 点击：{type,x,y,button,hold,duration,easing}。hold=按住秒数。duration=点击前的平滑移动。'dblclick' —— 双击：{type,x,y,duration}。'mousedown'/'mouseup' —— {type,x,y,button}。'key' —— 按键：{type,keys} 例如 'ctrl+z'、'tab'、'escape'、'shift+f5'。Mac 上需要辅助功能权限。'type' —— 类人输入：{type,text,wpm,variance} —— 与布局无关的 Unicode，可变时序。'wait' —— 暂停：{type,duration}。'scroll' —— {type,x,y,dx,dy,steps} —— 类人滚动：先将鼠标移到 (x,y)，然后以自然时序分多 tick 发送 dy（垂直，+向上）和 dx（水平，+向右）。默认 steps=4。鼠标命令可包含 coord_space='logical'（默认）或 coord_space='physical'。在 macOS 上，'physical' 表示来自 td_get_screen_screenshot 的实际屏幕像素，并自动转换为 CGEvent 逻辑坐标。顶层 coord_space 适用于未覆盖它的命令。on_error：'stop'（默认）在出错时清空队列；'continue' 跳过失败的命令。重要：首次使用前调用 td_get_hints('input_simulation') 以学习焦点恢复、坐标系和测试工作流。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `commands` | array | yes | 要按顺序执行的命令字典列表。 |
| `coord_space` | `logical` / `physical` | no | 未指定自己 coord_space 的鼠标命令的默认坐标空间。'logical' 直接使用 CGEvent 坐标。'physical' 使用来自 td_get_screen_screenshot 的实际屏幕像素，并在 macOS 上自动转换。 |
| `on_error` | `stop` / `continue` | no | 出错时做什么。默认 'stop'。 |

### td_input_status

获取 td_input 命令队列的当前状态。在 td_input_execute 之后轮询此工具直到 status='idle'。返回：status（'idle'/'running'）、当前命令、queue_remaining、上一个错误。

无参数（除可选的 `target_instance` 外）。

### td_input_clear

清空 td_input 命令队列并立即停止当前执行。

无参数（除可选的 `target_instance` 外）。

### td_op_screen_rect

获取网络编辑器中算子节点的屏幕坐标。返回 {x,y,w,h,cx,cy}，其中 cx,cy 是点击用的中心。用它找到点击特定算子的位置。仅当算子的父网络当前在网络编辑器窗格中打开时有效。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `path` | string | yes | 算子的完整路径，例如 '/project1/myComp/noise1' |

### td_click_screen_point

解析先前 td_get_screen_screenshot 结果中的一个点并点击它。传入截图 request_id 以及归一化的 u/v 或 image_x/image_y。使用物理屏幕坐标排队一个 td_input 点击，因此它直接适用于从截图派生的点。用 duration/easing 控制点击前的光标移动。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `request_id` | string | yes | td_get_screen_screenshot 最初返回的请求 ID。 |
| `u` | number | no | 截图区域内的归一化水平位置（0=左，1=右）。与 v 配合使用。 |
| `v` | number | no | 截图区域内的归一化垂直位置（0=上，1=下）。与 u 配合使用。 |
| `image_x` | number | no | 返回截图图像内的水平像素坐标。与 image_y 配合使用。 |
| `image_y` | number | no | 返回截图图像内的垂直像素坐标。与 image_x 配合使用。 |
| `button` | `left` / `right` / `middle` | no | 要点击的鼠标按钮。默认 left。 |
| `hold` | number | no | 释放前按住鼠标按钮的秒数。 |
| `duration` | number | no | 点击前光标移动到目标的秒数。 |
| `easing` | `linear` / `ease-in` / `ease-out` / `ease-in-out` | no | 点击前移动的光标移动缓动。 |
| `focus` | boolean | no | 如果为 true，点击前将 TD 带到前台并短暂等待焦点稳定。 |

### td_screen_point_to_global

将先前 td_get_screen_screenshot 结果中的一个点转换为绝对屏幕坐标。传入截图 request_id 以及归一化的 u/v（在该截图区域内 0..1）或返回图像像素中的 image_x/image_y。返回绝对物理屏幕坐标、逻辑坐标以及可直接使用的 td_input_execute 载荷。元数据会保留最近屏幕截图，以便多个 agent 之后可通过 request_id 解析点。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `request_id` | string | yes | td_get_screen_screenshot 最初返回的请求 ID。 |
| `u` | number | no | 截图区域内的归一化水平位置（0=左，1=右）。与 v 配合使用。 |
| `v` | number | no | 截图区域内的归一化垂直位置（0=上，1=下）。与 u 配合使用。 |
| `image_x` | number | no | 返回截图图像内的水平像素坐标。与 image_y 配合使用。 |
| `image_y` | number | no | 返回截图图像内的垂直像素坐标。与 image_x 配合使用。 |

## 系统

### td_list_instances

列出所有运行中且有活动 MCP 服务器的 TouchDesigner (TD) 实例。返回每个实例的端口、项目名、PID 和 instanceId。在每次对话开始时调用此工具以发现可用实例并选择要使用的一个。instanceId 在 TD 进程生命周期内是稳定的，并用作所有其他工具调用中的 target_instance。

无参数（除可选的 `target_instance` 外）。

### td_project_quit

保存和/或关闭当前 TouchDesigner (TD) 项目。可在关闭前保存。报告项目是否有未保存的更改。要关闭其他实例，传递 target_instance=instanceId。警告：这将关闭该实例上的 MCP 服务器。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `save` | boolean | no | 关闭前保存项目。默认 true。 |
| `force` | boolean | no | 强制关闭而不弹保存对话框。默认 false。 |

### td_reinit_extension

在 TouchDesigner (TD) 中重新初始化 COMP 上的扩展。在通过 td_write_dat 完成所有代码编辑之后调用此工具以应用更改。不要在每次小编辑后调用 —— 先批量处理你的更改。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `path` | string | yes | 带扩展的 COMP 的路径 |

### td_dev_log

读取 MCP 开发日志的最后 N 条。仅在启用 Devmode 时可用。显示请求/响应历史。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `count` | integer | no | 要返回的最近日志条目数 |

### td_clear_dev_log

通过关闭旧文件并启动新文件来清空当前 MCP 开发日志。仅在启用 Devmode 时可用。

无参数（除可选的 `target_instance` 外）。

### td_test_session

管理测试会话、缺陷报告和对话导出。重要：不要主动建议导出聊天或提交报告。这些是针对特定情况的工具：- export_chat / submit_report：仅当用户遇到插件或 TouchDesigner 的 BUG 并想报告时，或当用户明确要求导出对话时。绝不要在会话结束时或作为例行操作建议此功能。用户短语 → 操作：'разбор тестовых сессий' / 'analyze test sessions' → 列出，然后拉取、读取 meta.json → index.jsonl → calls/。'разбор репортов' / 'analyze user reports' → 用 session='user' 列出，然后按名称拉取。'экспортируй чат' / 'export chat' → (1) export_chat_id → 标记，(2) 带 session=标记 的 export_chat。'сообщи о проблеме' / 'report bug' → 导出聊天、审查隐私，然后带 summary + tags + result_op=file_path 提交 report。操作：export_chat_id | export_chat | submit_report | start | note | import_chat | end | list | pull。list：默认=自动检测仓库。session='user' 用于 user_reports（仅限开发）。pull：自动搜索两个仓库。自动检测开发与用户 Hub 访问权限。

| 参数 | 类型 | 必需 | 说明 |
|-------|------|----------|-------------|
| `action` | `export_chat_id` / `export_chat` / `submit_report` / `start` / `note` / `import_chat` / `end` / `list` / `pull` | yes | 操作：export_chat_id / export_chat / submit_report / start / note / import_chat / end / list / pull |
| `prompt` | string | no | (start) 测试提示/任务描述 |
| `tags` | array | no | (start) 用于分类的标签，例如 ['ui', 'layout'] |
| `text` | string | no | (note) 观察文本。(import_chat) 完整对话文本。 |
| `outcome` | `success` / `partial` / `failure` | no | (end) 结果：success / partial / failure |
| `summary` | string | no | (end) 发生了什么的简短摘要 |
| `result_op` | string | no | (end) 要作为 result.tox 保存的算子路径 |
| `session` | string | no | (pull) 要下载的会话名称或子串 |
