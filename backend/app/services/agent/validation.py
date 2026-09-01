"""LLM output validation — JSON extraction, schema checks, and the
hallucination guard.

The guard enforces the trust boundary: every numeric FINANCIAL claim in the
LLM's draft must exactly match a number that came out of a tool result during
the same run. Anything unverifiable is stripped and flagged (the report is
marked degraded); the LLM never gets the benefit of the doubt on money.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.services.agent.report import AUTO_EXECUTE_CONFIDENCE_FLOOR

# ---------------------------------------------------------------------------
# LLM draft schema (what the model is allowed to produce). Lenient on extra
# keys — the system rebuilds a strict InvestigationOutput from the draft.
# ---------------------------------------------------------------------------


class LlmFact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    statement: str
    tool: str
    evidence_ids: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class LlmInference(BaseModel):
    model_config = ConfigDict(extra="ignore")

    statement: str
    label: str = "inference"
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_fact_ids: list[str] = Field(default_factory=list)


class LlmHypothesis(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cause: str
    confidence: float = Field(ge=0.0, le=1.0)


class LlmNextStep(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action_type: str
    rationale: str
    payment_id: str | None = None
    opportunity_id: str | None = None


class LlmDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    what_happened: str
    observed_facts: list[LlmFact] = Field(default_factory=list)
    ai_inferences: list[LlmInference] = Field(default_factory=list)
    alternative_hypotheses: list[LlmHypothesis] = Field(default_factory=list)
    recommended_next_step: LlmNextStep | None = None
    uncertainties: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# hallucination guard primitives
# ---------------------------------------------------------------------------

#: A JSON key that marks a numeric value as a financial claim.
MONEY_KEY_RE = re.compile(r"paise|amount|revenue|loss|recover|refund|price|fee|inr|rupee", re.I)

#: Money written into free text: "₹1,500", "Rs 999 crore", "INR 3.5 lakh",
#: "Rs. 250", "999cr", "12 paise".
MONEY_TEXT_RE = re.compile(
    r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)\s*(crore|cr|lakh|lakhs|l|k|paise)?",
    re.IGNORECASE,
)

#: Bare integers with 7+ digits inside free text are treated as paise claims.
BARE_LARGE_INT_RE = re.compile(r"\b(\d{7,})\b")

#: Execution-advocacy language. The reasoner is advisory: it may recommend an
#: action, but it may not tell the operator to execute it, skip approval, or
#: bypass the policy gate. Such language is excised like invented money. The
#: "auto-execute" alternative requires an imperative object so descriptive
#: text about the policy floor ("below the auto-execute floor of 0.85") is
#: NOT flagged.
ADVOCACY_RE = re.compile(
    r"auto-?execut\w*\s+(?:this|it|the|these|those|now|immediately|all|every|without|with\s+no)"
    r"|execute immediately|execute (?:this|it) now"
    r"|without (?:any )?(?:human )?approval|no approval (?:is )?(?:needed|required)"
    r"|skip(?:ping)? (?:the )?approval|bypass(?:ing)? (?:the )?(?:policy|gate|approval)",
    re.IGNORECASE,
)

_STRIPPED_PLACEHOLDER = "[unverified figure removed]"
_ADVOCACY_PLACEHOLDER = "[execution advocacy removed — the policy engine decides execution]"

_UNIT_TO_PAISE = {
    None: 100,  # bare "Rs X" is rupees
    "paise": 1,
    "k": 100_000,
    "l": 10_000_000,
    "lakh": 10_000_000,
    "lakhs": 10_000_000,
    "cr": 1_000_000_000,
    "crore": 1_000_000_000,
}


def collect_numbers(obj: Any) -> set[float]:
    """Every numeric value inside a JSON-like structure (bools excluded)."""
    out: set[float] = set()

    def walk(v: Any) -> None:
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            out.add(float(v))
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    walk(obj)
    return out


def collect_target_ids(obj: Any) -> set[str]:
    """Every ``payment_id``/``opportunity_id`` value inside a JSON-like
    structure — the action targets a tool actually surfaced this run."""
    out: set[str] = set()

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if k in ("payment_id", "opportunity_id") and isinstance(x, str) and x:
                    out.add(x)
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    walk(obj)
    return out


def _text_amounts_paise(text: str) -> list[tuple[str, float]]:
    """(matched span, paise value) for every money phrase in ``text``."""
    found: list[tuple[str, float]] = []
    for m in MONEY_TEXT_RE.finditer(text):
        num = float(m.group(1).replace(",", ""))
        unit = m.group(2).lower() if m.group(2) else None
        found.append((m.group(0), num * _UNIT_TO_PAISE[unit]))
    for m in BARE_LARGE_INT_RE.finditer(text):
        # skip spans already covered by an explicit money phrase
        if any(m.start() >= s and m.end() <= e for s, e in (mm.span() for mm in MONEY_TEXT_RE.finditer(text))):
            continue
        found.append((m.group(0), float(m.group(1))))
    return found


def _json_money_violations(obj: Any, numbers: set[float], path: str, *, strict: bool = False) -> list[dict[str, Any]]:
    """Numeric values that do not exactly match a tool-result number.

    With ``strict=False`` only values under money-ish keys are checked; with
    ``strict=True`` every number in the structure is checked (used for a
    fact's ``data`` payload, which is supposed to mirror tool output)."""
    violations: list[dict[str, Any]] = []

    def walk(v: Any, p: str, on: bool) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, f"{p}.{k}", on or bool(MONEY_KEY_RE.search(str(k))))
        elif isinstance(v, (list, tuple)):
            for i, x in enumerate(v):
                walk(x, f"{p}[{i}]", on)
        elif isinstance(v, bool) or v is None:
            return
        elif isinstance(v, (int, float)) and on:
            if float(v) not in numbers:
                violations.append(
                    {"location": p, "value": v, "reason": "numeric claim not present in any tool result"}
                )

    walk(obj, path, strict)
    return violations


