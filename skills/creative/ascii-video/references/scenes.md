# 场景系统与创意编排

> **另请参阅：** architecture.md · composition.md · effects.md · shaders.md

## 场景设计哲学

场景是叙事单元，而不是特效演示。每个场景都需要：
- 一个**概念** —— 视觉上正在发生什么？不是「等离子 + 圆环」，而是「从虚无中浮现」或「结晶化」
- 一条**弧线** —— 它在持续时间内如何变化？铺陈、衰减、变形、揭示？
- 一个**角色** —— 它如何服务于更大的视频叙事？开场张力、能量峰值、收束？

下面的设计模式提供了编排技法。场景示例则展示了它们在不同复杂度下的实际运用。协议部分涵盖所有场景必须遵循的技术契约。

好的场景设计从概念出发，然后选择服务于该概念的特效和参数。设计模式部分展示*如何*有意图地组织图层。示例部分展示每种复杂度下完整可运行的场景。协议部分涵盖所有场景必须遵循的技术契约。

---

## 场景设计模式

用于组织场景、让它们显得有意图而非随机的更高阶模式。这些模式使用既有的构建块（值场、混合模式、着色器、反馈），但以编排意图来组织它们。

## 图层层级

每个场景都应有清晰、各司其职的视觉图层：

| 图层 | 网格 | 亮度 | 用途 |
|-------|------|-----------|---------|
| **背景** | xs 或 sm（密集） | 0.1–0.25 | 氛围、纹理。绝不与内容抢戏。 |
| **内容** | md（均衡） | 0.4–0.8 | 主要视觉构想。承载场景的概念。 |
| **点缀** | lg 或 sm（稀疏） | 0.5–1.0（稀疏覆盖） | 高光、点睛、稀疏的亮点。 |

背景设定基调。内容图层是场景*要表达的东西*。点缀在不喧宾夺主的前提下增加视觉趣味。

```python
def fx_example(r, f, t, S):
    local = t
    progress = min(local / 5.0, 1.0)

    g_bg = r.get_grid("sm")
    g_main = r.get_grid("md")
    g_accent = r.get_grid("lg")

    # --- 背景：暗淡的氛围 ---
    bg_val = vf_smooth_noise(g_bg, f, t * 0.3, S, octaves=2, bri=0.15)
    # ... 将 bg 渲染到画布

    # --- 内容：主要的视觉构想 ---
    content_val = vf_spiral(g_main, f, t, S, n_arms=n_arms, tightness=tightness)
    # ... 将内容渲染到画布之上

    # --- 点缀：稀疏的高光 ---
    accent_val = vf_noise_static(g_accent, f, t, S, density=0.05)
    # ... 将点缀渲染到最上层

    return canvas
```

## 有方向性的参数弧线

参数应当在场景持续时间内*有所去向* —— 而不是用 `sin(t * N)` 漫无目的地来回震荡。

**糟糕：** `twist = 3.0 + 2.0 * math.sin(t * 0.6)` —— 来回摆动，显得漫无目的。

**良好：** `twist = 2.0 + progress * 5.0` —— 起步温和，结尾强烈。场景在*层层累积*。

用 `progress = min(local / duration, 1.0)`（场景内 0→1）来驱动有方向的变化：

| 模式 | 公式 | 感觉 |
|---------|---------|------|
| 线性递增 | `progress * range` | 稳定的累积 |
| 缓出（ease-out） | `1 - (1 - progress) ** 2` | 快速起步，温和收尾 |
| 缓入（ease-in） | `progress ** 2` | 缓慢起步，逐渐加速 |
| 阶梯揭示 | `np.clip((progress - 0.5) / 0.25, 0, 1)` | 直到 50% 才出现，然后淡入 |
| 累积 + 平台 | `min(1.0, progress * 1.5)` | 在 67% 达到满值，之后保持 |

震荡适合*次要*参数（饱和度微动、色相漂移）。但场景的*定义性*参数应当有方向。

### 有方向性弧线的示例

| 场景概念 | 参数 | 弧线 |
|--------------|-----------|-----|
| 浮现 | 圆环半径 | 0 → max（缓出） |
| 破碎 | Voronoi 单元数 | 8 → 38（线性） |
| 坠落 | 隧道速度 | 2.0 → 10.0（线性） |
| 曼陀罗 | 形状复杂度 | ring → +polygon → +star → +rosette（阶梯揭示） |
| 渐强 | 图层数 | 1 → 7（错峰入场） |
| 熵增 | 几何可见度 | 1.0 → 0.0（被吞噬） |

## 场景概念

每个场景都应围绕一个*视觉构想*构建，而不是一个特效名。

**糟糕：** "fx_plasma_cascade" —— 以特效命名。没有概念。
**良好：** "fx_emergence" —— 一个光点扩展成一片光场。名字告诉你*发生了什么*。

好的场景概念具备：
1. 一个**视觉隐喻**（浮现、坠落、碰撞、熵增）
2. 一条**有方向性的弧线**（事物从 A 变到 B，而非震荡）
3. **有动机的图层选择**（每个图层都服务于概念）
4. **有动机的反馈**（变换方向与隐喻一致）

| 概念 | 隐喻 | 反馈变换 | 原因 |
|---------|----------|-------------------|-----|
| 浮现 | 诞生、扩展 | 缩小（zoom-out） | 过去的画面向外扩张 |
| 坠落 | 下落、加速 | 放大（zoom-in） | 过去的画面向中心冲来 |
| 烈焰 | 升腾的火 | 向上偏移（shift-up） | 过去的画面随火焰上升 |
| 熵增 | 衰败、消散 | 无 | 干净、无残留 —— 事物直接消失 |
| 渐强 | 累积 | 放大 + 色相偏移 | 一切都在叠加并流转 |

