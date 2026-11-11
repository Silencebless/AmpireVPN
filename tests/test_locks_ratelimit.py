"""Проверка логики распределённых блокировок, лимитера и кэша на «поддельном Redis».

Fake реализует только используемые команды и эмулирует Lua-скрипты проекта теми же правилами,
так что тесты проверяют Python-часть (захват, ожидание, продление, снятие только владельцем, бакет).
"""
import asyncio
import time
import unittest

from app.infra import locks, ratelimit
from app.infra.cache import Cache


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, tuple[object, float | None]] = {}   # key -> (value, expire_at_monotonic)
        self.buckets: dict[str, tuple[float, float]] = {}

    def _alive(self, key):
        item = self.data.get(key)
        if item is None:
            return None
        if item[1] is not None and item[1] <= time.monotonic():
            del self.data[key]
            return None
        return item

    async def set(self, key, value, nx=False, px=None, ex=None):
        if nx and self._alive(key) is not None:
            return None
        ttl = (px / 1000.0) if px else (ex if ex else None)
        self.data[key] = (value, time.monotonic() + ttl if ttl else None)
        return True

    async def get(self, key):
        item = self._alive(key)
        return None if item is None else item[0]

    async def delete(self, *keys):
        for k in keys:
            self.data.pop(k, None)

    async def eval(self, script, numkeys, *args):
        key = args[0]
        if script == locks._RELEASE:
            item = self._alive(key)
            if item and item[0] == args[1]:
                del self.data[key]
                return 1
            return 0
        if script == locks._EXTEND:
            item = self._alive(key)
            if item and item[0] == args[1]:
                self.data[key] = (item[0], time.monotonic() + int(args[2]) / 1000.0)
                return 1
            return 0
        if script == ratelimit._BUCKET:
            rate, burst, cost = float(args[1]), float(args[2]), float(args[3])
            now = time.monotonic()
            tokens, ts = self.buckets.get(key, (burst, now))
            tokens = min(burst, tokens + max(0.0, now - ts) * rate)
            wait = 0
            if tokens >= cost:
                tokens -= cost
            else:
                wait = int(-(-((cost - tokens) / rate * 1000) // 1))
            self.buckets[key] = (tokens, now)
            return wait
        raise AssertionError("неизвестный скрипт")


def run(coro):
    return asyncio.run(coro)


class Locks(unittest.TestCase):
    def test_mutual_exclusion_and_release(self):
        async def go():
            r = FakeRedis()
            a = locks.RedisLock(r, "k", ttl=5, wait=0, soft=True)
            b = locks.RedisLock(r, "k", ttl=5, wait=0, soft=True)
            self.assertTrue(await a.__aenter__())
            self.assertFalse(await b.__aenter__())     # занято — мягкая блокировка возвращает False
            await a.__aexit__(None, None, None)
            self.assertTrue(await b.__aenter__())      # после снятия — свободно
            await b.__aexit__(None, None, None)
        run(go())

    def test_hard_lock_times_out(self):
        async def go():
            r = FakeRedis()
            a = locks.RedisLock(r, "k", ttl=5, wait=0)
            async with a:
                with self.assertRaises(locks.LockTimeout):
                    async with locks.RedisLock(r, "k", ttl=5, wait=0.15, poll=0.02):
                        pass
        run(go())

    def test_waiter_gets_lock_after_release(self):
        async def go():
            r = FakeRedis()
            order = []

            async def worker(name, hold):
                async with locks.RedisLock(r, "u", ttl=5, wait=3, poll=0.01):
                    order.append(f"{name}+")
                    await asyncio.sleep(hold)
                    order.append(f"{name}-")

            await asyncio.gather(worker("a", 0.1), worker("b", 0.0))
            # никакого перекрытия: после «x+» всегда сразу «x-»
            self.assertEqual(order[0][-1] + order[1][-1], "+-")
            self.assertEqual(order[2][-1] + order[3][-1], "+-")
        run(go())

    def test_only_owner_can_release(self):
        async def go():
            r = FakeRedis()
            a = locks.RedisLock(r, "k", ttl=5, wait=0, soft=True)
            await a.__aenter__()
            thief = locks.RedisLock(r, "k", ttl=5, wait=0, soft=True)
            thief._token = "чужой-токен"
            await thief.release()
            self.assertIsNotNone(await r.get("k"))     # замок остался за владельцем
            await a.release()
            self.assertIsNone(await r.get("k"))
        run(go())

    def test_watchdog_keeps_lock_alive_longer_than_ttl(self):
        async def go():
            r = FakeRedis()
            a = locks.RedisLock(r, "k", ttl=0.3, wait=0, soft=True)
            await a.__aenter__()
            await asyncio.sleep(0.9)                    # втрое дольше TTL — «сторож» продлевает
            other = locks.RedisLock(r, "k", ttl=0.3, wait=0, soft=True)
            self.assertFalse(await other.__aenter__())  # всё ещё занято
            await a.release()
            self.assertTrue(await other.__aenter__())
            await other.release()
        run(go())

    def test_lock_expires_if_owner_dies(self):
        async def go():
            r = FakeRedis()
            await r.set("k", "dead-owner", nx=True, px=100)
            await asyncio.sleep(0.15)
            self.assertTrue(await locks.RedisLock(r, "k", ttl=1, wait=0, soft=True).__aenter__())
        run(go())

    def test_manager_keys(self):
        m = locks.LockManager(FakeRedis())
        self.assertEqual(m.user(7).key, "ampire:lock:user:7")
        self.assertEqual(m.job("expiry").key, "ampire:lock:job:expiry")
        self.assertTrue(m.job("expiry").soft)


class BrokenRedis:
    """Redis, который упал: любая операция бросает исключение."""

    async def set(self, *_, **__):
        raise ConnectionError("redis down")

    async def get(self, *_, **__):
        raise ConnectionError("redis down")

    async def eval(self, *_, **__):
        raise ConnectionError("redis down")


class RedisOutage(unittest.TestCase):
    def test_user_lock_degrades_to_process_lock(self):
        async def go():
            m = locks.LockManager(BrokenRedis())
            order = []

            async def worker(name):
                async with m.user(5, wait=2):
                    order.append(f"{name}+")
                    await asyncio.sleep(0.05)
                    order.append(f"{name}-")

            await asyncio.gather(worker("a"), worker("b"))
            # без Redis блокировка продолжает работать внутри процесса — операции не перекрываются
            self.assertEqual(order[0][-1] + order[1][-1], "+-")
            self.assertEqual(order[2][-1] + order[3][-1], "+-")
        run(go())

    def test_job_lock_does_not_run_without_redis(self):
        async def go():
            async with locks.LockManager(BrokenRedis()).job("expiry") as got:
                self.assertFalse(got)       # задачу нельзя запускать «вслепую» сразу в нескольких репликах
        run(go())


class RateLimitPause(unittest.TestCase):
    def test_penalize_pauses_every_replica_sharing_the_bucket(self):
        async def go():
            r = FakeRedis()
            rl1, rl2 = ratelimit.RateLimiter(r), ratelimit.RateLimiter(r)
            self.assertEqual(await rl1.reserve("tg:bc", 100, 100), 0)
            await rl1.penalize("tg:bc", 0.3)
            self.assertGreater(await rl2.reserve("tg:bc", 100, 100), 0)     # другая реплика видит паузу
            t0 = time.monotonic()
            await rl2.acquire("tg:bc", 100, 100)
            self.assertGreaterEqual(time.monotonic() - t0, 0.25)
            self.assertEqual(await rl2.reserve("tg:bc", 100, 100), 0)       # пауза закончилась
        run(go())

    def test_other_buckets_are_independent(self):
        async def go():
            r = FakeRedis()
            rl = ratelimit.RateLimiter(r)
            await rl.penalize("tg:bc", 5)
            self.assertEqual(await rl.reserve("tg:bg", 10, 10), 0)          # рассылка не душит служебные сообщения
        run(go())


class RateLimit(unittest.TestCase):
    def test_burst_then_wait(self):
        async def go():
            rl = ratelimit.RateLimiter(FakeRedis())
            self.assertEqual(await rl.reserve("t", rate=10, burst=2), 0)
            self.assertEqual(await rl.reserve("t", rate=10, burst=2), 0)
            wait = await rl.reserve("t", rate=10, burst=2)
            self.assertTrue(0.05 < wait <= 0.11, wait)   # ~100 мс до следующего токена
        run(go())

    def test_acquire_paces_requests(self):
        async def go():
            rl = ratelimit.RateLimiter(FakeRedis())
            t0 = time.monotonic()
            for _ in range(6):
                await rl.acquire("t", rate=20, burst=1)
            elapsed = time.monotonic() - t0
            self.assertGreater(elapsed, 0.2)             # 5 ожиданий по ~50 мс
            self.assertLess(elapsed, 1.0)
        run(go())

    def test_allow_is_non_blocking(self):
        async def go():
            rl = ratelimit.RateLimiter(FakeRedis())
            results = [await rl.allow("ip", rate=1, burst=3) for _ in range(5)]
            self.assertEqual(results, [True, True, True, False, False])
        run(go())

    def test_independent_buckets(self):
        async def go():
            rl = ratelimit.RateLimiter(FakeRedis())
            self.assertTrue(await rl.allow("a", 1, 1))
            self.assertTrue(await rl.allow("b", 1, 1))
        run(go())


class CacheTests(unittest.TestCase):
    def test_remember_and_force(self):
        async def go():
            c = Cache(FakeRedis())
            calls = []

            async def factory():
                calls.append(1)
                return {"n": len(calls)}

            self.assertEqual(await c.remember("k", 60, factory), {"n": 1})
            self.assertEqual(await c.remember("k", 60, factory), {"n": 1})        # из кэша
            self.assertEqual(await c.remember("k", 60, factory, force=True), {"n": 2})
            await c.delete("k")
            self.assertEqual(await c.remember("k", 60, factory), {"n": 3})
        run(go())

    def test_redis_failure_is_a_miss_not_an_error(self):
        class Broken:
            async def get(self, *_):
                raise ConnectionError("redis down")

            async def set(self, *_, **__):
                raise ConnectionError("redis down")

            async def delete(self, *_):
                raise ConnectionError("redis down")

        async def go():
            c = Cache(Broken())

            async def factory():
                return [1, 2]

            self.assertEqual(await c.remember("k", 5, factory), [1, 2])
            await c.delete("k")
        run(go())


if __name__ == "__main__":
    unittest.main()
