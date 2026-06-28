# ComfyUI REST + WebSocket API 参考

ComfyUI 暴露 REST + WebSocket 接口用于工作流执行和管理。**本地和 Comfy Cloud 使用相同的接口表面，但认证/路径有所不同。**

## 连接

| | 本地 ComfyUI | Comfy Cloud |
|---|---|---|
| Base URL | `http://127.0.0.1:8188` | `https://cloud.comfy.org` |
| API 路径前缀 | 无（`/prompt`、`/view`、…） | `/api/...`（`/api/prompt`、`/api/view`、…） |
| 认证 | 无（或配置后的 bearer token） | `X-API-Key` 头 |
| WebSocket | `ws://host:port/ws?clientId={uuid}` | `wss://cloud.comfy.org/ws?clientId={uuid}&token={API_KEY}` |
| `/api/view` 响应 | 直接字节 | 302 重定向 → 签名 URL（使用 `curl -L`） |

本技能脚本通过 `_common.resolve_url()` 自动路由 URL。

## Comfy Cloud 上的端点差异

云端接口在几个方面与本地 ComfyUI 不同。本技能脚本透明地处理这些；在此记录以便任何直接调用 `curl` 的人了解。

| 本地路径 | 云端路径 | 备注 |
|------------|-----------|-------|
| `/system_stats` | `/api/system_stats` | 云端版本是**公开的**（无需认证） |
| `/object_info` | `/api/object_info` | **仅付费层** — 免费返回 403 |
| `/queue` | `/api/queue` | 仅付费层 |
| `/userdata` | `/api/userdata` | 仅付费层 |
| `/prompt`（POST） | `/api/prompt`（POST） | 仅付费层 |
| `/upload/image` | `/api/upload/image` | 仅付费层；接受 `subfolder` 但被忽略 |
| `/upload/mask` | `/api/upload/mask` | 同上 |
| `/view` | `/api/view` | 仅付费层；**返回 302** 到签名 URL |
| `/history` | `/api/history_v2` | **已重命名**；旧路径返回 404 |
| `/history/{id}` | `/api/history_v2/{id}` 或 `/api/jobs/{id}` | 两者都可用；`/jobs` 返回完整任务 |
| `/models` | `/api/experiment/models` | **已重命名** |
| `/models/{folder}` | `/api/experiment/models/{folder}` | **已重命名**；响应结构不同（见下文） |

### 云端模型列表响应结构

- **本地：** `["a.safetensors", "b.safetensors", …]` — 字符串的扁平列表。
- **云端：** `[{"name": "a.safetensors", "pathIndex": 0}, …]` — 对象列表。
- **云端 404 带 `code: "folder_not_found"`** — 文件夹为空或未知，
  而非 "端点缺失" 错误。通过读取响应体来区分。

本技能辅助函数 `_common.parse_model_list()` 对两者进行归一化。

## 工作流执行

### 提交工作流

```bash
# 本地
curl -X POST "http://127.0.0.1:8188/prompt" \
  -H "Content-Type: application/json" \
  -d '{"prompt": '"$(cat workflow_api.json)"', "client_id": "'"$(uuidgen)"'"}'

# 云端
curl -X POST "https://cloud.comfy.org/api/prompt" \
  -H "X-API-Key: $COMFY_CLOUD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt": '"$(cat workflow_api.json)"'}'
```

**响应：**
```json
{"prompt_id": "abc-123-def", "number": 1, "node_errors": {}}
```

如果 `node_errors` 非空，则工作流有验证错误（缺少节点、错误输入）。

### 检查任务状态（云端）

```bash
curl -X GET "https://cloud.comfy.org/api/job/{prompt_id}/status" \
  -H "X-API-Key: $COMFY_CLOUD_API_KEY"
```

| 状态        | 描述                        |
| ------------- | ---------------------------------- |
| `pending`     | 任务已排队等待开始 |
| `in_progress` | 任务正在执行         |
| `completed`   | 任务成功完成          |
| `failed`      | 任务遇到错误           |
| `cancelled`   | 任务被用户取消          |

