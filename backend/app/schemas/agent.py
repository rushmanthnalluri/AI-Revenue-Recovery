"""Agent schemas — AI investigation of incidents (advisory only; the reasoner
never executes financial actions).

The investigation response cleanly separates the three things the frontend
renders differently:
- ``observed_facts``      — deterministic tool output with evidence ids
- ``ai_inferences``       — probabilistic claims with confidence + fact refs
- ``recommended_actions`` — proposals with a system-attached policy preview
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class InvestigateRequest(BaseModel):
    force_refresh: bool = False  # re-investigate even if a report exists


# ---------------------------------------------------------------------------
# structured report views (mirror app.services.agent.report)
# ---------------------------------------------------------------------------


class ObservedFactView(BaseModel):
    id: str
    statement: str
    tool: str
    evidence_ids: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class AiInferenceView(BaseModel):
    id: str
    statement: str
    label: str = "inference"
    confidence: float
    supporting_fact_ids: list[str] = Field(default_factory=list)


class AlternativeHypothesisView(BaseModel):
    rank: int
    cause: str
    confidence: float
    source: str = "diagnosis"


class PolicyOutcomePreview(BaseModel):
    outcome: str
    reasons: list[str] = Field(default_factory=list)
    rules_matched: list[str] = Field(default_factory=list)
    policy_version: str = "unknown"


class RecommendedActionView(BaseModel):
    action_type: str
    rationale: str
    amount_paise: int | None = None
    currency: str = "INR"
    confidence: float | None = None
    payment_id: str | None = None
    opportunity_id: str | None = None
    expected_recovery_paise: int | None = None
    policy_preview: PolicyOutcomePreview | None = None


class RevenueImplicationsView(BaseModel):
    currency: str = "INR"
    observed_loss_point_paise: int | None = None
    observed_loss_lower_paise: int = 0
    observed_loss_upper_paise: int = 0
    recoverable_point_paise: int | None = None
    recoverable_lower_paise: int = 0
    recoverable_upper_paise: int = 0
    expected_recovery_point_by_strategy: dict[str, int | None] = Field(default_factory=dict)
    actual_recovered_paise: int = 0
    recovered_actions_count: int = 0
    confidence: float = 0.0
    low_confidence: bool = True
    basis: str = ""


class HypothesisView(BaseModel):
    """Legacy flat hypothesis (kept for consumers of the scaffold contract);
    derived from ai_inferences."""

    title: str
    confidence: float
    supporting_evidence: list[str] = Field(default_factory=list)


class InvestigationReportView(BaseModel):
    id: str
    incident_id: str
    summary: str
    # clean separation for the UI
    observed_facts: list[ObservedFactView] = Field(default_factory=list)
    ai_inferences: list[AiInferenceView] = Field(default_factory=list)
    recommended_actions: list[RecommendedActionView] = Field(default_factory=list)
    recommended_next_step: RecommendedActionView | None = None
    # Ranked top-N candidate proposals ([0] is the headline, i.e. identical to
    # recommended_next_step); additive — recommended_actions/recommended_next_step
    # are unchanged.
    recommended_candidates: list[RecommendedActionView] = Field(default_factory=list)
    alternative_hypotheses: list[AlternativeHypothesisView] = Field(default_factory=list)
    revenue_implications: RevenueImplicationsView | None = None
    uncertainties: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    escalated: bool = False
    escalation_reasons: list[str] = Field(default_factory=list)
    degraded: bool = False
    degraded_reasons: list[str] = Field(default_factory=list)
    stripped_claims: list[dict[str, Any]] = Field(default_factory=list)
    tools_called: list[str] = Field(default_factory=list)
    reasoner: str = "heuristic"
    diagnosis: dict[str, Any] | None = None
    # legacy scaffold fields
    hypotheses: list[HypothesisView] = Field(default_factory=list)
    generated_by: str = "heuristic"
    tokens_used: int | None = None
    duration_ms: int | None = None
    created_at: datetime
    raw: dict[str, Any] = Field(default_factory=dict)


class InvestigateResponse(BaseModel):
    report_id: str
    incident_id: str
    status: str  # running | completed | failed
    started_at: datetime
    report: InvestigationReportView
