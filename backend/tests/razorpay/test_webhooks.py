"""Webhook endpoint tests: signature gate, event-id dedupe, handler state
machine, at-least-once + out-of-order safety (payment.failed then
payment.captured must end captured).
"""

import json

import sqlalchemy as sa

import app.models as models
from app.db import utcnow
from app.ports import ActionType, RecoveryStatus


def make_event_body(event_type: str, entity: dict, kind: str = "payment") -> bytes:
    return json.dumps(
        {
            "entity": "event",
            "account_id": "acc_test",
            "event": event_type,
            "contains": [kind],
            "payload": {kind: {"entity": entity}},
            "created_at": 1700000000,
        }
    ).encode()


def post_event(client, sign, body: bytes, event_id: str, secret: str | None = None):
    sig = sign(body) if secret is None else sign(body, secret)
    return client.post(
        "/webhooks/razorpay",
        content=body,
        headers={
            "content-type": "application/json",
            "x-razorpay-signature": sig,
            "x-razorpay-event-id": event_id,
        },
    )


def payment_entity(gateway_payment_id: str, status: str = "captured", **kw) -> dict:
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


def _events_for(db_session, payment_id: str) -> list[models.PaymentEvent]:
    return list(
        db_session.scalars(
            sa.select(models.PaymentEvent)
            .where(models.PaymentEvent.payment_id == payment_id)
            .order_by(models.PaymentEvent.created_at, models.PaymentEvent.id)
        )
    )


# --- signature gate ---------------------------------------------------------


def test_valid_signature_accepted_and_stored(client, sign, db_session, make_payment):
    p = make_payment(gateway_payment_id="pay_ok1", status="created")
    body = make_event_body("payment.captured", payment_entity("pay_ok1"))
    r = post_event(client, sign, body, "evt_ok1")
    assert r.status_code == 200
    ack = r.json()
    assert ack["status"] == "received"
    assert ack["duplicate"] is False
    assert ack["processed"] is True
    row = db_session.scalar(
        sa.select(models.WebhookEvent).where(models.WebhookEvent.gateway_event_id == "evt_ok1")
    )
    assert row is not None
    assert row.event_type == "payment.captured"
    assert row.signature_valid is True
    db_session.refresh(p)
    assert p.status == "captured"
    assert p.captured is True


def test_invalid_signature_rejected_400_nothing_stored(client, sign, db_session):
    body = make_event_body("payment.captured", payment_entity("pay_x"))
    r = post_event(client, sign, body, "evt_bad_sig", secret="wrong_secret")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_webhook_signature"
    n = db_session.scalar(sa.select(sa.func.count()).select_from(models.WebhookEvent))
    assert n == 0


def test_tampered_body_rejected(client, sign):
    body = make_event_body("payment.captured", payment_entity("pay_x"))
    sig = sign(body)
    tampered = make_event_body("payment.captured", payment_entity("pay_x", amount=1))
    r = client.post(
        "/webhooks/razorpay",
        content=tampered,
        headers={
            "content-type": "application/json",
            "x-razorpay-signature": sig,
            "x-razorpay-event-id": "evt_tampered",
        },
    )
    assert r.status_code == 400


def test_missing_signature_rejected(client):
    body = make_event_body("payment.captured", payment_entity("pay_x"))
    r = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"x-razorpay-event-id": "evt_nosig"},
    )
    assert r.status_code == 400


def test_missing_event_id_rejected(client, sign):
    body = make_event_body("payment.captured", payment_entity("pay_x"))
    r = client.post(
        "/webhooks/razorpay",
        content=body,
        headers={"x-razorpay-signature": sign(body)},
    )
    assert r.status_code == 400


# --- dedupe (at-least-once) ---------------------------------------------------


