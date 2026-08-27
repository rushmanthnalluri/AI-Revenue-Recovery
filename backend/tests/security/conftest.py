"""Fixtures for the security/adversarial test package.

Mirrors the razorpay webhook conftest (gateway override with a known webhook
secret) and adds gateway doubles used across the attack vectors:

- CountingGateway: records every mutating call; tests assert zero mutations.
- ConfusedGateway: fetch_* return entities for DIFFERENT ids than requested.
- HangingGateway: fetch_* raise GatewayTransientError (timeout-shaped).

Root conftest fixtures (db_session, make_payment, make_incident, ...) remain
available; this package's `client` shadows the root one to add the gateway
override (same pattern as tests/razorpay/conftest.py).
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.models as models
from app.api.deps import get_gateway_dependency
from app.db import get_db, utcnow
from app.main import create_app
from app.ports import ActionType, RecoveryStatus
from app.services.razorpay.errors import (
    GatewayBadRequestError,
    GatewayTransientError,
)
from app.services.razorpay.simulated import SimulatedPaymentGateway

WH_SECRET = "whsec_security_tests"
API_KEY_HEADER = {"X-API-Key": "dev-key"}


@pytest.fixture()
def gateway() -> SimulatedPaymentGateway:
    return SimulatedPaymentGateway(webhook_secret=WH_SECRET)


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


# --- gateway doubles ---------------------------------------------------------


class CountingGateway(SimulatedPaymentGateway):
    """Simulator that counts every mutating call (mutation-side tripwire)."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.mutation_calls = 0
        self.fetch_calls = 0

    def create_order(self, **kw: Any) -> dict[str, Any]:
        self.mutation_calls += 1
        return super().create_order(**kw)

    def create_payment_link(self, **kw: Any) -> dict[str, Any]:
        self.mutation_calls += 1
        return super().create_payment_link(**kw)

    def create_subscription(self, **kw: Any) -> dict[str, Any]:
        self.mutation_calls += 1
        return super().create_subscription(**kw)

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        self.fetch_calls += 1
        return super().fetch_payment(payment_id)

    def fetch_order(self, order_id: str) -> dict[str, Any]:
        self.fetch_calls += 1
        return super().fetch_order(order_id)


class ConfusedGateway:
    """PaymentGateway-shaped double that answers fetch_* with an entity whose
    id is DIFFERENT from the requested id (identity confusion), always
    'captured'/'paid' — the inconsistent-gateway worst case."""

    def __init__(self) -> None:
        self.mutation_calls = 0
        self.requested_payment_ids: list[str] = []
        self.requested_order_ids: list[str] = []

    def create_order(self, **kw: Any) -> dict[str, Any]:
        self.mutation_calls += 1
        raise GatewayBadRequestError("confused gateway rejects", status_code=400)

    def create_payment_link(self, **kw: Any) -> dict[str, Any]:
        self.mutation_calls += 1
        raise GatewayBadRequestError("confused gateway rejects", status_code=400)

    def create_subscription(self, **kw: Any) -> dict[str, Any]:
        self.mutation_calls += 1
        raise GatewayBadRequestError("confused gateway rejects", status_code=400)

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        self.requested_payment_ids.append(payment_id)
        return {
            "id": f"{payment_id}_DIFFERENT",
            "entity": "payment",
            "status": "captured",
            "captured": True,
            "amount": 10_000_000,
            "amount_paid": 10_000_000,
        }

    def fetch_order(self, order_id: str) -> dict[str, Any]:
        self.requested_order_ids.append(order_id)
        return {
            "id": f"{order_id}_DIFFERENT",
            "entity": "order",
            "status": "paid",
            "amount": 10_000_000,
            "amount_paid": 10_000_000,
        }

    def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        return False


class HangingGateway(ConfusedGateway):
    """Every fetch times out (transient) — the 'gateway hangs' reconcile case."""

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        self.requested_payment_ids.append(payment_id)
        raise GatewayTransientError(f"read timeout fetching {payment_id}")

    def fetch_order(self, order_id: str) -> dict[str, Any]:
        self.requested_order_ids.append(order_id)
        raise GatewayTransientError(f"read timeout fetching {order_id}")


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
def make_unknown_action(db_session: Session):
    """An UNKNOWN recovery action (mutation sent once, outcome ambiguous) —
    the input resolve()/reconcile operate on."""

    def _make(
        opportunity,
        *,
        action_type: ActionType = ActionType.RETRY_PAYMENT,
        gateway_response: dict | None = None,
        **kw,
    ) -> models.RecoveryAction:
        action = models.RecoveryAction(
            opportunity_id=opportunity.id,
            incident_id=opportunity.incident_id,
            action_type=action_type,
            status=RecoveryStatus.UNKNOWN,
            amount_paise=opportunity.amount_paise,
            confidence=0.95,
            actor="agent:strategist",
            gateway_request_id=kw.pop("gateway_request_id", f"gwr_{opportunity.id}"),
            gateway_response=gateway_response,
            proposed_at=utcnow(),
            executed_at=utcnow(),
            last_error="GatewayTransientError: simulated timeout",
            **kw,
        )
        db_session.add(action)
        db_session.commit()
        return action

    return _make
