# GGUF 量化指南

GGUF 量化格式与模型转换的完整指南。

## Hub 优先的量化选择

在使用通用表格之前，先用以下地址打开模型仓库：

```text
https://huggingface.co/<repo>?local-app=llama.cpp
```

优先使用抓取的 `?local-app=llama.cpp` 页面文本或 HTML 中 `Hardware compatibility` 部分显示的精确量化标签和大小。然后在以下地址确认匹配的文件名：

```text
https://huggingface.co/api/models/<repo>/tree/main?recursive=true
```

先看 Hub 页面，只有当仓库页面未给出明确推荐时，才回退到下面的通用启发式规则。

## 量化概述

**GGUF**（GPT-Generated Unified Format）- llama.cpp 模型的标准格式。

### 格式对比

| 格式 | 困惑度 | 大小（7B） | Tokens/秒 | 说明 |
|--------|------------|-----------|------------|-------|
| FP16 | 5.9565（基线） | 13.0 GB | 15 tok/s | 原始质量 |
| Q8_0 | 5.9584（+0.03%） | 7.0 GB | 25 tok/s | 几乎无损 |
| **Q6_K** | 5.9642（+0.13%） | 5.5 GB | 30 tok/s | 最佳质量/大小比 |
| **Q5_K_M** | 5.9796（+0.39%） | 4.8 GB | 35 tok/s | 均衡 |
| **Q4_K_M** | 6.0565（+1.68%） | 4.1 GB | 40 tok/s | **推荐** |
| Q4_K_S | 6.1125（+2.62%） | 3.9 GB | 42 tok/s | 更快，质量更低 |
| Q3_K_M | 6.3184（+6.07%） | 3.3 GB | 45 tok/s | 仅用于小模型 |
| Q2_K | 6.8673（+15.3%） | 2.7 GB | 50 tok/s | 不推荐 |

**建议**：使用 **Q4_K_M** 以获得质量与速度的最佳平衡。

## 转换模型

### 从 Hugging Face 到 GGUF

```bash
# 1. 下载 Hugging Face 模型
hf download meta-llama/Llama-2-7b-chat-hf \
    --local-dir models/llama-2-7b-chat/

# 2. 转换为 FP16 GGUF
python convert_hf_to_gguf.py \
    models/llama-2-7b-chat/ \
    --outtype f16 \
    --outfile models/llama-2-7b-chat-f16.gguf

# 3. 量化为 Q4_K_M
./llama-quantize \
    models/llama-2-7b-chat-f16.gguf \
    models/llama-2-7b-chat-Q4_K_M.gguf \
    Q4_K_M
```

### 批量量化

```bash
# 量化为多种格式
for quant in Q4_K_M Q5_K_M Q6_K Q8_0; do
    ./llama-quantize \
        model-f16.gguf \
        model-${quant}.gguf \
        $quant
done
```

## K 量化方法

**K-quants** 使用混合精度以获得更高质量：
- 注意力权重：更高精度
- 前馈权重：更低精度

**变体**：
- `_S`（Small，小）：更快，质量更低
- `_M`（Medium，中）：均衡（推荐）
- `_L`（Large，大）：更高质量，更大体积

**示例**：`Q4_K_M`
- `Q4`：4 比特量化
- `K`：混合精度方法
- `M`：中等质量

## 质量测试

```bash
# 计算困惑度（质量指标）
./llama-perplexity \
    -m model.gguf \
    -f wikitext-2-raw/wiki.test.raw \
    -c 512

# 困惑度越低 = 质量越好
# 基线（FP16）：~5.96
# Q4_K_M：~6.06（+1.7%）
# Q2_K：~6.87（+15.3% - 退化太多）
```

## 使用场景指南

### 通用场景（聊天机器人、助手）
```
Q4_K_M - 最佳平衡
Q5_K_M - 如果你有额外 RAM
```

