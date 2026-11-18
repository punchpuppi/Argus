"""Read-only retrieval of Gmail mailbox history records.

The Gmail History API reports changes to a mailbox since a supplied
``startHistoryId``. This module implements a single-page request only: it does
not own synchronization state, does not advance checkpoints, and does not loop
over pages.

Design notes:

* Only ``messagesAdded`` records are treated as newly received email. Label
  changes and deletions are deliberately ignored.
* History responses are chronological, but history identifiers are not
  contiguous. Nothing here infers, compares, or persists a checkpoint.
* History records are only retained for a limited period. Gmail returns HTTP
  404 when ``startHistoryId`` is invalid or too old; that specific case is
  surfaced as :class:`GmailHistoryExpiredError` so a later synchronization
  milestone can start a full resynchronization.
* Authentication (401), permission (403), rate-limit, and transient failures
  are never classified as history expiration.
* Retries and error sanitisation are shared with message retrieval through
  :mod:`app.providers.gmail.transport`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.providers.gmail.client import GmailApiError
from app.providers.gmail.transport import (
    GmailNotFoundError,
    execute_request,
    validate_page_size,
)
from config import get_settings

HISTORY_LIST_OPERATION = "history list request"


class GmailHistoryExpiredError(GmailNotFoundError):
    """Raised when the supplied history cursor is no longer usable.

    Gmail documents an HTTP 404 for an invalid or out-of-date
    ``startHistoryId``; the caller should perform a full synchronization.
    """


@dataclass(frozen=True)
class GmailHistoryPage:
    """One page of mailbox changes.

    ``message_ids`` contains only messages reported as added, deduplicated
    within this page in chronological encounter order. ``history_id`` is the
    mailbox position reported by this response; callers must not treat it as a
    committed checkpoint until every message on every page is processed.
    """

    message_ids: tuple[str, ...]
    history_id: str
    next_page_token: str | None = None


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def _require_history_id(value: Any) -> str:
    """Return a validated top-level ``historyId`` or raise."""

    if not isinstance(value, str) or not value.strip():
        raise GmailApiError(
            "Gmail API history response did not include a valid historyId."
        )
    return value.strip()


def _message_ids_from_records(records: Any) -> tuple[str, ...]:
    """Collect added-message identifiers from history records in order.

    Only ``messagesAdded`` entries are considered. Records for label changes
    and deletions are skipped, and a record without ``messagesAdded`` is a
    valid no-op.
    """

    if records is None:
        return ()
    if not isinstance(records, list):
        raise GmailApiError(
            "Gmail API returned an unexpected history records payload."
        )

    message_ids: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            raise GmailApiError(
                "Gmail API returned a malformed history record."
            )

        added = record.get("messagesAdded")
        if added is None:
            continue
        if not isinstance(added, list):
            raise GmailApiError(
                "Gmail API returned a malformed messagesAdded payload."
            )

        for entry in added:
            if not isinstance(entry, dict):
                raise GmailApiError(
                    "Gmail API returned a malformed messagesAdded entry."
                )
            message = entry.get("message")
            if not isinstance(message, dict):
                raise GmailApiError(
                    "Gmail API returned a messagesAdded entry without a message."
                )
            message_id = message.get("id")
            if not isinstance(message_id, str) or not message_id.strip():
                raise GmailApiError(
                    "Gmail API returned a message without a usable id."
                )
            message_ids.append(message_id.strip())

    # Preserve chronological encounter order while removing repeats.
    return tuple(dict.fromkeys(message_ids))


def _parse_history_page(data: Any) -> GmailHistoryPage:
    """Validate and convert a raw history response into a typed page."""

    if not isinstance(data, dict):
        raise GmailApiError(
            "Gmail API returned an unexpected history response."
        )

    history_id = _require_history_id(data.get("historyId"))
    message_ids = _message_ids_from_records(data.get("history"))

    next_page_token = data.get("nextPageToken")
    if next_page_token is not None:
        if not isinstance(next_page_token, str):
            raise GmailApiError(
                "Gmail API returned a malformed pagination token."
            )
        next_page_token = next_page_token.strip() or None

    return GmailHistoryPage(
        message_ids=message_ids,
        history_id=history_id,
        next_page_token=next_page_token,
    )


def _validate_request_token(page_token: Any) -> str | None:
    """Validate a request pagination token, treating blanks as absent."""

    if page_token is None:
        return None
    if not isinstance(page_token, str):
        raise ValueError("page_token must be a string.")
    cleaned = page_token.strip()
    return cleaned or None


def list_history(
    service: Any,
    *,
    start_history_id: str,
    page_size: int | None = None,
    page_token: str | None = None,
) -> GmailHistoryPage:
    """Retrieve one page of mailbox history after ``start_history_id``.

    ``start_history_id`` is required. ``page_size`` defaults to the configured
    Gmail page size and must be within the API limit. ``page_token`` resumes a
    previous page; the caller is responsible for storing tokens between calls.
    """

    if not isinstance(start_history_id, str) or not start_history_id.strip():
        raise ValueError("start_history_id must not be empty.")

    effective_page_size = (
        page_size if page_size is not None else get_settings().gmail_page_size
    )
    validate_page_size(effective_page_size)

    request_kwargs: dict[str, Any] = {
        "userId": "me",
        "startHistoryId": start_history_id.strip(),
        "maxResults": effective_page_size,
    }
    request_page_token = _validate_request_token(page_token)
    if request_page_token:
        request_kwargs["pageToken"] = request_page_token

    request = service.users().history().list(**request_kwargs)

    expired = False
    data: Any = None
    try:
        data = execute_request(request, HISTORY_LIST_OPERATION)
    except GmailNotFoundError:
        # Raised outside the handler so no cause/context chain is attached.
        expired = True

    if expired:
        raise GmailHistoryExpiredError(
            "Gmail history is no longer available for the supplied "
            "startHistoryId (HTTP 404). A full synchronization is required."
        )

    return _parse_history_page(data)
