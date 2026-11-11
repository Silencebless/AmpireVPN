"""Схема БД: Alembic-миграции при старте (безопасно при одновременном запуске нескольких процессов).

Порядок:
1) берём advisory-lock в PostgreSQL (остальные реплики ждут);
2) если БД создана старой версией через create_all (есть таблица users, но нет alembic_version) —
   «мост»: добавляем недостающие колонки/индексы и ставим метку head;
3) alembic upgrade head;
4) если миграции не отработали: в production запуск ОСТАНАВЛИВАЕТСЯ (бот со старой схемой падал бы на каждом
   запросе), в development — запасной вариант create_all.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import models  # noqa: F401  (регистрирует таблицы в metadata)
from app.db.base import Base, init_db
from app.startup_checks import StartupError

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
LOCK_KEY = 7272701

# колонки/индексы, добавленные после первой версии (идемпотентно)
LEGACY_BRIDGE_SQL = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS autopay boolean NOT NULL DEFAULT false",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS autopay_failures integer NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS autopay_last_at timestamptz",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS autopay_notified_for timestamptz",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS sync_attempts integer NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS sync_next_at timestamptz",
    "CREATE INDEX IF NOT EXISTS ix_users_active_exp ON users (expires_at) WHERE tier IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_users_sync_due ON users (sync_next_at) WHERE sync_pending",
    "CREATE INDEX IF NOT EXISTS ix_users_autopay_exp ON users (expires_at) WHERE autopay",
    "CREATE INDEX IF NOT EXISTS ix_payments_provider_status ON payments (provider, status)",
    "CREATE INDEX IF NOT EXISTS ix_payments_paid_at ON payments (paid_at) WHERE status = 'succeeded'",
    "CREATE INDEX IF NOT EXISTS ix_referrals_status_created ON referrals (status, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_sub_events_kind_created ON sub_events (kind, created_at)",
]


def _config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("prepend_sys_path", str(ROOT))
    return cfg


def _upgrade() -> None:
    command.upgrade(_config(), "head")


# ревизия, до которой доводит LEGACY_BRIDGE_SQL (дальше — обычные миграции)
LEGACY_BASE_REVISION = "0001"


def _stamp(revision: str = LEGACY_BASE_REVISION) -> None:
    command.stamp(_config(), revision)


async def _table_exists(conn, name: str) -> bool:
    return bool(await conn.scalar(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": f"public.{name}"}))


async def _bridge_legacy(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        legacy = await _table_exists(conn, "users") and not await _table_exists(conn, "alembic_version")
    if not legacy:
        return False
    log.warning("Найдена БД старой версии (без alembic_version) — привожу схему к актуальной")
    async with engine.begin() as conn:
        for stmt in LEGACY_BRIDGE_SQL:
            await conn.execute(text(stmt))
        await conn.run_sync(Base.metadata.create_all)  # недостающие таблицы (например, payment_methods)
    await asyncio.to_thread(_stamp)  # метка 0001 — миграции после неё применятся следующим шагом
    return True


async def ensure_schema(engine: AsyncEngine, *, strict: bool = True) -> None:
    """Приводит схему БД к актуальной. ``strict=True`` (production): при ошибке миграций бросает StartupError."""
    failure: Exception | None = None
    async with engine.connect() as lock_conn:
        await lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": LOCK_KEY})
        await lock_conn.commit()  # advisory-lock сессионный: переживает commit
        try:
            try:
                await _bridge_legacy(engine)
                await asyncio.to_thread(_upgrade)  # после «моста» доводит схему от 0001 до head
                log.info("Схема БД актуальна")
            except Exception as e:  # noqa: BLE001
                failure = e
                log.exception("Миграции не применились")
                if not strict:
                    log.warning("development: запасной вариант — create_all")
                    await init_db(engine)
        finally:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
            await lock_conn.commit()
    if failure is not None and strict:
        raise StartupError(f"Миграции БД не применились ({type(failure).__name__}: {failure}). Запуск остановлен.") from failure
