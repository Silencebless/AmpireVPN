"""Точка входа. Роли процесса (APP_ROLE):

* all    — всё в одном: бот (polling или webhook) + веб + воркеры (по умолчанию, для небольших установок);
* api    — веб + приём апдейтов Telegram по webhook; без воркеров. Запускай несколько реплик за балансировщиком;
* worker — только фоновые задачи (напоминания, истечение, платежи, рассылки, автопродление, бэкапы).
"""
from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand, BotCommandScopeChat
from redis.asyncio import Redis
from sqlalchemy import text

from app.bot.factory import create_dispatcher
from app.config import Settings, get_settings
from app.context import AppContext
from app.db.base import create_engine, create_sessionmaker
from app.infra.failopen import FailOpenStorage
from app.integrations.cryptobot import CryptoBotClient
from app.integrations.remnawave import RemnaClient
from app.integrations.yookassa import YooKassaClient
from app.logging_setup import LogSink, attach_sink, setup_logging
from app.migrate import ensure_schema
from app.services import build_services
from app.startup_checks import StartupError, check_plans, collect
from app.utils import drain_tasks, set_timezone, spawn
from app.web.server import start_web
from app.workers.runner import Workers

log = logging.getLogger("app")


def init_sentry(dsn: str) -> None:
    if not dsn:
        return
    try:
        import sentry_sdk

        sentry_sdk.init(dsn=dsn, traces_sample_rate=0.0, send_default_pii=False, release="ampirevpn-bot")
        log.info("Sentry подключён")
    except Exception:  # noqa: BLE001
        log.warning("Не удалось инициализировать Sentry", exc_info=True)


async def _wait_db(engine, attempts: int = 30) -> None:
    for i in range(1, attempts + 1):
        try:
            async with engine.connect() as conn:
                await conn.execute(text("select 1"))
            return
        except Exception as e:  # noqa: BLE001
            log.warning("БД ещё не готова (%s/%s): %s", i, attempts, e)
            await asyncio.sleep(2)
    raise RuntimeError("Не удалось подключиться к базе данных")


async def _periodic_reload(handlers: dict[str, Callable[[], Awaitable[None]]], interval: int = 60) -> None:
    """Страховка к pub/sub: раз в минуту перечитываем настройки/тексты/тарифы/админов."""
    while True:
        await asyncio.sleep(interval)
        for name, fn in handlers.items():
            try:
                await fn()
            except Exception:  # noqa: BLE001
                log.warning("перезагрузка %s не удалась", name, exc_info=True)


def _validate(settings: Settings) -> None:
    """Проверка конфигурации. В production любая критичная проблема останавливает запуск (с понятным списком)."""
    errors, warns = collect(settings)
    for w in warns:
        log.warning("Конфигурация: %s", w)
    if errors:
        for e in errors:
            log.critical("Конфигурация: %s", e)
        raise SystemExit("Запуск остановлен — исправьте настройки (.env):\n  • " + "\n  • ".join(errors))
    if settings.role == "api" and not settings.webhook_mode:
        raise SystemExit("APP_ROLE=api работает только с TELEGRAM_MODE=webhook (несколько реплик не могут вместе опрашивать Telegram)")
    if settings.webhook_mode and not settings.base_url.startswith("https://"):
        raise SystemExit("TELEGRAM_MODE=webhook требует PUBLIC_BASE_URL с https://")


