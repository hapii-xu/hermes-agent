"""对工具的 JSON schema 做净化，以兼容更广泛的 LLM 后端。

部分本地推理后端（尤其是 llama.cpp 用于构建 GBNF 工具调用解析器的
``json-schema-to-grammar`` 转换器）对接受的 JSON Schema 形状要求非常严格。
OpenAI / Anthropic / 多数云服务商会默默接受的 schema，可能让 llama.cpp
直接让整个请求失败：

    HTTP 400: Unable to generate parser for this template.
    Automatic parser generation failed: JSON schema conversion failed:
    Unrecognized schema: "object"

我们在实际中见过的失败模式：

* ``{"type": "object"}`` 但没有 ``properties`` —— 被当作语法生成器无法约束的
  节点而拒绝。
* schema 值是裸字符串 ``"object"`` 而不是 dict（MCP 服务端输出畸形，例如
  ``additionalProperties: "object"``）。
* ``"type": ["string", "null"]`` 数组形式类型 —— 很多转换器只接受单字符串形式
  的 ``type``。
* ``anyOf`` / ``oneOf`` 联合类型，其唯一作用是为可选字段允许 ``null``（常见的
  Pydantic/MCP 形状）。Anthropic 在 ``input_schema`` 顶部就会拒绝这类结构；
  需要把它们折叠为非 null 分支。
* 在 properties 为空的对象上使用不受约束的 ``additionalProperties``。
* ``default``（以及其他注解关键字）与 ``$ref`` 并存 —— 严格后端（Fireworks 托管的
  Kimi、JSON Schema draft-07 校验器）会拒绝与 ``$ref`` 同级的兄弟关键字。这是
  可空联合折叠后常见的 MCP/Pydantic 形状::

      {"$ref": "#/$defs/Foo", "default": null}

本模块会遍历最终的工具 schema 树（在 MCP 层归一化以及任何按工具的动态重建之后），
并在一份深拷贝上就地修复这些已知的有害结构。它被刻意设计得很保守：只修改 LLM
后端反正也无法使用的那些形状。
"""

from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)


def sanitize_tool_schemas(tools: list[dict]) -> list[dict]:
    """返回 ``tools`` 的副本，其中每个工具的参数 schema 都已净化。

    输入是 OpenAI 格式的工具列表：
    ``[{"type": "function", "function": {"name": ..., "parameters": {...}}}]``

    返回的列表是深拷贝 —— 调用方可以安全地修改它，而不会影响原始的注册表条目。
    """
    if not tools:
        return tools

    sanitized: list[dict] = []
    for tool in tools:
        sanitized.append(_sanitize_single_tool(tool))
    return sanitized


def _sanitize_single_tool(tool: dict) -> dict:
    """深拷贝并净化单个 OpenAI 格式的工具条目。"""
    out = copy.deepcopy(tool)
    fn = out.get("function") if isinstance(out, dict) else None
    if not isinstance(fn, dict):
        return out

    params = fn.get("parameters")
    # 缺失或非 dict 的 parameters → 替换为最小合法形状。
    if not isinstance(params, dict):
        fn["parameters"] = {"type": "object", "properties": {}}
        return out

    fn["parameters"] = _sanitize_node(params, path=fn.get("name", "<tool>"))
    # 递归之后，确保顶层是带 properties 的 object。
    top = fn["parameters"]
    if not isinstance(top, dict):
        fn["parameters"] = {"type": "object", "properties": {}}
    else:
        if top.get("type") != "object":
            top["type"] = "object"
        if "properties" not in top or not isinstance(top.get("properties"), dict):
            top["properties"] = {}
    # 最后再扫一遍：折叠上面递归净化器原样保留的可空 anyOf/oneOf 联合类型
    # （它只处理数组形式的 ``type: [X, "null"]``）。保留 ``nullable: true``
    # 这个提示，以便运行时参数强制转换（``model_tools._schema_allows_null``）
    # 仍能把模型生成的 ``"null"`` 字符串映射为 Python 的 ``None``。
    fn["parameters"] = strip_nullable_unions(fn["parameters"], keep_nullable_hint=True)
    # 剥离严格后端（OpenAI 位于 chatgpt.com/backend-api/codex 的 Codex 端点）
    # 会直接拒绝掉的顶层组合关键字。properties 内部嵌套的组合关键字予以保留。
    fn["parameters"] = _strip_top_level_combinators(
        fn["parameters"], path=fn.get("name", "<tool>")
    )
    fn["parameters"] = _strip_ref_siblings(fn["parameters"])
    return out


