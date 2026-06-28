"""飞书 Drive 工具 —— 通过飞书/Lark API 进行文档评论操作。

提供列出、回复、新增文档评论的工具。采用与 feishu_comment.py 相同的
懒加载 + BaseRequest 模式。lark 客户端由评论事件处理器按线程注入。
"""

import json
import logging
import threading

from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

# 用于存放由 feishu_comment 处理器注入的 lark 客户端的线程本地存储。
_local = threading.local()


def set_client(client):
    """为当前线程保存一个 lark 客户端（由 feishu_comment 调用）。"""
    _local.client = client


def get_client():
    """返回当前线程的 lark 客户端，没有则返回 None。"""
    return getattr(_local, "client", None)


def _check_feishu():
    # 参见 ``tools/feishu_doc_tool.py::_check_feishu`` —— 用 ``find_spec`` 可以让
    # CLI 启动保持快速（该 SDK 本身完整导入大约需要 5 秒）。
    import importlib.util
    try:
        return importlib.util.find_spec("lark_oapi") is not None
    except (ImportError, ValueError):
        return False


def _do_request(client, method, uri, paths=None, queries=None, body=None):
    """构建并执行一个 BaseRequest，返回 (code, msg, data_dict)。"""
    from lark_oapi import AccessTokenType
    from lark_oapi.core.enum import HttpMethod
    from lark_oapi.core.model.base_request import BaseRequest

    http_method = HttpMethod.GET if method == "GET" else HttpMethod.POST

    builder = (
        BaseRequest.builder()
        .http_method(http_method)
        .uri(uri)
        .token_types({AccessTokenType.TENANT})
    )
    if paths:
        builder = builder.paths(paths)
    if queries:
        builder = builder.queries(queries)
    if body is not None:
        builder = builder.body(body)

    request = builder.build()

    # 工具处理器在工作线程中同步运行（没有正在运行的事件循环），
    # 因此直接调用阻塞式的 lark 客户端。
    response = client.request(request)

    code = getattr(response, "code", None)
    msg = getattr(response, "msg", "")

    # 解析响应数据
    data = {}
    raw = getattr(response, "raw", None)
    if raw and hasattr(raw, "content"):
        try:
            body_json = json.loads(raw.content)
            data = body_json.get("data", {})
        except (json.JSONDecodeError, AttributeError):
            pass
    if not data:
        resp_data = getattr(response, "data", None)
        if isinstance(resp_data, dict):
            data = resp_data
        elif resp_data and hasattr(resp_data, "__dict__"):
            data = vars(resp_data)

    return code, msg, data


# ---------------------------------------------------------------------------
# feishu_drive_list_comments
# ---------------------------------------------------------------------------

_LIST_COMMENTS_URI = "/open-apis/drive/v1/files/:file_token/comments"

FEISHU_DRIVE_LIST_COMMENTS_SCHEMA = {
    "name": "feishu_drive_list_comments",
    "description": (
        "List comments on a Feishu document. "
        "Use is_whole=true to list whole-document comments only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_token": {
                "type": "string",
                "description": "The document file token.",
            },
            "file_type": {
                "type": "string",
                "description": "File type (default: docx).",
                "default": "docx",
            },
            "is_whole": {
                "type": "boolean",
                "description": "If true, only return whole-document comments.",
                "default": False,
            },
            "page_size": {
                "type": "integer",
                "description": "Number of comments per page (max 100).",
                "default": 100,
            },
            "page_token": {
                "type": "string",
                "description": "Pagination token for next page.",
            },
        },
        "required": ["file_token"],
    },
}


def _handle_list_comments(args: dict, **kwargs) -> str:
    client = get_client()
    if client is None:
        return tool_error("Feishu client not available")

    file_token = args.get("file_token", "").strip()
    if not file_token:
        return tool_error("file_token is required")

    file_type = args.get("file_type", "docx") or "docx"
    is_whole = args.get("is_whole", False)
    page_size = args.get("page_size", 100)
    page_token = args.get("page_token", "")

    queries = [
        ("file_type", file_type),
        ("user_id_type", "open_id"),
        ("page_size", str(page_size)),
    ]
    if is_whole:
        queries.append(("is_whole", "true"))
    if page_token:
        queries.append(("page_token", page_token))

    code, msg, data = _do_request(
        client, "GET", _LIST_COMMENTS_URI,
        paths={"file_token": file_token},
        queries=queries,
    )
    if code != 0:
        return tool_error(f"List comments failed: code={code} msg={msg}")

    return tool_result(data)


# ---------------------------------------------------------------------------
# feishu_drive_list_comment_replies
# ---------------------------------------------------------------------------

_LIST_REPLIES_URI = "/open-apis/drive/v1/files/:file_token/comments/:comment_id/replies"

FEISHU_DRIVE_LIST_REPLIES_SCHEMA = {
    "name": "feishu_drive_list_comment_replies",
    "description": "List all replies in a comment thread on a Feishu document.",
    "parameters": {
        "type": "object",
        "properties": {
            "file_token": {
                "type": "string",
                "description": "The document file token.",
            },
            "comment_id": {
                "type": "string",
                "description": "The comment ID to list replies for.",
            },
            "file_type": {
                "type": "string",
                "description": "File type (default: docx).",
                "default": "docx",
            },
            "page_size": {
                "type": "integer",
                "description": "Number of replies per page (max 100).",
                "default": 100,
            },
            "page_token": {
                "type": "string",
                "description": "Pagination token for next page.",
            },
        },
        "required": ["file_token", "comment_id"],
    },
}


