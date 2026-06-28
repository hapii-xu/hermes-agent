---
name: obsidian
description: 在 Obsidian vault 中读取、搜索、创建和编辑笔记。
platforms: [linux, macos, windows]
---

# Obsidian Vault

当涉及以文件系统为先的 Obsidian vault 操作时使用此技能：读取笔记、列出笔记、搜索笔记文件、创建笔记、追加内容以及添加 wikilink。

## Vault 路径

在调用文件工具之前，请使用一个已知或已解析的 vault 路径。

文档约定的 vault 路径变量是 `OBSIDIAN_VAULT_PATH` 环境变量，例如来自 `${HERMES_HOME:-~/.hermes}/.env`。如果未设置，则使用 `~/Documents/Obsidian Vault`。

文件工具不会展开 shell 变量。不要把包含 `$OBSIDIAN_VAULT_PATH` 的路径传给 `read_file`、`write_file`、`patch` 或 `search_files`；先解析出 vault 路径，再传入具体的绝对路径。Vault 路径中可能包含空格，这也是优先使用文件工具而非 shell 命令的另一个原因。

如果 vault 路径未知，可以使用 `terminal` 来解析 `OBSIDIAN_VAULT_PATH` 或检查回退路径是否存在。一旦路径确定，就切回文件工具。

## 读取笔记

使用 `read_file` 并传入已解析的笔记绝对路径。优先选择它而不是 `cat`，因为它提供行号和分页。

## 列出笔记

使用 `search_files`，设置 `target: "files"` 并传入已解析的 vault 路径。优先选择它而不是 `find` 或 `ls`。

- 要列出所有 markdown 笔记，在 vault 路径下使用 `pattern: "*.md"`。
- 要列出某个子文件夹，就在该子文件夹的绝对路径下搜索。

## 搜索

使用 `search_files` 同时进行文件名和内容搜索。优先选择它而不是 `grep`、`find` 或 `ls`。

- 对于文件名，使用带 `target: "files"` 和文件名 `pattern` 的 `search_files`。
- 对于笔记内容，使用带 `target: "content"` 的 `search_files`，把内容正则作为 `pattern`，并在希望将匹配限定于 markdown 笔记时设置 `file_glob: "*.md"`。

## 创建笔记

使用 `write_file`，传入已解析的绝对路径和完整的 markdown 内容。优先选择它而不是 shell heredoc 或 `echo`，因为它能避免 shell 引号问题并返回结构化结果。

## 追加内容到笔记

在不显得别扭的情况下，优先使用原生文件工具工作流：

- 用 `read_file` 读取目标笔记。
- 当存在稳定的上下文时（例如在某个已有标题之后添加一节，或在已知的结尾块之前追加），使用 `patch` 进行锚定追加。
- 当重写整个笔记比构造一个易碎的 patch 更清晰时，使用 `write_file`。

使用 `patch` 进行锚定追加时，把锚点替换为「锚点 + 新内容」。

对于没有稳定上下文的简单追加，如果 `terminal` 是最清晰且安全的选项，则可以使用。

## 定向编辑

当当前内容为你提供稳定上下文时，使用 `patch` 进行聚焦的笔记修改。优先选择它而不是 shell 文本重写。

## Wikilink

Obsidian 使用 `[[Note Name]]` 语法来链接笔记。创建笔记时，使用这种语法来关联相关内容。
