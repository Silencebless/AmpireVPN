from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete, update

from app.bot.handlers.admin.common import ask, make_router, on_input
from app.bot.ui import btn, kb, show
from app.db.models import Admin
from app.permissions import ROLE_ORDER, ROLE_TITLES, ROLES
from app.utils import parse_int

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)
router = make_router("admins", owner_only=True)

ROLE_HINTS = {
    "support": "пользователи, платежи (просмотр), помощь клиенту",
    "billing": "платежи и спорные платежи, выдача/снятие дней, статистика",
    "marketing": "промокоды, рефералка, рассылки, тексты, статистика",
    "ops": "узлы и сквады, диагностика, журнал ошибок",
    "admin": "всё, кроме действий владельца",
}


async def _screen(event: CallbackQuery | Message, ctx: AppContext, flash: str = "") -> None:
    owners = sorted(ctx.settings.owners)
    admins = sorted(ctx.admin_ids - ctx.settings.owners)
    lines = ["<b>Администраторы</b>\n"]
    if flash:
        lines.insert(0, flash + "\n")
    lines.append("Владельцы (из .env): " + (", ".join(f"<code>{i}</code>" for i in owners) or "—"))
    if admins:
        lines.append("Администраторы:")
        lines += [f"• <code>{i}</code> — {ROLE_TITLES.get(ctx.admin_roles.get(i, 'admin'), '—')}" for i in admins]
    else:
        lines.append("Администраторы: —")
    lines.append("\n<b>Роли</b>")
    lines += [f"• {ROLE_TITLES[r]} — {ROLE_HINTS[r]}" for r in ROLE_ORDER]
    lines.append("\nТолько владельцам доступны: возвраты денег, управление админами, удаление пользователей, бэкап, сверка и массовая пересинхронизация.")
    rows = []
    for i in admins:
        rows.append([btn(f"Роль {i}", f"a:mr:{i}"), btn(f"Убрать {i}", f"a:md:{i}")])
    rows.append(btn("Добавить администратора", "a:ma"))
    rows.append(btn("← Назад", "a:home"))
    await show(event, "\n".join(lines), kb(*rows))


@router.callback_query(F.data == "a:m")
async def cb_menu(cb: CallbackQuery, ctx: AppContext) -> None:
    await _screen(cb, ctx)


@router.callback_query(F.data == "a:ma")
async def cb_add(cb: CallbackQuery, state: FSMContext) -> None:
    await ask(cb, state, "m_add", "Отправьте Telegram ID нового администратора (число). Узнать ID можно у бота @userinfobot.", "a:m")


def _role_picker(uid: int, back: str):
    rows = [btn(f"{ROLE_TITLES[r]} — {ROLE_HINTS[r]}"[:62], f"a:mp:{uid}:{r}") for r in ROLE_ORDER]
    rows.append(btn("← Назад", back))
    return kb(*rows)


@on_input("m_add", "admins.manage")
async def in_add(message: Message, state: FSMContext, ctx: AppContext, data: dict) -> None:
    uid = parse_int(message.text, lo=1)
    if uid is None:
        await message.answer("Нужен числовой Telegram ID.")
        return
    await state.clear()
    await show(
        message,
        f"Какую роль выдать администратору <code>{uid}</code>?\n\nЛучше начинать с самой узкой — права можно расширить позже.",
        _role_picker(uid, "a:m"),
    )


@router.callback_query(F.data.startswith("a:mr:"))
async def cb_change_role(cb: CallbackQuery, ctx: AppContext) -> None:
    uid = int((cb.data or "").split(":")[2])
    await show(cb, f"Новая роль для администратора <code>{uid}</code>:", _role_picker(uid, "a:m"))


@router.callback_query(F.data.startswith("a:mp:"))
async def cb_pick_role(cb: CallbackQuery, ctx: AppContext) -> None:
    _, _, uid_s, role = (cb.data or "").split(":")
    uid = int(uid_s)
    if role not in ROLES:
        await _screen(cb, ctx, "Неизвестная роль")
        return
    async with ctx.session() as s:
        res = await s.execute(update(Admin).where(Admin.user_id == uid).values(role=role))
        if res.rowcount == 0:
            s.add(Admin(user_id=uid, added_by=cb.from_user.id, role=role))
        await s.commit()
    ctx.admin_ids.add(uid)
    ctx.admin_roles[uid] = role
    await ctx.bus.publish("admins")
    await ctx.audit.log(cb.from_user.id, "admin.role", uid, role=role)
    await _screen(cb, ctx, f"✅ Администратор <code>{uid}</code>: {ROLE_TITLES[role]}")


@router.callback_query(F.data.startswith("a:md:"))
async def cb_remove(cb: CallbackQuery, ctx: AppContext) -> None:
    uid = int((cb.data or "").split(":")[2])
    async with ctx.session() as s:
        await s.execute(delete(Admin).where(Admin.user_id == uid))
        await s.commit()
    ctx.admin_ids.discard(uid)
    ctx.admin_roles.pop(uid, None)
    await ctx.bus.publish("admins")
    await ctx.audit.log(cb.from_user.id, "admin.remove", uid)
    await _screen(cb, ctx, f"✅ Администратор <code>{uid}</code> убран")
