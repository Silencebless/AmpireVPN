from __future__ import annotations

import logging
import re
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.admin.common import ask, make_router, on_input
from app.bot.ui import btn, confirm_kb, kb, nav_row, page_info, show
from app.core.pricing import PROMO_DAYS, PROMO_FIXED, PROMO_PERCENT
from app.db.models import Promo
from app.services.promos import KIND_TITLES, describe_value
from app.utils import esc, fmt_dt, parse_decimal, parse_int, truncate, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)
router = make_router("promos", scope="promos.manage")
PAGE = 8
CODE_RE = re.compile(r"^[A-Za-z0-9_\-]{3,24}$")


def _status(p: Promo, used: int) -> str:
    if not p.is_active:
        return "⛔️"
    if p.expires_at and p.expires_at <= utcnow():
        return "⌛️"
    if p.max_uses is not None and used >= p.max_uses:
        return "🈵"
    return "🟢"


@router.callback_query(F.data.startswith("a:cl:"))
async def cb_list(cb: CallbackQuery, ctx: AppContext) -> None:
    page_req = int((cb.data or "").split(":")[2])
    items, total = await ctx.promos.page(page_req, PAGE)
    page, pages = page_info(total, page_req, PAGE)
    if page != page_req:
        items, _ = await ctx.promos.page(page, PAGE)
    rows = [btn("➕ Создать промокод", "a:cn")]
    for p, used in items:
        limit = f"/{p.max_uses}" if p.max_uses is not None else ""
        rows.append(btn(f"{_status(p, used)} {p.code} · {describe_value(p)} · {used}{limit}", f"a:cv:{p.id}"))
    rows.append(nav_row(page, pages, lambda n: f"a:cl:{n}"))
    rows.append(btn("← Назад", "a:home"))
    await show(cb, f"🎟 <b>Промокоды</b> — {total}\n\n🟢 действует · ⛔️ выключен · ⌛️ истёк · 🈵 лимит исчерпан", kb(*rows))


# ───────────────────────── карточка ─────────────────────────
async def _view(event: CallbackQuery | Message, ctx: AppContext, promo_id: int, flash: str = "") -> None:
    p = await ctx.promos.get(promo_id)
    if p is None:
        await show(event, "Промокод не найден.", kb(btn("← Назад", "a:cl:0")))
        return
    used = await ctx.promos.uses_count(p.id)
    limit = f"{used} из {p.max_uses}" if p.max_uses is not None else f"{used} (без лимита)"
    where = ""
    if p.kind != PROMO_DAYS:
        where = f"\nПрименим к: {', '.join(t.upper() for t in p.tiers) if p.tiers else 'любому тарифу'}"
    text = (
        (f"{flash}\n\n" if flash else "")
        + f"🎟 <b>{esc(p.code)}</b> {_status(p, used)}\n\n"
        f"Тип: {KIND_TITLES[p.kind]}\nЭффект: <b>{describe_value(p)}</b>{where}\n"
        f"Активаций: {limit}\n"
        f"Действует до: {fmt_dt(p.expires_at) if p.expires_at else 'бессрочно'}\n"
        f"Создан: {fmt_dt(p.created_at)}"
    )
    i = p.id
    markup = kb(
        [btn("⏸ Выключить" if p.is_active else "▶️ Включить", f"a:cvt:{i}")],
        [btn("🔢 Лимит", f"a:cvl:{i}"), btn("📅 Срок", f"a:cve:{i}")],
        [btn("📋 Активации", f"a:cva:{i}"), btn("🗑 Удалить", f"a:cvd:{i}")],
        [btn("← Назад", "a:cl:0")],
    )
    await show(event, text, markup)


@router.callback_query(F.data.startswith("a:cv:"))
async def cb_view(cb: CallbackQuery, ctx: AppContext) -> None:
    await _view(cb, ctx, int((cb.data or "").split(":")[2]))


@router.callback_query(F.data.startswith("a:cvt:"))
async def cb_toggle(cb: CallbackQuery, ctx: AppContext) -> None:
    pid = int((cb.data or "").split(":")[2])
    p = await ctx.promos.get(pid)
    if p:
        await ctx.promos.update(pid, is_active=not p.is_active)
        await ctx.audit.log(cb.from_user.id, "promo.toggle", p.code, active=not p.is_active)
    await _view(cb, ctx, pid)


