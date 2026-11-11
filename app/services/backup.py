from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

from aiogram.types import BufferedInputFile
from sqlalchemy.engine import make_url

from app.backup_crypto import encrypt_bytes
from app.utils import fmt_dt, to_local, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)

TG_LIMIT = 49 * 1024 * 1024  # лимит загрузки файла ботом ~50 МБ

# Метки свежести копий (Redis). PITR-контейнер и тест восстановления пишут их из shell (redis-cli SET … <unix-time>).
KEY_DUMP_OK = "ampire:backup:dump_ok_ts"        # последний успешно отправленный зашифрованный дамп
KEY_PITR_OK = "ampire:backup:pitr_ok_ts"        # последний успешный бэкап pgBackRest (full/diff/incr)
KEY_RESTORE_OK = "ampire:backup:restore_ok_ts"  # последний успешный тест восстановления


class BackupError(Exception):
    pass


class BackupService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def dump(self) -> tuple[bytes, str]:
        """Делает pg_dump (формат custom, уже сжат). Возвращает (байты, имя файла)."""
        url = make_url(self.ctx.settings.database_url)
        env = {**os.environ, "PGPASSWORD": url.password or ""}
        cmd = [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "-h", url.host or "localhost",
            "-p", str(url.port or 5432),
            "-U", url.username or "postgres",
            "-d", url.database or "postgres",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=300)
        except FileNotFoundError as e:
            raise BackupError("pg_dump не найден в контейнере") from e
        except asyncio.TimeoutError as e:
            raise BackupError("pg_dump завис (таймаут 5 минут)") from e
        if proc.returncode != 0:
            raise BackupError(err.decode(errors="replace")[:400] or f"pg_dump завершился с кодом {proc.returncode}")
        name = f"ampirevpn-backup-{to_local(utcnow()).strftime('%Y-%m-%d_%H%M')}.dump"
        return out, name

    async def send(self, chat_ids: set[int] | list[int]) -> str:
        """Отправляет ЗАШИФРОВАННЫЙ дамп в Telegram. Возвращает: sent | disabled | no_password | too_big | failed.

        Без BACKUP_PASSWORD дамп не отправляется вообще: внутри ссылки доступа к VPN, email и платежи.
        """
        if not self.ctx.settings.backup_telegram:
            return "disabled"
        password = self.ctx.settings.backup_password
        if not password:
            await self.ctx.notify.alert_once(
                "backup_no_password",
                "🔐 <b>Бэкап в Telegram не отправлен</b>: не задан BACKUP_PASSWORD. Открытый дамп содержит ссылки доступа к VPN, "
                "email и платежи, поэтому бот отправляет его только зашифрованным. Задайте пароль в .env и перезапустите бота.",
                ttl=6 * 3600,
            )
            return "no_password"
        try:
            data, name = await self.dump()
        except BackupError as e:
            log.error("backup failed: %s", e)
            for cid in chat_ids:
                await self.ctx.notify.send(cid, f"⚠️ Бэкап БД не удался: {e}")
            return "failed"
        data = await asyncio.to_thread(encrypt_bytes, data, password)  # PBKDF2 тяжёлый — не блокируем цикл событий
        name += ".enc"
        if len(data) > TG_LIMIT:
            for cid in chat_ids:
                await self.ctx.notify.send(
                    cid,
                    f"⚠️ Дамп БД ({len(data) // 1024 // 1024} МБ) не помещается в Telegram — копия в чат больше не делается. "
                    "Полагайтесь на непрерывный архив WAL (PITR) и копию вне сервера: docs/BACKUP.md.",
                )
            return "too_big"
        caption = f"💾 Бэкап БД · {fmt_dt(utcnow())} · {len(data) // 1024} КБ · 🔐 зашифрован"
        delivered = 0
        for cid in chat_ids:
            try:
                await self.ctx.bot.send_document(cid, BufferedInputFile(data, name), caption=caption)
                delivered += 1
            except Exception:  # noqa: BLE001
                log.exception("не удалось отправить бэкап в %s", cid)
        if not delivered:
            return "failed"
        try:
            await self.ctx.redis.set(KEY_DUMP_OK, int(time.time()))
        except Exception:  # noqa: BLE001
            pass
        return "sent"

    def targets(self) -> set[int]:
        if self.ctx.settings.backup_chat_id:
            return {self.ctx.settings.backup_chat_id}
        return set(self.ctx.settings.owners)

    async def freshness(self) -> dict[str, int | None]:
        """Возраст (в секундах) последних успешных копий; None — отметки нет."""
        out: dict[str, int | None] = {}
        now = time.time()
        for name, key in (("dump", KEY_DUMP_OK), ("pitr", KEY_PITR_OK), ("restore_test", KEY_RESTORE_OK)):
            try:
                raw = await self.ctx.redis.get(key)
            except Exception:  # noqa: BLE001
                raw = None
            try:
                out[name] = max(0, int(now - int(raw))) if raw is not None else None
            except (TypeError, ValueError):
                out[name] = None
        return out
