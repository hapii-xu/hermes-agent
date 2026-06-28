# ComfyUI 工作流模板完整性

> **由 [@purzbeats](https://github.com/purzbeats) 编写** — 改编自
> [purzbeats/hermes-agent-comfyui-helper](https://github.com/purzbeats/hermes-agent-comfyui-helper)。
> 当从官方 `comfyui-workflow-templates` 包（编辑器格式）转换为通过
> `/api/prompt` 提交的 API 格式时，请使用此参考。转换中有一些微妙的陷阱，
> 如果不遵循这些规则，会导致难以诊断的验证错误。

## 背景

官方 ComfyUI 模板包（`comfyui-workflow-templates`，目前
v0.9.69）安装在 ComfyUI venv 内，路径类似：

```
<comfy-install>/.venv/lib/python3.*/site-packages/comfyui_workflow_templates_*/templates/
```

确切路径取决于 ComfyUI 的安装方式（comfy-cli 默认、
Comfy Desktop、手动 venv 等）。用以下命令找到一次：

```bash
comfy --workspace <ws> run-python -c "import comfyui_workflow_templates, pathlib; print(pathlib.Path(comfyui_workflow_templates.__file__).parent / 'templates')"
```

模板以**编辑器格式**发布 — `data['definitions']['subgraphs'][0]` 内的 `nodes` / `links` 数组。它们必须在提交前转换为**API 格式**（`node_id -> {class_type, inputs}` 映射）。

---

## 规则 #1：尽可能原样使用模板

- **绝不从模板中剥离、简化或"最小化"节点。**
- 完整的模板架构（双通道流水线、LoRA 链、蒸馏
  sigmas、条件路径）是有意为之 — 移除任何部分都会破坏质量。
- 如果存在依赖图像的路径但任务是文本到视频，**保持其连接并启用绕过开关** — 不要移除节点。
- 仅更改：提示词文本、种子和尺寸（当明确要求时）。

## 规则 #2：服务器验证错误是事实来源

当工作流提交失败时，服务器响应如下：

```json
{
  "node_errors": {
    "238": {
      "errors": [{
        "message": "Required input is missing",
        "details": "width",
        "extra_info": { "input_name": "resize_type.width" }
      }]
    }
  }
}
```

**`extra_info.input_name` 字段准确告诉你服务器想要的 JSON 键。按字面使用。** 如果它显示 `"values.a"` 或 `"resize_type.width"`，
那些就是 JSON 对象中实际的键名。不要根据对该字段"应该"叫什么的假设将它们"简化"为扁平名称。

## 规则 #3：不要从头重建 — 修补失败的节点

每次从模板重新生成都重新引入相同的 bug。相反：

1. 提交一次工作流。
2. 读取服务器错误详情以获取确切的键名。
3. 对磁盘上的工作流文件使用有针对性的 patch/fix 调用。
4. 重新提交并检查错误是否解决。

---

## Reroute 节点：绕过，不要删除

大多数服务器（本地、云端）没有 `Reroute` 节点类型。转换
模板时：

1. 通过查看 `target_id` = Reroute 节点 ID 的链接找到 Reroute 的输入源。
2. 将所有引用 Reroute 的输入替换为 `[source_node_id, source_slot]`。
3. 从 API 映射中删除 Reroute 节点。

**真实示例 — LTX 2.3 t2v 模板：**

- Reroute 节点 255 从 `CheckpointLoaderSimple 236` slot 2 接收 VAE。
- 三个节点为 VAE 输入引用 Reroute 255：
  `LTXVImgToVideoInplace`（230）、`LTXVLatentUpsampler`（253）、
  `VAEDecodeTiled`（251）。
- 修复：将所有 `vae: ["255", 0]` 替换为 `vae: ["236", 2]`。
- `CheckpointLoaderSimple` slot 2 = VAE（不是 slot 0 = MODEL）。

| | |
|---|---|
| ❌ 错误  | `vae: ["236", 0]` → `MODELV mismatch input_type(VAE)` |
| ✅ 正确 | `vae: ["236", 2]` |

---

## 动态模板节点：点分键名是正确的

### ComfyMathExpression（COMFY_AUTOGROW_V3）

```json
{
  "class_type": "ComfyMathExpression",
  "inputs": {
    "expression": "a/2",
    "values.a": ["257", 0]
  }
}
```

- `values` 是一个 `COMFY_AUTOGROW_V3` 模板。
- 链接中的输入名是 `values.a`、`values.b` 等。
- **保持点分格式作为 JSON 键。**
- 不要转换为 `{"values": {"a": ...}}` 或扁平化为仅 `"a"`。

### ResizeImageMaskNode（COMFY_DYNAMICCOMBO_V3）

```json
{
  "class_type": "ResizeImageMaskNode",
  "inputs": {
    "input": ["276", 0],
    "scale_method": "lanczos",
    "resize_type": "scale dimensions",
    "resize_type.width": 1920,
    "resize_type.height": 1088,
    "resize_type.crop": "center"
  }
}
```

- `resize_type` 是一个 `COMFY_DYNAMICCOMBO_V3`。
- 模式特定字段：`resize_type.width`、`resize_type.height`、`resize_type.crop`。
- `scale_method` 选项：`"nearest-exact"`、`"bilinear"`、`"area"`、`"bicubic"`、`"lanczos"`。
- **保持点分格式作为 JSON 键。**
- 不要将 `resize_type.width` 扁平化为仅 `"width"`。

---

## 转换配方

1. 从安装的包路径加载模板。
2. 解析 `data['definitions']['subgraphs'][0]`。
3. 对每个节点（跳过 Reroute）：
   - 从 `sg['links']` 字典解析链接输入。
   - 将 `widgets_values` 映射到输入字段名。
   - 保持模板中所有点分键名原样。
4. 绕过 Reroute：追踪源，替换引用。
5. 仅更改：提示词文本、种子值和用户请求的参数。
6. 如果模板仅使用 `CreateVideo`，则添加 `SaveVideo` 终端节点。
7. 提交 → 读取错误 → 修补特定节点 → 重新提交。

## 模板中绝不更改的内容

| 元素 | 原因 |
|---------|-----|
| 节点拓扑 | 图是为特定模型设计的 |
| Sigmas 值 | 为模型/采样器组合调优 |
| LoRA/蒸馏路径 | 质量所必需，即使看起来未使用 |
| 模型参数（cfg、steps、shifts） | 模型特定 |
| 条件链（zero-out、crop guides） | 正确条件所必需 |
| 传递连接 | 不要移除节点，绕过它们 |

---

## 云端兼容性（2025 年 5 月验证）

完整的 LTX 2.3 T2V 模板（`video_ltx2_3_t2v.json`）在 Comfy Cloud 上**无需修改**即可运行。

**在云端确认可用（所有自定义节点可用）：**
`ComfyMathExpression`、`ResizeImageMaskNode`、`ResizeImagesByLongerEdge`、
`PrimitiveInt`、`PrimitiveStringMultiline`、`PrimitiveBoolean`、`SaveVideo`、
`LTXVCropGuides`、`LTXVImgToVideoInplace`、`LTXVConcatAVLatent`、
`LTXVSeparateAVLatent`、`LTXVLatentUpsampler`、`LTXVAudioVAELoader`、
`LTXVAudioVAEDecode`、`LTXVEmptyLatentAudio`、`LTXVPreprocess`、
`LTXVConditioning`、`ManualSigmas`、`LTXAVTextEncoderLoader`，加上所有核心节点。

**LTX 2.3 的云端 vs 本地（768x512）：**

- 云端：每个视频约 39 秒（快 4 倍）。
- 本地（RTX 5090）：每个视频约 160 秒。
- `example.png` 占位符在云端可用于绕过的依赖图像路径。
- 提交格式在本地和云端之间**完全相同**：
  `{"prompt": wf, "extra_data": {}}` 到 `/api/prompt`。
- 免费层 = 1 个并发任务。

**云端提交陷阱：**

- `/api/object_info/<node>` 在免费层返回 404 — 无法远程查询节点
  schema，但工作流仍然正常运行。始终在构建工作流之前在本地探测
  `object_info`。
- 云端快约 4 倍 — 批量运行时优先使用云端，除非调试需要本地。
- 云端 `/api/view` 返回**302 重定向到签名 GCS URL** — 使用
  `curl -s -L` 跟随并下载。Python `urllib` 因 401 失败
  （将认证头转发给 GCS CDN）。
- `COMFY_CLOUD_API_KEY` 仅在终端/bash 环境中，不在 Python
  沙盒中。对云端 API 调用使用 subprocess 或终端脚本。
- 云端免费层**顺序**处理任务（一次 1 个）。提交全部，
  然后轮询历史。
- LTX 2.3 在 **1920x1080 本地 OOM**（即使 RTX 5090）— 放大器通道
  超出 VRAM。1080p 优先使用云端；本地使用 1280x720（约 90 秒/视频）。

---

## FFmpeg 拼接设置（Discord 兼容）

生成的 ComfyUI 视频通常使用 `yuv444p` 像素格式，这在 Discord 上不起作用。用以下命令重新编码：

```bash
ffmpeg -y -i input.mp4 \
  -c:v libx264 -profile:v main -preset medium -crf 13 -pix_fmt yuv420p \
  -c:a aac -b:a 192k \
  output_discord.mp4
```

关键设置：

- `-pix_fmt yuv420p` — **Discord 必需**，ComfyUI 默认输出 `yuv444p`。
- `-crf 13` — 高质量而不会产生巨大文件（默认 23 损失太大）。
- `-profile:v main` — 广泛兼容。

对于多视频交叉淡入淡出拼接，链接 `xfade`（视频）和 `acrossfade`
（音频）：

```bash
ffmpeg -y -i a.mp4 -i b.mp4 -i c.mp4 \
  -filter_complex "[0:v][1:v]xfade=transition=fade:duration=1:offset=3.04[v1];[v1][2:v]xfade=transition=fade:duration=1:offset=6.08[vout];[0:a][1:a]acrossfade=duration=1:c1=tri:c2=tri[a1];[a1][2:a]acrossfade=duration=1:c1=tri:c2=tri[aout]" \
  -map "[vout]" -map "[aout]" \
  -c:v libx264 -profile:v main -crf 13 -pix_fmt yuv420p \
  -c:a aac -b:a 192k \
  output.mp4
```

xfade #N 的偏移 = `(N+1) × 时长 - N × 重叠`。
