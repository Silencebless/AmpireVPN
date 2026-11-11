from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot.handlers.admin.common import ask, make_router, need_owner, need_scope, on_input
from app.bot.ui import btn, confirm_kb, kb, nav_row, page_info, show, toast
from app.core.subscription import SubState, Tier
from app.db.models import Payment, User
from app.integrations.remnawave import RemnaError, usage_of
from app.services.users import ADMIN_FILTERS
from app.utils import (
    days_left, days_word, esc, fmt_date, fmt_dt, human_bytes, money, parse_int, parse_iso, truncate, utcnow,
)

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)
router = make_router("users", scope="users.view")
PAGE = 8


def user_label(ctx: AppContext, u: User) -> str:
    now = utcnow()
    st = ctx.subs.view(u, now)
    if u.is_banned:
        icon = "🚫"
    elif st.is_active(now):
        icon = "🟢"
    elif u.remna_id or u.trial_used:
        icon = "🔴"
    else:
        icon = "⚪️"
    tail = f" · {st.tier.title} до {fmt_date(st.expires_at)[:5]}" if st.is_active(now) else ""
    return f"{icon} {truncate(u.display_name, 18)}{tail}"


# ───────────────────────── меню, списки, поиск ─────────────────────────
@router.callback_query(F.data == "a:u")
async def cb_menu(cb: CallbackQuery, ctx: AppContext) -> None:
    counts = await asyncio.gather(*(ctx.users.count(k) for k in ADMIN_FILTERS))
    items = [btn(f"{title} · {n}", f"a:ul:{key}:0") for (key, title), n in zip(ADMIN_FILTERS.items(), counts)]
    rows = [[btn("🔎 Найти пользователя", "a:us")]] + [items[i : i + 2] for i in range(0, len(items), 2)]
    rows.append([btn("← Назад", "a:home")])
    text = "👥 <b>Пользователи</b>\n\nИщи по ID, @username, имени, email или ссылке подписки — либо открой список по фильтру."
    await show(cb, text, kb(*rows))


@router.callback_query(F.data.startswith("a:ul:"))
async def cb_list(cb: CallbackQuery, ctx: AppContext) -> None:
    _, _, flt, page_s = (cb.data or "").split(":")
    if flt not in ADMIN_FILTERS:
        flt = "all"
    users, total = await ctx.users.page(flt, int(page_s), PAGE)
    page, pages = page_info(total, int(page_s), PAGE)
    if page != int(page_s):
        users, _ = await ctx.users.page(flt, page, PAGE)
    rows = [btn(user_label(ctx, u), f"a:u:{u.id}") for u in users]
    rows.append(nav_row(page, pages, lambda p: f"a:ul:{flt}:{p}"))
    rows.append(btn("← Назад", "a:u"))
    await show(cb, f"👥 <b>{ADMIN_FILTERS[flt]}</b> — {total}", kb(*rows))


@router.callback_query(F.data == "a:us")
async def cb_search(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "u_search", "🔎 Отправь ID, @username, имя, email, код приглашения или shortUuid.", "a:u")


@on_input("u_search", "users.view")
async def in_search(message: Message, state: FSMContext, ctx: AppContext, data: dict) -> None:
    found = await ctx.users.find(message.text or "")
    if not found:
        await message.answer("Ничего не найдено. Попробуй другой запрос.", reply_markup=kb(btn("Отмена", "a:u")))
        return
    await state.clear()
    if len(found) == 1:
        text, markup = await user_card(ctx, found[0].id, viewer=message.from_user.id)
        await show(message, text, markup)
        return
    rows = [btn(user_label(ctx, u), f"a:u:{u.id}") for u in found]
    rows.append(btn("← Назад", "a:u"))
    await show(message, f"Найдено: {len(found)}", kb(*rows))


