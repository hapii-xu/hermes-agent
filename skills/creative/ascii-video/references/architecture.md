# 架构参考

> **另请参阅：** composition.md · effects.md · scenes.md · shaders.md · inputs.md · optimization.md · troubleshooting.md

## 网格系统

### 分辨率预设

```python
RESOLUTION_PRESETS = {
    "landscape":  (1920, 1080),  # 16:9 — YouTube，默认
    "portrait":   (1080, 1920),  # 9:16 — TikTok、Reels、Stories
    "square":     (1080, 1080),  # 1:1  — Instagram 信息流
    "ultrawide":  (2560, 1080),  # 21:9 — 电影感
    "landscape4k":(3840, 2160),  # 16:9 — 4K
    "portrait4k": (2160, 3840),  # 9:16 — 4K 竖屏
}

def get_resolution(preset="landscape", custom=None):
    """返回 (VW, VH) 元组。"""
    if custom:
        return custom
    return RESOLUTION_PRESETS.get(preset, RESOLUTION_PRESETS["landscape"])
```

### 多密度网格

预先初始化多种网格尺寸。按段落切换以获得视觉多样性。网格维度由分辨率自动计算：

**横屏（1920x1080）：**

| 键 | 字号 | 网格（列 x 行） | 用途 |
|-----|-----------|-------------------|-----|
| xs | 8 | 400x108 | 超密集数据场 |
| sm | 10 | 320x83 | 密集细节、雨、星空 |
| md | 16 | 192x56 | 默认均衡、过渡 |
| lg | 20 | 160x45 | 引用/歌词文本（1080p 下可读） |
| xl | 24 | 137x37 | 短引用、大标题 |
| xxl | 40 | 80x22 | 巨型文本、极简 |

**竖屏（1080x1920）：**

| 键 | 字号 | 网格（列 x 行） | 用途 |
|-----|-----------|-------------------|-----|
| xs | 8 | 225x192 | 超密集、高数据列 |
| sm | 10 | 180x148 | 密集细节、垂直雨 |
| md | 16 | 112x100 | 默认均衡 |
| lg | 20 | 90x80 | 可读文本（居中约 30 字符/行） |
| xl | 24 | 75x66 | 短引用、堆叠 |
| xxl | 40 | 45x39 | 巨型文本、极简 |

**正方形（1080x1080）：**

| 键 | 字号 | 网格（列 x 行） | 用途 |
|-----|-----------|-------------------|-----|
| sm | 10 | 180x83 | 密集细节 |
| md | 16 | 112x56 | 默认均衡 |
| lg | 20 | 90x45 | 可读文本 |

**竖屏模式的关键差异：**
- 列数更少（`lg` 时 90 列对比 160 列）—— 文本行必须更短或换行
- 行数多得多（`lg` 时 80 行对比 45 行）—— 垂直堆叠很自然
- 长宽比修正反转：`asp = cw / ch` 仍然有效，但视觉重心是垂直的
- 径向效果会呈现为高椭圆，除非加以修正
- 垂直效果（雨、余烬、火柱）天然得到增强
- 水平效果（频谱条、波形）需要旋转或压缩

**竖屏文本的网格尺寸**：使用 `lg`（20px）承载 2-3 个词的行。舒适的最大行长约为 25-30 字符。对于较长的引用，大胆地拆成多行短文本垂直堆叠 —— 竖屏有充足的垂直空间。`xl`（24px）适合单个词或极短短语。

网格维度：`cols = VW // cell_width`，`rows = VH // cell_height`。

### 字体选择

不要硬编码单一字体。根据项目气质选择字体。网格对齐需要等宽字体，但等宽字体的个性差异很大：

| 字体 | 气质 | 平台 |
|------|-------------|----------|
| Menlo | 干净、中性、Apple 原生 | macOS |
| Monaco | 复古终端、紧凑 | macOS |
| Courier New | 经典打字机、宽 | 跨平台 |
| SF Mono | 现代、紧凑字距 | macOS |
| Consolas | Windows 原生、干净 | Windows |
| JetBrains Mono | 开发者、支持连字 | 需安装 |
| Fira Code | 几何感、现代 | 需安装 |
| IBM Plex Mono | 企业感、权威 | 需安装 |
| Source Code Pro | Adobe、均衡 | 需安装 |

**初始化时探测字体**：探测可用字体并优雅回退：

