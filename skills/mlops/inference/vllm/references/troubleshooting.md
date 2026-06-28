# 故障排查指南

## 目录
- 内存不足（OOM）错误
- 性能问题
- 模型加载错误
- 网络与连接问题
- 量化问题
- 分布式服务问题
- 调试工具与命令

## 内存不足（OOM）错误

### 症状：模型加载期间出现 `torch.cuda.OutOfMemoryError`

**原因**：模型 + KV cache 超出了可用 VRAM

**解决方案（按顺序尝试）**：

1. **降低 GPU 显存利用率**：
```bash
vllm serve MODEL --gpu-memory-utilization 0.7  # 尝试 0.7、0.75、0.8
```

2. **减小最大序列长度**：
```bash
vllm serve MODEL --max-model-len 4096  # 而不是 8192
```

3. **启用量化**：
```bash
vllm serve MODEL --quantization awq  # 显存减少 4 倍
```

4. **使用张量并行**（多 GPU）：
```bash
vllm serve MODEL --tensor-parallel-size 2  # 拆分到 2 块 GPU
```

5. **降低最大并发序列数**：
```bash
vllm serve MODEL --max-num-seqs 128  # 默认为 256
```

### 症状：推理期间 OOM（非模型加载阶段）

**原因**：生成过程中 KV cache 被填满

**解决方案**：

```bash
# 降低 KV cache 分配
vllm serve MODEL --gpu-memory-utilization 0.85

# 降低批大小
vllm serve MODEL --max-num-seqs 64

# 降低每请求的最大 token 数
# 在客户端请求中设置：max_tokens=512
```

### 症状：量化模型出现 OOM

**原因**：量化开销或配置不正确

**解决方案**：
```bash
# 确保量化标志与模型匹配
vllm serve TheBloke/Llama-2-70B-AWQ --quantization awq  # 必须显式指定

# 尝试不同的 dtype
vllm serve MODEL --quantization awq --dtype float16
```

## 性能问题

### 症状：吞吐量低（<50 req/sec，预期 >100）

**诊断步骤**：

1. **检查 GPU 利用率**：
```bash
watch -n 1 nvidia-smi
# GPU 利用率应 >80%
```

若 <80%，请提高并发请求数：
```bash
vllm serve MODEL --max-num-seqs 512  # 从 256 调高
```

2. **检查是否受限于显存带宽**：
```bash
# 若显存 100% 占满但 GPU 利用率 <80%，减小序列长度
vllm serve MODEL --max-model-len 4096
```

3. **启用优化项**：
```bash
vllm serve MODEL \
  --enable-prefix-caching \
  --enable-chunked-prefill \
  --max-num-seqs 512
```

4. **检查张量并行设置**：
```bash
# 必须使用 2 的幂次个 GPU
vllm serve MODEL --tensor-parallel-size 4  # 不能是 3 或 5
```

### 症状：TTFT 高（time to first token >1 秒）

**原因与解决方案**：

**长提示词**：
```bash
vllm serve MODEL --enable-chunked-prefill
```

**无前缀缓存**：
```bash
vllm serve MODEL --enable-prefix-caching  # 针对重复提示词
```

**并发请求过多**：
```bash
vllm serve MODEL --max-num-seqs 64  # 调低以优先保证延迟
```

**模型对单 GPU 过大**：
```bash
vllm serve MODEL --tensor-parallel-size 2  # 并行 prefill
```

### 症状：token 生成慢（tokens/sec 低）

**诊断**：
```bash
# 检查模型大小是否正确
vllm serve MODEL  # 应在日志中看到模型大小

# 检查投机解码
vllm serve MODEL --speculative-model DRAFT_MODEL
```

**对 H100 GPU**，启用 FP8：
```bash
vllm serve MODEL --quantization fp8
```

## 模型加载错误

### 症状：`OSError: MODEL not found`

**原因**：

1. **模型名称拼写错误**：
```bash
# 在 HuggingFace 上核对精确的模型名
vllm serve meta-llama/Llama-3-8B-Instruct  # 注意大小写
```

2. **私有/受限（gated）模型**：
```bash
# 先登录 HuggingFace
huggingface-cli login
# 然后运行 vLLM
vllm serve meta-llama/Llama-3-70B-Instruct
```

3. **自定义模型需要 trust 标志**：
```bash
vllm serve MODEL --trust-remote-code
```

### 症状：`ValueError: Tokenizer not found`

**解决方案**：
```bash
# 先手动下载模型
python -c "from transformers import AutoTokenizer; AutoTokenizer.from_pretrained('MODEL')"

# 再启动 vLLM
vllm serve MODEL
```

### 症状：`ImportError: No module named 'flash_attn'`

**解决方案**：
```bash
# 安装 flash attention
pip install flash-attn --no-build-isolation

# 或禁用 flash attention
vllm serve MODEL --disable-flash-attn
```

## 网络与连接问题

### 症状：查询服务器时 `Connection refused`

**诊断**：

1. **检查服务器是否在运行**：
```bash
curl http://localhost:8000/health
```

2. **检查端口绑定**：
```bash
# 远程访问需绑定到所有接口
vllm serve MODEL --host 0.0.0.0 --port 8000

# 检查端口是否被占用
lsof -i :8000
```

