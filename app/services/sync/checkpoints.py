"""Persistence operations for per-account synchronization checkpoints.

Ownership and guarantees:

* Every public operation owns one short transaction through
  :func:`app.database.session.session_scope`. Callers do not manage sessions.
* Returned :class:`~app.database.models.SyncCheckpoint` instances are detached
  snapshots. Scalar attributes are safe to read after the call; relationships
  are not loaded and must not be accessed.
* Cursor values are opaque strings. They are stored and compared for identity
  only; this module never orders or parses them.
* Cursor advancement is optimistic: the caller supplies the cursor it last
  observed, and the update is rejected when the stored state differs. This is
  conflict detection, not a lock and not cross-process exclusivity.
* Nested transactions are deliberately not used, matching the transaction
  contract established by the tracking engine.
* No email content, provider payloads, credentials, or tokens are stored here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import (
    EmailAccount,
    SyncCheckpoint,
    utcnow,
)
from app.database.session import session_scope
from app.services.sync.errors import CheckpointConflictError, SyncError

#: Upper bound for a persisted failure description.
MAX_ERROR_DESCRIPTION_LENGTH = 500

_FALLBACK_ERROR_DESCRIPTION = "Synchronization failed without a description."


def _validate_account_id(account_id: int) -> int:
    """Validate an account identifier used by every public operation."""

    if (
        not isinstance(account_id, int)
        or isinstance(account_id, bool)
        or account_id <= 0
    ):
        raise ValueError("account_id must be a positive integer.")
    return account_id


def _normalize_expected_cursor(expected_cursor: str | None) -> str | None:
    """Validate an expected cursor, allowing ``None`` for the initial state."""

    if expected_cursor is None:
        return None
    if not isinstance(expected_cursor, str):
        raise ValueError("expected_cursor must be a string or None.")
    cleaned = expected_cursor.strip()
    if not cleaned:
        raise ValueError("expected_cursor must not be blank.")
    return cleaned


def _normalize_new_cursor(new_cursor: str) -> str:
    """Validate a cursor supplied for storage."""

    if not isinstance(new_cursor, str):
        raise ValueError("new_cursor must be a string.")
    cleaned = new_cursor.strip()
    if not cleaned:
        raise ValueError("new_cursor must not be blank.")
    return cleaned


def _normalize_timestamp(value: object) -> datetime:
    """Return an aware UTC datetime, treating naive input as UTC."""

    if not isinstance(value, datetime):
        raise ValueError("last_seen_received_at must be a datetime.")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _sanitize_error_description(error: str | BaseException) -> str:
    """Return a single-line, bounded description for persistence.

    Control characters are flattened to spaces so a message cannot inject log
    lines, and the result is truncated to a fixed bound. The caller remains
    responsible for not supplying sensitive content in the first place.
    """

    if isinstance(error, BaseException):
        text = f"{type(error).__name__}: {error}"
    elif isinstance(error, str):
        text = error
    else:
        raise TypeError("error must be a string or an exception.")

    flattened = "".join(
        character if character.isprintable() else " " for character in text
    )
    cleaned = " ".join(flattened.split())
    if not cleaned:
        return _FALLBACK_ERROR_DESCRIPTION
    return cleaned[:MAX_ERROR_DESCRIPTION_LENGTH]


def _load_checkpoint(
    session: Session,
    account_id: int,
) -> SyncCheckpoint | None:
    """Return the account's checkpoint within the given session, if any."""

    return session.scalar(
        select(SyncCheckpoint).where(SyncCheckpoint.account_id == account_id)
    )


def _require_checkpoint(session: Session, account_id: int) -> SyncCheckpoint:
    """Return the account's checkpoint or raise a sanitized error."""

    checkpoint = _load_checkpoint(session, account_id)
    if checkpoint is None:
        raise SyncError(
            f"No synchronization checkpoint exists for account {account_id}."
        )
    return checkpoint


def _require_account(session: Session, account_id: int) -> EmailAccount:
    """Return the account or raise a sanitized error."""

    account = session.get(EmailAccount, account_id)
    if account is None:
        raise SyncError(f"Email account {account_id} does not exist.")
    return account


def _ensure_expected_cursor(
    checkpoint: SyncCheckpoint,
    expected_cursor: str | None,
    account_id: int,
) -> None:
    """Raise when the stored cursor differs from the caller's expectation."""

    if checkpoint.cursor != expected_cursor:
        raise CheckpointConflictError(
            "The synchronization checkpoint for account "
            f"{account_id} changed since it was read."
        )