@dataclass
class ValidationResult:
    draft: LlmDraft | None
    errors: list[str] = field(default_factory=list)
    stripped_claims: list[dict[str, Any]] = field(default_factory=list)
    degraded_reasons: list[str] = field(default_factory=list)


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of an LLM message (fences tolerated)."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty LLM response")
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    else:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object found in LLM response")
        candidate = candidate[start : end + 1]
    parsed = json.loads(candidate)
    if not isinstance(parsed, dict):
        raise ValueError("LLM response JSON is not an object")
    return parsed


def validate_llm_payload(
    payload: dict[str, Any],
    *,
    whitelisted_tools: set[str],
    tools_called: set[str],
    known_evidence_ids: set[str],
    tool_numbers: set[float],
    known_target_ids: set[str] | None = None,
) -> ValidationResult:
    """Schema-validate an LLM draft, then apply the hallucination guard.

    Schema/parse problems land in ``errors`` (caller may retry). Unverifiable
    numeric claims and bogus evidence citations are stripped into
    ``stripped_claims`` and degrade the report — they are not retried, because
    the correct behavior is to continue with the lie removed, flagged.

    Beyond the phrase-level guards, two STRUCTURED checks run on the draft
    (they do not depend on wording, so paraphrasing cannot evade them):

    - proposal grounding: a recommended action may only target a
      ``payment_id``/``opportunity_id`` a tool surfaced this run
      (``known_target_ids``); anything else is stripped like a fake citation.
    - confidence vs evidence coverage: self-reported confidence at or above
      the auto-execute floor while some (or all) of the cited evidence failed
      validation is flagged as a degraded reason — the numeric cap itself is
      applied by the caller at the evidence-calibrated ceiling.
    """
    result = ValidationResult(draft=None)
    try:
        draft = LlmDraft.model_validate(payload)
    except ValidationError as exc:
        result.errors.append(f"schema validation failed: {exc.error_count()} error(s): {exc}")
        return result

    stripped: list[dict[str, Any]] = []

    # -- execution-advocacy language: excise in place (advisory-only contract).
    def sanitize_advocacy(text: str, location: str) -> str:
        def _sub(m: re.Match) -> str:
            stripped.append(
                {
                    "location": location,
                    "excerpt": m.group(0),
                    "reason": "execution-advocacy language (the reasoner is advisory; the policy engine decides execution)",
                }
            )
            return _ADVOCACY_PLACEHOLDER

        return ADVOCACY_RE.sub(_sub, text)

    draft.what_happened = sanitize_advocacy(draft.what_happened, "what_happened")

    # -- free-text money claims in the summary/uncertainties: excise in place.
    def sanitize_text(text: str, location: str) -> str:
        out = text
        for span, paise in _text_amounts_paise(text):
            if paise not in tool_numbers:
                stripped.append(
                    {
                        "location": location,
                        "excerpt": span,
                        "reason": "unverifiable financial claim (not present in any tool result)",
                    }
                )
                out = out.replace(span, _STRIPPED_PLACEHOLDER)
        return out

    draft.what_happened = sanitize_text(draft.what_happened, "what_happened")
    draft.uncertainties = [
        sanitize_text(
            sanitize_advocacy(u, f"uncertainties[{i}]"), f"uncertainties[{i}]"
        )
        for i, u in enumerate(draft.uncertainties)
    ]
    draft.alternative_hypotheses = [
        h.model_copy(
            update={"cause": sanitize_advocacy(h.cause, f"alternative_hypotheses[{i}].cause")}
        )
        for i, h in enumerate(draft.alternative_hypotheses)
    ]

    # -- observed facts: tool must be whitelisted AND actually called this run;
    #    evidence ids must be ones the tools actually returned; every number in
    #    the fact's data payload must match a tool number.
    kept_facts: list[LlmFact] = []
    for i, fact in enumerate(draft.observed_facts):
        loc = f"observed_facts[{i}]"
        fact.statement = sanitize_advocacy(fact.statement, f"{loc}.statement")
        if fact.tool not in whitelisted_tools:
            stripped.append({"location": loc, "excerpt": fact.tool, "reason": "tool is not on the whitelist"})
            continue
        if fact.tool not in tools_called:
            stripped.append(
                {"location": loc, "excerpt": fact.tool, "reason": "cites a tool that was never called in this run"}
            )
            continue
        unknown_ids = [e for e in fact.evidence_ids if e not in known_evidence_ids]
        if unknown_ids:
            stripped.append(
                {"location": loc, "excerpt": ",".join(unknown_ids), "reason": "cites unknown evidence ids"}
            )
            continue
        data_violations = _json_money_violations(fact.data, tool_numbers, f"{loc}.data", strict=True)
        if data_violations:
            stripped.extend(data_violations)
            continue
        text_hits = [(s, p) for s, p in _text_amounts_paise(fact.statement) if p not in tool_numbers]
        if text_hits:
            for span, _ in text_hits:
                stripped.append(
                    {"location": f"{loc}.statement", "excerpt": span, "reason": "unverifiable financial claim"}
                )
            continue
        kept_facts.append(fact)
    draft.observed_facts = kept_facts

    # -- inferences: same text guard; fact refs are validated by the caller
    #    after id assignment (refs to stripped facts are dropped there).
    kept_inferences: list[LlmInference] = []
    for i, inf in enumerate(draft.ai_inferences):
        loc = f"ai_inferences[{i}]"
        inf.statement = sanitize_advocacy(inf.statement, f"{loc}.statement")
        text_hits = [(s, p) for s, p in _text_amounts_paise(inf.statement) if p not in tool_numbers]
        if text_hits:
            for span, _ in text_hits:
                stripped.append(
                    {"location": f"{loc}.statement", "excerpt": span, "reason": "unverifiable financial claim"}
                )
            continue
        kept_inferences.append(inf)
    draft.ai_inferences = kept_inferences

    # -- recommended next step: guard the rationale text; the numeric payload
    #    (amount, policy outcome) is attached by the system afterwards.
    if draft.recommended_next_step is not None:
        step = draft.recommended_next_step
        step.rationale = sanitize_advocacy(step.rationale, "recommended_next_step.rationale")
        text_hits = [(s, p) for s, p in _text_amounts_paise(step.rationale) if p not in tool_numbers]
        if text_hits:
            for span, _ in text_hits:
                stripped.append(
                    {
                        "location": "recommended_next_step.rationale",
                        "excerpt": span,
                        "reason": "unverifiable financial claim",
                    }
                )
            draft.recommended_next_step = None

    # -- structured check 1: the proposal may only target an id a tool
    #    surfaced this run (candidate, sampled payment, request result).
    if draft.recommended_next_step is not None and known_target_ids is not None:
        step = draft.recommended_next_step
        cited = [t for t in (step.payment_id, step.opportunity_id) if t]
        unknown_targets = [t for t in cited if t not in known_target_ids]
        if unknown_targets:
            stripped.append(
                {
                    "location": "recommended_next_step",
                    "excerpt": ",".join(unknown_targets),
                    "reason": "recommended action targets an id no tool returned this run",
                }
            )
            draft.recommended_next_step = None

    # -- structured check 2: confidence at the auto-execute floor while cited
    #    evidence failed validation is wrong-but-confident by construction.
    n_cited_facts = len(payload.get("observed_facts") or [])
    if draft.confidence >= AUTO_EXECUTE_CONFIDENCE_FLOOR and (
        n_cited_facts == 0 or len(draft.observed_facts) < n_cited_facts
    ):
        result.degraded_reasons.append(
            f"model confidence {draft.confidence} meets the "
            f"{AUTO_EXECUTE_CONFIDENCE_FLOOR} auto-execute floor but only "
            f"{len(draft.observed_facts)}/{n_cited_facts} cited fact(s) survived "
            "validation — confidence exceeds evidence coverage"
        )

    if stripped:
        result.degraded_reasons.append(f"hallucination guard stripped {len(stripped)} unverifiable claim(s)")
    result.draft = draft
    result.stripped_claims = stripped
    return result


__all__ = [
    "ADVOCACY_RE",
    "BARE_LARGE_INT_RE",
    "MONEY_KEY_RE",
    "MONEY_TEXT_RE",
    "LlmDraft",
    "LlmFact",
    "LlmHypothesis",
    "LlmInference",
    "LlmNextStep",
    "ValidationResult",
    "collect_numbers",
    "collect_target_ids",
    "extract_json",
    "validate_llm_payload",
]
