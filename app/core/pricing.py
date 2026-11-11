"""Цены, сроки (1/3/6/12 месяцев), доплата за апгрейд, дополнительные устройства, скидки.

Чистая логика без БД и сети — целиком покрыта тестами.

Ключевые принципы
-----------------
* Цена тарифа задаётся за «месяц» (``Plan.period_days``, по умолчанию 30 дней). Срок 3/6/12 месяцев — это
  цена месяца × число месяцев со скидкой за срок. Скидки настраиваются в админке.
* Доплата при переходе LITE → PREMIUM считается по **суточным ставкам базовых (месячных) цен**:
  ``(цена_PREMIUM / дней_PREMIUM − цена_LITE / дней_LITE) × оставшиеся_дни``.
  Поэтому она не зависит ни от выбранного срока и скидок за срок, ни от того, как менялись цены раньше,
  ни от того, что у тарифов разная длина «месяца». Результат никогда не отрицателен.
* Итоговая сумма платежа фиксируется в момент создания счёта — последующая смена цен уже созданный счёт не ломает.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from app.core.subscription import SubState, Tier, normalize, purchase

PROMO_PERCENT = "percent"   # скидка в процентах
PROMO_FIXED = "fixed"       # скидка в рублях
PROMO_DAYS = "days"         # бонусные дни

MIN_PRICE = Decimal("1.00")  # ЮKassa не принимает платёж на 0 ₽
ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")

TERMS: tuple[int, ...] = (1, 3, 6, 12)                     # доступные сроки, месяцев
DEFAULT_TERM_DISCOUNTS: dict[int, int] = {1: 0, 3: 5, 6: 10, 12: 20}
MAX_DISCOUNT_PCT = 90
YEAR_DAYS = 365


def q2(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


# ───────────────────────── сроки ─────────────────────────
def term_days(months: int, month_days: int = 30) -> int:
    """Сколько дней даёт срок ``months`` месяцев. Год (12 мес.) при 30-дневном месяце — ровно 365 дней."""
    if months not in TERMS:
        raise ValueError(f"недопустимый срок: {months}")
    month_days = max(1, int(month_days))
    if months == 12 and month_days == 30:
        return YEAR_DAYS
    return months * month_days


def clamp_pct(pct: int | Decimal | float | None) -> int:
    try:
        v = int(pct or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_DISCOUNT_PCT, v))


def term_price(monthly: Decimal | int | str, months: int, discount_pct: int = 0) -> Decimal:
    """Цена срока: месяц × число месяцев со скидкой за срок. Со скидкой — округляем до целого рубля."""
    gross = Decimal(str(monthly)) * months
    pct = clamp_pct(discount_pct)
    if pct == 0:
        return q2(gross)
    return max(MIN_PRICE, (gross * (100 - pct) / Decimal(100)).quantize(ONE, rounding=ROUND_HALF_UP))


def devices_term_cost(extra_devices: int, device_price: Decimal | int | str, months: int, discount_pct: int = 0) -> Decimal:
    """Стоимость дополнительных устройств на срок продления (цена за устройство — в месяц)."""
    n = max(0, int(extra_devices))
    price = Decimal(str(device_price))
    if n == 0 or price <= 0:
        return ZERO
    return term_price(price * n, months, discount_pct)


def renewal_amount(
    monthly: Decimal | int | str,
    months: int,
    discount_pct: int = 0,
    extra_devices: int = 0,
    device_price: Decimal | int | str = ZERO,
) -> tuple[Decimal, Decimal, Decimal]:
    """Сумма продления без промокодов и доплат: (цена тарифа за срок, доп. устройства за срок, итого).

    Используется и в обычной покупке, и в автопродлении — суммы всегда считаются одинаково.
    """
    plan_part = term_price(monthly, months, discount_pct)
    dev_part = devices_term_cost(extra_devices, device_price, months, discount_pct)
    return plan_part, dev_part, q2(plan_part + dev_part)


# ───────────────────────── апгрейд ─────────────────────────
def _seconds(td: timedelta) -> Decimal:
    return Decimal(td.days) * 86400 + Decimal(td.seconds) + Decimal(td.microseconds) / Decimal(1_000_000)


def upgrade_extra(
    price_lite: Decimal,
    price_premium: Decimal,
    period_days: int,
    remaining: timedelta,
    *,
    premium_period_days: int | None = None,
) -> Decimal:
    """Доплата за остаток LITE при переходе на PREMIUM: разница суточных ставок × остаток. Вверх до целого рубля.

    ``period_days`` — длина «месяца» тарифа LITE (и PREMIUM, если ``premium_period_days`` не задан).
    Пример: LITE 149, PREMIUM 249, оба на 30 дней, осталось 15 дней → (249 − 149) × 15 / 30 = 50.
    """
    lite_days = max(1, int(period_days))
    prem_days = max(1, int(premium_period_days or period_days))
    daily_diff = Decimal(str(price_premium)) / prem_days - Decimal(str(price_lite)) / lite_days
    secs = _seconds(remaining)
    if daily_diff <= 0 or secs <= 0:
        return ZERO
    return (daily_diff * secs / Decimal(86400)).quantize(ONE, rounding=ROUND_CEILING)


# ───────────────────────── скидки ─────────────────────────
def discount_amount(amount: Decimal, kind: str | None, value: Decimal | None) -> Decimal:
    """Размер скидки в рублях (не опускаем цену ниже 1 ₽)."""
    if kind is None or value is None or amount <= MIN_PRICE:
        return Decimal("0.00")
    value = Decimal(str(value))
    if kind == PROMO_PERCENT:
        d = (amount * value / Decimal(100)).quantize(ONE, rounding=ROUND_HALF_UP)
    elif kind == PROMO_FIXED:
        d = value
    else:
        return Decimal("0.00")
    d = max(Decimal("0"), min(d, amount - MIN_PRICE))
    return q2(d)


# ───────────────────────── расчёт покупки ─────────────────────────
@dataclass(frozen=True, slots=True)
class Quote:
    tier: Tier
    kind: str                 # "buy" | "upgrade"
    base: Decimal             # цена до скидки (тариф + устройства + доплата)
    discount: Decimal         # скидка по промокоду
    amount: Decimal           # итог к оплате
    period_days: int          # сколько дней даёт покупка
    extra: Decimal            # доплата за остаток LITE (только upgrade)
    remaining: timedelta      # остаток текущего отрезка (для пояснений)
    before: SubState          # состояние до покупки
    after: SubState           # состояние после покупки
    months: int = 1           # выбранный срок
    plan_amount: Decimal = ZERO      # цена тарифа за срок (со скидкой за срок)
    devices_amount: Decimal = ZERO   # доп. устройства за срок
    extra_devices: int = 0           # сколько доп. устройств оплачено в этом продлении
    term_discount_pct: int = 0       # скидка за срок, %


def build_quote(
    state: SubState,
    tier: Tier,
    *,
    price_lite: Decimal,
    price_premium: Decimal,
    period_days: int,
    now: datetime,
    want_upgrade: bool = False,
    promo_kind: str | None = None,
    promo_value: Decimal | None = None,
    months: int = 1,
    term_discount_pct: int = 0,
    extra_devices: int = 0,
    device_price: Decimal | int | str = ZERO,
    lite_period_days: int | None = None,
    premium_period_days: int | None = None,
) -> Quote:
    """``period_days`` — длина «месяца» выбранного тарифа; сколько дней даст срок — решает ``term_days``."""
    before = normalize(state, now)
    p_lite, p_prem = Decimal(str(price_lite)), Decimal(str(price_premium))
    monthly = {Tier.LITE: p_lite, Tier.PREMIUM: p_prem}[tier]
    days = term_days(months, period_days)

    is_upgrade = bool(want_upgrade and tier == Tier.PREMIUM and before.tier == Tier.LITE)
    remaining = before.remaining(now)
    extra = ZERO
    if is_upgrade:
        extra = upgrade_extra(
            p_lite,
            p_prem,
            lite_period_days or period_days,
            remaining,
            premium_period_days=premium_period_days or period_days,
        )
    plan_part, dev_part, renewal = renewal_amount(monthly, months, term_discount_pct, extra_devices, device_price)
    base = q2(renewal + extra)
    disc = discount_amount(base, promo_kind, promo_value)
    after = purchase(before, tier, timedelta(days=days), now, upgrade=is_upgrade)
    return Quote(
        tier=tier,
        kind="upgrade" if is_upgrade else "buy",
        base=base,
        discount=disc,
        amount=q2(base - disc),
        period_days=days,
        extra=q2(extra),
        remaining=remaining,
        before=before,
        after=after,
        months=months,
        plan_amount=plan_part,
        devices_amount=dev_part,
        extra_devices=max(0, int(extra_devices)),
        term_discount_pct=clamp_pct(term_discount_pct),
    )


# ───────────────────────── дополнительные устройства ─────────────────────────
@dataclass(frozen=True, slots=True)
class DevicesQuote:
    count: int                # сколько устройств добавляем
    amount: Decimal           # сколько платим сейчас (пропорционально оставшемуся сроку)
    days: int                 # на сколько дней оплата (остаток подписки, вверх до суток)
    month_total: Decimal      # сколько это стоит в месяц (в составе продлений)
    before: SubState


def build_devices_quote(
    state: SubState,
    *,
    count: int,
    device_price: Decimal | int | str,
    month_days: int,
    now: datetime,
) -> DevicesQuote | None:
    """Доплата за новые места: цена за устройство в месяц × остаток подписки (по суткам, вверх до рубля).

    Возвращает None, если подписка не активна или ``count`` ≤ 0.
    """
    before = normalize(state, now)
    end = before.total_end
    if count <= 0 or not before.is_active(now) or end is None:
        return None
    price = Decimal(str(device_price))
    if price <= 0:
        return None
    days = max(1, math.ceil((end - now).total_seconds() / 86400))
    per_day = price / Decimal(max(1, int(month_days)))
    amount = max(MIN_PRICE, (per_day * days * count).quantize(ONE, rounding=ROUND_CEILING))
    return DevicesQuote(count=count, amount=q2(amount), days=days, month_total=q2(price * count), before=before)
