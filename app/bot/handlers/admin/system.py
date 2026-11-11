from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.types import CallbackQuery
from sqlalchemy import func, or_, select, text as sql_text

from app.bot.handlers.admin.common import make_router, need_owner
from app.bot.ui import btn, confirm_kb, kb, show, toast
from app.db.models import User
from app.logging_setup import LOG_BUFFER, read_shared_errors
from app.utils import esc, fmt_dt, spawn, truncate, utcnow
from app.workers.runner import INTERVALS

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)
router = make_router("system", scope="system.view")

WORKER_INTERVALS = INTERVALS  # ожидаемый интервал воркеров, сек (для оценки «завис ли воркер»)


@router.callback_query(F.data == "a:y")
async def cb_menu(cb: CallbackQuery) -> None:
    markup = kb(
        [btn("🩺 Диагностика", "a:yd"), btn("💾 Бэкап БД", "a:yb")],
        [btn("🔄 Ресинк всех в панель", "a:yr"), btn("⚠️ Журнал ошибок", "a:ye")],
        [btn("🔎 Сверка ЮKassa → БД", "a:yk"), btn("🧾 Журнал действий", "a:ya")],
        [btn("← Назад", "a:home")],
    )
    await show(cb, "🧰 <b>Система</b>\n\nДиагностика, резервные копии и обслуживание — без консоли.", markup)


async def _timed(coro, timeout: float = 6.0):
    t0 = time.perf_counter()
    res = await asyncio.wait_for(coro, timeout)
    return res, int((time.perf_counter() - t0) * 1000)


class _Skip(Exception):
    """Проверка не нужна (компонент не настроен)."""


