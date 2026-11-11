"""HTTP-сервер: страницы /dl и /add, вебхуки (Telegram, ЮKassa, CryptoBot, Remnawave), health и метрики."""
from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from aiogram import Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web
from sqlalchemy import select
from sqlalchemy import text as sql_text

from app.db.models import User
from app.infra.metrics import metrics
from app.utils import esc, spawn, utcnow
from app.web import pages
from app.web.security import ip_allowed, safe_eq, verify_cryptobot_signature, verify_signature

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)

SEC_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}
LIMITED_PREFIXES = ("/add/", "/dl")


def _html(text: str, status: int = 200) -> web.Response:
    return web.Response(text=text, status=status, content_type="text/html", charset="utf-8", headers=SEC_HEADERS)


def create_web_app(ctx: AppContext, dp: Dispatcher | None = None) -> web.Application:
    settings = ctx.settings

    def client_ip(request: web.Request) -> str:
        if settings.trusted_proxy:
            xff = request.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[0].strip()
        return request.remote or ""

    @web.middleware
    async def guard(request: web.Request, handler):
        """Лимит запросов с одного IP к публичным страницам (защита от перебора и нагрузки)."""
        path = request.path
        if path == "/" or path.startswith(LIMITED_PREFIXES):
            per_min = max(10, settings.web_rate_per_min)
            if not await ctx.limiter.allow(f"web:{client_ip(request)}", per_min / 60.0, max(10.0, per_min / 3)):
                metrics.inc("web_requests_total", status="429")
                return web.Response(status=429, text="Too many requests", headers={"Retry-After": "30"})
        resp = await handler(request)
        metrics.inc("web_requests_total", status=f"{resp.status // 100}xx")
        return resp

    app = web.Application(client_max_size=2 * 1024 * 1024, middlewares=[guard])
    app["ctx"] = ctx

    async def index(_: web.Request) -> web.Response:
        return _html(pages.landing_page(ctx.bot_username))

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "instance": ctx.instance_id, "role": settings.role})

    ready_cache: dict[str, object] = {"ts": 0.0, "body": None, "status": 200}

    async def ready(_: web.Request) -> web.Response:
        """Готовность реплики для балансировщика.

        * БД проверяется отдельным крошечным пулом (health_engine): при исчерпании основного пула бота реплика
          НЕ выпадает из ротации из-за самой проверки;
        * результат кэшируется на 2 секунды — частые проверки не превращаются в нагрузку на БД и Redis;
        * недоступный Redis — «деградация», но не отказ: бот продолжает работать (fail-open), и выводить из ротации
          все реплики разом из-за Redis значило бы устроить полный простой.
        Эндпоинт не должен быть доступен из интернета (Caddy отдаёт 404 снаружи).
        """
        now = time.monotonic()
        if ready_cache["body"] is not None and now - float(ready_cache["ts"]) < 2.0:
            return web.json_response(ready_cache["body"], status=int(ready_cache["status"]))
        checks: dict[str, bool] = {}
        try:
            engine = ctx.health_engine
            if engine is not None:
                async with engine.connect() as conn:
                    await conn.execute(sql_text("select 1"))
            else:
                async with ctx.session() as s:
                    await s.execute(sql_text("select 1"))
            checks["db"] = True
        except Exception:  # noqa: BLE001
            checks["db"] = False
        try:
            await ctx.redis.ping()
            checks["redis"] = True
        except Exception:  # noqa: BLE001
            checks["redis"] = False
        ok = checks["db"]
        body = {"ok": ok, "degraded": not checks["redis"], **checks}
        ready_cache.update(ts=now, body=body, status=200 if ok else 503)
        return web.json_response(body, status=200 if ok else 503)

    async def metrics_endpoint(request: web.Request) -> web.Response:
        token = settings.metrics_token
        if not token:
            return web.Response(status=404)
        # токен принимается только в заголовке: адреса с токеном в query попадают в логи прокси и историю
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not safe_eq(supplied, token):
            return web.Response(status=401)
        try:  # свежие «срезы» из кэшированной сводки (дёшево)
            ov = await ctx.stats.overview()
            metrics.set_gauge("users_total", ov["users_total"])
            metrics.set_gauge("subscriptions_active", ov["premium"], tier="premium")
            metrics.set_gauge("subscriptions_active", ov["lite"], tier="lite")
            metrics.set_gauge("payments_pending", ov["pending_payments"])
            metrics.set_gauge("sync_pending_users", ov["unsynced"])
        except Exception:  # noqa: BLE001
            log.debug("metrics: сводка недоступна", exc_info=True)
        metrics.set_gauge("panel_circuit_open", 1 if ctx.remna.circuit_open else 0)
        metrics.set_gauge("uptime_seconds", (utcnow() - ctx.started_at).total_seconds())
        for name, ts in (await ctx.heartbeats()).items():
            metrics.set_gauge("worker_last_run_timestamp", ts.timestamp(), worker=name)
        return web.Response(text=metrics.render(), content_type="text/plain", charset="utf-8")

    async def dl(_: web.Request) -> web.Response:
        return _html(pages.download_page(await ctx.links.dead_keys()))

    async def add_sub(request: web.Request) -> web.Response:
        short = request.match_info["short"]
        async with ctx.session() as s:
            sub_url = await s.scalar(select(User.sub_url).where(User.short_uuid == short))
        if not sub_url:
            return _html(pages.not_found_page(ctx.bot_username), 404)
        return _html(pages.add_subscription_page(sub_url))

    # ------------------------------------------------------------------ ЮKassa
    async def yookassa_hook(request: web.Request) -> web.Response:
        if not safe_eq(request.match_info["secret"], settings.webhook_secret):
            return web.Response(status=404)
        if settings.yookassa_check_ip and not ip_allowed(client_ip(request)):
            log.warning("уведомление ЮKassa с чужого IP %s", client_ip(request))
            return web.Response(status=403)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.Response(status=400)
        await ctx.mark_webhook("yookassa")
        # отвечаем сразу; если обработка упадёт — платёж добёрет воркер reconcile
        spawn(ctx.pay.handle_yookassa_event(payload))
        return web.Response(text="ok")

    # ------------------------------------------------------------------ CryptoBot
    async def cryptobot_hook(request: web.Request) -> web.Response:
        if not safe_eq(request.match_info["secret"], settings.webhook_secret):
            return web.Response(status=404)
        raw = await request.read()
        if not settings.cryptobot_token or not verify_cryptobot_signature(
            raw, request.headers.get("crypto-pay-api-signature", ""), settings.cryptobot_token
        ):
            log.warning("вебхук CryptoBot: неверная подпись")
            return web.Response(status=401)
        try:
            payload = json.loads(raw)
        except ValueError:
            return web.Response(status=400)
        await ctx.mark_webhook("cryptobot")
        spawn(ctx.pay.handle_cryptobot_update(payload))
        return web.Response(text="ok")

    # ------------------------------------------------------------------ Remnawave
    async def remnawave_hook(request: web.Request) -> web.Response:
        if not safe_eq(request.match_info["secret"], settings.webhook_secret):
            return web.Response(status=404)
        raw = await request.read()
        secret = settings.remnawave_webhook_secret
        if secret and not verify_signature(raw, request.headers.get("X-Remnawave-Signature", ""), secret):
            log.warning("вебхук Remnawave: неверная подпись")
            return web.Response(status=401)
        try:
            payload = json.loads(raw)
        except ValueError:
            return web.Response(status=400)
        await ctx.mark_webhook("remnawave")
        spawn(_handle_remna_event(ctx, str(payload.get("event", "")), payload.get("data") or {}))
        return web.Response(text="ok")

    app.router.add_get("/", index)
    app.router.add_get("/healthz", health)
    app.router.add_get("/readyz", ready)
    app.router.add_get("/metrics", metrics_endpoint)
    app.router.add_get("/dl", dl)
    app.router.add_get("/add/{short}", add_sub)
    app.router.add_post("/webhooks/yookassa/{secret}", yookassa_hook)
    app.router.add_post("/webhooks/cryptobot/{secret}", cryptobot_hook)
    app.router.add_post("/webhooks/remnawave/{secret}", remnawave_hook)

    if dp is not None:  # режим webhook для Telegram: можно запускать несколько реплик за балансировщиком
        SimpleRequestHandler(
            dispatcher=dp, bot=ctx.bot, secret_token=settings.telegram_secret_token
        ).register(app, path=settings.telegram_webhook_path)
    return app


