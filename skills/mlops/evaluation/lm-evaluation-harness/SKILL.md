---
name: evaluating-llms-harness
description: "lm-eval-harness：对 LLM 进行基准测试（MMLU、GSM8K 等）。"
version: 1.0.0
author: Orchestra Research
license: MIT
dependencies: [lm-eval, transformers, vllm]
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Evaluation, LM Evaluation Harness, Benchmarking, MMLU, HumanEval, GSM8K, EleutherAI, Model Quality, Academic Benchmarks, Industry Standard]

---

# lm-evaluation-harness - LLM 基准测试

## 内含内容

跨 60+ 学术基准（MMLU、HumanEval、GSM8K、TruthfulQA、HellaSwag）评估 LLM。在对模型质量做基准测试、比较模型、报告学术结果或跟踪训练进度时使用。这是被 EleutherAI、HuggingFace 和各大实验室采用的行业标准。支持 HuggingFace、vLLM、API。

## 快速开始

lm-evaluation-harness 使用标准化的提示词和指标，跨 60+ 学术基准评估 LLM。

**安装**：
```bash
pip install lm-eval
```

**评估任意 HuggingFace 模型**：
```bash
lm_eval --model hf \
  --model_args pretrained=meta-llama/Llama-2-7b-hf \
  --tasks mmlu,gsm8k,hellaswag \
  --device cuda:0 \
  --batch_size 8
```

**查看可用任务**：
```bash
lm_eval --tasks list
```

## 常见工作流

### 工作流 1：标准基准评估

在核心基准（MMLU、GSM8K、HumanEval）上评估模型。

复制此清单：

```
基准评估：
- [ ] 步骤 1：选择基准套件
- [ ] 步骤 2：配置模型
- [ ] 步骤 3：运行评估
- [ ] 步骤 4：分析结果
```

**步骤 1：选择基准套件**

**核心推理基准**：
- **MMLU**（Massive Multitask Language Understanding，大规模多任务语言理解）—— 57 个学科，选择题
- **GSM8K** —— 小学数学应用题
- **HellaSwag** —— 常识推理
- **TruthfulQA** —— 真实性与事实性
- **ARC**（AI2 Reasoning Challenge，AI2 推理挑战）—— 科学问题

**代码基准**：
- **HumanEval** —— Python 代码生成（164 道题）
- **MBPP**（Mostly Basic Python Problems，基本 Python 问题集）—— Python 编码

**标准套件**（推荐用于模型发布）：
```bash
--tasks mmlu,gsm8k,hellaswag,truthfulqa,arc_challenge
```

**步骤 2：配置模型**

**HuggingFace 模型**：
```bash
lm_eval --model hf \
  --model_args pretrained=meta-llama/Llama-2-7b-hf,dtype=bfloat16 \
  --tasks mmlu \
  --device cuda:0 \
  --batch_size auto  # 自动探测最优 batch size
```

**量化模型（4-bit/8-bit）**：
```bash
lm_eval --model hf \
  --model_args pretrained=meta-llama/Llama-2-7b-hf,load_in_4bit=True \
  --tasks mmlu \
  --device cuda:0
```

**自定义 checkpoint**：
```bash
lm_eval --model hf \
  --model_args pretrained=/path/to/my-model,tokenizer=/path/to/tokenizer \
  --tasks mmlu \
  --device cuda:0
```

**步骤 3：运行评估**

```bash
# 完整 MMLU 评估（57 个学科）
lm_eval --model hf \
  --model_args pretrained=meta-llama/Llama-2-7b-hf \
  --tasks mmlu \
  --num_fewshot 5 \  # 5-shot 评估（标准）
  --batch_size 8 \
  --output_path results/ \
  --log_samples  # 保存每条预测

# 一次跑多个基准
lm_eval --model hf \
  --model_args pretrained=meta-llama/Llama-2-7b-hf \
  --tasks mmlu,gsm8k,hellaswag,truthfulqa,arc_challenge \
  --num_fewshot 5 \
  --batch_size 8 \
  --output_path results/llama2-7b-eval.json
```

**步骤 4：分析结果**

结果保存到 `results/llama2-7b-eval.json`：

```json
{
  "results": {
    "mmlu": {
      "acc": 0.459,
      "acc_stderr": 0.004
    },
    "gsm8k": {
      "exact_match": 0.142,
      "exact_match_stderr": 0.006
    },
    "hellaswag": {
      "acc_norm": 0.765,
      "acc_norm_stderr": 0.004
    }
  },
  "config": {
    "model": "hf",
    "model_args": "pretrained=meta-llama/Llama-2-7b-hf",
    "num_fewshot": 5
  }
}
```

### 工作流 2：跟踪训练进度

在训练过程中评估 checkpoint。

