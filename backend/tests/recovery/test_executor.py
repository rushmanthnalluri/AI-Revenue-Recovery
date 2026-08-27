"""Executor lifecycle tests: the recovery_actions state machine, happy paths
and human-decision paths. Failure-mode proofs live in test_failure_modes.py.
"""

import pytest

import app.models as models
from app.ports import ActionType, PolicyOutcome, RecoveryStatus
from app.services.recovery import (
    InvalidStateError,
    RecoveryExecutor,
    RecoveryNotFoundError,
)
from app.services.recovery.strategies import StrategyGenerator

ACTOR = "human:console"


def _link_strategy(db_session, opp):
    """The create_payment_link strategy row for opp (generates the set)."""
    rows = StrategyGenerator(db_session).generate(opp)
    return next(r for r in rows if r.action_type is ActionType.CREATE_PAYMENT_LINK)


class TestAutoExecute:
    def test_allowed_action_executes_and_verifies_inline(
        self, db_session, sim_gateway, make_executor, make_opportunity,
        make_diagnosis, abandoned_payment,
    ):
        # abandonment class: the payment-link strategy is auto-executable
        # (0.95 diagnosis x 0.90 fit = 0.855 >= 0.85 floor)
        opp = make_opportunity(payment=abandoned_payment())
        make_diagnosis(db_session.get(models.Incident, opp.incident_id), confidence=0.95)
        strategy = _link_strategy(db_session, opp)
        db_session.commit()

        action = make_executor(sim_gateway).execute(
            opp.id, strategy_id=strategy.id, actor=ACTOR
        )

        assert action.status is RecoveryStatus.RECOVERED
        assert action.attempts == 1
        assert action.executed_at is not None
        assert action.verified_at is not None
        assert action.completed_at is not None
        assert action.gateway_request_id
        # the gateway saw exactly one mutation, keyed by our idempotency id
        (link,) = sim_gateway.payment_links.values()
        assert link["reference_id"] == action.gateway_request_id
        assert link["status"] == "paid"
        # policy decision persisted and linked
        decision = db_session.get(models.PolicyDecisionRecord, action.policy_decision_id)
        assert decision is not None
        assert decision.outcome is PolicyOutcome.ALLOWED
        # opportunity shadow status follows the action
        db_session.refresh(opp)
        assert opp.status is RecoveryStatus.RECOVERED

    def test_retry_payment_waits_in_verifying_for_webhook(
        self, db_session, sim_gateway, make_executor, make_opportunity,
        make_diagnosis, failed_payment,
    ):
        opp = make_opportunity(payment=failed_payment())
        make_diagnosis(db_session.get(models.Incident, opp.incident_id), confidence=0.95)
        db_session.commit()

        action = make_executor(sim_gateway).execute(opp.id, actor=ACTOR)

        # recommended strategy is retry_payment -> fresh order; the simulator
        # never pays an order inline, so the action awaits webhook/fetch truth.
        assert action.action_type is ActionType.RETRY_PAYMENT
        assert action.status is RecoveryStatus.VERIFYING
        (order,) = sim_gateway.orders.values()
        assert order["receipt"] == action.gateway_request_id

    def test_agent_created_proposed_row_is_valid_input(
        self, db_session, sim_gateway, make_executor, make_opportunity,
        make_proposed_action,
    ):
        """The agent package creates PROPOSED rows via policy; execute picks
        them up instead of creating a duplicate action."""
        opp = make_opportunity()
        proposed = make_proposed_action(
            opp, action_type=ActionType.CREATE_PAYMENT_LINK, confidence=0.95
        )
        db_session.commit()

        action = make_executor(sim_gateway).execute(opp.id, actor=ACTOR)

        assert action.id == proposed.id
        assert action.status is RecoveryStatus.RECOVERED
        assert action.actor == "agent:strategist"  # proposer preserved
        assert len(sim_gateway.payment_links) == 1