3. **检查防火墙**：
```bash
# 在防火墙中放行端口
sudo ufw allow 8000
```

### 症状：通过网络响应缓慢

**解决方案**：

1. **增加超时时间**：
```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="EMPTY",
    timeout=300.0  # 5 分钟超时
)
```

2. **检查网络延迟**：
```bash
ping SERVER_IP  # 局域网应 <10ms
```

3. **使用连接池**：
```python
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

session = requests.Session()
retries = Retry(total=3, backoff_factor=1)
session.mount('http://', HTTPAdapter(max_retries=retries))
```

## 量化问题

### 症状：`RuntimeError: Quantization format not supported`

**解决方案**：
```bash
# 确保量化方法正确
vllm serve MODEL --quantization awq  # 用于 AWQ 模型
vllm serve MODEL --quantization gptq  # 用于 GPTQ 模型

# 查看模型卡片以确认量化类型
```

### 症状：量化后输出质量差

**诊断**：

1. **确认模型已正确量化**：
```bash
# 检查模型的 config.json 中的 quantization_config
cat ~/.cache/huggingface/hub/models--MODEL/config.json
```

2. **尝试不同的量化方法**：
```bash
# 若 AWQ 质量有问题，尝试 FP8（仅限 H100）
vllm serve MODEL --quantization fp8

# 或使用更温和的量化
vllm serve MODEL  # 不量化
```

3. **提高 temperature 以获得更好的多样性**：
```python
sampling_params = SamplingParams(temperature=0.8, top_p=0.95)
```

## 分布式服务问题

### 症状：`RuntimeError: Distributed init failed`

**诊断**：

1. **检查环境变量**：
```bash
# 在所有节点上
echo $MASTER_ADDR  # 应保持一致
echo $MASTER_PORT  # 应保持一致
echo $RANK  # 每个节点应唯一（0、1、2、…）
echo $WORLD_SIZE  # 应保持一致（总节点数）
```

2. **检查网络连通性**：
```bash
# 从节点 1 到节点 2
ping NODE2_IP
nc -zv NODE2_IP 29500  # 检查端口可达性
```

3. **检查 NCCL 设置**：
```bash
export NCCL_DEBUG=INFO
export NCCL_SOCKET_IFNAME=eth0  # 或你的网络接口
vllm serve MODEL --tensor-parallel-size 8
```

### 症状：`NCCL error: unhandled cuda error`

**解决方案**：

```bash
# 设置 NCCL 使用正确的网络接口
export NCCL_SOCKET_IFNAME=eth0  # 替换为你的接口

# 增加超时时间
export NCCL_TIMEOUT=1800  # 30 分钟

# 强制 P2P 以便调试
export NCCL_P2P_DISABLE=1
```

## 调试工具与命令

### 启用调试日志

```bash
export VLLM_LOGGING_LEVEL=DEBUG
vllm serve MODEL
```

### 监控 GPU 使用情况

```bash
# 实时 GPU 监控
watch -n 1 nvidia-smi

# 显存明细
nvidia-smi --query-gpu=memory.used,memory.free --format=csv -l 1
```

### 性能分析

```bash
# 内置基准测试
vllm bench throughput \
  --model MODEL \
  --input-tokens 128 \
  --output-tokens 256 \
  --num-prompts 100

vllm bench latency \
  --model MODEL \
  --input-tokens 128 \
  --output-tokens 256 \
  --batch-size 8
```

### 检查指标

```bash
# Prometheus 指标
curl http://localhost:9090/metrics

# 过滤特定指标
curl http://localhost:9090/metrics | grep vllm_time_to_first_token

# 需要监控的关键指标：
# - vllm_time_to_first_token_seconds
# - vllm_time_per_output_token_seconds
# - vllm_num_requests_running
# - vllm_gpu_cache_usage_perc
# - vllm_request_success_total
```

### 测试服务器健康状态

```bash
# 健康检查
curl http://localhost:8000/health

# 模型信息
curl http://localhost:8000/v1/models

# 测试补全
curl http://localhost:8000/v1/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "MODEL",
    "prompt": "Hello",
    "max_tokens": 10
  }'
```

### 常用环境变量

```bash
# CUDA 设置
export CUDA_VISIBLE_DEVICES=0,1,2,3  # 限定使用特定 GPU

# vLLM 设置
export VLLM_LOGGING_LEVEL=DEBUG
export VLLM_TRACE_FUNCTION=1  # 对函数进行性能分析
export VLLM_USE_V1=1  # 使用 v1.0 引擎（更快）

# NCCL 设置（分布式）
export NCCL_DEBUG=INFO
export NCCL_SOCKET_IFNAME=eth0
export NCCL_IB_DISABLE=0  # 启用 InfiniBand
```

### 为 bug 报告收集诊断信息

```bash
# 系统信息
nvidia-smi
python --version
pip show vllm

# vLLM 版本与配置
vllm --version
python -c "import vllm; print(vllm.__version__)"

# 开启调试日志运行
export VLLM_LOGGING_LEVEL=DEBUG
vllm serve MODEL 2>&1 | tee vllm_debug.log

# 在 bug 报告中附上：
# - vllm_debug.log
# - nvidia-smi 输出
# - 使用的完整命令
# - 预期与实际行为
```
