"""Errors raised by the tracking engine.

Tracking errors are intended for operators and logs. Their messages must never
contain email subjects, snippets, message bodies, provider payloads, or
credentials; only identifiers and structural details belong here.
"""

from __future__ import annotations


class TrackingError(Exception):
    """Raised when an email cannot be evaluated safely against tracking rules."""
