"""Tests for Gmail message retrieval, pagination, and normalization."""

from __future__ import annotations

import traceback
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from google.auth.exceptions import TransportError
from googleapiclient.errors import HttpError
from httplib2 import HttpLib2Error

from app.database.enums import ProviderName
from app.email.schemas import NormalizedEmail
from app.providers.gmail import messages

DEFAULT_HEADERS = [
    {"name": "Message-ID", "value": "<abc123@example.com>"},
    {"name": "From", "value": "Shop <shop@example.com>"},
    {
        "name": "To",
        "value": "Alice <alice@example.com>, bob@example.com",
    },
    {"name": "Cc", "value": "carol@example.com"},
    {"name": "Subject", "value": "Your order has shipped"},
]

SENSITIVE_BODY = b'{"error": {"message": "sensitive-response-body"}}'
RATE_LIMIT_BODY = (
    b'{"error": {"code": 403, "message": "Rate Limit Exceeded", '
    b'"errors": [{"domain": "usageLimits", "reason": "rateLimitExceeded"}]}}'
)
PERMISSION_BODY = (
    b'{"error": {"code": 403, "message": "The caller does not have permission", '
    b'"errors": [{"reason": "insufficientPermissions"}]}}'
)


def make_service() -> tuple[MagicMock, MagicMock, MagicMock]:
    service = MagicMock()
    messages_api = service.users.return_value.messages.return_value
    return service, messages_api.list, messages_api.get


def message_response(
    message_id: str,
    *,
    thread_id: str | None = "thread-1",
    internal_date: str | None = "1700000000123",
    headers: list[dict[str, object]] | None = None,
    label_ids: list[str] | None = None,
    snippet: str | None = "A short snippet",
) -> dict[str, object]:
    response: dict[str, object] = {
        "id": message_id,
        "threadId": thread_id,
        "labelIds": ["INBOX", "UNREAD"] if label_ids is None else label_ids,
        "snippet": snippet,
        "payload": {
            "headers": DEFAULT_HEADERS if headers is None else headers,
        },
    }
    if internal_date is not None:
        response["internalDate"] = internal_date
    return response


def http_error(status: int, body: bytes = SENSITIVE_BODY) -> HttpError:
    response = httplib2.Response({"status": status})
    response.reason = "Error"
    return HttpError(response, body)


def test_iter_messages_retrieves_and_normalizes_metadata() -> None:
    service, list_mock, get_mock = make_service()
    list_mock.return_value.execute.return_value = {
        "messages": [{"id": "message-1"}, {"id": "message-2"}]
    }
    get_mock.return_value.execute.side_effect = [
        message_response("message-1"),
        message_response(
            "message-2",
            thread_id="thread-2",
            internal_date="1700000001123",
            label_ids=["INBOX"],
            snippet="Second snippet",
        ),
    ]

    emails = list(
        messages.iter_messages(service, query="is:unread", page_size=25)
    )

    assert len(emails) == 2
    first = emails[0]
    assert first.provider is ProviderName.GMAIL
    assert first.provider_message_id == "message-1"
    assert first.thread_id == "thread-1"
    assert first.internet_message_id == "<abc123@example.com>"
    assert first.sender == "Shop <shop@example.com>"
    assert first.recipients == (
        "Alice <alice@example.com>",
        "bob@example.com",
        "carol@example.com",
    )
    assert first.subject == "Your order has shipped"
    assert first.snippet == "A short snippet"
    assert first.received_at == datetime.fromtimestamp(
        1700000000123 / 1000,
        tz=UTC,
    )
    assert first.received_at is not None
    assert first.received_at.tzinfo is not None
    assert first.labels == ("INBOX", "UNREAD")
    assert emails[1].provider_message_id == "message-2"
    assert emails[1].labels == ("INBOX",)

    list_mock.assert_called_once_with(
        userId="me",
        q="is:unread",
        maxResults=25,
    )
    get_mock.assert_any_call(
        userId="me",
        id="message-1",
        format="metadata",
        metadataHeaders=list(messages.GMAIL_METADATA_HEADERS),
    )


