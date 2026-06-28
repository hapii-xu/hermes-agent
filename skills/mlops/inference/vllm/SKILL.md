---
name: serving-llms-vllm
description: "vLLM：高吞吐 LLM 服务、OpenAI API、量化。"
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [vllm, torch, transformers]
platforms: [linux, macos]
metadata:
  hermes:
    tags: [vLLM, Inference Serving, PagedAttention, Continuous Batching, High Throughput, Production, OpenAI API, Quantization, Tensor Parallelism]

---

# vLLM - 高性能 LLM 服务

## 何时使用

在部署生产级 LLM API、优化推理延迟/吞吐量，或在有限的 GPU 显存下服务模型时使用。支持 OpenAI 兼容的端点、量化（GPTQ/AWQ/FP8）以及张量并行（tensor parallelism）。

## 快速开始

vLLM 通过 PagedAttention（基于块的 KV cache）和连续批处理（continuous batching，混合 prefill/decode 请求）实现了比标准 transformers 高 24 倍的吞吐量。

**安装**：
```bash
pip install vllm
```

**基础离线推理**：
```python
from vllm import LLM, SamplingParams

llm = LLM(model="meta-llama/Llama-3-8B-Instruct")
sampling = SamplingParams(temperature=0.7, max_tokens=256)

outputs = llm.generate(["Explain quantum computing"], sampling)
print(outputs[0].outputs[0].text)
```

**OpenAI 兼容服务器**：
```bash
vllm serve meta-llama/Llama-3-8B-Instruct

# 使用 OpenAI SDK 查询
python -c "
from openai import OpenAI
client = OpenAI(base_url='http://localhost:8000/v1', api_key='EMPTY')
print(client.chat.completions.create(
    model='meta-llama/Llama-3-8B-Instruct',
    messages=[{'role': 'user', 'content': 'Hello!'}]
).choices[0].message.content)
"
```

## 常见工作流

### 工作流 1：生产 API 部署

复制这份清单并跟踪进度：

```
Deployment Progress:
- [ ] Step 1: Configure server settings
- [ ] Step 2: Test with limited traffic
- [ ] Step 3: Enable monitoring
- [ ] Step 4: Deploy to production
- [ ] Step 5: Verify performance metrics
```

**第 1 步：配置服务器设置**

根据你的模型大小选择配置：

```bash
# 适用于单 GPU 上的 7B-13B 模型
vllm serve meta-llama/Llama-3-8B-Instruct \
  --gpu-memory-utilization 0.9 \
  --max-model-len 8192 \
  --port 8000

# 适用于 30B-70B 模型配合张量并行
vllm serve meta-llama/Llama-2-70b-hf \
  --tensor-parallel-size 4 \
  --gpu-memory-utilization 0.9 \
  --quantization awq \
  --port 8000

# 用于生产环境，带缓存和指标
vllm serve meta-llama/Llama-3-8B-Instruct \
  --gpu-memory-utilization 0.9 \
  --enable-prefix-caching \
  --enable-metrics \
  --metrics-port 9090 \
  --port 8000 \
  --host 0.0.0.0
```

**第 2 步：用有限的流量进行测试**

上线前先做负载测试：

```bash
# 安装负载测试工具
pip install locust

# 用示例请求创建 test_load.py
# 运行：locust -f test_load.py --host http://localhost:8000
```

确认 TTFT（time to first token，首 token 延迟）< 500ms，吞吐量 > 100 req/sec。

**第 3 步：启用监控**

vLLM 会在 9090 端口暴露 Prometheus 指标：

```bash
curl http://localhost:9090/metrics | grep vllm
```

需要监控的关键指标：
- `vllm:time_to_first_token_seconds` - 延迟
- `vllm:num_requests_running` - 活跃请求数
- `vllm:gpu_cache_usage_perc` - KV cache 利用率

**第 4 步：部署到生产环境**

使用 Docker 进行一致的部署：

```bash
# 在 Docker 中运行 vLLM
docker run --gpus all -p 8000:8000 \
  vllm/vllm-openai:latest \
  --model meta-llama/Llama-3-8B-Instruct \
  --gpu-memory-utilization 0.9 \
  --enable-prefix-caching
```

**第 5 步：核验性能指标**

检查部署是否达标：
- TTFT < 500ms（针对短提示）
- 吞吐量 > 目标 req/sec
- GPU 利用率 > 80%
- 日志中没有 OOM 错误

### 工作流 2：离线批量推理

用于在不产生服务器开销的情况下处理大型数据集。

复制这份清单：

```
Batch Processing:
- [ ] Step 1: Prepare input data
- [ ] Step 2: Configure LLM engine
- [ ] Step 3: Run batch inference
- [ ] Step 4: Process results
```

**第 1 步：准备输入数据**

```python
# 从文件加载提示词
prompts = []
with open("prompts.txt") as f:
    prompts = [line.strip() for line in f]

print(f"Loaded {len(prompts)} prompts")
```

**第 2 步：配置 LLM 引擎**

```python
from vllm import LLM, SamplingParams

llm = LLM(
    model="meta-llama/Llama-3-8B-Instruct",
    tensor_parallel_size=2,  # 使用 2 块 GPU
    gpu_memory_utilization=0.9,
    max_model_len=4096
)

sampling = SamplingParams(
    temperature=0.7,
    top_p=0.95,
    max_tokens=512,
    stop=["</s>", "\n\n"]
)
```

