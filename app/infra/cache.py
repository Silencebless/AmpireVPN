"""Тонкий кэш поверх Redis. Сбой Redis не ломает основной сценарий — просто промах кэша."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

from app.infra import codec

log = logging.getLogger(__name__)


class Cache:
    def __init__(self, redis: Redis, prefix: str = "ampire:cache:") -> None:
        self.redis = redis
        self.prefix = prefix

    async def get(self, key: str) -> Any | None:
        try:
            raw = await self.redis.get(self.prefix + key)
            return None if raw is None else codec.loads(raw)
        except Exception:  # noqa: BLE001
            log.debug("cache get failed: %s", key, exc_info=True)
            return None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        try:
            await self.redis.set(self.prefix + key, codec.dumps(value), ex=ttl)
        except Exception:  # noqa: BLE001
            log.debug("cache set failed: %s", key, exc_info=True)

    async def delete(self, *keys: str) -> None:
        try:
            if keys:
                await self.redis.delete(*(self.prefix + k for k in keys))
        except Exception:  # noqa: BLE001
            log.debug("cache delete failed", exc_info=True)

    async def remember(
        self, key: str, ttl: int, factory: Callable[[], Awaitable[Any]], *, force: bool = False
    ) -> Any:
        if not force:
            hit = await self.get(key)
            if hit is not None:
                return hit
        value = await factory()
        await self.set(key, value, ttl)
        return value
