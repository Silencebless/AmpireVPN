"""Единый стиль AmpireVPN: бренд, обращение на «вы», подписи кнопок без эмодзи, ссылки-заглушки."""
import re
import unittest
from pathlib import Path

from app.bot import labels
from app.legal import CONSENT_DOCS, DEFAULT_URLS, DOCS
from app.texts import TEXTS

ROOT = Path(__file__).resolve().parent.parent
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2B06\u2B07\uFE0F]")
INFORMAL = re.compile(r"\b(ты|тебе|тебя|тобой|твой|твоя|твоё|твои|твоих|твоему|твою|твоей|нажми|выбери|открой|отправь|подпишись|попробуй|проверь)\b", re.I)

# где упоминание прежнего названия допустимо (совместимость с уже созданными данными/объяснения для миграции)
LEGACY_OK = {
    "app/services/subscriptions.py",   # поиск пользователя в панели под прежним именем celeste_<id>
    "app/startup_checks.py",           # пароль БД по умолчанию прежней версии считается небезопасным
    ".env.example",                    # подсказка про том БД от прежней версии
    "docker-compose.yml",              # то же
    "docs/SETUP.md", "docs/OPERATIONS.md", "docs/BACKUP.md", "docs/SCALING.md", "CHANGELOG.md", "README.md",
    "tests/test_brand_and_style.py",
}


class Brand(unittest.TestCase):
    def test_no_old_brand_in_project(self):
        offenders = []
        for p in ROOT.rglob("*"):
            if not p.is_file() or p.suffix in {".pyc", ".zip", ".png"} or any(x in p.parts for x in (".git", "__pycache__")):
                continue
            rel = p.relative_to(ROOT).as_posix()
            if rel in LEGACY_OK:
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if re.search(r"celeste", text, re.I):
                offenders.append(rel)
        self.assertEqual(offenders, [], "осталось прежнее название: " + ", ".join(offenders))

    def test_texts_use_new_brand(self):
        joined = " ".join(t.default for t in TEXTS.values())
        self.assertIn("AmpireVPN", joined)


class Style(unittest.TestCase):
    def test_polite_form_everywhere(self):
        bad = {k: m.group(0) for k, t in TEXTS.items() if (m := INFORMAL.search(t.default))}
        self.assertEqual(bad, {}, "обращение на «ты» в текстах")

    def test_user_button_labels_have_no_emoji_and_are_short(self):
        for name in dir(labels):
            if name.isupper():
                value = getattr(labels, name)
                self.assertFalse(EMOJI.search(value), f"эмодзи в подписи {name}")
                self.assertLessEqual(len(value), 40, name)

    def test_labels_are_unique_where_it_matters(self):
        main = [labels.SUBSCRIPTION, labels.REFERRAL, labels.SUPPORT]
        self.assertEqual(len(set(main)), 3)

    def test_texts_are_not_emoji_heavy(self):
        for key, t in TEXTS.items():
            self.assertLessEqual(len(EMOJI.findall(t.default)), 1, f"слишком много эмодзи в «{key}»")

    def test_welcome_mentions_agreement_and_buttons_documents(self):
        for key in ("welcome_new", "welcome_no_trial"):
            self.assertIn("Подключить", TEXTS[key].default)
            self.assertIn("согласны", TEXTS[key].default)
        self.assertEqual(len(CONSENT_DOCS), 4)          # 2 × 2 кнопок-ссылок + «Подключить»
        self.assertEqual(len(CONSENT_DOCS) % 2, 0)


class Placeholders(unittest.TestCase):
    def test_every_document_has_placeholder_link_and_faq(self):
        for doc in DOCS.values():
            self.assertTrue(DEFAULT_URLS[doc.setting].startswith("https://telegra.ph/"), doc.key)
        self.assertTrue(DEFAULT_URLS["faq_url"].startswith("https://telegra.ph/"))

    def test_required_documents_exist(self):
        # оферта (требуется ЮKassa), политика ПДн, согласие на ПДн, условия автопродления и возврата
        self.assertTrue({"offer", "privacy", "pd", "autopay", "refund"} <= set(DOCS))


if __name__ == "__main__":
    unittest.main()
