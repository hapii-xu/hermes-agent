# 合成与亮度参考

可组合系统是视觉复杂度的核心。它在三个层面运作：像素级混合模式、多网格合成和自适应亮度管理。本文档涵盖这三个层面，以及用于空间控制的遮罩/模板系统。

> **另请参阅：** architecture.md · effects.md · scenes.md · shaders.md · troubleshooting.md

## 像素级混合模式

### `blend_canvas()` 函数

所有混合都在完整像素画布（`uint8 H,W,3`）上操作。内部转换为 float32 [0,1] 以保证精度，混合后按不透明度插值，再转换回来。

```python
def blend_canvas(base, top, mode="normal", opacity=1.0):
    af = base.astype(np.float32) / 255.0
    bf = top.astype(np.float32) / 255.0
    fn = BLEND_MODES.get(mode, BLEND_MODES["normal"])
    result = fn(af, bf)
    if opacity < 1.0:
        result = af * (1 - opacity) + result * opacity
    return np.clip(result * 255, 0, 255).astype(np.uint8)
```

### 20 种混合模式

```python
BLEND_MODES = {
    # 基本算术
    "normal":       lambda a, b: b,
    "add":          lambda a, b: np.clip(a + b, 0, 1),
    "subtract":     lambda a, b: np.clip(a - b, 0, 1),
    "multiply":     lambda a, b: a * b,
    "screen":       lambda a, b: 1 - (1 - a) * (1 - b),

    # 对比度
    "overlay":      lambda a, b: np.where(a < 0.5, 2*a*b, 1 - 2*(1-a)*(1-b)),
    "softlight":    lambda a, b: (1 - 2*b)*a*a + 2*b*a,
    "hardlight":    lambda a, b: np.where(b < 0.5, 2*a*b, 1 - 2*(1-a)*(1-b)),

    # 差值
    "difference":   lambda a, b: np.abs(a - b),
    "exclusion":    lambda a, b: a + b - 2*a*b,

    # 减淡 / 加深
    "colordodge":   lambda a, b: np.clip(a / (1 - b + 1e-6), 0, 1),
    "colorburn":    lambda a, b: np.clip(1 - (1 - a) / (b + 1e-6), 0, 1),

    # 光
    "linearlight":  lambda a, b: np.clip(a + 2*b - 1, 0, 1),
    "vividlight":   lambda a, b: np.where(b < 0.5,
                        np.clip(1 - (1-a)/(2*b + 1e-6), 0, 1),
                        np.clip(a / (2*(1-b) + 1e-6), 0, 1)),
    "pin_light":    lambda a, b: np.where(b < 0.5,
                        np.minimum(a, 2*b), np.maximum(a, 2*b - 1)),
    "hard_mix":     lambda a, b: np.where(a + b >= 1.0, 1.0, 0.0),

    # 比较
    "lighten":      lambda a, b: np.maximum(a, b),
    "darken":       lambda a, b: np.minimum(a, b),

    # 颗粒
    "grain_extract": lambda a, b: np.clip(a - b + 0.5, 0, 1),
    "grain_merge":  lambda a, b: np.clip(a + b - 0.5, 0, 1),
}
```

### 混合模式选择指南

**提亮的模式**（对暗输入安全）：
- `screen` —— 始终提亮。两个 50% 灰色图层 screen 得到 75%。首选的安全混合。
- `add` —— 简单相加，在白色处裁剪。适合火花、光晕、粒子叠加。
- `colordodge` —— 在重叠区域极端提亮。可能过曝。使用低不透明度 (0.3-0.5)。
- `linearlight` —— 激进提亮。类似于 add 但带偏移。

**变暗的模式**（暗输入时避免使用）：
- `multiply` —— 使一切变暗。仅在两个图层都已很亮时使用。
- `overlay` —— 底层 < 0.5 时变暗，> 0.5 时提亮。会压垮暗输入：`2 * 0.12 * 0.12 = 0.03`。对暗色素材改用 `screen`。
- `colorburn` —— 在重叠区域极端变暗。

**制造对比度的模式**：
- `softlight` —— 柔和对比。适合细腻的纹理叠加。
- `hardlight` —— 强烈对比。类似 overlay 但以顶层为基准。
- `vividlight` —— 非常激进的对比。谨慎使用。

**制造颜色效果的模式**：
- `difference` —— 类似 XOR 的图案。两个相同图层 difference 得到黑色；偏移图层产生狂野的颜色。非常适合迷幻外观。
- `exclusion` —— 较柔和的 difference 版本。产生互补色图案。
- `hard_mix` —— 在交叉处色调分离为纯黑/纯白/饱和色。

**用于纹理混合的模式**：
- `grain_extract` / `grain_merge` —— 从一个图层提取纹理，应用到另一个图层。

### 多图层链式

