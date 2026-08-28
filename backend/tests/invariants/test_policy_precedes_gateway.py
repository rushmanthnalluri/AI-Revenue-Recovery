"""Invariant 6: policy evaluation ALWAYS precedes any financial mutation.

No code path may reach the payment gateway without a persisted policy
decision that authorizes THIS action (ALLOWED), or — for the human lane — a
persisted REQUIRES_APPROVAL decision plus a recorded human approval.

Existing proofs (referenced from docs/payment-invariants.md):
- tests/policy/test_policy_engine.py::TestPersistenceAndAudit
- tests/recovery/test_executor.py (decision linked before fire)
- tests/agent/test_tools.py (request_* tools evaluate, never execute)

This module adds the missing MECHANICAL sweep: a spy gateway sits on the
transport seam and fails the test if ANY mutating call arrives whose
idempotency key (recovery_actions.gateway_request_id) is not already backed
by a persisted, authorizing policy decision — checked at the moment the
gateway is touched, across every entry path:

1. executor auto-execute (ALLOWED),
2. human approval lane (REQUIRES_APPROVAL -> approve -> execute),
3. agent request_* tool row executed via the executor (re-gated),
4. blocked action (decision persisted, zero gateway calls),
5. structural: the agent tool layer holds no gateway handle at all.
"""

from __future__ import annotations

import inspect

import pytest
import sqlalchemy as sa

import app.models as models
from app.ports import ActionType, PolicyOutcome, RecoveryStatus
from app.services.agent.tools import AgentTools
from app.services.recovery import RecoveryExecutor
from app.services.recovery.strategies import StrategyGenerator

from tests.security.conftest import CountingGateway

ACTOR = "human:invariant"


class PolicyAssertingGateway(CountingGateway):
    """Simulator double that verifies the policy-first invariant AT the
    transport seam: every mutating call must carry the idempotency key of a
    recovery action that already has a persisted policy decision authorizing
    it (ALLOWED, or REQUIRES_APPROVAL + recorded human approval)."""

    def __init__(self, session, **kw):
        super().__init__(**kw)
        self._session = session
        self.violations: list[str] = []
        self.keys_seen: list[str | None] = []

    def _check(self, idempotency_key) -> None:
        self.keys_seen.append(idempotency_key)
        action = self._session.scalar(
            sa.select(models.RecoveryAction).where(
                models.RecoveryAction.gateway_request_id == idempotency_key
            )
        )
        if action is None:
            self.violations.append(
                f"gateway mutation with idempotency key {idempotency_key!r} "
                "not traceable to any recovery action"
            )
            return
        record = self._session.get(models.PolicyDecisionRecord, action.policy_decision_id)
        if record is None:
            self.violations.append(
                f"action {action.id} reached the gateway with NO persisted policy decision"
            )
            return
        if record.outcome is PolicyOutcome.ALLOWED:
            return
        if record.outcome is PolicyOutcome.REQUIRES_APPROVAL and action.approved_by:
            return  # human lane: approval is the authorization
        self.violations.append(
            f"action {action.id} reached the gateway on decision "
            f"{record.outcome.value} without human approval"
        )

    def create_order(self, **kw):
        self._check(kw.get("idempotency_key"))
        return super().create_order(**kw)

    def create_payment_link(self, **kw):
        self._check(kw.get("idempotency_key"))
        return super().create_payment_link(**kw)

    def create_subscription(self, **kw):
        self._check(kw.get("idempotency_key"))
        return super().create_subscription(**kw)


def _link_strategy(db_session, opp):
    rows = StrategyGenerator(db_session).generate(opp)
    return next(r for r in rows if r.action_type is ActionType.CREATE_PAYMENT_LINK)


def test_auto_execute_persists_allow_decision_before_fire(
    db_session, make_opportunity, make_diagnosis, abandoned_payment
):
    opp = make_opportunity(payment=abandoned_payment())
    make_diagnosis(db_session.get(models.Incident, opp.incident_id), confidence=0.95)
    strategy = _link_strategy(db_session, opp)
    db_session.commit()

    gateway = PolicyAssertingGateway(db_session, success_rate=1.0)
    action = RecoveryExecutor(db_session, gateway).execute(
        opp.id, strategy_id=strategy.id, actor=ACTOR
    )
    db_session.commit()

    assert action.status is RecoveryStatus.RECOVERED
    assert gateway.mutation_calls == 1
    assert gateway.violations == []
    decision = db_session.get(models.PolicyDecisionRecord, action.policy_decision_id)
    assert decision.outcome is PolicyOutcome.ALLOWED
    # ordering: the decision predates the gateway fire
    assert decision.decided_at <= action.executed_at


