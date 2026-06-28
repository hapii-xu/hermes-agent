"""
DM 配对（Pairing）系统

基于验证码的审批流程，用于在消息平台上授权新用户。与使用用户 ID 的静态
allowlist 不同，未知用户会收到一个一次性配对码，由 bot owner 通过 CLI
审批。

安全特性（基于 OWASP + NIST SP 800-63-4 指南）：
  - 8 字符的码，取自 32 字符的无歧义字母表（不含 0/O/1/I）
  - 通过 secrets.choice() 实现密码学随机性
  - 1 小时的码有效期
  - 每个平台最多 3 个待处理码
  - 限流：每个用户每 10 分钟 1 次请求
  - 5 次失败审批后锁定（1 小时）
  - 文件权限：所有数据文件 chmod 0600
  - 码绝不输出到 stdout

存储位置：~/.hermes/pairing/
"""

import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from gateway.whatsapp_identity import (
    expand_whatsapp_aliases,
    normalize_whatsapp_identifier,
)
from hermes_constants import get_hermes_dir
from utils import atomic_replace


# 无歧义字母表 —— 排除 0/O、1/I 以防混淆
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8

# 时间常量
CODE_TTL_SECONDS = 3600             # 码在 1 小时后过期
RATE_LIMIT_SECONDS = 600            # 每个用户每 10 分钟 1 次请求
LOCKOUT_SECONDS = 3600              # 失败次数过多后的锁定时长

# 限制
MAX_PENDING_PER_PLATFORM = 3        # 每个平台的最大待处理码数
MAX_FAILED_ATTEMPTS = 5             # 锁定前的失败审批次数

PAIRING_DIR = get_hermes_dir("platforms/pairing", "pairing")


