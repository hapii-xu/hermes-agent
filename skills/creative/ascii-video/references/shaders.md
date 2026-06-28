# 着色器管线与可组合特效

应用于像素画布（`numpy uint8 array, shape (H,W,3)`）的后处理特效，发生在字符渲染之后、编码之前。还涵盖**像素级混合模式**、**反馈缓冲**以及 **ShaderChain** 合成器。

> **另请参阅：** composition.md（混合模式、色调映射）· effects.md · scenes.md · architecture.md · optimization.md · troubleshooting.md
>
> **混合模式：** 关于 20 种像素混合模式和 `blend_canvas()`，参见 `composition.md`。所有混合都使用 `blend_canvas(base, top, mode, opacity)`。

## 设计哲学

着色器管线把原始 ASCII 渲染转化为电影级输出。系统为**可组合性**而设计 —— 每个着色器、混合模式和反馈变换都是独立的构建块。组合它们能从少量原语中创造出无限的视觉变化。

选择能强化氛围的着色器：
- **复古终端**：CRT + 扫描线 + 颗粒 + 绿色/琥珀色调
- **干净现代**：轻度辉光 + 仅细微暗角
- **故障艺术**：强烈的色差 + 故障条带 + 颜色摆动 + 像素排序
- **电影感**：辉光 + 暗角 + 颗粒 + 调色
- **梦幻**：强辉光 + 柔焦 + 颜色摆动 + 低对比度
- **粗粝/工业**：高对比度 + 颗粒 + 扫描线 + 无辉光
- **迷幻**：颜色摆动 + 色差 + 万花筒镜像 + 高饱和度 + 带色相偏移的反馈
- **数据损坏**：像素排序 + 数据弯曲 + 块故障 + 色调分离
- **递归/无限**：带缩放的反馈缓冲 + 屏幕混合 + 色相偏移

---

## 像素级混合模式

全部在 float32 [0,1] 画布上运算以保证精度。使用 `blend_canvas(base, top, mode, opacity)`，它会处理 uint8 与 float 之间的转换。

### 可用模式

```python
BLEND_MODES = {
    "normal":       lambda a, b: b,
    "add":          lambda a, b: np.clip(a + b, 0, 1),
    "subtract":     lambda a, b: np.clip(a - b, 0, 1),
    "multiply":     lambda a, b: a * b,
    "screen":       lambda a, b: 1 - (1-a)*(1-b),
    "overlay":      # a<0.5 时为 2*a*b，否则为 1-2*(1-a)*(1-b)
    "softlight":    lambda a, b: (1-2*b)*a*a + 2*b*a,
    "hardlight":    # 类似 overlay，但以 b 为键
    "difference":   lambda a, b: abs(a - b),
    "exclusion":    lambda a, b: a + b - 2*a*b,
    "colordodge":   lambda a, b: a / (1-b),
    "colorburn":    lambda a, b: 1 - (1-a)/b,
    "linearlight":  lambda a, b: a + 2*b - 1,
    "vividlight":   # b<0.5 时 burn，b>=0.5 时 dodge
    "pin_light":    # b<0.5 时 min(a,2b)，b>=0.5 时 max(a,2b-1)
    "hard_mix":     lambda a, b: 1 if a+b>=1 else 0,
    "lighten":      lambda a, b: max(a, b),
    "darken":       lambda a, b: min(a, b),
    "grain_extract": lambda a, b: a - b + 0.5,
    "grain_merge":  lambda a, b: a + b - 0.5,
}
```

### 用法

```python
def blend_canvas(base, top, mode="normal", opacity=1.0):
    """用命名的混合模式 + 不透明度混合两个 uint8 画布 (H,W,3)。"""
    af = base.astype(np.float32) / 255.0
    bf = top.astype(np.float32) / 255.0
    result = BLEND_MODES[mode](af, bf)
    if opacity < 1.0:
        result = af * (1-opacity) + result * opacity
    return np.clip(result * 255, 0, 255).astype(np.uint8)

# 多层合成
result = blend_canvas(base, layer_a, "screen", 0.7)
result = blend_canvas(result, layer_b, "difference", 0.5)
result = blend_canvas(result, layer_c, "multiply", 0.3)
```

### 创意组合

- **反馈 + 差值** = 迷幻的色彩演化（每帧与上一帧做 XOR）
- **screen + screen** = 加性辉光叠加
- **multiply** 作用于两个不同特效 = 只在两者都有亮度的地方显示（交集）
- **exclusion** 作用于两层 = 在它们相异处生成互补图样
- **color dodge/burn** = 在重叠区产生极端对比增强
- **hard mix** = 在交集处把一切都简化为纯黑/白/色

---

## 反馈缓冲

递归的时间效应：第 N-1 帧以衰减和可选的空间变换反馈到第 N 帧。产生拖尾、回声、涂抹、缩放隧道、旋转反馈、彩虹拖尾。

```python
class FeedbackBuffer:
    def __init__(self):
        self.buf = None  # 上一帧（float32，0-1）
    
    def apply(self, canvas, decay=0.85, blend="screen", opacity=0.5,
              transform=None, transform_amt=0.02, hue_shift=0.0):
        """将当前帧与衰减/变换后的上一帧混合。
        
        Args:
            canvas: 当前帧（uint8 H,W,3）
            decay: 旧帧淡出速度（0=立即，1=永久）
            blend: 用于混合反馈的混合模式
            opacity: 反馈混合的强度
            transform: None、"zoom"、"shrink"、"rotate_cw"、"rotate_ccw"、
                       "shift_up"、"shift_down"、"mirror_h"
            transform_amt: 每帧空间变换的强度
            hue_shift: 每帧旋转反馈缓冲的色相（0-1）
        """
```

### 反馈预设

```python
# 无限缩放隧道
fb_cfg = {"decay": 0.8, "blend": "screen", "opacity": 0.4,
          "transform": "zoom", "transform_amt": 0.015}

# 彩虹拖尾（迷幻）
fb_cfg = {"decay": 0.7, "blend": "screen", "opacity": 0.3,
          "transform": "zoom", "transform_amt": 0.01, "hue_shift": 0.02}

# 幽灵回声（恐怖）
fb_cfg = {"decay": 0.9, "blend": "add", "opacity": 0.15,
          "transform": "shift_up", "transform_amt": 0.01}

# 万花筒递归
fb_cfg = {"decay": 0.75, "blend": "screen", "opacity": 0.35,
          "transform": "rotate_cw", "transform_amt": 0.005, "hue_shift": 0.01}

# 色彩演化（抽象）
fb_cfg = {"decay": 0.8, "blend": "difference", "opacity": 0.4, "hue_shift": 0.03}

# 相乘深度
fb_cfg = {"decay": 0.65, "blend": "multiply", "opacity": 0.3, "transform": "mirror_h"}

# 升腾的热浪
fb_cfg = {"decay": 0.5, "blend": "add", "opacity": 0.2,
          "transform": "shift_up", "transform_amt": 0.02}
```

---

## ShaderChain

可组合的着色器管线。用参数构建命名着色器链。顺序很重要 —— 着色器按顺序依次作用于画布。

```python
class ShaderChain:
    """可组合的着色器管线。
    
    用法：
        chain = ShaderChain()
        chain.add("bloom", thr=120)
        chain.add("chromatic", amt=5)
        chain.add("kaleidoscope", folds=6)
        chain.add("vignette", s=0.2)
        chain.add("grain", amt=12)
        canvas = chain.apply(canvas, f=features, t=time)
    """
    def __init__(self):
        self.steps = []

    def add(self, shader_name, **kwargs):
        self.steps.append((shader_name, kwargs))
        return self  # 可链式调用

    def apply(self, canvas, f=None, t=0):
        if f is None: f = {}
        for name, kwargs in self.steps:
            canvas = _apply_shader_step(canvas, name, kwargs, f, t)
        return canvas
```

