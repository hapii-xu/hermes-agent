---
name: llm-wiki
description: "Karpathy 的 LLM Wiki：构建/查询相互链接的 markdown 知识库。"
version: 2.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [wiki, knowledge-base, research, notes, markdown, rag-alternative]
    category: research
    related_skills: [obsidian, arxiv]
---

# Karpathy 的 LLM Wiki

构建并维护一个持久的、可不断累积的知识库，形式是相互链接的 markdown 文件。
基于 [Andrej Karpathy 的 LLM Wiki 模式](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)。

与传统 RAG（每次查询都从零开始重新发现知识）不同，wiki 会一次性编译知识并保持更新。交叉引用已经就位。矛盾之处已被标记。综合分析反映了所有已摄取的内容。

**分工：** 人类负责筛选信息源并指导分析方向。Agent 负责总结、交叉引用、归档以及维护一致性。

## 此技能何时激活

当用户出现以下需求时使用此技能：
- 要求创建、构建或启动一个 wiki 或知识库
- 要求向其 wiki 中摄取、添加或处理某个信息源
- 提出问题，且配置路径下已存在一个 wiki
- 要求对其 wiki 进行 lint、审计或健康检查
- 在研究场景中提到自己的 wiki、知识库或「笔记」

## Wiki 位置

**位置：** 通过 `WIKI_PATH` 环境变量设置（例如在 `${HERMES_HOME:-~/.hermes}/.env` 中）。

如果未设置，默认为 `~/wiki`。

```bash
WIKI="${WIKI_PATH:-$HOME/wiki}"
```

wiki 只是一个由 markdown 文件组成的目录——可以用 Obsidian、VS Code 或任意编辑器打开。无需数据库，也无需任何特殊工具。

## 架构：三层结构

```
wiki/
├── SCHEMA.md           # 约定、结构规则、领域配置
├── index.md            # 分门别类的内容目录，每条一行摘要
├── log.md              # 按时间排序的操作日志（仅追加，每年轮转一次）
├── raw/                # 第 1 层：不可变的原始素材
│   ├── articles/       # 网页文章、剪报
│   ├── papers/         # PDF、arxiv 论文
│   ├── transcripts/    # 会议笔记、访谈
│   └── assets/         # 被信息源引用的图片、图表
├── entities/           # 第 2 层：实体页面（人物、机构、产品、模型）
├── concepts/           # 第 2 层：概念/主题页面
├── comparisons/        # 第 2 层：并排分析
└── queries/            # 第 2 层：值得保留的查询结果
```

**第 1 层 — 原始信息源：** 不可变。Agent 只读取，从不修改这些内容。
**第 2 层 — Wiki 本体：** Agent 拥有的 markdown 文件。由 Agent 创建、更新和交叉引用。
**第 3 层 — Schema：** `SCHEMA.md` 定义结构、约定以及标签分类体系。

## 恢复已有 Wiki（关键步骤——每个会话都要做）

当用户已存在一个 wiki 时，**在做任何事之前先自行定位**：

① **阅读 `SCHEMA.md`** —— 了解领域、约定和标签分类体系。
② **阅读 `index.md`** —— 了解已存在哪些页面及其摘要。
③ **浏览最近的 `log.md`** —— 阅读最近 20-30 条记录，了解近期活动。

```bash
WIKI="${WIKI_PATH:-$HOME/wiki}"
# 会话开始时的定位性阅读
read_file "$WIKI/SCHEMA.md"
read_file "$WIKI/index.md"
read_file "$WIKI/log.md" offset=<最后 30 行>
```

只有在完成定位之后，才应进行摄取、查询或 lint 操作。这能避免：
- 为已存在的实体创建重复页面
- 遗漏指向已有内容的交叉引用
- 与 schema 的约定相冲突
- 重复已记录过的工作

对于较大的 wiki（100+ 页），在创建任何新内容之前，还应针对当前主题快速运行一次 `search_files`。

## 初始化新 Wiki

当用户要求创建或启动一个 wiki 时：

1. 确定 wiki 路径（来自 `$WIKI_PATH` 环境变量，或询问用户；默认 `~/wiki`）
2. 创建上述目录结构
3. 询问用户该 wiki 覆盖什么领域——要具体
4. 编写针对该领域定制的 `SCHEMA.md`（见下方模板）
5. 编写带分节标题的初始 `index.md`
6. 编写包含创建记录的初始 `log.md`
7. 确认 wiki 已就绪，并建议首先摄取的信息源

