# 添加新的消息平台

向 Hermes gateway 添加平台有两种方式：

## 插件路径（推荐社区/第三方使用）

在 `~/.hermes/plugins/`（或对于内置插件，在 `plugins/platforms/`
下）创建一个插件目录，其中包含 `plugin.yaml` 和 `adapter.py`。该适配器
继承自 `BasePlatformAdapter`，并通过
`register(ctx)` 入口点中的 `ctx.register_platform()` 进行注册。这
**完全不需要修改核心 Hermes 代码**。

插件系统会自动处理：适配器创建、配置解析、
用户授权、cron 投递、send_message 路由、系统提示词、
状态展示、gateway 设置等。

**覆盖大多数适配器所需边缘场景的可选钩子：**

- `env_enablement_fn: () -> Optional[dict]` —— 在适配器构造
  之前，从环境变量中填充 `PlatformConfig.extra`
  （以及可选的 `home_channel` 字典）。如果没有这个，纯环境变量配置不会
  反映在 `hermes gateway status` 或 `get_connected_platforms()` 中，
  直到 SDK 实例化。
- `apply_yaml_config_fn: (yaml_cfg, platform_cfg) -> Optional[dict]` ——
  将该平台的 `config.yaml` 键转换为环境变量，和/或直接填充
  `PlatformConfig.extra`。让插件拥有自己的 YAML schema，
  而无需每个平台都增加核心 `gateway/config.py` 的样板代码。
  允许修改 `os.environ`（使用 `not os.getenv(...)` 守卫来
  保持 env > YAML 的优先级）；返回的字典会被合并到
  `PlatformConfig.extra`。在 `load_gateway_config()` 期间、
  通用共享键循环之后、`_apply_env_overrides()` 之前被调用。
- `cron_deliver_env_var: str` —— `*_HOME_CHANNEL` 环境变量的名称。当
  设置后，`deliver=<name>` 的 cron 任务会路由到该变量，无需修改
  `cron/scheduler.py` 中硬编码的集合。
- `standalone_sender_fn: async (...) -> dict`：用于独立于 gateway 运行的
  cron 任务的进程外投递。如果没有这个，`deliver=<name>` 任务
  能正确触发，但实际发送会返回
  `No live adapter for platform '<name>'`。与 `cron_deliver_env_var`
  配合使用以获得端到端的 cron 支持。签名见文档站。
- `plugin.yaml` 的 `requires_env` / `optional_env` 富字典条目 ——
  自动填充 `hermes_cli/config.py` 中的 `OPTIONAL_ENV_VARS`，使配置
  向导能展示恰当的描述、提示、密码标记和 URL。

**针对平台特有 UX 的子类化。** 当一个平台有基类适配器无法预见的硬性
时间窗口约束（LINE 的 60 秒一次性回复 token、WhatsApp 的 24 小时会话窗口等），适配器
可以覆盖 `_keep_typing`，在某个阈值处叠加一个飞行中的气泡，
而无需扩展 kwarg 表面。务必
`await super()._keep_typing(...)`，以保持输入心跳持续运行，
并在 `finally` 中清理你的辅助任务。完整模式见 `plugins/platforms/line/`
（45 秒时的 Template Buttons 回传、`RequestCache`
状态机、针对 `/stop` 孤儿任务的 `interrupt_session_activity` 覆盖），
散文式讲解见开发者指南页面。

**共享行为的兄弟适配器。** 当单个平台有两种用户可切换的
传输模式 —— 非官方 vs 官方
API、轮询 vs websocket、库 A vs 库 B —— 正确的
结构是两个适配器共享一个行为 mixin。WhatsApp 就是
这样做的：`gateway/platforms/whatsapp.py`（Baileys 桥接）和
`gateway/platforms/whatsapp_cloud.py`（Meta Cloud API）都继承
`gateway/platforms/whatsapp_common.py` 中的 `WhatsAppBehaviorMixin`。
该 mixin 负责门控、允许列表、提及解析、广播
过滤以及 WhatsApp 风味的 markdown 转换 —— 所有
与平台协议无关的内容。每个适配器各自负责自己的传输。
两者都注册了不同的 `Platform.*` 枚举值，这样 gateway 就能
针对不同的手机号同时运行两者。该 mixin 必须在基类列表中排在
**第一位** —— `class WhatsAppAdapter(Mixin,
BasePlatformAdapter)` —— 这样 mixin 的 `format_message` 才会覆盖
`BasePlatformAdapter` 的通用默认实现。

