"""Фоновые задачи. Каждая работает в своём цикле; сбой одной не влияет на остальные.

Каждая итерация берёт Redis-блокировку задачи, поэтому воркеры можно запускать в нескольких экземплярах:
в каждый момент задачу выполняет ровно один. Исключение — watchdog: он должен работать, даже когда Redis недоступен.

Метрики этого процесса (job_seconds, job_errors_total, часть payments_total, panel_*) отдаёт /metrics контейнера
worker — его нужно скрейпить отдельным job в Prometheus (deploy/prometheus/prometheus.yml).
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import case, func, select, update

from app.bot import labels
from app.bot.ui import btn, kb
from app.core.subscription import Tier
from app.db.models import Payment, User
from app.infra.metrics import metrics
from app.logging_setup import log_user
from app.services.maintenance import cleanup
from app.services.notifier import BLOCKED, FAILED, OK
from app.utils import days_word, fmt_dt, to_local, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)

# имя -> интервал в секундах (используется и диагностикой в админке, и watchdog)
INTERVALS = {
    "expiry": 30,
    "payments": 60,
    "referrals": 180,
    "reminders": 600,
    "sync": 60,
    "autopay": 300,
    "broadcast": 5,
    "daily": 1800,
    "recon": 1800,
    "outbox": 30,
    "watchdog": 120,
    "links": 6 * 3600,
}
UNLOCKED = frozenset({"watchdog"})   # работают без Redis-блокировки (должны жить при падении Redis)
REMINDER_GIVE_UP_AFTER = 3           # сколько раз пробуем доставить напоминание при «постоянной» ошибке
BACKUP_MAX_ATTEMPTS = 6              # попыток бэкапа в сутки
RESTORE_TEST_MAX_AGE_DAYS = 40


def left_phrase(left: timedelta) -> str:
    n = math.ceil(left.total_seconds() / 86400)
    return "меньше чем через сутки" if n <= 1 else f"через {days_word(n)}"


class Workers:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    def start(self) -> None:
        jobs: dict[str, Callable[[], Awaitable[None]]] = {
            "expiry": self.expiry,
            "payments": self.payments,
            "referrals": self.referrals,
            "reminders": self.reminders,
            "sync": self.sync,
            "autopay": self.autopay,
            "broadcast": self.broadcast,
            "daily": self.daily,
            "recon": self.recon,
            "outbox": self.outbox,
            "watchdog": self.watchdog,
            "links": self.links,
        }
        for i, (name, fn) in enumerate(jobs.items()):
            self._tasks.append(
                asyncio.create_task(self._loop(name, INTERVALS[name], fn, 2 + i), name=f"worker-{name}")
            )

    async def stop(self, timeout: float = 15.0) -> None:
        """Мягкая остановка: новые итерации не начинаются, текущие дорабатывают (до ``timeout`` секунд), затем отмена."""
        self._stopping = True
        if not self._tasks:
            return
        _done, pending = await asyncio.wait(self._tasks, timeout=timeout)
        for t in pending:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run_once(self, name: str, fn: Callable[[], Awaitable[None]]) -> bool:
        t0 = time.monotonic()
        try:
            if name in UNLOCKED:
                await fn()
            else:
                async with self.ctx.locks.job(name, ttl=60.0) as got:
                    if not got:
                        return False
                    await fn()
            await self.ctx.beat(name)
            metrics.observe("job_seconds", time.monotonic() - t0, task=name)
            metrics.set_gauge("job_last_success_timestamp", time.time(), task=name)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("воркер %s: ошибка итерации", name)
            metrics.inc("job_errors_total", task=name)
            metrics.observe("job_seconds", time.monotonic() - t0, task=name)
            return False

    async def _loop(self, name: str, interval: int, fn: Callable[[], Awaitable[None]], delay: int) -> None:
        await asyncio.sleep(delay)
        while not self._stopping:
            t0 = time.monotonic()
            await self._run_once(name, fn)
            # спим короткими шагами, чтобы остановка не ждала целый интервал
            wake = t0 + max(1.0, interval)
            while not self._stopping and time.monotonic() < wake:
                await asyncio.sleep(min(1.0, max(0.0, wake - time.monotonic())))

    # ------------------------------------------------------------------ задачи
    async def expiry(self) -> None:
        """PREMIUM закончился -> LITE из очереди; всё закончилось -> снимаем подписку и уведомляем."""
        n = await self.ctx.subs.advance_due()
        if n:
            log.info("expiry: обработано %s", n)

    async def payments(self) -> None:
        """Добираем статусы платежей (ЮKassa и CryptoBot), по которым не пришло уведомление."""
        await self.ctx.pay.reconcile_pending()

    async def referrals(self) -> None:
        n = await self.ctx.refs.check_pending()
        if n:
            log.info("referrals: начислено наград %s", n)

    async def sync(self) -> None:
        """Повторяем синхронизацию с панелью (с экспоненциальной паузой) для тех, у кого она не удалась."""
        n = await self.ctx.subs.retry_pending()
        if n:
            log.info("sync: восстановлено %s", n)

    async def autopay(self) -> None:
        n = await self.ctx.pay.autopay_run()
        if n:
            log.info("autopay: обработано %s", n)

    async def broadcast(self) -> None:
        await self.ctx.bc.dispatch()

    async def outbox(self) -> None:
        """Доставляем важные сообщения (например, «оплата получена»), которые не ушли из-за сбоя сети."""
        n = await self.ctx.notify.drain_outbox()
        if n:
            log.info("outbox: доставлено %s", n)

    async def links(self) -> None:
        await self.ctx.links.check_all()

    async def recon(self) -> None:
        """Раз в сутки (после YOOKASSA_RECON_HOUR): сверка «ЮKassa → БД» — все успешные платежи за 48 часов учтены?"""
        ctx = self.ctx
        if ctx.yk is None:
            return
        local = to_local(utcnow())
        if local.hour < ctx.settings.yookassa_recon_hour:
            return
        key = f"ampire:recon:{local.date()}"
        try:
            if await ctx.redis.get(key):
                return
        except Exception:  # noqa: BLE001
            return  # без Redis не можем гарантировать «раз в сутки» — пропускаем итерацию
        await ctx.pay.reconcile_yookassa(hours=48)
        await ctx.redis.set(key, 1, ex=48 * 3600)   # флаг дня — только после успешной сверки

    async def watchdog(self) -> None:
        """Сторож: зависшие задачи, недоступные Redis/панель, устаревшие бэкапы, очередь недоставленных сообщений."""
        ctx = self.ctx
        now = utcnow()
        warmup = (now - ctx.started_at).total_seconds() < 300

        # Redis
        redis_ok = True
        try:
            await asyncio.wait_for(ctx.redis.ping(), 3)
        except Exception:  # noqa: BLE001
            redis_ok = False
        metrics.set_gauge("redis_up", 1 if redis_ok else 0)
        if not redis_ok:
            await ctx.notify.alert_once(
                "redis_down",
                "🚨 <b>Redis недоступен</b>. Бот продолжает отвечать (деградация: антиспам и состояние диалогов — в памяти, "
                "блокировки — внутри процесса), но фоновые задачи и общие лимиты не работают. Проверьте контейнер redis.",
                ttl=1800,
            )
            return  # остальное зависит от Redis

        # задачи воркера
        if not warmup:
            hb = await ctx.heartbeats()
            for name, interval in INTERVALS.items():
                if name == "watchdog":
                    continue
                last = hb.get(name)
                if last is None or (now - last).total_seconds() > interval * 3 + 90:
                    await ctx.notify.alert_once(
                        f"stale:{name}",
                        f"🚨 <b>Фоновая задача «{name}» не выполняется</b>"
                        + (f" с {fmt_dt(last)}" if last else " (сигналов нет)")
                        + ". Проверьте контейнер worker и журнал ошибок (Система → Журнал ошибок).",
                        ttl=3600,
                    )

        # панель
        metrics.set_gauge("panel_circuit_open", 1 if ctx.remna.circuit_open else 0)
        if ctx.remna.circuit_open:
            await ctx.notify.alert_once(
                "panel_down",
                "🚨 <b>Панель Remnawave не отвечает</b>: запросы приостановлены. Оплаты принимаются, доступ будет выдан "
                "автоматически, когда панель оживёт.",
                ttl=1800,
            )

        # исходящая очередь
        size = await ctx.notify.outbox_size()
        metrics.set_gauge("outbox_size", max(size, 0))
        if size > 100:
            await ctx.notify.alert_once("outbox_big", f"⚠️ В очереди недоставленных важных сообщений: {size}. Проверьте доступность Telegram.", ttl=3600)

        # платежи на ручной проверке
        async with ctx.session() as s:
            review = int(await s.scalar(select(func.count()).select_from(Payment).where(Payment.status == "needs_review")) or 0)
        metrics.set_gauge("payments_needs_review", review)

        # бэкапы и тест восстановления
        if not warmup:
            fresh = await ctx.backup.freshness()
            limit = ctx.settings.backup_max_age_h * 3600
            ages = [a for a in (fresh["pitr"], fresh["dump"]) if a is not None]
            if not ages or min(ages) > limit:
                await ctx.notify.alert_once(
                    "backup_stale",
                    f"🚨 <b>Нет свежей резервной копии БД</b> (последняя — "
                    f"{('%d ч назад' % (min(ages) // 3600)) if ages else 'не зафиксирована'}). "
                    "Проверьте контейнер pgbackrest и BACKUP_PASSWORD (docs/BACKUP.md).",
                    ttl=6 * 3600,
                )
            rt = fresh["restore_test"]
            if fresh["pitr"] is not None and (rt is None or rt > RESTORE_TEST_MAX_AGE_DAYS * 86400):
                await ctx.notify.alert_once(
                    "restore_test_stale",
                    f"⚠️ Тест восстановления из бэкапа не выполнялся более {RESTORE_TEST_MAX_AGE_DAYS} дней — "
                    "копия, которую не проверяли, не считается копией. Запустите deploy/backup/restore-test.sh.",
                    ttl=24 * 3600,
                )

    async def reminders(self) -> None:
        """Напоминания об окончании. Постранично (keyset), только тем, кому ещё не отправлено; отправка с общим лимитом.

        Напоминание считается отправленным, только если Telegram его принял (или пользователь заблокировал бота).
        При сбое сети/Telegram оно будет повторено на следующей итерации.
        """
        ctx = self.ctx
        if not ctx.cfg.get("reminders_enabled"):
            return
        days = sorted({int(d) for d in ctx.cfg.get("reminder_days")}, reverse=True)
        if not days:
            return
        t0 = time.monotonic()
        last_id = 0
        while time.monotonic() - t0 < 480 and not self._stopping:
            now = utcnow()
            # сколько порогов уже пройдено / сколько уже отправлено — считаем прямо в SQL, чтобы не читать лишнее
            needed = sum(case((User.expires_at <= now + timedelta(days=d), 1), else_=0) for d in days)
            done = case((User.notify_expiry == User.expires_at, User.notify_stage), else_=0)
            async with ctx.session() as s:
                users = (
                    await s.scalars(
                        select(User)
                        .where(
                            User.tier.is_not(None),
                            User.queued_lite_s == 0,  # если дальше идёт LITE — это не конец доступа
                            User.expires_at > now,
                            User.expires_at <= now + timedelta(days=days[0]),
                            User.is_banned.is_(False),
                            User.bot_blocked.is_(False),
                            User.id > last_id,
                            done < needed,
                        )
                        .order_by(User.id)
                        .limit(500)
                    )
                ).all()
            if not users:
                break
            last_id = users[-1].id
            for u in users:
                assert u.expires_at is not None
                left = u.expires_at - now
                stage = sum(1 for d in days if left <= timedelta(days=d))
                text = ctx.texts.render(
                    "n_expire_soon", tier=Tier(u.tier).title, until=fmt_dt(u.expires_at), left=left_phrase(left)
                )
                with log_user(u.id):
                    res = await ctx.notify.send_result(u.id, text, kb(btn(labels.RENEW, "pay:menu")), paced=True)
                    if res == FAILED and not await self._reminder_may_retry(u.id, u.expires_at):
                        res = BLOCKED  # устойчивая ошибка: прекращаем попытки, чтобы не повторять вечно
                if res not in (OK, BLOCKED):
                    metrics.inc("reminders_deferred_total")
                    continue  # не доставлено из-за сбоя — не отмечаем, повторим позже
                async with ctx.session() as s:
                    await s.execute(
                        update(User)
                        .where(User.id == u.id, User.expires_at == u.expires_at)
                        .values(notify_expiry=u.expires_at, notify_stage=stage)
                    )
                    await s.commit()
            if len(users) < 500:
                break

    async def _reminder_may_retry(self, user_id: int, expires_at) -> bool:  # noqa: ANN001
        """Ограничивает число повторов напоминания при постоянной ошибке отправки."""
        key = f"ampire:remind-fail:{user_id}:{int(expires_at.timestamp())}"
        try:
            n = int(await self.ctx.redis.incr(key))
            await self.ctx.redis.expire(key, 4 * 86400)
        except Exception:  # noqa: BLE001
            return False
        return n < REMINDER_GIVE_UP_AFTER

    async def daily(self) -> None:
        """Раз в сутки (после BACKUP_HOUR): зашифрованный дамп владельцам и очистка устаревших записей.

        Флаг «сделано» ставится только ПОСЛЕ успешной операции — сбой не означает потерянный день: следующая
        итерация (раз в 30 минут) повторит попытку, но не более ``BACKUP_MAX_ATTEMPTS`` раз в сутки.
        """
        ctx = self.ctx
        local = to_local(utcnow())
        if local.hour < ctx.settings.backup_hour:
            return
        day = local.date()

        backup_key = f"ampire:daily:backup:{day}"
        try:
            backup_done = bool(await ctx.redis.get(backup_key))
        except Exception:  # noqa: BLE001
            return
        if not backup_done:
            attempts = int(await ctx.redis.incr(f"ampire:daily:backup_attempts:{day}"))
            await ctx.redis.expire(f"ampire:daily:backup_attempts:{day}", 36 * 3600)
            targets = ctx.backup.targets()
            result = await ctx.backup.send(targets) if targets else "disabled"
            if result == "failed":
                if attempts >= BACKUP_MAX_ATTEMPTS:
                    await ctx.notify.alert_once("backup_failing", f"🚨 Бэкап БД не удаётся сделать уже {attempts} раз подряд — проверьте журнал ошибок.", ttl=6 * 3600)
                    await ctx.redis.set(backup_key, 1, ex=36 * 3600)   # на сегодня хватит — завтра попробуем снова
                else:
                    log.warning("бэкап не удался (попытка %s/%s) — повторю на следующей итерации", attempts, BACKUP_MAX_ATTEMPTS)
            else:
                await ctx.redis.set(backup_key, 1, ex=36 * 3600)

        cleanup_key = f"ampire:daily:cleanup:{day}"
        if not await ctx.redis.get(cleanup_key):
            await cleanup(ctx)
            await ctx.redis.set(cleanup_key, 1, ex=36 * 3600)
