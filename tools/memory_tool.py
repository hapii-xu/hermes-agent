#!/usr/bin/env python3
"""
记忆工具模块 - 持久化的精选记忆

提供有界、基于文件、跨会话持久化的记忆。两个存储区：
  - MEMORY.md：agent 的个人笔记与观察（环境事实、项目约定、
    工具特性、学到的东西）
  - USER.md：agent 对用户的了解（偏好、沟通风格、期望、
    工作习惯）

两者在会话开始时作为冻结快照注入系统提示词。
会话中途的写入会立即更新磁盘上的文件（持久化），但不会改变
系统提示词——这样可为整个会话保留前缀缓存。
快照在下一次会话开始时刷新。

条目分隔符：§（章节符）。条目可以多行。
使用字符数限制（而非 token 数），因为字符计数与模型无关。

设计：
- 单一 `memory` 工具，带 action 参数：add、replace、remove
- replace/remove 使用简短的唯一子串匹配（而非全文或 ID）
- 行为指引放在工具 schema 描述里
- 冻结快照模式：系统提示词稳定，工具响应展示实时状态
"""

import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from hermes_constants import get_hermes_home
from typing import Dict, Any, List, Optional

from utils import atomic_replace

# fcntl 仅 Unix 可用；Windows 上使用 msvcrt 进行文件锁定
msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        pass

logger = logging.getLogger(__name__)

# 记忆文件所在位置——动态解析，以便始终尊重 profile 覆盖
# （HERMES_HOME 环境变量变更）。旧的模块级常量在导入时被缓存，
# 若首次导入后发生 profile 切换则可能失效。
def get_memory_dir() -> Path:
    """返回 profile 作用域内的记忆目录。"""
    return get_hermes_home() / "memories"

ENTRY_DELIMITER = "\n§\n"


# ---------------------------------------------------------------------------
# 记忆内容扫描——对注入到系统提示词中的内容进行轻量级注入/外泄检查。
#
# 模式位于 ``tools/threat_patterns.py``——这是单一事实来源，
# 与上下文文件扫描器和工具结果分隔符系统共享。
# 记忆使用 "strict"（严格）作用域（最广的模式集合），因为：
#  - 记忆条目由用户精选；用户可以重写被标记的条目
#  - 记忆以 FROZEN（冻结）快照形式进入系统提示词，因此被污染的
#    条目会在整个会话以及跨会话中持续存在，直到被显式移除。
# ---------------------------------------------------------------------------

from tools.threat_patterns import first_threat_message as _first_threat_message


def _scan_memory_content(content: str) -> Optional[str]:
    """扫描记忆内容中的注入/外泄模式。若被阻止则返回错误字符串。"""
    return _first_threat_message(content, scope="strict")


def _drift_error(path: "Path", bak_path: str) -> Dict[str, Any]:
    """构建检测到外部漂移时返回的错误字典。

    磁盘上的记忆文件包含无法通过工具的解析器/序列化器往返的内容——
    刷新会丢弃来自 patch 工具、shell 追加、手动编辑或姐妹会话写入的
    被追加/编辑的内容。我们拒绝该变更，把操作者指向我们创建的
    .bak.<ts> 快照，并告诉他们接下来该怎么做。
    """
    return {
        "success": False,
        "error": (
            f"Refusing to write {path.name}: file on disk has content that "
            f"wouldn't round-trip through the memory tool (likely added by "
            f"the patch tool, a shell append, a manual edit, or a "
            f"concurrent session). A snapshot was saved to {bak_path}. "
            f"Resolve the drift first — either rewrite the file as a clean "
            f"§-delimited list of entries, or move the extra content out — "
            f"then retry. This guard exists to prevent silent data loss "
            f"(issue #26045)."
        ),
        "drift_backup": bak_path,
        "remediation": (
            "Open the .bak file, integrate the missing entries into the "
            "memory tool one at a time via memory(action=add, content=...), "
            "then remove or rewrite the original file to a clean state."
        ),
    }