@pytest.mark.parametrize("response", ({}, {"messages": []}))
def test_empty_inbox_returns_no_messages(response: dict[str, object]) -> None:
    service, list_mock, get_mock = make_service()
    list_mock.return_value.execute.return_value = response

    result = list(messages.iter_messages(service, page_size=10))

    assert result == []
    get_mock.assert_not_called()


def test_search_query_and_page_size_are_forwarded() -> None:
    service, list_mock, _ = make_service()
    list_mock.return_value.execute.return_value = {}

    page = messages.list_message_ids(
        service,
        query="newer_than:7d",
        page_size=50,
    )

    assert page.message_ids == ()
    assert page.next_page_token is None
    list_mock.assert_called_once_with(
        userId="me",
        q="newer_than:7d",
        maxResults=50,
    )


def test_blank_search_query_is_omitted() -> None:
    service, list_mock, _ = make_service()
    list_mock.return_value.execute.return_value = {}

    messages.list_message_ids(service, query="   ", page_size=50)

    list_mock.assert_called_once_with(userId="me", maxResults=50)


def test_iter_messages_uses_configured_page_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, list_mock, _ = make_service()
    list_mock.return_value.execute.return_value = {}
    monkeypatch.setattr(
        messages,
        "get_settings",
        lambda: SimpleNamespace(gmail_page_size=17),
    )

    list(messages.iter_messages(service, query="is:unread"))

    list_mock.assert_called_once_with(
        userId="me",
        q="is:unread",
        maxResults=17,
    )


@pytest.mark.parametrize("page_size", (0, 501))
def test_page_size_out_of_range_is_rejected(page_size: int) -> None:
    service, list_mock, _ = make_service()

    with pytest.raises(ValueError):
        messages.list_message_ids(service, page_size=page_size)

    list_mock.assert_not_called()


def test_iter_messages_follows_next_page_token() -> None:
    service, list_mock, get_mock = make_service()
    list_mock.return_value.execute.side_effect = [
        {"messages": [{"id": "message-1"}], "nextPageToken": "page-2"},
        {"messages": [{"id": "message-2"}]},
    ]
    get_mock.return_value.execute.side_effect = [
        message_response("message-1"),
        message_response("message-2"),
    ]

    emails = list(messages.iter_messages(service, page_size=10))

    assert [email.provider_message_id for email in emails] == [
        "message-1",
        "message-2",
    ]
    assert list_mock.call_args_list[0].kwargs == {
        "userId": "me",
        "maxResults": 10,
    }
    assert list_mock.call_args_list[1].kwargs == {
        "userId": "me",
        "maxResults": 10,
        "pageToken": "page-2",
    }


def test_repeated_pagination_token_is_rejected() -> None:
    service, list_mock, _ = make_service()
    list_mock.return_value.execute.side_effect = [
        {"messages": [], "nextPageToken": "page-2"},
        {"messages": [], "nextPageToken": "page-2"},
    ]

    with pytest.raises(
        messages.GmailApiError,
        match="repeated page token",
    ):
        list(messages.iter_messages(service, page_size=10))


def test_pagination_failure_is_sanitized() -> None:
    service, list_mock, get_mock = make_service()
    list_mock.return_value.execute.side_effect = [
        {"messages": [], "nextPageToken": "page-2"},
        http_error(403),
    ]

    with pytest.raises(messages.GmailApiError) as excinfo:
        list(messages.iter_messages(service, page_size=10))

    message = str(excinfo.value)
    assert "403" in message
    assert "enabled" in message.lower()
    assert "sensitive-response-body" not in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    get_mock.assert_not_called()


def test_missing_headers_and_incomplete_message_are_normalized_safely() -> None:
    service, _, get_mock = make_service()
    get_mock.return_value.execute.return_value = {"id": "message-1"}

    email = messages.get_message_metadata(service, "message-1")

    assert email == NormalizedEmail(
        provider=ProviderName.GMAIL,
        provider_message_id="message-1",
        thread_id=None,
        internet_message_id=None,
        sender=None,
        recipients=(),
        subject=None,
        snippet=None,
        received_at=None,
        labels=(),
    )