### `_apply_shader_step()` —— 完整的分派函数

把着色器名路由到具体实现。某些着色器具备**音频响应缩放** —— 分派函数会读取 `f["bdecay"]` 和 `f["rms"]` 以在节拍上调制参数。

```python
def _apply_shader_step(canvas, name, kwargs, f, t):
    """按名称 + kwargs 分派单个着色器。
    
    Args:
        canvas: uint8 (H,W,3) 像素数组
        name: 着色器键名字符串（如 "bloom"、"chromatic"）
        kwargs: 着色器参数字典
        f: 音频特征字典（键：bdecay、rms、sub 等）
        t: 当前时间（秒，浮点）
    Returns:
        canvas: uint8 (H,W,3) —— 处理后
    """
    bd = f.get("bdecay", 0)    # 节拍衰减（0-1，节拍上偏高）
    rms = f.get("rms", 0.3)   # 音频能量（0-1）

    # --- 几何 ---
    if name == "crt":
        return sh_crt(canvas, kwargs.get("strength", 0.05))
    elif name == "pixelate":
        return sh_pixelate(canvas, kwargs.get("block", 4))
    elif name == "wave_distort":
        return sh_wave_distort(canvas, t,
            kwargs.get("freq", 0.02), kwargs.get("amp", 8), kwargs.get("axis", "x"))
    elif name == "kaleidoscope":
        return sh_kaleidoscope(canvas.copy(), kwargs.get("folds", 6))
    elif name == "mirror_h":
        return sh_mirror_h(canvas.copy())
    elif name == "mirror_v":
        return sh_mirror_v(canvas.copy())
    elif name == "mirror_quad":
        return sh_mirror_quad(canvas.copy())
    elif name == "mirror_diag":
        return sh_mirror_diag(canvas.copy())

    # --- 通道 ---
    elif name == "chromatic":
        base = kwargs.get("amt", 3)
        return sh_chromatic(canvas, max(1, int(base * (0.4 + bd * 0.8))))
    elif name == "channel_shift":
        return sh_channel_shift(canvas,
            kwargs.get("r", (0,0)), kwargs.get("g", (0,0)), kwargs.get("b", (0,0)))
    elif name == "channel_swap":
        return sh_channel_swap(canvas, kwargs.get("order", (2,1,0)))
    elif name == "rgb_split_radial":
        return sh_rgb_split_radial(canvas, kwargs.get("strength", 5))

    # --- 颜色 ---
    elif name == "invert":
        return sh_invert(canvas)
    elif name == "posterize":
        return sh_posterize(canvas, kwargs.get("levels", 4))
    elif name == "threshold":
        return sh_threshold(canvas, kwargs.get("thr", 128))
    elif name == "solarize":
        return sh_solarize(canvas, kwargs.get("threshold", 128))
    elif name == "hue_rotate":
        return sh_hue_rotate(canvas, kwargs.get("amount", 0.1))
    elif name == "saturation":
        return sh_saturation(canvas, kwargs.get("factor", 1.5))
    elif name == "color_grade":
        return sh_color_grade(canvas, kwargs.get("tint", (1,1,1)))
    elif name == "color_wobble":
        return sh_color_wobble(canvas, t, kwargs.get("amt", 0.3) * (0.5 + rms * 0.8))
    elif name == "color_ramp":
        return sh_color_ramp(canvas, kwargs.get("ramp", [(0,0,0),(255,255,255)]))

    # --- 辉光 / 模糊 ---
    elif name == "bloom":
        return sh_bloom(canvas, kwargs.get("thr", 130))
    elif name == "edge_glow":
        return sh_edge_glow(canvas, kwargs.get("hue", 0.5))
    elif name == "soft_focus":
        return sh_soft_focus(canvas, kwargs.get("strength", 0.3))
    elif name == "radial_blur":
        return sh_radial_blur(canvas, kwargs.get("strength", 0.03))

    # --- 噪声 ---
    elif name == "grain":
        return sh_grain(canvas, int(kwargs.get("amt", 10) * (0.5 + rms * 0.8)))
    elif name == "static":
        return sh_static_noise(canvas, kwargs.get("density", 0.05), kwargs.get("color", True))

    # --- 线条 / 图案 ---
    elif name == "scanlines":
        return sh_scanlines(canvas, kwargs.get("intensity", 0.08), kwargs.get("spacing", 3))
    elif name == "halftone":
        return sh_halftone(canvas, kwargs.get("dot_size", 6))

    # --- 色调 ---
    elif name == "vignette":
        return sh_vignette(canvas, kwargs.get("s", 0.22))
    elif name == "contrast":
        return sh_contrast(canvas, kwargs.get("factor", 1.3))
    elif name == "gamma":
        return sh_gamma(canvas, kwargs.get("gamma", 1.5))
    elif name == "levels":
        return sh_levels(canvas,
            kwargs.get("black", 0), kwargs.get("white", 255), kwargs.get("midtone", 1.0))
    elif name == "brightness":
        return sh_brightness(canvas, kwargs.get("factor", 1.5))

    # --- 故障 / 数据 ---
    elif name == "glitch_bands":
        return sh_glitch_bands(canvas, f)
    elif name == "block_glitch":
        return sh_block_glitch(canvas, kwargs.get("n_blocks", 8), kwargs.get("max_size", 40))
    elif name == "pixel_sort":
        return sh_pixel_sort(canvas, kwargs.get("threshold", 100), kwargs.get("direction", "h"))
    elif name == "data_bend":
        return sh_data_bend(canvas, kwargs.get("offset", 1000), kwargs.get("chunk", 500))

    else:
        return canvas  # 未知着色器 —— 直通
```

### 音频响应着色器

有三种着色器会根据音频特征缩放参数：

| 着色器 | 响应于 | 效果 |
|--------|------------|--------|
| `chromatic` | `bdecay` | `amt * (0.4 + bdecay * 0.8)` —— 色差在节拍上爆发 |
| `color_wobble` | `rms` | `amt * (0.5 + rms * 0.8)` —— 摆动强度跟随能量 |
| `grain` | `rms` | `amt * (0.5 + rms * 0.8)` —— 颗粒在响亮段落更粗粝 |
| `glitch_bands` | `bdecay`、`sub` | 条带数量与位移随节拍能量缩放 |

要让任意着色器响应节拍，可在分派中缩放其参数：`base_val * (low + bd * range)`。

---

## 完整着色器目录

### 几何着色器

| 着色器 | 关键参数 | 描述 |
|--------|-----------|-------------|
| `crt` | `strength=0.05` | CRT 桶形畸变（缓存的重映射） |
| `pixelate` | `block=4` | 降低有效分辨率 |
| `wave_distort` | `freq, amp, axis` | 正弦行/列位移 |
| `kaleidoscope` | `folds=6` | 通过极坐标重映射实现径向对称 |
| `mirror_h` | — | 水平镜像 |
| `mirror_v` | — | 垂直镜像 |
| `mirror_quad` | — | 四向镜像 |
| `mirror_diag` | — | 对角镜像 |

### 通道操作

