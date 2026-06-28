# 原生 MCP 客户端

Hermes Agent 内置了 MCP 客户端，它在启动时连接 MCP 服务器、发现其工具，并将其作为 agent 可直接调用的一等工具提供。无需 bridge CLI —— 来自 MCP 服务器的工具与 `terminal`、`read_file` 等内置工具并列出现。

## 何时使用

当你想要做以下任何事时，都可以使用：
- 连接 MCP 服务器并在 Hermes Agent 中使用其工具
- 通过 MCP 添加外部能力（文件系统访问、GitHub、数据库、API）
- 运行本地基于 stdio 的 MCP 服务器（npx、uvx 或任何命令）
- 连接远程 HTTP/StreamableHTTP MCP 服务器
- 让 MCP 工具被自动发现并在每次对话中可用

如需从终端做临时的、一次性的 MCP 工具调用而无需任何配置，请改用 `mcporter` skill。

## 前置条件

- **mcp Python 包** —— 可选依赖；用 `pip install mcp` 安装。若未安装，MCP 支持会被静默禁用。
- **Node.js** —— 基于 `npx` 的 MCP 服务器所需（大多数社区服务器）
- **uv** —— 基于 `uvx` 的 MCP 服务器所需（基于 Python 的服务器）

安装 MCP SDK：

```bash
pip install mcp
# 或，若使用 uv：
uv pip install mcp
```

## 快速开始

在 `~/.hermes/config.yaml` 的 `mcp_servers` 键下添加 MCP 服务器：

```yaml
mcp_servers:
  time:
    command: "uvx"
    args: ["mcp-server-time"]
```

重启 Hermes Agent。启动时它会：
1. 连接到服务器
2. 发现可用工具
3. 以 `mcp_time_*` 为前缀注册它们
4. 将其注入所有平台工具集

然后你就可以自然地使用这些工具 —— 直接让 agent 获取当前时间即可。

## 配置参考

`mcp_servers` 下的每个条目都是一个服务器名映射到其配置。有两种传输类型：**stdio**（基于命令）和 **HTTP**（基于 url）。

### Stdio 传输（command + args）

```yaml
mcp_servers:
  server_name:
    command: "npx"             # （必需）要运行的可执行文件
    args: ["-y", "pkg-name"]   # （可选）命令参数，默认：[]
    env:                       # （可选）子进程的环境变量
      SOME_API_KEY: "value"
    timeout: 120               # （可选）每次工具调用超时（秒），默认：120
    connect_timeout: 60        # （可选）初始连接超时（秒），默认：60
```

### HTTP 传输（url）

```yaml
mcp_servers:
  server_name:
    url: "https://my-server.example.com/mcp"   # （必需）服务器 URL
    headers:                                     # （可选）HTTP 头
      Authorization: "Bearer sk-..."
    timeout: 180               # （可选）每次工具调用超时（秒），默认：120
    connect_timeout: 60        # （可选）初始连接超时（秒），默认：60
```

### 所有配置选项

| 选项              | 类型   | 默认值 | 描述                                       |
|-------------------|--------|---------|---------------------------------------------------|
| `command`         | string | --      | 要运行的可执行文件（stdio 传输，必需）     |
| `args`            | list   | `[]`    | 传给命令的参数                   |
| `env`             | dict   | `{}`    | 子进程的额外环境变量    |
| `url`             | string | --      | 服务器 URL（HTTP 传输，必需）             |
| `headers`         | dict   | `{}`    | 每次请求发送的 HTTP 头              |
| `timeout`         | int    | `120`   | 每次工具调用超时（秒）                  |
| `connect_timeout` | int    | `60`    | 初始连接和发现的超时      |

注意：一个服务器配置必须有 `command`（stdio）或 `url`（HTTP）之一，不能两者都有。

## 工作原理

### 启动发现

当 Hermes Agent 启动时，会在工具初始化期间调用 `discover_mcp_tools()`：