## 编排技法

### 反向旋转的双系统

同一特效的两个实例朝相反方向旋转，产生视觉干涉：

```python
# 主螺旋（顺时针）
s1_val = vf_spiral(g_main, f, t * 1.5, S, n_arms=n_arms_1, tightness=tightness_1)

# 反向旋转的螺旋（通过负时间实现逆时针）
s2_val = vf_spiral(g_accent, f, -t * 1.2, S, n_arms=n_arms_2, tightness=tightness_2)

# 屏幕混合在交叉点产生明亮的干涉
canvas = blend_canvas(canvas_with_s1, c2, "screen", 0.7)
```

适用于螺旋、漩涡、圆环。反向旋转会产生不断变换的干涉图样。

### 波浪碰撞

两道波前从两侧汇聚，在一个碰撞点相遇：

```python
collision_phase = abs(progress - 0.5) * 2  # 1→0→1（碰撞时为 0）

# 波 A 从左侧逼近
offset_a = (1 - progress) * g.cols * 0.4
wave_a = np.sin((g.cc + offset_a) * 0.08 + t * 2) * 0.5 + 0.5

# 波 B 从右侧逼近
offset_b = -(1 - progress) * g.cols * 0.4
wave_b = np.sin((g.cc + offset_b) * 0.08 - t * 2) * 0.5 + 0.5

# 干涉在碰撞处达到峰值
combined = wave_a * 0.5 + wave_b * 0.5 + np.abs(wave_a - wave_b) * (1 - collision_phase) * 0.5
```

### 渐进式破碎

Voronoi 的单元数随时间增加 —— 视觉上的碎裂：

```python
n_pts = int(8 + progress * 30)  # 8 个单元 → 38 个单元
# 预生成足够多的点，再切片到 n_pts
px = base_x[:n_pts] + np.sin(t * 0.3 + np.arange(n_pts) * 0.7) * (3 + progress * 3)
```

边缘辉光的宽度也可以随 progress 增大，以强调裂纹。

### 熵增 / 吞噬

干净的几何图样被一个有机过程逐渐吞没：

```python
# 几何淡出
geo_val = clean_pattern * max(0.05, 1.0 - progress * 0.9)

# 有机过程生长
rd_val = vf_reaction_diffusion(g, f, t, S) * min(1.0, progress * 1.5)

# 先渲染几何，有机图样叠加在上 —— 有机吞噬几何
```

### 错峰图层入场（渐强）

图层逐一入场，累积到压倒性的密度：

```python
def layer_strength(enter_t, ramp=1.5):
    """在 enter_t 之前为 0.0，在 ramp 秒内升至 1.0。"""
    return max(0.0, min(1.0, (local - enter_t) / ramp))

# 图层 1：始终存在
s1 = layer_strength(0.0)
# 图层 2：2 秒入场
s2 = layer_strength(2.0)
# 图层 3：4 秒入场
s3 = layer_strength(4.0)
# ……等等

# 每个图层使用不同的特效、网格、色板和混合模式
# 图层之间用屏幕混合，让光线累积
```

对一个 15 秒的渐强，每 2 秒入场一个、共 7 个图层效果不错。使用不同的混合模式（多数用 screen，能量用 add，最后的覆盖用 colordodge）。

## 场景排序

对于多场景连播或完整视频：
- **相邻场景之间 mood 要有变化** —— 不要把两个宁静场景挨在一起
- **顺序随机化**，而不是按类型分组 —— 避免「特效演示」的感觉
- **以最强的场景收尾** —— 渐强或某个有明显回报的场景
- **以能量开场** —— 在前 2 秒抓住注意力

---

## 场景协议

场景是顶层的创意单元。每个场景都是一个有自身特效函数、着色器链、反馈配置和色调映射 gamma 的时间限定片段。

### 场景协议（v2）

### 函数签名

```python
def fx_scene_name(r, f, t, S) -> canvas:
    """
    Args:
        r: Renderer 实例 —— 通过 r.get_grid("sm") 访问多个网格
        f: 音频/视频特征字典，所有值归一化到 [0, 1]
        t: 以秒为单位的时间 —— 场景内本地时间（场景开始处为 0.0）
        S: 用于持久状态（粒子、雨列等）的字典

    Returns:
        canvas: numpy uint8 数组，形状 (VH, VW, 3) —— 完整像素帧
    """
```

**本地时间约定：** 场景函数收到的 `t` 从场景第一帧的 0.0 开始，无论该场景出现在时间线的何处。渲染循环会在调用函数前减去场景的起始时间：

```python
# 在 render_clip 中：
t_local = fi / FPS - scene_start
canvas = fx_fn(r, feat, t_local, S)
```

这让场景可以在不修改代码的情况下重新排序。场景进度计算为：

```python
progress = min(t / scene_duration, 1.0)  # 场景内 0→1
```

这取代了 v1 协议中场景返回 `(chars, colors)` 元组的做法。v2 协议让场景完全掌控内部的多网格渲染和像素级合成。

### Renderer 类

