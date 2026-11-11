"""Обслуживание БД: удаление устаревших записей, чтобы таблицы не росли бесконечно.

Удаление идёт небольшими пачками с паузами, а не одним огромным DELETE: он держал бы блокировки, раздувал WAL
и тормозил платежи и меню.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select

from app.db.models import AuditLog, Broadcast, Payment, SubEvent
from app.utils import utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)

# сколько хранить
AUDIT_DAYS = 180
FAILED_PAYMENT_DAYS = 90
EVENT_DAYS = 540
BROADCAST_DAYS = 180

BATCH = 2000
PAUSE = 0.2          # секунд между пачками — даём базе передохнуть
BUDGET = 240.0       # на весь прогон; остаток доберём в следующий раз


async def delete_in_batches(ctx: AppContext, model: Any, where: list, *, batch: int = BATCH, pause: float = PAUSE, deadline: float | None = None) -> int:
    """Удаляет строки пачками по первичному ключу. Возвращает, сколько удалено."""
    total = 0
    while deadline is None or time.monotonic() < deadline:
        async with ctx.session() as s:
            ids = list((await s.scalars(select(model.id).where(*where).limit(batch))).all())
            if not ids:
                break
            await s.execute(delete(model).where(model.id.in_(ids)))
            await s.commit()
        total += len(ids)
        if len(ids) < batch:
            break
        await asyncio.sleep(pause)
    return total


async def cleanup(ctx: AppContext) -> dict[str, int]:
    """Удаляет устаревшее. Успешные платежи и возвраты НЕ удаляются никогда (бухгалтерия)."""
    now = utcnow()
    deadline = time.monotonic() + BUDGET
    removed: dict[str, int] = {}
    removed["audit_log"] = await delete_in_batches(ctx, AuditLog, [AuditLog.created_at < now - timedelta(days=AUDIT_DAYS)], deadline=deadline)
    removed["payments_failed"] = await delete_in_batches(
        ctx, Payment, [Payment.status.in_(("pending", "canceled")), Payment.created_at < now - timedelta(days=FAILED_PAYMENT_DAYS)], deadline=deadline
    )
    removed["sub_events"] = await delete_in_batches(ctx, SubEvent, [SubEvent.created_at < now - timedelta(days=EVENT_DAYS)], deadline=deadline)
    removed["broadcasts"] = await delete_in_batches(
        ctx, Broadcast, [Broadcast.status.in_(("done", "cancelled")), Broadcast.created_at < now - timedelta(days=BROADCAST_DAYS)], deadline=deadline
    )
    # брони промокодов без оплаты и давние ожидающие рефералы
    removed["promo_reservations"] = await ctx.promos.release_stale_reservations()
    removed["referrals_expired"] = await ctx.refs.expire_stale()
    if any(removed.values()):
        log.info("cleanup: удалено %s", removed)
    return removed
