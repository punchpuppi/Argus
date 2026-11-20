"""Tests for synchronization checkpoint persistence and concurrency handling."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.database import session as session_module
from app.database.enums import ProviderName
from app.database.models import EmailAccount, SyncCheckpoint
from app.services.sync import checkpoints
from app.services.sync.checkpoints import (
    MAX_ERROR_DESCRIPTION_LENGTH,
    get_checkpoint,
    get_or_create_checkpoint,
    record_failure,
    record_success,
)
from app.services.sync.errors import CheckpointConflictError, SyncError

EARLY = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
MIDDLE = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
LATE = datetime(2026, 1, 3, 12, 0, tzinfo=UTC)


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
    email: str = "user@example.com",
) -> EmailAccount:
    account = EmailAccount(
        provider=ProviderName.GMAIL,
        email_address=email,
    )
    session.add(account)
    session.flush()
    return account


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


def count_checkpoints(engine: Engine) -> int:
    with Session(engine) as reader:
        return (
            reader.scalar(select(func.count()).select_from(SyncCheckpoint)) or 0
        )


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def test_get_checkpoint_returns_existing_checkpoint(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(
        session,
        account,
        cursor="12345",
        pending_cursor="token-1",
        provider_metadata={"historyId": "12345"},
    )

    checkpoint = get_checkpoint(account.id)

    assert checkpoint is not None
    assert checkpoint.account_id == account.id
    assert checkpoint.cursor == "12345"
    assert checkpoint.pending_cursor == "token-1"
    assert checkpoint.provider_metadata == {"historyId": "12345"}


def test_get_checkpoint_returns_none_when_absent(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    session.commit()

    assert get_checkpoint(account.id) is None
    assert count_checkpoints(engine) == 0


def test_get_checkpoint_does_not_create_rows(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    session.commit()

    get_checkpoint(account.id)
    get_checkpoint(account.id)

    assert count_checkpoints(engine) == 0


@pytest.mark.parametrize("account_id", (0, -1, True, "1", None))
def test_get_checkpoint_rejects_invalid_account_id(account_id: object) -> None:
    with pytest.raises(ValueError, match="account_id"):
        get_checkpoint(account_id)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def test_get_or_create_creates_checkpoint(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    session.commit()

    checkpoint = get_or_create_checkpoint(account.id)

    assert checkpoint.id is not None
    assert checkpoint.account_id == account.id
    assert checkpoint.cursor is None
    assert checkpoint.pending_cursor is None
    assert checkpoint.consecutive_failures == 0
    assert checkpoint.last_error is None
    assert count_checkpoints(engine) == 1


def test_get_or_create_is_idempotent(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    session.commit()

    first = get_or_create_checkpoint(account.id)
    second = get_or_create_checkpoint(account.id)

    assert first.id == second.id
    assert count_checkpoints(engine) == 1


def test_get_or_create_preserves_existing_state(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="555", pending_cursor="token-9")

    checkpoint = get_or_create_checkpoint(account.id)

    assert checkpoint.cursor == "555"
    assert checkpoint.pending_cursor == "token-9"


def test_get_or_create_rejects_missing_account(
    engine: Engine,
    scoped_sessions: sessionmaker[Session],
) -> None:
    with pytest.raises(SyncError, match="does not exist"):
        get_or_create_checkpoint(9999)

    assert count_checkpoints(engine) == 0


@pytest.mark.parametrize("account_id", (0, -1, True, "1", None))
def test_get_or_create_rejects_invalid_account_id(account_id: object) -> None:
    with pytest.raises(ValueError, match="account_id"):
        get_or_create_checkpoint(account_id)  # type: ignore[arg-type]


def test_get_or_create_handles_unique_insert_race(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost uniqueness race must re-read the winner through a new session."""

    account = make_account(session)
    session.commit()
    real_scope = session_module.session_scope
    state = {"raced": False}

    @contextmanager
    def racing_scope() -> Iterator[Session]:
        with real_scope() as scoped:
            if not state["raced"]:
                state["raced"] = True

                def commit_with_race() -> None:
                    with Session(engine) as rival:
                        rival.add(SyncCheckpoint(account_id=account.id))
                        rival.commit()
                    raise IntegrityError(
                        "INSERT INTO sync_checkpoints",
                        {},
                        RuntimeError("unique"),
                    )

                scoped.commit = commit_with_race  # type: ignore[method-assign]
            yield scoped

    monkeypatch.setattr(checkpoints, "session_scope", racing_scope)

    checkpoint = get_or_create_checkpoint(account.id)

    assert state["raced"] is True
    assert checkpoint.account_id == account.id
    assert count_checkpoints(engine) == 1