def test_duplicate_event_id_already_processed_zero_side_effects(
    client, sign, db_session, make_payment
):
    p = make_payment(gateway_payment_id="pay_dup1", status="created")
    body = make_event_body("payment.captured", payment_entity("pay_dup1"))
    r1 = post_event(client, sign, body, "evt_dup1")
    assert r1.status_code == 200 and r1.json()["duplicate"] is False

    r2 = post_event(client, sign, body, "evt_dup1")
    assert r2.status_code == 200
    ack = r2.json()
    assert ack["status"] == "already_processed"
    assert ack["duplicate"] is True

    rows = db_session.scalars(
        sa.select(models.WebhookEvent).where(
            models.WebhookEvent.gateway_event_id == "evt_dup1"
        )
    ).all()
    assert len(rows) == 1
    # Exactly one payment transition recorded — the retry had zero side effects.
    events = _events_for(db_session, p.id)
    assert len(events) == 1
    assert events[0].to_status == "captured"


# --- out-of-order safety ------------------------------------------------------


def test_failed_then_captured_ends_captured(client, sign, db_session, make_payment):
    p = make_payment(gateway_payment_id="pay_ooo1", status="created")
    failed = make_event_body(
        "payment.failed",
        payment_entity(
            "pay_ooo1",
            status="failed",
            error_code="BAD_REQUEST_ERROR",
            error_reason="insufficient_fund",
            error_source="customer",
        ),
    )
    captured = make_event_body("payment.captured", payment_entity("pay_ooo1"))
    assert post_event(client, sign, failed, "evt_ooo1a").status_code == 200
    db_session.refresh(p)
    assert p.status == "failed"
    assert p.error_code == "BAD_REQUEST_ERROR"
    assert p.error_source == "customer"

    assert post_event(client, sign, captured, "evt_ooo1b").status_code == 200
    db_session.refresh(p)
    assert p.status == "captured"
    assert p.captured is True
    transitions = [(e.from_status, e.to_status) for e in _events_for(db_session, p.id)]
    assert transitions == [("created", "failed"), ("failed", "captured")]


def test_captured_then_late_failed_stays_captured(client, sign, db_session, make_payment):
    p = make_payment(gateway_payment_id="pay_ooo2", status="created")
    captured = make_event_body("payment.captured", payment_entity("pay_ooo2"))
    failed = make_event_body("payment.failed", payment_entity("pay_ooo2", status="failed"))
    post_event(client, sign, captured, "evt_ooo2a")
    post_event(client, sign, failed, "evt_ooo2b")
    db_session.refresh(p)
    assert p.status == "captured"
    assert p.captured is True
    transitions = [(e.from_status, e.to_status) for e in _events_for(db_session, p.id)]
    assert transitions == [("created", "captured")]


# --- recovery action verification ----------------------------------------------


def _make_opportunity_with_action(
    db_session, make_payment, *, gateway_payment_id=None, gateway_request_id=None, action_type
):
    payment = None
    if gateway_payment_id:
        payment = make_payment(gateway_payment_id=gateway_payment_id, status="failed")
    opp = models.RecoveryOpportunity(
        opportunity_type="failed_payment_retry",
        amount_paise=50000,
        payment_id=payment.id if payment else None,
    )
    db_session.add(opp)
    db_session.flush()
    action = models.RecoveryAction(
        opportunity_id=opp.id,
        action_type=action_type,
        status=RecoveryStatus.EXECUTING,
        amount_paise=50000,
        gateway_request_id=gateway_request_id,
        proposed_at=utcnow(),
    )
    db_session.add(action)
    db_session.commit()
    return payment, opp, action


def test_payment_link_paid_marks_linked_action_recovered(
    client, sign, db_session, make_payment
):
    _, _, action = _make_opportunity_with_action(
        db_session,
        make_payment,
        gateway_request_id="act_link_paid",
        action_type=ActionType.CREATE_PAYMENT_LINK,
    )
    link_entity = {
        "id": "plink_1",
        "entity": "payment_link",
        "reference_id": "act_link_paid",
        "amount": 50000,
        "amount_paid": 50000,
        "status": "paid",
    }
    body = make_event_body("payment_link.paid", link_entity, kind="payment_link")
    r = post_event(client, sign, body, "evt_plink1")
    assert r.status_code == 200
    assert r.json()["processed"] is True
    db_session.refresh(action)
    assert action.status == RecoveryStatus.RECOVERED
    assert action.verified_at is not None
    audit = db_session.scalar(
        sa.select(models.AuditLog).where(models.AuditLog.entity_id == action.id)
    )
    assert audit is not None
    assert audit.action == "verify_recovered"


