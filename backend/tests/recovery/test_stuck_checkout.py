"""Stuck-checkout recovery: payments that never resolved past `created`.

Detection v2 raises checkout_abandonment_spike incidents (metric
`checkout_abandonment_rate`) for payments stuck in `created` beyond the
30-minute inactivity threshold. These tests prove the recovery loop can ACT
on that signal end to end:

- the builder sources exactly-once opportunities from stuck-created payments
  and never double-counts a checkout against the order-level
  `dropped_checkout` path (dedup rule: payment-level wins at selection time,
  first-write wins across builds);
- the plan ranks `create_payment_link` first and marks `retry_payment`
  ineligible (a stuck payment has no failed charge to resubmit);
- execution fires a Razorpay payment link for the payment's OWN amount and
  the `payment_link.paid` webhook verifies the action to RECOVERED;
- the deterministic policy gate still decides (confidence floor, amount
  ceiling) before anything reaches the gateway.
"""

from datetime import timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

import app.models as models
from app.api.deps import get_gateway_dependency
from app.db import get_db, utcnow
from app.main import create_app
from app.ports import ActionType, PolicyOutcome, RecoveryStatus
from app.services.razorpay.simulated import SimulatedPaymentGateway
from app.services.recovery import OpportunityBuilder, StrategyGenerator
from app.services.recovery.builder import STUCK_CHECKOUT_PAYMENT_TYPE

API_KEY = {"X-API-Key": "dev-key"}
ACTOR = "agent:strategist"

STUCK_AGE = timedelta(minutes=45)  # beyond the 30-minute stuck threshold


@pytest.fixture()
def stuck_payment(db_session, make_payment):
    """A payment created inside the incident window that never resolved —
    still `created` 45 minutes later (detection's abandonment signature)."""

    def _make(**kw) -> models.Payment:
        kw.setdefault("status", "created")
        payment = make_payment(**kw)
        payment.created_at = utcnow() - STUCK_AGE
        db_session.commit()
        return payment

    return _make


@pytest.fixture()
def unpaid_gateway() -> SimulatedPaymentGateway:
    """Simulator whose payment links are NOT paid inline (success_rate 0), so
    verification must arrive through the payment_link.paid webhook."""
    return SimulatedPaymentGateway(success_rate=0.0)


@pytest.fixture()
def api_client(db_session, unpaid_gateway):
    app = create_app()

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_gateway_dependency] = lambda: unpaid_gateway
    with TestClient(app) as c:
        yield c


def _build(db_session, incident):
    return OpportunityBuilder(db_session).build_for_incident(incident.id)


class TestStuckPaymentSelection:
    def test_stuck_created_payment_becomes_opportunity(
        self, db_session, windowed_incident, stuck_payment, make_customer
    ):
        incident = windowed_incident()
        customer = make_customer()
        payment = stuck_payment(
            amount_paise=42_500, customer_id=customer.id, gateway_payment_id="pay_stuck1"
        )

        result = _build(db_session, incident)

        (opp,) = result.created
        assert opp.opportunity_type == STUCK_CHECKOUT_PAYMENT_TYPE
        assert opp.payment_id == payment.id
        assert opp.customer_id == customer.id
        assert opp.amount_paise == 42_500  # the payment's amount, never invented
        assert opp.currency == "INR"
        assert opp.status is RecoveryStatus.PROPOSED
        assert opp.meta["gateway_payment_id"] == "pay_stuck1"

    def test_fresh_created_payment_is_not_yet_stuck(
        self, db_session, windowed_incident, make_payment
    ):
        incident = windowed_incident()
        make_payment(status="created")  # created just now — may still complete

        result = _build(db_session, incident)
        assert result.created == []

    def test_payment_created_before_the_window_is_out_of_scope(
        self, db_session, windowed_incident, stuck_payment
    ):
        incident = windowed_incident()
        payment = stuck_payment()
        payment.created_at = utcnow() - timedelta(hours=3)  # stuck, but pre-window
        db_session.commit()

        result = _build(db_session, incident)
        assert result.created == []

    def test_resolved_payments_are_not_stuck(
        self, db_session, windowed_incident, make_payment
    ):
        incident = windowed_incident()
        captured = make_payment(status="captured", captured=True)
        captured.created_at = utcnow() - STUCK_AGE
        failed = make_payment(status="failed")
        db_session.commit()

        result = _build(db_session, incident)

        # Only the failed payment yields work, and only via the retry source.
        (opp,) = result.created
        assert opp.opportunity_type == "failed_payment_retry"
        assert opp.payment_id == failed.id