class MemoryStore:
    """
    有界精选记忆，带文件持久化。每个 AIAgent 一个实例。

    维护两个并行状态：
      - _system_prompt_snapshot：在加载时冻结，用于系统提示词注入。
        会话中途从不修改。保持前缀缓存稳定。
      - memory_entries / user_entries：实时状态，由工具调用修改，持久化到磁盘。
        工具响应始终反映此实时状态。
    """

    def __init__(self, memory_char_limit: int = 2200, user_char_limit: int = 1375):
        self.memory_entries: List[str] = []
        self.user_entries: List[str] = []
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        # 系统提示词的冻结快照——在 load_from_disk() 时设置一次
        self._system_prompt_snapshot: Dict[str, str] = {"memory": "", "user": ""}

    def load_from_disk(self):
        """从 MEMORY.md 和 USER.md 加载条目，捕获系统提示词快照。

        冻结的快照就是进入系统提示词的内容。我们在构建快照时扫描每个
        条目的注入/promptware 模式——
        任何命中都会把快照中的条目文本替换为占位符，例如
        ``[BLOCKED: …]``，因此磁盘上被污染的记忆文件（供应链、
        被破坏的工具、姐妹会话写入）无法注入到
        系统提示词中。

        实时的 ``memory_entries`` / ``user_entries`` 列表保留
        原始文本，这样用户仍然可以直接查看源文件以 SEE（看到）被污染的条目，
        并移除它们——静默丢弃会把攻击对用户隐藏。

        扫描基于磁盘字节确定性进行，因此快照在整个
        会话中保持稳定（前缀缓存不变式成立）。
        """
        mem_dir = get_memory_dir()
        mem_dir.mkdir(parents=True, exist_ok=True)

        self.memory_entries = self._read_file(mem_dir / "MEMORY.md")
        self.user_entries = self._read_file(mem_dir / "USER.md")

        # 条目去重（保留顺序，保留首次出现）
        self.memory_entries = list(dict.fromkeys(self.memory_entries))
        self.user_entries = list(dict.fromkeys(self.user_entries))

        # 仅对系统提示词快照的条目进行净化。实时状态
        # （memory_entries / user_entries）保留原始文本，以便用户
        # 可以通过 memory 工具查看并移除被污染的条目。
        sanitized_memory = self._sanitize_entries_for_snapshot(self.memory_entries, "MEMORY.md")
        sanitized_user = self._sanitize_entries_for_snapshot(self.user_entries, "USER.md")

        # 捕获用于系统提示词注入的冻结快照
        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", sanitized_memory),
            "user": self._render_block("user", sanitized_user),
        }

    @staticmethod
    def _sanitize_entries_for_snapshot(entries: List[str], filename: str) -> List[str]:
        """返回 ``entries``，其中任何匹配威胁的条目被替换为占位符。

        每个条目都用共享的威胁模式库在 ``"strict"`` 作用域下扫描
        （与记忆写入相同）。匹配时，返回列表中的该条目被替换为
        ``"[BLOCKED: <filename> entry
        contained threat pattern: <ids>. Removed from system prompt.]"``——
        占位符进入快照，原始条目保留在实时状态中供用户检查和删除。

        空条目或已是 block 标记的条目原样通过。
        """
        from tools.threat_patterns import scan_for_threats

        sanitized: List[str] = []
        for entry in entries:
            if not entry or entry.startswith("[BLOCKED:"):
                sanitized.append(entry)
                continue
            findings = scan_for_threats(entry, scope="strict")
            if findings:
                logger.warning(
                    "Memory entry from %s blocked at load time: %s",
                    filename, ", ".join(findings),
                )
                sanitized.append(
                    f"[BLOCKED: {filename} entry contained threat pattern(s): "
                    f"{', '.join(findings)}. Removed from system prompt; "
                    f"use memory(action=remove) "
                    f"to delete the original.]"
                )
            else:
                sanitized.append(entry)
        return sanitized

    @staticmethod
    @contextmanager
    def _file_lock(path: Path):
        """获取独占文件锁以保证读-改-写的安全性。

        使用单独的 .lock 文件，这样记忆文件本身仍可通过
        os.replace() 原子性地替换。
        """
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        if fcntl is None and msvcrt is None:
            yield
            return

        fd = open(lock_path, "a+", encoding="utf-8")
        try:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            if fcntl:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except (OSError, IOError):
                    pass
            elif msvcrt:
                try:
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, IOError):
                    pass
            fd.close()

    @staticmethod
    def _path_for(target: str) -> Path:
        mem_dir = get_memory_dir()
        if target == "user":
            return mem_dir / "USER.md"
        return mem_dir / "MEMORY.md"

    def _reload_target(self, target: str, *, skip_drift: bool = False) -> Optional[str]:
        """从磁盘重新读取条目到内存状态。

        在文件锁下调用，以便在修改前获取最新状态。
        如果检测到外部漂移则返回备份路径（磁盘上的文件包含无法
        通过我们的解析器/序列化器往返的内容，或者某个条目大于
        存储区的字符限制）。检测到漂移时，调用者必须中止修改——
        刷新会丢弃无法往返的内容。
        干净重载时返回 None。

        当 *skip_drift* 为 True 时，跳过往返/条目大小检查。
        由 ``add`` 动作使用，它追加而不重写，因此现有内容
        永不会被覆盖。
        """
        path = self._path_for(target)
        bak = None if skip_drift else self._detect_external_drift(target)
        fresh = self._read_file(path)
        fresh = list(dict.fromkeys(fresh))  # 去重
        self._set_entries(target, fresh)
        return bak

    def save_to_disk(self, target: str):
        """将条目持久化到相应文件。在每次修改后调用。"""
        get_memory_dir().mkdir(parents=True, exist_ok=True)
        self._write_file(self._path_for(target), self._entries_for(target))

    def _entries_for(self, target: str) -> List[str]:
        if target == "user":
            return self.user_entries
        return self.memory_entries

    def _set_entries(self, target: str, entries: List[str]):
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        if not entries:
            return 0
        return len(ENTRY_DELIMITER.join(entries))

    def _char_limit(self, target: str) -> int:
        if target == "user":
            return self.user_char_limit
        return self.memory_char_limit

    def add(self, target: str, content: str) -> Dict[str, Any]:
        """追加一条新条目。若会超出字符限制则返回错误。"""
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        # 在接受前扫描注入/外泄
        scan_error = _scan_memory_content(content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            # 在锁下从磁盘重新读取，以获取其他会话的写入。
            # 对于 add（仅追加），我们跳过漂移防护——追加从不
            # 覆盖现有内容，因此同一会话中先前工具写入条目的
            # 往返不匹配是无害的。漂移防护对 replace/remove 仍然
            # 有效，因为整文件重写会丢弃无法往返的内容（issue #26045）。
            self._reload_target(target, skip_drift=True)

            entries = self._entries_for(target)
            limit = self._char_limit(target)

            # 拒绝完全重复
            if content in entries:
                return self._success_response(target, "Entry already exists (no duplicate added).")

            # 计算新的总数会是多少
            new_entries = entries + [content]
            new_total = len(ENTRY_DELIMITER.join(new_entries))

            if new_total > limit:
                current = self._char_count(target)
                return {
                    "success": False,
                    "error": (
                        f"Memory at {current:,}/{limit:,} chars. "
                        f"Adding this entry ({len(content)} chars) would exceed the limit. "
                        f"Consolidate now: use 'replace' to merge overlapping entries into "
                        f"shorter ones or 'remove' stale or less important entries (see "
                        f"current_entries below), then retry this add — all in this turn."
                    ),
                    "current_entries": entries,
                    "usage": f"{current:,}/{limit:,}",
                }

            entries.append(content)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry added.")

    def replace(self, target: str, old_text: str, new_content: str) -> Dict[str, Any]:
        """查找包含 old_text 子串的条目，将其替换为 new_content。"""
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {"success": False, "error": "new_content cannot be empty. Use 'remove' to delete entries."}

        # 扫描替换内容中的注入/外泄
        scan_error = _scan_memory_content(new_content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak:
                return _drift_error(self._path_for(target), bak)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return {"success": False, "error": f"No entry matched '{old_text}'."}

            if len(matches) > 1:
                # 如果所有匹配完全相同（精确重复），对第一个操作
                unique_texts = {e for _, e in matches}
                if len(unique_texts) > 1:
                    previews = [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches]
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }
                # 全部相同——只替换第一个是安全的

            idx = matches[0][0]
            limit = self._char_limit(target)

            # 检查替换不会超出预算
            test_entries = entries.copy()
            test_entries[idx] = new_content
            new_total = len(ENTRY_DELIMITER.join(test_entries))

            if new_total > limit:
                current = self._char_count(target)
                return {
                    "success": False,
                    "error": (
                        f"Replacement would put memory at {new_total:,}/{limit:,} chars. "
                        f"Shorten the new content, or 'remove' other stale or less important "
                        f"entries to make room (see current_entries below), then retry — all "
                        f"in this turn."
                    ),
                    "current_entries": entries,
                    "usage": f"{current:,}/{limit:,}",
                }

            entries[idx] = new_content
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> Dict[str, Any]:
        """移除包含 old_text 子串的条目。"""
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak:
                return _drift_error(self._path_for(target), bak)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return {"success": False, "error": f"No entry matched '{old_text}'."}

            if len(matches) > 1:
                # 如果所有匹配完全相同（精确重复），移除第一个
                unique_texts = {e for _, e in matches}
                if len(unique_texts) > 1:
                    previews = [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches]
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }
                # 全部相同——只移除第一个是安全的

            idx = matches[0][0]
            entries.pop(idx)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry removed.")

    def apply_batch(self, target: str, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """对一个目标原子地应用一连串 add/replace/remove 操作。

        所有操作都针对 FINAL（最终）预算进行验证和应用——
        中间溢出无关紧要。这让模型可以在单次工具调用中腾出空间
        （remove/replace）并添加新条目，而不必进行
        多轮的"先合并再重试"流程（该流程会多次重发整个
        对话上下文）。

        语义：全有或全无。如果任何操作格式错误、不匹配，或者
        净结果会超出字符限制，则什么都不写入，并返回
        描述首个失败的错误以及实时状态。
        """
        if not operations:
            return {"success": False, "error": "operations list is empty."}

        # 在触碰磁盘之前，扫描每个 add/replace 内容的注入/外泄——
        # 单个被污染的操作会拒绝整个批次。
        for i, op in enumerate(operations):
            act = (op or {}).get("action")
            new_content = (op or {}).get("content")
            if act in {"add", "replace"} and new_content:
                scan_error = _scan_memory_content(new_content)
                if scan_error:
                    return {"success": False, "error": f"Operation {i + 1}: {scan_error}"}

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak:
                return _drift_error(self._path_for(target), bak)

            # 在副本上工作；仅当整个批次验证通过才提交。
            working: List[str] = list(self._entries_for(target))
            limit = self._char_limit(target)

            for i, op in enumerate(operations):
                op = op or {}
                act = op.get("action")
                content = (op.get("content") or "").strip()
                old_text = (op.get("old_text") or "").strip()
                pos = f"Operation {i + 1} ({act or 'unknown'})"

                if act == "add":
                    if not content:
                        return self._batch_error(target, f"{pos}: content is required.")
                    if content in working:
                        continue  # 幂等——跳过重复项，不让批次失败
                    working.append(content)

                elif act == "replace":
                    if not old_text:
                        return self._batch_error(target, f"{pos}: old_text is required.")
                    if not content:
                        return self._batch_error(
                            target,
                            f"{pos}: content is required (use action='remove' to delete).",
                        )
                    matches = [j for j, e in enumerate(working) if old_text in e]
                    if not matches:
                        return self._batch_error(target, f"{pos}: no entry matched '{old_text}'.")
                    if len({working[j] for j in matches}) > 1:
                        return self._batch_error(
                            target,
                            f"{pos}: '{old_text}' matched multiple distinct entries -- be more specific.",
                        )
                    working[matches[0]] = content

                elif act == "remove":
                    if not old_text:
                        return self._batch_error(target, f"{pos}: old_text is required.")
                    matches = [j for j, e in enumerate(working) if old_text in e]
                    if not matches:
                        return self._batch_error(target, f"{pos}: no entry matched '{old_text}'.")
                    if len({working[j] for j in matches}) > 1:
                        return self._batch_error(
                            target,
                            f"{pos}: '{old_text}' matched multiple distinct entries -- be more specific.",
                        )
                    working.pop(matches[0])

                else:
                    return self._batch_error(
                        target,
                        f"{pos}: unknown action. Use add, replace, or remove.",
                    )

            # 仅针对 FINAL（最终）状态进行预算检查。
            new_total = len(ENTRY_DELIMITER.join(working)) if working else 0
            if new_total > limit:
                current = self._char_count(target)
                return {
                    "success": False,
                    "error": (
                        f"After applying all {len(operations)} operations, memory would be at "
                        f"{new_total:,}/{limit:,} chars -- over the limit. Remove or shorten more "
                        f"entries in the same batch (see current_entries below), then retry."
                    ),
                    "current_entries": self._entries_for(target),
                    "usage": f"{current:,}/{limit:,}",
                }

            # 提交。
            self._set_entries(target, working)
            self.save_to_disk(target)

        return self._success_response(target, f"Applied {len(operations)} operation(s).")

    def _batch_error(self, target: str, message: str) -> Dict[str, Any]:
        """构建报告实时（未提交）状态的批次中止错误。"""
        current = self._char_count(target)
        limit = self._char_limit(target)
        return {
            "success": False,
            "error": message + " No operations were applied (batch is all-or-nothing).",
            "current_entries": self._entries_for(target),
            "usage": f"{current:,}/{limit:,}",
        }

    def format_for_system_prompt(self, target: str) -> Optional[str]:
        """
        返回用于系统提示词注入的冻结快照。

        这返回在 load_from_disk() 时捕获的状态，而不是实时
        状态。会话中途的写入不影响它。这使系统
        提示词在所有轮次中保持稳定，保留前缀缓存。

        如果快照为空（加载时没有条目），返回 None。
        """
        block = self._system_prompt_snapshot.get(target, "")
        return block if block else None

    # -- 内部辅助函数 --

    def _success_response(self, target: str, message: str = None) -> Dict[str, Any]:
        entries = self._entries_for(target)
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        # 成功响应有意设计为 TERMINAL（终态）：它确认写入
        # 已落地并告诉模型停止。我们在这里不回显完整条目
        # 列表——转储它会引诱模型"找更多要修复的"并
        # 重新发出相同操作（观察到的抖动：第 1 次调用正确的批次，
        # 然后 5 次冗余重复）。条目仅在
        # 错误/超预算路径上展示，在这些路径上模型确实需要它们来
        # 决定要合并什么。
        resp = {
            "success": True,
            "done": True,
            "target": target,
            "usage": f"{pct}% — {current:,}/{limit:,} chars",
            "entry_count": len(entries),
        }
        if message:
            resp["message"] = message
        resp["note"] = "Write saved. This update is complete — do not repeat it."
        return resp

    def _render_block(self, target: str, entries: List[str]) -> str:
        """渲染带有头部和使用量指示的系统提示词块。"""
        if not entries:
            return ""

        limit = self._char_limit(target)
        content = ENTRY_DELIMITER.join(entries)
        current = len(content)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        if target == "user":
            header = f"USER PROFILE (who the user is) [{pct}% — {current:,}/{limit:,} chars]"
        else:
            header = f"MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]"

        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"

    @staticmethod
    def _read_file(path: Path) -> List[str]:
        """读取记忆文件并拆分为条目。

        不需要文件锁：_write_file 使用原子重命名，因此读取者
        始终看到前一个完整文件或新的完整文件。
        """
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, IOError):
            return []

        if not raw.strip():
            return []

        # 使用 ENTRY_DELIMITER 以与 _write_file 保持一致。仅按 "§" 拆分
        # 会错误地拆分内容中包含 "§" 的条目。
        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
        return [e for e in entries if e]

    def _detect_external_drift(self, target: str) -> Optional[str]:
        """如果磁盘上的内容显示外部漂移，返回备份路径字符串。

        记忆文件应该是工具写入的小条目列表，由 § 连接。通过两个信号检测漂移：

        1. 往返不匹配——重新解析并重新序列化文件
           不产生相同的字节（罕见；会捕获到编码异常的
           分隔符）。
        2. 条目大小溢出——任何单个解析出的条目超过
           存储区的整文件字符限制。工具将 ENTIRE（整个）存储区
           预算到该限制；没有任何单个工具写入的条目能超过它。
           当我们看到一个条目大于限制时，说明外部写入者
           （patch 工具、shell 追加、手动编辑、姐妹会话）向工具
           将视为单个条目的位置追加了自由格式内容。
           刷新随后会把该条目截断为模型的新
           内容，丢弃被追加的字节——issue #26045。

        当发现漂移并已备份时返回 .bak 文件的绝对路径；
        当文件看起来是工具形状时返回 None。

        说明：这是一个 INSTANCE（实例）方法（非静态），因为信号 #2
        需要每个 target 的 char_limit。
        """
        path = self._path_for(target)
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, IOError):
            return None
        if not raw.strip():
            return None

        parsed = [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]
        roundtrip = ENTRY_DELIMITER.join(parsed)

        char_limit = self._char_limit(target)
        max_entry_len = max((len(e) for e in parsed), default=0)

        drift_detected = (raw.strip() != roundtrip) or (max_entry_len > char_limit)
        if not drift_detected:
            return None

        # 确认漂移——对文件做快照，以便操作者可以恢复
        # 外部写入者添加的任何内容，然后返回 .bak 路径，
        # 让调用者可以拒绝该修改。
        ts = int(time.time())
        bak_path = path.with_suffix(path.suffix + f".bak.{ts}")
        try:
            bak_path.write_text(raw, encoding="utf-8")
        except (OSError, IOError):
            return str(bak_path) + " (BACKUP FAILED — file unchanged on disk)"
        return str(bak_path)

    @staticmethod
    def _write_file(path: Path, entries: List[str]):
        """使用原子临时文件 + 重命名将条目写入记忆文件。

        之前的实现使用 open("w") + flock，但 "w" 在获取锁
        *之前*就截断文件，制造了一个竞争窗口，并发
        读取者会看到空文件。原子重命名避免了这一点：
        读取者始终看到旧的完整文件或新的完整文件。
        """
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        try:
            # 写入同一目录下的临时文件（同一文件系统以保证原子重命名）
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp", prefix=".mem_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                atomic_replace(tmp_path, path)
            except BaseException:
                # 任何失败时清理临时文件
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except (OSError, IOError) as e:
            raise RuntimeError(f"Failed to write memory file {path}: {e}")


