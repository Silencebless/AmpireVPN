from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.utils import utcnow


def _dt(**kw):
    return mapped_column(DateTime(timezone=True), **kw)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # частичные индексы: маленькие и быстрые даже на миллионах пользователей
        Index("ix_users_active_exp", "expires_at", postgresql_where=text("tier IS NOT NULL")),
        Index("ix_users_sync_due", "sync_next_at", postgresql_where=text("sync_pending")),
        Index("ix_users_autopay_exp", "expires_at", postgresql_where=text("autopay")),
        Index("ix_users_autopay_next", "autopay_next_at", postgresql_where=text("autopay_next_at IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)  # Telegram ID
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    first_name: Mapped[str] = mapped_column(String(256), default="")
    last_name: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = _dt(default=utcnow, index=True)
    last_seen_at: Mapped[datetime] = _dt(default=utcnow)

    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    ban_reason: Mapped[str | None] = mapped_column(String(256))
    bot_blocked: Mapped[bool] = mapped_column(Boolean, default=False)

    ref_code: Mapped[str] = mapped_column(String(16), unique=True)
    referrer_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    # --- Remnawave (3.x: пользователь идентифицируется числовым id) ---
    remna_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    remna_username: Mapped[str | None] = mapped_column(String(64))
    short_uuid: Mapped[str | None] = mapped_column(String(64), index=True)
    sub_url: Mapped[str | None] = mapped_column(Text)

    # --- подписка ---
    tier: Mapped[str | None] = mapped_column(String(16))
    expires_at: Mapped[datetime | None] = _dt()
    queued_lite_s: Mapped[int] = mapped_column(BigInteger, default=0)

    # дополнительные устройства сверх тарифа (платное расширение; сбрасывается, когда подписка заканчивается)
    extra_devices: Mapped[int] = mapped_column(Integer, default=0)

    trial_used: Mapped[bool] = mapped_column(Boolean, default=False)
    trial_at: Mapped[datetime | None] = _dt()
    # пробный период с ограничениями (трафик, устройства); снимается первой оплатой или выдачей дней админом
    trial_limited: Mapped[bool] = mapped_column(Boolean, default=False)
    first_paid_at: Mapped[datetime | None] = _dt()
    connected_at: Mapped[datetime | None] = _dt()  # первое реальное использование VPN

    # --- юридические согласия (доказательство акцепта оферты и политики) ---
    consent_at: Mapped[datetime | None] = _dt()
    consent_version: Mapped[str | None] = mapped_column(String(16))
    email_consent_at: Mapped[datetime | None] = _dt()

    email: Mapped[str | None] = mapped_column(String(256))
    pending_promo_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("promos.id", ondelete="SET NULL")
    )

    # --- синхронизация с панелью ---
    sync_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    sync_error: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime | None] = _dt()
    sync_attempts: Mapped[int] = mapped_column(Integer, default=0)
    sync_next_at: Mapped[datetime | None] = _dt()

    # --- напоминания об окончании ---
    notify_expiry: Mapped[datetime | None] = _dt()
    notify_stage: Mapped[int] = mapped_column(Integer, default=0)

    # --- автопродление (ЮKassa, сохранённый способ оплаты) ---
    autopay: Mapped[bool] = mapped_column(Boolean, default=False)
    autopay_failures: Mapped[int] = mapped_column(Integer, default=0)
    autopay_last_at: Mapped[datetime | None] = _dt()
    autopay_notified_for: Mapped[datetime | None] = _dt()
    autopay_months: Mapped[int] = mapped_column(Integer, default=1)  # на какой срок продлеваем автоматически
    autopay_cycle_end: Mapped[datetime | None] = _dt()  # конец подписки на момент первой попытки текущего цикла
    autopay_next_at: Mapped[datetime | None] = _dt()  # когда пора делать следующую попытку (повторы D+1 / D+3)

    notes: Mapped[str | None] = mapped_column(Text)

    @property
    def display_name(self) -> str:
        name = (self.first_name or "").strip()
        if self.last_name:
            name = f"{name} {self.last_name}".strip()
        return name or (f"@{self.username}" if self.username else str(self.id))


class PaymentMethod(Base):
    """Сохранённый способ оплаты (для автопродления)."""

    __tablename__ = "payment_methods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(16), default="yookassa")
    method_id: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(96), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _dt(default=utcnow)


