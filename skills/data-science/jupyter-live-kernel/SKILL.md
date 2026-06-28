---
name: jupyter-live-kernel
description: "通过实时 Jupyter kernel (hamelnb) 进行迭代式 Python。"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [jupyter, notebook, repl, data-science, exploration, iterative]
    category: data-science
---

# Jupyter Live Kernel (hamelnb)

通过一个活跃的 Jupyter kernel 为你提供一个**有状态的 Python REPL**。变量在多次
执行之间持久保留。当你需要逐步累积状态、探索 API、检查 DataFrame 或迭代复杂代码时，
请使用本 skill 而非 `execute_code`。

## 何时使用本 skill 与其他工具

| 工具 | 适用场景 |
|------|----------|
| **本 skill** | 迭代式探索、跨步骤保持状态、数据科学、ML、"让我试一下再看看结果" |
| `execute_code` | 需要 hermes 工具访问（web_search、文件操作）的一次性脚本。无状态。 |
| `terminal` | Shell 命令、构建、安装、git、进程管理 |

**经验法则：** 如果这个任务你会想用 Jupyter notebook，就用本 skill。

## 前置条件

1. 必须已安装 **uv**（检查：`which uv`）
2. 必须已安装 **JupyterLab**：`uv tool install jupyterlab`
3. 必须有一个正在运行的 Jupyter server（参见下文的"配置"）

## 配置

hamelnb 脚本位置：
```
SCRIPT="$HOME/.agent-skills/hamelnb/skills/jupyter-live-kernel/scripts/jupyter_live_kernel.py"
```

如果尚未克隆：
```
git clone https://github.com/hamelsmu/hamelnb.git ~/.agent-skills/hamelnb
```

### 启动 JupyterLab

检查是否已有 server 在运行：
```
uv run "$SCRIPT" servers
```

如果没有找到 server，启动一个：
```
jupyter-lab --no-browser --port=8888 --notebook-dir=$HOME/notebooks \
  --IdentityProvider.token='' --ServerApp.password='' > /tmp/jupyter.log 2>&1 &
sleep 3
```

注意：为方便本地 agent 访问，已禁用 token/password。该 server 以无头方式运行。

### 为 REPL 用途创建一个 notebook

如果你只需要一个 REPL（没有现成的 notebook），创建一个最小的 notebook 文件：
```
mkdir -p ~/notebooks
```
写一个只含一个空代码单元的最小 .ipynb JSON 文件，然后通过 Jupyter REST API
启动一个 kernel 会话：
```
curl -s -X POST http://127.0.0.1:8888/api/sessions \
  -H "Content-Type: application/json" \
  -d '{"path":"scratch.ipynb","type":"notebook","name":"scratch.ipynb","kernel":{"name":"python3"}}'
```

## 核心工作流

所有命令都返回结构化 JSON。始终使用 `--compact` 以节省 token。

### 1. 发现 server 和 notebook

```
uv run "$SCRIPT" servers --compact
uv run "$SCRIPT" notebooks --compact
```

### 2. 执行代码（主要操作）

```
uv run "$SCRIPT" execute --path <notebook.ipynb> --code '<python code>' --compact
```

状态在多次 execute 调用之间持久保留。变量、import、对象都会保留。

多行代码可使用 $'...' 引号：
```
uv run "$SCRIPT" execute --path scratch.ipynb --code $'import os\nfiles = os.listdir(".")\nprint(f"Found {len(files)} files")' --compact
```

### 3. 检查实时变量

```
uv run "$SCRIPT" variables --path <notebook.ipynb> list --compact
uv run "$SCRIPT" variables --path <notebook.ipynb> preview --name <varname> --compact
```

### 4. 编辑 notebook 单元

```
# 查看当前单元
uv run "$SCRIPT" contents --path <notebook.ipynb> --compact

# 插入新单元
uv run "$SCRIPT" edit --path <notebook.ipynb> insert \
  --at-index <N> --cell-type code --source '<code>' --compact

# 替换单元源码（使用 contents 输出中的 cell-id）
uv run "$SCRIPT" edit --path <notebook.ipynb> replace-source \
  --cell-id <id> --source '<new code>' --compact

# 删除单元
uv run "$SCRIPT" edit --path <notebook.ipynb> delete --cell-id <id> --compact
```

### 5. 验证（重启 + 全部运行）

仅当用户要求一次干净的验证，或你需要确认 notebook 能从头到尾完整运行时才使用：

```
uv run "$SCRIPT" restart-run-all --path <notebook.ipynb> --save-outputs --compact
```

## 来自实践的经验技巧

1. **server 启动后的首次执行可能超时** —— kernel 需要一点时间
   初始化。如果遇到超时，重试即可。

2. **kernel 的 Python 是 JupyterLab 的 Python** —— 包必须安装在该
   环境中。如果需要额外的包，先把它们安装到 JupyterLab 工具环境里。

3. **--compact 标志能显著节省 token** —— 始终使用它。没有它的话 JSON 输出可能
   非常冗长。

4. **纯粹用作 REPL 时**，创建一个 scratch.ipynb，不必费心编辑单元。
   只需反复使用 `execute`。

5. **参数顺序很重要** —— 子命令的标志（如 `--path`）要放在
   子子命令之前。例如：应为 `variables --path nb.ipynb list` 而不是 `variables list --path nb.ipynb`。

6. **如果会话尚不存在**，你需要通过 REST API 启动一个
   （参见"配置"小节）。没有活跃的 kernel 会话，工具无法执行。

7. **错误以 JSON 形式返回**并附带 traceback —— 阅读 `ename` 和 `evalue`
   字段来了解出了什么问题。

8. **偶发的 websocket 超时** —— 某些操作首次尝试可能超时，
   尤其是在 kernel 重启之后。在升级处理前先重试一次。

## 超时默认值

脚本默认每次执行超时为 30 秒。对于长时间运行的操作，
传入 `--timeout 120`。在初始设置或重计算时使用较宽裕的超时（60 以上）。