def _handle_list_replies(args: dict, **kwargs) -> str:
    client = get_client()
    if client is None:
        return tool_error("Feishu client not available")

    file_token = args.get("file_token", "").strip()
    comment_id = args.get("comment_id", "").strip()
    if not file_token or not comment_id:
        return tool_error("file_token and comment_id are required")

    file_type = args.get("file_type", "docx") or "docx"
    page_size = args.get("page_size", 100)
    page_token = args.get("page_token", "")

    queries = [
        ("file_type", file_type),
        ("user_id_type", "open_id"),
        ("page_size", str(page_size)),
    ]
    if page_token:
        queries.append(("page_token", page_token))

    code, msg, data = _do_request(
        client, "GET", _LIST_REPLIES_URI,
        paths={"file_token": file_token, "comment_id": comment_id},
        queries=queries,
    )
    if code != 0:
        return tool_error(f"List replies failed: code={code} msg={msg}")

    return tool_result(data)


# ---------------------------------------------------------------------------
# feishu_drive_reply_comment
# ---------------------------------------------------------------------------

_REPLY_COMMENT_URI = "/open-apis/drive/v1/files/:file_token/comments/:comment_id/replies"

FEISHU_DRIVE_REPLY_SCHEMA = {
    "name": "feishu_drive_reply_comment",
    "description": (
        "Reply to a local comment thread on a Feishu document. "
        "Use this for local (quoted-text) comments. "
        "For whole-document comments, use feishu_drive_add_comment instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_token": {
                "type": "string",
                "description": "The document file token.",
            },
            "comment_id": {
                "type": "string",
                "description": "The comment ID to reply to.",
            },
            "content": {
                "type": "string",
                "description": "The reply text content (plain text only, no markdown).",
            },
            "file_type": {
                "type": "string",
                "description": "File type (default: docx).",
                "default": "docx",
            },
        },
        "required": ["file_token", "comment_id", "content"],
    },
}


def _handle_reply_comment(args: dict, **kwargs) -> str:
    client = get_client()
    if client is None:
        return tool_error("Feishu client not available")

    file_token = args.get("file_token", "").strip()
    comment_id = args.get("comment_id", "").strip()
    content = args.get("content", "").strip()
    if not file_token or not comment_id or not content:
        return tool_error("file_token, comment_id, and content are required")

    file_type = args.get("file_type", "docx") or "docx"

    body = {
        "content": {
            "elements": [
                {
                    "type": "text_run",
                    "text_run": {"text": content},
                }
            ]
        }
    }

    code, msg, data = _do_request(
        client, "POST", _REPLY_COMMENT_URI,
        paths={"file_token": file_token, "comment_id": comment_id},
        queries=[("file_type", file_type)],
        body=body,
    )
    if code != 0:
        return tool_error(f"Reply comment failed: code={code} msg={msg}")

    return tool_result(success=True, data=data)


# ---------------------------------------------------------------------------
# feishu_drive_add_comment
# ---------------------------------------------------------------------------

_ADD_COMMENT_URI = "/open-apis/drive/v1/files/:file_token/new_comments"

FEISHU_DRIVE_ADD_COMMENT_SCHEMA = {
    "name": "feishu_drive_add_comment",
    "description": (
        "Add a new whole-document comment on a Feishu document. "
        "Use this for whole-document comments or as a fallback when "
        "reply_comment fails with code 1069302."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_token": {
                "type": "string",
                "description": "The document file token.",
            },
            "content": {
                "type": "string",
                "description": "The comment text content (plain text only, no markdown).",
            },
            "file_type": {
                "type": "string",
                "description": "File type (default: docx).",
                "default": "docx",
            },
        },
        "required": ["file_token", "content"],
    },
}


def _handle_add_comment(args: dict, **kwargs) -> str:
    client = get_client()
    if client is None:
        return tool_error("Feishu client not available")

    file_token = args.get("file_token", "").strip()
    content = args.get("content", "").strip()
    if not file_token or not content:
        return tool_error("file_token and content are required")

    file_type = args.get("file_type", "docx") or "docx"

    body = {
        "file_type": file_type,
        "reply_elements": [
            {"type": "text", "text": content},
        ],
    }

    code, msg, data = _do_request(
        client, "POST", _ADD_COMMENT_URI,
        paths={"file_token": file_token},
        body=body,
    )
    if code != 0:
        return tool_error(f"Add comment failed: code={code} msg={msg}")

    return tool_result(success=True, data=data)


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

registry.register(
    name="feishu_drive_list_comments",
    toolset="feishu_drive",
    schema=FEISHU_DRIVE_LIST_COMMENTS_SCHEMA,
    handler=_handle_list_comments,
    check_fn=_check_feishu,
    requires_env=[],
    is_async=False,
    description="List document comments",
    emoji="\U0001f4ac",
)

registry.register(
    name="feishu_drive_list_comment_replies",
    toolset="feishu_drive",
    schema=FEISHU_DRIVE_LIST_REPLIES_SCHEMA,
    handler=_handle_list_replies,
    check_fn=_check_feishu,
    requires_env=[],
    is_async=False,
    description="List comment replies",
    emoji="\U0001f4ac",
)

registry.register(
    name="feishu_drive_reply_comment",
    toolset="feishu_drive",
    schema=FEISHU_DRIVE_REPLY_SCHEMA,
    handler=_handle_reply_comment,
    check_fn=_check_feishu,
    requires_env=[],
    is_async=False,
    description="Reply to a document comment",
    emoji="\u2709\ufe0f",
)

registry.register(
    name="feishu_drive_add_comment",
    toolset="feishu_drive",
    schema=FEISHU_DRIVE_ADD_COMMENT_SCHEMA,
    handler=_handle_add_comment,
    check_fn=_check_feishu,
    requires_env=[],
    is_async=False,
    description="Add a whole-document comment",
    emoji="\u2709\ufe0f",
)
