"""Чистая логика автопродления: что продлевать, когда предупреждать, списывать и повторять.

График одного цикла продления (E — конец подписки, на момент первой попытки):

* E − 24 ч — предупреждение о списании (один раз на цикл);
* E − 3 ч  — попытка № 1;
* E + 1 сут — попытка № 2 (если с картой всё в порядке, но «нет денег», это даёт шанс на пополнение);
* E + 3 сут — попытка № 3, после неё автопродление выключается.

Пока идут повторы, подписку продлевают «льготным периодом» (grace): доступ не обрывается до очередной попытки.
Постоянные отказы (карта просрочена, разрешение отозвано и т. п.) повторять бессмысленно — автопродление выключается сразу.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.core.subscription import SubState, Tier

ZERO = timedelta(0)
NOTICE_BEFORE = timedelta(hours=24)   # за сколько предупреждаем о списании
CHARGE_BEFORE = timedelta(hours=3)    # за сколько до конца начинаем списывать
RETRY_GAP = timedelta(minutes=45)     # минимальная пауза между любыми двумя попытками (защита от дублей)
RETRY_OFFSET_DAYS = (1, 3)            # повторы: через сутки и через трое суток после конца цикла
MAX_GRACE_DAYS = max(RETRY_OFFSET_DAYS)
DEFAULT_GRACE_DAYS = 3
MAX_FAILURES = 1 + len(RETRY_OFFSET_DAYS)   # совместимость: сколько всего попыток при полном льготном периоде
PENDING_GUARD = timedelta(hours=3)    # платёж «в обработке» моложе этого срока блокирует новое списание

# причины отказа ЮKassa, при которых повтор не поможет: нужна новая карта или новое согласие
PERMANENT_DECLINES = frozenset(
    {
        "permission_revoked",
        "card_expired",
        "invalid_card_number",
        "invalid_csc",
        "payment_method_restricted",
        "identification_required",
        "country_forbidden",
        "unsupported_mobile_operator",
        "canceled_by_merchant",
    }
)


def is_permanent_decline(reason: str | None) -> bool:
    return (reason or "").strip().lower() in PERMANENT_DECLINES


def renewal_tier(state: SubState) -> Tier | None:
    """Продлеваем тариф, который идёт последним в цепочке (очередь LITE после PREMIUM — продлеваем LITE)."""
    if state.tier is None:
        return None
    return Tier.LITE if state.queued_lite.total_seconds() > 0 else state.tier


def retry_offsets(grace_days: int) -> list[timedelta]:
    g = max(0, min(MAX_GRACE_DAYS, int(grace_days)))
    return [timedelta(days=d) for d in RETRY_OFFSET_DAYS if d <= g]


def max_attempts(grace_days: int) -> int:
    return 1 + len(retry_offsets(grace_days))


def next_attempt_at(cycle_end: datetime, failures: int, grace_days: int = DEFAULT_GRACE_DAYS) -> datetime | None:
    """Когда пора делать следующую попытку после ``failures`` неудач; None — попыток больше нет."""
    if failures <= 0:
        return cycle_end - CHARGE_BEFORE
    offsets = retry_offsets(grace_days)
    if failures > len(offsets):
        return None
    return cycle_end + offsets[failures - 1]


def grace_extension(cycle_end: datetime, failures: int, current_end: datetime, grace_days: int = DEFAULT_GRACE_DAYS) -> timedelta:
    """На сколько продлить подписку после неудачи, чтобы доступ дожил до следующей попытки (с запасом CHARGE_BEFORE).

    Следующая попытка всегда происходит, пока подписка ещё активна (за CHARGE_BEFORE до её конца) — иначе воркер
    «потерял» бы пользователя: просроченная подписка снимается раз в полминуты.
    """
    nxt = next_attempt_at(cycle_end, failures, grace_days)
    if nxt is None:
        return ZERO
    return max(ZERO, nxt + CHARGE_BEFORE - current_end)


def autopay_phase(
    state: SubState,
    now: datetime,
    notified_for: datetime | None,
    last_attempt: datetime | None,
    *,
    next_at: datetime | None = None,
    failures: int = 0,
) -> str | None:
    """'notice' — пора предупредить; 'charge' — пора списывать; None — ничего не делать."""
    too_soon = last_attempt is not None and now - last_attempt < RETRY_GAP
    if failures > 0 and next_at is not None:
        # идёт цикл повторов: ждём назначенного времени (подписка может быть уже неактивной — это нормально)
        return "charge" if (now >= next_at and not too_soon) else None
    end = state.total_end
    if not state.is_active(now) or end is None:
        return None
    left = end - now
    if left > NOTICE_BEFORE:
        return None
    if left > CHARGE_BEFORE:
        return "notice" if notified_for != end else None
    return None if too_soon else "charge"