def test_malformed_headers_and_fields_are_ignored_safely() -> None:
    service, _, get_mock = make_service()
    get_mock.return_value.execute.return_value = {
        "id": "message-1",
        "threadId": 123,
        "internalDate": "not-a-timestamp",
        "labelIds": "not-a-list",
        "snippet": 42,
        "payload": {
            "headers": [
                {"name": "From", "value": 123},
                {"name": "Subject"},
                "not-a-header",
            ]
        },
    }

    email = messages.get_message_metadata(service, "message-1")

    assert email.thread_id is None
    assert email.sender is None
    assert email.subject is None
    assert email.snippet is None
    assert email.received_at is None
    assert email.labels == ()
    assert email.recipients == ()


def test_invalid_internal_date_returns_no_timestamp() -> None:
    service, _, get_mock = make_service()
    get_mock.return_value.execute.return_value = message_response(
        "message-1",
        internal_date="not-a-timestamp",
    )

    email = messages.get_message_metadata(service, "message-1")

    assert email.received_at is None


def test_internal_date_is_converted_to_utc() -> None:
    service, _, get_mock = make_service()
    get_mock.return_value.execute.return_value = message_response(
        "message-1",
        internal_date="1700000000123",
    )

    email = messages.get_message_metadata(service, "message-1")

    assert email.received_at == datetime.fromtimestamp(
        1700000000123 / 1000,
        tz=UTC,
    )
    assert email.received_at is not None
    assert email.received_at.utcoffset() is not None


def test_401_list_error_requests_reauthorization_without_body() -> None:
    service, list_mock, _ = make_service()
    list_mock.return_value.execute.side_effect = http_error(401)

    with pytest.raises(messages.GmailApiError) as excinfo:
        messages.list_message_ids(service, page_size=10)

    message = str(excinfo.value)
    assert "401" in message
    assert "delete the token file" in message.lower()
    assert "reauthorize" in message.lower()
    assert "sensitive-response-body" not in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_403_get_error_mentions_permissions_without_body() -> None:
    service, _, get_mock = make_service()
    get_mock.return_value.execute.side_effect = http_error(403)

    with pytest.raises(messages.GmailApiError) as excinfo:
        messages.get_message_metadata(service, "message-1")

    message = str(excinfo.value)
    assert "403" in message
    assert "enabled" in message.lower()
    assert "scope" in message.lower()
    assert "sensitive-response-body" not in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


def test_rate_limit_is_retried() -> None:
    service, list_mock, _ = make_service()
    list_mock.return_value.execute.side_effect = [
        http_error(429),
        {"messages": []},
    ]

    with patch.object(messages.time, "sleep") as sleep_mock:
        page = messages.list_message_ids(service, page_size=10)

    assert page.message_ids == ()
    assert list_mock.return_value.execute.call_count == 2
    sleep_mock.assert_called_once_with(0.5)


def test_transient_server_error_is_retried_then_reported() -> None:
    service, list_mock, _ = make_service()
    list_mock.return_value.execute.side_effect = [
        http_error(500),
        http_error(503),
        http_error(502),
    ]

    with patch.object(messages.time, "sleep") as sleep_mock:
        with pytest.raises(messages.GmailApiError) as excinfo:
            messages.list_message_ids(service, page_size=10)

    message = str(excinfo.value)
    assert "temporarily failed" in message.lower()
    assert "sensitive-response-body" not in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert list_mock.return_value.execute.call_count == 3
    assert sleep_mock.call_count == 2


def test_non_retryable_http_error_is_reported_without_body() -> None:
    service, list_mock, _ = make_service()
    list_mock.return_value.execute.side_effect = http_error(404)

    with patch.object(messages.time, "sleep") as sleep_mock:
        with pytest.raises(messages.GmailApiError) as excinfo:
            messages.list_message_ids(service, page_size=10)

    message = str(excinfo.value)
    assert "404" in message
    assert "sensitive-response-body" not in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    sleep_mock.assert_not_called()


def test_unexpected_error_is_sanitized() -> None:
    service, list_mock, _ = make_service()
    list_mock.return_value.execute.side_effect = RuntimeError(
        "sensitive-response-body"
    )

    with patch.object(messages.time, "sleep") as sleep_mock:
        with pytest.raises(messages.GmailApiError) as excinfo:
            messages.list_message_ids(service, page_size=10)

    message = str(excinfo.value)
    assert "RuntimeError" in message
    assert "sensitive-response-body" not in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    sleep_mock.assert_not_called()
    rendered = "".join(
        traceback.format_exception(
            type(excinfo.value),
            excinfo.value,
            excinfo.value.__traceback__,
        )
    )
    assert "sensitive-response-body" not in rendered