```python
class Renderer:
    def __init__(self):
        self.grids = {}   # 懒加载的网格缓存
        self.g = None      # 「活动」网格（向后兼容）
        self.S = {}        # 持久状态字典

    def get_grid(self, key):
        """按尺寸 key 获取或创建 GridLayer。"""
        if key not in self.grids:
            sizes = {"xs": 8, "sm": 10, "md": 16, "lg": 20, "xl": 24, "xxl": 40}
            self.grids[key] = GridLayer(FONT_PATH, sizes[key])
        return self.grids[key]

    def set_grid(self, key):
        """设置活动网格（遗留用法）。多网格场景请用 get_grid()。"""
        self.g = self.get_grid(key)
        return self.g
```

**与 v1 的关键区别**：场景调用 `r.get_grid("sm")`、`r.get_grid("lg")` 等来访问多个网格。每个网格都是懒加载并缓存的。`set_grid()` 方法仍可用于单网格场景。

### 最小场景（单网格）

```python
def fx_simple_rings(r, f, t, S):
    """单网格场景：圆环，色相按距离映射。"""
    canvas = _render_vf(r, "md",
        lambda g, f, t, S: vf_rings(g, f, t, S, n_base=8, spacing_base=3),
        hf_distance(0.3, 0.02), PAL_STARS, f, t, S, sat=0.85)
    return canvas
```

### 标准场景（双网格 + 混合）

```python
def fx_tunnel_ripple(r, f, t, S):
    """双网格场景：隧道深度与波纹做排除混合。"""
    canvas_a = _render_vf(r, "md",
        lambda g, f, t, S: vf_tunnel(g, f, t, S, speed=5.0, complexity=10) * 1.3,
        hf_distance(0.55, 0.02), PAL_GREEK, f, t, S, sat=0.7)

    canvas_b = _render_vf(r, "sm",
        lambda g, f, t, S: vf_ripple(g, f, t, S,
            sources=[(0.3,0.3), (0.7,0.7), (0.5,0.2)], freq=0.5, damping=0.012) * 1.4,
        hf_angle(0.1), PAL_STARS, f, t, S, sat=0.8)

    return blend_canvas(canvas_a, canvas_b, "exclusion", 0.8)
```

### 复杂场景（三网格 + 条件 + 自定义渲染）

```python
def fx_rings_explosion(r, f, t, S):
    """三网格场景，带粒子和条件触发的万花筒。"""
    # 图层 1：圆环
    canvas_a = _render_vf(r, "sm",
        lambda g, f, t, S: vf_rings(g, f, t, S, n_base=10, spacing_base=2) * 1.4,
        lambda g, f, t, S: (g.angle / (2*np.pi) + t * 0.15) % 1.0,
        PAL_STARS, f, t, S, sat=0.9)

    # 图层 2：不同网格上的漩涡
    canvas_b = _render_vf(r, "md",
        lambda g, f, t, S: vf_vortex(g, f, t, S, twist=6.0) * 1.2,
        hf_time_cycle(0.15), PAL_BLOCKS, f, t, S, sat=0.8)

    result = blend_canvas(canvas_b, canvas_a, "screen", 0.7)

    # 图层 3：粒子（自定义渲染，不走 _render_vf）
    g = r.get_grid("sm")
    if "px" not in S:
        S["px"], S["py"], S["vx"], S["vy"], S["life"], S["pch"] = (
            [], [], [], [], [], [])
    if f.get("beat", 0) > 0.5:
        chars = list("★✶✳✸✦✨*+")
        for _ in range(int(80 + f.get("rms", 0.3) * 120)):
            ang = random.uniform(0, 2 * math.pi)
            sp = random.uniform(1, 10) * (0.5 + f.get("sub_r", 0.3) * 2)
            S["px"].append(float(g.cols // 2))
            S["py"].append(float(g.rows // 2))
            S["vx"].append(math.cos(ang) * sp * 2.5)
            S["vy"].append(math.sin(ang) * sp)
            S["life"].append(1.0)
            S["pch"].append(random.choice(chars))

    # 更新 + 绘制粒子
    ch_p = np.full((g.rows, g.cols), " ", dtype="U1")
    co_p = np.zeros((g.rows, g.cols, 3), dtype=np.uint8)
    i = 0
    while i < len(S["px"]):
        S["px"][i] += S["vx"][i]; S["py"][i] += S["vy"][i]
        S["vy"][i] += 0.03; S["life"][i] -= 0.02
        if S["life"][i] <= 0:
            for k in ("px","py","vx","vy","life","pch"): S[k].pop(i)
        else:
            pr, pc = int(S["py"][i]), int(S["px"][i])
            if 0 <= pr < g.rows and 0 <= pc < g.cols:
                ch_p[pr, pc] = S["pch"][i]
                co_p[pr, pc] = hsv2rgb_scalar(
                    0.08 + (1-S["life"][i])*0.15, 0.95, S["life"][i])
            i += 1

    canvas_p = g.render(ch_p, co_p)
    result = blend_canvas(result, canvas_p, "add", 0.8)

    # 强拍上触发条件式万花筒
    if f.get("bdecay", 0) > 0.4:
        result = sh_kaleidoscope(result.copy(), folds=6)

    return result
```

### 带自定义字符渲染的场景（矩阵雨）

当你需要 `_render_vf()` 之外的逐单元控制时：

