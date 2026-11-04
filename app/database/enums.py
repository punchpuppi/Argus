"""Enumerations shared by the persistence and domain layers."""

from __future__ import annotations

from enum import StrEnum


class ProviderName(StrEnum):
    """Supported email providers."""

    GMAIL = "gmail"
    OUTLOOK = "outlook"


class RuleState(StrEnum):
    """Lifecycle state of a tracking rule."""

    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"


class ArgusStatus(StrEnum):
    """Provider-independent tracking status values."""

    UNKNOWN = "unknown"
    ORDER_PLACED = "order_placed"
    SHIPPED = "shipped"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    DELAYED = "delayed"
    CANCELLED = "cancelled"


class NotificationChannel(StrEnum):
    """Supported notification channels."""

    TELEGRAM = "telegram"


class NotificationState(StrEnum):
    """Delivery state of a notification."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