# 严格的 JSON Schema 校验器会拒绝与 ``$ref`` 并列出现的兄弟关键字。
_REF_FORBIDDEN_SIBLINGS = frozenset({"default"})


def _strip_ref_siblings(node: Any) -> Any:
    """从带有 ``$ref`` 的节点上去掉被禁止的兄弟关键字。

    Fireworks（以及其他 draft-07 严格后端）会让工具请求失败，报错为::

        JSON Schema not supported: keyword(s) ['default'] not allowed at
        the same level as $ref.

    可空联合折叠和 MCP 摄入可能会在 ``$ref`` 节点上遗留 ``default``；这里递归地
    把它剥离掉。
    """
    if isinstance(node, list):
        return [_strip_ref_siblings(item) for item in node]
    if not isinstance(node, dict):
        return node

    out = {key: _strip_ref_siblings(value) for key, value in node.items()}
    if "$ref" in out:
        for key in _REF_FORBIDDEN_SIBLINGS:
            if key in out:
                out.pop(key, None)
    return out


_TOP_LEVEL_FORBIDDEN_KEYS = ("allOf", "anyOf", "oneOf", "enum", "not")


def _strip_top_level_combinators(params: dict, *, path: str = "<tool>") -> dict:
    """从函数 parameters schema 的顶层去掉组合关键字。

    OpenAI 的 Codex 后端（``chatgpt.com/backend-api/codex``）比公开的 Functions
    API 更严格，会以下面的报错拒绝请求::

        Invalid schema for function 'X': schema must have type 'object' and
        not have 'oneOf'/'anyOf'/'allOf'/'enum'/'not' at the top level.

    这些关键字通常用于条件性的必填字段提示
    （``allOf: [{if: ..., then: {required: [...]}]}``）。在顶层移除它们只是丢弃了
    这个提示，但不会改变哪些参数 *值* 是合法的 —— 工具处理器总是会重新校验必填字段。

    只有 *顶层* 会被剥离；嵌套在某个 property 的 schema 内部的组合关键字予以保留
    （这条严格规则只适用于最外层的 parameters 对象）。
    """
    if not isinstance(params, dict):
        return params
    out = dict(params)
    for key in _TOP_LEVEL_FORBIDDEN_KEYS:
        if key in out:
            logger.debug(
                "schema_sanitizer[%s]: stripped top-level %r combinator "
                "from tool parameters (strict-backend compat)",
                path, key,
            )
            out.pop(key, None)
    return out