完整可用示例见 `plugins/platforms/irc/`、`plugins/platforms/teams/` 和
`plugins/platforms/google_chat/`，包含代码示例和钩子文档的完整
插件指南见
`website/docs/developer-guide/adding-platform-adapters.md`。

---

## 内置路径（仅限核心贡献者）

将平台直接集成到 Hermes 核心的清单。
在构建内置适配器时，请将其作为参考 —— 这里的每一项
都是一个真实的集成点。遗漏其中任何一项都会导致功能损坏、
特性缺失或行为不一致。

---

## 1. 核心适配器（`gateway/platforms/<platform>.py`）

该适配器是 `gateway/platforms/base.py` 中 `BasePlatformAdapter` 的子类。

### 必需方法

| 方法 | 用途 |
|--------|---------|
| `__init__(self, config)` | 解析配置、初始化状态。调用 `super().__init__(config, Platform.YOUR_PLATFORM)` |
| `connect() -> bool` | 连接到平台、启动监听器。成功返回 True |
| `disconnect()` | 停止监听器、关闭连接、取消任务 |
| `send(chat_id, text, ...) -> SendResult` | 发送文本消息 |
| `send_typing(chat_id)` | 发送输入指示器 |
| `send_image(chat_id, image_url, caption) -> SendResult` | 发送图片 |
| `get_chat_info(chat_id) -> dict` | 返回某个会话的 `{name, type, chat_id}` |

### 可选方法（基类中有默认桩实现）

| 方法 | 用途 |
|--------|---------|
| `send_document(chat_id, path, caption)` | 发送文件附件 |
| `send_voice(chat_id, path)` | 发送语音消息 |
| `send_video(chat_id, path, caption)` | 发送视频 |
| `send_animation(chat_id, path, caption)` | 发送 GIF/动画 |
| `send_image_file(chat_id, path, caption)` | 从本地文件发送图片 |

### 交互式 UX（如果你的平台支持可点击按钮，则推荐实现）

如果你的平台支持交互式按钮/菜单消息，实现这些方法可以获得更完善的 agent 体验。它们在未覆盖时都能优雅降级为纯文本：

| 方法 | 用途 |
|--------|---------|
| `send_clarify(chat_id, question, choices, clarify_id, session_key, ...)` | 将 `clarify` 工具的多选问题渲染为可点击按钮。需与入站分发配合，把按钮点击路由到 `tools.clarify_gateway.resolve_gateway_clarify`。 |
| `send_exec_approval(chat_id, command, session_key, description, ...)` | 将危险命令审批渲染为"批准/拒绝"按钮。入站分发路由到 `tools.approval.resolve_gateway_approval`。 |
| `send_slash_confirm(chat_id, title, message, session_key, confirm_id, ...)` | 将斜杠命令确认（例如 `/reload-mcp`）渲染为"一次/总是/取消"按钮。入站分发路由到 `tools.slash_confirm.resolve`。 |
| `send_model_picker(...)` | 交互式 `/model` 选择器。由 Telegram 和 Discord 使用。 |

参考实现见 `gateway/platforms/telegram.py`、`discord.py` 和 `whatsapp_cloud.py`。按钮回调 id 的约定（`cl:<id>:<idx>`、`appr:<id>:<choice>`、`sc:<choice>:<id>`）在各适配器间共享 —— 请保持一致，这样 gateway 侧的解析器无需修改即可工作。

### 必需函数

```python
def check_<platform>_requirements() -> bool:
    """检查该平台的依赖是否可用。"""
```

### 需遵循的关键模式

