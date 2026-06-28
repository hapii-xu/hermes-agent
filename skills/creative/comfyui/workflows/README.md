# 示例工作流

这些是最常见任务的可直接运行的 API 格式入门工作流。一旦你安装（或通过云端访问）了列出的模型，它们即可用 `scripts/run_workflow.py` 运行。

| 文件 | 用途 | 所需模型 | 最低 VRAM |
|------|---------|-----------------|----------|
| `sd15_txt2img.json` | SD 1.5 文本到图像（512×512） | SD1.5 checkpoint，例如 `v1-5-pruned-emaonly.safetensors` | 4 GB |
| `sdxl_txt2img.json` | SDXL 文本到图像（1024×1024） | `sd_xl_base_1.0.safetensors` | 8 GB |
| `flux_dev_txt2img.json` | Flux Dev 文本到图像（1024×1024） | `flux1-dev.safetensors`、`t5xxl_fp16.safetensors`、`clip_l.safetensors`、`ae.safetensors` | 24 GB（或使用 `flux1-dev-fp8`） |
| `sdxl_img2img.json` | SDXL 图像到图像 | SDXL checkpoint | 8 GB |
| `sdxl_inpaint.json` | SDXL inpainting（图像 + 蒙版） | SDXL checkpoint | 8 GB |
| `upscale_4x.json` | 独立 4× ESRGAN 放大 | `4x-UltraSharp.pth`（或任何放大器） | 4 GB |
| `animatediff_video.json` | AnimateDiff 文本到视频（16 帧） | SD1.5 checkpoint、`mm_sd_v15_v2.ckpt` 运动模块 | 8 GB |
| `wan_video_t2v.json` | Wan 2.x 文本到视频（约 33 帧） | `wan2.2_t2v_1.3B_fp16.safetensors`、`umt5_xxl_fp16.safetensors`、`wan_2.1_vae.safetensors` | 24 GB |

## 快速开始

```bash
# 运行带提示注入的工作流
python3 ../scripts/run_workflow.py \
  --workflow sdxl_txt2img.json \
  --args '{"prompt": "majestic eagle in flight", "seed": 12345, "steps": 35}' \
  --output-dir ./out

# Img2img：先通过脚本的辅助函数上传输入图像
python3 ../scripts/run_workflow.py \
  --workflow sdxl_img2img.json \
  --input-image image=./photo.png \
  --args '{"prompt": "make it watercolor", "denoise": 0.6}' \
  --output-dir ./out

# 云端（设置一次 API key）
export COMFY_CLOUD_API_KEY="comfyui-..."
python3 ../scripts/run_workflow.py \
  --workflow flux_dev_txt2img.json \
  --args '{"prompt": "a fox in a misty forest"}' \
  --host https://cloud.comfy.org \
  --output-dir ./out

# 这个工作流中我可以调整什么？
python3 ../scripts/extract_schema.py sdxl_txt2img.json --summary-only

# 所有必需的模型/节点是否已安装？
python3 ../scripts/check_deps.py wan_video_t2v.json
```

## 注意事项

- **Inpaint 蒙版**：白色像素 = "重新生成此区域"，黑色 = 保留。
  ComfyUI 的 `LoadImageMask` 默认读取**红色通道**；将你的
  蒙版导出为单通道图像或正常的 RGB，其中 red==intensity。

- **img2img 中的去噪强度**：`0.0` = 输出与输入相同，
  `1.0` = 完全忽略输入。最佳点通常在 0.4–0.7。

- **Flux Dev** 在其基本形式下需要约 24 GB VRAM。`flux1-dev-fp8.safetensors`
  变体（已在 Comfy Cloud 上）将其大致减半。

- **视频工作流**可能需要很多分钟。本技能自动检测视频
  输出节点并将默认超时提升至 900 秒。用 `--timeout 1800` 覆盖。

- 这些 JSON 文件特意采用**API 格式**（顶层键是带 `class_type` 的节点 ID
  ），而非编辑器格式。要在 ComfyUI 的 Web UI 中打开它们进行
  可视化编辑，使用 `Workflow → Load (API Format)` 或 `Workflow → Open` 并
  按照提示操作。

## 云端 vs 本地模型名

Comfy Cloud 预装的 checkpoint 有时带有 `-fp16` 后缀
（`v1-5-pruned-emaonly-fp16.safetensors`），而规范的本地下载
保留原始名（`v1-5-pruned-emaonly.safetensors`）。示例
工作流使用本地规范名。在云端运行时，用以下方式覆盖：

```bash
python3 ../scripts/run_workflow.py \
  --workflow sd15_txt2img.json \
  --args '{"ckpt_name": "v1-5-pruned-emaonly-fp16.safetensors", "prompt": "..."}' \
  --host https://cloud.comfy.org
```

`ckpt_name`、`vae_name`、`lora_name`、`unet_name` 等都由 `extract_schema.py` 暴露为可控参数 — 用 `comfy model list`（本地）或 `curl /api/experiment/models/checkpoints`（云端）发现已安装的内容。
