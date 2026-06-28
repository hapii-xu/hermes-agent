"""供工具实现共享的路径校验辅助函数。

抽取出此前在 skill_manager_tool、skills_tool、skills_hub、
cronjob_tools 和 credential_files 中重复出现的 ``resolve() + relative_to()``
以及 ``..`` 路径穿越检查模式。
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def validate_within_dir(path: Path, root: Path) -> Optional[str]:
    """确保 *path* 解析后的位置位于 *root* 之内。

    校验失败时返回错误消息字符串，路径安全时返回 ``None``。内部使用
    ``Path.resolve()`` 来跟随符号链接并规范化 ``..`` 组成部分。

    用法::

        error = validate_within_dir(user_path, allowed_root)
        if error:
            return json.dumps({"error": error})
    """
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
        resolved.relative_to(root_resolved)
    except (ValueError, OSError) as exc:
        return f"Path escapes allowed directory: {exc}"
    return None


def has_traversal_component(path_str: str) -> bool:
    """当 *path_str* 含有 ``..`` 路径穿越组成部分时返回 True。

    在执行完整解析之前，对明显的穿越尝试做一次快速检查。
    """
    parts = Path(path_str).parts
    return ".." in parts
