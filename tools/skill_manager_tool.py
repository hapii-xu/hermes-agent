#!/usr/bin/env python3
"""
Skill Manager Tool -- 代理管理的技能创建与编辑

允许代理创建、更新和删除技能，把成功的做法沉淀为可复用的过程性
知识。新技能会创建到 ~/.hermes/skills/ 下。已有技能（内置、hub
安装或用户创建的）无论位于何处都可被修改或删除。

技能是代理的过程性记忆：它们记录的是*如何完成某类特定任务*，
基于已被验证的经验。通用记忆（MEMORY.md、USER.md）是宽泛、
声明式的；技能则是聚焦、可执行的。

操作：
  create     -- 创建新技能（SKILL.md + 目录结构）
  edit       -- 替换某个用户技能的 SKILL.md 内容（整体重写）
  patch      -- 在 SKILL.md 或任意辅助文件中做定向查找替换
  delete     -- 彻底移除某个用户技能
  write_file -- 新增/覆盖辅助文件（参考、模板、脚本、素材）
  remove_file-- 从用户技能中移除某个辅助文件

用户技能的目录布局：
    ~/.hermes/skills/
    ├── my-skill/
    │   ├── SKILL.md
    │   ├── references/
    │   ├── templates/
    │   ├── scripts/
    │   └── assets/
    └── category-name/
        └── another-skill/
            └── SKILL.md
"""

import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from hermes_constants import get_hermes_home, display_hermes_home
from typing import Dict, Any, List, Optional, Tuple

from utils import atomic_replace, is_truthy_value
from hermes_cli.config import cfg_get

logger = logging.getLogger(__name__)

# 导入安全扫描器 —— 外部 hub 安装总是会扫描；代理创建的技能仅在
# skills.guard_agent_created 开启时才会被扫描。
try:
    from tools.skills_guard import scan_skill, should_allow_install, format_scan_report
    _GUARD_AVAILABLE = True
except ImportError:
    _GUARD_AVAILABLE = False


def _guard_agent_created_enabled() -> bool:
    """从配置读取 skills.guard_agent_created（默认 False）。

    默认关闭，因为代理本就可以通过 terminal() 无门槛地执行同样的
    代码路径，扫描只会增加摩擦而无实质安全收益。想要「双保险」的
    用户可以通过 `hermes config set skills.guard_agent_created true`
    开启。
    """
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        return is_truthy_value(
            cfg_get(cfg, "skills", "guard_agent_created"),
            default=False,
        )
    except Exception:
        return False


def _security_scan_skill(skill_dir: Path) -> Optional[str]:
    """在写入后扫描技能目录。若被拦截则返回错误字符串，否则返回 None。

    当 skills.guard_agent_created 关闭时（默认）为空操作。
    """
    if not _GUARD_AVAILABLE:
        return None
    if not _guard_agent_created_enabled():
        return None
    try:
        result = scan_skill(skill_dir, source="agent-created")
        allowed, reason = should_allow_install(result)
        if allowed is False:
            report = format_scan_report(result)
            return f"Security scan blocked this skill ({reason}):\n{report}"
        if allowed is None:
            # "ask" 判定 —— 对于代理创建的技能，意味着检测到了危险
            # 内容。作为错误返回，以便代理在移除被标记的内容后重试。
            report = format_scan_report(result)
            logger.warning("Agent-created skill blocked (dangerous findings): %s", reason)
            return f"Security scan blocked this skill ({reason}):\n{report}"
    except Exception as e:
        logger.warning("Security scan failed for %s: %s", skill_dir, e, exc_info=True)
    return None

import yaml


# 所有技能都位于 ~/.hermes/skills/（唯一可信源）
HERMES_HOME = get_hermes_home()
SKILLS_DIR = HERMES_HOME / "skills"

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024


def _containing_skills_root(skill_path: Path) -> Path:
    """返回包含 ``skill_path`` 的技能根目录（本地或 external_dirs 条目）。
    若无匹配则回退到本地 ``SKILLS_DIR``（防御性处理 —— 调用方理应已
    通过 ``_find_skill`` 定位到该技能）。
    """
    from agent.skill_utils import get_all_skills_dirs

    try:
        resolved = skill_path.resolve()
    except OSError:
        resolved = skill_path

    for root in get_all_skills_dirs():
        try:
            resolved.relative_to(root.resolve())
            return root
        except (ValueError, OSError):
            continue
    return SKILLS_DIR


def _is_path_redirect(path: Path) -> bool:
    """当 ``path`` 是符号链接或（Windows 上的）目录联接（junction）时返回 True。

    这两种形式都可能让被污染的技能树把随后的 ``shutil.rmtree``
    重定向到技能根之外的内容。``is_junction`` 仅在 Python 3.12+ 的
    Windows 上存在；用 ``hasattr`` 做条件判断。
    """
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return False


