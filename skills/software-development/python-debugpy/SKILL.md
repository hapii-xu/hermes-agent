---
name: python-debugpy
description: "调试 Python：pdb REPL + debugpy 远程（DAP）。"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [debugging, python, pdb, debugpy, breakpoints, dap, post-mortem]
    related_skills: [systematic-debugging, node-inspect-debugger, debugging-hermes-tui-commands]
---

# Python 调试器（pdb + debugpy）

## 概述

三种工具，按场景选用：

| 工具 | 何时使用 |
|---|---|
| **`breakpoint()` + pdb** | 本地、交互式、最简单。在源码中加 `breakpoint()`，正常运行，在该行得到一个 REPL。 |
| **`python -m pdb`** | 无需改源码即可在 pdb 下启动已有脚本。适合快速探查。 |
| **`debugpy`** | 远程 / 无头 / "附加到已运行进程"。说 DAP，可从终端脚本化驱动，适用于长生命周期进程（网关、守护进程、PTY 子进程）。 |

**从 `breakpoint()` 起步。** 它是最省事且有效的选择。

## 何时使用

- 一个测试失败，而 traceback 没揭示某个值为什么错
- 你需要单步走过一个函数并观察一个集合的变化
- 一个长生命周期进程（hermes gateway、tui_gateway）行为异常且无法重启
- 事后剖析：一个异常在类生产代码中触发，你想检查崩溃点的局部变量
- 子进程 / 子组件（Python `_SlashWorker`、PTY 桥 worker）才是真正的 bug 所在

**不要用于：** `print()` / `logging.debug` 一分钟内能解决的事，或 `pytest -vv --tb=long --showlocals` 已揭示的事。

## pdb 快速参考

在任何 pdb 提示符（`(Pdb)`）内：

| 命令 | 动作 |
|---|---|
| `h` / `h cmd` | 帮助 |
| `n` | 下一行（步过） |
| `s` | 步入 |
| `r` | 从当前函数返回 |
| `c` | 继续 |
| `unt N` | 继续直到第 N 行 |
| `j N` | 跳到第 N 行（仅同一函数内） |
| `l` / `ll` | 列出当前行附近源码 / 完整函数 |
| `w` | 在哪（栈追踪） |
| `u` / `d` | 在栈中上移 / 下移 |
| `a` | 打印当前函数的参数 |
| `p expr` / `pp expr` | 打印 / 漂亮打印表达式 |
| `display expr` | 每次停止时自动打印 expr |
| `b file:line` | 设断点 |
| `b func` | 函数入口处断点 |
| `b file:line, cond` | 条件断点 |
| `cl N` | 清除断点 N |
| `tbreak file:line` | 一次性断点 |
| `!stmt` | 执行任意 Python（包括赋值） |
| `interact` | 在当前作用域进入完整 Python REPL（Ctrl+D 退出） |
| `q` | 退出 |

`interact` 命令最强大 —— 你可以 import 任何东西、检查复杂对象，甚至调用会改变状态的方法。局部变量默认只读；从 `(Pdb)` 提示符用 `!x = 42` 来修改。

## 配方 1：本地断点

最简单。编辑文件：

```python
def compute(x, y):
    result = some_helper(x)
    breakpoint()           # <-- 在这里进入 pdb
    return result + y
```

正常运行代码。你落在 `breakpoint()` 行，拥有对局部变量的完整访问。

**提交前别忘了移除 `breakpoint()`。** 用 `git diff` 或 pre-commit grep：
```bash
rg -n 'breakpoint\(\)' --type py
```

## 配方 2：在 pdb 下启动脚本（不改源码）

```bash
python -m pdb path/to/script.py arg1 arg2
# 落在脚本第一行
(Pdb) b path/to/script.py:42
(Pdb) c
```

## 配方 3：调试一个 pytest 测试

hermes 测试运行器和 pytest 都支持：

```bash
# 失败时（或任何异常抛出时）进入 pdb：
scripts/run_tests.sh tests/path/to/test_file.py::test_name --pdb

# 在测试开始时就进入 pdb：
scripts/run_tests.sh tests/path/to/test_file.py::test_name --trace

# 不进 pdb，在 traceback 中显示局部变量：
scripts/run_tests.sh tests/path/to/test_file.py --showlocals --tb=long
```

注意：`scripts/run_tests.sh` 默认用 xdist（`-n 4`），而 pdb 在 xdist 下**不**工作。加 `-p no:xdist` 或用 `-n 0` 跑单个测试：

```bash
scripts/run_tests.sh tests/foo_test.py::test_bar --pdb -p no:xdist
# 或
source .venv/bin/activate
python -m pytest tests/foo_test.py::test_bar --pdb
```

这绕过了密封环境保证 —— 调试时没问题，但推送前要在包装器下重新运行以确认。

## 配方 4：对任何异常做事后剖析

```python
import pdb, sys
try:
    run_the_thing()
except Exception:
    pdb.post_mortem(sys.exc_info()[2])
```

