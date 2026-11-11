"""Параллельная отправка пачками и разбор ошибок Telegram (чистая логика — тестируется без aiogram)."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")

_BLOCKED = (
    "chat not found",
    "user is deactivated",
    "bot was blocked",
    "bot can't initiate",
    "have no rights to send",
    "peer_id_invalid",
    "forbidden",
)
_SOURCE_GONE = (
    "message to copy not found",
    "message to forward not found",
    "message_id_invalid",
    "message can't be copied",
    "message not found",
)


def classify_send_error(text: str) -> str:
    """'blocked' — пользователь недоступен; 'source_gone' — исходное сообщение рассылки удалено; иначе 'failed'."""
    t = (text or "").lower()
    if any(s in t for s in _SOURCE_GONE):
        return "source_gone"
    if any(s in t for s in _BLOCKED):
        return "blocked"
    return "failed"


async def run_chunk(items: Sequence[T], worker: Callable[[T], Awaitable[R]], *, concurrency: int) -> list[R]:
    """Выполняет ``worker`` для всех элементов пачки параллельно (не больше ``concurrency`` одновременно).

    Порядок результатов совпадает с порядком элементов. Общий лимит скорости обеспечивает сам ``worker``
    (токен-бакет Redis), поэтому параллелизм ускоряет отправку, но не нарушает лимиты Telegram.
    """
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def one(x: T) -> R:
        async with sem:
            return await worker(x)

    return list(await asyncio.gather(*(one(x) for x in items)))
