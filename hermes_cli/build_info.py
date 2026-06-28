"""
Hermes Agent 的内置构建元数据。

源码安装通过 ``git rev-parse`` 实时报告 git 修订版本（参见
``hermes_cli/dump.py`` 和 ``hermes_cli/banner.py``）。但这在发布的
Docker 镜像中无法工作，因为 ``.dockerignore`` 排除了 ``.git``，
所以这些调用点会回退到 ``"(unknown)"`` / 完全丢弃 banner 后缀。

为了让 ``hermes dump`` 和启动 banner 能标识镜像构建时的确切 commit，
Docker 构建会将构建时的 ``$HERMES_GIT_SHA`` 参数写入
``<project_root>/.hermes_build_sha``。本模块是唯一的读取端辅助函数，
供两个调用点共同使用 —— 将查找逻辑集中在一处，确保文件路径和
文件缺失行为保持一致。

行为：

- 当文件不存在时返回 ``None``。源码安装和未使用 ``HERMES_GIT_SHA``
  构建参数构建的开发镜像会在调用方回退到实时 git 解析，
  因此非 Docker 安装不受影响。
- 在任何 IO / 解码错误时返回 ``None``。构建 SHA 仅用于支持分类，
  CLI 中的任何功能都不允许因此崩溃。
- 截断为 ``short`` 个字符（默认 8），与整个代码库中
  ``git rev-parse --short=8`` 使用的格式一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# 路径相对于此模块解析，这样无论 cwd 是什么都能正常工作 ——
# 与 ``banner._resolve_repo_dir`` 使用的模式一致。
_BUILD_SHA_FILE = Path(__file__).parent.parent / ".hermes_build_sha"


def get_build_sha(short: int = 8) -> Optional[str]:
    """返回内置的构建 SHA，截断为 ``short`` 个字符，如果不可用则返回 None。

    读取 ``<project_root>/.hermes_build_sha``（如果存在）。该文件
    由 Dockerfile 的 ``HERMES_GIT_SHA`` 构建参数写入，
    包含单行的完整 40 字符 commit 哈希。
    """
    try:
        if not _BUILD_SHA_FILE.is_file():
            return None
        sha = _BUILD_SHA_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    if not sha:
        return None
    return sha[:short] if short and short > 0 else sha
