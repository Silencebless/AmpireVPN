"""Настройки, которые владелец меняет прямо из бота (хранятся в БД, кэшируются в памяти)."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.pricing import MAX_DISCOUNT_PCT, TERMS
from app.db.models import Setting
from app.legal import DEFAULT_URLS, DOCS, doc_url
from app.utils import utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    # пробный период
    "trial_enabled": True,
    "trial_days": 7,
    "trial_tier": "premium",
    "trial_channel": "",               # @канал или -100…: подписка обязательна для получения пробного периода
    "trial_traffic_gb": 10,            # лимит трафика на пробном периоде (0 = без лимита) — защита от фарма аккаунтов
    "trial_devices": 1,                # устройств на пробном периоде
    # сроки и дополнительные устройства
    "term_discount_3": 5,              # скидка за 3 месяца, %
    "term_discount_6": 10,             # скидка за 6 месяцев, %
    "term_discount_12": 20,            # скидка за 12 месяцев, %
    "device_price": 25,                # ₽ за одно дополнительное устройство в месяц
    "max_extra_devices": 10,           # максимум дополнительных устройств на пользователя
    # доступ
    "registration_open": True,
    "maintenance": False,
    "payments_enabled": True,
    "pay_yookassa_enabled": True,      # способы оплаты (показываются, если подключены ключи в .env)
    "pay_crypto_enabled": True,
    "autopay_enabled": False,          # автопродление картой: сначала включите автоплатежи у ЮKassa
    "autopay_grace_days": 3,           # льготный период: сколько суток держим доступ, пока повторяем списание (0–3)
    "receipts_enabled": True,          # перезаписывается значением из .env при старте
    # уведомления
    "reminders_enabled": True,
    "reminder_days": [3, 1],
    "notify_sales": True,
    "alerts_nodes": True,
    # устройства
    "trim_devices_on_downgrade": True,
    # поддержка и документы
    "support_url": "",
    **DEFAULT_URLS,                    # faq_url и legal_*_url — ссылки-заглушки, замените на боевые
    # рефералка
    "referral_enabled": True,
    "referral_days": 7,
    "referral_tier": "lite",
    "referral_min_mb": 10,
    "referral_max_rewards": 50,        # потолок наград на одного пригласившего (0 = без ограничений — не рекомендуется)
    "referral_daily_cap": 5,           # макс. наград одному пригласившему за сутки (0 = без лимита)
    "referral_min_age_h": 24,          # приглашённый должен «прожить» столько часов до награды (защита от фарма)
    "referral_check_hwid": True,       # отклонять награду, если у обоих одно и то же устройство
}

# (ключ, название, тип) — для экрана «Настройки»
SETTING_META: list[tuple[str, str, str]] = [
    ("trial_enabled", "Пробный период", "bool"),
    ("trial_days", "Дней пробного периода", "int"),
    ("trial_tier", "Тариф пробного периода", "tier"),
    ("trial_channel", "Канал для пробного периода (@имя, пусто — не требовать)", "text"),
    ("registration_open", "Приём новых пользователей", "bool"),
    ("payments_enabled", "Приём платежей", "bool"),
    ("pay_yookassa_enabled", "Оплата: ЮKassa (карты, СБП)", "bool"),
    ("pay_crypto_enabled", "Оплата: CryptoBot (крипта)", "bool"),
    ("autopay_enabled", "Автопродление картой (нужны автоплатежи в ЮKassa)", "bool"),
    ("receipts_enabled", "Чеки (данные для ЮKassa)", "bool"),
    ("maintenance", "Режим техработ", "bool"),
    ("reminders_enabled", "Напоминания об окончании", "bool"),
    ("reminder_days", "За сколько дней напоминать", "days_list"),
    ("trim_devices_on_downgrade", "Отключать лишние устройства при переходе на LITE", "bool"),
    ("notify_sales", "Уведомлять владельцев об оплатах", "bool"),
    ("alerts_nodes", "Алерты о падении нод", "bool"),
    ("support_url", "Ссылка на поддержку", "text"),
]

# настройки цен и устройств — отдельный экран «Цены и сроки»
PRICING_META: list[tuple[str, str, str]] = [
    ("device_price", "Цена доп. устройства, ₽ в месяц", "int"),
    ("max_extra_devices", "Макс. доп. устройств на пользователя", "int"),
    ("term_discount_3", "Скидка за 3 месяца, %", "int"),
    ("term_discount_6", "Скидка за 6 месяцев, %", "int"),
    ("term_discount_12", "Скидка за 12 месяцев, %", "int"),
    ("autopay_grace_days", "Льготный период автопродления, суток (0–3)", "int"),
]

# защита пробного периода от фарма
TRIAL_META: list[tuple[str, str, str]] = [
    ("trial_traffic_gb", "Трафик на пробном периоде, ГБ (0 = без лимита)", "int"),
    ("trial_devices", "Устройств на пробном периоде", "int"),
]

# ссылки на документы и FAQ — экран «Документы»
LEGAL_META: list[tuple[str, str, str]] = [(d.setting, d.title, "text") for d in DOCS.values()] + [
    ("faq_url", "FAQ (Telegraph)", "text"),
]

# допустимые значения числовых настроек: (мин, макс)
INT_BOUNDS: dict[str, tuple[int, int]] = {
    "trial_days": (1, 90),
    "trial_traffic_gb": (0, 1000),
    "trial_devices": (1, 10),
    "device_price": (1, 5000),
    "max_extra_devices": (0, 50),
    "term_discount_3": (0, MAX_DISCOUNT_PCT),
    "term_discount_6": (0, MAX_DISCOUNT_PCT),
    "term_discount_12": (0, MAX_DISCOUNT_PCT),
    "autopay_grace_days": (0, 3),
    "referral_days": (1, 365),
    "referral_min_mb": (0, 100000),
    "referral_max_rewards": (0, 100000),
    "referral_daily_cap": (0, 1000),
    "referral_min_age_h": (0, 720),
}

REFERRAL_META: list[tuple[str, str, str]] = [
    ("referral_enabled", "Реферальная программа", "bool"),
    ("referral_days", "Бонус пригласившему, дней", "int"),
    ("referral_tier", "Тариф бонуса", "tier"),
    ("referral_min_mb", "Порог «пользуется VPN», МБ трафика", "int"),
    ("referral_max_rewards", "Макс. наград на одного (0 = без лимита)", "int"),
    ("referral_daily_cap", "Макс. наград в сутки на одного (0 = без лимита)", "int"),
    ("referral_min_age_h", "Мин. возраст приглашённого, часов", "int"),
    ("referral_check_hwid", "Проверять совпадение устройств (антифрод)", "bool"),
]

META_BY_KEY = {k: (t, kind) for k, t, kind in SETTING_META + REFERRAL_META + PRICING_META + TRIAL_META + LEGAL_META}


class SettingsService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._cache: dict[str, Any] = {}
        self._defaults = dict(DEFAULTS)
        self._defaults["receipts_enabled"] = ctx.settings.yookassa_receipts
        # боевые ссылки из .env (LEGAL_*_URL, FAQ_URL) важнее заглушек
        for key in DEFAULT_URLS:
            env_value = str(getattr(ctx.settings, key, "") or "").strip()
            if env_value:
                self._defaults[key] = env_value

    async def load(self) -> None:
        async with self.ctx.session() as s:
            rows = (await s.execute(select(Setting))).scalars().all()
        self._cache = {r.key: r.value for r in rows}

    def get(self, key: str) -> Any:
        if key in self._cache:
            return self._cache[key]
        return self._defaults[key]

    def is_overridden(self, key: str) -> bool:
        return key in self._cache

    # --- удобные производные значения ---
    def term_discounts(self) -> dict[int, int]:
        """Скидка за срок в процентах: {1: 0, 3: …, 6: …, 12: …}."""
        out = {1: 0}
        for m in TERMS:
            if m != 1:
                out[m] = int(self.get(f"term_discount_{m}") or 0)
        return out

    def device_price(self) -> int:
        return max(0, int(self.get("device_price") or 0))

    def legal_url(self, key: str) -> str:
        return doc_url(self.get, key)

    def faq_url(self) -> str:
        return str(self.get("faq_url") or "").strip() or DEFAULT_URLS["faq_url"]

    async def set(self, key: str, value: Any) -> None:
        if key not in self._defaults:
            raise KeyError(key)
        async with self.ctx.session() as s:
            stmt = pg_insert(Setting).values(key=key, value=value, updated_at=utcnow())
            stmt = stmt.on_conflict_do_update(
                index_elements=[Setting.key],
                set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at},
            )
            await s.execute(stmt)
            await s.commit()
        self._cache[key] = value
        await self.ctx.bus.publish("settings")

    async def reset(self, key: str) -> None:
        async with self.ctx.session() as s:
            row = await s.get(Setting, key)
            if row:
                await s.delete(row)
                await s.commit()
        self._cache.pop(key, None)
        await self.ctx.bus.publish("settings")

    def format_value(self, key: str) -> str:
        _, kind = META_BY_KEY[key]
        v = self.get(key)
        if kind == "bool":
            return "включено ✅" if v else "выключено ⛔️"
        if kind == "days_list":
            return ", ".join(str(x) for x in v) or "—"
        if kind == "tier":
            return str(v).upper()
        if kind == "text":
            return str(v) or "— (берётся из .env)"
        return str(v)
