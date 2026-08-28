"""Fixtures for the payment-action invariants package.

One package, one job: prove the 12 payment-action invariants from
docs/payment-invariants.md that are not already proven elsewhere. Existing
proofs live in tests/recovery, tests/razorpay, tests/security, tests/policy
and tests/agent — this package REFERENCES them from the doc and only adds the
missing ones (concurrent duplicate execute, policy-precedes-gateway sweep,
refund zero-transport, route-level no-secret fail-closed, out-of-range
confidence end-to-end, audit-trail transition sweep).

Mirrors tests/security/conftest.py: `client` shadows the root fixture to add
a gateway override with a known webhook secret; the ORM factories copy the
same small builders used by the recovery/security packages.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.models as models
from app.api.deps import get_gateway_dependency
from app.db import get_db, utcnow
from app.main import create_app
from app.ports import ActionType, RecoveryStatus
from app.services.razorpay.simulated import SimulatedPaymentGateway

WH_SECRET = "whsec_invariants_tests"
API_KEY_HEADER = {"X-API-Key": "dev-key"}


@pytest.fixture()
def sim_gateway() -> SimulatedPaymentGateway:
    """Fresh deterministic simulator per test (always pays inline)."""
    return SimulatedPaymentGateway(success_rate=1.0, webhook_secret=WH_SECRET)


@pytest.fixture()
def gateway(sim_gateway: SimulatedPaymentGateway) -> SimulatedPaymentGateway:
    return sim_gateway


@pytest.fixture()
def client(db_session: Session, gateway: SimulatedPaymentGateway) -> Generator[TestClient, None, None]:
    app = create_app()

    def _override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_gateway_dependency] = lambda: gateway
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def sign():
    def _sign(body: bytes, secret: str = WH_SECRET) -> str:
        return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    return _sign


# --- ORM builders ------------------------------------------------------------


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
def make_opportunity(db_session: Session, make_incident):
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
def make_proposed_action(db_session: Session):
    """A PROPOSED recovery_actions row exactly as the agent tools create it."""

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
    """Gateway-side transient failure — the retry_payment strategy class."""

    def _make(**kw) -> models.Payment:
        kw.setdefault("status", "failed")
        kw.setdefault("error_code", "GATEWAY_ERROR")
        kw.setdefault("error_description", "payment_timed_out")
        kw.setdefault("error_source", "gateway")
        return make_payment(**kw)

    return _make


@pytest.fixture()
def abandoned_payment(make_payment):
    """Customer-side failure (wrong OTP) — the payment-link auto-execute class."""

    def _make(**kw) -> models.Payment:
        kw.setdefault("status", "failed")
        kw.setdefault("error_code", "BAD_REQUEST_ERROR")
        kw.setdefault("error_description", "incorrect_otp")
        kw.setdefault("error_source", "customer")
        return make_payment(**kw)

    return _make