def _validate_delete_target(skill_dir: Path) -> Optional[str]:
    """在 ``_delete_skill`` 中执行 ``shutil.rmtree(skill_dir)`` 前的最后一道防线。

    ``_find_skill`` 已经把 ``skill_dir`` 限定在遍历技能根目录时发现的
    真实 ``SKILL.md`` 父目录上，因此代理无法像 Kilo Code 的 HTTP
    接口那样注入任意路径（他们的 issue #11227：一个内置技能哨兵
    被解析为服务器当前工作目录，递归删除把用户整个工作目录清空）。
    这是面向代理的 ``skill_manage`` 删除路径上对等的纵深防御：
    即使发现逻辑或被污染的技能树交给我们一个错误目录，也绝不递归
    删除

      1. 不严格位于某个已知技能根*之内*的路径，
      2. 技能根本身（会清空所有已安装技能），或
      3. 经由符号链接/联接到达的目录（``rmtree`` 会顺着它进入技能树
         之外的内容）。

    返回错误字符串表示拒绝；删除安全时返回 ``None``。
    """
    from agent.skill_utils import get_all_skills_dirs

    # (3) 拒绝技能目录自身的符号链接/联接重定向。
    if _is_path_redirect(skill_dir):
        return (
            f"Refusing to delete '{skill_dir}': the skill directory is a "
            f"symlink/junction. Remove the link target manually if intended."
        )

    try:
        resolved = skill_dir.resolve()
    except OSError as exc:
        return f"Refusing to delete '{skill_dir}': could not resolve path ({exc})."

    roots = []
    for root in get_all_skills_dirs():
        try:
            roots.append(root.resolve())
        except OSError:
            continue

    for root in roots:
        # (2) 绝不 rmtree 技能根本身。
        if resolved == root:
            return (
                f"Refusing to delete '{skill_dir}': resolves to the skills root "
                f"itself, which would remove every installed skill."
            )
        # (1) 必须严格位于某个已知根之内。
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        if rel.parts:  # 至少在根之下有一层目录
            return None

    return (
        f"Refusing to delete '{skill_dir}': path does not resolve inside any "
        f"known skills root."
    )


def _pinned_guard(name: str) -> Optional[str]:
    """若 *name* 被固定（pinned）则返回拒绝消息，否则返回 None。

    固定保护技能免遭**删除** —— 包括 curator 的自动归档流程和代理的
    ``skill_manage(action="delete")`` 工具调用。代理仍可对已固定的技能
    进行 patch/edit；固定只防止不可恢复的丢失，不阻止内容演进。

    尽力而为：若 sidecar 文件不可读，则放行删除，而不是因为损坏的
    遥测文件而阻塞。
    """
    try:
        from tools import skill_usage
        rec = skill_usage.get_record(name)
        if rec.get("pinned"):
            return (
                f"Skill '{name}' is pinned and cannot be deleted by "
                f"skill_manage. Ask the user to run "
                f"`hermes curator unpin {name}` if they want to delete it. "
                f"Patches and edits are allowed on pinned skills; only "
                f"deletion is blocked."
            )
    except Exception:
        logger.debug("pinned-guard lookup failed for %s", name, exc_info=True)
    return None


MAX_SKILL_CONTENT_CHARS = 100_000   # 按 2.75 字符/折算约 36k token
MAX_SKILL_FILE_BYTES = 1_048_576    # 每个辅助文件 1 MiB

# 技能名允许的字符（文件系统安全、URL 友好）
VALID_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]*$')

# write_file/remove_file 允许的子目录
ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}


# =============================================================================
# 校验辅助函数
# =============================================================================

def _validate_name(name: str) -> Optional[str]:
    """校验技能名。返回错误消息；合法时返回 None。"""
    if not name:
        return "Skill name is required."
    if len(name) > MAX_NAME_LENGTH:
        return f"Skill name exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(name):
        return (
            f"Invalid skill name '{name}'. Use lowercase letters, numbers, "
            f"hyphens, dots, and underscores. Must start with a letter or digit."
        )
    return None


def _validate_category(category: Optional[str]) -> Optional[str]:
    """校验可选的分类名，它将被用作单个目录段。"""
    if category is None:
        return None
    if not isinstance(category, str):
        return "Category must be a string."

    category = category.strip()
    if not category:
        return None
    if "/" in category or "\\" in category:
        return (
            f"Invalid category '{category}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores. Categories must be a single directory name."
        )
    if len(category) > MAX_NAME_LENGTH:
        return f"Category exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(category):
        return (
            f"Invalid category '{category}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores. Categories must be a single directory name."
        )
    return None