```python
def fx_matrix_layered(r, f, t, S):
    """矩阵雨与隧道混合 —— 双网格，屏幕混合。"""
    # 图层 1：矩阵雨（自定义逐列渲染）
    g = r.get_grid("md")
    rows, cols = g.rows, g.cols
    pal = PAL_KATA

    if "ry" not in S or len(S["ry"]) != cols:
        S["ry"] = np.random.uniform(-rows, rows, cols).astype(np.float32)
        S["rsp"] = np.random.uniform(0.3, 2.0, cols).astype(np.float32)
        S["rln"] = np.random.randint(8, 35, cols)
        S["rch"] = np.random.randint(1, len(pal), (rows, cols))

    speed = 0.6 + f.get("bass", 0.3) * 3
    if f.get("beat", 0) > 0.5: speed *= 2.5
    S["ry"] += S["rsp"] * speed

    ch = np.full((rows, cols), " ", dtype="U1")
    co = np.zeros((rows, cols, 3), dtype=np.uint8)
    heads = S["ry"].astype(int)
    for c in range(cols):
        head = heads[c]
        for i in range(S["rln"][c]):
            row = head - i
            if 0 <= row < rows:
                fade = 1.0 - i / S["rln"][c]
                ch[row, c] = pal[S["rch"][row, c] % len(pal)]
                if i == 0:
                    v = int(min(255, fade * 300))
                    co[row, c] = (int(v*0.9), v, int(v*0.9))
                else:
                    v = int(fade * 240)
                    co[row, c] = (int(v*0.1), v, int(v*0.4))
    canvas_a = g.render(ch, co)

    # 图层 2：sm 网格上的隧道，提供深度纹理
    canvas_b = _render_vf(r, "sm",
        lambda g, f, t, S: vf_tunnel(g, f, t, S, speed=5.0, complexity=10),
        hf_distance(0.3, 0.02), PAL_BLOCKS, f, t, S, sat=0.6)

    return blend_canvas(canvas_a, canvas_b, "screen", 0.5)
```

---

## 场景表

场景表定义了时间线：哪个场景何时播放、用什么配置。

### 结构

```python
SCENES = [
    {
        "start": 0.0,           # 起始时间（秒）
        "end": 3.96,            # 结束时间（秒）
        "name": "starfield",    # 标识符（用于片段文件名）
        "grid": "sm",           # 默认网格（供 render_clip 设置用）
        "fx": fx_starfield,     # 场景函数引用（必须是模块级）
        "gamma": 0.75,          # 色调映射 gamma 覆盖（默认 0.75）
        "shaders": [            # 着色器链（在色调映射 + 反馈之后应用）
            ("bloom", {"thr": 120}),
            ("vignette", {"s": 0.2}),
            ("grain", {"amt": 8}),
        ],
        "feedback": None,       # 反馈缓冲配置（None = 禁用）
        # "feedback": {"decay": 0.8, "blend": "screen", "opacity": 0.3,
        #              "transform": "zoom", "transform_amt": 0.02, "hue_shift": 0.02},
    },
    {
        "start": 3.96,
        "end": 6.58,
        "name": "matrix_layered",
        "grid": "md",
        "fx": fx_matrix_layered,
        "shaders": [
            ("crt", {"strength": 0.05}),
            ("scanlines", {"intensity": 0.12}),
            ("color_grade", {"tint": (0.7, 1.2, 0.7)}),
            ("bloom", {"thr": 100}),
        ],
        "feedback": {"decay": 0.5, "blend": "add", "opacity": 0.2},
    },
    # ……更多场景……
]
```

### 节拍同步的场景切换

从音频分析中推导切换点：

```python
# 获取节拍时间戳
beats = [fi / FPS for fi in range(N_FRAMES) if features["beat"][fi] > 0.5]

# 将节拍按乐句边界分组（每 4-8 拍）
cuts = [0.0]
for i in range(0, len(beats), 4):  # 每 4 拍切一次
    cuts.append(beats[i])
cuts.append(DURATION)

# 或使用音乐结构：静默间隙、能量变化
energy = features["rms"]
# 找出能量显著下降的时间点 -> 自然断点
```

### `render_clip()` —— 渲染循环

该函数将一个场景渲染为一个片段文件：

```python
def render_clip(seg, features, clip_path):
    r = Renderer()
    r.set_grid(seg["grid"])
    S = r.S
    random.seed(hash(seg["id"]) + 42)  # 每场景确定性

    # 从配置构建着色器链
    chain = ShaderChain()
    for shader_name, kwargs in seg.get("shaders", []):
        chain.add(shader_name, **kwargs)

    # 设置反馈缓冲
    fb = None
    fb_cfg = seg.get("feedback", None)
    if fb_cfg:
        fb = FeedbackBuffer()

    fx_fn = seg["fx"]

    # 打开 ffmpeg 管道
    cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{VW}x{VH}", "-r", str(FPS), "-i", "pipe:0",
           "-c:v", "libx264", "-preset", "fast", "-crf", "20",
           "-pix_fmt", "yuv420p", clip_path]
    stderr_fh = open(clip_path.replace(".mp4", ".log"), "w")
    pipe = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=stderr_fh)

    for fi in range(seg["frame_start"], seg["frame_end"]):
        t = fi / FPS
        feat = {k: float(features[k][fi]) for k in features}

        # 1. 场景渲染画布
        canvas = fx_fn(r, feat, t, S)

        # 2. 色调映射归一化亮度
        canvas = tonemap(canvas, gamma=seg.get("gamma", 0.75))

        # 3. 反馈增加时间递归
        if fb and fb_cfg:
            canvas = fb.apply(canvas, **{k: fb_cfg[k] for k in fb_cfg})

        # 4. 着色器链添加后处理
        canvas = chain.apply(canvas, f=feat, t=t)

        pipe.stdin.write(canvas.tobytes())

    pipe.stdin.close(); pipe.wait(); stderr_fh.close()
```

### 从场景表构建片段