def load_on_disk_store() -> "MemoryStore":
    """构建一个全新的基于磁盘的 :class:`MemoryStore`，遵循配置的字符限制。

    从任何没有活跃 agent 的上下文（消息网关、
    Desktop GUI、裸 CLI ``/memory`` 处理器）中使用此函数，但仍需要读取或
    应用已批准的记忆写入。它镜像了活跃 agent 在
    ``agent/agent_init.py`` 中构建其存储的方式——包括用户的 ``memory.memory_char_limit``
    / ``memory.user_char_limit`` 覆盖——因此在没有活跃 agent 的情况下应用的审批
    强制执行与有 agent 时 SAME（相同）的上限。

    如果无法加载配置，则回退到内置默认值，因此这
    绝不会因配置缺失/不可读而抛出异常。
    """
    memory_char_limit = 2200
    user_char_limit = 1375
    try:
        from hermes_cli.config import load_config

        mem_cfg = (load_config() or {}).get("memory", {}) or {}
        memory_char_limit = int(mem_cfg.get("memory_char_limit", memory_char_limit))
        user_char_limit = int(mem_cfg.get("user_char_limit", user_char_limit))
    except Exception:
        pass  # 配置可选——回退到默认值，而不是破坏 /memory

    store = MemoryStore(
        memory_char_limit=memory_char_limit,
        user_char_limit=user_char_limit,
    )
    store.load_from_disk()
    return store


