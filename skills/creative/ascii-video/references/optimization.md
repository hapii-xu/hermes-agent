# 优化参考

> **另请参阅：** architecture.md · composition.md · scenes.md · shaders.md · inputs.md · troubleshooting.md

## 硬件检测

在脚本启动时检测用户硬件并自动适配渲染参数。切勿硬编码 worker 数量或分辨率。

### CPU 和内存检测

```python
import multiprocessing
import platform
import shutil
import os

def detect_hardware():
    """检测硬件能力并返回渲染配置。"""
    cpu_count = multiprocessing.cpu_count()

    # 为 OS + ffmpeg 编码留出 1-2 个核心
    if cpu_count >= 16:
        workers = cpu_count - 2
    elif cpu_count >= 8:
        workers = cpu_count - 1
    elif cpu_count >= 4:
        workers = cpu_count - 1
    else:
        workers = max(1, cpu_count)

    # 内存检测（平台相关）
    try:
        if platform.system() == "Darwin":
            import subprocess
            mem_bytes = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip())
        elif platform.system() == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal"):
                        mem_bytes = int(line.split()[1]) * 1024
                        break
        else:
            mem_bytes = 8 * 1024**3  # 未知平台假设 8GB
    except Exception:
        mem_bytes = 8 * 1024**3

    mem_gb = mem_bytes / (1024**3)

    # 每个 worker 视网格大小使用约 50-150MB
    # 内存紧张时限制 worker 数
    mem_per_worker_mb = 150
    max_workers_by_mem = int(mem_gb * 1024 * 0.6 / mem_per_worker_mb)  # 使用 60% 的 RAM
    workers = min(workers, max_workers_by_mem)

    # ffmpeg 可用性和编解码器支持
    has_ffmpeg = shutil.which("ffmpeg") is not None

    return {
        "cpu_count": cpu_count,
        "workers": workers,
        "mem_gb": mem_gb,
        "platform": platform.system(),
        "arch": platform.machine(),
        "has_ffmpeg": has_ffmpeg,
    }
```

### 自适应质量配置

基于硬件缩放分辨率、FPS、CRF 和网格密度：

```python
def quality_profile(hw, target_duration_s, user_preference="auto"):
    """
    返回适配硬件的渲染设置。
    user_preference: "auto", "draft", "preview", "production", "max"
    """
    if user_preference == "draft":
        return {"vw": 960, "vh": 540, "fps": 12, "crf": 28, "workers": min(4, hw["workers"]),
                "grid_scale": 0.5, "shaders": "minimal", "particles_max": 200}

    if user_preference == "preview":
        return {"vw": 1280, "vh": 720, "fps": 15, "crf": 25, "workers": hw["workers"],
                "grid_scale": 0.75, "shaders": "standard", "particles_max": 500}

    if user_preference == "max":
        return {"vw": 3840, "vh": 2160, "fps": 30, "crf": 15, "workers": hw["workers"],
                "grid_scale": 2.0, "shaders": "full", "particles_max": 3000}

    # "production" 或 "auto"
    # 自动检测：估算渲染时间，若太久则降级
    n_frames = int(target_duration_s * 24)
    est_seconds_per_frame = 0.18  # 1080p 下约 180ms
    est_total_s = n_frames * est_seconds_per_frame / max(1, hw["workers"])

    if hw["mem_gb"] < 4 or hw["cpu_count"] <= 2:
        # 低端：720p，15fps
        return {"vw": 1280, "vh": 720, "fps": 15, "crf": 23, "workers": hw["workers"],
                "grid_scale": 0.75, "shaders": "standard", "particles_max": 500}

    if est_total_s > 3600:  # 会超过一小时
        # 降级到 720p 以加速
        return {"vw": 1280, "vh": 720, "fps": 24, "crf": 20, "workers": hw["workers"],
                "grid_scale": 0.75, "shaders": "standard", "particles_max": 800}

    # 标准生产：1080p 24fps
    return {"vw": 1920, "vh": 1080, "fps": 24, "crf": 20, "workers": hw["workers"],
            "grid_scale": 1.0, "shaders": "full", "particles_max": 1200}


def apply_quality_profile(profile):
    """从质量配置设置全局变量。"""
    global VW, VH, FPS, N_WORKERS
    VW = profile["vw"]
    VH = profile["vh"]
    FPS = profile["fps"]
    N_WORKERS = profile["workers"]
    # 网格尺寸随分辨率缩放
    # CRF 传给 ffmpeg 编码器
    # 着色器集决定哪些后期处理处于活动状态
```

