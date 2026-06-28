# AudioCraft 故障排查指南

## 安装问题

### 导入错误

**错误**：`ModuleNotFoundError: No module named 'audiocraft'`

**解决方案**：
```bash
# 从 PyPI 安装
pip install audiocraft

# 或从 GitHub 安装
pip install git+https://github.com/facebookresearch/audiocraft.git

# 验证安装
python -c "from audiocraft.models import MusicGen; print('OK')"
```

### 找不到 FFmpeg

**错误**：`RuntimeError: ffmpeg not found`

**解决方案**：
```bash
# Ubuntu/Debian
sudo apt-get install ffmpeg

# macOS
brew install ffmpeg

# Windows（用 conda）
conda install -c conda-forge ffmpeg

# 验证
ffmpeg -version
```

### PyTorch CUDA 不匹配

**错误**：`RuntimeError: CUDA error: no kernel image is available`

**解决方案**：
```bash
# 检查 CUDA 版本
nvcc --version
python -c "import torch; print(torch.version.cuda)"

# 安装匹配的 PyTorch
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121

# 适用于 CUDA 11.8
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu118
```

### xformers 问题

**错误**：`ImportError: xformers` 相关错误

**解决方案**：
```bash
# 安装 xformers 以节省内存
pip install xformers

# 或禁用 xformers
export AUDIOCRAFT_USE_XFORMERS=0

# 在 Python 中
import os
os.environ["AUDIOCRAFT_USE_XFORMERS"] = "0"
from audiocraft.models import MusicGen
```

## 模型加载问题

### 加载时内存不足

**错误**：加载模型时 `torch.cuda.OutOfMemoryError`

**解决方案**：
```python
# 使用更小的模型
model = MusicGen.get_pretrained('facebook/musicgen-small')

# 先强制在 CPU 上加载
import torch
device = "cpu"
model = MusicGen.get_pretrained('facebook/musicgen-small', device=device)
model = model.to("cuda")

# 用 HuggingFace 的 device_map
from transformers import MusicgenForConditionalGeneration
model = MusicgenForConditionalGeneration.from_pretrained(
    "facebook/musicgen-small",
    device_map="auto"
)
```

### 下载失败

**错误**：连接错误或下载不完整

**解决方案**：
```python
# 设置缓存目录
import os
os.environ["AUDIOCRAFT_CACHE_DIR"] = "/path/to/cache"

# 或用于 HuggingFace
os.environ["HF_HOME"] = "/path/to/hf_cache"

# 续传下载
from huggingface_hub import snapshot_download
snapshot_download("facebook/musicgen-small", resume_download=True)

# 使用本地文件
model = MusicGen.get_pretrained('/local/path/to/model')
```

### 模型类型选错

**错误**：为任务加载了错误的模型

**解决方案**：
```python
# 文生音乐：用 MusicGen
from audiocraft.models import MusicGen
model = MusicGen.get_pretrained('facebook/musicgen-medium')

# 文生音效：用 AudioGen
from audiocraft.models import AudioGen
model = AudioGen.get_pretrained('facebook/audiogen-medium')

# 旋律条件化：用 melody 变体
model = MusicGen.get_pretrained('facebook/musicgen-melody')

# 立体声：用 stereo 变体
model = MusicGen.get_pretrained('facebook/musicgen-stereo-medium')
```

## 生成问题

### 输出为空或静音

**问题**：生成的音频是静音或非常轻

**解决方案**：
```python
import torch

# 检查输出
wav = model.generate(["upbeat music"])
print(f"Shape: {wav.shape}")
print(f"Max amplitude: {wav.abs().max().item()}")
print(f"Mean amplitude: {wav.abs().mean().item()}")

# 如果太轻，做归一化
def normalize_audio(audio, target_db=-14.0):
    rms = torch.sqrt(torch.mean(audio ** 2))
    target_rms = 10 ** (target_db / 20)
    gain = target_rms / (rms + 1e-8)
    return audio * gain

wav_normalized = normalize_audio(wav)
```

### 输出质量差

**问题**：生成的音乐听起来很差或很多噪音

