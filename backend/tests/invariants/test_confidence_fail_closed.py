"""Invariant 11: malformed confidence (NaN / +/-Inf / negative / >1 /
non-numeric) cannot reach execution.

Existing proofs (referenced from docs/payment-invariants.md):
- tests/policy/test_policy_engine.py::TestFailClosed::test_invalid_confidence_blocked
  [nan, inf, -0.1, 1.01, high] — the gate BLOCKs every malformed value.
- tests/security/test_input_abuse.py::TestExtremeNumericInputs — NaN/Inf and
  non-numeric confidence via the agent tool path: ToolError/TypeError BEFORE
  any row exists, and the dry-run gate BLOCKs as malformed.confidence.

Gaps closed here (finite out-of-range was only proven at gate level):
1. the agent MUTATION tools reject finite out-of-range confidence (-0.5, 1.5)
   before any row exists;
2. a hand-forged action row carrying an out-of-range confidence is BLOCKED
   by the executor's re-gate with zero gateway calls (defense in depth even
   if a producer skipped validation).
"""

from __future__ import annotations

import pytest

import app.models as models
from app.ports import PolicyOutcome, RecoveryStatus
from app.services.agent.tools import AgentTools, ToolError
from app.services.recovery import RecoveryExecutor

from tests.security.conftest import CountingGateway


@pytest.mark.parametrize("bad", [-0.5, 1.5])
def test_out_of_range_confidence_rejected_by_agent_mutation_tools(
    db_session, make_incident, make_payment, bad
):
    incident = make_incident()
    payment = make_payment(status="failed")
    tools = AgentTools(db_session, incident_id=incident.id)
    with pytest.raises(ToolError, match="invalid confidence"):
        tools.call(
            "request_recovery_execution",
            {
                "action_type": "create_payment_link",
                "payment_id": payment.id,
                "confidence": bad,
            },
        )
    # fail closed BEFORE any row: no action, no opportunity, no decision
    assert db_session.query(models.RecoveryAction).count() == 0
    assert db_session.query(models.RecoveryOpportunity).count() == 0
    assert db_session.query(models.PolicyDecisionRecord).count() == 0


@pytest.mark.parametrize("bad", [-0.1, 1.5])
def test_executor_gate_blocks_out_of_range_confidence_with_zero_gateway_calls(
    db_session, make_opportunity, make_proposed_action, bad
):
    opp = make_opportunity()
    make_proposed_action(opp, confidence=bad)
    db_session.commit()
    gateway = CountingGateway(success_rate=1.0)

    action = RecoveryExecutor(db_session, gateway).execute(opp.id, actor="human:invariant")
    db_session.commit()

    assert action.status is RecoveryStatus.REJECTED
    assert gateway.mutation_calls == 0
    assert gateway.fetch_calls == 0
    decision = db_session.get(models.PolicyDecisionRecord, action.policy_decision_id)
    assert decision.outcome is PolicyOutcome.BLOCKED
    assert "malformed.confidence" in decision.rules_matched
