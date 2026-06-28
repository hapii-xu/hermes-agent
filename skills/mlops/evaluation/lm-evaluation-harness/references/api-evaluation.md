# API 评估

评估 OpenAI、Anthropic 及其他基于 API 的语言模型的指南。

## 概览

lm-evaluation-harness 通过统一的 `TemplateAPI` 接口支持评估基于 API 的模型。这允许对以下对象做基准测试：
- OpenAI 模型（GPT-4、GPT-3.5 等）
- Anthropic 模型（Claude 3、Claude 2 等）
- 本地 OpenAI 兼容 API
- 自定义 API 端点

**为什么要评估 API 模型**：
- 对闭源模型做基准测试
- 把 API 模型和开源模型做比较
- 验证 API 性能
- 随时间跟踪模型更新

## 支持的 API 模型

| 提供方 | 模型类型 | 请求类型 | Logprobs |
|----------|------------|---------------|----------|
| OpenAI（completions） | `openai-completions` | 全部 | ✅ 支持 |
| OpenAI（chat） | `openai-chat-completions` | 仅 `generate_until` | ❌ 不支持 |
| Anthropic（completions） | `anthropic-completions` | 全部 | ❌ 不支持 |
| Anthropic（chat） | `anthropic-chat` | 仅 `generate_until` | ❌ 不支持 |
| 本地（OpenAI 兼容） | `local-completions` | 取决于服务端 | 视情况而定 |

**注意**：不支持 logprobs 的模型只能在生成类任务上评估，不能用于困惑度（perplexity）或对数似然（loglikelihood）任务。

## OpenAI 模型

### 设置

```bash
export OPENAI_API_KEY=sk-...
```

### Completion 模型（旧版）

**可用模型**：`davinci-002`、`babbage-002`

```bash
lm_eval --model openai-completions \
  --model_args model=davinci-002 \
  --tasks lambada_openai,hellaswag \
  --batch_size auto
```

**支持**：
- `generate_until`：✅
- `loglikelihood`：✅
- `loglikelihood_rolling`：✅

### Chat 模型

**可用模型**：`gpt-4`、`gpt-4-turbo`、`gpt-3.5-turbo`

```bash
lm_eval --model openai-chat-completions \
  --model_args model=gpt-4-turbo \
  --tasks mmlu,gsm8k,humaneval \
  --num_fewshot 5 \
  --batch_size auto
```

**支持**：
- `generate_until`：✅
- `loglikelihood`：❌（无 logprobs）
- `loglikelihood_rolling`：❌

**重要**：Chat 模型不提供 logprobs，所以只能用于生成类任务（MMLU、GSM8K、HumanEval），不能用于困惑度任务。

### 配置选项

```bash
lm_eval --model openai-chat-completions \
  --model_args \
    model=gpt-4-turbo,\
    base_url=https://api.openai.com/v1,\
    num_concurrent=5,\
    max_retries=3,\
    timeout=60,\
    batch_size=auto
```

**参数**：
- `model`：模型标识符（必填）
- `base_url`：API 端点（默认：OpenAI）
- `num_concurrent`：并发请求数（默认：5）
- `max_retries`：失败请求重试次数（默认：3）
- `timeout`：请求超时秒数（默认：60）
- `tokenizer`：要使用的分词器（默认：与模型匹配）
- `tokenizer_backend`：`"tiktoken"` 或 `"huggingface"`

### 成本管理

OpenAI 按 token 计费。运行前先估算成本：

```python
# 粗略估算
num_samples = 1000
avg_tokens_per_sample = 500  # 输入 + 输出
cost_per_1k_tokens = 0.01  # GPT-3.5 Turbo

total_cost = (num_samples * avg_tokens_per_sample / 1000) * cost_per_1k_tokens
print(f"Estimated cost: ${total_cost:.2f}")
```

**省钱技巧**：
- 测试时用 `--limit N`
- 先从 `gpt-3.5-turbo` 起步，再用 `gpt-4`
- 把 `max_gen_toks` 设到所需的最小值
- 可能时用 `num_fewshot=0` 做 zero-shot

## Anthropic 模型

### 设置

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

### Completion 模型（旧版）

```bash
lm_eval --model anthropic-completions \
  --model_args model=claude-2.1 \
  --tasks lambada_openai,hellaswag \
  --batch_size auto
```

### Chat 模型（推荐）

**可用模型**：`claude-3-5-sonnet-20241022`、`claude-3-opus-20240229`、`claude-3-sonnet-20240229`、`claude-3-haiku-20240307`