def test_payment_captured_recovers_action_linked_via_opportunity(
    client, sign, db_session, make_payment
):
    payment, _, action = _make_opportunity_with_action(
        db_session,
        make_payment,
        gateway_payment_id="pay_act1",
        gateway_request_id="act_retry1",
        action_type=ActionType.RETRY_PAYMENT,
    )
    body = make_event_body("payment.captured", payment_entity("pay_act1"))
    r = post_event(client, sign, body, "evt_act1")
    assert r.status_code == 200
    db_session.refresh(action)
    assert action.status == RecoveryStatus.RECOVERED
    assert action.verified_at is not None
    db_session.refresh(payment)
    assert payment.status == "captured"


def test_failed_then_captured_action_failed_then_recovered(
    client, sign, db_session, make_payment
):
    """Out-of-order/late success: payment.failed marks the linked action
    FAILED, a later payment.captured (failed is not terminal) recovers it."""
    _, _, action = _make_opportunity_with_action(
        db_session,
        make_payment,
        gateway_payment_id="pay_act2",
        gateway_request_id="act_retry2",
        action_type=ActionType.RETRY_PAYMENT,
    )
    failed = make_event_body(
        "payment.failed",
        payment_entity("pay_act2", status="failed", error_reason="payment_timed_out"),
    )
    post_event(client, sign, failed, "evt_act2a")
    db_session.refresh(action)
    assert action.status == RecoveryStatus.FAILED
    assert action.last_error and "payment_timed_out" in action.last_error

    captured = make_event_body("payment.captured", payment_entity("pay_act2"))
    post_event(client, sign, captured, "evt_act2b")
    db_session.refresh(action)
    assert action.status == RecoveryStatus.RECOVERED
    assert action.verified_at is not None
    assert action.last_error is None


def test_recovered_action_is_terminal_against_late_failure(
    client, sign, db_session, make_payment
):
    _, _, action = _make_opportunity_with_action(
        db_session,
        make_payment,
        gateway_payment_id="pay_act3",
        gateway_request_id="act_retry3",
        action_type=ActionType.RETRY_PAYMENT,
    )
    captured = make_event_body("payment.captured", payment_entity("pay_act3"))
    post_event(client, sign, captured, "evt_act3a")
    db_session.refresh(action)
    assert action.status == RecoveryStatus.RECOVERED

    failed = make_event_body("payment.failed", payment_entity("pay_act3", status="failed"))
    post_event(client, sign, failed, "evt_act3b")
    db_session.refresh(action)
    assert action.status == RecoveryStatus.RECOVERED  # unchanged


