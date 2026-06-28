# 输入源

> **另请参阅：** architecture.md · effects.md · scenes.md · shaders.md · optimization.md · troubleshooting.md

## 音频分析

### 加载

```python
tmp = tempfile.mktemp(suffix=".wav")
subprocess.run(["ffmpeg", "-y", "-i", input_path, "-ac", "1", "-ar", "22050",
                "-sample_fmt", "s16", tmp], capture_output=True, check=True)
with wave.open(tmp) as wf:
    sr = wf.getframerate()
    raw = wf.readframes(wf.getnframes())
samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
```

### 逐帧 FFT

```python
hop = sr // fps          # 每帧采样数
win = hop * 2            # 分析窗口（2 倍 hop 用于重叠）
window = np.hanning(win)
freqs = rfftfreq(win, 1.0 / sr)

bands = {
    "sub":   (freqs >= 20)  & (freqs < 80),
    "bass":  (freqs >= 80)  & (freqs < 250),
    "lomid": (freqs >= 250) & (freqs < 500),
    "mid":   (freqs >= 500) & (freqs < 2000),
    "himid": (freqs >= 2000)& (freqs < 6000),
    "hi":    (freqs >= 6000),
}
```

对每一帧：提取数据块，应用窗口，FFT，计算频带能量。

### 特征集

| 特征 | 公式 | 控制 |
|---------|---------|----------|
| `rms` | `sqrt(mean(chunk²))` | 整体响度/能量 |
| `sub`..`hi` | `sqrt(mean(band_magnitudes²))` | 各频带能量 |
| `centroid` | `sum(freq*mag) / sum(mag)` | 明亮度/音色 |
| `flatness` | `geomean(mag) / mean(mag)` | 噪声 vs 纯音 |
| `flux` | `sum(max(0, mag - prev_mag))` | 瞬态强度 |
| `sub_r`..`hi_r` | `band / sum(all_bands)` | 频谱形状（与音量无关） |
| `cent_d` | `abs(gradient(centroid))` | 音色变化率 |
| `beat` | Flux 峰值检测 | 二值节拍 onset |
| `bdecay` | 从节拍开始的指数衰减 | 平滑节拍脉冲 (0→1→0) |

**频带比例至关重要** —— 它们将频谱形状与音量解耦，因此安静的低音段和响亮的低音段都被解读为"低音重"，而不仅仅是"安静"与"响亮"的区别。

### 平滑

EMA 防止视觉抖动：

```python
def ema(arr, alpha):
    out = np.empty_like(arr); out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i-1]
    return out

# 慢变化特征 (alpha=0.12): centroid, flatness, 频带比例, cent_d
# 快变化特征 (alpha=0.3): rms, flux, 原始频带
```

### 节拍检测

```python
flux_smooth = np.convolve(flux, np.ones(5)/5, mode="same")
peaks, _ = signal.find_peaks(flux_smooth, height=0.15, distance=fps//5, prominence=0.05)

beat = np.zeros(n_frames)
bdecay = np.zeros(n_frames, dtype=np.float32)
for p in peaks:
    beat[p] = 1.0
    for d in range(fps // 2):
        if p + d < n_frames:
            bdecay[p + d] = max(bdecay[p + d], math.exp(-d * 2.5 / (fps // 2)))
```

`bdecay` 为每个节拍给出平滑的 0→1→0 脉冲，在约 0.5 秒内衰减。用于闪光/故障/镜像触发。

### 归一化

计算所有帧后，将每个特征归一化到 0-1：

```python
for k in features:
    a = features[k]
    lo, hi = a.min(), a.max()
    features[k] = (a - lo) / (hi - lo + 1e-10)
```

## 视频采样

### 帧提取

```python
# 方法 1：ffmpeg 管道（内存高效）
cmd = ["ffmpeg", "-i", input_video, "-f", "rawvideo", "-pix_fmt", "rgb24",
       "-s", f"{target_w}x{target_h}", "-r", str(fps), "-"]
pipe = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
frame_size = target_w * target_h * 3
for fi in range(n_frames):
    raw = pipe.stdout.read(frame_size)
    if len(raw) < frame_size: break
    frame = np.frombuffer(raw, dtype=np.uint8).reshape(target_h, target_w, 3)
    # 处理画布帧...

# 方法 2：OpenCV（如果可用）
cap = cv2.VideoCapture(input_video)
```

### 亮度到字符的映射

基于亮度将视频像素转换为 ASCII 字符：

