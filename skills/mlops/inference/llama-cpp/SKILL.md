---
name: llama-cpp
description: llama.cpp 本地 GGUF 推理 + HF Hub 模型发现。
version: 2.1.2
author: Orchestra Research
license: MIT
dependencies: [llama-cpp-python>=0.2.0]
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [llama.cpp, GGUF, Quantization, Hugging Face Hub, CPU Inference, Apple Silicon, Edge Deployment, AMD GPUs, Intel GPUs, NVIDIA, URL-first]
---

# llama.cpp + GGUF

当你需要进行本地 GGUF 推理、量化选择，或为 llama.cpp 发现 Hugging Face 仓库时，请使用本技能。

## 何时使用

- 在 CPU、Apple Silicon、CUDA、ROCm 或 Intel GPU 上运行本地模型
- 为特定的 Hugging Face 仓库找到合适的 GGUF
- 从 Hub 构建 `llama-server` 或 `llama-cli` 命令
- 搜索 Hub 上已支持 llama.cpp 的模型
- 列出某个仓库中可用的 `.gguf` 文件及其大小
- 根据用户的 RAM 或 VRAM 在 Q4/Q5/Q6/IQ 变体之间做出选择

## 模型发现工作流

在要求使用 `hf`、Python 或自定义脚本之前，优先使用 URL 工作流。

1. 在 Hub 上搜索候选仓库：
   - 基础地址：`https://huggingface.co/models?apps=llama.cpp&sort=trending`
   - 为模型系列添加 `search=<term>`
   - 当用户有尺寸约束时，添加 `num_parameters=min:0,max:24B` 或类似参数
2. 用 llama.cpp local-app 视图打开仓库：
   - `https://huggingface.co/<repo>?local-app=llama.cpp`
3. 当 local-app 代码片段可见时，将其视为权威来源：
   - 复制精确的 `llama-server` 或 `llama-cli` 命令
   - 完全按照 HF 显示的方式报告推荐的量化
4. 以页面文本或 HTML 形式读取同一个 `?local-app=llama.cpp` URL，并提取 `Hardware compatibility` 下的内容：
   - 优先使用其精确的量化标签和大小，而不是通用表格
   - 保留仓库特定的标签，例如 `UD-Q4_K_M` 或 `IQ4_NL_XL`
   - 如果该部分在抓取的页面源码中不可见，请说明并回退到 tree API 加上通用量化指南
5. 查询 tree API 以确认实际存在的内容：
   - `https://huggingface.co/api/models/<repo>/tree/main?recursive=true`
   - 保留 `type` 为 `file` 且 `path` 以 `.gguf` 结尾的条目
   - 将 `path` 和 `size` 作为文件名和字节大小的权威来源
   - 将量化检查点与 `mmproj-*.gguf` 投影器文件以及 `BF16/` 分片文件区分开
   - 仅在 API 失败时才将 `https://huggingface.co/<repo>/tree/main` 作为人工查看的备用方式
6. 如果 local-app 代码片段在文本中不可见，则根据仓库和所选量化重构命令：
   - 简写量化选择：`llama-server -hf <repo>:<QUANT>`
   - 精确文件回退：`llama-server --hf-repo <repo> --hf-file <filename.gguf>`
7. 只有当仓库尚未提供 GGUF 文件时，才建议从 Transformers 权重进行转换。

## 快速开始

### 安装 llama.cpp

```bash
# macOS / Linux（最简单的方式）
brew install llama.cpp
```

```bash
winget install llama.cpp
```

```bash
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
cmake -B build
cmake --build build --config Release
```

### 直接从 Hugging Face Hub 运行

```bash
llama-cli -hf bartowski/Llama-3.2-3B-Instruct-GGUF:Q8_0
```

```bash
llama-server -hf bartowski/Llama-3.2-3B-Instruct-GGUF:Q8_0
```

### 从 Hub 运行精确的 GGUF 文件

当 tree API 显示自定义文件命名或缺少精确的 HF 代码片段时使用此方式。

```bash
llama-server \
    --hf-repo microsoft/Phi-3-mini-4k-instruct-gguf \
    --hf-file Phi-3-mini-4k-instruct-q4.gguf \
    -c 4096
```

### OpenAI 兼容服务器检查

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "user", "content": "Write a limerick about Python exceptions"}
    ]
  }'
```

## Python 绑定（llama-cpp-python）

`pip install llama-cpp-python`（CUDA：`CMAKE_ARGS="-DGGML_CUDA=on" pip install llama-cpp-python --force-reinstall --no-cache-dir`；Metal：`CMAKE_ARGS="-DGGML_METAL=on" ...`）。

### 基础生成

```python
from llama_cpp import Llama

llm = Llama(
    model_path="./model-q4_k_m.gguf",
    n_ctx=4096,
    n_gpu_layers=35,     # 0 表示 CPU，99 表示全部卸载到 GPU
    n_threads=8,
)

out = llm("What is machine learning?", max_tokens=256, temperature=0.7)
print(out["choices"][0]["text"])
```

### 对话 + 流式输出

```python
llm = Llama(
    model_path="./model-q4_k_m.gguf",
    n_ctx=4096,
    n_gpu_layers=35,
    chat_format="llama-3",   # 或 "chatml"、"mistral" 等
)

