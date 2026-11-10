"""Retrieval of tracking rules that may currently be evaluated.

Only rules owned by the requested account, currently active, and not
soft-deleted are relevant. Accounts that are inactive or soft-deleted are
excluded as well, so a disabled mailbox cannot generate new tracking events.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from app.database.enums import RuleState
from app.database.models import EmailAccount, TrackingRule


def get_active_rules(session: Session, account_id: int) -> list[TrackingRule]:
    """Return the active, non-deleted rules belonging to an available account.

    The query filters in the database rather than in Python and loads only the
    columns needed for matching, avoiding unnecessary row payloads.
    """

    statement = (
        select(TrackingRule)
        .join(TrackingRule.account)
        .where(
            TrackingRule.account_id == account_id,
            TrackingRule.state == RuleState.ACTIVE,
            TrackingRule.deleted_at.is_(None),
            EmailAccount.is_active.is_(True),
            EmailAccount.deleted_at.is_(None),
        )
        .options(
            load_only(
                TrackingRule.id,
                TrackingRule.account_id,
                TrackingRule.name,
                TrackingRule.sender_pattern,
                TrackingRule.required_keywords,
                TrackingRule.reference_number,
                TrackingRule.state,
            )
        )
        .order_by(TrackingRule.id)
    )

    return list(session.scalars(statement))
