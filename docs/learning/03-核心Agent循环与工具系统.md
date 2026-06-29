# 03 · 核心 Agent 循环与工具系统

> 引用约定：本文用 `文件:行号` 指向源码（如 `agent/conversation_loop.py:589`）。行号基于撰写时的 `learning` 分支快照，后续重构可能漂移；不确定处标注「待确认」。

## 本章导读

Hermes 是 Nous Research 的「自我改进型」个人 AI agent，同一套 agent 内核跑在 CLI、消息网关（Telegram/Discord/Slack 等 20+ 平台）、TUI 和桌面应用上。本章聚焦它的**心脏**——一个用户「turn（轮次）」是如何被驱动的：

1. **Agent 主循环**：一个 turn 的生命周期（构建 prompt → 调模型 → 解析 tool calls → 执行工具 → 喂回结果 → 循环）。
2. **Prompt 构建与缓存神圣性**：为什么系统提示必须「逐字节稳定（byte-stable）」。
3. **上下文管理 / 压缩**：对话变长时如何压缩。
4. **工具系统**：工具如何定义、注册、按 toolset 分组、按需启用。
5. **Footprint Ladder（足迹阶梯）**：为什么「新增 core tool 是最后手段」。
6. **子 agent / 委派**：如何 spawn 隔离的子 agent 并行工作。

贯穿全篇有两条**设计铁律**（`AGENTS.md:16-27`）：

- **每会话的 prompt 缓存是神圣的**：长对话每轮复用同一段缓存前缀，任何中途改动过去上下文 / 换工具集 / 重建系统提示的行为都会让缓存失效，使用户成本翻数倍。唯一例外是上下文压缩。
- **内核是「窄腰（narrow waist）」，能力活在边缘**：每个 model tool 都会随**每一次** API 调用发送，所以新增 core tool 的门槛极高。能力应以 CLI+skill、service-gated tool 或 plugin 的形式落地，而不是长在内核里。

---

## 1. Agent 主循环：一个 turn 的生命周期

### 1.1 总览流程图

```
用户消息
   │
   ▼
run_conversation()            agent/conversation_loop.py:495  ← 薄入口
   │
   ├─ build_turn_context()    agent/turn_context.py:118       ← 「序幕 / 每轮一次的准备」
   │     · stdio 守护、重置重试计数器、清洗 surrogate 字符
   │     · 恢复或重建系统提示（缓存前缀）
   │     · turn 之间的 MCP 工具刷新（cache-safe）
   │     · 预飞（preflight）压缩检查
   │     · pre_llm_call 插件钩子、外部记忆预取
   │
   ▼
while 主循环                    agent/conversation_loop.py:589
   │  条件：api_call_count < max_iterations
   │        且 iteration_budget.remaining > 0
   │        （或 _budget_grace_call 宽限调用）
   │
   ├─ 中断检查（用户发了新消息？）         :594
   ├─ 消费迭代预算 consume()              :610
   ├─ step_callback（gateway agent:step 事件）:617
   ├─ /steer 漂移：在构建 api_messages 前注入  :650
   ├─ 构建 api_messages（系统提示 + 历史）
   ├─ 调用模型（streaming / 非 streaming，带重试）
   │
   ├─ 若 assistant_message.tool_calls：     :3834
   │     · 校验工具名是否在 valid_tool_names
   │     · 去重 / 限流 delegate_task 调用
   │     · append assistant 消息 + 落盘到 session DB :4080
   │     · agent._execute_tool_calls(...)         :4101
   │     · 工具护栏（guardrail）可触发 halt       :4103
   │     · execute_code-only 轮次 → 退还预算       :4144
   │     · should_compress() → 压缩上下文          :4182
   │     · continue（进入下一轮）                  :4198
   │
   └─ 否则（无 tool calls）→ 这是最终回复          :4200
         · final_response = assistant_message.content
         · 处理空回复 / 仅 think 块 / 部分流恢复 / nudge
         · break
   │
   ▼
finalize_turn()                agent/turn_finalizer.py:30
   · 预算耗尽则做一次「无工具」的总结调用 _handle_max_iterations
   · kanban worker 失败上报、记忆复盘、组装 result dict
   │
   ▼
返回 {final_response, messages, ...}
```

### 1.2 三个角色文件的分工

