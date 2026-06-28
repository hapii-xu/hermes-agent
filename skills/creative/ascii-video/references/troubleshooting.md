# 故障排查参考

> **另见：** composition.md · architecture.md · shaders.md · scenes.md · optimization.md

## 快速诊断

| 症状 | 可能原因 | 修复 |
|---------|-------------|-----|
| 全黑输出 | tonemap 伽马过高或无效果渲染 | 把伽马降到 0.5，检查 scene_fn 返回非零画布 |
| 洗白/过亮 | 用了线性亮度乘法而非 tonemap | 把 `canvas * N` 换成 `tonemap(canvas, gamma=0.75)` |
| ffmpeg 渲染中途挂起 | stderr=subprocess.PIPE 死锁 | 把 stderr 重定向到文件 |
| "read-only" 数组错误 | broadcast_to 视图未 .copy() | 在 broadcast_to 后加 `.copy()` |
| PicklingError | SCENES 表中有 lambda 或闭包 | 把所有 fx_* 定义在模块级 |
| 输出中随机黑洞 | 字体缺少 Unicode 字形 | 初始化时验证调色板 |
| 音视频不同步 | 帧时序累积 | 用整数帧计数器，每帧重新计算 t |
| 单色扁平输出 | 色相场形状不匹配 | 确保 h、s、v 数组在 hsv2rgb 前都是 (rows,cols) |
| 文本在繁忙背景上不可读 | 文本与背景无对比 | 用 `apply_text_backdrop()`（composition.md）+ `reverse_vignette` 着色器（shaders.md） |
| 文本乱码/镜像 | 对文本场景应用了万花筒或镜像着色器 | **绝不把万花筒、mirror_h/v/quad/diag 应用于有可读文本的场景** —— 径向折叠会摧毁可读性。只把它们应用于背景层或无文本场景 |

ASCII 视频开发中遇到的常见 bug、陷阱和平台专属问题。

## NumPy 广播

### `broadcast_to().copy()` 陷阱

色相场生成器经常返回广播视图 —— 它们有 `(1, cols)` 或 `(rows, 1)` 的形状，numpy 广播成 `(rows, cols)`。这些视图是**只读**的。如果任何下游代码尝试原地修改（例如 `h %= 1.0`），numpy 抛出：

```
ValueError: output array is read-only
```

**修复**：在 `broadcast_to()` 后总是 `.copy()`：

```python
h = np.broadcast_to(h, (g.rows, g.cols)).copy()
```

这在 `_render_vf()` 中尤为重要，色相数组在那里流经 `hsv2rgb()`。

### `+=` vs `+` 陷阱

当操作数形状不完全匹配时，原地操作符也会失败：

```python
# 若结果是 (rows,1) 而操作数是 (rows, cols) 则失败
val += np.sin(g.cc * 0.02 + t * 0.3) * 0.5

# 可行 —— 创建新数组
val = val + np.sin(g.cc * 0.02 + t * 0.3) * 0.5
```

`vf_plasma()` 函数曾有这个 bug。混合不同形状数组时用 `+` 而非 `+=`。

### `hsv2rgb()` 中的形状不匹配

`hsv2rgb(h, s, v)` 要求三个数组形状完全一致。若 `h` 是 `(1, cols)` 而 `s` 是 `(rows, cols)`，函数会崩溃或产生错误输出。

**修复**：确保所有输入在调用前都被广播并 copy 到 `(rows, cols)`。

---

## 混合模式陷阱

### Overlay 压碎暗输入

`overlay(a, b) = 2*a*b`（当 `a < 0.5` 时）。两个 0.12 的值产生 `2 * 0.12 * 0.12 = 0.03`。结果比任一输入都暗。

**影响**：若两层都暗（ASCII 艺术通常如此），overlay 产生接近全黑的输出。

**修复**：暗源素材用 `screen`。Screen 总是变亮：`1 - (1-a)*(1-b)`。

### Colordodge 除零

`colordodge(a, b) = a / (1 - b)`。当 `b = 1.0`（纯白像素）时除以零。

**修复**：加 epsilon：`a / (1 - b + 1e-6)`。`BLEND_MODES` 中的实现应包含此项。

### Colorburn 除零

`colorburn(a, b) = 1 - (1-a) / b`。当 `b = 0`（纯黑像素）时除以零。

**修复**：加 epsilon：`1 - (1-a) / (b + 1e-6)`。

### Multiply 总是变暗

`multiply(a, b) = a * b`。由于两个操作数都在 [0,1]，结果总是 <= min(a,b)。绝不用 multiply 作为反馈混合模式 —— 几帧内画面就全黑。

**修复**：反馈用 `screen`，或低不透明度的 `add`。

---

## 多进程

### Pickle 约束

`ProcessPoolExecutor` 通过 pickle 序列化函数参数。这约束了你能传给 worker 的内容：

