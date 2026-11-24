"""Command-line entry point for Gmail authorization.

Usage::

    python -m app.providers.gmail

The command runs the local OAuth consent flow if needed, then verifies the
authenticated account through the Gmail profile endpoint. It never prints
access tokens, refresh tokens, or client secrets.
"""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler

from google.oauth2.credentials import Credentials

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.database.enums import ProviderName
from app.database.models import EmailAccount
from app.database.session import session_scope
from app.providers.gmail.auth import (
    GmailAuthError, GmailReauthorizationRequiredError, account_settings,
    get_credentials, request_consent, save_credentials,
)
from app.services.sync import SyncError, sync_gmail_account
from app.providers.gmail.client import (
    GmailApiError,
    GmailProfile,
    build_gmail_service,
    fetch_authenticated_profile,
)
from app.providers.gmail.transport import GmailUnauthorizedError
from config import BASE_DIR, Settings, get_settings


def _account_id(address: str, *, create: bool) -> int:
    with session_scope() as session:
        account = session.scalar(select(EmailAccount).where(
            EmailAccount.provider == ProviderName.GMAIL,
            EmailAccount.normalized_email_address == address.strip().lower(),
        ))
        if account is None:
            if not create:
                raise GmailAuthError("Connect this mailbox with --account first.")
            account = EmailAccount(provider=ProviderName.GMAIL, email_address=address)
            session.add(account)
            session.flush()
        return account.id


def _configure_logging(level: str) -> logging.Logger:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger("app")
    logger.setLevel(level)
    if not any(handler.name == "argus-file" for handler in logger.handlers):
        directory = BASE_DIR / "logs"
        directory.mkdir(exist_ok=True)
        handler = RotatingFileHandler(
            directory / "argus.log", maxBytes=5 * 1024 * 1024,
            backupCount=3, encoding="utf-8",
        )
        handler.name = "argus-file"
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    return logger


def _connect_account(
    base: Settings, address: str, *, force: bool, migrate: bool,
) -> tuple[GmailProfile, Settings]:
    scoped = account_settings(base, address)
    if migrate:
        if scoped.gmail_token_file.exists():
            raise GmailAuthError("This mailbox already has a token. Use --reauthorize EMAIL to replace it.")
        credentials = get_credentials(base, persist_refresh=False)
    elif force:
        credentials = request_consent(scoped)
    else:
        try:
            credentials = get_credentials(scoped, persist_refresh=False)
            profile = fetch_authenticated_profile(build_gmail_service(credentials))
        except (GmailReauthorizationRequiredError, GmailUnauthorizedError):
            credentials = request_consent(scoped)
        else:
            return _register_verified(scoped, address, credentials, profile)

    profile = fetch_authenticated_profile(build_gmail_service(credentials))
    return _register_verified(scoped, address, credentials, profile)


def _register_verified(
    scoped: Settings, address: str, credentials: Credentials, profile: GmailProfile,
) -> tuple[GmailProfile, Settings]:
    if profile.email_address.lower() != address.strip().lower():
        raise GmailAuthError("Selected Google mailbox differs from the requested account.")
    save_credentials(credentials, scoped.gmail_token_file)
    try:
        _account_id(profile.email_address, create=True)
    except SQLAlchemyError:
        raise GmailAuthError(
            "Verified credentials were saved, but account registration failed. "
            "Check the database, apply migrations, and retry --account EMAIL."
        ) from None
    return profile, scoped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Connect or synchronize a local Gmail account.")
    options = parser.add_mutually_exclusive_group()
    options.add_argument("--account", metavar="EMAIL", help="authorize and register this mailbox")
    options.add_argument("--reauthorize", metavar="EMAIL", help="replace this mailbox's token after consent")
    options.add_argument("--import-legacy", metavar="EMAIL", help="import a verified legacy token once")
    options.add_argument("--sync", metavar="EMAIL", help="run one synchronization")
    args = parser.parse_args(argv)
    settings = get_settings()
    try:
        logger = _configure_logging(settings.log_level)
    except OSError:
        print("Cannot open the local rotating log. Check write access to the logs directory.", file=sys.stderr)
        return 1

    try:
        if args.sync:
            result = sync_gmail_account(_account_id(args.sync, create=False))
            if not result.success:
                print(result.error, file=sys.stderr)
                return 1
            status = "Superseded" if result.superseded else "Synchronized"
            summary = (f"{status} account {result.account_id}: {result.processed_count} processed, "
                       f"{result.skipped_missing_count} missing, {result.events_created} events.")
            logger.info(summary)
            print(summary)
            return 0
        address = args.account or args.reauthorize or args.import_legacy
        if address is not None:
            profile, settings = _connect_account(
                settings, address, force=args.reauthorize is not None,
                migrate=args.import_legacy is not None,
            )
        else:
            credentials = get_credentials(settings, interactive=True)
            profile = fetch_authenticated_profile(build_gmail_service(credentials))
    except SQLAlchemyError:
        print("Database operation failed. Check the database configuration and run alembic upgrade head.", file=sys.stderr)
        return 1
    except (GmailAuthError, GmailApiError, SyncError, OSError) as exc:
        print(f"Gmail authentication failed: {exc}", file=sys.stderr)
        return 1

    print(f"Authenticated Gmail account: {profile.email_address}")
    print(f"Token file: {settings.gmail_token_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
