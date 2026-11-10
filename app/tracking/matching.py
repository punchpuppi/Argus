"""Provider-independent matching of normalized emails against tracking rules.

Matching is deliberately pure: it operates on :class:`~app.email.schemas.NormalizedEmail`
values and on a small, ORM-free projection of :class:`~app.database.models.TrackingRule`.
That keeps the decision logic testable without a database and free of any
Gmail-specific assumptions.

Only message metadata is inspected. Full message bodies are never fetched,
stored, or interpreted; the text examined here is the subject and the provider
snippet, both of which are already part of the normalized schema.

Matching semantics:

* ``sender_pattern`` containing ``@`` requires an exact normalised address.
  Otherwise it is a domain that must equal the sender domain or be a dotted
  parent of it, so ``shop.example`` accepts ``a@shop.example`` and
  ``a@mail.shop.example`` but never ``a@shop.example.attacker.net``.
* ``reference_number`` matches on token boundaries, so ``1234`` cannot match
  inside ``12345`` while punctuation around it is tolerated.
* ``required_keywords`` remain intentional substring matches and are not
  boundary-checked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.utils import parseaddr

from app.database.models import TrackingRule
from app.email.schemas import NormalizedEmail

# Defensive cap on how much text is inspected per message. Snippets are short by
# design; the bound simply keeps matching cost predictable for malformed input.
_MAX_SEARCH_TEXT = 4096

# Characters that continue an identifier. A reference number may not be adjacent
# to any of these on either side.
_TOKEN_CHARACTERS = "0-9a-z_"


def _clean(value: str | None) -> str | None:
    """Return a stripped, case-folded value, or ``None`` when unusable."""

    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned.casefold() if cleaned else None


def _sender_address(sender: str | None) -> str:
    """Extract the address from a ``From`` header value, tolerating odd input.

    ``email.utils`` never executes or interprets content; it only splits a
    display name from an address. Malformed values fall back to the raw text so
    that matching still behaves deterministically.
    """

    if not isinstance(sender, str):
        return ""
    try:
        _display_name, address = parseaddr(sender)
    except (TypeError, ValueError):  # pragma: no cover - parseaddr is tolerant
        address = ""
    candidate = address or sender
    return candidate.strip().casefold()


def _searchable_text(email: NormalizedEmail) -> str:
    """Return the lower-cased subject/snippet text used for keyword matching."""

    parts: list[str] = []
    for value in (email.subject, email.snippet):
        if isinstance(value, str) and value.strip():
            parts.append(value)
    if not parts:
        return ""
    return "\n".join(parts)[:_MAX_SEARCH_TEXT].casefold()


def _sender_matches(pattern: str, sender: str | None) -> bool:
    """Return whether a normalised sender pattern matches the sender address.

    The pattern is either a complete address (contains ``@``) or a domain. A
    domain must equal the sender's domain or be a dotted parent of it; arbitrary
    suffix matches are rejected so look-alike domains cannot satisfy a rule.
    """

    address = _sender_address(sender)
    if not address:
        return False

    if "@" in pattern:
        return address == pattern

    domain = address.rsplit("@", 1)[1] if "@" in address else address
    return domain == pattern or domain.endswith(f".{pattern}")


def _contains_token(text: str, value: str) -> bool:
    """Return whether ``value`` appears in ``text`` on identifier boundaries."""

    boundary_start = f"(?<![{_TOKEN_CHARACTERS}])"
    boundary_end = f"(?![{_TOKEN_CHARACTERS}])"
    expression = re.compile(f"{boundary_start}{re.escape(value)}{boundary_end}")
    return expression.search(text) is not None


@dataclass(frozen=True)
class RuleCriteria:
    """The matching fields of a tracking rule, detached from the ORM.

    ``sender_pattern`` is a case-insensitive address or domain, matched on
    domain boundaries. ``required_keywords`` are case-insensitive substrings
    that must all appear in the subject or snippet. ``reference_number`` must
    appear as a whole token in the subject or snippet.
    """

    sender_pattern: str | None
    required_keywords: tuple[str, ...]
    reference_number: str | None

    def __post_init__(self) -> None:
        """Canonicalise criteria so matching is case-insensitive by construction."""

        object.__setattr__(self, "sender_pattern", _clean(self.sender_pattern))
        object.__setattr__(self, "reference_number", _clean(self.reference_number))
        keywords = tuple(
            keyword
            for keyword in (_clean(value) for value in self.required_keywords)
            if keyword is not None
        )
        object.__setattr__(self, "required_keywords", keywords)

    @classmethod
    def from_rule(cls, rule: TrackingRule) -> "RuleCriteria":
        """Project a persisted rule onto its matching criteria."""

        return cls(
            sender_pattern=rule.sender_pattern,
            required_keywords=tuple(rule.required_keywords or ()),
            reference_number=rule.reference_number,
        )

    @property
    def has_criteria(self) -> bool:
        """Whether the rule defines anything to match against.

        A rule with no populated criteria is treated as matching nothing rather
        than as an implicit catch-all, so an accidentally empty rule cannot
        record status events for every message in the mailbox.
        """

        return bool(self.sender_pattern or self.required_keywords or self.reference_number)


def email_matches_rule(criteria: RuleCriteria, email: NormalizedEmail) -> bool:
    """Return whether an email satisfies every populated rule criterion.

    Missing or malformed metadata simply fails to satisfy the relevant
    criterion; matching never raises for ``None`` senders or subjects.
    """

    if not criteria.has_criteria:
        return False

    if criteria.sender_pattern is not None:
        if not _sender_matches(criteria.sender_pattern, email.sender):
            return False

    text = _searchable_text(email)

    for keyword in criteria.required_keywords:
        if keyword not in text:
            return False

    if criteria.reference_number is not None:
        if not _contains_token(text, criteria.reference_number):
            return False

    return True