**解决方案**：
```python
# 使用更大的模型
model = MusicGen.get_pretrained('facebook/musicgen-large')

# 调整生成参数
model.set_generation_params(
    duration=15,
    top_k=250,          # 调高以增加多样性
    temperature=0.8,    # 调低以获得更聚焦的输出
    cfg_coef=4.0        # 调高以获得更好的文本贴合度
)

# 用更好的提示
# 差："music"
# 好："upbeat electronic dance music with synthesizers and punchy drums"

# 尝试 MultiBand Diffusion
from audiocraft.models import MultiBandDiffusion
mbd = MultiBandDiffusion.get_mbd_musicgen()
tokens = model.generate_tokens(["prompt"])
wav = mbd.tokens_to_wav(tokens)
```

### 生成太短

**问题**：音频比预期短

**解决方案**：
```python
# 检查时长设置
model.set_generation_params(duration=30)  # 在 generate 之前设置

# 在生成时验证
print(f"Duration setting: {model.generation_params}")

# 检查输出形状
wav = model.generate(["prompt"])
actual_duration = wav.shape[-1] / 32000
print(f"Actual duration: {actual_duration}s")

# 注意：最大时长通常是 30 秒
```

### 旋律条件化失败

**错误**：旋律条件化生成有问题

**解决方案**：
```python
import torchaudio
from audiocraft.models import MusicGen

# 加载旋律模型（不是基础模型）
model = MusicGen.get_pretrained('facebook/musicgen-melody')

# 加载并准备旋律
melody, sr = torchaudio.load("melody.wav")

# 如有需要，重采样到模型采样率
if sr != 32000:
    resampler = torchaudio.transforms.Resample(sr, 32000)
    melody = resampler(melody)

# 确保形状正确 [batch, channels, samples]
if melody.dim() == 1:
    melody = melody.unsqueeze(0).unsqueeze(0)
elif melody.dim() == 2:
    melody = melody.unsqueeze(0)

# 立体声转单声道
if melody.shape[1] > 1:
    melody = melody.mean(dim=1, keepdim=True)

# 用旋律生成
model.set_generation_params(duration=min(melody.shape[-1] / 32000, 30))
wav = model.generate_with_chroma(["piano cover"], melody, 32000)
```

## 内存问题

### CUDA 内存不足

**错误**：`torch.cuda.OutOfMemoryError: CUDA out of memory`

**解决方案**：
```python
import torch

# 在生成前清空缓存
torch.cuda.empty_cache()

# 使用更小的模型
model = MusicGen.get_pretrained('facebook/musicgen-small')

# 缩短时长
model.set_generation_params(duration=10)  # 而非 30

# 一次只生成一个
for prompt in prompts:
    wav = model.generate([prompt])
    save_audio(wav)
    torch.cuda.empty_cache()

# 对非常大的生成，用 CPU
model = MusicGen.get_pretrained('facebook/musicgen-small', device="cpu")
```

### 批处理时内存泄漏

**问题**：内存随时间增长

**解决方案**：
```python
import gc
import torch

def generate_with_cleanup(model, prompts):
    results = []

    for prompt in prompts:
        with torch.no_grad():
            wav = model.generate([prompt])
            results.append(wav.cpu())

        # 清理
        del wav
        gc.collect()
        torch.cuda.empty_cache()

    return results

# 用上下文管理器
with torch.inference_mode():
    wav = model.generate(["prompt"])
```

## 音频格式问题

### 采样率错误

**问题**：音频以错误的速度播放

**解决方案**：
```python
import torchaudio

# MusicGen 输出为 32kHz
sample_rate = 32000

# AudioGen 输出为 16kHz
sample_rate = 16000

# 保存时始终使用正确的采样率
torchaudio.save("output.wav", wav[0].cpu(), sample_rate=sample_rate)

# 如有需要做重采样
resampler = torchaudio.transforms.Resample(32000, 44100)
wav_resampled = resampler(wav)
```

### 立体声/单声道不匹配

**问题**：声道数不对

**解决方案**：
```python
# 检查模型类型
print(f"Audio channels: {wav.shape}")
# 单声道：[batch, 1, samples]
# 立体声：[batch, 2, samples]

# 单声道转立体声
if wav.shape[1] == 1:
    wav_stereo = wav.repeat(1, 2, 1)

# 立体声转单声道
if wav.shape[1] == 2:
    wav_mono = wav.mean(dim=1, keepdim=True)

# 立体声输出请使用立体声模型
model = MusicGen.get_pretrained('facebook/musicgen-stereo-medium')
```

