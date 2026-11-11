from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import Date, cast, distinct, exists, func, literal_column, or_, select

from app.db.models import Payment, Promo, PromoUse, SubEvent, User
from app.services.users import user_filter
from app.utils import fmt_dt, to_local, tz_name, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)


def _local_day(column):
    """Локальная календарная дата. Часовой пояс вшит литералом (не bind-параметром):
    иначе с asyncpg выражения в SELECT и GROUP BY получают разные $N и PostgreSQL ругается."""
    tz = tz_name()
    if not re.fullmatch(r"[A-Za-z0-9_+\-/]+", tz):
        tz = "UTC"
    return cast(func.timezone(literal_column(f"'{tz}'"), column), Date)


class StatsService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ сводка
    async def overview(self, *, fresh: bool = False) -> dict[str, Any]:
        """Сводка для админки. Кэш 45 с: на миллионах строк десятки COUNT/SUM — дорогое удовольствие."""
        data = await self.ctx.cache.remember("stats:overview", 45, self._overview_uncached, force=fresh)
        return data

    async def _overview_uncached(self) -> dict[str, Any]:
        now = utcnow()
        local = to_local(now)
        today = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(now.tzinfo)
        month = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(now.tzinfo)
        out: dict[str, Any] = {"now": now}

        async with self.ctx.session() as s:
            async def users(*conds: Any) -> int:
                return int(await s.scalar(select(func.count()).select_from(User).where(*conds)) or 0)

            out["users_total"] = await users()
            out["new_24h"] = await users(User.created_at >= now - timedelta(days=1))
            out["new_7d"] = await users(User.created_at >= now - timedelta(days=7))
            out["new_30d"] = await users(User.created_at >= now - timedelta(days=30))
            out["blocked_bot"] = await users(User.bot_blocked.is_(True))
            out["banned"] = await users(User.is_banned.is_(True))
            out["premium"] = await users(*user_filter("premium", now))
            out["lite"] = await users(*user_filter("lite", now))
            out["free_active"] = await users(*user_filter("free", now))
            out["exp_24h"] = await users(
                User.tier.is_not(None), User.queued_lite_s == 0, User.expires_at > now,
                User.expires_at <= now + timedelta(days=1),
            )
            out["exp_72h"] = await users(
                User.tier.is_not(None), User.queued_lite_s == 0, User.expires_at > now,
                User.expires_at <= now + timedelta(days=3),
            )
            out["lost_7d"] = await users(
                User.remna_id.is_not(None),
                or_(User.tier.is_(None), User.expires_at <= now),
                User.expires_at >= now - timedelta(days=7),
            )
            out["trial_used"] = await users(User.trial_used.is_(True))
            paid = exists().where(Payment.user_id == User.id, Payment.status == "succeeded")
            out["trial_paid"] = await users(User.trial_used.is_(True), paid)
            out["unsynced"] = await users(User.sync_pending.is_(True))

            out["payers"] = int(
                await s.scalar(
                    select(func.count(distinct(Payment.user_id))).where(Payment.status == "succeeded")
                ) or 0
            )

            async def revenue(since: datetime | None) -> tuple[Decimal, int]:
                conds = [Payment.status == "succeeded"]
                if since is not None:
                    conds.append(Payment.paid_at >= since)
                row = (
                    await s.execute(select(func.coalesce(func.sum(Payment.amount), 0), func.count()).where(*conds))
                ).one()
                return Decimal(row[0]), int(row[1])

            out["rev_today"] = await revenue(today)
            out["rev_7d"] = await revenue(now - timedelta(days=7))
            out["rev_30d"] = await revenue(now - timedelta(days=30))
            out["rev_month"] = await revenue(month)
            out["rev_all"] = await revenue(None)

            refunded = (
                await s.execute(
                    select(func.coalesce(func.sum(Payment.amount), 0), func.count()).where(Payment.status == "refunded")
                )
            ).one()
            out["refunded"] = (Decimal(refunded[0]), int(refunded[1]))
            out["pending_payments"] = int(
                await s.scalar(select(func.count()).select_from(Payment).where(Payment.status == "pending")) or 0
            )
            out["needs_review"] = int(
                await s.scalar(select(func.count()).select_from(Payment).where(Payment.status == "needs_review")) or 0
            )
            out["upgrades_30d"] = int(
                await s.scalar(
                    select(func.count()).select_from(Payment).where(
                        Payment.status == "succeeded", Payment.kind == "upgrade", Payment.paid_at >= now - timedelta(days=30)
                    )
                ) or 0
            )
            split = (
                await s.execute(
                    select(Payment.plan_code, func.count(), func.coalesce(func.sum(Payment.amount), 0))
                    .where(Payment.status == "succeeded", Payment.paid_at >= now - timedelta(days=30))
                    .group_by(Payment.plan_code)
                )
            ).all()
            out["plan_split"] = {code: (int(n), Decimal(total)) for code, n, total in split}
            out["discounts_30d"] = Decimal(
                await s.scalar(
                    select(func.coalesce(func.sum(Payment.discount), 0)).where(
                        Payment.status == "succeeded", Payment.paid_at >= now - timedelta(days=30)
                    )
                ) or 0
            )
            by_prov = (
                await s.execute(
                    select(Payment.provider, func.count(), func.coalesce(func.sum(Payment.amount), 0))
                    .where(Payment.status == "succeeded", Payment.paid_at >= now - timedelta(days=30))
                    .group_by(Payment.provider)
                )
            ).all()
            out["provider_split"] = {code: (int(n), Decimal(total)) for code, n, total in by_prov}
            out["autopay_users"] = await users(User.autopay.is_(True))
            out["promo_total"] = int(await s.scalar(select(func.count()).select_from(Promo)) or 0)
            out["promo_uses"] = int(await s.scalar(select(func.count()).select_from(PromoUse)) or 0)
        return out

    # ------------------------------------------------------------------ временные ряды
    @staticmethod
    def _fill(days: int, rows: dict[date, tuple[Any, ...]], default: tuple[Any, ...]) -> list[tuple[date, tuple[Any, ...]]]:
        end = to_local(utcnow()).date()
        return [(d, rows.get(d, default)) for d in (end - timedelta(days=i) for i in range(days - 1, -1, -1))]

    async def revenue_series(self, days: int = 30) -> list[tuple[date, Decimal, int]]:
        since = utcnow() - timedelta(days=days + 1)
        day = _local_day(Payment.paid_at)
        async with self.ctx.session() as s:
            res = await s.execute(
                select(day, func.coalesce(func.sum(Payment.amount), 0), func.count())
                .where(Payment.status == "succeeded", Payment.paid_at >= since)
                .group_by(day)
                .order_by(day)
            )
            rows = {d: (Decimal(a), int(c)) for d, a, c in res.all()}
        return [(d, v[0], v[1]) for d, v in self._fill(days, rows, (Decimal(0), 0))]

    async def signups_series(self, days: int = 30) -> list[tuple[date, int]]:
        since = utcnow() - timedelta(days=days + 1)
        day = _local_day(User.created_at)
        async with self.ctx.session() as s:
            res = await s.execute(
                select(day, func.count()).where(User.created_at >= since).group_by(day).order_by(day)
            )
            rows = {d: (int(c),) for d, c in res.all()}
        return [(d, v[0]) for d, v in self._fill(days, rows, (0,))]

    async def events_summary(self, days: int = 30) -> dict[str, int]:
        since = utcnow() - timedelta(days=days)
        async with self.ctx.session() as s:
            res = await s.execute(
                select(SubEvent.kind, func.count()).where(SubEvent.created_at >= since).group_by(SubEvent.kind)
            )
            return {k: int(n) for k, n in res.all()}

    # ------------------------------------------------------------------ графики
    @staticmethod
    def _render_bar(title: str, labels: list[str], values: list[float], ylabel: str) -> bytes:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(8, 4), dpi=140)
        fig.patch.set_facecolor("#10131c")
        ax.set_facecolor("#10131c")
        ax.bar(range(len(values)), values, color="#8E93FF", width=0.7)
        step = max(1, len(labels) // 8)
        ax.set_xticks(range(0, len(labels), step))
        ax.set_xticklabels(labels[::step], color="#cfd3e6", fontsize=8)
        ax.tick_params(axis="y", colors="#cfd3e6", labelsize=8)
        ax.set_title(title, color="#ffffff", fontsize=12, pad=10)
        ax.set_ylabel(ylabel, color="#cfd3e6", fontsize=9)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color("#3a3f55")
        ax.grid(axis="y", color="#262b3d", linewidth=0.6)
        ax.set_axisbelow(True)
        buf = io.BytesIO()
        fig.tight_layout()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
        return buf.getvalue()

    async def revenue_chart(self, days: int = 30) -> bytes:
        series = await self.revenue_series(days)
        total = sum(v for _, v, _ in series)
        return await asyncio.to_thread(
            self._render_bar,
            f"Выручка за {days} дн. — {total:,.0f} ₽".replace(",", " "),
            [d.strftime("%d.%m") for d, _, _ in series],
            [float(v) for _, v, _ in series],
            "₽",
        )

    async def signups_chart(self, days: int = 30) -> bytes:
        series = await self.signups_series(days)
        return await asyncio.to_thread(
            self._render_bar,
            f"Новые пользователи за {days} дн. — {sum(n for _, n in series)}",
            [d.strftime("%d.%m") for d, _ in series],
            [float(n) for _, n in series],
            "чел.",
        )

    # ------------------------------------------------------------------ экспорт
    async def export_users_csv(self) -> bytes:
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["id", "username", "имя", "регистрация", "тариф", "действует до", "очередь LITE, дн", "пробный",
                    "email", "реферер", "бан", "заблокировал бота", "remna_id"])
        async with self.ctx.session() as s:
            res = await s.stream_scalars(select(User).order_by(User.created_at))
            async for u in res:
                st = self.ctx.subs.view(u)
                w.writerow([
                    u.id, u.username or "", u.display_name, fmt_dt(u.created_at),
                    st.tier.value if st.tier else "", fmt_dt(st.expires_at) if st.tier else "",
                    round(st.queued_lite.total_seconds() / 86400, 1), int(u.trial_used), u.email or "",
                    u.referrer_id or "", int(u.is_banned), int(u.bot_blocked), u.remna_id or "",
                ])
        return ("\ufeff" + buf.getvalue()).encode("utf-8")

    async def export_payments_csv(self) -> bytes:
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["id", "user_id", "тариф", "тип", "сумма", "скидка", "статус", "создан", "оплачен", "external_id", "email"])
        async with self.ctx.session() as s:
            res = await s.stream_scalars(select(Payment).order_by(Payment.id))
            async for p in res:
                w.writerow([
                    p.id, p.user_id or "", p.plan_code, p.kind, f"{p.amount:.2f}", f"{p.discount:.2f}", p.status,
                    fmt_dt(p.created_at), fmt_dt(p.paid_at), p.external_id or "", p.receipt_email or "",
                ])
        return ("\ufeff" + buf.getvalue()).encode("utf-8")