def _validate_frontmatter(content: str) -> Optional[str]:
    """
    校验 SKILL.md 内容是否带有正确的 frontmatter 和必填字段。
    返回错误消息；合法时返回 None。
    """
    if not content.strip():
        return "Content cannot be empty."

    if not content.startswith("---"):
        return "SKILL.md must start with YAML frontmatter (---). See existing skills for format."

    end_match = re.search(r'\n---\s*\n', content[3:])
    if not end_match:
        return "SKILL.md frontmatter is not closed. Ensure you have a closing '---' line."

    yaml_content = content[3:end_match.start() + 3]

    try:
        parsed = yaml.safe_load(yaml_content)
    except yaml.YAMLError as e:
        return f"YAML frontmatter parse error: {e}"

    if not isinstance(parsed, dict):
        return "Frontmatter must be a YAML mapping (key: value pairs)."

    if "name" not in parsed:
        return "Frontmatter must include 'name' field."
    if "description" not in parsed:
        return "Frontmatter must include 'description' field."
    if len(str(parsed["description"])) > MAX_DESCRIPTION_LENGTH:
        return f"Description exceeds {MAX_DESCRIPTION_LENGTH} characters."

    body = content[end_match.end() + 3:].strip()
    if not body:
        return "SKILL.md must have content after the frontmatter (instructions, procedures, etc.)."

    return None


def _validate_content_size(content: str, label: str = "SKILL.md") -> Optional[str]:
    """检查内容是否超出代理写入的字符上限。

    返回错误消息；在范围内则返回 None。
    """
    if len(content) > MAX_SKILL_CONTENT_CHARS:
        return (
            f"{label} content is {len(content):,} characters "
            f"(limit: {MAX_SKILL_CONTENT_CHARS:,}). "
            f"Consider splitting into a smaller SKILL.md with supporting files "
            f"in references/ or templates/."
        )
    return None


def _resolve_skill_dir(name: str, category: str = None) -> Path:
    """为新技能构建目录路径，可选地置于某个分类下。"""
    if category:
        return SKILLS_DIR / category / name
    return SKILLS_DIR / name


def _find_skill(name: str) -> Optional[Dict[str, Any]]:
    """
    跨所有技能目录按名称查找技能。

    先搜索本地技能目录（~/.hermes/skills/），再搜索通过
    skills.external_dirs 配置的外部目录。返回
    {"path": Path} 或 None。
    """
    from agent.skill_utils import get_all_skills_dirs, is_excluded_skill_path
    for skills_dir in get_all_skills_dirs():
        if not skills_dir.exists():
            continue
        for skill_md in skills_dir.rglob("SKILL.md"):
            if is_excluded_skill_path(skill_md):
                continue
            if skill_md.parent.name == name:
                return {"path": skill_md.parent}
    return None


def _find_skill_in_other_profiles(name: str) -> List[Tuple[str, Path]]:
    """在其它 Hermes 配置文件的 SKILL.md 中查找 ``name``。

    返回 ``(profile_name, skill_dir)`` 对的列表。用于让「技能 X 未
    找到」错误能解释清楚用户正在编辑错误的配置文件。当没有任何其它
    配置文件拥有该技能时返回空列表（或配置文件发现失败时也返回空
    ——静默失败，调用方会回退到普通的「未找到」错误）。
    """
    matches: List[Tuple[str, Path]] = []
    try:
        from hermes_constants import get_default_hermes_root
        from agent.skill_utils import is_excluded_skill_path
    except Exception:
        return matches

    try:
        root = get_default_hermes_root()
    except Exception:
        return matches

    # 收集除已在 _find_skill() 中搜索过的那个 SKILLS_DIR 之外的所有
    # 配置文件的 (profile_name, skills_dir)。
    active_dir = SKILLS_DIR.resolve() if SKILLS_DIR.exists() else SKILLS_DIR
    candidates: List[Tuple[str, Path]] = []

    # 默认配置文件（~/.hermes/skills）—— 仅在当前活动配置文件非默认时考虑。
    default_skills = root / "skills"
    try:
        if default_skills.resolve() != active_dir:
            candidates.append(("default", default_skills))
    except (OSError, RuntimeError):
        pass

    # 所有命名配置文件（~/.hermes/profiles/*/skills）
    profiles_root = root / "profiles"
    if profiles_root.is_dir():
        try:
            for entry in profiles_root.iterdir():
                if not entry.is_dir():
                    continue
                pskills = entry / "skills"
                try:
                    if pskills.resolve() == active_dir:
                        continue
                except (OSError, RuntimeError):
                    continue
                candidates.append((entry.name, pskills))
        except OSError:
            pass

    for profile_name, skills_dir in candidates:
        if not skills_dir.is_dir():
            continue
        try:
            for skill_md in skills_dir.rglob("SKILL.md"):
                if is_excluded_skill_path(skill_md):
                    continue
                if skill_md.parent.name == name:
                    matches.append((profile_name, skill_md.parent))
                    break  # 每个配置文件匹配到一个就够
        except OSError:
            continue
    return matches


def _skill_not_found_error(name: str, suffix: str = "") -> str:
    """构造一个「技能未找到」错误，列出持有该技能的其它配置文件，
    以便代理识别配置文件作用域错误。

    若 ``suffix`` 存在，则追加在跨配置文件提示之后
    （例如 ``" Create it first with action='create'."``）。
    """
    from agent.file_safety import _resolve_active_profile_name
    active = _resolve_active_profile_name()
    base = f"Skill '{name}' not found in active profile '{active}'."

    others = _find_skill_in_other_profiles(name)
    if others:
        if len(others) == 1:
            other_profile, other_path = others[0]
            base += (
                f" A skill by that name exists in profile "
                f"'{other_profile}' ({other_path}). To edit a skill in "
                f"another profile, switch profiles (`hermes -p "
                f"{other_profile}`) or operate via explicit file tools "
                f"with ``cross_profile=True``."
            )
        else:
            names = ", ".join(f"'{p}'" for p, _ in others)
            base += (
                f" Skills by that name exist in other profiles: {names}. "
                f"Switch profiles (`hermes -p <name>`) to edit there, or "
                f"operate via explicit file tools with ``cross_profile=True``."
            )
    else:
        base += " Use skills_list() to see available skills."

    if suffix:
        base += suffix
    return base


