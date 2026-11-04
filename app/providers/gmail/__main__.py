"""Command-line entry point for Gmail authorization.

Usage::

    python -m app.providers.gmail

The command runs the local OAuth consent flow if needed, then verifies the
authenticated account through the Gmail profile endpoint. It never prints
access tokens, refresh tokens, or client secrets.
"""

from __future__ import annotations

import logging
import sys

from app.providers.gmail.auth import GmailAuthError, get_credentials
from app.providers.gmail.client import (
    GmailApiError,
    build_gmail_service,
    fetch_authenticated_profile,
)
from config import get_settings


def main() -> int:
    settings = get_settings()
    configured_level = getattr(logging, settings.log_level, logging.INFO)

    # Keep third-party libraries at WARNING so their debug output cannot emit
    # request details or credentials. Only Argus loggers use the configured
    # application log level.
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("app").setLevel(configured_level)

    try:
        credentials = get_credentials(settings, interactive=True)
        profile = fetch_authenticated_profile(build_gmail_service(credentials))
    except (GmailAuthError, GmailApiError) as exc:
        print(f"Gmail authentication failed: {exc}", file=sys.stderr)
        return 1

    print(f"Authenticated Gmail account: {profile.email_address}")
    print(f"Token file: {settings.gmail_token_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
