"""
QQBot 平台包。

从 ``adapter.py``（原 ``qqbot.py``）重新导出主要适配器符号，
以便 **所有现有导入路径保持不变**::

    from gateway.platforms.qqbot import QQAdapter          # 可用
    from gateway.platforms.qqbot import check_qq_requirements  # 可用

新模块：
    - ``constants`` — 共享常量（API URL、超时、消息类型）
    - ``utils`` — User-Agent 构建器、配置辅助函数
    - ``crypto`` — AES-256-GCM 密钥生成与解密
    - ``onboard`` — 二维码扫码配置流程
"""

# -- 适配器（原 qqbot.py）------------------------------------------
from .adapter import (  # noqa: F401
    QQAdapter,
    QQCloseError,
    check_qq_requirements,
    _coerce_list,
    _ssrf_redirect_guard,
)

# -- 入驻引导（二维码扫码配置）-----------------------------------
from .onboard import (  # noqa: F401
    BindStatus,
    build_connect_url,
    qr_register,
)
from .crypto import decrypt_secret, generate_bind_key  # noqa: F401

# -- 工具函数 -----------------------------------------------------------------
from .utils import build_user_agent, get_api_headers, coerce_list  # noqa: F401

# -- 分块上传 --------------------------------------------------------
from .chunked_upload import (  # noqa: F401
    ChunkedUploader,
    UploadDailyLimitExceededError,
    UploadFileTooLargeError,
)

# -- 内联键盘 ------------------------------------------------------
from .keyboards import (  # noqa: F401
    ApprovalRequest,
    ApprovalSender,
    InlineKeyboard,
    InteractionEvent,
    build_approval_keyboard,
    build_approval_text,
    build_update_prompt_keyboard,
    parse_approval_button_data,
    parse_interaction_event,
    parse_update_prompt_button_data,
)

__all__ = [
    # 适配器
    "QQAdapter",
    "QQCloseError",
    "check_qq_requirements",
    "_coerce_list",
    "_ssrf_redirect_guard",
    # 入驻引导
    "BindStatus",
    "build_connect_url",
    "qr_register",
    # 加密
    "decrypt_secret",
    "generate_bind_key",
    # 工具函数
    "build_user_agent",
    "get_api_headers",
    "coerce_list",
    # 分块上传
    "ChunkedUploader",
    "UploadDailyLimitExceededError",
    "UploadFileTooLargeError",
    # 内联键盘
    "ApprovalRequest",
    "ApprovalSender",
    "InlineKeyboard",
    "InteractionEvent",
    "build_approval_keyboard",
    "build_approval_text",
    "build_update_prompt_keyboard",
    "parse_approval_button_data",
    "parse_interaction_event",
    "parse_update_prompt_button_data",
]