# ───────────────────────── карточка ─────────────────────────
async def user_card(ctx: AppContext, uid: int, flash: str = "", viewer: int | None = None):
    u = await ctx.users.get(uid)
    if u is None:
        return "Пользователь не найден.", kb(btn("← Назад", "a:u"))
    now = utcnow()
    st = ctx.subs.view(u, now)

    panel: dict | None = None
    devices_n: int | None = None
    if u.remna_id:
        res = await asyncio.gather(
            asyncio.wait_for(ctx.remna.get_user(u.remna_id), 5),
            asyncio.wait_for(ctx.remna.list_devices(u.remna_id), 5),
            return_exceptions=True,
        )
        panel = res[0] if isinstance(res[0], dict) else None
        devices_n = len(res[1]) if isinstance(res[1], list) else None

    async with ctx.session() as s:
        pay_n, pay_sum = (
            await s.execute(
                select(func.count(), func.coalesce(func.sum(Payment.amount), 0)).where(
                    Payment.user_id == uid, Payment.status == "succeeded"
                )
            )
        ).one()
    refs = await ctx.refs.stats(uid)
    referrer = await ctx.users.get(u.referrer_id) if u.referrer_id else None

    if st.is_active(now):
        sub = f"🟢 <b>{st.tier.title}</b> до {fmt_dt(st.expires_at)} (ещё {days_word(days_left(st.expires_at, now))})"
        if st.queued_lite.total_seconds() > 0:
            sub += f"\n    ↳ затем LITE {days_word(round(st.queued_lite.total_seconds() / 86400))} (до {fmt_dt(st.total_end)})"
    elif u.remna_id or u.trial_used:
        sub = f"🔴 закончилась {fmt_dt(st.expires_at)}"
    else:
        sub = "⚪️ подписки нет"

    limit = ctx.devices.limit_for(u)
    extra_note = f" (+{u.extra_devices} доп.)" if u.extra_devices else ""
    lines = [
        f"👤 <b>{esc(u.display_name)}</b>" + (f" · @{esc(u.username)}" if u.username else ""),
        f"ID: <code>{u.id}</code> · с {fmt_date(u.created_at)}",
        f"Подписка: {sub}",
        f"Платежей: {pay_n} на {money(pay_sum)} · Пробный: {'использован ' + fmt_date(u.trial_at) if u.trial_used else 'нет'}",
    ]
    if u.remna_id:
        usage = usage_of(panel) if panel else None
        online = fmt_dt(parse_iso(usage["online_at"])) if usage and usage["online_at"] else "—"
        traffic = human_bytes(usage["lifetime"] or usage["used"]) if usage else "—"
        lines.append(
            f"Устройства: {'—' if devices_n is None else devices_n}/{limit}{extra_note} · Трафик: {traffic} · Онлайн: {online}"
        )
        lines.append(
            f"Панель: id {u.remna_id} · <code>{esc(u.remna_username or '')}</code>"
            + (f" · {esc(panel.get('status', ''))}" if panel else " · ⚠️ нет ответа")
        )
    if u.sync_pending:
        lines.append(f"⚠️ <b>Не синхронизирован с панелью</b>: {esc(truncate(u.sync_error or '', 120))}")
    if referrer:
        lines.append(f"Пригласил: {esc(referrer.display_name)} (<code>{referrer.id}</code>)")
    if refs["invited"]:
        lines.append(f"Привёл: {refs['invited']} (подключились {refs['connected']}, +{days_word(refs['days'])})")
    if u.trial_limited:
        lines.append("🧪 Пробный период с ограничениями (трафик/устройства)")
    if u.consent_at:
        lines.append(f"Согласие с документами: {fmt_date(u.consent_at)} (ред. {esc(u.consent_version or '—')})")
    else:
        lines.append("⚠️ Согласие с документами не зафиксировано")
    if u.autopay:
        lines.append(f"🔁 Автопродление: включено · {u.autopay_months} мес. (неудач подряд: {u.autopay_failures})")
    if u.email:
        lines.append(f"Email: {esc(u.email)}")
    if u.is_banned:
        lines.append(f"🚫 <b>Бан</b>{': ' + esc(u.ban_reason) if u.ban_reason else ''}")
    if u.bot_blocked:
        lines.append("🔕 Заблокировал бота")
    if u.notes:
        lines.append(f"📝 {esc(u.notes)}")
    text = (f"{flash}\n\n" if flash else "") + "\n".join(lines)

    p = f"a:u:{uid}:"
    can = (lambda scope: True) if viewer is None else (lambda scope: ctx.has_scope(viewer, scope))  # noqa: E731

    def row(*pairs: tuple[str, str, str]) -> list:
        return [btn(t, cb) for t, cb, scope in pairs if can(scope)]

    markup = kb(
        row(("➕ Дни", p + "add", "subs.grant"), ("➖ Дни", p + "rem", "subs.grant")),
        row(("🎚 Тариф и срок", p + "set", "subs.grant"), ("➕ Доп. устройства", p + "xdev", "subs.grant")),
        row(("📱 Устройства", p + "dev", "users.manage"), ("💳 Платежи", p + "pays", "payments.view")),
        row(("👥 Рефералы", p + "refs", "users.view"), ("🔄 Синхронизировать", p + "sync", "users.manage")),
        row(("🔗 Новая ссылка", p + "rev", "users.manage"), ("📈 Сброс трафика", p + "trf", "users.manage")),
        row(("🎁 Сброс пробного", p + "trial", "users.manage"), ("✉️ Написать", p + "msg", "users.manage")),
        row(("📝 Заметка", p + "note", "users.manage"),
            ("✅ Разбанить", p + "unban", "users.manage") if u.is_banned else ("🚫 Бан", p + "ban", "users.manage")),
        row(("🗑 Удалить", p + "del", "users.delete")),
        row(("🔄 Обновить", p + "card", "users.view"), ("← К списку", "a:ul:all:0", "users.view")),
    )
    return text, markup