`run_conversation` 历史上是一个约 3900 行的巨函数，现已拆分（`agent/conversation_loop.py:4-10` 的模块 docstring 说明）：

| 文件 | 职责 |
|---|---|
| `agent/conversation_loop.py` | 主循环本体（`run_conversation`，`:495`）。`AIAgent.run_conversation` 现在只是薄转发器。 |
| `agent/turn_context.py` | **序幕**：`build_turn_context()`（`:118`）执行每轮一次的全部准备，返回一个 `TurnContext` dataclass（`:92`），其字段被主循环读回（`conversation_loop.py:551-561`）。 |
| `agent/turn_finalizer.py` | **尾声**：`finalize_turn()`（`:30`）处理预算耗尽总结、失败上报、组装返回 dict。 |
| `agent/tool_executor.py` | 工具批次的真正执行（并发 / 串行）。 |
| `agent/tool_dispatch_helpers.py` | 纯函数辅助：并行安全判定、工具结果消息构造等。 |
| `agent/iteration_budget.py` | 线程安全的迭代预算计数器。 |

### 1.3 迭代预算（Iteration Budget）

`agent/iteration_budget.py` 定义了 `IterationBudget` 类（`:17`）——一个加锁的消费/退还计数器：

- 每个 `AIAgent`（父或子）各持一个预算。父 agent 上限 = `max_iterations`（默认 90），每个子 agent 上限 = `delegation.max_iterations`（默认 50）。因此父+子的总迭代数可能超过父的上限（`iteration_budget.py:21-26`）。
- `consume()`（`:37`）尝试消费一次，超限返回 `False`。
- `refund()`（`:45`）退还一次——专用于 `execute_code`（编程式工具调用）轮次，这种 RPC 式调用不该吃预算（主循环 `conversation_loop.py:4142-4144`：当本轮工具集恰好 `== {"execute_code"}` 时退还）。
- 循环条件还有一个 `_budget_grace_call`「宽限调用」机制（`conversation_loop.py:589`、`:608`）：预算耗尽时再给模型一次机会，消费掉 grace flag 后强制退出。

预算真正耗尽时，`finalize_turn`（`turn_finalizer.py:53-70`）会剥掉工具、追加一条 user 消息、做**一次**「无工具」的 API 调用，让模型给出总结（`_handle_max_iterations`）。若是 kanban worker，则同时上报 `timed_out` 失败（`:85-107`），以触发调度器的熔断计数。

### 1.4 工具调用的并发 vs 串行

执行入口在 `agent/tool_executor.py`：

- `execute_tool_calls_concurrent()`（`:283`）：用线程池并发执行，结果按**原始顺序**收集后 append，保证 API 看到的顺序符合预期。
- `execute_tool_calls_sequential()`（`:852`）：逐个执行。

是否能并发，由 `agent/tool_dispatch_helpers.py` 的 `_should_parallelize_tool_batch()`（`:104`）裁决，规则保守：

1. 批次只有 1 个调用 → 串行（`:106`）。
2. 含 `clarify`（`_NEVER_PARALLEL_TOOLS`，`:42`）→ 串行。
3. 路径作用域工具 `read_file/write_file/patch`（`_PATH_SCOPED_TOOLS`，`:60`）：抽取目标路径，若两调用路径**子树重叠**（`_paths_overlap`，`:167`）→ 串行（防文件竞争）。
4. 其余工具必须在只读白名单 `_PARALLEL_SAFE_TOOLS`（`:45`，含 `read_file`、`search_files`、`web_search`、`web_extract`、`session_search`、`vision_analyze`、`skills_list`、`skill_view`、`ha_*` 只读项）中，或是显式声明可并行的 MCP 工具（`_is_mcp_tool_parallel_safe`），否则 → 串行。

### 1.5 工具结果如何喂回

工具执行完，结果被包装成 OpenAI 格式的 `tool` 角色消息（`tool_dispatch_helpers.py:make_tool_result_message`，`:350`），append 进 `messages`。注意 `:394` 的 `_is_untrusted_tool` / `_maybe_wrap_untrusted`（`:402`）：来自不可信来源（如浏览器抓取、第三方内容）的工具结果会被包裹，缓解 prompt injection。

每次工具进展后会增量落盘 session DB（`_flush_session_db_after_tool_progress`，`tool_executor.py:72`），保证进程中途崩溃也能恢复。