```python
def frame_to_ascii(frame_rgb, grid, pal=PAL_DEFAULT):
    """将视频画布帧转换为字符 + 颜色数组。"""
    rows, cols = grid.rows, grid.cols
    # 将画布帧调整为网格尺寸
    small = np.array(Image.fromarray(frame_rgb).resize((cols, rows), Image.LANCZOS))
    # 亮度
    lum = (0.299 * small[:,:,0] + 0.587 * small[:,:,1] + 0.114 * small[:,:,2]) / 255.0
    # 映射到字符
    chars = val2char(lum, lum > 0.02, pal)
    # 颜色：使用源像素颜色，按亮度缩放以提高可见度
    colors = np.clip(small * np.clip(lum[:,:,None] * 1.5 + 0.3, 0.3, 1), 0, 255).astype(np.uint8)
    return chars, colors
```

### 边缘加权字符映射

使用边缘检测在轮廓区域获得更多细节：

```python
def frame_to_ascii_edges(frame_rgb, grid, pal=PAL_DEFAULT, edge_pal=PAL_BOX):
    gray = np.mean(frame_rgb, axis=2)
    small_gray = resize(gray, (grid.rows, grid.cols))
    lum = small_gray / 255.0

    # Sobel 边缘检测
    gx = np.abs(small_gray[:, 2:] - small_gray[:, :-2])
    gy = np.abs(small_gray[2:, :] - small_gray[:-2, :])
    edge = np.zeros_like(small_gray)
    edge[:, 1:-1] += gx; edge[1:-1, :] += gy
    edge = np.clip(edge / edge.max(), 0, 1)

    # 边缘区域使用制表字符，平坦区域使用亮度字符
    is_edge = edge > 0.15
    chars = val2char(lum, lum > 0.02, pal)
    edge_chars = val2char(edge, is_edge, edge_pal)
    chars[is_edge] = edge_chars[is_edge]

    return chars, colors
```

### 运动检测

检测画布帧之间的像素变化，用于运动响应效果：

```python
prev_frame = None
def compute_motion(frame):
    global prev_frame
    if prev_frame is None:
        prev_frame = frame.astype(np.float32)
        return np.zeros(frame.shape[:2])
    diff = np.abs(frame.astype(np.float32) - prev_frame).mean(axis=2)
    prev_frame = frame.astype(np.float32) * 0.7 + prev_frame * 0.3  # 平滑
    return np.clip(diff / 30.0, 0, 1)  # 归一化的运动图
```

使用运动图驱动粒子发射、故障强度或字符密度。

### 视频特征提取

逐帧特征类似于音频特征，用于驱动效果：

```python
def analyze_video_frame(frame_rgb):
    gray = np.mean(frame_rgb, axis=2)
    return {
        "brightness": gray.mean() / 255.0,
        "contrast": gray.std() / 128.0,
        "edge_density": compute_edge_density(gray),
        "motion": compute_motion(frame_rgb).mean(),
        "dominant_hue": compute_dominant_hue(frame_rgb),
        "color_variance": compute_color_variance(frame_rgb),
    }
```

## 图像序列

### 静态图像转 ASCII

与单个视频画布帧转换相同。对于动画序列：

```python
import glob
frames = sorted(glob.glob("frames/*.png"))
for fi, path in enumerate(frames):
    img = np.array(Image.open(path).resize((VW, VH)))
    chars, colors = frame_to_ascii(img, grid, pal)
```

### 图像作为纹理源

将图像用作效果调制的背景纹理：

```python
def load_texture(path, grid):
    img = np.array(Image.open(path).resize((grid.cols, grid.rows)))
    lum = np.mean(img, axis=2) / 255.0
    return lum, img  # 亮度用于字符映射，RGB 用于颜色
```

## 文本 / 歌词

### SRT 解析

```python
import re
def parse_srt(path):
    """返回 [(start_sec, end_sec, text), ...]"""
    entries = []
    with open(path) as f:
        content = f.read()
    blocks = content.strip().split("\n\n")
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) >= 3:
            times = lines[1]
            m = re.match(r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)", times)
            if m:
                g = [int(x) for x in m.groups()]
                start = g[0]*3600 + g[1]*60 + g[2] + g[3]/1000
                end = g[4]*3600 + g[5]*60 + g[6] + g[7]/1000
                text = " ".join(lines[2:])
                entries.append((start, end, text))
    return entries
```

### 歌词显示模式

