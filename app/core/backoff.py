"""Паузы между повторами (чистые функции): синхронизация с панелью, проверка рефералов, доставка сообщений."""
from __future__ import annotations

from datetime import timedelta

SYNC_BACKOFF_CAP = timedelta(minutes=10)


def sync_backoff(attempts: int) -> timedelta:
    """Пауза перед следующей попыткой синхронизации с панелью: 60 с, 2, 4, 8 мин и далее не дольше 10 минут.

    Раньше потолок был час — после аварии панели оплатившие новички ждали доступ слишком долго.
    """
    n = max(1, int(attempts))
    return min(timedelta(seconds=30 * 2 ** min(n, 5)), SYNC_BACKOFF_CAP)


def referral_check_delay(age: timedelta) -> timedelta:
    """Как часто опрашивать панель по ожидающему рефералу: чем он старше, тем реже.

    Основной путь — вебхук user.first_connected; опрос только страхует. Так нагрузка не растёт вместе с числом
    «залежавшихся» приглашённых (десятки тысяч не превращаются в постоянный поток запросов).
    """
    h = age.total_seconds() / 3600
    if h < 6:
        return timedelta(minutes=15)
    if h < 24:
        return timedelta(hours=1)
    if h < 72:
        return timedelta(hours=6)
    if h < 24 * 14:
        return timedelta(hours=12)
    return timedelta(hours=24)


def send_retry_delay(attempt: int) -> float:
    """Пауза (сек) между повторами отправки сообщения Telegram при сетевых сбоях."""
    steps = (0.5, 2.0, 5.0, 15.0, 30.0)
    return steps[min(max(0, attempt), len(steps) - 1)]


def outbox_delay(attempts: int) -> timedelta:
    """Пауза перед очередной попыткой доставить важное сообщение из «исходящей очереди»."""
    return min(timedelta(seconds=30 * 2 ** min(max(0, int(attempts)), 10)), timedelta(minutes=30))
