---
name: apple-notes
description: "通过 memo CLI 管理 Apple Notes：创建、搜索、编辑。"
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [Notes, Apple, macOS, note-taking]
    related_skills: [obsidian]
prerequisites:
  commands: [memo]
---

# Apple Notes

使用 `memo` 直接在终端中管理 Apple Notes。笔记会通过 iCloud 在所有 Apple 设备间同步。

## 前置条件

- 安装了 Notes.app 的 **macOS**
- 安装：`brew tap antoniorodr/memo && brew install antoniorodr/memo/memo`
- 出现提示时授予对 Notes.app 的自动化访问权限（系统设置 → 隐私 → 自动化）

## 适用场景

- 用户要求创建、查看或搜索 Apple Notes
- 将信息保存到 Notes.app 以便跨设备访问
- 将笔记整理到文件夹中
- 将笔记导出为 Markdown/HTML

## 不适用场景

- Obsidian 知识库管理 → 使用 `obsidian` skill
- Bear Notes → 独立应用（此处不支持）
- 仅用于 agent 内部的快速笔记 → 改用 `memory` 工具

## 快速参考

### 查看笔记

```bash
memo notes                        # 列出所有笔记
memo notes -f "Folder Name"       # 按文件夹筛选
memo notes -s "query"             # 搜索笔记（模糊匹配）
```

### 创建笔记

```bash
memo notes -a                     # 交互式编辑器
memo notes -a "Note Title"        # 以标题快速添加
```

### 编辑笔记

```bash
memo notes -e                     # 交互式选择要编辑的笔记
```

### 删除笔记

```bash
memo notes -d                     # 交互式选择要删除的笔记
```

### 移动笔记

```bash
memo notes -m                     # 将笔记移动到文件夹（交互式）
```

### 导出笔记

```bash
memo notes -ex                    # 导出为 HTML/Markdown
```

## 限制

- 无法编辑包含图片或附件的笔记
- 交互式提示需要终端访问权限（如有需要可设置 pty=true）
- 仅支持 macOS — 需要 Apple Notes.app

## 规则

1. 当用户希望跨设备同步（iPhone/iPad/Mac）时，优先使用 Apple Notes
2. 对于不需要同步的 agent 内部笔记，使用 `memory` 工具
3. 对于 Markdown 原生的知识管理，使用 `obsidian` skill