### CLI 集成

```python
parser = argparse.ArgumentParser()
parser.add_argument("--quality", choices=["draft", "preview", "production", "max", "auto"],
                    default="auto", help="渲染质量预设")
parser.add_argument("--aspect", choices=["landscape", "portrait", "square"],
                    default="landscape", help="宽高比预设")
parser.add_argument("--workers", type=int, default=0, help="覆盖 worker 数量 (0=自动)")
parser.add_argument("--resolution", type=str, default="", help="覆盖分辨率，例如 1280x720")
args = parser.parse_args()

hw = detect_hardware()
if args.workers > 0:
    hw["workers"] = args.workers
profile = quality_profile(hw, target_duration, args.quality)

# 应用宽高比预设（在手动分辨率覆盖之前）
ASPECT_PRESETS = {
    "landscape": (1920, 1080),
    "portrait":  (1080, 1920),
    "square":    (1080, 1080),
}
if args.aspect != "landscape" and not args.resolution:
    profile["vw"], profile["vh"] = ASPECT_PRESETS[args.aspect]

if args.resolution:
    w, h = args.resolution.split("x")
    profile["vw"], profile["vh"] = int(w), int(h)
apply_quality_profile(profile)

log(f"硬件：{hw['cpu_count']} 核心，{hw['mem_gb']:.1f}GB 内存，{hw['platform']}")
log(f"渲染：  {profile['vw']}x{profile['vh']} @{profile['fps']}fps，"
    f"CRF {profile['crf']}，{profile['workers']} 个 worker")
```

### 竖屏模式注意事项

竖屏 (1080x1920) 与横屏 1080p 像素数相同，因此性能相当。但构图模式不同：

| 关注点 | 横屏 | 竖屏 |
|---------|-----------|----------|
| `lg` 时网格列数 | 160 | 90 |
| `lg` 时网格行数 | 45 | 80 |
| 文本行最大字符数 | 居中约 50 | 居中约 25-30 |
| 垂直雨 | 短行程 | 长、戏剧化的行程 |
| 水平频谱 | 全宽 | 需旋转或压缩 |
| 径向效果 | 自然圆形 | 高椭圆（长宽比修正可处理） |
| 粒子爆炸 | 宽散布 | 高散布 |
| 文本堆叠 | 3-4 行舒适 | 8-10 行舒适 |
| 引语布局 | 2-3 条宽行 | 5-6 条短行 |

**竖屏优化模式：**
- 垂直雨/矩阵效果天然增强 —— 更长的列行程
- 火焰柱穿过更多屏幕空间上升
- 上升余烬/粒子有更多垂直跑道
- 文本可更激进地堆叠更多行
- 径向效果若应用长宽比修正则有效（GridLayer 自动处理）
- 频谱条可旋转 90 度（从底部向上的垂直条）

**竖屏文本布局：**
```python
def layout_text_portrait(text, max_chars_per_line=25, grid=None):
    """将文本拆成短行以适应竖屏显示。"""
    words = text.split()
    lines = []; current = ""
    for w in words:
        if len(current) + len(w) + 1 > max_chars_per_line:
            lines.append(current.strip())
            current = w + " "
        else:
            current += w + " "
    if current.strip():
        lines.append(current.strip())
    return lines
```

## 性能预算

目标：每画布帧 100-200ms（单线程 5-10 fps，8 个 worker 下 40-80 fps）。

| 组件 | 时间 | 说明 |
|-----------|------|-------|
| 特征提取 | 1-5ms | 渲染前为所有画布帧预计算 |
| 效果函数 | 2-15ms | 向量化 numpy，避免 Python 循环 |
| 字符渲染 | 80-150ms | **瓶颈** —— 逐单元 Python 循环 |
| 着色器流水线 | 5-25ms | 取决于活动的着色器 |
| ffmpeg 编码 | ~5ms | 由管道缓冲分摊 |

## 位图预栅格化

在初始化时栅格化每个字符，而非每画布帧：

```python
# 初始化时 —— 仅执行一次
for c in all_characters:
    img = Image.new("L", (cell_w, cell_h), 0)
    ImageDraw.Draw(img).text((0, 0), c, fill=255, font=font)
    bitmaps[c] = np.array(img, dtype=np.float32) / 255.0  # float32 用于快速乘法

# 渲染时 —— 快速查找
bitmap = bitmaps[char]
canvas[y:y+ch, x:x+cw] = np.maximum(canvas[y:y+ch, x:x+cw],
                                      (bitmap[:,:,None] * color).astype(np.uint8))
```

