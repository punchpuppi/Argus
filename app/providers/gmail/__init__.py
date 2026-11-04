"""Gmail provider authentication and API client."""

from app.providers.gmail.auth import (
    GMAIL_READONLY_SCOPE,
    GMAIL_SCOPES,
    GmailAuthError,
    GmailClientSecretMissingError,
    GmailReauthorizationRequiredError,
    GmailTokenStorageError,
    authorize,
    get_credentials,
    save_credentials,
)
from app.providers.gmail.client import (
    GmailApiError,
    GmailProfile,
    build_gmail_service,
    fetch_authenticated_profile,
)

__all__ = [
    "GMAIL_READONLY_SCOPE",
    "GMAIL_SCOPES",
    "GmailApiError",
    "GmailAuthError",
    "GmailClientSecretMissingError",
    "GmailProfile",
    "GmailReauthorizationRequiredError",
    "GmailTokenStorageError",
    "authorize",
    "build_gmail_service",
    "fetch_authenticated_profile",
    "get_credentials",
    "save_credentials",
]