```python
import platform

def find_font(preferences):
    """按顺序尝试字体，返回第一个存在的字体。"""
    for name, path in preferences:
        if os.path.exists(path):
            return path
    raise FileNotFoundError(f"No monospace font found. Tried: {[p for _,p in preferences]}")

FONT_PREFS_MACOS = [
    ("Menlo", "/System/Library/Fonts/Menlo.ttc"),
    ("Monaco", "/System/Library/Fonts/Monaco.ttf"),
    ("SF Mono", "/System/Library/Fonts/SFNSMono.ttf"),
    ("Courier", "/System/Library/Fonts/Courier.ttc"),
]
FONT_PREFS_LINUX = [
    ("DejaVu Sans Mono", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
    ("Liberation Mono", "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"),
    ("Noto Sans Mono", "/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf"),
    ("Ubuntu Mono", "/usr/share/fonts/truetype/ubuntu/UbuntuMono-R.ttf"),
]
FONT_PREFS_WINDOWS = [
    ("Consolas", r"C:\Windows\Fonts\consola.ttf"),
    ("Courier New", r"C:\Windows\Fonts\cour.ttf"),
    ("Lucida Console", r"C:\Windows\Fonts\lucon.ttf"),
    ("Cascadia Code", os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts\CascadiaCode.ttf")),
    ("Cascadia Mono", os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts\CascadiaMono.ttf")),
]

def _get_font_prefs():
    s = platform.system()
    if s == "Darwin":
        return FONT_PREFS_MACOS
    elif s == "Windows":
        return FONT_PREFS_WINDOWS
    return FONT_PREFS_LINUX

FONT_PREFS = _get_font_prefs()
```

**多字体渲染**：为不同图层使用不同字体（例如背景用等宽字体，叠加文本用更粗的变体）。每个 GridLayer 拥有自己的字体：

```python
grid_bg = GridLayer(find_font(FONT_PREFS), 16)       # 背景
grid_text = GridLayer(find_font(BOLD_PREFS), 20)      # 可读文本
```

### 收集所有字符

在初始化网格之前，收集所有需要预栅格化为位图的字符：

```python
all_chars = set()
for pal in [PAL_DEFAULT, PAL_DENSE, PAL_BLOCKS, PAL_RUNE, PAL_KATA,
            PAL_GREEK, PAL_MATH, PAL_DOTS, PAL_BRAILLE, PAL_STARS,
            PAL_HALFFILL, PAL_HATCH, PAL_BINARY, PAL_MUSIC, PAL_BOX,
            PAL_CIRCUIT, PAL_ARROWS, PAL_HERMES]:  # ... 项目中使用的所有调色板
    all_chars.update(pal)
# 添加任何叠加文本字符
all_chars.update("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .,-:;!?/|")
all_chars.discard(" ")  # 空格从不渲染
```

### GridLayer 初始化

每个网格预计算坐标数组用于向量化效果运算。网格自动适应任何分辨率（横屏、竖屏、正方形）：

```python
class GridLayer:
    def __init__(self, font_path, font_size, vw=None, vh=None):
        """为任意分辨率初始化网格。
        vw, vh：以像素为单位的视频宽/高。默认为全局 VW, VH。"""
        vw = vw or VW; vh = vh or VH
        self.vw = vw; self.vh = vh

        self.font = ImageFont.truetype(font_path, font_size)
        asc, desc = self.font.getmetrics()
        bbox = self.font.getbbox("M")
        self.cw = bbox[2] - bbox[0]  # 字符单元宽度
        self.ch = asc + desc  # 关键：不是 textbbox 的高度

        self.cols = vw // self.cw
        self.rows = vh // self.ch
        self.ox = (vw - self.cols * self.cw) // 2  # 居中
        self.oy = (vh - self.rows * self.ch) // 2

        # 长宽比元数据
        self.aspect = vw / vh  # >1 = 横屏，<1 = 竖屏，1 = 正方形
        self.is_portrait = vw < vh
        self.is_landscape = vw > vh

        # 索引数组
        self.rr = np.arange(self.rows, dtype=np.float32)[:, None]
        self.cc = np.arange(self.cols, dtype=np.float32)[None, :]

        # 极坐标（已做长宽比修正）
        cx, cy = self.cols / 2.0, self.rows / 2.0
        asp = self.cw / self.ch
        self.dx = self.cc - cx
        self.dy = (self.rr - cy) * asp
        self.dist = np.sqrt(self.dx**2 + self.dy**2)
        self.angle = np.arctan2(self.dy, self.dx)

        # 归一化（0-1 范围）—— 用于距离衰减
        self.dx_n = (self.cc - cx) / max(self.cols, 1)
        self.dy_n = (self.rr - cy) / max(self.rows, 1) * asp
        self.dist_n = np.sqrt(self.dx_n**2 + self.dy_n**2)

        # 将所有字符预栅格化为 float32 位图
        self.bm = {}
        for c in all_chars:
            img = Image.new("L", (self.cw, self.ch), 0)
            ImageDraw.Draw(img).text((0, 0), c, fill=255, font=self.font)
            self.bm[c] = np.array(img, dtype=np.float32) / 255.0
```

### 字符渲染循环

性能瓶颈所在。将预栅格化的位图合成到像素画布上：