---

## 2. Prompt 构建与系统提示：缓存的神圣性

### 2.1 系统提示的「恢复或重建」

主循环的序幕调用 `_restore_or_build_system_prompt()`（`agent/conversation_loop.py:254`）。核心逻辑：

- **续接会话**：从 session DB 读出上一轮存的 `system_prompt` 列，如果存在且**运行时身份匹配**（`_stored_prompt_matches_runtime`，`:378` —— 比对 prompt 里的 `Model:` / `Provider:` 行），就**逐字复用**它（`:303-307`），让 Anthropic 缓存前缀精确命中。
- **首轮 / 损坏恢复**：调用 `agent._build_system_prompt(system_message)` 重建（`:333`），并立刻写回 session DB（`:366-368`）。

这里有大量「三态」诊断日志（`missing` / `null` / `empty` / `present` / `stale_runtime`，`:262-272`）：因为 **gateway 路径每轮都新建一个 `AIAgent`**，完全依赖这次 DB 往返来复用前缀。所以读写失败被刻意提到 WARNING 级别——一次静默失败就意味着该会话此后每轮都重建系统提示、每轮都缓存未命中、成本飙升。

### 2.2 prompt_caching.py：缓存断点怎么打

`agent/prompt_caching.py` 是**纯函数**模块（无类状态、不依赖 AIAgent），实现单一布局 `system_and_3`：

- `apply_anthropic_cache_control()`（`:49`）在消息上放置最多 **4 个 `cache_control` 断点**：系统提示 + 最后 3 条非系统消息，统一 TTL（`5m` 或 `1h`）。
- 文件 docstring（`:1-9`）说明：这能在单会话多轮对话里把输入 token 成本降约 **75%**。
- `_apply_cache_marker()`（`:15`）处理各种 content 形态（str / list / tool 角色 / native_anthropic）。

> 关键：断点放在**最后 3 条**意味着前缀（系统提示 + 早期历史）保持稳定，每轮只有尾部增量是新写入。这正是「缓存神圣」能成立的机制基础。

### 2.3 为什么系统提示必须 byte-stable

`AGENTS.md` 多处强调（`:19-23`、`:88-91`、`:1101-1113`）。具体规则：

- **不得**中途改动过去上下文、中途换工具集、中途重载记忆或重建系统提示（`AGENTS.md:1104-1106`）。
- 系统提示必须在整个会话生命周期内**逐字节稳定**（`AGENTS.md:91`）。
- 会改动系统提示状态的 slash 命令（skills / tools / memory 等）必须**缓存感知**：默认延迟失效（下次会话才生效），提供 `--now` 显式立即失效（`AGENTS.md:1110-1113`，规范样例是 `/skills install --now`）。
- skill slash 命令注入为 **user 消息**而非系统提示，正是为了保住缓存（`AGENTS.md:370`）。

唯一允许改动上下文的时机：**上下文压缩**（见第 3 节）。

> 与缓存相关的「序幕」细节：turn 之间的 MCP 工具刷新（`turn_context.py:169-185`）被设计为 **cache-safe by construction**——它在本轮第一次 API 调用组装 `tools=` 之前运行，只会**扩展**一个全新请求的前缀，绝不改动正在进行中的缓存前缀。

---

## 3. 上下文管理 / 压缩

### 3.1 可插拔的 ContextEngine 抽象

`agent/context_engine.py` 定义抽象基类 `ContextEngine`（`:32`）。这是一个**插件点**：通过 `config.yaml` 的 `context.engine` 选择（默认 `"compressor"`，即内置实现），第三方引擎（如 LCM）可经插件系统替换（`context_engine.py:1-26`）。

引擎负责（`:12-17`）：决定何时压缩、执行压缩、可选暴露工具（如 `lcm_grep`）、跟踪 token 用量。关键接口：

| 方法 | 作用 |
|---|---|
| `update_from_response(usage)` `:71` | 每次 API 响应后更新 token 统计 |
| `should_compress(prompt_tokens)` `:83` | 本轮是否该压缩 |
| `compress(messages, ...)` `:87` | 压缩并返回新消息列表 |

默认参数（`:64-66`）：`threshold_percent=0.75`、`protect_first_n=3`（系统提示之外再保护 3 条头部消息）、`protect_last_n=6`。

