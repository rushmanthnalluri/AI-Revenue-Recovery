"""Structured advocacy-guard checks (validation layer): the phrase-level
advocacy regex is now backed by two wording-independent structural checks.

1. proposal grounding — a recommended action may only target a
   payment_id/opportunity_id a tool surfaced this run; anything else is
   stripped like a fake evidence citation.
2. confidence vs evidence coverage — self-reported confidence at/above the
   0.85 auto-execute floor while cited evidence failed validation is flagged
   as a degraded reason (the numeric cap stays with the caller's
   evidence-calibrated ceiling).
"""

from app.services.agent.validation import (
    collect_target_ids,
    validate_llm_payload,
)

_WHITELIST = {"get_incident", "get_payment_stats"}
_CALLED = {"get_incident", "get_payment_stats"}
_EVIDENCE = {"inc_1", "pay_1"}
_NUMBERS = {100.0}


def _fact(eid: str = "inc_1") -> dict:
    return {
        "statement": "A grounded fact.",
        "tool": "get_incident",
        "evidence_ids": [eid],
        "data": {},
    }


def _validate(payload: dict, **overrides):
    kwargs = {
        "whitelisted_tools": set(_WHITELIST),
        "tools_called": set(_CALLED),
        "known_evidence_ids": set(_EVIDENCE),
        "tool_numbers": set(_NUMBERS),
        "known_target_ids": {"pay_1", "opp_1"},
    }
    kwargs.update(overrides)
    return validate_llm_payload(payload, **kwargs)


# -- collect_target_ids ---------------------------------------------------------


def test_collect_target_ids_walks_nested_payloads():
    data = {
        "candidates": [
            {"payment_id": "pay_a", "opportunity_id": None},
            {"payment_id": "pay_b", "opportunity_id": "opp_b", "amount_paise": 5},
        ],
        "policy": {"outcome": "ALLOWED"},
    }
    assert collect_target_ids(data) == {"pay_a", "pay_b", "opp_b"}
    assert collect_target_ids({}) == set()
    assert collect_target_ids({"payment_id": 42}) == set()  # non-strings ignored


# -- check 1: proposal grounding -------------------------------------------------


def test_proposal_citing_unknown_target_is_stripped():
    result = _validate(
        {
            "what_happened": "Failures spiked.",
            "observed_facts": [_fact()],
            "recommended_next_step": {
                "action_type": "retry_payment",
                "rationale": "retry the payment named in the evidence",
                "payment_id": "pay_deadbeef",  # no tool returned this id
            },
            "confidence": 0.7,
        }
    )
    assert result.draft is not None
    assert result.draft.recommended_next_step is None
    assert any(
        "targets an id no tool returned" in c["reason"] for c in result.stripped_claims
    )
    assert result.degraded_reasons  # stripping degrades the report


def test_proposal_with_grounded_target_survives():
    result = _validate(
        {
            "what_happened": "Failures spiked.",
            "observed_facts": [_fact()],
            "recommended_next_step": {
                "action_type": "retry_payment",
                "rationale": "bounded retry of the largest failed payment",
                "payment_id": "pay_1",
            },
            "confidence": 0.7,
        }
    )
    assert result.draft is not None
    assert result.draft.recommended_next_step is not None
    assert result.stripped_claims == []


def test_targetless_proposal_is_not_grounding_checked():
    """escalate_human / no_action carry no target — nothing to ground."""
    result = _validate(
        {
            "what_happened": "Evidence is thin.",
            "observed_facts": [_fact()],
            "recommended_next_step": {
                "action_type": "escalate_human",
                "rationale": "a human should review before any automation",
            },
            "confidence": 0.4,
        }
    )
    assert result.draft is not None
    assert result.draft.recommended_next_step is not None
    assert result.stripped_claims == []


# -- check 2: confidence vs evidence coverage ------------------------------------


def test_floor_confidence_with_stripped_evidence_is_flagged():
    result = _validate(
        {
            "what_happened": "Failures spiked.",
            "observed_facts": [_fact("inc_1"), _fact("pay_unknown")],  # 2nd is stripped
            "confidence": 0.95,
        }
    )
    assert result.draft is not None
    assert len(result.draft.observed_facts) == 1
    assert any("confidence exceeds evidence coverage" in r for r in result.degraded_reasons)


def test_floor_confidence_with_zero_cited_facts_is_flagged():
    result = _validate(
        {"what_happened": "Trust me.", "observed_facts": [], "confidence": 0.9}
    )
    assert result.draft is not None
    assert any("confidence exceeds evidence coverage" in r for r in result.degraded_reasons)


def test_floor_confidence_with_full_coverage_is_not_flagged():
    result = _validate(
        {
            "what_happened": "Failures spiked.",
            "observed_facts": [_fact("inc_1"), _fact("pay_1")],
            "confidence": 0.9,
        }
    )
    assert result.draft is not None
    assert len(result.draft.observed_facts) == 2
    assert not any(
        "confidence exceeds evidence coverage" in r for r in result.degraded_reasons
    )


def test_below_floor_confidence_is_not_coverage_flagged():
    result = _validate(
        {
            "what_happened": "Failures spiked.",
            "observed_facts": [_fact("inc_1"), _fact("pay_unknown")],
            "confidence": 0.8,
        }
    )
    assert result.draft is not None
    assert not any(
        "confidence exceeds evidence coverage" in r for r in result.degraded_reasons
    )