class TestCheckoutDedup:
    def test_order_with_stuck_payment_is_not_double_counted(
        self, db_session, windowed_incident, make_order, stuck_payment
    ):
        incident = windowed_incident()
        order = make_order(status="created")
        payment = stuck_payment(order_id=order.id)

        result = _build(db_session, incident)

        # Payment-level wins: exactly one opportunity, of the stuck type.
        (opp,) = result.created
        assert opp.opportunity_type == STUCK_CHECKOUT_PAYMENT_TYPE
        assert opp.payment_id == payment.id
        assert opp.meta["order_id"] == order.id
        assert db_session.query(models.RecoveryOpportunity).count() == 1

    def test_two_stuck_attempts_on_one_order_yield_one_opportunity(
        self, db_session, windowed_incident, make_order, stuck_payment
    ):
        incident = windowed_incident()
        order = make_order(status="created")
        first = stuck_payment(order_id=order.id)
        first.created_at = utcnow() - timedelta(minutes=50)
        second = stuck_payment(order_id=order.id)
        db_session.commit()

        result = _build(db_session, incident)

        (opp,) = result.created
        assert opp.payment_id == first.id  # earliest attempt represents the checkout
        assert [o.id for o in result.existing] == [opp.id]
        assert db_session.query(models.RecoveryOpportunity).count() == 1

    def test_stuck_payment_on_already_represented_order_is_covered(
        self, db_session, windowed_incident, make_order, stuck_payment
    ):
        """First-write wins across builds: an order-level dropped_checkout
        built while the order had no payments already covers the checkout; a
        later-appearing stuck attempt adds no second opportunity."""
        incident = windowed_incident()
        order = make_order(status="created")
        builder = OpportunityBuilder(db_session)
        first = builder.build_for_incident(incident.id)
        db_session.commit()
        (order_opp,) = first.created
        assert order_opp.opportunity_type == "dropped_checkout"

        payment = stuck_payment(order_id=order.id)
        second = builder.build_for_incident(incident.id)

        assert second.created == []
        assert [o.id for o in second.existing] == [order_opp.id]
        assert db_session.query(models.RecoveryOpportunity).count() == 1
        assert payment.id  # the payment exists but stays represented by the order


class TestIdempotency:
    def test_rerun_creates_nothing_for_stuck_payments(
        self, db_session, windowed_incident, stuck_payment
    ):
        incident = windowed_incident()
        stuck_payment()
        builder = OpportunityBuilder(db_session)

        first = builder.build_for_incident(incident.id)
        db_session.commit()
        second = builder.build_for_incident(incident.id)

        assert len(first.created) == 1
        assert second.created == []
        assert len(second.existing) == 1
        assert db_session.query(models.RecoveryOpportunity).count() == 1

    def test_every_stuck_opportunity_is_audited(
        self, db_session, windowed_incident, stuck_payment
    ):
        incident = windowed_incident()
        stuck_payment()

        result = OpportunityBuilder(db_session).build_for_incident(
            incident.id, actor="agent:strategist"
        )

        rows = (
            db_session.query(models.AuditLog)
            .filter_by(entity_type="recovery_opportunity", entity_id=result.created[0].id)
            .all()
        )
        assert [r.action for r in rows] == ["recovery.opportunity_created"]
        assert rows[0].actor == "agent:strategist"
        assert rows[0].details["opportunity_type"] == STUCK_CHECKOUT_PAYMENT_TYPE


