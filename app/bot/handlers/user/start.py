from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import labels, views
from app.bot.ui import btn, kb, show, toast
from app.db.models import User
from app.legal import CONSENT_VERSION
from app.services.subscriptions import TrialError
from app.utils import esc, fmt_dt, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)
router = Router(name="user.start")


async def send_home(event: Message | CallbackQuery, ctx: AppContext, user: User) -> None:
    """Первый запуск — приветствие с документами и кнопкой «Подключить», иначе — главное меню."""
    user = await ctx.users.get(user.id) or user
    now = utcnow()
    first_launch = (
        user.consent_at is None
        and not user.remna_id
        and not user.trial_used
        and not ctx.subs.view(user, now).is_active(now)
    )
    if first_launch:
        text, markup = views.welcome_screen(ctx, user)
        await show(event, text, markup)
        return
    text, markup = views.main_menu(ctx, user)
    await show(event, text, markup)


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    ctx: AppContext,
    user: User,
    is_new_user: bool,
) -> None:
    await state.clear()
    if is_new_user and command.args and command.args.startswith("ref_"):
        await ctx.refs.register(user.id, command.args[4:])
    await send_home(message, ctx, user)


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext, ctx: AppContext, user: User) -> None:
    await state.clear()
    await send_home(message, ctx, user)