def get_checkpoint(account_id: int) -> SyncCheckpoint | None:
    """Return the account's checkpoint, or ``None``.

    This is a read-only operation: it never creates a checkpoint, and a missing
    account is indistinguishable from an account without a checkpoint.
    """

    account_id = _validate_account_id(account_id)

    with session_scope() as session:
        return _load_checkpoint(session, account_id)


def get_or_create_checkpoint(account_id: int) -> SyncCheckpoint:
    """Return the account's checkpoint, creating it when absent.

    The account must exist. Creation is safe under a concurrent insert race:
    the failed transaction is rolled back and the winner's checkpoint is read
    through a fresh session, so repeated calls return one logical checkpoint.
    """

    account_id = _validate_account_id(account_id)

    try:
        with session_scope() as session:
            _require_account(session, account_id)
            checkpoint = _load_checkpoint(session, account_id)
            if checkpoint is not None:
                return checkpoint

            checkpoint = SyncCheckpoint(account_id=account_id)
            session.add(checkpoint)
            return checkpoint
    except IntegrityError:
        # The insert lost a uniqueness race (or the account disappeared).
        # session_scope has already rolled back and closed that session.
        pass

    with session_scope() as session:
        checkpoint = _load_checkpoint(session, account_id)

    if checkpoint is None:
        raise SyncError(
            "Could not create a synchronization checkpoint for account "
            f"{account_id}."
        )
    return checkpoint


def record_success(
    account_id: int,
    *,
    expected_cursor: str | None,
    new_cursor: str | None = None,
    last_seen_received_at: datetime | None = None,
) -> SyncCheckpoint:
    """Record a successful synchronization, optionally advancing the cursor.

    ``expected_cursor`` is the cursor the caller last observed (``None`` for an
    uninitialized checkpoint). The update is rejected with
    :class:`~app.services.sync.errors.CheckpointConflictError` when the stored
    cursor differs.

    When ``new_cursor`` is omitted the cursor and ``cursor_updated_at`` are left
    untouched, which represents a successful run that found no new upstream
    position. Supplying a cursor identical to the stored value is also treated
    as no change.

    ``last_seen_received_at`` is only applied when supplied, and never moves
    backwards: the stored value is the newest message the checkpoint covers.
    """

    account_id = _validate_account_id(account_id)
    expected = _normalize_expected_cursor(expected_cursor)
    candidate = None if new_cursor is None else _normalize_new_cursor(new_cursor)
    seen_at = (
        None if last_seen_received_at is None
        else _normalize_timestamp(last_seen_received_at)
    )

    with session_scope() as session:
        checkpoint = _require_checkpoint(session, account_id)
        _ensure_expected_cursor(checkpoint, expected, account_id)

        now = utcnow()
        checkpoint.last_attempt_at = now
        checkpoint.last_success_at = now
        checkpoint.consecutive_failures = 0
        checkpoint.last_error = None

        if candidate is not None and candidate != checkpoint.cursor:
            checkpoint.cursor = candidate
            checkpoint.cursor_updated_at = now

        if seen_at is not None:
            if (
                checkpoint.last_seen_received_at is None
                or seen_at > checkpoint.last_seen_received_at
            ):
                checkpoint.last_seen_received_at = seen_at

        return checkpoint


def record_failure(account_id: int, error: str | BaseException) -> SyncCheckpoint:
    """Record a failed synchronization attempt for an existing checkpoint.

    Only failure bookkeeping changes: ``last_attempt_at`` is updated,
    ``consecutive_failures`` is incremented, and ``last_error`` stores a
    sanitized, bounded description. The cursor, ``cursor_updated_at``,
    ``last_success_at``, and ``last_seen_received_at`` are preserved.

    A missing checkpoint raises :class:`~app.services.sync.errors.SyncError`;
    failures never create checkpoint rows.
    """

    account_id = _validate_account_id(account_id)
    description = _sanitize_error_description(error)

    with session_scope() as session:
        checkpoint = _require_checkpoint(session, account_id)
        checkpoint.last_attempt_at = utcnow()
        checkpoint.consecutive_failures += 1
        checkpoint.last_error = description
        return checkpoint
