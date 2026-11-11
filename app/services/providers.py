"""Адаптеры платёжных провайдеров.

PaymentService не знает деталей ЮKassa/CryptoBot: он вызывает create() и status(),
а адаптер приводит ответы провайдера к единому виду (Invoice / Status).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.core.provider_status import Status, map_yookassa, parse_cryptobot
from app.db.models import Payment, User
from app.integrations.cryptobot import CryptoBotError
from app.integrations.yookassa import YooKassaError, fmt_amount

if TYPE_CHECKING:  # pragma: no cover
    from app.context import AppContext

log = logging.getLogger(__name__)


BRAND = "AmpireVPN"


class ProviderError(Exception):
    def __init__(self, message: str, *, forbidden: bool = False, transient: bool = False) -> None:
        super().__init__(message)
        self.forbidden = forbidden  # провайдер отказал в операции (например, автоплатежи не включены) — касается всех
        # исход запроса неизвестен/временный (таймаут, обрыв, 5xx): платёж могли провести — отменять нельзя,
        # повторяем с тем же ключом идемпотентности
        self.transient = transient


@dataclass(slots=True)
class Invoice:
    external_id: str
    pay_url: str | None = None
    status: Status | None = None    # заполняется, если провайдер сразу вернул итог (автосписание)


# ───────────────────────── ЮKassa ─────────────────────────
class YooKassaProvider:
    code = "yookassa"
    title = "Карта или СБП"
    emoji = "💳"

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def ready(self) -> bool:
        return self.ctx.yk is not None

    def available(self) -> bool:
        return self.ready() and bool(self.ctx.cfg.get("pay_yookassa_enabled"))

    def _receipt(self, email: str, description: str, amount: Decimal) -> dict:
        st = self.ctx.settings
        receipt: dict[str, Any] = {
            "customer": {"email": email},
            "items": [
                {
                    "description": description[:128],
                    "quantity": "1.00",
                    "amount": fmt_amount(amount),
                    "vat_code": st.yookassa_vat_code,
                    "payment_mode": "full_payment",
                    "payment_subject": "service",
                }
            ],
        }
        if st.yookassa_tax_system_code:
            receipt["tax_system_code"] = st.yookassa_tax_system_code
        return receipt

    map_status = staticmethod(map_yookassa)

    def _receipt_for(self, pay: Payment, user: User) -> dict | None:
        """Чек строится по email, сохранённому в платеже: при повторе запроса с тем же ключом тело должно совпадать."""
        email = pay.receipt_email or (user.email if self.ctx.cfg.get("receipts_enabled") else None)
        if self.ctx.cfg.get("receipts_enabled") and email:
            return self._receipt(email, pay.description or BRAND, pay.amount)
        return None

    @staticmethod
    def _metadata(pay: Payment, user: User, **extra: str) -> dict:
        """Метаданные платежа: по ним вебхук и сверка находят нашу строку, даже если external_id не успел сохраниться."""
        meta = pay.meta or {}
        data = {
            "payment_id": str(pay.id),
            "user_id": str(user.id),
            "plan": pay.plan_code,
            "kind": pay.kind,
            "months": str(meta.get("months", 1)),
        }
        if meta.get("devices"):
            data["devices"] = str(meta["devices"])
        data.update(extra)
        return data

    async def create(self, pay: Payment, user: User, *, save_method: bool = False) -> Invoice:
        assert self.ctx.yk is not None
        try:
            obj = await self.ctx.yk.create_payment(
                amount=pay.amount,
                description=pay.description or BRAND,
                return_url=f"https://t.me/{self.ctx.bot_username}",
                metadata=self._metadata(pay, user),
                idempotence_key=f"ampire-pay-{pay.id}",
                receipt=self._receipt_for(pay, user),
                save_payment_method=save_method,
            )
        except YooKassaError as e:
            raise ProviderError(str(e), forbidden=e.forbidden, transient=e.transient) from e
        return Invoice(str(obj["id"]), (obj.get("confirmation") or {}).get("confirmation_url"))

    async def charge_saved(self, pay: Payment, user: User, method_id: str) -> Invoice:
        """Списание по сохранённому способу оплаты (автопродление) — без участия пользователя."""
        assert self.ctx.yk is not None
        try:
            obj = await self.ctx.yk.create_payment(
                amount=pay.amount,
                description=pay.description or BRAND,
                metadata=self._metadata(pay, user, autopay="1"),
                idempotence_key=f"ampire-auto-{pay.id}",   # один ключ на платёж: повтор после таймаута не создаст второго списания
                receipt=self._receipt_for(pay, user),
                payment_method_id=method_id,
            )
        except YooKassaError as e:
            raise ProviderError(str(e), forbidden=e.forbidden, transient=e.transient) from e
        return Invoice(str(obj["id"]), None, self.map_status(obj))

    async def status(self, pay: Payment) -> Status:
        assert self.ctx.yk is not None and pay.external_id
        try:
            return self.map_status(await self.ctx.yk.get_payment(pay.external_id))
        except YooKassaError as e:
            raise ProviderError(str(e)) from e

    async def statuses(self, pays: list[Payment]) -> dict[int, Status]:
        out: dict[int, Status] = {}
        for p in pays:
            try:
                out[p.id] = await self.status(p)
            except ProviderError as e:
                log.warning("ЮKassa: статус платежа #%s не получен: %s", p.id, e)
        return out


# ───────────────────────── CryptoBot ─────────────────────────
class CryptoBotProvider:
    code = "cryptobot"
    title = "Криптовалюта"
    emoji = "🪙"

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    def ready(self) -> bool:
        return self.ctx.crypto is not None

    def available(self) -> bool:
        return self.ready() and bool(self.ctx.cfg.get("pay_crypto_enabled"))

    @staticmethod
    def parse(inv: dict, pay: Payment) -> Status:
        return parse_cryptobot(inv)

    async def create(self, pay: Payment, user: User, *, save_method: bool = False) -> Invoice:
        assert self.ctx.crypto is not None
        try:
            inv = await self.ctx.crypto.create_invoice(
                amount=pay.amount,
                description=pay.description or BRAND,
                payload=f"p{pay.id}",
                expires_in=3600,
                accepted_assets=self.ctx.settings.crypto_asset_list or None,
                paid_btn_url=f"https://t.me/{self.ctx.bot_username}",
            )
        except CryptoBotError as e:
            raise ProviderError(str(e)) from e
        return Invoice(str(inv["invoice_id"]), inv.get("bot_invoice_url") or inv.get("pay_url"))

    async def statuses(self, pays: list[Payment]) -> dict[int, Status]:
        assert self.ctx.crypto is not None
        ids = [p.external_id for p in pays if p.external_id]
        if not ids:
            return {}
        try:
            invoices = {str(i.get("invoice_id")): i for i in await self.ctx.crypto.get_invoices(ids)}
        except CryptoBotError as e:
            raise ProviderError(str(e)) from e
        out: dict[int, Status] = {}
        for p in pays:
            inv = invoices.get(str(p.external_id))
            out[p.id] = self.parse(inv, p) if inv else Status("pending")
        return out

    async def status(self, pay: Payment) -> Status:
        return (await self.statuses([pay])).get(pay.id, Status("pending"))


# ───────────────────────── общие правила ─────────────────────────
STALE_AFTER = timedelta(hours=3)


def is_stale(created_at: datetime, now: datetime) -> bool:
    """Платёж «висит» слишком долго: ссылка давно истекла у любого провайдера."""
    return now - created_at > STALE_AFTER
