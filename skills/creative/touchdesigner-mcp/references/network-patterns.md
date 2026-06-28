# TouchDesigner 网络模式

针对常见创意编码任务的完整网络配方。每个模式展示了算子链、构建它的 MCP 工具调用，以及关键参数设置。

## 音频响应式视觉

### 模式 1：音频频谱 -> 噪声位移

音频驱动噪声参数，产生有机的、随音乐响应的纹理。

```
Audio File In CHOP -> Audio Spectrum CHOP -> Math CHOP (scale)
                                                |
                                                v (export to noise params)
                          Noise TOP -> Level TOP -> Feedback TOP -> Composite TOP -> Null TOP (out)
                                                        ^                |
                                                        |________________|
```

**MCP 构建序列：**

```
1. td_create_operator(parent="/project1", type="audiofileinChop", name="audio_in")
2. td_create_operator(parent="/project1", type="audiospectrumChop", name="spectrum")
3. td_create_operator(parent="/project1", type="mathChop", name="spectrum_scale")
4. td_create_operator(parent="/project1", type="noiseTop", name="noise1")
5. td_create_operator(parent="/project1", type="levelTop", name="level1")
6. td_create_operator(parent="/project1", type="feedbackTop", name="feedback1")
7. td_create_operator(parent="/project1", type="compositeTop", name="comp1")
8. td_create_operator(parent="/project1", type="nullTop", name="out")

9. td_set_operator_pars(path="/project1/audio_in",
     properties={"file": "/path/to/music.wav", "play": true})
10. td_set_operator_pars(path="/project1/spectrum",
     properties={"size": 512})
11. td_set_operator_pars(path="/project1/spectrum_scale",
     properties={"gain": 2.0, "postoff": 0.0})
12. td_set_operator_pars(path="/project1/noise1",
     properties={"type": 1, "monochrome": false, "resolutionw": 1280, "resolutionh": 720,
                  "period": 4.0, "harmonics": 3, "amp": 1.0})
13. td_set_operator_pars(path="/project1/level1",
     properties={"opacity": 0.95, "gamma1": 0.75})
14. td_set_operator_pars(path="/project1/feedback1",
     properties={"top": "/project1/comp1"})
15. td_set_operator_pars(path="/project1/comp1",
     properties={"operand": 0})

16. td_execute_python: """
op('/project1/audio_in').outputConnectors[0].connect(op('/project1/spectrum'))
op('/project1/spectrum').outputConnectors[0].connect(op('/project1/spectrum_scale'))
op('/project1/noise1').outputConnectors[0].connect(op('/project1/level1'))
op('/project1/level1').outputConnectors[0].connect(op('/project1/comp1').inputConnectors[0])
op('/project1/feedback1').outputConnectors[0].connect(op('/project1/comp1').inputConnectors[1])
op('/project1/comp1').outputConnectors[0].connect(op('/project1/out'))
"""

17. td_execute_python: """
# 将频谱值导出以驱动噪声参数
# 这使噪声对音频频率做出响应
op('/project1/noise1').par.seed.expr = "op('/project1/spectrum_scale')['chan1']"
op('/project1/noise1').par.period.expr = "tdu.remap(op('/project1/spectrum_scale')['chan1'].eval(), 0, 1, 1, 8)"
"""
```

### 模式 2：节拍检测 -> 视觉脉冲

从音频检测节拍并触发视觉事件。

```
Audio Device In CHOP -> Audio Spectrum CHOP -> Math CHOP (isolate bass)
                                                    |
                                              Trigger CHOP (envelope)
                                                    |
                                              [export to visual params]
```

**关键参数设置：**

```
# 分离低频（20-200 Hz）
Math CHOP: chanop=1 (Add channels), range1low=0, range1high=10
           (在 44100Hz 下 512 FFT 时，前 10 个 FFT bin = 低频)

# 每个节拍上的 ADSR 包络
Trigger CHOP: attack=0.02, peak=1.0, decay=0.3, sustain=0.0, release=0.1

# 导出到视觉：缩放、亮度或颜色强度
td_execute_python: "op('/project1/level1').par.brightness1.expr = \"1.0 + op('/project1/trigger1')['chan1'] * 0.5\""
```

### 模式 3：多频段音频 -> 多层视觉

将音频拆分为多个频段，每个频段驱动不同的视觉层。

```
Audio In -> Spectrum -> Audio Band EQ (3 bands: bass, mid, treble)
                              |
                    +---------+---------+
                    |         |         |
                 Bass      Mids     Treble
                  |          |         |
           Noise TOP   Circle TOP  Text TOP
           (slow,dark) (mid,warm)  (fast,bright)
                  |          |         |
                  +-----+----+----+----+
                        |         |
                   Composite  Composite
                        |
                       Out
```

### 模式 3b：音频响应式 GLSL 分形（经过验证的配方）

