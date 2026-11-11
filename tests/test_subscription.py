import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.pricing import (
    PROMO_FIXED,
    PROMO_PERCENT,
    build_quote,
    discount_amount,
    upgrade_extra,
)
from app.core.subscription import (
    SubState,
    Tier,
    grant,
    normalize,
    purchase,
    remove,
    set_exact,
)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
D = lambda n: timedelta(days=n)  # noqa: E731


class StateMachine(unittest.TestCase):
    def test_empty_grant_starts_now(self):
        s = grant(SubState(), Tier.PREMIUM, D(7), NOW)
        self.assertEqual((s.tier, s.expires_at, s.queued_lite), (Tier.PREMIUM, NOW + D(7), timedelta(0)))

    def test_same_tier_extends(self):
        s = SubState(Tier.LITE, NOW + D(10))
        s = grant(s, Tier.LITE, D(30), NOW)
        self.assertEqual(s.expires_at, NOW + D(40))

    def test_premium_then_lite_is_queued(self):
        s = SubState(Tier.PREMIUM, NOW + D(10))
        s = purchase(s, Tier.LITE, D(30), NOW)
        self.assertEqual(s.tier, Tier.PREMIUM)
        self.assertEqual(s.expires_at, NOW + D(10))
        self.assertEqual(s.queued_lite, D(30))
        self.assertEqual(s.total_end, NOW + D(40))

    def test_queued_lite_activates_exactly_when_premium_ends(self):
        s = SubState(Tier.PREMIUM, NOW + D(10), D(30))
        later = NOW + D(12)  # воркер опоздал на 2 дня
        n = normalize(s, later)
        self.assertEqual(n.tier, Tier.LITE)
        self.assertEqual(n.expires_at, NOW + D(40))  # LITE считается от конца PREMIUM

    def test_everything_expired(self):
        s = SubState(Tier.PREMIUM, NOW + D(10), D(30))
        n = normalize(s, NOW + D(50))
        self.assertIsNone(n.tier)
        self.assertEqual(n.expires_at, NOW + D(40))

    def test_upgrade_converts_remaining_lite(self):
        s = SubState(Tier.LITE, NOW + D(15))
        s = purchase(s, Tier.PREMIUM, D(30), NOW, upgrade=True)
        self.assertEqual(s.tier, Tier.PREMIUM)
        self.assertEqual(s.expires_at, NOW + D(45))  # 15 + 30 дней PREMIUM
        self.assertEqual(s.queued_lite, timedelta(0))

    def test_premium_bonus_on_lite_pauses_lite(self):
        s = SubState(Tier.LITE, NOW + D(15))
        s = grant(s, Tier.PREMIUM, D(7), NOW)
        self.assertEqual((s.tier, s.expires_at, s.queued_lite), (Tier.PREMIUM, NOW + D(7), D(15)))
        self.assertEqual(s.total_end, NOW + D(22))

    def test_trial_then_buy_lite_is_queued(self):
        trial = grant(SubState(), Tier.PREMIUM, D(7), NOW)
        s = purchase(trial, Tier.LITE, D(30), NOW + D(1))
        self.assertEqual(s.tier, Tier.PREMIUM)
        self.assertEqual(s.queued_lite, D(30))

    def test_remove_from_active_tier(self):
        s = SubState(Tier.PREMIUM, NOW + D(10), D(5))
        r = remove(s, Tier.PREMIUM, D(4), NOW)
        self.assertEqual(r.expires_at, NOW + D(6))
        r = remove(s, Tier.PREMIUM, D(10), NOW)  # премиум полностью снят -> стартует LITE
        self.assertEqual((r.tier, r.expires_at), (Tier.LITE, NOW + D(5)))
        r = remove(SubState(Tier.LITE, NOW + D(3)), Tier.LITE, D(5), NOW)
        self.assertIsNone(r.tier)

    def test_remove_queued_lite(self):
        s = SubState(Tier.PREMIUM, NOW + D(10), D(30))
        r = remove(s, Tier.LITE, D(10), NOW)
        self.assertEqual(r.queued_lite, D(20))
        r = remove(s, Tier.LITE, D(100), NOW)
        self.assertEqual(r.queued_lite, timedelta(0))

    def test_set_exact(self):
        self.assertIsNone(set_exact(None, None, NOW).tier)
        s = set_exact(Tier.LITE, NOW + D(3), NOW)
        self.assertEqual((s.tier, s.expires_at), (Tier.LITE, NOW + D(3)))
        self.assertIsNone(set_exact(Tier.LITE, NOW - D(1), NOW).tier)


class Pricing(unittest.TestCase):
    P_LITE, P_PREM = Decimal("149"), Decimal("249")

    def test_upgrade_extra_matches_spec_example(self):
        # осталось 15 из 30 дней: (249-149)*15/30 = 50
        self.assertEqual(upgrade_extra(self.P_LITE, self.P_PREM, 30, D(15)), Decimal("50"))
        # итого 50 + 249 = 299
        st = SubState(Tier.LITE, NOW + D(15))
        q = build_quote(st, Tier.PREMIUM, price_lite=self.P_LITE, price_premium=self.P_PREM,
                        period_days=30, now=NOW, want_upgrade=True)
        self.assertEqual(q.kind, "upgrade")
        self.assertEqual(q.amount, Decimal("299.00"))
        self.assertEqual(q.after.tier, Tier.PREMIUM)
        self.assertEqual(q.after.expires_at, NOW + D(45))

    def test_upgrade_rounds_up(self):
        self.assertEqual(upgrade_extra(self.P_LITE, self.P_PREM, 30, timedelta(days=14, hours=12)), Decimal("49"))
        self.assertEqual(upgrade_extra(self.P_LITE, self.P_PREM, 30, timedelta(hours=1)), Decimal("1"))

    def test_no_upgrade_when_not_lite(self):
        q = build_quote(SubState(), Tier.PREMIUM, price_lite=self.P_LITE, price_premium=self.P_PREM,
                        period_days=30, now=NOW, want_upgrade=True)
        self.assertEqual((q.kind, q.amount), ("buy", Decimal("249.00")))

    def test_queue_quote(self):
        st = SubState(Tier.PREMIUM, NOW + D(10))
        q = build_quote(st, Tier.LITE, price_lite=self.P_LITE, price_premium=self.P_PREM,
                        period_days=30, now=NOW)
        self.assertEqual(q.amount, Decimal("149.00"))
        self.assertEqual(q.after.queued_lite, D(30))

    def test_discounts(self):
        self.assertEqual(discount_amount(Decimal("249"), PROMO_PERCENT, Decimal("20")), Decimal("50.00"))
        self.assertEqual(discount_amount(Decimal("249"), PROMO_FIXED, Decimal("100")), Decimal("100.00"))
        # не уходим ниже 1 ₽
        self.assertEqual(discount_amount(Decimal("100"), PROMO_FIXED, Decimal("500")), Decimal("99.00"))
        self.assertEqual(discount_amount(Decimal("100"), PROMO_PERCENT, Decimal("100")), Decimal("99.00"))
        self.assertEqual(discount_amount(Decimal("100"), None, None), Decimal("0.00"))

    def test_quote_with_promo(self):
        q = build_quote(SubState(), Tier.LITE, price_lite=self.P_LITE, price_premium=self.P_PREM,
                        period_days=30, now=NOW, promo_kind=PROMO_PERCENT, promo_value=Decimal("10"))
        self.assertEqual((q.base, q.discount, q.amount), (Decimal("149.00"), Decimal("15.00"), Decimal("134.00")))


if __name__ == "__main__":
    unittest.main()