class TestStrategyFit:
    def test_retry_is_ineligible_when_nothing_failed(
        self, db_session, make_opportunity, stuck_payment
    ):
        opp = make_opportunity(
            payment=stuck_payment(),
            opportunity_type=STUCK_CHECKOUT_PAYMENT_TYPE,
            amount_paise=50_000,
        )
        rows = StrategyGenerator(db_session).generate(opp)

        retries = [r for r in rows if r.action_type is ActionType.RETRY_PAYMENT]
        assert len(retries) == 2  # immediate + delayed
        assert all(not r.eligibility for r in retries)
        assert all("no charge to retry" in r.reason for r in retries)

    def test_payment_link_is_the_recommendation(
        self, db_session, make_opportunity, stuck_payment
    ):
        opp = make_opportunity(
            payment=stuck_payment(),
            opportunity_type=STUCK_CHECKOUT_PAYMENT_TYPE,
            amount_paise=50_000,
        )
        rows = StrategyGenerator(db_session).generate(opp)
        recommended = next(r for r in rows if r.selected)

        assert recommended.action_type is ActionType.CREATE_PAYMENT_LINK
        assert recommended.rank == 0
        assert recommended.eligibility is True

    def test_stuck_checkout_classifies_as_abandonment(
        self, db_session, make_opportunity, stuck_payment
    ):
        """The stuck state IS the abandonment signal: link fit 0.90 applies,
        not the UNKNOWN-class 0.50 a blank error field would produce."""
        opp = make_opportunity(
            payment=stuck_payment(),
            opportunity_type=STUCK_CHECKOUT_PAYMENT_TYPE,
            amount_paise=50_000,
        )
        rows = StrategyGenerator(db_session).generate(opp)
        link = next(r for r in rows if r.action_type is ActionType.CREATE_PAYMENT_LINK)

        # diagnosis-free evidence 0.80 x ABANDONMENT link fit 0.90
        assert link.confidence == round(0.80 * 0.90, 4)

    def test_failed_payment_retry_behavior_is_unchanged(
        self, db_session, make_opportunity, failed_payment
    ):
        """Guard: the eligibility tightening only affects non-failed links."""
        opp = make_opportunity(payment=failed_payment())
        rows = StrategyGenerator(db_session).generate(opp)
        immediate = next(
            r
            for r in rows
            if r.action_type is ActionType.RETRY_PAYMENT and not r.constraints
        )
        assert immediate.eligibility is True


