"""Failure-mode integration tests: gateway outage -> UNKNOWN safe stop (never
a blind retry), and the policy stopping rule blocking automation after three
consecutive failed recoveries on one incident."""

from datetime import timedelta

import sqlalchemy as sa

from app.db import utcnow
from app.models import RecoveryAction, RecoveryOpportunity
from app.ports import ActionType, RecoveryStatus
from app.services.razorpay.simulated import SimulatedPaymentGateway

API_KEY = {"X-API-Key": "dev-key"}


def _seed_opportunity(db_session, *, amount_paise: int = 50000) -> RecoveryOpportunity:
    from app.models import Merchant

    merchant = Merchant(name="Safe-stop merchant")
    db_session.add(merchant)
    db_session.flush()
    opp = RecoveryOpportunity(
        incident_id=None,
        opportunity_type="failed_payment_retry",
        status=RecoveryStatus.PROPOSED,
        amount_paise=amount_paise,
        currency="INR",
        reason="integration test opportunity",
    )
    db_session.add(opp)
    db_session.commit()
    return opp


def test_gateway_outage_lands_unknown_and_never_blind_retries(make_client, db_session):
    # The twin raises GatewayTransientError on every mutating call: the
    # gateway may or may not have processed the request -> UNKNOWN, and the
    # only sanctioned resolution is re-querying gateway truth (resolve()).
    gateway = SimulatedPaymentGateway(success_rate=1.0, incident={"outage": True})
    opp = _seed_opportunity(db_session)
    with make_client(gateway=gateway) as client:
        r = client.post(
            f"/api/v1/recovery/{opp.id}/execute",
            json={"actor": "human:integration-test"},
            headers=API_KEY,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] == "PENDING_APPROVAL":
            client.post(
                f"/api/v1/recovery/{opp.id}/approve",
                json={"actor": "human:integration-test"},
                headers=API_KEY,
            )
            r = client.post(
                f"/api/v1/recovery/{opp.id}/execute",
                json={"actor": "human:integration-test"},
                headers=API_KEY,
            )
            body = r.json()
        assert body["status"] == "UNKNOWN", body
        action_id = body["action_id"]
        attempts = db_session.get(RecoveryAction, action_id).attempts

        # Re-execute: must NOT re-fire the mutation — it re-queries instead.
        r = client.post(
            f"/api/v1/recovery/{opp.id}/execute",
            json={"actor": "human:integration-test"},
            headers=API_KEY,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "UNKNOWN"
        assert body["action_id"] == action_id  # same action, no duplicate
        assert db_session.get(RecoveryAction, action_id).attempts == attempts


def test_stopping_rule_blocks_after_three_consecutive_failures(make_client, db_session):
    from app.models import Incident

    opp = _seed_opportunity(db_session)
    incident_id = "inc_stopping_rule_test"
    # Parent row required under FK enforcement (SQLite PRAGMA foreign_keys=ON).
    db_session.add(
        Incident(
            id=incident_id,
            title="Stopping-rule test incident",
            metric="payment_success_rate",
            detected_at=utcnow(),
        )
    )
    opp.incident_id = incident_id
    # Three consecutive FAILED recoveries on the incident (the documented
    # stopping rule in policies/default.yaml).
    for i in range(3):
        db_session.add(
            RecoveryAction(
                opportunity_id=opp.id,
                incident_id=incident_id,
                action_type=ActionType.RETRY_PAYMENT,
                status=RecoveryStatus.FAILED,
                amount_paise=1000,
                currency="INR",
                actor="agent:integration-test",
                attempts=1,
                proposed_at=utcnow() - timedelta(minutes=30 - i),
                completed_at=utcnow() - timedelta(minutes=30 - i),
                gateway_request_id=f"gwr_stoptest_{i}",
            )
        )
    db_session.commit()

    gateway = SimulatedPaymentGateway(success_rate=1.0)
    with make_client(gateway=gateway) as client:
        r = client.post(
            f"/api/v1/recovery/{opp.id}/execute",
            json={"actor": "human:integration-test"},
            headers=API_KEY,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "REJECTED", body  # blocked by the gate
        assert body["policy_decision"]["outcome"] == "BLOCKED"
        assert any(
            "stopping" in reason.lower() or "consecutive" in reason.lower()
            for reason in body["policy_decision"]["reasons"]
        ), body["policy_decision"]
        # Nothing reached the gateway: no orders created by the blocked call.
        assert gateway.orders == {}
