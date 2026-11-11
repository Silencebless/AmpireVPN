from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.subscription import Tier
from app.db.models import Plan
from app.utils import devices_word, esc, money, money_btn

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)


class PlanService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._cache: dict[str, Plan] = {}

    async def ensure_defaults(self) -> None:
        """Создаёт тарифы LITE и PREMIUM при первом запуске.

        INSERT … ON CONFLICT DO NOTHING: при одновременном первом старте нескольких реплик гонки нет.
        Устройства по умолчанию: LITE — 1, PREMIUM — 3; сверх этого можно докупать (см. настройку «Цена доп. устройства»).
        """
        st = self.ctx.settings
        defaults = [
            dict(code="lite", title="LITE", price=Decimal("149"), period_days=30, device_limit=1, locations=5,
                 whitelist=False, features="Поддержка в Telegram", squads=st.lite_squad_list, is_active=True, sort=1),
            dict(code="premium", title="PREMIUM", price=Decimal("249"), period_days=30, device_limit=3, locations=10,
                 whitelist=True, features="Поддержка в Telegram", squads=st.premium_squad_list, is_active=True, sort=2),
        ]
        async with self.ctx.session() as s:
            for row in defaults:
                await s.execute(pg_insert(Plan).values(**row).on_conflict_do_nothing(index_elements=[Plan.code]))
            await s.commit()
        await self.reload()

    async def reload(self) -> None:
        async with self.ctx.session() as s:
            rows = (await s.execute(select(Plan).order_by(Plan.sort, Plan.code))).scalars().all()
        self._cache = {p.code: p for p in rows}

    # --- чтение (из кэша) ---
    def get(self, code: str | Tier) -> Plan:
        return self._cache[code.value if isinstance(code, Tier) else code]

    def all(self) -> list[Plan]:
        return sorted(self._cache.values(), key=lambda p: (p.sort, p.code))

    def active(self) -> list[Plan]:
        return [p for p in self.all() if p.is_active]

    def is_sellable(self, plan: Plan) -> bool:
        """Тариф можно продавать и выдавать: он включён и у него есть сквады (иначе подписка без серверов)."""
        return bool(plan.is_active and (plan.squads or self.ctx.settings.allow_empty_squads))

    def sellable(self) -> list[Plan]:
        return [p for p in self.active() if self.is_sellable(p)]

    # --- запись ---
    async def update(self, code: str, **fields: Any) -> Plan:
        async with self.ctx.session() as s:
            plan = await s.get(Plan, code)
            if plan is None:
                raise KeyError(code)
            for k, v in fields.items():
                setattr(plan, k, v)
            await s.commit()
        await self.reload()
        await self.ctx.bus.publish("plans")
        return self.get(code)

    # --- представление ---
    @staticmethod
    def features_block(plan: Plan, *, device_price: int | None = None) -> str:
        lines = [
            f"◆ Устройств в тарифе — <b>{plan.device_limit}</b>",
            f"◆ Локаций — <b>{plan.locations}</b>",
            "◆ Обход белых списков" if plan.whitelist else "◇ Без обхода белых списков",
            "◆ Безлимитный трафик",
        ]
        if device_price:
            lines.append(f"◆ Дополнительное устройство — <b>{money(device_price)}</b> в месяц")
        extra = [f"◆ {esc(x.strip())}" for x in (plan.features or "").splitlines() if x.strip()]
        return "\n".join(lines + extra)

    @staticmethod
    def short_line(plan: Plan) -> str:
        wl = " · белые списки" if plan.whitelist else ""
        return (
            f"<b>{esc(plan.title)}</b> — {money(plan.price)} в месяц\n"
            f"{devices_word(plan.device_limit)} · {plan.locations} лок.{wl}"
        )

    @staticmethod
    def button_label(plan: Plan) -> str:
        return f"{plan.title} · {money_btn(plan.price)} в месяц"
