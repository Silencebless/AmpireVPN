"""Отправка сообщений пользователям и владельцам.

* Сетевые сбои и 5xx Telegram повторяются автоматически (с короткой паузой).
* Важные сообщения (``critical=True``, например «оплата получена») при затяжном сбое не теряются:
  они попадают в исходящую очередь Redis, и воркер доставляет их позже.
* Фоновые сообщения (``paced=True``) идут через общий бакет скорости ``tg:bg`` — отдельный от рассылок.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import TYPE_CHECKING

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import update

from app.core.backoff import outbox_delay, send_retry_delay
from app.core.fanout import classify_send_error
from app.db.models import User
from app.infra.metrics import metrics

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)

OUTBOX_KEY = "ampire:outbox"
OUTBOX_MAX_ATTEMPTS = 14          # ≈ сутки с учётом растущей паузы
BG_BUCKET = "tg:bg"               # служебные фоновые сообщения (отдельно от рассылок — tg:bc)

OK, BLOCKED, FAILED, RETRY, QUEUED = "ok", "blocked", "failed", "retry", "queued"


class Notifier:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._alerted: dict[str, float] = {}   # запасной «раз в N секунд» на случай недоступности Redis

    async def _mark_blocked(self, user_id: int) -> None:
        try:
            async with self.ctx.session() as s:
                await s.execute(update(User).where(User.id == user_id).values(bot_blocked=True))
                await s.commit()
        except Exception:  # noqa: BLE001
            log.exception("cannot mark user %s as blocked", user_id)

    # ------------------------------------------------------------------ отправка
    async def send_result(
        self,
        user_id: int,
        text: str,
        markup: InlineKeyboardMarkup | None = None,
        *,
        silent: bool = False,
        paced: bool = False,
        critical: bool = False,
    ) -> str:
        """Возвращает: ok | blocked (пользователь недоступен) | failed (постоянная ошибка) | queued (отложено в очередь) | retry.

        ``retry`` — сообщение не доставлено из-за временного сбоя и в очередь не поставлено (не critical):
        вызывающий код может повторить попытку позже (например, напоминание не помечается отправленным).
        """
        if paced:
            rate = max(1, self.ctx.settings.tg_bg_rate)
            await self.ctx.limiter.acquire(BG_BUCKET, rate, rate)
        attempts = 6 if critical else 4
        for attempt in range(attempts):
            try:
                await self.ctx.bot.send_message(user_id, text, reply_markup=markup, disable_notification=silent)
                metrics.inc("tg_sent_total", result="ok")
                return OK
            except TelegramRetryAfter as e:
                metrics.inc("tg_sent_total", result="flood")
                with contextlib.suppress(Exception):
                    await self.ctx.limiter.penalize(BG_BUCKET, min(e.retry_after + 1, 60))
                if attempt + 1 >= attempts:
                    break
                await asyncio.sleep(min(e.retry_after + 1, 30))
            except TelegramForbiddenError:
                metrics.inc("tg_sent_total", result="blocked")
                await self._mark_blocked(user_id)
                return BLOCKED
            except TelegramBadRequest as e:
                if classify_send_error(str(e)) == "blocked":
                    await self._mark_blocked(user_id)
                    return BLOCKED
                log.warning("send to %s failed: %s", user_id, e)
                return FAILED
            except (TelegramNetworkError, TelegramServerError, asyncio.TimeoutError) as e:
                metrics.inc("tg_sent_total", result="network")
                if attempt + 1 >= attempts:
                    log.warning("send to %s: сеть/сервер Telegram недоступны (%s)", user_id, type(e).__name__)
                    break
                await asyncio.sleep(send_retry_delay(attempt))
            except TelegramAPIError as e:
                log.warning("send to %s failed: %s", user_id, e)
                return FAILED
        # временный сбой не прошёл за все попытки
        if critical and await self._enqueue(user_id, text, markup):
            return QUEUED
        return RETRY

    async def send(
        self,
        user_id: int,
        text: str,
        markup: InlineKeyboardMarkup | None = None,
        *,
        silent: bool = False,
        paced: bool = False,
        critical: bool = False,
    ) -> bool:
        """True — доставлено. paced=True — фоновая отправка: ждём общий лимит Telegram на все реплики."""
        res = await self.send_result(user_id, text, markup, silent=silent, paced=paced, critical=critical)
        return res == OK

    # ------------------------------------------------------------------ исходящая очередь (важные сообщения)
    async def _enqueue(self, user_id: int, text: str, markup: InlineKeyboardMarkup | None, attempts: int = 0, created: float | None = None) -> bool:
        entry = {
            "u": user_id,
            "t": text,
            "m": markup.model_dump_json(exclude_none=True) if markup is not None else None,
            "a": attempts,
            "at": time.time() + outbox_delay(attempts).total_seconds(),
            "c": created or time.time(),
        }
        try:
            await self.ctx.redis.rpush(OUTBOX_KEY, json.dumps(entry, ensure_ascii=False))
            metrics.inc("outbox_enqueued_total")
            return True
        except Exception:  # noqa: BLE001
            log.error("не удалось поставить сообщение для %s в исходящую очередь", user_id, exc_info=True)
            return False

    async def drain_outbox(self, batch: int = 200) -> int:
        """Воркер: доставляет отложенные важные сообщения. Возвращает число доставленных."""
        try:
            size = int(await self.ctx.redis.llen(OUTBOX_KEY))
        except Exception:  # noqa: BLE001
            return 0
        delivered = 0
        now = time.time()
        for _ in range(min(size, batch)):
            try:
                raw = await self.ctx.redis.lpop(OUTBOX_KEY)
            except Exception:  # noqa: BLE001
                break
            if raw is None:
                break
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if float(entry.get("at", 0)) > now:           # ещё рано — возвращаем в конец
                await self.ctx.redis.rpush(OUTBOX_KEY, raw)
                continue
            markup = InlineKeyboardMarkup.model_validate_json(entry["m"]) if entry.get("m") else None
            res = await self.send_result(int(entry["u"]), entry["t"], markup, paced=True)
            if res == OK:
                delivered += 1
            elif res == RETRY:
                attempts = int(entry.get("a", 0)) + 1
                if attempts >= OUTBOX_MAX_ATTEMPTS:
                    log.error("сообщение для %s не доставлено за %s попыток — отбрасываю", entry["u"], attempts)
                    metrics.inc("outbox_dropped_total")
                else:
                    await self._enqueue(int(entry["u"]), entry["t"], markup, attempts, float(entry.get("c", now)))
        return delivered

    async def outbox_size(self) -> int:
        try:
            return int(await self.ctx.redis.llen(OUTBOX_KEY))
        except Exception:  # noqa: BLE001
            return -1

    # ------------------------------------------------------------------ владельцы
    async def to_admins(self, text: str, *, owners_only: bool = True, markup: InlineKeyboardMarkup | None = None) -> None:
        ids = set(self.ctx.settings.owners) if owners_only else self.ctx.all_admin_ids()
        for uid in ids:
            await self.send(uid, text, markup, silent=False)

    async def alert_once(self, key: str, text: str, *, ttl: int = 3600) -> bool:
        """Алерт владельцам не чаще раза в ``ttl`` секунд для данного ключа (в т. ч. при недоступном Redis). True — отправлено."""
        fresh = False
        try:
            fresh = bool(await self.ctx.redis.set(f"ampire:alert:{key}", 1, ex=ttl, nx=True))
        except Exception:  # noqa: BLE001
            now = time.monotonic()
            if now - self._alerted.get(key, -1e9) >= ttl:
                self._alerted[key] = now
                fresh = True
        if fresh:
            await self.to_admins(text)
        return fresh
