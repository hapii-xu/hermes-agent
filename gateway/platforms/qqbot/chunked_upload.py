"""QQ Bot 分块上传流程。

QQ v2 API 对内联 base64 上传（``file_data`` / ``url``）的限制约为 10 MB。
对于 10 MB 至约 100 MB 之间的文件，必须使用三步分块上传流程::

    1. POST /v2/{users|groups}/{id}/upload_prepare
       → 返回 upload_id、block_size 以及预签名 COS 分片 URL 数组。
    2. 对于每个分片：
         将分片字节 PUT 到其预签名 COS URL，
         然后 POST /v2/{users|groups}/{id}/upload_part_finish 进行确认。
    3. POST /v2/{users|groups}/{id}/files，携带 {"upload_id": ...}
       → 返回调用方在 RichMedia 消息中使用的 ``file_info`` 令牌。

错误码语义（来自 QQ Bot v2 API 规范）：

- ``40093001`` — ``upload_part_finish`` 可重试。持续重试直到服务器提供的
  ``retry_timeout`` 耗尽（或本地上限到达）。
- ``40093002`` — 每日累计上传配额已超限。不可重试；以
  :class:`UploadDailyLimitExceededError` 的形式暴露，以便调用方构建
  对用户友好的回复。

异常类：

- :class:`UploadDailyLimitExceededError` — 每日配额已达上限（不可重试）。
- :class:`UploadFileTooLargeError` — 文件超出平台单文件限制。
- :class:`RuntimeError` — 通用上传失败（网络、分片 PUT、完成接口）。

移植自 WideLee 的 qqbot-agent-sdk v1.2.2（``media_loader.py::ChunkedUploader``），
以便大文件上传路径保留在代码库中。通过 Co-authored-by 保留原作者信息。
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from gateway.platforms.qqbot.constants import FILE_UPLOAD_TIMEOUT

logger = logging.getLogger(__name__)


# ── 错误码 ──────────────────────────────────────────────────────
_BIZ_CODE_DAILY_LIMIT = 40093002     # upload_prepare：每日累计上传配额超限
_BIZ_CODE_PART_RETRYABLE = 40093001  # upload_part_finish：瞬态错误，可重试

# ── 分片上传调优参数 ───────────────────────────────────────────────
_DEFAULT_CONCURRENT_PARTS = 1
_MAX_CONCURRENT_PARTS = 10

_PART_UPLOAD_TIMEOUT = 300.0        # 每次 COS PUT 最长 5 分钟
_PART_UPLOAD_MAX_RETRIES = 2
_PART_FINISH_RETRY_INTERVAL = 1.0
_PART_FINISH_DEFAULT_TIMEOUT = 120.0
_PART_FINISH_MAX_TIMEOUT = 600.0

_COMPLETE_UPLOAD_MAX_RETRIES = 2
_COMPLETE_UPLOAD_BASE_DELAY = 2.0

# 前 10,002,432 字节用于计算 ``md5_10m`` 哈希值（依据 QQ API 规范）。
_MD5_10M_SIZE = 10_002_432


# ── 异常类 ───────────────────────────────────────────────────────

class UploadDailyLimitExceededError(Exception):
    """当 ``upload_prepare`` 返回 biz_code 40093002 时抛出。

    该 bot 的每日累计上传配额已达上限。调用方应将 :attr:`file_name`
    与 :attr:`file_size_human` 暴露给模型，以便其组织出友好的回复。
    """

    def __init__(self, file_name: str, file_size: int, message: str = "") -> None:
        self.file_name = file_name
        self.file_size = file_size
        super().__init__(
            message or f"Daily upload limit exceeded for {file_name!r}"
        )

    @property
    def file_size_human(self) -> str:
        return format_size(self.file_size)


class UploadFileTooLargeError(Exception):
    """当文件超出平台单文件大小限制时抛出。"""

    def __init__(
        self,
        file_name: str,
        file_size: int,
        limit_bytes: int = 0,
        message: str = "",
    ) -> None:
        self.file_name = file_name
        self.file_size = file_size
        self.limit_bytes = limit_bytes
        limit_str = f" ({format_size(limit_bytes)})" if limit_bytes else ""
        super().__init__(
            message
            or (
                f"File {file_name!r} ({format_size(file_size)}) "
                f"exceeds platform limit{limit_str}"
            )
        )

    @property
    def file_size_human(self) -> str:
        return format_size(self.file_size)

    @property
    def limit_human(self) -> str:
        return format_size(self.limit_bytes) if self.limit_bytes else "unknown"


# ── 上传进度追踪 ────────────────────────────────────────────────

@dataclass
class _UploadProgress:
    total_parts: int = 0
    total_bytes: int = 0
    completed_parts: int = 0
    uploaded_bytes: int = 0


# ── Prepare 响应结构 ───────────────────────────────────────────

@dataclass
class _PreparePart:
    index: int
    presigned_url: str
    block_size: int = 0


@dataclass
class _PrepareResult:
    upload_id: str
    block_size: int
    parts: List[_PreparePart]
    concurrency: int = _DEFAULT_CONCURRENT_PARTS
    retry_timeout: float = 0.0


def _parse_prepare_response(raw: Dict[str, Any]) -> _PrepareResult:
    """将 upload_prepare API 响应解析为规范化结构。

    API 可能直接返回响应，也可能将其包装在 ``data`` 字段中。
    """
    src = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    upload_id = str(src.get("upload_id", ""))
    if not upload_id:
        raise ValueError(
            f"upload_prepare response missing upload_id: {str(raw)[:200]}"
        )
    block_size = int(src.get("block_size", 0))
    raw_parts = src.get("parts") or src.get("part_list") or []
    if not isinstance(raw_parts, list) or not raw_parts:
        raise ValueError(
            f"upload_prepare response missing parts: {str(raw)[:200]}"
        )
    parts: List[_PreparePart] = []
    for p in raw_parts:
        if not isinstance(p, dict):
            continue
        parts.append(
            _PreparePart(
                index=int(p.get("part_index") or p.get("index") or 0),
                presigned_url=str(
                    p.get("presigned_url") or p.get("url") or ""
                ),
                block_size=int(p.get("block_size", 0)),
            )
        )
    return _PrepareResult(
        upload_id=upload_id,
        block_size=block_size,
        parts=parts,
        concurrency=int(src.get("concurrency", _DEFAULT_CONCURRENT_PARTS)) or _DEFAULT_CONCURRENT_PARTS,
        retry_timeout=float(src.get("retry_timeout", 0.0) or 0.0),
    )


# ── 分块上传驱动器 ────────────────────────────────────────────

ApiRequestFn = Callable[..., Awaitable[Dict[str, Any]]]
"""适配器 ``_api_request`` 可调用对象的函数签名。