```python
# 模式：渲染图层 -> 顺序混合
canvas_a = _render_vf(r, "md", vf_plasma, hf_angle(0.0), PAL_DENSE, f, t, S)
canvas_b = _render_vf(r, "sm", vf_vortex, hf_time_cycle(0.1), PAL_RUNE, f, t, S)
canvas_c = _render_vf(r, "lg", vf_rings, hf_distance(), PAL_BLOCKS, f, t, S)

result = blend_canvas(canvas_a, canvas_b, "screen", 0.8)
result = blend_canvas(result, canvas_c, "difference", 0.6)
```

顺序很重要：`screen(A, B)` 是可交换的，但 `difference(screen(A,B), C)` 与 `difference(A, screen(B,C))` 不同。

### 线性光混合模式

标准 `blend_canvas()` 在 sRGB 空间操作 —— 即原始字节值。这对大多数用途没问题，但 sRGB 在感知上是非线性的：在 sRGB 中混合会使中间调变暗并略微偏移色相。要获得物理准确的混合（匹配光线实际组合的方式），需先转换到线性光。

使用 `architecture.md` § OKLAB 颜色系统中的 `srgb_to_linear()` / `linear_to_srgb()`。

```python
def blend_canvas_linear(base, top, mode="normal", opacity=1.0):
    """在线性光空间混合以获得物理准确的结果。

    API 与 blend_canvas() 相同，但在混合前将 sRGB → 线性，
    混合后将线性 → sRGB。由于伽马转换开销更大（约 2 倍），
    但对加性混合、screen 以及任何亮度重要的模式能产生正确结果。
    """
    af = srgb_to_linear(base.astype(np.float32) / 255.0)
    bf = srgb_to_linear(top.astype(np.float32) / 255.0)
    fn = BLEND_MODES.get(mode, BLEND_MODES["normal"])
    result = fn(af, bf)
    if opacity < 1.0:
        result = af * (1 - opacity) + result * opacity
    result = linear_to_srgb(np.clip(result, 0, 1))
    return np.clip(result * 255, 0, 255).astype(np.uint8)
```

**何时使用 `blend_canvas_linear()` 对比 `blend_canvas()`：**

| 场景 | 使用 | 原因 |
|----------|-----|-----|
| Screen 混合两个亮图层 | `linear` | sRGB screen 会使高光过亮 |
| Add 模式用于光晕/bloom 效果 | `linear` | 加性光遵循线性物理 |
| 低不透明度混合文本叠加 | `srgb` | 感知混合对文本更自然 |
| Multiply 用于阴影/变暗 | `srgb` | 变暗操作的差异极小 |
| 颜色关键工作（匹配参考） | `linear` | 避免中间调的 sRGB 色相偏移 |
| 性能关键的内部循环 | `srgb` | 约快 2 倍，对大多数 ASCII 艺术足够好 |

**批量版本**用于合成多个图层（转换一次，混合多次，转换回来）：

```python
def blend_many_linear(layers, modes, opacities):
    """在线性光空间混合一叠图层。

    Args:
        layers: uint8 (H,W,3) 画布列表
        modes: 混合模式字符串列表（长度 = len(layers) - 1）
        opacities: 浮点数列表（长度 = len(layers) - 1）
    Returns:
        uint8 (H,W,3) 画布
    """
    # 一次性全部转换为线性
    linear = [srgb_to_linear(l.astype(np.float32) / 255.0) for l in layers]
    result = linear[0]
    for i in range(1, len(linear)):
        fn = BLEND_MODES.get(modes[i-1], BLEND_MODES["normal"])
        blended = fn(result, linear[i])
        op = opacities[i-1]
        if op < 1.0:
            blended = result * (1 - op) + blended * op
        result = np.clip(blended, 0, 1)
    result = linear_to_srgb(result)
    return np.clip(result * 255, 0, 255).astype(np.uint8)
```

---

## 多网格合成

这是核心视觉技术。以不同网格密度（字符尺寸）渲染同一概念场景会产生自然的纹理干涉，因为不同尺度的字符在不同的空间频率上重叠。

### 为什么有效

- `sm` 网格（10pt 字体）：320x83 字符。精细细节、密集纹理。
- `md` 网格（16pt）：192x56 字符。中等密度。
- `lg` 网格（20pt）：160x45 字符。粗糙、块状字符。

当你在 `sm` 上渲染等离子场、在 `lg` 上渲染漩涡，然后 screen 混合它们时，精细的等离子纹理会从粗糙漩涡字符的间隙中透出。结果比任一图层单独都更具视觉复杂度。

### `_render_vf()` 辅助函数

这是主力函数。它接受一个值场 + 色相场 + 调色板 + 网格，渲染出完整的像素画布：

