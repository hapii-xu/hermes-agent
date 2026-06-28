"""Prompt 大小诊断工具：``hermes prompt-size``。

报告代理为新会话构建的系统提示的字节/字符明细——系统提示总量、
``<available_skills>`` 索引、memory + 用户画像，以及 tool schema JSON。
让用户了解固定 prompt 预算的分配情况 (issue #34667)，无需手动解析
已保存的会话 JSON。

该诊断工具构建一个真实的检查代理（使数字与实际发送的数据一致），
但不会发起网络调用：它传入虚拟凭证使 ``AIAgent.__init__`` 走直接构造路径，
然后离线调用 ``build_system_prompt_parts`` / 检查 ``agent.tools``。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

# skills 索引在 stable 层中被包裹在此标签对里。
_SKILLS_BLOCK_RE = re.compile(r"<available_skills>.*?</available_skills>", re.DOTALL)


def _bytes(s: str) -> int:
    return len(s.encode("utf-8"))


def _build_inspection_agent(platform: str) -> Any:
    """构建一个离线的 AIAgent 用于 prompt 检查。

    虚拟的 ``api_key`` + ``base_url`` 强制 ``run_agent.py`` 走直接构造路径
    （不进行 provider 自动检测，不发起网络请求）。toolset 和 platform
    来自调用方，使明细与实际会话一致。
    """
    from run_agent import AIAgent
    from hermes_cli.config import load_config

    cfg = load_config()
    model_cfg = cfg.get("model", {}) if isinstance(cfg.get("model"), dict) else {}
    model = model_cfg.get("default") or model_cfg.get("model") or ""

    return AIAgent(
        model=model,
        api_key="inspect-only",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        save_trajectories=False,
        platform=platform,
    )


def compute_prompt_breakdown(platform: str = "cli") -> Dict[str, Any]:
    """返回一个新会话的 prompt 大小测量字典。

    键：``system_prompt``（字符/字节）、``skills_index``、``memory``、
    ``user_profile``、``tools``（数量 + JSON 字节数），以及 ``sections``
    （三个 prompt 层级的 (标签, 字符数, 字节数) 列表）。
    """
    from agent.system_prompt import build_system_prompt, build_system_prompt_parts

    agent = _build_inspection_agent(platform)

    parts = build_system_prompt_parts(agent)
    full = build_system_prompt(agent)

    stable = parts.get("stable", "")
    context = parts.get("context", "")
    volatile = parts.get("volatile", "")

    # Skills 索引——<available_skills> 块（安装大量 skill 时是最大的单块）。
    # 在 stable 层内测量。
    skills_match = _SKILLS_BLOCK_RE.search(stable)
    skills_index = skills_match.group(0) if skills_match else ""

    # Memory + 用户画像位于 volatile 层。我们直接从 memory store
    # 重新提取它们的内容块，使数字可追溯，即使它们被合并到 ``volatile`` 中。
    memory_block = ""
    user_block = ""
    store = getattr(agent, "_memory_store", None)
    if store is not None:
        try:
            if getattr(agent, "_memory_enabled", True):
                memory_block = store.format_for_system_prompt("memory") or ""
            if getattr(agent, "_user_profile_enabled", True):
                user_block = store.format_for_system_prompt("user") or ""
        except Exception:
            pass

    # Tool schema JSON——固定每次调用载荷的另一半。
    tools = getattr(agent, "tools", None) or []
    tools_json = json.dumps(tools, ensure_ascii=False)

    sections: List[Tuple[str, int, int]] = [
        ("stable (identity/guidance/skills)", len(stable), _bytes(stable)),
        ("context (AGENTS.md/cwd files)", len(context), _bytes(context)),
        ("volatile (memory/profile/timestamp)", len(volatile), _bytes(volatile)),
    ]

    return {
        "platform": platform,
        "model": getattr(agent, "model", "") or "",
        "system_prompt": {"chars": len(full), "bytes": _bytes(full)},
        "skills_index": {"chars": len(skills_index), "bytes": _bytes(skills_index)},
        "memory": {"chars": len(memory_block), "bytes": _bytes(memory_block)},
        "user_profile": {"chars": len(user_block), "bytes": _bytes(user_block)},
        "tools": {"count": len(tools), "json_bytes": _bytes(tools_json)},
        "sections": sections,
    }


def _fmt_kb(n: int) -> str:
    return f"{n / 1024:.1f} KB"


def render_breakdown(data: Dict[str, Any]) -> str:
    """将明细渲染为适合终端显示的纯文本。"""
    lines: List[str] = []
    sp = data["system_prompt"]
    lines.append(f"Prompt-size breakdown (platform={data['platform']}, model={data['model'] or 'unset'})")
    lines.append("")
    lines.append(f"  System prompt total : {sp['bytes']:>8,} B  ({_fmt_kb(sp['bytes'])}, {sp['chars']:,} chars)")
    lines.append("")
    lines.append("  Major blocks:")
    si = data["skills_index"]
    mem = data["memory"]
    up = data["user_profile"]
    lines.append(f"    skills index       : {si['bytes']:>8,} B  ({_fmt_kb(si['bytes'])})")
    lines.append(f"    memory             : {mem['bytes']:>8,} B  ({_fmt_kb(mem['bytes'])})")
    lines.append(f"    user profile       : {up['bytes']:>8,} B  ({_fmt_kb(up['bytes'])})")
    lines.append("")
    lines.append("  Prompt tiers:")
    for label, chars, byts in data["sections"]:
        lines.append(f"    {label:<36}: {byts:>8,} B  ({_fmt_kb(byts)})")
    lines.append("")
    tools = data["tools"]
    lines.append(f"  Tool schemas         : {tools['json_bytes']:>8,} B  ({_fmt_kb(tools['json_bytes'])}, {tools['count']} tools)")
    return "\n".join(lines)


def cmd_prompt_size(args: Any) -> None:
    """``hermes prompt-size`` 的入口点。"""
    platform = getattr(args, "platform", "cli") or "cli"
    as_json = getattr(args, "json", False)
    try:
        data = compute_prompt_breakdown(platform)
    except Exception as e:
        print(f"Could not compute prompt-size breakdown: {e}")
        return
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render_breakdown(data))
