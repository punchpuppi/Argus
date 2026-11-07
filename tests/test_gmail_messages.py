"""Tests for Gmail message retrieval, pagination, and normalization."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from googleapiclient.errors import HttpError

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


def http_error(status: int) -> HttpError:
    response = httplib2.Response({"status": status})
    response.reason = "Error"
    return HttpError(response, SENSITIVE_BODY)


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
    sleep_mock.assert_not_called()


def test_unexpected_error_is_sanitized() -> None:
    service, list_mock, _ = make_service()
    list_mock.return_value.execute.side_effect = RuntimeError(
        "sensitive-response-body"
    )

    with pytest.raises(messages.GmailApiError) as excinfo:
        messages.list_message_ids(service, page_size=10)

    message = str(excinfo.value)
    assert "RuntimeError" in message
    assert "sensitive-response-body" not in message


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