将所有调色板中的字符 + 叠加文本收集到初始化集中。对任何遗漏字符惰性初始化。

## 预渲染背景纹理

`_render_vf()` 的替代方案，适用于字符无需每画布帧变化的背景。在初始化时预烘焙一个静态 ASCII 纹理，然后每画布帧乘以一个逐单元颜色场。一次矩阵乘法对比数千次位图块拷贝。

适用场景：背景层使用固定字符调色板，且每画布帧仅颜色/亮度变化。不适用于字符选择依赖于变化值场的图层。

### 初始化：烘焙纹理

```python
# 在 GridLayer.__init__ 中：
self._bg_row_idx = np.clip(
    (np.arange(VH) - self.oy) // self.ch, 0, self.rows - 1
)
self._bg_col_idx = np.clip(
    (np.arange(VW) - self.ox) // self.cw, 0, self.cols - 1
)
self._bg_textures = {}

def make_bg_texture(self, palette):
    """一次性预渲染静态 ASCII 纹理（灰度 float32）。"""
    if palette not in self._bg_textures:
        texture = np.zeros((VH, VW), dtype=np.float32)
        rng = random.Random(12345)
        ch_list = [c for c in palette if c != " " and c in self.bm]
        if not ch_list:
            ch_list = list(self.bm.keys())[:5]
        for row in range(self.rows):
            y = self.oy + row * self.ch
            if y + self.ch > VH:
                break
            for col in range(self.cols):
                x = self.ox + col * self.cw
                if x + self.cw > VW:
                    break
                bm = self.bm[rng.choice(ch_list)]
                texture[y:y+self.ch, x:x+self.cw] = bm
        self._bg_textures[palette] = texture
    return self._bg_textures[palette]
```

### 渲染：颜色场 x 缓存纹理

```python
def render_bg(self, color_field, palette=PAL_CIRCUIT):
    """快速背景：预渲染的 ASCII 纹理 * 逐单元颜色场。
    color_field: (rows, cols, 3) uint8。返回 (VH, VW, 3) uint8。"""
    texture = self.make_bg_texture(palette)
    # 通过预计算的索引映射将单元颜色扩展到像素坐标
    color_px = color_field[
        self._bg_row_idx[:, None], self._bg_col_idx[None, :]
    ].astype(np.float32)
    return (texture[:, :, None] * color_px).astype(np.uint8)
```

### 在场景中的使用

```python
# 从效果场构建逐单元颜色（廉价 —— rows*cols，而非 VH*VW）
hue = ((t * 0.05 + val * 0.2) % 1.0).astype(np.float32)
R, G, B = hsv2rgb(hue, np.full_like(val, 0.5), val)
color_field = mkc(R, G, B, g.rows, g.cols)  # (rows, cols, 3) uint8

# 渲染背景 —— 单次矩阵乘法，无逐单元循环
canvas_bg = g.render_bg(color_field, PAL_DENSE)
```

纹理初始化循环运行一次并按调色板缓存。每画布帧开销为一次花式索引查找 + 一次广播乘法 —— 对于密集背景，比 `render()` 中的逐单元位图块拷贝循环快几个数量级。

## 坐标数组缓存

在初始化时预计算所有网格相对坐标数组，而非每画布帧：

```python
# 这些是 O(rows*cols) 且在每个效果中使用
self.rr = np.arange(rows)[:, None]    # 行索引
self.cc = np.arange(cols)[None, :]    # 列索引
self.dist = np.sqrt(dx**2 + dy**2)   # 距中心距离
self.angle = np.arctan2(dy, dx)       # 距中心角度
self.dist_n = ...                      # 归一化距离
```

## 向量化效果模式

### 避免效果中的逐单元 Python 循环

渲染循环（合成位图）不可避免地是逐单元的。但效果函数必须是完全向量化的 numpy —— 永远不要在 Python 中遍历行/列。

差（O(rows*cols) Python 循环）：
```python
for r in range(rows):
    for c in range(cols):
        val[r, c] = math.sin(c * 0.1 + t) * math.cos(r * 0.1 - t)
```

好（向量化）：
```python
val = np.sin(g.cc * 0.1 + t) * np.cos(g.rr * 0.1 - t)
```

### 向量化矩阵雨

朴素的逐列逐拖尾像素循环是仅次于渲染循环的第二大瓶颈。使用 numpy 花式索引：

