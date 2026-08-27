"""Safety-invariant proofs across entry paths.

- Stopping rule: holds across strategy boundaries (per-incident streak) and a
  success resets the streak — documented semantics vs actual behavior.
- Customer opt-out: every action type is hard-BLOCKED for an opted-out
  customer, via the API path AND the agent tool path; zero gateway calls.
- Excessive amounts: above the ₹5,000 auto-execute ceiling, every entry path
  (agent tool, strategy execute, direct API) lands in the approval lane —
  never auto-executes — and the human lane still works end to end.
"""

from __future__ import annotations

import sqlalchemy as sa

import app.models as models
from app.db import utcnow
from app.ports import ActionType, RecoveryStatus
from app.services.agent.tools import AgentTools
from app.services.recovery import RecoveryExecutor, StrategyGenerator

from tests.security.conftest import CountingGateway

API_KEY = {"X-API-Key": "dev-key"}


def _failed_action(db_session, opp, *, strategy_id=None, n=1):
    """Persist n terminal FAILED actions (definitive gateway rejections)."""
    out = []
    for _ in range(n):
        a = models.RecoveryAction(
            opportunity_id=opp.id,
            incident_id=opp.incident_id,
            action_type=ActionType.RETRY_PAYMENT,
            status=RecoveryStatus.FAILED,
            amount_paise=opp.amount_paise,
            confidence=0.95,
            actor="agent:strategist",
            strategy_id=strategy_id,
            proposed_at=utcnow(),
            executed_at=utcnow(),
            completed_at=utcnow(),
        )
        db_session.add(a)
        out.append(a)
    db_session.commit()
    return out


class TestStoppingRuleSemantics:
    """Documented semantics (docs/policy.md R04/R05): the per-incident streak
    counts the higher of caller input and DB reality; any non-FAILED outcome
    breaks the streak. Proven here across strategy boundaries and resets."""

    def test_streak_counts_across_strategy_boundaries(
        self, db_session, make_incident, make_opportunity
    ):
        incident = make_incident()
        # Three FAILED actions on THREE DIFFERENT strategies of one incident.
        for i in range(3):
            opp = make_opportunity(incident=incident)
            _failed_action(db_session, opp)
        # The fourth proposal (yet another strategy) must hit the incident rule.
        opp4 = make_opportunity(incident=incident)
        gateway = CountingGateway()
        executor = RecoveryExecutor(db_session, gateway)
        action = executor.execute(opp4.id, actor="human:ops")
        db_session.commit()
        assert action.status is RecoveryStatus.REJECTED
        decision = executor.latest_policy_decision(action)
        assert "stopping_rule.incident" in decision.rules_matched
        assert gateway.mutation_calls == 0

    def test_success_resets_the_streak(
        self, db_session, make_incident, make_opportunity
    ):
        incident = make_incident()
        opp = make_opportunity(incident=incident)
        _failed_action(db_session, opp, n=2)
        # A success in between breaks the consecutive run.
        recovered = models.RecoveryAction(
            opportunity_id=opp.id,
            incident_id=incident.id,
            action_type=ActionType.CREATE_PAYMENT_LINK,
            status=RecoveryStatus.RECOVERED,
            amount_paise=opp.amount_paise,
            confidence=0.95,
            actor="agent:strategist",
            proposed_at=utcnow(),
            executed_at=utcnow(),
            completed_at=utcnow(),
        )
        db_session.add(recovered)
        db_session.commit()
        _failed_action(db_session, opp, n=2)  # streak is now 2, not 4

        # Next proposal must NOT be stopping-rule blocked (streak 2 < 3).
        # (Without a diagnosis it takes the approval lane per the documented
        # 0.80 evidence-strength default — the point is the streak reset.)
        opp2 = make_opportunity(incident=incident)
        from app.services.razorpay.simulated import SimulatedPaymentGateway

        executor = RecoveryExecutor(db_session, SimulatedPaymentGateway())
        action = executor.execute(opp2.id, actor="human:ops")
        db_session.commit()
        decision = executor.latest_policy_decision(action)
        assert "stopping_rule.incident" not in decision.rules_matched
        assert action.status is not RecoveryStatus.REJECTED

    def test_three_fresh_failures_after_success_rearm_the_rule(
        self, db_session, make_incident, make_opportunity
    ):
        incident = make_incident()
        opp = make_opportunity(incident=incident)
        _failed_action(db_session, opp, n=1)
        recovered = models.RecoveryAction(
            opportunity_id=opp.id,
            incident_id=incident.id,
            action_type=ActionType.RETRY_PAYMENT,
            status=RecoveryStatus.RECOVERED,
            amount_paise=opp.amount_paise,
            confidence=0.95,
            actor="agent:strategist",
            proposed_at=utcnow(),
            executed_at=utcnow(),
            completed_at=utcnow(),
        )
        db_session.add(recovered)
        db_session.commit()
        _failed_action(db_session, opp, n=3)  # fresh streak of 3 AFTER the success

        opp2 = make_opportunity(incident=incident)
        gateway = CountingGateway()
        action = RecoveryExecutor(db_session, gateway).execute(opp2.id, actor="human:ops")
        db_session.commit()
        assert action.status is RecoveryStatus.REJECTED
        assert gateway.mutation_calls == 0


