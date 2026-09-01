"""Ranked multi-candidate proposals (heuristic-1.2): the heuristic reasoner
ranks the top-N eligible recovery candidates, policy-previews each, and keeps
the single headline recommendation backward-compatible
(recommended_next_step == recommended_candidates[0])."""

from app.ports import EvidenceBundle
from app.services.agent.reasoners import HeuristicReasoner
from app.services.agent.report import (
    RANKED_CANDIDATE_LIMIT,
    InvestigationOutput,
)
from app.services.agent.tools import AgentTools
from app.services.diagnosis.service import DiagnosisService


def _bundle(incident, diagnosis=None):
    return EvidenceBundle(
        incident_id=incident.id,
        metric=incident.metric,
        window_start=incident.window_start,
        window_end=incident.window_end,
        events=[],
        context={"diagnosis": diagnosis},
    )


def _structured(report) -> InvestigationOutput:
    return InvestigationOutput.model_validate(report.raw["structured"])


def _investigate(db_session, incident):
    DiagnosisService(db_session, artifacts_dir="/nonexistent-agent-test").classify(incident.id)
    tools = AgentTools(db_session, incident_id=incident.id)
    diagnosis = tools.get_incident().data["diagnosis"]
    return HeuristicReasoner(tools).investigate(_bundle(incident, diagnosis))


def test_heuristic_ranks_top_n_candidates(db_session, agent_seed):
    out = _structured(_investigate(db_session, agent_seed["incident"]))

    candidates = out.recommended_candidates
    assert len(candidates) == RANKED_CANDIDATE_LIMIT
    assert [c.rank for c in candidates] == [1, 2, 3]
    # ordered by descending exposure
    amounts = [c.amount_paise for c in candidates]
    assert amounts == sorted(amounts, reverse=True)
    # every ranked candidate is policy-previewed by the system and records the
    # exact confidence passed to the gate (machine-reproducible)
    for c in candidates:
        assert c.policy_preview is not None
        assert c.policy_preview.outcome in {"ALLOWED", "BLOCKED", "REQUIRES_APPROVAL"}
        assert c.confidence is not None
        assert c.action_type == "retry_payment"  # dominant class: soft_decline
        assert c.policy_preview.outcome in c.rationale


def test_rank1_matches_the_headline_recommendation(db_session, agent_seed):
    """Backward compatibility: recommended_next_step is unchanged and equals
    the rank-1 candidate; recommended_actions still holds only the headline."""
    out = _structured(_investigate(db_session, agent_seed["incident"]))

    assert out.recommended_next_step is not None
    rank1 = out.recommended_candidates[0]
    assert rank1.model_dump(exclude={"rank"}) == out.recommended_next_step.model_dump(
        exclude={"rank"}
    )
    assert [a.model_dump() for a in out.recommended_actions] == [
        out.recommended_next_step.model_dump()
    ]


def test_alternates_carry_no_invented_expected_recovery(db_session, agent_seed):
    """The revenue engine prices strategies incident-wide; ranked alternates
    must not fabricate a per-candidate split."""
    out = _structured(_investigate(db_session, agent_seed["incident"]))
    for alt in out.recommended_candidates[1:]:
        assert alt.expected_recovery_paise is None
        assert f"rank {alt.rank} alternate" in alt.rationale


def test_ranked_candidates_are_deterministic(db_session, agent_seed):
    incident = agent_seed["incident"]
    DiagnosisService(db_session, artifacts_dir="/nonexistent-agent-test").classify(incident.id)
    tools = AgentTools(db_session, incident_id=incident.id)
    diagnosis = tools.get_incident().data["diagnosis"]
    bundle = _bundle(incident, diagnosis)
    r1 = HeuristicReasoner(AgentTools(db_session, incident_id=incident.id)).investigate(bundle)
    r2 = HeuristicReasoner(AgentTools(db_session, incident_id=incident.id)).investigate(bundle)
    assert r1.raw["structured"] == r2.raw["structured"]


def test_escalated_report_ranks_only_the_escalation(db_session, empty_incident):
    tools = AgentTools(db_session, incident_id=empty_incident.id)
    report = HeuristicReasoner(tools).investigate(_bundle(empty_incident))
    out = _structured(report)
    assert out.escalated is True
    assert len(out.recommended_candidates) == 1
    assert out.recommended_candidates[0].rank == 1
    assert out.recommended_candidates[0].action_type == "escalate_human"


def test_llm_report_mirrors_headline_into_ranked_candidates(db_session, agent_seed):
    """The LLM path proposes a single candidate; the ranked list mirrors what
    was actually previewed (headline at rank 1)."""
    import json

    from app.services.agent.reasoners import LlmReasoner

    read_tools = [
        "get_incident",
        "get_payment_stats",
        "get_failure_distribution",
        "get_revenue_at_risk",
        "get_recovery_candidates",
    ]

    def responder(messages, specs):
        tool_results = [
            json.loads(m["content"]) for m in messages if m.get("role") == "tool"
        ]
        if not any(r.get("name") == "get_recovery_candidates" for r in tool_results):
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": f"call_{i}",
                                    "type": "function",
                                    "function": {"name": n, "arguments": "{}"},
                                }
                                for i, n in enumerate(read_tools)
                            ],
                        }
                    }
                ],
                "usage": {"total_tokens": 10},
            }
        candidates = next(
            r for r in tool_results if r["name"] == "get_recovery_candidates"
        )["data"]["candidates"]
        target = max(candidates, key=lambda c: c["amount_paise"])
        draft = {
            "what_happened": "Failures moved in the incident window.",
            "observed_facts": [
                {
                    "statement": f"Reading from {r['name']}.",
                    "tool": r["name"],
                    "evidence_ids": (r.get("evidence_ids") or [])[:1],
                    "data": {},
                }
                for r in tool_results
            ],
            "ai_inferences": [],
            "alternative_hypotheses": [],
            "recommended_next_step": {
                "action_type": "retry_payment",
                "rationale": "bounded retry of the largest failed payment",
                "payment_id": target["payment_id"],
            },
            "uncertainties": [],
            "confidence": 0.7,
        }
        return {
            "choices": [{"message": {"role": "assistant", "content": json.dumps(draft)}}],
            "usage": {"total_tokens": 5},
        }

    incident = agent_seed["incident"]
    DiagnosisService(db_session, artifacts_dir="/nonexistent-agent-test").classify(incident.id)
    tools = AgentTools(db_session, incident_id=incident.id)
    diagnosis = tools.get_incident().data["diagnosis"]
    reasoner = LlmReasoner(tools, api_key="k", model="m", chat_fn=responder)
    out = _structured(reasoner.investigate(_bundle(incident, diagnosis)))
    assert out.recommended_next_step is not None
    assert len(out.recommended_candidates) == len(out.recommended_actions)
    assert [c.rank for c in out.recommended_candidates] == list(
        range(1, len(out.recommended_candidates) + 1)
    )
    assert out.recommended_candidates[0].model_dump(
        exclude={"rank"}
    ) == out.recommended_next_step.model_dump(exclude={"rank"})