我们传入绑定方法而非直接导入适配器，以避免循环导入并保持本模块可独立测试。
"""


class ChunkedUploader:
    """执行 prepare → PUT 分片 → complete 的完整上传序列。

    :param api_request: 来自适配器的绑定协程
        ``_api_request(method, path, body=..., timeout=...)``。
        在 API 错误时必须抛出携带 biz_code 的 ``RuntimeError``。
    :param http_put: 用于 COS 分片上传的协程
        ``(url, data, headers, timeout) -> response``，
        通常封装自 ``httpx.AsyncClient.put``。
    :param log_tag: 日志前缀标签。
    """

    def __init__(
        self,
        api_request: ApiRequestFn,
        http_put: Callable[..., Awaitable[Any]],
        log_tag: str = "QQBot",
    ) -> None:
        self._api_request = api_request
        self._http_put = http_put
        self._log_tag = log_tag

    async def upload(
        self,
        chat_type: str,
        target_id: str,
        file_path: str,
        file_type: int,
        file_name: str,
    ) -> Dict[str, Any]:
        """执行完整的分块上传并返回 ``complete_upload`` 响应。

        :param chat_type: ``'c2c'`` 或 ``'group'``。
        :param target_id: 用户或群组 openid。
        :param file_path: 本地文件的绝对路径。
        :param file_type: ``MEDIA_TYPE_*`` 常量。
        :param file_name: 原始文件名（用于 upload_prepare）。
        :returns: 来自 ``complete_upload`` 的原始响应字典 —
            包含调用方在 RichMedia 消息体中使用的 ``file_info``。
        :raises UploadDailyLimitExceededError: 当 biz_code 为 40093002 时。
        :raises UploadFileTooLargeError: 当文件超出平台限制时。
        :raises RuntimeError: 其他 API 或 I/O 故障时。
        """
        if chat_type not in {"c2c", "group"}:
            raise ValueError(
                f"ChunkedUploader: unsupported chat_type {chat_type!r}"
            )

        path = Path(file_path)
        file_size = path.stat().st_size

        logger.info(
            "[%s] Chunked upload start: file=%s size=%s type=%d",
            self._log_tag, file_name, format_size(file_size), file_type,
        )

        # 步骤 1：计算哈希值（阻塞 I/O → 线程池执行器）。
        hashes = await asyncio.get_running_loop().run_in_executor(
            None, _compute_file_hashes, file_path, file_size
        )

        # 步骤 2：upload_prepare。
        prepare = await self._prepare(
            chat_type, target_id, file_type, file_name, file_size, hashes
        )
        max_concurrent = min(prepare.concurrency, _MAX_CONCURRENT_PARTS)
        retry_timeout = min(
            prepare.retry_timeout if prepare.retry_timeout > 0 else _PART_FINISH_DEFAULT_TIMEOUT,
            _PART_FINISH_MAX_TIMEOUT,
        )
        logger.info(
            "[%s] Prepared: upload_id=%s block_size=%s parts=%d concurrency=%d",
            self._log_tag, prepare.upload_id, format_size(prepare.block_size),
            len(prepare.parts), max_concurrent,
        )

        progress = _UploadProgress(
            total_parts=len(prepare.parts),
            total_bytes=file_size,
        )

        # 步骤 3：PUT 每个分片并通知服务端。
        tasks: List[Callable[[], Awaitable[None]]] = [
            functools.partial(
                self._upload_one_part,
                chat_type=chat_type,
                target_id=target_id,
                file_path=file_path,
                file_size=file_size,
                upload_id=prepare.upload_id,
                rsp_block_size=prepare.block_size,
                part=part,
                retry_timeout=retry_timeout,
                progress=progress,
            )
            for part in prepare.parts
        ]
        await _run_with_concurrency(tasks, max_concurrent)

        logger.info(
            "[%s] All %d parts uploaded, completing…",
            self._log_tag, len(prepare.parts),
        )

        # 步骤 4：complete_upload（瞬态错误时重试）。
        return await self._complete(chat_type, target_id, prepare.upload_id)

    # ──────────────────────────────────────────────────────────────────
    # 步骤 1 — upload_prepare
    # ──────────────────────────────────────────────────────────────────

    async def _prepare(
        self,
        chat_type: str,
        target_id: str,
        file_type: int,
        file_name: str,
        file_size: int,
        hashes: Dict[str, str],
    ) -> _PrepareResult:
        base = "/v2/users" if chat_type == "c2c" else "/v2/groups"
        path = f"{base}/{target_id}/upload_prepare"
        body = {
            "file_type": file_type,
            "file_name": file_name,
            "file_size": file_size,
            "md5": hashes["md5"],
            "sha1": hashes["sha1"],
            "md5_10m": hashes["md5_10m"],
        }
        try:
            raw = await self._api_request(
                "POST", path, body=body, timeout=FILE_UPLOAD_TIMEOUT
            )
        except RuntimeError as exc:
            err_msg = str(exc)
            if f"{_BIZ_CODE_DAILY_LIMIT}" in err_msg:
                raise UploadDailyLimitExceededError(
                    file_name, file_size, err_msg
                ) from exc
            raise
        return _parse_prepare_response(raw)

    # ──────────────────────────────────────────────────────────────────
    # 步骤 2 — PUT 单个分片 + part_finish
    # ──────────────────────────────────────────────────────────────────

    async def _upload_one_part(
        self,
        chat_type: str,
        target_id: str,
        file_path: str,
        file_size: int,
        upload_id: str,
        rsp_block_size: int,
        part: _PreparePart,
        retry_timeout: float,
        progress: _UploadProgress,
    ) -> None:
        """将单个分片 PUT 到 COS，然后调用 ``upload_part_finish``。"""
        part_index = part.index
        # 优先使用分片级别的 block_size；若无则退回到响应级别的值。
        actual_block_size = part.block_size if part.block_size > 0 else rsp_block_size
        offset = (part_index - 1) * rsp_block_size
        length = min(actual_block_size, file_size - offset)

        # 读取文件中的该切片（阻塞操作 → 线程池执行器）。
        data = await asyncio.get_running_loop().run_in_executor(
            None, _read_file_chunk, file_path, offset, length
        )
        md5_hex = hashlib.md5(data).hexdigest()

        logger.debug(
            "[%s] Part %d/%d: uploading %s (offset=%d md5=%s)",
            self._log_tag, part_index, progress.total_parts,
            format_size(length), offset, md5_hex,
        )

        await self._put_to_presigned_url(
            part.presigned_url, data, part_index, progress.total_parts
        )
        await self._part_finish_with_retry(
            chat_type, target_id, upload_id,
            part_index, length, md5_hex, retry_timeout,
        )

        progress.completed_parts += 1
        progress.uploaded_bytes += length
        logger.debug(
            "[%s] Part %d/%d done (%d/%d total)",
            self._log_tag, part_index, progress.total_parts,
            progress.completed_parts, progress.total_parts,
        )

    async def _put_to_presigned_url(
        self,
        url: str,
        data: bytes,
        part_index: int,
        total_parts: int,
    ) -> None:
        """将分片数据 PUT 到预签名 COS URL，失败时重试。"""
        last_exc: Optional[Exception] = None
        for attempt in range(_PART_UPLOAD_MAX_RETRIES + 1):
            try:
                resp = await asyncio.wait_for(
                    self._http_put(
                        url,
                        data=data,
                        headers={"Content-Length": str(len(data))},
                    ),
                    timeout=_PART_UPLOAD_TIMEOUT,
                )
                # 调用方的 http_put 预期返回类 httpx 的响应对象。
                status = getattr(resp, "status_code", 0)
                if 200 <= status < 300:
                    logger.debug(
                        "[%s] PUT part %d/%d: %d OK",
                        self._log_tag, part_index, total_parts, status,
                    )
                    return
                body_preview = ""
                try:
                    body_preview = getattr(resp, "text", "")[:200]
                except Exception:  # pragma: no cover — defensive
                    pass
                raise RuntimeError(
                    f"COS PUT returned {status}: {body_preview}"
                )
            except Exception as exc:
                last_exc = exc
                if attempt < _PART_UPLOAD_MAX_RETRIES:
                    delay = 1.0 * (2 ** attempt)
                    logger.warning(
                        "[%s] PUT part %d/%d attempt %d failed, retry in %.1fs: %s",
                        self._log_tag, part_index, total_parts,
                        attempt + 1, delay, exc,
                    )
                    await asyncio.sleep(delay)
        raise RuntimeError(
            f"Part {part_index}/{total_parts} upload failed after "
            f"{_PART_UPLOAD_MAX_RETRIES + 1} attempts: {last_exc}"
        )

    async def _part_finish_with_retry(
        self,
        chat_type: str,
        target_id: str,
        upload_id: str,
        part_index: int,
        block_size: int,
        md5: str,
        retry_timeout: float,
    ) -> None:
        """调用 ``upload_part_finish``，遇到 biz_code 40093001 时重试。"""
        base = "/v2/users" if chat_type == "c2c" else "/v2/groups"
        path = f"{base}/{target_id}/upload_part_finish"
        body = {
            "upload_id": upload_id,
            "part_index": part_index,
            "block_size": block_size,
            "md5": md5,
        }

        loop = asyncio.get_running_loop()
        start = loop.time()
        attempt = 0
        while True:
            try:
                await self._api_request(
                    "POST", path, body=body, timeout=FILE_UPLOAD_TIMEOUT
                )
                return
            except RuntimeError as exc:
                err_msg = str(exc)
                if f"{_BIZ_CODE_PART_RETRYABLE}" not in err_msg:
                    raise
                elapsed = loop.time() - start
                if elapsed >= retry_timeout:
                    raise RuntimeError(
                        f"upload_part_finish persistent retry timed out "
                        f"after {retry_timeout:.0f}s ({attempt} retries): {exc}"
                    ) from exc
                attempt += 1
                logger.debug(
                    "[%s] part_finish retryable error, attempt %d, "
                    "elapsed=%.1fs: %s",
                    self._log_tag, attempt, elapsed, exc,
                )
                await asyncio.sleep(_PART_FINISH_RETRY_INTERVAL)

    # ──────────────────────────────────────────────────────────────────
    # 步骤 3 — complete_upload
    # ──────────────────────────────────────────────────────────────────

    async def _complete(
        self,
        chat_type: str,
        target_id: str,
        upload_id: str,
    ) -> Dict[str, Any]:
        """调用 ``complete_upload``，失败时重试。

        复用 ``/files`` 接口（与基于 URL 的简单上传相同），
        但仅发送 ``upload_id`` 以表明走分块上传完成路径。
        """
        base = "/v2/users" if chat_type == "c2c" else "/v2/groups"
        path = f"{base}/{target_id}/files"
        body = {"upload_id": upload_id}

        last_exc: Optional[Exception] = None
        for attempt in range(_COMPLETE_UPLOAD_MAX_RETRIES + 1):
            try:
                return await self._api_request(
                    "POST", path, body=body, timeout=FILE_UPLOAD_TIMEOUT
                )
            except Exception as exc:
                last_exc = exc
                if attempt < _COMPLETE_UPLOAD_MAX_RETRIES:
                    delay = _COMPLETE_UPLOAD_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        "[%s] complete_upload attempt %d failed, "
                        "retry in %.1fs: %s",
                        self._log_tag, attempt + 1, delay, exc,
                    )
                    await asyncio.sleep(delay)
        raise RuntimeError(
            f"complete_upload failed after "
            f"{_COMPLETE_UPLOAD_MAX_RETRIES + 1} attempts: {last_exc}"
        )


# ── 辅助函数（模块级，便于测试） ───────────────────────────

def format_size(size_bytes: int) -> str:
    """返回人类可读的文件大小字符串（例如 ``'12.3 MB'``）。"""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def _read_file_chunk(file_path: str, offset: int, length: int) -> bytes:
    """从 *file_path* 的 *offset* 位置开始读取 *length* 字节。

    :raises IOError: 实际读取的字节数少于预期（文件被截断）。
    """
    with open(file_path, "rb") as fh:
        fh.seek(offset)
        data = fh.read(length)
        if len(data) != length:
            raise IOError(
                f"Short read from {file_path}: expected {length} bytes at "
                f"offset {offset}, got {len(data)} (file may be truncated)"
            )
        return data


def _compute_file_hashes(file_path: str, file_size: int) -> Dict[str, str]:
    """单次遍历计算 md5、sha1 和 md5_10m。"""
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    md5_10m = hashlib.md5()

    need_10m = file_size > _MD5_10M_SIZE
    bytes_read = 0

    with open(file_path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            md5.update(chunk)
            sha1.update(chunk)
            if need_10m:
                remaining = _MD5_10M_SIZE - bytes_read
                if remaining > 0:
                    md5_10m.update(chunk[:remaining])
            bytes_read += len(chunk)

    full_md5 = md5.hexdigest()
    return {
        "md5": full_md5,
        "sha1": sha1.hexdigest(),
        # 对于小文件，"10m" 哈希值就是完整的 md5。
        "md5_10m": md5_10m.hexdigest() if need_10m else full_md5,
    }


async def _run_with_concurrency(
    tasks: List[Callable[[], Awaitable[None]]],
    concurrency: int,
) -> None:
    """并发执行一组 thunk，同时最多在途数量受限。"""
    concurrency = max(concurrency, 1)
    sem = asyncio.Semaphore(concurrency)

    async def _wrap(thunk: Callable[[], Awaitable[None]]) -> None:
        async with sem:
            await thunk()

    await asyncio.gather(*(_wrap(t) for t in tasks))
