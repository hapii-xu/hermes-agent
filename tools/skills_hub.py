#!/usr/bin/env python3
"""
Skills Hub —— Hermes Skills Hub 的来源适配器与 hub 状态管理。

这是一个库模块（不是 agent 工具）。它提供：
  - GitHubAuth：共享的 GitHub API 认证（PAT、gh CLI、GitHub App）
  - SkillSource ABC：所有 skill registry 适配器的接口
  - OptionalSkillSource：随仓库分发的官方可选 skill（默认不启用）
  - GitHubSource：通过 Contents API 从任意 GitHub 仓库获取 skill
  - HubLockFile：跟踪已安装 hub skill 的来源信息
  - Hub 状态目录管理（隔离区、审计日志、tap、索引缓存）

供 hermes_cli/skills_hub.py 用于 CLI 命令和 /skills 斜杠命令使用。
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from hermes_constants import get_hermes_home
from agent.skill_utils import is_excluded_skill_path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
import yaml

from tools.skills_guard import (
    ScanResult, content_hash, TRUSTED_REPOS,
)
from tools.url_safety import is_safe_url
from tools.website_policy import check_website_access

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

HERMES_HOME = get_hermes_home()
SKILLS_DIR = HERMES_HOME / "skills"
HUB_DIR = SKILLS_DIR / ".hub"
LOCK_FILE = HUB_DIR / "lock.json"
QUARANTINE_DIR = HUB_DIR / "quarantine"
AUDIT_LOG = HUB_DIR / "audit.log"
TAPS_FILE = HUB_DIR / "taps.json"
INDEX_CACHE_DIR = HUB_DIR / "index-cache"

# 远程索引拉取的缓存时长
INDEX_CACHE_TTL = 3600  # 1 小时

_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
_MAX_SKILL_FETCH_REDIRECTS = 5


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class SkillMeta:
    """搜索结果返回的最小元数据。"""
    name: str
    description: str
    source: str           # "official"、"github"、"clawhub"、"claude-marketplace"、"lobehub"
    identifier: str       # 来源专属 ID（例如 "openai/skills/skill-creator"）
    trust_level: str      # "builtin" | "trusted" | "community"
    repo: Optional[str] = None
    path: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillBundle:
    """已下载、可供隔离/扫描/安装的 skill。"""
    name: str
    files: Dict[str, Union[str, bytes]]   # 相对路径 -> 文件内容
    source: str
    identifier: str
    trust_level: str
    metadata: Dict[str, Any] = field(default_factory=dict)


def _normalize_bundle_path(path_value: str, *, field_name: str, allow_nested: bool) -> str:
    """在触碰磁盘之前，对 bundle 控制的路径做归一化与校验。"""
    if not isinstance(path_value, str):
        raise ValueError(f"Unsafe {field_name}: expected a string")

    raw = path_value.strip()
    if not raw:
        raise ValueError(f"Unsafe {field_name}: empty path")

    normalized = raw.replace("\\", "/")
    path = PurePosixPath(normalized)
    parts = [part for part in path.parts if part not in {"", "."}]

    if normalized.startswith("/") or path.is_absolute():
        raise ValueError(f"Unsafe {field_name}: {path_value}")
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"Unsafe {field_name}: {path_value}")
    if re.fullmatch(r"[A-Za-z]:", parts[0]):
        raise ValueError(f"Unsafe {field_name}: {path_value}")
    if not allow_nested and len(parts) != 1:
        raise ValueError(f"Unsafe {field_name}: {path_value}")

    return "/".join(parts)


def _validate_skill_name(name: str) -> str:
    return _normalize_bundle_path(name, field_name="skill name", allow_nested=False)


def _validate_install_parent_path(category: str) -> str:
    return _normalize_bundle_path(category, field_name="install parent path", allow_nested=True)


def _normalize_lock_install_path(install_path: str, skill_name: str) -> str:
    """在 skill 安装路径触碰 lock 文件或磁盘之前对其进行校验。

    lock 文件中的 ``install_path`` 条目是 ``uninstall_skill`` 将调用
    ``shutil.rmtree`` 的目标位置的事实来源。一个被投毒或有缺陷的条目——空字符串、
    ``"."``、绝对路径、``../..`` 目录穿越，或任何末尾组件与 skill 名不匹配的路径——
    都会让 ``rmtree`` 清空整个 ``skills/`` 目录树或目录之外的内容。

    强制要求 ``install_path`` 以 ``<skill_name>`` 结尾。嵌套的官方可选 skill
    可以合法地安装到 ``mlops/training/<skill_name>`` 这样的路径下；但目录穿越、
    绝对路径、空路径以及末尾组件不匹配的情况仍然会被拒绝。
    """
    safe_skill_name = _validate_skill_name(skill_name)
    normalized = _normalize_bundle_path(
        install_path,
        field_name="install path",
        allow_nested=True,
    )
    parts = normalized.split("/")
    if not parts or parts[-1] != safe_skill_name:
        raise ValueError(f"Unsafe install path: {install_path}")
    return normalized


def _is_path_redirect(path: Path) -> bool:
    """当 ``path`` 是符号链接，或在 Windows 上是目录联接（junction）时返回真。

    这两种形式都会让能写入 ``skills/`` 目录树的攻击者，把随后的 ``rmtree``
    重定向到目录之外的内容。``is_junction`` 仅在 Python 3.12+ 的 Windows 上存在；
    用 ``hasattr`` 做存在性判断。
    """
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _resolve_lock_install_path(install_path: str, skill_name: str) -> Path:
    """解析 lock 文件中的安装路径，但不允许逃逸出 ``SKILLS_DIR``。

    在 main 分支上已有的 ``is_relative_to`` 检查之上再加两层防御：

    1. 逐级遍历路径的每个组件，若任一中间组件是符号链接/联接则拒绝
       （否则跟随符号链接逃出 skills/ 的路径解析会被 Path.resolve 隐藏）。
    2. 在 resolve() 之后，不仅拒绝逃逸到外部，也拒绝 ``resolved == SKILLS_DIR``
       ——空的 / ``"."`` / ``""`` 的 install_path 会解析到 skills 根目录本身，
       而 ``rmtree(SKILLS_DIR)`` 会清空所有已安装的 skill。
    """
    normalized = _normalize_lock_install_path(install_path, skill_name)
    skills_root = SKILLS_DIR.resolve()

    target = SKILLS_DIR
    for part in normalized.split("/"):
        target = target / part
        if _is_path_redirect(target):
            raise ValueError(f"Unsafe install path: {install_path}")

    target = target.resolve()
    if target == skills_root or not target.is_relative_to(skills_root):
        raise ValueError(f"Unsafe install path: {install_path}")
    return target


def _guarded_http_get(url: str, *, timeout: int = 20) -> Optional[httpx.Response]:
    """在 SSRF 防护和重定向目标校验下抓取一个 URL。"""
    current_url = url

    for _ in range(_MAX_SKILL_FETCH_REDIRECTS + 1):
        if not is_safe_url(current_url):
            logger.warning("Blocked unsafe Skills Hub URL: %s", current_url)
            return None

        blocked = check_website_access(current_url)
        if blocked:
            logger.info(
                "Blocked Skills Hub fetch for %s by rule %s",
                blocked["host"],
                blocked["rule"],
            )
            return None

        try:
            resp = httpx.get(current_url, timeout=timeout, follow_redirects=False)
        except httpx.HTTPError as exc:
            logger.debug("Skills Hub fetch failed for %s: %s", current_url, exc)
            return None

        if resp.status_code in _REDIRECT_STATUS_CODES:
            location = getattr(resp, "headers", {}).get("location")
            if not location:
                return None
            current_url = urljoin(current_url, location)
            continue

        return resp

    logger.warning("Skills Hub fetch exceeded redirect limit for %s", url)
    return None


def _validate_bundle_rel_path(rel_path: str) -> str:
    return _normalize_bundle_path(rel_path, field_name="bundle file path", allow_nested=True)


# ---------------------------------------------------------------------------
# GitHub 认证
# ---------------------------------------------------------------------------

class GitHubAuth:
    """
    GitHub API 认证。按优先级依次尝试以下方式：
      1. GITHUB_TOKEN / GH_TOKEN 环境变量（PAT —— 默认方式）
      2. `gh auth token` 子进程（如果已安装 gh CLI）
      3. GitHub App JWT + 安装令牌（如果配置了 app 凭据）
      4. 未认证（60 次/小时，仅限公开仓库）
    """

    def __init__(self):
        self._cached_token: Optional[str] = None
        self._cached_method: Optional[str] = None
        self._app_token_expiry: float = 0

    def get_headers(self) -> Dict[str, str]:
        """返回 GitHub API 请求所用的认证头。"""
        token = self._resolve_token()
        headers = {"Accept": "application/vnd.github.v3+json"}
        if token:
            headers["Authorization"] = f"token {token}"
        return headers

    def is_authenticated(self) -> bool:
        return self._resolve_token() is not None

    def auth_method(self) -> str:
        """返回当前生效的认证方式：'pat'、'gh-cli'、'github-app' 或 'anonymous'。"""
        self._resolve_token()
        return self._cached_method or "anonymous"

    def _resolve_token(self) -> Optional[str]:
        # 若缓存的 token 仍然有效则直接返回
        if self._cached_token:
            if self._cached_method != "github-app" or time.time() < self._app_token_expiry:
                return self._cached_token

        # 1. 环境变量
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            self._cached_token = token
            self._cached_method = "pat"
            return token

        # 2. gh CLI
        token = self._try_gh_cli()
        if token:
            self._cached_token = token
            self._cached_method = "gh-cli"
            return token

        # 3. GitHub App
        token = self._try_github_app()
        if token:
            self._cached_token = token
            self._cached_method = "github-app"
            self._app_token_expiry = time.time() + 3500  # 约 58 分钟（令牌有效期为 1 小时）
            return token

        self._cached_method = "anonymous"
        return None

    def _try_gh_cli(self) -> Optional[str]:
        """尝试通过 gh CLI 获取一个令牌。"""
        try:
            result = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True, text=True, timeout=5,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.debug("gh CLI token lookup failed: %s", e)
        return None

    def _try_github_app(self) -> Optional[str]:
        """如果配置了凭据，则尝试 GitHub App JWT 认证。"""
        app_id = os.environ.get("GITHUB_APP_ID")
        key_path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
        installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID")

        if not all([app_id, key_path, installation_id]):
            return None

        try:
            import jwt  # PyJWT
        except ImportError:
            logger.debug("PyJWT not installed, skipping GitHub App auth")
            return None

        try:
            key_file = Path(key_path)
            if not key_file.exists():
                return None
            private_key = key_file.read_text(encoding="utf-8")

            now = int(time.time())
            payload = {
                "iat": now - 60,
                "exp": now + (10 * 60),
                "iss": app_id,
            }
            encoded_jwt = jwt.encode(payload, private_key, algorithm="RS256")

            resp = httpx.post(
                f"https://api.github.com/app/installations/{installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {encoded_jwt}",
                    "Accept": "application/vnd.github.v3+json",
                },
                timeout=10,
            )
            if resp.status_code == 201:
                return resp.json().get("token")
        except Exception as e:
            logger.debug(f"GitHub App auth failed: {e}")

        return None


# ---------------------------------------------------------------------------
# 来源适配器接口
# ---------------------------------------------------------------------------

class SkillSource(ABC):
    """所有 skill registry 适配器的抽象基类。"""

    @abstractmethod
    def search(self, query: str, limit: int = 10) -> List[SkillMeta]:
        """搜索与查询字符串匹配的 skill。"""
        ...

    @abstractmethod
    def fetch(self, identifier: str) -> Optional[SkillBundle]:
        """按标识符下载一个 skill bundle。"""
        ...

    @abstractmethod
    def inspect(self, identifier: str) -> Optional[SkillMeta]:
        """获取某个 skill 的元数据，但不下载全部文件。"""
        ...

    @abstractmethod
    def source_id(self) -> str:
        """该来源的唯一标识（例如 'github'、'clawhub'）。"""
        ...

    def trust_level_for(self, identifier: str) -> str:
        """判定来自该来源的某个 skill 的信任级别。"""
        return "community"


# ---------------------------------------------------------------------------
# GitHub 来源适配器
# ---------------------------------------------------------------------------

class GitHubSource(SkillSource):
    """通过 Contents API 从 GitHub 仓库获取 skill。"""

    DEFAULT_TAPS = [
        # 注意：openai/skills 把内容迁移到了 skills/.curated/（系统级 skill
        # 则在 skills/.system/）。_list_skills_in_repo 会跳过以 "." 或 "_"
        # 开头的目录，所以我们直接把这两条指向内部路径。
        {"repo": "openai/skills", "path": "skills/.curated/"},
        {"repo": "openai/skills", "path": "skills/.system/"},
        {"repo": "anthropics/skills", "path": "skills/"},
        {"repo": "huggingface/skills", "path": "skills/"},
        # NVIDIA/skills：NVIDIA 验证的 skill，覆盖 CUDA-X、AIQ、cuOpt、
        # cuPyNumeric、DeepStream、NeMo、NemoClaw 等。每个 skill 都附带
        # 一个已签名的 `skill.oms.sig`、一个 OMS 签名的 `skill-card.md`
        # （治理卡片）以及一个 `evals/` 目录——每日从 NVIDIA 产品仓库同步。
        # 视为 `trusted`（见 `tools/skills_guard.py::TRUSTED_REPOS`）。示例布局：
        # https://github.com/NVIDIA/skills/tree/main/skills
        {"repo": "NVIDIA/skills", "path": "skills/"},
        {"repo": "garrytan/gstack", "path": ""},
    ]

    def __init__(self, auth: GitHubAuth, extra_taps: Optional[List[Dict]] = None):
        self.auth = auth
        self.taps = list(self.DEFAULT_TAPS)
        if extra_taps:
            self.taps.extend(extra_taps)
        # 实例级缓存：repo -> (default_branch, tree_entries)
        # 在单次 search/install 流程内复用，避免重复的 API 调用。
        self._tree_cache: Dict[str, Tuple[str, List[dict]]] = {}
        # 可选的 skills.sh.json 分组附带文件按仓库缓存，
        # 把 skill_name 映射到人类可读的分组标题。``None`` 表示
        # “已拉取、无附带文件”；缺少该键表示“尚未拉取”。
        self._skillsh_groupings: Dict[str, Optional[Dict[str, str]]] = {}
        # 当 GitHub 返回 403 且速率限制耗尽时置位
        self._rate_limited: bool = False

    def source_id(self) -> str:
        return "github"

    @property
    def is_rate_limited(self) -> bool:
        """操作过程中是否触发了 GitHub API 的速率限制。"""
        return self._rate_limited

    def trust_level_for(self, identifier: str) -> str:
        # identifier 格式："owner/repo/path/to/skill"
        parts = identifier.split("/", 2)
        if len(parts) >= 2:
            repo = f"{parts[0]}/{parts[1]}"
            if repo in TRUSTED_REPOS:
                return "trusted"
        return "community"

    def search(self, query: str, limit: int = 10) -> List[SkillMeta]:
        """在所有 tap 中搜索与查询匹配的 skill。"""
        results: List[SkillMeta] = []
        query_lower = query.lower()

        for tap in self.taps:
            try:
                skills = self._list_skills_in_repo(tap["repo"], tap.get("path", ""))
                for skill in skills:
                    searchable = f"{skill.name} {skill.description} {' '.join(skill.tags)}".lower()
                    if query_lower in searchable:
                        results.append(skill)
            except Exception as e:
                logger.debug(f"Failed to search {tap['repo']}: {e}")
                continue

        # 按 identifier 去重，优先保留信任级别更高的条目。
        # identifier 每个 skill 唯一；name 并不唯一（两个配置的 tap 可能
        # 发布同名但 identifier 不同的 skill）。
        _trust_rank = {"builtin": 2, "trusted": 1, "community": 0}
        seen = {}
        for r in results:
            if r.identifier not in seen:
                seen[r.identifier] = r
            elif _trust_rank.get(r.trust_level, 0) > _trust_rank.get(seen[r.identifier].trust_level, 0):
                seen[r.identifier] = r
        results = list(seen.values())

        return results[:limit]

    def fetch(self, identifier: str) -> Optional[SkillBundle]:
        """
        从 GitHub 下载一个 skill。
        identifier 格式："owner/repo/path/to/skill-dir"
        """
        parts = identifier.split("/", 2)
        if len(parts) < 3:
            return None

        repo = f"{parts[0]}/{parts[1]}"
        skill_path = parts[2]

        files = self._download_directory(repo, skill_path)
        if not files or "SKILL.md" not in files:
            return None

        skill_name = skill_path.rstrip("/").split("/")[-1]
        trust = self.trust_level_for(identifier)

        return SkillBundle(
            name=skill_name,
            files=files,
            source="github",
            identifier=identifier,
            trust_level=trust,
        )

    def inspect(self, identifier: str) -> Optional[SkillMeta]:
        """仅获取 SKILL.md 的元数据用于预览。"""
        parts = identifier.split("/", 2)
        if len(parts) < 3:
            return None

        repo = f"{parts[0]}/{parts[1]}"
        skill_path = parts[2].rstrip("/")
        skill_md_path = f"{skill_path}/SKILL.md"

        content = self._fetch_file_content(repo, skill_md_path)
        if not content:
            return None

        fm = self._parse_frontmatter_quick(content)
        skill_name = fm.get("name", skill_path.split("/")[-1])
        description = fm.get("description", "")

        tags = []
        metadata = fm.get("metadata", {})
        if isinstance(metadata, dict):
            hermes_meta = metadata.get("hermes", {})
            if isinstance(hermes_meta, dict):
                tags = hermes_meta.get("tags", [])
        if not tags:
            raw_tags = fm.get("tags", [])
            tags = raw_tags if isinstance(raw_tags, list) else []

        return SkillMeta(
            name=skill_name,
            description=str(description),
            source="github",
            identifier=identifier,
            trust_level=self.trust_level_for(identifier),
            repo=repo,
            path=skill_path,
            tags=[str(t) for t in tags],
        )

    # -- 内部辅助方法 --

    def _list_skills_in_repo(self, repo: str, path: str) -> List[SkillMeta]:
        """使用缓存的索引列出 GitHub 仓库某路径下的 skill 目录。"""
        cache_key = f"{repo}_{path}".replace("/", "_").replace(" ", "_")
        cached = self._read_cache(cache_key)
        if cached is not None:
            return [SkillMeta(**s) for s in cached]

        url = f"https://api.github.com/repos/{repo}/contents/{path.rstrip('/')}"
        resp = self._github_get(url)
        if resp is None or resp.status_code != 200:
            return []

        entries = resp.json()
        if not isinstance(entries, list):
            return []

        skills: List[SkillMeta] = []
        groupings = self._get_skillsh_groupings(repo)
        for entry in entries:
            if entry.get("type") != "dir":
                continue

            dir_name = entry["name"]
            if dir_name.startswith((".", "_")):
                continue

            prefix = path.rstrip("/")
            skill_identifier = f"{repo}/{prefix}/{dir_name}" if prefix else f"{repo}/{dir_name}"
            meta = self.inspect(skill_identifier)
            if meta:
                if groupings:
                    category = groupings.get(meta.name) or groupings.get(dir_name)
                    if category:
                        meta.extra["category"] = category
                skills.append(meta)

        # 缓存结果
        self._write_cache(cache_key, [self._meta_to_dict(s) for s in skills])
        return skills

    # -- 仓库树缓存（避免冗余的 API 调用）--

    def _get_repo_tree(self, repo: str) -> Optional[Tuple[str, List[dict]]]:
        """获取缓存的或新拉取的仓库树。

        返回 ``(default_branch, tree_entries)`` 或 ``None``。
        单次安装可能对同一个仓库多次调用 ``_download_directory_via_tree``
        和 ``_find_skill_in_repo_tree``——这个缓存消除了冗余的
        ``GET /repos/{repo}`` + ``GET /repos/{repo}/git/trees/{branch}``
        往返（此前每次安装最多重复 6 对，白白消耗未认证速率限制
        60 次/小时中的约 12 次）。
        """
        if repo in self._tree_cache:
            return self._tree_cache[repo]

        headers = self.auth.get_headers()

        # 解析默认分支
        try:
            resp = httpx.get(
                f"https://api.github.com/repos/{repo}",
                headers=headers, timeout=15, follow_redirects=True,
            )
            if resp.status_code != 200:
                self._check_rate_limit_response(resp)
                return None
            default_branch = resp.json().get("default_branch", "main")
        except (httpx.HTTPError, ValueError):
            return None

        # 拉取递归树
        try:
            resp = httpx.get(
                f"https://api.github.com/repos/{repo}/git/trees/{default_branch}",
                params={"recursive": "1"},
                headers=headers, timeout=30, follow_redirects=True,
            )
            if resp.status_code != 200:
                self._check_rate_limit_response(resp)
                return None
            tree_data = resp.json()
            if tree_data.get("truncated"):
                logger.debug("Git tree truncated for %s, cannot cache", repo)
                return None
        except (httpx.HTTPError, ValueError):
            return None

        entries = tree_data.get("tree", [])
        self._tree_cache[repo] = (default_branch, entries)
        return (default_branch, entries)

    def _check_rate_limit_response(self, resp: "httpx.Response") -> None:
        """当 GitHub 返回 403 且配额耗尽时，把该实例标记为已被速率限制。"""
        if resp.status_code in (403, 429):
            remaining = resp.headers.get("X-RateLimit-Remaining", "")
            if remaining == "0" or resp.status_code == 429:
                self._rate_limited = True
                logger.warning(
                    "GitHub API rate limit exhausted (unauthenticated: 60 req/hr). "
                    "Set GITHUB_TOKEN or install the gh CLI to raise the limit to 5,000/hr."
                )

    def _github_get(
        self,
        url: str,
        *,
        params: Optional[Dict] = None,
        headers: Optional[Dict] = None,
        timeout: float = 15.0,
        max_retries: int = 3,
    ) -> Optional["httpx.Response"]:
        """对 GitHub API 发起 GET 请求，对瞬时失败做重试/退避。

        返回最终的 ``httpx.Response``（由调用方检查状态码），或当每次尝试都
        抛出传输错误时返回 ``None``。

        重试场景：
          - 403/429 且带有 ``X-RateLimit-Remaining: 0`` —— 当响应头存在时
            等到重置时间（有上限），否则指数退避。这是所有 GitHub tap 一起
            崩掉的场景：一条共享速率限制在索引构建期间同时把 github +
            claude-marketplace + well-known 清零。
          - 5xx 以及连接/超时错误 —— 指数退避。

        当速率限制彻底耗尽时，通过 ``_check_rate_limit_response`` 标记该实例，
        以便构建过程能显式失败，而不是静默地发布一个把 GitHub 来源清零的索引。
        """
        hdrs = headers if headers is not None else self.auth.get_headers()
        backoff = 1.0
        last_resp: Optional["httpx.Response"] = None
        for attempt in range(max_retries):
            try:
                resp = httpx.get(
                    url, params=params, headers=hdrs,
                    timeout=timeout, follow_redirects=True,
                )
            except httpx.HTTPError as e:
                logger.debug("GitHub GET %s failed (attempt %d/%d): %s",
                             url, attempt + 1, max_retries, e)
                if attempt < max_retries - 1:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
                return None

            last_resp = resp
            if resp.status_code == 200:
                return resp

            # 被速率限制：若存在重置响应头则遵守它，否则退避。
            if resp.status_code in (403, 429):
                remaining = resp.headers.get("X-RateLimit-Remaining", "")
                is_rl = remaining == "0" or resp.status_code == 429
                if is_rl and attempt < max_retries - 1:
                    wait = backoff
                    reset = resp.headers.get("X-RateLimit-Reset", "")
                    retry_after = resp.headers.get("Retry-After", "")
                    if retry_after.isdigit():
                        wait = min(float(retry_after), 60.0)
                    elif reset.isdigit():
                        delta = float(reset) - time.time()
                        if 0 < delta <= 60.0:
                            wait = delta
                    logger.debug(
                        "GitHub rate limited on %s, waiting %.1fs (attempt %d/%d)",
                        url, wait, attempt + 1, max_retries,
                    )
                    time.sleep(wait)
                    backoff = min(backoff * 2, 30.0)
                    continue
                # 重试次数用尽（或并非速率限制的 403）—— 标记后返回。
                self._check_rate_limit_response(resp)
                return resp

            # 5xx —— 重试；4xx（非速率限制）—— 立即返回。
            if 500 <= resp.status_code < 600 and attempt < max_retries - 1:
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            return resp

        return last_resp


    def _download_directory(self, repo: str, path: str) -> Dict[str, str]:
        """递归下载 GitHub 目录下的所有文本文件。

        优先使用 Git Trees API（一次调用拿到整棵树），避免按目录调用
        带来的速率限制导致子目录被静默漏掉。当树接口不可用或响应被截断时，
        回退到递归的 Contents API。
        """
        files = self._download_directory_via_tree(repo, path)
        if files is not None:
            return files
        logger.debug("Tree API unavailable for %s/%s, falling back to Contents API", repo, path)
        return self._download_directory_recursive(repo, path)

    def _download_directory_via_tree(self, repo: str, path: str) -> Optional[Dict[str, str]]:
        """使用 Git Trees API（单次请求）下载整个目录。

        返回：
            若该路径存在且有内容，返回文件字典；
            若树已缓存但该路径不存在，返回空字典 ``{}``
            （避免不必要的 Contents API 回退）；
            若无法拉取到树，返回 ``None``（触发 Contents API 回退）。
        """
        path = path.rstrip("/")

        cached = self._get_repo_tree(repo)
        if cached is None:
            return None
        _default_branch, tree_entries = cached

        # 检查目标路径下是否存在任意条目
        prefix = f"{path}/"
        has_entries = any(
            item.get("path", "").startswith(prefix) for item in tree_entries
        )
        if not has_entries:
            # 路径确定不存在于仓库中 —— 返回空字典
            # 而不是 None，以跳过 Contents API 回退。
            return {}

        # 过滤出目标路径下的 blob 并拉取内容
        files: Dict[str, str] = {}
        for item in tree_entries:
            if item.get("type") != "blob":
                continue
            item_path = item.get("path", "")
            if not item_path.startswith(prefix):
                continue
            rel_path = item_path[len(prefix):]
            content = self._fetch_file_content(repo, item_path)
            if content is not None:
                files[rel_path] = content
            else:
                logger.debug("Skipped file (fetch failed): %s/%s", repo, item_path)

        return files if files else None

    def _download_directory_recursive(self, repo: str, path: str) -> Dict[str, str]:
        """通过 Contents API 递归下载（回退方案）。"""
        url = f"https://api.github.com/repos/{repo}/contents/{path.rstrip('/')}"
        try:
            resp = httpx.get(url, headers=self.auth.get_headers(), timeout=15, follow_redirects=True)
            if resp.status_code != 200:
                logger.debug("Contents API returned %d for %s/%s", resp.status_code, repo, path)
                return {}
        except httpx.HTTPError:
            return {}

        entries = resp.json()
        if not isinstance(entries, list):
            return {}

        files: Dict[str, str] = {}
        for entry in entries:
            name = entry.get("name", "")
            entry_type = entry.get("type", "")

            if entry_type == "file":
                content = self._fetch_file_content(repo, entry.get("path", ""))
                if content is not None:
                    rel_path = name
                    files[rel_path] = content
            elif entry_type == "dir":
                sub_files = self._download_directory_recursive(repo, entry.get("path", ""))
                if not sub_files:
                    logger.debug("Empty or failed subdirectory: %s/%s", repo, entry.get("path", ""))
                for sub_name, sub_content in sub_files.items():
                    files[f"{name}/{sub_name}"] = sub_content

        return files

    def _find_skill_in_repo_tree(self, repo: str, skill_name: str) -> Optional[str]:
        """使用 GitHub Trees API 在仓库任意位置查找 skill 目录。

        返回完整的 identifier（``repo/path/to/skill``）或 ``None``。
        无论仓库有多深，都只需一次 API 调用，因此能高效处理
        ``cli-tool/components/skills/development/<skill>/SKILL.md`` 这种
        深层嵌套的目录结构。
        """
        cached = self._get_repo_tree(repo)
        if cached is None:
            return None
        _default_branch, tree_entries = cached

        # 在名为 <skill_name> 的目录中查找 SKILL.md 文件
        skill_md_suffix = f"/{skill_name}/SKILL.md"
        for entry in tree_entries:
            if entry.get("type") != "blob":
                continue
            path = entry.get("path", "")
            if path.endswith(skill_md_suffix) or path == f"{skill_name}/SKILL.md":
                # 去掉末尾的 /SKILL.md 得到 skill 目录路径
                skill_dir = path[: -len("/SKILL.md")]
                return f"{repo}/{skill_dir}"

        return None

    def _fetch_file_content(self, repo: str, path: str) -> Optional[str]:
        """从 GitHub 拉取单个文件的内容。"""
        url = f"https://api.github.com/repos/{repo}/contents/{path}"
        resp = self._github_get(
            url,
            headers={**self.auth.get_headers(), "Accept": "application/vnd.github.v3.raw"},
        )
        if resp is not None and resp.status_code == 200:
            return resp.text
        return None

    def _get_skillsh_groupings(self, repo: str) -> Optional[Dict[str, str]]:
        """拉取并解析仓库根目录下的 ``skills.sh.json`` 分组附带文件。

        ``skills.sh.json`` 是一份已发布的跨生态标准
        （``$schema: https://skills.sh/schemas/skills.sh.schema.json``），
        允许某个 tap 为其 skill 声明人类可读的分类分组：

            {"groupings": [{"title": "Inference AI", "skills": ["dynamo-..."]}]}

        我们把它展平成 ``{skill_name: grouping_title}``，这样 Skills Hub
        界面就能显示真实的分类标签，而不是基于 tag 的猜测。任何附带此文件的
        tap 都能免费获得分类能力——这并非 NVIDIA 专属。

        成功时返回该映射（可能为空），当仓库没有附带文件或无法解析时返回 ``None``。
        按仓库缓存在实例上。
        """
        if repo in self._skillsh_groupings:
            return self._skillsh_groupings[repo]

        content = self._fetch_file_content(repo, "skills.sh.json")
        groupings = self._parse_skillsh_groupings(content) if content else None
        self._skillsh_groupings[repo] = groupings
        return groupings

    @staticmethod
    def _parse_skillsh_groupings(content: str) -> Optional[Dict[str, str]]:
        """把一份 ``skills.sh.json`` 文档展平成 ``{skill_name: title}``。

        当内容不是可用的分组文档时返回 ``None``。
        """
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        groupings = data.get("groupings")
        if not isinstance(groupings, list):
            return None

        mapping: Dict[str, str] = {}
        for group in groupings:
            if not isinstance(group, dict):
                continue
            title = group.get("title")
            members = group.get("skills")
            if not isinstance(title, str) or not isinstance(members, list):
                continue
            for member in members:
                if isinstance(member, str) and member:
                    # 若一个 skill 被列出两次，以第一个分组为准。
                    mapping.setdefault(member, title)
        return mapping

    def _read_cache(self, key: str) -> Optional[list]:
        """读取未过期的缓存索引。"""
        cache_file = INDEX_CACHE_DIR / f"{key}.json"
        if not cache_file.exists():
            return None
        try:
            stat = cache_file.stat()
            if time.time() - stat.st_mtime > INDEX_CACHE_TTL:
                return None
            return json.loads(cache_file.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _write_cache(self, key: str, data: list) -> None:
        """把索引数据写入缓存。"""
        INDEX_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = INDEX_CACHE_DIR / f"{key}.json"
        try:
            cache_file.write_text(json.dumps(data, ensure_ascii=False))
        except OSError as e:
            logger.debug("Could not write cache: %s", e)

    @staticmethod
    def _meta_to_dict(meta: SkillMeta) -> dict:
        return {
            "name": meta.name,
            "description": meta.description,
            "source": meta.source,
            "identifier": meta.identifier,
            "trust_level": meta.trust_level,
            "repo": meta.repo,
            "path": meta.path,
            "tags": meta.tags,
            "extra": meta.extra,
        }

    @staticmethod
    def _parse_frontmatter_quick(content: str) -> dict:
        """从 SKILL.md 内容中解析 YAML frontmatter。"""
        if not content.startswith("---"):
            return {}
        match = re.search(r'\n---\s*\n', content[3:])
        if not match:
            return {}
        yaml_text = content[3:match.start() + 3]
        try:
            parsed = yaml.safe_load(yaml_text)
            return parsed if isinstance(parsed, dict) else {}
        except yaml.YAMLError:
            return {}


# ---------------------------------------------------------------------------
# Well-known Agent Skills 端点来源适配器
# ---------------------------------------------------------------------------

class WellKnownSkillSource(SkillSource):
    """从暴露 /.well-known/skills/index.json 的域名读取 skill。"""

    BASE_PATH = "/.well-known/skills"

    def source_id(self) -> str:
        return "well-known"

    def trust_level_for(self, identifier: str) -> str:
        return "community"

    def search(self, query: str, limit: int = 10) -> List[SkillMeta]:
        index_url = self._query_to_index_url(query)
        if not index_url:
            return []

        parsed = self._parse_index(index_url)
        if not parsed:
            return []

        results: List[SkillMeta] = []
        for entry in parsed["skills"][:limit]:
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            description = entry.get("description", "")
            files = entry.get("files", ["SKILL.md"])
            results.append(SkillMeta(
                name=name,
                description=str(description),
                source="well-known",
                identifier=self._wrap_identifier(parsed["base_url"], name),
                trust_level="community",
                path=name,
                extra={
                    "index_url": parsed["index_url"],
                    "base_url": parsed["base_url"],
                    "files": files if isinstance(files, list) else ["SKILL.md"],
                },
            ))
        return results

    def inspect(self, identifier: str) -> Optional[SkillMeta]:
        parsed = self._parse_identifier(identifier)
        if not parsed:
            return None

        entry = self._index_entry(parsed["index_url"], parsed["skill_name"])
        if not entry:
            return None

        skill_md = self._fetch_text(f"{parsed['skill_url']}/SKILL.md")
        if skill_md is None:
            return None

        fm = GitHubSource._parse_frontmatter_quick(skill_md)
        description = str(fm.get("description") or entry.get("description") or "")
        name = str(fm.get("name") or parsed["skill_name"])
        return SkillMeta(
            name=name,
            description=description,
            source="well-known",
            identifier=self._wrap_identifier(parsed["base_url"], parsed["skill_name"]),
            trust_level="community",
            path=parsed["skill_name"],
            extra={
                "index_url": parsed["index_url"],
                "base_url": parsed["base_url"],
                "files": entry.get("files", ["SKILL.md"]),
                "endpoint": parsed["skill_url"],
            },
        )

    def fetch(self, identifier: str) -> Optional[SkillBundle]:
        parsed = self._parse_identifier(identifier)
        if not parsed:
            return None

        try:
            skill_name = _validate_skill_name(parsed["skill_name"])
        except ValueError:
            logger.warning("Well-known skill identifier contained unsafe skill name: %s", identifier)
            return None

        entry = self._index_entry(parsed["index_url"], parsed["skill_name"])
        if not entry:
            return None

        files = entry.get("files", ["SKILL.md"])
        if not isinstance(files, list) or not files:
            files = ["SKILL.md"]

        downloaded: Dict[str, str] = {}
        for rel_path in files:
            if not isinstance(rel_path, str) or not rel_path:
                continue
            try:
                safe_rel_path = _validate_bundle_rel_path(rel_path)
            except ValueError:
                logger.warning(
                    "Well-known skill %s advertised unsafe file path: %r",
                    identifier,
                    rel_path,
                )
                return None
            text = self._fetch_text(f"{parsed['skill_url']}/{safe_rel_path}")
            if text is None:
                return None
            downloaded[safe_rel_path] = text

        if "SKILL.md" not in downloaded:
            return None

        return SkillBundle(
            name=skill_name,
            files=downloaded,
            source="well-known",
            identifier=self._wrap_identifier(parsed["base_url"], skill_name),
            trust_level="community",
            metadata={
                "index_url": parsed["index_url"],
                "base_url": parsed["base_url"],
                "endpoint": parsed["skill_url"],
                "files": files,
            },
        )

    def _query_to_index_url(self, query: str) -> Optional[str]:
        query = query.strip()
        if not query.startswith(("http://", "https://")):
            return None
        if query.endswith("/index.json"):
            return query
        if f"{self.BASE_PATH}/" in query:
            base_url = query.split(f"{self.BASE_PATH}/", 1)[0] + self.BASE_PATH
            return f"{base_url}/index.json"
        return query.rstrip("/") + f"{self.BASE_PATH}/index.json"

    def _parse_identifier(self, identifier: str) -> Optional[dict]:
        raw = identifier[len("well-known:"):] if identifier.startswith("well-known:") else identifier
        if not raw.startswith(("http://", "https://")):
            return None

        parsed_url = urlparse(raw)
        clean_url = urlunparse(parsed_url._replace(fragment=""))
        fragment = parsed_url.fragment

        if clean_url.endswith("/index.json"):
            if not fragment:
                return None
            base_url = clean_url[:-len("/index.json")]
            skill_name = fragment
            skill_url = f"{base_url}/{skill_name}"
            return {
                "index_url": clean_url,
                "base_url": base_url,
                "skill_name": skill_name,
                "skill_url": skill_url,
            }

        if clean_url.endswith("/SKILL.md"):
            skill_url = clean_url[:-len("/SKILL.md")]
        else:
            skill_url = clean_url.rstrip("/")

        if f"{self.BASE_PATH}/" not in skill_url:
            return None

        base_url, skill_name = skill_url.rsplit("/", 1)
        return {
            "index_url": f"{base_url}/index.json",
            "base_url": base_url,
            "skill_name": skill_name,
            "skill_url": skill_url,
        }

    def _parse_index(self, index_url: str) -> Optional[dict]:
        cache_key = f"well_known_index_{hashlib.md5(index_url.encode()).hexdigest()}"
        cached = _read_index_cache(cache_key)
        if isinstance(cached, dict) and isinstance(cached.get("skills"), list):
            return cached

        resp = _guarded_http_get(index_url, timeout=20)
        if resp is None or resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except json.JSONDecodeError:
            return None

        skills = data.get("skills", []) if isinstance(data, dict) else []
        if not isinstance(skills, list):
            return None

        parsed = {
            "index_url": index_url,
            "base_url": index_url[:-len("/index.json")],
            "skills": skills,
        }
        _write_index_cache(cache_key, parsed)
        return parsed

    def _index_entry(self, index_url: str, skill_name: str) -> Optional[dict]:
        parsed = self._parse_index(index_url)
        if not parsed:
            return None
        for entry in parsed["skills"]:
            if isinstance(entry, dict) and entry.get("name") == skill_name:
                return entry
        return None

    @staticmethod
    def _fetch_text(url: str) -> Optional[str]:
        resp = _guarded_http_get(url, timeout=20)
        if resp is not None and resp.status_code == 200:
            return resp.text
        return None

    @staticmethod
    def _wrap_identifier(base_url: str, skill_name: str) -> str:
        return f"well-known:{base_url.rstrip('/')}/{skill_name}"


# ---------------------------------------------------------------------------
# 直接 URL 来源适配器
# ---------------------------------------------------------------------------

class UrlSource(SkillSource):
    """直接从 HTTP(S) URL 拉取单文件 SKILL.md 形式的 skill。

    identifier 就是 URL 本身（例如 ``https://example.com/path/SKILL.md``）。
    仅支持单文件 skill —— 带 ``references/`` 或 ``scripts/`` 子目录的多文件
    skill 需要清单文件，而我们无法从一个裸 URL 推断出来。

    skill 名从 SKILL.md 的 YAML frontmatter 中的 ``name:`` 字段读取
    （并以 URL slug 作为回退）。信任级别始终为 ``community``，并会运行与
    其他所有来源相同的安全扫描。
    """

    def source_id(self) -> str:
        return "url"

    def trust_level_for(self, identifier: str) -> str:
        return "community"

    # 对直接 URL 来说搜索没有意义 —— 直接跳过（返回空）。
    def search(self, query: str, limit: int = 10) -> List[SkillMeta]:
        return []

    def _matches(self, identifier: str) -> bool:
        """当本来源应当处理 ``identifier`` 时返回真。

        我们认领以 ``.md`` 结尾的裸 HTTP(S) URL（通常是
        ``.../SKILL.md``）。带前缀的 identifier（``github:``、
        ``well-known:`` 等）以及 ``/.well-known/skills/`` URL 留给
        各自的适配器处理。
        """
        if not isinstance(identifier, str):
            return False
        ident = identifier.strip()
        if not ident.lower().startswith(("http://", "https://")):
            return False
        # 不要抢占 well-known URL。
        if "/.well-known/skills/" in ident or ident.rstrip("/").endswith("/index.json"):
            return False
        # 只认领看起来像 markdown 文件的 URL。
        try:
            path = urlparse(ident).path
        except ValueError:
            return False
        return path.lower().endswith(".md")

    def inspect(self, identifier: str) -> Optional[SkillMeta]:
        if not self._matches(identifier):
            return None
        url = identifier.strip()
        text = self._fetch_text(url)
        if text is None:
            return None
        fm = GitHubSource._parse_frontmatter_quick(text)
        name = self._resolve_skill_name(fm, url)
        description = str(fm.get("description") or "")
        tags: List[str] = []
        metadata = fm.get("metadata", {})
        if isinstance(metadata, dict):
            hermes_meta = metadata.get("hermes", {})
            if isinstance(hermes_meta, dict):
                raw_tags = hermes_meta.get("tags", [])
                if isinstance(raw_tags, list):
                    tags = [str(t) for t in raw_tags]
        return SkillMeta(
            name=name or "",
            description=description,
            source="url",
            identifier=url,
            trust_level="community",
            path=name or "",
            tags=tags,
            extra={"url": url, "awaiting_name": name is None},
        )

    def fetch(self, identifier: str) -> Optional[SkillBundle]:
        if not self._matches(identifier):
            return None
        url = identifier.strip()
        text = self._fetch_text(url)
        if text is None:
            return None

        fm = GitHubSource._parse_frontmatter_quick(text)
        name = self._resolve_skill_name(fm, url)

        # 当自动解析失败时，返回一个名字为空、metadata 中带 ``awaiting_name=True``
        # 的 bundle。安装流程（``do_install``）要么在 TTY 上提示用户输入，
        # 要么在非交互场景下以可操作的错误拒绝。保留这次较昂贵的 HTTP 抓取结果，
        # 这样调用方在选定名字后就不必重新下载。
        skill_name = ""
        if name is not None:
            try:
                skill_name = _validate_skill_name(name)
            except ValueError:
                logger.warning("URL skill %s produced unsafe skill name: %r", url, name)
                return None

        return SkillBundle(
            name=skill_name,
            files={"SKILL.md": text},
            source="url",
            identifier=url,
            trust_level="community",
            metadata={"url": url, "awaiting_name": not skill_name},
        )

    @staticmethod
    def _fetch_text(url: str) -> Optional[str]:
        resp = _guarded_http_get(url, timeout=20)
        if resp is not None and resp.status_code == 200:
            return resp.text
        return None

    # skill 名必须形似标识符：小写字母/数字，可选地包含连字符/下划线。
    # 在写入磁盘之前拦截危险（``../evil``）以及无用（``SKILL``、``README``、空）的候选名。
    _VALID_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

    @classmethod
    def _is_valid_skill_name(cls, name: Optional[str]) -> bool:
        if not isinstance(name, str):
            return False
        candidate = name.strip().lower()
        if not candidate or candidate in {"skill", "readme", "index", "unnamed-skill"}:
            return False
        return bool(cls._VALID_NAME_RE.match(candidate))

    @classmethod
    def _resolve_skill_name(cls, fm: dict, url: str) -> Optional[str]:
        """从 frontmatter 或 URL 中挑出一个 skill 名。

        当两个来源都无法给出合法 identifier 时返回 ``None``；
        调用方（CLI 的 ``do_install``）随后提示用户或拒绝。宁可给出一个
        干净的失败，也不要自动起一个像 ``SKILL`` 或 ``unnamed-skill`` 这样
        无意义的名字。
        """
        # 1. frontmatter 的 ``name:`` 在存在且合法时具有权威性。
        fm_name = fm.get("name") if isinstance(fm, dict) else None
        if isinstance(fm_name, str) and cls._is_valid_skill_name(fm_name):
            return fm_name.strip()

        # 2. URL slug 启发式：``.../<name>/SKILL.md`` → ``<name>``；
        #    ``.../<name>.md`` → ``<name>``。逐一对候选做校验。
        try:
            path = urlparse(url).path
        except ValueError:
            return None
        parts = [p for p in path.split("/") if p]
        if parts and parts[-1].lower() == "skill.md" and len(parts) >= 2:
            candidate = parts[-2]
            if cls._is_valid_skill_name(candidate):
                return candidate
        if parts:
            candidate = re.sub(r"\.md$", "", parts[-1], flags=re.IGNORECASE)
            if cls._is_valid_skill_name(candidate):
                return candidate

        # 没有可用的候选 —— 交由调用方处理。
        return None


# ---------------------------------------------------------------------------
# skills.sh 来源适配器
# ---------------------------------------------------------------------------

class SkillsShSource(SkillSource):
    """通过 skills.sh 发现 skill，并从其背后的 GitHub 仓库拉取内容。"""

    BASE_URL = "https://skills.sh"
    SEARCH_URL = f"{BASE_URL}/api/search"
    # 站点地图索引 —— 真正的目录来源。首页抓取只会暴露一条精选的
    # 推荐位（约 200 条）；站点地图覆盖了完整的约 2 万+ 条目录。
    # https://www.skills.sh/sitemap.xml 指向 sitemap-skills-1.xml +
    # sitemap-skills-2.xml，每个最多 1 万个 URL。
    SITEMAP_INDEX_URL = "https://www.skills.sh/sitemap.xml"
    _SITEMAP_LOC_RE = re.compile(r"<loc>([^<]+)</loc>", re.IGNORECASE)
    _SITEMAP_SKILL_RE = re.compile(
        r"^https?://(?:www\.)?skills\.sh/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?P<skill>[^/]+)/?$",
        re.IGNORECASE,
    )
    _SKILL_LINK_RE = re.compile(r'href=["\']/(?P<id>(?!agents/|_next/|api/)[^"\'/]+/[^"\'/]+/[^"\'/]+)["\']')
    _INSTALL_CMD_RE = re.compile(
        r'npx\s+skills\s+add\s+(?P<repo>https?://github\.com/[^\s<]+|[^\s<]+)'
        r'(?:\s+--skill\s+(?P<skill>[^\s<]+))?',
        re.IGNORECASE,
    )
    _PAGE_H1_RE = re.compile(r'<h1[^>]*>(?P<title>.*?)</h1>', re.IGNORECASE | re.DOTALL)
    _PROSE_H1_RE = re.compile(
        r'<div[^>]*class=["\'][^"\']*prose[^"\']*["\'][^>]*>.*?<h1[^>]*>(?P<title>.*?)</h1>',
        re.IGNORECASE | re.DOTALL,
    )
    _PROSE_P_RE = re.compile(
        r'<div[^>]*class=["\'][^"\']*prose[^"\']*["\'][^>]*>.*?<p[^>]*>(?P<body>.*?)</p>',
        re.IGNORECASE | re.DOTALL,
    )
    _WEEKLY_INSTALLS_RE = re.compile(r'Weekly Installs.*?children\\":\\"(?P<count>[0-9.,Kk]+)\\"', re.DOTALL)

    def __init__(self, auth: GitHubAuth):
        self.auth = auth
        self.github = GitHubSource(auth=auth)

    def source_id(self) -> str:
        return "skills-sh"

    def trust_level_for(self, identifier: str) -> str:
        return self.github.trust_level_for(self._normalize_identifier(identifier))

    def search(self, query: str, limit: int = 10) -> List[SkillMeta]:
        if not query.strip():
            # 空查询 = 批量目录导出（build_skills_index.py 就是这么调用的）。
            # 首页抓取只能看到约 200 条推荐条目；站点地图会遍历完整的约 2 万+ 条目录。
            return self._sitemap_catalog(limit)

        cache_key = f"skills_sh_search_{hashlib.md5(f'{query}|{limit}'.encode()).hexdigest()}"
        cached = _read_index_cache(cache_key)
        if cached is not None:
            return [SkillMeta(**item) for item in cached][:limit]

        try:
            resp = httpx.get(
                self.SEARCH_URL,
                params={"q": query, "limit": limit},
                timeout=20,
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError):
            return []

        items = data.get("skills", []) if isinstance(data, dict) else []
        if not isinstance(items, list):
            return []

        results: List[SkillMeta] = []
        for item in items[:limit]:
            meta = self._meta_from_search_item(item)
            if meta:
                results.append(meta)

        _write_index_cache(cache_key, [_skill_meta_to_dict(item) for item in results])
        return results

    def fetch(self, identifier: str) -> Optional[SkillBundle]:
        canonical = self._normalize_identifier(identifier)
        detail = self._fetch_detail_page(canonical)
        for candidate in self._candidate_identifiers(canonical):
            bundle = self.github.fetch(candidate)
            if bundle:
                bundle.source = "skills.sh"
                bundle.identifier = self._wrap_identifier(canonical)
                bundle.metadata.update(self._detail_to_metadata(canonical, detail))
                return bundle

        resolved = self._discover_identifier(canonical, detail=detail)
        if resolved:
            bundle = self.github.fetch(resolved)
            if bundle:
                bundle.source = "skills.sh"
                bundle.identifier = self._wrap_identifier(canonical)
                bundle.metadata.update(self._detail_to_metadata(canonical, detail))
                return bundle
        return None

    def inspect(self, identifier: str) -> Optional[SkillMeta]:
        canonical = self._normalize_identifier(identifier)
        detail = self._fetch_detail_page(canonical)
        meta = self._resolve_github_meta(canonical, detail=detail)
        if meta:
            return self._finalize_inspect_meta(meta, canonical, detail)
        return None

    def _sitemap_catalog(self, limit: int) -> List[SkillMeta]:
        """遍历 skills.sh 的站点地图来枚举完整目录。

        按标准索引 TTL 做缓存，以免每次构建都重新拉取约 2 MB 的站点地图 XML。
        当站点地图不可达或为空时（网络故障、域名变更等），回退到
        ``_featured_skills``。
        """
        cache_key = "skills_sh_sitemap_v1"
        cached = _read_index_cache(cache_key)
        if cached is not None:
            metas = [SkillMeta(**item) for item in cached]
            return metas[:limit] if limit > 0 else metas

        # skills.sh 的按 skill 站点地图是 brotli 压缩的，而 httpx 可选的
        # brotlicffi 后端在流式解码这些特定负载时存在 bug。从 Accept-Encoding
        # 中去掉 "br" 可以让服务器回退到 gzip（或 identity），这在所有 httpx
        # 安装上都正常工作。
        sitemap_headers = {"Accept-Encoding": "gzip"}

        # 第 1 步：拉取站点地图索引 → 得到 skill 站点地图 URL 列表。
        skill_sitemap_urls: List[str] = []
        try:
            resp = httpx.get(
                self.SITEMAP_INDEX_URL,
                timeout=20,
                follow_redirects=True,
                headers=sitemap_headers,
            )
            if resp.status_code != 200:
                return self._featured_skills(limit)
            for match in self._SITEMAP_LOC_RE.finditer(resp.text):
                loc = match.group(1).strip()
                # 站点地图索引中指向按 skill 划分的地图的条目。
                if "sitemap-skills" in loc:
                    skill_sitemap_urls.append(loc)
        except httpx.HTTPError:
            return self._featured_skills(limit)

        if not skill_sitemap_urls:
            return self._featured_skills(limit)

        # 第 2 步：拉取每个 skill 站点地图，收集规范化的 "owner/repo/skill" ID。
        seen: set[str] = set()
        results: List[SkillMeta] = []
        for sitemap_url in skill_sitemap_urls:
            try:
                resp = httpx.get(
                    sitemap_url,
                    timeout=30,
                    follow_redirects=True,
                    headers=sitemap_headers,
                )
                if resp.status_code != 200:
                    continue
            except httpx.HTTPError:
                continue
            for loc_match in self._SITEMAP_LOC_RE.finditer(resp.text):
                url = loc_match.group(1).strip()
                m = self._SITEMAP_SKILL_RE.match(url)
                if not m:
                    continue
                owner = m.group("owner")
                repo_name = m.group("repo")
                skill_name = m.group("skill")
                canonical = f"{owner}/{repo_name}/{skill_name}"
                if canonical in seen:
                    continue
                seen.add(canonical)
                repo = f"{owner}/{repo_name}"
                results.append(SkillMeta(
                    name=skill_name,
                    description=f"Indexed by skills.sh from {repo}",
                    source="skills.sh",
                    identifier=self._wrap_identifier(canonical),
                    trust_level=self.github.trust_level_for(canonical),
                    repo=repo,
                    path=skill_name,
                    extra={
                        "detail_url": f"{self.BASE_URL}/{canonical}",
                        "repo_url": f"https://github.com/{repo}",
                    },
                ))

        if not results:
            return self._featured_skills(limit)

        _write_index_cache(cache_key, [_skill_meta_to_dict(item) for item in results])
        return results[:limit] if limit > 0 else results

    def _featured_skills(self, limit: int) -> List[SkillMeta]:
        cache_key = "skills_sh_featured"
        cached = _read_index_cache(cache_key)
        if cached is not None:
            return [SkillMeta(**item) for item in cached][:limit]

        try:
            resp = httpx.get(self.BASE_URL, timeout=20)
            if resp.status_code != 200:
                return []
        except httpx.HTTPError:
            return []

        seen: set[str] = set()
        results: List[SkillMeta] = []
        for match in self._SKILL_LINK_RE.finditer(resp.text):
            canonical = match.group("id")
            if canonical in seen:
                continue
            seen.add(canonical)
            parts = canonical.split("/", 2)
            if len(parts) < 3:
                continue
            repo = f"{parts[0]}/{parts[1]}"
            skill_path = parts[2]
            results.append(SkillMeta(
                name=skill_path.split("/")[-1],
                description=f"Featured on skills.sh from {repo}",
                source="skills.sh",
                identifier=self._wrap_identifier(canonical),
                trust_level=self.github.trust_level_for(canonical),
                repo=repo,
                path=skill_path,
            ))
            if len(results) >= limit:
                break

        _write_index_cache(cache_key, [_skill_meta_to_dict(item) for item in results])
        return results

    def _meta_from_search_item(self, item: dict) -> Optional[SkillMeta]:
        if not isinstance(item, dict):
            return None

        canonical = item.get("id")
        repo = item.get("source")
        skill_path = item.get("skillId")
        if not isinstance(canonical, str) or canonical.count("/") < 2:
            if not (isinstance(repo, str) and isinstance(skill_path, str)):
                return None
            canonical = f"{repo}/{skill_path}"

        parts = canonical.split("/", 2)
        if len(parts) < 3:
            return None

        repo = f"{parts[0]}/{parts[1]}"
        skill_path = parts[2]
        installs = item.get("installs")
        installs_label = f" · {int(installs):,} installs" if isinstance(installs, int) else ""

        return SkillMeta(
            name=str(item.get("name") or skill_path.split("/")[-1]),
            description=f"Indexed by skills.sh from {repo}{installs_label}",
            source="skills.sh",
            identifier=self._wrap_identifier(canonical),
            trust_level=self.github.trust_level_for(canonical),
            repo=repo,
            path=skill_path,
            extra={
                "installs": installs,
                "detail_url": f"{self.BASE_URL}/{canonical}",
                "repo_url": f"https://github.com/{repo}",
            },
        )

    def _fetch_detail_page(self, identifier: str) -> Optional[dict]:
        cache_key = f"skills_sh_detail_{hashlib.md5(identifier.encode()).hexdigest()}"
        cached = _read_index_cache(cache_key)
        if isinstance(cached, dict):
            return cached

        try:
            resp = httpx.get(f"{self.BASE_URL}/{identifier}", timeout=20)
            if resp.status_code != 200:
                return None
        except httpx.HTTPError:
            return None

        detail = self._parse_detail_page(identifier, resp.text)
        if detail:
            _write_index_cache(cache_key, detail)
        return detail

    def _parse_detail_page(self, identifier: str, html: str) -> Optional[dict]:
        parts = identifier.split("/", 2)
        if len(parts) < 3:
            return None

        default_repo = f"{parts[0]}/{parts[1]}"
        skill_token = parts[2]
        repo = default_repo
        install_skill = skill_token

        install_command = None
        install_match = self._INSTALL_CMD_RE.search(html)
        if install_match:
            install_command = install_match.group(0).strip()
            repo_value = (install_match.group("repo") or "").strip()
            install_skill = (install_match.group("skill") or install_skill).strip()
            repo = self._extract_repo_slug(repo_value) or repo

        page_title = self._extract_first_match(self._PAGE_H1_RE, html)
        body_title = self._extract_first_match(self._PROSE_H1_RE, html)
        body_summary = self._extract_first_match(self._PROSE_P_RE, html)
        weekly_installs = self._extract_weekly_installs(html)
        security_audits = self._extract_security_audits(html, identifier)

        return {
            "repo": repo,
            "install_skill": install_skill,
            "page_title": page_title,
            "body_title": body_title,
            "body_summary": body_summary,
            "weekly_installs": weekly_installs,
            "install_command": install_command,
            "repo_url": f"https://github.com/{repo}",
            "detail_url": f"{self.BASE_URL}/{identifier}",
            "security_audits": security_audits,
        }

    def _discover_identifier(self, identifier: str, detail: Optional[dict] = None) -> Optional[str]:
        parts = identifier.split("/", 2)
        if len(parts) < 3:
            return None

        default_repo = f"{parts[0]}/{parts[1]}"
        repo = detail.get("repo", default_repo) if isinstance(detail, dict) else default_repo
        skill_token=parts[2].split("/")[-1]
        tokens=[skill_token]
        if isinstance(detail, dict):
            tokens.extend([
                detail.get("install_skill", ""),
                detail.get("page_title", ""),
                detail.get("body_title", ""),
            ])

        # 标准 skill 路径
        base_paths = ["skills/", ".agents/skills/", ".claude/skills/"]

        for base_path in base_paths:
            try:
                skills = self.github._list_skills_in_repo(repo, base_path)
            except Exception:
                continue
            for meta in skills:
                if self._matches_skill_tokens(meta, tokens):
                    return meta.identifier

        # 优先做一次递归树查找，再考虑暴力遍历每个顶层目录。
        # 这样可以避免在 borghei/claude-skills 这类分了类的仓库上产生大量请求突发。
        tree_result = self.github._find_skill_in_repo_tree(repo, skill_token)
        if tree_result:
            return tree_result

        # 回退：扫描仓库根目录，查找可能包含 skill 的目录
        try:
            root_url = f"https://api.github.com/repos/{repo}/contents/"
            resp = httpx.get(root_url, headers=self.github.auth.get_headers(),
                             timeout=15, follow_redirects=True)
            if resp.status_code == 200:
                entries = resp.json()
                if isinstance(entries, list):
                    for entry in entries:
                        if entry.get("type") != "dir":
                            continue
                        dir_name = entry["name"]
                        if dir_name.startswith((".", "_")):
                            continue
                        if dir_name in {"skills", ".agents", ".claude"}:
                            continue  # 已经试过
                        # 直接尝试：repo/dir/skill_token
                        direct_id = f"{repo}/{dir_name}/{skill_token}"
                        meta = self.github.inspect(direct_id)
                        if meta:
                            return meta.identifier
                        # 尝试列出该目录下的 skill
                        try:
                            skills = self.github._list_skills_in_repo(repo, dir_name + "/")
                        except Exception:
                            continue
                        for meta in skills:
                            if self._matches_skill_tokens(meta, tokens):
                                return meta.identifier
        except Exception:
            pass

        return None

    def _resolve_github_meta(self, identifier: str, detail: Optional[dict] = None) -> Optional[SkillMeta]:
        for candidate in self._candidate_identifiers(identifier):
            meta = self.github.inspect(candidate)
            if meta:
                return meta

        resolved = self._discover_identifier(identifier, detail=detail)
        if resolved:
            return self.github.inspect(resolved)
        return None

    def _finalize_inspect_meta(self, meta: SkillMeta, canonical: str, detail: Optional[dict]) -> SkillMeta:
        meta.source = "skills.sh"
        meta.identifier = self._wrap_identifier(canonical)
        meta.trust_level = self.trust_level_for(canonical)
        merged_extra = dict(meta.extra)
        merged_extra.update(self._detail_to_metadata(canonical, detail))
        meta.extra = merged_extra

        if isinstance(detail, dict):
            body_summary = detail.get("body_summary")
            weekly_installs = detail.get("weekly_installs")
            if body_summary:
                meta.description = body_summary
            elif meta.description and weekly_installs:
                meta.description = f"{meta.description} · {weekly_installs} weekly installs on skills.sh"
        return meta

    @classmethod
    def _matches_skill_tokens(cls, meta: SkillMeta, skill_tokens: List[str]) -> bool:
        candidates = set()
        candidates.update(cls._token_variants(meta.name))
        candidates.update(cls._token_variants(meta.path))
        candidates.update(cls._token_variants(meta.identifier.split("/", 2)[-1] if meta.identifier else None))

        for token in skill_tokens:
            variants = cls._token_variants(token)
            if variants & candidates:
                return True
        return False

    @staticmethod
    def _token_variants(value: Optional[str]) -> set[str]:
        if not value:
            return set()

        plain = SkillsShSource._strip_html(str(value)).strip().strip("/").lower()
        if not plain:
            return set()

        base = plain.split("/")[-1]
        sanitized = re.sub(r'[^a-z0-9/_-]+', '-', plain).strip('-')
        sanitized_base = sanitized.split("/")[-1] if sanitized else ""
        slash_tail = plain.split("/")[-1]
        slash_tail_clean = slash_tail.lstrip('@')
        slash_tail_clean = slash_tail_clean.split('/')[-1]

        variants = {
            plain,
            plain.replace("_", "-"),
            plain.replace("/", "-"),
            base,
            base.replace("_", "-"),
            base.replace("/", "-"),
            sanitized,
            sanitized.replace("/", "-") if sanitized else "",
            sanitized_base,
            slash_tail_clean,
            slash_tail_clean.replace("_", "-"),
        }
        return {v for v in variants if v}

    @staticmethod
    def _extract_repo_slug(repo_value: str) -> Optional[str]:
        repo_value = repo_value.strip()
        if repo_value.startswith("https://github.com/"):
            repo_value = repo_value[len("https://github.com/"):]
        repo_value = repo_value.strip("/")
        parts = repo_value.split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
        return None

    @staticmethod
    def _extract_first_match(pattern: re.Pattern, text: str) -> Optional[str]:
        match = pattern.search(text)
        if not match:
            return None
        value = next((group for group in match.groups() if group), None)
        if value is None:
            return None
        return SkillsShSource._strip_html(value).strip() or None

    def _detail_to_metadata(self, canonical: str, detail: Optional[dict]) -> Dict[str, Any]:
        parts = canonical.split("/", 2)
        repo = f"{parts[0]}/{parts[1]}" if len(parts) >= 2 else ""
        metadata = {
            "detail_url": f"{self.BASE_URL}/{canonical}",
        }
        if repo:
            metadata["repo_url"] = f"https://github.com/{repo}"
        if isinstance(detail, dict):
            for key in ("weekly_installs", "install_command", "repo_url", "detail_url", "security_audits"):
                value = detail.get(key)
                if value:
                    metadata[key] = value
        return metadata

    @staticmethod
    def _extract_weekly_installs(html: str) -> Optional[str]:
        match = SkillsShSource._WEEKLY_INSTALLS_RE.search(html)
        if not match:
            return None
        return match.group("count")

    @staticmethod
    def _extract_security_audits(html: str, identifier: str) -> Dict[str, str]:
        audits: Dict[str, str] = {}
        for audit in ("agent-trust-hub", "socket", "snyk"):
            idx = html.find(f"/security/{audit}")
            if idx == -1:
                continue
            window = html[idx:idx + 500]
            match = re.search(r'(Pass|Warn|Fail)', window, re.IGNORECASE)
            if match:
                audits[audit] = match.group(1).title()
        return audits

    @staticmethod
    def _strip_html(value: str) -> str:
        return re.sub(r'<[^>]+>', '', value)

    @staticmethod
    def _normalize_identifier(identifier: str) -> str:
        prefix_aliases = (
            "skills-sh/",
            "skills.sh/",
            "skils-sh/",
            "skils.sh/",
        )
        for prefix in prefix_aliases:
            if identifier.startswith(prefix):
                return identifier[len(prefix):]
        return identifier

    @staticmethod
    def _candidate_identifiers(identifier: str) -> List[str]:
        parts = identifier.split("/", 2)
        if len(parts) < 3:
            return [identifier]

        repo = f"{parts[0]}/{parts[1]}"
        skill_path = parts[2].lstrip("/")
        candidates = [
            f"{repo}/{skill_path}",
            f"{repo}/skills/{skill_path}",
            f"{repo}/.agents/skills/{skill_path}",
            f"{repo}/.claude/skills/{skill_path}",
        ]

        seen = set()
        deduped: List[str] = []
        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                deduped.append(candidate)
        return deduped

    @staticmethod
    def _wrap_identifier(identifier: str) -> str:
        return f"skills-sh/{identifier}"


# ---------------------------------------------------------------------------
# ClawHub 来源适配器
# ---------------------------------------------------------------------------

class ClawHubSource(SkillSource):
    """
    通过 HTTP API 从 ClawHub（clawhub.ai）获取 skill。
    所有 skill 一律视为 community 信任级别 —— ClawHavoc 事件表明
    它们的审核并不充分（2026 年 2 月曾发现 341 个恶意 skill）。
    """

    BASE_URL = "https://clawhub.ai/api/v1"

    # 完整目录遍历的墙钟预算。ClawHub 有 5 万+ 个 skill，遍历是串行的
    # （约 250 次请求，每次都在 per-request timeout=30 之内，因此不会报错），
    # 所以一次无限制的遍历可能阻塞数分钟。给它设个上限，避免缓慢/庞大的目录
    # 挂起调用方。
    CATALOG_WALK_BUDGET_SECONDS = 12

    def source_id(self) -> str:
        return "clawhub"

    def trust_level_for(self, identifier: str) -> str:
        return "community"

    @staticmethod
    def _normalize_tags(tags: Any) -> List[str]:
        if isinstance(tags, list):
            return [str(t) for t in tags]
        if isinstance(tags, dict):
            return [str(k) for k in tags if str(k) != "latest"]
        return []

    @staticmethod
    def _coerce_skill_payload(data: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(data, dict):
            return None
        nested = data.get("skill")
        if isinstance(nested, dict):
            merged = dict(nested)
            latest_version = data.get("latestVersion")
            if latest_version is not None and "latestVersion" not in merged:
                merged["latestVersion"] = latest_version
            return merged
        return data

    @staticmethod
    def _query_terms(query: str) -> List[str]:
        return [term for term in re.split(r"[^a-z0-9]+", query.lower()) if term]

    @classmethod
    def _search_score(cls, query: str, meta: SkillMeta) -> int:
        query_norm = query.strip().lower()
        if not query_norm:
            return 1

        identifier = (meta.identifier or "").lower()
        name = (meta.name or "").lower()
        description = (meta.description or "").lower()
        normalized_identifier = " ".join(cls._query_terms(identifier))
        normalized_name = " ".join(cls._query_terms(name))
        query_terms = cls._query_terms(query_norm)
        identifier_terms = cls._query_terms(identifier)
        name_terms = cls._query_terms(name)
        score = 0

        if query_norm == identifier:
            score += 140
        if query_norm == name:
            score += 130
        if normalized_identifier == query_norm:
            score += 125
        if normalized_name == query_norm:
            score += 120
        if normalized_identifier.startswith(query_norm):
            score += 95
        if normalized_name.startswith(query_norm):
            score += 90
        if query_terms and identifier_terms[: len(query_terms)] == query_terms:
            score += 70
        if query_terms and name_terms[: len(query_terms)] == query_terms:
            score += 65
        if query_norm in identifier:
            score += 40
        if query_norm in name:
            score += 35
        if query_norm in description:
            score += 10

        for term in query_terms:
            if term in identifier_terms:
                score += 15
            if term in name_terms:
                score += 12
            if term in description:
                score += 3

        return score

    @staticmethod
    def _dedupe_results(results: List[SkillMeta]) -> List[SkillMeta]:
        seen: set[str] = set()
        deduped: List[SkillMeta] = []
        for result in results:
            key = (result.identifier or result.name).lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(result)
        return deduped

    def _exact_slug_meta(self, query: str) -> Optional[SkillMeta]:
        slug = query.strip().split("/")[-1]
        query_terms = self._query_terms(query)
        candidates: List[str] = []

        if slug and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", slug):
            candidates.append(slug)

        if query_terms:
            base_slug = "-".join(query_terms)
            if len(query_terms) >= 2:
                candidates.extend([
                    f"{base_slug}-agent",
                    f"{base_slug}-skill",
                    f"{base_slug}-tool",
                    f"{base_slug}-assistant",
                    f"{base_slug}-playbook",
                    base_slug,
                ])
            else:
                candidates.append(base_slug)

        seen: set[str] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            meta = self.inspect(candidate)
            if meta:
                return meta

        return None

    def _finalize_search_results(self, query: str, results: List[SkillMeta], limit: int) -> List[SkillMeta]:
        query_norm = query.strip()
        if not query_norm:
            return self._dedupe_results(results)[:limit]

        filtered = [meta for meta in results if self._search_score(query_norm, meta) > 0]
        filtered.sort(
            key=lambda meta: (
                -self._search_score(query_norm, meta),
                meta.name.lower(),
                meta.identifier.lower(),
            )
        )
        filtered = self._dedupe_results(filtered)

        exact = self._exact_slug_meta(query_norm)
        if exact:
            filtered = [meta for meta in filtered if self._search_score(query_norm, meta) >= 20]
            filtered = self._dedupe_results([exact] + filtered)

        if filtered:
            return filtered[:limit]

        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", query_norm):
            return []

        return self._dedupe_results(results)[:limit]

    def search(self, query: str, limit: int = 10) -> List[SkillMeta]:
        query = query.strip()

        if query:
            query_terms = self._query_terms(query)
            if len(query_terms) >= 2:
                direct = self._exact_slug_meta(query)
                if direct:
                    return [direct]

            results = self._search_catalog(query, limit=limit)
            if results:
                return results
        else:
            # 空查询：走分页的目录遍历器。当完整目录已经在磁盘缓存中时，
            # 会整体返回，由调用方在客户端分页。冷缓存时，把遍历上限设为
            # `limit`，这样 browse 命令渲染第一页时不必遍历整个 5 万+ 条目录
            # （max_items=0 → 无限制，仅由离线索引构建器通过 search("", limit=0) 使用）。
            catalog = self._load_catalog_index(max_items=limit if limit > 0 else 0)
            if catalog:
                return self._dedupe_results(catalog)[:limit] if limit > 0 else self._dedupe_results(catalog)

        # 非空查询在目录中未命中，或目录遍历失败：回退到轻量的 listing API
        # 做一次尽力而为的响应。
        cache_key = f"clawhub_search_listing_v1_{hashlib.md5(query.encode()).hexdigest()}_{limit}"
        cached = _read_index_cache(cache_key)
        if cached is not None:
            return self._finalize_search_results(
                query,
                [SkillMeta(**s) for s in cached],
                limit,
            )

        try:
            resp = httpx.get(
                f"{self.BASE_URL}/skills",
                params={"search": query, "limit": limit},
                timeout=15,
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError):
            return []

        skills_data = data.get("items", data) if isinstance(data, dict) else data
        if not isinstance(skills_data, list):
            return []

        results = []
        for item in skills_data[:limit]:
            slug = item.get("slug")
            if not slug:
                continue
            display_name = item.get("displayName") or item.get("name") or slug
            summary = item.get("summary") or item.get("description") or ""
            tags = self._normalize_tags(item.get("tags", []))
            results.append(SkillMeta(
                name=display_name,
                description=summary,
                source="clawhub",
                identifier=slug,
                trust_level="community",
                tags=tags,
            ))

        final_results = self._finalize_search_results(query, results, limit)
        _write_index_cache(cache_key, [_skill_meta_to_dict(s) for s in final_results])
        return final_results

    def fetch(self, identifier: str) -> Optional[SkillBundle]:
        slug = identifier.split("/")[-1]

        skill_data = self._get_json(f"{self.BASE_URL}/skills/{slug}")
        if not isinstance(skill_data, dict):
            return None

        latest_version = self._resolve_latest_version(slug, skill_data)
        if not latest_version:
            logger.warning("ClawHub fetch failed for %s: could not resolve latest version", slug)
            return None

        # 主要方式：从 /download 以 ZIP bundle 形式下载 skill
        files = self._download_zip(slug, latest_version)

        # 回退：尝试版本元数据端点获取内联/原始内容
        if "SKILL.md" not in files:
            version_data = self._get_json(f"{self.BASE_URL}/skills/{slug}/versions/{latest_version}")
            if isinstance(version_data, dict):
                # 文件可能嵌套在 version_data["version"]["files"] 下
                files = self._extract_files(version_data) or files
                if "SKILL.md" not in files:
                    nested = version_data.get("version", {})
                    if isinstance(nested, dict):
                        files = self._extract_files(nested) or files

        if "SKILL.md" not in files:
            logger.warning(
                "ClawHub fetch for %s resolved version %s but could not retrieve file content",
                slug,
                latest_version,
            )
            return None

        return SkillBundle(
            name=slug,
            files=files,
            source="clawhub",
            identifier=slug,
            trust_level="community",
        )

    def inspect(self, identifier: str) -> Optional[SkillMeta]:
        slug = identifier.split("/")[-1]
        data = self._coerce_skill_payload(self._get_json(f"{self.BASE_URL}/skills/{slug}"))
        if not isinstance(data, dict):
            return None

        tags = self._normalize_tags(data.get("tags", []))

        return SkillMeta(
            name=data.get("displayName") or data.get("name") or data.get("slug") or slug,
            description=data.get("summary") or data.get("description") or "",
            source="clawhub",
            identifier=data.get("slug") or slug,
            trust_level="community",
            tags=tags,
        )

    def _search_catalog(self, query: str, limit: int = 10) -> List[SkillMeta]:
        cache_key = f"clawhub_search_catalog_v1_{hashlib.md5(f'{query}|{limit}'.encode()).hexdigest()}"
        cached = _read_index_cache(cache_key)
        if cached is not None:
            return [SkillMeta(**s) for s in cached][:limit]

        catalog = self._load_catalog_index()
        if not catalog:
            return []

        results = self._finalize_search_results(query, catalog, limit)
        _write_index_cache(cache_key, [_skill_meta_to_dict(s) for s in results])
        return results

    def _load_catalog_index(self, max_items: int = 0) -> List[SkillMeta]:
        """通过游标分页遍历 ClawHub 目录。

        ``max_items`` 为遍历设定上限：一旦收集到至少那么多不重复的 skill，
        遍历就提前停止。这正是 browse 冷启动回退所需要的——它只渲染一页，
        所以为了切下前 N 条而遍历整个 5 万+ 目录纯属浪费。
        ``max_items=0``（默认值，由离线索引构建器使用）表示遍历到耗尽。

        缓存：只有 *完整* 的目录（游标耗尽或达到页数上限）才会写入共享的
        ``clawhub_catalog_v1`` 缓存。被 ``max_items`` 或墙钟预算截断的遍历
        是不完整的，缓存它会把不完整的切片污染到完整目录缓存中。
        """
        cache_key = "clawhub_catalog_v1"
        cached = _read_index_cache(cache_key)
        if cached is not None:
            return [SkillMeta(**s) for s in cached]

        cursor: Optional[str] = None
        results: List[SkillMeta] = []
        seen: set[str] = set()
        # 截至 2026 年 5 月，ClawHub 有 5 万+ 个 skill（线上 E2E 遍历到了
        # 49,698 个，且仍有一个活跃游标待处理）；750 页 * 200/页 = 15 万的上限
        # 为目录增长留出了余量。遍历到耗尽通常在 `nextCursor` 变为 None 时
        # 远未达到此上限就终止了——这个上限只是防止无限游标循环的一道护栏。
        max_pages = 750
        # 墙钟预算仅用于交互式 browse（max_items > 0）。离线索引构建器传入
        # max_items=0，必须遍历完整目录——在那里设 12 秒上限只会产出约 3k 个
        # skill 并触发部署健康下限（2 万）。
        deadline = (
            time.monotonic() + self.CATALOG_WALK_BUDGET_SECONDS
            if max_items > 0
            else None
        )
        hit_deadline = False
        hit_max_items = False

        for _ in range(max_pages):
            if deadline is not None and time.monotonic() > deadline:
                hit_deadline = True
                break
            params: Dict[str, Any] = {"limit": 200}
            if cursor:
                params["cursor"] = cursor

            try:
                resp = httpx.get(f"{self.BASE_URL}/skills", params=params, timeout=30)
                if resp.status_code != 200:
                    break
                data = resp.json()
            except (httpx.HTTPError, json.JSONDecodeError):
                break

            items = data.get("items", []) if isinstance(data, dict) else []
            if not isinstance(items, list) or not items:
                break

            for item in items:
                slug = item.get("slug")
                if not isinstance(slug, str) or not slug or slug in seen:
                    continue
                seen.add(slug)
                display_name = item.get("displayName") or item.get("name") or slug
                summary = item.get("summary") or item.get("description") or ""
                tags = self._normalize_tags(item.get("tags", []))
                results.append(SkillMeta(
                    name=display_name,
                    description=summary,
                    source="clawhub",
                    identifier=slug,
                    trust_level="community",
                    tags=tags,
                ))

            cursor = data.get("nextCursor") if isinstance(data, dict) else None
            if not isinstance(cursor, str) or not cursor:
                break

            # Browse 的冷启动回退只渲染一页，所以一旦凑够满足调用方上限的数量
            # 就立即停止。索引构建器传入 max_items=0（无限制），会遍历到耗尽。
            if max_items > 0 and len(results) >= max_items:
                hit_max_items = True
                break

        # 只缓存到达自然停止点（游标耗尽或达到页数上限）的遍历。被墙钟预算
        # 或 max_items 截断的遍历是不完整的，写入它会把不完整的数据污染到共享的
        # 完整目录缓存中。
        if not hit_deadline and not hit_max_items:
            _write_index_cache(cache_key, [_skill_meta_to_dict(s) for s in results])
        return results

    def _get_json(self, url: str, timeout: int = 20) -> Optional[Any]:
        try:
            resp = httpx.get(url, timeout=timeout)
            if resp.status_code != 200:
                return None
            return resp.json()
        except (httpx.HTTPError, json.JSONDecodeError):
            return None

    def _resolve_latest_version(self, slug: str, skill_data: Dict[str, Any]) -> Optional[str]:
        latest = skill_data.get("latestVersion")
        if isinstance(latest, dict):
            version = latest.get("version")
            if isinstance(version, str) and version:
                return version

        tags = skill_data.get("tags")
        if isinstance(tags, dict):
            latest_tag = tags.get("latest")
            if isinstance(latest_tag, str) and latest_tag:
                return latest_tag

        versions_data = self._get_json(f"{self.BASE_URL}/skills/{slug}/versions")
        if isinstance(versions_data, list) and versions_data:
            first = versions_data[0]
            if isinstance(first, dict):
                version = first.get("version")
                if isinstance(version, str) and version:
                    return version
        return None

    def _extract_files(self, version_data: Dict[str, Any]) -> Dict[str, str]:
        files: Dict[str, str] = {}
        file_list = version_data.get("files")

        if isinstance(file_list, dict):
            return {k: v for k, v in file_list.items() if isinstance(v, str)}

        if not isinstance(file_list, list):
            return files

        for file_meta in file_list:
            if not isinstance(file_meta, dict):
                continue

            fname = file_meta.get("path") or file_meta.get("name")
            if not fname or not isinstance(fname, str):
                continue

            inline_content = file_meta.get("content")
            if isinstance(inline_content, str):
                files[fname] = inline_content
                continue

            raw_url = file_meta.get("rawUrl") or file_meta.get("downloadUrl") or file_meta.get("url")
            if isinstance(raw_url, str) and raw_url.startswith("http"):
                content = self._fetch_text(raw_url)
                if content is not None:
                    files[fname] = content

        return files

    def _download_zip(self, slug: str, version: str) -> Dict[str, str]:
        """从 /download 端点以 ZIP bundle 形式下载 skill，并解压出文本文件。"""
        import io
        import zipfile

        files: Dict[str, str] = {}
        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = httpx.get(
                    f"{self.BASE_URL}/download",
                    params={"slug": slug, "version": version},
                    timeout=30,
                    follow_redirects=True,
                )
                if resp.status_code == 429:
                    try:
                        retry_after = int(resp.headers.get("retry-after", "5"))
                    except (ValueError, TypeError):
                        retry_after = 5
                    retry_after = min(retry_after, 15)  # 等待时间上限
                    logger.debug(
                        "ClawHub download rate-limited for %s, retrying in %ds (attempt %d/%d)",
                        slug, retry_after, attempt + 1, max_retries,
                    )
                    time.sleep(retry_after)
                    continue
                if resp.status_code != 200:
                    logger.debug("ClawHub ZIP download for %s v%s returned %s", slug, version, resp.status_code)
                    return files

                with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        try:
                            name = _validate_bundle_rel_path(info.filename)
                        except ValueError:
                            logger.debug("Skipping unsafe ZIP member path: %s", info.filename)
                            continue
                        # 只解压文本大小的文件（跳过大二进制文件）
                        if info.file_size > 500_000:
                            logger.debug("Skipping large file in ZIP: %s (%d bytes)", name, info.file_size)
                            continue
                        try:
                            raw = zf.read(info.filename)
                            files[name] = raw.decode("utf-8")
                        except (UnicodeDecodeError, KeyError):
                            logger.debug("Skipping non-text file in ZIP: %s", name)
                            continue

                return files

            except zipfile.BadZipFile:
                logger.warning("ClawHub returned invalid ZIP for %s v%s", slug, version)
                return files
            except httpx.HTTPError as exc:
                logger.debug("ClawHub ZIP download failed for %s v%s: %s", slug, version, exc)
                return files

        logger.debug("ClawHub ZIP download exhausted retries for %s v%s", slug, version)
        return files

    def _fetch_text(self, url: str) -> Optional[str]:
        resp = _guarded_http_get(url, timeout=20)
        if resp is not None and resp.status_code == 200:
            return resp.text
        return None


# ---------------------------------------------------------------------------
# Claude Code marketplace 来源适配器
# ---------------------------------------------------------------------------

class ClaudeMarketplaceSource(SkillSource):
    """
    从 Claude Code marketplace 仓库中发现 skill。
    marketplace 仓库内含 .claude-plugin/marketplace.json，其中列出各个插件。
    """

    KNOWN_MARKETPLACES = [
        "anthropics/skills",
        "aiskillstore/marketplace",
    ]

    def __init__(self, auth: GitHubAuth):
        self.auth = auth
        # 持久化的 GitHubSource，让速率限制状态能在 marketplace 索引拉取
        # + 按 skill 的 inspect 调用之间保留，并暴露给索引构建器（见 is_rate_limited）。
        self.github = GitHubSource(auth=auth)

    def source_id(self) -> str:
        return "claude-marketplace"

    @property
    def is_rate_limited(self) -> bool:
        """底层 GitHub API 在爬取过程中是否触发了速率限制。"""
        return self.github.is_rate_limited

    def trust_level_for(self, identifier: str) -> str:
        parts = identifier.split("/", 2)
        if len(parts) >= 2:
            repo = f"{parts[0]}/{parts[1]}"
            if repo in TRUSTED_REPOS:
                return "trusted"
        return "community"

    def search(self, query: str, limit: int = 10) -> List[SkillMeta]:
        results: List[SkillMeta] = []
        query_lower = query.lower()

        for marketplace_repo in self.KNOWN_MARKETPLACES:
            plugins = self._fetch_marketplace_index(marketplace_repo)
            for plugin in plugins:
                searchable = f"{plugin.get('name', '')} {plugin.get('description', '')}".lower()
                if query_lower in searchable:
                    source_path = plugin.get("source", "")
                    if source_path.startswith("./"):
                        identifier = f"{marketplace_repo}/{source_path[2:]}"
                    elif "/" in source_path:
                        identifier = source_path
                    else:
                        identifier = f"{marketplace_repo}/{source_path}"

                    results.append(SkillMeta(
                        name=plugin.get("name", ""),
                        description=plugin.get("description", ""),
                        source="claude-marketplace",
                        identifier=identifier,
                        trust_level=self.trust_level_for(identifier),
                        repo=marketplace_repo,
                    ))

        return results[:limit]

    def fetch(self, identifier: str) -> Optional[SkillBundle]:
        # 委托给 GitHub Contents API，因为 marketplace skill 实际存在于 GitHub 仓库中
        bundle = self.github.fetch(identifier)
        if bundle:
            bundle.source = "claude-marketplace"
        return bundle

    def inspect(self, identifier: str) -> Optional[SkillMeta]:
        meta = self.github.inspect(identifier)
        if meta:
            meta.source = "claude-marketplace"
            meta.trust_level = self.trust_level_for(identifier)
        return meta

    def _fetch_marketplace_index(self, repo: str) -> List[dict]:
        """从仓库中拉取并解析 .claude-plugin/marketplace.json。"""
        cache_key = f"claude_marketplace_{repo.replace('/', '_')}"
        cached = _read_index_cache(cache_key)
        if cached is not None:
            return cached

        url = f"https://api.github.com/repos/{repo}/contents/.claude-plugin/marketplace.json"
        resp = self.github._github_get(
            url,
            headers={**self.auth.get_headers(), "Accept": "application/vnd.github.v3.raw"},
        )
        if resp is None or resp.status_code != 200:
            return []
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError:
            return []

        plugins = data.get("plugins", [])
        _write_index_cache(cache_key, plugins)
        return plugins


# ---------------------------------------------------------------------------
# LobeHub 来源适配器
# ---------------------------------------------------------------------------

class LobeHubSource(SkillSource):
    """
    从 LobeHub 的 agent marketplace（14,500+ 个 agent）获取 skill。
    LobeHub 的 agent 本质上是系统提示词模板 —— 我们在抓取时把它们转换成 SKILL.md。
    数据存放在 GitHub：lobehub/lobe-chat-agents。
    """

    INDEX_URL = "https://chat-agents.lobehub.com/index.json"

    def source_id(self) -> str:
        return "lobehub"

    def trust_level_for(self, identifier: str) -> str:
        return "community"

    def search(self, query: str, limit: int = 10) -> List[SkillMeta]:
        index = self._fetch_index()
        if not index:
            return []

        query_lower = query.lower()
        results: List[SkillMeta] = []

        agents = index.get("agents", index) if isinstance(index, dict) else index
        if not isinstance(agents, list):
            return []

        for agent in agents:
            meta = agent.get("meta", agent)
            title = meta.get("title", agent.get("identifier", ""))
            desc = meta.get("description", "")
            tags = meta.get("tags", [])

            searchable = f"{title} {desc} {' '.join(tags) if isinstance(tags, list) else ''}".lower()
            if query_lower in searchable:
                identifier = agent.get("identifier", title.lower().replace(" ", "-"))
                results.append(SkillMeta(
                    name=identifier,
                    description=desc[:200],
                    source="lobehub",
                    identifier=f"lobehub/{identifier}",
                    trust_level="community",
                    tags=tags if isinstance(tags, list) else [],
                ))

            if len(results) >= limit:
                break

        return results

    def fetch(self, identifier: str) -> Optional[SkillBundle]:
        # 如果存在 "lobehub/" 前缀则去掉
        agent_id = identifier.split("/", 1)[-1] if identifier.startswith("lobehub/") else identifier

        agent_data = self._fetch_agent(agent_id)
        if not agent_data:
            return None

        skill_md = self._convert_to_skill_md(agent_data)
        return SkillBundle(
            name=agent_id,
            files={"SKILL.md": skill_md},
            source="lobehub",
            identifier=f"lobehub/{agent_id}",
            trust_level="community",
        )

    def inspect(self, identifier: str) -> Optional[SkillMeta]:
        agent_id = identifier.split("/", 1)[-1] if identifier.startswith("lobehub/") else identifier
        index = self._fetch_index()
        if not index:
            return None

        agents = index.get("agents", index) if isinstance(index, dict) else index
        if not isinstance(agents, list):
            return None

        for agent in agents:
            if agent.get("identifier") == agent_id:
                meta = agent.get("meta", agent)
                return SkillMeta(
                    name=agent_id,
                    description=meta.get("description", ""),
                    source="lobehub",
                    identifier=f"lobehub/{agent_id}",
                    trust_level="community",
                    tags=meta.get("tags", []) if isinstance(meta.get("tags"), list) else [],
                )
        return None

    def _fetch_index(self) -> Optional[Any]:
        """拉取 LobeHub 的 agent 索引（缓存 1 小时）。"""
        cache_key = "lobehub_index"
        cached = _read_index_cache(cache_key)
        if cached is not None:
            return cached

        try:
            resp = httpx.get(self.INDEX_URL, timeout=30)
            if resp.status_code != 200:
                return None
            data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError):
            return None

        _write_index_cache(cache_key, data)
        return data

    def _fetch_agent(self, agent_id: str) -> Optional[dict]:
        """拉取单个 agent 的 JSON 文件。"""
        url = f"https://chat-agents.lobehub.com/{agent_id}.json"
        try:
            resp = httpx.get(url, timeout=15)
            if resp.status_code == 200:
                return resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            logger.debug("LobeHub agent fetch failed: %s", e)
        return None

    @staticmethod
    def _convert_to_skill_md(agent_data: dict) -> str:
        """把 LobeHub 的 agent JSON 转换成 SKILL.md 格式。"""
        meta = agent_data.get("meta", agent_data)
        identifier = agent_data.get("identifier", "lobehub-agent")
        title = meta.get("title", identifier)
        description = meta.get("description", "")
        tags = meta.get("tags", [])
        system_role = agent_data.get("config", {}).get("systemRole", "")

        tag_list = tags if isinstance(tags, list) else []
        fm_lines = [
            "---",
            f"name: {identifier}",
            f"description: {description[:500]}",
            "metadata:",
            "  hermes:",
            f"    tags: [{', '.join(str(t) for t in tag_list)}]",
            "  lobehub:",
            "    source: lobehub",
            "---",
        ]

        body_lines = [
            f"# {title}",
            "",
            description,
            "",
            "## Instructions",
            "",
            system_role if system_role else "(No system role defined)",
        ]

        return "\n".join(fm_lines) + "\n\n" + "\n".join(body_lines) + "\n"


# ---------------------------------------------------------------------------
# browse.sh 来源适配器
# ---------------------------------------------------------------------------


class BrowseShSource(SkillSource):
    """从 browse.sh 发现并安装站点专用的浏览器自动化 skill。

    browse.sh（https://browse.sh）是 Browserbase 提供的目录，包含 200+ 个
    SKILL.md 文件，描述如何自动化特定网站（Airbnb、Amazon、arXiv 等）。
    目录位于 ``/api/skills``，每个 skill 实际的 SKILL.md 内容通过
    ``/api/skills/{slug}`` 拉取，该端点返回一个 ``skillMdUrl`` 字段，指向
    CDN 上的一个 blob —— 目录中的 ``sourceUrl`` 字段是一个 GitHub HTML URL，
    其背后的仓库并不总是公开的，因此不能依赖它来抓取内容。
    """

    CATALOG_URL = "https://browse.sh/api/skills"
    SKILL_DETAIL_URL = "https://browse.sh/api/skills/{slug}"
    _CACHE_KEY = "browse_sh_catalog"

    def source_id(self) -> str:
        return "browse-sh"

    def trust_level_for(self, identifier: str) -> str:
        return "community"

    def _fetch_catalog(self) -> List[Dict]:
        cached = _read_index_cache(self._CACHE_KEY)
        if cached is not None:
            return cached
        try:
            resp = httpx.get(self.CATALOG_URL, timeout=20)
            if resp.status_code != 200:
                return []
            data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError):
            return []
        skills = data.get("skills", []) if isinstance(data, dict) else []
        if isinstance(skills, list):
            _write_index_cache(self._CACHE_KEY, skills)
        return skills if isinstance(skills, list) else []

    def _item_to_meta(self, item: Dict) -> Optional[SkillMeta]:
        slug = item.get("slug", "")
        name = item.get("name", "")
        title = item.get("title", name)
        description = item.get("description", title)
        if not slug or not name:
            return None
        if len(description) > 1024:
            description = description[:1021] + "..."
        return SkillMeta(
            name=name,
            description=description,
            source="browse-sh",
            identifier=f"browse-sh/{slug}",
            trust_level="community",
            tags=item.get("tags", []),
            extra={
                "slug": slug,
                "hostname": item.get("hostname", ""),
                "category": item.get("category", ""),
                "source_url": item.get("sourceUrl", ""),
                "recommended_method": item.get("recommendedMethod", ""),
                "proxies": item.get("proxies", False),
                "install_count": item.get("installCount", 0),
            },
        )

    def search(self, query: str, limit: int = 10) -> List[SkillMeta]:
        catalog = self._fetch_catalog()
        query_lower = query.lower()
        results = []
        for item in catalog:
            text = " ".join([
                item.get("name", ""),
                item.get("title", ""),
                item.get("description", ""),
                item.get("hostname", ""),
                item.get("category", ""),
                " ".join(item.get("tags", [])),
            ]).lower()
            if not query_lower or query_lower in text:
                meta = self._item_to_meta(item)
                if meta:
                    results.append(meta)
            if len(results) >= limit:
                break
        return results

    def inspect(self, identifier: str) -> Optional[SkillMeta]:
        slug = self._slug_from_identifier(identifier)
        if not slug:
            return None
        catalog = self._fetch_catalog()
        for item in catalog:
            if item.get("slug") == slug:
                return self._item_to_meta(item)
        return None

    def fetch(self, identifier: str) -> Optional[SkillBundle]:
        slug = self._slug_from_identifier(identifier)
        if not slug:
            return None
        catalog = self._fetch_catalog()
        item = next((i for i in catalog if i.get("slug") == slug), None)
        if not item:
            return None

        # 通过按 skill 的详情端点解析出真正的 SKILL.md 内容 URL，该端点返回
        # 一个 ``skillMdUrl``（CDN blob）。目录中的 ``sourceUrl`` 是一个
        # GitHub HTML 链接，其背后的仓库并不总是公开的，因此我们不用于抓取内容。
        md_url = self._resolve_skill_md_url(slug, item)
        if not md_url:
            return None
        try:
            resp = httpx.get(md_url, timeout=20, follow_redirects=True)
            if resp.status_code != 200:
                return None
            content = resp.text
        except httpx.HTTPError:
            return None

        meta = self._item_to_meta(item)
        name = meta.name if meta else slug.split("/")[-1]
        return SkillBundle(
            name=name,
            files={"SKILL.md": content},
            source="browse-sh",
            identifier=identifier,
            trust_level="community",
            metadata={
                "slug": slug,
                "hostname": item.get("hostname", ""),
                "source_url": item.get("sourceUrl", ""),
                "skill_md_url": md_url,
            },
        )

    def _resolve_skill_md_url(self, slug: str, item: Dict) -> Optional[str]:
        """为某个 slug 解析出 SKILL.md 的内容 URL。

        主路径：请求 ``/api/skills/{slug}`` 并读取 ``skillMdUrl``。
        回退：如果目录条目已经带有一个 ``raw.githubusercontent.com`` 的
        ``sourceUrl``（部分条目可能有），则直接使用它。
        """
        try:
            detail = httpx.get(
                self.SKILL_DETAIL_URL.format(slug=slug),
                timeout=20,
                follow_redirects=True,
            )
            if detail.status_code == 200:
                data = detail.json()
                if isinstance(data, dict):
                    md_url = data.get("skillMdUrl")
                    if isinstance(md_url, str) and md_url.startswith("http"):
                        return md_url
        except (httpx.HTTPError, json.JSONDecodeError):
            pass

        source_url = item.get("sourceUrl", "") if isinstance(item, dict) else ""
        if source_url and "raw.githubusercontent.com" in source_url:
            return source_url
        return None

    def _slug_from_identifier(self, identifier: str) -> str:
        """从形如 'browse-sh/airbnb.com/search-listings-abc' 的 identifier 中提取 slug。"""
        if identifier.startswith("browse-sh/"):
            return identifier[len("browse-sh/"):]
        return identifier


# ---------------------------------------------------------------------------
# 官方可选 skill 来源适配器
# ---------------------------------------------------------------------------

class OptionalSkillSource(SkillSource):
    """
    从随仓库分发的 optional-skills/ 目录中获取 skill。

    这些 skill 是官方的（由 Nous Research 维护），但默认不启用——它们不会
    出现在系统提示词中，也不会在 setup 时复制到 ~/.hermes/skills/。它们可以
    通过 Skills Hub（search / install / inspect）发现，并标注为 "official"、
    信任级别为 "builtin"。
    """

    def __init__(self):
        from hermes_constants import get_optional_skills_dir

        self._optional_dir = get_optional_skills_dir(
            Path(__file__).parent.parent / "optional-skills"
        )

    def source_id(self) -> str:
        return "official"

    def trust_level_for(self, identifier: str) -> str:
        return "builtin"

    # -- 搜索 -------------------------------------------------------------

    def search(self, query: str, limit: int = 10) -> List[SkillMeta]:
        results: List[SkillMeta] = []
        query_lower = query.lower()

        for meta in self._scan_all():
            searchable = f"{meta.name} {meta.description} {' '.join(meta.tags)}".lower()
            if query_lower in searchable:
                results.append(meta)
            if len(results) >= limit:
                break

        return results

    # -- 抓取 ------------------------------------------------------------

    def fetch(self, identifier: str) -> Optional[SkillBundle]:
        # identifier 格式："official/category/skill" 或 "official/skill"
        rel = identifier.split("/", 1)[-1] if identifier.startswith("official/") else identifier
        skill_dir = self._optional_dir / rel

        # 防御目录穿越（例如 "official/../../etc"）
        try:
            resolved = skill_dir.resolve()
            if not str(resolved).startswith(str(self._optional_dir.resolve())):
                return None
        except (OSError, ValueError):
            return None

        if not resolved.is_dir():
            # 尝试仅按 skill 名（最后一段）搜索
            skill_name = rel.rsplit("/", 1)[-1]
            skill_dir = self._find_skill_dir(skill_name)
            if not skill_dir:
                return None
        else:
            skill_dir = resolved

        files: Dict[str, Union[str, bytes]] = {}
        for f in skill_dir.rglob("*"):
            if (
                f.is_file()
                and not f.name.startswith(".")
                and "__pycache__" not in f.parts
                and f.suffix != ".pyc"
            ):
                rel_path = str(f.relative_to(skill_dir))
                try:
                    files[rel_path] = f.read_bytes()
                except OSError:
                    continue

        if not files:
            return None

        # 从目录结构中确定分类
        name = skill_dir.name

        return SkillBundle(
            name=name,
            files=files,
            source="official",
            identifier=f"official/{skill_dir.relative_to(self._optional_dir)}",
            trust_level="builtin",
        )

    # -- 检视 -------------------------------------------------------------

    def inspect(self, identifier: str) -> Optional[SkillMeta]:
        rel = identifier.split("/", 1)[-1] if identifier.startswith("official/") else identifier
        skill_name = rel.rsplit("/", 1)[-1]

        for meta in self._scan_all():
            if meta.name == skill_name:
                return meta
        return None

    # -- 内部辅助方法 -----------------------------------------------------

    def _find_skill_dir(self, name: str) -> Optional[Path]:
        """按名字在 optional-skills/ 任意位置查找 skill 目录。"""
        if not self._optional_dir.is_dir():
            return None
        for skill_md in self._optional_dir.rglob("SKILL.md"):
            if is_excluded_skill_path(skill_md):
                continue
            if skill_md.parent.name == name:
                return skill_md.parent
        return None

    def _scan_all(self) -> List[SkillMeta]:
        """枚举所有可选 skill 及其元数据。"""
        if not self._optional_dir.is_dir():
            return []

        results: List[SkillMeta] = []
        for skill_md in sorted(self._optional_dir.rglob("SKILL.md")):
            if is_excluded_skill_path(skill_md):
                continue
            parent = skill_md.parent

            try:
                content = skill_md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            fm = self._parse_frontmatter(content)
            name = fm.get("name", parent.name)
            desc = fm.get("description", "")
            tags = []
            meta_block = fm.get("metadata", {})
            if isinstance(meta_block, dict):
                hermes_meta = meta_block.get("hermes", {})
                if isinstance(hermes_meta, dict):
                    tags = hermes_meta.get("tags", [])

            rel_path = str(parent.relative_to(self._optional_dir))

            results.append(SkillMeta(
                name=name,
                description=desc[:200],
                source="official",
                identifier=f"official/{rel_path}",
                trust_level="builtin",
                path=rel_path,
                tags=tags if isinstance(tags, list) else [],
            ))

        return results

    @staticmethod
    def _parse_frontmatter(content: str) -> dict:
        """从 SKILL.md 内容中解析 YAML frontmatter。"""
        if not content.startswith("---"):
            return {}
        match = re.search(r'\n---\s*\n', content[3:])
        if not match:
            return {}
        yaml_text = content[3:match.start() + 3]
        try:
            parsed = yaml.safe_load(yaml_text)
            return parsed if isinstance(parsed, dict) else {}
        except yaml.YAMLError:
            return {}


# ---------------------------------------------------------------------------
# 共享缓存辅助函数（被多个适配器使用）
# ---------------------------------------------------------------------------

def _read_index_cache(key: str) -> Optional[Any]:
    """读取未过期的缓存数据。"""
    cache_file = INDEX_CACHE_DIR / f"{key}.json"
    if not cache_file.exists():
        return None
    try:
        stat = cache_file.stat()
        if time.time() - stat.st_mtime > INDEX_CACHE_TTL:
            return None
        return json.loads(cache_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_index_cache(key: str, data: Any) -> None:
    """把数据写入缓存。"""
    INDEX_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # 确保 .ignore 存在，以便 ripgrep（以及遵守 .ignore 的工具）跳过此目录。
    # 缓存文件中含有未经审核的社区内容，可能包含对抗性文本
    # （通过目录条目进行的提示词注入）。
    ignore_file = HUB_DIR / ".ignore"
    if not ignore_file.exists():
        try:
            ignore_file.write_text("# Exclude hub internals from search tools\n*\n")
        except OSError:
            pass
    cache_file = INDEX_CACHE_DIR / f"{key}.json"
    try:
        cache_file.write_text(json.dumps(data, ensure_ascii=False, default=str))
    except OSError as e:
        logger.debug("Could not write cache: %s", e)


def _skill_meta_to_dict(meta: SkillMeta) -> dict:
    """把 SkillMeta 转成字典以便缓存。"""
    return {
        "name": meta.name,
        "description": meta.description,
        "source": meta.source,
        "identifier": meta.identifier,
        "trust_level": meta.trust_level,
        "repo": meta.repo,
        "path": meta.path,
        "tags": meta.tags,
        "extra": meta.extra,
    }


# ---------------------------------------------------------------------------
# Lock 文件管理
# ---------------------------------------------------------------------------

class HubLockFile:
    """管理 skills/.hub/lock.json —— 跟踪已安装 hub skill 的来源信息。"""

    def __init__(self, path: Path = LOCK_FILE):
        self.path = path

    def load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "installed": {}}
        try:
            return json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return {"version": 1, "installed": {}}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

    def record_install(
        self,
        name: str,
        source: str,
        identifier: str,
        trust_level: str,
        scan_verdict: str,
        skill_hash: str,
        install_path: str,
        files: List[str],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        # 在写入 lock.json 之前，同时校验 skill 名和安装路径的形状。
        # 一个被投毒的 lock 条目是 uninstall_skill 发生 rmtree 逃逸的前提条件；
        # 在写入时拒绝畸形输入，使文件永远不会带有这种坏状态。
        safe_name = _validate_skill_name(name)
        safe_install_path = _normalize_lock_install_path(install_path, safe_name)
        data = self.load()
        data["installed"][safe_name] = {
            "source": source,
            "identifier": identifier,
            "trust_level": trust_level,
            "scan_verdict": scan_verdict,
            "content_hash": skill_hash,
            "install_path": safe_install_path,
            "files": files,
            "metadata": metadata or {},
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.save(data)

    def record_uninstall(self, name: str) -> None:
        data = self.load()
        data["installed"].pop(name, None)
        self.save(data)

    def get_installed(self, name: str) -> Optional[dict]:
        data = self.load()
        return data["installed"].get(name)

    def list_installed(self) -> List[dict]:
        data = self.load()
        result = []
        for name, entry in data["installed"].items():
            result.append({"name": name, **entry})
        return result


# ---------------------------------------------------------------------------
# Tap 管理
# ---------------------------------------------------------------------------

class TapsManager:
    """管理 taps.json 文件 —— 自定义的 GitHub 仓库来源。"""

    def __init__(self, path: Path = TAPS_FILE):
        self.path = path

    def load(self) -> List[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text())
            return data.get("taps", [])
        except (json.JSONDecodeError, OSError):
            return []

    def save(self, taps: List[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"taps": taps}, indent=2) + "\n")

    def add(self, repo: str, path: str = "skills/") -> bool:
        """添加一个 tap。若已存在则返回 False。"""
        taps = self.load()
        if any(t["repo"] == repo for t in taps):
            return False
        taps.append({"repo": repo, "path": path})
        self.save(taps)
        return True

    def remove(self, repo: str) -> bool:
        """按仓库名移除一个 tap。若未找到则返回 False。"""
        taps = self.load()
        new_taps = [t for t in taps if t["repo"] != repo]
        if len(new_taps) == len(taps):
            return False
        self.save(new_taps)
        return True

    def list_taps(self) -> List[dict]:
        return self.load()


# ---------------------------------------------------------------------------
# 审计日志
# ---------------------------------------------------------------------------

def append_audit_log(action: str, skill_name: str, source: str,
                     trust_level: str, verdict: str, extra: str = "") -> None:
    """向审计日志追加一行。"""
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [timestamp, action, skill_name, f"{source}:{trust_level}", verdict]
    if extra:
        parts.append(extra)
    line = " ".join(parts) + "\n"
    try:
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        logger.debug("Could not write audit log: %s", e)


# ---------------------------------------------------------------------------
# Hub 操作（高层）
# ---------------------------------------------------------------------------

def ensure_hub_dirs() -> None:
    """如果 .hub 目录结构不存在则创建它。"""
    HUB_DIR.mkdir(parents=True, exist_ok=True)
    QUARANTINE_DIR.mkdir(exist_ok=True)
    INDEX_CACHE_DIR.mkdir(exist_ok=True)
    if not LOCK_FILE.exists():
        LOCK_FILE.write_text('{"version": 1, "installed": {}}\n')
    if not AUDIT_LOG.exists():
        AUDIT_LOG.touch()
    if not TAPS_FILE.exists():
        TAPS_FILE.write_text('{"taps": []}\n')


def quarantine_bundle(bundle: SkillBundle) -> Path:
    """把一个 skill bundle 写入隔离区目录以供扫描。"""
    ensure_hub_dirs()
    skill_name = _validate_skill_name(bundle.name)
    validated_files: List[Tuple[str, Union[str, bytes]]] = []
    for rel_path, file_content in bundle.files.items():
        safe_rel_path = _validate_bundle_rel_path(rel_path)
        validated_files.append((safe_rel_path, file_content))

    dest = QUARANTINE_DIR / skill_name
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    for rel_path, file_content in validated_files:
        file_dest = dest.joinpath(*rel_path.split("/"))
        file_dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(file_content, bytes):
            file_dest.write_bytes(file_content)
        else:
            file_dest.write_text(file_content, encoding="utf-8")

    return dest


def install_from_quarantine(
    quarantine_path: Path,
    skill_name: str,
    category: str,
    bundle: SkillBundle,
    scan_result: ScanResult,
) -> Path:
    """把已扫描的 skill 从隔离区移入 skills 目录。"""
    safe_skill_name = _validate_skill_name(skill_name)
    safe_category = _validate_install_parent_path(category) if category else ""
    quarantine_resolved = quarantine_path.resolve()
    quarantine_root = QUARANTINE_DIR.resolve()
    if not quarantine_resolved.is_relative_to(quarantine_root):
        raise ValueError(f"Unsafe quarantine path: {quarantine_path}")

    if safe_category:
        install_rel_path = f"{safe_category}/{safe_skill_name}"
    else:
        install_rel_path = safe_skill_name

    # 通过与卸载器相同的 lock 路径校验器做解析。在安装时就能捕获 skills
    # 目录树中的符号链接重定向，使 lock 条目的路径永远不会指向被重定向的目标。
    install_dir = _resolve_lock_install_path(install_rel_path, safe_skill_name)

    if install_dir.exists():
        shutil.rmtree(install_dir)

    # 若 SKILL.md 非常大则告警（但不阻止安装）
    skill_md = quarantine_path / "SKILL.md"
    if skill_md.exists():
        try:
            skill_size = skill_md.stat().st_size
            if skill_size > 100_000:
                logger.warning(
                    "Skill '%s' has a large SKILL.md (%s chars). "
                    "Large skills consume significant context when loaded. "
                    "Consider asking the author to split it into smaller files.",
                    safe_skill_name,
                    f"{skill_size:,}",
                )
        except OSError:
            pass

    # 在移动之前，拒绝隔离 skill 内部的符号链接。
    # 恶意的 skill bundle 可能包含指向 skills 目录树外部的符号链接；
    # 其目标内容随后会被复制到 skills/ 中，并在下一次 skill_view 调用时
    # 泄露给 agent。
    for entry in quarantine_path.rglob("*"):
        if not _is_path_redirect(entry):
            continue
        try:
            rel = entry.relative_to(quarantine_resolved)
        except ValueError:
            rel = entry
        raise ValueError(
            f"Installed skill contains symlinks, which is not allowed: {rel}"
        )

    install_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(quarantine_path), str(install_dir))

    # 记录到 lock 文件
    lock = HubLockFile()
    lock.record_install(
        name=safe_skill_name,
        source=bundle.source,
        identifier=bundle.identifier,
        trust_level=bundle.trust_level,
        scan_verdict=scan_result.verdict,
        skill_hash=content_hash(install_dir),
        install_path=str(install_dir.relative_to(SKILLS_DIR)),
        files=list(bundle.files.keys()),
        metadata=bundle.metadata,
    )

    append_audit_log(
        "INSTALL", safe_skill_name, bundle.source,
        bundle.trust_level, scan_result.verdict,
        content_hash(install_dir),
    )

    return install_dir


def uninstall_skill(skill_name: str) -> Tuple[bool, str]:
    """移除一个 hub 安装的 skill。拒绝移除内置 skill。"""
    lock = HubLockFile()
    entry = lock.get_installed(skill_name)
    if not entry:
        return False, f"'{skill_name}' is not a hub-installed skill (may be a builtin)"

    # 把 lock 条目的 install_path 对照 skill 名做校验。这是破坏性操作的边界——
    # 任何进入下方 rmtree 的路径都必须位于 SKILLS_DIR 内部，且绝不能是
    # SKILLS_DIR 本身（否则空的 / "." / "/" 的 install_path 会清空整棵目录树）。
    # _resolve_lock_install_path 强制要求相对路径并以 <skill_name> 结尾，
    # 拒绝绝对路径/目录穿越路径，并逐级遍历路径组件、拒绝符号链接/联接重定向。
    try:
        install_path = _resolve_lock_install_path(
            entry.get("install_path", ""), skill_name
        )
    except ValueError as exc:
        return False, f"Refusing to uninstall '{skill_name}': {exc}"

    if install_path.exists():
        shutil.rmtree(install_path)

    lock.record_uninstall(skill_name)
    append_audit_log("UNINSTALL", skill_name, entry["source"], entry["trust_level"], "n/a", "user_request")

    return True, f"Uninstalled '{skill_name}' from {entry['install_path']}"


def bundle_content_hash(bundle: SkillBundle) -> str:
    """为内存中的 skill bundle 计算确定性哈希。"""
    h = hashlib.sha256()
    for rel_path in sorted(bundle.files):
        # 把路径也纳入哈希，这样在两个路径之间交换文件内容会改变哈希
        # （避免通过文件名互换绕过更新检测）。
        h.update(rel_path.encode("utf-8"))
        h.update(b"\x00")
        content = bundle.files[rel_path]
        if isinstance(content, bytes):
            h.update(content)
        else:
            h.update(content.encode("utf-8"))
    return f"sha256:{h.hexdigest()[:16]}"


def _source_matches(source: SkillSource, source_name: str) -> bool:
    aliases = {
        "skills.sh": "skills-sh",
    }
    normalized = aliases.get(source_name, source_name)
    return source.source_id() == normalized


def check_for_skill_updates(
    name: Optional[str] = None,
    *,
    lock: Optional[HubLockFile] = None,
    sources: Optional[List[SkillSource]] = None,
    auth: Optional[GitHubAuth] = None,
) -> List[dict]:
    """检查已安装的 hub skill 是否有上游变更。"""
    lock = lock or HubLockFile()
    installed = lock.list_installed()
    if name:
        installed = [entry for entry in installed if entry.get("name") == name]

    if sources is None:
        sources = create_source_router(auth=auth)

    results: List[dict] = []
    for entry in installed:
        identifier = entry.get("identifier", "")
        source_name = entry.get("source", "")
        candidate_sources = [src for src in sources if _source_matches(src, source_name)] or sources

        bundle = None
        for src in candidate_sources:
            try:
                bundle = src.fetch(identifier)
            except Exception:
                bundle = None
            if bundle:
                break

        if not bundle:
            results.append({
                "name": entry.get("name", ""),
                "identifier": identifier,
                "source": source_name,
                "status": "unavailable",
            })
            continue

        current_hash = entry.get("content_hash", "")
        latest_hash = bundle_content_hash(bundle)
        status = "up_to_date" if current_hash == latest_hash else "update_available"
        results.append({
            "name": entry.get("name", ""),
            "identifier": identifier,
            "source": source_name,
            "status": status,
            "current_hash": current_hash,
            "latest_hash": latest_hash,
            "bundle": bundle,
        })

    return results


# ---------------------------------------------------------------------------
# Hermes 集中式索引来源
# ---------------------------------------------------------------------------

HERMES_INDEX_URL = "https://hermes-agent.nousresearch.com/docs/api/skills-index.json"
HERMES_INDEX_CACHE_FILE = INDEX_CACHE_DIR / "hermes-index.json"
HERMES_INDEX_TTL = 6 * 3600  # 6 小时


def _load_hermes_index() -> Optional[dict]:
    """拉取集中式 skill 索引，并带本地缓存。

    该索引是托管在文档站点上的一个 JSON 文件，由 CI 每日重建。
    我们在本地缓存 HERMES_INDEX_TTL 秒，以避免在一次会话内重复下载。
    """
    # 检查本地缓存
    if HERMES_INDEX_CACHE_FILE.exists():
        try:
            age = time.time() - HERMES_INDEX_CACHE_FILE.stat().st_mtime
            if age < HERMES_INDEX_TTL:
                return json.loads(HERMES_INDEX_CACHE_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            pass

    # 从文档站点拉取
    try:
        resp = httpx.get(HERMES_INDEX_URL, timeout=15, follow_redirects=True)
        if resp.status_code != 200:
            logger.debug("Hermes index fetch returned %d", resp.status_code)
            return _load_stale_index_cache()
        data = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        logger.debug("Hermes index fetch failed: %s", e)
        return _load_stale_index_cache()

    # 校验结构
    if not isinstance(data, dict) or "skills" not in data:
        return _load_stale_index_cache()

    # 写入本地缓存
    try:
        HERMES_INDEX_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        HERMES_INDEX_CACHE_FILE.write_text(json.dumps(data))
    except OSError:
        pass

    return data


def _load_stale_index_cache() -> Optional[dict]:
    """当网络拉取失败时，回退使用过期的缓存。"""
    if HERMES_INDEX_CACHE_FILE.exists():
        try:
            return json.loads(HERMES_INDEX_CACHE_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    return None


class HermesIndexSource(SkillSource):
    """以集中式 Hermes Skills Index 为后端的 skill 来源。

    该索引是发布到文档站点、由 CI 每日重建的 JSON 目录。它包含每个 skill 的
    元数据 + 已解析的 GitHub 路径，使用户无需再为搜索或路径发现去请求
    GitHub API。

    当索引不可用时，所有方法都返回空 / None，以便下游来源透明地接管。
    """

    def __init__(self, auth: GitHubAuth):
        self._index: Optional[dict] = None
        self._loaded = False
        self.auth = auth
        # 懒加载创建 GitHubSource 用于 fetch —— 仅在实际下载文件时才用到，
        # 因为那需要真正的 GitHub API 调用。
        self._github: Optional[GitHubSource] = None

    def _ensure_loaded(self) -> dict:
        if not self._loaded:
            self._index = _load_hermes_index()
            self._loaded = True
        return self._index or {}

    def _get_github(self) -> GitHubSource:
        if self._github is None:
            self._github = GitHubSource(auth=self.auth)
        return self._github

    def source_id(self) -> str:
        return "hermes-index"

    @property
    def is_available(self) -> bool:
        """索引是否已加载且包含 skill。"""
        index = self._ensure_loaded()
        return bool(index.get("skills"))

    def trust_level_for(self, identifier: str) -> str:
        index = self._ensure_loaded()
        for skill in index.get("skills", []):
            if skill.get("identifier") == identifier:
                return skill.get("trust_level", "community")
        return "community"

    def search(self, query: str, limit: int = 10) -> List[SkillMeta]:
        """在缓存的索引中搜索。零次 API 调用。"""
        index = self._ensure_loaded()
        skills = index.get("skills", [])
        if not skills:
            return []

        if not query.strip():
            # 无查询 —— 返回推荐/热门条目
            return [self._to_meta(s) for s in skills[:limit]]

        query_lower = query.lower()
        results: List[SkillMeta] = []
        for s in skills:
            searchable = f"{s.get('name', '')} {s.get('description', '')} {' '.join(s.get('tags', []))}".lower()
            if query_lower in searchable:
                results.append(self._to_meta(s))
                if len(results) >= limit:
                    break
        return results

    def fetch(self, identifier: str) -> Optional[SkillBundle]:
        """使用索引中解析出的路径来抓取 skill。

        如果索引中为该 skill 记录了 ``resolved_github_id``，我们就跳过整条
        候选/发现链，直接用精确路径请求 GitHub。这把安装的 API 调用从约 31 次
        降到了仅文件内容下载（视 skill 大小约 5-22 次）。
        """
        index = self._ensure_loaded()
        entry = self._find_entry(identifier, index)
        if not entry:
            return None

        # 若存在已解析的路径则使用它
        resolved = entry.get("resolved_github_id")
        if resolved:
            bundle = self._get_github().fetch(resolved)
            if bundle:
                bundle.source = entry.get("source", "hermes-index")
                bundle.identifier = identifier
                return bundle

        # 回退到基于 identifier 的 repo/path 抓取
        repo = entry.get("repo", "")
        path = entry.get("path", "")
        if repo and path:
            github_id = f"{repo}/{path}"
            bundle = self._get_github().fetch(github_id)
            if bundle:
                bundle.source = entry.get("source", "hermes-index")
                bundle.identifier = identifier
                return bundle

        return None

    def inspect(self, identifier: str) -> Optional[SkillMeta]:
        """返回索引中的元数据。零次 API 调用。"""
        index = self._ensure_loaded()
        entry = self._find_entry(identifier, index)
        if entry:
            return self._to_meta(entry)
        return None

    def _find_entry(self, identifier: str, index: dict) -> Optional[dict]:
        """按 identifier 或名字在索引中查找 skill。"""
        skills = index.get("skills", [])

        # 精确匹配 identifier
        for s in skills:
            if s.get("identifier") == identifier:
                return s

        # 尝试去掉来源前缀（例如去掉 "skills-sh/"）
        normalized = identifier
        for prefix in ("skills-sh/", "skills.sh/", "official/", "github/", "clawhub/"):
            if identifier.startswith(prefix):
                normalized = identifier[len(prefix):]
                break

        # 基于规范化后的 identifier 或名字做匹配
        for s in skills:
            sid = s.get("identifier", "")
            # 同时去掉存储 identifier 上的前缀
            stored_normalized = sid
            for prefix in ("skills-sh/", "skills.sh/", "official/", "github/", "clawhub/"):
                if sid.startswith(prefix):
                    stored_normalized = sid[len(prefix):]
                    break
            if stored_normalized == normalized:
                return s

        return None

    @staticmethod
    def _to_meta(entry: dict) -> SkillMeta:
        return SkillMeta(
            name=entry.get("name", ""),
            description=entry.get("description", ""),
            source=entry.get("source", "hermes-index"),
            identifier=entry.get("identifier", ""),
            trust_level=entry.get("trust_level", "community"),
            repo=entry.get("repo"),
            path=entry.get("path"),
            tags=entry.get("tags", []),
            extra=entry.get("extra", {}),
        )


def create_source_router(auth: Optional[GitHubAuth] = None) -> List[SkillSource]:
    """
    创建所有已配置的来源适配器。
    返回一个用于 search/fetch 操作的活跃来源列表。
    """
    if auth is None:
        auth = GitHubAuth()

    taps_mgr = TapsManager()
    extra_taps = taps_mgr.list_taps()

    sources: List[SkillSource] = [
        OptionalSkillSource(),        # 官方可选 skill（优先级最高）
        HermesIndexSource(auth=auth), # 集中式索引（搜索 + 已解析的安装路径）
        SkillsShSource(auth=auth),
        WellKnownSkillSource(),
        UrlSource(),                  # 指向 SKILL.md 文件的直接 HTTP(S) URL
        GitHubSource(auth=auth, extra_taps=extra_taps),
        ClawHubSource(),
        ClaudeMarketplaceSource(auth=auth),
        LobeHubSource(),
        BrowseShSource(),   # browse.sh：169+ 个站点专用的浏览器自动化 skill
    ]

    return sources


def _search_one_source(
    src: SkillSource, query: str, limit: int
) -> Tuple[str, List[SkillMeta]]:
    """搜索单个来源。在独立线程中运行以实现并行。"""
    try:
        return src.source_id(), src.search(query, limit=limit)
    except Exception as e:
        logger.debug("Search failed for %s: %s", src.source_id(), e)
        return src.source_id(), []


def parallel_search_sources(
    sources: List[SkillSource],
    query: str = "",
    per_source_limits: Optional[Dict[str, int]] = None,
    source_filter: str = "all",
    overall_timeout: float = 30,
    on_source_done: Optional[Any] = None,
) -> Tuple[List[SkillMeta], Dict[str, int], List[str]]:
    """并行搜索所有来源，每个来源有自己的超时。

    返回 ``(all_results, source_counts, timed_out_ids)``。

    *on_source_done* 是一个可选回调 ``(source_id, count) -> None``，在每个
    来源完成时被调用 —— 可用于进度指示。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    per_source_limits = per_source_limits or {}

    active: List[SkillSource] = []
    # 当集中式索引可用、且用户没有过滤到特定来源时，跳过外部 API 来源
    # （github、skills-sh、clawhub 等）—— 索引里已经有它们的数据。
    # 这样可以为未认证用户每次搜索省下约 70 次 GitHub API 调用。
    _index_available = False
    _api_source_ids = frozenset({"github", "skills-sh", "clawhub",
                                  "claude-marketplace", "lobehub", "well-known"})
    if source_filter == "all":
        for src in sources:
            if (src.source_id() == "hermes-index"
                    and getattr(src, "is_available", False)):
                _index_available = True
                break

    for src in sources:
        sid = src.source_id()
        if source_filter != "all" and sid != source_filter and sid != "official":
            continue
        # 当索引已覆盖外部 API 来源时跳过它们
        if _index_available and sid in _api_source_ids:
            continue
        active.append(src)

    all_results: List[SkillMeta] = []
    source_counts: Dict[str, int] = {}
    timed_out_ids: List[str] = []

    if not active:
        return all_results, source_counts, timed_out_ids

    # 注意：使用 ``with ThreadPoolExecutor(...) as pool`` 代码块会在退出时
    # 调用 ``shutdown(wait=True)``，这会阻塞直到所有已提交的 worker 完成 ——
    # 所以单个慢来源（例如 ClawHub）会让调用方被阻塞数分钟，使
    # ``overall_timeout`` 形同虚设。我们手动管理执行器，并用 ``wait=False``
    # 关闭它，确保超时真正生效。
    pool = ThreadPoolExecutor(max_workers=min(len(active), 8))
    futures = {}
    for src in active:
        lim = per_source_limits.get(src.source_id(), 50)
        fut = pool.submit(_search_one_source, src, query, lim)
        futures[fut] = src.source_id()

    try:
        try:
            for fut in as_completed(futures, timeout=overall_timeout):
                try:
                    sid, results = fut.result(timeout=0)
                    source_counts[sid] = len(results)
                    all_results.extend(results)
                    if on_source_done:
                        on_source_done(sid, len(results))
                except Exception:
                    pass
        except TimeoutError:
            timed_out_ids = [
                futures[f] for f in futures if not f.done()
            ]
            if timed_out_ids:
                logger.debug(
                    "Skills browse timed out waiting for: %s",
                    ", ".join(timed_out_ids),
                )
    finally:
        # wait=False，使慢来源不会阻塞调用方返回；
        # cancel_futures 丢弃尚未开始的工作。
        pool.shutdown(wait=False, cancel_futures=True)

    return all_results, source_counts, timed_out_ids


def unified_search(query: str, sources: List[SkillSource],
                   source_filter: str = "all", limit: int = 10) -> List[SkillMeta]:
    """（并行）搜索所有来源并合并结果。"""
    all_results, _, _ = parallel_search_sources(
        sources,
        query=query,
        source_filter=source_filter,
        overall_timeout=30,
    )

    # 按 identifier 去重，优先保留信任级别更高的条目。
    # identifier 对每个 skill 都是唯一的（例如 "browse-sh/airbnb.com/search-listings-ddgioa"）。
    # 若改用 name，会错误地合并来自不同站点、但任务名相同的 browse-sh skill
    # （例如 Airbnb 和 Booking.com 的 "search-listings"）。
    _TRUST_RANK = {"builtin": 2, "trusted": 1, "community": 0}
    seen: Dict[str, SkillMeta] = {}
    for r in all_results:
        if r.identifier not in seen:
            seen[r.identifier] = r
        elif _TRUST_RANK.get(r.trust_level, 0) > _TRUST_RANK.get(seen[r.identifier].trust_level, 0):
            seen[r.identifier] = r
    deduped = list(seen.values())

    return deduped[:limit]