| 可 pickle | 不可 pickle |
|-----------|---------------|
| 模块级函数（`def fx_foo():`） | lambda（`lambda x: x + 1`） |
| 字典、列表、numpy 数组 | 闭包（函数内定义的函数） |
| 类实例（带 `__reduce__`） | 实例方法 |
| 字符串、数字 | 文件句柄、套接字 |

**影响**：SCENES 表中引用的所有场景函数必须用 `def` 定义在模块级。若用 lambda 或闭包，会得到：

```
_pickle.PicklingError: Can't pickle <function <lambda> at 0x...>
```

**修复**：把所有场景函数定义在模块顶层。在 `_render_vf()` 内作为 val_fn/hue_fn 使用的 lambda 没问题，因为它们在 worker 进程内执行 —— 不会跨进程边界被 pickle。

### macOS spawn vs Linux fork

在 macOS 上，`multiprocessing` 默认用 `spawn`（完整序列化）。在 Linux 上默认用 `fork`（写时复制）。这意味着：

- **macOS**：特征数组按 worker 序列化（30s 视频约 57KB，但随时长增长）。每个 worker 重新导入整个模块。
- **Linux**：特征数组通过 COW 共享。worker 继承父进程内存。

**影响**：在 macOS 上，模块级代码（如 `detect_hardware()`）在每个 worker 进程中运行。若它有副作用（如子进程调用），那些会发生 N+1 次。

### 每 worker 状态隔离

每个 worker 创建自己的：
- `Renderer` 实例（带全新网格缓存）
- `FeedbackBuffer`（反馈不跨场景边界）
- 随机种子（`random.seed(hash(seg_id) + 42)`）

这意味着：
- 粒子状态不在场景间延续（符合预期）
- 反馈尾迹在场景切换时重置（符合预期）
- `np.random` 状态**不会**被 `random.seed()` 播种 —— 它们用独立的 RNG

**确定性噪声的修复**：显式用 `np.random.RandomState(seed)`：

```python
rng = np.random.RandomState(hash(seg_id) + 42)
noise = rng.random((rows, cols))
```

---

## 亮度问题

### Tonemap 后场景仍暗

若场景在 tonemap 后仍暗，检查：

1. **伽马过高**：对有破坏性后处理的场景降低伽马（0.5-0.6）
2. **着色器摧毁亮度**：着色器链中的日晒、色调分离或对比度调整会抵消 tonemap 的工作。把破坏性着色器移到链的更早位置，或增大伽马补偿。
3. **带 multiply 的反馈**：multiply 反馈每帧变暗。改用 screen 或 add。
4. **场景中的 overlay 混合**：若场景函数对暗层用 `blend_canvas(..., "overlay", ...)`，改用 screen。

### 诊断：测试帧亮度

```bash
python reel.py --test-frame 10.0
# 输出：Mean brightness: 44.3, max: 255
```

若均值 < 20，场景需要关注。常见修复：
- 在 SCENES 条目中降低伽马
- 把内部混合模式从 overlay/multiply 改为 screen/add
- 增大亮度场乘数（例如 `vf_plasma(...) * 1.5`）
- 检查着色器链没有激进的日晒或阈值

### v1 亮度模式（已弃用）

旧模式用线性乘法：

```python
# 旧 —— 不要用
canvas = np.clip(canvas.astype(np.float32) * 2.0, 0, 255).astype(np.uint8)
```

这会失败，因为：
- 暗场景（均值 8）：`8 * 2.0 = 16` —— 仍暗
- 亮场景（均值 130）：`130 * 2.0 = 255` —— 裁剪，丢失细节

改用 `tonemap()`。见 `composition.md` § Adaptive Tone Mapping。

---

## ffmpeg 问题

### 管道死锁

头号生产 bug。若你用 `stderr=subprocess.PIPE`：

```python
# 死锁 —— stderr 缓冲在 64KB 填满，阻塞 ffmpeg，阻塞你的写入
pipe = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
```

**修复**：总是把 stderr 重定向到文件：

```python
stderr_fh = open(err_path, "w")
pipe = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL, stderr=stderr_fh)
```

### 帧数不匹配

若写入管道的帧数与 ffmpeg 期望的（基于 `-r` 和时长）不符，输出可能有：
- 末尾缺帧
- 时长不正确
- 音视频不同步

**修复**：显式计算帧数：`n_frames = int(duration * FPS)`。不要在没有验证总数匹配的情况下用 `range(int(start*FPS), int(end*FPS))`。

### concat 因 "unsafe file name" 失败

```
[concat @ ...] Unsafe file name
```

**修复**：总是用 `-safe 0`：
```python
["ffmpeg", "-f", "concat", "-safe", "0", "-i", concat_path, ...]
```

---

## 字体问题

### 单元高度（macOS Pillow）

`textbbox()` 和 `getbbox()` 在某些 macOS Pillow 版本上返回错误高度。用 `getmetrics()`：

