# comfy-cli 命令参考

来自 [Comfy-Org/comfy-cli](https://github.com/Comfy-Org/comfy-cli) 的官方 CLI。
文档：https://docs.comfy.org/comfy-cli/getting-started

## 安装

优先顺序：

```bash
pipx install comfy-cli            # 推荐（隔离环境）
uvx --from comfy-cli comfy --help # 通过 uv 零安装
pip install --user comfy-cli      # 备选
```

本技能的 `comfyui_setup.sh` 会选择最佳可用方式。

首次运行可能提示分析。非交互式禁用：
```bash
comfy --skip-prompt tracking disable
```

## 全局选项

| 选项 | 描述 |
|--------|-------------|
| `--workspace <path>` | 指定特定的 ComfyUI 工作区 |
| `--recent` | 使用最近使用的工作区 |
| `--here` | 使用当前目录作为工作区 |
| `--skip-prompt` | 无交互式提示（使用默认值） |
| `-v` / `--version` | 打印版本 |

工作区解析优先级：
1. `--workspace`（显式路径）
2. `--recent`（来自配置）
3. `--here`（cwd）
4. `comfy set-default` 路径
5. 最近使用过的
6. `~/comfy/ComfyUI`（Linux）或 `~/Documents/comfy/ComfyUI`（macOS/Win）

## 生命周期命令

### `comfy install`

下载并安装 ComfyUI + ComfyUI-Manager。

```bash
comfy install                    # 交互式 GPU 选择
comfy install --nvidia
comfy install --amd              # ROCm (Linux)
comfy install --m-series         # Apple Silicon (MPS)
comfy install --cpu              # 仅 CPU（慢）
comfy install --fast-deps        # 使用 uv 处理依赖
comfy install --skip-manager     # 跳过 ComfyUI-Manager
```

| 选项 | 描述 |
|--------|-------------|
| `--nvidia` / `--amd` / `--m-series` / `--cpu` | GPU 类型 |
| `--cuda-version` | 11.8、12.1、12.4、12.6、12.8、12.9、13.0 |
| `--rocm-version` | 6.1、6.2、6.3、7.0、7.1 |
| `--fast-deps` | 基于 uv 的依赖解析 |
| `--skip-manager` | 不安装 ComfyUI-Manager |
| `--skip-torch-or-directml` | 跳过 PyTorch 安装 |
| `--version <ver>` | `0.2.0`、`latest`、`nightly` |
| `--commit <hash>` | 安装特定 commit |
| `--pr "#1234"` | 从 PR 安装 |
| `--restore` | 为现有安装恢复依赖 |

### `comfy launch`

```bash
comfy launch                                   # 前台 :8188
comfy launch --background                      # 后台守护进程
comfy launch -- --listen 0.0.0.0               # LAN 可访问
comfy launch -- --port 8190                    # 自定义端口
comfy launch -- --cpu                          # 强制 CPU 模式
comfy launch -- --lowvram                      # 6 GB 显卡
comfy launch --background -- --listen 0.0.0.0 --port 8190
```

`--` 之后的常用额外参数：`--listen`、`--port`、`--cpu`、`--lowvram`、`--novram`、`--fp16-vae`、`--force-fp32`、`--disable-cuda-malloc`。

### `comfy stop`

```bash
comfy stop
```

### `comfy run`

向运行中的服务器提交原始工作流 JSON。**功能有限** — 无参数注入，无结构化输出下载。对于智能体，改用 `scripts/run_workflow.py`。

```bash
comfy run --workflow workflow_api.json
comfy run --workflow workflow_api.json --host 10.0.0.5 --port 8188
comfy run --workflow workflow_api.json --timeout 300 --wait
```

### `comfy which`

```bash
comfy which          # 显示目标工作区
comfy --recent which
```

### `comfy set-default`

```bash
comfy set-default /path/to/ComfyUI
comfy set-default /path/to/ComfyUI --launch-extras="--listen 0.0.0.0"
```

### `comfy update`

```bash
comfy update               # 更新 ComfyUI 核心
comfy node update all      # 更新所有自定义节点
```

---

## `comfy node` — 自定义节点管理

所有节点操作在底层使用 ComfyUI-Manager（`cm-cli`）。

```bash
comfy node show installed              # 列出已安装
comfy node show enabled                # 列出已启用
comfy node show all                    # registry 中所有可用
comfy node simple-show installed       # 紧凑列表

comfy node install comfyui-impact-pack
comfy node install <name> --uv-compile # ComfyUI-Manager v4.1+ 统一解析器
comfy node uninstall <name>
comfy node update <name> | all
comfy node enable <name>
comfy node disable <name>
comfy node fix <name>                  # 修复损坏的依赖

comfy node install-deps --workflow=workflow.json
comfy node deps-in-workflow --workflow=w.json --output=deps.json

comfy node save-snapshot
comfy node restore-snapshot <file>

comfy node bisect start                # 二分查找有问题的节点
comfy node bisect good
comfy node bisect bad
comfy node bisect reset
```

### 依赖解析选项

| 标志 | 描述 |
|------|-------------|
| `--fast-deps` | comfy-cli 内置 uv 解析器 |
| `--uv-compile` | ComfyUI-Manager v4.1+ 统一解析器（推荐） |
| `--no-deps` | 跳过依赖安装 |

将 `uv-compile` 设为默认：`comfy manager uv-compile-default true`

---

## `comfy model` — 模型管理

```bash
comfy model list
comfy model list --relative-path models/checkpoints

comfy model download --url <URL>
comfy model download --url <URL> --relative-path models/loras
comfy model download --url <URL> --filename custom_name.safetensors

comfy model remove                     # 交互式
comfy model remove --relative-path models/checkpoints --model-names "model.safetensors"
```

| 选项 | 描述 |
|--------|-------------|
| `--url` | 下载 URL（CivitAI、HuggingFace、直连） |
| `--relative-path` | 工作区下的子目录（例如 `models/checkpoints`） |
| `--filename` | 自定义保存文件名 |
| `--set-civitai-api-token` | 持久化 CivitAI token |
| `--set-hf-api-token` | 持久化 HuggingFace token |
| `--downloader` | `httpx`（默认）或 `aria2` |

标准模型目录：
```
ComfyUI/models/
├── checkpoints/        # 完整模型文件
├── loras/              # LoRA 适配器
├── vae/                # VAE 模型
├── controlnet/         # ControlNet 模型
├── clip/               # CLIP / T5 文本编码器
├── clip_vision/        # CLIP 视觉编码器
├── upscale_models/     # ESRGAN / SwinIR 等
├── embeddings/         # 文本反转 embeddings
├── unet/               # 独立 UNet 权重
├── diffusion_models/   # Flux / SD3 / Wan 扩散模型
├── animatediff_models/ # AnimateDiff 运动模块
├── ipadapter/          # IPAdapter 权重
└── style_models/       # 风格适配器
```

---

## `comfy manager` — ComfyUI-Manager 设置

```bash
comfy manager disable               # 完全禁用 Manager
comfy manager enable-gui            # 启用新 GUI
comfy manager disable-gui           # 仅 API
comfy manager enable-legacy-gui     # 旧版 GUI
comfy manager uv-compile-default true   # 将 --uv-compile 设为默认
comfy manager clear                 # 清除启动动作
```

---

## `comfy pr-cache` — 前端 PR 缓存

```bash
comfy pr-cache list
comfy pr-cache clean
comfy pr-cache clean 456
```

缓存 7 天后过期；最多 10 个构建。

---

## 配置

| 操作系统 | 路径 |
|----|------|
| Linux | `~/.config/comfy-cli/config.ini` |
| macOS | `~/Library/Application Support/comfy-cli/config.ini` |
| Windows | `~/AppData/Local/comfy-cli/config.ini` |

存储：默认工作区、最近工作区、后台服务器 PID、API token、Manager GUI 模式、启动额外参数。

## 发现

自定义节点 registry：
- https://registry.comfy.org/

模型浏览器：
- https://huggingface.co/models
- https://civitai.com（NSFW；许多需要 API token）
- https://comfyworkflows.com（社区工作流）
