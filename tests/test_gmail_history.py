"""Tests for Gmail History API retrieval, validation, and error classification."""

from __future__ import annotations

import traceback
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from google.auth.exceptions import TransportError
from googleapiclient.errors import HttpError
from httplib2 import HttpLib2Error

from app.providers.gmail import history, transport
from app.providers.gmail.client import GmailApiError
from app.providers.gmail.history import (
    GmailHistoryExpiredError,
    GmailHistoryPage,
    list_history,
)
from app.providers.gmail.transport import GmailNotFoundError

SENSITIVE_BODY = b'{"error": {"message": "sensitive-response-body"}}'
_MISSING = object()


def make_service() -> tuple[MagicMock, MagicMock]:
    """Return a mocked Gmail service and its history.list callable."""

    service = MagicMock()
    history_api = service.users.return_value.history.return_value
    return service, history_api.list


def http_error(
    status: int,
    body: bytes = SENSITIVE_BODY,
    reason: str = "Error",
) -> HttpError:
    response = httplib2.Response({"status": status})
    response.reason = reason
    return HttpError(response, body)


def message_added(message_id: str) -> dict[str, object]:
    return {"message": {"id": message_id, "threadId": "thread-1"}}


def history_response(
    *,
    history_id: object = "100",
    records: object = _MISSING,
    next_page_token: object = _MISSING,
) -> dict[str, object]:
    payload: dict[str, object] = {"historyId": history_id}
    if records is not _MISSING:
        payload["history"] = records
    if next_page_token is not _MISSING:
        payload["nextPageToken"] = next_page_token
    return payload


def test_list_history_returns_added_message_ids() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response(
        records=[{"messagesAdded": [message_added("message-1")]}],
    )

    page = list_history(service, start_history_id="123", page_size=10)

    assert page == GmailHistoryPage(
        message_ids=("message-1",),
        history_id="100",
        next_page_token=None,
    )
    list_mock.assert_called_once_with(
        userId="me",
        startHistoryId="123",
        maxResults=10,
    )


def test_list_history_strips_start_history_id() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response()

    list_history(service, start_history_id="  123  ", page_size=10)

    list_mock.assert_called_once_with(
        userId="me",
        startHistoryId="123",
        maxResults=10,
    )


def test_multiple_history_records_preserve_chronological_order() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response(
        records=[
            {"messagesAdded": [message_added("message-2")]},
            {"messagesAdded": [message_added("message-1")]},
        ],
    )

    page = list_history(service, start_history_id="1", page_size=10)

    assert page.message_ids == ("message-2", "message-1")


def test_multiple_messages_in_one_record_are_all_returned() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response(
        records=[
            {
                "messagesAdded": [
                    message_added("message-1"),
                    message_added("message-2"),
                    message_added("message-3"),
                ]
            }
        ],
    )

    page = list_history(service, start_history_id="1", page_size=10)

    assert page.message_ids == ("message-1", "message-2", "message-3")


def test_duplicate_message_ids_are_removed_in_encounter_order() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response(
        records=[
            {"messagesAdded": [message_added("message-2"), message_added("message-1")]},
            {"messagesAdded": [message_added("message-1"), message_added("message-3")]},
        ],
    )

    page = list_history(service, start_history_id="1", page_size=10)

    assert page.message_ids == ("message-2", "message-1", "message-3")


def test_duplicate_message_ids_within_one_record_are_removed() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response(
        records=[
            {
                "messagesAdded": [
                    message_added("message-1"),
                    message_added("message-1"),
                ]
            }
        ],
    )

    page = list_history(service, start_history_id="1", page_size=10)

    assert page.message_ids == ("message-1",)


def test_empty_history_payload_returns_no_message_ids() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response()

    page = list_history(service, start_history_id="1", page_size=10)

    assert page.message_ids == ()
    assert page.history_id == "100"
    assert page.next_page_token is None


def test_empty_history_list_with_history_id_is_valid() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response(records=[])

    page = list_history(service, start_history_id="1", page_size=10)

    assert page.message_ids == ()
    assert page.history_id == "100"


def test_deletions_and_label_changes_are_ignored() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response(
        records=[
            {"messagesDeleted": [{"message": {"id": "deleted-1"}}]},
            {"labelsAdded": [{"message": {"id": "labelled-1"}, "labelIds": ["X"]}]},
            {"labelsRemoved": [{"message": {"id": "labelled-2"}, "labelIds": ["Y"]}]},
            {"messagesAdded": []},
        ],
    )

    page = list_history(service, start_history_id="1", page_size=10)

    assert page.message_ids == ()


