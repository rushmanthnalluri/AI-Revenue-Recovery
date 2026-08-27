"""Reconciliation sweep tests (ADR 0011): UNKNOWN actions resolved by GET-only
gateway re-query, failed webhook events reprocessed through the live handler
registry, second-sweep idempotency, and the sweep report/audit contract.
"""

import sqlalchemy as sa
import pytest
from fastapi.testclient import TestClient

import app.models as models
from app.api.deps import get_gateway_dependency
from app.db import get_db, utcnow
from app.main import create_app
from app.ports import ActionType, RecoveryStatus
from app.services.recovery import run_reconciliation

API_KEY = {"X-API-Key": "dev-key"}


def _payment_entity(gateway_payment_id: str, status: str = "captured", **kw) -> dict:
    entity = {
        "id": gateway_payment_id,
        "entity": "payment",
        "amount": 50000,
        "currency": "INR",
        "status": status,
        "method": "upi",
        "captured": status == "captured",
    }
    entity.update(kw)
    return entity


def _event_payload(event_type: str, entity: dict, kind: str = "payment") -> dict:
    return {
        "entity": "event",
        "account_id": "acc_test",
        "event": event_type,
        "contains": [kind],
        "payload": {kind: {"entity": entity}},
        "created_at": 1700000000,
    }


def _failed_webhook_event(
    db_session, *, gateway_event_id: str, event_type: str, entity: dict, kind="payment"
) -> models.WebhookEvent:
    """A stored event whose handler failed at intake (processed=false)."""
    row = models.WebhookEvent(
        gateway_event_id=gateway_event_id,
        event_type=event_type,
        payload=_event_payload(event_type, entity, kind),
        signature_valid=True,
        processed=False,
        received_at=utcnow(),
        source="simulator",
        error="handler error: boom",
    )
    db_session.add(row)
    db_session.commit()
    return row


def _unknown_action(db_session, make_opportunity, *, payment=None):
    opp = make_opportunity(payment=payment)
    action = models.RecoveryAction(
        opportunity_id=opp.id,
        incident_id=opp.incident_id,
        action_type=ActionType.RETRY_PAYMENT,
        status=RecoveryStatus.UNKNOWN,
        amount_paise=opp.amount_paise,
        confidence=0.95,
        actor="agent:strategist",
        gateway_request_id="gwr_reconcile_test",
        gateway_response=None,
        proposed_at=utcnow(),
        executed_at=utcnow(),
        last_error="GatewayTransientError: simulated timeout",
    )
    db_session.add(action)
    db_session.commit()
    return opp, action


# --- UNKNOWN resolution -------------------------------------------------------


def test_unknown_action_stays_unknown_without_gateway_truth(
    db_session, sim_gateway, make_opportunity, failed_payment
):
    payment = failed_payment(gateway_payment_id="pay_ghost1")
    _, action = _unknown_action(db_session, make_opportunity, payment=payment)

    report = run_reconciliation(db_session, sim_gateway, actor="human:ops")

    assert report.unknown_scanned == 1
    assert report.resolved == 0
    assert report.still_unknown == 1
    db_session.refresh(action)
    assert action.status is RecoveryStatus.UNKNOWN  # surfaced, never guessed
    check = db_session.scalar(
        sa.select(models.AuditLog).where(
            models.AuditLog.action == "recovery.action.resolve_check",
            models.AuditLog.entity_id == action.id,
        )
    )
    assert check is not None


def test_unknown_action_resolves_once_gateway_truth_appears(
    db_session, sim_gateway, make_opportunity, failed_payment
):
    payment = failed_payment(gateway_payment_id="pay_truth1")
    opp, action = _unknown_action(db_session, make_opportunity, payment=payment)

    first = run_reconciliation(db_session, sim_gateway, actor="human:ops")
    assert (first.resolved, first.still_unknown) == (0, 1)

    # The ambiguous gateway call lands: the payment is now captured upstream.
    sim_gateway.payments["pay_truth1"] = _payment_entity("pay_truth1")

    second = run_reconciliation(db_session, sim_gateway, actor="human:ops")
    assert second.unknown_scanned == 1
    assert second.resolved == 1
    assert second.still_unknown == 0
    db_session.refresh(action)
    db_session.refresh(opp)
    assert action.status is RecoveryStatus.RECOVERED
    assert action.verified_at is not None
    assert opp.status is RecoveryStatus.RECOVERED  # opportunity kept in lockstep
    recovered = db_session.scalar(
        sa.select(models.AuditLog).where(
            models.AuditLog.action == "recovery.action.recovered",
            models.AuditLog.entity_id == action.id,
        )
    )
    assert recovered is not None
    assert recovered.details["verification"] == "fetch_payment"


# --- failed webhook reprocessing ----------------------------------------------


