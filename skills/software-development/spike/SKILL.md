---
name: spike
description: "在正式构建前用一次性实验验证某个想法。"
version: 1.0.0
author: Hermes Agent (adapted from gsd-build/get-shit-done)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [spike, prototype, experiment, feasibility, throwaway, exploration, research, planning, mvp, proof-of-concept]
    related_skills: [sketch, subagent-driven-development, plan]
---

# Spike（探针实验）

当用户想在正式投入构建之前**先摸清一个想法**——验证可行性、对比方案，或暴露出再多研究也回答不了的未知——时，使用本 skill。Spike 天生是一次性的。一旦还清了它的"信息债"就扔掉。

当用户说类似 "let me try this"、"I want to see if X works"、"spike this out"、"before I commit to Y"、"quick prototype of Z"、"is this even possible?"、"compare A vs B" 时加载本 skill。

## 什么时候不要用

- 答案能从文档或读代码中得到——那就做研究，不要构建
- 工作是生产路径——改用 `plan` skill
- 想法已经验证过——直接进入实现

## 如果用户安装了完整的 GSD 系统

如果 `gsd-spike` 作为兄弟 skill 出现（通过 `npx get-shit-done-cc --hermes` 安装），当用户想要完整 GSD 工作流时优先使用 **`gsd-spike`**：持久化的 `.planning/spikes/` 状态、跨会话的 MANIFEST 追踪、Given/When/Then 裁决格式，以及与 GSD 其余部分集成的提交模式。本 skill 是为没有（或不想要）完整系统的用户准备的轻量独立版本。

## 核心方法

不论规模大小，每个 spike 都遵循这个循环：

```
decompose  →  research  →  build  →  verdict
   ↑__________________________________________↓
                  iterate on findings
```

### 1. Decompose（拆解）

把用户的想法拆成 **2-5 个独立的可行性问题**。每个问题就是一个 spike。用 Given/When/Then 框架以表格形式呈现：

| # | Spike | 验证内容（Given/When/Then） | 风险 |
|---|-------|----------------------------|------|
| 001 | websocket-streaming | 给定一条 WS 连接，当 LLM 流式输出 token 时，客户端在 100ms 内收到分块 | 高 |
| 002a | pdf-parse-pdfjs | 给定一份多页 PDF，用 pdfjs 解析时，能提取出结构化文本 | 中 |
| 002b | pdf-parse-camelot | 给定一份多页 PDF，用 camelot 解析时，能提取出结构化文本 | 中 |

**Spike 类型：**
- **standard（标准）** —— 一种方案回答一个问题
- **comparison（对比）** —— 同一个问题，不同方案（共享编号，加字母后缀 `a`/`b`/`c`）

**好的 spike 问题：** 具体的可行性，且有可观察的输出。
**坏的 spike 问题：** 太宽泛、无可观察输出，或只是 "读一下 X 的文档"。

**按风险排序。** 最可能扼杀想法的 spike 先跑。如果难的部分根本行不通，做简单的部分也没意义。

**跳过拆解**——仅当用户已经明确知道要 spike 什么并说出口时。此时把他们的想法当作单个 spike。

### 2. Align（对齐，针对多 spike 的想法）

展示 spike 表格。问："按这个顺序全部做，还是要调整？" 在你写任何代码之前，让用户删减、重排或重新定义。

### 3. Research（研究，每个 spike 在构建前都要做）

Spike 并非不做研究——你要研究到足以选出正确方案，然后再构建。每个 spike：

1. **简述。** 2-3 句话：这个 spike 是什么、为什么重要、关键风险。
2. **列出竞争方案**，如果有真正的选择余地：

   | 方案 | 工具/库 | 优点 | 缺点 | 状态 |
   |----------|-------------|------|------|--------|
   | ... | ... | ... | ... | 维护中 / 已弃置 / beta |

3. **选定一个。** 说明理由。如果有 2 个以上都可信，就在 spike 内部快速做几个变体。
4. **跳过研究**，如果是纯逻辑且无外部依赖。

用 Hermes 工具做研究这一步：

- `web_search("python websocket streaming libraries 2025")` —— 找候选
- `web_extract(urls=["https://websockets.readthedocs.io/..."])` —— 读真实文档（返回 markdown）
- `terminal("pip show websockets | grep Version")` —— 检查项目 venv 里装了什么

对于没有文档页的库，通过 `read_file` 克隆并阅读它们的 `README.md` / `examples/`。Context7 MCP（如果用户配置了）也是好来源——先 `mcp_*_resolve-library-id` 再 `mcp_*_query-docs`。

### 4. Build（构建）

