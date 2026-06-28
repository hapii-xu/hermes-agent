---
name: audiocraft-audio-generation
description: "AudioCraft：MusicGen 文生音乐、AudioGen 文生音效。"
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [audiocraft, torch>=2.0.0, transformers>=4.30.0]
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Multimodal, Audio Generation, Text-to-Music, Text-to-Audio, MusicGen]

---

# AudioCraft：音频生成

使用 Meta 的 AudioCraft 通过 MusicGen、AudioGen 和 EnCodec 进行文生音乐和文生音频生成的综合指南。

## 何时使用 AudioCraft

**在以下情况使用 AudioCraft：**
- 需要从文本描述生成音乐
- 创建音效和环境音频
- 构建音乐生成应用
- 需要旋律条件的音乐生成
- 想要立体声音频输出
- 需要可控的音乐生成与风格迁移

**关键特性：**
- **MusicGen**：带旋律条件的文生音乐生成
- **AudioGen**：文生音效生成
- **EnCodec**：高保真神经音频编解码器
- **多种模型尺寸**：从 Small（300M）到 Large（3.3B）
- **立体声支持**：完整的立体声音频生成
- **风格条件化**：MusicGen-Style 支持基于参考音频的生成

**在以下情况改用替代方案：**
- **Stable Audio**：用于更长的商业音乐生成
- **Bark**：用于带音乐/音效的文本转语音
- **Riffusion**：用于基于频谱图的音乐生成
- **OpenAI Jukebox**：用于带歌词的原始音频生成

## 快速开始

### 安装

```bash
# 从 PyPI 安装
pip install audiocraft

# 从 GitHub 安装（最新）
pip install git+https://github.com/facebookresearch/audiocraft.git

# 或使用 HuggingFace Transformers
pip install transformers torch torchaudio
```

### 基础文生音乐（AudioCraft）

```python
import torchaudio
from audiocraft.models import MusicGen

# 加载模型
model = MusicGen.get_pretrained('facebook/musicgen-small')

# 设置生成参数
model.set_generation_params(
    duration=8,  # 秒
    top_k=250,
    temperature=1.0
)

# 从文本生成
descriptions = ["happy upbeat electronic dance music with synths"]
wav = model.generate(descriptions)

# 保存音频
torchaudio.save("output.wav", wav[0].cpu(), sample_rate=32000)
```

### 使用 HuggingFace Transformers

```python
from transformers import AutoProcessor, MusicgenForConditionalGeneration
import scipy

# 加载模型和 processor
processor = AutoProcessor.from_pretrained("facebook/musicgen-small")
model = MusicgenForConditionalGeneration.from_pretrained("facebook/musicgen-small")
model.to("cuda")

# 生成音乐
inputs = processor(
    text=["80s pop track with bassy drums and synth"],
    padding=True,
    return_tensors="pt"
).to("cuda")

audio_values = model.generate(
    **inputs,
    do_sample=True,
    guidance_scale=3,
    max_new_tokens=256
)

# 保存
sampling_rate = model.config.audio_encoder.sampling_rate
scipy.io.wavfile.write("output.wav", rate=sampling_rate, data=audio_values[0, 0].cpu().numpy())
```

### 用 AudioGen 做文生音效

```python
from audiocraft.models import AudioGen

# 加载 AudioGen
model = AudioGen.get_pretrained('facebook/audiogen-medium')

model.set_generation_params(duration=5)

# 生成音效
descriptions = ["dog barking in a park with birds chirping"]
wav = model.generate(descriptions)

torchaudio.save("sound.wav", wav[0].cpu(), sample_rate=16000)
```

## 核心概念

### 架构概览

```
AudioCraft 架构：
┌──────────────────────────────────────────────────────────────┐
│                    文本编码器（T5）                            │
│                         │                                     │
│                    文本嵌入                                  │
└────────────────────────┬─────────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────────┐
│              Transformer 解码器（LM）                          │
│     自回归地生成音频 token                                     │
│     使用高效的 token 交错模式                                   │
└────────────────────────┬─────────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────────┐
│                EnCodec 音频解码器                              │
│        把 token 转回音频波形                                   │
└──────────────────────────────────────────────────────────────┘
```

