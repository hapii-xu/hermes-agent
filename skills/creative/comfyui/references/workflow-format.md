# ComfyUI 工作流 JSON 格式

## 两种格式 — 仅 API 格式可执行

**API 格式**是 `/api/prompt` 和本技能中每个脚本所必需的。Web UI 还会生成用于可视化编辑的"编辑器格式"，它**不能**直接提交。

### API 格式

顶层键是字符串节点 ID。每个节点有 `class_type` 和 `inputs`：

```json
{
  "3": {
    "class_type": "KSampler",
    "inputs": {
      "seed": 156680208700286,
      "steps": 20,
      "cfg": 8,
      "sampler_name": "euler",
      "scheduler": "normal",
      "denoise": 1.0,
      "model": ["4", 0],
      "positive": ["6", 0],
      "negative": ["7", 0],
      "latent_image": ["5", 0]
    },
    "_meta": {"title": "KSampler"}
  },
  "4": {
    "class_type": "CheckpointLoaderSimple",
    "inputs": {"ckpt_name": "v1-5-pruned-emaonly.safetensors"}
  }
}
```

**检测：** 每个顶层值都有 `class_type`。本技能的 `_common.is_api_format()` 执行此检查。

### 编辑器格式（不可直接执行）

有 `nodes[]` 和 `links[]` 数组 — 可视图。转换方法：在 ComfyUI 的 Web UI 中打开并使用 **Workflow → Export (API)**（较新的 UI）或 "Save (API Format)" 按钮（较旧的 UI）。

**检测：** 顶层有 `"nodes"` 和 `"links"` 键。

## 输入：字面量 vs 链接

```json
"inputs": {
  "text": "a cat",         // 字面量 — 可修改
  "seed": 42,              // 字面量 — 可修改
  "clip": ["4", 1]         // 链接 — 连线；不要覆盖
}
```

链接是长度为 2 的 `[upstream_node_id, output_slot]` 数组。本技能的参数注入器拒绝用字面量覆盖链接（记录警告并跳过）。

## 常见节点类型及其可控参数

完整目录位于 `scripts/_common.py`（`PARAM_PATTERNS` 和 `MODEL_LOADERS`）。要点：

### 文本提示词

| 节点类 | 关键字段 |
|------------|------------|
| `CLIPTextEncode` | `text` |
| `CLIPTextEncodeSDXL` | `text_g`、`text_l`、`width`、`height` |
| `CLIPTextEncodeFlux` | `clip_l`、`t5xxl`、`guidance` |

为区分正向和负向，本技能通过 Reroute / Primitive 节点从 `KSampler.negative` 追溯到源 CLIPTextEncode。回退到 `_meta.title` 启发式（"negative"、"neg"、"anti"）。

### 采样

| 节点类 | 关键字段 |
|------------|------------|
| `KSampler` | `seed`、`steps`、`cfg`、`sampler_name`、`scheduler`、`denoise` |
| `KSamplerAdvanced` | `noise_seed`、`steps`、`cfg`、`start_at_step`、`end_at_step` |
| `SamplerCustom` | `noise_seed`、`cfg`、`sampler`、`sigmas` |
| `SamplerCustomAdvanced` | `noise_seed`（通过 RandomNoise 输入） |
| `RandomNoise` | `noise_seed` |
| `BasicScheduler` | `steps`、`scheduler`、`denoise` |
| `KSamplerSelect` | `sampler_name` |
| `BasicGuider` / `CFGGuider` | `cfg` |
| `ModelSamplingFlux` | `max_shift`、`base_shift`、`width`、`height` |
| `SDTurboScheduler` | `steps`、`denoise` |

### Latent / 尺寸

| 节点类 | 关键字段 |
|------------|------------|
| `EmptyLatentImage` | `width`、`height`、`batch_size` |
| `EmptySD3LatentImage` | `width`、`height`、`batch_size` |
| `EmptyHunyuanLatentVideo` | `width`、`height`、`length`、`batch_size` |
| `EmptyMochiLatentVideo` | `width`、`height`、`length`、`batch_size` |
| `EmptyLTXVLatentVideo` | `width`、`height`、`length`、`batch_size` |

### 模型加载

| 节点类 | 关键字段 | 文件夹 |
|------------|------------|--------|
| `CheckpointLoaderSimple` | `ckpt_name` | `checkpoints` |
| `LoraLoader` | `lora_name`、`strength_model`、`strength_clip` | `loras` |
| `LoraLoaderModelOnly` | `lora_name`、`strength_model` | `loras` |
| `VAELoader` | `vae_name` | `vae` |
| `ControlNetLoader` | `control_net_name` | `controlnet` |
| `CLIPLoader` | `clip_name` | `clip` |
| `DualCLIPLoader` | `clip_name1`、`clip_name2` | `clip` |
| `TripleCLIPLoader` | `clip_name1/2/3` | `clip` |
| `UNETLoader` | `unet_name` | `unet` |
| `DiffusionModelLoader` | `model_name` | `diffusion_models` |
| `UpscaleModelLoader` | `model_name` | `upscale_models` |
| `IPAdapterModelLoader` | `ipadapter_file` | `ipadapter` |
| `ADE_AnimateDiffLoaderWithContext` | `model_name`、`motion_scale` | `animatediff_models` |

