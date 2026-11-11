"""Шифрование бэкапов паролем: PBKDF2-HMAC-SHA256 (600k итераций) + Fernet (AES-128-CBC + HMAC).

Формат файла: MAGIC (7 байт) + соль (16 байт) + токен Fernet. Файлы со старым заголовком CLSTBK1 тоже читаются.
Расшифровка: python scripts/decrypt_backup.py backup.dump.enc backup.dump
"""
from __future__ import annotations

import base64
import os

MAGIC = b"AMPRBK1"
LEGACY_MAGICS = (b"CLSTBK1",)
ITERATIONS = 600_000


def _fernet(password: str, salt: bytes):
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(password.encode())))


def encrypt_bytes(data: bytes, password: str) -> bytes:
    salt = os.urandom(16)
    return MAGIC + salt + _fernet(password, salt).encrypt(data)


def decrypt_bytes(blob: bytes, password: str) -> bytes:
    magic = next((m for m in (MAGIC, *LEGACY_MAGICS) if blob.startswith(m)), None)
    if magic is None:
        raise ValueError("Это не зашифрованный бэкап AmpireVPN (неверный заголовок файла)")
    salt = blob[len(magic) : len(magic) + 16]
    token = blob[len(magic) + 16 :]
    try:
        return _fernet(password, salt).decrypt(token)
    except Exception as e:  # noqa: BLE001
        raise ValueError("Неверный пароль или файл повреждён") from e