```bash
lm_eval --model anthropic-chat \
  --model_args model=claude-3-5-sonnet-20241022 \
  --tasks mmlu,gsm8k,humaneval \
  --num_fewshot 5 \
  --batch_size auto
```

**别名**：`anthropic-chat-completions`（与 `anthropic-chat` 相同）

### 配置选项

```bash
lm_eval --model anthropic-chat \
  --model_args \
    model=claude-3-5-sonnet-20241022,\
    base_url=https://api.anthropic.com,\
    num_concurrent=5,\
    max_retries=3,\
    timeout=60
```

### 成本管理

Anthropic 定价（截至 2024 年）：
- Claude 3.5 Sonnet：$3.00 / 百万输入，$15.00 / 百万输出
- Claude 3 Opus：$15.00 / 百万输入，$75.00 / 百万输出
- Claude 3 Haiku：$0.25 / 百万输入，$1.25 / 百万输出

**预算友好的策略**：
```bash
# 先在小样本上测试
lm_eval --model anthropic-chat \
  --model_args model=claude-3-haiku-20240307 \
  --tasks mmlu \
  --limit 100

# 然后用最好的模型跑完整评估
lm_eval --model anthropic-chat \
  --model_args model=claude-3-5-sonnet-20241022 \
  --tasks mmlu \
  --num_fewshot 5
```

## 本地 OpenAI 兼容 API

很多本地推理服务都暴露 OpenAI 兼容 API（vLLM、Text Generation Inference、llama.cpp、Ollama）。

### vLLM 本地服务

**启动服务**：
```bash
vllm serve meta-llama/Llama-2-7b-hf \
  --host 0.0.0.0 \
  --port 8000
```

**评估**：
```bash
lm_eval --model local-completions \
  --model_args \
    model=meta-llama/Llama-2-7b-hf,\
    base_url=http://localhost:8000/v1,\
    num_concurrent=1 \
  --tasks mmlu,gsm8k \
  --batch_size auto
```

### Text Generation Inference (TGI)

**启动服务**：
```bash
docker run --gpus all --shm-size 1g -p 8080:80 \
  ghcr.io/huggingface/text-generation-inference:latest \
  --model-id meta-llama/Llama-2-7b-hf
```

**评估**：
```bash
lm_eval --model local-completions \
  --model_args \
    model=meta-llama/Llama-2-7b-hf,\
    base_url=http://localhost:8080/v1 \
  --tasks hellaswag,arc_challenge
```

### Ollama

**启动服务**：
```bash
ollama serve
ollama pull llama2:7b
```

**评估**：
```bash
lm_eval --model local-completions \
  --model_args \
    model=llama2:7b,\
    base_url=http://localhost:11434/v1 \
  --tasks mmlu
```

### llama.cpp 服务

**启动服务**：
```bash
./server -m models/llama-2-7b.gguf --host 0.0.0.0 --port 8080
```

**评估**：
```bash
lm_eval --model local-completions \
  --model_args \
    model=llama2,\
    base_url=http://localhost:8080/v1 \
  --tasks gsm8k
```

## 自定义 API 实现

对于自定义 API 端点，子类化 `TemplateAPI`：

### 创建 `my_api.py`

```python
from lm_eval.models.api_models import TemplateAPI
import requests

class MyCustomAPI(TemplateAPI):
    """自定义 API 模型。"""

    def __init__(self, base_url, api_key, **kwargs):
        super().__init__(base_url=base_url, **kwargs)
        self.api_key = api_key

    def _create_payload(self, messages, gen_kwargs):
        """创建 API 请求载荷。"""
        return {
            "messages": messages,
            "api_key": self.api_key,
            **gen_kwargs
        }

    def parse_generations(self, response):
        """解析生成响应。"""
        return response.json()["choices"][0]["text"]

    def parse_logprobs(self, response):
        """解析 logprobs（如果可用）。"""
        # 如果 API 不提供 logprobs，返回 None
        logprobs = response.json().get("logprobs")
        if logprobs:
            return logprobs["token_logprobs"]
        return None
```

### 注册并使用

```python
from lm_eval import evaluator
from my_api import MyCustomAPI

model = MyCustomAPI(
    base_url="https://api.example.com/v1",
    api_key="your-key"
)

results = evaluator.simple_evaluate(
    model=model,
    tasks=["mmlu", "gsm8k"],
    num_fewshot=5,
    batch_size="auto"
)
```

## 比较 API 模型和开源模型

### 并排评估

