"""Recovery domain: opportunities, strategy candidates, gated actions, and the
policy decisions that authorize (or block) them.

NOTE on references: recovery_actions.policy_decision_id is a real FK, while
policy_decisions.action_id is a deliberate SOFT reference (indexed string, no
FK constraint) to avoid a circular dependency in DDL.
"""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app import ids
from app.db import Base, TZDateTime
from app.models.base import EnvironmentMixin, TimestampMixin, enum_col
from app.ports import ActionType, PolicyOutcome, RecoveryStatus


class RecoveryOpportunity(EnvironmentMixin, TimestampMixin, Base):
    __tablename__ = "recovery_opportunities"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.opportunity_id)
    incident_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="SET NULL"), index=True
    )
    payment_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("payments.id", ondelete="SET NULL"), index=True
    )
    customer_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("customers.id", ondelete="SET NULL"), index=True
    )
    subscription_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("subscriptions.id", ondelete="SET NULL"), index=True
    )
    # failed_payment_retry | dropped_checkout | subscription_halted |
    # authorization_stuck | refund_leakage
    opportunity_type: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    status: Mapped[RecoveryStatus] = enum_col(
        RecoveryStatus, "opportunity_status", default=RecoveryStatus.PROPOSED,
        nullable=False, index=True,
    )
    amount_paise: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(8), default="INR", nullable=False)
    expected_recovery_paise: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    confidence: Mapped[float] = mapped_column(sa.Float, default=0.0, nullable=False)
    risk: Mapped[str] = mapped_column(sa.String(16), default="low", nullable=False)
    reason: Mapped[str | None] = mapped_column(sa.Text)
    constraints: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    meta: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)

    strategies: Mapped[list["RecoveryStrategy"]] = relationship(
        back_populates="opportunity", cascade="all, delete-orphan"
    )
    actions: Mapped[list["RecoveryAction"]] = relationship(back_populates="opportunity")


class RecoveryStrategy(TimestampMixin, Base):
    """A candidate strategy for an opportunity (mirrors ports.StrategyCandidate)."""

    __tablename__ = "recovery_strategies"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.strategy_id)
    opportunity_id: Mapped[str] = mapped_column(
        sa.ForeignKey("recovery_opportunities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action_type: Mapped[ActionType] = enum_col(
        ActionType, "strategy_action_type", nullable=False
    )
    rank: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    expected_recovery_paise: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    confidence: Mapped[float] = mapped_column(sa.Float, default=0.0, nullable=False)
    risk: Mapped[str] = mapped_column(sa.String(16), default="low", nullable=False)
    eligibility: Mapped[bool] = mapped_column(sa.Boolean, default=True, nullable=False)
    reason: Mapped[str | None] = mapped_column(sa.Text)
    constraints: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)
    generated_by: Mapped[str] = mapped_column(sa.String(64), default="heuristic", nullable=False)
    selected: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)

    opportunity: Mapped[RecoveryOpportunity] = relationship(back_populates="strategies")


class RecoveryAction(EnvironmentMixin, TimestampMixin, Base):
    """One attempted recovery. Status follows the RecoveryStatus state machine:
    PROPOSED -> POLICY_EVALUATED -> PENDING_APPROVAL -> APPROVED -> EXECUTING ->
    VERIFYING -> RECOVERED | FAILED | UNKNOWN; side exits REJECTED / CANCELLED /
    ESCALATED. Every transition is audit-logged."""

    __tablename__ = "recovery_actions"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.action_id)
    opportunity_id: Mapped[str] = mapped_column(
        sa.ForeignKey("recovery_opportunities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    strategy_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("recovery_strategies.id", ondelete="SET NULL"), index=True
    )
    # Denormalized for fast per-incident rollups.
    incident_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("incidents.id", ondelete="SET NULL"), index=True
    )
    action_type: Mapped[ActionType] = enum_col(
        ActionType, "action_type", nullable=False
    )
    status: Mapped[RecoveryStatus] = enum_col(
        RecoveryStatus, "action_status", default=RecoveryStatus.PROPOSED,
        nullable=False, index=True,
    )
    amount_paise: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(8), default="INR", nullable=False)
    confidence: Mapped[float] = mapped_column(sa.Float, default=0.0, nullable=False)
    policy_decision_id: Mapped[str | None] = mapped_column(
        sa.ForeignKey("policy_decisions.id", ondelete="SET NULL"), index=True
    )
    # Idempotency key sent to the gateway; unique so retries can't double-execute.
    gateway_request_id: Mapped[str | None] = mapped_column(sa.String(128), unique=True, index=True)
    gateway_response: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON)
    actor: Mapped[str] = mapped_column(sa.String(128), default="agent:strategist", nullable=False)
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    proposed_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    approved_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    approved_by: Mapped[str | None] = mapped_column(sa.String(128))
    executed_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    verified_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    note: Mapped[str | None] = mapped_column(sa.Text)

    opportunity: Mapped[RecoveryOpportunity] = relationship(back_populates="actions")


class PolicyDecisionRecord(TimestampMixin, Base):
    """Immutable record of every deterministic policy evaluation."""

    __tablename__ = "policy_decisions"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.policy_decision_id)
    # Soft reference to recovery_actions.id (no FK — see module docstring).
    action_id: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    action_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    amount_paise: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(8), default="INR", nullable=False)
    confidence: Mapped[float] = mapped_column(sa.Float, default=0.0, nullable=False)
    outcome: Mapped[PolicyOutcome] = enum_col(
        PolicyOutcome, "policy_outcome", nullable=False, index=True
    )
    reasons: Mapped[list[str]] = mapped_column(sa.JSON, default=list, nullable=False)
    rules_matched: Mapped[list[str]] = mapped_column(sa.JSON, default=list, nullable=False)
    policy_version: Mapped[str] = mapped_column(sa.String(64), default="unknown", nullable=False)
    actor: Mapped[str] = mapped_column(sa.String(128), default="system", nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)
