"""Логирование: user_id в каждой строке, JSON-формат, кольцевой буфер ошибок + общий журнал в Redis."""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import sys
from collections import deque
from collections.abc import Iterator
from datetime import datetime, timezone

LOG_BUFFER: deque[str] = deque(maxlen=200)
ERRLOG_KEY = "ampire:errlog"          # общий журнал ошибок всех реплик (список в Redis)
ERRLOG_MAX = 500
INSTANCE = os.environ.get("HOSTNAME", "local")[-6:]
ROLE = "app"

_current_user: contextvars.ContextVar[int | None] = contextvars.ContextVar("log_user_id", default=None)


def set_log_user(user_id: int | None) -> None:
    """Привязывает user_id к текущей задаче: он попадёт во все строки лога, пока идёт обработка."""
    _current_user.set(user_id)


@contextlib.contextmanager
def log_user(user_id: int | None) -> Iterator[None]:
    token = _current_user.set(user_id)
    try:
        yield
    finally:
        _current_user.reset(token)


class ContextFilter(logging.Filter):
    """Добавляет к записи user_id, роль и идентификатор реплики."""

    def filter(self, record: logging.LogRecord) -> bool:
        uid = _current_user.get()
        record.user_id = uid if uid is not None else "-"
        record.role = ROLE
        record.instance = INSTANCE
        return True


class LogSink:
    """Складывает важные записи в Redis (общий журнал для админки). Работает в фоне и никогда не блокирует логирование."""

    def __init__(self, redis) -> None:  # noqa: ANN001
        self._redis = redis
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self._task: asyncio.Task | None = None

    def put(self, line: str) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(line)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def _run(self) -> None:
        while True:
            line = await self._queue.get()
            try:
                pipe = self._redis.pipeline()
                pipe.lpush(ERRLOG_KEY, line)
                pipe.ltrim(ERRLOG_KEY, 0, ERRLOG_MAX - 1)
                await pipe.execute()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — Redis недоступен: остаётся локальный буфер
                await asyncio.sleep(1.0)


_sink: LogSink | None = None


def attach_sink(sink: LogSink | None) -> None:
    global _sink
    _sink = sink


async def read_shared_errors(redis, limit: int = 20) -> list[str]:  # noqa: ANN001
    """Последние ошибки со всех реплик (новые — первыми); при сбое Redis — пустой список."""
    try:
        raw = await redis.lrange(ERRLOG_KEY, 0, max(0, limit - 1))
    except Exception:  # noqa: BLE001
        return []
    return [x.decode() if isinstance(x, bytes) else str(x) for x in raw]


class RingBufferHandler(logging.Handler):
    """Хранит последние WARNING+ записи в памяти реплики и (если подключён sink) в общем журнале Redis."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%d.%m %H:%M:%S")
            uid = getattr(record, "user_id", "-")
            line = f"{ts} {record.levelname} {record.name}"
            if uid != "-":
                line += f" u={uid}"
            line += f": {record.getMessage()}"
            if record.exc_info:
                exc = record.exc_info[1]
                line += f" | {type(exc).__name__}: {exc}"
            line = line[:600]
            LOG_BUFFER.append(line)
            if _sink is not None:
                _sink.put(f"[{ROLE}-{INSTANCE}] {line}")
        except Exception:  # noqa: BLE001
            pass


class JsonFormatter(logging.Formatter):
    """Одна строка = один JSON (удобно для Loki / ELK / Datadog)."""

    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "role": getattr(record, "role", ROLE),
            "instance": getattr(record, "instance", INSTANCE),
        }
        uid = getattr(record, "user_id", "-")
        if uid != "-":
            data["user_id"] = uid
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def setup_logging(level: str = "INFO", *, json_logs: bool = False, role: str = "app") -> None:
    global ROLE
    ROLE = role
    root = logging.getLogger()
    root.handlers.clear()
    root.filters.clear()
    root.setLevel(level.upper())
    ctx_filter = ContextFilter()

    stream = logging.StreamHandler(sys.stdout)
    stream.addFilter(ctx_filter)
    if json_logs:
        stream.setFormatter(JsonFormatter())
    else:
        stream.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s [u=%(user_id)s]: %(message)s", "%H:%M:%S")
        )
    root.addHandler(stream)

    ring = RingBufferHandler(level=logging.WARNING)
    ring.addFilter(ctx_filter)
    root.addHandler(ring)

    # шумные библиотеки
    for noisy in ("httpx", "httpcore", "aiohttp.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
