"""Typed application configuration for Argus.

Secrets are read from environment variables or the local ``.env`` file.
The ``.env`` file and credential files are intentionally excluded from Git.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_DIR = (BASE_DIR / "credentials").resolve()


def resolve_credential_path(value: Path | str) -> Path:
    """Resolve a credential path and enforce project-level safety rules.

    Relative paths are anchored to the project root and ``~`` is expanded.
    Credential files that resolve inside the Argus project must live in the
    dedicated ``credentials`` directory. Paths outside the project are allowed
    so credentials can live in an operating-system configuration directory.
    """

    path = Path(value).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path

    try:
        path = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            f"Credential path could not be resolved: {value!s}"
        ) from exc

    if path == CREDENTIALS_DIR / ".gitkeep":
        raise ValueError(
            "The .gitkeep placeholder cannot be used as a credential file."
        )

    if path == BASE_DIR or BASE_DIR in path.parents:
        if CREDENTIALS_DIR not in path.parents:
            raise ValueError(
                f"Credential path {path} is inside the Argus project but outside "
                f"{CREDENTIALS_DIR}. Store credentials in the credentials directory "
                "or use an absolute path outside the project."
            )
        if path.suffix.lower() != ".json":
            raise ValueError(
                f"Credential path {path} inside the project must use a .json "
                "extension."
            )

    return path


class Environment(StrEnum):
    """Deployment environment names."""

    DEVELOPMENT = "development"
    TESTING = "testing"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Runtime settings loaded from ``.env`` and process environment variables.

    ``extra="forbid"`` makes misspelled keys in ``.env`` (or unexpected init
    keyword arguments) fail fast instead of being silently ignored. Unknown
    operating-system environment variables that do not match a field are still
    ignored by pydantic-settings.
    """

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        env_prefix="",
        case_sensitive=False,
        extra="forbid",
    )

    app_name: str = "Argus"
    environment: Environment = Environment.DEVELOPMENT
    log_level: str = Field(default="INFO", min_length=1)

    # SecretStr keeps database passwords out of repr()/logging.
    database_url: SecretStr = SecretStr(
        f"sqlite:///{(BASE_DIR / 'argus.db').as_posix()}"
    )
    database_echo: bool = False

    telegram_bot_token: SecretStr | None = None
    telegram_allowed_user_id: int | None = Field(default=None, gt=0)

    gmail_client_secret_file: Path = BASE_DIR / "credentials" / "google_client_secret.json"
    gmail_token_file: Path = BASE_DIR / "credentials" / "gmail_token.json"
    gmail_page_size: int = Field(default=100, ge=1, le=500)

    poll_interval_seconds: int = Field(default=300, ge=30)

    @property
    def resolved_database_url(self) -> str:
        """Return the plain database URL for engine creation."""

        return self.database_url.get_secret_value()

    @field_validator("environment", mode="before")
    @classmethod
    def _normalize_environment(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if normalized not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return normalized

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("database_url must not be empty")
        return value

    @field_validator("gmail_client_secret_file", "gmail_token_file", mode="after")
    @classmethod
    def _resolve_credential_paths(cls, value: Path) -> Path:
        return resolve_credential_path(value)

    @model_validator(mode="after")
    def _credential_paths_must_differ(self) -> "Settings":
        if self.gmail_client_secret_file == self.gmail_token_file:
            raise ValueError(
                "GMAIL_CLIENT_SECRET_FILE and GMAIL_TOKEN_FILE must point to "
                "different files."
            )
        return self

    @field_validator("telegram_bot_token", mode="before")
    @classmethod
    def _blank_token_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("telegram_allowed_user_id", mode="before")
    @classmethod
    def _blank_user_id_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings instance for application use."""

    return Settings()
