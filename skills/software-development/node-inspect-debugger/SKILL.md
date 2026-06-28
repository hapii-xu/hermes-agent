---
name: node-inspect-debugger
description: "通过 --inspect + Chrome DevTools Protocol CLI 调试 Node.js。"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [debugging, nodejs, node-inspect, cdp, breakpoints, ui-tui]
    related_skills: [systematic-debugging, python-debugpy, debugging-hermes-tui-commands]
---

# Node.js Inspect 调试器

## 概述

当 `console.log` 不够用时，可以在终端里以编程方式驱动 Node 内置的 V8 inspector。你能获得真正的断点、步入/步过/步出、调用栈遍历、本地/闭包作用域转储，以及在暂停帧中求值任意表达式。

两个工具，任选其一：

- **`node inspect`**——内置、零安装、CLI REPL。最适合快速探查。
- **`ndb` / 通过 `chrome-remote-interface` 的 CDP**——可从 Node/Python 脚本化；适合你想自动化设置大量断点、跨多次运行收集状态，或在 agent 循环里非交互调试的场景。

**优先尝试 `node inspect`。** 它始终可用，REPL 也很快。

## 何时使用

- 某个 Node 测试失败，你需要查看中间状态
- ui-tui 崩溃或行为异常，你想在渲染前检查 React/Ink 状态
- tui_gateway 的子进程（`_SlashWorker`、PTY bridge worker）表现异常
- 你需要检查某个 `console.log` 不打补丁就无法触及的闭包值
- 性能：附加到正在运行的进程，抓取 CPU profile 或堆快照

**不要用于：** `console.log` 一分钟内就能解决的问题。基于断点的调试更重，应在确实有回报时才用。

## 快速参考：`node inspect` REPL

在第一行就暂停启动：

```bash
node inspect path/to/script.js
# 或配合 tsx
node --inspect-brk $(which tsx) path/to/script.ts
```

`debug>` 提示符支持的命令：

| 命令 | 动作 |
|---|---|
| `c` 或 `cont` | 继续 |
| `n` 或 `next` | 步过 |
| `s` 或 `step` | 步入 |
| `o` 或 `out` | 步出 |
| `pause` | 暂停运行中的代码 |
| `sb('file.js', 42)` | 在 file.js 第 42 行设断点 |
| `sb(42)` | 在当前文件第 42 行设断点 |
| `sb('functionName')` | 函数被调用时中断 |
| `cb('file.js', 42)` | 清除断点 |
| `breakpoints` | 列出所有断点 |
| `bt` | 回溯（调用栈） |
| `list(5)` | 显示当前位置周围 5 行源码 |
| `watch('expr')` | 每次暂停都求值 expr |
| `watchers` | 显示被监视的表达式 |
| `repl` | 进入当前作用域的 REPL（Ctrl+C 退出 REPL） |
| `exec expr` | 求值一次表达式 |
| `restart` | 重启脚本 |
| `kill` | 终止脚本 |
| `.exit` | 退出调试器 |

**在 `repl` 子模式里：** 输入任意 JS 表达式，包括访问本地变量/闭包变量。`Ctrl+C` 退回 `debug>`。

## 附加到正在运行的进程

当进程已经在运行（例如长驻开发服务器或 TUI gateway）：

```bash
# 1. 给已存在的进程发 SIGUSR1 以启用 inspector
kill -SIGUSR1 <pid>
# Node 会打印：Debugger listening on ws://127.0.0.1:9229/<uuid>

# 2. 附加调试器 CLI
node inspect -p <pid>
# 或按 URL 附加
node inspect ws://127.0.0.1:9229/<uuid>
```

要让进程从启动就开启 inspector：

```bash
node --inspect script.js           # 监听 127.0.0.1:9229，继续运行
node --inspect-brk script.js       # 监听且在第一行暂停
node --inspect=0.0.0.0:9230 script.js   # 自定义 host:port
```

通过 tsx 跑 TypeScript：

```bash
node --inspect-brk --import tsx script.ts
# 或较旧的 tsx
node --inspect-brk -r tsx/cjs script.ts
```

## 编程式 CDP（从终端脚本化）

当你想自动化——设大量断点、抓取作用域状态、脚本化复现——就用 `chrome-remote-interface`：