### 代码生成
```
Q5_K_M 或 Q6_K - 更高精度对代码有帮助
```

### 创意写作
```
Q4_K_M - 质量足够
Q3_K_M - 可用于草稿生成
```

### 技术/医学
```
Q6_K 或 Q8_0 - 最高准确度
```

### 边缘设备（Raspberry Pi）
```
Q2_K 或 Q3_K_S - 适配有限 RAM
```

## 模型大小扩展

### 7B 参数模型

| 格式 | 大小 | 所需 RAM |
|--------|------|------------|
| Q2_K | 2.7 GB | 5 GB |
| Q3_K_M | 3.3 GB | 6 GB |
| Q4_K_M | 4.1 GB | 7 GB |
| Q5_K_M | 4.8 GB | 8 GB |
| Q6_K | 5.5 GB | 9 GB |
| Q8_0 | 7.0 GB | 11 GB |

### 13B 参数模型

| 格式 | 大小 | 所需 RAM |
|--------|------|------------|
| Q2_K | 5.1 GB | 8 GB |
| Q3_K_M | 6.2 GB | 10 GB |
| Q4_K_M | 7.9 GB | 12 GB |
| Q5_K_M | 9.2 GB | 14 GB |
| Q6_K | 10.7 GB | 16 GB |

### 70B 参数模型

| 格式 | 大小 | 所需 RAM |
|--------|------|------------|
| Q2_K | 26 GB | 32 GB |
| Q3_K_M | 32 GB | 40 GB |
| Q4_K_M | 41 GB | 48 GB |
| Q4_K_S | 39 GB | 46 GB |
| Q5_K_M | 48 GB | 56 GB |

**70B 的建议**：使用 Q3_K_M 或 Q4_K_S 以适配消费级硬件。

## 查找已预量化的模型

使用带 llama.cpp 应用过滤器的 Hub 搜索：

```text
https://huggingface.co/models?apps=llama.cpp&sort=trending
https://huggingface.co/models?search=<term>&apps=llama.cpp&sort=trending
https://huggingface.co/models?search=<term>&apps=llama.cpp&num_parameters=min:0,max:24B&sort=trending
```

对于特定仓库，打开：

```text
https://huggingface.co/<repo>?local-app=llama.cpp
https://huggingface.co/api/models/<repo>/tree/main?recursive=true
```

然后无需额外的 Hub 工具，直接从 Hub 启动：

```bash
llama-cli -hf <repo>:Q4_K_M
llama-server -hf <repo>:Q4_K_M
```

如果你需要从 tree API 获取精确文件名：

```bash
llama-server --hf-repo <repo> --hf-file <filename.gguf>
```

## 重要性矩阵（imatrix）

**是什么**：用于提升量化质量的校准数据。

**好处**：
- 对 Q4 带来 10-20% 的困惑度改善
- 对 Q3 及更低量化必不可少

**用法**：
```bash
# 1. 生成重要性矩阵
./llama-imatrix \
    -m model-f16.gguf \
    -f calibration-data.txt \
    -o model.imatrix

# 2. 带 imatrix 量化
./llama-quantize \
    --imatrix model.imatrix \
    model-f16.gguf \
    model-Q4_K_M.gguf \
    Q4_K_M
```

**校准数据**：
- 使用领域特定文本（例如对代码模型用代码）
- 约 100MB 有代表性的文本
- 数据质量越高 = 量化越好

## 故障排查

**模型输出乱码**：
- 量化过于激进（Q2_K）
- 尝试 Q4_K_M 或 Q5_K_M
- 验证模型是否正确转换

**内存不足**：
- 使用更低量化（用 Q4_K_S 代替 Q5_K_M）
- 减少卸载到 GPU 的层数（`-ngl`）
- 使用更小的上下文（`-c 2048`）

**推理缓慢**：
- 更高量化使用更多算力
- Q8_0 比 Q4_K_M 慢得多
- 考虑速度与质量的权衡
