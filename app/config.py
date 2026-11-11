"""Настройки приложения из переменных окружения / .env"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def _split(value: str) -> list[str]:
    return [x.strip() for x in value.replace(";", ",").split(",") if x.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )

    # --- Telegram ---
    bot_token: str
    owner_ids: str = ""            # ID владельцев через запятую (полный доступ)
    support_url: str = ""          # https://t.me/your_support (чат с поддержкой)
    # Ссылки на юридические документы и FAQ (можно поменять и позже в админке: Настройки → Документы)
    faq_url: str = ""
    legal_offer_url: str = ""
    legal_privacy_url: str = ""
    legal_terms_url: str = ""
    legal_pd_url: str = ""
    legal_autopay_url: str = ""
    legal_refund_url: str = ""

    # --- Режим окружения ---
    # production — строгие проверки при старте (бот не запустится с небезопасной конфигурацией или схемой БД);
    # development — те же проверки превращаются в предупреждения, схема при сбое миграций создаётся через create_all
    app_env: str = "production"

    # --- Публичный адрес (редиректы /dl и /add, вебхуки) ---
    public_base_url: str = "http://localhost:8080"
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    webhook_secret: str = "change-me"   # секретная часть URL вебхуков

    # --- Инфраструктура ---
    database_url: str = "postgresql+asyncpg://ampire:ampire@postgres:5432/ampire"
    redis_url: str = "redis://redis:6379/0"
    timezone: str = "Europe/Moscow"
    log_level: str = "INFO"

    # --- Remnawave ---
    remnawave_url: str = "http://remnawave:3000"
    remnawave_token: str = ""
    remnawave_proxy_headers: bool = True      # слать X-Forwarded-For/Proto (нужно при доступе без reverse-proxy)
    remnawave_caddy_key: str = ""             # X-Api-Key, если панель за Caddy Security
    remnawave_cookie: str = ""                # name=value, если панель за cookie-защитой
    remnawave_webhook_secret: str = ""        # = WEBHOOK_SECRET_HEADER панели

    # --- ЮKassa ---
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    yookassa_receipts: bool = True            # передавать данные чека (54-ФЗ / самозанятые)
    yookassa_vat_code: int = 1                # 1 = без НДС
    yookassa_tax_system_code: int | None = None
    yookassa_check_ip: bool = False           # проверять IP отправителя уведомлений

    # --- Режим работы и масштабирование ---
    app_role: str = "all"                     # all | api | worker (см. docs/SCALING.md)
    telegram_mode: str = "polling"            # polling | webhook (webhook обязателен для нескольких реплик api)
    db_pool_size: int = 10                    # соединений с БД на процесс
    db_max_overflow: int = 10
    db_pool_timeout: float = 5.0              # сколько ждать свободное соединение (сек) — дальше ошибка, а не зависание
    db_statement_timeout_ms: int = 30000      # серверный лимит одного запроса (0 — без лимита)
    db_idle_tx_timeout_ms: int = 60000        # серверный лимит «простоя» внутри транзакции (0 — без лимита)
    migration_database_url: str = ""          # прямой адрес БД для миграций (если основной идёт через PgBouncer)
    db_pgbouncer: bool = False                # включи, если между ботом и PostgreSQL стоит PgBouncer (transaction mode)
    # Лимиты Telegram (~30 сообщений/с на бота) делим между независимыми «корзинами», чтобы рассылка не душила
    # уведомления об оплате и окончании подписки: сумма двух значений ниже должна быть не больше ~28.
    tg_broadcast_rate: int = 18               # рассылки, сообщений в секунду
    tg_bg_rate: int = 8                       # служебные фоновые сообщения (напоминания, «подписка закончилась»)
    broadcast_concurrency: int = 16           # параллельных отправителей рассылки на процесс
    panel_concurrency: int = 20               # одновременных запросов к панели Remnawave на процесс
    panel_timeout: float = 15.0
    web_rate_per_min: int = 90                # лимит запросов к публичным страницам с одного IP
    trusted_proxy: bool = True                # доверять X-Forwarded-For от реверс-прокси
    metrics_token: str = ""                   # токен для /metrics (только заголовок Authorization: Bearer; пусто — выключен)
    log_json: bool = False                    # логи в JSON (для Loki/ELK)
    sentry_dsn: str = ""

    # --- CryptoBot (Crypto Pay) ---
    cryptobot_token: str = ""
    cryptobot_testnet: bool = False
    cryptobot_assets: str = ""                # какие монеты принимать, через запятую; пусто = все

    # --- Начальные значения тарифов (используются только при первом запуске) ---
    lite_squads: str = ""                     # UUID внутренних сквадов через запятую
    premium_squads: str = ""
    # Разрешить запуск с тарифами без сквадов (только для первичной настройки: такой тариф не продаётся)
    allow_empty_squads: bool = False
    remna_username_prefix: str = "ampire"     # имена пользователей в панели: ampire_<telegram_id>

    # --- Бэкапы ---
    # Основа — непрерывный архив WAL (PITR) + копия вне сервера (docker-compose.backup.yml, docs/BACKUP.md).
    # Логический дамп в Telegram — дополнительная копия «под рукой»: отправляется ТОЛЬКО зашифрованным.
    backup_hour: int = 4                      # час (по timezone), когда слать бэкап владельцам
    backup_chat_id: int | None = None         # куда слать бэкап (по умолчанию — владельцам)
    backup_password: str = ""                 # шифрует дамп (расшифровка: scripts/decrypt_backup.py); без него дамп НЕ отправляется
    backup_telegram: bool = True              # слать зашифрованный дамп в Telegram (выключите, если есть PITR)
    backup_max_age_h: int = 36                # алерт, если свежей копии (PITR/дамп) нет дольше этого срока
    yookassa_recon_hour: int = 5              # час ежедневной сверки «ЮKassa → БД»

    @property
    def owners(self) -> set[int]:
        out: set[int] = set()
        for x in _split(self.owner_ids):
            try:
                out.add(int(x))
            except ValueError:
                continue
        return out

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() not in ("development", "dev", "local", "test")

    @property
    def base_url(self) -> str:
        return self.public_base_url.rstrip("/")

    @property
    def lite_squad_list(self) -> list[str]:
        return _split(self.lite_squads)

    @property
    def premium_squad_list(self) -> list[str]:
        return _split(self.premium_squads)

    @property
    def yookassa_enabled(self) -> bool:
        return bool(self.yookassa_shop_id and self.yookassa_secret_key)

    @property
    def cryptobot_enabled(self) -> bool:
        return bool(self.cryptobot_token)

    @property
    def crypto_asset_list(self) -> list[str]:
        return [x.upper() for x in _split(self.cryptobot_assets)]

    @property
    def role(self) -> str:
        r = self.app_role.strip().lower()
        return r if r in ("all", "api", "worker") else "all"

    @property
    def webhook_mode(self) -> bool:
        return self.telegram_mode.strip().lower() == "webhook"

    @property
    def telegram_secret_token(self) -> str:
        """Секрет заголовка X-Telegram-Bot-Api-Secret-Token (только A-Za-z0-9_-)."""
        import hashlib

        return hashlib.sha256(f"tg:{self.bot_token}:{self.webhook_secret}".encode()).hexdigest()[:48]

    @property
    def telegram_webhook_path(self) -> str:
        return f"/tg/{self.webhook_secret}"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