完整可用配方。播放 MP3、运行 FFT，将频谱作为纹理输入 GLSL 着色器，其中内层分形对低音响应，外层对高音响应。

**网络：**
```
AudioFileIn CHOP → AudioSpectrum CHOP (FFT=512, outlength=256)
    → Math CHOP (gain=10) → CHOP To TOP (256x2 spectrum texture, dataformat=r)
                                                                   ↓
Constant TOP (time, rgba32float) → GLSL TOP (input 0=time, input 1=spectrum) → Null → MovieFileOut
                                                                                        ↓
AudioFileIn CHOP → Audio Device Out CHOP                                          Record to .mov
```

**通过 td_execute_python 构建（每步一次调用以保证可靠性）：**

```python
# 第 1 步：音频链
# td_execute_python 脚本：
td_execute_python(code="""
root = op('/project1')
audio = root.create(audiofileinCHOP, 'audio_in')
audio.par.file = '/path/to/music.mp3'
audio.par.playmode = 0  # 锁定到时间线
audio.par.volume = 0.5

spec = root.create(audiospectrumCHOP, 'spectrum')
audio.outputConnectors[0].connect(spec.inputConnectors[0])

math_n = root.create(mathCHOP, 'math_norm')
spec.outputConnectors[0].connect(math_n.inputConnectors[0])
math_n.par.gain = 5  # 提升信号

resamp = root.create(resampleCHOP, 'resample_spec')
math_n.outputConnectors[0].connect(resamp.inputConnectors[0])
resamp.par.timeslice = True
resamp.par.rate = 256

chop2top = root.create(choptoTOP, 'spectrum_tex')
chop2top.par.chop = resamp  # CHOP To TOP 没有输入连接器 —— 使用 par.chop 引用

# 音频输出（听到音乐）
aout = root.create(audiodeviceoutCHOP, 'audio_out')
audio.outputConnectors[0].connect(aout.inputConnectors[0])
result = 'audio chain ok'
""")

# 第 2 步：时间驱动器（必须是 rgba32float —— 见坑 #6）
# td_execute_python 脚本：
td_execute_python(code="""
root = op('/project1')
td = root.create(constantTOP, 'time_driver')
td.par.format = 'rgba32float'
td.par.outputresolution = 'custom'
td.par.resolutionw = 1
td.par.resolutionh = 1
td.par.colorr.expr = "absTime.seconds % 1000.0"
td.par.colorg.expr = "int(absTime.seconds / 1000.0)"
result = 'time ok'
""")

# 第 3 步：GLSL 着色器（写入 /tmp，从文件加载）
# td_execute_python 脚本：
td_execute_python(code="""
root = op('/project1')
glsl = root.create(glslTOP, 'audio_shader')
glsl.par.outputresolution = 'custom'
glsl.par.resolutionw = 1280
glsl.par.resolutionh = 720

sd = root.create(textDAT, 'shader_code')
sd.text = open('/tmp/my_shader.glsl').read()
glsl.par.pixeldat = sd

# 连线：input 0 = time，input 1 = spectrum texture
op('/project1/time_driver').outputConnectors[0].connect(glsl.inputConnectors[0])
op('/project1/spectrum_tex').outputConnectors[0].connect(glsl.inputConnectors[1])
result = 'glsl ok'
""")

# 第 4 步：输出 + 录制器
# td_execute_python 脚本：
td_execute_python(code="""
root = op('/project1')
out = root.create(nullTOP, 'output')
op('/project1/audio_shader').outputConnectors[0].connect(out.inputConnectors[0])

rec = root.create(moviefileoutTOP, 'recorder')
out.outputConnectors[0].connect(rec.inputConnectors[0])
rec.par.type = 'movie'
rec.par.file = '/tmp/output.mov'
rec.par.videocodec = 'mjpa'
result = 'output ok'
""")
```

**GLSL 着色器模式（音频响应式分形）：**
```glsl
out vec4 fragColor;

vec3 palette(float t) {
    vec3 a = vec3(0.5); vec3 b = vec3(0.5);
    vec3 c = vec3(1.0); vec3 d = vec3(0.263, 0.416, 0.557);
    return a + b * cos(6.28318 * (c * t + d));
}

void main() {
    // Input 0 = time（1x1 rgba32float 常量）
    // Input 1 = audio spectrum（256x2 CHOP To TOP，立体声 —— 在 y=0.25 处采样第一通道）
    vec4 td = texture(sTD2DInputs[0], vec2(0.5));
    float t = td.r + td.g * 1000.0;

    vec2 res = uTDOutputInfo.res.zw;
    vec2 uv = (gl_FragCoord.xy * 2.0 - res) / min(res.x, res.y);
    vec2 uv0 = uv;
    vec3 finalColor = vec3(0.0);

    float bass = texture(sTD2DInputs[1], vec2(0.05, 0.25)).r;
    float mids = texture(sTD2DInputs[1], vec2(0.25, 0.25)).r;

    for (float i = 0.0; i < 4.0; i++) {
        uv = fract(uv * (1.4 + bass * 0.3)) - 0.5;
        float d = length(uv) * exp(-length(uv0));

        // 按距离采样频谱：内层=低音，外层=高音
        float freq = texture(sTD2DInputs[1], vec2(clamp(d * 0.5, 0.0, 1.0), 0.25)).r;

        vec3 col = palette(length(uv0) + i * 0.4 + t * 0.35);
        d = sin(d * (7.0 + bass * 4.0) + t * 1.5) / 8.0;
        d = abs(d);
        d = pow(0.012 / d, 1.2 + freq * 0.8 + bass * 0.5);
        finalColor += col * d;
    }

    // 色调映射
    finalColor = finalColor / (finalColor + vec3(1.0));
    fragColor = TDOutputSwizzle(vec4(finalColor, 1.0));
}
```