```python
def render(self, chars, colors, canvas=None):
    if canvas is None:
        canvas = np.zeros((VH, VW, 3), dtype=np.uint8)
    for row in range(self.rows):
        y = self.oy + row * self.ch
        if y + self.ch > VH: break
        for col in range(self.cols):
            c = chars[row, col]
            if c == " ": continue
            x = self.ox + col * self.cw
            if x + self.cw > VW: break
            a = self.bm[c]  # float32 位图
            canvas[y:y+self.ch, x:x+self.cw] = np.maximum(
                canvas[y:y+self.ch, x:x+self.cw],
                (a[:, :, None] * colors[row, col]).astype(np.uint8))
    return canvas
```

使用 `np.maximum` 实现加性混合（更亮的字符覆盖更暗的字符，绝不会变暗）。

### 多图层渲染

将多个网格渲染到同一画布上以营造深度：

```python
canvas = np.zeros((VH, VW, 3), dtype=np.uint8)
canvas = grid_lg.render(bg_chars, bg_colors, canvas)   # 背景层
canvas = grid_md.render(main_chars, main_colors, canvas)  # 主层
canvas = grid_sm.render(detail_chars, detail_colors, canvas)  # 细节叠加层
```

---

## 字符调色板

### 设计原则

字符调色板是 ASCII 视频的主要视觉纹理。它们不仅控制亮度映射，还控制整体视觉感受。要有意识地设计调色板：

- **视觉权重**：字符按其填充的墨迹/像素数量排序。空格始终是索引 0。
- **一致性**：同一调色板内的字符应属于同一视觉家族。
- **密度曲线**：亮度到字符的映射是非线性的。密集调色板（字符多）给出更平滑的渐变；稀疏调色板（5-8 个字符）给出色调分离/图形化外观。
- **渲染兼容性**：调色板中的每个字符都必须存在于字体中。在初始化时测试并移除缺失的字形。

### 调色板库

按视觉家族组织。按项目混搭使用 —— 不要对一切都默认使用 PAL_DEFAULT。

#### 密度 / 亮度调色板
```python
PAL_DEFAULT  = " .`'-:;!><=+*^~?/|(){}[]#&$@%"       # 经典 ASCII 艺术
PAL_DENSE    = " .:;+=xX$#@█"                          # 简单的 11 级渐变
PAL_MINIMAL  = " .:-=+#@"                               # 8 级，图形化
PAL_BINARY   = " █"                                      # 2 级，极高对比度
PAL_GRADIENT = " ░▒▓█"                              # 4 级方块渐变
```

#### Unicode 方块元素
```python
PAL_BLOCKS   = " ░▒▓█▄▀▐▌"                 # 标准方块
PAL_BLOCKS_EXT = " ▖▗▘▙▚▛▜▝▞▟░▒▓█"  # 象限方块（细节更多）
PAL_SHADE    = " ░▒▓█▇▆▅▄▃▂▁"          # 垂直填充递进
```

#### 符号 / 主题
```python
PAL_MATH     = " ·∘∙•°±∓×÷≈≠≡≤≥∞∫∑∏√∇∂∆Ω"    # 数学符号
PAL_BOX      = " ─│┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬"          # 制表符
PAL_CIRCUIT  = " .·─│┌┐└┘┼○●□■∆∇≡"                 # 电路板
PAL_RUNE     = " .ᚠᚢᚦᚱᚷᛁᛇᛒᛖᛚᛞᛟ"                   # 古代北欧如尼文
PAL_ALCHEMIC = " ☉☽♀♂♃♄♅♆♇♈♉♊♋"            # 行星/炼金术符号
PAL_ZODIAC   = " ♈♉♊♋♌♍♎♏♐♑♒♓"            # 黄道带
PAL_ARROWS   = " ←↑→↓↔↕↖↗↘↙↩↪↻➡"             # 方向箭头
PAL_MUSIC    = " ♪♫♬♩♭♮♯○●"                       # 音乐记谱
```

#### 文字 / 书写系统
```python
PAL_KATA     = " ·ｦｧｨｩｪｫｬｭｮｯｰｱｲｳｴｵｶｷ"          # 半角片假名（矩阵雨）
PAL_GREEK    = " αβγδεζηθικλμνξπρστφψω"    # 希腊文小写
PAL_CYRILLIC = " абвгдежзиклмнопрстуфхцчш"  # 西里尔文小写
PAL_ARABIC   = " ابتثجحخدذرزسشصضط"       # 阿拉伯字母（独立形式）
```

#### 点 / 点状递进
```python
PAL_DOTS     = " ⋅∘∙●◉◎◆✦★"                   # 点尺寸递进
PAL_BRAILLE  = " ⠁⠂⠃⠄⠅⠆⠇⠈⠉⠊⠋⠌⠍⠎⠏⠐⠑⠒⠓⠔⠕⠖⠗⠘⠙⠚⠛⠜⠝⠞⠟⠿"  # 盲文图案
PAL_STARS    = " ·✧✦✩✨★✶✳✸"               # 星形递进
PAL_HALFFILL = " ◔◑◕◐◒◓◖◗◙"               # 方向半填充递进
PAL_HATCH    = " ▣▤▥▦▧▨▩"                     # 交叉阴影密度渐变
```

#### 项目专用（示例 —— 按项目发明新的）
```python
PAL_HERMES   = " .·~=≈∞⚡☿✦★⊕◊◆▲▼●■"   # 神话/科技混合
PAL_OCEAN    = " ~≈≈≈∼⌇≈≋≌≈"                       # 水/波浪字符
PAL_ORGANIC  = " .°∘•◦◉❂✿❁❃"                 # 生长/植物
PAL_MACHINE  = " _─│┌┐┼≡■█▓▒░"             # 机械/工业
```

### 创建自定义调色板

为项目设计时，从内容主题出发构建调色板：

1. **选择一个视觉家族**（点、方块、符号、文字）
2. **按视觉权重排序** —— 在目标字号下渲染每个字符，统计点亮像素数，升序排列
3. **在目标网格尺寸下测试** —— 某些字符在小尺寸下会塌缩成色块
4. **在字体中验证** —— 移除字体无法渲染的字符：

```python
def validate_palette(pal, font):
    """移除字体无法渲染的字符。"""
    valid = []
    for c in pal:
        if c == " ":
            valid.append(c)
            continue
        img = Image.new("L", (20, 20), 0)
        ImageDraw.Draw(img).text((0, 0), c, fill=255, font=font)
        if np.array(img).max() > 0:  # 字符确实渲染出了东西
            valid.append(c)
    return "".join(valid)