def test_webhook_recovery_syncs_opportunity_status_for_filtered_list(
    client, sign, db_session, make_payment
):
    """Regression: a webhook-driven action transition must sync the
    opportunity's stored status, otherwise status-filtered list queries miss
    rows the UI displays under the projected (latest-action) status."""
    _, opp, action = _make_opportunity_with_action(
        db_session,
        make_payment,
        gateway_payment_id="pay_act4",
        gateway_request_id="act_retry4",
        action_type=ActionType.RETRY_PAYMENT,
    )
    assert opp.status == RecoveryStatus.PROPOSED
    body = make_event_body("payment.captured", payment_entity("pay_act4"))
    r = post_event(client, sign, body, "evt_act4")
    assert r.status_code == 200
    db_session.refresh(action)
    db_session.refresh(opp)
    assert action.status == RecoveryStatus.RECOVERED
    assert opp.status == RecoveryStatus.RECOVERED

    r = client.get(
        "/api/v1/recovery/opportunities",
        params={"status": "RECOVERED", "environment": "research"},
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["total"] >= 1
    ids = [item["id"] for item in payload["items"]]
    assert opp.id in ids
    item = next(i for i in payload["items"] if i["id"] == opp.id)
    assert item["status"] == "RECOVERED"


# --- misc ----------------------------------------------------------------------


def test_unhandled_event_type_stored_and_acked(client, sign, db_session):
    body = make_event_body("payment.authorized", payment_entity("pay_unhandled"))
    r = post_event(client, sign, body, "evt_unhandled")
    assert r.status_code == 200
    ack = r.json()
    assert ack["status"] == "received"
    row = db_session.scalar(
        sa.select(models.WebhookEvent).where(
            models.WebhookEvent.gateway_event_id == "evt_unhandled"
        )
    )
    assert row is not None
    assert row.event_type == "payment.authorized"


def test_unknown_payment_is_stored_for_reconciliation(client, sign, db_session):
    body = make_event_body("payment.captured", payment_entity("pay_ghost"))
    r = post_event(client, sign, body, "evt_ghost")
    assert r.status_code == 200
    assert r.json()["processed"] is False
    row = db_session.scalar(
        sa.select(models.WebhookEvent).where(
            models.WebhookEvent.gateway_event_id == "evt_ghost"
        )
    )
    assert row.processed is False
    assert "unknown payment" in row.error


# --- environment stamping (real_test/research boundary) ------------------------


def test_verify_audit_row_stamped_with_action_environment(
    client, sign, db_session, make_payment
):
    """REGRESSION: webhook verification audit rows used to default to
    'research', so real_test rows vanished from real_test audit queries."""
    _, _, action = _make_opportunity_with_action(
        db_session,
        make_payment,
        gateway_payment_id="pay_env1",
        gateway_request_id="act_env1",
        action_type=ActionType.RETRY_PAYMENT,
    )
    action.environment = "real_test"
    db_session.commit()

    body = make_event_body("payment.captured", payment_entity("pay_env1"))
    assert post_event(client, sign, body, "evt_env1").status_code == 200

    audit = db_session.scalar(
        sa.select(models.AuditLog).where(
            models.AuditLog.entity_id == action.id,
            models.AuditLog.action == "verify_recovered",
        )
    )
    assert audit is not None
    assert audit.environment == "real_test"


def test_verification_hold_audit_row_stamped_with_action_environment(
    client, sign, db_session, make_payment
):
    _, _, action = _make_opportunity_with_action(
        db_session,
        make_payment,
        gateway_request_id="act_env2",
        action_type=ActionType.CREATE_PAYMENT_LINK,
    )
    action.environment = "real_test"
    db_session.commit()

    link_entity = {
        "id": "plink_env2",
        "entity": "payment_link",
        "reference_id": "act_env2",
        "amount": 49000,  # mismatch vs the action's 50000 -> hold
        "amount_paid": 49000,
        "status": "paid",
    }
    body = make_event_body("payment_link.paid", link_entity, kind="payment_link")
    assert post_event(client, sign, body, "evt_env2").status_code == 200

    db_session.refresh(action)
    assert action.status != RecoveryStatus.RECOVERED  # held, not recovered
    audit = db_session.scalar(
        sa.select(models.AuditLog).where(
            models.AuditLog.entity_id == action.id,
            models.AuditLog.action == "verification.amount_mismatch",
        )
    )
    assert audit is not None
    assert audit.environment == "real_test"


# --- ingress source stamping ----------------------------------------------------


def test_ingress_source_uses_settings_not_gateway_class_name(
    client, sign, db_session
):
    """REGRESSION: ingress used `type(gateway).__name__ == "SimulatedPaymentGateway"`,
    which any subclass silently breaks; the predicate is use_simulator(settings)."""
    from app.api.deps import get_gateway_dependency
    from app.services.razorpay.simulated import SimulatedPaymentGateway

    class RenamedSimulator(SimulatedPaymentGateway):
        pass

    sub = RenamedSimulator(webhook_secret="whsec_test_secret")
    client.app.dependency_overrides[get_gateway_dependency] = lambda: sub

    body = make_event_body("payment.captured", payment_entity("pay_src1"))
    assert post_event(client, sign, body, "evt_src1").status_code == 200
    row = db_session.scalar(
        sa.select(models.WebhookEvent).where(
            models.WebhookEvent.gateway_event_id == "evt_src1"
        )
    )
    # No real keys configured -> simulator, regardless of the class name.
    assert row.source == "simulator"


def test_ingress_source_razorpay_when_real_keys_configured(
    client, sign, db_session, monkeypatch
):
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "RAZORPAY_KEY_ID", "rzp_test_fixture")
    monkeypatch.setattr(app_settings, "RAZORPAY_KEY_SECRET", "fixture_secret")

    body = make_event_body("payment.captured", payment_entity("pay_src2"))
    assert post_event(client, sign, body, "evt_src2").status_code == 200
    row = db_session.scalar(
        sa.select(models.WebhookEvent).where(
            models.WebhookEvent.gateway_event_id == "evt_src2"
        )
    )
    assert row.source == "razorpay"