```python
segments = []
for i, scene in enumerate(SCENES):
    segments.append({
        "id": f"s{i:02d}_{scene['name']}",
        "name": scene["name"],
        "grid": scene["grid"],
        "fx": scene["fx"],
        "shaders": scene.get("shaders", []),
        "feedback": scene.get("feedback", None),
        "gamma": scene.get("gamma", 0.75),
        "frame_start": int(scene["start"] * FPS),
        "frame_end": int(scene["end"] * FPS),
    })
```

### 并行渲染

场景是独立单元，可派发到进程池：

```python
from concurrent.futures import ProcessPoolExecutor, as_completed

with ProcessPoolExecutor(max_workers=N_WORKERS) as pool:
    futures = {
        pool.submit(render_clip, seg, features, clip_path): seg["id"]
        for seg, clip_path in zip(segments, clip_paths)
    }
    for fut in as_completed(futures):
        try:
            fut.result()
        except Exception as e:
            log(f"ERROR {futures[fut]}: {e}")
```

**Pickle 约束**：`ProcessPoolExecutor` 通过 pickle 序列化参数。模块级函数可被 pickle；lambda 和闭包不行。所有 `fx_*` 场景函数必须定义在模块级别，不能是闭包或类方法。

### 测试帧模式

在指定时间戳渲染单帧，无需完整渲染即可核验视觉效果：

```python
if args.test_frame >= 0:
    fi = min(int(args.test_frame * FPS), N_FRAMES - 1)
    t = fi / FPS
    feat = {k: float(features[k][fi]) for k in features}
    scene = next(sc for sc in reversed(SCENES) if t >= sc["start"])
    r = Renderer()
    r.set_grid(scene["grid"])
    canvas = scene["fx"](r, feat, t, r.S)
    canvas = tonemap(canvas, gamma=scene.get("gamma", 0.75))
    chain = ShaderChain()
    for sn, kw in scene.get("shaders", []):
        chain.add(sn, **kw)
    canvas = chain.apply(canvas, f=feat, t=t)
    Image.fromarray(canvas).save(f"test_{args.test_frame:.1f}s.png")
    print(f"Mean brightness: {canvas.astype(float).mean():.1f}")
```

CLI：`python reel.py --test-frame 10.0`

---

## 场景设计清单

对每个场景：

1. **选择 2-3 种网格尺寸** —— 不同尺度产生干涉
2. **每个图层选择不同的值场** —— 不要在每个网格上用同一个特效
3. **每个图层选择不同的色相场** —— 或至少不同的色相偏移
4. **每个图层选择不同的色板** —— PAL_RUNE 与 PAL_BLOCKS 混合，与 PAL_RUNE 和 PAL_DENSE 混合看起来不同
5. **选择与能量匹配的混合模式** —— 明亮用 screen，迷幻用 difference，细腻用 exclusion
6. **在节拍上添加条件特效** —— 万花筒、镜像、故障
7. **配置反馈以获得拖尾/递归观感** —— 或用 None 做干净切换
8. **使用破坏性着色器（solarize、posterize）时设置 gamma**
9. **在完整渲染前用 --test-frame 测试场景中点**

---

## 场景示例

复杂度递增、可直接复制粘贴的场景函数。每个都是完整的、可运行的 v2 场景函数，返回一个像素画布。关于场景协议见上文的「场景协议」章节；关于混合模式和色调映射见 `composition.md`。

---

### 最小化 —— 单网格、单特效

### 呼吸的等离子体

一个网格、一个值场、一个色相场。可能的最简单场景。

```python
def fx_breathing_plasma(r, f, t, S):
    """等离子体场，色相随时间循环。音频调制亮度。"""
    canvas = _render_vf(r, "md",
        lambda g, f, t, S: vf_plasma(g, f, t, S) * 1.3,
        hf_time_cycle(0.08), PAL_DENSE, f, t, S, sat=0.8)
    return canvas
```

### 反应-扩散珊瑚

单网格，基于模拟的场。随时间有机演化。

```python
def fx_coral(r, f, t, S):
    """Gray-Scott 反应-扩散 —— 珊瑚状分支图样。
    缓慢演化、有机。适合氛围/舒缓段落。"""
    canvas = _render_vf(r, "sm",
        lambda g, f, t, S: vf_reaction_diffusion(g, f, t, S,
            feed=0.037, kill=0.060, steps_per_frame=6, init_mode="center"),
        hf_distance(0.55, 0.015), PAL_DOTS, f, t, S, sat=0.7)
    return canvas
```

### SDF 几何

来自 SDF 的几何形状。干净、精确、图形化。

```python
def fx_sdf_rings(r, f, t, S):
    """同心 SDF 圆环，带平滑脉动。"""
    def val_fn(g, f, t, S):
        d1 = sdf_ring(g, radius=0.15 + f.get("bass", 0.3) * 0.05, thickness=0.015)
        d2 = sdf_ring(g, radius=0.25 + f.get("mid", 0.3) * 0.05, thickness=0.012)
        d3 = sdf_ring(g, radius=0.35 + f.get("hi", 0.3) * 0.04, thickness=0.010)
        combined = sdf_smooth_union(sdf_smooth_union(d1, d2, 0.05), d3, 0.05)
        return sdf_glow(combined, falloff=0.08) * (0.5 + f.get("rms", 0.3) * 0.8)
    canvas = _render_vf(r, "md", val_fn, hf_angle(0.0), PAL_STARS, f, t, S, sat=0.85)
    return canvas
```

---

### 标准 —— 双网格 + 混合

### 穿越噪声的隧道

两种密度的网格，屏幕混合。精细的噪声纹理透过较粗的隧道字符显现。

