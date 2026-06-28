# 特效目录

用于生成视觉图案的特效构建块。在 v2 中，这些特效在**场景函数内部**使用，直接返回一个像素画布。下面的构建块作用于网格坐标数组，生成 `(chars, colors)` 或 value/hue 场，再由场景函数通过 `_render_vf()` 渲染到画布上。

> **另见：** architecture.md · composition.md · scenes.md · shaders.md · troubleshooting.md

## 设计哲学

特效是创意的核心。不要为每个项目原样照抄这些代码——把它们当作**构建块**，去**组合、修改、发明**新的特效。每个项目都应该有独特的气质。

关键原则：
- **多层特效叠加**，而不是用一个庞大臃肿的函数搞定一切
- **把所有参数都参数化** —— hue、speed、density、amplitude 都应该作为参数传入
- **响应音频/视频特征** —— 音频/视频特征至少应调制每个特效的 2-3 个参数
- **每个段落要有变化** —— 永远不要对整段视频使用相同的特效配置
- **发明项目专属特效** —— 下面的目录只是起步词汇表，不是固定不变的全部

---

## 背景填充

每个特效都应从背景开始。永远不要留一片纯黑。

### 动画正弦场（通用）
```python
def bg_sinefield(g, f, t, hue=0.6, bri=0.5, pal=PAL_DEFAULT,
                 freq=(0.13, 0.17, 0.07, 0.09), speed=(0.5, -0.4, -0.3, 0.2)):
    """多层正弦场。调整 freq/speed 元组可获得不同纹理。"""
    v1 = np.sin(g.cc*freq[0] + t*speed[0]) * np.sin(g.rr*freq[1] - t*speed[1]) * 0.5 + 0.5
    v2 = np.sin(g.cc*freq[2] - t*speed[2] + g.rr*freq[3]) * 0.4 + 0.5
    v3 = np.sin(g.dist_n*5 + t*0.2) * 0.3 + 0.4
    v4 = np.cos(g.angle*3 - t*0.6) * 0.15 + 0.5
    val = np.clip((v1*0.3 + v2*0.25 + v3*0.25 + v4*0.2) * bri * (0.6 + f["rms"]*0.6), 0.06, 1)
    mask = val > 0.03
    ch = val2char(val, mask, pal)
    h = np.full_like(val, hue) + f.get("cent", 0.5)*0.1 + val*0.08
    R, G, B = hsv2rgb(h, np.clip(0.35+f.get("flat",0.4)*0.4, 0, 1) * np.ones_like(val), val)
    return ch, mkc(R, G, B, g.rows, g.cols)
```

### 视频源背景
```python
def bg_video(g, frame_rgb, pal=PAL_DEFAULT, brightness=0.5):
    small = np.array(Image.fromarray(frame_rgb).resize((g.cols, g.rows)))
    lum = np.mean(small, axis=2) / 255.0 * brightness
    mask = lum > 0.02
    ch = val2char(lum, mask, pal)
    co = np.clip(small * np.clip(lum[:,:,None]*1.5+0.3, 0.3, 1), 0, 255).astype(np.uint8)
    return ch, co
```

### 噪点 / 雪花场
```python
def bg_noise(g, f, t, pal=PAL_BLOCKS, density=0.3, hue_drift=0.02):
    val = np.random.random((g.rows, g.cols)).astype(np.float32) * density * (0.5 + f["rms"]*0.5)
    val = np.clip(val, 0, 1); mask = val > 0.02
    ch = val2char(val, mask, pal)
    R, G, B = hsv2rgb(np.full_like(val, t*hue_drift % 1), np.full_like(val, 0.3), val)
    return ch, mkc(R, G, B, g.rows, g.cols)
```

### 类柏林平滑噪点
```python
def bg_smooth_noise(g, f, t, hue=0.5, bri=0.5, pal=PAL_DOTS, octaves=3):
    """对柏林噪点的多层正弦近似。廉价、平滑、有机。"""
    val = np.zeros((g.rows, g.cols), dtype=np.float32)
    for i in range(octaves):
        freq = 0.05 * (2 ** i)
        amp = 0.5 / (i + 1)
        phase = t * (0.3 + i * 0.2)
        val += np.sin(g.cc * freq + phase) * np.cos(g.rr * freq * 0.7 - phase * 0.5) * amp
    val = np.clip(val * 0.5 + 0.5, 0, 1) * bri
    mask = val > 0.03
    ch = val2char(val, mask, pal)
    h = np.full_like(val, hue) + val * 0.1
    R, G, B = hsv2rgb(h, np.full_like(val, 0.5), val)
    return ch, mkc(R, G, B, g.rows, g.cols)
```

### 细胞 / Voronoi 近似
```python
def bg_cellular(g, f, t, n_centers=12, hue=0.5, bri=0.6, pal=PAL_BLOCKS):
    """基于到 N 个移动中心点最近距离的类 Voronoi 单元。"""
    rng = np.random.RandomState(42)  # 确定性的中心点
    cx = (rng.rand(n_centers) * g.cols).astype(np.float32)
    cy = (rng.rand(n_centers) * g.rows).astype(np.float32)
    # 让中心点动起来
    cx_t = cx + np.sin(t * 0.5 + np.arange(n_centers) * 0.7) * 5
    cy_t = cy + np.cos(t * 0.4 + np.arange(n_centers) * 0.9) * 3
    # 到任意中心点的最小距离
    min_d = np.full((g.rows, g.cols), 999.0, dtype=np.float32)
    for i in range(n_centers):
        d = np.sqrt((g.cc - cx_t[i])**2 + (g.rr - cy_t[i])**2)
        min_d = np.minimum(min_d, d)
    val = np.clip(1.0 - min_d / (g.cols * 0.3), 0, 1) * bri
    # 单元边缘（两个中心点距离近乎相等处）
    # ... 用第二最近距离技巧来突出边缘
    mask = val > 0.03
    ch = val2char(val, mask, pal)
    R, G, B = hsv2rgb(np.full_like(val, hue) + min_d * 0.005, np.full_like(val, 0.5), val)
    return ch, mkc(R, G, B, g.rows, g.cols)
```

---

> **注意：** v1 的 `eff_rings`、`eff_rays`、`eff_spiral`、`eff_glow`、`eff_tunnel`、`eff_vortex`、`eff_freq_waves`、`eff_interference`、`eff_aurora` 和 `eff_ripple` 函数已被下方的 `vf_*` value 场生成器取代（通过 `_render_vf()` 使用）。`vf_*` 版本与多网格合成管线集成，所有新场景都应优先使用它们。

---

## 粒子系统

### 通用模式
所有粒子系统都通过 `S` dict 参数维护持久化状态：
```python
# S 是持久化状态 dict（与 r.S 相同，显式传入）
if "px" not in S:
    S["px"]=[]; S["py"]=[]; S["vx"]=[]; S["vy"]=[]; S["life"]=[]; S["char"]=[]

# 发射新粒子（节拍触发、持续发射或外部触发）
# 更新：position += velocity，施加力，衰减 life
# 绘制：映射到网格，根据 life 设置字符/颜色
# 剔除：移除已死粒子，限制总数量上限
```

### 粒子字符集

不要硬编码粒子字符。按项目/氛围选择：

```python
# 能量 / 爆炸
PART_ENERGY  = list("*+#@⚡✦★█▓")
PART_SPARK   = list("·•●★✶*+")
# 有机 / 自然
PART_LEAF    = list("❀❁❂❃✿☘•")
PART_SNOW    = list("❄❅❆·•*○")
PART_RAIN    = list("|│┃║/\\")
PART_BUBBLE  = list("○◎◉●∘∙°")
# 数据 / 科技
PART_DATA    = list("01{}[]<>|/\\")
PART_HEX     = list("0123456789ABCDEF")
PART_BINARY  = list("01")
# 神秘
PART_RUNE    = list("ᚠᚢᚦᚱᚷᛁᛇᛒᛖᛚᛞᛟ✦★")
PART_ZODIAC  = list("♈♉♊♋♌♍♎♏♐♑♒♓")
# 极简
PART_DOT     = list("·•●")
PART_DASH    = list("-=~─═")
```

### 爆炸（节拍触发）
```python
def emit_explosion(S, f, center_r, center_c, char_set=PART_ENERGY, count_base=80):
    if f.get("beat", 0) > 0:
        for _ in range(int(count_base + f["rms"]*150)):
            ang = random.uniform(0, 2*math.pi)
            sp = random.uniform(1, 9) * (0.5 + f.get("sub_r", 0.3)*2)
            S["px"].append(float(center_c))
            S["py"].append(float(center_r))
            S["vx"].append(math.cos(ang)*sp*2.5)
            S["vy"].append(math.sin(ang)*sp)
            S["life"].append(1.0)
            S["char"].append(random.choice(char_set))
# 更新：vy 上施加重力 += 0.03，life -= 0.015
# 颜色：life * 255 决定亮度，hue 渐变由调用方控制
```

### 上升余烬
```python
# 发射：sy = rows-1, vy = -random.uniform(1,5), vx = random.uniform(-1.5,1.5)
# 更新：vx += 随机抖动 * 0.3，life -= 0.01
# 粒子数上限约 1500
```

### 消散云雾
```python
# 初始化：N=600 个粒子铺满屏幕
# 更新：缓慢向上漂移，life 逐渐衰减
# life -= 0.002 * (1 + elapsed * 0.05)  # 加速衰减
```

### 星空（3D 投影）
```python
# N 颗星，每颗有归一化坐标 (sx, sy, sz)
# 移动：sz -= speed（星星向相机靠近）
# 投影：px = cx + sx/sz * cx, py = cy + sy/sz * cy
# 重置越过相机的星星（sz <= 0.01）
# 亮度 = (1 - sz)，为明亮星星绘制拖尾
```

### 轨道（圆周/椭圆运动）
```python
def emit_orbit(S, n=20, radius=15, speed=1.0, char_set=PART_DOT):
    """围绕中心点做轨道运动的粒子。"""
    for i in range(n):
        angle = i * 2 * math.pi / n
        S["px"].append(0.0); S["py"].append(0.0)  # 将根据 angle 计算
        S["vx"].append(angle)  # 把 angle 存到 "vx" 用于轨道
        S["vy"].append(radius + random.uniform(-2, 2))  # 存储半径
        S["life"].append(1.0)
        S["char"].append(random.choice(char_set))
# 更新：angle += speed * dt, px = cx + radius * cos(angle), py = cy + radius * sin(angle)
```

### 引力井
```python
# 粒子被一个或多个引力点吸引
# 更新：计算朝向每个引力井的力向量，作为加速度施加
# 到达引力井中心的粒子从边缘重生
```

### 群集 / Boids

由三条简单规则涌现出的群体行为：分离、对齐、聚合。