### 3.2 内置压缩器 ContextCompressor

`agent/context_compressor.py` 的 `ContextCompressor(ContextEngine)`（`:612`）是默认引擎：

- `should_compress()`（`:964`）：`tokens >= threshold_tokens` 才压缩；带**防抖（anti-thrashing）**保护——若最近两次压缩各自节省不足 10%，则跳过，避免每次只删 1-2 条消息的无限循环，并提示用户改用 `/new` 或 `/compress <topic>`（`:974-983`）。
- `_prune_old_tool_results()`（`:990`）：**廉价预处理（无 LLM 调用）**——把旧的工具结果替换成一行信息化摘要（如 `[terminal] ran \`npm test\` -> exit 0, 47 lines output`），并对相同工具结果去重、截断超大 tool_call 参数。从尾部往前走，保护最近的消息（按 token 预算或消息条数）。

### 3.3 压缩的编排：conversation_compression.py

`agent/conversation_compression.py` 的 `compress_context()`（`:281`）是真正的压缩编排入口：

- 懒加载可行性检查 `check_compression_model_feasibility()`（`:74`）——首次压缩时才探测辅助 provider + 上下文长度，省掉短会话的冷启动开销（`:314-328`）。
- 用 **state.db 支撑的 per-session 锁**防止两次压缩并发（`:349-350`）。
- 支持 `in_place` 模式（config `compression.in_place`，`:331-337`）：原地重写消息列表 + 重建系统提示，但**保持同一 session_id**，消除 session 轮转引发的一类 bug。
- 支持 `focus_topic`（受 Claude Code `/compact <focus>` 启发，`:299-301`）做引导式压缩。
- 压缩**中止**时（辅助 LLM 没产出可用摘要）返回原消息不变、不轮转 session，调用方据 `len(returned)==len(input)` 检测 no-op 并停止重试（`:307-312`）。

### 3.4 主循环里的两个压缩触发点

1. **预飞（preflight）**：在序幕里、第一次 API 调用前做粗估检查（`turn_context.py` 调度，`context_engine.should_compress_preflight`，`context_engine.py:110`）。
2. **响应后**：每轮工具执行后（`conversation_loop.py:4160-4192`），用 API 真实上报的 `last_prompt_tokens` 决定是否压缩（注释 `:4162-4166` 强调**只用 prompt_tokens**，因为思考型模型的 completion/reasoning token 会虚高、导致过早压缩）。若 `last_prompt_tokens==0`（断连后 stale），回退到 `estimate_request_tokens_rough`（含工具 schema，因 50+ 工具能多出 20-30K token，`:4174-4180`）。触发后调用 `agent._compress_context(...)`（`:4184`），并清空 `conversation_history`，让压缩后的消息写入新 session。

### 3.5 trajectory_compressor.py（不同用途）

`trajectory_compressor.py`（根目录）是 `TrajectoryCompressor` 类（`:332`）+ `CompressionConfig`（`:83`）+ `TrajectoryMetrics`（`:183`）。它是**离线轨迹压缩工具**（用于 datagen / 训练数据生成、`main()` 在 `:1356`），与运行时的对话压缩链路不同，不要混淆。「待确认」：本文未深入其内部，仅定位用途。

---

## 4. 工具系统

### 4.1 工具如何定义与注册

中央注册表是 `tools/registry.py`（docstring `:1-15`，已译为中文）。每个工具文件在**模块级**调用 `registry.register(...)` 声明自己。`register()` 签名（`:231-245`）：

```python
registry.register(
    name="example_tool",          # 工具名
    toolset="example",            # 归属的 toolset
    schema={...},                 # 发给模型的 JSON schema
    handler=lambda args, **kw: ...,# 调度入口，必须返回 JSON 字符串
    check_fn=check_requirements,  # 可用性门控（service-gated）
    requires_env=["EXAMPLE_API_KEY"],
    is_async=False, emoji="", ...
)
```

`model_tools.py`（根目录，58KB）查询注册表来生成发给模型的工具定义，而不是维护自己的并行结构。

**自动发现**：`discover_builtin_tools()`（`registry.py:57`）用 AST 扫描 `tools/*.py`，凡含顶层 `registry.register(...)` 调用（`_module_registers_tools`，`:42`）的模块就自动 import——无需维护手动 import 列表。

