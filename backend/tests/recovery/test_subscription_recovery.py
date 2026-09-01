"""Subscription-aware recovery: Razorpay subscriptions stuck in `pending` /
`halted` become `subscription_halted` opportunities, recovered through the
arrears lane — a fresh payment link for the subscription's outstanding
amount. Razorpay's own dunning retries stop in those states; that gap is
PulseRecover's differentiation lane.

Proven here:
- the builder sources exactly-once opportunities from stuck subscriptions
  (per-incident dedupe, same as failed payments), never from healthy ones;
- environment scoping rides on source_type: an incident only ever picks up
  its own environment's subscriptions, and the opportunity inherits the
  incident's environment stamp;
- the strategy generator proposes CREATE_PAYMENT_LINK (arrears) first and
  marks retry_payment ineligible (there is no failed charge to resubmit);
- execution fires a payment link for the subscription's OWN outstanding
  amount, still behind the deterministic policy gate.
"""

import pytest
import sqlalchemy as sa

import app.models as models
from app.models.base import (
    ENVIRONMENT_REAL_TEST,
    ENVIRONMENT_RESEARCH,
    SOURCE_TYPE_RAZORPAY_TEST,
    SOURCE_TYPE_SIMULATOR,
)
from app.ports import ActionType, PolicyOutcome, RecoveryStatus
from app.services.recovery import OpportunityBuilder, StrategyGenerator
from app.services.recovery.builder import SUBSCRIPTION_HALTED_TYPE

ACTOR = "agent:strategist"


def _build(db_session, incident):
    return OpportunityBuilder(db_session).build_for_incident(incident.id)


class TestSubscriptionSelection:
    @pytest.mark.parametrize("status", ["halted", "pending"])
    def test_stuck_subscription_becomes_opportunity(
        self, db_session, windowed_incident, make_subscription, make_customer, status
    ):
        incident = windowed_incident()
        customer = make_customer()
        subscription = make_subscription(
            status=status, customer_id=customer.id, amount_paise=18_500,
            gateway_subscription_id=f"sub_{status}1",
        )

        result = _build(db_session, incident)

        (opp,) = result.created
        assert opp.opportunity_type == SUBSCRIPTION_HALTED_TYPE
        assert opp.subscription_id == subscription.id
        assert opp.payment_id is None
        assert opp.customer_id == customer.id
        assert opp.amount_paise == 18_500  # the subscription's outstanding amount
        assert opp.currency == "INR"
        assert opp.status is RecoveryStatus.PROPOSED
        assert opp.meta["subscription_status"] == status
        assert opp.meta["gateway_subscription_id"] == f"sub_{status}1"
        assert "dunning" in opp.reason

    @pytest.mark.parametrize("status", ["created", "authenticated", "active", "paused", "cancelled", "completed"])
    def test_healthy_subscriptions_yield_nothing(
        self, db_session, windowed_incident, make_subscription, status
    ):
        incident = windowed_incident()
        make_subscription(status=status)

        result = _build(db_session, incident)
        assert result.created == []

    def test_subscription_without_customer_still_builds(
        self, db_session, windowed_incident, make_subscription
    ):
        incident = windowed_incident()
        make_subscription(customer_id=None)

        (opp,) = _build(db_session, incident).created
        assert opp.customer_id is None


class TestIdempotency:
    def test_rerun_creates_nothing(
        self, db_session, windowed_incident, make_subscription
    ):
        incident = windowed_incident()
        make_subscription()
        builder = OpportunityBuilder(db_session)

        first = builder.build_for_incident(incident.id)
        db_session.commit()
        second = builder.build_for_incident(incident.id)

        assert len(first.created) == 1
        assert second.created == []
        assert [o.id for o in second.existing] == [first.created[0].id]
        assert db_session.query(models.RecoveryOpportunity).count() == 1

    def test_every_subscription_opportunity_is_audited(
        self, db_session, windowed_incident, make_subscription
    ):
        incident = windowed_incident()
        subscription = make_subscription()

        result = OpportunityBuilder(db_session).build_for_incident(incident.id)

        rows = (
            db_session.query(models.AuditLog)
            .filter_by(entity_type="recovery_opportunity", entity_id=result.created[0].id)
            .all()
        )
        assert [r.action for r in rows] == ["recovery.opportunity_created"]
        assert rows[0].details["opportunity_type"] == SUBSCRIPTION_HALTED_TYPE
        assert rows[0].details["subscription_id"] == subscription.id