```

### 将数值映射到字符

```python
def val2char(v, mask, pal=PAL_DEFAULT):
    """使用调色板将浮点数组 (0-1) 映射到字符数组。"""
    n = len(pal)
    idx = np.clip((v * n).astype(int), 0, n - 1)
    out = np.full(v.shape, " ", dtype="U1")
    for i, ch in enumerate(pal):
        out[mask & (idx == i)] = ch
    return out
```

**非线性映射**用于不同的视觉曲线：

```python
def val2char_gamma(v, mask, pal, gamma=1.0):
    """伽马校正后的调色板映射。gamma<1 = 更亮，gamma>1 = 更暗。"""
    v_adj = np.power(np.clip(v, 0, 1), gamma)
    return val2char(v_adj, mask, pal)

def val2char_step(v, mask, pal, thresholds):
    """自定义阈值映射。thresholds = 浮点断点列表。"""
    out = np.full(v.shape, pal[0], dtype="U1")
    for i, thr in enumerate(thresholds):
        out[mask & (v > thr)] = pal[min(i + 1, len(pal) - 1)]
    return out
```

---

## 颜色系统

### HSV->RGB（向量化）

所有颜色计算在 HSV 空间进行以便直观控制，在渲染时转换：

```python
def hsv2rgb(h, s, v):
    """向量化 HSV->RGB。h,s,v 为 numpy 数组。返回 (R,G,B) uint8 数组。"""
    h = h % 1.0
    c = v * s; x = c * (1 - np.abs((h*6) % 2 - 1)); m = v - c
    # ... 6 扇区赋值 ...
    return (np.clip((r+m)*255, 0, 255).astype(np.uint8),
            np.clip((g+m)*255, 0, 255).astype(np.uint8),
            np.clip((b+m)*255, 0, 255).astype(np.uint8))
