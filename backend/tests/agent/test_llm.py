"""LlmReasoner tests: bounded tool loop, whitelist enforcement, strict output
validation, hallucination guard, and heuristic fallback. All offline — the
transport is a fake chat_fn returning OpenAI-shaped responses."""

import json

import pytest

from app.ports import EvidenceBundle
from app.services.agent.reasoners import HeuristicReasoner, LlmReasoner
from app.services.agent.report import InvestigationOutput
from app.services.agent.tools import AgentTools
from app.services.agent.validation import extract_json, validate_llm_payload
from app.services.diagnosis.service import DiagnosisService

READ_TOOLS = [
    "get_incident",
    "get_payment_stats",
    "get_failure_distribution",
    "get_revenue_at_risk",
    "get_recovery_candidates",
]


def _tool_call_response(calls):
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
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                        for i, (name, args) in enumerate(calls)
                    ],
                }
            }
        ],
        "usage": {"total_tokens": 120},
    }


def _content_response(text):
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"total_tokens": 40},
    }


def _tool_results_from(messages):
    """Parse the tool results the conversation has collected so far."""
    out = []
    for m in messages:
        if m.get("role") == "tool":
            out.append(json.loads(m["content"]))
    return out


class FakeLlm:
    """Scriptable fake transport. `responder(messages, tools) -> response`."""

    def __init__(self, responder):
        self.responder = responder
        self.conversations: list[list[dict]] = []

    def __call__(self, messages, specs):
        self.conversations.append([dict(m) for m in messages])
        return self.responder(messages, specs)


def _valid_draft(tool_results):
    """A well-formed draft that only cites numbers the tools produced."""
    stats = next(r for r in tool_results if r["name"] == "get_payment_stats")
    revenue = next(r for r in tool_results if r["name"] == "get_revenue_at_risk")
    candidates = next(r for r in tool_results if r["name"] == "get_recovery_candidates")
    loss_point = revenue["data"]["observed_loss"]["point_paise"]
    target = candidates["data"]["candidates"][0]
    facts = []
    for r in tool_results:
        facts.append(
            {
                "statement": f"Fact from {r['name']}",
                "tool": r["name"],
                "evidence_ids": r["evidence_ids"][:2],
                "data": {},
            }
        )
    facts[1]["data"] = {"window_failed": stats["data"]["window"]["failed"]}
    what = f"Failures spiked; counterfactual observed loss is {loss_point} paise."
    return {
        "what_happened": what,
        "observed_facts": facts,
        "ai_inferences": [
            {
                "statement": "Bank-side technical errors dominate the window.",
                "label": "failure_nature",
                "confidence": 0.7,
                "supporting_fact_ids": ["f3"],
            }
        ],
        "alternative_hypotheses": [{"cause": "gateway_degradation", "confidence": 0.2}],
        "recommended_next_step": {
            "action_type": "create_payment_link",
            "rationale": "Give the customer a fresh link for the largest failed payment.",
            "payment_id": target["payment_id"],
        },
        "uncertainties": ["baseline sample is thin"],
        "confidence": 0.8,
    }


def _good_responder(messages, specs):
    if not _tool_results_from(messages):
        return _tool_call_response([(name, {}) for name in READ_TOOLS])
    return _content_response(json.dumps(_valid_draft(_tool_results_from(messages))))


@pytest.fixture()
def ctx(db_session, agent_seed):
    incident = agent_seed["incident"]
    DiagnosisService(db_session, artifacts_dir="/nonexistent-agent-test").classify(incident.id)
    tools = AgentTools(db_session, incident_id=incident.id)
    diagnosis = tools.get_incident().data["diagnosis"]
    bundle = EvidenceBundle(
        incident_id=incident.id,
        metric=incident.metric,
        window_start=incident.window_start,
        window_end=incident.window_end,
        context={"diagnosis": diagnosis},
    )
    return {"incident": incident, "tools": tools, "bundle": bundle, "seed": agent_seed}


def _reasoner(tools, fake):
    return LlmReasoner(tools, api_key="test-key", model="test-model", chat_fn=fake)


def _structured(report) -> InvestigationOutput:
    return InvestigationOutput.model_validate(report.raw["structured"])


# -- happy path -----------------------------------------------------------------


def test_llm_happy_path_produces_validated_report(ctx):
    fake = FakeLlm(_good_responder)
    report = _reasoner(ctx["tools"], fake).investigate(ctx["bundle"])
    out = _structured(report)
    assert out.reasoner == "llm"
    assert out.generated_by == "test-model"
    assert not out.degraded
    assert out.stripped_claims == []
    assert len(out.observed_facts) == len(READ_TOOLS)
    # the loss figure in the summary is the tool's number
    rev = next(c for c in ctx["tools"].calls if c.name == "get_revenue_at_risk")
    assert str(rev.data["observed_loss"]["point_paise"]) in out.what_happened
    # the system attached the REAL policy outcome to the recommended action
    assert out.recommended_next_step is not None
    assert out.recommended_next_step.policy_preview is not None
    assert out.recommended_next_step.policy_preview.outcome == "REQUIRES_APPROVAL"
    assert out.recommended_next_step.amount_paise == ctx["seed"]["top_failed"].amount_paise
    assert report.raw["tokens_used"] == 160  # 120 tool turn + 40 final turn


# -- malformed output -> fallback ----------------------------------------------


