"""Проверки конфигурации при старте.

В production любая критичная проблема останавливает запуск (лучше упасть сразу, чем тихо работать
с небезопасными настройками или схемой БД). В development те же проблемы — предупреждения.
Модуль не зависит от aiogram/SQLAlchemy: ему нужен лишь объект настроек с нужными атрибутами.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

WEAK_SECRETS = frozenset({"", "change-me", "changeme", "secret", "password", "12345678", "webhook"})
DEFAULT_DB_PASSWORDS = ("ampire:ampire@", "celeste:celeste@", "postgres:postgres@")


def collect(cfg: Any) -> tuple[list[str], list[str]]:
    """Возвращает (критичные_ошибки, предупреждения) по настройкам процесса."""
    errors: list[str] = []
    warns: list[str] = []
    prod = bool(getattr(cfg, "is_production", True))

    def problem(text: str, *, fatal: bool = True) -> None:
        (errors if (prod and fatal) else warns).append(text)

    secret = str(getattr(cfg, "webhook_secret", "") or "")
    if secret.lower() in WEAK_SECRETS or len(secret) < 16:
        problem("WEBHOOK_SECRET не задан или слишком простой (нужна случайная строка от 16 символов: openssl rand -hex 24)")

    if not getattr(cfg, "owners", None):
        problem("OWNER_IDS пуст — у бота не будет ни одного владельца: некому подтверждать платежи и принимать алерты")

    if not str(getattr(cfg, "remnawave_token", "") or "").strip():
        problem("REMNAWAVE_TOKEN не задан — бот не сможет выдавать доступ")

    base = str(getattr(cfg, "public_base_url", "") or "")
    if not base.startswith("https://"):
        problem("PUBLIC_BASE_URL должен начинаться с https:// (вебхуки платежей и Telegram требуют HTTPS)")

    db_url = str(getattr(cfg, "database_url", "") or "")
    if any(p in db_url for p in DEFAULT_DB_PASSWORDS):
        problem("DATABASE_URL использует пароль по умолчанию — задайте POSTGRES_PASSWORD и DATABASE_URL")

    if getattr(cfg, "role", "all") == "api" and getattr(cfg, "telegram_mode", "polling") != "webhook":
        problem("роль api работает только в режиме TELEGRAM_MODE=webhook")

    # --- предупреждения (запуск не блокируют) ---
    if not (getattr(cfg, "yookassa_enabled", False) or getattr(cfg, "cryptobot_enabled", False)):
        warns.append("не подключён ни один платёжный провайдер — продажи будут недоступны")
    if getattr(cfg, "backup_telegram", False) and not str(getattr(cfg, "backup_password", "") or ""):
        warns.append("BACKUP_PASSWORD не задан — дамп в Telegram отправляться НЕ будет (открытый дамп содержит ссылки доступа, email и платежи)")
    if not str(getattr(cfg, "support_url", "") or ""):
        warns.append("SUPPORT_URL не задан — кнопка «Написать в поддержку» будет недоступна")
    return errors, warns


def check_plans(plans: Iterable[Any], *, allow_empty_squads: bool, prod: bool) -> tuple[list[str], list[str]]:
    """Тариф без сквадов продаёт подписку без серверов — в проде это ошибка запуска."""
    errors: list[str] = []
    warns: list[str] = []
    for p in plans:
        if not getattr(p, "is_active", True):
            continue
        if not list(getattr(p, "squads", None) or []):
            msg = f"у активного тарифа {getattr(p, 'title', p)} нет сквадов (LITE_SQUADS / PREMIUM_SQUADS): подписка выдавалась бы без серверов"
            (errors if (prod and not allow_empty_squads) else warns).append(msg)
    return errors, warns


class StartupError(RuntimeError):
    """Конфигурация или схема БД не позволяют безопасно запуститься."""
