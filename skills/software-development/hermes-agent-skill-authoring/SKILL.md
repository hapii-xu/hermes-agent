---
name: hermes-agent-skill-authoring
description: "编写仓库内 SKILL.md：frontmatter、校验器、结构和写作质量原则。"
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [skills, authoring, hermes-agent, conventions, skill-md]
    related_skills: [plan, requesting-code-review]
---

# 编写 hermes-agent 的 Skill（仓库内）

## 概览

SKILL.md 可以放在两个地方：

1. **用户本地：** `~/.hermes/skills/<可能含分类>/<name>/SKILL.md` —— 私人、不共享。通过 `skill_manage(action='create')` 创建。
2. **仓库内（本 skill 讨论的就是这种情况）：** `/home/bb/hermes-agent/skills/<category>/<name>/SKILL.md` —— 提交进仓库，随包发布。用 `write_file` + `git add`。`skill_manage(action='create')` 不会指向这个目录树。

## 何时使用

- 用户让你把一个 skill 加到「这个分支 / 仓库 / 提交」里
- 你正在提交一个应该随 hermes-agent 一起发布的可复用工作流
- 你在编辑 `/home/bb/hermes-agent/skills/` 下的某个已有 skill（小改动用 `patch`，重写用 `write_file`；`skill_manage` 在仓库内 skill 上仍可做 patch，但不能 `create`）

## 必需的 frontmatter

权威来源：`tools/skill_manager_tool.py::_validate_frontmatter`。硬性要求：

- 以 `---` 作为最开头的字节（没有前导空行）。
- 在正文之前以 `\n---\n` 闭合。
- 能解析为 YAML mapping。
- 存在 `name` 字段。
- 存在 `description` 字段，且 ≤ **1024 字符**（`MAX_DESCRIPTION_LENGTH`）。
- 闭合的 `---` 之后有非空正文。

`skills/software-development/` 下每个 skill 都采用的同类对齐结构：

```yaml
---
name: my-skill-name               # 小写、连字符、≤64 字符 (MAX_NAME_LENGTH)
description: Use when <trigger>. <one-line behavior>.
version: 1.1.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [short, descriptive, tags]
    related_skills: [other-skill, another-skill]
---
```

`version` / `author` / `license` / `metadata` 不会被校验器强制要求，但每个同类 skill 都有 —— 省略会让你的 skill 显得格格不入。

## 大小限制

- description：≤ 1024 字符（强制）。
- 整个 SKILL.md：≤ 100,000 字符（以 `MAX_SKILL_CONTENT_CHARS` 强制，约 36k tokens）。
- `software-development/` 下的同类 skill 大多在 **8-14k 字符**。瞄准这个区间。如果要超过 20k，就拆出 `references/*.md` 并在 SKILL.md 中引用。

## 写作质量原则

一个 skill 的存在，是为了让 agent 的过程更可预测。可预测性**不**等于每次输出都一模一样；它意味着 agent 能可靠地遵循同一套有用的纪律。

在编写或编辑任何 skill 时，请用这些质量检查：

1. **为过程的可预测性而优化。** 问自己：当这个 skill 加载后，什么行为应当改变？如果某一行不能改变行为，就删掉。
2. **选择合适的上下文加载方式。** 一个由模型调用的 Hermes skill，每一轮都要为它的 description 付代价。让 description 聚焦于触发类别和该 skill 独特的行为。细节放进正文或链接的 references 里。
3. **使用信息层次。** 把总是需要的步骤放进 `SKILL.md`；把分支专用或体量较大的参考资料放进 `references/`、`templates/` 或 `scripts/`，并只在需要时指向它。
4. **让步骤以完成标准收尾。** 每个有序步骤都应说明 agent 如何知道它已完成。好的标准是可检查的，并且在重要时是穷尽的：「每个修改过的文件都已 accounted for」胜过「总结改动」。
5. **把规则和它治理的概念放在一起。** 不要把同一个想法散落在文件各处。把定义、注意事项、示例和验证彼此靠近。
6. **使用强有力的引导词。** 优先选用模型已熟知的紧凑概念 —— 例如「tight loop」「tracer bullet」「root cause」「regression test」—— 而不是冗长的重复解释。一个好的引导词既省 token 又锚定行为。
7. **删掉重复和无操作内容。** 让每个含义只有一个权威来源。逐句自问：这句话相对于默认行为，是否改变了 agent 的行为。如果没有，删掉它，而不是打磨它。
8. **警惕过早完成。** 如果 agent 容易在某一步赶进度，先锐化那一步的完成标准。只有当后续步骤会干扰把当前步骤做好时，才拆分序列。

常见的质量失败：

- **过早完成** —— skill 让 agent 在工作真正完成之前就继续往下走。
- **重复** —— 同一条规则出现在多处并发生漂移。
- **沉积** —— 过时的行留着，因为加比删更让人觉得安全。
- **膨胀** —— 总是可见的材料太多；把分支专用的参考推到指针后面。
- **无操作正文** —— 没有 skill 时 agent 本来也会遵循的通用建议。

## 同类对齐结构

每个仓库内 skill 大致遵循：

```
# <标题>

## 概览（Overview）
一两段话：是什么、为什么。

## 何时使用（When to Use）
- 项目符号形式的触发条件
- "Don't use for:" 反向触发

## <针对该 skill 的主题小节>
- 常用速查表
- 带确切命令的代码块
- hermes 专属的配方（通过 scripts/run_tests.sh 跑测试、ui-tui 路径等）

## 常见陷阱（Common Pitfalls）
编号列表：错误及其修复。

## 验证清单（Verification Checklist）
- [ ] 操作后验证项的复选框清单

## 一键配方（One-Shot Recipes，可选）
命名场景 → 具体命令序列。
```