```python
def fx_tunnel_noise(r, f, t, S):
    """md 网格上的隧道深度 + sm 网格上的 fBM 噪声，屏幕混合。"""
    canvas_a = _render_vf(r, "md",
        lambda g, f, t, S: vf_tunnel(g, f, t, S, speed=4.0, complexity=8) * 1.2,
        hf_distance(0.5, 0.02), PAL_BLOCKS, f, t, S, sat=0.7)

    canvas_b = _render_vf(r, "sm",
        lambda g, f, t, S: vf_fbm(g, f, t, S, octaves=4, freq=0.05, speed=0.15) * 1.3,
        hf_time_cycle(0.06), PAL_RUNE, f, t, S, sat=0.6)

    return blend_canvas(canvas_a, canvas_b, "screen", 0.7)
```

### Voronoi 单元 + 螺旋叠加

Voronoi 单元边缘叠加一个螺旋臂图样。

```python
def fx_voronoi_spiral(r, f, t, S):
    """md 上的 Voronoi 边缘检测 + lg 上的对数螺旋。"""
    canvas_a = _render_vf(r, "md",
        lambda g, f, t, S: vf_voronoi(g, f, t, S,
            n_cells=15, mode="edge", edge_width=2.0, speed=0.4),
        hf_angle(0.2), PAL_CIRCUIT, f, t, S, sat=0.75)

    canvas_b = _render_vf(r, "lg",
        lambda g, f, t, S: vf_spiral(g, f, t, S, n_arms=4, tightness=3.0) * 1.2,
        hf_distance(0.1, 0.03), PAL_BLOCKS, f, t, S, sat=0.9)

    return blend_canvas(canvas_a, canvas_b, "exclusion", 0.6)
```

### 域扭曲的 fBM

两层相同的 fBM，其中一层做域扭曲（domain-warp），差值混合，产生迷幻的有机纹理。

```python
def fx_organic_warp(r, f, t, S):
    """干净 fBM 与域扭曲 fBM，差值混合。"""
    canvas_a = _render_vf(r, "sm",
        lambda g, f, t, S: vf_fbm(g, f, t, S, octaves=5, freq=0.04, speed=0.1),
        hf_plasma(0.2), PAL_DENSE, f, t, S, sat=0.6)

    canvas_b = _render_vf(r, "md",
        lambda g, f, t, S: vf_domain_warp(g, f, t, S,
            warp_strength=20.0, freq=0.05, speed=0.15),
        hf_time_cycle(0.05), PAL_BRAILLE, f, t, S, sat=0.7)

    return blend_canvas(canvas_a, canvas_b, "difference", 0.7)
```

---

### 复杂 —— 三网格 + 条件 + 反馈

### 迷幻大教堂

三网格合成，节拍触发的万花筒和反馈缩放隧道。视觉上最复杂的图样。

```python
def fx_cathedral(r, f, t, S):
    """三层大教堂：干涉 + 圆环 + 噪声，节拍上万花筒，
    反馈缩放隧道。"""
    # 图层 1：sm 网格上的干涉图样
    canvas_a = _render_vf(r, "sm",
        lambda g, f, t, S: vf_interference(g, f, t, S, n_waves=7) * 1.3,
        hf_angle(0.0), PAL_MATH, f, t, S, sat=0.8)

    # 图层 2：md 网格上的脉动圆环
    canvas_b = _render_vf(r, "md",
        lambda g, f, t, S: vf_rings(g, f, t, S, n_base=10, spacing_base=3) * 1.4,
        hf_distance(0.3, 0.02), PAL_STARS, f, t, S, sat=0.9)

    # 图层 3：lg 网格上的时变噪声（缓慢形变）
    canvas_c = _render_vf(r, "lg",
        lambda g, f, t, S: vf_temporal_noise(g, f, t, S,
            freq=0.04, t_freq=0.2, octaves=3),
        hf_time_cycle(0.12), PAL_BLOCKS, f, t, S, sat=0.7)

    # 混合：A 屏幕 B，再与 C 做差值
    result = blend_canvas(canvas_a, canvas_b, "screen", 0.8)
    result = blend_canvas(result, canvas_c, "difference", 0.5)

    # 节拍触发的万花筒
    if f.get("bdecay", 0) > 0.3:
        folds = 6 if f.get("sub_r", 0.3) > 0.4 else 8
        result = sh_kaleidoscope(result.copy(), folds=folds)

    return result

# 带反馈的场景表条目：
# {"start": 30.0, "end": 50.0, "name": "cathedral", "fx": fx_cathedral,
#  "gamma": 0.65, "shaders": [("bloom", {"thr": 110}), ("chromatic", {"amt": 4}),
#                              ("vignette", {"s": 0.2}), ("grain", {"amt": 8})],
#  "feedback": {"decay": 0.75, "blend": "screen", "opacity": 0.35,
#               "transform": "zoom", "transform_amt": 0.012, "hue_shift": 0.015}}
```

### 带吸引子叠加的遮罩反应-扩散

反应-扩散仅在动画光圈遮罩内可见，其下是奇异吸引子密度场。

