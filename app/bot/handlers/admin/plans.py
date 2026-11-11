from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.bot.handlers.admin.common import ask, make_router, on_input
from app.bot.ui import btn, confirm_kb, kb, show, toast
from app.core.pricing import TERMS, term_price
from app.db.models import User
from app.integrations.remnawave import RemnaError
from app.utils import days_word, esc, money, months_word, parse_decimal, parse_int, spawn, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)
router = make_router("plans", scope="plans.edit")

FIELDS = {
    "price": "Отправь новую цену за 1 месяц в рублях (например 149 или 149.50). Цены на 3/6/12 месяцев посчитаются сами по скидкам из «Настройки → Цены, сроки и устройства». Доплата за апгрейд LITE → PREMIUM пересчитается автоматически.",
    "period": "Отправь длину «месяца» в днях (1–365, обычно 30). Для LITE и PREMIUM лучше ставить одинаковую. Год (12 месяцев) при 30 днях даёт ровно 365 дней.",
    "devices": "Отправь, сколько устройств ВХОДИТ в тариф (1–100). Сверх этого пользователь докупает устройства по цене из «Настройки». Применяется к панели при следующей синхронизации.",
    "locations": "Отправь число локаций (оно показывается в описании тарифа).",
    "features": "Отправь дополнительные преимущества — по одному на строку (эмодзи приветствуются). «-» — очистить.",
}


async def _squad_names(ctx: AppContext) -> dict[str, str] | None:
    try:
        return {str(s.get("uuid")): str(s.get("name") or s.get("uuid")) for s in await asyncio.wait_for(ctx.remna.list_squads(), 5)}
    except Exception:  # noqa: BLE001
        return None


@router.callback_query(F.data == "a:t")
async def cb_list(cb: CallbackQuery, ctx: AppContext) -> None:
    rows = [
        btn(f"{'🟢' if p.is_active else '⛔️'} {p.title} · {money(p.price)}/мес · {p.device_limit} устр.", f"a:tv:{p.code}")
        for p in ctx.plans.all()
    ]
    rows.append(btn("← Назад", "a:home"))
    await show(cb, "🏷 <b>Тарифы</b>\n\nЦены, лимиты, сквады и описание — всё меняется здесь, без перезапуска.", kb(*rows))


async def _view(event: CallbackQuery | Message, ctx: AppContext, code: str, flash: str = "") -> None:
    p = ctx.plans.get(code)
    names = await _squad_names(ctx)
    if p.squads:
        shown = ", ".join(esc((names or {}).get(u, u[:8] + "…")) for u in p.squads)
    else:
        shown = "⚠️ <b>не выбраны</b> — пользователи не получат серверы!"
    async with ctx.session() as s:
        users = int(await s.scalar(select(func.count()).select_from(User).where(User.tier == code, User.expires_at > utcnow())) or 0)
    extra = "\n".join(f" • {esc(x)}" for x in (p.features or "").splitlines() if x.strip()) or " —"
    disc = ctx.cfg.term_discounts()
    terms = " · ".join(f"{months_word(m)} {money(term_price(p.price, m, disc[m]))}" for m in TERMS)
    text = (
        (f"{flash}\n\n" if flash else "")
        + f"🏷 <b>Тариф {esc(p.title)}</b> — {'включён ✅' if p.is_active else 'выключен ⛔️'}\n\n"
        f"💰 Цена: <b>{money(p.price)}</b> за месяц ({days_word(p.period_days)})\n"
        f"🗓 По срокам: {terms}\n"
        f"📱 Устройств в тарифе: <b>{p.device_limit}</b> (+ докупка по {money(ctx.cfg.device_price())}/мес) · 🌍 Локаций: <b>{p.locations}</b>\n"
        f"🛡 Белые списки: <b>{'да' if p.whitelist else 'нет'}</b>\n"
        f"🧩 Сквады: {shown}\n"
        f"📝 Доп. преимущества:\n{extra}\n\n"
        f"Активных пользователей на тарифе: {users}"
    )
    c = p.code
    markup = kb(
        [btn("💰 Цена", f"a:te:{c}:price"), btn("📅 Срок", f"a:te:{c}:period")],
        [btn("📱 Устройства", f"a:te:{c}:devices"), btn("🌍 Локации", f"a:te:{c}:locations")],
        [btn(f"🛡 Белые списки: {'выкл' if p.whitelist else 'вкл'}", f"a:tt:{c}:wl")],
        [btn("📝 Преимущества", f"a:te:{c}:features"), btn("🧩 Сквады", f"a:tq:{c}")],
        [btn("⛔️ Выключить тариф" if p.is_active else "✅ Включить тариф", f"a:tt:{c}:act")],
        [btn("🔄 Применить ко всем пользователям", f"a:ts:{c}")],
        [btn("← Назад", "a:t")],
    )
    await show(event, text, markup)


