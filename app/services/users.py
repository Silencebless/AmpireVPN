from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from aiogram.types import User as TgUser
from sqlalchemy import and_, delete, desc, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.db.models import Payment, User
from app.integrations.remnawave import RemnaError, RemnaNotFound
from app.utils import rand_code, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)

# сегменты для списков в админке и рассылок
SEGMENTS: dict[str, str] = {
    "all": "Все",
    "active": "С активной подпиской",
    "premium": "PREMIUM",
    "lite": "LITE",
    "expired": "Подписка закончилась",
    "never": "Не подключались",
    "free": "Только бесплатные дни",
    "paid": "Платили когда-либо",
    "expiring": "Истекает ≤ 3 дней",
    "trial": "Брали пробный период",
}
ADMIN_FILTERS: dict[str, str] = {
    **SEGMENTS,
    "banned": "Заблокированные",
    "blocked": "Заблокировали бота",
    "unsynced": "Не синхронизированы с панелью",
}


def user_filter(name: str, now: datetime) -> list[Any]:
    paid = exists().where(Payment.user_id == User.id, Payment.status == "succeeded")
    active = and_(User.tier.is_not(None), User.expires_at > now)
    match name:
        case "active":
            return [active]
        case "premium":
            return [User.tier == "premium", User.expires_at > now]
        case "lite":
            return [User.tier == "lite", User.expires_at > now]
        case "expired":
            return [or_(User.tier.is_(None), User.expires_at <= now), User.remna_id.is_not(None)]
        case "never":
            return [User.remna_id.is_(None)]
        case "free":
            return [active, ~paid]
        case "paid":
            return [paid]
        case "expiring":
            return [active, User.queued_lite_s == 0, User.expires_at <= now + timedelta(days=3)]
        case "trial":
            return [User.trial_used.is_(True)]
        case "banned":
            return [User.is_banned.is_(True)]
        case "blocked":
            return [User.bot_blocked.is_(True)]
        case "unsynced":
            return [User.sync_pending.is_(True)]
        case _:
            return []


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class UserService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    async def get(self, user_id: int) -> User | None:
        async with self.ctx.session() as s:
            return await s.get(User, user_id)

    async def upsert_from_telegram(self, tg: TgUser, *, allow_create: bool = True) -> tuple[User | None, bool]:
        """Возвращает (пользователь, создан_сейчас)."""
        now = utcnow()
        async with self.ctx.session() as s:
            user = await s.get(User, tg.id)
            if user is None:
                if not allow_create:
                    return None, False
                user = User(
                    id=tg.id,
                    username=tg.username,
                    first_name=tg.first_name or "",
                    last_name=tg.last_name,
                    ref_code=await self._unique_ref_code(s),
                    created_at=now,
                    last_seen_at=now,
                )
                s.add(user)
                try:
                    await s.commit()
                    return user, True
                except IntegrityError:
                    await s.rollback()
                    return await s.get(User, tg.id), False

            changed = False
            if user.username != tg.username:
                user.username, changed = tg.username, True
            if user.first_name != (tg.first_name or ""):
                user.first_name, changed = tg.first_name or "", True
            if user.last_name != tg.last_name:
                user.last_name, changed = tg.last_name, True
            if user.bot_blocked:
                user.bot_blocked, changed = False, True
            if now - user.last_seen_at > timedelta(minutes=5):
                user.last_seen_at, changed = now, True
            if changed:
                await s.commit()
            return user, False

    @staticmethod
    async def _unique_ref_code(s) -> str:
        for _ in range(8):
            code = rand_code(8)
            if await s.scalar(select(User.id).where(User.ref_code == code)) is None:
                return code
        return rand_code(12)

    # ------------------------------------------------------------------ поиск и списки
    async def find(self, query: str, limit: int = 20) -> list[User]:
        q = query.strip().lstrip("@")
        if not q:
            return []
        conds: list[Any] = []
        if q.isdigit():
            n = int(q)
            conds += [User.id == n, User.remna_id == n]
        conds += [
            User.short_uuid == q,
            User.remna_username == q,
            User.ref_code == q.upper(),
            User.username.ilike(_like(q), escape="\\"),
            User.first_name.ilike(_like(q), escape="\\"),
            User.last_name.ilike(_like(q), escape="\\"),
            User.email.ilike(_like(q), escape="\\"),
        ]
        async with self.ctx.session() as s:
            res = await s.execute(select(User).where(or_(*conds)).order_by(desc(User.created_at)).limit(limit))
            return list(res.scalars().all())

    async def page(self, flt: str, page: int, size: int = 8) -> tuple[list[User], int]:
        now = utcnow()
        conds = user_filter(flt, now)
        order = User.expires_at.asc() if flt == "expiring" else desc(User.created_at)
        async with self.ctx.session() as s:
            total = await s.scalar(select(func.count()).select_from(User).where(*conds)) or 0
            res = await s.execute(select(User).where(*conds).order_by(order).limit(size).offset(page * size))
            return list(res.scalars().all()), int(total)

    async def count(self, flt: str, *, fresh: bool = False) -> int:
        """Число пользователей в сегменте (кэш 60 с: на больших базах COUNT дорогой)."""

        async def compute() -> int:
            async with self.ctx.session() as s:
                return int(await s.scalar(select(func.count()).select_from(User).where(*user_filter(flt, utcnow()))) or 0)

        return int(await self.ctx.cache.remember(f"ucount:{flt}", 60, compute, force=fresh))

    # ------------------------------------------------------------------ изменения
    async def set_email(self, user_id: int, email: str | None, *, consented: bool = False) -> None:
        """Сохраняет email для чеков. ``consented=True`` — пользователь сам ввёл адрес и согласился на его обработку."""
        values: dict[str, Any] = {"email": email}
        if email and consented:
            values["email_consent_at"] = utcnow()
        async with self.ctx.session() as s:
            await s.execute(update(User).where(User.id == user_id).values(**values))
            await s.commit()

    async def set_note(self, user_id: int, note: str | None) -> None:
        async with self.ctx.session() as s:
            await s.execute(update(User).where(User.id == user_id).values(notes=note))
            await s.commit()

    async def set_banned(self, user_id: int, banned: bool, reason: str | None = None) -> None:
        async with self.ctx.session() as s:
            await s.execute(
                update(User).where(User.id == user_id).values(is_banned=banned, ban_reason=reason if banned else None, sync_pending=True, sync_next_at=None)
            )
            await s.commit()
        await self.ctx.subs.sync_now(user_id)

    async def reset_trial(self, user_id: int) -> None:
        async with self.ctx.session() as s:
            await s.execute(update(User).where(User.id == user_id).values(trial_used=False, trial_at=None))
            await s.commit()

    async def delete(self, user_id: int) -> None:
        """Полностью удаляет пользователя: в панели Remnawave и в БД (платежи остаются для учёта)."""
        async with self.ctx.subs.lock(user_id):
            user = await self.get(user_id)
            if user is None:
                return
            if user.remna_id:
                try:
                    await self.ctx.remna.delete_user(user.remna_id)
                except RemnaNotFound:
                    pass
                except RemnaError:
                    log.exception("не удалось удалить %s из панели", user_id)
                    raise
            async with self.ctx.session() as s:
                await s.execute(delete(User).where(User.id == user_id))
                await s.commit()
