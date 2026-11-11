#!/usr/bin/env python3
"""Нагрузочный тест веб-слоя и обработки апдейтов Telegram. ТОЛЬКО НА СТЕНДЕ — не на боевой базе!

Что делает: шлёт на вебхук Telegram синтетические апдейты «/start» и «нажатие кнопки» от случайных несуществующих
пользователей (они будут созданы в БД — используйте отдельную БД стенда) и параллельно опрашивает /dl. Печатает
RPS, долю ошибок и задержки p50/p95/p99. Боту в таком режиме отправка ответов в Telegram не удаётся (чужие chat_id) —
это нормально: измеряется приём и обработка, а не доставка.

    pip install httpx
    python scripts/loadtest.py --base https://stage.example.com --bot-token <BOT_TOKEN> --webhook-secret <WEBHOOK_SECRET> --rps 100 --duration 60

Ориентиры: ошибок < 1 %, p95 < 1 с при целевом RPS; смотрите при этом Grafana/Prometheus (пул БД, задержки панели, очередь задач).
Чтобы проверить рассылку и автопродление на когорте, создайте на стенде нужное число пользователей и запустите задачи воркера.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import random
import statistics
import time

import httpx


def update(uid: int, n: int) -> dict:
    base = {"update_id": n, "message": {"message_id": n, "date": int(time.time()), "chat": {"id": uid, "type": "private"},
                                        "from": {"id": uid, "is_bot": False, "first_name": f"load{uid}", "language_code": "ru"}}}
    if n % 3 == 0:
        base["message"].update(text="/menu", entities=[{"type": "bot_command", "offset": 0, "length": 5}])
    else:
        base["message"].update(text="/start", entities=[{"type": "bot_command", "offset": 0, "length": 6}])
    return base


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="https://домен-стенда")
    ap.add_argument("--bot-token", required=True, help="BOT_TOKEN стенда")
    ap.add_argument("--webhook-secret", required=True, help="WEBHOOK_SECRET стенда")
    ap.add_argument("--rps", type=int, default=50)
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--users", type=int, default=5000, help="сколько разных пользователей имитировать")
    args = ap.parse_args()

    # те же формулы, что в app/config.py: путь вебхука и секрет заголовка Telegram выводятся из настроек
    path = f"/tg/{args.webhook_secret}"
    secret_token = hashlib.sha256(f"tg:{args.bot_token}:{args.webhook_secret}".encode()).hexdigest()[:48]
    lat: list[float] = []
    errors = 0
    sent = 0
    sem = asyncio.Semaphore(max(10, args.rps * 2))
    async with httpx.AsyncClient(base_url=args.base, timeout=15.0) as client:

        async def one(n: int) -> None:
            nonlocal errors
            uid = 7_000_000_000 + random.randrange(args.users)
            t0 = time.perf_counter()
            try:
                if n % 10 == 0:
                    r = await client.get("/dl")
                else:
                    r = await client.post(path, json=update(uid, n), headers={"X-Telegram-Bot-Api-Secret-Token": secret_token})
                if r.status_code >= 500:
                    errors += 1
            except httpx.HTTPError:
                errors += 1
            finally:
                lat.append(time.perf_counter() - t0)
                sem.release()

        start = time.perf_counter()
        tasks: list[asyncio.Task] = []
        n = 0
        while (elapsed := time.perf_counter() - start) < args.duration:
            target = int(elapsed * args.rps)
            while n < target:
                await sem.acquire()
                tasks.append(asyncio.create_task(one(n)))
                n += 1
                sent += 1
            await asyncio.sleep(0.005)
        await asyncio.gather(*tasks)
        total = time.perf_counter() - start

    lat.sort()
    q = lambda p: lat[min(len(lat) - 1, int(len(lat) * p))] * 1000  # noqa: E731
    print(f"запросов: {sent} за {total:.1f} с ({sent / total:.0f} RPS), ошибок 5xx/сеть: {errors} ({errors / max(sent, 1):.2%})")
    print(f"задержка, мс: p50={q(0.50):.0f}  p95={q(0.95):.0f}  p99={q(0.99):.0f}  среднее={statistics.mean(lat) * 1000:.0f}")


if __name__ == "__main__":
    asyncio.run(main())