class Plan(Base):
    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)  # "lite" | "premium"
    title: Mapped[str] = mapped_column(String(32))
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    period_days: Mapped[int] = mapped_column(Integer, default=30)
    device_limit: Mapped[int] = mapped_column(Integer, default=3)
    locations: Mapped[int] = mapped_column(Integer, default=5)
    whitelist: Mapped[bool] = mapped_column(Boolean, default=False)
    features: Mapped[str] = mapped_column(Text, default="")  # дополнительные преимущества, по строке на пункт
    squads: Mapped[list] = mapped_column(JSON, default=list)  # UUID внутренних сквадов Remnawave
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        Index("ix_payments_status_created", "status", "created_at"),
        Index("ix_payments_user_status", "user_id", "status"),
        Index("ix_payments_provider_status", "provider", "status"),
        Index("ix_payments_paid_at", "paid_at", postgresql_where=text("status = 'succeeded'")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    provider: Mapped[str] = mapped_column(String(16), default="yookassa")
    external_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    plan_code: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16), default="buy")  # buy | upgrade | devices
    period_days: Mapped[int] = mapped_column(Integer, default=30)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    base_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    discount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    # pending | succeeded | canceled | refunding | refunded | needs_review (деньги пришли, нужна ручная проверка)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    promo_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("promos.id", ondelete="SET NULL"))
    confirmation_url: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    receipt_email: Mapped[str | None] = mapped_column(String(256))
    cancel_reason: Mapped[str | None] = mapped_column(String(128))
    refund_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _dt(default=utcnow)
    expires_at: Mapped[datetime | None] = _dt()
    paid_at: Mapped[datetime | None] = _dt()
    refunded_at: Mapped[datetime | None] = _dt()
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class Promo(Base):
    __tablename__ = "promos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    kind: Mapped[str] = mapped_column(String(16))  # percent | fixed | days
    value: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    days_tier: Mapped[str | None] = mapped_column(String(16))  # для kind=days: lite | premium
    tiers: Mapped[list | None] = mapped_column(JSON)  # для скидок: к каким тарифам применима (None = ко всем)
    max_uses: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[datetime | None] = _dt()
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(256))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _dt(default=utcnow)


class PromoUse(Base):
    __tablename__ = "promo_uses"
    __table_args__ = (UniqueConstraint("promo_id", "user_id", name="uq_promo_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    promo_id: Mapped[int] = mapped_column(Integer, ForeignKey("promos.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16))  # reserved | used | granted
    payment_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("payments.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _dt(default=utcnow)


class Referral(Base):
    __tablename__ = "referrals"
    __table_args__ = (
        Index("ix_referrals_status_created", "status", "created_at"),
        Index("ix_referrals_check_due", "next_check_at", postgresql_where=text("status = 'pending'")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    referred_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | rewarded | rejected
    reward_days: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _dt(default=utcnow)
    rewarded_at: Mapped[datetime | None] = _dt()
    next_check_at: Mapped[datetime | None] = _dt()  # когда в следующий раз опросить панель (backoff по возрасту)


class SubEvent(Base):
    """Журнал всех изменений подписки (аудит + статистика)."""

    __tablename__ = "sub_events"
    __table_args__ = (Index("ix_sub_events_kind_created", "kind", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # trial|buy|upgrade|promo|referral|admin_add|admin_remove|admin_set|expire|downgrade|refund
    tier: Mapped[str | None] = mapped_column(String(16))
    seconds: Mapped[int] = mapped_column(BigInteger, default=0)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _dt(default=utcnow, index=True)


class DeviceAlias(Base):
    __tablename__ = "device_aliases"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    hwid: Mapped[str] = mapped_column(String(255), primary_key=True)
    alias: Mapped[str] = mapped_column(String(64))


class Broadcast(Base):
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_by: Mapped[int] = mapped_column(BigInteger)
    src_chat_id: Mapped[int] = mapped_column(BigInteger)
    src_message_id: Mapped[int] = mapped_column(BigInteger)
    segment: Mapped[str] = mapped_column(String(24))
    button_text: Mapped[str | None] = mapped_column(String(64))
    button_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft|running|paused|done|cancelled
    total: Mapped[int] = mapped_column(Integer, default=0)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[int] = mapped_column(Integer, default=0)
    cursor_id: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = _dt(default=utcnow)
    started_at: Mapped[datetime | None] = _dt()
    finished_at: Mapped[datetime | None] = _dt()


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[object] = mapped_column(JSON)
    updated_at: Mapped[datetime] = _dt(default=utcnow)


class TextOverride(Base):
    __tablename__ = "text_overrides"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = _dt(default=utcnow)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)


class Admin(Base):
    __tablename__ = "admins"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    added_by: Mapped[int | None] = mapped_column(BigInteger)
    added_at: Mapped[datetime] = _dt(default=utcnow)
    role: Mapped[str] = mapped_column(String(16), default="admin")  # admin | support | billing | marketing | ops


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _dt(default=utcnow, index=True)