def test_next_page_token_is_returned() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response(
        next_page_token="page-2",
    )

    page = list_history(service, start_history_id="1", page_size=10)

    assert page.next_page_token == "page-2"


def test_page_token_is_forwarded_to_the_request() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response()

    list_history(
        service,
        start_history_id="1",
        page_size=10,
        page_token="page-2",
    )

    list_mock.assert_called_once_with(
        userId="me",
        startHistoryId="1",
        maxResults=10,
        pageToken="page-2",
    )


def test_blank_request_page_token_is_omitted() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response()

    list_history(
        service,
        start_history_id="1",
        page_size=10,
        page_token="   ",
    )

    list_mock.assert_called_once_with(
        userId="me",
        startHistoryId="1",
        maxResults=10,
    )


def test_non_string_request_page_token_is_rejected() -> None:
    service, list_mock = make_service()

    with pytest.raises(ValueError, match="page_token"):
        list_history(service, start_history_id="1", page_size=10, page_token=5)  # type: ignore[arg-type]

    list_mock.assert_not_called()


def test_empty_response_next_page_token_means_end_of_results() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response(
        next_page_token="   ",
    )

    page = list_history(service, start_history_id="1", page_size=10)

    assert page.next_page_token is None


def test_malformed_response_next_page_token_is_rejected() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response(
        next_page_token=99,
    )

    with pytest.raises(GmailApiError, match="pagination token"):
        list_history(service, start_history_id="1", page_size=10)


def test_next_page_token_is_not_interpreted_by_this_client() -> None:
    """A single-page client has no loop, so tokens pass through unmodified."""

    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response(
        next_page_token="page-2",
    )

    page = list_history(
        service,
        start_history_id="1",
        page_size=10,
        page_token="page-1",
    )

    assert page.next_page_token == "page-2"


def test_configured_page_size_is_used_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response()
    monkeypatch.setattr(
        history,
        "get_settings",
        lambda: SimpleNamespace(gmail_page_size=23),
    )

    list_history(service, start_history_id="1")

    list_mock.assert_called_once_with(
        userId="me",
        startHistoryId="1",
        maxResults=23,
    )


@pytest.mark.parametrize("page_size", (0, -1, 501, True, "10"))
def test_invalid_page_size_is_rejected(page_size: object) -> None:
    service, list_mock = make_service()

    with pytest.raises(ValueError, match="page_size"):
        list_history(
            service,
            start_history_id="1",
            page_size=page_size,  # type: ignore[arg-type]
        )

    list_mock.assert_not_called()


@pytest.mark.parametrize("start_history_id", (None, "", "   ", 123))
def test_invalid_start_history_id_is_rejected(start_history_id: object) -> None:
    service, list_mock = make_service()

    with pytest.raises(ValueError, match="start_history_id"):
        list_history(
            service,
            start_history_id=start_history_id,  # type: ignore[arg-type]
            page_size=10,
        )

    list_mock.assert_not_called()


def test_missing_history_id_is_rejected() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = {"history": []}

    with pytest.raises(GmailApiError, match="historyId"):
        list_history(service, start_history_id="1", page_size=10)


@pytest.mark.parametrize("history_id", ("", "   ", 123, {"id": "1"}))
def test_malformed_history_id_is_rejected(history_id: object) -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response(
        history_id=history_id,
    )

    with pytest.raises(GmailApiError, match="historyId"):
        list_history(service, start_history_id="1", page_size=10)


def test_non_dict_history_response_is_rejected() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = None

    with pytest.raises(GmailApiError, match="unexpected history response"):
        list_history(service, start_history_id="1", page_size=10)


def test_malformed_history_structure_is_rejected() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response(records={})

    with pytest.raises(GmailApiError, match="history records"):
        list_history(service, start_history_id="1", page_size=10)


def test_malformed_history_record_is_rejected() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response(
        records=["not-a-record"],
    )

    with pytest.raises(GmailApiError, match="history record"):
        list_history(service, start_history_id="1", page_size=10)


def test_malformed_messages_added_payload_is_rejected() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response(
        records=[{"messagesAdded": "not-a-list"}],
    )

    with pytest.raises(GmailApiError, match="messagesAdded payload"):
        list_history(service, start_history_id="1", page_size=10)


def test_malformed_messages_added_entry_is_rejected() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response(
        records=[{"messagesAdded": ["not-an-entry"]}],
    )

    with pytest.raises(GmailApiError, match="messagesAdded entry"):
        list_history(service, start_history_id="1", page_size=10)


@pytest.mark.parametrize("entry", ({"threadId": "thread-1"}, {"message": "bad"}))
def test_messages_added_without_message_object_is_rejected(
    entry: dict[str, object],
) -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response(
        records=[{"messagesAdded": [entry]}],
    )

    with pytest.raises(GmailApiError, match="without a message"):
        list_history(service, start_history_id="1", page_size=10)