```bash
# 评估 OpenAI GPT-4
lm_eval --model openai-chat-completions \
  --model_args model=gpt-4-turbo \
  --tasks mmlu,gsm8k,hellaswag \
  --num_fewshot 5 \
  --output_path results/gpt4.json

# 评估开源 Llama 2 70B
lm_eval --model hf \
  --model_args pretrained=meta-llama/Llama-2-70b-hf,dtype=bfloat16 \
  --tasks mmlu,gsm8k,hellaswag \
  --num_fewshot 5 \
  --output_path results/llama2-70b.json

# 比较结果
python scripts/compare_results.py \
  results/gpt4.json \
  results/llama2-70b.json
```

### 典型对比

| 模型 | MMLU | GSM8K | HumanEval | 成本 |
|-------|------|-------|-----------|------|
| GPT-4 Turbo | 86.4% | 92.0% | 67.0% | $$$$ |
| Claude 3 Opus | 86.8% | 95.0% | 84.9% | $$$$ |
| GPT-3.5 Turbo | 70.0% | 57.1% | 48.1% | $$ |
| Llama 2 70B | 68.9% | 56.8% | 29.9% | 免费（自托管） |
| Mixtral 8x7B | 70.6% | 58.4% | 40.2% | 免费（自托管） |

## 最佳实践

### 速率限制

遵守 API 速率限制：
```bash
lm_eval --model openai-chat-completions \
  --model_args \
    model=gpt-4-turbo,\
    num_concurrent=3,\  # 更低的并发
    timeout=120 \  # 更长的超时
  --tasks mmlu
```

### 可复现性

把 temperature 设为 0 以获得确定性结果：
```bash
lm_eval --model openai-chat-completions \
  --model_args model=gpt-4-turbo \
  --tasks mmlu \
  --gen_kwargs temperature=0.0
```

或用 `seed` 做采样：
```bash
lm_eval --model anthropic-chat \
  --model_args model=claude-3-5-sonnet-20241022 \
  --tasks gsm8k \
  --gen_kwargs temperature=0.7,seed=42
```

### 缓存

API 模型会自动缓存响应，避免冗余调用：
```bash
# 第一次运行：发起 API 调用
lm_eval --model openai-chat-completions \
  --model_args model=gpt-4-turbo \
  --tasks mmlu \
  --limit 100

# 第二次运行：使用缓存（即时，免费）
lm_eval --model openai-chat-completions \
  --model_args model=gpt-4-turbo \
  --tasks mmlu \
  --limit 100
```

缓存位置：`~/.cache/lm_eval/`

### 错误处理

API 可能失败。使用重试：
```bash
lm_eval --model openai-chat-completions \
  --model_args \
    model=gpt-4-turbo,\
    max_retries=5,\
    timeout=120 \
  --tasks mmlu
```

## 故障排查

### 「Authentication failed」（认证失败）

检查 API key：
```bash
echo $OPENAI_API_KEY  # 应打印 sk-...
echo $ANTHROPIC_API_KEY  # 应打印 sk-ant-...
```

### 「Rate limit exceeded」（超出速率限制）

降低并发：
```bash
--model_args num_concurrent=1
```

或 在请求之间加延迟。

### 「Timeout error」（超时错误）

增加超时：
```bash
--model_args timeout=180
```

### 「Model not found」（找不到模型）

对于本地 API，验证服务是否在运行：
```bash
curl http://localhost:8000/v1/models
```

### 成本失控

测试时用 `--limit`：
```bash
lm_eval --model openai-chat-completions \
  --model_args model=gpt-4-turbo \
  --tasks mmlu \
  --limit 50  # 仅 50 个样本
```

## 高级功能

### 自定义请求头

```bash
lm_eval --model local-completions \
  --model_args \
    base_url=http://api.example.com/v1,\
    header="Authorization: Bearer token,X-Custom: value"
```

### 禁用 SSL 验证（仅用于开发）

```bash
lm_eval --model local-completions \
  --model_args \
    base_url=https://localhost:8000/v1,\
    verify_certificate=false
```

### 自定义分词器

```bash
lm_eval --model openai-chat-completions \
  --model_args \
    model=gpt-4-turbo,\
    tokenizer=gpt2,\
    tokenizer_backend=huggingface
```

## 参考

- OpenAI API：https://platform.openai.com/docs/api-reference
- Anthropic API：https://docs.anthropic.com/claude/reference
- TemplateAPI：`lm_eval/models/api_models.py`
- OpenAI 模型：`lm_eval/models/openai_completions.py`
- Anthropic 模型：`lm_eval/models/anthropic_llms.py`
