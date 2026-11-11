"""Сроки 1/3/6/12 месяцев, доп. устройства и — главное — устойчивость доплаты LITE → PREMIUM к смене цен."""
import itertools
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.pricing import (
    DEFAULT_TERM_DISCOUNTS,
    TERMS,
    build_devices_quote,
    build_quote,
    devices_term_cost,
    term_days,
    term_price,
    upgrade_extra,
)
from app.core.subscription import SubState, Tier

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
D = lambda n: timedelta(days=n)  # noqa: E731


class Terms(unittest.TestCase):
    def test_days(self):
        self.assertEqual([term_days(m) for m in TERMS], [30, 90, 180, 365])  # год — ровно 365 дней
        self.assertEqual(term_days(3, 31), 93)
        with self.assertRaises(ValueError):
            term_days(2)

    def test_prices(self):
        d = DEFAULT_TERM_DISCOUNTS
        self.assertEqual(term_price(149, 1, d[1]), Decimal("149.00"))
        self.assertEqual(term_price(149, 3, d[3]), Decimal("425"))
        self.assertEqual(term_price(149, 6, d[6]), Decimal("805"))
        self.assertEqual(term_price(149, 12, d[12]), Decimal("1430"))
        self.assertEqual(term_price(249, 12, d[12]), Decimal("2390"))

    def test_longer_term_is_never_more_expensive_per_month(self):
        for monthly in (Decimal("99"), Decimal("149"), Decimal("249.50"), Decimal("1000")):
            per_month = [term_price(monthly, m, DEFAULT_TERM_DISCOUNTS[m]) / m for m in TERMS]
            self.assertEqual(per_month, sorted(per_month, reverse=True))

    def test_discount_is_clamped(self):
        self.assertEqual(term_price(100, 3, 500), term_price(100, 3, 90))
        self.assertEqual(term_price(100, 3, -5), Decimal("300.00"))

    def test_minimum_price_is_one_ruble(self):
        self.assertGreaterEqual(term_price(1, 1, 90), Decimal("1"))


class UpgradeSurchargeIsRobustToPriceChanges(unittest.TestCase):
    PRICE_SETS = [
        (149, 249), (99, 399), (1, 2), (500, 501), (149, 149), (249, 149),  # апгрейд «дороже/равно/дешевле»
        (Decimal("149.50"), Decimal("249.99")), (10000, 25000),
    ]

    def test_never_negative_and_never_exceeds_premium_for_remaining_time(self):
        for (pl, pp), left in itertools.product(self.PRICE_SETS, (timedelta(seconds=1), timedelta(hours=7), D(1), D(15), D(30), D(300))):
            x = upgrade_extra(Decimal(str(pl)), Decimal(str(pp)), 30, left)
            self.assertGreaterEqual(x, 0, (pl, pp, left))
            full = Decimal(str(pp)) * Decimal(left.total_seconds()) / Decimal(30 * 86400)
            self.assertLessEqual(x, full.to_integral_value(rounding="ROUND_CEILING") + 1, (pl, pp, left))

    def test_matches_spec_for_any_prices(self):
        # (premium − lite) × осталось / период, вверх до рубля — для любых цен, а не только для 149/249
        for pl, pp in ((149, 249), (199, 499), (80, 160), (333, 777)):
            exp = ((Decimal(pp) - Decimal(pl)) * 12 / 30).quantize(Decimal(1), rounding="ROUND_CEILING")
            self.assertEqual(upgrade_extra(Decimal(pl), Decimal(pp), 30, D(12)), exp)

    def test_zero_when_premium_not_more_expensive(self):
        self.assertEqual(upgrade_extra(Decimal(249), Decimal(149), 30, D(20)), 0)
        self.assertEqual(upgrade_extra(Decimal(149), Decimal(149), 30, D(20)), 0)

    def test_monotonic_in_remaining_time(self):
        vals = [upgrade_extra(Decimal(149), Decimal(249), 30, D(n)) for n in range(0, 31)]
        self.assertEqual(vals, sorted(vals))

    def test_different_month_lengths_use_daily_rates(self):
        # у LITE «месяц» 30 дней, у PREMIUM 60 дней: суточные ставки 5 ₽ и 4.5 ₽ → апгрейд бесплатный, а не «отрицательный»
        self.assertEqual(upgrade_extra(Decimal(150), Decimal(270), 30, D(10), premium_period_days=60), 0)
        # LITE 150/30 = 5 ₽/сут, PREMIUM 600/60 = 10 ₽/сут → 5 ₽/сут × 10 дней = 50
        self.assertEqual(upgrade_extra(Decimal(150), Decimal(600), 30, D(10), premium_period_days=60), Decimal(50))

    def test_does_not_depend_on_term_or_term_discount(self):
        st = SubState(Tier.LITE, NOW + D(15))
        extras = set()
        for months in TERMS:
            q = build_quote(st, Tier.PREMIUM, price_lite=Decimal(149), price_premium=Decimal(249), period_days=30, now=NOW,
                            want_upgrade=True, months=months, term_discount_pct=DEFAULT_TERM_DISCOUNTS[months])
            extras.add(q.extra)
            self.assertEqual(q.kind, "upgrade")
            # остаток LITE + купленный срок PREMIUM
            self.assertEqual(q.after.tier, Tier.PREMIUM)
            self.assertEqual(q.after.expires_at, NOW + D(15) + D(term_days(months)))
        self.assertEqual(extras, {Decimal("50.00")})

    def test_quote_after_price_change_uses_new_prices_but_old_quote_is_frozen(self):
        st = SubState(Tier.LITE, NOW + D(15))
        old = build_quote(st, Tier.PREMIUM, price_lite=Decimal(149), price_premium=Decimal(249), period_days=30, now=NOW, want_upgrade=True)
        new = build_quote(st, Tier.PREMIUM, price_lite=Decimal(199), price_premium=Decimal(499), period_days=30, now=NOW, want_upgrade=True)
        self.assertEqual((old.extra, old.amount), (Decimal("50.00"), Decimal("299.00")))
        self.assertEqual((new.extra, new.amount), (Decimal("150.00"), Decimal("649.00")))
        # состояние после покупки от цен не зависит
        self.assertEqual(old.after, new.after)

    def test_upgrade_total_includes_devices_and_promo(self):
        st = SubState(Tier.LITE, NOW + D(15))
        q = build_quote(st, Tier.PREMIUM, price_lite=Decimal(149), price_premium=Decimal(249), period_days=30, now=NOW, want_upgrade=True,
                        months=1, extra_devices=2, device_price=25, promo_kind="fixed", promo_value=Decimal(30))
        self.assertEqual((q.plan_amount, q.devices_amount, q.extra), (Decimal("249.00"), Decimal("50.00"), Decimal("50.00")))
        self.assertEqual((q.base, q.discount, q.amount), (Decimal("349.00"), Decimal("30.00"), Decimal("319.00")))


