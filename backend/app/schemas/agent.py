"""Agent schemas — AI investigation of incidents (advisory only; the reasoner
never executes financial actions)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class InvestigateRequest(BaseModel):
    force_refresh: bool = False  # re-investigate even if a report exists


class InvestigateResponse(BaseModel):
    report_id: str
    incident_id: str
    status: str  # running | completed | failed
    started_at: datetime


class HypothesisView(BaseModel):
    title: str
    confidence: float
    supporting_evidence: list[str] = Field(default_factory=list)


class InvestigationReportView(BaseModel):
    id: str
    incident_id: str
    summary: str
    hypotheses: list[HypothesisView] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    generated_by: str = "heuristic"
    tokens_used: int | None = None
    duration_ms: int | None = None
    created_at: datetime
    raw: dict[str, Any] = Field(default_factory=dict)
