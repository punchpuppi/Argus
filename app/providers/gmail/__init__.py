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
from app.providers.gmail.messages import (
    GMAIL_MAX_PAGE_SIZE,
    GMAIL_METADATA_HEADERS,
    GmailMessagePage,
    get_message_metadata,
    iter_messages,
    list_message_ids,
)

__all__ = [
    "GMAIL_MAX_PAGE_SIZE",
    "GMAIL_METADATA_HEADERS",
    "GMAIL_READONLY_SCOPE",
    "GMAIL_SCOPES",
    "GmailApiError",
    "GmailAuthError",
    "GmailClientSecretMissingError",
    "GmailMessagePage",
    "GmailProfile",
    "GmailReauthorizationRequiredError",
    "GmailTokenStorageError",
    "authorize",
    "build_gmail_service",
    "fetch_authenticated_profile",
    "get_credentials",
    "get_message_metadata",
    "iter_messages",
    "list_message_ids",
    "save_credentials",
]
