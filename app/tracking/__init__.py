"""Tracking rules, matching, and status detection."""

from app.tracking.engine import (
    REASON_CREATED,
    REASON_DUPLICATE_EVENT,
    REASON_NO_STATUS_CUE,
    REASON_OUT_OF_ORDER,
    REASON_STATUS_UNCHANGED,
    EmailProcessingResult,
    RuleOutcome,
    process_email,
)
from app.tracking.errors import TrackingError
from app.tracking.matching import RuleCriteria, email_matches_rule
from app.tracking.rules import get_active_rules
from app.tracking.status import StatusDetection, detect_status

__all__ = [
    "REASON_CREATED",
    "REASON_DUPLICATE_EVENT",
    "REASON_NO_STATUS_CUE",
    "REASON_OUT_OF_ORDER",
    "REASON_STATUS_UNCHANGED",
    "EmailProcessingResult",
    "RuleCriteria",
    "RuleOutcome",
    "StatusDetection",
    "TrackingError",
    "detect_status",
    "email_matches_rule",
    "get_active_rules",
    "process_email",
]