- **打字机**：字符在时间窗口内从左到右出现
- **淡入**：整行从暗到亮淡入
- **闪光**：节拍时瞬间出现，然后淡出
- **散射**：字符从随机位置开始，汇聚到最终位置
- **波浪**：文本沿正弦波路径排列

```python
def lyrics_typewriter(ch, co, text, row, col, t, t_start, t_end, color):
    """在时间窗口内逐步显示字符。"""
    progress = np.clip((t - t_start) / (t_end - t_start), 0, 1)
    n_visible = int(len(text) * progress)
    stamp(ch, co, text[:n_visible], row, col, color)
```

## 生成式（无输入）

对于纯生成式 ASCII 艺术，"特征"字典由时间合成：

```python
def synthetic_features(t, bpm=120):
    """仅从时间生成类音频特征。"""
    beat_period = 60.0 / bpm
    beat_phase = (t % beat_period) / beat_period
    return {
        "rms": 0.5 + 0.3 * math.sin(t * 0.5),
        "bass": 0.5 + 0.4 * math.sin(t * 2 * math.pi / beat_period),
        "sub": 0.3 + 0.3 * math.sin(t * 0.8),
        "mid": 0.4 + 0.3 * math.sin(t * 1.3),
        "hi": 0.3 + 0.2 * math.sin(t * 2.1),
        "cent": 0.5 + 0.2 * math.sin(t * 0.3),
        "flat": 0.4,
        "flux": 0.3 + 0.2 * math.sin(t * 3),
        "beat": 1.0 if beat_phase < 0.05 else 0.0,
        "bdecay": max(0, 1.0 - beat_phase * 4),
        # 比例
        "sub_r": 0.2, "bass_r": 0.25, "lomid_r": 0.15,
        "mid_r": 0.2, "himid_r": 0.12, "hi_r": 0.08,
        "cent_d": 0.1,
    }
```

## TTS 集成

对于带旁白的视频（证言、引语、讲故事），按片段生成语音音频并与背景音乐混合。

### ElevenLabs 语音生成

```python
import requests, time, os

def generate_tts(text, voice_id, api_key, output_path, model="eleven_multilingual_v2"):
    """通过 ElevenLabs API 生成 TTS 音频。将响应流式写入磁盘。"""
    # 如果已生成则跳过（幂等重跑）
    if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
        return

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    data = {
        "text": text,
        "model_id": model,
        "voice_settings": {
            "stability": 0.65,
            "similarity_boost": 0.80,
            "style": 0.15,
            "use_speaker_boost": True,
        },
    }
    resp = requests.post(url, json=data, headers=headers, stream=True)
    resp.raise_for_status()
    with open(output_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=4096):
            f.write(chunk)
    time.sleep(0.3)  # 限流：避免批量生成时的 429
```

语音设置说明：
- `stability` 0.65 给出自然变化而不漂移。更低 (0.3-0.5) 更具表现力，更高 (0.7-0.9) 更单调/适合旁白。
- `similarity_boost` 0.80 使其贴近声音档案。更低则声音更通用。
- `style` 0.15 增加轻微的风格变化。简单朗读时保持低位 (0-0.2)。
- `use_speaker_boost` True 以略多处理时间为代价提升清晰度。

### 声音池

ElevenLabs 有约 20 种内置声音。跨引语使用多种声音增加多样性。参考池：

```python
VOICE_POOL = [
    ("JBFqnCBsd6RMkjVDRZzb", "George"),
    ("nPczCjzI2devNBz1zQrb", "Brian"),
    ("pqHfZKP75CvOlQylNhV4", "Bill"),
    ("CwhRBWXzGAHq8TQ4Fs17", "Roger"),
    ("cjVigY5qzO86Huf0OWal", "Eric"),
    ("onwK4e9ZLuTAKqWW03F9", "Daniel"),
    ("IKne3meq5aSn9XLyUdCD", "Charlie"),
    ("iP95p4xoKVk53GoZ742B", "Chris"),
    ("bIHbv24MWmeRgasZH58o", "Will"),
    ("TX3LPaxmHKxFdv7VOQHJ", "Liam"),
    ("SAz9YHcvj6GT2YYXdXww", "River"),
    ("EXAVITQu4vr4xnSDxMaL", "Sarah"),
    ("Xb7hH8MSUJpSbSDYk0k2", "Alice"),
    ("pFZP5JQG7iQjIQuC4Bku", "Lily"),
    ("XrExE9yKIg1WjnnlVkGX", "Matilda"),
    ("FGY2WhTYpPnrIDTdsKH5", "Laura"),
    ("SOYHLrjzK2X1ezoPC6cr", "Harry"),
    ("hpp4J3VqNfWAUOO0d1Us", "Bella"),
    ("N2lVS1w4EtoT3dr4eOWO", "Callum"),
    ("cgSgspJ2msm6clMCkdW9", "Jessica"),
    ("pNInz6obpgDQGcFmaJgB", "Adam"),
]
```