```python
def update_boids(S, g, f, n_boids=200, perception=8.0, max_speed=2.0,
                 sep_weight=1.5, ali_weight=1.0, coh_weight=1.0,
                 char_set=None):
    """Boids 群集模拟。粒子自组织成有机的群组。

    perception：每只 boid 的视野范围（网格格数）
    sep_weight：分离（避免拥挤）强度
    ali_weight：对齐（匹配邻居速度）强度
    coh_weight：聚合（朝群体中心转向）强度
    """
    if char_set is None:
        char_set = list("·•●◦∘⬤")
    if "boid_x" not in S:
        rng = np.random.RandomState(42)
        S["boid_x"] = rng.uniform(0, g.cols, n_boids).astype(np.float32)
        S["boid_y"] = rng.uniform(0, g.rows, n_boids).astype(np.float32)
        S["boid_vx"] = (rng.random(n_boids).astype(np.float32) - 0.5) * max_speed
        S["boid_vy"] = (rng.random(n_boids).astype(np.float32) - 0.5) * max_speed
        S["boid_ch"] = [random.choice(char_set) for _ in range(n_boids)]

    bx = S["boid_x"]; by = S["boid_y"]
    bvx = S["boid_vx"]; bvy = S["boid_vy"]
    n = len(bx)

    # 为每只 boid 计算转向力
    ax = np.zeros(n, dtype=np.float32)
    ay = np.zeros(n, dtype=np.float32)

    # 空间哈希，加速邻居查找
    cell_size = perception
    cells = {}
    for i in range(n):
        cx_i = int(bx[i] / cell_size)
        cy_i = int(by[i] / cell_size)
        key = (cx_i, cy_i)
        if key not in cells:
            cells[key] = []
        cells[key].append(i)

    for i in range(n):
        cx_i = int(bx[i] / cell_size)
        cy_i = int(by[i] / cell_size)
        sep_x, sep_y = 0.0, 0.0
        ali_x, ali_y = 0.0, 0.0
        coh_x, coh_y = 0.0, 0.0
        count = 0

        # 检查相邻单元格
        for dcx in range(-1, 2):
            for dcy in range(-1, 2):
                for j in cells.get((cx_i + dcx, cy_i + dcy), []):
                    if j == i:
                        continue
                    dx = bx[j] - bx[i]
                    dy = by[j] - by[i]
                    dist = np.sqrt(dx * dx + dy * dy)
                    if dist < perception and dist > 0.01:
                        count += 1
                        # 分离：远离过近的邻居
                        if dist < perception * 0.4:
                            sep_x -= dx / (dist * dist)
                            sep_y -= dy / (dist * dist)
                        # 对齐：匹配速度
                        ali_x += bvx[j]
                        ali_y += bvy[j]
                        # 聚合：朝群体中心转向
                        coh_x += bx[j]
                        coh_y += by[j]

        if count > 0:
            # 归一化并加权
            ax[i] += sep_x * sep_weight
            ay[i] += sep_y * sep_weight
            ax[i] += (ali_x / count - bvx[i]) * ali_weight * 0.1
            ay[i] += (ali_y / count - bvy[i]) * ali_weight * 0.1
            ax[i] += (coh_x / count - bx[i]) * coh_weight * 0.01
            ay[i] += (coh_y / count - by[i]) * coh_weight * 0.01

    # 音频响应：低音把 boids 从中心向外推
    if f.get("bass", 0) > 0.5:
        cx_g, cy_g = g.cols / 2, g.rows / 2
        dx = bx - cx_g; dy = by - cy_g
        dist = np.sqrt(dx**2 + dy**2) + 1
        ax += (dx / dist) * f["bass"] * 2
        ay += (dy / dist) * f["bass"] * 2

    # 更新速度和位置
    bvx += ax; bvy += ay
    # 限速
    speed = np.sqrt(bvx**2 + bvy**2) + 1e-10
    over = speed > max_speed
    bvx[over] *= max_speed / speed[over]
    bvy[over] *= max_speed / speed[over]
    bx += bvx; by += bvy

    # 边缘环绕
    bx %= g.cols; by %= g.rows

    S["boid_x"] = bx; S["boid_y"] = by
    S["boid_vx"] = bvx; S["boid_vy"] = bvy

    # 绘制
    ch = np.full((g.rows, g.cols), " ", dtype="U1")
    co = np.zeros((g.rows, g.cols, 3), dtype=np.uint8)
    for i in range(n):
        r, c = int(by[i]) % g.rows, int(bx[i]) % g.cols
        ch[r, c] = S["boid_ch"][i]
        spd = min(1.0, speed[i] / max_speed)
        R, G, B = hsv2rgb_scalar(spd * 0.3, 0.8, 0.5 + spd * 0.5)
        co[r, c] = (R, G, B)
    return ch, co
```

### 流场粒子

沿 value 场梯度流动的粒子。任何 `vf_*` 函数都能变成一条承载粒子的「河流」：

```python
def update_flow_particles(S, g, f, flow_field, n=500, speed=1.0,
                          life_drain=0.005, emit_rate=10,
                          char_set=None):
    """由 value 场梯度驱动的粒子。

    flow_field：float32 (rows, cols) —— 粒子跟随的场。
                粒子从低值流向高值（上坡）或沿梯度方向流动。
    """
    if char_set is None:
        char_set = list("·•∘◦°⋅")
    if "fp_x" not in S:
        S["fp_x"] = []; S["fp_y"] = []; S["fp_vx"] = []; S["fp_vy"] = []
        S["fp_life"] = []; S["fp_ch"] = []

    # 在随机位置发射新粒子
    for _ in range(emit_rate):
        if len(S["fp_x"]) < n:
            S["fp_x"].append(random.uniform(0, g.cols - 1))
            S["fp_y"].append(random.uniform(0, g.rows - 1))
            S["fp_vx"].append(0.0); S["fp_vy"].append(0.0)
            S["fp_life"].append(1.0)
            S["fp_ch"].append(random.choice(char_set))

    # 计算流场梯度（中心差分）
    pad = np.pad(flow_field, 1, mode="wrap")
    grad_x = (pad[1:-1, 2:] - pad[1:-1, :-2]) * 0.5
    grad_y = (pad[2:, 1:-1] - pad[:-2, 1:-1]) * 0.5

    # 更新粒子
    i = 0
    while i < len(S["fp_x"]):
        px, py = S["fp_x"][i], S["fp_y"][i]
        # 在粒子位置采样梯度
        gc = int(px) % g.cols; gr = int(py) % g.rows
        gx = grad_x[gr, gc]; gy = grad_y[gr, gc]
        # 让速度朝梯度方向转向
        S["fp_vx"][i] = S["fp_vx"][i] * 0.9 + gx * speed * 10
        S["fp_vy"][i] = S["fp_vy"][i] * 0.9 + gy * speed * 10
        S["fp_x"][i] += S["fp_vx"][i]
        S["fp_y"][i] += S["fp_vy"][i]
        S["fp_life"][i] -= life_drain

        if S["fp_life"][i] <= 0:
            for k in ("fp_x", "fp_y", "fp_vx", "fp_vy", "fp_life", "fp_ch"):
                S[k].pop(i)
        else:
            i += 1

    # 绘制
    ch = np.full((g.rows, g.cols), " ", dtype="U1")
    co = np.zeros((g.rows, g.cols, 3), dtype=np.uint8)
    for i in range(len(S["fp_x"])):
        r = int(S["fp_y"][i]) % g.rows
        c = int(S["fp_x"][i]) % g.cols
        ch[r, c] = S["fp_ch"][i]
        v = S["fp_life"][i]
        co[r, c] = (int(v * 200), int(v * 180), int(v * 255))
    return ch, co
```

### 粒子拖尾

在当前位置和上一帧位置之间绘制渐隐的线段：

```python
def draw_particle_trails(S, g, trail_key="trails", max_trail=8, fade=0.7):
    """为任意粒子系统添加拖尾。在更新位置后调用。
    将之前的位置存入 S[trail_key]，并绘制渐隐的线段。

    要求 S 中有 'px'、'py' 列表（标准粒子键）。
    max_trail：记忆的历史位置数量
    fade：每个拖尾步长的亮度乘数（0.7 = 每后退一步保留 70%）
    """
    if trail_key not in S:
        S[trail_key] = []

    # 存储当前位置
    current = list(zip(
        [int(y) for y in S.get("py", [])],
        [int(x) for x in S.get("px", [])]
    ))
    S[trail_key].append(current)
    if len(S[trail_key]) > max_trail:
        S[trail_key] = S[trail_key][-max_trail:]

    # 把拖尾绘制到字符/颜色数组上
    ch = np.full((g.rows, g.cols), " ", dtype="U1")
    co = np.zeros((g.rows, g.cols, 3), dtype=np.uint8)
    trail_chars = list("·∘◦°⋅.,'`")

    for age, positions in enumerate(reversed(S[trail_key])):
        bri = fade ** age
        if bri < 0.05:
            break
        ci = min(age, len(trail_chars) - 1)
        for r, c in positions:
            if 0 <= r < g.rows and 0 <= c < g.cols and ch[r, c] == " ":
                ch[r, c] = trail_chars[ci]
                v = int(bri * 180)
                co[r, c] = (v, v, int(v * 0.8))
    return ch, co
```

---

## 雨幕 / 矩阵特效

### 列雨（向量化）
```python
def eff_matrix_rain(g, f, t, S, hue=0.33, bri=0.6, pal=PAL_KATA,
                    speed_base=0.5, speed_beat=3.0):
    """向量化的矩阵雨。S dict 持久化各列位置。"""
    if "ry" not in S or len(S["ry"]) != g.cols:
        S["ry"] = np.random.uniform(-g.rows, g.rows, g.cols).astype(np.float32)
        S["rsp"] = np.random.uniform(0.3, 2.0, g.cols).astype(np.float32)
        S["rln"] = np.random.randint(8, 40, g.cols)
        S["rch"] = np.random.randint(0, len(pal), (g.rows, g.cols))  # 预分配字符

    speed_mult = speed_base + f.get("bass", 0.3)*speed_beat + f.get("sub_r", 0.3)*3
    if f.get("beat", 0) > 0: speed_mult *= 2.5
    S["ry"] += S["rsp"] * speed_mult

    # 重置掉到底部以下的列
    rst = (S["ry"] - S["rln"]) > g.rows
    S["ry"][rst] = np.random.uniform(-25, -2, rst.sum())

    # 用 fancy indexing 向量化绘制
    ch = np.full((g.rows, g.cols), " ", dtype="U1")
    co = np.zeros((g.rows, g.cols, 3), dtype=np.uint8)
    heads = S["ry"].astype(int)
    for c in range(g.cols):
        head = heads[c]
        trail_len = S["rln"][c]
        for i in range(trail_len):
            row = head - i
            if 0 <= row < g.rows:
                fade = 1.0 - i / trail_len
                ci = S["rch"][row, c] % len(pal)
                ch[row, c] = pal[ci]
                v = fade * bri * 255
                if i == 0:  # 头部为明亮的偏白色
                    co[row, c] = (int(v*0.9), int(min(255, v*1.1)), int(v*0.9))
                else:
                    R, G, B = hsv2rgb_single(hue, 0.7, fade * bri)
                    co[row, c] = (R, G, B)
    return ch, co, S
