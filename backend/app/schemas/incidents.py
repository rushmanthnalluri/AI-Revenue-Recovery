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


class IncidentDetail(IncidentSummary):
    description: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    resolved_at: datetime | None = None
    root_cause: str | None = None
    timeline: list[IncidentTimelineEvent] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    diagnosis: DiagnosisView | None = None
