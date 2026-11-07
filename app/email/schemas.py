"""Provider-independent normalized email representation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.database.enums import ProviderName


@dataclass(frozen=True)
class NormalizedEmail:
    """Metadata for one email message.

    Full message bodies are deliberately not represented or stored. All
    timestamp values are timezone-aware UTC datetimes.
    """

    provider: ProviderName
    provider_message_id: str
    thread_id: str | None
    internet_message_id: str | None
    sender: str | None
    recipients: tuple[str, ...]
    subject: str | None
    snippet: str | None
    received_at: datetime | None
    labels: tuple[str, ...]
