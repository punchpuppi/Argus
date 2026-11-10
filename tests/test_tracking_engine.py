"""Tests for tracking-rule retrieval, status detection, and event persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.database.enums import (
    ArgusStatus,
    ProviderName,
    RuleState,
)
from app.database.models import (
    EmailAccount,
    ProcessedEmail,
    StatusEvent,
    TrackingRule,
    utcnow,
)
from app.email.schemas import NormalizedEmail
from app.tracking import engine
from app.tracking.engine import (
    REASON_DUPLICATE_EVENT,
    REASON_NO_STATUS_CUE,
    REASON_OUT_OF_ORDER,
    REASON_STATUS_UNCHANGED,
    process_email,
)
from app.tracking.errors import TrackingError
from app.tracking.rules import get_active_rules
from app.tracking.status import StatusDetection

RECEIVED_AT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def make_account(
    session: Session,
    *,
    email: str = "user@example.com",
    provider: ProviderName = ProviderName.GMAIL,
    is_active: bool = True,
) -> EmailAccount:
    account = EmailAccount(
        provider=provider,
        email_address=email,
        token_path="credentials/gmail_token.json",
        is_active=is_active,
    )
    session.add(account)
    session.flush()
    return account


def make_rule(
    session: Session,
    account: EmailAccount,
    *,
    name: str = "Order tracking",
    sender_pattern: str | None = "shop.example",
    required_keywords: list[str] | None = None,
    reference_number: str | None = None,
    state: RuleState = RuleState.ACTIVE,
    deleted_at: datetime | None = None,
) -> TrackingRule:
    rule = TrackingRule(
        account_id=account.id,
        name=name,
        sender_pattern=sender_pattern,
        required_keywords=["order"] if required_keywords is None else required_keywords,
        reference_number=reference_number,
        state=state,
        deleted_at=deleted_at,
    )
    session.add(rule)
    session.flush()
    return rule


def make_email(
    *,
    message_id: str = "message-1",
    sender: str | None = "Shop <no-reply@shop.example>",
    subject: str | None = "Your order has shipped",
    snippet: str | None = None,
    received_at: datetime | None = RECEIVED_AT,
    provider: ProviderName = ProviderName.GMAIL,
) -> NormalizedEmail:
    return NormalizedEmail(
        provider=provider,
        provider_message_id=message_id,
        thread_id="thread-1",
        internet_message_id=f"<{message_id}@example.com>",
        sender=sender,
        recipients=("user@example.com",),
        subject=subject,
        snippet=snippet,
        received_at=received_at,
        labels=(),
    )


def count_rows(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_get_active_rules_returns_enabled_rules(session: Session) -> None:
    account = make_account(session)
    rule = make_rule(session, account)
    session.commit()

    rules = get_active_rules(session, account.id)

    assert [item.id for item in rules] == [rule.id]


@pytest.mark.parametrize("state", (RuleState.PAUSED, RuleState.STOPPED))
def test_get_active_rules_excludes_inactive_states(
    session: Session,
    state: RuleState,
) -> None:
    account = make_account(session)
    make_rule(session, account, state=state)
    session.commit()

    assert get_active_rules(session, account.id) == []


def test_get_active_rules_excludes_soft_deleted_rules(session: Session) -> None:
    account = make_account(session)
    make_rule(session, account, deleted_at=utcnow())
    session.commit()

    assert get_active_rules(session, account.id) == []


def test_get_active_rules_is_scoped_to_the_account(session: Session) -> None:
    account = make_account(session)
    other_account = make_account(session, email="other@example.com")
    make_rule(session, account)
    make_rule(session, other_account)
    session.commit()

    rules = get_active_rules(session, account.id)

    assert {item.account_id for item in rules} == {account.id}


def test_get_active_rules_excludes_inactive_accounts(session: Session) -> None:
    account = make_account(session, is_active=False)
    make_rule(session, account)
    session.commit()

    assert get_active_rules(session, account.id) == []


def test_get_active_rules_returns_empty_list_when_none_configured(
    session: Session,
) -> None:
    account = make_account(session)
    session.commit()

    assert get_active_rules(session, account.id) == []


def test_process_email_creates_status_event(session: Session) -> None:
    account = make_account(session)
    rule = make_rule(session, account)
    session.commit()

    result = process_email(session, account.id, make_email())
    session.commit()

    assert result.duplicate_email is False
    assert result.matched_rule_ids == (rule.id,)
    assert len(result.created_event_ids) == 1

    event = session.scalars(select(StatusEvent)).one()
    assert event.rule_id == rule.id
    assert event.provider_message_id == "message-1"
    assert event.status is ArgusStatus.SHIPPED
    assert event.previous_status is None
    assert event.matched_cue == "shipped"
    assert event.occurred_at == RECEIVED_AT
    assert count_rows(session, ProcessedEmail) == 1


def test_process_email_records_only_metadata(session: Session) -> None:
    account = make_account(session)
    make_rule(session, account)
    session.commit()

    process_email(
        session,
        account.id,
        make_email(subject="Your order has shipped - tracking ABC123"),
    )
    session.commit()

    event = session.scalars(select(StatusEvent)).one()
    assert event.matched_cue == "shipped"
    assert "ABC123" not in event.matched_cue


def test_process_email_claims_email_without_matching_rules(session: Session) -> None:
    account = make_account(session)
    make_rule(session, account, required_keywords=["invoice"])
    session.commit()

    result = process_email(session, account.id, make_email())
    session.commit()

    assert result.outcomes == ()
    assert count_rows(session, ProcessedEmail) == 1
    assert count_rows(session, StatusEvent) == 0


def test_process_email_ignores_paused_rules(session: Session) -> None:
    account = make_account(session)
    make_rule(session, account, state=RuleState.PAUSED)
    session.commit()

    result = process_email(session, account.id, make_email())
    session.commit()

    assert result.outcomes == ()
    assert count_rows(session, ProcessedEmail) == 1
    assert count_rows(session, StatusEvent) == 0


def test_process_email_skips_unchanged_status(session: Session) -> None:
    account = make_account(session)
    make_rule(session, account)
    session.commit()

    process_email(session, account.id, make_email(message_id="message-1"))
    session.commit()
    result = process_email(session, account.id, make_email(message_id="message-2"))
    session.commit()

    assert [outcome.reason for outcome in result.outcomes] == [REASON_STATUS_UNCHANGED]
    assert result.created_event_ids == ()
    assert count_rows(session, StatusEvent) == 1


def test_process_email_deduplicates_repeated_email(session: Session) -> None:
    account = make_account(session)
    make_rule(session, account)
    session.commit()

    first = process_email(session, account.id, make_email())
    session.commit()
    second = process_email(session, account.id, make_email())
    session.commit()

    assert first.duplicate_email is False
    assert second.duplicate_email is True
    assert second.outcomes == ()
    assert count_rows(session, ProcessedEmail) == 1
    assert count_rows(session, StatusEvent) == 1


def test_process_email_skips_duplicate_status_event(session: Session) -> None:
    account = make_account(session)
    rule = make_rule(session, account)
    session.add_all(
        [
            StatusEvent(
                rule_id=rule.id,
                provider_message_id="message-1",
                status=ArgusStatus.SHIPPED,
                occurred_at=RECEIVED_AT - timedelta(hours=2),
            ),
            StatusEvent(
                rule_id=rule.id,
                provider_message_id="message-2",
                status=ArgusStatus.DELIVERED,
                occurred_at=RECEIVED_AT - timedelta(hours=1),
            ),
        ]
    )
    session.commit()

    result = process_email(session, account.id, make_email(message_id="message-1"))
    session.commit()

    assert [outcome.reason for outcome in result.outcomes] == [REASON_DUPLICATE_EVENT]
    assert result.created_event_ids == ()
    assert count_rows(session, StatusEvent) == 2


def test_process_email_records_previous_status_chain(session: Session) -> None:
    account = make_account(session)
    make_rule(session, account)
    session.commit()

    process_email(
        session,
        account.id,
        make_email(message_id="message-1", subject="Your order has been confirmed"),
    )
    session.commit()
    process_email(
        session,
        account.id,
        make_email(
            message_id="message-2",
            subject="Your order has shipped",
            received_at=RECEIVED_AT + timedelta(hours=1),
        ),
    )
    session.commit()

    events = session.scalars(
        select(StatusEvent).order_by(StatusEvent.occurred_at)
    ).all()

    assert [event.status for event in events] == [
        ArgusStatus.ORDER_PLACED,
        ArgusStatus.SHIPPED,
    ]
    assert events[0].previous_status is None
    assert events[1].previous_status is ArgusStatus.ORDER_PLACED


def test_process_email_skips_out_of_order_email(session: Session) -> None:
    account = make_account(session)
    make_rule(session, account)
    session.commit()

    process_email(session, account.id, make_email(message_id="message-1"))
    session.commit()

    result = process_email(
        session,
        account.id,
        make_email(
            message_id="message-2",
            subject="Your order was delivered",
            received_at=RECEIVED_AT - timedelta(days=1),
        ),
    )
    session.commit()

    assert [outcome.reason for outcome in result.outcomes] == [REASON_OUT_OF_ORDER]
    assert count_rows(session, StatusEvent) == 1


def test_process_email_evaluates_each_matching_rule(session: Session) -> None:
    account = make_account(session)
    first = make_rule(session, account)
    second = make_rule(session, account, name="Order tracking secondary")
    session.commit()

    result = process_email(session, account.id, make_email())
    session.commit()

    assert len(result.created_event_ids) == 2
    assert count_rows(session, StatusEvent) == 2
    assert {event.rule_id for event in session.scalars(select(StatusEvent))} == {
        first.id,
        second.id,
    }


def test_process_email_records_no_event_without_status_cue(session: Session) -> None:
    account = make_account(session)
    make_rule(session, account)
    session.commit()

    result = process_email(session, account.id, make_email(subject="Your order details"))
    session.commit()

    assert [outcome.reason for outcome in result.outcomes] == [REASON_NO_STATUS_CUE]
    assert count_rows(session, StatusEvent) == 0
    assert count_rows(session, ProcessedEmail) == 1


def test_process_email_handles_missing_metadata(session: Session) -> None:
    account = make_account(session)
    make_rule(session, account)
    session.commit()

    result = process_email(
        session,
        account.id,
        make_email(sender=None, subject=None, snippet=None),
    )
    session.commit()

    assert result.outcomes == ()
    assert count_rows(session, StatusEvent) == 0
    assert count_rows(session, ProcessedEmail) == 1


def test_process_email_uses_current_time_when_received_at_is_missing(
    session: Session,
) -> None:
    account = make_account(session)
    make_rule(session, account)
    session.commit()

    before = utcnow()
    process_email(session, account.id, make_email(received_at=None))
    session.commit()

    event = session.scalars(select(StatusEvent)).one()
    assert event.occurred_at >= before


def test_process_email_rejects_unknown_account(session: Session) -> None:
    with pytest.raises(TrackingError, match="9999"):
        process_email(session, 9999, make_email())


def test_process_email_rejects_inactive_account(session: Session) -> None:
    account = make_account(session, is_active=False)
    session.commit()

    with pytest.raises(TrackingError, match="not active"):
        process_email(session, account.id, make_email())


def test_process_email_rejects_provider_mismatch(session: Session) -> None:
    account = make_account(session)
    session.commit()

    email = make_email(
        provider=ProviderName.OUTLOOK,
        subject="confidential-subject",
    )

    with pytest.raises(TrackingError) as excinfo:
        process_email(session, account.id, email)

    assert "confidential-subject" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_process_email_rejects_blank_message_id(session: Session) -> None:
    account = make_account(session)
    session.commit()

    with pytest.raises(TrackingError, match="message identifier"):
        process_email(session, account.id, make_email(message_id="   "))


def test_persistence_failure_leaves_no_partial_records(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = make_account(session)
    make_rule(session, account)
    session.commit()

    email = make_email()
    original_flush = session.flush

    def failing_flush(*args: object, **kwargs: object) -> None:
        if any(isinstance(item, StatusEvent) for item in session.new):
            raise OperationalError(
                "INSERT INTO status_events",
                {},
                RuntimeError("simulated write failure"),
            )
        original_flush(*args, **kwargs)

    monkeypatch.setattr(session, "flush", failing_flush)

    with pytest.raises(OperationalError):
        process_email(session, account.id, email)

    monkeypatch.undo()
    session.rollback()

    assert count_rows(session, ProcessedEmail) == 0
    assert count_rows(session, StatusEvent) == 0

    result = process_email(session, account.id, email)
    session.commit()

    assert len(result.created_event_ids) == 1
    assert count_rows(session, ProcessedEmail) == 1
    assert count_rows(session, StatusEvent) == 1


def test_padded_message_id_is_normalised_before_persistence(
    session: Session,
) -> None:
    account = make_account(session)
    make_rule(session, account)
    session.commit()

    first = process_email(
        session,
        account.id,
        make_email(message_id="  message-1  "),
    )
    session.commit()
    second = process_email(session, account.id, make_email(message_id="message-1"))
    session.commit()

    stored = session.scalars(select(ProcessedEmail)).one()
    assert stored.provider_message_id == "message-1"
    assert first.provider_message_id == "message-1"
    assert second.duplicate_email is True
    assert count_rows(session, ProcessedEmail) == 1
    assert count_rows(session, StatusEvent) == 1


def test_naive_received_at_is_treated_as_utc(session: Session) -> None:
    account = make_account(session)
    make_rule(session, account)
    session.commit()

    process_email(
        session,
        account.id,
        make_email(
            message_id="message-1",
            subject="Your order has been confirmed",
            received_at=datetime(2026, 1, 1, 12, 0),
        ),
    )
    session.commit()

    # A second, newer naive timestamp must compare cleanly with the stored
    # aware value instead of raising a naive-versus-aware TypeError.
    process_email(
        session,
        account.id,
        make_email(
            message_id="message-2",
            subject="Your order has shipped",
            received_at=datetime(2026, 1, 1, 13, 0),
        ),
    )
    session.commit()

    events = session.scalars(
        select(StatusEvent).order_by(StatusEvent.occurred_at)
    ).all()

    assert [event.status for event in events] == [
        ArgusStatus.ORDER_PLACED,
        ArgusStatus.SHIPPED,
    ]
    assert events[0].occurred_at == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert events[1].occurred_at == datetime(2026, 1, 1, 13, 0, tzinfo=UTC)


def test_status_detection_runs_once_per_email(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = make_account(session)
    make_rule(session, account)
    make_rule(session, account, name="Order tracking secondary")
    session.commit()

    calls: list[str] = []
    original = engine.detect_status

    def counting_detect(email: NormalizedEmail) -> StatusDetection | None:
        calls.append(email.provider_message_id)
        return original(email)

    monkeypatch.setattr(engine, "detect_status", counting_detect)

    result = process_email(session, account.id, make_email())
    session.commit()

    assert len(result.created_event_ids) == 2
    assert len(calls) == 1
