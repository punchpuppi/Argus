"""Tests for Gmail API client construction and profile retrieval."""

from __future__ import annotations

import traceback
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app.providers.gmail import client


def test_build_gmail_service_uses_v1_and_credentials() -> None:
    credentials = object()
    service = object()

    with patch.object(client, "build", return_value=service) as build_mock:
        result = client.build_gmail_service(credentials)

    assert result is service
    build_mock.assert_called_once_with(
        "gmail",
        "v1",
        credentials=credentials,
        cache_discovery=False,
    )


def test_build_failure_is_wrapped_without_details() -> None:
    with patch.object(
        client,
        "build",
        side_effect=RuntimeError("client-secret-value"),
    ):
        with pytest.raises(client.GmailApiError) as excinfo:
            client.build_gmail_service(object())

    assert "initialise" in str(excinfo.value)
    assert "RuntimeError" in str(excinfo.value)
    assert "client-secret-value" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    rendered = "".join(
        traceback.format_exception(
            type(excinfo.value),
            excinfo.value,
            excinfo.value.__traceback__,
        )
    )
    assert "client-secret-value" not in rendered


def test_fetch_authenticated_profile_returns_email_address() -> None:
    service = MagicMock()
    service.users.return_value.getProfile.return_value.execute.return_value = {
        "emailAddress": "person@example.com"
    }

    profile = client.fetch_authenticated_profile(service)

    assert profile == client.GmailProfile(email_address="person@example.com")
    service.users.return_value.getProfile.assert_called_once_with(userId="me")


def test_fetch_authenticated_profile_rejects_missing_email_address() -> None:
    service = MagicMock()
    service.users.return_value.getProfile.return_value.execute.return_value = {
        "historyId": "123"
    }

    with pytest.raises(client.GmailApiError) as excinfo:
        client.fetch_authenticated_profile(service)

    assert "email address" in str(excinfo.value)


def test_fetch_authenticated_profile_rejects_unexpected_response() -> None:
    service = MagicMock()
    service.users.return_value.getProfile.return_value.execute.return_value = None

    with pytest.raises(client.GmailApiError) as excinfo:
        client.fetch_authenticated_profile(service)

    assert "unexpected" in str(excinfo.value)


def test_http_error_is_wrapped_without_response_body() -> None:
    response = httplib2.Response({"status": 403})
    response.reason = "Forbidden"
    error = HttpError(
        response,
        b'{"error": {"message": "insufficientPermissions"}}',
    )
    service = MagicMock()
    service.users.return_value.getProfile.return_value.execute.side_effect = error

    with pytest.raises(client.GmailApiError) as excinfo:
        client.fetch_authenticated_profile(service)

    message = str(excinfo.value)
    assert "403" in message
    assert "insufficientPermissions" not in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    rendered = "".join(
        traceback.format_exception(
            type(excinfo.value),
            excinfo.value,
            excinfo.value.__traceback__,
        )
    )
    assert "insufficientPermissions" not in rendered


def test_http_401_includes_actionable_reauthorization_guidance() -> None:
    response = httplib2.Response({"status": 401})
    response.reason = "Unauthorized"
    error = HttpError(
        response,
        b'{"error": {"message": "invalidCredentials"}}',
    )
    service = MagicMock()
    service.users.return_value.getProfile.return_value.execute.side_effect = error

    with pytest.raises(client.GmailApiError) as excinfo:
        client.fetch_authenticated_profile(service)

    message = str(excinfo.value)
    assert "401" in message
    assert "delete the token file" in message.lower()
    assert "reauthorize" in message.lower()
    assert "invalidCredentials" not in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_http_403_mentions_permission_checks_without_assuming_revocation() -> None:
    response = httplib2.Response({"status": 403})
    response.reason = "Forbidden"
    error = HttpError(
        response,
        b'{"error": {"message": "insufficientPermissions"}}',
    )
    service = MagicMock()
    service.users.return_value.getProfile.return_value.execute.side_effect = error

    with pytest.raises(client.GmailApiError) as excinfo:
        client.fetch_authenticated_profile(service)

    message = str(excinfo.value)
    assert "403" in message
    assert "enabled" in message.lower()
    assert "scope" in message.lower()
    assert "insufficientPermissions" not in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_unexpected_api_error_is_wrapped_without_details() -> None:
    service = MagicMock()
    service.users.return_value.getProfile.return_value.execute.side_effect = (
        RuntimeError("access-token-value")
    )

    with pytest.raises(client.GmailApiError) as excinfo:
        client.fetch_authenticated_profile(service)

    assert "profile request failed" in str(excinfo.value)
    assert "RuntimeError" in str(excinfo.value)
    assert "access-token-value" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    rendered = "".join(
        traceback.format_exception(
            type(excinfo.value),
            excinfo.value,
            excinfo.value.__traceback__,
        )
    )
    assert "access-token-value" not in rendered
