"""Structured investigation report models — the canonical shape every reasoner
produces, the LLM output schema, and the API response payload.

The separation is deliberate and load-bearing for the UI:
- ``observed_facts``  — deterministic tool output, each citing the tool name
  and the DB evidence ids it came from. Never AI text.
- ``ai_inferences``   — probabilistic claims, each labeled with confidence and
  references to the observed facts that support it. Advisory only.
- ``recommended_actions`` / ``recommended_next_step`` — proposals whose
  ``policy_preview`` is attached by the SYSTEM from a real PolicyEngine
  evaluation, never by the reasoner.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Below this report confidence the investigation is escalated to a human.
ESCALATION_CONFIDENCE_THRESHOLD = 0.5

HEURISTIC_REASONER_VERSION = "heuristic-1.0"
LLM_REASONER_VERSION = "llm-1.0"


class ObservedFact(BaseModel):
    """A deterministic fact from one tool call, with evidence pointers."""

    model_config = ConfigDict(extra="forbid")

    id: str  # "f1", "f2", ... — referenced by AiInference.supporting_fact_ids
    statement: str
    tool: str  # whitelisted tool name the fact came from
    evidence_ids: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class AiInference(BaseModel):
    """A probabilistic claim. Advisory only — never an execution path."""

    model_config = ConfigDict(extra="forbid")

    id: str  # "i1", "i2", ...
    statement: str
    label: str = "inference"  # root_cause | failure_nature | recoverability | ...
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_fact_ids: list[str] = Field(default_factory=list)


class AlternativeHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1)
    cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    source: str = "diagnosis"


class PolicyOutcomeView(BaseModel):
    """A policy decision, verbatim, as attached by the system (never by AI)."""

    model_config = ConfigDict(extra="forbid")

    outcome: str  # ALLOWED | BLOCKED | REQUIRES_APPROVAL
    reasons: list[str] = Field(default_factory=list)
    rules_matched: list[str] = Field(default_factory=list)
    policy_version: str = "unknown"


class RecommendedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: str
    rationale: str
    amount_paise: int | None = None
    currency: str = "INR"
    payment_id: str | None = None
    opportunity_id: str | None = None
    expected_recovery_paise: int | None = None
    policy_preview: PolicyOutcomeView | None = None


class RevenueImplications(BaseModel):
    """All values copied from the revenue engine's tool result."""

    model_config = ConfigDict(extra="forbid")

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


class InvestigationOutput(BaseModel):
    """The full structured report persisted in agent_reports.output."""

    model_config = ConfigDict(extra="forbid")

    incident_id: str
    what_happened: str
    observed_facts: list[ObservedFact] = Field(default_factory=list)
    ai_inferences: list[AiInference] = Field(default_factory=list)
    alternative_hypotheses: list[AlternativeHypothesis] = Field(default_factory=list)
    revenue_implications: RevenueImplications | None = None
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    recommended_next_step: RecommendedAction | None = None
    uncertainties: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    escalated: bool = False
    escalation_reasons: list[str] = Field(default_factory=list)
    degraded: bool = False
    degraded_reasons: list[str] = Field(default_factory=list)
    stripped_claims: list[dict[str, Any]] = Field(default_factory=list)
    tools_called: list[str] = Field(default_factory=list)
    reasoner: str = "heuristic"  # heuristic | llm
    generated_by: str = "heuristic"  # "heuristic" or the LLM model name
    reasoner_version: str = HEURISTIC_REASONER_VERSION
    diagnosis: dict[str, Any] | None = None


__all__ = [
    "ESCALATION_CONFIDENCE_THRESHOLD",
    "HEURISTIC_REASONER_VERSION",
    "LLM_REASONER_VERSION",
    "AiInference",
    "AlternativeHypothesis",
    "InvestigationOutput",
    "ObservedFact",
    "PolicyOutcomeView",
    "RecommendedAction",
    "RevenueImplications",
]
