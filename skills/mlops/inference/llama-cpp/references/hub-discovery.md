# llama.cpp 的 Hugging Face URL 工作流

优先使用仅基于 URL 的工作流。不要仅仅为了查找 GGUF 文件、选择量化或构建 `llama-server` 命令而要求使用 `hf` 或 API 客户端。

## 核心 URL

```text
搜索：
https://huggingface.co/models?apps=llama.cpp&sort=trending

带文本搜索：
https://huggingface.co/models?search=<term>&apps=llama.cpp&sort=trending

带大小范围搜索：
https://huggingface.co/models?search=<term>&apps=llama.cpp&num_parameters=min:0,max:24B&sort=trending

仓库 local-app 视图：
https://huggingface.co/<repo>?local-app=llama.cpp

仓库 tree API：
https://huggingface.co/api/models/<repo>/tree/main?recursive=true

仓库文件树：
https://huggingface.co/<repo>/tree/main
```

## 1. 搜索兼容 llama.cpp 的模型

从带 `apps=llama.cpp` 的模型页面开始。

使用：

- 用 `search=<term>` 指定模型系列名称，例如 `Qwen`、`Gemma`、`Phi` 或 `Mistral`
- 当用户有硬件限制时，使用 `num_parameters=min:0,max:24B` 或类似参数
- 当用户想立即看到热门仓库时，使用 `sort=trending`

如果用户尚未选定模型系列，不要直接从随机的 GGUF 仓库开始。先搜索，再筛选候选。

示例：https://huggingface.co/models?search=Qwen&apps=llama.cpp&num_parameters=min:0,max:24B&sort=trending

## 2. 使用 local-app 页面获取推荐的量化

打开：

```text
https://huggingface.co/<repo>?local-app=llama.cpp
```

按以下顺序提取：

1. 精确的 `Use this model` 代码片段（如果以文本形式可见）
2. 从抓取的页面文本或 HTML 中提取的 `Hardware compatibility` 部分：
   - 量化标签
   - 文件大小
   - 位深分组
3. 代码片段中显示的任何额外启动标志，例如 `--jinja`

当 HF local-app 代码片段可见时，将其视为权威来源。

通过读取 URL 本身来完成，而不是假设 UI 在浏览器中已渲染。如果抓取的页面源码没有暴露 `Hardware compatibility`，请说明该部分在文本中不可见，并回退到 tree API 加上 `quantization.md` 中的通用指南。

## 3. 从 tree API 确认精确文件

打开：

```text
https://huggingface.co/api/models/<repo>/tree/main?recursive=true
```

将 JSON 响应视为仓库清单的权威来源。

保留满足以下条件的条目：

- `type` 为 `file`
- `path` 以 `.gguf` 结尾

使用以下字段：

- `path` 获取文件名和子目录
- `size` 获取字节大小
- 可选地使用 `lfs.size` 确认 LFS 负载大小

将文件分类为：

- 量化的单文件检查点，例如 `Qwen3.6-35B-A3B-UD-Q4_K_M.gguf`
- 投影器权重，通常为 `mmproj-*.gguf`
- BF16 分片文件，通常在 `BF16/` 下
- 其他所有内容

除非用户询问，否则忽略：

- `README.md`
- imatrix 或校准数据块

仅当 API 端点失败或用户想查看网页视图时，才将 `https://huggingface.co/<repo>/tree/main` 作为人工备用方式。

## 4. 构建命令

优先顺序：

1. 从 local-app 页面复制精确的 HF 代码片段
2. 如果页面提供了干净的量化标签，使用简写选择：

```bash
llama-server -hf <repo>:<QUANT>
```

3. 如果你需要从 tree API 获取精确文件，使用指定文件的形式：

```bash
llama-server --hf-repo <repo> --hf-file <filename.gguf>
```

4. 如果要使用 CLI 而非服务器，使用：

```bash
llama-cli -hf <repo>:<QUANT>
```

当仓库使用自定义标签或非标准命名而可能使 `:<QUANT>` 产生歧义时，使用指定文件的形式。

## 5. 示例：`unsloth/Qwen3.6-35B-A3B-GGUF`

使用以下 URL：

```text
https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF?local-app=llama.cpp
https://huggingface.co/api/models/unsloth/Qwen3.6-35B-A3B-GGUF/tree/main?recursive=true
https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/tree/main
```

在 local-app 页面上，硬件兼容性部分可能暴露如下条目：

- `UD-IQ4_XS` - 17.7 GB
- `UD-Q4_K_S` - 20.9 GB
- `UD-Q4_K_M` - 22.1 GB
- `UD-Q5_K_M` - 26.5 GB
- `UD-Q6_K` - 29.3 GB
- `Q8_0` - 36.9 GB

在 tree API 上，你可以确认精确的文件名，例如：

- `Qwen3.6-35B-A3B-UD-Q4_K_M.gguf`
- `Qwen3.6-35B-A3B-UD-Q5_K_M.gguf`
- `Qwen3.6-35B-A3B-UD-Q6_K.gguf`
- `Qwen3.6-35B-A3B-Q8_0.gguf`
- `mmproj-F16.gguf`

该仓库的良好最终输出：

```text
Repo: unsloth/Qwen3.6-35B-A3B-GGUF
Recommended quant from HF: UD-Q4_K_M (22.1 GB)
llama-server: llama-server --hf-repo unsloth/Qwen3.6-35B-A3B-GGUF --hf-file Qwen3.6-35B-A3B-UD-Q4_K_M.gguf
Other GGUFs:
- Qwen3.6-35B-A3B-UD-Q5_K_M.gguf - 26.5 GB
- Qwen3.6-35B-A3B-UD-Q6_K.gguf - 29.3 GB
- Qwen3.6-35B-A3B-Q8_0.gguf - 36.9 GB
Projector:
- mmproj-F16.gguf - 899 MB
```

## 注意事项

- 仓库特定的量化标签很重要。不要将 `UD-Q4_K_M` 改写为 `Q4_K_M`，除非页面本身这样做。
- `mmproj` 文件是多模态模型的投影器权重，不是主语言模型检查点。
- 如果 HF 硬件兼容性面板缺失（因为用户未配置硬件配置，或抓取的页面源码未暴露它），仍应使用 tree API 加上 `quantization.md` 中的通用量化指南。
- 如果仓库已有 GGUF，不要直接跳到转换工作流。