### 声音分配

确定性洗牌，使重跑产生相同的声音映射：

```python
import random as _rng

def assign_voices(n_quotes, voice_pool, seed=42):
    """为每条引语分配不同声音，需要时循环。"""
    r = _rng.Random(seed)
    ids = [v[0] for v in voice_pool]
    r.shuffle(ids)
    return [ids[i % len(ids)] for i in range(n_quotes)]
```

### 发音控制

TTS 文本必须与显示文本分开。显示文本有用于视觉布局的换行；TTS 文本是带注音修正的平铺句子。

常见修正：
- 品牌名：按发音拼写（"Nous" -> "Noose"，"nginx" -> "engine-x"）
- 缩写：展开（"API" -> "A P I"，"CLI" -> "C L I"）
- 技术术语：添加发音提示
- 用于节奏的标点：句号产生停顿，逗号产生轻微停顿

```python
# 显示文本：换行控制视觉布局
QUOTES = [
    ("It can do far more than the Claws,\nand you don't need to buy a Mac Mini.\nNous Research has a winner here.", "Brian Roemmele"),
]

# TTS 文本：平铺，按发音修正用于朗读
QUOTES_TTS = [
    "It can do far more than the Claws, and you don't need to buy a Mac Mini. Noose Research has a winner here.",
]
# 保持两个数组同步 —— 索引相同
```

### 音频流水线

1. 生成各个 TTS 片段（每条引语一个 MP3，跳过已存在的）
2. 将每个转换为 WAV（单声道，22050 Hz）用于时长测量和拼接
3. 计算时间：前奏填充 + 语音 + 间隙 + 尾奏填充 = 目标时长
4. 拼接成单一 TTS 轨道，带静音填充
5. 与背景音乐混合

```python
def build_tts_track(tts_clips, target_duration, intro_pad=5.0, outro_pad=4.0):
    """按计算的间隙拼接 TTS 片段，填充到目标时长。

    Returns:
        timing: (start_time, end_time, quote_index) 元组列表
    """
    sr = 22050

    # 将 MP3 转为 WAV 用于时长和采样级拼接
    durations = []
    for clip in tts_clips:
        wav = clip.replace(".mp3", ".wav")
        subprocess.run(
            ["ffmpeg", "-y", "-i", clip, "-ac", "1", "-ar", str(sr),
             "-sample_fmt", "s16", wav],
            capture_output=True, check=True)
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", wav],
            capture_output=True, text=True)
        durations.append(float(result.stdout.strip()))

    # 计算填满目标时长的间隙
    total_speech = sum(durations)
    n_gaps = len(tts_clips) - 1
    remaining = target_duration - total_speech - intro_pad - outro_pad
    gap = max(1.0, remaining / max(1, n_gaps))

    # 构建时间并拼接采样
    timing = []
    t = intro_pad
    all_audio = [np.zeros(int(sr * intro_pad), dtype=np.int16)]

    for i, dur in enumerate(durations):
        wav = tts_clips[i].replace(".mp3", ".wav")
        with wave.open(wav) as wf:
            samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
        timing.append((t, t + dur, i))
        all_audio.append(samples)
        t += dur
        if i < len(tts_clips) - 1:
            all_audio.append(np.zeros(int(sr * gap), dtype=np.int16))
            t += gap

    all_audio.append(np.zeros(int(sr * outro_pad), dtype=np.int16))

    # 填充或裁剪到精确的 target_duration
    full = np.concatenate(all_audio)
    target_samples = int(sr * target_duration)
    if len(full) < target_samples:
        full = np.pad(full, (0, target_samples - len(full)))
    else:
        full = full[:target_samples]

    # 写入拼接的 TTS 轨道
    with wave.open("tts_full.wav", "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(full.tobytes())

    return timing
```

### 音频混合

将 TTS（居中）与背景音乐（宽立体声、低音量）混合。滤镜链：
1. TTS 单声道复制到两个声道（居中）
2. BGM 响度归一化，音量降至 15%，用 `extrastereo` 拓宽立体声
3. 混合在一起，结尾带 dropout 过渡以获得平滑收尾