每个 spike 一个目录。保持独立。

```
spikes/
├── 001-websocket-streaming/
│   ├── README.md
│   └── main.py
├── 002a-pdf-parse-pdfjs/
│   ├── README.md
│   └── parse.js
└── 002b-pdf-parse-camelot/
    ├── README.md
    └── parse.py
```

**偏向于做出用户能交互的东西。** 当 spike 的唯一输出只是一行写着 "it works" 的日志时，spike 就失败了。用户想要*感受*到 spike 起作用。默认选择，按偏好排序：

1. 一个能接收输入并打印可观察输出的可运行 CLI
2. 一个展示行为的极简 HTML 页面
3. 一个带单一端点的小型 web 服务器
4. 一个用可识别断言检验问题的单元测试

**深度优先于速度。** 永远不要在一次顺利路径跑通后就宣布 "it works"。测试边界情况。追查意外的发现。只有当调查是诚实的，裁决才可信。

**避免**，除非 spike 明确需要：复杂的包管理、构建工具/打包器、Docker、env 文件、配置系统。把一切硬编码——这只是个 spike。

**构建单个 spike** —— 典型的工具序列：

```
terminal("mkdir -p spikes/001-websocket-streaming")
write_file("spikes/001-websocket-streaming/README.md", "# 001: websocket-streaming\n\n...")
write_file("spikes/001-websocket-streaming/main.py", "...")
terminal("cd spikes/001-websocket-streaming && python3 main.py")
# 观察输出，迭代。
```

**并行对比 spike（002a / 002b）—— 委派。** 当两种方案可以并行跑且都需要真正的工程量（不是 10 行原型）时，用 `delegate_task` 展开：

```
delegate_task(tasks=[
    {"goal": "Build 002a-pdf-parse-pdfjs: ...", "toolsets": ["terminal", "file", "web"]},
    {"goal": "Build 002b-pdf-parse-camelot: ...", "toolsets": ["terminal", "file", "web"]},
])
```

每个子代理返回自己的裁决；由你来写正面 PK。

### 5. Verdict（裁决）

每个 spike 的 `README.md` 以如下内容收尾：

```markdown
## Verdict: VALIDATED | PARTIAL | INVALIDATED

### What worked
- ...

### What didn't
- ...

### Surprises
- ...

### Recommendation for the real build
- ...
```

**VALIDATED** = 核心问题以证据回答了"是"。
**PARTIAL** = 在 X、Y、Z 约束下可行——把它们记下来。
**INVALIDATED** = 行不通，原因是这个。这也是一次成功的 spike。

## 对比 spike

当两种方案回答同一个问题（002a / 002b）时，**背靠背**地构建它们，最后做一次正面 PK：

```markdown
## Head-to-head: pdfjs vs camelot

| 维度 | pdfjs (002a) | camelot (002b) |
|-----------|--------------|----------------|
| 提取质量 | 9/10 结构化 | 7/10 仅表格 |
| 搭建复杂度 | npm install，1 行 | pip + ghostscript |
| 100 页 PDF 性能 | 3s | 18s |
| 处理旋转文字 | 否 | 是 |

**胜者：** 对我们的用例是 pdfjs。如果以后需要表格优先的提取，再用 Camelot。
```

## Frontier 模式（选择下一个 spike 目标）

如果 spike 已经存在，而用户问 "我接下来该 spike 什么？"，遍历现有目录，寻找：

- **集成风险** —— 两个已验证的 spike 触碰同一资源，但当时是独立测试的
- **数据交接** —— spike A 的输出被假设与 spike B 的输入兼容；从未被证明
- **愿景中的空缺** —— 被假设但未被证明的能力
- **替代方案** —— 针对 PARTIAL 或 INVALIDATED spike 的不同角度

以 Given/When/Then 形式提出 2-4 个候选。让用户挑选。

## 输出

- 在仓库根目录创建 `spikes/`（如果用户使用 GSD 约定则是 `.planning/spikes/`）
- 每个 spike 一个目录：`NNN-descriptive-name/`
- 每个 spike 的 `README.md` 记录问题、方案、结果、裁决
- 让代码保持一次性——一个要花 2 天 "为生产清理" 的 spike，本身就是一个坏 spike

## 出处

改编自 GSD（Get Shit Done）项目的 `/gsd-spike` 工作流——MIT © 2025 Lex Christopherson（[gsd-build/get-shit-done](https://github.com/gsd-build/get-shit-done)）。完整的 GSD 系统提供持久化的 spike 状态、MANIFEST 追踪，以及与更广泛的规范驱动开发流水线的集成；用 `npx get-shit-done-cc --hermes --global` 安装。
