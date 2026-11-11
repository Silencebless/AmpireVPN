"""Лимиты доступа (чистая логика): устройства и трафик с учётом доп. устройств и пробного периода."""
from __future__ import annotations

GIB = 1024**3


def device_limit(base: int, extra: int = 0, *, trial_limited: bool = False, trial_devices: int = 1) -> int:
    """Сколько устройств разрешено: включённые в тариф + купленные дополнительно.

    На пробном периоде (``trial_limited``) лимит жёстко ограничен ``trial_devices`` — защита от раздачи пробного доступа.
    """
    total = max(1, int(base)) + max(0, int(extra))
    if trial_limited:
        total = min(total, max(1, int(trial_devices)))
    return total


def traffic_limit_bytes(trial_limited: bool, trial_gb: int) -> int:
    """Лимит трафика в байтах для панели: 0 — без лимита (платные подписки)."""
    gb = max(0, int(trial_gb))
    return gb * GIB if (trial_limited and gb > 0) else 0
