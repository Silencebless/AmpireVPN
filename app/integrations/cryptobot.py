"""Клиент Crypto Pay API (@CryptoBot).

Документация: https://help.send.tg/en/articles/10279948-crypto-pay-api
Счёт создаём в фиатной валюте (RUB) — клиент платит любой разрешённой монетой по текущему курсу.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Any

import httpx

log = logging.getLogger(__name__)

MAINNET = "https://pay.crypt.bot/api"
TESTNET = "https://testnet-pay.crypt.bot/api"


class CryptoBotError(Exception):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class CryptoBotClient:
    def __init__(self, token: str, *, testnet: bool = False, timeout: float = 20.0) -> None:
        self._http = httpx.AsyncClient(
            base_url=TESTNET if testnet else MAINNET,
            headers={"Crypto-Pay-API-Token": token, "User-Agent": "ampirevpn-bot/1.0"},
            timeout=httpx.Timeout(timeout, connect=8.0),
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, params: dict | None = None, *, retries: int = 0) -> Any:
        attempt = 0
        while True:
            try:
                resp = await self._http.post(f"/{method}", json=params or {})
            except httpx.TransportError as e:
                if attempt < retries:
                    attempt += 1
                    await asyncio.sleep(0.8 * attempt)
                    continue
                raise CryptoBotError(f"CryptoBot недоступен ({type(e).__name__})") from e
            if resp.status_code >= 500 and attempt < retries:
                attempt += 1
                await asyncio.sleep(0.8 * attempt)
                continue
            break
        try:
            data = resp.json()
        except ValueError:
            raise CryptoBotError(f"CryptoBot: некорректный ответ (HTTP {resp.status_code})") from None
        if not data.get("ok"):
            err = data.get("error") or {}
            raise CryptoBotError(f"{method}: {err.get('name') or err or resp.text[:200]}", err.get("code"))
        return data.get("result")

    async def get_me(self) -> dict:
        return await self._call("getMe", retries=1)

    async def create_invoice(
        self,
        *,
        amount: Decimal,
        description: str,
        payload: str,
        expires_in: int = 3600,
        fiat: str = "RUB",
        accepted_assets: list[str] | None = None,
        paid_btn_url: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {
            "currency_type": "fiat",
            "fiat": fiat,
            "amount": f"{Decimal(str(amount)):.2f}",
            "description": description[:1024],
            "payload": payload[:4000],
            "expires_in": expires_in,
            "allow_comments": False,
        }
        if accepted_assets:
            params["accepted_assets"] = ",".join(accepted_assets)
        if paid_btn_url:
            params["paid_btn_name"] = "openBot"
            params["paid_btn_url"] = paid_btn_url
        return await self._call("createInvoice", params)

    async def get_invoices(self, invoice_ids: list[int | str]) -> list[dict]:
        """До 1000 счетов за вызов."""
        result = await self._call(
            "getInvoices", {"invoice_ids": ",".join(str(i) for i in invoice_ids), "count": max(1, min(len(invoice_ids), 1000))}, retries=2
        )
        items = result.get("items", []) if isinstance(result, dict) else (result or [])
        return [i for i in items if isinstance(i, dict)]
