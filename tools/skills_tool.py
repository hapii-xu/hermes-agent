#!/usr/bin/env python3
"""
技能工具模块

本模块提供用于列出和查看技能文档的工具。
技能以目录形式组织，每个目录包含一个 SKILL.md 文件（主指令）
以及可选的辅助文件，如参考文档、模板和示例。

灵感来自 Anthropic 的 Claude Skills 系统，采用渐进式披露架构：
- 元数据（name ≤64 字符，description ≤1024 字符）——在 skills_list 中展示
- 完整指令——在需要时通过 skill_view 加载
- 关联文件（参考文档、模板）——按需加载

目录结构：
    skills/
    ├── my-skill/
    │   ├── SKILL.md           # 主指令（必需）
    │   ├── references/        # 辅助文档
    │   │   ├── api.md
    │   │   └── examples.md
    │   ├── templates/         # 输出模板
    │   │   └── template.md
    │   └── assets/            # 补充文件（agentskills.io 标准）
    └── category/              # 用于组织的分类文件夹
        └── another-skill/
            └── SKILL.md

SKILL.md 格式（YAML Frontmatter，兼容 agentskills.io）：
    ---
    name: skill-name              # 必需，最长 64 字符
    description: Brief description # 必需，最长 1024 字符
    version: 1.0.0                # 可选
    license: MIT                  # 可选（agentskills.io）
    platforms: [macos]            # 可选——限制为特定操作系统平台
                                  #   取值：macos、linux、windows
                                  #   省略则在所有平台加载（默认）
    prerequisites:                # 可选——遗留的运行时依赖
      env_vars: [API_KEY]         #   遗留环境变量名在加载时会被规范化为
                                  #   required_environment_variables。
      commands: [curl, jq]        #   命令检查仅作为提示。
    compatibility: Requires X     # 可选（agentskills.io）
    metadata:                     # 可选，任意键值对（agentskills.io）
      hermes:
        tags: [fine-tuning, llm]
        related_skills: [peft, lora]
    ---

    # 技能标题

    完整的指令和内容写在这里...

可用工具：
- skills_list：列出技能及其元数据（渐进式披露第 1 层）
- skill_view：加载技能的完整内容（渐进式披露第 2-3 层）

用法：
    from tools.skills_tool import skills_list, skill_view, check_skills_requirements

    # 列出所有技能（仅返回元数据——节省 token）
    result = skills_list()

    # 查看某个技能的主要内容（加载完整指令）
    content = skill_view("axolotl")

    # 查看技能内的某个参考文件（加载关联文件）
    content = skill_view("axolotl", "references/dataset-formats.md")
"""

import json
import logging

from hermes_constants import get_hermes_home, display_hermes_home
import os
import re
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Dict, Any, List, Optional, Set, Tuple

from tools.registry import registry, tool_error
from hermes_cli.config import cfg_get
from utils import env_var_enabled
from agent.skill_utils import (
    EXCLUDED_SKILL_DIRS as _EXCLUDED_SKILL_DIRS,
    is_skill_support_path as _is_skill_support_path,
)

logger = logging.getLogger(__name__)


# 所有技能都位于 ~/.hermes/skills/（安装时从内置的 skills/ 目录种子化生成）。
# 这是唯一的数据源——agent 编辑、hub 安装和内置技能都共存于此，
# 不会污染 git 仓库。
HERMES_HOME = get_hermes_home()
SKILLS_DIR = HERMES_HOME / "skills"

# Anthropic 推荐的限制值，用于保证渐进式披露的效率
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024

# 'platforms' frontmatter 字段的平台标识符。
# 将用户友好的名称映射到 sys.platform 前缀。
_PLATFORM_MAP = {
    "macos": "darwin",
    "linux": "linux",
    "windows": "win32",
}
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REMOTE_ENV_BACKENDS = frozenset(
    {"docker", "singularity", "modal", "ssh", "daytona"}
)
_secret_capture_callback = None


def _skill_lookup_path_error(name: str) -> Optional[str]:
    """返回当本地技能查找的 *name* 可能逃出搜索根目录时的错误信息。

    技能的 ``name`` 会被拼接到每个受信任的搜索目录上以构建磁盘查找路径，
    因此它必须保持相对路径且不含 ``..`` 段——否则 ``name="../outside"``
    或绝对路径可能选中（并读取）技能目录之外的技能。这与稍后通过
    ``tools.path_security`` 对 ``file_path`` 做的校验保持一致。我们还会拒绝
    Windows 盘符路径（例如 ``C:\\skills``），因为其中的 ``:`` 否则会被误读为
    插件命名空间分隔符。
    """
    from tools.path_security import has_traversal_component

    if not isinstance(name, str):
        return "Skill name must be a string."
    candidate = name.strip()
    if (
        PurePosixPath(candidate).is_absolute()
        or PureWindowsPath(candidate).is_absolute()
        or PureWindowsPath(candidate).drive
    ):
        return "Skill name must be a relative path within the skills directory."
    if has_traversal_component(candidate):
        return "Skill name cannot contain '..' path traversal components."
    return None


def load_env() -> Dict[str, str]:
    """从 HERMES_HOME/.env 加载 profile 作用域的环境变量。"""
    env_path = get_hermes_home() / ".env"
    env_vars: Dict[str, str] = {}
    if not env_path.exists():
        return env_vars

    with env_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env_vars[key.strip()] = value.strip().strip("\"'")
    return env_vars


class SkillReadinessStatus(str, Enum):
    AVAILABLE = "available"
    SETUP_NEEDED = "setup_needed"
    UNSUPPORTED = "unsupported"


# 提示词注入检测——本地技能路径和插件技能路径共用。
_INJECTION_PATTERNS: list = [
    "ignore previous instructions",
    "ignore all previous",
    "you are now",
    "disregard your",
    "forget your instructions",
    "new instructions:",
    "system prompt:",
    "<system>",
    "]]>",
]


def set_secret_capture_callback(callback) -> None:
    global _secret_capture_callback
    _secret_capture_callback = callback


def skill_matches_platform(frontmatter: Dict[str, Any]) -> bool:
    """检查技能是否与当前操作系统平台兼容。

    委托给 ``agent.skill_utils.skill_matches_platform``——保留在此处作为
    公开再导出，这样现有的调用方就不需要修改。
    """
    from agent.skill_utils import skill_matches_platform as _impl
    return _impl(frontmatter)


