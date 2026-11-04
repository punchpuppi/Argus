"""SQLAlchemy models for Argus persistence.

The schema supports multiple email accounts and providers from the start,
even though the first MVP will connect a single Gmail account. Email content
is deliberately not stored here; only stable identifiers and tracking events
are persisted.

Design notes:

* All timestamps use :class:`~app.database.types.UTCDateTime`, which stores
  naive UTC values and returns timezone-aware UTC datetimes. This keeps
  SQLite and PostgreSQL behaviour consistent.
* Accounts and tracking rules are deactivated/soft-deleted instead of being
  hard-deleted. Status events and notifications use restrictive foreign keys
  so that history cannot be destroyed by an accidental parent deletion.
* Provider message identity is stored as a primary ``provider_message_id``
  plus an optional ``internet_message_id`` (RFC 5322 ``Message-ID``). The
  latter is supplementary: it is indexed for fallback lookups but is not the
  sole deduplication key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum as SAEnum,
    ForeignKey,
    Index,
    JSON,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    validates,
)

from app.database.enums import (
    ArgusStatus,
    NotificationChannel,
    NotificationState,
    ProviderName,
    RuleState,
)
from app.database.types import UTCDateTime

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """Return the current UTC timestamp as a timezone-aware datetime."""

    return datetime.now(UTC)


def _enum_values(enum_cls: type[StrEnum]) -> list[str]:
    """Store enum values, not member names, in the database."""

    return [member.value for member in enum_cls]


def _enum_type(enum_cls: type[StrEnum], *, constraint_name: str) -> SAEnum:
    """Build a portable, validated string enum column type.

    ``create_constraint=True`` adds a database-level CHECK constraint so that
    invalid values are rejected even when written through raw SQL. The
    constraint name is expanded by the metadata naming convention.
    """

    return SAEnum(
        enum_cls,
        native_enum=False,
        length=32,
        values_callable=_enum_values,
        validate_strings=True,
        create_constraint=True,
        name=constraint_name,
    )


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """Created/updated timestamps shared by mutable records."""

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        default=utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )


class EmailAccount(TimestampMixin, Base):
    """A connected email account and its non-secret provider metadata."""

    __tablename__ = "email_accounts"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "normalized_email_address",
            name="uq_email_accounts_provider_email",
        ),
        Index("ix_email_accounts_is_active", "is_active"),
        Index("ix_email_accounts_normalized_email", "normalized_email_address"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[ProviderName] = mapped_column(
        _enum_type(ProviderName, constraint_name="provider_name"),
        nullable=False,
    )

    # Original address as supplied, preserved for display.
    email_address: Mapped[str] = mapped_column(String(320), nullable=False)
    # Lower-cased address used for case-insensitive uniqueness.
    normalized_email_address: Mapped[str] = mapped_column(
        String(320),
        nullable=False,
    )
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Path to an ignored credential file. Never store refresh tokens here.
    token_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON),
        default=dict,
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime,
        nullable=True,
    )

    tracking_rules: Mapped[list["TrackingRule"]] = relationship(
        back_populates="account",
        cascade="save-update, merge",
        passive_deletes="all",
    )
    sync_checkpoint: Mapped["SyncCheckpoint | None"] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    processed_emails: Mapped[list["ProcessedEmail"]] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @validates("email_address")
    def _normalize_email_address(self, _key: str, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("email_address must not be empty")
        self.normalized_email_address = cleaned.lower()
        return cleaned


class SyncCheckpoint(TimestampMixin, Base):
    """Per-account synchronization cursor and health information."""

    __tablename__ = "sync_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            name="uq_sync_checkpoints_account_id",
        ),
        CheckConstraint(
            "consecutive_failures >= 0",
            name="consecutive_failures_non_negative",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey(
            "email_accounts.id",
            ondelete="CASCADE",
            name="fk_sync_checkpoints_account_id_email_accounts",
        ),
        nullable=False,
    )

    # Persisted provider cursor: Gmail historyId or Microsoft Graph deltaLink.
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Transient pagination cursor (for example, a Graph nextLink) that is used
    # only while a single synchronization run is in progress.
    pending_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    cursor_updated_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime,
        nullable=True,
    )
    # Provider-neutral timestamp of the newest message this checkpoint covers.
    last_seen_received_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime,
        nullable=True,
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime,
        nullable=True,
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime,
        nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(
        default=0,
        nullable=False,
    )
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON),
        default=dict,
        nullable=False,
    )

    account: Mapped["EmailAccount"] = relationship(back_populates="sync_checkpoint")


class TrackingRule(TimestampMixin, Base):
    """A user-defined request to track status changes for related emails."""

    __tablename__ = "tracking_rules"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "name",
            name="uq_tracking_rules_account_name",
        ),
        Index("ix_tracking_rules_account_state", "account_id", "state"),
        Index("ix_tracking_rules_state", "state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey(
            "email_accounts.id",
            ondelete="RESTRICT",
            name="fk_tracking_rules_account_id_email_accounts",
        ),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    # Simple, deterministic matching inputs for the first MVP.
    sender_pattern: Mapped[str | None] = mapped_column(String(320), nullable=True)
    required_keywords: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
    )
    reference_number: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[RuleState] = mapped_column(
        _enum_type(RuleState, constraint_name="rule_state"),
        default=RuleState.ACTIVE,
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime,
        nullable=True,
    )

    account: Mapped["EmailAccount"] = relationship(back_populates="tracking_rules")
    status_events: Mapped[list["StatusEvent"]] = relationship(
        back_populates="rule",
        cascade="save-update, merge",
        passive_deletes="all",
    )


class ProcessedEmail(TimestampMixin, Base):
    """Deduplication ledger for emails already evaluated by the monitor."""

    __tablename__ = "processed_emails"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "provider_message_id",
            name="uq_processed_emails_account_message",
        ),
        Index(
            "ix_processed_emails_account_received",
            "account_id",
            "received_at",
        ),
        Index(
            "ix_processed_emails_account_internet_message",
            "account_id",
            "internet_message_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey(
            "email_accounts.id",
            ondelete="CASCADE",
            name="fk_processed_emails_account_id_email_accounts",
        ),
        nullable=False,
    )
    # Opaque provider identifier. Text avoids dialect-specific length limits.
    provider_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    # RFC 5322 Message-ID when the provider exposes it. Supplementary only.
    internet_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    thread_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime,
        nullable=True,
    )
    processed_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        default=utcnow,
        nullable=False,
    )

    account: Mapped["EmailAccount"] = relationship(back_populates="processed_emails")


class StatusEvent(TimestampMixin, Base):
    """An immutable detected status transition for a tracking rule."""

    __tablename__ = "status_events"
    __table_args__ = (
        UniqueConstraint(
            "rule_id",
            "provider_message_id",
            "status",
            name="uq_status_events_rule_message_status",
        ),
        Index(
            "ix_status_events_rule_occurred",
            "rule_id",
            "occurred_at",
        ),
        Index(
            "ix_status_events_rule_internet_message",
            "rule_id",
            "internet_message_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(
        ForeignKey(
            "tracking_rules.id",
            ondelete="RESTRICT",
            name="fk_status_events_rule_id_tracking_rules",
        ),
        nullable=False,
    )
    provider_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    internet_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_status: Mapped[ArgusStatus | None] = mapped_column(
        _enum_type(ArgusStatus, constraint_name="previous_status"),
        nullable=True,
    )
    status: Mapped[ArgusStatus] = mapped_column(
        _enum_type(ArgusStatus, constraint_name="status"),
        nullable=False,
    )
    matched_cue: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        default=utcnow,
        nullable=False,
    )

    rule: Mapped["TrackingRule"] = relationship(back_populates="status_events")
    notifications: Mapped[list["Notification"]] = relationship(
        back_populates="status_event",
        cascade="save-update, merge",
        passive_deletes="all",
    )


class Notification(TimestampMixin, Base):
    """Delivery record for a status event notification."""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint(
            "status_event_id",
            "channel",
            name="uq_notifications_event_channel",
        ),
        Index(
            "ix_notifications_state_created",
            "state",
            "created_at",
        ),
        CheckConstraint(
            "attempts >= 0",
            name="attempts_non_negative",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    status_event_id: Mapped[int] = mapped_column(
        ForeignKey(
            "status_events.id",
            ondelete="RESTRICT",
            name="fk_notifications_status_event_id_status_events",
        ),
        nullable=False,
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        _enum_type(
            NotificationChannel,
            constraint_name="channel",
        ),
        default=NotificationChannel.TELEGRAM,
        nullable=False,
    )
    state: Mapped[NotificationState] = mapped_column(
        _enum_type(
            NotificationState,
            constraint_name="state",
        ),
        default=NotificationState.PENDING,
        nullable=False,
    )
    # Channel-neutral external identifier (for example, a Telegram message id).
    external_message_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime,
        nullable=True,
    )

    status_event: Mapped["StatusEvent"] = relationship(back_populates="notifications")