def strip_nullable_unions(
    schema: Any,
    *,
    keep_nullable_hint: bool = True,
) -> Any:
    """把 ``anyOf`` / ``oneOf`` 可空联合类型折叠为非 null 分支。

    MCP / Pydantic 的可选字段通常长这样::

        {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null}

    Anthropic 的工具 input-schema 校验器会拒绝 null 分支。工具的可选性已经由
    父对象的 ``required`` 数组表达，因此我们把联合类型折叠为单个非 null 变体。

    外层联合节点上的元数据（``title``、``description``、``default``、``examples``）
    会被搬到替换后的变体上。

    参数：
        schema: JSON-Schema 片段（dict、list 或标量）。
        keep_nullable_hint: 若为 True，则在替换项上设置 ``nullable: true``，
            以便为关心此事的下游消费者保留「该字段可能为 None」这一信号
            （例如把字面字符串 ``"null"`` 映射为 Python ``None`` 的运行时参数
            强制转换）。Anthropic 的校验器接受 ``nullable: true``，但严格的生产方
            可能更倾向于 False。

    返回：
        折叠了可空联合类型后的 schema。非联合节点原样返回。
    """
    if isinstance(schema, list):
        return [strip_nullable_unions(item, keep_nullable_hint=keep_nullable_hint) for item in schema]
    if not isinstance(schema, dict):
        return schema

    stripped = {
        k: strip_nullable_unions(v, keep_nullable_hint=keep_nullable_hint)
        for k, v in schema.items()
    }
    for key in ("anyOf", "oneOf"):
        variants = stripped.get(key)
        if not isinstance(variants, list):
            continue
        non_null = [
            item for item in variants
            if not (isinstance(item, dict) and item.get("type") == "null")
        ]
        # 只有当我们确实丢弃了 null 分支、并且恰好剩下一个非 null 分支时才折叠
        # （否则这个联合是有意义的，我们就不动它）。
        if len(non_null) == 1 and len(non_null) != len(variants):
            replacement = dict(non_null[0]) if isinstance(non_null[0], dict) else {}
            if keep_nullable_hint:
                replacement.setdefault("nullable", True)
            for meta_key in ("title", "description", "default", "examples"):
                if meta_key in stripped and meta_key not in replacement:
                    # ``default`` 在严格后端上与 ``$ref`` 并列是非法的。
                    if meta_key == "default" and "$ref" in replacement:
                        continue
                    replacement[meta_key] = stripped[meta_key]
            return strip_nullable_unions(replacement, keep_nullable_hint=keep_nullable_hint)
    return stripped


