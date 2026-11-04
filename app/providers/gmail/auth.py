"""Gmail OAuth 2.0 desktop authentication.

This module owns credential loading, refresh, the local browser consent flow,
and token-file storage. It deliberately contains no Gmail API request logic;
see :mod:`app.providers.gmail.client` for that.

Security notes:

* Access tokens, refresh tokens and client secrets are never logged or printed.
* Only the Gmail read-only scope is requested.
* Tokens are written to the configured local token file, never to the database.
* Credential paths are validated before use; project-local credentials must
  live in the dedicated credentials directory.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from config import Settings, get_settings, resolve_credential_path

logger = logging.getLogger(__name__)

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SCOPES: tuple[str, ...] = (GMAIL_READONLY_SCOPE,)

_REAUTHORIZATION_HINT = (
    "Run 'python -m app.providers.gmail' to authorize Argus, then retry."
)


class GmailAuthError(RuntimeError):
    """Base class for Gmail authentication failures."""


class GmailClientSecretMissingError(GmailAuthError):
    """Raised when Google's OAuth client credentials file cannot be found."""


class GmailReauthorizationRequiredError(GmailAuthError):
    """Raised when stored credentials are missing, invalid, or no longer usable."""


class GmailTokenStorageError(GmailAuthError):
    """Raised when refreshed or new credentials cannot be persisted."""


def _load_json_object(
    path: Path,
    *,
    error_type: type[GmailAuthError],
    description: str,
) -> dict[str, Any]:
    """Load a JSON file and require a top-level object without logging content."""

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise error_type(
            f"{description} at {path} could not be read as valid JSON."
        ) from exc

    if not isinstance(data, dict):
        raise error_type(
            f"{description} at {path} must contain a JSON object."
        )

    return data


def _load_saved_credentials(token_path: Path) -> Credentials | None:
    """Load a stored token, validating its shape and the read-only Gmail scope."""

    if not token_path.is_file():
        return None

    data = _load_json_object(
        token_path,
        error_type=GmailReauthorizationRequiredError,
        description="Stored Gmail credentials",
    )

    try:
        credentials = Credentials.from_authorized_user_info(data)
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        raise GmailReauthorizationRequiredError(
            f"Stored Gmail credentials at {token_path} are not a valid "
            f"authorized-user credential file. {_REAUTHORIZATION_HINT}"
        ) from exc

    if not credentials.has_scopes(GMAIL_SCOPES):
        raise GmailReauthorizationRequiredError(
            "Stored Gmail credentials do not grant the required read-only "
            f"Gmail scope. {_REAUTHORIZATION_HINT}"
        )

    return credentials


def save_credentials(credentials: Credentials, token_path: Path) -> None:
    """Atomically write credentials to the token file.

    Permissions are best-effort. On POSIX, ``chmod(0o600)`` restricts access to
    the owner. On Windows, ``chmod(0o600)`` only changes the read-only attribute
    and does **not** establish a restrictive ACL; effective protection depends
    on the ACL inherited by the credentials directory. Keep that directory in a
    user-private location.

    ``credentials.to_json()`` contains a refresh token, so the payload is never
    logged. The temporary file is created beside the destination and replaced
    atomically to avoid leaving a partially written token file.
    """

    try:
        token_path = resolve_credential_path(token_path)
    except ValueError as exc:
        raise GmailTokenStorageError(str(exc)) from exc

    try:
        token_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise GmailTokenStorageError(
            f"Gmail credentials directory could not be created: {token_path.parent}."
        ) from exc

    try:
        payload = credentials.to_json()
    except Exception as exc:
        raise GmailTokenStorageError(
            "Gmail credentials could not be serialised for storage."
        ) from exc

    temp_path: str | None = None
    file_descriptor: int | None = None
    try:
        file_descriptor, temp_path = tempfile.mkstemp(
            dir=token_path.parent,
            prefix=f".{token_path.name}.",
            suffix=".tmp",
        )
        handle = os.fdopen(file_descriptor, "w", encoding="utf-8")
        file_descriptor = None
        with handle:
            handle.write(payload)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, token_path)
        temp_path = None
    except Exception as exc:
        raise GmailTokenStorageError(
            f"Gmail credentials could not be written to {token_path}."
        ) from exc
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    logger.info("Stored Gmail OAuth credentials.")
    logger.debug("Gmail token file: %s", token_path)


