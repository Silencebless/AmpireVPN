"""Правила антифрода для рефералок (чистые функции)."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta


def hwids_overlap(a: Iterable[str], b: Iterable[str]) -> bool:
    """Одно и то же устройство у пригласившего и приглашённого — признак самореферала."""
    return bool({x.strip().lower() for x in a if x} & {x.strip().lower() for x in b if x})


def daily_cap_reached(rewards_last_24h: int, cap: int) -> bool:
    return cap > 0 and rewards_last_24h >= cap


def old_enough(created_at: datetime, now: datetime, min_hours: int) -> bool:
    return min_hours <= 0 or now - created_at >= timedelta(hours=min_hours)
