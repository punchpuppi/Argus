"""Incremental Gmail synchronization service.

This module connects the existing Gmail History client, Gmail metadata
retrieval, the tracking engine, and the checkpoint store into one reliable
incremental run. It deliberately owns only orchestration:

* A run starts from an existing account and an existing checkpoint. Accounts
  and checkpoints are never provisioned here.
* History is read page by page from the checkpoint cursor. Only
  ``messagesAdded`` identifiers exposed by :func:`list_history` are collected,
  deduplicated across pages, and then resolved to metadata. Full message bodies
  are never requested or stored.
* All Gmail API calls happen before any database write. No session or
  transaction is held open while the provider is queried.
* Each message is evaluated through :func:`app.tracking.engine.process_email`
  in its own short transaction. The tracking engine's duplicate-claim ledger
  makes replaying a partially completed run safe.
* The checkpoint is advanced in a separate transaction only after the final
  history page and every message have been processed successfully. The cursor
  from the final history response is the only candidate; intermediate page
  cursors are never persisted.
* Failures are recorded with :func:`record_failure`, which preserves the last
  successful cursor and only updates failure bookkeeping. Provider and tracking
  errors are recorded through their existing sanitized messages.
* An in-process, per-account lock prevents overlapping runs in the current
  process. It is not a distributed lock; optimistic checkpoint advancement via
  ``expected_cursor`` remains the cross-process conflict detector.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError, OperationalError

from app.database.enums import ProviderName
from app.database.models import EmailAccount
from app.database.session import session_scope
from app.email.schemas import NormalizedEmail
from app.providers.gmail.auth import GmailAuthError, get_credentials
from app.providers.gmail.client import GmailApiError, build_gmail_service
from app.providers.gmail.history import GmailHistoryExpiredError, list_history
from app.providers.gmail.messages import get_message_metadata
from app.services.sync.checkpoints import (
    get_checkpoint,
    record_failure,
    record_success,
)
from app.services.sync.errors import (
    CheckpointConflictError,
    SyncAlreadyRunningError,
    SyncError,
    SyncPaginationError,
)
from app.tracking.engine import process_email
from app.tracking.errors import TrackingError
from config import get_settings

logger = logging.getLogger(__name__)

#: Process-local locks keyed by account identifier.
_account_locks: dict[int, threading.Lock] = {}
_account_locks_guard = threading.Lock()


@dataclass(frozen=True)
class SyncResult:
    """Outcome of one incremental synchronization attempt.

    ``processed_count`` counts every message in the deduplicated history run,
    including messages the tracking engine recognized as already claimed.
    ``duplicate_count`` is the subset that was already claimed, and
    ``events_created`` counts new status events. On failure the cursor is not
    advanced and ``error`` contains the sanitized description stored by the
    checkpoint store.
    """

    account_id: int
    success: bool
    message_ids: tuple[str, ...] = ()
    processed_count: int = 0
    duplicate_count: int = 0
    events_created: int = 0
    history_id: str | None = None
    error: str | None = None
    history_expired: bool = False
    conflict: bool = False


@dataclass(frozen=True)
class _AccountSnapshot:
    """Detached account fields needed to validate a sync run."""

    id: int
    provider: ProviderName
    is_active: bool
    deleted_at: datetime | None


def _validate_account_id(account_id: int) -> int:
    """Validate the account identifier accepted by the public entry point."""

    if (
        not isinstance(account_id, int)
        or isinstance(account_id, bool)
        or account_id <= 0
    ):
        raise ValueError("account_id must be a positive integer.")
    return account_id


def _lock_for(account_id: int) -> threading.Lock:
    """Return the process-local lock for an account, creating it on demand."""

    with _account_locks_guard:
        lock = _account_locks.get(account_id)
        if lock is None:
            lock = threading.Lock()
            _account_locks[account_id] = lock
        return lock


def _load_account(account_id: int) -> _AccountSnapshot:
    """Load an existing account through a short read transaction.

    Accounts are never created here. A missing account is a caller error and is
    raised before any failure bookkeeping can be attempted.
    """

    with session_scope() as session:
        account = session.get(EmailAccount, account_id)
        if account is None:
            raise SyncError(f"Email account {account_id} does not exist.")
        return _AccountSnapshot(
            id=account.id,
            provider=account.provider,
            is_active=account.is_active,
            deleted_at=account.deleted_at,
        )


def _as_utc(value: datetime) -> datetime:
    """Return an aware UTC timestamp, treating naive input as UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _order_emails(emails: tuple[NormalizedEmail, ...]) -> tuple[NormalizedEmail, ...]:
    """Return emails in ascending received time, preserving ties.

    Messages without a received timestamp sort last. The sort is stable, so the
    history encounter order remains the tie-breaker for equal timestamps.
    """

    def sort_key(email: NormalizedEmail) -> tuple[int, datetime]:
        received_at = email.received_at
        if received_at is None:
            return (1, datetime.min.replace(tzinfo=UTC))
        return (0, _as_utc(received_at))

    return tuple(sorted(emails, key=sort_key))


