"""Тексты бота: значения по умолчанию из ``app.texts`` + переопределения из БД."""
from __future__ import annotations

import logging
import string
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import TextOverride
from app.texts import TEXTS
from app.utils import utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)


class _Safe(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class TextService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._over: dict[str, str] = {}

    async def load(self) -> None:
        async with self.ctx.session() as s:
            rows = (await s.execute(select(TextOverride))).scalars().all()
        self._over = {r.key: r.value for r in rows if r.key in TEXTS}

    def render(self, key: str, **values: Any) -> str:
        td = TEXTS[key]
        tmpl = self._over.get(key) or td.default
        try:
            return tmpl.format_map(_Safe(values))
        except Exception:  # noqa: BLE001
            log.warning("Ошибка шаблона текста %s — использую значение по умолчанию", key)
            return td.default.format_map(_Safe(values))

    def current(self, key: str) -> str:
        return self._over.get(key) or TEXTS[key].default

    def is_overridden(self, key: str) -> bool:
        return key in self._over

    @staticmethod
    def validate(key: str, value: str) -> str | None:
        """Возвращает текст ошибки или None, если шаблон корректен."""
        td = TEXTS[key]
        try:
            names = {n for _, n, _, _ in string.Formatter().parse(value) if n}
        except ValueError as e:
            return f"Ошибка в фигурных скобках: {e}. Для вставки значений используй {{имя}}."
        unknown = names - set(td.vars)
        if unknown:
            allowed = ", ".join("{" + v + "}" for v in td.vars) or "—"
            return f"Неизвестные переменные: {', '.join('{' + n + '}' for n in sorted(unknown))}. Доступны: {allowed}"
        if len(value) > 3500:
            return "Слишком длинный текст (максимум 3500 символов)."
        try:
            value.format_map({v: "x" for v in td.vars})
        except Exception as e:  # noqa: BLE001
            return f"Шаблон не форматируется: {e}"
        return None

    def sample(self, key: str, value: str | None = None) -> str:
        """Текст с подставленными примерами — для предпросмотра."""
        td = TEXTS[key]
        tmpl = value if value is not None else self.current(key)
        return tmpl.format_map(_Safe({v: f"‹{v}›" for v in td.vars}))

    async def set(self, key: str, value: str, actor_id: int | None) -> None:
        async with self.ctx.session() as s:
            stmt = pg_insert(TextOverride).values(key=key, value=value, updated_at=utcnow(), updated_by=actor_id)
            stmt = stmt.on_conflict_do_update(
                index_elements=[TextOverride.key],
                set_={
                    "value": stmt.excluded.value,
                    "updated_at": stmt.excluded.updated_at,
                    "updated_by": stmt.excluded.updated_by,
                },
            )
            await s.execute(stmt)
            await s.commit()
        self._over[key] = value
        await self.ctx.bus.publish("texts")

    async def reset(self, key: str) -> None:
        async with self.ctx.session() as s:
            row = await s.get(TextOverride, key)
            if row:
                await s.delete(row)
                await s.commit()
        self._over.pop(key, None)
        await self.ctx.bus.publish("texts")