```python
def fx_masked_life(r, f, t, S):
    """吸引子底层 + 通过光圈遮罩可见的反应-扩散 + 粒子。"""
    g_sm = r.get_grid("sm")
    g_md = r.get_grid("md")

    # 图层 1：奇异吸引子密度场（背景）
    canvas_bg = _render_vf(r, "sm",
        lambda g, f, t, S: vf_strange_attractor(g, f, t, S,
            attractor="clifford", n_points=30000),
        hf_time_cycle(0.04), PAL_DOTS, f, t, S, sat=0.5)

    # 图层 2：反应-扩散（前景，将被遮罩）
    canvas_rd = _render_vf(r, "md",
        lambda g, f, t, S: vf_reaction_diffusion(g, f, t, S,
            feed=0.046, kill=0.063, steps_per_frame=4, init_mode="ring"),
        hf_angle(0.15), PAL_HALFFILL, f, t, S, sat=0.85)

    # 动画光圈遮罩 —— 在场景的前 5 秒内打开
    scene_start = S.get("_scene_start", t)
    if "_scene_start" not in S:
        S["_scene_start"] = t
    mask = mask_iris(g_md, t, scene_start, scene_start + 5.0,
                     max_radius=0.6)
    canvas_rd = apply_mask_canvas(canvas_rd, mask, bg_canvas=canvas_bg)

    # 图层 3：跟随 R-D 梯度的流场粒子
    rd_field = vf_reaction_diffusion(g_sm, f, t, S,
        feed=0.046, kill=0.063, steps_per_frame=0)  # 只读取，不步进
    ch_p, co_p = update_flow_particles(S, g_sm, f, rd_field,
        n=300, speed=0.8, char_set=list("·•◦∘°"))
    canvas_p = g_sm.render(ch_p, co_p)

    result = blend_canvas(canvas_rd, canvas_p, "add", 0.7)
    return result
```

### 带缓动关键帧的形变场序列

展示时间连贯性：在不同特效之间用关键帧参数做平滑形变。

```python
def fx_morphing_journey(r, f, t, S):
    """在 20 秒内形变穿越 4 个值场，带缓动过渡。
    参数（扭转、臂数）也做了关键帧。"""
    # 关键帧的 twist 参数
    twist = keyframe(t, [(0, 1.0), (5, 5.0), (10, 2.0), (15, 8.0), (20, 1.0)],
                     ease_fn=ease_in_out_cubic, loop=True)

    # 值场序列，2 秒交叉淡化
    fields = [
        lambda g, f, t, S: vf_plasma(g, f, t, S),
        lambda g, f, t, S: vf_vortex(g, f, t, S, twist=twist),
        lambda g, f, t, S: vf_fbm(g, f, t, S, octaves=5, freq=0.04),
        lambda g, f, t, S: vf_domain_warp(g, f, t, S, warp_strength=15),
    ]
    durations = [5.0, 5.0, 5.0, 5.0]

    val_fn = lambda g, f, t, S: vf_sequence(g, f, t, S, fields, durations,
                                             crossfade=2.0)

    # 用缓慢旋转的色相渲染
    canvas = _render_vf(r, "md", val_fn, hf_time_cycle(0.06),
                        PAL_DENSE, f, t, S, sat=0.8)

    # 第二层：同一序列的平铺版本，用更小网格
    tiled_fn = lambda g, f, t, S: vf_sequence(
        make_tgrid(g, *uv_tile(g, 3, 3, mirror=True)),
        f, t, S, fields, durations, crossfade=2.0)
    canvas_b = _render_vf(r, "sm", tiled_fn, hf_angle(0.1),
                          PAL_RUNE, f, t, S, sat=0.6)

    return blend_canvas(canvas, canvas_b, "screen", 0.5)
```

---

### 专用 —— 独特的状态模式

### 带幻影拖尾的生命游戏

带模拟淡入拖尾的元胞自动机。节拍注入随机单元。

```python
def fx_life(r, f, t, S):
    """Conway 生命游戏，带渐隐的幻影拖尾。
    节拍事件注入随机活单元以制造扰动。"""
    canvas = _render_vf(r, "sm",
        lambda g, f, t, S: vf_game_of_life(g, f, t, S,
            rule="life", steps_per_frame=1, fade=0.92, density=0.25),
        hf_fixed(0.33), PAL_BLOCKS, f, t, S, sat=0.8)

    # 叠加层：lg 网格上的珊瑚自动机，提供块状纹理
    canvas_b = _render_vf(r, "lg",
        lambda g, f, t, S: vf_game_of_life(g, f, t, S,
            rule="coral", steps_per_frame=1, fade=0.85, density=0.15, seed=99),
        hf_time_cycle(0.1), PAL_HATCH, f, t, S, sat=0.6)

    return blend_canvas(canvas, canvas_b, "screen", 0.5)
```

### Voronoi 之上的 Boids 群

涌现的群体运动覆盖在单元背景之上。

```python
def fx_boid_swarm(r, f, t, S):
    """动画 Voronoi 单元之上的群集 boids。"""
    # 背景：Voronoi 单元
    canvas_bg = _render_vf(r, "md",
        lambda g, f, t, S: vf_voronoi(g, f, t, S,
            n_cells=20, mode="distance", speed=0.2),
        hf_distance(0.4, 0.02), PAL_CIRCUIT, f, t, S, sat=0.5)

    # 前景：boids
    g = r.get_grid("md")
    ch_b, co_b = update_boids(S, g, f, n_boids=150, perception=6.0,
                              max_speed=1.5, char_set=list("▸▹►▻→⟶"))
    canvas_boids = g.render(ch_b, co_b)

    # boids 的拖尾
    # (boid 位置存放在 S["boid_x"]、S["boid_y"])
    S["px"] = list(S.get("boid_x", []))
    S["py"] = list(S.get("boid_y", []))
    ch_t, co_t = draw_particle_trails(S, g, max_trail=6, fade=0.6)
    canvas_trails = g.render(ch_t, co_t)

    result = blend_canvas(canvas_bg, canvas_trails, "add", 0.3)
    result = blend_canvas(result, canvas_boids, "add", 0.9)
    return result
```