### 带输出的任务详情（云端）

```bash
curl -X GET "https://cloud.comfy.org/api/jobs/{prompt_id}" \
  -H "X-API-Key: $COMFY_CLOUD_API_KEY"
```

响应包含按节点 ID 索引的 `outputs`。云端在输出结构中使用 `video`（单数）；本地使用 `videos`（复数）。本技能脚本两者都接受。

### 获取历史（本地）

```bash
curl -s "http://127.0.0.1:8188/history"          # 全部
curl -s "http://127.0.0.1:8188/history/{id}"     # 单个 prompt_id
```

本地条目结构：
```json
{
  "<prompt_id>": {
    "prompt": [...],
    "outputs": {"<node_id>": {"images": [...]}},
    "status": {
      "status_str": "success" | "error",
      "completed": true | false,
      "messages": [["execution_start", {...}], ["execution_error", {...}], …]
    }
  }
}
```

**重要：** 读取状态时，先检查 `status_str == "error"` 再检查 `completed`，因为失败运行两者都可能为 true。

### 下载输出

```bash
# 本地（直接字节）
curl -s "http://127.0.0.1:8188/view?filename=ComfyUI_00001_.png&subfolder=&type=output" \
  -o output.png

# 云端（302 → 签名 URL；-L 跟随；第二跳剥离 X-API-Key）
curl -L "https://cloud.comfy.org/api/view?filename=...&type=output" \
  -H "X-API-Key: $COMFY_CLOUD_API_KEY" \
  -o output.png
```

本技能的 `run_workflow.py` 在跨主机重定向时自动剥离 `X-API-Key`，因此签名 URL 永远不会看到你的认证。

## WebSocket 监控

连接以获取实时执行事件。

```bash
# 本地
wscat -c "ws://127.0.0.1:8188/ws?clientId=MY-UUID"

# 云端
wscat -c "wss://cloud.comfy.org/ws?clientId=MY-UUID&token=$COMFY_CLOUD_API_KEY"
```

**注意：** 在云端 `clientId` 目前被忽略 — 用户的所有消息广播到每个连接。在客户端按 `data.prompt_id` 过滤消息。

### JSON 消息类型

| 类型 | 何时 | 关键字段 |
|------|------|------------|
| `status` | 队列变化 | `status.exec_info.queue_remaining` |
| `notification` | 用户友好的状态字符串 | `value` |
| `execution_start` | 工作流开始 | `prompt_id` |
| `executing` | 节点运行中（本地 `node` 为 null 时表示运行结束） | `node`、`prompt_id` |
| `progress` | 采样步数 | `node`、`value`、`max` |
| `progress_state` | 带每节点元数据的扩展进度 | `nodes`（dict） |
| `executed` | 节点输出就绪 | `node`、`output`（带 `images`/`video` 等） |
| `execution_cached` | 因缓存跳过的节点 | `nodes`（ID 列表） |
| `execution_success` | 全部完成 | `prompt_id` |
| `execution_error` | 失败 | `exception_type`、`exception_message`、`traceback`、`node_id` |
| `execution_interrupted` | 已取消 | `prompt_id` |

### 二进制帧（预览图像）

| 类型代码 | 含义 |
|-----------|---------|
| `0x00000001` | `PREVIEW_IMAGE` — `[type:4][image_type:4][data]`（image_type 1=JPEG，2=PNG） |
| `0x00000003` | `TEXT` — `[type:4][nid_len:4][nid][text]`（UTF-8） |
| `0x00000004` | `PREVIEW_IMAGE_WITH_METADATA` — `[type:4][meta_len:4][json][image_data]` |

`scripts/ws_monitor.py --previews <dir>` 将预览帧保存到磁盘。

## 文件上传

```bash
# 图像
curl -X POST "http://127.0.0.1:8188/upload/image" \
  -F "image=@photo.png" -F "type=input" -F "overwrite=true"
# 返回：{"name": "photo.png", "subfolder": "", "type": "input"}

# 蒙版（链接到先前上传的图像）
curl -X POST "http://127.0.0.1:8188/upload/mask" \
  -F "image=@mask.png" -F "type=input" \
  -F 'original_ref={"filename":"photo.png","subfolder":"","type":"input"}'
```

