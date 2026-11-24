"""Tests for the Gmail authorization command-line entry point."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from app.providers.gmail import __main__ as cli
from app.providers.gmail.client import GmailProfile
from config import Settings


@pytest.fixture(autouse=True)
def isolated_log(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "BASE_DIR", tmp_path)
    yield
    logger = logging.getLogger("app")
    for handler in list(logger.handlers):
        if handler.name == "argus-file":
            logger.removeHandler(handler)
            handler.close()


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        gmail_client_secret_file=tmp_path / "google_client_secret.json",
        gmail_token_file=tmp_path / "gmail_token.json",
    )


def test_cli_reuses_credentials_and_prints_profile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = make_settings(tmp_path)
    credentials = object()
    service = object()
    profile = GmailProfile(email_address="person@example.com")

    with (
        patch.object(cli, "get_settings", return_value=settings),
        patch.object(
            cli,
            "get_credentials",
            return_value=credentials,
        ) as get_credentials_mock,
        patch.object(
            cli,
            "build_gmail_service",
            return_value=service,
        ) as build_mock,
        patch.object(
            cli,
            "fetch_authenticated_profile",
            return_value=profile,
        ) as profile_mock,
    ):
        exit_code = cli.main([])

    assert exit_code == 0
    get_credentials_mock.assert_called_once_with(settings, interactive=True)
    build_mock.assert_called_once_with(credentials)
    profile_mock.assert_called_once_with(service)

    output = capsys.readouterr().out
    assert "person@example.com" in output
    assert str(settings.gmail_token_file) in output


def test_cli_reports_auth_failure_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = make_settings(tmp_path)

    with (
        patch.object(cli, "get_settings", return_value=settings),
        patch.object(
            cli,
            "get_credentials",
            side_effect=cli.GmailAuthError("recovery instructions"),
        ),
    ):
        exit_code = cli.main([])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "recovery instructions" in captured.err


def test_connect_keeps_mailbox_tokens_separate(tmp_path, monkeypatch, engine):
    from sqlalchemy import select
    from sqlalchemy.orm import Session, sessionmaker

    from app.database import session as db
    from app.database.models import EmailAccount
    from app.providers.gmail.auth import account_settings

    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(db, "_session_factory", sessionmaker(engine, expire_on_commit=False))
    selected = ["first@example.com"]
    from unittest.mock import Mock
    credential = Mock()
    credential.to_json.side_effect = lambda: '{"account": "' + selected[0] + '"}'
    monkeypatch.setattr(cli, "request_consent", lambda _: credential)
    monkeypatch.setattr(cli, "build_gmail_service", lambda _: object())
    monkeypatch.setattr(cli, "fetch_authenticated_profile", lambda _: GmailProfile(selected[0]))

    assert cli.main(["--account", "FIRST@example.com"]) == 0
    first_token = account_settings(settings, selected[0]).gmail_token_file
    second = "second@example.com"
    second_token = account_settings(settings, second).gmail_token_file
    assert cli.main(["--account", second]) == 1
    assert not second_token.exists()
    selected[0] = second
    assert cli.main(["--account", second]) == 0
    assert first_token.read_text() != second_token.read_text()
    assert not settings.gmail_token_file.exists()
    with Session(engine) as session:
        assert set(session.scalars(select(EmailAccount.normalized_email_address))) == {
            "first@example.com", second,
        }

def test_rotating_log_retains_three_backups_and_reuses_handler(tmp_path):
    logger = cli._configure_logging("INFO")
    handler, = [h for h in logger.handlers if h.name == "argus-file"]
    assert handler.maxBytes == 5 * 1024 * 1024 and handler.backupCount == 3
    cli._configure_logging("INFO")
    assert [h for h in logger.handlers if h.name == "argus-file"] == [handler]
    handler.maxBytes = 100
    for index in range(6):
        logger.info("entry-%d %s", index, "x" * 60)
    directory = tmp_path / "logs"
    assert sorted(p.name for p in directory.iterdir()) == [
        "argus.log", "argus.log.1", "argus.log.2", "argus.log.3",
    ]
    assert "entry-2" in (directory / "argus.log.3").read_text()


def test_log_directory_unavailable(tmp_path, capsys, monkeypatch):
    (tmp_path / "logs").write_text("occupied")
    monkeypatch.setattr(cli, "get_credentials", lambda *_a, **_k: pytest.fail("OAuth started"))
    assert cli.main([]) == 1
    error = capsys.readouterr().err
    assert "rotating log" in error and "authentication failed" not in error


def token_payload(token):
    return json.dumps({"token": token, "refresh_token": "test-refresh",
                       "client_id": "test-client", "client_secret": "test-secret",
                       "expiry": "2099-01-01T00:00:00Z",
                       "scopes": ["https://www.googleapis.com/auth/gmail.readonly"]})


@pytest.mark.parametrize("force", [False, True])
def test_profile_rejection_and_explicit_reauthorization(tmp_path, monkeypatch, force):
    from google.oauth2.credentials import Credentials

    settings = make_settings(tmp_path)
    address = "selected@example.com"
    selected = cli.account_settings(settings, address).gmail_token_file
    other = cli.account_settings(settings, "other@example.com").gmail_token_file
    selected.parent.mkdir()
    selected.write_text(token_payload("rejected"))
    other.write_bytes(b"other-mailbox")
    settings.gmail_token_file.write_bytes(b"broken legacy credential")
    original = selected.read_bytes()
    command = ["--reauthorize" if force else "--account", address]
    identity = ["wrong@example.com"]
    profiles = []

    def profile(credentials):
        profiles.append(credentials.token)
        if credentials.token == "rejected":
            raise cli.GmailUnauthorizedError("HTTP 401")
        return GmailProfile(identity[0])

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "build_gmail_service", lambda credentials: credentials)
    monkeypatch.setattr(cli, "fetch_authenticated_profile", profile)
    monkeypatch.setattr(cli, "request_consent", lambda _: Credentials.from_authorized_user_info(
        json.loads(token_payload("replacement"))))
    monkeypatch.setattr(cli, "_account_id", lambda *_a, **_kw: 1)
    assert cli.main(command) == 1 and selected.read_bytes() == original
    identity[0] = address
    assert cli.main(command) == 0
    assert profiles == (["replacement"] if force else ["rejected", "replacement"]) * 2
    assert json.loads(selected.read_text())["token"] == "replacement"
    assert other.read_bytes() == b"other-mailbox"
    assert settings.gmail_token_file.read_bytes() == b"broken legacy credential"


def test_explicit_import_and_registration_retry(tmp_path, monkeypatch, capsys):
    settings = make_settings(tmp_path)
    address = "selected@example.com"
    scoped = cli.account_settings(settings, address)
    settings.gmail_token_file.write_text(token_payload("legacy"))
    original = settings.gmail_token_file.read_bytes()
    identity = ["other@example.com"]
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "build_gmail_service", lambda credentials: credentials)
    monkeypatch.setattr(cli, "fetch_authenticated_profile", lambda _: GmailProfile(identity[0]))
    monkeypatch.setattr(cli, "request_consent", lambda _: pytest.fail("unexpected consent"))
    assert cli.main(["--import-legacy", address]) == 1 and not scoped.gmail_token_file.exists()
    identity[0] = address
    with patch.object(cli, "_account_id", side_effect=cli.SQLAlchemyError("private SQL")):
        assert cli.main(["--import-legacy", address]) == 1
    assert "credentials were saved" in capsys.readouterr().err
    assert json.loads(scoped.gmail_token_file.read_text())["token"] == "legacy"
    with patch.object(cli, "_account_id", return_value=1) as register:
        assert cli.main(["--account", address]) == 0
        register.assert_called_once_with(address, create=True)
    assert cli.main(["--import-legacy", address]) == 1
    assert settings.gmail_token_file.read_bytes() == original


def test_log_permission_denied(capsys):
    with patch.object(cli, "RotatingFileHandler", side_effect=PermissionError("denied")):
        assert cli.main([]) == 1
    error = capsys.readouterr().err
    assert "write access" in error and "authentication failed" not in error


def test_sync_loads_only_registered_mailbox_token(tmp_path, monkeypatch, engine):
    from sqlalchemy.orm import sessionmaker
    from app.database import session as db
    from app.services.sync import gmail_sync
    from unittest.mock import MagicMock

    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(gmail_sync, "get_settings", lambda: settings)
    monkeypatch.setattr(db, "_session_factory", sessionmaker(engine, expire_on_commit=False))
    settings.gmail_token_file.write_text("broken legacy token")
    address = "target@example.com"
    cli._account_id(address, create=True)
    assert cli.main(["--sync", address]) == 1
    token = cli.account_settings(settings, address).gmail_token_file
    token.parent.mkdir()
    token.write_text(token_payload("selected"))
    service = MagicMock()
    users = service.users.return_value
    users.getProfile.return_value.execute.return_value = {"emailAddress": address, "historyId": "9"}
    users.messages.return_value.list.return_value.execute.return_value = {}

    def build(credentials):
        assert credentials.token == "selected"
        return service

    monkeypatch.setattr(gmail_sync, "build_gmail_service", build)
    assert cli.main(["--sync", address]) == 0
    assert settings.gmail_token_file.read_text() == "broken legacy token"