- 使用 `self.build_source(...)` 构造 `SessionSource` 对象
- 调用 `self.handle_message(event)` 将入站消息分发给 gateway
- 使用基类中的 `MessageEvent`、`MessageType`、`SendResult`
- 对附件使用 `cache_image_from_bytes`、`cache_audio_from_bytes`、`cache_document_from_bytes`
- 过滤自发消息（防止回复循环）
- 如果平台有同步/回显消息，则过滤它们
- 在所有日志输出中对敏感标识符（电话号码、token）进行脱敏
- 为流式连接实现带指数退避 + 抖动的重连
- 如果平台有消息大小限制，则设置 `MAX_MESSAGE_LENGTH`

---

## 2. 平台枚举（`gateway/config.py`）

将该平台添加到 `Platform` 枚举中：

```python
class Platform(Enum):
    ...
    YOUR_PLATFORM = "your_platform"
```

在 `_apply_env_overrides()` 中添加环境变量加载：

```python
# Your Platform
your_token = os.getenv("YOUR_PLATFORM_TOKEN")
if your_token:
    if Platform.YOUR_PLATFORM not in config.platforms:
        config.platforms[Platform.YOUR_PLATFORM] = PlatformConfig()
    config.platforms[Platform.YOUR_PLATFORM].enabled = True
    config.platforms[Platform.YOUR_PLATFORM].token = your_token
```

如果你的平台不使用 token/api_key，请更新 `get_connected_platforms()`
（例如，WhatsApp 使用 `enabled` 标志，Signal 使用 `extra` 字典）。

---

## 3. 适配器工厂（`gateway/run.py`）

添加到 `_create_adapter()`：

```python
elif platform == Platform.YOUR_PLATFORM:
    from gateway.platforms.your_platform import YourAdapter, check_your_requirements
    if not check_your_requirements():
        logger.warning("Your Platform: dependencies not met")
        return None
    return YourAdapter(config)
```

---

## 4. 授权映射（`gateway/run.py`）

添加到 `_is_user_authorized()` 中的两个字典：

```python
platform_env_map = {
    ...
    Platform.YOUR_PLATFORM: "YOUR_PLATFORM_ALLOWED_USERS",
}
platform_allow_all_map = {
    ...
    Platform.YOUR_PLATFORM: "YOUR_PLATFORM_ALLOW_ALL_USERS",
}
```

---

## 5. 会话来源（`gateway/session.py`）

如果你的平台需要额外的身份字段（例如，Signal 在电话号码之外还需
UUID），请将它们添加到 `SessionSource` 数据类中，并设为 `Optional` 默认值，
同时更新 base.py 中的 `to_dict()`、`from_dict()` 和 `build_source()`。

---

## 6. 系统提示词（`agent/prompt_builder.py`）

添加一个 `PLATFORM_HINTS` 条目，让 agent 知道自己在哪个平台上：

```python
PLATFORM_HINTS = {
    ...
    "your_platform": (
        "You are on Your Platform. "
        "Describe formatting capabilities, media support, etc."
    ),
}
```

如果没有这个，agent 不会知道自己运行在你的平台上，可能使用
不恰当的格式（例如，在不渲染 markdown 的平台上使用 markdown）。

---

## 7. 工具集（`toolsets.py`）

为你的平台添加一个命名工具集：

```python
"hermes-your-platform": {
    "description": "Your Platform bot toolset",
    "tools": _HERMES_CORE_TOOLS,
    "includes": []
},
```

并将其添加到 `hermes-gateway` 组合中：

```python
"hermes-gateway": {
    "includes": [..., "hermes-your-platform"]
}
```

---

## 8. Cron 投递（`cron/scheduler.py`）

添加到 `_deliver_result()` 中的 `platform_map`：

```python
platform_map = {
    ...
    "your_platform": Platform.YOUR_PLATFORM,
}
```

如果没有这个，`cronjob(action="create", deliver="your_platform", ...)` 会静默失败。

---

## 9. 发送消息工具（`tools/send_message_tool.py`）