**测试中得到的关键洞察：**
- `spectrum_tex`（CHOP To TOP）产生一个 256x2 纹理 —— x 位置 = 频率，y=0.25 为第一通道
- 在 `vec2(0.05, 0.0)` 采样得到低音，`vec2(0.65, 0.0)` 采样得到高音
- 基于像素距离（`d * 0.5`）的采样使内层分形对低音响应，外层对高音响应
- `fract()` 缩放中的 `bass * 0.3` 使分形随鼓点呼吸
- 需要 Math CHOP gain 为 5，因为原始频谱值非常小

## 生成艺术

### 模式 4：带变换的反馈循环

经典生成技术 —— 纹理通过递归变换演化。

```
Noise TOP -> Composite TOP -> Level TOP -> Null TOP (out)
                  ^      |
                  |      v
            Transform TOP <- Feedback TOP
```

**MCP 构建序列：**

```
1. td_create_operator(parent="/project1", type="noiseTop", name="seed_noise")
2. td_create_operator(parent="/project1", type="compositeTop", name="mix")
3. td_create_operator(parent="/project1", type="transformTop", name="evolve")
4. td_create_operator(parent="/project1", type="feedbackTop", name="fb")
5. td_create_operator(parent="/project1", type="levelTop", name="color_correct")
6. td_create_operator(parent="/project1", type="nullTop", name="out")

7. td_set_operator_pars(path="/project1/seed_noise",
     properties={"type": 1, "monochrome": false, "period": 2.0, "amp": 0.3,
                  "resolutionw": 1280, "resolutionh": 720})
8. td_set_operator_pars(path="/project1/mix",
     properties={"operand": 27})  # 27 = Screen 混合
9. td_set_operator_pars(path="/project1/evolve",
     properties={"sx": 1.003, "sy": 1.003, "rz": 0.5, "extend": 2})  # 轻微缩放 + 旋转，重复边缘
10. td_set_operator_pars(path="/project1/fb",
     properties={"top": "/project1/mix"})
11. td_set_operator_pars(path="/project1/color_correct",
     properties={"opacity": 0.98, "gamma1": 0.85})

12. td_execute_python: """
op('/project1/seed_noise').outputConnectors[0].connect(op('/project1/mix').inputConnectors[0])
op('/project1/fb').outputConnectors[0].connect(op('/project1/evolve'))
op('/project1/evolve').outputConnectors[0].connect(op('/project1/mix').inputConnectors[1])
op('/project1/mix').outputConnectors[0].connect(op('/project1/color_correct'))
op('/project1/color_correct').outputConnectors[0].connect(op('/project1/out'))
"""
```

**变体：**
- 更改 Transform：`rz`（旋转）、`sx/sy`（缩放）、`tx/ty`（漂移）
- 更改 Composite 操作数：Screen（发光）、Add（明亮）、Multiply（暗）
- 在反馈环中加入 HSV Adjust 实现颜色演化
- 加入 Blur 获得梦幻般的柔和
- 用 GLSL TOP 替换 Noise 以获得自定义种子图案

### 模式 5：实例化（类粒子系统）

渲染成千上万个几何体副本，每个具有由 CHOP 数据或 DAT 驱动的独特位置/旋转/缩放。

```
Table DAT (instance data) -> DAT to CHOP -> Geometry COMP (instancing on) -> Render TOP
                                              + Sphere SOP (template geometry)
                                              + Constant MAT (material)
                                              + Camera COMP
                                              + Light COMP
```

**MCP 构建序列：**