```python
ascent, descent = font.getmetrics()
cell_height = ascent + descent  # 正确
# 不是：font.getbbox("M")[3]  # 某些版本上错误
```

### 缺少 Unicode 字形

并非所有字体都能渲染所有 Unicode 字符。若调色板字符不在字体中，字形渲染为空白或豆腐块，在输出中表现为黑洞。

**修复**：初始化时验证：

```python
all_chars = set()
for pal in [PAL_DEFAULT, PAL_DENSE, PAL_RUNE, ...]:
    all_chars.update(pal)

valid_chars = set()
for c in all_chars:
    if c == " ":
        valid_chars.add(c)
        continue
    img = Image.new("L", (20, 20), 0)
    ImageDraw.Draw(img).text((0, 0), c, fill=255, font=font)
    if np.array(img).max() > 0:
        valid_chars.add(c)
    else:
        log(f"WARNING: '{c}' (U+{ord(c):04X}) missing from font")
```

### 平台字体路径

| 平台 | 常见路径 |
|----------|-------------|
| macOS | `/System/Library/Fonts/Menlo.ttc`、`/System/Library/Fonts/Monaco.ttf` |
| Linux | `/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf` |
| Windows | `C:\Windows\Fonts\consola.ttf`（Consolas） |

总是探测多个路径并优雅回退。见 `architecture.md` § Font Selection。

---

## 性能

### 慢着色器

某些着色器用 Python 循环，在 1080p 下非常慢：

| 着色器 | 问题 | 修复 |
|--------|-------|-----|
| `wave_distort` | 每行 Python 循环 | 用向量化的花式索引 |
| `halftone` | 三重嵌套循环 | 用块归约向量化 |
| `matrix rain` | 每列每尾迹循环 | 累积索引数组，批量赋值 |

### 渲染时间扩展

若渲染比预期慢得多：
1. 检查网格数 —— 每个额外网格初始化加约 100-150ms/帧
2. 检查粒子数 —— 上限设为质量合适的值
3. 检查着色器数 —— 每个着色器加 2-25ms
4. 检查效果中是否有意外的 Python 循环（应只用 numpy）

---

## 常见错误

### 用 `r.S` vs `S` 参数

v2 场景协议把 `S`（状态字典）作为显式参数传入。但 `S` 就是 `r.S` —— 它们是同一对象。两者都行：

```python
def fx_scene(r, f, t, S):
    S["counter"] = S.get("counter", 0) + 1   # 经参数（推荐）
    r.S["counter"] = r.S.get("counter", 0) + 1  # 经 renderer（也可行）
```

为清晰起见用 `S` 参数。显式参数让函数有持久状态这一事实一目了然。

### 忘记处理空特征值

音频静默时特征默认为 0.0。用带合理默认值的 `.get()`：

```python
energy = f.get("bass", 0.3)  # 默认 0.3，而非 0
```

若默认 0，静默期间效果会变空白。

### 写新文件而非编辑现有状态

粒子系统中的常见 bug：每帧创建新数组而非更新持久状态。

```python
# 错误 —— 粒子每帧重置
S["px"] = []
for _ in range(100):
    S["px"].append(random.random())

# 正确 —— 只初始化一次，每帧更新
if "px" not in S:
    S["px"] = []
# ... 基于节拍发射新粒子
# ... 更新现有粒子
```

### 不裁剪亮度场

亮度场应在 [0, 1]。若超出此范围，`val2char()` 产生索引错误：

```python
# 错误 —— vf_plasma() * 1.5 可能超过 1.0
val = vf_plasma(g, f, t, S) * 1.5

# 正确 —— 缩放后裁剪
val = np.clip(vf_plasma(g, f, t, S) * 1.5, 0, 1)
```

`_render_vf()` helper 会自动裁剪，但若你构建自定义场景，请显式裁剪。

## 亮度最佳实践

- 密集动画背景 —— 绝不扁平纯黑，总是填满网格
- 暗角最小值钳制到 0.15（而非 0.12）
- 泛光阈值 130（而非 170），让更多像素贡献发光
- 暗 ASCII 层用 `screen` 混合模式（而非 `overlay`）—— overlay 对暗值取平方：`2 * 0.12 * 0.12 = 0.03`
- FeedbackBuffer 衰减最小 0.5 —— 低于此反馈消失太快而看不见
- 亮度场地板：`vf * 0.8 + 0.05` 确保没有单元真正为零
- 每场景伽马覆盖：默认 0.75、日晒 0.55、色调分离 0.50、明亮场景 0.85
- 尽早测试帧：在承诺完整渲染前渲染关键时间戳的单帧

**完整渲染前快速清单：**
1. 渲染 3 个测试帧（开头、中间、结尾）
2. 检查 tonemap 后 `canvas.mean() > 8`
3. 检查没有场景视觉上扁平全黑
4. 验证每小节变化（每场景不同的背景/调色板/颜色）
5. 确认着色器链包含泛光（阈值 130）
6. 确认暗角强度 ≤ 0.25