```python
# 而非对列和拖尾像素的嵌套 Python 循环：
# 一次性为所有活动拖尾像素构建行索引数组
all_rows = []
all_cols = []
all_fades = []
for c in range(cols):
    head = int(S["ry"][c])
    trail_len = S["rln"][c]
    for i in range(trail_len):
        row = head - i
        if 0 <= row < rows:
            all_rows.append(row)
            all_cols.append(c)
            all_fades.append(1.0 - i / trail_len)

# 向量化赋值
ar = np.array(all_rows)
ac = np.array(all_cols)
af = np.array(all_fades, dtype=np.float32)
# 使用花式索引批量赋值字符和颜色
ch[ar, ac] = ...  # 向量化字符赋值
co[ar, ac, 1] = (af * bri * 255).astype(np.uint8)  # 绿色通道
```

### 向量化火焰柱

同样模式 —— 累积索引数组，批量赋值：

```python
fire_val = np.zeros((rows, cols), dtype=np.float32)
for fi in range(n_cols):
    fx_c = int((fi * cols / n_cols + np.sin(t * 2 + fi * 0.7) * 3) % cols)
    height = int(energy * rows * 0.7)
    dy = np.arange(min(height, rows))
    fr = rows - 1 - dy
    frac = dy / max(height, 1)
    # 宽度散布：底部的基础列更宽
    for dx in range(-1, 2):  # 3 宽列
        c = fx_c + dx
        if 0 <= c < cols:
            fire_val[fr, c] = np.maximum(fire_val[fr, c],
                                          (1 - frac * 0.6) * (0.5 + rms * 0.5))
# 现在在一次向量化遍历中将 fire_val 映射到字符和颜色
```

## PIL 字符串渲染（用于文本密集场景）

渲染许多长文本字符串（滚动行情、打字机序列、灵感涌现）时，逐单元位图块拷贝的替代方案。使用 PIL 原生的 `ImageDraw.text()`，它在一次 C 调用中渲染整个字符串，对比每个字符一次 Python 循环位图块拷贝。

典型收益：一个含 56 行行情的场景渲染 56 次 PIL `text()` 调用，而非约 1 万次单独位图块拷贝。

适用场景：场景渲染许多可读文本字符串行。不适用于稀疏或空间分散的单个字符（那些用普通 `render()`）。

```python
from PIL import Image, ImageDraw

def render_text_layer(grid, rows_data, font):
    """通过 PIL 渲染密集文本行，而非逐单元位图块拷贝。

    Args:
        grid: GridLayer 实例（用于 oy, ch, ox, 字体度量）
        rows_data: (row_index, text_string, rgb_tuple) 列表 —— 每行一个
        font: PIL ImageFont 实例 (grid.font)

    Returns:
        uint8 数组 (VH, VW, 3) —— 含渲染文本的画布
    """
    img = Image.new("RGB", (VW, VH), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    for row_idx, text, color in rows_data:
        y = grid.oy + row_idx * grid.ch
        if y + grid.ch > VH:
            break
        draw.text((grid.ox, y), text, fill=color, font=font)
    return np.array(img)
```

### 在行情场景中的使用

```python
# 构建行情数据（每行的文本 + 颜色）
rows_data = []
for row in range(n_tickers):
    text = build_ticker_text(row, t)       # 滚动子串
    color = hsv2rgb_scalar(hue, 0.85, bri) # (R, G, B) 元组
    rows_data.append((row, text, color))

# 一次 PIL 遍历而非数千次位图块拷贝
canvas_tickers = render_text_layer(g_md, rows_data, g_md.font)

# 正常与其他图层混合
result = blend_canvas(canvas_bg, canvas_tickers, "screen", 0.9)
```

这纯粹是渲染优化 —— 相同视觉输出，更少绘制调用。网格的 `render()` 方法对于字符基于值场单独放置的稀疏字符场仍然需要。

## Bloom 优化

**不要使用 `scipy.ndimage.uniform_filter`** —— 实测 424ms/画布帧。

改用 4 倍下采样 + 手动盒式模糊 —— 84ms/画布帧（快 5 倍）：

```python
sm = canvas[::4, ::4].astype(np.float32)  # 4 倍下采样
br = np.where(sm > threshold, sm, 0)
for _ in range(3):                          # 3 趟手动盒式模糊
    p = np.pad(br, ((1,1),(1,1),(0,0)), mode='edge')
    br = (p[:-2,:-2] + p[:-2,1:-1] + p[:-2,2:] +
          p[1:-1,:-2] + p[1:-1,1:-1] + p[1:-1,2:] +
          p[2:,:-2] + p[2:,1:-1] + p[2:,2:]) / 9.0
bl = np.repeat(np.repeat(br, 4, axis=0), 4, axis=1)[:H, :W]
```

