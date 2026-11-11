"""Проверка ссылок на скачивание Happ.

Ссылки на приложение зашиты в страницу /dl (GitHub «latest», App Store, Google Play). Если разработчик Happ
переименует файл или уберёт приложение из магазина, кнопка «Скачать Happ» молча приведёт на 404 — а пользователь
не сможет подключиться. Воркер раз в несколько часов проверяет каждую ссылку, предупреждает владельцев и
автоматически подменяет «мёртвые» прямые ссылки GitHub на страницу релизов.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

import httpx

from app.infra.metrics import metrics
from app.web.pages import LINKS

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)

LINKS_KEY = "ampire:links"
ALERT_AFTER_FAILS = 2          # сколько проверок подряд должна быть «мертва» ссылка, чтобы поднять тревогу
USER_AGENT = "Mozilla/5.0 (compatible; AmpireVPN-linkcheck/1.0)"


def classify_status(code: int | None) -> str:
    """ok — ссылка живая; dead — её точно нет (404/410); unknown — сайт не пустил бота или временный сбой (не тревожим)."""
    if code is None:
        return "unknown"
    if 200 <= code < 400:
        return "ok"
    if code in (404, 410):
        return "dead"
    return "unknown"


class LinkChecker:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._memo: tuple[float, frozenset[str]] = (0.0, frozenset())

    async def _probe(self, client: httpx.AsyncClient, url: str) -> int | None:
        try:
            resp = await client.head(url)
            if resp.status_code in (405, 403, 501):  # не все магазины отвечают на HEAD
                resp = await client.get(url, headers={"Range": "bytes=0-0"})
            return resp.status_code
        except httpx.HTTPError:
            return None

    async def check_all(self) -> dict[str, str]:
        """Проверяет все ссылки, сохраняет итог в Redis, предупреждает о «мёртвых». Возвращает {ключ: статус}."""
        result: dict[str, str] = {}
        seen: dict[str, int | None] = {}
        async with httpx.AsyncClient(follow_redirects=True, timeout=12.0, headers={"User-Agent": USER_AGENT}) as client:
            for key, url in LINKS.items():
                if url not in seen:           # одинаковые адреса проверяем один раз
                    seen[url] = await self._probe(client, url)
                    await asyncio.sleep(0.2)
                code = seen[url]
                status = classify_status(code)
                result[key] = status
                try:
                    raw = await self.ctx.redis.hget(LINKS_KEY, key)
                    prev = json.loads(raw) if raw else {}
                    fails = int(prev.get("fails", 0)) + 1 if status == "dead" else 0
                    await self.ctx.redis.hset(LINKS_KEY, key, json.dumps({"s": status, "code": code, "ts": int(time.time()), "fails": fails}))
                except Exception:  # noqa: BLE001 — Redis недоступен: проверка всё равно полезна (логи)
                    fails = 1 if status == "dead" else 0
                metrics.set_gauge("happ_link_ok", 1 if status != "dead" else 0, link=key)
                if status == "dead" and fails >= ALERT_AFTER_FAILS:
                    await self.ctx.notify.alert_once(
                        f"link:{key}",
                        f"🔗 <b>Ссылка на Happ не работает</b>: <code>{key}</code> → {LINKS[key]} (код {code}).\n"
                        "Страница скачивания подменяет битые прямые ссылки GitHub на страницу релизов, но проверьте остальные: app/web/pages.py → LINKS.",
                        ttl=24 * 3600,
                    )
        self._memo = (0.0, frozenset())
        return result

    async def dead_keys(self) -> frozenset[str]:
        """Ключи ссылок, которые сейчас «мертвы» (кэш на минуту — страница /dl открывается часто)."""
        now = time.monotonic()
        if now - self._memo[0] < 60 and self._memo[0] > 0:
            return self._memo[1]
        dead: set[str] = set()
        try:
            raw = await self.ctx.redis.hgetall(LINKS_KEY)
            for k, v in (raw or {}).items():
                key = k.decode() if isinstance(k, bytes) else k
                try:
                    if json.loads(v).get("s") == "dead":
                        dead.add(key)
                except ValueError:
                    continue
        except Exception:  # noqa: BLE001
            pass
        self._memo = (now, frozenset(dead))
        return self._memo[1]

    async def report(self) -> list[tuple[str, str, int | None, int]]:
        """[(ключ, статус, код, возраст_проверки_сек)] для диагностики."""
        out: list[tuple[str, str, int | None, int]] = []
        try:
            raw = await self.ctx.redis.hgetall(LINKS_KEY)
        except Exception:  # noqa: BLE001
            return out
        now = int(time.time())
        for k, v in (raw or {}).items():
            key = k.decode() if isinstance(k, bytes) else k
            try:
                d = json.loads(v)
                out.append((key, str(d.get("s")), d.get("code"), now - int(d.get("ts", now))))
            except ValueError:
                continue
        return sorted(out)