```python
def mix_audio(tts_path, bgm_path, output_path, bgm_volume=0.15):
    """将居中的 TTS 与左右铺开的立体声 BGM 混合。"""
    filter_complex = (
        # TTS：单声道 -> 立体声居中
        "[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=mono,"
        "pan=stereo|c0=c0|c1=c0[tts];"
        # BGM：归一化响度，降低音量，拓宽立体声
        f"[1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
        f"loudnorm=I=-16:TP=-1.5:LRA=11,"
        f"volume={bgm_volume},"
        f"extrastereo=m=2.5[bgm];"
        # 混合并结尾平滑 dropout
        "[tts][bgm]amix=inputs=2:duration=longest:dropout_transition=3,"
        "aformat=sample_fmts=s16:sample_rates=44100:channel_layouts=stereo[out]"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", tts_path,
        "-i", bgm_path,
        "-filter_complex", filter_complex,
        "-map", "[out]", output_path,
    ]
    subprocess.run(cmd, capture_output=True, check=True)
```

### 逐引语视觉风格

逐引语循环视觉预设以增加多样性。每个预设定义一个背景效果、配色方案和文本颜色：

```python
QUOTE_STYLES = [
    {"hue": 0.08, "accent": 0.7, "bg": "spiral",       "text_rgb": (255, 220, 140)},  # 暖金色
    {"hue": 0.55, "accent": 0.6, "bg": "rings",         "text_rgb": (180, 220, 255)},  # 冷蓝
    {"hue": 0.75, "accent": 0.7, "bg": "wave",          "text_rgb": (220, 180, 255)},  # 紫色
    {"hue": 0.35, "accent": 0.6, "bg": "matrix",        "text_rgb": (140, 255, 180)},  # 绿色
    {"hue": 0.95, "accent": 0.8, "bg": "fire",          "text_rgb": (255, 180, 160)},  # 红/珊瑚
    {"hue": 0.12, "accent": 0.5, "bg": "interference",  "text_rgb": (255, 240, 200)},  # 琥珀
    {"hue": 0.60, "accent": 0.7, "bg": "tunnel",        "text_rgb": (160, 210, 255)},  # 青色
    {"hue": 0.45, "accent": 0.6, "bg": "aurora",        "text_rgb": (180, 255, 220)},  # 蓝绿
]

style = QUOTE_STYLES[quote_index % len(QUOTE_STYLES)]
```

这保证没有两个相邻引语共享相同外观，即使没有随机性。

### 打字机文本渲染

将引语文本逐字符显示，与语音进度同步。最近显示的字符更亮，营造"刚打出"的发光感：

```python
def render_typewriter(ch, co, lines, block_start, cols, progress, total_chars, text_rgb, t):
    """将打字机文本叠加到字符/颜色网格上。
    progress：0.0（无可见）到 1.0（全部文本可见）。"""
    chars_visible = int(total_chars * min(1.0, progress * 1.2))  # 轻微超调以获得干脆感
    tr, tg, tb = text_rgb
    char_count = 0
    for li, line in enumerate(lines):
        row = block_start + li
        col = (cols - len(line)) // 2
        for ci, c in enumerate(line):
            if char_count < chars_visible:
                age = chars_visible - char_count
                bri_factor = min(1.0, 0.5 + 0.5 / (1 + age * 0.015))  # 越新 = 越亮
                hue_shift = math.sin(char_count * 0.3 + t * 2) * 0.05
                stamp(ch, co, c, row, col + ci,
                      (int(min(255, tr * bri_factor * (1.0 + hue_shift))),
                       int(min(255, tg * bri_factor)),
                       int(min(255, tb * bri_factor * (1.0 - hue_shift)))))
            char_count += 1

    # 插入点处的闪烁光标
    if progress < 1.0 and int(t * 3) % 2 == 0:
        # 寻找光标位置 (char_count == chars_visible)
        cc = 0
        for li, line in enumerate(lines):
            for ci, c in enumerate(line):
                if cc == chars_visible:
                    stamp(ch, co, "▌", block_start + li,
                          (cols - len(line)) // 2 + ci, (255, 220, 100))
                    return
                cc += 1
```

### 对混合音频的特征分析

对最终混合轨道运行标准音频分析（FFT、节拍检测），使视觉效果同时对 TTS 和音乐做出反应：

```python
# 分析 mixed_final.wav（而非各个轨道）
features = analyze_audio("mixed_final.wav", fps=24)
```

视觉随音乐节拍和语音能量共同脉动。

