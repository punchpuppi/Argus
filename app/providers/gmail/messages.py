"""Read-only Gmail message listing and metadata normalization.

Only Gmail's ``metadata`` message format is requested. Full message bodies are
never fetched or stored by this module.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import formataddr, getaddresses
from typing import Any

from google.auth.exceptions import TransportError
from googleapiclient.errors import HttpError
from httplib2 import HttpLib2Error

from app.database.enums import ProviderName
from app.email.schemas import NormalizedEmail
from app.providers.gmail.client import GmailApiError
from config import get_settings

GMAIL_MAX_PAGE_SIZE = 500
GMAIL_METADATA_HEADERS: tuple[str, ...] = (
    "Message-ID",
    "From",
    "To",
    "Cc",
    "Subject",
)

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_TRANSIENT_EXCEPTIONS = (
    HttpLib2Error,
    TimeoutError,
    ConnectionError,
    TransportError,
)
_MAX_ATTEMPTS = 3
_RETRY_DELAYS = (0.5, 1.0)


@dataclass(frozen=True)
class GmailMessagePage:
    """One page of Gmail message identifiers."""

    message_ids: tuple[str, ...]
    next_page_token: str | None = None


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def _header_values(headers: Any, header_name: str) -> list[str]:
    if not isinstance(headers, list):
        return []

    target = header_name.lower()
    values: list[str] = []
    for header in headers:
        if not isinstance(header, dict):
            continue
        name = header.get("name")
        value = header.get("value")
        if (
            isinstance(name, str)
            and name.lower() == target
            and isinstance(value, str)
        ):
            cleaned = value.strip()
            if cleaned:
                values.append(cleaned)
    return values


def _first_header(headers: Any, header_name: str) -> str | None:
    values = _header_values(headers, header_name)
    return values[0] if values else None


def _message_headers(data: dict[str, Any]) -> list[Any]:
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return []
    headers = payload.get("headers")
    return headers if isinstance(headers, list) else []


def _parse_recipients(to_values: list[str], cc_values: list[str]) -> tuple[str, ...]:
    recipients: list[str] = []
    for value in (*to_values, *cc_values):
        addresses = getaddresses([value])
        parsed_any = False
        for name, address in addresses:
            if not address:
                continue
            try:
                formatted = formataddr((name, address)) if name else address
            except (TypeError, ValueError, UnicodeError):
                formatted = address or value
            recipients.append(formatted)
            parsed_any = True
        if not parsed_any:
            recipients.append(value)

    return tuple(dict.fromkeys(recipients))


def _parse_internal_date(value: Any) -> datetime | None:
    if isinstance(value, bool):
        return None

    try:
        milliseconds = int(value)
    except (TypeError, ValueError, OverflowError):
        return None

    try:
        return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_labels(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()

    return tuple(
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    )


def _http_status(exc: HttpError) -> Any:
    response = getattr(exc, "resp", None)
    return getattr(response, "status", "unknown")


def _http_reason(exc: HttpError) -> str:
    response = getattr(exc, "resp", None)
    reason = getattr(response, "reason", "")
    return " ".join(reason.split()) if isinstance(reason, str) else ""


def _collect_error_payload(
    payload: Any,
    codes: list[str],
    texts: list[str],
) -> None:
    if not isinstance(payload, dict):
        return

    for key in ("reason", "domain", "status"):
        value = payload.get(key)
        if isinstance(value, str):
            codes.append(value)

    message = payload.get("message")
    if isinstance(message, str):
        texts.append(message)

    errors = payload.get("errors")
    if isinstance(errors, list):
        for item in errors:
            _collect_error_payload(item, codes, texts)


def _error_details(exc: HttpError) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return sanitized reason codes and messages for internal classification."""

    codes: list[str] = []
    texts: list[str] = []

    content = getattr(exc, "content", None)
    if isinstance(content, (bytes, bytearray)):
        try:
            data = json.loads(bytes(content).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                _collect_error_payload(error, codes, texts)

    details = getattr(exc, "error_details", None)
    if isinstance(details, dict):
        _collect_error_payload(details, codes, texts)
    elif isinstance(details, list):
        for item in details:
            _collect_error_payload(item, codes, texts)

    return tuple(codes), tuple(texts)


def _is_rate_limit_error(exc: HttpError) -> bool:
    codes, texts = _error_details(exc)
    combined = " ".join((*codes, *texts)).lower()
    return any(
        token in combined
        for token in (
            "ratelimit",
            "rate limit",
            "quotaexceeded",
            "quota exceeded",
            "dailylimit",
            "daily limit",
            "usagelimits",
        )
    )


def _api_error(exc: HttpError, operation: str) -> GmailApiError:
    status = _http_status(exc)

    if status == 401:
        message = (
            f"Gmail API {operation} was rejected (HTTP 401). The stored Gmail "
            "authorization is no longer valid. Delete the token file and rerun "
            "'python -m app.providers.gmail' to reauthorize."
        )
    elif status == 403:
        if _is_rate_limit_error(exc):
            message = (
                f"Gmail API rate limit exceeded during the {operation} "
                "(HTTP 403). Retry later."
            )
        else:
            message = (
                f"Gmail API denied the {operation} (HTTP 403). Check that the Gmail "
                "API is enabled, the account has access, and the read-only Gmail "
                "scope was granted."
            )
    elif status == 429:
        message = (
            f"Gmail API rate limit exceeded during the {operation} (HTTP 429). "
            "Retry later."
        )
    elif isinstance(status, int) and 500 <= status < 600:
        message = (
            f"Gmail API temporarily failed during the {operation} "
            f"(HTTP {status}). Retry later."
        )
    else:
        reason = _http_reason(exc)
        suffix = f" {reason}" if reason else ""
        message = f"Gmail API {operation} failed (HTTP {status}{suffix})."

    return GmailApiError(message)


def _execute_request(request: Any, operation: str) -> Any:
    """Execute a Gmail request, retrying transient failures only."""

    failure: GmailApiError | None = None

    for attempt in range(_MAX_ATTEMPTS):
        try:
            return request.execute()
        except HttpError as exc:
            status = _http_status(exc)
            retryable = status in _RETRYABLE_STATUS_CODES or (
                status == 403 and _is_rate_limit_error(exc)
            )
            if retryable and attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            failure = _api_error(exc, operation)
            break
        except _TRANSIENT_EXCEPTIONS as exc:
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            failure = GmailApiError(
                f"Gmail API {operation} failed after transient errors "
                f"({type(exc).__name__}). Retry later."
            )
            break
        except Exception as exc:
            failure = GmailApiError(
                f"Gmail API {operation} failed ({type(exc).__name__})."
            )
            break

    if failure is not None:
        raise failure
    raise GmailApiError(f"Gmail API {operation} failed after retries.")


def _validate_page_size(page_size: int) -> None:
    if (
        not isinstance(page_size, int)
        or isinstance(page_size, bool)
        or page_size < 1
        or page_size > GMAIL_MAX_PAGE_SIZE
    ):
        raise ValueError(
            f"page_size must be between 1 and {GMAIL_MAX_PAGE_SIZE}."
        )


def _parse_message_page(data: Any) -> GmailMessagePage:
    if not isinstance(data, dict):
        raise GmailApiError(
            "Gmail API returned an unexpected message-list response."
        )

    raw_messages = data.get("messages", [])
    if raw_messages is None:
        raw_messages = []
    if not isinstance(raw_messages, list):
        raise GmailApiError(
            "Gmail API returned an unexpected message-list payload."
        )

    message_ids: list[str] = []
    for entry in raw_messages:
        if not isinstance(entry, dict):
            raise GmailApiError(
                "Gmail API returned a malformed message-list entry."
            )
        message_id = entry.get("id")
        if not isinstance(message_id, str) or not message_id.strip():
            raise GmailApiError(
                "Gmail API returned a message-list entry without an id."
            )
        message_ids.append(message_id.strip())

    next_page_token = data.get("nextPageToken")
    if next_page_token is not None:
        if not isinstance(next_page_token, str):
            raise GmailApiError(
                "Gmail API returned a malformed pagination token."
            )
        next_page_token = next_page_token.strip() or None

    return GmailMessagePage(
        message_ids=tuple(message_ids),
        next_page_token=next_page_token,
    )


def normalize_gmail_message(
    data: Any,
    *,
    message_id: str,
) -> NormalizedEmail:
    """Convert a Gmail metadata response into a normalized email."""

    if not isinstance(data, dict):
        raise GmailApiError(
            "Gmail API returned an unexpected message response."
        )

    headers = _message_headers(data)
    return NormalizedEmail(
        provider=ProviderName.GMAIL,
        provider_message_id=message_id,
        thread_id=_string_or_none(data.get("threadId")),
        internet_message_id=_first_header(headers, "Message-ID"),
        sender=_first_header(headers, "From"),
        recipients=_parse_recipients(
            _header_values(headers, "To"),
            _header_values(headers, "Cc"),
        ),
        subject=_first_header(headers, "Subject"),
        snippet=_string_or_none(data.get("snippet")),
        received_at=_parse_internal_date(data.get("internalDate")),
        labels=_parse_labels(data.get("labelIds")),
    )


def list_message_ids(
    service: Any,
    *,
    query: str | None = None,
    page_size: int | None = None,
    page_token: str | None = None,
) -> GmailMessagePage:
    """Retrieve one page of Gmail message identifiers."""

    effective_page_size = (
        page_size if page_size is not None else get_settings().gmail_page_size
    )
    _validate_page_size(effective_page_size)

    request_kwargs: dict[str, Any] = {
        "userId": "me",
        "maxResults": effective_page_size,
    }
    if query is not None and query.strip():
        request_kwargs["q"] = query.strip()
    if page_token:
        request_kwargs["pageToken"] = page_token

    request = service.users().messages().list(**request_kwargs)
    data = _execute_request(request, "message list request")
    return _parse_message_page(data)


def get_message_metadata(service: Any, message_id: str) -> NormalizedEmail:
    """Retrieve and normalize one message using Gmail's metadata format."""

    if not isinstance(message_id, str) or not message_id.strip():
        raise ValueError("message_id must not be empty.")
    cleaned_message_id = message_id.strip()

    request = service.users().messages().get(
        userId="me",
        id=cleaned_message_id,
        format="metadata",
        metadataHeaders=list(GMAIL_METADATA_HEADERS),
    )
    data = _execute_request(request, "message metadata request")
    return normalize_gmail_message(data, message_id=cleaned_message_id)


def iter_messages(
    service: Any,
    query: str | None = None,
    *,
    page_size: int | None = None,
) -> Iterator[NormalizedEmail]:
    """Yield normalized Gmail messages across all result pages."""

    effective_page_size = (
        page_size if page_size is not None else get_settings().gmail_page_size
    )
    _validate_page_size(effective_page_size)

    page_token: str | None = None
    seen_tokens: set[str] = set()
    seen_message_ids: set[str] = set()

    while True:
        page = list_message_ids(
            service,
            query=query,
            page_size=effective_page_size,
            page_token=page_token,
        )
        for message_id in page.message_ids:
            if message_id in seen_message_ids:
                continue
            seen_message_ids.add(message_id)
            yield get_message_metadata(service, message_id)

        page_token = page.next_page_token
        if not page_token:
            return
        if page_token in seen_tokens:
            raise GmailApiError(
                "Gmail API pagination returned a repeated page token."
            )
        seen_tokens.add(page_token)
