#!/usr/bin/env python3
"""Расшифровка бэкапа БД, присланного ботом.

    pip install cryptography
    python scripts/decrypt_backup.py ampirevpn-backup-2026-11-05_0400.dump.enc restored.dump
    (пароль спросит интерактивно; либо переменная окружения BACKUP_PASSWORD)

Затем восстановление в PostgreSQL:
    docker compose exec -T postgres pg_restore -U ampire -d ampire --clean --if-exists < restored.dump
"""
import getpass
import importlib.util
import os
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("backup_crypto", Path(__file__).resolve().parent.parent / "app" / "backup_crypto.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

if len(sys.argv) != 3:
    sys.exit(__doc__)
src, dst = sys.argv[1], sys.argv[2]
password = os.getenv("BACKUP_PASSWORD") or getpass.getpass("Пароль бэкапа: ")
try:
    Path(dst).write_bytes(mod.decrypt_bytes(Path(src).read_bytes(), password))
except ValueError as e:
    sys.exit(f"Ошибка: {e}")
print(f"Готово: {dst}")
