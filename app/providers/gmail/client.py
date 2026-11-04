"""Authenticated Gmail API v1 client construction and profile lookup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

GMAIL_SERVICE_NAME = "gmail"
GMAIL_API_VERSION = "v1"


class GmailApiError(RuntimeError):
    """Raised when the Gmail API client cannot be built or queried safely."""


@dataclass(frozen=True)
class GmailProfile:
    """Minimal authenticated Gmail profile information."""

    email_address: str


def build_gmail_service(credentials: Credentials) -> Any:
    """Build an authenticated Gmail API v1 client.

    Discovery caching is disabled so the application does not write provider
    cache files into the project or user profile.
    """

    try:
        return build(
            GMAIL_SERVICE_NAME,
            GMAIL_API_VERSION,
            credentials=credentials,
            cache_discovery=False,
        )
    except Exception as exc:
        raise GmailApiError(
            "Failed to initialise the authenticated Gmail API client "
            f"({type(exc).__name__})."
        ) from exc


def fetch_authenticated_profile(service: Any) -> GmailProfile:
    """Return the email address of the authenticated Gmail account.

    Only the profile metadata endpoint is used; no messages are listed or
    retrieved in this milestone.
    """

    try:
        response = service.users().getProfile(userId="me").execute()
    except HttpError as exc:
        response = getattr(exc, "resp", None)
        status = getattr(response, "status", "unknown")
        reason = getattr(response, "reason", "")
        reason = " ".join(reason.split()) if isinstance(reason, str) else ""

        if status == 401:
            message = (
                "Gmail API rejected the profile request (HTTP 401). The stored "
                "Gmail authorization is no longer valid. Delete the token file "
                "and rerun 'python -m app.providers.gmail' to reauthorize."
            )
        elif status == 403:
            message = (
                "Gmail API denied the profile request (HTTP 403). Check that the "
                "Gmail API is enabled for the project, the account has access, "
                "and the read-only Gmail scope was granted. If the authorization "
                "was revoked, delete the token file and rerun "
                "'python -m app.providers.gmail'."
            )
        else:
            reason_suffix = f" {reason}" if reason else ""
            message = (
                "Gmail API rejected the profile request "
                f"(HTTP {status}{reason_suffix})."
            )

        raise GmailApiError(message) from exc
    except Exception as exc:
        raise GmailApiError(
            f"Gmail API profile request failed ({type(exc).__name__})."
        ) from exc

    if not isinstance(response, dict):
        raise GmailApiError("Gmail API returned an unexpected profile response.")

    email_address = response.get("emailAddress")
    if not isinstance(email_address, str) or not email_address.strip():
        raise GmailApiError(
            "Gmail API did not return an email address for the authenticated account."
        )

    return GmailProfile(email_address=email_address.strip())
