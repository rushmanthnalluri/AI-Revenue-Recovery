"""Fixtures for worker-tier tests: fake clock, sender doubles, worker factory,
and ORM builders for delayed strategies and outbox rows.

The policy engine under test always loads the real policies/default.yaml —
these tests guard the shipped configuration's integration contract, same as
tests/recovery.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

import app.models as models
from app.db import utcnow
from app.ports import ActionType, RecoveryStatus
from app.services.razorpay.simulated import SimulatedPaymentGateway
from app.services.worker import NotificationDeliveryError, Worker


class FakeClock:
    """Mutable, injectable clock (Worker/RecoveryExecutor take the callable)."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value

    def advance(self, **kwargs: Any) -> None:
        self._now = self._now + timedelta(**kwargs)


@pytest.fixture()
def fake_clock() -> FakeClock:
    return FakeClock(datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc))


class RecordingSender:
    """NotificationSender double: records calls, fails on demand."""

    name = "recording"

    def __init__(self, *, fail_first: int = 0, always_fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail_first = fail_first
        self._always_fail = always_fail

    def send(self, *, customer, channel, payload):
        self.calls.append({"customer": customer, "channel": channel, "payload": payload})
        if self._always_fail or len(self.calls) <= self._fail_first:
            raise NotificationDeliveryError("simulated delivery outage")
        return {"via": self.name, "channel": channel}


@pytest.fixture()
def sim_gateway() -> SimulatedPaymentGateway:
    """Fresh simulator per test: deterministic, no shared state."""
    return SimulatedPaymentGateway(success_rate=1.0)


@pytest.fixture()
def session_factory(db_session: Session):
    """Sessions on the SAME in-memory database as db_session: the worker opens
    and commits its own short-lived sessions, exactly like in production."""
    return sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False)


@pytest.fixture()
def make_worker(session_factory, sim_gateway, fake_clock):
    """Worker factory: injected fake clock; reconcile stubbed out by default
    (the cadence tests override reconcile_fn with a spy)."""

    def _make(**kw) -> Worker:
        kw.setdefault("clock", fake_clock)
        kw.setdefault("reconcile_seconds", 900.0)
        kw.setdefault("reconcile_fn", lambda db, gateway, *, actor: None)
        return Worker(session_factory, sim_gateway, **kw)

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
def make_strategy(db_session: Session):
    """A strategy row with explicit constraints — the deterministic way to
    request a delayed retry (constraints={"delay_seconds": N})."""

    def _make(
        opportunity,
        *,
        action_type: ActionType = ActionType.RETRY_PAYMENT,
        constraints: dict | None = None,
        confidence: float = 0.95,
        eligibility: bool = True,
        **kw,
    ) -> models.RecoveryStrategy:
        row = models.RecoveryStrategy(
            opportunity_id=opportunity.id,
            action_type=action_type,
            rank=0,
            expected_recovery_paise=opportunity.amount_paise,
            confidence=confidence,
            risk="medium",
            eligibility=eligibility,
            reason="worker test strategy",
            constraints=constraints or {},
            selected=False,
            **kw,
        )
        db_session.add(row)
        db_session.commit()
        return row

    return _make
