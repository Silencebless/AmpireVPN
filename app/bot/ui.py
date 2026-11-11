"""Хелперы интерфейса: кнопки, клавиатуры, навигация «одним сообщением», пагинация."""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.labels import BACK


def btn(text: str, cb: str | None = None, *, url: str | None = None) -> InlineKeyboardButton:
    if url:
        return InlineKeyboardButton(text=text, url=url)
    if cb is None:
        raise ValueError("нужен callback_data или url")
    if len(cb.encode()) > 64:
        raise ValueError(f"callback_data длиннее 64 байт: {cb}")
    return InlineKeyboardButton(text=text, callback_data=cb)


def kb(*rows: Any) -> InlineKeyboardMarkup:
    """Собирает клавиатуру. Строка — кнопка или список кнопок; None пропускается."""
    out: list[list[InlineKeyboardButton]] = []
    for row in rows:
        if row is None:
            continue
        if isinstance(row, InlineKeyboardButton):
            out.append([row])
        else:
            r = [b for b in row if b is not None]
            if r:
                out.append(r)
    return InlineKeyboardMarkup(inline_keyboard=out)


def back_kb(cb: str, text: str = BACK) -> InlineKeyboardMarkup:
    return kb(btn(text, cb))


def confirm_kb(yes_cb: str, no_cb: str, yes_text: str = "Подтвердить", no_text: str = "Отмена") -> InlineKeyboardMarkup:
    return kb([btn(yes_text, yes_cb)], [btn(no_text, no_cb)])


async def show(
    event: Message | CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    *,
    new: bool = False,
) -> Message | None:
    """Показывает экран: редактирует текущее сообщение (для callback) или отправляет новое."""
    if isinstance(event, CallbackQuery):
        msg = event.message
        result: Message | None = None
        if isinstance(msg, Message) and not new:
            try:
                edited = await msg.edit_text(text, reply_markup=markup)
                result = edited if isinstance(edited, Message) else msg
            except TelegramBadRequest as e:
                if "message is not modified" in str(e).lower():
                    result = msg
                else:
                    # сообщение нельзя отредактировать (медиа, слишком старое) — шлём новое
                    try:
                        await msg.delete()
                    except TelegramBadRequest:
                        pass
                    result = await event.bot.send_message(event.from_user.id, text, reply_markup=markup)
        else:
            result = await event.bot.send_message(event.from_user.id, text, reply_markup=markup)
        try:
            await event.answer()
        except TelegramBadRequest:
            pass
        return result
    return await event.answer(text, reply_markup=markup)


async def toast(cb: CallbackQuery, text: str, *, alert: bool = False) -> None:
    try:
        await cb.answer(text, show_alert=alert)
    except TelegramBadRequest:
        pass


# ───────────────────────── пагинация ─────────────────────────
def page_info(total: int, page: int, per_page: int) -> tuple[int, int]:
    pages = max(1, math.ceil(total / per_page))
    return min(max(0, page), pages - 1), pages


def page_slice(items: Sequence[Any], page: int, per_page: int) -> tuple[list[Any], int, int]:
    page, pages = page_info(len(items), page, per_page)
    return list(items[page * per_page : (page + 1) * per_page]), page, pages


def nav_row(page: int, pages: int, make_cb: Callable[[int], str]) -> list[InlineKeyboardButton] | None:
    if pages <= 1:
        return None
    row: list[InlineKeyboardButton] = []
    row.append(btn("‹", make_cb(page - 1)) if page > 0 else btn("·", "noop"))
    row.append(btn(f"{page + 1}/{pages}", "noop"))
    row.append(btn("›", make_cb(page + 1)) if page < pages - 1 else btn("·", "noop"))
    return row