### SCHEMA.md 模板

根据用户的领域进行调整。schema 约束 agent 的行为并确保一致性：

```markdown
# Wiki Schema

## 领域（Domain）
[本 wiki 覆盖的内容——例如「AI/ML 研究」、「个人健康」、「初创公司情报」]

## 约定（Conventions）
- 文件名：小写、连字符、无空格（例如 `transformer-architecture.md`）
- 每个 wiki 页面以 YAML frontmatter 开头（见下文）
- 使用 `[[wikilink]]` 在页面之间链接（每页至少 2 个出链）
- 更新页面时，务必同步更新 `updated` 日期
- 每个新页面都必须添加到 `index.md` 对应的分节下
- 每个操作都必须追加到 `log.md`
- **来源标记（Provenance markers）：** 在综合了 3 个及以上信息源的页面中，对于那些论断来自特定信息源的段落，在段落末尾追加 `^[raw/articles/source-file.md]`。
  这样读者无需重读整个原始文件就能追溯每条论断。在单一信息源页面（其 `sources:` frontmatter 已足够）上可选。

## Frontmatter
  ```yaml
  ---
  title: Page Title
  created: YYYY-MM-DD
  updated: YYYY-MM-DD
  type: entity | concept | comparison | query | summary
  tags: [来自下方分类体系]
  sources: [raw/articles/source-name.md]
  # 可选的质量信号：
  confidence: high | medium | low        # 论断被支持的程度
  contested: true                        # 当页面存在未解决矛盾时设置
  contradictions: [other-page-slug]      # 与本页面相冲突的页面
  ---
  ```

`confidence` 和 `contested` 是可选的，但对于观点密集或快速变动的主题推荐使用。Lint 会把 `contested: true` 和 `confidence: low` 的页面标记出来供复核，以免薄弱论断悄然固化为被认可的 wiki 事实。

### raw/ Frontmatter

原始信息源也会获得一小段 frontmatter，以便重新摄取时能检测到漂移：

```yaml
---
source_url: https://example.com/article   # 原始 URL（如适用）
ingested: YYYY-MM-DD
sha256: <frontmatter 下方原始内容的十六进制摘要>
---
```

`sha256:` 使得将来对同一 URL 的重新摄取可以在内容未变时跳过处理，并在内容变化时标记漂移。仅对正文（即结尾 `---` 之后的所有内容）计算，不包括 frontmatter 本身。

## 标签分类体系（Tag Taxonomy）
[为本领域定义 10-20 个顶级标签。新标签必须先在此处添加，然后才能使用。]

以 AI/ML 为例：
- 模型：model, architecture, benchmark, training
- 人物/机构：person, company, lab, open-source
- 技术：optimization, fine-tuning, inference, alignment, data
- 元信息：comparison, timeline, controversy, prediction

规则：页面上的每个标签都必须出现在该分类体系中。如果需要新标签，先在此添加，再使用。这能防止标签泛滥。

## 页面阈值（Page Thresholds）
- **创建页面**：当一个实体/概念出现在 2 个及以上信息源中，或是某一信息源的核心主题时
- **添加到已有页面**：当某个信息源提到了已被覆盖的内容时
- **不要创建页面**：针对一笔带过的提及、次要细节，或领域之外的事物
- **拆分页面**：当页面超过约 200 行时——拆成带交叉链接的子主题
- **归档页面**：当其内容已被完全取代时——移动到 `_archive/`，并从 index 中移除

## 实体页面（Entity Pages）
每个值得记录的实体一个页面。包括：
- 概述 / 它是什么
- 关键事实和日期
- 与其他实体的关系（`[[wikilink]]`）
- 信息源引用

## 概念页面（Concept Pages）
每个概念或主题一个页面。包括：
- 定义 / 解释
- 当前知识状态
- 开放问题或争议
- 相关概念（`[[wikilink]]`）

## 对比页面（Comparison Pages）
并排分析。包括：
- 被比较的对象及原因
- 比较的维度（优先使用表格形式）
- 结论或综合判断
- 信息源

## 更新策略（Update Policy）
当新信息与已有内容冲突时：
1. 检查日期——较新的信息源通常会取代较旧的
2. 如果确实矛盾，记录两种观点及其日期和信息源
3. 在 frontmatter 中标记矛盾：`contradictions: [page-name]`
4. 在 lint 报告中标记以供用户复核
```

