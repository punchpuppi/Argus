"""Errors raised by the synchronization layer.

Messages are intended for operators and logs. They must never contain email
subjects, snippets, bodies, provider payloads, OAuth credentials, or tokens;
only account identifiers and structural details belong here.
"""

from __future__ import annotations


class SyncError(Exception):
    """Base error for synchronization-layer failures."""


class CheckpointConflictError(SyncError):
    """Raised when a checkpoint no longer matches the caller's expected state.

    This is optimistic state detection, not a lock or a cross-process
    exclusion guarantee.
    """