| 着色器 | 关键参数 | 描述 |
|--------|-----------|-------------|
| `chromatic` | `amt=3` | R/B 通道水平偏移（节拍响应） |
| `channel_shift` | `r=(sx,sy), g, b` | 每通道独立的 x、y 偏移 |
| `channel_swap` | `order=(2,1,0)` | 重排 RGB 通道（BGR、GRB 等） |
| `rgb_split_radial` | `strength=5` | 从中心辐射的色差 |

### 颜色操作

| 着色器 | 关键参数 | 描述 |
|--------|-----------|-------------|
| `invert` | — | 反相所有颜色 |
| `posterize` | `levels=4` | 将色阶降到 N 级 |
| `threshold` | `thr=128` | 二值黑白 |
| `solarize` | `threshold=128` | 反相高于阈值的像素 |
| `hue_rotate` | `amount=0.1` | 所有色相旋转 amount（0-1） |
| `saturation` | `factor=1.5` | 缩放饱和度（>1=更饱和，<1=更淡） |
| `color_grade` | `tint=(r,g,b)` | 每通道乘数 |
| `color_wobble` | `amt=0.3` | 时变的每通道正弦调制 |
| `color_ramp` | `ramp=[(R,G,B),...]` | 将亮度映射到自定义色彩渐变 |

### 辉光 / 模糊

| 着色器 | 关键参数 | 描述 |
|--------|-----------|-------------|
| `bloom` | `thr=130` | 亮区辉光（4 倍降采样 + 盒模糊） |
| `edge_glow` | `hue=0.5` | 检测边缘，叠加彩色覆盖 |
| `soft_focus` | `strength=0.3` | 与模糊版本混合 |
| `radial_blur` | `strength=0.03` | 从中心向外的缩放模糊 |

### 噪声 / 颗粒

| 着色器 | 关键参数 | 描述 |
|--------|-----------|-------------|
| `grain` | `amt=10` | 2 倍降采样的胶片颗粒（节拍响应） |
| `static` | `density=0.05, color=True` | 随机像素噪声（电视雪花） |

### 线条 / 图案

| 着色器 | 关键参数 | 描述 |
|--------|-----------|-------------|
| `scanlines` | `intensity=0.08, spacing=3` | 每 N 行压暗 |
| `halftone` | `dot_size=6` | 半色调点阵叠加 |

### 色调

| 着色器 | 关键参数 | 描述 |
|--------|-----------|-------------|
| `vignette` | `s=0.22` | 边缘压暗（缓存的距离场） |
| `contrast` | `factor=1.3` | 围绕中点 128 调整对比度 |
| `gamma` | `gamma=1.5` | gamma 校正（>1=中间调更亮） |
| `levels` | `black, white, midtone` | 色阶调整（Photoshop 风格） |
| `brightness` | `factor=1.5` | 全局亮度乘数 |

### 故障 / 数据

| 着色器 | 关键参数 | 描述 |
|--------|-----------|-------------|
| `glitch_bands` | （使用 `f`） | 节拍响应的水平行位移 |
| `block_glitch` | `n_blocks=8, max_size=40` | 随机矩形块位移 |
| `pixel_sort` | `threshold=100, direction="h"` | 按亮度在行/列内排序像素 |
| `data_bend` | `offset, chunk` | 原始字节位移（datamoshing） |

---

## 着色器实现

每个着色器函数接收一个画布（`uint8 H,W,3`）并返回同形状的画布。命名约定为 `sh_<name>`。构建坐标重映射表的几何着色器应当**缓存**这些表，因为表只取决于分辨率 + 参数，而不取决于帧内容。

### 辅助函数

操作色相/饱和度的着色器需要向量化的 HSV 转换：

```python
def rgb2hsv(r, g, b):
    """向量化的 RGB (0-255 uint8) -> HSV (float32 0-1)。"""
    rf = r.astype(np.float32) / 255.0
    gf = g.astype(np.float32) / 255.0
    bf = b.astype(np.float32) / 255.0
    cmax = np.maximum(np.maximum(rf, gf), bf)
    cmin = np.minimum(np.minimum(rf, gf), bf)
    delta = cmax - cmin + 1e-10
    h = np.zeros_like(rf)
    m = cmax == rf; h[m] = ((gf[m] - bf[m]) / delta[m]) % 6
    m = cmax == gf; h[m] = (bf[m] - rf[m]) / delta[m] + 2
    m = cmax == bf; h[m] = (rf[m] - gf[m]) / delta[m] + 4
    h = h / 6.0 % 1.0
    s = np.where(cmax > 0, delta / (cmax + 1e-10), 0)
    return h, s, cmax

def hsv2rgb(h, s, v):
    """向量化的 HSV->RGB。h、s、v 为 numpy float32 数组。"""
    h = h % 1.0
    c = v * s; x = c * (1 - np.abs((h * 6) % 2 - 1)); m = v - c
    r = np.zeros_like(h); g = np.zeros_like(h); b = np.zeros_like(h)
    mask = h < 1/6;            r[mask]=c[mask]; g[mask]=x[mask]
    mask = (h>=1/6)&(h<2/6);   r[mask]=x[mask]; g[mask]=c[mask]
    mask = (h>=2/6)&(h<3/6);   g[mask]=c[mask]; b[mask]=x[mask]
    mask = (h>=3/6)&(h<4/6);   g[mask]=x[mask]; b[mask]=c[mask]
    mask = (h>=4/6)&(h<5/6);   r[mask]=x[mask]; b[mask]=c[mask]
    mask = h >= 5/6;            r[mask]=c[mask]; b[mask]=x[mask]
    R = np.clip((r+m)*255, 0, 255).astype(np.uint8)
    G = np.clip((g+m)*255, 0, 255).astype(np.uint8)
    B = np.clip((b+m)*255, 0, 255).astype(np.uint8)
    return R, G, B

def mkc(R, G, B, rows, cols):
    """把 R、G、B uint8 数组堆叠成 (rows,cols,3) 画布。"""
    o = np.zeros((rows, cols, 3), dtype=np.uint8)
    o[:,:,0] = R; o[:,:,1] = G; o[:,:,2] = B
    return o
```

---

### 几何着色器

#### CRT 桶形畸变
缓存坐标重映射表 —— 它逐帧永不改变：
```python
_crt_cache = {}
def sh_crt(c, strength=0.05):
    k = (c.shape[0], c.shape[1], round(strength, 3))
    if k not in _crt_cache:
        h, w = c.shape[:2]; cy, cx = h/2, w/2
        Y = np.arange(h, dtype=np.float32)[:, None]
        X = np.arange(w, dtype=np.float32)[None, :]
        ny = (Y - cy) / cy; nx = (X - cx) / cx
        r2 = nx**2 + ny**2
        factor = 1 + strength * r2
        sx = np.clip((nx * factor * cx + cx), 0, w-1).astype(np.int32)
        sy = np.clip((ny * factor * cy + cy), 0, h-1).astype(np.int32)
        _crt_cache[k] = (sy, sx)
    sy, sx = _crt_cache[k]
    return c[sy, sx]
```

#### 像素化
```python
def sh_pixelate(c, block=4):
    """降低有效分辨率。"""
    sm = c[::block, ::block]
    return np.repeat(np.repeat(sm, block, axis=0), block, axis=1)[:c.shape[0], :c.shape[1]]
```

#### 波浪扭曲
```python
def sh_wave_distort(c, t, freq=0.02, amp=8, axis="x"):
    """正弦行/列位移。用时间 t 做动画。"""
    h, w = c.shape[:2]
    out = c.copy()
    if axis == "x":
        for y in range(h):
            shift = int(amp * math.sin(y * freq + t * 3))
            out[y] = np.roll(c[y], shift, axis=0)
    else:
        for x in range(w):
            shift = int(amp * math.sin(x * freq + t * 3))
            out[:, x] = np.roll(c[:, x], shift, axis=0)
    return out
```

