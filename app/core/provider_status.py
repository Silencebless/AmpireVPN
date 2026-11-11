"""Приведение ответов платёжных провайдеров к единому виду (чистые функции — покрыты тестами).

Именно здесь решается, считать ли платёж оплаченным и на какую сумму: ошибка тут = потеря денег.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(slots=True)
class Status:
    state: str                      # "pending" | "paid" | "failed"
    amount: Decimal | None = None   # подтверждённая сумма в рублях (для сверки с нашим счётом)
    reason: str | None = None
    meta: dict = field(default_factory=dict)


def to_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None


def map_yookassa(obj: dict) -> Status:
    st = obj.get("status")
    if st == "succeeded" and obj.get("paid"):
        return Status("paid", to_decimal((obj.get("amount") or {}).get("value")), meta={"payment_method": obj.get("payment_method")})
    if st == "canceled":
        return Status("failed", reason=str((obj.get("cancellation_details") or {}).get("reason") or "canceled"))
    return Status("pending")


def parse_cryptobot(inv: dict) -> Status:
    st = inv.get("status")
    if st == "paid":
        # сверяем именно сумму СЧЁТА в рублях: чужой или изменённый счёт (другая валюта) не пройдёт
        fiat_ok = inv.get("currency_type") == "fiat" and str(inv.get("fiat")) == "RUB"
        meta = {
            "asset": inv.get("paid_asset"),
            "paid_amount": inv.get("paid_amount"),
            "rate": inv.get("paid_fiat_rate"),
            "fee_asset": inv.get("fee_asset"),
            "fee_amount": inv.get("fee_amount"),
        }
        return Status("paid", to_decimal(inv.get("amount")) if fiat_ok else None, meta=meta)
    if st == "expired":
        return Status("failed", reason="expired")
    return Status("pending")
