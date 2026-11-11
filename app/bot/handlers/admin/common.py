"""Общий каркас админки: фабрика роутеров, реестр обработчиков текстового ввода, главный экран."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.filters import HasScope, IsAdmin, IsOwner, NotCommand
from app.bot.states import AdminFlow
from app.bot.ui import btn, kb, show, toast
from app.utils import fmt_dt, money, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

InputHandler = Callable[[Message, FSMContext, "AppContext", dict[str, Any]], Awaitable[None]]
_INPUTS: dict[str, tuple[InputHandler, str | None]] = {}


def make_router(name: str, *, owner_only: bool = False, scope: str | None = None) -> Router:
    """Роутер админки: только владельцы (owner_only), администраторы с правом ``scope`` или любые админы.

    Права зависят от роли (support / billing / marketing / ops / admin) — см. app/permissions.py.
    """
    r = Router(name=f"admin.{name}")
    flt = IsOwner() if owner_only else (HasScope(scope) if scope else IsAdmin())
    r.message.filter(flt)
    r.callback_query.filter(flt)
    return r


def on_input(action: str, scope: str | None = None) -> Callable[[InputHandler], InputHandler]:
    """Регистрирует обработчик текстового ввода. ``scope`` проверяется в момент ввода (роль могли сменить)."""

    def deco(fn: InputHandler) -> InputHandler:
        _INPUTS[action] = (fn, scope)
        return fn

    return deco


async def ask(
    event: Message | CallbackQuery,
    state: FSMContext,
    action: str,
    prompt: str,
    back_cb: str,
    *,
    keep: bool = False,
    **data: Any,
) -> None:
    """Просит админа прислать текст; результат придёт в обработчик, зарегистрированный через on_input."""
    current = await state.get_data() if keep else {}
    await state.set_state(AdminFlow.input)
    await state.set_data({**current, "action": action, **data})
    await show(event, prompt, kb(btn("Отмена", back_cb)))


router = make_router("common")


@router.message(AdminFlow.input, NotCommand())
async def on_any_input(message: Message, state: FSMContext, ctx: AppContext) -> None:
    data = await state.get_data()
    entry = _INPUTS.get(str(data.get("action")))
    if entry is None:
        await state.clear()
        return
    handler, scope = entry
    if scope is not None and not ctx.has_scope(message.from_user.id, scope):
        await state.clear()
        await message.answer("Недостаточно прав для этого действия.")
        return
    await handler(message, state, ctx, data)


async def home_screen(ctx: AppContext, user_id: int):
    """Главный экран админки: сводка (только при праве на статистику) и кнопки, доступные роли."""
    can = lambda scope: ctx.has_scope(user_id, scope)  # noqa: E731
    head = f"<b>Админ-панель · AmpireVPN</b>\nРоль — {ctx.role_label(user_id)}\n\n"
    if can("stats.view"):
        s = await ctx.stats.overview()
        rev_today, n_today = s["rev_today"]
        text = (
            head
            + f"Пользователей: <b>{s['users_total']}</b> (за сутки +{s['new_24h']})\n"
            f"Активных подписок: <b>{s['premium'] + s['lite']}</b> (PREMIUM {s['premium']} · LITE {s['lite']})\n"
            f"Сегодня: <b>{money(rev_today)}</b> ({n_today} оплат)\n"
            f"Платежей в ожидании: {s['pending_payments']}"
            + (f"\n⚠️ Платежей на проверке: <b>{s['needs_review']}</b>" if s.get("needs_review") else "")
            + (f"\n⚠️ Не синхронизировано с панелью: <b>{s['unsynced']}</b>" if s["unsynced"] else "")
            + f"\n\n<i>Обновлено {fmt_dt(utcnow())}</i>"
        )
    else:
        text = head + f"<i>Обновлено {fmt_dt(utcnow())}</i>"

    def row(*pairs: tuple[str, str, str | None]) -> list:
        return [btn(t, cb) for t, cb, scope in pairs if scope is None or can(scope)]

    rows = [
        row(("📊 Статистика", "a:st", "stats.view"), ("👥 Пользователи", "a:u", "users.view")),
        row(("💳 Платежи", "a:pl:all:0", "payments.view"), ("🏷 Тарифы", "a:t", "plans.edit")),
        row(("🎟 Промокоды", "a:cl:0", "promos.manage"), ("🤝 Рефералка", "a:r", "referrals.manage")),
        row(("📣 Рассылки", "a:b", "broadcast.send"), ("✏️ Тексты", "a:x:0", "texts.edit")),
        row(("🌐 Панель VPN", "a:i", "infra.manage"), ("⚙️ Настройки", "a:s", "settings.edit")),
        row(("🧰 Система", "a:y", "system.view"), ("🛡 Админы", "a:m", "admins.manage")),
        [btn("← В меню бота", "menu:main")],
    ]
    return text, kb(*rows)


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext, ctx: AppContext) -> None:
    await state.clear()
    text, markup = await home_screen(ctx, message.from_user.id)
    await show(message, text, markup)


@router.callback_query(F.data == "a:home")
async def cb_home(cb: CallbackQuery, ctx: AppContext) -> None:
    text, markup = await home_screen(ctx, cb.from_user.id)
    await show(cb, text, markup)


async def need_scope(cb: CallbackQuery, ctx: AppContext, scope: str) -> bool:
    """True, если у администратора нет права на действие — уже показали уведомление."""
    if ctx.has_scope(cb.from_user.id, scope):
        return False
    await toast(cb, "Недостаточно прав для этого действия.", alert=True)
    return True


async def need_owner(cb: CallbackQuery, ctx: AppContext) -> bool:
    """True, если действие запрещено (не владелец) — уже показали уведомление."""
    if ctx.is_owner(cb.from_user.id):
        return False
    await toast(cb, "Это действие доступно только владельцу.", alert=True)
    return True


# Замыкающий обработчик: нажатие админской кнопки без нужного права не должно «зависать» без ответа.
denied_router = Router(name="admin.denied")


@denied_router.callback_query(F.data.startswith("a:"), IsAdmin())
async def cb_denied(cb: CallbackQuery) -> None:
    await toast(cb, "Недостаточно прав для этого действия.", alert=True)
