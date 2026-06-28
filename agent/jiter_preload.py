"""尽力提前导入 OpenAI SDK 的原生流式解析器。

OpenAI SDK 在构建流式聊天补全响应时会导入 ``jiter``。
在某些 Windows 安装环境下，原生扩展可以直接从 Hermes venv 导入，
但如果首次导入发生在后续的线程流式请求路径中则会失败。
在 agent 包导入期间提前加载一次，可以避免该导入顺序问题，
同时保留对真正缺失或损坏安装的正常 SDK 报错路径。
"""

from __future__ import annotations

import importlib

_JITER_PRELOADED = False
_JITER_PRELOAD_ERROR: Exception | None = None


def preload_jiter_native_extension() -> bool:
    """如果 jiter 原生扩展可用，则提前导入。"""

    global _JITER_PRELOADED, _JITER_PRELOAD_ERROR

    if _JITER_PRELOADED:
        return True

    try:
        importlib.import_module("jiter.jiter")
        from jiter import from_json as _from_json  # noqa: F401
    except Exception as exc:
        _JITER_PRELOAD_ERROR = exc
        return False

    _JITER_PRELOADED = True
    _JITER_PRELOAD_ERROR = None
    return True


preload_jiter_native_extension()
