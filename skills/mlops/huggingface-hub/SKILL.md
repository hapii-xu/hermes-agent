---
name: huggingface-hub
description: "HuggingFace hf CLI：搜索/下载/上传模型与数据集。"
version: 1.0.0
author: Hugging Face
license: MIT
tags: [huggingface, hf, models, datasets, hub, mlops]
platforms: [linux, macos, windows]
---

# Hugging Face CLI (`hf`) 参考指南

`hf` 命令是与 Hugging Face Hub 交互的现代命令行界面，提供管理仓库、模型、数据集和 Spaces 的工具。

> **重要：** `hf` 命令取代了现已废弃的 `huggingface-cli` 命令。

## 快速开始
*   **安装：** `curl -LsSf https://hf.co/cli/install.sh | bash -s`
*   **帮助：** 使用 `hf --help` 查看所有可用功能与实际示例。
*   **认证：** 推荐通过 `HF_TOKEN` 环境变量或 `--token` 标志完成。

---

## 核心命令

### 通用操作
*   `hf download REPO_ID`：从 Hub 下载文件。
*   `hf upload REPO_ID`：上传文件/文件夹（推荐用于单次提交）。
*   `hf upload-large-folder REPO_ID LOCAL_PATH`：推荐用于大目录的可断点续传上传。
*   `hf sync`：在本地目录与 bucket 之间同步文件。
*   `hf env` / `hf version`：查看环境与版本详情。

### 认证（`hf auth`）
*   `login` / `logout`：使用来自 [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) 的 token 管理会话。
*   `list` / `switch`：管理多个已保存的访问 token 并在它们之间切换。
*   `whoami`：识别当前登录的账号。

### 仓库管理（`hf repos`）
*   `create` / `delete`：创建或永久删除仓库。
*   `duplicate`：将一个模型、数据集或 Space 克隆到一个新 ID。
*   `move`：在命名空间之间转移仓库。
*   `branch` / `tag`：管理类 Git 引用。
*   `delete-files`：使用模式匹配删除特定文件。

---

## 专门的 Hub 交互

### 数据集与模型
*   **数据集：** `hf datasets list`、`info` 和 `parquet`（列出 parquet URL）。
*   **SQL 查询：** `hf datasets sql SQL` —— 通过 DuckDB 对数据集 parquet URL 执行原生 SQL。
*   **模型：** `hf models list` 和 `info`。
*   **论文：** `hf papers list` —— 查看每日论文。

### 讨论与 Pull Request（`hf discussions`）
*   管理 Hub 贡献的生命周期：`list`、`create`、`info`、`comment`、`close`、`reopen` 和 `rename`。
*   `diff`：查看 PR 中的改动。
*   `merge`：完成 pull request 的合并。

### 基础设施与计算
*   **Endpoints：** 部署并管理 Inference Endpoints（`deploy`、`pause`、`resume`、`scale-to-zero`、`catalog`）。
*   **Jobs：** 在 HF 基础设施上运行计算任务。包含用于以行内依赖运行 Python 脚本的 `hf jobs uv`，以及用于资源监控的 `stats`。
*   **Spaces：** 管理交互式应用。包含 `dev-mode` 和 `hot-reload`，可对 Python 文件进行热重载而无需完整重启。

### 存储与自动化
*   **Buckets：** 完整的类 S3 bucket 管理（`create`、`cp`、`mv`、`rm`、`sync`）。
*   **Cache：** 管理本地存储，含 `list`、`prune`（移除游离的修订）和 `verify`（校验和检查）。
*   **Webhooks：** 通过管理 Hub webhooks 实现工作流自动化（`create`、`watch`、`enable`/`disable`）。
*   **Collections：** 将 Hub 条目组织为合集（`add-item`、`update`、`list`）。

---

## 高级用法与技巧

### 全局标志
*   `--format json`：产出可供自动化使用的机器可读输出。
*   `-q` / `--quiet`：将输出限制为仅 ID。

### 扩展与 Skills
*   **Extensions：** 通过 GitHub 仓库扩展 CLI 功能，使用 `hf extensions install REPO_ID`。
*   **Skills：** 使用 `hf skills add` 管理 AI 助手 skills。
