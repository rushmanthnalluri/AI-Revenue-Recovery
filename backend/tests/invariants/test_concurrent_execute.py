"""Invariant 1: one logical action -> at most one gateway mutation, even when
two execute requests race concurrently.

The sequential half is already proven (see docs/payment-invariants.md):
- tests/recovery/test_executor.py::TestGuards::test_execute_refuses_in_flight_action
- tests/recovery/test_failure_modes.py::TestDuplicateExecute
- tests/recovery/test_failure_modes.py::TestTimeoutUnknownResolution
- tests/security/test_gateway_inconsistency.py::TestTimeoutsBounded::test_mutating_call_never_retried_on_timeout

This module proves the CONCURRENT half: two barrier-synchronized execute
requests for the same opportunity + strategy, on a file-backed DB with
independent sessions per request thread, with the gateway artificially
slowed so both requests are genuinely in flight together. Exactly one
gateway mutation may ever happen; the loser must be cleanly refused/blocked,
never error and never double-fire.

Regression anchor (the bug this guards): before the executor took a
SELECT ... FOR UPDATE lock on the opportunity row (RecoveryExecutor._lock_opportunity),
the same race against Postgres 16 double-fired: 3/3 runs produced TWO payment
links, both RECOVERED (verified live 2026-08-28 against postgres:16-alpine,
throwaway container). With the lock: 3/3 runs produced exactly ONE link; the
loser was 200 REJECTED via duplicate.cooldown. On SQLite, writer serialization
already ordered the race; the lock makes it explicit and portable.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import app.models as models
from app.api.deps import get_gateway_dependency
from app.db import Base, get_db, utcnow
from app.main import create_app
from app.ports import ActionType, RecoveryStatus
from app.services.razorpay.simulated import SimulatedPaymentGateway
from app.services.recovery.strategies import StrategyGenerator

GATEWAY_DELAY_SECONDS = 0.4  # holds the winner's transaction open mid-fire


class _SlowGateway(SimulatedPaymentGateway):
    """Simulator that pauses inside the mutation, widening the race window so
    both racing requests overlap with near-certainty."""

    def create_payment_link(self, **kw):
        time.sleep(GATEWAY_DELAY_SECONDS)
        return super().create_payment_link(**kw)


def _seed_race_target(session) -> tuple[str, str]:
    """One opportunity with a generated payment-link strategy. Returns
    (opportunity_id, strategy_id)."""
    merchant = models.Merchant(name="race merchant")
    session.add(merchant)
    session.flush()
    customer = models.Customer(merchant_id=merchant.id, name="Racer", email="r@x.com")
    session.add(customer)
    session.flush()
    payment = models.Payment(
        merchant_id=merchant.id,
        customer_id=customer.id,
        amount_paise=100_000,
        status="failed",
        error_code="BAD_REQUEST_ERROR",
        error_description="incorrect_otp",
        error_source="customer",
    )
    session.add(payment)
    session.flush()
    incident = models.Incident(
        title="race incident", metric="payment_success_rate", detected_at=utcnow()
    )
    session.add(incident)
    session.flush()
    session.add(
        models.Diagnosis(
            incident_id=incident.id,
            model_name="root-cause-v1",
            predicted_cause="gateway_outage",
            confidence=0.95,
        )
    )
    opp = models.RecoveryOpportunity(
        incident_id=incident.id,
        payment_id=payment.id,
        customer_id=customer.id,
        opportunity_type="failed_payment_retry",
        status=RecoveryStatus.PROPOSED,
        amount_paise=100_000,
    )
    session.add(opp)
    session.flush()
    strategies = StrategyGenerator(session).generate(opp)
    link = next(s for s in strategies if s.action_type is ActionType.CREATE_PAYMENT_LINK)
    session.commit()
    return opp.id, link.id


@pytest.mark.parametrize("round_no", [1, 2, 3])  # repeat: races are stochastic
def test_concurrent_duplicate_execute_exactly_one_gateway_mutation(tmp_path, round_no):
    # File-backed DB: two request threads get genuinely independent
    # connections (a StaticPool in-memory DB would serialize on one shared
    # connection and prove nothing about the race).
    db_file = tmp_path / f"race_{round_no}.db"
    engine = sa.create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )

    @sa.event.listens_for(engine, "connect")
    def _busy_timeout(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        # Writers wait instead of erroring with 'database is locked' — mirrors
        # production semantics where the guard, not a lock error, decides.
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()

    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    seed = TestingSession()
    opp_id, strategy_id = _seed_race_target(seed)
    seed.close()

    gateway = _SlowGateway(success_rate=1.0)  # pays inline -> winner RECOVERS
    app = create_app()

    def _override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_gateway_dependency] = lambda: gateway

    results: list[tuple[int, dict]] = []
    barrier = threading.Barrier(2)

    def _execute(tag: int) -> None:
        # One TestClient per thread: httpx clients are not thread-shared.
        with TestClient(app) as c:
            barrier.wait(timeout=15)
            r = c.post(
                f"/api/v1/recovery/{opp_id}/execute",
                json={"strategy_id": strategy_id, "actor": f"human:racer{tag}"},
                headers={"X-API-Key": "dev-key"},
            )
            results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=_execute, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    for t in threads:
        assert not t.is_alive(), "execute request thread hung"

    assert len(results) == 2
    codes = sorted(code for code, _ in results)
    assert codes == [200, 200], f"both racing executes must answer cleanly: {results}"

    check = TestingSession()
    try:
        actions = check.scalars(
            sa.select(models.RecoveryAction).where(
                models.RecoveryAction.opportunity_id == opp_id
            )
        ).all()
        assert len(actions) == 2, f"expected winner + refused loser, saw {actions}"
        statuses = sorted(a.status.value for a in actions)
        assert statuses == ["RECOVERED", "REJECTED"], (
            f"exactly one winner, one refused loser: {statuses}"
        )

        # The core invariant: ONE gateway mutation, ever.
        assert len(gateway.payment_links) == 1, (
            f"duplicate gateway mutation under race: {list(gateway.payment_links)}"
        )
        winner = next(a for a in actions if a.status is RecoveryStatus.RECOVERED)
        loser = next(a for a in actions if a.status is RecoveryStatus.REJECTED)
        link = next(iter(gateway.payment_links.values()))
        assert link["reference_id"] == winner.gateway_request_id
        assert winner.attempts == 1 and loser.attempts == 0
        assert loser.gateway_response is None  # never reached the gateway

        # The loser was refused by the deterministic duplicate guard, and the
        # refusal is persisted + auditable.
        decision = check.get(models.PolicyDecisionRecord, loser.policy_decision_id)
        assert decision is not None
        assert decision.outcome.value == "BLOCKED"
        assert "duplicate.cooldown" in decision.rules_matched
        loser_trail = [
            r.action
            for r in check.scalars(
                sa.select(models.AuditLog)
                .where(
                    models.AuditLog.entity_type == "recovery_action",
                    models.AuditLog.entity_id == loser.id,
                )
                .order_by(models.AuditLog.created_at, models.AuditLog.id)
            )
        ]
        assert loser_trail == [
            "recovery.action.proposed",
            "recovery.action.policy_evaluated",
            "recovery.action.rejected",
        ]
    finally:
        check.close()
        engine.dispose()
