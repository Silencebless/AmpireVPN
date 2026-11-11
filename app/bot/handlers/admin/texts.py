from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.admin.common import ask, make_router, on_input
from app.bot.ui import btn, kb, nav_row, page_slice, show, toast
from app.texts import TEXTS
from app.utils import esc, truncate

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)
router = make_router("texts", scope="texts.edit")
KEYS = list(TEXTS.keys())
PAGE = 8


@router.callback_query(F.data.startswith("a:x:"))
async def cb_list(cb: CallbackQuery, ctx: AppContext) -> None:
    items, page, pages = page_slice(KEYS, int((cb.data or "").split(":")[2]), PAGE)
    rows = [
        btn(f"{'✏️' if ctx.texts.is_overridden(k) else '▫️'} {TEXTS[k].title}"[:60], f"a:xv:{KEYS.index(k)}")
        for k in items
    ]
    rows.append(nav_row(page, pages, lambda p: f"a:x:{p}"))
    rows.append(btn("← Назад", "a:home"))
    text = (
        "✏️ <b>Тексты бота</b>\n\nВсе сообщения бота можно переписать без перезапуска. "
        "Выбери текст, чтобы посмотреть или изменить.\n✏️ — текст изменён тобой, ▫️ — стандартный."
    )
    await show(cb, text, kb(*rows))


async def _view(event: CallbackQuery | Message, ctx: AppContext, i: int, flash: str = "") -> None:
    key = KEYS[i]
    td = TEXTS[key]
    changed = ctx.texts.is_overridden(key)
    vars_line = ", ".join("{" + v + "}" for v in td.vars) or "—"
    text = (
        (f"{flash}\n\n" if flash else "")
        + f"✏️ <b>{esc(td.title)}</b>{' · изменён' if changed else ''}\n\n"
        f"Переменные: <code>{esc(vars_line)}</code>\n\n"
        f"<b>Исходник:</b>\n<pre>{esc(truncate(ctx.texts.current(key), 2600))}</pre>"
    )
    rows = [[btn("✏️ Изменить", f"a:xe:{i}"), btn("👁 Как выглядит", f"a:xp:{i}")]]
    if changed:
        rows.append([btn("♻️ Сбросить к стандартному", f"a:xr:{i}")])
    rows.append([btn("← К списку", f"a:x:{i // PAGE}")])
    await show(event, text, kb(*rows))


@router.callback_query(F.data.startswith("a:xv:"))
async def cb_view(cb: CallbackQuery, ctx: AppContext) -> None:
    await _view(cb, ctx, int((cb.data or "").split(":")[2]))


@router.callback_query(F.data.startswith("a:xp:"))
async def cb_preview(cb: CallbackQuery, ctx: AppContext) -> None:
    key = KEYS[int((cb.data or "").split(":")[2])]
    try:
        await ctx.bot.send_message(cb.from_user.id, ctx.texts.sample(key))
        await toast(cb, "Предпросмотр отправлен ниже (переменные показаны как ‹имя›)")
    except TelegramBadRequest as e:
        await toast(cb, f"Telegram не принял разметку: {e}"[:190], alert=True)


@router.callback_query(F.data.startswith("a:xr:"))
async def cb_reset(cb: CallbackQuery, ctx: AppContext) -> None:
    i = int((cb.data or "").split(":")[2])
    await ctx.texts.reset(KEYS[i])
    await ctx.audit.log(cb.from_user.id, "text.reset", KEYS[i])
    await _view(cb, ctx, i, "♻️ Возвращён стандартный текст")


@router.callback_query(F.data.startswith("a:xe:"))
async def cb_edit(cb: CallbackQuery, ctx: AppContext, state: FSMContext) -> None:
    i = int((cb.data or "").split(":")[2])
    td = TEXTS[KEYS[i]]
    vars_line = ", ".join("{" + v + "}" for v in td.vars) or "нет"
    prompt = (
        f"✏️ <b>{esc(td.title)}</b>\n\nОтправь новый текст целиком. Форматирование делай средствами Telegram "
        "(выдели текст → жирный, курсив, ссылка) или HTML-тегами.\n\n"
        f"Доступные переменные: <code>{esc(vars_line)}</code> — они подставятся автоматически."
    )
    await ask(cb, state, "x_edit", prompt, f"a:xv:{i}", i=i)


@on_input("x_edit", "texts.edit")
async def in_edit(message: Message, state: FSMContext, ctx: AppContext, data: dict) -> None:
    i = int(data["i"])
    key = KEYS[i]
    # есть форматирование Telegram -> переводим в HTML; нет -> берём как есть (можно писать HTML руками)
    value = (message.html_text if message.entities else message.text or "").strip()
    err = ctx.texts.validate(key, value)
    if err:
        await message.answer(f"❌ {err}")
        return
    try:
        await message.answer(ctx.texts.sample(key, value))  # заодно показываем результат
    except TelegramBadRequest as e:
        await message.answer(f"❌ Telegram не принял разметку: {esc(str(e))[:250]}\nПроверь теги и экранирование &lt; и &amp;.")
        return
    await state.clear()
    await ctx.texts.set(key, value, message.from_user.id)
    await ctx.audit.log(message.from_user.id, "text.edit", key)
    await _view(message, ctx, i, "✅ Текст сохранён — ниже видно, как он выглядит")
