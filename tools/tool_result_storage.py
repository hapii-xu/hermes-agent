"""工具结果持久化 —— 保留大输出而非截断。

针对上下文窗口溢出的防御分三层：

1. **单工具输出上限**（在各工具内部）：search_files 等工具在返回前会先
   截断自身输出。这是第一道防线，也是工具作者唯一能控制的一层。

2. **单结果持久化**（maybe_persist_tool_result）：工具返回后，若其输出
   超过该工具注册的阈值（registry.get_max_result_size），完整输出会被
   写入沙箱临时目录（例如标准 Linux 上的 /tmp/hermes-results/{tool_use_id}.txt，
   或 Termux 上的 $TMPDIR/hermes-results/{tool_use_id}.txt），通过
   env.execute() 完成。上下文中的内容被替换为预览 + 文件路径引用。
   模型可以在任意后端上调用 read_file 来访问完整输出。

3. **单轮聚合预算**（enforce_turn_budget）：单个助手轮次内收集完所有工具
   结果后，若总量超过 MAX_TURN_BUDGET_CHARS（200K），会把最大的未持久化
   结果逐个转储到磁盘，直到总量低于预算。这一层用于兜底——当许多中等大小
   的结果叠加导致上下文溢出时。
"""

import logging
import os
import shlex
import uuid

from tools.budget_config import (
    DEFAULT_PREVIEW_SIZE_CHARS,
    BudgetConfig,
    DEFAULT_BUDGET,
)

logger = logging.getLogger(__name__)
PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"
STORAGE_DIR = "/tmp/hermes-results"
HEREDOC_MARKER = "HERMES_PERSIST_EOF"
_BUDGET_TOOL_NAME = "__budget_enforcement__"


def _resolve_storage_dir(env) -> str:
    """返回当前环境下最适合的、由临时目录支撑的存储目录。"""
    if env is not None:
        get_temp_dir = getattr(env, "get_temp_dir", None)
        if callable(get_temp_dir):
            try:
                temp_dir = get_temp_dir()
            except Exception as exc:
                logger.debug("Could not resolve env temp dir: %s", exc)
            else:
                if temp_dir:
                    temp_dir = temp_dir.rstrip("/") or "/"
                    return f"{temp_dir}/hermes-results"
    return STORAGE_DIR


def generate_preview(content: str, max_chars: int = DEFAULT_PREVIEW_SIZE_CHARS) -> tuple[str, bool]:
    """在 max_chars 范围内的最后一个换行处截断。返回 (preview, has_more)。"""
    if len(content) <= max_chars:
        return content, False
    truncated = content[:max_chars]
    last_nl = truncated.rfind("\n")
    if last_nl > max_chars // 2:
        truncated = truncated[:last_nl + 1]
    return truncated, True


def _heredoc_marker(content: str) -> str:
    """返回一个不与内容冲突的 heredoc 分隔符。"""
    if HEREDOC_MARKER not in content:
        return HEREDOC_MARKER
    return f"HERMES_PERSIST_{uuid.uuid4().hex[:8]}"


def _write_to_sandbox(content: str, remote_path: str, env) -> bool:
    """通过 env.execute() 将 content 写入沙箱。成功返回 True。

    通过 stdin 推送 ``content``，而不是将其嵌入命令字符串。Linux 的
    ``MAX_ARG_STRLEN`` 将单个 argv 元素上限限制为 128 KB（32 * PAGE_SIZE），
    因此之前把 heredoc 嵌入命令字符串的做法，在工具结果超过约 128 KB 时
    会以 ``OSError: [Errno 7] Argument list too long`` 静默失败——而这
    恰恰正是持久化机制要处理的场景。改为经 stdin 传输后，本地 + ssh
    （``_stdin_mode == "pipe"``）上不再有这一上限；``_stdin_mode ==
    "heredoc"`` 的远程后端仍保留其基于 API body 的体积上限，该上限比 exec
    参数上限高出数个数量级。
    """
    storage_dir = os.path.dirname(remote_path)
    cmd = f"mkdir -p {shlex.quote(storage_dir)} && cat > {shlex.quote(remote_path)}"
    result = env.execute(cmd, timeout=30, stdin_data=content)
    return result.get("returncode", 1) == 0