**第 3 步：运行批量推理**

vLLM 会自动对请求进行批处理以提高效率：

```python
# 一次性处理所有提示词
outputs = llm.generate(prompts, sampling)

# vLLM 内部会处理批处理
# 无需手动对提示词分块
```

**第 4 步：处理结果**

```python
# 提取生成的文本
results = []
for output in outputs:
    prompt = output.prompt
    generated = output.outputs[0].text
    results.append({
        "prompt": prompt,
        "generated": generated,
        "tokens": len(output.outputs[0].token_ids)
    })

# 保存到文件
import json
with open("results.jsonl", "w") as f:
    for result in results:
        f.write(json.dumps(result) + "\n")

print(f"Processed {len(results)} prompts")
```

### 工作流 3：量化模型服务

将大模型塞进有限的 GPU 显存中。

```
Quantization Setup:
- [ ] Step 1: Choose quantization method
- [ ] Step 2: Find or create quantized model
- [ ] Step 3: Launch with quantization flag
- [ ] Step 4: Verify accuracy
```

**第 1 步：选择量化方法**

- **AWQ**：最适合 70B 模型，精度损失极小
- **GPTQ**：模型支持广泛，压缩效果好
- **FP8**：在 H100 GPU 上速度最快

**第 2 步：查找或创建量化模型**

使用来自 HuggingFace 的预量化模型：

```bash
# 搜索 AWQ 模型
# 例如：TheBloke/Llama-2-70B-AWQ
```

**第 3 步：带量化标志启动**

```bash
# 使用预量化模型
vllm serve TheBloke/Llama-2-70B-AWQ \
  --quantization awq \
  --tensor-parallel-size 1 \
  --gpu-memory-utilization 0.95

# 结果：70B 模型只需约 40GB VRAM
```

**第 4 步：核验精度**

测试输出是否符合预期的质量：

```python
# 比较量化与非量化版本的响应
# 确认特定任务上的性能没有变化
```

## 何时使用 vs 替代方案

**在以下情况使用 vLLM：**
- 部署生产级 LLM API（100+ req/sec）
- 服务 OpenAI 兼容端点
- GPU 显存有限但需要大模型
- 多用户应用（聊天机器人、助手）
- 需要低延迟的同时保持高吞吐

**在以下情况改用替代方案：**
- **llama.cpp**：CPU/边缘推理、单用户
- **HuggingFace transformers**：研究、原型验证、一次性生成
- **TensorRT-LLM**：仅限 NVIDIA，需要绝对最高的性能
- **Text-Generation-Inference**：已身处 HuggingFace 生态

## 常见问题

**问题：加载模型时内存不足（Out of memory）**

降低内存占用：
```bash
vllm serve MODEL \
  --gpu-memory-utilization 0.7 \
  --max-model-len 4096
```

或使用量化：
```bash
vllm serve MODEL --quantization awq
```

**问题：首 token 慢（TTFT > 1 秒）**

为重复的提示词启用前缀缓存（prefix caching）：
```bash
vllm serve MODEL --enable-prefix-caching
```

对于长提示词，启用分块 prefill（chunked prefill）：
```bash
vllm serve MODEL --enable-chunked-prefill
```

**问题：找不到模型（Model not found）错误**

对自定义模型使用 `--trust-remote-code`：
```bash
vllm serve MODEL --trust-remote-code
```

**问题：吞吐量低（<50 req/sec）**

增加并发序列数：
```bash
vllm serve MODEL --max-num-seqs 512
```

用 `nvidia-smi` 检查 GPU 利用率 —— 应当 >80%。

**问题：推理比预期慢**

确认张量并行使用的是 2 的幂次个 GPU：
```bash
vllm serve MODEL --tensor-parallel-size 4  # 不是 3
```

启用投机解码（speculative decoding）以加速生成：
```bash
vllm serve MODEL --speculative-model DRAFT_MODEL
```

## 进阶主题

**服务器部署模式**：参见 [references/server-deployment.md](references/server-deployment.md)，了解 Docker、Kubernetes 和负载均衡配置。

**性能优化**：参见 [references/optimization.md](references/optimization.md)，了解 PagedAttention 调优、连续批处理细节以及基准测试结果。

**量化指南**：参见 [references/quantization.md](references/quantization.md)，了解 AWQ/GPTQ/FP8 设置、模型准备和精度对比。

**故障排查**：参见 [references/troubleshooting.md](references/troubleshooting.md)，了解详细的错误信息、调试步骤和性能诊断。

## 硬件要求

- **小模型（7B-13B）**：1x A10 (24GB) 或 A100 (40GB)
- **中等模型（30B-40B）**：2x A100 (40GB)，配合张量并行
- **大模型（70B+）**：4x A100 (40GB) 或 2x A100 (80GB)，使用 AWQ/GPTQ

支持的平台：NVIDIA（主要）、AMD ROCm、Intel GPU、TPU

## 资源

- 官方文档：https://docs.vllm.ai
- GitHub：https://github.com/vllm-project/vllm
- 论文："Efficient Memory Management for Large Language Model Serving with PagedAttention" (SOSP 2023)
- 社区：https://discuss.vllm.ai



