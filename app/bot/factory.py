from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import Dispatcher
from aiogram.fsm.storage.base import BaseStorage
from aiogram.types import ErrorEvent

from app.bot.handlers import admin as admin_handlers
from app.bot.handlers import user as user_handlers
from app.bot.middlewares import ResetStateMiddleware, ThrottleMiddleware, UserMiddleware
from app.infra.metrics import metrics
from app.utils import esc

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)


async def on_error(event: ErrorEvent, ctx: AppContext) -> bool:
    exc = event.exception
    log.error("Необработанная ошибка в хендлере: %s: %s", type(exc).__name__, exc, exc_info=exc)
    metrics.inc("handler_errors_total", exc=type(exc).__name__)
    upd = event.update
    try:
        if upd.callback_query:
            await upd.callback_query.answer(ctx.texts.render("error_alert"), show_alert=True)
        elif upd.message:
            await upd.message.answer(ctx.texts.render("error_message"))
    except Exception:  # noqa: BLE001
        pass
    # владельцам — не чаще раза в минуту, чтобы не засыпать
    try:
        if await ctx.redis.set("ampire:err:notify", 1, ex=60, nx=True):
            await ctx.notify.to_admins(
                f"🚨 <b>Ошибка в боте</b>\n<code>{esc(type(exc).__name__)}: {esc(str(exc))[:300]}</code>\n"
                "Подробности: Админка → Система → Журнал ошибок."
            )
    except Exception:  # noqa: BLE001
        pass
    return True


def create_dispatcher(ctx: AppContext, storage: BaseStorage) -> Dispatcher:
    dp = Dispatcher(storage=storage, ctx=ctx)

    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(ThrottleMiddleware())
        observer.outer_middleware(UserMiddleware())
    dp.callback_query.outer_middleware(ResetStateMiddleware())

    for router in admin_handlers.get_routers():
        dp.include_router(router)
    for router in user_handlers.get_routers():
        dp.include_router(router)

    dp.errors.register(on_error)
    return dp
