"""
QQBot 扫码配置（二维码入驻引导）模块。

复刻飞书入驻引导模式：同步 HTTP + 单一公开入口 ``qr_register()``，
该函数处理完整流程（创建任务 → 展示二维码 → 轮询 → 解密凭据）。

调用 ``q.qq.com`` 的 ``create_bind_task`` / ``poll_bind_result`` API，
生成二维码 URL 并轮询扫码完成状态。成功后调用方将收到 bot 的
*app_id*、（本地解密的）*client_secret* 以及扫码者的 *user_openid*，
足以完整配置 QQBot 网关。

参考文档：https://bot.q.qq.com/wiki/develop/api-v2/
"""

from __future__ import annotations

import logging
import time
from enum import IntEnum
from typing import Optional, Tuple
from urllib.parse import quote

from .constants import (
    ONBOARD_API_TIMEOUT,
    ONBOARD_CREATE_PATH,
    ONBOARD_POLL_INTERVAL,
    ONBOARD_POLL_PATH,
    PORTAL_HOST,
    QR_URL_TEMPLATE,
)
from .crypto import decrypt_secret, generate_bind_key
from .utils import get_api_headers

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 绑定状态
# ---------------------------------------------------------------------------


class BindStatus(IntEnum):
    """``_poll_bind_result`` 返回的状态码。"""

    NONE = 0
    PENDING = 1
    COMPLETED = 2
    EXPIRED = 3


# ---------------------------------------------------------------------------
# 二维码渲染
# ---------------------------------------------------------------------------

try:
    import qrcode as _qrcode_mod
except (ImportError, TypeError):
    _qrcode_mod = None  # type: ignore[assignment]


def _render_qr(url: str) -> bool:
    """尝试在终端渲染二维码。成功返回 True。"""
    if _qrcode_mod is None:
        return False
    try:
        qr = _qrcode_mod.QRCode(
            error_correction=_qrcode_mod.constants.ERROR_CORRECT_M,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 同步 HTTP 辅助函数（复刻飞书 _post_registration 模式）
# ---------------------------------------------------------------------------


def _create_bind_task(timeout: float = ONBOARD_API_TIMEOUT) -> Tuple[str, str]:
    """创建绑定任务并返回 *(task_id, aes_key_base64)*。

    Raises:
        RuntimeError: 若 API 返回非零 ``retcode``。
    """
    import httpx

    url = f"https://{PORTAL_HOST}{ONBOARD_CREATE_PATH}"
    key = generate_bind_key()

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.post(url, json={"key": key}, headers=get_api_headers())
        resp.raise_for_status()
        data = resp.json()

    if data.get("retcode") != 0:
        raise RuntimeError(data.get("msg", "create_bind_task failed"))

    task_id = data.get("data", {}).get("task_id")
    if not task_id:
        raise RuntimeError("create_bind_task: missing task_id in response")

    logger.debug("create_bind_task ok: task_id=%s", task_id)
    return task_id, key


def _poll_bind_result(
    task_id: str,
    timeout: float = ONBOARD_API_TIMEOUT,
) -> Tuple[BindStatus, str, str, str]:
    """轮询 *task_id* 对应的绑定结果。

    Returns:
        4 元组 ``(status, bot_appid, bot_encrypt_secret, user_openid)``。

    Raises:
        RuntimeError: 若 API 返回非零 ``retcode``。
    """
    import httpx

    url = f"https://{PORTAL_HOST}{ONBOARD_POLL_PATH}"

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.post(url, json={"task_id": task_id}, headers=get_api_headers())
        resp.raise_for_status()
        data = resp.json()

    if data.get("retcode") != 0:
        raise RuntimeError(data.get("msg", "poll_bind_result failed"))

    d = data.get("data", {})
    return (
        BindStatus(d.get("status", 0)),
        str(d.get("bot_appid", "")),
        d.get("bot_encrypt_secret", ""),
        d.get("user_openid", ""),
    )


def build_connect_url(task_id: str) -> str:
    """为给定的 *task_id* 构建二维码目标 URL。"""
    return QR_URL_TEMPLATE.format(task_id=quote(task_id))


# ---------------------------------------------------------------------------
# 公开入口点
# ---------------------------------------------------------------------------

_MAX_REFRESHES = 3


def qr_register(timeout_seconds: int = 600) -> Optional[dict]:
    """执行 QQBot 扫码配置二维码注册流程。

    复刻 ``feishu.qr_register()``：在单次调用中依次处理创建 → 展示 →
    轮询 → 解密。意外错误会向上层调用方传播。

    :returns:
        成功时返回 ``{"app_id": ..., "client_secret": ..., "user_openid": ...}``，
        失败、过期或取消时返回 ``None``。
    """
    deadline = time.monotonic() + timeout_seconds

    for refresh_count in range(_MAX_REFRESHES + 1):
        # ── 创建绑定任务 ──
        try:
            task_id, aes_key = _create_bind_task()
        except Exception as exc:
            logger.warning("[QQBot onboard] Failed to create bind task: %s", exc)
            return None

        url = build_connect_url(task_id)

        # ── 展示二维码与 URL ──
        print()
        if _render_qr(url):
            print(f"  请扫描上方二维码，或直接打开以下链接：\n  {url}")
        else:
            print(f"  请在手机 QQ 中打开以下链接：\n  {url}")
            print("  提示：执行 pip install qrcode 可在此处显示可扫描的二维码")
        print()

        # ── 轮询循环 ──
        while time.monotonic() < deadline:
            try:
                status, app_id, encrypted_secret, user_openid = _poll_bind_result(task_id)
            except Exception:
                time.sleep(ONBOARD_POLL_INTERVAL)
                continue

            if status == BindStatus.COMPLETED:
                client_secret = decrypt_secret(encrypted_secret, aes_key)
                print()
                print(f"  二维码扫描完成！（App ID：{app_id}）")
                if user_openid:
                    print(f"  扫码者 OpenID：{user_openid}")
                return {
                    "app_id": app_id,
                    "client_secret": client_secret,
                    "user_openid": user_openid,
                }

            if status == BindStatus.EXPIRED:
                if refresh_count >= _MAX_REFRESHES:
                    logger.warning("[QQBot onboard] QR code expired %d times — giving up", _MAX_REFRESHES)
                    return None
                print(f"\n  二维码已过期，正在刷新…（{refresh_count + 1}/{_MAX_REFRESHES}）")
                break  # 进入下一次 for 循环迭代，创建新任务

            time.sleep(ONBOARD_POLL_INTERVAL)
        else:
            # 已到截止时间但仍未完成
            logger.warning("[QQBot onboard] Poll timed out after %ds", timeout_seconds)
            return None

    return None