```

### 颜色映射策略

不要默认使用单一策略。根据视觉意图选择：

| 策略 | 色相来源 | 效果 | 适用场景 |
|----------|------------|--------|----------|
| 角度映射 | `g.angle / (2*pi)` | 围绕中心的彩虹 | 径向效果、万花筒 |
| 距离映射 | `g.dist_n * 0.3` | 从中心向外渐变 | 隧道、深度效果 |
| 频率映射 | `f["cent"] * 0.2` | 音色色彩偏移 | 音频响应 |
| 数值映射 | `val * 0.15` | 依赖亮度的色相 | 火焰、热力图 |
| 时间循环 | `t * rate` | 缓慢色彩旋转 | 氛围、舒缓 |
| 源采样 | 视频帧像素颜色 | 保留原始颜色 | 视频转 ASCII |
| 调色板索引 | 离散颜色查表 | 平面图形风格 | 复古、像素艺术 |
| 温度 | 暖色与冷色之间混合 | 情感基调 | 情绪驱动场景 |
| 互补色 | `hue` 和 `hue + 0.5` | 高对比 | 醒目、戏剧化 |
| 三色组 | `hue`、`hue + 0.33`、`hue + 0.66` | 鲜艳、平衡 | 迷幻 |
| 类似色 | `hue +/- 0.08` | 和谐、微妙 | 优雅、协调 |
| 单色 | 固定色相，变化 S 和 V | 克制、聚焦 | 黑色电影、极简 |

### 颜色调色板（离散 RGB）

用于非 HSV 工作流 —— 直接的 RGB 颜色集，适合图形/复古外观：

```python
# 命名颜色调色板 —— 用于平面/图形风格或逐字符着色
COLORS_NEON = [(255,0,102), (0,255,153), (102,0,255), (255,255,0), (0,204,255)]
COLORS_PASTEL = [(255,179,186), (255,223,186), (255,255,186), (186,255,201), (186,225,255)]
COLORS_MONO_GREEN = [(0,40,0), (0,80,0), (0,140,0), (0,200,0), (0,255,0)]
COLORS_MONO_AMBER = [(40,20,0), (80,50,0), (140,90,0), (200,140,0), (255,191,0)]
COLORS_CYBERPUNK = [(255,0,60), (0,255,200), (180,0,255), (255,200,0)]
COLORS_VAPORWAVE = [(255,113,206), (1,205,254), (185,103,255), (5,255,161)]
COLORS_EARTH = [(86,58,26), (139,90,43), (189,154,91), (222,193,136), (245,230,193)]
COLORS_ICE = [(200,230,255), (150,200,240), (100,170,230), (60,130,210), (30,80,180)]
COLORS_BLOOD = [(80,0,0), (140,10,10), (200,20,20), (255,50,30), (255,100,80)]
COLORS_FOREST = [(10,30,10), (20,60,15), (30,100,20), (50,150,30), (80,200,50)]

def rgb_palette_map(val, mask, palette):
    """将浮点数组 (0-1) 映射到离散调色板的 RGB 颜色。"""
    n = len(palette)
    idx = np.clip((val * n).astype(int), 0, n - 1)
    R = np.zeros(val.shape, dtype=np.uint8)
    G = np.zeros(val.shape, dtype=np.uint8)
    B = np.zeros(val.shape, dtype=np.uint8)
    for i, (r, g, b) in enumerate(palette):
        m = mask & (idx == i)
        R[m] = r; G[m] = g; B[m] = b
    return R, G, B
```

### OKLAB 颜色空间（感知均匀）

HSV 的色相在感知上不均匀：绿色占据的视觉范围远大于蓝色。OKLAB / OKLCH 提供感知均匀的色彩步进 —— 0.1 的色相增量无论起始色相如何，看起来都同样不同。在以下场景使用 OKLAB：
- 渐变插值（不会出现不想要的中间色相）
- 配色和谐生成（感知均衡的调色板）
- 时间上的平滑颜色过渡

```python
# --- sRGB <-> 线性 sRGB ---

def srgb_to_linear(c):
    """将 sRGB [0,1] 转换为线性光。c：float32 数组。"""
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)

def linear_to_srgb(c):
    """将线性光转换为 sRGB [0,1]。"""
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(np.maximum(c, 0), 1/2.4) - 0.055)

# --- 线性 sRGB <-> OKLAB ---

def linear_rgb_to_oklab(r, g, b):
    """线性 sRGB 转 OKLAB。r,g,b：float32 数组 [0,1]。
    返回 (L, a, b)，其中 L=[0,1]，a,b 约为 [-0.4, 0.4]。"""
    l_ = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m_ = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s_ = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_c = np.cbrt(l_); m_c = np.cbrt(m_); s_c = np.cbrt(s_)
    L = 0.2104542553 * l_c + 0.7936177850 * m_c - 0.0040720468 * s_c
    a = 1.9779984951 * l_c - 2.4285922050 * m_c + 0.4505937099 * s_c
    b_ = 0.0259040371 * l_c + 0.7827717662 * m_c - 0.8086757660 * s_c
    return L, a, b_

def oklab_to_linear_rgb(L, a, b):
    """OKLAB 转线性 sRGB。返回 (r, g, b) float32 数组 [0,1]。"""
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l_c = l_ ** 3; m_c = m_ ** 3; s_c = s_ ** 3
    r = +4.0767416621 * l_c - 3.3077115913 * m_c + 0.2309699292 * s_c
    g = -1.2684380046 * l_c + 2.6097574011 * m_c - 0.3413193965 * s_c
    b_ = -0.0041960863 * l_c - 0.7034186147 * m_c + 1.7076147010 * s_c
    return np.clip(r, 0, 1), np.clip(g, 0, 1), np.clip(b_, 0, 1)

# --- 便捷：sRGB uint8 <-> OKLAB ---

def rgb_to_oklab(R, G, B):
    """sRGB uint8 数组转 OKLAB。"""
    r = srgb_to_linear(R.astype(np.float32) / 255.0)
    g = srgb_to_linear(G.astype(np.float32) / 255.0)
    b = srgb_to_linear(B.astype(np.float32) / 255.0)
    return linear_rgb_to_oklab(r, g, b)