def test_malformed_llm_json_rejected_then_falls_back(ctx):
    fake = FakeLlm(lambda messages, specs: _content_response("this is not json at all"))
    report = _reasoner(ctx["tools"], fake).investigate(ctx["bundle"])
    assert report.raw["llm_fallback"] is True
    assert "unparseable" in report.raw["llm_error"]
    out = _structured(report)
    assert out.degraded is True
    assert any("fell back" in r for r in out.degraded_reasons)
    assert "fallback: heuristic" in out.generated_by
    # the fallback report is the deterministic heuristic one
    tools2 = AgentTools(ctx["tools"]._session, incident_id=ctx["incident"].id)
    heuristic = HeuristicReasoner(tools2).investigate(ctx["bundle"])
    assert out.what_happened == heuristic.raw["structured"]["what_happened"]
    # both attempts were made
    assert len(fake.conversations) == 2


def test_schema_invalid_llm_json_rejected(ctx):
    bad = json.dumps({"what_happened": 42, "confidence": "high"})  # wrong types
    fake = FakeLlm(lambda messages, specs: _content_response(bad))
    report = _reasoner(ctx["tools"], fake).investigate(ctx["bundle"])
    assert report.raw["llm_fallback"] is True
    assert "schema validation failed" in report.raw["llm_error"]


# -- hallucination guard ----------------------------------------------------------


def test_fake_financial_claim_is_stripped_and_flagged(ctx):
    def responder(messages, specs):
        if not _tool_results_from(messages):
            return _tool_call_response([(name, {}) for name in READ_TOOLS])
        draft = _valid_draft(_tool_results_from(messages))
        draft["what_happened"] += " Total exposure is Rs 999 crore."
        draft["observed_facts"].append(
            {
                "statement": "Invented loss figure.",
                "tool": "get_revenue_at_risk",
                "evidence_ids": [ctx["incident"].id],
                "data": {"loss_paise": 99_900_000_000_000},  # Rs 999cr — invented
            }
        )
        return _content_response(json.dumps(draft))

    fake = FakeLlm(responder)
    report = _reasoner(ctx["tools"], fake).investigate(ctx["bundle"])
    out = _structured(report)
    assert out.degraded is True
    assert len(out.stripped_claims) == 2  # the text claim + the invented fact
    assert "999 crore" not in out.what_happened
    assert "[unverified figure removed]" in out.what_happened
    assert all(
        "99_900_000_000_000" not in json.dumps(f.data) for f in out.observed_facts
    )
    assert any("hallucination guard" in r for r in out.degraded_reasons)


def test_fact_citing_unknown_evidence_is_stripped(ctx):
    def responder(messages, specs):
        if not _tool_results_from(messages):
            return _tool_call_response([(name, {}) for name in READ_TOOLS])
        draft = _valid_draft(_tool_results_from(messages))
        draft["observed_facts"].append(
            {
                "statement": "Payment pay_deadbeef failed.",
                "tool": "get_payment_stats",
                "evidence_ids": ["pay_deadbeef"],  # never returned by any tool
                "data": {},
            }
        )
        return _content_response(json.dumps(draft))

    report = _reasoner(ctx["tools"], FakeLlm(responder)).investigate(ctx["bundle"])
    out = _structured(report)
    assert out.degraded is True
    assert any("unknown evidence ids" in c["reason"] for c in out.stripped_claims)
    assert all("pay_deadbeef" not in f.statement for f in out.observed_facts)


# -- tool restriction ---------------------------------------------------------------


def test_llm_cannot_invoke_non_whitelisted_tools(ctx):
    calls_seen = []

    def responder(messages, specs):
        if not _tool_results_from(messages):
            calls_seen.append("rogue")
            return _tool_call_response([("execute_refund_now", {"amount": 1})])
        calls_seen.append("rogue-again")
        return _tool_call_response([("delete_everything", {})])

    fake = FakeLlm(responder)
    report = _reasoner(ctx["tools"], fake).investigate(ctx["bundle"])
    # after two violations per attempt the run aborts and falls back
    assert report.raw["llm_fallback"] is True
    assert "non-whitelisted" in report.raw["llm_error"]
    # the model was told the call is refused, with the allowed list
    tool_messages = [
        json.loads(m["content"])
        for convo in fake.conversations
        for m in convo
        if m.get("role") == "tool"
    ]
    assert any("not on the agent whitelist" in m.get("error", "") for m in tool_messages)
    # and no recovery action was ever created
    import sqlalchemy as sa

    from app.models import RecoveryAction

    n = ctx["tools"]._session.scalar(sa.select(sa.func.count()).select_from(RecoveryAction))
    assert n == 0


# -- validation unit tests ---------------------------------------------------------


def test_extract_json_tolerates_fences_and_prose():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('here you go: {"a": 2} hope that helps') == {"a": 2}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_guard_money_units_convert_to_paise():
    # Rs 999 crore = 999 * 1e7 rupees = 9.99e11 paise — must not match tools.
    result = validate_llm_payload(
        {
            "what_happened": "Loss is Rs 999 crore.",
            "confidence": 0.5,
            "observed_facts": [],
        },
        whitelisted_tools={"get_incident"},
        tools_called={"get_incident"},
        known_evidence_ids=set(),
        tool_numbers={100.0, 200.0},
    )
    assert result.draft is not None
    assert result.stripped_claims
    assert "[unverified figure removed]" in result.draft.what_happened

    # a claim that DOES match a tool number survives
    ok = validate_llm_payload(
        {"what_happened": "Loss is Rs 1.00 (100 paise).", "confidence": 0.5},
        whitelisted_tools=set(),
        tools_called=set(),
        known_evidence_ids=set(),
        tool_numbers={100.0},
    )
    assert ok.stripped_claims == []
    assert "100 paise" in ok.draft.what_happened
