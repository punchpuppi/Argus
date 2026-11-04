"""Tests for typed application configuration."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from config import BASE_DIR, Environment, Settings, get_settings

ENV_VARS = (
    "APP_NAME",
    "ENVIRONMENT",
    "LOG_LEVEL",
    "DATABASE_URL",
    "DATABASE_ECHO",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_ID",
    "GMAIL_CLIENT_SECRET_FILE",
    "GMAIL_TOKEN_FILE",
    "POLL_INTERVAL_SECONDS",
)

ENV_EXAMPLE = BASE_DIR / ".env.example"
CREDENTIALS_DIR = (BASE_DIR / "credentials").resolve()


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Prevent host environment variables from changing test expectations."""

    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_defaults_are_safe_for_foundation() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_name == "Argus"
    assert settings.environment is Environment.DEVELOPMENT
    assert settings.log_level == "INFO"
    assert settings.resolved_database_url.startswith("sqlite:///")
    assert settings.resolved_database_url.endswith("argus.db")
    assert settings.database_echo is False
    assert settings.telegram_bot_token is None
    assert settings.telegram_allowed_user_id is None
    assert settings.poll_interval_seconds == 300
    assert settings.gmail_client_secret_file == (
        CREDENTIALS_DIR / "google_client_secret.json"
    )
    assert settings.gmail_token_file == (CREDENTIALS_DIR / "gmail_token.json")


def test_blank_optional_values_become_none() -> None:
    settings = Settings(
        _env_file=None,
        telegram_bot_token="   ",
        telegram_allowed_user_id="",
    )

    assert settings.telegram_bot_token is None
    assert settings.telegram_allowed_user_id is None


def test_secret_token_is_not_stored_as_plain_string() -> None:
    settings = Settings(_env_file=None, telegram_bot_token="test-token")

    assert settings.telegram_bot_token is not None
    assert settings.telegram_bot_token.get_secret_value() == "test-token"
    assert "test-token" not in repr(settings.telegram_bot_token)


def test_database_password_is_masked() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql://argus:supersecret@localhost:5432/argus",
    )

    assert settings.resolved_database_url == (
        "postgresql://argus:supersecret@localhost:5432/argus"
    )
    assert "supersecret" not in repr(settings)
    assert "supersecret" not in str(settings)
    assert "supersecret" not in str(settings.model_dump())
    assert "supersecret" not in settings.model_dump_json()


def test_database_url_must_not_be_blank() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="   ")


def test_relative_credential_paths_are_anchored_to_project_root() -> None:
    settings = Settings(
        _env_file=None,
        gmail_client_secret_file="credentials/client.json",
        gmail_token_file="credentials/token.json",
    )

    assert settings.gmail_client_secret_file == (
        BASE_DIR / "credentials" / "client.json"
    ).resolve()
    assert settings.gmail_token_file == (
        BASE_DIR / "credentials" / "token.json"
    ).resolve()


def test_absolute_external_credential_paths_are_preserved(tmp_path: Path) -> None:
    external_secret = tmp_path / "client.json"
    external_token = tmp_path / "token.json"

    settings = Settings(
        _env_file=None,
        gmail_client_secret_file=external_secret,
        gmail_token_file=external_token,
    )

    assert settings.gmail_client_secret_file == external_secret.resolve()
    assert settings.gmail_token_file == external_token.resolve()


@pytest.mark.parametrize(
    "value",
    (
        "oauth.json",
        "README.md",
        "config.py",
        ".gitignore",
        "app/providers/gmail/auth.py",
        "secrets/token.json",
    ),
)
def test_token_paths_inside_project_outside_credentials_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("GMAIL_TOKEN_FILE", value)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert "credentials" in str(excinfo.value)


@pytest.mark.parametrize(
    "value",
    (
        "gmail_client_secret.json",
        "README.md",
        "config.py",
        "secrets/client.json",
    ),
)
def test_client_secret_paths_inside_project_outside_credentials_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("GMAIL_CLIENT_SECRET_FILE", value)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert "credentials" in str(excinfo.value)


def test_valid_credentials_paths_are_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GMAIL_CLIENT_SECRET_FILE", "credentials/client.json")
    monkeypatch.setenv("GMAIL_TOKEN_FILE", "credentials/token.json")

    settings = Settings(_env_file=None)

    assert settings.gmail_client_secret_file == (
        CREDENTIALS_DIR / "client.json"
    )
    assert settings.gmail_token_file == (CREDENTIALS_DIR / "token.json")


def test_external_credentials_paths_are_accepted(tmp_path: Path) -> None:
    external_secret = tmp_path / "client.json"
    external_token = tmp_path / "token.json"

    settings = Settings(
        _env_file=None,
        gmail_client_secret_file=external_secret,
        gmail_token_file=external_token,
    )

    assert settings.gmail_client_secret_file.is_relative_to(tmp_path.resolve())
    assert settings.gmail_token_file.is_relative_to(tmp_path.resolve())


def test_tilde_paths_are_expanded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    settings = Settings(_env_file=None, gmail_token_file="~/token.json")

    assert settings.gmail_token_file == (tmp_path / "token.json").resolve()


def test_credential_paths_must_point_to_different_files(tmp_path: Path) -> None:
    shared_path = tmp_path / "shared.json"

    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            gmail_client_secret_file=shared_path,
            gmail_token_file=shared_path,
        )

    assert "different files" in str(excinfo.value)


def test_gitkeep_placeholder_cannot_be_used_as_a_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GMAIL_TOKEN_FILE", "credentials/.gitkeep")

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert ".gitkeep" in str(excinfo.value)


def test_ordinary_file_inside_credentials_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GMAIL_TOKEN_FILE", "credentials/README.md")

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert ".json" in str(excinfo.value)


def test_unknown_env_file_key_is_rejected(tmp_path: Path) -> None:
    env_file = tmp_path / "bad.env"
    env_file.write_text("NOT_A_SETTING=1\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        Settings(_env_file=env_file)


def test_unknown_init_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, databse_url="sqlite:///typo.db")


def test_env_example_keys_match_settings_fields() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    example_keys = set(
        re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE)
    )
    expected_keys = {name.upper() for name in Settings.model_fields}

    assert example_keys == expected_keys


def test_get_settings_is_cached_and_clearable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_NAME", "Cached")
    first = get_settings()

    monkeypatch.setenv("APP_NAME", "Changed")
    assert get_settings() is first
    assert first.app_name == "Cached"

    get_settings.cache_clear()
    assert get_settings().app_name == "Changed"