### 图像输入/输出

| 节点类 | 关键字段 |
|------------|------------|
| `LoadImage` | `image`（服务器端文件名，上传后） |
| `LoadImageMask` | `image`、`channel`（`red` / `green` / `blue` / `alpha`） |
| `VAEEncode` / `VAEDecode` | （无可控字段） |
| `VAEEncodeForInpaint` | `grow_mask_by` |
| `SaveImage` | `filename_prefix` |
| `VHS_VideoCombine` | `frame_rate`、`format`、`filename_prefix`、`loop_count`、`pingpong` |

### ControlNet

| 节点类 | 关键字段 |
|------------|------------|
| `ControlNetApply` | `strength` |
| `ControlNetApplyAdvanced` | `strength`、`start_percent`、`end_percent` |

### IPAdapter（社区包 `comfyui_ipadapter_plus`）

| 节点类 | 关键字段 |
|------------|------------|
| `IPAdapterAdvanced` | `weight`、`start_at`、`end_at` |
| `IPAdapter` | `weight` |

### Embeddings（在提示字符串内引用）

ComfyUI 扫描提示文本中的 `embedding:NAME` 语法。本技能的 `_common.iter_embedding_refs()` 将这些提取为模型依赖。

```text
"a beautiful cat, embedding:goodvibes:1.2, embedding:art-style"
```

`extract_schema.py` 和 `check_deps.py` 在 `embedding_dependencies` / `missing_embeddings` 中显示这些。

## 参数注入模式

```python
import json, copy

with open("workflow_api.json") as f:
    workflow = json.load(f)

wf = copy.deepcopy(workflow)
wf["6"]["inputs"]["text"] = "a beautiful sunset"
wf["7"]["inputs"]["text"] = "ugly, blurry"
wf["3"]["inputs"]["seed"] = 42
wf["3"]["inputs"]["steps"] = 30
wf["5"]["inputs"]["width"] = 1024
wf["5"]["inputs"]["height"] = 1024
```

`scripts/extract_schema.py` 自动发现哪些节点 ID/字段对应哪些面向用户的参数。它返回一个 `parameters` 字典，`run_workflow.py` 读取它以从 `--args` 注入值。

## 识别可控参数（启发式）

对于未知工作流：

1. **提示文本** — 任何 `CLIPTextEncode.text`。使用从 `KSampler.positive` / `.negative` 回溯的连接追踪来消除歧义（不要仅信任 meta-title）。
2. **种子** — `KSampler.seed` / `KSamplerAdvanced.noise_seed` / `RandomNoise.noise_seed`。
3. **尺寸** — `Empty*LatentImage.width/height`（必须是 8 的倍数）。
4. **步数 / CFG** — `KSampler.steps`、`KSampler.cfg`。步数典型 20–50。
   CFG 典型 5–15（Flux 使用 guidance，而非 CFG）。
5. **模型 / checkpoint** — `CheckpointLoaderSimple.ckpt_name`。文件名必须与已安装文件*精确*匹配。
6. **LoRA** — `LoraLoader.lora_name`、`.strength_model`。
7. **用于 img2img / inpaint 的图像** — `LoadImage.image`。上传后的服务器端文件名。
8. **去噪** — `KSampler.denoise`。0.0–1.0；1.0 = 忽略输入图像，
   0.0 = 直通。img2img 的最佳点：0.4–0.7。

## 输出节点

输出由这些节点类型产生。本技能的 `OUTPUT_NODES` 集合扩展到常见的社区包。

| 节点 | 输出键 | 内容 |
|------|-----------|---------|
| `SaveImage` | `images` | `{filename, subfolder, type}` 列表 |
| `PreviewImage` | `images` | 临时预览（不保存） |
| `VHS_VideoCombine` | `gifs`（较旧）或 `videos`/`video`（较新云端） | 视频文件引用 |
| `SaveAudio` | `audio` | 音频文件引用 |
| `SaveAnimatedWEBP` / `SaveAnimatedPNG` | `images` | 动画图像 |
| `Save3D` | `3d` | 3D 资产引用 |

执行后，从 `/history/{prompt_id}`（本地）或 `/api/jobs/{prompt_id}`（云端）→ `outputs` → `{node_id}` → `{key}` 获取输出。

## 包装器变体

一些保存的 JSON 文件将工作流包装在 `"prompt"` 键下（匹配 `/api/prompt` 负载结构）。本技能的 `_common.unwrap_workflow()` 处理此情况 — 传递以下任一：

- 原始 API 格式：`{"3": {...}, "4": {...}}`
- 包装的：`{"prompt": {"3": {...}}, "client_id": "..."}`

它以清晰的错误和重新导出指令拒绝编辑器格式。