```bash
npm i -g chrome-remote-interface        # 或装在项目本地
# 启动你的目标：
node --inspect-brk=9229 target.js &
```

驱动脚本（保存为 `/tmp/cdp-debug.js`）：

```javascript
const CDP = require('chrome-remote-interface');

(async () => {
  const client = await CDP({ port: 9229 });
  const { Debugger, Runtime } = client;

  Debugger.paused(async ({ callFrames, reason }) => {
    const top = callFrames[0];
    console.log(`PAUSED: ${reason} @ ${top.url}:${top.location.lineNumber + 1}`);

    // 遍历作用域拿本地变量
    for (const scope of top.scopeChain) {
      if (scope.type === 'local' || scope.type === 'closure') {
        const { result } = await Runtime.getProperties({
          objectId: scope.object.objectId,
          ownProperties: true,
        });
        for (const p of result) {
          console.log(`  ${scope.type}.${p.name} =`, p.value?.value ?? p.value?.description);
        }
      }
    }

    // 在暂停帧中求值表达式
    const { result } = await Debugger.evaluateOnCallFrame({
      callFrameId: top.callFrameId,
      expression: 'typeof state !== "undefined" ? JSON.stringify(state) : "n/a"',
    });
    console.log('state =', result.value ?? result.description);

    await Debugger.resume();
  });

  await Runtime.enable();
  await Debugger.enable();

  // 按 URL 正则 + 行号设断点
  await Debugger.setBreakpointByUrl({
    urlRegex: '.*app\\.tsx$',
    lineNumber: 119,       // 0 基
    columnNumber: 0,
  });

  await Runtime.runIfWaitingForDebugger();
})();
```

运行它：

```bash
node /tmp/cdp-debug.js
```

Hermes 专属说明：`chrome-remote-interface` 不在 `ui-tui/package.json` 中。如果不想弄脏项目，可装到一个临时位置：

```bash
mkdir -p /tmp/cdp-tools && cd /tmp/cdp-tools && npm i chrome-remote-interface
NODE_PATH=/tmp/cdp-tools/node_modules node /tmp/cdp-debug.js
```

## 调试 Hermes ui-tui

TUI 用 Ink + tsx 构建。两种常见场景：

### 在开发态调试单个 Ink 组件

`ui-tui/package.json` 提供 `npm run dev`（tsx --watch）。通过直接跑 tsx 来加上 `--inspect-brk`：

```bash
cd /home/bb/hermes-agent/ui-tui
npm run build    # 先产出 dist/，这样首次加载不需要转译
node --inspect-brk dist/entry.js
# 在另一个终端：
node inspect -p <node pid>
```

然后在 `debug>` 里：

```
sb('dist/app.js', 220)     # 或可疑的 render 所在位置
cont
```

暂停后，`repl` → 检查 `props`、state ref、`useInput` 处理器的值等。

### 调试运行中的 `hermes --tui`

TUI 由 Python CLI 派生 Node 进程。最简单的路径：

```bash
# 1. 启动 TUI
hermes --tui &
TUI_PID=$(pgrep -f 'ui-tui/dist/entry' | head -1)

# 2. 在该 Node PID 上启用 inspector
kill -SIGUSR1 "$TUI_PID"

# 3. 找到 WS URL
curl -s http://127.0.0.1:9229/json/list | jq -r '.[0].webSocketDebuggerUrl'

# 4. 附加
node inspect ws://127.0.0.1:9229/<uuid>
```

在 TUI 窗口里与之交互（键入）会继续推进执行；你的调试器随时可以在任意 `sb(...)` 断点把它暂停。

### 调试 `_SlashWorker` / PTY 子进程

那些是 Python 而非 Node——对它们使用 `python-debugpy` skill。只有 Node 部分（Ink UI、tui_gateway 客户端、`ui-tui/` 下 tsx 跑的测试）使用本 skill。

## 在调试器下运行 Vitest 测试

```bash
cd /home/bb/hermes-agent/ui-tui
# 跑单个测试文件，在入口暂停
node --inspect-brk ./node_modules/vitest/vitest.mjs run --no-file-parallelism src/app/foo.test.tsx
```

