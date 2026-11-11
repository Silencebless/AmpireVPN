import unittest
from decimal import Decimal

from app.core.provider_status import map_yookassa, parse_cryptobot


class YooKassaStatus(unittest.TestCase):
    def test_paid_with_saved_method(self):
        st = map_yookassa({"status": "succeeded", "paid": True, "amount": {"value": "249.00", "currency": "RUB"},
                           "payment_method": {"id": "pm-1", "saved": True, "title": "Bank card *4477"}})
        self.assertEqual((st.state, st.amount), ("paid", Decimal("249.00")))
        self.assertEqual(st.meta["payment_method"]["id"], "pm-1")

    def test_succeeded_but_not_paid_is_pending(self):
        self.assertEqual(map_yookassa({"status": "succeeded", "paid": False}).state, "pending")

    def test_canceled_reason(self):
        st = map_yookassa({"status": "canceled", "cancellation_details": {"party": "yoo_money", "reason": "insufficient_funds"}})
        self.assertEqual((st.state, st.reason), ("failed", "insufficient_funds"))
        self.assertEqual(map_yookassa({"status": "canceled"}).reason, "canceled")

    def test_pending_states(self):
        for s in ("pending", "waiting_for_capture", None):
            self.assertEqual(map_yookassa({"status": s}).state, "pending")


class CryptoBotStatus(unittest.TestCase):
    paid = {"invoice_id": 42, "status": "paid", "currency_type": "fiat", "fiat": "RUB", "amount": "249.00",
            "paid_asset": "USDT", "paid_amount": "2.71", "paid_fiat_rate": "91.9", "fee_asset": "USDT", "fee_amount": "0.08"}

    def test_paid_fiat_rub_amount_verified(self):
        st = parse_cryptobot(self.paid)
        self.assertEqual((st.state, st.amount), ("paid", Decimal("249.00")))
        self.assertEqual(st.meta["asset"], "USDT")
        self.assertEqual(st.meta["paid_amount"], "2.71")

    def test_wrong_currency_or_crypto_invoice_has_no_amount(self):
        # счёт выставлен не в рублях -> сумма не подтверждена -> PaymentService откажется начислять
        self.assertIsNone(parse_cryptobot({**self.paid, "fiat": "USD"}).amount)
        self.assertIsNone(parse_cryptobot({**self.paid, "currency_type": "crypto", "fiat": None}).amount)

    def test_expired_and_active(self):
        self.assertEqual((parse_cryptobot({"status": "expired"}).state, parse_cryptobot({"status": "expired"}).reason), ("failed", "expired"))
        self.assertEqual(parse_cryptobot({"status": "active"}).state, "pending")

    def test_garbage_amount(self):
        self.assertIsNone(parse_cryptobot({**self.paid, "amount": "abc"}).amount)


if __name__ == "__main__":
    unittest.main()
