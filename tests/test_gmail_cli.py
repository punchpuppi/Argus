"""Tests for the Gmail authorization command-line entry point."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.providers.gmail import __main__ as cli
from app.providers.gmail.client import GmailProfile
from config import Settings


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
        exit_code = cli.main()

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
        exit_code = cli.main()

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "recovery instructions" in captured.err
