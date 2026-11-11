from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from aiogram import F
from aiogram.types import CallbackQuery

from app.bot.handlers.admin.common import make_router
from app.bot.ui import btn, confirm_kb, kb, show, toast
from app.integrations.remnawave import RemnaError
from app.utils import esc, fmt_dt, human_bytes, parse_iso

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)
router = make_router("infra", scope="infra.manage")


def _err(e: Exception) -> str:
    return f"⚠️ Панель не ответила: {esc(str(e))[:200]}"


@router.callback_query(F.data == "a:i")
async def cb_menu(cb: CallbackQuery, ctx: AppContext) -> None:
    try:
        st = await asyncio.wait_for(ctx.remna.system_stats(), 8)
    except (RemnaError, asyncio.TimeoutError) as e:
        await show(cb, _err(e), kb(btn("🔄 Повторить", "a:i"), btn("← Назад", "a:home")))
        return
    users = st.get("users") or {}
    online = st.get("onlineStats") or {}
    nodes = st.get("nodes") or {}
    counts = users.get("statusCounts") or {}
    lines = ["🌐 <b>Панель VPN (Remnawave)</b>\n"]
    if users:
        lines.append(f"Пользователей в панели: <b>{users.get('totalUsers', '—')}</b>")
    if counts:
        lines.append("По статусам: " + " · ".join(f"{k} {v}" for k, v in counts.items()))
    if online:
        lines.append(
            f"Онлайн: сейчас <b>{online.get('onlineNow', '—')}</b> · за сутки {online.get('lastDay', '—')} · "
            f"за неделю {online.get('lastWeek', '—')} · не подключались {online.get('neverOnline', '—')}"
        )
    if nodes:
        lines.append(f"Нод в сети: <b>{nodes.get('totalOnline', '—')}</b>")
    if st.get("uptime") is not None:
        lines.append(f"Аптайм панели: {int(float(st['uptime'])) // 3600} ч")
    markup = kb(
        [btn("🖥 Ноды", "a:in"), btn("🧩 Сквады", "a:iq")],
        [btn("🔄 Обновить", "a:i"), btn("← Назад", "a:home")],
    )
    await show(cb, "\n".join(lines), markup)


def _node_icon(n: dict[str, Any]) -> str:
    if n.get("isDisabled"):
        return "⛔️"
    if n.get("isConnected"):
        return "🟢"
    if n.get("isConnecting"):
        return "🟡"
    return "🔴"


async def _nodes_sorted(ctx: AppContext) -> list[dict[str, Any]]:
    return sorted(await ctx.remna.list_nodes(), key=lambda n: (str(n.get("name", "")).lower(), str(n.get("uuid", ""))))


@router.callback_query(F.data == "a:in")
async def cb_nodes(cb: CallbackQuery, ctx: AppContext) -> None:
    try:
        nodes = await _nodes_sorted(ctx)
    except RemnaError as e:
        await show(cb, _err(e), kb(btn("← Назад", "a:i")))
        return
    rows = [
        btn(f"{_node_icon(n)} {n.get('name', n.get('address', '?'))} · {n.get('usersOnline', 0)} онл.", f"a:inv:{i}")
        for i, n in enumerate(nodes)
    ]
    rows.append(btn("🔄 Обновить", "a:in"))
    rows.append(btn("← Назад", "a:i"))
    online = sum(1 for n in nodes if n.get("isConnected") and not n.get("isDisabled"))
    await show(cb, f"🖥 <b>Ноды</b> — в сети {online} из {len(nodes)}\n\n🟢 работает · 🟡 подключается · 🔴 нет связи · ⛔️ отключена", kb(*rows))