#### 位移贴图
```python
def sh_displacement_map(c, dx_map, dy_map, strength=10):
    """用 float32 位移贴图（与 c 同 HxW）移动像素。
    dx_map/dy_map：正值 = 向右/下偏移。"""
    h, w = c.shape[:2]
    Y = np.arange(h)[:, None]; X = np.arange(w)[None, :]
    ny = np.clip((Y + (dy_map * strength).astype(int)), 0, h-1)
    nx = np.clip((X + (dx_map * strength).astype(int)), 0, w-1)
    return c[ny, nx]
```

#### 万花筒
```python
def sh_kaleidoscope(c, folds=6):
    """通过极坐标重映射实现径向对称。"""
    h, w = c.shape[:2]; cy, cx = h//2, w//2
    Y = np.arange(h, dtype=np.float32)[:, None] - cy
    X = np.arange(w, dtype=np.float32)[None, :] - cx
    angle = np.arctan2(Y, X)
    dist = np.sqrt(X**2 + Y**2)
    wedge = 2 * np.pi / folds
    folded_angle = np.abs((angle % wedge) - wedge/2)
    ny = np.clip((cy + dist * np.sin(folded_angle)).astype(int), 0, h-1)
    nx = np.clip((cx + dist * np.cos(folded_angle)).astype(int), 0, w-1)
    return c[ny, nx]
```

#### 镜像变体
```python
def sh_mirror_h(c):
    """水平镜像 —— 左半反射到右半。"""
    w = c.shape[1]; c[:, w//2:] = c[:, :w//2][:, ::-1]; return c

def sh_mirror_v(c):
    """垂直镜像 —— 上半反射到下半。"""
    h = c.shape[0]; c[h//2:, :] = c[:h//2, :][::-1, :]; return c

def sh_mirror_quad(c):
    """四向镜像 —— 左上象限反射到全部四个象限。"""
    h, w = c.shape[:2]; hh, hw = h//2, w//2
    tl = c[:hh, :hw].copy()
    c[:hh, hw:hw+tl.shape[1]] = tl[:, ::-1]
    c[hh:hh+tl.shape[0], :hw] = tl[::-1, :]
    c[hh:hh+tl.shape[0], hw:hw+tl.shape[1]] = tl[::-1, ::-1]
    return c

def sh_mirror_diag(c):
    """对角镜像 —— 左上三角反射。"""
    h, w = c.shape[:2]
    for y in range(h):
        x_cut = int(w * y / h)
        if x_cut > 0 and x_cut < w:
            c[y, x_cut:] = c[y, :x_cut+1][::-1][:w-x_cut]
    return c
```

> **注意：** 镜像着色器会原地修改。分派函数传入 `canvas.copy()` 以免损坏原始数据。

---

### 通道操作着色器

#### 色差
```python
def sh_chromatic(c, amt=3):
    """R/B 通道水平偏移。在分派中节拍响应（amt 按 bdecay 缩放）。"""
    if amt < 1: return c
    a = int(amt)
    o = c.copy()
    o[:, a:, 0] = c[:, :-a, 0]   # 红色右移
    o[:, :-a, 2] = c[:, a:, 2]   # 蓝色左移
    return o
```

#### 通道偏移
```python
def sh_channel_shift(c, r_shift=(0,0), g_shift=(0,0), b_shift=(0,0)):
    """每通道独立的 x、y 偏移。"""
    o = c.copy()
    for ch_i, (sx, sy) in enumerate([r_shift, g_shift, b_shift]):
        if sx != 0: o[:,:,ch_i] = np.roll(c[:,:,ch_i], sx, axis=1)
        if sy != 0: o[:,:,ch_i] = np.roll(o[:,:,ch_i], sy, axis=0)
    return o
```

#### 通道交换
```python
def sh_channel_swap(c, order=(2,1,0)):
    """重排 RGB 通道。(2,1,0)=BGR、(1,0,2)=GRB 等。"""
    return c[:, :, list(order)]
```

#### 径向 RGB 分裂
```python
def sh_rgb_split_radial(c, strength=5):
    """从中心辐射的色差 —— 边缘更强。"""
    h, w = c.shape[:2]; cy, cx = h//2, w//2
    Y = np.arange(h, dtype=np.float32)[:, None]
    X = np.arange(w, dtype=np.float32)[None, :]
    dist = np.sqrt((Y-cy)**2 + (X-cx)**2)
    max_dist = np.sqrt(cy**2 + cx**2)
    factor = dist / max_dist * strength
    dy = ((Y-cy) / (dist+1) * factor).astype(int)
    dx = ((X-cx) / (dist+1) * factor).astype(int)
    out = c.copy()
    ry = np.clip(Y.astype(int)+dy, 0, h-1); rx = np.clip(X.astype(int)+dx, 0, w-1)
    out[:,:,0] = c[ry, rx, 0]  # 红色向外移
    by = np.clip(Y.astype(int)-dy, 0, h-1); bx = np.clip(X.astype(int)-dx, 0, w-1)
    out[:,:,2] = c[by, bx, 2]  # 蓝色向内移
    return out
```

---

### 颜色操作着色器

#### 反相
```python
def sh_invert(c):
    return 255 - c
```

#### 色调分离
```python
def sh_posterize(c, levels=4):
    """将每通道色阶降到 N 级。"""
    step = 256.0 / levels
    return (np.floor(c.astype(np.float32) / step) * step).astype(np.uint8)
```

#### 阈值
```python
def sh_threshold(c, thr=128):
    """阈值处的二值黑白。"""
    gray = c.astype(np.float32).mean(axis=2)
    out = np.zeros_like(c); out[gray > thr] = 255
    return out
```

#### 日光化
```python
def sh_solarize(c, threshold=128):
    """反相高于阈值的像素 —— 经典暗房效果。"""
    o = c.copy(); mask = c > threshold; o[mask] = 255 - c[mask]
    return o
```

#### 色相旋转
```python
def sh_hue_rotate(c, amount=0.1):
    """所有色相旋转 amount（0-1）。"""
    h, s, v = rgb2hsv(c[:,:,0], c[:,:,1], c[:,:,2])
    h = (h + amount) % 1.0
    R, G, B = hsv2rgb(h, s, v)
    return mkc(R, G, B, c.shape[0], c.shape[1])
```

#### 饱和度
```python
def sh_saturation(c, factor=1.5):
    """调整饱和度。>1=更饱和，<1=更淡。"""
    h, s, v = rgb2hsv(c[:,:,0], c[:,:,1], c[:,:,2])
    s = np.clip(s * factor, 0, 1)
    R, G, B = hsv2rgb(h, s, v)
    return mkc(R, G, B, c.shape[0], c.shape[1])
```

#### 调色
```python
def sh_color_grade(c, tint):
    """每通道乘数。tint=(r_mul, g_mul, b_mul)。"""
    o = c.astype(np.float32)
    o[:,:,0] *= tint[0]; o[:,:,1] *= tint[1]; o[:,:,2] *= tint[2]
    return np.clip(o, 0, 255).astype(np.uint8)
```

