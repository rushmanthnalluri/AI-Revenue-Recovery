"""Fixtures for recovery-engine tests: gateway doubles (simulator + stub),
executor factory, and ORM builders for opportunities/strategies/actions.

The policy engine under test always loads the real policies/default.yaml —
these tests guard the shipped configuration's integration contract.
"""

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

import app.models as models
from app.db import utcnow
from app.ports import ActionType, RecoveryStatus
from app.services.razorpay.errors import GatewayBadRequestError, GatewayNotFoundError
from app.services.razorpay.simulated import SimulatedPaymentGateway
from app.services.recovery import RecoveryExecutor


# --- gateway doubles ---------------------------------------------------------


@pytest.fixture()
def sim_gateway() -> SimulatedPaymentGateway:
    """Fresh simulator per test: deterministic, no shared state."""
    return SimulatedPaymentGateway(success_rate=1.0)


class StubGateway:
    """PaymentGateway-shaped double: mutations raise a definitive 4xx (the
    request is rejected before processing — a truthful FAILED), GETs 404."""

    def __init__(self) -> None:
        self.mutation_calls = 0

    def create_order(self, **kw: Any) -> dict[str, Any]:
        self.mutation_calls += 1
        raise GatewayBadRequestError(
            "stub rejects every mutation", status_code=400, code="BAD_REQUEST_ERROR"
        )

    def create_payment_link(self, **kw: Any) -> dict[str, Any]:
        self.mutation_calls += 1
        raise GatewayBadRequestError(
            "stub rejects every mutation", status_code=400, code="BAD_REQUEST_ERROR"
        )

    def create_subscription(self, **kw: Any) -> dict[str, Any]:
        self.mutation_calls += 1
        raise GatewayBadRequestError(
            "stub rejects every mutation", status_code=400, code="BAD_REQUEST_ERROR"
        )

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        raise GatewayNotFoundError(f"{payment_id} does not exist", status_code=404)

    def fetch_order(self, order_id: str) -> dict[str, Any]:
        raise GatewayNotFoundError(f"{order_id} does not exist", status_code=404)

    def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        return False


@pytest.fixture()
def stub_gateway() -> StubGateway:
    """Gateway double whose mutations all fail definitively (4xx)."""
    return StubGateway()


@pytest.fixture()
def make_executor(db_session: Session):
    def _make(gateway) -> RecoveryExecutor:
        return RecoveryExecutor(db_session, gateway)

    return _make


# --- ORM builders -------------------------------------------------------------


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
def make_order(db_session: Session, make_merchant):
    def _make(merchant=None, **kw) -> models.Order:
        merchant = merchant or make_merchant()
        order = models.Order(
            merchant_id=merchant.id,
            amount_paise=kw.pop("amount_paise", 50_000),
            status=kw.pop("status", "created"),
            **kw,
        )
        db_session.add(order)
        db_session.commit()
        return order

    return _make


@pytest.fixture()
def make_subscription(db_session: Session, make_merchant):
    def _make(merchant=None, **kw) -> models.Subscription:
        merchant = merchant or make_merchant()
        subscription = models.Subscription(
            merchant_id=merchant.id,
            amount_paise=kw.pop("amount_paise", 25_000),
            status=kw.pop("status", "halted"),
            **kw,
        )
        db_session.add(subscription)
        db_session.commit()
        return subscription

    return _make


@pytest.fixture()
def make_diagnosis(db_session: Session):
    def _make(incident, **kw) -> models.Diagnosis:
        diagnosis = models.Diagnosis(
            incident_id=incident.id,
            model_name=kw.pop("model_name", "root-cause-v1"),
            predicted_cause=kw.pop("predicted_cause", "gateway_outage"),
            confidence=kw.pop("confidence", 0.95),
            **kw,
        )
        db_session.add(diagnosis)
        db_session.commit()
        return diagnosis

    return _make


@pytest.fixture()
def make_opportunity(db_session: Session, make_incident):
    """Opportunity row factory; pass incident/payment/customer to link."""

    def _make(
        *,
        incident=None,
        payment=None,
        customer=None,
        opportunity_type: str = "failed_payment_retry",
        amount_paise: int = 100_000,  # INR 1,000 — under the auto-execute ceiling
        status: RecoveryStatus = RecoveryStatus.PROPOSED,
        **kw,
    ) -> models.RecoveryOpportunity:
        if incident is None:
            incident = make_incident()
        opp = models.RecoveryOpportunity(
            incident_id=incident.id,
            payment_id=payment.id if payment else None,
            customer_id=(customer.id if customer else (payment.customer_id if payment else None)),
            opportunity_type=opportunity_type,
            status=status,
            amount_paise=amount_paise,
            **kw,
        )
        db_session.add(opp)
        db_session.commit()
        return opp

    return _make


@pytest.fixture()
def make_proposed_action(db_session: Session):
    """A PROPOSED recovery_actions row exactly as the agent package would
    create it (actor agent:strategist) — input the executor must accept."""

    def _make(
        opportunity,
        *,
        action_type: ActionType = ActionType.CREATE_PAYMENT_LINK,
        status: RecoveryStatus = RecoveryStatus.PROPOSED,
        amount_paise: int | None = None,
        confidence: float = 0.95,
        actor: str = "agent:strategist",
        strategy=None,
        **kw,
    ) -> models.RecoveryAction:
        action = models.RecoveryAction(
            opportunity_id=opportunity.id,
            strategy_id=strategy.id if strategy else None,
            incident_id=opportunity.incident_id,
            action_type=action_type,
            status=status,
            amount_paise=amount_paise if amount_paise is not None else opportunity.amount_paise,
            confidence=confidence,
            actor=actor,
            proposed_at=kw.pop("proposed_at", utcnow()),
            **kw,
        )
        db_session.add(action)
        db_session.commit()
        return action

    return _make


@pytest.fixture()
def failed_payment(make_payment):
    """A payment that failed on a transient timeout — the recoverable kind."""

    def _make(**kw) -> models.Payment:
        kw.setdefault("status", "failed")
        kw.setdefault("error_code", "GATEWAY_ERROR")
        kw.setdefault("error_description", "payment_timed_out")
        kw.setdefault("error_source", "gateway")
        return make_payment(**kw)

    return _make


@pytest.fixture()
def abandoned_payment(make_payment):
    """A payment that failed customer-side (wrong OTP) — abandonment class,
    where a payment link is the high-fit, auto-executable strategy."""

    def _make(**kw) -> models.Payment:
        kw.setdefault("status", "failed")
        kw.setdefault("error_code", "BAD_REQUEST_ERROR")
        kw.setdefault("error_description", "incorrect_otp")
        kw.setdefault("error_source", "customer")
        return make_payment(**kw)

    return _make


@pytest.fixture()
def windowed_incident(make_incident):
    """Incident with an explicit [start, end) window around now."""

    def _make(**kw) -> models.Incident:
        now = utcnow()
        kw.setdefault("window_start", now - timedelta(hours=1))
        kw.setdefault("window_end", now + timedelta(minutes=5))
        kw.setdefault("detected_at", now)
        return make_incident(**kw)

    return _make