@router.callback_query(F.data == "menu:main")
async def cb_main(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    await send_home(cb, ctx, user)


async def accept_terms(ctx: AppContext, user: User, source: str) -> None:
    """Фиксирует согласие с документами (время и версия) — доказательство акцепта оферты и политики."""
    if user.consent_at is None:
        await ctx.subs.record_consent(user.id, CONSENT_VERSION)
        await ctx.audit.log(user.id, "consent.accepted", user.id, version=CONSENT_VERSION, source=source)


async def channel_gate(ctx: AppContext, user_id: int) -> tuple[str, str]:
    """('ok' | 'no' | 'error', название_канала).

    При сбое проверки (бот не админ канала, Telegram недоступен) доступ НЕ выдаётся автоматически —
    иначе при любой ошибке канал-гейт пропускал бы всех. Владельцы получают предупреждение.
    """
    channel = (ctx.cfg.get("trial_channel") or "").strip()
    if not channel:
        return "ok", ""
    chat: int | str = int(channel) if channel.lstrip("-").isdigit() else (channel if channel.startswith("@") else "@" + channel)
    try:
        member = await ctx.bot.get_chat_member(chat, user_id)
    except TelegramAPIError as e:
        log.warning("проверка подписки на канал %s не удалась (бот должен быть админом канала): %s", channel, e)
        await ctx.notify.alert_once(
            "gate_channel_error",
            f"⚠️ <b>Не удаётся проверить подписку на канал</b> {esc(channel)}: <code>{esc(e)}</code>\n"
            "Пробный период временно не выдаётся. Проверьте, что бот — администратор канала.",
        )
        return "error", channel
    ok = member.status in ("member", "administrator", "creator") or (
        member.status == "restricted" and getattr(member, "is_member", False)
    )
    return ("ok" if ok else "no"), channel


async def _show_gate(cb: CallbackQuery, ctx: AppContext, status: str, channel: str, checking: bool) -> None:
    link = f"https://t.me/{channel.lstrip('@')}" if not channel.lstrip("-").isdigit() else None
    if status == "error":
        await show(cb, ctx.texts.render("gate_error"), kb(btn(labels.RETRY, "connect:chk"), btn(labels.BACK, "menu:main")))
        return
    text = ctx.texts.render("gate_channel", channel=esc(channel))
    if checking:
        text += "\n\n" + ctx.texts.render("gate_failed")
    await show(cb, text, kb(btn("Подписаться", url=link) if link else None, btn("Проверить", "connect:chk")))


@router.callback_query(F.data == "connect")
@router.callback_query(F.data == "connect:chk")
async def cb_connect(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    # нажатие «Подключить» = согласие с документами с первого экрана
    await accept_terms(ctx, user, "connect")

    if user.trial_used or not ctx.cfg.get("trial_enabled"):
        # пробного периода нет (отключён или уже использован) — ведём к выбору тарифа / в меню
        fresh = await ctx.users.get(user.id) or user
        if not user.trial_used and not ctx.cfg.get("trial_enabled"):
            text, markup = views.pay_menu(ctx, fresh)
            await show(cb, text, markup)
        else:
            await send_home(cb, ctx, fresh)
        return

    status, channel = await channel_gate(ctx, user.id)
    if status != "ok":
        await _show_gate(cb, ctx, status, channel, checking=cb.data == "connect:chk")
        return
    try:
        new = await ctx.subs.activate_trial(user.id)
    except TrialError as e:
        if e.reason == "unavailable":
            await show(cb, ctx.texts.render("connect_unavailable"), kb(btn(labels.RETRY, "connect"), btn(labels.BACK, "menu:main")))
            return
        await send_home(cb, ctx, user)  # уже использован / уже есть подписка
        return

    user = await ctx.users.get(user.id) or user
    await ctx.audit.log(user.id, "trial.activated", user.id)
    if not user.short_uuid or not user.sub_url:
        # панель сейчас недоступна: пробный период уже записан у нас, воркер доведёт настройку
        text = ctx.texts.render("connect_pending", tier=new.tier.title, until=fmt_dt(new.total_end))
        await show(cb, text, kb(btn(labels.RETRY, "setup:retry"), btn(labels.HOME, "menu:main")))
        return
    limits_line = f"\nПробный период — {views.trial_limits_text(ctx).lower()}" if user.trial_limited else ""
    text = ctx.texts.render(
        "connected",
        tier=new.tier.title,
        until=fmt_dt(new.total_end),
        devices=ctx.subs.device_limit_for(user, new),
        limits_line=limits_line,
    )
    markup = kb(
        btn("Скачать Happ", url=ctx.dl_url()) if ctx.web_ready else None,
        btn("Добавить подписку", url=ctx.add_url(user.short_uuid)) if ctx.web_ready else None,
        views.support_button(ctx),
        btn(labels.HOME, "menu:main"),
    )
    await show(cb, text, markup)


@router.callback_query(F.data.startswith("legal:ok:"))
async def cb_legal_ok(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    """«Принимаю и продолжить» на экране согласия (для тех, кто доходит до оплаты без приветствия)."""
    await accept_terms(ctx, user, "gate")
    target = (cb.data or "").split(":", 2)[2]
    fresh = await ctx.users.get(user.id) or user
    if target == "pay":
        text, markup = views.pay_menu(ctx, fresh)
    else:
        text, markup = views.main_menu(ctx, fresh)
    await show(cb, text, markup)


@router.callback_query(F.data == "setup:retry")
async def cb_setup_retry(cb: CallbackQuery, ctx: AppContext, user: User) -> None:
    """«Повторить» на экране «настраиваем доступ»: пробуем довести синхронизацию с панелью прямо сейчас."""
    try:
        fresh_ok = bool(await ctx.redis.set(f"ampire:retry:{user.id}", 1, ex=4, nx=True))
    except Exception:  # noqa: BLE001 — Redis недоступен: не блокируем кнопку
        fresh_ok = True
    if not fresh_ok:
        await toast(cb, "Уже проверяем — повторите через несколько секунд")
        return
    try:
        await ctx.subs.sync_now(user.id)
    except Exception:  # noqa: BLE001
        log.exception("setup:retry %s", user.id)
    fresh = await ctx.users.get(user.id) or user
    text, markup = views.setup_screen(ctx, fresh)
    await show(cb, text, markup)


# ───────────────────────── поддержка ─────────────────────────
@router.callback_query(F.data == "sup:menu")
async def cb_support(cb: CallbackQuery, ctx: AppContext) -> None:
    text, markup = views.support_screen(ctx)
    await show(cb, text, markup)


@router.callback_query(F.data == "sup:docs")
async def cb_docs(cb: CallbackQuery, ctx: AppContext) -> None:
    text, markup = views.docs_screen(ctx)
    await show(cb, text, markup)


@router.callback_query(F.data == "sup:none")
async def cb_no_support(cb: CallbackQuery, ctx: AppContext) -> None:
    await toast(cb, ctx.texts.render("support_no_chat"), alert=True)


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery) -> None:
    await toast(cb, "")


# Любой другой текст вне сценариев — показываем меню
@router.message(StateFilter(None), F.text)
async def fallback(message: Message, ctx: AppContext, user: User) -> None:
    text, markup = views.main_menu(ctx, await ctx.users.get(user.id) or user)
    await message.answer(ctx.texts.render("unknown_input"))
    await message.answer(text, reply_markup=markup)