#### 颜色摆动
```python
def sh_color_wobble(c, t, amt=0.3):
    """时变的每通道正弦调制。在分派中音频响应（amt 按 rms 缩放）。"""
    o = c.astype(np.float32)
    o[:,:,0] *= 1.0 + amt * math.sin(t * 5.0)
    o[:,:,1] *= 1.0 + amt * math.sin(t * 5.0 + 2.09)
    o[:,:,2] *= 1.0 + amt * math.sin(t * 5.0 + 4.19)
    return np.clip(o, 0, 255).astype(np.uint8)
```

#### 色彩渐变
```python
def sh_color_ramp(c, ramp_colors):
    """将亮度映射到自定义色彩渐变。
    ramp_colors = (R,G,B) 元组列表，从暗到亮均匀分布。"""
    gray = c.astype(np.float32).mean(axis=2) / 255.0
    n = len(ramp_colors)
    idx = np.clip(gray * (n-1), 0, n-1.001)
    lo = np.floor(idx).astype(int); hi = np.minimum(lo+1, n-1)
    frac = idx - lo
    ramp = np.array(ramp_colors, dtype=np.float32)
    out = ramp[lo] * (1-frac[:,:,None]) + ramp[hi] * frac[:,:,None]
    return np.clip(out, 0, 255).astype(np.uint8)
```

---

### 辉光 / 模糊着色器

#### 辉光
```python
def sh_bloom(c, thr=130):
    """亮区辉光：4 倍降采样、阈值、3 轮盒模糊、屏幕混合。"""
    sm = c[::4, ::4].astype(np.float32)
    br = np.where(sm > thr, sm, 0)
    for _ in range(3):
        p = np.pad(br, ((1,1),(1,1),(0,0)), mode="edge")
        br = (p[:-2,:-2]+p[:-2,1:-1]+p[:-2,2:]+p[1:-1,:-2]+p[1:-1,1:-1]+
              p[1:-1,2:]+p[2:,:-2]+p[2:,1:-1]+p[2:,2:]) / 9.0
    bl = np.repeat(np.repeat(br, 4, axis=0), 4, axis=1)[:c.shape[0], :c.shape[1]]
    return np.clip(c.astype(np.float32) + bl * 0.5, 0, 255).astype(np.uint8)
```

#### 边缘辉光
```python
def sh_edge_glow(c, hue=0.5):
    """通过梯度检测边缘，叠加彩色覆盖。"""
    gray = c.astype(np.float32).mean(axis=2)
    gx = np.abs(gray[:, 2:] - gray[:, :-2])
    gy = np.abs(gray[2:, :] - gray[:-2, :])
    ex = np.zeros_like(gray); ey = np.zeros_like(gray)
    ex[:, 1:-1] = gx; ey[1:-1, :] = gy
    edge = np.clip((ex + ey) / 255 * 2, 0, 1)
    R, G, B = hsv2rgb(np.full_like(edge, hue), np.full_like(edge, 0.8), edge * 0.5)
    out = c.astype(np.int16).copy()
    out[:,:,0] = np.clip(out[:,:,0] + R.astype(np.int16), 0, 255)
    out[:,:,1] = np.clip(out[:,:,1] + G.astype(np.int16), 0, 255)
    out[:,:,2] = np.clip(out[:,:,2] + B.astype(np.int16), 0, 255)
    return out.astype(np.uint8)
```

#### 柔焦
```python
def sh_soft_focus(c, strength=0.3):
    """将原图与 2 倍降采样的盒模糊混合。"""
    sm = c[::2, ::2].astype(np.float32)
    p = np.pad(sm, ((1,1),(1,1),(0,0)), mode="edge")
    bl = (p[:-2,:-2]+p[:-2,1:-1]+p[:-2,2:]+p[1:-1,:-2]+p[1:-1,1:-1]+
          p[1:-1,2:]+p[2:,:-2]+p[2:,1:-1]+p[2:,2:]) / 9.0
    bl = np.repeat(np.repeat(bl, 2, axis=0), 2, axis=1)[:c.shape[0], :c.shape[1]]
    return np.clip(c * (1-strength) + bl * strength, 0, 255).astype(np.uint8)
```

#### 径向模糊
```python
def sh_radial_blur(c, strength=0.03, center=None):
    """从中心向外的缩放模糊 —— 辐射状运动模糊。"""
    h, w = c.shape[:2]
    cy, cx = center if center else (h//2, w//2)
    Y = np.arange(h, dtype=np.float32)[:, None]
    X = np.arange(w, dtype=np.float32)[None, :]
    out = c.astype(np.float32)
    for s in [strength, strength*2]:
        dy = (Y - cy) * s; dx = (X - cx) * s
        sy = np.clip((Y + dy).astype(int), 0, h-1)
        sx = np.clip((X + dx).astype(int), 0, w-1)
        out += c[sy, sx].astype(np.float32)
    return np.clip(out / 3, 0, 255).astype(np.uint8)
```

---

### 噪声 / 颗粒着色器

#### 胶片颗粒
```python
def sh_grain(c, amt=10):
    """2 倍降采样的胶片颗粒。在分派中音频响应（amt 按 rms 缩放）。"""
    noise = np.random.randint(-amt, amt+1, (c.shape[0]//2, c.shape[1]//2, 1), dtype=np.int16)
    noise = np.repeat(np.repeat(noise, 2, axis=0), 2, axis=1)[:c.shape[0], :c.shape[1]]
    return np.clip(c.astype(np.int16) + noise, 0, 255).astype(np.uint8)
```

#### 静态噪声
```python
def sh_static_noise(c, density=0.05, color=True):
    """随机像素噪声叠加（电视雪花）。"""
    mask = np.random.random((c.shape[0]//2, c.shape[1]//2)) < density
    mask = np.repeat(np.repeat(mask, 2, axis=0), 2, axis=1)[:c.shape[0], :c.shape[1]]
    out = c.copy()
    if color:
        noise = np.random.randint(0, 256, (c.shape[0], c.shape[1], 3), dtype=np.uint8)
    else:
        v = np.random.randint(0, 256, (c.shape[0], c.shape[1]), dtype=np.uint8)
        noise = np.stack([v, v, v], axis=2)
    out[mask] = noise[mask]
    return out
```

---

### 线条 / 图案着色器

#### 扫描线
```python
def sh_scanlines(c, intensity=0.08, spacing=3):
    """每 N 行压暗。"""
    m = np.ones(c.shape[0], dtype=np.float32)
    m[::spacing] = 1.0 - intensity
    return np.clip(c * m[:, None, None], 0, 255).astype(np.uint8)
```

#### 半色调
```python
def sh_halftone(c, dot_size=6):
    """半色调点阵叠加 —— 圆点大小由局部亮度决定。"""
    h, w = c.shape[:2]
    gray = c.astype(np.float32).mean(axis=2) / 255.0
    out = np.zeros_like(c)
    for y in range(0, h, dot_size):
        for x in range(0, w, dot_size):
            block = gray[y:y+dot_size, x:x+dot_size]
            if block.size == 0: continue
            radius = block.mean() * dot_size * 0.5
            cy_b, cx_b = dot_size//2, dot_size//2
            for dy in range(min(dot_size, h-y)):
                for dx in range(min(dot_size, w-x)):
                    if math.sqrt((dy-cy_b)**2 + (dx-cx_b)**2) < radius:
                        out[y+dy, x+dx] = c[y+dy, x+dx]
    return out
```

> **性能提示：** 半色调因 Python 循环而较慢。对小分辨率或单帧测试可接受。生产环境建议用预计算距离掩码的向量化版本。

---

### 色调着色器

