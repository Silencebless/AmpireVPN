"""Клиент Remnawave 3.x.

Особенности 3.x, которые учтены:
* пользователи идентифицируются числовым ``id`` (поля ``uuid`` больше нет);
* 202/204 — ответ без тела (удаления, перезапуски и т.п.);
* без заголовков ``X-Forwarded-For`` и ``X-Forwarded-Proto: https`` панель рвёт соединение,
  если запрос идёт напрямую (``http://remnawave:3000``), поэтому мы всегда их добавляем;
* если панель стоит за Caddy Security — нужен ``X-Api-Key``; за cookie-защитой — Cookie.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import time
from datetime import datetime
from typing import Any

import httpx

from app.infra.metrics import metrics
from app.utils import iso_z

log = logging.getLogger(__name__)


class RemnaError(Exception):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class RemnaNotFound(RemnaError):
    pass


def usage_of(user: dict) -> dict:
    """Достаёт данные об использовании из ответа панели (поля могут лежать в userTraffic)."""
    nested = user.get("userTraffic") if isinstance(user.get("userTraffic"), dict) else {}

    def pick(name: str) -> Any:
        v = user.get(name)
        return v if v is not None else nested.get(name)

    def as_int(v: Any) -> int:
        try:
            return int(float(v or 0))
        except (TypeError, ValueError):
            return 0

    return {
        "used": as_int(pick("usedTrafficBytes")),
        "lifetime": as_int(pick("lifetimeUsedTrafficBytes")),
        "online_at": pick("onlineAt"),
        "first_connected_at": pick("firstConnectedAt"),
    }


class RemnaClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        proxy_headers: bool = True,
        caddy_key: str = "",
        cookie: str = "",
        timeout: float = 15.0,
        concurrency: int = 20,
    ) -> None:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "ampirevpn-bot/1.0",
        }
        if proxy_headers:
            headers["X-Forwarded-For"] = "127.0.0.1"
            headers["X-Forwarded-Proto"] = "https"
        if caddy_key:
            headers["X-Api-Key"] = caddy_key
        if cookie:
            headers["Cookie"] = cookie
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=6.0),
            limits=httpx.Limits(max_connections=concurrency + 10, max_keepalive_connections=concurrency),
        )
        # ограничение параллелизма и «предохранитель»: если панель лежит, не копим зависшие запросы
        self._sem = asyncio.Semaphore(concurrency)
        self._failures = 0
        self._open_until = 0.0
        self.breaker_threshold = 5
        self.breaker_cooldown = 20.0
        self._was_down = False      # были серии сбоев (панель считалась лежащей)
        self._recovered = False     # панель снова ответила после сбоев — сервис сбросит бэкофф синхронизации

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ предохранитель
    @property
    def circuit_open(self) -> bool:
        return time.monotonic() < self._open_until

    def _record(self, ok: bool) -> None:
        if ok:
            if self._was_down:
                self._was_down = False
                self._recovered = True
                log.info("Remnawave: панель снова отвечает")
                metrics.inc("panel_recovered_total")
            self._failures = 0
            return
        self._failures += 1
        if self._failures >= 3:
            self._was_down = True
        if self._failures >= self.breaker_threshold and not self.circuit_open:
            self._open_until = time.monotonic() + self.breaker_cooldown
            log.error("Remnawave: %s ошибок подряд — пауза запросов на %.0f с", self._failures, self.breaker_cooldown)
            metrics.inc("panel_circuit_opened_total")

    def take_recovered(self) -> bool:
        """True один раз после того, как панель ожила после серии сбоев."""
        was, self._recovered = self._recovered, False
        return was

    # ------------------------------------------------------------------ transport
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict | None = None,
        retries: int = 0,
    ) -> Any:
        if self.circuit_open:
            metrics.inc("panel_requests_total", status="circuit_open")
            raise RemnaError("Панель временно недоступна (защита от перегрузки)", 503, "CIRCUIT")
        url = f"/api{path}"
        async with self._sem:
            attempt = 0
            t0 = time.perf_counter()
            while True:
                try:
                    resp = await self._http.request(method, url, json=json, params=params)
                except httpx.TransportError as e:
                    self._record(False)
                    if attempt < retries and not self.circuit_open:
                        attempt += 1
                        await asyncio.sleep(0.6 * attempt)
                        continue
                    metrics.inc("panel_requests_total", status="error")
                    raise RemnaError(f"Панель недоступна ({type(e).__name__})") from e
                if resp.status_code in (502, 503, 504):
                    self._record(False)
                    if attempt < retries and not self.circuit_open:
                        attempt += 1
                        await asyncio.sleep(0.6 * attempt)
                        continue
                else:
                    self._record(True)
                break
            metrics.observe("panel_request_seconds", time.perf_counter() - t0, method=method)
            metrics.inc("panel_requests_total", status=f"{resp.status_code // 100}xx")

        if resp.status_code >= 400:
            msg, code = f"HTTP {resp.status_code}", None
            try:
                body = resp.json()
                if isinstance(body, dict):
                    msg = str(body.get("message") or body.get("error") or msg)
                    code = body.get("errorCode")
                    if body.get("errors"):
                        msg += " " + _json.dumps(body["errors"], ensure_ascii=False)[:300]
            except ValueError:
                pass
            cls = RemnaNotFound if resp.status_code == 404 else RemnaError
            raise cls(f"{method} {path}: {msg}", resp.status_code, code)

        if resp.status_code in (202, 204) or not resp.content:
            return None
        try:
            data = resp.json()
        except ValueError:
            return resp.text
        if isinstance(data, dict) and "response" in data:
            return data["response"]
        return data

    # ------------------------------------------------------------------ users
    @staticmethod
    def _unwrap_user(data: Any) -> dict:
        if isinstance(data, dict) and isinstance(data.get("user"), dict):
            return data["user"]
        return data if isinstance(data, dict) else {}

    async def create_user(
        self,
        *,
        username: str,
        expire_at: datetime,
        hwid_limit: int,
        squads: list[str],
        telegram_id: int | None,
        description: str = "",
        tag: str = "AMPIRE",
        traffic_limit_bytes: int = 0,
    ) -> dict:
        body: dict[str, Any] = {
            "username": username,
            "status": "ACTIVE",
            "trafficLimitBytes": max(0, int(traffic_limit_bytes)),
            "trafficLimitStrategy": "NO_RESET",
            "expireAt": iso_z(expire_at),
            "hwidDeviceLimit": hwid_limit,
            "description": description[:240],
            "tag": tag,
        }
        if telegram_id:
            body["telegramId"] = telegram_id
        if squads:
            body["activeInternalSquads"] = squads
        return self._unwrap_user(await self._request("POST", "/users", json=body))

    async def get_user(self, user_id: int) -> dict:
        return self._unwrap_user(await self._request("GET", f"/users/{user_id}", retries=2))

    async def resolve_user(self, *, username: str | None = None, short_uuid: str | None = None) -> dict | None:
        """Ищет пользователя по username/shortUuid. Ответ resolve урезан — дозагружаем полный."""
        body = {k: v for k, v in (("username", username), ("shortUuid", short_uuid)) if v}
        try:
            found = self._unwrap_user(await self._request("POST", "/users/resolve", json=body, retries=1))
        except RemnaNotFound:
            return None
        if found.get("id") is None:
            return None
        if not found.get("subscriptionUrl"):
            try:
                return await self.get_user(int(found["id"]))
            except RemnaError:
                return found
        return found

    async def update_user(self, user_id: int, **fields: Any) -> dict:
        body = {"id": user_id, **fields}
        return self._unwrap_user(await self._request("PATCH", "/users", json=body, retries=1))

    async def delete_user(self, user_id: int) -> None:
        await self._request("DELETE", f"/users/{user_id}")

    async def enable_user(self, user_id: int) -> None:
        await self._request("POST", f"/users/{user_id}/actions/enable")

    async def disable_user(self, user_id: int) -> None:
        await self._request("POST", f"/users/{user_id}/actions/disable")

    async def reset_traffic(self, user_id: int) -> None:
        await self._request("POST", f"/users/{user_id}/actions/reset-traffic")

    async def revoke_subscription(self, user_id: int) -> dict:
        """Выпускает новую ссылку подписки (меняется shortUuid)."""
        return self._unwrap_user(await self._request("POST", f"/users/{user_id}/actions/revoke", json={}))

    # ------------------------------------------------------------------ HWID
    async def list_devices(self, user_id: int) -> list[dict]:
        data = await self._request("GET", f"/hwid/devices/{user_id}", retries=2)
        if isinstance(data, dict):
            data = data.get("devices", [])
        return [d for d in (data or []) if isinstance(d, dict)]

    async def _hwid_write(self, path: str, body: dict) -> None:
        try:
            await self._request("POST", path, json=body)
        except RemnaError as e:
            if e.status not in (404, 405):
                raise
            # на части версий глагол отличается
            await self._request("DELETE", path, json=body)

    async def delete_device(self, user_id: int, hwid: str) -> None:
        await self._hwid_write("/hwid/devices/delete", {"userId": user_id, "hwid": hwid})

    async def delete_all_devices(self, user_id: int) -> None:
        await self._hwid_write("/hwid/devices/delete-all", {"userId": user_id})

    # ------------------------------------------------------------------ инфраструктура
    async def list_squads(self) -> list[dict]:
        data = await self._request("GET", "/internal-squads", retries=2)
        if isinstance(data, dict):
            data = data.get("internalSquads", [])
        return [s for s in (data or []) if isinstance(s, dict)]

    async def list_nodes(self) -> list[dict]:
        data = await self._request("GET", "/nodes", retries=2)
        if isinstance(data, dict):
            data = data.get("nodes", [])
        return [n for n in (data or []) if isinstance(n, dict)]

    async def node_action(self, node_uuid: str, action: str) -> None:
        if action not in ("enable", "disable", "restart"):
            raise ValueError(action)
        body = {"forceRestart": True} if action == "restart" else None
        await self._request("POST", f"/nodes/{node_uuid}/actions/{action}", json=body)

    async def system_stats(self) -> dict:
        data = await self._request("GET", "/system/stats", retries=1)
        return data if isinstance(data, dict) else {}
