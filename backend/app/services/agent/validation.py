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

_STRIPPED_PLACEHOLDER = "[unverified figure removed]"

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
) -> ValidationResult:
    """Schema-validate an LLM draft, then apply the hallucination guard.

    Schema/parse problems land in ``errors`` (caller may retry). Unverifiable
    numeric claims and bogus evidence citations are stripped into
    ``stripped_claims`` and degrade the report — they are not retried, because
    the correct behavior is to continue with the lie removed, flagged.
    """
    result = ValidationResult(draft=None)
    try:
        draft = LlmDraft.model_validate(payload)
    except ValidationError as exc:
        result.errors.append(f"schema validation failed: {exc.error_count()} error(s): {exc}")
        return result

    stripped: list[dict[str, Any]] = []

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
        sanitize_text(u, f"uncertainties[{i}]") for i, u in enumerate(draft.uncertainties)
    ]

    # -- observed facts: tool must be whitelisted AND actually called this run;
    #    evidence ids must be ones the tools actually returned; every number in
    #    the fact's data payload must match a tool number.
    kept_facts: list[LlmFact] = []
    for i, fact in enumerate(draft.observed_facts):
        loc = f"observed_facts[{i}]"
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

    if stripped:
        result.degraded_reasons.append(f"hallucination guard stripped {len(stripped)} unverifiable claim(s)")
    result.draft = draft
    result.stripped_claims = stripped
    return result


__all__ = [
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
    "extract_json",
    "validate_llm_payload",
]