```python
def _render_vf(r, grid_key, val_fn, hue_fn, pal, f, t, S, sat=0.8, threshold=0.03):
    """通过命名网格将值场 + 色相场渲染为像素画布。

    Args:
        r: Renderer 实例（拥有 .get_grid()）
        grid_key: "xs", "sm", "md", "lg", "xl", "xxl"
        val_fn: (g, f, t, S) -> float32 [0,1] 数组 (rows, cols)
        hue_fn: 可调用对象 (g, f, t, S) -> float32 色相数组，或浮点标量
        pal: 字符调色板字符串
        f: 特征字典
        t: 时间（秒）
        S: 持久状态字典
        sat: HSV 饱和度 (0-1)
        threshold: 最小渲染值（低于则为空格）

    Returns:
        uint8 数组 (VH, VW, 3) —— 完整像素画布
    """
    g = r.get_grid(grid_key)
    val = np.clip(val_fn(g, f, t, S), 0, 1)
    mask = val > threshold
    ch = val2char(val, mask, pal)

    # 色相：可以是可调用对象或固定浮点数
    if callable(hue_fn):
        h = hue_fn(g, f, t, S) % 1.0
    else:
        h = np.full((g.rows, g.cols), float(hue_fn), dtype=np.float32)

    # 关键：广播到完整形状并拷贝（见故障排查）
    h = np.broadcast_to(h, (g.rows, g.cols)).copy()

    R, G, B = hsv2rgb(h, np.full_like(val, sat), val)
    co = mkc(R, G, B, g.rows, g.cols)
    return g.render(ch, co)
```

### 网格组合策略

| 组合 | 效果 | 适用 |
|-------------|--------|----------|
| `sm` + `lg` | 精细细节与块状字符之间最大对比 | 醒目、图形化外观 |
| `sm` + `md` | 微妙纹理分层，尺度相近 | 有机、流动的外观 |
| `md` + `lg` + `xs` | 三尺度干涉，最大复杂度 | 迷幻、密集 |
| `sm` + `sm`（不同效果） | 同尺度，仅图案干涉 | 莫尔条纹、干涉 |

### 完整多网格场景示例

```python
def fx_psychedelic(r, f, t, S):
    """带节拍反应万花筒的三层多网格场景。"""
    # A 层：中等网格上的等离子，彩虹色相
    canvas_a = _render_vf(r, "md",
        lambda g, f, t, S: vf_plasma(g, f, t, S) * 1.3,
        hf_angle(0.0), PAL_DENSE, f, t, S, sat=0.8)

    # B 层：小网格上的漩涡，循环色相
    canvas_b = _render_vf(r, "sm",
        lambda g, f, t, S: vf_vortex(g, f, t, S, twist=5.0) * 1.2,
        hf_time_cycle(0.1), PAL_RUNE, f, t, S, sat=0.7)

    # C 层：大网格上的环，距离色相
    canvas_c = _render_vf(r, "lg",
        lambda g, f, t, S: vf_rings(g, f, t, S, n_base=8, spacing_base=3) * 1.4,
        hf_distance(0.3, 0.02), PAL_BLOCKS, f, t, S, sat=0.9)

    # 混合：A 与 B screen，再与 C difference
    result = blend_canvas(canvas_a, canvas_b, "screen", 0.8)
    result = blend_canvas(result, canvas_c, "difference", 0.6)

    # 节拍触发的万花筒
    if f.get("bdecay", 0) > 0.3:
        result = sh_kaleidoscope(result.copy(), folds=6)

    return result
```

---

## 自适应色调映射

### 亮度问题

ASCII 字符是黑色背景上的小亮点。任何画布帧中的大部分像素都是背景（黑色）。这意味着：
- 平均画布帧亮度天然偏低（通常为 255 中的 5-30）
- 不同的效果组合产生差异极大的亮度水平
- 螺旋场景可能是平均 50，而火焰场景是平均 9
- 线性乘数（例如 `canvas * 2.0`）要么让暗场景仍然暗，要么让亮场景过曝

### `tonemap()` 函数

用自适应逐帧归一化 + 伽马校正替代线性亮度乘数：

```python
def tonemap(canvas, target_mean=90, gamma=0.75, black_point=2, white_point=253):
    """自适应色调映射：归一化 + 伽马校正，使没有画布帧
    完全变暗或完全过曝。

    1. 在 4 倍下采样上计算第 1 和第 99.5 百分位（值减少 16 倍，
       精度损失可忽略，1080p+ 下大幅加速）
    2. 将该范围拉伸到 [0, 1]
    3. 应用伽马曲线（< 1 提亮阴影，> 1 变暗）
    4. 重新缩放到 [black_point, white_point]
    """
    f = canvas.astype(np.float32)
    sub = f[::4, ::4]  # 4 倍下采样：1080p 下约 39 万值对比约 620 万
    lo = np.percentile(sub, 1)
    hi = np.percentile(sub, 99.5)
    if hi - lo < 10:
        hi = max(hi, lo + 10)  # 近均匀画布帧回退
    f = np.clip((f - lo) / (hi - lo), 0.0, 1.0)
    np.power(f, gamma, out=f)          # 原地操作：避免分配
    np.multiply(f, (white_point - black_point), out=f)
    np.add(f, black_point, out=f)
    return np.clip(f, 0, 255).astype(np.uint8)
```

### 为什么用伽马而非线性

线性乘数 `* 2.0`：
```
输入 10  -> 输出 20   （仍然暗）
输入 100 -> 输出 200  （尚可）
输入 200 -> 输出 255  （裁剪，丢失细节）
```

