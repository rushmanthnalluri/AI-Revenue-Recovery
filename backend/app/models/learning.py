"""Durable, immutable recovery outcome observations for Phase-B learning."""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app import ids
from app.db import Base, TZDateTime, utcnow
from app.models.base import EnvironmentMixin
from app.ports import ActionType, RecoveryStatus


class RecoveryOutcomeObservation(EnvironmentMixin, Base):
    """One canonical observation of an action reaching an outcome state.

    The action row remains the mutable current state. This table preserves the
    first observation of each outcome state so delayed verification and later
    transitions cannot rewrite historical evidence.
    """

    __tablename__ = "recovery_outcome_observations"
    __table_args__ = (
        sa.UniqueConstraint(
            "action_id", "observed_status", name="uq_recovery_outcome_action_status"
        ),
        sa.Index("ix_recovery_outcome_environment_observed_at", "environment", "observed_at"),
        sa.Index("ix_recovery_outcome_action_id", "action_id"),
    )

    id: Mapped[str] = mapped_column(
        sa.String(64), primary_key=True, default=lambda: ids.new_id("out_")
    )
    action_id: Mapped[str] = mapped_column(
        sa.ForeignKey("recovery_actions.id", ondelete="CASCADE"), nullable=False
    )
    opportunity_id: Mapped[str] = mapped_column(
        sa.ForeignKey("recovery_opportunities.id", ondelete="CASCADE"), nullable=False
    )
    action_type: Mapped[ActionType] = mapped_column(
        sa.Enum(ActionType, native_enum=False, length=64), nullable=False
    )
    observed_status: Mapped[RecoveryStatus] = mapped_column(
        sa.Enum(RecoveryStatus, native_enum=False, length=32), nullable=False
    )
    decision_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        TZDateTime(), nullable=False, default=utcnow, index=True
    )
    policy_decision_id: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    policy_version: Mapped[str | None] = mapped_column(sa.String(64))
    gateway_request_id: Mapped[str | None] = mapped_column(sa.String(128), index=True)
    source: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow, nullable=False)