1. 从 `~/.hermes/config.yaml` 读取 `mcp_servers`
2. 为每个服务器在专用后台事件循环中派生连接
3. 初始化 MCP 会话并调用 `list_tools()` 发现可用工具
4. 在 Hermes 工具注册表中注册每个工具

### 工具命名约定

MCP 工具按以下命名模式注册：

```
mcp_{server_name}_{tool_name}
```

名称中的连字符和点会被替换为下划线，以兼容 LLM API。

示例：
- 服务器 `filesystem`、工具 `read_file` → `mcp_filesystem_read_file`
- 服务器 `github`、工具 `list-issues` → `mcp_github_list_issues`
- 服务器 `my-api`、工具 `fetch.data` → `mcp_my_api_fetch_data`

### 自动注入

发现后，MCP 工具会被自动注入所有 `hermes-*` 平台工具集（CLI、Discord、Telegram 等）。这意味着 MCP 工具在每次对话中都可用，无需额外配置。

### 连接生命周期

- 每个服务器作为一个长期存活的 asyncio Task 运行在后台守护线程中
- 连接持续 agent 进程的整个生命周期
- 若连接断开，会自动以指数退避重连（最多 5 次重试，最长 60s 退避）
- agent 关闭时，所有连接被优雅关闭

### 幂等性

`discover_mcp_tools()` 是幂等的 —— 多次调用只会连接尚未连接的服务器。失败的服务器会在后续调用中重试。

## 传输类型

### Stdio 传输

最常见的传输方式。Hermes 将 MCP 服务器作为子进程启动，并通过 stdin/stdout 通信。

```yaml
mcp_servers:
  filesystem:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/projects"]
```

子进程继承一个**经过过滤的**环境（见下面的安全小节），加上你在 `env` 中指定的任何变量。

### HTTP / StreamableHTTP 传输

用于远程或共享的 MCP 服务器。要求 `mcp` 包包含 HTTP 客户端支持（`mcp.client.streamable_http`）。

```yaml
mcp_servers:
  remote_api:
    url: "https://mcp.example.com/mcp"
    headers:
      Authorization: "Bearer sk-..."
```

如果你安装的 `mcp` 版本不支持 HTTP，服务器会以 ImportError 失败，其他服务器继续正常运行。

## 安全

### 环境变量过滤

对于 stdio 服务器，Hermes **不会**把你的完整 shell 环境传给 MCP 子进程。只继承安全的基线变量：

- `PATH`、`HOME`、`USER`、`LANG`、`LC_ALL`、`TERM`、`SHELL`、`TMPDIR`
- 任何 `XDG_*` 变量

所有其他环境变量（API key、token、密钥）都会被排除，除非你通过 `env` 配置键显式添加。这防止了向不受信任的 MCP 服务器意外泄漏凭据。

```yaml
mcp_servers:
  github:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      # 只有这个 token 被传给子进程
      GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_..."
```

### 错误消息中的凭据剥离

如果 MCP 工具调用失败，错误消息中任何形如凭据的模式会在展示给 LLM 前被自动脱敏。覆盖范围：

- GitHub PAT（`ghp_...`）
- OpenAI 风格的 key（`sk-...`）
- Bearer token
- 通用的 `token=`、`key=`、`API_KEY=`、`password=`、`secret=` 模式

## 故障排查

### "MCP SDK not available -- skipping MCP tool discovery"

未安装 `mcp` Python 包。安装它：

```bash
pip install mcp
```

### "No MCP servers configured"

`~/.hermes/config.yaml` 中没有 `mcp_servers` 键，或它为空。至少添加一个服务器。

### "Failed to connect to MCP server 'X'"

常见原因：
- **Command not found**：`command` 二进制不在 PATH 上。确保 `npx`、`uvx` 或相关命令已安装。
- **Package not found**：对于 npx 服务器，npm 包可能不存在或需要在 args 中加 `-y` 以自动安装。
- **Timeout**：服务器启动太慢。增大 `connect_timeout`。
- **Port conflict**：对于 HTTP 服务器，URL 可能不可达。

### "MCP server 'X' requires HTTP transport but mcp.client.streamable_http is not available"