```
1. td_create_operator(parent="/project1", type="tableDat", name="instance_data")
2. td_create_operator(parent="/project1", type="geometryComp", name="geo1")
3. td_create_operator(parent="/project1/geo1", type="sphereSop", name="sphere")
4. td_create_operator(parent="/project1", type="constMat", name="mat1")
5. td_create_operator(parent="/project1", type="cameraComp", name="cam1")
6. td_create_operator(parent="/project1", type="lightComp", name="light1")
7. td_create_operator(parent="/project1", type="renderTop", name="render1")

8. td_execute_python: """
import random, math
dat = op('/project1/instance_data')
dat.clear()
dat.appendRow(['tx', 'ty', 'tz', 'sx', 'sy', 'sz', 'cr', 'cg', 'cb'])
for i in range(500):
    angle = i * 0.1
    r = 2 + i * 0.01
    dat.appendRow([
        str(math.cos(angle) * r),
        str(math.sin(angle) * r),
        str((i - 250) * 0.02),
        '0.05', '0.05', '0.05',
        str(random.random()),
        str(random.random()),
        str(random.random())
    ])
"""

9. td_set_operator_pars(path="/project1/geo1",
     properties={"instancing": true, "instancechop": "",
                  "instancedat": "/project1/instance_data",
                  "material": "/project1/mat1"})
10. td_set_operator_pars(path="/project1/render1",
     properties={"camera": "/project1/cam1", "geometry": "/project1/geo1",
                  "light": "/project1/light1",
                  "resolutionw": 1280, "resolutionh": 720})
11. td_set_operator_pars(path="/project1/cam1",
     properties={"tz": 10})
```

### 模式 6：反应-扩散（GLSL）

在 GPU 上运行的经典 Gray-Scott 反应-扩散系统。

```
Text DAT (GLSL code) -> GLSL TOP (resolution, dat reference) -> Feedback TOP
                              ^                                       |
                              |_______________________________________|
                         Level TOP (out)
```

**关键 GLSL 代码（通过 td_execute_python 写入 Text DAT）：**

```glsl
// Gray-Scott 反应-扩散
uniform float feed;    // 0.037
uniform float kill;    // 0.06
uniform float dA;      // 1.0
uniform float dB;      // 0.5

layout(location = 0) out vec4 fragColor;

void main() {
    vec2 uv = vUV.st;
    vec2 texel = 1.0 / uTDOutputInfo.res.zw;

    vec4 c = texture(sTD2DInputs[0], uv);
    float a = c.r;
    float b = c.g;

    // 拉普拉斯算子（9 点模板）
    float lA = 0.0, lB = 0.0;
    for(int dx = -1; dx <= 1; dx++) {
        for(int dy = -1; dy <= 1; dy++) {
            float w = (dx == 0 && dy == 0) ? -1.0 : (abs(dx) + abs(dy) == 1 ? 0.2 : 0.05);
            vec4 s = texture(sTD2DInputs[0], uv + vec2(dx, dy) * texel);
            lA += s.r * w;
            lB += s.g * w;
        }
    }

    float reaction = a * b * b;
    float newA = a + (dA * lA - reaction + feed * (1.0 - a));
    float newB = b + (dB * lB + reaction - (kill + feed) * b);

    fragColor = vec4(clamp(newA, 0.0, 1.0), clamp(newB, 0.0, 1.0), 0.0, 1.0);
}
```

## 视频处理

### 模式 7：视频特效链

对视频文件应用一连串特效。

```
Movie File In TOP -> HSV Adjust TOP -> Level TOP -> Blur TOP -> Composite TOP -> Null TOP (out)
                                                                      ^
                                                          Text TOP ---+
```

**MCP 构建序列：**

```
1. td_create_operator(parent="/project1", type="moviefileinTop", name="video_in")
2. td_create_operator(parent="/project1", type="hsvadjustTop", name="color")
3. td_create_operator(parent="/project1", type="levelTop", name="levels")
4. td_create_operator(parent="/project1", type="blurTop", name="blur")
5. td_create_operator(parent="/project1", type="compositeTop", name="overlay")
6. td_create_operator(parent="/project1", type="textTop", name="title")
7. td_create_operator(parent="/project1", type="nullTop", name="out")

8. td_set_operator_pars(path="/project1/video_in",
     properties={"file": "/path/to/video.mp4", "play": true})
9. td_set_operator_pars(path="/project1/color",
     properties={"hueoffset": 0.1, "saturationmult": 1.3})
10. td_set_operator_pars(path="/project1/levels",
     properties={"brightness1": 1.1, "contrast": 1.2, "gamma1": 0.9})
11. td_set_operator_pars(path="/project1/blur",
     properties={"sizex": 2, "sizey": 2})
12. td_set_operator_pars(path="/project1/title",
     properties={"text": "My Video", "fontsizex": 48, "alignx": 1, "aligny": 1})

13. td_execute_python: """
chain = ['video_in', 'color', 'levels', 'blur']
for i in range(len(chain) - 1):
    op(f'/project1/{chain[i]}').outputConnectors[0].connect(op(f'/project1/{chain[i+1]}'))
op('/project1/blur').outputConnectors[0].connect(op('/project1/overlay').inputConnectors[0])
op('/project1/title').outputConnectors[0].connect(op('/project1/overlay').inputConnectors[1])
op('/project1/overlay').outputConnectors[0].connect(op('/project1/out'))
"""
```

