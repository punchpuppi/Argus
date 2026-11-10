"""Provider-independent status-cue detection for normalized emails.

The persisted model already defines the set of tracking statuses in
:class:`~app.database.enums.ArgusStatus`; it does not define which email text
implies which status. This module supplies the smallest deterministic mapping
that can be derived from those existing status names, using fixed phrase cues
found in the subject/snippet metadata.

Design notes:

* Only metadata is inspected. Message bodies are never fetched or stored.
* Email text is treated purely as data. Cues are compared as inert substrings;
  nothing in an email is parsed as a command, expression, or instruction.
* **Recency by position.** Every non-negated cue match is collected, and the
  match that occurs latest in the text wins. This handles lifecycle summaries
  such as "delayed but now delivered" without needing a chronology model.
  ``STATUS_CUES`` order is retained only to break exact-position ties.
* **Position is a heuristic.** Later text usually means a more recent state, but
  this is not an understanding of email chronology. A message that lists an
  older status after the current one will be misread.
* **Negation is guarded narrowly and does not cross clause boundaries.** A cue
  is discarded when a negation token appears within a few tokens before it and
  the two are in the same clause, when the cue sits inside an ``un-`` prefix
  form (``undelivered``, ``unshipped``), or when an explicit phrase such as
  ``not only`` marks it as additive. Punctuation and coordinating or
  contrastive conjunctions end the negation scope, so ``not delayed and
  shipped`` reports SHIPPED while ``not shipped and not delivered`` reports
  nothing. Where a cue is genuinely unreachable, the guard errs towards
  suppression, because a missed event is preferable to an inverted one. It does
  not attempt sentence parsing.
* **Coordination is ambiguous.** Because ``and`` starts a new clause, an email
  that shares one negation across a coordinated pair (``not shipped and
  delivered``) yields DELIVERED rather than nothing. Merchant notifications
  normally repeat the negation per status, which the guard handles correctly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.database.enums import ArgusStatus
from app.email.schemas import NormalizedEmail

_MAX_SEARCH_TEXT = 4096

# Ordered by priority: earlier entries win exact-position ties during selection.
STATUS_CUES: tuple[tuple[ArgusStatus, tuple[str, ...]], ...] = (
    (
        ArgusStatus.CANCELLED,
        ("cancelled", "canceled", "cancellation", "has been canceled"),
    ),
    (
        ArgusStatus.DELAYED,
        ("delayed", "running late", "delivery delay", "has been delayed"),
    ),
    (
        ArgusStatus.OUT_FOR_DELIVERY,
        ("out for delivery", "with your courier", "with the courier"),
    ),
    (
        ArgusStatus.DELIVERED,
        ("delivered", "delivery complete", "was delivered"),
    ),
    (
        ArgusStatus.SHIPPED,
        ("shipped", "has been shipped", "dispatched", "on its way"),
    ),
    (
        ArgusStatus.ORDER_PLACED,
        (
            "order placed",
            "order confirmed",
            "order confirmation",
            "order received",
            "has been placed",
            "has been confirmed",
            "thank you for your order",
        ),
    ),
)

# Words that negate a following status cue. Contractions appear both with a
# straight apostrophe and without one because merchants write both forms.
_NEGATION_TOKENS = frozenset(
    {
        "not",
        "no",
        "never",
        "none",
        "neither",
        "nor",
        "without",
        "cannot",
        "can't",
        "cant",
        "couldn't",
        "couldnt",
        "didn't",
        "didnt",
        "doesn't",
        "doesnt",
        "don't",
        "dont",
        "hadn't",
        "hadnt",
        "hasn't",
        "hasnt",
        "haven't",
        "havent",
        "isn't",
        "isnt",
        "shouldn't",
        "shouldnt",
        "wasn't",
        "wasnt",
        "weren't",
        "werent",
        "won't",
        "wont",
        "wouldn't",
        "wouldnt",
    }
)

# Conjunctions mark a new clause, so a negation before them does not reach a cue
# after them: "not shipped but delivered" yields DELIVERED, and "not delayed and
# shipped" yields SHIPPED. Coordination is included because a negated predicate
# does not distribute across "and"; sentences that rely on a shared negation
# ("not shipped and delivered") are handled conservatively in the docstring.
_CLAUSE_BOUNDARY_TOKENS = frozenset(
    {"and", "but", "however", "although", "though", "whereas"}
)

# Punctuation ends the negation scope, including the newline joining subject and
# snippet so a negated subject cannot silence a positive snippet.
_PUNCTUATION = ".,;:!?\n"

_NEGATION_WINDOW = 3

_TOKEN_PATTERN = re.compile(r"[0-9a-z']+")


@dataclass(frozen=True)
class StatusDetection:
    """A status inferred from message metadata, plus the cue that matched."""

    status: ArgusStatus
    matched_cue: str


def _searchable_text(email: NormalizedEmail) -> str:
    """Return the lower-cased subject/snippet text used for cue detection.

    Typographic apostrophes are normalised so ``hasn’t`` and ``hasn't`` behave
    identically during negation checks.
    """

    parts: list[str] = []
    for value in (email.subject, email.snippet):
        if isinstance(value, str) and value.strip():
            parts.append(value)
    if not parts:
        return ""
    text = "\n".join(parts)[:_MAX_SEARCH_TEXT].casefold()
    return text.replace("\u2019", "'")


def _word_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """Return the bounds of the alphanumeric word enclosing ``start``/``end``."""

    word_start = start
    while word_start > 0 and text[word_start - 1].isalnum():
        word_start -= 1
    word_end = end
    while word_end < len(text) and text[word_end].isalnum():
        word_end += 1
    return word_start, word_end


def _has_negated_prefix(text: str, start: int, end: int, cue: str) -> bool:
    """Return whether the cue sits inside an ``un-`` form such as ``undelivered``."""

    word_start, word_end = _word_bounds(text, start, end)
    word = text[word_start:word_end]
    return word != cue and word.startswith("un") and word[2:].startswith(cue)


def _is_negated(
    text: str,
    tokens: list[tuple[int, int, str]],
    word_start: int,
    start: int,
    end: int,
    cue: str,
) -> bool:
    """Return whether a cue occurrence is negated by nearby context."""

    if _has_negated_prefix(text, start, end, cue):
        return True

    preceding = [token for token in tokens if token[1] <= word_start]
    examined = 0

    for index in range(len(preceding) - 1, -1, -1):
        _token_start, token_end, token_text = preceding[index]
        gap_end = preceding[index + 1][0] if index + 1 < len(preceding) else word_start

        if any(character in text[token_end:gap_end] for character in _PUNCTUATION):
            return False
        if token_text in _CLAUSE_BOUNDARY_TOKENS:
            return False
        if token_text in _NEGATION_TOKENS:
            following = preceding[index + 1][2] if index + 1 < len(preceding) else ""
            if following == "only":
                # "not only shipped but delivered" is additive, not negative.
                continue
            return True

        examined += 1
        if examined >= _NEGATION_WINDOW:
            return False

    return False


def _last_unnegated_position(
    text: str,
    tokens: list[tuple[int, int, str]],
    cue: str,
) -> int | None:
    """Return the position of the last non-negated occurrence of ``cue``."""

    found: int | None = None
    start = text.find(cue)

    while start != -1:
        end = start + len(cue)
        word_start, _word_end = _word_bounds(text, start, end)
        if not _is_negated(text, tokens, word_start, start, end, cue):
            found = start
        start = text.find(cue, start + 1)

    return found


def detect_status(email: NormalizedEmail) -> StatusDetection | None:
    """Return the inferred status and cue, or ``None`` when no cue is present.

    The winning status is the one whose non-negated cue appears latest in the
    text, with :data:`STATUS_CUES` order breaking exact-position ties.

    ``None`` means the email is a legitimate rule match but does not describe a
    status (or describes only negated statuses), so no tracking event should be
    recorded for it.
    """

    text = _searchable_text(email)
    if not text:
        return None

    tokens = [
        (match.start(), match.end(), match.group())
        for match in _TOKEN_PATTERN.finditer(text)
    ]

    best_position = -1
    best_priority = -1
    best_detection: StatusDetection | None = None

    for priority, (status, cues) in enumerate(STATUS_CUES):
        for cue in cues:
            position = _last_unnegated_position(text, tokens, cue)
            if position is None:
                continue
            if position > best_position or (
                position == best_position and priority < best_priority
            ):
                best_position = position
                best_priority = priority
                best_detection = StatusDetection(status=status, matched_cue=cue)

    return best_detection
