---
name: songsee
description: "通过 CLI 生成音频频谱图/特征（mel、chroma、MFCC）。"
version: 1.0.0
author: community
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Audio, Visualization, Spectrogram, Music, Analysis]
    homepage: https://github.com/steipete/songsee
prerequisites:
  commands: [songsee]
---

# songsee

从音频文件生成频谱图和多面板音频特征可视化。

## 前置条件

需要 [Go](https://go.dev/doc/install)：
```bash
go install github.com/steipete/songsee/cmd/songsee@latest
```

可选：处理 WAV/MP3 以外的格式需要 `ffmpeg`。

## 快速开始

```bash
# 基础频谱图
songsee track.mp3

# 保存到指定文件
songsee track.mp3 -o spectrogram.png

# 多面板可视化网格
songsee track.mp3 --viz spectrogram,mel,chroma,hpss,selfsim,loudness,tempogram,mfcc,flux

# 时间切片（从 12.5 秒开始，持续 8 秒）
songsee track.mp3 --start 12.5 --duration 8 -o slice.jpg

# 从 stdin 读取
cat track.mp3 | songsee - --format png -o out.png
```

## 可视化类型

使用 `--viz` 并以逗号分隔多个值：

| 类型 | 说明 |
|------|-------------|
| `spectrogram` | 标准频率频谱图 |
| `mel` | Mel 尺度频谱图 |
| `chroma` | 音高类别分布 |
| `hpss` | 谐波/打击声分离 |
| `selfsim` | 自相似矩阵 |
| `loudness` | 随时间变化的响度 |
| `tempogram` | 节拍估计 |
| `mfcc` | Mel 频率倒谱系数 |
| `flux` | 频谱通量（用于 onset 检测） |

多个 `--viz` 类型会以网格形式渲染到同一张图片中。

## 常用标志

| 标志 | 说明 |
|------|-------------|
| `--viz` | 可视化类型（逗号分隔） |
| `--style` | 配色方案：`classic`、`magma`、`inferno`、`viridis`、`gray` |
| `--width` / `--height` | 输出图片尺寸 |
| `--window` / `--hop` | FFT 窗口和步长 |
| `--min-freq` / `--max-freq` | 频率范围筛选 |
| `--start` / `--duration` | 音频的时间切片 |
| `--format` | 输出格式：`jpg` 或 `png` |
| `-o` | 输出文件路径 |

## 注意事项

- WAV 和 MP3 原生解码；其他格式需要 `ffmpeg`
- 输出的图片可通过 `vision_analyze` 检视，用于自动化音频分析
- 适用于对比音频输出、调试合成或记录音频处理流程