或包装整个脚本：

```bash
python -m pdb -c continue script.py
# 崩溃时，pdb 捕获它，你就在异常的帧里
```

或在 repl/jupyter 中设全局 hook：

```python
import sys
def excepthook(etype, value, tb):
    import pdb; pdb.post_mortem(tb)
sys.excepthook = excepthook
```

## 配方 5：用 debugpy 远程调试（附加到运行中的进程）

适用于长生命周期进程：Hermes 网关、tui_gateway、守护进程、一个已经行为异常且无法干净重启的进程。

### 设置

```bash
source /home/bb/hermes-agent/.venv/bin/activate
pip install debugpy
```

### 模式 A：改源码 —— 进程在启动时等调试器

在入口点附近（或你想调试的函数内）加：

```python
import debugpy
debugpy.listen(("127.0.0.1", 5678))
print("debugpy listening on 5678, waiting for client...", flush=True)
debugpy.wait_for_client()
debugpy.breakpoint()       # 可选：附加后立即暂停
```

启动进程；它阻塞在 `wait_for_client()`。

### 模式 B：不改源码 —— 用 `-m debugpy` 启动

```bash
python -m debugpy --listen 127.0.0.1:5678 --wait-for-client your_script.py arg1
```

等价的模块入口：

```bash
python -m debugpy --listen 127.0.0.1:5678 --wait-for-client -m your.module
```

### 模式 C：附加到已运行进程

需要 PID 且 debugpy 已预装在目标环境中：

```bash
python -m debugpy --listen 127.0.0.1:5678 --pid <pid>
# debugpy 把自己注入进程。然后按下面附加客户端。
```

某些内核/安全配置会阻止基于 ptrace 的注入（`/proc/sys/kernel/yama/ptrace_scope`）。修复：
```bash
echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope
```

### 从终端连接客户端

最简单的终端侧 DAP 客户端是 VS Code CLI 或一个小脚本。在 Hermes 内你有两个实用选项：

**选项 1：`debugpy` 自带的 CLI REPL** —— 非官方功能，而是一个微型 DAP 客户端脚本：

```python
# /tmp/dap_client.py
import socket, json, itertools, time, sys

HOST, PORT = "127.0.0.1", 5678
s = socket.create_connection((HOST, PORT))
seq = itertools.count(1)

def send(msg):
    msg["seq"] = next(seq)
    body = json.dumps(msg).encode()
    s.sendall(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)

def recv():
    header = b""
    while b"\r\n\r\n" not in header:
        header += s.recv(1)
    length = int(header.decode().split("Content-Length:")[1].split("\r\n")[0].strip())
    body = b""
    while len(body) < length:
        body += s.recv(length - len(body))
    return json.loads(body)

send({"type": "request", "command": "initialize", "arguments": {"adapterID": "python"}})
print(recv())
send({"type": "request", "command": "attach", "arguments": {}})
print(recv())
send({"type": "request", "command": "setBreakpoints",
      "arguments": {"source": {"path": sys.argv[1]},
                    "breakpoints": [{"line": int(sys.argv[2])}]}})
print(recv())
send({"type": "request", "command": "configurationDone"})
# ... 循环读取事件并发送 continue/stepIn 等
```

这对一次性自动化没问题，但作为交互式 UX 很痛苦。

**选项 2：从 VS Code / Cursor / Zed 附加** —— 若用户有其一打开，可以加一个 `launch.json`：

```json
{
  "name": "Attach to Hermes",
  "type": "debugpy",
  "request": "attach",
  "connect": { "host": "127.0.0.1", "port": 5678 },
  "justMyCode": false,
  "pathMappings": [
    { "localRoot": "${workspaceFolder}", "remoteRoot": "/home/bb/hermes-agent" }
  ]
}
```

**选项 3：抛弃 DAP，用 `remote-pdb`** —— 通常是终端 agent 实际想要的：

```bash
pip install remote-pdb
```

在你的代码中：
```python
from remote_pdb import set_trace
set_trace(host="127.0.0.1", port=4444)   # 阻塞直到连接
```

然后从终端：
```bash
nc 127.0.0.1 4444
# 你得到一个 (Pdb) 提示符，如同本地调试一样。
```

当 `debugpy` 的 DAP 协议过于复杂时，`remote-pdb` 是最干净的 agent 友好选择。仅当你确实需要 IDE 集成时才用 `debugpy`。

## 调试 Hermes 专属进程

### 测试
见配方 3。总是加 `-p no:xdist` 或不带 xdist 跑单个测试。

### `run_agent.py` / CLI —— 单次
最简单：在可疑行附近加 `breakpoint()`，然后正常运行 `hermes`。控制权在暂停点回到你的终端。

### `tui_gateway` 子进程（由 `hermes --tui` 派生）
网关作为 Node TUI 的子进程运行。选项：

