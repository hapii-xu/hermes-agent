# 量化指南

## 目录
- 量化方法对比
- AWQ 设置与使用
- GPTQ 设置与使用
- FP8 量化（H100）
- 模型准备
- 精度与压缩的折衷

## 量化方法对比

| 方法 | 压缩率 | 精度损失 | 速度 | 最适用于 |
|--------|-------------|---------------|-------|----------|
| **AWQ** | 4-bit (75%) | <1% | 快 | 70B 模型、生产环境 |
| **GPTQ** | 4-bit (75%) | 1-2% | 快 | 广泛的模型支持 |
| **FP8** | 8-bit (50%) | <0.5% | 最快 | 仅限 H100 GPU |
| **SqueezeLLM** | 3-4 bit (75-80%) | 2-3% | 中等 | 极致压缩 |

**建议**：
- **生产环境**：对 70B 模型使用 AWQ
- **H100 GPU**：使用 FP8 获得最佳速度
- **最大兼容性**：使用 GPTQ
- **极致压缩**：使用 SqueezeLLM

## AWQ 设置与使用

**AWQ**（Activation-aware Weight Quantization，激活感知权重量化）在 4-bit 下精度最佳。

**第 1 步：查找预量化模型**

在 HuggingFace 上搜索 AWQ 模型：
```bash
# 例如：TheBloke/Llama-2-70B-AWQ
# 例如：TheBloke/Mixtral-8x7B-Instruct-v0.1-AWQ
```

**第 2 步：使用 AWQ 启动**

```bash
vllm serve TheBloke/Llama-2-70B-AWQ \
  --quantization awq \
  --tensor-parallel-size 1 \
  --gpu-memory-utilization 0.95
```

**显存节省**：
```
Llama 2 70B fp16：140GB VRAM（需要 4x A100）
Llama 2 70B AWQ：35GB VRAM（1x A100 40GB）
= 显存减少 4 倍
```

**第 3 步：验证性能**

测试输出是否可接受：
```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="EMPTY")

# 测试复杂推理
response = client.chat.completions.create(
    model="TheBloke/Llama-2-70B-AWQ",
    messages=[{"role": "user", "content": "Explain quantum entanglement"}]
)

print(response.choices[0].message.content)
# 验证质量是否满足你的要求
```

**量化你自己的模型**（需要 80GB+ VRAM 的 GPU）：

```python
from awq import AutoAWQForCausalLM
from transformers import AutoTokenizer

model_path = "meta-llama/Llama-2-70b-hf"
quant_path = "llama-2-70b-awq"

# 加载模型
model = AutoAWQForCausalLM.from_pretrained(model_path)
tokenizer = AutoTokenizer.from_pretrained(model_path)

# 量化
quant_config = {"zero_point": True, "q_group_size": 128, "w_bit": 4}
model.quantize(tokenizer, quant_config=quant_config)

# 保存
model.save_quantized(quant_path)
tokenizer.save_pretrained(quant_path)
```

## GPTQ 设置与使用

**GPTQ** 拥有最广的模型支持和良好的压缩效果。

**第 1 步：查找 GPTQ 模型**

```bash
# 例如：TheBloke/Llama-2-13B-GPTQ
# 例如：TheBloke/CodeLlama-34B-GPTQ
```

**第 2 步：使用 GPTQ 启动**

```bash
vllm serve TheBloke/Llama-2-13B-GPTQ \
  --quantization gptq \
  --dtype float16
```

**GPTQ 配置选项**：
```bash
# 如有需要，指定 GPTQ 参数
vllm serve MODEL \
  --quantization gptq \
  --gptq-act-order \  # 激活顺序
  --dtype float16
```

**量化你自己的模型**：

```python
from auto_gptq import AutoGPTQForCausalLM, BaseQuantizeConfig
from transformers import AutoTokenizer

model_name = "meta-llama/Llama-2-13b-hf"
quantized_name = "llama-2-13b-gptq"

# 加载模型
tokenizer = AutoTokenizer.from_pretrained(model_name)
model = AutoGPTQForCausalLM.from_pretrained(model_name, quantize_config)

# 准备校准数据
calib_data = [...]  # 样本文本列表

# 量化
quantize_config = BaseQuantizeConfig(
    bits=4,
    group_size=128,
    desc_act=True
)
model.quantize(calib_data)

# 保存
model.save_quantized(quantized_name)
```

