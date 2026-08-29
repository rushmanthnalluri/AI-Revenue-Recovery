"""Environment isolation (c): POST /api/v1/demo/reset deletes ONLY
simulator-sourced commerce rows and research-environment derived rows.
real_test rows (Razorpay Test Mode data) are untouchable by reset."""

import sqlalchemy as sa

import app.models as models
from app.db import utcnow
from app.ports import ActionType, PolicyOutcome, RecoveryStatus


def _count(db_session, model) -> int:
    return int(db_session.scalar(sa.select(sa.func.count()).select_from(model)) or 0)


def test_reset_deletes_research_but_never_touches_real_test(
    client, db_session, make_merchant, make_real_payment, make_incident, seed_sim_payments
):
    # --- research graph (simulator-sourced) ---------------------------------
    seed_sim_payments()
    research_inc = make_incident(environment="research")
    research_opp = models.RecoveryOpportunity(
        incident_id=research_inc.id,
        opportunity_type="failed_payment_retry",
        status=RecoveryStatus.PROPOSED,
        amount_paise=10_000,
        environment="research",
    )
    db_session.add(research_opp)
    db_session.flush()
    db_session.add(
        models.RecoveryStrategy(
            opportunity_id=research_opp.id,
            action_type=ActionType.CREATE_PAYMENT_LINK,
        )
    )
    research_action = models.RecoveryAction(
        opportunity_id=research_opp.id,
        incident_id=research_inc.id,
        action_type=ActionType.CREATE_PAYMENT_LINK,
        status=RecoveryStatus.PROPOSED,
        amount_paise=10_000,
        actor="agent:test",
        proposed_at=utcnow(),
        environment="research",
    )
    db_session.add(research_action)
    db_session.flush()
    db_session.add(
        models.PolicyDecisionRecord(
            action_id=research_action.id,
            action_type="create_payment_link",
            outcome=PolicyOutcome.ALLOWED,
            decided_at=utcnow(),
        )
    )
    db_session.add(
        models.WebhookEvent(
            gateway_event_id="evt_sim_reset",
            event_type="payment.captured",
            payload={},
            received_at=utcnow(),
            source="simulator",
        )
    )

    # --- real_test graph (Razorpay Test Mode provenance) ---------------------
    real_merchant = make_merchant(
        name="Real merchant",
        source_type="razorpay_test",
        source_system="razorpay",
        gateway_account_id="acc_realtest_reset",
    )
    real_payment = make_real_payment(merchant=real_merchant)
    real_inc = make_incident(title="real incident", environment="real_test")
    real_opp = models.RecoveryOpportunity(
        incident_id=real_inc.id,
        payment_id=real_payment.id,
        opportunity_type="failed_payment_retry",
        status=RecoveryStatus.PROPOSED,
        amount_paise=50_000,
        environment="real_test",
    )
    db_session.add(real_opp)
    db_session.flush()
    real_action = models.RecoveryAction(
        opportunity_id=real_opp.id,
        incident_id=real_inc.id,
        action_type=ActionType.RETRY_PAYMENT,
        status=RecoveryStatus.PROPOSED,
        amount_paise=50_000,
        actor="agent:test",
        proposed_at=utcnow(),
        environment="real_test",
    )
    db_session.add(real_action)
    db_session.flush()
    db_session.add(
        models.WebhookEvent(
            gateway_event_id="evt_real_reset",
            event_type="payment.captured",
            payload={},
            received_at=utcnow(),
            source="razorpay",
        )
    )
    db_session.commit()

    r = client.post("/api/v1/demo/reset")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cleared"]["payments"] > 0  # the simulator payments were cleared

    # --- research rows are gone ----------------------------------------------
    assert _count(db_session, models.Payment) == 1  # only the real_test payment
    assert _count(db_session, models.Incident) == 1
    assert _count(db_session, models.RecoveryOpportunity) == 1
    assert _count(db_session, models.RecoveryAction) == 1
    assert _count(db_session, models.RecoveryStrategy) == 0
    assert _count(db_session, models.PolicyDecisionRecord) == 0
    assert _count(db_session, models.WebhookEvent) == 1

    # --- every real_test row survived, untouched ------------------------------
    assert db_session.get(models.Payment, real_payment.id) is not None
    assert db_session.get(models.Merchant, real_merchant.id) is not None
    assert db_session.get(models.Incident, real_inc.id) is not None
    assert db_session.get(models.RecoveryOpportunity, real_opp.id) is not None
    assert db_session.get(models.RecoveryAction, real_action.id) is not None
    surviving_webhook = db_session.scalars(sa.select(models.WebhookEvent)).one()
    assert surviving_webhook.gateway_event_id == "evt_real_reset"

    # --- the reset's own audit row is research-tagged --------------------------
    audit_row = db_session.get(models.AuditLog, body["audit_id"])
    assert audit_row is not None and audit_row.environment == "research"