async def main() -> None:
    settings = get_settings()
    role = settings.role
    setup_logging(settings.log_level, json_logs=settings.log_json, role=role)
    set_timezone(settings.timezone)
    init_sentry(settings.sentry_dsn)
    _validate(settings)
    log.info(
        "AmpireVPN: роль=%s, режим Telegram=%s, окружение=%s",
        role,
        "webhook" if settings.webhook_mode else "polling",
        "production" if settings.is_production else "development",
    )

    # миграции — по прямому адресу БД (PgBouncer для advisory-lock не подходит), без серверных таймаутов
    mig_engine = create_engine(
        settings.migration_database_url or settings.database_url, pool_size=3, max_overflow=0, command_timeout=None, app_name="ampirevpn-migrate"
    )
    try:
        await _wait_db(mig_engine)
        await ensure_schema(mig_engine, strict=settings.is_production)
    except StartupError as e:
        raise SystemExit(str(e)) from e
    finally:
        await mig_engine.dispose()

    pgb = settings.db_pgbouncer
    engine = create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pgbouncer=pgb,
        pool_timeout=settings.db_pool_timeout,
        statement_timeout_ms=settings.db_statement_timeout_ms,
        idle_tx_timeout_ms=settings.db_idle_tx_timeout_ms,
        app_name=f"ampirevpn-{role}",
    )
    # отдельный крошечный пул для /readyz: проверка готовности не конкурирует с ботом за соединения
    health_engine = create_engine(
        settings.database_url, pool_size=1, max_overflow=0, pgbouncer=pgb, pool_timeout=2.0,
        statement_timeout_ms=3000, idle_tx_timeout_ms=5000, command_timeout=5.0, app_name="ampirevpn-health",
    )
    sessionmaker = create_sessionmaker(engine)

    redis = Redis.from_url(settings.redis_url, socket_connect_timeout=3, socket_timeout=5, health_check_interval=30)
    sink = LogSink(redis)
    attach_sink(sink)
    sink.start()
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    remna = RemnaClient(
        settings.remnawave_url,
        settings.remnawave_token,
        proxy_headers=settings.remnawave_proxy_headers,
        caddy_key=settings.remnawave_caddy_key,
        cookie=settings.remnawave_cookie,
        timeout=settings.panel_timeout,
        concurrency=settings.panel_concurrency,
    )
    yk = YooKassaClient(settings.yookassa_shop_id, settings.yookassa_secret_key) if settings.yookassa_enabled else None
    crypto = CryptoBotClient(settings.cryptobot_token, testnet=settings.cryptobot_testnet) if settings.cryptobot_enabled else None

    ctx = AppContext(settings, sessionmaker, redis, bot, remna, yk, crypto)
    ctx.health_engine = health_engine
    build_services(ctx)

    me = await bot.get_me()
    ctx.bot_username = me.username or ""
    log.info("Бот: @%s (instance %s)", ctx.bot_username, ctx.instance_id)

    await ctx.cfg.load()
    await ctx.texts.load()
    await ctx.plans.ensure_defaults()
    await ctx.reload_admins()

    # тариф без сквадов продавал бы подписку без серверов — в production это ошибка запуска
    plan_errors, plan_warns = check_plans(ctx.plans.all(), allow_empty_squads=settings.allow_empty_squads, prod=settings.is_production)
    for w in plan_warns:
        log.warning("Тарифы: %s", w)
    if plan_errors:
        for e in plan_errors:
            log.critical("Тарифы: %s", e)
        raise SystemExit("Запуск остановлен — укажите сквады тарифов (LITE_SQUADS / PREMIUM_SQUADS):\n  • " + "\n  • ".join(plan_errors))

    # реплики узнают об изменениях настроек/текстов/тарифов/админов через шину + периодическую перезагрузку
    reload_handlers = {
        "settings": ctx.cfg.load,
        "texts": ctx.texts.load,
        "plans": ctx.plans.reload,
        "admins": ctx.reload_admins,
    }
    bg = [spawn(ctx.bus.listen(reload_handlers)), spawn(_periodic_reload(reload_handlers))]

    dp = None
    storage = None
    if role != "worker":
        # состояние диалогов в Redis; при его падении бот продолжает отвечать (FailOpenStorage)
        storage = FailOpenStorage(RedisStorage(redis, state_ttl=86400, data_ttl=86400))
        dp = create_dispatcher(ctx, storage)

    web_runner = await start_web(ctx, dp if (settings.webhook_mode and dp is not None) else None)
    workers = Workers(ctx) if role in ("all", "worker") else None
    if workers:
        workers.start()

    stop = asyncio.Event()
    try:
        if role != "worker":
            await _setup_bot_commands(bot, settings)
            try:
                if await redis.set("ampire:boot:notify", 1, ex=600, nx=True):
                    await ctx.notify.to_admins("🟢 <b>AmpireVPN запущен.</b> Откройте /admin → Система → Диагностика, чтобы проверить подключения.")
            except Exception:  # noqa: BLE001
                log.warning("не удалось отправить уведомление о запуске", exc_info=True)

        if role != "worker" and not settings.webhook_mode:
            await bot.delete_webhook(drop_pending_updates=False)
            assert dp is not None
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())  # сам обрабатывает SIGINT/SIGTERM
        else:
            if role != "worker":
                assert dp is not None
                await bot.set_webhook(
                    url=settings.base_url + settings.telegram_webhook_path,
                    secret_token=settings.telegram_secret_token,
                    allowed_updates=dp.resolve_used_update_types(),
                    drop_pending_updates=False,
                    max_connections=100,
                )
                log.info("Webhook Telegram установлен")
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, stop.set)
                except NotImplementedError:  # Windows
                    pass
            await stop.wait()
    finally:
        # Корректная остановка (docker stop_grace_period: 45s): сначала перестаём принимать новую работу,
        # затем дожидаемся уже начатой — иначе оплаты, принятые вебхуком, могли бы оборваться на полпути.
        log.info("Останавливаюсь…")
        await web_runner.cleanup()                 # новые HTTP-запросы больше не принимаются
        pending = await drain_tasks(timeout=20.0)  # фоновая обработка вебхуков оплаты и т. п.
        if workers:
            await workers.stop(timeout=15.0)       # текущая итерация задач дорабатывает, дальше — отмена
        await ctx.bc.shutdown(timeout=10.0)        # рассылка сохраняет курсор и продолжится при следующем запуске
        for t in bg:
            t.cancel()
        attach_sink(None)
        await sink.stop()
        await remna.close()
        if yk is not None:
            await yk.close()
        if crypto is not None:
            await crypto.close()
        await bot.session.close()
        await redis.aclose()
        await health_engine.dispose()
        await engine.dispose()
        if pending:
            log.warning("при остановке не завершены фоновые задачи: %d (платежи добёрет сверка)", pending)


async def _setup_bot_commands(bot: Bot, settings: Settings) -> None:
    try:
        base = [BotCommand(command="start", description="Главное меню"), BotCommand(command="menu", description="Меню")]
        await bot.set_my_commands(base)
        for owner in settings.owners:
            await bot.set_my_commands(
                base + [BotCommand(command="admin", description="Админ-панель")], scope=BotCommandScopeChat(chat_id=owner)
            )
    except Exception:  # noqa: BLE001
        log.warning("не удалось выставить команды бота", exc_info=True)
