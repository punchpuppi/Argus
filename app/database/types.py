"""Custom SQLAlchemy column types used by the Argus persistence layer."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Store timezone-aware datetimes as naive UTC and restore UTC on read.

    SQLite does not persist timezone offsets. This type normalises every value
    to UTC before it reaches the driver and re-attaches ``UTC`` when a value is
    loaded, so application code always receives timezone-aware datetimes from
    any supported dialect.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(
        self,
        value: datetime | None,
        dialect: Dialect,
    ) -> datetime | None:
        if value is None:
            return None

        if value.tzinfo is None:
            # Treat naive input as UTC rather than silently assuming local time.
            value = value.replace(tzinfo=UTC)

        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(
        self,
        value: datetime | None,
        dialect: Dialect,
    ) -> datetime | None:
        if value is None:
            return None

        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)

        return value.astimezone(UTC)
