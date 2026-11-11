"""Распределённые блокировки на Redis (работают между репликами и процессами).

* user-lock сериализует операции над подпиской одного пользователя;
* job-lock гарантирует, что фоновая задача выполняется ровно одной репликой.
Блокировка продлевается «сторожем», пока держится, и снимается атомарно (только владельцем).

Redis — не жёсткая зависимость: если он недоступен, user-lock деградирует до блокировки внутри процесса
(целостность БД по-прежнему защищают построчные блокировки PostgreSQL), а job-lock просто пропускает итерацию —
задача не должна выполняться двумя репликами одновременно.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid

from typing import TYPE_CHECKING

from app.infra.metrics import metrics

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

log = logging.getLogger(__name__)

# аварийные блокировки внутри процесса — на случай недоступности Redis
_LOCAL: dict[str, asyncio.Lock] = {}

_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""
_EXTEND = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('pexpire', KEYS[1], ARGV[2]) end
return 0
"""


class LockTimeout(Exception):
    pass


class RedisLock:
    """async with RedisLock(...): …  — бросает LockTimeout; с soft=True возвращает bool вместо исключения."""

    def __init__(
        self,
        redis: Redis,
        key: str,
        *,
        ttl: float = 60.0,
        wait: float = 30.0,
        poll: float = 0.05,
        soft: bool = False,
        local_fallback: bool = False,
    ) -> None:
        self._redis = redis
        self.key = key
        self.ttl = ttl
        self.wait = wait
        self.poll = poll
        self.soft = soft
        self.local_fallback = local_fallback
        self._token: str | None = None
        self._task: asyncio.Task | None = None
        self._local: asyncio.Lock | None = None

    async def _acquire_local(self, deadline: float) -> bool:
        """Redis недоступен: блокировка в пределах процесса (между репликами не защищает)."""
        lock = _LOCAL.setdefault(self.key, asyncio.Lock())
        left = max(0.0, deadline - time.monotonic())
        if left <= 0:
            if lock.locked():
                return False
            await lock.acquire()
        else:
            try:
                await asyncio.wait_for(lock.acquire(), timeout=left)
            except asyncio.TimeoutError:
                return False
        self._local = lock
        return True

    async def acquire(self) -> bool:
        token = uuid.uuid4().hex
        deadline = time.monotonic() + self.wait
        while True:
            try:
                got = await self._redis.set(self.key, token, nx=True, px=int(self.ttl * 1000))
            except Exception:  # noqa: BLE001 — Redis недоступен
                metrics.inc("lock_redis_errors_total")
                if self.local_fallback:
                    log.warning("Redis недоступен — блокировка %s работает локально", self.key)
                    return await self._acquire_local(deadline)
                log.warning("Redis недоступен — блокировка %s не взята", self.key)
                return False
            if got:
                self._token = token
                self._task = asyncio.create_task(self._watchdog())
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self.poll + random.random() * self.poll)

    async def release(self) -> None:
        local, self._local = self._local, None
        if local is not None and local.locked():
            local.release()
        task, token = self._task, self._token
        self._task = self._token = None
        if task is not None:
            task.cancel()
        if token is not None:
            try:
                await self._redis.eval(_RELEASE, 1, self.key, token)
            except Exception:  # noqa: BLE001
                log.warning("не удалось снять блокировку %s (истечёт сама)", self.key)

    async def _watchdog(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.ttl / 3)
                if not self._token:
                    return
                ok = await self._redis.eval(_EXTEND, 1, self.key, self._token, int(self.ttl * 1000))
                if not ok:
                    log.warning("блокировка %s потеряна (истекла?)", self.key)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("watchdog блокировки %s остановлен", self.key, exc_info=True)

    async def __aenter__(self):
        ok = await self.acquire()
        if self.soft:
            return ok
        if not ok:
            raise LockTimeout(self.key)
        return True

    async def __aexit__(self, *exc) -> None:
        await self.release()


class LockManager:
    def __init__(self, redis: Redis, prefix: str = "ampire:lock:") -> None:
        self.redis = redis
        self.prefix = prefix

    def user(self, user_id: int, *, wait: float = 30.0) -> RedisLock:
        return RedisLock(self.redis, f"{self.prefix}user:{user_id}", ttl=60.0, wait=wait, local_fallback=True)

    def job(self, name: str, ttl: float = 300.0) -> RedisLock:
        """Фоновая задача: если блокировка занята — не ждём, пропускаем итерацию (async with … as got)."""
        return RedisLock(self.redis, f"{self.prefix}job:{name}", ttl=ttl, wait=0.0, soft=True)

    def named(self, name: str, *, ttl: float = 60.0, wait: float = 10.0) -> RedisLock:
        return RedisLock(self.redis, f"{self.prefix}{name}", ttl=ttl, wait=wait, local_fallback=True)