@router.callback_query(F.data.startswith("a:inv:"))
async def cb_node(cb: CallbackQuery, ctx: AppContext) -> None:
    idx = int((cb.data or "").split(":")[2])
    try:
        nodes = await _nodes_sorted(ctx)
        n = nodes[idx]
    except (RemnaError, IndexError) as e:
        await show(cb, _err(e) if isinstance(e, RemnaError) else "Список нод изменился — открой его заново.", kb(btn("← Назад", "a:in")))
        return
    u8 = str(n.get("uuid", ""))[:8]
    used = n.get("trafficUsedBytes")
    lines = [
        f"{_node_icon(n)} <b>{esc(n.get('name', '?'))}</b>",
        f"Адрес: <code>{esc(n.get('address', '—'))}</code>" + (f":{n['port']}" if n.get("port") else ""),
        f"Страна: {esc(n.get('countryCode') or '—')}",
        f"Пользователей онлайн: <b>{n.get('usersOnline', 0)}</b>",
        f"Xray: {esc(n.get('xrayVersion') or '—')}",
    ]
    if used is not None:
        lines.append(f"Трафик: {human_bytes(float(used))}")
    if n.get("lastStatusChange"):
        lines.append(f"Статус изменился: {fmt_dt(parse_iso(n['lastStatusChange']))}")
    if n.get("lastStatusMessage"):
        lines.append(f"Сообщение: {esc(str(n['lastStatusMessage'])[:150])}")
    toggle = ("✅ Включить", "enable") if n.get("isDisabled") else ("⛔️ Отключить", "disable")
    markup = kb(
        [btn(toggle[0], f"a:inc:{idx}:{toggle[1]}:{u8}"), btn("♻️ Перезапустить", f"a:inc:{idx}:restart:{u8}")],
        [btn("🔄 Обновить", f"a:inv:{idx}"), btn("← Назад", "a:in")],
    )
    await show(cb, "\n".join(lines), markup)


@router.callback_query(F.data.startswith("a:inc:"))
async def cb_node_confirm(cb: CallbackQuery) -> None:
    _, _, idx, action, u8 = (cb.data or "").split(":")
    verb = {"enable": "включить", "disable": "отключить", "restart": "перезапустить"}[action]
    await show(
        cb,
        f"Точно {verb} эту ноду? Пользователи на ней могут потерять соединение.",
        confirm_kb(f"a:ina:{idx}:{action}:{u8}", f"a:inv:{idx}", yes_text=f"Да, {verb}"),
    )


@router.callback_query(F.data.startswith("a:ina:"))
async def cb_node_action(cb: CallbackQuery, ctx: AppContext) -> None:
    _, _, idx, action, u8 = (cb.data or "").split(":")
    try:
        nodes = await _nodes_sorted(ctx)
        node = nodes[int(idx)]
        if not str(node.get("uuid", "")).startswith(u8):
            raise IndexError
        await ctx.remna.node_action(str(node["uuid"]), action)
    except IndexError:
        await toast(cb, "Список нод изменился — открой его заново.", alert=True)
        return
    except RemnaError as e:
        await toast(cb, f"Ошибка панели: {e}"[:190], alert=True)
        return
    await ctx.audit.log(cb.from_user.id, f"node.{action}", node.get("name"))
    await toast(cb, "Готово. Состояние обновится через несколько секунд.")
    await cb_node(cb, ctx)


@router.callback_query(F.data == "a:iq")
async def cb_squads(cb: CallbackQuery, ctx: AppContext) -> None:
    try:
        squads = sorted(await ctx.remna.list_squads(), key=lambda s: str(s.get("name", "")).lower())
    except RemnaError as e:
        await show(cb, _err(e), kb(btn("← Назад", "a:i")))
        return
    plans = {u: p.title for p in ctx.plans.all() for u in (p.squads or [])}
    lines = []
    for s in squads:
        info = s.get("info") or {}
        bound = f" → тариф {plans[str(s.get('uuid'))]}" if str(s.get("uuid")) in plans else ""
        lines.append(f"• <b>{esc(s.get('name', '?'))}</b> — участников {info.get('membersCount', '—')}, inbound'ов {info.get('inboundsCount', '—')}{bound}")
    text = "🧩 <b>Внутренние сквады</b>\n\n" + ("\n".join(lines) if lines else "Сквадов нет.")
    await show(cb, text, kb(btn("🏷 Привязка к тарифам", "a:t"), btn("← Назад", "a:i")))
