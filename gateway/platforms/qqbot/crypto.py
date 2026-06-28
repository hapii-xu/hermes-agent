"""QQBot 扫码配置凭据解密所用的 AES-256-GCM 工具函数。"""

from __future__ import annotations

import base64
import os


def generate_bind_key() -> str:
    """生成一个 256 位随机 AES 密钥并以 base64 形式返回。

    该密钥传递给 ``create_bind_task``，以便服务器在返回 bot 的
    *client_secret* 之前对其进行加密。只有本 CLI 持有此密钥，
    确保密钥在传输过程中不以明文形式出现。
    """
    return base64.b64encode(os.urandom(32)).decode()


def decrypt_secret(encrypted_base64: str, key_base64: str) -> str:
    """解密 base64 编码的 AES-256-GCM 密文。

    密文结构（base64 解码后）::

        IV（12 字节）‖ 密文（N 字节）‖ 认证标签（16 字节）

    Args:
        encrypted_base64: 来自 ``poll_bind_result`` 的 ``bot_encrypt_secret`` 值。
        key_base64: 由 :func:`generate_bind_key` 生成的 base64 AES 密钥。

    Returns:
        解密后的 *client_secret* UTF-8 字符串。
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = base64.b64decode(key_base64)
    raw = base64.b64decode(encrypted_base64)

    iv = raw[:12]
    ciphertext_with_tag = raw[12:]  # AESGCM 要求密文与认证标签拼接在一起

    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(iv, ciphertext_with_tag, None)
    return plaintext.decode("utf-8")