def _collect_history(
    service: Any,
    *,
    start_history_id: str,
    page_size: int | None,
) -> tuple[tuple[str, ...], str]:
    """Collect deduplicated message identifiers across every history page.

    Returns the identifiers in encounter order and the ``historyId`` from the
    final page. Repeated page tokens are rejected to guarantee termination and
    to avoid re-reading a page indefinitely.
    """

    message_ids: list[str] = []
    seen_message_ids: set[str] = set()
    seen_page_tokens: set[str] = set()
    page_token: str | None = None
    final_history_id: str | None = None

    while True:
        page = list_history(
            service,
            start_history_id=start_history_id,
            page_size=page_size,
            page_token=page_token,
        )
        final_history_id = page.history_id

        for message_id in page.message_ids:
            if message_id in seen_message_ids:
                continue
            seen_message_ids.add(message_id)
            message_ids.append(message_id)

        next_page_token = page.next_page_token
        if not next_page_token:
            break
        if next_page_token in seen_page_tokens:
            raise SyncPaginationError(
                "Gmail history pagination returned a repeated page token; "
                "aborting synchronization without advancing the checkpoint."
            )
        seen_page_tokens.add(next_page_token)
        page_token = next_page_token

    if final_history_id is None:  # pragma: no cover - list_history guarantees this
        raise SyncError("Gmail history did not report a usable history identifier.")

    return tuple(message_ids), final_history_id


def _fetch_metadata(
    service: Any,
    message_ids: tuple[str, ...],
) -> tuple[NormalizedEmail, ...]:
    """Retrieve metadata-only normalized emails for the collected identifiers."""

    return tuple(
        get_message_metadata(service, message_id) for message_id in message_ids
    )


def _process_emails(
    account_id: int,
    emails: tuple[NormalizedEmail, ...],
) -> tuple[int, int, int, datetime | None]:
    """Process emails in order, one short transaction per message.

    Returns ``(processed, duplicates, events_created, last_seen_received_at)``.
    The tracking engine owns idempotency inside each transaction; a crash after
    some messages are committed is safe because a later replay reports them as
    duplicates instead of creating new claims or events.
    """

    processed = 0
    duplicates = 0
    events_created = 0
    last_seen_received_at: datetime | None = None

    for email in emails:
        with session_scope() as session:
            result = process_email(session, account_id, email)

        processed += 1
        if result.duplicate_email:
            duplicates += 1
        events_created += len(result.created_event_ids)

        if email.received_at is not None:
            received_at = _as_utc(email.received_at)
            if (
                last_seen_received_at is None
                or received_at > last_seen_received_at
            ):
                last_seen_received_at = received_at

    return processed, duplicates, events_created, last_seen_received_at


def _resolve_service(service: Any | None) -> Any:
    """Return an injected Gmail service or build one from stored credentials."""

    if service is not None:
        return service
    settings = get_settings()
    return build_gmail_service(get_credentials(settings, interactive=False))


def _is_transient_sqlite_lock_error(exc: OperationalError) -> bool:
    """Return whether an OperationalError is SQLite lock contention.

    Only ``SQLITE_BUSY`` and ``SQLITE_LOCKED``, including their extended
    codes, are treated as transient. Permanent failures such as missing
    tables, I/O errors, and read-only databases must propagate.
    """

    original = exc.orig
    if not isinstance(original, sqlite3.OperationalError):
        return False

    code = getattr(original, "sqlite_errorcode", None)
    if isinstance(code, int) and not isinstance(code, bool):
        # Extended codes share the low byte with their primary code, for
        # example SQLITE_BUSY_SNAPSHOT (517) and SQLITE_LOCKED_SHAREDCACHE (262).
        primary_code = code & 0xFF
        return primary_code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)

    name = getattr(original, "sqlite_errorname", None)
    if isinstance(name, str):
        return name.startswith("SQLITE_BUSY") or name.startswith("SQLITE_LOCKED")

    return False