async def _card(event: CallbackQuery | Message, ctx: AppContext, uid: int, flash: str = "") -> None:
    viewer = event.from_user.id if event.from_user else None
    text, markup = await user_card(ctx, uid, flash, viewer)
    await show(event, text, markup)


async def _after_change(ctx: AppContext, uid: int, old: SubState, new: SubState) -> None:
    """Если PREMIUM сменился на LITE — отключаем лишние устройства."""
    if old.tier == Tier.PREMIUM and new.tier == Tier.LITE and ctx.cfg.get("trim_devices_on_downgrade"):
        await ctx.devices.trim_to_limit(uid)


def _tier_pick(uid: int, action: str, back: str, *, none_btn: bool = False):
    rows = [[btn("LITE", f"a:u:{uid}:{action}:lite"), btn("PREMIUM", f"a:u:{uid}:{action}:premium")]]
    if none_btn:
        rows.append([btn("Без подписки", f"a:u:{uid}:setn")])
    rows.append([btn("← Назад", back)])
    return kb(*rows)


# какое право нужно для каждого действия в карточке (роль «поддержка» не может выдавать дни и т. п.)
OP_SCOPES: dict[str, str] = {
    "card": "users.view", "refs": "users.view", "pays": "payments.view",
    "add": "subs.grant", "rem": "subs.grant", "addt": "subs.grant", "remt": "subs.grant",
    "set": "subs.grant", "sett": "subs.grant", "setn": "subs.grant", "setnok": "subs.grant", "xdev": "subs.grant",
    "dev": "users.manage", "devd": "users.manage", "deva": "users.manage", "devaok": "users.manage",
    "sync": "users.manage", "rev": "users.manage", "revok": "users.manage", "trf": "users.manage",
    "trial": "users.manage", "msg": "users.manage", "note": "users.manage", "ban": "users.manage", "unban": "users.manage",
    "del": "users.delete", "delok": "users.delete",
}