class TestApprovalFlow:
    def test_low_confidence_goes_to_pending_then_approve_then_execute(
        self, db_session, sim_gateway, make_executor, make_opportunity, failed_payment
    ):
        # No diagnosis: evidence 0.80 -> confidence below the 0.85 floor.
        opp = make_opportunity(payment=failed_payment())
        db_session.commit()
        executor = make_executor(sim_gateway)

        action = executor.execute(opp.id, actor=ACTOR)
        assert action.status is RecoveryStatus.PENDING_APPROVAL
        decision = db_session.get(models.PolicyDecisionRecord, action.policy_decision_id)
        assert decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert "approval.confidence" in decision.rules_matched
        assert len(sim_gateway.payment_links) == 0  # nothing fired yet

        with pytest.raises(InvalidStateError, match="await"):
            executor.execute(opp.id, actor=ACTOR)

        approved = executor.approve(opp.id, actor="human:ops", note="looks safe")
        assert approved.status is RecoveryStatus.APPROVED
        assert approved.approved_by == "human:ops"
        assert approved.approved_at is not None

        final = executor.execute(opp.id, actor=ACTOR)
        assert final.id == action.id
        assert final.status is RecoveryStatus.VERIFYING  # retry fired, awaiting truth
        assert len(sim_gateway.orders) == 1

    def test_approve_requires_pending_state(
        self, db_session, sim_gateway, make_executor, make_opportunity
    ):
        opp = make_opportunity()
        with pytest.raises(InvalidStateError):
            make_executor(sim_gateway).approve(opp.id, actor="human:ops")

    def test_reject_from_pending_approval(
        self, db_session, sim_gateway, make_executor, make_opportunity, failed_payment
    ):
        opp = make_opportunity(payment=failed_payment())
        db_session.commit()
        executor = make_executor(sim_gateway)
        action = executor.execute(opp.id, actor=ACTOR)
        assert action.status is RecoveryStatus.PENDING_APPROVAL

        rejected = executor.reject(opp.id, actor="human:ops", reason="not worth it")

        assert rejected.status is RecoveryStatus.REJECTED
        assert rejected.note == "not worth it"
        assert rejected.completed_at is not None
        assert len(sim_gateway.orders) == 0
        db_session.refresh(opp)
        assert opp.status is RecoveryStatus.REJECTED


class TestEscalateAndCancel:
    def test_escalate_from_pending_approval(
        self, db_session, sim_gateway, make_executor, make_opportunity, failed_payment
    ):
        opp = make_opportunity(payment=failed_payment())
        db_session.commit()
        executor = make_executor(sim_gateway)
        executor.execute(opp.id, actor=ACTOR)

        action = executor.escalate(opp.id, actor="human:ops", reason="needs a human")

        assert action.status is RecoveryStatus.ESCALATED
        assert action.completed_at is not None

    def test_cancel_proposed_action(
        self, db_session, sim_gateway, make_executor, make_opportunity,
        make_proposed_action,
    ):
        opp = make_opportunity()
        make_proposed_action(opp)
        db_session.commit()

        action = make_executor(sim_gateway).cancel(
            opp.id, actor="human:ops", reason="customer already paid offline"
        )
        assert action.status is RecoveryStatus.CANCELLED

    def test_cancel_refused_once_fired(
        self, db_session, sim_gateway, make_executor, make_opportunity,
        make_proposed_action,
    ):
        opp = make_opportunity()
        make_proposed_action(opp, status=RecoveryStatus.EXECUTING)
        db_session.commit()
        with pytest.raises(InvalidStateError, match="pre-execution"):
            make_executor(sim_gateway).cancel(opp.id, actor="human:ops")

    def test_escalate_strategy_executes_without_gateway(
        self, db_session, sim_gateway, make_executor, make_opportunity, failed_payment
    ):
        """Executing an escalate_human strategy terminates ESCALATED with zero
        gateway calls (safe actions are always policy-ALLOWED)."""
        opp = make_opportunity(payment=failed_payment())
        escalate = next(
            s
            for s in StrategyGenerator(db_session).generate(opp)
            if s.action_type is ActionType.ESCALATE_HUMAN
        )
        db_session.commit()

        action = make_executor(sim_gateway).execute(
            opp.id, strategy_id=escalate.id, actor=ACTOR
        )

        assert action.status is RecoveryStatus.ESCALATED
        assert not sim_gateway.orders and not sim_gateway.payment_links

    def test_opportunity_level_reject_and_escalate_without_action(
        self, db_session, sim_gateway, make_executor, make_opportunity
    ):
        opp = make_opportunity()
        executor = make_executor(sim_gateway)

        assert executor.escalate(opp.id, actor="human:ops", reason="vip") is None
        db_session.refresh(opp)
        assert opp.status is RecoveryStatus.ESCALATED
        rows = (
            db_session.query(models.AuditLog)
            .filter_by(entity_type="recovery_opportunity", entity_id=opp.id)
            .all()
        )
        assert [r.action for r in rows] == ["recovery.opportunity.escalated"]

        opp2 = make_opportunity()
        assert executor.reject(opp2.id, actor="human:ops", reason="noise") is None
        db_session.refresh(opp2)
        assert opp2.status is RecoveryStatus.REJECTED


