import asyncio
import unittest
from datetime import timedelta
from decimal import Decimal

from app.core.backoff import outbox_delay, referral_check_delay, send_retry_delay, sync_backoff
from app.core.fanout import classify_send_error, run_chunk
from app.core.settlement import extract_cryptobot_refs, extract_yookassa_refs, settle_action

AMT = Decimal("249.00")


class Settlement(unittest.TestCase):
    def act(self, status="pending", applied=False, state="paid", amount=AMT, expected=AMT):
        return settle_action(status=status, applied=applied, provider_state=state, provider_amount=amount, expected=expected)

    def test_paid_exact_amount_is_applied(self):
        self.assertEqual(self.act(), "apply")
        self.assertEqual(self.act(amount=Decimal("249")), "apply")

    def test_wrong_or_missing_amount_goes_to_review_once(self):
        self.assertEqual(self.act(amount=Decimal("100.00")), "review")
        self.assertEqual(self.act(amount=None), "review")
        # повторная проверка уже помеченного платежа не должна снова слать алерт
        self.assertEqual(self.act(status="needs_review", amount=Decimal("100.00")), "noop")

    def test_idempotent(self):
        self.assertEqual(self.act(applied=True), "noop")
        for st in ("succeeded", "refunded", "refunding"):
            self.assertEqual(self.act(status=st), "noop")

    def test_cancel_only_pending(self):
        self.assertEqual(self.act(state="failed"), "cancel")
        self.assertEqual(self.act(state="failed", status="needs_review"), "noop")
        self.assertEqual(self.act(state="pending"), "noop")

    def test_payment_we_wrongly_canceled_is_recovered_when_provider_says_paid(self):
        self.assertEqual(self.act(status="canceled"), "apply")


class Webhooks(unittest.TestCase):
    def test_yookassa_refs_use_metadata(self):
        payload = {"event": "payment.succeeded", "object": {"id": "2f-abc", "metadata": {"payment_id": "77", "user_id": "5001"}}}
        r = extract_yookassa_refs(payload)
        self.assertEqual((r.event, r.external_id, r.payment_id, r.user_id), ("payment.succeeded", "2f-abc", 77, 5001))

    def test_garbage_metadata_is_safe(self):
        r = extract_yookassa_refs({"event": "payment.succeeded", "object": {"id": "x", "metadata": {"payment_id": "../1", "user_id": None}}})
        self.assertEqual((r.payment_id, r.user_id), (None, None))
        self.assertEqual(extract_yookassa_refs({}).external_id, "")
        self.assertEqual(extract_yookassa_refs({"object": "oops"}).external_id, "")

    def test_refund_event_points_to_payment(self):
        r = extract_yookassa_refs({"event": "refund.succeeded", "object": {"id": "rf-1", "payment_id": "2f-abc"}})
        self.assertEqual((r.external_id, r.refund_of), ("rf-1", "2f-abc"))

    def test_cryptobot_refs(self):
        r = extract_cryptobot_refs({"update_type": "invoice_paid", "payload": {"invoice_id": 42, "payload": "p123"}})
        self.assertEqual((r.external_id, r.payment_id), ("42", 123))
        self.assertIsNone(extract_cryptobot_refs({"payload": {"invoice_id": 1, "payload": "zzz"}}).payment_id)


class Backoff(unittest.TestCase):
    def test_sync_backoff_is_capped_at_ten_minutes(self):
        vals = [sync_backoff(n) for n in range(1, 12)]
        self.assertEqual(vals[0], timedelta(seconds=60))
        self.assertEqual(max(vals), timedelta(minutes=10))
        self.assertEqual(vals, sorted(vals))

    def test_referral_polling_slows_with_age(self):
        ages = [timedelta(hours=h) for h in (1, 8, 48, 24 * 5, 24 * 30)]
        delays = [referral_check_delay(a) for a in ages]
        self.assertEqual(delays, sorted(delays))
        self.assertEqual(delays[0], timedelta(minutes=15))
        self.assertEqual(delays[-1], timedelta(hours=24))

    def test_polling_load_is_bounded_for_big_backlog(self):
        # 50 000 ожидающих. Реальный «хвост» состоит в основном из давних неподключившихся:
        # 5% моложе 6 ч, 5% до суток, 10% до 3 сут, 30% до 14 сут, 50% старше.
        n = 50_000
        mix = [(0.05, timedelta(hours=3)), (0.05, timedelta(hours=12)), (0.10, timedelta(days=2)), (0.30, timedelta(days=8)), (0.50, timedelta(days=40))]
        per_hour = sum(n * share * 3600 / referral_check_delay(age).total_seconds() for share, age in mix)
        legacy = n * 2          # раньше: каждого раз в 30 минут
        self.assertLess(per_hour, legacy / 5)

    def test_send_and_outbox_delays(self):
        self.assertLess(send_retry_delay(0), send_retry_delay(3))
        self.assertEqual(send_retry_delay(99), 30.0)
        self.assertEqual(outbox_delay(50), timedelta(minutes=30))


class Fanout(unittest.TestCase):
    def test_runs_in_parallel_but_respects_limit_and_order(self):
        active = peak = 0

        async def work(x):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return x * 2

        out = asyncio.run(run_chunk(list(range(40)), work, concurrency=8))
        self.assertEqual(out, [x * 2 for x in range(40)])
        self.assertEqual(peak, 8)

    def test_parallel_is_much_faster_than_sequential(self):
        async def work(_):
            await asyncio.sleep(0.02)

        loop = asyncio.new_event_loop()
        try:
            t0 = loop.time()
            loop.run_until_complete(run_chunk(list(range(40)), work, concurrency=20))
            elapsed = loop.time() - t0
        finally:
            loop.close()
        self.assertLess(elapsed, 0.02 * 40 / 4)   # последовательно было бы 0.8 с

    def test_classify_errors(self):
        self.assertEqual(classify_send_error("Bad Request: chat not found"), "blocked")
        self.assertEqual(classify_send_error("Forbidden: bot was blocked by the user"), "blocked")
        self.assertEqual(classify_send_error("Bad Request: message to copy not found"), "source_gone")
        self.assertEqual(classify_send_error("Bad Request: MESSAGE_ID_INVALID"), "source_gone")
        self.assertEqual(classify_send_error("Bad Request: wrong file identifier"), "failed")


if __name__ == "__main__":
    unittest.main()