### 模式 8：视频录制

将输出录制到文件。**H.264/H.265 需要商业授权** —— 在非商业版上使用 Motion JPEG（`mjpa`）。

```
[any TOP chain] -> Null TOP -> Movie File Out TOP
```

```python
# 通过 td_execute_python 构建：
root = op('/project1')

# 录制器之前总是放一个 Null TOP
null_out = root.op('out')  # 或创建一个
rec = root.create(moviefileoutTOP, 'recorder')
null_out.outputConnectors[0].connect(rec.inputConnectors[0])

rec.par.type = 'movie'
rec.par.file = '/tmp/output.mov'
rec.par.videocodec = 'mjpa'  # Motion JPEG —— 在非商业版上可用

# 开始录制（par.record 是一个开关 —— .record() 方法可能不存在）
rec.par.record = True
# ... 让 TD 运行所需的时长 ...
rec.par.record = False

# 对于图像序列：
# rec.par.type = 'imagesequence'
# rec.par.imagefiletype = 'png'
# rec.par.file.expr = "'/tmp/frames/out' + me.fileSuffix"  # fileSuffix 必需
```

**坑：**
- 在同一脚本中设置 `par.file` + `par.record = True` 可能会竞争 —— 使用 `run("...", delayFrames=2)`
- 快速调用 `TOP.save()` 总是捕获同一帧 —— 动画请使用 MovieFileOut
- 完整详情见 `pitfalls.md` #25-27

### 模式 8b：TD → 外部管线（FFmpeg / Python / 后处理）

导出 TD 视觉以供其他工具（ffmpeg、Python、ASCII 艺术等）使用。当你需要将 TD 输出与外部处理（ASCII 转换、Python 着色器链、ML 推理等）合成时，这是标准工作流。

**第 1 步：在 TD 中录制为视频**

```python
# 首选：macOS 上的 ProRes（无损，非商业版 OK，1280x720 时约 55MB/s）
rec.par.videocodec = 'prores'
# 非 macOS 的备选：mjpa（Motion JPEG）
# rec.par.videocodec = 'mjpa'
rec.par.record = True
# ... 等待 N 秒 ...
rec.par.record = False
```

**第 2 步：用 ffmpeg 提取帧**

```bash
# 以 30fps 提取所有帧
ffmpeg -y -i /tmp/output.mov -vf 'fps=30' /tmp/frames/frame_%06d.png

# 或提取特定时长
ffmpeg -y -i /tmp/output.mov -t 25 -vf 'fps=30' /tmp/frames/frame_%06d.png

# 或提取特定帧范围
ffmpeg -y -i /tmp/output.mov -vf 'select=between(n\,0\,749)' -vsync vfr /tmp/frames/frame_%06d.png
```

**第 3 步：在 Python 中处理帧**

```python
from PIL import Image
import os

frames_dir = '/tmp/frames'
output_dir = '/tmp/processed'
os.makedirs(output_dir, exist_ok=True)

for fname in sorted(os.listdir(frames_dir)):
    if not fname.endswith('.png'):
        continue
    img = Image.open(os.path.join(frames_dir, fname))
    # ... 应用你的处理 ...
    img.save(os.path.join(output_dir, fname))
```

**第 4 步：把处理后的帧与音频复用回去**

```bash
# 用处理后的帧 + 音频创建视频，带淡出
ffmpeg -y \
  -framerate 30 -i /tmp/processed/frame_%06d.png \
  -i /tmp/audio.mp3 \
  -c:v libx264 -pix_fmt yuv420p -crf 18 \
  -c:a aac -b:a 192k \
  -shortest \
  -af 'afade=t=out:st=23:d=2' \
  /tmp/final_output.mp4
```

**关键注意事项：**
- TD 录制步骤使用 ProRes 以避免合成时的代际损失
- 以目标输出帧率提取（而非 TD 的渲染帧率）
- 对于音频同步内容，在 Python 中单独分析音频文件（scipy FFT）以获取每帧特征（rms、频谱带、节拍），并驱动合成参数
- 录制前总是验证 TD FPS > 0（见坑 #37、#38）

## 数据可视化

### 模式 9：表格数据 -> 通过实例化生成柱状图

将表格数据可视化为 3D 柱状图。

```
Table DAT (data) -> Script DAT (transform to instance format) -> DAT to CHOP
                                                                      |
Box SOP -> Geometry COMP (instancing from CHOP) -> Render TOP -> Null TOP (out)
           + PBR MAT
           + Camera COMP
           + Light COMP
```