def _secure_write(path: Path, data: str) -> None:
    """以严格的权限（仅 owner 可读写）将数据写入文件。

    使用临时文件 + 原子重命名，这样读取者要么看到旧的完整文件，要么看到
    新的完整文件——绝不会看到写了一半的内容。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # Windows 对 chmod 的支持方式不同
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class PairingStore:
    """
    管理配对码和已批准用户列表。

    每个平台对应的数据文件：
      - {platform}-pending.json   : 待处理的配对请求
      - {platform}-approved.json  : 已批准（已配对）的用户
      - _rate_limits.json         : 限流追踪
    """

    def __init__(self):
        PAIRING_DIR.mkdir(parents=True, exist_ok=True)
        # 保护所有的 read-modify-write 周期。gateway 会并发地在多个线程中
        # 运行多个平台 adapter，它们共享同一个 PairingStore。
        self._lock = threading.RLock()

    def _pending_path(self, platform: str) -> Path:
        return PAIRING_DIR / f"{platform}-pending.json"

    def _approved_path(self, platform: str) -> Path:
        return PAIRING_DIR / f"{platform}-approved.json"

    def _rate_limit_path(self) -> Path:
        return PAIRING_DIR / "_rate_limits.json"

    def _load_json(self, path: Path) -> dict:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_json(self, path: Path, data: dict) -> None:
        _secure_write(path, json.dumps(data, indent=2, ensure_ascii=False))

    def _normalize_user_id(self, platform: str, user_id: str) -> str:
        """在持久化之前规范化平台特定的用户 ID。"""
        raw_user_id = str(user_id or "").strip()
        if platform == "whatsapp":
            return normalize_whatsapp_identifier(raw_user_id) or raw_user_id
        return raw_user_id

    def _user_id_aliases(self, platform: str, user_id: str) -> set[str]:
        """返回所有已知的等价用户 ID，用于授权/限流检查。"""
        raw_user_id = str(user_id or "").strip()
        if not raw_user_id:
            return set()

        aliases = {raw_user_id, self._normalize_user_id(platform, raw_user_id)}
        if platform == "whatsapp":
            aliases.update(expand_whatsapp_aliases(raw_user_id))
        aliases.discard("")
        return aliases

    def _user_ids_match(self, platform: str, left: str, right: str) -> bool:
        """当两个用户 ID 代表同一个主体时返回 True。"""
        left_aliases = self._user_id_aliases(platform, left)
        right_aliases = self._user_id_aliases(platform, right)
        return bool(left_aliases and right_aliases and (left_aliases & right_aliases))

    # ----- 已批准用户 -----

    def is_approved(self, platform: str, user_id: str) -> bool:
        """检查某用户是否在某平台上已被批准（已配对）。"""
        approved = self._load_json(self._approved_path(platform))
        for approved_user_id in approved:
            if self._user_ids_match(platform, approved_user_id, user_id):
                return True
        return False

    def list_approved(self, platform: str = None) -> list:
        """列出已批准用户，可按平台过滤。"""
        results = []
        platforms = [platform] if platform else self._all_platforms("approved")
        for p in platforms:
            approved = self._load_json(self._approved_path(p))
            for uid, info in approved.items():
                results.append({"platform": p, "user_id": uid, **info})
        return results

    def _approve_user(self, platform: str, user_id: str, user_name: str = "") -> None:
        """将用户加入已批准列表。必须在持有 self._lock 时调用。"""
        approved = self._load_json(self._approved_path(platform))
        normalized_user_id = self._normalize_user_id(platform, user_id)
        duplicate_ids = [
            approved_user_id
            for approved_user_id in approved
            if self._user_ids_match(platform, approved_user_id, normalized_user_id)
        ]
        for approved_user_id in duplicate_ids:
            del approved[approved_user_id]

        approved[normalized_user_id] = {
            "user_name": user_name,
            "approved_at": time.time(),
        }
        self._save_json(self._approved_path(platform), approved)

    def revoke(self, platform: str, user_id: str) -> bool:
        """从已批准列表中移除用户。找到则返回 True。"""
        path = self._approved_path(platform)
        with self._lock:
            approved = self._load_json(path)
            matching_ids = [
                approved_user_id
                for approved_user_id in approved
                if self._user_ids_match(platform, approved_user_id, user_id)
            ]
            if matching_ids:
                for approved_user_id in matching_ids:
                    del approved[approved_user_id]
                self._save_json(path, approved)
                return True
        return False

    # ----- 待处理码 -----

    @staticmethod
    def _hash_code(code: str, salt: bytes) -> str:
        """使用给定 salt 对配对码做 SHA-256 哈希。"""
        return hashlib.sha256(salt + code.encode("utf-8")).hexdigest()

    def generate_code(
        self, platform: str, user_id: str, user_name: str = ""
    ) -> Optional[str]:
        """
        为新用户生成一个配对码。

        返回码字符串；在以下情况返回 None：
          - 用户被限流（请求过于频繁）
          - 本平台的待处理码已达上限
          - 因失败次数过多，用户/平台处于锁定状态

        码不会以明文存储。只持久化一个带 salt 的 SHA-256 哈希，因此读取
        待处理文件并不能还原出码。
        """
        with self._lock:
            self._cleanup_expired(platform)
            normalized_user_id = self._normalize_user_id(platform, user_id)

            # 检查锁定
            if self._is_locked_out(platform):
                return None

            # 检查该特定用户的限流
            if self._is_rate_limited(platform, user_id):
                return None

            # 检查待处理上限
            pending = self._load_json(self._pending_path(platform))
            if len(pending) >= MAX_PENDING_PER_PLATFORM:
                return None

            # 生成密码学随机码
            code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))

            # 在存储前用随机 salt 对码做哈希
            salt = os.urandom(16)
            code_hash = self._hash_code(code, salt)

            # 用一个唯一的 entry id 作为键（而不是码本身）
            entry_id = secrets.token_hex(8)

            # 以哈希后的码存储待处理请求
            pending[entry_id] = {
                "hash": code_hash,
                "salt": salt.hex(),
                "user_id": normalized_user_id,
                "user_name": user_name,
                "created_at": time.time(),
            }
            self._save_json(self._pending_path(platform), pending)

            # 记录限流
            self._record_rate_limit(platform, user_id)

            return code

    def approve_code(self, platform: str, code: str) -> Optional[dict]:
        """
        审批一个配对码。把对应用户加入已批准列表。

        成功时返回 ``{user_id, user_name}``；当码无效/过期，或平台因
        ``MAX_FAILED_ATTEMPTS`` 次失败审批而处于锁定状态时返回 ``None``
        （#10195）。调用方可以用 ``_is_locked_out(platform)`` 加以区分。

        校验方式：用户提供的码会用每个已存储条目的 salt 做哈希，再用
        常数时间比较与已存储的哈希进行比对。预哈希条目（升级前
        pending.json 文件中的遗留明文键格式）会被静默忽略——它们会在
        TTL 到期时被 ``_cleanup_expired`` 清理。
        """
        with self._lock:
            self._cleanup_expired(platform)
            code = code.upper().strip()

            # 锁定检查 —— 必须在待处理查找之前运行，这样一旦锁定触发，
            # 一个有效的码（例如已存在于 pending 中的码）也无法被接受。
            # 如果不做这一步，锁定只会挡住 `generate_code`，而不会挡住
            # `approve_code`——这会让任何已发出的码都失去暴力破解防护。
            if self._is_locked_out(platform):
                return None

            pending = self._load_json(self._pending_path(platform))

            # 查找其哈希与所提供码匹配的条目。
            # 容忍遗留的明文键条目（没有 salt/hash）和格式错误的条目 ——
            # 跳过它们而不是抛出 KeyError，这样在现有 pending.json 之上
            # 进行原地升级时，第一次 approve 调用就不会崩溃。遗留条目会在
            # 它们的 TTL 到期时被 _cleanup_expired 清理。
            matched_key = None
            matched_entry = None
            for entry_id, entry in pending.items():
                if not isinstance(entry, dict):
                    continue
                if "salt" not in entry or "hash" not in entry:
                    continue
                try:
                    salt = bytes.fromhex(entry["salt"])
                except ValueError:
                    continue
                candidate_hash = self._hash_code(code, salt)
                if secrets.compare_digest(candidate_hash, entry["hash"]):
                    matched_key = entry_id
                    matched_entry = entry
                    break

            if matched_key is None:
                self._record_failed_attempt(platform)
                return None

            del pending[matched_key]
            self._save_json(self._pending_path(platform), pending)

            # 加入已批准列表
            self._approve_user(platform, matched_entry["user_id"],
                               matched_entry.get("user_name", ""))

            return {
                "user_id": matched_entry["user_id"],
                "user_name": matched_entry.get("user_name", ""),
            }

    def list_pending(self, platform: str = None) -> list:
        """列出待处理配对请求，可按平台过滤。

        码以哈希形式存储——``code`` 字段会被替换为哈希的前 8 个十六进制字符，
        这样管理员就能区分条目，而不会暴露原始码。遗留的明文键条目
        （预哈希格式）会以 "legacy" 占位符展示，这样管理员能看到它们逐步
        老化淘汰，而不会因为缺少 ``hash`` 字段而崩溃。
        """
        results = []
        with self._lock:
            platforms = [platform] if platform else self._all_platforms("pending")
            for p in platforms:
                self._cleanup_expired(p)
                pending = self._load_json(self._pending_path(p))
                for entry_id, info in pending.items():
                    if not isinstance(info, dict):
                        continue
                    created_at = info.get("created_at")
                    if not isinstance(created_at, (int, float)):
                        continue
                    age_min = int((time.time() - created_at) / 60)
                    hash_val = info.get("hash")
                    code_display = hash_val[:8] if isinstance(hash_val, str) else "legacy"
                    results.append({
                        "platform": p,
                        "code": code_display,
                        "user_id": info.get("user_id", ""),
                        "user_name": info.get("user_name", ""),
                        "age_minutes": age_min,
                    })
        return results

    def clear_pending(self, platform: str = None) -> int:
        """清空所有待处理请求。返回被移除的数量。"""
        with self._lock:
            count = 0
            platforms = [platform] if platform else self._all_platforms("pending")
            for p in platforms:
                pending = self._load_json(self._pending_path(p))
                count += len(pending)
                self._save_json(self._pending_path(p), {})
        return count

    # ----- 限流与锁定 -----

    def _is_rate_limited(self, platform: str, user_id: str) -> bool:
        """检查用户是否在过于近期内已请求过码。"""
        limits = self._load_json(self._rate_limit_path())
        for alias in self._user_id_aliases(platform, user_id):
            key = f"{platform}:{alias}"
            last_request = limits.get(key, 0)
            if (time.time() - last_request) < RATE_LIMIT_SECONDS:
                return True
        return False

    def _record_rate_limit(self, platform: str, user_id: str) -> None:
        """记录配对请求的时间，用于限流。"""
        limits = self._load_json(self._rate_limit_path())
        now = time.time()
        for alias in self._user_id_aliases(platform, user_id):
            key = f"{platform}:{alias}"
            limits[key] = now
        self._save_json(self._rate_limit_path(), limits)

    def _is_locked_out(self, platform: str) -> bool:
        """检查某平台是否因失败审批次数过多而处于锁定状态。"""
        limits = self._load_json(self._rate_limit_path())
        lockout_key = f"_lockout:{platform}"
        lockout_until = limits.get(lockout_key, 0)
        return time.time() < lockout_until

    def _record_failed_attempt(self, platform: str) -> None:
        """记录一次失败的审批尝试。在达到 MAX_FAILED_ATTEMPTS 后触发锁定。"""
        limits = self._load_json(self._rate_limit_path())
        fail_key = f"_failures:{platform}"
        fails = limits.get(fail_key, 0) + 1
        limits[fail_key] = fails
        if fails >= MAX_FAILED_ATTEMPTS:
            lockout_key = f"_lockout:{platform}"
            limits[lockout_key] = time.time() + LOCKOUT_SECONDS
            limits[fail_key] = 0  # 重置计数器
            print(f"[pairing] Platform {platform} locked out for {LOCKOUT_SECONDS}s "
                  f"after {MAX_FAILED_ATTEMPTS} failed attempts", flush=True)
        self._save_json(self._rate_limit_path(), limits)

    # ----- 清理 -----

    def _cleanup_expired(self, platform: str) -> None:
        """移除已过期的待处理码。

        容忍格式错误 / 遗留条目 —— 任何没有数字型 ``created_at`` 的条目
        都被视为已过期（反正在新哈希键 schema 下它也无法使用）。
        """
        path = self._pending_path(platform)
        pending = self._load_json(path)
        now = time.time()
        expired = []
        for entry_id, info in pending.items():
            if not isinstance(info, dict):
                expired.append(entry_id)
                continue
            created_at = info.get("created_at")
            if not isinstance(created_at, (int, float)):
                expired.append(entry_id)
                continue
            if (now - created_at) > CODE_TTL_SECONDS:
                expired.append(entry_id)
        if expired:
            for entry_id in expired:
                del pending[entry_id]
            self._save_json(path, pending)

    def _all_platforms(self, suffix: str) -> list:
        """列出所有拥有指定后缀数据文件的平台。"""
        platforms = []
        for f in PAIRING_DIR.iterdir():
            if f.name.endswith(f"-{suffix}.json"):
                platform = f.name.replace(f"-{suffix}.json", "")
                if not platform.startswith("_"):
                    platforms.append(platform)
        return platforms
