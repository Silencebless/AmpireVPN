from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.types import BufferedInputFile, CallbackQuery

from app.bot.handlers.admin.common import make_router
from app.bot.ui import btn, kb, show, toast
from app.utils import fmt_dt, money, sparkline, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)
router = make_router("dashboard", scope="stats.view")


def _pct(a: int, b: int) -> str:
    return f"{a / b * 100:.1f}%".replace(".", ",") if b else "—"


async def _panel_line(ctx: AppContext) -> str:
    try:
        st = await asyncio.wait_for(ctx.remna.system_stats(), 4)
    except Exception:  # noqa: BLE001
        return "🌐 Панель VPN: нет данных"
    online = (st.get("onlineStats") or {}).get("onlineNow")
    nodes_online = (st.get("nodes") or {}).get("totalOnline")
    parts = []
    if online is not None:
        parts.append(f"онлайн сейчас <b>{online}</b>")
    if nodes_online is not None:
        parts.append(f"нод в сети <b>{nodes_online}</b>")
    return "🌐 Панель VPN: " + (" · ".join(parts) if parts else "доступна")


PROV_EMOJI = {"yookassa": "💳", "cryptobot": "🪙"}


async def dashboard_text(ctx: AppContext, *, fresh: bool = False) -> str:
    s, rev14, usr14, panel = await asyncio.gather(
        ctx.stats.overview(fresh=fresh),
        ctx.stats.revenue_series(14),
        ctx.stats.signups_series(14),
        _panel_line(ctx),
    )
    rt, rn = s["rev_today"]
    r7, n7 = s["rev_7d"]
    r30, n30 = s["rev_30d"]
    rm, nm = s["rev_month"]
    ra, na = s["rev_all"]
    avg = (ra / na) if na else Decimal(0)
    arppu = (ra / s["payers"]) if s["payers"] else Decimal(0)
    ref_amount, ref_n = s["refunded"]
    split = s["plan_split"]
    split_line = " · ".join(
        f"{code.upper()} {n} ({money(total)})" for code, (n, total) in sorted(split.items())
    ) or "—"

    prov_line = " · ".join(
        f"{PROV_EMOJI.get(code, '•')} {n} ({money(total)})" for code, (n, total) in sorted(s.get("provider_split", {}).items())
    ) or "—"
    return (
        f"📊 <b>Статистика</b> · данные на {fmt_dt(s['now'])}\n\n"
        "👥 <b>Пользователи</b>\n"
        f"Всего: <b>{s['users_total']}</b>\n"
        f"Новые: 24ч <b>{s['new_24h']}</b> · 7д <b>{s['new_7d']}</b> · 30д <b>{s['new_30d']}</b>\n"
        f"Заблокировали бота: {s['blocked_bot']} · В бане: {s['banned']}\n\n"
        "🟢 <b>Подписки сейчас</b>\n"
        f"PREMIUM <b>{s['premium']}</b> · LITE <b>{s['lite']}</b> · на бесплатных днях {s['free_active']}\n"
        f"Истекают: 24ч <b>{s['exp_24h']}</b> · 72ч <b>{s['exp_72h']}</b>\n"
        f"Потеряно за 7 дней: {s['lost_7d']}\n\n"
        "💰 <b>Выручка</b>\n"
        f"Сегодня: <b>{money(rt)}</b> ({rn})\n"
        f"7 дней: <b>{money(r7)}</b> ({n7}) · 30 дней: <b>{money(r30)}</b> ({n30})\n"
        f"Этот месяц: <b>{money(rm)}</b> ({nm}) · Всего: <b>{money(ra)}</b> ({na})\n"
        f"Средний чек: {money(avg.quantize(Decimal('1')))} · Платящих: {s['payers']} · ARPPU: {money(arppu.quantize(Decimal('1')))}\n"
        f"Возвраты: {ref_n} ({money(ref_amount)}) · Ждут оплаты: {s['pending_payments']}\n\n"
        "🎯 <b>Воронка и продажи (30 дн.)</b>\n"
        f"Брали пробный: {s['trial_used']} → оплатили: {s['trial_paid']} ({_pct(s['trial_paid'], s['trial_used'])})\n"
        f"Тарифы: {split_line}\n"
        f"Способы оплаты: {prov_line}\n"
        f"🔁 Автопродление включено у: {s.get('autopay_users', 0)}\n"
        f"Апгрейдов LITE→PREMIUM: {s['upgrades_30d']} · Скидки по промокодам: {money(s['discounts_30d'])}\n"
        f"Промокодов: {s['promo_total']} · Активаций: {s['promo_uses']}\n\n"
        f"{panel}\n\n"
        f"📈 Выручка, 14 дн.\n<code>{sparkline([float(v) for _, v, _ in rev14])}</code>\n"
        f"👥 Регистрации, 14 дн.\n<code>{sparkline([float(n) for _, n in usr14])}</code>"
    )


def _kb():
    return kb(
        [btn("🔄 Обновить", "a:st:r"), btn("🏠 Админ-панель", "a:home")],
        [btn("📈 График выручки", "a:st:chart:rev"), btn("👥 График роста", "a:st:chart:usr")],
        [btn("📥 Пользователи CSV", "a:st:csv:users"), btn("📥 Платежи CSV", "a:st:csv:pays")],
    )


@router.callback_query(F.data == "a:st")
async def cb_dashboard(cb: CallbackQuery, ctx: AppContext) -> None:
    await show(cb, await dashboard_text(ctx), _kb())


@router.callback_query(F.data == "a:st:r")
async def cb_dashboard_refresh(cb: CallbackQuery, ctx: AppContext) -> None:
    await show(cb, await dashboard_text(ctx, fresh=True), _kb())


@router.callback_query(F.data.startswith("a:st:chart:"))
async def cb_chart(cb: CallbackQuery, ctx: AppContext) -> None:
    kind = (cb.data or "").split(":")[3]
    await toast(cb, "Строю график…")
    png = await (ctx.stats.revenue_chart(30) if kind == "rev" else ctx.stats.signups_chart(30))
    name = "revenue.png" if kind == "rev" else "signups.png"
    await ctx.bot.send_photo(cb.from_user.id, BufferedInputFile(png, name))


@router.callback_query(F.data.startswith("a:st:csv:"))
async def cb_csv(cb: CallbackQuery, ctx: AppContext) -> None:
    kind = (cb.data or "").split(":")[3]
    await toast(cb, "Готовлю файл…")
    if kind == "users":
        data, name = await ctx.stats.export_users_csv(), "users.csv"
    else:
        data, name = await ctx.stats.export_payments_csv(), "payments.csv"
    await ctx.bot.send_document(cb.from_user.id, BufferedInputFile(data, name), caption=f"📥 {name} · {fmt_dt(utcnow())}")
    await ctx.audit.log(cb.from_user.id, "export.csv", name)
