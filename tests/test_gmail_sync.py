"""Tests for the incremental Gmail synchronization service."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httplib2
import pytest
from googleapiclient.errors import HttpError
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, InvalidRequestError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.database import session as session_module
from app.database.enums import ArgusStatus, ProviderName, RuleState
from app.database.models import (
    EmailAccount,
    ProcessedEmail,
    StatusEvent,
    SyncCheckpoint,
    TrackingRule,
)
from app.providers.gmail.messages import GMAIL_METADATA_HEADERS
from app.services.sync import gmail_sync
from app.services.sync.errors import (
    CheckpointConflictError,
    SyncAlreadyRunningError,
    SyncError,
)
from app.services.sync.gmail_sync import sync_gmail_account
from app.tracking.engine import UNKNOWN_TIMESTAMP
from app.tracking.errors import TrackingError

EARLY = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
MIDDLE = EARLY + timedelta(hours=1)
LATE = EARLY + timedelta(hours=2)


def milliseconds(value: datetime) -> str:
    """Return a Gmail ``internalDate`` string for a datetime."""

    return str(int(value.timestamp() * 1000))


@pytest.fixture()
def scoped_sessions(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
) -> sessionmaker[Session]:
    """Point the project session factory at the temporary test database."""

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(session_module, "_session_factory", factory)
    return factory


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
) -> TrackingRule:
    rule = TrackingRule(
        account_id=account.id,
        name=name,
        sender_pattern=sender_pattern,
        required_keywords=["order"] if required_keywords is None else required_keywords,
        state=RuleState.ACTIVE,
    )
    session.add(rule)
    session.flush()
    return rule


def seed_checkpoint(
    session: Session,
    account: EmailAccount,
    **fields: Any,
) -> SyncCheckpoint:
    checkpoint = SyncCheckpoint(account_id=account.id, **fields)
    session.add(checkpoint)
    session.commit()
    return checkpoint


def read_checkpoint(engine: Engine, account_id: int) -> SyncCheckpoint | None:
    with Session(engine) as reader:
        return reader.scalar(
            select(SyncCheckpoint).where(
                SyncCheckpoint.account_id == account_id
            )
        )


def count_rows(engine: Engine, model: type) -> int:
    with Session(engine) as reader:
        return reader.scalar(select(func.count()).select_from(model)) or 0


def history_response(
    history_id: str,
    *,
    message_ids: tuple[str, ...] = (),
    next_page_token: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "historyId": history_id,
        "history": [
            {
                "messagesAdded": [
                    {"message": {"id": message_id}}
                    for message_id in message_ids
                ]
            }
        ]
        if message_ids
        else [],
    }
    if next_page_token is not None:
        payload["nextPageToken"] = next_page_token
    return payload


def message_response(
    message_id: str,
    *,
    received_at: datetime | None = EARLY,
    subject: str = "Your order has shipped",
    sender: str = "Shop <no-reply@shop.example>",
) -> dict[str, Any]:
    headers = [
        {"name": "Message-ID", "value": f"<{message_id}@example.com>"},
        {"name": "From", "value": sender},
        {"name": "To", "value": "user@example.com"},
        {"name": "Subject", "value": subject},
    ]
    payload: dict[str, Any] = {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "labelIds": ["INBOX"],
        "payload": {"headers": headers},
    }
    if received_at is not None:
        payload["internalDate"] = milliseconds(received_at)
    return payload


def http_error(status: int, body: bytes = b'{"error": {"message": "raw"}}') -> HttpError:
    response = httplib2.Response({"status": status})
    response.reason = "Error"
    return HttpError(response, body)


def sqlite_operational_error(
    code: int,
    name: str,
    message: str = "database error",
) -> OperationalError:
    """Build a synthetic SQLAlchemy OperationalError with SQLite metadata."""

    original = sqlite3.OperationalError(message)
    original.sqlite_errorcode = code
    original.sqlite_errorname = name
    return OperationalError("statement", {}, original)


class _Execute:
    """Minimal request object compatible with ``execute_request``."""

    def __init__(self, value: Any = None, error: BaseException | None = None) -> None:
        self._value = value
        self._error = error

    def execute(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._value


def _execute(value: Any) -> _Execute:
    if isinstance(value, BaseException):
        return _Execute(error=value)
    return _Execute(value=value)


class FakeGmailService:
    """Small Gmail service double for history pagination and metadata lookup."""

    def __init__(
        self,
        pages: list[Any],
        metadata: dict[str, Any],
        *,
        on_history: Any = None,
    ) -> None:
        self._pages = list(pages)
        self._metadata = metadata
        self._page_index = 0
        self._on_history = on_history
        self.history_calls: list[dict[str, Any]] = []
        self.metadata_calls: list[dict[str, Any]] = []

    def users(self) -> "FakeGmailService":
        return self

    def history(self) -> "FakeGmailService":
        return self

    def messages(self) -> "FakeGmailService":
        return self

    def list(self, **kwargs: Any) -> _Execute:
        self.history_calls.append(kwargs)
        if self._on_history is not None:
            self._on_history(kwargs)
        if self._page_index >= len(self._pages):
            raise AssertionError("unexpected history request")
        page = self._pages[self._page_index]
        self._page_index += 1
        return _execute(page)

    def get(self, **kwargs: Any) -> _Execute:
        self.metadata_calls.append(kwargs)
        message_id = kwargs["id"]
        if message_id not in self._metadata:
            raise AssertionError(f"unexpected metadata request for {message_id}")
        return _execute(self._metadata[message_id])


# ---------------------------------------------------------------------------
# Successful runs
# ---------------------------------------------------------------------------


def test_successful_sync_advances_checkpoint_and_records_events(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    make_rule(session, account)
    seed_checkpoint(session, account, cursor="100")

    service = FakeGmailService(
        [history_response("200", message_ids=("message-1", "message-2"))],
        {
            "message-1": message_response(
                "message-1",
                received_at=EARLY,
                subject="Your order has been confirmed",
            ),
            "message-2": message_response(
                "message-2",
                received_at=MIDDLE,
                subject="Your order has shipped",
            ),
        },
    )

    result = sync_gmail_account(account.id, service=service)

    assert result.success is True
    assert result.account_id == account.id
    assert result.message_ids == ("message-1", "message-2")
    assert result.processed_count == 2
    assert result.duplicate_count == 0
    assert result.events_created == 2
    assert result.history_id == "200"
    assert result.error is None

    assert service.history_calls[0]["startHistoryId"] == "100"
    for call in service.metadata_calls:
        assert call["format"] == "metadata"
        assert call["metadataHeaders"] == list(GMAIL_METADATA_HEADERS)

    checkpoint = read_checkpoint(engine, account.id)
    assert checkpoint is not None
    assert checkpoint.cursor == "200"
    assert checkpoint.cursor_updated_at is not None
    assert checkpoint.last_success_at is not None
    assert checkpoint.consecutive_failures == 0

    events = session.scalars(
        select(StatusEvent).order_by(StatusEvent.occurred_at)
    ).all()
    assert [event.status for event in events] == [
        ArgusStatus.ORDER_PLACED,
        ArgusStatus.SHIPPED,
    ]


def test_empty_history_advances_cursor_without_processing(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")
    service = FakeGmailService([history_response("200")], {})

    result = sync_gmail_account(account.id, service=service)

    assert result.success is True
    assert result.processed_count == 0
    assert result.message_ids == ()
    assert service.metadata_calls == []
    assert read_checkpoint(engine, account.id).cursor == "200"  # type: ignore[union-attr]


def test_multiple_pages_deduplicate_message_ids_across_pages(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")
    service = FakeGmailService(
        [
            history_response(
                "200",
                message_ids=("message-1", "message-2"),
                next_page_token="page-2",
            ),
            history_response(
                "300",
                message_ids=("message-2", "message-3"),
            ),
        ],
        {
            "message-1": message_response("message-1"),
            "message-2": message_response("message-2"),
            "message-3": message_response("message-3"),
        },
    )

    result = sync_gmail_account(account.id, service=service)

    assert result.success is True
    assert result.message_ids == ("message-1", "message-2", "message-3")
    assert result.processed_count == 3
    assert len(service.metadata_calls) == 3
    assert service.history_calls[1]["pageToken"] == "page-2"
    assert read_checkpoint(engine, account.id).cursor == "300"  # type: ignore[union-attr]


def test_checkpoint_is_not_advanced_from_an_intermediate_page(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")
    observed_cursors: list[str | None] = []

    def on_history(_kwargs: dict[str, Any]) -> None:
        observed_cursors.append(read_checkpoint(engine, account.id).cursor)  # type: ignore[union-attr]

    service = FakeGmailService(
        [
            history_response(
                "200",
                message_ids=("message-1",),
                next_page_token="page-2",
            ),
            history_response("300", message_ids=("message-2",)),
        ],
        {
            "message-1": message_response("message-1"),
            "message-2": message_response("message-2"),
        },
        on_history=on_history,
    )

    result = sync_gmail_account(account.id, service=service)

    assert result.success is True
    assert observed_cursors == ["100", "100"]
    assert read_checkpoint(engine, account.id).cursor == "300"  # type: ignore[union-attr]


def test_messages_are_processed_in_ascending_received_at_order(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")
    processed: list[str] = []

    def fake_process_email(
        _session: Session,
        _account_id: int,
        email: Any,
    ) -> SimpleNamespace:
        processed.append(email.provider_message_id)
        return SimpleNamespace(duplicate_email=False, created_event_ids=())

    monkeypatch.setattr(gmail_sync, "process_email", fake_process_email)

    service = FakeGmailService(
        [
            history_response(
                "200",
                message_ids=("late", "early", "middle"),
            )
        ],
        {
            "late": message_response("late", received_at=LATE),
            "early": message_response("early", received_at=EARLY),
            "middle": message_response("middle", received_at=MIDDLE),
        },
    )

    result = sync_gmail_account(account.id, service=service)

    assert result.success is True
    assert processed == ["early", "middle", "late"]


# ---------------------------------------------------------------------------
# Pagination and provider failures
# ---------------------------------------------------------------------------


def test_repeated_page_token_fails_without_advancing(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")
    service = FakeGmailService(
        [
            history_response(
                "200",
                message_ids=("message-1",),
                next_page_token="repeat",
            ),
            history_response(
                "300",
                message_ids=("message-2",),
                next_page_token="repeat",
            ),
        ],
        {"message-1": message_response("message-1")},
    )

    result = sync_gmail_account(account.id, service=service)

    assert result.success is False
    assert result.error is not None
    assert "repeated page token" in result.error
    assert len(service.history_calls) == 2
    assert service.metadata_calls == []

    checkpoint = read_checkpoint(engine, account.id)
    assert checkpoint is not None
    assert checkpoint.cursor == "100"
    assert checkpoint.consecutive_failures == 1


def test_history_expiration_is_a_failed_sync_without_advancing(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")
    service = FakeGmailService([http_error(404)], {})

    result = sync_gmail_account(account.id, service=service)

    assert result.success is False
    assert result.history_expired is True
    assert result.error is not None
    assert "GmailHistoryExpiredError" in result.error
    assert service.metadata_calls == []

    checkpoint = read_checkpoint(engine, account.id)
    assert checkpoint is not None
    assert checkpoint.cursor == "100"
    assert checkpoint.consecutive_failures == 1


def test_metadata_failure_preserves_cursor_and_processing_state(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    make_rule(session, account)
    seed_checkpoint(
        session,
        account,
        cursor="100",
        cursor_updated_at=EARLY,
        last_success_at=MIDDLE,
        last_seen_received_at=LATE,
    )
    service = FakeGmailService(
        [history_response("200", message_ids=("message-1", "message-2"))],
        {
            "message-1": message_response("message-1"),
            "message-2": RuntimeError("sensitive provider body"),
        },
    )

    result = sync_gmail_account(account.id, service=service)

    assert result.success is False
    assert result.error is not None
    assert "RuntimeError" in result.error
    assert "sensitive provider body" not in result.error
    assert len(service.metadata_calls) == 2

    checkpoint = read_checkpoint(engine, account.id)
    assert checkpoint is not None
    assert checkpoint.cursor == "100"
    assert checkpoint.cursor_updated_at == EARLY
    assert checkpoint.last_success_at == MIDDLE
    assert checkpoint.last_seen_received_at == LATE
    assert checkpoint.consecutive_failures == 1
    assert count_rows(engine, ProcessedEmail) == 0
    assert count_rows(engine, StatusEvent) == 0


# ---------------------------------------------------------------------------
# Tracking-engine failures
# ---------------------------------------------------------------------------


def test_tracking_failure_records_failure_and_preserves_cursor(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")

    def failing_process_email(*_args: Any, **_kwargs: Any) -> Any:
        raise TrackingError("rule evaluation failed")

    monkeypatch.setattr(gmail_sync, "process_email", failing_process_email)
    service = FakeGmailService(
        [history_response("200", message_ids=("message-1",))],
        {"message-1": message_response("message-1")},
    )

    result = sync_gmail_account(account.id, service=service)

    assert result.success is False
    assert result.error is not None
    assert "TrackingError" in result.error
    checkpoint = read_checkpoint(engine, account.id)
    assert checkpoint is not None
    assert checkpoint.cursor == "100"
    assert checkpoint.consecutive_failures == 1


# ---------------------------------------------------------------------------
# Checkpoint advancement and replay
# ---------------------------------------------------------------------------


def test_replay_after_checkpoint_advancement_failure_is_idempotent(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = make_account(session)
    make_rule(session, account)
    seed_checkpoint(session, account, cursor="100")

    def make_service() -> FakeGmailService:
        return FakeGmailService(
            [history_response("200", message_ids=("message-1", "message-2"))],
            {
                "message-1": message_response(
                    "message-1",
                    received_at=EARLY,
                    subject="Your order has been confirmed",
                ),
                "message-2": message_response(
                    "message-2",
                    received_at=MIDDLE,
                    subject="Your order has shipped",
                ),
            },
        )

    real_record_success = gmail_sync.record_success
    calls = {"count": 0}

    def flaky_record_success(*args: Any, **kwargs: Any) -> Any:
        calls["count"] += 1
        if calls["count"] == 1:
            raise OperationalError(
                "UPDATE sync_checkpoints",
                {},
                RuntimeError("crash after processing"),
            )
        return real_record_success(*args, **kwargs)

    monkeypatch.setattr(gmail_sync, "record_success", flaky_record_success)

    with pytest.raises(OperationalError):
        sync_gmail_account(account.id, service=make_service())

    checkpoint = read_checkpoint(engine, account.id)
    assert checkpoint is not None
    assert checkpoint.cursor == "100"
    assert checkpoint.last_success_at is None
    assert count_rows(engine, ProcessedEmail) == 2
    assert count_rows(engine, StatusEvent) == 2

    result = sync_gmail_account(account.id, service=make_service())

    assert result.success is True
    assert result.processed_count == 2
    assert result.duplicate_count == 2
    assert result.events_created == 0
    assert read_checkpoint(engine, account.id).cursor == "200"  # type: ignore[union-attr]
    assert count_rows(engine, ProcessedEmail) == 2
    assert count_rows(engine, StatusEvent) == 2


def test_optimistic_checkpoint_conflict_is_reported(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")

    def conflicting_record_success(*_args: Any, **_kwargs: Any) -> Any:
        raise CheckpointConflictError("checkpoint changed")

    monkeypatch.setattr(gmail_sync, "record_success", conflicting_record_success)
    service = FakeGmailService(
        [history_response("200", message_ids=("message-1",))],
        {"message-1": message_response("message-1")},
    )

    result = sync_gmail_account(account.id, service=service)

    assert result.success is False
    assert result.conflict is True
    assert result.error is not None
    assert "CheckpointConflictError" in result.error
    checkpoint = read_checkpoint(engine, account.id)
    assert checkpoint is not None
    assert checkpoint.cursor == "100"
    assert checkpoint.consecutive_failures == 1


# ---------------------------------------------------------------------------
# Preconditions and concurrency
# ---------------------------------------------------------------------------


def test_missing_account_raises_without_creating_checkpoint(
    engine: Engine,
    scoped_sessions: sessionmaker[Session],
) -> None:
    service = FakeGmailService([], {})

    with pytest.raises(SyncError, match="does not exist"):
        sync_gmail_account(9999, service=service)

    assert count_rows(engine, SyncCheckpoint) == 0


def test_missing_checkpoint_raises_without_creating_one(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    session.commit()
    service = FakeGmailService([], {})

    with pytest.raises(SyncError, match="No synchronization checkpoint"):
        sync_gmail_account(account.id, service=service)

    assert count_rows(engine, SyncCheckpoint) == 0


def test_checkpoint_without_cursor_records_failure_without_requests(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account)
    service = FakeGmailService([], {})

    result = sync_gmail_account(account.id, service=service)

    assert result.success is False
    assert result.error is not None
    assert "no history cursor" in result.error
    assert service.history_calls == []
    checkpoint = read_checkpoint(engine, account.id)
    assert checkpoint is not None
    assert checkpoint.consecutive_failures == 1


def test_non_gmail_account_is_rejected_with_failure_bookkeeping(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session, provider=ProviderName.OUTLOOK)
    seed_checkpoint(session, account, cursor="100")
    service = FakeGmailService([], {})

    result = sync_gmail_account(account.id, service=service)

    assert result.success is False
    assert result.error is not None
    assert "not a Gmail account" in result.error
    assert service.history_calls == []
    assert read_checkpoint(engine, account.id).cursor == "100"  # type: ignore[union-attr]


def test_concurrent_sync_for_same_account_is_rejected(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")

    entered = threading.Event()
    release = threading.Event()

    def on_history(_kwargs: dict[str, Any]) -> None:
        entered.set()
        assert release.wait(timeout=5)

    blocking_service = FakeGmailService(
        [history_response("200", message_ids=("message-1",))],
        {"message-1": message_response("message-1")},
        on_history=on_history,
    )
    results: list[Any] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(
                sync_gmail_account(account.id, service=blocking_service)
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(timeout=5)
        with pytest.raises(SyncAlreadyRunningError):
            sync_gmail_account(
                account.id,
                service=FakeGmailService([], {}),
            )
    finally:
        release.set()
        thread.join(timeout=10)

    assert thread.is_alive() is False
    assert errors == []
    assert results[0].success is True
    assert read_checkpoint(engine, account.id).cursor == "200"  # type: ignore[union-attr]


def test_different_accounts_synchronize_independently(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    first = make_account(session, email="first@example.com")
    second = make_account(session, email="second@example.com")
    seed_checkpoint(session, first, cursor="100")
    seed_checkpoint(session, second, cursor="500")

    entered = threading.Event()
    release = threading.Event()

    def on_history(_kwargs: dict[str, Any]) -> None:
        entered.set()
        assert release.wait(timeout=5)

    first_service = FakeGmailService(
        [history_response("200", message_ids=("first-message",))],
        {"first-message": message_response("first-message")},
        on_history=on_history,
    )
    second_service = FakeGmailService(
        [history_response("600", message_ids=("second-message",))],
        {"second-message": message_response("second-message")},
    )

    first_results: list[Any] = []
    first_errors: list[BaseException] = []

    def run_first() -> None:
        try:
            first_results.append(
                sync_gmail_account(first.id, service=first_service)
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            first_errors.append(exc)

    thread = threading.Thread(target=run_first)
    thread.start()
    try:
        assert entered.wait(timeout=5)
        second_result = sync_gmail_account(second.id, service=second_service)
        assert second_result.success is True
        assert read_checkpoint(engine, second.id).cursor == "600"  # type: ignore[union-attr]
    finally:
        release.set()
        thread.join(timeout=10)

    assert thread.is_alive() is False
    assert first_errors == []
    assert first_results[0].success is True
    assert read_checkpoint(engine, first.id).cursor == "200"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Database exception classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "name", "expected"),
    (
        (sqlite3.SQLITE_BUSY, "SQLITE_BUSY", True),
        (sqlite3.SQLITE_LOCKED, "SQLITE_LOCKED", True),
        (sqlite3.SQLITE_BUSY_SNAPSHOT, "SQLITE_BUSY_SNAPSHOT", True),
        (262, "SQLITE_LOCKED_SHAREDCACHE", True),
        (sqlite3.SQLITE_ERROR, "SQLITE_ERROR", False),
        (sqlite3.SQLITE_IOERR, "SQLITE_IOERR", False),
        (sqlite3.SQLITE_READONLY, "SQLITE_READONLY", False),
    ),
)
def test_sqlite_operational_error_classification(
    code: int,
    name: str,
    expected: bool,
) -> None:
    exc = sqlite_operational_error(code, name)
    assert gmail_sync._is_transient_sqlite_lock_error(exc) is expected


def test_non_sqlite_operational_error_is_not_a_transient_lock() -> None:
    exc = OperationalError("statement", {}, RuntimeError("not sqlite"))
    assert gmail_sync._is_transient_sqlite_lock_error(exc) is False


@pytest.mark.parametrize(
    ("code", "name"),
    (
        (sqlite3.SQLITE_BUSY, "SQLITE_BUSY"),
        (sqlite3.SQLITE_LOCKED, "SQLITE_LOCKED"),
        (sqlite3.SQLITE_BUSY_SNAPSHOT, "SQLITE_BUSY_SNAPSHOT"),
        (262, "SQLITE_LOCKED_SHAREDCACHE"),
    ),
)
def test_transient_sqlite_lock_is_recorded_and_cursor_preserved(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    name: str,
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")

    def locked_process_email(*_args: Any, **_kwargs: Any) -> Any:
        raise sqlite_operational_error(code, name, "database is locked")

    monkeypatch.setattr(gmail_sync, "process_email", locked_process_email)
    service = FakeGmailService(
        [history_response("200", message_ids=("message-1",))],
        {"message-1": message_response("message-1")},
    )

    result = sync_gmail_account(account.id, service=service)

    assert result.success is False
    assert result.error is not None
    assert "locked" in result.error
    checkpoint = read_checkpoint(engine, account.id)
    assert checkpoint is not None
    assert checkpoint.cursor == "100"
    assert checkpoint.consecutive_failures == 1


def test_permanent_operational_error_propagates_without_bookkeeping(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")

    def missing_table_process_email(*_args: Any, **_kwargs: Any) -> Any:
        raise sqlite_operational_error(
            sqlite3.SQLITE_ERROR,
            "SQLITE_ERROR",
            "no such table: status_events",
        )

    monkeypatch.setattr(gmail_sync, "process_email", missing_table_process_email)
    service = FakeGmailService(
        [history_response("200", message_ids=("message-1",))],
        {"message-1": message_response("message-1")},
    )

    with pytest.raises(OperationalError):
        sync_gmail_account(account.id, service=service)

    checkpoint = read_checkpoint(engine, account.id)
    assert checkpoint is not None
    assert checkpoint.cursor == "100"
    assert checkpoint.consecutive_failures == 0
    assert checkpoint.last_error is None
    assert checkpoint.last_attempt_at is None


def test_unexpected_sqlalchemy_error_propagates(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")

    def failing_process_email(*_args: Any, **_kwargs: Any) -> Any:
        raise InvalidRequestError("unexpected query construction")

    monkeypatch.setattr(gmail_sync, "process_email", failing_process_email)
    service = FakeGmailService(
        [history_response("200", message_ids=("message-1",))],
        {"message-1": message_response("message-1")},
    )

    with pytest.raises(InvalidRequestError):
        sync_gmail_account(account.id, service=service)

    checkpoint = read_checkpoint(engine, account.id)
    assert checkpoint is not None
    assert checkpoint.cursor == "100"
    assert checkpoint.consecutive_failures == 0
    assert checkpoint.last_error is None


def test_integrity_error_is_recorded_as_failure(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")

    def conflicting_process_email(*_args: Any, **_kwargs: Any) -> Any:
        raise IntegrityError(
            "INSERT INTO processed_emails",
            {},
            RuntimeError("unique"),
        )

    monkeypatch.setattr(gmail_sync, "process_email", conflicting_process_email)
    service = FakeGmailService(
        [history_response("200", message_ids=("message-1",))],
        {"message-1": message_response("message-1")},
    )

    result = sync_gmail_account(account.id, service=service)

    assert result.success is False
    assert result.error is not None
    assert "IntegrityError" in result.error
    checkpoint = read_checkpoint(engine, account.id)
    assert checkpoint is not None
    assert checkpoint.cursor == "100"
    assert checkpoint.consecutive_failures == 1


# ---------------------------------------------------------------------------
# Unknown timestamps during synchronization
# ---------------------------------------------------------------------------


def test_sync_processes_unknown_timestamp_without_blocking_known_message(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    make_rule(session, account)
    seed_checkpoint(session, account, cursor="100")
    service = FakeGmailService(
        [history_response("200", message_ids=("unknown", "known"))],
        {
            "unknown": message_response(
                "unknown",
                received_at=None,
                subject="Your order has shipped",
            ),
            "known": message_response(
                "known",
                received_at=LATE,
                subject="Your order was delivered",
            ),
        },
    )

    result = sync_gmail_account(account.id, service=service)

    assert result.success is True
    assert result.processed_count == 2
    assert result.events_created == 2
    events = session.scalars(select(StatusEvent).order_by(StatusEvent.id)).all()
    assert {event.status for event in events} == {
        ArgusStatus.SHIPPED,
        ArgusStatus.DELIVERED,
    }
    unknown_event = next(
        event for event in events if event.status is ArgusStatus.SHIPPED
    )
    known_event = next(
        event for event in events if event.status is ArgusStatus.DELIVERED
    )
    assert unknown_event.occurred_at == UNKNOWN_TIMESTAMP
    assert known_event.occurred_at == LATE
    assert read_checkpoint(engine, account.id).cursor == "200"  # type: ignore[union-attr]