def _apply_write_gate(action: str, target: str, content: Optional[str],
                      old_text: Optional[str]) -> Optional[str]:
    """评估记忆写入门控。当写入不应正常进行（被阻止或暂存）时返回 JSON 工具结果字符串，
    当调用者应执行真正写入时返回 None。

    只有修改性动作（add/replace/remove）被门控。
    """
    if action not in {"add", "replace", "remove"}:
        return None

    try:
        from tools import write_approval as wa
    except Exception:
        # 如果门控模块无法加载，则 fail open（放行，当前行为），而不是
        # 阻止所有记忆写入。
        return None

    # 为前台审批提示构建一个简短的 inline 摘要/详情。
    label = "user profile" if target == "user" else "memory"
    if action == "add":
        summary = f"add to {label}"
        detail = content or ""
    elif action == "replace":
        summary = f"replace in {label}"
        detail = f"old: {old_text}\nnew: {content}"
    else:  # remove
        summary = f"remove from {label}"
        detail = old_text or ""

    decision = wa.evaluate_gate(wa.MEMORY, inline_summary=summary, inline_detail=detail)

    if decision.allow:
        return None

    if decision.blocked:
        return tool_error(decision.message, success=False)

    # 暂存
    payload = {
        "action": action,
        "target": target,
        "content": content,
        "old_text": old_text,
    }
    record = wa.stage_write(
        wa.MEMORY, payload,
        summary=f"{summary}: {detail[:120]}",
        origin=wa.current_origin(),
    )
    return json.dumps(
        {"success": True, "staged": True, "pending_id": record["id"],
         "message": decision.message},
        ensure_ascii=False,
    )