```

---

## 故障 / 数据特效

### 水平条带位移
```python
def eff_glitch_displace(ch, co, f, intensity=1.0):
    n_bands = int(8 + f.get("flux", 0.3)*25 + f.get("bdecay", 0)*15) * intensity
    for _ in range(int(n_bands)):
        y = random.randint(0, ch.shape[0]-1)
        h = random.randint(1, int(3 + f.get("sub", 0.3)*8))
        shift = int((random.random()-0.5) * f.get("rms", 0.3)*40 + f.get("bdecay", 0)*20*(random.random()-0.5))
        if shift != 0:
            for row in range(h):
                rr = y + row
                if 0 <= rr < ch.shape[0]:
                    ch[rr] = np.roll(ch[rr], shift)
                    co[rr] = np.roll(co[rr], shift, axis=0)
    return ch, co
```

### 块状损坏
```python
def eff_block_corrupt(ch, co, f, char_pool=None, count_base=20):
    if char_pool is None:
        char_pool = list(PAL_BLOCKS[4:] + PAL_KATA[2:8])
    for _ in range(int(count_base + f.get("flux", 0.3)*60 + f.get("bdecay", 0)*40)):
        bx = random.randint(0, max(1, ch.shape[1]-6))
        by = random.randint(0, max(1, ch.shape[0]-4))
        bw, bh = random.randint(2,6), random.randint(1,4)
        block_char = random.choice(char_pool)
        # 用单一字符和随机颜色填充矩形
        for r in range(bh):
            for c in range(bw):
                rr, cc = by+r, bx+c
                if 0 <= rr < ch.shape[0] and 0 <= cc < ch.shape[1]:
                    ch[rr, cc] = block_char
                    co[rr, cc] = (random.randint(100,255), random.randint(0,100), random.randint(0,80))
    return ch, co
```

### 扫描条（垂直）
```python
def eff_scanbars(ch, co, f, t, n_base=4, chars="|║|!1l"):
    for bi in range(int(n_base + f.get("himid_r", 0.3)*12)):
        sx = int((t*50*(1+bi*0.3) + bi*37) % ch.shape[1])
        for rr in range(ch.shape[0]):
            if random.random() < 0.7:
                ch[rr, sx] = random.choice(chars)
    return ch, co
```

### 错误信息
```python
# 按项目参数化错误词汇表：
ERRORS_TECH = ["SEGFAULT","0xDEADBEEF","BUFFER_OVERRUN","PANIC!","NULL_PTR",
               "CORRUPT","SIGSEGV","ERR_OVERFLOW","STACK_SMASH","BAD_ALLOC"]
ERRORS_COSMIC = ["VOID_BREACH","ENTROPY_MAX","SINGULARITY","DIMENSION_FAULT",
                 "REALITY_ERR","TIME_PARADOX","DARK_MATTER_LEAK","QUANTUM_DECOHERE"]
ERRORS_ORGANIC = ["CELL_DIVISION_ERR","DNA_MISMATCH","MUTATION_OVERFLOW",
                  "NEURAL_DEADLOCK","SYNAPSE_TIMEOUT","MEMBRANE_BREACH"]