def skill_matches_environment(frontmatter: Dict[str, Any]) -> bool:
    """检查技能是否与当前运行时环境相关。

    委托给 ``agent.skill_utils.skill_matches_environment``——保留在此处作为
    公开再导出，这样现有的调用方就不需要修改。这是一个 offer 阶段的相关性
    闸门（kanban/docker/s6），而非硬性兼容性闸门；显式的技能加载会绕过它。
    """
    from agent.skill_utils import skill_matches_environment as _impl
    return _impl(frontmatter)


def _normalize_prerequisite_values(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(item) for item in value if str(item).strip()]


def _collect_prerequisite_values(
    frontmatter: Dict[str, Any],
) -> Tuple[List[str], List[str]]:
    prereqs = frontmatter.get("prerequisites")
    if not prereqs or not isinstance(prereqs, dict):
        return [], []
    return (
        _normalize_prerequisite_values(prereqs.get("env_vars")),
        _normalize_prerequisite_values(prereqs.get("commands")),
    )


def _normalize_setup_metadata(frontmatter: Dict[str, Any]) -> Dict[str, Any]:
    setup = frontmatter.get("setup")
    if not isinstance(setup, dict):
        return {"help": None, "collect_secrets": []}

    help_text = setup.get("help")
    normalized_help = (
        str(help_text).strip()
        if isinstance(help_text, str) and help_text.strip()
        else None
    )

    collect_secrets_raw = setup.get("collect_secrets")
    if isinstance(collect_secrets_raw, dict):
        collect_secrets_raw = [collect_secrets_raw]
    if not isinstance(collect_secrets_raw, list):
        collect_secrets_raw = []

    collect_secrets: List[Dict[str, Any]] = []
    for item in collect_secrets_raw:
        if not isinstance(item, dict):
            continue

        env_var = str(item.get("env_var") or "").strip()
        if not env_var:
            continue

        prompt = str(item.get("prompt") or f"Enter value for {env_var}").strip()
        provider_url = str(item.get("provider_url") or item.get("url") or "").strip()

        entry: Dict[str, Any] = {
            "env_var": env_var,
            "prompt": prompt,
            "secret": bool(item.get("secret", True)),
        }
        if provider_url:
            entry["provider_url"] = provider_url
        collect_secrets.append(entry)

    return {
        "help": normalized_help,
        "collect_secrets": collect_secrets,
    }


def _get_required_environment_variables(
    frontmatter: Dict[str, Any],
    legacy_env_vars: List[str] | None = None,
) -> List[Dict[str, Any]]:
    setup = _normalize_setup_metadata(frontmatter)
    required_raw = frontmatter.get("required_environment_variables")
    if isinstance(required_raw, dict):
        required_raw = [required_raw]
    if not isinstance(required_raw, list):
        required_raw = []

    required: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def _append_required(entry: Dict[str, Any]) -> None:
        env_name = str(entry.get("name") or entry.get("env_var") or "").strip()
        if not env_name or env_name in seen:
            return
        if not _ENV_VAR_NAME_RE.match(env_name):
            return

        normalized: Dict[str, Any] = {
            "name": env_name,
            "prompt": str(entry.get("prompt") or f"Enter value for {env_name}").strip(),
        }

        help_text = (
            entry.get("help")
            or entry.get("provider_url")
            or entry.get("url")
            or setup.get("help")
        )
        if isinstance(help_text, str) and help_text.strip():
            normalized["help"] = help_text.strip()

        required_for = entry.get("required_for")
        if isinstance(required_for, str) and required_for.strip():
            normalized["required_for"] = required_for.strip()

        if entry.get("optional"):
            normalized["optional"] = True

        seen.add(env_name)
        required.append(normalized)

    for item in required_raw:
        if isinstance(item, str):
            _append_required({"name": item})
            continue
        if isinstance(item, dict):
            _append_required(item)

    for item in setup["collect_secrets"]:
        _append_required(
            {
                "name": item.get("env_var"),
                "prompt": item.get("prompt"),
                "help": item.get("provider_url") or setup.get("help"),
            }
        )

    if legacy_env_vars is None:
        legacy_env_vars, _ = _collect_prerequisite_values(frontmatter)
    for env_var in legacy_env_vars:
        _append_required({"name": env_var})

    return required


