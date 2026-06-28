---
name: heartmula
description: "HeartMuLa：基于歌词 + 标签的类 Suno 作曲生成。"
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [music, audio, generation, ai, heartmula, heartcodec, lyrics, songs]
    related_skills: [audiocraft]
---

# HeartMuLa —— 开源音乐生成

## 概览
HeartMuLa 是一系列开源音乐基础模型（Apache-2.0），能根据歌词和标签生成音乐，并支持多语言。可从歌词 + 标签生成完整的歌曲，可作为开源版的 Suno 替代品。包含：
- **HeartMuLa** —— 音乐语言模型（3B/7B），根据歌词 + 标签生成
- **HeartCodec** —— 12.5Hz 音乐编解码器，用于高保真音频重建
- **HeartTranscriptor** —— 基于 Whisper 的歌词转录
- **HeartCLAP** —— 音频-文本对齐模型

## 何时使用
- 用户想从文本描述生成音乐/歌曲
- 用户想要开源的 Suno 替代品
- 用户想要本地/离线音乐生成
- 用户询问 HeartMuLa、heartlib 或 AI 音乐生成

## 硬件要求
- **最低**：8GB VRAM，配合 `--lazy_load true`（按顺序加载/卸载模型）
- **推荐**：16GB+ VRAM，可在单 GPU 上舒适运行
- **多 GPU**：使用 `--mula_device cuda:0 --codec_device cuda:1` 在多块 GPU 间分担
- 3B 模型启用 lazy_load 时峰值约 6.2GB VRAM

## 安装步骤

### 1. 克隆仓库
```bash
cd ~/  # 或你想要的目录
git clone https://github.com/HeartMuLa/heartlib.git
cd heartlib
```

### 2. 创建虚拟环境（需要 Python 3.10）
```bash
uv venv --python 3.10 .venv
. .venv/bin/activate
uv pip install -e .
```

### 3. 修复依赖兼容性问题

**重要**：截至 2026 年 2 月，锁定的依赖与较新的包存在冲突。请应用以下修复：

```bash
# 升级 datasets（旧版本与当前 pyarrow 不兼容）
uv pip install --upgrade datasets

# 升级 transformers（huggingface-hub 1.x 兼容性所需）
uv pip install --upgrade transformers
```

### 4. 修改源码（transformers 5.x 必需）

**补丁 1 —— RoPE 缓存修复**，位于 `src/heartlib/heartmula/modeling_heartmula.py`：

在 `HeartMuLa` 类的 `setup_caches` 方法中，于 `reset_caches` 的 try/except 块之后、`with device:` 块之前，添加 RoPE 重新初始化：

```python
# 重新初始化在 meta-device 加载时被跳过的 RoPE 缓存
from torchtune.models.llama3_1._position_embeddings import Llama3ScaledRoPE
for module in self.modules():
    if isinstance(module, Llama3ScaledRoPE) and not module.is_cache_built:
        module.rope_init()
        module.to(device)
```

**原因**：`from_pretrained` 会先在 meta device 上创建模型；`Llama3ScaledRoPE.rope_init()` 在 meta 张量上会跳过缓存构建，且在权重加载到真实设备后也不会重建。

**补丁 2 —— HeartCodec 加载修复**，位于 `src/heartlib/pipelines/music_generation.py`：

为所有 `HeartCodec.from_pretrained()` 调用添加 `ignore_mismatched_sizes=True`（共有 2 处：`__init__` 中的即时加载，以及 `codec` 属性中的懒加载）。

**原因**：VQ codebook 的 `initted` 缓冲区在检查点中形状为 `[1]`，而模型中为 `[]`。数据相同，只是标量与 0 维张量之别。可安全忽略。

### 5. 下载模型检查点
```bash
cd heartlib  # 项目根目录
hf download --local-dir './ckpt' 'HeartMuLa/HeartMuLaGen'
hf download --local-dir './ckpt/HeartMuLa-oss-3B' 'HeartMuLa/HeartMuLa-oss-3B-happy-new-year'
hf download --local-dir './ckpt/HeartCodec-oss' 'HeartMuLa/HeartCodec-oss-20260123'
```

这 3 个可并行下载。总大小为数 GB。

## GPU / CUDA

HeartMuLa 默认使用 CUDA（`--mula_device cuda --codec_device cuda`）。如果用户有安装了 PyTorch CUDA 支持的 NVIDIA GPU，则无需额外配置。

- 已安装的 `torch==2.4.1` 自带 CUDA 12.1 支持
- `torchtune` 可能报告版本 `0.4.0+cpu` —— 这只是包元数据，它仍会通过 PyTorch 使用 CUDA
- 要验证是否在使用 GPU，可在输出中查找 "CUDA memory" 相关行（例如 "CUDA memory before unloading: 6.20 GB"）
- **没有 GPU？** 可用 `--mula_device cpu --codec_device cpu` 在 CPU 上运行，但生成会**极其缓慢**（单首歌可能需要 30-60+ 分钟，而 GPU 上约 4 分钟）。CPU 模式还需要可观的内存（空闲约 12GB+）。如果用户没有 NVIDIA GPU，建议使用云 GPU 服务（Google Colab 免费档的 T4、Lambda Labs 等），或改用 https://heartmula.github.io/ 上的在线演示。

## 用法

### 基础生成
```bash
cd heartlib
. .venv/bin/activate
python ./examples/run_music_generation.py \
  --model_path=./ckpt \
  --version="3B" \
  --lyrics="./assets/lyrics.txt" \
  --tags="./assets/tags.txt" \
  --save_path="./assets/output.mp3" \
  --lazy_load true
```

### 输入格式

**标签（Tags）**（逗号分隔，无空格）：
```
piano,happy,wedding,synthesizer,romantic
```
或
```
rock,energetic,guitar,drums,male-vocal
```

**歌词（Lyrics）**（使用方括号包裹的结构标签）：
```
[Intro]

[Verse]
Your lyrics here...

[Chorus]
Chorus lyrics...

[Bridge]
Bridge lyrics...

[Outro]
```

### 关键参数
| 参数 | 默认值 | 说明 |
|-----------|---------|-------------|
| `--max_audio_length_ms` | 240000 | 最大时长（毫秒，240s = 4 分钟） |
| `--topk` | 50 | Top-k 采样 |
| `--temperature` | 1.0 | 采样温度 |
| `--cfg_scale` | 1.5 | 无分类器引导（classifier-free guidance）强度 |
| `--lazy_load` | false | 按需加载/卸载模型（节省 VRAM） |
| `--mula_dtype` | bfloat16 | HeartMuLa 的数据类型（推荐 bf16） |
| `--codec_dtype` | float32 | HeartCodec 的数据类型（为质量推荐 fp32） |

### 性能
- RTF（实时系数，Real-Time Factor）≈ 1.0 —— 一首 4 分钟的歌大约需要 4 分钟生成
- 输出：MP3，48kHz 立体声，128kbps

## 常见陷阱
1. **不要对 HeartCodec 使用 bf16** —— 会降低音频质量。请使用 fp32（默认）。
2. **标签可能被忽略** —— 已知问题（#90）。歌词倾向于占主导；可尝试调整标签顺序。
3. **Triton 在 macOS 上不可用** —— 仅 Linux/CUDA 支持 GPU 加速。
4. 上游 issue 中报告过 **RTX 5080 不兼容**。
5. 依赖锁定的冲突需要按上文所述手动升级和打补丁。

## 链接
- 仓库：https://github.com/HeartMuLa/heartlib
- 模型：https://huggingface.co/HeartMuLa
- 论文：https://arxiv.org/abs/2601.10547
- 许可证：Apache-2.0