class TestGuards:
    def test_unknown_opportunity_404s(
        self, db_session, sim_gateway, make_executor
    ):
        with pytest.raises(RecoveryNotFoundError):
            make_executor(sim_gateway).execute("opp_missing", actor=ACTOR)

    def test_execute_refuses_in_flight_action(
        self, db_session, sim_gateway, make_executor, make_opportunity,
        make_proposed_action,
    ):
        opp = make_opportunity()
        make_proposed_action(opp, status=RecoveryStatus.VERIFYING)
        db_session.commit()
        with pytest.raises(InvalidStateError, match="in flight"):
            make_executor(sim_gateway).execute(opp.id, actor=ACTOR)

    def test_ineligible_strategy_cannot_be_executed(
        self, db_session, sim_gateway, make_executor, make_opportunity,
        make_customer, failed_payment,
    ):
        customer = make_customer(opted_out=True)
        opp = make_opportunity(payment=failed_payment(customer_id=customer.id))
        rows = StrategyGenerator(db_session).generate(opp)
        notify = next(r for r in rows if r.action_type is ActionType.NOTIFY_CUSTOMER)
        db_session.commit()
        with pytest.raises(InvalidStateError, match="ineligible"):
            make_executor(sim_gateway).execute(
                opp.id, strategy_id=notify.id, actor=ACTOR
            )


class TestAuditTrail:
    def test_every_transition_is_audited_with_actor_and_request_id(
        self, db_session, sim_gateway, make_executor, make_opportunity,
        make_diagnosis, abandoned_payment,
    ):
        opp = make_opportunity(payment=abandoned_payment())
        make_diagnosis(db_session.get(models.Incident, opp.incident_id), confidence=0.95)
        strategy = _link_strategy(db_session, opp)
        db_session.commit()

        action = make_executor(sim_gateway).execute(
            opp.id, strategy_id=strategy.id, actor=ACTOR, request_id="req-test-1"
        )
        assert action.status is RecoveryStatus.RECOVERED

        rows = (
            db_session.query(models.AuditLog)
            .filter_by(entity_type="recovery_action", entity_id=action.id)
            .order_by(models.AuditLog.created_at, models.AuditLog.id)
            .all()
        )
        transitions = [r.action for r in rows]
        assert transitions == [
            "recovery.action.proposed",
            "recovery.action.policy_evaluated",
            "recovery.action.executing",
            "recovery.action.verifying",
            "recovery.action.recovered",
        ]
        for row in rows:
            assert row.actor == ACTOR
            assert row.request_id == "req-test-1"
            assert row.details.get("to_status") is not None
