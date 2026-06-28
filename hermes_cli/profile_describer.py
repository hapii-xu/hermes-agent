"""Profile describer — 为 profile 自动生成 ``description``。

供 ``hermes profile describe <name> --auto`` 和 dashboard 的
"自动生成描述" 按钮使用。读取 profile 已安装的 skills、
model+provider、name，以及可选的一小部分 memory，
然后请求辅助 LLM 生成 1-2 句话描述该 profile 的用途。

结果写入 ``<profile_dir>/profile.yaml``，并标记
``description_auto: true``，以便 dashboard 显示"待审核"
徽标。用户随后可编辑以确认。

设计说明
------------
- 参照 ``hermes_cli/kanban_specify.py`` 的模式：函数内部
  延迟导入 aux client，宽松地解析响应，对于预期的失败场景
  永不抛出异常。
- 最多读取 ``MAX_SKILLS_FOR_PROMPT`` 个 skill 名称以保持
  prompt 可控。不包含 skill 正文 — 名称 + 类别已提供足够信号，
  避免在拥有 100+ skills 的 profile 上耗尽上下文。
- 此处故意不读取 memory。Memory 属于个人信息，
  编排器将工作路由到的是*角色*而非*履历*。如果后续发现
  memory 能提供有用信号可以再接入；目前，
  skills + name + model 已足够。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hermes_cli import profiles as profiles_mod
from agent.skill_utils import is_excluded_skill_path

logger = logging.getLogger(__name__)

# 限制送入 LLM 的 skill 名称数量。拥有 200+ skills 的
# profile（不常见但有可能）否则会撑爆上下文。此上限
# 按类别计算 — 详见 _collect_skills。
MAX_SKILLS_FOR_PROMPT = 60


_SYSTEM_PROMPT = """You are a profile-describer for the Hermes Agent kanban board.

A user runs multiple "profiles" — distinct agent identities, each with their
own skills, model, and configuration. The kanban board's orchestrator routes
work to whichever profile best fits each task. To do that well, every
profile needs a short, concrete description of what it's good at.

You are given a profile's:
  - Name
  - Model / provider
  - List of installed skill names (a strong signal of role / domain)

Produce a single JSON object with exactly one key:

  {
    "description": "<1-2 sentence description, plain prose, no preamble>"
  }

Rules:
  - The description is what an orchestrator will read to decide whether to
    route a task here. Lead with the profile's strongest capability.
  - Stay concrete. Bad: "an AI agent that helps users."
                  Good: "Reads and modifies Python codebases — runs tests,
                         refactors functions, opens GitHub PRs."
  - 1-2 sentences, <= 280 characters total.
  - Never invent capabilities the skills don't suggest.
  - Never write "Hermes Agent profile" or other meta-narration.
  - No code fences, no preamble, no closing remarks. Output only JSON.
