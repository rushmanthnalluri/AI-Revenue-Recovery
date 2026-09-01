"""Tool-layer tests: whitelist enforcement, evidence traceability, amounts
from original rows only, policy gating on the mutation path, and NO gateway
access anywhere."""

import sqlalchemy as sa
import pytest

from app.models import AuditLog, PolicyDecisionRecord, RecoveryAction
from app.ports import PolicyOutcome, RecoveryStatus
from app.services.agent.tools import AgentTools, ToolError, ToolNotAllowed


@pytest.fixture()
def tools(db_session, agent_seed):
    return AgentTools(db_session, incident_id=agent_seed["incident"].id)


# -- whitelist / restriction ---------------------------------------------------


def test_reasoner_cannot_invoke_non_whitelisted_callables(tools):
    with pytest.raises(ToolNotAllowed):
        tools.call("drop_tables")
    with pytest.raises(ToolNotAllowed):
        tools.call("execute_refund_now")
    with pytest.raises(ToolNotAllowed):
        tools.call("get_db")
    # nothing was logged as a call
    assert tools.calls == []


def test_whitelisted_tools_are_exactly_the_documented_nine(tools):
    assert set(tools.tool_names) == {
        "get_incident",
        "get_payment_stats",
        "get_failure_distribution",
        "get_customer_history",
        "get_revenue_at_risk",
        "get_recovery_candidates",
        "propose_recovery_strategy",
        "request_payment_link",
        "request_recovery_execution",
    }
    spec_names = {s["function"]["name"] for s in tools.specs()}
    assert spec_names == set(tools.tool_names)


# -- read tools -----------------------------------------------------------------


def test_get_payment_stats_matches_seeded_window(tools, agent_seed):
    result = tools.call("get_payment_stats")
    w = result.data["window"]
    assert w["failed"] == 10
    assert w["captured"] == 3
    assert w["total"] == 13
    assert w["failed_amount_paise"] == sum(p.amount_paise for p in agent_seed["failed_payments"])
    assert result.data["baseline"]["failed"] == 1
    # evidence ids are the sampled failed payment ids
    assert result.evidence_ids
    assert all(e.startswith("pay_") for e in result.evidence_ids)


def test_get_failure_distribution_dominant_class(tools):
    result = tools.call("get_failure_distribution")
    d = result.data
    assert d["failed_count"] == 10
    assert d["by_error_reason"]["bank_technical_error"] == 7
    assert d["dominant_failure_class"] == "soft_decline"  # bank_technical_error
    assert len(result.evidence_ids) == 10


def test_get_revenue_at_risk_originates_from_revenue_engine(tools, agent_seed):
    result = tools.call("get_revenue_at_risk")
    d = result.data
    assert d["currency"] == "INR"
    assert d["observed_loss"]["lower_paise"] <= d["observed_loss"]["upper_paise"]
    assert d["recoverable"]["upper_paise"] >= 0
    assert d["actual_recovered_paise"] == 0
    assert result.evidence_ids == [agent_seed["incident"].id]


def test_get_recovery_candidates_derives_from_failed_payments(tools):
    result = tools.call("get_recovery_candidates")
    d = result.data
    assert d["source"] == "derived_from_failed_payments"
    assert len(d["candidates"]) == 10
    # sorted by amount desc; largest first
    amounts = [c["amount_paise"] for c in d["candidates"]]
    assert amounts == sorted(amounts, reverse=True)
    assert all(c["payment_id"] for c in d["candidates"])


def test_get_customer_history(tools, agent_seed):
    cid = agent_seed["customer"].id
    result = tools.call("get_customer_history", {"customer_id": cid})
    d = result.data
    assert d["customer_id"] == cid
    assert d["opted_out"] is False
    assert d["total_payments"] > 0
    assert cid in result.evidence_ids
    with pytest.raises(ToolError):
        tools.call("get_customer_history", {"customer_id": "cus_missing"})


# -- policy preview (read-only) ---------------------------------------------------


def test_propose_recovery_strategy_refund_is_blocked_and_creates_nothing(tools, agent_seed, db_session):
    payment = agent_seed["top_failed"]
    result = tools.call(
        "propose_recovery_strategy",
        {"action_type": "refund", "payment_id": payment.id, "confidence": 0.99},
    )
    policy = result.data["policy"]
    assert policy["outcome"] == PolicyOutcome.BLOCKED.value
    assert "allowlist" in policy["rules_matched"]
    assert "never_auto_execute.refund" in policy["rules_matched"]
    assert result.data["executed"] is False
    # a preview never creates a recovery action
    n_actions = db_session.scalar(sa.select(sa.func.count()).select_from(RecoveryAction))
    assert n_actions == 0


def test_propose_compliant_retry_is_allowed(tools, agent_seed):
    payment = agent_seed["top_failed"]
    result = tools.call(
        "propose_recovery_strategy",
        {"action_type": "retry_payment", "payment_id": payment.id, "confidence": 0.95},
    )
    assert result.data["policy"]["outcome"] == PolicyOutcome.ALLOWED.value
    # amount copied from the original payment (INR 1,100.00 -> under the ceiling)
    assert result.data["amount_paise"] == payment.amount_paise


def test_propose_safe_action_needs_no_target(tools):
    result = tools.call("propose_recovery_strategy", {"action_type": "escalate_human"})
    assert result.data["policy"]["outcome"] == PolicyOutcome.ALLOWED.value
    assert result.data["amount_paise"] == 0


