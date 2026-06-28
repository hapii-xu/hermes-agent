---
name: codebase-inspection
description: "使用 pygount 检视代码库：LOC、语言、占比。"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [LOC, Code Analysis, pygount, Codebase, Metrics, Repository]
    related_skills: [github-repo-management]
prerequisites:
  commands: [pygount]
---

# 使用 pygount 检视代码库

使用 `pygount` 分析仓库的代码行数、语言构成、文件数量以及代码与注释的占比。

## 适用场景

- 用户要求统计 LOC（代码行数）
- 用户想了解某个仓库的语言构成
- 用户询问代码库的规模或组成
- 用户想了解代码与注释的占比
- 一般性的“这个仓库有多大”类问题

## 前置条件

```bash
pip install --break-system-packages pygount 2>/dev/null || pip install pygount
```

## 1. 基础摘要（最常用）

获取完整的语言构成、文件数量、代码行数和注释行数：

```bash
cd /path/to/repo
pygount --format=summary \
  --folders-to-skip=".git,node_modules,venv,.venv,__pycache__,.cache,dist,build,.next,.tox,.eggs,*.egg-info" \
  .
```

**重要：** 务必使用 `--folders-to-skip` 排除依赖/构建目录，否则 pygount 会遍历它们，可能会耗时极长甚至卡死。

## 2. 常见的文件夹排除项

根据项目类型进行调整：

```bash
# Python 项目
--folders-to-skip=".git,venv,.venv,__pycache__,.cache,dist,build,.tox,.eggs,.mypy_cache"

# JavaScript/TypeScript 项目
--folders-to-skip=".git,node_modules,dist,build,.next,.cache,.turbo,coverage"

# 通用兜底
--folders-to-skip=".git,node_modules,venv,.venv,__pycache__,.cache,dist,build,.next,.tox,vendor,third_party"
```

## 3. 按指定语言筛选

```bash
# 仅统计 Python 文件
pygount --suffix=py --format=summary .

# 仅统计 Python 和 YAML
pygount --suffix=py,yaml,yml --format=summary .
```

## 4. 逐文件的详细输出

```bash
# 默认格式展示每个文件的明细
pygount --folders-to-skip=".git,node_modules,venv" .

# 按代码行数排序（通过 sort 管道）
pygount --folders-to-skip=".git,node_modules,venv" . | sort -t$'\t' -k1 -nr | head -20
```

## 5. 输出格式

```bash
# 摘要表格（默认推荐）
pygount --format=summary .

# 用于程序化处理的 JSON 输出
pygount --format=json .

# 适合管道处理：语言、文件数、代码、文档、空行、字符串
pygount --format=summary . 2>/dev/null
```

## 6. 解读结果

摘要表格的各列含义：
- **Language** — 检测到的编程语言
- **Files** — 该语言的文件数量
- **Code** — 实际代码行数（可执行/声明式）
- **Comment** — 注释或文档行数
- **%** — 占总数的百分比

特殊的伪语言：
- `__empty__` — 空文件
- `__binary__` — 二进制文件（图片、编译产物等）
- `__generated__` — 自动生成的文件（通过启发式方法检测）
- `__duplicate__` — 内容完全相同的文件
- `__unknown__` — 无法识别的文件类型

## 常见陷阱

1. **务必排除 .git、node_modules、venv** — 如果不使用 `--folders-to-skip`，pygount 会遍历所有内容，在大型依赖树中可能耗时数分钟甚至卡死。
2. **Markdown 显示 0 行代码** — pygount 会把所有 Markdown 内容归类为注释而非代码。这是预期行为。
3. **JSON 文件代码行数偏低** — pygount 对 JSON 行数的统计可能偏保守。如需精确的 JSON 行数，请直接使用 `wc -l`。
4. **大型 monorepo** — 对于非常大的仓库，可考虑使用 `--suffix` 针对特定语言，而不是扫描全部内容。