#### 暗角
```python
_vig_cache = {}
def sh_vignette(c, s=0.22):
    """用缓存的距离场做边缘压暗。"""
    k = (c.shape[0], c.shape[1], round(s, 2))
    if k not in _vig_cache:
        h, w = c.shape[:2]
        Y = np.linspace(-1, 1, h)[:, None]; X = np.linspace(-1, 1, w)[None, :]
        _vig_cache[k] = np.clip(1.0 - np.sqrt(X**2 + Y**2) * s, 0.15, 1).astype(np.float32)
    return np.clip(c * _vig_cache[k][:,:,None], 0, 255).astype(np.uint8)
```

#### 反向暗角

反向的暗角：压暗**中心**而保留边缘明亮。适用于文字居中叠在繁杂背景上的情况 —— 营造自然的暗区以提升可读性，且没有硬边框。

与 `apply_text_backdrop()`（见 composition.md）结合可实现逐帧的字形感知压暗。

```python
_rvignette_cache = {}

def sh_reverse_vignette(c, strength=0.5):
    """中心压暗、边缘提亮。已缓存。"""
    k = ('rv', c.shape[0], c.shape[1], round(strength, 2))
    if k not in _rvignette_cache:
        h, w = c.shape[:2]
        Y = np.linspace(-1, 1, h)[:, None]
        X = np.linspace(-1, 1, w)[None, :]
        d = np.sqrt(X**2 + Y**2)
        # 反转：边缘亮、中心暗
        mask = np.clip(1.0 - (1.0 - d * 0.7) * strength, 0.2, 1.0)
        _rvignette_cache[k] = mask[:, :, np.newaxis].astype(np.float32)
    return np.clip(c.astype(np.float32) * _rvignette_cache[k], 0, 255).astype(np.uint8)
```

| 参数 | 默认值 | 效果 |
|-------|---------|--------|
| `strength` | 0.5 | 0 = 无效果，1.0 = 中心近乎全黑 |

加入 ShaderChain 分派：
```python
elif name == "reverse_vignette":
    return sh_reverse_vignette(canvas, kwargs.get("strength", 0.5))
```

#### 对比度
```python
def sh_contrast(c, factor=1.3):
    """围绕中点 128 调整对比度。"""
    return np.clip((c.astype(np.float32) - 128) * factor + 128, 0, 255).astype(np.uint8)
```

#### gamma
```python
def sh_gamma(c, gamma=1.5):
    """gamma 校正。>1=中间调更亮，<1=中间调更暗。"""
    return np.clip(((c.astype(np.float32)/255.0) ** (1.0/gamma)) * 255, 0, 255).astype(np.uint8)
```

#### 色阶
```python
def sh_levels(c, black=0, white=255, midtone=1.0):
    """色阶调整（Photoshop 风格）。重映射黑/白点，再施加中间调 gamma。"""
    o = (c.astype(np.float32) - black) / max(1, white - black)
    o = np.clip(o, 0, 1) ** (1.0 / midtone)
    return (o * 255).astype(np.uint8)
```

#### 亮度
```python
def sh_brightness(c, factor=1.5):
    """全局亮度乘数。场景级亮度控制建议优先用 tonemap()。"""
    return np.clip(c.astype(np.float32) * factor, 0, 255).astype(np.uint8)
```

---

### 故障 / 数据着色器

#### 故障条带
```python
def sh_glitch_bands(c, f):
    """节拍响应的水平行位移。f = 音频特征字典。
    用 f["bdecay"] 控制强度，f["sub"] 控制条带高度。"""
    n = int(3 + f.get("bdecay", 0) * 10)
    out = c.copy()
    for _ in range(n):
        y = random.randint(0, c.shape[0]-1)
        h = random.randint(1, max(2, int(4 + f.get("sub", 0.3) * 12)))
        shift = int((random.random()-0.5) * f.get("bdecay", 0) * 60)
        if shift != 0 and y+h < c.shape[0]:
            out[y:y+h] = np.roll(out[y:y+h], shift, axis=1)
    return out
```

#### 块故障
```python
def sh_block_glitch(c, n_blocks=8, max_size=40):
    """随机矩形块位移 —— 把块复制到随机位置。"""
    out = c.copy(); h, w = c.shape[:2]
    for _ in range(n_blocks):
        bw = random.randint(10, max_size); bh = random.randint(5, max_size//2)
        sx = random.randint(0, w-bw-1); sy = random.randint(0, h-bh-1)
        dx = random.randint(0, w-bw-1); dy = random.randint(0, h-bh-1)
        out[dy:dy+bh, dx:dx+bw] = c[sy:sy+bh, sx:sx+bw]
    return out
```

#### 像素排序
```python
def sh_pixel_sort(c, threshold=100, direction="h"):
    """在连续的亮区内按亮度排序像素。"""
    gray = c.astype(np.float32).mean(axis=2)
    out = c.copy()
    if direction == "h":
        for y in range(0, c.shape[0], 3):  # 每 3 行一次以提速
            row_bright = gray[y]
            mask = row_bright > threshold
            regions = np.diff(np.concatenate([[0], mask.astype(int), [0]]))
            starts = np.where(regions == 1)[0]
            ends = np.where(regions == -1)[0]
            for s, e in zip(starts, ends):
                if e - s > 2:
                    indices = np.argsort(gray[y, s:e])
                    out[y, s:e] = c[y, s:e][indices]
    else:
        for x in range(0, c.shape[1], 3):
            col_bright = gray[:, x]
            mask = col_bright > threshold
            regions = np.diff(np.concatenate([[0], mask.astype(int), [0]]))
            starts = np.where(regions == 1)[0]
            ends = np.where(regions == -1)[0]
            for s, e in zip(starts, ends):
                if e - s > 2:
                    indices = np.argsort(gray[s:e, x])
                    out[s:e, x] = c[s:e, x][indices]
    return out
```

#### 数据弯曲
```python
def sh_data_bend(c, offset=1000, chunk=500):
    """把原始像素字节当作数据，把一个块复制到另一个偏移 —— datamosh 伪影。"""
    flat = c.flatten().copy()
    n = len(flat)
    src = offset % n; dst = (offset + chunk*3) % n
    length = min(chunk, n-src, n-dst)
    if length > 0:
        flat[dst:dst+length] = flat[src:src+length]
    return flat.reshape(c.shape)
```

---

## 色调预设

```python
TINT_WARM      = (1.15, 1.0, 0.85)   # 金色暖调
TINT_COOL      = (0.85, 0.95, 1.15)  # 蓝色冷调
TINT_MATRIX    = (0.7, 1.2, 0.7)     # 绿色终端
TINT_AMBER     = (1.2, 0.9, 0.6)     # 琥珀色显示器
TINT_SEPIA     = (1.2, 1.05, 0.8)    # 老胶片
TINT_NEON_PINK = (1.3, 0.7, 1.1)     # 赛博朋克粉
TINT_ICE       = (0.8, 1.0, 1.3)     # 冰冻
TINT_BLOOD     = (1.4, 0.7, 0.7)     # 恐怖红
TINT_FOREST    = (0.8, 1.15, 0.75)   # 自然绿
TINT_VOID      = (0.85, 0.85, 1.1)   # 深空
TINT_SUNSET    = (1.3, 0.85, 0.7)    # 橙色黄昏
```

---

## 转场

> **注意：** 这些作用于字符级 `(chars, colors)` 数组（v1 接口）。在 v2 中，场景之间的转场通常由节拍边界的硬切来处理（见 `scenes.md`），或通过把两个场景渲染成画布、再用 `blend_canvas()` 配合时变不透明度来实现。下面的字符级转场对场景内的特效仍然有用。