### 模型变体

| 模型 | 尺寸 | 说明 | 用途 |
|-------|------|-------------|----------|
| `musicgen-small` | 300M | 文生音乐 | 快速生成 |
| `musicgen-medium` | 1.5B | 文生音乐 | 均衡 |
| `musicgen-large` | 3.3B | 文生音乐 | 最佳质量 |
| `musicgen-melody` | 1.5B | 文本 + 旋律 | 旋律条件化 |
| `musicgen-melody-large` | 3.3B | 文本 + 旋律 | 最佳旋律 |
| `musicgen-stereo-*` | 不定 | 立体声输出 | 立体声生成 |
| `musicgen-style` | 1.5B | 风格迁移 | 基于参考 |
| `audiogen-medium` | 1.5B | 文生音效 | 音效 |

### 生成参数

| 参数 | 默认值 | 说明 |
|-----------|---------|-------------|
| `duration` | 8.0 | 长度（秒，1-120） |
| `top_k` | 250 | Top-k 采样 |
| `top_p` | 0.0 | 核采样（0 = 禁用） |
| `temperature` | 1.0 | 采样温度 |
| `cfg_coef` | 3.0 | 无分类器引导 |

## MusicGen 用法

### 文生音乐生成

```python
from audiocraft.models import MusicGen
import torchaudio

model = MusicGen.get_pretrained('facebook/musicgen-medium')

# 配置生成
model.set_generation_params(
    duration=30,          # 最多 30 秒
    top_k=250,            # 采样多样性
    top_p=0.0,            # 0 = 仅用 top_k
    temperature=1.0,      # 创造力（越高 = 越多变）
    cfg_coef=3.0          # 文本贴合度（越高 = 越严格）
)

# 生成多个样本
descriptions = [
    "epic orchestral soundtrack with strings and brass",
    "chill lo-fi hip hop beat with jazzy piano",
    "energetic rock song with electric guitar"
]

# 生成（返回 [batch, channels, samples]）
wav = model.generate(descriptions)

# 逐个保存
for i, audio in enumerate(wav):
    torchaudio.save(f"music_{i}.wav", audio.cpu(), sample_rate=32000)
```

### 旋律条件化生成

```python
from audiocraft.models import MusicGen
import torchaudio

# 加载旋律模型
model = MusicGen.get_pretrained('facebook/musicgen-melody')
model.set_generation_params(duration=30)

# 加载旋律音频
melody, sr = torchaudio.load("melody.wav")

# 用旋律条件化生成
descriptions = ["acoustic guitar folk song"]
wav = model.generate_with_chroma(descriptions, melody, sr)

torchaudio.save("melody_conditioned.wav", wav[0].cpu(), sample_rate=32000)
```

### 立体声生成

```python
from audiocraft.models import MusicGen

# 加载立体声模型
model = MusicGen.get_pretrained('facebook/musicgen-stereo-medium')
model.set_generation_params(duration=15)

descriptions = ["ambient electronic music with wide stereo panning"]
wav = model.generate(descriptions)

# 立体声 wav 形状：[batch, 2, samples]
print(f"Stereo shape: {wav.shape}")  # [1, 2, 480000]
torchaudio.save("stereo.wav", wav[0].cpu(), sample_rate=32000)
```

### 音频续写

```python
from transformers import AutoProcessor, MusicgenForConditionalGeneration

processor = AutoProcessor.from_pretrained("facebook/musicgen-medium")
model = MusicgenForConditionalGeneration.from_pretrained("facebook/musicgen-medium")

# 加载要续写的音频
import torchaudio
audio, sr = torchaudio.load("intro.wav")

# 用文本和音频一起处理
inputs = processor(
    audio=audio.squeeze().numpy(),
    sampling_rate=sr,
    text=["continue with a epic chorus"],
    padding=True,
    return_tensors="pt"
)

# 生成续写
audio_values = model.generate(**inputs, do_sample=True, guidance_scale=3, max_new_tokens=512)
```