**A. 改源码改网关：**
```python
# tui_gateway/server.py 在 serve() 顶部附近
import debugpy
debugpy.listen(("127.0.0.1", 5678))
debugpy.wait_for_client()
```
启动 `hermes --tui`。TUI 会显得冻结（其后端在等待）。附加客户端；你 `continue` 时执行恢复。

**B. 在特定 handler 用 `remote-pdb`：**
```python
from remote_pdb import set_trace
set_trace(host="127.0.0.1", port=4444)   # 在你想困住的 RPC handler 中
```
从 TUI 触发匹配的 slash 命令，然后在另一个终端 `nc 127.0.0.1 4444`。

### `_SlashWorker` 子进程
同样模式 —— 在 worker 的 `exec` 路径内用带 `set_trace()` 的 `remote-pdb`。worker 在 slash 命令间持久，所以首次触发会阻塞直到你连接；后续 slash 命令正常通过，除非你重新布防。

### 网关（`gateway/run.py`）
长生命周期。在某个 handler 用 `remote-pdb`，或若你反正要重启网关则用带 `--wait-for-client` 的 `debugpy`。

## 常见陷阱

1. **pytest-xdist 下的 pdb 静默无效。** 你看不到提示符，测试就是挂起。总是用 `-p no:xdist` 或 `-n 0`。

2. **CI / 非 TTY 上下文中的 `breakpoint()` 会挂起进程。** 本地安全；绝不要提交它。加一个 pre-commit grep 作为安全网。

3. **`PYTHONBREAKPOINT=0`** 禁用所有 `breakpoint()` 调用。若你的断点没命中，检查环境：
   ```bash
   echo $PYTHONBREAKPOINT
   ```

4. **`debugpy.listen` 仅当你同时调用 `wait_for_client()` 时才阻塞。** 否则执行继续，你的第一个断点可能在客户端附加前就触发。

5. **附加到 PID 在加固内核上失败。** `ptrace_scope=1`（Ubuntu 默认）只允许同用户对子进程的 ptrace。变通：`echo 0 > /proc/sys/kernel/yama/ptrace_scope`（需要 root）或从一开始就在 `debugpy` 下启动。

6. **线程。** `pdb` 只调试当前线程。对多线程代码，用 `debugpy`（线程感知的 DAP）或对每个线程设 `threading.settrace()`。

7. **asyncio。** `pdb` 在协程中工作，但 pdb 内的 `await` 需要 Python 3.13+ 或旧版本上从 `interact` 模式 `await`。对 3.11/3.12，用 `asyncio.run_coroutine_threadsafe` 技巧或通过 `asyncio.ensure_future` 的 `!stmt` 式 await。

8. **`scripts/run_tests.sh` 剥离凭据并设置 `HOME=<tmpdir>`。** 若你的 bug 依赖用户配置或真实 API key，在包装器下不会复现。先用原始 `pytest` 调试以复现，再在包装器下重新确认。

9. **fork / 多进程。** pdb 不跟随 fork。每个子进程需要自己的 `breakpoint()` 或 `set_trace()`。对 Hermes 子 agent，一次调试一个进程。

## 验证清单

- [ ] `pip install debugpy` 后，确认：`python -c "import debugpy; print(debugpy.__version__)"`
- [ ] 对远程调试，确认端口确实在监听：`ss -tlnp | grep 5678`
- [ ] 第一个断点确实命中（若没命中，你可能有 `PYTHONBREAKPOINT=0`、在 xdist 下、或执行在附加前就结束了）
- [ ] `where` / `w` 显示预期的调用栈
- [ ] 调试后清理：提交的代码中没有遗留的 `breakpoint()` / `set_trace()`
  ```bash
  rg -n 'breakpoint\(\)|set_trace\(|debugpy\.listen' --type py
  ```

## 单次配方

**"为什么这个字典少了一个键？"**
```python
# 在 KeyError 处上方加
breakpoint()
# 然后在 pdb 中：
(Pdb) pp d
(Pdb) pp list(d.keys())
(Pdb) w                # 我们怎么到这的
```

**"这个测试单独跑通过，但在套件中失败。"**
```bash
scripts/run_tests.sh tests/the_test.py --pdb -p no:xdist
# 但若它只跟其他测试一起失败：
source .venv/bin/activate
python -m pytest tests/ -x --pdb -p no:xdist
# 现在它在状态累积后于确切的失败测试处 pdb 困住。
```

**"我的 async handler 死锁。"**
```python
# 在 handler 入口加
import remote_pdb; remote_pdb.set_trace(host="127.0.0.1", port=4444)
```
触发该 handler。`nc 127.0.0.1 4444`，然后 `w` 看挂起的帧，`!import asyncio; asyncio.all_tasks()` 看还有什么在等待。

**"对一个 Ink 子进程 / 子进程中的崩溃做事后剖析。"**
```bash
PYTHONFAULTHANDLER=1 python -m pdb -c continue path/to/entrypoint.py
# 崩溃时，pdb 落在异常的帧，带完整局部变量
```
