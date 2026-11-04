"""Foundation baseline schema.

Revision ID: 0001_foundation_baseline
Revises:
Create Date: 2026-11-03

This baseline represents the corrected foundation schema:

* UTC-normalised timestamps (stored as naive UTC by the application).
* Soft-delete columns on accounts and tracking rules.
* Case-insensitive account uniqueness through ``normalized_email_address``.
* Restrictive foreign keys for tracking and notification history.
* Provider-neutral, unbounded message identifiers and sync cursors.
* Supplementary RFC 5322 ``internet_message_id`` columns.
* Database-level CHECK constraints for enum columns and counters.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0001_foundation_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("email_address", sa.String(length=320), nullable=False),
        sa.Column("normalized_email_address", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("token_path", sa.String(length=1024), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("provider_metadata", sa.JSON(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_email_accounts"),
        sa.UniqueConstraint(
            "provider",
            "normalized_email_address",
            name="uq_email_accounts_provider_email",
        ),
        sa.CheckConstraint(
            "provider IN ('gmail', 'outlook')",
            name="provider_name",
        ),
    )
    op.create_index(
        "ix_email_accounts_is_active",
        "email_accounts",
        ["is_active"],
    )
    op.create_index(
        "ix_email_accounts_normalized_email",
        "email_accounts",
        ["normalized_email_address"],
    )

    op.create_table(
        "sync_checkpoints",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("pending_cursor", sa.Text(), nullable=True),
        sa.Column("cursor_updated_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_received_at", sa.DateTime(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("provider_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_sync_checkpoints"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["email_accounts.id"],
            name="fk_sync_checkpoints_account_id_email_accounts",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "account_id",
            name="uq_sync_checkpoints_account_id",
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name="consecutive_failures_non_negative",
        ),
    )

    op.create_table(
        "tracking_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("sender_pattern", sa.String(length=320), nullable=True),
        sa.Column("required_keywords", sa.JSON(), nullable=False),
        sa.Column("reference_number", sa.String(length=120), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_tracking_rules"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["email_accounts.id"],
            name="fk_tracking_rules_account_id_email_accounts",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "account_id",
            "name",
            name="uq_tracking_rules_account_name",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'paused', 'stopped')",
            name="rule_state",
        ),
    )
    op.create_index(
        "ix_tracking_rules_account_state",
        "tracking_rules",
        ["account_id", "state"],
    )
    op.create_index(
        "ix_tracking_rules_state",
        "tracking_rules",
        ["state"],
    )

    op.create_table(
        "processed_emails",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("provider_message_id", sa.Text(), nullable=False),
        sa.Column("internet_message_id", sa.Text(), nullable=True),
        sa.Column("thread_id", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=True),
        sa.Column("processed_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_processed_emails"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["email_accounts.id"],
            name="fk_processed_emails_account_id_email_accounts",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "account_id",
            "provider_message_id",
            name="uq_processed_emails_account_message",
        ),
    )
    op.create_index(
        "ix_processed_emails_account_received",
        "processed_emails",
        ["account_id", "received_at"],
    )
    op.create_index(
        "ix_processed_emails_account_internet_message",
        "processed_emails",
        ["account_id", "internet_message_id"],
    )

    op.create_table(
        "status_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("provider_message_id", sa.Text(), nullable=False),
        sa.Column("internet_message_id", sa.Text(), nullable=True),
        sa.Column("previous_status", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("matched_cue", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_status_events"),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["tracking_rules.id"],
            name="fk_status_events_rule_id_tracking_rules",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "rule_id",
            "provider_message_id",
            "status",
            name="uq_status_events_rule_message_status",
        ),
        sa.CheckConstraint(
            "previous_status IN ('unknown', 'order_placed', 'shipped', "
            "'out_for_delivery', 'delivered', 'delayed', 'cancelled')",
            name="previous_status",
        ),
        sa.CheckConstraint(
            "status IN ('unknown', 'order_placed', 'shipped', "
            "'out_for_delivery', 'delivered', 'delayed', 'cancelled')",
            name="status",
        ),
    )
    op.create_index(
        "ix_status_events_rule_occurred",
        "status_events",
        ["rule_id", "occurred_at"],
    )
    op.create_index(
        "ix_status_events_rule_internet_message",
        "status_events",
        ["rule_id", "internet_message_id"],
    )

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("status_event_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("external_message_id", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
        sa.ForeignKeyConstraint(
            ["status_event_id"],
            ["status_events.id"],
            name="fk_notifications_status_event_id_status_events",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "status_event_id",
            "channel",
            name="uq_notifications_event_channel",
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="attempts_non_negative",
        ),
        sa.CheckConstraint(
            "channel IN ('telegram')",
            name="channel",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'sent', 'failed')",
            name="state",
        ),
    )
    op.create_index(
        "ix_notifications_state_created",
        "notifications",
        ["state", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_state_created", table_name="notifications")
    op.drop_table("notifications")

    op.drop_index(
        "ix_status_events_rule_internet_message",
        table_name="status_events",
    )
    op.drop_index("ix_status_events_rule_occurred", table_name="status_events")
    op.drop_table("status_events")

    op.drop_index(
        "ix_processed_emails_account_internet_message",
        table_name="processed_emails",
    )
    op.drop_index(
        "ix_processed_emails_account_received",
        table_name="processed_emails",
    )
    op.drop_table("processed_emails")

    op.drop_index("ix_tracking_rules_state", table_name="tracking_rules")
    op.drop_index(
        "ix_tracking_rules_account_state",
        table_name="tracking_rules",
    )
    op.drop_table("tracking_rules")

    op.drop_table("sync_checkpoints")

    op.drop_index(
        "ix_email_accounts_normalized_email",
        table_name="email_accounts",
    )
    op.drop_index("ix_email_accounts_is_active", table_name="email_accounts")
    op.drop_table("email_accounts")
