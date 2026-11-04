"""Tests for persistence relationships, constraints, and retention behaviour."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.enums import (
    ArgusStatus,
    NotificationChannel,
    NotificationState,
    ProviderName,
    RuleState,
)
from app.database.models import (
    EmailAccount,
    Notification,
    ProcessedEmail,
    StatusEvent,
    SyncCheckpoint,
    TrackingRule,
    utcnow,
)


def now() -> datetime:
    return datetime.now(UTC)


def make_account(
    session: Session,
    email: str = "user@example.com",
    provider: ProviderName = ProviderName.GMAIL,
) -> EmailAccount:
    account = EmailAccount(
        provider=provider,
        email_address=email,
        token_path="credentials/gmail_token.json",
    )
    session.add(account)
    session.flush()
    return account


def make_rule(session: Session, account: EmailAccount, name: str = "Rule") -> TrackingRule:
    rule = TrackingRule(
        account_id=account.id,
        name=name,
        sender_pattern="example.com",
        required_keywords=["order"],
    )
    session.add(rule)
    session.flush()
    return rule


def make_event(
    session: Session,
    rule: TrackingRule,
    message_id: str = "message-1",
    status: ArgusStatus = ArgusStatus.SHIPPED,
) -> StatusEvent:
    event = StatusEvent(
        rule_id=rule.id,
        provider_message_id=message_id,
        status=status,
        occurred_at=now(),
    )
    session.add(event)
    session.flush()
    return event


def test_account_relationship_graph(session: Session) -> None:
    account = make_account(session)
    checkpoint = SyncCheckpoint(
        account_id=account.id,
        cursor="12345",
        pending_cursor="https://graph.microsoft.com/next-page",
        provider_metadata={"historyId": "12345"},
    )
    rule = TrackingRule(
        account_id=account.id,
        name="Apple order",
        sender_pattern="apple.com",
        required_keywords=["apple", "order"],
        reference_number="W123456789",
    )
    processed = ProcessedEmail(
        account_id=account.id,
        provider_message_id="message-1",
        internet_message_id="<message-1@example.com>",
        thread_id="thread-1",
        received_at=now(),
    )
    session.add_all([checkpoint, rule, processed])
    session.flush()

    event = StatusEvent(
        rule_id=rule.id,
        provider_message_id="message-1",
        internet_message_id="<message-1@example.com>",
        previous_status=ArgusStatus.ORDER_PLACED,
        status=ArgusStatus.SHIPPED,
        matched_cue="shipped",
        occurred_at=now(),
    )
    session.add(event)
    session.flush()

    notification = Notification(status_event_id=event.id)
    session.add(notification)
    session.commit()

    assert account.sync_checkpoint is checkpoint
    assert rule in account.tracking_rules
    assert processed in account.processed_emails
    assert event in rule.status_events
    assert notification in event.notifications
    assert rule.state is RuleState.ACTIVE
    assert notification.channel is NotificationChannel.TELEGRAM
    assert notification.state is NotificationState.PENDING
    assert checkpoint.provider_metadata == {"historyId": "12345"}
    assert checkpoint.pending_cursor == "https://graph.microsoft.com/next-page"


def test_status_history_is_preserved(session: Session) -> None:
    account = make_account(session)
    rule = make_rule(session, account, name="Preserve history")

    session.add_all(
        [
            StatusEvent(
                rule_id=rule.id,
                provider_message_id="message-1",
                status=ArgusStatus.ORDER_PLACED,
                occurred_at=now(),
            ),
            StatusEvent(
                rule_id=rule.id,
                provider_message_id="message-2",
                previous_status=ArgusStatus.ORDER_PLACED,
                status=ArgusStatus.SHIPPED,
                occurred_at=now(),
            ),
        ]
    )
    session.commit()

    rows = session.scalars(
        select(StatusEvent)
        .where(StatusEvent.rule_id == rule.id)
        .order_by(StatusEvent.id)
    ).all()

    assert [row.status for row in rows] == [
        ArgusStatus.ORDER_PLACED,
        ArgusStatus.SHIPPED,
    ]


def test_timestamps_round_trip_as_utc(engine: Engine) -> None:
    occurred_at = datetime(
        2026,
        1,
        2,
        3,
        4,
        5,
        123456,
        tzinfo=timezone(timedelta(hours=-5)),
    )

    with Session(engine, expire_on_commit=False) as first:
        account = make_account(first, email="timezone@example.com")
        rule = make_rule(first, account, name="Timezone")
        event = StatusEvent(
            rule_id=rule.id,
            provider_message_id="message-tz",
            status=ArgusStatus.SHIPPED,
            occurred_at=occurred_at,
        )
        first.add(event)
        first.commit()

    with Session(engine) as second:
        loaded = second.scalar(select(StatusEvent))

    assert loaded is not None
    assert loaded.occurred_at.tzinfo is not None
    assert loaded.occurred_at.utcoffset() == timedelta(0)
    assert loaded.occurred_at == occurred_at.astimezone(UTC)


def test_json_mutations_are_persisted(engine: Engine) -> None:
    with Session(engine, expire_on_commit=False) as first:
        account = make_account(first, email="json@example.com")
        checkpoint = SyncCheckpoint(account_id=account.id)
        rule = TrackingRule(
            account_id=account.id,
            name="Mutable JSON",
            required_keywords=["order"],
        )
        first.add_all([checkpoint, rule])
        first.commit()

        # Mutate in place: this must be detected by the Mutable* types.
        account.provider_metadata["mailbox"] = "inbox"
        checkpoint.provider_metadata["delta"] = "abc"
        rule.required_keywords.append("dispatch")
        first.commit()

    with Session(engine) as second:
        loaded_account = second.scalar(select(EmailAccount))
        loaded_checkpoint = second.scalar(select(SyncCheckpoint))
        loaded_rule = second.scalar(select(TrackingRule))

    assert loaded_account is not None
    assert loaded_checkpoint is not None
    assert loaded_rule is not None
    assert loaded_account.provider_metadata == {"mailbox": "inbox"}
    assert loaded_checkpoint.provider_metadata == {"delta": "abc"}
    assert loaded_rule.required_keywords == ["order", "dispatch"]


def test_original_email_address_is_preserved_and_normalized(session: Session) -> None:
    account = make_account(session, email="User@Example.COM")
    session.commit()

    assert account.email_address == "User@Example.COM"
    assert account.normalized_email_address == "user@example.com"


def test_duplicate_provider_account_is_rejected(session: Session) -> None:
    session.add_all(
        [
            EmailAccount(
                provider=ProviderName.GMAIL,
                email_address="duplicate@example.com",
            ),
            EmailAccount(
                provider=ProviderName.GMAIL,
                email_address="duplicate@example.com",
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        session.flush()

    session.rollback()


def test_case_insensitive_duplicate_account_is_rejected(session: Session) -> None:
    session.add_all(
        [
            EmailAccount(
                provider=ProviderName.GMAIL,
                email_address="User@Example.com",
            ),
            EmailAccount(
                provider=ProviderName.GMAIL,
                email_address="USER@example.COM",
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        session.flush()

    session.rollback()


def test_same_address_is_allowed_across_providers(session: Session) -> None:
    session.add_all(
        [
            EmailAccount(
                provider=ProviderName.GMAIL,
                email_address="shared@example.com",
            ),
            EmailAccount(
                provider=ProviderName.OUTLOOK,
                email_address="SHARED@example.com",
            ),
        ]
    )
    session.commit()

    count = session.scalar(select(func.count()).select_from(EmailAccount))
    assert count == 2


def test_duplicate_processed_email_is_rejected(session: Session) -> None:
    account = make_account(session)
    session.add_all(
        [
            ProcessedEmail(
                account_id=account.id,
                provider_message_id="duplicate-message",
            ),
            ProcessedEmail(
                account_id=account.id,
                provider_message_id="duplicate-message",
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        session.flush()

    session.rollback()


def test_duplicate_status_event_is_rejected(session: Session) -> None:
    account = make_account(session)
    rule = make_rule(session, account, name="Duplicate event")

    session.add_all(
        [
            StatusEvent(
                rule_id=rule.id,
                provider_message_id="message-1",
                status=ArgusStatus.SHIPPED,
                occurred_at=now(),
            ),
            StatusEvent(
                rule_id=rule.id,
                provider_message_id="message-1",
                status=ArgusStatus.SHIPPED,
                occurred_at=now(),
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        session.flush()

    session.rollback()


def test_duplicate_notification_is_rejected(session: Session) -> None:
    account = make_account(session)
    rule = make_rule(session, account, name="Duplicate notification")
    event = make_event(session, rule, status=ArgusStatus.DELIVERED)

    session.add_all(
        [
            Notification(
                status_event_id=event.id,
                channel=NotificationChannel.TELEGRAM,
            ),
            Notification(
                status_event_id=event.id,
                channel=NotificationChannel.TELEGRAM,
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        session.flush()

    session.rollback()


def test_notification_state_transitions_are_persisted(engine: Engine) -> None:
    with Session(engine, expire_on_commit=False) as first:
        account = make_account(first, email="notify@example.com")
        rule = make_rule(first, account, name="Notify")
        event = make_event(first, rule, status=ArgusStatus.DELIVERED)
        notification = Notification(status_event_id=event.id)
        first.add(notification)
        first.commit()

        assert notification.state is NotificationState.PENDING
        assert notification.sent_at is None
        assert notification.attempts == 0

        # A failed attempt is recorded on the existing row.
        notification.state = NotificationState.FAILED
        notification.error = "temporary failure"
        notification.attempts += 1
        first.commit()

        # A later retry succeeds and records the external identifier.
        notification.state = NotificationState.SENT
        notification.error = None
        notification.sent_at = utcnow()
        notification.external_message_id = 4242
        notification.attempts += 1
        first.commit()

    with Session(engine) as second:
        loaded = second.scalar(select(Notification))

    assert loaded is not None
    assert loaded.state is NotificationState.SENT
    assert loaded.sent_at is not None
    assert loaded.sent_at.tzinfo is not None
    assert loaded.external_message_id == 4242
    assert loaded.attempts == 2


def test_soft_delete_fields_are_available(session: Session) -> None:
    account = make_account(session, email="soft@example.com")
    rule = make_rule(session, account, name="Soft delete")
    session.commit()

    account.is_active = False
    account.deleted_at = utcnow()
    rule.state = RuleState.STOPPED
    rule.deleted_at = utcnow()
    session.commit()

    loaded_account = session.scalar(
        select(EmailAccount).where(EmailAccount.email_address == "soft@example.com")
    )
    loaded_rule = session.scalar(
        select(TrackingRule).where(TrackingRule.name == "Soft delete")
    )

    assert loaded_account is not None
    assert loaded_account.is_active is False
    assert loaded_account.deleted_at is not None
    assert loaded_rule is not None
    assert loaded_rule.state is RuleState.STOPPED
    assert loaded_rule.deleted_at is not None


def test_long_provider_identifiers_and_cursors_are_stored(session: Session) -> None:
    account = make_account(session, email="long@example.com")
    long_message_id = "m" * 4000
    long_cursor = "https://graph.microsoft.com/v1.0/me/messages/delta?$deltatoken=" + ("x" * 4000)
    long_internet_id = "<" + ("a" * 2000) + "@example.com>"

    checkpoint = SyncCheckpoint(
        account_id=account.id,
        cursor=long_cursor,
        pending_cursor="https://graph.microsoft.com/next=" + ("y" * 2000),
    )
    processed = ProcessedEmail(
        account_id=account.id,
        provider_message_id=long_message_id,
        internet_message_id=long_internet_id,
    )
    session.add_all([checkpoint, processed])
    session.commit()

    loaded_checkpoint = session.scalar(select(SyncCheckpoint))
    loaded_processed = session.scalar(select(ProcessedEmail))

    assert loaded_checkpoint is not None
    assert loaded_processed is not None
    assert loaded_checkpoint.cursor == long_cursor
    assert loaded_checkpoint.pending_cursor.endswith("y" * 2000)
    assert loaded_processed.provider_message_id == long_message_id
    assert loaded_processed.internet_message_id == long_internet_id


def test_deleting_account_with_rules_is_rejected(session: Session) -> None:
    account = make_account(session, email="protected-account@example.com")
    make_rule(session, account, name="Protected")
    session.commit()

    session.delete(account)
    with pytest.raises(IntegrityError):
        session.flush()

    session.rollback()
    assert session.scalar(select(func.count()).select_from(EmailAccount)) == 1
    assert session.scalar(select(func.count()).select_from(TrackingRule)) == 1


def test_deleting_rule_with_status_events_is_rejected(session: Session) -> None:
    account = make_account(session, email="protected-rule@example.com")
    rule = make_rule(session, account, name="Protected rule")
    make_event(session, rule)
    session.commit()

    session.delete(rule)
    with pytest.raises(IntegrityError):
        session.flush()

    session.rollback()
    assert session.scalar(select(func.count()).select_from(TrackingRule)) == 1
    assert session.scalar(select(func.count()).select_from(StatusEvent)) == 1


def test_deleting_status_event_with_notification_is_rejected(session: Session) -> None:
    account = make_account(session, email="protected-event@example.com")
    rule = make_rule(session, account, name="Protected event")
    event = make_event(session, rule)
    session.add(Notification(status_event_id=event.id))
    session.commit()

    session.delete(event)
    with pytest.raises(IntegrityError):
        session.flush()

    session.rollback()
    assert session.scalar(select(func.count()).select_from(StatusEvent)) == 1
    assert session.scalar(select(func.count()).select_from(Notification)) == 1


def test_deleting_account_without_history_cascades_disposable_rows(
    session: Session,
) -> None:
    account = make_account(session, email="disposable@example.com")
    session.add_all(
        [
            SyncCheckpoint(account_id=account.id, cursor="1"),
            ProcessedEmail(
                account_id=account.id,
                provider_message_id="message-1",
            ),
        ]
    )
    session.commit()

    session.delete(account)
    session.commit()

    assert session.scalar(select(func.count()).select_from(EmailAccount)) == 0
    assert session.scalar(select(func.count()).select_from(SyncCheckpoint)) == 0
    assert session.scalar(select(func.count()).select_from(ProcessedEmail)) == 0


def test_invalid_provider_enum_is_rejected_by_database(engine: Engine) -> None:
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO email_accounts ("
                "provider, email_address, normalized_email_address, is_active, "
                "provider_metadata, created_at, updated_at"
                ") VALUES ("
                "'not-a-provider', 'a@example.com', 'a@example.com', 1, '{}', "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00'"
                ")"
            )


def test_invalid_status_enum_is_rejected_by_database(engine: Engine) -> None:
    with Session(engine, expire_on_commit=False) as seed:
        account = make_account(seed, email="enum@example.com")
        rule = make_rule(seed, account, name="Enum")
        make_event(seed, rule)
        seed.commit()

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE status_events SET status = 'not-a-status'"
            )