@router.callback_query(F.data.startswith("a:cvl:"))
async def cb_limit(cb: CallbackQuery, state: FSMContext) -> None:
    pid = int((cb.data or "").split(":")[2])
    await ask(cb, state, "p_limit", "Сколько раз можно использовать промокод? Число, «0» — без лимита.", f"a:cv:{pid}", pid=pid)


@router.callback_query(F.data.startswith("a:cve:"))
async def cb_expiry(cb: CallbackQuery, state: FSMContext) -> None:
    pid = int((cb.data or "").split(":")[2])
    await ask(cb, state, "p_expiry", "Через сколько дней промокод перестанет действовать? Число, «0» — бессрочно.", f"a:cv:{pid}", pid=pid)


@on_input("p_limit", "promos.manage")
async def in_limit(message: Message, state: FSMContext, ctx: AppContext, data: dict) -> None:
    n = parse_int(message.text, lo=0, hi=1_000_000)
    if n is None:
        await message.answer("Нужно целое число (0 — без лимита).")
        return
    await state.clear()
    await ctx.promos.update(int(data["pid"]), max_uses=n or None)
    await _view(message, ctx, int(data["pid"]), "✅ Лимит обновлён")


@on_input("p_expiry", "promos.manage")
async def in_expiry(message: Message, state: FSMContext, ctx: AppContext, data: dict) -> None:
    n = parse_int(message.text, lo=0, hi=3650)
    if n is None:
        await message.answer("Нужно целое число дней (0 — бессрочно).")
        return
    await state.clear()
    await ctx.promos.update(int(data["pid"]), expires_at=utcnow() + timedelta(days=n) if n else None)
    await _view(message, ctx, int(data["pid"]), "✅ Срок обновлён")


@router.callback_query(F.data.startswith("a:cva:"))
async def cb_uses(cb: CallbackQuery, ctx: AppContext) -> None:
    pid = int((cb.data or "").split(":")[2])
    rows = await ctx.promos.activations(pid, 30)
    icon = {"granted": "🎁", "reserved": "⏳", "used": "✅"}
    lines = [
        f"{icon.get(u.status, '•')} {esc(truncate(usr.display_name, 24)) if usr else u.user_id} · {fmt_dt(u.created_at)}"
        for u, usr in rows
    ] or ["Пока никто не активировал."]
    await show(
        cb,
        "📋 <b>Активации</b> (последние 30)\n\n🎁 дни начислены · ⏳ скидка ждёт оплаты · ✅ скидка использована\n\n" + "\n".join(lines),
        kb(btn("← Назад", f"a:cv:{pid}")),
    )


@router.callback_query(F.data.startswith("a:cvd:"))
async def cb_delete_ask(cb: CallbackQuery) -> None:
    pid = int((cb.data or "").split(":")[2])
    await show(cb, "Удалить промокод вместе с историей активаций?", confirm_kb(f"a:cvdo:{pid}", f"a:cv:{pid}", yes_text="Да, удалить"))


@router.callback_query(F.data.startswith("a:cvdo:"))
async def cb_delete(cb: CallbackQuery, ctx: AppContext) -> None:
    pid = int((cb.data or "").split(":")[2])
    await ctx.promos.delete(pid)
    await ctx.audit.log(cb.from_user.id, "promo.delete", pid)
    await show(cb, "🗑 Промокод удалён.", kb(btn("← К списку", "a:cl:0")))