# -- mutation path (policy-gated, never executes) ---------------------------------


def test_request_payment_link_creates_proposed_row_with_original_amount(
    tools, agent_seed, db_session
):
    payment = agent_seed["top_failed"]
    result = tools.call(
        "request_payment_link",
        {"payment_id": payment.id, "confidence": 0.5, "note": "test"},
    )
    d = result.data
    assert d["amount_paise"] == payment.amount_paise  # never AI-invented
    assert d["currency"] == "INR"
    assert d["executed"] is False
    # confidence 0.5 < 0.85 floor -> must wait for a human
    assert d["policy"]["outcome"] == PolicyOutcome.REQUIRES_APPROVAL.value
    assert d["status"] == RecoveryStatus.PENDING_APPROVAL.value

    action = db_session.get(RecoveryAction, d["action_id"])
    assert action is not None
    assert action.amount_paise == payment.amount_paise
    assert action.actor == "agent:investigator"
    assert action.policy_decision_id is not None
    assert action.gateway_request_id is None  # never sent to a gateway
    assert action.gateway_response is None
    # the policy decision was persisted and points at the action
    record = db_session.get(PolicyDecisionRecord, action.policy_decision_id)
    assert record.action_id == action.id
    # and the request itself is audited
    entry = db_session.scalars(
        sa.select(AuditLog).where(
            AuditLog.entity_type == "recovery_action", AuditLog.entity_id == action.id
        )
    ).first()
    assert entry is not None and entry.action == "agent.action_requested"


def test_request_recovery_execution_refund_is_blocked_with_no_execution_path(
    tools, agent_seed, db_session
):
    payment = agent_seed["top_failed"]
    result = tools.call(
        "request_recovery_execution",
        {"action_type": "refund", "payment_id": payment.id, "confidence": 0.99},
    )
    d = result.data
    assert d["policy"]["outcome"] == PolicyOutcome.BLOCKED.value
    assert d["status"] == RecoveryStatus.REJECTED.value
    assert d["executed"] is False
    action = db_session.get(RecoveryAction, d["action_id"])
    assert action.status == RecoveryStatus.REJECTED
    assert action.gateway_request_id is None  # proof nothing reached a gateway


def test_request_tools_reject_unknown_targets(tools):
    with pytest.raises(ToolError):
        tools.call("request_payment_link", {"payment_id": "pay_missing"})
    with pytest.raises(ToolError):
        tools.call("request_recovery_execution", {"action_type": "not_a_real_action"})
    with pytest.raises(ToolError):
        tools.call("request_payment_link", {})  # no target at all


def test_high_confidence_small_retry_auto_allows(tools, agent_seed):
    payment = agent_seed["top_failed"]
    result = tools.call(
        "request_recovery_execution",
        {"action_type": "retry_payment", "payment_id": payment.id, "confidence": 0.95},
    )
    d = result.data
    assert d["policy"]["outcome"] == PolicyOutcome.ALLOWED.value
    assert d["status"] == RecoveryStatus.POLICY_EVALUATED.value
    assert d["executed"] is False  # the agent still does not execute


# -- audit trail shape (invariant 12: structured from/to on the tool row) -------


def _request_audit_entry(db_session, action_id: str) -> AuditLog:
    return db_session.scalars(
        sa.select(AuditLog).where(
            AuditLog.entity_type == "recovery_action",
            AuditLog.entity_id == action_id,
            AuditLog.action == "agent.action_requested",
        )
    ).one()


def test_request_tool_audit_row_carries_structured_from_to_status(
    tools, agent_seed, db_session
):
    """The single agent.action_requested row covers creation (None) AND the
    gate's verdict, so the from/to audit chain starts at this row
    (docs/payment-invariants.md invariant 12 note)."""
    payment = agent_seed["top_failed"]
    auto = tools.call(
        "request_payment_link", {"payment_id": payment.id, "confidence": 0.99}
    )
    entry = _request_audit_entry(db_session, auto.data["action_id"])
    assert entry.details["from_status"] is None  # the row's creation
    assert entry.details["to_status"] == RecoveryStatus.POLICY_EVALUATED.value
    assert entry.details["policy_outcome"] == PolicyOutcome.ALLOWED.value

    gated = tools.call(
        "request_payment_link",
        {"payment_id": payment.id, "confidence": 0.5},
    )
    entry = _request_audit_entry(db_session, gated.data["action_id"])
    assert entry.details["from_status"] is None
    assert entry.details["to_status"] == RecoveryStatus.PENDING_APPROVAL.value
    assert entry.details["policy_outcome"] == PolicyOutcome.REQUIRES_APPROVAL.value


def test_request_tool_audit_from_to_matches_the_blocked_status(
    tools, agent_seed, db_session
):
    payment = agent_seed["top_failed"]
    result = tools.call(
        "request_recovery_execution",
        {"action_type": "refund", "payment_id": payment.id, "confidence": 0.99},
    )
    entry = _request_audit_entry(db_session, result.data["action_id"])
    assert entry.details["from_status"] is None
    assert entry.details["to_status"] == RecoveryStatus.REJECTED.value
    assert entry.details["policy_outcome"] == PolicyOutcome.BLOCKED.value