归一化后伽马 0.75：
```
输入 0.04 -> 输出 0.08（从不可见提升为可见）
输入 0.39 -> 输出 0.50（中等提升）
输入 0.78 -> 输出 0.84（温和提升，无裁剪）
```

伽马 < 1 压缩高光、扩展阴影。这正是我们需要的：将暗的 ASCII 内容提升到可见，同时不让亮部过曝。

### 流水线顺序

`render_clip()` 中的流水线为：

```
scene_fn(r, f, t, S)  ->  canvas
         |
    tonemap(canvas, gamma=scene_gamma)
         |
    FeedbackBuffer.apply(canvas, ...)
         |
    ShaderChain.apply(canvas, f=f, t=t)
         |
    ffmpeg pipe
```

色调映射在反馈和着色器之前运行。这意味着：
- 反馈在归一化数据上操作（无论场景亮度如何，行为一致）
- 像 solarize、posterize、contrast 这样的着色器在范围正确的数据上操作
- 链中的亮度着色器不再需要（tonemap 已处理）

### 逐场景伽马调优

默认伽马为 0.75。应用破坏性后期处理的场景需要更激进的提亮，因为破坏发生在 tonemap 之后：

| 场景类型 | 推荐伽马 | 原因 |
|------------|-------------------|-----|
| 标准效果 | 0.75 | 默认，对大多数场景有效 |
| Solarize 后期处理 | 0.50-0.60 | Solarize 反转亮像素，降低整体亮度 |
| Posterize 后期处理 | 0.50-0.55 | Posterize 量化，常把中间值压成黑色 |
| 重度 difference 混合 | 0.60-0.70 | Difference 模式产生许多接近零的像素 |
| 本身已亮的场景 | 0.85-1.0 | 不要过度提升天然就亮的场景 |

通过场景表配置：

```python
SCENES = [
    {"start": 9.17, "end": 11.25, "name": "fire", "gamma": 0.55,
     "fx": fx_fire, "shaders": [("solarize", {"threshold": 200}), ...]},
    {"start": 25.96, "end": 27.29, "name": "diamond", "gamma": 0.5,
     "fx": fx_diamond, "shaders": [("bloom", {"thr": 90}), ...]},
]
```

### 亮度验证

渲染后，抽查画布帧亮度：

```python
# 在测试画布帧模式下
canvas = scene["fx"](r, feat, t, r.S)
canvas = tonemap(canvas, gamma=scene.get("gamma", 0.75))
chain = ShaderChain()
for sn, kw in scene.get("shaders", []):
    chain.add(sn, **kw)
canvas = chain.apply(canvas, f=feat, t=t)
print(f"平均亮度：{canvas.astype(float).mean():.1f}，最大值：{canvas.max()}")
```

tonemap + 着色器后的目标范围：
- 安静/氛围场景：平均 30-60
- 活跃场景：平均 40-100
- 高潮/峰值场景：平均 60-150
- 若平均 < 20：伽马太高或某个着色器在破坏亮度
- 若平均 > 180：伽马太低或 add 叠加过多

---

## FeedbackBuffer 空间变换

反馈缓冲区存储前一画布帧并以衰减将其混合到当前画布帧。在混合前对缓冲区应用空间变换，会在反馈拖尾中产生运动错觉。

### 实现