@router.callback_query(F.data.startswith("a:tv:"))
async def cb_view(cb: CallbackQuery, ctx: AppContext) -> None:
    await _view(cb, ctx, (cb.data or "").split(":")[2])


@router.callback_query(F.data.startswith("a:te:"))
async def cb_edit(cb: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    _, _, code, field = (cb.data or "").split(":")
    await ask(cb, state, "t_edit", FIELDS[field], f"a:tv:{code}", code=code, field=field)


@on_input("t_edit", "plans.edit")
async def in_edit(message: Message, state: FSMContext, ctx: AppContext, data: dict) -> None:
    code, field = str(data["code"]), str(data["field"])
    text = (message.text or "").strip()
    value: object | None
    column = {"price": "price", "period": "period_days", "devices": "device_limit", "locations": "locations", "features": "features"}[field]
    if field == "price":
        value = parse_decimal(text, lo=Decimal("1"), hi=Decimal("100000"))
    elif field == "period":
        value = parse_int(text, lo=1, hi=365)
    elif field == "devices":
        value = parse_int(text, lo=1, hi=100)
    elif field == "locations":
        value = parse_int(text, lo=0, hi=500)
    else:
        value = "" if text == "-" else "\n".join(x.strip() for x in text.splitlines() if x.strip())[:800]
    if value is None:
        await message.answer("Не получилось разобрать значение. Проверь формат и отправь ещё раз.")
        return
    await state.clear()
    await ctx.plans.update(code, **{column: value})
    await ctx.audit.log(message.from_user.id, "plan.edit", code, field=field, value=str(value)[:100])
    note = ""
    if field == "devices":
        note = "\n\nЧтобы новый лимит получили уже подключённые пользователи — нажми «Применить ко всем»."
    await _view(message, ctx, code, f"✅ Сохранено{note}")


@router.callback_query(F.data.startswith("a:tt:"))
async def cb_toggle(cb: CallbackQuery, ctx: AppContext) -> None:
    _, _, code, what = (cb.data or "").split(":")
    p = ctx.plans.get(code)
    if what == "wl":
        await ctx.plans.update(code, whitelist=not p.whitelist)
    else:
        if p.is_active and sum(1 for x in ctx.plans.all() if x.is_active) <= 1:
            await toast(cb, "Нельзя выключить единственный активный тариф", alert=True)
            return
        await ctx.plans.update(code, is_active=not p.is_active)
    await ctx.audit.log(cb.from_user.id, "plan.toggle", code, what=what)
    await _view(cb, ctx, code)


# ───────────────────────── сквады ─────────────────────────
async def _squads_screen(cb: CallbackQuery, ctx: AppContext, code: str) -> None:
    try:
        squads = sorted(await ctx.remna.list_squads(), key=lambda s: str(s.get("name", "")).lower())
    except RemnaError as e:
        await show(cb, f"⚠️ Не удалось получить сквады из панели: {esc(str(e))[:200]}", kb(btn("← Назад", f"a:tv:{code}")))
        return
    selected = set(ctx.plans.get(code).squads or [])
    rows = []
    for i, sq in enumerate(squads):
        members = (sq.get("info") or {}).get("membersCount")
        label = f"{'✅' if str(sq.get('uuid')) in selected else '▫️'} {sq.get('name', sq.get('uuid'))}"
        if members is not None:
            label += f" · {members}"
        rows.append(btn(label[:60], f"a:tqt:{code}:{i}"))
    rows.append(btn("Готово", f"a:tv:{code}"))
    text = (
        f"🧩 <b>Сквады тарифа {code.upper()}</b>\n\n"
        "Отметь внутренние сквады Remnawave, которые получают подписчики этого тарифа "
        "(в них лежат ноды и хосты: 5 локаций для LITE, 10 + белые списки для PREMIUM).\n\n"
        "Изменения применятся при следующей синхронизации пользователя — или сразу, если нажать «Применить ко всем»."
    )
    if not squads:
        text += "\n\n⚠️ В панели пока нет сквадов — создай их в Remnawave (Internal Squads)."
    await show(cb, text, kb(*rows))


@router.callback_query(F.data.startswith("a:tq:"))
async def cb_squads(cb: CallbackQuery, ctx: AppContext) -> None:
    await _squads_screen(cb, ctx, (cb.data or "").split(":")[2])


@router.callback_query(F.data.startswith("a:tqt:"))
async def cb_squad_toggle(cb: CallbackQuery, ctx: AppContext) -> None:
    _, _, code, idx = (cb.data or "").split(":")
    try:
        squads = sorted(await ctx.remna.list_squads(), key=lambda s: str(s.get("name", "")).lower())
        uuid = str(squads[int(idx)]["uuid"])
    except (RemnaError, IndexError, KeyError, ValueError):
        await toast(cb, "Список сквадов изменился — открой экран заново", alert=True)
        return
    current = list(ctx.plans.get(code).squads or [])
    current = [u for u in current if u != uuid] if uuid in current else current + [uuid]
    await ctx.plans.update(code, squads=current)
    await ctx.audit.log(cb.from_user.id, "plan.squads", code, squads=",".join(current))
    await _squads_screen(cb, ctx, code)


# ───────────────────────── применить ко всем ─────────────────────────
@router.callback_query(F.data.startswith("a:ts:"))
async def cb_apply_ask(cb: CallbackQuery, ctx: AppContext) -> None:
    code = (cb.data or "").split(":")[2]
    async with ctx.session() as s:
        n = int(await s.scalar(select(func.count()).select_from(User).where(User.tier == code, User.expires_at > utcnow())) or 0)
    await show(
        cb,
        f"🔄 Отправить настройки тарифа <b>{code.upper()}</b> (лимит устройств, сквады, срок) в панель для <b>{n}</b> активных пользователей?",
        confirm_kb(f"a:tso:{code}", f"a:tv:{code}", yes_text="Да, применить"),
    )


async def _bulk(ctx: AppContext, code: str, admin_id: int) -> None:
    async with ctx.session() as s:
        ids = list((await s.scalars(select(User.id).where(User.tier == code, User.expires_at > utcnow()))).all())
    ok, fail = await ctx.subs.resync_many(ids)
    await ctx.notify.send(admin_id, f"✅ Тариф <b>{code.upper()}</b> применён: успешно {ok}, с ошибкой {fail}.")


@router.callback_query(F.data.startswith("a:tso:"))
async def cb_apply(cb: CallbackQuery, ctx: AppContext) -> None:
    code = (cb.data or "").split(":")[2]
    spawn(_bulk(ctx, code, cb.from_user.id))
    await ctx.audit.log(cb.from_user.id, "plan.apply_all", code)
    await toast(cb, "Запущено в фоне — пришлю отчёт.", alert=True)
    await _view(cb, ctx, code)
