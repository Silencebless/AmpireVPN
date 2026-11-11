"""Юридические документы: реестр, ссылки-заглушки и версия согласия.

Сами документы здесь не хранятся — только ссылки. По умолчанию стоят заглушки (telegra.ph); боевые ссылки
задаются переменными окружения (LEGAL_*_URL, FAQ_URL) или в админке: Настройки → Документы.
Тексты документов должен согласовать юрист (оферта, политика ПДн, условия автопродления и возврата).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# Меняйте версию, когда документы обновляются: по ней видно, с какой редакцией согласился пользователь.
CONSENT_VERSION = "2026-11"


@dataclass(frozen=True, slots=True)
class Doc:
    key: str
    short: str      # подпись на кнопке (влезает в половину строки)
    title: str      # полное название
    slug: str       # хвост адреса-заглушки

    @property
    def setting(self) -> str:
        return f"legal_{self.key}_url"

    @property
    def placeholder(self) -> str:
        return f"https://telegra.ph/AmpireVPN-{self.slug}"


DOCS: dict[str, Doc] = {
    d.key: d
    for d in (
        Doc("offer", "Оферта", "Публичная оферта", "Offer"),
        Doc("privacy", "Конфиденциальность", "Политика конфиденциальности", "Privacy"),
        Doc("terms", "Соглашение", "Пользовательское соглашение", "Terms"),
        Doc("pd", "Персональные данные", "Согласие на обработку персональных данных", "PersonalData"),
        Doc("autopay", "Автопродление", "Условия автопродления", "Autopay"),
        Doc("refund", "Возврат", "Условия возврата", "Refund"),
    )
}

# документы, с которыми соглашается пользователь, нажимая «Подключить» (порядок = порядок кнопок 2 × 2)
CONSENT_DOCS: tuple[str, ...] = ("offer", "privacy", "terms", "pd")
FAQ_SETTING = "faq_url"
FAQ_PLACEHOLDER = "https://telegra.ph/AmpireVPN-FAQ"

# значения по умолчанию для runtime-настроек: ключ настройки → ссылка-заглушка
DEFAULT_URLS: dict[str, str] = {d.setting: d.placeholder for d in DOCS.values()} | {FAQ_SETTING: FAQ_PLACEHOLDER}


def doc_url(get: Callable[[str], object], key: str) -> str:
    """Ссылка на документ из настроек (get — ctx.cfg.get); при пустом значении — заглушка."""
    doc = DOCS[key]
    value = str(get(doc.setting) or "").strip()
    return value or doc.placeholder


def is_http_url(value: str) -> bool:
    v = value.strip()
    return v.startswith(("https://", "http://")) and " " not in v and len(v) <= 512 and "." in v