```

### 十六进制数据流
```python
hex_str = "".join(random.choice("0123456789ABCDEF") for _ in range(random.randint(8,20)))
stamp(ch, co, hex_str, rand_row, rand_col, (0, 160, 80))
```

---

## 频谱 / 可视化

### 镜像频谱条
```python
def eff_spectrum(g, f, t, n_bars=64, pal=PAL_BLOCKS, mirror=True):
    bar_w = max(1, g.cols // n_bars); mid = g.rows // 2
    band_vals = np.array([f.get("sub",0.3), f.get("bass",0.3), f.get("lomid",0.3),
                          f.get("mid",0.3), f.get("himid",0.3), f.get("hi",0.3)])
    ch = np.full((g.rows, g.cols), " ", dtype="U1")
    co = np.zeros((g.rows, g.cols, 3), dtype=np.uint8)
    for b in range(n_bars):
        frac = b / n_bars
        fi = frac * 5; lo_i = int(fi); hi_i = min(lo_i+1, 5)
        bval = min(1, (band_vals[lo_i]*(1-fi%1) + band_vals[hi_i]*(fi%1)) * 1.8)
        height = int(bval * (g.rows//2 - 2))
        for dy in range(height):
            hue = (f.get("cent",0.5)*0.3 + frac*0.3 + dy/max(height,1)*0.15) % 1.0
            ci = pal[min(int(dy/max(height,1)*len(pal)*0.7+len(pal)*0.2), len(pal)-1)]
            for dc in range(bar_w - (1 if bar_w > 2 else 0)):
                cc = b*bar_w + dc
                if 0 <= cc < g.cols:
                    rows_to_draw = [mid - dy, mid + dy] if mirror else [g.rows - 1 - dy]
                    for row in rows_to_draw:
                        if 0 <= row < g.rows:
                            ch[row, cc] = ci
                            co[row, cc] = hsv_to_rgb_single(hue, 0.85, 0.5+dy/max(height,1)*0.5)
    return ch, co
```

### 波形
```python
def eff_waveform(g, f, t, row_offset=-5, hue=0.1):
    ch = np.full((g.rows, g.cols), " ", dtype="U1")
    co = np.zeros((g.rows, g.cols, 3), dtype=np.uint8)
    for c in range(g.cols):
        wv = (math.sin(c*0.15+t*5)*f.get("bass",0.3)*0.5
            + math.sin(c*0.3+t*8)*f.get("mid",0.3)*0.3
            + math.sin(c*0.6+t*12)*f.get("hi",0.3)*0.15)
        wr = g.rows + row_offset + int(wv * 4)
        if 0 <= wr < g.rows:
            ch[wr, c] = "~"
            v = int(120 + f.get("rms",0.3)*135)
            co[wr, c] = [v, int(v*0.7), int(v*0.4)]
    return ch, co
```

---

## 火焰 / 岩浆

### 火焰柱
```python
def eff_fire(g, f, t, n_base=20, hue_base=0.02, hue_range=0.12, pal=PAL_BLOCKS):
    n_cols = int(n_base + f.get("bass",0.3)*30 + f.get("sub_r",0.3)*20)
    ch = np.full((g.rows, g.cols), " ", dtype="U1")
    co = np.zeros((g.rows, g.cols, 3), dtype=np.uint8)
    for fi in range(n_cols):
        fx_c = int((fi*g.cols/n_cols + np.sin(t*2+fi*0.7)*3) % g.cols)
        height = int((f.get("bass",0.3)*0.4 + f.get("sub_r",0.3)*0.3 + f.get("rms",0.3)*0.3) * g.rows * 0.7)
        for dy in range(min(height, g.rows)):
            fr = g.rows - 1 - dy
            frac = dy / max(height, 1)
            bri = max(0.1, (1 - frac*0.6) * (0.5 + f.get("rms",0.3)*0.5))
            hue = hue_base + frac * hue_range
            ci = "█" if frac<0.2 else ("▓" if frac<0.4 else ("▒" if frac<0.6 else "░"))
            ch[fr, fx_c] = ci
            R, G, B = hsv2rgb_single(hue, 0.9, bri)
            co[fr, fx_c] = (R, G, B)
    return ch, co
```

### 冰 / 冷火（结构相同，hue 范围不同）
```python
# hue_base=0.55, hue_range=0.15 —— 蓝色到青色
# 强度更低，移动更慢
```

---

## 文字叠加

### 滚动字幕
```python
def eff_ticker(ch, co, t, text, row, speed=15, color=(80, 100, 140)):
    off = int(t * speed) % max(len(text), 1)
    doubled = text + "   " + text
    stamp(ch, co, doubled[off:off+ch.shape[1]], row, 0, color)
```

### 节拍触发文字
```python
def eff_beat_words(ch, co, f, words, row_center=None, color=(255,240,220)):
    if f.get("beat", 0) > 0:
        w = random.choice(words)
        r = (row_center or ch.shape[0]//2) + random.randint(-5,5)
        stamp(ch, co, w, r, (ch.shape[1]-len(w))//2, color)
```

### 渐隐消息序列
```python
def eff_fading_messages(ch, co, t, elapsed, messages, period=4.0, color_base=(220,220,220)):
    msg_idx = int(elapsed / period) % len(messages)
    phase = elapsed % period
    fade = max(0, min(1.0, phase) * min(1.0, period - phase))
    if fade > 0.05:
        v = fade
        msg = messages[msg_idx]
        cr, cg, cb = [int(c * v) for c in color_base]
        stamp(ch, co, msg, ch.shape[0]//2, (ch.shape[1]-len(msg))//2, (cr, cg, cb))
```

---

## 屏幕抖动
按节拍平移整个字符/颜色数组：
```python
def eff_shake(ch, co, f, x_amp=6, y_amp=3):
    shake_x = int(f.get("sub",0.3)*x_amp*(random.random()-0.5)*2 + f.get("bdecay",0)*4*(random.random()-0.5)*2)
    shake_y = int(f.get("bass",0.3)*y_amp*(random.random()-0.5)*2)
    if abs(shake_x) > 0:
        ch = np.roll(ch, shake_x, axis=1)
        co = np.roll(co, shake_x, axis=1)
    if abs(shake_y) > 0:
        ch = np.roll(ch, shake_y, axis=0)
        co = np.roll(co, shake_y, axis=0)
    return ch, co
```

---

## 可组合特效系统

真正的创意威力来自**组合**。共有三个层级：

### 第 1 层：字符级叠加

将多个特效作为 `(chars, colors)` 层堆叠：

```python
class LayerStack(EffectNode):
    """自下而上渲染特效，并进行字符级合成。"""
    def add(self, effect, alpha=1.0):
        """alpha < 1.0 = 概率性覆盖（稀疏叠加）。"""
        self.layers.append((effect, alpha))

# 用法：
stack = LayerStack()
stack.add(bg_effect)           # 底层 —— 铺满屏幕
stack.add(main_effect)         # 在其上叠加（空格字符 = 透明）
stack.add(particle_effect)     # 再在其上做稀疏叠加
ch, co = stack.render(g, f, t, S)
```

### 第 2 层：像素级混合

渲染到画布后，用类 Photoshop 的混合模式进行混合：

```python
class PixelBlendStack:
    """用混合模式堆叠画布，实现复杂合成。"""
    def add(self, canvas, mode="normal", opacity=1.0)
    def composite(self) -> canvas

# 用法：
pbs = PixelBlendStack()
pbs.add(canvas_a)                        # 底层
pbs.add(canvas_b, "screen", 0.7)        # 加性辉光
pbs.add(canvas_c, "difference", 0.5)    # 迷幻的干涉
result = pbs.composite()
```

### 第 3 层：时间反馈

把上一帧反馈到当前帧，实现递归效果：

```python
fb = FeedbackBuffer()
for each frame:
    canvas = render_current()
    canvas = fb.apply(canvas, decay=0.8, blend="screen",
                      transform="zoom", transform_amt=0.015, hue_shift=0.02)
```

### 特效节点 —— 统一接口

在 v2 协议中，特效节点在场景函数**内部**使用。场景函数本身返回一个画布。特效节点生成中间的 `(chars, colors)`，通过网格的 `.render()` 方法或 `_render_vf()` 渲染到画布。

```python
class EffectNode:
    def render(self, g, f, t, S) -> (chars, colors)

# 具体实现：
class ValueFieldEffect(EffectNode):
    """包装一个 value 场函数 + hue 场函数 + 调色板。"""
    def __init__(self, val_fn, hue_fn, pal=PAL_DEFAULT, sat=0.7)

class LambdaEffect(EffectNode):
    """包装任意 (g,f,t,S) -> (ch,co) 函数。"""
    def __init__(self, fn)

class ConditionalEffect(EffectNode):
    """根据音频特征切换特效。"""
    def __init__(self, condition, if_true, if_false=None)
```

### Value 场生成器（原子构建块）

它们生成范围在 [0,1] 的 float32 数组 `(rows, cols)`。这些就是原始的视觉图案。所有函数签名都是 `(g, f, t, S, **params) -> float32 array`。

#### 三角函数场（基于 sine/cosine）

```python
def vf_sinefield(g, f, t, S, bri=0.5,
                 freq=(0.13, 0.17, 0.07, 0.09), speed=(0.5, -0.4, -0.3, 0.2)):
    """多层正弦场。通用背景/纹理。"""
    v1 = np.sin(g.cc*freq[0] + t*speed[0]) * np.sin(g.rr*freq[1] - t*speed[1]) * 0.5 + 0.5
    v2 = np.sin(g.cc*freq[2] - t*speed[2] + g.rr*freq[3]) * 0.4 + 0.5
    v3 = np.sin(g.dist_n*5 + t*0.2) * 0.3 + 0.4
    return np.clip((v1*0.35 + v2*0.35 + v3*0.3) * bri * (0.6 + f.get("rms",0.3)*0.6), 0, 1)

def vf_smooth_noise(g, f, t, S, octaves=3, bri=0.5):
    """多倍频正弦近似的柏林噪点。"""
    val = np.zeros((g.rows, g.cols), dtype=np.float32)
    for i in range(octaves):
        freq = 0.05 * (2 ** i); amp = 0.5 / (i + 1)
        phase = t * (0.3 + i * 0.2)
        val = val + np.sin(g.cc*freq + phase) * np.cos(g.rr*freq*0.7 - phase*0.5) * amp
    return np.clip(val * 0.5 + 0.5, 0, 1) * bri

def vf_rings(g, f, t, S, n_base=6, spacing_base=4):
    """同心圆环，数量和摆动由低音驱动。"""
    n = int(n_base + f.get("sub_r",0.3)*25 + f.get("bass",0.3)*10)
    sp = spacing_base + f.get("bass_r",0.3)*7 + f.get("rms",0.3)*3
    val = np.zeros((g.rows, g.cols), dtype=np.float32)
    for ri in range(n):
        rad = (ri+1)*sp + f.get("bdecay",0)*15
        wobble = f.get("mid_r",0.3)*5*np.sin(g.angle*3+t*4)
        rd = np.abs(g.dist - rad - wobble)
        th = 1 + f.get("sub",0.3)*3
        val = np.maximum(val, np.clip((1 - rd/th) * (0.4 + f.get("bass",0.3)*0.8), 0, 1))
    return val

def vf_spiral(g, f, t, S, n_arms=3, tightness=2.5):
    """对数螺旋臂。"""
    val = np.zeros((g.rows, g.cols), dtype=np.float32)
    for ai in range(n_arms):
        offset = ai * 2*np.pi / n_arms
        log_r = np.log(g.dist + 1) * tightness
        arm_phase = g.angle + offset - log_r + t * 0.8
        arm_val = np.clip(np.cos(arm_phase * n_arms) * 0.6 + 0.2, 0, 1)
        arm_val *= (0.4 + f.get("rms",0.3)*0.6) * np.clip(1 - g.dist_n*0.5, 0.2, 1)
        val = np.maximum(val, arm_val)
    return val

def vf_tunnel(g, f, t, S, speed=3.0, complexity=6):
    """隧道纵深感 —— 无限缩放的感觉。"""
    tunnel_d = 1.0 / (g.dist_n + 0.1)
    v1 = np.sin(tunnel_d*2 - t*speed) * 0.45 + 0.55
    v2 = np.sin(g.angle*complexity + tunnel_d*1.5 - t*2) * 0.35 + 0.55
    return np.clip(v1*0.5 + v2*0.5, 0, 1)

def vf_vortex(g, f, t, S, twist=3.0):
    """扭转的径向图案 —— 距离调制角度。"""
    twisted = g.angle + g.dist_n * twist * np.sin(t * 0.5)
    val = np.sin(twisted * 4 - t * 2) * 0.5 + 0.5
    return np.clip(val * (0.5 + f.get("bass",0.3)*0.8), 0, 1)

def vf_interference(g, f, t, S, n_waves=6):
    """重叠的正弦波，产生莫尔条纹。"""
    drivers = ["mid_r", "himid_r", "bass_r", "lomid_r", "hi_r", "sub_r"]
    vals = np.zeros((g.rows, g.cols), dtype=np.float32)
    for i in range(min(n_waves, len(drivers))):
        angle = i * np.pi / n_waves
        freq = 0.06 + i * 0.03; sp = 0.5 + i * 0.3
        proj = g.cc * np.cos(angle) + g.rr * np.sin(angle)
        vals = vals + np.sin(proj*freq + t*sp) * f.get(drivers[i], 0.3) * 2.5
    return np.clip(vals * 0.12 + 0.45, 0.1, 1)

def vf_aurora(g, f, t, S, n_bands=3):
    """水平极光带。"""
    val = np.zeros((g.rows, g.cols), dtype=np.float32)
    for i in range(n_bands):
        fr = 0.08 + i*0.04; fc = 0.012 + i*0.008
        sr = 0.7 + i*0.3; sc = 0.18 + i*0.12
        val = val + np.sin(g.rr*fr + t*sr) * np.sin(g.cc*fc + t*sc) * (0.6/n_bands)
    return np.clip(val * (f.get("lomid_r",0.3)*3 + 0.2), 0, 0.7)

def vf_ripple(g, f, t, S, sources=None, freq=0.3, damping=0.02):
    """从点源发出的同心涟漪。"""
    if sources is None: sources = [(0.5, 0.5)]
    val = np.zeros((g.rows, g.cols), dtype=np.float32)
    for ry, rx in sources:
        dy = g.rr - g.rows*ry; dx = g.cc - g.cols*rx
        d = np.sqrt(dy**2 + dx**2)
        val = val + np.sin(d*freq - t*4) * np.exp(-d*damping) * 0.5
    return np.clip(val + 0.5, 0, 1)

def vf_plasma(g, f, t, S):
    """经典等离子体：不同方向和速度的正弦叠加。"""
    v = np.sin(g.cc * 0.03 + t * 0.7) * 0.5
    v = v + np.sin(g.rr * 0.04 - t * 0.5) * 0.4
    v = v + np.sin((g.cc * 0.02 + g.rr * 0.03) + t * 0.3) * 0.3
    v = v + np.sin(g.dist_n * 4 - t * 0.8) * 0.3
    return np.clip(v * 0.5 + 0.5, 0, 1)

def vf_diamond(g, f, t, S, freq=0.15):
    """菱形/棋盘图案。"""
    val = np.abs(np.sin(g.cc * freq + t * 0.5)) * np.abs(np.sin(g.rr * freq * 1.2 - t * 0.3))
    return np.clip(val * (0.6 + f.get("rms",0.3)*0.8), 0, 1)

def vf_noise_static(g, f, t, S, density=0.4):
    """随机噪点 —— 每帧都不同。非确定性。"""
    return np.random.random((g.rows, g.cols)).astype(np.float32) * density * (0.5 + f.get("rms",0.3)*0.5)
```

#### 基于噪点的场（有机、非周期）

它们生成与正弦场质感截然不同的纹理 —— 有机、不重复、没有可见的轴向对齐。这是高端生成艺术的基础。

```python
def _hash2d(ix, iy):
    """整数坐标哈希，用于梯度噪点。返回 [0,1] 的 float32。"""
    # 用大质数混合得到高质量哈希
    n = ix * 374761393 + iy * 668265263
    n = (n ^ (n >> 13)) * 1274126177
    return ((n ^ (n >> 16)) & 0x7fffffff).astype(np.float32) / 0x7fffffff

def _smoothstep(t):
    """Hermite smoothstep：3t^2 - 2t^3。在 [0,1] 内平滑插值。"""
    t = np.clip(t, 0, 1)
    return t * t * (3 - 2 * t)

def _smootherstep(t):
    """Perlin 改进版 smoothstep：6t^5 - 15t^4 + 10t^3。C2 连续。"""
    t = np.clip(t, 0, 1)
    return t * t * t * (t * (t * 6 - 15) + 10)

def _value_noise_2d(x, y):
    """任意浮点坐标处的 2D value 噪点。返回 [0,1] 的 float32。
    x, y：同形状的 float32 数组。"""
    ix = np.floor(x).astype(np.int64)
    iy = np.floor(y).astype(np.int64)
    fx = _smootherstep(x - ix)
    fy = _smootherstep(y - iy)
    # 4 个角点的哈希
    n00 = _hash2d(ix, iy)
    n10 = _hash2d(ix + 1, iy)
    n01 = _hash2d(ix, iy + 1)
    n11 = _hash2d(ix + 1, iy + 1)
    # 双线性插值
    nx0 = n00 * (1 - fx) + n10 * fx
    nx1 = n01 * (1 - fx) + n11 * fx
    return nx0 * (1 - fy) + nx1 * fy

def vf_noise(g, f, t, S, freq=0.08, speed=0.3, bri=0.7):
    """Value 噪点。平滑、有机、无轴向对齐瑕疵。
    freq：空间频率（越高 = 细节越细）。
    speed：时间滚动速率。"""
    x = g.cc * freq + t * speed
    y = g.rr * freq * 0.8 - t * speed * 0.4
    return np.clip(_value_noise_2d(x, y) * bri, 0, 1)

def vf_fbm(g, f, t, S, octaves=5, freq=0.06, lacunarity=2.0, gain=0.5,
           speed=0.2, bri=0.8):
    """分形布朗运动 —— 带 lacunarity/gain 控制的倍频噪点。
    云、地形、烟雾、有机纹理的标准构建块。

    octaves：噪点层数（越多 = 细节越细，开销越大）
    freq：基础空间频率
    lacunarity：每倍频的频率乘数（2.0 = 标准）
    gain：每倍频的振幅乘数（0.5 = 标准，<0.5 = 更平滑）
    speed：时间演化速率
    """
    val = np.zeros((g.rows, g.cols), dtype=np.float32)
    amplitude = 1.0
    f_x = freq
    f_y = freq * 0.85  # 轻微各向异性可避免网格瑕疵
    for i in range(octaves):
        phase = t * speed * (1 + i * 0.3)
        x = g.cc * f_x + phase + i * 17.3  # 每倍频偏移
        y = g.rr * f_y - phase * 0.6 + i * 31.7
        val = val + _value_noise_2d(x, y) * amplitude
        amplitude *= gain
        f_x *= lacunarity
        f_y *= lacunarity
    # 归一化到 [0,1]
    max_amp = (1 - gain ** octaves) / (1 - gain) if gain != 1 else octaves
    return np.clip(val / max_amp * bri * (0.6 + f.get("rms", 0.3) * 0.6), 0, 1)

def vf_domain_warp(g, f, t, S, base_fn=None, warp_fn=None,
                   warp_strength=15.0, freq=0.06, speed=0.2):
    """域扭曲 —— 把一个噪点场的输出作为坐标偏移喂给另一个噪点场。
    产生流动、融化的有机扭曲。高端生成艺术的标志性技法（Inigo Quilez）。

    base_fn：要被扭曲的 value 场（默认：fbm）
    warp_fn：用于位移的 value 场（默认：不同频率的 noise）
    warp_strength：位移多少个网格单元（越高 = 扭曲越强）
    """
    # 扭曲场：x 和 y 方向的位移
    wx = _value_noise_2d(g.cc * freq * 1.3 + t * speed, g.rr * freq + 7.1)
    wy = _value_noise_2d(g.cc * freq + t * speed * 0.7 + 3.2, g.rr * freq * 1.1 - 11.8)
    # 把扭曲中心化到 0（噪点返回 [0,1]，平移到 [-0.5, 0.5]）
    wx = (wx - 0.5) * warp_strength * (0.5 + f.get("rms", 0.3) * 1.0)
    wy = (wy - 0.5) * warp_strength * (0.5 + f.get("bass", 0.3) * 0.8)
    # 在扭曲后的坐标处采样基础场
    warped_cc = g.cc + wx
    warped_rr = g.rr + wy
    if base_fn is not None:
        # 创建一个带扭曲坐标的临时类网格对象
        # 简化：用修改后的坐标评估 base_fn
        val = _value_noise_2d(warped_cc * freq * 0.8 + t * speed * 0.5,
                              warped_rr * freq * 0.7 - t * speed * 0.3)
    else:
        # 默认：在扭曲坐标处做 fbm
        val = np.zeros((g.rows, g.cols), dtype=np.float32)
        amp = 1.0
        fx, fy = freq * 0.8, freq * 0.7
        for i in range(4):
            val = val + _value_noise_2d(warped_cc * fx + t * speed * 0.5 + i * 13.7,
                                        warped_rr * fy - t * speed * 0.3 + i * 27.3) * amp
            amp *= 0.5; fx *= 2.0; fy *= 2.0
        val = val / 1.875  # 归一化 4 倍频之和
    return np.clip(val * 0.8, 0, 1)

def vf_voronoi(g, f, t, S, n_cells=20, speed=0.3, edge_width=1.5,
               mode="distance", seed=42):
    """作为 value 场的 Voronoi 图。用最近/次最近距离正确实现，
    得到单元内部和边缘。

    mode："distance"（中心亮、边缘暗）、
          "edge"（单元边界亮）、
          "cell_id"（每个单元平涂一种颜色 —— 配合离散调色板使用）
    edge_width：边缘高亮厚度（用于 "edge" 模式）
    """
    rng = np.random.RandomState(seed)
    # 动画化的单元中心
    cx = rng.rand(n_cells).astype(np.float32) * g.cols
    cy = rng.rand(n_cells).astype(np.float32) * g.rows
    vx = (rng.rand(n_cells).astype(np.float32) - 0.5) * speed * 10
    vy = (rng.rand(n_cells).astype(np.float32) - 0.5) * speed * 10
    cx_t = (cx + vx * np.sin(t * 0.5 + np.arange(n_cells) * 0.8)) % g.cols
    cy_t = (cy + vy * np.cos(t * 0.4 + np.arange(n_cells) * 1.1)) % g.rows

    # 计算最近和次近距离
    d1 = np.full((g.rows, g.cols), 1e9, dtype=np.float32)
    d2 = np.full((g.rows, g.cols), 1e9, dtype=np.float32)
    id1 = np.zeros((g.rows, g.cols), dtype=np.int32)
    for i in range(n_cells):
        d = np.sqrt((g.cc - cx_t[i]) ** 2 + (g.rr - cy_t[i]) ** 2)
        mask = d < d1
        d2 = np.where(mask, d1, np.minimum(d2, d))
        id1 = np.where(mask, i, id1)
        d1 = np.minimum(d1, d)

    if mode == "edge":
        # 边缘：d2 - d1 较小处
        edge_val = np.clip(1.0 - (d2 - d1) / edge_width, 0, 1)
        return edge_val * (0.5 + f.get("rms", 0.3) * 0.8)
    elif mode == "cell_id":
        # 每个单元的平涂值
        return (id1.astype(np.float32) / n_cells) % 1.0
    else:
        # 距离：中心附近亮，边缘暗
        max_d = g.cols * 0.15
        return np.clip(1.0 - d1 / max_d, 0, 1) * (0.5 + f.get("rms", 0.3) * 0.7)
```

#### 基于仿真的场（涌现、演化）

它们利用持久化状态 `S` 逐帧演化图案。能产生无状态数学无法达到的复杂度。

```python
def vf_reaction_diffusion(g, f, t, S, feed=0.055, kill=0.062,
                          da=1.0, db=0.5, dt=1.0, steps_per_frame=8,
                          init_mode="spots"):
    """Gray-Scott 反应-扩散模型。根据 feed/kill 不同，
    产生珊瑚、豹纹、有丝分裂、蠕虫状和迷宫状图案。

    两种化学物质 A 和 B 相互作用：
        A + 2B → 3B  （自催化）
        B → P        （衰减）
        feed：A 的补充速率，kill：B 的衰减速率
    不同的 feed/kill 比例会产生截然不同的图案。

    预设（feed, kill）：
        斑点/圆点：       (0.055, 0.062)
        蠕虫/条纹：       (0.046, 0.063)
        珊瑚/分叉：       (0.037, 0.060)
        有丝分裂/分裂：    (0.028, 0.062)
        迷宫：            (0.029, 0.057)
        孔洞/负形：       (0.039, 0.058)
        混沌/不稳定：     (0.026, 0.051)

    steps_per_frame：每视频帧的仿真步数（越多 = 演化越快）
    """
    key = "rd_" + str(id(g))  # 每个网格唯一
    if key + "_a" not in S:
        # 初始化化学物质场
        A = np.ones((g.rows, g.cols), dtype=np.float32)
        B = np.zeros((g.rows, g.cols), dtype=np.float32)
        if init_mode == "spots":
            # 随机种子点
            rng = np.random.RandomState(42)
            for _ in range(max(3, g.rows * g.cols // 200)):
                r, c = rng.randint(2, g.rows - 2), rng.randint(2, g.cols - 2)
                B[r - 1:r + 2, c - 1:c + 2] = 1.0
        elif init_mode == "center":
            cr, cc = g.rows // 2, g.cols // 2
            B[cr - 3:cr + 3, cc - 3:cc + 3] = 1.0
        elif init_mode == "ring":
            mask = (g.dist_n > 0.2) & (g.dist_n < 0.3)
            B[mask] = 1.0
        S[key + "_a"] = A
        S[key + "_b"] = B

    A = S[key + "_a"]
    B = S[key + "_b"]

    # 音频调制：feed/kill 随音频轻微偏移
    f_mod = feed + f.get("bass", 0.3) * 0.003
    k_mod = kill + f.get("hi_r", 0.3) * 0.002

    for _ in range(steps_per_frame):
        # 用 3x3 卷积核求拉普拉斯算子
        # [0.05, 0.2, 0.05]
        # [0.2, -1.0, 0.2]
        # [0.05, 0.2, 0.05]
        pA = np.pad(A, 1, mode="wrap")
        pB = np.pad(B, 1, mode="wrap")
        lapA = (pA[:-2, 1:-1] + pA[2:, 1:-1] + pA[1:-1, :-2] + pA[1:-1, 2:]) * 0.2 \
             + (pA[:-2, :-2] + pA[:-2, 2:] + pA[2:, :-2] + pA[2:, 2:]) * 0.05 \
             - A * 1.0
        lapB = (pB[:-2, 1:-1] + pB[2:, 1:-1] + pB[1:-1, :-2] + pB[1:-1, 2:]) * 0.2 \
             + (pB[:-2, :-2] + pB[:-2, 2:] + pB[2:, :-2] + pB[2:, 2:]) * 0.05 \
             - B * 1.0
        ABB = A * B * B
        A = A + (da * lapA - ABB + f_mod * (1 - A)) * dt
        B = B + (db * lapB + ABB - (f_mod + k_mod) * B) * dt
        A = np.clip(A, 0, 1)
        B = np.clip(B, 0, 1)

    S[key + "_a"] = A
    S[key + "_b"] = B
    # 输出 B 化学物质作为 value（可见图案）
    return np.clip(B * 2.0, 0, 1)

def vf_game_of_life(g, f, t, S, rule="life", birth=None, survive=None,
                    steps_per_frame=1, density=0.3, fade=0.92, seed=42):
    """作为 value 场的元胞自动机，带模拟渐隐拖尾。
    网格单元按邻居数量规则出生/死亡。死亡单元逐渐淡出
    而非瞬间变黑，产生残影。

    rule 预设：
        "life":     B3/S23（Conway 生命游戏）
        "coral":    B3/S45678（缓慢结晶式生长）
        "maze":     B3/S12345（填满成迷宫）
        "anneal":   B4678/S35678（平滑色块）
        "day_night": B3678/S34678（生长/衰亡平衡）
    或直接指定 birth/survive 集合：birth={3}, survive={2,3}

    fade：死亡单元变暗的速度（0.9 = 慢拖尾，0.5 = 快）
    """
    presets = {
        "life":      ({3}, {2, 3}),
        "coral":     ({3}, {4, 5, 6, 7, 8}),
        "maze":      ({3}, {1, 2, 3, 4, 5}),
        "anneal":    ({4, 6, 7, 8}, {3, 5, 6, 7, 8}),
        "day_night": ({3, 6, 7, 8}, {3, 4, 6, 7, 8}),
    }
    if birth is None or survive is None:
        birth, survive = presets.get(rule, presets["life"])

    key = "gol_" + str(id(g))
    if key + "_grid" not in S:
        rng = np.random.RandomState(seed)
        S[key + "_grid"] = (rng.random((g.rows, g.cols)) < density).astype(np.float32)
        S[key + "_display"] = S[key + "_grid"].copy()

    grid = S[key + "_grid"]
    display = S[key + "_display"]

    # 节拍可注入随机噪点
    if f.get("beat", 0) > 0.5:
        inject = np.random.random((g.rows, g.cols)) < 0.02
        grid = np.clip(grid + inject.astype(np.float32), 0, 1)

    for _ in range(steps_per_frame):
        # 统计邻居数（环面环绕）
        padded = np.pad(grid > 0.5, 1, mode="wrap").astype(np.int8)
        neighbors = (padded[:-2, :-2] + padded[:-2, 1:-1] + padded[:-2, 2:] +
                     padded[1:-1, :-2] +                     padded[1:-1, 2:] +
                     padded[2:, :-2]  + padded[2:, 1:-1]  + padded[2:, 2:])
        alive = grid > 0.5
        new_alive = np.zeros_like(grid, dtype=bool)
        for b in birth:
            new_alive |= (~alive) & (neighbors == b)
        for s in survive:
            new_alive |= alive & (neighbors == s)
        grid = new_alive.astype(np.float32)

    # 模拟显示：存活单元 = 1.0，死亡单元渐隐
    display = np.where(grid > 0.5, 1.0, display * fade)
    S[key + "_grid"] = grid
    S[key + "_display"] = display
    return np.clip(display, 0, 1)

def vf_strange_attractor(g, f, t, S, attractor="clifford",
                         n_points=50000, warmup=500, bri=0.8, seed=42,
                         params=None):
    """投影到 2D 密度场的奇异吸引子。
    把 N 个点迭代通过吸引子方程，分箱到网格，
    生成密度图。优雅、不重复的曲线。

    attractor 预设：
        "clifford":  sin(a*y) + c*cos(a*x), sin(b*x) + d*cos(b*y)
        "de_jong":   sin(a*y) - cos(b*x), sin(c*x) - cos(d*y)
        "bedhead":   sin(x*y/b) + cos(a*x - y), x*sin(a*y) + cos(b*x - y)

    params：(a, b, c, d) 浮点数 —— 每个吸引子有不同的甜点区间。
            若为 None，使用随时间变化的默认值以产生动画。
    """
    key = "attr_" + attractor
    if params is None:
        # 随时间变化的参数，缓慢变形
        a = -1.4 + np.sin(t * 0.05) * 0.3
        b = 1.6 + np.cos(t * 0.07) * 0.2
        c = 1.0 + np.sin(t * 0.03 + 1) * 0.3
        d = 0.7 + np.cos(t * 0.04 + 2) * 0.2
    else:
        a, b, c, d = params

    # 迭代吸引子
    rng = np.random.RandomState(seed)
    x = rng.uniform(-0.1, 0.1, n_points).astype(np.float64)
    y = rng.uniform(-0.1, 0.1, n_points).astype(np.float64)

    # 预热迭代（到达吸引子）
    for _ in range(warmup):
        if attractor == "clifford":
            xn = np.sin(a * y) + c * np.cos(a * x)
            yn = np.sin(b * x) + d * np.cos(b * y)
        elif attractor == "de_jong":
            xn = np.sin(a * y) - np.cos(b * x)
            yn = np.sin(c * x) - np.cos(d * y)
        elif attractor == "bedhead":
            xn = np.sin(x * y / b) + np.cos(a * x - y)
            yn = x * np.sin(a * y) + np.cos(b * x - y)
        else:
            xn = np.sin(a * y) + c * np.cos(a * x)
            yn = np.sin(b * x) + d * np.cos(b * y)
        x, y = xn, yn

    # 分箱到网格
    # 找边界
    margin = 0.1
    x_min, x_max = x.min() - margin, x.max() + margin
    y_min, y_max = y.min() - margin, y.max() + margin

    # 映射到网格坐标
    gx = ((x - x_min) / (x_max - x_min) * (g.cols - 1)).astype(np.int32)
    gy = ((y - y_min) / (y_max - y_min) * (g.rows - 1)).astype(np.int32)
    valid = (gx >= 0) & (gx < g.cols) & (gy >= 0) & (gy < g.rows)
    gx, gy = gx[valid], gy[valid]

    # 累加密度
    density = np.zeros((g.rows, g.cols), dtype=np.float32)
    np.add.at(density, (gy, gx), 1.0)

    # 对密度做对数缩放以提升可见性（多数分箱命中很少）
    density = np.log1p(density)
    mx = density.max()
    if mx > 0:
        density = density / mx
    return np.clip(density * bri * (0.5 + f.get("rms", 0.3) * 0.8), 0, 1)
```

#### 基于 SDF 的场（几何精度）

有符号距离场能产生数学上精确的形状。不同于正弦场（有机、模糊），SDF 给出硬几何边界，边缘柔化程度可控。配合域扭曲，能创造「融化的几何」效果。

所有 SDF 图元返回一个**有符号距离**（内部为负，外部为正）。用 `sdf_render()` 转换为 value 场。

```python
def sdf_render(dist, edge_width=1.5, invert=False):
    """把有符号距离转换为 [0,1] value 场。
    edge_width：控制边界的抗锯齿/柔化程度。
    invert：True = 形状内部亮，False = 形状外部亮。"""
    val = 1.0 - np.clip(dist / edge_width, 0, 1) if not invert else np.clip(dist / edge_width, 0, 1)
    return np.clip(val, 0, 1)

def sdf_glow(dist, falloff=0.05):
    """把 SDF 渲染为发光轮廓 —— 边界处亮，向两侧渐隐。"""
    return np.clip(np.exp(-np.abs(dist) * falloff), 0, 1)

# --- 图元 ---

def sdf_circle(g, cx_frac=0.5, cy_frac=0.5, radius=0.3):
    """圆形 SDF。cx/cy/radius 用归一化 [0,1] 坐标。"""
    dx = (g.cc / g.cols - cx_frac) * (g.cols / g.rows)  # 宽高比校正
    dy = g.rr / g.rows - cy_frac
    return np.sqrt(dx**2 + dy**2) - radius

def sdf_box(g, cx_frac=0.5, cy_frac=0.5, w=0.3, h=0.2, round_r=0.0):
    """圆角矩形 SDF。"""
    dx = np.abs(g.cc / g.cols - cx_frac) * (g.cols / g.rows) - w + round_r
    dy = np.abs(g.rr / g.rows - cy_frac) - h + round_r
    outside = np.sqrt(np.maximum(dx, 0)**2 + np.maximum(dy, 0)**2)
    inside = np.minimum(np.maximum(dx, dy), 0)
    return outside + inside - round_r

def sdf_ring(g, cx_frac=0.5, cy_frac=0.5, radius=0.3, thickness=0.03):
    """环形（圆环）SDF。"""
    d = sdf_circle(g, cx_frac, cy_frac, radius)
    return np.abs(d) - thickness

def sdf_line(g, x0=0.2, y0=0.5, x1=0.8, y1=0.5, thickness=0.01):
    """两点之间的线段 SDF（归一化坐标）。"""
    ax = g.cc / g.cols * (g.cols / g.rows) - x0 * (g.cols / g.rows)
    ay = g.rr / g.rows - y0
    bx = (x1 - x0) * (g.cols / g.rows)
    by = y1 - y0
    h = np.clip((ax * bx + ay * by) / (bx * bx + by * by + 1e-10), 0, 1)
    dx = ax - bx * h
    dy = ay - by * h
    return np.sqrt(dx**2 + dy**2) - thickness

def sdf_triangle(g, cx=0.5, cy=0.5, size=0.25):
    """以 (cx, cy) 为中心的等边三角形 SDF。"""
    px = (g.cc / g.cols - cx) * (g.cols / g.rows) / size
    py = (g.rr / g.rows - cy) / size
    # 等边三角形数学
    k = np.sqrt(3.0)
    px = np.abs(px) - 1.0
    py = py + 1.0 / k
    cond = px + k * py > 0
    px2 = np.where(cond, (px - k * py) / 2.0, px)
    py2 = np.where(cond, (-k * px - py) / 2.0, py)
    px2 = np.clip(px2, -2.0, 0.0)
    return -np.sqrt(px2**2 + py2**2) * np.sign(py2) * size

def sdf_star(g, cx=0.5, cy=0.5, n_points=5, outer_r=0.25, inner_r=0.12):
    """星形多边形 SDF —— n 角星。"""
    px = (g.cc / g.cols - cx) * (g.cols / g.rows)
    py = g.rr / g.rows - cy
    angle = np.arctan2(py, px)
    dist = np.sqrt(px**2 + py**2)
    # 星形对称的模角度
    wedge = 2 * np.pi / n_points
    a = np.abs((angle % wedge) - wedge / 2)
    # 在内外半径之间插值
    r_at_angle = inner_r + (outer_r - inner_r) * np.clip(np.cos(a * n_points) * 0.5 + 0.5, 0, 1)
    return dist - r_at_angle

def sdf_heart(g, cx=0.5, cy=0.45, size=0.25):
    """心形 SDF。"""
    px = (g.cc / g.cols - cx) * (g.cols / g.rows) / size
    py = -(g.rr / g.rows - cy) / size + 0.3  # 翻转 y，偏移
    px = np.abs(px)
    cond = (px + py) > 1.0
    d1 = np.sqrt((px - 0.25)**2 + (py - 0.75)**2) - np.sqrt(2.0) / 4.0
    d2 = np.sqrt((px + py - 1.0)**2) / np.sqrt(2.0)
    return np.where(cond, d1, d2) * size

# --- 组合器 ---

def sdf_union(d1, d2):
    """布尔并集 —— 任一 SDF 内部即为形状。"""
    return np.minimum(d1, d2)

def sdf_intersect(d1, d2):
    """布尔交集 —— 两 SDF 重叠处即为形状。"""
    return np.maximum(d1, d2)

def sdf_subtract(d1, d2):
    """布尔减 —— d1 减去 d2。"""
    return np.maximum(d1, -d2)

def sdf_smooth_union(d1, d2, k=0.1):
    """平滑最小值（多项式）—— 用圆角连接融合形状。
    k：平滑半径。越大 = 越圆。"""
    h = np.clip(0.5 + 0.5 * (d2 - d1) / k, 0, 1)
    return d2 * (1 - h) + d1 * h - k * h * (1 - h)

def sdf_smooth_subtract(d1, d2, k=0.1):
    """平滑减 —— d1 减去 d2，带圆角边缘。"""
    return sdf_smooth_union(d1, -d2, k)

def sdf_repeat(g, sdf_fn, spacing_x=0.25, spacing_y=0.25, **sdf_kwargs):
    """无限平铺一个 SDF 图元。spacing 用归一化坐标。"""
    # 模坐标
    mod_cc = (g.cc / g.cols) % spacing_x - spacing_x / 2
    mod_rr = (g.rr / g.rows) % spacing_y - spacing_y / 2
    # 为 SDF 构建修改后的类网格数组
    # 这是简化做法 —— 构建临时命名空间
    class ModGrid:
        pass
    mg = ModGrid()
    mg.cc = mod_cc * g.cols; mg.rr = mod_rr * g.rows
    mg.cols = g.cols; mg.rows = g.rows
    return sdf_fn(mg, **sdf_kwargs)

# --- SDF 作为 Value 场 ---

def vf_sdf(g, f, t, S, sdf_fn=sdf_circle, edge_width=1.5, glow=False,
           glow_falloff=0.03, animate=True, **sdf_kwargs):
    """把任意 SDF 图元包装为标准 vf_* value 场。
    若 animate=True，对形状施加缓慢旋转和呼吸。"""
    if animate:
        sdf_kwargs.setdefault("cx_frac", 0.5)
        sdf_kwargs.setdefault("cy_frac", 0.5)
    d = sdf_fn(g, **sdf_kwargs)
    if glow:
        return sdf_glow(d, glow_falloff) * (0.5 + f.get("rms", 0.3) * 0.8)
    return sdf_render(d, edge_width) * (0.5 + f.get("rms", 0.3) * 0.8)
```

### Hue 场生成器（颜色映射）

它们生成 [0,1] 的 float32 hue 数组。可与任意 value 场独立组合。每个都是一个工厂函数，返回签名为 `(g, f, t, S) -> float32 array` 的闭包。也可以是固定 hue 的纯浮点数。

```python
def hf_fixed(hue):
    """到处都是单一 hue。"""
    def fn(g, f, t, S):
        return np.full((g.rows, g.cols), hue, dtype=np.float32)
    return fn

def hf_angle(offset=0.0):
    """hue 映射到相对中心的角度 —— 彩虹色轮。"""
    def fn(g, f, t, S):
        return (g.angle / (2 * np.pi) + offset + t * 0.05) % 1.0
    return fn

def hf_distance(base=0.5, scale=0.02):
    """hue 映射到到中心的距离。"""
    def fn(g, f, t, S):
        return (base + g.dist * scale + t * 0.03) % 1.0
    return fn

def hf_time_cycle(speed=0.1):
    """hue 随时间均匀循环。"""
    def fn(g, f, t, S):
        return np.full((g.rows, g.cols), (t * speed) % 1.0, dtype=np.float32)
    return fn

def hf_audio_cent():
    """hue 跟随频谱质心 —— 音色色彩漂移。"""
    def fn(g, f, t, S):
        return np.full((g.rows, g.cols), f.get("cent", 0.5) * 0.3, dtype=np.float32)
    return fn

def hf_gradient_h(start=0.0, end=1.0):
    """从左到右的 hue 渐变。"""
    def fn(g, f, t, S):
        h = np.broadcast_to(
            start + (g.cc / g.cols) * (end - start),
            (g.rows, g.cols)
        ).copy()  # .copy() 至关重要 —— 见 troubleshooting.md
        return h % 1.0
    return fn

def hf_gradient_v(start=0.0, end=1.0):
    """从上到下的 hue 渐变。"""
    def fn(g, f, t, S):
        h = np.broadcast_to(
            start + (g.rr / g.rows) * (end - start),
            (g.rows, g.cols)
        ).copy()
        return h % 1.0
    return fn

def hf_plasma(speed=0.3):
    """等离子体风格 hue 场 —— 有机的色彩变化。"""
    def fn(g, f, t, S):
        return (np.sin(g.cc*0.02 + t*speed)*0.5 + np.sin(g.rr*0.015 + t*speed*0.7)*0.5) % 1.0
    return fn
```

---

## 坐标变换

在特效求值**之前**施加的 UV 空间变换。任何 `vf_*` 函数都可以通过变换它看到的网格坐标来旋转、缩放、平铺或扭曲。

### 变换辅助函数

```python
def uv_rotate(g, angle):
    """围绕网格中心旋转 UV 坐标。
    返回 (rotated_cc, rotated_rr) 数组 —— 用它替代 g.cc、g.rr。"""
    cx, cy = g.cols / 2.0, g.rows / 2.0
    cos_a, sin_a = np.cos(angle), np.sin(angle)
    dx = g.cc - cx
    dy = g.rr - cy
    return cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a

def uv_scale(g, sx=1.0, sy=1.0, cx_frac=0.5, cy_frac=0.5):
    """围绕中心点缩放 UV 坐标。
    sx, sy > 1 = 放大（重复更少），< 1 = 缩小（重复更多）。"""
    cx = g.cols * cx_frac; cy = g.rows * cy_frac
    return cx + (g.cc - cx) / sx, cy + (g.rr - cy) / sy

def uv_skew(g, kx=0.0, ky=0.0):
    """对 UV 坐标做剪切。kx 水平剪切，ky 垂直剪切。"""
    return g.cc + g.rr * kx, g.rr + g.cc * ky

def uv_tile(g, nx=3.0, ny=3.0, mirror=False):
    """平铺 UV 坐标。nx, ny = 重复次数。
    mirror=True：交替平铺翻转（无缝）。"""
    u = (g.cc / g.cols * nx) % 1.0
    v = (g.rr / g.rows * ny) % 1.0
    if mirror:
        flip_u = ((g.cc / g.cols * nx).astype(int) % 2) == 1
        flip_v = ((g.rr / g.rows * ny).astype(int) % 2) == 1
        u = np.where(flip_u, 1.0 - u, u)
        v = np.where(flip_v, 1.0 - v, v)
    return u * g.cols, v * g.rows

def uv_polar(g):
    """笛卡尔转极坐标 UV。返回 (angle_as_cc, dist_as_rr)。
    用来把任何线性效果变成径向效果。"""
    # 角度环绕 [0, cols)，距离环绕 [0, rows)
    return g.angle / (2 * np.pi) * g.cols, g.dist_n * g.rows

def uv_cartesian_from_polar(g):
    """把极坐标寻址的效果转回笛卡尔。
    把 g.cc 当作角度、g.rr 当作半径。"""
    angle = g.cc / g.cols * 2 * np.pi
    radius = g.rr / g.rows
    cx, cy = g.cols / 2.0, g.rows / 2.0
    return cx + radius * np.cos(angle) * cx, cy + radius * np.sin(angle) * cy

def uv_twist(g, amount=2.0):
    """扭转：旋转量随到中心距离增加。产生螺旋扭曲。"""
    twist_angle = g.dist_n * amount
    return uv_rotate_raw(g.cc, g.rr, g.cols / 2, g.rows / 2, twist_angle)

def uv_rotate_raw(cc, rr, cx, cy, angle):
    """对任意坐标数组做原始旋转。"""
    cos_a, sin_a = np.cos(angle), np.sin(angle)
    dx = cc - cx; dy = rr - cy
    return cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a

def uv_fisheye(g, strength=1.5):
    """UV 坐标上的鱼眼 / 桶形畸变。"""
    cx, cy = g.cols / 2.0, g.rows / 2.0
    dx = (g.cc - cx) / cx
    dy = (g.rr - cy) / cy
    r = np.sqrt(dx**2 + dy**2)
    r_distort = np.power(r, strength)
    scale = np.where(r > 0, r_distort / (r + 1e-10), 1.0)
    return cx + dx * scale * cx, cy + dy * scale * cy

def uv_wave(g, t, freq=0.1, amp=3.0, axis="x"):
    """正弦坐标位移。让 UV 空间波动。"""
    if axis == "x":
        return g.cc + np.sin(g.rr * freq + t * 3) * amp, g.rr
    else:
        return g.cc, g.rr + np.sin(g.cc * freq + t * 3) * amp

def uv_mobius(g, a=1.0, b=0.0, c=0.0, d=1.0):
    """莫比乌斯变换（共形映射）：f(z) = (az + b) / (cz + d)。
    在复平面上操作。产生数学上精确、视觉上
    引人注目的反演和圆形变换。"""
    cx, cy = g.cols / 2.0, g.rows / 2.0
    # 把网格映射到复平面 [-1, 1]
    zr = (g.cc - cx) / cx
    zi = (g.rr - cy) / cy
    # 复数除法：(a*z + b) / (c*z + d)
    num_r = a * zr - 0 * zi + b  # 实参数时 a,b,c,d 的虚部 = 0
    num_i = a * zi + 0 * zr + 0
    den_r = c * zr - 0 * zi + d
    den_i = c * zi + 0 * zr + 0
    denom = den_r**2 + den_i**2 + 1e-10
    wr = (num_r * den_r + num_i * den_i) / denom
    wi = (num_i * den_r - num_r * den_i) / denom
    return cx + wr * cx, cy + wi * cy
```

### 把变换与 Value 场配合使用

变换会修改 value 场看到的坐标。把变换包裹在 `vf_*` 调用外面：

```python
# 把等离子体场旋转 45 度
def vf_rotated_plasma(g, f, t, S):
    rc, rr = uv_rotate(g, np.pi / 4 + t * 0.1)
    class TG:  # 变换后的网格
        pass
    tg = TG(); tg.cc = rc; tg.rr = rr
    tg.rows = g.rows; tg.cols = g.cols
    tg.dist_n = g.dist_n; tg.angle = g.angle; tg.dist = g.dist
    return vf_plasma(tg, f, t, S)

# 把漩涡以镜像方式平铺成 3x3
def vf_tiled_vortex(g, f, t, S):
    tc, tr = uv_tile(g, 3, 3, mirror=True)
    class TG:
        pass
    tg = TG(); tg.cc = tc; tg.rr = tr
    tg.rows = g.rows; tg.cols = g.cols
    tg.dist = np.sqrt((tc - g.cols/2)**2 + (tr - g.rows/2)**2)
    tg.dist_n = tg.dist / (tg.dist.max() + 1e-10)
    tg.angle = np.arctan2(tr - g.rows/2, tc - g.cols/2)
    return vf_vortex(tg, f, t, S)

# 辅助函数：从坐标数组创建变换后的网格
def make_tgrid(g, new_cc, new_rr):
    """用变换后的坐标构建类网格对象。
    保留 rows/cols 用于尺寸计算，重新计算极坐标。"""
    class TG:
        pass
    tg = TG()
    tg.cc = new_cc; tg.rr = new_rr
    tg.rows = g.rows; tg.cols = g.cols
    cx, cy = g.cols / 2.0, g.rows / 2.0
    dx = new_cc - cx; dy = new_rr - cy
    tg.dist = np.sqrt(dx**2 + dy**2)
    tg.dist_n = tg.dist / (max(cx, cy) + 1e-10)
    tg.angle = np.arctan2(dy, dx)
    tg.dx = dx; tg.dy = dy
    tg.dx_n = dx / max(g.cols, 1)
    tg.dy_n = dy / max(g.rows, 1)
    return tg
```

---

## 时间连贯性

用于在时间上平滑、有意识地演化参数的工具。取代「要么静态参数、要么原始音频响应」的默认模式。

### 缓动函数

标准动画缓动曲线。所有函数接收 [0,1] 的 `t`，返回 [0,1]：

```python
def ease_linear(t): return t
def ease_in_quad(t): return t * t
def ease_out_quad(t): return t * (2 - t)
def ease_in_out_quad(t): return np.where(t < 0.5, 2*t*t, -1 + (4-2*t)*t)
def ease_in_cubic(t): return t**3
def ease_out_cubic(t): return (t - 1)**3 + 1
def ease_in_out_cubic(t):
    return np.where(t < 0.5, 4*t**3, 1 - (-2*t + 2)**3 / 2)
def ease_in_expo(t): return np.where(t == 0, 0, 2**(10*(t-1)))
def ease_out_expo(t): return np.where(t == 1, 1, 1 - 2**(-10*t))
def ease_elastic(t):
    """弹性缓出 —— 先过冲再回稳。"""
    return np.where(t == 0, 0, np.where(t == 1, 1,
        2**(-10*t) * np.sin((t*10 - 0.75) * (2*np.pi) / 3) + 1))
def ease_bounce(t):
    """弹跳缓出 —— 末端弹跳。"""
    t = np.asarray(t, dtype=np.float64)
    result = np.empty_like(t)
    m1 = t < 1/2.75
    m2 = (~m1) & (t < 2/2.75)
    m3 = (~m1) & (~m2) & (t < 2.5/2.75)
    m4 = ~(m1 | m2 | m3)
    result[m1] = 7.5625 * t[m1]**2
    t2 = t[m2] - 1.5/2.75;   result[m2] = 7.5625 * t2**2 + 0.75
    t3 = t[m3] - 2.25/2.75;  result[m3] = 7.5625 * t3**2 + 0.9375
    t4 = t[m4] - 2.625/2.75; result[m4] = 7.5625 * t4**2 + 0.984375
    return result
```

### 关键帧插值

在特定时刻定义参数值。在它们之间用缓动插值：

```python
def keyframe(t, points, ease_fn=ease_in_out_cubic, loop=False):
    """在关键帧值之间插值。

    参数：
        t：当前时间（浮点，秒）
        points：(time, value) 元组列表，按时间排序
        ease_fn：插值用的缓动函数
        loop：若为 True，最后一个关键帧后循环环绕

    返回：
        时刻 t 处的插值

    示例：
        twist = keyframe(t, [(0, 1.0), (5, 6.0), (10, 2.0)], ease_out_cubic)
    """
    if not points:
        return 0.0
    if loop:
        period = points[-1][0] - points[0][0]
        if period > 0:
            t = points[0][0] + (t - points[0][0]) % period

    # 钳制到范围
    if t <= points[0][0]:
        return points[0][1]
    if t >= points[-1][0]:
        return points[-1][1]

    # 找到包围的关键帧
    for i in range(len(points) - 1):
        t0, v0 = points[i]
        t1, v1 = points[i + 1]
        if t0 <= t <= t1:
            progress = (t - t0) / (t1 - t0)
            eased = ease_fn(progress)
            return v0 + (v1 - v0) * eased

    return points[-1][1]

def keyframe_array(t, points, ease_fn=ease_in_out_cubic):
    """支持 numpy 数组作为值的关键帧插值。
    points：(time, np.array) 元组列表。"""
    if t <= points[0][0]: return points[0][1].copy()
    if t >= points[-1][0]: return points[-1][1].copy()
    for i in range(len(points) - 1):
        t0, v0 = points[i]
        t1, v1 = points[i + 1]
        if t0 <= t <= t1:
            progress = ease_fn((t - t0) / (t1 - t0))
            return v0 * (1 - progress) + v1 * progress
    return points[-1][1].copy()
```

### Value 场变形

在两个不同的 value 场之间平滑过渡：

```python
def vf_morph(g, f, t, S, vf_a, vf_b, t_start, t_end,
             ease_fn=ease_in_out_cubic):
    """在一段时间范围内让两个 value 场互相变形。

    用法：
        val = vf_morph(g, f, t, S,
            lambda g,f,t,S: vf_plasma(g,f,t,S),
            lambda g,f,t,S: vf_vortex(g,f,t,S, twist=5),
            t_start=10.0, t_end=15.0)
    """
    if t <= t_start:
        return vf_a(g, f, t, S)
    if t >= t_end:
        return vf_b(g, f, t, S)
    progress = ease_fn((t - t_start) / (t_end - t_start))
    a = vf_a(g, f, t, S)
    b = vf_b(g, f, t, S)
    return a * (1 - progress) + b * progress

def vf_sequence(g, f, t, S, fields, durations, crossfade=1.0,
                ease_fn=ease_in_out_cubic):
    """带交叉淡入淡出地循环播放一组 value 场序列。

    fields：vf_* 可调用对象列表
    durations：每个场的浮点秒数列表
    crossfade：相邻场之间重叠的秒数
    """
    total = sum(durations)
    t_local = t % total  # 循环
    elapsed = 0
    for i, dur in enumerate(durations):
        if t_local < elapsed + dur:
            # 当前场
            base = fields[i](g, f, t, S)
            # 检查是否处于交叉淡入淡出区
            time_in = t_local - elapsed
            time_left = dur - time_in
            if time_in < crossfade and i > 0:
                # 从前一个场淡入
                prev = fields[(i - 1) % len(fields)](g, f, t, S)
                blend = ease_fn(time_in / crossfade)
                return prev * (1 - blend) + base * blend
            if time_left < crossfade and i < len(fields) - 1:
                # 向下一个场淡出
                nxt = fields[(i + 1) % len(fields)](g, f, t, S)
                blend = ease_fn(1 - time_left / crossfade)
                return base * (1 - blend) + nxt * blend
            return base
        elapsed += dur
    return fields[-1](g, f, t, S)
```

### 时间噪点

在 `(x, y, t)` 处采样的 3D 噪点 —— 图案随时间平滑演化，没有逐帧不连续：

```python
def vf_temporal_noise(g, f, t, S, freq=0.06, t_freq=0.3, octaves=4,
                      bri=0.8):
    """随时间平滑演化的噪点场。通过两次 2D 噪点查找
    加时间插值来实现 3D 噪点。

    与 vf_fbm（滚动噪点，产生方向性运动）不同，
    这个是原地变形图案 —— 单元格变亮变暗，
    而场不会朝任何方向移动。"""
    # 在时间坐标的 floor/ceil 处采样两次噪点
    t_scaled = t * t_freq
    t_lo = np.floor(t_scaled)
    t_frac = _smootherstep(np.full((g.rows, g.cols), t_scaled - t_lo, dtype=np.float32))

    val_lo = np.zeros((g.rows, g.cols), dtype=np.float32)
    val_hi = np.zeros((g.rows, g.cols), dtype=np.float32)
    amp = 1.0; fx = freq
    for i in range(octaves):
        val_lo = val_lo + _value_noise_2d(
            g.cc * fx + t_lo * 7.3 + i * 13, g.rr * fx + t_lo * 3.1 + i * 29) * amp
        val_hi = val_hi + _value_noise_2d(
            g.cc * fx + (t_lo + 1) * 7.3 + i * 13, g.rr * fx + (t_lo + 1) * 3.1 + i * 29) * amp
        amp *= 0.5; fx *= 2.0
    max_amp = (1 - 0.5 ** octaves) / 0.5
    val = (val_lo * (1 - t_frac) + val_hi * t_frac) / max_amp
    return np.clip(val * bri * (0.6 + f.get("rms", 0.3) * 0.6), 0, 1)
```

---

### 组合 Value 场

组合爆炸来自用数学混合 value 场：

```python
# 乘法 = 交集（只显示两者都亮的地方）
combined = vf_plasma(g,f,t,S) * vf_vortex(g,f,t,S)

# 加法 = 并集（两者都显示，裁剪到 1.0）
combined = np.clip(vf_rings(g,f,t,S) + vf_spiral(g,f,t,S), 0, 1)

# 干涉 = 拍频图案（显示类 XOR 图案）
combined = np.abs(vf_plasma(g,f,t,S) - vf_tunnel(g,f,t,S))

# 调制 = 一个效果塑造另一个
combined = vf_rings(g,f,t,S) * (0.3 + 0.7 * vf_plasma(g,f,t,S))

# 取最大 = 显示两个效果中最亮的
combined = np.maximum(vf_spiral(g,f,t,S), vf_aurora(g,f,t,S))
```

### 完整场景示例（v2 —— 返回画布）

v2 场景函数在内部组合特效并返回一个像素画布：

```python
def scene_complex(r, f, t, S):
    """v2 场景函数：返回画布（uint8 H,W,3）。
    r = Renderer，f = 音频特征，t = 时间，S = 持久化状态 dict。"""
    g = r.grids["md"]
    rows, cols = g.rows, g.cols
    
    # 1. Value 场组合
    plasma = vf_plasma(g, f, t, S)
    vortex = vf_vortex(g, f, t, S, twist=4.0)
    combined = np.clip(plasma * 0.6 + vortex * 0.5 + plasma * vortex * 0.4, 0, 1)
    
    # 2. 从 hue 场取色
    h = (hf_angle(0.3)(g,f,t,S) * 0.5 + hf_time_cycle(0.08)(g,f,t,S) * 0.5) % 1.0
    
    # 3. 通过 _render_vf 辅助函数渲染到画布
    canvas = _render_vf(g, combined, h, sat=0.75, pal=PAL_DENSE)
    
    # 4. 可选：混合第二层
    overlay = _render_vf(r.grids["sm"], vf_rings(r.grids["sm"],f,t,S),
                         hf_fixed(0.6)(r.grids["sm"],f,t,S), pal=PAL_BLOCK)
    canvas = blend_canvas(canvas, overlay, "screen", 0.4)
    
    return canvas
    
# 在 render_clip() 循环中（由框架处理）：
# canvas = scene_fn(r, f, t, S)
# canvas = tonemap(canvas, gamma=scene_gamma)
# canvas = feedback.apply(canvas, ...)
# canvas = shader_chain.apply(canvas, f=f, t=t)
# pipe.stdin.write(canvas.tobytes())
```

每个段落变换 **value 场组合**、**hue 场**、**调色板**、**混合模式**、**反馈配置**、**着色器链**，以最大化视觉多样性。有了 12 个 value 场 × 8 个 hue 场 × 14 个调色板 × 20 种混合模式 × 7 种反馈变换 × 38 个着色器，组合数实际上是无限的。

---

## 组合特效 —— 创意指南

上面的目录是词汇表。下面讲如何把它组合得看起来有意图。

### 分层营造深度
每个场景在不同网格密度上至少要有两层：
- **背景**（sm 或 xs）：密集、暗淡的纹理，防止纯黑。低亮度（bri=0.15-0.25）的 fBM、平滑噪点或域扭曲。
- **内容**（md）：主要视觉 —— 圆环、voronoi、螺旋、隧道。全亮度。
- **点缀**（lg 或 xl）：稀疏高光 —— 粒子、文字镂空、辉光脉冲。用 screen 模式叠加在最上层。

### 有趣的特效组合
| 组合 | 混合模式 | 为何有效 |
|------|-------|-------------|
| fBM + voronoi 边缘 | `screen` | 有机物填满单元，边缘增加结构 |
| 域扭曲 + 等离子体 | `difference` | 迷幻的有机干涉 |
| 隧道 + 漩涡 | `screen` | 纵深透视 + 旋转能量 |
| 螺旋 + 干涉 | `exclusion` | 不同空间频率的莫尔图案 |
| 反应-扩散 + 火焰 | `add` | 活的有机底座 + 动态前景 |
| SDF 几何 + 域扭曲 | `screen` | 干净形状漂浮在有机纹理中 |

### 把特效当作遮罩
任意 value 场都可以通过 `mask_from_vf()` 当作另一个效果的遮罩：
- Voronoi 单元遮罩火焰（火焰只在单元内可见）
- fBM 遮罩纯色层（有机色云）
- SDF 形状遮罩反应-扩散场
- 动画光圈/擦除，让一个效果在另一个之上逐渐显现

### 发明新特效
每个项目都应创造至少一个目录里没有的效果：
- **用数学组合两个 vf_* 函数**：`np.clip(vf_fbm(...) * vf_rings(...), 0, 1)`
- **在求值前施加坐标变换**：`vf_plasma(twisted_grid, ...)`
- **用一个场调制另一个的参数**：`vf_spiral(..., tightness=2 + vf_fbm(...) * 5)`
- **堆叠时间偏移**：在 `t` 和 `t - 0.5` 处渲染同一个场，用 difference 混合得到运动拖尾
- **通过 SDF 边界镜像 value 场**，得到万花筒几何