def _refresh_credentials(
    credentials: Credentials,
    token_path: Path,
) -> Credentials:
    """Refresh an expired access token and persist the updated credentials."""

    if not credentials.refresh_token:
        raise GmailReauthorizationRequiredError(
            "Stored Gmail credentials have expired and no refresh token is "
            f"available. {_REAUTHORIZATION_HINT}"
        )

    try:
        credentials.refresh(Request())
    except RefreshError as exc:
        raise GmailReauthorizationRequiredError(
            "Stored Gmail credentials could not be refreshed; authorization "
            f"may have been revoked or expired. {_REAUTHORIZATION_HINT}"
        ) from exc
    except Exception as exc:
        raise GmailAuthError(
            "Gmail credentials could not be refreshed because of a network or "
            "configuration error. Check connectivity and retry."
        ) from exc

    save_credentials(credentials, token_path)
    logger.info("Refreshed Gmail OAuth credentials.")
    return credentials


def authorize(
    settings: Settings | None = None,
    *,
    port: int = 0,
    open_browser: bool = True,
) -> Credentials:
    """Run Google's local browser consent flow and store the resulting token.

    ``port=0`` lets the operating system choose a free loopback port, which is
    the expected behaviour for a Google "Desktop app" OAuth client.
    """

    settings = settings or get_settings()
    client_secret_path = settings.gmail_client_secret_file

    if not client_secret_path.is_file():
        raise GmailClientSecretMissingError(
            "Google OAuth client secret file was not found at "
            f"{client_secret_path}. Download the desktop OAuth client JSON "
            "from Google Cloud Console and save it to that path."
        )

    client_config = _load_json_object(
        client_secret_path,
        error_type=GmailAuthError,
        description="Google OAuth client secret file",
    )
    if "installed" not in client_config and "web" not in client_config:
        raise GmailAuthError(
            "Google OAuth client secret JSON must contain an 'installed' or "
            "'web' configuration."
        )

    try:
        flow = InstalledAppFlow.from_client_config(
            client_config,
            scopes=list(GMAIL_SCOPES),
        )
    except Exception as exc:
        raise GmailAuthError(
            "Google OAuth client secret file is invalid or unreadable."
        ) from exc

    try:
        credentials = flow.run_local_server(
            port=port,
            open_browser=open_browser,
            access_type="offline",
            prompt="consent",
        )
    except Exception as exc:
        raise GmailAuthError(
            "Gmail authorization did not complete. Retry and grant the "
            "read-only Gmail scope in the browser."
        ) from exc

    if not credentials.has_scopes(GMAIL_SCOPES):
        raise GmailAuthError(
            "Gmail authorization did not grant the required read-only scope. "
            "Rerun the command and approve the Gmail read-only permission."
        )

    save_credentials(credentials, settings.gmail_token_file)
    logger.info("Completed Gmail OAuth authorization.")
    return credentials


def get_credentials(
    settings: Settings | None = None,
    *,
    interactive: bool = False,
    open_browser: bool = True,
) -> Credentials:
    """Return usable Gmail credentials, refreshing or reauthorizing if needed.

    ``interactive=False`` is the safe default for background workers: it never
    opens a browser and raises a recovery error instead. The command-line entry
    point passes ``interactive=True``.
    """

    settings = settings or get_settings()

    try:
        credentials = _load_saved_credentials(settings.gmail_token_file)
    except GmailReauthorizationRequiredError:
        if interactive:
            return authorize(settings, open_browser=open_browser)
        raise

    if credentials is None:
        if interactive:
            return authorize(settings, open_browser=open_browser)
        raise GmailReauthorizationRequiredError(
            "No stored Gmail credentials were found at "
            f"{settings.gmail_token_file}. {_REAUTHORIZATION_HINT}"
        )

    if credentials.valid:
        return credentials

    if credentials.refresh_token:
        try:
            return _refresh_credentials(
                credentials,
                settings.gmail_token_file,
            )
        except GmailReauthorizationRequiredError:
            if interactive:
                return authorize(settings, open_browser=open_browser)
            raise

    if interactive:
        return authorize(settings, open_browser=open_browser)

    raise GmailReauthorizationRequiredError(
        "Stored Gmail credentials are not valid and no refresh token is "
        f"available. {_REAUTHORIZATION_HINT}"
    )