def test_human_approval_lane_authorizes_before_fire(
    db_session, make_opportunity, make_proposed_action
):
    opp = make_opportunity()
    make_proposed_action(opp, confidence=0.80)  # below the 0.85 floor
    db_session.commit()
    gateway = PolicyAssertingGateway(db_session, success_rate=1.0)
    executor = RecoveryExecutor(db_session, gateway)

    pending = executor.execute(opp.id, actor=ACTOR)
    assert pending.status is RecoveryStatus.PENDING_APPROVAL
    assert gateway.mutation_calls == 0  # nothing fired before approval

    executor.approve(opp.id, actor="human:approver", note="reviewed")
    final = executor.execute(opp.id, actor=ACTOR)
    db_session.commit()

    assert final.status is RecoveryStatus.RECOVERED
    assert final.approved_by == "human:approver"
    assert gateway.mutation_calls == 1
    assert gateway.violations == []
    decision = db_session.get(models.PolicyDecisionRecord, final.policy_decision_id)
    assert decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
    assert decision.decided_at <= final.executed_at


def test_agent_tool_row_is_regated_before_fire_and_tool_never_fires(
    db_session, make_incident, make_payment
):
    """The agent request_* path: the tool itself must cause ZERO gateway
    calls, and the PROPOSED row it creates must be re-gated by the executor
    (its own persisted decision) before any mutation."""
    incident = make_incident()
    payment = make_payment(amount_paise=100_000, status="failed")
    gateway = PolicyAssertingGateway(db_session, success_rate=1.0)

    tools = AgentTools(db_session, incident_id=incident.id)
    result = tools.call(
        "request_payment_link", {"payment_id": payment.id, "confidence": 0.99}
    )
    db_session.commit()
    assert result.data["policy"]["outcome"] == "ALLOWED"
    assert result.data["executed"] is False
    assert gateway.mutation_calls == 0  # the tool NEVER touches the gateway

    action = db_session.get(models.RecoveryAction, result.data["action_id"])
    assert action.status is RecoveryStatus.POLICY_EVALUATED
    tool_decision = db_session.get(
        models.PolicyDecisionRecord, action.policy_decision_id
    )
    assert tool_decision is not None  # the tool's own evaluation is persisted

    fired = RecoveryExecutor(db_session, gateway).execute(
        action.opportunity_id, actor=ACTOR
    )
    db_session.commit()
    assert fired.id == action.id
    assert fired.status is RecoveryStatus.RECOVERED
    assert gateway.mutation_calls == 1
    assert gateway.violations == []
    # the executor re-gated: a second persisted decision now authorizes the fire
    decisions = db_session.scalars(
        sa.select(models.PolicyDecisionRecord).where(
            models.PolicyDecisionRecord.action_id == action.id
        )
    ).all()
    assert len(decisions) >= 2
    assert decisions[-1].outcome is PolicyOutcome.ALLOWED


def test_blocked_action_persists_decision_and_never_fires(
    db_session, make_customer, make_payment, make_opportunity
):
    customer = make_customer(opted_out=True)
    payment = make_payment(customer_id=customer.id, status="failed")
    opp = make_opportunity(payment=payment, customer=customer)
    gateway = PolicyAssertingGateway(db_session, success_rate=1.0)

    action = RecoveryExecutor(db_session, gateway).execute(opp.id, actor=ACTOR)
    db_session.commit()

    assert action.status is RecoveryStatus.REJECTED
    assert gateway.mutation_calls == 0  # blocked => transport never touched
    assert gateway.keys_seen == []
    decision = db_session.get(models.PolicyDecisionRecord, action.policy_decision_id)
    assert decision is not None  # even blocks are persisted decisions
    assert decision.outcome is PolicyOutcome.BLOCKED


def test_agent_tools_have_no_gateway_handle(db_session, make_incident):
    """Structural half of invariant 6 for the agent path: AgentTools cannot
    reach the gateway even in principle — it is constructed without one."""
    params = set(inspect.signature(AgentTools.__init__).parameters)
    assert "gateway" not in params
    tools = AgentTools(db_session, incident_id=make_incident().id)
    assert not any("gateway" in name.lower() for name in vars(tools))
    from app.ports import PaymentGateway

    assert not any(
        isinstance(v, PaymentGateway) for v in vars(tools).values()
    ), "agent tool layer holds a live gateway handle"
