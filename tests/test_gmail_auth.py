"""Tests for Gmail OAuth authentication and token handling."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials

from app.providers.gmail import auth
from config import BASE_DIR, Settings


def make_settings(
    tmp_path: Path,
    *,
    client_secret_exists: bool = True,
) -> Settings:
    client_secret = tmp_path / "google_client_secret.json"
    token_file = tmp_path / "gmail_token.json"
    if client_secret_exists:
        client_secret.write_text('{"installed": {}}', encoding="utf-8")

    return Settings(
        _env_file=None,
        gmail_client_secret_file=client_secret,
        gmail_token_file=token_file,
    )


def write_token_file(settings: Settings) -> None:
    settings.gmail_token_file.write_text("{}", encoding="utf-8")


def write_client_secret(settings: Settings, content: str) -> None:
    settings.gmail_client_secret_file.write_text(content, encoding="utf-8")


def make_credentials(
    *,
    valid: bool = True,
    expired: bool = False,
    refresh_token: str | None = "refresh-token-value",
    scopes: list[str] | None = None,
) -> MagicMock:
    credentials = MagicMock(spec=Credentials)
    credentials.valid = valid
    credentials.expired = expired
    credentials.refresh_token = refresh_token
    credentials.scopes = list(auth.GMAIL_SCOPES) if scopes is None else scopes
    credentials.has_scopes.side_effect = lambda requested: set(requested).issubset(
        set(credentials.scopes or [])
    )
    return credentials


def test_only_read_only_gmail_scope_is_requested() -> None:
    assert auth.GMAIL_SCOPES == (
        "https://www.googleapis.com/auth/gmail.readonly",
    )


def test_valid_saved_credentials_are_reused(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_token_file(settings)
    credentials = make_credentials(valid=True)

    with (
        patch.object(
            auth.Credentials,
            "from_authorized_user_info",
            return_value=credentials,
        ) as loader,
        patch.object(auth, "authorize") as authorize_mock,
    ):
        result = auth.get_credentials(settings)

    assert result is credentials
    loader.assert_called_once_with({})
    credentials.refresh.assert_not_called()
    authorize_mock.assert_not_called()


def test_expired_credentials_are_refreshed_and_saved(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_token_file(settings)
    credentials = make_credentials(valid=False, expired=True)

    with (
        patch.object(
            auth.Credentials,
            "from_authorized_user_info",
            return_value=credentials,
        ),
        patch.object(auth, "Request") as request_factory,
        patch.object(auth, "save_credentials") as save_mock,
    ):
        result = auth.get_credentials(settings)

    assert result is credentials
    credentials.refresh.assert_called_once_with(request_factory.return_value)
    save_mock.assert_called_once_with(credentials, settings.gmail_token_file)


def test_refresh_failure_requests_reauthorization(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_token_file(settings)
    credentials = make_credentials(valid=False, expired=True)
    credentials.refresh.side_effect = RefreshError("invalid_grant")

    with (
        patch.object(
            auth.Credentials,
            "from_authorized_user_info",
            return_value=credentials,
        ),
        patch.object(auth, "save_credentials") as save_mock,
    ):
        with pytest.raises(auth.GmailReauthorizationRequiredError) as excinfo:
            auth.get_credentials(settings)

    message = str(excinfo.value)
    assert "python -m app.providers.gmail" in message
    assert "refresh-token-value" not in message
    save_mock.assert_not_called()


def test_expired_credentials_without_refresh_token_require_reauthorization(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    write_token_file(settings)
    credentials = make_credentials(valid=False, expired=True, refresh_token=None)

    with patch.object(
        auth.Credentials,
        "from_authorized_user_info",
        return_value=credentials,
    ):
        with pytest.raises(auth.GmailReauthorizationRequiredError) as excinfo:
            auth.get_credentials(settings)

    assert "no refresh token" in str(excinfo.value).lower()


def test_missing_token_file_requires_authorization(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    with patch.object(auth, "authorize") as authorize_mock:
        with pytest.raises(auth.GmailReauthorizationRequiredError) as excinfo:
            auth.get_credentials(settings)

    assert "python -m app.providers.gmail" in str(excinfo.value)
    authorize_mock.assert_not_called()


def test_scope_mismatch_requires_reauthorization(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_token_file(settings)
    credentials = make_credentials(
        valid=True,
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )

    with patch.object(
        auth.Credentials,
        "from_authorized_user_info",
        return_value=credentials,
    ):
        with pytest.raises(auth.GmailReauthorizationRequiredError) as excinfo:
            auth.get_credentials(settings)

    assert "read-only" in str(excinfo.value).lower()


@pytest.mark.parametrize("content", ("not-json", "[]", "42", "{}"))
def test_malformed_token_files_require_reauthorization(
    tmp_path: Path,
    content: str,
) -> None:
    settings = make_settings(tmp_path)
    settings.gmail_token_file.write_text(content, encoding="utf-8")

    with pytest.raises(auth.GmailReauthorizationRequiredError) as excinfo:
        auth.get_credentials(settings)

    message = str(excinfo.value)
    assert "Stored Gmail credentials" in message
    assert "refresh-token-value" not in message


def test_interactive_mode_authorizes_when_token_is_missing(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fresh_credentials = make_credentials(valid=True)

    with patch.object(
        auth,
        "authorize",
        return_value=fresh_credentials,
    ) as authorize_mock:
        result = auth.get_credentials(settings, interactive=True)

    assert result is fresh_credentials
    authorize_mock.assert_called_once_with(settings, open_browser=True)


def test_interactive_mode_reauthorizes_after_refresh_failure(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_token_file(settings)
    expired = make_credentials(valid=False, expired=True)
    expired.refresh.side_effect = RefreshError("invalid_grant")
    fresh_credentials = make_credentials(valid=True)

    with (
        patch.object(
            auth.Credentials,
            "from_authorized_user_info",
            return_value=expired,
        ),
        patch.object(
            auth,
            "authorize",
            return_value=fresh_credentials,
        ) as authorize_mock,
    ):
        result = auth.get_credentials(settings, interactive=True)

    assert result is fresh_credentials
    authorize_mock.assert_called_once_with(settings, open_browser=True)


def test_authorize_rejects_insufficient_granted_scope(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    credentials = make_credentials(
        valid=True,
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )
    flow = MagicMock()
    flow.run_local_server.return_value = credentials

    with (
        patch.object(
            auth.InstalledAppFlow,
            "from_client_config",
            return_value=flow,
        ),
        patch.object(auth, "save_credentials") as save_mock,
    ):
        with pytest.raises(auth.GmailAuthError) as excinfo:
            auth.authorize(settings)

    assert "read-only scope" in str(excinfo.value)
    save_mock.assert_not_called()


def test_missing_client_secret_file_is_reported(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, client_secret_exists=False)

    with patch.object(
        auth.InstalledAppFlow,
        "from_client_config",
    ) as flow_factory:
        with pytest.raises(auth.GmailClientSecretMissingError) as excinfo:
            auth.authorize(settings)

    assert str(settings.gmail_client_secret_file) in str(excinfo.value)
    flow_factory.assert_not_called()


def test_authorize_runs_local_flow_and_saves_token(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fresh_credentials = make_credentials(valid=True)
    flow = MagicMock()
    flow.run_local_server.return_value = fresh_credentials

    with (
        patch.object(
            auth.InstalledAppFlow,
            "from_client_config",
            return_value=flow,
        ) as flow_factory,
        patch.object(auth, "save_credentials") as save_mock,
    ):
        result = auth.authorize(settings, port=0, open_browser=False)

    assert result is fresh_credentials
    flow_factory.assert_called_once_with(
        {"installed": {}},
        scopes=list(auth.GMAIL_SCOPES),
    )
    flow.run_local_server.assert_called_once_with(
        port=0,
        open_browser=False,
        access_type="offline",
        prompt="consent",
    )
    save_mock.assert_called_once_with(
        fresh_credentials,
        settings.gmail_token_file,
    )


def test_invalid_client_secret_file_is_reported(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    with patch.object(
        auth.InstalledAppFlow,
        "from_client_config",
        side_effect=ValueError("malformed client secret"),
    ):
        with pytest.raises(auth.GmailAuthError) as excinfo:
            auth.authorize(settings)

    assert "invalid or unreadable" in str(excinfo.value)
    assert "malformed client secret" not in str(excinfo.value)


@pytest.mark.parametrize("content", ("not-json", "[]", "42", "{}"))
def test_malformed_client_secret_files_are_rejected(
    tmp_path: Path,
    content: str,
) -> None:
    settings = make_settings(tmp_path)
    write_client_secret(settings, content)

    with patch.object(
        auth.InstalledAppFlow,
        "from_client_config",
    ) as flow_factory:
        with pytest.raises(auth.GmailAuthError) as excinfo:
            auth.authorize(settings)

    message = str(excinfo.value)
    assert "client secret" in message.lower()
    assert "refresh-token-value" not in message
    flow_factory.assert_not_called()


def test_malformed_installed_client_secret_is_wrapped(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    with patch.object(
        auth.InstalledAppFlow,
        "from_client_config",
        side_effect=KeyError("client_id"),
    ):
        with pytest.raises(auth.GmailAuthError) as excinfo:
            auth.authorize(settings)

    assert "invalid or unreadable" in str(excinfo.value)
    assert "client_id" not in str(excinfo.value)


def test_authorization_flow_failure_is_reported_without_details(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    flow = MagicMock()
    flow.run_local_server.side_effect = RuntimeError("sensitive-provider-detail")

    with patch.object(
        auth.InstalledAppFlow,
        "from_client_config",
        return_value=flow,
    ):
        with pytest.raises(auth.GmailAuthError) as excinfo:
            auth.authorize(settings)

    assert "did not complete" in str(excinfo.value)
    assert "sensitive-provider-detail" not in str(excinfo.value)


def test_save_credentials_writes_token_file_and_does_not_log_it(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    credentials = MagicMock(spec=Credentials)
    credentials.to_json.return_value = '{"refresh_token": "super-secret-refresh"}'
    token_path = tmp_path / "nested" / "gmail_token.json"

    with caplog.at_level(logging.DEBUG):
        auth.save_credentials(credentials, token_path)

    assert token_path.read_text(encoding="utf-8") == (
        '{"refresh_token": "super-secret-refresh"}'
    )
    assert {path.name for path in token_path.parent.iterdir()} == {
        token_path.name
    }
    assert "super-secret-refresh" not in caplog.text


def test_save_credentials_rejects_project_files() -> None:
    credentials = MagicMock(spec=Credentials)

    with pytest.raises(auth.GmailTokenStorageError) as excinfo:
        auth.save_credentials(credentials, BASE_DIR / "README.md")

    assert "credentials" in str(excinfo.value)
    credentials.to_json.assert_not_called()


def test_save_credentials_rejects_gitkeep_placeholder() -> None:
    credentials = MagicMock(spec=Credentials)

    with pytest.raises(auth.GmailTokenStorageError) as excinfo:
        auth.save_credentials(credentials, BASE_DIR / "credentials" / ".gitkeep")

    assert ".gitkeep" in str(excinfo.value)
    credentials.to_json.assert_not_called()


def test_save_credentials_wraps_directory_creation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = MagicMock(spec=Credentials)
    token_path = tmp_path / "new" / "gmail_token.json"

    def fail_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)

    with pytest.raises(auth.GmailTokenStorageError) as excinfo:
        auth.save_credentials(credentials, token_path)

    assert "directory" in str(excinfo.value).lower()
    credentials.to_json.assert_not_called()


def test_save_credentials_cleans_up_when_atomic_replace_fails(
    tmp_path: Path,
) -> None:
    credentials = MagicMock(spec=Credentials)
    credentials.to_json.return_value = '{"refresh_token": "super-secret"}'
    token_path = tmp_path / "gmail_token.json"

    with patch.object(auth.os, "replace", side_effect=OSError("disk full")):
        with pytest.raises(auth.GmailTokenStorageError) as excinfo:
            auth.save_credentials(credentials, token_path)

    assert "disk full" not in str(excinfo.value)
    assert not token_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_save_credentials_cleans_up_when_fdopen_fails(tmp_path: Path) -> None:
    credentials = MagicMock(spec=Credentials)
    credentials.to_json.return_value = '{"refresh_token": "super-secret"}'
    token_path = tmp_path / "gmail_token.json"

    with patch.object(auth.os, "fdopen", side_effect=OSError("fd failure")):
        with pytest.raises(auth.GmailTokenStorageError) as excinfo:
            auth.save_credentials(credentials, token_path)

    assert "fd failure" not in str(excinfo.value)
    assert not token_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_tokens_are_not_logged_when_refresh_fails(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = make_settings(tmp_path)
    write_token_file(settings)
    credentials = make_credentials(valid=False, expired=True)
    credentials.refresh.side_effect = RefreshError(
        "invalid_grant refresh-token-value"
    )

    with (
        patch.object(
            auth.Credentials,
            "from_authorized_user_info",
            return_value=credentials,
        ),
        caplog.at_level(logging.DEBUG),
    ):
        with pytest.raises(auth.GmailReauthorizationRequiredError):
            auth.get_credentials(settings)

    assert "refresh-token-value" not in caplog.text


def test_gitignore_excludes_credentials_tokens_and_database_sidecars() -> None:
    patterns = (BASE_DIR / ".gitignore").read_text(encoding="utf-8")

    for pattern in (
        "credentials/*",
        "!credentials/.gitkeep",
        "google_client_secret*.json",
        "*client_secret*.json",
        "token.json",
        "*_token.json",
        "*.sqlite-wal",
        "*.sqlite-shm",
        "*.sqlite-journal",
        "*.sqlite3-wal",
        "*.sqlite3-shm",
        "*.sqlite3-journal",
    ):
        assert pattern in patterns

    assert "token*.json" not in patterns