在另一个终端：`node inspect -p <pid>`，然后 `sb('src/app/foo.tsx', 42)`、`cont`。

使用 `--no-file-parallelism`（vitest）或 `--runInBand`（jest），这样只有一个 worker——调试一个进程池会很痛苦。

## 堆快照与 CPU Profile（非交互）

把上面的 CDP 驱动里的 Debugger 换成 `HeapProfiler` / `Profiler`：

```javascript
// 采集 5 秒 CPU profile
await client.Profiler.enable();
await client.Profiler.start();
await new Promise(r => setTimeout(r, 5000));
const { profile } = await client.Profiler.stop();
require('fs').writeFileSync('/tmp/cpu.cpuprofile', JSON.stringify(profile));
// 在 Chrome DevTools → Performance 面板打开 /tmp/cpu.cpuprofile
```

```javascript
// 堆快照
await client.HeapProfiler.enable();
const chunks = [];
client.HeapProfiler.addHeapSnapshotChunk(({ chunk }) => chunks.push(chunk));
await client.HeapProfiler.takeHeapSnapshot({ reportProgress: false });
require('fs').writeFileSync('/tmp/heap.heapsnapshot', chunks.join(''));
```

## 常见陷阱

1. **TS 源码行号不对。** 断点命中的是编译后的 JS，而不是 `.ts`。要么 (a) 在构建出的 `dist/*.js` 里打断点，要么 (b) 启用 sourcemap（`node --enable-source-maps`）并用 `sb('src/app.tsx', N)`——但仅限会跟随 sourcemap 的 CDP 客户端。`node inspect` CLI 不跟随。

2. **`--inspect` 与 `--inspect-brk`。** `--inspect` 启动 inspector 但不暂停；如果你附加得太晚，脚本会越过第一个断点。当代码运行前需要设断点时，用 `--inspect-brk`。

3. **端口冲突。** 默认是 `9229`。如果有多个 Node 进程都在 inspect，传 `--inspect=0`（随机端口）并从 `/json/list` 读取实际 URL：
   ```bash
   curl -s http://127.0.0.1:9229/json/list   # 列出该主机上所有可 inspect 的目标
   ```

4. **子进程。** 在父进程上加 `--inspect` 不会 inspect 它的子进程。用 `NODE_OPTIONS='--inspect-brk' node parent.js` 把它传递给每个子进程；注意它们都需要唯一端口（继承 `NODE_OPTIONS='--inspect'` 时 Node 会自动递增）。

5. **后台被杀。** 如果在目标暂停时按 `Ctrl+C` 退出 `node inspect`，目标会保持暂停。要么先 `cont`，要么显式 `kill` 掉目标。

6. **通过 agent 终端运行 `node inspect`。** 它是 PTY 友好的 REPL。在 Hermes 里用 `terminal(pty=true)` 或 `background=true` + `process(action='submit', data='...')` 启动。非 PTY 前台模式可以跑一次性命令，但不能交互式步进。

7. **安全。** `--inspect=0.0.0.0:9229` 等于暴露任意代码执行。除非你有隔离网络，否则始终绑定到 `127.0.0.1`（默认值）。

## 验证清单

设置好调试会话后，验证：

- [ ] `curl -s http://127.0.0.1:9229/json/list` 返回的恰好是你期望的目标
- [ ] 第一个断点确实命中（若没有，很可能是漏了 `--inspect-brk`，或附加时执行已经完成）
- [ ] 暂停处的源码列表显示正确的文件（不匹配 = sourcemap 问题，见陷阱 1）
- [ ] 在 `repl` 中执行 `exec process.pid` 返回的就是你想附加的 PID

## 一次性配方

**「为什么这个变量在第 X 行是 undefined？」**
```bash
node --inspect-brk script.js &
node inspect -p $!
# debug>
sb('script.js', X)
cont
# 已暂停。现在：
repl
> myVariable
> Object.keys(this)
```

**「进入这个函数的调用路径是什么？」**
```
debug> sb('suspectFn')
debug> cont
# 在入口暂停
debug> bt
```

**「这条异步链卡住了——卡在哪？」**
```
# 先用 --inspect（不要 -brk），让它跑到卡住的位置，然后：
debug> pause
debug> bt
# 现在你能看到卡住的栈帧
```