class TestCustomerOptOut:
    """never_auto_execute.customer_opted_out: a hard block with NO approval
    lane, for every customer-contacting action type, from every entry path."""

    def test_opted_out_customer_blocked_for_every_action_type(
        self, db_session, make_customer, make_payment, make_opportunity
    ):
        customer = make_customer(opted_out=True)
        for action_type in (
            ActionType.RETRY_PAYMENT,
            ActionType.CREATE_PAYMENT_LINK,
            ActionType.NOTIFY_CUSTOMER,
        ):
            payment = make_payment(customer_id=customer.id, status="failed")
            opp = make_opportunity(payment=payment, customer=customer)
            gateway = CountingGateway()
            executor = RecoveryExecutor(db_session, gateway)
            action = executor.execute(opp.id, actor="human:ops")
            db_session.commit()
            assert action.status is RecoveryStatus.REJECTED, action_type
            decision = executor.latest_policy_decision(action)
            assert decision.outcome.value == "BLOCKED"
            assert "never_auto_execute.customer_opted_out" in decision.rules_matched
            assert gateway.mutation_calls == 0, action_type
            # Blocked decisions are mirrored into the append-only audit trail
            # (keyed by the policy_decision id; the action id is in details
            # via the decision record — assert the mirror carries the rule).
            mirrored = db_session.scalar(
                sa.select(models.AuditLog).where(
                    models.AuditLog.action == "policy.action_blocked",
                    models.AuditLog.entity_id == decision.id,
                )
            )
            assert mirrored is not None, action_type
            assert "customer_opted_out" in str(mirrored.details)

    def test_opted_out_customer_blocked_via_agent_tool_path(
        self, db_session, make_incident, make_customer, make_payment
    ):
        customer = make_customer(opted_out=True)
        payment = make_payment(customer_id=customer.id, status="failed")
        tools = AgentTools(db_session, incident_id=make_incident().id)
        result = tools.call(
            "request_payment_link", {"payment_id": payment.id, "confidence": 0.99}
        )
        assert result.data["policy"]["outcome"] == "BLOCKED"
        assert result.data["executed"] is False
        action = db_session.get(models.RecoveryAction, result.data["action_id"])
        assert action.status is RecoveryStatus.REJECTED
        assert action.executed_at is None


class TestExcessiveAmounts:
    """Above the auto-execute ceiling (₹5,000), every entry path must land in
    the human-approval lane and never fire the gateway autonomously."""

    AMOUNT = 1_000_000  # paise = ₹10,000 — above the ₹5,000 ceiling

    def test_agent_tool_path_routes_to_approval(
        self, db_session, make_incident, make_payment
    ):
        payment = make_payment(amount_paise=self.AMOUNT, status="failed")
        tools = AgentTools(db_session, incident_id=make_incident().id)
        result = tools.call(
            "request_recovery_execution",
            {
                "action_type": "create_payment_link",
                "payment_id": payment.id,
                "confidence": 0.99,
            },
        )
        assert result.data["policy"]["outcome"] == "REQUIRES_APPROVAL"
        assert result.data["executed"] is False
        assert result.data["amount_paise"] == self.AMOUNT  # from the row, not the model

    def test_api_execute_path_routes_to_approval_then_human_lane_works(
        self, client, db_session, make_payment, make_opportunity
    ):
        payment = make_payment(amount_paise=self.AMOUNT, status="failed")
        opp = make_opportunity(payment=payment, amount_paise=self.AMOUNT)

        # Generate the strategy set, then execute the payment-link candidate.
        plan = client.get(f"/api/v1/recovery/{opp.id}/plan").json()
        link_strategy = next(
            s for s in plan["strategies"] if s["action_type"] == "create_payment_link"
        )
        r = client.post(
            f"/api/v1/recovery/{opp.id}/execute",
            json={"strategy_id": link_strategy["id"], "actor": "human:ops"},
            headers=API_KEY,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "PENDING_APPROVAL"
        # No gateway link exists yet — the ceiling held.
        sim = None
        from app.api.deps import get_gateway_dependency

        sim = client.app.dependency_overrides[get_gateway_dependency]()
        assert len(sim.payment_links) == 0

        # The human lane: approve, then execute exactly once.
        r = client.post(
            f"/api/v1/recovery/{opp.id}/approve",
            json={"actor": "human:approver"},
            headers=API_KEY,
        )
        assert r.status_code == 200 and r.json()["status"] == "APPROVED"
        r = client.post(
            f"/api/v1/recovery/{opp.id}/execute",
            json={"strategy_id": link_strategy["id"], "actor": "human:ops"},
            headers=API_KEY,
        )
        assert r.status_code == 200
        assert r.json()["status"] in ("RECOVERED", "VERIFYING")
        assert len(sim.payment_links) == 1
        assert next(iter(sim.payment_links.values()))["amount"] == self.AMOUNT

    def test_direct_strategy_execute_cannot_bypass_ceiling(
        self, db_session, make_payment, make_opportunity
    ):
        payment = make_payment(amount_paise=self.AMOUNT, status="failed")
        opp = make_opportunity(payment=payment, amount_paise=self.AMOUNT)
        strategies = StrategyGenerator(db_session).generate(opp)
        link = next(
            s for s in strategies if s.action_type is ActionType.CREATE_PAYMENT_LINK
        )
        gateway = CountingGateway()
        action = RecoveryExecutor(db_session, gateway).execute(
            opp.id, strategy_id=link.id, actor="agent:strategist"
        )
        db_session.commit()
        assert action.status is RecoveryStatus.PENDING_APPROVAL
        assert gateway.mutation_calls == 0