def _sanitize_node(node: Any, path: str) -> Any:
    """递归净化一个 JSON-Schema 片段。

    - 把裸字符串形式的 schema 值（"object"、"string"……）替换为
      ``{"type": <value>}``，以便下游消费者看到的是 dict。
    - 为缺失 ``properties`` 的 object 类型节点注入 ``properties: {}``。
    - 把 ``type: [X, "null"]`` 数组形式归一化为单个 ``type: X``（保留
      ``nullable: true`` 作为提示）。
    - 递归进入 ``properties``、``items``、``additionalProperties``、
      ``anyOf``、``oneOf``、``allOf``，以及 ``$defs`` / ``definitions``。
    """
    # 畸形情况：schema 位置上放的是一个裸字符串，比如 "object"。
    if isinstance(node, str):
        if node in {"object", "string", "number", "integer", "boolean", "array", "null"}:
            logger.debug(
                "schema_sanitizer[%s]: replacing bare-string schema %r "
                "with {'type': %r}",
                path, node, node,
            )
            return {"type": node} if node != "object" else {
                "type": "object",
                "properties": {},
            }
        # 任何其他散落的字符串都不是 schema —— 用一个宽松的 object schema 替换
        # 它，而不是传播后端会拒绝的东西。
        logger.debug(
            "schema_sanitizer[%s]: replacing non-schema string %r "
            "with empty object schema", path, node,
        )
        return {"type": "object", "properties": {}}

    if isinstance(node, list):
        return [_sanitize_node(item, f"{path}[{i}]") for i, item in enumerate(node)]

    if not isinstance(node, dict):
        return node

    out: dict = {}
    for key, value in node.items():
        # type: [X, "null"] → type: X（后端的工具调用解析器只接受单字符串类型；
        # 可空性会丢失，但调用仍能成功，而且模型仍可以自行传入 null。）
        if key == "type" and isinstance(value, list):
            non_null = [t for t in value if t != "null"]
            if len(non_null) == 1 and isinstance(non_null[0], str):
                out["type"] = non_null[0]
                if "null" in value:
                    out.setdefault("nullable", True)
                continue
            # 兜底：选取第一个字符串类型，丢弃其余的。
            first_str = next((t for t in value if isinstance(t, str) and t != "null"), None)
            if first_str:
                out["type"] = first_str
                continue
            # 全为 null 或空列表 → 当作 object 处理。
            out["type"] = "object"
            continue

        if key in {"properties", "$defs", "definitions"} and isinstance(value, dict):
            out[key] = {
                sub_k: _sanitize_node(sub_v, f"{path}.{key}.{sub_k}")
                for sub_k, sub_v in value.items()
            }
        elif key in {"items", "additionalProperties"}:
            if isinstance(value, bool):
                # 保留布尔值形式的 ``additionalProperties`` 不变 —— 它是合法形式
                # 且被广泛接受。``items: true/false`` 虽然非标准，但我们也保留
                # 而不是丢弃。
                out[key] = value
            else:
                out[key] = _sanitize_node(value, f"{path}.{key}")
        elif key in {"anyOf", "oneOf", "allOf"} and isinstance(value, list):
            out[key] = [
                _sanitize_node(item, f"{path}.{key}[{i}]")
                for i, item in enumerate(value)
            ]
        elif key in {"required", "enum", "examples"}:
            # schema 的「兄弟」关键字，其值本身并不是 schema：
            #  - ``required``：属性名字符串组成的列表
            #  - ``enum``：字面值组成的列表（任意 JSON 类型）
            #  - ``examples``：示例值组成的列表（任意 JSON 类型）
            # 用 _sanitize_node() 递归进这些值，会把 "path" 这样的字面字符串
            # 误判为裸字符串 schema，并替换成 {"type": "object"} dict。这里原样透传。
            out[key] = copy.deepcopy(value) if isinstance(value, (list, dict)) else value
        else:
            out[key] = _sanitize_node(value, f"{path}.{key}") if isinstance(value, (dict, list)) else value

    # 没有 properties 的 object 节点：注入空的 properties dict。
    # llama.cpp 的语法生成器无法约束一个自由形式的 object。
    if out.get("type") == "object" and not isinstance(out.get("properties"), dict):
        out["properties"] = {}

    # 修剪 ``required`` 中不存在于 properties 的条目（针对畸形 MCP schema 的防御；
    # 对 MCP 工具也会在上游被捕获，但内置工具或插件工具可能没走过那条路径）。
    if out.get("type") == "object" and isinstance(out.get("required"), list):
        props = out.get("properties") or {}
        valid = [r for r in out["required"] if isinstance(r, str) and r in props]
        if not valid:
            out.pop("required", None)
        elif len(valid) != len(out["required"]):
            out["required"] = valid

    return out


# =============================================================================
# 响应式剥离 —— 仅在 llama.cpp 拒绝某个 schema 时才调用
# =============================================================================

_STRIP_ON_RECOVERY_KEYS = frozenset({"pattern", "format"})


