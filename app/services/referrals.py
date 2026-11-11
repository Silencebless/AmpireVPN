"""Реферальная система.

Награда (по умолчанию +7 дней LITE) начисляется пригласившему, когда приглашённый
не просто нажал «Подключиться», а реально воспользовался VPN: по данным Remnawave у него
есть первое подключение или израсходовано не меньше заданного объёма трафика.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import desc, func, or_, select, update

from app.bot import labels
from app.bot.ui import btn, kb
from app.core import subscription as core
from app.core.abuse import daily_cap_reached, hwids_overlap, old_enough
from app.core.backoff import referral_check_delay
from app.core.subscription import Tier
from app.db.models import Referral, User
from app.integrations.remnawave import RemnaError, usage_of
from app.utils import days_word, fmt_dt, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)


class ReferralService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ регистрация
    async def register(self, new_user_id: int, ref_code: str) -> bool:
        """Привязывает нового пользователя к пригласившему (только пока он «чистый»)."""
        if not self.ctx.cfg.get("referral_enabled"):
            return False
        code = ref_code.strip().upper()
        async with self.ctx.session() as s:
            referrer = await s.scalar(select(User).where(User.ref_code == code))
            if referrer is None or referrer.id == new_user_id or referrer.is_banned:
                return False
            user = await s.get(User, new_user_id, with_for_update=True)
            if user is None or user.referrer_id is not None or user.trial_used or user.remna_id:
                return False
            user.referrer_id = referrer.id
            s.add(Referral(referrer_id=referrer.id, referred_id=user.id, status="pending"))
            await s.commit()
        return True

    # ------------------------------------------------------------------ награда
    async def _is_used(self, referred: User) -> bool:
        if not referred.remna_id:
            return False
        try:
            data = await self.ctx.remna.get_user(referred.remna_id)
        except RemnaError as e:
            log.warning("referral check %s: %s", referred.id, e)
            return False
        usage = usage_of(data)
        min_bytes = int(self.ctx.cfg.get("referral_min_mb")) * 1024 * 1024
        return bool(usage["first_connected_at"]) or max(usage["used"], usage["lifetime"]) >= min_bytes

    async def _hwids(self, user: User | None) -> set[str]:
        if user is None or not user.remna_id:
            return set()
        try:
            return {str(d.get("hwid", "")) for d in await self.ctx.remna.list_devices(user.remna_id)}
        except RemnaError:
            return set()

    async def _rewards_last_24h(self, referrer_id: int) -> int:
        async with self.ctx.session() as s:
            return int(
                await s.scalar(
                    select(func.count()).select_from(Referral).where(
                        Referral.referrer_id == referrer_id,
                        Referral.status == "rewarded",
                        Referral.rewarded_at >= utcnow() - timedelta(hours=24),
                    )
                )
                or 0
            )

    async def try_reward(self, referral_id: int, *, force: bool = False) -> bool:
        async with self.ctx.session() as s:
            ref = await s.get(Referral, referral_id)
            if ref is None or ref.status != "pending":
                return False
            referred = await s.get(User, ref.referred_id)
            referrer_row = await s.get(User, ref.referrer_id)
        if referred is None:
            return False
        if not force:
            if not await self._is_used(referred):
                return False
            cfg0 = self.ctx.cfg
            # антифрод: возраст аккаунта, суточный лимит, одно и то же устройство у обоих
            if not old_enough(referred.created_at, utcnow(), int(cfg0.get("referral_min_age_h"))):
                return False  # подождём — останется в ожидании
            if daily_cap_reached(await self._rewards_last_24h(ref.referrer_id), int(cfg0.get("referral_daily_cap"))):
                return False  # лимит на сегодня исчерпан — вернёмся позже
            if cfg0.get("referral_check_hwid") and hwids_overlap(await self._hwids(referrer_row), await self._hwids(referred)):
                await self.reject(referral_id)
                await self.ctx.audit.log(None, "referral.rejected_same_device", referral_id)
                return False

        cfg = self.ctx.cfg
        days = int(cfg.get("referral_days"))
        tier = Tier(cfg.get("referral_tier"))
        cap = int(cfg.get("referral_max_rewards"))
        subs = self.ctx.subs
        referrer_id = ref.referrer_id
        new_state = None

        async with subs.lock(referrer_id):
            async with self.ctx.session() as s:
                r = await s.scalar(select(Referral).where(Referral.id == referral_id).with_for_update())
                if r is None or r.status != "pending":
                    return False
                referrer = await s.get(User, referrer_id, with_for_update=True)
                if referrer is None or referrer.is_banned:
                    r.status = "rejected"
                    await s.commit()
                    return False
                if cap > 0:
                    done = await s.scalar(
                        select(func.count()).select_from(Referral).where(
                            Referral.referrer_id == referrer_id, Referral.status == "rewarded"
                        )
                    )
                    if int(done or 0) >= cap:
                        r.status = "rejected"
                        await s.commit()
                        return False
                delta = timedelta(days=days)
                _, new_state = subs.apply_op(
                    s,
                    referrer,
                    lambda st, n: core.grant(st, tier, delta, n),
                    kind="referral",
                    tier=tier,
                    seconds=int(delta.total_seconds()),
                    meta={"referred": r.referred_id},
                )
                now = utcnow()
                r.status = "rewarded"
                r.rewarded_at = now
                r.reward_days = days
                await s.execute(
                    update(User).where(User.id == r.referred_id, User.connected_at.is_(None)).values(connected_at=now)
                )
                await s.commit()
            await subs.sync_inside_lock(referrer_id)

        await self.ctx.notify.send(
            referrer_id,
            self.ctx.texts.render("n_ref_reward", bonus=days_word(days), tier=tier.title, until=fmt_dt(new_state.total_end)),
            kb(btn(labels.SUBSCRIPTION, "menu:sub")),
        )
        return True

    async def reject(self, referral_id: int) -> None:
        async with self.ctx.session() as s:
            await s.execute(
                update(Referral).where(Referral.id == referral_id, Referral.status == "pending").values(status="rejected")
            )
            await s.commit()

    async def on_user_connected(self, user_id: int) -> None:
        """Вызывается по вебхуку Remnawave (user.first_connected) — награждаем сразу."""
        async with self.ctx.session() as s:
            rid = await s.scalar(
                select(Referral.id).where(Referral.referred_id == user_id, Referral.status == "pending")
            )
        if rid:
            await self.try_reward(rid)

    async def _schedule_next(self, referral_id: int, created_at) -> None:  # noqa: ANN001
        """Следующий опрос панели по этому рефералу: чем старше приглашённый, тем реже (см. referral_check_delay)."""
        now = utcnow()
        async with self.ctx.session() as s:
            await s.execute(
                update(Referral)
                .where(Referral.id == referral_id, Referral.status == "pending")
                .values(next_check_at=now + referral_check_delay(now - created_at))
            )
            await s.commit()

    async def check_pending(self, batch: int = 100, concurrency: int = 5, budget: float = 120.0) -> int:
        """Воркер: страховочная проверка ожидающих рефералов (основной путь — вебхук user.first_connected).

        Выбираются только те, у кого подошло время next_check_at; после проверки время сдвигается по возрасту
        (15 мин → 1 ч → 6 ч → 12 ч → 24 ч). Нагрузка на панель определяется числом «созревших», а не всей базой
        ожидающих — десятки тысяч давних приглашённых не создают постоянного потока запросов.
        """
        cutoff = utcnow() - timedelta(days=60)
        t0 = time.monotonic()
        sem = asyncio.Semaphore(concurrency)
        last_id = 0
        rewarded = 0

        async def one(rid: int, created_at) -> bool:  # noqa: ANN001
            async with sem:
                ok = False
                try:
                    ok = await self.try_reward(rid)
                except Exception:  # noqa: BLE001
                    log.exception("referral %s", rid)
                try:
                    await self._schedule_next(rid, created_at)
                except Exception:  # noqa: BLE001
                    log.exception("referral %s: не удалось назначить следующую проверку", rid)
                return ok

        while time.monotonic() - t0 < budget:
            now = utcnow()
            async with self.ctx.session() as s:
                rows = (
                    await s.execute(
                        select(Referral.id, Referral.created_at)
                        .join(User, User.id == Referral.referred_id)
                        .where(
                            Referral.status == "pending",
                            Referral.created_at > cutoff,
                            User.remna_id.is_not(None),
                            or_(Referral.next_check_at.is_(None), Referral.next_check_at <= now),
                            Referral.id > last_id,
                        )
                        .order_by(Referral.id)
                        .limit(batch)
                    )
                ).all()
            if not rows:
                break
            last_id = rows[-1][0]
            rewarded += sum(await asyncio.gather(*(one(rid, created) for rid, created in rows)))
            if len(rows) < batch:
                break
        return rewarded

    async def expire_stale(self, days: int = 60, batch: int = 2000) -> int:
        """Ожидающие рефералы старше ``days`` дней больше не опрашиваются — закрываем их (rejected), пачками."""
        cutoff = utcnow() - timedelta(days=days)
        total = 0
        while True:
            async with self.ctx.session() as s:
                ids = list(
                    (await s.scalars(select(Referral.id).where(Referral.status == "pending", Referral.created_at < cutoff).limit(batch))).all()
                )
                if not ids:
                    return total
                await s.execute(update(Referral).where(Referral.id.in_(ids)).values(status="rejected"))
                await s.commit()
            total += len(ids)
            if len(ids) < batch:
                return total

    # ------------------------------------------------------------------ статистика
    async def stats(self, user_id: int) -> dict:
        async with self.ctx.session() as s:
            invited = await s.scalar(select(func.count()).select_from(Referral).where(Referral.referrer_id == user_id))
            rewarded = await s.scalar(
                select(func.count()).select_from(Referral).where(
                    Referral.referrer_id == user_id, Referral.status == "rewarded"
                )
            )
            days = await s.scalar(
                select(func.coalesce(func.sum(Referral.reward_days), 0)).where(
                    Referral.referrer_id == user_id, Referral.status == "rewarded"
                )
            )
        return {"invited": int(invited or 0), "connected": int(rewarded or 0), "days": int(days or 0)}

    async def overview(self) -> dict:
        async with self.ctx.session() as s:
            rows = (await s.execute(select(Referral.status, func.count()).group_by(Referral.status))).all()
            days = await s.scalar(
                select(func.coalesce(func.sum(Referral.reward_days), 0)).where(Referral.status == "rewarded")
            )
        by = {st: int(n) for st, n in rows}
        return {
            "total": sum(by.values()),
            "pending": by.get("pending", 0),
            "rewarded": by.get("rewarded", 0),
            "rejected": by.get("rejected", 0),
            "days": int(days or 0),
        }

    async def top_inviters(self, limit: int = 10) -> list[tuple[User, int, int]]:
        async with self.ctx.session() as s:
            rewarded = func.count().filter(Referral.status == "rewarded")
            res = await s.execute(
                select(User, func.count(Referral.id), rewarded)
                .join(Referral, Referral.referrer_id == User.id)
                .group_by(User.id)
                .order_by(desc(func.count(Referral.id)))
                .limit(limit)
            )
            return [(u, int(n), int(r)) for u, n, r in res.all()]

    async def children(self, user_id: int, limit: int = 20) -> list[tuple[Referral, User | None]]:
        async with self.ctx.session() as s:
            res = await s.execute(
                select(Referral, User)
                .outerjoin(User, User.id == Referral.referred_id)
                .where(Referral.referrer_id == user_id)
                .order_by(desc(Referral.id))
                .limit(limit)
            )
            return [(r, u) for r, u in res.all()]

    async def pending_list(self, limit: int = 15) -> list[tuple[Referral, User | None]]:
        async with self.ctx.session() as s:
            res = await s.execute(
                select(Referral, User)
                .outerjoin(User, User.id == Referral.referred_id)
                .where(Referral.status == "pending")
                .order_by(desc(Referral.id))
                .limit(limit)
            )
            return [(r, u) for r, u in res.all()]