def _failure_result(
    account_id: int,
    error: str | BaseException,
    *,
    history_expired: bool = False,
    conflict: bool = False,
) -> SyncResult:
    """Record failure bookkeeping and return a sanitized failed result."""

    checkpoint = record_failure(account_id, error)
    description = checkpoint.last_error
    logger.warning(
        "Gmail synchronization failed for account %s: %s",
        account_id,
        description,
    )
    return SyncResult(
        account_id=account_id,
        success=False,
        error=description,
        history_expired=history_expired,
        conflict=conflict,
    )


def _sync_locked(
    account_id: int,
    *,
    service: Any | None,
    page_size: int | None,
) -> SyncResult:
    """Perform one synchronization while the caller holds the account lock."""

    account = _load_account(account_id)
    checkpoint = get_checkpoint(account_id)
    if checkpoint is None:
        raise SyncError(
            f"No synchronization checkpoint exists for account {account_id}."
        )

    if account.provider is not ProviderName.GMAIL:
        return _failure_result(
            account_id,
            SyncError(
                f"Email account {account_id} is not a Gmail account and cannot "
                "be synchronized by the Gmail service."
            ),
        )
    if not account.is_active or account.deleted_at is not None:
        return _failure_result(
            account_id,
            SyncError(
                f"Email account {account_id} is not active for synchronization."
            ),
        )

    start_history_id = checkpoint.cursor
    if not start_history_id:
        return _failure_result(
            account_id,
            SyncError(
                f"Synchronization checkpoint for account {account_id} has no "
                "history cursor."
            ),
        )

    try:
        resolved_service = _resolve_service(service)
        message_ids, final_history_id = _collect_history(
            resolved_service,
            start_history_id=start_history_id,
            page_size=page_size,
        )
        emails = _fetch_metadata(resolved_service, message_ids)
    except GmailHistoryExpiredError as exc:
        return _failure_result(account_id, exc, history_expired=True)
    except SyncPaginationError as exc:
        return _failure_result(account_id, exc)
    except GmailAuthError as exc:
        return _failure_result(account_id, exc)
    except GmailApiError as exc:
        return _failure_result(account_id, exc)

    ordered_emails = _order_emails(emails)

    try:
        processed, duplicates, events_created, last_seen = _process_emails(
            account_id,
            ordered_emails,
        )
    except TrackingError as exc:
        return _failure_result(account_id, exc)
    except IntegrityError as exc:
        return _failure_result(
            account_id,
            "Database integrity failure while processing synchronized email "
            f"metadata ({type(exc).__name__}). The next synchronization will "
            "retry.",
        )
    except OperationalError as exc:
        if not _is_transient_sqlite_lock_error(exc):
            raise
        return _failure_result(
            account_id,
            "The database was locked while processing synchronized email "
            "metadata (SQLITE_BUSY/SQLITE_LOCKED). The next synchronization "
            "will retry.",
        )

    try:
        record_success(
            account_id,
            expected_cursor=start_history_id,
            new_cursor=final_history_id,
            last_seen_received_at=last_seen,
        )
    except CheckpointConflictError as exc:
        return _failure_result(account_id, exc, conflict=True)

    return SyncResult(
        account_id=account_id,
        success=True,
        message_ids=message_ids,
        processed_count=processed,
        duplicate_count=duplicates,
        events_created=events_created,
        history_id=final_history_id,
    )


def sync_gmail_account(
    account_id: int,
    *,
    service: Any | None = None,
    page_size: int | None = None,
) -> SyncResult:
    """Run one incremental synchronization for an existing Gmail account.

    ``service`` may inject an authenticated Gmail client (used by tests and by
    callers that already own a client). When omitted, a client is built from the
    stored OAuth credentials using the non-interactive path.

    The function never creates accounts or checkpoints. Missing preconditions
    raise :class:`SyncError`; provider, tracking, pagination, and checkpoint
    conflict failures are recorded through the checkpoint store and returned as
    an unsuccessful :class:`SyncResult`. A failure while advancing the
    checkpoint itself is allowed to propagate so the caller can retry; the
    cursor remains at its previous value and the next run safely replays.
    """

    account_id = _validate_account_id(account_id)
    lock = _lock_for(account_id)
    if not lock.acquire(blocking=False):
        raise SyncAlreadyRunningError(
            f"A synchronization is already running for account {account_id} "
            "in this process."
        )

    try:
        return _sync_locked(account_id, service=service, page_size=page_size)
    finally:
        lock.release()