@pytest.mark.parametrize(
    "response",
    (
        None,
        {"messages": "not-a-list"},
        {"messages": [{}]},
        {"messages": [], "nextPageToken": 123},
    ),
)
def test_malformed_list_responses_are_rejected(response: object) -> None:
    service, list_mock, _ = make_service()
    list_mock.return_value.execute.return_value = response

    with pytest.raises(messages.GmailApiError):
        messages.list_message_ids(service, page_size=10)


def test_malformed_message_response_is_rejected() -> None:
    service, _, get_mock = make_service()
    get_mock.return_value.execute.return_value = None

    with pytest.raises(messages.GmailApiError):
        messages.get_message_metadata(service, "message-1")


def test_metadata_requests_never_request_full_or_raw_messages() -> None:
    service, list_mock, get_mock = make_service()
    list_mock.return_value.execute.return_value = {
        "messages": [{"id": "message-1"}]
    }
    get_mock.return_value.execute.return_value = message_response("message-1")

    list(messages.iter_messages(service, page_size=10))

    call_kwargs = get_mock.call_args.kwargs
    assert call_kwargs["format"] == "metadata"
    assert call_kwargs["format"] != "full"
    assert "raw" not in call_kwargs
    assert "body" not in call_kwargs
    assert set(messages.GMAIL_METADATA_HEADERS) <= set(
        call_kwargs["metadataHeaders"]
    )


def test_403_rate_limit_is_retried() -> None:
    service, list_mock, _ = make_service()
    list_mock.return_value.execute.side_effect = [
        http_error(403, RATE_LIMIT_BODY),
        {"messages": []},
    ]

    with patch.object(messages.time, "sleep") as sleep_mock:
        page = messages.list_message_ids(service, page_size=10)

    assert page.message_ids == ()
    assert list_mock.return_value.execute.call_count == 2
    sleep_mock.assert_called_once_with(0.5)


def test_403_permission_error_is_not_retried() -> None:
    service, list_mock, _ = make_service()
    list_mock.return_value.execute.side_effect = http_error(
        403,
        PERMISSION_BODY,
    )

    with patch.object(messages.time, "sleep") as sleep_mock:
        with pytest.raises(messages.GmailApiError) as excinfo:
            messages.list_message_ids(service, page_size=10)

    message = str(excinfo.value)
    assert "403" in message
    assert "enabled" in message.lower()
    assert "scope" in message.lower()
    assert "insufficientPermissions" not in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    sleep_mock.assert_not_called()


def test_403_rate_limit_exhaustion_reports_rate_limit() -> None:
    service, list_mock, _ = make_service()
    list_mock.return_value.execute.side_effect = [
        http_error(403, RATE_LIMIT_BODY),
        http_error(403, RATE_LIMIT_BODY),
        http_error(403, RATE_LIMIT_BODY),
    ]

    with patch.object(messages.time, "sleep") as sleep_mock:
        with pytest.raises(messages.GmailApiError) as excinfo:
            messages.list_message_ids(service, page_size=10)

    message = str(excinfo.value)
    assert "rate limit" in message.lower()
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert list_mock.return_value.execute.call_count == 3
    assert sleep_mock.call_count == 2


@pytest.mark.parametrize(
    "exception",
    (
        TimeoutError("temporary timeout"),
        TransportError("temporary transport failure"),
        HttpLib2Error("temporary transport library failure"),
    ),
)
def test_transient_transport_errors_are_retried(exception: Exception) -> None:
    service, list_mock, _ = make_service()
    list_mock.return_value.execute.side_effect = [
        exception,
        {"messages": []},
    ]

    with patch.object(messages.time, "sleep") as sleep_mock:
        page = messages.list_message_ids(service, page_size=10)

    assert page.message_ids == ()
    assert list_mock.return_value.execute.call_count == 2
    sleep_mock.assert_called_once_with(0.5)