你的 `mcp` 包版本不包含 HTTP 客户端支持。升级：

```bash
pip install --upgrade mcp
```

### 工具未出现

- 检查服务器是否列在 `mcp_servers` 下（而非 `mcp` 或 `servers`）
- 确保 YAML 缩进正确
- 查看 Hermes Agent 启动日志中的连接消息
- 工具名以 `mcp_{server}_{tool}` 为前缀 —— 找该模式

### 连接不断断开

客户端最多重试 5 次，指数退避（1s、2s、4s、8s、16s，上限 60s）。如果服务器从根本上不可达，5 次尝试后放弃。检查服务器进程和网络连通性。

## 示例

### 时间服务器（uvx）

```yaml
mcp_servers:
  time:
    command: "uvx"
    args: ["mcp-server-time"]
```

注册如 `mcp_time_get_current_time` 这样的工具。

### 文件系统服务器（npx）

```yaml
mcp_servers:
  filesystem:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/documents"]
    timeout: 30
```

注册如 `mcp_filesystem_read_file`、`mcp_filesystem_write_file`、`mcp_filesystem_list_directory` 这样的工具。

### 带认证的 GitHub 服务器

```yaml
mcp_servers:
  github:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_xxxxxxxxxxxxxxxxxxxx"
    timeout: 60
```

注册如 `mcp_github_list_issues`、`mcp_github_create_pull_request` 等工具。

### 远程 HTTP 服务器

```yaml
mcp_servers:
  company_api:
    url: "https://mcp.mycompany.com/v1/mcp"
    headers:
      Authorization: "Bearer sk-xxxxxxxxxxxxxxxxxxxx"
      X-Team-Id: "engineering"
    timeout: 180
    connect_timeout: 30
```

### 多服务器

```yaml
mcp_servers:
  time:
    command: "uvx"
    args: ["mcp-server-time"]

  filesystem:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]

  github:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_xxxxxxxxxxxxxxxxxxxx"

  company_api:
    url: "https://mcp.internal.company.com/mcp"
    headers:
      Authorization: "Bearer sk-xxxxxxxxxxxxxxxxxxxx"
    timeout: 300
```

来自所有服务器的所有工具都会被注册并同时可用。每个服务器的工具以其名称为前缀以避免冲突。

## Sampling（服务器发起的 LLM 请求）

Hermes 支持 MCP 的 `sampling/createMessage` 能力 —— MCP 服务器可以在工具执行期间通过 agent 请求 LLM 补全。这支持 agent 在环的工作流（数据分析、内容生成、决策制定）。

Sampling **默认启用**。按服务器配置：

```yaml
mcp_servers:
  my_server:
    command: "npx"
    args: ["-y", "my-mcp-server"]
    sampling:
      enabled: true           # 默认：true
      model: "gemini-3-flash" # 模型覆盖（可选）
      max_tokens_cap: 4096    # 每次请求的最大 token 数
      timeout: 30             # LLM 调用超时（秒）
      max_rpm: 10             # 每分钟最大请求数
      allowed_models: []      # 模型白名单（空 = 全部）
      max_tool_rounds: 5      # 工具循环上限（0 = 禁用）
      log_level: "info"       # 审计详细度
```

服务器还可以在 sampling 请求中包含 `tools`，用于多轮工具增强工作流。`max_tool_rounds` 配置防止无限工具循环。每服务器的审计指标（请求数、错误数、token 数、工具使用次数）通过 `get_mcp_status()` 跟踪。

对不受信任的服务器用 `sampling: { enabled: false }` 禁用 sampling。

## 说明

- 从 agent 视角看，MCP 工具是同步调用的，但实际在专用后台事件循环上异步运行
- 工具结果以 JSON 返回，格式为 `{"result": "..."}` 或 `{"error": "..."}`
- 原生 MCP 客户端独立于 `mcporter` —— 你可以同时使用两者
- 服务器连接是持久的，在同一 agent 进程的所有对话间共享
- 添加或移除服务器需要重启 agent（目前无热重载）