## 暗角缓存

距离场依赖分辨率和强度，永不在每画布帧变化：

```python
_vig_cache = {}
def sh_vignette(canvas, strength):
    key = (canvas.shape[0], canvas.shape[1], round(strength, 2))
    if key not in _vig_cache:
        Y = np.linspace(-1, 1, H)[:, None]
        X = np.linspace(-1, 1, W)[None, :]
        _vig_cache[key] = np.clip(1.0 - np.sqrt(X**2+Y**2) * strength, 0.15, 1).astype(np.float32)
    return np.clip(canvas * _vig_cache[key][:,:,None], 0, 255).astype(np.uint8)
```

CRT 桶形畸变（缓存重映射坐标）使用相同模式。

## 胶片颗粒优化

以半分辨率生成噪声，再平铺放大：

```python
noise = np.random.randint(-amt, amt+1, (H//2, W//2, 1), dtype=np.int16)
noise = np.repeat(np.repeat(noise, 2, axis=0), 2, axis=1)[:H, :W]
```

2 倍块状颗粒看起来像胶片颗粒，且随机生成开销仅为 1/4。

## 并行渲染

### Worker 架构

```python
hw = detect_hardware()
N_WORKERS = hw["workers"]

# 批次拆分（用于非分段架构）
batch_size = (n_frames + N_WORKERS - 1) // N_WORKERS
batches = [(i, i*batch_size, min((i+1)*batch_size, n_frames), features, seg_path) ...]

with multiprocessing.Pool(N_WORKERS) as pool:
    segments = pool.starmap(render_batch, batches)
```

### 逐片段并行（分段视频首选）

```python
from concurrent.futures import ProcessPoolExecutor, as_completed

with ProcessPoolExecutor(max_workers=N_WORKERS) as pool:
    futures = {pool.submit(render_clip, seg, features, path): seg["id"]
               for seg, path in clip_args}
    for fut in as_completed(futures):
        clip_id = futures[fut]
        try:
            fut.result()
            log(f"  {clip_id} 完成")
        except Exception as e:
            log(f"  {clip_id} 失败：{e}")
```

### Worker 隔离

每个 worker：
- 创建自己的 `Renderer` 实例（含完整网格 + 位图初始化）
- 打开自己的 ffmpeg 子进程
- 拥有独立随机种子（`random.seed(batch_id * 10000)`）
- 写入自己的分段文件和 stderr 日志

### ffmpeg 管道安全

**关键**：长时间运行的 ffmpeg 永远不要 `stderr=subprocess.PIPE`。stderr 缓冲区在约 64KB 时填满并死锁：

```python
# 错误 —— 会死锁
pipe = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

# 正确 —— stderr 输出到文件
stderr_fh = open(err_path, "w")
pipe = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=stderr_fh)
# ... 写入所有画布帧 ...
pipe.stdin.close()
pipe.wait()
stderr_fh.close()
```

### 拼接

```python
with open(concat_file, "w") as cf:
    for seg in segments:
        cf.write(f"file '{seg}'\n")

cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file]
if audio_path:
    cmd += ["-i", audio_path, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest"]
else:
    cmd += ["-c:v", "copy"]
cmd.append(output_path)
subprocess.run(cmd, capture_output=True, check=True)
```

## 粒子系统性能

根据质量配置限制粒子数量：

| 系统 | 低 | 标准 | 高 |
|--------|-----|----------|------|
| 爆炸 | 300 | 1000 | 2500 |
| 余烬 | 500 | 1500 | 3000 |
| 星空 | 300 | 800 | 1500 |
| 溶解 | 200 | 600 | 1200 |

通过截断列表来剔除：
```python
MAX_PARTICLES = profile.get("particles_max", 1200)
if len(S["px"]) > MAX_PARTICLES:
    for k in ("px", "py", "vx", "vy", "life", "char"):
        S[k] = S[k][-MAX_PARTICLES:]  # 保留最新的
```

## 内存管理

- 特征数组：为所有画布帧预计算，通过 fork 语义（COW）在 worker 间共享
- 画布：每个 worker 分配一次，复用（`np.zeros(...)`）
- 字符数组：每画布帧分配（廉价 —— rows*cols U1 字符串）
- 位图缓存：每个网格尺寸约 500KB，每个 worker 初始化一次

