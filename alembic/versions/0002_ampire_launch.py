"""AmpireVPN: доп. устройства, согласия, повторы автопродления, роли админов, backoff рефералов.

Revision ID: 0002
Revises: 0001

Миграция обратно-совместима со старым кодом (только новые колонки с значениями по умолчанию),
поэтому её безопасно применять при поочерёдном обновлении реплик.
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- users ---
    op.add_column("users", sa.Column("extra_devices", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("trial_limited", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("first_paid_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("consent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("consent_version", sa.String(16), nullable=True))
    op.add_column("users", sa.Column("email_consent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("autopay_months", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("users", sa.Column("autopay_cycle_end", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("autopay_next_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_users_autopay_next', "users", ['autopay_next_at'], postgresql_where=sa.text('autopay_next_at IS NOT NULL'))

    # --- referrals: опрос панели по расписанию вместо «всех каждые 30 минут» ---
    op.add_column("referrals", sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_referrals_check_due', "referrals", ['next_check_at'], postgresql_where=sa.text("status = 'pending'"))

    # --- admins: раздельные роли (существующие админы остаются «admin») ---
    op.add_column("admins", sa.Column("role", sa.String(16), nullable=False, server_default="admin"))

    # --- данные ---
    # Новая модель устройств: LITE — 1, PREMIUM — 3. Меняем только нетронутые старые значения по умолчанию (3 и 5),
    # чтобы не затереть лимиты, которые владелец настроил вручную.
    op.execute("UPDATE plans SET device_limit = 1 WHERE code = 'lite' AND device_limit = 3")
    op.execute("UPDATE plans SET device_limit = 3 WHERE code = 'premium' AND device_limit = 5")
    # Дата первой оплаты — по уже совершённым платежам.
    op.execute(
        "UPDATE users u SET first_paid_at = p.first_at FROM ("
        "SELECT user_id, MIN(paid_at) AS first_at FROM payments "
        "WHERE status IN ('succeeded', 'refunded') AND applied AND user_id IS NOT NULL GROUP BY user_id"
        ") p WHERE p.user_id = u.id AND u.first_paid_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("admins", "role")
    op.drop_index('ix_referrals_check_due', table_name="referrals")
    op.drop_column("referrals", "next_check_at")
    op.drop_index('ix_users_autopay_next', table_name="users")
    for col in (
        "autopay_next_at",
        "autopay_cycle_end",
        "autopay_months",
        "email_consent_at",
        "consent_version",
        "consent_at",
        "first_paid_at",
        "trial_limited",
        "extra_devices",
    ):
        op.drop_column("users", col)
