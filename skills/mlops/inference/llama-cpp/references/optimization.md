# 性能优化指南

最大化 llama.cpp 的推理速度和效率。

## CPU 优化

### 线程调优
```bash
# 设置线程数（默认：物理核心数）
./llama-cli -m model.gguf -t 8

# 对于 AMD Ryzen 9 7950X（16 核，32 线程）
-t 16  # 最佳：物理核心数

# 避免使用超线程（对矩阵运算更慢）
```

### BLAS 加速
```bash
# OpenBLAS（更快的矩阵运算）
make LLAMA_OPENBLAS=1

# BLAS 带来 2-3 倍加速
```

## GPU 卸载

### 层卸载
```bash
# 将 35 层卸载到 GPU（混合模式）
./llama-cli -m model.gguf -ngl 35

# 卸载所有层
./llama-cli -m model.gguf -ngl 999

# 寻找最优值：
# 从 -ngl 999 开始
# 如果 OOM，每次减少 5，直到能放下
```

### 内存使用
```bash
# 检查 VRAM 使用
nvidia-smi dmon

# 如有需要，减少上下文
./llama-cli -m model.gguf -c 2048  # 用 2K 上下文代替 4K
```

## 批处理

```bash
# 增大批处理量以提高吞吐
./llama-cli -m model.gguf -b 512  # 默认：512

# 物理批量（GPU）
--ubatch 128  # 一次处理 128 个 token
```

## 上下文管理

```bash
# 默认上下文（512 个 token）
-c 512

# 更长上下文（更慢，更多内存）
-c 4096

# 超长上下文（如果模型支持）
-c 32768
```

## 基准测试

### CPU 性能（Llama 2-7B Q4_K_M）

| 配置 | 速度 | 说明 |
|-------|-------|-------|
| Apple M3 Max | 50 tok/s | Metal 加速 |
| AMD 7950X（16c） | 35 tok/s | OpenBLAS |
| Intel i9-13900K | 30 tok/s | AVX2 |

### GPU 卸载（RTX 4090）

| GPU 层数 | 速度 | VRAM |
|------------|-------|------|
| 0（仅 CPU） | 30 tok/s | 0 GB |
| 20（混合） | 80 tok/s | 8 GB |
| 35（全部） | 120 tok/s | 12 GB |
