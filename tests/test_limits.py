import unittest

from app.core.limits import GIB, device_limit, traffic_limit_bytes


class Limits(unittest.TestCase):
    def test_included_plus_extra(self):
        self.assertEqual(device_limit(1, 0), 1)          # LITE
        self.assertEqual(device_limit(3, 0), 3)          # PREMIUM
        self.assertEqual(device_limit(1, 2), 3)          # LITE + 2 докупленных
        self.assertEqual(device_limit(3, 4), 7)

    def test_trial_is_capped_even_with_extras(self):
        self.assertEqual(device_limit(3, 0, trial_limited=True, trial_devices=1), 1)
        self.assertEqual(device_limit(3, 5, trial_limited=True, trial_devices=2), 2)

    def test_garbage_is_sanitised(self):
        self.assertEqual(device_limit(0, -5), 1)
        self.assertEqual(device_limit(2, None or 0), 2)

    def test_traffic(self):
        self.assertEqual(traffic_limit_bytes(True, 10), 10 * GIB)
        self.assertEqual(traffic_limit_bytes(True, 0), 0)        # 0 = без лимита
        self.assertEqual(traffic_limit_bytes(False, 10), 0)      # платные подписки без лимита


if __name__ == "__main__":
    unittest.main()
