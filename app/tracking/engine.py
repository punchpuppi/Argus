"""Evaluate normalized emails against tracking rules and persist status events.

Processing is transactional and idempotent:

* The email itself is claimed once per account. Duplicates are recognised by a
  database lookup against ``processed_emails``, and a duplicate claim
  short-circuits the whole evaluation.
* A candidate status event is only inserted when no event already exists for the
  same rule, message, and status. The existing unique constraints remain the
  final guarantee: a concurrent race surfaces as an integrity error, aborting
  the caller's transaction rather than recording a partial result.
* Nothing is committed here. Callers own the transaction (for example through
  :func:`app.database.session.session_scope`), so a failure never leaves a
  partially persisted email behind.
* SAVEPOINTs are deliberately avoided. Under pysqlite's legacy transaction mode
  a ``RELEASE SAVEPOINT`` can commit the surrounding work early, which would
  defeat the atomicity this module promises.

Claim contract (permanent, per account):

* A message is claimed permanently the first time it is evaluated, whether or
  not any rule matched it. ``ProcessedEmail`` has no rule association, so a
  claim is never revisited.
* Adding a rule, reactivating a paused rule, or editing a rule does **not**
  cause previously claimed messages to be re-evaluated.
* Consequence for a future synchronization service: choose the initial backfill
  window with rule creation dates in mind, because anything claimed before a
  rule existed is invisible to it. A backfill cutoff reduces this exposure but
  does not fully solve it - for example, messages claimed while a rule was
  paused stay unprocessed after the rule is reactivated.
* Replaying history would require an explicit operational step that removes
  claims for a bounded window; it is not a behaviour of this module.

Concurrency responsibility (synchronization layer):

* This module never retries and never converts a database failure into a
  successful result. ``IntegrityError`` from a lost insert race and transient
  ``OperationalError`` (for example SQLite lock contention) propagate.
* The caller must roll back, discard the session, and start a fresh transaction
  before retrying, because the failed session cannot continue. After retrying,
  a duplicate insert is reported as ``duplicate_email=True`` rather than raised.
* Serialising writers per account avoids the race entirely.

Only metadata already present in :class:`~app.email.schemas.NormalizedEmail` is
used. Message bodies, subjects, and snippets are never written to the database
or included in errors or logs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from app.database.enums import ArgusStatus, ProviderName
from app.database.models import (
    EmailAccount,
    ProcessedEmail,
    StatusEvent,
    utcnow,
)
from app.email.schemas import NormalizedEmail
from app.tracking.errors import TrackingError
from app.tracking.matching import RuleCriteria, email_matches_rule
from app.tracking.rules import get_active_rules
from app.tracking.status import StatusDetection, detect_status

logger = logging.getLogger(__name__)

# Reasons recorded for each rule considered during a processing run.
REASON_CREATED = "created"
REASON_NO_STATUS_CUE = "no_status_cue"
REASON_STATUS_UNCHANGED = "status_unchanged"
REASON_OUT_OF_ORDER = "out_of_order"
REASON_DUPLICATE_EVENT = "duplicate_event"



@dataclass(frozen=True)
class RuleOutcome:
    """What happened for one matched rule."""

    rule_id: int
    event_id: int | None
    reason: str


@dataclass(frozen=True)
class EmailProcessingResult:
    """Summary of evaluating a single email."""

    account_id: int
    provider_message_id: str
    duplicate_email: bool
    outcomes: tuple[RuleOutcome, ...]

    @property
    def matched_rule_ids(self) -> tuple[int, ...]:
        """Identifiers of the rules the email matched, in evaluation order."""

        return tuple(outcome.rule_id for outcome in self.outcomes)

    @property
    def created_event_ids(self) -> tuple[int, ...]:
        """Identifiers of the status events created during this run.

        The values are assigned when rows are flushed, not committed. They are
        only meaningful after the caller's transaction commits successfully;
        after a rollback they must be discarded, because the database may reuse
        the identifiers for later inserts.
        """

        return tuple(
            outcome.event_id
            for outcome in self.outcomes
            if outcome.event_id is not None
        )


def _load_account(
    session: Session,
    account_id: int,
    provider: ProviderName,
) -> EmailAccount:
    """Return the account if it may be processed, otherwise raise."""

    account = session.get(EmailAccount, account_id)
    if account is None:
        raise TrackingError(f"Email account {account_id} does not exist.")
    if account.deleted_at is not None or not account.is_active:
        raise TrackingError(f"Email account {account_id} is not active for tracking.")
    if account.provider != provider:
        raise TrackingError(
            f"Email provider {provider.value!r} does not match account "
            f"{account_id} provider {account.provider.value!r}."
        )
    return account


def _normalize_timestamp(value: datetime | None) -> datetime:
    """Return an aware UTC timestamp for event ordering and storage.

    Naive values are treated as UTC, matching the convention documented by
    :class:`~app.database.types.UTCDateTime`, so a provider adapter that returns
    naive datetimes cannot trigger a naive-versus-aware comparison error.
    """

    if value is None:
        return utcnow()
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _claim_email(
    session: Session,
    account_id: int,
    message_id: str,
    email: NormalizedEmail,
) -> bool:
    """Record the email as processed, returning ``False`` when already claimed.

    The claim is permanent and per account: adding or reactivating rules later
    does not re-evaluate this message. The lookup and the insert stay inside the
    caller's transaction, so a failure elsewhere in the run also removes it.
    """

    already_claimed = session.scalar(
        select(ProcessedEmail.id)
        .where(
            ProcessedEmail.account_id == account_id,
            ProcessedEmail.provider_message_id == message_id,
        )
        .limit(1)
    )
    if already_claimed is not None:
        return False

    session.add(
        ProcessedEmail(
            account_id=account_id,
            provider_message_id=message_id,
            internet_message_id=email.internet_message_id,
            thread_id=email.thread_id,
            received_at=email.received_at,
        )
    )
    session.flush()
    return True


def _event_exists(
    session: Session,
    rule_id: int,
    provider_message_id: str,
    status: ArgusStatus,
) -> bool:
    """Return whether this exact rule/message/status event already exists."""

    existing = session.scalar(
        select(StatusEvent.id)
        .where(
            StatusEvent.rule_id == rule_id,
            StatusEvent.provider_message_id == provider_message_id,
            StatusEvent.status == status,
        )
        .limit(1)
    )
    return existing is not None


def _latest_event(session: Session, rule_id: int) -> StatusEvent | None:
    """Return the most recent status event for a rule, if any."""

    statement = (
        select(StatusEvent)
        .where(StatusEvent.rule_id == rule_id)
        .order_by(StatusEvent.occurred_at.desc(), StatusEvent.id.desc())
        .options(
            load_only(
                StatusEvent.id,
                StatusEvent.status,
                StatusEvent.occurred_at,
            )
        )
        .limit(1)
    )
    return session.scalars(statement).first()


def _record_event(
    session: Session,
    rule_id: int,
    message_id: str,
    email: NormalizedEmail,
    detection: StatusDetection,
    previous_status: ArgusStatus | None,
    occurred_at: datetime,
) -> int | None:
    """Insert a status event, returning ``None`` when it already exists."""

    if _event_exists(
        session,
        rule_id,
        message_id,
        detection.status,
    ):
        return None

    event = StatusEvent(
        rule_id=rule_id,
        provider_message_id=message_id,
        internet_message_id=email.internet_message_id,
        previous_status=previous_status,
        status=detection.status,
        matched_cue=detection.matched_cue,
        occurred_at=occurred_at,
    )
    session.add(event)
    session.flush()
    return event.id


def process_email(
    session: Session,
    account_id: int,
    email: NormalizedEmail,
) -> EmailProcessingResult:
    """Evaluate one normalized email against an account's active rules.

    The caller is responsible for committing or rolling back the surrounding
    transaction. Re-processing an email that was already claimed returns a
    result with ``duplicate_email`` set and creates no events.

    The claim is permanent and per account: adding or reactivating a rule does
    not cause previously claimed messages to be evaluated again. A caller that
    wants a specific backfill window must decide it before messages are claimed.

    Database failures are propagated, not retried. On ``IntegrityError`` or a
    transient ``OperationalError`` the caller must roll back, discard this
    session, and retry the email with a fresh transaction.
    """

    raw_message_id = email.provider_message_id
    if not isinstance(raw_message_id, str) or not raw_message_id.strip():
        raise TrackingError("A provider message identifier is required for tracking.")

    # One normalised identifier is used for the claim, the lookup, the stored
    # row, and the result, so a padded retry cannot slip past deduplication.
    message_id = raw_message_id.strip()

    _load_account(session, account_id, email.provider)

    if not _claim_email(session, account_id, message_id, email):
        logger.debug(
            "Skipping already-processed email %s for account %s",
            message_id,
            account_id,
        )
        return EmailProcessingResult(
            account_id=account_id,
            provider_message_id=message_id,
            duplicate_email=True,
            outcomes=(),
        )

    occurred_at = _normalize_timestamp(email.received_at)
    detection = detect_status(email)
    outcomes: list[RuleOutcome] = []

    for rule in get_active_rules(session, account_id):
        criteria = RuleCriteria.from_rule(rule)
        if not email_matches_rule(criteria, email):
            continue

        if detection is None:
            outcomes.append(
                RuleOutcome(rule_id=rule.id, event_id=None, reason=REASON_NO_STATUS_CUE)
            )
            continue

        latest = _latest_event(session, rule.id)
        previous_status = latest.status if latest is not None else None

        if latest is not None:
            if latest.status == detection.status:
                outcomes.append(
                    RuleOutcome(
                        rule_id=rule.id,
                        event_id=None,
                        reason=REASON_STATUS_UNCHANGED,
                    )
                )
                continue
            if latest.occurred_at is not None and occurred_at < latest.occurred_at:
                outcomes.append(
                    RuleOutcome(
                        rule_id=rule.id,
                        event_id=None,
                        reason=REASON_OUT_OF_ORDER,
                    )
                )
                continue

        event_id = _record_event(
            session,
            rule.id,
            message_id,
            email,
            detection,
            previous_status,
            occurred_at,
        )
        outcomes.append(
            RuleOutcome(
                rule_id=rule.id,
                event_id=event_id,
                reason=REASON_CREATED if event_id is not None else REASON_DUPLICATE_EVENT,
            )
        )

    created = sum(1 for outcome in outcomes if outcome.event_id is not None)
    logger.info(
        "Evaluated email %s for account %s across %d matched rule(s); %d event(s) created",
        message_id,
        account_id,
        len(outcomes),
        created,
    )

    return EmailProcessingResult(
        account_id=account_id,
        provider_message_id=message_id,
        duplicate_email=False,
        outcomes=tuple(outcomes),
    )
