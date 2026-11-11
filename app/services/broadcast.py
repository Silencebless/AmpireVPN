"""Рассылки.

Админ присылает боту любое сообщение (текст, фото, видео, файл, кружок…) — мы пересылаем его
пользователям через ``copy_message``, поэтому форматирование и медиа сохраняются без доп. кода.
Рассылка идёт фоном небольшими пачками (по 25 получателей, параллельно), переживает перезапуск
(хранится курсор — после аварии возможен повтор не более одной пачки) и умеет паузу/отмену, которые срабатывают
между пачками. Скорость ограничена общим бакетом Redis ``tg:bc`` — отдельным от служебных уведомлений (``tg:bg``).
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from sqlalchemy import desc, func, select, update

from app.bot.ui import btn, kb
from app.core.backoff import send_retry_delay
from app.core.fanout import classify_send_error, run_chunk
from app.db.models import Broadcast, User
from app.services.users import user_filter
from app.utils import utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)

CHUNK = 25                 # получателей в одной пачке (курсор сохраняется после каждой пачки)
BC_BUCKET = "tg:bc"        # бакет скорости рассылок (служебные уведомления — tg:bg)


class BroadcastService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._tasks: dict[int, asyncio.Task] = {}
        self._stopping = False

    @staticmethod
    def _conds(segment: str) -> list:
        return [User.is_banned.is_(False), User.bot_blocked.is_(False), *user_filter(segment, utcnow())]

    # ------------------------------------------------------------------ CRUD
    async def audience_count(self, segment: str) -> int:
        async with self.ctx.session() as s:
            return int(await s.scalar(select(func.count()).select_from(User).where(*self._conds(segment))) or 0)

    async def create(
        self,
        admin_id: int,
        src_chat_id: int,
        src_message_id: int,
        segment: str,
        button_text: str | None = None,
        button_url: str | None = None,
    ) -> Broadcast:
        async with self.ctx.session() as s:
            b = Broadcast(
                created_by=admin_id,
                src_chat_id=src_chat_id,
                src_message_id=src_message_id,
                segment=segment,
                button_text=button_text,
                button_url=button_url,
                status="draft",
            )
            s.add(b)
            await s.commit()
            return b

    async def get(self, bid: int) -> Broadcast | None:
        async with self.ctx.session() as s:
            return await s.get(Broadcast, bid)

    async def recent(self, limit: int = 10) -> list[Broadcast]:
        async with self.ctx.session() as s:
            return list((await s.execute(select(Broadcast).order_by(desc(Broadcast.id)).limit(limit))).scalars().all())

    @staticmethod
    def markup(b: Broadcast):
        if b.button_text and b.button_url:
            return kb(btn(b.button_text, url=b.button_url))
        return None

    async def preview(self, b: Broadcast, chat_id: int) -> None:
        await self.ctx.bot.copy_message(
            chat_id=chat_id, from_chat_id=b.src_chat_id, message_id=b.src_message_id, reply_markup=self.markup(b)
        )

    # ------------------------------------------------------------------ управление
    def is_running(self, bid: int) -> bool:
        t = self._tasks.get(bid)
        return t is not None and not t.done()

    async def start(self, bid: int) -> None:
        async with self.ctx.session() as s:
            b = await s.get(Broadcast, bid)
            if b is None or b.status in ("done", "cancelled"):
                return
            if b.status == "draft":
                b.total = await s.scalar(select(func.count()).select_from(User).where(*self._conds(b.segment))) or 0
                b.started_at = utcnow()
            b.status = "running"
            await s.commit()
        if self.ctx.settings.role != "api":
            self._spawn(bid)  # в режиме api рассылку подхватит worker (dispatch раз в несколько секунд)

    async def pause(self, bid: int) -> None:
        await self._set_status(bid, "paused", only_from=("running",))

    async def cancel(self, bid: int) -> None:
        await self._set_status(bid, "cancelled", only_from=("running", "paused", "draft"))

    async def _set_status(self, bid: int, status: str, only_from: tuple[str, ...]) -> None:
        async with self.ctx.session() as s:
            await s.execute(
                update(Broadcast).where(Broadcast.id == bid, Broadcast.status.in_(only_from)).values(status=status)
            )
            await s.commit()

    def _spawn(self, bid: int) -> None:
        if not self.is_running(bid):
            self._tasks[bid] = asyncio.create_task(self._run(bid), name=f"broadcast-{bid}")

    async def dispatch(self) -> None:
        """Воркер: запускает исполнителей для всех рассылок в статусе running (в т.ч. прерванных перезапуском).

        Исполнитель берёт Redis-блокировку рассылки, поэтому дубли между репликами невозможны.
        """
        async with self.ctx.session() as s:
            ids = list((await s.scalars(select(Broadcast.id).where(Broadcast.status == "running"))).all())
        for bid in ids:
            self._spawn(bid)

    async def resume_running(self) -> None:  # совместимость
        await self.dispatch()

    async def shutdown(self, timeout: float = 25.0) -> None:
        """Мягкая остановка: текущие пачки дорабатывают и сохраняют курсор; по таймауту — отмена."""
        self._stopping = True
        tasks = [t for t in self._tasks.values() if not t.done()]
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=timeout)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------------ отправка
    async def _send_one(self, b: Broadcast, uid: int) -> str:
        """sent | blocked | failed | source_gone (исходное сообщение рассылки удалено)."""
        markup = self.markup(b)
        rate = max(1, self.ctx.settings.tg_broadcast_rate)
        for attempt in range(4):
            await self.ctx.limiter.acquire(BC_BUCKET, rate, rate)  # общий лимит рассылок на все реплики
            try:
                await self.ctx.bot.copy_message(
                    chat_id=uid, from_chat_id=b.src_chat_id, message_id=b.src_message_id, reply_markup=markup
                )
                return "sent"
            except TelegramRetryAfter as e:
                # flood control: ставим на паузу бакет у ВСЕХ реплик, а не только у этого отправителя
                await self.ctx.limiter.penalize(BC_BUCKET, min(e.retry_after + 1, 300))
            except TelegramForbiddenError:
                return "blocked"
            except TelegramBadRequest as e:
                kind = classify_send_error(str(e))
                if kind == "source_gone":
                    return "source_gone"
                if kind == "blocked":
                    return "blocked"
                log.warning("broadcast %s -> %s: %s", b.id, uid, e)
                return "failed"
            except (TelegramNetworkError, TelegramServerError, asyncio.TimeoutError):
                await asyncio.sleep(send_retry_delay(attempt))
            except TelegramAPIError as e:
                log.warning("broadcast %s -> %s: %s", b.id, uid, e)
                return "failed"
        return "failed"

    async def _run(self, bid: int) -> None:
        async with self.ctx.locks.job(f"bc:{bid}", ttl=60.0) as got:
            if not got:
                return  # рассылку уже ведёт другая реплика
            await self._run_locked(bid)

    async def _abort_source_gone(self, b: Broadcast) -> None:
        await self._set_status(b.id, "paused", only_from=("running",))
        msg = (
            f"⚠️ <b>Рассылка #{b.id} приостановлена</b>: исходное сообщение удалено или недоступно боту.\n"
            "Отмените эту рассылку и создайте новую."
        )
        await self.ctx.notify.send(b.created_by, msg)
        await self.ctx.notify.alert_once(f"bc_source_gone:{b.id}", msg, ttl=86400)

    async def _run_locked(self, bid: int) -> None:
        conc = max(1, self.ctx.settings.broadcast_concurrency)
        try:
            while not self._stopping:
                # статус проверяется перед КАЖДОЙ пачкой: пауза и отмена срабатывают почти мгновенно
                async with self.ctx.session() as s:
                    b = await s.get(Broadcast, bid)
                    if b is None or b.status != "running":
                        return
                    ids = list(
                        (
                            await s.scalars(
                                select(User.id)
                                .where(User.id > b.cursor_id, *self._conds(b.segment))
                                .order_by(User.id)
                                .limit(CHUNK)
                            )
                        ).all()
                    )
                if not ids:
                    break
                results = await run_chunk(ids, lambda uid, b=b: self._send_one(b, uid), concurrency=conc)
                if "source_gone" in results:
                    await self._abort_source_gone(b)
                    return
                sent = results.count("sent")
                blocked_ids = [uid for uid, r in zip(ids, results, strict=True) if r == "blocked"]
                failed = len(results) - sent - len(blocked_ids)
                async with self.ctx.session() as s:
                    await s.execute(
                        update(Broadcast)
                        .where(Broadcast.id == bid)
                        .values(
                            sent=Broadcast.sent + sent,
                            failed=Broadcast.failed + failed,
                            blocked=Broadcast.blocked + len(blocked_ids),
                            cursor_id=ids[-1],
                        )
                    )
                    if blocked_ids:
                        await s.execute(update(User).where(User.id.in_(blocked_ids)).values(bot_blocked=True))
                    await s.commit()
            else:
                return  # процесс останавливается: курсор сохранён, рассылку продолжит следующий запуск

            async with self.ctx.session() as s:
                await s.execute(
                    update(Broadcast)
                    .where(Broadcast.id == bid, Broadcast.status == "running")
                    .values(status="done", finished_at=utcnow())
                )
                await s.commit()
                b = await s.get(Broadcast, bid)
            if b is not None and b.status == "done":
                await self.ctx.notify.send(
                    b.created_by,
                    f"📣 <b>Рассылка #{b.id} завершена</b>\n\n"
                    f"Доставлено: <b>{b.sent}</b>\nОшибок: {b.failed}\nЗаблокировали бота: {b.blocked}",
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("broadcast %s crashed", bid)
            await self._set_status(bid, "paused", only_from=("running",))