### 削波和失真

**问题**：音频有削波或失真

**解决方案**：
```python
import torch

# 检查削波
max_val = wav.abs().max().item()
print(f"Max amplitude: {max_val}")

# 归一化以防止削波
if max_val > 1.0:
    wav = wav / max_val

# 施加软削波
def soft_clip(x, threshold=0.9):
    return torch.tanh(x / threshold) * threshold

wav_clipped = soft_clip(wav)

# 生成时降低 temperature
model.set_generation_params(temperature=0.7)  # 更可控
```

## HuggingFace Transformers 问题

### Processor 错误

**错误**：MusicgenProcessor 相关问题

**解决方案**：
```python
from transformers import AutoProcessor, MusicgenForConditionalGeneration

# 加载匹配的 processor 和模型
processor = AutoProcessor.from_pretrained("facebook/musicgen-small")
model = MusicgenForConditionalGeneration.from_pretrained("facebook/musicgen-small")

# 确保输入在同一设备上
inputs = processor(
    text=["prompt"],
    padding=True,
    return_tensors="pt"
).to("cuda")

# 检查 processor 配置
print(processor.tokenizer)
print(processor.feature_extractor)
```

### 生成参数错误

**错误**：无效的生成参数

**解决方案**：
```python
# HuggingFace 使用不同的参数名
audio_values = model.generate(
    **inputs,
    do_sample=True,           # 启用采样
    guidance_scale=3.0,       # CFG（不是 cfg_coef）
    max_new_tokens=256,       # token 上限（不是 duration）
    temperature=1.0
)

# 从时长换算 token
# 每秒约 50 个 token
duration_seconds = 10
max_tokens = duration_seconds * 50
audio_values = model.generate(**inputs, max_new_tokens=max_tokens)
```

## 性能问题

### 生成缓慢

**问题**：生成耗时过长

**解决方案**：
```python
# 使用更小的模型
model = MusicGen.get_pretrained('facebook/musicgen-small')

# 缩短时长
model.set_generation_params(duration=10)

# 使用 GPU
model.to("cuda")

# 如可用，启用 flash attention
# （需要兼容的硬件）

# 批量处理多个提示
prompts = ["prompt1", "prompt2", "prompt3"]
wav = model.generate(prompts)  # 单个批次比循环快

# 使用 compile（PyTorch 2.0+）
model.lm = torch.compile(model.lm)
```

### 回退到 CPU

**问题**：生成在 CPU 而非 GPU 上运行

**解决方案**：
```python
import torch

# 检查 CUDA 可用性
print(f"CUDA available: {torch.cuda.is_available()}")
print(f"CUDA device: {torch.cuda.get_device_name(0)}")

# 显式移到 GPU
model = MusicGen.get_pretrained('facebook/musicgen-small')
model.to("cuda")

# 验证模型所在设备
print(f"Model device: {next(model.lm.parameters()).device}")
```

## 常见错误信息

| 错误 | 原因 | 解决方案 |
|-------|-------|----------|
| `CUDA out of memory` | 模型太大 | 使用更小的模型，缩短时长 |
| `ffmpeg not found` | 未安装 FFmpeg | 安装 FFmpeg |
| `No module named 'audiocraft'` | 未安装 | `pip install audiocraft` |
| `RuntimeError: Expected 3D tensor` | 输入形状错误 | 检查张量维度 |
| `KeyError: 'melody'` | 旋律用了错误的模型 | 使用 musicgen-melody |
| `Sample rate mismatch` | 音频格式错误 | 重采样到模型采样率 |

## 获取帮助

1. **GitHub Issues**: https://github.com/facebookresearch/audiocraft/issues
2. **HuggingFace 论坛**: https://discuss.huggingface.co
3. **论文**: https://arxiv.org/abs/2306.05284

### 反馈问题

请包含：
- Python 版本
- PyTorch 版本
- CUDA 版本
- AudioCraft 版本：`pip show audiocraft`
- 完整的错误回溯
- 最小可复现代码
- 硬件（GPU 型号、显存）