```python
class FeedbackBuffer:
    def __init__(self):
        self.buf = None

    def apply(self, canvas, decay=0.85, blend="screen", opacity=0.5,
              transform=None, transform_amt=0.02, hue_shift=0.0):
        if self.buf is None:
            self.buf = canvas.astype(np.float32) / 255.0
            return canvas

        # 衰减旧缓冲区
        self.buf *= decay

        # 空间变换
        if transform:
            self.buf = self._transform(self.buf, transform, transform_amt)

        # 为彩虹拖尾对反馈做色相偏移
        if hue_shift > 0:
            self.buf = self._hue_shift(self.buf, hue_shift)

        # 将反馈混合到当前画布帧
        result = blend_canvas(canvas,
                              np.clip(self.buf * 255, 0, 255).astype(np.uint8),
                              blend, opacity)

        # 用当前画布帧更新缓冲区
        self.buf = result.astype(np.float32) / 255.0
        return result

    def _transform(self, buf, transform, amt):
        h, w = buf.shape[:2]
        if transform == "zoom":
            # 放大：从稍内侧采样（产生扩展的隧道）
            m = int(h * amt); n = int(w * amt)
            if m > 0 and n > 0:
                cropped = buf[m:-m or None, n:-n or None]
                # 调整回完整尺寸（最近邻以提速）
                buf = np.array(Image.fromarray(
                    np.clip(cropped * 255, 0, 255).astype(np.uint8)
                ).resize((w, h), Image.NEAREST)).astype(np.float32) / 255.0
        elif transform == "shrink":
            # 缩小：填充边缘，缩小中心
            m = int(h * amt); n = int(w * amt)
            small = np.array(Image.fromarray(
                np.clip(buf * 255, 0, 255).astype(np.uint8)
            ).resize((w - 2*n, h - 2*m), Image.NEAREST))
            new = np.zeros((h, w, 3), dtype=np.uint8)
            new[m:m+small.shape[0], n:n+small.shape[1]] = small
            buf = new.astype(np.float32) / 255.0
        elif transform == "rotate_cw":
            # 通过仿射做小幅顺时针旋转
            angle = amt * 10  # amt=0.005 -> 每画布帧 0.05 度
            cy, cx = h / 2, w / 2
            Y = np.arange(h, dtype=np.float32)[:, None]
            X = np.arange(w, dtype=np.float32)[None, :]
            cos_a, sin_a = np.cos(angle), np.sin(angle)
            sx = (X - cx) * cos_a + (Y - cy) * sin_a + cx
            sy = -(X - cx) * sin_a + (Y - cy) * cos_a + cy
            sx = np.clip(sx.astype(int), 0, w - 1)
            sy = np.clip(sy.astype(int), 0, h - 1)
            buf = buf[sy, sx]
        elif transform == "rotate_ccw":
            angle = -amt * 10
            cy, cx = h / 2, w / 2
            Y = np.arange(h, dtype=np.float32)[:, None]
            X = np.arange(w, dtype=np.float32)[None, :]
            cos_a, sin_a = np.cos(angle), np.sin(angle)
            sx = (X - cx) * cos_a + (Y - cy) * sin_a + cx
            sy = -(X - cx) * sin_a + (Y - cy) * cos_a + cy
            sx = np.clip(sx.astype(int), 0, w - 1)
            sy = np.clip(sy.astype(int), 0, h - 1)
            buf = buf[sy, sx]
        elif transform == "shift_up":
            pixels = max(1, int(h * amt))
            buf = np.roll(buf, -pixels, axis=0)
            buf[-pixels:] = 0  # 底部黑色填充
        elif transform == "shift_down":
            pixels = max(1, int(h * amt))
            buf = np.roll(buf, pixels, axis=0)
            buf[:pixels] = 0
        elif transform == "mirror_h":
            buf = buf[:, ::-1]
        return buf

    def _hue_shift(self, buf, amount):
        """旋转反馈缓冲区的色相。在 float32 [0,1] 上操作。"""
        rgb = np.clip(buf * 255, 0, 255).astype(np.uint8)
        hsv = np.zeros_like(buf)
        # 简化的近似 RGB->HSV->偏移->RGB
        r, g, b = buf[:,:,0], buf[:,:,1], buf[:,:,2]
        mx = np.maximum(np.maximum(r, g), b)
        mn = np.minimum(np.minimum(r, g), b)
        delta = mx - mn + 1e-10
        # 色相
        h = np.where(mx == r, ((g - b) / delta) % 6,
            np.where(mx == g, (b - r) / delta + 2, (r - g) / delta + 4))
        h = (h / 6 + amount) % 1.0
        # 用偏移后的色相重建（简化）
        s = delta / (mx + 1e-10)
        v = mx
        c = v * s; x = c * (1 - np.abs((h * 6) % 2 - 1)); m = v - c
        ro = np.zeros_like(h); go = np.zeros_like(h); bo = np.zeros_like(h)
        for lo, hi, rv, gv, bv in [(0,1,c,x,0),(1,2,x,c,0),(2,3,0,c,x),
                                     (3,4,0,x,c),(4,5,x,0,c),(5,6,c,0,x)]:
            mask = ((h*6) >= lo) & ((h*6) < hi)
            ro[mask] = rv[mask] if not isinstance(rv, (int,float)) else rv
            go[mask] = gv[mask] if not isinstance(gv, (int,float)) else gv
            bo[mask] = bv[mask] if not isinstance(bv, (int,float)) else bv
        return np.stack([ro+m, go+m, bo+m], axis=2)
```

### 反馈预设

| 预设 | 配置 | 视觉效果 |
|--------|--------|---------------|
| 无限缩放隧道 | `decay=0.8, blend="screen", transform="zoom", transform_amt=0.015` | 扩展的环图案 |
| 彩虹拖尾 | `decay=0.7, blend="screen", transform="zoom", transform_amt=0.01, hue_shift=0.02` | 迷幻色彩拖尾 |
| 幽灵回声 | `decay=0.9, blend="add", opacity=0.15, transform="shift_up", transform_amt=0.01` | 微弱的上行涂抹 |
| 万花筒递归 | `decay=0.75, blend="screen", transform="rotate_cw", transform_amt=0.005, hue_shift=0.01` | 旋转的曼陀罗反馈 |
| 色彩演化 | `decay=0.8, blend="difference", opacity=0.4, hue_shift=0.03` | 画布帧间的颜色 XOR |
| 上升热浪 | `decay=0.5, blend="add", opacity=0.2, transform="shift_up", transform_amt=0.02` | 热空气闪烁 |

---

## 遮罩 / 模板系统

