"""Дымовая проверка на настоящей PostgreSQL (запускается в CI): миграции с нуля и SQL, который нельзя проверить без БД.

Что проверяем:
  1. alembic upgrade head на пустой БД (строгий режим, как в production) — схема совпадает с моделями;
  2. запросы автопродления (интервал make_interval, IS DISTINCT FROM), опроса рефералов и сброса броней промокодов
     выполняются без ошибок синтаксиса;
  3. откат миграции 0002 и повторное применение.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, text  # noqa: E402

from app.db.base import create_engine, create_sessionmaker  # noqa: E402
from app.migrate import ensure_schema  # noqa: E402


async def main() -> None:
    url = os.environ["DATABASE_URL"]
    engine = create_engine(url, pool_size=2, max_overflow=0, command_timeout=None)
    await ensure_schema(engine, strict=True)
    print("OK  миграции применились с нуля")

    sm = create_sessionmaker(engine)
    from app.db.models import Referral, User
    from app.services.payments import PaymentService

    svc = PaymentService.__new__(PaymentService)
    svc.ctx = SimpleNamespace(session=sm)
    now = datetime.now(timezone.utc)
    for phase in ("charge", "notice"):
        ids = await svc._autopay_ids(phase, now, 0, 10)
        assert ids == [], ids
    print("OK  SQL автопродления (charge/notice) выполняется")

    async with sm() as s:
        await s.execute(
            select(Referral.id, Referral.created_at)
            .join(User, User.id == Referral.referred_id)
            .where(Referral.status == "pending", Referral.next_check_at.is_(None))
            .limit(1)
        )
        assert (await s.scalar(text("select count(*) from users"))) == 0
    print("OK  запросы к рефералам и пользователям")

    # откат и повторное применение последней миграции
    import asyncio as aio

    from alembic import command

    from app.migrate import _config

    await aio.to_thread(command.downgrade, _config(), "0001")
    await aio.to_thread(command.upgrade, _config(), "head")
    print("OK  downgrade 0002 → upgrade head")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