```
训练进度跟踪：
- [ ] 步骤 1：设置周期性评估
- [ ] 步骤 2：选择快速基准
- [ ] 步骤 3：自动化评估
- [ ] 步骤 4：绘制学习曲线
```

**步骤 1：设置周期性评估**

每 N 个训练步评估一次：

```bash
#!/bin/bash
# eval_checkpoint.sh

CHECKPOINT_DIR=$1
STEP=$2

lm_eval --model hf \
  --model_args pretrained=$CHECKPOINT_DIR/checkpoint-$STEP \
  --tasks gsm8k,hellaswag \
  --num_fewshot 0 \  # 为速度用 0-shot
  --batch_size 16 \
  --output_path results/step-$STEP.json
```

**步骤 2：选择快速基准**

适合频繁评估的快速基准：
- **HellaSwag**：在 1 张 GPU 上约 10 分钟
- **GSM8K**：约 5 分钟
- **PIQA**：约 2 分钟

避免用于频繁评估（太慢）：
- **MMLU**：约 2 小时（57 个学科）
- **HumanEval**：需要执行代码

**步骤 3：自动化评估**

集成到训练脚本：

```python
# 在训练循环里
if step % eval_interval == 0:
    model.save_pretrained(f"checkpoints/step-{step}")

    # 运行评估
    os.system(f"./eval_checkpoint.sh checkpoints step-{step}")
```

或者用 PyTorch Lightning 回调：

```python
from pytorch_lightning import Callback

class EvalHarnessCallback(Callback):
    def on_validation_epoch_end(self, trainer, pl_module):
        step = trainer.global_step
        checkpoint_path = f"checkpoints/step-{step}"

        # 保存 checkpoint
        trainer.save_checkpoint(checkpoint_path)

        # 运行 lm-eval
        os.system(f"lm_eval --model hf --model_args pretrained={checkpoint_path} ...")
```

**步骤 4：绘制学习曲线**

```python
import json
import matplotlib.pyplot as plt

# 加载所有结果
steps = []
mmlu_scores = []

for file in sorted(glob.glob("results/step-*.json")):
    with open(file) as f:
        data = json.load(f)
        step = int(file.split("-")[1].split(".")[0])
        steps.append(step)
        mmlu_scores.append(data["results"]["mmlu"]["acc"])

# 绘图
plt.plot(steps, mmlu_scores)
plt.xlabel("Training Step")
plt.ylabel("MMLU Accuracy")
plt.title("Training Progress")
plt.savefig("training_curve.png")
```

### 工作流 3：比较多个模型

用于模型比较的基准套件。

```
模型比较：
- [ ] 步骤 1：定义模型清单
- [ ] 步骤 2：运行评估
- [ ] 步骤 3：生成对比表
```

**步骤 1：定义模型清单**

```bash
# models.txt
meta-llama/Llama-2-7b-hf
meta-llama/Llama-2-13b-hf
mistralai/Mistral-7B-v0.1
microsoft/phi-2
```

**步骤 2：运行评估**

```bash
#!/bin/bash
# eval_all_models.sh

TASKS="mmlu,gsm8k,hellaswag,truthfulqa"

while read model; do
    echo "Evaluating $model"

    # 提取模型名用于输出文件
    model_name=$(echo $model | sed 's/\//-/g')

    lm_eval --model hf \
      --model_args pretrained=$model,dtype=bfloat16 \
      --tasks $TASKS \
      --num_fewshot 5 \
      --batch_size auto \
      --output_path results/$model_name.json

done < models.txt
```

**步骤 3：生成对比表**

```python
import json
import pandas as pd

models = [
    "meta-llama-Llama-2-7b-hf",
    "meta-llama-Llama-2-13b-hf",
    "mistralai-Mistral-7B-v0.1",
    "microsoft-phi-2"
]

tasks = ["mmlu", "gsm8k", "hellaswag", "truthfulqa"]

results = []
for model in models:
    with open(f"results/{model}.json") as f:
        data = json.load(f)
        row = {"Model": model.replace("-", "/")}
        for task in tasks:
            # 取每个任务的主指标
            metrics = data["results"][task]
            if "acc" in metrics:
                row[task.upper()] = f"{metrics['acc']:.3f}"
            elif "exact_match" in metrics:
                row[task.upper()] = f"{metrics['exact_match']:.3f}"
        results.append(row)

df = pd.DataFrame(results)
print(df.to_markdown(index=False))
```

输出：
```
| Model                  | MMLU  | GSM8K | HELLASWAG | TRUTHFULQA |
|------------------------|-------|-------|-----------|------------|
| meta-llama/Llama-2-7b  | 0.459 | 0.142 | 0.765     | 0.391      |
| meta-llama/Llama-2-13b | 0.549 | 0.287 | 0.801     | 0.430      |
| mistralai/Mistral-7B   | 0.626 | 0.395 | 0.812     | 0.428      |
| microsoft/phi-2        | 0.560 | 0.613 | 0.682     | 0.447      |
```