def test_transient_transport_errors_exhaust_retries() -> None:
    service, list_mock, _ = make_service()
    list_mock.return_value.execute.side_effect = [
        TimeoutError("temporary timeout"),
        TimeoutError("temporary timeout"),
        TimeoutError("temporary timeout"),
    ]

    with patch.object(messages.time, "sleep") as sleep_mock:
        with pytest.raises(messages.GmailApiError) as excinfo:
            messages.list_message_ids(service, page_size=10)

    message = str(excinfo.value)
    assert "transient errors" in message.lower()
    assert "TimeoutError" in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert list_mock.return_value.execute.call_count == 3
    assert sleep_mock.call_count == 2


def test_iter_messages_deduplicates_message_ids_across_pages() -> None:
    service, list_mock, get_mock = make_service()
    list_mock.return_value.execute.side_effect = [
        {
            "messages": [{"id": "message-1"}, {"id": "message-1"}],
            "nextPageToken": "page-2",
        },
        {"messages": [{"id": "message-1"}, {"id": "message-2"}]},
    ]
    get_mock.return_value.execute.side_effect = [
        message_response("message-1"),
        message_response("message-2"),
    ]

    emails = list(messages.iter_messages(service, page_size=10))

    assert [email.provider_message_id for email in emails] == [
        "message-1",
        "message-2",
    ]
    assert get_mock.return_value.execute.call_count == 2


def test_http_error_does_not_chain_raw_response_body() -> None:
    service, list_mock, _ = make_service()
    list_mock.return_value.execute.side_effect = http_error(404)

    with pytest.raises(messages.GmailApiError) as excinfo:
        messages.list_message_ids(service, page_size=10)

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    rendered = "".join(
        traceback.format_exception(
            type(excinfo.value),
            excinfo.value,
            excinfo.value.__traceback__,
        )
    )
    assert "sensitive-response-body" not in rendered


def test_internal_date_overflow_returns_no_timestamp() -> None:
    service, _, get_mock = make_service()
    response = message_response("message-1")
    response["internalDate"] = float("inf")
    get_mock.return_value.execute.return_value = response

    email = messages.get_message_metadata(service, "message-1")

    assert email.received_at is None


def test_empty_next_page_token_means_end_of_results() -> None:
    service, list_mock, _ = make_service()
    list_mock.return_value.execute.return_value = {
        "messages": [],
        "nextPageToken": "   ",
    }

    page = messages.list_message_ids(service, page_size=10)

    assert page.message_ids == ()
    assert page.next_page_token is None


def test_list_message_ids_uses_configured_page_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, list_mock, _ = make_service()
    list_mock.return_value.execute.return_value = {}
    monkeypatch.setattr(
        messages,
        "get_settings",
        lambda: SimpleNamespace(gmail_page_size=23),
    )

    messages.list_message_ids(service)

    list_mock.assert_called_once_with(userId="me", maxResults=23)


def test_get_message_metadata_strips_message_id() -> None:
    service, _, get_mock = make_service()
    get_mock.return_value.execute.return_value = message_response("message-1")

    email = messages.get_message_metadata(service, "  message-1  ")

    assert get_mock.call_args.kwargs["id"] == "message-1"
    assert email.provider_message_id == "message-1"


def test_malformed_display_name_does_not_crash_normalization() -> None:
    service, _, get_mock = make_service()
    get_mock.return_value.execute.return_value = message_response(
        "message-1",
        headers=[
            {
                "name": "To",
                "value": "Bad \ud800 <bad@example.com>",
            }
        ],
    )

    email = messages.get_message_metadata(service, "message-1")

    assert any("bad@example.com" in recipient for recipient in email.recipients)


@pytest.mark.parametrize("page_size", (0, 501))
def test_iter_messages_rejects_invalid_page_size(page_size: int) -> None:
    service, list_mock, _ = make_service()

    with pytest.raises(ValueError):
        list(messages.iter_messages(service, page_size=page_size))

    list_mock.assert_not_called()


def test_empty_page_with_next_token_continues() -> None:
    service, list_mock, get_mock = make_service()
    list_mock.return_value.execute.side_effect = [
        {"messages": [], "nextPageToken": "page-2"},
        {"messages": [{"id": "message-1"}]},
    ]
    get_mock.return_value.execute.return_value = message_response("message-1")

    emails = list(messages.iter_messages(service, page_size=10))

    assert [email.provider_message_id for email in emails] == ["message-1"]
    assert list_mock.return_value.execute.call_count == 2