def _apply_batch_write_gate(target: str, operations: List[Dict[str, Any]]) -> Optional[str]:
    """评估一批记忆操作的写入门控。

    当批次不应进行（被阻止或暂存）时返回 JSON 工具结果字符串，
    当调用者应执行真正的批次写入时返回 None。整个批次作为单个单元被门控。
    """
    try:
        from tools import write_approval as wa
    except Exception:
        return None

    label = "user profile" if target == "user" else "memory"
    summary = f"apply {len(operations)} op(s) to {label}"
    detail_lines = []
    for op in operations:
        op = op or {}
        act = op.get("action", "?")
        if act == "remove":
            detail_lines.append(f"- remove: {op.get('old_text', '')}")
        elif act == "replace":
            detail_lines.append(f"- replace: {op.get('old_text', '')} -> {op.get('content', '')}")
        else:
            detail_lines.append(f"- {act}: {op.get('content', '')}")
    detail = "\n".join(detail_lines)

    decision = wa.evaluate_gate(wa.MEMORY, inline_summary=summary, inline_detail=detail)

    if decision.allow:
        return None

    if decision.blocked:
        return tool_error(decision.message, success=False)

    payload = {"action": "batch", "target": target, "operations": operations}
    record = wa.stage_write(
        wa.MEMORY, payload,
        summary=f"{summary}: {detail[:120]}",
        origin=wa.current_origin(),
    )
    return json.dumps(
        {"success": True, "staged": True, "pending_id": record["id"],
         "message": decision.message},
        ensure_ascii=False,
    )


