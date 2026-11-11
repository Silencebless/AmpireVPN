from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.infra.metrics import metrics
from app.logging_setup import set_log_user

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)

Handler = Callable[[Any, dict[str, Any]], Awaitable[Any]]


async def _tell(event: Message | CallbackQuery, text: str) -> None:
    try:
        if isinstance(event, CallbackQuery):
            await event.answer(text[:190], show_alert=True)
        else:
            await event.answer(text)
    except TelegramAPIError:
        pass


class ThrottleMiddleware(BaseMiddleware):
    """Не даёт спамить: не чаще одного действия за ~0.4 сек от пользователя (админы без ограничений).

    Fail-open: если Redis недоступен, бот не замолкает — работает локальный ограничитель в памяти процесса.
    """

    def __init__(self, interval_ms: int = 400) -> None:
        self.interval_ms = interval_ms
        self._local: dict[int, float] = {}

    def _local_allow(self, user_id: int) -> bool:
        now = time.monotonic()
        if len(self._local) > 20000:  # защита от разрастания словаря
            cutoff = now - 5
            self._local = {k: v for k, v in self._local.items() if v > cutoff}
        last = self._local.get(user_id, 0.0)
        if now - last < self.interval_ms / 1000.0:
            return False
        self._local[user_id] = now
        return True

    async def __call__(self, handler: Handler, event: Message | CallbackQuery, data: dict[str, Any]) -> Any:
        ctx: AppContext = data["ctx"]
        tg = data.get("event_from_user")
        metrics.inc("tg_updates_total", type=type(event).__name__.lower())
        if tg is not None and not ctx.is_admin(tg.id):
            try:
                ok = bool(await ctx.redis.set(f"ampire:thr:{tg.id}", 1, px=self.interval_ms, nx=True))
            except Exception:  # noqa: BLE001 — Redis недоступен: не роняем бота
                metrics.inc("throttle_redis_errors_total")
                ok = self._local_allow(tg.id)
            if not ok:
                metrics.inc("tg_throttled_total")
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer()
                    except TelegramAPIError:
                        pass
                return None
        return await handler(event, data)


class UserMiddleware(BaseMiddleware):
    """Загружает/создаёт пользователя и применяет бан и режим техработ."""

    async def __call__(self, handler: Handler, event: Message | CallbackQuery, data: dict[str, Any]) -> Any:
        ctx: AppContext = data["ctx"]
        tg = data.get("event_from_user")
        if tg is None or tg.is_bot:
            return await handler(event, data)

        set_log_user(tg.id)
        is_admin = ctx.is_admin(tg.id)
        allow_create = is_admin or bool(ctx.cfg.get("registration_open"))
        user, created = await ctx.users.upsert_from_telegram(tg, allow_create=allow_create)
        if user is None:
            await _tell(event, ctx.texts.render("registration_closed"))
            return None
        data["user"] = user
        data["is_new_user"] = created

        if not is_admin:
            if user.is_banned:
                await _tell(event, ctx.texts.render("banned"))
                return None
            if ctx.cfg.get("maintenance"):
                await _tell(event, ctx.texts.render("maintenance"))
                return None
        return await handler(event, data)


class ResetStateMiddleware(BaseMiddleware):
    """Любое нажатие кнопки выводит из режима ожидания текстового ввода (данные wizard-ов сохраняются)."""

    async def __call__(self, handler: Handler, event: CallbackQuery, data: dict[str, Any]) -> Any:
        state: FSMContext | None = data.get("state")
        if state is not None:
            try:
                if await state.get_state() is not None:
                    await state.set_state(None)
            except Exception:  # noqa: BLE001 — Redis недоступен: идём дальше без сброса состояния
                metrics.inc("fsm_storage_errors_total", op="reset")
        return await handler(event, data)