遮罩是范围 [0, 1] 的 float32 数组 `(rows, cols)` 或 `(VH, VW)`。它们控制效果在哪里可见：1.0 = 完全可见，0.0 = 完全隐藏。使用遮罩创建图底关系、焦点和形状揭示。

### 形状遮罩

```python
def mask_circle(g, cx_frac=0.5, cy_frac=0.5, radius=0.3, feather=0.05):
    """以归一化坐标 (cx_frac, cy_frac) 为中心的圆形遮罩。
    feather：软边宽度（0 = 硬截断）。"""
    asp = g.cw / g.ch if hasattr(g, 'cw') else 1.0
    dx = (g.cc / g.cols - cx_frac)
    dy = (g.rr / g.rows - cy_frac) * asp
    d = np.sqrt(dx**2 + dy**2)
    if feather > 0:
        return np.clip(1.0 - (d - radius) / feather, 0, 1)
    return (d <= radius).astype(np.float32)

def mask_rect(g, x0=0.2, y0=0.2, x1=0.8, y1=0.8, feather=0.03):
    """矩形遮罩。坐标为 [0,1] 归一化。"""
    dx = np.maximum(x0 - g.cc / g.cols, g.cc / g.cols - x1)
    dy = np.maximum(y0 - g.rr / g.rows, g.rr / g.rows - y1)
    d = np.maximum(dx, dy)
    if feather > 0:
        return np.clip(1.0 - d / feather, 0, 1)
    return (d <= 0).astype(np.float32)

def mask_ring(g, cx_frac=0.5, cy_frac=0.5, inner_r=0.15, outer_r=0.35,
              feather=0.03):
    """环形 / 环带遮罩。"""
    inner = mask_circle(g, cx_frac, cy_frac, inner_r, feather)
    outer = mask_circle(g, cx_frac, cy_frac, outer_r, feather)
    return outer - inner

def mask_gradient_h(g, start=0.0, end=1.0):
    """从左到右的渐变遮罩。"""
    return np.clip((g.cc / g.cols - start) / (end - start + 1e-10), 0, 1).astype(np.float32)

def mask_gradient_v(g, start=0.0, end=1.0):
    """从上到下的渐变遮罩。"""
    return np.clip((g.rr / g.rows - start) / (end - start + 1e-10), 0, 1).astype(np.float32)

def mask_gradient_radial(g, cx_frac=0.5, cy_frac=0.5, inner=0.0, outer=0.5):
    """径向渐变遮罩 —— 中心亮，边缘暗。"""
    d = np.sqrt((g.cc / g.cols - cx_frac)**2 + (g.rr / g.rows - cy_frac)**2)
    return np.clip(1.0 - (d - inner) / (outer - inner + 1e-10), 0, 1)
```

### 值场作为遮罩

将任何 `vf_*` 函数的输出用作空间遮罩：

```python
def mask_from_vf(vf_result, threshold=0.5, feather=0.1):
    """通过阈值化将值场转换为遮罩。
    feather：阈值周围的平滑边缘宽度。"""
    if feather > 0:
        return np.clip((vf_result - threshold + feather) / (2 * feather), 0, 1)
    return (vf_result > threshold).astype(np.float32)

def mask_select(mask, vf_a, vf_b):
    """空间条件：遮罩为 1 处显示 vf_a，为 0 处显示 vf_b。
    mask：float32 [0,1] 数组。中间值会混合。"""
    return vf_a * mask + vf_b * (1 - mask)
```

### 文本模板

将文本渲染为遮罩。效果仅在字形笔画中可见：

```python
def mask_text(grid, text, row_frac=0.5, font=None, font_size=None):
    """将文本字符串渲染为网格分辨率的 float32 遮罩 [0,1]。
    字符 = 1.0，背景 = 0.0。

    row_frac：以网格高度比例为单位的垂直位置。
    font：PIL ImageFont（为 None 时默认为网格的字体）。
    font_size：覆盖模板文本的字号（用于更大的模板文本）。
    """
    from PIL import Image, ImageDraw, ImageFont

    f = font or grid.font
    if font_size and font != grid.font:
        f = ImageFont.truetype(font.path, font_size)

    # 以像素分辨率将文本渲染到图像，再下采样到网格
    img = Image.new("L", (grid.cols * grid.cw, grid.ch), 0)
    draw = ImageDraw.Draw(img)
    bbox = draw.textbbox((0, 0), text, font=f)
    tw = bbox[2] - bbox[0]
    x = (grid.cols * grid.cw - tw) // 2
    draw.text((x, 0), text, fill=255, font=f)
    row_mask = np.array(img, dtype=np.float32) / 255.0

    # 放置到完整网格遮罩中
    mask = np.zeros((grid.rows, grid.cols), dtype=np.float32)
    target_row = int(grid.rows * row_frac)
    # 将渲染的文本下采样到网格单元
    for c in range(grid.cols):
        px = c * grid.cw
        if px + grid.cw <= row_mask.shape[1]:
            cell = row_mask[:, px:px + grid.cw]
            if cell.mean() > 0.1:
                mask[target_row, c] = cell.mean()
    return mask

def mask_text_block(grid, lines, start_row_frac=0.3, font=None):
    """多行文本模板。返回完整网格遮罩。"""
    mask = np.zeros((grid.rows, grid.cols), dtype=np.float32)
    for i, line in enumerate(lines):
        row_frac = start_row_frac + i / grid.rows
        line_mask = mask_text(grid, line, row_frac, font)
        mask = np.maximum(mask, line_mask)
    return mask
```

