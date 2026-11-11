"""Мелкие утилиты: время, форматирование, склонения, экранирование."""
from __future__ import annotations

import asyncio
import html
import logging
import math
import re
import secrets
import string
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

UTC = timezone.utc
_TZ: ZoneInfo = ZoneInfo("UTC")
_log = logging.getLogger(__name__)
_bg_tasks: set[asyncio.Task] = set()

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def set_timezone(name: str) -> None:
    global _TZ
    try:
        _TZ = ZoneInfo(name)
    except Exception:  # noqa: BLE001
        _TZ = ZoneInfo("UTC")


def tz_name() -> str:
    return str(_TZ)


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_TZ)


def fmt_dt(dt: datetime | None) -> str:
    return "—" if dt is None else to_local(dt).strftime("%d.%m.%Y %H:%M")


def fmt_date(dt: datetime | None) -> str:
    return "—" if dt is None else to_local(dt).strftime("%d.%m.%Y")


def iso_z(dt: datetime) -> str:
    """Формат даты для Remnawave: 2026-11-03T12:00:00.000Z"""
    d = dt.astimezone(UTC)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def days_left(expires_at: datetime | None, now: datetime | None = None) -> int:
    """Сколько дней осталось (округление вверх). 0 — уже закончилось."""
    if expires_at is None:
        return 0
    now = now or utcnow()
    secs = (expires_at - now).total_seconds()
    return max(0, math.ceil(secs / 86400)) if secs > 0 else 0


def plural(n: int, forms: tuple[str, str, str]) -> str:
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return forms[2]
    if n % 10 == 1:
        return forms[0]
    if 2 <= n % 10 <= 4:
        return forms[1]
    return forms[2]


def days_word(n: int) -> str:
    return f"{n} {plural(n, ('день', 'дня', 'дней'))}"


def devices_word(n: int) -> str:
    return f"{n} {plural(n, ('устройство', 'устройства', 'устройств'))}"


def months_word(n: int) -> str:
    return f"{n} {plural(n, ('месяц', 'месяца', 'месяцев'))}"


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def money(value: Decimal | int | float | str) -> str:
    d = Decimal(str(value))
    if d == d.to_integral_value():
        return f"{int(d):,}".replace(",", " ") + " ₽"
    return f"{d:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def money_btn(value: Decimal | int | float | str) -> str:
    """Для кнопок: 149₽ (без пробела)."""
    return money(value).replace(" ₽", "₽")


def rand_code(n: int = 8, alphabet: str = string.ascii_uppercase + string.digits) -> str:
    # без похожих символов
    alphabet = "".join(c for c in alphabet if c not in "O0I1L")
    return "".join(secrets.choice(alphabet) for _ in range(n))


def human_bytes(n: float | int | None) -> str:
    if not n:
        return "0 Б"
    n = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def sparkline(values: list[float]) -> str:
    bars = "▁▂▃▄▅▆▇█"
    if not values:
        return ""
    hi = max(values)
    if hi <= 0:
        return bars[0] * len(values)
    return "".join(bars[min(len(bars) - 1, int(v / hi * (len(bars) - 1)))] for v in values)


def parse_int(text: str | None, *, lo: int | None = None, hi: int | None = None) -> int | None:
    if text is None:
        return None
    t = text.strip().replace(" ", "")
    if not re.fullmatch(r"-?\d{1,9}", t):
        return None
    v = int(t)
    if lo is not None and v < lo:
        return None
    if hi is not None and v > hi:
        return None
    return v


def parse_decimal(text: str | None, *, lo: Decimal | None = None, hi: Decimal | None = None) -> Decimal | None:
    if text is None:
        return None
    t = text.strip().replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"\d{1,7}(\.\d{1,2})?", t):
        return None
    v = Decimal(t)
    if lo is not None and v < lo:
        return None
    if hi is not None and v > hi:
        return None
    return v


def spawn(coro) -> asyncio.Task:
    """Запускает фоновую задачу, удерживая ссылку на неё и логируя исключения."""

    async def runner() -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _log.exception("фоновая задача упала")

    task = asyncio.create_task(runner())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


def bg_tasks_count() -> int:
    return len(_bg_tasks)


async def drain_tasks(timeout: float = 30.0) -> int:
    """Дожидается фоновых задач (обработка вебхуков оплаты и т. п.) при остановке процесса.

    Возвращает, сколько задач не успело завершиться за ``timeout`` секунд (они будут отменены вместе с циклом;
    платежи при этом подберёт сверка — вебхук не теряется, он лишь обрабатывается позже).
    """
    pending = {t for t in _bg_tasks if not t.done()}
    if not pending:
        return 0
    _log.info("ждём завершения фоновых задач: %d", len(pending))
    _done, still = await asyncio.wait(pending, timeout=timeout)
    if still:
        _log.warning("не успели завершиться %d фоновых задач", len(still))
    return len(still)
