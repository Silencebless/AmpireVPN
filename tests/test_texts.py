import string
import unittest

from app.texts import TEXTS
from app.utils import days_word, human_bytes, money, money_btn, parse_decimal, parse_int, plural, sparkline


class TextRegistry(unittest.TestCase):
    def test_placeholders_match_declared_vars(self):
        fmt = string.Formatter()
        for key, td in TEXTS.items():
            used = {name for _, name, _, _ in fmt.parse(td.default) if name}
            self.assertLessEqual(used, set(td.vars), f"{key}: использует необъявленные {used - set(td.vars)}")

    def test_format_with_sample_values(self):
        for key, td in TEXTS.items():
            sample = {v: "X" for v in td.vars}
            out = td.default.format_map(sample)
            self.assertTrue(out.strip(), key)
            self.assertLess(len(out), 3500, key)

    def test_html_tags_balanced(self):
        for key, td in TEXTS.items():
            for tag in ("b", "i", "code", "a", "blockquote"):
                self.assertEqual(td.default.count(f"<{tag}>") + td.default.count(f"<{tag} "),
                                 td.default.count(f"</{tag}>"), f"{key}: <{tag}>")


class Utils(unittest.TestCase):
    def test_plural(self):
        self.assertEqual([plural(n, ("день", "дня", "дней")) for n in (1, 2, 5, 11, 21, 22, 25, 111)],
                         ["день", "дня", "дней", "дней", "день", "дня", "дней", "дней"])
        self.assertEqual(days_word(7), "7 дней")

    def test_money(self):
        self.assertEqual(money(149), "149 ₽")
        self.assertEqual(money("1249.50"), "1 249,50 ₽")
        self.assertEqual(money_btn(249), "249₽")

    def test_parsers(self):
        self.assertEqual(parse_int(" 30 "), 30)
        self.assertIsNone(parse_int("3.5"))
        self.assertIsNone(parse_int("-1", lo=0))
        self.assertEqual(str(parse_decimal("149,5")), "149.5")
        self.assertIsNone(parse_decimal("abc"))

    def test_misc(self):
        self.assertEqual(human_bytes(1536), "1.5 КБ")
        self.assertEqual(sparkline([0, 1, 2, 4]), "▁▂▄█")


if __name__ == "__main__":
    unittest.main()