```python
# 将数据转换为实例位置的 Script DAT 代码
td_execute_python: """
source = op('/project1/data_table')
instance = op('/project1/instance_transform')
instance.clear()
instance.appendRow(['tx', 'ty', 'tz', 'sx', 'sy', 'sz', 'cr', 'cg', 'cb'])

for i in range(1, source.numRows):
    value = float(source[i, 'value'])
    name = source[i, 'name']
    instance.appendRow([
        str(i * 1.5),          # x 位置（展开柱子）
        str(value / 2),        # y 位置（垂直居中柱子）
        '0',                   # z 位置
        '1', str(value), '1',  # 缩放（高度 = 数据值）
        '0.2', '0.6', '1.0'   # 颜色（蓝色）
    ])
"""
```

### 模式 9b：音频响应式 GLSL 分形（经过验证的配方）

音频频谱通过频谱纹理输入直接驱动 GLSL 分形着色器。低音加粗内层分形线条，中音扭转旋转，高音点亮外层边缘。**在使用这些配方中的任何参数名之前，始终先运行发现流程（SKILL.md 第 0 步）—— 它们在你的 TD 版本中可能不同。**

```
Audio File In CHOP → Audio Spectrum CHOP (FFT=512, outlength=256)
    → Math CHOP (gain=10)
    → CHOP To TOP (spectrum texture, 256x2, dataformat=r)
                                          ↓ (input 1)
Constant TOP (rgba32float, time) → GLSL TOP (audio-reactive shader) → Null TOP
        (input 0)                    ↑
                              Text DAT (shader code)
```

**通过 td_execute_python 构建（完整的可用脚本）：**

```python
# td_execute_python 脚本：
td_execute_python(code="""
import os
root = op('/project1')

# 音频输入
audio = root.create(audiofileinCHOP, 'audio_in')
audio.par.file = '/path/to/music.mp3'
audio.par.playmode = 0  # 锁定到时间线

# FFT 分析（输出长度手动设为 256 bin）
spectrum = root.create(audiospectrumCHOP, 'spectrum')
audio.outputConnectors[0].connect(spectrum.inputConnectors[0])
spectrum.par.fftsize = '512'
spectrum.par.outputmenu = 'setmanually'
spectrum.par.outlength = 256

# 然后提升原始频谱的 gain（不要用 Lag CHOP —— 见坑 #34）
math = root.create(mathCHOP, 'math_norm')
spectrum.outputConnectors[0].connect(math.inputConnectors[0])
math.par.gain = 10

# 频谱 → 纹理（256x2 图像 —— 立体声，在 y=0.25 处采样第一通道）
# 注意：choptoTOP 没有输入连接器 —— 使用 par.chop 引用！
spec_tex = root.create(choptoTOP, 'spectrum_tex')
spec_tex.par.chop = math
spec_tex.par.dataformat = 'r'
spec_tex.par.layout = 'rowscropped'

# 时间驱动器（rgba32float 以避免 0-1 钳制！）
time_drv = root.create(constantTOP, 'time_driver')
time_drv.par.format = 'rgba32float'
time_drv.par.outputresolution = 'custom'
time_drv.par.resolutionw = 1
time_drv.par.resolutionh = 1
time_drv.par.colorr.expr = "absTime.seconds % 1000.0"
time_drv.par.colorg.expr = "int(absTime.seconds / 1000.0)"

# GLSL 着色器
glsl = root.create(glslTOP, 'audio_shader')
glsl.par.outputresolution = 'custom'
glsl.par.resolutionw = 1280; glsl.par.resolutionh = 720

shader_dat = root.create(textDAT, 'shader_code')
shader_dat.text = open('/tmp/shader.glsl').read()
glsl.par.pixeldat = shader_dat

# 连线：input 0=time，input 1=spectrum
time_drv.outputConnectors[0].connect(glsl.inputConnectors[0])
spec_tex.outputConnectors[0].connect(glsl.inputConnectors[1])

# 输出 + 音频回放
out = root.create(nullTOP, 'output')
glsl.outputConnectors[0].connect(out.inputConnectors[0])
audio_out = root.create(audiodeviceoutCHOP, 'audio_out')
audio.outputConnectors[0].connect(audio_out.inputConnectors[0])

result = 'network built'
""")
```

**GLSL 着色器（从 input 1 纹理读取频谱）：**