### 动画遮罩

随时间变化的遮罩，用于揭示、擦除和变形：

```python
def mask_iris(g, t, t_start, t_end, cx_frac=0.5, cy_frac=0.5,
              max_radius=0.7, ease_fn=None):
    """光圈开/关：从 0 增长到 max_radius 的圆形。
    ease_fn：缓动函数（默认：effects.md 中的 ease_in_out_cubic）。"""
    if ease_fn is None:
        ease_fn = lambda x: x * x * (3 - 2 * x)  # smoothstep 回退
    progress = np.clip((t - t_start) / (t_end - t_start), 0, 1)
    radius = ease_fn(progress) * max_radius
    return mask_circle(g, cx_frac, cy_frac, radius, feather=0.03)

def mask_wipe_h(g, t, t_start, t_end, direction="right"):
    """水平擦除揭示。"""
    progress = np.clip((t - t_start) / (t_end - t_start), 0, 1)
    if direction == "left":
        progress = 1 - progress
    return mask_gradient_h(g, start=progress - 0.05, end=progress + 0.05)

def mask_wipe_v(g, t, t_start, t_end, direction="down"):
    """垂直擦除揭示。"""
    progress = np.clip((t - t_start) / (t_end - t_start), 0, 1)
    if direction == "up":
        progress = 1 - progress
    return mask_gradient_v(g, start=progress - 0.05, end=progress + 0.05)

def mask_dissolve(g, t, t_start, t_end, seed=42):
    """随机像素溶解 —— 噪声阈值从 0 扫描到 1。"""
    progress = np.clip((t - t_start) / (t_end - t_start), 0, 1)
    rng = np.random.RandomState(seed)
    noise = rng.random((g.rows, g.cols)).astype(np.float32)
    return (noise < progress).astype(np.float32)
```

### 遮罩布尔运算

```python
def mask_union(a, b):
    """或 —— 任一遮罩激活处可见。"""
    return np.maximum(a, b)

def mask_intersect(a, b):
    """与 —— 仅两个遮罩都激活处可见。"""
    return np.minimum(a, b)

def mask_subtract(a, b):
    """A 减 B —— A 激活但 B 未激活处可见。"""
    return np.clip(a - b, 0, 1)

def mask_invert(m):
    """非 —— 翻转遮罩。"""
    return 1.0 - m
```

### 将遮罩应用到画布

```python
def apply_mask_canvas(canvas, mask, bg_canvas=None):
    """将网格分辨率的遮罩应用到像素画布。
    通过最近邻将遮罩从 (rows, cols) 扩展到 (VH, VW)。

    canvas: uint8 (VH, VW, 3)
    mask: float32 (rows, cols) [0,1]
    bg_canvas: 遮罩=0 处透出的内容。None = 黑色。
    """
    # 将遮罩扩展到像素分辨率
    mask_px = np.repeat(np.repeat(mask, canvas.shape[0] // mask.shape[0] + 1, axis=0),
                        canvas.shape[1] // mask.shape[1] + 1, axis=1)
    mask_px = mask_px[:canvas.shape[0], :canvas.shape[1]]

    if bg_canvas is not None:
        return np.clip(canvas * mask_px[:, :, None] +
                       bg_canvas * (1 - mask_px[:, :, None]), 0, 255).astype(np.uint8)
    return np.clip(canvas * mask_px[:, :, None], 0, 255).astype(np.uint8)

def apply_mask_vf(vf_a, vf_b, mask):
    """在值场层面应用遮罩 —— 在空间上混合两个值场。
    所有数组均为 (rows, cols) float32。"""
    return vf_a * mask + vf_b * (1 - mask)
```

---

## PixelBlendStack

用于多层合成的更高层封装：

```python
class PixelBlendStack:
    def __init__(self):
        self.layers = []

    def add(self, canvas, mode="normal", opacity=1.0):
        self.layers.append((canvas, mode, opacity))
        return self

    def composite(self):
        if not self.layers:
            return np.zeros((VH, VW, 3), dtype=np.uint8)
        result = self.layers[0][0]
        for canvas, mode, opacity in self.layers[1:]:
            result = blend_canvas(result, canvas, mode, opacity)
        return result
```

## 文本背景板（可读性遮罩）

当在繁忙的多网格 ASCII 背景上放置可读文本时，文本会融入背景变得难以辨认。**务必在文本区域后面应用深色背景板。**

该技术：计算所有文本字形的边界框，创建一个覆盖该区域（带填充）的高斯模糊深色遮罩，并在渲染文本之前将背景乘以 `(1 - mask * darkness)`。