def _missing_old_text_error(store: "MemoryStore", target: str, action: str) -> str:
    """为没有带 ``old_text`` 的 replace/remove 调用构建一个可恢复的错误。

    ``replace``/``remove`` 本质上是定向的——没有 ``old_text`` 就没有
    要操作的条目，因此我们无法完成该调用。但返回一个光秃秃的
    "old_text is required" 是死胡同：一些结构化输出客户端会省略
    可选的 ``old_text`` 字段（它不是，也不能成为 schema 必需的，除非
    有 Codex 后端拒绝的顶层组合子——见
    tests/tools/test_memory_tool_schema.py）。因此我们改为返回当前
    条目清单加上一个显式的重试指令，让模型重新发出
    调用，并将 ``old_text`` 设置为它所指条目的唯一子串。
    镜像批次路径的 ``_batch_error`` 形状。（issues #43412, #49466）
    """
    entries = store._entries_for(target)
    current = store._char_count(target)
    limit = store._char_limit(target)
    return json.dumps(
        {
            "success": False,
            "error": (
                f"'{action}' needs old_text -- a short unique substring of the entry "
                f"to {action}. None was provided. Reissue the {action} with old_text "
                f"set to part of one of the current_entries below."
            ),
            "current_entries": entries,
            "usage": f"{current:,}/{limit:,}",
        },
        ensure_ascii=False,
    )