def strip_pattern_and_format(tools: list[dict]) -> tuple[list[dict], int]:
    """从工具 schema 中剥离 ``pattern`` 和 ``format`` 这两个 JSON Schema 关键字。

    这是一个 *响应式* 净化器，仅当 llama.cpp 的 ``json-schema-to-grammar``
    转换器以 HTTP 400 语法解析错误拒绝了某个工具 schema 时才调用。llama.cpp 的
    正则引擎只支持 ECMAScript 正则的一小部分（字面量、``.``、``[...]``、``|``、
    ``*``、``+``、``?``、``{n,m}``）—— 它会拒绝 ``\\d``、``\\w``、``\\s`` 这类
    转义类以及绝大多数 ``format`` 值。云服务商（OpenAI、Anthropic、OpenRouter、
    Gemini）都能正常接受这些关键字，并把它们当作提示用的提示信息，因此我们在默认
    schema 中保留它们，仅按需剥离。

    剥离操作作用于 ``type`` 的兄弟位置（所以被移除的是 schema 关键字）—— 一个字面
    名字就叫 ``pattern`` 的属性（例如内置 ``search_files`` 工具的第一个参数）不会
    受影响，因为属性名位于 ``properties`` dict 内部，而不是 ``type`` 的兄弟。

    参数：
        tools: OpenAI 格式的工具列表，为效率起见会就地修改。
            需要保留原始数据的调用方应先做深拷贝。

    返回：
        ``(tools, stripped_count)`` —— 同一个列表引用，外加一个计数，表示所有工具
        一共移除了多少个 ``pattern``/``format`` 关键字。
    """
    if not tools:
        return tools, 0

    stripped = 0

    def _walk(node: Any) -> None:
        nonlocal stripped
        if isinstance(node, dict):
            # 只在作为 ``type`` 的兄弟时才剥离 —— 即当这个节点本身就是 schema 时。
            # 这样可以避免剥离字面上叫 "pattern" 的属性键（search_files.pattern
            # 等），因为它们位于 ``properties`` dict 内部，而不是 ``type`` 的兄弟。
            is_schema_node = "type" in node or "anyOf" in node or "oneOf" in node or "allOf" in node
            for key in list(node.keys()):
                if is_schema_node and key in _STRIP_ON_RECOVERY_KEYS:
                    node.pop(key, None)
                    stripped += 1
                    continue
                _walk(node[key])
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    for tool in tools:
        if not isinstance(tool, dict):
            continue

        # OpenAI 格式：{"function": {"parameters": {...}}}
        fn = tool.get("function")
        if isinstance(fn, dict):
            params = fn.get("parameters")
            if isinstance(params, dict):
                _walk(params)
                continue

        # Responses 格式：{"name": "...", "parameters": {...}}
        # （由 codex_responses API 模式使用 —— xAI、OpenAI Codex 等）
        params = tool.get("parameters")
        if isinstance(params, dict):
            _walk(params)
            continue

    if stripped:
        logger.info(
            "schema_sanitizer: stripped %d pattern/format keyword(s) from "
            "tool schemas (llama.cpp grammar-parse recovery)",
            stripped,
        )
    return tools, stripped


def strip_slash_enum(tools: list[dict]) -> tuple[list[dict], int]:
    """剥离其字符串值包含正斜杠的 ``enum`` 关键字。

    xAI 的 ``/v1/responses`` 和 ``/v1/chat/completions`` 端点会把工具 schema
    编译成一种语法，该语法会拒绝包含 ``/`` 的 ``enum`` 值（请求会以 HTTP 400
    "Invalid arguments passed to the model" 失败，此时还没有生成任何 token）。
    最常见于由 MCP 派生、enum 列出 HuggingFace 模型 ID
    （``Qwen/Qwen3.5-0.8B``、``openai/gpt-oss-20b``）或 owner/name 环境 ID 的
    工具。这个约束纯粹是提示用的提示信息；丢弃它后，模型仍能看到字段描述并选取一个
    值，而不会让 xAI 在斜杠上绊倒。

    参数：
        tools: OpenAI 格式或 Responses 格式的工具列表，就地修改。
            需要保留原始数据的调用方应先做深拷贝。

    返回：
        ``(tools, stripped_count)`` —— 同一个列表引用，外加一个计数，表示移除了
        多少个 ``enum`` 关键字。
    """
    if not tools:
        return tools, 0

    stripped = 0

    def _walk(node: Any) -> None:
        nonlocal stripped
        if isinstance(node, dict):
            enum_val = node.get("enum")
            if isinstance(enum_val, list) and any(
                isinstance(v, str) and "/" in v for v in enum_val
            ):
                node.pop("enum", None)
                stripped += 1
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if isinstance(fn, dict):
            params = fn.get("parameters")
            if isinstance(params, dict):
                _walk(params)
                continue
        params = tool.get("parameters")
        if isinstance(params, dict):
            _walk(params)

    if stripped:
        logger.info(
            "schema_sanitizer: stripped %d enum keyword(s) containing '/' "
            "from tool schemas (xAI Responses grammar-compile recovery)",
            stripped,
        )
    return tools, stripped
