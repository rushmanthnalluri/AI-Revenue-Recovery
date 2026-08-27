"""Fixtures for policy tests: real config, engine over in-memory SQLite,
ActionContext factory, and recovery-action row builders for the stateful
guards (rate limits, duplicates, stopping rules).
"""

from collections.abc import Generator

import pytest
from sqlalchemy.orm import Session

import app.models as models
from app.db import utcnow
from app.ports import ActionContext, ActionType, RecoveryStatus
from app.services.policy import PolicyEngine, load_policy_config


@pytest.fixture(scope="session")
def policy_config():
    """The real policies/default.yaml — these tests guard the shipped config."""
    return load_policy_config()


@pytest.fixture()
def engine(policy_config, db_session: Session) -> PolicyEngine:
    return PolicyEngine(policy_config, session=db_session)


@pytest.fixture()
def make_ctx():
    """ActionContext factory; defaults describe a compliant low-risk action."""

    def _make(**over) -> ActionContext:
        base = dict(
            action_type=ActionType.RETRY_PAYMENT,
            amount_paise=10_000,  # INR 100.00
            confidence=0.95,
            actor="agent:strategist",
        )
        base.update(over)
        return ActionContext(**base)

    return _make


@pytest.fixture()
def make_customer(db_session: Session, make_merchant):
    def _make(merchant=None, **kw) -> models.Customer:
        merchant = merchant or make_merchant()
        customer = models.Customer(merchant_id=merchant.id, **kw)
        db_session.add(customer)
        db_session.commit()
        return customer

    return _make


@pytest.fixture()
def make_recovery_action(db_session: Session):
    """Build a recovery action row (plus its opportunity and, unless given, a
    fresh strategy) with controllable customer / incident / strategy / status /
    created_at — the raw material for the stateful policy guards."""

    def _make(
        *,
        customer: models.Customer | None = None,
        incident: models.Incident | None = None,
        strategy: models.RecoveryStrategy | None = None,
        action_type: ActionType = ActionType.RETRY_PAYMENT,
        status: RecoveryStatus = RecoveryStatus.EXECUTING,
        amount_paise: int = 10_000,
        created_at=None,
    ) -> models.RecoveryAction:
        opportunity = models.RecoveryOpportunity(
            customer_id=customer.id if customer else None,
            incident_id=incident.id if incident else None,
            opportunity_type="failed_payment_retry",
            amount_paise=amount_paise,
        )
        db_session.add(opportunity)
        db_session.flush()
        if strategy is None:
            strategy = models.RecoveryStrategy(
                opportunity_id=opportunity.id, action_type=action_type
            )
            db_session.add(strategy)
            db_session.flush()
        action = models.RecoveryAction(
            opportunity_id=opportunity.id,
            strategy_id=strategy.id,
            incident_id=incident.id if incident else None,
            action_type=action_type,
            status=status,
            amount_paise=amount_paise,
            proposed_at=created_at or utcnow(),
        )
        if created_at is not None:
            action.created_at = created_at
        db_session.add(action)
        db_session.commit()
        return action

    return _make