def oklab_to_rgb(L, a, b):
    """OKLAB 转 sRGB uint8 数组。"""
    r, g, b_ = oklab_to_linear_rgb(L, a, b)
    R = np.clip(linear_to_srgb(r) * 255, 0, 255).astype(np.uint8)
    G = np.clip(linear_to_srgb(g) * 255, 0, 255).astype(np.uint8)
    B = np.clip(linear_to_srgb(b_) * 255, 0, 255).astype(np.uint8)
    return R, G, B

# --- OKLCH（OKLAB 的柱坐标形式） ---

def oklab_to_oklch(L, a, b):
    """OKLAB 转 OKLCH。返回 (L, C, H)，其中 H 在 [0, 1]（归一化）。"""
    C = np.sqrt(a**2 + b**2)
    H = (np.arctan2(b, a) / (2 * np.pi)) % 1.0
    return L, C, H

def oklch_to_oklab(L, C, H):
    """OKLCH 转 OKLAB。H 在 [0, 1]。"""
    angle = H * 2 * np.pi
    a = C * np.cos(angle)
    b = C * np.sin(angle)
    return L, a, b
```

### 渐变插值（OKLAB 对比 HSV）

通过 OKLAB 插值颜色可以避免 HSV 产生的色相绕行：

```python
def lerp_oklab(color_a, color_b, t_array):
    """通过 OKLAB 在两个 sRGB 颜色之间插值。
    color_a, color_b：(R, G, B) 元组，0-255
    t_array：float32 数组 [0,1] —— 每像素的插值参数。
    返回 (R, G, B) uint8 数组。"""
    La, aa, ba = rgb_to_oklab(
        np.full_like(t_array, color_a[0], dtype=np.uint8),
        np.full_like(t_array, color_a[1], dtype=np.uint8),
        np.full_like(t_array, color_a[2], dtype=np.uint8))
    Lb, ab, bb = rgb_to_oklab(
        np.full_like(t_array, color_b[0], dtype=np.uint8),
        np.full_like(t_array, color_b[1], dtype=np.uint8),
        np.full_like(t_array, color_b[2], dtype=np.uint8))
    L = La + (Lb - La) * t_array
    a = aa + (ab - aa) * t_array
    b = ba + (bb - ba) * t_array
    return oklab_to_rgb(L, a, b)

def lerp_oklch(color_a, color_b, t_array, short_path=True):
    """通过 OKLCH 插值（保留色度，平滑色相路径）。
    short_path：沿色相轮较短的弧。"""
    La, aa, ba = rgb_to_oklab(
        np.full_like(t_array, color_a[0], dtype=np.uint8),
        np.full_like(t_array, color_a[1], dtype=np.uint8),
        np.full_like(t_array, color_a[2], dtype=np.uint8))
    Lb, ab, bb = rgb_to_oklab(
        np.full_like(t_array, color_b[0], dtype=np.uint8),
        np.full_like(t_array, color_b[1], dtype=np.uint8),
        np.full_like(t_array, color_b[2], dtype=np.uint8))
    L1, C1, H1 = oklab_to_oklch(La, aa, ba)
    L2, C2, H2 = oklab_to_oklch(Lb, ab, bb)
    # 最短色相路径
    if short_path:
        dh = H2 - H1
        dh = np.where(dh > 0.5, dh - 1.0, np.where(dh < -0.5, dh + 1.0, dh))
        H = (H1 + dh * t_array) % 1.0
    else:
        H = H1 + (H2 - H1) * t_array
    L = L1 + (L2 - L1) * t_array
    C = C1 + (C2 - C1) * t_array
    Lout, aout, bout = oklch_to_oklab(L, C, H)
    return oklab_to_rgb(Lout, aout, bout)
```

### 配色和谐生成

从种子色自动生成和谐的调色板：

```python
def harmony_complementary(seed_rgb):
    """两种颜色：种子色 + 对立色相。"""
    L, a, b = rgb_to_oklab(np.array([seed_rgb[0]]), np.array([seed_rgb[1]]), np.array([seed_rgb[2]]))
    _, C, H = oklab_to_oklch(L, a, b)
    return [seed_rgb, _oklch_to_srgb_tuple(L[0], C[0], (H[0] + 0.5) % 1.0)]

def harmony_triadic(seed_rgb):
    """三种颜色：种子色 + 两个 120 度偏移色。"""
    L, a, b = rgb_to_oklab(np.array([seed_rgb[0]]), np.array([seed_rgb[1]]), np.array([seed_rgb[2]]))
    _, C, H = oklab_to_oklch(L, a, b)
    return [seed_rgb,
            _oklch_to_srgb_tuple(L[0], C[0], (H[0] + 0.333) % 1.0),
            _oklch_to_srgb_tuple(L[0], C[0], (H[0] + 0.667) % 1.0)]