添加到 `send_message_tool()` 中的 `platform_map`：

```python
platform_map = {
    ...
    "your_platform": Platform.YOUR_PLATFORM,
}
```

在 `_send_to_platform()` 中添加路由：

```python
elif platform == Platform.YOUR_PLATFORM:
    return await _send_your_platform(pconfig, chat_id, message)
```

实现 `_send_your_platform()` —— 一个独立的 async 函数，用于发送
单条消息，而无需完整适配器（供 cron 任务以及
gateway 进程之外的 send_message 工具使用）。

更新工具 schema 的 `target` 描述，加入你的平台示例。

---

## 10. Cronjob 工具 Schema（`tools/cronjob_tools.py`）

更新 `deliver` 参数的描述和文档字符串，提及你的
平台作为一个投递选项。

---

## 11. 会话目录（`gateway/channel_directory.py`)

如果你的平台无法枚举会话（大多数都不能），请将其添加到
基于会话的发现列表中：

```python
for plat_name in ("telegram", "whatsapp", "signal", "your_platform"):
```

---

## 12. 状态展示（`hermes_cli/status.py`)

添加到 Messaging Platforms 章节的 `platforms` 字典中：

```python
platforms = {
    ...
    "Your Platform": ("YOUR_PLATFORM_TOKEN", "YOUR_PLATFORM_HOME_CHANNEL"),
}
```

---

## 13. Gateway 配置向导（`hermes_cli/gateway.py`)

添加到 `_PLATFORMS` 列表：

```python
{
    "key": "your_platform",
    "label": "Your Platform",
    "emoji": "📱",
    "token_var": "YOUR_PLATFORM_TOKEN",
    "setup_instructions": [...],
    "vars": [...],
}
```

如果你的平台需要自定义配置逻辑（连通性测试、二维码、
策略选择），请添加一个 `_setup_your_platform()` 函数，并在
平台选择 switch 中路由到它。

如果你的平台"已配置"检查与
标准的 `bool(get_env_value(token_var))` 不同，请更新 `_platform_status()`。

---

## 14. 电话/ID 脱敏（`agent/redact.py`)

如果你的平台使用敏感标识符（电话号码等），请向 `agent/redact.py` 添加
正则模式和脱敏函数。这确保标识符在
所有日志输出中被掩码，而不仅仅是你的适配器日志。

---

## 15. 文档

| 文件 | 需更新的内容 |
|------|---------------|
| `README.md` | 特性表中的平台列表 + 文档表 |
| `AGENTS.md` | Gateway 描述 + 环境变量配置章节 |
| `website/docs/user-guide/messaging/<platform>.md` | **新增** —— 完整配置指南（模板见现有平台文档） |
| `website/docs/user-guide/messaging/index.md` | 架构图、工具集表、安全示例、Next Steps 链接 |
| `website/docs/reference/environment-variables.md` | 该平台的所有环境变量 |

---

## 16. 测试（`tests/gateway/test_<platform>.py`)

推荐的测试覆盖范围：

- 平台枚举存在且值正确
- 通过 `_apply_env_overrides` 从环境变量加载配置
- 适配器初始化（配置解析、允许列表处理、默认值）
- 辅助函数（脱敏、解析、文件类型检测）
- 会话来源往返（to_dict → from_dict）
- 授权集成（平台在允许列表映射中）
- 发送消息工具路由（平台在 platform_map 中）

可选但有价值：
- 消息处理流程的 async 测试（mock 平台 API）
- SSE/WebSocket 重连逻辑
- 附件处理
- 群组消息过滤

---

## 快速验证

实现完所有内容后，使用以下命令验证：

```bash
# All tests pass
python -m pytest tests/ -q

# Grep for your platform name to find any missed integration points
grep -r "telegram\|discord\|whatsapp\|slack" gateway/ tools/ agent/ cron/ hermes_cli/ toolsets.py \
  --include="*.py" -l | sort -u
# Check each file in the output — if it mentions other platforms but not yours, you missed it
```