并非每个小节都是强制的，但 `Overview` + `When to Use` + 可执行的正文 + 陷阱，是让这个 skill 看起来像同类成员的最低要求。

## 目录放置

```
skills/<category>/<skill-name>/SKILL.md
```

仓库中现有的分类（用 `ls skills/` 确认）：`autonomous-ai-agents`、`creative`、`data-science`、`devops`、`dogfood`、`email`、`gaming`、`github`、`leisure`、`mcp`、`media`、`mlops/*`、`note-taking`、`productivity`、`red-teaming`、`research`、`smart-home`、`social-media`、`software-development`。

选最贴近的已有分类。不要随意发明新的顶级分类。

## 工作流

1. **考察同类** 在目标分类下：
   ```
   ls skills/<category>/
   ```
   读 2-3 个同类 SKILL.md，匹配语气和结构。
2. **核实校验器约束**，不确定时看 `tools/skill_manager_tool.py`。
3. **起草**，用 `write_file` 写到 `skills/<category>/<name>/SKILL.md`。
4. **本地校验**：
   ```python
   import yaml, re, pathlib
   content = pathlib.Path("skills/<category>/<name>/SKILL.md").read_text()
   assert content.startswith("---")
   m = re.search(r'\n---\s*\n', content[3:])
   fm = yaml.safe_load(content[3:m.start()+3])
   assert "name" in fm and "description" in fm
   assert len(fm["description"]) <= 1024
   assert len(content) <= 100_000
   ```
5. **git add + commit** 到当前活动分支。
6. **注意：** 当前会话的 skill 加载器是缓存的 —— `skill_view` / `skills_list` 在新会话之前看不到新 skill。这是预期行为，不是 bug。

## 交叉引用其他 skill

`metadata.hermes.related_skills` 在加载时合并两棵树（仓库内 `skills/` 和 `~/.hermes/skills/`）。你可以从仓库内 skill 引用一个用户本地 skill，但对于重新 clone 仓库的其他用户它不会解析。从仓库内 skill 出发，最好只引用仓库内的 skill。如果某个被频繁引用的 skill 只存在于 `~/.hermes/skills/`，考虑把它提升进仓库。

## 编辑已有的仓库内 skill

- **小修（错别字、新增陷阱、收紧触发条件）：** `skill_manage(action='patch', name=..., old_string=..., new_string=...)` 在仓库内 skill 上工作正常。
- **大重写：** 用 `write_file` 写整个 SKILL.md。`skill_manage(action='edit')` 也可以，但需要提供完整的新内容。
- **新增辅助文件：** 用 `write_file` 写到 `skills/<category>/<name>/references/<file>.md`、`templates/<file>` 或 `scripts/<file>`。`skill_manage(action='write_file')` 也可以，并会强制 references/templates/scripts/assets 子目录白名单。
- **务必提交**改动 —— 仓库内 skill 是源码，不是运行时状态。

## 常见陷阱

1. **对仓库内 skill 使用 `skill_manage(action='create')`。** 它写到 `~/.hermes/skills/`，而不是仓库目录树。仓库内创建请用 `write_file`。

2. **`---` 前有前导空白。** 校验器检查 `content.startswith("---")`；任何前导空行或 BOM 都会通不过校验。

3. **description 过于泛化。** 同类 skill 的 description 以 "Use when ..." 开头，描述的是*触发类别*，而不是某一个任务。"Use when debugging X" 胜过 "Debug X"。

4. **漏掉 author/license/metadata 块。** 虽不被校验器强制，但每个同类都有；省略会让 skill 显得半成品。

5. **写一个与同类重复的 skill。** 创建前，`ls skills/<category>/` 并打开 2-3 个同类。优先扩展现有 skill，而不是新建一个狭窄的兄弟 skill。

6. **指望当前会话能看到新 skill。** 看不到。skill 加载器在会话开始时初始化。在全新会话中验证，或用确切路径走 `skill_view`。

7. **任由 skill 累积沉积。** 一个 skill 应当随时间变得更短或更锐利。添加一条规则时，删掉它替换掉的旧措辞；不要永远叠加建议。

8. **写无操作正文。** "Be careful"、"be thorough"、"use best practices" 很少改变模型行为。换成可检查的完成标准或更强的引导词。

9. **链接到仓库内并不存在的 skill。** `related_skills: [some-user-local-skill]` 对你有效，但对其他 clone 会失效。最好只用仓库内链接。

## 验证清单

- [ ] 文件位于 `skills/<category>/<name>/SKILL.md`（不在 `~/.hermes/skills/`）
- [ ] frontmatter 从字节 0 开始为 `---`，以 `\n---\n` 闭合
- [ ] `name`、`description`、`version`、`author`、`license`、`metadata.hermes.{tags, related_skills}` 全部存在
- [ ] name ≤ 64 字符，小写 + 连字符
- [ ] description ≤ 1024 字符，并以 "Use when ..." 开头
- [ ] 文件总计 ≤ 100,000 字符（目标 8-15k）
- [ ] 结构：`# Title` → `## Overview` → `## When to Use` → 正文 → `## Common Pitfalls` → `## Verification Checklist`
- [ ] 每个有序步骤都有可检查的完成标准
- [ ] description 以触发条件为核心，避免与正文内容重复
- [ ] 体量较大或分支专用的参考内容渐进式地披露到链接文件中
- [ ] 无操作正文和重复规则已删除
- [ ] `related_skills` 的引用在仓库内可解析（或明确允许是用户本地）
- [ ] 在目标分支上完成了 `git add skills/<category>/<name>/ && git commit`