# ---------------------------------------------------------------------------
# Successful advancement
# ---------------------------------------------------------------------------


def test_record_success_advances_cursor(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")

    checkpoint = record_success(
        account.id,
        expected_cursor="100",
        new_cursor="200",
    )

    assert checkpoint.cursor == "200"
    assert checkpoint.cursor_updated_at is not None
    assert checkpoint.last_attempt_at is not None
    assert checkpoint.last_success_at is not None
    assert checkpoint.consecutive_failures == 0
    assert checkpoint.last_error is None
    assert read_checkpoint(engine, account.id).cursor == "200"  # type: ignore[union-attr]


def test_record_success_initializes_null_cursor(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account)

    checkpoint = record_success(
        account.id,
        expected_cursor=None,
        new_cursor="100",
    )

    assert checkpoint.cursor == "100"
    assert checkpoint.cursor_updated_at is not None


def test_record_success_rejects_stale_expected_cursor(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")

    with pytest.raises(CheckpointConflictError):
        record_success(account.id, expected_cursor="099", new_cursor="200")

    row = read_checkpoint(engine, account.id)
    assert row is not None
    assert row.cursor == "100"
    assert row.last_success_at is None
    assert row.cursor_updated_at is None


def test_record_success_rejects_conflict_with_null_expectation(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")

    with pytest.raises(CheckpointConflictError):
        record_success(account.id, expected_cursor=None, new_cursor="200")


def test_record_success_rejects_conflict_against_null_state(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account)

    with pytest.raises(CheckpointConflictError):
        record_success(account.id, expected_cursor="100", new_cursor="200")


@pytest.mark.parametrize("new_cursor", ("", "   ", 123))
def test_record_success_rejects_invalid_new_cursor(new_cursor: object) -> None:
    with pytest.raises(ValueError, match="new_cursor"):
        record_success(
            1,
            expected_cursor=None,
            new_cursor=new_cursor,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("expected_cursor", ("", "   ", 123))
def test_record_success_rejects_invalid_expected_cursor(
    expected_cursor: object,
) -> None:
    with pytest.raises(ValueError, match="expected_cursor"):
        record_success(
            1,
            expected_cursor=expected_cursor,  # type: ignore[arg-type]
        )


def test_record_success_treats_cursors_as_opaque(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="9")

    checkpoint = record_success(
        account.id,
        expected_cursor="9",
        new_cursor="0009",
    )

    assert checkpoint.cursor == "0009"


def test_record_success_normalizes_naive_timestamp(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")

    record_success(
        account.id,
        expected_cursor="100",
        last_seen_received_at=datetime(2026, 1, 1, 12, 0),
    )

    row = read_checkpoint(engine, account.id)
    assert row is not None
    assert row.last_seen_received_at == EARLY
    assert row.last_seen_received_at.utcoffset() is not None  # type: ignore[union-attr]


@pytest.mark.parametrize("value", ("not-a-date", 123))
def test_record_success_rejects_invalid_last_seen(value: object) -> None:
    with pytest.raises(ValueError, match="last_seen_received_at"):
        record_success(
            1,
            expected_cursor=None,
            last_seen_received_at=value,  # type: ignore[arg-type]
        )


def test_record_success_preserves_last_seen_when_omitted(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100", last_seen_received_at=EARLY)

    checkpoint = record_success(account.id, expected_cursor="100")

    assert checkpoint.last_seen_received_at == EARLY


def test_record_success_updates_last_seen_when_later(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100", last_seen_received_at=EARLY)

    checkpoint = record_success(
        account.id,
        expected_cursor="100",
        last_seen_received_at=LATE,
    )

    assert checkpoint.last_seen_received_at == LATE


def test_record_success_does_not_regress_last_seen(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100", last_seen_received_at=MIDDLE)

    checkpoint = record_success(
        account.id,
        expected_cursor="100",
        last_seen_received_at=EARLY,
    )

    assert checkpoint.last_seen_received_at == MIDDLE


def test_no_change_success_leaves_cursor_untouched(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100", cursor_updated_at=EARLY)

    checkpoint = record_success(account.id, expected_cursor="100")

    assert checkpoint.cursor == "100"
    assert checkpoint.cursor_updated_at == EARLY
    assert checkpoint.last_success_at is not None


def test_identical_cursor_is_treated_as_no_change(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100", cursor_updated_at=EARLY)

    checkpoint = record_success(
        account.id,
        expected_cursor="100",
        new_cursor="100",
    )

    assert checkpoint.cursor == "100"
    assert checkpoint.cursor_updated_at == EARLY


def test_record_success_clears_failure_state(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(
        session,
        account,
        cursor="100",
        consecutive_failures=3,
        last_error="previous failure",
    )

    checkpoint = record_success(account.id, expected_cursor="100")

    assert checkpoint.consecutive_failures == 0
    assert checkpoint.last_error is None


def test_record_success_requires_checkpoint(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    session.commit()

    with pytest.raises(SyncError, match="No synchronization checkpoint"):
        record_success(account.id, expected_cursor=None, new_cursor="100")

    assert count_checkpoints(engine) == 0


@pytest.mark.parametrize("account_id", (0, -1, True, "1", None))
def test_record_success_rejects_invalid_account_id(account_id: object) -> None:
    with pytest.raises(ValueError, match="account_id"):
        record_success(
            account_id,  # type: ignore[arg-type]
            expected_cursor=None,
        )


# ---------------------------------------------------------------------------
# Failure bookkeeping
# ---------------------------------------------------------------------------


def test_record_failure_increments_counter(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")

    checkpoint = record_failure(account.id, "network failure")

    assert checkpoint.consecutive_failures == 1
    assert checkpoint.last_attempt_at is not None
    assert checkpoint.last_error == "network failure"


def test_record_failure_increments_repeatedly(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")

    record_failure(account.id, "first")
    checkpoint = record_failure(account.id, "second")

    assert checkpoint.consecutive_failures == 2
    assert checkpoint.last_error == "second"


def test_record_failure_sanitizes_and_bounds_description(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account)

    raw = "line one\nline two\t" + ("x" * (MAX_ERROR_DESCRIPTION_LENGTH + 200))
    checkpoint = record_failure(account.id, raw)

    assert checkpoint.last_error is not None
    assert "\n" not in checkpoint.last_error
    assert "\t" not in checkpoint.last_error
    assert checkpoint.last_error.startswith("line one line two")
    assert len(checkpoint.last_error) <= MAX_ERROR_DESCRIPTION_LENGTH


def test_record_failure_accepts_exception_and_keeps_type(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account)

    checkpoint = record_failure(account.id, RuntimeError("boom\nsecond line"))

    assert checkpoint.last_error is not None
    assert checkpoint.last_error.startswith("RuntimeError: boom second line")


def test_record_failure_preserves_successful_state(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(
        session,
        account,
        cursor="100",
        cursor_updated_at=EARLY,
        last_success_at=MIDDLE,
        last_seen_received_at=LATE,
    )

    checkpoint = record_failure(account.id, "transient")

    assert checkpoint.cursor == "100"
    assert checkpoint.cursor_updated_at == EARLY
    assert checkpoint.last_success_at == MIDDLE
    assert checkpoint.last_seen_received_at == LATE
    assert checkpoint.last_attempt_at is not None


def test_record_failure_does_not_create_checkpoint(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    session.commit()

    with pytest.raises(SyncError, match="No synchronization checkpoint"):
        record_failure(account.id, "boom")

    assert count_checkpoints(engine) == 0


def test_record_failure_rejects_invalid_error_type(
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account)

    with pytest.raises(TypeError, match="error"):
        record_failure(account.id, 123)  # type: ignore[arg-type]


@pytest.mark.parametrize("account_id", (0, -1, True, "1", None))
def test_record_failure_rejects_invalid_account_id(account_id: object) -> None:
    with pytest.raises(ValueError, match="account_id"):
        record_failure(account_id, "boom")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Transaction integrity
# ---------------------------------------------------------------------------


def _failing_commit_scope(
    real_scope: Any,
    failure: BaseException,
) -> Any:
    @contextmanager
    def scope() -> Iterator[Session]:
        with real_scope() as scoped:
            def failing_commit() -> None:
                raise failure

            scoped.commit = failing_commit  # type: ignore[method-assign]
            yield scoped

    return scope


def test_failed_commit_rolls_back_advance(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")
    real_scope = session_module.session_scope
    failure = OperationalError("UPDATE sync_checkpoints", {}, RuntimeError("brk"))
    monkeypatch.setattr(
        checkpoints,
        "session_scope",
        _failing_commit_scope(real_scope, failure),
    )

    with pytest.raises(OperationalError):
        record_success(account.id, expected_cursor="100", new_cursor="200")

    row = read_checkpoint(engine, account.id)
    assert row is not None
    assert row.cursor == "100"
    assert row.cursor_updated_at is None
    assert row.last_success_at is None

    # A later operation must work on a fresh session.
    monkeypatch.setattr(checkpoints, "session_scope", real_scope)
    checkpoint = record_success(account.id, expected_cursor="100", new_cursor="200")
    assert checkpoint.cursor == "200"


def test_failed_commit_rolls_back_failure_bookkeeping(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")
    real_scope = session_module.session_scope
    failure = OperationalError("UPDATE sync_checkpoints", {}, RuntimeError("brk"))
    monkeypatch.setattr(
        checkpoints,
        "session_scope",
        _failing_commit_scope(real_scope, failure),
    )

    with pytest.raises(OperationalError):
        record_failure(account.id, "boom")

    row = read_checkpoint(engine, account.id)
    assert row is not None
    assert row.consecutive_failures == 0
    assert row.last_error is None
    assert row.last_attempt_at is None


def test_checkpoint_module_uses_no_nested_transactions() -> None:
    source = Path(checkpoints.__file__).read_text(encoding="utf-8").lower()

    assert "begin_nested" not in source
    assert "savepoint" not in source


def test_concurrent_advancement_conflict_is_detected(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
) -> None:
    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")

    observed = "100"
    record_success(account.id, expected_cursor=observed, new_cursor="200")

    with pytest.raises(CheckpointConflictError):
        record_success(account.id, expected_cursor=observed, new_cursor="300")

    row = read_checkpoint(engine, account.id)
    assert row is not None
    assert row.cursor == "200"


def test_atomic_update_rejects_cursor_advanced_after_observation(
    engine: Engine,
    session: Session,
    scoped_sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale writer must not overwrite a cursor changed before its UPDATE."""

    account = make_account(session)
    seed_checkpoint(session, account, cursor="100")
    real_scope = session_module.session_scope
    state = {"raced": False}

    @contextmanager
    def racing_scope() -> Iterator[Session]:
        with real_scope() as scoped:
            real_execute = scoped.execute

            def execute_with_race(*args: Any, **kwargs: Any) -> Any:
                if not state["raced"]:
                    state["raced"] = True
                    with Session(engine) as rival:
                        rival.execute(
                            update(SyncCheckpoint)
                            .where(SyncCheckpoint.account_id == account.id)
                            .values(cursor="999", cursor_updated_at=EARLY)
                        )
                        rival.commit()
                return real_execute(*args, **kwargs)

            scoped.execute = execute_with_race  # type: ignore[method-assign]
            yield scoped

    monkeypatch.setattr(checkpoints, "session_scope", racing_scope)

    with pytest.raises(CheckpointConflictError):
        record_success(account.id, expected_cursor="100", new_cursor="200")

    assert state["raced"] is True
    row = read_checkpoint(engine, account.id)
    assert row is not None
    assert row.cursor == "999"
    assert row.last_success_at is None
    assert row.cursor_updated_at == EARLY
