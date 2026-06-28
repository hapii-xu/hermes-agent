---
name: plan
description: "计划模式：把可执行的 markdown 计划写到 .hermes/plans/，不做执行。任务拆成小块、精确路径、完整代码。"
version: 2.0.0
author: Hermes Agent (writing-craft adapted from obra/superpowers)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [planning, plan-mode, implementation, workflow, design, documentation]
    related_skills: [subagent-driven-development, test-driven-development, requesting-code-review]
---

# 计划模式（Plan Mode）

当用户需要的是计划而非执行时，使用本 skill。

## 核心行为

本回合中，你只负责做计划。

- 不要实现代码。
- 不要编辑项目文件，除了计划 markdown 文件本身。
- 不要运行会改变状态的终端命令、不要 commit、push，或执行外部动作。
- 需要时可以用只读命令/工具检查仓库或其他上下文。
- 你的交付物是一份保存在当前工作区 `.hermes/plans/` 下的 markdown 计划。

## 输出要求

写出具体且可执行的 markdown 计划。

在相关时包含：
- 目标
- 当前上下文 / 假设
- 建议方案
- 分步计划
- 可能改动的文件
- 测试 / 验证
- 风险、权衡与开放问题

如果任务与代码相关，要包含精确的文件路径、可能的测试目标以及验证步骤。

## 保存位置

用 `write_file` 把计划保存到：
- `.hermes/plans/YYYY-MM-DD_HHMMSS-<slug>.md`

把它视为相对于当前工作目录 / 后端工作区的路径。Hermes 文件工具对后端有感知，因此使用这个相对路径可以在 local、docker、ssh、modal、daytona 等后端上把计划与工作区放在一起。

如果运行时提供了具体目标路径，就用那个精确路径。
如果没有，就在 `.hermes/plans/` 下自己创建一个合理的时间戳文件名。

## 交互风格

- 如果请求足够清晰，直接写计划。
- 如果 `/plan` 没有附带明确指令，就从当前对话上下文中推断任务。
- 如果确实不够明确，就问一个简短的澄清问题，而不是猜测。
- 保存计划后，简短回复你计划了什么以及保存路径。

---

# 如何写好计划

本 skill 的其余部分讲的是撰写一份*好的*实现计划的技艺——也就是上面 markdown 文件里要写的内容。

## 概览

编写详尽的实现计划，假设实现者对代码库毫无上下文，且品味堪忧。把他们需要的一切都写下来：要改哪些文件、完整代码、测试命令、要查的文档、如何验证。把任务拆成小块。DRY。YAGNI。TDD。频繁提交。

假设实现者是个熟练的开发者，但对工具链或问题领域几乎一无所知。假设他们不太懂好的测试设计。

**核心原则：** 好的计划让实现变得显而易见。如果实现者还要猜，那计划就不完整。

## 什么时候需要完整实现计划

**在以下情况之前一定要用：**
- 实现多步骤功能
- 拆解复杂需求
- 通过 subagent-driven-development 委派给子代理

**不要在这些时候跳过：**
- 功能看起来简单（假设会导致 bug）
- 你打算自己实现（未来的你也需要指引）
- 独立工作（文档同样重要）

## 小块任务粒度

**每个任务 = 2-5 分钟的专注工作。**

每一步都只是一个动作：
- "写失败的测试" —— 一步
- "运行确认它失败" —— 一步
- "实现让测试通过的最小代码" —— 一步
- "运行测试确认通过" —— 一步
- "提交" —— 一步

**太大了：**
```markdown
### Task 1: Build authentication system
[50 lines of code across 5 files]
```

**大小合适：**
```markdown
### Task 1: Create User model with email field
[10 lines, 1 file]

### Task 2: Add password hash field to User
[8 lines, 1 file]

### Task 3: Create password hashing utility
[15 lines, 1 file]
```

## 计划文档结构

### 头部（必需）

每份计划必须以下面格式开头：

```markdown
# [Feature Name] Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** [One sentence describing what this builds]

**Architecture:** [2-3 sentences about approach]

**Tech Stack:** [Key technologies/libraries]

---
```

### 任务结构

每个任务遵循以下格式：

````markdown
### Task N: [Descriptive Name]

**Objective:** What this task accomplishes (one sentence)

**Files:**
- Create: `exact/path/to/new_file.py`
- Modify: `exact/path/to/existing.py:45-67` (line numbers if known)
- Test: `tests/path/to/test_file.py`