"""


_USER_TEMPLATE = """Profile name: {name}
Default model: {model}
Provider: {provider}
Installed skill count: {skill_count}
Notable skills (up to {skill_cap}):
{skill_list}
"""


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass
class DescribeOutcome:
    """描述单个 profile 的结果。"""

    profile_name: str
    ok: bool
    reason: str = ""
    description: Optional[str] = None


def _collect_skills(profile_dir: Path) -> list[str]:
    """返回用于 prompt 的稳定、有上限的 skill 名称列表。

    格式：``category/skill_name``，其中 category 是 ``skills/`` 下的
    直接子目录（例如 ``devops``、``research``）。直接位于 ``skills/`` 下
    的 skill 显示为裸 ``skill_name``。
    """
    skills_dir = profile_dir / "skills"
    if not skills_dir.is_dir():
        return []
    names: list[str] = []
    for md in skills_dir.rglob("SKILL.md"):
        if is_excluded_skill_path(md):
            continue
        try:
            rel = md.relative_to(skills_dir)
        except ValueError:
            continue
        parts = rel.parts[:-1]  # drop SKILL.md filename
        if not parts:
            continue
        # parts[-1] 是 skill 目录名；parts[:-1] 是类别路径
        if len(parts) == 1:
            names.append(parts[0])
        else:
            names.append(f"{parts[0]}/{parts[-1]}")
    names.sort()
    # 保持在 prompt 预算之内。字母序靠前的 skill 并不更重要
    # — 让 LLM 看到一个采样即可。选取等间距的条目而非仅取
    # 头部，避免 A..Z 的 profile 被描述为"以 A 开头"。
    if len(names) <= MAX_SKILLS_FOR_PROMPT:
        return names
    step = len(names) / MAX_SKILLS_FOR_PROMPT
    sampled = [names[int(i * step)] for i in range(MAX_SKILLS_FOR_PROMPT)]
    return sampled


def _extract_json_blob(raw: str) -> Optional[dict]:
    if not raw:
        return None
    stripped = _FENCE_RE.sub("", raw.strip())
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first == -1 or last == -1 or last <= first:
        return None
    candidate = stripped[first : last + 1]
    try:
        val = json.loads(candidate)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(val, dict):
        return None
    return val


def describe_profile(
    profile_name: str,
    *,
    overwrite: bool = False,
    timeout: Optional[int] = None,
) -> DescribeOutcome:
    """为一个 profile 自动生成描述。

    返回描述执行结果的 outcome。对于预期的失败场景
    （profile 不存在、未配置 aux client、API 错误、
    响应格式错误）永不抛出异常 — 这些通过 ``ok=False`` 呈现，
    以便批量扫描可以跳过单个失败继续执行。

    ``overwrite`` 控制是否替换已有的用户编写描述。
    默认情况下，我们拒绝覆盖 ``description_auto: false``
    的描述以保护人工编写的内容。自动生成的描述
    （``description_auto: true``）始终可被替换。
    """
    canon = profiles_mod.normalize_profile_name(profile_name)
    if not profiles_mod.profile_exists(canon):
        # 特殊情况："default" 作为虚拟 profile 名称存在，
        # 映射到默认 home 目录。profile_exists() 会处理它。
        return DescribeOutcome(canon, False, "profile not found")

    try:
        if canon == "default":
            from hermes_constants import get_hermes_home  # type: ignore
            profile_dir = Path(get_hermes_home())
        else:
            profile_dir = profiles_mod.get_profile_dir(canon)
    except Exception as exc:
        return DescribeOutcome(canon, False, f"cannot resolve profile dir: {exc}")

    # 除非 --overwrite，否则保留人工编写的描述。
    existing = profiles_mod.read_profile_meta(profile_dir)
    if existing.get("description") and not existing.get("description_auto") and not overwrite:
        return DescribeOutcome(
            canon,
            False,
            "profile already has a user-authored description "
            "(use --overwrite to replace)",
        )

    skill_names = _collect_skills(profile_dir)
    skill_list = "\n".join(f"  - {n}" for n in skill_names) or "  (no skills installed)"
    skill_count = sum(
        1 for _ in (profile_dir / "skills").rglob("SKILL.md")
        if not is_excluded_skill_path(_)
    ) if (profile_dir / "skills").is_dir() else 0

    # 从 profile 的 config 中读取 model + provider。
    try:
        model, provider = profiles_mod._read_config_model(profile_dir)
    except Exception:
        model, provider = None, None

    try:
        from agent.auxiliary_client import (  # type: ignore
            get_auxiliary_extra_body,
            get_text_auxiliary_client,
        )
    except Exception as exc:
        logger.debug("describe: auxiliary client import failed: %s", exc)
        return DescribeOutcome(canon, False, "auxiliary client unavailable")

    try:
        client, aux_model = get_text_auxiliary_client("profile_describer")
    except Exception as exc:
        logger.debug("describe: get_text_auxiliary_client failed: %s", exc)
        return DescribeOutcome(canon, False, "auxiliary client unavailable")

    if client is None or not aux_model:
        return DescribeOutcome(canon, False, "no auxiliary client configured")

    user_msg = _USER_TEMPLATE.format(
        name=canon,
        model=(model or "(unset)"),
        provider=(provider or "(unset)"),
        skill_count=skill_count,
        skill_cap=MAX_SKILLS_FOR_PROMPT,
        skill_list=skill_list,
    )

    try:
        resp = client.chat.completions.create(
            model=aux_model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=400,
            timeout=timeout or 60,
            extra_body=get_auxiliary_extra_body() or None,
        )
    except Exception as exc:
        logger.info("describe: API call failed for %s (%s)", canon, exc)
        return DescribeOutcome(canon, False, f"LLM error: {type(exc).__name__}")

    try:
        raw = resp.choices[0].message.content or ""
    except Exception:
        raw = ""

    parsed = _extract_json_blob(raw)
    if parsed is None:
        # 回退方案：取原始文本并裁剪为一段。
        text = raw.strip().split("\n\n", 1)[0]
        if not text:
            return DescribeOutcome(canon, False, "LLM returned an empty response")
        description = text[:280]
    else:
        val = parsed.get("description")
        if not isinstance(val, str) or not val.strip():
            return DescribeOutcome(
                canon, False, "LLM response missing 'description' field"
            )
        description = val.strip()[:280]

    try:
        profiles_mod.write_profile_meta(
            profile_dir,
            description=description,
            description_auto=True,
        )
    except Exception as exc:
        return DescribeOutcome(canon, False, f"failed to write profile.yaml: {exc}")

    return DescribeOutcome(canon, True, "described", description=description)


def list_describable_profiles(*, missing_only: bool = True) -> list[str]:
    """返回可被描述的 profile 名称列表。

    ``missing_only=True``（默认）仅返回没有描述的 profile。
    ``missing_only=False`` 返回所有 profile。
    """
    out: list[str] = []
    for p in profiles_mod.list_profiles():
        if missing_only and (p.description or "").strip() and not p.description_auto:
            continue
        out.append(p.name)
    return out
