"""Demo schemas — judge-facing scenario triggers and environment reset."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DemoResetResponse(BaseModel):
    status: str = "ok"
    cleared: dict[str, int] = Field(default_factory=dict)  # table -> rows deleted
    reset_at: datetime | None = None
    # --- additive ---
    # What was deliberately kept (evaluation_runs / experiments / audit trail).
    kept: list[str] = Field(default_factory=list)
    audit_id: str | None = None  # the one audit_logs row recording this reset


class ScenarioInfo(BaseModel):
    name: str
    description: str
    expected_incident_metric: str | None = None


class ScenarioListResponse(BaseModel):
    scenarios: list[ScenarioInfo] = Field(default_factory=list)


class ScenarioTriggerResponse(BaseModel):
    scenario: str
    status: str  # started | completed | failed
    simulator_run_id: str | None = None
    incident_id: str | None = None
    detail: str | None = None
    # --- additive ---
    skipped: bool = False  # True when the identical run was already seeded
    stats: dict[str, Any] = Field(default_factory=dict)  # simulator run stats
    detection: dict[str, Any] | None = None  # one anchored detection pass summary