def _capture_required_environment_variables(
    skill_name: str,
    missing_entries: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not missing_entries:
        return {
            "missing_names": [],
            "setup_skipped": False,
            "gateway_setup_hint": None,
        }

    missing_names = [entry["name"] for entry in missing_entries]
    # 大多数 gateway 表面（消息平台）无法弹出密钥输入提示，因此会直接短路
    # 返回“不支持”的提示。交互式 gateway 表面——桌面应用 / TUI——会设置
    # HERMES_INTERACTIVE 并注册一个 secret-capture 回调，该回调会路由到一个
    # 安全的 secret.request 浮层，因此它们会走到真正弹出提示的逻辑。
    # （HERMES_INTERACTIVE 与 tools/approval.py 用来区分交互式表面和消息
    # 表面的标志是同一个。）
    if _is_gateway_surface() and not env_var_enabled("HERMES_INTERACTIVE"):
        return {
            "missing_names": missing_names,
            "setup_skipped": False,
            "gateway_setup_hint": _gateway_setup_hint(),
        }

    if _secret_capture_callback is None:
        return {
            "missing_names": missing_names,
            "setup_skipped": False,
            "gateway_setup_hint": None,
        }

    setup_skipped = False
    remaining_names: List[str] = []

    for entry in missing_entries:
        metadata = {"skill_name": skill_name}
        if entry.get("help"):
            metadata["help"] = entry["help"]
        if entry.get("required_for"):
            metadata["required_for"] = entry["required_for"]

        try:
            callback_result = _secret_capture_callback(
                entry["name"],
                entry["prompt"],
                metadata,
            )
        except Exception:
            logger.warning(
                f"Secret capture callback failed for {entry['name']}", exc_info=True
            )
            callback_result = {
                "success": False,
                "stored_as": entry["name"],
                "validated": False,
                "skipped": True,
            }

        success = isinstance(callback_result, dict) and bool(
            callback_result.get("success")
        )
        skipped = isinstance(callback_result, dict) and bool(
            callback_result.get("skipped")
        )
        if success and not skipped:
            continue

        setup_skipped = True
        remaining_names.append(entry["name"])

    return {
        "missing_names": remaining_names,
        "setup_skipped": setup_skipped,
        "gateway_setup_hint": None,
    }


def _is_gateway_surface() -> bool:
    if env_var_enabled("HERMES_GATEWAY_SESSION"):
        return True
    from gateway.session_context import get_session_env
    return bool(get_session_env("HERMES_SESSION_PLATFORM"))


def _get_terminal_backend_name() -> str:
    return str(os.getenv("TERMINAL_ENV", "local")).strip().lower() or "local"


def _is_env_var_persisted(
    var_name: str, env_snapshot: Dict[str, str] | None = None
) -> bool:
    if env_snapshot is None:
        env_snapshot = load_env()
    if var_name in env_snapshot:
        return bool(env_snapshot.get(var_name))
    return bool(os.getenv(var_name))


def _remaining_required_environment_names(
    required_env_vars: List[Dict[str, Any]],
    capture_result: Dict[str, Any],
    *,
    env_snapshot: Dict[str, str] | None = None,
) -> List[str]:
    missing_names = set(capture_result["missing_names"])

    if env_snapshot is None:
        env_snapshot = load_env()
    remaining = []
    for entry in required_env_vars:
        name = entry["name"]
        if entry.get("optional"):
            continue
        if name in missing_names or not _is_env_var_persisted(name, env_snapshot):
            remaining.append(name)
    return remaining


def _gateway_setup_hint() -> str:
    try:
        from gateway.platforms.base import GATEWAY_SECRET_CAPTURE_UNSUPPORTED_MESSAGE

        return GATEWAY_SECRET_CAPTURE_UNSUPPORTED_MESSAGE
    except Exception:
        return f"Secure secret entry is not available. Load this skill in the local CLI to be prompted, or add the key to {display_hermes_home()}/.env manually."


def _build_setup_note(
    readiness_status: SkillReadinessStatus,
    missing: List[str],
    setup_help: str | None = None,
) -> str | None:
    if readiness_status == SkillReadinessStatus.SETUP_NEEDED:
        missing_str = ", ".join(missing) if missing else "required prerequisites"
        note = f"Setup needed before using this skill: missing {missing_str}."
        if setup_help:
            return f"{note} {setup_help}"
        return note
    return None


def check_skills_requirements() -> bool:
    """技能始终可用——目录会在首次使用时按需创建。"""
    return True


def _parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """从 markdown 内容中解析 YAML frontmatter。

    委托给 ``agent.skill_utils.parse_frontmatter``——保留在此处作为
    公开再导出，这样现有的调用方就不需要修改。
    """
    from agent.skill_utils import parse_frontmatter
    return parse_frontmatter(content)


def _get_category_from_path(skill_path: Path) -> Optional[str]:
    """
    根据目录结构从技能路径中提取分类。

    例如路径：~/.hermes/skills/mlops/axolotl/SKILL.md -> "mlops"
    同样适用于通过 skills.external_dirs 配置的外部技能目录。
    """
    # 先尝试模块级的 SKILLS_DIR（兼容测试中的 monkeypatch），
    # 再回退到配置中的外部目录。
    dirs_to_check = [SKILLS_DIR]
    try:
        from agent.skill_utils import get_external_skills_dirs
        dirs_to_check.extend(get_external_skills_dirs())
    except Exception:
        pass
    for skills_dir in dirs_to_check:
        try:
            rel_path = skill_path.relative_to(skills_dir)
            parts = rel_path.parts
            if len(parts) >= 3:
                return parts[0]
        except ValueError:
            continue
    return None


def _parse_tags(tags_value) -> List[str]:
    """
    从 frontmatter 值中解析标签。

    处理以下情况：
    - 已解析的列表（来自 yaml.safe_load）：[tag1, tag2]
    - 带括号的字符串："[tag1, tag2]"
    - 逗号分隔的字符串："tag1, tag2"

    参数：
        tags_value：原始标签值——可能是列表或字符串

    返回：
        标签字符串列表
    """
    if not tags_value:
        return []

    # yaml.safe_load 对 [tag1, tag2] 已经返回一个列表
    if isinstance(tags_value, list):
        return [str(t).strip() for t in tags_value if t]

    # 字符串回退——处理带括号或逗号分隔的情况
    tags_value = str(tags_value).strip()
    if tags_value.startswith("[") and tags_value.endswith("]"):
        tags_value = tags_value[1:-1]

    return [t.strip().strip("\"'") for t in tags_value.split(",") if t.strip()]



def _get_disabled_skill_names() -> Set[str]:
    """从配置加载被禁用的技能名称。

    委托给 ``agent.skill_utils.get_disabled_skill_names``——保留在此处作为
    公开再导出，这样现有的调用方就不需要修改。
    """
    from agent.skill_utils import get_disabled_skill_names
    return get_disabled_skill_names()


def _get_session_platform() -> str:
    """从 gateway 会话上下文中解析当前平台。

    镜像了 ``agent.skill_utils.get_disabled_skill_names`` 中的平台解析逻辑，
    使得 ``_is_skill_disabled`` 也会遵循 ``HERMES_SESSION_PLATFORM``。
    """
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_PLATFORM") or ""
    except Exception:
        return ""


def _is_skill_disabled(name: str, platform: str = None) -> bool:
    """检查某个技能是否在配置中被禁用。

    按以下优先级顺序解析当前激活的平台：
    1. 显式传入的 ``platform`` 参数
    2. ``HERMES_PLATFORM`` 环境变量
    3. 来自 gateway 会话上下文的 ``HERMES_SESSION_PLATFORM``
    """
    try:
        from hermes_cli.config import load_config
        config = load_config()
        skills_cfg = config.get("skills", {})
        resolved_platform = platform or os.getenv("HERMES_PLATFORM") or _get_session_platform()
        global_disabled = skills_cfg.get("disabled", [])
        if resolved_platform:
            platform_disabled = cfg_get(skills_cfg, "platform_disabled", resolved_platform)
            if platform_disabled is not None:
                # 全局禁用的技能在每个平台上都保持禁用；
                # 平台列表是追加而非替换。需与
                # agent.skill_utils.get_disabled_skill_names 保持同步。
                return name in platform_disabled or name in global_disabled
        return name in global_disabled
    except Exception:
        return False


def _find_all_skills(*, skip_disabled: bool = False) -> List[Dict[str, Any]]:
    """递归查找 ~/.hermes/skills/ 及外部目录中的所有技能。

    参数：
        skip_disabled：若为 True，则返回所有技能，忽略禁用状态
            （由 ``hermes skills`` 配置界面使用）。默认 False 会过滤掉
            被禁用的技能。

    返回：
        技能元数据字典列表（name、description、category）。
    """
    from agent.skill_utils import get_external_skills_dirs, iter_skill_index_files

    skills = []
    seen_names: set = set()

    # 只加载一次禁用集合（而不是逐技能加载）
    disabled = set() if skip_disabled else _get_disabled_skill_names()

    # 先扫描本地目录，再扫描外部目录（本地目录优先）
    dirs_to_scan = []
    if SKILLS_DIR.exists():
        dirs_to_scan.append(SKILLS_DIR)
    dirs_to_scan.extend(get_external_skills_dirs())

    for scan_dir in dirs_to_scan:
        for skill_md in iter_skill_index_files(scan_dir, "SKILL.md"):
            if any(part in _EXCLUDED_SKILL_DIRS for part in skill_md.parts):
                continue

            skill_dir = skill_md.parent

            try:
                content = skill_md.read_text(encoding="utf-8")[:4000]
                frontmatter, body = _parse_frontmatter(content)

                if not skill_matches_platform(frontmatter):
                    continue

                if not skill_matches_environment(frontmatter):
                    continue

                name = frontmatter.get("name", skill_dir.name)[:MAX_NAME_LENGTH]
                if name in seen_names:
                    continue
                if name in disabled:
                    continue

                description = frontmatter.get("description", "")
                if not description:
                    for line in body.strip().split("\n"):
                        line = line.strip()
                        if line and not line.startswith("#"):
                            description = line
                            break

                if len(description) > MAX_DESCRIPTION_LENGTH:
                    description = description[:MAX_DESCRIPTION_LENGTH - 3] + "..."

                category = _get_category_from_path(skill_md)

                seen_names.add(name)
                skills.append({
                    "name": name,
                    "description": description,
                    "category": category,
                })

            except (UnicodeDecodeError, PermissionError) as e:
                logger.debug("Failed to read skill file %s: %s", skill_md, e)
                continue
            except Exception as e:
                logger.debug(
                    "Skipping skill at %s: failed to parse: %s", skill_md, e, exc_info=True
                )
                continue

    return skills


def _sort_skills(skills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """保持每个技能列表路径的排序方式一致。"""
    return sorted(skills, key=lambda s: (s.get("category") or "", s["name"]))


def skills_list(category: str = None, task_id: str = None) -> str:
    """
    列出所有可用技能（渐进式披露第 1 层——最少的元数据）。

    只返回 name + description 以尽量减少 token 用量。使用 skill_view() 来
    加载完整内容、标签、关联文件等。

    参数：
        category：可选的分类过滤器（例如 "mlops"）
        task_id：可选的任务标识符，用于探测当前激活的后端

    返回：
        包含最少技能信息（name、description、category）的 JSON 字符串
    """
    try:
        if not SKILLS_DIR.exists():
            SKILLS_DIR.mkdir(parents=True, exist_ok=True)
            return json.dumps(
                {
                    "success": True,
                    "skills": [],
                    "categories": [],
                    "message": f"No skills found. Skills directory created at {display_hermes_home()}/skills/",
                },
                ensure_ascii=False,
            )

        # 查找所有技能
        all_skills = _find_all_skills()

        if not all_skills:
            return json.dumps(
                {
                    "success": True,
                    "skills": [],
                    "categories": [],
                    "message": "No skills found in skills/ directory.",
                },
                ensure_ascii=False,
            )

        # 如果指定了分类则进行过滤
        if category:
            all_skills = [s for s in all_skills if s.get("category") == category]

        # 先按分类再按名称排序
        all_skills = _sort_skills(all_skills)

        # 提取唯一的分类
        categories = sorted(
            {s.get("category") for s in all_skills if s.get("category")}
        )

        return json.dumps(
            {
                "success": True,
                "skills": all_skills,
                "categories": categories,
                "count": len(all_skills),
                "hint": "Use skill_view(name) to see full content, tags, and linked files",
            },
            ensure_ascii=False,
        )

    except Exception as e:
        return tool_error(str(e), success=False)


# ── 插件技能服务 ──────────────────────────────────────────────────


def _serve_plugin_skill(
    skill_md: Path,
    namespace: str,
    bare: str,
    *,
    preprocess: bool = True,
    session_id: str | None = None,
) -> str:
    """读取插件提供的技能，应用安全检查，返回 JSON。"""
    from hermes_cli.plugins import _get_disabled_plugins, get_plugin_manager

    if namespace in _get_disabled_plugins():
        return json.dumps(
            {
                "success": False,
                "error": (
                    f"Plugin '{namespace}' is disabled. "
                    f"Re-enable with: hermes plugins enable {namespace}"
                ),
            },
            ensure_ascii=False,
        )

    try:
        content = skill_md.read_text(encoding="utf-8")
    except Exception as e:
        return json.dumps(
            {"success": False, "error": f"Failed to read skill '{namespace}:{bare}': {e}"},
            ensure_ascii=False,
        )

    parsed_frontmatter: Dict[str, Any] = {}
    try:
        parsed_frontmatter, _ = _parse_frontmatter(content)
    except Exception:
        pass

    if not skill_matches_platform(parsed_frontmatter):
        return json.dumps(
            {
                "success": False,
                "error": f"Skill '{namespace}:{bare}' is not supported on this platform.",
                "readiness_status": SkillReadinessStatus.UNSUPPORTED.value,
            },
            ensure_ascii=False,
        )

    # 注入扫描——记录日志但仍然提供服务（与本地技能行为一致）
    if any(p in content.lower() for p in _INJECTION_PATTERNS):
        logger.warning(
            "Plugin skill '%s:%s' contains patterns that may indicate prompt injection",
            namespace, bare,
        )

    description = str(parsed_frontmatter.get("description", ""))
    if len(description) > MAX_DESCRIPTION_LENGTH:
        description = description[: MAX_DESCRIPTION_LENGTH - 3] + "..."

    # Bundle 上下文横幅——告诉 agent 同属一个 bundle 的兄弟技能
    try:
        siblings = [
            s for s in get_plugin_manager().list_plugin_skills(namespace)
            if s != bare
        ]
        if siblings:
            sib_list = ", ".join(siblings)
            banner = (
                f"[Bundle context: This skill is part of the '{namespace}' plugin.\n"
                f"Sibling skills: {sib_list}.\n"
                f"Use qualified form to invoke siblings (e.g. {namespace}:{siblings[0]}).]\n\n"
            )
        else:
            banner = f"[Bundle context: This skill is part of the '{namespace}' plugin.]\n\n"
    except Exception:
        banner = ""

    rendered_content = content
    if preprocess:
        try:
            from agent.skill_preprocessing import preprocess_skill_content

            rendered_content = preprocess_skill_content(
                content,
                skill_md.parent,
                session_id=session_id,
            )
        except Exception:
            logger.debug(
                "Could not preprocess plugin skill %s:%s", namespace, bare, exc_info=True
            )

    return json.dumps(
        {
            "success": True,
            "name": f"{namespace}:{bare}",
            "content": f"{banner}{rendered_content}" if banner else rendered_content,
            "description": description,
            "linked_files": None,
            "readiness_status": SkillReadinessStatus.AVAILABLE.value,
        },
        ensure_ascii=False,
    )


def skill_view(
    name: str,
    file_path: str = None,
    task_id: str = None,
    preprocess: bool = True,
) -> str:
    """
    查看某个技能的内容，或技能目录中某个具体文件的内容。

    参数：
        name：技能的名称或路径（例如 "axolotl" 或 "03-fine-tuning/axolotl"）。
            像 "plugin:skill" 这样的限定名会解析为插件提供的技能。
        file_path：可选，技能内某个具体文件的路径（例如 "references/api.md"）
        task_id：可选，用于探测当前激活后端的任务标识符
        preprocess：对主技能内容应用已配置的 SKILL.md 模板和内联 shell 渲染。
            内部的 slash/preload 调用方会禁用此项，因为它们会自行渲染技能消息。

    返回：
        包含技能内容或错误信息的 JSON 字符串
    """
    try:
        # 在 ':' 限定名分派之前进行校验，这样 Windows 盘符路径
        # （例如 C:\skills\foo）就不会被重新解释为插件命名空间，
        # 并且包含穿越/绝对路径的名称永远不会到达下面构建 direct_path
        # 的搜索目录拼接逻辑。
        lookup_error = _skill_lookup_path_error(name)
        if lookup_error:
            return json.dumps(
                {
                    "success": False,
                    "error": lookup_error,
                    "hint": "Use a skill name or relative path within the skills directory.",
                },
                ensure_ascii=False,
            )

        local_category_name: str | None = None
        # ── 限定名分派（插件技能）──────────────────
        # 包含 ':' 的名称会路由到插件技能注册表。
        # 裸名称会落到下面已有的扁平树扫描逻辑。
        if ":" in name:
            from agent.skill_utils import is_valid_namespace, parse_qualified_name
            from hermes_cli.plugins import discover_plugins, get_plugin_manager

            namespace, bare = parse_qualified_name(name)
            if not is_valid_namespace(namespace):
                return json.dumps(
                    {
                        "success": False,
                        "error": (
                            f"Invalid namespace '{namespace}' in '{name}'. "
                            f"Namespaces must match [a-zA-Z0-9_-]+."
                        ),
                    },
                    ensure_ascii=False,
                )

            discover_plugins()  # 幂等
            pm = get_plugin_manager()
            plugin_skill_md = pm.find_plugin_skill(name)

            if plugin_skill_md is not None:
                if not plugin_skill_md.exists():
                    # 过期的注册表条目——文件已被外部删除
                    pm.remove_plugin_skill(name)
                    return json.dumps(
                        {
                            "success": False,
                            "error": (
                                f"Skill '{name}' file no longer exists at "
                                f"{plugin_skill_md}. The registry entry has "
                                f"been cleaned up — try again after the "
                                f"plugin is reloaded."
                            ),
                        },
                        ensure_ascii=False,
                    )
                return _serve_plugin_skill(
                    plugin_skill_md,
                    namespace,
                    bare,
                    preprocess=preprocess,
                    session_id=task_id,
                )

            # 插件存在但缺少这个具体技能？
            available = pm.list_plugin_skills(namespace)
            if available:
                return json.dumps(
                    {
                        "success": False,
                        "error": f"Skill '{bare}' not found in plugin '{namespace}'.",
                        "available_skills": [f"{namespace}:{s}" for s in available],
                        "hint": f"The '{namespace}' plugin provides {len(available)} skill(s).",
                    },
                    ensure_ascii=False,
                )
            # 插件本身未找到——落到扁平树扫描逻辑。
            # 分类的本地技能在配置和 gateway 提示中也使用 `category:skill`
            # 形式，因此保留该形式，并在下面的本地扫描中将其转换为
            # 磁盘上的 `category/skill` 路径。
            if bare:
                local_category_name = f"{namespace}/{bare}"

        from agent.skill_utils import get_external_skills_dirs

        # 分类的回退形式（namespace/bare）也会拼接到每个搜索目录上；
        # 由于 `bare` 没有经过命名空间校验，这里重新校验一次。
        if local_category_name:
            lookup_error = _skill_lookup_path_error(local_category_name)
            if lookup_error:
                return json.dumps(
                    {
                        "success": False,
                        "error": lookup_error,
                        "hint": "Use a skill name or relative path within the skills directory.",
                    },
                    ensure_ascii=False,
                )

        # 构建要搜索的所有技能目录列表
        all_dirs = []
        if SKILLS_DIR.exists():
            all_dirs.append(SKILLS_DIR)
        all_dirs.extend(get_external_skills_dirs())

        if not all_dirs:
            return json.dumps(
                {
                    "success": False,
                    "error": "Skills directory does not exist yet. It will be created on first install.",
                },
                ensure_ascii=False,
            )

        skill_dir = None
        skill_md = None

        # 冲突检测：使用每种查找策略（直接路径、按父目录名递归、遗留的
        # 扁平 <name>.md），跨每个目录收集所有候选。如果匹配到多个，则拒绝
        # 并告知调用方——本地技能被同名外部技能悄悄覆盖是一类真实的 bug
        # （`/skills` 显示一个，agent 却加载了另一个），所以我们大声地暴露
        # 它，而不是靠猜测。
        from agent.skill_utils import iter_skill_index_files

        candidates: List[Tuple[Optional[Path], Path]] = []  # (skill_dir, skill_md) —— (技能目录, 技能 md 文件)
        seen_md: set = set()

        def _record(sd: Optional[Path], smd: Path) -> None:
            try:
                key = smd.resolve()
            except Exception:
                key = smd
            if key in seen_md:
                return
            seen_md.add(key)
            candidates.append((sd, smd))

        for search_dir in all_dirs:
            # 策略 1：直接路径（例如 "mlops/axolotl"，或位于目录顶层的裸名 "axolotl"）。
            direct_path = search_dir / name
            if (
                not _is_skill_support_path(direct_path)
                and direct_path.is_dir()
                and (direct_path / "SKILL.md").exists()
            ):
                _record(direct_path, direct_path / "SKILL.md")
            elif direct_path.with_suffix(".md").exists() and not _is_skill_support_path(
                direct_path.with_suffix(".md")
            ):
                _record(None, direct_path.with_suffix(".md"))

            # 策略 1b：针对插件命名空间回退的分类形式
            # （例如，一个没有注册插件的 "myplugin:explore" 名称也会尝试
            # 磁盘路径 "myplugin/explore"）。
            if local_category_name:
                categorized_path = search_dir / local_category_name
                if (
                    not _is_skill_support_path(categorized_path)
                    and categorized_path.is_dir()
                    and (categorized_path / "SKILL.md").exists()
                ):
                    _record(categorized_path, categorized_path / "SKILL.md")
                elif categorized_path.with_suffix(
                    ".md"
                ).exists() and not _is_skill_support_path(
                    categorized_path.with_suffix(".md")
                ):
                    _record(None, categorized_path.with_suffix(".md"))

            # 策略 2：按目录名递归查找（捕获嵌套技能，例如通过裸名调用的
            # "foundations/runtime/explore-codebase"），外加 frontmatter 的
            # `name:` 查找。`skills_list()` 暴露的是 frontmatter 中的 name，
            # 因此 `skill_view(name)` 也必须能接受它，即使磁盘上的目录是一个
            # 更短的分类/别名。
            for found_skill_md in iter_skill_index_files(search_dir, "SKILL.md"):
                if found_skill_md.parent.name == name:
                    _record(found_skill_md.parent, found_skill_md)
                    continue
                try:
                    fm_content = found_skill_md.read_text(encoding="utf-8")
                    fm, _ = _parse_frontmatter(fm_content)
                except Exception:
                    fm = {}
                if fm.get("name") == name:
                    _record(found_skill_md.parent, found_skill_md)

            # 策略 3：目录下任意位置的遗留扁平 <name>.md 文件。
            # 排除技能支持文档：references/templates/assets/scripts 通过
            # skill_view(skill, file_path=...) 加载，不能与同名真实技能发生
            # 遮蔽或冲突。
            for found_md in search_dir.rglob(f"{name}.md"):
                if found_md.name != "SKILL.md" and not _is_skill_support_path(
                    found_md
                ):
                    _record(None, found_md)

        if len(candidates) > 1:
            paths = [str(smd) for _, smd in candidates]
            logging.getLogger(__name__).warning(
                "Skill name collision for '%s': %d candidates — %s",
                name, len(candidates), "; ".join(paths),
            )
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Ambiguous skill name '{name}': {len(candidates)} skills "
                        "match across your local skills dir and external_dirs. "
                        "Refusing to guess — load one explicitly by its categorized path."
                    ),
                    "matches": paths,
                    "hint": (
                        "Pass the full relative path instead of the bare name "
                        "(e.g., 'category/skill-name'), or rename one of the "
                        "colliding skills so each name is unique."
                    ),
                },
                ensure_ascii=False,
            )

        if candidates:
            skill_dir, skill_md = candidates[0]

        if not skill_md or not skill_md.exists():
            available = [s["name"] for s in _sort_skills(_find_all_skills())[:20]]
            return json.dumps(
                {
                    "success": False,
                    "error": f"Skill '{name}' not found.",
                    "available_skills": available,
                    "hint": "Use skills_list to see all available skills",
                },
                ensure_ascii=False,
            )

        # 只读一次文件——下面的平台检查和主要内容都会复用它
        try:
            content = skill_md.read_text(encoding="utf-8")
        except Exception as e:
            return json.dumps(
                {
                    "success": False,
                    "error": f"Failed to read skill '{name}': {e}",
                },
                ensure_ascii=False,
            )

        # 安全性：如果技能来自受信任目录之外则发出警告
        # （本地技能目录 + 配置的 external_dirs 都是受信任的）
        _outside_skills_dir = True
        _trusted_dirs = [SKILLS_DIR.resolve()]
        try:
            _trusted_dirs.extend(d.resolve() for d in all_dirs[1:])
        except Exception:
            pass
        for _td in _trusted_dirs:
            try:
                skill_md.resolve().relative_to(_td)
                _outside_skills_dir = False
                break
            except ValueError:
                continue

        # 安全性：检测常见的提示词注入模式
        # （模式列表位于模块级，名称为 _INJECTION_PATTERNS）
        _content_lower = content.lower()
        _injection_detected = any(p in _content_lower for p in _INJECTION_PATTERNS)

        if _outside_skills_dir or _injection_detected:
            _warnings = []
            if _outside_skills_dir:
                _warnings.append(f"skill file is outside the trusted skills directory (~/.hermes/skills/): {skill_md}")
            if _injection_detected:
                _warnings.append("skill content contains patterns that may indicate prompt injection")
            logging.getLogger(__name__).warning("Skill security warning for '%s': %s", name, "; ".join(_warnings))

        parsed_frontmatter: Dict[str, Any] = {}
        try:
            parsed_frontmatter, _ = _parse_frontmatter(content)
        except Exception:
            parsed_frontmatter = {}

        if not skill_matches_platform(parsed_frontmatter):
            return json.dumps(
                {
                    "success": False,
                    "error": f"Skill '{name}' is not supported on this platform.",
                    "readiness_status": SkillReadinessStatus.UNSUPPORTED.value,
                },
                ensure_ascii=False,
            )

        # 检查该技能是否被用户禁用
        resolved_name = parsed_frontmatter.get("name", skill_md.parent.name)
        if _is_skill_disabled(resolved_name):
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Skill '{resolved_name}' is disabled. "
                        "Enable it with `hermes skills` or inspect the files directly on disk."
                    ),
                },
                ensure_ascii=False,
            )

        # 如果请求了具体的文件路径，则改为读取该文件
        if file_path and skill_dir:
            from tools.path_security import validate_within_dir, has_traversal_component

            # 安全性：防止路径穿越攻击
            if has_traversal_component(file_path):
                return json.dumps(
                    {
                        "success": False,
                        "error": "Path traversal ('..') is not allowed.",
                        "hint": "Use a relative path within the skill directory",
                    },
                    ensure_ascii=False,
                )

            target_file = skill_dir / file_path

            # 安全性：校验解析后的路径是否仍在技能目录内
            traversal_error = validate_within_dir(target_file, skill_dir)
            if traversal_error:
                return json.dumps(
                    {
                        "success": False,
                        "error": traversal_error,
                        "hint": "Use a relative path within the skill directory",
                    },
                    ensure_ascii=False,
                )
            if not target_file.exists():
                # 列出技能目录中的可用文件，按类型组织
                available_files = {
                    "references": [],
                    "templates": [],
                    "assets": [],
                    "scripts": [],
                    "other": [],
                }

                # 扫描所有可读文件
                for f in skill_dir.rglob("*"):
                    if f.is_file() and f.name != "SKILL.md":
                        rel = str(f.relative_to(skill_dir))
                        if rel.startswith("references/"):
                            available_files["references"].append(rel)
                        elif rel.startswith("templates/"):
                            available_files["templates"].append(rel)
                        elif rel.startswith("assets/"):
                            available_files["assets"].append(rel)
                        elif rel.startswith("scripts/"):
                            available_files["scripts"].append(rel)
                        elif f.suffix in {
                            ".md",
                            ".py",
                            ".yaml",
                            ".yml",
                            ".json",
                            ".tex",
                            ".sh",
                        }:
                            available_files["other"].append(rel)

                # 移除空分类
                available_files = {k: v for k, v in available_files.items() if v}

                return json.dumps(
                    {
                        "success": False,
                        "error": f"File '{file_path}' not found in skill '{name}'.",
                        "available_files": available_files,
                        "hint": "Use one of the available file paths listed above",
                    },
                    ensure_ascii=False,
                )

            # 读取文件内容
            try:
                content = target_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                # 二进制文件——改为返回其信息
                return json.dumps(
                    {
                        "success": True,
                        "name": name,
                        "file": file_path,
                        "content": f"[Binary file: {target_file.name}, size: {target_file.stat().st_size} bytes]",
                        "is_binary": True,
                    },
                    ensure_ascii=False,
                )

            return json.dumps(
                {
                    "success": True,
                    "name": name,
                    "file": file_path,
                    "content": content,
                    "file_type": target_file.suffix,
                },
                ensure_ascii=False,
            )

        # 复用上面平台检查时的解析结果
        frontmatter = parsed_frontmatter

        # 如果这是一个基于目录的技能，则获取其参考、模板、资产和脚本文件
        reference_files = []
        template_files = []
        asset_files = []
        script_files = []

        if skill_dir:
            references_dir = skill_dir / "references"
            if references_dir.exists():
                reference_files = [
                    str(f.relative_to(skill_dir)) for f in references_dir.glob("*.md")
                ]

            templates_dir = skill_dir / "templates"
            if templates_dir.exists():
                for ext in [
                    "*.md",
                    "*.py",
                    "*.yaml",
                    "*.yml",
                    "*.json",
                    "*.tex",
                    "*.sh",
                ]:
                    template_files.extend(
                        [
                            str(f.relative_to(skill_dir))
                            for f in templates_dir.rglob(ext)
                        ]
                    )

            # assets/ —— agentskills.io 标准的补充文件目录
            assets_dir = skill_dir / "assets"
            if assets_dir.exists():
                for f in assets_dir.rglob("*"):
                    if f.is_file():
                        asset_files.append(str(f.relative_to(skill_dir)))

            scripts_dir = skill_dir / "scripts"
            if scripts_dir.exists():
                for ext in ["*.py", "*.sh", "*.bash", "*.js", "*.ts", "*.rb"]:
                    script_files.extend(
                        [str(f.relative_to(skill_dir)) for f in scripts_dir.glob(ext)]
                    )

        # 读取 tags/related_skills，保持向后兼容：
        # 先检查 metadata.hermes.*（agentskills.io 约定），再回退到顶层
        hermes_meta = {}
        metadata = frontmatter.get("metadata")
        if isinstance(metadata, dict):
            hermes_meta = metadata.get("hermes", {}) or {}

        tags = _parse_tags(hermes_meta.get("tags") or frontmatter.get("tags", ""))
        related_skills = _parse_tags(
            hermes_meta.get("related_skills") or frontmatter.get("related_skills", "")
        )

        # 构建 linked files 结构，便于清晰发现
        linked_files = {}
        if reference_files:
            linked_files["references"] = reference_files
        if template_files:
            linked_files["templates"] = template_files
        if asset_files:
            linked_files["assets"] = asset_files
        if script_files:
            linked_files["scripts"] = script_files

        try:
            rel_path = str(skill_md.relative_to(SKILLS_DIR))
        except ValueError:
            # 外部技能——使用相对于技能自身父目录的路径
            rel_path = str(skill_md.relative_to(skill_md.parent.parent)) if skill_md.parent.parent else skill_md.name
        skill_name = frontmatter.get(
            "name", skill_md.stem if not skill_dir else skill_dir.name
        )
        legacy_env_vars, _ = _collect_prerequisite_values(frontmatter)
        required_env_vars = _get_required_environment_variables(
            frontmatter, legacy_env_vars
        )
        backend = _get_terminal_backend_name()
        env_snapshot = load_env()
        missing_required_env_vars = [
            e
            for e in required_env_vars
            if not e.get("optional")
            and not _is_env_var_persisted(e["name"], env_snapshot)
        ]
        capture_result = _capture_required_environment_variables(
            skill_name,
            missing_required_env_vars,
        )
        if missing_required_env_vars:
            env_snapshot = load_env()
        remaining_missing_required_envs = _remaining_required_environment_names(
            required_env_vars,
            capture_result,
            env_snapshot=env_snapshot,
        )
        setup_needed = bool(remaining_missing_required_envs)

        # 注册可用的技能环境变量，使其能透传到沙箱执行环境
        # （execute_code、terminal）。只有实际已设置的变量才会被注册——
        # 缺失的会以 setup_needed 的形式上报。
        available_env_names = [
            e["name"]
            for e in required_env_vars
            if e["name"] not in remaining_missing_required_envs
        ]
        if available_env_names:
            try:
                from tools.env_passthrough import register_env_passthrough

                register_env_passthrough(available_env_names)
            except Exception:
                logger.debug(
                    "Could not register env passthrough for skill %s",
                    skill_name,
                    exc_info=True,
                )

        # 注册凭据文件，以便挂载到远程沙箱（Modal、Docker）。
        # 主机上存在的文件会被注册；缺失的会加入 setup_needed 指示项。
        required_cred_files_raw = frontmatter.get("required_credential_files", [])
        if not isinstance(required_cred_files_raw, list):
            required_cred_files_raw = []
        missing_cred_files: list = []
        if required_cred_files_raw:
            try:
                from tools.credential_files import register_credential_files

                missing_cred_files = register_credential_files(required_cred_files_raw)
                if missing_cred_files:
                    setup_needed = True
            except Exception:
                logger.debug(
                    "Could not register credential files for skill %s",
                    skill_name,
                    exc_info=True,
                )

        rendered_content = content
        if preprocess:
            try:
                from agent.skill_preprocessing import preprocess_skill_content

                rendered_content = preprocess_skill_content(
                    content,
                    skill_dir,
                    session_id=task_id,
                )
            except Exception:
                logger.debug(
                    "Could not preprocess skill content for %s", skill_name, exc_info=True
                )

        result = {
            "success": True,
            "name": skill_name,
            "description": frontmatter.get("description", ""),
            "tags": tags,
            "related_skills": related_skills,
            "content": rendered_content,
            "path": rel_path,
            "skill_dir": str(skill_dir) if skill_dir else None,
            "linked_files": linked_files if linked_files else None,
            "usage_hint": "To view linked files, call skill_view(name, file_path) where file_path is e.g. 'references/api.md' or 'assets/config.yaml'"
            if linked_files
            else None,
            "required_environment_variables": required_env_vars,
            "required_commands": [],
            "missing_required_environment_variables": remaining_missing_required_envs,
            "missing_credential_files": missing_cred_files,
            "missing_required_commands": [],
            "setup_needed": setup_needed,
            "setup_skipped": capture_result["setup_skipped"],
            "readiness_status": SkillReadinessStatus.SETUP_NEEDED.value
            if setup_needed
            else SkillReadinessStatus.AVAILABLE.value,
        }

        setup_help = next((e["help"] for e in required_env_vars if e.get("help")), None)
        if setup_help:
            result["setup_help"] = setup_help

        if capture_result["gateway_setup_hint"]:
            result["gateway_setup_hint"] = capture_result["gateway_setup_hint"]

        if setup_needed:
            missing_items = [
                f"env ${env_name}" for env_name in remaining_missing_required_envs
            ] + [
                f"file {path}" for path in missing_cred_files
            ]
            setup_note = _build_setup_note(
                SkillReadinessStatus.SETUP_NEEDED,
                missing_items,
                setup_help,
            )
            if backend in _REMOTE_ENV_BACKENDS and setup_note:
                setup_note = f"{setup_note} {backend.upper()}-backed skills need these requirements available inside the remote environment as well."
            if setup_note:
                result["setup_note"] = setup_note

        # 当存在时，呈现 agentskills.io 的可选字段
        if frontmatter.get("compatibility"):
            result["compatibility"] = frontmatter["compatibility"]
        if isinstance(metadata, dict):
            result["metadata"] = metadata

        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        return tool_error(str(e), success=False)