### index.md 模板

index 按类型分节。每条记录占一行：wikilink + 摘要。

```markdown
# Wiki Index

> 内容目录。每个 wiki 页面都按其类型列出，并附一行摘要。
> 查询时先读这个文件以找到相关页面。
> Last updated: YYYY-MM-DD | Total pages: N

## Entities
<!-- 分节内按字母排序 -->

## Concepts

## Comparisons

## Queries
```

**扩展规则：** 当任一分节超过 50 条时，按首字母或子领域将其拆分为子分节。当 index 总条数超过 200 时，创建一个 `_meta/topic-map.md`，按主题对页面分组以便更快导航。

### log.md 模板

```markdown
# Wiki Log

> 所有 wiki 操作的时间顺序记录。仅追加。
> 格式：`## [YYYY-MM-DD] action | subject`
> 操作类型：ingest, update, query, lint, create, archive, delete
> 当本文件超过 500 条时，轮转：重命名为 log-YYYY.md，重新开始。

## [YYYY-MM-DD] create | Wiki initialized
- Domain: [领域]
- 已创建结构 SCHEMA.md、index.md、log.md
```

## 核心操作

### 1. 摄取（Ingest）

当用户提供一个信息源（URL、文件、粘贴文本）时，将其整合进 wiki：

① **捕获原始信息源：**
   - URL → 使用 `web_extract` 获取 markdown，保存到 `raw/articles/`
   - PDF → 使用 `web_extract`（可处理 PDF），保存到 `raw/papers/`
   - 粘贴文本 → 保存到合适的 `raw/` 子目录
   - 给文件取一个描述性的名字：`raw/articles/karpathy-llm-wiki-2026.md`
   - **添加 raw frontmatter**（`source_url`、`ingested`、正文的 `sha256`）。
     对同一 URL 重新摄取时：重新计算 sha256，与已存储的值比较——相同则跳过，不同则标记漂移并更新。这在每次重新摄取时都做一遍成本很低，并能捕获静默的信息源变更。

② **与用户讨论要点**——哪些内容有趣、对领域意味着什么。（在自动化/cron 场景中跳过这一步——直接继续。）

③ **检查已存在内容**——搜索 index.md 并使用 `search_files` 查找被提及实体/概念的已有页面。这是一个不断生长的 wiki 与一堆重复内容的区别所在。

④ **编写或更新 wiki 页面：**
   - **新实体/概念：** 仅当它们满足 SCHEMA.md 中的页面阈值（2+ 信息源提及，或为某一信息源的核心）时才创建页面
   - **已有页面：** 添加新信息，更新事实，更新 `updated` 日期。
     当新信息与已有内容冲突时，遵循更新策略。
   - **交叉引用：** 每个新建或更新的页面必须通过 `[[wikilink]` 链接到至少 2 个其他页面。检查已有页面是否反向链接。
   - **标签：** 只使用 SCHEMA.md 分类体系中的标签
   - **来源：** 在综合了 3+ 信息源的页面上，对于论断可追溯到特定信息源的段落，追加 `^[raw/articles/source.md]` 标记。
   - **置信度：** 对于观点密集、快速变动或单一信息源的论断，在 frontmatter 中设置 `confidence: medium` 或 `low`。除非论断在多个信息源中得到充分支持，否则不要标为 `high`。

⑤ **更新导航：**
   - 将新页面按字母顺序添加到 `index.md` 对应分节下
   - 更新 index 头部的「Total pages」计数和「Last updated」日期
   - 追加到 `log.md`：`## [YYYY-MM-DD] ingest | Source Title`
   - 在日志条目中列出每个创建或更新的文件

⑥ **报告变更**——向用户列出每个创建或更新的文件。

一个信息源可能会触发对 5-15 个 wiki 页面的更新。这是正常的，也是期望看到的——这就是复利效应。

### 2. 查询（Query）

当用户提出一个关于 wiki 领域的问题时：

① **阅读 `index.md`** 以确定相关页面。
② **对于 100+ 页的 wiki**，还要跨所有 `.md` 文件用 `search_files` 搜索关键词——仅靠 index 可能会遗漏相关内容。
③ **使用 `read_file` 阅读相关页面**。
④ **基于已编译的知识综合出答案**。引用你所依据的 wiki 页面：「基于 [[page-a]] 和 [[page-b]]……」
⑤ **将有价值的答案归档**——如果答案是一次实质性的对比、深度分析或新颖的综合，就在 `queries/` 或 `comparisons/` 中创建一个页面。
   不要归档琐碎的查询——只归档那些重新推导会很痛苦的答案。
⑥ **更新 log.md**，记录查询及是否已归档。

### 3. Lint

当用户要求对 wiki 进行 lint、健康检查或审计时：

① **孤儿页面（Orphan pages）：** 查找没有任何其他页面通过 `[[wikilink]]` 指向它的页面。
```python
# 使用 execute_code 执行此操作——跨所有 wiki 页面进行程序化扫描
import os, re
from collections import defaultdict
wiki = "<WIKI_PATH>"
# 扫描 entities/, concepts/, comparisons/, queries/ 下所有 .md 文件
# 提取所有 [[wikilink]]——构建入链映射
# 入链数为零的页面即为孤儿
```

② **失效的 wikilink：** 查找指向不存在页面的 `[[links]]`。

③ **Index 完整性：** 每个 wiki 页面都应出现在 `index.md` 中。将文件系统与 index 条目进行比对。

④ **Frontmatter 校验：** 每个 wiki 页面都必须包含所有必填字段（title、created、updated、type、tags、sources）。标签必须在分类体系中。

⑤ **过时内容：** 其 `updated` 日期比提及相同实体的最新信息源还要旧 90 天以上的页面。

⑥ **矛盾：** 同一主题下论断相互冲突的页面。寻找共享标签/实体但陈述不同事实的页面。把所有带有 `contested: true` 或 `contradictions:` frontmatter 的页面标记出来供用户复核。

⑦ **质量信号：** 列出 `confidence: low` 的页面，以及任何只引用单一信息源却没有设置 confidence 字段的页面——这些是需要进一步寻找佐证或降级为 `confidence: medium` 的候选。

⑧ **信息源漂移（Source drift）：** 对于 `raw/` 中每个带 `sha256:` frontmatter 的文件，重新计算哈希并标记不匹配。不匹配意味着原始文件被编辑过（不应该发生——raw/ 是不可变的），或来自一个之后发生了变化的 URL。不算硬错误，但值得报告。

⑨ **页面大小：** 标记超过 200 行的页面——适合拆分的候选。

⑩ **标签审计：** 列出所有使用中的标签，标记任何不在 SCHEMA.md 分类体系中的标签。

⑪ **日志轮转：** 如果 log.md 超过 500 条，将其轮转。

⑫ **报告发现**，附上具体文件路径和建议操作，按严重程度分组（失效链接 > 孤儿页面 > 信息源漂移 > 矛盾页面 > 过时内容 > 风格问题）。

⑬ **追加到 log.md：** `## [YYYY-MM-DD] lint | N issues found`

## 使用 Wiki

### 搜索

```bash
# 按内容查找页面
search_files "transformer" path="$WIKI" file_glob="*.md"

# 按文件名查找页面
search_files "*.md" target="files" path="$WIKI"

# 按标签查找页面
search_files "tags:.*alignment" path="$WIKI" file_glob="*.md"

# 最近活动
read_file "$WIKI/log.md" offset=<最后 20 行>
```

### 批量摄取

一次性摄取多个信息源时，分批进行更新：
1. 先读取所有信息源
2. 识别所有信息源中的全部实体和概念
3. 一次性检查所有已有页面（一次搜索，而不是 N 次）
4. 一次性创建/更新页面（避免冗余更新）
5. 在最后一次性更新 index.md
6. 写一条覆盖整批的日志条目

### 归档

当内容被完全取代或领域范围发生变化时：
1. 如果 `_archive/` 目录不存在则创建
2. 将页面以其原始路径移动到 `_archive/`（例如 `_archive/entities/old-page.md`）
3. 从 `index.md` 中移除
4. 更新所有链接到它的页面——把 wikilink 替换为纯文本 +「（已归档）」
5. 记录归档操作

### Obsidian 集成

该 wiki 目录开箱即可作为 Obsidian vault 使用：
- `[[wikilink]]` 渲染为可点击链接
- Graph View 可视化知识网络
- YAML frontmatter 驱动 Dataview 查询
- `raw/assets/` 文件夹存放通过 `![[image.png]]` 引用的图片

为获得最佳效果：
- 将 Obsidian 的附件文件夹设置为 `raw/assets/`
- 在 Obsidian 设置中启用「Wikilinks」（通常默认开启）
- 安装 Dataview 插件以进行诸如 `TABLE tags FROM "entities" WHERE contains(tags, "company")` 的查询

如果同时使用 Obsidian 技能与此技能，请将 `OBSIDIAN_VAULT_PATH` 设置为与 wiki 路径相同的目录。

### Obsidian Headless（服务器和无头机器）

在没有显示器的机器上，请使用 `obsidian-headless` 而不是桌面应用。
它无需 GUI 即可通过 Obsidian Sync 同步 vault——非常适合运行在服务器上的 agent 写入 wiki，同时 Obsidian 桌面端在另一台设备上读取它。

**安装：**
```bash
# 需要 Node.js 22+
npm install -g obsidian-headless

# 登录（需要带 Sync 订阅的 Obsidian 账户）
ob login --email <email> --password '<password>'

# 为 wiki 创建一个远程 vault
ob sync-create-remote --name "LLM Wiki"

# 将 wiki 目录连接到该 vault
cd ~/wiki
ob sync-setup --vault "<vault-id>"

# 初始同步
ob sync

# 持续同步（前台运行——后台运行请使用 systemd）
ob sync --continuous
```

**通过 systemd 实现持续后台同步：**
```ini
# ~/.config/systemd/user/obsidian-wiki-sync.service
[Unit]
Description=Obsidian LLM Wiki Sync
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/path/to/ob sync --continuous
WorkingDirectory=/home/user/wiki
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now obsidian-wiki-sync
# 启用 linger，使同步在登出后依然存活：
sudo loginctl enable-linger $USER
```

这样 agent 就可以在服务器上写入 `~/wiki`，而你可以在笔记本/手机上的 Obsidian 中浏览同一个 vault——变更会在几秒内出现。

## 陷阱（Pitfalls）

- **永远不要修改 `raw/` 中的文件**——信息源是不可变的。修正应放在 wiki 页面里。
- **永远先做定位**——在新会话中进行任何操作之前，阅读 SCHEMA + index + 最近日志。跳过这一步会导致重复和遗漏交叉引用。
- **务必更新 index.md 和 log.md**——跳过这一步会让 wiki 退化。这些是导航骨干。
- **不要为了一笔带过的提及创建页面**——遵循 SCHEMA.md 中的页面阈值。一个在脚注里出现一次的名字不值得为其创建实体页面。
- **不要创建没有交叉引用的页面**——孤立的页面是不可见的。每个页面都必须链接到至少 2 个其他页面。
- **Frontmatter 是必需的**——它使搜索、过滤和过时检测成为可能。
- **标签必须来自分类体系**——自由格式的标签会退化为噪声。新标签先添加到 SCHEMA.md，然后再使用。
- **让页面保持可扫读**——一个 wiki 页面应能在 30 秒内读完。拆分超过 200 行的页面。把详细分析移到专门的深度页面。
- **大批量更新前先询问**——如果一次摄取会触及 10+ 个已有页面，先与用户确认范围。
- **轮转日志**——当 log.md 超过 500 条时，将其重命名为 `log-YYYY.md` 并重新开始。Agent 应在 lint 期间检查日志大小。
- **显式处理矛盾**——不要静默覆盖。记录两种论断及其日期，在 frontmatter 中标记，并标记供用户复核。

## 相关工具

[llm-wiki-compiler](https://github.com/atomicmemory/llm-wiki-compiler) 是一个 Node.js CLI，可将信息源编译成概念 wiki，灵感同样来自 Karpathy。它与 Obsidian 兼容，因此想要一个定时/CLI 驱动编译流水线的用户可以把它指向此技能所维护的同一个 vault。权衡之处：它自己拥有页面生成权（替代 agent 在页面创建上的判断），并且针对小型语料进行了调优。当你希望在循环中保留 agent 策展时使用此技能；当你希望对信息源目录进行批量编译时使用 llmwiki。