---

## 音视频同步验证

渲染后，验证视觉节拍标记与实际音频节拍对齐。漂移累积自画布帧计时误差、ffmpeg 拼接边界以及 `fi / fps` 中的舍入。

### 节拍时间戳提取

```python
def extract_beat_timestamps(features, fps, threshold=0.5):
    """提取节拍特征超过阈值的时间戳。"""
    beat = features["beat"]
    timestamps = []
    for fi in range(len(beat)):
        if beat[fi] > threshold:
            timestamps.append(fi / fps)
    return timestamps

def extract_visual_beat_timestamps(video_path, fps, brightness_jump=30):
    """通过连续画布帧之间的亮度跳变检测视觉节拍。
    返回平均亮度增加超过阈值的画布帧时间戳。"""
    import subprocess
    cmd = ["ffmpeg", "-i", video_path, "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    frames = np.frombuffer(proc.stdout, dtype=np.uint8)
    # 从总字节数推断画布帧尺寸
    n_pixels = len(frames)
    # 对于 1080p：每画布帧 1920*1080 像素
    # 从视频元数据自动检测更稳健：
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "csv=p=0", video_path],
        capture_output=True, text=True)
    w, h = map(int, probe.stdout.strip().split(","))
    ppf = w * h  # 每画布帧像素数
    n_frames = n_pixels // ppf
    frames = frames[:n_frames * ppf].reshape(n_frames, ppf)
    means = frames.mean(axis=1)

    timestamps = []
    for i in range(1, len(means)):
        if means[i] - means[i-1] > brightness_jump:
            timestamps.append(i / fps)
    return timestamps
```

### 同步报告

```python
def sync_report(audio_beats, visual_beats, tolerance_ms=50):
    """将音频节拍时间戳与视觉节拍时间戳进行对比。

    Args:
        audio_beats: 来自音频分析的时间戳（秒）列表
        visual_beats: 来自视频亮度分析的时间戳（秒）列表
        tolerance_ms: 可接受的最大漂移（毫秒）

    Returns:
        含 matched/unmatched/drift 统计的字典
    """
    tolerance = tolerance_ms / 1000.0
    matched = []
    unmatched_audio = []
    unmatched_visual = list(visual_beats)

    for at in audio_beats:
        best_match = None
        best_delta = float("inf")
        for vt in unmatched_visual:
            delta = abs(at - vt)
            if delta < best_delta:
                best_delta = delta
                best_match = vt
        if best_match is not None and best_delta < tolerance:
            matched.append({"audio": at, "visual": best_match, "drift_ms": best_delta * 1000})
            unmatched_visual.remove(best_match)
        else:
            unmatched_audio.append(at)

    drifts = [m["drift_ms"] for m in matched]
    return {
        "matched": len(matched),
        "unmatched_audio": len(unmatched_audio),
        "unmatched_visual": len(unmatched_visual),
        "total_audio_beats": len(audio_beats),
        "total_visual_beats": len(visual_beats),
        "mean_drift_ms": np.mean(drifts) if drifts else 0,
        "max_drift_ms": np.max(drifts) if drifts else 0,
        "p95_drift_ms": np.percentile(drifts, 95) if len(drifts) > 1 else 0,
    }

# 用法：
audio_beats = extract_beat_timestamps(features, fps=24)
visual_beats = extract_visual_beat_timestamps("output.mp4", fps=24)
report = sync_report(audio_beats, visual_beats)
print(f"匹配：{report['matched']}/{report['total_audio_beats']} 个节拍")
print(f"平均漂移：{report['mean_drift_ms']:.1f}ms，最大：{report['max_drift_ms']:.1f}ms")
# 目标：平均漂移 < 20ms，最大漂移 < 42ms（24fps 下 1 帧）
```

### 常见同步问题

| 症状 | 原因 | 修复 |
|---------|-------|-----|
| 视觉节拍持续偏晚 | ffmpeg concat 在边界添加画布帧 | 使用 `-vsync cfr` 标志；将分段填充到精确画布帧数 |
| 漂移随时间增大 | `t = fi / fps` 中的浮点累积 | 使用整数画布帧计数器，每画布帧重新计算 `t` |
| 随机漏拍 | 节拍阈值太高 / 特征平滑过度 | 降低阈值；减小节拍特征的 EMA alpha |
| 节拍落在错误画布帧 | 画布帧索引差一 | 验证：画布帧 0 = t=0，画布帧 1 = t=1/fps（而非 t=0） |
