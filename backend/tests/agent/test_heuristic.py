"""HeuristicReasoner tests: report structure, evidence citation, escalation,
and byte-for-byte determinism over the same DB state."""

from app.ports import EvidenceBundle
from app.services.agent.reasoners import HeuristicReasoner
from app.services.agent.report import InvestigationOutput
from app.services.agent.tools import AgentTools


def _bundle(incident, diagnosis=None, diagnosis_error=None):
    return EvidenceBundle(
        incident_id=incident.id,
        metric=incident.metric,
        window_start=incident.window_start,
        window_end=incident.window_end,
        events=[],
        context={"diagnosis": diagnosis, "diagnosis_error": diagnosis_error},
    )


def _structured(report) -> InvestigationOutput:
    return InvestigationOutput.model_validate(report.raw["structured"])


def _diagnosis_ctx(tools):
    return tools.get_incident().data["diagnosis"]


def test_heuristic_report_structure_and_citations(db_session, agent_seed):
    incident = agent_seed["incident"]
    tools = AgentTools(db_session, incident_id=incident.id)
    diagnosis = _diagnosis_ctx(tools)  # None here — no diagnosis row yet
    assert diagnosis is None
    # give the reasoner the same context the service would (diagnosis run)
    from app.services.diagnosis.service import DiagnosisService

    DiagnosisService(db_session, artifacts_dir="/nonexistent-agent-test").classify(incident.id)
    diagnosis = _diagnosis_ctx(tools)
    assert diagnosis is not None

    report = HeuristicReasoner(tools).investigate(_bundle(incident, diagnosis))
    out = _structured(report)

    assert out.generated_by == "heuristic"
    assert out.reasoner == "heuristic"
    assert out.what_happened
    assert len(out.observed_facts) >= 4
    for fact in out.observed_facts:
        assert fact.tool in tools.tool_names
        assert fact.evidence_ids, f"fact {fact.id} carries no evidence ids"
    fact_ids = {f.id for f in out.observed_facts}
    for inf in out.ai_inferences:
        assert 0.0 <= inf.confidence <= 1.0
        assert set(inf.supporting_fact_ids) <= fact_ids
    # root-cause inference present with diagnosis confidence
    root = next(i for i in out.ai_inferences if i.label == "root_cause")
    assert root.confidence == round(diagnosis["confidence"], 4)
    # alternatives come from the diagnosis top3 (minus the primary)
    assert [h.cause for h in out.alternative_hypotheses] == [
        t["label"] for t in diagnosis["top3"][1:4]
    ]
    # revenue implications copied from the tool
    assert out.revenue_implications is not None
    rev_call = next(c for c in tools.calls if c.name == "get_revenue_at_risk")
    assert (
        out.revenue_implications.observed_loss_point_paise
        == rev_call.data["observed_loss"]["point_paise"]
    )
    # recommended next step carries a REAL policy preview
    assert out.recommended_next_step is not None
    assert out.recommended_next_step.policy_preview is not None
    assert out.recommended_next_step.policy_preview.outcome in {
        "ALLOWED",
        "BLOCKED",
        "REQUIRES_APPROVAL",
    }
    assert out.recommended_next_step.amount_paise == agent_seed["top_failed"].amount_paise
    # bank_technical_error -> soft_decline -> retry_payment
    assert out.recommended_next_step.action_type == "retry_payment"
    assert not out.escalated
    assert set(out.tools_called) <= set(tools.tool_names)


def test_heuristic_escalates_when_evidence_insufficient(db_session, empty_incident):
    incident = empty_incident
    tools = AgentTools(db_session, incident_id=incident.id)
    report = HeuristicReasoner(tools).investigate(_bundle(incident, diagnosis=None))
    out = _structured(report)
    assert out.escalated is True
    assert any("no payments" in r for r in out.escalation_reasons)
    assert any("no diagnosis" in r for r in out.escalation_reasons)
    # the only recommendation is a (policy-allowed) human escalation
    assert out.recommended_next_step is not None
    assert out.recommended_next_step.action_type == "escalate_human"
    assert out.recommended_next_step.policy_preview.outcome == "ALLOWED"
    assert out.confidence < 0.5


def test_heuristic_low_confidence_without_diagnosis_escalates(db_session, agent_seed):
    """Plenty of evidence but no diagnosis -> confidence below threshold."""
    incident = agent_seed["incident"]
    tools = AgentTools(db_session, incident_id=incident.id)
    report = HeuristicReasoner(tools).investigate(_bundle(incident, diagnosis=None))
    out = _structured(report)
    # 0.30 base + 0.10 (>=10 failed) - 0.10 (revenue low confidence) = 0.30
    assert out.confidence < 0.5
    assert out.escalated is True
    assert any("no diagnosis available" in r for r in out.escalation_reasons)
    # escalation action is appended alongside any candidate recommendation
    assert any(a.action_type == "escalate_human" for a in out.recommended_actions)


def test_heuristic_is_deterministic(db_session, agent_seed):
    incident = agent_seed["incident"]
    from app.services.diagnosis.service import DiagnosisService

    DiagnosisService(db_session, artifacts_dir="/nonexistent-agent-test").classify(incident.id)

    tools1 = AgentTools(db_session, incident_id=incident.id)
    ctx1 = _diagnosis_ctx(tools1)
    r1 = HeuristicReasoner(tools1).investigate(_bundle(incident, ctx1))

    tools2 = AgentTools(db_session, incident_id=incident.id)
    ctx2 = _diagnosis_ctx(tools2)
    r2 = HeuristicReasoner(tools2).investigate(_bundle(incident, ctx2))

    assert r1.raw["structured"] == r2.raw["structured"]
    assert r1.summary == r2.summary