## FP8 量化（H100）

**FP8**（8-bit 浮点）在 H100 GPU 上速度最佳，精度损失极小。

**要求**：
- H100 或 H800 GPU
- CUDA 12.3+（推荐 12.8）
- Hopper 架构支持

**第 1 步：启用 FP8**

```bash
vllm serve meta-llama/Llama-3-70B-Instruct \
  --quantization fp8 \
  --tensor-parallel-size 2
```

**在 H100 上的性能提升**：
```
fp16：180 tokens/sec
FP8：320 tokens/sec
= 1.8 倍加速
```

**第 2 步：验证精度**

FP8 通常精度下降 <0.5%：
```python
# 运行评测套件
# 在你的任务上对比 FP8 与 FP16
# 验证精度可接受
```

**动态 FP8 量化**（无需预量化模型）：

```bash
# vLLM 在运行时自动量化
vllm serve MODEL --quantization fp8
# 无需准备模型
```

## 模型准备

**预量化模型（最简单）**：

1. 在 HuggingFace 搜索：`[model name] AWQ` 或 `[model name] GPTQ`
2. 下载或直接使用：`TheBloke/[Model]-AWQ`
3. 用对应的 `--quantization` 标志启动

**量化你自己的模型**：

**AWQ**：
```bash
# 安装 AutoAWQ
pip install autoawq

# 运行量化脚本
python quantize_awq.py --model MODEL --output OUTPUT
```

**GPTQ**：
```bash
# 安装 AutoGPTQ
pip install auto-gptq

# 运行量化脚本
python quantize_gptq.py --model MODEL --output OUTPUT
```

**校准数据**：
- 使用来自目标领域的 128-512 个多样化样本
- 应能代表生产环境的输入
- 校准数据质量越高 = 精度越好

## 精度与压缩的折衷

**实测结果**（Llama 2 70B 在 MMLU 基准上）：

| 量化方式 | 精度 | 显存 | 速度 | 是否生产就绪 |
|--------------|----------|--------|-------|------------------|
| FP16（基线） | 100% | 140GB | 1.0x | ✅（显存充足时） |
| FP8 | 99.5% | 70GB | 1.8x | ✅（仅 H100） |
| AWQ 4-bit | 99.0% | 35GB | 1.5x | ✅（70B 模型最佳） |
| GPTQ 4-bit | 98.5% | 35GB | 1.5x | ✅（兼容性好） |
| SqueezeLLM 3-bit | 96.0% | 26GB | 1.3x | ⚠️（需核对精度） |

**何时使用哪种方案**：

**不量化（FP16）**：
- GPU 显存充足
- 需要绝对最佳精度
- 模型 <13B 参数

**FP8**：
- 使用 H100/H800 GPU
- 需要在精度损失极小的前提下获得最佳速度
- 生产部署

**AWQ 4-bit**：
- 需要把 70B 模型塞进 40GB GPU
- 生产部署
- 可接受 <1% 的精度损失

**GPTQ 4-bit**：
- 需要广泛的模型支持
- 不在 H100 上（改用 FP8）
- 可接受 1-2% 的精度损失

**测试策略**：

1. **基线**：在你的评测集上测量 FP16 精度
2. **量化**：创建量化版本
3. **评测**：在相同任务上对比量化版与基线
4. **决策**：当下降 < 阈值（通常 1-2%）时接受

**评测示例**：
```python
from evaluate import load_evaluation_suite

# 在 FP16 基线上运行
baseline_score = evaluate(model_fp16, eval_suite)

# 在量化版本上运行
quant_score = evaluate(model_awq, eval_suite)

# 对比
degradation = (baseline_score - quant_score) / baseline_score * 100
print(f"Accuracy degradation: {degradation:.2f}%")

# 决策
if degradation < 1.0:
    print("✅ Quantization acceptable for production")
else:
    print("⚠️ Review accuracy loss")
```
