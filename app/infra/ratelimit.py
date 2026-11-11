"""Токен-бакет в Redis: общий лимит на все реплики (исходящие сообщения Telegram, веб-запросы по IP)."""
from __future__ import annotations

import asyncio
import logging
import time

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

log = logging.getLogger(__name__)

_BUCKET = """
local rate = tonumber(ARGV[1]); local burst = tonumber(ARGV[2]); local cost = tonumber(ARGV[3])
local t = redis.call('TIME'); local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local d = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(d[1]); local ts = tonumber(d[2])
if tokens == nil then tokens = burst; ts = now end
tokens = math.min(burst, tokens + math.max(0, now - ts) / 1000.0 * rate)
local wait = 0
if tokens >= cost then tokens = tokens - cost else wait = math.ceil((cost - tokens) / rate * 1000) end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('PEXPIRE', KEYS[1], 120000)
return wait
"""


class RateLimiter:
    def __init__(self, redis: Redis, prefix: str = "ampire:rl:") -> None:
        self.redis = redis
        self.prefix = prefix

    async def penalize(self, name: str, seconds: float) -> None:
        """Ставит бакет на паузу для ВСЕХ реплик (например, после RetryAfter от Telegram)."""
        seconds = max(0.0, float(seconds))
        until_ms = int((time.time() + seconds) * 1000)
        try:
            await self.redis.set(f"{self.prefix}pause:{name}", str(until_ms), px=max(1, int(seconds * 1000)))
        except Exception:  # noqa: BLE001
            log.debug("не удалось поставить паузу бакета %s", name, exc_info=True)

    async def pause_left(self, name: str) -> float:
        """Сколько секунд бакет ещё на паузе (0 — не на паузе)."""
        try:
            raw = await self.redis.get(f"{self.prefix}pause:{name}")
        except Exception:  # noqa: BLE001
            return 0.0
        if raw is None:
            return 0.0
        try:
            until_ms = int(raw.decode() if isinstance(raw, bytes) else raw)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, until_ms / 1000.0 - time.time())

    async def reserve(self, name: str, rate: float, burst: float, cost: float = 1.0) -> float:
        """Возвращает 0, если токен получен, иначе — сколько секунд подождать."""
        paused = await self.pause_left(name)
        if paused > 0:
            return paused
        wait_ms = await self.redis.eval(_BUCKET, 1, self.prefix + name, rate, burst, cost)
        return float(wait_ms) / 1000.0

    async def acquire(self, name: str, rate: float, burst: float, cost: float = 1.0, *, max_wait: float = 60.0) -> None:
        """Ждёт, пока в бакете появится токен (но не дольше max_wait).

        Пауза после flood-control (``penalize``) ждётся целиком (не более 15 минут): отправлять во время паузы
        значит получить новый бан от Telegram.
        """
        waited = 0.0
        paused_total = 0.0
        while True:
            paused = await self.pause_left(name)
            if paused > 0:
                if paused_total >= 900:
                    return
                step = min(paused, 1.0)
                await asyncio.sleep(step)
                paused_total += step
                continue
            wait = float(await self.redis.eval(_BUCKET, 1, self.prefix + name, rate, burst, cost)) / 1000.0
            if wait <= 0:
                return
            if waited >= max_wait:
                return  # лучше отправить, чем зависнуть навсегда
            step = min(wait, 1.0)
            await asyncio.sleep(step)
            waited += step

    async def allow(self, name: str, rate: float, burst: float) -> bool:
        """Неблокирующая проверка (для веб-лимитов по IP)."""
        try:
            return await self.reserve(name, rate, burst) <= 0
        except Exception:  # noqa: BLE001 — при сбое Redis не блокируем пользователей
            log.debug("ratelimit failed open", exc_info=True)
            return True
