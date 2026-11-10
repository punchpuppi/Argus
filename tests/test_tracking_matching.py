"""Tests for provider-independent rule matching and status-cue detection."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.database.enums import ArgusStatus, ProviderName
from app.database.models import TrackingRule
from app.email.schemas import NormalizedEmail
from app.tracking.matching import RuleCriteria, email_matches_rule
from app.tracking.status import StatusDetection, detect_status

RECEIVED_AT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def make_email(
    *,
    message_id: str = "message-1",
    sender: str | None = "Shop <no-reply@shop.example>",
    subject: str | None = "Your order has shipped",
    snippet: str | None = None,
) -> NormalizedEmail:
    return NormalizedEmail(
        provider=ProviderName.GMAIL,
        provider_message_id=message_id,
        thread_id="thread-1",
        internet_message_id=None,
        sender=sender,
        recipients=("user@example.com",),
        subject=subject,
        snippet=snippet,
        received_at=RECEIVED_AT,
        labels=(),
    )


def make_criteria(
    *,
    sender_pattern: str | None = "shop.example",
    required_keywords: tuple[str, ...] = ("order",),
    reference_number: str | None = None,
) -> RuleCriteria:
    return RuleCriteria(
        sender_pattern=sender_pattern,
        required_keywords=required_keywords,
        reference_number=reference_number,
    )


def test_email_matches_all_populated_criteria() -> None:
    email = make_email(subject="Your order ABC123 has shipped")

    assert email_matches_rule(
        make_criteria(reference_number="abc123"),
        email,
    )


def test_rule_without_criteria_matches_nothing() -> None:
    email = make_email()

    assert not email_matches_rule(
        make_criteria(sender_pattern=None, required_keywords=()),
        email,
    )


def test_missing_keyword_prevents_match() -> None:
    email = make_email(subject="Your order has shipped")

    assert not email_matches_rule(
        make_criteria(required_keywords=("order", "invoice")),
        email,
    )


def test_sender_pattern_matches_display_name_sender() -> None:
    email = make_email(sender="Shop Support <no-reply@shop.example>")

    assert email_matches_rule(make_criteria(), email)


def test_sender_pattern_must_match_sender_address() -> None:
    email = make_email(sender="Someone <other@elsewhere.example>")

    assert not email_matches_rule(make_criteria(), email)


def test_matching_is_case_insensitive() -> None:
    email = make_email(
        sender="SHOP <NO-REPLY@SHOP.EXAMPLE>",
        subject="YOUR ORDER HAS SHIPPED",
    )

    assert email_matches_rule(
        make_criteria(sender_pattern="SHOP.EXAMPLE", required_keywords=("Order",)),
        email,
    )


def test_reference_number_must_appear_in_metadata() -> None:
    email = make_email(subject="Your order has shipped", snippet="Tracking 1Z999")

    assert email_matches_rule(make_criteria(reference_number="1z999"), email)
    assert not email_matches_rule(make_criteria(reference_number="missing-ref"), email)


def test_snippet_is_searched_when_subject_lacks_keyword() -> None:
    email = make_email(subject="Update from the shop", snippet="Your order is confirmed")

    assert email_matches_rule(make_criteria(required_keywords=("order",)), email)


def test_missing_metadata_fails_safely_without_raising() -> None:
    email = make_email(sender=None, subject=None, snippet=None)

    assert not email_matches_rule(make_criteria(), email)


def test_malformed_sender_does_not_raise() -> None:
    email = make_email(sender="not an address <<<")

    assert email_matches_rule(make_criteria(required_keywords=("order",)), email) is False


def test_criteria_from_rule_ignores_blank_values() -> None:
    rule = TrackingRule(
        name="Order tracking",
        sender_pattern="  ",
        required_keywords=["", "   ", "Order"],
        reference_number="   ",
    )

    criteria = RuleCriteria.from_rule(rule)

    assert criteria.sender_pattern is None
    assert criteria.required_keywords == ("order",)
    assert criteria.reference_number is None
    assert criteria.has_criteria


def test_criteria_from_rule_without_matching_fields_has_no_criteria() -> None:
    rule = TrackingRule(name="Empty rule")

    assert not RuleCriteria.from_rule(rule).has_criteria


@pytest.mark.parametrize(
    ("subject", "expected"),
    (
        ("Your order has been confirmed", ArgusStatus.ORDER_PLACED),
        ("Your order has shipped", ArgusStatus.SHIPPED),
        ("Your order is out for delivery", ArgusStatus.OUT_FOR_DELIVERY),
        ("Your order was delivered", ArgusStatus.DELIVERED),
        ("Your order has been delayed", ArgusStatus.DELAYED),
        ("Your order was cancelled", ArgusStatus.CANCELLED),
    ),
)
def test_detects_expected_status_from_subject(
    subject: str,
    expected: ArgusStatus,
) -> None:
    detection = detect_status(make_email(subject=subject))

    assert detection is not None
    assert detection.status is expected


def test_status_detection_is_case_insensitive() -> None:
    detection = detect_status(make_email(subject="YOUR ORDER HAS SHIPPED"))

    assert detection is not None
    assert detection.status is ArgusStatus.SHIPPED


def test_specific_cue_wins_over_broader_cue() -> None:
    detection = detect_status(
        make_email(subject="Your order has shipped and is out for delivery")
    )

    assert detection is not None
    assert detection.status is ArgusStatus.OUT_FOR_DELIVERY
    assert detection.matched_cue == "out for delivery"


def test_snippet_participates_in_status_detection() -> None:
    detection = detect_status(
        make_email(subject="Shipment update", snippet="Your parcel was delivered")
    )

    assert detection == StatusDetection(
        status=ArgusStatus.DELIVERED,
        matched_cue="delivered",
    )


def test_email_without_status_cue_returns_none() -> None:
    assert detect_status(make_email(subject="Your order details")) is None


def test_email_without_text_returns_none() -> None:
    assert detect_status(make_email(subject=None, snippet=None)) is None


def test_cancellation_takes_priority_over_delay() -> None:
    detection = detect_status(
        make_email(subject="Your delayed order has been cancelled")
    )

    assert detection is not None
    assert detection.status is ArgusStatus.CANCELLED


def test_matched_cue_is_the_phrase_not_the_whole_subject() -> None:
    subject = "Your order has shipped - tracking ABC123"

    detection = detect_status(make_email(subject=subject))

    assert detection is not None
    assert detection.matched_cue == "shipped"
    assert "ABC123" not in detection.matched_cue


@pytest.mark.parametrize(
    "subject",
    (
        "Your order has not shipped yet",
        "Your order has never shipped",
        "Your order hasn't been shipped",
        "Your order wasn't shipped",
        "Your order is unshipped",
        "Your package is undelivered",
        "The delivery is not delayed",
        "Your order has not been delivered",
        "Your order is no longer delayed",
    ),
)
def test_negated_status_cues_produce_no_detection(subject: str) -> None:
    assert detect_status(make_email(subject=subject)) is None


@pytest.mark.parametrize(
    "subject",
    (
        "Your order was not only shipped but delivered",
        "Your order is not only shipped but delivered",
    ),
)
def test_additive_phrase_is_not_treated_as_negation(subject: str) -> None:
    detection = detect_status(make_email(subject=subject))

    assert detection is not None
    assert detection.status is ArgusStatus.DELIVERED


def test_negation_scope_ends_at_a_contrastive_clause() -> None:
    detection = detect_status(
        make_email(subject="Your order has not shipped but was delivered")
    )

    assert detection is not None
    assert detection.status is ArgusStatus.DELIVERED


def test_negation_scope_ends_at_punctuation() -> None:
    detection = detect_status(
        make_email(subject="Your order has not shipped. It has now shipped.")
    )

    assert detection is not None
    assert detection.status is ArgusStatus.SHIPPED


@pytest.mark.parametrize(
    "subject",
    (
        "Your order is not delayed and has shipped",
        "Your order is not delayed and shipped",
        "Your order is not delayed, and shipped",
    ),
)
def test_coordination_does_not_propagate_negation(subject: str) -> None:
    detection = detect_status(make_email(subject=subject))

    assert detection is not None
    assert detection.status is ArgusStatus.SHIPPED
    assert detection.matched_cue == "shipped"


def test_each_negated_cue_is_suppressed_independently() -> None:
    assert (
        detect_status(make_email(subject="Your order has not shipped and not delivered"))
        is None
    )


def test_contrastive_clause_preserves_the_later_positive_cue() -> None:
    detection = detect_status(
        make_email(subject="Your order is not delayed but now delivered")
    )

    assert detection is not None
    assert detection.status is ArgusStatus.DELIVERED


def test_later_cue_position_wins_over_earlier_delay() -> None:
    detection = detect_status(
        make_email(subject="Your order was delayed but is now delivered")
    )

    assert detection is not None
    assert detection.status is ArgusStatus.DELIVERED


def test_later_cue_position_wins_over_earlier_cancellation() -> None:
    detection = detect_status(
        make_email(
            subject="Your order was cancelled but the replacement has shipped"
        )
    )

    assert detection is not None
    assert detection.status is ArgusStatus.SHIPPED


@pytest.mark.parametrize(
    "sender",
    (
        "no-reply@shop.example",
        "Orders <no-reply@shop.example>",
        "alerts@mail.shop.example",
        "alerts@notifications.mail.shop.example",
        "Shop.example",
    ),
)
def test_sender_domain_matches_legitimate_addresses(sender: str) -> None:
    assert email_matches_rule(
        make_criteria(required_keywords=()),
        make_email(sender=sender),
    )


@pytest.mark.parametrize(
    "sender",
    (
        "no-reply@shop.example.attacker.net",
        "no-reply@shop.exampleattacker.net",
        "no-reply@notshop.example",
        "no-reply@bookshop.example",
    ),
)
def test_sender_domain_rejects_lookalike_addresses(sender: str) -> None:
    assert not email_matches_rule(
        make_criteria(required_keywords=()),
        make_email(sender=sender),
    )


def test_full_address_pattern_requires_an_exact_match() -> None:
    criteria = make_criteria(
        sender_pattern="no-reply@shop.example",
        required_keywords=(),
    )

    assert email_matches_rule(criteria, make_email(sender="No-Reply <no-reply@shop.example>"))
    assert not email_matches_rule(criteria, make_email(sender="other@shop.example"))
    assert not email_matches_rule(criteria, make_email(sender="no-reply@mail.shop.example"))


@pytest.mark.parametrize(
    "subject",
    (
        "Your order 1234 has shipped",
        "Your order (1234) has shipped",
        "Your order REF:1234. has shipped",
        "Your order ABC-1234 has shipped",
    ),
)
def test_reference_number_matches_on_token_boundaries(subject: str) -> None:
    assert email_matches_rule(
        make_criteria(reference_number="1234"),
        make_email(subject=subject),
    )


@pytest.mark.parametrize(
    "subject",
    (
        "Your order 12345 has shipped",
        "Your order 123456 has shipped",
        "Your order X1234 has shipped",
        "Your order 1234A has shipped",
    ),
)
def test_reference_number_does_not_match_partial_tokens(subject: str) -> None:
    assert not email_matches_rule(
        make_criteria(reference_number="1234"),
        make_email(subject=subject),
    )


def test_reference_number_matching_is_case_insensitive() -> None:
    assert email_matches_rule(
        make_criteria(reference_number="abc-1234"),
        make_email(subject="Your order ABC-1234 has shipped"),
    )


def test_keywords_remain_intentional_substring_matches() -> None:
    assert email_matches_rule(
        make_criteria(required_keywords=("order",)),
        make_email(subject="Preorder bonus available"),
    )