if __name__ == "__main__":
    """测试技能工具"""
    print("🎯 Skills Tool Test")
    print("=" * 60)

    # 测试列出技能
    print("\n📋 Listing all skills:")
    result = json.loads(skills_list())
    if result["success"]:
        print(
            f"Found {result['count']} skills in {len(result.get('categories', []))} categories"
        )
        print(f"Categories: {result.get('categories', [])}")
        print("\nFirst 10 skills:")
        for skill in result["skills"][:10]:
            cat = f"[{skill['category']}] " if skill.get("category") else ""
            print(f"  • {cat}{skill['name']}: {skill['description'][:60]}...")
    else:
        print(f"Error: {result['error']}")

    # 测试查看某个技能
    print("\n📖 Viewing skill 'axolotl':")
    result = json.loads(skill_view("axolotl"))
    if result["success"]:
        print(f"Name: {result['name']}")
        print(f"Description: {result.get('description', 'N/A')[:100]}...")
        print(f"Content length: {len(result['content'])} chars")
        if result.get("linked_files"):
            print(f"Linked files: {result['linked_files']}")
    else:
        print(f"Error: {result['error']}")

    # 测试查看某个参考文件
    print("\n📄 Viewing reference file 'axolotl/references/dataset-formats.md':")
    result = json.loads(skill_view("axolotl", "references/dataset-formats.md"))
    if result["success"]:
        print(f"File: {result['file']}")
        print(f"Content length: {len(result['content'])} chars")
        print(f"Preview: {result['content'][:150]}...")
    else:
        print(f"Error: {result['error']}")


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