# ───────────────────────── действия ─────────────────────────
@router.callback_query(F.data.startswith("a:u:"))
async def cb_user(cb: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    parts = (cb.data or "").split(":")
    uid = int(parts[2])
    op = parts[3] if len(parts) > 3 else "card"
    arg = parts[4] if len(parts) > 4 else ""
    actor = cb.from_user.id
    back = f"a:u:{uid}:card"
    if await need_scope(cb, ctx, OP_SCOPES.get(op, "users.manage")):
        return

    if op == "card":
        await _card(cb, ctx, uid)

    elif op in ("add", "rem"):
        verb = "начислить" if op == "add" else "снять"
        await show(cb, f"На каком тарифе {verb} дни?", _tier_pick(uid, op + "t", back))

    elif op in ("addt", "remt"):
        sign = 1 if op == "addt" else -1
        verb = "начислить" if sign > 0 else "снять"
        await ask(cb, state, "u_days", f"Сколько дней {verb} ({arg.upper()})? Отправь число.", back,
                  uid=uid, tier=arg, sign=sign)

    elif op == "set":
        await show(cb, "Задать тариф и срок. Выбери тариф:", _tier_pick(uid, "sett", back, none_btn=True))

    elif op == "sett":
        await ask(cb, state, "u_set", f"На сколько дней от сегодня установить {arg.upper()}? Отправь число.", back,
                  uid=uid, tier=arg)

    elif op == "setn":
        await show(cb, "Полностью снять подписку у пользователя?", confirm_kb(f"a:u:{uid}:setnok", back))

    elif op == "setnok":
        old, new = await ctx.subs.set_exact(uid, None, 0, actor_id=actor)
        await ctx.audit.log(actor, "user.clear_sub", uid)
        await _card(cb, ctx, uid, "✅ Подписка снята")

    elif op == "xdev":
        u = await ctx.users.get(uid)
        cur = int(u.extra_devices or 0) if u else 0
        mx = int(ctx.cfg.get("max_extra_devices") or 0)
        await ask(cb, state, "u_xdev", f"Сколько дополнительных устройств должно быть у пользователя (сейчас {cur}, максимум {mx})? Отправь число.", back, uid=uid)

    elif op == "dev":
        await _devices(cb, ctx, uid)

    elif op == "devd":
        u = await ctx.users.get(uid)
        try:
            if u:
                await ctx.devices.delete(u, arg)
        except RemnaError as e:
            await toast(cb, f"Ошибка панели: {e}"[:190], alert=True)
            return
        await ctx.audit.log(actor, "user.device_delete", uid)
        await _devices(cb, ctx, uid)

    elif op == "deva":
        await show(cb, "Удалить ВСЕ устройства пользователя?", confirm_kb(f"a:u:{uid}:devaok", f"a:u:{uid}:dev"))

    elif op == "devaok":
        u = await ctx.users.get(uid)
        try:
            if u:
                await ctx.devices.delete_all(u)
        except RemnaError as e:
            await toast(cb, f"Ошибка панели: {e}"[:190], alert=True)
            return
        await ctx.audit.log(actor, "user.devices_reset", uid)
        await _devices(cb, ctx, uid)

    elif op == "pays":
        pays, total = await ctx.pay.page(None, 0, 10, user_id=uid)
        rows = [
            btn(f"#{p.id} · {money(p.amount)} · {p.plan_code.upper()} · {_pay_icon(p.status)} {fmt_date(p.created_at)[:5]}", f"a:pv:{p.id}")
            for p in pays
        ]
        rows.append(btn("← Назад", back))
        await show(cb, f"💳 Платежи пользователя <code>{uid}</code> — {total}", kb(*rows))

    elif op == "refs":
        await _refs(cb, ctx, uid)

    elif op == "sync":
        ok = await ctx.subs.sync_now(uid)
        await _card(cb, ctx, uid, "✅ Синхронизировано с панелью" if ok else "❌ Панель не ответила — попробуем позже")

    elif op == "rev":
        await show(cb, "Выпустить новую ссылку подписки? Старая перестанет работать, устройствам придётся добавить подписку заново.",
                   confirm_kb(f"a:u:{uid}:revok", back))

    elif op == "revok":
        try:
            await ctx.subs.revoke_link(uid)
        except (RemnaError, LookupError) as e:
            await toast(cb, f"Не удалось: {e}"[:190], alert=True)
            return
        await ctx.audit.log(actor, "user.revoke_link", uid)
        await _card(cb, ctx, uid, "✅ Новая ссылка выпущена")

    elif op == "trf":
        u = await ctx.users.get(uid)
        if not u or not u.remna_id:
            await toast(cb, "У пользователя нет учётной записи в панели", alert=True)
            return
        try:
            await ctx.remna.reset_traffic(u.remna_id)
        except RemnaError as e:
            await toast(cb, f"Ошибка панели: {e}"[:190], alert=True)
            return
        await ctx.audit.log(actor, "user.reset_traffic", uid)
        await _card(cb, ctx, uid, "✅ Трафик сброшен")

    elif op == "trial":
        await ctx.users.reset_trial(uid)
        await ctx.audit.log(actor, "user.reset_trial", uid)
        await _card(cb, ctx, uid, "✅ Пробный период сброшен — пользователь сможет взять его снова")

    elif op == "msg":
        await ask(cb, state, "u_msg", "✉️ Отправь текст сообщения — оно придёт пользователю от имени бота.", back, uid=uid)

    elif op == "note":
        await ask(cb, state, "u_note", "📝 Отправь заметку о пользователе (или «-», чтобы очистить).", back, uid=uid)

    elif op == "ban":
        await ask(cb, state, "u_ban", "🚫 Укажи причину бана (или «-» без причины).", back, uid=uid)

    elif op == "unban":
        await ctx.users.set_banned(uid, False)
        await ctx.audit.log(actor, "user.unban", uid)
        await _card(cb, ctx, uid, "✅ Пользователь разбанен")

    elif op == "del":
        if await need_owner(cb, ctx):
            return
        await show(cb, "⚠️ Удалить пользователя полностью (бот + панель VPN)? Это необратимо. Платежи сохранятся для учёта.",
                   confirm_kb(f"a:u:{uid}:delok", back, yes_text="Да, удалить навсегда"))

    elif op == "delok":
        if await need_owner(cb, ctx):
            return
        try:
            await ctx.users.delete(uid)
        except RemnaError as e:
            await toast(cb, f"Панель не дала удалить: {e}"[:190], alert=True)
            return
        await ctx.audit.log(actor, "user.delete", uid)
        await show(cb, f"🗑 Пользователь <code>{uid}</code> удалён.", kb(btn("← К списку", "a:ul:all:0")))

    else:
        await toast(cb, "Неизвестное действие")


def _pay_icon(status: str) -> str:
    return {"succeeded": "✅", "pending": "⏳", "canceled": "❌", "refunded": "↩️", "refunding": "↩️", "needs_review": "⚠️"}.get(status, "•")


async def _devices(cb: CallbackQuery, ctx: AppContext, uid: int) -> None:
    u = await ctx.users.get(uid)
    back = f"a:u:{uid}:card"
    if u is None or not u.remna_id:
        await show(cb, "У пользователя нет учётной записи в панели.", kb(btn("← Назад", back)))
        return
    try:
        devices = await ctx.devices.list(u)
    except RemnaError as e:
        await show(cb, f"⚠️ Панель не ответила: {esc(str(e))[:200]}", kb(btn("← Назад", back)))
        return
    limit = ctx.devices.limit_for(u)
    lines = "\n".join(f"{i}. {esc(d.button)} · {fmt_date(d.created_at)}" for i, d in enumerate(devices, 1)) or "Нет устройств."
    rows = [btn(f"🗑 {d.button}", f"a:u:{uid}:devd:{d.key}") for d in devices]
    if devices:
        rows.append(btn("🗑 Удалить все", f"a:u:{uid}:deva"))
    rows.append(btn("← Назад", back))
    await show(cb, f"📱 <b>Устройства</b> ({len(devices)}/{limit})\n\n{lines}", kb(*rows))


async def _refs(cb: CallbackQuery, ctx: AppContext, uid: int) -> None:
    u = await ctx.users.get(uid)
    children = await ctx.refs.children(uid, 20)
    lines = []
    if u and u.referrer_id:
        ref = await ctx.users.get(u.referrer_id)
        lines.append(f"Пригласил: {esc(ref.display_name) if ref else '—'} (<code>{u.referrer_id}</code>)")
    lines.append(f"Приглашённые ({len(children)}):")
    icon = {"pending": "⏳", "rewarded": "✅", "rejected": "❌"}
    for r, ch in children:
        lines.append(f"{icon.get(r.status, '•')} {esc(ch.display_name) if ch else r.referred_id} · {fmt_date(r.created_at)}")
    await show(cb, "👥 <b>Рефералы</b>\n\n" + "\n".join(lines), kb(btn("← Назад", f"a:u:{uid}:card")))


# ───────────────────────── ввод текста ─────────────────────────
@on_input("u_days", "subs.grant")
async def in_days(message: Message, state: FSMContext, ctx: AppContext, data: dict) -> None:
    days = parse_int(message.text, lo=1, hi=3650)
    if days is None:
        await message.answer("Нужно целое число от 1 до 3650. Попробуй ещё раз.")
        return
    uid, tier, sign = int(data["uid"]), Tier(data["tier"]), int(data["sign"])
    await state.clear()
    actor = message.from_user.id
    if sign > 0:
        old, new = await ctx.subs.grant_days(uid, tier, days, kind="admin_add", actor_id=actor)
        await ctx.notify.send(
            uid, ctx.texts.render("n_admin_grant", days=days_word(days), tier=tier.title, until=fmt_dt(new.total_end))
        )
        flash = f"✅ Начислено {days_word(days)} {tier.title}"
    else:
        old, new = await ctx.subs.remove_days(uid, tier, days, actor_id=actor)
        flash = f"✅ Снято {days_word(days)} {tier.title}"
    await _after_change(ctx, uid, old, new)
    await ctx.audit.log(actor, "user.add_days" if sign > 0 else "user.remove_days", uid, tier=tier.value, days=days)
    await _card(message, ctx, uid, flash)


@on_input("u_set", "subs.grant")
async def in_set(message: Message, state: FSMContext, ctx: AppContext, data: dict) -> None:
    days = parse_int(message.text, lo=1, hi=3650)
    if days is None:
        await message.answer("Нужно целое число от 1 до 3650. Попробуй ещё раз.")
        return
    uid, tier = int(data["uid"]), Tier(data["tier"])
    await state.clear()
    old, new = await ctx.subs.set_exact(uid, tier, days, actor_id=message.from_user.id)
    await _after_change(ctx, uid, old, new)
    await ctx.audit.log(message.from_user.id, "user.set_sub", uid, tier=tier.value, days=days)
    await _card(message, ctx, uid, f"✅ Установлено: {tier.title} на {days_word(days)}")


@on_input("u_xdev", "subs.grant")
async def in_xdev(message: Message, state: FSMContext, ctx: AppContext, data: dict) -> None:
    mx = int(ctx.cfg.get("max_extra_devices") or 0)
    n = parse_int(message.text, lo=0, hi=max(mx, 0))
    if n is None:
        await message.answer(f"Нужно целое число от 0 до {mx}. Попробуйте ещё раз.")
        return
    uid = int(data["uid"])
    await state.clear()
    u = await ctx.users.get(uid)
    if u is None or not ctx.subs.view(u).is_active(utcnow()):
        await _card(message, ctx, uid, "❌ Дополнительные устройства возможны только при активной подписке")
        return
    delta = n - int(u.extra_devices or 0)
    if delta:
        await ctx.subs.change_extra_devices(uid, delta, kind="admin_devices", actor_id=message.from_user.id)
        await ctx.audit.log(message.from_user.id, "user.extra_devices", uid, total=n)
    await _card(message, ctx, uid, f"✅ Дополнительных устройств: {n}")


@on_input("u_msg", "users.manage")
async def in_msg(message: Message, state: FSMContext, ctx: AppContext, data: dict) -> None:
    uid = int(data["uid"])
    await state.clear()
    ok = await ctx.notify.send(uid, message.html_text or message.text or "")
    await ctx.audit.log(message.from_user.id, "user.message", uid, delivered=ok)
    await _card(message, ctx, uid, "✅ Сообщение доставлено" if ok else "❌ Не удалось доставить (пользователь заблокировал бота?)")


@on_input("u_note", "users.manage")
async def in_note(message: Message, state: FSMContext, ctx: AppContext, data: dict) -> None:
    uid = int(data["uid"])
    await state.clear()
    text = (message.text or "").strip()
    await ctx.users.set_note(uid, None if text in ("", "-") else text[:500])
    await _card(message, ctx, uid, "✅ Заметка сохранена")


@on_input("u_ban", "users.manage")
async def in_ban(message: Message, state: FSMContext, ctx: AppContext, data: dict) -> None:
    uid = int(data["uid"])
    await state.clear()
    reason = (message.text or "").strip()
    await ctx.users.set_banned(uid, True, None if reason in ("", "-") else reason[:200])
    await ctx.audit.log(message.from_user.id, "user.ban", uid, reason=reason)
    await _card(message, ctx, uid, "🚫 Пользователь забанен, VPN отключён")
