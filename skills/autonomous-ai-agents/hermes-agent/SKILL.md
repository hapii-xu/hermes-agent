---
name: hermes-agent
description: "配置、扩展或为 Hermes Agent 贡献代码。"
version: 2.2.0
author: Hermes Agent + Teknium
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hermes, setup, configuration, multi-agent, spawning, cli, gateway, development]
    homepage: https://github.com/NousResearch/hermes-agent
    related_skills: [claude-code, codex, opencode]
---

# Hermes Agent

Hermes Agent 是 Nous Research 推出的开源 AI agent 框架，可运行于你的终端、消息平台和 IDE。它属于与 Claude Code（Anthropic）、Codex（OpenAI）和 OpenClaw 同一类别——通过工具调用与你的系统交互的自主编程与任务执行 agent。Hermes 兼容任何 LLM 提供商（OpenRouter、Anthropic、OpenAI、DeepSeek、本地模型，以及 15+ 其他厂商），可在 Linux、macOS 和 WSL 上运行。

Hermes 的不同之处：

- **通过 skill 自我改进** — Hermes 通过将可复用的流程保存为 skill 来从经验中学习。当它解决了一个复杂问题、发现了一套工作流，或被纠正时，可以将这些知识固化为 skill 文档，加载到后续会话中。skill 会随时间不断积累，使 agent 在你的具体任务和环境中越来越出色。
- **跨会话持久记忆** — 记住你是谁、你的偏好、环境细节和经验教训。可插拔的记忆后端（内置、Honcho、Mem0 等）让你自由选择记忆的实现方式。
- **多平台网关** — 同一个 agent 可运行于 Telegram、Discord、Slack、WhatsApp、Signal、Matrix、Email 以及 10+ 其他平台，并享有完整工具访问权限，而不仅仅是聊天。
- **与提供商无关** — 可在 workflow 中途切换模型和提供商，其他部分无需改动。凭据池会在多个 API key 之间自动轮换。
- **Profile（配置档案）** — 运行多个相互独立的 Hermes 实例，各自拥有隔离的配置、会话、skill 和记忆。
- **可扩展** — 插件、MCP 服务器、自定义工具、webhook 触发器、cron 调度，以及完整的 Python 生态。

人们用 Hermes 做软件开发、研究、系统管理、数据分析、内容创作、家庭自动化，以及任何能从拥有持久上下文和完整系统访问权限的 AI agent 中获益的事。

**本 skill 帮助你高效地使用 Hermes Agent** —— 包括安装配置、功能配置、派生额外的 agent 实例、排查问题、找到合适的命令和设置，以及在需要扩展或贡献代码时理解系统的工作方式。

**文档：** https://hermes-agent.nousresearch.com/docs/

## 范围与验证

本 skill 是一份简明的操作指南，并非每个 Hermes 功能的完整权威来源。如果某个 Hermes 功能、命令或设置在这里没有被提及，不要把这种缺失当作它不存在的证据。在给出否定回答前，请查阅实时仓库和官方文档。

良好的验证目标：

- CLI 命令：`hermes --help`、`hermes <command> --help` 以及 `hermes_cli/main.py`
- 用户文档：https://hermes-agent.nousresearch.com/docs/
- 源码树：https://github.com/NousResearch/hermes-agent

## 快速开始

```bash
# 安装
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash

# 交互式聊天（默认）
hermes

# 单次查询
hermes chat -q "What is the capital of France?"

# 设置向导
hermes setup

# 更改模型/提供商
hermes model

# 健康检查
hermes doctor
```

---

## CLI 参考

### 全局标志

```
hermes [flags] [command]

  --version, -V             显示版本
  --resume, -r SESSION      按 ID 或标题恢复会话
  --continue, -c [NAME]     按名称恢复，或恢复最近会话
  --worktree, -w            隔离的 git worktree 模式（并行 agent）
  --skills, -s SKILL        预加载 skill（逗号分隔或重复使用）
  --profile, -p NAME        使用具名 profile
  --yolo                    跳过危险命令审批
  --pass-session-id         在系统提示词中包含会话 ID
```

不带子命令时默认为 `chat`。

### Chat

```
hermes chat [flags]
  -q, --query TEXT          单次查询，非交互式
  -m, --model MODEL         模型（例如 anthropic/claude-sonnet-4）
  -t, --toolsets LIST       逗号分隔的工具集
  --provider PROVIDER       强制使用提供商（openrouter、anthropic、nous 等）
  -v, --verbose             详细输出
  -Q, --quiet               抑制 banner、spinner、工具预览
  --checkpoints             启用文件系统检查点（/rollback）
  --source TAG              会话来源标签（默认：cli）
```

### 配置

```
hermes setup [section]      交互式向导（model|terminal|gateway|tools|agent）
hermes model                交互式模型/提供商选择器
hermes config               查看当前配置
hermes config edit          在 $EDITOR 中打开 config.yaml
hermes config set KEY VAL   设置某个配置值
hermes config path          打印 config.yaml 路径
hermes config env-path      打印 .env 路径
hermes config check         检查缺失/过时的配置
hermes config migrate       用新选项更新配置
hermes auth                 交互式凭据管理器
hermes auth add PROVIDER    添加 OAuth 或 API key 凭据（例如 nous、openai-codex、qwen-oauth）
hermes auth list            列出已存储的凭据
hermes auth remove PROVIDER 移除某个已存储的凭据
hermes doctor [--fix]       检查依赖和配置
hermes status [--all]       显示组件状态
```

### 工具与 skill

```
hermes tools                交互式工具启用/禁用（curses UI）
hermes tools list           显示所有工具及状态
hermes tools enable NAME    启用某个工具集
hermes tools disable NAME   禁用某个工具集

hermes skills list          列出已安装的 skill
hermes skills search QUERY  搜索 skill 中心
hermes skills install ID    安装 skill（ID 可以是中心标识符或直接的 https://…/SKILL.md URL；当 frontmatter 没有 name 时，传 --name 覆盖）
hermes skills inspect ID    不安装直接预览
hermes skills config        按平台启用/禁用 skill
hermes skills check         检查更新
hermes skills update        更新过时的 skill
hermes skills uninstall N   移除中心 skill
hermes skills publish PATH  发布到注册表
hermes skills browse        浏览所有可用 skill
hermes skills tap add REPO  添加 GitHub 仓库作为 skill 源
```

### MCP 服务器

```
hermes mcp serve            将 Hermes 作为 MCP 服务器运行
hermes mcp add NAME         添加 MCP 服务器（--url 或 --command）
hermes mcp remove NAME      移除 MCP 服务器
hermes mcp list             列出已配置的服务器
hermes mcp test NAME        测试连接
hermes mcp configure NAME   切换工具选择
```