云端等价：在前面加 `https://cloud.comfy.org/api` 并添加 `-H "X-API-Key: $COMFY_CLOUD_API_KEY"`。

## 节点与模型发现

```bash
# 所有节点类型及其输入规格
curl -s "http://127.0.0.1:8188/object_info" | python3 -m json.tool

# 特定节点
curl -s "http://127.0.0.1:8188/object_info/KSampler"

# 每个文件夹的模型（本地）
curl -s "http://127.0.0.1:8188/models/checkpoints"
curl -s "http://127.0.0.1:8188/models/loras"

# 每个文件夹的模型（云端 — 注意实验性前缀）
curl -s "https://cloud.comfy.org/api/experiment/models/checkpoints" \
  -H "X-API-Key: $COMFY_CLOUD_API_KEY"
```

## 队列管理

```bash
# 查看队列
curl -s "http://127.0.0.1:8188/queue"

# 清除所有待处理
curl -X POST "http://127.0.0.1:8188/queue" \
  -H "Content-Type: application/json" \
  -d '{"clear": true}'

# 删除特定项
curl -X POST "http://127.0.0.1:8188/queue" \
  -H "Content-Type: application/json" \
  -d '{"delete": ["prompt_id_1", "prompt_id_2"]}'

# 取消当前运行的任务
curl -X POST "http://127.0.0.1:8188/interrupt"
```

## 系统管理

```bash
# 统计（VRAM、RAM、GPU、ComfyUI 版本）
curl -s "http://127.0.0.1:8188/system_stats"

# 释放 GPU 内存
curl -X POST "http://127.0.0.1:8188/free" \
  -H "Content-Type: application/json" \
  -d '{"unload_models": true, "free_memory": true}'
```

## ComfyUI-Manager 端点（可选）

这些需要安装 ComfyUI-Manager。用于通过 API 而非 `comfy-cli` 安装节点/模型时很有用。

```bash
# 从 git URL 安装自定义节点
curl -X POST "http://127.0.0.1:8188/manager/queue/install" \
  -H "Content-Type: application/json" \
  -d '{"git_url": "https://github.com/user/comfyui-node.git"}'

# 检查安装队列状态
curl -s "http://127.0.0.1:8188/manager/queue/status"

# 安装模型
curl -X POST "http://127.0.0.1:8188/manager/queue/install_model" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://...", "path": "models/checkpoints", "filename": "model.safetensors"}'
```

## POST /prompt 负载格式

```json
{
  "prompt": {
    "3": {
      "class_type": "KSampler",
      "inputs": {
        "seed": 42,
        "steps": 20,
        "cfg": 7.5,
        "sampler_name": "euler",
        "scheduler": "normal",
        "denoise": 1.0,
        "model": ["4", 0],
        "positive": ["6", 0],
        "negative": ["7", 0],
        "latent_image": ["5", 0]
      }
    }
  },
  "client_id": "unique-uuid-for-ws-filtering",
  "extra_data": {
    "api_key_comfy_org": "optional-PARTNER-NODE-key (NOT the cloud auth key)"
  }
}
```

- `prompt`：API 格式的工作流图
- `client_id`：UUID — 本地服务器用它过滤 WebSocket 事件；云端忽略它。
- `extra_data.api_key_comfy_org`：仅当工作流使用合作伙伴节点（Flux Pro、Ideogram 等）时需要。不要与 `X-API-Key` 混淆。

## 错误类别（云端 `execution_error` 的 `exception_type`）

| 类型 | 含义 |
|------|---------|
| `ValidationError` | 错误的工作流/输入（通常从 `node_errors` 提取更友好） |
| `ModelDownloadError` | 所需模型不可用 |
| `ImageDownloadError` | 从 URL 获取输入图像失败 |
| `OOMError` | GPU 内存不足 |
| `InsufficientFundsError` | 账户余额太低（合作伙伴节点） |
| `InactiveSubscriptionError` | 订阅未激活 |