> 但「被发现」≠「被暴露」。`AGENTS.md:534-536` 强调：工具只有当其名字出现在某个 toolset 里时才会真正暴露给 agent。新增 core tool 需改 **2 个文件**：`tools/your_tool.py` + 在 `toolsets.py` 里把工具名加入 `_HERMES_CORE_TOOLS` 或新 toolset。

### 4.2 Toolset：分组与组合

所有 toolset 定义在 `toolsets.py`（docstring 已译中文 `:1-23`）的单一 `TOOLSETS` dict（`:88`）。每个平台适配器挑一个基础 toolset（如 Telegram 用 `"messaging"`），`_HERMES_CORE_TOOLS`（`:30-73`）是多数平台继承的默认包。

`_HERMES_CORE_TOOLS` 当前包含：Web（`web_search`/`web_extract`）、终端+进程（`terminal`/`process`/`read_terminal`）、文件（`read_file`/`write_file`/`patch`/`search_files`）、视觉与图像生成、skills（`skills_list`/`skill_view`/`skill_manage`）、浏览器自动化（一组 `browser_*`）、TTS、规划与记忆（`todo`/`memory`）、`session_search`、`clarify`、`execute_code`/`delegate_task`、`cronjob`、Home Assistant（`ha_*`，由 `HASS_TOKEN` 经 check_fn 门控）、kanban（仅 worker 或显式启用时进 schema）、`computer_use`（macOS，门控）。

**组合机制**：`resolve_toolset()`（`toolsets.py:657`）递归解析——一个 toolset 可经 `includes` 引用其他 toolset，函数带循环/钻石依赖检测（`:684-690`），并支持 `"all"`/`"*"` 特殊别名展开所有工具（`:676-682`）。当前 toolset key 列表见 `AGENTS.md:942-946`（`browser`/`code_execution`/`delegation`/`file`/`memory`/`messaging`/`terminal`/`vision`/`web` 等约 30 个）。

**启用/禁用**：用户经 `hermes tools`（curses UI）或 `config.yaml` 的 `tools.<platform>.enabled` / `disabled` 列表按平台控制（`AGENTS.md:948-950`）。

**按需门控（service-gated）**：`check_fn` 让工具仅在前置条件满足时出现（如 HA 工具需 token），否则零足迹——这是 Footprint Ladder 的第 3 级。

`toolset_distributions.py`（根目录）是另一回事：它为 **datagen 批量运行**定义「按概率选用哪些 toolset」的分发方案（docstring `:1-20`），不影响交互式运行。

### 4.3 代表性工具分类速览

| 类别 | 工具文件 | 说明 |
|---|---|---|
| 文件 | `tools/file_tools.py` | `read_file`/`write_file`/`patch`/`search_files`，路径作用域工具（并发时防竞争） |
| 终端 | `tools/terminal_tool.py` | 在 local/Docker/Modal/SSH/Singularity/Daytona 多后端执行命令，含托管 Modal 模式 |
| 浏览器 | `tools/browser_tool.py` | 基于 agent-browser CLI，后端可选 Browser Use（云）/Browserbase/本地 Chromium，按凭据自动选择；另有 `browser_cdp_tool.py`、`browser_dialog_tool.py` 等 |
| 编程式工具调用 | `tools/code_execution_tool.py` | `execute_code`：让模型写 Python 脚本经 RPC 调 Hermes 工具，把多步工具链压成一次推理回合（PTC）；其迭代会被退还预算 |
| 委派 | `tools/delegate_tool.py` + `tools/async_delegation.py` | spawn 隔离子 agent（见第 6 节） |
| 跨平台消息 | `tools/send_message_tool.py` | 向 Telegram/Discord/Slack 等已连接平台的用户/渠道发消息；CLI 与 gateway 通用 |
| 记忆 | `tools/memory_tool.py` | 有界、基于文件、跨会话的精选记忆：`MEMORY.md`（agent 笔记）+ `USER.md`（对用户的了解）；agent-level 工具，在 `run_agent.py` 中被拦截 |
| 规划 | `tools/todo_tool.py` | 内存中任务列表，每会话一个，压缩后重新注入对话 |
| 技能 | `tools/skills_tool.py` / `skill_manager_tool.py` | 列出/查看 SKILL.md 文档；管理技能生命周期（配合 Curator） |
| 视觉/Web | `tools/vision_tools.py` / `web_tools.py` | 视觉分析（多 provider 路由）/ Web 搜索抓取（backend 在 `hermes tools` 配置） |
| 交互澄清 | `tools/clarify_tool.py` | 向用户出结构化多选/开放题；真正交互逻辑在平台层（CLI/gateway） |
| 其他门控工具 | `homeassistant_tool.py`、`kanban_tools.py`、`cronjob_tools.py`、`computer_use_tool.py`、`image_generation_tool.py`、`tts_tool.py`、`mcp_tool.py` 等 | 多数经 check_fn 按服务/环境门控 |

