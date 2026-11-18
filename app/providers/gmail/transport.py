"""Shared Gmail API request execution, retries, and sanitized error mapping.

This module exists so that message retrieval and history retrieval share a
single retry and error-sanitisation policy. It was extracted from
:mod:`app.providers.gmail.messages` without changing behaviour.

Only HTTP status codes and the HTTP reason phrase are used to build error
messages. Raw Google ``HttpError`` content is never attached to a raised error:
provider errors are constructed inside the exception handler but raised only
after it exits, so ``__cause__``, ``__context__``, and formatted tracebacks stay
clean.
"""

from __future__ import annotations

import json
import time
from typing import Any

from google.auth.exceptions import TransportError
from googleapiclient.errors import HttpError
from httplib2 import HttpLib2Error

from app.providers.gmail.client import GmailApiError

GMAIL_MAX_PAGE_SIZE = 500

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_TRANSIENT_EXCEPTIONS = (
    HttpLib2Error,
    TimeoutError,
    ConnectionError,
    TransportError,
)
_MAX_ATTEMPTS = 3
_RETRY_DELAYS = (0.5, 1.0)


class GmailNotFoundError(GmailApiError):
    """Raised when the Gmail API reports a missing resource (HTTP 404)."""


def http_status(exc: HttpError) -> Any:
    """Return the HTTP status reported by an ``HttpError``, if available."""

    response = getattr(exc, "resp", None)
    return getattr(response, "status", "unknown")


def http_reason(exc: HttpError) -> str:
    """Return the sanitized HTTP reason phrase reported by an ``HttpError``."""

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


def is_rate_limit_error(exc: HttpError) -> bool:
    """Return whether an ``HttpError`` describes a quota or rate-limit failure."""

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


def api_error(exc: HttpError, operation: str) -> GmailApiError:
    """Return a sanitized, typed error for an ``HttpError``.

    A 404 becomes :class:`GmailNotFoundError` so callers can distinguish a
    missing resource from other failures without parsing error text.
    """

    status = http_status(exc)
    error_type: type[GmailApiError] = GmailApiError

    if status == 401:
        message = (
            f"Gmail API {operation} was rejected (HTTP 401). The stored Gmail "
            "authorization is no longer valid. Delete the token file and rerun "
            "'python -m app.providers.gmail' to reauthorize."
        )
    elif status == 403:
        if is_rate_limit_error(exc):
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
    elif status == 404:
        error_type = GmailNotFoundError
        message = (
            f"Gmail API {operation} failed: the requested resource was not "
            "found (HTTP 404)."
        )
    else:
        reason = http_reason(exc)
        suffix = f" {reason}" if reason else ""
        message = f"Gmail API {operation} failed (HTTP {status}{suffix})."

    return error_type(message)


def execute_request(request: Any, operation: str) -> Any:
    """Execute a Gmail request, retrying transient failures only."""

    failure: GmailApiError | None = None

    for attempt in range(_MAX_ATTEMPTS):
        try:
            return request.execute()
        except HttpError as exc:
            status = http_status(exc)
            retryable = status in _RETRYABLE_STATUS_CODES or (
                status == 403 and is_rate_limit_error(exc)
            )
            if retryable and attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            failure = api_error(exc, operation)
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


def validate_page_size(page_size: int) -> None:
    """Validate a Gmail page size against the API limit."""

    if (
        not isinstance(page_size, int)
        or isinstance(page_size, bool)
        or page_size < 1
        or page_size > GMAIL_MAX_PAGE_SIZE
    ):
        raise ValueError(
            f"page_size must be between 1 and {GMAIL_MAX_PAGE_SIZE}."
        )
