from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import labels, views
from app.bot.filters import NotCommand
from app.bot.states import UserFlow
from app.bot.ui import btn, kb, show, toast
from app.core.pricing import TERMS
from app.core.subscription import Tier
from app.db.models import User
from app.services.payments import NeedEmail, PaymentError
from app.utils import EMAIL_RE, devices_word, fmt_dt, money, months_word

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)
router = Router(name="user.billing")


async def _fresh(ctx: AppContext, user: User) -> User:
    return await ctx.users.get(user.id) or user


def _parse(data: str | None, *, start: int) -> tuple[str, int, bool] | None:
    """Разбирает «…:<тариф>:<месяцев>[:up]» начиная с позиции start. None — мусор в данных кнопки."""
    parts = (data or "").split(":")
    try:
        code, months = parts[start], int(parts[start + 1])
    except (IndexError, ValueError):
        return None
    if code not in ("lite", "premium") or months not in TERMS:
        return None
    return code, months, len(parts) > start + 2 and parts[start + 2] == "up"


@router.callback_query(F.data == "menu:sub")
async def cb_sub(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    text, markup = await views.sub_menu(ctx, await _fresh(ctx, user))
    await show(cb, text, markup)


@router.callback_query(F.data == "pay:menu")
async def cb_pay_menu(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    user = await _fresh(ctx, user)
    if user.consent_at is None:
        # пользователь прежней версии бота: перед оплатой берём согласие с документами
        text, markup = views.legal_gate_screen(ctx, "pay")
    else:
        text, markup = views.pay_menu(ctx, user)
    await show(cb, text, markup)


@router.callback_query(F.data.startswith("pay:plan:"))
async def cb_plan(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    code = (cb.data or "").split(":")[2]
    screen = await views.plan_view(ctx, await _fresh(ctx, user), code)
    if screen is None:
        await toast(cb, "Этот тариф сейчас недоступен", alert=True)
        return
    await show(cb, *screen)


@router.callback_query(F.data.startswith("pay:t:"))
async def cb_term(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    """Выбран срок — показываем сводку заказа."""
    parsed = _parse(cb.data, start=2)
    screen = await views.order_screen(ctx, await _fresh(ctx, user), *parsed) if parsed else None
    if screen is None:
        await toast(cb, "Этот тариф сейчас недоступен", alert=True)
        return
    await show(cb, *screen)


async def start_payment(
    event: Message | CallbackQuery,
    ctx: AppContext,
    user: User,
    state: FSMContext,
    code: str,
    months: int,
    upgrade: bool,
    option: str,
    *,
    autopay_consent: bool = False,
) -> None:
    if code not in ("lite", "premium") or option not in views.PAY_OPTIONS or months not in TERMS:
        if isinstance(event, CallbackQuery):
            await toast(event, "Этот тариф сейчас недоступен", alert=True)
        return
    tier = Tier(code)
    provider, save = views.PAY_OPTIONS[option]
    try:
        pay = await ctx.pay.create(
            user.id, tier, months=months, upgrade=upgrade, provider=provider, save_method=save, autopay_consent=autopay_consent
        )
    except NeedEmail:
        await state.set_state(UserFlow.email)
        await state.update_data(code=code, months=months, upgrade=upgrade, option=option, autopay_consent=autopay_consent)
        await show(
            event,
            ctx.texts.render("email_ask", privacy_url=views.attr(ctx.cfg.legal_url("privacy"))),
            kb(btn(labels.BACK, f"pay:plan:{code}")),
        )
        return
    except PaymentError as e:
        if e.reason == "need_consent":
            text, markup = views.legal_gate_screen(ctx, "pay")
            await show(event, text, markup)
            return
        if e.reason == "need_autopay_consent":
            text, markup = await views.autopay_consent_screen(ctx, user, code, months, upgrade)
            await show(event, text, markup)
            return
        log.warning("оплата недоступна для %s: %s", user.id, e)
        await show(event, ctx.texts.render("pay_unavailable"), kb(btn(labels.BACK, "pay:menu")))
        return

    term = months_word(int((pay.meta or {}).get("months") or months))
    discount_line = f"\nСкидка по промокоду — −{money(pay.discount)}" if pay.discount and pay.discount > 0 else ""
    if pay.provider == "cryptobot":
        text = ctx.texts.render("pay_created_crypto", what=f"{tier.title} · {term}", amount=money(pay.amount), discount_line=discount_line)
        pay_label = "Оплатить в CryptoBot"
    else:
        text = ctx.texts.render("pay_created", plan=tier.title, term=term, amount=money(pay.amount), discount_line=discount_line)
        pay_label = labels.GO_PAY
    markup = kb(
        btn(pay_label, url=pay.confirmation_url) if pay.confirmation_url else None,
        btn(labels.CHECK_PAYMENT, f"pay:chk:{pay.id}"),
        btn(labels.BACK, "pay:menu"),
    )
    await show(event, text, markup)


async def _choose_or_pay(
    event: Message | CallbackQuery, ctx: AppContext, user: User, state: FSMContext, code: str, months: int, upgrade: bool
) -> None:
    options = views.payment_options(ctx)
    if not options:
        await show(event, ctx.texts.render("pay_unavailable"), kb(btn(labels.BACK, "pay:menu")))
        return
    if len(options) == 1:
        await start_payment(event, ctx, user, state, code, months, upgrade, options[0])
        return
    text, markup = await views.pay_method_screen(ctx, user, code, months, upgrade)
    await show(event, text, markup)


@router.callback_query(F.data.startswith("pay:go:"))
async def cb_pay_go(cb: CallbackQuery, ctx: AppContext, user: User, state: FSMContext) -> None:
    parsed = _parse(cb.data, start=2)
    if parsed is None:
        await toast(cb, "")
        return
    await _choose_or_pay(cb, ctx, await _fresh(ctx, user), state, *parsed)


@router.callback_query(F.data.startswith("pay:m:"))
async def cb_pay_method(cb: CallbackQuery, ctx: AppContext, user: User, state: FSMContext) -> None:
    parts = (cb.data or "").split(":")  # pay m <option> <code> <months> [up]
    parsed = _parse(cb.data, start=3)
    if parsed is None or len(parts) < 5:
        await toast(cb, "")
        return
    option = parts[2]
    user = await _fresh(ctx, user)
    if option == "yka":
        # карту с автопродлением привязываем только после явного согласия с условиями
        text, markup = await views.autopay_consent_screen(ctx, user, *parsed)
        await show(cb, text, markup)
        return
    await start_payment(cb, ctx, user, state, *parsed, option)


@router.callback_query(F.data.startswith("pay:mc:"))
async def cb_pay_method_consented(cb: CallbackQuery, ctx: AppContext, user: User, state: FSMContext) -> None:
    """«Согласен и продолжить» на экране условий автопродления."""
    parts = (cb.data or "").split(":")  # pay mc <option> <code> <months> [up]
    parsed = _parse(cb.data, start=3)
    if parsed is None or len(parts) < 5:
        await toast(cb, "")
        return
    await start_payment(cb, ctx, await _fresh(ctx, user), state, *parsed, parts[2], autopay_consent=True)


# ───────────────────────── автопродление ─────────────────────────
@router.callback_query(F.data == "autopay:off")
async def cb_autopay_off(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    await ctx.pay.set_autopay(user.id, False)
    await show(cb, ctx.texts.render("autopay_off"), kb(btn(labels.SUBSCRIPTION, "menu:sub")))


@router.callback_query(F.data == "autopay:on")
async def cb_autopay_on(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    if not await ctx.pay.set_autopay(user.id, True):
        await toast(cb, "Сначала оплатите подписку картой с автопродлением — так мы сохраним способ оплаты.", alert=True)
        return
    await show(cb, ctx.texts.render("autopay_on"), kb(btn(labels.SUBSCRIPTION, "menu:sub")))


@router.callback_query(F.data == "autopay:unlink")
async def cb_autopay_unlink(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    await ctx.pay.unlink_methods(user.id)
    await show(cb, ctx.texts.render("autopay_unlinked"), kb(btn(labels.SUBSCRIPTION, "menu:sub")))


@router.callback_query(F.data.startswith("pay:chk:"))
async def cb_pay_check(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    try:
        pay_id = int((cb.data or "").split(":")[2])
    except (IndexError, ValueError):
        await toast(cb, "")
        return
    pay = await ctx.pay.check(pay_id, user.id)
    if pay is None:
        await toast(cb, "Платёж не найден", alert=True)
        return
    if pay.status == "succeeded":
        fresh = await _fresh(ctx, user)
        st = ctx.subs.view(fresh)
        if pay.kind == "devices":
            text = ctx.texts.render(
                "pay_success_devices",
                count=(pay.meta or {}).get("devices", 0),
                limit=ctx.subs.device_limit_for(fresh, st),
                until=fmt_dt(st.total_end),
            )
        else:
            text = ctx.texts.render("pay_success", plan=Tier(pay.plan_code).title, until=fmt_dt(st.total_end), extra="")
        await show(cb, text, kb([btn(labels.SETUP, "setup:open"), btn(labels.SUBSCRIPTION, "menu:sub")]))
    elif pay.status in ("canceled", "refunded"):
        await show(cb, ctx.texts.render("pay_canceled"), kb(btn(labels.BUY, "pay:menu"), btn(labels.BACK, "menu:sub")))
    elif pay.status == "needs_review":
        await toast(cb, ctx.texts.render("pay_review"), alert=True)
    else:
        await toast(cb, ctx.texts.render("pay_pending"), alert=True)


# ───────────────────────── email для чека ─────────────────────────
@router.message(UserFlow.email, F.text, NotCommand())
async def email_input(message: Message, ctx: AppContext, user: User, state: FSMContext) -> None:
    email = (message.text or "").strip()
    if len(email) > 200 or not EMAIL_RE.match(email):
        await message.answer(ctx.texts.render("email_bad"))
        return
    data = await state.get_data()
    await state.clear()
    # пользователь сам ввёл адрес после уведомления об обработке — фиксируем согласие
    await ctx.users.set_email(user.id, email.lower(), consented=True)
    fresh = await _fresh(ctx, user)
    if data.get("devices"):
        from app.bot.handlers.user.devices import start_devices_payment

        await start_devices_payment(message, ctx, fresh, state, int(data["devices"]), str(data.get("option", "yk")))
        return
    await start_payment(
        message,
        ctx,
        fresh,
        state,
        str(data.get("code", "lite")),
        int(data.get("months") or 1),
        bool(data.get("upgrade")),
        str(data.get("option", "yk")),
        autopay_consent=bool(data.get("autopay_consent")),
    )


@router.message(UserFlow.email)
async def email_other(message: Message, ctx: AppContext) -> None:
    await message.answer(ctx.texts.render("email_bad"))