### 交叉淡化
```python
def tr_crossfade(ch_a, co_a, ch_b, co_b, blend):
    co = (co_a.astype(np.float32) * (1-blend) + co_b.astype(np.float32) * blend).astype(np.uint8)
    mask = np.random.random(ch_a.shape) < blend
    ch = ch_a.copy(); ch[mask] = ch_b[mask]
    return ch, co
```

### v2 画布级交叉淡化
```python
def tr_canvas_crossfade(canvas_a, canvas_b, blend):
    """两个画布之间的平滑像素交叉淡化。"""
    return np.clip(canvas_a * (1-blend) + canvas_b * blend, 0, 255).astype(np.uint8)
```

### 擦除（有方向）
```python
def tr_wipe(ch_a, co_a, ch_b, co_b, blend, direction="left"):
    """direction：left、right、up、down、radial、diagonal"""
    rows, cols = ch_a.shape
    if direction == "radial":
        cx, cy = cols/2, rows/2
        rr = np.arange(rows)[:, None]; cc = np.arange(cols)[None, :]
        d = np.sqrt((cc-cx)**2 + (rr-cy)**2)
        mask = d < blend * np.sqrt(cx**2 + cy**2)
        ch = ch_a.copy(); co = co_a.copy()
        ch[mask] = ch_b[mask]; co[mask] = co_b[mask]
    return ch, co
```

### 故障切换
```python
def tr_glitch_cut(ch_a, co_a, ch_b, co_b, blend):
    if blend < 0.5: ch, co = ch_a.copy(), co_a.copy()
    else: ch, co = ch_b.copy(), co_b.copy()
    if 0.3 < blend < 0.7:
        intensity = 1.0 - abs(blend - 0.5) * 4
        for _ in range(int(intensity * 20)):
            y = random.randint(0, ch.shape[0]-1)
            shift = int((random.random()-0.5) * 40 * intensity)
            if shift: ch[y] = np.roll(ch[y], shift); co[y] = np.roll(co[y], shift, axis=0)
    return ch, co
```

---

## 输出格式

### MP4（默认）
```python
cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
       "-s", f"{W}x{H}", "-r", str(fps), "-i", "pipe:0",
       "-c:v", "libx264", "-preset", "fast", "-crf", str(crf),
       "-pix_fmt", "yuv420p", output_path]
```

### GIF
```python
cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
       "-s", f"{W}x{H}", "-r", str(fps), "-i", "pipe:0",
       "-vf", f"fps={fps},scale={W}:{H}:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
       "-loop", "0", output_gif]
```

### PNG 序列

用于逐帧精确编辑、在外部工具（After Effects、Nuke）中合成，或无损归档：

```python
import os

def output_png_sequence(frames, output_dir, W, H, fps, prefix="frame"):
    """将帧写为带编号的 PNG。frames = uint8 (H,W,3) 数组的可迭代对象。"""
    os.makedirs(output_dir, exist_ok=True)
    
    # 方法 1：直接用 PIL 写入（无 ffmpeg 依赖）
    from PIL import Image
    for i, frame in enumerate(frames):
        img = Image.fromarray(frame)
        img.save(os.path.join(output_dir, f"{prefix}_{i:06d}.png"))
    
    # 方法 2：ffmpeg 管道（对大序列更快）
    cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(fps), "-i", "pipe:0",
           os.path.join(output_dir, f"{prefix}_%06d.png")]
```

把 PNG 序列重新组装成视频：
```bash
ffmpeg -framerate 24 -i frame_%06d.png -c:v libx264 -crf 18 -pix_fmt yuv420p output.mp4
```

### Alpha 通道 / 透明背景（RGBA）

用于在其他视频或图像之上合成 ASCII 艺术。使用 RGBA 画布（4 通道）而非 RGB（3 通道）：

```python
def create_rgba_canvas(H, W):
    """透明画布 —— alpha 通道从 0 开始（完全透明）。"""
    return np.zeros((H, W, 4), dtype=np.uint8)

def render_char_rgba(canvas, row, col, char_img, color_rgb, alpha=255):
    """渲染带 alpha 的字符。char_img = PIL 字形掩码（灰度）。
    alpha 来自字形掩码 —— 背景保持透明。"""
    r, g, b = color_rgb
    y0, x0 = row * cell_h, col * cell_w
    mask = np.array(char_img)  # 灰度 0-255
    canvas[y0:y0+cell_h, x0:x0+cell_w, 0] = np.maximum(canvas[y0:y0+cell_h, x0:x0+cell_w, 0], (mask * r / 255).astype(np.uint8))
    canvas[y0:y0+cell_h, x0:x0+cell_w, 1] = np.maximum(canvas[y0:y0+cell_h, x0:x0+cell_w, 1], (mask * g / 255).astype(np.uint8))
    canvas[y0:y0+cell_h, x0:x0+cell_w, 2] = np.maximum(canvas[y0:y0+cell_h, x0:x0+cell_w, 2], (mask * b / 255).astype(np.uint8))
    canvas[y0:y0+cell_h, x0:x0+cell_w, 3] = np.maximum(canvas[y0:y0+cell_h, x0:x0+cell_w, 3], mask)

def blend_onto_background(rgba_canvas, bg_rgb):
    """将 RGBA 画布合成到纯色或图像背景之上。"""
    alpha = rgba_canvas[:, :, 3:4].astype(np.float32) / 255.0
    fg = rgba_canvas[:, :, :3].astype(np.float32)
    bg = bg_rgb.astype(np.float32)
    result = fg * alpha + bg * (1.0 - alpha)
    return result.astype(np.uint8)
```

通过 ffmpeg 输出 RGBA（剪辑用 ProRes 4444，Web 用 WebM VP9）：
```bash
# ProRes 4444 —— 保留 alpha，在各 NLE 中被广泛支持
ffmpeg -y -f rawvideo -pix_fmt rgba -s {W}x{H} -r {fps} -i pipe:0 \
    -c:v prores_ks -profile:v 4444 -pix_fmt yuva444p10le output.mov

# WebM VP9 —— 为 web/浏览器合成提供 alpha 支持
ffmpeg -y -f rawvideo -pix_fmt rgba -s {W}x{H} -r {fps} -i pipe:0 \
    -c:v libvpx-vp9 -pix_fmt yuva420p -crf 30 -b:v 0 output.webm

# 带 alpha 的 PNG 序列（无损）
ffmpeg -y -f rawvideo -pix_fmt rgba -s {W}x{H} -r {fps} -i pipe:0 \
    frame_%06d.png
```

**关键约束**：操作 `(H,W,3)` 数组的着色器需要为 RGBA 做适配。要么只对 RGB 通道施加着色器并保留 alpha，要么编写 RGBA 感知的版本：

```python
def apply_shader_rgba(canvas_rgba, shader_fn, **kwargs):
    """把 RGB 着色器施加到 RGBA 画布的颜色通道上。"""
    rgb = canvas_rgba[:, :, :3]
    alpha = canvas_rgba[:, :, 3:4]
    rgb_out = shader_fn(rgb, **kwargs)
    return np.concatenate([rgb_out, alpha], axis=2)
```

---

## 实时终端渲染

使用 ANSI 转义码在终端中实时显示 ASCII。适用于开发期预览场景、现场表演以及交互式参数调优。

### ANSI 颜色转义码

```python
def rgb_to_ansi(r, g, b):
    """24 位真彩色 ANSI 转义（大多数现代终端支持）。"""
    return f"\033[38;2;{r};{g};{b}m"

ANSI_RESET = "\033[0m"
ANSI_CLEAR = "\033[2J\033[H"  # 清屏 + 光标归位
ANSI_HIDE_CURSOR = "\033[?25l"
ANSI_SHOW_CURSOR = "\033[?25h"
```

