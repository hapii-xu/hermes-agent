"""Blueprints：叠加在技能 + cron 之上的、可分享的自然语言自动化。

"blueprint"（蓝图）并不是一种新的对象类型。它就是一个普通的技能（一个 agent 会加载
的 SKILL.md），只不过额外在其 frontmatter 中声明了一个自动化调度：

    metadata:
      hermes:
        blueprint:
          schedule: "0 9 * * *"     # 出现 `blueprint:` 即表示它可被调度运行
          deliver: origin            # 可选（默认 "origin"）
          prompt: "..."              # 可选，本次运行的任务指令
          no_agent: false            # 可选

因为蓝图本质上就是一个技能，所以它可以免费流经整个已有的 skills-hub 流水线 —— 搜索、
查看、隔离、安全扫描、安装、lock-file 溯源、审计日志、taps、集中化索引，以及
`hermes skills publish` 用于分享。没有新的来源类型、没有新的存储、没有新的传输方式。
本模块只是技能元数据与已有 cron `create_job()` API 之间的一层薄薄的桥接：

  * ``parse_blueprint(skill_md_text)``  -> BlueprintSpec | None
  * ``blueprint_spec_for_installed(name)`` -> BlueprintSpec | None
  * ``create_blueprint_job(spec, ...)`` -> 创建出的 cron job dict
  * ``export_blueprint(job, body)``      -> 一份可分享的 SKILL.md 字符串

开发指南中的「扩展而非重复」原则就是整个设计的核心：蓝图是技能，调度是 cron job，
分享是已有的 publish/tap/index 路径。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "BlueprintSpec",
    "parse_blueprint",
    "blueprint_spec_for_installed",
    "blueprint_to_job_spec",
    "create_blueprint_job",
    "register_blueprint_suggestion",
    "export_blueprint",
    "BlueprintError",
]


class BlueprintError(ValueError):
    """当 blueprint 块存在但格式错误时抛出。"""


@dataclass
class BlueprintSpec:
    """从某个技能解析出的 ``metadata.hermes.blueprint`` 自动化规格。"""

    skill_name: str
    schedule: str
    deliver: str = "origin"
    prompt: Optional[str] = None
    no_agent: bool = False
    model: Optional[str] = None
    provider: Optional[str] = None
    enabled_toolsets: Optional[List[str]] = None
    raw: Dict[str, Any] = field(default_factory=dict)


def _split_frontmatter(text: str) -> Optional[Dict[str, Any]]:
    """返回解析后的 YAML frontmatter 映射；若不存在或无效则返回 None。"""
    if not isinstance(text, str):
        return None
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return None
    # 在开栏之后寻找闭合栏。
    after_open = stripped[3:]
    end = after_open.find("\n---")
    if end == -1:
        return None
    fm_text = after_open[:end]
    try:
        import yaml

        data = yaml.safe_load(fm_text)
    except Exception as e:  # pragma: no cover - malformed YAML
        logger.debug("blueprint: frontmatter YAML parse failed: %s", e)
        return None
    return data if isinstance(data, dict) else None


def parse_blueprint(skill_md_text: str) -> Optional[BlueprintSpec]:
    """从一段 SKILL.md 字符串中提取 BlueprintSpec；若不是蓝图则返回 None。

    当且仅当 ``metadata.hermes.blueprint`` 是一个包含非空 ``schedule`` 的映射时，
    该技能才是一个蓝图。若该块存在但结构非法，则抛出 BlueprintError（这样一处笔误
    会被暴露出来，而不是静默地变成无操作）。
    """
    fm = _split_frontmatter(skill_md_text)
    if not fm:
        return None

    name = str(fm.get("name", "")).strip()

    meta = fm.get("metadata")
    hermes = meta.get("hermes") if isinstance(meta, dict) else None
    blueprint = hermes.get("blueprint") if isinstance(hermes, dict) else None
    if blueprint is None:
        return None
    if not isinstance(blueprint, dict):
        raise BlueprintError("metadata.hermes.blueprint must be a mapping")

    schedule = str(blueprint.get("schedule", "")).strip()
    if not schedule:
        raise BlueprintError("blueprint.schedule is required and must be non-empty")

    deliver = str(blueprint.get("deliver", "origin")).strip() or "origin"
    prompt = blueprint.get("prompt")
    if prompt is not None:
        prompt = str(prompt)
    no_agent = bool(blueprint.get("no_agent", False))
    model = blueprint.get("model")
    provider = blueprint.get("provider")
    toolsets = blueprint.get("enabled_toolsets")
    if toolsets is not None and not isinstance(toolsets, list):
        raise BlueprintError("blueprint.enabled_toolsets must be a list when present")

    return BlueprintSpec(
        skill_name=name,
        schedule=schedule,
        deliver=deliver,
        prompt=prompt,
        no_agent=no_agent,
        model=str(model).strip() if model else None,
        provider=str(provider).strip() if provider else None,
        enabled_toolsets=[str(t) for t in toolsets] if toolsets else None,
        raw=blueprint,
    )


def blueprint_spec_for_installed(skill_name: str) -> Optional[BlueprintSpec]:
    """定位某个已安装技能的 SKILL.md 并解析其中的 blueprint 块。

    在标准技能树中搜索 ``<skill_name>/SKILL.md``。若找不到该技能，或它不是蓝图，
    则返回 None。
    """
    try:
        from tools.skills_hub import SKILLS_DIR
    except Exception:  # pragma: no cover - import guard
        return None

    base = Path(SKILLS_DIR)
    # 技能位于 skills/<category>/<name>/SKILL.md 或 skills/<name>/SKILL.md。
    candidates = list(base.glob(f"**/{skill_name}/SKILL.md"))
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        spec = parse_blueprint(text)
        if spec is not None:
            # 优先用 frontmatter 里的 name，其次回退到目录名。
            if not spec.skill_name:
                spec.skill_name = skill_name
            return spec
    return None


def blueprint_to_job_spec(
    spec: BlueprintSpec,
    *,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """为 BlueprintSpec 构造 ``cron.jobs.create_job`` 的 kwargs dict。

    这是把蓝图翻译成 job 的唯一真相来源。直接的 ``create_blueprint_job`` 路径和
    建议路径（``register_blueprint_suggestion``）都建立在它之上，因此「现在调度的蓝图」
    和「从建议中采纳的蓝图」会产生完全相同的 job。
    """
    return {
        "prompt": spec.prompt,
        "schedule": spec.schedule,
        "name": name or f"blueprint:{spec.skill_name}",
        "deliver": spec.deliver,
        "skills": [spec.skill_name] if spec.skill_name else None,
        "model": spec.model,
        "provider": spec.provider,
        "enabled_toolsets": spec.enabled_toolsets,
        "no_agent": spec.no_agent,
    }


def create_blueprint_job(
    spec: BlueprintSpec,
    *,
    origin: Optional[Dict[str, Any]] = None,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """通过已有的 cron API 创建 BlueprintSpec 所描述的 cron job。

    该蓝图的技能会在运行之前被加载（cron 的 ``skills=[name]``）；可选的 ``prompt``
    成为任务指令。delivery、model 和 toolsets 会透传过去。返回创建出的 job dict。
    """
    from cron.jobs import create_job

    job_spec = blueprint_to_job_spec(spec, name=name)
    if origin is not None:
        job_spec["origin"] = origin
    return create_job(**job_spec)


def register_blueprint_suggestion(spec: BlueprintSpec) -> Optional[Dict[str, Any]]:
    """把一个已安装的蓝图变成一条待定的 Suggested Cron Job。

    蓝图是统一建议入口中来源为 ``blueprint`` 的条目：安装一个带 ``blueprint:`` 块的
    技能并不会自动调度它 —— 而是注册一条建议，让用户像对待其他建议一样去采纳（或忽略）。
    返回该建议记录；若被跳过（已见过/已忽略、积压已满等）则返回 None。
    """
    if not spec.skill_name:
        return None
    try:
        from cron.suggestions import add_suggestion
    except Exception:  # pragma: no cover - import guard
        return None

    return add_suggestion(
        title=f"Schedule '{spec.skill_name}'",
        description=(
            f"The '{spec.skill_name}' blueprint runs on schedule {spec.schedule}"
            + (f", delivering to {spec.deliver}" if spec.deliver and spec.deliver != "origin" else "")
            + "."
        ),
        source="blueprint",
        job_spec=blueprint_to_job_spec(spec),
        dedup_key=f"blueprint:{spec.skill_name}:{spec.schedule}",
    )


def export_blueprint(job: Dict[str, Any], body: str, *, blueprint_name: Optional[str] = None) -> str:
    """从已有的 cron job dict 渲染出一份可分享的蓝图 SKILL.md。

    这是 ``create_blueprint_job`` 的逆操作：拿一个用户已经建好的 cron job，产出一个
    SKILL.md（带 ``metadata.hermes.blueprint`` 块），用户可以把它交给
    ``hermes skills publish`` 来分享。``body`` 是自然语言的描述/说明，会成为 SKILL.md
    的正文。
    """
    import yaml

    name = blueprint_name or job.get("name") or "shared-blueprint"
    # 净化为合法的技能标识符。
    name = "".join(c if (c.isalnum() or c in "-_") else "-" for c in str(name).lower())
    name = name.strip("-_") or "shared-blueprint"

    schedule = job.get("schedule_display") or _schedule_to_string(job.get("schedule"))
    skills = job.get("skills") or ([job["skill"]] if job.get("skill") else [])

    blueprint_block: Dict[str, Any] = {"schedule": schedule}
    deliver = job.get("deliver")
    if deliver and deliver != "origin":
        blueprint_block["deliver"] = deliver
    if job.get("prompt"):
        blueprint_block["prompt"] = job["prompt"]
    if job.get("no_agent"):
        blueprint_block["no_agent"] = True
    if job.get("model"):
        blueprint_block["model"] = job["model"]
    if job.get("provider"):
        blueprint_block["provider"] = job["provider"]
    if job.get("enabled_toolsets"):
        blueprint_block["enabled_toolsets"] = job["enabled_toolsets"]

    description = (
        (body.strip().splitlines() or ["Shared automation blueprint."])[0][:200]
        if body.strip()
        else "Shared automation blueprint."
    )

    frontmatter = {
        "name": name,
        "description": description,
        "version": "1.0.0",
        "license": "MIT",
        "metadata": {
            "hermes": {
                "tags": ["blueprint", "automation"],
                "blueprint": blueprint_block,
            }
        },
    }
    fm_yaml = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    body_text = body.strip() or f"# {name}\n\nShared automation blueprint."
    return f"---\n{fm_yaml}\n---\n\n{body_text}\n"


def _schedule_to_string(schedule: Any) -> str:
    """尽力把一个已解析的 schedule dict 重新渲染回字符串。"""
    if isinstance(schedule, str):
        return schedule
    if isinstance(schedule, dict):
        kind = schedule.get("kind")
        if kind == "cron" and schedule.get("expr"):
            return str(schedule["expr"])
        if kind == "interval":
            # parse_schedule 把 interval 周期存为 "minutes"；同时也容忍遗留/外来的
            # "seconds" 形式。
            if schedule.get("minutes"):
                mins = int(schedule["minutes"])
                if mins % 60 == 0:
                    return f"every {mins // 60}h"
                return f"every {mins}m"
            if schedule.get("seconds"):
                secs = int(schedule["seconds"])
                if secs % 3600 == 0:
                    return f"every {secs // 3600}h"
                if secs % 60 == 0:
                    return f"every {secs // 60}m"
                return f"every {secs}s"
    return "0 9 * * *"  # 安全的每日兜底
