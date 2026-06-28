---
name: comfyui
description: "使用 ComfyUI 生成图像、视频和音频 — 安装、启动、管理节点/模型、通过参数注入运行工作流。使用官方 comfy-cli 进行生命周期管理，使用直接 REST/WebSocket API 进行执行。"
version: 5.1.0
author: [kshitijk4poor, alt-glitch, purzbeats]
license: MIT
platforms: [macos, linux, windows]
compatibility: "需要 ComfyUI（本地、Comfy Desktop 或 Comfy Cloud）和 comfy-cli（由 setup 脚本通过 pipx/uvx 自动安装）。"
prerequisites:
  commands: ["python3"]
setup:
  help: "先运行 scripts/hardware_check.py 来决定本地还是 Comfy Cloud；然后 scripts/comfyui_setup.sh 会自动在本地安装（或使用 Cloud API key 访问 platform.comfy.org）。"
metadata:
  hermes:
    tags:
      - comfyui
      - image-generation
      - stable-diffusion
      - flux
      - sd3
      - wan-video
      - hunyuan-video
      - creative
      - generative-ai
      - video-generation
    related_skills: [stable-diffusion-image-generation, image_gen]
    category: creative
---

# ComfyUI

通过 ComfyUI 生成图像、视频、音频和 3D 内容，使用官方 `comfy-cli` 进行设置/生命周期管理，使用直接 REST/WebSocket API 进行工作流执行。

## 本技能包含的内容

**参考文档（`references/`）：**

