import unittest
from datetime import datetime, timedelta, timezone

from app.core.autopay import (
    CHARGE_BEFORE,
    RETRY_GAP,
    autopay_phase,
    grace_extension,
    is_permanent_decline,
    max_attempts,
    next_attempt_at,
    renewal_tier,
)
from app.core.subscription import SubState, Tier

E = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)   # конец цикла
H = lambda n: timedelta(hours=n)  # noqa: E731
D = lambda n: timedelta(days=n)  # noqa: E731


class Schedule(unittest.TestCase):
    def test_attempt_times(self):
        self.assertEqual(next_attempt_at(E, 0), E - CHARGE_BEFORE)
        self.assertEqual(next_attempt_at(E, 1), E + D(1))      # D+1
        self.assertEqual(next_attempt_at(E, 2), E + D(3))      # D+3
        self.assertIsNone(next_attempt_at(E, 3))

    def test_grace_zero_means_single_attempt(self):
        self.assertEqual(max_attempts(0), 1)
        self.assertIsNone(next_attempt_at(E, 1, grace_days=0))
        self.assertEqual(max_attempts(3), 3)
        self.assertEqual(max_attempts(1), 2)
        self.assertEqual(next_attempt_at(E, 1, grace_days=1), E + D(1))
        self.assertIsNone(next_attempt_at(E, 2, grace_days=1))

    def test_grace_keeps_access_until_next_attempt_with_margin(self):
        # после первой неудачи подписка (конец E) дотягивается до D+1 плюс запас на попытку
        ext = grace_extension(E, 1, E)
        self.assertEqual(E + ext, E + D(1) + CHARGE_BEFORE)
        # после второй — до D+3 плюс запас
        end2 = E + ext
        ext2 = grace_extension(E, 2, end2)
        self.assertEqual(end2 + ext2, E + D(3) + CHARGE_BEFORE)
        # после третьей — больше не продлеваем
        self.assertEqual(grace_extension(E, 3, end2 + ext2), timedelta(0))

    def test_total_free_access_is_bounded(self):
        end = E
        for failures in (1, 2, 3):
            end += grace_extension(E, failures, end)
        self.assertLessEqual(end - E, D(3) + CHARGE_BEFORE)

    def test_retry_always_happens_while_subscription_is_still_active(self):
        end = E
        for failures in (1, 2):
            end += grace_extension(E, failures, end)
            attempt = next_attempt_at(E, failures)
            self.assertGreater(end, attempt)   # на момент попытки подписка ещё активна

    def test_renewal_tier(self):
        self.assertEqual(renewal_tier(SubState(Tier.PREMIUM, E, D(30))), Tier.LITE)
        self.assertEqual(renewal_tier(SubState(Tier.PREMIUM, E)), Tier.PREMIUM)
        self.assertIsNone(renewal_tier(SubState()))


class Phases(unittest.TestCase):
    def test_notice_then_charge(self):
        st = SubState(Tier.LITE, E)
        self.assertIsNone(autopay_phase(st, E - H(48), None, None))
        self.assertEqual(autopay_phase(st, E - H(20), None, None), "notice")
        self.assertIsNone(autopay_phase(st, E - H(20), E, None))      # уже предупредили о этом сроке
        self.assertEqual(autopay_phase(st, E - H(2), None, None), "charge")

    def test_guard_against_double_attempt(self):
        st = SubState(Tier.LITE, E)
        now = E - H(2)
        self.assertIsNone(autopay_phase(st, now, None, now - RETRY_GAP + timedelta(minutes=1)))

    def test_scheduled_retry_waits_for_its_time_even_if_expired(self):
        st = SubState(None, E)                                       # подписка уже снята воркером
        nxt = E + D(1)
        self.assertIsNone(autopay_phase(st, nxt - H(1), None, E - H(3), next_at=nxt, failures=1))
        self.assertEqual(autopay_phase(st, nxt + H(1), None, E - H(3), next_at=nxt, failures=1), "charge")

    def test_big_cohort_is_not_capped(self):
        # у фазы нет «лимита 300»: каждому подходящему пользователю — своё решение
        users = [SubState(Tier.LITE, E) for _ in range(10_000)]
        due = sum(1 for s in users if autopay_phase(s, E - H(2), None, None) == "charge")
        self.assertEqual(due, 10_000)


class Declines(unittest.TestCase):
    def test_permanent_vs_retryable(self):
        for r in ("permission_revoked", "card_expired", "Invalid_Card_Number"):
            self.assertTrue(is_permanent_decline(r), r)
        for r in ("insufficient_funds", "issuer_unavailable", "general_decline", "internal_timeout", "", None):
            self.assertFalse(is_permanent_decline(r), r)


if __name__ == "__main__":
    unittest.main()
