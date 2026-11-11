"""Шина событий на Redis pub/sub: сообщает репликам, что настройки/тексты/тарифы изменились."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

log = logging.getLogger(__name__)


class Bus:
    CHANNEL = "ampire:bus"

    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def publish(self, kind: str) -> None:
        try:
            await self.redis.publish(self.CHANNEL, kind)
        except Exception:  # noqa: BLE001
            log.warning("bus: не удалось опубликовать %s", kind)

    async def listen(self, handlers: dict[str, Callable[[], Awaitable[None]]]) -> None:
        while True:
            pubsub = self.redis.pubsub()
            try:
                await pubsub.subscribe(self.CHANNEL)
                async for msg in pubsub.listen():
                    if msg.get("type") != "message":
                        continue
                    data = msg["data"]
                    kind = data.decode() if isinstance(data, bytes) else str(data)
                    handler = handlers.get(kind)
                    if handler is not None:
                        try:
                            await handler()
                        except Exception:  # noqa: BLE001
                            log.exception("bus handler %s", kind)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.warning("bus: соединение потеряно, переподключаюсь", exc_info=True)
                await asyncio.sleep(2)
            finally:
                try:
                    await pubsub.aclose()
                except Exception:  # noqa: BLE001
                    pass
