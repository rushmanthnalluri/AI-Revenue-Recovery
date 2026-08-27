"""Read-side history for the stateful policy guards (rate limits, duplicate
protection, per-incident / per-strategy stopping rules).

The engine core is a deterministic function over (ActionContext, PolicyConfig,
PolicyHistory). `SqlPolicyHistory` derives every signal from the shared
recovery tables; tests may substitute any object satisfying the protocol.

Conventions:
- An action "consumed budget" unless it ended REJECTED or CANCELLED (those
  never reached the gateway); only budget-consuming actions count against
  rate limits.
- For duplicate protection an action is "active" unless it conclusively ended
  REJECTED / CANCELLED / FAILED. RECOVERED and UNKNOWN stay active: never
  double-collect, never re-fire an action whose outcome is unclear.
- `exclude_action_id` must be the id of the action currently being evaluated
  (if the caller already persisted it as PROPOSED), so an action never counts
  against itself.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import RecoveryAction, RecoveryOpportunity
from app.ports import ActionType, RecoveryStatus

_BUDGET_EXCLUDED = (RecoveryStatus.REJECTED, RecoveryStatus.CANCELLED)
_DUPLICATE_INACTIVE = (
    RecoveryStatus.REJECTED,
    RecoveryStatus.CANCELLED,
    RecoveryStatus.FAILED,
)

# Streak queries only need enough rows to cover any sane configured threshold.
_STREAK_SCAN_LIMIT = 64


@runtime_checkable
class PolicyHistory(Protocol):
    """History signals the policy engine may consult. All counts exclude the
    action identified by `exclude_action_id` (the action under evaluation)."""

    def count_actions_for_incident(
        self, incident_id: str, *, exclude_action_id: str | None = None
    ) -> int: ...

    def count_actions_for_customer_since(
        self, customer_id: str, since: datetime, *, exclude_action_id: str | None = None
    ) -> int: ...

    def count_actions_global_since(
        self, since: datetime, *, exclude_action_id: str | None = None
    ) -> int: ...

    def last_active_action_at(
        self,
        customer_id: str,
        action_type: ActionType,
        *,
        exclude_action_id: str | None = None,
    ) -> datetime | None: ...

    def consecutive_failed_for_incident(
        self, incident_id: str, *, exclude_action_id: str | None = None
    ) -> int: ...

    def consecutive_failed_for_strategy(
        self, strategy_id: str, *, exclude_action_id: str | None = None
    ) -> int: ...


class SqlPolicyHistory:
    """PolicyHistory over the shared recovery tables (SQLAlchemy 2 sync)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- rate-limit counts -----------------------------------------------------

    def count_actions_for_incident(
        self, incident_id: str, *, exclude_action_id: str | None = None
    ) -> int:
        stmt = (
            sa.select(sa.func.count())
            .select_from(RecoveryAction)
            .where(
                RecoveryAction.incident_id == incident_id,
                RecoveryAction.status.notin_(_BUDGET_EXCLUDED),
            )
        )
        return self._count(stmt, exclude_action_id)

    def count_actions_for_customer_since(
        self, customer_id: str, since: datetime, *, exclude_action_id: str | None = None
    ) -> int:
        stmt = (
            sa.select(sa.func.count())
            .select_from(RecoveryAction)
            .join(RecoveryOpportunity, RecoveryAction.opportunity_id == RecoveryOpportunity.id)
            .where(
                RecoveryOpportunity.customer_id == customer_id,
                RecoveryAction.created_at >= since,
                RecoveryAction.status.notin_(_BUDGET_EXCLUDED),
            )
        )
        return self._count(stmt, exclude_action_id)

    def count_actions_global_since(
        self, since: datetime, *, exclude_action_id: str | None = None
    ) -> int:
        stmt = (
            sa.select(sa.func.count())
            .select_from(RecoveryAction)
            .where(
                RecoveryAction.created_at >= since,
                RecoveryAction.status.notin_(_BUDGET_EXCLUDED),
            )
        )
        return self._count(stmt, exclude_action_id)

    # -- duplicate protection ---------------------------------------------------

    def last_active_action_at(
        self,
        customer_id: str,
        action_type: ActionType,
        *,
        exclude_action_id: str | None = None,
    ) -> datetime | None:
        stmt = (
            sa.select(sa.func.max(RecoveryAction.created_at))
            .select_from(RecoveryAction)
            .join(RecoveryOpportunity, RecoveryAction.opportunity_id == RecoveryOpportunity.id)
            .where(
                RecoveryOpportunity.customer_id == customer_id,
                RecoveryAction.action_type == action_type,
                RecoveryAction.status.notin_(_DUPLICATE_INACTIVE),
            )
        )
        if exclude_action_id is not None:
            stmt = stmt.where(RecoveryAction.id != exclude_action_id)
        return self._session.execute(stmt).scalar_one()

    # -- stopping-rule streaks ---------------------------------------------------

    def consecutive_failed_for_incident(
        self, incident_id: str, *, exclude_action_id: str | None = None
    ) -> int:
        return self._leading_failed(
            RecoveryAction.incident_id == incident_id, exclude_action_id=exclude_action_id
        )

    def consecutive_failed_for_strategy(
        self, strategy_id: str, *, exclude_action_id: str | None = None
    ) -> int:
        return self._leading_failed(
            RecoveryAction.strategy_id == strategy_id, exclude_action_id=exclude_action_id
        )

    # -- internals ----------------------------------------------------------------

    def _count(self, stmt: sa.Select, exclude_action_id: str | None) -> int:
        if exclude_action_id is not None:
            stmt = stmt.where(RecoveryAction.id != exclude_action_id)
        return int(self._session.execute(stmt).scalar_one())

    def _leading_failed(self, *where, exclude_action_id: str | None = None) -> int:
        stmt = (
            sa.select(RecoveryAction.status)
            .where(*where)
            .order_by(RecoveryAction.created_at.desc(), RecoveryAction.id.desc())
            .limit(_STREAK_SCAN_LIMIT)
        )
        if exclude_action_id is not None:
            stmt = stmt.where(RecoveryAction.id != exclude_action_id)
        streak = 0
        for (status,) in self._session.execute(stmt):
            if status == RecoveryStatus.FAILED:
                streak += 1
            else:
                break
        return streak


__all__ = ["PolicyHistory", "SqlPolicyHistory"]