def test_failed_webhook_reprocessed_recovers_action(
    db_session, sim_gateway, make_payment, make_opportunity
):
    # The event arrived before the payment row existed: handler returned an
    # unresolved note and the event was stored processed=false.
    event = _failed_webhook_event(
        db_session,
        gateway_event_id="evt_late1",
        event_type="payment.captured",
        entity=_payment_entity("pay_late1"),
    )
    payment = make_payment(gateway_payment_id="pay_late1", status="created")
    opp = make_opportunity(payment=payment)
    action = models.RecoveryAction(
        opportunity_id=opp.id,
        incident_id=opp.incident_id,
        action_type=ActionType.RETRY_PAYMENT,
        status=RecoveryStatus.EXECUTING,
        amount_paise=opp.amount_paise,
        confidence=0.95,
        actor="agent:strategist",
        proposed_at=utcnow(),
        executed_at=utcnow(),
    )
    db_session.add(action)
    db_session.commit()

    report = run_reconciliation(db_session, sim_gateway, actor="human:ops")

    assert report.webhooks_reprocessed == 1
    assert report.webhooks_still_failing == 0
    db_session.refresh(event)
    assert event.processed is True
    assert event.error is None
    db_session.refresh(payment)
    assert payment.status == "captured"
    db_session.refresh(action)
    assert action.status == RecoveryStatus.RECOVERED
    assert action.verified_at is not None


def test_still_unresolvable_webhook_remains_failing(
    db_session, sim_gateway
):
    event = _failed_webhook_event(
        db_session,
        gateway_event_id="evt_ghost2",
        event_type="payment.captured",
        entity=_payment_entity("pay_never_exists"),
    )

    report = run_reconciliation(db_session, sim_gateway, actor="human:ops")

    assert report.webhooks_reprocessed == 0
    assert report.webhooks_still_failing == 1
    db_session.refresh(event)
    assert event.processed is False
    assert "unknown payment" in event.error


# --- idempotency ----------------------------------------------------------------


def test_second_sweep_is_a_noop_beyond_its_own_audit_row(
    db_session, sim_gateway, make_opportunity, failed_payment
):
    payment = failed_payment(gateway_payment_id="pay_idem1")
    _, action = _unknown_action(db_session, make_opportunity, payment=payment)
    sim_gateway.payments["pay_idem1"] = _payment_entity("pay_idem1")
    event = _failed_webhook_event(
        db_session,
        gateway_event_id="evt_idem1",
        event_type="payment.captured",
        entity=_payment_entity("pay_idem1"),
    )

    first = run_reconciliation(db_session, sim_gateway, actor="human:ops")
    assert first.unknown_scanned == 1
    assert first.resolved == 1
    assert first.webhooks_reprocessed == 1
    db_session.refresh(action)
    assert action.status is RecoveryStatus.RECOVERED

    recovered_rows = list(
        db_session.scalars(
            sa.select(models.AuditLog).where(
                models.AuditLog.action == "recovery.action.recovered",
                models.AuditLog.entity_id == action.id,
            )
        )
    )
    assert len(recovered_rows) == 1

    second = run_reconciliation(db_session, sim_gateway, actor="human:ops")
    assert second.unknown_scanned == 0
    assert second.resolved == 0
    assert second.still_unknown == 0
    assert second.webhooks_reprocessed == 0
    assert second.webhooks_still_failing == 0

    # No duplicate transitions: still exactly one recovery.action.recovered row.
    recovered_rows = list(
        db_session.scalars(
            sa.select(models.AuditLog).where(
                models.AuditLog.action == "recovery.action.recovered",
                models.AuditLog.entity_id == action.id,
            )
        )
    )
    assert len(recovered_rows) == 1
    # The only new audit rows are the sweeps' own recovery.reconcile rows.
    reconcile_rows = list(
        db_session.scalars(
            sa.select(models.AuditLog).where(
                models.AuditLog.action == "recovery.reconcile"
            )
        )
    )
    assert len(reconcile_rows) == 2  # one per sweep, nothing else accumulated


# --- API surface ---------------------------------------------------------------


@pytest.fixture()
def api_client(db_session, sim_gateway):
    app = create_app()

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_gateway_dependency] = lambda: sim_gateway
    with TestClient(app) as c:
        yield c


def test_reconcile_endpoint_report_and_audit_row(
    api_client, db_session, sim_gateway, make_opportunity, failed_payment
):
    payment = failed_payment(gateway_payment_id="pay_api1")
    _, action = _unknown_action(db_session, make_opportunity, payment=payment)
    sim_gateway.payments["pay_api1"] = _payment_entity("pay_api1")
    _failed_webhook_event(
        db_session,
        gateway_event_id="evt_api1",
        event_type="payment.captured",
        entity=_payment_entity("pay_still_ghost"),
    )

    resp = api_client.post(
        "/api/v1/recovery/reconcile", json={"actor": "human:oncall"}, headers=API_KEY
    )
    assert resp.status_code == 200
    report = resp.json()
    assert report["sweep_id"].startswith("rcn_")
    assert report["unknown_scanned"] == 1
    assert report["resolved"] == 1
    assert report["still_unknown"] == 0
    assert report["webhooks_reprocessed"] == 0  # ghost payment still unresolvable
    assert report["webhooks_still_failing"] == 1

    db_session.refresh(action)
    assert action.status is RecoveryStatus.RECOVERED
    row = db_session.scalar(
        sa.select(models.AuditLog).where(
            models.AuditLog.action == "recovery.reconcile",
            models.AuditLog.entity_id == report["sweep_id"],
        )
    )
    assert row is not None
    assert row.actor == "human:oncall"
    assert row.details["resolved"] == 1
    assert row.details["webhooks_still_failing"] == 1


def test_reconcile_endpoint_requires_api_key(api_client):
    resp = api_client.post("/api/v1/recovery/reconcile", json={"actor": "human:oncall"})
    assert resp.status_code == 401