每个 worker 总内存：约 50-150MB。8 个 worker 总计：约 400-800MB。

对于低内存系统（< 4GB），减少 worker 数量并使用更小网格。

## 亮度验证

渲染后，在采样时间戳处抽查亮度：

```python
for t in [2, 30, 60, 120, 180]:
    cmd = ["ffmpeg", "-ss", str(t), "-i", output_path,
           "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    r = subprocess.run(cmd, capture_output=True)
    arr = np.frombuffer(r.stdout, dtype=np.uint8)
    print(f"t={t}s  平均={arr.mean():.1f}  最大={arr.max()}")
```

目标：安静段平均 > 5，活跃段平均 > 15。若持续低于此，提高效果中的亮度下限和/或全局提升乘数。

## 渲染时间估算

随硬件缩放。基线：1080p，24fps，约 180ms/画布帧/worker。

| 时长 | 画布帧数 | 4 个 worker | 8 个 worker | 16 个 worker |
|----------|--------|-----------|-----------|------------|
| 30s | 720 | ~3 分钟 | ~2 分钟 | ~1 分钟 |
| 2 分钟 | 2,880 | ~13 分钟 | ~7 分钟 | ~4 分钟 |
| 3.5 分钟 | 5,040 | ~23 分钟 | ~12 分钟 | ~6 分钟 |
| 5 分钟 | 7,200 | ~33 分钟 | ~17 分钟 | ~9 分钟 |
| 10 分钟 | 14,400 | ~65 分钟 | ~33 分钟 | ~17 分钟 |

720p 下：时间乘以约 0.5。4K 下：乘以约 4。

更重的效果（多粒子、密集网格、额外着色器遍历）增加约 20-50%。

---

## 临时文件清理

渲染会产生跨次运行累积的中间文件。在最终拼接/混合步骤之后清理。

### 待清理文件

| 文件类型 | 来源 | 位置 |
|-----------|--------|----------|
| WAV 提取 | `ffmpeg -i input.mp3 ... tmp.wav` | `tempfile.mktemp()` 或项目目录 |
| 分段片段 | `render_clip()` 输出 | `segments/seg_00.mp4` 等 |
| 拼接列表 | ffmpeg concat demuxer 输入 | `segments/concat.txt` |
| ffmpeg stderr 日志 | 管道输出到文件用于调试 | 项目目录中的 `*.log` |
| 特征缓存 | pickle 的 numpy 数组 | `*.pkl` 或 `*.npz` |

### 清理函数

```python
import glob
import tempfile
import shutil

def cleanup_render_artifacts(segments_dir="segments", keep_final=True):
    """成功渲染后移除中间文件。

    在验证最终输出存在且能正常播放后调用。

    Args:
        segments_dir: 包含分段片段和拼接列表的目录
        keep_final: 若为 True，仅删除中间产物（不删最终输出）
    """
    removed = []

    # 1. 分段片段
    if os.path.isdir(segments_dir):
        shutil.rmtree(segments_dir)
        removed.append(f"目录: {segments_dir}")

    # 2. 临时 WAV 文件
    for wav in glob.glob("*.wav"):
        if wav.startswith("tmp") or wav.startswith("extracted_"):
            os.remove(wav)
            removed.append(wav)

    # 3. ffmpeg stderr 日志
    for log in glob.glob("ffmpeg_*.log"):
        os.remove(log)
        removed.append(log)

    # 4. 特征缓存（可选 —— 保留便于重渲染很有用）
    # for cache in glob.glob("features_*.npz"):
    #     os.remove(cache)
    #     removed.append(cache)

    print(f"已清理 {len(removed)} 个产物: {removed}")
    return removed
```

### 与渲染流水线集成

在主渲染脚本末尾、验证最终输出后调用清理：

```python
# 在 main() 末尾
if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
    cleanup_render_artifacts(segments_dir="segments")
    print(f"完成。输出：{output_path}")
else:
    print("警告：最终输出缺失或为空 —— 跳过清理")
```

### 临时文件最佳实践

- 分段目录使用 `tempfile.mkdtemp()` —— 避免污染项目目录
- WAV 提取用 `tempfile.mktemp(suffix=".wav")` 命名，使其位于 OS 临时目录
- 调试时设置 `KEEP_INTERMEDIATES=1` 环境变量以跳过清理
- 特征缓存 (`.npz`) 存储廉价但重算昂贵 —— 默认保留
