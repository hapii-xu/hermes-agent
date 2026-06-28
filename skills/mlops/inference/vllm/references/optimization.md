# 性能优化

## 目录
- PagedAttention 原理详解
- 连续批处理机制
- 前缀缓存策略
- 投机解码设置
- 基准测试结果与对比
- 性能调优指南

## PagedAttention 原理详解

**传统 attention 的问题**：
- KV cache 存储在连续内存中
- 因内存碎片浪费约 50% 的 GPU 显存
- 无法为不同序列长度动态重新分配

**PagedAttention 的解决方案**：
- 将 KV cache 划分为固定大小的块（类似操作系统的虚拟内存）
- 从空闲块队列中动态分配
- 跨序列共享块（用于前缀缓存）

**显存节省示例**：
```
传统方式：70B 模型需要 160GB KV cache → 8x A100 上 OOM
PagedAttention：70B 模型需要 80GB KV cache → 4x A100 即可容纳
```

**配置**：
```bash
# 块大小（默认：16 个 token）
vllm serve MODEL --block-size 16

# GPU 块数量（自动计算）
# 由 --gpu-memory-utilization 控制
vllm serve MODEL --gpu-memory-utilization 0.9
```

## 连续批处理机制

**传统批处理**：
- 等待批次中所有序列完成
- GPU 在等待最长序列时空闲
- GPU 利用率低（约 40-60%）

**连续批处理**：
- 有空位时立即加入新请求
- 在同一批次中混合 prefill（新请求）与 decode（进行中的请求）
- GPU 利用率高（>90%）

**吞吐量提升**：
```
传统批处理：50 req/sec @ 50% GPU 利用率
连续批处理：200 req/sec @ 90% GPU 利用率
= 4 倍吞吐量提升
```

**调优参数**：
```bash
# 最大并发序列数（越高 = 批处理越多）
vllm serve MODEL --max-num-seqs 256

# prefill/decode 调度（默认自动均衡）
# 无需手动调优
```

## 前缀缓存策略

对常见的提示词前缀复用已计算的 KV cache。

**适用场景**：
- 跨请求重复的系统提示词
- 每个提示词中都有的 few-shot 示例
- 存在重叠分块的 RAG 上下文

**节省示例**：
```
提示词：[System: 500 tokens] + [User: 100 tokens]

无缓存：每次请求计算 600 个 token
有缓存：一次性计算 500 个 token，之后每请求只算 100 个 token
= TTFT 提速 83%
```

**启用前缀缓存**：
```bash
vllm serve MODEL --enable-prefix-caching
```

**自动前缀检测**：
- vLLM 自动检测公共前缀
- 无需改动代码
- 与 OpenAI 兼容 API 协同工作

**缓存命中率监控**：
```bash
curl http://localhost:9090/metrics | grep cache_hit
# vllm_cache_hit_rate: 0.75  (75% 命中率)
```

## 投机解码设置

用较小的「草稿（draft）」模型提出 token，再由大模型验证。

**速度提升**：
```
标准方式：每次前向传播生成 1 个 token
投机解码：每次前向传播生成 3-5 个 token
= 生成速度快 2-3 倍
```

**工作原理**：
1. 草稿模型提出 K 个 token（快速）
2. 目标模型并行验证全部 K 个 token（一次前向传播）
3. 接受已验证的 token，从第一个被拒处重新开始

**使用独立草稿模型设置**：
```bash
vllm serve meta-llama/Llama-3-70B-Instruct \
  --speculative-model TinyLlama/TinyLlama-1.1B-Chat-v1.0 \
  --num-speculative-tokens 5
```

**使用 n-gram 草稿的设置**（无需独立模型）：
```bash
vllm serve MODEL \
  --speculative-method ngram \
  --num-speculative-tokens 3
```

**何时使用**：
- 输出长度 > 100 个 token
- 草稿模型比目标模型小 5-10 倍
- 可接受 2-3% 的精度折损

## 基准测试结果

**vLLM vs HuggingFace Transformers**（Llama 3 8B，A100）：
```
指标                    | HF Transformers | vLLM   | 提升
------------------------|-----------------|--------|------------
吞吐量 (req/sec)        | 12              | 280    | 23 倍
TTFT (ms)               | 850             | 120    | 7 倍
Tokens/sec              | 45              | 2,100  | 47 倍
GPU 显存 (GB)           | 28              | 16     | 少用 1.75 倍
```

**vLLM vs TensorRT-LLM**（Llama 2 70B，4x A100）：
```
指标                    | TensorRT-LLM | vLLM   | 备注
------------------------|--------------|--------|------------------
吞吐量 (req/sec)        | 320          | 285    | TRT 快 12%
配置复杂度              | 高           | 低     | vLLM 更易用
仅限 NVIDIA             | 是           | 否     | vLLM 跨平台
量化支持                | FP8, INT8    | AWQ/GPTQ/FP8 | vLLM 选项更多
```

## 性能调优指南

**第 1 步：测量基线**

```bash
# 安装基准测试工具
pip install locust

# 运行基线基准测试
vllm bench throughput \
  --model MODEL \
  --input-tokens 128 \
  --output-tokens 256 \
  --num-prompts 1000

# 记录：吞吐量、TTFT、tokens/sec
```

**第 2 步：调优显存利用率**

```bash
# 尝试不同取值：0.7、0.85、0.9、0.95
vllm serve MODEL --gpu-memory-utilization 0.9
```

越高 = 批处理容量越大 = 吞吐量越高，但有 OOM 风险。

**第 3 步：调优并发度**

```bash
# 尝试取值：128、256、512、1024
vllm serve MODEL --max-num-seqs 256
```

越高 = 批处理机会越多，但可能增加延迟。

**第 4 步：启用优化项**

```bash
vllm serve MODEL \
  --enable-prefix-caching \     # 针对重复提示词
  --enable-chunked-prefill \    # 针对长提示词
  --gpu-memory-utilization 0.9 \
  --max-num-seqs 512
```

**第 5 步：重新基准测试并对比**

目标改进：
- 吞吐量：+30-100%
- TTFT：-20-50%
- GPU 利用率：>85%

**常见性能问题**：

**吞吐量低（<50 req/sec）**：
- 增大 `--max-num-seqs`
- 启用 `--enable-prefix-caching`
- 检查 GPU 利用率（应 >80%）

**TTFT 高（>1 秒）**：
- 启用 `--enable-chunked-prefill`
- 尽可能减小 `--max-model-len`
- 检查模型是否对 GPU 而言过大

**OOM 错误**：
- 将 `--gpu-memory-utilization` 降到 0.7
- 减小 `--max-model-len`
- 使用量化（`--quantization awq`）
