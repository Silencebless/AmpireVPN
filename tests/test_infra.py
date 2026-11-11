import unittest
from datetime import datetime, timezone
from decimal import Decimal

from app.infra import codec
from app.infra.metrics import Metrics


class Codec(unittest.TestCase):
    def test_roundtrip_decimal_datetime_nested(self):
        now = datetime(2026, 11, 5, 12, 30, tzinfo=timezone.utc)
        src = {"rev": (Decimal("1234.50"), 3), "now": now, "split": {"lite": [2, Decimal("298")]}, "s": {"a", "b"}}
        out = codec.loads(codec.dumps(src))
        self.assertEqual(out["rev"], [Decimal("1234.50"), 3])  # кортеж -> список
        self.assertEqual(out["now"], now)
        self.assertEqual(out["split"]["lite"], [2, Decimal("298")])
        self.assertEqual(out["s"], ["a", "b"])
        self.assertIsInstance(out["rev"][0], Decimal)

    def test_plain_dicts_untouched(self):
        self.assertEqual(codec.loads(codec.dumps({"__d": "1", "x": 1})), {"__d": "1", "x": 1})

    def test_unserializable_raises(self):
        with self.assertRaises(TypeError):
            codec.dumps(object())


class MetricsRender(unittest.TestCase):
    def test_counter_gauge_summary(self):
        m = Metrics()
        m.inc("updates_total", type="message")
        m.inc("updates_total", 2, type="message")
        m.inc("updates_total", type="callback_query")
        m.set_gauge("users", 42)
        m.observe("panel_seconds", 0.5, status="200")
        m.observe("panel_seconds", 1.5, status="200")
        text = m.render()
        self.assertIn("# TYPE ampire_updates_total counter", text)
        self.assertIn('ampire_updates_total{type="message"} 3', text)
        self.assertIn('ampire_updates_total{type="callback_query"} 1', text)
        self.assertIn("ampire_users 42", text)
        self.assertIn('ampire_panel_seconds_sum{status="200"} 2', text)
        self.assertIn('ampire_panel_seconds_count{status="200"} 2', text)

    def test_histogram_buckets_are_cumulative(self):
        m = Metrics()
        for v in (0.01, 0.2, 0.2, 3.0, 500.0):
            m.observe("job_seconds", v, task="sync")
        text = m.render()
        self.assertIn("# TYPE ampire_job_seconds histogram", text)
        self.assertIn('ampire_job_seconds_bucket{task="sync",le="0.025"} 1', text)
        self.assertIn('ampire_job_seconds_bucket{task="sync",le="0.25"} 3', text)
        self.assertIn('ampire_job_seconds_bucket{task="sync",le="5"} 4', text)
        self.assertIn('ampire_job_seconds_bucket{task="sync",le="+Inf"} 5', text)
        self.assertIn('ampire_job_seconds_count{task="sync"} 5', text)

    def test_label_escaping(self):
        m = Metrics()
        m.inc("x", why='a"b\\c\nd')
        self.assertIn(r'why="a\"b\\c\nd"', m.render())


if __name__ == "__main__":
    unittest.main()