# ───────────────────────── мастер создания ─────────────────────────
@router.callback_query(F.data == "a:cn")
async def cb_new(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_data({})
    markup = kb(
        [btn("Скидка в %", f"a:cnk:{PROMO_PERCENT}"), btn("Скидка в ₽", f"a:cnk:{PROMO_FIXED}")],
        [btn("Бонусные дни", f"a:cnk:{PROMO_DAYS}")],
        [btn("← Отмена", "a:cl:0")],
    )
    await show(cb, "➕ <b>Новый промокод</b>\n\nШаг 1. Что даёт промокод?", markup)


@router.callback_query(F.data.startswith("a:cnk:"))
async def cb_kind(cb: CallbackQuery, state: FSMContext) -> None:
    kind = (cb.data or "").split(":")[2]
    await state.update_data(kind=kind)
    if kind == PROMO_DAYS:
        await show(
            cb,
            "Шаг 2. Дни какого тарифа начислять?",
            kb([btn("LITE", "a:cnt:lite"), btn("PREMIUM", "a:cnt:premium")], [btn("← Отмена", "a:cl:0")]),
        )
    else:
        hint = "процент скидки (1–99)" if kind == PROMO_PERCENT else "сумму скидки в рублях"
        await ask(cb, state, "p_value", f"Шаг 2. Отправь {hint}.", "a:cl:0", keep=True)


@router.callback_query(F.data.startswith("a:cnt:"))
async def cb_days_tier(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(days_tier=(cb.data or "").split(":")[2])
    await ask(cb, state, "p_value", "Шаг 3. Сколько дней начислять? Отправь число.", "a:cl:0", keep=True)


@on_input("p_value", "promos.manage")
async def in_value(message: Message, state: FSMContext, ctx: AppContext, data: dict) -> None:
    kind = data["kind"]
    if kind == PROMO_DAYS:
        n = parse_int(message.text, lo=1, hi=3650)
        value = Decimal(n) if n is not None else None
    elif kind == PROMO_PERCENT:
        n = parse_int(message.text, lo=1, hi=99)
        value = Decimal(n) if n is not None else None
    else:
        value = parse_decimal(message.text, lo=Decimal("1"), hi=Decimal("100000"))
    if value is None:
        await message.answer("Не получилось разобрать значение. Попробуй ещё раз.")
        return
    await state.update_data(value=str(value))
    await state.set_state(None)
    if kind == PROMO_DAYS:
        await _ask_code(message, state)
    else:
        markup = kb(
            [btn("Любой тариф", "a:cna:all")],
            [btn("Только LITE", "a:cna:lite"), btn("Только PREMIUM", "a:cna:premium")],
            [btn("← Отмена", "a:cl:0")],
        )
        await show(message, "Шаг 3. К каким тарифам применима скидка?", markup)


@router.callback_query(F.data.startswith("a:cna:"))
async def cb_applies(cb: CallbackQuery, state: FSMContext) -> None:
    which = (cb.data or "").split(":")[2]
    await state.update_data(tiers=None if which == "all" else [which])
    await _ask_code(cb, state)


async def _ask_code(event: CallbackQuery | Message, state: FSMContext) -> None:
    await ask(
        event, state, "p_code",
        "Следующий шаг. Отправь код промокода (латиница/цифры, 3–24 символа) или «-» — сгенерирую случайный.",
        "a:cl:0", keep=True,
    )


@on_input("p_code", "promos.manage")
async def in_code(message: Message, state: FSMContext, ctx: AppContext, data: dict) -> None:
    text = (message.text or "").strip()
    if text != "-" and not CODE_RE.match(text):
        await message.answer("Код: латиница, цифры, «-» и «_», от 3 до 24 символов. Или «-» для случайного.")
        return
    await ask(
        message, state, "p_max",
        "Сколько раз можно использовать промокод? Число, «0» — без лимита.",
        "a:cl:0", keep=True, code=None if text == "-" else text.upper(),
    )


@on_input("p_max", "promos.manage")
async def in_max(message: Message, state: FSMContext, ctx: AppContext, data: dict) -> None:
    n = parse_int(message.text, lo=0, hi=1_000_000)
    if n is None:
        await message.answer("Нужно целое число (0 — без лимита).")
        return
    await ask(
        message, state, "p_exp",
        "Сколько дней промокод будет действовать? Число, «0» — бессрочно.",
        "a:cl:0", keep=True, max_uses=n or None,
    )


@on_input("p_exp", "promos.manage")
async def in_exp(message: Message, state: FSMContext, ctx: AppContext, data: dict) -> None:
    n = parse_int(message.text, lo=0, hi=3650)
    if n is None:
        await message.answer("Нужно целое число дней (0 — бессрочно).")
        return
    await state.clear()
    try:
        promo = await ctx.promos.create(
            kind=data["kind"],
            value=Decimal(data["value"]),
            code=data.get("code"),
            days_tier=data.get("days_tier"),
            tiers=data.get("tiers"),
            max_uses=data.get("max_uses"),
            expires_at=utcnow() + timedelta(days=n) if n else None,
            created_by=message.from_user.id,
        )
    except ValueError as e:
        await message.answer(f"❌ {e}. Начни создание заново.", reply_markup=kb(btn("← К списку", "a:cl:0")))
        return
    await ctx.audit.log(message.from_user.id, "promo.create", promo.code, kind=promo.kind, value=str(promo.value))
    await _view(message, ctx, promo.id, "✅ Промокод создан")