`memory`/`todo` 这类**agent-level 工具**在 `run_agent.py` 里于 `handle_function_call()` 之前被拦截（`AGENTS.md:544`）。

---

## 5. Footprint Ladder（足迹阶梯）

### 5.1 哲学：内核是窄腰，每个工具都「按次收费」

`AGENTS.md:24-27` 与 `:171-200` 阐明：每个 model tool 都随**每一次** API 调用发送，所以新增 *core* tool 的门槛极高。新增能力时应**选最高（足迹最小）且能正确解决问题的那一级**：

| 级别 | 名称 | 何时用 | 足迹 |
|---|---|---|---|
| 1 | **扩展现有代码** | 能力只是已有东西的变体 | 零新增 |
| 2 | **CLI 命令 + skill** | 可表达为 shell 命令的配置/状态/基建管理；agent 在 skill 引导下跑 `hermes <子命令>` | 零 model-tool 足迹（订阅、定时任务、服务安装的默认选择，如 `hermes webhook`/`cron`/`tools`） |
| 3 | **Service-gated tool（`check_fn`）** | 需结构化参数/返回，且仅当前置条件配置好才出现 | 否则零足迹（如 HA 工具、记忆 provider 工具） |
| 4 | **Plugin** | 第三方/小众/用户特定能力，不进内核 | 在 `~/.hermes/plugins/` 或 pip 包，运行时发现 |
| 5 | **MCP server（进 catalog）** | 确实需要是工具但非内核基础；建成 MCP server 加入 catalog，经内置 MCP 客户端连接 | 零永久 core-schema 足迹，任何 MCP host 可复用 |
| 6 | **新 core tool** | **最后手段**：能力是基础性的、几乎对每个用户都广泛有用、且 terminal+file（或 MCP）无法触达 | 永久 schema 足迹（正例：terminal、read_file、web_search、browser_navigate） |

### 5.2 实践含义

- 「当 terminal + file 已能完成、或一个 skill 就够时，新增 core tool 会被拒」（`AGENTS.md:108-110`）。若障碍只是「远端后端文件不可见」，应修挂载点而非加工具。
- 自定义/本地工具**不要改 Hermes core**：走 plugin 路线（`~/.hermes/plugins/<name>/plugin.yaml` + `__init__.py`，`ctx.register_tool(...)`），plugin toolset 自动发现，无需碰 `tools/` 或 `toolsets.py`（`AGENTS.md:500-506`）。
- 当 3+ 个 open PR 想集成**同一类**东西（记忆后端、provider、通知器），不要一个个 merge——设计一个 ABC + orchestrator，把内置实现包装成第一个 provider，把竞争 PR 变成插件（`AGENTS.md:197-200`）。

---

## 6. 子 agent / 委派

### 6.1 delegate_task：派生隔离子 agent

`tools/delegate_tool.py`（docstring 已译中文 `:1-17`）派生子 `AIAgent` 实例，每个子 agent 拥有：

- 全新对话（**不含**父 agent 历史）；
- 自己的 `task_id`（独立终端会话、文件操作缓存）；
- 受限工具集（始终剥离被屏蔽工具）；
- 由委派目标 + 上下文构建的聚焦型系统提示。

**关键隔离性**：父 agent 的上下文只能看到「委派调用本身 + 汇总结果」，**永远看不到**子 agent 的中间工具调用或推理过程（`delegate_tool.py:15-16`）。这既保护父 agent 的 prompt 缓存前缀，又压缩了上下文。

### 6.2 两种形态 + 角色

`AGENTS.md:954-985`：