```glsl
out vec4 fragColor;

vec3 palette(float t) {
    vec3 a = vec3(0.5); vec3 b = vec3(0.5);
    vec3 c = vec3(1.0); vec3 d = vec3(0.263, 0.416, 0.557);
    return a + b * cos(6.28318 * (c * t + d));
}

void main() {
    vec4 td = texture(sTD2DInputs[0], vec2(0.5));
    float t = td.r + td.g * 1000.0;

    vec2 res = uTDOutputInfo.res.zw;
    vec2 uv = (gl_FragCoord.xy * 2.0 - res) / min(res.x, res.y);
    vec2 uv0 = uv;
    vec3 finalColor = vec3(0.0);

    float bass = texture(sTD2DInputs[1], vec2(0.05, 0.25)).r;
    float mids = texture(sTD2DInputs[1], vec2(0.25, 0.25)).r;
    float highs = texture(sTD2DInputs[1], vec2(0.65, 0.25)).r;

    float ca = cos(t * (0.15 + mids * 0.3));
    float sa = sin(t * (0.15 + mids * 0.3));
    uv = mat2(ca, -sa, sa, ca) * uv;

    for (float i = 0.0; i < 4.0; i++) {
        uv = fract(uv * (1.4 + bass * 0.3)) - 0.5;
        float d = length(uv) * exp(-length(uv0));
        float freq = texture(sTD2DInputs[1], vec2(clamp(d*0.5, 0.0, 1.0), 0.25)).r;
        vec3 col = palette(length(uv0) + i * 0.4 + t * 0.35);
        d = sin(d * (7.0 + bass * 4.0) + t * 1.5) / 8.0;
        d = abs(d);
        d = pow(0.012 / d, 1.2 + freq * 0.8 + bass * 0.5);
        finalColor += col * d;
    }

    float glow = (0.03 + bass * 0.05) / (length(uv0) + 0.03);
    finalColor += vec3(0.4, 0.1, 0.7) * glow * (0.6 + 0.4 * sin(t * 2.5));

    float ring = abs(length(uv0) - 0.4 - mids * 0.3);
    finalColor += vec3(0.1, 0.6, 0.8) * (0.005 / ring) * (0.2 + highs * 0.5);

    finalColor *= smoothstep(0.0, 1.0, 1.0 - dot(uv0*0.55, uv0*0.55));
    finalColor = finalColor / (finalColor + vec3(1.0));

    fragColor = TDOutputSwizzle(vec4(finalColor, 1.0));
}
```

**频谱采样如何驱动视觉：**
- `texture(sTD2DInputs[1], vec2(x, 0.0)).r` —— x 位置 = 频率（0=低音，1=高音）
- 内层分形迭代采样较低的 x → 对低音响应
- 外层迭代采样较高的 x → 对高音响应
- `fract()` 缩放上的 `bass * 0.3` → 分形缩放随低音脉冲
- sin 频率上的 `bass * 4.0` → 线条密度随低音脉冲
- 旋转速度上的 `mids * 0.3` → 人声/中频段期间螺旋扭转更快
- 环不透明度上的 `highs * 0.5` → 外环上的高频闪烁

**录制输出：**使用带 `mjpa` 编解码器的 MovieFileOut TOP（H.264 需要商业授权）。见坑 #25-27。

## GLSL 着色器

### 模式 10：自定义片段着色器

将自定义视觉效果编写为 GLSL 片段着色器。

```
Text DAT (shader code) -> GLSL TOP -> Level TOP -> Null TOP (out)
                           + optional input TOPs for texture sampling
```

**TouchDesigner 中可用的常见 GLSL uniform：**

```glsl
// 由 TD 自动提供
uniform vec4 uTDOutputInfo;  // .res.zw = 分辨率

// 注意：TD 099 中不存在 uTDCurrentTime！
// 通过 1x1 Constant TOP（format=rgba32float）馈送时间：
//   t.par.colorr.expr = "absTime.seconds % 1000.0"
//   t.par.colorg.expr = "int(absTime.seconds / 1000.0)"
// 然后在 GLSL 中读取：
//   vec4 td = texture(sTD2DInputs[0], vec2(0.5));
//   float t = td.r + td.g * 1000.0;

// 输入纹理（来自已连接的 TOP 输入）
uniform sampler2D sTD2DInputs[1];  // 输入采样器数组

// 来自顶点着色器
in vec3 vUV;  // UV 坐标（0-1 范围）
```

**示例：Plasma 着色器（使用来自输入纹理的时间）**

```glsl
layout(location = 0) out vec4 fragColor;

void main() {
    vec2 uv = vUV.st;
    // 从 Constant TOP input 0（rgba32float 格式）读取时间
    vec4 td = texture(sTD2DInputs[0], vec2(0.5));
    float t = td.r + td.g * 1000.0;

    float v1 = sin(uv.x * 10.0 + t);
    float v2 = sin(uv.y * 10.0 + t * 0.7);
    float v3 = sin((uv.x + uv.y) * 10.0 + t * 1.3);
    float v4 = sin(length(uv - 0.5) * 20.0 - t * 2.0);

    float v = (v1 + v2 + v3 + v4) * 0.25;

    vec3 color = vec3(
        sin(v * 3.14159 + 0.0) * 0.5 + 0.5,
        sin(v * 3.14159 + 2.094) * 0.5 + 0.5,
        sin(v * 3.14159 + 4.189) * 0.5 + 0.5
    );

    fragColor = vec4(color, 1.0);
}
```

### 模式 11：多遍 GLSL（Ping-Pong）

对于需要跨帧状态的效果（粒子、流体、元胞自动机），使用带多个 pass 的 GLSL Multi TOP 或 Feedback TOP 循环。