```python
from scipy.ndimage import gaussian_filter

def apply_text_backdrop(canvas, glyphs, padding=80, darkness=0.75):
    """为可读性而使文本背后的背景变暗。

    在渲染背景之后、渲染文本之前调用。

    Args:
        canvas: (VH, VW, 3) uint8 背景
        glyphs: {"x": float, "y": float, ...} 字形位置列表
        padding: 文本边界框周围的像素填充
        darkness: 0.0 = 不变暗，1.0 = 完全黑色
    Returns:
        变暗后的画布 (uint8)
    """
    if not glyphs:
        return canvas
    xs = [g['x'] for g in glyphs]
    ys = [g['y'] for g in glyphs]
    x0 = max(0, int(min(xs)) - padding)
    y0 = max(0, int(min(ys)) - padding)
    x1 = min(VW, int(max(xs)) + padding + 50)   # 额外空间用于字符宽度
    y1 = min(VH, int(max(ys)) + padding + 60)   # 额外空间用于字符高度

    # 用高斯模糊制作软深色遮罩以获得羽化边缘
    mask = np.zeros((VH, VW), dtype=np.float32)
    mask[y0:y1, x0:x1] = 1.0
    mask = gaussian_filter(mask, sigma=padding * 0.6)

    factor = 1.0 - mask * darkness
    return (canvas.astype(np.float32) * factor[:, :, np.newaxis]).astype(np.uint8)
```

### 在渲染流水线中的使用

插入在背景渲染和文本渲染之间：

```python
# 1. 渲染背景（多网格 ASCII 效果）
bg = render_background(cfg, t)

# 2. 使文本区域背后变暗
bg = apply_text_backdrop(bg, frame_glyphs, padding=80, darkness=0.75)

# 3. 在上方渲染文本（现在在深色背景板上可读）
bg = text_renderer.render(bg, frame_glyphs, color=(255, 255, 255))
```

对于文本始终居中的场景，可与**反向暗角**（见 shaders.md）结合 —— 反向暗角提供持久的中心暗区，而背景板处理逐画布帧的字形位置。

## 外部布局 Oracle 模式

对于需要动态围绕障碍物（形状、图标、其他文本）重新排版的文本密集型视频，使用外部布局引擎预先计算字形位置，并通过 JSON 送入 Python 渲染器。

### 架构

```
布局引擎（浏览器/Node.js）  →  layouts.json  →  Python ASCII 渲染器
         ↑                                                    ↑
   计算逐画布帧的                                  读取字形位置，
   字形 (x,y) 位置                                  作为 ASCII 字符渲染
   带障碍物感知的重排                                并走完整效果流水线
```

### JSON 交换格式

```json
{
  "meta": {
    "canvas_width": 1080, "canvas_height": 1080,
    "fps": 24, "total_frames": 1248,
    "fonts": {
      "body": {"charW": 12.04, "charH": 24, "fontSize": 20},
      "hero": {"charW": 24.08, "charH": 48, "fontSize": 40}
    }
  },
  "scenes": [
    {
      "id": "scene_name",
      "start_frame": 0, "end_frame": 96,
      "frames": {
        "0": {
          "glyphs": [
            {"char": "H", "x": 287.1, "y": 400.0, "alpha": 1.0},
            {"char": "e", "x": 311.2, "y": 400.0, "alpha": 1.0}
          ],
          "obstacles": [
            {"type": "circle", "cx": 540, "cy": 540, "r": 80},
            {"type": "rect", "x": 300, "y": 500, "w": 120, "h": 80}
          ]
        }
      }
    }
  ]
}
```

### 何时使用

- 围绕移动物体动态重排的文本
- 逐字形动画（揭示、散射、物理）
- 需要精确测量的可变字体排印
- Python 的 Pillow 文本布局不够用的任何情况

### 何时不使用

- 静态居中文本（直接用 PIL `draw.text()`）
- 仅淡入/淡出而无空间动画的文本
- 简单打字机效果（用字符计数器在 Python 中处理）

### 运行 oracle

使用 Playwright 在无头浏览器中运行布局引擎：

```javascript
// extract.mjs
import { chromium } from 'playwright';
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
await page.goto(`file://${oraclePath}`);
await page.waitForFunction(() => window.__ORACLE_DONE__ === true, null, { timeout: 60000 });
const result = await page.evaluate(() => window.__ORACLE_RESULT__);
writeFileSync('layouts.json', JSON.stringify(result));
await browser.close();
```

### 在 Python 中消费

```python
# 在渲染器中，将像素位置映射到画布：
for glyph in frame_data['glyphs']:
    char, px, py = glyph['char'], glyph['x'], glyph['y']
    alpha = glyph.get('alpha', 1.0)
    # 在精确像素位置用 PIL draw.text() 渲染
    draw.text((px, py), char, fill=(int(255*alpha),)*3, font=font)
```

JSON 中的障碍物也可渲染为发光的 ASCII 形状（圆形、矩形），以可视化重排区域。
