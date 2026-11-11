from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.types import CallbackQuery

from app.bot.handlers.admin.common import make_router
from app.bot.ui import btn, kb, show, toast
from app.services.runtime_settings import REFERRAL_META
from app.utils import days_word, esc, fmt_dt, truncate

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)
router = make_router("referrals", scope="referrals.manage")


@router.callback_query(F.data == "a:r")
async def cb_menu(cb: CallbackQuery, ctx: AppContext) -> None:
    o = await ctx.refs.overview()
    text = (
        "🤝 <b>Реферальная система</b>\n\n"
        f"Приглашений всего: <b>{o['total']}</b>\n"
        f"✅ Награждено: <b>{o['rewarded']}</b> (выдано {days_word(o['days'])})\n"
        f"⏳ Ждут подключения: <b>{o['pending']}</b>\n"
        f"❌ Отклонено: {o['rejected']}\n\n"
        "<b>Условия:</b> награда начисляется, когда приглашённый реально воспользовался VPN "
        "(первое подключение или порог трафика).\n\nНастройки:"
    )
    rows = [btn(f"{title}: {ctx.cfg.format_value(key)}"[:62], f"a:sv:{key}:r") for key, title, _ in REFERRAL_META]
    rows += [
        [btn("🏆 Топ приглашающих", "a:rt"), btn("⏳ Ожидающие", "a:rp")],
        [btn("← Назад", "a:home")],
    ]
    await show(cb, text, kb(*rows))


@router.callback_query(F.data == "a:rt")
async def cb_top(cb: CallbackQuery, ctx: AppContext) -> None:
    top = await ctx.refs.top_inviters(10)
    lines = [
        f"{i}. {esc(truncate(u.display_name, 24))} — {n} (награждено {r})"
        for i, (u, n, r) in enumerate(top, 1)
    ] or ["Пока никто никого не пригласил."]
    rows = [btn(f"👤 {truncate(u.display_name, 24)}", f"a:u:{u.id}:card") for u, _, _ in top[:5]]
    rows.append(btn("← Назад", "a:r"))
    await show(cb, "🏆 <b>Топ приглашающих</b>\n\n" + "\n".join(lines), kb(*rows))


@router.callback_query(F.data == "a:rp")
async def cb_pending(cb: CallbackQuery, ctx: AppContext) -> None:
    items = await ctx.refs.pending_list(10)
    lines, rows = [], []
    for r, referred in items:
        name = esc(truncate(referred.display_name, 22)) if referred else str(r.referred_id)
        connected = "панель создана" if referred and referred.remna_id else "ещё не подключался"
        lines.append(f"#{r.id} · {name} ← <code>{r.referrer_id}</code> · {connected} · {fmt_dt(r.created_at)[:10]}")
        rows.append([btn(f"✅ #{r.id} начислить", f"a:rf:{r.id}"), btn(f"❌ #{r.id}", f"a:rx:{r.id}")])
    rows.append([btn("← Назад", "a:r")])
    text = "⏳ <b>Ожидающие рефералы</b>\n\n" + ("\n".join(lines) if lines else "Список пуст.")
    await show(cb, text, kb(*rows))


@router.callback_query(F.data.startswith("a:rf:"))
async def cb_force(cb: CallbackQuery, ctx: AppContext) -> None:
    rid = int((cb.data or "").split(":")[2])
    ok = await ctx.refs.try_reward(rid, force=True)
    await ctx.audit.log(cb.from_user.id, "referral.force_reward", rid, ok=ok)
    await toast(cb, "Награда начислена" if ok else "Не удалось начислить (уже обработан или есть лимит)", alert=not ok)
    await cb_pending(cb, ctx)


@router.callback_query(F.data.startswith("a:rx:"))
async def cb_reject(cb: CallbackQuery, ctx: AppContext) -> None:
    rid = int((cb.data or "").split(":")[2])
    await ctx.refs.reject(rid)
    await ctx.audit.log(cb.from_user.id, "referral.reject", rid)
    await cb_pending(cb, ctx)
