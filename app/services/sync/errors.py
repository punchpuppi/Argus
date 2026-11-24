"""Errors raised by the synchronization layer.

Messages are intended for operators and logs. They must never contain email
subjects, snippets, bodies, provider payloads, OAuth credentials, or tokens;
only account identifiers and structural details belong here.
"""

from __future__ import annotations


from datetime import datetime


class SyncError(Exception):
    """Base error for synchronization-layer failures."""


class SyncAlreadyRunningError(SyncError):
    """Raised when a synchronization run is already active for an account.

    This is a process-local guard only. It does not prevent another process
    from synchronizing the same account; optimistic checkpoint advancement
    remains the cross-process conflict detector.
    """


class SyncPaginationError(SyncError):
    """Raised when provider pagination cannot be followed safely.

    A repeated page token would otherwise loop forever or re-read the same
    page, so synchronization aborts without advancing the checkpoint.
    """


class CheckpointConflictError(SyncError):
    """Raised when a checkpoint no longer matches the caller's expected state.

    This is optimistic state detection, not a lock or a cross-process
    exclusion guarantee.
    """

    def __init__(
        self, message: str, *, advanced_cursor: str | None = None,
        cursor_updated_at: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.advanced_cursor = advanced_cursor
        self.cursor_updated_at = cursor_updated_at