```
GLSL Multi TOP (pass 0: simulation, pass 1: rendering)
   + Text DAT (simulation shader)
   + Text DAT (render shader)
   -> Level TOP -> Null TOP (out)
      ^
      |__ Feedback TOP (feeds simulation state back)
```

## 交互装置

### 模式 12：鼠标/触摸 -> 视觉响应

```
Mouse In CHOP -> Math CHOP (normalize to 0-1) -> [export to visual params]

# 或用于触摸/多指触摸：
Multi Touch In DAT -> Script CHOP (parse touches) -> [export to visual params]
```

```python
# 将鼠标位置归一化到 0-1 范围
td_execute_python: """
op('/project1/noise1').par.offsetx.expr = "op('/project1/mouse_norm')['tx']"
op('/project1/noise1').par.offsety.expr = "op('/project1/mouse_norm')['ty']"
"""
```

### 模式 13：OSC 控制（来自外部软件）

```
OSC In CHOP (port 7000) -> Select CHOP (pick channels) -> [export to visual params]
```

```
1. td_create_operator(parent="/project1", type="oscinChop", name="osc_in")
2. td_set_operator_pars(path="/project1/osc_in", properties={"port": 7000})

# 像 /frequency 440 这样的 OSC 消息将显示为值为 440 的通道 "frequency"
# 导出到任何参数：
3. td_execute_python: "op('/project1/noise1').par.period.expr = \"op('/project1/osc_in')['frequency']\""
```

### 模式 14：MIDI 控制（DJ/VJ）

```
MIDI In CHOP (device) -> Select CHOP -> [export channels to visual params]
```

常见 MIDI 映射：
- CC 通道（旋钮/推子）：连续 0-127，映射到浮点参数
- Note On/Off：二进制触发，映射到 Trigger CHOP 做包络
- Velocity：强度/亮度

## 现场表演

### 模式 15：多源 VJ 设置

```
Source A (generative) ----+
Source B (video) ---------+-- Switch/Cross TOP -- Level TOP -- Window COMP (output)
Source C (camera) --------+
                           ^
                    MIDI/OSC control selects active source and crossfade
```

```python
# MIDI CC1 控制哪个源是活动的（0-127 -> 0-2）
td_execute_python: """
op('/project1/switch1').par.index.expr = "int(op('/project1/midi_in')['cc1'] / 42)"
"""

# MIDI CC2 控制当前和下一个之间的交叉淡入淡出
td_execute_python: """
op('/project1/cross1').par.cross.expr = "op('/project1/midi_in')['cc2'] / 127.0"
"""
```

### 模式 16：投影映射

```
Content TOPs ----+
                 |
Stoner TOP (UV mapping) -> Composite TOP -> Window COMP (projector output)
   or
Kantan Mapper COMP (external .tox)
```

对于投影映射，关键是：
1. 将你的视觉内容创建为标准 TOP
2. 使用 Stoner TOP 或第三方映射工具将内容 UV 映射到物理表面
3. 通过 Window COMP 输出到投影仪

### 模式 17：Cue 系统

```
Table DAT (cue list: cue_number, scene_name, duration, transition_type)
    |
Script CHOP (cue state: current_cue, progress, next_cue_trigger)
    |
[export to Switch/Cross TOPs to transition between scenes]
```

```python
td_execute_python: """
# 简单的 cue 系统
cue_table = op('/project1/cue_list')
cue_state = op('/project1/cue_state')

def advance_cue():
    current = int(cue_state.par.value0.val)
    next_cue = min(current + 1, cue_table.numRows - 1)
    cue_state.par.value0.val = next_cue
    
    scene = cue_table[next_cue, 'scene']
    duration = float(cue_table[next_cue, 'duration'])
    
    # 设置交叉淡入淡出目标和时长
    op('/project1/cross1').par.cross.val = 0
    # 在 duration 秒内将交叉动画到 1.0
    # （使用 Timer CHOP 或 LFO CHOP 做平滑动画）
"""
```

## 网络

### 模式 18：OSC 服务器/客户端

```
# 发送 OSC
OSC Out CHOP -> (network) -> external application

# 接收 OSC  
(network) -> OSC In CHOP -> Select CHOP -> [use values]
```

### 模式 19：NDI 视频流

```
# 通过网络发送视频
[any TOP chain] -> NDI Out TOP (source name)

# 从网络接收视频
NDI In TOP (select source) -> [process as normal TOP]
```

### 模式 20：WebSocket 通信

```
WebSocket DAT -> Script DAT (parse JSON messages) -> [update visuals]
```

```python
td_execute_python: """
ws = op('/project1/websocket1')
ws.par.address = 'ws://localhost:8080'
ws.par.active = True

# 在 DAT Execute 回调中（监视 WebSocket DAT 的 Script DAT）：
# def onTableChange(dat):
#     import json
#     msg = json.loads(dat.text)
#     op('/project1/noise1').par.seed.val = msg.get('seed', 0)
"""
```