def _validate_file_path(file_path: str) -> Optional[str]:
    """
    校验 write_file/remove_file 的文件路径。
    必须位于某个允许的子目录之下，且不得逃逸出技能目录。
    """
    from tools.path_security import has_traversal_component

    if not file_path:
        return "file_path is required."

    normalized = Path(file_path)

    # 防止路径穿越（在任何白名单检查之前做，以免下面 SKILL.md 的
    # 例外被含有穿越片段的路径触及）。
    if has_traversal_component(file_path):
        return "Path traversal ('..') is not allowed."

    # SKILL.md 是规范的技能文件，位于技能根目录而非某个允许的
    # 子目录下。接受它的两种自然写法 —— 'SKILL.md' 和
    # '<skill-name>/SKILL.md' —— 以便调用方能定位主文件。上方的
    # 穿越保护仍然适用，因此这不会逃逸。
    if normalized.parts and normalized.name == "SKILL.md":
        if len(normalized.parts) == 1 or len(normalized.parts) == 2:
            return None

    # 必须位于某个允许的子目录之下
    if not normalized.parts or normalized.parts[0] not in ALLOWED_SUBDIRS:
        allowed = ", ".join(sorted(ALLOWED_SUBDIRS))
        return f"File must be under one of: {allowed}. Got: '{file_path}'"

    # 必须有文件名（而不能只是一个目录）
    if len(normalized.parts) < 2:
        return f"Provide a file path, not just a directory. Example: '{normalized.parts[0]}/myfile.md'"

    return None


def _resolve_skill_target(skill_dir: Path, file_path: str) -> Tuple[Optional[Path], Optional[str]]:
    """解析辅助文件路径并确保它停留在技能目录之内。"""
    from tools.path_security import validate_within_dir

    target = skill_dir / file_path
    error = validate_within_dir(target, skill_dir)
    if error:
        return None, error
    return target, None