def memory_tool(
    action: str = None,
    target: str = "memory",
    content: str = None,
    old_text: str = None,
    operations: Optional[List[Dict[str, Any]]] = None,
    store: Optional[MemoryStore] = None,
) -> str:
    """
    memory 工具的单一入口点。分派到 MemoryStore 方法。

    两种形状：
      - 单操作：action + (content / old_text)。
      - 批次：operations=[{action, content?, old_text?}, ...] 在单次调用中针对最终字符预算原子地应用。

    返回包含结果的 JSON 字符串。
    """
    if store is None:
        return tool_error("Memory is not available. It may be disabled in config or this environment.", success=False)

    if target not in {"memory", "user"}:
        return tool_error(f"Invalid target '{target}'. Use 'memory' or 'user'.", success=False)

    # --- 批次路径 -------------------------------------------------------
    if operations:
        if not isinstance(operations, list):
            return tool_error("operations must be a list of {action, content?, old_text?} objects.", success=False)
        gate_result = _apply_batch_write_gate(target, operations)
        if gate_result is not None:
            return gate_result
        result = store.apply_batch(target, operations)
        return json.dumps(result, ensure_ascii=False)

    # --- 单操作路径 ---------------------------------------------------
    # 在门控之前验证必需参数，以便无效写入被立即拒绝，
    # 而不是被暂存后只在审批时才失败。
    if action == "add" and not content:
        return tool_error("Content is required for 'add' action.", success=False)
    if action == "replace" and (not old_text or not content):
        missing = "old_text" if not old_text else "content"
        if not old_text:
            # 客户端/模型省略了 old_text。Replace 本质上是定向的
            # ——我们无法猜测是哪个条目。返回当前清单加上
            # 重试指令，让模型可以用 old_text 重新发出调用，
            # 而不是撞上死胡同错误。（issues #43412, #49466）
            return _missing_old_text_error(store, target, "replace")
        return tool_error(f"{missing} is required for 'replace' action.", success=False)
    if action == "remove" and not old_text:
        return _missing_old_text_error(store, target, "remove")

    # 审批门控：开启时，暂存写入（后台/网关）或内联提示
    # （交互式 CLI）；关闭时（默认）直接放行。
    gate_result = _apply_write_gate(action, target, content, old_text)
    if gate_result is not None:
        return gate_result

    if action == "add":
        result = store.add(target, content)

    elif action == "replace":
        result = store.replace(target, old_text, content)

    elif action == "remove":
        result = store.remove(target, old_text)

    else:
        return tool_error(f"Unknown action '{action}'. Use: add, replace, remove", success=False)

    return json.dumps(result, ensure_ascii=False)


