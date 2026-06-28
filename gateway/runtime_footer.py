"""gateway 运行时元数据页脚（footer）。

渲染一个紧凑的页脚，展示运行时状态（model、context %、cwd），并在启用时
追加到 agent 某个 turn 的 FINAL（最终）消息上。默认关闭，以保持回复简洁。

配置（``~/.hermes/config.yaml``）::

    display:
      runtime_footer:
        enabled: true                       # 默认关闭
        fields: [model, context_pct, cwd]   # 展示顺序；去掉任意一项即可隐藏

按平台覆盖配置位于 ``display.platforms.<platform>.runtime_footer``。
用户可以从 CLI 和任意 gateway 平台使用 ``/footer on|off`` 来切换全局设置。

该页脚会在 ``gateway/run.py`` 中、即将把响应返回给 adapter 的发送路径之前，
追加到最终响应文本的末尾——因此它只会出现在用户看到的最终消息上，而不会
出现在 tool 进度更新或流式分片上。当启用了流式输出且最终文本已经被分片
投递完毕时，页脚会通过 ``send_trailing_footer()`` 作为一条独立的尾随消息
发送。
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional

_DEFAULT_FIELDS: tuple[str, ...] = ("model", "context_pct", "cwd")
_SEP = " · "


def _home_relative_cwd(cwd: str) -> str:
    """返回把 ``$HOME`` 折叠为 ``~`` 后的 *cwd*。未设置时返回空字符串。"""
    if not cwd:
        return ""
    try:
        home = os.path.expanduser("~")
        p = os.path.abspath(cwd)
        if home and (p == home or p.startswith(home + os.sep)):
            return "~" + p[len(home):]
        return p
    except Exception:
        return cwd


def _model_short(model: Optional[str]) -> str:
    """为可读性去掉 ``vendor/`` 前缀（``openai/gpt-5.4`` → ``gpt-5.4``）。"""
    if not model:
        return ""
    return model.rsplit("/", 1)[-1]


def resolve_footer_config(
    user_config: dict[str, Any] | None,
    platform_key: str | None = None,
) -> dict[str, Any]:
    """解析 *platform_key* 对应的实际生效的运行时页脚配置。

    合并顺序（后者覆盖前者）：
        1. 内置默认值（enabled=False）
        2. ``display.runtime_footer``
        3. ``display.platforms.<platform_key>.runtime_footer``
    """
    resolved = {"enabled": False, "fields": list(_DEFAULT_FIELDS)}
    cfg = (user_config or {}).get("display") or {}

    global_cfg = cfg.get("runtime_footer")
    if isinstance(global_cfg, dict):
        if "enabled" in global_cfg:
            resolved["enabled"] = bool(global_cfg.get("enabled"))
        if isinstance(global_cfg.get("fields"), list) and global_cfg["fields"]:
            resolved["fields"] = [str(f) for f in global_cfg["fields"]]

    if platform_key:
        platforms = cfg.get("platforms") or {}
        plat_cfg = platforms.get(platform_key)
        if isinstance(plat_cfg, dict):
            plat_footer = plat_cfg.get("runtime_footer")
            if isinstance(plat_footer, dict):
                if "enabled" in plat_footer:
                    resolved["enabled"] = bool(plat_footer.get("enabled"))
                if isinstance(plat_footer.get("fields"), list) and plat_footer["fields"]:
                    resolved["fields"] = [str(f) for f in plat_footer["fields"]]

    return resolved


def format_runtime_footer(
    *,
    model: Optional[str],
    context_tokens: int,
    context_length: Optional[int],
    cwd: Optional[str] = None,
    fields: Iterable[str] = _DEFAULT_FIELDS,
) -> str:
    """渲染页脚行；如果所有字段都没有数据则返回 ""。

    当字段对应的底层数据缺失时会静默跳过——一个部分填充的页脚，要好过
    一行带有 ``?%`` 或空槽的内容。
    """
    parts: list[str] = []
    for field in fields:
        if field == "model":
            m = _model_short(model)
            if m:
                parts.append(m)
        elif field == "context_pct":
            if context_length and context_length > 0 and context_tokens >= 0:
                pct = max(0, min(100, round((context_tokens / context_length) * 100)))
                parts.append(f"{pct}%")
        elif field == "cwd":
            rel = _home_relative_cwd(cwd or os.environ.get("TERMINAL_CWD", ""))
            if rel:
                parts.append(rel)
        # 未知的字段名会被静默忽略。

    if not parts:
        return ""
    return _SEP.join(parts)


def build_footer_line(
    *,
    user_config: dict[str, Any] | None,
    platform_key: str | None,
    model: Optional[str],
    context_tokens: int,
    context_length: Optional[int],
    cwd: Optional[str] = None,
) -> str:
    """gateway/run.py 使用的顶层入口。

    返回页脚文本（禁用或无数据时返回空字符串）。调用方自行将其追加到
    最终响应中，并保留一行空行作为分隔。
    """
    cfg = resolve_footer_config(user_config, platform_key)
    if not cfg.get("enabled"):
        return ""
    return format_runtime_footer(
        model=model,
        context_tokens=context_tokens,
        context_length=context_length,
        cwd=cwd,
        fields=cfg.get("fields") or _DEFAULT_FIELDS,
    )