## MusicGen-Style 用法

### 风格条件化生成

```python
from audiocraft.models import MusicGen

# 加载风格模型
model = MusicGen.get_pretrained('facebook/musicgen-style')

# 配置带风格的生成
model.set_generation_params(
    duration=30,
    cfg_coef=3.0,
    cfg_coef_beta=5.0  # 风格影响
)

# 配置风格条件器
model.set_style_conditioner_params(
    eval_q=3,          # RVQ 量化器（1-6）
    excerpt_length=3.0  # 风格摘录长度
)

# 加载风格参考
style_audio, sr = torchaudio.load("reference_style.wav")

# 用文本 + 风格生成
descriptions = ["upbeat dance track"]
wav = model.generate_with_style(descriptions, style_audio, sr)
```

### 纯风格生成（无文本）

```python
# 不带文本提示、按匹配风格生成
model.set_generation_params(
    duration=30,
    cfg_coef=3.0,
    cfg_coef_beta=None  # 纯风格时禁用双重 CFG
)

wav = model.generate_with_style([None], style_audio, sr)
```

## AudioGen 用法

### 音效生成

```python
from audiocraft.models import AudioGen
import torchaudio

model = AudioGen.get_pretrained('facebook/audiogen-medium')
model.set_generation_params(duration=10)

# 生成各种声音
descriptions = [
    "thunderstorm with heavy rain and lightning",
    "busy city traffic with car horns",
    "ocean waves crashing on rocks",
    "crackling campfire in forest"
]

wav = model.generate(descriptions)

for i, audio in enumerate(wav):
    torchaudio.save(f"sound_{i}.wav", audio.cpu(), sample_rate=16000)
```

## EnCodec 用法

### 音频压缩

```python
from audiocraft.models import CompressionModel
import torch
import torchaudio

# 加载 EnCodec
model = CompressionModel.get_pretrained('facebook/encodec_32khz')

# 加载音频
wav, sr = torchaudio.load("audio.wav")

# 确保采样率正确
if sr != 32000:
    resampler = torchaudio.transforms.Resample(sr, 32000)
    wav = resampler(wav)

# 编码为 token
with torch.no_grad():
    encoded = model.encode(wav.unsqueeze(0))
    codes = encoded[0]  # 音频码本

# 解码回音频
with torch.no_grad():
    decoded = model.decode(codes)

torchaudio.save("reconstructed.wav", decoded[0].cpu(), sample_rate=32000)
```

## 常见工作流

### 工作流 1：音乐生成管线

```python
import torch
import torchaudio
from audiocraft.models import MusicGen

class MusicGenerator:
    def __init__(self, model_name="facebook/musicgen-medium"):
        self.model = MusicGen.get_pretrained(model_name)
        self.sample_rate = 32000

    def generate(self, prompt, duration=30, temperature=1.0, cfg=3.0):
        self.model.set_generation_params(
            duration=duration,
            top_k=250,
            temperature=temperature,
            cfg_coef=cfg
        )

        with torch.no_grad():
            wav = self.model.generate([prompt])

        return wav[0].cpu()

    def generate_batch(self, prompts, duration=30):
        self.model.set_generation_params(duration=duration)

        with torch.no_grad():
            wav = self.model.generate(prompts)

        return wav.cpu()

    def save(self, audio, path):
        torchaudio.save(path, audio, sample_rate=self.sample_rate)

# 用法
generator = MusicGenerator()
audio = generator.generate(
    "epic cinematic orchestral music",
    duration=30,
    temperature=1.0
)
generator.save(audio, "epic_music.wav")
```

### 工作流 2：声音设计批处理