class TestEnvironmentScoping:
    def test_research_incident_picks_only_simulator_subscriptions(
        self, db_session, windowed_incident, make_subscription
    ):
        incident = windowed_incident(environment=ENVIRONMENT_RESEARCH)
        sim_sub = make_subscription(source_type=SOURCE_TYPE_SIMULATOR)
        make_subscription(source_type=SOURCE_TYPE_RAZORPAY_TEST)  # real_test row

        result = _build(db_session, incident)

        (opp,) = result.created
        assert opp.subscription_id == sim_sub.id
        assert opp.environment == ENVIRONMENT_RESEARCH

    def test_real_test_incident_picks_only_razorpay_subscriptions(
        self, db_session, windowed_incident, make_subscription
    ):
        incident = windowed_incident(environment=ENVIRONMENT_REAL_TEST)
        real_sub = make_subscription(source_type=SOURCE_TYPE_RAZORPAY_TEST)
        make_subscription(source_type=SOURCE_TYPE_SIMULATOR)  # research row

        result = _build(db_session, incident)

        (opp,) = result.created
        assert opp.subscription_id == real_sub.id
        assert opp.environment == ENVIRONMENT_REAL_TEST


class TestArrearsStrategy:
    def _opp(self, db_session, windowed_incident, make_subscription, *, amount=18_500):
        incident = windowed_incident()
        make_subscription(amount_paise=amount)
        return _build(db_session, incident).created[0]

    def test_payment_link_is_the_arrears_recommendation(
        self, db_session, windowed_incident, make_subscription
    ):
        opp = self._opp(db_session, windowed_incident, make_subscription)
        rows = StrategyGenerator(db_session).generate(opp)

        recommended = next(r for r in rows if r.selected)
        assert recommended.action_type is ActionType.CREATE_PAYMENT_LINK
        assert recommended.eligibility is True
        assert "arrears" in recommended.reason
        retries = [r for r in rows if r.action_type is ActionType.RETRY_PAYMENT]
        assert all(not r.eligibility for r in retries)

    def test_execute_fires_link_for_the_subscriptions_own_amount(
        self, db_session, sim_gateway, make_executor, windowed_incident, make_subscription
    ):
        # No diagnosis: evidence 0.80 x SOFT_DECLINE link fit 0.75 = 0.60,
        # under the 0.85 auto-execute floor — the approval lane, then fire.
        opp = self._opp(db_session, windowed_incident, make_subscription, amount=18_500)
        db_session.commit()
        executor = make_executor(sim_gateway)

        action = executor.execute(opp.id, actor=ACTOR)
        assert action.status is RecoveryStatus.PENDING_APPROVAL
        decision = db_session.get(models.PolicyDecisionRecord, action.policy_decision_id)
        assert decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert len(sim_gateway.payment_links) == 0  # the gate still decides first

        executor.approve(opp.id, actor="human:ops", note="reviewed")
        final = executor.execute(opp.id, actor="human:ops")

        assert final.id == action.id
        assert final.action_type is ActionType.CREATE_PAYMENT_LINK
        assert final.amount_paise == 18_500  # never AI-invented
        assert final.status is RecoveryStatus.RECOVERED  # sim pays links inline
        (link,) = sim_gateway.payment_links.values()
        assert link["amount"] == 18_500
        assert link["reference_id"] == final.gateway_request_id