### 帧到 ANSI 的转换

```python
def frame_to_ansi(chars, colors):
    """把字符 + 颜色数组转换成单个 ANSI 字符串以供终端输出。
    
    Args:
        chars: (rows, cols) 单字符数组
        colors: (rows, cols, 3) uint8 RGB 数组
    Returns:
        str: 已编码的 ANSI 帧，可直接 sys.stdout.write()
    """
    rows, cols = chars.shape
    lines = []
    for r in range(rows):
        parts = []
        prev_color = None
        for c in range(cols):
            rgb = tuple(colors[r, c])
            ch = chars[r, c]
            if ch == " " or rgb == (0, 0, 0):
                parts.append(" ")
            else:
                if rgb != prev_color:
                    parts.append(rgb_to_ansi(*rgb))
                    prev_color = rgb
                parts.append(ch)
        parts.append(ANSI_RESET)
        lines.append("".join(parts))
    return "\n".join(lines)
```

### 优化：增量更新

只重绘自上一帧以来发生变化的字符。消除静态区域的冗余终端写入：

```python
def frame_to_ansi_delta(chars, colors, prev_chars, prev_colors):
    """只为变化的单元输出 ANSI 转义。"""
    rows, cols = chars.shape
    parts = []
    for r in range(rows):
        for c in range(cols):
            if (chars[r, c] != prev_chars[r, c] or
                not np.array_equal(colors[r, c], prev_colors[r, c])):
                parts.append(f"\033[{r+1};{c+1}H")  # 移动光标
                rgb = tuple(colors[r, c])
                parts.append(rgb_to_ansi(*rgb))
                parts.append(chars[r, c])
    return "".join(parts)
```

### 实时渲染循环

```python
import sys
import time

def render_live(scene_fn, r, fps=24, duration=None):
    """在终端中实时渲染场景函数。
    
    Args:
        scene_fn: v2 场景函数 (r, f, t, S) -> canvas
                  或填充网格的 v1 风格函数
        r: Renderer 实例
        fps: 目标帧率
        duration: 运行秒数（None = 运行到 Ctrl+C）
    """
    frame_time = 1.0 / fps
    S = {}
    f = {}  # 合成特征或接入实时音频
    
    sys.stdout.write(ANSI_HIDE_CURSOR + ANSI_CLEAR)
    sys.stdout.flush()
    
    t0 = time.monotonic()
    frame_count = 0
    try:
        while True:
            t = time.monotonic() - t0
            if duration and t > duration:
                break
            
            # 从时间合成特征（或通过 pyaudio 接入实时音频）
            f = synthesize_features(t)
            
            # 渲染场景 —— 终端用小网格
            g = r.get_grid("sm")
            # 选项 A：v2 场景 → 从画布提取字符/颜色（反向渲染）
            # 选项 B：直接调用特效函数获取字符/颜色
            canvas = scene_fn(r, f, t, S)
            
            # 用于终端显示时，直接渲染字符 + 颜色
            # （绕过像素画布 —— 终端用字符单元）
            chars, colors = scene_to_terminal(scene_fn, r, f, t, S, g)
            
            frame_str = ANSI_CLEAR + frame_to_ansi(chars, colors)
            sys.stdout.write(frame_str)
            sys.stdout.flush()
            
            # 帧计时
            elapsed = time.monotonic() - t0 - (frame_count * frame_time)
            sleep_time = frame_time - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
            frame_count += 1
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(ANSI_SHOW_CURSOR + ANSI_RESET + "\n")
        sys.stdout.flush()

def scene_to_terminal(scene_fn, r, f, t, S, g):
    """运行特效函数并返回供终端显示的 (chars, colors)。
    终端模式下跳过像素画布，直接处理字符数组。"""
    # 返回 (chars, colors) 的特效可直接工作
    # 对基于 vf 的特效，把值场 + 色相场渲染成字符/颜色：
    val = vf_plasma(g, f, t, S)
    hue = hf_time_cycle(0.08)(g, t)
    mask = val > 0.03
    chars = val2char(val, mask, PAL_DENSE)
    R, G, B = hsv2rgb(hue, np.full_like(val, 0.8), val)
    colors = mkc(R, G, B, g.rows, g.cols)
    return chars, colors
```

### 基于 curses 的渲染（更稳健）

用于带正确缩放处理和输入的完整终端 UI：

```python
import curses

def render_curses(scene_fn, r, fps=24):
    """基于 curses 的实时渲染器，带缩放处理和按键输入。"""
    
    def _main(stdscr):
        curses.start_color()
        curses.use_default_colors()
        curses.curs_set(0)  # 隐藏光标
        stdscr.nodelay(True)  # 非阻塞输入
        
        # 初始化颜色对（curses 支持 256 色）
        # 把 RGB 映射到最近的 curses 颜色对
        color_cache = {}
        next_pair = [1]
        
        def get_color_pair(r, g, b):
            key = (r >> 4, g >> 4, b >> 4)  # 量化以减少颜色对数
            if key not in color_cache:
                if next_pair[0] < curses.COLOR_PAIRS - 1:
                    ci = 16 + (r // 51) * 36 + (g // 51) * 6 + (b // 51)  # 6x6x6 立方体
                    curses.init_pair(next_pair[0], ci, -1)
                    color_cache[key] = next_pair[0]
                    next_pair[0] += 1
                else:
                    return 0
            return curses.color_pair(color_cache[key])
        
        S = {}
        f = {}
        frame_time = 1.0 / fps
        t0 = time.monotonic()
        
        while True:
            t = time.monotonic() - t0
            f = synthesize_features(t)
            
            # 把网格适配到终端尺寸
            max_y, max_x = stdscr.getmaxyx()
            g = r.get_grid_for_size(max_x, max_y)  # 动态网格尺寸
            
            chars, colors = scene_to_terminal(scene_fn, r, f, t, S, g)
            rows, cols = chars.shape
            
            for row in range(min(rows, max_y - 1)):
                for col in range(min(cols, max_x - 1)):
                    ch = chars[row, col]
                    rgb = tuple(colors[row, col])
                    try:
                        stdscr.addch(row, col, ch, get_color_pair(*rgb))
                    except curses.error:
                        pass  # 忽略终端边界外的写入
            
            stdscr.refresh()
            
            # 处理输入
            key = stdscr.getch()
            if key == ord('q'):
                break
            
            time.sleep(max(0, frame_time - (time.monotonic() - t0 - t)))
    
    curses.wrapper(_main)
```

### 终端渲染约束

| 约束 | 取值 | 备注 |
|-----------|-------|-------|
| 实用网格上限 | ~200x60 | 取决于终端尺寸 |
| 颜色支持 | 24-bit（现代）、256（回退）、16（最小） | 检查 `$COLORTERM` 是否为真彩色 |
| 帧率上限 | ~30 fps | 终端 I/O 是瓶颈 |
| 增量更新 | 快 2-5 倍 | 仅当每帧变化单元 <30% 时才值得 |
| SSH 延迟 | 拖垮性能 | 实时渲染仅限本地终端 |

**检测颜色支持：**
```python
import os
def get_terminal_color_depth():
    ct = os.environ.get("COLORTERM", "")
    if ct in ("truecolor", "24bit"):
        return 24
    term = os.environ.get("TERM", "")
    if "256color" in term:
        return 8  # 256 色
    return 4  # 16 色基础 ANSI
```