@pytest.mark.parametrize("message", ({"threadId": "thread-1"}, {"id": "   "}, {"id": 42}))
def test_messages_added_without_usable_id_is_rejected(
    message: dict[str, object],
) -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.return_value = history_response(
        records=[{"messagesAdded": [{"message": message}]}],
    )

    with pytest.raises(GmailApiError, match="usable id"):
        list_history(service, start_history_id="1", page_size=10)


def test_404_is_classified_as_expired_history() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.side_effect = http_error(404)

    with patch.object(transport.time, "sleep") as sleep_mock:
        with pytest.raises(GmailHistoryExpiredError) as excinfo:
            list_history(service, start_history_id="1", page_size=10)

    error = excinfo.value
    assert isinstance(error, GmailNotFoundError)
    assert isinstance(error, GmailApiError)
    message = str(error)
    assert "404" in message
    assert "full synchronization" in message
    assert "sensitive-response-body" not in message
    assert error.__cause__ is None
    assert error.__context__ is None
    assert list_mock.return_value.execute.call_count == 1
    sleep_mock.assert_not_called()
    rendered = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    assert "sensitive-response-body" not in rendered


@pytest.mark.parametrize("status", (401, 403))
def test_authorization_failures_are_not_expired_history(status: int) -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.side_effect = http_error(status)

    with patch.object(transport.time, "sleep") as sleep_mock:
        with pytest.raises(GmailApiError) as excinfo:
            list_history(service, start_history_id="1", page_size=10)

    error = excinfo.value
    assert not isinstance(error, GmailNotFoundError)
    assert not isinstance(error, GmailHistoryExpiredError)
    assert str(status) in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert list_mock.return_value.execute.call_count == 1
    sleep_mock.assert_not_called()


def test_403_mentions_permission_checks() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.side_effect = http_error(403)

    with pytest.raises(GmailApiError) as excinfo:
        list_history(service, start_history_id="1", page_size=10)

    message = str(excinfo.value).lower()
    assert "scope" in message
    assert "enabled" in message


def test_rate_limit_is_retried() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.side_effect = [
        http_error(429),
        history_response(records=[]),
    ]

    with patch.object(transport.time, "sleep") as sleep_mock:
        page = list_history(service, start_history_id="1", page_size=10)

    assert page.message_ids == ()
    assert list_mock.return_value.execute.call_count == 2
    sleep_mock.assert_called_once_with(0.5)


def test_transient_server_errors_are_retried_then_reported() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.side_effect = [
        http_error(500),
        http_error(503),
        http_error(502),
    ]

    with patch.object(transport.time, "sleep") as sleep_mock:
        with pytest.raises(GmailApiError) as excinfo:
            list_history(service, start_history_id="1", page_size=10)

    assert "temporarily failed" in str(excinfo.value).lower()
    assert "sensitive-response-body" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert list_mock.return_value.execute.call_count == 3
    assert sleep_mock.call_count == 2


def test_transport_errors_are_retried_then_reported() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.side_effect = [
        TransportError("boom-1"),
        TransportError("boom-2"),
        TransportError("boom-3"),
    ]

    with patch.object(transport.time, "sleep") as sleep_mock:
        with pytest.raises(GmailApiError) as excinfo:
            list_history(service, start_history_id="1", page_size=10)

    message = str(excinfo.value)
    assert "transient errors" in message
    assert "TransportError" in message
    assert "boom" not in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert list_mock.return_value.execute.call_count == 3
    assert sleep_mock.call_count == 2


def test_httplib2_errors_are_retried_then_succeed() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.side_effect = [
        HttpLib2Error("boom"),
        history_response(),
    ]

    with patch.object(transport.time, "sleep") as sleep_mock:
        page = list_history(service, start_history_id="1", page_size=10)

    assert page.history_id == "100"
    assert list_mock.return_value.execute.call_count == 2
    sleep_mock.assert_called_once_with(0.5)


def test_unexpected_error_is_sanitized_without_retry() -> None:
    service, list_mock = make_service()
    list_mock.return_value.execute.side_effect = RuntimeError(
        "sensitive-response-body"
    )

    with patch.object(transport.time, "sleep") as sleep_mock:
        with pytest.raises(GmailApiError) as excinfo:
            list_history(service, start_history_id="1", page_size=10)

    error = excinfo.value
    assert "RuntimeError" in str(error)
    assert "sensitive-response-body" not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert list_mock.return_value.execute.call_count == 1
    sleep_mock.assert_not_called()
    rendered = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    assert "sensitive-response-body" not in rendered
