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
from app.db.models import User
from app.integrations.remnawave import RemnaError
from app.services.payments import NeedEmail, PaymentError
from app.utils import devices_word, esc, money, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)
router = Router(name="user.devices")


async def _fresh(ctx: AppContext, user: User) -> User:
    return await ctx.users.get(user.id) or user


def _key(cb: CallbackQuery) -> str:
    parts = (cb.data or "").split(":")
    return parts[2] if len(parts) > 2 else ""


def _count(data: str | None, idx: int = 2) -> int:
    try:
        return int((data or "").split(":")[idx])
    except (IndexError, ValueError):
        return 1


@router.callback_query(F.data == "setup:open")
async def cb_setup(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    text, markup = views.setup_screen(ctx, await _fresh(ctx, user))
    await show(cb, text, markup)


@router.callback_query(F.data == "dev:list")
async def cb_list(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    text, markup = await views.devices_screen(ctx, await _fresh(ctx, user))
    await show(cb, text, markup)


@router.callback_query(F.data.startswith("dev:v:"))
async def cb_view(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    user = await _fresh(ctx, user)
    try:
        dev = await ctx.devices.find(user, _key(cb))
    except RemnaError:
        await toast(cb, "Не удалось получить данные. Попробуйте ещё раз.", alert=True)
        return
    if dev is None:
        text, markup = await views.devices_screen(ctx, user)
        await show(cb, text, markup)
        return
    text = ctx.texts.render("device_card", label=esc(dev.label), info=dev.info())
    markup = kb(
        [btn(labels.RENAME, f"dev:rn:{dev.key}"), btn(labels.DELETE, f"dev:del:{dev.key}")],
        btn(labels.BACK, "dev:list"),
    )
    await show(cb, text, markup)


@router.callback_query(F.data.startswith("dev:del:"))
async def cb_delete_ask(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    user = await _fresh(ctx, user)
    try:
        dev = await ctx.devices.find(user, _key(cb))
    except RemnaError:
        await toast(cb, "Не удалось получить данные. Попробуйте ещё раз.", alert=True)
        return
    if dev is None:
        text, markup = await views.devices_screen(ctx, user)
        await show(cb, text, markup)
        return
    text = ctx.texts.render("device_delete_confirm", label=esc(dev.label))
    await show(cb, text, kb(btn("Да, удалить", f"dev:ok:{dev.key}"), btn(labels.CANCEL, f"dev:v:{dev.key}")))


@router.callback_query(F.data.startswith("dev:ok:"))
async def cb_delete(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    user = await _fresh(ctx, user)
    try:
        await ctx.devices.delete(user, _key(cb))
    except RemnaError:
        await toast(cb, "Не удалось удалить устройство. Попробуйте ещё раз.", alert=True)
        return
    await show(cb, ctx.texts.render("device_deleted"), kb(btn(labels.DEVICES, "dev:list")))


@router.callback_query(F.data.startswith("dev:rn:"))
async def cb_rename_ask(cb: CallbackQuery, ctx: AppContext, user: User, state: FSMContext) -> None:
    user = await _fresh(ctx, user)
    try:
        dev = await ctx.devices.find(user, _key(cb))
    except RemnaError:
        await toast(cb, "Не удалось получить данные. Попробуйте ещё раз.", alert=True)
        return
    if dev is None:
        await toast(cb, "Устройство не найдено", alert=True)
        return
    await state.set_state(UserFlow.device_name)
    await state.update_data(key=dev.key)
    await show(cb, ctx.texts.render("device_rename_ask", label=esc(dev.label)), kb(btn(labels.BACK, f"dev:v:{dev.key}")))


@router.message(UserFlow.device_name, F.text, NotCommand())
async def rename_input(message: Message, ctx: AppContext, user: User, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 24:
        await message.answer("Название должно быть от 1 до 24 символов. Попробуйте ещё раз.")
        return
    data = await state.get_data()
    await state.clear()
    user = await _fresh(ctx, user)
    try:
        await ctx.devices.rename(user, str(data.get("key", "")), name)
    except RemnaError:
        await message.answer(ctx.texts.render("panel_unavailable"))
        return
    text, markup = await views.devices_screen(ctx, user)
    await show(message, text, markup)


@router.callback_query(F.data == "dev:add")
async def cb_add(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    """Единая точка входа «Подключить устройство»: есть свободное место — инструкция, нет — выбор (докупить / удалить / PREMIUM)."""
    user = await _fresh(ctx, user)
    now = utcnow()
    if not ctx.subs.view(user, now).is_active(now) or not user.remna_id:
        text, markup = await views.devices_screen(ctx, user)
        await show(cb, text, markup)
        return
    try:
        used = len(await ctx.devices.list(user))
    except RemnaError:
        await toast(cb, "Не удалось получить данные. Попробуйте ещё раз.", alert=True)
        return
    if used < ctx.devices.limit_for(user):
        text, markup = views.setup_screen(ctx, user, back_cb="dev:list")
    else:
        text, markup = await views.device_limit_screen(ctx, user)
    await show(cb, text, markup)


# ───────────────────────── дополнительные устройства ─────────────────────────
@router.callback_query(F.data.startswith("dv:buy:"))
async def cb_buy(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    """Выбор числа дополнительных устройств: [−] N [+], цена пересчитывается на лету."""
    text, markup = await views.device_buy_screen(ctx, await _fresh(ctx, user), _count(cb.data))
    await show(cb, text, markup)


async def start_devices_payment(
    event: Message | CallbackQuery, ctx: AppContext, user: User, state: FSMContext, count: int, option: str
) -> None:
    if option not in views.PAY_OPTIONS or option == "yka":
        if isinstance(event, CallbackQuery):
            await toast(event, "Этот способ оплаты недоступен", alert=True)
        return
    provider, _save = views.PAY_OPTIONS[option]
    try:
        pay = await ctx.pay.create_devices(user.id, count, provider=provider)
    except NeedEmail:
        await state.set_state(UserFlow.email)
        await state.update_data(devices=count, option=option)
        await show(
            event,
            ctx.texts.render("email_ask", privacy_url=views.attr(ctx.cfg.legal_url("privacy"))),
            kb(btn(labels.BACK, f"dv:buy:{count}")),
        )
        return
    except PaymentError as e:
        if e.reason == "need_consent":
            text, markup = views.legal_gate_screen(ctx, "pay")
        elif e.reason in ("devices_unavailable", "devices_limit"):
            text, markup = await views.device_buy_screen(ctx, user, count)
        else:
            log.warning("оплата устройств недоступна для %s: %s", user.id, e)
            text, markup = ctx.texts.render("pay_unavailable"), kb(btn(labels.BACK, "dev:list"))
        await show(event, text, markup)
        return

    what = f"Дополнительно {devices_word(count)}"
    if pay.provider == "cryptobot":
        text = ctx.texts.render("pay_created_crypto", what=what, amount=money(pay.amount), discount_line="")
        pay_label = "Оплатить в CryptoBot"
    else:
        text = ctx.texts.render("pay_created_devices", what=what, amount=money(pay.amount))
        pay_label = labels.GO_PAY
    markup = kb(
        btn(pay_label, url=pay.confirmation_url) if pay.confirmation_url else None,
        btn(labels.CHECK_PAYMENT, f"pay:chk:{pay.id}"),
        btn(labels.BACK, "dev:list"),
    )
    await show(event, text, markup)


@router.callback_query(F.data.startswith("dv:go:"))
async def cb_buy_go(cb: CallbackQuery, ctx: AppContext, user: User, state: FSMContext) -> None:
    user = await _fresh(ctx, user)
    count = _count(cb.data)
    options = views.payment_options(ctx, autopay=False)
    if not options:
        await show(cb, ctx.texts.render("pay_unavailable"), kb(btn(labels.BACK, "dev:list")))
        return
    if len(options) == 1:
        await start_devices_payment(cb, ctx, user, state, count, options[0])
        return
    text, markup = await views.device_pay_method_screen(ctx, user, count)
    await show(cb, text, markup)


@router.callback_query(F.data.startswith("dv:m:"))
async def cb_buy_method(cb: CallbackQuery, ctx: AppContext, user: User, state: FSMContext) -> None:
    parts = (cb.data or "").split(":")  # dv m <option> <count>
    if len(parts) < 4:
        await toast(cb, "")
        return
    await start_devices_payment(cb, ctx, await _fresh(ctx, user), state, _count(cb.data, 3), parts[2])


async def _less_screen(ctx: AppContext, user: User) -> tuple[str, object]:
    extra = int(user.extra_devices or 0)
    if extra <= 0:
        return ctx.texts.render("devices_empty"), kb(btn(labels.BACK, "dev:list"))
    limit_after = ctx.devices.limit_for(user) - 1
    devices = await ctx.devices.list(user)
    if len(devices) > limit_after:
        rows = [btn(f"{labels.DELETE}: {d.label}", f"dev:del:{d.key}") for d in devices]
        text = ctx.texts.render("device_less_blocked", used=len(devices), limit_after=limit_after)
        return text, kb(*rows, btn(labels.BACK, "dev:list"))
    text = ctx.texts.render(
        "device_less_confirm", extra_after=extra - 1, limit_after=limit_after, unit_price=money(ctx.cfg.device_price())
    )
    return text, kb(btn(labels.LESS_DEVICES, "dv:less:ok"), btn(labels.CANCEL, "dev:list"))


@router.callback_query(F.data == "dv:less")
async def cb_less(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    user = await _fresh(ctx, user)
    try:
        text, markup = await _less_screen(ctx, user)
    except RemnaError:
        await toast(cb, "Не удалось получить данные. Попробуйте ещё раз.", alert=True)
        return
    await show(cb, text, markup)


@router.callback_query(F.data == "dv:less:ok")
async def cb_less_ok(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    """Уменьшаем число доп. устройств на одно — только если после этого подключённые устройства помещаются в лимит."""
    user = await _fresh(ctx, user)
    try:
        text, markup = await _less_screen(ctx, user)
    except RemnaError:
        await toast(cb, "Не удалось получить данные. Попробуйте ещё раз.", alert=True)
        return
    if "dv:less:ok" not in str(markup):  # экран «сначала удалите устройство» или нечего уменьшать
        await show(cb, text, markup)
        return
    await ctx.subs.change_extra_devices(user.id, -1, kind="devices_remove", actor_id=user.id)
    fresh = await _fresh(ctx, user)
    await show(
        cb,
        ctx.texts.render("device_less_done", limit=ctx.devices.limit_for(fresh)),
        kb(btn(labels.DEVICES, "dev:list")),
    )
