from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import labels, views
from app.bot.filters import NotCommand
from app.bot.states import UserFlow
from app.bot.ui import btn, kb, show
from app.core.pricing import PROMO_DAYS
from app.db.models import User
from app.services.promos import PromoError

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)
router = Router(name="user.referral")


@router.callback_query(F.data == "ref:menu")
async def cb_ref(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    text, markup = await views.referral_screen(ctx, await ctx.users.get(user.id) or user)
    await show(cb, text, markup)


@router.callback_query(F.data == "promo:ask")
async def cb_promo_ask(cb: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    await state.set_state(UserFlow.promo)
    await show(cb, ctx.texts.render("promo_ask"), kb(btn(labels.BACK, "ref:menu")))


@router.message(UserFlow.promo, F.text, NotCommand())
async def promo_input(message: Message, ctx: AppContext, user: User, state: FSMContext) -> None:
    try:
        result = await ctx.promos.redeem(user.id, message.text or "")
    except PromoError as e:
        # состояние оставляем — можно сразу попробовать другой код
        await message.answer(ctx.texts.render(f"promo_err_{e.code}"), reply_markup=kb(btn(labels.BACK, "ref:menu")))
        return
    await state.clear()
    text = views.promo_ok_text(ctx, result.promo, result.new_state)
    if result.promo.kind == PROMO_DAYS:
        markup = kb(btn(labels.SETUP, "setup:open"), btn(labels.SUBSCRIPTION, "menu:sub"), btn(labels.BACK, "ref:menu"))
    else:
        markup = kb(btn(labels.BUY, "pay:menu"), btn(labels.BACK, "ref:menu"))
    await message.answer(text, reply_markup=markup)
    await ctx.audit.log(user.id, "promo.redeem", result.promo.code)


@router.message(UserFlow.promo)
async def promo_other(message: Message, ctx: AppContext) -> None:
    await message.answer(ctx.texts.render("promo_ask"), reply_markup=kb(btn(labels.BACK, "ref:menu")))