**Step 1: Write failing test**

```python
def test_specific_behavior():
    result = function(input)
    assert result == expected
```

**Step 2: Run test to verify failure**

Run: `pytest tests/path/test.py::test_specific_behavior -v`
Expected: FAIL — "function not defined"

**Step 3: Write minimal implementation**

```python
def function(input):
    return expected
```

**Step 4: Run test to verify pass**

Run: `pytest tests/path/test.py::test_specific_behavior -v`
Expected: PASS

**Step 5: Commit**

```bash
git add tests/path/test.py src/path/file.py
git commit -m "feat: add specific feature"
```
````

## 写作流程

### 第 1 步：理解需求

阅读并理解：
- 功能需求
- 设计文档或用户描述
- 验收标准
- 约束条件

### 第 2 步：探索代码库

用 Hermes 工具了解项目：

```python
# 了解项目结构
search_files("*.py", target="files", path="src/")

# 查看相似功能
search_files("similar_pattern", path="src/", file_glob="*.py")

# 检查已有测试
search_files("*.py", target="files", path="tests/")

# 阅读关键文件
read_file("src/app.py")
```

### 第 3 步：设计方案

决定：
- 架构模式
- 文件组织
- 所需依赖
- 测试策略

### 第 4 步：编写任务

按以下顺序创建任务：
1. 搭建/基础设施
2. 核心功能（每个都用 TDD）
3. 边界情况
4. 集成
5. 清理/文档

### 第 5 步：补充完整细节

对每个任务，包含：
- **精确的文件路径**（不是 "那个配置文件"，而是 `src/config/settings.py`）
- **完整的代码示例**（不是 "加个校验"，而是真实代码）
- **精确的命令**及预期输出
- **验证步骤**，能证明任务完成

### 第 6 步：审查计划

检查：
- [ ] 任务顺序连贯且合乎逻辑
- [ ] 每个任务都是小块（2-5 分钟）
- [ ] 文件路径精确
- [ ] 代码示例完整（可直接复制粘贴）
- [ ] 命令精确且有预期输出
- [ ] 没有遗漏上下文
- [ ] 应用了 DRY、YAGNI、TDD 原则

## 原则

### DRY（不要重复自己）

**坏：** 在 3 处复制粘贴校验逻辑
**好：** 抽出校验函数，到处复用

### YAGNI（你不会需要它）

**坏：** 为未来需求添加 "灵活性"
**好：** 只实现当下需要的

```python
# 坏 —— 违反 YAGNI
class User:
    def __init__(self, name, email):
        self.name = name
        self.email = email
        self.preferences = {}  # 现在还用不上！
        self.metadata = {}     # 现在还用不上！

# 好 —— YAGNI
class User:
    def __init__(self, name, email):
        self.name = name
        self.email = email
```

### TDD（测试驱动开发）

每个产出代码的任务都应包含完整 TDD 循环：
1. 写失败的测试
2. 运行确认失败
3. 写最小代码
4. 运行确认通过

详见 `test-driven-development` skill。

### 频繁提交

每个任务后都提交：
```bash
git add [files]
git commit -m "type: description"
```

## 常见错误

### 任务模糊

**坏：** "加个认证"
**好：** "创建带 email 和 password_hash 字段的 User 模型"

### 代码不完整

**坏：** "Step 1: 加个校验函数"
**好：** "Step 1: 加个校验函数"，后面紧跟完整的函数代码

### 缺少验证

**坏：** "Step 3: 测试一下能用"
**好：** "Step 3: 运行 `pytest tests/test_auth.py -v`，预期：3 passed"

### 缺少文件路径

**坏：** "创建模型文件"
**好：** "创建：`src/models/user.py`"

## 执行交接

保存计划后，提供执行方式：

**"计划已完成并保存。可以用 subagent-driven-development 来执行——我会为每个任务派发一个全新子代理，并做两阶段审查（先规范符合性，再代码质量）。要我继续吗？"**

执行时使用 `subagent-driven-development` skill：
- 每个任务一次全新的 `delegate_task`，附带完整上下文
- 每个任务后做规范符合性审查
- 规范通过后做代码质量审查
- 两项审查都通过后才继续

## 切记

```
小块任务（每个 2-5 分钟）
精确的文件路径
完整代码（可直接复制粘贴）
精确的命令与预期输出
验证步骤
DRY、YAGNI、TDD
频繁提交
```

**好的计划让实现变得显而易见。**
