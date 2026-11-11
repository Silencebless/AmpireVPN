from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import delete, desc, func, or_, select, update

from app.core import subscription as core
from app.core.pricing import PROMO_DAYS, PROMO_FIXED, PROMO_PERCENT
from app.core.subscription import SubState, Tier
from app.db.models import Promo, PromoUse, User
from app.utils import money, rand_code, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)

KIND_TITLES = {PROMO_PERCENT: "Скидка, %", PROMO_FIXED: "Скидка, ₽", PROMO_DAYS: "Бонусные дни"}

# Бронь промокода (скидка «на следующую оплату») живёт неделю: если человек не заплатил, место в лимите max_uses освобождается.
RESERVATION_TTL = timedelta(days=7)


class PromoError(Exception):
    """code: not_found | inactive | expired | used | limit"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(slots=True)
class RedeemResult:
    promo: Promo
    new_state: SubState | None = None


def describe_value(promo: Promo) -> str:
    v = Decimal(promo.value)
    if promo.kind == PROMO_PERCENT:
        return f"−{v.normalize():f}%"
    if promo.kind == PROMO_FIXED:
        return f"−{money(v)}"
    return f"+{int(v)} дн. {str(promo.days_tier).upper()}"


class PromoService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ админ
    async def create(
        self,
        *,
        kind: str,
        value: Decimal,
        code: str | None,
        days_tier: str | None = None,
        tiers: list[str] | None = None,
        max_uses: int | None = None,
        expires_at: datetime | None = None,
        note: str | None = None,
        created_by: int | None = None,
    ) -> Promo:
        code = (code or rand_code(8)).strip().upper()
        async with self.ctx.session() as s:
            if await s.scalar(select(Promo.id).where(Promo.code == code)):
                raise ValueError("Промокод с таким названием уже есть")
            promo = Promo(
                code=code,
                kind=kind,
                value=value,
                days_tier=days_tier,
                tiers=tiers,
                max_uses=max_uses,
                expires_at=expires_at,
                note=note,
                created_by=created_by,
            )
            s.add(promo)
            await s.commit()
            return promo

    async def get(self, promo_id: int) -> Promo | None:
        async with self.ctx.session() as s:
            return await s.get(Promo, promo_id)

    async def page(self, page: int, size: int = 8) -> tuple[list[tuple[Promo, int]], int]:
        """Промокоды вместе с числом активаций."""
        async with self.ctx.session() as s:
            total = int(await s.scalar(select(func.count()).select_from(Promo)) or 0)
            uses = (
                select(PromoUse.promo_id, func.count().label("n")).group_by(PromoUse.promo_id).subquery()
            )
            res = await s.execute(
                select(Promo, func.coalesce(uses.c.n, 0))
                .outerjoin(uses, uses.c.promo_id == Promo.id)
                .order_by(desc(Promo.id))
                .limit(size)
                .offset(page * size)
            )
            return [(p, int(n)) for p, n in res.all()], total

    async def uses_count(self, promo_id: int) -> int:
        async with self.ctx.session() as s:
            return int(await s.scalar(select(func.count()).select_from(PromoUse).where(PromoUse.promo_id == promo_id)) or 0)

    async def activations(self, promo_id: int, limit: int = 30) -> list[tuple[PromoUse, User | None]]:
        async with self.ctx.session() as s:
            res = await s.execute(
                select(PromoUse, User)
                .outerjoin(User, User.id == PromoUse.user_id)
                .where(PromoUse.promo_id == promo_id)
                .order_by(desc(PromoUse.id))
                .limit(limit)
            )
            return [(u, usr) for u, usr in res.all()]

    async def update(self, promo_id: int, **fields) -> None:
        async with self.ctx.session() as s:
            promo = await s.get(Promo, promo_id)
            if promo:
                for k, v in fields.items():
                    setattr(promo, k, v)
                await s.commit()

    async def delete(self, promo_id: int) -> None:
        async with self.ctx.session() as s:
            await s.execute(delete(Promo).where(Promo.id == promo_id))
            await s.commit()

    # ------------------------------------------------------------------ пользователь
    async def redeem(self, user_id: int, raw_code: str) -> RedeemResult:
        code = raw_code.strip().upper()
        if not code or len(code) > 32:
            raise PromoError("not_found")
        subs = self.ctx.subs
        async with subs.lock(user_id):
            async with self.ctx.session() as s:
                promo = await s.scalar(select(Promo).where(Promo.code == code).with_for_update())
                if promo is None:
                    raise PromoError("not_found")
                now = utcnow()
                if not promo.is_active:
                    raise PromoError("inactive")
                if promo.expires_at and promo.expires_at <= now:
                    raise PromoError("expired")
                existing = await s.scalar(
                    select(PromoUse).where(PromoUse.promo_id == promo.id, PromoUse.user_id == user_id)
                )
                if existing is not None:
                    if existing.status == "reserved" and existing.created_at < now - RESERVATION_TTL:
                        await s.delete(existing)  # старая бронь без оплаты — можно взять код заново
                        await s.flush()
                    else:
                        raise PromoError("used")
                if promo.max_uses is not None:
                    # в лимит входят использованные/выданные и «живые» брони; просроченные брони места не занимают
                    used = await s.scalar(
                        select(func.count())
                        .select_from(PromoUse)
                        .where(
                            PromoUse.promo_id == promo.id,
                            or_(PromoUse.status != "reserved", PromoUse.created_at > now - RESERVATION_TTL),
                        )
                    )
                    if int(used or 0) >= promo.max_uses:
                        raise PromoError("limit")
                user = await s.get(User, user_id, with_for_update=True)
                if user is None:
                    raise LookupError(user_id)

                if promo.kind == PROMO_DAYS:
                    tier = Tier(promo.days_tier or "lite")
                    days = int(promo.value)
                    delta = timedelta(days=days)
                    _, new = subs.apply_op(
                        s,
                        user,
                        lambda st, n: core.grant(st, tier, delta, n),
                        kind="promo",
                        tier=tier,
                        seconds=int(delta.total_seconds()),
                        meta={"promo": promo.code},
                    )
                    s.add(PromoUse(promo_id=promo.id, user_id=user_id, status="granted"))
                    await s.commit()
                    result = RedeemResult(promo, new)
                else:
                    user.pending_promo_id = promo.id
                    s.add(PromoUse(promo_id=promo.id, user_id=user_id, status="reserved"))
                    await s.commit()
                    result = RedeemResult(promo, None)
            if result.new_state is not None:
                await subs.sync_inside_lock(user_id)
        return result

    # ------------------------------------------------------------------ обслуживание
    async def release_stale_reservations(self, batch: int = 1000) -> int:
        """Освобождает просроченные брони: удаляет PromoUse(reserved) и снимает pending_promo_id у владельцев (пачками)."""
        cutoff = utcnow() - RESERVATION_TTL
        total = 0
        while True:
            async with self.ctx.session() as s:
                rows = (
                    await s.execute(
                        select(PromoUse.id, PromoUse.user_id, PromoUse.promo_id)
                        .where(PromoUse.status == "reserved", PromoUse.created_at < cutoff)
                        .limit(batch)
                    )
                ).all()
                if not rows:
                    return total
                for _id, uid, pid in rows:
                    await s.execute(update(User).where(User.id == uid, User.pending_promo_id == pid).values(pending_promo_id=None))
                await s.execute(delete(PromoUse).where(PromoUse.id.in_([r[0] for r in rows])))
                await s.commit()
            total += len(rows)
            if len(rows) < batch:
                return total
