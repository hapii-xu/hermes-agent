# Hermes Agent 学习文档索引

> 本目录包含 Hermes Agent 项目的详细学习文档。

## 文档列表

| # | 文档 | 描述 |
|---|------|------|
| 01 | [项目概览](./01-项目概览.md) | 项目简介、核心文件、技术栈、两种运行模式、安装与开发 |
| 02 | [消息处理全链路](./02-消息处理全链路.md) | ⭐ 从用户发送消息到 AI 响应的完整流程，含时序图和代码路径 |
| 03 | [记忆系统](./03-记忆系统.md) | MEMORY.md/USER.md 的存储格式、加载时机、冻结快照模式 |
| 04 | [技能系统](./04-技能系统.md) | SKILL.md 结构、发现/加载机制、条件加载、自我创建 |
| 05 | [存储系统](./05-存储系统.md) | SQLite SessionDB、JSONL 日志、配置文件、Profile 多实例 |
| 06 | [工具系统](./06-工具系统.md) | 自注册机制、工具集、并行执行、审批机制、终端后端 |

## 推荐阅读顺序

1. **01-项目概览** — 先了解项目全貌
2. **02-消息处理全链路** — 理解核心流程（最重要的一篇）
3. **03-记忆系统** — 理解 AI 如何记忆
4. **04-技能系统** — 理解 AI 如何扩展能力
5. **05-存储系统** — 理解数据如何持久化
6. **06-工具系统** — 理解 AI 如何与外部世界交互

## 核心代码路径速查

| 组件 | 关键文件 | 关键函数/类 |
|------|---------|------------|
| 核心对话循环 | `run_agent.py` | `AIAgent.run_conversation()` |
| CLI | `cli.py` | `HermesCLI` |
| 工具注册 | `tools/registry.py` | `ToolRegistry`, `discover_builtin_tools()` |
| 工具分发 | `model_tools.py` | `handle_function_call()` |
| 记忆管理 | `agent/memory_manager.py` | `MemoryManager` |
| 记忆存储 | `tools/memory_tool.py` | `MemoryStore` |
| 记忆抽象 | `agent/memory_provider.py` | `MemoryProvider` (ABC) |
| 提示词构建 | `agent/prompt_builder.py` | `build_skills_system_prompt()` |
| 会话存储 | `hermes_state.py` | `SessionDB` |
| 消息网关 | `gateway/run.py` | `GatewayRunner._handle_message()` |
| 技能命令 | `agent/skill_commands.py` | `_load_skill_payload()` |
| 工具集 | `toolsets.py` | `_HERMES_CORE_TOOLS` |
| 终端后端 | `tools/environments/` | `BaseEnvironment` 及子类 |
| 上下文压缩 | `agent/context_compressor.py` | `ContextCompressor` |