def _build_persisted_message(
    preview: str,
    has_more: bool,
    original_size: int,
    file_path: str,
) -> str:
    """构建 <persisted-output> 替换块。"""
    size_kb = original_size / 1024
    if size_kb >= 1024:
        size_str = f"{size_kb / 1024:.1f} MB"
    else:
        size_str = f"{size_kb:.1f} KB"

    msg = f"{PERSISTED_OUTPUT_TAG}\n"
    msg += f"This tool result was too large ({original_size:,} characters, {size_str}).\n"
    msg += f"Full output saved to: {file_path}\n"
    msg += "Use the read_file tool with offset and limit to access specific sections of this output.\n\n"
    msg += f"Preview (first {len(preview)} chars):\n"
    msg += preview
    if has_more:
        msg += "\n..."
    msg += f"\n{PERSISTED_OUTPUT_CLOSING_TAG}"
    return msg


def maybe_persist_tool_result(
    content: str,
    tool_name: str,
    tool_use_id: str,
    env=None,
    config: BudgetConfig = DEFAULT_BUDGET,
    threshold: int | float | None = None,
) -> str:
    """第二层：将超大结果持久化到沙箱，返回预览 + 路径。

    通过 env.execute() 写入，使文件可从任意后端访问（本地、Docker、SSH、
    Modal、Daytona）。写入失败或没有 env 时，回退为内联截断。

    参数：
        content: 原始工具结果字符串。
        tool_name: 工具名称（用于阈值查找）。
        tool_use_id: 本次工具调用的唯一 ID（用作文件名）。
        env: 当前活动的 BaseEnvironment 实例，或 None。
        config: BudgetConfig，控制阈值与预览大小。
        threshold: 显式覆盖；优先于配置解析结果。

    返回：
        若内容较小则返回原始内容，否则返回 <persisted-output> 替换块。
    """
    effective_threshold = threshold if threshold is not None else config.resolve_threshold(tool_name)

    if effective_threshold == float("inf"):
        return content

    if len(content) <= effective_threshold:
        return content

    storage_dir = _resolve_storage_dir(env)
    remote_path = f"{storage_dir}/{tool_use_id}.txt"
    preview, has_more = generate_preview(content, max_chars=config.preview_size)

    if env is not None:
        try:
            if _write_to_sandbox(content, remote_path, env):
                logger.info(
                    "Persisted large tool result: %s (%s, %d chars -> %s)",
                    tool_name, tool_use_id, len(content), remote_path,
                )
                return _build_persisted_message(preview, has_more, len(content), remote_path)
        except Exception as exc:
            logger.warning("Sandbox write failed for %s: %s", tool_use_id, exc)

    logger.info(
        "Inline-truncating large tool result: %s (%d chars, no sandbox write)",
        tool_name, len(content),
    )
    return (
        f"{preview}\n\n"
        f"[Truncated: tool response was {len(content):,} chars. "
        f"Full output could not be saved to sandbox.]"
    )


def enforce_turn_budget(
    tool_messages: list[dict],
    env=None,
    config: BudgetConfig = DEFAULT_BUDGET,
) -> list[dict]:
    """第三层：对一轮内所有工具结果执行聚合预算控制。

    若总字符数超出预算，优先（通过沙箱写入）持久化最大的未持久化结果，直到
    总量低于预算。已持久化的结果会被跳过。

    就地修改该列表并返回。
    """
    candidates = []
    total_size = 0
    for i, msg in enumerate(tool_messages):
        content = msg.get("content", "")
        size = len(content)
        total_size += size
        if PERSISTED_OUTPUT_TAG not in content:
            candidates.append((i, size))

    if total_size <= config.turn_budget:
        return tool_messages

    candidates.sort(key=lambda x: x[1], reverse=True)

    for idx, size in candidates:
        if total_size <= config.turn_budget:
            break
        msg = tool_messages[idx]
        content = msg["content"]
        tool_use_id = msg.get("tool_call_id", f"budget_{idx}")

        replacement = maybe_persist_tool_result(
            content=content,
            tool_name=_BUDGET_TOOL_NAME,
            tool_use_id=tool_use_id,
            env=env,
            config=config,
            threshold=0,
        )
        if replacement != content:
            total_size -= size
            total_size += len(replacement)
            tool_messages[idx]["content"] = replacement
            logger.info(
                "Budget enforcement: persisted tool result %s (%d chars)",
                tool_use_id, size,
            )

    return tool_messages
