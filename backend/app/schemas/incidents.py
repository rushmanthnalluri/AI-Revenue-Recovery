"""Incident schemas — list/detail, timeline, evidence, diagnosis."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.ports import IncidentStatus, Severity
from app.schemas.common import Paginated


class IncidentSummary(BaseModel):
    id: str
    title: str
    status: IncidentStatus
    severity: Severity
    metric: str
    detection_method: str
    detected_at: datetime
    baseline_value: float | None = None
    observed_value: float | None = None
    deviation_pct: float | None = None
    affected_payments_count: int = 0
    revenue_at_risk_paise: int = 0
    currency: str = "INR"


class IncidentListResponse(Paginated[IncidentSummary]):
    pass


class IncidentTimelineEvent(BaseModel):
    ts: datetime
    kind: str  # detected | status_change | evidence_added | diagnosis | action | note
    summary: str
    actor: str = "system"
    details: dict[str, Any] = Field(default_factory=dict)


class EvidenceItem(BaseModel):
    id: str
    evidence_type: str
    title: str
    payload: dict[str, Any] = Field(default_factory=dict)
    collector: str = "agent:investigator"
    collected_at: datetime


class DiagnosisView(BaseModel):
    id: str
    model_name: str
    model_version: str
    predicted_cause: str
    confidence: float
    explanation: str | None = None
    created_at: datetime


class EstimateView(BaseModel):
    """Flattened app.services.revenue.types.Estimate (integer paise + band)."""

    point_paise: int | None = None
    lower_paise: int = 0
    upper_paise: int = 0
    confidence: float = 0.0
    low_confidence: bool = True
    basis: str = ""


class FailureClassView(BaseModel):
    failure_class: str
    failed_count: int
    failed_amount_paise: int
    allocated_loss: EstimateView
    recoverability_factor: float
    recoverable: EstimateView


class RevenueBreakdown(BaseModel):
    """RevenueService.revenue_at_risk flattened for the incident detail page.

    observed_loss / recoverable are counterfactual ESTIMATES with bands;
    actual_recovered_paise is measured from webhook-verified actions only.
    """

    currency: str = "INR"
    window_start: datetime
    window_end: datetime
    baseline_start: datetime
    baseline_end: datetime
    observed_loss: EstimateView
    recoverable: EstimateView
    expected_recovery_by_strategy: dict[str, EstimateView] = Field(default_factory=dict)
    actual_recovered_paise: int = 0
    recovered_actions_count: int = 0
    failure_classes: list[FailureClassView] = Field(default_factory=list)


class IncidentDetail(IncidentSummary):
    description: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    resolved_at: datetime | None = None
    root_cause: str | None = None
    timeline: list[IncidentTimelineEvent] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    diagnosis: DiagnosisView | None = None
    # --- additive (backwards compatible) ---
    segment: dict[str, str] = Field(default_factory=dict)
    simulator_run_id: str | None = None
    opportunities_count: int = 0
    recovery_actions_count: int = 0
    revenue: RevenueBreakdown | None = None