def harmony_analogous(seed_rgb, spread=0.08, n=5):
    """围绕种子色相均匀分布的 N 种颜色。"""
    L, a, b = rgb_to_oklab(np.array([seed_rgb[0]]), np.array([seed_rgb[1]]), np.array([seed_rgb[2]]))
    _, C, H = oklab_to_oklch(L, a, b)
    offsets = np.linspace(-spread * (n-1)/2, spread * (n-1)/2, n)
    return [_oklch_to_srgb_tuple(L[0], C[0], (H[0] + off) % 1.0) for off in offsets]

def harmony_split_complementary(seed_rgb, split=0.08):
    """三种颜色：种子色 + 两个在互补色两侧的颜色。"""
    L, a, b = rgb_to_oklab(np.array([seed_rgb[0]]), np.array([seed_rgb[1]]), np.array([seed_rgb[2]]))
    _, C, H = oklab_to_oklch(L, a, b)
    comp = (H[0] + 0.5) % 1.0
    return [seed_rgb,
            _oklch_to_srgb_tuple(L[0], C[0], (comp - split) % 1.0),
            _oklch_to_srgb_tuple(L[0], C[0], (comp + split) % 1.0)]

def harmony_tetradic(seed_rgb):
    """四种颜色：两对互补色，偏移 90 度。"""
    L, a, b = rgb_to_oklab(np.array([seed_rgb[0]]), np.array([seed_rgb[1]]), np.array([seed_rgb[2]]))
    _, C, H = oklab_to_oklch(L, a, b)
    return [seed_rgb,
            _oklch_to_srgb_tuple(L[0], C[0], (H[0] + 0.25) % 1.0),
            _oklch_to_srgb_tuple(L[0], C[0], (H[0] + 0.5) % 1.0),
            _oklch_to_srgb_tuple(L[0], C[0], (H[0] + 0.75) % 1.0)]

def _oklch_to_srgb_tuple(L, C, H):
    """辅助：单个 OKLCH -> sRGB (R,G,B) 整数元组。"""
    La = np.array([L]); Ca = np.array([C]); Ha = np.array([H])
    Lo, ao, bo = oklch_to_oklab(La, Ca, Ha)
    R, G, B = oklab_to_rgb(Lo, ao, bo)
    return (int(R[0]), int(G[0]), int(B[0]))
```

### OKLAB 色相场

`hf_*` 生成器的直接替代品，产生感知均匀的色相变化：

```python
def hf_oklch_angle(offset=0.0, chroma=0.12, lightness=0.7):
    """将 OKLCH 色相映射到距中心的角度。感知均匀的彩虹。
    返回 (R, G, B) uint8 颜色数组而非浮点色相。
    注意：与 _render_vf_rgb() 变体配合使用，而非标准 _render_vf()。"""
    def fn(g, f, t, S):
        H = (g.angle / (2 * np.pi) + offset + t * 0.05) % 1.0
        L = np.full_like(H, lightness)
        C = np.full_like(H, chroma)
        Lo, ao, bo = oklch_to_oklab(L, C, H)
        R, G, B = oklab_to_rgb(Lo, ao, bo)
        return mkc(R, G, B, g.rows, g.cols)
    return fn
```

### 合成辅助函数

```python
def mkc(R, G, B, rows, cols):
    """将 3 个 uint8 数组打包成 (rows, cols, 3) 颜色数组。"""
    o = np.zeros((rows, cols, 3), dtype=np.uint8)
    o[:,:,0] = R; o[:,:,1] = G; o[:,:,2] = B
    return o

def layer_over(base_ch, base_co, top_ch, top_co):
    """将顶层合成到底层上。非空格字符覆盖。"""
    m = top_ch != " "
    base_ch[m] = top_ch[m]; base_co[m] = top_co[m]
    return base_ch, base_co

def layer_blend(base_co, top_co, alpha):
    """将顶层颜色按 alpha 混合到底层。alpha 是浮点数组 (0-1) 或标量。"""
    if isinstance(alpha, (int, float)):
        alpha = np.full(base_co.shape[:2], alpha, dtype=np.float32)
    a = alpha[:,:,None]
    return np.clip(base_co * (1 - a) + top_co * a, 0, 255).astype(np.uint8)

def stamp(ch, co, text, row, col, color=(255,255,255)):
    """在指定位置写入文本字符串。"""
    for i, c in enumerate(text):
        cc = col + i
        if 0 <= row < ch.shape[0] and 0 <= cc < ch.shape[1]:
            ch[row, cc] = c; co[row, cc] = color
```

---

## 段落系统

将时间区间映射到效果函数 + 着色器配置 + 网格尺寸：

```python
SECTIONS = [
    (0.0, "void"), (3.94, "starfield"), (21.0, "matrix"),
    (46.0, "drop"), (130.0, "glitch"), (187.0, "outro"),
]

