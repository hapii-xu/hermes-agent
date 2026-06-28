---
name: nano-pdf
description: "通过 nano-pdf CLI（使用自然语言提示）编辑 PDF 文本/错字/标题。"
version: 1.0.0
author: community
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [PDF, Documents, Editing, NLP, Productivity]
    homepage: https://pypi.org/project/nano-pdf/
---

# nano-pdf

使用自然语言指令编辑 PDF。指向某一页，然后描述需要修改的内容。

## 前置条件

```bash
# 使用 uv 安装（推荐 —— 已内置于 Hermes）
uv pip install nano-pdf

# 或者使用 pip
pip install nano-pdf
```

## 用法

```bash
nano-pdf edit <file.pdf> <page_number> "<instruction>"
```

## 示例

```bash
# 修改第 1 页的标题
nano-pdf edit deck.pdf 1 "Change the title to 'Q3 Results' and fix the typo in the subtitle"

# 更新某一页上的日期
nano-pdf edit report.pdf 3 "Update the date from January to February 2026"

# 修正内容
nano-pdf edit contract.pdf 2 "Change the client name from 'Acme Corp' to 'Acme Industries'"
```

## 注意事项

- 页码可能是从 0 开始计数，也可能从 1 开始，具体取决于版本 —— 如果编辑作用到了错误的页面，请尝试 ±1 后重试
- 编辑后务必核对输出 PDF（使用 `read_file` 检查文件大小，或直接打开它）
- 该工具底层使用了 LLM —— 需要一个 API key（查看 `nano-pdf --help` 了解配置方式）
- 对文本改动效果良好；复杂的版式修改可能需要换一种方式处理