class RenewalTerms(unittest.TestCase):
    def test_renew_extends_current_tier(self):
        st = SubState(Tier.PREMIUM, NOW + D(10))
        q = build_quote(st, Tier.PREMIUM, price_lite=Decimal(149), price_premium=Decimal(249), period_days=30, now=NOW, months=6,
                        term_discount_pct=10)
        self.assertEqual(q.kind, "buy")
        self.assertEqual(q.amount, Decimal("1345.00"))
        self.assertEqual(q.after.expires_at, NOW + D(10 + 180))

    def test_devices_are_billed_per_month_and_discounted_with_term(self):
        self.assertEqual(devices_term_cost(2, 25, 1), Decimal("50.00"))
        self.assertEqual(devices_term_cost(2, 25, 12, 20), Decimal("480"))
        self.assertEqual(devices_term_cost(0, 25, 12, 20), 0)
        self.assertEqual(devices_term_cost(3, 0, 12, 20), 0)

    def test_new_buyer_term_quote(self):
        for months in TERMS:
            q = build_quote(SubState(), Tier.LITE, price_lite=Decimal(149), price_premium=Decimal(249), period_days=30, now=NOW, months=months,
                            term_discount_pct=DEFAULT_TERM_DISCOUNTS[months])
            self.assertEqual(q.period_days, term_days(months))
            self.assertEqual(q.after.expires_at, NOW + D(term_days(months)))


class DevicesAddOn(unittest.TestCase):
    def test_prorated_by_days_left(self):
        st = SubState(Tier.LITE, NOW + D(17))
        q = build_devices_quote(st, count=1, device_price=25, month_days=30, now=NOW)
        self.assertEqual((q.days, q.amount, q.month_total), (17, Decimal("15.00"), Decimal("25.00")))
        self.assertEqual(build_devices_quote(st, count=3, device_price=25, month_days=30, now=NOW).amount, Decimal("43.00"))

    def test_full_month_costs_exactly_the_price(self):
        st = SubState(Tier.LITE, NOW + D(30))
        self.assertEqual(build_devices_quote(st, count=1, device_price=25, month_days=30, now=NOW).amount, Decimal("25.00"))

    def test_counts_queued_lite_time(self):
        st = SubState(Tier.PREMIUM, NOW + D(10), D(20))
        self.assertEqual(build_devices_quote(st, count=1, device_price=25, month_days=30, now=NOW).days, 30)

    def test_minimum_is_one_ruble_and_inactive_is_none(self):
        st = SubState(Tier.LITE, NOW + timedelta(minutes=5))
        self.assertEqual(build_devices_quote(st, count=1, device_price=25, month_days=30, now=NOW).amount, Decimal("1.00"))
        self.assertIsNone(build_devices_quote(SubState(), count=1, device_price=25, month_days=30, now=NOW))
        self.assertIsNone(build_devices_quote(SubState(Tier.LITE, NOW - D(1)), count=1, device_price=25, month_days=30, now=NOW))
        self.assertIsNone(build_devices_quote(SubState(Tier.LITE, NOW + D(5)), count=0, device_price=25, month_days=30, now=NOW))

    def test_price_change_scales_linearly(self):
        st = SubState(Tier.LITE, NOW + D(15))
        a = build_devices_quote(st, count=1, device_price=25, month_days=30, now=NOW).amount
        b = build_devices_quote(st, count=1, device_price=50, month_days=30, now=NOW).amount
        self.assertEqual((a, b), (Decimal("13.00"), Decimal("25.00")))


if __name__ == "__main__":
    unittest.main()