resp = llm.create_chat_completion(
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is Python?"},
    ],
    max_tokens=256,
)
print(resp["choices"][0]["message"]["content"])

# 流式输出
for chunk in llm("Explain quantum computing:", max_tokens=256, stream=True):
    print(chunk["choices"][0]["text"], end="", flush=True)
```

### 嵌入向量

```python
llm = Llama(model_path="./model-q4_k_m.gguf", embedding=True, n_gpu_layers=35)
vec = llm.embed("This is a test sentence.")
print(f"Embedding dimension: {len(vec)}")
```

你也可以直接从 Hub 加载 GGUF：

```python
llm = Llama.from_pretrained(
    repo_id="bartowski/Llama-3.2-3B-Instruct-GGUF",
    filename="*Q4_K_M.gguf",
    n_gpu_layers=35,
)
```

## 选择量化

先看 Hub 页面，其次再用通用启发式规则。

- 优先选择 HF 为用户硬件配置标记为兼容的精确量化。
- 对于一般对话，从 `Q4_K_M` 开始。
- 对于代码或技术类工作，如果内存允许，优先使用 `Q5_K_M` 或 `Q6_K`。
- 对于非常紧张的 RAM 预算，只有当用户明确优先考虑适配而非质量时，才考虑 `Q3_K_M`、`IQ` 变体或 `Q2` 变体。
- 对于多模态仓库，单独提及 `mmproj-*.gguf`。投影器不是主模型文件。
- 不要规范化仓库原生标签。如果页面显示 `UD-Q4_K_M`，就报告 `UD-Q4_K_M`。

## 从仓库中提取可用的 GGUF

当用户询问存在哪些 GGUF 时，返回：

- 文件名
- 文件大小
- 量化标签
- 它是主模型还是辅助投影器

除非用户要求，否则忽略：

- README
- BF16 分片文件
- imatrix 数据块或校准产物

此步骤使用 tree API：

- `https://huggingface.co/api/models/<repo>/tree/main?recursive=true`

对于像 `unsloth/Qwen3.6-35B-A3B-GGUF` 这样的仓库，local-app 页面可以显示 `UD-Q4_K_M`、`UD-Q5_K_M`、`UD-Q6_K` 和 `Q8_0` 等量化标签，而 tree API 则暴露精确的文件路径，如 `Qwen3.6-35B-A3B-UD-Q4_K_M.gguf` 和 `Qwen3.6-35B-A3B-Q8_0.gguf` 及其字节大小。使用 tree API 将量化标签转换为精确的文件名。

## 搜索模式

直接使用这些 URL 形式：

```text
https://huggingface.co/models?apps=llama.cpp&sort=trending
https://huggingface.co/models?search=<term>&apps=llama.cpp&sort=trending
https://huggingface.co/models?search=<term>&apps=llama.cpp&num_parameters=min:0,max:24B&sort=trending
https://huggingface.co/<repo>?local-app=llama.cpp
https://huggingface.co/api/models/<repo>/tree/main?recursive=true
https://huggingface.co/<repo>/tree/main
```

## 输出格式

在回答发现类请求时，优先使用如下紧凑的结构化结果：

```text
Repo: <repo>
Recommended quant from HF: <label> (<size>)
llama-server: <command>
Other GGUFs:
- <filename> - <size>
- <filename> - <size>
Source URLs:
- <local-app URL>
- <tree API URL>
```

## 参考资料

- **[hub-discovery.md](references/hub-discovery.md)** - 仅基于 URL 的 Hugging Face 工作流、搜索模式、GGUF 提取和命令重构
- **[advanced-usage.md](references/advanced-usage.md)** — 投机解码、批量推理、语法约束生成、LoRA、多 GPU、自定义构建、基准测试脚本
- **[quantization.md](references/quantization.md)** — 量化质量权衡、何时使用 Q4/Q5/Q6/IQ、模型大小扩展、imatrix
- **[server.md](references/server.md)** — 直接从 Hub 启动服务器、OpenAI API 端点、Docker 部署、NGINX 负载均衡、监控
- **[optimization.md](references/optimization.md)** — CPU 线程、BLAS、GPU 卸载启发式、批处理调优、基准测试
- **[troubleshooting.md](references/troubleshooting.md)** — 安装/转换/量化/推理/服务器问题、Apple Silicon、调试

## 资源

- **GitHub**：https://github.com/ggml-org/llama.cpp
- **Hugging Face GGUF + llama.cpp 文档**：https://huggingface.co/docs/hub/gguf-llamacpp
- **Hugging Face Local Apps 文档**：https://huggingface.co/docs/hub/main/local-apps
- **Hugging Face Local Agents 文档**：https://huggingface.co/docs/hub/agents-local
- **示例 local-app 页面**：https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF?local-app=llama.cpp
- **示例 tree API**：https://huggingface.co/api/models/unsloth/Qwen3.6-35B-A3B-GGUF/tree/main?recursive=true
- **示例 llama.cpp 搜索**：https://huggingface.co/models?num_parameters=min:0,max:24B&apps=llama.cpp&sort=trending
- **许可证**：MIT