内置 MCP 客户端如何连接服务器（stdio/HTTP）、自动发现其工具并将其暴露为
一等工具，以及目录安装（`hermes mcp install <name>`）：
`skill_view(name="hermes-agent", file_path="references/native-mcp.md")`。

### 网关（消息平台）

```
hermes gateway run          前台启动网关
hermes gateway install      安装为后台服务
hermes gateway start/stop   控制该服务
hermes gateway restart      重启该服务
hermes gateway status       检查状态
hermes gateway setup        配置平台
```

支持的平台：Telegram、Discord、Slack、WhatsApp、Signal、Email、SMS、Matrix、Mattermost、Home Assistant、DingTalk、Feishu、WeCom、BlueBubbles（iMessage）、Weixin（WeChat）、API Server、Webhooks。Open Web UI 通过 API Server 适配器接入。

平台文档：https://hermes-agent.nousresearch.com/docs/user-guide/messaging/

### 会话

```
hermes sessions list        列出最近的会话
hermes sessions browse      交互式选择器
hermes sessions export OUT  导出为 JSONL
hermes sessions rename ID T 重命名会话
hermes sessions delete ID   删除会话
hermes sessions prune       清理旧会话（--older-than N days）
hermes sessions stats       会话存储统计
```

### Cron 任务

```
hermes cron list            列出任务（--all 包含已禁用的）
hermes cron create SCHED    创建：'30m'、'every 2h'、'0 9 * * *'
hermes cron edit ID         编辑调度、提示词、投递方式
hermes cron pause/resume ID 控制任务状态
hermes cron run ID          在下一个 tick 触发
hermes cron remove ID       删除任务
hermes cron status          调度器状态
```

### Webhook

```
hermes webhook subscribe N  在 /webhooks/<name> 创建路由
hermes webhook list         列出订阅
hermes webhook remove NAME  移除某个订阅
hermes webhook test NAME    发送测试 POST
```

完整设置、路由配置、payload 模板和事件驱动 agent 运行模式：
`skill_view(name="hermes-agent", file_path="references/webhooks.md")`。

### Profile

```
hermes profile list         列出所有 profile
hermes profile create NAME  创建（--clone、--clone-all、--clone-from）
hermes profile use NAME     设为固定默认
hermes profile delete NAME  删除 profile
hermes profile show NAME    显示详情
hermes profile alias NAME   管理包装脚本
hermes profile rename A B   重命名 profile
hermes profile export NAME  导出为 tar.gz
hermes profile import FILE  从归档导入
```

### 凭据池

```
hermes auth add             交互式凭据向导
hermes auth list [PROVIDER] 列出池化凭据
hermes auth remove P INDEX  按提供商 + 索引移除
hermes auth reset PROVIDER  清除耗尽状态
```

### 其他

```
hermes insights [--days N]  使用分析
hermes update               更新到最新版本
hermes pairing list/approve/revoke  DM 授权
hermes plugins list/install/remove  插件管理
hermes honcho setup/status  Honcho 记忆集成（需要 honcho 插件）
hermes memory setup/status/off  记忆提供商配置
hermes completion bash|zsh  Shell 自动补全
hermes acp                  ACP 服务器（IDE 集成）
hermes claw migrate         从 OpenClaw 迁移
hermes uninstall            卸载 Hermes
```

---

## Slash 命令（会话内）

