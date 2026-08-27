"""Incident schemas — list/detail, timeline, evidence, diagnosis."""

from datetime import datetime
from typing import Any, Literal

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


class InsightsOutlierView(BaseModel):
    """One failure facet overrepresented in the incident window vs baseline.

    basis=failure_rate: rate = within-group failure rate (method/bank/gateway).
    basis=failure_share: rate = share of all failures (error_code/error_reason).
    lift=null means the facet was absent at baseline ("new") and ranks first.
    """

    dimension: str
    value: str
    basis: Literal["failure_rate", "failure_share"]
    incident_rate: float
    baseline_rate: float
    lift: float | None = None
    support: int
    window_group_size: int
    baseline_group_size: int
    low_confidence: bool = False


class PlatformCalloutView(BaseModel):
    """Merchant-vs-network benchmark of the top outlier (Pagos pattern).

    platform_scope documents the deployment reality: this is the
    single-merchant simulator, so "platform" = the simulated fleet (this
    merchant's full payment stream). In a multi-merchant deployment the same
    comparison would benchmark against all merchants on the platform.
    """

    dimension: str
    value: str
    classification: Literal["platform_wide", "incident_specific"]
    platform_scope: str = "simulated_fleet"
    platform_window_rate: float
    platform_baseline_rate: float
    platform_lift: float | None = None
    platform_support: int
    summary: str


class InsightsComputedFrom(BaseModel):
    """Provenance windows + support behind the insights numbers."""

    window_start: datetime
    window_end: datetime
    baseline_start: datetime
    baseline_end: datetime
    segment: dict[str, str] = Field(default_factory=dict)
    window_payments: int = 0
    window_failures: int = 0
    baseline_payments: int = 0
    baseline_failures: int = 0


class IncidentInsightsView(BaseModel):
    """Decline-outlier diagnostics: ranked facets + platform callout."""

    outliers: list[InsightsOutlierView] = Field(default_factory=list)
    platform_callout: PlatformCalloutView | None = None
    computed_from: InsightsComputedFrom


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
    insights: IncidentInsightsView | None = None