class TestExecutionEndToEnd:
    def test_execute_fires_link_for_the_payments_own_amount(
        self, db_session, sim_gateway, make_executor, windowed_incident,
        stuck_payment, make_diagnosis,
    ):
        incident = windowed_incident()
        payment = stuck_payment(amount_paise=73_400)
        make_diagnosis(incident, confidence=0.95)  # auto-execute band
        (opp,) = _build(db_session, incident).created
        db_session.commit()

        action = make_executor(sim_gateway).execute(opp.id, actor=ACTOR)

        assert action.action_type is ActionType.CREATE_PAYMENT_LINK
        assert action.amount_paise == payment.amount_paise
        assert len(sim_gateway.payment_links) == 1
        (link,) = sim_gateway.payment_links.values()
        assert link["amount"] == payment.amount_paise  # never AI-invented
        assert link["reference_id"] == action.gateway_request_id
        assert action.status is RecoveryStatus.RECOVERED  # sim pays inline

    def test_execute_link_webhook_recovers_via_testclient(
        self, api_client, db_session, unpaid_gateway, windowed_incident,
        stuck_payment, make_diagnosis,
    ):
        incident = windowed_incident()
        payment = stuck_payment(amount_paise=42_500)
        make_diagnosis(incident, confidence=0.95)
        db_session.commit()

        build = api_client.post(
            "/api/v1/recovery/opportunities/build",
            json={"incident_id": incident.id},
            headers=API_KEY,
        )
        assert build.status_code == 200
        (opp_body,) = build.json()["opportunities"]
        assert opp_body["opportunity_type"] == STUCK_CHECKOUT_PAYMENT_TYPE
        assert opp_body["payment_id"] == payment.id

        plan = api_client.get(f"/api/v1/recovery/{opp_body['id']}/plan")
        recommended = next(s for s in plan.json()["strategies"] if s["selected"])
        assert recommended["action_type"] == "create_payment_link"

        exe = api_client.post(
            f"/api/v1/recovery/{opp_body['id']}/execute",
            json={"actor": "human:console"},
            headers=API_KEY,
        )
        assert exe.status_code == 200
        assert exe.json()["status"] == "VERIFYING"  # link fired, awaiting truth
        assert len(unpaid_gateway.payment_links) == 1
        (link,) = unpaid_gateway.payment_links.values()
        assert link["amount"] == payment.amount_paise
        action = db_session.scalar(
            sa.select(models.RecoveryAction).where(
                models.RecoveryAction.opportunity_id == opp_body["id"]
            )
        )
        assert link["reference_id"] == action.gateway_request_id

        # The customer pays the link: the signed webhook is the verification.
        paid_link = dict(link, status="paid", amount_paid=link["amount"])
        body, signature, event_id = unpaid_gateway.build_event(
            "payment_link.paid", paid_link
        )
        wh = api_client.post(
            "/webhooks/razorpay",
            content=body,
            headers={
                "X-Razorpay-Signature": signature,
                "X-Razorpay-Event-Id": event_id,
            },
        )
        assert wh.status_code == 200

        detail = api_client.get(f"/api/v1/recovery/{opp_body['id']}").json()
        assert detail["status"] == "RECOVERED"
        (action_body,) = detail["actions"]
        assert action_body["status"] == "RECOVERED"
        assert action_body["verified_at"] is not None
        assert len(unpaid_gateway.payment_links) == 1  # exactly one mutation, ever


class TestPolicyStillGates:
    def test_low_confidence_takes_the_approval_lane(
        self, db_session, sim_gateway, make_executor, windowed_incident, stuck_payment
    ):
        # No diagnosis: evidence 0.80 x link fit 0.90 = 0.72 < 0.85 floor.
        incident = windowed_incident()
        stuck_payment()
        (opp,) = _build(db_session, incident).created
        db_session.commit()
        executor = make_executor(sim_gateway)

        action = executor.execute(opp.id, actor=ACTOR)
        assert action.status is RecoveryStatus.PENDING_APPROVAL
        decision = db_session.get(models.PolicyDecisionRecord, action.policy_decision_id)
        assert decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert "approval.confidence" in decision.rules_matched
        assert len(sim_gateway.payment_links) == 0  # nothing reached the gateway

        approved = executor.approve(opp.id, actor="human:ops", note="reviewed")
        assert approved.status is RecoveryStatus.APPROVED

        final = executor.execute(opp.id, actor="human:ops")
        assert final.id == action.id
        assert final.status is RecoveryStatus.RECOVERED  # sim pays links inline
        assert len(sim_gateway.payment_links) == 1

    def test_amount_above_ceiling_requires_approval(
        self, db_session, sim_gateway, make_executor, windowed_incident,
        stuck_payment, make_diagnosis,
    ):
        incident = windowed_incident()
        stuck_payment(amount_paise=600_000)  # INR 6,000 > the INR 5,000 ceiling
        make_diagnosis(incident, confidence=0.95)
        (opp,) = _build(db_session, incident).created
        db_session.commit()

        action = make_executor(sim_gateway).execute(opp.id, actor=ACTOR)

        assert action.status is RecoveryStatus.PENDING_APPROVAL
        decision = db_session.get(models.PolicyDecisionRecord, action.policy_decision_id)
        assert decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert "approval.amount" in decision.rules_matched
        assert len(sim_gateway.payment_links) == 0