async def _handle_remna_event(ctx: AppContext, event: str, data: dict) -> None:
    if event == "user.first_connected":
        uid: int | None = None
        tg = data.get("telegramId")
        async with ctx.session() as s:
            if tg:
                uid = await s.scalar(select(User.id).where(User.id == int(tg)))
            if uid is None and data.get("id") is not None:
                uid = await s.scalar(select(User.id).where(User.remna_id == int(data["id"])))
        if uid:
            await ctx.refs.on_user_connected(uid)
        return

    if event.startswith("node.") and ctx.cfg.get("alerts_nodes"):
        name = esc(data.get("name") or data.get("address") or "нода")
        if "connection_lost" in event or "offline" in event:
            await ctx.notify.to_admins(f"🔴 <b>Нода недоступна:</b> {name}")
        elif "connection_restored" in event or "online" in event:
            await ctx.notify.to_admins(f"🟢 <b>Нода снова в сети:</b> {name}")


async def start_web(ctx: AppContext, dp: Dispatcher | None = None) -> web.AppRunner:
    app = create_web_app(ctx, dp)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, ctx.settings.web_host, ctx.settings.web_port)
    await site.start()
    log.info("Web: слушаю %s:%s (роль %s, instance %s)", ctx.settings.web_host, ctx.settings.web_port, ctx.settings.role, ctx.instance_id)
    return runner