### 透过 SDF 文字模板升腾的火焰

火焰特效仅在文字字形内可见。

```python
def fx_fire_text(r, f, t, S):
    """透过文字模板可见的火柱。文字充当窗口。"""
    g = r.get_grid("lg")

    # 全屏火焰（将被遮罩）
    canvas_fire = _render_vf(r, "sm",
        lambda g, f, t, S: np.clip(
            vf_fbm(g, f, t, S, octaves=4, freq=0.08, speed=0.8) *
            (1.0 - g.rr / g.rows) *  # 向顶部淡化
            (0.6 + f.get("bass", 0.3) * 0.8), 0, 1),
        hf_fixed(0.05), PAL_BLOCKS, f, t, S, sat=0.9)  # 火焰色相

    # 背景：深色域扭曲
    canvas_bg = _render_vf(r, "md",
        lambda g, f, t, S: vf_domain_warp(g, f, t, S,
            warp_strength=8, freq=0.03, speed=0.05) * 0.3,
        hf_fixed(0.6), PAL_DENSE, f, t, S, sat=0.4)

    # 文字模板遮罩
    mask = mask_text(g, "FIRE", row_frac=0.45)
    # 垂直扩展以覆盖多行
    for offset in range(-2, 3):
        shifted = mask_text(g, "FIRE", row_frac=0.45 + offset / g.rows)
        mask = mask_union(mask, shifted)

    canvas_masked = apply_mask_canvas(canvas_fire, mask, bg_canvas=canvas_bg)
    return canvas_masked
```

### 竖屏模式：垂直雨 + 引言

为 9:16 优化。利用垂直空间做长雨迹和堆叠文字。

```python
def fx_portrait_rain_quote(r, f, t, S):
    """竖屏优化：矩阵雨（长垂直拖尾）配堆叠引言。
    为 1080x1920 (9:16) 设计。"""
    g = r.get_grid("md")  # 竖屏下约 112x100

    # 矩阵雨 —— 长拖尾受益于竖屏多出来的行
    ch, co, S = eff_matrix_rain(g, f, t, S,
        hue=0.33, bri=0.6, pal=PAL_KATA, speed_base=0.4, speed_beat=2.5)
    canvas_rain = g.render(ch, co)

    # 下方隧道深度提供纹理
    canvas_tunnel = _render_vf(r, "sm",
        lambda g, f, t, S: vf_tunnel(g, f, t, S, speed=3.0, complexity=6) * 0.8,
        hf_fixed(0.33), PAL_BLOCKS, f, t, S, sat=0.5)

    result = blend_canvas(canvas_tunnel, canvas_rain, "screen", 0.8)

    # 引言文字 —— 竖屏布局：短行、行数多
    g_text = r.get_grid("lg")  # 竖屏下约 90x80
    quote_lines = layout_text_portrait(
        "The code is the art and the art is the code",
        max_chars_per_line=20)
    # 垂直居中
    block_start = (g_text.rows - len(quote_lines)) // 2
    ch_t = np.full((g_text.rows, g_text.cols), " ", dtype="U1")
    co_t = np.zeros((g_text.rows, g_text.cols, 3), dtype=np.uint8)
    total_chars = sum(len(l) for l in quote_lines)
    progress = min(1.0, (t - S.get("_scene_start", t)) / 3.0)
    if "_scene_start" not in S: S["_scene_start"] = t
    render_typewriter(ch_t, co_t, quote_lines, block_start, g_text.cols,
                      progress, total_chars, (200, 255, 220), t)
    canvas_text = g_text.render(ch_t, co_t)

    result = blend_canvas(result, canvas_text, "add", 0.9)
    return result
```

---

### 场景表模板

把场景串联成完整视频：

```python
SCENES = [
    {"start": 0.0,  "end": 5.0,  "name": "coral",
     "fx": fx_coral, "grid": "sm", "gamma": 0.70,
     "shaders": [("bloom", {"thr": 110}), ("vignette", {"s": 0.2})],
     "feedback": {"decay": 0.8, "blend": "screen", "opacity": 0.3,
                  "transform": "zoom", "transform_amt": 0.01}},

    {"start": 5.0,  "end": 15.0, "name": "tunnel_noise",
     "fx": fx_tunnel_noise, "grid": "md", "gamma": 0.75,
     "shaders": [("chromatic", {"amt": 3}), ("bloom", {"thr": 120}),
                 ("scanlines", {"intensity": 0.06}), ("grain", {"amt": 8})],
     "feedback": None},

    {"start": 15.0, "end": 35.0, "name": "cathedral",
     "fx": fx_cathedral, "grid": "sm", "gamma": 0.65,
     "shaders": [("bloom", {"thr": 100}), ("chromatic", {"amt": 5}),
                 ("color_wobble", {"amt": 0.2}), ("vignette", {"s": 0.18})],
     "feedback": {"decay": 0.75, "blend": "screen", "opacity": 0.35,
                  "transform": "zoom", "transform_amt": 0.012, "hue_shift": 0.015}},

    {"start": 35.0, "end": 50.0, "name": "morphing",
     "fx": fx_morphing_journey, "grid": "md", "gamma": 0.70,
     "shaders": [("bloom", {"thr": 110}), ("grain", {"amt": 6})],
     "feedback": {"decay": 0.7, "blend": "screen", "opacity": 0.25,
                  "transform": "rotate_cw", "transform_amt": 0.003}},
]
```