- **单任务**：传 `goal`（+ 可选 `context`、`toolsets`）。
- **批量（并行）**：传 `tasks: [...]`，每个起一个并发子 agent；并发上限 `delegation.max_concurrent_children`（默认 3）。

**角色**：

- `role="leaf"`（默认）：聚焦工人。**不能**调 `delegate_task`、`clarify`、`memory`、`send_message`、`execute_code`（`AGENTS.md:971-973`）。被屏蔽工具集中定义在 `delegate_tool.py:45-54` 的 `DELEGATE_BLOCKED_TOOLS`（含 `delegate_task` 防递归、`memory` 防写共享 `MEMORY.md`、`cronjob` 防替父调度等）。
- `role="orchestrator"`：保留 `delegate_task` 以便自己再派生工人；受 `delegation.orchestrator_enabled`（默认 true）和 `delegation.max_spawn_depth`（默认 2）约束。

默认**父 agent 阻塞**等子 agent 的 summary 返回再继续自己的循环。

### 6.3 同步 vs 异步（background）

- 默认同步：父等待。
- `background=true`：立即返回一个 delegation id，结果稍后经**异步委派完成队列**重新进入对话（`AGENTS.md:957-960`）。
- 异步实现在 `tools/async_delegation.py`：`dispatch_async_delegation()`（`:147`）/ `dispatch_async_delegation_batch()`（`:327`）用守护线程池 `_DaemonThreadPoolExecutor`（`:46`）执行；`_finalize()`（`:256`）+ `_push_completion_event()`（`:272`）把完成事件推回；`list_async_delegations()`（`:493`）/ `interrupt_all()`（`:505`）做管理。

**子 agent 审批**：子 agent 跑在线程池工作线程，不继承 CLI 的交互式审批回调，否则会与持有 stdin 的父 TUI 死锁。修复是经 `ThreadPoolExecutor(initializer=...)` 给每个工作线程装一个非交互回调（`delegate_tool.py:57-71`）：默认 `_subagent_auto_deny`（安全），`delegation.subagent_auto_approve=true` 时才用 auto-approve（cron/批量的可选 YOLO 模式）。

### 6.4 持久性规则

background `delegate_task` 虽脱离当前 turn 但仍是**进程本地**的。需要在进程重启后存活的工作，应改用 `cronjob` 或 `terminal(background=True, notify_on_complete=True)`（`AGENTS.md:982-984`）。

> 已知陷阱（`AGENTS.md:1202-1203`）：`model_tools.py` 里的 `_last_resolved_tool_names` 是进程全局，`delegate_tool.py` 的 `_run_single_child()` 会在子 agent 执行前后保存/恢复它——子 agent 运行期间该全局可能暂时是 stale 的。

---

## 附：本章关键文件索引

| 主题 | 文件 |
|---|---|
| 主循环 | `agent/conversation_loop.py`（`run_conversation` `:495`，`while` `:589`） |
| 序幕 | `agent/turn_context.py`（`build_turn_context` `:118`，`TurnContext` `:92`） |
| 尾声 | `agent/turn_finalizer.py`（`finalize_turn` `:30`） |
| 工具执行 | `agent/tool_executor.py`（并发 `:283`/串行 `:852`） |
| 调度辅助 | `agent/tool_dispatch_helpers.py`（并行判定 `:104`） |
| 预算 | `agent/iteration_budget.py` |
| 缓存断点 | `agent/prompt_caching.py`（`apply_anthropic_cache_control` `:49`） |
| 提示恢复 | `agent/conversation_loop.py:254`（`_restore_or_build_system_prompt`） |
| 上下文引擎抽象 | `agent/context_engine.py` |
| 内置压缩器 | `agent/context_compressor.py`（`should_compress` `:964`） |
| 压缩编排 | `agent/conversation_compression.py`（`compress_context` `:281`） |
| 工具注册表 | `tools/registry.py`（`register` `:231`，自动发现 `:57`） |
| 工具集 | `toolsets.py`（`_HERMES_CORE_TOOLS` `:30`，`resolve_toolset` `:657`） |
| 委派 | `tools/delegate_tool.py`、`tools/async_delegation.py` |
| 设计铁律 | `AGENTS.md`（缓存神圣 `:19`；Footprint Ladder `:171`；Toolsets `:935`；Delegation `:954`） |
