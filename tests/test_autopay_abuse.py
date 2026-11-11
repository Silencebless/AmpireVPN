import unittest
from datetime import datetime, timedelta, timezone

from app.core.abuse import daily_cap_reached, hwids_overlap, old_enough
from app.core.autopay import RETRY_GAP, autopay_phase, renewal_tier
from app.core.subscription import SubState, Tier

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
H = lambda n: timedelta(hours=n)  # noqa: E731


class Autopay(unittest.TestCase):
    def test_renewal_tier(self):
        self.assertIsNone(renewal_tier(SubState()))
        self.assertEqual(renewal_tier(SubState(Tier.PREMIUM, NOW + H(5))), Tier.PREMIUM)
        self.assertEqual(renewal_tier(SubState(Tier.PREMIUM, NOW + H(5), timedelta(days=30))), Tier.LITE)

    def test_phases(self):
        far = SubState(Tier.LITE, NOW + H(48))
        self.assertIsNone(autopay_phase(far, NOW, None, None))
        near = SubState(Tier.LITE, NOW + H(10))
        self.assertEqual(autopay_phase(near, NOW, None, None), "notice")
        self.assertIsNone(autopay_phase(near, NOW, near.total_end, None))     # уже предупредили
        final = SubState(Tier.LITE, NOW + H(2))
        self.assertEqual(autopay_phase(final, NOW, None, None), "charge")
        self.assertIsNone(autopay_phase(final, NOW, None, NOW - RETRY_GAP + timedelta(minutes=1)))  # рано повторять
        self.assertEqual(autopay_phase(final, NOW, None, NOW - RETRY_GAP - timedelta(minutes=1)), "charge")

    def test_queue_uses_total_end(self):
        # PREMIUM заканчивается через 2 ч, но дальше 30 дней LITE — продлевать рано
        st = SubState(Tier.PREMIUM, NOW + H(2), timedelta(days=30))
        self.assertIsNone(autopay_phase(st, NOW, None, None))

    def test_inactive(self):
        self.assertIsNone(autopay_phase(SubState(Tier.LITE, NOW - H(1)), NOW, None, None))


class Abuse(unittest.TestCase):
    def test_overlap(self):
        self.assertTrue(hwids_overlap(["AbC", "x"], ["abc "]))
        self.assertFalse(hwids_overlap(["a"], ["b"]))
        self.assertFalse(hwids_overlap([], ["b"]))

    def test_cap(self):
        self.assertFalse(daily_cap_reached(100, 0))
        self.assertTrue(daily_cap_reached(5, 5))
        self.assertFalse(daily_cap_reached(4, 5))

    def test_age(self):
        self.assertTrue(old_enough(NOW, NOW, 0))
        self.assertFalse(old_enough(NOW - H(1), NOW, 2))
        self.assertTrue(old_enough(NOW - H(3), NOW, 2))


if __name__ == "__main__":
    unittest.main()