def _atomic_write_text(file_path: Path, content: str, encoding: str = "utf-8") -> None:
    """
    以原子方式把文本内容写入文件。

    在同目录下使用临时文件，并用 os.replace() 切换，确保目标文件
    永远不会在进程崩溃或被打断时处于半写入状态。

    参数：
        file_path: 目标文件路径
        content: 要写入的内容
        encoding: 文本编码（默认：utf-8）
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=str(file_path.parent),
        prefix=f".{file_path.name}.tmp.",
        suffix="",
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        atomic_replace(temp_path, file_path)
    except Exception:
        # 出错时清理临时文件
        try:
            os.unlink(temp_path)
        except OSError:
            logger.error("Failed to remove temporary file %s during atomic write", temp_path, exc_info=True)
        raise


# =============================================================================
# 核心动作
# =============================================================================

def _create_skill(name: str, content: str, category: str = None) -> Dict[str, Any]:
    """用给定的 SKILL.md 内容创建一个新用户技能。"""
    # 校验名称
    err = _validate_name(name)
    if err:
        return {"success": False, "error": err}

    err = _validate_category(category)
    if err:
        return {"success": False, "error": err}

    # 校验内容
    err = _validate_frontmatter(content)
    if err:
        return {"success": False, "error": err}

    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    # 检查跨所有目录的名称冲突
    existing = _find_skill(name)
    if existing:
        return {
            "success": False,
            "error": f"A skill named '{name}' already exists at {existing['path']}."
        }

    # 创建技能目录
    skill_dir = _resolve_skill_dir(name, category)
    skill_dir.mkdir(parents=True, exist_ok=True)

    # 原子地写入 SKILL.md
    skill_md = skill_dir / "SKILL.md"
    _atomic_write_text(skill_md, content)

    # 安全扫描 —— 被拦截则回滚
    scan_error = _security_scan_skill(skill_dir)
    if scan_error:
        shutil.rmtree(skill_dir, ignore_errors=True)
        return {"success": False, "error": scan_error}

    # 从 frontmatter 提取 description，用于详细通知
    _desc = ""
    try:
        _fm_end = re.search(r'\n---\s*\n', content[3:])
        if _fm_end:
            _parsed = yaml.safe_load(content[3:_fm_end.start() + 3])
            _desc = str(_parsed.get("description", ""))[:120]
    except Exception:
        pass

    result = {
        "success": True,
        "message": f"Skill '{name}' created.",
        "path": str(skill_dir.relative_to(SKILLS_DIR)),
        "skill_md": str(skill_md),
        "_change": {"description": _desc},
    }
    if category:
        result["category"] = category
    result["hint"] = (
        "To add reference files, templates, or scripts, use "
        "skill_manage(action='write_file', name='{}', file_path='references/example.md', file_content='...')".format(name)
    )
    return result


def _edit_skill(name: str, content: str) -> Dict[str, Any]:
    """替换任意已有技能的 SKILL.md（整体重写）。"""
    err = _validate_frontmatter(content)
    if err:
        return {"success": False, "error": err}

    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(name)}

    skill_md = existing["path"] / "SKILL.md"
    # 备份原始内容以便回滚
    original_content = skill_md.read_text(encoding="utf-8") if skill_md.exists() else None
    _atomic_write_text(skill_md, content)

    # 安全扫描 —— 被拦截则回滚
    scan_error = _security_scan_skill(existing["path"])
    if scan_error:
        if original_content is not None:
            _atomic_write_text(skill_md, original_content)
        return {"success": False, "error": scan_error}

    # 从新内容提取 description，用于详细通知
    _desc = ""
    try:
        _fm_end = re.search(r'\n---\s*\n', content[3:])
        if _fm_end:
            _parsed = yaml.safe_load(content[3:_fm_end.start() + 3])
            _desc = str(_parsed.get("description", ""))[:120]
    except Exception:
        pass

    return {
        "success": True,
        "message": f"Skill '{name}' updated (full rewrite).",
        "path": str(existing["path"]),
        "_change": {"description": _desc},
    }


def _patch_skill(
    name: str,
    old_string: str,
    new_string: str,
    file_path: str = None,
    replace_all: bool = False,
) -> Dict[str, Any]:
    """在某个技能文件中做定向查找替换。

    默认作用于 SKILL.md。使用 file_path 改为 patch 某个辅助文件。
    除非 replace_all 为 True，否则要求唯一匹配。
    """
    if not old_string:
        return {"success": False, "error": "old_string is required for 'patch'."}
    if new_string is None:
        return {"success": False, "error": "new_string is required for 'patch'. Use an empty string to delete matched text."}

    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(name)}

    skill_dir = existing["path"]

    if file_path:
        # 正在 patch 某个辅助文件
        err = _validate_file_path(file_path)
        if err:
            return {"success": False, "error": err}
        target, err = _resolve_skill_target(skill_dir, file_path)
        if err:
            return {"success": False, "error": err}
    else:
        # 正在 patch SKILL.md
        target = skill_dir / "SKILL.md"

    if not target.exists():
        return {"success": False, "error": f"File not found: {target.relative_to(skill_dir)}"}

    content = target.read_text(encoding="utf-8")

    # 使用与文件 patch 工具相同的模糊匹配引擎。
    # 它能处理空白规范化、缩进差异、转义序列以及块锚点匹配 ——
    # 让代理免于因细微格式不符而精确匹配失败。
    from tools.fuzzy_match import fuzzy_find_and_replace

    new_content, match_count, _strategy, match_error = fuzzy_find_and_replace(
        content, old_string, new_string, replace_all
    )
    if match_error:
        # 展示文件的一小段预览，便于模型自行纠正
        preview = content[:500] + ("..." if len(content) > 500 else "")
        err_msg = match_error
        try:
            from tools.fuzzy_match import format_no_match_hint
            err_msg += format_no_match_hint(match_error, match_count, old_string, content)
        except Exception:
            pass
        return {
            "success": False,
            "error": err_msg,
            "file_preview": preview,
        }

    # 对结果检查大小限制
    target_label = "SKILL.md" if not file_path else file_path
    err = _validate_content_size(new_content, label=target_label)
    if err:
        return {"success": False, "error": err}

    # 若 patch 的是 SKILL.md，校验 frontmatter 是否仍然完整
    if not file_path:
        err = _validate_frontmatter(new_content)
        if err:
            return {
                "success": False,
                "error": f"Patch would break SKILL.md structure: {err}",
            }

    original_content = content  # 用于回滚
    _atomic_write_text(target, new_content)

    # 安全扫描 —— 被拦截则回滚
    scan_error = _security_scan_skill(skill_dir)
    if scan_error:
        _atomic_write_text(target, original_content)
        return {"success": False, "error": scan_error}

    result = {
        "success": True,
        "message": f"Patched {'SKILL.md' if not file_path else file_path} in skill '{name}' ({match_count} replacement{'s' if match_count > 1 else ''}).",
    }
    # 包含变更预览，用于详细通知
    result["_change"] = {
        "old": old_string[:200] + ("…" if len(old_string) > 200 else ""),
        "new": new_string[:200] + ("…" if len(new_string) > 200 else ""),
    }
    return result


def _delete_skill(name: str, absorbed_into: Optional[str] = None) -> Dict[str, Any]:
    """删除一个技能。

    ``absorbed_into`` 用于声明意图：
      - ``None`` / 缺省  → 调用方未声明（旧路径 / 非 curator 路径）；
        出于向后兼容予以接受，但会记录一条警告，因为 curator 的分类
        流水线在没有它的情况下无法区分「合并」与「修剪」。
      - ``""`` （空字符串）→ 显式表示「真正被修剪，没有转发目标」。
      - ``"<skill-name>"``  → 内容已被吸收进该伞形技能；目标必须在
        磁盘上存在。在此处校验，模型无法谎称一个不存在的伞形技能。
    """
    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(name)}

    pinned_err = _pinned_guard(name)
    if pinned_err:
        return {"success": False, "error": pinned_err}

    # 声明非空时校验 absorbed_into 目标
    if absorbed_into is not None and isinstance(absorbed_into, str) and absorbed_into.strip():
        target_name = absorbed_into.strip()
        if target_name == name:
            return {
                "success": False,
                "error": f"absorbed_into='{target_name}' cannot equal the skill being deleted.",
            }
        target = _find_skill(target_name)
        if not target:
            return {
                "success": False,
                "error": (
                    f"absorbed_into='{target_name}' does not exist. "
                    f"Create or patch the umbrella skill first, then retry the delete."
                ),
            }

    skill_dir = existing["path"]
    skills_root = _containing_skills_root(skill_dir)

    # 递归删除前的纵深防御（移植自 Kilo Code #11240）。
    unsafe = _validate_delete_target(skill_dir)
    if unsafe:
        return {"success": False, "error": unsafe}

    shutil.rmtree(skill_dir)

    # 清理空的分类目录（不要移除技能根本身）
    parent = skill_dir.parent
    if parent != skills_root and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()

    message = f"Skill '{name}' deleted."
    if absorbed_into is not None and isinstance(absorbed_into, str) and absorbed_into.strip():
        message += f" Content absorbed into '{absorbed_into.strip()}'."

    return {
        "success": True,
        "message": message,
    }


def _write_file(name: str, file_path: str, file_content: str) -> Dict[str, Any]:
    """在任意技能目录中新增或覆盖辅助文件。"""
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}

    if not file_content and file_content != "":
        return {"success": False, "error": "file_content is required."}

    # 检查大小限制
    content_bytes = len(file_content.encode("utf-8"))
    if content_bytes > MAX_SKILL_FILE_BYTES:
        return {
            "success": False,
            "error": (
                f"File content is {content_bytes:,} bytes "
                f"(limit: {MAX_SKILL_FILE_BYTES:,} bytes / 1 MiB). "
                f"Consider splitting into smaller files."
            ),
        }
    err = _validate_content_size(file_content, label=file_path)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(name, " Create it first with action='create'.")}

    target, err = _resolve_skill_target(existing["path"], file_path)
    if err:
        return {"success": False, "error": err}
    target.parent.mkdir(parents=True, exist_ok=True)
    # 备份以便回滚
    original_content = target.read_text(encoding="utf-8") if target.exists() else None
    _atomic_write_text(target, file_content)

    # 安全扫描 —— 被拦截则回滚
    scan_error = _security_scan_skill(existing["path"])
    if scan_error:
        if original_content is not None:
            _atomic_write_text(target, original_content)
        else:
            target.unlink(missing_ok=True)
        return {"success": False, "error": scan_error}

    return {
        "success": True,
        "message": f"File '{file_path}' written to skill '{name}'.",
        "path": str(target),
    }


def _remove_file(name: str, file_path: str) -> Dict[str, Any]:
    """从任意技能目录中移除辅助文件。"""
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name)
    if not existing:
        return {"success": False, "error": _skill_not_found_error(name)}

    skill_dir = existing["path"]

    target, err = _resolve_skill_target(skill_dir, file_path)
    if err:
        return {"success": False, "error": err}
    if not target.exists():
        # 列出实际存在的内容供模型查看
        available = []
        for subdir in ALLOWED_SUBDIRS:
            d = skill_dir / subdir
            if d.exists():
                for f in d.rglob("*"):
                    if f.is_file():
                        available.append(str(f.relative_to(skill_dir)))
        return {
            "success": False,
            "error": f"File '{file_path}' not found in skill '{name}'.",
            "available_files": available if available else None,
        }

    target.unlink()

    # 清理空的子目录
    parent = target.parent
    if parent != skill_dir and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()

    return {
        "success": True,
        "message": f"File '{file_path}' removed from skill '{name}'.",
    }


# =============================================================================
# 主入口
# =============================================================================

# ContextVar 旁路：在重放一个已批准的暂存技能写入时置位，使
# skill_manage() 不会再次把关（也不会再次暂存）。
import contextvars as _ctxvars
_skill_gate_bypass: "_ctxvars.ContextVar[bool]" = _ctxvars.ContextVar(
    "skill_gate_bypass", default=False
)


def _apply_skill_write_gate(action, name, **payload_kwargs):
    """评估技能写入关卡。当写入不应继续（被拦截或被暂存）时返回 JSON
    工具结果字符串；返回 None 表示执行真正的写入。在重放已批准的
    暂存写入时被旁路。
    """
    if action not in {"create", "edit", "patch", "delete", "write_file", "remove_file"}:
        return None
    if _skill_gate_bypass.get():
        return None

    try:
        from tools import write_approval as wa
    except Exception:
        return None  # 失败放行

    decision = wa.evaluate_gate(wa.SKILLS)
    if decision.allow:
        return None
    if decision.blocked:
        return tool_error(decision.message, success=False)

    # 暂存 —— 记录完整的 skill_manage 关键字参数，以便审批后可重放。
    payload = {"action": action, "name": name}
    payload.update({k: v for k, v in payload_kwargs.items() if v is not None})
    gist = wa.skill_gist(
        action, name,
        content=payload_kwargs.get("content") or "",
        file_path=payload_kwargs.get("file_path") or "",
        old_string=payload_kwargs.get("old_string") or "",
        new_string=payload_kwargs.get("new_string") or "",
    )
    record = wa.stage_write(wa.SKILLS, payload, summary=gist, origin=wa.current_origin())
    return json.dumps(
        {"success": True, "staged": True, "pending_id": record["id"],
         "gist": gist, "message": decision.message},
        ensure_ascii=False,
    )


def apply_skill_pending(payload: Dict[str, Any]) -> str:
    """重放一次暂存的技能写入，旁路关卡。返回工具结果 JSON 字符串。
    由 /skills 审批处理器调用。
    """
    token = _skill_gate_bypass.set(True)
    try:
        return skill_manage(
            action=payload.get("action", ""),
            name=payload.get("name", ""),
            content=payload.get("content"),
            category=payload.get("category"),
            file_path=payload.get("file_path"),
            file_content=payload.get("file_content"),
            old_string=payload.get("old_string"),
            new_string=payload.get("new_string"),
            replace_all=payload.get("replace_all", False),
            absorbed_into=payload.get("absorbed_into"),
        )
    finally:
        _skill_gate_bypass.reset(token)


def skill_manage(
    action: str,
    name: str,
    content: str = None,
    category: str = None,
    file_path: str = None,
    file_content: str = None,
    old_string: str = None,
    new_string: str = None,
    replace_all: bool = False,
    absorbed_into: str = None,
) -> str:
    """
    管理用户创建的技能。分派到对应的动作处理器。

    返回包含结果的 JSON 字符串。
    """
    # 审批关卡：开启时把写入暂存以供审阅（技能体量过大无法内联审阅，
    # 因此无论来源一律暂存）；关闭时（默认）直接放行。当本次调用本身
    # 就是在重放一个已批准的暂存写入时（_skill_apply_pending），关卡被旁路。
    gate_result = _apply_skill_write_gate(
        action, name, content=content, category=category,
        file_path=file_path, file_content=file_content,
        old_string=old_string, new_string=new_string,
        replace_all=replace_all, absorbed_into=absorbed_into,
    )
    if gate_result is not None:
        return gate_result

    if action == "create":
        if not content:
            return tool_error("content is required for 'create'. Provide the full SKILL.md text (frontmatter + body).", success=False)
        result = _create_skill(name, content, category)

    elif action == "edit":
        if not content:
            return tool_error("content is required for 'edit'. Provide the full updated SKILL.md text.", success=False)
        result = _edit_skill(name, content)

    elif action == "patch":
        if not old_string:
            return tool_error("old_string is required for 'patch'. Provide the text to find.", success=False)
        if new_string is None:
            return tool_error("new_string is required for 'patch'. Use empty string to delete matched text.", success=False)
        result = _patch_skill(name, old_string, new_string, file_path, replace_all)

    elif action == "delete":
        result = _delete_skill(name, absorbed_into=absorbed_into)

    elif action == "write_file":
        if not file_path:
            return tool_error("file_path is required for 'write_file'. Example: 'references/api-guide.md'", success=False)
        if file_content is None:
            return tool_error("file_content is required for 'write_file'.", success=False)
        result = _write_file(name, file_path, file_content)

    elif action == "remove_file":
        if not file_path:
            return tool_error("file_path is required for 'remove_file'.", success=False)
        result = _remove_file(name, file_path)

    else:
        result = {"success": False, "error": f"Unknown action '{action}'. Use: create, edit, patch, delete, write_file, remove_file"}

    if result.get("success"):
        try:
            from agent.prompt_builder import clear_skills_system_prompt_cache
            clear_skills_system_prompt_cache(clear_snapshot=True)
        except Exception:
            pass
        # Curator 遥测：在 edit/patch/write_file（会改动已有技能指引的
        # 动作）上递增 patch_count，在 delete 上丢弃记录。仅当后台自我
        # 改进审阅 fork 创建技能时才把它标记为 agent-created —— 前台
        # `skill_manage(create)` 调用是用户主导的，这些技能归属于用户
        # （curator 不得触碰它们）。尽力而为；遥测失败永不破坏工具。
        try:
            from tools.skill_usage import bump_patch, forget, mark_agent_created
            from tools.skill_provenance import is_background_review
            if action == "create":
                if is_background_review():
                    mark_agent_created(name)
            elif action in {"patch", "edit", "write_file", "remove_file"}:
                bump_patch(name)
            elif action == "delete":
                forget(name)
        except Exception:
            pass

    return json.dumps(result, ensure_ascii=False)


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

SKILL_MANAGE_SCHEMA = {
    "name": "skill_manage",
    "description": (
        "Manage skills (create, update, delete). Skills are your procedural "
        "memory — reusable approaches for recurring task types. "
        f"New skills go to {display_hermes_home()}/skills/; existing skills can be modified wherever they live.\n\n"
        "Actions: create (full SKILL.md + optional category), "
        "patch (old_string/new_string — preferred for fixes), "
        "edit (full SKILL.md rewrite — major overhauls only), "
        "delete, write_file, remove_file.\n\n"
        "On delete, pass `absorbed_into=<umbrella>` when you're merging this "
        "skill's content into another one, or `absorbed_into=\"\"` when you're "
        "pruning it with no forwarding target. This lets the curator tell "
        "consolidation from pruning without guessing, so downstream consumers "
        "(cron jobs that reference the old skill name, etc.) get updated "
        "correctly. The target you name in `absorbed_into` must already "
        "exist — create/patch the umbrella first, then delete.\n\n"
        "Create when: complex task succeeded (5+ calls), errors overcome, "
        "user-corrected approach worked, non-trivial workflow discovered, "
        "or user asks you to remember a procedure.\n"
        "Update when: instructions stale/wrong, OS-specific failures, "
        "missing steps or pitfalls found during use. "
        "If you used a skill and hit issues not covered by it, patch it immediately.\n\n"
        "After difficult/iterative tasks, offer to save as a skill. "
        "Skip for simple one-offs. Confirm with user before creating/deleting.\n\n"
        "Good skills: trigger conditions, numbered steps with exact commands, "
        "pitfalls section, verification steps. Use skill_view() to see format examples.\n\n"
        "Pinned skills are protected from deletion only — skill_manage(action='delete') "
        "will refuse with a message pointing the user to `hermes curator unpin <name>`. "
        "Patches and edits go through on pinned skills so you can still improve them as "
        "pitfalls come up; pin only guards against irrecoverable loss."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "patch", "edit", "delete", "write_file", "remove_file"],
                "description": "The action to perform."
            },
            "name": {
                "type": "string",
                "description": (
                    "Skill name (lowercase, hyphens/underscores, max 64 chars). "
                    "Must match an existing skill for patch/edit/delete/write_file/remove_file."
                )
            },
            "content": {
                "type": "string",
                "description": (
                    "Full SKILL.md content (YAML frontmatter + markdown body). "
                    "Required for 'create' and 'edit'. For 'edit', read the skill "
                    "first with skill_view() and provide the complete updated text."
                )
            },
            "old_string": {
                "type": "string",
                "description": (
                    "Text to find in the file (required for 'patch'). Must be unique "
                    "unless replace_all=true. Include enough surrounding context to "
                    "ensure uniqueness."
                )
            },
            "new_string": {
                "type": "string",
                "description": (
                    "Replacement text (required for 'patch'). Can be empty string "
                    "to delete the matched text."
                )
            },
            "replace_all": {
                "type": "boolean",
                "description": "For 'patch': replace all occurrences instead of requiring a unique match (default: false)."
            },
            "category": {
                "type": "string",
                "description": (
                    "Optional category/domain for organizing the skill (e.g., 'devops', "
                    "'data-science', 'mlops'). Creates a subdirectory grouping. "
                    "Only used with 'create'."
                )
            },
            "file_path": {
                "type": "string",
                "description": (
                    "Path to a supporting file within the skill directory. "
                    "For 'write_file'/'remove_file': required, must be under references/, "
                    "templates/, scripts/, or assets/. "
                    "For 'patch': optional, defaults to SKILL.md if omitted."
                )
            },
            "file_content": {
                "type": "string",
                "description": "Content for the file. Required for 'write_file'."
            },
            "absorbed_into": {
                "type": "string",
                "description": (
                    "For 'delete' only — declares intent so the curator can "
                    "tell consolidation from pruning without guessing. "
                    "Pass the umbrella skill name when this skill's content "
                    "was merged into another (the target must already exist). "
                    "Pass an empty string when the skill is truly stale and "
                    "being pruned with no forwarding target. Omitting the arg "
                    "on delete is supported for backward compatibility but "
                    "downstream tooling (e.g. cron-job skill reference "
                    "rewriting) will have to guess at intent."
                )
            },
        },
        "required": ["action", "name"],
    },
}


# --- 注册表 ---
from tools.registry import registry, tool_error

registry.register(
    name="skill_manage",
    toolset="skills",
    schema=SKILL_MANAGE_SCHEMA,
    handler=lambda args, **kw: skill_manage(
        action=args.get("action", ""),
        name=args.get("name", ""),
        content=args.get("content"),
        category=args.get("category"),
        file_path=args.get("file_path"),
        file_content=args.get("file_content"),
        old_string=args.get("old_string"),
        new_string=args.get("new_string"),
        replace_all=args.get("replace_all", False),
        absorbed_into=args.get("absorbed_into")),
    emoji="📝",
)