SKILLS_LIST_SCHEMA = {
    "name": "skills_list",
    "description": "List available skills (name + description). Use skill_view(name) to load full content.",
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Optional category filter to narrow results",
            }
        },
        "required": [],
    },
}

SKILL_VIEW_SCHEMA = {
    "name": "skill_view",
    "description": "Skills allow for loading information about specific tasks and workflows, as well as scripts and templates. Load a skill's full content or access its linked files (references, templates, scripts). First call returns SKILL.md content plus a 'linked_files' dict showing available references/templates/scripts. To access those, call again with file_path parameter.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The skill name (use skills_list to see available skills). For plugin-provided skills, use the qualified form 'plugin:skill' (e.g. 'superpowers:writing-plans').",
            },
            "file_path": {
                "type": "string",
                "description": "OPTIONAL: Path to a linked file within the skill (e.g., 'references/api.md', 'templates/config.yaml', 'scripts/validate.py'). Omit to get the main SKILL.md content.",
            },
        },
        "required": ["name"],
    },
}

registry.register(
    name="skills_list",
    toolset="skills",
    schema=SKILLS_LIST_SCHEMA,
    handler=lambda args, **kw: skills_list(
        category=args.get("category"), task_id=kw.get("task_id")
    ),
    check_fn=check_skills_requirements,
    emoji="📚",
)
def _skill_view_with_bump(args, **kw):
    """调用 skill_view，成功后增加 view_count。尽力而为：遥测失败永远不会
    导致工具调用中断。"""
    name = args.get("name", "")
    result = skill_view(
        name, file_path=args.get("file_path"), task_id=kw.get("task_id")
    )
    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict) and parsed.get("success"):
            # 当载荷中存在时，使用解析后的技能名——
            # 限定名（"plugin:skill"）会返回规范名称。
            resolved = parsed.get("name") or name
            if resolved:
                from tools.skill_usage import bump_use, bump_view
                bump_view(str(resolved))
                # 一次 skill_view 工具调用意味着 agent 正在主动加载该技能
                # 并准备执行——这算作一次使用，而不仅仅是浏览/查看。
                # Curator 的过期计时器以 last_used_at 为依据（见 agent/curator.py）。
                bump_use(str(resolved))
    except Exception:
        pass
    return result


registry.register(
    name="skill_view",
    toolset="skills",
    schema=SKILL_VIEW_SCHEMA,
    handler=_skill_view_with_bump,
    check_fn=check_skills_requirements,
    emoji="📚",
)
