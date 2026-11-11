"""Чистая логика подписок (без БД и сети).

Модель состояния
----------------
* ``tier``        — активный тариф (LITE / PREMIUM) или None.
* ``expires_at``  — когда заканчивается *активный отрезок*.
* ``queued_lite`` — оплаченное время LITE, которое начнётся сразу после PREMIUM.

Правила (из ТЗ):
* Продление того же тарифа — просто +N дней к отрезку.
* PREMIUM -> LITE: LITE встаёт в очередь и стартует после окончания PREMIUM.
* LITE -> PREMIUM (с доплатой): остаток LITE превращается в PREMIUM, плюс новый период.
* Бонусные дни PREMIUM поверх активного LITE: PREMIUM идёт первым, LITE «замораживается»
  и продолжится после него. Бонусные дни LITE поверх PREMIUM встают в очередь.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

ZERO = timedelta(0)


class Tier(StrEnum):
    LITE = "lite"
    PREMIUM = "premium"

    @property
    def title(self) -> str:
        return self.value.upper()


@dataclass(frozen=True, slots=True)
class SubState:
    tier: Tier | None = None
    expires_at: datetime | None = None
    queued_lite: timedelta = ZERO

    def is_active(self, now: datetime) -> bool:
        return self.tier is not None and self.expires_at is not None and self.expires_at > now

    @property
    def total_end(self) -> datetime | None:
        """Когда пользователь окончательно потеряет доступ (с учётом очереди LITE)."""
        if self.expires_at is None:
            return None
        if self.tier == Tier.PREMIUM:
            return self.expires_at + self.queued_lite
        return self.expires_at

    def remaining(self, now: datetime) -> timedelta:
        if self.expires_at is None or self.expires_at <= now:
            return ZERO
        return self.expires_at - now


def normalize(s: SubState, now: datetime) -> SubState:
    """Продвигает состояние до момента ``now``: закончившийся PREMIUM -> очередь LITE -> пусто."""
    if s.tier is None or s.expires_at is None:
        return SubState(None, s.expires_at, ZERO)
    tier, exp, queued = s.tier, s.expires_at, s.queued_lite
    while exp <= now:
        if tier == Tier.PREMIUM and queued > ZERO:
            # LITE стартует ровно в момент окончания PREMIUM — не «теряем» и не «дарим» время
            tier, exp, queued = Tier.LITE, exp + queued, ZERO
        else:
            return SubState(None, exp, ZERO)
    return SubState(tier, exp, queued)


def grant(s: SubState, tier: Tier, delta: timedelta, now: datetime) -> SubState:
    """Добавить время (оплата без апгрейда, промокод, реферал, подарок админа)."""
    s = normalize(s, now)
    if delta <= ZERO:
        return s
    if s.tier is None:
        return SubState(tier, now + delta, ZERO)
    assert s.expires_at is not None
    if s.tier == tier:
        return replace(s, expires_at=s.expires_at + delta)
    if tier == Tier.PREMIUM:
        # активен LITE: PREMIUM идёт первым, остаток LITE ждёт своей очереди
        return SubState(Tier.PREMIUM, now + delta, s.expires_at - now)
    # активен PREMIUM, добавляем LITE: в очередь
    return replace(s, queued_lite=s.queued_lite + delta)


def purchase(s: SubState, tier: Tier, period: timedelta, now: datetime, *, upgrade: bool = False) -> SubState:
    """Платная покупка. ``upgrade=True`` — LITE -> PREMIUM с доплатой за остаток."""
    s = normalize(s, now)
    if upgrade and tier == Tier.PREMIUM and s.tier == Tier.LITE and s.expires_at is not None:
        # остаток LITE «переезжает» в PREMIUM (за него доплатили), затем + новый период
        return SubState(Tier.PREMIUM, s.expires_at + period, ZERO)
    return grant(s, tier, period, now)


def remove(s: SubState, tier: Tier, delta: timedelta, now: datetime) -> SubState:
    """Снять время с тарифа (админ / возврат платежа)."""
    s = normalize(s, now)
    if s.tier is None or s.expires_at is None or delta <= ZERO:
        return s
    if tier == s.tier:
        new_exp = s.expires_at - delta
        if new_exp > now:
            return replace(s, expires_at=new_exp)
        if s.tier == Tier.PREMIUM and s.queued_lite > ZERO:
            return SubState(Tier.LITE, now + s.queued_lite, ZERO)
        return SubState(None, now, ZERO)
    if tier == Tier.LITE and s.tier == Tier.PREMIUM:
        return replace(s, queued_lite=max(ZERO, s.queued_lite - delta))
    return s


def set_exact(tier: Tier | None, expires_at: datetime | None, now: datetime) -> SubState:
    """Админ задаёт тариф и срок явно."""
    if tier is None or expires_at is None or expires_at <= now:
        return SubState(None, now, ZERO)
    return SubState(tier, expires_at, ZERO)