@router.callback_query(F.data == "a:yd")
async def cb_diag(cb: CallbackQuery, ctx: AppContext) -> None:
    st = ctx.settings
    lines: list[str] = ["🩺 <b>Диагностика</b>\n"]
    warn: list[str] = []

    async def check(title: str, fn) -> None:
        try:
            detail = await fn()
            lines.append(f"✅ {title}{(' — ' + detail) if detail else ''}")
        except _Skip as e:
            lines.append(f"⚪️ {title} — {e}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"❌ {title} — {esc(str(e) or type(e).__name__)[:120]}")

    async def db() -> str:
        async with ctx.session() as s:
            _, ms = await _timed(s.execute(sql_text("select 1")))
            n = await s.scalar(select(func.count()).select_from(User))
        return f"{ms} мс, пользователей {n}"

    async def redis() -> str:
        _, ms = await _timed(ctx.redis.ping())
        return f"{ms} мс"

    async def remna() -> str:
        _, ms = await _timed(ctx.remna.system_stats())
        return f"{ms} мс"

    async def yk() -> str:
        if ctx.yk is None:
            raise _Skip("не подключена (YOOKASSA_*)")
        me, ms = await _timed(ctx.yk.me())
        mode = "тестовый магазин" if me.get("test") else "боевой магазин"
        fiscal = me.get("fiscalization_enabled")
        if fiscal and not ctx.cfg.get("receipts_enabled"):
            warn.append("в ЮKassa включены чеки (фискализация), а бот не передаёт данные чека — платежи будут отклоняться. Включи «Чеки» в Настройках.")
        fiscal_txt = ("чеки ЮKassa: " + ("вкл" if fiscal else "выкл")) if fiscal is not None else ""
        return ", ".join(x for x in (mode, fiscal_txt, f"{ms} мс") if x)

    async def crypto() -> str:
        if ctx.crypto is None:
            raise _Skip("не подключён (CRYPTOBOT_TOKEN)")
        me, ms = await _timed(ctx.crypto.get_me())
        net = "testnet" if st.cryptobot_testnet else "боевой"
        return f"{net}, приложение «{esc(me.get('name', '?'))}», {ms} мс"

    await check("PostgreSQL", db)
    await check("Redis", redis)
    await check("Remnawave API", remna)
    await check("ЮKassa API", yk)
    await check("CryptoBot API", crypto)

    for p in ctx.plans.all():
        if p.is_active and not p.squads:
            warn.append(f"у тарифа {p.title} не выбраны сквады — подписчики не получат серверы (Тарифы → Сквады)")
    if not ctx.support_url:
        warn.append("не задана ссылка поддержки (Настройки или SUPPORT_URL)")
    if st.base_url.startswith("http://"):
        warn.append("PUBLIC_BASE_URL без https — вебхуки и страницы Happ не заработают")
    if st.webhook_secret in ("", "change-me"):
        warn.append("WEBHOOK_SECRET не изменён — задай случайную строку")
    if not st.owners:
        warn.append("OWNER_IDS пуст — некому управлять ботом")
    if not st.remnawave_webhook_secret:
        warn.append("REMNAWAVE_WEBHOOK_SECRET пуст — вебхуки Remnawave (мгновенные награды, алерты нод) отключены")
    if st.backup_telegram and not st.backup_password:
        warn.append("BACKUP_PASSWORD не задан — дамп в Telegram НЕ отправляется (открытый дамп содержит ссылки доступа, email и платежи)")
    if ctx.remna.circuit_open:
        warn.append("панель Remnawave сейчас не отвечает — запросы временно приостановлены")

    now = utcnow()
    try:
        ov = await ctx.stats.overview()
        lines.append("")
        lines.append(
            f"📊 Очереди: ждут оплаты {ov['pending_payments']} · на проверке {ov.get('needs_review', 0)} · "
            f"не синхронизировано с панелью {ov['unsynced']} · автопродление у {ov.get('autopay_users', 0)}"
        )
        if ov.get("needs_review"):
            warn.append(f"платежей на ручной проверке: {ov['needs_review']} (Платежи → «На проверке»)")
    except Exception:  # noqa: BLE001
        pass

    # резервные копии: PITR (pgBackRest), дамп в Telegram, тест восстановления
    fresh = await ctx.backup.freshness()

    def age(sec: int | None) -> str:
        return "нет отметки" if sec is None else (f"{sec // 3600} ч назад" if sec < 172800 else f"{sec // 86400} дн. назад")

    lines.append(f"💾 Копии: WAL/PITR — {age(fresh['pitr'])} · дамп — {age(fresh['dump'])} · тест восстановления — {age(fresh['restore_test'])}")
    ages = [a for a in (fresh["pitr"], fresh["dump"]) if a is not None]
    if not ages or min(ages) > st.backup_max_age_h * 3600:
        warn.append("нет свежей резервной копии БД — настройте PITR (docs/BACKUP.md)")
    outbox = await ctx.notify.outbox_size()
    if outbox > 0:
        lines.append(f"📬 Недоставленных важных сообщений в очереди: {outbox}")
    links = await ctx.links.report()
    dead = [k for k, status, _c, _a in links if status == "dead"]
    lines.append("🔗 Ссылки Happ: " + ("⚠️ не работают — " + ", ".join(dead) if dead else ("все доступны" if links else "ещё не проверялись")))
    if dead:
        warn.append("часть ссылок на скачивание Happ не работает — проверьте app/web/pages.py")

    hb = await ctx.heartbeats()
    stale = [
        name for name, interval in WORKER_INTERVALS.items()
        if name not in hb or (now - hb[name]).total_seconds() > interval * 3 + 60
    ]
    wh = {n: await ctx.last_webhook(n) for n in ("yookassa", "cryptobot", "remnawave")}
    lines.append(
        "🔔 Вебхуки: ЮKassa — " + (fmt_dt(wh["yookassa"]) if wh["yookassa"] else "не приходили")
        + " · CryptoBot — " + (fmt_dt(wh["cryptobot"]) if wh["cryptobot"] else "не приходили")
        + " · Remnawave — " + (fmt_dt(wh["remnawave"]) if wh["remnawave"] else "не приходили")
    )
    if ctx.crypto is not None and wh["cryptobot"] is None:
        lines.append("   ℹ️ Вебхук CryptoBot задаётся в @CryptoBot → Crypto Pay → приложение → Webhooks. Без него оплата подтверждается проверкой раз в минуту.")
    lines.append(f"⚙️ Воркеры: {'все работают' if not stale else '⚠️ нет сигнала: ' + ', '.join(stale)}")
    if st.role == "api" and stale:
        warn.append("в этом процессе воркеров нет (APP_ROLE=api) — должен работать отдельный процесс с APP_ROLE=worker")
    mode = "webhook" if st.webhook_mode else "polling"
    lines.append(f"🧩 Роль: {st.role} · Telegram: {mode} · instance {ctx.instance_id} · @{ctx.bot_username}")
    lines.append(f"⏱ Аптайм этой реплики: {int((now - ctx.started_at).total_seconds() // 3600)} ч")
    if warn:
        lines.append("\n⚠️ <b>Что стоит поправить:</b>")
        lines += [f"• {esc(w)}" for w in warn]
    else:
        lines.append("\n🎉 Замечаний нет.")
    await show(cb, "\n".join(lines), kb(btn("🔄 Повторить", "a:yd"), btn("← Назад", "a:y")))


@router.callback_query(F.data == "a:yb")
async def cb_backup(cb: CallbackQuery, ctx: AppContext) -> None:
    if await need_owner(cb, ctx):
        return
    await toast(cb, "Делаю бэкап… файл придёт отдельным сообщением.")
    result = await ctx.backup.send({cb.from_user.id})
    await ctx.audit.log(cb.from_user.id, "backup.manual", result=result)
    notes = {
        "no_password": "Дамп не отправлен: не задан BACKUP_PASSWORD (открытые дампы бот не отправляет).",
        "disabled": "Отправка дампа в Telegram выключена (BACKUP_TELEGRAM=false) — используйте PITR: docs/BACKUP.md.",
    }
    if result in notes:
        await ctx.notify.send(cb.from_user.id, "🔐 " + notes[result])


@router.callback_query(F.data == "a:yk")
async def cb_recon_ask(cb: CallbackQuery, ctx: AppContext) -> None:
    if await need_owner(cb, ctx):
        return
    if ctx.yk is None:
        await toast(cb, "ЮKassa не подключена", alert=True)
        return
    await show(
        cb,
        "🔎 <b>Сверка ЮKassa → БД</b>\n\nПроверим все успешные платежи ЮKassa за 72 часа: каждый ли учтён в боте. "
        "Недостающие начисления выполнятся автоматически, неизвестные платежи уйдут на проверку. Отчёт придёт сообщением.",
        confirm_kb("a:yko", "a:y", yes_text="Запустить сверку"),
    )


async def _run_recon(ctx: AppContext, admin_id: int) -> None:
    try:
        stats = await ctx.pay.reconcile_yookassa(hours=72)
    except Exception as e:  # noqa: BLE001
        log.exception("ручная сверка ЮKassa")
        await ctx.notify.send(admin_id, f"❌ Сверка не удалась: {esc(str(e))[:300]}")
        return
    await ctx.notify.send(
        admin_id,
        f"✅ Сверка завершена: платежей {stats.get('seen', 0)}, исправлено {stats.get('fixed', 0)}, "
        f"на проверке {stats.get('review', 0)}, неизвестных {stats.get('missing', 0)}.",
    )


@router.callback_query(F.data == "a:yko")
async def cb_recon(cb: CallbackQuery, ctx: AppContext) -> None:
    if await need_owner(cb, ctx):
        return
    spawn(_run_recon(ctx, cb.from_user.id))
    await ctx.audit.log(cb.from_user.id, "recon.yookassa")
    await show(cb, "🔎 Сверка запущена в фоне — пришлю отчёт.", kb(btn("← Назад", "a:y")))


@router.callback_query(F.data == "a:yr")
async def cb_resync_ask(cb: CallbackQuery, ctx: AppContext) -> None:
    if await need_owner(cb, ctx):
        return
    async with ctx.session() as s:
        n = int(await s.scalar(select(func.count()).select_from(User).where(or_(User.remna_id.is_not(None), User.tier.is_not(None)))) or 0)
    await show(
        cb,
        f"🔄 Отправить в панель актуальные срок, лимит устройств и сквады для <b>{n}</b> пользователей? "
        "Это безопасно, но займёт время — отчёт придёт сообщением.",
        confirm_kb("a:yro", "a:y", yes_text="Да, запустить"),
    )


async def _resync_all(ctx: AppContext, admin_id: int) -> None:
    async with ctx.session() as s:
        ids = list((await s.scalars(select(User.id).where(or_(User.remna_id.is_not(None), User.tier.is_not(None))))).all())
    ok, fail = await ctx.subs.resync_many(ids)
    await ctx.notify.send(admin_id, f"✅ Ресинк завершён: успешно {ok}, с ошибкой {fail} (из {len(ids)}).")


@router.callback_query(F.data == "a:yro")
async def cb_resync(cb: CallbackQuery, ctx: AppContext) -> None:
    if await need_owner(cb, ctx):
        return
    spawn(_resync_all(ctx, cb.from_user.id))
    await ctx.audit.log(cb.from_user.id, "resync.all")
    await show(cb, "🔄 Ресинк запущен в фоне — пришлю отчёт.", kb(btn("← Назад", "a:y")))


@router.callback_query(F.data == "a:ye")
async def cb_errors(cb: CallbackQuery, ctx: AppContext) -> None:
    """Общий журнал ошибок всех реплик (Redis); если Redis недоступен — локальный буфер этой реплики."""
    shared = await read_shared_errors(ctx.redis, 12)
    lines = shared or list(reversed(list(LOG_BUFFER)[-12:]))
    source = "всех реплик" if shared else "этой реплики"
    body = "\n".join(truncate(esc(x), 300) for x in lines) or "Ошибок и предупреждений нет 🎉"
    await show(cb, f"⚠️ <b>Последние предупреждения и ошибки</b> ({source})\n\n<pre>{body[:3300]}</pre>", kb(btn("🔄 Обновить", "a:ye"), btn("← Назад", "a:y")))


@router.callback_query(F.data == "a:ya")
async def cb_audit(cb: CallbackQuery, ctx: AppContext) -> None:
    rows = await ctx.audit.recent(20)
    lines = [
        f"{fmt_dt(r.created_at)[:16]} · <code>{r.actor_id or '—'}</code> · {esc(r.action)}"
        + (f" · {esc(truncate(str(r.target), 20))}" if r.target else "")
        for r in rows
    ] or ["Пока пусто."]
    await show(cb, "🧾 <b>Журнал действий</b> (последние 20)\n\n" + "\n".join(lines), kb(btn("🔄 Обновить", "a:ya"), btn("← Назад", "a:y")))
