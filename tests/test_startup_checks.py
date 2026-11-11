import unittest
from types import SimpleNamespace as NS

from app.legal import CONSENT_DOCS, DEFAULT_URLS, DOCS, doc_url, is_http_url
from app.startup_checks import check_plans, collect


def good(**kw):
    base = dict(
        is_production=True, webhook_secret="x" * 32, owners={1}, remnawave_token="tok", public_base_url="https://vpn.example.com",
        database_url="postgresql+asyncpg://u:Str0ngPass@db/app", role="all", telegram_mode="webhook", yookassa_enabled=True,
        cryptobot_enabled=False, backup_telegram=True, backup_password="pw", support_url="https://t.me/x",
    )
    base.update(kw)
    return NS(**base)


class Startup(unittest.TestCase):
    def test_good_config_is_clean(self):
        self.assertEqual(collect(good()), ([], []))

    def test_each_critical_problem_blocks_production(self):
        cases = {
            "change-me secret": dict(webhook_secret="change-me"),
            "short secret": dict(webhook_secret="abc"),
            "no owners": dict(owners=set()),
            "no panel token": dict(remnawave_token=""),
            "http base url": dict(public_base_url="http://localhost:8080"),
            "default db password": dict(database_url="postgresql+asyncpg://ampire:ampire@postgres:5432/ampire"),
        }
        for name, patch in cases.items():
            errors, _ = collect(good(**patch))
            self.assertTrue(errors, name)

    def test_development_only_warns(self):
        errors, warns = collect(good(is_production=False, webhook_secret="change-me", owners=set()))
        self.assertEqual(errors, [])
        self.assertGreaterEqual(len(warns), 2)

    def test_warnings_do_not_block(self):
        errors, warns = collect(good(yookassa_enabled=False, backup_password="", support_url=""))
        self.assertEqual(errors, [])
        self.assertEqual(len(warns), 3)

    def test_plan_without_squads(self):
        plans = [NS(title="LITE", squads=["a"], is_active=True), NS(title="PREMIUM", squads=[], is_active=True)]
        errors, _ = check_plans(plans, allow_empty_squads=False, prod=True)
        self.assertEqual(len(errors), 1)
        self.assertEqual(check_plans(plans, allow_empty_squads=True, prod=True)[0], [])
        self.assertEqual(check_plans(plans, allow_empty_squads=False, prod=False)[0], [])
        off = [NS(title="X", squads=[], is_active=False)]
        self.assertEqual(check_plans(off, allow_empty_squads=False, prod=True), ([], []))


class Legal(unittest.TestCase):
    def test_registry(self):
        self.assertEqual(CONSENT_DOCS, ("offer", "privacy", "terms", "pd"))
        for key in CONSENT_DOCS:
            self.assertIn(key, DOCS)
        # каждая заглушка — валидная ссылка для кнопки Telegram
        for url in DEFAULT_URLS.values():
            self.assertTrue(is_http_url(url), url)
        self.assertEqual(len(DEFAULT_URLS), len(DOCS) + 1)   # документы + FAQ

    def test_doc_url_prefers_setting_and_falls_back(self):
        self.assertEqual(doc_url(lambda k: "https://example.com/offer", "offer"), "https://example.com/offer")
        self.assertEqual(doc_url(lambda k: "", "offer"), DOCS["offer"].placeholder)

    def test_url_validation(self):
        for bad in ("t.me/x", "javascript:alert(1)", "https://a b.com", "https://", ""):
            self.assertFalse(is_http_url(bad), bad)

    def test_short_labels_fit_two_column_buttons(self):
        for key in CONSENT_DOCS:
            self.assertLessEqual(len(DOCS[key].short), 20)


if __name__ == "__main__":
    unittest.main()