FX_DISPATCH = {"void": fx_void, "starfield": fx_starfield, ...}
SECTION_FX = {"void": {"vignette": 0.3, "bloom": 170}, ...}
SECTION_GRID = {"void": "md", "starfield": "sm", "drop": "lg", ...}
SECTION_MIRROR = {"drop": "h", "bass_rings": "quad"}

def get_section(t):
    sec = SECTIONS[0][1]
    for ts, name in SECTIONS:
        if t >= ts: sec = name
    return sec
```

---

## 并行编码

将帧拆分到 N 个 worker。每个 worker 将原始 RGB 通过管道传给各自的 ffmpeg 子进程：

```python
def render_batch(batch_id, frame_start, frame_end, features, seg_path):
    r = Renderer()
    cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{VW}x{VH}", "-r", str(FPS), "-i", "pipe:0",
           "-c:v", "libx264", "-preset", "fast", "-crf", "18",
           "-pix_fmt", "yuv420p", seg_path]

    # 关键：stderr 输出到文件，而非管道
    stderr_fh = open(os.path.join(workdir, f"err_{batch_id:02d}.log"), "w")
    pipe = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=stderr_fh)

    for fi in range(frame_start, frame_end):
        t = fi / FPS
        sec = get_section(t)
        f = {k: float(features[k][fi]) for k in features}
        ch, co = FX_DISPATCH[sec](r, f, t)
        canvas = r.render(ch, co)
        canvas = apply_mirror(canvas, sec, f)
        canvas = apply_shaders(canvas, sec, f, t)
        pipe.stdin.write(canvas.tobytes())

    pipe.stdin.close()
    pipe.wait()
    stderr_fh.close()
```

拼接分段 + 混合音频：

```python
# 写入 concat 文件
with open(concat_path, "w") as cf:
    for seg in segments:
        cf.write(f"file '{seg}'\n")

subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_path,
                "-i", audio_path, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-shortest", output_path])
```

## 效果函数契约

### v2 协议（当前）

每个场景函数：`(r, f, t, S) -> canvas_uint8` —— 其中 `r` = Renderer，`f` = 特征字典，`t` = 时间浮点数，`S` = 持久状态字典

```python
def fx_example(r, f, t, S):
    """场景函数返回完整像素画布 (uint8 H,W,3)。
    场景对多网格渲染和像素级合成拥有完全控制权。
    """
    # 以不同网格密度渲染多个图层
    canvas_a = _render_vf(r, "md", vf_plasma, hf_angle(0.0), PAL_DENSE, f, t, S)
    canvas_b = _render_vf(r, "sm", vf_vortex, hf_time_cycle(0.1), PAL_RUNE, f, t, S)

    # 像素级混合
    result = blend_canvas(canvas_a, canvas_b, "screen", 0.8)
    return result
```

完整的场景协议、Renderer 类、`_render_vf()` 辅助函数和完整场景示例见 `references/scenes.md`。

混合模式、色调映射、反馈缓冲和多网格合成见 `references/composition.md`。

### v1 协议（遗留）

使用单一网格的简单场景仍可返回 `(chars, colors)` 并让调用者处理渲染，但所有新代码推荐使用 v2 画布协议。

```python
def fx_simple(r, f, t, S):
    g = r.get_grid("md")
    val = np.sin(g.dist * 0.1 - t * 3) * f.get("bass", 0.3) * 2
    val = np.clip(val, 0, 1); mask = val > 0.03
    ch = val2char(val, mask, PAL_DEFAULT)
    R, G, B = hsv2rgb(np.full_like(val, 0.6), np.full_like(val, 0.7), val)
    co = mkc(R, G, B, g.rows, g.cols)
    return g.render(ch, co)  # 直接返回画布
```

### 持久状态

需要跨帧状态的效果（粒子、雨柱）使用 `S` 字典参数（即 `r.S` —— 同一对象，但显式传入以增强可读性）：

```python
def fx_with_state(r, f, t, S):
    if "particles" not in S:
        S["particles"] = initialize_particles()
    update_particles(S["particles"])
    # ...
```

状态在单个场景/片段内跨帧持久存在。每个 worker 进程（以及每个场景）拥有各自独立的状态。

### 辅助函数

```python
def hsv2rgb_scalar(h, s, v):
    """单值 HSV 转 RGB。返回 0-255 整数 (R, G, B) 元组。"""
    h = h % 1.0
    c = v * s; x = c * (1 - abs((h * 6) % 2 - 1)); m = v - c
    if h * 6 < 1:   r, g, b = c, x, 0
    elif h * 6 < 2:  r, g, b = x, c, 0
    elif h * 6 < 3:  r, g, b = 0, c, x
    elif h * 6 < 4:  r, g, b = 0, x, c
    elif h * 6 < 5:  r, g, b = x, 0, c
    else:             r, g, b = c, 0, x
    return (int((r+m)*255), int((g+m)*255), int((b+m)*255))

def log(msg):
    """打印带时间戳的日志消息。"""
    print(msg, flush=True)
```