```python
import json
from pathlib import Path
from audiocraft.models import AudioGen
import torchaudio

def batch_generate_sounds(sound_specs, output_dir):
    """
    根据规格说明批量生成多个声音。

    参数：
        sound_specs：{"name": str, "description": str, "duration": float} 的列表
        output_dir：输出目录路径
    """
    model = AudioGen.get_pretrained('facebook/audiogen-medium')
    output_dir = Path(output_dir)
    output_dir.mkdir(exist_ok=True)

    results = []

    for spec in sound_specs:
        model.set_generation_params(duration=spec.get("duration", 5))

        wav = model.generate([spec["description"]])

        output_path = output_dir / f"{spec['name']}.wav"
        torchaudio.save(str(output_path), wav[0].cpu(), sample_rate=16000)

        results.append({
            "name": spec["name"],
            "path": str(output_path),
            "description": spec["description"]
        })

    return results

# 用法
sounds = [
    {"name": "explosion", "description": "massive explosion with debris", "duration": 3},
    {"name": "footsteps", "description": "footsteps on wooden floor", "duration": 5},
    {"name": "door", "description": "wooden door creaking and closing", "duration": 2}
]

results = batch_generate_sounds(sounds, "sound_effects/")
```

### 工作流 3：Gradio demo

```python
import gradio as gr
import torch
import torchaudio
from audiocraft.models import MusicGen

model = MusicGen.get_pretrained('facebook/musicgen-small')

def generate_music(prompt, duration, temperature, cfg_coef):
    model.set_generation_params(
        duration=duration,
        temperature=temperature,
        cfg_coef=cfg_coef
    )

    with torch.no_grad():
        wav = model.generate([prompt])

    # 保存到临时文件
    path = "temp_output.wav"
    torchaudio.save(path, wav[0].cpu(), sample_rate=32000)
    return path

demo = gr.Interface(
    fn=generate_music,
    inputs=[
        gr.Textbox(label="Music Description", placeholder="upbeat electronic dance music"),
        gr.Slider(1, 30, value=8, label="Duration (seconds)"),
        gr.Slider(0.5, 2.0, value=1.0, label="Temperature"),
        gr.Slider(1.0, 10.0, value=3.0, label="CFG Coefficient")
    ],
    outputs=gr.Audio(label="Generated Music"),
    title="MusicGen Demo"
)

demo.launch()
```

## 性能优化

### 内存优化

```python
# 使用更小的模型
model = MusicGen.get_pretrained('facebook/musicgen-small')

# 在多次生成之间清空缓存
torch.cuda.empty_cache()

# 生成更短的时长
model.set_generation_params(duration=10)  # 而非 30

# 使用半精度
model = model.half()
```

### 批处理效率

```python
# 一次处理多个提示（更高效）
descriptions = ["prompt1", "prompt2", "prompt3", "prompt4"]
wav = model.generate(descriptions)  # 单个批次

# 而不是
for desc in descriptions:
    wav = model.generate([desc])  # 多个批次（更慢）
```

### GPU 显存需求

| 模型 | FP32 显存 | FP16 显存 |
|-------|-----------|-----------|
| musicgen-small | ~4GB | ~2GB |
| musicgen-medium | ~8GB | ~4GB |
| musicgen-large | ~16GB | ~8GB |

## 常见问题

| 问题 | 解决方案 |
|-------|----------|
| CUDA OOM | 用更小的模型，缩短时长 |
| 质量差 | 提高 cfg_coef，改进提示词 |
| 生成太短 | 检查最大时长设置 |
| 音频伪音 | 尝试不同的 temperature |
| 立体声不工作 | 使用立体声模型变体 |

## 参考

- **[高级用法](references/advanced-usage.md)** - 训练、微调、部署
- **[故障排查](references/troubleshooting.md)** - 常见问题与解决方案

## 资源

- **GitHub**: https://github.com/facebookresearch/audiocraft
- **论文（MusicGen）**: https://arxiv.org/abs/2306.05284
- **论文（AudioGen）**: https://arxiv.org/abs/2209.15352
- **HuggingFace**: https://huggingface.co/facebook/musicgen-small
- **Demo**: https://huggingface.co/spaces/facebook/MusicGen
