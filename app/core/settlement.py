"""Чистые правила обработки платежей: что делать с ответом провайдера и как разобрать уведомление.

Ошибка здесь = потеря денег, поэтому логика вынесена из сервиса и покрыта тестами.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

# статусы платежа в нашей БД
PENDING = "pending"
SUCCEEDED = "succeeded"
CANCELED = "canceled"
REFUNDED = "refunded"
REFUNDING = "refunding"        # возврат запущен (идёт обращение к провайдеру)
NEEDS_REVIEW = "needs_review"  # деньги пришли, но автоматически начислять нельзя — решает человек
FINAL_STATUSES = frozenset({SUCCEEDED, REFUNDED, REFUNDING})


def settle_action(
    *,
    status: str,
    applied: bool,
    provider_state: str,
    provider_amount: Decimal | None,
    expected: Decimal,
) -> str:
    """'noop' | 'apply' | 'review' | 'cancel'.

    * оплачено и сумма совпала → начислить (в том числе платёж, который мы по ошибке успели отменить);
    * оплачено, но сумма не подтверждена или иная → на ручную проверку (ровно один раз, без повторных алертов);
    * провайдер отменил → отменить, но только ожидающий платёж.
    """
    if applied or status in FINAL_STATUSES:
        return "noop"
    if provider_state == "paid":
        if provider_amount is None or Decimal(provider_amount) != Decimal(expected):
            return "review" if status != NEEDS_REVIEW else "noop"
        return "apply"
    if provider_state == "failed":
        return "cancel" if status == PENDING else "noop"
    return "noop"


@dataclass(slots=True)
class Refs:
    event: str = ""
    external_id: str = ""
    payment_id: int | None = None   # наш id из metadata.payment_id
    user_id: int | None = None      # из metadata.user_id
    refund_of: str = ""             # для refund.* — id исходного платежа


def _to_int(v: Any) -> int | None:
    try:
        s = str(v).strip()
        return int(s) if s.isdigit() else None
    except (TypeError, ValueError):
        return None


def extract_yookassa_refs(payload: dict) -> Refs:
    obj = payload.get("object") if isinstance(payload.get("object"), dict) else {}
    meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    event = str(payload.get("event", ""))
    refs = Refs(event=event, external_id=str(obj.get("id") or ""), payment_id=_to_int(meta.get("payment_id")), user_id=_to_int(meta.get("user_id")))
    if event.startswith("refund."):
        refs.refund_of = str(obj.get("payment_id") or "")
    return refs


def extract_cryptobot_refs(payload: dict) -> Refs:
    inv = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    refs = Refs(event=str(payload.get("update_type", "")), external_id=str(inv.get("invoice_id") or ""))
    raw = str(inv.get("payload") or "")  # наша метка вида p123
    if raw.startswith("p"):
        refs.payment_id = _to_int(raw[1:])
    return refs
