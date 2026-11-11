from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.admin.common import ask, make_router, on_input
from app.bot.ui import btn, confirm_kb, kb, show, toast
from app.services.users import SEGMENTS
from app.utils import esc, fmt_dt

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)
router = make_router("broadcast", scope="broadcast.send")

STATUS = {
    "draft": ("📝", "черновик"),
    "running": ("▶️", "идёт"),
    "paused": ("⏸", "на паузе"),
    "done": ("✅", "завершена"),
    "cancelled": ("⛔️", "отменена"),
}


def _bar(done: int, total: int) -> str:
    pct = min(100, int(done / total * 100)) if total else 0
    filled = pct // 10
    return "▰" * filled + "▱" * (10 - filled) + f" {pct}%"


@router.callback_query(F.data == "a:b")
async def cb_menu(cb: CallbackQuery, ctx: AppContext) -> None:
    items = await ctx.bc.recent(8)
    rows = [btn("➕ Новая рассылка", "a:bn")]
    for b in items:
        icon, _ = STATUS[b.status]
        rows.append(btn(f"{icon} #{b.id} · {SEGMENTS.get(b.segment, b.segment)} · {b.sent}/{b.total}", f"a:bv:{b.id}"))
    rows.append(btn("← Назад", "a:home"))
    text = (
        "📣 <b>Рассылки</b>\n\n"
        "Пришли боту любое сообщение — текст, фото, видео, файл — и выбери, кому отправить. "
        "Форматирование и медиа сохраняются. Заблокировавшие бота и забаненные пропускаются."
    )
    await show(cb, text, kb(*rows))


@router.callback_query(F.data == "a:bn")
async def cb_new(cb: CallbackQuery, ctx: AppContext) -> None:
    counts = await asyncio.gather(*(ctx.bc.audience_count(k) for k in SEGMENTS))
    items = [btn(f"{title} · {n}", f"a:bns:{key}") for (key, title), n in zip(SEGMENTS.items(), counts)]
    rows = [items[i : i + 2] for i in range(0, len(items), 2)]
    rows.append([btn("← Отмена", "a:b")])
    await show(cb, "➕ <b>Новая рассылка</b>\n\nКому отправляем? (в кнопках — число получателей)", kb(*rows))


@router.callback_query(F.data.startswith("a:bns:"))
async def cb_segment(cb: CallbackQuery, state: FSMContext) -> None:
    segment = (cb.data or "").split(":")[2]
    await ask(
        cb, state, "b_content",
        f"Сегмент: <b>{SEGMENTS.get(segment, segment)}</b>\n\nТеперь отправь сообщение для рассылки "
        "(текст, фото, видео, файл — что угодно). Оно уйдёт получателям в таком же виде.",
        "a:b", segment=segment,
    )


async def _draft(event: CallbackQuery | Message, ctx: AppContext, bid: int, flash: str = "") -> None:
    b = await ctx.bc.get(bid)
    if b is None:
        await show(event, "Рассылка не найдена.", kb(btn("← Назад", "a:b")))
        return
    n = await ctx.bc.audience_count(b.segment)
    text = (
        (f"{flash}\n\n" if flash else "")
        + f"📝 <b>Рассылка #{b.id} — черновик</b>\n\n"
        f"Аудитория: <b>{SEGMENTS.get(b.segment, b.segment)}</b> — {n} получателей\n"
        f"Кнопка: {esc(b.button_text) + ' → ' + esc(b.button_url) if b.button_text else 'нет'}"
    )
    markup = kb(
        [btn("👁 Предпросмотр", f"a:bpv:{b.id}"), btn("🔗 Кнопка", f"a:bbt:{b.id}")],
        [btn("🚀 Запустить", f"a:bgo:{b.id}")],
        [btn("🗑 Отменить", f"a:bx:{b.id}")],
    )
    await show(event, text, markup)


@on_input("b_content", "broadcast.send")
async def in_content(message: Message, state: FSMContext, ctx: AppContext, data: dict) -> None:
    await state.clear()
    b = await ctx.bc.create(message.from_user.id, message.chat.id, message.message_id, str(data["segment"]))
    try:
        await ctx.bc.preview(b, message.chat.id)
    except TelegramAPIError as e:
        await message.answer(f"⚠️ Не удалось сделать предпросмотр: {esc(str(e))[:200]}")
    await _draft(message, ctx, b.id)


@router.callback_query(F.data.startswith("a:bpv:"))
async def cb_preview(cb: CallbackQuery, ctx: AppContext) -> None:
    b = await ctx.bc.get(int((cb.data or "").split(":")[2]))
    if b is None:
        return
    try:
        await ctx.bc.preview(b, cb.from_user.id)
        await toast(cb, "Предпросмотр отправлен ниже")
    except TelegramAPIError as e:
        await toast(cb, f"Ошибка: {e}"[:190], alert=True)