在交互式聊天会话中输入这些命令。新命令会相当频繁地加入；如果下面的内容看起来过时，可在会话中运行 `/help` 获取权威列表，或参见[实时 slash 命令参考](https://hermes-agent.nousresearch.com/docs/reference/slash-commands)。权威注册表是 `hermes_cli/commands.py` —— 每个消费者（自动补全、Telegram 菜单、Slack 映射、`/help`）都从它派生。

### 会话控制
```
/new (/reset)        全新会话
/clear               清屏 + 新会话（CLI）
/retry               重发上一条消息
/undo                移除上一轮交互
/title [name]        为会话命名
/compress            手动压缩上下文
/stop                终止后台进程
/rollback [N]        恢复文件系统检查点
/snapshot [sub]      创建或恢复 Hermes 配置/状态快照（CLI）
/background <prompt> 在后台运行提示词
/queue <prompt>      排队等待下一轮
/steer <prompt>      在下一次工具调用后注入消息而不打断
/agents (/tasks)     显示活跃的 agent 和运行中的任务
/resume [name]       恢复具名会话
/goal [text|sub]     设置一个 Hermes 跨轮次持续推进直至完成的目标
                     （子命令：status、pause、resume、clear）
/redraw              强制完整重绘 UI（CLI）
```

### 配置
```
/config              显示配置（CLI）
/model [name]        显示或更改模型
/personality [name]  设置人格
/reasoning [level]   设置推理（none|minimal|low|medium|high|xhigh|show|hide）
/verbose             循环：off → new → all → verbose
/voice [on|off|tts]  语音模式
/yolo                切换审批绕过
/busy [sub]          控制 Hermes 工作时 Enter 键的行为（CLI）
                     （子命令：queue、steer、interrupt、status）
/indicator [style]   选择 TUI 忙碌指示器样式（CLI）
                     （样式：kaomoji、emoji、unicode、ascii）
/footer [on|off]     切换最终回复上的网关运行时元数据 footer
/skin [name]         更改主题（CLI）
/statusbar           切换状态栏（CLI）
```

### 工具与 skill
```
/tools               管理工具（CLI）
/toolsets            列出工具集（CLI）
/skills              搜索/安装 skill（CLI）
/skill <name>        将 skill 加载到会话
/reload-skills       重新扫描 ~/.hermes/skills/ 以发现新增/移除的 skill
/reload              将 .env 变量重新加载到运行中的会话（CLI）
/reload-mcp          重新加载 MCP 服务器
/cron                管理 cron 任务（CLI）
/curator [sub]       后台 skill 维护（status、run、pin、archive、…）
/kanban [sub]        多 profile 协作看板（tasks、links、comments）
/plugins             列出插件（CLI）
```

### 网关
```
/approve             批准一个待处理命令（gateway）
/deny                拒绝一个待处理命令（gateway）
/restart             重启网关（gateway）
/sethome             将当前聊天设为 home 频道（gateway）
/update              将 Hermes 更新到最新（gateway）
/topic [sub]         启用或查看 Telegram DM topic 会话（gateway）
/platforms (/gateway) 显示平台连接状态（gateway）
```

### 工具
```
/branch (/fork)      分叉当前会话
/fast                切换优先/快速处理
/browser             打开 CDP 浏览器连接
/history             显示对话历史（CLI）
/save                将对话保存到文件（CLI）
/copy [N]            将上一条助手回复复制到剪贴板（CLI）
/paste               附上剪贴板图片（CLI）
/image               附上本地图片文件（CLI）
```

### 信息
```
/help                显示命令
/commands [page]     浏览所有命令（gateway）
/usage               token 使用量
/insights [days]     使用分析
/status              会话信息（gateway）
/profile             活跃 profile 信息
/debug               上传调试报告（系统信息 + 日志）并获取可分享链接
```

### 退出
```
/quit (/exit, /q)    退出 CLI
```

---

## 关键路径与配置

```
~/.hermes/config.yaml       主配置
~/.hermes/.env              API key 和密钥（在 $HERMES_HOME 下，如已设置）
$HERMES_HOME/skills/        已安装的 skill
~/.hermes/sessions/         网关路由索引、请求转储、*.jsonl 转录（当 sessions.write_json_snapshots: true 时还有可选的每会话 JSON 快照）
~/.hermes/state.db          权威会话存储（SQLite + FTS5）
~/.hermes/logs/             网关和错误日志
~/.hermes/auth.json         OAuth token 和凭据池
~/.hermes/hermes-agent/     源代码（如果是 git 安装）
```

Profile 使用 `~/.hermes/profiles/<name>/`，布局相同。

### 配置小节

用 `hermes config edit` 或 `hermes config set section.key value` 编辑。

| 小节 | 键选项 |
|---------|-------------|
| `model` | `default`、`provider`、`base_url`、`api_key`、`context_length` |
| `agent` | `max_turns`（90）、`tool_use_enforcement` |
| `terminal` | `backend`（local/docker/ssh/modal）、`cwd`、`timeout`（180） |
| `compression` | `enabled`、`threshold`（0.50）、`target_ratio`（0.20） |
| `display` | `skin`、`tool_progress`、`show_reasoning`、`show_cost` |
| `stt` | `enabled`、`provider`（local/groq/openai/mistral） |
| `tts` | `provider`（edge/elevenlabs/openai/minimax/mistral/neutts） |
| `memory` | `memory_enabled`、`user_profile_enabled`、`provider` |
| `security` | `tirith_enabled`、`website_blocklist` |
| `delegation` | `model`、`provider`、`base_url`、`api_key`、`max_iterations`（50）、`reasoning_effort` |
| `checkpoints` | `enabled`、`max_snapshots`（50） |

完整配置参考：https://hermes-agent.nousresearch.com/docs/user-guide/configuration

### 提供商

支持 20+ 提供商。通过 `hermes model` 或 `hermes setup` 设置。

| 提供商 | 认证方式 | Key 环境变量 |
|----------|------|-------------|
| OpenRouter | API key | `OPENROUTER_API_KEY` |
| Anthropic | API key | `ANTHROPIC_API_KEY` |
| Nous Portal | OAuth | `hermes auth` |
| OpenAI Codex | OAuth | `hermes auth` |
| GitHub Copilot | Token | `COPILOT_GITHUB_TOKEN` |
| Google Gemini | API key | `GOOGLE_API_KEY` 或 `GEMINI_API_KEY` |
| DeepSeek | API key | `DEEPSEEK_API_KEY` |
| xAI / Grok | API key | `XAI_API_KEY` |
| Hugging Face | Token | `HF_TOKEN` |
| Z.AI / GLM | API key | `GLM_API_KEY` |
| MiniMax | API key | `MINIMAX_API_KEY` |
| MiniMax CN | API key | `MINIMAX_CN_API_KEY` |
| Kimi / Moonshot | API key | `KIMI_API_KEY` |
| Alibaba / DashScope | API key | `DASHSCOPE_API_KEY` |
| Xiaomi MiMo | API key | `XIAOMI_API_KEY` |
| Kilo Code | API key | `KILOCODE_API_KEY` |
| OpenCode Zen | API key | `OPENCODE_ZEN_API_KEY` |
| OpenCode Go | API key | `OPENCODE_GO_API_KEY` |
| Qwen OAuth | OAuth | `hermes auth add qwen-oauth` |
| 自定义端点 | 配置 | config.yaml 中的 `model.base_url` + `model.api_key` |
| GitHub Copilot ACP | 外部 | `COPILOT_CLI_PATH` 或 Copilot CLI |

完整提供商文档：https://hermes-agent.nousresearch.com/docs/integrations/providers

### 工具集

通过 `hermes tools`（交互式）或 `hermes tools enable/disable NAME` 启用/禁用。

| 工具集 | 提供的能力 |
|---------|-----------------|
| `web` | 网络搜索和内容提取 |
| `search` | 仅网络搜索（`web` 的子集） |
| `browser` | 浏览器自动化（Browserbase、Camofox 或本地 Chromium） |
| `terminal` | Shell 命令和进程管理 |
| `file` | 文件读/写/搜索/打补丁 |
| `code_execution` | 沙箱化的 Python 执行 |
| `vision` | 图像分析 |
| `image_gen` | AI 图像生成 |
| `video` | 视频分析和生成 |
| `tts` | 文本转语音 |
| `skills` | skill 浏览和管理 |
| `memory` | 跨会话持久记忆 |
| `session_search` | 搜索过往对话 |
| `delegation` | 子 agent 任务委派 |
| `cronjob` | 定时任务管理 |
| `clarify` | 向用户提出澄清性问题 |
| `messaging` | 跨平台消息发送 |
| `todo` | 会话内任务规划和跟踪 |
| `kanban` | 多 agent 工作队列工具（仅对 worker 开放） |
| `debugging` | 额外的内省/调试工具（默认关闭） |
| `safe` | 用于锁定会话的极简、低风险工具集 |
| `spotify` | Spotify 播放和歌单控制 |
| `homeassistant` | 智能家居控制（默认关闭） |
| `discord` | Discord 集成工具 |
| `discord_admin` | Discord 管理/审核工具 |
| `feishu_doc` | 飞书（Lark）文档工具 |
| `feishu_drive` | 飞书（Lark）云盘工具 |
| `yuanbao` | 元宝集成工具 |
| `rl` | 强化学习工具（默认关闭） |
| `moa` | Mixture of Agents（默认关闭） |

完整枚举位于 `toolsets.py` 的 `TOOLSETS` 字典；`_HERMES_CORE_TOOLS` 是大多数平台继承的默认捆绑包。

工具更改在 `/reset`（新会话）时生效。它们不会在对话中途应用，以保留提示词缓存。

---

## 项目上下文文件

Hermes 通过读取工作目录中的上下文文件，将项目级指令注入系统提示词。发现顺序是**第一个匹配胜出** —— 每个会话只加载一个项目上下文来源。

| 文件（按优先级顺序） | 发现方式 | 适用场景 |
|---|---|---|
| `.hermes.md` / `HERMES.md` | 向上遍历父目录至 git 根，在 git 根停止 | 你想要分层项目规则（根级 + 每包覆盖） |
| `AGENTS.md` / `agents.md` | **仅当前目录** —— 忽略子目录和父目录副本 | 你想要可在 Hermes、Claude Code、Codex 等之间通用的可移植 agent 指令 |
| `CLAUDE.md` / `claude.md` | 仅当前目录 | 同 AGENTS.md，Claude 风味 |
| `.cursorrules` / `.cursor/rules/*.mdc` | 仅当前目录 | 从 Cursor 迁移 |

`SOUL.md`（位于 `$HERMES_HOME`）是独立的，存在时总是加载 —— 它设定 agent 的身份，而非项目规则。

### 选择合适的一个

- **使用 `.hermes.md`** 当你想要位于 cwd 之上（根 + 子树）的 Hermes 专属行为，或希望规则从父目录继承时。父目录遍历会在 git 根停止，因此家目录级的 `.hermes.md` 不会泄漏到每个项目中（git 仓库的根是边界）。
- **使用 `AGENTS.md`** 当同一项目也会被其他 agent（Codex、Claude Code、OpenCode）处理时。这些工具对 `AGENTS.md` 有各自约定，而"仅当前目录"契约让该文件保持可移植。
- **不要把项目规则放在 `~/.hermes/AGENTS.md`**（或任何其他家目录级位置）。当 Hermes 以该目录为 cwd 运行时，文件会加载 —— 但仅对那一个目录有效。对于跨项目上下文，请使用 `SOUL.md`（位于 `$HERMES_HOME`，仅身份）或通过 `hermes skills install` 安装 skill。

### 大小与截断

每个上下文文件上限为 20,000 字符。超出此长度的文件会被**头 + 尾截断**（中间被丢弃，并带有 `[...truncated...]` 标记）。对于大型项目规则，宁可拆分为多个 skill，也不要塞进一个文件。

### 安全

所有上下文文件在进入系统提示词前都会经过威胁模式扫描器。匹配提示词注入或 promptware 的模式会被替换为 `[BLOCKED: ...]` 占位符。这意味着含有明显注入企图的 `AGENTS.md` 不会到达模型 —— 扫描器屏蔽的是内容而非文件，因此文件的其余部分仍会加载。

### 对单个会话禁用

`hermes --ignore-rules` 跳过所有项目上下文文件（`.hermes.md`、`AGENTS.md`、`CLAUDE.md`、`.cursorrules`）**以及** `SOUL.md` 身份、用户配置、插件和 MCP 服务器的自动注入。用它来隔离问题是出自你的设置还是 Hermes 本身。

### 示例：一个小型 `.hermes.md`

```markdown
# My Project

Hermes：在本仓库中工作时，遵循这些规则。

## 构建
- 在宣布改动完成前，始终运行 `make test`。
- Python 用 `uv run`，不用 `pip install`。

## 风格
- 优先用 `pathlib.Path` 而非 `os.path`。
- 生产代码中不要 `print()` —— 用 `logger`。
```

该文件位于 `/home/me/projects/myrepo/.hermes.md`，当 Hermes 在 `/home/me/projects/myrepo` 的任何子目录中运行时自动加载，但在 `/home/me/other-project` 中运行时不会加载。

## 安全与隐私开关

常见的"Hermes 为什么对我的输出/工具调用/命令做 X？"开关 —— 以及更改它们的确切命令。其中大多数需要新会话（聊天中 `/reset`，或开启新的 `hermes` 调用），因为它们在启动时只读取一次。

### 工具输出中的密钥脱敏

密钥脱敏**默认开启** —— 工具输出（终端 stdout、`read_file`、网页内容、子 agent 摘要等）在进入对话上下文和日志前会被扫描，查找形如 API key、token 和密钥的字符串。正常使用时请保持开启：

```bash
hermes config set security.redact_secrets true       # 全局保持开启
```

**需要重启。** `security.redact_secrets` 在导入时快照 —— 在会话中途切换（例如通过工具调用 `export HERMES_REDACT_SECRETS=false`）对正在运行的进程**不会**生效。告诉用户从终端修改 config，然后开启新会话。这是有意为之 —— 它防止 LLM 在任务中途自行翻转开关。

仅在你确实需要原始凭据类字符串用于调试或脱敏器开发时禁用：
```bash
hermes config set security.redact_secrets false
```

### 网关消息中的 PII 脱敏

与密钥脱敏分开。启用时，网关在会话上下文到达模型前对用户 ID 做哈希、剥离电话号码：

```bash
hermes config set privacy.redact_pii true    # 启用
hermes config set privacy.redact_pii false   # 禁用（默认）
```

### 命令审批提示

默认情况下（`approvals.mode: manual`），Hermes 在运行被标记为破坏性的 shell 命令（`rm -rf`、`git reset --hard` 等）前会提示用户。模式有：

- `manual` —— 总是提示（默认）
- `smart` —— 用辅助 LLM 自动批准低风险命令，对高风险命令仍提示
- `off` —— 跳过所有审批提示（等同于 `--yolo`）

```bash
hermes config set approvals.mode smart       # 推荐的折中
hermes config set approvals.mode off         # 绕过一切（不推荐）
```

不改配置的按调用绕过：
- `hermes --yolo …`
- `export HERMES_YOLO_MODE=1`

注意：YOLO / `approvals.mode: off` **不会**关闭密钥脱敏。它们是独立的。

### Shell hook 允许列表

某些 shell hook 集成在触发前需要显式加入允许列表。通过 `~/.hermes/shell-hooks-allowlist.json` 管理 —— hook 首次想运行时会交互式提示。

### 禁用 web/browser/image-gen 工具

要让模型完全不接触网络或媒体工具，打开 `hermes tools` 并按平台切换。在下次会话（`/reset`）生效。参见上面的工具与 skill 小节。

---

## 语音与转录

### STT（语音 → 文本）

来自消息平台的语音消息会自动转录。

提供商优先级（自动检测）：
1. **本地 faster-whisper** —— 免费，无需 API key：`pip install faster-whisper`
2. **Groq Whisper** —— 免费层：设置 `GROQ_API_KEY`
3. **OpenAI Whisper** —— 付费：设置 `VOICE_TOOLS_OPENAI_KEY`
4. **Mistral Voxtral** —— 设置 `MISTRAL_API_KEY`

配置：
```yaml
stt:
  enabled: true
  provider: local        # local、groq、openai、mistral
  local:
    model: base          # tiny、base、small、medium、large-v3
```

### TTS（文本 → 语音）

| 提供商 | 环境变量 | 免费？ |
|----------|---------|-------|
| Edge TTS | 无 | 是（默认） |
| ElevenLabs | `ELEVENLABS_API_KEY` | 免费层 |
| OpenAI | `VOICE_TOOLS_OPENAI_KEY` | 付费 |
| MiniMax | `MINIMAX_API_KEY` | 付费 |
| Mistral（Voxtral） | `MISTRAL_API_KEY` | 付费 |
| NeuTTS（本地） | 无（`pip install neutts[all]` + `espeak-ng`） | 免费 |

语音命令：`/voice on`（语音到语音）、`/voice tts`（始终语音）、`/voice off`。

---

## 派生额外的 Hermes 实例

将额外的 Hermes 进程作为完全独立的子进程运行 —— 各自独立的会话、工具和环境。

### 何时使用它 vs delegate_task

| | `delegate_task` | 派生 `hermes` 进程 |
|-|-----------------|--------------------------|
| 隔离 | 独立对话，共享进程 | 完全独立进程 |
| 持续时间 | 分钟级（受父循环约束） | 小时/天级 |
| 工具访问 | 父工具的子集 | 完整工具访问 |
| 交互 | 否 | 是（PTY 模式） |
| 使用场景 | 快速并行子任务 | 长时间自主任务 |

### 单次模式

```
terminal(command="hermes chat -q 'Research GRPO papers and write summary to ~/research/grpo.md'", timeout=300)

# 长任务用后台：
terminal(command="hermes chat -q 'Set up CI/CD for ~/myapp'", background=true)
```

### 交互式 PTY 模式（通过 tmux）

Hermes 使用 prompt_toolkit，需要一个真实终端。用 tmux 进行交互式派生：

```
# 启动
terminal(command="tmux new-session -d -s agent1 -x 120 -y 40 'hermes'", timeout=10)

# 等待启动后发送消息
terminal(command="sleep 8 && tmux send-keys -t agent1 'Build a FastAPI auth service' Enter", timeout=15)

# 读取输出
terminal(command="sleep 20 && tmux capture-pane -t agent1 -p", timeout=5)

# 发送后续
terminal(command="tmux send-keys -t agent1 'Add rate limiting middleware' Enter", timeout=5)

# 退出
terminal(command="tmux send-keys -t agent1 '/exit' Enter && sleep 2 && tmux kill-session -t agent1", timeout=10)
```

### 多 agent 协作

```
# Agent A：后端
terminal(command="tmux new-session -d -s backend -x 120 -y 40 'hermes -w'", timeout=10)
terminal(command="sleep 8 && tmux send-keys -t backend 'Build REST API for user management' Enter", timeout=15)

# Agent B：前端
terminal(command="tmux new-session -d -s frontend -x 120 -y 40 'hermes -w'", timeout=10)
terminal(command="sleep 8 && tmux send-keys -t frontend 'Build React dashboard for user management' Enter", timeout=15)

# 检查进度，在两者间转发上下文
terminal(command="tmux capture-pane -t backend -p | tail -30", timeout=5)
terminal(command="tmux send-keys -t frontend 'Here is the API schema from the backend agent: ...' Enter", timeout=5)
```

### 会话恢复

```
# 恢复最近会话
terminal(command="tmux new-session -d -s resumed 'hermes --continue'", timeout=10)

# 恢复特定会话
terminal(command="tmux new-session -d -s resumed 'hermes --resume 20260225_143052_a1b2c3'", timeout=10)
```

### 提示

- **快速子任务优先用 `delegate_task`** —— 比派生完整进程开销小
- **使用 `-w`（worktree 模式）** 当派生会编辑代码的 agent 时 —— 避免 git 冲突
- **为单次模式设置超时** —— 复杂任务可能需要 5-10 分钟
- **用 `hermes chat -q` 做即发即忘** —— 无需 PTY
- **交互式会话用 tmux** —— 原始 PTY 模式与 prompt_toolkit 有 `\r` vs `\n` 问题
- **对于定时任务**，用 `cronjob` 工具而非派生 —— 它处理投递和重试

---

## 持久化与后台系统

有四个系统与主对话循环并行运行。这里是快速参考；完整开发者笔记位于 `AGENTS.md`，面向用户的文档在 `website/docs/user-guide/features/` 下。

### 委派（`delegate_task`）

同步子 agent 派生 —— 父级在继续自身循环前等待子级的摘要。隔离的上下文 + 终端会话。

- **单个：** `delegate_task(goal, context, toolsets)`。
- **批量：** `delegate_task(tasks=[{goal, ...}, ...])` 并行运行子级，受 `delegation.max_concurrent_children`（默认 3）限制。
- **角色：** `leaf`（默认；不能再委派）vs `orchestrator`（可派生自己的 worker，受 `delegation.max_spawn_depth` 约束）。
- **非持久。** 如果父级被中断，子级会被取消。对于必须超越当前轮次的工作，用 `cronjob` 或 `terminal(background=True, notify_on_complete=True)`。

配置：`config.yaml` 中的 `delegation.*`。

### Cron（定时任务）

持久化调度器 —— `cron/jobs.py` + `cron/scheduler.py`。通过 `cronjob` 工具、`hermes cron` CLI（`list`、`add`、`edit`、`pause`、`resume`、`run`、`remove`）或 `/cron` slash 命令驱动。

- **调度：** 时长（`"30m"`、`"2h"`）、"every" 短语（`"every monday 9am"`）、5 字段 cron（`"0 9 * * *"`）或 ISO 时间戳。
- **每任务旋钮：** `skills`、`model`/`provider` 覆盖、`script`（运行前的数据收集；`no_agent=True` 让脚本成为整个任务）、`context_from`（把任务 A 的输出链入任务 B）、`workdir`（在特定目录运行并加载其 `AGENTS.md` / `CLAUDE.md`）、多平台投递。
- **不变量：** 每次运行 3 分钟硬中断、`.tick.lock` 文件防止跨进程重复 tick、cron 会话默认传 `skip_memory=True`，且 cron 投递用 header/footer 包装而非镜像到目标网关会话（保持角色交替完整）。

用户文档：https://hermes-agent.nousresearch.com/docs/user-guide/features/cron

### Curator（skill 生命周期）

对 agent 创建的 skill 做后台维护。跟踪使用情况、将闲置 skill 标记为陈旧、归档陈旧 skill，并保留运行前的 tar.gz 备份以免丢失。

- **CLI：** `hermes curator <verb>` —— `status`、`run`、`pause`、`resume`、`pin`、`unpin`、`archive`、`restore`、`prune`、`backup`、`rollback`。
- **Slash：** `/curator <subcommand>` 与 CLI 对应。
- **范围：** 只触碰带有 `created_by: "agent"` 来源标记的 skill。捆绑和中心安装的 skill 不受影响。**从不删除** —— 最大破坏性动作是归档。被 pin 的 skill 豁免所有自动转换和所有 LLM 审查轮次。
- **遥测：** sidecar 文件位于 `~/.hermes/skills/.usage.json`，保存每个 skill 的 `use_count`、`view_count`、`patch_count`、`last_activity_at`、`state`、`pinned`。

配置：`curator.*`（`enabled`、`interval_hours`、`min_idle_hours`、`stale_after_days`、`archive_after_days`、`backup.*`）。
用户文档：https://hermes-agent.nousresearch.com/docs/user-guide/features/curator

### Kanban（多 agent 工作队列）

持久化 SQLite 看板，用于多 profile / 多 worker 协作。用户通过 `hermes kanban <verb>` 驱动；dispatcher 派生的 worker 看到一个由 `HERMES_KANBAN_TASK` 限制的精简 `kanban_*` 工具集，orchestrator profile 可选择加入更广的 `kanban` 工具集。普通会话除非配置，否则没有任何 `kanban_*` schema 占用。

- **CLI 动词（常用）：** `init`、`create`、`list`（别名 `ls`）、`show`、`assign`、`link`、`unlink`、`comment`、`complete`、`block`、`unblock`、`archive`、`tail`。较不常用：`watch`、`stats`、`runs`、`log`、`dispatch`、`daemon`、`gc`。
- **Worker/orchestrator 工具集：** `kanban_show`、`kanban_complete`、`kanban_block`、`kanban_heartbeat`、`kanban_comment`、`kanban_create`、`kanban_link`；在 dispatcher 派生任务之外显式启用 `kanban` 工具集的 profile 还获得用于看板路由的 `kanban_list` 和 `kanban_unblock`。
- **Dispatcher** 默认在网关内运行（`kanban.dispatch_in_gateway: true`）—— 回收陈旧认领、推进就绪任务、原子化认领、派生已分配的 profile。连续派生失败 `failure_limit` 次（默认 2；可通过 `kanban.failure_limit` 或每任务 `max_retries` 配置）后自动 block 任务。
- **隔离：** 看板是硬边界（worker 的 `HERMES_KANBAN_BOARD` 在 env 中固定）；租户是看板内用于工作区路径 + 记忆键隔离的软命名空间。

用户文档：https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban

---

## Windows 专属怪癖

Hermes 在 Windows 上原生运行（PowerShell、cmd、Windows Terminal、git-bash mintty、VS Code 集成终端）。大部分功能直接可用，但 Win32 与 POSIX 之间的一些差异曾经坑过我们 —— 遇到新问题就记录在这里，让下一个人（或下一个会话）不必从零重新发现。

### 输入 / 键位绑定

**Alt+Enter 不能插入换行。** Windows Terminal 在终端层拦截 Alt+Enter 以切换全屏 —— 该按键永远不会到达 prompt_toolkit。改用 **Ctrl+Enter**。Windows Terminal 把 Ctrl+Enter 投递为 LF（`c-j`），与普通 Enter（`c-m` / CR）不同，CLI 仅在 `win32` 上把 `c-j` 绑定到插入换行（见 `_bind_prompt_submit_keys` + `cli.py` 中 Windows 专属的 `c-j` 绑定）。副作用：原始 Ctrl+J 按键在 Windows 上也会插入换行 —— 不可避免，因为 Windows Terminal 在 Win32 控制台 API 层把 Ctrl+Enter 和 Ctrl+J 合并为同一键码。Windows 上 Ctrl+J 没有冲突绑定，所以这是无害副作用。

mintty / git-bash 行为相同（Alt+Enter 切全屏），除非你在 Options → Keys 中禁用 Alt+Fn 快捷键。直接用 Ctrl+Enter 更省事。

**诊断键位绑定。** 运行 `python scripts/keystroke_diagnostic.py`（仓库根），查看 prompt_toolkit 在当前终端如何识别每个按键。它能回答诸如"Shift+Enter 是否作为独立键到达？"（几乎不会 —— 大多数终端把它合并为普通 Enter）或"我的终端为 Ctrl+Enter 发送了什么字节序列？"之类的问题。Ctrl+Enter = c-j 这一事实就是这样确立的。

### 配置 / 文件

**首次运行出现 HTTP 400 "No models provided"。** `config.yaml` 被保存为带 UTF-8 BOM（Windows 应用写入时常见）。重新保存为不带 BOM 的 UTF-8。`hermes config edit` 写入时不带 BOM；在记事本中手动编辑是常见元凶。

### `execute_code` / 沙箱

**WinError 10106**（"The requested service provider could not be loaded or initialized"）来自沙箱子进程 —— 它无法创建 `AF_INET` 套接字，因此回环 TCP RPC 回落在 `connect()` 前失败。根本原因通常**不是** Winsock LSP 损坏；而是 Hermes 自身的 env 清洗器从子进程 env 中丢弃了 `SYSTEMROOT` / `WINDIR` / `COMSPEC`。Python 的 `socket` 模块需要 `SYSTEMROOT` 来定位 `mswsock.dll`。通过 `tools/code_execution_tool.py` 中的 `_WINDOWS_ESSENTIAL_ENV_VARS` 允许列表修复。如果仍然遇到，在 `execute_code` 块中 echo `os.environ` 以确认 `SYSTEMROOT` 已设置。完整诊断步骤见 `references/execute-code-sandbox-env-windows.md`。

### 测试 / 贡献

**`scripts/run_tests.sh` 在 Windows 上无法直接使用** —— 它查找 POSIX venv 布局（`.venv/bin/activate`）。Hermes 安装的 venv 位于 `venv/Scripts/`，也没有 pip 或 pytest（为安装体积而精简）。变通方法：将 `pytest + pytest-xdist + pyyaml` 安装到一个系统 Python 3.11 的用户 site，然后用设置好的 `PYTHONPATH` 直接调用 pytest：

```bash
"/c/Program Files/Python311/python" -m pip install --user pytest pytest-xdist pyyaml
export PYTHONPATH="$(pwd)"
"/c/Program Files/Python311/python" -m pytest tests/foo/test_bar.py -v --tb=short -n 0
```

用 `-n 0`，而非 `-n 4` —— `pyproject.toml` 的默认 `addopts` 已包含 `-n`，且包装器的 CI 一致性保证不适用于非 POSIX 环境。

**仅 POSIX 的测试需要 skip 守卫。** 代码库中已有的常见标记：
- 符号链接 —— Windows 上需要提权
- `0o600` 文件模式 —— POSIX 模式位在 NTFS 上默认不强制
- `signal.SIGALRM` —— 仅 Unix（见 `tests/conftest.py::_enforce_test_timeout`）
- Winsock / Windows 专属回归 —— `@pytest.mark.skipif(sys.platform != "win32", ...)`

使用现有的 skip 模式风格（`sys.platform == "win32"` 或 `sys.platform.startswith("win")`）以与套件其余部分保持一致。

### 路径 / 文件系统

**换行符。** Git 可能警告 `LF will be replaced by CRLF the next time Git touches it`。这是表面问题 —— 仓库的 `.gitattributes` 会做规范化。不要让编辑器自动把已提交的 POSIX 换行文件转换为 CRLF。

**正斜杠几乎处处可用。** `C:/Users/...` 被每个 Hermes 工具和大多数 Windows API 接受。代码和日志中优先用正斜杠 —— 避免 bash 中反斜杠的 shell 转义。

---

## 故障排查

### 语音不工作
1. 检查 config.yaml 中 `stt.enabled: true`
2. 验证提供商：`pip install faster-whisper` 或设置 API key
3. 网关中：`/restart`。CLI 中：退出并重新启动。

### 工具不可用
1. `hermes tools` —— 检查工具集是否为你的平台启用
2. 某些工具需要环境变量（检查 `.env`）
3. 启用工具后 `/reset`

### 模型/提供商问题
1. `hermes doctor` —— 检查配置和依赖
2. `hermes auth` —— 重新认证 OAuth 提供商（或 `hermes auth add <provider>`）
3. 检查 `.env` 是否有正确的 API key
4. **Copilot 403**：`gh auth login` 的 token **不能**用于 Copilot API。你必须通过 `hermes model` → GitHub Copilot 使用 Copilot 专属的 OAuth 设备码流程。

### 更改未生效
- **工具/skill：** `/reset` 开启带有更新工具集的新会话
- **配置更改：** 网关中：`/restart`。CLI 中：退出并重新启动。
- **代码更改：** 重启 CLI 或网关进程

### skill 未显示
1. `hermes skills list` —— 验证已安装
2. `hermes skills config` —— 检查平台启用情况
3. 显式加载：`/skill name` 或 `hermes -s name`

### 网关问题
先检查日志：
```bash
grep -i "failed to send\|error" ~/.hermes/logs/gateway.log | tail -20
```

常见网关问题：
- **SSH 注销时网关死亡**：启用 linger：`sudo loginctl enable-linger $USER`
- **WSL2 关闭时网关死亡**：WSL2 需要 `/etc/wsl.conf` 中 `systemd=true` 才能让 systemd 服务工作。否则，网关回退到 `nohup`（会话关闭时死亡）。
- **网关崩溃循环**：重置失败状态：`systemctl --user reset-failed hermes-gateway`

### 平台专属问题
- **Discord bot 静默**：必须在 Bot → Privileged Gateway Intents 中启用 **Message Content Intent**。
- **Slack bot 仅在 DM 中工作**：必须订阅 `message.channels` 事件。否则，bot 忽略公共频道。
- **Windows 专属问题**（`Alt+Enter` 换行、WinError 10106、UTF-8 BOM 配置、测试套件、换行符）：见上面专门的 **Windows 专属怪癖**小节。

### 辅助模型不工作
如果 `auxiliary` 任务（vision、compression、session_search）静默失败，`auto` 提供商找不到后端。要么设置 `OPENROUTER_API_KEY` 或 `GOOGLE_API_KEY`，要么显式配置每个辅助任务的提供商：
```bash
hermes config set auxiliary.vision.provider <your_provider>
hermes config set auxiliary.vision.model <model_name>
```

---

## 在哪里找东西

| 想找... | 位置 |
|----------------|----------|
| 配置选项 | `hermes config edit` 或[配置文档](https://hermes-agent.nousresearch.com/docs/user-guide/configuration) |
| 可用工具 | `hermes tools list` 或[工具参考](https://hermes-agent.nousresearch.com/docs/reference/tools-reference) |
| Slash 命令 | 会话中 `/help` 或[Slash 命令参考](https://hermes-agent.nousresearch.com/docs/reference/slash-commands) |
| skill 目录 | `hermes skills browse` 或[skill 目录](https://hermes-agent.nousresearch.com/docs/reference/skills-catalog) |
| 提供商设置 | `hermes model` 或[提供商指南](https://hermes-agent.nousresearch.com/docs/integrations/providers) |
| 平台设置 | `hermes gateway setup` 或[消息文档](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/) |
| MCP 服务器 | `hermes mcp list` 或[MCP 指南](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp) |
| Profile | `hermes profile list` 或[Profile 文档](https://hermes-agent.nousresearch.com/docs/user-guide/profiles) |
| Cron 任务 | `hermes cron list` 或[Cron 文档](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron) |
| 记忆 | `hermes memory status` 或[记忆文档](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory) |
| 环境变量 | `hermes config env-path` 或[环境变量参考](https://hermes-agent.nousresearch.com/docs/reference/environment-variables) |
| CLI 命令 | `hermes --help` 或[CLI 参考](https://hermes-agent.nousresearch.com/docs/reference/cli-commands) |
| 网关日志 | `~/.hermes/logs/gateway.log` |
| 会话文件 | `hermes sessions browse`（读取 state.db） |
| 源代码 | `~/.hermes/hermes-agent/` |

---

## 贡献者快速参考

面向偶尔贡献者和 PR 作者。完整开发者文档：https://hermes-agent.nousresearch.com/docs/developer-guide/

### 项目布局

```
hermes-agent/
├── run_agent.py          # AIAgent —— 核心对话循环
├── model_tools.py        # 工具发现与分发
├── toolsets.py           # 工具集定义
├── cli.py                # 交互式 CLI（HermesCLI）
├── hermes_state.py       # SQLite 会话存储
├── agent/                # 提示词构建器、上下文压缩、记忆、模型路由、凭据池、skill 分发
├── hermes_cli/           # CLI 子命令、配置、设置、命令
│   ├── commands.py       # Slash 命令注册表（CommandDef）
│   ├── config.py         # DEFAULT_CONFIG、环境变量定义
│   └── main.py           # CLI 入口点和 argparse
├── tools/                # 每个工具一个文件
│   └── registry.py       # 中央工具注册表
├── gateway/              # 消息网关
│   └── platforms/        # 平台适配器（telegram、discord 等）
├── cron/                 # 任务调度器
├── tests/                # 约 3000 个 pytest 测试
└── website/              # Docusaurus 文档站点
```

配置：`~/.hermes/config.yaml`（设置）、`~/.hermes/.env`（API key）—— 两者都在 `$HERMES_HOME`（如已设置）之下。

### 添加工具（3 个文件）

**1. 创建 `tools/your_tool.py`：**
```python
import json, os
from tools.registry import registry

def check_requirements() -> bool:
    return bool(os.getenv("EXAMPLE_API_KEY"))

def example_tool(param: str, task_id: str = None) -> str:
    return json.dumps({"success": True, "data": "..."})

registry.register(
    name="example_tool",
    toolset="example",
    schema={"name": "example_tool", "description": "...", "parameters": {...}},
    handler=lambda args, **kw: example_tool(
        param=args.get("param", ""), task_id=kw.get("task_id")),
    check_fn=check_requirements,
    requires_env=["EXAMPLE_API_KEY"],
)
```

**2. 添加到 `toolsets.py`** → `_HERMES_CORE_TOOLS` 列表。

自动发现：任何带有顶层 `registry.register()` 调用的 `tools/*.py` 文件都会被自动导入 —— 无需手动列单。

所有 handler 必须返回 JSON 字符串。路径用 `get_hermes_home()`，绝不硬编码 `~/.hermes`。

### 添加 Slash 命令

1. 在 `hermes_cli/commands.py` 的 `COMMAND_REGISTRY` 中添加 `CommandDef`
2. 在 `cli.py` 的 `process_command()` 中添加 handler
3. （可选）在 `gateway/run.py` 中添加网关 handler

所有消费者（帮助文本、自动补全、Telegram 菜单、Slack 映射）都自动从中央注册表派生。

### Agent 循环（高层）

```
run_conversation():
  1. 构建系统提示词
  2. 循环，当 iterations < max：
     a. 调用 LLM（OpenAI 格式消息 + 工具 schema）
     b. 若 tool_calls → 通过 handle_function_call() 分发每个 → 追加结果 → 继续
     c. 若文本响应 → 返回
  3. 上下文压缩在接近 token 上限时自动触发
```

### 测试

```bash
python -m pytest tests/ -o 'addopts=' -q   # 完整套件
python -m pytest tests/tools/ -q            # 特定区域
```

- 测试会自动把 `HERMES_HOME` 重定向到临时目录 —— 永远不会触碰真实 `~/.hermes/`
- 推送任何更改前运行完整套件
- 用 `-o 'addopts='` 清除任何内置 pytest 标志

**Windows 贡献者：** `scripts/run_tests.sh` 目前查找 POSIX venv（`.venv/bin/activate` / `venv/bin/activate`），在布局为 `venv/Scripts/activate` + `python.exe` 的 Windows 上会报错。Hermes 安装的 venv 位于 `venv/Scripts/`，也没有 `pip` 或 `pytest` —— 为终端用户安装体积而精简。变通方法：把 pytest + pytest-xdist + pyyaml 安装到一个系统 Python 3.11 用户 site（`/c/Program Files/Python311/python -m pip install --user pytest pytest-xdist pyyaml`），然后直接运行测试：

```bash
export PYTHONPATH="$(pwd)"
"/c/Program Files/Python311/python" -m pytest tests/tools/test_foo.py -v --tb=short -n 0
```

用 `-n 0`（而非 `-n 4`），因为 `pyproject.toml` 的默认 `addopts` 已包含 `-n`，且包装器的 CI 一致性保证不适用于非 POSIX。

**跨平台测试守卫：** 使用仅 POSIX 系统调用的测试需要 skip 标记。代码库中已有的常见标记：
- 符号链接创建 → `@pytest.mark.skipif(sys.platform == "win32", reason="Symlinks require elevated privileges on Windows")`（见 `tests/cron/test_cron_script.py`）
- POSIX 文件模式（0o600 等）→ `@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX mode bits not enforced on Windows")`（见 `tests/hermes_cli/test_auth_toctou_file_modes.py`）
- `signal.SIGALRM` → 仅 Unix（见 `tests/conftest.py::_enforce_test_timeout`）
- 实时 Winsock / Windows 专属回归测试 → `@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific regression")`

**仅 monkeypatch `sys.platform` 不够**，当代测代码也调用 `platform.system()` / `platform.release()` / `platform.mac_ver()` 时。这些函数会独立重新读取真实 OS，因此在 Windows runner 上把 `sys.platform = "linux"` 的测试仍会看到 `platform.system() == "Windows"` 并走 Windows 分支。要一起 patch 三者：

```python
monkeypatch.setattr(sys, "platform", "linux")
monkeypatch.setattr(platform, "system", lambda: "Linux")
monkeypatch.setattr(platform, "release", lambda: "6.8.0-generic")
```

参见 `tests/agent/test_prompt_builder.py::TestEnvironmentHints` 的完整示例。

### 扩展系统提示词的执行环境块

关于宿主 OS、用户 home、cwd、终端后端和 shell（Windows 上 bash vs. PowerShell）的事实性信息，由 `agent/prompt_builder.py::build_environment_hints()` 发出。WSL 提示和每后端探测逻辑也位于此处。约定：

- **本地终端后端** → 发出宿主信息（OS、`$HOME`、cwd）+ Windows 专属说明（hostname ≠ username、`terminal` 用 bash 而非 PowerShell）。
- **远程终端后端**（`_REMOTE_TERMINAL_BACKENDS` 中的任何：`docker, singularity, modal, daytona, ssh, managed_modal`）→ **完全抑制**宿主信息，只描述后端。会通过 `tools.environments.get_environment(...).execute(...)` 在后端内运行实时 `uname`/`whoami`/`pwd` 探测，按进程缓存在 `_BACKEND_PROBE_CACHE`，探测超时有静态回退。
- **提示词编写的关键事实：** 当 `TERMINAL_ENV != "local"` 时，*每个*文件工具（`read_file`、`write_file`、`patch`、`search_files`）都在后端容器内运行，而非宿主上。此时系统提示词绝不能描述宿主 —— agent 接触不到它。

完整设计说明、确切发出的字符串和测试陷阱：
`references/prompt-builder-environment-hints.md`。

**重构安全模式（POSIX 等价守卫）：** 当你把内联逻辑抽取为一个添加 Windows/平台专属行为的 helper 时，在测试文件中保留一个 `_legacy_<name>` oracle 函数（旧代码的逐字副本），然后对它做参数化差异对比。示例：`tests/tools/test_code_execution_windows_env.py::TestPosixEquivalence`。这锁定了 POSIX 行为逐位相同的不变量，并使任何未来漂移以清晰 diff 显式失败。

### 提交约定

```
type: 简明主题行

可选正文。
```

类型：`fix:`、`feat:`、`refactor:`、`docs:`、`chore:`

### 关键规则

- **绝不破坏提示词缓存** —— 不要在对话中途更改上下文、工具或系统提示词
- **消息角色交替** —— 绝不连续两条助手或两条用户消息
- 所有路径用 `hermes_constants` 中的 `get_hermes_home()`（profile 安全）
- 配置值放 `config.yaml`，密钥放 `.env`
- 新工具需要 `check_fn`，以便仅在满足要求时才出现
