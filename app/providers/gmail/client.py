"""Authenticated Gmail API v1 client construction and profile lookup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.providers.gmail.transport import GmailApiError, execute_request

GMAIL_SERVICE_NAME = "gmail"
GMAIL_API_VERSION = "v1"


@dataclass(frozen=True)
class GmailProfile:
    """Minimal authenticated Gmail profile information.

    ``history_id`` is the mailbox's current history record identifier when the
    provider supplies one. It is optional so existing callers that construct a
    profile from an email address alone continue to work.
    """

    email_address: str
    history_id: str | None = None


def _parse_history_id(value: Any) -> str | None:
    """Return a validated mailbox history identifier, or ``None``.

    A missing or blank value is treated as unavailable so profile verification
    keeps working. An unexpected type is a malformed provider response.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise GmailApiError("Gmail API returned a malformed history identifier.")
    cleaned = value.strip()
    return cleaned or None


def build_gmail_service(credentials: Credentials) -> Any:
    """Build an authenticated Gmail API v1 client.

    Discovery caching is disabled so the application does not write provider
    cache files into the project or user profile.
    """

    failure: GmailApiError | None = None
    try:
        return build(
            GMAIL_SERVICE_NAME,
            GMAIL_API_VERSION,
            credentials=credentials,
            cache_discovery=False,
        )
    except Exception as exc:
        failure = GmailApiError(
            "Failed to initialise the authenticated Gmail API client "
            f"({type(exc).__name__})."
        )

    if failure is not None:
        raise failure
    raise GmailApiError(
        "Failed to initialise the authenticated Gmail API client."
    )


def fetch_authenticated_profile(service: Any) -> GmailProfile:
    """Return the authenticated Gmail account's email address and history id.

    Only the profile metadata endpoint is used; no messages are listed or
    retrieved here.
    """

    try:
        request = service.users().getProfile(userId="me")
    except Exception as exc:
        failure = GmailApiError(f"Gmail API profile request failed ({type(exc).__name__}).")
    else:
        failure = None
    if failure is not None:
        raise failure

    response = execute_request(request, "profile request")
    if not isinstance(response, dict):
        raise GmailApiError("Gmail API returned an unexpected profile response.")

    email_address = response.get("emailAddress")
    if not isinstance(email_address, str) or not email_address.strip():
        raise GmailApiError(
            "Gmail API did not return an email address for the authenticated account."
        )

    return GmailProfile(
        email_address=email_address.strip(),
        history_id=_parse_history_id(response.get("historyId")),
    )