@router.callback_query(F.data.startswith("a:bbt:"))
async def cb_button(cb: CallbackQuery, state: FSMContext) -> None:
    bid = int((cb.data or "").split(":")[2])
    await ask(cb, state, "b_button", "Отправь кнопку в формате:\n<code>Текст кнопки | https://ссылка</code>\n\n«-» — убрать кнопку.",
              f"a:bv:{bid}", bid=bid)


@on_input("b_button", "broadcast.send")
async def in_button(message: Message, state: FSMContext, ctx: AppContext, data: dict) -> None:
    from sqlalchemy import update

    from app.db.models import Broadcast

    bid = int(data["bid"])
    raw = (message.text or "").strip()
    text = url = None
    if raw != "-":
        parts = [p.strip() for p in raw.split("|", 1)]
        if len(parts) != 2 or not parts[0] or not parts[1].startswith(("http://", "https://", "tg://")):
            await message.answer("Формат: <code>Текст | https://ссылка</code>")
            return
        text, url = parts[0][:60], parts[1]
    await state.clear()
    async with ctx.session() as s:
        await s.execute(update(Broadcast).where(Broadcast.id == bid).values(button_text=text, button_url=url))
        await s.commit()
    await _draft(message, ctx, bid, "✅ Кнопка обновлена")


@router.callback_query(F.data.startswith("a:bgo:"))
async def cb_go_ask(cb: CallbackQuery, ctx: AppContext) -> None:
    bid = int((cb.data or "").split(":")[2])
    b = await ctx.bc.get(bid)
    if b is None:
        return
    n = await ctx.bc.audience_count(b.segment)
    await show(
        cb,
        f"🚀 Запустить рассылку #{b.id} для <b>{n}</b> получателей ({SEGMENTS.get(b.segment, b.segment)})?\n\n"
        "Остановить можно в любой момент кнопкой паузы или отмены.",
        confirm_kb(f"a:bgo2:{bid}", f"a:bv:{bid}", yes_text="Да, запустить"),
    )


@router.callback_query(F.data.startswith("a:bgo2:"))
async def cb_go(cb: CallbackQuery, ctx: AppContext) -> None:
    bid = int((cb.data or "").split(":")[2])
    await ctx.bc.start(bid)
    await ctx.audit.log(cb.from_user.id, "broadcast.start", bid)
    await _status(cb, ctx, bid)


async def _status(event: CallbackQuery | Message, ctx: AppContext, bid: int) -> None:
    b = await ctx.bc.get(bid)
    if b is None:
        await show(event, "Рассылка не найдена.", kb(btn("← Назад", "a:b")))
        return
    if b.status == "draft":
        await _draft(event, ctx, bid)
        return
    icon, title = STATUS[b.status]
    done = b.sent + b.failed + b.blocked
    text = (
        f"{icon} <b>Рассылка #{b.id} — {title}</b>\n\n"
        f"Аудитория: {SEGMENTS.get(b.segment, b.segment)} · всего {b.total}\n"
        f"{_bar(done, b.total)}\n"
        f"✅ Доставлено: <b>{b.sent}</b>\n❌ Ошибок: {b.failed}\n🔕 Заблокировали бота: {b.blocked}\n\n"
        f"Запуск: {fmt_dt(b.started_at)} · Конец: {fmt_dt(b.finished_at)}"
    )
    rows = []
    if b.status == "running":
        rows.append([btn("⏸ Пауза", f"a:bp:{bid}"), btn("⛔️ Отменить", f"a:bx:{bid}")])
    elif b.status == "paused":
        rows.append([btn("▶️ Продолжить", f"a:br:{bid}"), btn("⛔️ Отменить", f"a:bx:{bid}")])
    rows.append([btn("🔄 Обновить", f"a:bv:{bid}"), btn("← Рассылки", "a:b")])
    await show(event, text, kb(*rows))


@router.callback_query(F.data.startswith("a:bv:"))
async def cb_view(cb: CallbackQuery, ctx: AppContext) -> None:
    await _status(cb, ctx, int((cb.data or "").split(":")[2]))


@router.callback_query(F.data.startswith("a:bp:"))
async def cb_pause(cb: CallbackQuery, ctx: AppContext) -> None:
    bid = int((cb.data or "").split(":")[2])
    await ctx.bc.pause(bid)
    await asyncio.sleep(0.3)
    await _status(cb, ctx, bid)


@router.callback_query(F.data.startswith("a:br:"))
async def cb_resume(cb: CallbackQuery, ctx: AppContext) -> None:
    bid = int((cb.data or "").split(":")[2])
    await ctx.bc.start(bid)
    await _status(cb, ctx, bid)


@router.callback_query(F.data.startswith("a:bx:"))
async def cb_cancel(cb: CallbackQuery, ctx: AppContext) -> None:
    bid = int((cb.data or "").split(":")[2])
    await ctx.bc.cancel(bid)
    await ctx.audit.log(cb.from_user.id, "broadcast.cancel", bid)
    await show(cb, f"⛔️ Рассылка #{bid} отменена.", kb(btn("← Рассылки", "a:b")))
