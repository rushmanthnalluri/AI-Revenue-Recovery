"""Recovery schemas — opportunities, plans (strategy candidates), and the
approve/reject/escalate/execute/cancel action contracts."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.ports import ActionType, PolicyOutcome, RecoveryStatus
from app.schemas.common import Paginated


class OpportunitySummary(BaseModel):
    id: str
    incident_id: str | None = None
    payment_id: str | None = None
    customer_id: str | None = None
    subscription_id: str | None = None
    opportunity_type: str
    status: RecoveryStatus
    amount_paise: int
    currency: str = "INR"
    expected_recovery_paise: int = 0
    confidence: float = 0.0
    risk: str = "low"
    reason: str | None = None
    created_at: datetime
    expires_at: datetime | None = None
    # --- additive (backwards compatible) ---
    environment: str = "research"  # real_test | research


class OpportunityListResponse(Paginated[OpportunitySummary]):
    pass


class OpportunityDetail(OpportunitySummary):
    constraints: dict[str, Any] = Field(default_factory=dict)
    actions: list["RecoveryActionView"] = Field(default_factory=list)
    audit: list["AuditRef"] = Field(default_factory=list)


class StrategyOption(BaseModel):
    id: str
    action_type: ActionType
    rank: int = 0
    expected_recovery_paise: int = 0
    confidence: float = 0.0
    risk: str = "low"
    eligibility: bool = True
    reason: str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    generated_by: str = "heuristic"
    selected: bool = False


class PolicyPreview(BaseModel):
    outcome: PolicyOutcome
    reasons: list[str] = Field(default_factory=list)


class RecoveryPlan(BaseModel):
    opportunity_id: str
    strategies: list[StrategyOption] = Field(default_factory=list)
    recommended_strategy_id: str | None = None
    policy_preview: PolicyPreview | None = None


class PolicyDecisionView(BaseModel):
    id: str
    outcome: PolicyOutcome
    reasons: list[str] = Field(default_factory=list)
    rules_matched: list[str] = Field(default_factory=list)
    policy_version: str = "unknown"
    decided_at: datetime


class AuditRef(BaseModel):
    """One append-only audit trail row referenced by a recovery resource."""

    id: str
    actor: str
    action: str
    entity_type: str
    entity_id: str
    request_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class RecoveryActionView(BaseModel):
    id: str
    opportunity_id: str
    strategy_id: str | None = None
    action_type: ActionType
    status: RecoveryStatus
    amount_paise: int
    currency: str = "INR"
    confidence: float = 0.0
    actor: str
    attempts: int = 0
    gateway_request_id: str | None = None
    policy_decision: PolicyDecisionView | None = None
    proposed_at: datetime
    executed_at: datetime | None = None
    verified_at: datetime | None = None
    completed_at: datetime | None = None
    approved_by: str | None = None
    note: str | None = None
    last_error: str | None = None


class ActionOutcomeCellView(BaseModel):
    action_type: str
    failure_class: str
    failure_class_source: str
    n_executed: int
    n_recovered: int
    n_failed: int
    n_unknown: int
    rate_recovered: float | None = None
    wilson_low: float
    wilson_high: float
    low_confidence: bool
    sample_confidence: float


class OrganicOutcomeCellView(BaseModel):
    failure_class: str
    n_failed_payments: int
    n_self_captured: int
    rate_organic: float | None = None
    wilson_low: float
    wilson_high: float
    low_confidence: bool
    sample_confidence: float


class IncrementalOutcomeCellView(BaseModel):
    action_type: str
    failure_class: str
    action_rate: float | None = None
    organic_rate: float | None = None
    incremental: float | None = None
    clamped: float
    ci_low: float
    ci_high: float
    inconclusive: bool


class RecoveryOutcomeRatesResponse(BaseModel):
    environment: str
    window_start: datetime
    window_end: datetime
    provenance: str
    min_cell: int
    cells: list[ActionOutcomeCellView] = Field(default_factory=list)
    organic: list[OrganicOutcomeCellView] = Field(default_factory=list)
    incremental: list[IncrementalOutcomeCellView] = Field(default_factory=list)


# --- mutating requests ------------------------------------------------------

class ApproveRequest(BaseModel):
    actor: str = "human:unknown"
    note: str | None = None


class RejectRequest(BaseModel):
    actor: str = "human:unknown"
    reason: str


class EscalateRequest(BaseModel):
    actor: str = "human:unknown"
    reason: str


class ExecuteRequest(BaseModel):
    strategy_id: str | None = None  # defaults to the recommended strategy
    actor: str = "human:unknown"


class CancelRequest(BaseModel):
    actor: str = "human:unknown"
    reason: str | None = None


class ActionResponse(BaseModel):
    action_id: str | None = None
    opportunity_id: str
    status: RecoveryStatus
    message: str = ""
    policy_decision: PolicyDecisionView | None = None


# --- opportunity builder (incident -> opportunities) -------------------------

class BuildRequest(BaseModel):
    incident_id: str
    actor: str = "system:builder"


class BuildResponse(BaseModel):
    incident_id: str
    created_count: int = 0
    existing_count: int = 0
    opportunities: list[OpportunitySummary] = Field(default_factory=list)


# --- reconciliation sweep (ADR 0011) -----------------------------------------


class ReconcileRequest(BaseModel):
    actor: str = "human:operator"


class ReconcileResponse(BaseModel):
    """One sweep's report. `webhooks_reprocessed` counts events now
    processed=true; `webhooks_still_failing` remain unprocessed;
    `webhooks_dead_lettered` counts events older than 24h that were
    permanently marked processed to stop retry loops."""

    sweep_id: str
    unknown_scanned: int = 0
    resolved: int = 0
    still_unknown: int = 0
    webhooks_reprocessed: int = 0
    webhooks_still_failing: int = 0
    webhooks_dead_lettered: int = 0