def check_memory_requirements() -> bool:
    """memory 工具没有外部依赖——始终可用。"""
    return True


def apply_memory_pending(payload: Dict[str, Any], store: "MemoryStore") -> Dict[str, Any]:
    """直接对存储重放已暂存的记忆写入，绕过
    写入门控。由 /memory approve 处理器调用。

    返回存储的结果字典。
    """
    action = payload.get("action")
    target = payload.get("target", "memory")
    content = payload.get("content") or ""
    old_text = payload.get("old_text") or ""
    if action == "batch":
        return store.apply_batch(target, payload.get("operations") or [])
    if action == "add":
        return store.add(target, content)
    if action == "replace":
        return store.replace(target, old_text, content)
    if action == "remove":
        return store.remove(target, old_text)
    return {"success": False, "error": f"Unknown staged action '{action}'."}
# OpenAI 函数调用 Schema
# =============================================================================

MEMORY_SCHEMA = {
    "name": "memory",
    "description": (
        "Save durable facts to persistent memory that survive across sessions. Memory is "
        "injected into every future turn, so keep entries compact and high-signal.\n\n"
        "HOW: make ALL your changes in ONE call via an 'operations' array (each item: "
        "{action, content?, old_text?}). The batch applies atomically and the char limit is "
        "checked only on the FINAL result — so a single call can remove/replace stale entries "
        "to free room AND add new ones, even when an add alone would overflow. The response "
        "reports current/limit chars and confirms completion; one batch call finishes the "
        "update, so don't repeat it. Use the bare action/content/old_text fields only for a "
        "single lone change.\n\n"
        "WHEN: save proactively when the user states a preference, correction, or personal "
        "detail, or you learn a stable fact about their environment, conventions, or workflow. "
        "Priority: user preferences & corrections > environment facts > procedures. The best "
        "memory stops the user repeating themselves.\n\n"
        "IF FULL: an add is rejected with the current entries shown. Reissue as ONE batch that "
        "removes or shortens enough stale entries and adds the new one together.\n\n"
        "TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your "
        "notes (environment, conventions, tool quirks, lessons).\n\n"
        "SKIP: trivial/obvious info, easily re-discovered facts, raw data dumps, task progress, "
        "completed-work logs, temporary TODO state (use session_search for those). Reusable "
        "procedures belong in a skill, not memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "replace", "remove"],
                "description": "The action to perform (single-op shape). Omit when using 'operations'."
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Which memory store: 'memory' for personal notes, 'user' for user profile."
            },
            "content": {
                "type": "string",
                "description": "The entry content. Required for 'add' and 'replace' (single-op shape)."
            },
            "old_text": {
                "type": "string",
                "description": "REQUIRED for 'replace' and 'remove' (single-op shape): a short unique substring identifying the existing entry to modify. Omit only for 'add'."
            },
            "operations": {
                "type": "array",
                "description": (
                    "Batch shape: a list of operations applied atomically in one call "
                    "against the final char budget. Preferred when making multiple changes "
                    "or consolidating to make room. Each item is {action, content?, old_text?}."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                        "content": {"type": "string", "description": "Entry content for add/replace."},
                        "old_text": {"type": "string", "description": "Substring identifying the entry for replace/remove."},
                    },
                    "required": ["action"],
                },
            },
        },
        "required": ["target"],
    },
}


# --- 注册表 ---
from tools.registry import registry, tool_error

registry.register(
    name="memory",
    toolset="memory",
    schema=MEMORY_SCHEMA,
    handler=lambda args, **kw: memory_tool(
        action=args.get("action", ""),
        target=args.get("target", "memory"),
        content=args.get("content"),
        old_text=args.get("old_text"),
        operations=args.get("operations"),
        store=kw.get("store")),
    check_fn=check_memory_requirements,
    emoji="🧠",
)




