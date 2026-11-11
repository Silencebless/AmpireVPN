"""Начальная схема AmpireVPN.

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "promos",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("value", sa.Numeric(10, 2), nullable=False),
        sa.Column("days_tier", sa.String(16), nullable=True),
        sa.Column("tiers", sa.JSON(), nullable=True),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("note", sa.String(256), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("code"),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=False, nullable=False),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("first_name", sa.String(256), nullable=False),
        sa.Column("last_name", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_banned", sa.Boolean(), nullable=False),
        sa.Column("ban_reason", sa.String(256), nullable=True),
        sa.Column("bot_blocked", sa.Boolean(), nullable=False),
        sa.Column("ref_code", sa.String(16), nullable=False),
        sa.Column("referrer_id", sa.BigInteger(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column("remna_id", sa.Integer(), nullable=True),
        sa.Column("remna_username", sa.String(64), nullable=True),
        sa.Column("short_uuid", sa.String(64), nullable=True),
        sa.Column("sub_url", sa.Text(), nullable=True),
        sa.Column("tier", sa.String(16), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("queued_lite_s", sa.BigInteger(), nullable=False),
        sa.Column("trial_used", sa.Boolean(), nullable=False),
        sa.Column("trial_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email", sa.String(256), nullable=True),
        sa.Column("pending_promo_id", sa.Integer(), sa.ForeignKey('promos.id', ondelete='SET NULL'), nullable=True),
        sa.Column("sync_pending", sa.Boolean(), nullable=False),
        sa.Column("sync_error", sa.Text(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_attempts", sa.Integer(), nullable=False),
        sa.Column("sync_next_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notify_expiry", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notify_stage", sa.Integer(), nullable=False),
        sa.Column("autopay", sa.Boolean(), nullable=False),
        sa.Column("autopay_failures", sa.Integer(), nullable=False),
        sa.Column("autopay_last_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("autopay_notified_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.UniqueConstraint("ref_code"),
        sa.UniqueConstraint("remna_id"),
    )
    op.create_index(op.f("ix_users_username"), "users", ["username"])
    op.create_index(op.f("ix_users_created_at"), "users", ["created_at"])
    op.create_index(op.f("ix_users_referrer_id"), "users", ["referrer_id"])
    op.create_index(op.f("ix_users_short_uuid"), "users", ["short_uuid"])
    op.create_index('ix_users_active_exp', "users", ['expires_at'], postgresql_where=sa.text('tier IS NOT NULL'))
    op.create_index('ix_users_sync_due', "users", ['sync_next_at'], postgresql_where=sa.text('sync_pending'))
    op.create_index('ix_users_autopay_exp', "users", ['expires_at'], postgresql_where=sa.text('autopay'))

    op.create_table(
        "plans",
        sa.Column("code", sa.String(16), primary_key=True, nullable=False),
        sa.Column("title", sa.String(32), nullable=False),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
        sa.Column("period_days", sa.Integer(), nullable=False),
        sa.Column("device_limit", sa.Integer(), nullable=False),
        sa.Column("locations", sa.Integer(), nullable=False),
        sa.Column("whitelist", sa.Boolean(), nullable=False),
        sa.Column("features", sa.Text(), nullable=False),
        sa.Column("squads", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("sort", sa.Integer(), nullable=False),
    )

    op.create_table(
        "payment_methods",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("method_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(96), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("method_id"),
    )
    op.create_index(op.f("ix_payment_methods_user_id"), "payment_methods", ["user_id"])

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=True),
        sa.Column("plan_code", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("period_days", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("base_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("discount", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.Column("promo_id", sa.Integer(), sa.ForeignKey('promos.id', ondelete='SET NULL'), nullable=True),
        sa.Column("confirmation_url", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("receipt_email", sa.String(256), nullable=True),
        sa.Column("cancel_reason", sa.String(128), nullable=True),
        sa.Column("refund_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.UniqueConstraint("external_id"),
    )
    op.create_index('ix_payments_status_created', "payments", ['status', 'created_at'])
    op.create_index('ix_payments_user_status', "payments", ['user_id', 'status'])
    op.create_index('ix_payments_provider_status', "payments", ['provider', 'status'])
    op.create_index('ix_payments_paid_at', "payments", ['paid_at'], postgresql_where=sa.text("status = 'succeeded'"))

    op.create_table(
        "promo_uses",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("promo_id", sa.Integer(), sa.ForeignKey('promos.id', ondelete='CASCADE'), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payment_id", sa.Integer(), sa.ForeignKey('payments.id', ondelete='SET NULL'), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('promo_id', 'user_id', name='uq_promo_user'),
    )
    op.create_index(op.f("ix_promo_uses_promo_id"), "promo_uses", ["promo_id"])
    op.create_index(op.f("ix_promo_uses_user_id"), "promo_uses", ["user_id"])

    op.create_table(
        "referrals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("referrer_id", sa.BigInteger(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column("referred_id", sa.BigInteger(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reward_days", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rewarded_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("referred_id"),
    )
    op.create_index(op.f("ix_referrals_referrer_id"), "referrals", ["referrer_id"])
    op.create_index('ix_referrals_status_created', "referrals", ['status', 'created_at'])

    op.create_table(
        "sub_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("tier", sa.String(16), nullable=True),
        sa.Column("seconds", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=True),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(op.f("ix_sub_events_user_id"), "sub_events", ["user_id"])
    op.create_index(op.f("ix_sub_events_created_at"), "sub_events", ["created_at"])
    op.create_index('ix_sub_events_kind_created', "sub_events", ['kind', 'created_at'])

    op.create_table(
        "device_aliases",
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True, nullable=False),
        sa.Column("hwid", sa.String(255), primary_key=True, nullable=False),
        sa.Column("alias", sa.String(64), nullable=False),
    )

    op.create_table(
        "broadcasts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("src_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("src_message_id", sa.BigInteger(), nullable=False),
        sa.Column("segment", sa.String(24), nullable=False),
        sa.Column("button_text", sa.String(64), nullable=True),
        sa.Column("button_url", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("sent", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("blocked", sa.Integer(), nullable=False),
        sa.Column("cursor_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "settings",
        sa.Column("key", sa.String(64), primary_key=True, nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "text_overrides",
        sa.Column("key", sa.String(64), primary_key=True, nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
    )

    op.create_table(
        "admins",
        sa.Column("user_id", sa.BigInteger(), primary_key=True, autoincrement=False, nullable=False),
        sa.Column("added_by", sa.BigInteger(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target", sa.String(64), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(op.f("ix_audit_log_actor_id"), "audit_log", ["actor_id"])
    op.create_index(op.f("ix_audit_log_created_at"), "audit_log", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("admins")
    op.drop_table("text_overrides")
    op.drop_table("settings")
    op.drop_table("broadcasts")
    op.drop_table("device_aliases")
    op.drop_table("sub_events")
    op.drop_table("referrals")
    op.drop_table("promo_uses")
    op.drop_table("payments")
    op.drop_table("payment_methods")
    op.drop_table("plans")
    op.drop_table("users")
    op.drop_table("promos")