- `official-cli.md` — 每条 `comfy ...` 命令及其标志
- `rest-api.md` — REST + WebSocket 端点（本地 + 云端）、负载 schema
- `workflow-format.md` — API 格式 JSON、常见节点类型、参数映射
- `template-integrity.md` — 将 `comfyui-workflow-templates` 从编辑器格式转换为 API 格式：Reroute 绕过、点分动态输入键（`values.a`、`resize_type.width`）、云端特性（302 重定向、免费层 1 个并发任务、1080p VRAM 上限）、Discord 兼容的 ffmpeg 拼接。
  由 [@purzbeats](https://github.com/purzbeats) 编写。当你从官方模板开始时请加载此文档。

**脚本（`scripts/`）：**

| 脚本 | 用途 |
|--------|---------|
| `_common.py` | 共享的 HTTP、云端路由、节点目录（不要直接运行） |
| `hardware_check.py` | 探测 GPU/VRAM/磁盘 → 推荐本地还是 Comfy Cloud |
| `comfyui_setup.sh` | 硬件检查 + comfy-cli + ComfyUI 安装 + 启动 + 验证 |
| `extract_schema.py` | 读取工作流 → 列出可控参数 + 模型依赖 |
| `check_deps.py` | 对照运行中的服务器检查工作流 → 列出缺失的节点/模型 |
| `auto_fix_deps.py` | 运行 check_deps 然后 `comfy node install` / `comfy model download` |
| `run_workflow.py` | 注入参数、提交、监控、下载输出（HTTP 或 WS） |
| `run_batch.py` | 以扫描方式提交工作流 N 次，并行至你的套餐上限 |
| `ws_monitor.py` | 用于执行中任务的实时 WebSocket 查看器（实时进度） |
| `health_check.py` | 验证清单运行器 — comfy-cli + 服务器 + 模型 + 冒烟测试 |
| `fetch_logs.py` | 拉取给定 prompt_id 的 traceback / 状态消息 |

**示例工作流（`workflows/`）：** SD 1.5、SDXL、Flux Dev、SDXL img2img、SDXL inpaint、ESRGAN 放大、AnimateDiff 视频、Wan T2V。参见 `workflows/README.md`。

## 何时使用

- 用户要求使用 Stable Diffusion、SDXL、Flux、SD3 等生成图像
- 用户想运行特定的 ComfyUI 工作流文件
- 用户想串联生成步骤（txt2img → 放大 → 人脸修复）
- 用户需要 ControlNet、inpainting、img2img 或其他高级流水线
- 用户要求管理 ComfyUI 队列、检查模型或安装自定义节点
- 用户想通过 AnimateDiff、Hunyuan、Wan、AudioCraft 等生成视频/音频/3D

## 架构：两层

```
┌─────────────────────────────────────────────────────┐
│ Layer 1: comfy-cli (official lifecycle tool)        │
│   Setup, server lifecycle, custom nodes, models     │
│   → comfy install / launch / stop / node / model    │
└─────────────────────────┬───────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────┐
│ Layer 2: REST/WebSocket API + skill scripts         │
│   Workflow execution, param injection, monitoring   │
│   POST /api/prompt, GET /api/view, WS /ws           │
│   → run_workflow.py, run_batch.py, ws_monitor.py    │
└─────────────────────────────────────────────────────┘
```

**为什么分两层？** 官方 CLI 在安装和服务器管理方面表现出色，但工作流执行支持很少。REST/WS API 填补了这一空白 — 脚本处理参数注入、执行监控和输出下载，这些是 CLI 不做的。

## 快速开始

### 检测环境

```bash
# 有哪些可用？
command -v comfy >/dev/null 2>&1 && echo "comfy-cli: installed"
curl -s http://127.0.0.1:8188/system_stats 2>/dev/null && echo "server: running"

# 这台机器能在本地运行 ComfyUI 吗？（GPU/VRAM/磁盘检查）
python3 scripts/hardware_check.py
```

如果什么都没安装，请参阅下面的**设置与入门** — 但始终先运行硬件检查。

### 一行式健康检查

```bash
python3 scripts/health_check.py
# → JSON：comfy_cli 在 PATH 上？服务器可达？至少一个 checkpoint？冒烟测试通过？
```

## 核心工作流

### 步骤 1：获取 API 格式的工作流 JSON

工作流必须是 API 格式（每个节点有 `class_type`）。它们来自：

- ComfyUI web UI → **Workflow → Export (API)**（较新的 UI）或
  旧版的 "Save (API Format)" 按钮（较旧的 UI）
- 本技能的 `workflows/` 目录（可直接运行的示例）
- 社区下载（civitai、Reddit、Discord）— 通常是编辑器格式，
  必须加载到 ComfyUI 中然后重新导出

编辑器格式（顶层 `nodes` 和 `links` 数组）**不能直接执行**。脚本会检测到这一点并告诉你重新导出。

### 步骤 2：查看可控内容

```bash
python3 scripts/extract_schema.py workflow_api.json --summary-only
# → {"parameter_count": 12, "has_negative_prompt": true, "has_seed": true, ...}

python3 scripts/extract_schema.py workflow_api.json
# → 完整 schema，包含参数、模型依赖、embedding 引用
```

### 步骤 3：带参数运行

```bash
# 本地（默认为 http://127.0.0.1:8188）
python3 scripts/run_workflow.py \
  --workflow workflow_api.json \
  --args '{"prompt": "a beautiful sunset over mountains", "seed": -1, "steps": 30}' \
  --output-dir ./outputs

# 云端（导出一次 API key；自动使用正确的 /api 路由）
export COMFY_CLOUD_API_KEY="comfyui-..."
python3 scripts/run_workflow.py \
  --workflow workflow_api.json \
  --args '{"prompt": "..."}' \
  --host https://cloud.comfy.org \
  --output-dir ./outputs

# 通过 WebSocket 实时进度（需要 `pip install websocket-client`）
python3 scripts/run_workflow.py \
  --workflow flux_dev.json \
  --args '{"prompt": "..."}' \
  --ws

# img2img / inpaint：传递 --input-image 自动上传 + 引用
python3 scripts/run_workflow.py \
  --workflow sdxl_img2img.json \
  --input-image image=./photo.png \
  --args '{"prompt": "make it watercolor", "denoise": 0.6}'

# 批量 / 扫描：8 个随机种子，并行至云套餐上限
python3 scripts/run_batch.py \
  --workflow sdxl.json \
  --args '{"prompt": "abstract"}' \
  --count 8 --randomize-seed --parallel 3 \
  --output-dir ./outputs/batch
```

`seed` 为 `-1`（或使用 `--randomize-seed` 省略它）会在每次运行时生成一个新的随机种子。

### 步骤 4：展示结果

脚本向 stdout 输出描述每个输出文件的 JSON：

```json
{
  "status": "success",
  "prompt_id": "abc-123",
  "outputs": [
    {"file": "./outputs/sdxl_00001_.png", "node_id": "9",
     "type": "image", "filename": "sdxl_00001_.png"}
  ]
}
```

## 决策树

| 用户说 | 工具 | 命令 |
|-----------|------|---------|
| **生命周期（使用 comfy-cli）** | | |
| "install ComfyUI" | comfy-cli | `bash scripts/comfyui_setup.sh` |
| "start ComfyUI" | comfy-cli | `comfy launch --background` |
| "stop ComfyUI" | comfy-cli | `comfy stop` |
| "install X node" | comfy-cli | `comfy node install <name>` |
| "download X model" | comfy-cli | `comfy model download --url <url> --relative-path models/checkpoints` |
| "list installed models" | comfy-cli | `comfy model list` |
| "list installed nodes" | comfy-cli | `comfy node show installed` |
| **执行（使用脚本）** | | |
| "is everything ready?" | script | `health_check.py`（可选带 `--workflow X --smoke-test`） |
| "what can I change in this workflow?" | script | `extract_schema.py W.json` |
| "check if W's deps are met" | script | `check_deps.py W.json` |
| "fix missing deps" | script | `auto_fix_deps.py W.json` |
| "generate an image" | script | `run_workflow.py --workflow W --args '{...}'` |
| "use this image"（img2img） | script | `run_workflow.py --input-image image=./x.png ...` |
| "8 variations with random seeds" | script | `run_batch.py --count 8 --randomize-seed ...` |
| "show me live progress" | script | `ws_monitor.py --prompt-id <id>` |
| "fetch the error from job X" | script | `fetch_logs.py <prompt_id>` |
| **直接 REST** | | |
| "what's in the queue?" | REST | `curl http://HOST:8188/queue`（本地）或 `--host https://cloud.comfy.org` |
| "cancel that" | REST | `curl -X POST http://HOST:8188/interrupt` |
| "free GPU memory" | REST | `curl -X POST http://HOST:8188/free` |

## 设置与入门

当用户要求设置 ComfyUI 时，**第一件事是询问他们想要 Comfy Cloud（托管、零安装、API key）还是本地（在其机器上安装 ComfyUI）**。在他们回答之前不要开始运行安装命令或硬件检查。

**官方文档：** https://docs.comfy.org/installation
**CLI 文档：** https://docs.comfy.org/comfy-cli/getting-started
**云端文档：** https://docs.comfy.org/get_started/cloud
**云端 API：** https://docs.comfy.org/development/cloud/overview

### 步骤 0：询问本地还是云端（始终第一）

建议的话术：

> "你想在本地机器上运行 ComfyUI，还是使用 Comfy Cloud？
>
> - **Comfy Cloud** — 托管在 RTX 6000 Pro GPU 上，所有常用模型预装，零设置。需要 API key（实际运行工作流需要付费订阅；免费层为只读）。如果你没有 capable 的 GPU 则最佳。
> - **本地** — 免费，但你的机器必须满足硬件要求：
>   - 带 **≥6 GB VRAM** 的 NVIDIA GPU（SDXL 需 ≥8 GB，Flux/视频需 ≥12 GB），或
>   - 支持 ROCm 的 AMD GPU（Linux），或
>   - Apple Silicon Mac（M1+），**≥16 GB 统一内存**（推荐 ≥32 GB）。
>   - Intel Mac 和没有 GPU 的机器将无法工作 — 改用 Cloud。
>
> 你想要哪个？"

路由：

- **云端** → 跳到**路径 A**。
- **本地** → 先运行硬件检查，然后根据结论从路径 B–E 中选择。
- **不确定** → 运行硬件检查让结论决定。

### 步骤 1：验证硬件（仅当用户选择本地时）

```bash
python3 scripts/hardware_check.py --json
# 可选：还探测 `torch` 获取实际的 CUDA/MPS：
python3 scripts/hardware_check.py --json --check-pytorch
```

| 结论    | 含义                                                       | 动作 |
|------------|---------------------------------------------------------------|--------|
| `ok`       | ≥8 GB VRAM（独立）或 ≥32 GB 统一（Apple Silicon）       | 本地安装 — 使用报告中的 `comfy_cli_flag` |
| `marginal` | SD1.5 可用；SDXL 紧张；Flux/视频不太可能                  | 本地可用于轻量工作流，否则**路径 A（云端）** |
| `cloud`    | 无可用 GPU、<6 GB VRAM、<16 GB Apple 统一、Intel Mac、Rosetta Python | **切换到云端**，除非用户明确强制本地 |

脚本还会提示 `wsl: true`（带 NVIDIA 直通的 WSL2）和 `rosetta: true`（Apple Silicon 上的 x86_64 Python — 必须重新安装为 ARM64）。

如果结论是 `cloud` 但用户想要本地，不要默默地继续。原样显示 `notes` 数组并询问他们是想 (a) 切换到云端还是 (b) 强制本地安装（在现代模型上会 OOM 或慢到不可用）。

### 选择安装路径

先使用硬件检查。下表是用户已经告诉你硬件时的备选方案：

| 情况 | 推荐路径 |
|-----------|------------------|
| 硬件检查的 `verdict: cloud` | **路径 A：Comfy Cloud** |
| 无 GPU / 想不承诺就试用 | **路径 A：Comfy Cloud** |
| Windows + NVIDIA + 非技术用户 | **路径 B：ComfyUI Desktop** |
| Windows + NVIDIA + 技术用户 | **路径 C：Portable** 或 **路径 D：comfy-cli** |
| Linux + 任何 GPU | **路径 D：comfy-cli**（最简单） |
| macOS + Apple Silicon | **路径 B：Desktop** 或 **路径 D：comfy-cli** |
| 无头 / 服务器 / CI / 智能体 | **路径 D：comfy-cli** |

对于完全自动化的路径（硬件检查 → 安装 → 启动 → 验证）：

```bash
bash scripts/comfyui_setup.sh
# 或带覆盖参数：
bash scripts/comfyui_setup.sh --m-series --port=8190 --workspace=/data/comfy
```

它在内部运行 `hardware_check.py`，当结论为 `cloud` 时拒绝本地安装（除非 `--force-cloud-override`），选择正确的 `comfy-cli` 标志，并优先使用 `pipx`/`uvx` 而非全局 `pip` 以避免污染系统 Python。

---

### 路径 A：Comfy Cloud（无本地安装）

适用于没有 capable GPU 或想要零设置的用户。托管在 RTX 6000 Pro 上。

**文档：** https://docs.comfy.org/get_started/cloud

1. 在 https://comfy.org/cloud 注册
2. 在 https://platform.comfy.org/login 生成 API key
3. 设置 key：
   ```bash
   export COMFY_CLOUD_API_KEY="comfyui-xxxxxxxxxxxx"
   ```
4. 运行工作流：
   ```bash
   python3 scripts/run_workflow.py \
     --workflow workflows/flux_dev_txt2img.json \
     --args '{"prompt": "..."}' \
     --host https://cloud.comfy.org \
     --output-dir ./outputs
   ```

**定价：** https://www.comfy.org/cloud/pricing
**并发任务：** Free/Standard 为 1，Creator 为 3，Pro 为 5。免费层**无法通过 API 运行工作流** — 只能浏览模型。`/api/prompt`、`/api/upload/*`、`/api/view` 等需要付费订阅。

---

### 路径 B：ComfyUI Desktop（Windows / macOS）

面向非技术用户的一键安装程序。目前为 Beta。

**文档：** https://docs.comfy.org/installation/desktop
- **Windows（NVIDIA）：** https://download.comfy.org/windows/nsis/x64
- **macOS（Apple Silicon）：** https://comfy.org

Linux **不支持** Desktop — 使用路径 D。

---

### 路径 C：ComfyUI Portable（仅 Windows）

**文档：** https://docs.comfy.org/installation/comfyui_portable_windows

从 https://github.com/comfyanonymous/ComfyUI/releases 下载，解压，运行 `run_nvidia_gpu.bat`。通过 `update/update_comfyui_stable.bat` 更新。

---

### 路径 D：comfy-cli（所有平台 — 推荐用于智能体）

官方 CLI 是无头/自动化设置的最佳路径。

**文档：** https://docs.comfy.org/comfy-cli/getting-started

#### 安装 comfy-cli

```bash
# 推荐：
pipx install comfy-cli
# 或使用 uvx 无需安装：
uvx --from comfy-cli comfy --help
# 或（如果 pipx/uvx 不可用）：
pip install --user comfy-cli
```

非交互式禁用分析：
```bash
comfy --skip-prompt tracking disable
```

#### 安装 ComfyUI

```bash
comfy --skip-prompt install --nvidia              # NVIDIA (CUDA)
comfy --skip-prompt install --amd                 # AMD (ROCm, Linux)
comfy --skip-prompt install --m-series            # Apple Silicon (MPS)
comfy --skip-prompt install --cpu                 # 仅 CPU（慢）
comfy --skip-prompt install --nvidia --fast-deps  # 基于 uv 的依赖解析
```

默认位置：`~/comfy/ComfyUI`（Linux），`~/Documents/comfy/ComfyUI`（macOS/Win）。用 `comfy --workspace /custom/path install` 覆盖。

#### 启动 / 验证

```bash
comfy launch --background                       # 后台守护进程在 :8188
comfy launch -- --listen 0.0.0.0 --port 8190    # LAN 可访问的自定义端口
curl -s http://127.0.0.1:8188/system_stats      # 健康检查
```

---

### 路径 E：手动安装（高级 / 不支持的硬件）

适用于 Ascend NPU、Cambricon MLU、Intel Arc 或其他不支持的硬件。

**文档：** https://docs.comfy.org/installation/manual_install

```bash
git clone https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI
pip install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu130
pip install -r requirements.txt
python main.py
```

---

### 安装后：下载模型

```bash
# SDXL（通用，约 6.5 GB）
comfy model download \
  --url "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors" \
  --relative-path models/checkpoints

# SD 1.5（更轻量，约 4 GB，适合 6 GB 显卡）
comfy model download \
  --url "https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5/resolve/main/v1-5-pruned-emaonly.safetensors" \
  --relative-path models/checkpoints

# Flux Dev fp8（更小的变体，约 12 GB）
comfy model download \
  --url "https://huggingface.co/Comfy-Org/flux1-dev/resolve/main/flux1-dev-fp8.safetensors" \
  --relative-path models/checkpoints

# CivitAI（先设置 token）：
comfy model download \
  --url "https://civitai.com/api/download/models/128713" \
  --relative-path models/checkpoints \
  --set-civitai-api-token "YOUR_TOKEN"
```

列出已安装：`comfy model list`。

### 安装后：安装自定义节点

```bash
comfy node install comfyui-impact-pack             # 流行的实用工具包
comfy node install comfyui-animatediff-evolved     # 视频生成
comfy node install comfyui-controlnet-aux          # ControlNet 预处理器
comfy node install comfyui-essentials              # 常用辅助工具
comfy node update all
comfy node install-deps --workflow=workflow.json   # 安装工作流所需的一切
```

### 安装后：验证

```bash
python3 scripts/health_check.py
# → comfy_cli 在 PATH 上？服务器可达？checkpoints？冒烟测试？

python3 scripts/check_deps.py my_workflow.json
# → 这个工作流的 nodes/models/embeddings 是否已安装？

python3 scripts/run_workflow.py \
  --workflow workflows/sd15_txt2img.json \
  --args '{"prompt": "test", "steps": 4}' \
  --output-dir ./test-outputs
```

## 图像上传（img2img / Inpainting）

最简单的方式是使用 `run_workflow.py` 的 `--input-image`：

```bash
python3 scripts/run_workflow.py \
  --workflow workflows/sdxl_img2img.json \
  --input-image image=./photo.png \
  --args '{"prompt": "make it cyberpunk", "denoise": 0.6}'
```

该标志上传 `photo.png`，然后将其服务器端文件名注入到任何名为 `image` 的 schema 参数中。对于 inpainting，两者都传：

```bash
python3 scripts/run_workflow.py \
  --workflow workflows/sdxl_inpaint.json \
  --input-image image=./photo.png \
  --input-image mask_image=./mask.png \
  --args '{"prompt": "fill with flowers"}'
```

通过 REST 手动上传：
```bash
curl -X POST "http://127.0.0.1:8188/upload/image" \
  -F "image=@photo.png" -F "type=input" -F "overwrite=true"
# 返回：{"name": "photo.png", "subfolder": "", "type": "input"}

# 云端等价：
curl -X POST "https://cloud.comfy.org/api/upload/image" \
  -H "X-API-Key: $COMFY_CLOUD_API_KEY" \
  -F "image=@photo.png" -F "type=input" -F "overwrite=true"
```

## 云端细节

- **Base URL：** `https://cloud.comfy.org`
- **认证：** `X-API-Key` 头（或 WebSocket 用 `?token=KEY`）
- **API key：** 设置一次 `$COMFY_CLOUD_API_KEY`，脚本会自动拾取
- **输出下载：** `/api/view` 返回 302 到签名 URL；脚本会跟随它并在从存储后端获取之前剥离 `X-API-Key`（不要将 API key 泄漏给 S3/CloudFront）。
- **与本地 ComfyUI 的端点差异：**
  - `/api/object_info`、`/api/queue`、`/api/userdata` — **免费层 403**；仅付费。
  - `/history` 在云端重命名为 `/history_v2`（脚本自动路由）。
  - `/models/<folder>` 在云端重命名为 `/experiment/models/<folder>`（脚本自动路由）。
  - WebSocket 中的 `clientId` 目前被忽略 — 用户的所有连接接收相同的广播。在客户端按 `prompt_id` 过滤。
  - 上传时接受 `subfolder` 但被忽略 — 云端是扁平命名空间。
- **并发任务：** Free/Standard：1，Creator：3，Pro：5。额外的自动排队。使用 `run_batch.py --parallel N` 来饱和你的套餐。

## 队列与系统管理

```bash
# 本地
curl -s http://127.0.0.1:8188/queue | python3 -m json.tool
curl -X POST http://127.0.0.1:8188/queue -d '{"clear": true}'    # 取消待处理的
curl -X POST http://127.0.0.1:8188/interrupt                      # 取消运行中的
curl -X POST http://127.0.0.1:8188/free \
  -H "Content-Type: application/json" \
  -d '{"unload_models": true, "free_memory": true}'

# 云端 — /api/ 下的相同路径，加上：
python3 scripts/fetch_logs.py --tail-queue --host https://cloud.comfy.org
```

## 陷阱

1. **需要 API 格式** — 每个脚本和 `/api/prompt` 端点都期望 API 格式的工作流 JSON。脚本检测编辑器格式（顶层 `nodes` 和 `links` 数组）并告诉你通过 "Workflow → Export (API)"（较新的 UI）或 "Save (API Format)"（较旧的 UI）重新导出。

2. **服务器必须运行** — 所有执行都需要运行中的服务器。`comfy launch --background` 启动一个。用 `curl http://127.0.0.1:8188/system_stats` 验证。

3. **模型名称必须精确** — 区分大小写，包括文件扩展名。`check_deps.py` 做模糊匹配（带/不带扩展名和文件夹前缀），但工作流本身必须使用规范名称。使用 `comfy model list` 发现已安装的内容。

4. **缺少自定义节点** — "class_type not found" 意味着所需的节点未安装。`check_deps.py` 报告要安装哪个包；`auto_fix_deps.py` 为你运行安装。

5. **工作目录** — `comfy-cli` 自动检测 ComfyUI 工作区。如果命令以 "no workspace found" 失败，使用 `comfy --workspace /path/to/ComfyUI <command>` 或 `comfy set-default /path/to/ComfyUI`。

6. **云端免费层 API 限制** — `/api/prompt`、`/api/view`、`/api/upload/*`、`/api/object_info` 在免费账户上都返回 403。`health_check.py` 和 `check_deps.py` 优雅地处理这一点并显示清晰的消息。

7. **视频/音频工作流的超时** — 当输出节点是 `VHS_VideoCombine`、`SaveVideo` 等时自动检测；默认从 300 秒跳到 900 秒。用 `--timeout 1800` 显式覆盖。

8. **输出文件名中的路径遍历** — 服务器提供的文件名通过 `safe_path_join` 传递，拒绝任何逃出 `--output-dir` 的内容。保持此保护开启 — 带自定义保存节点的工作流可能产生任意路径。

9. **工作流 JSON 即任意代码** — 自定义节点运行 Python，因此提交未知工作流具有与 `eval` 相同的信任特征。在运行前检查来自不可信来源的工作流。

10. **自动随机种子** — 在 `--args` 中传 `seed: -1`（或使用 `--randomize-seed` 并省略种子）以在每次运行获得新种子。实际种子记录到 stderr。

11. **`tracking` 提示** — 首次运行 `comfy` 可能提示分析。使用 `comfy --skip-prompt tracking disable` 非交互式跳过。`comfyui_setup.sh` 为你执行此操作。

## 验证清单

使用 `python3 scripts/health_check.py` 一次运行整个清单。手动：

- [ ] `hardware_check.py` 结论为 `ok` 或用户明确选择了 Comfy Cloud
- [ ] `comfy --version` 可用（或 `uvx --from comfy-cli comfy --help`）
- [ ] `curl http://HOST:PORT/system_stats` 返回 JSON
- [ ] `comfy model list` 显示至少一个 checkpoint（本地）或
      `/api/experiment/models/checkpoints` 返回模型（云端）
- [ ] 工作流 JSON 为 API 格式
- [ ] `check_deps.py` 报告 `is_ready: true`（或云端免费层仅 `node_check_skipped`）
- [ ] 小工作流测试运行完成；输出落入 `--output-dir`