### 工作流 4：用 vLLM 评估（更快的推理）

用 vLLM 后端获得 5-10 倍加速。

```
vLLM 评估：
- [ ] 步骤 1：安装 vLLM
- [ ] 步骤 2：配置 vLLM 后端
- [ ] 步骤 3：运行评估
```

**步骤 1：安装 vLLM**

```bash
pip install vllm
```

**步骤 2：配置 vLLM 后端**

```bash
lm_eval --model vllm \
  --model_args pretrained=meta-llama/Llama-2-7b-hf,tensor_parallel_size=1,dtype=auto,gpu_memory_utilization=0.8 \
  --tasks mmlu \
  --batch_size auto
```

**步骤 3：运行评估**

vLLM 比标准 HuggingFace 快 5-10×：

```bash
# 标准 HF：7B 模型跑 MMLU 约 2 小时
lm_eval --model hf \
  --model_args pretrained=meta-llama/Llama-2-7b-hf \
  --tasks mmlu \
  --batch_size 8

# vLLM：7B 模型跑 MMLU 约 15-20 分钟
lm_eval --model vllm \
  --model_args pretrained=meta-llama/Llama-2-7b-hf,tensor_parallel_size=2 \
  --tasks mmlu \
  --batch_size auto
```

## 何时使用 vs 替代方案

**使用 lm-evaluation-harness 的场景：**
- 为学术论文给模型做基准测试
- 跨标准任务比较模型质量
- 跟踪训练进度
- 报告标准化指标（大家都用同样的提示词）
- 需要可复现的评估

**改用替代方案：**
- **HELM**（斯坦福）：更广的评估（公平性、效率、校准）
- **AlpacaEval**：带 LLM 评审的指令遵循评估
- **MT-Bench**：对话式多轮评估
- **自定义脚本**：领域专用评估

## 常见问题

**问题：评估太慢**

用 vLLM 后端：
```bash
lm_eval --model vllm \
  --model_args pretrained=model-name,tensor_parallel_size=2
```

或减少 fewshot 示例：
```bash
--num_fewshot 0  # 而非 5
```

或评估 MMLU 的子集：
```bash
--tasks mmlu_stem  # 仅 STEM 学科
```

**问题：内存不足**

降低 batch size：
```bash
--batch_size 1  # 或 --batch_size auto
```

使用量化：
```bash
--model_args pretrained=model-name,load_in_8bit=True
```

启用 CPU 卸载：
```bash
--model_args pretrained=model-name,device_map=auto,offload_folder=offload
```

**问题：结果与报告不一致**

检查 fewshot 数量：
```bash
--num_fewshot 5  # 大多数论文用 5-shot
```

检查确切任务名：
```bash
--tasks mmlu  # 不是 mmlu_direct 或 mmlu_fewshot
```

验证模型和分词器匹配：
```bash
--model_args pretrained=model-name,tokenizer=same-model-name
```

**问题：HumanEval 不执行代码**

安装执行依赖：
```bash
pip install human-eval
```

启用代码执行：
```bash
lm_eval --model hf \
  --model_args pretrained=model-name \
  --tasks humaneval \
  --allow_code_execution  # HumanEval 必需
```

## 进阶主题

**基准说明**：所有 60+ 任务的详细说明、它们测量什么以及如何解读，见 [references/benchmark-guide.md](references/benchmark-guide.md)。

**自定义任务**：创建领域专用评估任务见 [references/custom-tasks.md](references/custom-tasks.md)。

**API 评估**：评估 OpenAI、Anthropic 及其他 API 模型见 [references/api-evaluation.md](references/api-evaluation.md)。

**多 GPU 策略**：数据并行和张量并行的评估见 [references/distributed-eval.md](references/distributed-eval.md)。

## 硬件要求

- **GPU**：NVIDIA（CUDA 11.8+），可在 CPU 上跑（非常慢）
- **VRAM**：
  - 7B 模型：16GB（bf16）或 8GB（8-bit）
  - 13B 模型：28GB（bf16）或 14GB（8-bit）
  - 70B 模型：需要多 GPU 或量化
- **耗时**（7B 模型，单张 A100）：
  - HellaSwag：10 分钟
  - GSM8K：5 分钟
  - MMLU（完整）：2 小时
  - HumanEval：20 分钟

## 资源

- GitHub：https://github.com/EleutherAI/lm-evaluation-harness
- 文档：https://github.com/EleutherAI/lm-evaluation-harness/tree/main/docs
- 任务库：60+ 任务，包括 MMLU、GSM8K、HumanEval、TruthfulQA、HellaSwag、ARC、WinoGrande 等
- 排行榜：https://huggingface.co/spaces/HuggingFaceH4/open_llm_leaderboard（使用本 harness）