def test_handler_registry_is_exactly_the_supported_events():
    """DEF-01 regression guard (2026-09-02): docs/render.yaml told operators to
    subscribe to six event types while the registry handled three — and the one
    that verifies link recoveries (`payment_link.paid`) was not among the six,
    so live recoveries could never reach RECOVERED via webhook. The dashboard
    subscription, the docs, and this registry must stay exactly these three.
    """
    from app.services.recovery.webhook_handlers import EVENT_HANDLERS

    assert set(EVENT_HANDLERS) == {
        "payment.captured",
        "payment.failed",
        "payment_link.paid",
    }


def test_stale_capture_cannot_regress_a_refunded_payment(
    client, sign, db_session, make_payment
):
    """Phase-b adversarial F2: sync observed captured, then a refund; a stale
    payment.captured webhook (replay/late redelivery) must NOT flip the
    payment back to captured, set the captured flag, write a duplicate event,
    or mark anything RECOVERED — the money is gone."""
    p = make_payment(gateway_payment_id="pay_refunded1", status="refunded", captured=False)
    for to_status in ("captured", "refunded"):
        db_session.add(
            models.PaymentEvent(
                payment_id=p.id,
                event_type=f"payment.{to_status}",
                from_status=None,
                to_status=to_status,
                source="sync",
                payload={},
                occurred_at=utcnow(),
                source_type=p.source_type,
                source_system=p.source_system,
                external_id=p.external_id,
            )
        )
    db_session.commit()

    body = make_event_body("payment.captured", payment_entity("pay_refunded1"))
    assert post_event(client, sign, body, "evt_stale_capture").status_code == 200
    db_session.refresh(p)
    assert p.status == "refunded"  # never regressed
    assert p.captured is False
    events = list(
        db_session.scalars(
            sa.select(models.PaymentEvent).where(
                models.PaymentEvent.payment_id == p.id,
                models.PaymentEvent.to_status == "captured",
            )
        )
    )
    assert len(events) == 1  # the original sync row; no webhook duplicate
    assert events[0].source == "sync"


def test_webhook_after_sync_observation_does_not_double_write(
    client, sign, db_session, make_payment
):
    """The benign mirror: sync observed the capture first; the authoritative
    webhook arriving later advances nothing new but also writes no duplicate
    event row (dedupe fires inside the transition, not by skipping it)."""
    p = make_payment(gateway_payment_id="pay_sync_first", status="captured", captured=True)
    db_session.add(
        models.PaymentEvent(
            payment_id=p.id,
            event_type="payment.captured",
            from_status=None,
            to_status="captured",
            source="sync",
            payload={},
            occurred_at=utcnow(),
            source_type=p.source_type,
            source_system=p.source_system,
            external_id=p.external_id,
        )
    )
    db_session.commit()

    body = make_event_body("payment.captured", payment_entity("pay_sync_first"))
    assert post_event(client, sign, body, "evt_sync_first").status_code == 200
    events = list(
        db_session.scalars(
            sa.select(models.PaymentEvent).where(
                models.PaymentEvent.payment_id == p.id,
                models.PaymentEvent.to_status == "captured",
            )
        )
    )
    assert len(events) == 1
    assert events[0].source == "sync"
